"""ReverseIndex — target -> referrers, the completeness path for "who points at me?"
([LTS-REVERSE-LOOKUP], additive Phase-3 build over `main`@5504567).

WHY THIS EXISTS. `lts_node` already returns a node's own reverse edges (`rev_adj` in
`Partition.adjacency()`), but that map is built ONLY from the edges authored INSIDE
that node's own partition. There is no index anywhere for the direction a
cross-corpus author actually needs: "which OTHER corpora point at me?" — answering
that today means loading every other loaded corpus by hand and re-deriving it. This
module is that missing direction, built once and cached like every other
freshness-keyed structure in this engine.

[INV-LTS-CORPUS-ISOLATION] — INDEXES ACROSS PARTITIONS, NEVER MERGES THEM. Every
`Referrer` keeps its OWN corpus_id; a target bucket is keyed by the CONCRETE
(corpus_id, node_id) pair a reference actually resolves to, never by domain_suffix
alone. A same-corpus reference (no domain qualifier, or a domain qualifier equal to
the referrer's own `domain_suffix`) resolves to the REFERRER'S OWN corpus_id and can
never attach to a same-named node living in a different corpus — this is what keeps
the id-collision case (two unrelated corpora that both happen to define `DEC-1`) from
producing a false cross-corpus hit. A cross-corpus reference resolves through the
domain->corpus_ids map exactly as `router._resolve_corpus` does: zero or more-than-one
candidate is UNRESOLVED and filed separately (`unresolved`), never guessed at.

TWO CHANNELS, unioned into one referrer list per target:

  * typed edges     — every edge already living in a partition's `layers[...].edges`
                      (`Partition.iter_edges()`, both same-corpus and cross-corpus,
                      dangling or not — dangling-ness is about the TARGET's existence,
                      not about whether the SOURCE authored the reference).
                      `how = "edge:<TYPE>"`.
  * property refs   — bracketed `[ID]` references living in the value of a property key
                      that is NOT in `_CORE_MINTING_KEYS` (below — the parser's
                      core-only, no-profile derivation, [FL-VERB-PARITY] 2026-09-23): a
                      key like `tracks`/`rationale`/`stage-evidence` that never mints an
                      edge, so the reference is invisible to `lts_traverse` today.
                      `how = "property:<key>"`. Reuses `factlattice.parser._property_refs`
                      — itself built on `_resolve_ref`/`classify_bracket_token`, the SAME
                      extraction the edge minter runs over the SAME parsed value — rather
                      than hand-rolling a second bracket scan. `binding.py`'s own
                      docstring names that exact failure mode ("a third hand-written copy
                      here is how a receipt starts telling authors about a vocabulary the
                      parser stopped having"); this module is one more consumer of that
                      one implementation, not a second one.
                      Deliberately NEVER reads source/body text: [FL-PROSE-BLIND]'s scope
                      stays with `lts_find_in_prose`. This only ever reads the
                      ALREADY-PARSED `properties` dict a partition carries in memory —
                      no file I/O, so building the index costs a `find` nothing extra
                      it did not already pay to load the partition.
  ⚠ KNOWN SCOPE LIMIT, AND IT IS LIVE NOW, NOT PENDING ON FUTURE WORK: this channel
    checks membership against `_CORE_MINTING_KEYS` — the CORE-ONLY, no-profile minting
    set (`parser.rel_property_edge_map()` union `FLOW_PROPERTY_EDGES`; [FL-VERB-PARITY]
    2026-09-23 UNIFY) — NEVER against the PROFILE-AWARE `binding.minting_keys_for(profile)`
    that also folds in a profile's own registered `relationship_verbs` / `property_edges`.
    (Before the unify pass this checked the static `binding.MINTING_KEYS` — the ten
    hand-curated entries only, which under-counted verbs a profile had registered.
    `_CORE_MINTING_KEYS` is the set the PARSER actually mints from with NO profile at
    all — the vetted core verbs' own mechanical kebab spellings, e.g. `depends-on` ->
    DEPENDS_ON, `part-of` -> PART_OF, `used-by` -> USED_BY [reversed], `tested-by` ->
    TESTED_BY [reversed]. ⛔ 2026-09-23 CORRECTION (an internal audit finding): an
    earlier revision of the unify briefly made `rel_property_edge_map()` derive a kebab
    key for EVERY bare core verb unconditionally — including common-English-word verbs
    like `holds`/`manages`/`causes`/`sequence` used as ordinary prose-citation
    properties — which a cold audit found minted spurious edges (`- sequence: [X]
    describes a generic display device...`). That derivation was removed; those verbs
    are back to `TRIPLE_ONLY` (no property surface at all, core or profile), so a
    `- holds: [X]` line with no matching `[SRC] HOLDS [X]` triple is now correctly filed
    as a non-minting `property:holds` reference by this module, not silently treated as
    already-minted.)
    The Era-1/C3 fix (`ParserConfig.from_profile`, parser.py `_rel_edges.update
    (property_edges)`) already threads a profile's `property_edges` into what the PARSER
    mints; this module's "is it minting" check still cannot see a profile's registrations
    (see the root cause below) — so a corpus whose registered profile declares
    `relationship_verbs` or `property_edges` beyond the core set would see the SAME fact
    reported twice — once as `edge:<TYPE>` (correctly minted) and once as `property:<key>`
    (wrongly treated as non-minting) — a REDUNDANT, INFLATED count, never a false referrer
    and never a cross-corpus-unsafe one. Unexercised today only because no profile under
    `domain-profiles/` currently registers `relationship_verbs`/`property_edges`; the
    trigger fires the day one does, not after some pending change.

    ROOT CAUSE IS A SEPARATE, DEEPER GAP, AND IT IS NOT SOMETHING THAT (EXTERNAL,
    PENDING) FL-VERB-PARITY WORK FIXES: `Partition`/`LtsStore` carry no live
    per-partition `Profile` handle at query time — only `profile_documents: List[str]`,
    the filenames, not a parsed `Profile` object — so `minting_keys_for(profile)` is not
    even CALLABLE from this module as written. Closing this needs new plumbing (a
    partition-to-Profile lookup at query time), not a change to the parser's minting
    tables, which is what FL-VERB-PARITY addresses. No corpus in this engine's own
    fixtures/tests exercises the property_edges-registered case; flagged rather than
    silently assumed away.

FRESHNESS ([LEC-FRESHNESS]) — keyed to a SNAPSHOT across every partition folded in:
`snapshot_key()` below is `sorted[(corpus_id, part._freshness_key())]`, the SAME
per-partition key `Partition.adjacency()`/`degree_index()` already validate against.
A real content change in ANY loaded partition changes the snapshot (a cross-corpus edge
can point into any of them), which is the caller's signal to rebuild. Building the
index itself never reads a partition that failed the ordinary freshness gate — the
caller (`tools.LtsEngine.reverse`) gates every corpus through the SAME `_gate()` choke
point every other tool uses (with self-heal) BEFORE handing this module a partition
list, so a corpus that cannot be verified fresh never contributes silently-wrong
referrers: it is reported as `stale_corpora` and the response's `complete` flag says so
— [INV-LTS-FRESH]'s "never silently-omit-and-call-complete" rule, the SAME shape
`lts_find`'s unscoped multi-corpus arm already uses for exactly this reason.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from factlattice.parser import FLOW_PROPERTY_EDGES, _property_refs, rel_property_edge_map

from .store import Partition

# [FL-VERB-PARITY] 2026-09-23 UNIFY: the CORE-ONLY (no-profile) effective minting set,
# computed once. `binding.MINTING_KEYS` stays the OLD static ten-entry baseline on
# purpose (`tests/test_repair_and_binding.py:281` pins it); this is the set the parser
# ACTUALLY mints from with no profile at all — the vetted core verbs' own kebab
# spellings (`depends-on`, `part-of`, `used-by`, `tested-by`, ...). Common-English-word
# core verbs (`holds`, `manages`, `causes`, `sequence`, ...) do NOT appear here: an
# unvetted grant for exactly that class was found by cold audit to mint spurious edges
# from prose citations and was removed (an internal audit finding, see the module
# docstring) — see the module docstring.
# Deliberately STILL profile-blind: closing that half needs the Profile-handle plumbing
# named below (`an open internal ticket`), which this module does not have.
_CORE_MINTING_KEYS = frozenset(rel_property_edge_map()) | frozenset(FLOW_PROPERTY_EDGES)


@dataclass(frozen=True)
class Referrer:
    """One fact: `corpus:node_id` refers to the query's target."""

    corpus: str
    node_id: str
    how: str                       # "edge:<TYPE>" | "property:<key>"
    layer: Optional[str] = None    # edge layer, when `how` is an edge
    source_line: Optional[int] = None

    def to_dict(self) -> dict:
        d = {"corpus": self.corpus, "node_id": self.node_id, "how": self.how}
        if self.layer is not None:
            d["layer"] = self.layer
        if self.source_line is not None:
            d["source_line"] = self.source_line
        return d


def _sort_key(r: Referrer) -> Tuple[str, str, str]:
    return (r.corpus, r.node_id, r.how)


def build_domain_index(parts: List[Partition]) -> Dict[str, List[str]]:
    """domain_suffix -> [corpus_id, ...] over the partitions being folded in. A
    resolution helper independent of `LtsStore.filter_by_domain` so this module can be
    unit-tested over an arbitrary partition LIST (a subset of a store, or a synthetic
    fixture) without a real `LtsStore` — the same shape `Router._resolve_corpus`
    resolves against, just addressable without constructing a store."""
    idx: Dict[str, List[str]] = {}
    for p in parts:
        idx.setdefault(p.domain_suffix, []).append(p.corpus_id)
    return idx


def snapshot_key(parts: List[Partition]) -> Tuple[Tuple[str, str], ...]:
    """The cache-invalidation key: one `(corpus_id, freshness_key)` pair per partition,
    sorted for a stable comparison. Identical to the key `Partition.adjacency()` already
    validates per-partition, just folded over every partition the index spans (a
    cross-corpus edge can point into any of them, so any one of them changing must
    invalidate the whole index)."""
    return tuple(sorted((p.corpus_id, p._freshness_key()) for p in parts))


class ReverseIndex:
    """`(target_corpus_id, target_node_id) -> sorted list[Referrer]`.

    Built ONCE from a snapshot of already-gated, fresh partitions — see the module
    docstring for the freshness contract and [INV-LTS-CORPUS-ISOLATION] for why a
    target bucket is keyed by a CONCRETE corpus_id and never by domain_suffix alone.
    """

    def __init__(self) -> None:
        self._by_target: Dict[Tuple[str, str], List[Referrer]] = {}
        # Cross-corpus refs whose domain qualifier did not resolve to EXACTLY one
        # partition among those folded in — surfaced for diagnostics, never guessed
        # into a bucket. `reason` is "unresolved_domain" (zero candidates) or
        # "ambiguous_domain" (more than one) — the same two Router.`_resolve_corpus`
        # names, so a caller comparing the two never sees two names for one idea.
        self.unresolved: List[dict] = []

    def _add(self, key: Tuple[str, str], ref: Referrer) -> None:
        self._by_target.setdefault(key, []).append(ref)

    def _file_reference(self, *, referrer_corpus: str, referrer_node: str,
                        domain_index: Dict[str, List[str]],
                        target_domain: Optional[str], own_domain: Optional[str],
                        target_id: str, ref: Referrer) -> None:
        """Resolve ONE reference to its target bucket (same-corpus fast path, or a
        domain lookup) and file it there, or into `unresolved` if the domain does not
        resolve to exactly one corpus."""
        if not target_domain or target_domain == own_domain:
            self._add((referrer_corpus, target_id), ref)
            return
        candidates = domain_index.get(target_domain, [])
        if len(candidates) == 1:
            self._add((candidates[0], target_id), ref)
            return
        self.unresolved.append({
            "from_corpus": referrer_corpus, "from_node": referrer_node,
            "target_domain": target_domain, "target": target_id, "how": ref.how,
            "reason": "unresolved_domain" if not candidates else "ambiguous_domain",
        })

    @classmethod
    def build(cls, parts: List[Partition],
             domain_index: Optional[Dict[str, List[str]]] = None) -> "ReverseIndex":
        """Build over a snapshot of already-freshness-verified partitions.

        `domain_index` defaults to `build_domain_index(parts)` — pass an explicit one
        only when the caller's domain resolution must span MORE partitions than are
        being folded into referrer content (not used by the engine today; kept for a
        future partial-refresh path so this signature does not need to change for it).
        """
        idx = cls()
        if domain_index is None:
            domain_index = build_domain_index(parts)
        for part in parts:
            own_domain = part.domain_suffix
            # --- channel 1: typed edges already in the graph -------------------
            for edge in part.iter_edges():
                target_domain = edge.get("target_domain")
                target_id = edge.get("target")
                if target_id is None:
                    continue
                etype = edge.get("type") or "(untyped)"
                ref = Referrer(corpus=part.corpus_id, node_id=edge.get("source"),
                               how=f"edge:{etype}", layer=edge.get("layer"),
                               source_line=edge.get("source_line"))
                idx._file_reference(
                    referrer_corpus=part.corpus_id, referrer_node=edge.get("source"),
                    domain_index=domain_index, target_domain=target_domain,
                    own_domain=own_domain, target_id=target_id, ref=ref)

            # --- channel 2: non-minting property-VALUE id references -----------
            for nid, nd in part.nodes.items():
                props = nd.get("properties") or {}
                for key, value in props.items():
                    if key in _CORE_MINTING_KEYS:
                        continue  # channel 1 already covers this key's references
                    for (domain, target_id) in _property_refs(value):
                        ref = Referrer(corpus=part.corpus_id, node_id=nid,
                                       how=f"property:{key}")
                        idx._file_reference(
                            referrer_corpus=part.corpus_id, referrer_node=nid,
                            domain_index=domain_index, target_domain=domain,
                            own_domain=own_domain, target_id=target_id, ref=ref)

        for bucket in idx._by_target.values():
            bucket.sort(key=_sort_key)
        return idx

    def referrers(self, corpus_id: str, node_id: str) -> List[Referrer]:
        """ALL referrers of `corpus_id:node_id`, sorted and complete — callers apply
        their own budget/limit slice; this method never truncates."""
        return list(self._by_target.get((corpus_id, node_id), ()))

    def total_referrers(self, corpus_id: str, node_id: str) -> int:
        return len(self._by_target.get((corpus_id, node_id), ()))


__all__ = ["Referrer", "ReverseIndex", "build_domain_index", "snapshot_key"]
