"""CLI for factlattice_engine.analytics — count / group-by / numeric / distinct / cross-tab /
top-k / degree / edge-types / orphans / worked examples / selftest.

    python3 -m factlattice_engine.analytics count    --corpus alpha [--group-by ATTR] [filters]
    python3 -m factlattice_engine.analytics group-by ATTR --corpus alpha [filters]
    python3 -m factlattice_engine.analytics sum   ATTR [--group-by ATTR] [filters]   # avg|min|max too
    python3 -m factlattice_engine.analytics distinct  ATTR [filters]
    python3 -m factlattice_engine.analytics cross-tab ATTR1 ATTR2 [filters]
    python3 -m factlattice_engine.analytics top-k ATTR --k 5 [--by count|<numeric_attr> --agg sum] [filters]
    python3 -m factlattice_engine.analytics degree NODE --corpus ...
    python3 -m factlattice_engine.analytics top-by-degree --k 5 [--direction both|in|out] [filters]
    python3 -m factlattice_engine.analytics edge-types --corpus ...
    python3 -m factlattice_engine.analytics orphans [--limit 20] [filters]
    python3 -m factlattice_engine.analytics examples  [--corpus alpha]
    python3 -m factlattice_engine.analytics --selftest

filters: --prefix DEC --domain LIB.OPS --status ... --area decisions
         --since 2026-07-01 --until 2026-07-31 --text ... --raw
         --date-granularity {day,month,year}
"""

from __future__ import annotations

import argparse
import json
import sys

from .core import LtsAggregator

# The worked example queries shipped with the deliverable — each is (label, kwargs
# for group_by/count). `examples` runs them all against the live corpus.
DEFAULT_CORPUS = "alpha"


def _example_specs(corpus: str):
    return [
        ("count decisions group-by lifecycle-position (canonical)",
         "group_by", dict(attr="lifecycle-position", corpus=corpus, area="decisions")),
        ("count decisions group-by lifecycle-position (RAW, un-collapsed)",
         "group_by", dict(attr="lifecycle-position", corpus=corpus, area="decisions", raw=True)),
        ("count nodes group-by domain",
         "group_by", dict(attr="domain", corpus=corpus)),
        ("count nodes group-by prefix",
         "group_by", dict(attr="prefix", corpus=corpus)),
        ("count nodes group-by importance",
         "group_by", dict(attr="importance", corpus=corpus)),
        ("count DEC nodes group-by decided-in-session (month)",
         "group_by", dict(attr="decided-in-session", corpus=corpus, prefix="DEC",
                          date_granularity="month")),
        ("count all nodes (scalar)",
         "count", dict(corpus=corpus)),
        ("count FIX nodes (scalar)",
         "count", dict(corpus=corpus, prefix="FIX")),
        # Tier 1 — numeric / distinct / cross-tab / top_k
        ("sum token_count over all nodes",
         "numeric", dict(attr="token_count", op="sum", corpus=corpus)),
        ("avg token_count per prefix",
         "numeric", dict(attr="token_count", op="avg", corpus=corpus, group_by="prefix")),
        ("distinct lifecycle-position values (decisions)",
         "distinct", dict(attr="lifecycle-position", corpus=corpus, area="decisions")),
        ("decisions cross-tab lifecycle-position x domain",
         "cross_tab", dict(attr1="lifecycle-position", attr2="domain", corpus=corpus, area="decisions")),
        ("top-5 prefixes by node count",
         "top_k", dict(attr="prefix", k=5, corpus=corpus)),
        ("top-5 root-causes (decided-in-session) by count",
         "top_k", dict(attr="decided-in-session", k=5, corpus=corpus, prefix="DEC")),
        # Tier 2 — graph-native
        ("edge-type histogram",
         "count_by_edge_type", dict(corpus=corpus)),
        ("most-connected decisions (top-5 by degree)",
         "top_by_degree", dict(k=5, corpus=corpus, area="decisions")),
        ("orphan decisions (no incoming edge)",
         "orphans", dict(corpus=corpus, area="decisions", limit=10)),
    ]


def _render(label: str, res: dict) -> None:
    print(f"\n=== {label} ===")
    if not res.get("success"):
        print(f"  (query failed): {res.get('error') or res}")
        return
    # numeric global aggregate
    if "op" in res and "value" in res and "buckets" not in res:
        print(f"  {res['op']}({res['attr']}) = {res['value']}   "
              f"(count={res['count']} contributed, skipped={res['skipped']}, total={res['total']})")
        print(f"  sum={res['sum']} avg={res['avg']} min={res['min']} max={res['max']}")
        return
    # numeric grouped aggregate
    if "op" in res and res.get("buckets") and "sum" in res["buckets"][0]:
        op = res["op"]
        print(f"  {op}({res['attr']}) by {res['group_attr']}  total={res['total']} skipped={res['skipped']}")
        width = max((len(b["value"]) for b in res["buckets"]), default=0)
        for b in res["buckets"]:
            print(f"    {b['value']:<{width}}  {op}={b[op]}  (n={b['count']})")
        return
    # cross-tab
    if "matrix" in res:
        print(f"  {res['attr1']} x {res['attr2']}  total={res['total']}")
        cols = res["cols"]
        cw = max((len(c) for c in cols), default=3)
        rw = max((len(r) for r in res["rows"]), default=3)
        print("    " + " " * rw + "  " + "  ".join(f"{c:>{cw}}" for c in cols))
        for r in res["rows"]:
            row = res["matrix"][r]
            print(f"    {r:<{rw}}  " + "  ".join(f"{row[c]:>{cw}}" for c in cols))
        return
    # distinct
    if "values" in res and "distinct" in res:
        print(f"  distinct({res['attr']}) = {res['distinct']}  (counted {res['counted']}/{res['total']})")
        print(f"    {res['values']}")
        return
    # degree (single node)
    if "node" in res and "degree" in res:
        print(f"  degree({res['node']}) = {res['degree']}  (in {res['in_degree']} / out {res['out_degree']})")
        return
    # top_by_degree
    if res.get("buckets") and "node" in res["buckets"][0]:
        print(f"  top-{res['k']} by degree ({res.get('direction','both')})  total={res['total']}")
        width = max((len(b["node"]) for b in res["buckets"]), default=0)
        for b in res["buckets"]:
            print(f"    {b['node']:<{width}}  deg={b['degree']} (in {b['in_degree']}/out {b['out_degree']})")
        return
    # edge-type histogram
    if "total_edges" in res:
        print(f"  edge types  resolved={res['resolved_edges']}/{res['total_edges']}  distinct={res['distinct']}")
        width = max((len(b["value"]) for b in res["buckets"]), default=0)
        for b in res["buckets"]:
            print(f"    {b['value']:<{width}}  {b['count']:>4}  {'#' * min(b['count'], 50)}")
        return
    # orphans
    if "orphan_count" in res:
        print(f"  orphans (in-degree 0): {res['orphan_count']}/{res['total']}  "
              f"(ratio {res['orphan_ratio']:.3f}){'  [truncated]' if res.get('truncated') else ''}")
        print(f"    {res['orphans']}")
        return
    # count-style buckets (group_by / top_k by count)
    if "buckets" in res:
        head = f"  total={res['total']}"
        if "distinct" in res:
            head += f"  distinct={res['distinct']}"
        if "attr" in res:
            head += f"  attr={res['attr']}"
        print(head)
        width = max((len(b["value"]) for b in res["buckets"]), default=0)
        for b in res["buckets"]:
            print(f"    {b['value']:<{width}}  {b['count']:>4}  {'#' * min(b['count'], 50)}")
        return
    # scalar count
    print(f"  total={res['total']}  filters={res.get('filters')}")


def _run_examples(agg: LtsAggregator, corpus: str) -> None:
    for label, kind, kwargs in _example_specs(corpus):
        fn = getattr(agg, kind)
        _render(label, fn(**kwargs))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="factlattice_engine.analytics", add_help=True)
    ap.add_argument("--selftest", action="store_true", help="run the selftest and exit")
    sub = ap.add_subparsers(dest="cmd")

    def add_filters(p):
        p.add_argument("--corpus", default=DEFAULT_CORPUS)
        p.add_argument("--prefix"); p.add_argument("--domain"); p.add_argument("--status")
        p.add_argument("--area"); p.add_argument("--function"); p.add_argument("--service")
        p.add_argument("--text"); p.add_argument("--since"); p.add_argument("--until")
        p.add_argument("--raw", action="store_true")
        p.add_argument("--date-granularity", default="month",
                       choices=("day", "month", "year"))
        p.add_argument("--json", action="store_true", help="emit raw JSON")

    pc = sub.add_parser("count"); pc.add_argument("--group-by", dest="group_by"); add_filters(pc)
    pg = sub.add_parser("group-by"); pg.add_argument("attr"); add_filters(pg)
    for op in ("sum", "avg", "min", "max"):
        pn = sub.add_parser(op); pn.add_argument("attr")
        pn.add_argument("--group-by", dest="group_by"); add_filters(pn)
    pd = sub.add_parser("distinct"); pd.add_argument("attr"); add_filters(pd)
    px = sub.add_parser("cross-tab"); px.add_argument("attr1"); px.add_argument("attr2"); add_filters(px)
    pt = sub.add_parser("top-k"); pt.add_argument("attr"); pt.add_argument("--k", type=int, default=10)
    pt.add_argument("--by", default="count"); pt.add_argument("--agg", default="sum",
                    choices=("sum", "avg", "min", "max")); add_filters(pt)
    pdeg = sub.add_parser("degree"); pdeg.add_argument("node"); add_filters(pdeg)
    ptd = sub.add_parser("top-by-degree"); ptd.add_argument("--k", type=int, default=10)
    ptd.add_argument("--direction", default="both", choices=("both", "in", "out")); add_filters(ptd)
    pet = sub.add_parser("edge-types"); add_filters(pet)
    po = sub.add_parser("orphans"); po.add_argument("--limit", type=int, default=None); add_filters(po)
    pe = sub.add_parser("examples"); pe.add_argument("--corpus", default=DEFAULT_CORPUS)

    args = ap.parse_args(argv)

    if args.selftest:
        from .selftest import run
        return 0 if run() else 1

    if args.cmd is None:
        ap.print_help()
        print("\nTip: `python3 -m factlattice_engine.analytics examples` runs worked queries.")
        return 0

    if args.cmd == "examples":
        _run_examples(LtsAggregator(), args.corpus)
        return 0

    filt = {k: getattr(args, k) for k in
            ("prefix", "domain", "status", "area", "function", "service",
             "text", "since", "until") if getattr(args, k, None)}
    agg = LtsAggregator()
    raw = getattr(args, "raw", False)
    dg = getattr(args, "date_granularity", "month")
    if args.cmd == "group-by":
        res = agg.group_by(args.attr, corpus=args.corpus, raw=raw,
                           date_granularity=dg, **filt)
        label = f"group-by {args.attr}"
    elif args.cmd in ("sum", "avg", "min", "max"):
        res = agg.numeric(args.attr, op=args.cmd, corpus=args.corpus,
                          group_by=args.group_by, raw=raw, date_granularity=dg, **filt)
        label = f"{args.cmd}({args.attr})" + (f" by {args.group_by}" if args.group_by else "")
    elif args.cmd == "distinct":
        res = agg.distinct(args.attr, corpus=args.corpus, raw=raw,
                           date_granularity=dg, **filt)
        label = f"distinct {args.attr}"
    elif args.cmd == "cross-tab":
        res = agg.cross_tab(args.attr1, args.attr2, corpus=args.corpus, raw=raw,
                            date_granularity=dg, **filt)
        label = f"cross-tab {args.attr1} x {args.attr2}"
    elif args.cmd == "top-k":
        res = agg.top_k(args.attr, args.k, by=args.by, agg=args.agg,
                        corpus=args.corpus, raw=raw, date_granularity=dg, **filt)
        label = f"top-{args.k} {args.attr} by {args.by}"
    elif args.cmd == "degree":
        res = agg.degree(args.node, corpus=args.corpus, **filt)
        label = f"degree {args.node}"
    elif args.cmd == "top-by-degree":
        res = agg.top_by_degree(args.k, corpus=args.corpus,
                                direction=args.direction, **filt)
        label = f"top-{args.k} by degree"
    elif args.cmd == "edge-types":
        res = agg.count_by_edge_type(corpus=args.corpus, **filt)
        label = "edge-type histogram"
    elif args.cmd == "orphans":
        res = agg.orphans(corpus=args.corpus, limit=args.limit, **filt)
        label = "orphans"
    else:  # count
        res = agg.count(corpus=args.corpus, group_by=args.group_by, raw=raw,
                        date_granularity=dg, **filt)
        label = f"count group-by {args.group_by}" if args.group_by else "count"

    if getattr(args, "json", False):
        print(json.dumps(res, indent=2))
    else:
        _render(label, res)
    return 0 if res.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
