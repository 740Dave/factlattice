"""[SEM-FIND] S3: `lts_semantic_find`, the explicit, opt-in semantic suggestion tool, and its CLI.

Ratified rules ([DEC-SEMANTIC-ASSIST]; ISSUE-SEMANTIC-DESIGN honesty
rules 1-8; the architect + the maintainer rulings 2026-10-02):
  * suggestions live in their OWN labelled block: never `nodes`, never a `count` of matches,
    `is_assertion: false`, and an empty list is not evidence of absence;
  * every candidate carries its title, a plain confidence word, the matched AND unmatched query
    terms, and an evidence line (humans will read this, per the maintainer);
  * `corpus` is REQUIRED (no fan-out, so a private corpus is searched only when named);
  * a missing model stack is a STATUS (`unavailable` + reason), never an error;
  * no network; the index is per corpus, incremental, and never mixed across models.
These tests pin the CONTRACT on a hermetic tiny model; ranking QUALITY is the fixture's job.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")

from factlattice_engine import config  # noqa: E402
from factlattice_engine.semantic import suggest as S  # noqa: E402
from factlattice_engine.tools import LTS_ALL_TOOL_NAMES, LtsEngine, dispatch  # noqa: E402
from tests.semantic_helpers import _tiny_model  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _corpus(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "00-c.lts").write_text(
        "# Corpus [DOC-C]\n\n"
        "## Tile map version field [XL-LEDGER-PROTOBUF]\n\n- status: active\n"
        "- what: the tile map version field records which extractor made it\n\ntile map version prose\n\n"
        "## Hello world [N-HELLO]\n\n- what: hello world greeting\n\nhello world prose\n\n"
        "## Map overview [MAP-1]\n\n- what: map overview\n", encoding="utf-8")


@pytest.fixture()
def world(tmp_path):
    _corpus(tmp_path / "corpus")
    pin = _tiny_model(str(tmp_path / "models"))
    reg = {"c": {"roots": [{"path": str(tmp_path / "corpus"), "area": "specs"}], "domain_suffix": "proj.c"},
           "someone-personality": {"roots": [{"path": str(tmp_path / "corpus"), "area": "specs"}],
                                   "domain_suffix": "proj.p"}}
    svc = S.SemanticService(state_root=str(tmp_path / "state"), model_root=str(tmp_path / "models"),
                            pin=pin, build_mode="sync")
    eng = LtsEngine(config.build_store(reg))
    eng.semantic = svc
    return tmp_path, eng, svc, pin, reg


def test_corpus_is_required(world):
    _t, eng, *_ = world
    r = eng.semantic_find(text="tile map")
    assert r["success"] is False and "corpus" in r["error"]


def test_response_is_a_labelled_suggestion_block_never_matches(world):
    _t, eng, svc, *_ = world
    r = eng.semantic_find(corpus="c", text="tile map version")
    assert r["success"] and r["status"] == "ok", r
    assert r["kind"] == "semantic-suggestions-not-matches" and r["is_assertion"] is False
    for forbidden in ("nodes", "count", "total_matched"):
        assert forbidden not in r, forbidden
    assert r["model"] == svc.model_id and r["index_fresh"] is True
    assert "not matches" in r["note"].lower() and "absence" in r["note"].lower()
    assert r["candidates"], r
    for c in r["candidates"]:
        assert set(c) >= {"rank", "id", "title", "label", "confidence", "matched_terms",
                          "unmatched_terms", "evidence", "via"}
        assert c["label"] in ("high", "medium")
        assert c["confidence"] == {"high": "strong match", "medium": "related, not asserted"}[c["label"]]
    assert r["extent"]["corpora_consulted"] == ["c"]


def test_unknown_terms_are_reported_as_unmatched(world):
    _t, eng, *_ = world
    r = eng.semantic_find(corpus="c", text="tile map smart tv")
    top = r["candidates"][0]
    assert "smart" in top["unmatched_terms"] and "tv" in top["unmatched_terms"]
    assert top["label"] == "medium"                     # an unknown term can never be 'high'


def test_missing_model_is_a_status_not_an_error(world, tmp_path):
    _t, eng, svc, pin, _reg = world
    eng.semantic = S.SemanticService(state_root=str(tmp_path / "s2"), model_root=str(tmp_path / "nowhere"),
                                     pin=pin, build_mode="sync")
    r = eng.semantic_find(corpus="c", text="tile map")
    assert r["success"] is True and r["status"] == "unavailable"
    assert r["reason"] == "weights-missing" and "nowhere" in r["live_detail"] and r["candidates"] == []


def test_background_mode_says_index_building_then_serves(world, tmp_path):
    _t, eng, _svc, pin, _reg = world
    svc = S.SemanticService(state_root=str(tmp_path / "s3"), model_root=str(tmp_path / "models"),
                            pin=pin, build_mode="background")
    eng.semantic = svc
    first = eng.semantic_find(corpus="c", text="tile map")
    assert first["success"] and first["status"] == "index_building" and first["candidates"] == []
    assert svc.wait_idle(30)
    second = eng.semantic_find(corpus="c", text="tile map")
    assert second["status"] == "ok" and second["candidates"]


def test_a_small_edit_is_topped_up_synchronously(world):
    tmp, eng, svc, _pin, reg = world
    eng.semantic_find(corpus="c", text="tile map")
    p = tmp / "corpus" / "00-c.lts"
    p.write_text(p.read_text().replace("hello world prose", "hello world prose, edited"), encoding="utf-8")
    eng2 = LtsEngine(config.build_store(reg))
    eng2.semantic = svc
    r = eng2.semantic_find(corpus="c", text="hello world")
    assert r["status"] == "ok" and r["index_fresh"] is True
    assert r["index"]["embedded_on_query"] == 1


def test_filters_apply_to_candidates(world):
    _t, eng, *_ = world
    r = eng.semantic_find(corpus="c", text="tile map version", prefix="MAP")
    assert r["candidates"] and all(c["id"].startswith("MAP") for c in r["candidates"])


def test_a_private_corpus_is_searchable_only_by_name(world):
    _t, eng, *_ = world
    r = eng.semantic_find(corpus="someone-personality", text="tile map")
    assert r["success"] and r["extent"]["corpora_consulted"] == ["someone-personality"]


def test_no_network_during_a_query(world, monkeypatch):
    _t, eng, *_ = world

    def _deny(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket, "socket", _deny)
    monkeypatch.setattr(socket, "create_connection", _deny)
    assert eng.semantic_find(corpus="c", text="tile map")["status"] == "ok"


def test_high_requires_both_rankers_to_agree_with_margin_and_known_terms():
    ok = dict(bm25_top="A", emb=[("A", 0.80), ("B", 0.70)], fused_top="A", unknown=[])
    assert S.top_label(**ok) == "high"
    assert S.top_label(**{**ok, "bm25_top": "B"}) == "medium"            # rankers disagree
    assert S.top_label(**{**ok, "emb": [("A", 0.80), ("B", 0.79)]}) == "medium"   # thin margin
    assert S.top_label(**{**ok, "unknown": ["tv"]}) == "medium"           # out-of-corpus term
    assert S.top_label(**{**ok, "fused_top": "B"}) == "medium"


def test_tool_is_registered_and_dispatchable(world):
    _t, eng, *_ = world
    assert "lts_semantic_find" in LTS_ALL_TOOL_NAMES
    from factlattice_engine import server
    assert any(t["name"] == "lts_semantic_find" for t in server.TOOLS)
    r = dispatch("lts_semantic_find", {"corpus": "c", "text": "tile map"}, tools=eng)
    assert r["success"] and r["kind"] == "semantic-suggestions-not-matches"


def test_cli_human_and_json(world):
    tmp, _eng, _svc, pin, reg = world
    (tmp / "reg.json").write_text(json.dumps(reg))
    (tmp / "pin.json").write_text(json.dumps(pin.__dict__))
    env = {**os.environ, "FACTLATTICE_CORPUS_CONFIG": str(tmp / "reg.json"),
           "LTS_SEMANTIC_MODEL_DIR": str(tmp / "models"), "LTS_SEMANTIC_PIN_FILE": str(tmp / "pin.json"),
           "LTS_ENGINE_STATE_DIR": str(tmp / "cli-state"), "PYTHONPATH": REPO}
    human = subprocess.run([sys.executable, "-m", "factlattice_engine", "semantic", "tile map version",
                            "--corpus", "c"], capture_output=True, text=True, env=env, cwd=REPO, timeout=120)
    assert human.returncode == 0, human.stderr[-2000:]
    assert "NOT matches" in human.stdout and "XL-LEDGER-PROTOBUF" in human.stdout
    assert "matched:" in human.stdout
    js = subprocess.run([sys.executable, "-m", "factlattice_engine", "semantic", "tile map version",
                         "--corpus", "c", "--json"], capture_output=True, text=True, env=env, cwd=REPO, timeout=120)
    assert js.returncode == 0, js.stderr[-2000:]
    assert json.loads(js.stdout)["kind"] == "semantic-suggestions-not-matches"
