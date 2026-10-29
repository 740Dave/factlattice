"""factlattice_engine.analytics — the ANALYTICS layer of lts-engine.

The QUERY layer (`factlattice_engine.tools`) answers "which nodes / how connected" —
it returns nodes, edges, subgraphs, never a scalar or an aggregate row. The
meta-questions a rollup actually needs are AGGREGATES: "count nodes group-by
lifecycle-position", "count nodes group-by domain / prefix", "how many FIX nodes this
month". This layer is that missing primitive: a deterministic fold over the QUERY
layer's OWN `find`, so it inherits the freshness gate and completeness for free.

    from factlattice_engine.analytics import (count, group_by, numeric, sum, avg, min,
        max, distinct, cross_tab, top_k, degree, top_by_degree, count_by_edge_type,
        orphans)

    # --- the count / group-by kernel ---
    group_by("lifecycle-position", corpus="alpha", area="decisions")
    #   -> {"buckets": [{"value":"committed","count":52}, {"value":"built","count":9}, ...]}
    group_by("domain", corpus="alpha")                 # / "prefix"
    count(corpus="alpha", prefix="DEC")                # -> {"total": ...}

    # --- TIER 1: numeric / distinct / cross-tab / top_k ---
    sum("token_count", corpus="alpha", group_by="decided-in-session")
    avg("dur_s", corpus="alpha", group_by="root-cause")
    distinct("lifecycle-position", corpus="alpha", area="decisions")
    cross_tab("lifecycle-position", "domain", corpus="alpha", area="decisions")
    top_k("decided-in-session", 5, corpus="alpha", prefix="DEC")
    top_k("decided-in-session", 5, by="token_count", agg="sum", corpus="alpha")

    # --- TIER 2: graph-native (degree / edge-type / orphans) ---
    top_by_degree(5, corpus="alpha", area="decisions")
    degree("SOME-NODE", corpus="alpha")                # in/out/total
    count_by_edge_type(corpus="alpha")                 # DEPENDS_ON/SUPERSEDES/...
    orphans(corpus="alpha", area="decisions")          # nodes with no incoming edge

CLI (also prints a menu of worked example queries)::

    python3 -m factlattice_engine.analytics count  --corpus alpha --group-by lifecycle-position
    python3 -m factlattice_engine.analytics sum token_count --group-by decided-in-session
    python3 -m factlattice_engine.analytics cross-tab lifecycle-position domain --area decisions
    python3 -m factlattice_engine.analytics top-k decided-in-session --k 5 --prefix DEC
    python3 -m factlattice_engine.analytics top-by-degree --k 5 --area decisions
    python3 -m factlattice_engine.analytics edge-types
    python3 -m factlattice_engine.analytics orphans --area decisions
    python3 -m factlattice_engine.analytics examples        # run the worked example queries
    python3 -m factlattice_engine.analytics --selftest

DESIGN NOTES
------------
* DETERMINISTIC, NO LLM — a pure fold over the found node set.
* COMPLETE — drives `find` across its token-budget cursor so counts see every match
  (`core._find_all`), never a truncated slice.
* FRESHNESS-HONEST — propagates the QUERY layer's not-found / STALE markers verbatim; a
  stale corpus never yields a silently-wrong count ([INV-LTS-FRESH]).
* CANONICALIZING — `lifecycle-position` / `status` collapse to their leading status
  token (a corpus stores `committed   # prose` as a distinct raw string from
  `committed`); dates bucket to day/month/year. Pass `raw=True` to see raw values.
"""

from __future__ import annotations

from .core import (
    LtsAggregator,
    avg,
    build_default_tools,
    canonical_token,
    coerce_number,
    count,
    count_by_edge_type,
    cross_tab,
    date_bucket,
    degree,
    distinct,
    exemplifies_coverage,
    group_by,
    make_key_fn,
    max,
    min,
    numeric,
    numeric_summary,
    orphans,
    profile_audit,
    resolve_attr,
    sum,
    top_by_degree,
    top_k,
    NONE_KEY,
)
from .profile_audit import (
    NullProfileAdapter,
    ProfileAdapter,
    YamlProfileAdapter,
    resolve_profile_adapter,
)
from .profile_registration import audit_profile_registration, document_tag_of

__all__ = [
    "audit_profile_registration", "document_tag_of",
    "LtsAggregator", "count", "group_by", "numeric", "sum", "avg", "min", "max",
    "distinct", "cross_tab", "top_k", "degree", "top_by_degree",
    "count_by_edge_type", "orphans", "profile_audit", "exemplifies_coverage",
    "ProfileAdapter", "NullProfileAdapter", "YamlProfileAdapter",
    "resolve_profile_adapter",
    "resolve_attr", "make_key_fn", "canonical_token", "date_bucket",
    "coerce_number", "numeric_summary", "build_default_tools", "NONE_KEY",
]
