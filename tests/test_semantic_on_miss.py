"""[SEM-ON-MISS] S4: `lts_find(..., semantic="on_miss")`, suggestions on a lexical ZERO only.

Ratified rules ([DEC-SEMANTIC-ASSIST]; honesty rules R1-R5) and rulings (2026-10-02):
  * DEFAULT OFF (env LTS_SEMANTIC_ON_MISS unset): the response is byte-identical to today;
  * R1 never mixed: nodes / count / total_matched / truncated / complete are identical with the
    block on or off; the block lives under its own key `did_you_mean_semantic`;
  * R2 a zero stays a zero: scope_note says the zero covers exact matching and suggestions are
    not matches;
  * the block is computed ONLY on a zero (no embedding work when there are hits) and ONLY for a
    NAMED corpus (no fan-out); with corpus omitted it says so;
  * R5 a missing model stack makes the block `unavailable`; the exact result is untouched.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")

from factlattice_engine import config  # noqa: E402
from factlattice_engine.semantic import suggest as S  # noqa: E402
from factlattice_engine.tools import LtsEngine  # noqa: E402
from tests.semantic_helpers import _tiny_model  # noqa: E402
from tests.test_semantic_find import _corpus  # noqa: E402

EXACT_KEYS = ("nodes", "count", "total_matched", "truncated", "complete", "digest")


class CountingService(S.SemanticService):
    calls = 0

    def suggest(self, *a, **k):
        CountingService.calls += 1
        return super().suggest(*a, **k)


@pytest.fixture()
def eng(tmp_path, monkeypatch):
    monkeypatch.delenv("LTS_SEMANTIC_ON_MISS", raising=False)
    _corpus(tmp_path / "corpus")
    pin = _tiny_model(str(tmp_path / "models"))
    reg = {"c": {"roots": [{"path": str(tmp_path / "corpus"), "area": "specs"}], "domain_suffix": "proj.c"}}
    e = LtsEngine(config.build_store(reg))
    e.semantic = CountingService(state_root=str(tmp_path / "state"), model_root=str(tmp_path / "models"),
                                 pin=pin, build_mode="sync")
    CountingService.calls = 0
    return e


def _strip(r):
    r = json.loads(json.dumps(r, default=str))
    r.pop("did_you_mean_semantic", None)
    (r.get("extent") or {}).pop("scope_note", None)
    return r


def test_default_is_off_and_byte_identical(eng):
    a = eng.find(corpus="c", text="which extractor made this thing")
    b = eng.find(corpus="c", text="which extractor made this thing", semantic="off")
    assert "did_you_mean_semantic" not in a and a == b
    assert CountingService.calls == 0


def test_on_miss_adds_its_own_block_and_never_touches_the_exact_answer(eng):
    off = eng.find(corpus="c", text="which extractor made this thing", semantic="off")
    on = eng.find(corpus="c", text="which extractor made this thing", semantic="on_miss")
    assert off["count"] == on["count"] == 0
    for k in EXACT_KEYS:
        assert off.get(k) == on.get(k), k
    blk = on["did_you_mean_semantic"]
    assert blk["kind"] == "semantic-suggestions-not-matches" and blk["is_assertion"] is False
    assert blk["status"] == "ok" and len(blk["candidates"]) <= 5
    assert _strip(on) == _strip(off)                     # nothing else moved
    note = on["extent"]["scope_note"].lower()
    assert "exact" in note and "suggestion" in note and "not matches" in note


def test_no_semantic_work_when_the_exact_path_has_hits(eng):
    r = eng.find(corpus="c", text="tile map", semantic="on_miss")
    assert r["count"] > 0 and "did_you_mean_semantic" not in r
    assert CountingService.calls == 0


def test_corpus_omitted_is_skipped_not_fanned_out(eng):
    r = eng.find(text="which extractor made this thing", semantic="on_miss")
    assert r["count"] == 0
    blk = r["did_you_mean_semantic"]
    assert blk["status"] == "skipped" and "corpus" in blk["reason"] and blk["candidates"] == []
    assert CountingService.calls == 0


def test_env_switch_turns_it_on(eng, monkeypatch):
    monkeypatch.setenv("LTS_SEMANTIC_ON_MISS", "1")
    r = eng.find(corpus="c", text="which extractor made this thing")
    assert r["did_you_mean_semantic"]["status"] == "ok"


def test_unavailable_model_leaves_the_exact_answer_untouched(eng, tmp_path):
    eng.semantic = S.SemanticService(state_root=str(tmp_path / "s9"), model_root=str(tmp_path / "nowhere"),
                                     pin=eng.semantic.pin, build_mode="sync")
    off = eng.find(corpus="c", text="which extractor made this thing", semantic="off")
    on = eng.find(corpus="c", text="which extractor made this thing", semantic="on_miss")
    assert _strip(on) == _strip(off)
    assert on["did_you_mean_semantic"]["status"] == "unavailable"


def test_bad_value_is_refused_loudly(eng):
    r = eng.find(corpus="c", text="x", semantic="always")
    assert r["success"] is False and "semantic" in r["error"]


def test_analytics_never_triggers_suggestions_even_with_the_env_on(eng, monkeypatch):
    monkeypatch.setenv("LTS_SEMANTIC_ON_MISS", "1")
    from factlattice_engine.analytics import LtsAggregator
    r = LtsAggregator(eng).count(corpus="c", text="which extractor made this thing")
    assert r["success"] and CountingService.calls == 0
