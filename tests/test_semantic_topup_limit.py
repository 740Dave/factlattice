"""[SEM-TOPUP-LIMIT] The synchronous query-time top-up is configurable, and its cost is logged.

Measured 2026-10-04/05 (the architect): an on-miss `lts_find` pays ~90 ms per node edited since the last
query, up to SYNC_TOPUP_LIMIT (25) nodes, ON the query path (live query log: zero-result finds with
suggestions p50 696 ms, p95 6.5 s, max 13.3 s, against 17 ms p50 for finds with matches). Past the
limit the stale index is served and a background refresh starts, so a lower limit trades a short
embedding-leg lag (disclosed as `nodes_unembedded_stale`) for latency.

  * LTS_SEMANTIC_SYNC_TOPUP_LIMIT=<int >= 0> sets the limit, read per call; unset = 25 (no change);
    a bad value warns once and uses 25. An explicit constructor value still wins (tests, CLI).
  * The query log's suggestion record carries `embedded_on_query` and `nodes_unembedded_stale`,
    so a slow on-miss find can be decomposed after the fact.
"""
from __future__ import annotations

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")

from factlattice_engine import config, querylog  # noqa: E402
from factlattice_engine.semantic import suggest as S  # noqa: E402
from factlattice_engine.tools import LtsEngine  # noqa: E402
from tests.semantic_helpers import _tiny_model  # noqa: E402


def _corpus(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "00-c.lts").write_text(
        "# Corpus [DOC-C]\n\n"
        "## Tile map version field [XL-LEDGER-PROTOBUF]\n\n- status: active\n"
        "- what: the tile map version field records which extractor made it\n\ntile map version prose\n\n"
        "## Hello world [N-HELLO]\n\n- what: hello world greeting\n\nhello world prose\n\n"
        "## Map overview [MAP-1]\n\n- what: map overview\n", encoding="utf-8")


@pytest.fixture()
def bg(tmp_path, monkeypatch):
    """A BACKGROUND-mode service, as the live engine runs (sync mode always tops up)."""
    monkeypatch.delenv("LTS_SEMANTIC_SYNC_TOPUP_LIMIT", raising=False)
    _corpus(tmp_path / "corpus")
    pin = _tiny_model(str(tmp_path / "models"))
    reg = {"c": {"roots": [{"path": str(tmp_path / "corpus"), "area": "specs"}], "domain_suffix": "proj.c"}}
    svc = S.SemanticService(state_root=str(tmp_path / "state"), model_root=str(tmp_path / "models"),
                            pin=pin, build_mode="background")
    eng = LtsEngine(config.build_store(reg))
    eng.semantic = svc
    eng.semantic_find(corpus="c", text="tile map")
    assert svc.wait_idle(30)
    assert eng.semantic_find(corpus="c", text="tile map")["status"] == "ok"
    return tmp_path, svc, reg


def _edit_one_and_query(tmp_path, svc, reg, text="hello world"):
    p = tmp_path / "corpus" / "00-c.lts"
    p.write_text(p.read_text().replace("hello world prose", "hello world prose, edited"), encoding="utf-8")
    eng2 = LtsEngine(config.build_store(reg))
    eng2.semantic = svc
    return eng2.semantic_find(corpus="c", text=text)


def test_default_still_tops_up_a_small_edit_on_the_query_path(bg):
    tmp, svc, reg = bg
    r = _edit_one_and_query(tmp, svc, reg)
    assert r["status"] == "ok" and r["index_fresh"] is True
    assert r["index"]["embedded_on_query"] == 1


def test_env_limit_zero_serves_stale_and_refreshes_in_background(bg, monkeypatch):
    tmp, svc, reg = bg
    monkeypatch.setenv("LTS_SEMANTIC_SYNC_TOPUP_LIMIT", "0")
    r = _edit_one_and_query(tmp, svc, reg)
    assert r["status"] == "ok"
    assert r["index_fresh"] is False and r["nodes_unembedded_stale"] == 1   # disclosed, not hidden
    assert r["index"]["embedded_on_query"] == 0                              # nothing embedded on the query path
    assert svc.wait_idle(30)                                                 # the background refresh ran
    eng3 = LtsEngine(config.build_store(reg)); eng3.semantic = svc
    r2 = eng3.semantic_find(corpus="c", text="hello world")
    assert r2["index_fresh"] is True and r2["index"]["embedded_on_query"] == 0


def test_env_limit_is_read_per_call(bg, monkeypatch):
    """One service, the env changed between two edits: the second edit follows the NEW value (review F4:
    the earlier version of this test also passed at the base, so it proved nothing)."""
    tmp, svc, reg = bg
    monkeypatch.setenv("LTS_SEMANTIC_SYNC_TOPUP_LIMIT", "0")
    r1 = _edit_one_and_query(tmp, svc, reg)
    assert r1["index"]["embedded_on_query"] == 0 and r1["nodes_unembedded_stale"] == 1
    assert svc.wait_idle(30)
    monkeypatch.setenv("LTS_SEMANTIC_SYNC_TOPUP_LIMIT", "5")
    p = tmp / "corpus" / "00-c.lts"
    p.write_text(p.read_text().replace("map overview\n", "map overview, edited\n"), encoding="utf-8")
    eng = LtsEngine(config.build_store(reg)); eng.semantic = svc
    r2 = eng.semantic_find(corpus="c", text="map overview")
    assert r2["index_fresh"] is True and r2["index"]["embedded_on_query"] == 1   # same service, new value


@pytest.mark.parametrize("raw", ["1_0", "+3", "\u0663", "2.5", "ten"])
def test_only_ascii_digits_are_accepted(raw, monkeypatch, capsys):
    monkeypatch.setenv("LTS_SEMANTIC_SYNC_TOPUP_LIMIT", raw)
    assert S.sync_topup_limit_from_env() == S.SYNC_TOPUP_LIMIT
    assert "LTS_SEMANTIC_SYNC_TOPUP_LIMIT" in capsys.readouterr().err


def test_bad_env_value_warns_once_and_uses_the_default(bg, monkeypatch, capsys):
    tmp, svc, reg = bg
    monkeypatch.setenv("LTS_SEMANTIC_SYNC_TOPUP_LIMIT", "-3")
    r = _edit_one_and_query(tmp, svc, reg)
    assert r["index"]["embedded_on_query"] == 1                              # default 25 applied
    err = capsys.readouterr().err
    assert "LTS_SEMANTIC_SYNC_TOPUP_LIMIT" in err and "-3" in err
    S.sync_topup_limit_from_env()
    assert "LTS_SEMANTIC_SYNC_TOPUP_LIMIT" not in capsys.readouterr().err    # once per value


def test_explicit_constructor_value_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LTS_SEMANTIC_SYNC_TOPUP_LIMIT", "0")
    svc = S.SemanticService(state_root=str(tmp_path / "s"), sync_topup_limit=7)
    assert svc.effective_sync_topup_limit() == 7


def test_querylog_suggestion_record_carries_the_topup_cost():
    blk = {"kind": "semantic-suggestions-not-matches", "status": "ok", "candidates": [],
           "embedded_on_query": 4, "nodes_unembedded_stale": 0}
    rec = querylog.suggestions({"did_you_mean_semantic": blk})
    assert rec["embedded_on_query"] == 4 and rec["nodes_unembedded_stale"] == 0
    stale = querylog.suggestions({"did_you_mean_semantic": {**blk, "embedded_on_query": None,
                                                             "nodes_unembedded_stale": 3}})
    assert stale["nodes_unembedded_stale"] == 3
    old = querylog.suggestions({"did_you_mean_semantic": {"status": "ok", "candidates": []}})
    assert "embedded_on_query" not in old                     # absent stays absent, never a fake 0
    building = querylog.suggestions({"did_you_mean_semantic": {**blk, "status": "index_building"}})
    assert "embedded_on_query" not in building and "nodes_unembedded_stale" not in building   # review F1
