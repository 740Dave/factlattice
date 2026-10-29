"""[FIND-QUERY-SURFACE-6] byte ceiling on the query-surface FieldCaches (the architect, the architect 2026-10-05).

Spec: each FieldCache keeps an incremental ESTIMATE of its retained bytes; FieldCaches enforces a total
ceiling (env LTS_FIND_CACHE_MAX_MB, ASCII digits, default 150, 0 = cache nothing) by LRU eviction; a single
corpus over the ceiling is used for its call and not retained; results are identical for any ceiling;
`FieldCaches.stats()` exposes entries/estimated_bytes/ceiling_bytes/evictions/oversize_skips.
"""
from __future__ import annotations

import gc
import json
import os
import tracemalloc
from pathlib import Path

import pytest

from factlattice_engine import config, query_surface as qs, tools
from tests import qs_fixture as F

ENV = "LTS_FIND_CACHE_MAX_MB"
SNAP_CFG = Path.home() / ".lts-engine" / "v3-snapshot-20261003T203844" / "corpus-config.json"
# Machine-local, deployment-specific: the snapshot corpora and a query word present in the big one are named by the
# environment, never in this file. Unset => these real-corpus checks are SKIPPED (and say so).
BIG = os.environ.get("FACTLATTICE_SNAP_BIG_CORPUS", "")
SECOND = os.environ.get("FACTLATTICE_SNAP_SECOND_CORPUS", "")
WORD = os.environ.get("FACTLATTICE_SNAP_WORD", "")
needs_snap = pytest.mark.skipif(not SNAP_CFG.exists() or not (BIG and WORD), reason=f"snapshot corpus config missing or FACTLATTICE_SNAP_BIG_CORPUS/WORD unset: {SNAP_CFG}")
MB = 1024 * 1024


class FakePart:
    def __init__(self, cid, n_nodes, size, key=1):
        self.corpus_id = cid
        self.key = key
        self.nodes = {f"{cid.upper()}-{i}": {"title": f"t{i}", "properties": {"k": "x" * size}}
                      for i in range(n_nodes)}

    def _freshness_key(self):
        return self.key


def touch(fc, part):
    for nid, nd in part.nodes.items():
        fc.entry(nd, nid).words()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    qs._WARNED_CEILINGS.clear() if hasattr(qs, "_WARNED_CEILINGS") else None


# ------------------------------------------------------------------ estimate + stats
def test_stats_shape_and_default_ceiling():
    s = qs.FieldCaches().stats()
    assert set(s) == {"entries", "estimated_bytes", "ceiling_bytes", "evictions", "oversize_skips"}
    assert s["ceiling_bytes"] == 150 * MB


def test_estimate_grows_incrementally_with_entries():
    caches = qs.FieldCaches()
    p = FakePart("a", 3, 1000)
    fc = caches.for_part(p)
    assert fc.est_bytes == 0
    fc.entry(p.nodes["A-0"], "A-0")
    one = fc.est_bytes
    assert one >= 1000
    touch(fc, p)
    assert fc.est_bytes >= 3 * 2000          # lows + words of 3 nodes with a 1000-char value each
    assert caches.stats()["estimated_bytes"] == fc.est_bytes


# ------------------------------------------------------------------ env
@pytest.mark.parametrize("val,expect", [("1", 1 * MB), ("0", 0), ("150", 150 * MB), ("007", 7 * MB)])
def test_env_sets_ceiling(monkeypatch, val, expect):
    monkeypatch.setenv(ENV, val)
    assert qs.FieldCaches().stats()["ceiling_bytes"] == expect


def test_env_read_per_call(monkeypatch):
    c = qs.FieldCaches()
    monkeypatch.setenv(ENV, "3")
    assert c.stats()["ceiling_bytes"] == 3 * MB
    monkeypatch.setenv(ENV, "4")
    assert c.stats()["ceiling_bytes"] == 4 * MB


@pytest.mark.parametrize("bad", ["abc", "-1", "1.5", "", " 5", "١٢", "1e3", "5 "])
def test_bad_env_uses_default_and_warns_once_per_value(monkeypatch, capsys, bad):
    monkeypatch.setenv(ENV, bad)
    c = qs.FieldCaches()
    assert c.stats()["ceiling_bytes"] == 150 * MB
    assert c.stats()["ceiling_bytes"] == 150 * MB
    err = capsys.readouterr().err
    assert err.count(ENV) == 1, err
    monkeypatch.setenv(ENV, bad + "z")
    c.stats()
    assert capsys.readouterr().err.count(ENV) == 1       # a different bad value warns again


# ------------------------------------------------------------------ eviction
def test_lru_eviction_to_ceiling_keeps_most_recent(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    caches = qs.FieldCaches()
    for cid in ("a", "b", "c"):
        p = FakePart(cid, 1, 200_000)
        touch(caches.for_part(p), p)
    s = caches.stats()
    assert s["estimated_bytes"] <= MB
    assert s["evictions"] >= 1
    assert "a" not in caches._c and "c" in caches._c


def test_recency_protects_a_reused_corpus(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    caches = qs.FieldCaches()
    pa, pb, pc = (FakePart(c, 1, 200_000) for c in "abc")
    touch(caches.for_part(pa), pa)
    touch(caches.for_part(pb), pb)
    caches.for_part(pa)                                   # a is now the most recent
    touch(caches.for_part(pc), pc)
    assert "a" in caches._c and "b" not in caches._c


def test_count_bound_still_applies(monkeypatch):
    monkeypatch.setenv(ENV, "150")
    caches = qs.FieldCaches()
    for i in range(qs.CACHE_MAX + 3):
        p = FakePart(f"c{i}", 1, 10)
        touch(caches.for_part(p), p)
    assert caches.stats()["entries"] == qs.CACHE_MAX


def test_oversize_single_corpus_is_used_but_not_retained(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    caches = qs.FieldCaches()
    p = FakePart("big", 4, 200_000)
    fc = caches.for_part(p)
    touch(fc, p)                                          # the call still works on the cache it holds
    assert fc.entry(p.nodes["BIG-0"], "BIG-0").words()
    s = caches.stats()
    assert s["entries"] == 0 and s["estimated_bytes"] == 0 and s["oversize_skips"] == 1
    # next call rebuilds from nothing (and again is not retained)
    fc2 = caches.for_part(p)
    assert fc2 is not fc and fc2.est_bytes == 0


def test_oversize_does_not_evict_the_small_neighbours_needlessly(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    caches = qs.FieldCaches()
    small = FakePart("s", 1, 1000)
    touch(caches.for_part(small), small)
    big = FakePart("big", 4, 200_000)
    touch(caches.for_part(big), big)
    assert "s" in caches._c and "big" not in caches._c


def test_zero_ceiling_caches_nothing(monkeypatch):
    monkeypatch.setenv(ENV, "0")
    caches = qs.FieldCaches()
    p = FakePart("a", 2, 100)
    fc = caches.for_part(p)
    touch(fc, p)
    s = caches.stats()
    assert s["entries"] == 0 and s["estimated_bytes"] == 0 and s["ceiling_bytes"] == 0
    assert caches.for_part(p) is not fc


def test_freshness_key_change_replaces_entry_and_total(monkeypatch):
    caches = qs.FieldCaches()
    p = FakePart("a", 2, 5000)
    touch(caches.for_part(p), p)
    before = caches.stats()["estimated_bytes"]
    p.key = 2
    fc = caches.for_part(p)
    assert caches.stats()["estimated_bytes"] == 0 and fc.est_bytes == 0
    touch(fc, p)
    assert caches.stats()["estimated_bytes"] == before


def test_vocab_is_counted(monkeypatch):
    caches = qs.FieldCaches()
    p = FakePart("a", 50, 10)
    p.nodes = {f"A-{i}": {"title": f"word{i} other{i}", "properties": {}} for i in range(500)}
    fc = caches.for_part(p)
    touch(fc, p)
    before = fc.est_bytes
    fc.vocab(p.nodes)
    assert fc.est_bytes > before and caches.stats()["estimated_bytes"] == fc.est_bytes


# ------------------------------------------------------------------ parity at any ceiling
CALLS = [
    dict(all_of=["lantern"], match="word"),
    dict(all_of=["go", "live"], match="word"),
    dict(any_of=["gno", "qa"], match="prefix"),
    dict(all_of=["lantern qa"], match="phrase"),
    dict(all_of=["alias"], none_of=["second"], match="word"),
    dict(where=[{"key": "status", "equals": "operational"}]),
    dict(where=[{"key": "tier", "lacks": True}], any_of=["go"], match="prefix"),
    dict(text="borrower card contents"),
    dict(all_of=["zorblax"], match="word"),
]


@pytest.mark.parametrize("ceil", ["0", "1", "150", None])
def test_results_identical_at_every_ceiling(tmp_path, monkeypatch, ceil):
    outs = {}
    for label, val in (("ref", None), ("var", ceil)):
        if val is None:
            monkeypatch.delenv(ENV, raising=False)
        else:
            monkeypatch.setenv(ENV, val)
        monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / f"st-{label}"))
        d = tmp_path / label
        d.mkdir()
        eng, root = F.engine_a(d)
        res = []
        for _ in range(2):          # twice: the second pass hits a warm cache where one is retained
            for kw in CALLS:
                res.append(F.norm(eng.find(corpus="qs", semantic="off", **kw), root))
        outs[label] = json.dumps(res, sort_keys=True)
    assert outs["ref"] == outs["var"]


def test_tool_path_honours_zero_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "st"))
    monkeypatch.setenv(ENV, "0")
    eng, root = F.engine_a(tmp_path)
    r = eng.find(corpus="qs", semantic="off", all_of=["lantern"], match="word")
    assert r["success"] and r["total_matched"] >= 1
    assert eng._qs_caches.stats()["entries"] == 0


# ------------------------------------------------------------------ real snapshot: calibration + oversize
def _engine_snap(corpora=None):
    cfg = json.loads(SNAP_CFG.read_text())
    if corpora:
        cfg = {k: cfg[k] for k in corpora}
    return tools.LtsEngine(config.build_store(cfg))


@needs_snap
def test_estimate_is_calibrated_against_tracemalloc_on_a_real_corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "st"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("LTS_ENGINE_STATE_DIR", str(tmp_path / "st2"))
    eng = _engine_snap([BIG])
    eng.find(corpus=BIG, prefix="ZZ-NONE-", semantic="off", tier="digest", limit=1)
    gc.collect()
    tracemalloc.start()
    base, _ = tracemalloc.get_traced_memory()
    eng.find(corpus=BIG, all_of=[WORD], match="word", semantic="off", tier="digest", limit=5)
    eng.find(corpus=BIG, any_of=["zero"], match="prefix", semantic="off", tier="digest", limit=5)
    gc.collect()
    now, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    measured = now - base
    est = eng._qs_caches.stats()["estimated_bytes"]
    ratio = est / measured
    print(f"CALIBRATION estimate={est} measured={measured} ratio={ratio:.3f}")
    assert measured > 10 * MB, measured
    assert 0.65 <= ratio <= 1.35, f"estimate/measured = {ratio:.3f}"


@needs_snap
def test_snapshot_oversize_corpus_not_retained_but_answers_match(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "st"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("LTS_ENGINE_STATE_DIR", str(tmp_path / "st2"))
    eng = _engine_snap([BIG])
    kw = dict(corpus=BIG, all_of=[WORD], match="word", semantic="off", tier="digest", limit=5)
    monkeypatch.setenv(ENV, "150")
    ref = eng.find(**kw)
    assert eng._qs_caches.stats()["entries"] == 1
    monkeypatch.setenv(ENV, "1")
    eng2 = _engine_snap([BIG])
    got = eng2.find(**kw)
    s = eng2._qs_caches.stats()
    assert s["entries"] == 0 and s["estimated_bytes"] == 0 and s["oversize_skips"] >= 1
    assert F._scrub_timing(got) == F._scrub_timing(ref)


@needs_snap
def test_snapshot_total_stays_under_a_small_ceiling_across_corpora(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "st"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("LTS_ENGINE_STATE_DIR", str(tmp_path / "st2"))
    monkeypatch.setenv(ENV, "20")
    eng = _engine_snap()
    for c in sorted(json.loads(SNAP_CFG.read_text())):
        r = eng.find(corpus=c, all_of=["zero"], match="word", semantic="off", tier="digest", limit=1)
        assert r["success"], (c, r)
        assert eng._qs_caches.stats()["estimated_bytes"] <= 20 * MB
    assert eng._qs_caches.stats()["evictions"] + eng._qs_caches.stats()["oversize_skips"] >= 1


# ------------------------------------------------------------------ SDLC rule
def test_sdlc_rule_states_the_byte_ceiling():
    txt = (Path(__file__).parent.parent / "SDLC.md").read_text(encoding="utf-8")
    line = next(l for l in txt.splitlines() if "[FIND-QUERY-SURFACE-6]" in l)
    assert ENV in line and "150" in line and "byte" in line.lower()
    assert "NOT byte-bounded" not in line


# ------------------------------------------------------------------ no reference cycles (found by the footprint measurement)
def test_evicted_cache_is_freed_by_refcount_not_left_for_the_cyclic_gc(monkeypatch):
    """An evicted FieldCache must die immediately: a cycle (entry -> cache -> entries -> entry) would
    keep evicted corpora alive until the cyclic GC runs, inflating the process footprint."""
    import weakref
    monkeypatch.setenv(ENV, "1")
    caches = qs.FieldCaches()
    pa = FakePart("a", 1, 300_000)
    fa = caches.for_part(pa)
    touch(fa, pa)
    ref = weakref.ref(fa)
    del fa
    pb = FakePart("b", 1, 300_000)
    gc.disable()
    try:
        touch(caches.for_part(pb), pb)           # evicts a
        assert "a" not in caches._c
        assert ref() is None, "evicted FieldCache is still alive (reference cycle)"
    finally:
        gc.enable()


# ------------------------------------------------------------------ review remediation: F1 (trim in for_part), F5 (clamp + SPEC)
def _warm_three(caches, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    parts = [FakePart(c, 1, 300_000) for c in "abc"]
    for p in parts:
        touch(caches.for_part(p), p)
    assert caches.stats()["entries"] == 3 and caches.stats()["estimated_bytes"] > 1 * MB
    return parts


@pytest.mark.parametrize("val,limit", [("1", MB), ("0", 0)])
def test_lowering_the_ceiling_on_a_warm_cache_trims_on_the_next_call(monkeypatch, val, limit):
    caches = qs.FieldCaches()
    parts = _warm_three(caches, monkeypatch)
    monkeypatch.setenv(ENV, val)
    caches.for_part(parts[2])                  # one call, no growth
    s = caches.stats()
    assert s["estimated_bytes"] <= limit, s
    if limit == 0:
        assert s["entries"] == 0 and s["estimated_bytes"] == 0


def test_trim_keeps_the_most_recent_corpus_when_it_fits(monkeypatch):
    caches = qs.FieldCaches()
    parts = _warm_three(caches, monkeypatch)
    monkeypatch.setenv(ENV, "1")               # one 600 KB corpus fits, two do not
    caches.for_part(parts[1])
    assert list(caches._c) == ["b"] and caches.stats()["evictions"] >= 2


def test_sum_of_cache_estimates_equals_total_after_trim(monkeypatch):
    caches = qs.FieldCaches()
    parts = _warm_three(caches, monkeypatch)
    monkeypatch.setenv(ENV, "1")
    caches.for_part(parts[0])
    assert sum(c.est_bytes for c in caches._c.values()) == caches.stats()["estimated_bytes"]


@pytest.mark.parametrize("val", ["4097", "99999999999999999999"])
def test_env_above_the_upper_bound_clamps_to_4096_and_warns_once(monkeypatch, capsys, val):
    monkeypatch.setenv(ENV, val)
    c = qs.FieldCaches()
    assert c.stats()["ceiling_bytes"] == 4096 * MB
    assert c.stats()["ceiling_bytes"] == 4096 * MB
    assert capsys.readouterr().err.count(ENV) == 1


def test_env_at_the_upper_bound_is_accepted_silently(monkeypatch, capsys):
    monkeypatch.setenv(ENV, "4096")
    assert qs.FieldCaches().stats()["ceiling_bytes"] == 4096 * MB
    assert capsys.readouterr().err == ""


def test_spec_documents_the_env_var_in_the_query_surface_section():
    txt = (Path(__file__).parent.parent / "SPEC.md").read_text(encoding="utf-8")
    sec = txt.split("#### 4.6.5", 1)[1].split("\n#### ", 1)[0]
    assert ENV in sec and "150" in sec and "4096" in sec and "17" in sec
