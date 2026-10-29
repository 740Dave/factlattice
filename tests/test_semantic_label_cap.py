"""[SEM-LABEL-CAP] LTS_SEMANTIC_HIGH_LABEL=on|off (default on): a deployment switch that stops
semantic suggestions ever claiming `high` / `strong match` (maintainer ruling, option (b))."""
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

Q = "which extractor made this thing"


@pytest.fixture()
def eng(tmp_path, monkeypatch):
    monkeypatch.delenv("LTS_SEMANTIC_HIGH_LABEL", raising=False)
    monkeypatch.delenv("LTS_SEMANTIC_ON_MISS", raising=False)
    monkeypatch.setattr(S, "top_label", lambda *a, **k: "high")     # force a `high` top-1
    _corpus(tmp_path / "corpus")
    pin = _tiny_model(str(tmp_path / "models"))
    reg = {"c": {"roots": [{"path": str(tmp_path / "corpus"), "area": "specs"}], "domain_suffix": "proj.c"}}
    e = LtsEngine(config.build_store(reg))
    e.semantic = S.SemanticService(state_root=str(tmp_path / "state"), model_root=str(tmp_path / "models"),
                                   pin=pin, build_mode="sync")
    return e


def _strip(b):
    b = json.loads(json.dumps(b, default=str))
    b.pop("preface", None)
    return b


def _labels(b):
    return [(c["label"], c["confidence"]) for c in b["candidates"]]


def test_default_and_on_keep_high_and_no_policy_field(eng, monkeypatch):
    eng.semantic_find(corpus="c", text="warm up")          # build the index outside the comparison
    a = eng.semantic_find(corpus="c", text=Q)
    assert a["candidates"][0]["label"] == "high" and "label_policy" not in a
    monkeypatch.setenv("LTS_SEMANTIC_HIGH_LABEL", "on")
    b = eng.semantic_find(corpus="c", text=Q)
    assert _strip(a) == _strip(b)


def test_off_never_emits_high_and_keeps_everything_else(eng, monkeypatch):
    eng.semantic_find(corpus="c", text="warm up")
    on = eng.semantic_find(corpus="c", text=Q)
    monkeypatch.setenv("LTS_SEMANTIC_HIGH_LABEL", "off")
    off = eng.semantic_find(corpus="c", text=Q)
    assert all(l == "medium" and c == "related, not asserted" for l, c in _labels(off))
    assert [(c["id"], c["rank"], c["scores"]) for c in on["candidates"]] == \
           [(c["id"], c["rank"], c["scores"]) for c in off["candidates"]]
    assert off["label_policy"] == "strong-match disabled by deployment"


def test_off_applies_to_on_miss_too(eng, monkeypatch):
    monkeypatch.setenv("LTS_SEMANTIC_HIGH_LABEL", "off")
    r = eng.find(corpus="c", text=Q, semantic="on_miss")
    blk = r["did_you_mean_semantic"]
    assert blk["status"] == "ok" and blk["candidates"]
    assert all(l == "medium" for l, _ in _labels(blk)) and blk["label_policy"]


def test_unknown_value_warns_once_with_the_raw_value_and_behaves_as_on(eng, monkeypatch, capsys):
    monkeypatch.setattr(S, "_WARNED_HIGH_LABEL", set())
    monkeypatch.setenv("LTS_SEMANTIC_HIGH_LABEL", "OF ")
    r = eng.semantic_find(corpus="c", text=Q)
    assert r["candidates"][0]["label"] == "high" and "label_policy" not in r
    eng.semantic_find(corpus="c", text=Q)
    err = capsys.readouterr().err
    assert err.count("LTS_SEMANTIC_HIGH_LABEL") == 1 and "'OF '" in err


def test_off_label_policy_is_on_every_ok_block_including_empty_and_never_on_unavailable(eng, monkeypatch, tmp_path):
    monkeypatch.setenv("LTS_SEMANTIC_HIGH_LABEL", "off")
    empty = eng.semantic_find(corpus="c", text=Q, prefix="ZZZNONE-")
    assert empty["status"] == "ok" and empty["candidates"] == [] and empty["label_policy"]
    eng.semantic = S.SemanticService(state_root=str(tmp_path / "s9"), model_root=str(tmp_path / "nowhere"),
                                     pin=eng.semantic.pin, build_mode="sync")
    u = eng.semantic_find(corpus="c", text=Q)
    assert u["status"] == "unavailable" and "label_policy" not in u


def test_off_keeps_on_miss_order_and_scores(eng, monkeypatch):
    eng.find(corpus="c", text="warm up", semantic="on_miss")
    on = eng.find(corpus="c", text=Q, semantic="on_miss")["did_you_mean_semantic"]["candidates"]
    monkeypatch.setenv("LTS_SEMANTIC_HIGH_LABEL", "off")
    off = eng.find(corpus="c", text=Q, semantic="on_miss")["did_you_mean_semantic"]["candidates"]
    assert [(c["id"], c["rank"], c["scores"]) for c in on] == [(c["id"], c["rank"], c["scores"]) for c in off]
