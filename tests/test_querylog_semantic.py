"""[QUERYLOG-SEMANTIC-COVERAGE] semantic suggestions, prose search and reverse lookups are logged;
suggestions go to their own field, never into result_ids (a suggestion shown is not a read)."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")

from factlattice_engine import config, querylog  # noqa: E402
from factlattice_engine.semantic import suggest as S  # noqa: E402
from factlattice_engine.tools import LtsEngine, dispatch  # noqa: E402
from tests.semantic_helpers import _tiny_model  # noqa: E402
from tests.test_semantic_find import _corpus  # noqa: E402

MISS = "which extractor made this thing"


@pytest.fixture()
def w(tmp_path, monkeypatch):
    for v in ("LTS_SEMANTIC_ON_MISS", "LTS_SEMANTIC_HIGH_LABEL"):
        monkeypatch.delenv(v, raising=False)
    log = tmp_path / "q.jsonl"
    monkeypatch.setenv(querylog.ENV_LOG, str(log))
    _corpus(tmp_path / "corpus")
    pin = _tiny_model(str(tmp_path / "models"))
    reg = {"c": {"roots": [{"path": str(tmp_path / "corpus"), "area": "specs"}], "domain_suffix": "proj.c"}}
    e = LtsEngine(config.build_store(reg))
    e.semantic = S.SemanticService(state_root=str(tmp_path / "state"), model_root=str(tmp_path / "models"),
                                   pin=pin, build_mode="sync")
    return e, log


def _recs(log):
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def test_semantic_find_is_logged_with_suggestions_not_result_ids(w):
    e, log = w
    r = dispatch("lts_semantic_find", {"corpus": "c", "text": MISS}, tools=e)
    rec = _recs(log)[-1]
    assert rec["tool"] == "lts_semantic_find"
    served = [(c["id"], c["rank"], c["label"]) for c in r["candidates"]]
    assert [(c["id"], c["rank"], c["label"]) for c in rec["suggestions"]["candidates"]] == served
    assert rec["suggestions"]["status"] == "ok" and rec["suggestions"]["preface"] is True
    assert not set(rec["result_ids"]) & {c[0] for c in served}


def test_on_miss_block_is_recorded_and_exact_hits_are_unchanged(w, monkeypatch):
    e, log = w
    dispatch("lts_find", {"corpus": "c", "text": MISS, "semantic": "on_miss"}, tools=e)
    rec = _recs(log)[-1]
    assert rec["count"] == 0 and rec["suggestions"]["candidates"] and rec["suggestions"]["status"] == "ok"
    dispatch("lts_find", {"corpus": "c", "text": "tile map", "semantic": "on_miss"}, tools=e)
    assert "suggestions" not in _recs(log)[-1]


def test_label_policy_is_recorded(w, monkeypatch):
    e, log = w
    monkeypatch.setenv("LTS_SEMANTIC_HIGH_LABEL", "off")
    dispatch("lts_semantic_find", {"corpus": "c", "text": MISS}, tools=e)
    assert _recs(log)[-1]["suggestions"]["label_policy"] == "strong-match disabled by deployment"


def test_prose_and_reverse_are_logged(w):
    e, log = w
    dispatch("lts_find_in_prose", {"corpus": "c", "text": "hello world prose"}, tools=e)
    rec = _recs(log)[-1]
    assert rec["tool"] == "lts_find_in_prose" and "N-HELLO" in rec["result_ids"]
    dispatch("lts_reverse", {"corpus": "c", "node_id": "MAP-1"}, tools=e)
    rec = _recs(log)[-1]
    assert rec["tool"] == "lts_reverse" and "MAP-1" in rec["result_ids"]


def test_build_record_suggestions_cap_is_declared():
    cands = [{"id": f"N-{i}", "rank": i + 1, "label": "medium"} for i in range(querylog.MAX_RESULT_IDS + 3)]
    rec = querylog.build_record("lts_semantic_find", {"corpus": "c", "text": "x"},
                                {"kind": "semantic-suggestions-not-matches", "status": "ok", "candidates": cands}, 1.0)
    s = rec["suggestions"]
    assert len(s["candidates"]) == querylog.MAX_RESULT_IDS and s["capped"] is True


def test_failures_are_recorded_ok_false(w):
    e, log = w
    for name, args in (("lts_semantic_find", {"text": MISS}), ("lts_semantic_find", {"corpus": "c", "text": ""}),
                       ("lts_find_in_prose", {"corpus": "nope", "text": "x"})):
        dispatch(name, args, tools=e)
        rec = _recs(log)[-1]
        assert rec["tool"] == name and rec["ok"] is False


def test_a_malformed_block_keeps_the_record():
    rec = querylog.build_record("lts_semantic_find", {"corpus": "c"},
                                {"kind": "semantic-suggestions-not-matches", "status": "ok", "candidates": 5}, 1.0)
    assert rec["suggestions"]["candidates"] == [] and rec["tool"] == "lts_semantic_find"
