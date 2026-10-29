"""[LEC-SELF-HEAL] — the SURGICAL re-index: re-parse only the files that changed.

============================================================================
WHY THIS EXISTS
============================================================================
[LEC-FRESHNESS] (phase 2) made DETECTION surgical: a 1-file edit in a 300-file corpus
re-hashes ONE leaf, and `Partition.freshness()` becomes an O(1) compare. [LEC-SELF-HEAL]
makes the RESPONSE surgical too. Rebuilding 300 files because 1 changed would re-introduce
exactly the cost class phase 2 removed, one layer up — on the corpus that is MOST actively
written (the decision graph). So the re-index re-parses the changed file(s) and NOTHING
else.

============================================================================
WHY A PER-FILE SPLICE IS SAFE (the determinism argument)
============================================================================
The parser is FILE-LOCAL: `parse_file(path)` is a pure function of that file's bytes. It
has no visibility into any other file. EVERY cross-file property of the index —

    * node_map assembly + first-definition-wins id collisions
    * the alias index
    * the `dangling` flag on every edge (target resolvable in the corpus?)
    * per-layer edge lists / adjacency / reverse adjacency
    * all stats

— is a pure fold over the per-file parse results. So this module caches the per-file
parse results and RECOMPUTES THE ENTIRE FOLD on every `assemble()`. Nothing is spliced
into a previously-resolved structure; only the PARSE is incremental.

That closes the failure mode that would otherwise be fatal here: a node removed or renamed
in file A leaving edges from untouched files pointing at nothing. Because the dangling pass
is re-run over ALL cached edges against the NEW resolvable set, an id that disappears
re-flags every inbound edge dangling, and an id that appears un-flags every previously
dangling edge that now resolves. There is no "targeted re-resolution" heuristic to get
wrong — the resolution is total, it is just cheap (it is in-memory list/dict work, no I/O).

Consequence: `assemble()` after N incremental file updates is byte-identical to a cold
`factlattice.build_index()` over the same content. `tests/test_self_heal.py` asserts that
parity directly (it is THE safety net for this module).

============================================================================
THE CACHE INVARIANT (self-certifying)
============================================================================
    leaves[rel] == file_content_hash(the bytes this cache's parse of `rel` was taken from)

`_parse` hashes the file BEFORE and AFTER `parse_file` and refuses (raises `CorpusMoved`)
if the two differ — so a file rewritten DURING the re-parse can never leave the cache
claiming a parse it does not hold. The caller (`selfheal`) treats that as a fail-closed
refusal and retries on the next query, when the corpus has settled.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Sequence, Tuple

from factlattice import hashing as _hashing
from factlattice import parser as _parser
from factlattice.hashing import list_lts_files
from factlattice.index_builder import build_alias_index
from factlattice.parser import (
    CANONICAL_LAYERS,
    DEFAULT_REGISTERED_NS,
    is_profile_document,
)

from .kernel import combine_leaf_map, combine_roots


class CorpusMoved(RuntimeError):
    """The corpus content changed WHILE the re-index was reading it.

    A loud abort, never a silent partial: the caller must refuse (fail-closed) and let the
    next query heal against the settled content. [LEC-SELF-HEAL] invariant 'never serve
    stale' — an index assembled from a moving corpus cannot honestly claim any content hash.
    """


# ===========================================================================
# ONE root — the incremental parse cache
# ===========================================================================
class RootIndex:
    """Per-root parse cache that reproduces `factlattice.build_index`'s nodes/layers
    EXACTLY while re-parsing only the files whose content hash moved.

    `parse_ops` is the proof instrument (mirrors `FreshnessKernel.hash_ops`): it counts
    `parse_file` invocations, so a test can assert that a 1-file edit re-parsed 1 file.
    """

    def __init__(self, root_path: str, registered_ns=None,
                 profile_layers=None, config=None) -> None:
        self.root_path = os.path.abspath(root_path)
        # ⛔ [C3, Era 1] ONE vocabulary object. Before this, THIS path threaded
        # `registered_ns` + `profile_layers` and NOT `domain_verbs` / `property_edges`
        # — a different subset from every other production path, which is review V5b:
        # the same (document, profile) pair produced a different graph depending on
        # which entry point served it. The loose kwargs are kept for one deprecation
        # release and are folded into the same object here.
        from factlattice.parser_config import ParserConfig
        if config is None:
            config = ParserConfig.from_kwargs(registered_ns=registered_ns,
                                              profile_layers=profile_layers)
        self.config = config
        self.registered_ns = config.registered_ns      # back-compat read-only views
        self.profile_layers = dict(config.profile_layers)
        # rel -> parsed products (the cache). A file that failed to parse is present with
        # empty node/edge lists plus an entry in `_errors` (build_index's behaviour).
        self._nodes: Dict[str, List[dict]] = {}
        self._edges: Dict[str, List[dict]] = {}
        self._dangling0: Dict[str, List[bool]] = {}   # parser's own dangling verdict
        self._errors: Dict[str, dict] = {}
        # rels whose file declares itself a domain profile (`document_tag: PROFILE`).
        # STILL PARSED and still cached+hash-certified (the cache must certify every file
        # it read, or freshness lies); simply omitted from `assemble`'s content fold —
        # exactly what `factlattice.build_index` does. See `parser.is_profile_document`.
        self._profile_docs: set = set()
        # rel -> content hash of the bytes the cached parse was taken from.
        self.leaves: Dict[str, str] = {}
        self.parse_ops = 0                            # proof instrument
        self.seeded = False

    @staticmethod
    def _hash(abs_path: str, rel: str) -> str:
        """`file_content_hash` through the MODULE (so a spy still counts), a refusal re-labelled with the root-relative
        path ([ISSUE-NONREGULAR-MEMBER])."""
        try:
            return _hashing.file_content_hash(abs_path)
        except _hashing.CorpusFileError as exc:
            raise exc.set_rel(rel) from None

    # --- parsing (the ONLY place a corpus file is parsed) -------------------
    def _parse(self, rel: str) -> None:
        """Parse ONE file into the cache, certifying the content hash it came from.

        Hash -> parse -> hash. A mismatch means the file was rewritten mid-read: raise
        `CorpusMoved` rather than cache a parse we cannot attribute to any content hash.
        `parse_file` (not `parse_text`) is used deliberately so newline/encoding handling
        is bit-for-bit what `build_index` does — determinism parity is load-bearing here.
        """
        abs_path = os.path.join(self.root_path, rel)
        # Called through the MODULE (not a from-import) so the same spy phase-2 uses to
        # count content hashes also sees the ones the re-index performs.
        h_before = self._hash(abs_path, rel)              # OSError propagates (loud),
        self.parse_ops += 1                               # as `files_hash` would have
        try:
            parsed = _parser.parse_file(abs_path, rel_source_file=rel,
                                        config=self.config)
        except Exception as exc:  # noqa: BLE001 — one bad file never kills the build
            self._errors[rel] = {"source_file": rel, "error": repr(exc)}
            self._nodes[rel] = []
            self._edges[rel] = []
            self._dangling0[rel] = []
            self.leaves[rel] = h_before
            return
        h_after = self._hash(abs_path, rel)
        if h_after != h_before:
            raise CorpusMoved(f"{rel!r} changed while being re-indexed "
                              f"({h_before} -> {h_after})")
        self._errors.pop(rel, None)
        self._nodes[rel] = [n.to_dict() for n in parsed.nodes]
        edges = [e.to_dict() for e in parsed.edges]
        self._edges[rel] = edges
        self._dangling0[rel] = [bool(e.get("dangling")) for e in edges]
        self.leaves[rel] = h_before
        # Re-evaluated on EVERY parse (both directions): a file that gains the PROFILE
        # doctag leaves the content graph on the next heal, and one that loses it
        # re-enters. A one-way set would make the classification stick to a stale edit.
        if is_profile_document(parsed):
            self._profile_docs.add(rel)
        else:
            self._profile_docs.discard(rel)

    def _forget(self, rel: str) -> None:
        self._nodes.pop(rel, None)
        self._edges.pop(rel, None)
        self._dangling0.pop(rel, None)
        self._errors.pop(rel, None)
        self.leaves.pop(rel, None)
        self._profile_docs.discard(rel)

    # --- seeding / refreshing ----------------------------------------------
    def full_build(self) -> int:
        """Parse EVERY .lts under the root (the cold path). Returns files parsed."""
        rels = list_lts_files(self.root_path)
        for stale_rel in set(self._nodes) - set(rels):
            self._forget(stale_rel)
        for rel in rels:
            self._parse(rel)
        self.seeded = True
        return len(rels)

    def refresh(self, live_leaves: Dict[str, str]) -> List[str]:
        """Re-parse ONLY the files whose authoritative content hash differs from the
        cache's. `live_leaves` is {rel: content_hash} taken by the freshness kernel
        (the same content oracle) — added / removed / modified are all derived from it.

        Returns the list of re-parsed rel paths (empty == nothing to do).
        """
        if not self.seeded:
            self.full_build()
            return sorted(self._nodes)
        live = set(live_leaves)
        cached = set(self._nodes)
        for rel in cached - live:                     # removed
            self._forget(rel)
        touched: List[str] = []
        for rel in sorted(live):                      # added + modified
            if self.leaves.get(rel) != live_leaves[rel]:
                self._parse(rel)
                touched.append(rel)
        return touched

    def root_hash(self) -> str:
        """The content-hash root over the cache's own certified leaves.

        Byte-identical to `factlattice.hashing.files_hash(root)[0]` for the same content
        — which is what makes it safe to stamp as the rebuilt index's `files_hash`.
        """
        return combine_leaf_map(self.leaves)

    # --- the fold (recomputed IN FULL every time — see module docstring) ----
    def assemble(self) -> dict:
        """Reproduce `build_index`'s cross-file fold from the cached per-file parses.

        Returns {nodes, layers, alias_index, id_collisions, parse_errors, stats}, the
        subset of the index schema that is a pure function of corpus content.
        """
        rels = sorted(self._nodes)

        node_map: Dict[str, dict] = {}
        id_collisions: List[dict] = []
        all_edges: List[dict] = []
        edge_dangling0: List[bool] = []
        parse_errors: List[dict] = []

        profile_documents: List[str] = []

        for rel in rels:
            if rel in self._errors:
                parse_errors.append(self._errors[rel])
                continue                              # build_index: `continue` on error
            if rel in self._profile_docs:
                profile_documents.append(rel)
                continue                     # vocabulary, not content — as build_index
            for nd in self._nodes[rel]:
                nid = nd["id"]
                if nid in node_map:
                    id_collisions.append({
                        "id": nid,
                        "kept_source_file": node_map[nid]["source_file"],
                        "kept_line_range": node_map[nid]["line_range"],
                        "collision_source_file": nd["source_file"],
                        "collision_line_range": nd["line_range"],
                    })
                    continue                          # first definition wins
                node_map[nid] = nd
            all_edges.extend(self._edges[rel])
            edge_dangling0.extend(self._dangling0[rel])

        # --- alias resolution ([LTS-PARSER] MULTI-ID CO-DEF alias-form) -----
        alias_index = build_alias_index(node_map)
        resolvable = set(node_map) | set(alias_index)

        # --- dangling pass, TOTAL over every cached edge --------------------
        # This is the cross-file re-resolution: an id that appeared/disappeared in ONE
        # file re-flags inbound edges from files that were never re-parsed.
        dangling_count = 0
        unregistered_ns_count = 0
        unresolved_target_count = 0
        for ed, orig in zip(all_edges, edge_dangling0):
            if ed["target"] not in resolvable:
                ed["dangling"] = True
                dangling_count += 1
            else:
                ed["dangling"] = orig                 # restore the parser's own verdict
            flags = ed.get("flags") or ()
            if "unregistered_ns" in flags:
                unregistered_ns_count += 1
            if "unresolved_target" in flags:
                unresolved_target_count += 1

        # --- per-layer adjacency + reverse_adjacency ------------------------
        layers: Dict[str, dict] = {}
        edges_by_layer: Dict[str, int] = {}
        for layer in CANONICAL_LAYERS:
            layer_edges = [e for e in all_edges if e["layer"] == layer]
            adjacency: Dict[str, List[str]] = {}
            reverse: Dict[str, List[str]] = {}
            for e in layer_edges:
                adjacency.setdefault(e["source"], []).append(e["target"])
                reverse.setdefault(e["target"], []).append(e["source"])
            layers[layer] = {"edges": layer_edges, "adjacency": adjacency,
                             "reverse_adjacency": reverse}
            edges_by_layer[layer] = len(layer_edges)

        return {
            "nodes": node_map,
            "layers": layers,
            "alias_index": alias_index,
            "id_collisions": id_collisions,
            "parse_errors": parse_errors,
            "profile_documents": profile_documents,
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
        }


# ===========================================================================
# ONE corpus — the rebuild RECIPE + the per-root caches
# ===========================================================================
class CorpusIndex:
    """A partition's rebuild recipe (its labeled root specs) plus one `RootIndex` per
    root. Carried ON the Partition so `selfheal` can re-index it without re-deriving
    anything from `root_paths` (which has lost the `area` labels and `only_file` pins).

    The SAME object serves the cold build (`config.build_merged_partition`) and every
    subsequent heal, so healed output is identical to cold-start output BY CONSTRUCTION,
    not by a parallel implementation that has to be kept in sync.

    Counters (observable, asserted by tests):
        `full_rebuilds`        — times the whole corpus had to be re-parsed (the fallback)
        `incremental_rebuilds` — times only the changed files were re-parsed
        `parse_ops`            — total files parsed across all roots, ever
    """

    def __init__(self, corpus_id: str, domain_suffix: str,
                 root_specs: Sequence[dict],
                 registered_ns=None,
                 profile_layers=None, config=None) -> None:
        self.corpus_id = corpus_id
        self.domain_suffix = domain_suffix
        # Normalized copy — the recipe must survive independently of the caller's dict.
        self.root_specs: List[dict] = [dict(spec) for spec in root_specs]
        from factlattice.parser_config import ParserConfig
        if config is None:
            config = ParserConfig.from_kwargs(registered_ns=registered_ns,
                                              profile_layers=profile_layers)
        self.config = config
        self.registered_ns = config.registered_ns
        self.profile_layers = dict(config.profile_layers)
        self._roots: Dict[str, RootIndex] = {}        # abs root path -> RootIndex
        self.full_rebuilds = 0
        self.incremental_rebuilds = 0
        # ABS paths of the files excluded from the content fold as domain profiles,
        # refreshed on every `_materialize`. Recorded (not discarded) so the exclusion is
        # auditable and so the config-vs-doctag AGREEMENT CHECK
        # (`analytics.profile_registration`) costs nothing — the fact already exists here.
        self.profile_documents: List[str] = []

    # --- instruments --------------------------------------------------------
    @property
    def parse_ops(self) -> int:
        return sum(ri.parse_ops for ri in self._roots.values())

    def reset_parse_ops(self) -> None:
        for ri in self._roots.values():
            ri.parse_ops = 0

    @property
    def seeded(self) -> bool:
        return bool(self._roots) and all(ri.seeded for ri in self._roots.values())

    @property
    def cached_file_count(self) -> int:
        """Files currently held in the parse cache across all roots."""
        return sum(len(ri.leaves) for ri in self._roots.values())

    # --- root-set resolution (a spec whose path is absent is SKIPPED) -------
    def live_roots(self) -> List[Tuple[dict, str]]:
        """The root specs that currently resolve on disk, in declared order.

        The graceful skip `config._build_root_contribution` used to carry: a root dir that
        does not exist, or an `only_file` pin whose file is absent, contributes nothing
        (a registered-but-empty project), never an error.
        """
        out: List[Tuple[dict, str]] = []
        for spec in self.root_specs:
            path = spec["path"]
            if not os.path.isdir(path):
                continue
            only_file = spec.get("only_file")
            if only_file and not os.path.isfile(os.path.join(path, only_file)):
                continue
            out.append((spec, os.path.abspath(path)))
        return out

    def _root_index(self, abs_path: str) -> RootIndex:
        ri = self._roots.get(abs_path)
        if ri is None:
            ri = RootIndex(abs_path, config=self.config)
            self._roots[abs_path] = ri
        return ri

    def root_index_for(self, abs_path: str) -> Optional[RootIndex]:
        return self._roots.get(os.path.abspath(abs_path))

    # --- building -----------------------------------------------------------
    def cold_build(self, *, as_rebuild: bool = True,
                   ) -> Tuple[Dict[str, dict], Dict[str, dict], List[str], str]:
        """Parse every file under every live root. Returns (nodes, layers, root_paths,
        files_hash).

        `as_rebuild=False` marks the INITIAL build (config's cold start), which is not a
        self-heal fallback and so must not pollute the `full_rebuilds` counter that tests
        and operators read as "how often did the surgical path have to give up?".
        """
        if as_rebuild:
            self.full_rebuilds += 1
        live = self.live_roots()
        for _spec, abs_path in live:
            self._root_index(abs_path).full_build()
        for gone in set(self._roots) - {p for _s, p in live}:
            self._roots.pop(gone, None)
        return self._materialize(live)

    def incremental_build(self, live_leaves: Dict[str, Dict[str, str]],
                          ) -> Tuple[Dict[str, dict], Dict[str, dict], List[str], str,
                                     List[str]]:
        """Re-parse ONLY the files whose content hash moved, per `live_leaves`
        ({abs_root_path: {rel: content_hash}} — the freshness kernel's leaf maps).

        A root present on disk but ABSENT from `live_leaves` (i.e. one the freshness
        kernel is not tracking, because it appeared after the partition was seated) is a
        structural change this path cannot honestly cover: the caller checks the returned
        root_paths against the seated set and re-seats.

        Returns (nodes, layers, root_paths, files_hash, reparsed_rels).
        """
        self.incremental_rebuilds += 1
        live = self.live_roots()
        reparsed: List[str] = []
        for _spec, abs_path in live:
            ri = self._root_index(abs_path)
            leaves = live_leaves.get(abs_path)
            if leaves is None:
                ri.full_build()                       # untracked root -> parse it whole
                reparsed.extend(f"{abs_path}::{r}" for r in sorted(ri.leaves))
                continue
            for rel in ri.refresh(leaves):
                reparsed.append(f"{abs_path}::{rel}")
        for gone in set(self._roots) - {p for _s, p in live}:
            self._roots.pop(gone, None)
        nodes, layers, root_paths, fhash = self._materialize(live)
        return nodes, layers, root_paths, fhash, reparsed

    # --- the merge (identical to config.build_merged_partition's merge) -----
    def _materialize(self, live: List[Tuple[dict, str]],
                     ) -> Tuple[Dict[str, dict], Dict[str, dict], List[str], str]:
        merged_nodes: Dict[str, dict] = {}
        merged_layers: Dict[str, dict] = {}
        root_paths: List[str] = []
        per_root_hash: Dict[str, str] = {}
        profile_documents: List[str] = []
        for spec, abs_path in live:
            ri = self._roots[abs_path]
            assembled = ri.assemble()
            for rel in assembled.get("profile_documents") or []:
                profile_documents.append(os.path.join(abs_path, rel))
            nodes, layers = assembled["nodes"], assembled["layers"]
            only_file = spec.get("only_file")
            if only_file:
                nodes = {nid: nd for nid, nd in nodes.items()
                         if nd.get("source_file") == only_file}
                layers = {ln: {"edges": [e for e in ld.get("edges", [])
                                         if e.get("source_file") == only_file]}
                          for ln, ld in layers.items()}
            area = spec.get("area")
            if area is not None:
                for nd in nodes.values():
                    nd["area"] = area
            for nid, nd in nodes.items():
                merged_nodes.setdefault(nid, nd)      # first root wins on a colliding id
            for layer, ld in layers.items():
                merged_layers.setdefault(layer, {"edges": []})["edges"].extend(
                    ld.get("edges", []))
            root_paths.append(abs_path)
            per_root_hash[abs_path] = ri.root_hash()

        # Recompute per-layer adjacency over the MERGED (and only_file-filtered) edge set,
        # exactly as `build_index` does over its own. Keeping the full layer shape here
        # means a single-root partition healed through this path is byte-identical to a
        # cold `build_index` — determinism with no "modulo an unused field" caveat.
        for ld in merged_layers.values():
            adjacency: Dict[str, List[str]] = {}
            reverse: Dict[str, List[str]] = {}
            for e in ld["edges"]:
                adjacency.setdefault(e["source"], []).append(e["target"])
                reverse.setdefault(e["target"], []).append(e["source"])
            ld["adjacency"] = adjacency
            ld["reverse_adjacency"] = reverse

        self.profile_documents = sorted(set(profile_documents))
        files_hash = combine_roots(per_root_hash) if per_root_hash else ""
        return merged_nodes, merged_layers, root_paths, files_hash


__all__ = ["CorpusIndex", "CorpusMoved", "RootIndex"]
