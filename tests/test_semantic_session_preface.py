"""[SEM-SESSION-PREFACE] a short disclaimer before the FIRST semantic results of a session.

The maintainer ruling on (2026-10-03): before the semantic results, say they are not
definitive and should drive a second, targeted exact query; once per session, not every query.
One engine process = one MCP client session, so the flag lives on the engine instance and is
shared by lts_semantic_find and the on_miss `did_you_mean_semantic` block.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")

from factlattice_engine import config  # noqa: E402
from factlattice_engine.semantic import suggest as S  # noqa: E402
from factlattice_engine.tools import LtsEngine  # noqa: E402
from tests.semantic_helpers import _tiny_model  # noqa: E402
from tests.test_semantic_find import _corpus  # noqa: E402

PREFACE = ("Semantic suggestions are leads, not answers. Confirm one with a targeted exact query "
           "(lts_node on its id, or lts_find) before relying on it.")
MISS = "which extractor made this thing"


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("LTS_SEMANTIC_ON_MISS", raising=False)
    _corpus(tmp_path / "corpus")
    pin = _tiny_model(str(tmp_path / "models"))
    reg = {"c": {"roots": [{"path": str(tmp_path / "corpus"), "area": "specs"}], "domain_suffix": "proj.c"}}

    def make():
        e = LtsEngine(config.build_store(reg))
        e.semantic = S.SemanticService(state_root=str(tmp_path / "state"), model_root=str(tmp_path / "models"),
                                       pin=pin, build_mode="sync")
        return e
    return tmp_path, make, pin


def _wire(r):
    return json.loads(json.dumps(r, default=str))    # key order as the client sees it


def test_constant_matches_spec():
    from factlattice_engine import tools
    assert tools.SEMANTIC_PREFACE == PREFACE


def test_first_semantic_find_carries_it_first_then_never_again(world):
    _t, make, _p = world
    eng = make()
    a = _wire(eng.semantic_find(corpus="c", text=MISS))
    assert a["status"] == "ok" and a["candidates"]
    assert next(iter(a)) == "preface" and a["preface"] == PREFACE
    b = _wire(eng.semantic_find(corpus="c", text="hello greeting"))
    assert b["status"] == "ok" and b["candidates"] and "preface" not in b
    m = eng.find(corpus="c", text=MISS, semantic="on_miss")
    assert m["did_you_mean_semantic"]["status"] == "ok" and "preface" not in m["did_you_mean_semantic"]


def test_first_on_miss_block_carries_it_first_then_semantic_find_does_not(world):
    _t, make, _p = world
    eng = make()
    m = _wire(eng.find(corpus="c", text=MISS, semantic="on_miss"))
    blk = m["did_you_mean_semantic"]
    assert blk["status"] == "ok" and blk["candidates"]
    assert next(iter(blk)) == "preface" and blk["preface"] == PREFACE
    assert "preface" not in {k for k in m if k != "did_you_mean_semantic"}
    m2 = eng.find(corpus="c", text=MISS, semantic="on_miss")
    assert "preface" not in m2["did_you_mean_semantic"]
    s = eng.semantic_find(corpus="c", text=MISS)
    assert s["candidates"] and "preface" not in s


def test_a_new_process_shows_it_again(world):
    _t, make, _p = world
    assert "preface" in make().semantic_find(corpus="c", text=MISS)
    assert "preface" in make().semantic_find(corpus="c", text=MISS)


def test_unavailable_block_does_not_consume_it(world):
    tmp, make, pin = world
    eng = make()
    good = eng.semantic
    eng.semantic = S.SemanticService(state_root=str(tmp / "s9"), model_root=str(tmp / "nowhere"),
                                     pin=pin, build_mode="sync")
    u = eng.semantic_find(corpus="c", text=MISS)
    assert u["status"] == "unavailable" and "preface" not in u
    u2 = eng.find(corpus="c", text=MISS, semantic="on_miss")
    assert u2["did_you_mean_semantic"]["status"] == "unavailable" and "preface" not in u2["did_you_mean_semantic"]
    eng.semantic = good
    assert eng.semantic_find(corpus="c", text=MISS).get("preface") == PREFACE


def test_skipped_and_empty_blocks_do_not_consume_it(world):
    _t, make, _p = world
    eng = make()
    sk = eng.find(text=MISS, semantic="on_miss")                       # corpus omitted -> skipped
    assert sk["did_you_mean_semantic"]["status"] == "skipped" and "preface" not in sk["did_you_mean_semantic"]
    e = eng.semantic_find(corpus="c", text=MISS, prefix="NOPE-")         # filter leaves nothing
    assert e["candidates"] == [] and "preface" not in e
    assert eng.semantic_find(corpus="c", text=MISS).get("preface") == PREFACE


def test_off_path_is_byte_identical(world):
    _t, make, _p = world
    eng = make()
    a = eng.find(corpus="c", text=MISS)
    b = eng.find(corpus="c", text=MISS, semantic="off")
    assert "did_you_mean_semantic" not in a and a == b and "preface" not in json.dumps(a)
    # and the off path does not consume it either
    assert eng.semantic_find(corpus="c", text=MISS).get("preface") == PREFACE


def test_concurrent_first_calls_show_it_exactly_once(world):
    _t, make, _p = world
    eng = make()
    eng.semantic_find(corpus="c", text="warm up")       # build the index outside the race
    eng._semantic_preface_shown = False
    real, entered = eng._semantic_preface_lock, []

    class SlowLock:                                     # widen the check-then-set window, and
        def __enter__(self):                            # witness that the lock is actually used
            entered.append(1)
            real.acquire()
            time.sleep(0.005)
            return self

        def __exit__(self, *a):
            real.release()
    eng._semantic_preface_lock = SlowLock()
    out, go = [], threading.Barrier(8)

    def call():
        go.wait()
        out.append(eng.semantic_find(corpus="c", text=MISS))
    ts = [threading.Thread(target=call) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum("preface" in r for r in out) == 1
    assert len(entered) == 8                          # every ok block went through the lock


def test_per_block_label_is_unchanged(world):
    _t, make, _p = world
    eng = make()
    for r in (eng.semantic_find(corpus="c", text=MISS), eng.semantic_find(corpus="c", text=MISS)):
        assert r["kind"] == "semantic-suggestions-not-matches" and r["is_assertion"] is False
        assert r["note"] == S.NOTE


def test_over_the_mcp_wire_the_preface_is_the_first_thing_the_client_reads(world):
    from factlattice_engine.server import Server
    _t, make, _p = world
    srv = Server(make())

    def call(i):
        r = srv.handle({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {
            "name": "lts_semantic_find", "arguments": {"corpus": "c", "text": MISS}}})
        return r["result"]["content"][0]["text"]
    first, second = call(1), call(2)
    assert first.lstrip("{ \n").startswith('"preface": ' + json.dumps(PREFACE))
    assert '"preface"' not in second and '"candidates"' in second
