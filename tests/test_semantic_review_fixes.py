"""Fixes for the 2026-10-02 reviews: audit-SEMANTIC-QUERY-S1-S4 (F4 F5 F6 F7 F10) and
audit-S5-RANK-RELEVANCE (F1 F2 F5 P3). Each test names its finding."""
from __future__ import annotations

import json
import threading

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")

from factlattice_engine import config, server  # noqa: E402
from factlattice_engine.semantic import embedder as E  # noqa: E402
from factlattice_engine.semantic import index as I  # noqa: E402
from factlattice_engine.semantic import suggest as S  # noqa: E402
from factlattice_engine.tools import LtsEngine  # noqa: E402
from tests.semantic_helpers import _tiny_model  # noqa: E402
from tests.test_semantic_find import _corpus  # noqa: E402


@pytest.fixture()
def w(tmp_path):
    _corpus(tmp_path / "corpus")
    pin = _tiny_model(str(tmp_path / "models"))
    reg = {"c": {"roots": [{"path": str(tmp_path / "corpus"), "area": "specs"}], "domain_suffix": "proj.c"}}
    e = LtsEngine(config.build_store(reg))
    e.semantic = S.SemanticService(state_root=str(tmp_path / "state"), model_root=str(tmp_path / "models"),
                                   pin=pin, build_mode="sync")
    return tmp_path, e, pin, reg


# --- S1-S4 F4: lts_semantic_find isolates a service fault into a status ----------------------
def test_F4_semantic_find_isolates_service_faults(w, monkeypatch):
    _t, e, *_ = w
    monkeypatch.setattr(e.semantic, "suggest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = e.semantic_find(corpus="c", text="tile map")
    assert r["success"] is True and r["status"] == "unavailable" and r["reason"] == "semantic-service-fault" and "boom" in r["live_detail"]
    assert r["candidates"] == [] and r["is_assertion"] is False


# --- S1-S4 F5: concurrent writers on one sidecar never raise and leave a valid index ----------
def test_F5_concurrent_refreshes_are_serialised(w):
    tmp, e, pin, reg = w
    part = e.store.get_partition("c")
    emb = E.load_embedder(pin, str(tmp / "models"))
    errors = []

    def go():
        try:
            I.SemanticIndex(str(tmp / "conc"), "c", emb).refresh(part)
        except Exception as ex:  # noqa: BLE001
            errors.append(repr(ex))
    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert errors == []
    I.SemanticIndex(str(tmp / "conc"), "c", emb)._load()          # valid, not corrupt


# --- S1-S4 F6: a failed background build is reported, not 'building' forever -----------------
def test_F6_failed_background_build_is_reported(w, monkeypatch):
    tmp, e, pin, _reg = w
    svc = S.SemanticService(state_root=str(tmp / "s6"), model_root=str(tmp / "models"), pin=pin,
                            build_mode="background", retry_after_s=3600)
    e.semantic = svc
    monkeypatch.setattr(I.SemanticIndex, "refresh", lambda self, part: (_ for _ in ()).throw(OSError("disk full")))
    assert e.semantic_find(corpus="c", text="tile map")["status"] == "index_building"
    assert svc.wait_idle(30)
    r = e.semantic_find(corpus="c", text="tile map")
    assert r["status"] == "index_failed" and r["reason"] == "index-build-failed" and "disk full" in r["live_detail"]


def test_F6_a_second_corpus_queued_during_a_build_is_not_stranded(w):
    tmp, e, pin, reg = w
    reg2 = {**reg, "d": {"roots": [{"path": str(tmp / "corpus"), "area": "specs"}], "domain_suffix": "proj.d"}}
    e2 = LtsEngine(config.build_store(reg2))
    svc = S.SemanticService(state_root=str(tmp / "s7"), model_root=str(tmp / "models"), pin=pin,
                            build_mode="background")
    e2.semantic = svc
    e2.semantic_find(corpus="c", text="tile map")
    e2.semantic_find(corpus="d", text="tile map")
    assert svc.wait_idle(60)
    assert e2.semantic_find(corpus="c", text="tile map")["status"] == "ok"
    assert e2.semantic_find(corpus="d", text="tile map")["status"] == "ok"


# --- S1-S4 F7: ratified rule 2: score + model + index version on the block/candidates ---------
def test_F7_candidates_carry_scores_and_block_carries_index_version(w):
    _t, e, *_ = w
    r = e.semantic_find(corpus="c", text="tile map version")
    assert r["index_version"] == I.index_version(r["model"], e.semantic._get_embedder())
    for c in r["candidates"]:
        assert set(c["scores"]) == {"fused", "embedding", "bm25"}
        assert isinstance(c["scores"]["fused"], float)


# --- S1-S4 F10: on_miss suggestions honour EVERY filter of the exact query --------------------
def test_F10_on_miss_respects_all_exact_filters(w):
    _t, e, *_ = w
    r = e.find(corpus="c", text="which extractor made this thing", semantic="on_miss", domain="no.such.domain")
    assert r["count"] == 0
    assert r["did_you_mean_semantic"]["candidates"] == []      # nothing passes the domain filter


# --- S5 F2: the pre-registered composite itself is exercised ----------------------------------
def test_S5_F2_composite_relevance_plus_on_miss(w):
    _t, e, *_ = w
    hit = e.find(corpus="c", text="tile map", rank="relevance", semantic="on_miss")
    assert hit["count"] > 0 and "did_you_mean_semantic" not in hit and hit["rank_applied"]["by"] == "bm25"
    miss = e.find(corpus="c", text="which extractor made this thing", rank="relevance", semantic="on_miss")
    assert miss["count"] == 0 and miss["did_you_mean_semantic"]["status"] == "ok"


# --- S5 F5: the BM25 cache is capped -----------------------------------------------------------
def test_S5_F5_lexrank_cache_is_capped(w, monkeypatch):
    _t, e, *_ = w
    import factlattice_engine.tools as T
    monkeypatch.setattr(T, "LEXRANK_CACHE_MAX", 1)
    e.find(corpus="c", text="tile map", rank="relevance")
    assert len(e._lexrank_cache) <= 1


# --- S5 P3: whitespace-only text is refused for relevance -------------------------------------
def test_S5_P3_whitespace_text_is_refused(w):
    _t, e, *_ = w
    r = e.find(corpus="c", text="   ", rank="relevance")
    assert r["success"] is False and "text" in r["error"]


# --- S5 F1 (P1) / DS guard: the description must not recommend on_miss before its gate --------
def test_S5_F1_description_does_not_advertise_ungated_on_miss():
    t = [x for x in server.TOOLS if x["name"] == "lts_find"][0]
    lead = t["description"][:400]
    assert "rank='relevance'" in lead
    # The maintainer ruling (2026-10-03): on_miss is turned on by the deployment's env, so
    # the lead names the env as the default's source and never claims a fixed default either way.
    assert "not yet enabled" not in t["description"].lower()
    assert "LTS_SEMANTIC_ON_MISS" in lead
    sem = t["inputSchema"]["properties"]["semantic"]["description"]
    assert "not yet enabled" not in sem.lower() and "LTS_SEMANTIC_ON_MISS" in sem


# --- UX (real-corpus smoke 2026-10-02): evidence must not repeat the id + title ---------------
def test_evidence_does_not_repeat_the_id_and_title(w):
    _t, e, *_ = w
    r = e.semantic_find(corpus="c", text="tile map version")
    for c in r["candidates"]:
        assert not c["evidence"].lstrip("…").startswith(c["id"].replace("-", " ")), c["evidence"]
        assert not c["evidence"].lstrip("…").startswith(c["title"] or "\0"), c["evidence"]


# ===== delta review (audit-SEMANTIC-FIXES-DELTA-2026-10-02) =================================
def _all_text(tool):
    return tool["description"] + " " + json.dumps(tool["inputSchema"])


def test_FD1_no_part_of_lts_find_recommends_ungated_on_miss():
    """P1: the WHOLE tool text, every parameter included. The CALL form `semantic='on_miss'` is a
    recommendation to use it; until the gate passes it must appear nowhere. (A first version of
    this test looked for 'not yet enabled' within +-300 chars and passed falsely on a neighbour.)"""
    t = [x for x in server.TOOLS if x["name"] == "lts_find"][0]
    txt = _all_text(t)
    for form in ("semantic='on_miss'", 'semantic="on_miss"', "semantic=\\'on_miss\\'"):
        assert form not in txt, form


def test_FD2_a_bad_model_name_is_a_status_not_an_error(w, monkeypatch):
    _t, e, *_ = w
    e.semantic = None
    monkeypatch.setenv("LTS_SEMANTIC_MODEL", "no-such-model")
    r = e.semantic_find(corpus="c", text="tile map")
    assert r["success"] is True and r["status"] == "unavailable"


def test_FD3_failed_refresh_of_a_stale_index_is_reported_and_cooled_down(w, monkeypatch):
    tmp, e, pin, reg = w
    svc = S.SemanticService(state_root=str(tmp / "sx"), model_root=str(tmp / "models"), pin=pin,
                            build_mode="sync", sync_topup_limit=0, retry_after_s=3600)
    e.semantic = svc
    assert e.semantic_find(corpus="c", text="tile map")["status"] == "ok"      # index built
    p = tmp / "corpus" / "00-c.lts"
    p.write_text(p.read_text().replace("hello world prose", "hello world prose, edited"), encoding="utf-8")
    e2 = LtsEngine(config.build_store(reg)); e2.semantic = svc
    calls = {"n": 0}

    def boom(self, part):
        calls["n"] += 1
        raise OSError("disk full")
    monkeypatch.setattr(I.SemanticIndex, "refresh", boom)
    svc.build_mode = "background"
    e2.semantic_find(corpus="c", text="hello"); svc.wait_idle(30)
    r = e2.semantic_find(corpus="c", text="hello"); svc.wait_idle(30)
    e2.semantic_find(corpus="c", text="hello"); svc.wait_idle(30)
    assert r["status"] == "ok" and r["index_fresh"] is False      # still serves the stale index
    assert "disk full" in r.get("refresh_failed", "")
    assert calls["n"] == 1                                         # cooled down, not retried per query


def test_FD4_evidence_never_shows_the_property_key_line(tmp_path):
    root = tmp_path / "c"; root.mkdir()
    (root / "00.lts").write_text("# D [DOC]\n\n## Only props [P-1]\n\n- status: active\n- what: tile map\n",
                                 encoding="utf-8")
    from factlattice_engine.semantic.cards import node_cards
    from factlattice_engine.semantic.suggest import _evidence
    nd = {"id": "P-1", "title": "Only props", "properties": {"status": "active", "what": "tile map"}}
    ev = _evidence(node_cards("P-1", nd, []), ["zero"])
    assert "tile map" in ev and "status" not in ev and "what" not in ev.replace("tile map", "")


def test_FD5_on_miss_filter_actually_excludes_a_nonempty_candidate_set(w):
    _t, e, *_ = w
    allc = e.find(corpus="c", text="which extractor made this thing", semantic="on_miss")
    ids = {c["id"] for c in allc["did_you_mean_semantic"]["candidates"]}
    assert ids                                                     # non-empty without the filter
    r = e.find(corpus="c", text="which extractor made this thing", semantic="on_miss", prefix="MAP")
    got = {c["id"] for c in r["did_you_mean_semantic"]["candidates"]}
    assert got and all(i.startswith("MAP") for i in got) and got < ids | got


def test_FD7_a_thread_start_failure_does_not_wedge_the_worker(w, monkeypatch):
    tmp, e, pin, _reg = w
    svc = S.SemanticService(state_root=str(tmp / "sw"), model_root=str(tmp / "models"), pin=pin,
                            build_mode="background")
    e.semantic = svc
    real = threading.Thread.start
    monkeypatch.setattr(threading.Thread, "start", lambda self: (_ for _ in ()).throw(RuntimeError("no threads")))
    r = e.semantic_find(corpus="c", text="tile map")
    assert r["success"] is True
    monkeypatch.setattr(threading.Thread, "start", real)
    e.semantic_find(corpus="c", text="tile map")
    assert svc.wait_idle(30)
    assert e.semantic_find(corpus="c", text="tile map")["status"] == "ok"


def test_FD11_retry_message_never_says_zero_seconds(w, monkeypatch):
    tmp, e, pin, _reg = w
    svc = S.SemanticService(state_root=str(tmp / "s11"), model_root=str(tmp / "models"), pin=pin,
                            build_mode="background", retry_after_s=0.0001)
    e.semantic = svc
    monkeypatch.setattr(I.SemanticIndex, "refresh", lambda self, part: (_ for _ in ()).throw(OSError("x")))
    e.semantic_find(corpus="c", text="tile map"); svc.wait_idle(30)
    svc.retry_after_s = 3600
    r = e.semantic_find(corpus="c", text="tile map")
    assert "retry in 0 s" not in r.get("live_detail", "")


def test_FG1_service_caches_are_bounded(w):
    """Composition review F-G1: SemanticService._corpus and _indexes must be LRU-bounded."""
    tmp, e, pin, reg = w
    svc = S.SemanticService(state_root=str(tmp / "sb"), model_root=str(tmp / "models"), pin=pin,
                            build_mode="sync", cache_max=1)
    reg2 = {**reg, "d": {"roots": [{"path": str(tmp / "corpus"), "area": "specs"}], "domain_suffix": "proj.d"}}
    e2 = LtsEngine(config.build_store(reg2)); e2.semantic = svc
    e2.semantic_find(corpus="c", text="tile map"); e2.semantic_find(corpus="d", text="tile map")
    assert len(svc._corpus) <= 1 and len(svc._indexes) <= 1


def test_every_package_in_the_tree_is_in_pyproject():
    """Composition review: a wheel built from the tree lacked factlattice_engine.render and
    factlattice.authoring (pre-existing on main)."""
    import os, re
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    declared = set(re.findall(r'"([a-z_.]+)"', open(os.path.join(repo, "pyproject.toml")).read().split("packages = [")[1].split("]")[0]))
    found = set()
    for top in ("factlattice", "factlattice_engine"):
        for dp, dn, fn in os.walk(os.path.join(repo, top)):
            if "__init__.py" in fn and "__pycache__" not in dp:
                found.add(os.path.relpath(dp, repo).replace(os.sep, "."))
    assert not (found - declared), sorted(found - declared)
