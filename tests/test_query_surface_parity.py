"""[FIND-QUERY-SURFACE] parity tests (spec §10 items 1, 7, 9, 10).

* C2 surface parity: >= 20 existing calls are BYTE-IDENTICAL to the responses recorded on main 64ee63f
  (tests/fixtures/qs_base_responses.json, recorded by tests/fixtures/record_qs_base.py on a detached
  worktree of the base). These are CHARACTERISATION tests: green at base by construction.
* Cross-tool parity: lts_aggregate count == lts_find total, lts_find_in_prose's population == lts_find's set.
* An independent re-computation over a REAL snapshot corpus (read-only), property-based with a fixed seed.
* The axis-1 budget of §8 on that snapshot.
"""
from __future__ import annotations

import json
import os
import random
import re
import statistics
import time
from pathlib import Path

import pytest

from factlattice_engine import config, tools
from tests import qs_fixture as F

FIX = Path(__file__).parent / "fixtures" / "qs_base_responses.json"
SNAP_CFG = Path.home() / ".lts-engine" / "v3-snapshot-20261003T203844" / "corpus-config.json"
# Machine-local, deployment-specific: the snapshot corpora and a query word present in the big one are named by the
# environment, never in this file. Unset => these real-corpus checks are SKIPPED (and say so).
BIG = os.environ.get("FACTLATTICE_SNAP_BIG_CORPUS", "")
SECOND = os.environ.get("FACTLATTICE_SNAP_SECOND_CORPUS", "")
WORD = os.environ.get("FACTLATTICE_SNAP_WORD", "")


# ------------------------------------------------------------------ C2 surface parity (item 1)
def test_c2_surface_parity_against_recorded_base(tmp_path):
    base = json.loads(FIX.read_text(encoding="utf-8"))
    assert len(base) >= 20
    now = F.run_base_calls(tmp_path, tmp_path / "state")
    assert list(now) == list(base)
    bad = [k for k in base if json.dumps(now[k], sort_keys=False) != json.dumps(base[k], sort_keys=False)]
    assert not bad, f"responses changed for calls that use none of the new parameters: {bad}"


def test_c2_old_calls_carry_no_new_keys(tmp_path):
    now = F.run_base_calls(tmp_path, tmp_path / "state")
    for name, v in now.items():
        for k in ("query_interpreted", "warnings", "matched_all_words"):
            assert k not in v["response"], (name, k)
        assert "word_fallback" not in v["log"], name


def test_c2_explicit_none_params_are_the_old_path(tmp_path):
    """The dispatch passes only keys present in the arguments, but a client may send nulls."""
    eng, _ = F.engine_a(tmp_path)
    plain = tools.dispatch("lts_find", {"corpus": "qs", "prefix": "PROC-", "semantic": "off"}, eng)
    nulls = tools.dispatch("lts_find", {"corpus": "qs", "prefix": "PROC-", "semantic": "off", "all_of": None,
                                        "any_of": None, "none_of": None, "match": None, "where": None,
                                        "fields": None, "source_file": None, "document_tag": None,
                                        "explain": False}, eng)
    assert json.dumps(plain, sort_keys=True) == json.dumps(nulls, sort_keys=True)


# ------------------------------------------------------------------ cross-tool parity (item 7)
CONDS = [
    dict(all_of=["live"]),
    dict(prefix="PROC-", none_of=["live"]),
    dict(where=[{"key": "ykey", "not_one_of": ["a", "b"]}]),
    dict(all_of=[{"term": "lantern", "match": "prefix"}], any_of=["qa", "alias"]),
    dict(where=[{"key": "kind", "equals": "note"}], document_tag="SPEC"),
    dict(source_file="b.lts"),
]


@pytest.mark.parametrize("cond", CONDS, ids=lambda c: ",".join(sorted(c)))
def test_aggregate_count_equals_find_total(tmp_path, cond):
    eng, _ = F.engine_a(tmp_path)
    f = eng.find(corpus="qs", semantic="off", tier="digest", **cond)
    assert f["success"], f
    agg = tools.dispatch("lts_aggregate", {"op": "count", "corpus": "qs", **cond}, eng)
    assert agg["success"], agg
    assert agg["total"] == f["total_matched"]
    assert "query_interpreted" in agg      # the aggregate carries the echo of the find it drove


@pytest.mark.parametrize("cond", CONDS, ids=lambda c: ",".join(sorted(c)))
def test_prose_population_equals_find_set(tmp_path, cond):
    """Every fixture node has a `- status:` property line (a body row), so prose for 'status'
    over the same conditions returns exactly the find set: one predicate, one population."""
    eng, _ = F.engine_a(tmp_path)
    f = eng.find(corpus="qs", semantic="off", **cond)
    p = eng.find_in_prose(text="status", corpus="qs", **cond)
    assert p["success"], p
    assert sorted(h["id"] for h in p["hits"]) == sorted(n["id"] for n in f["nodes"])
    assert p["total_matched"] == f["total_matched"]


def test_prose_refuses_what_find_refuses(tmp_path):
    eng, _ = F.engine_a(tmp_path)
    for kw in (dict(all_of=[]), dict(where=[{"key": "Bad", "has": True}]), dict(source_file="nope.lts")):
        f = eng.find(corpus="qs", semantic="off", **kw)
        p = eng.find_in_prose(text="status", corpus="qs", **kw)
        assert f["success"] is False and p["success"] is False


def test_profile_audit_required_scope_accepts_a_dict(tmp_path):
    eng, _ = F.engine_a(tmp_path)
    r = tools.dispatch("lts_profile_audit", {"corpus": "qs", "required_property": "kind",
                                             "required_scope": {"prefix": "PROC-",
                                                                "where": [{"key": "tier", "has": True}]}}, eng)
    assert r["success"], r
    assert r["conformance"]["total"] == 3          # PROC- nodes carrying `tier`: LANTERN-ARCH, ALANTERN, LIVE2
    assert r["conformance"]["present"] == 2        # of those, LANTERN-ARCH and ALANTERN carry `kind`


# ------------------------------------------------------------------ real-snapshot recomputation (item 9)
@pytest.fixture(scope="module")
def snap():
    if not SNAP_CFG.exists() or not (BIG and SECOND and WORD):
        pytest.skip(f"snapshot {SNAP_CFG} or FACTLATTICE_SNAP_BIG_CORPUS/SECOND_CORPUS/WORD not set: the independent real-corpus re-computation did NOT run")
    cfg = json.loads(SNAP_CFG.read_text())
    return tools.LtsEngine(config.build_store(cfg))


def _fields(nid, nd):
    """INDEPENDENT of the engine: id, title, string values and string list elements."""
    out = [nid, nd.get("title") or ""]
    for v in (nd.get("properties") or {}).values():
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            out += [e for e in v if isinstance(e, str)]
    return out


def _w(s):
    return re.findall(r"[a-z0-9]+", s.lower())


class _Pre(list):
    """A node's field strings with the (independent) lowercase/word splits computed once."""
    def __init__(self, fields):
        super().__init__(fields)
        self.low = [f.lower() for f in fields]
        self.words = [_w(f) for f in fields]


def _hit(term, mode, fields):
    if not isinstance(fields, _Pre):
        fields = _Pre(fields)
    if mode == "phrase":
        t = term.lower()
        return any(t in f for f in fields.low)
    tw = _w(term)
    if mode == "prefix":
        return any(w.startswith(tw[0]) for ws in fields.words for w in ws)
    n = len(tw)
    for ws in fields.words:
        if any(ws[i:i + n] == tw for i in range(len(ws) - n + 1)):
            return True
    return False


def _ids_of(eng, corpus, **kw):
    """One page with a budget no corpus reaches (the aggregator's cursor loop would re-run the
    whole predicate per page, which is slow on 9,661 nodes and not what is under test)."""
    r = eng.find(corpus=corpus, semantic="off", token_budget=10 ** 9, **kw)
    assert r["success"] and not r["truncated"], r
    return sorted(n["id"] for n in r["nodes"])


@pytest.mark.parametrize("corpus", [c for c in (BIG, SECOND) if c])
def test_snapshot_modes_match_an_independent_recomputation(snap, corpus):
    part = snap._gate(corpus)[0]
    nodes = part.nodes
    pre = {nid: _Pre(_fields(nid, nd)) for nid, nd in nodes.items()}
    rng = random.Random(20261005)
    titles = [nd.get("title") or "" for nd in nodes.values() if len(_w(nd.get("title") or "")) >= 2]
    checked = 0
    for _ in range(40):
        t = rng.choice(titles)
        ws = _w(t)
        i = rng.randrange(len(ws))
        word, bigram = ws[i], " ".join(ws[i:i + 2])
        cases = [(word, "phrase"), (word, "word"), (bigram, "word"), (bigram, "phrase"),
                 (word[: max(2, len(word) - 1)], "prefix")]
        for term, mode in cases:
            if mode == "prefix" and len(term) < 2:
                term = word + "x" if len(word) < 2 else term       # 1-char words: a refused prefix; probe a 2-char one
            exp = sorted(nid for nid, p in pre.items() if _hit(term, mode, p))
            got = _ids_of(snap, corpus, all_of=[{"term": term, "match": mode}])
            assert got == exp, (corpus, term, mode)
            checked += 1
    assert checked == 200
    # all/any/none combine
    a, b, c = "zero", "key", "map"
    got = _ids_of(snap, corpus, all_of=[a], any_of=[{"term": b, "match": "prefix"}, c], none_of=["note"],
                  match="word")
    # `match='word'` is the default for plain-string terms; the object form pins prefix for b, and `note` is word mode too
    exp = sorted(nid for nid, f in pre.items()
                 if _hit(a, "word", f) and (_hit(b, "prefix", f) or _hit(c, "word", f))
                 and not _hit("note", "word", f))
    assert got == exp


def _vals(v):
    if isinstance(v, str):
        return [v.strip()]
    if isinstance(v, list):
        return [e.strip() for e in v if isinstance(e, str)]
    return []


@pytest.mark.parametrize("corpus", [c for c in (BIG, SECOND) if c])
def test_snapshot_where_matches_an_independent_recomputation(snap, corpus):
    nodes = snap._gate(corpus)[0].nodes
    keys = {}
    for nd in nodes.values():
        for k in (nd.get("properties") or {}):
            keys[k] = keys.get(k, 0) + 1
    rng = random.Random(7)
    pool = sorted(keys, key=lambda k: (-keys[k], k))[:25]
    n = 0
    for k in pool:
        observed = sorted({s for nd in nodes.values() for s in _vals((nd.get("properties") or {}).get(k))})
        if not observed:
            continue
        v1 = rng.choice(observed)
        vs = sorted(set(rng.sample(observed, min(3, len(observed)))))
        P = lambda nd: nd.get("properties") or {}
        cases = {
            "has": ({"key": k, "has": True}, lambda nd: k in P(nd)),
            "lacks": ({"key": k, "lacks": True}, lambda nd: k not in P(nd)),
            "equals": ({"key": k, "equals": v1}, lambda nd: v1 in _vals(P(nd).get(k))),
            "one_of": ({"key": k, "one_of": vs}, lambda nd: any(x in vs for x in _vals(P(nd).get(k)))),
            "not_equals": ({"key": k, "not_equals": v1},
                           lambda nd: k in P(nd) and any(x != v1 for x in _vals(P(nd).get(k)))),
            "not_one_of": ({"key": k, "not_one_of": vs},
                           lambda nd: k in P(nd) and any(x not in vs for x in _vals(P(nd).get(k)))),
        }
        for name, (cond, pred) in cases.items():
            exp = sorted(nid for nid, nd in nodes.items() if pred(nd))
            assert _ids_of(snap, corpus, where=[cond]) == exp, (corpus, k, name)
            n += 1
    assert n >= 100


def test_snapshot_fallback_set_is_the_independent_word_and(snap, monkeypatch):
    monkeypatch.setenv("LTS_FIND_WORD_FALLBACK", "on")
    from factlattice_engine.semantic.lexrank import STOP
    corpus = BIG
    nodes = snap._gate(corpus)[0].nodes
    wordsets = {nid: {x for f in _fields(nid, nd) for x in _w(f)} for nid, nd in nodes.items()}
    rng = random.Random(3)
    titles = [nd.get("title") or "" for nd in nodes.values() if len(_w(nd.get("title") or "")) >= 3]
    seen_nonzero = 0
    for _ in range(25):
        ws = [w for w in _w(rng.choice(titles)) if w not in STOP]
        if len(ws) < 3:
            continue
        q = f"{ws[0]} {ws[-1]} {ws[len(ws) // 2]}"            # scattered words: usually NOT a phrase
        r = snap.find(corpus=corpus, text=q, semantic="off", tier="digest", limit=1)
        if r["total_matched"] != 0 or "matched_all_words" not in r:
            continue
        words = []
        for w in _w(q):
            if w not in STOP and w not in words:
                words.append(w)
        if len(words) < 2:
            continue
        exp = sorted(nid for nid, ws in wordsets.items() if set(words) <= ws)
        blk = r["matched_all_words"]
        assert blk["total"] == len(exp), q
        assert sorted(n["id"] for n in blk["nodes"]) == sorted(exp[:20]) or blk["capped"], q
        seen_nonzero += blk["total"] > 0
    assert seen_nonzero >= 3


# ------------------------------------------------------------------ axis 1 (item 10)
def _p95(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]


def _time(eng, n=30, **kw):
    ts = []
    for _ in range(n):
        t = time.perf_counter()
        r = eng.find(corpus=BIG, semantic="off", tier="digest", limit=5, **kw)
        ts.append((time.perf_counter() - t) * 1000)
        assert r["success"], r
    return ts


@pytest.mark.parametrize("kw", [
    dict(all_of=[WORD]),
    dict(all_of=[WORD], match="word"),
    dict(any_of=["zero"], match="prefix"),
    dict(all_of=["zero", "map"], none_of=["note"]),
    dict(where=[{"key": "status", "equals": "operational"}]),
    dict(prefix="DEC-", where=[{"key": "tier", "lacks": True}]),
], ids=["phrase", "word", "prefix", "multi", "where_eq", "where_lacks"])
def test_axis1_new_calls_are_no_slower_than_the_old_text_path(snap, kw):
    """[SPEC §8] axis-1, LOAD-INDEPENDENT form. The absolute bound (warm p95 <= 50 ms on 9,661 nodes)
    is a wall-clock number and is measured by a benchmark kept outside the published tree (pooled p95 <= 40.7 ms on a quiet
    machine; see the build report). A 50 ms assert in a 2,000-test suite on a shared laptop failed
    at load average 17 for reasons unrelated to the code, so the suite asserts the RATIO against the
    unchanged `text` path measured in the same window: a cached term/where call must not cost more
    than today's uncached substring scan (about 36 ms quiet; new calls run 8-25 ms)."""
    import gc
    _time(snap, n=1, **kw)                                  # cold: builds the cache
    gc.collect()
    gc.disable()
    try:
        ratios = []
        for _ in range(3):
            new = statistics.median(_time(snap, **kw))
            old = statistics.median(_time(snap, text=WORD))
            ratios.append(new / old)
    finally:
        gc.enable()
    assert statistics.median(ratios) <= 1.0, f"new/old median ratios: {[round(r, 2) for r in ratios]}"
