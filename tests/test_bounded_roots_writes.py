"""[FL-BOUNDED-ROOTS] slice 3: membership and the write rules ([MECH-BR-WRITES], [INV-BR-FAIL-CLOSED-KEPT], [ACC-BR] x-c3 and x-c9 for writes,
[PLAN-BR] x-build-traps 2 and 5).

EVERY RESULT HERE IS ABOUT THE STALL MODEL (tests/stall_harness.py), never a real hung drive.

WHAT EACH TEST IS
  BEHAVIOUR, shown red on ea4d8cd (the slice 2 tip) through the in-process engine, where the old engine blocks or answers wrong for the
  reason the test names: every test whose docstring does not say otherwise. A blocked call is asked through `run_limited`, so a red
  run reports "did not finish", never a hang.
  PIN, labelled so and NOT claimed red-first for a behavioural reason (it fails on the old tree only because the symbol it pins is new):
  the stable-code pin and the kernel's kept-path unit test.

THE CHECK THAT MATTERS MOST. A write to a healthy file makes NO disk call under a marked root (`stall.hit_count` is unchanged across it),
and a refused write makes none either. The model counts every gated call, so a write that merely finished because the model was slow
cannot pass. Where a call under the root is EXPECTED (the bounded resolve of a process with no probe) the test checks that every gated
call came from a pool worker and none from the calling thread.

WRITER PROBES use sibling dirs (h, s, c/<id> under one temp root): HOME is never an ancestor or descendant of a corpus dir, and every
refusal test runs an ordinary control write first in the same setup.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.resolver_mode import needs_per_component_resolver
from tests.stall_harness import RootStall, Workspace, run_limited
from tests.test_bounded_roots import CODE_UNRESPONSIVE, find_key, ledger_records
from tests.test_bounded_roots_probes import _engine

LIMIT_S = 6.0


#: tests of the per-component progress belong to the CPython copy; on an interpreter whose algorithm differs the fallback is active, progress is an elapsed
#: budget of the root deadline times the path's components plus a margin, and a slow root of bounded depth is attached, not refused (`test_on_the_fallback_...`).
#: The predicate and the stated skip reason are in `tests/resolver_mode.py`.
copy_only = needs_per_component_resolver


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    return Workspace(tmp_path / "w")


def _T():
    from factlattice_engine import tools as T
    return T


def _mark_b1(eng) -> None:
    """A named read of b1 waits out the root deadline (0.4 s here) and marks it: the state every test below starts its stalled phase from."""
    r = _T().dispatch("lts_find", {"corpus": "b1", "text": "x"}, tools=eng)
    assert r.get("reason") == CODE_UNRESPONSIVE, f"b1 was not marked: {r}"


def _write(eng, name, args, limit=LIMIT_S):
    T = _T()
    return run_limited(lambda: T.dispatch(name, args, tools=eng), limit, name=f"write-{name}")


def _finished(out, stall) -> dict:
    assert out.done, f"the call was still blocked after the limit (gated calls: {stall.hit_names()}, first hits {stall.hits[:3]})"
    assert out.error is None, repr(out.error)
    return out.value


def _a1_props(ws, key="stamp", value="x") -> dict:
    return {"path": str(ws.file("a1")), "node_id": "A1-N", "key": key, "value": value}


def _recs(ws, name):
    return [r for r in ledger_records(ws) if str(r.get("path_real", "")).endswith(name)]


# ======================================================================================================================
# x-healthy-file: a write to a file under no marked root proceeds, whole, with membership_partial
# ======================================================================================================================
def test_healthy_write_with_a_marked_root_makes_no_call_under_it_and_carries_membership_partial(ws, monkeypatch):
    eng = _engine(ws, monkeypatch)
    T = _T()
    ctl = T.dispatch("lts_add_property", _a1_props(ws, "ctl", "before"), tools=eng)
    assert ctl["success"] and ctl["ledger"]["status"] == "ok", ctl
    assert find_key(ctl, "membership_partial") is None, "control: with no root marked the receipt carries no membership_partial"
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            _mark_b1(eng)
            hits = stall.hit_count
            r = _finished(_write(eng, "lts_add_property", _a1_props(ws, "stamp", "healthy")), stall)
            assert r["success"] and r["written"] and r["ledger"]["status"] == "ok", r
            assert stall.hit_count == hits, f"the write made {stall.hit_count - hits} call(s) under the marked root: {stall.hit_names()}"
            assert r["membership_partial"] == {"marked_count": 1, "unconfirmed_count": 0}, r.get("membership_partial")
            assert r["root_membership"]["corpora"] == ["a1"] and "b1" not in json.dumps(r["root_membership"]["corpora"])
            assert "stamp: healthy" in ws.file("a1").read_text()
            recs = _recs(ws, "a1.lts")
            assert len(recs) == 2, "the control record and this one, whole"
            assert "membership_partial" not in recs[0] and recs[1]["membership_partial"] == {"marked_count": 1, "unconfirmed_count": 0}
            assert recs[1]["corpora"] == ["a1"] and recs[1]["ledger_seq"] == 2 and recs[1]["prev_sha256"], recs[1]
            full = _finished(_write(eng, "lts_add_property", {**_a1_props(ws, "stamp2", "again"), "receipt": "full"}), stall)
            assert full["membership_partial"] == {"marked_count": 1, "unconfirmed_count": 0}, "the full receipt carries it too"
            # a write followed by a read returns the write (x-verify), while b1 hangs
            node = _finished(run_limited(lambda: T.dispatch("lts_node", {"corpus": "a1", "node_id": "A1-N"}, tools=eng), LIMIT_S), stall)
            assert "healthy" in json.dumps(node) and "again" in json.dumps(node)
            assert stall.hit_count == hits
        finally:
            stall.release()
            time.sleep(0.3)


def test_every_writer_proceeds_on_a_healthy_file_beside_a_marked_root(ws, monkeypatch):
    """The shared functions (`_meta`, `_ledger_append`, `locate`, `append`) serve all five writers and the create path: each is given its
    own assertion, so a fix inside one that breaks another is seen."""
    eng = _engine(ws, monkeypatch)
    a1 = ws.file("a1")
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            _mark_b1(eng)
            hits = stall.hit_count
            f = str(a1)
            calls = [
                ("lts_add_property", {"path": f, "node_id": "A1-N", "key": "k1", "value": "v1"}),
                ("lts_add_node", {"path": f, "node_id": "A1-NEW", "title": "New"}),
                ("lts_rename_property", {"path": f, "node_id": "A1-N", "key": "k1", "new_key": "k2"}),
                ("lts_remove_property", {"path": f, "node_id": "A1-N", "key": "k2"}),
                ("lts_add_node", {"path": str(ws.root("a1") / "made.lts"), "node_id": "MADE-N", "title": "Made", "create": True}),
            ]
            for name, args in calls:
                r = _finished(_write(eng, name, args), stall)
                assert r["success"], (name, r.get("reason"), r.get("explanation") or r.get("error"))
                assert r["membership_partial"] == {"marked_count": 1, "unconfirmed_count": 0}, name
                led = r["ledger"]
                assert led["status"] in ("ok", "ok_without_undo"), (name, led)
            line = next(i + 1 for i, t in enumerate(a1.read_text().splitlines()) if "topic: lanternfish" in t)
            r = _finished(_write(eng, "lts_repair_property", {"path": f, "node_id": "A1-N", "line": line, "key": "topic", "value": "kept"}), stall)
            assert r["success"] or r.get("refused"), r              # repair may refuse a line that already parses; it must not block
            assert r.get("membership_partial") == {"marked_count": 1, "unconfirmed_count": 0} or r.get("refused")
            assert stall.hit_count == hits, f"{stall.hit_count - hits} call(s) under the marked root: {stall.hit_names()}"
            assert all(x.get("membership_partial") == {"marked_count": 1, "unconfirmed_count": 0} for x in _recs(ws, "a1.lts")), "every record carries it"
            assert len(_recs(ws, "made.lts")) == 1
        finally:
            stall.release()
            time.sleep(0.3)


def test_a_write_with_a_named_roots_table_makes_no_call_under_a_marked_root(ws, monkeypatch):
    """`locate` with a table resolves every root in it (`_root_of`, `_old_paths`, the owner of an old spelling): all of them from the kept
    reading now. Red on the old tree: `_root_of` resolves b1's root and blocks."""
    reg = {"named_roots": {"da": {"path": str(ws.root("a1"))}, "db": {"path": str(ws.root("b1"))}},
           "a1": {"roots": [{"path": "${da}"}], "domain_suffix": "x.a1"},
           "b1": {"roots": [{"path": "${db}"}], "domain_suffix": "x.b1"}}
    eng = _engine(ws, monkeypatch, registry=reg)
    T = _T()
    ctl = T.dispatch("lts_add_property", _a1_props(ws, "ctl", "before"), tools=eng)
    assert ctl["success"] and ctl["ledger"]["status"] == "ok", ctl
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            _mark_b1(eng)
            hits = stall.hit_count
            r = _finished(_write(eng, "lts_add_property", _a1_props(ws, "stamp", "named")), stall)
            assert r["success"] and r["ledger"]["status"] == "ok" and r["membership_partial"] == {"marked_count": 1, "unconfirmed_count": 0}, r
            assert stall.hit_count == hits, stall.hit_names()
            rec = _recs(ws, "a1.lts")[-1]
            assert rec["named_root"] == {"name": "da", "rel": "a1.lts"} and rec["membership_partial"] == {"marked_count": 1, "unconfirmed_count": 0}, rec
        finally:
            stall.release()
            time.sleep(0.3)


# ======================================================================================================================
# x-inside-marked: a file under a marked root is refused BEFORE anything is read, and nothing is written
# ======================================================================================================================
def _b1_calls(ws):
    f = str(ws.file("b1"))
    return [
        ("lts_add_property", {"path": f, "node_id": "B1-N", "key": "stamp", "value": "into-hung"}),
        ("lts_add_node", {"path": f, "node_id": "B1-NEW", "title": "New"}),
        ("lts_add_node", {"path": str(ws.root("b1") / "made.lts"), "node_id": "MADE-N", "title": "Made", "create": True}),
        ("lts_repair_property", {"path": f, "node_id": "B1-N", "line": 12, "key": "topic", "value": "x"}),
        ("lts_rename_property", {"path": f, "node_id": "B1-N", "key": "topic", "new_key": "subject"}),
        ("lts_remove_property", {"path": f, "node_id": "B1-N", "key": "topic"}),
    ]


def test_every_writer_is_refused_root_unresponsive_before_anything_is_read(ws, monkeypatch):
    eng = _engine(ws, monkeypatch)
    T = _T()
    b1_before = ws.file("b1").read_bytes()
    ctl = T.dispatch("lts_add_property", {"path": str(ws.file("b1")), "node_id": "B1-N", "key": "ctl", "value": "ok"}, tools=eng)
    assert ctl["success"], f"control: the same writer, before the stall, writes: {ctl}"
    b1_before = ws.file("b1").read_bytes()
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            _mark_b1(eng)
            hits = stall.hit_count
            for name, args in _b1_calls(ws):
                r = _finished(_write(eng, name, args), stall)
                assert r["success"] is False and r["refused"] is True and r["written"] is False, (name, r)
                assert r["reason"] == CODE_UNRESPONSIVE and r["evidence"]["refusal"] == CODE_UNRESPONSIVE, (name, r.get("reason"))
                assert "clear" in r["evidence"]["fix"] and "no restart" in r["evidence"]["fix"]
                text = json.dumps(r)
                assert str(ws.root("b1")) not in text and "b1" not in text.replace("B1-", ""), f"{name}: the reply names the root or the corpus"
                assert r["path"] is None
            assert stall.hit_count == hits, f"refused writes made {stall.hit_count - hits} call(s) under the root: {stall.hit_names()}"
        finally:
            stall.release()
            time.sleep(0.3)
    assert ws.file("b1").read_bytes() == b1_before, "bytes unchanged"
    assert not (ws.root("b1") / "made.lts").exists(), "the create made no file"
    assert not _recs(ws, "made.lts") and len(_recs(ws, "b1.lts")) == 1, "no ledger record for a refused write"


def test_the_refusal_sits_ahead_of_the_serialising_wrapper_and_never_takes_or_waits_on_the_lock(ws, monkeypatch):
    """x-inside-marked: `edit._serialised` (the realpath of the file, then `_file_lock`) is the first touch of a writer. The refusal is in
    `authoring.writes_scoped` and in the dispatcher, ahead of it: a refused write neither takes the lock nor waits for one another holds."""
    monkeypatch.setenv("FACTLATTICE_WRITE_LOCK_TIMEOUT", "0.3")
    eng = _engine(ws, monkeypatch)
    from factlattice.authoring import edit
    T = _T()
    held = threading.Event()
    release = threading.Event()

    def holder():
        with edit._file_lock(Path(os.path.realpath(ws.file("b1")))):
            held.set()
            release.wait(30)

    th = threading.Thread(target=holder, daemon=True)
    th.start()
    assert held.wait(5), "the test could not take the lock"
    try:
        ctl = T.dispatch("lts_add_property", {"path": str(ws.file("b1")), "node_id": "B1-N", "key": "ctl", "value": "x"}, tools=eng)
        assert ctl["success"] is False and ctl["evidence"]["refusal"] == "lock_busy", f"control: a held lock refuses a write as lock_busy: {ctl.get('evidence')}"
        entered = []
        real_lock = edit._file_lock
        monkeypatch.setattr(edit, "_file_lock", lambda *a, **k: (entered.append(a), real_lock(*a, **k))[1])
        with RootStall(ws.root("b1"), cap_s=30) as stall:
            try:
                _mark_b1(eng)
                t0 = time.monotonic()
                r = _finished(_write(eng, "lts_add_property", {"path": str(ws.file("b1")), "node_id": "B1-N", "key": "k", "value": "v"}), stall)
                took = time.monotonic() - t0
                assert r["reason"] == CODE_UNRESPONSIVE, r.get("reason")
                assert not entered, "the writer's lock was entered"
                assert took < 0.25, f"the refusal waited {took:.2f}s (the lock timeout is 0.3 s): it waited on the lock"
            finally:
                stall.release()
                time.sleep(0.3)
    finally:
        release.set()
        th.join(5)


def test_a_direct_adapter_call_is_refused_too(ws, monkeypatch):
    """A caller of `authoring.add_property` without the dispatcher (the call CLI, a test, another module) gets the same refusal: the scope is in
    the writers, not only in the dispatcher."""
    eng = _engine(ws, monkeypatch)
    from factlattice_engine import authoring as A
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            _mark_b1(eng)
            hits = stall.hit_count
            out = run_limited(lambda: A.add_property(str(ws.file("b1")), "B1-N", "k", "v", store=eng.store), LIMIT_S)
            r = _finished(out, stall)
            assert r["reason"] == CODE_UNRESPONSIVE and r["refused"] is True
            assert stall.hit_count == hits
        finally:
            stall.release()
            time.sleep(0.3)


# ======================================================================================================================
# INV-BR-FAIL-CLOSED-KEPT: history must be provable
# ======================================================================================================================
def _plant_old_ledger(ws, old_file: Path) -> None:
    from factlattice import ledger
    d = ws.state / "ledger"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"v": 1, "ts": "2026-01-01T00:00:00.000+00:00", "ledger_seq": 1, "path_real": str(old_file), "verb": "set_property",
           "file_sha256_after": "0" * 64, "prev_sha256": None}
    (d / f"{ledger._machine_key(str(old_file))}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _was_registry(ws, was: Path) -> dict:
    return {"named_roots": {"da": {"path": str(ws.root("a1")), "was": [str(was)]}},
            "a1": {"roots": [{"path": "${da}"}], "domain_suffix": "x.a1"},
            "b1": {"roots": [{"path": str(ws.root("b1"))}], "domain_suffix": "x.b1"}}


def test_a_marked_root_that_decides_whose_old_ledger_it_is_refuses_old_location_unknown(ws, monkeypatch):
    was = ws.root("b1") / "old"
    eng = _engine(ws, monkeypatch, registry=_was_registry(ws, was))
    T = _T()
    _plant_old_ledger(ws, was / "a1.lts")
    ctl = T.dispatch("lts_add_property", _a1_props(ws, "ctl", "before"), tools=eng)
    assert ctl["success"] and ctl["ledger"]["status"] == "ok", f"control: with b1 answering the write goes through: {ctl.get('reason')} {ctl.get('explanation')}"
    before = ws.file("a1").read_bytes()
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            _mark_b1(eng)
            hits = stall.hit_count
            r = _finished(_write(eng, "lts_add_property", _a1_props(ws, "stamp", "unprovable")), stall)
            assert r["success"] is False and r["refused"] is True, r
            assert r["evidence"]["refusal"] == "state-unavailable" and r["evidence"]["detail"] == "old-location-unknown", r.get("evidence")
            assert r["evidence"]["refusal"] != "named-root-state-unprovable", "that code stays reserved for a failed registry entry"
            assert ws.file("a1").read_bytes() == before, "nothing written"
            assert stall.hit_count == hits, f"the decision read the marked root: {stall.hit_names()}"
        finally:
            stall.release()
            time.sleep(0.3)


def test_a_marked_root_that_decides_nothing_about_this_file_does_not_stop_it(ws, monkeypatch):
    """The refusal is only for the case the spec names. A table whose `was` lies elsewhere (no old ledger under a marked root) writes."""
    elsewhere = ws.base / "elsewhere" / "old"
    eng = _engine(ws, monkeypatch, registry=_was_registry(ws, elsewhere))
    _plant_old_ledger(ws, elsewhere / "a1.lts")
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            _mark_b1(eng)
            r = _finished(_write(eng, "lts_add_property", _a1_props(ws, "stamp", "fine")), stall)
            assert r["success"] and r["ledger"]["status"] == "ok", (r.get("reason"), r.get("explanation"))
        finally:
            stall.release()
            time.sleep(0.3)


def test_a_state_location_that_does_not_answer_refuses_state_unavailable_within_the_bound(ws, monkeypatch):
    """A named root's state lives on another drive (never under a corpus root: [L8] refuses that). Its marker read and ledger stat are bounded
    by the engine's deadline, not left to hang the writer: the write is refused state-unavailable, nothing written. Red on the old tree: the
    marker read blocks on the calling thread."""
    drive = ws.base / "drive"
    state = drive / "hist"
    state.mkdir(parents=True)
    (state / ".factlattice-state").write_text(json.dumps({"root": "da"}), encoding="utf-8")
    reg = {"named_roots": {"da": {"path": str(ws.root("a1")), "state": str(state)}},
           "a1": {"roots": [{"path": "${da}"}], "domain_suffix": "x.a1"},
           "b1": {"roots": [{"path": str(ws.root("b1"))}], "domain_suffix": "x.b1"}}
    eng = _engine(ws, monkeypatch, registry=reg)
    ctl = _T().dispatch("lts_add_property", _a1_props(ws, "ctl", "before"), tools=eng)
    assert ctl["success"] and ctl["ledger"]["status"] == "ok", f"control: with the state reachable the write goes through: {ctl.get('evidence')} {ctl.get('explanation')}"
    assert any(drive.rglob("*.jsonl")), "control: the ledger went to the state location"
    before = ws.file("a1").read_bytes()
    with RootStall(drive, cap_s=30) as stall:
        try:
            t0 = time.monotonic()
            r = _finished(_write(eng, "lts_add_property", _a1_props(ws, "stamp", "x")), stall)
            took = time.monotonic() - t0
            assert r["refused"] is True and r["evidence"]["refusal"] == "state-unavailable", r.get("evidence")
            assert took < 0.4 + 2.5, f"the refusal waited {took:.2f}s (deadline 0.4 s)"
            callers = {h[2] for h in stall.hits}
            assert callers and all(c.startswith("lts-root-pool") for c in callers), f"a gated call came from the calling thread: {callers}"
            assert ws.file("a1").read_bytes() == before
        finally:
            stall.release()
            time.sleep(0.3)


# ======================================================================================================================
# x-build-traps 2: a process with no probe
# ======================================================================================================================
def _noprobe_env(ws, monkeypatch, deadline="0.4"):
    for k, v in ws.env({"LTS_ROOT_DEADLINE_S": deadline}).items():
        monkeypatch.setenv(k, v)
    try:
        from factlattice_engine import boundedfs  # absent on the tree before slice 3: the behaviour is what is asked of it, not the symbol
    except ImportError:
        return None
    boundedfs._reset(float(deadline))
    return boundedfs


def test_a_process_with_no_probe_resolves_declared_roots_with_a_bound(ws, monkeypatch):
    """No store, no kernel (an in-process writer): membership falls back to the declared roots and resolves them on its own bounded machinery.
    Red on the old tree: the write resolves b1's root on the calling thread and blocks."""
    _noprobe_env(ws, monkeypatch)
    from factlattice_engine import authoring as A
    ctl = A.add_property(str(ws.file("a1")), "A1-N", "ctl", "before", store=None)
    assert ctl["success"] and "membership_partial" not in ctl, "control: nothing marked, nothing partial"
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            t0 = time.monotonic()
            r = _finished(run_limited(lambda: A.add_property(str(ws.file("a1")), "A1-N", "stamp", "noprobe", store=None), LIMIT_S, name="caller"), stall)
            took = time.monotonic() - t0
            assert r["success"] and r["membership_partial"] == {"marked_count": 1, "unconfirmed_count": 0}, r.get("membership_partial")
            assert r["root_membership"]["corpora"] == ["a1"], r["root_membership"]
            assert took < 0.4 + 2.5, f"the write waited {took:.2f}s for a root that hung (deadline 0.4 s)"
            callers = {h[2] for h in stall.hits}
            assert callers and all(c.startswith("lts-root-pool") for c in callers), f"a gated call came from the caller's own thread: {callers}"
            before = threading.active_count()
            t0 = time.monotonic()
            r2 = _finished(run_limited(lambda: A.add_property(str(ws.file("a1")), "A1-N", "stamp2", "again", store=None), LIMIT_S), stall)
            assert r2["success"] and time.monotonic() - t0 < 0.3, "a root already marked is not asked again and not waited for"
            assert threading.active_count() <= before + 1, "no thread per write while the root hangs"
            into = _finished(run_limited(lambda: A.add_property(str(ws.file("b1")), "B1-N", "k", "v", store=None), LIMIT_S), stall)
            assert into["reason"] == CODE_UNRESPONSIVE and into["refused"] is True
            from factlattice.rootview import lexical
            from factlattice_engine import boundedfs
            slot = boundedfs.registry().slot(lexical(ws.root("b1")), create=False)
            assert slot is not None and len(slot._queue) == 0, "one job per root, ever: writes during the hang queued nothing behind the stuck one"
            # (depends on RootSlot._queue, stated)
        finally:
            stall.release()
            time.sleep(0.3)


def test_a_pending_corpus_never_built_does_not_read_as_outside_every_root(ws, monkeypatch):
    """x-build-traps 2: a declared corpus that is in no store (never built) still counts as a root while another root hangs."""
    p1 = ws.base / "c" / "p1"
    p1.mkdir(parents=True)
    (p1 / "p1.lts").write_text(ws.file("a1").read_text().replace("A1", "P1"), encoding="utf-8")
    ws.registry["p1"] = {"roots": [{"path": str(p1)}], "domain_suffix": "x.p1"}
    ws.registry_path.write_text(json.dumps(ws.registry), encoding="utf-8")
    _noprobe_env(ws, monkeypatch)
    from factlattice_engine import authoring as A
    ctl = A.add_property(str(p1 / "p1.lts"), "P1-N", "ctl", "x", store=None)
    assert ctl["root_membership"]["corpora"] == ["p1"], f"control: {ctl['root_membership']}"
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            r = _finished(run_limited(lambda: A.add_property(str(p1 / "p1.lts"), "P1-N", "stamp", "x", store=None), LIMIT_S), stall)
            assert r["success"] and r["root_membership"]["inside_corpus"] is True and r["root_membership"]["corpora"] == ["p1"], r["root_membership"]
            assert r["membership_partial"] == {"marked_count": 1, "unconfirmed_count": 0}
        finally:
            stall.release()
            time.sleep(0.3)


def test_a_failed_corpus_in_a_store_does_not_read_as_outside_every_root(ws, monkeypatch):
    """x-build-traps 2, the store form: a corpus that failed to build is in no partition, and a write under its root used to read as outside
    every root. Red on the old tree for that reason."""
    f1 = ws.base / "c" / "f1"
    f1.mkdir(parents=True)
    (f1 / "f1.lts").write_text(ws.file("a1").read_text().replace("A1", "F1"), encoding="utf-8")
    os.symlink(str(f1 / "nowhere.lts"), str(f1 / "dangling.lts"))            # a member that cannot be read: the corpus fails to build
    ws.registry["f1"] = {"roots": [{"path": str(f1)}], "domain_suffix": "x.f1"}
    ws.registry_path.write_text(json.dumps(ws.registry), encoding="utf-8")
    eng = _engine(ws, monkeypatch)
    assert "f1" in eng.store._failed_corpora and "f1" not in eng.store.corpus_ids(), "setup: f1 must be a failed corpus"
    r = _T().dispatch("lts_add_property", {"path": str(f1 / "f1.lts"), "node_id": "F1-N", "key": "stamp", "value": "x"}, tools=eng)
    assert r["root_membership"]["inside_corpus"] is True and r["root_membership"]["corpora"] == ["f1"], r["root_membership"]
    assert str(f1) not in [f.declared for f in eng.store.kernel.root_facts()], "a root only this call asked about is never listed as a registered root"
    assert r.get("membership_partial") is None, "and does not count as a marked one"


# ======================================================================================================================
# x-c9: a root's link swapped while serving
# ======================================================================================================================
_LDOC = ('---\nlts: "1.1"\ntitle: t\ndomain: x.l\ndoctype: SPEC\n---\n\n# R [L-ROOT]\n\n- summary: r\n\n## N [L-N]\n\n- topic: {}\n')


def _link_world(ws, monkeypatch, deadline="0.4"):
    base = ws.base
    A, B = base / "dirA", base / "dirB"
    for d, topic in ((A, "alphaword"), (B, "bravoword")):
        d.mkdir(exist_ok=True)
        (d / "l.lts").write_text(_LDOC.format(topic), encoding="utf-8")
    link = base / "link"
    link.symlink_to(A)
    ws.registry_path.write_text(json.dumps({"l": {"roots": [{"path": str(link)}], "domain_suffix": "x.l"}}))
    for k, v in ws.env({"LTS_ROOT_DEADLINE_S": deadline}).items():
        monkeypatch.setenv(k, v)
    try:
        from factlattice_engine import boundedfs
        boundedfs._reset(float(deadline))
    except ImportError:
        pass
    from factlattice_engine.server import build_tools
    eng = build_tools(watch=False, require_corpora=True)
    return eng, A, B, link


def _swap(base: Path, link: Path, target: Path) -> None:
    tmp = base / "swap"
    tmp.symlink_to(target)
    os.replace(tmp, link)


def test_a_write_in_the_window_after_a_link_swap_lands_by_its_own_real_path_and_the_next_poll_sees_the_swap(ws, monkeypatch):
    """x-c9, the write half. A GUARD, not a red-first test: it passes on ea4d8cd too (that engine resolves every root afresh) and on every commit since.
    It pins that a write in the window after a swap, spelled through the root's own link, lands by ITS OWN real path (the ledger record names it,
    with no record for the old place) and attaches to the corpus the link now leads to, and that the poll after the swap changes nothing it should
    not. The red-first tests of the window are the two below it (`..._through_the_old_target...` and `..._two_corpora...`)."""
    eng, A, B, link = _link_world(ws, monkeypatch)
    T = _T()
    _swap(ws.base, link, B)
    w = T.dispatch("lts_add_property", {"path": str(link / "l.lts"), "node_id": "L-N", "key": "stamp", "value": "mid"}, tools=eng)
    assert w["success"], w
    assert "stamp: mid" in (B / "l.lts").read_text() and "stamp: mid" not in (A / "l.lts").read_text(), "the write landed by its own real path"
    recs = ledger_records(ws)
    assert [r["path_real"] for r in recs] == [os.path.realpath(B / "l.lts")], "the record names the file's real path, and there is no record for the old place"
    assert w["root_membership"]["corpora"] == ["l"], "in the window the kept path is one poll old, but the write is spelled through the root's own link: it attaches (fresh resolve of that one root)"
    assert recs[0]["corpora"] == ["l"], "and so does the record"
    eng.store.kernel.tick()                                             # ONE poll
    w2 = T.dispatch("lts_add_property", {"path": str(link / "l.lts"), "node_id": "L-N", "key": "stamp2", "value": "after"}, tools=eng)
    assert w2["success"] and w2["root_membership"]["corpora"] == ["l"], w2["root_membership"]
    assert [r["corpora"] for r in ledger_records(ws)][-1] == ["l"]


# ======================================================================================================================
# PIN: the codes
# ======================================================================================================================
def test_pin_the_write_refusal_code_and_its_fix_text_join_the_ledgers_stable_codes():
    """PIN (red on the old tree because the symbol is new). New write refusals join the ledger's stable codes and their fix text."""
    from factlattice import ledger
    from factlattice.authoring import edit
    from factlattice_engine import rootstate
    assert ledger.ROOT_UNRESPONSIVE == "root-unresponsive" == rootstate.CODE_ROOT_UNRESPONSIVE
    assert ledger.WRITE_REFUSAL_CODES == ("root-unresponsive", "membership-unconfirmed", "corpus-verdict-pending")
    assert ledger.MEMBERSHIP_UNCONFIRMED == "membership-unconfirmed"
    for code in ledger.WRITE_REFUSAL_CODES:
        assert code in edit.WRITER_REFUSAL_CODES and code in edit.KNOWN_CREATE_CODES and code in ledger.REFUSAL_FIX and code not in ledger.LOCATE_CODES
        t = ledger.fix_for(code).lower()
        assert "already" not in t and "exists" not in t and "no restart is needed" in t, code
    assert "retry" in ledger.fix_for("membership-unconfirmed") and "not marked" in ledger.fix_for("membership-unconfirmed")
    assert ledger.ROOT_UNRESPONSIVE not in ledger.LOCATE_CODES, "it is decided before the writer starts, not by `locate`"
    fix = ledger.fix_for(ledger.ROOT_UNRESPONSIVE)
    assert fix == ledger.REFUSAL_FIX[ledger.ROOT_UNRESPONSIVE] and "no restart is needed" in fix and "Nothing was read" in fix
    assert "already" not in fix.lower() and "exists" not in fix.lower(), "a consumer reads those words as an idempotent re-save"
    assert "membership_partial" in ledger.FIELDS


def test_pin_the_kernel_keeps_a_real_path_from_each_answered_probe_and_a_swap_is_seen_by_the_next_poll(ws, monkeypatch):
    """PIN (red on the old tree because the symbol is new). The kept path is replaced by every answered probe, so it is never older than the
    last answer; an earlier change that cached a disk fact for speed regressed, and this is the guard. The control swaps a link that is NOT
    the root: nothing the kernel keeps moves."""
    from factlattice_engine.kernel import FreshnessKernel
    eng, A, B, link = _link_world(ws, monkeypatch)
    k = eng.store.kernel
    facts = {f.declared: f for f in k.root_facts()}
    assert facts[str(link)].real == os.path.realpath(A) and facts[str(link)].state == "ok"
    assert FreshnessKernel().root_real(str(link)) is None, "a kernel that never probed it keeps nothing"
    other = ws.base / "other"
    other.symlink_to(A)
    _swap(ws.base, other, B)
    k.tick()
    assert k.root_real(str(link)) == os.path.realpath(A), "control: a link that is not the root changes nothing"
    _swap(ws.base, link, B)
    assert k.root_real(str(link)) == os.path.realpath(A), "no poll yet: the last answer still stands"
    k.tick()
    assert k.root_real(str(link)) == os.path.realpath(B), "one poll later the swap is seen"
    k.tick()
    assert k.root_real(str(link)) == os.path.realpath(B)


# ======================================================================================================================
# x-sites: report_miss's root guard
# ======================================================================================================================
def test_report_miss_makes_no_call_under_a_marked_root(ws, monkeypatch):
    """The miss log's guard compared the log's place against every registered root by stat and realpath of each root. Red on the old tree: the
    guard stats b1's root and blocks."""
    eng = _engine(ws, monkeypatch)
    T = _T()
    args = {"corpus": "a1", "query": "lanternfish", "expected_ids": ["A1-N"], "why": "w"}
    ctl = T.dispatch("lts_report_miss", args, tools=eng)
    assert ctl["success"] is True, f"control: before the stall the miss is filed: {ctl}"
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            _mark_b1(eng)
            hits = stall.hit_count
            r = _finished(run_limited(lambda: T.dispatch("lts_report_miss", args, tools=eng), LIMIT_S), stall)
            assert r["success"] is True, r
            assert stall.hit_count == hits, f"the guard made {stall.hit_count - hits} call(s) under the marked root: {stall.hit_names()}"
        finally:
            stall.release()
            time.sleep(0.3)


# ======================================================================================================================
# the refusal's own logic, with no stall: strings first, then the file as the writer will resolve it
# ======================================================================================================================
def test_marked_refusal_compares_strings_first_and_then_the_resolved_file(tmp_path):
    """Unit test of `authoring.marked_refusal` and `RootView.marked_holding` on a hand-made reading, with no disk model. The second comparison
    (the file resolved as the writer will) is what catches a path that reaches a marked root through a link nobody registered; under a real hang
    that resolve itself would block, so this is the only form in which it can be tested. Red on the old tree: the symbol is new."""
    from factlattice.rootview import RootFact, RootView
    from factlattice_engine import authoring as A
    real_root = tmp_path / "drive" / "root"
    real_root.mkdir(parents=True)
    (real_root / "f.lts").write_text("x", encoding="utf-8")
    (tmp_path / "alias").symlink_to(real_root)
    declared = str(tmp_path / "declared-spelling")
    view = RootView([RootFact(declared, str(real_root), "unresponsive"), RootFact(str(tmp_path / "ok"), str(tmp_path / "ok"), "ok")])
    assert view.marked_count == 1
    assert A.marked_refusal(view, declared + "/f.lts")["reason"] == "root-unresponsive", "by the declared spelling, as a string"
    assert A.marked_refusal(view, str(real_root / "f.lts"))["reason"] == "root-unresponsive", "by the real path it last answered"
    assert A.marked_refusal(view, str(tmp_path / "alias" / "f.lts"))["reason"] == "root-unresponsive", "through a link nobody registered: the resolved file"
    assert A.marked_refusal(view, str(tmp_path / "ok" / "f.lts")) is None and A.marked_refusal(view, str(tmp_path / "elsewhere.lts")) is None
    assert A.marked_refusal(view, str(real_root) + "-old/f.lts") is None, "a sibling that shares a prefix is not under the root"
    assert A.marked_refusal(RootView([RootFact(declared, str(real_root), "ok")]), declared + "/f.lts") is None, "nothing marked: nothing refused"
    for bad in (None, 3, "", "  ", "a\x00b"):
        assert A.marked_refusal(view, bad) is None, "a path that is not usable is the writer's own to refuse"


def test_the_marked_count_and_the_view_do_not_outlive_the_call(ws, monkeypatch):
    """The reading is taken per call and removed at its end: once the root answers again, the next write carries no `membership_partial` (x-c4 for
    writes), and no view stays installed on the thread."""
    from factlattice import rootview
    eng = _engine(ws, monkeypatch)
    T = _T()
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            _mark_b1(eng)
            r = _finished(_write(eng, "lts_add_property", _a1_props(ws, "k", "during")), stall)
            assert r["membership_partial"] == {"marked_count": 1, "unconfirmed_count": 0}
            assert rootview.current() is None, "the dispatcher removed its view when the call ended"
            refused = _finished(_write(eng, "lts_add_property", {"path": str(ws.file("b1")), "node_id": "B1-N", "key": "k", "value": "v"}), stall)
            assert refused["reason"] == CODE_UNRESPONSIVE and rootview.current() is None, "also after a refusal"
        finally:
            stall.release()
    end = time.monotonic() + 8
    while time.monotonic() < end:
        listing = T.dispatch("lts_list_corpora", {"heal": False}, tools=eng)
        if not any((c.get("root_health") or {}).get("status") in ("pending", "unresponsive") for c in listing["corpora"]):
            break
        time.sleep(0.1)
    else:
        pytest.fail("b1 did not return to ok after the release")
    r2 = T.dispatch("lts_add_property", _a1_props(ws, "k2", "after"), tools=eng)
    assert r2["success"] and "membership_partial" not in r2, "the root answers again: nothing partial"
    assert "membership_partial" not in _recs(ws, "a1.lts")[-1]
    again = T.dispatch("lts_add_property", {"path": str(ws.file("b1")), "node_id": "B1-N", "key": "k3", "value": "v"}, tools=eng)
    assert again["success"], "and a write into that root goes through, with no restart"


def test_a_failed_corpus_whose_root_then_hangs_refuses_a_write_under_it_within_the_bound(ws, monkeypatch):
    """A corpus that failed to build has no probe, so nothing marks its root: the write's own reading resolves it on the bounded machinery and
    marks it, and a file under it is refused root-unresponsive instead of blocking in the writer. Red on the old tree: the write resolves the root
    on the calling thread and blocks."""
    f1 = ws.base / "c" / "f1"
    f1.mkdir(parents=True)
    (f1 / "f1.lts").write_text(ws.file("a1").read_text().replace("A1", "F1"), encoding="utf-8")
    os.symlink(str(f1 / "nowhere.lts"), str(f1 / "dangling.lts"))
    ws.registry["f1"] = {"roots": [{"path": str(f1)}], "domain_suffix": "x.f1"}
    ws.registry_path.write_text(json.dumps(ws.registry), encoding="utf-8")
    eng = _engine(ws, monkeypatch)
    _noprobe_env(ws, monkeypatch)                      # resets the bounded machinery to the 0.4 s deadline; absent on the tree before slice 3
    assert "f1" in eng.store._failed_corpora, "setup: f1 must be a failed corpus"
    ctl = _T().dispatch("lts_add_property", {"path": str(f1 / "f1.lts"), "node_id": "F1-N", "key": "ctl", "value": "x"}, tools=eng)
    assert ctl["success"], f"control: with the root answering the write goes through: {ctl.get('evidence')}"
    with RootStall(f1, cap_s=30) as stall:
        try:
            t0 = time.monotonic()
            r = _finished(_write(eng, "lts_add_property", {"path": str(f1 / "f1.lts"), "node_id": "F1-N", "key": "k", "value": "v"}), stall)
            assert r["refused"] is True and r["reason"] == CODE_UNRESPONSIVE, (r.get("reason"), r.get("evidence"))
            assert time.monotonic() - t0 < 0.4 + 2.5
            callers = {h[2] for h in stall.hits}
            assert callers and all(c.startswith("lts-root-pool") for c in callers), f"a gated call came from the calling thread: {callers}"
        finally:
            stall.release()
            time.sleep(0.3)


def test_a_marked_root_is_not_asked_about_a_case_variant_and_the_variant_is_taken_as_one(tmp_path, monkeypatch):
    """`ledger._variant_rest` compares a root and a case-variant spelling by identity (a stat of the root). With the root marked it asks nothing, and a
    spelling that folds equal is taken as a variant (fail closed: the write is refused, never quietly left outside the state). Works on any volume:
    the variant need not exist. Control: with the root ok the identity check is asked (and answers per the volume)."""
    from factlattice import ledger, rootview
    root = str(tmp_path / "Root")
    os.makedirs(root)
    variant = str(tmp_path / "root" / "x.lts")
    asked = []
    monkeypatch.setattr(os.path, "samefile", lambda a, b: (asked.append((a, b)), False)[1])
    with rootview.use(rootview.RootView([rootview.RootFact(root, root, "ok")])):
        assert ledger._variant_rest(root, variant) is None and len(asked) == 1, "control: an ok root is asked, and said no"
    with rootview.use(rootview.RootView([rootview.RootFact(root, root, "ok")], bounded=lambda key, fn, default: default)):
        assert ledger._variant_rest(root, variant) == "x.lts", "an identity check that does not answer is a variant too (fail closed)"
    asked.clear()
    with rootview.use(rootview.RootView([rootview.RootFact(root, root, "unresponsive")])):
        assert ledger._variant_rest(root, variant) == "x.lts" and asked == [], "marked: nothing is asked, and the variant counts"
        table = {"da": {"path": root, "state_path": str(tmp_path / "drive")}}
        assert ledger._variant_of_state_root(variant, table) is True, "so a write spelled in another case under a marked state root is refused, not left outside it"


def test_a_pending_only_mark_yields_marked_count_one_on_the_record_and_the_view(tmp_path, monkeypatch):
    """membership_partial counts every root that is not ok, pending included, and is named for that (`marked_count`). Red on 864fda7: the key was
    `unresponsive_count`."""
    from factlattice import ledger, rootview
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    f = tmp_path / "corpus" / "f.lts"
    f.parent.mkdir()
    f.write_text("x", encoding="utf-8")
    view = rootview.RootView([rootview.RootFact(str(tmp_path / "p"), None, "pending"), rootview.RootFact(str(tmp_path / "k"), str(tmp_path / "k"), "ok")])
    assert view.marked_count == 1
    with rootview.use(view):
        out = ledger.append(f, {"verb": "set_property", "file_sha256_before": None, "file_sha256_after": "x"}, state_dir=tmp_path / "state")
    rec = json.loads(Path(out["file"]).read_text().splitlines()[-1])
    assert rec["membership_partial"] == {"marked_count": 1, "unconfirmed_count": 0}
    with rootview.use(rootview.RootView([rootview.RootFact(str(tmp_path / "k"), str(tmp_path / "k"), "ok")])):
        out = ledger.append(f, {"verb": "set_property", "file_sha256_before": "x", "file_sha256_after": "y"}, state_dir=tmp_path / "state")
    assert "membership_partial" not in json.loads(Path(out["file"]).read_text().splitlines()[-1]), "control: nothing marked, no field"


# ======================================================================================================================
# x-membership on a miss: the typed path through an ok root's declared spelling, resolved once and bounded
# ======================================================================================================================
def _two_corpora_with_links(ws, monkeypatch):
    base = ws.base
    A, B, O = base / "dirA", base / "dirB", base / "dirO"
    for d, tag in ((A, "A"), (B, "B"), (O, "O")):
        d.mkdir()
        (d / f"{tag.lower()}.lts").write_text(_LDOC.format("w").replace("L-", f"{tag}-").replace("x.l", f"x.{tag.lower()}"), encoding="utf-8")
    (A / "ln").symlink_to(B)
    (A / "out").symlink_to(O)
    ws.registry_path.write_text(json.dumps({"c1": {"roots": [{"path": str(A)}], "domain_suffix": "x.a"},
                                            "c2": {"roots": [{"path": str(B)}], "domain_suffix": "x.b"}}))
    for k, v in ws.env({"LTS_ROOT_DEADLINE_S": "0.4"}).items():
        monkeypatch.setenv(k, v)
    from factlattice_engine.server import build_tools
    return build_tools(watch=False, require_corpora=True), A, B, O


def test_a_link_inside_a_root_never_attaches_the_write_to_the_wrong_corpus(ws, monkeypatch):
    """GUARD (passes before and after the declared-spelling fallback; it fails under the plain fallback, mutant b): A/ln leads into corpus 2's root, so
    a write typed through A attaches to corpus 2 only; A/out leads out of every root, so it reads outside every root."""
    eng, A, B, O = _two_corpora_with_links(ws, monkeypatch)
    T = _T()
    r = T.dispatch("lts_add_property", {"path": str(A / "ln" / "b.lts"), "node_id": "B-N", "key": "k", "value": "v"}, tools=eng)
    assert r["success"] and r["root_membership"]["corpora"] == ["c2"], r["root_membership"]
    o = T.dispatch("lts_add_property", {"path": str(A / "out" / "o.lts"), "node_id": "O-N", "key": "k", "value": "v"}, tools=eng)
    assert o["success"] and o["root_membership"]["corpora"] == [] and o["root_membership"]["inside_corpus"] is False, o["root_membership"]
    a = T.dispatch("lts_add_property", {"path": str(A / "a.lts"), "node_id": "A-N", "key": "k", "value": "v"}, tools=eng)
    assert a["root_membership"]["corpora"] == ["c1"], "control: a plain file in A attaches to corpus 1"


@needs_per_component_resolver
def test_a_confirm_that_does_not_answer_refuses_the_write_membership_unconfirmed_and_marks_nothing(ws, monkeypatch):
    """D. The root is ok in the view; its confirm hangs on the pool worker only (the model engages for pool threads, as if the drive went away just
    after the file's own path resolved). The write typed through that root's declared spelling cannot say where its file lives: it is REFUSED
    `membership-unconfirmed` within the deadline (retryable, nothing written, the root NOT marked); a second write finds the confirm stuck and is
    refused at once without a second thread; after release the root and the writes are normal."""
    eng, A, B, link = _link_world(ws, monkeypatch)
    T = _T()
    k = eng.store.kernel
    _swap(ws.base, link, B)
    before = (B / "l.lts").read_bytes()
    with RootStall(link, cap_s=30, engaged_fn=lambda: threading.current_thread().name.startswith("lts-root-pool")) as stall:
        try:
            t0 = time.monotonic()
            w = _finished(_write(eng, "lts_add_property", {"path": str(link / "l.lts"), "node_id": "L-N", "key": "k", "value": "1"}), stall)
            assert w["refused"] is True and w["written"] is False and w["reason"] == "membership-unconfirmed", w.get("reason")
            assert w["evidence"]["refusal"] == "membership-unconfirmed" and "retry" in w["evidence"]["fix"] and w["path"] is None
            assert time.monotonic() - t0 < 0.4 + 2.5, "the write waited past the deadline"
            assert (B / "l.lts").read_bytes() == before and not _recs(ws, "l.lts"), "nothing written, no record"
            cslot = k.roots.slot("confirm:" + os.path.abspath(link), create=False)
            assert cslot is not None and len(cslot._queue) == 0, "nothing queued behind the stuck confirm"
            assert k.roots.state_of(os.path.abspath(link)) == "ok", "a confirm marks NOTHING: the root stays ok for its probes"
            assert stall.hit_count > 0 and all(h[2].startswith("lts-root-pool") for h in stall.hits), "the hang was on a worker, never the caller"
            threads = threading.active_count()
            t0 = time.monotonic()
            for i in range(10):
                w2 = _finished(_write(eng, "lts_add_property", {"path": str(link / "l.lts"), "node_id": "L-N", "key": "k", "value": str(i)}), stall)
                assert w2["refused"] is True and w2["reason"] == "membership-unconfirmed"
            assert time.monotonic() - t0 < 1.0 and threading.active_count() <= threads, "the stuck confirm is not asked again and adds no thread"
            other = _finished(_write(eng, "lts_add_property", _a1_props(ws, "elsewhere", "v")), stall)
            assert other["success"], "a write under another root is unaffected"
            assert len(cslot._queue) == 0, "ten more writes queued nothing behind the stuck confirm"
        finally:
            stall.release()
            time.sleep(0.5)
    end = time.monotonic() + 10
    ok = None
    while time.monotonic() < end:
        ok = T.dispatch("lts_add_property", {"path": str(link / "l.lts"), "node_id": "L-N", "key": "k", "value": "after"}, tools=eng)
        if ok.get("success"):
            break
        time.sleep(0.2)
    assert ok["success"] and ok["root_membership"]["corpora"] == ["l"] and "membership_partial" not in ok, "released: normal again with no restart"


def test_a_marked_root_is_never_resolved_on_a_miss_and_an_ok_one_is_resolved_once(tmp_path, monkeypatch):
    """Unit: `_confirmed_hits` on a hand-made view. A marked or pending root is never resolved; an ok root with a stale kept path is resolved once and
    attaches by the fresh path; a typed path that is not the file being checked attaches nothing; a root whose fresh path does not hold the file does
    not attach, however its kept path reads."""
    from factlattice.rootview import RootFact, RootView
    from factlattice_engine import boundedfs, membership
    d = tmp_path / "decl"
    d.mkdir()
    (d / "f.lts").write_text("x", encoding="utf-8")
    stale = tmp_path / "elsewhere"
    stale.mkdir()
    real = os.path.realpath(d / "f.lts")
    calls = []
    orig = boundedfs.confirm_real
    monkeypatch.setattr(boundedfs, "confirm_real", lambda path, reg=None, not_before=None: (calls.append(path), orig(path, reg, not_before))[1])

    class P:
        corpus_id, root_paths = "c", [str(d)]

    class Store:
        def partitions(self):
            return [P()]

        def snapshot_all(self):                                   # the one look membership takes: (served, pending, failed, no_roots)
            return [P()], {}, {}, {}
    for state in ("unresponsive", "pending"):
        v = RootView([RootFact(str(d), str(stale), state)])
        v.typed = str(d / "f.lts")
        assert membership._confirmed_hits(v, Store(), real, []) == [] and calls == [], f"a {state} root was resolved"
    v = RootView([RootFact(str(d), str(stale), "ok")])
    v.typed = str(d / "f.lts")
    assert membership._confirmed_hits(v, Store(), real, []) == ["c"] and len(calls) == 1, "an ok root with a stale kept path attaches, resolved once"
    v2 = RootView([RootFact(str(d), str(stale), "ok")])
    v2.typed = str(d / "g.lts")      # under the root, but not the file being checked
    assert membership._confirmed_hits(v2, Store(), real, []) == [], "a typed path that is not the file attaches nothing"
    v3 = RootView([RootFact(str(d), str(tmp_path), "ok")])      # the kept path (the parent) holds the file; the fresh one is d, which also holds it
    v3.typed = None
    assert membership._confirmed_hits(v3, Store(), real, ["c"]) == ["c"]
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(boundedfs, "confirm_real", lambda path, reg=None, not_before=None: str(other))
    v4 = RootView([RootFact(str(d), real and os.path.dirname(real), "ok")])
    assert membership._confirmed_hits(v4, Store(), real, ["c"]) == [], "the kept path holds the file but the fresh one does not: nothing attaches"


# ======================================================================================================================
# round 4 (cold read of 672ecc7)
# ======================================================================================================================
# ---- F1: the refusal matches case-insensitively on every volume ---------------------------------------------------------
def test_a_write_typed_through_a_case_variant_of_a_marked_root_is_refused_before_any_read(ws, monkeypatch):
    """F1. A volume that does not fold case in `realpath` named the marked root by another spelling and the write landed. The REFUSAL folds case on
    every volume (a distinct directory differing only in case is over-refused on a case-sensitive one: the cheaper error). Needs no existing variant,
    so it runs on any volume. Red on 69f2f08."""
    eng = _engine(ws, monkeypatch)
    variant = str(ws.base / "C" / "b1" / "b1.lts")
    before = ws.file("b1").read_bytes()
    ctl = _T().dispatch("lts_add_property", {"path": str(ws.file("b1")), "node_id": "B1-N", "key": "ctl", "value": "x"}, tools=eng)
    assert ctl["success"], "control: the exact spelling writes before the stall"
    before = ws.file("b1").read_bytes()
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            _mark_b1(eng)
            hits = stall.hit_count
            r = _finished(_write(eng, "lts_add_property", {"path": variant, "node_id": "B1-N", "key": "k", "value": "via-variant"}), stall)
            assert r["refused"] is True and r["reason"] == CODE_UNRESPONSIVE, (r.get("reason"), r.get("success"))
            assert stall.hit_count == hits, "refused before any read"
        finally:
            stall.release()
            time.sleep(0.3)
    assert ws.file("b1").read_bytes() == before, "bytes unchanged"


def test_the_refusal_folds_case_for_the_declared_spelling_and_the_kept_real_path(tmp_path):
    """F1, unit, any volume."""
    from factlattice.rootview import RootFact, RootView
    from factlattice_engine import authoring as A
    v = RootView([RootFact(str(tmp_path / "Decl" / "Root"), str(tmp_path / "Real" / "Root"), "unresponsive")])
    for p in (tmp_path / "decl" / "root" / "f.lts", tmp_path / "DECL" / "ROOT" / "f.lts", tmp_path / "real" / "root" / "f.lts"):
        assert A.marked_refusal(v, str(p))["reason"] == CODE_UNRESPONSIVE, p
    assert A.marked_refusal(v, str(tmp_path / "decl" / "root-old" / "f.lts")) is None, "a sibling sharing a prefix is still not under it"


# ---- F2: a stat that does not answer is unknown, never "another file" ---------------------------------------------------
def _same_place_world(tmp_path, *, hardlink: bool):
    from factlattice import ledger
    cur = tmp_path / "cur" / "inner"
    cur.mkdir(parents=True)
    f = cur / "x.lts"
    f.write_text("x", encoding="utf-8")
    other = tmp_path / "otherdrive" / "sub"
    other.mkdir(parents=True)
    if hardlink:
        os.link(f, other / "x.lts")
    else:
        (other / "x.lts").write_text("a different file", encoding="utf-8")
    was = str(tmp_path / "was")
    table = {"da": {"path": str(tmp_path / "cur"), "was": [was]}, "dx": {"path": str(other), "was": [was + "/inner"]}}
    state = tmp_path / "st"
    (state / "ledger").mkdir(parents=True)
    old = was + "/inner/x.lts"
    rec = {"v": 1, "ts": "2026-01-01T00:00:00.000+00:00", "ledger_seq": 1, "path_real": old, "verb": "set_property", "file_sha256_after": "0" * 64, "prev_sha256": None}
    (state / "ledger" / f"{ledger._machine_key(old)}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return table, state, os.path.realpath(f), str(other / "x.lts")


def test_a_same_place_stat_that_does_not_answer_is_unknown_and_one_that_answers_no_is_another_file(tmp_path):
    """F2, both directions. The owner of an old spelling is a different path from the file; whether it is the same file is asked of the disk. No
    answer: unknown (`old-location-unknown`, the write is refused). An answer of no: the ledger belongs to another file and is left out. An answer of
    yes: it counts. Red on 69f2f08: a timeout read as no."""
    from factlattice import ledger, rootview
    table, state, real, owner = _same_place_world(tmp_path, hardlink=True)

    def run(bounded):
        with rootview.use(rootview.RootView([], bounded=bounded)):
            return ledger._legacy(real, state, table)
    counted, left, unknown = run(lambda key, fn, default: fn())
    assert len(counted) == 1 and not unknown and left == [], "control: it answers yes (a hard link): the ledger counts"
    counted, left, unknown = run(lambda key, fn, default: default if key == owner else fn())
    assert counted == [] and unknown is True and left == ["old-location-unknown"], (counted, left, unknown)
    table2, state2, real2, owner2 = _same_place_world(tmp_path / "second", hardlink=False)
    with rootview.use(rootview.RootView([], bounded=lambda key, fn, default: fn())):
        counted, left, unknown = ledger._legacy(real2, state2, table2)
    assert counted == [] and unknown is False and left == ["old-spelling-belongs-to-another-file"], "it answers no: another file's ledger, left out, no refusal"


# ---- F3: one registry per process ----------------------------------------------------------------------------------------
def test_a_process_with_a_kernel_bounds_its_reads_on_the_kernels_registry_and_no_root_gets_two_stuck_threads(ws, monkeypatch):
    """F3. Through `dispatch` with the store `build_tools` made. The bounded resolve of an ok root that then hangs is a job on the KERNEL's slot for
    that root: the kernel counts it as stuck (SLICE 4: and `boundedfs` is the SAME registry, not a second one), a kernel poll during the same stall queues nothing behind it,
    and `lts_list_corpora` reports the one true stuck worker. Red on 69f2f08: two registries, each with its own stuck thread."""
    from factlattice.rootview import lexical
    from factlattice_engine import boundedfs
    eng, A, B, link = _link_world(ws, monkeypatch)
    T = _T()
    k = eng.store.kernel
    _swap(ws.base, link, B)
    with RootStall(link, cap_s=60, engaged_fn=lambda: threading.current_thread().name.startswith("lts-root-pool")) as stall:
        try:
            w = _finished(_write(eng, "lts_add_property", {"path": str(link / "l.lts"), "node_id": "L-N", "key": "k", "value": "1"}), stall)
            assert w["refused"] is True and w["reason"] == "membership-unconfirmed"
            assert k.roots.stuck_workers() == 1, "the kernel's own registry holds the stuck confirm (on its own auxiliary slot, not the root's)"
            assert boundedfs.registry() is k.roots, "SLICE 4: ONE registry per process: boundedfs and the kernel are the same object, so the count above is the whole count"
            slot = k.roots.slot(lexical(link), create=False)
            assert slot is not None and slot.aux is False and k.roots.state_of(lexical(link)) == "ok", "the root's own slot is untouched and the root is not marked"
            threads0 = threading.active_count()
            for i in range(6):
                r = _finished(_write(eng, "lts_add_property", {"path": str(link / "l.lts"), "node_id": "L-N", "key": "k", "value": str(i + 2)}), stall)
                assert r["refused"] is True and r["reason"] == "membership-unconfirmed"
            assert k.roots.stuck_workers() == 1 and threading.active_count() <= threads0, "many writes add no thread"
            lst = T.dispatch("lts_list_corpora", {"heal": False}, tools=eng)
            assert lst.get("stuck_workers") == k.roots.stuck_workers() >= 1, "the listing reports the true count (its own probe of the same hung root may be the second)"
        finally:
            stall.release()
            time.sleep(1.0)
    end = time.monotonic() + 8
    while time.monotonic() < end and k.roots.stuck_workers():
        time.sleep(0.1)
    assert k.roots.stuck_workers() == 0, "released: no stuck worker left"


# ---- F5: the attach decision is confirmed by a fresh resolve -----------------------------------------------------------
def test_a_write_through_the_old_target_of_a_swapped_link_does_not_attach_to_the_link_corpus(ws, monkeypatch):
    """F5, the reader's first window case. Root `l` is a link to A; the link is swapped to B; a write typed through A (real in A) is no longer under
    `l`. The kept path (A) still holds it, so the candidate is confirmed with one fresh resolve of `l` (B): not attached, on the receipt and the
    record. Red on 69f2f08: ['l']."""
    eng, A, B, link = _link_world(ws, monkeypatch)
    T = _T()
    _swap(ws.base, link, B)
    w = T.dispatch("lts_add_property", {"path": str(A / "l.lts"), "node_id": "L-N", "key": "k", "value": "viaA"}, tools=eng)
    assert w["success"] and "k: viaA" in (A / "l.lts").read_text()
    assert w["root_membership"]["corpora"] == [] and w["root_membership"]["inside_corpus"] is False, w["root_membership"]
    assert _recs(ws, "l.lts")[-1]["corpora"] == []
    eng.store.kernel.tick()
    w2 = T.dispatch("lts_add_property", {"path": str(A / "l.lts"), "node_id": "L-N", "key": "k", "value": "viaA2"}, tools=eng)
    assert w2["root_membership"]["corpora"] == [], "after the poll too"


def test_a_write_through_the_link_in_the_window_attaches_to_every_corpus_that_now_holds_it(ws, monkeypatch):
    """F5, the reader's second window case. Corpus `l` is a link to A and corpus `o` is the root B. The link is swapped to B and a write typed through
    the link (real in B) is under both. Red on 69f2f08: ['o'] only."""
    base = ws.base
    A, B = base / "dirA", base / "dirB"
    for d, t in ((A, "a"), (B, "b")):
        d.mkdir()
        (d / "l.lts").write_text(_LDOC.format(t), encoding="utf-8")
    (B / "o.lts").write_text(_LDOC.format("o").replace("L-", "O-").replace("x.l", "x.o"), encoding="utf-8")
    link = base / "link"
    link.symlink_to(A)
    ws.registry_path.write_text(json.dumps({"l": {"roots": [{"path": str(link)}], "domain_suffix": "x.l"},
                                            "o": {"roots": [{"path": str(B)}], "domain_suffix": "x.o"}}))
    for k, v in ws.env({"LTS_ROOT_DEADLINE_S": "0.4"}).items():
        monkeypatch.setenv(k, v)
    from factlattice_engine.server import build_tools
    eng = build_tools(watch=False, require_corpora=True)
    _swap(base, link, B)
    w = _T().dispatch("lts_add_property", {"path": str(link / "o.lts"), "node_id": "O-N", "key": "k", "value": "v"}, tools=eng)
    assert w["success"] and w["root_membership"]["corpora"] == ["l", "o"], w["root_membership"]["corpora"]
    assert _recs(ws, "o.lts")[-1]["corpora"] == ["l", "o"]


# ---- F6: a path-like is a path -----------------------------------------------------------------------------------------
def test_a_pathlib_path_is_refused_under_a_marked_root_like_a_string(ws, monkeypatch):
    """F6. A `pathlib.Path` reached the writer unchecked (`isinstance(path, str)`), so the lock and the root were read. Red on 69f2f08."""
    eng = _engine(ws, monkeypatch)
    from factlattice_engine import authoring as A
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            _mark_b1(eng)
            hits = stall.hit_count
            r = _finished(run_limited(lambda: A.add_property(ws.file("b1"), "B1-N", "k", "v", store=eng.store), LIMIT_S), stall)
            assert r["refused"] is True and r["reason"] == CODE_UNRESPONSIVE and stall.hit_count == hits
            ctl = _finished(run_limited(lambda: A.add_property(ws.file("a1"), "A1-N", "k", "v", store=eng.store), LIMIT_S), stall)
            assert ctl["success"] and ctl["membership_partial"] == {"marked_count": 1, "unconfirmed_count": 0}, "control: a Path to a healthy file writes"
        finally:
            stall.release()
            time.sleep(0.3)


# ---- F7: every public writer on the adapter is refused -------------------------------------------------------------------
def _public_writers():
    import inspect

    from factlattice_engine import authoring as A
    out = {}
    for name, fn in inspect.getmembers(A, inspect.isfunction):
        if name.startswith("_") or fn.__module__ != A.__name__:
            continue
        params = list(inspect.signature(fn).parameters)
        if params and params[0] == "path" and "store" in params:
            out[name] = fn
    return out


def test_every_public_writer_of_the_adapter_is_refused_under_a_marked_root(tmp_path, monkeypatch):
    """F7. The writers are FOUND by introspection (a public function of the adapter whose first parameter is `path` and which takes `store`), not
    listed by hand, and must be exactly the tools the adapter declares; each is called with dummy arguments and a marked root and must be refused
    root-unresponsive. Dropping `@writes_scoped` from any one lets it run and fail for another reason. A new writer without the scope fails here."""
    import inspect

    from factlattice.rootview import RootFact, RootView
    from factlattice_engine import authoring as A
    from factlattice_engine import membership
    writers = _public_writers()
    declared = {n[len("lts_"):] for n in A._TOOL_NAMES if n != "lts_property_loss"}
    assert set(writers) == declared, f"the adapter's writers by introspection {sorted(writers)} are not its declared tools {sorted(declared)}"
    root = tmp_path / "drive"
    root.mkdir()
    view = RootView([RootFact(str(root), str(root), "unresponsive")])
    monkeypatch.setattr(membership, "snapshot", lambda store=None: view)
    dummy = {"node_id": "N", "key": "k", "value": "v", "title": "T", "line": 1, "new_key": "k2"}
    for name, fn in writers.items():
        args = [dummy[p] for p in list(inspect.signature(fn).parameters)[1:]
                if inspect.signature(fn).parameters[p].default is inspect.Parameter.empty and inspect.signature(fn).parameters[p].kind
                is inspect.Parameter.POSITIONAL_OR_KEYWORD]
        r = fn(str(root / "f.lts"), *args)
        assert r.get("reason") == CODE_UNRESPONSIVE and r.get("refused") is True, f"{name}: {r.get('reason')}"
        r = fn(root / "f.lts", *args)
        assert r.get("reason") == CODE_UNRESPONSIVE, f"{name} with a Path: {r.get('reason')}"


# ---- F10: the seat path keeps the root's real path ------------------------------------------------------------------------
def test_a_re_seat_after_a_link_swap_keeps_the_new_real_path(ws, monkeypatch):
    """F10. `seat_partition` (the heal's re-seat) publishes the root's real path too, replacing the old one. Passes on 69f2f08 (it pins the seat path
    against a mutant that keeps the first answer)."""
    eng, A, B, link = _link_world(ws, monkeypatch)
    k = eng.store.kernel
    part = next(p for p in eng.store.partitions() if p.corpus_id == "l")
    assert k.root_real(str(link)) == os.path.realpath(A)
    _swap(ws.base, link, B)
    k.seat_partition(part)
    assert k.root_real(str(link)) == os.path.realpath(B)


def test_a_resolve_on_the_kernels_slot_does_not_settle_a_root_that_was_never_read(tmp_path):
    """F3. The bounded resolve shares the kernel's slot for a registered root; a resolve proves the root answers a realpath, not that its content was
    read, so it must not turn a PENDING root OK. A root only the call asked about (no kernel slot) gets an auxiliary slot, which does settle."""
    from factlattice_engine import boundedfs, rootstate
    reg = rootstate.RootRegistry(1.0)
    d = tmp_path / "r"
    d.mkdir()
    from factlattice.rootview import lexical
    kslot = reg.slot(lexical(d))                       # a registered root, never read
    assert kslot.state() == rootstate.PENDING
    f = boundedfs.resolve_roots([str(d)], reg)[0]
    assert f.real == os.path.realpath(d) and kslot.state() == rootstate.PENDING, "answered a realpath, still never read"
    other = tmp_path / "o"
    other.mkdir()
    g = boundedfs.resolve_roots([str(other)], reg)[0]
    assert g.state == rootstate.OK and reg.slot(lexical(other), create=False) is None, "an auxiliary slot that answered is pruned"
    assert lexical(d) in reg.root_paths()


def test_a_pathlib_path_spelled_through_the_link_in_the_window_attaches_like_a_string(ws, monkeypatch):
    """F6: the typed path that confirms a declared spelling is the `fspath` of what the caller passed, so a Path attaches in the swap window exactly
    as a string does."""
    eng, A_dir, B, link = _link_world(ws, monkeypatch)
    from factlattice_engine import authoring as A
    _swap(ws.base, link, B)
    r = A.add_property(link / "l.lts", "L-N", "k", "v", store=eng.store)
    assert r["success"] and r["root_membership"]["corpora"] == ["l"], r["root_membership"]


# ======================================================================================================================
# round 5 (delta read of dac7f55)
# ======================================================================================================================
def test_a_healthy_write_never_waits_for_a_slow_but_progressing_poll(ws, monkeypatch, record_property):
    """P1-1. A poll of a slow root that keeps making progress runs for seconds; the write's confirm used to queue on the root's own slot behind it
    (7.3 s on dac7f55 against 0.0 to 0.3 s before). The confirm runs on its own auxiliary slot and never queues behind root work. Slow-but-answering
    model: 40 extra files, each gated call under the root sleeps 0.05 s on pool workers only. Load is recorded beside the figures."""
    from tests.stall_harness import _DOC
    for i in range(40):
        (ws.root("a1") / f"extra{i}.lts").write_text(_DOC.format(c=f"a1e{i}", u=f"A1E{i}"), encoding="utf-8")
    eng = _engine(ws, monkeypatch)
    T = _T()
    k = eng.store.kernel
    assert T.dispatch("lts_add_property", _a1_props(ws, "ctl", "x"), tools=eng)["success"], "control: a write before the slow poll"
    k.tick()
    time.sleep(0.3)
    T.dispatch("lts_add_property", _a1_props(ws, "touch", "x"), tools=eng)       # a change for the poll to chew on
    with RootStall(ws.root("a1"), cap_s=120, slow_s=0.05, engaged_fn=lambda: threading.current_thread().name.startswith("lts-root-pool")) as stall:
        done = {}

        def tk():
            t = time.monotonic()
            k.tick()
            done["tick"] = time.monotonic() - t
        th = threading.Thread(target=tk, daemon=True)
        th.start()
        time.sleep(0.5)
        t0 = time.monotonic()
        out = run_limited(lambda: T.dispatch("lts_add_property", _a1_props(ws, "during", "v"), tools=eng), 60, name="w")
        lat = time.monotonic() - t0
        polling = th.is_alive()
        th.join(90)
    record_property("load", json.dumps({"loadavg": os.getloadavg(), "write_s": round(lat, 2), "poll_s": round(done.get("tick") or 0, 2)}))
    assert out.done and out.value["success"], out.value
    assert lat < 2.0, f"the write waited {lat:.2f}s for a poll of its root that was still progressing (load {os.getloadavg()[0]:.1f})"
    assert polling and done["tick"] > 1.5, f"the poll must still have been running when the write ended (it took {done.get('tick')}s): the setup did not make a slow poll"


def test_a_slow_but_answering_root_is_never_marked_by_a_confirm(ws, monkeypatch, record_property):
    """P1-1 guard (green on 6fad325 and after): each gated call under the root sleeps 0.05 s on pool workers, far under the 0.4 s root deadline, so a
    confirm's one realpath answers. The root is never marked, the write attaches and carries no membership_partial. Load recorded."""
    eng = _engine(ws, monkeypatch)
    k = eng.store.kernel
    T = _T()
    assert T.dispatch("lts_add_property", _a1_props(ws, "ctl", "x"), tools=eng)["success"]
    with RootStall(ws.root("a1"), cap_s=60, slow_s=0.05, engaged_fn=lambda: threading.current_thread().name.startswith("lts-root-pool")) as stall:
        for i in range(5):
            r = T.dispatch("lts_add_property", _a1_props(ws, "slow", str(i)), tools=eng)
            assert r["success"] and r["root_membership"]["corpora"] == ["a1"] and "membership_partial" not in r, r.get("membership_partial")
            assert k.roots.state_of(os.path.abspath(ws.root("a1"))) == "ok", "a slow root that answers is not marked"
        assert stall.hit_count > 0, "the confirm did go through the slow model"
    record_property("load", json.dumps({"loadavg": os.getloadavg()}))


def test_concurrent_confirms_of_one_root_share_one_job_and_all_get_its_answer(tmp_path, monkeypatch):
    """D, unit: a confirm that finds one in flight for that root JOINS it (waits for its result) and starts no second job or thread."""
    from factlattice_engine import boundedfs, rootstate
    reg = rootstate.RootRegistry(5.0)
    d = tmp_path / "r"
    d.mkdir()
    gate, started, calls = threading.Event(), threading.Event(), []
    real = boundedfs.realpath_progress

    def slow(p):
        calls.append(p)
        started.set()
        gate.wait(3)
        return real(p)
    monkeypatch.setattr(boundedfs, "realpath_progress", slow)
    got = {}
    ths = [threading.Thread(target=lambda i=i: got.update({i: boundedfs.confirm_real(str(d), reg)}), daemon=True) for i in range(6)]
    ths[0].start()
    assert started.wait(3)
    for t in ths[1:]:
        t.start()
    time.sleep(0.2)
    threads = threading.active_count()
    gate.set()
    for t in ths:
        t.join(5)
    assert len(calls) == 1, f"one confirm job for six writers: {len(calls)}"
    assert set(got.values()) == {os.path.realpath(d)} and len(got) == 6
    assert boundedfs.confirm_real(str(d), reg) == os.path.realpath(d) and len(calls) == 2, "a later confirm is a new job"


def test_a_slot_made_aux_by_a_failed_corpus_write_is_a_root_again_once_the_corpus_is_registered(ws, monkeypatch):
    """P1-2. Failed corpus, a write under its root (an auxiliary slot), then register_corpus of the fixed corpus: the kernel's slot request must clear
    `aux`, or `root_paths()`/`root_facts()` hide the root and a later hang under it is neither refused nor fast. Red on dac7f55."""
    f1 = ws.base / "c" / "f1"
    f1.mkdir(parents=True)
    (f1 / "f1.lts").write_text(ws.file("a1").read_text().replace("A1", "F1"), encoding="utf-8")
    dang = f1 / "dangling.lts"
    os.symlink(str(f1 / "nowhere.lts"), str(dang))
    ws.registry["f1"] = {"roots": [{"path": str(f1)}], "domain_suffix": "x.f1"}
    ws.registry_path.write_text(json.dumps(ws.registry), encoding="utf-8")
    eng = _engine(ws, monkeypatch)
    T = _T()
    k = eng.store.kernel
    assert "f1" in eng.store._failed_corpora
    # the auxiliary slot the write makes: held open by a resolve that is still running, so pruning cannot hide the sequence
    gate = threading.Event()
    k.roots.slot(os.path.abspath(f1), aux=True)                 # what a write under the failed corpus's root leaves while its resolve is unsettled
    r = T.dispatch("lts_add_property", {"path": str(f1 / "f1.lts"), "node_id": "F1-N", "key": "k", "value": "x"}, tools=eng)
    assert r["success"]
    os.unlink(dang)
    assert eng.register_corpus("f1").get("success"), "the fixed corpus registers"
    assert "f1" in eng.store.corpus_ids()
    assert os.path.abspath(f1) in k.roots.root_paths() and any(f.declared == os.path.abspath(f1) for f in k.root_facts()), "its root is a root again"
    with RootStall(f1, cap_s=30) as stall:
        try:
            named = T.dispatch("lts_find", {"corpus": "f1", "text": "x"}, tools=eng)
            assert named.get("reason") == CODE_UNRESPONSIVE, named.get("reason")
            hits = stall.hit_count
            r2 = _finished(_write(eng, "lts_add_property", {"path": str(f1 / "f1.lts"), "node_id": "F1-N", "key": "k2", "value": "y"}), stall)
            assert r2["refused"] is True and r2["reason"] == CODE_UNRESPONSIVE and stall.hit_count == hits
        finally:
            stall.release()
            time.sleep(0.5)
            gate.set()


def test_a_slot_request_for_a_registered_root_clears_aux():
    """P1-2, unit."""
    from factlattice_engine import rootstate
    reg = rootstate.RootRegistry(1.0)
    s = reg.slot("/x", aux=True)
    assert s.aux is True and "/x" not in reg.root_paths()
    assert reg.slot("/x") is s and s.aux is False and "/x" in reg.root_paths()
    assert reg.slot("/x", aux=True) is s and s.aux is False, "a later auxiliary request does not demote a root"
    assert reg.slot("/x", create=False) is s and s.aux is False


def test_an_nfd_spelling_of_an_nfc_root_is_refused(tmp_path):
    """P2-1. APFS treats a decomposed and a composed spelling as one file."""
    import unicodedata

    from factlattice.rootview import RootFact, RootView, fold
    from factlattice_engine import authoring as A
    nfc = "Café"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    v = RootView([RootFact(str(tmp_path / nfc), str(tmp_path / nfc), "unresponsive")])
    assert A.marked_refusal(v, str(tmp_path / nfd / "f.lts"))["reason"] == CODE_UNRESPONSIVE
    assert A.marked_refusal(v, str(tmp_path / nfc.upper() / "f.lts"))["reason"] == CODE_UNRESPONSIVE
    assert fold(str(tmp_path / nfd)) == fold(str(tmp_path / nfc))


@pytest.mark.parametrize("err,unknown", [("EIO", True), ("ETIMEDOUT", True), ("ESTALE", True), ("EACCES", True), ("ENOENT", False)])
def test_a_same_place_stat_that_fails_with_anything_but_not_found_is_unknown(tmp_path, monkeypatch, err, unknown):
    """P2-2. Only an answered 'no' (or not found) means another file; any other OSError is unknown, as `_present` treats it. Red on dac7f55 for the
    four unknown errnos."""
    import errno

    from factlattice import ledger, rootview
    table, state, real, owner = _same_place_world(tmp_path, hardlink=True)
    orig = os.path.samefile

    def boom(a, b):
        if str(a) == owner:
            raise OSError(getattr(errno, err), os.strerror(getattr(errno, err)))
        return orig(a, b)
    monkeypatch.setattr(os.path, "samefile", boom)
    with rootview.use(rootview.RootView([], bounded=lambda key, fn, default: fn())):
        counted, left, unk = ledger._legacy(real, state, table)
    if unknown:
        assert unk is True and left == ["old-location-unknown"] and counted == []
    else:
        assert unk is False and left == ["old-spelling-belongs-to-another-file"]


def test_every_bounded_read_a_kernel_process_makes_goes_to_the_kernels_registry(ws, monkeypatch):
    """P2-3, one route each: the failed-corpus resolve in `snapshot`, the view's resolver, the view's bounded call; and the op slot is auxiliary."""
    from factlattice_engine import boundedfs, membership
    f1 = ws.base / "c" / "f1"
    f1.mkdir(parents=True)
    (f1 / "f1.lts").write_text(ws.file("a1").read_text().replace("A1", "F1"), encoding="utf-8")
    os.symlink(str(f1 / "nowhere.lts"), str(f1 / "dangling.lts"))
    ws.registry["f1"] = {"roots": [{"path": str(f1)}], "domain_suffix": "x.f1"}
    ws.registry_path.write_text(json.dumps(ws.registry), encoding="utf-8")
    eng = _engine(ws, monkeypatch)
    kreg = eng.store.kernel.roots
    seen = {"roots": [], "one": [], "call": []}
    o_roots, o_one, o_call = boundedfs.resolve_roots, boundedfs.resolve_one, boundedfs.bounded_call
    monkeypatch.setattr(boundedfs, "resolve_roots", lambda paths, reg=None, untrusted=(): (seen["roots"].append(reg), o_roots(paths, reg, untrusted))[1])
    monkeypatch.setattr(boundedfs, "resolve_one", lambda path, reg=None: (seen["one"].append(reg), o_one(path, reg))[1])
    monkeypatch.setattr(boundedfs, "bounded_call", lambda key, fn, default, reg=None: (seen["call"].append(reg), o_call(key, fn, default, reg))[1])
    view = membership.snapshot(eng.store)                       # the failed corpus's root is resolved here (N08)
    assert seen["roots"] and all(r is kreg for r in seen["roots"]), "snapshot's resolve of a failed corpus's root"
    view.real(str(ws.base / "nowhere-known"))                  # a path the view does not know (N09)
    assert seen["one"] and all(r is kreg for r in seen["one"]), "the view's resolver"
    assert view.bounded("somewhere", lambda: 7, 0) == 7         # (N10)
    assert seen["call"] and all(r is kreg for r in seen["call"]), "the view's bounded call"


def test_the_op_slot_of_a_bounded_call_is_auxiliary_and_idle_aux_slots_are_pruned(tmp_path):
    """P2-3 (op slots are aux) and P3 (no growth with the keys asked about: 50 keys gave 50 slots)."""
    from factlattice_engine import boundedfs, rootstate
    reg = rootstate.RootRegistry(2.0)
    seen = {}

    def fn():
        slot = reg.slot("op:" + os.path.abspath("/k"), create=False)
        seen["aux"] = slot is not None and slot.aux
        seen["listed"] = "op:" + os.path.abspath("/k") in reg.root_paths()
        return 1
    assert boundedfs.bounded_call("/k", fn, 0, reg) == 1
    assert seen == {"aux": True, "listed": False}
    for i in range(50):
        boundedfs.bounded_call(f"/key{i}", lambda: 1, 0, reg)
        boundedfs.confirm_real(str(tmp_path / f"c{i}"), reg)
    assert len(reg.paths()) == 0, f"idle auxiliary slots are pruned: {len(reg.paths())} left"


def test_registered_roots_names_a_failed_corpus_root(ws, monkeypatch):
    """P3 (M17). `registered_roots` itself, which the miss-log guard and `roots_checked` read, carries the root of a corpus that failed to build."""
    from factlattice_engine import membership
    f1 = ws.base / "c" / "f1"
    f1.mkdir(parents=True)
    (f1 / "f1.lts").write_text(ws.file("a1").read_text().replace("A1", "F1"), encoding="utf-8")
    os.symlink(str(f1 / "nowhere.lts"), str(f1 / "dangling.lts"))
    ws.registry["f1"] = {"roots": [{"path": str(f1)}], "domain_suffix": "x.f1"}
    ws.registry_path.write_text(json.dumps(ws.registry), encoding="utf-8")
    eng = _engine(ws, monkeypatch)
    roots, src = membership.registered_roots(eng.store)
    assert src == "store" and roots.get("f1") == [os.path.realpath(f1)] and "a1" in roots, roots


def test_a_confirm_marks_nothing_creates_no_root_slot_and_its_stuck_slot_recovers(tmp_path, monkeypatch):
    """D/E, unit. An error from the call, a stuck call, and an overrun mark nothing and create no root slot (R21); the confirm slot has a recovery
    (R19), so when the stuck call returns the next confirm answers."""
    from factlattice.rootview import lexical
    from factlattice_engine import boundedfs, rootstate
    reg = rootstate.RootRegistry(0.3)
    d = tmp_path / "r"
    d.mkdir()
    root = reg.slot(lexical(d))
    unknown = tmp_path / "u"
    unknown.mkdir()
    monkeypatch.setattr(boundedfs, "realpath_progress", lambda p: (_ for _ in ()).throw(OSError(5, "EIO")))
    assert boundedfs.confirm_real(str(d), reg) is None and root.state() == rootstate.PENDING, "an error marks nothing"
    gate = threading.Event()
    from factlattice_engine.rootstate import realpath_progress as true_rp
    monkeypatch.setattr(boundedfs, "realpath_progress", lambda p: (gate.wait(5), true_rp(p))[1])
    t0 = time.monotonic()
    assert boundedfs.confirm_real(str(d), reg) is None and time.monotonic() - t0 < 2.0, "no answer within the deadline"
    assert root.state() == rootstate.PENDING, "and the root is not marked"
    assert boundedfs.confirm_real(str(d), reg) is None, "the stuck slot answers None at once"
    assert lexical(unknown) not in reg.paths(), "a root nobody registered gets no root slot from a confirm"
    assert all(p.startswith("confirm:") or p == lexical(d) for p in reg.paths()), reg.paths()
    assert lexical(d) in reg.root_paths() and not any(p.startswith("confirm:") for p in reg.root_paths())
    gate.set()
    end = time.monotonic() + 5
    out = None
    while time.monotonic() < end:
        out = boundedfs.confirm_real(str(d), reg)
        if out:
            break
        time.sleep(0.1)
    assert out == os.path.realpath(d), "the stuck call returned: the confirm slot recovered"


def test_a_confirm_is_a_foreground_job_not_queued_behind_busy_background_workers(tmp_path):
    """E (R05). Background workers all busy on other roots' long jobs do not delay a confirm: it is foreground and gets a thread at once."""
    from factlattice_engine import boundedfs, rootstate
    reg = rootstate.RootRegistry(10.0, pool_size=2)
    d = tmp_path / "r"
    d.mkdir()
    gate = threading.Event()
    jobs = []
    for i in range(4):
        sl = reg.slot(str(tmp_path / f"busy{i}"))
        jobs.append(sl.submit("poll", lambda: (gate.wait(8), None)[1], coalesce=False, foreground=False))
    time.sleep(0.5)
    t0 = time.monotonic()
    got = boundedfs.confirm_real(str(d), reg)
    took = time.monotonic() - t0
    gate.set()
    assert got == os.path.realpath(d) and took < 1.5, f"the confirm waited {took:.2f}s behind background work"


@copy_only
@pytest.mark.parametrize("slow", [0.15, 0.25])
def test_a_slow_multi_component_root_that_answers_every_call_is_never_marked_and_its_writes_attach(ws, monkeypatch, slow, record_property):
    """D(1). `os.path.realpath` is one lstat per component and used to report progress only at its end, so a root whose EVERY call answered (0.15 s
    each against a 0.4 s deadline, two components below the stalled directory) looked like one with no progress. The confirm resolves component by
    component and reports progress after each: the write attaches, the root stays ok, nothing is partial. Load recorded."""
    eng = _engine(ws, monkeypatch)
    k = eng.store.kernel
    T = _T()
    assert T.dispatch("lts_add_property", _a1_props(ws, "ctl", "x"), tools=eng)["success"]
    with RootStall(ws.root("a1").parent, cap_s=60, slow_s=slow, engaged_fn=lambda: threading.current_thread().name.startswith("lts-root-pool")) as stall:
        t0 = time.monotonic()
        out = [run_limited(lambda i=i: T.dispatch("lts_add_property", _a1_props(ws, "slow", str(i)), tools=eng), 30, name=f"w{i}") for i in range(3)]
        record_property("load", json.dumps({"loadavg": os.getloadavg(), "slow_s": slow, "wall_s": round(time.monotonic() - t0, 2)}))
        for o in out:
            r = o.value or {}
            assert o.done and r.get("success") and r["root_membership"]["corpora"] == ["a1"] and "membership_partial" not in r, r.get("reason") or r.get("membership_partial")
        assert stall.hit_count >= 2, "the confirm made several gated calls (one per component)"
    assert k.roots.state_of(os.path.abspath(ws.root("a1"))) == "ok" and k.root_mark(next(p for p in eng.store.partitions() if p.corpus_id == "a1")) is None


@copy_only
def test_concurrent_writers_to_one_root_share_one_confirm_and_all_attach(ws, monkeypatch):
    """D(3), the dia2_conc shape. Six writers to six files of one root overlap a slow confirm (0.2 s per gated call on pool workers): they share ONE
    confirm job (one gated call, not six), and every write attaches."""
    from tests.stall_harness import _DOC
    eng = _engine(ws, monkeypatch)
    T = _T()
    n = 6
    for i in range(n):
        (ws.root("a1") / f"e{i}.lts").write_text(_DOC.format(c=f"e{i}", u=f"E{i}"), encoding="utf-8")
    res, bar = {}, threading.Barrier(n)

    def worker(i):
        bar.wait()
        res[i] = T.dispatch("lts_add_property", {"path": str(ws.root("a1") / f"e{i}.lts"), "node_id": f"E{i}-N", "key": "k", "value": "v"}, tools=eng)
    with RootStall(ws.root("a1"), cap_s=60, slow_s=0.2, engaged_fn=lambda: threading.current_thread().name.startswith("lts-root-pool")) as stall:
        ths = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        [t.start() for t in ths]
        [t.join(30) for t in ths]
        hits = stall.hit_count
    assert len(res) == n and all(r["success"] and r["root_membership"]["corpora"] == ["a1"] and "membership_partial" not in r for r in res.values()), \
        [r.get("reason") for r in res.values()]
    assert hits <= 2, f"{hits} gated confirm calls for {n} concurrent writers: they must share one (a straggler may start a second)"


def test_receipt_and_record_always_agree_under_twelve_concurrent_writers(ws, monkeypatch):
    """A, the dia2_conc shape (12 threads, 8 writes, a root that is slow but answers). Membership is evaluated once per write, so the receipt's corpora
    and membership_partial are the ledger record's, always. Red on 78282ff (40 of 96 disagreed there at the reader's load)."""
    from tests.stall_harness import _DOC
    eng = _engine(ws, monkeypatch)
    T = _T()
    nthreads, nwrites = 12, 8
    for i in range(nthreads):
        (ws.root("a1") / f"e{i}.lts").write_text(_DOC.format(c=f"e{i}", u=f"E{i}"), encoding="utf-8")
    res, lock, bar = [], threading.Lock(), threading.Barrier(nthreads)

    def worker(i):
        bar.wait()
        for j in range(nwrites):
            r = T.dispatch("lts_add_property", {"path": str(ws.root("a1") / f"e{i}.lts"), "node_id": f"E{i}-N", "key": f"k{j}", "value": "v"}, tools=eng)
            with lock:
                res.append((i, r))
    with RootStall(ws.root("a1"), cap_s=120, slow_s=0.03, engaged_fn=lambda: threading.current_thread().name.startswith("lts-root-pool")):
        ths = [threading.Thread(target=worker, args=(i,)) for i in range(nthreads)]
        [t.start() for t in ths]
        [t.join(120) for t in ths]
    assert len(res) == nthreads * nwrites
    by = {}
    for r in ledger_records(ws):
        by[(r["path_real"], r["ledger_seq"])] = r
    bad = 0
    for i, r in res:
        if not r.get("success"):
            assert r.get("reason") == "membership-unconfirmed", r.get("reason")      # a refusal is allowed; a wrong or silently partial record is not
            continue
        rec = by[(os.path.realpath(ws.root("a1") / f"e{i}.lts"), r["ledger"]["seq"])]
        if rec.get("corpora") != r["root_membership"]["corpora"] or rec.get("membership_partial") != r.get("membership_partial"):
            bad += 1
        assert r["root_membership"]["corpora"] == ["a1"] or r.get("membership_partial"), "silently empty"
    assert bad == 0, f"{bad} writes whose receipt and ledger record disagree"


def test_a_partial_membership_makes_the_edge_context_incomplete_and_gives_no_outside_warning(tmp_path, monkeypatch):
    """B, the dia2_edge shape. A profile-registered corpus, an edge key removed, a confirm in flight (stuck) for the corpus's root: the write that
    removes the edge refuses as an incomplete edge context does, or (when the confirm's root holds the file) is refused membership-unconfirmed, and in
    NO case is the edge removed. Control: with no confirm in flight the removal refuses on the profile's own edge guard. Red on 78282ff."""
    import json as _json

    from factlattice.rootview import lexical
    from factlattice_engine import boundedfs
    from tests.test_rename_remove_round2 import DOC, PROFILE, props
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    monkeypatch.setenv("LTS_ROOT_DEADLINE_S", "0.4")
    boundedfs._reset(0.4)
    prof = tmp_path / "cmp-profile.json"
    prof.write_text(_json.dumps(PROFILE))
    (tmp_path / "withprof").mkdir()
    f = tmp_path / "withprof" / "c.lts"
    f.write_text(DOC)
    reg = {"withprof": {"roots": [{"path": str(tmp_path / "withprof"), "area": "specs"}], "domain_suffix": "a", "profile": str(prof)}}
    (tmp_path / "reg.json").write_text(_json.dumps(reg))
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(tmp_path / "reg.json"))
    monkeypatch.chdir(tmp_path)
    T = _T()
    args = {"path": str(f), "node_id": "CP-ONE", "key": "documents"}
    ctl = T.dispatch("lts_remove_property", dict(args), tools=None)
    assert ctl.get("refused") and ctl["evidence"]["refusal"] == "edge_change_refused", "control: the profile's edge guard refuses the removal"
    gate = threading.Event()
    reg_ = boundedfs.registry()
    slot = reg_.slot("confirm:" + lexical(tmp_path / "withprof"), aux=True)
    slot.recover = lambda: None
    slot.submit("confirm", lambda: (gate.wait(8), None)[1], coalesce=False, foreground=True)
    time.sleep(0.2)
    try:
        out = T.dispatch("lts_remove_property", dict(args), tools=None)
    finally:
        gate.set()
    assert out.get("refused") and not out.get("written"), "the edge-removal guard must not be bypassed while a confirm is in flight"
    assert "documents" in props(f)["CP-ONE"], "the edge is still in the file"
    assert out["evidence"]["refusal"] in ("membership-unconfirmed", "edge-context-incomplete")


def test_a_partial_membership_gives_no_outside_every_root_warning(ws, monkeypatch):
    """B. A healthy write beside a marked root: its corpora may be incomplete, so it must not say 'outside every root'. Here the file IS inside a1, so
    the corpora are right; the warning and the false inside_corpus False are for a result with no hit, which the partial result must not give."""
    from factlattice.rootview import RootFact, RootView
    from factlattice_engine import membership
    v = RootView([RootFact("/nowhere/marked", "/nowhere/marked", "unresponsive")])
    class P:
        corpus_id, root_paths = "c", ["/nowhere/other"]
    class Store:
        def partitions(self):
            return [P()]

        def snapshot_all(self):
            return [P()], {}, {}, {}
    from factlattice import rootview
    with rootview.use(v):
        out = membership.check("/tmp/not-under-any-root/f.lts", store=Store())
    assert out["corpora"] == [] and out["membership_partial"] == {"marked_count": 1, "unconfirmed_count": 0}
    assert out["warning"] is None and out["inside_corpus"] is None, "partial: unknown, not 'outside every root'"


@copy_only
def test_realpath_progress_answers_like_os_path_realpath_and_reports_progress_per_component(tmp_path, monkeypatch):
    """P1-2. The component-by-component resolve returns what `os.path.realpath` returns on links (relative, absolute, chained), `..`, a missing tail and
    a loop, and reports progress after each disk call, not once at the end."""
    from factlattice_engine import rootstate
    base = tmp_path / "b"
    (base / "real" / "sub").mkdir(parents=True)
    (base / "real" / "sub" / "f.lts").write_text("x", encoding="utf-8")
    os.symlink("real", base / "rel")                                   # relative link
    os.symlink(str(base / "real" / "sub"), base / "abs")                # absolute link
    os.symlink("abs", base / "chain")                                   # a link to a link
    os.symlink("loop2", base / "loop1")
    os.symlink("loop1", base / "loop2")
    for p in (base / "rel" / "sub" / "f.lts", base / "abs" / "f.lts", base / "chain" / "f.lts", base / "real" / "sub" / ".." / "sub" / "f.lts",
              base / "rel" / "nope" / "deeper.lts", base / "real" / "." / "sub", base / "loop1" / "x", base, base / "rel" / ".." / "real"):
        assert rootstate.realpath_progress(str(p)) == os.path.realpath(p), p
    ticks = []
    monkeypatch.setattr(rootstate, "progress", lambda: ticks.append(1))
    rootstate.realpath_progress(str(base / "rel" / "sub" / "f.lts"))
    assert len(ticks) >= 4, f"progress reported {len(ticks)} times for a path of several components and a link"


@pytest.mark.parametrize("spelling", ["real-path", "alias-link"])
def test_a_write_typed_by_another_spelling_that_a_stuck_confirm_root_could_hold_is_refused(ws, monkeypatch, spelling):
    """F5. The rule is not 'the declared spelling as typed': ANY unconfirmed candidate that could hold the file, by its kept real path or by its declared
    spelling, refuses the write membership-unconfirmed. Root `l` is a link, swapped; its confirm is stuck. A write typed by the file's REAL (old) path,
    or through an alias link nobody registered, is a candidate of `l` by its kept path. It used to proceed partial. Red on 9350c34."""
    eng, A, B, link = _link_world(ws, monkeypatch)
    alias = ws.base / "alias"
    alias.symlink_to(A)
    _swap(ws.base, link, B)
    typed = (A if spelling == "real-path" else alias) / "l.lts"
    before = (A / "l.lts").read_bytes()
    with RootStall(link, cap_s=30, engaged_fn=lambda: threading.current_thread().name.startswith("lts-root-pool")) as stall:
        try:
            w = _finished(_write(eng, "lts_add_property", {"path": str(typed), "node_id": "L-N", "key": "stamp", "value": "viaA"}), stall)
            assert w["refused"] is True and w["reason"] == "membership-unconfirmed", (w.get("reason"), w.get("membership_partial"))
            assert (A / "l.lts").read_bytes() == before and not _recs(ws, "l.lts")
        finally:
            stall.release()
            time.sleep(0.5)


def test_membership_is_evaluated_once_per_write(ws, monkeypatch):
    """A. One confirm per write for one candidate root, however many places (ledger record, edge context, receipt) read the result. Deterministic
    form of the 12-thread test. Red on 78282ff: the verdict was recomputed three times."""
    from factlattice_engine import boundedfs
    eng = _engine(ws, monkeypatch)
    calls = []
    orig = boundedfs.confirm_real
    monkeypatch.setattr(boundedfs, "confirm_real", lambda path, reg=None, not_before=None: (calls.append(path), orig(path, reg, not_before))[1])
    for tool, args in (("lts_add_property", _a1_props(ws, "once", "v")),
                       ("lts_rename_property", {"path": str(ws.file("a1")), "node_id": "A1-N", "key": "once", "new_key": "once2"}),
                       ("lts_remove_property", {"path": str(ws.file("a1")), "node_id": "A1-N", "key": "once2"})):
        calls.clear()
        r = _T().dispatch(tool, args, tools=eng)
        assert r["success"], (tool, r.get("reason"))
        assert len(calls) == 1, f"{tool}: {len(calls)} confirms for one write"


@copy_only
def test_the_probes_own_resolve_of_a_root_reports_progress_per_component(tmp_path, monkeypatch):
    """P1-2. `FreshnessKernel._observe` resolves the root through `realpath_progress`, so a probe of a slow multi-component root reports progress
    after each component too (it used to report once, at the end). Counted on a path five components deep."""
    from factlattice_engine import rootstate
    from factlattice_engine.kernel import FreshnessKernel
    deep = tmp_path / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    ticks = []
    monkeypatch.setattr(rootstate, "progress", lambda: ticks.append(1))
    FreshnessKernel()._observe(str(deep), {None})
    assert len(ticks) >= 5, f"{len(ticks)} progress reports for a five-component root"


def test_a_healthy_edge_change_beside_a_marked_root_refuses_as_an_incomplete_edge_context(ws, monkeypatch, tmp_path):
    """B. A corpus with a registered profile, a root elsewhere marked: which corpora hold the file is not fully known, so the edge context is incomplete
    and removing a profile edge key refuses `edge-context-incomplete` (unless `allow_edge_change`). Control: with every root answering the same removal
    refuses on the profile's own edge guard. Red on 78282ff (the write went through the 'outside every corpus' branch and guarded nothing)."""
    from tests.test_rename_remove_round2 import DOC, PROFILE, props
    prof = tmp_path / "cmp-profile.json"
    prof.write_text(json.dumps(PROFILE))
    ws.file("a1").write_text(DOC, encoding="utf-8")
    reg = dict(ws.registry)
    reg["a1"] = {"roots": [{"path": str(ws.root("a1")), "area": "specs"}], "domain_suffix": "x.a1", "profile": str(prof)}
    eng = _engine(ws, monkeypatch, registry=reg)
    T = _T()
    args = {"path": str(ws.file("a1")), "node_id": "CP-ONE", "key": "documents"}
    ctl = T.dispatch("lts_remove_property", dict(args), tools=eng)
    assert ctl["refused"] and ctl["evidence"]["refusal"] == "edge_change_refused", "control: the profile's own edge guard"
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            _mark_b1(eng)
            out = _finished(_write(eng, "lts_remove_property", dict(args)), stall)
            assert out["refused"] and out["evidence"]["refusal"] == "edge-context-incomplete", out.get("evidence")
            assert "membership_partial" in json.dumps(out["evidence"]), "the reason is named"
            assert "documents" in props(ws.file("a1"))["CP-ONE"], "the edge is still in the file"
        finally:
            stall.release()
            time.sleep(0.3)


def test_a_partial_result_is_evaluated_once_too(ws, monkeypatch):
    """A. The same single evaluation when the result is partial (a root marked): one confirm for the write. Kills a cache that is filled only for whole
    results."""
    from factlattice_engine import boundedfs
    eng = _engine(ws, monkeypatch)
    calls = []
    orig = boundedfs.confirm_real
    monkeypatch.setattr(boundedfs, "confirm_real", lambda path, reg=None, not_before=None: (calls.append(path), orig(path, reg, not_before))[1])
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            _mark_b1(eng)
            calls.clear()
            r = _finished(_write(eng, "lts_add_property", _a1_props(ws, "partial-once", "v")), stall)
            assert r["success"] and r["membership_partial"]["marked_count"] == 1
            assert len(calls) == 1, f"{len(calls)} confirms for one partial write"
        finally:
            stall.release()
            time.sleep(0.3)


def test_a_write_never_attaches_by_a_confirm_that_began_before_its_own_file_resolve(ws, monkeypatch):
    """F1, the reader's swap scenario. Write 1 starts a confirm; the link is swapped; write 2 resolves its file (now in B) and finds that confirm in
    flight. It must not use an answer from before the swap: it waits for that confirm to end, then starts one that began after its resolve. Both
    writes attach to `l`, with no partial and no outside warning, on receipt and record. Red on 9350c34 (write 2 got corpora [], 'outside every
    root', no membership_partial, on both)."""
    from factlattice_engine import boundedfs
    from factlattice_engine.rootstate import realpath_progress as true_rp
    eng, A, B, link = _link_world(ws, monkeypatch, deadline="20")      # the held confirm must not overrun while this test waits for write 2 under load
    T = _T()
    gate, started = threading.Event(), threading.Event()
    seen = []

    def slow(p):
        v = true_rp(p)                                  # the answer as of NOW
        seen.append((v, threading.current_thread().name))
        started.set()
        if len(seen) == 1:
            gate.wait(5)
        return v
    monkeypatch.setattr(boundedfs, "realpath_progress", slow)
    # write 2 is known to have found the confirm in flight when a SECOND thread waits on the very job write 1's thread waits on: an event, not a sleep
    from factlattice_engine import rootstate
    first_waiter, t2_in_wait = {}, threading.Event()
    real_wait = rootstate.RootRegistry.wait

    def spy_wait(self, slot, job):
        who = first_waiter.setdefault(id(job), threading.get_ident())
        if who != threading.get_ident():
            t2_in_wait.set()
        return real_wait(self, slot, job)
    monkeypatch.setattr(rootstate.RootRegistry, "wait", spy_wait)
    res = {}
    t1 = threading.Thread(target=lambda: res.update(w1=T.dispatch("lts_add_property", {"path": str(link / "l.lts"), "node_id": "L-N", "key": "k1", "value": "one"}, tools=eng)))
    t1.start()
    assert started.wait(5)
    _swap(ws.base, link, B)
    t2 = threading.Thread(target=lambda: res.update(w2=T.dispatch("lts_add_property", {"path": str(link / "l.lts"), "node_id": "L-N", "key": "k2", "value": "two"}, tools=eng)))
    t2.start()
    assert t2_in_wait.wait(15), "write 2 never waited on write 1's confirm"
    gate.set()
    t1.join(15)
    t2.join(15)
    assert "k2: two" in (B / "l.lts").read_text(), "write 2 landed in B by its own real path"
    for k in ("w1", "w2"):
        r = res[k]
        assert r["success"] and r["root_membership"]["corpora"] == ["l"] and "membership_partial" not in r and r["root_membership"]["warning"] is None, (k, r.get("root_membership"))
    recs = ledger_records(ws)
    assert all(rec["corpora"] == ["l"] and "membership_partial" not in rec for rec in recs) and len(recs) == 2
    # write 1's file resolved to A at its start and to B when the writer took it (the swap came between), so its membership is evaluated again for B.
    # Each write's answers came from jobs begun after its own resolve; none is shared across the swap. How MANY confirm calls that takes depends on timing,
    # and both counts are right: 3 when write 2's confirm of B had already started before write 1's re-resolve (a running job is never joined), 2 when
    # write 1's re-check arrived while write 2's confirm of B was still queued (it joins that job: a job that begins after the re-resolve is allowed).
    # Seen once on ubuntu-latest (2 calls). Each path is forced, with its count pinned, by the two tests below this one
    # (`test_two_writes_that_wait_on_one_queued_confirm_join_it_and_make_two_confirm_calls` and
    # `test_a_write_never_joins_a_confirm_that_is_already_running_from_before_its_own_resolve`). The invariant here: the first answer is from A, every
    # later one from B, and there are two or three of them.
    assert len(seen) in (2, 3) and seen[0][0].endswith("dirA") and all(x[0].endswith("dirB") for x in seen[1:]), [(x[0][-14:], x[1]) for x in seen]


def _two_writes_across_a_swap(ws, monkeypatch, hold):
    """W56 helper: the scenario of the test above (write 1 starts a confirm, the link is swapped, write 2 resolves its file in B and finds that confirm in
    flight), with the start of write 2's confirm of B CONTROLLED by events and monkeypatches, never by sleeps. `hold`:
      "queued"  : the second confirm job (write 2's, or write 1's re-check's, whichever submits first) is kept QUEUED, not started, until both writes
                  are waiting on it (the pool is not told about it until then);
      "running" : write 1 is kept from re-resolving its file until that second job is RUNNING, and the job is held mid-call until write 1's re-check
                  is waiting on it, so write 1's re-check finds a job that began BEFORE its own resolve.
      "afterresolve": write 1 re-resolves its file (its `not_before` is taken), then is held at the door of `confirm_real` while the second job is
                  kept queued; the job is then let run and is held mid-call, and only then does write 1 ask: it finds a RUNNING job that began AFTER
                  its own resolve.
    Returns what happened: the answers seen, the confirm jobs submitted, who waited on which job, and each `confirm_real` call's `not_before`."""
    from factlattice_engine import boundedfs, rootstate
    from factlattice_engine.rootstate import realpath_progress as true_rp
    eng, A, B, link = _link_world(ws, monkeypatch, deadline="20")
    T = _T()
    reg = boundedfs.registry()
    gate1, started, j2_running, gate2, t1_at_door, t1_go = (threading.Event() for _ in range(6))
    both_waiting, release_queued = threading.Event(), threading.Event()
    seen, submitted, waiters, nb_calls = [], [], {}, []
    state = {"holding": False, "slot": None, "t1": None, "t2": None}

    def slow(p):
        v = true_rp(p)
        seen.append((v, threading.current_thread().name, time.monotonic()))
        n = len(seen)
        started.set()
        if n == 1:
            gate1.wait(5)
        elif n == 2 and hold in ("running", "afterresolve"):
            j2_running.set()
            assert gate2.wait(15), "write 1's re-check never waited on the running confirm"
        return v
    monkeypatch.setattr(boundedfs, "realpath_progress", slow)

    real_submit = rootstate.RootSlot.submit

    def spy_submit(self, kind, fn, **kw):
        if kind == "confirm":
            if len(submitted) == 1 and hold in ("queued", "afterresolve"):
                state["holding"], state["slot"] = True, self          # set BEFORE the job is queued: its enqueue is skipped below
            job = real_submit(self, kind, fn, **kw)
            submitted.append((threading.get_ident(), job))
            return job
        return real_submit(self, kind, fn, **kw)
    monkeypatch.setattr(rootstate.RootSlot, "submit", spy_submit)

    real_enqueue = rootstate.RootRegistry._enqueue_locked

    def spy_enqueue(self, slot, foreground=False):
        if state["holding"] and slot is state["slot"] and not release_queued.is_set():
            return                                                  # the job stays in the slot's queue: queued, not started, the pool unaware of it
        return real_enqueue(self, slot, foreground)
    monkeypatch.setattr(rootstate.RootRegistry, "_enqueue_locked", spy_enqueue)

    real_wait = rootstate.RootRegistry.wait

    def spy_wait(self, slot, job):
        who = waiters.setdefault(id(job), [job, []])[1]
        who.append(threading.get_ident())
        first = submitted and submitted[0][1] is job
        second = len(submitted) >= 2 and submitted[1][1] is job
        if second and len({*who}) >= 2:
            both_waiting.set()                                     # two DIFFERENT threads wait on the very same Job object
            if hold in ("running", "afterresolve"):
                gate2.set()
        try:
            return real_wait(self, slot, job)
        finally:
            if first and hold == "running" and threading.get_ident() == state["t1"]:
                assert j2_running.wait(15), "write 2's confirm never started"   # write 1 re-resolves only now
    monkeypatch.setattr(rootstate.RootRegistry, "wait", spy_wait)

    real_confirm = boundedfs.confirm_real

    def spy_confirm(path, reg_=None, not_before=None):
        if hold == "afterresolve" and threading.get_ident() == state["t1"]:
            state["t1_calls"] = state.get("t1_calls", 0) + 1
            if state["t1_calls"] == 2:                               # write 1's re-check: its `not_before` is already taken; it waits at the door
                t1_at_door.set()
                assert t1_go.wait(15), "write 1 was never let through"
        out = real_confirm(path, reg_, not_before=not_before)
        nb_calls.append((threading.get_ident(), not_before, out))
        return out
    monkeypatch.setattr(boundedfs, "confirm_real", spy_confirm)

    res = {}
    t1 = threading.Thread(target=lambda: res.update(w1=T.dispatch("lts_add_property", {"path": str(link / "l.lts"), "node_id": "L-N", "key": "k1", "value": "one"}, tools=eng)), daemon=True)
    t2 = threading.Thread(target=lambda: res.update(w2=T.dispatch("lts_add_property", {"path": str(link / "l.lts"), "node_id": "L-N", "key": "k2", "value": "two"}, tools=eng)), daemon=True)
    out = {"queued_at_release": None}
    try:
        t1.start()
        state["t1"] = t1.ident
        assert started.wait(5), "write 1's confirm never started"
        _swap(ws.base, link, B)
        t2.start()
        state["t2"] = t2.ident
        j1_waiters = lambda: len({*waiters.get(id(submitted[0][1]), [None, []])[1]}) if submitted else 0
        deadline = time.monotonic() + 15
        while j1_waiters() < 2 and time.monotonic() < deadline:
            time.sleep(0.01)                                         # bounded poll for write 2's arrival at write 1's confirm
        assert j1_waiters() >= 2, "write 2 never waited on write 1's confirm"
        if hold == "queued":
            gate1.set()
            assert both_waiting.wait(15), "the two writes never waited on one job (the second confirm job was waited on by one write only)"
            job2 = submitted[1][1]
            slot = state["slot"]
            out["queued_at_release"] = (job2.started is None and job2 in slot._queue and len(seen) == 1)
            with reg._cv:                                            # now the pool is told: the job runs
                release_queued.set()
                real_enqueue(reg, slot, True)
        elif hold == "afterresolve":
            gate1.set()
            assert t1_at_door.wait(15), "write 1 never reached its re-check"
            deadline = time.monotonic() + 15
            while len(submitted) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)                                     # bounded poll for write 2's confirm of B to be submitted (held queued)
            assert len(submitted) >= 2, "write 2 never submitted its confirm of B"
            slot = state["slot"]
            out["queued_at_release"] = (submitted[1][1].started is None and submitted[1][1] in slot._queue)
            with reg._cv:
                release_queued.set()
                real_enqueue(reg, slot, True)
            assert j2_running.wait(15), "write 2's confirm of B never started"
            t1_go.set()
        else:
            gate1.set()
        t1.join(30)
        t2.join(30)
        assert not t1.is_alive() and not t2.is_alive(), "a write did not finish within 30 s"
    finally:
        # A failure must not leave a write thread blocked on a job that is held queued or a gate: let every gate go, enqueue a held job, join with a
        # timeout. (Seen under a mutant: the assert above failed while write 1 waited, unbounded, on a job never enqueued, and the process hung.)
        gate1.set(); gate2.set(); j2_running.set(); release_queued.set(); t1_go.set()
        if state["slot"] is not None:
            with reg._cv:
                real_enqueue(reg, state["slot"], True)
        for t in (t1, t2):
            if t.ident is not None:
                t.join(30)
    out.update(seen=seen, submitted=[j for _, j in submitted], submitters=[i for i, _ in submitted], waiters=waiters, nb_calls=nb_calls, t1=t1.ident, t2=t2.ident, res=res, A=A, B=B)
    return out


def _assert_both_writes_attach_to_l(ws, o):
    """The behavioural half of the F1 test, shared: both writes landed and attach to `l`, no partial, no outside warning, on receipt and record."""
    assert "k2: two" in (o["B"] / "l.lts").read_text(), "write 2 landed in B by its own real path"
    for k in ("w1", "w2"):
        r = o["res"][k]
        assert r["success"] and r["root_membership"]["corpora"] == ["l"] and "membership_partial" not in r and r["root_membership"]["warning"] is None, (k, r.get("root_membership"))
    recs = ledger_records(ws)
    assert all(rec["corpora"] == ["l"] and "membership_partial" not in rec for rec in recs) and len(recs) == 2


def test_two_writes_that_wait_on_one_queued_confirm_join_it_and_make_two_confirm_calls(ws, monkeypatch):
    """W56, the join, forced. Write 2's confirm of B is held QUEUED (not started) until write 1's re-check of B is waiting on it too: the re-check finds
    it in the flight table (its box has no `started` yet) and JOINS it, so the two writes share ONE job and the whole scenario makes TWO confirm calls,
    not three. That is what the F1 test above can also see on a runner whose timing lets write 1's re-check arrive before write 2's confirm starts
    (seen on ubuntu-latest: 2 calls). Every behavioural claim of that test still holds on this path: both writes attach to `l`, no partial, no warning,
    two ledger records. The join is observed directly: two different threads wait on the IDENTICAL Job object (`RootRegistry.wait` receives it), only two
    confirm jobs were ever submitted, and at the moment of release the job was still queued, with no confirm call yet made for it. (The join is made in
    `boundedfs.confirm_real`'s flight table, not in `RootSlot.submit`: a confirm is submitted with `coalesce=False`.)"""
    o = _two_writes_across_a_swap(ws, monkeypatch, "queued")
    seen, subs, waiters = o["seen"], o["submitted"], o["waiters"]
    assert o["queued_at_release"] is True, "the second confirm job was not held queued and unstarted at the moment both writes waited on it"
    assert len(subs) == 2, f"{len(subs)} confirm jobs submitted"
    who = waiters[id(subs[1])][1]
    assert {o["t1"], o["t2"]} <= set(who), "both writes wait on the second confirm job: the very same Job object"
    assert len(seen) == 2 and [x[0].endswith("dirA") for x in seen] == [True, False], [(x[0][-14:], x[1]) for x in seen]
    assert seen[1][0].endswith("dirB")
    _assert_both_writes_attach_to_l(ws, o)


def test_a_write_never_joins_a_confirm_that_is_already_running_from_before_its_own_resolve(ws, monkeypatch):
    """W56, the no-join, forced. Write 2's confirm of B is RUNNING (started, held mid-call) when write 1 re-resolves its file and asks for B again: write 1
    must NOT use it (it began before write 1's resolve, x-verify). It waits for it to end and a third confirm, begun after write 1's own resolve, answers:
    THREE confirm calls. Checked on the clocks: the running job's call began before the `not_before` of write 1's re-check, and the third call began
    after it. Behaviour as in the F1 test."""
    o = _two_writes_across_a_swap(ws, monkeypatch, "running")
    seen, subs = o["seen"], o["submitted"]
    assert len(subs) == 3, f"{len(subs)} confirm jobs submitted"
    assert len(seen) == 3 and [x[0].endswith("dirA") for x in seen] == [True, False, False], [(x[0][-14:], x[1]) for x in seen]
    w1_calls = [c for c in o["nb_calls"] if c[0] == o["t1"]]
    assert len(w1_calls) == 2, "write 1 asked for its root's real path twice (its first resolve, then B)"
    nb = w1_calls[1][1]
    assert seen[1][2] < nb, "the running job began before write 1's re-resolve: it must not be joined"
    assert seen[2][2] > nb, "write 1's B answer comes from a job that began after write 1's own resolve"
    assert w1_calls[1][2] == seen[2][0] and seen[2][0].endswith("dirB")
    _assert_both_writes_attach_to_l(ws, o)



def test_a_confirm_that_finished_before_the_write_resolved_is_never_read(tmp_path):
    """F6, unit. A finished job still in the flight table (its creator has not yet removed it) has an answer from before `not_before`: it is dropped,
    and a new job answers."""
    from factlattice.rootview import lexical
    from factlattice_engine import boundedfs, rootstate
    reg = rootstate.RootRegistry(5.0)
    d = tmp_path / "r"
    d.mkdir()
    slot = reg.slot("confirm:" + lexical(d), aux=True)
    job = slot.submit("confirm", lambda: None, coalesce=False, foreground=True)
    assert job.done.wait(3)
    boundedfs._FLIGHT[(id(reg), lexical(d))] = (job, {"started": time.monotonic() - 10, "v": "/stale/answer"})
    try:
        got = boundedfs.confirm_real(str(d), reg, not_before=time.monotonic())
    finally:
        boundedfs._FLIGHT.pop((id(reg), lexical(d)), None)
    assert got == os.path.realpath(d), got


def test_a_confirm_that_began_after_the_resolve_is_joined(tmp_path, monkeypatch):
    """F1, the other side: a confirm that began AFTER the write's resolve is joined (one job), not repeated."""
    from factlattice_engine import boundedfs, rootstate
    reg = rootstate.RootRegistry(5.0)
    d = tmp_path / "r"
    d.mkdir()
    gate, started, calls = threading.Event(), threading.Event(), []
    from factlattice_engine.rootstate import realpath_progress as true_rp

    def slow(p):
        calls.append(p)
        started.set()
        gate.wait(3)
        return true_rp(p)
    monkeypatch.setattr(boundedfs, "realpath_progress", slow)
    nb = time.monotonic() - 1.0                           # the joiner resolved its file before the confirm began
    got = {}
    t1 = threading.Thread(target=lambda: got.update(a=boundedfs.confirm_real(str(d), reg, not_before=nb)), daemon=True)
    t1.start()
    assert started.wait(3)
    t2 = threading.Thread(target=lambda: got.update(b=boundedfs.confirm_real(str(d), reg, not_before=nb)), daemon=True)
    t2.start()
    time.sleep(0.2)
    gate.set()
    t1.join(5)
    t2.join(5)
    assert got["a"] == got["b"] == os.path.realpath(d) and len(calls) == 1


def test_the_library_path_refuses_membership_unconfirmed_too(ws, monkeypatch):
    """A GUARD, not a red-first test (it passes on 9350c34 too): it pins the writer scope's own refusal, which M25 (the scope without it) removes.
    `authoring.add_property` called directly (no dispatcher), with a stuck confirm: refused membership-unconfirmed by the writer scope itself."""
    from factlattice_engine import authoring as A
    eng, Adir, B, link = _link_world(ws, monkeypatch)
    _swap(ws.base, link, B)
    before = (B / "l.lts").read_bytes()
    with RootStall(link, cap_s=30, engaged_fn=lambda: threading.current_thread().name.startswith("lts-root-pool")) as stall:
        try:
            out = run_limited(lambda: A.add_property(str(link / "l.lts"), "L-N", "k", "v", store=eng.store), LIMIT_S)
            r = _finished(out, stall)
            assert r["refused"] is True and r["reason"] == "membership-unconfirmed"
            assert (B / "l.lts").read_bytes() == before
        finally:
            stall.release()
            time.sleep(0.5)


def test_a_failure_of_the_confirm_stage_refuses_closed_and_a_registry_stage_failure_keeps_its_loud_rule(ws, monkeypatch):
    """F8. An exception from the CONFIRM stage of membership refuses the write membership-unconfirmed (fail closed, nothing written). A failure of the
    REGISTRY stage keeps its pinned, loud rule (maintainers' ruling of 2026-11-03; tests/test_node_change_ledger.py F6): the write lands under today's
    key and the receipt says the roots check was skipped. A non-path argument is the writer's own bad_argument."""
    from factlattice.registry import RegistryError
    from factlattice_engine import membership
    eng = _engine(ws, monkeypatch)
    T = _T()
    before = ws.file("a1").read_bytes()
    real_confirmed = membership._confirmed_hits

    def boom(view, store, real, kept):
        raise RuntimeError("a bug in the confirm stage")
    monkeypatch.setattr(membership, "_confirmed_hits", boom)
    r = T.dispatch("lts_add_property", _a1_props(ws, "k", "v"), tools=eng)
    assert r["refused"] is True and r["reason"] == "membership-unconfirmed" and ws.file("a1").read_bytes() == before
    monkeypatch.setattr(membership, "_confirmed_hits", real_confirmed)
    real_roots = membership.registered_roots
    n = {"calls": 0}

    def regfail(store=None):
        n["calls"] += 1
        if n["calls"] == 1:
            raise RegistryError("no registry")
        return real_roots(store)
    monkeypatch.setattr(membership, "registered_roots", regfail)
    ok = T.dispatch("lts_add_property", _a1_props(ws, "k2", "v"), tools=eng)
    assert ok["success"], ok.get("reason")


# ---- F2: the CPython-derived resolver -------------------------------------------------------------------------------------------------
def _posixpath_realpath_lines():
    import posixpath
    src = Path(posixpath.__file__).read_text(encoding="utf-8")
    i = src.index("def realpath(filename, *, strict=False):")
    return src[i:src.index("supports_unicode_filenames", i)].rstrip().splitlines()


def test_realpath_copy_is_cpythons_realpath_with_only_the_hook_lines_added():
    """F2. The copy differs from the running interpreter's `posixpath.realpath` only at the lines ending `# HOOK` (and its name). On an interpreter whose
    algorithm differs this test says so, and `realpath_progress` is the fallback (see the pin below)."""
    from factlattice_engine import rootstate
    t = Path(rootstate.__file__).read_text(encoding="utf-8")
    k = t.index("def _realpath_copy(filename, *, strict=False):")
    body = t[k:t.index("# --- end realpath copy", k)]
    hooks = [ln for ln in body.splitlines() if ln.rstrip().endswith("# HOOK")]
    assert len(hooks) == 3 and all(ln.strip() == "progress()  # HOOK" for ln in hooks)
    mine = [ln for ln in body.rstrip().splitlines() if not ln.rstrip().endswith("# HOOK")]
    mine[0] = mine[0].replace("_realpath_copy", "realpath")
    assert "Python Software Foundation" in t[max(0, k - 3500):k]
    if sys.version_info[:3] == (3, 13, 2):
        assert mine == _posixpath_realpath_lines(), "the running Python's posixpath.realpath changed: re-copy it, hooks only"
    else:
        assert mine != _posixpath_realpath_lines() or rootstate.REALPATH_IMPL == "cpython-copy"


def test_pin_which_resolver_is_active_copy_on_3_13_2_and_the_fallback_elsewhere(tmp_path):
    """P2-3. At import the engine compares the running interpreter's posixpath.realpath (hooks removed) with the copy: equal -> the copy, per-component
    progress; not equal -> os.path.realpath with progress when it returns. `REALPATH_IMPL` says which. Pinned for this interpreter, and run on every other
    interpreter installed here (Homebrew 3.13.12, 3.12, 3.9, 3.14) in a subprocess, where it must say the fallback and still answer like os.path.realpath."""
    import subprocess

    from factlattice_engine import rootstate
    assert rootstate.REALPATH_IMPL in ("cpython-copy", "os.path.realpath")
    assert rootstate.REALPATH_IMPL == ("cpython-copy" if sys.version_info[:3] == (3, 13, 2) else "os.path.realpath"), sys.version
    assert rootstate.realpath_progress is (rootstate._realpath_copy if rootstate.REALPATH_IMPL == "cpython-copy" else rootstate._realpath_fallback)
    repo = Path(rootstate.__file__).resolve().parent.parent
    code = ("import os,sys,tempfile\nsys.path.insert(0, sys.argv[1])\nfrom factlattice_engine import rootstate as r\n"
            "d=tempfile.mkdtemp(); os.symlink(d, d+'/l')\n"
            "print(sys.version.split()[0], r.REALPATH_IMPL, r.realpath_progress(d+'/l/x/..')==os.path.realpath(d+'/l/x/..'))\n")
    import glob
    ran, seen = [], []
    candidates = ["/opt/homebrew/bin/python3.13", "/opt/homebrew/bin/python3.12", "/opt/homebrew/bin/python3.9", "/opt/homebrew/bin/python3.14"]
    for d in os.environ.get("PATH", "").split(os.pathsep):             # and every python3.N on PATH
        candidates += sorted(glob.glob(os.path.join(d, "python3.[0-9]*")))
    for exe in dict.fromkeys(os.path.realpath(c) for c in candidates if os.path.exists(c) and not c.endswith(("-config", "m"))):
        if exe == os.path.realpath(sys.executable):
            continue
        r = subprocess.run([exe, "-c", code, str(repo)], capture_output=True, text=True, timeout=60)
        seen.append(exe)
        if r.returncode != 0:
            continue                                   # an interpreter this package does not import on (pre-existing): nothing to say about it
        ver, impl, same = r.stdout.split()
        if tuple(int(x) for x in ver.split(".")) == (3, 13, 2):
            continue
        ran.append(ver)
        assert impl == "os.path.realpath" and same == "True", r.stdout
    if not ran:
        pytest.skip(f"no other interpreter ran this package: looked at {seen or 'none (none found on PATH or under /opt/homebrew)'}; the fallback was not exercised on another Python")


def test_the_fallback_resolver_reports_progress_at_its_start_and_its_return_and_answers_like_os_path_realpath(tmp_path, monkeypatch):
    from factlattice_engine import rootstate
    (tmp_path / "a" / "b").mkdir(parents=True)
    os.symlink(tmp_path / "a", tmp_path / "l")
    ticks = []
    monkeypatch.setattr(rootstate, "progress", lambda: ticks.append(1))
    p = str(tmp_path / "l" / "b" / ".." / "zz")
    assert rootstate._realpath_fallback(p) == os.path.realpath(p) and len(ticks) == 2        # at its start and when it returns
    assert rootstate._realpath_fallback(p, strict=False) == os.path.realpath(p)


def test_on_the_fallback_resolver_a_slow_multi_component_root_through_the_engine_is_attached_not_refused(ws, monkeypatch):
    """P2-1 through the engine, on the fallback (the pin's seam): a root whose every call answers (0.25 s per call, two slowed components, 0.4 s
    deadline) has an elapsed budget of deadline x components + 1 s, so the write attaches, the root stays ok and nothing is partial. (Round 9 refused it
    membership-unconfirmed; the ruling of round 10 replaces that.)"""
    from factlattice_engine import boundedfs, kernel, rootstate
    monkeypatch.setattr(boundedfs, "realpath_progress", rootstate._realpath_fallback)
    monkeypatch.setattr(kernel, "_realpath_progress", rootstate._realpath_fallback)
    eng = _engine(ws, monkeypatch)
    k = eng.store.kernel
    T = _T()
    assert T.dispatch("lts_add_property", _a1_props(ws, "ctl", "x"), tools=eng)["success"]
    with RootStall(ws.root("a1").parent, cap_s=60, slow_s=0.25, engaged_fn=lambda: threading.current_thread().name.startswith("lts-root-pool")):
        out = run_limited(lambda: T.dispatch("lts_add_property", _a1_props(ws, "slow", "v"), tools=eng), 30, name="w")
        r = out.value or {}
        assert out.done and r.get("success") and r["root_membership"]["corpora"] == ["a1"] and "membership_partial" not in r, r.get("reason")
    assert k.roots.state_of(os.path.abspath(ws.root("a1"))) == "ok"


def test_list_corpora_says_which_resolver_is_active_only_when_it_is_the_fallback(ws, monkeypatch):
    """P2-3. `realpath_impl` is ABSENT when the CPython copy is active, and `os.path.realpath` when the fallback is (so an operator can see it)."""
    from factlattice_engine import rootstate
    eng = _engine(ws, monkeypatch)
    T = _T()
    monkeypatch.setattr(rootstate, "REALPATH_IMPL", rootstate.REALPATH_COPY)
    assert "realpath_impl" not in T.dispatch("lts_list_corpora", {"heal": False}, tools=eng)
    monkeypatch.setattr(rootstate, "REALPATH_IMPL", rootstate.REALPATH_FALLBACK)
    assert T.dispatch("lts_list_corpora", {"heal": False}, tools=eng)["realpath_impl"] == "os.path.realpath"


def _build_random_tree(rnd, root):
    names = ["a", "b", "c", "d", "Ab", "aB", "caf\u00e9", "cafe\u0301"]
    os.mkdir(root)
    dirs = [root]
    for _ in range(14):
        d = rnd.choice(dirs)
        p = os.path.join(d, rnd.choice(names) + str(rnd.randint(0, 2)))
        try:
            os.mkdir(p)
            dirs.append(p)
        except OSError:
            pass
    for i in range(10):
        open(os.path.join(rnd.choice(dirs), "f" + str(i)), "w").close()
    links = []
    for i in range(14):
        d, ln, kind = rnd.choice(dirs), "l" + str(i), rnd.random()
        if kind < .3:
            tgt = rnd.choice(dirs)
        elif kind < .55:
            tgt = os.path.relpath(rnd.choice(dirs), d)
        elif kind < .65:
            tgt = "l" + str(rnd.randint(0, 13))
        elif kind < .75:
            tgt = "../" * rnd.randint(1, 3) + rnd.choice(["a0", "b1", "l2", "nope"])
        elif kind < .82:
            tgt = "nonexistent/" + rnd.choice(["x", "..", "../l3"])
        elif kind < .9:
            tgt = os.path.join(rnd.choice(dirs), "f" + str(rnd.randint(0, 9)))
        elif kind < .95:
            tgt = ln
        else:
            tgt = rnd.choice(dirs) + "/"
        try:
            os.symlink(tgt, os.path.join(d, ln))
            links.append(os.path.join(d, ln))
        except OSError:
            pass
    return dirs, links, names


def test_realpath_progress_agrees_with_os_path_realpath_on_20000_sampled_cases(tmp_path):
    """F2, the reader's generator, fixed seed: random trees of directories, files, absolute / relative / chained / looping / dangling links, then paths
    with `..`, `.`, doubled and trailing slashes, `..` above the root and past `/`, case and Unicode variants. At least 20,000 cases. It calls the COPY
    itself (`_realpath_copy`), not the selected resolver, so a mutated copy cannot hide behind the import-time fallback. Red on 9350c34's hand-derived resolver.

    The copy is CPython 3.13.2's algorithm, so it is compared with the running interpreter's `os.path.realpath` only where the engine selected it. On any
    other interpreter the copy is never used (3.12.13 differs from it on 14 to 15 of these 20,000 paths: case variants and `//`), and what is asserted
    instead is that the selector chose the fallback."""
    import random

    from factlattice_engine import rootstate
    if rootstate.realpath_progress is not rootstate._realpath_copy:
        assert rootstate.realpath_progress is rootstate._realpath_fallback, "the selector chose neither the copy nor the fallback"
        return
    rnd = random.Random(20261105)
    total, bad, trees = 0, [], 0
    while total < 20000:
        root = str(tmp_path / f"t{trees}")
        trees += 1
        dirs, links, names = _build_random_tree(rnd, root)
        pool = [os.path.relpath(x, root) for x in dirs + links] + ["nope", "missing"] + names + ["l%d" % i for i in range(14)] + ["f%d" % i for i in range(10)]
        for _ in range(400):
            parts = []
            for _ in range(rnd.randint(1, 8)):
                r = rnd.random()
                parts.append(".." if r < .12 else "." if r < .2 else "" if r < .3 else rnd.choice(pool))
            p = root + "/" + "/".join(parts) + ("/" if rnd.random() < .2 else "")
            r2 = rnd.random()
            if r2 < .04:
                p = root + "/" + "/".join([".."] * rnd.randint(6, 16)) + "/" + rnd.choice(pool)      # `..` above the root, to and past `/`
            elif r2 < .07:
                p = "/" + "/".join([".."] * rnd.randint(1, 12)) + ("/" + rnd.choice(pool) if rnd.random() < .5 else "")
            elif r2 < .08:
                p = "/.."
            if rnd.random() < .1:
                q = p.split("/")
                i = rnd.randrange(1, len(q))
                q[i] = q[i].swapcase()
                p = "/".join(q)
            total += 1
            if os.path.realpath(p) != rootstate._realpath_copy(p):
                bad.append(p)
    assert total >= 20000 and not bad, f"{len(bad)} of {total} cases differ, first: {bad[:2]}"


def test_realpath_progress_dedicated_cases_chain_of_41_missing_dotdot_and_a_race(tmp_path):
    """F2, the dedicated cases: a chain of 41 links (the old copy counted traversals, not loops), a missing component followed by `..` and then a later
    link, and a lstat/readlink race that must never raise (os does not)."""
    from factlattice_engine import rootstate
    base = tmp_path / "d"
    base.mkdir()
    (base / "real").mkdir()
    (base / "real" / "f").write_text("x", encoding="utf-8")
    for n in (31, 32, 33, 39, 40, 41, 45, 100):
        prev = str(base / "real")
        for i in range(n):
            ln = str(base / f"c{n}_{i}")
            os.symlink(prev, ln)
            prev = ln
        assert rootstate._realpath_copy(prev + "/f") == os.path.realpath(prev + "/f"), n
    os.symlink("missing/..", base / "m_dd")
    os.symlink("missing/../real", base / "m_dd2")
    os.symlink("real", base / "later")
    for p in (base / "m_dd", base / "m_dd" / "real", base / "m_dd2" / "f", base / "missing" / ".." / "later" / "f", base / "missing" / ".." / "later"):
        assert rootstate._realpath_copy(str(p)) == os.path.realpath(p), p
    for p in ("/..", "/../..", "/../x", "//..", "/" + "/".join([".."] * 30), str(base) + "/" + "/".join([".."] * 40), str(base) + "/" + "/".join([".."] * 40) + "/tmp"):
        assert rootstate._realpath_copy(p) == os.path.realpath(p), p
    os.symlink("loop2", base / "la")
    os.symlink("la", base / "lb")
    os.symlink("lb", base / "loop2")
    assert rootstate._realpath_copy(str(base / "la" / "x")) == os.path.realpath(base / "la" / "x")
    stop = threading.Event()
    (base / "real2").mkdir()

    def flipper():
        i = 0
        while not stop.is_set():
            tmp = str(base / f"tmp{i % 2}")
            try:
                os.symlink(str(base / "real2"), tmp)
                os.replace(tmp, base / "x")
                os.unlink(base / "x")
                os.mkdir(base / "x")
                os.rmdir(base / "x")
            except OSError:
                pass
            i += 1
    th = threading.Thread(target=flipper, daemon=True)
    th.start()
    raised = 0
    end = time.monotonic() + 2.0
    try:
        while time.monotonic() < end:
            try:
                rootstate._realpath_copy(str(base / "x" / "y"))
            except Exception:  # noqa: BLE001
                raised += 1
    finally:
        stop.set()
        th.join(5)
    assert raised == 0, f"{raised} exceptions from a lstat/readlink race where os.path.realpath never raises"


def test_a_confirm_that_began_at_the_very_moment_of_the_resolve_is_not_read(tmp_path):
    """P3 (the freshness tie). `started == not_before` is not "after": the job is waited out and a new one answers (the comparison is `<=`)."""
    from factlattice.rootview import lexical
    from factlattice_engine import boundedfs, rootstate
    reg = rootstate.RootRegistry(5.0)
    d = tmp_path / "r"
    d.mkdir()
    slot = reg.slot("confirm:" + lexical(d), aux=True)
    job = slot.submit("confirm", lambda: None, coalesce=False, foreground=True)
    assert job.done.wait(3)
    t = time.monotonic()
    boundedfs._FLIGHT[(id(reg), lexical(d))] = (job, {"started": t, "v": "/tie/answer"})
    try:
        got = boundedfs.confirm_real(str(d), reg, not_before=t)
    finally:
        boundedfs._FLIGHT.pop((id(reg), lexical(d)), None)
    assert got == os.path.realpath(d), got


def test_a_confirms_started_stamp_is_taken_when_a_worker_picks_it_up_not_when_it_is_submitted(tmp_path, monkeypatch):
    """P3 (kills stamp-at-submit). The worker is slow to start, so the job sits submitted-but-not-started while a second write, whose resolve came after
    the submit, finds it. A job that has not started will start after that resolve, so the second write JOINS it (one job, not two)."""
    from factlattice_engine import boundedfs, rootstate
    reg = rootstate.RootRegistry(5.0)
    d = tmp_path / "r"
    d.mkdir()
    orig = rootstate.RootRegistry._worker

    def slow_start(self, me):
        time.sleep(0.5)
        return orig(self, me)
    monkeypatch.setattr(rootstate.RootRegistry, "_worker", slow_start)
    calls = []
    from factlattice_engine.rootstate import realpath_progress as true_rp
    monkeypatch.setattr(boundedfs, "realpath_progress", lambda p: (calls.append(p), true_rp(p))[1])
    got = {}
    t1 = threading.Thread(target=lambda: got.update(a=boundedfs.confirm_real(str(d), reg, not_before=time.monotonic() - 1)), daemon=True)
    t1.start()
    time.sleep(0.2)                                                 # submitted, worker not yet running it
    t2 = threading.Thread(target=lambda: got.update(b=boundedfs.confirm_real(str(d), reg, not_before=time.monotonic())), daemon=True)
    t2.start()
    t1.join(8)
    t2.join(8)
    assert got["a"] == got["b"] == os.path.realpath(d) and len(calls) == 1, (got, calls)


def test_a_non_registry_failure_before_the_confirm_stage_refuses_closed(ws, monkeypatch):
    """P2-2. Narrowed: only a RegistryError, or a failure inside the roots lookup (`registered_roots`), takes the pinned loud rule. An exception from
    anywhere else in the evaluation (here, resolving the file, before the confirm stage) refuses membership-unconfirmed with nothing written. Red on e397764."""
    from factlattice_engine import membership
    eng = _engine(ws, monkeypatch)
    T = _T()
    before = ws.file("a1").read_bytes()
    real_norm = membership._norm
    target = str(ws.file("a1"))

    def norm(p):
        if str(p) == target:
            raise RuntimeError("a bug before the confirm stage")
        return real_norm(p)
    monkeypatch.setattr(membership, "_norm", norm)
    r = T.dispatch("lts_add_property", _a1_props(ws, "k", "v"), tools=eng)
    assert r["refused"] is True and r["reason"] == "membership-unconfirmed" and ws.file("a1").read_bytes() == before
    monkeypatch.setattr(membership, "_norm", real_norm)
    real_roots = membership.registered_roots
    n = {"c": 0}

    def lookup_fails(store=None):
        n["c"] += 1
        if n["c"] == 1:
            raise RuntimeError("the roots lookup exploded")
        return real_roots(store)
    monkeypatch.setattr(membership, "registered_roots", lookup_fails)
    ok = T.dispatch("lts_add_property", _a1_props(ws, "k2", "v"), tools=eng)
    assert ok["success"], "a failure INSIDE the roots lookup keeps the pinned loud rule: the write lands, flagged"
    assert any("ledger roots check SKIPPED" in w for w in ok.get("warnings", [])) or ok.get("ledger", {}).get("status") == "ok"


# ---- round 10 -------------------------------------------------------------------------------------------------------------------------
def test_a_registry_stage_failure_is_the_one_result_of_the_write_beside_a_stuck_candidate(ws, monkeypatch):
    """P1. A candidate root is stuck AND the first read of the registry's roots fails once. The first evaluation's outcome, the registry-stage failure
    included, is THE result for the write: nothing evaluates membership again in it. The write lands under the pinned loud rule (flagged `corpora_error`,
    `ledger roots check SKIPPED`), with NO membership_partial, and the stuck confirm is never even asked. Red on 2e648c2, where `_ledger_kw` evaluated
    again, the registry read fine, the candidate was unconfirmed and nobody refused: the write landed with unconfirmed_count 1."""
    from factlattice.registry import RegistryError
    from factlattice_engine import membership
    for fail_calls in (1, 99):
        eng, A, B, link = _link_world(ws, monkeypatch) if fail_calls == 1 else _link_world(Workspace(ws.base.parent / "w2"), monkeypatch)[0:4]
        wsx = ws if fail_calls == 1 else Workspace(ws.base.parent / "w2")
        real_roots, real_confirmed = membership.registered_roots, membership._confirmed_hits
        n, asked = {"c": 0}, []

        def regfail(store=None, n=n, fail_calls=fail_calls, real_roots=real_roots):
            n["c"] += 1
            if n["c"] <= fail_calls:
                raise RegistryError("a transient registry read failure")
            return real_roots(store)
        monkeypatch.setattr(membership, "registered_roots", regfail)
        monkeypatch.setattr(membership, "_confirmed_hits", lambda *a, **k: (asked.append(1), real_confirmed(*a, **k))[1])
        _swap(wsx.base, wsx.base / "link", wsx.base / "dirB")
        with RootStall(wsx.base / "link", cap_s=30, engaged_fn=lambda: threading.current_thread().name.startswith("lts-root-pool")) as stall:
            try:
                r = _finished(_write(eng, "lts_add_property", {"path": str(wsx.base / "link" / "l.lts"), "node_id": "L-N", "key": "kr", "value": "v"}), stall)
                assert r.get("success") and "membership_partial" not in r, (fail_calls, r.get("membership_partial"), r.get("reason"))
                assert asked == [], f"fail_calls={fail_calls}: membership was evaluated past the registry-stage failure"
                recs = ledger_records(wsx)
                assert recs and recs[-1].get("corpora_error") and "membership_partial" not in recs[-1], recs[-1:]
                assert any("ledger roots check SKIPPED" in w for w in r.get("warnings", [])) or r["root_membership"].get("registry_error"), r.get("warnings")
            finally:
                stall.release()
                time.sleep(0.5)
        monkeypatch.setattr(membership, "registered_roots", real_roots)
        monkeypatch.setattr(membership, "_confirmed_hits", real_confirmed)


def test_on_the_fallback_a_slow_multi_component_root_that_answers_every_call_is_neither_marked_nor_refused(tmp_path, monkeypatch, record_property):
    """P2-1, the reader's slowroot shape (0.2 s per lstat under the root, ten slowed components, 0.6 s deadline), forced onto the fallback through the same seam the pin uses
    (`boundedfs.realpath_progress` and the kernel's alias set to `_realpath_fallback`). The confirm and a probe slot running the same resolve are given
    an elapsed budget of deadline x components + 1 s: the confirm answers, the probe slot is never UNRESPONSIVE. Red on 2e648c2 (no budget: marked and
    unconfirmed at 0.6 s). Load recorded."""
    from factlattice.rootview import lexical
    from factlattice_engine import boundedfs, rootstate
    target = tmp_path.joinpath(*[f"d{i}" for i in range(10)])           # ten slowed components: 2 s in all against a 0.6 s deadline
    target.mkdir(parents=True)
    target = str(target)
    ncomp = len([c for c in target.split("/") if c])
    monkeypatch.setattr(boundedfs, "realpath_progress", rootstate._realpath_fallback)
    real_lstat = os.lstat

    def slow(p, *a, **k):
        if str(p).startswith(str(tmp_path)) or str(p) == "/":
            time.sleep(0.2)
        return real_lstat(p, *a, **k)
    monkeypatch.setattr(os, "lstat", slow)
    reg = rootstate.RootRegistry(deadline_s=0.6)
    t0 = time.monotonic()
    got = boundedfs.confirm_real(target, reg, not_before=time.monotonic())
    took = time.monotonic() - t0
    record_property("load", json.dumps({"loadavg": os.getloadavg(), "components": ncomp, "resolve_s": round(took, 2)}))
    assert got == os.path.realpath(target) and took > 0.6, (got, took)
    slot = reg.slot(lexical(target))
    job = slot.submit("full", lambda: (rootstate._realpath_fallback(target), None)[1], foreground=True)
    time.sleep(0.9)
    assert slot.state() != rootstate.UNRESPONSIVE, "a probe whose every call answers is not marked while its resolve is still making its calls"
    assert job.done.wait(15)
    time.sleep(0.2)
    assert slot.state() == rootstate.OK
    assert job.allow is None, "the allowance applies to the resolve only: the rest of the job is held to the plain deadline again"


def test_on_the_fallback_a_truly_hung_resolve_is_still_refused_within_its_budget(tmp_path, monkeypatch):
    """P2-1. The budget is bounded: a resolve whose lstat never returns is unconfirmed after deadline x components + margin, not later."""
    from factlattice_engine import boundedfs, rootstate
    target = tmp_path / "a" / "b"
    target.mkdir(parents=True)
    comps = len([c for c in str(target).split("/") if c])
    deadline = 0.15
    budget = deadline * comps + 1.0                      # the stated margin, written here and not read from the module under test
    assert rootstate.FALLBACK_MARGIN_S == 1.0
    monkeypatch.setattr(boundedfs, "realpath_progress", rootstate._realpath_fallback)
    gate = threading.Event()
    real_lstat = os.lstat

    def hang(p, *a, **k):
        if str(p) == str(target):
            gate.wait(30)
        return real_lstat(p, *a, **k)
    monkeypatch.setattr(os, "lstat", hang)
    reg = rootstate.RootRegistry(deadline_s=deadline)
    t0 = time.monotonic()
    try:
        got = boundedfs.confirm_real(str(target), reg, not_before=time.monotonic())
        took = time.monotonic() - t0
    finally:
        gate.set()
    assert got is None and budget - 0.1 <= took <= budget + 1.5, (got, took, budget)


def test_an_import_with_no_file_attribute_selects_the_fallback_and_starts(tmp_path):
    """P2-2. A frozen or embedded build has no `__file__` for rootstate: the import-time comparison must not raise. The module source is executed with no
    `__file__` (and with `__file__` None): it imports, and the fallback is active. Red on 2e648c2 (NameError / TypeError at import)."""
    from factlattice_engine import rootstate
    src = Path(rootstate.__file__).read_text(encoding="utf-8")
    for glb in ({"__name__": "factlattice_engine._rootstate_nofile"}, {"__name__": "factlattice_engine._rootstate_nofile", "__file__": None}):
        ns = dict(glb)
        exec(compile(src, "<rootstate with no __file__>", "exec"), ns)
        assert ns["REALPATH_IMPL"] == "os.path.realpath" and ns["realpath_progress"] is ns["_realpath_fallback"], glb


def test_an_unreadable_posixpath_source_selects_the_fallback(monkeypatch, tmp_path):
    """P2-3 (kills the mutant that treats an unreadable source as equal). With no readable `posixpath` source `_choose_copy` is False."""
    import posixpath

    from factlattice_engine import rootstate
    assert rootstate._choose_copy() is (rootstate.REALPATH_IMPL == "cpython-copy")
    monkeypatch.setattr(posixpath, "__file__", str(tmp_path / "does-not-exist.py"))
    assert rootstate._posixpath_realpath_lines() is None and rootstate._choose_copy() is False
    monkeypatch.setattr(posixpath, "__file__", None)
    assert rootstate._posixpath_realpath_lines() is None and rootstate._choose_copy() is False
    (tmp_path / "pp.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(posixpath, "__file__", str(tmp_path / "pp.py"))
    assert rootstate._posixpath_realpath_lines() is None and rootstate._choose_copy() is False


def test_a_failure_inside_annotate_that_is_not_the_registrys_still_surfaces(ws, monkeypatch):
    """`_annotate` reports a REGISTRY-stage failure on a landed write instead of raising; any other exception is a bug and must surface (kills the
    mutant that swallows everything)."""
    from factlattice_engine import membership
    eng = _engine(ws, monkeypatch)

    def boom(receipt, path, store=None):
        raise RuntimeError("a bug in annotate")
    monkeypatch.setattr(membership, "annotate", boom)
    from factlattice_engine import authoring as A
    with pytest.raises(RuntimeError, match="a bug in annotate"):
        A.add_property(str(ws.file("a1")), "A1-N", "k", "v", store=eng.store)


# ---- round 11 -------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fail_at", [1, 2, 3, 4, 5, 6, 7, 8])
def test_the_ledger_record_and_the_receipt_always_agree_whichever_registry_read_fails(ws, monkeypatch, fail_at):
    """P2-1, rule A. A single failure injected at the Nth read of the registry (`config.default_corpora`) in a write with no store: the ledger takes its
    corpora from the write's ONE membership result, so the record and the receipt agree. Either both name the corpus, or the record carries
    `corpora_error` and the receipt carries `registry_error` (flagged on both). Red on b8cb7c4 for N=4: the ledger read the registry directly after the
    first evaluation, the record got corpora None with `corpora_error` and the receipt said ['a1']."""
    from factlattice.registry import RegistryError
    from factlattice_engine import authoring as A
    from factlattice_engine import config
    _noprobe_env(ws, monkeypatch)
    real = config.default_corpora
    n = {"c": 0}

    def flaky(*a, **k):
        n["c"] += 1
        if n["c"] == fail_at:
            raise RegistryError("a transient registry read failure")
        return real(*a, **k)
    monkeypatch.setattr(config, "default_corpora", flaky)
    r = A.add_property(str(ws.file("a1")), "A1-N", "k", "v", store=None)
    assert r["success"], r.get("reason")
    rec = _recs(ws, "a1.lts")[-1]
    rm = r["root_membership"]
    if rec.get("corpora_error"):
        assert rec["corpora"] is None and rm.get("registry_error"), (fail_at, rec.get("corpora"), rm)
    else:
        assert rec["corpora"] == rm["corpora"] == ["a1"] and not rm.get("registry_error"), (fail_at, rec.get("corpora"), rm)
    assert "membership_partial" not in r or r["membership_partial"] == rec.get("membership_partial")


def test_the_fallbacks_closing_progress_report_runs_in_a_finally_before_the_allowance_is_dropped(monkeypatch, tmp_path):
    """P3-1 and P3-2. The closing `progress()` of `_realpath_fallback` is taken (1) on the exception path too, and (2) BEFORE `job.allow` goes back to
    None, so there is no moment when the plain deadline is measured from a report older than the resolve. Red on b8cb7c4 (no closing report on an
    exception; the report came after the reset)."""
    from factlattice_engine import rootstate
    reg = rootstate.RootRegistry(deadline_s=5.0)
    job = rootstate.Job("full", lambda: None, clock=reg.clock)
    seen = []
    monkeypatch.setattr(rootstate, "progress", lambda: seen.append(job.allow))
    monkeypatch.setattr(rootstate._tls, "job", job, raising=False)
    monkeypatch.setattr(rootstate._tls, "reg", reg, raising=False)

    def boom(p, **k):
        raise RuntimeError("the resolve failed")
    monkeypatch.setattr(os.path, "realpath", boom)
    with pytest.raises(RuntimeError):
        rootstate._realpath_fallback(str(tmp_path / "a" / "b"))
    assert len(seen) == 2 and seen[0] is not None and seen[1] is not None, f"start and closing reports, both while the allowance was set: {seen}"
    assert job.allow is None, "and the allowance is dropped after the exception"
    seen.clear()
    monkeypatch.undo()
    monkeypatch.setattr(rootstate, "progress", lambda: seen.append(job.allow))
    monkeypatch.setattr(rootstate._tls, "job", job, raising=False)
    monkeypatch.setattr(rootstate._tls, "reg", reg, raising=False)
    rootstate._realpath_fallback(str(tmp_path))
    assert len(seen) == 2 and seen[1] is not None and job.allow is None, seen


def test_a_write_through_the_engine_takes_one_look_at_the_store(ws, monkeypatch):
    """F5 (MECH-BR-WRITES A): membership is evaluated ONCE per write and takes ONE look at the store, which it carries on the call's view. Counted on a
    real engine: an add_property takes exactly one `snapshot_all` (before round 8: three, in `registered_roots`, `_declared_map` and `snapshot`).
    Red on 2ef8895 (3)."""
    eng = _engine(ws, monkeypatch)
    T = _T()
    real, n = eng.store.snapshot_all, []

    def counted():
        n.append(1)
        return real()
    eng.store.snapshot_all = counted
    r = T.dispatch("lts_add_property", _a1_props(ws, "k_one_look", "v"), tools=eng)
    assert r["success"], r.get("reason")
    assert len(n) == 1, f"{len(n)} looks for one write"


def test_a_write_joins_a_confirm_that_is_running_but_began_after_its_own_resolve(ws, monkeypatch):
    """W56, the other two-call path, forced. Write 1 re-resolves its file (its `not_before` is taken) and is held at the door of `confirm_real`; write 2's
    confirm of B then starts and is held mid-call. Write 1 finds a job that is RUNNING but began AFTER its own resolve: that is a usable answer, so it
    JOINS it: two confirm calls, and the clocks say `not_before` < the job's call. (The x-verify rule is about when the job began, not whether it runs.)"""
    o = _two_writes_across_a_swap(ws, monkeypatch, "afterresolve")
    seen, subs = o["seen"], o["submitted"]
    assert o["queued_at_release"] is True, "write 2's confirm was not held queued until write 1 had taken its `not_before`"
    assert len(subs) == 2, f"{len(subs)} confirm jobs submitted"
    assert len(seen) == 2 and [x[0].endswith("dirA") for x in seen] == [True, False], [(x[0][-14:], x[1]) for x in seen]
    w1_calls = [c for c in o["nb_calls"] if c[0] == o["t1"]]
    assert len(w1_calls) == 2 and seen[1][2] > w1_calls[1][1], "the running job began after write 1's re-resolve"
    assert {o["t1"], o["t2"]} <= set(o["waiters"][id(subs[1])][1]), "both writes waited on that one running job"
    assert w1_calls[1][2] == seen[1][0] and seen[1][0].endswith("dirB")
    _assert_both_writes_attach_to_l(ws, o)
