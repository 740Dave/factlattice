"""The bidirectional dangle-lint over a parsed FactLattice corpus.

Two classes, both keyed off the parser's own resolved graph (never a regex):

  forward  -- a node REFERENCES a `[PREFIX-NAME]` that is DEFINED NOWHERE in the
              corpus. This is a straight re-labeling of factlattice's own `dangling`
              diagnostic (`ParsedCorpus.diagnostics_of("dangling")`) -- the parser
              already resolves this correctly over the WHOLE corpus (post-parse,
              whole-node-map pass, alias-aware); re-deriving it here would risk
              drifting from the parser, the single source of truth.

  orphan   -- a DEFINED node that NOTHING references. Not something factlattice's
              corpus.diagnostics surfaces (it is a whole-graph "what points AT this"
              query, i.e. an empty reverse_adjacency), so it is computed here, over
              the SAME `to_graph_index()` dict `diagnostics` is derived from (no
              second parse, no divergence).

Design call: orphan-hood is computed from `inference in {"authored", "property"}`
edges ONLY -- explicit, intentional cross-references. `structural` (CONTAINS, from
heading nesting) is EXCLUDED on purpose: every non-root node has a parent heading
that structurally "points at" it, so including CONTAINS would make almost nothing
ever count as an orphan and defeat the point of the check. `mention`/`computed`
edges are out of parser v1 scope (extraction contract SS2) so there is nothing to
exclude there yet -- revisit this set if/when the parser adds them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional

import factlattice

# Edge `inference` values counted as a "real" reference for orphan purposes.
# Deliberately excludes "structural" (heading-nesting CONTAINS) -- see module
# docstring. Keep in sync with factlattice.parser's inference vocabulary
# (authored | property | structural; mention/computed deferred upstream).
REFERENCING_INFERENCE: FrozenSet[str] = frozenset({"authored", "property"})

DIR_FORWARD = "forward"
DIR_ORPHAN = "orphan"


@dataclass
class DangleFinding:
    """One forward or orphan finding, file:line located where the source has one."""

    direction: str  # DIR_FORWARD | DIR_ORPHAN
    node_id: str
    message: str
    source_file: Optional[str] = None
    source_line: Optional[int] = None
    detail: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "node_id": self.node_id,
            "message": self.message,
            "source_file": self.source_file,
            "source_line": self.source_line,
            "detail": self.detail,
        }


def find_dangles(corpus: "factlattice.ParsedCorpus",
                  *, exclude_orphan_ids: FrozenSet[str] = frozenset()) -> List[DangleFinding]:
    """Return every forward + orphan finding for an already-parsed corpus.

    `exclude_orphan_ids` lets a caller carve out known-legitimate unreferenced
    roots (e.g. a corpus's single top-level index/entry node) without touching
    forward-dangle detection.
    """
    findings: List[DangleFinding] = []

    # --- forward: reuse the parser's own whole-corpus dangling resolution -----
    # corpus.py sets Diagnostic.node_id = the MISSING target id for `dangling`;
    # the referencing (source) node is in `.edge["source"]` for detail/context.
    for d in corpus.diagnostics_of("dangling"):
        findings.append(DangleFinding(
            direction=DIR_FORWARD,
            node_id=d.node_id or "",
            message=d.message,
            source_file=d.source_file,
            source_line=d.source_line,
            detail=d.edge or {},
        ))

    # --- orphan: defined node, no authored/property edge targets it -----------
    index = corpus.to_graph_index()
    alias_index: Dict[str, str] = index.get("alias_index", {}) or {}
    nodes: Dict[str, dict] = index.get("nodes", {}) or {}

    referenced: set = set()
    for layer in index.get("layers", {}).values():
        for e in layer.get("edges", []):
            if e.get("inference") not in REFERENCING_INFERENCE:
                continue
            target = e.get("target")
            canonical = alias_index.get(target, target)
            referenced.add(canonical)
            # A self-reference still counts the node as "referenced" only by
            # something else, per usual orphan semantics -- but we don't special
            # case self-loops here; a node referencing only itself is vanishingly
            # rare in practice and still a signal worth a human's eyes if it shows.

    for nid, node in nodes.items():
        if nid in referenced or nid in exclude_orphan_ids:
            continue
        line_range = node.get("line_range") or [None, None]
        findings.append(DangleFinding(
            direction=DIR_ORPHAN,
            node_id=nid,
            message=(f"node {nid!r} ({node.get('title', '')!r}) is defined but "
                     f"nothing references it (no authored/property edge targets it)"),
            source_file=node.get("source_file"),
            source_line=line_range[0],
            detail={"type_prefix": node.get("type_prefix"), "title": node.get("title")},
        ))

    return findings


# ---------------------------------------------------------------------------
# RULING C (the maintainer 2026-08-17) — the BARE-vs-NAMESPACE-QUALIFIED reference channel,
# the namespace analog of the edge-verb Case-mismatch WARN. Surface loudly, DON'T FIX:
# strict identity is KEPT (a bare id is never silently equated to a namespaced one —
# that "breaks the day two namespaces share a bare id"), the engine only WARNs the
# likely-intended target so the AUTHOR corrects the source.
# ---------------------------------------------------------------------------
NS_SEP = ":"

DIR_UNQUALIFIED_REF = "unqualified_reference"
DIR_AMBIGUOUS_REF = "ambiguous_unqualified_reference"


def _bare_of(qualified_id: str) -> str:
    """The bare id of a (possibly) namespace-qualified id — its last `:`-segment."""
    return qualified_id.rsplit(NS_SEP, 1)[-1]


def classify_unqualified_references(corpus: "factlattice.ParsedCorpus",
                                    *, source_file: Optional[str] = None) -> Dict[str, dict]:
    """A BARE reference `[X]` that fails to resolve, for which a NAMESPACE-QUALIFIED
    definition `[NS:X]` exists (RULING C). STRICT identity is kept — bare and `NS:X` are
    NEVER auto-equated — and the engine only names the likely target so the author fixes
    the source. Returns, both DE-DUPLICATED by bare id:

      `unqualified_references`  {bare -> NS:bare}   exactly ONE namespaced twin: "did you
                                                    mean [NS:bare]?"
      `ambiguous_references`    {bare -> [NS:bare, …]}  TWO+ namespaced twins: genuinely
                                                    ambiguous — candidates listed, never a
                                                    single guess.
    A bare target with ZERO namespaced twins is an ORDINARY forward-dangle (not here).

    ⛔ MEASURED INERT ON THE CURRENT ID MODEL, and this is a first-class finding, not a
    silent zero. `factlattice.parser` strips the namespace/doctype segment from EVERY node
    id (`_heading_id` / `_resolve_ref` keep the LAST `:`-segment), so `[SPEC:ACME-VISION]`
    is STORED as bare `ACME-VISION` and a bare `[ACME-VISION]` ALREADY resolves to it — there
    are ZERO namespace-qualified stored ids on any current corpus, so this returns empty
    everywhere today (measured: 0 across the acme corpora, against 80 forward-dangles).
    Making it fire is a STRICT-NAMESPACE-IDENTITY decision (SPEC §1.1/§9.2) — the maintainer's /
    architect's call, and a BREAKING change to bare<->qualified resolution. The logic is
    proven by unit tests over constructed namespaced-id inputs, so it is correct the day
    that decision lands; this function does not take it.
    """
    by_bare: Dict[str, List[str]] = {}
    for n in corpus.nodes:
        if NS_SEP in n.id:
            by_bare.setdefault(_bare_of(n.id), []).append(n.id)
    resolvable = {n.id for n in corpus.nodes}
    unq: Dict[str, str] = {}
    amb: Dict[str, List[str]] = {}
    for d in corpus.diagnostics_of("dangling"):
        if source_file is not None and d.source_file != source_file:
            continue
        t = d.node_id
        if not t or NS_SEP in t or t in resolvable:
            continue  # a namespaced ref, or one that already resolves — not this channel
        cands = sorted(set(by_bare.get(t, [])))
        if len(cands) == 1:
            unq[t] = cands[0]
        elif len(cands) >= 2:
            amb[t] = cands
    return {
        "unqualified_references": dict(sorted(unq.items())),
        "ambiguous_references": {k: v for k, v in sorted(amb.items())},
    }


def unqualified_reference_targets(channels: Dict[str, dict]) -> FrozenSet[str]:
    """The bare targets claimed by this channel — used to keep them OUT of the plain
    forward-dangle stream (RULING C: 'MUST NOT be reported as a plain unresolved dangle')."""
    return frozenset(channels.get("unqualified_references", {})) | \
        frozenset(channels.get("ambiguous_references", {}))


def unqualified_warn_line(channels: Dict[str, dict]) -> Optional[str]:
    """`Unqualified-reference {ACME-VISION -> SPEC:ACME-VISION, …}` — or None when empty."""
    unq = channels.get("unqualified_references") or {}
    if not unq:
        return None
    return "Unqualified-reference {" + ", ".join(
        f"{k} -> {v}" for k, v in unq.items()) + "}"


def ambiguous_reference_warn_line(channels: Dict[str, dict]) -> Optional[str]:
    """`Ambiguous-unqualified-reference {X -> [SPEC:X | DEC:X]}` — or None when empty."""
    amb = channels.get("ambiguous_references") or {}
    if not amb:
        return None
    return "Ambiguous-unqualified-reference {" + ", ".join(
        f"{k} -> [{' | '.join(v)}]" for k, v in amb.items()) + "}"
