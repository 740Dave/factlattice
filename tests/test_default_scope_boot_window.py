"""A default-scope search while corpora OFF the list are still PENDING, FORCED with an event (no sleeps).

The intermittent failures on a Linux runner (tests/test_default_scope.py): `left_out_count` read 0 where 1 was expected (and 1 where 2), and a private corpus on the
list was counted as `private-corpus-failed` where `private: name it explicitly` was expected. Cause: the server answers while corpora are still pending (spec 38
x-server). A search with a default list waits only for the pending corpora ON the list; a corpus off the list (here b1, public, and `secret`, private) can still be
pending when it runs, and a pending corpus is counted differently from a served one: a pending public corpus is not yet in left_out_count, and a pending private one
is counted once, anonymously, in `private-corpus-failed`, never named (spec 38 x-list: "A private corpus is counted as today, also while pending, never named").
So the replies were right and the tests assumed a load order the engine does not promise. Here the loader's read of b1 and secret is held on an event, so the
ordering is the one the runner met, every time; then it is released and the settled state is asserted. Every wait has a timeout."""
from __future__ import annotations

import json
import threading

import pytest

WAIT_S = 30.0
LISTED = "a1, a2"


def _doc(c):
    return (f'---\nlts: "1.1"\ntitle: {c}\ndomain: x.{c}\ndoctype: SPEC\n---\n\n# {c} root [{c.upper()}-ROOT]\n\n'
            f"- summary: root\n\n## Node in {c} [{c.upper()}-NODE]\n\n- part-of: [{c.upper()}-ROOT]\n- topic: lanternfish\n")


@pytest.fixture()
def held(tmp_path, monkeypatch):
    """Registry a1, a2, b1 (public) and secret (private), booted as the server boots (async), with b1's and secret's reads held until `release` is set."""
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "st"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    reg = {}
    for c in ("a1", "a2", "b1", "secret"):
        (tmp_path / c).mkdir()
        (tmp_path / c / f"{c}.lts").write_text(_doc(c), encoding="utf-8")
        reg[c] = {"roots": [{"path": str(tmp_path / c)}], "domain_suffix": f"x.{c}"}
    reg["secret"]["private"] = True
    (tmp_path / "reg.json").write_text(json.dumps(reg), encoding="utf-8")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(tmp_path / "reg.json"))
    monkeypatch.setenv("LTS_DEFAULT_CORPORA", LISTED)
    monkeypatch.chdir(tmp_path)

    from factlattice_engine import bootload
    release, reached = threading.Event(), threading.Event()
    real = bootload.StoreLoader._load_one

    def gated(self, cid):
        if cid in ("b1", "secret"):
            reached.set()
            if not release.wait(WAIT_S):                    # a hang is a failure, never a pass
                raise AssertionError("the test never released the held corpora")
        return real(self, cid)

    monkeypatch.setattr(bootload.StoreLoader, "_load_one", gated)
    from factlattice_engine import server
    eng = server.boot_engine(retry_delay_s=0.0, watch=False, async_boot=True)
    p = type("H", (), {})()
    p.eng, p.release, p.reached = eng, release, reached
    yield p
    release.set()                                           # never leave a loader thread held


def _search(eng):
    from factlattice_engine import tools
    return tools.dispatch("lts_find", {"text": "lanternfish"}, tools=eng)


def _consulted(r):
    return sorted({n["corpus"] for n in r["nodes"]})


def test_the_pending_state_is_counted_as_the_spec_says_then_the_settled_state_holds(held):
    from factlattice_engine import tools
    assert held.reached.wait(WAIT_S), "the loader never reached the held corpora"
    r = _search(held.eng)                                   # waits for a1 and a2 (on the list) only; b1 and secret are held PENDING
    assert _consulted(r) == ["a1", "a2"], r["extent"]
    sc = r["extent"]["search_scope"]
    # the exact values seen on the runner
    assert sc["source"] == "server-list" and sc["left_out_count"] == 0, sc                 # pending b1 is not yet counted as left out
    skipped = r["extent"]["corpora_skipped"]
    assert {"reason": "private-corpus-failed", "count": 1} in skipped, skipped            # the pending private corpus: counted, never named
    assert {"reason": "private: name it explicitly", "count": 1} not in skipped, skipped   # that entry is for a SERVED private corpus
    assert "secret" not in json.dumps(r)
    held.release.set()                                      # the reads end; a call NAMING a corpus waits for it (spec 38 x-server)
    for c in ("b1", "secret"):
        named = tools.dispatch("lts_find", {"corpus": c, "prefix": "NOSUCH-"}, tools=held.eng)
        assert named.get("success") is not False, named
    r = _search(held.eng)
    assert _consulted(r) == ["a1", "a2"]
    sc = r["extent"]["search_scope"]
    assert sc["source"] == "server-list" and sc["left_out_count"] == 1, sc                 # b1 is now served and left out
    skipped = r["extent"]["corpora_skipped"]
    assert {"reason": "private: name it explicitly", "count": 1} in skipped, skipped
    assert not [x for x in skipped if x.get("reason") == "private-corpus-failed"], skipped
    assert "secret" not in json.dumps(r)
