"""THE READ PROFILE — what the corpus is asked for, and what it is never asked for.

    python -m factlattice_engine.readprofile --corpus <id> [--days 30] [--json]

⛔ A LOG NOBODY READS IS TELEMETRY, NOT AN INSTRUMENT. This project has already shipped
two counters structurally incapable of incrementing and a `lost: 0` that could not fall;
writing a query log and stopping there would be the same mistake with a bigger file. So
the reader ships with the writer.

THREE VIEWS, AND THE FIRST IS THE POINT
-----------------------------------------
1. ⭐ NEVER READ — nodes in the corpus that appear in NO result over the window, WITH A
   DENOMINATOR. This is the view no other instrument in the engine can produce. A node
   that is well-formed, correct, lint-clean and entirely unread passes every check there
   is; the only thing wrong with it is that it answers no question anyone asks. Over a
   window that makes it one of three things — mis-named (nobody guesses its id),
   mis-filed (it sits in a corpus nobody queries), or it should not have been written.

   ⛔ AND IT IS REPORTED AS A RATE, NEVER AS A WORKLIST. "412 of 1,471 nodes unread in 30
   days" is a measurement. A list of 412 ids to go and delete is an invitation to destroy
   a reference corpus because nobody happened to query it this month. The ids are
   available (`--list`) and are deliberately not the headline.

2. RE-QUERY STORMS — one caller, one corpus, N DISTINCT questions inside a short window,
   and EVERY ONE of the counted questions came back EMPTY. That is
   [FL-RECALL-ZERO-IS-AMBIGUOUS] observed from the engine side rather than reconstructed
   from a transcript: "the correct response to any zero is currently to re-query three
   more ways and then grep." Each storm is one place the tool lost, and the count is a
   number that can FALL as the recall work lands.

3. SHAPE — tool mix, zero-rate, truncation rate, p50/p95 latency. Ordinary, and last,
   because it is the view that is easy to build and least worth having.

⚠ WHAT THIS CANNOT TELL YOU, STATED UP FRONT. Absence of a read is not absence of a
reader: a node read via `grep` — which is the documented, measured behaviour this whole
series is about — never appears here. So the never-read population is an UPPER BOUND on
what is genuinely unused, and it will overstate until reflexive reach improves. Reporting
it any other way would repeat the mistake of quoting a number whose scope is not attached
to it.
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .querylog import log_path

# A storm: this many DISTINCT questions that ALL returned NOTHING, to one corpus, from one
# session, inside this many seconds. Deliberately conservative on both axes — a caller
# paginating or drilling down is not a storm, and a rule that calls ordinary work a
# failure is a rule that gets ignored, which costs the cases that were real.
STORM_WINDOW_S = 180
STORM_MIN_DISTINCT = 3


def read_records(path: Optional[Path] = None) -> List[dict]:
    """Every well-formed record. ⛔ A corrupt line is SKIPPED AND COUNTED, never fatal:
    an append-only log written by a crashing process will have a torn tail, and a reader
    that dies on it is a reader that stops working exactly when it is most needed."""
    p = path or log_path()
    if p is None or not Path(p).is_file():
        return []
    out: List[dict] = []
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _ts(rec: dict) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(rec.get("ts")))
    except (TypeError, ValueError):
        return None


def within(records: Iterable[dict], days: Optional[int]) -> List[dict]:
    if not days:
        return list(records)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for r in records:
        t = _ts(r)
        if t is None or t >= cutoff:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# 1. NEVER READ
# ---------------------------------------------------------------------------
def never_read(records: Iterable[dict], corpus_nodes: Dict[str, Iterable[str]]) -> dict:
    """Nodes never delivered by any logged call, per corpus, WITH the denominator.

    `corpus_nodes` is `{corpus_id: [node_id, ...]}` — supplied by the caller from a live
    store, so this module needs no engine import and stays testable with a dict.
    """
    read: set = set()
    capped_records = 0
    for r in records:
        if r.get("result_ids_capped"):
            capped_records += 1
        for nid in (r.get("result_ids") or []):
            read.add(str(nid))

    per_corpus = {}
    for cid, nodes in sorted(corpus_nodes.items()):
        all_ids = sorted({str(n) for n in nodes})
        unread = [n for n in all_ids if n not in read]
        total = len(all_ids)
        per_corpus[cid] = {
            "nodes_total": total,
            "nodes_read": total - len(unread),
            "nodes_never_read": len(unread),
            "never_read_rate": round(len(unread) / total, 4) if total else None,
            "never_read_ids": unread,
        }
    return {
        "per_corpus": per_corpus,
        "records_considered": sum(1 for _ in records) if isinstance(records, list) else None,
        "records_with_capped_ids": capped_records,
        "caveat": (
            "⚠ AN UPPER BOUND, NOT A DELETION LIST. Absence of a logged read is not "
            "absence of a reader: a node read via `grep`, or via a file open, never "
            "appears here — and that is the documented, measured behaviour this engine's "
            "recall work exists to change. The rate will overstate until reflexive reach "
            "improves. A never-read node is a prompt to ask whether it is mis-NAMED or "
            "mis-FILED; it is not evidence that it is unwanted."
            + (f" ⛔ {capped_records} record(s) hit the per-record id cap, so a few reads "
               f"are not represented and the unread count is slightly HIGH."
               if capped_records else "")),
    }


# ---------------------------------------------------------------------------
# 2. RE-QUERY STORMS
# ---------------------------------------------------------------------------
def _question_key(rec: dict) -> str:
    """What was ASKED, independent of phrasing. The digest is enough — distinctness is
    exactly what a hash preserves, and it is all a storm needs."""
    f = rec.get("filters") or {}
    parts = [rec.get("tool", "")]
    for k in ("prefix", "status", "area", "domain", "node_id", "start_node", "topic"):
        if f.get(k) is not None:
            parts.append(f"{k}={f[k]}")
    if f.get("text_sha") or f.get("text"):
        parts.append(f"text={f.get('text_sha') or f.get('text')}")
    # [FIND-QUERY-SURFACE] a where-only / terms-only call is a question like a prefix-only one:
    # distinct conditions are distinct questions (values are digests unless literals are on).
    for k in ("document_tag", "source_file", "match", "fields", "all_of", "any_of", "none_of", "where"):
        if f.get(k) is not None:
            parts.append(f"{k}={json.dumps(f[k], sort_keys=True, default=str)}")
    return "|".join(parts)


def storms(records: Iterable[dict], *, window_s: int = STORM_WINDOW_S,
           min_distinct: int = STORM_MIN_DISTINCT) -> List[dict]:
    """Bursts of DISTINCT questions to one corpus from one session, with a zero among them.

    ⛔ THE DISTINCTNESS IS COUNTED AMONG THE ZEROS, NOT ACROSS THE WINDOW, and that
    correction came from the first live run: a window containing two SUCCESSFUL calls
    followed by three empty ones reported "5 distinct questions", inflating the storm with
    the work that went fine. A caller who asks four sharpening questions and gets four
    useful answers is WORKING; a caller who asks three different ways and gets nothing
    three times is re-phrasing against a wall. Only the second is a place the tool lost,
    and only the second is a number that should fall — so only the second is counted.
    """
    by_session: Dict[Any, List[dict]] = defaultdict(list)
    for r in records:
        if not (r.get("tool") or "").startswith("lts_"):
            continue
        t = _ts(r)
        if t is None:
            continue
        cid = (r.get("filters") or {}).get("corpus", "<all>")
        by_session[(r.get("session"), cid)].append({**r, "_t": t})

    out: List[dict] = []
    for (session, cid), recs in sorted(by_session.items(), key=lambda kv: str(kv[0])):
        recs.sort(key=lambda r: r["_t"])
        i = 0
        while i < len(recs):
            j, window = i, [recs[i]]
            while j + 1 < len(recs) and (recs[j + 1]["_t"] - recs[i]["_t"]).total_seconds() <= window_s:
                j += 1
                window.append(recs[j])
            zeros = [r for r in window if (r.get("count") == 0
                                           or r.get("total_matched") == 0)]
            distinct_zero = {_question_key(r) for r in zeros}
            if len(distinct_zero) >= min_distinct:
                span = round((zeros[-1]["_t"] - zeros[0]["_t"]).total_seconds(), 1)
                out.append({
                    "session": session,
                    "corpus": cid,
                    "started": zeros[0]["_t"].isoformat(timespec="seconds"),
                    "span_s": span,
                    "calls_in_window": len(window),
                    "distinct_empty_questions": len(distinct_zero),
                    "zero_answers": len(zeros),
                    "tools": sorted({r.get("tool") for r in zeros}),
                    "reading": (f"a caller asked {len(distinct_zero)} DIFFERENT questions "
                                f"in {span}s and every one came back empty. That is not a "
                                f"corpus with nothing in it — it is a caller who cannot "
                                f"find the way in, and the first answer having no extent "
                                f"is why they had to guess again."),
                })
                i = j + 1
            else:
                i += 1
    return out


# ---------------------------------------------------------------------------
# 3. SHAPE
# ---------------------------------------------------------------------------
def shape(records: List[dict]) -> dict:
    if not records:
        return {"calls": 0}
    lat = sorted(float(r.get("elapsed_ms") or 0.0) for r in records)
    counted = [r for r in records if isinstance(r.get("count"), int)]
    zeros = sum(1 for r in counted if r["count"] == 0)
    trunc = sum(1 for r in records if r.get("truncated"))

    def pct(p: float) -> float:
        return round(lat[min(len(lat) - 1, int(len(lat) * p))], 2)

    return {
        "calls": len(records),
        "by_tool": dict(Counter(r.get("tool") for r in records).most_common()),
        "by_corpus": dict(Counter((r.get("filters") or {}).get("corpus", "<all>")
                                  for r in records).most_common()),
        "answers_counted": len(counted),
        "zero_answers": zeros,
        "zero_rate": round(zeros / len(counted), 4) if counted else None,
        "truncated": trunc,
        "latency_ms": {"p50": pct(0.50), "p95": pct(0.95), "max": round(lat[-1], 2)},
        "sessions": len({r.get("session") for r in records}),
        "window": {"first": records[0].get("ts"), "last": records[-1].get("ts")},
    }


def report(corpus_nodes: Dict[str, Iterable[str]], *, days: Optional[int] = 30,
           path: Optional[Path] = None) -> dict:
    records = within(read_records(path), days)
    return {
        "log_path": str(path or log_path()),
        "window_days": days,
        "shape": shape(records),
        "never_read": never_read(records, corpus_nodes),
        "storms": storms(records),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _live_corpus_nodes(corpus: Optional[str]) -> Dict[str, List[str]]:
    from . import config
    store = config.build_store()
    return {p.corpus_id: list(p.nodes)
            for p in store.partitions()
            if corpus is None or p.corpus_id == corpus}


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m factlattice_engine.readprofile",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", default=None, help="restrict to one corpus id")
    ap.add_argument("--days", type=int, default=30, help="window (0 = everything)")
    ap.add_argument("--log", default=None, help="log path (default: the state dir)")
    ap.add_argument("--list", action="store_true", help="print the never-read ids")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    try:
        nodes = _live_corpus_nodes(a.corpus)
    except Exception as exc:  # noqa: BLE001
        print(f"could not load corpora ({type(exc).__name__}: {exc}); "
              f"reporting SHAPE and STORMS only")
        nodes = {}
    rep = report(nodes, days=a.days or None,
                 path=Path(a.log).expanduser() if a.log else None)

    if a.json:
        if not a.list:
            for c in rep["never_read"]["per_corpus"].values():
                c.pop("never_read_ids", None)
        print(json.dumps(rep, indent=2, default=str))
        return 0

    s = rep["shape"]
    print(f"READ PROFILE — {rep['log_path']}")
    print(f"  window: {rep['window_days'] or 'all'} day(s)   calls: {s.get('calls', 0)}")
    if not s.get("calls"):
        print("  ⚠ NO RECORDS. Either nothing has been asked yet, or the log is "
              "switched off (FACTLATTICE_QUERY_LOG=0). A zero here is not a finding.")
        return 0
    print(f"  zero answers: {s['zero_answers']}/{s['answers_counted']} "
          f"({s['zero_rate']})   truncated: {s['truncated']}   sessions: {s['sessions']}")
    print(f"  latency ms: p50={s['latency_ms']['p50']} p95={s['latency_ms']['p95']} "
          f"max={s['latency_ms']['max']}")
    print("\n  NEVER READ (the view nothing else can produce)")
    for cid, c in rep["never_read"]["per_corpus"].items():
        print(f"    {cid:<28} {c['nodes_never_read']:>5} of {c['nodes_total']:>5} "
              f"unread  ({c['never_read_rate']})")
        if a.list and c["never_read_ids"]:
            for nid in c["never_read_ids"]:
                print(f"        {nid}")
    print(f"    {rep['never_read']['caveat']}")
    print(f"\n  RE-QUERY STORMS: {len(rep['storms'])}")
    for st in rep["storms"]:
        print(f"    {st['started']}  {st['corpus']}  "
              f"{st['distinct_empty_questions']} distinct questions, ALL empty, "
              f"in {st['span_s']}s")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
