"""factlattice_engine.analytics.exemplifies — the EXEMPLIFIES-coverage report.

A deterministic, freshness-gated fold that answers the D5 question
(an internal decision): which DECISIONS embody a design/collaboration
PRINCIPLE (carry an `EXEMPLIFIES -> [PRINCIPLE-*]` edge) and which do NOT — so the
unmapped decisions are FINDABLE and the exemplifies backfill has signal.

It is a pure composition over the SAME machinery the rest of the analytics layer uses:
  * the DECISION + PRINCIPLE node universe comes from `LtsAggregator._find_all` (the
    cursor-drained, freshness-gated `find` — a STALE/not-found corpus propagates its
    loud marker VERBATIM, never a silently-partial count — [INV-LTS-FRESH]); and
  * the edges come from the SAME fresh partitions via the public `Partition.iter_edges()`
    (every edge, no token-budget truncation), resolved by the engine's own rule
    (skip `dangling` + `unresolved_target`).

EDGE-TYPE MATCH — bare OR namespaced. A decision links via the profile's namespaced
verb `dec.EXEMPLIFIES` (the parser only edge-generates CORE bare verbs or NAMESPACED
domain verbs; a BARE `EXEMPLIFIES` does not parse). So the match accepts an edge whose
type is `edge_verb` OR whose namespaced suffix (`type.split('.')[-1]`) is `edge_verb` —
future-proof if the verb is ever promoted to core-bare.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Set

from ..privacy import is_private_partition


def _fresh_partitions(agg, corpus: Optional[str], res: dict):
    """The non-stale partitions to read edges from — mirrors `LtsAggregator._adjacency`'s
    selection exactly so edge reads share the freshness verdict of the node fetch."""
    tools = agg._get_tools()
    if corpus:
        part = tools.store.get_partition(corpus)
        return [part] if part is not None else []
    stale = set(res.get("stale_corpora") or [])
    # [INV-PRIVATE-FANOUT] an unscoped read never reads a private corpus.
    return [p for p in tools.store.partitions()
            if p.corpus_id not in stale and not is_private_partition(p)]


def _matches_verb(edge_type: Optional[str], edge_verb: str) -> bool:
    if not edge_type:
        return False
    return edge_type == edge_verb or edge_type.split(".")[-1] == edge_verb


def _diagnose(total: int, edge_count: int, edges_seen: int,
              off_target: Dict[str, int], non_decision_sources: int) -> Optional[str]:
    """Name WHY the coverage is zero, so the reader is not left to guess between three
    indistinguishable causes. Returns None when coverage is non-zero."""
    if edge_count:
        return None
    if not total:
        return ("no decision nodes in scope — coverage is undefined here, not zero")
    if edges_seen == 0:
        return (
            "NO EXEMPLIFIES EDGE EXISTS AT ALL in scope. Note this cannot distinguish "
            "'never authored' from 'authored via a property key that mints no edge' — "
            "check the corpus for an `exemplifies:` property, and check whether the "
            "domain profile registers it (Profile.property_edges).")
    parts = [f"{edges_seen} EXEMPLIFIES edge(s) EXIST but none qualified"]
    if off_target:
        fams = ", ".join(f"{k}={v}" for k, v in sorted(off_target.items(),
                                                       key=lambda kv: -kv[1]))
        parts.append(
            f"{sum(off_target.values())} target a family other than the expected "
            f"principle prefix ({fams}) — the backfill may have been authored against "
            f"a different target family")
    if non_decision_sources:
        parts.append(f"{non_decision_sources} originate from a non-decision source")
    return "; ".join(parts)


def exemplifies_coverage(
    agg,
    *,
    corpus: Optional[str] = None,
    decision_prefixes: Sequence[str] = ("DEC", "FIX"),
    principle_prefix: str = "PRINCIPLE",
    edge_verb: str = "EXEMPLIFIES",
    limit: Optional[int] = None,
    **filters,
) -> dict:
    """Coverage of DECISION nodes by an `EXEMPLIFIES -> [PRINCIPLE-*]` edge.

    Returns a deterministic dict::

        {success, corpus,
         total_decisions, mapped_count, unmapped_count, coverage_ratio,
         edge_count,                 # resolved EXEMPLIFIES->principle edges seen
         mapped: {decision_id: [principle_id, ...]},   # sorted
         unmapped: [decision_id, ...],                 # sorted, capped by `limit`
         by_principle: [{principle, decisions:[...], count}],  # sorted -count, id
         uncovered_principles: [principle_id, ...],    # principles NO decision embodies
         decision_prefixes, principle_prefix, edge_verb, filters}

    A STALE/not-found corpus propagates the engine's loud marker verbatim.
    `filters` narrow the DECISION universe (area/since/until/... — same keys as
    `lts_find`); the PRINCIPLE universe is read from the SAME found set by prefix, so
    the default (no prefix filter) sees both. Extra kwargs beyond the known find filter
    keys are ignored by `_find_all`.
    """
    nodes, res = agg._find_all(corpus, filters)
    if nodes is None:
        return res  # verbatim not-found / STALE

    dset: Set[str] = set(decision_prefixes)
    decision_ids: Set[str] = {n["id"] for n in nodes if n.get("type_prefix") in dset}
    principle_ids: Set[str] = {
        n["id"] for n in nodes if n.get("type_prefix") == principle_prefix
    }

    # source -> set(principle targets) over resolved EXEMPLIFIES edges
    #
    # ⛔ WHY THE REJECTED EDGES ARE COUNTED SEPARATELY (added 2026-08-15).
    # This fold used to report a bare `coverage_ratio: 0.0` and nothing else. Measured on
    # `acme-console`: 0.0 with `edge_count: 0` — while the corpus in fact carried
    # 17 EXEMPLIFIES edges, none of which targeted a `PRINCIPLE-*` node, plus 4 uses of an
    # `exemplifies:` PROPERTY that minted nothing at all. THREE different states produce
    # the identical zero:
    #     (a) the backfill was never authored,
    #     (b) it was authored as a property key that did not mint,
    #     (c) it was authored at a different TARGET FAMILY (DX-*, ACME-VISION-*).
    # All three were true simultaneously, and the report could not tell them apart. That
    # is the epistemics rule `instance-bitrate-sweep` exactly — an instrument whose
    # output does not distinguish the hypotheses it was run to distinguish. A zero that
    # cannot be diagnosed is not a measurement.
    mapped_sets: Dict[str, Set[str]] = {}
    edge_count = 0
    edges_seen = 0                      # EXEMPLIFIES edges of any shape
    off_target: Dict[str, int] = {}     # target type_prefix -> count, for edges rejected
    non_decision_sources = 0
    prefix_of = {n["id"]: n.get("type_prefix") for n in nodes}
    for part in _fresh_partitions(agg, corpus, res):
        for e in part.iter_edges():
            if not _matches_verb(e.get("type"), edge_verb):
                continue
            if e.get("dangling") or "unresolved_target" in (e.get("flags") or ()):
                continue
            edges_seen += 1
            tgt = e.get("target")
            src = e.get("source")
            if tgt not in principle_ids:
                fam = prefix_of.get(tgt) or "<unresolved>"
                off_target[fam] = off_target.get(fam, 0) + 1
                continue
            if src not in decision_ids:
                non_decision_sources += 1
                continue
            mapped_sets.setdefault(src, set()).add(tgt)
            edge_count += 1

    mapped = {d: sorted(t) for d, t in mapped_sets.items()}
    unmapped_all = sorted(d for d in decision_ids if d not in mapped_sets)
    total = len(decision_ids)

    # reverse index: which decisions embody each principle
    by_principle_map: Dict[str, List[str]] = {}
    for d, tgts in mapped.items():
        for t in tgts:
            by_principle_map.setdefault(t, []).append(d)
    by_principle = [
        {"principle": p, "count": len(ds), "decisions": sorted(ds)}
        for p, ds in by_principle_map.items()
    ]
    by_principle.sort(key=lambda b: (-b["count"], b["principle"]))
    uncovered_principles = sorted(principle_ids - set(by_principle_map.keys()))

    shown = unmapped_all if limit is None else unmapped_all[: max(0, int(limit))]
    out = {
        "success": True,
        "corpus": corpus,
        "total_decisions": total,
        "mapped_count": len(mapped),
        "unmapped_count": len(unmapped_all),
        "coverage_ratio": (len(mapped) / total) if total else 0.0,
        "edge_count": edge_count,
        "mapped": mapped,
        "unmapped": shown,
        "by_principle": by_principle,
        "total_principles": len(principle_ids),
        "uncovered_principles": uncovered_principles,
        "decision_prefixes": list(decision_prefixes),
        "principle_prefix": principle_prefix,
        "edge_verb": edge_verb,
        "filters": {k: v for k, v in filters.items() if v is not None},
        # --- what a ZERO actually means here (see the note at the fold) ---
        "edges_seen": edges_seen,
        "edges_off_target_family": off_target,
        "edges_from_non_decision_sources": non_decision_sources,
        "zero_diagnosis": _diagnose(total, edge_count, edges_seen,
                                    off_target, non_decision_sources),
    }
    if limit is not None:
        out["truncated"] = len(unmapped_all) > len(shown)
    if not corpus and "stale_corpora" in res:
        out["stale_corpora"] = res["stale_corpora"]
    return out


__all__ = ["exemplifies_coverage"]
