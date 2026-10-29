"""Assemble graph_index.json from a corpus root.

Contract: FactLattice SPEC.md §3 (graph index schema), [LTS-INDEX] shape:

  node_map          : id -> node  (SPEC §1.1 fields)
  adjacency         : per-layer FORWARD edge lists
  reverse_adjacency : per-layer REVERSE edge lists  (REQUIRED — the
                      highest-value 'what points AT this?' query)
  source_corpus     : {corpus_id, domain_suffix, root_paths[], files_hash,
                       node_count, edge_count, built_at, factlattice_profile_version,
                       commit_ref, snapshot_hash, parent_snapshot_version,
                       parent_snapshot_hash}

Determinism: pure over (corpus content, injected built_at, injected provenance).
No datetime.now() inside the build — `built_at` and provenance fields are passed
in (Phase-1 provenance carried as placeholders; the mechanism is a later phase,
per the task contract).

Duplicate/colliding node IDs across files are PRESERVED (never silently dropped):
the first definition wins node_map, and every collision is recorded under
`id_collisions` so a pre-finalization corpus surfaces them instead of losing data.
Dangling edges (target absent from node_map) are FLAGGED, not dropped.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Dict, List, Optional

from .constants import FACTLATTICE_PROFILE_VERSION, GENERATOR
from .hashing import files_hash, list_lts_files
from .parser import (
    CANONICAL_LAYERS,
    DEFAULT_REGISTERED_NS,
    is_profile_document,
    parse_file,
)

SCHEMA_VERSION = "1.0"



def build_alias_index(node_map: Dict[str, dict]) -> Dict[str, str]:
    """alias id -> canonical node id, from each node's `properties["aliases"]` ([LTS-PARSER] alias-form).

    THE ONE definition of the dangling check's resolution namespace: `resolvable = node ids | this index's keys`. The index
    builder, the incremental builder and the reviewer's `dangles` helper all call it, so a reference resolves the same way in
    every one of them. A real node id always wins over an alias claim (no silent clobber)."""
    alias_index: Dict[str, str] = {}
    for cid, nd in node_map.items():
        for alias in (nd.get("properties") or {}).get("aliases") or []:
            if alias not in node_map and alias not in alias_index:
                alias_index[alias] = cid
    return alias_index

def build_index(
    corpus_root: str,
    corpus_id: str = "example",
    domain_suffix: str = "demo.example",
    built_at: Optional[str] = None,
    registered_ns=None,
    profile_layers: Optional[Dict[str, str]] = None,
    domain_verbs: Optional[frozenset] = None,
    property_edges: Optional[Dict[str, tuple]] = None,
    config: Optional[object] = None,
    commit_ref: Optional[str] = None,
    snapshot_version: Optional[int] = None,
    snapshot_hash: Optional[str] = None,
    parent_snapshot_version: Optional[int] = None,
    parent_snapshot_hash: Optional[str] = None,
) -> dict:
    """Parse every .lts under corpus_root and assemble the graph index dict.

    `built_at` MUST be supplied by the caller (an injectable clock); it is never
    read from the wall clock here so the function stays pure/reproducible.
    Provenance fields (commit_ref/snapshot_*) are Phase-1 placeholders carried in
    the schema — no provenance mechanism is invented here (later phase).

    ⛔ `config=` (a `ParserConfig`) is the supported vocabulary input as of Era 1/C3.
    Before it, this function ACCEPTED `profile_layers` and `domain_verbs` and had NO
    `property_edges` PARAMETER AT ALL — so a profile-registered minting key could not
    reach a parse from here even in principle, and nothing failed, because a parameter
    that does not exist is not a parameter anyone forgot to pass (review V5). The loose
    kwargs are retained for one deprecation release.
    """
    from .parser_config import ParserConfig

    if config is None:
        config = ParserConfig.from_kwargs(
            registered_ns=registered_ns, profile_layers=profile_layers,
            domain_verbs=domain_verbs, property_edges=property_edges)
    elif (registered_ns is not None or profile_layers is not None
          or domain_verbs is not None or property_edges is not None):
        raise TypeError("pass EITHER config= OR the loose vocabulary kwargs, not both")

    rel_files = list_lts_files(corpus_root)

    node_map: Dict[str, dict] = {}
    id_collisions: List[dict] = []
    all_edges: List[dict] = []
    parse_errors: List[dict] = []
    profile_documents: List[str] = []

    for rel in rel_files:
        abs_path = os.path.join(corpus_root, rel)
        try:
            parsed = parse_file(abs_path, rel_source_file=rel, config=config)
        except Exception as exc:  # never crash the whole build on one bad file
            parse_errors.append({"source_file": rel, "error": repr(exc)})
            continue
        # A domain profile is VOCABULARY, not CONTENT. It is parsed in full (above) and
        # loaded as a profile elsewhere (`factlattice.profile.load_profile`); only its
        # contribution to this content graph is dropped — and RECORDED, never silent.
        # See `parser.is_profile_document` for why the file's own doctag is the signal.
        if is_profile_document(parsed):
            profile_documents.append(rel)
            continue
        for node in parsed.nodes:
            nd = node.to_dict()
            if node.id in node_map:
                id_collisions.append({
                    "id": node.id,
                    "kept_source_file": node_map[node.id]["source_file"],
                    "kept_line_range": node_map[node.id]["line_range"],
                    "collision_source_file": nd["source_file"],
                    "collision_line_range": nd["line_range"],
                })
                # First definition wins; collision preserved in id_collisions.
                continue
            node_map[node.id] = nd
        for edge in parsed.edges:
            all_edges.append(edge.to_dict())

    # --- alias resolution ([LTS-PARSER] MULTI-ID CO-DEF alias-form) -------
    # A node collapsed from an explicit alias-form def (`- [A] [B] **… aka …**`) carries its
    # other id(s) in properties["aliases"]. A ref to an alias id must RESOLVE to the one
    # canonical node, not dangle. Build a resolution namespace = canonical ids + alias ids
    # (each alias -> its canonical), used ONLY for the dangling check + exposed for consumers.
    # Aliases are NOT added to node_map (total_nodes stays the count of real nodes).
    alias_index = build_alias_index(node_map)
    resolvable = set(node_map.keys()) | set(alias_index.keys())

    # --- dangling flagging pass (needs the whole-corpus node set) ----------
    dangling_count = 0
    unregistered_ns_count = 0
    unresolved_target_count = 0
    for ed in all_edges:
        if ed["target"] not in resolvable:
            ed["dangling"] = True
            dangling_count += 1
        if "unregistered_ns" in ed["flags"]:
            unregistered_ns_count += 1
        if "unresolved_target" in ed["flags"]:
            unresolved_target_count += 1

    # --- per-layer adjacency + reverse_adjacency ---------------------------
    layers: Dict[str, dict] = {}
    edges_by_layer: Dict[str, int] = {}
    for layer in CANONICAL_LAYERS:
        layer_edges = [e for e in all_edges if e["layer"] == layer]
        adjacency: Dict[str, List[str]] = {}
        reverse: Dict[str, List[str]] = {}
        for e in layer_edges:
            adjacency.setdefault(e["source"], []).append(e["target"])
            reverse.setdefault(e["target"], []).append(e["source"])
        layers[layer] = {
            "edges": layer_edges,
            "adjacency": adjacency,
            "reverse_adjacency": reverse,
        }
        edges_by_layer[layer] = len(layer_edges)

    fhash, file_hashes = files_hash(corpus_root)

    index = {
        "version": SCHEMA_VERSION,
        "generator": GENERATOR,
        "generated_at": built_at,
        "source_corpus": {
            "corpus_id": corpus_id,
            "domain_suffix": domain_suffix,
            "root_paths": [os.path.abspath(corpus_root)],
            "files_hash": fhash,
            "file_hashes": file_hashes,
            "node_count": len(node_map),
            "edge_count": len(all_edges),
            "built_at": built_at,
            "factlattice_profile_version": FACTLATTICE_PROFILE_VERSION,
            # --- provenance chain (Phase-1 placeholders; mechanism = later phase) ---
            # TODO(phase-2): populate from the Builder-signed ratified-commit
            # attestation ([INV-LTS-PROVENANCE]); left null/injectable for now.
            "commit_ref": commit_ref,
            "snapshot_hash": snapshot_hash,
            "parent_snapshot_version": parent_snapshot_version,
            "parent_snapshot_hash": parent_snapshot_hash,
        },
        "snapshot_version": snapshot_version,
        "stats": {
            "total_nodes": len(node_map),
            "total_edges": len(all_edges),
            "edges_by_layer": edges_by_layer,
            "dangling_edges": dangling_count,
            "unregistered_ns_edges": unregistered_ns_count,
            "unresolved_target_edges": unresolved_target_count,
            "id_collisions": len(id_collisions),
            "parse_errors": len(parse_errors),
            "profile_documents": len(profile_documents),
        },
        "nodes": node_map,
        "layers": layers,
        "id_collisions": id_collisions,
        "parse_errors": parse_errors,
        # Files excluded from the CONTENT graph because they declare themselves domain
        # profiles (`document_tag: PROFILE`). Recorded so the exclusion is auditable —
        # a reader can always see WHICH files the graph does not speak for.
        "profile_documents": profile_documents,
        "alias_index": alias_index,   # alias id -> canonical node id ([LTS-PARSER] alias-form)
    }
    return index


def compute_snapshot_hash(index: dict) -> str:
    """SHA-256 over the canonical JSON of the index EXCLUDING the snapshot_hash
    field (SPEC §3.2: hash of graph_index.json content excluding this field)."""
    clone = json.loads(json.dumps(index))
    if "source_corpus" in clone:
        clone["source_corpus"]["snapshot_hash"] = None
    payload = json.dumps(clone, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_index_atomic(index: dict, out_path: str) -> None:
    """Write the index to out_path ATOMICALLY (temp file + os.rename).

    A torn/interrupted rebuild never leaves a half-written index the query path
    could read ([LTS-WATCHDOG] torn-rebuild-stability). The temp file is
    created in the SAME directory as out_path so os.rename is atomic (same mount).
    """
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=out_dir, prefix=".graph_index.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(index, fh, indent=2, sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, out_path)  # atomic on POSIX + Windows
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
