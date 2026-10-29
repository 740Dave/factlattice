"""factlattice_engine.analytics.selftest — deterministic, no-LLM verification.

Two tiers:
  A. HERMETIC — a hand-built in-memory lts-engine partition with deliberately MESSY
     lifecycle values, verifying canonicalization, the cursor-drain (counts see every
     node even under a 1-token budget), filter pass-through, and STALE propagation.
     Always runs; never touches any real corpus.
  B. LIVE — best-effort against whatever corpus is configured (default `alpha`). If
     it is present + fresh, assert structural invariants (bucket counts sum to total,
     canonical distinct <= raw distinct) and PRINT the histogram. If the corpus is
     missing/stale, SKIP with a notice — a box without a configured corpus still gets
     a green tier A.

Run: `python3 -m factlattice_engine.analytics --selftest`  (or `.selftest.run()`).
"""

from __future__ import annotations

import os
import sys

from .core import LtsAggregator, canonical_token, date_bucket

# Default corpus for the best-effort LIVE tier (overridable). Generic example id from
# the config registry; the tier SKIPs cleanly when it isn't configured.
DEFAULT_CORPUS = os.environ.get("LTS_ANALYTICS_SELFTEST_CORPUS", "alpha")


class _AlwaysFresh:
    """A freshness provider that declares every partition fresh — lets a hermetic,
    rootless in-memory partition pass the engine's gate."""
    def evaluate(self, part):
        from ..store import FreshnessResult
        return FreshnessResult(part.corpus_id, True, "hermetic", "hermetic")


class _AlwaysStale:
    def evaluate(self, part):
        from ..store import FreshnessResult
        return FreshnessResult(part.corpus_id, False, "old", "new", "hermetic-stale")


def _node(nid, prefix, domain, area, lifecycle, session, importance, tokens=5):
    return {
        "id": nid, "type_prefix": prefix, "title": nid, "domain": domain,
        "status": None, "area": area, "token_count": tokens,
        "properties": {k: v for k, v in (
            ("lifecycle-position", lifecycle),
            ("decided-in-session", session),
            ("importance", importance)) if v is not None},
    }


# Deliberately messy lifecycle strings — the real-corpus failure mode.
_HERMETIC_NODES = [
    _node("DEC-A", "DEC", "D1", "decisions", "committed   # proven across c9/c10", "2026-07-20 (c14)", "high"),
    _node("DEC-B", "DEC", "D1", "decisions", "committed", "2026-07-21", "high"),
    _node("DEC-C", "DEC", "D2", "decisions", "built   # spec-authored 2026-07-19", "2026-06-15", "medium"),
    _node("DEC-D", "DEC", "D2", "decisions", "raised", "2026-07-01", "low"),
    _node("FIX-E", "FIX", "D1", "decisions", "committed (via COMMIT-abc123)", "2026-05-10", "medium"),
    _node("DEC-F", "DEC", "D1", "decisions", None, None, "low"),
]


def _build_hermetic_tools(freshness):
    from ..store import LtsStore, Partition
    from ..tools import LtsEngine
    index = {
        "source_corpus": {"corpus_id": "hermetic", "domain_suffix": "test",
                          "files_hash": "hermetic", "root_paths": []},
        "nodes": {n["id"]: n for n in _HERMETIC_NODES},
        "layers": {},
        "stats": {"total_nodes": len(_HERMETIC_NODES)},
    }
    store = LtsStore()
    store.add_partition(Partition(index))
    # default_token_budget=1 forces per-node truncation -> exercises the cursor-drain.
    return LtsEngine(store, default_token_budget=1, freshness_provider=freshness)


def _check(cond, msg, failures):
    if not cond:
        failures.append(msg)
        print(f"  FAIL: {msg}")
    return cond


def _tier_a(failures) -> None:
    print("[tier A] hermetic aggregation")

    # unit: canonicalization + date bucketing
    _check(canonical_token("committed   # proven") == "committed", "canonical strips '# ...'", failures)
    _check(canonical_token("committed (via X)") == "committed", "canonical strips '(...)'", failures)
    _check(canonical_token("spec-authored") == "spec-authored", "canonical keeps hyphenated token", failures)
    _check(canonical_token(None) is None and canonical_token("  ") is None, "canonical None/blank -> None", failures)
    _check(date_bucket("2026-07-20 (c14)", "month") == "2026-07", "date bucket month", failures)
    _check(date_bucket("2026-07-20 (c14)", "day") == "2026-07-20", "date bucket day", failures)
    _check(date_bucket("2026", "year") == "2026", "date bucket bare year", failures)
    _check(date_bucket("garbage") is None, "date bucket unparseable -> None", failures)

    agg = LtsAggregator(_build_hermetic_tools(_AlwaysFresh()))

    # count total (cursor-drain past a 1-token budget must see all 6)
    c = agg.count(corpus="hermetic")
    _check(c.get("success") and c.get("total") == 6, f"count total == 6 (got {c.get('total')})", failures)

    # group-by lifecycle canonical
    g = agg.group_by("lifecycle-position", corpus="hermetic")
    _check(g["total"] == 6, "lifecycle total == 6", failures)
    _check(g["groups"].get("committed") == 3, f"committed == 3 (got {g['groups'].get('committed')})", failures)
    _check(g["groups"].get("built") == 1, "built == 1", failures)
    _check(g["groups"].get("raised") == 1, "raised == 1", failures)
    _check(g["groups"].get("(none)") == 1, "missing lifecycle -> (none) == 1", failures)
    _check([b["value"] for b in g["buckets"]][0] == "committed", "buckets sorted: committed first", failures)
    _check(sum(b["count"] for b in g["buckets"]) == g["total"], "bucket counts sum to total", failures)

    # raw must expose MORE distinct values than canonical (the point of canonicalizing)
    graw = agg.group_by("lifecycle-position", corpus="hermetic", raw=True)
    _check(graw["distinct"] > g["distinct"], f"raw distinct ({graw['distinct']}) > canonical ({g['distinct']})", failures)

    # group-by prefix / domain
    gp = agg.group_by("prefix", corpus="hermetic")
    _check(gp["groups"] == {"DEC": 5, "FIX": 1}, f"prefix histogram (got {gp['groups']})", failures)
    gd = agg.group_by("domain", corpus="hermetic")
    _check(gd["groups"].get("D1") == 4 and gd["groups"].get("D2") == 2, "domain histogram", failures)

    # group-by decided-in-session (month bucket)
    gs = agg.group_by("decided-in-session", corpus="hermetic", date_granularity="month")
    _check(gs["groups"].get("2026-07") == 3, f"session month 2026-07 == 3 (got {gs['groups'].get('2026-07')})", failures)
    _check(gs["groups"].get("(none)") == 1, "missing session -> (none) == 1", failures)

    # filter pass-through: count only the FIX nodes
    cf = agg.count(corpus="hermetic", prefix="FIX")
    _check(cf.get("total") == 1, f"prefix=FIX count == 1 (got {cf.get('total')})", failures)
    # filter pass-through on group-by: DEC nodes group-by importance
    gi = agg.group_by("importance", corpus="hermetic", prefix="DEC")
    _check(gi["total"] == 5 and gi["groups"].get("high") == 2, "DEC group-by importance", failures)

    # STALE propagation — a stale corpus must NOT return a silently-wrong count
    agg_stale = LtsAggregator(_build_hermetic_tools(_AlwaysStale()))
    s = agg_stale.count(corpus="hermetic")
    _check(s.get("success") is False and s.get("stale") is True, "stale corpus -> loud stale marker", failures)

    # not-found propagation
    nf = agg.count(corpus="does-not-exist")
    _check(nf.get("success") is False, "unknown corpus -> failure", failures)


# --------------------------------------------------------------------------- #
# tier A2 — Tier-1 (numeric/distinct/cross-tab/top_k) + Tier-2 (degree/edge-type/
# orphans) over a graph partition with KNOWN numeric attrs and KNOWN edges.
# --------------------------------------------------------------------------- #
def _gnode(nid, prefix, domain, tokens=None, commits=None):
    props = {}
    if tokens is not None:
        props["tokens"] = tokens
    if commits is not None:
        props["commits"] = commits
    return {"id": nid, "type_prefix": prefix, "title": nid, "domain": domain,
            "status": None, "area": "decisions", "token_count": 5, "properties": props}


_GRAPH_NODES = [
    _gnode("DEC-N1", "DEC", "D1", tokens=100, commits=5),
    _gnode("DEC-N2", "DEC", "D1", tokens=200, commits=3),
    _gnode("DEC-N3", "DEC", "D2", tokens=50, commits="oops"),  # non-numeric -> skip
    _gnode("DEC-N4", "DEC", "D2"),                             # absent -> skip
    _gnode("FIX-N5", "FIX", "D1", tokens=10, commits=1),
    _gnode("DEC-N6", "DEC", "D1", tokens=40, commits=2),
]
_GRAPH_EDGES = [
    {"source": "DEC-N1", "target": "DEC-N2", "type": "DEPENDS_ON", "dangling": False, "flags": []},
    {"source": "DEC-N1", "target": "DEC-N3", "type": "DEPENDS_ON", "dangling": False, "flags": []},
    {"source": "DEC-N2", "target": "DEC-N3", "type": "RELATES_TO", "dangling": False, "flags": []},
    {"source": "FIX-N5", "target": "DEC-N1", "type": "SUPERSEDES", "dangling": False, "flags": []},
    {"source": "DEC-N6", "target": "DEC-X", "type": "CONTAINS", "dangling": True, "flags": []},
    {"source": "DEC-N4", "target": "DEC-Y", "type": "RELATES_TO", "dangling": False, "flags": ["unresolved_target"]},
]


def _build_graph_tools(freshness, token_budget=1000):
    from ..store import LtsStore, Partition
    from ..tools import LtsEngine
    index = {
        "source_corpus": {"corpus_id": "graph", "domain_suffix": "test",
                          "files_hash": "graph", "root_paths": []},
        "nodes": {n["id"]: n for n in _GRAPH_NODES},
        "layers": {"semantic": {"edges": _GRAPH_EDGES}},
        "stats": {"total_nodes": len(_GRAPH_NODES), "total_edges": len(_GRAPH_EDGES)},
    }
    store = LtsStore()
    store.add_partition(Partition(index))
    return LtsEngine(store, default_token_budget=token_budget,
                      freshness_provider=freshness)


def _tier_a2(failures) -> None:
    print("[tier A2] numeric + graph-native aggregates")
    agg = LtsAggregator(_build_graph_tools(_AlwaysFresh()))

    # numeric global — honest skip of non-numeric/absent
    s = agg.sum("tokens", corpus="graph")
    _check(s["sum"] == 400.0 and s["count"] == 5 and s["skipped"] == 1,
           f"sum(tokens)==400 over 5 (skip 1); got {s.get('sum')}/{s.get('count')}/{s.get('skipped')}", failures)
    _check(s["avg"] == 80.0 and s["min"] == 10.0 and s["max"] == 200.0, "avg/min/max(tokens)", failures)
    c = agg.avg("commits", corpus="graph")
    _check(c["count"] == 4 and c["skipped"] == 2 and c["sum"] == 11.0,
           f"avg(commits): 4 numeric, 2 skipped, sum 11; got {c.get('count')}/{c.get('skipped')}/{c.get('sum')}", failures)

    # numeric grouped
    g = agg.numeric("tokens", op="sum", corpus="graph", group_by="domain")
    _check(g["groups"] == {"D1": 350.0, "D2": 50.0}, f"sum(tokens) by domain; got {g.get('groups')}", failures)
    _check([b["value"] for b in g["buckets"]] == ["D1", "D2"], "numeric buckets sorted by sum desc", failures)

    # distinct
    d = agg.distinct("prefix", corpus="graph")
    _check(d["distinct"] == 2 and d["values"] == ["DEC", "FIX"], "distinct(prefix)==2", failures)

    # cross-tab
    ct = agg.cross_tab("prefix", "domain", corpus="graph")
    _check(ct["matrix"] == {"DEC": {"D1": 3, "D2": 2}, "FIX": {"D1": 1, "D2": 0}},
           f"cross_tab prefix x domain matrix; got {ct.get('matrix')}", failures)
    _check(ct["buckets"][0] == {"attr1": "DEC", "attr2": "D1", "count": 3}, "cross_tab top cell deterministic", failures)

    # top_k
    tkc = agg.top_k("prefix", 1, corpus="graph")
    _check(tkc["buckets"] == [{"value": "DEC", "count": 5}], "top_k(prefix,1) by count", failures)
    tkn = agg.top_k("domain", 5, by="tokens", agg="sum", corpus="graph")
    _check([b["value"] for b in tkn["buckets"]] == ["D1", "D2"] and tkn["buckets"][0]["sum"] == 350.0,
           "top_k(domain) by sum(tokens)", failures)

    # Tier 2 — degree / top_by_degree / edge-type / orphans
    deg = agg.degree("DEC-N1", corpus="graph")
    _check(deg["in_degree"] == 1 and deg["out_degree"] == 2 and deg["degree"] == 3, "degree(DEC-N1)=3", failures)
    tbd = agg.top_by_degree(3, corpus="graph")
    _check([b["node"] for b in tbd["buckets"]] == ["DEC-N1", "DEC-N2", "DEC-N3"],
           f"top_by_degree deterministic; got {[b['node'] for b in tbd['buckets']]}", failures)
    et = agg.count_by_edge_type(corpus="graph")
    _check(et["resolved_edges"] == 4 and et["total_edges"] == 6, "edge counts exclude dangling+unresolved", failures)
    _check(et["groups"] == {"DEPENDS_ON": 2, "RELATES_TO": 1, "SUPERSEDES": 1},
           f"edge-type histogram; got {et.get('groups')}", failures)
    orph = agg.orphans(corpus="graph")
    _check(orph["orphan_count"] == 3 and orph["orphans"] == ["DEC-N4", "DEC-N6", "FIX-N5"],
           f"orphans (in_degree==0); got {orph.get('orphans')}", failures)

    # COMPLETENESS under a 1-token budget — every op still sees all 6 / all edges
    aggd = LtsAggregator(_build_graph_tools(_AlwaysFresh(), token_budget=1))
    _check(aggd.sum("tokens", corpus="graph")["sum"] == 400.0, "drain: sum(tokens) complete under budget=1", failures)
    _check(aggd.top_by_degree(10, corpus="graph")["total"] == 6, "drain: node universe complete under budget=1", failures)
    _check(aggd.count_by_edge_type(corpus="graph")["resolved_edges"] == 4, "drain: edges complete (iter_edges, unbudgeted)", failures)

    # STALE — every new op propagates the loud marker, never a number
    aggs = LtsAggregator(_build_graph_tools(_AlwaysStale()))
    for name, res in (("sum", aggs.sum("tokens", corpus="graph")),
                      ("distinct", aggs.distinct("prefix", corpus="graph")),
                      ("cross_tab", aggs.cross_tab("prefix", "domain", corpus="graph")),
                      ("top_k", aggs.top_k("prefix", 3, corpus="graph")),
                      ("degree", aggs.degree("DEC-N1", corpus="graph")),
                      ("top_by_degree", aggs.top_by_degree(5, corpus="graph")),
                      ("count_by_edge_type", aggs.count_by_edge_type(corpus="graph")),
                      ("orphans", aggs.orphans(corpus="graph"))):
        _check(res.get("success") is False and res.get("stale") is True,
               f"stale propagates for {name}", failures)


def _tier_b(failures) -> None:
    corpus = DEFAULT_CORPUS
    print(f"[tier B] live '{corpus}' corpus (best-effort; corpus-agnostic invariants)")
    try:
        agg = LtsAggregator()  # builds the default store from the corpus config
        g = agg.group_by("lifecycle-position", corpus=corpus, area="decisions")
    except Exception as exc:  # noqa: BLE001
        print(f"  SKIP: engine/corpus unavailable ({type(exc).__name__}: {exc})")
        return
    if not g.get("success"):
        print(f"  SKIP: corpus not queryable ({g.get('error') or g})")
        return

    print(f"  group-by lifecycle-position  (total={g['total']}, distinct={g['distinct']}):")
    width = max((len(b["value"]) for b in g["buckets"]), default=0)
    for b in g["buckets"]:
        print(f"    {b['value']:<{width}}  {b['count']:>4}  {'#' * min(b['count'], 50)}")

    # Corpus-AGNOSTIC structural invariants only (no assumptions about which values or
    # prefixes a real corpus contains — this library ships generic).
    _check(sum(b["count"] for b in g["buckets"]) == g["total"], "live: bucket counts sum to total", failures)

    graw = agg.group_by("lifecycle-position", corpus=corpus, area="decisions", raw=True)
    _check(graw["distinct"] >= g["distinct"],
           f"live: raw distinct >= canonical distinct (raw {graw['distinct']} >= canonical {g['distinct']})",
           failures)

    gd = agg.group_by("domain", corpus=corpus)
    print(f"  count nodes group-by domain: {gd['groups']}")
    _check(sum(b["count"] for b in gd["buckets"]) == gd["total"], "live: domain counts sum to total", failures)

    tk = agg.sum("token_count", corpus=corpus)
    if tk.get("success"):
        print(f"  sum(token_count): {int(tk['sum'])}  over {tk['count']} contributing (skipped {tk['skipped']})")
        _check(tk["count"] + tk["skipped"] == tk["total"], "live: numeric contributed + skipped == total", failures)
    ct = agg.cross_tab("lifecycle-position", "domain", corpus=corpus, area="decisions")
    if ct.get("success"):
        print(f"  lifecycle x domain: rows={ct['rows']} cols={ct['cols']}")
        _check(sum(b["count"] for b in ct["buckets"]) == ct["total"], "live: cross-tab cells sum to total", failures)
    et = agg.count_by_edge_type(corpus=corpus)
    if et.get("success"):
        print(f"  edge-type histogram (resolved={et['resolved_edges']}/{et['total_edges']}): {et['groups']}")
        _check(sum(b["count"] for b in et["buckets"]) == et["resolved_edges"], "live: edge-type counts sum to resolved", failures)
    tbd = agg.top_by_degree(5, corpus=corpus, area="decisions")
    if tbd.get("success"):
        print("  most-connected nodes:")
        for b in tbd["buckets"]:
            print(f"    {b['node']:<32} deg={b['degree']} (in {b['in_degree']}/out {b['out_degree']})")
        _check(tbd["buckets"] == sorted(tbd["buckets"], key=lambda r: (-r["degree"], r["node"])),
               "live: top_by_degree sorted deterministically", failures)
    orph = agg.orphans(corpus=corpus, area="decisions", limit=8)
    if orph.get("success"):
        print(f"  orphans (in-degree 0): {orph['orphan_count']}/{orph['total']} — {orph['orphans']}")


def run() -> bool:
    failures: list = []
    _tier_a(failures)
    _tier_a2(failures)
    _tier_b(failures)
    if failures:
        print(f"\nSELFTEST FAILED — {len(failures)} check(s): {failures}")
        return False
    print("\nSELFTEST PASSED")
    return True


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
