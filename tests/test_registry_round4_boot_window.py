"""The boot window and a PRIVATE pending corpus in `lts_list_corpora`, FORCED with an event (no sleeps).

The intermittent failure on a Linux runner (tests/test_registry_round4.py, case [private as a string]): the listing held only ['zz-planted-a'] where
['zz-planted-a', 'zz-planted-b'] was expected. Cause: corpus b (`private: "yes"`, which `privacy.private_reason` reads as PRIVATE) was still PENDING when the
listing was taken. The loader reads corpora one at a time, `lts_find` naming corpus a waits for a only, and `tools._list_corpora_body` skips a pending
private corpus (`if prec.get("private"): continue`: counted, never named). Spec 38 x-list: "A private corpus is counted as today, also while pending,
never named." So the listing is correct; a test that expects b by name in a boot-window listing assumed an ordering the engine does not promise.
Here the loader's read of b is held on an event, so the ordering is the one the runner met, every time. Every wait has a timeout."""
from __future__ import annotations

import json
import threading

import pytest

IDS = ("zz-planted-a", "zz-planted-b")
WAIT_S = 30.0


@pytest.fixture()
def held(tmp_path, monkeypatch):
    """A registry of a (healthy) and b (`private` as a string), booted as the server boots (async), with b's read held until `release` is set."""
    st = tmp_path / "st"
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(st))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    (tmp_path / "corp").mkdir()
    (tmp_path / "corp" / "a.lts").write_text("# D [D-1]\n\n## N [N-1]\n\n- status: active\n", encoding="utf-8")
    badd = tmp_path / "badd"
    badd.mkdir()
    (badd / "x.lts").write_text("# D [X-1]\n\n## N [X-2]\n\n- status: active\n", encoding="utf-8")
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps({IDS[0]: {"root": str(tmp_path / "corp"), "domain_suffix": "proj.a"},
                               IDS[1]: {"root": str(badd), "private": "yes"}}), encoding="utf-8")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(reg))
    monkeypatch.chdir(tmp_path)

    from factlattice_engine import bootload
    release, reached = threading.Event(), threading.Event()
    real = bootload.StoreLoader._load_one

    def gated(self, cid):
        if cid == IDS[1]:
            reached.set()
            if not release.wait(WAIT_S):                    # a hang is a failure, never a pass
                raise AssertionError("the test never released corpus b's read")
        return real(self, cid)

    monkeypatch.setattr(bootload.StoreLoader, "_load_one", gated)
    from factlattice_engine import server
    eng = server.boot_engine(retry_delay_s=0.0, watch=False, async_boot=True)
    p = type("H", (), {})()
    p.eng, p.release, p.reached = eng, release, reached
    yield p
    release.set()                                           # never leave the loader thread held


def _list(eng):
    from factlattice_engine import tools
    return tools.dispatch("lts_list_corpora", {"heal": False}, tools=eng)


def _ids(lst):
    return sorted(c["corpus_id"] for c in lst["corpora"])


def test_a_pending_private_corpus_is_absent_from_the_listing_then_listed_once_read(held):
    from factlattice_engine import tools
    assert held.reached.wait(WAIT_S), "the loader never reached corpus b"
    # a is read (the loader takes it first); a call naming a returns as soon as a is served, b is still held PENDING
    find = tools.dispatch("lts_find", {"corpus": IDS[0], "prefix": "N"}, tools=held.eng)
    assert find.get("success") is True, find
    lst = _list(held.eng)
    # the exact failure seen on the runner: assert sorted(...) == ['zz-planted-a', 'zz-planted-b'] -> ['zz-planted-a'] only
    assert _ids(lst) == [IDS[0]], lst["corpora"]
    held.release.set()                                      # b's read ends; it is served and the listing names it
    named = tools.dispatch("lts_find", {"corpus": IDS[1], "prefix": "N"}, tools=held.eng)       # a call NAMING b waits for it (spec 38 x-server)
    assert named.get("success") is True, named
    assert _ids(_list(held.eng)) == list(IDS)
