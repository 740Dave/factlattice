"""[FL-BOUNDED-ROOTS] slice 4: boot and build_store ([MECH-BR-BOOT], ACC-BR x-c1 and x-c6 as the boot parts, PLAN-BR x-build-traps 1, 5, 6).

THE MODEL. Every stall here is `tests/stall_harness.py`'s model of a hung (or slow) disk under one named root. No test points at /Volumes or a
real corpus, and every result is a result about that model, not about a drive. Writer probes use sibling temp dirs (HOME is never an ancestor or a
descendant of a corpus) and run an ordinary control write first.

RED FIRST. Each test below was run on bf94c02 (the tree before this slice) and failed there for the reason its docstring names; the commit message
lists them. A test that fails there only because a symbol does not exist is marked SYMBOL in its docstring: those are counted apart, and each has a
behaviour test beside it that does not depend on the symbol.

TIME. Every blocked call is asked with a hard limit (`run_limited`), every stall is released in `finally`, every child process is killed in `finally`.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests.stall_harness import NoReply, RealServer, RootStall, Workspace, run_limited

DEADLINE_S = 0.5
CODE_UNRESPONSIVE = "root-unresponsive"
CODE_BUILD = "corpus-root-unresponsive"
DEADLINES = {"LTS_ROOT_DEADLINE_S": "1", "LTS_CALL_DEADLINE_S": "2", "LTS_CALL_WORKERS": "1"}


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    return Workspace(tmp_path / "w")


_LOADERS: list[Any] = []


def _env(ws: Workspace, monkeypatch, deadline: str = str(DEADLINE_S)) -> None:
    """The environment of an in-process test, and the process registry started over so that THIS deadline is the one it reads (it is read once per
    process; a thread left over from an earlier test could otherwise have made the registry with the default)."""
    for k, v in ws.env({"LTS_ROOT_DEADLINE_S": deadline}).items():
        monkeypatch.setenv(k, v)
    from factlattice_engine import boundedfs
    boundedfs._reset()


@pytest.fixture(autouse=True)
def _no_loader_outlives_its_test():
    """A boot loader is a thread; one still running when its test ends could touch the process registry in the next test."""
    yield
    for ldr in _LOADERS:
        ldr.first_pass_done.wait(30)
    _LOADERS.clear()


def _wait(cond, limit_s: float, step: float = 0.05) -> bool:
    end = time.monotonic() + limit_s
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


def entry(listing: dict | None, cid: str) -> dict | None:
    for c in (listing or {}).get("corpora") or []:
        if c.get("corpus_id") == cid:
            return c
    return None


# ======================================================================================================================
# ONE ROOT REGISTRY PER PROCESS  (ruling 1)
# ======================================================================================================================
def test_every_kernel_boundedfs_and_build_store_share_one_registry(ws, monkeypatch):
    """BEHAVIOUR, red on bf94c02: a kernel made its own registry and boundedfs another, so two objects answered 'is this root stuck'. Now one
    object does, and `build_store` reads on it (a root it reads leaves no slot of its own once idle, but a stuck one is counted there)."""
    _env(ws, monkeypatch)
    from factlattice_engine import boundedfs, config
    from factlattice_engine.kernel import FreshnessKernel
    k1, k2 = FreshnessKernel(), FreshnessKernel()
    assert k1.roots is k2.roots, "two kernels in one process share a registry"
    assert boundedfs.registry() is k1.roots, "boundedfs and the kernel share it too"
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            out = run_limited(lambda: config.build_store(ws.registry), DEADLINE_S + 3.0, name="build")
            assert out.done and out.error is None, "build_store returned"
            assert k1.roots.stuck_workers() == 1, "the stuck read of b1 is counted ONCE, on the one registry every kernel and boundedfs also see"
            assert boundedfs.registry().stuck_workers() == 1
        finally:
            stall.release()
            time.sleep(0.2)


def test_symbol_the_process_registry_is_one_object_and_its_deadline_is_read_once(ws, monkeypatch):
    """SYMBOL (`process_registry`, `reset_process_registry` do not exist on bf94c02). The deadline is read when the registry is first made."""
    from factlattice_engine import rootstate as RS
    monkeypatch.setenv(RS.ROOT_DEADLINE_ENV, "3")
    RS.reset_process_registry()
    r = RS.process_registry()
    assert r is RS.process_registry() and r.deadline_s == 3.0
    monkeypatch.setenv(RS.ROOT_DEADLINE_ENV, "9")
    assert RS.process_registry().deadline_s == 3.0, "read once per process"
    RS.reset_process_registry()
    assert RS.process_registry().deadline_s == 9.0, "the one reset helper starts it over"


def _stuck_workers(reg, paths, release: threading.Event) -> list[Any]:
    """Make each of `paths` a registered-root slot with a job that does not return (a model of a stuck worker) and wait until all are counted stuck."""
    out = []
    for p in paths:
        slot = reg.slot(p)
        out.append(slot.submit("full", lambda: (release.wait(30), None)[1], coalesce=False, foreground=True))
    def all_stuck():
        reg.rebalance()                      # what a waiter does every few milliseconds: lets the pool grow for foreground work that has waited
        return reg.stuck_workers() >= len(paths)
    assert _wait(all_stuck, reg.deadline_s * 4 + 4), f"setup: {reg.stuck_workers()} of {len(paths)} workers stuck"
    return out


def test_a_root_with_a_stuck_worker_fails_its_corpus_at_once_with_no_thread_and_no_queued_job(ws, monkeypatch):
    """x-library: 'for a root with a worker still stuck, build_store marks the corpus failed at once with no new thread'. Red on bf94c02: build_store
    ignored the registry and served a1."""
    _env(ws, monkeypatch, "0.3")
    from factlattice_engine import boundedfs, config
    reg = boundedfs.registry()
    release = threading.Event()
    try:
        _stuck_workers(reg, [os.path.abspath(str(ws.root("a1")))], release)
        threads0 = threading.active_count()
        t0 = time.monotonic()
        store = config.build_store(ws.registry)
        took = time.monotonic() - t0
        assert (store._failed_corpora.get("a1") or {}).get("code") == CODE_BUILD, store._failed_corpora
        assert "a1" not in store.corpus_ids() and "b1" in store.corpus_ids(), "only the corpus on the stuck root is lost"
        assert took < 1.5, f"a root with a stuck worker is refused at once, not waited for: {took:.2f}s"
        slot = reg.slot(os.path.abspath(str(ws.root("a1"))), create=False)
        assert slot is not None and len(slot._queue) == 0, "nothing queued behind the stuck job"
        assert reg.stuck_workers() == 1
        for _ in range(5):
            config.build_store(ws.registry)
        assert len(slot._queue) == 0 and reg.stuck_workers() == 1, "repeated builds queue nothing and add no stuck worker"
        assert threading.active_count() <= threads0 + 1, "and add no thread (one healthy read may leave a pool thread for a moment)"
    finally:
        release.set()
        time.sleep(0.3)


def test_the_process_cap_of_sixteen_stuck_workers_fails_a_corpus_on_a_healthy_root_at_once(tmp_path, ws, monkeypatch):
    """x-library: 'at most one stuck worker per root and 16 in all: past that ... build_store marks the corpus failed at once with no new thread'.
    16 stuck workers are made on 16 other roots; a1's own root is healthy and has no worker. Red on bf94c02: a1 was served."""
    _env(ws, monkeypatch, "0.3")
    from factlattice_engine import boundedfs, config, rootstate
    reg = boundedfs.registry()
    release = threading.Event()
    paths = [str(tmp_path / f"x{i}") for i in range(rootstate.MAX_STUCK_WORKERS)] if hasattr(rootstate, "MAX_STUCK_WORKERS") else \
        [str(tmp_path / f"x{i}") for i in range(16)]
    try:
        _stuck_workers(reg, paths, release)
        assert reg.stuck_workers() == 16
        threads0 = threading.active_count()
        store = config.build_store({"a1": ws.registry["a1"]})
        assert (store._failed_corpora.get("a1") or {}).get("code") == CODE_BUILD, ("past the cap the corpus is refused", store._failed_corpora)
        assert threading.active_count() <= threads0, "no new thread past the cap"
        assert reg.slot(os.path.abspath(str(ws.root("a1"))), create=False) is None, "and no slot, so no job, was made for it"
    finally:
        release.set()
        time.sleep(0.3)
    store = config.build_store({"a1": ws.registry["a1"]})
    assert "a1" in store.corpus_ids(), "the cap clears when the workers return (control: the same call serves a1 again)"


def test_symbol_the_process_cap_is_sixteen():
    from factlattice_engine import rootstate
    assert rootstate.MAX_STUCK_WORKERS == 16


# ======================================================================================================================
# build_store with hung roots  (x-c6)
# ======================================================================================================================
def test_k_hung_roots_cost_one_deadline_not_k_and_the_healthy_corpus_is_served(tmp_path, monkeypatch):
    """x-library/x-c6: every root is read at once. Three hung roots out of four: build_store returns in about one deadline. Red on bf94c02: it never
    returned."""
    ws4 = Workspace(tmp_path / "w4", corpora=("a1", "b1", "c1", "d1"))
    _env(ws4, monkeypatch, "1")
    from factlattice_engine import config
    with RootStall(ws4.root("b1"), cap_s=30, extra_roots=[str(ws4.root("c1")), str(ws4.root("d1"))]) as stall:
        try:
            t0 = time.monotonic()
            out = run_limited(lambda: config.build_store(ws4.registry), 6.0, name="build-k")
            took = time.monotonic() - t0
            assert out.done and out.error is None, f"build_store did not return (blocked in {stall.hit_names()})"
            store = out.value
            assert took < 2.2, f"three hung roots cost {took:.2f}s (deadline 1 s): they were read one after another (three in turn would be 3 s)"
            assert store.corpus_ids() == ["a1"]
            assert {c: f["code"] for c, f in store._failed_corpora.items()} == {"b1": CODE_BUILD, "c1": CODE_BUILD, "d1": CODE_BUILD}
            assert all(f["path"] is None for f in store._failed_corpora.values()), "a hung root has no member file and so no path"
        finally:
            stall.release()
            time.sleep(0.3)


def test_the_served_corpora_keep_the_registrys_order_whatever_order_their_reads_end_in(tmp_path, monkeypatch):
    """GUARD (green on bf94c02 as well): a slow first corpus and fast later ones must not reorder `corpus_ids()` (output order of unnamed reads)."""
    ws3 = Workspace(tmp_path / "w3", corpora=("a1", "b1", "c1"))
    _env(ws3, monkeypatch, "5")
    from factlattice_engine import config
    with RootStall(ws3.root("a1"), cap_s=30, slow_s=0.05):
        store = config.build_store(ws3.registry)
    assert store.corpus_ids() == ["a1", "b1", "c1"], store.corpus_ids()


def test_after_the_stall_ends_the_root_serves_again_with_no_restart(ws, monkeypatch):
    """x-deadlines: 'A root past the deadline is unresponsive until it answers; then it serves, with no restart.' build_store, release, build_store.
    Red on bf94c02: the first build never returned."""
    _env(ws, monkeypatch)
    from factlattice_engine import boundedfs, config
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        out = run_limited(lambda: config.build_store(ws.registry), DEADLINE_S + 3.0, name="build-1")
        assert out.done and "b1" in out.value._failed_corpora
        again = config.build_store(ws.registry)
        assert "b1" in again._failed_corpora, "control: while it hangs it stays refused"
        stall.release()
        reg = boundedfs.registry()
        assert _wait(lambda: reg.stuck_workers() == 0 and reg.state_of(os.path.abspath(str(ws.root("b1")))) in (None, "ok"), 5), "the root recovers"
    after = config.build_store(ws.registry)
    assert sorted(after.corpus_ids()) == ["a1", "b1"] and not after._failed_corpora


def test_every_in_process_caller_of_build_store_in_the_engine_gets_the_bound(ws, monkeypatch):
    """The callers of `config.build_store` inside this repository (`server.build_tools`, which `callcli` and the reviewer also use, the analytics
    default engine, the read-profile CLI's listing) each get the bound unasked: with b1 hung each returns inside the deadline, serves a1, and does not
    list b1. Red on bf94c02: each of them never returned."""
    _env(ws, monkeypatch)
    from factlattice_engine import readprofile
    from factlattice_engine.analytics.core import build_default_tools
    from factlattice_engine.server import build_tools
    callers = {
        "server.build_tools": lambda: sorted(build_tools(watch=False, require_corpora=True).store.corpus_ids()),
        "callcli/reviewer door (require_corpora=False)": lambda: sorted(build_tools(watch=False, require_corpora=False).store.corpus_ids()),
        "analytics.core.build_default_tools": lambda: sorted(build_default_tools().store.corpus_ids()),
        "readprofile._live_corpus_nodes": lambda: sorted(readprofile._live_corpus_nodes(None)),
    }
    with RootStall(ws.root("b1"), cap_s=40) as stall:
        try:
            for label, fn in callers.items():
                out = run_limited(fn, DEADLINE_S + 4.0, name=label)
                assert out.done and out.error is None, f"{label}: did not return (blocked in {stall.hit_names()}, error {out.error!r})"
                assert out.value == ["a1"], (label, out.value)
        finally:
            stall.release()
            time.sleep(0.3)


def test_find_on_a_failed_build_corpus_is_refused_with_no_disk_call_through_every_tool_and_self_heal(ws, monkeypatch):
    """x-c6 + x-build-traps 6, with the CONSUMER'S engine (`LtsEngine(build_store(...), self_heal=True)`, as the agent-definition renderer builds it):
    every tool that takes a corpus answers success false, `corpus-root-unresponsive`, with no absolute path, and the stalled root sees NO call from
    any of them. Red on bf94c02: build_store never returned."""
    _env(ws, monkeypatch)
    from factlattice_engine import config
    from factlattice_engine import tools as T
    from factlattice_engine.tools import LtsEngine
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            out = run_limited(lambda: config.build_store({"b1": ws.registry["b1"]}), DEADLINE_S + 3.0, name="build")
            assert out.done and out.error is None
            eng = LtsEngine(out.value, self_heal=True)
            assert out.value.corpus_ids() == [] and out.value._failed_corpora["b1"]["code"] == CODE_BUILD
            hits = stall.hit_count
            calls = [("lts_find", {"corpus": "b1", "text": "lanternfish"}), ("lts_find", {"corpus": "b1", "tier": "full"}),
                     ("lts_node", {"corpus": "b1", "node_id": "B1-N"}), ("lts_traverse", {"corpus": "b1", "start_node": "B1-N"}),
                     ("lts_reverse", {"corpus": "b1", "node_id": "B1-N"}), ("lts_find_in_prose", {"corpus": "b1", "text": "x"}),
                     ("lts_aggregate", {"corpus": "b1", "op": "count"}), ("lts_capabilities", {"corpus": "b1", "node_id": "B1-N"}),
                     ("lts_preview", {"corpus": "b1", "content": "# x [X-Y]\n\n- k: v\n"})]
            for name, args in calls:
                res = run_limited(lambda n=name, a=args: T.dispatch(n, a, tools=eng), 2.0, name=name)
                assert res.done, f"{name} did not return"
                r = res.value
                assert r.get("success") is False and r.get("reason") == CODE_BUILD, (name, r)
                assert "error" in r and str(ws.root("b1")) not in json.dumps(r), (name, "no absolute path in the reply")
            assert stall.hit_count == hits, f"a refused call touched the stalled root: {stall.hit_names()}"
        finally:
            stall.release()
            time.sleep(0.3)


def test_every_reader_of_a_failed_corpus_record_survives_a_record_with_no_path(ws, monkeypatch):
    """x-build-traps 6: the failed view, the skips, the listing and the named-call refusal all assumed a record has a file path. A record with
    `path: None` and the new code goes through each of them. GUARD, not red: slice 3 had already built those branches ("nothing emits the code
    yet"), and this slice is what first emits it, so the test passed on bf94c02 and pins the readers for the emission."""
    _env(ws, monkeypatch)
    from factlattice_engine import config
    from factlattice_engine import tools as T
    from factlattice_engine.tools import LtsEngine
    store = config.build_store({"a1": ws.registry["a1"]})
    store._failed_corpora["z9"] = {"code": CODE_BUILD, "path": None, "private": False}
    store._failed_corpora[PRIV_ID + "-personality"] = {"code": CODE_BUILD, "path": None, "private": True}
    eng = LtsEngine(store)
    lst = T.dispatch("lts_list_corpora", {"heal": False}, tools=eng)
    assert {"corpus_id": "z9", "code": CODE_BUILD} in lst["failed_corpora"], lst.get("failed_corpora")
    assert lst["private_failed"] == 1 and PRIV_ID not in json.dumps(lst), "a private failed corpus is counted, never named"
    un = T.dispatch("lts_find", {"text": "lanternfish", "tier": "digest"}, tools=eng)
    skipped = un["extent"]["corpora_skipped"]
    assert {"corpus": "z9", "reason": CODE_BUILD} in skipped and PRIV_ID not in json.dumps(un)
    named = T.dispatch("lts_find", {"corpus": "z9", "text": "x"}, tools=eng)
    assert named["reason"] == CODE_BUILD and "None" not in named["error"] and "'None'" not in named["error"], named["error"]
    pv = T.dispatch("lts_preview", {"corpus": PRIV_ID + "-personality", "content": "# x [X-Y]\n"}, tools=eng)
    assert pv["reason"] == CODE_BUILD and PRIV_ID not in pv.get("error", "").replace(f"corpus '{PRIV_ID}-personality'", ""), pv


def test_a_built_store_process_exits_at_once_with_a_worker_still_stuck(ws, tmp_path):
    """x-c6: 'the process able to exit at once'. A child process builds a store under a stall that does NOT release (cap 120 s), prints, and ends
    normally; the parent measures how long the child lives after it printed. Red on bf94c02: the child never printed."""
    script = tmp_path / "child.py"
    script.write_text(textwrap.dedent(f"""
        import os, sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
        from tests.stall_harness import RootStall
        from factlattice_engine import config
        with RootStall({str(ws.root("b1"))!r}, cap_s=120):
            t0 = time.monotonic()
            store = config.build_store({{"a1": {ws.registry["a1"]!r}, "b1": {ws.registry["b1"]!r}}})
            print("BUILT", round(time.monotonic() - t0, 2), sorted(store.corpus_ids()), sorted(store._failed_corpora), flush=True)
            os._exit(0) if os.environ.get("CHILD_HARD") else None
        """), encoding="utf-8")
    env = dict(os.environ, **ws.env({"LTS_ROOT_DEADLINE_S": "0.5", "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}))
    p = subprocess.Popen([sys.executable, "-u", str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(ws.base), text=True)
    try:
        import select
        ready, _w, _x = select.select([p.stdout], [], [], 20.0)          # a child that never builds must fail this test, not hang it
        assert ready, "the child printed nothing in 20 s: build_store did not return"
        line = p.stdout.readline()
        t_print = time.monotonic()
        assert line.startswith("BUILT"), (line, p.stderr.read() if p.poll() is not None else "")
        assert "['a1']" in line and "['b1']" in line
        # leaving the `with` releases the model, which would end the stuck thread by itself: the child must not depend on it, so the
        # block is cut short by the child's own exit; here we only require the exit to come promptly after the print
        rc = p.wait(timeout=8)
        assert rc == 0 and time.monotonic() - t_print < 6, "the child exits promptly (stuck workers are daemon threads)"
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)


# ======================================================================================================================
# the server's boot  (x-server, x-c1, x-build-traps 1, 5)
# ======================================================================================================================
def test_a_server_whose_every_corpus_is_pending_is_not_in_registry_error_mode(tmp_path):
    """x-build-traps 1, real server: ONE corpus, and its root hangs, so every corpus is pending. The handshake answers inside a second, the listing
    shows the corpus pending with null totals and NO registry error, a call naming it waits for the deadline and is then refused root-unresponsive
    (not registry-not-found), and an unnamed read declares it. Red on bf94c02: the handshake never came."""
    w1 = Workspace(tmp_path / "w1", corpora=("b1",))
    with RealServer(w1, stall_root=w1.root("b1"), engaged=True, env=DEADLINES) as srv:
        try:
            srv.initialize(timeout=1.0)
        except NoReply:
            raise AssertionError(f"no handshake within 1 s (boot blocked in {sorted({(h['call'], h['thread']) for h in srv.hits()})[:3]})")
        t0 = time.monotonic()
        listing, _ = srv.call("lts_list_corpora", {"heal": False}, timeout=2.0)
        assert time.monotonic() - t0 < 1.0, "the listing never waits"
        assert "error" not in (listing.get("registry_choice") or {}), listing.get("registry_choice")
        b1 = entry(listing, "b1")
        assert b1 and b1["total_nodes"] is None and b1["root_health"]["status"] == "pending", b1
        res, _ = srv.call("lts_find", {"corpus": "b1", "text": "lanternfish"}, timeout=4.0)
        assert res["success"] is False and res["reason"] == CODE_UNRESPONSIVE, res
        un, _ = srv.call("lts_find", {"text": "lanternfish", "tier": "digest"}, timeout=4.0)
        assert un["success"] is True and un["complete"] is False and un["count"] == 0, un
        assert {"corpus": "b1", "reason": CODE_UNRESPONSIVE} in un["extent"]["corpora_skipped"], un["extent"]["corpora_skipped"]
        again, _ = srv.call("lts_list_corpora", {"heal": False}, timeout=2.0)
        assert entry(again, "b1")["root_health"]["status"] == "unresponsive" and "error" not in (again.get("registry_choice") or {})
        srv.require_hit()


def test_a_corpus_that_never_answered_at_boot_loads_when_the_stuck_call_returns_with_no_restart(tmp_path):
    """x-deadlines on the boot path: unresponsive at boot, then the stall ends: the corpus is served by the next calls, no restart. Red on bf94c02:
    the handshake never came (the load half has no old-tree form)."""
    w2 = Workspace(tmp_path / "w2")
    with RealServer(w2, stall_root=w2.root("b1"), engaged=True, env=DEADLINES) as srv:
        srv.initialize(timeout=1.0)
        r, _ = srv.call("lts_find", {"corpus": "b1", "text": "lanternfish"}, timeout=4.0)
        assert r["reason"] == CODE_UNRESPONSIVE
        srv.release()

        def served():
            res, _ = srv.call("lts_find", {"corpus": "b1", "text": "lanternfish", "tier": "digest"}, timeout=4.0)
            return bool(res.get("success")) and res.get("count") == 1
        assert _wait(served, 20, step=0.5), "b1 was not served after the stall ended"
        listing, _ = srv.call("lts_list_corpora", {"heal": False}, timeout=3.0)
        b1 = entry(listing, "b1")
        assert b1["total_nodes"] == 2 and "root_health" not in b1 and not listing.get("stuck_workers"), b1


def test_the_handshake_does_not_wait_and_a_call_during_a_slow_boot_waits_and_is_served(tmp_path):
    """x-server: 'in the normal boot no call sees a refusal it would not see today'. A SLOW root (0.1 s per call, answering): the handshake is quick, a
    call naming it right away waits and gets the answer, never a refusal. Red on bf94c02: the handshake came after the whole slow read."""
    w3 = Workspace(tmp_path / "w3")
    env = dict(DEADLINES, LTS_ROOT_DEADLINE_S="20")
    with RealServer(w3, stall_root=w3.root("b1"), engaged=True, slow_s=0.1, env=env, confirm_s=60.0) as srv:
        t0 = time.monotonic()
        srv.initialize(timeout=1.5)
        assert time.monotonic() - t0 < 1.5
        res, _ = srv.call("lts_find", {"corpus": "b1", "text": "lanternfish", "tier": "digest"}, timeout=25.0)
        assert res["success"] is True and res["count"] == 1, res
        a1, _ = srv.call("lts_find", {"corpus": "a1", "text": "lanternfish", "tier": "digest"}, timeout=5.0)
        assert a1["success"] and a1["count"] == 1


def test_no_corpus_tools_do_not_wait_while_a_corpus_is_pending(tmp_path, ws):
    """x-build-traps 5: 'no-corpus tools do not wait'. selfcheck-free tools that name no corpus answer at once while b1 is pending. Red on bf94c02:
    no handshake."""
    w4 = Workspace(tmp_path / "w4")
    d = tmp_path / "scratch"
    d.mkdir()
    (d / "x.lts").write_text(w4.file("a1").read_text(), encoding="utf-8")
    with RealServer(w4, stall_root=w4.root("b1"), engaged=True, env=DEADLINES) as srv:
        srv.initialize(timeout=1.0)
        for name, args in (("lts_lint", {"source": str(d)}), ("lts_preview", {"content": w4.file("a1").read_text()}),
                           ("lts_list_corpora", {"heal": False})):
            t0 = time.monotonic()
            res, _ = srv.call(name, args, timeout=3.0)
            assert time.monotonic() - t0 < 0.9, f"{name} waited for the pending corpus"
            assert res.get("success") is not False, (name, res)


# ---- writes in the boot window (x-build-traps 5) --------------------------------------------------------------
def test_a_write_under_a_pending_root_waits_for_its_first_read_and_then_lands(ws, monkeypatch):
    """x-build-traps 5: slice 3 refused a write under a PENDING root at once; slice 4 makes it wait, as a named read waits. A kernel with b1's root
    pending (declared, never read). The write runs on a thread and must still be waiting after 0.6 s (a timer would not prove it: the first dispatch
    can take longer than the timer on a loaded machine); then the root's first read ends, and the write lands. Red on bf94c02: the write was
    refused root-unresponsive at once."""
    _env(ws, monkeypatch, "20")
    from factlattice_engine import config
    from factlattice_engine import tools as T
    from factlattice_engine.kernel import FreshnessKernel
    from factlattice_engine.tools import LtsEngine
    store = config.build_store(ws.registry)
    k = FreshnessKernel()
    store.kernel = k
    root_b1 = os.path.abspath(str(ws.root("b1")))
    slot = k._slot(root_b1)                                  # declared, never read: PENDING
    assert slot.state() == "pending", "setup"
    eng = LtsEngine(store)
    out = {}
    t = threading.Thread(target=lambda: out.update(r=T.dispatch("lts_add_property", {"path": str(ws.file("b1")), "node_id": "B1-N", "key": "stamp", "value": "x"}, tools=eng)))
    t.start()
    try:
        t.join(0.6)
        assert t.is_alive() and "r" not in out, f"the write did not wait for the pending root: {out.get('r')}"
        assert "stamp: x" not in ws.file("b1").read_text(encoding="utf-8")
    finally:
        slot.submit("seat", lambda: None, coalesce=False, foreground=True)        # the root's first read ends
    t.join(30)
    assert out["r"].get("success") is True, out.get("r")
    assert "stamp: x" in ws.file("b1").read_text(encoding="utf-8")


def test_a_write_under_a_pending_root_that_never_answers_is_refused_after_the_deadline_and_writes_nothing(ws, monkeypatch):
    """The other end of the wait: the root is pending and a read of it hangs. The write waits the root deadline, then is refused root-unresponsive,
    the bytes are unchanged and the stalled root saw no call from the write's own thread. A write to the healthy root lands at once, and says
    membership_partial (the control, run first)."""
    _env(ws, monkeypatch)
    from factlattice_engine import config
    from factlattice_engine import tools as T
    from factlattice_engine.kernel import FreshnessKernel
    from factlattice_engine.tools import LtsEngine
    store = config.build_store({"a1": ws.registry["a1"]})
    k = FreshnessKernel()
    store.kernel = k
    root_b1 = os.path.abspath(str(ws.root("b1")))
    slot = k._slot(root_b1)
    eng = LtsEngine(store)
    before = ws.file("b1").read_bytes()
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            slot.submit("seat", lambda: __import__("os").stat(root_b1) and None, coalesce=False, foreground=True)
            ctl = T.dispatch("lts_add_property", {"path": str(ws.file("a1")), "node_id": "A1-N", "key": "ctl", "value": "1"}, tools=eng)
            assert ctl.get("success") is True and ctl["membership_partial"]["marked_count"] == 1, ctl
            t0 = time.monotonic()
            res = run_limited(lambda: T.dispatch("lts_add_property", {"path": str(ws.file("b1")), "node_id": "B1-N", "key": "k", "value": "v"}, tools=eng),
                              DEADLINE_S + 3.0, name="write-b1")
            assert res.done, "the write was still blocked"
            assert res.value["success"] is False and res.value["reason"] == CODE_UNRESPONSIVE, res.value
            assert time.monotonic() - t0 >= DEADLINE_S * 0.8, "it waited for the root deadline first"
            callers = {h[2] for h in stall.hits}
            assert all(c.startswith("lts-root-pool") for c in callers), f"the write touched the root from its own thread: {callers}"
        finally:
            stall.release()
            time.sleep(0.3)
    assert ws.file("b1").read_bytes() == before, "nothing was written"


# ---- the engine over a store that is being loaded ----------------------------------------------------------------
def _async_engine(ws: Workspace, monkeypatch, deadline: str = "5"):
    _env(ws, monkeypatch, deadline)
    from factlattice_engine.server import build_tools
    eng = build_tools(watch=False, async_boot=True)
    _LOADERS.append(eng.boot_loader)
    return eng


def test_a_new_engine_starts_with_every_corpus_pending_and_the_listing_says_so(tmp_path, monkeypatch):
    """x-server: 'After a land every corpus is pending again, by the same rule' (a new process is a new engine). Both roots are slow, so the
    engine returns before either is read: every corpus is in `pending_corpora`, in none of corpus_ids, and the listing reports pending with null
    totals and no wait. Then they join. Red on bf94c02 (SYMBOL: no `async_boot`)."""
    w = Workspace(tmp_path / "w")
    with RootStall(w.root("a1"), cap_s=30, slow_s=0.15, extra_roots=[str(w.root("b1"))]):
        eng = _async_engine(w, monkeypatch)
        assert sorted(eng.store._pending_corpora) == ["a1", "b1"] and eng.store.corpus_ids() == []
        from factlattice_engine import tools as T
        t0 = time.monotonic()
        lst = T.dispatch("lts_list_corpora", {"heal": False}, tools=eng)
        assert time.monotonic() - t0 < 0.5, "the listing does not wait"
        assert [(c["corpus_id"], c["total_nodes"], c["root_health"]["status"]) for c in lst["corpora"]] == [("a1", None, "pending"), ("b1", None, "pending")]
        assert lst["extent"]["index"]["fresh"] is False
        assert eng.registry_error is None, "all pending is not the no-corpus registry error"
        assert eng.boot_loader.first_pass_done.wait(60)
    assert sorted(eng.store.corpus_ids()) == ["a1", "b1"] and not eng.store._pending_corpora and not eng.store._failed_corpora
    done = T.dispatch("lts_list_corpora", {"heal": False}, tools=eng)
    assert [c["total_nodes"] for c in done["corpora"]] == [2, 2] and all("root_health" not in c for c in done["corpora"])


def test_the_all_pending_store_is_safe_for_the_profile_audit_the_seat_and_the_registry_verdict(ws, monkeypatch):
    """x-build-traps 1, each of the three it names, on a store in which every corpus is pending. Red on bf94c02 (SYMBOL: `pending_corpora`,
    `_none_loaded_error`)."""
    _env(ws, monkeypatch)
    from factlattice import registry
    from factlattice_engine import server
    from factlattice_engine.analytics.profile_registration import audit_profile_registration
    from factlattice_engine.kernel import FreshnessKernel
    from factlattice_engine.store import LtsStore
    store = LtsStore()
    for cid in ("a1", "b1"):
        store.declare_pending(cid, {"spec": ws.registry[cid], "private": False, "paths": [str(ws.root(cid))], "since": time.time(), "stuck": False})
    assert server._none_loaded_error(store, registry.resolve_registry(), True) is None, "pending corpora are not 'none loaded'"
    rep = audit_profile_registration(store)
    assert not rep.get("findings"), rep                      # nothing to disagree about yet, and no crash
    k = FreshnessKernel()
    k.seat_store(store)
    assert sorted(store._pending_corpora) == ["a1", "b1"] and not store._failed_corpora, "seat_store neither seats nor fails a pending corpus"
    empty = LtsStore()
    assert server._none_loaded_error(empty, registry.resolve_registry(), True) is not None, "control: a store with nothing at all IS the registry error"


def test_a_registry_whose_every_root_is_missing_becomes_registry_error_mode_after_the_first_pass(tmp_path, monkeypatch):
    """The conversion the async boot does in place of raising: nothing at all could be loaded, so THIS engine takes the registry-error path with the
    same words, and corpus tools answer registry-not-found. (The synchronous boot raises as before: tests/test_registry_*.) Red on bf94c02 (SYMBOL)."""
    w = Workspace(tmp_path / "w", corpora=("m1",))
    import shutil
    shutil.rmtree(w.root("m1"))
    eng = _async_engine(w, monkeypatch)
    assert eng.boot_loader.first_pass_done.wait(30)
    assert eng.registry_error is not None and "none of its corpora could be loaded" in str(eng.registry_error)
    from factlattice_engine import tools as T
    r = T.dispatch("lts_find", {"corpus": "m1", "text": "x"}, tools=eng)
    assert r["success"] is False and r["reason"] == "registry-not-found", r
    assert eng.store.kernel.root_facts() == [], "a corpus that will not be seated gave back the pending slot it declared (else the missing root reads pending for ever)"


def test_the_first_call_after_the_handshake_of_a_registry_with_nothing_loadable_is_still_registry_not_found(tmp_path):
    """GUARD (green on bf94c02, where the verdict came before the handshake): the registry-error mode must look the same to the first caller. Two
    corpora whose roots are missing; the very first corpus-scoped call after `initialize` waits for the first pass (a verdict not yet made must not
    be answered as `no-roots`), then gets `registry-not-found`; the listing then shows registry_choice.error and ZERO corpora."""
    w2 = Workspace(tmp_path / "w2", corpora=("m1", "m2"))
    import shutil
    shutil.rmtree(w2.root("m1"))
    shutil.rmtree(w2.root("m2"))
    with RealServer(w2, env=DEADLINES) as srv:
        srv.initialize(timeout=10)
        res, _ = srv.call("lts_find", {"corpus": "m1", "text": "x"}, timeout=10)
        assert res["success"] is False and res["reason"] == "registry-not-found", res
        un, _ = srv.call("lts_find", {"text": "x", "tier": "digest"}, timeout=10)
        assert un["reason"] == "registry-not-found", un
        lst, _ = srv.call("lts_list_corpora", {"heal": False}, timeout=10)
        assert lst["count"] == 0 and "none of its corpora could be loaded" in lst["registry_choice"]["error"], lst


def test_register_and_unregister_of_a_pending_corpus(tmp_path, monkeypatch):
    """A second registration of a pending id would race the loader on the same files (and, on a hung root, stop the request thread); an
    unregistration of one stops its read from being published. Red on bf94c02 (SYMBOL)."""
    w = Workspace(tmp_path / "w")
    with RootStall(w.root("a1"), cap_s=30, slow_s=0.2, extra_roots=[str(w.root("b1"))]) as stall:
        eng = _async_engine(w, monkeypatch)
        assert "a1" in eng.store._pending_corpora
        r = eng.register_corpus("a1")
        assert r["success"] is False and "still being read" in r["error"], r
        u = eng.unregister_corpus("b1")
        assert u["success"] is True and u.get("was_pending") is True and "b1" not in eng.store._pending_corpora
        assert eng.boot_loader.first_pass_done.wait(60)
        stall.release()
    assert eng.store.corpus_ids() == ["a1"], "the unregistered corpus was not published by its still-running read"
    assert "b1" not in eng.store._failed_corpora and not eng.store._pending_corpora


def test_a_private_pending_corpus_is_counted_and_never_named(tmp_path, monkeypatch):
    """[INV-PRIVATE-FANOUT]: a pending private corpus has no partition to carry its privacy, so the pending record does. The listing, an unnamed
    read's skips and the error for an unknown corpus never carry its id; a call that names it waits like any other. Red on bf94c02 (SYMBOL)."""
    w = Workspace(tmp_path / "w", corpora=("a1", "me-personality"))
    from factlattice_engine import tools as T
    with RootStall(w.root("me-personality"), cap_s=30, slow_s=0.2):
        eng = _async_engine(w, monkeypatch, "5")
        assert "me-personality" in eng.store._pending_corpora and eng.store._pending_corpora["me-personality"]["private"] is True
        lst = T.dispatch("lts_list_corpora", {"heal": False}, tools=eng)
        assert "me-personality" not in json.dumps(lst) and lst["private_failed"] == 1, lst.get("private_failed")
        assert eng.boot_loader.first_pass_done.wait(60)
    # after the load: the private corpus serves a call that names it, and never appears in an unnamed one
    named = T.dispatch("lts_find", {"corpus": "me-personality", "text": "lanternfish", "tier": "digest"}, tools=eng)
    assert named["success"] and named["count"] == 1


def test_a_hung_private_corpus_is_declared_by_an_unnamed_read_as_an_anonymous_count(tmp_path, monkeypatch):
    """[INV-PRIVATE-FANOUT] for a private corpus that is STILL pending when an unnamed read has waited: the read leaves it out and declares it, but only
    as the anonymous private count (`private-corpus-failed`, count 1), never by id; a call that names it exactly is refused root-unresponsive as any
    other corpus is. Red on bf94c02 (SYMBOL)."""
    w = Workspace(tmp_path / "w", corpora=("a1", "me-personality"))
    from factlattice_engine import tools as T
    with RootStall(w.root("me-personality"), cap_s=30):
        eng = _async_engine(w, monkeypatch, "0.5")
        un = T.dispatch("lts_find", {"text": "lanternfish", "tier": "digest"}, tools=eng)
        assert un["success"] and un["count"] == 1, un                # (`complete` is not touched: an unnamed read never reaches a private corpus, hung or not)
        assert "me-personality" not in json.dumps(un), "an unnamed read never names a private corpus"
        assert {"reason": "private-corpus-failed", "count": 1} in un["extent"]["corpora_skipped"], un["extent"]["corpora_skipped"]
        named = T.dispatch("lts_find", {"corpus": "me-personality", "text": "x"}, tools=eng)
        assert named["success"] is False and named["reason"] == CODE_UNRESPONSIVE, named
        lst = T.dispatch("lts_list_corpora", {"heal": False}, tools=eng)
        assert "me-personality" not in json.dumps(lst) and lst["private_failed"] == 1


def test_a_corpus_omitted_read_waits_for_the_pending_ones_then_declares_what_is_still_unread(tmp_path, monkeypatch):
    """x-server: 'A corpus-omitted read during that time waits the same way for an unread root, then leaves it out and declares it.' c1 is served at
    once (so the store is not empty and the boot-verdict wait of the registry gate does not apply); a1 takes 0.4 s to read, under the deadline, and
    must be WAITED for and served; b1 hangs and must be declared. Red on bf94c02 (SYMBOL)."""
    w = Workspace(tmp_path / "w", corpora=("a1", "b1", "c1"))
    from factlattice_engine import incremental
    from factlattice_engine import tools as T
    real_read = incremental.CorpusIndex.read_root

    def slow_a1(self, spec):                              # a pause inside a1's read job, under the 1 s deadline; no disk call is held
        if str(spec["path"]).endswith("a1"):
            time.sleep(0.4)
        return real_read(self, spec)
    monkeypatch.setattr(incremental.CorpusIndex, "read_root", slow_a1)
    with RootStall(w.root("b1"), cap_s=30):
        eng = _async_engine(w, monkeypatch, "1")
        assert _wait(lambda: "c1" in eng.store.corpus_ids(), 5) and "a1" in eng.store._pending_corpora, "setup: c1 served, a1 still unread"
        t0 = time.monotonic()
        res = T.dispatch("lts_find", {"text": "lanternfish", "tier": "digest"}, tools=eng)
        took = time.monotonic() - t0
        assert res["success"] and res["count"] == 2 and res["complete"] is False, ("a1 must be WAITED for and served beside c1", res)
        assert {"corpus": "a1", "reason": "root-pending"} not in res["extent"]["corpora_skipped"]
        assert {"corpus": "b1", "reason": "root-unresponsive"} in res["extent"]["corpora_skipped"] or \
               {"corpus": "b1", "reason": "root-pending"} in res["extent"]["corpora_skipped"], res["extent"]["corpora_skipped"]
        assert 0.3 <= took < 3.0, f"it waited the root deadline once, for all pending corpora together: {took:.2f}s"


def test_a_call_naming_a_pending_corpus_waits_while_other_corpora_are_served_and_is_then_answered(tmp_path, monkeypatch):
    """The gate's own wait, with the store NOT empty (c1 served), so the registry gate's boot-verdict wait does not stand in for it. The loader is held
    before a1; a call naming a1 waits (still waiting after 0.6 s) and, released, gets the answer, never a refusal. Red on bf94c02 (SYMBOL)."""
    w = Workspace(tmp_path / "w", corpora=("a1", "c1"))
    from factlattice_engine import bootload
    from factlattice_engine import tools as T
    go = threading.Event()
    real_load = bootload.StoreLoader._load_one

    def held(self, cid):
        if cid == "a1":
            assert go.wait(30)
        return real_load(self, cid)
    monkeypatch.setattr(bootload.StoreLoader, "_load_one", held)
    eng = _async_engine(w, monkeypatch, "20")
    out = {}
    try:
        assert _wait(lambda: "c1" in eng.store.corpus_ids(), 5), "setup: c1 served"
        t = threading.Thread(target=lambda: out.update(r=T.dispatch("lts_find", {"corpus": "a1", "text": "lanternfish", "tier": "digest"}, tools=eng)))
        t.start()
        t.join(0.6)
        assert t.is_alive() and "r" not in out, f"a call naming a pending corpus must wait, not be answered: {out.get('r')}"
    finally:
        go.set()
    t.join(30)
    assert out["r"]["success"] is True and out["r"]["count"] == 1, out["r"]


def test_a_write_to_a_served_corpus_while_another_is_still_pending_says_membership_partial(tmp_path, monkeypatch):
    """The slots the boot declares make the write's reading of the roots whole about what it does not know: with b1 not yet read (the loader is held
    before it), a write under a1 lands and says `membership_partial: {marked_count: 1}`, and a change that could be an edge key is not let through
    unchecked. Red on bf94c02 (SYMBOL)."""
    w = Workspace(tmp_path / "w")
    from factlattice_engine import bootload
    from factlattice_engine import tools as T
    go = threading.Event()
    real_load = bootload.StoreLoader._load_one

    def held(self, cid):
        if cid == "b1":
            assert go.wait(30)
        return real_load(self, cid)
    monkeypatch.setattr(bootload.StoreLoader, "_load_one", held)
    eng = _async_engine(w, monkeypatch, "20")
    try:
        assert _wait(lambda: "a1" in eng.store.corpus_ids(), 5), "setup: a1 served, b1 held"
        r = T.dispatch("lts_add_property", {"path": str(w.file("a1")), "node_id": "A1-N", "key": "stamp", "value": "x"}, tools=eng)
        assert r.get("success") is True and r["membership_partial"] == {"marked_count": 1, "unconfirmed_count": 0}, r.get("membership_partial")
    finally:
        go.set()


def test_a_write_in_the_boot_window_waits_for_its_root_and_lands_in_its_own_corpus(tmp_path, monkeypatch):
    """x-build-traps 2 and 5 for a corpus that is pending: the root has a slot from the moment the engine exists (declared before anything is read), so
    a write at once finds it PENDING and waits, instead of finding no root at all and recording the file as outside every registered root. Red on
    bf94c02 (SYMBOL: no async boot)."""
    w = Workspace(tmp_path / "w")
    from factlattice_engine import tools as T
    with RootStall(w.root("b1"), cap_s=30, slow_s=0.1):
        eng = _async_engine(w, monkeypatch, "10")
        assert "b1" in eng.store._pending_corpora, "setup: b1 must still be pending when the write arrives"
        r = T.dispatch("lts_add_property", {"path": str(w.file("b1")), "node_id": "B1-N", "key": "stamp", "value": "x"}, tools=eng)
    assert r.get("success") is True, r
    assert r["root_membership"]["inside_corpus"] is True and r["root_membership"]["corpora"] == ["b1"], r["root_membership"]
    assert "stamp: x" in w.file("b1").read_text(encoding="utf-8")


def test_the_ledger_table_is_pinned_after_the_first_pass_and_a_hung_root_does_not_stop_the_boot(tmp_path, monkeypatch):
    """`ledger.context_from_file` resolves every declared root, which on a hung root stops the handshake: the async boot pins after the first pass,
    on a worker with the deadline. Healthy: pinned when the first pass ends. Hung root: the boot is up and answering, the table is NOT pinned yet
    (the ledger reads it per call), and the pin is retried by the watcher cycle until the root answers. Red on bf94c02 (SYMBOL)."""
    w = Workspace(tmp_path / "w")
    from factlattice import ledger
    eng = _async_engine(w, monkeypatch)
    assert eng.boot_loader.first_pass_done.wait(30)
    assert ledger.pin_is_final(), "pinned for good after the first pass"
    ledger.unpin_table()
    w2 = Workspace(tmp_path / "w2")
    with RootStall(w2.root("b1"), cap_s=30) as stall:
        try:
            t0 = time.monotonic()
            eng2 = _async_engine(w2, monkeypatch, "0.5")
            assert time.monotonic() - t0 < 1.5, "the engine is returned without waiting for the declared roots"
            assert eng2.boot_loader.first_pass_done.wait(10)
            assert not ledger.pin_is_final() and ledger._PINNED is not None, "only the PROVISIONAL (booted) table while a declared root does not answer"
            tick = eng2.store.kernel
            stall.release()
            assert _wait(lambda: (tick.tick(), ledger.pin_is_final())[1], 15, step=0.3), "pinned for good by a watcher cycle once the root answers"
        finally:
            stall.release()
            time.sleep(0.3)


def test_seating_a_corpus_whose_root_stops_answering_is_bounded_and_coded(ws, monkeypatch):
    """`seat_partition` waited for its jobs with no limit. Now it ends when the root has made no progress for the deadline, rolls back completely
    (nothing half-seated), and `seat_store` records the corpus as failed `corpus-root-unresponsive`. Red on bf94c02: the seat never returned."""
    _env(ws, monkeypatch)
    from factlattice_engine import config
    from factlattice_engine.kernel import FreshnessKernel
    store = config.build_store({"b1": ws.registry["b1"]})
    part = store.get_partition("b1")
    k = FreshnessKernel()
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            out = run_limited(lambda: k.seat_store(store), DEADLINE_S + 3.0, name="seat")
            assert out.done and out.error is None, f"the seat did not return (blocked in {stall.hit_names()})"
            assert store._failed_corpora["b1"] == {"code": CODE_BUILD, "path": None, "private": False, "domain_suffix": "x.b1"}, store._failed_corpora
            assert "b1" not in store.corpus_ids() and k.current_root(part) is None and k.tracked_roots(part) is None, "nothing half-seated"
        finally:
            stall.release()
            time.sleep(0.3)


def test_register_corpus_rolls_back_whole_when_the_seat_does_not_answer(ws, monkeypatch):
    """`seat_partition`'s other caller, `lts_register_corpus` (the request thread builds, the root's worker seats): when the seat gets no answer the
    registration is rolled back COMPLETELY (not in the store, not tracked by the kernel, not listed as failed) with the build-root refusal and no path,
    and registering again after the root answers succeeds. Only the root's worker threads are held (`engaged_fn`), so the build, which runs on the
    caller's thread here, reaches the seat. Red on bf94c02: the seat never returned."""
    _env(ws, monkeypatch)
    from factlattice_engine import config
    from factlattice_engine.kernel import FreshnessKernel
    from factlattice_engine.tools import LtsEngine
    store = config.build_store({"a1": ws.registry["a1"]})
    store.kernel = FreshnessKernel()
    eng = LtsEngine(store)
    with RootStall(ws.root("b1"), cap_s=30, engaged_fn=lambda: threading.current_thread().name.startswith("lts-root-pool")) as stall:
        try:
            out = run_limited(lambda: eng.register_corpus("b1", spec=ws.registry["b1"]), DEADLINE_S + 3.0, name="register")
            assert out.done and out.error is None, f"register did not return (blocked in {stall.hit_names()})"
            r = out.value
            assert r["success"] is False and r["reason"] == CODE_BUILD and r.get("registered") is False, r
            assert str(ws.root("b1")) not in json.dumps(r)
            assert "b1" not in store.corpus_ids() and "b1" not in store._failed_corpora and store.kernel.tracked_roots(type("P", (), {"corpus_id": "b1"})()) is None
        finally:
            stall.release()
            time.sleep(0.3)
    from factlattice_engine import boundedfs
    assert _wait(lambda: boundedfs.registry().stuck_workers() == 0, 5)
    again = eng.register_corpus("b1", spec=ws.registry["b1"])
    assert again["success"] is True and "b1" in store.corpus_ids(), again


def test_a_safe_read_also_reports_progress_for_its_open_phase(tmp_path):
    """`_open_regular` serves two readers (`file_content_hash` and `safe_read_bytes`); the three reports after its lstat, open and fstat are in the
    shared function, so both get them. Red on bf94c02: no report before the read."""
    import factlattice.hashing as H
    f = tmp_path / "x.lts"
    f.write_bytes(b"x" * 10)
    calls = []
    prev = H.progress_hook
    H.progress_hook = lambda: calls.append(1)
    try:
        H.safe_read_bytes(str(f))
    finally:
        H.progress_hook = prev
    assert len(calls) == 3, len(calls)       # lstat, open, fstat: the file is read after, with no chunk report of its own


def test_a_waiter_for_a_first_read_stops_waiting_when_its_root_is_given_back(ws):
    """`RootSlot.wait_not_pending` on a slot the registry has dropped (a missing or empty root that will not be seated) returns at once instead of
    waiting the whole deadline. Red on bf94c02: it waited the full timeout."""
    from factlattice_engine.rootstate import RootRegistry
    reg = RootRegistry(5.0)
    slot = reg.slot("/nowhere/x")
    threading.Timer(0.2, lambda: reg.drop("/nowhere/x")).start()
    t0 = time.monotonic()
    st = slot.wait_not_pending(3.0)
    assert time.monotonic() - t0 < 1.5 and st == "pending", f"waited {time.monotonic() - t0:.2f}s"


def test_a_root_stays_pending_until_its_corpus_is_seated_and_published(tmp_path, monkeypatch):
    """A server's root is settled by the SEAT, not by the read that built the corpus: between the two the corpus is not served, so its root must not
    read ok (a write in that window would then see an ok root and no corpus). The seat is held on an event; the read has ended; the root is still
    pending and the corpus still in `pending_corpora`. Red on bf94c02 (SYMBOL)."""
    w = Workspace(tmp_path / "w", corpora=("a1",))
    from factlattice_engine import kernel as K
    reached, go = threading.Event(), threading.Event()
    real_seat = K.FreshnessKernel.seat_partition

    def held_seat(self, part):
        reached.set()
        assert go.wait(30)
        return real_seat(self, part)
    monkeypatch.setattr(K.FreshnessKernel, "seat_partition", held_seat)
    eng = _async_engine(w, monkeypatch, "10")
    try:
        assert reached.wait(20), "setup: the read ended and the seat was reached"
        reg = eng.store.kernel.roots
        assert reg.state_of(os.path.abspath(str(w.root("a1")))) == "pending", "read, not yet seated: still pending"
        assert "a1" in eng.store._pending_corpora and eng.store.corpus_ids() == []
    finally:
        go.set()
    assert eng.boot_loader.first_pass_done.wait(20)
    assert eng.store.kernel.roots.state_of(os.path.abspath(str(w.root("a1")))) == "ok" and eng.store.corpus_ids() == ["a1"]



def test_a_write_before_the_loader_has_reached_its_root_waits_on_the_slot_the_boot_declared(tmp_path, monkeypatch):
    """The slot a pending root needs exists from the moment the engine does (declared with no disk read), not from the moment the loader reaches it.
    The loader is held before b1; a write to b1's file arrives: it must WAIT (the root is pending, its corpus unread), not proceed on 'no such root'.
    Released, it lands inside b1's corpus. Red on bf94c02 (SYMBOL)."""
    w = Workspace(tmp_path / "w")
    from factlattice_engine import bootload
    from factlattice_engine import tools as T
    go = threading.Event()
    real_load = bootload.StoreLoader._load_one

    def held(self, cid):
        if cid == "b1":
            assert go.wait(30)
        return real_load(self, cid)
    monkeypatch.setattr(bootload.StoreLoader, "_load_one", held)
    eng = _async_engine(w, monkeypatch, "20")
    out = {}
    t = threading.Thread(target=lambda: out.update(r=T.dispatch("lts_add_property", {"path": str(w.file("b1")), "node_id": "B1-N", "key": "stamp", "value": "x"}, tools=eng)))
    t.start()
    try:
        t.join(0.8)
        assert t.is_alive() and "r" not in out, "the write went ahead of the first read of its root"
        assert "stamp: x" not in w.file("b1").read_text(encoding="utf-8")
    finally:
        go.set()
    t.join(30)
    assert out["r"].get("success") is True and out["r"]["root_membership"]["corpora"] == ["b1"], out.get("r")


def test_a_write_in_the_gap_between_a_root_turning_ok_and_its_corpus_being_published_waits(tmp_path, monkeypatch):
    """The gap is real: a root turns ok when its seat ends, and the corpus is published an instant later. A write that arrives in it sees an ok root
    and no corpus, and used to be recorded as outside every registered corpus. The publish is held on an event so the gap lasts as long as the test
    wants; the write must wait for the corpus, then land inside it. Red on bf94c02 (SYMBOL)."""
    w = Workspace(tmp_path / "w", corpora=("a1",))
    from factlattice_engine import store as S
    from factlattice_engine import tools as T
    reached, go = threading.Event(), threading.Event()
    real_settle = S.LtsStore.settle

    def held(self, cid, **kw):
        if kw.get("partition") is not None:
            reached.set()
            assert go.wait(30)
        return real_settle(self, cid, **kw)
    monkeypatch.setattr(S.LtsStore, "settle", held)
    eng = _async_engine(w, monkeypatch, "20")
    try:
        assert reached.wait(20)
        root = os.path.abspath(str(w.root("a1")))
        assert _wait(lambda: eng.store.kernel.roots.state_of(root) == "ok", 5), "setup: the root is ok and the corpus is not yet published"
        assert eng.store.corpus_ids() == [] and "a1" in eng.store._pending_corpora
        out = {}
        t = threading.Thread(target=lambda: out.update(r=T.dispatch("lts_add_property", {"path": str(w.file("a1")), "node_id": "A1-N", "key": "stamp", "value": "x"}, tools=eng)))
        t.start()
        t.join(0.8)
        assert t.is_alive() and "r" not in out, "the write went ahead in the gap"
    finally:
        go.set()
    t.join(30)
    assert out["r"].get("success") is True and out["r"]["root_membership"]["corpora"] == ["a1"] and out["r"]["root_membership"]["inside_corpus"] is True, out.get("r")


# ======================================================================================================================
# x-render: the consumer's flow
# ======================================================================================================================
def _consumer_expand(registry_path: Path, corpus: str, empty_ok: bool) -> dict[str, str]:
    """What the consumer that renders agent definitions does for `{{lts: <corpus>:<selector> [empty=ok]}}`, step for step: its default engine builder
    is `LtsEngine(build_store({corpus: spec}), self_heal=True)`, then `find(corpus=..., tier="full")`, and a failure banner when `success` is false,
    BEFORE the zero-nodes branch that `empty=ok` softens. Returns {status, reason}."""
    from factlattice_engine.config import build_store
    from factlattice_engine.tools import LtsEngine
    spec = json.loads(Path(registry_path).read_text())[corpus]
    eng = LtsEngine(build_store({corpus: spec}), self_heal=True)
    res = eng.find(corpus=corpus, tier="full", token_budget=20000)
    if not res.get("success", False):
        return {"status": "failed", "reason": f"lts_find failed: {res.get('error') or res.get('reason') or res}"}
    nodes = [n for n in (res.get("nodes") or [])]
    if not nodes:
        return {"status": "empty-ok" if empty_ok else "failed", "reason": "0 nodes matched"}
    return {"status": "ok", "reason": ""}


@pytest.mark.parametrize("empty_ok", [False, True])
def test_a_stalled_root_reaches_the_renderers_failure_path_with_and_without_empty_ok(ws, monkeypatch, empty_ok):
    """x-render + x-c6: the refusal is `success: false`, which the renderer shows as its expansion-failed banner, also under `empty=ok`, and never
    as an empty digest. The negative controls: a healthy corpus renders ok, and a healthy corpus with zero matches under `empty=ok` renders the
    empty NOTICE (so the two outcomes the banner could be confused with are shown to differ). Red on bf94c02: build_store never returned."""
    _env(ws, monkeypatch)
    assert _consumer_expand(ws.registry_path, "a1", empty_ok)["status"] == "ok", "control: a healthy corpus renders"
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            out = run_limited(lambda: _consumer_expand(ws.registry_path, "b1", empty_ok), DEADLINE_S + 3.0, name="render")
            assert out.done and out.error is None, f"the render did not return (blocked in {stall.hit_names()})"
            assert out.value["status"] == "failed", ("it must be a FAILURE, not an empty digest", out.value)
            assert CODE_BUILD in out.value["reason"] or "RootUnresponsive" in out.value["reason"], out.value
            assert str(ws.root("b1")) not in out.value["reason"]
        finally:
            stall.release()
            time.sleep(0.3)


#: the consumer's own expander, if its checkout is on this machine: FL_RENDERER_CHECKOUT is the directory to put on the path, FL_RENDERER_IMPORT is
#: "module:Class" (the class takes a `registry_resolver` and has `.expand(raw) -> outcome` with `.status` and `.text`). Neither is set in the public suite.
_RENDERER_CHECKOUT, _RENDERER_IMPORT = os.environ.get("FL_RENDERER_CHECKOUT", ""), os.environ.get("FL_RENDERER_IMPORT", "")


@pytest.mark.skipif(not (_RENDERER_CHECKOUT and _RENDERER_IMPORT and Path(_RENDERER_CHECKOUT).is_dir()),
                    reason="the consumer's checkout is not named (FL_RENDERER_CHECKOUT, FL_RENDERER_IMPORT)")
@pytest.mark.parametrize("empty_ok", [False, True])
def test_the_real_renderer_shows_its_banner_for_a_stalled_root(ws, monkeypatch, empty_ok):
    """The same, through the consumer's REAL expander (read from its checkout, put on the path for this test only, never edited): its default engine
    builder, its banner text. Skipped where the checkout is not named; the consumer's own suite is where its owner runs it."""
    _env(ws, monkeypatch)
    monkeypatch.syspath_prepend(_RENDERER_CHECKOUT)
    import importlib
    mod, _, cls = _RENDERER_IMPORT.partition(":")
    LtsExpander = getattr(importlib.import_module(mod), cls)
    ex = LtsExpander(registry_resolver=lambda: (str(ws.registry_path), "test"))
    sel = "b1:B1-*" + (" empty=ok" if empty_ok else "")
    ok = ex.expand("a1:A1-*" + (" empty=ok" if empty_ok else ""))
    assert ok.status == "ok", ("control", ok.status, ok.reason)
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        try:
            out = run_limited(lambda: ex.expand(sel), DEADLINE_S + 4.0, name="renderer")
            assert out.done and out.error is None, f"the real render did not return (blocked in {stall.hit_names()})"
            o = out.value
            assert o.status == "failed" and "FAILED" in o.text, (o.status, o.text)
            assert "0 nodes matched" not in o.text
        finally:
            stall.release()
            time.sleep(0.3)


# ======================================================================================================================
# the harness, for a consumer's suite  (x-render: 'the stall harness is an importable module')
# ======================================================================================================================
def test_the_stall_harness_imports_with_the_standard_library_alone_and_blocks_a_planted_site(tmp_path):
    """A consumer's suite puts tests/ on its path and imports `stall_harness`. Run in a fresh interpreter in ISOLATED mode (`-I`: no PYTHONPATH,
    no cwd, no user site, so neither the engine nor this repo's `tests` package can be found): it must import, wrap a stat under a root, hold a
    call there until released, and leave the engine unimported. GUARD: green on bf94c02 (the module was already standard-library only); it fails
    on any tree where the module starts to import the engine or this repo's `tests` package."""
    tests_dir = Path(__file__).resolve().parent
    root = tmp_path / "r"
    root.mkdir()
    (root / "f").write_text("x")
    script = textwrap.dedent(f"""
        import sys, os, threading, time
        sys.path.insert(0, {str(tests_dir)!r})
        import stall_harness as H
        assert "factlattice" not in sys.modules and "factlattice_engine" not in sys.modules and "tests" not in sys.modules
        done = {{}}
        with H.RootStall({str(root)!r}, cap_s=20) as st:
            out = H.run_limited(lambda: os.stat({str(root / "f")!r}), 0.5)
            assert not out.done, "a stat under the root must be held"
            ok = os.stat({str(tmp_path)!r})
            st.release()
            out.thread.join(5)
            assert out.done and out.error is None
        print("HARNESS-OK")
        """)
    p = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=60, cwd="/")
    assert p.returncode == 0 and "HARNESS-OK" in p.stdout, (p.stdout, p.stderr)


# ======================================================================================================================
# ROUND 2 (cold read of 04cc80c)
# ======================================================================================================================
def _hop_world(tmp_path, name="w") -> Workspace:
    w = Workspace(tmp_path / name)
    with open(w.file("a1"), "a", encoding="utf-8") as fh:
        fh.write("- depends-on: [x.b1:SPEC:B1-N]\n")
    return w


_HOP_CALLS = (("lts_node", {"corpus": "a1", "node_id": "A1-N"}), ("lts_node", {"corpus": "a1", "node_id": "A1-N", "tier": "digest"}),
              ("lts_traverse", {"corpus": "a1", "start_node": "A1-N", "max_depth": 1}), ("lts_capabilities", {"corpus": "a1", "node_id": "A1-N"}))


def test_a_hop_into_a_pending_corpus_is_declared_stale_by_node_digest_traverse_and_capabilities(tmp_path):
    """P1 of the cold read: a1's node has an edge into b1; b1 hangs at boot. Every tool that follows a routed hop (the four callers of `_route`) must
    DECLARE it in `routed_stale` with `root-pending` (then `root-unresponsive`), never drop it as unresolved: routed_nodes empty, digest and
    capabilities likewise. Control: with b1 healthy the same calls route the node. Real server. Red on 04cc80c: routed_stale was [] (the hop vanished)."""
    with RealServer(_hop_world(tmp_path, "ok"), env=DEADLINES) as srv:                      # control: nothing hangs, the hop is routed
        srv.initialize(timeout=5)
        assert _wait(lambda: srv.call("lts_node", {"corpus": "a1", "node_id": "A1-N"}, timeout=5)[0].get("routed_nodes"), 20, step=0.2), "control: the hop routes"
    w = _hop_world(tmp_path, "hung")
    with RealServer(w, stall_root=w.root("b1"), engaged=True, env=DEADLINES) as srv:
        srv.initialize(timeout=3)
        seen = set()
        for _round in range(2):
            for name, args in _HOP_CALLS:
                out, _dt = srv.call(name, args, timeout=10)
                assert out["success"] is True, (name, out)
                stale = out.get("routed_stale") or []
                assert [x for x in stale if x.get("corpus") == "b1" and x.get("reason") in ("root-pending", CODE_UNRESPONSIVE)], (name, args.get("tier"), "the hop vanished", stale)
                assert not (out.get("routed_nodes") or out.get("routed_digest") or out.get("routed_capabilities")), (name, "nothing is read from b1")
                seen.add(next(x["reason"] for x in stale if x.get("corpus") == "b1"))
            time.sleep(1.4)                                          # past the 1 s root deadline: pending has become unresponsive
        assert seen == {"root-pending", CODE_UNRESPONSIVE} or seen == {CODE_UNRESPONSIVE}, seen


def test_a_hop_into_a_corpus_that_failed_on_its_root_is_declared_stale_and_a_private_one_is_withheld(ws, monkeypatch):
    """The same declaration for a corpus that FAILED at build (library store): `root-unresponsive`; and a PRIVATE pending or failed corpus is the
    withheld stub, never named. Red on 04cc80c: the hop was dropped."""
    _env(ws, monkeypatch)
    with open(ws.file("a1"), "a", encoding="utf-8") as fh:
        fh.write("- depends-on: [x.b1:SPEC:B1-N]\n- relates-to: [x.pv:SPEC:PV-N]\n")
    from factlattice_engine import config
    from factlattice_engine import tools as T
    from factlattice_engine.tools import LtsEngine
    store = config.build_store({"a1": ws.registry["a1"]})
    store._failed_corpora["b1"] = {"code": CODE_BUILD, "path": None, "private": False, "domain_suffix": "x.b1"}
    store._failed_corpora["pv-personality"] = {"code": CODE_BUILD, "path": None, "private": True, "domain_suffix": "x.pv"}
    eng = LtsEngine(store)
    out = T.dispatch("lts_node", {"corpus": "a1", "node_id": "A1-N"}, tools=eng)
    assert [x for x in out["routed_stale"] if x["corpus"] == "b1" and x["reason"] == CODE_UNRESPONSIVE], out.get("routed_stale")
    assert out.get("routed_withheld") and "pv-personality" not in json.dumps(out), "a private corpus's hop is withheld and its id never named"


# ---- P2-1: the table that is pinned is the one the server booted from ------------------------------------------------
def test_the_pinned_ledger_table_is_the_one_the_server_booted_from_not_an_edited_registry(tmp_path, monkeypatch):
    """INV-NR-ONE-TABLE with the late pin: b1's root hangs at boot, so the pin waits; the operator edits the registry (a different named-roots state
    folder) during the stall; the stall ends and the pin lands. It must be the table of the bytes the store was built from, not of the edited file.
    Control: no stall pins at the first pass with the booted table. Red on 04cc80c: the edited table was pinned."""
    from factlattice import ledger
    out = {}
    for name, stall in (("ctl", False), ("stalled", True)):
        w = Workspace(tmp_path / name)
        s1, s2 = tmp_path / name / "stateA", tmp_path / name / "stateB"

        def regdata(state, w=w):
            d = json.loads(w.registry_path.read_text())
            d["named_roots"] = {"data": {"path": str(w.root("a1")), "state": str(state)}}
            return d
        w.registry_path.write_text(json.dumps(regdata(s1)))
        _env(w, monkeypatch, "0.5")
        ledger.unpin_table()
        cm = RootStall(w.root("b1"), cap_s=40) if stall else None
        if cm:
            cm.__enter__()
        try:
            from factlattice_engine.server import build_tools
            eng = build_tools(watch=False, async_boot=True)
            _LOADERS.append(eng.boot_loader)
            w.registry_path.write_text(json.dumps(regdata(s2)))                       # edited while the server is up, BEFORE the first pass ends
            assert eng.boot_loader.first_pass_done.wait(15)
            assert ledger.pin_is_final() is (not stall), "setup: pinned for good at the first pass only when no root hangs"
            out[name + "-window"] = {n: v.get("state") for n, v in (ledger.table_context().table or {}).items()}        # a read BEFORE the final pin
            if cm:
                cm.release()
            k = eng.store.kernel
            assert _wait(lambda: (k.tick(), ledger.pin_is_final())[1], 15, step=0.2)
            out[name] = {n: v.get("state") for n, v in (ledger._PINNED.table or {}).items()}
        finally:
            if cm:
                cm.release()
                time.sleep(0.3)
                cm.__exit__(None, None, None)
    assert out["ctl"] == {"data": str(tmp_path / "ctl" / "stateA")}, out
    assert out["stalled"] == {"data": str(tmp_path / "stalled" / "stateA")}, ("the pinned table is the booted one, not the edit", out)
    assert out["stalled-window"] == {"data": str(tmp_path / "stalled" / "stateA")}, ("a table read before the final pin goes by the boot bytes too", out)


# ---- P2-2: a write under a healthy root of a corpus whose sibling root is marked refuses at once -------------------
def test_a_write_under_a_healthy_root_refuses_at_once_once_a_sibling_root_is_marked_and_waits_while_it_is_only_pending(tmp_path):
    """Corpus m1 has roots m1a (healthy) and m1b (hung). While the sibling is merely PENDING a write under m1a waits (as for any pending root) and is
    then refused root-unresponsive; once the sibling is MARKED unresponsive the next attempt is refused at once, with no root wait. Control: a write
    under a1 lands. Real server, root deadline 2 s. Red on 04cc80c: 2.0 s on every attempt."""
    base = tmp_path / "w6"
    w = Workspace(base, corpora=("a1",))
    from tests.stall_harness import _DOC
    for c in ("m1a", "m1b"):
        (base / "c" / c).mkdir(parents=True)
        (base / "c" / c / f"{c}.lts").write_text(_DOC.format(c=c, u=c.upper()), encoding="utf-8")
    reg = {"a1": w.registry["a1"], "m1": {"roots": [{"path": str(base / "c" / "m1a")}, {"path": str(base / "c" / "m1b")}], "domain_suffix": "x.m1"}}
    w.registry_path.write_text(json.dumps(reg))
    env = {"LTS_ROOT_DEADLINE_S": "2", "LTS_CALL_DEADLINE_S": "4", "LTS_CALL_WORKERS": "1"}

    def write(srv, f, nid, key):
        t0 = time.monotonic()
        out, _ = srv.call("lts_add_property", {"path": str(f), "node_id": nid, "key": key, "value": "1"}, timeout=30)
        return out, time.monotonic() - t0
    with RealServer(w, stall_root=base / "c" / "m1b", engaged=True, env=env) as srv:
        srv.initialize(timeout=3)
        ctl, _ = write(srv, w.file("a1"), "A1-N", "ctl")
        assert ctl["success"] is True, ctl
        first, dt1 = write(srv, base / "c" / "m1a" / "m1a.lts", "M1A-N", "k1")
        assert first["reason"] == CODE_UNRESPONSIVE and dt1 >= 1.0, ("while the sibling is merely pending the write waits first", dt1, first.get("reason"))
        assert _wait(lambda: entry(srv.call("lts_list_corpora", {"heal": False}, timeout=5)[0], "m1")["root_health"]["status"] == "unresponsive", 10), "setup: m1 is marked"
        second, dt2 = write(srv, base / "c" / "m1a" / "m1a.lts", "M1A-N", "k2")
        assert second["reason"] == CODE_UNRESPONSIVE and dt2 < 1.0, ("once the sibling is marked the write is refused at once", dt2)
        assert "k1: 1" not in (base / "c" / "m1a" / "m1a.lts").read_text() and "k2: 1" not in (base / "c" / "m1a" / "m1a.lts").read_text()


# ---- P2-3: the profile read is bounded; the blocking join is bounded --------------------------------------------------
def _profile_world(tmp_path, name="w9"):
    w = Workspace(tmp_path / name, corpora=("p1", "p2", "p3", "p4", "a1", "b1"))
    reg = json.loads(w.registry_path.read_text())
    for p in ("p1", "p2", "p3", "p4"):
        reg[p]["profile"] = str(w.root("b1") / "prof.json")                         # a profile that lives under b1's root
    w.registry_path.write_text(json.dumps(reg))
    return w, reg


def test_build_store_returns_inside_the_deadline_when_profiles_live_under_a_hung_root(tmp_path, monkeypatch):
    """P2-3, library: four corpora name a profile under b1's hung root. build_store must return (inside about one deadline), the four and b1 failed
    `corpus-root-unresponsive`, a1 served. Red on 04cc80c: it never returned (all loader threads were held by the profile read)."""
    w, reg = _profile_world(tmp_path)
    _env(w, monkeypatch, "0.5")
    from factlattice_engine import config
    with RootStall(w.root("b1"), cap_s=40) as stall:
        try:
            out = run_limited(lambda: config.build_store(reg), 8.0, name="build-profile")
            assert out.done and out.error is None, f"build_store did not return (blocked in {stall.hit_names()})"
            st = out.value
            assert st.corpus_ids() == ["a1"], st.corpus_ids()
            assert {c: f["code"] for c, f in st._failed_corpora.items()} == {c: CODE_BUILD for c in ("p1", "p2", "p3", "p4", "b1")}
        finally:
            stall.release()
            time.sleep(0.3)


def test_a_healthy_corpus_is_served_while_four_profiles_hang_under_a_dead_root_in_a_real_server(tmp_path):
    """P2-3, server: the same world, the four loader threads must not be held; a1 (registered after them) is served and the handshake is quick.
    Red on 04cc80c: a1 stayed pending (named find refused root-unresponsive)."""
    w, _reg = _profile_world(tmp_path, "w9s")
    with RealServer(w, stall_root=w.root("b1"), engaged=True, env=DEADLINES) as srv:
        srv.initialize(timeout=3)
        time.sleep(2.5)                                                                # past the deadline
        res, dt = srv.call("lts_find", {"corpus": "a1", "text": "lanternfish", "tier": "digest"}, timeout=10)
        assert res["success"] is True and res["count"] == 1 and dt < 2.0, (res.get("reason"), dt)


def test_the_blocking_wait_for_the_loader_ends_one_deadline_after_its_last_sign_of_life(ws, monkeypatch):
    """P2-3, the join: an unforeseen call of the loader that nobody bounded (planted: the parser-config step blocks on an event) must not keep
    build_store past the root deadline of silence. a1 and b1 are both held, so both fail `corpus-root-unresponsive`; the return is prompt. Red on
    04cc80c: the untimed join never returned."""
    _env(ws, monkeypatch, "0.5")
    from factlattice_engine import config
    gate = threading.Event()
    real = config.parser_config_for

    def blocked(*a, **k):
        gate.wait(30)
        return real(*a, **k)
    monkeypatch.setattr(config, "parser_config_for", blocked)
    try:
        t0 = time.monotonic()
        out = run_limited(lambda: config.build_store(ws.registry), 8.0, name="build-join")
        assert out.done and out.error is None, "build_store was held by an unbounded call of the loader"
        assert time.monotonic() - t0 < 4.0
        assert {c: f["code"] for c, f in out.value._failed_corpora.items()} == {"a1": CODE_BUILD, "b1": CODE_BUILD}, out.value._failed_corpora
    finally:
        gate.set()
        time.sleep(0.3)


# ---- P2-4: the reviewer's surviving mutants ------------------------------------------------------------------------
def test_a_corpus_is_always_in_exactly_one_of_served_pending_and_failed_to_every_reader(monkeypatch):
    """M02 (settle publishes, then pops pending late) and M03 (lookup reads pending first, then the partition, with a gap): a reader must never find
    a corpus in two of the tables or in none. A loader settles 40 corpora, one every 2 ms, while a reader looks up the NEXT one to settle through
    `lookup` and `snapshot` as fast as it can. Red on both mutants (50 ms gaps), green on the real store."""
    import types

    from factlattice_engine import store as S
    ids = [f"c{i}" for i in range(40)]
    st = S.LtsStore()
    st.set_order(ids)
    for c in ids:
        st.declare_pending(c, {})
    settled = []
    bad = []
    stop = threading.Event()

    def loader():
        for c in ids:
            time.sleep(0.002)
            st.settle(c, partition=types.SimpleNamespace(corpus_id=c, domain_suffix="x." + c))
            settled.append(c)
        stop.set()

    def reader():
        while not stop.is_set():
            c = ids[min(len(settled), len(ids) - 1)]
            part, pend, failed = st.lookup(c)
            n = (part is not None) + (pend is not None) + (failed is not None)
            if n != 1:
                bad.append(("lookup", c, n))
            parts, pending = st.snapshot()
            for p in parts:
                if p.corpus_id in pending:
                    bad.append(("snapshot-both", p.corpus_id))
            served = {p.corpus_id for p in parts}
            if len(served) + len(pending) != len(ids):
                bad.append(("snapshot-none", len(served), len(pending)))
    ts = [threading.Thread(target=loader), threading.Thread(target=reader)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert not bad, bad[:5]


def test_one_lock_covers_settle_add_partition_and_remove_partition(tmp_path):
    """P2-5, the shape of the cold read's probe: a loader settles N corpora (each `_reorder` rebinds the dict) while a runtime thread registers and
    unregisters other corpora. No KeyError (which would be fatal, so registry-error mode), no partition lost. Red on 04cc80c: KeyError in `_reorder`
    and published corpora missing."""
    import types

    from factlattice_engine import store as S
    N = 1500
    ids = [f"p{i}" for i in range(N)]
    st = S.LtsStore()
    st.set_order(ids)
    for c in ids:
        st.declare_pending(c, {})
    P = lambda c: types.SimpleNamespace(corpus_id=c, domain_suffix="x." + c)    # noqa: E731
    errs, lost, stop = [], [0], threading.Event()

    def loader():
        try:
            for c in ids:
                st.settle(c, partition=P(c))
        except BaseException as exc:  # noqa: BLE001
            errs.append(("loader", type(exc).__name__, str(exc)[:60]))
        finally:
            stop.set()

    def runtime():
        i = 0
        while not stop.is_set():
            i += 1
            cid = f"rt{i}"
            try:
                st.add_partition(P(cid))
                if st.get_partition(cid) is None:
                    lost[0] += 1
                st.remove_partition(cid)
            except BaseException as exc:  # noqa: BLE001
                errs.append(("runtime", type(exc).__name__, str(exc)[:60]))
                break
    ts = [threading.Thread(target=runtime), threading.Thread(target=loader)]
    old_switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)                 # interleave as finely as the interpreter allows: the race is a window of a few bytecodes
    try:
        for t in ts:
            t.start()
        for t in ts:
            t.join(120)
    finally:
        sys.setswitchinterval(old_switch)
    assert not errs and lost[0] == 0, (errs[:3], lost[0])
    assert [c for c in ids if st.get_partition(c) is None] == [], "every published corpus is still served"
    assert st.corpus_ids() == ids, "and in the registry's order, with no runtime corpus left behind"


def test_a_write_through_the_library_door_also_waits_for_a_pending_root(ws, monkeypatch):
    """M08: `authoring.add_property` called directly (the in-process door, `writes_scoped`, not the dispatcher) must wait for a pending root as the
    dispatcher does. Red on the mutant that drops `await_pending` from `writes_scoped`; on 04cc80c the test passes (a guard of the door)."""
    _env(ws, monkeypatch, "20")
    from factlattice_engine import authoring as A
    from factlattice_engine import config
    from factlattice_engine.kernel import FreshnessKernel
    store = config.build_store(ws.registry)
    k = FreshnessKernel()
    store.kernel = k
    slot = k._slot(os.path.abspath(str(ws.root("b1"))))
    out = {}
    t = threading.Thread(target=lambda: out.update(r=A.add_property(str(ws.file("b1")), "B1-N", "stamp", "x", store=store)))
    t.start()
    try:
        t.join(0.6)
        assert t.is_alive() and "r" not in out, f"the library door did not wait: {out.get('r')}"
    finally:
        slot.submit("seat", lambda: None, coalesce=False, foreground=True)
    t.join(30)
    assert out["r"].get("success") is True, out.get("r")


def test_a_library_build_leaves_no_pool_thread_and_no_loader_thread_behind(ws, monkeypatch):
    """M10: after a HEALTHY build_store returns, the pool has no thread (`trim_idle`) and the loader threads are joined. Red on the mutant that
    drops `trim_idle`: the idle workers linger for 5 s."""
    _env(ws, monkeypatch, "5")
    from factlattice_engine import boundedfs, config
    base = threading.active_count()
    store = config.build_store(ws.registry)
    assert sorted(store.corpus_ids()) == ["a1", "b1"]
    assert boundedfs.registry().thread_count() == 0, "idle pool workers were left behind"
    assert threading.active_count() <= base, "and so were loader threads"


# ---- P3 ----------------------------------------------------------------------------------------------------------
def test_no_engine_code_passes_a_deadline_or_pool_size_to_the_kernel_constructor():
    """P3-2: `FreshnessKernel(root_deadline_s=, pool_size=)` starts the PROCESS registry over, which is a test seam; production code must never call it.
    An AST scan of every non-test python file of the repository."""
    import ast
    repo = Path(__file__).resolve().parent.parent
    bad = []
    for f in repo.rglob("*.py"):
        rel = f.relative_to(repo).as_posix()
        if rel.startswith(("tests/", "private/", ".git/")) or "/tests/" in rel:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                fn = n.func
                name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
                if name == "FreshnessKernel" and any(k.arg in ("root_deadline_s", "pool_size") for k in n.keywords):
                    bad.append((rel, n.lineno))
    assert not bad, bad
    # negative control: the scan fires on a planted call
    planted = ast.parse("k = FreshnessKernel(root_deadline_s=3)")
    assert any(isinstance(n, ast.Call) and any(k.arg == "root_deadline_s" for k in n.keywords) for n in ast.walk(planted))


def test_a_kernel_gives_back_only_the_root_slots_it_declared(ws, monkeypatch):
    """P3-1: the registry is the process's. Kernel 2 giving a root back (the unregister of a pending corpus) must not drop a slot kernel 1 declared.
    Red on 04cc80c: k1's slot was gone."""
    _env(ws, monkeypatch)
    from factlattice_engine.kernel import FreshnessKernel
    k1, k2 = FreshnessKernel(), FreshnessKernel()
    r1, r2 = os.path.abspath(str(ws.root("a1"))), os.path.abspath(str(ws.root("b1")))
    k1.declare_roots([r1])
    k2.declare_roots([r2])
    k2.release_roots([r1, r2], set())
    assert k1.roots.slot(r1, create=False) is not None, "another kernel's declared slot was dropped"
    assert k1.roots.slot(r2, create=False) is None, "control: the kernel's OWN declared slot is given back"


def test_an_unseat_after_a_lost_settle_does_not_drop_a_newer_registration(tmp_path, monkeypatch):
    """P3-4: the loader seated corpus a1, then lost the settle (a1 was unregistered while it was held) and the id was registered AGAIN. Its unseat must
    be of ITS registration only: the new one stays seated and served. Red on 04cc80c: the new registration's kernel state was dropped."""
    w = Workspace(tmp_path / "w", corpora=("a1",))
    from factlattice_engine import store as S
    reached, go = threading.Event(), threading.Event()
    real_settle = S.LtsStore.settle

    def held(self, cid, **kw):
        if kw.get("partition") is not None and not go.is_set():
            reached.set()
            assert go.wait(30)
        return real_settle(self, cid, **kw)
    monkeypatch.setattr(S.LtsStore, "settle", held)
    eng = _async_engine(w, monkeypatch, "20")
    try:
        assert reached.wait(20)
        assert eng.unregister_corpus("a1")["success"] is True                         # a1 is forgotten while its loader holds a seated partition
        got = eng.register_corpus("a1", spec=w.registry["a1"])
        assert got["success"] is True, got
        newpart = eng.store.get_partition("a1")
        assert eng.store.kernel.tracked_roots(newpart) is not None, "setup: the new registration is seated"
    finally:
        go.set()
    assert eng.boot_loader.first_pass_done.wait(20)
    time.sleep(0.2)
    assert eng.store.get_partition("a1") is newpart and eng.store.kernel.tracked_roots(newpart) is not None, "the loader's unseat dropped the NEW registration"


def test_the_read_profile_listing_declares_a_corpus_it_could_not_load(ws, monkeypatch, capsys):
    """P3-6: `readprofile._live_corpus_nodes` dropped a failed corpus silently, so its absence read as 'nothing unread'. Now it says so on stderr.
    Red on 04cc80c (nothing on stderr)."""
    _env(ws, monkeypatch)
    from factlattice_engine import readprofile
    with RootStall(ws.root("b1"), cap_s=40) as stall:
        try:
            out = run_limited(lambda: readprofile._live_corpus_nodes(None), DEADLINE_S + 4.0, name="readprofile")
            assert out.done and sorted(out.value) == ["a1"]
        finally:
            stall.release()
            time.sleep(0.3)
    err = capsys.readouterr().err
    assert "b1" in err and CODE_BUILD in err and "NOT loaded" in err, err


def test_a_healthy_corpus_does_not_wait_for_another_corpus_that_hangs_at_boot(tmp_path):
    """Found while fixing the hop (round 2): the registry gate's wait for the boot verdict ran to the end of the FIRST PASS while no corpus was
    served yet, and the first pass lasts a whole deadline when one root hangs. The very first call after the handshake, naming healthy a1, waited that
    deadline. It must end as soon as ANY corpus is served. Root deadline 3 s; the call must come back well inside it. Red on 04cc80c."""
    w = Workspace(tmp_path / "w")
    env = dict(DEADLINES, LTS_ROOT_DEADLINE_S="3")
    with RealServer(w, stall_root=w.root("b1"), engaged=True, env=env) as srv:
        srv.initialize(timeout=3)
        res, dt = srv.call("lts_find", {"corpus": "a1", "text": "lanternfish", "tier": "digest"}, timeout=10)
        assert res["success"] is True and res["count"] == 1, res
        assert dt < 1.8, f"a healthy corpus waited {dt:.2f}s for another corpus's hang (deadline 3 s)"
        un, dt2 = srv.call("lts_find", {"text": "lanternfish", "tier": "digest"}, timeout=10)
        assert un["success"] is True and un["count"] == 1


def test_add_and_remove_partition_wait_for_the_store_lock_that_settle_holds():
    """P2-5, deterministic: the race of the two writers is a window of a few bytecodes, so this holds the store's lock in the test and shows that
    `add_partition`, `remove_partition` and `filter_by_domain` do not get past it. Red on a store whose add or remove is outside the lock."""
    import types

    from factlattice_engine import store as S
    st = S.LtsStore()
    P = lambda c: types.SimpleNamespace(corpus_id=c, domain_suffix="x." + c)    # noqa: E731
    st.add_partition(P("seed"))
    done = {}

    def run(name, fn):
        fn()
        done[name] = True
    with st._pending_cv:
        ts = [threading.Thread(target=run, args=("add", lambda: st.add_partition(P("new"))), daemon=True),
              threading.Thread(target=run, args=("remove", lambda: st.remove_partition("seed")), daemon=True),
              threading.Thread(target=run, args=("domain", lambda: st.filter_by_domain("x.seed")), daemon=True)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(0.4)
        assert done == {}, f"these got past the store lock: {sorted(done)}"
    for t in ts:
        t.join(10)
    assert set(done) == {"add", "remove", "domain"}, "and they finish when it is released"


def test_a_hop_into_a_corpus_that_build_store_failed_on_a_hung_root_is_declared_stale(tmp_path, monkeypatch):
    """The loader's own failed record carries the corpus's domain, so a hop into a corpus that `build_store` could not read is declared (not dropped).
    One store, built under the stall: a1 served, b1 failed; a1's node has the edge into b1. Red on 04cc80c (the hop vanished)."""
    w = _hop_world(tmp_path)
    _env(w, monkeypatch)
    from factlattice_engine import config
    from factlattice_engine import tools as T
    from factlattice_engine.tools import LtsEngine
    with RootStall(w.root("b1"), cap_s=40) as stall:
        try:
            out = run_limited(lambda: config.build_store(w.registry), DEADLINE_S + 4.0, name="build-hop")
            assert out.done and out.error is None
            st = out.value
            assert st._failed_corpora["b1"]["domain_suffix"] == "x.b1", st._failed_corpora
            res = T.dispatch("lts_node", {"corpus": "a1", "node_id": "A1-N"}, tools=LtsEngine(st))
            assert [x for x in res["routed_stale"] if x["corpus"] == "b1" and x["reason"] == CODE_UNRESPONSIVE], res.get("routed_stale")
        finally:
            stall.release()
            time.sleep(0.3)


# ======================================================================================================================
# ROUND 3 (delta read of dcaa2c7)
# ======================================================================================================================
def test_a_malformed_named_roots_table_is_kept_as_a_problem_by_the_provisional_and_the_final_pin(tmp_path, monkeypatch):
    """P2-5: `RegistryChoice.table_problem` must reach the pinned context, provisional and final, or a write on such a server loses its
    malformed-table refusal and nothing says so. Control: a sound table has no problem. Red on 04cc80c's lineage by mutant N07."""
    from factlattice import ledger
    got = {}
    for name, bad in (("bad", True), ("ok", False)):
        w = Workspace(tmp_path / name)
        d = json.loads(w.registry_path.read_text())
        d["named_roots"] = {"data": "not-a-table-entry"} if bad else {"data": {"path": str(w.root("a1")), "state": str(tmp_path / name / "st")}}
        w.registry_path.write_text(json.dumps(d))
        _env(w, monkeypatch, "5")
        ledger.unpin_table()
        from factlattice import registry
        choice = registry.resolve_registry()
        prov = ledger.context_provisional(choice)
        fin = ledger.context_deferred(choice)()
        got[name] = (prov.problem, fin.problem)
    assert got["bad"][0] and got["bad"][0] == got["bad"][1] and got["bad"][0][0] == ledger.TABLE_MALFORMED, got
    assert got["ok"] == (None, None), got


def test_a_hop_into_a_corpus_that_shares_a_domain_with_a_served_one_is_declared(tmp_path):
    """P1-2: s1 (served) and s2 (hung) share the domain `x.shared`; a1's node points at S2-N. The served sibling does not hold it, so the hop
    must be declared stale with s2's reason, never dropped (node and traverse). Control: the hop to S1-N routes. Red on dcaa2c7."""
    w = Workspace(tmp_path / "w", corpora=("a1", "s1", "s2"))
    reg = json.loads(w.registry_path.read_text())
    reg["s1"]["domain_suffix"] = reg["s2"]["domain_suffix"] = "x.shared"
    w.registry_path.write_text(json.dumps(reg))
    with open(w.file("a1"), "a", encoding="utf-8") as fh:
        fh.write("- depends-on: [x.shared:SPEC:S2-N]\n- relates-to: [x.shared:SPEC:S1-N]\n")
    with RealServer(w, stall_root=w.root("s2"), engaged=True, env=DEADLINES) as srv:
        srv.initialize(timeout=3)
        for _r in range(2):
            for name, args in (("lts_node", {"corpus": "a1", "node_id": "A1-N"}), ("lts_traverse", {"corpus": "a1", "start_node": "A1-N", "max_depth": 1})):
                out, _dt = srv.call(name, args, timeout=10)
                stale = out.get("routed_stale") or []
                assert [x for x in stale if x.get("corpus") == "s2" and x.get("reason") in ("root-pending", CODE_UNRESPONSIVE)], (name, "the hop vanished", stale)
            time.sleep(1.4)


def test_a_hop_into_a_corpus_that_failed_for_any_reason_is_declared_with_its_code(tmp_path, monkeypatch):
    """P2-4: b1 holds a FIFO named bad.lts (corpus-file-not-regular); a root-less corpus is `no-roots`. Hops into each are declared stale with the code;
    a private failed one is withheld. Red on dcaa2c7: dropped."""
    w = Workspace(tmp_path / "w", corpora=("a1", "b1"))
    os.mkfifo(str(w.root("b1") / "bad.lts"))
    reg = json.loads(w.registry_path.read_text())
    reg["m1"] = {"roots": [{"path": str(tmp_path / "w" / "c" / "missing")}], "domain_suffix": "x.m1"}
    w.registry_path.write_text(json.dumps(reg))
    with open(w.file("a1"), "a", encoding="utf-8") as fh:
        fh.write("- depends-on: [x.b1:SPEC:B1-N]\n- relates-to: [x.m1:SPEC:M1-N]\n")
    _env(w, monkeypatch)
    from factlattice_engine import config
    from factlattice_engine import tools as T
    from factlattice_engine.tools import LtsEngine
    st = config.build_store(reg)
    assert st._failed_corpora["b1"]["code"] == "corpus-file-not-regular" and "m1" in st._no_roots_corpora, "setup"
    eng = LtsEngine(st)
    for name, args in (("lts_node", {"corpus": "a1", "node_id": "A1-N"}), ("lts_node", {"corpus": "a1", "node_id": "A1-N", "tier": "digest"}),
                       ("lts_traverse", {"corpus": "a1", "start_node": "A1-N", "max_depth": 1}), ("lts_capabilities", {"corpus": "a1", "node_id": "A1-N"})):
        out = T.dispatch(name, args, tools=eng)
        reasons = {x.get("corpus"): x.get("reason") for x in (out.get("routed_stale") or [])}
        assert reasons == {"b1": "corpus-file-not-regular", "m1": "no-roots"}, (name, out.get("tier"), reasons)
    st._failed_corpora["b1"]["private"] = True
    out = T.dispatch("lts_node", {"corpus": "a1", "node_id": "A1-N"}, tools=eng)
    assert out.get("routed_withheld") and "b1" not in [x.get("corpus") for x in out.get("routed_stale") or []]


def test_a_corpus_whose_profile_hangs_is_failed_after_one_deadline_in_the_server_and_read_again_when_it_answers(tmp_path):
    """P2-1: p1's profile lives under hung b1. After one deadline p1 is FAILED `corpus-root-unresponsive` with `reason: profile` (not pending for ever:
    a corpus-omitted read does not pay a deadline, and the refusal names the profile, not a root that answered). Released, the watcher reads it again."""
    w = Workspace(tmp_path / "w", corpora=("p1", "a1", "b1"))
    reg = json.loads(w.registry_path.read_text())
    reg["p1"]["profile"] = str(w.root("b1") / "prof.json")
    w.registry_path.write_text(json.dumps(reg))
    with RealServer(w, stall_root=w.root("b1"), engaged=True, env=DEADLINES) as srv:
        srv.initialize(timeout=3)
        assert _wait(lambda: any(c.get("corpus_id") == "p1" and c.get("reason") == "profile"
                                 for c in srv.call("lts_list_corpora", {"heal": False}, timeout=5)[0].get("failed_corpora", [])), 10, step=0.3), "p1 failed with reason profile"
        res, dt = srv.call("lts_find", {"corpus": "p1", "text": "x"}, timeout=10)
        assert res["reason"] == "corpus-root-unresponsive" and res.get("cause") == "profile" and "profile" in res["error"] and dt < 0.8, (res, dt)
        un, dt2 = srv.call("lts_find", {"text": "lanternfish", "tier": "digest"}, timeout=10)
        assert un["success"] is True and dt2 < 2.5, ("the b1 corpus still costs its deadline once; p1 must not add another", dt2)
        srv.release()
        assert _wait(lambda: srv.call("lts_find", {"corpus": "p1", "text": "lanternfish", "tier": "digest"}, timeout=10)[0].get("count") == 1, 25, step=0.5), "p1 read again"


def test_a_big_healthy_merge_is_never_cut_by_the_join(tmp_path, monkeypatch):
    """P2-2: the merge (finish_cold_build) runs on the loader thread outside any job; it must report a sign of life at least every BEAT_EVERY items.
    The floor is lowered to the deadline (1.2 s) and the merge made to take longer than that; the corpus must be SERVED. Also kills N09/N24."""
    from factlattice_engine import bootload, incremental
    ws2 = Workspace(tmp_path / "w2", corpora=("k0",))
    lines = ['---\nlts: "1.1"\ntitle: t\ndomain: x.k0\ndoctype: SPEC\n---\n', "# R [K0-ROOT]\n\n- summary: r\n"]
    for n in range(2500):
        lines.append(f"\n## N{n} [K0-N{n}]\n\n- topic: t\n")
    for f in range(8):
        (ws2.root("k0") / f"big{f}.lts").write_text("".join(lines).replace("K0-", f"K0F{f}-").replace("K0F0-ROOT", "K0F0-ROOT"), encoding="utf-8")
    _env(ws2, monkeypatch, "1.2")
    monkeypatch.setattr(bootload, "JOIN_FLOOR_S", 1.2)
    real = incremental.CorpusIndex._materialize

    def slow(self, live, tick=None):
        # a merge that is quiet for 0.15 s between beats and takes ~3 s in all: longer than the floor (1.2 s), never silent for it. Round 6 (F9): the margin
        # between the gap and the floor was 0.05 s (0.15 against 0.2), so a loaded machine that stretched one sleep past 0.2 s made the join give up on a
        # healthy merge (25 of 25 failures under 14 busy loops); it is 8x now, and the total is still longer than the floor
        def pace():
            time.sleep(0.15)
            if tick is not None:
                tick()
        for _ in range(20):
            pace()
        return real(self, live, tick)
    monkeypatch.setattr(incremental.CorpusIndex, "_materialize", slow)
    from factlattice_engine import config
    out = run_limited(lambda: config.build_store(ws2.registry), 60.0, name="build-merge")
    assert out.done and out.error is None
    assert out.value.corpus_ids() == ["k0"] and not out.value._failed_corpora, out.value._failed_corpora


def test_a_loader_that_pauses_less_than_the_floor_is_not_cut_whatever_the_root_deadline(ws, monkeypatch):
    """N23: a healthy thread that a loaded machine starves for 0.6 s (planted: the merge sleeps 0.6 s, giving no sign of life) is slow, not hung:
    with a 0.2 s root deadline the floor (1 s) keeps the corpus. A GUARD (green on dcaa2c7): it exists to kill the join-without-a-floor mutant."""
    from factlattice_engine import incremental
    _env(ws, monkeypatch, "0.2")
    real = incremental.CorpusIndex.finish_cold_build

    def paused(self, live, tick=None):
        time.sleep(0.6)
        return real(self, live) if tick is None else real(self, live, tick)
    monkeypatch.setattr(incremental.CorpusIndex, "finish_cold_build", paused)
    from factlattice_engine import config
    out = run_limited(lambda: config.build_store({"a1": ws.registry["a1"]}), 30.0, name="build-pause")
    assert out.done and out.value.corpus_ids() == ["a1"], out.value.tables()[1]


def test_a_slow_but_answering_read_is_never_cut_by_the_join(ws, monkeypatch):
    """N09: a root that answers every call, slowly (0.08 s per call, under the 0.4 s deadline), makes the corpus's read last well past the join's
    silence limit (floor lowered to 0.4 s): the registry job's own progress keeps the join open. Red on a join that ignores registry progress."""
    from factlattice_engine import bootload, config
    _env(ws, monkeypatch, "0.4")
    monkeypatch.setattr(bootload, "JOIN_FLOOR_S", 0.4)
    t0 = time.monotonic()
    with RootStall(ws.root("a1"), cap_s=30, slow_s=0.08):
        out = run_limited(lambda: config.build_store({"a1": ws.registry["a1"]}), 60.0, name="build-slow")
    assert out.done and out.value.corpus_ids() == ["a1"] and not out.value._failed_corpora, (out.value._failed_corpora if out.done else "blocked")
    assert time.monotonic() - t0 > 0.8, "setup: the read really lasted longer than the silence limit"


def test_the_merge_reports_a_sign_of_life_at_least_every_beat_items(tmp_path):
    """P2-2, the real merge: `finish_cold_build(live, tick)` calls `tick` at least once per BEAT_EVERY items of a corpus of 6000 nodes (and its
    edges), whatever the machine's speed. Red on a merge that gives none."""
    from factlattice_engine import incremental
    from factlattice_engine.incremental import CorpusIndex
    root = tmp_path / "r"
    root.mkdir()
    lines = ['---\nlts: "1.1"\ntitle: t\ndomain: x.r\ndoctype: SPEC\n---\n', "# R [R-ROOT]\n\n- summary: r\n"]
    for n in range(6000):
        lines.append(f"\n## N{n} [R-N{n}]\n\n- topic: t\n")
    (root / "a.lts").write_text("".join(lines), encoding="utf-8")
    ci = CorpusIndex("r", "x.r", [{"path": str(root)}])
    live = [(sp, ci.read_root(sp)) for sp in ci.root_specs]
    ticks = []
    nodes, _layers, _roots, _h = ci.finish_cold_build(live, lambda: ticks.append(1))
    assert len(nodes) >= 6000 and len(ticks) >= len(nodes) // 1024 and incremental.BEAT_EVERY <= 1024, (len(nodes), len(ticks))
    quiet = []
    ci.finish_cold_build(live)                                                      # control: no tick given, nothing is called, nothing breaks
    assert quiet == []


def test_every_failed_corpus_record_carries_the_contract_keys(tmp_path, monkeypatch):
    """P2-6: `store._failed_corpora` records come from ONE function (`store.failed_record`): code, path, private, domain_suffix. Written here by each
    kind of writer in the loader: an unreadable member (FIFO), a registry entry with an unknown key, a root that did not answer (hung)."""
    w = Workspace(tmp_path / "w", corpora=("f1", "k1", "h1", "pv-personality"))
    os.mkfifo(str(w.root("f1") / "bad.lts"))
    reg = json.loads(w.registry_path.read_text())
    reg["k1"]["bogus_key"] = 1
    w.registry_path.write_text(json.dumps(reg))
    os.mkfifo(str(w.root("pv-personality") / "bad.lts"))
    _env(w, monkeypatch)
    from factlattice_engine import config
    with RootStall(w.root("h1"), cap_s=40) as stall:
        try:
            out = run_limited(lambda: config.build_store(reg), DEADLINE_S + 5.0, name="build-records")
            assert out.done and out.error is None
            rec = out.value._failed_corpora
        finally:
            stall.release()
            time.sleep(0.3)
    assert {c: r["code"] for c, r in rec.items()} == {"f1": "corpus-file-not-regular", "k1": "registry-entry-unknown-key", "h1": CODE_BUILD,
                                                      "pv-personality": "corpus-file-not-regular"}, rec
    for cid, r in rec.items():
        assert {"code", "path", "private", "domain_suffix"} <= set(r), (cid, r)
        assert r["domain_suffix"] == reg[cid]["domain_suffix"], (cid, r)
    assert rec["pv-personality"]["private"] is True and rec["f1"]["private"] is False
    assert rec["h1"]["path"] is None and rec["f1"]["path"] == "bad.lts"


def test_a_wait_for_a_pending_corpus_ends_when_its_stop_says_so():
    """M18 (the reviewer's, older than this slice's round 2): `wait_loaded` with a `stop` that is already true returns at once instead of spending the
    timeout; this is what keeps a corpus-omitted read from paying a deadline per corpus whose root is already marked."""
    from factlattice_engine import store as S
    st = S.LtsStore()
    st.declare_pending("c", {})
    t0 = time.monotonic()
    assert st.wait_loaded("c", 5.0, stop=lambda: True) is False
    assert time.monotonic() - t0 < 0.5
    t0 = time.monotonic()
    assert st.wait_loaded("c", 0.3) is False and time.monotonic() - t0 >= 0.25, "control: with no stop it waits the whole timeout"


def test_a_table_read_during_the_first_pass_goes_by_the_booted_registry_bytes(tmp_path, monkeypatch):
    """P2-3, the window BEFORE the first pass ends: the loader is held; the registry file is edited; `table_context()` (what a write reads) must still
    be the booted table, because the provisional pin is made at boot. Red without the boot-time pin."""
    from factlattice import ledger
    w = Workspace(tmp_path / "w")
    s1, s2 = tmp_path / "w" / "stateA", tmp_path / "w" / "stateB"

    def regdata(state):
        d = json.loads(w.registry_path.read_text())
        d["named_roots"] = {"data": {"path": str(w.root("a1")), "state": str(state)}}
        return d
    w.registry_path.write_text(json.dumps(regdata(s1)))
    from factlattice_engine import bootload
    go = threading.Event()
    real_load = bootload.StoreLoader._load_one

    def held(self, cid):
        assert go.wait(30)
        return real_load(self, cid)
    monkeypatch.setattr(bootload.StoreLoader, "_load_one", held)
    _env(w, monkeypatch, "5")
    ledger.unpin_table()
    try:
        from factlattice_engine.server import build_tools
        eng = build_tools(watch=False, async_boot=True)
        _LOADERS.append(eng.boot_loader)
        w.registry_path.write_text(json.dumps(regdata(s2)))
        assert not eng.boot_loader.first_pass_done.is_set(), "setup: the first pass has not ended"
        assert {n: v.get("state") for n, v in (ledger.table_context().table or {}).items()} == {"data": str(s1)}
        assert not ledger.pin_is_final()
    finally:
        go.set()


# ======================================================================================================================
# ROUND 4 (delta read of de33583)
# ======================================================================================================================
def _retry_world(tmp_path, reason="profile"):
    """A loader with one corpus p1 recorded failed (reason), registered for retry, whose `_load_one` is a no-op (the object under test is the move)."""
    from factlattice_engine import bootload
    from factlattice_engine import store as S
    root = tmp_path / "c" / "p1"
    root.mkdir(parents=True)
    (root / "p1.lts").write_text('---\nlts: "1.1"\ntitle: p1\ndomain: x.p1\ndoctype: SPEC\n---\n\n# R [P1-ROOT]\n\n- summary: r\n', encoding="utf-8")
    spec = {"roots": [{"path": str(root)}], "domain_suffix": "x.p1", "profile": str(tmp_path / "nowhere" / "prof.json")}
    st = S.LtsStore()
    loader = bootload.StoreLoader(st, {"p1": spec}, kernel=None, retry=True)
    loader._load_one = lambda cid: None
    st.fail("p1", CODE_BUILD, None, False, "x.p1", **({"reason": reason} if reason else {}))
    loader._retry_failed["p1"] = "profile:" + spec["profile"]
    return st, loader, spec


def test_the_retry_of_a_profile_failed_corpus_is_one_step_for_every_reader(tmp_path):
    """F1: failed -> pending was two lock acquisitions (clear_unserved, then declare_pending): between them a reader found the corpus in NO table (a
    named read said not found; an omitted read dropped it undeclared). The store's lock is wrapped to look at the tables each time it is let go: the
    corpus must be in exactly one of failed and pending at every such moment. Red on de33583 (neither, between the two)."""
    st, loader, _spec = _retry_world(tmp_path)
    states = []

    class Spy(threading.Condition):
        depth = 0

        def __enter__(self):
            Spy.depth += 1
            return super().__enter__()

        def __exit__(self, *a):
            Spy.depth -= 1
            if Spy.depth == 0:
                states.append(("p1" in st._failed_corpora, "p1" in st._pending_corpora))
            return super().__exit__(*a)
    st._pending_cv = Spy()
    loader.retry_profile_failed()
    assert states, "setup: the move took the lock"
    assert all(a != b for a, b in states), f"the corpus was in neither (or both) tables at a point another thread could look: {states}"
    assert states[-1] == (False, True) and "p1" not in st._failed_corpora


def test_which_unserved_corpus_owns_a_domain_is_one_snapshot_not_three_reads(tmp_path):
    """F1: `_unserved_domain` read pending, then failed, then no_roots separately; a corpus moving failed -> pending between two reads was missed
    (the hop vanished). A thread flips p1 between failed and pending (both directions atomic) while the engine asks for its domain 3000 times: never
    None. Red on a three-read implementation. Round 5: the flip thread's death fails the test and the flip count has a floor (it passed vacuously)."""
    from factlattice_engine.store import failed_record
    st, _loader, spec = _retry_world(tmp_path)
    rec = _pending_rec(spec)

    def flip():
        st.move_failed_to_pending("p1", dict(rec))                                            # failed -> pending, one step
        st.settle("p1", failed=failed_record(CODE_BUILD, None, False, "x.p1", reason="profile"))      # pending -> failed, one step
    misses, flips = _hammer_unserved_domain(st, spec, flip=flip, n=3000, min_flips=100)
    assert not misses, f"the corpus was missed {misses} times in {flips} flips"


def test_private_is_kept_on_the_kernel_seat_and_registry_key_failure_paths(tmp_path, monkeypatch):
    """F3 (N18u, D32): a PRIVATE corpus that fails at the seat, or fails on an unknown registry key, is recorded private, so it is counted and never
    named. Red on a writer that drops the flag."""
    w = Workspace(tmp_path / "w", corpora=("pv-personality", "k2"))
    reg = json.loads(w.registry_path.read_text())
    reg["k2"]["private"] = True
    reg["k2"]["bogus_key"] = 1
    w.registry_path.write_text(json.dumps(reg))
    _env(w, monkeypatch)
    from factlattice_engine import config
    from factlattice_engine.kernel import FreshnessKernel
    st = config.build_store(reg)                                       # k2: the unknown-key failure; pv-personality is served
    assert st._failed_corpora["k2"]["code"] == "registry-entry-unknown-key" and st._failed_corpora["k2"]["private"] is True, st._failed_corpora
    k = FreshnessKernel()
    real = k.seat_partition
    from factlattice import CorpusFileError

    def boom(part):
        raise CorpusFileError("corpus-file-unreadable", "x.lts")
    k.seat_partition = boom
    k.seat_store(st)                                                    # the CorpusFileError branch
    assert st._failed_corpora["pv-personality"]["private"] is True and st._failed_corpora["pv-personality"]["code"] == "corpus-file-unreadable"
    st2 = config.build_store({"pv-personality": reg["pv-personality"]})
    k2 = FreshnessKernel()
    with RootStall(w.root("pv-personality"), cap_s=30, engaged_fn=lambda: threading.current_thread().name.startswith("lts-root-pool")) as stall:
        try:
            out = run_limited(lambda: k2.seat_store(st2), DEADLINE_S + 4.0, name="seat-private")
            assert out.done
        finally:
            stall.release()
            time.sleep(0.3)
    assert st2._failed_corpora["pv-personality"]["code"] == CODE_BUILD and st2._failed_corpora["pv-personality"]["private"] is True, st2._failed_corpora
    del real


def test_the_merge_never_goes_quiet_for_more_than_a_beat_in_any_loop(tmp_path):
    """F3 (D04-D07): the merge's sign of life, loop by loop. The ticks of a corpus of 5000 nodes (each with an edge) and 50 files are counted and
    compared with what the loops must give: one per file, and one per BEAT items of the node, edge and adjacency work in `assemble` and in
    `_materialize`. A merge whose total is right but whose edge or adjacency loop is silent (or whose tick is not forwarded) falls short. Not a clock."""
    from factlattice_engine.incremental import CorpusIndex
    root = tmp_path / "r"
    root.mkdir()
    for f in range(50):
        lines = ['---\nlts: "1.1"\ntitle: t\ndomain: x.r\ndoctype: SPEC\n---\n', f"# R [F{f}-ROOT]\n\n- summary: r\n"]
        for n in range(100):
            lines.append(f"\n## N{n} [F{f}-N{n}]\n\n- topic: t\n- depends-on: [F{f}-ROOT]\n")
        (root / f"f{f}.lts").write_text("".join(lines), encoding="utf-8")
    ci = CorpusIndex("r", "x.r", [{"path": str(root)}])
    live = [(sp, ci.read_root(sp)) for sp in ci.root_specs]
    ticks = []
    nodes, layers, _rp, _h = ci.finish_cold_build(live, lambda: ticks.append(1))
    n, e = len(nodes), sum(len(ly["edges"]) for ly in layers.values())
    need = 50 + (n + 2 * e) // 1024 + (n + e) // 1024      # per file; assemble's node, edge-fold and layer loops; _materialize's node and adjacency loops
    assert n >= 5000 and e >= 5000 and len(ticks) >= need, (n, e, len(ticks), need)


def test_the_retry_of_a_profile_failed_corpus_waits_for_a_quiet_profile_slot_and_only_for_reason_profile(tmp_path):
    """F3 (D18, D19, D21, D34): no retry while the profile's slot is stuck (marked) or merely busy (a call still running), and none for a corpus that
    did not fail because of its profile. Control: once the slot is quiet the corpus is declared pending again."""
    from factlattice_engine import boundedfs
    from factlattice_engine.rootstate import reset_process_registry
    reset_process_registry(0.3)
    st, loader, spec = _retry_world(tmp_path)
    key = "profile:" + spec["profile"]
    reg = boundedfs.registry()
    gate = threading.Event()
    from factlattice.rootview import lexical
    slot = reg.slot("op:" + lexical(key), aux=True)                   # the slot a bounded_call for this key runs on
    from factlattice_engine.rootstate import Job
    slot.recover = lambda: Job("full", lambda: None, clock=reg.clock)   # what bounded_call sets: the returning call IS the recovery
    slot.submit("full", lambda: (gate.wait(20), None)[1], coalesce=False, foreground=True)
    assert _wait(lambda: slot.busy(), 2), "setup: a call is running on the profile's slot"
    loader.retry_profile_failed()
    assert "p1" in st._failed_corpora and "p1" not in st._pending_corpora, "retried while the profile's call was still running (busy)"
    assert _wait(lambda: (reg.rebalance(), slot.state() == "unresponsive")[1], 3), "setup: the profile's slot is now marked"
    loader.retry_profile_failed()
    assert "p1" in st._failed_corpora and "p1" not in st._pending_corpora, "retried while the profile's slot is unresponsive"
    gate.set()
    slot.recover = None                                                # a slot nobody recovers: the call returns and the state stays unresponsive, idle
    assert _wait(lambda: not slot.busy(), 5), "setup: the stuck call returned"
    assert slot.state() == "unresponsive", "setup: idle but still marked (recovery has not run)"
    loader.retry_profile_failed()
    assert "p1" in st._failed_corpora and "p1" not in st._pending_corpora, "retried although the profile's slot is still marked unresponsive (idle)"
    slot._state = "ok"                                                  # the recovery has run
    st2, loader2, _ = _retry_world(tmp_path / "second", reason=None)
    loader2.retry_profile_failed()
    assert "p1" in st2._failed_corpora and "p1" not in st2._pending_corpora, "a corpus that did not fail because of its profile was moved to pending"
    loader.retry_profile_failed()
    assert "p1" in st._pending_corpora and "p1" not in st._failed_corpora, "control: with the profile quiet the corpus is read again"
    assert boundedfs.key_state(key, reg) in (None, "ok")


def test_a_synchronous_boot_with_a_hung_declared_root_keeps_the_booted_table(tmp_path, monkeypatch):
    """F3 (D09): the provisional pin of the SYNCHRONOUS boot (`build_tools`), which has no loader to make it: with b1's root hung the final pin does not
    land, and a table read must still be the booted one after the registry is edited. Red without the provisional pin in `_pin_ledger_table_bounded`."""
    from factlattice import ledger
    w = Workspace(tmp_path / "w")
    s1, s2 = tmp_path / "w" / "stateA", tmp_path / "w" / "stateB"

    def regdata(state):
        d = json.loads(w.registry_path.read_text())
        d["named_roots"] = {"data": {"path": str(w.root("a1")), "state": str(state)}}
        return d
    w.registry_path.write_text(json.dumps(regdata(s1)))
    _env(w, monkeypatch, "0.5")
    ledger.unpin_table()
    from factlattice_engine.server import build_tools
    with RootStall(w.root("b1"), cap_s=40) as stall:
        try:
            out = run_limited(lambda: build_tools(watch=False, async_boot=False), 20.0, name="sync-boot")
            assert out.done and out.error is None, out.error
            w.registry_path.write_text(json.dumps(regdata(s2)))
            assert not ledger.pin_is_final(), "setup: the final pin did not land (the declared root hangs)"
            assert {n: v.get("state") for n, v in (ledger.table_context().table or {}).items()} == {"data": str(s1)}
        finally:
            stall.release()
            time.sleep(0.3)
            stall.__exit__(None, None, None)


def test_the_provisional_pin_treats_a_nul_root_as_the_final_pin_does_and_the_ledger_check_refuses_cleanly(tmp_path):
    """P3-2: a root path with a NUL is UNKNOWN to the final pin (`unprovable`); the provisional pin said otherwise and kept the root in `declared`, so
    `check_outside_roots` later raised a bare ValueError with no view installed. Both pins now agree, and the check refuses with a LedgerError."""
    from factlattice import ledger, registry
    entries = {"c": {"root": "/x/a\x00b"}, "ok": {"root": "/tmp"}}
    choice = registry.RegistryChoice("env", "/x/r.json", "0" * 64, "/x", entries)
    prov, fin = ledger.context_provisional(choice), ledger.context_deferred(choice)()
    assert prov.unprovable is True and fin.unprovable is True and prov.declared == ("/tmp",) and tuple(fin.declared) == (os.path.realpath("/tmp"),), (prov, fin)
    with pytest.raises(ledger.LedgerError):
        ledger.check_outside_roots(Path("/tmp/zz"), ["/x/a\x00b"])


def test_the_provisional_and_deferred_tables_are_the_choices_own_bytes_not_a_second_read_of_the_file(tmp_path, monkeypatch):
    """P2-3 at the function, not the flow: the pin is made after the one lookup, so a file edited BETWEEN the lookup and the pin must not be seen.
    Both `context_provisional` and `context_deferred` take the table from the choice. Red if either reads the file again."""
    from factlattice import ledger, registry
    w = Workspace(tmp_path / "w")
    s1, s2 = tmp_path / "w" / "stateA", tmp_path / "w" / "stateB"

    def regdata(state):
        d = json.loads(w.registry_path.read_text())
        d["named_roots"] = {"data": {"path": str(w.root("a1")), "state": str(state)}}
        return d
    w.registry_path.write_text(json.dumps(regdata(s1)))
    _env(w, monkeypatch, "5")
    choice = registry.resolve_registry()
    w.registry_path.write_text(json.dumps(regdata(s2)))                  # edited after the lookup
    for ctx in (ledger.context_provisional(choice), ledger.context_deferred(choice)()):
        assert {n: v.get("state") for n, v in (ctx.table or {}).items()} == {"data": str(s1)}


# ======================================================================================================================
# ROUND 5 (short read of 549c949)
# ======================================================================================================================
def _spy_lock(st, probe):
    """Replace the store's one lock by a Condition that calls `probe()` each time the outermost holder lets go: what another thread could see."""
    seen = []

    class Spy(threading.Condition):
        depth = 0

        def __enter__(self):
            Spy.depth += 1
            return super().__enter__()

        def __exit__(self, *a):
            Spy.depth -= 1
            if Spy.depth == 0:
                seen.append(probe())
            return super().__exit__(*a)
    st._pending_cv = Spy()
    return seen


def _pending_rec(spec, **kw):
    return {"spec": spec, "private": False, "paths": [], "domain_suffix": "x.p1", "since": time.time(), "stuck": False, **kw}


def test_the_listing_reads_all_four_tables_from_one_snapshot(tmp_path):
    """P1: `lts_list_corpora` took served+pending at its start and failed / no_roots much later, so a corpus the watcher moved failed -> pending in
    between was in neither list. The move is made right after the listing's first look at the store (whichever snapshot call it makes); p1 must be
    listed exactly once, as failed (the look it began with). Red on 549c949: in neither."""
    from factlattice_engine import tools as T
    from factlattice_engine.tools import LtsEngine
    st, _loader, spec = _retry_world(tmp_path)
    eng = LtsEngine(st)
    st._no_roots_corpora["p2"] = {"spec": {"roots": [{"path": str(tmp_path / "gone")}], "domain_suffix": "x.p2"}, "private": False}
    fired = []
    for name in ("snapshot", "snapshot_all"):
        if hasattr(st, name):
            def wrap(real=getattr(st, name)):
                def snap():
                    r = real()
                    if not fired:
                        fired.append(st.move_failed_to_pending("p1", _pending_rec(spec)))
                        st.clear_no_roots()                          # and the no-roots record goes too: still listed, from the look it began with
                    return r
                return snap
            setattr(st, name, wrap())
    out = T.dispatch("lts_list_corpora", {"heal": False}, tools=eng)
    assert fired == [True], "setup: the move happened right after the listing's first look"
    listed = [c["corpus_id"] for c in out["corpora"]] + [f["corpus_id"] for f in (out.get("failed_corpora") or [])]
    assert listed.count("p1") == 1, f"the corpus must be listed exactly once, never in neither list: {listed}"
    assert "p1" in st._pending_corpora and "p2" in listed, listed
    assert {"corpus": "p1", "reason": CODE_BUILD} in out["extent"]["corpora_skipped"], out["extent"]["corpora_skipped"]


def test_a_preview_names_a_corpus_the_watcher_moves_between_its_looks(tmp_path):
    """P2-1: `lts_preview` read failed, then pending, then failed again and no_roots one after another; a corpus that settled pending -> failed
    after the first look fell through all of them and was refused as `unknown_corpus`. The settle is made right after the preview's first look
    (the old code's `failed_corpora` truth test, or the new code's `lookup_all`). It must be refused with the corpus's own code. Red on 549c949."""
    from factlattice_engine import tools as T
    from factlattice_engine.store import failed_record
    from factlattice_engine.tools import LtsEngine
    st, _l, spec = _retry_world(tmp_path)
    st._failed_corpora.clear()
    st.declare_pending("p1", _pending_rec(spec))
    eng = LtsEngine(st)
    fired = []

    def settle():
        if not fired:
            fired.append(st.settle("p1", failed=failed_record(CODE_BUILD, None, False, "x.p1")))

    class Hook(dict):
        def __bool__(self):
            settle()
            return False
    st._failed_corpora = Hook()
    real = st.lookup_all
    st.lookup_all = lambda cid: (lambda r: (settle(), r)[1])(real(cid))
    out = T.dispatch("lts_preview", {"corpus": "p1", "content": "# x [X-Y]\n"}, tools=eng)
    assert fired == [True], "setup: the settle ran after the first look"
    assert out.get("success") is False and out.get("reason") != "unknown_corpus", out
    assert out.get("reason") == CODE_BUILD, out                     # the failed record's own code; the pending refusal says `root-unresponsive`


def test_the_flip_test_cannot_pass_by_its_thread_dying(tmp_path):
    """P2-3: the owner-of-a-domain hammer counts misses only while its flip thread lives. The flip count is a floor and the thread's exception is
    raised, so a dead thread fails the test instead of passing it vacuously. Here the thread is made to die at once and the helper must say so."""
    st, _loader, spec = _retry_world(tmp_path)
    calls = []

    def dies_after_one():
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("flip boom")
    with pytest.raises(AssertionError, match="flip thread died"):                       # it flipped once, then died: the floor (1) is met, the error is not lost
        _hammer_unserved_domain(st, spec, flip=dies_after_one, n=50, min_flips=1, until=lambda flips, errors: bool(errors))
    with pytest.raises(AssertionError, match="did not hammer"):                          # alive and silent, but it never flipped
        _hammer_unserved_domain(st, spec, flip=lambda: time.sleep(0.5), n=50, min_flips=100, until=lambda flips, errors: bool(flips))


def _hammer_unserved_domain(st, spec, *, flip, n, min_flips, until=None):
    from factlattice_engine.tools import LtsEngine
    eng = LtsEngine(st)
    stop = threading.Event()
    flips, errors = [], []

    def run():
        try:
            while not stop.is_set():
                flip()
                flips.append(1)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    misses = 0
    t = threading.Thread(target=run, daemon=True)
    t.start()
    if until is not None:                  # round 10 (P3-8): the flip thread is given the time to reach the state under test BEFORE the main loop counts, so the
        end = time.monotonic() + 20        # outcome never depends on which thread the scheduler ran first (3 failures in 150 runs under 36 burners were that)
        while not until(flips, errors) and time.monotonic() < end:
            time.sleep(0.001)
    try:
        for _ in range(n):
            if eng._unserved_domain("x.p1", st.snapshot_all()) is None:
                misses += 1
    finally:
        stop.set()
        t.join(10)
        sys.setswitchinterval(old)
    assert not errors, f"the flip thread died: {errors!r}"
    assert len(flips) >= min_flips, f"the flip thread flipped only {len(flips)} times: the hammer did not hammer (flip floor {min_flips})"
    return misses, len(flips)


def test_move_failed_to_pending_refuses_a_corpus_that_is_no_longer_failed(tmp_path):
    """P2-2 (F1a, F1f): the move is a no-op that says False when the corpus is not failed any more (unregistered, or registered again and seated
    meanwhile); the retry then starts no read. Shape: the failed record is cleared right before the move."""
    st, loader, spec = _retry_world(tmp_path)
    assert st.move_failed_to_pending("nope", _pending_rec(spec)) is False and not st._pending_corpora       # unit: not failed at all
    started = []
    loader._load_one = lambda cid: started.append(cid)
    real = st.move_failed_to_pending

    def wrapped(cid, rec, **kw):
        st._failed_corpora.pop(cid, None)           # what the seat of a register does to the failed record
        return real(cid, rec, **kw)
    st.move_failed_to_pending = wrapped
    loader.retry_profile_failed()
    time.sleep(0.3)
    assert "p1" not in st._pending_corpora and "p1" not in st._failed_corpora and started == [], (dict(st._pending_corpora), started)


def test_the_retry_carries_private_and_clears_no_roots_and_declares_the_roots(tmp_path):
    """P2-2 (F1e, F1b, F1g): the pending record keeps `private` (a disclosure if lost: a private name would be listed), the move clears a no_roots
    record of the same id, and the kernel is told the corpus's roots."""
    st, loader, spec = _retry_world(tmp_path)
    st._failed_corpora["p1"]["private"] = True
    st._no_roots_corpora["p1"] = {"spec": spec, "private": True}
    declared = []

    class K:
        def declare_roots(self, paths):
            declared.append(list(paths))
    loader.kernel = K()
    loader.retry_profile_failed()
    rec = st._pending_corpora["p1"]
    assert rec["private"] is True, "private must survive the move"
    assert "p1" not in st._no_roots_corpora and "p1" not in st._failed_corpora
    assert declared == [rec["paths"]] and declared[0], declared
    assert rec["domain_suffix"] == "x.p1" and rec["stuck"] is False


def test_none_loaded_error_does_not_fire_for_failed_or_pending_corpora():
    """P2-2 (F1m): a store that holds only failed (or only pending) corpora is not 'nothing loaded'; an empty store is."""
    from factlattice import registry
    from factlattice_engine import store as S
    from factlattice_engine.server import _none_loaded_error
    choice = registry.RegistryChoice("example", None, "d", "/r", {}, "/c", None, None)
    empty = S.LtsStore()
    assert _none_loaded_error(empty, choice, True) is not None, "control: an empty store is the registry-error case"
    failed = S.LtsStore()
    failed.fail("p1", CODE_BUILD, None, False, "x.p1")
    assert _none_loaded_error(failed, choice, True) is None
    pending = S.LtsStore()
    pending.declare_pending("p1", _pending_rec({}))
    assert _none_loaded_error(pending, choice, True) is None


def test_the_store_snapshots_are_one_look_under_the_lock():
    """P2-2 (F1j): `lookup_all`, `tables` and `snapshot_all` read their tables while holding the store's lock (a copy made outside it could straddle a
    move)."""
    from factlattice_engine import store as S
    st = S.LtsStore()
    st.fail("p1", CODE_BUILD, None, False, "x.p1")
    held = []

    class Spy(threading.Condition):
        depth = 0

        def __enter__(self):
            Spy.depth += 1
            return super().__enter__()

        def __exit__(self, *a):
            Spy.depth -= 1
            return super().__exit__(*a)

    class Probe(dict):
        def get(self, k, d=None):
            held.append(("get", Spy.depth))
            return super().get(k, d)

        def keys(self):
            held.append(("keys", Spy.depth))
            return super().keys()

        def __iter__(self):
            held.append(("iter", Spy.depth))
            return super().__iter__()

        def values(self):
            held.append(("values", Spy.depth))
            return super().values()
    st._pending_cv = Spy()
    for t in ("_pending_corpora", "_failed_corpora", "_no_roots_corpora", "_by_corpus_id"):
        setattr(st, t, Probe(getattr(st, t)))
    for call in (lambda: st.lookup_all("p1"), st.tables, st.snapshot_all):
        held.clear()
        call()
        assert len(held) >= 3 and all(d >= 1 for _k, d in held), (call, held)


def test_registering_a_failed_corpus_again_is_one_step_and_a_refusal_puts_it_back(tmp_path, monkeypatch):
    """P3: `register_corpus` added the partition and, after the seat, cleared the failed record: in between the corpus was served AND failed. Now one
    store step on the way in, and one on the way out for a refused seat (the failed record returns). At every moment the lock is let go the corpus
    is in exactly one of served / failed. Red on 549c949 (both)."""
    w = Workspace(tmp_path / "w")
    _env(w, monkeypatch)
    from factlattice import CorpusFileError
    from factlattice_engine import config
    from factlattice_engine.kernel import FreshnessKernel
    from factlattice_engine.tools import LtsEngine
    st = config.build_store({"a1": w.registry["a1"]})
    st.kernel = FreshnessKernel()
    st.fail("b1", "corpus-file-unreadable", "x.lts", False, "x.b1")
    eng = LtsEngine(st)
    seen = _spy_lock(st, lambda: ("b1" in st.corpus_ids(), "b1" in st._failed_corpora))
    real_seat = st.kernel.seat_partition

    def refuse(part):
        raise CorpusFileError("corpus-file-unreadable", "x.lts")
    st.kernel.seat_partition = refuse
    r = eng.register_corpus("b1", spec=w.registry["b1"])
    assert r["success"] is False and "b1" in st._failed_corpora and "b1" not in st.corpus_ids(), r
    assert st._failed_corpora["b1"]["code"] == "corpus-file-unreadable"
    st.kernel.seat_partition = real_seat
    r = eng.register_corpus("b1", spec=w.registry["b1"])
    assert r["success"] is True and "b1" in st.corpus_ids() and "b1" not in st._failed_corpora, r
    assert seen and all(a != b for a, b in seen), f"the corpus was in none (or both) of served / failed at a point another thread could look: {seen}"


def test_a_move_from_failed_to_pending_wakes_a_waiter_on_the_store():
    """P2-2 (F1d): the move notifies the store's condition, so a thread waiting for the tables to change (the write wait, `await_pending`) wakes at
    once instead of at its timeout."""
    from factlattice_engine import store as S
    st = S.LtsStore()
    st.fail("p1", CODE_BUILD, None, False, "x.p1")
    ready, woke = threading.Event(), []

    def waiter():
        with st._pending_cv:
            ready.set()
            t0 = time.monotonic()
            st._pending_cv.wait(8)
            woke.append(time.monotonic() - t0)
    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    assert ready.wait(5)
    assert st.move_failed_to_pending("p1", _pending_rec({})) is True
    t.join(10)
    assert woke and woke[0] < 4, woke


# ======================================================================================================================
# ROUND 6 (rule [STORE-TABLES-ONE-SNAPSHOT-ONE-STEP]: reads of several tables are one snapshot, moves check their precondition inside the step)
# ======================================================================================================================
def _world6(tmp_path, monkeypatch):
    w = Workspace(tmp_path / "w")
    _env(w, monkeypatch)
    return w


def _part6(w, cid):
    from factlattice_engine import config
    return config.build_store({cid: w.registry[cid]}).get_partition(cid)


def _after_first_look(st, names, act):
    """Run `act()` once, right after the FIRST call of any of the store's read methods in `names` returns (a point at which a real second thread
    could run). Methods a given tree does not have are skipped, so the same test drives an old tree and a new one."""
    fired = []
    for name in names:
        if hasattr(st, name):
            def wrap(real=getattr(st, name)):
                def call(*a, **k):
                    r = real(*a, **k)
                    if not fired:
                        fired.append(act())
                    return r
                return call
            setattr(st, name, wrap())
    return fired


def test_a_named_corpora_list_is_never_dropped_when_a_corpus_ends_its_read(tmp_path, monkeypatch):
    """F1: `_search_scope` looked at the served table and then the pending one. A corpus that ended its read between the two was in neither: with
    `corpora=['a1']` the answer was `(None, None)` ("no list applies": every public corpus is read instead), and for the server's listed corpora a
    served corpus was counted unknown and left out. Red on e8e3a9f."""
    from factlattice_engine import store as S
    from factlattice_engine import tools as T
    from factlattice_engine.tools import LtsEngine
    w = _world6(tmp_path, monkeypatch)
    part = _part6(w, "a1")
    monkeypatch.setattr(T, "default_corpora_listed", lambda: ["a1"])

    def world():
        st = S.LtsStore()
        st.declare_pending("a1", _pending_rec(w.registry["a1"], domain_suffix=w.registry["a1"]["domain_suffix"]))
        return st, LtsEngine(st)
    st, eng = world()                                                                       # CONTROL: no move
    assert eng._search_scope({"corpora": ["a1"]})[0]["read"] == ["a1"]
    st, eng = world()
    fired = _after_first_look(st, ("corpus_ids", "snapshot_all"), lambda: st.settle("a1", partition=part))
    scope, err = eng._search_scope({"corpora": ["a1"]})
    assert fired == [True] and err is None and scope is not None and scope["read"] == ["a1"], (scope, err)
    st, eng = world()
    fired = _after_first_look(st, ("corpus_ids", "snapshot_all"), lambda: st.settle("a1", partition=part))
    scope, err = eng._search_scope({})
    assert fired == [True] and scope["read"] == ["a1"] and scope["unknown_count"] == 0, scope
    # a named corpus that is NOT in the look (it is registered right after it): read, never dropped, never refused as unknown
    st = S.LtsStore()
    eng = LtsEngine(st)
    fired = _after_first_look(st, ("corpus_ids", "snapshot_all"), lambda: st.add_partition(part) is part)
    scope, err = eng._search_scope({"corpora": ["a1"]})
    assert fired == [True] and err is None and scope is not None and scope["read"] == ["a1"], (scope, err)


def test_the_listing_extent_counts_the_nodes_of_the_corpora_it_lists(tmp_path, monkeypatch):
    """F3: `nodes_in_scope` read the served table a second time, so a corpus registered right after the listing's snapshot was counted in the nodes
    and not in `corpora_consulted`. Red on e8e3a9f."""
    from factlattice_engine import store as S
    from factlattice_engine import tools as T
    from factlattice_engine.tools import LtsEngine
    w = _world6(tmp_path, monkeypatch)
    pa, pb = _part6(w, "a1"), _part6(w, "b1")
    st = S.LtsStore()
    st.add_partition(pa)
    eng = LtsEngine(st)
    fired = _after_first_look(st, ("snapshot_all",), lambda: st.add_partition(pb))
    out = T.dispatch("lts_list_corpora", {"heal": False}, tools=eng)
    assert fired, "setup: the registration happened after the listing's snapshot"
    assert out["extent"]["corpora_consulted"] == ["a1"] and out["extent"]["nodes_in_scope"] == len(pa.nodes), out["extent"]


def test_a_pending_refusal_in_a_preview_is_built_from_the_record_already_held(tmp_path, monkeypatch):
    """F4: the pending refusal read the pending table again by id; a corpus that settled between the look and the refusal made `dispatch` return
    None. Red on e8e3a9f (None)."""
    from factlattice_engine import store as S
    from factlattice_engine import tools as T
    from factlattice_engine.tools import LtsEngine
    w = _world6(tmp_path, monkeypatch)
    part = _part6(w, "a1")
    st = S.LtsStore()
    st.declare_pending("a1", _pending_rec(w.registry["a1"], domain_suffix=w.registry["a1"]["domain_suffix"]))
    eng = LtsEngine(st)
    eng._await_loaded = lambda cids, deadline_s=None: None                    # the wait ended with the corpus still pending
    real, n = st.lookup_all, []

    def la(cid):
        r = real(cid)
        n.append(1)
        if len(n) == 2:                                                       # the look after the wait: still pending ... and it settles before the refusal
            st.settle("a1", partition=part)
        return r
    st.lookup_all = la
    out = T.dispatch("lts_preview", {"corpus": "a1", "content": "# x [X-Y]\n"}, tools=eng)
    assert len(n) == 2 and isinstance(out, dict) and out.get("success") is False, out


def test_the_declared_map_and_the_registered_roots_are_one_look(tmp_path, monkeypatch):
    """F6: `_declared_map` read the served table and then the others; `registered_roots` the served table and then the failed one. A corpus that
    moved between the two reads was missing from the answer. The move is made right after the first look. Red on e8e3a9f."""
    from factlattice_engine import membership
    from factlattice_engine import store as S
    w = _world6(tmp_path, monkeypatch)
    pa, pb = _part6(w, "a1"), _part6(w, "b1")
    for fn, where in ((lambda st: membership._declared_map(st.snapshot_all()), "pending"), (lambda st: membership.registered_roots(st)[0], "failed")):
        st = S.LtsStore()
        st.add_partition(pa)
        if where == "pending":
            st.declare_pending("b1", _pending_rec(w.registry["b1"], domain_suffix=w.registry["b1"]["domain_suffix"]))
            act = lambda: st.settle("b1", partition=pb)                                                 # noqa: E731
        else:
            st.fail("b1", "corpus-file-unreadable", "x.lts", False, w.registry["b1"]["domain_suffix"])
            act = lambda: st.add_partition_clearing(pb)                                                 # noqa: E731
        fired = _after_first_look(st, ("partitions", "snapshot_all"), act)
        got = fn(st)
        if where == "pending":
            fired.append(act())        # `_declared_map` takes the look as an argument now: the move is made after it was taken
        assert fired and "b1" in got and "a1" in got, (where, sorted(got))


def test_unregister_of_a_corpus_that_ends_its_read_meanwhile_removes_it(tmp_path, monkeypatch):
    """The unregister saw the corpus pending, then forgot it by id; one that had been served meanwhile answered `was_pending: true` and stayed
    served. The forget checks under the lock, and the call looks again. Red on e8e3a9f."""
    from factlattice_engine import store as S
    from factlattice_engine.tools import LtsEngine
    w = _world6(tmp_path, monkeypatch)
    part = _part6(w, "a1")
    st = S.LtsStore()
    st.declare_pending("a1", _pending_rec(w.registry["a1"], domain_suffix=w.registry["a1"]["domain_suffix"]))
    eng = LtsEngine(st)
    real, fired = st.forget_pending, []

    def forget(cid):
        if not fired:
            fired.append(st.settle(cid, partition=part))                       # it ends its read right before the forget
        return real(cid)
    st.forget_pending = forget
    out = eng.unregister_corpus("a1")
    assert fired == [True] and out["success"] is True and not out.get("was_pending"), out
    assert "a1" not in st.corpus_ids() and "a1" not in st._pending_corpora


def test_a_register_that_loses_the_race_to_the_watcher_refuses_and_discards_the_build(tmp_path, monkeypatch):
    """F2, order 1 (watcher first). `register_corpus` checked "not served, not pending" at its top, built (long), then seated without looking again. The
    watcher's retry moves the profile-failed corpus to pending during the build (the register thread is held at the seat by an event). The seat must
    refuse with the pending-id refusal, change nothing, and leave the corpus pending ONLY. Red on e8e3a9f: served and pending together."""
    from factlattice_engine.kernel import FreshnessKernel
    from factlattice_engine.tools import LtsEngine
    st, loader, spec = _retry_world(tmp_path)
    spec2 = {"roots": spec["roots"], "domain_suffix": spec["domain_suffix"]}
    st.kernel = FreshnessKernel()
    eng = LtsEngine(st)
    at_seat, go = threading.Event(), threading.Event()
    real_seat = getattr(st, "add_partition_clearing", None) or st.add_partition
    name = "add_partition_clearing" if hasattr(st, "add_partition_clearing") else "add_partition"

    def held(part):
        at_seat.set()
        assert go.wait(15)
        return real_seat(part)
    setattr(st, name, held)
    res = {}
    t = threading.Thread(target=lambda: res.update(r=eng.register_corpus("p1", spec=spec2)), daemon=True)
    t.start()
    assert at_seat.wait(15), "setup: the build finished and the register reached the seat"
    assert st.move_failed_to_pending("p1", _pending_rec(spec, domain_suffix="x.p1")) is True       # the watcher, mid-register
    go.set()
    t.join(20)
    r = res["r"]
    assert r["success"] is False and "still being read" in r["error"], r
    assert "p1" not in st.corpus_ids() and "p1" in st._pending_corpora and "p1" not in st._failed_corpora
    assert st.kernel.tracked_roots(type("P", (), {"corpus_id": "p1"})()) is None, "the discarded build was never seated"


def test_a_watcher_retry_that_loses_the_race_to_a_register_changes_nothing(tmp_path, monkeypatch):
    """F2, order 2 (register first). The watcher's retry has looked at the failed record and is held at the move (an event); `register_corpus`
    seats the corpus whole. The move's own no-longer-failed check, under the lock, must stop it: no pending record, no load thread, served only."""
    from factlattice_engine.kernel import FreshnessKernel
    from factlattice_engine.tools import LtsEngine
    st, loader, spec = _retry_world(tmp_path)
    spec2 = {"roots": spec["roots"], "domain_suffix": spec["domain_suffix"]}
    st.kernel = FreshnessKernel()
    eng = LtsEngine(st)
    started = []
    loader._load_one = lambda cid: started.append(cid)
    at_move, go = threading.Event(), threading.Event()
    real = st.move_failed_to_pending
    out = {}

    def held(cid, rec, **kw):
        at_move.set()
        assert go.wait(15)
        out["moved"] = real(cid, rec, **kw)
        return out["moved"]
    st.move_failed_to_pending = held
    t = threading.Thread(target=loader.retry_profile_failed, daemon=True)
    t.start()
    assert at_move.wait(15), "setup: the watcher read the failed record and is at the move"
    r = eng.register_corpus("p1", spec=spec2)
    assert r["success"] is True, r
    go.set()
    t.join(20)
    time.sleep(0.2)
    assert out["moved"] is False and started == [], (out, started)
    assert "p1" in st.corpus_ids() and "p1" not in st._pending_corpora and "p1" not in st._failed_corpora


def test_the_seat_store_failure_is_one_step(tmp_path):
    """F7: `kernel.seat_store` removed a partition and then recorded the failure (two calls); it is one step now, `remove_partition(failed=)`. At every
    point the lock is let go the corpus is in exactly one of served / failed, on both failure paths. Red on e8e3a9f (in neither)."""
    from factlattice import CorpusFileError
    from factlattice_engine import rootstate as _rs
    from factlattice_engine.kernel import FreshnessKernel
    w = Workspace(tmp_path / "w")
    from factlattice_engine import config
    for exc in (CorpusFileError("corpus-file-unreadable", "x.lts"), _rs.RootNotAnswering("p")):
        st = config.build_store({"a1": w.registry["a1"]})
        k = FreshnessKernel()

        def boom(part, exc=exc):
            raise exc
        k.seat_partition = boom
        seen = _spy_lock(st, lambda: ("a1" in st.corpus_ids(), "a1" in st._failed_corpora))
        k.seat_store(st)
        assert "a1" not in st.corpus_ids() and "a1" in st._failed_corpora
        assert seen and all(a != b for a, b in seen), f"{type(exc).__name__}: the corpus was in none (or both) of served / failed: {seen}"


def test_the_rollbacks_of_a_register_restore_what_the_seat_dropped(tmp_path, monkeypatch):
    """F5 (M15, M8): the `RootNotAnswering` rollback and the no-roots record. A corpus listed with no roots, registered again, and refused at the
    seat, is listed with no roots again (not lost); and on a root that does not answer the failed record returns too."""
    from factlattice import CorpusFileError
    from factlattice_engine import rootstate as _rs
    from factlattice_engine.kernel import FreshnessKernel
    from factlattice_engine.tools import LtsEngine
    w = _world6(tmp_path, monkeypatch)
    from factlattice_engine import config
    for exc in (_rs.RootNotAnswering("b1"), CorpusFileError("corpus-file-unreadable", "x.lts")):
        for kind in ("failed", "no_roots"):
            st = config.build_store({"a1": w.registry["a1"]})
            st.kernel = FreshnessKernel()
            rec = {"code": "corpus-file-unreadable", "path": "x.lts", "private": False, "domain_suffix": "x.b1"} if kind == "failed" else {"spec": w.registry["b1"], "private": False}
            (st._failed_corpora if kind == "failed" else st._no_roots_corpora)["b1"] = rec

            def boom(part, exc=exc):
                raise exc
            st.kernel.seat_partition = boom
            r = LtsEngine(st).register_corpus("b1", spec=w.registry["b1"])
            assert r["success"] is False and "b1" not in st.corpus_ids(), (type(exc).__name__, kind, r)
            table = st._failed_corpora if kind == "failed" else st._no_roots_corpora
            assert table.get("b1") == rec, (type(exc).__name__, kind, "the record the seat dropped was not put back", dict(table))


def test_an_unexpected_error_loading_one_corpus_fails_that_corpus_only_on_the_first_pass(tmp_path, monkeypatch):
    """[MECH-BR-BOOT] x-load-error, first pass, library and server alike. A load that raises (here a TypeError from the read of b1) fails b1 in one store
    step as `corpus-load-error`, path null, the CLASS NAME only; a1 keeps serving; `build_store` RETURNS the entry instead of raising; and the server is
    NOT in registry-error mode. Red on 5d668c4: `build_store` raised and the server answered `registry-not-found` for the healthy a1."""
    from factlattice_engine import bootload, config
    from factlattice_engine.server import build_tools
    w = _world6(tmp_path, monkeypatch)
    real = bootload.StoreLoader._read

    def read(self, cid, spec, rec):
        if cid == "b1":
            raise TypeError("a message that must never be shown: " + str(w.root("b1")))
        return real(self, cid, spec, rec)
    monkeypatch.setattr(bootload.StoreLoader, "_read", read)
    st = config.build_store(w.registry)                                  # no exception
    assert st.corpus_ids() == ["a1"] and st.tables()[1]["b1"] == {"code": "corpus-load-error", "path": None, "private": False,
                                                                  "domain_suffix": w.registry["b1"]["domain_suffix"], "error": "TypeError"}, st.tables()
    assert not st.tables()[0], "nothing left pending"
    eng = build_tools(watch=False, async_boot=True)
    _LOADERS.append(eng.boot_loader)
    assert eng.boot_loader.first_pass_done.wait(30)
    from factlattice_engine import tools as T
    named = T.dispatch("lts_find", {"corpus": "a1", "prefix": "A1"}, tools=eng)
    assert named.get("success") is True or named.get("reason") != "registry-not-found", named
    assert getattr(eng, "registry_error", None) is None
    bad = T.dispatch("lts_node", {"corpus": "b1", "node_id": "B1-ROOT"}, tools=eng)
    assert bad["success"] is False and bad["reason"] == "corpus-load-error" and bad["error_class"] == "CorpusLoadError", bad
    assert "message that must never" not in json.dumps(bad) and str(w.root("b1")) not in json.dumps(bad)
    listing = T.dispatch("lts_list_corpora", {"heal": False}, tools=eng)
    assert {"corpus_id": "b1", "code": "corpus-load-error", "error": "TypeError"} in listing["failed_corpora"], listing.get("failed_corpora")


def test_a_retry_load_that_raises_never_leaves_its_corpus_in_no_table(tmp_path):
    """[MECH-BR-BOOT] x-load-error, after the first pass (the retry of a profile-failed corpus): the corpus goes failed -> pending -> failed
    (`corpus-load-error`) and is in exactly one table at every moment the lock is let go. Red on 5d668c4: it ended in none."""
    st, loader, spec = _retry_world(tmp_path)
    from factlattice_engine import bootload
    loader._load_one = bootload.StoreLoader._load_one.__get__(loader)           # the real one (the world stubs it out)

    def boom(cid, rec_spec, rec):
        raise RuntimeError("secret " + str(tmp_path))
    loader._read = boom
    seen = _spy_lock(st, lambda: ("p1" in st._pending_corpora, "p1" in st._failed_corpora, "p1" in st._no_roots_corpora))
    loader.retry_profile_failed()
    assert _wait(lambda: "p1" in st.tables()[1] and st.tables()[1]["p1"].get("code") == "corpus-load-error", 10)
    assert st.tables()[1]["p1"] == {"code": "corpus-load-error", "path": None, "private": False, "domain_suffix": "x.p1", "error": "RuntimeError"}
    assert seen and all(sum(x) == 1 for x in seen), f"in none (or several) of pending / failed / no_roots at a moment another thread could look: {seen}"
    assert "secret" not in json.dumps(st.tables()[1])


def test_a_second_register_of_one_id_that_loses_to_the_first_refuses_at_the_seat(tmp_path):
    """F2 (served): two registers of one id; the first is held at the seat while the second seats it. The first must refuse with the already-loaded
    refusal (a dict), not raise out of the store."""
    from factlattice_engine.tools import LtsEngine
    st, _loader, spec = _retry_world(tmp_path)
    st._failed_corpora.clear()
    spec2 = {"roots": spec["roots"], "domain_suffix": spec["domain_suffix"]}
    eng = LtsEngine(st)
    at_seat, go = threading.Event(), threading.Event()
    real = st.add_partition_clearing
    first = []

    def held(part):
        if not first:
            first.append(1)
            at_seat.set()
            assert go.wait(15)
        return real(part)
    st.add_partition_clearing = held
    res = {}
    t = threading.Thread(target=lambda: res.update(r=eng.register_corpus("p1", spec=spec2)), daemon=True)
    t.start()
    assert at_seat.wait(15)
    assert eng.register_corpus("p1", spec=spec2)["success"] is True
    go.set()
    t.join(20)
    assert res["r"]["success"] is False and "already loaded" in res["r"]["error"], res
    assert st.corpus_ids() == ["p1"]


# ======================================================================================================================
# ROUND 7 (read of 5d668c4: the barrier is structural; G1 G2 G3; x-load-error; P3s)
# ======================================================================================================================
def _call_with_look(fn, *a, look_fn):
    """Call `fn(*a, look)` if the tree's `fn` takes a look, else `fn(*a)`: so one test drives an old tree and a new one."""
    import inspect
    if "look" in inspect.signature(fn).parameters:
        return fn(*a, look_fn())
    return fn(*a)


def test_a_not_found_reply_names_no_private_corpus_registered_between_its_looks(tmp_path, monkeypatch):
    """G1: `_gate`'s not-found path built the public list from two looks (the private ids from one snapshot, the served ids from another). A PRIVATE
    corpus registered between them was in the list a reply may name; only the extent's leak guard stopped it (the caller got an AssertionError text
    instead of the normal not-found). One look for both lists now. Red on 5d668c4."""
    from factlattice_engine import store as S
    from factlattice_engine import tools as T
    from factlattice_engine.tools import LtsEngine
    w = _world6(tmp_path, monkeypatch)
    st = S.LtsStore()
    st.add_partition(_part6(w, "a1"))
    from factlattice_engine import config as _cfg
    q = _cfg.build_store({PRIV_ID: w.registry["b1"]}).get_partition(PRIV_ID)
    q.private_reason = "test-private"
    eng = LtsEngine(st)
    fired = _after_first_look(st, ("snapshot_all",), lambda: st.add_partition(q) is q)
    out = T.dispatch("lts_node", {"corpus": "zzz", "node_id": "X"}, tools=eng)
    assert fired == [True], "setup: the private corpus was registered after the reply's first look"
    assert "AssertionError" not in json.dumps(out) and "leak guard" not in json.dumps(out), out
    assert out["success"] is False and PRIV_ID not in json.dumps(out) and out.get("corpora_available", out.get("available")) in (["a1"], None), out


def test_a_hop_into_a_corpus_that_ends_its_read_meanwhile_is_declared_not_unresolved(tmp_path, monkeypatch):
    """G2: the hop router looked at the served table twice (through the router) and then at the unserved tables; a target that was pending at the
    router's looks and served by the last one came out `unresolved`. The caller's one look decides now: pending in the look, declared stale
    (`root-pending`, `unserved: true`). Red on 5d668c4."""
    from factlattice_engine import store as S
    from factlattice_engine.tools import LtsEngine
    w = _world6(tmp_path, monkeypatch)
    st = S.LtsStore()
    st.declare_pending("a1", _pending_rec(w.registry["a1"], domain_suffix=w.registry["a1"]["domain_suffix"]))
    part = _part6(w, "a1")
    eng = LtsEngine(st)
    edge = {"target": "A1-N", "target_domain": w.registry["a1"]["domain_suffix"]}
    calls, fired = [], []
    real_fbd = st.filter_by_domain

    def fbd(dom):                                                       # an old tree: the move lands after the router's SECOND served look
        r = real_fbd(dom)
        calls.append(1)
        if len(calls) == 2 and not fired:
            fired.append(st.settle("a1", partition=part))
        return r
    st.filter_by_domain = fbd
    import inspect
    look = st.snapshot_all()                                                                           # a new tree: the caller's look, then the move
    if "look" in inspect.signature(eng._route).parameters:
        fired.append(st.settle("a1", partition=part))
    kind, payload = _call_with_look(eng._route, edge, "b1", look_fn=lambda: look)
    assert fired == [True]
    assert kind == "stale" and payload["reason"] == "root-pending" and payload["unserved"] is True, (kind, payload)


def test_the_mark_of_a_corpus_comes_from_the_callers_look(tmp_path, monkeypatch):
    """G3: `_corpus_mark` looked at the served table and then the pending one; a corpus that ended its read between them lost its mark and the skip
    entry read the generic `stale`. Red on 5d668c4."""
    from factlattice_engine import store as S
    from factlattice_engine.tools import LtsEngine
    w = _world6(tmp_path, monkeypatch)
    st = S.LtsStore()
    st.declare_pending("a1", _pending_rec(w.registry["a1"], domain_suffix=w.registry["a1"]["domain_suffix"]))
    part = _part6(w, "a1")
    eng = LtsEngine(st)
    fired = _after_first_look(st, ("get_partition",), lambda: st.settle("a1", partition=part))      # an old tree: after the served look
    import inspect
    look = st.snapshot_all()
    if "look" in inspect.signature(eng._stale_skip_entries).parameters:
        fired.append(st.settle("a1", partition=part))
    out = _call_with_look(eng._stale_skip_entries, ["a1"], look_fn=lambda: look)
    assert fired == [True] and out == [{"corpus": "a1", "reason": "root-pending"}], out


def test_the_router_takes_its_partitions_from_the_caller(tmp_path, monkeypatch):
    """The router resolves a domain in the partitions it is handed (the caller's look), and asks the store only when it is handed none."""
    from factlattice_engine import store as S
    from factlattice_engine.router import Router
    w = _world6(tmp_path, monkeypatch)
    st = S.LtsStore()
    pa = _part6(w, "a1")
    r = Router(st)
    assert r._resolve_corpus(pa.domain_suffix, [pa]) == ("resolved", pa)
    assert r._resolve_corpus(pa.domain_suffix, []) == ("unresolved_corpus", None), "a handed list is the whole truth: the (empty) store is not asked"
    assert r._resolve_corpus(pa.domain_suffix) == ("unresolved_corpus", None)


def test_the_listing_root_health_comes_from_the_listings_snapshot(tmp_path, monkeypatch):
    """P3-2: `_root_health_view` read the served table again after the snapshot; a corpus unregistered meanwhile lost its `root_health` field.
    Staged with a corpus that declares a missing second root (so it is degraded). Red on 5d668c4."""
    from factlattice_engine import config
    from factlattice_engine import store as S
    from factlattice_engine import tools as T
    from factlattice_engine.tools import LtsEngine
    w = _world6(tmp_path, monkeypatch)
    spec = dict(w.registry["a1"])
    spec["roots"] = [{"path": str(w.root("a1"))}, {"path": str(tmp_path / "no-such-root")}]
    spec.pop("root", None)
    part = config.build_store({"a1": spec}).get_partition("a1")
    st = S.LtsStore()
    st.add_partition(part)
    eng = LtsEngine(st)
    ctrl = T.dispatch("lts_list_corpora", {"heal": False}, tools=eng)
    assert ctrl["corpora"][0].get("root_health", {}).get("status") == "degraded", ctrl["corpora"]
    fired = _after_first_look(st, ("snapshot_all",), lambda: st.remove_partition("a1") is part)
    out = T.dispatch("lts_list_corpora", {"heal": False}, tools=eng)
    assert fired == [True] and out["corpora"][0]["corpus_id"] == "a1"
    assert out["corpora"][0].get("root_health", {}).get("status") == "degraded", "listed from the snapshot: its root_health is kept"


def test_registered_roots_takes_one_look_and_hands_its_failed_table_down(tmp_path, monkeypatch):
    """P3-3: `registered_roots` took its look and then `current_view` -> `membership.snapshot` read the failed table again. One look now."""
    from factlattice_engine import membership
    from factlattice_engine import store as S
    w = _world6(tmp_path, monkeypatch)
    st = S.LtsStore()
    st.add_partition(_part6(w, "a1"))
    calls = []
    real = st.snapshot_all

    def counted():
        calls.append(1)
        return real()
    st.snapshot_all = counted
    membership.registered_roots(st)
    assert len(calls) == 1, f"{len(calls)} looks for one call"


def test_the_retry_decides_the_profile_reason_inside_the_move(tmp_path):
    """P3-4: the retry read `old.get("reason") == "profile"` from a record it read BEFORE the move; the move checked only that the corpus was still
    failed. A record that stopped being a profile failure in between was moved to pending anyway. The reason is checked inside the step now (and
    `private` and `domain_suffix` come from the record that is moved). Red on 5d668c4."""
    st, loader, spec = _retry_world(tmp_path)
    started = []
    loader._load_one = lambda cid: started.append(cid)
    real = st.move_failed_to_pending

    def swapped(cid, rec, **kw):
        st._failed_corpora["p1"] = {"code": "corpus-file-unreadable", "path": "x.lts", "private": False, "domain_suffix": "x.p1"}      # not a profile failure now
        return real(cid, rec, **kw)
    st.move_failed_to_pending = swapped
    loader.retry_profile_failed()
    time.sleep(0.2)
    assert started == [] and "p1" not in st._pending_corpora and st._failed_corpora["p1"]["code"] == "corpus-file-unreadable", (started, dict(st._pending_corpora))


def test_the_tables_are_private_to_the_store(tmp_path):
    """The tables have no public spelling any more: code outside the store cannot read or write them by their old names."""
    from factlattice_engine import store as S
    st = S.LtsStore()
    for name in ("pending_corpora", "failed_corpora", "no_roots_corpora"):
        assert not hasattr(st, name), name
    assert not hasattr(st, "clear_unserved")


def test_a_named_corpus_in_the_look_is_in_scope_even_if_it_is_unregistered_right_after(tmp_path, monkeypatch):
    """M21 as a behaviour test: the scope is decided from the ONE look. A named corpus that was served in the look but is unregistered right after it is
    still in the read list (the gate that follows refuses it by name); a scope that took a second served look would answer from a different moment."""
    from factlattice_engine import store as S
    from factlattice_engine.tools import LtsEngine
    w = _world6(tmp_path, monkeypatch)
    part = _part6(w, "a1")
    st = S.LtsStore()
    st.add_partition(part)
    eng = LtsEngine(st)
    fired = _after_first_look(st, ("corpus_ids", "snapshot_all"), lambda: st.remove_partition("a1") is part)
    scope, err = eng._search_scope({"corpora": ["a1", "a1"]})
    assert fired == [True] and err is None and scope["read"] == ["a1"], (scope, err)


# ======================================================================================================================
# ROUND 8 (read of 2ef8895: entry shapes, F1 F4 F5 F7 F8 F9)
# ======================================================================================================================
# round 11: the BOOT verdict of every shape is release af4cfc2's (tests/test_boot_verdict_table.py is the whole table). Here: the shapes that release
# raised on still raise, the rest do not and the healthy corpus beside them is served; and the registry cannot state where such an entry lives, whatever
# the boot did, so the store records it (`unlocated`) and a write lands flagged.
@pytest.mark.parametrize("shape,raises,unlocated", [({"roots": "nope"}, True, False), ({"roots": 5}, True, False), ({"roots": [7]}, True, False),
                                                    ({"roots": [{"path": 7}]}, False, True), ({"root": 5}, False, True),
                                                    ({"private": "yes", "root": "/x"}, False, False), ({"profile": 5, "root": "/x"}, False, False),
                                                    ({"roots": [{"path": "/x", "only_file": 1}]}, False, False), ({"root": "/x", "domain_suffix": 3}, False, False)])
def test_a_malformed_entry_shape_keeps_the_releases_boot_verdict_and_unlocated_holds_only_what_has_no_readable_roots(tmp_path, monkeypatch, shape, raises, unlocated):
    """The third amendment (specs/38 x-load-error): no entry shape's boot verdict changes from release af4cfc2. Red on fe465bc for the shapes the release
    served (it raised for all nine). `unlocated` (clarified 2026-11-06, (1)) holds an entry only when the release does not serve it AND its roots cannot be
    read as paths: `roots: [{path: 7}]` and `root: 5` yes; `private: "yes"` or `domain_suffix: 3` with a readable root never (a missing `/x` is still a
    path)."""
    from factlattice_engine import config
    w = _world6(tmp_path, monkeypatch)
    reg = {"a1": w.registry["a1"], "zz-bad": dict(shape)}
    if raises:
        with pytest.raises((TypeError, AttributeError, ValueError)) as ei:
            config.build_store(reg)
        assert "zz-bad" not in str(ei.value) and "a1" not in str(ei.value), "the message names a class and a kind, never an id"
        return
    st = config.build_store(reg)
    assert "a1" in st.corpus_ids(), "the healthy corpus beside it is served"
    assert st.unlocated() == (frozenset({"zz-bad"}) if unlocated else frozenset()), st.unlocated()


def test_an_entry_with_an_unknown_key_still_fails_its_own_corpus_only(tmp_path, monkeypatch):
    """The shape check does not take over the unknown-key rule: an entry that carries an unknown key fails that corpus (`registry-entry-unknown-key`),
    and a healthy corpus beside it is served."""
    from factlattice_engine import config
    w = _world6(tmp_path, monkeypatch)
    bad = dict(w.registry["b1"])
    bad["bogus_key"] = 1
    st = config.build_store({"a1": w.registry["a1"], "b1": bad})
    assert st.corpus_ids() == ["a1"] and st.tables()[1]["b1"]["code"] == "registry-entry-unknown-key"


def test_a_corpus_that_is_both_served_and_pending_is_not_failed_when_its_load_raises(tmp_path, monkeypatch):
    """F4. The state is kept out by the seat's own check and is staged by hand (the loader declares a corpus that is already served). The loader's
    catch for a load that raises moves the corpus to failed only through `settle`, which checks INSIDE the step that it is not served: the pending
    record is dropped, nothing is recorded as failed, the corpus stays served, the call neither raises nor sets anything, and the corpus after it is
    still loaded. Red on 2ef8895: served AND failed. (Replaces the deleted evidence test: its outcome was a raised ValueError.)"""
    from factlattice_engine import bootload, config
    w = _world6(tmp_path, monkeypatch)
    reg = {c: w.registry[c] for c in ("a1", "b1")}
    st = config.build_store({"a1": reg["a1"]})
    loader = bootload.StoreLoader(st, reg, kernel=None, retry=False, workers=1)
    loader.declare()
    assert sorted(st.tables()[0]) == ["a1", "b1"] and "a1" in st.corpus_ids(), "setup: a1 is served and pending"
    loader.run_blocking()
    pending, failed, no_roots = st.tables()
    assert sorted(st.corpus_ids()) == ["a1", "b1"] and not pending and not failed and not no_roots, (pending, failed, no_roots)


def test_settle_refuses_a_move_to_failed_or_no_roots_for_a_served_corpus_inside_the_step(tmp_path, monkeypatch):
    """F4 at the store: `settle(failed=)` / `settle(no_roots=)` on a corpus that is served drops its stale pending record and records nothing, and says
    False."""
    from factlattice_engine import store as S
    w = _world6(tmp_path, monkeypatch)
    for kw in ({"failed": S.failed_record("corpus-load-error", None, False, "x.a1", error="X")}, {"no_roots": {"spec": {}, "private": False}}):
        st = S.LtsStore()
        st.add_partition(_part6(w, "a1"))
        st.declare_pending("a1", _pending_rec(w.registry["a1"]))
        assert st.settle("a1", **kw) is False
        pending, failed, no_roots = st.tables()
        assert "a1" in st.corpus_ids() and not pending and not failed and not no_roots


def test_reviewer_failed_container_stop_asks_the_store_and_catches_nothing(tmp_path):
    """Round 9 (P3): the reviewer's safety stop asks `store.tables()` and catches nothing: a double gives the method, a store without it raises (every
    store that reaches this in production is an `LtsStore`). With a double that has the method and nothing failed: no stop."""
    from types import SimpleNamespace

    from factlattice_engine.reviewer.__main__ import _failed_container_stop
    assert _failed_container_stop(SimpleNamespace(store=SimpleNamespace(tables=lambda: ({}, {}, {}))), {}, str(tmp_path / "x.lts")) is None
    with pytest.raises(AttributeError):
        _failed_container_stop(SimpleNamespace(store=SimpleNamespace()), {}, str(tmp_path / "x.lts"))


@pytest.mark.parametrize("spec", [{"roots": "nope"}, {"roots": 5}, {"roots": [7]}, {"roots": [{"path": 7}]}, {"root": 5}])
def test_the_ledger_counts_a_malformed_entry_shape_as_unprovable(spec):
    """F2 (hardening): the ledger's declared roots of an entry whose `roots` / `root` has a malformed shape are UNPROVABLE (the entry's location cannot be
    trusted), never 'no roots, provable'. Red on 2ef8895 (`((), False)`)."""
    from factlattice import ledger
    paths, unprovable = ledger._declared_roots({"c": spec})
    assert unprovable is True, (spec, paths)
    assert ledger._declared_roots({"c": {"roots": [{"path": "/x"}]}})[1] is False, "control: a clean entry is provable"
    assert ledger._declared_roots({"c": {"roots": [{"area": "x"}]}})[1] is True, "round 10 (2359cc4): a root object with no path and no unknown key is a shape problem"



def test_an_entry_with_an_unknown_key_and_a_malformed_shape_fails_its_own_corpus_not_the_registry(tmp_path, monkeypatch):
    """An entry that carries an unknown key (a misspelt root key makes its roots malformed too) is the unknown-key rule's: it fails ITS corpus and the
    healthy one is served; the shape rule does not take it. (Kills the shape check being applied to those entries.)"""
    from factlattice_engine import config
    w = _world6(tmp_path, monkeypatch)
    st = config.build_store({"a1": w.registry["a1"], "b1": {"roots": [{"pth": str(w.root("b1"))}], "domain_suffix": "x.b1"}})
    assert st.corpus_ids() == ["a1"] and st.tables()[1]["b1"]["code"] == "registry-root-unknown-key"


def test_membership_refuses_for_a_failed_store_look_and_keeps_the_pinned_rule_for_the_registry(tmp_path, monkeypatch):
    """F1, layer by layer, with the pinned rule kept (MECH-BR-WRITES D, as spec 38 states it): a failure of the STORE'S look refuses (unconfirmed); a
    failure of a REGISTRY read is the registry stage's and does not (a RegistryError, or a failure inside the roots lookup, is the write's one result
    and lands it flagged). Round 11 (P2-6; spec 38 x-membership: "that failure is the one result of the write's membership evaluation, kept for the whole write"):
    the confirm stage's declared-roots read and the failed-corpora read of `registered_roots` no longer LEAVE the served corpora's roots when the registry cannot
    be read: they raise it, and `check` makes it the registry stage's result (the write lands flagged). Two assertions here changed with that, (2) and (4).
    The old "leaves the served corpora's roots" carve-out was never ruled: it first appears as a code comment ("as before") in slice 3 (864fda7), is not on
    release af4cfc2, and spec 38 does not state it. Spec 38 (D) decides: a registry failure is the write's one result and the write lands flagged."""
    from factlattice.registry import RegistryError
    from factlattice_engine import config, membership
    from factlattice_engine import store as S
    w = _world6(tmp_path, monkeypatch)
    # (1) the look itself raises in `snapshot`: the view is marked unconfirmed there
    class Broken:
        def snapshot_all(self):
            raise RuntimeError("no look")
    v = membership.snapshot(Broken())
    assert v.look is None and v.unconfirmed_count >= 1
    out = membership.check(str(tmp_path / "x.lts"), store=Broken())
    assert out["membership_partial"]["unconfirmed_count"] >= 1 and out["inside_corpus"] is None and out["corpora"] == [], out
    # (2) `_declared_map` takes the look it is given; an unreadable REGISTRY raises (round 11: it used to leave the served corpora's roots)
    st = S.LtsStore()
    st.add_partition(_part6(w, "a1"))
    st.declare_pending("b1", _pending_rec(w.registry["b1"]))
    monkeypatch.setattr(config, "default_corpora", lambda: (_ for _ in ()).throw(RegistryError("unreadable")))
    with pytest.raises(RegistryError):
        membership._declared_map(st.snapshot_all())
    # (3) a failure in the confirm stage that is NOT the registry's notes the view (the write is refused)
    monkeypatch.undo()
    _env(w, monkeypatch)
    st2 = S.LtsStore()
    st2.add_partition(_part6(w, "a1"))
    monkeypatch.setattr(membership, "_declared_map", lambda look, view=None: (_ for _ in ()).throw(RuntimeError("confirm stage")))
    out = membership.check(str(w.root("a1") / "x.lts"), store=st2)
    assert out["membership_partial"]["unconfirmed_count"] >= 1 and out["inside_corpus"] is not False, out
    # (4) a registry that cannot be read while a corpus has failed raises (round 11, P2-6): the write lands flagged, never a stale served-only answer
    monkeypatch.undo()
    _env(w, monkeypatch)
    st3 = S.LtsStore()
    st3.add_partition(_part6(w, "a1"))
    st3.fail("b1", "corpus-file-unreadable", "x.lts", False, "x.b1")
    monkeypatch.setattr(config, "default_corpora", lambda: (_ for _ in ()).throw(RegistryError("unreadable")))
    view = membership.snapshot(st3)
    assert view.specs_error is not None and view.unconfirmed_count == 0, "the failed read is KEPT on the view, not swallowed"
    with __import__("factlattice").rootview.use(view):
        with pytest.raises(RegistryError):
            membership.registered_roots(st3)
    with pytest.raises(RegistryError):
        membership.check(str(w.root("a1") / "x.lts"), store=st3)       # `unconfirmed_refusal` leaves this to the pinned rule: the write lands flagged



# ======================================================================================================================
# ROUND 9 (read of 2b346c6)
# ======================================================================================================================
def test_roots_null_has_one_verdict_everywhere_no_roots(tmp_path, monkeypatch):
    """F4. `roots: null` means NO roots (the text that decides it: tests/test_reviewer_nested_private.py's `null-roots` case, and `declared_root_entries`'s
    `(val or [])`). The shape check accepts it, the ledger does not count it unprovable, membership reads no root from it. The boot is the release's
    (round 11: it raised, as before; the round 9 reading that served it as no roots moved a verdict the maintainers own). Red on 2b346c6 (ledger: unprovable)."""
    from factlattice import ledger
    from factlattice_engine import config, membership
    w = _world6(tmp_path, monkeypatch)
    spec = {"roots": None, "domain_suffix": "x.n"}
    config.check_entry_shape(spec)
    with pytest.raises(TypeError):
        config.build_store({"a1": w.registry["a1"], "n": spec})       # round 11: the BOOT verdict is the release's (it raised on a null `roots`), whatever the judge says
    assert ledger._declared_roots({"n": spec}) == ((), False)
    assert membership.declared_root_paths(spec) == []


def test_a_root_object_with_no_path_is_a_shape_problem_unless_a_misspelt_key_explains_it():
    """Round 10 (2359cc4) replaces round 9's reading: a root object with no `path` and no unknown key (empty, or `area` alone) is a shape problem; a path
    missing BECAUSE its key is misspelt (`pth`) is the unknown-key rule's, not the shape check's; a written `path` must be a non-blank string."""
    from factlattice_engine import config
    config.check_entry_shape({"roots": [{"pth": "/x"}]})
    for bad in ({"roots": [{"area": "x"}]}, {"roots": [{}]}, {"roots": [{"path": None}]}, {"roots": [{"path": ""}]}):
        with pytest.raises(TypeError):
            config.check_entry_shape(bad)


@pytest.mark.parametrize("flag", ["yes", 1, 0, "true", [], {}])
def test_a_private_flag_that_is_not_a_boolean_makes_the_corpus_private_and_is_a_shape_problem(flag):
    """`private` must be a boolean: anything else is a shape problem (registry error), and wherever privacy is decided the corpus counts as PRIVATE, so a
    typo can never make it public (before: `private: "yes"` fell through to the id-suffix rule and the corpus was public). Red on 2b346c6."""
    from factlattice_engine import config
    from factlattice_engine.privacy import private_reason
    assert private_reason("plain", {"private": flag}) is not None
    with pytest.raises(TypeError):
        config.check_entry_shape({"root": "/x", "private": flag})
    assert private_reason("plain", {"private": False}) is None and private_reason("plain", {}) is None and private_reason("plain", {"private": True}) == "registry"


@pytest.mark.parametrize("shape,fails_alone", [({"roots": "nope", "privat": True}, True), ({"roots": [{"path": 7}], "privat": True}, True),
                                                ("a string", False), (["a", "list"], False), (None, False), (5, False)])
def test_an_unknown_key_beside_a_shape_problem_fails_its_corpus_alone_and_a_non_object_entry_raises(tmp_path, monkeypatch, shape, fails_alone):
    """Round 11 (third amendment): the boot is the release's. An entry with an unknown key fails ITS corpus alone with the key code, whatever else is
    wrong with it (the round 9 'shape wins' rule moved that verdict); a non-object entry raises. The write side still judges the shape: the entry is
    recorded `unlocated`. Red on fe465bc (all six raised)."""
    from factlattice_engine import config
    w = _world6(tmp_path, monkeypatch)
    reg = {"a1": w.registry["a1"], "zz-bad": shape}
    if not fails_alone:
        with pytest.raises((TypeError, AttributeError, ValueError)):
            config.build_store(reg)
        return
    st = config.build_store(reg)
    assert st.corpus_ids() == ["a1"] and st.tables()[1]["zz-bad"]["code"] == "registry-entry-unknown-key"
    assert st.unlocated() == frozenset({"zz-bad"})


def test_a_served_and_pending_corpus_whose_load_raises_stays_served_and_the_failure_is_logged(tmp_path, monkeypatch, caplog):
    """P3. The state is kept out by the seat's own check. If it is reached anyway the corpus STAYS served (failing one that is answering would take it
    away) and the failure is RECORDED: a warning with the error's class name only. The move is the checked one (`settle`). Red on 2b346c6 (silent)."""
    import logging

    from factlattice_engine import bootload, config
    w = _world6(tmp_path, monkeypatch)
    reg = {c: w.registry[c] for c in ("a1", "b1")}
    st = config.build_store({"a1": reg["a1"]})
    loader = bootload.StoreLoader(st, reg, kernel=None, retry=False, workers=1)
    loader.declare()
    with caplog.at_level(logging.WARNING, logger="factlattice_engine.bootload"):
        loader.run_blocking()
    msgs = [r.getMessage() for r in caplog.records if "already served" in r.getMessage()]
    assert msgs and "ValueError" in msgs[0] and "a1" not in msgs[0] and "would clobber" not in msgs[0], msgs
    assert sorted(st.corpus_ids()) == ["a1", "b1"] and not st.tables()[1]


# ======================================================================================================================
# ROUND 10 (read of 49bfd12)
# ======================================================================================================================
def _twenty_healthy_writes(srv, ws, label):
    """20 writes to a file in a HEALTHY corpus: (replies, latencies)."""
    outs, dts = [], []
    for i in range(20):
        out, dt = srv.call("lts_add_property", {"path": str(ws.file("a1")), "node_id": "A1-N", "key": f"{label}{i}", "value": "v"}, timeout=12)
        outs.append(out)
        dts.append(dt)
    return outs, dts


def test_a_failed_entry_naming_a_hung_path_refuses_no_healthy_write_and_costs_what_a_declared_hung_root_costs(tmp_path):
    """P1-1 (round 11), through the real harness (a MODEL of a hang: it blocks the disk calls of one path and confirms it). An entry that failed on a typo'd
    root key (`pth`) holds a path on a hung disk. Each path-like string of it is probed off the writer's thread on the process's own registry (the kernel,
    at boot), so the string goes unresponsive on its own and NO write waits on it. Measured against the CONTROL of the same hung path declared properly as a
    root of a healthy corpus: 20 writes to a healthy corpus, 0 refused, each under 2 s in both servers, and the typo'd one no slower than twice the control
    (plus 0.5 s). A write whose file lies under the string AS TYPED is refused `root-unresponsive` before anything is read. Red on fe465bc (20 of 20 healthy
    writes refused `membership-unconfirmed`)."""
    ws = Workspace(tmp_path / "w", corpora=("a1", "b1"))
    hung = tmp_path / "hung"
    hung.mkdir()
    reg = dict(ws.registry)
    reg["t1"] = {"roots": [{"pth": str(hung)}], "domain_suffix": "x.t1"}
    ws.registry_path.write_text(json.dumps(reg))
    with RealServer(ws, stall_root=hung, engaged=True, env=DEADLINES, cap_s=60) as srv:
        srv.initialize()
        srv.call("lts_find", {"corpus": "a1", "prefix": "A"}, timeout=20)             # the boot read is done
        time.sleep(1.6)                                                                 # past the root deadline: the string reads unresponsive
        outs, dts = _twenty_healthy_writes(srv, ws, "t")
        assert all(o.get("written") is True for o in outs), [o for o in outs if o.get("written") is not True][:2]
        assert max(dts) < 2.0, dts
        under, dt_u = srv.call("lts_add_property", {"path": str(hung / "x.lts"), "node_id": "X-1", "key": "k", "value": "v"}, timeout=12)
        assert under.get("refused") is True and (under.get("reason") == "root-unresponsive" or under.get("evidence", {}).get("refusal") == "root-unresponsive"), under
        assert srv.hit_total() >= 1, "the model of the hang was reached (the probe did something)"
        typo_mean = sum(dts) / len(dts)
    ws2 = Workspace(tmp_path / "w2", corpora=("a1", "b1"))
    hung2 = tmp_path / "hung2"
    hung2.mkdir()
    reg2 = dict(ws2.registry)
    reg2["t1"] = {"roots": [{"path": str(hung2)}], "domain_suffix": "x.t1"}
    ws2.registry_path.write_text(json.dumps(reg2))
    with RealServer(ws2, stall_root=hung2, engaged=True, env=DEADLINES, cap_s=60) as srv2:
        srv2.initialize()
        srv2.call("lts_find", {"corpus": "a1", "prefix": "A"}, timeout=20)
        time.sleep(1.6)
        outs2, dts2 = _twenty_healthy_writes(srv2, ws2, "c")
        assert all(o.get("written") is True for o in outs2), "control: a hung DECLARED root refuses no healthy write either"
        assert typo_mean <= 2 * (sum(dts2) / len(dts2)) + 0.5, (typo_mean, sum(dts2) / len(dts2))


def test_an_untrusted_string_is_a_candidate_by_its_text_or_its_kept_real_path_and_never_touches_the_disk(tmp_path, monkeypatch):
    """P1-1 / task 3, the unit. The guard reads facts only: (1) a file lexically under a string of a failed entry is a candidate; (2) a file under the
    REAL path the probe kept for a string that is a link to it is a candidate; (3) neither: not a candidate; a candidate raises (the registry stage: the
    write lands flagged, never refused); (4) NO `os.path.realpath` and no `confirm_real` is called by the guard."""
    import os as _os

    from factlattice import rootview
    from factlattice_engine import boundedfs, config, membership
    from factlattice_engine import store as S
    w = _world6(tmp_path, monkeypatch)
    bad = tmp_path / "bad"
    bad.mkdir()
    f = bad / "x.lts"
    f.write_text("x", encoding="utf-8")
    link = tmp_path / "lnk"
    link.symlink_to(bad)
    reg = {"a1": w.registry["a1"]}

    def world(spec):
        st2 = S.LtsStore()
        st2.add_partition(_part6(w, "a1"))
        st2.fail("t1", "registry-root-unknown-key", None, False, "x.t1")
        monkeypatch.setattr(config, "default_corpora", lambda: {**reg, "t1": spec})
        return st2
    calls = {"confirm": 0}
    monkeypatch.setattr(boundedfs, "confirm_real", lambda *a, **k: calls.__setitem__("confirm", calls["confirm"] + 1))
    real_file = _os.path.realpath(str(f))
    guard = membership._untrusted_entry_guard
    # (1) by text
    s1 = world({"roots": [{"pth": str(bad)}]})
    with pytest.raises(RuntimeError):
        guard(real_file, membership.snapshot(s1), s1)
    # (2) by the kept real path: the string is a link, the view carries what its probe kept
    s2 = world({"roots": [{"pth": str(link)}]})
    view = membership.snapshot(s2)
    kept = [x for x in view.facts if x.untrusted]
    assert kept and kept[0].real == _os.path.realpath(str(bad)), "the bounded resolve (no kernel) kept the real path"
    with pytest.raises(RuntimeError):
        guard(real_file, view, s2)
    # (3) not a candidate
    s3 = world({"roots": [{"pth": str(tmp_path / "elsewhere")}]})
    guard(real_file, membership.snapshot(s3), s3)
    assert calls["confirm"] == 0
    # (4) no disk call in the guard itself (a view built by hand)
    monkeypatch.setattr(_os.path, "realpath", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the guard touched the disk")))
    v = rootview.RootView([rootview.RootFact(str(link), str(bad), "ok", True)])
    v.look, v.look_error = ([], {}, {"t1": {}}, {}), None
    v.specs = {"t1": {"roots": [{"pth": str(link)}]}}
    with pytest.raises(RuntimeError):
        guard(real_file, v, S.LtsStore())


def test_a_pending_or_unresponsive_untrusted_string_counts_as_marked_and_refuses_only_a_write_under_it_as_typed():
    """Task 3. A fact for a string of a failed entry that is not answering counts in `marked_count`; `marked_holding` (so `marked_refusal`) matches it by
    its declared text only. The real path it last answered is NOT a reason to refuse (the write is a candidate, flagged by membership). A declared root
    in the same state refuses by both (control)."""
    from factlattice.rootview import RootFact, RootView
    from factlattice_engine import authoring
    u = RootView([RootFact("/zz/typed", "/zz/real", "unresponsive", True)])
    assert u.marked_count == 1
    assert authoring.marked_refusal(u, "/zz/typed/f.lts") is not None, "as typed: refused"
    assert authoring.marked_refusal(u, "/zz/real/f.lts") is None, "under the kept real path only: a candidate, not refused"
    d = RootView([RootFact("/zz/typed", "/zz/real", "unresponsive")])
    assert authoring.marked_refusal(d, "/zz/typed/f.lts") is not None and authoring.marked_refusal(d, "/zz/real/f.lts") is not None


def test_a_marked_root_refuses_before_anything_is_read_whatever_the_store_look_did():
    """P1-2. `marked_refusal` comes first: a file under a root marked from memory is refused `root-unresponsive` even when the look at the store failed
    (`membership-unconfirmed` is decided after it, for a root that is merely pending or a look that failed with nothing marked). Red on fe465bc (the failed
    look returned None and the write went on to read the store)."""
    from factlattice.rootview import RootFact, RootView
    from factlattice_engine import authoring
    v = RootView([RootFact("/nowhere/root", "/nowhere/root", "unresponsive")])
    v.look, v.look_error = None, RuntimeError("no look")
    v.note_unconfirmed("<the look at the store failed>")
    out = authoring.marked_refusal(v, "/nowhere/root/f.lts")
    assert out is not None and out.get("evidence", {}).get("refusal") == "root-unresponsive", out
    v2 = RootView([])
    v2.look, v2.look_error = None, RuntimeError("no look")
    assert authoring.marked_refusal(v2, "/nowhere/root/f.lts") is None, "control: nothing marked, so the failed look is `membership-unconfirmed`'s"
    v3 = RootView([RootFact("/nowhere/root", "/nowhere/root", "pending")])
    v3.look, v3.look_error = None, RuntimeError("no look")
    assert authoring.marked_refusal(v3, "/nowhere/root/f.lts") is None, "control: a root merely PENDING with a failed look is `membership-unconfirmed`'s (P2-1)"


def test_a_hung_declared_root_and_a_failed_store_look_together_refuse_the_hung_file_at_once_and_the_rest_membership_unconfirmed(tmp_path):
    """P1-2, both faults STAGED together over a real server (a MODEL of the hang, plus a store whose look raises). The root of one corpus does not answer,
    and every look at the store fails. A write to a file under the hung root is refused `root-unresponsive` (from memory, before anything is read; the
    look never gets a say), a write to a healthy file is refused `membership-unconfirmed`, and neither waits on the disk. Red on fe465bc (the failed look
    returned first: the hung file was refused `membership-unconfirmed`, and the second fault was never decided from memory)."""
    ws = Workspace(tmp_path / "w", corpora=("a1", "b1"))
    hung = ws.root("b1")
    launcher = ("import runpy, factlattice_engine.membership as m\nreal = m._look\n"
                "def bad(store):\n    if store is None:\n        return real(store)\n    raise RuntimeError('SECRET-EXCEPTION-TEXT')\n"
                "m._look = bad\nrunpy.run_module('factlattice_engine.server', run_name='__main__')\n")
    srv = RealServer(ws, stall_root=hung, engaged=True, env=DEADLINES, cap_s=60)
    srv.cmd = [sys.executable, "-c", launcher]
    with srv:
        srv.initialize()
        time.sleep(2.2)                                                                 # past the root deadline: the hung root is marked
        out, dt = srv.call("lts_add_property", {"path": str(ws.file("b1")), "node_id": "B1-N", "key": "k", "value": "v"}, timeout=12)
        assert dt < 3.0, dt
        assert out.get("refused") is True and out.get("evidence", {}).get("refusal") == "root-unresponsive", out
        ok, dt2 = srv.call("lts_add_property", {"path": str(ws.file("a1")), "node_id": "A1-N", "key": "k", "value": "v"}, timeout=12)
        assert dt2 < 3.0, dt2
        assert ok.get("refused") is True and ok.get("evidence", {}).get("refusal") == "membership-unconfirmed" and "SECRET" not in json.dumps(ok), ok


def test_a_failed_look_right_after_initialize_is_membership_unconfirmed_over_a_server(tmp_path):
    """P2-1 over a real stdio server, the write sent straight after `initialize` (no find first, so the boot read may still be going)."""
    import subprocess
    w = Workspace(tmp_path / "w", corpora=("a1",))
    launcher = ("import runpy, factlattice_engine.membership as m\nreal = m._look\n"
                "def bad(store):\n    if store is None:\n        return real(store)\n    raise RuntimeError('SECRET-EXCEPTION-TEXT')\n"
                "m._look = bad\nrunpy.run_module('factlattice_engine.server', run_name='__main__')\n")
    env = dict(os.environ)
    env.update(w.env({"PYTHONPATH": str(Path(__file__).resolve().parent.parent), "FACTLATTICE_CODE_WATCH": "0"}))
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "lts_add_property", "arguments": {
                "path": str(w.file("a1")), "node_id": "A1-N", "key": "k", "value": "v"}}}]
    p = subprocess.run([sys.executable, "-c", launcher], input="\n".join(json.dumps(r) for r in reqs) + "\n", capture_output=True, text=True,
                       cwd=str(tmp_path), env=env, timeout=120)
    by = {o["id"]: o for o in (json.loads(l) for l in p.stdout.splitlines() if l.strip()) if "id" in o}
    out = json.loads(by[2]["result"]["content"][0]["text"])
    assert out.get("refused") is True and out.get("reason") == "membership-unconfirmed" and "SECRET" not in json.dumps(out), out


@pytest.mark.parametrize("spec", [{"roots": "x"}, {"roots": [7]}, {"roots": 5}, {"root": 5}, {"roots": [{"path": 7}]}, {"profile": 5, "root": "/x"},
                                  {"external_id_prefixes": 5, "root": "/x"}, {"roots": [""]}, {"roots": [{"path": "   "}]}, {"roots": [{}]}, {"root": " "},
                                  "a string", ["a"]])
def test_register_corpus_judges_the_entry_with_the_one_shape_judge_and_refuses_with_a_code(tmp_path, monkeypatch, spec):
    """P2-2. `lts_register_corpus` runs `check_entry_shape` before anything reads the entry: a malformed one is refused `corpus-entry-shape`, the
    class names only, never a raw ValueError / TypeError text, and nothing is registered. Red on 49bfd12 (a raw error text, or a corpus served)."""
    from factlattice_engine import store as S
    from factlattice_engine import tools as T
    from factlattice_engine.tools import LtsEngine
    _world6(tmp_path, monkeypatch)
    st = S.LtsStore()
    eng = LtsEngine(st)
    out = T.dispatch("lts_register_corpus", {"corpus_id": "zz", "spec": spec}, tools=eng)
    assert out.get("success") is False and out.get("reason") == "corpus-entry-shape" and out.get("error_class") == "CorpusEntryShape", out
    assert "dictionary update" not in json.dumps(out) and "Traceback" not in json.dumps(out)
    assert st.corpus_ids() == []


@pytest.mark.parametrize("spec,boot_raises", [({"roots": [""]}, True), ({"roots": [{"path": ""}]}, False), ({"roots": [{"path": " \t"}]}, False), ({"roots": [{}]}, True),
                                              ({"roots": [{"area": "specs"}]}, True), ({"root": ""}, False), ({"root": "  "}, False)])
def test_blank_paths_and_a_root_with_no_path_and_no_unknown_key_are_shape_problems_for_the_judge_and_the_readers(tmp_path, monkeypatch, spec, boot_raises):
    """P2-4, round 11. The shape judge, membership's readers and the ledger agree that these are shape problems. The BOOT is the release's: a blank
    root or path served nothing and the server went on (round 10's rule made a blank root a server-wide outage); an empty root object or a bare string
    raised, and still does. Red on fe465bc for the blank ones (it raised)."""
    from factlattice import ledger
    from factlattice_engine import config, membership
    w = _world6(tmp_path, monkeypatch)
    with pytest.raises(TypeError):
        config.check_entry_shape(spec)
    if boot_raises:
        with pytest.raises((TypeError, KeyError, AttributeError, ValueError)):
            config.build_store({"a1": w.registry["a1"], "zz": spec})
    else:
        st = config.build_store({"a1": w.registry["a1"], "zz": spec})
        assert "a1" in st.corpus_ids() and "zz" not in st.corpus_ids() and st.unlocated() == frozenset({"zz"})
    with pytest.raises(TypeError):
        membership.declared_root_paths(spec)
    assert ledger._declared_roots({"c": spec})[1] is True


def test_a_path_missing_because_its_key_is_misspelt_keeps_the_unknown_key_rule_everywhere(tmp_path, monkeypatch):
    """P2-4, the other side. `{"pth": ...}` is not a shape problem: the shape judge, the membership readers and the ledger accept it (the ledger marks it
    unprovable by the unknown-key problem), and the loader fails ONLY that corpus with the key code."""
    from factlattice import ledger
    from factlattice_engine import config, membership
    w = _world6(tmp_path, monkeypatch)
    spec = {"roots": [{"pth": str(w.root("b1"))}], "domain_suffix": "x.b1"}
    config.check_entry_shape(spec)
    membership.declared_root_paths(spec)
    assert ledger._declared_roots({"c": spec})[1] is True
    st = config.build_store({"a1": w.registry["a1"], "b1": spec})
    assert st.corpus_ids() == ["a1"] and st.tables()[1]["b1"]["code"] == "registry-root-unknown-key"


def test_the_unknown_key_write_guard_and_the_shape_rules_hold_on_every_write_route(tmp_path, monkeypatch):
    """P2-5. A write under a typo'd entry's folder lands flagged and attributed to none on THREE routes: the server (a store with a kernel; see the stdio
    test in test_registry_round4), the call CLI (`python -m factlattice_engine call ... --allow-write`), and a library writer with `store=None`. Red on 49bfd12
    for `store=None` (it read 'outside every registered corpus root')."""
    import subprocess

    from factlattice_engine import authoring
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "b.lts").write_text("# D [B-1]\n\n## N [B-2]\n\n- status: active\n", encoding="utf-8")
    ok = tmp_path / "ok"
    ok.mkdir()
    (ok / "a.lts").write_text("# D [A-1]\n\n## N [A-2]\n\n- status: active\n", encoding="utf-8")
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps({"okc": {"root": str(ok), "domain_suffix": "x.ok"}, "badc": {"roots": [{"pth": str(bad)}], "domain_suffix": "x.bad"}}))
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(reg))
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    # library, no store
    lib = authoring.add_property(str(bad / "b.lts"), "B-2", "k", "v", store=None)
    assert lib["root_membership"]["corpora"] == [] and lib["root_membership"].get("registry_error") and "OUTSIDE EVERY" not in json.dumps(lib), lib["root_membership"]
    fine = authoring.add_property(str(ok / "a.lts"), "A-2", "k", "v", store=None)
    assert fine["root_membership"]["corpora"] == ["okc"] and not fine["root_membership"].get("registry_error")
    # the call CLI
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    args = json.dumps({"path": str(bad / "b.lts"), "node_id": "B-2", "key": "k2", "value": "v"})
    p = subprocess.run([sys.executable, "-m", "factlattice_engine", "call", "lts_add_property", "--args-json", args, "--allow-write", "--json"],
                       capture_output=True, text=True, cwd=str(tmp_path), env=env, timeout=120)
    body = p.stdout[p.stdout.index("{"):] if "{" in p.stdout else p.stdout
    assert "OUTSIDE EVERY REGISTERED" not in p.stdout and "NOT LEDGERED" not in p.stdout, p.stdout[-600:]
    assert "registry_error" in p.stdout or "corpora_error" in p.stdout or '"corpora": []' in p.stdout, p.stdout[-600:]


def test_register_corpus_from_the_registry_file_judges_the_entry_too(tmp_path, monkeypatch):
    """P2-2, the route with no `spec`: the entry is read from the registry file (which reads fine: the shape is not what the registry reader checks) and the
    same judge refuses it `corpus-entry-shape`. Red on 49bfd12 (the corpus was registered and served)."""
    from factlattice_engine import store as S
    from factlattice_engine import tools as T
    from factlattice_engine.tools import LtsEngine
    w = _world6(tmp_path, monkeypatch)
    reg = json.loads(w.registry_path.read_text())
    reg["zz"] = {"root": str(w.root("b1")), "domain_suffix": "x.zz", "profile": 5}
    w.registry_path.write_text(json.dumps(reg))
    st = S.LtsStore()
    out = T.dispatch("lts_register_corpus", {"corpus_id": "zz"}, tools=LtsEngine(st))
    assert out.get("success") is False and out.get("reason") == "corpus-entry-shape", out
    assert st.corpus_ids() == []


# ======================================================================================================================
# ROUND 11 (read of fe465bc; the third amendment of specs/38 x-load-error: no entry shape's boot verdict changes from release af4cfc2)
# ======================================================================================================================
def test_corpus_entry_shape_is_a_stable_code_and_the_register_reply_uses_the_constant():
    """P2-2. The code lives in `rootstate.STABLE_CODES` (six codes now; tests/test_bounded_roots_probes.py pins the tuple), is a code by the same test as the
    others, and `tools` carries no second spelling of it. Red on fe465bc (the string was a literal in tools.py and not in the tuple)."""
    from factlattice_engine import rootstate as RS
    from factlattice_engine import tools as T
    assert RS.CODE_CORPUS_ENTRY_SHAPE == "corpus-entry-shape" and RS.CODE_CORPUS_ENTRY_SHAPE in RS.STABLE_CODES
    src = Path(T.__file__).read_text(encoding="utf-8")
    assert '"corpus-entry-shape"' not in src, "tools.py names the constant, not the string"


@pytest.mark.parametrize("spec", [{"roots": ["/secret/zz-path"]}, {"roots": ["/secret/zz-path", {"path": "/secret/other"}]}])
def test_register_a_string_inside_roots_gets_the_stable_code_not_a_raw_error(tmp_path, monkeypatch, spec):
    """P2-2. A bare string inside `roots` is accepted by the judge and the build cannot read it (a ValueError). The reply carries the stable code and the
    exception CLASS, never its text (which would carry the path), never a traceback, and nothing is registered. Red on fe465bc (a raised ValueError)."""
    from factlattice_engine import store as S
    from factlattice_engine import tools as T
    from factlattice_engine.tools import LtsEngine
    _world6(tmp_path, monkeypatch)
    st = S.LtsStore()
    out = T.dispatch("lts_register_corpus", {"corpus_id": "zz", "spec": spec}, tools=LtsEngine(st))
    assert out.get("success") is False and out.get("reason") == "corpus-entry-shape" and out.get("error_class") == "CorpusEntryShape", out
    text = json.dumps(out)
    assert "/secret" not in text and "Traceback" not in text and "ValueError" in text, text
    assert st.corpus_ids() == []


def test_a_string_inside_roots_at_boot_is_a_registry_error_with_a_class_only_reply(tmp_path):
    """P2-2, the boot side (the release's verdict: build_store raised). The server's reply names the exception class and never the message or the path."""
    ws = Workspace(tmp_path / "w", corpora=("a1",))
    reg = dict(ws.registry)
    reg["zz"] = {"roots": ["/secret/zz-path"], "domain_suffix": "x.zz"}
    ws.registry_path.write_text(json.dumps(reg))
    with RealServer(ws, env=DEADLINES) as srv:
        srv.initialize()
        out, _dt = srv.call("lts_find", {"corpus": "a1", "prefix": "A"}, timeout=20)
    text = json.dumps(out)
    assert out.get("reason") == "registry-not-found" and "/secret" not in text and "Traceback" not in text and "ValueError" in text, out


def test_register_from_the_registry_file_judges_a_blank_root_as_written(tmp_path, monkeypatch):
    """P3-2. `expand_entry` turns a blank root (" ") into an absolute path before the judge's second check, so the reply was the generic 'resolved to zero
    nodes' with no code. The judge now also reads the entry AS WRITTEN: the coded refusal. Red on fe465bc."""
    from factlattice_engine import store as S
    from factlattice_engine import tools as T
    from factlattice_engine.tools import LtsEngine
    w = _world6(tmp_path, monkeypatch)
    reg = json.loads(w.registry_path.read_text())
    reg["zz"] = {"root": " ", "domain_suffix": "x.zz"}
    w.registry_path.write_text(json.dumps(reg))
    st = S.LtsStore()
    out = T.dispatch("lts_register_corpus", {"corpus_id": "zz"}, tools=LtsEngine(st))
    assert out.get("success") is False and out.get("reason") == "corpus-entry-shape" and out.get("error_class") == "CorpusEntryShape", out
    assert st.corpus_ids() == []


def test_registering_a_fixed_entry_from_the_registry_clears_it_from_unlocated(tmp_path, monkeypatch):
    """The store's record of an entry the registry cannot place ends when the registry places it: the register that reads it from the file and judges it
    clean takes the corpus out of `unlocated`, so writes stop landing flagged because of it."""
    from factlattice_engine import config
    from factlattice_engine import tools as T
    from factlattice_engine.tools import LtsEngine
    w = _world6(tmp_path, monkeypatch)
    bad = {"roots": [{"path": 7}], "domain_suffix": "x.zz"}          # booted as no-roots, as the release did; the judge refuses it
    reg = json.loads(w.registry_path.read_text())
    reg["zz"] = bad
    w.registry_path.write_text(json.dumps(reg))
    st = config.build_store()
    assert "zz" in st.unlocated()
    reg["zz"] = {"root": str(w.root("b1")), "domain_suffix": "x.zz"}
    w.registry_path.write_text(json.dumps(reg))
    out = T.dispatch("lts_register_corpus", {"corpus_id": "zz"}, tools=LtsEngine(st))
    assert out.get("success") is True, out
    assert "zz" not in st.unlocated()


def _route_world(tmp_path, monkeypatch, odd_entry, **extra):
    ok = tmp_path / "ok"
    ok.mkdir()
    (ok / "a.lts").write_text("# D [A-1]\n\n## N [A-2]\n\n- status: active\n", encoding="utf-8")
    odd = tmp_path / "odd"
    odd.mkdir()
    (odd / "o.lts").write_text("# D [O-1]\n\n## N [O-2]\n\n- status: active\n", encoding="utf-8")
    entry = json.loads(json.dumps(odd_entry).replace("{ODD}", str(odd)))
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps({"okc": {"root": str(ok), "domain_suffix": "x.ok"}, "oddc": entry}))
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(reg))
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    return ok, odd


def _cli_write(tmp_path, path, node, key):
    import subprocess
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    args = json.dumps({"path": str(path), "node_id": node, "key": key, "value": "v"})
    return subprocess.run([sys.executable, "-m", "factlattice_engine", "call", "lts_add_property", "--args-json", args, "--allow-write", "--json"],
                          capture_output=True, text=True, cwd=str(tmp_path), env=env, timeout=120).stdout


def test_a_write_is_flagged_on_every_route_while_an_unplaceable_entry_stands(tmp_path, monkeypatch):
    """Task 2 / (1). `roots: [{path: 7}]` is an entry the release does not serve and whose roots cannot be read as paths. While it stands a write is flagged
    (`corpora_error`, `corpora` null) and attributed to nothing, never refused and never 'outside every root', on THREE routes: the library writer with
    `store=None`, a store from the loader, and the call CLI. Red on 90737fb for the CLI (an entry the judge refused was flagged; this one only if boot
    raised)."""
    from factlattice_engine import authoring, config
    ok, _odd = _route_world(tmp_path, monkeypatch, {"roots": [{"path": 7}], "domain_suffix": "x.odd"})
    lib = authoring.add_property(str(ok / "a.lts"), "A-2", "k", "v", store=None)
    assert lib["root_membership"]["corpora"] == [] and lib["root_membership"].get("registry_error") and "OUTSIDE EVERY" not in json.dumps(lib), lib["root_membership"]
    st = config.build_store()
    assert "okc" in st.corpus_ids() and st.unlocated() == frozenset({"oddc"})
    held = authoring.add_property(str(ok / "a.lts"), "A-2", "k2", "v", store=st)
    assert held["written"] is True and held["root_membership"]["corpora"] == [] and held["root_membership"].get("registry_error") and "OUTSIDE EVERY" not in json.dumps(held)
    out = _cli_write(tmp_path, ok / "a.lts", "A-2", "k3")
    assert "OUTSIDE EVERY REGISTERED" not in out and "NOT LEDGERED" not in out and ("registry_error" in out or "corpora_error" in out), out[-600:]


@pytest.mark.parametrize("field", [{"private": 1}, {"private": "yes"}, {"domain_suffix": 5}, {"profile": 5}, {"external_id_prefixes": 5},
                                   {"root": None, "roots": [{"path": 7}, {"path": "{ODD}"}]}])
def test_an_entry_the_release_serves_is_never_unlocated_and_writes_attribute_on_every_route(tmp_path, monkeypatch, field):
    """(1), the other side. `private: 1` and `domain_suffix: 5` still have readable roots and the release serves them, so they are never recorded as
    unlocated: a write under a healthy corpus and one under the entry's own folder attribute to their corpora, on the library route (`store=None`), a
    store from the loader and the call CLI. Red on 90737fb (one such field flagged every write in the process)."""
    from factlattice_engine import authoring, config
    entry = {"root": "{ODD}", **({"domain_suffix": "x.odd"} if "domain_suffix" not in field else {}), **field}
    entry = {k: v for k, v in entry.items() if v is not None}          # `{path: 7}` beside a readable root: the release serves it (the int is a missing root)
    ok, odd = _route_world(tmp_path, monkeypatch, entry)
    st = config.build_store()
    assert "oddc" in st.corpus_ids() and st.unlocated() == frozenset()
    for label, store in (("library", None), ("store", st)):
        a = authoring.add_property(str(ok / "a.lts"), "A-2", "k" + label, "v", store=store)
        o = authoring.add_property(str(odd / "o.lts"), "O-2", "k" + label, "v", store=store)
        assert a["root_membership"]["corpora"] == ["okc"] and not a["root_membership"].get("registry_error"), (label, a["root_membership"])
        assert o["root_membership"]["corpora"] == ["oddc"] and not o["root_membership"].get("registry_error"), (label, o["root_membership"])
    out = _cli_write(tmp_path, odd / "o.lts", "O-2", "kcli")
    assert '"corpora": [\n' in out or '"oddc"' in out, out[-500:]
    assert "registry_error" not in out and "corpora_error" not in out, out[-500:]


def test_a_registry_read_that_fails_after_the_view_was_built_is_the_registry_stage_and_is_read_once(tmp_path, monkeypatch):
    """P2-6. `snapshot` used to swallow a failing registry read and a later reader read the registry AGAIN, so a registry that flipped between the two got two
    answers in one write. The failed read is kept on the view (`specs_error`) and is the answer of every reader for the whole write: one read, and the
    write lands flagged (the pinned registry rule). The mirror: a read that succeeded is not repeated. Red on fe465bc (two reads; a stale answer)."""
    from factlattice.registry import RegistryError
    from factlattice_engine import config, membership
    from factlattice_engine import store as S
    w = _world6(tmp_path, monkeypatch)
    st = S.LtsStore()
    st.add_partition(_part6(w, "a1"))
    st.fail("b1", "corpus-file-unreadable", "x.lts", False, "x.b1")
    calls = []
    answers = [RegistryError("flap-1"), {"a1": w.registry["a1"], "b1": w.registry["b1"]}]

    def flipping():
        calls.append(1)
        a = answers[min(len(calls) - 1, 1)]
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(config, "default_corpora", flipping)
    f = str(w.root("a1") / "x.lts")
    with pytest.raises(RegistryError):
        membership.check(f, store=st)
    assert len(calls) == 1, "the registry was read once for the whole write"
    # the mirror: a good read first, a failure second: the first answer stands, no second read
    calls.clear()
    answers[:] = [{"a1": w.registry["a1"], "b1": w.registry["b1"]}, RegistryError("flap-2")]
    out = membership.check(f, store=st)
    assert len(calls) == 1 and out["corpora"] == ["a1"], (calls, out)


def test_the_kernel_probes_an_untrusted_string_in_the_background_and_keeps_its_real_path(tmp_path):
    """Task 3. `kernel.declare_untrusted` gives each string a slot and ONE background probe on the process registry; the call returns at once (memory only), the
    probe keeps the real path the string resolves to, and `root_facts` says the fact is untrusted. A path that a corpus DECLARED is not untrusted even when
    the same string is named (a root some corpus declared is a root). A probe on a hung string goes unresponsive by the registry's own rule (staged in
    `test_a_failed_entry_naming_a_hung_path_refuses_no_healthy_write_...`)."""
    from factlattice.rootview import lexical
    from factlattice_engine.kernel import FreshnessKernel
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "lnk"
    link.symlink_to(real_dir)
    k = FreshnessKernel(root_deadline_s=5)
    t0 = time.monotonic()
    k.declare_untrusted([str(link), str(link)])
    assert time.monotonic() - t0 < 0.5, "memory only: the call does not wait for the probe"
    end = time.monotonic() + 10
    got = []
    while time.monotonic() < end:
        got = [f for f in k.root_facts() if f.declared == lexical(str(link))]
        if got and got[0].real:
            break
        time.sleep(0.02)
    assert len(got) == 1 and got[0].untrusted and got[0].state == "ok" and got[0].real == os.path.realpath(str(real_dir)), got
    assert k.untrusted_paths() == {lexical(str(link))}
    k.declare_roots([str(link)])
    assert [f.untrusted for f in k.root_facts() if f.declared == lexical(str(link))] == [False], "a declared root is a root"


def test_the_reviewer_does_not_turn_a_malformed_entry_into_no_roots():
    """P3-5. `_declared_roots` raised nothing before fe465bc and returned [] after it, which `corpora_containing` read as 'contains nothing'. It raises again
    (class only), and the container search lists an entry whose shape states no location as a container (it may hold the document: fail safe), where
    a well-formed entry elsewhere is not. Red on fe465bc (`[]`)."""
    from factlattice_engine import reviewer
    with pytest.raises(TypeError):
        reviewer._declared_roots({"roots": 5})
    assert reviewer._declared_roots_or_none({"roots": 5}) is None
    got = reviewer.corpora_containing("/anywhere/else/f.lts", {"m": {"roots": 5}, "g": {"root": "/some/other/place"}})
    assert got == ["m"], got


def test_a_stale_untrusted_probe_marks_no_write(tmp_path, monkeypatch):
    """Task 3. A probe the kernel keeps for a string of an entry that has since been FIXED marks nothing: `snapshot` carries only the untrusted facts the
    registry still names (here the registry names none). Without that, a pending probe left from an old typo would count in `marked_count` for ever."""
    from factlattice.rootview import RootFact
    from factlattice_engine import config, membership
    from factlattice_engine import store as S
    w = _world6(tmp_path, monkeypatch)
    st = S.LtsStore()
    st.add_partition(_part6(w, "a1"))

    class K:
        roots = None
        def root_facts(self):
            return [RootFact("/zz/stale-string", None, "pending", True), RootFact(str(w.root("a1")), str(w.root("a1")), "ok", False)]
    st.kernel = K()
    monkeypatch.setattr(config, "default_corpora", lambda: {"a1": w.registry["a1"]})
    view = membership.snapshot(st)
    assert view.marked_count == 0 and not any(f.untrusted for f in view.facts), view.facts


def test_the_kernel_keeps_probing_an_untrusted_string_each_tick(tmp_path):
    """Task 3. The watcher's tick asks the untrusted strings again, so a link swapped under a string is seen by the next poll (the kept real path is never
    older than the last answer), exactly as for a declared root."""
    from factlattice.rootview import lexical
    from factlattice_engine.kernel import FreshnessKernel
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    link = tmp_path / "lnk"
    link.symlink_to(one)
    k = FreshnessKernel(root_deadline_s=5)
    k.declare_untrusted([str(link)])

    def real():
        return [f.real for f in k.root_facts() if f.declared == lexical(str(link))]
    end = time.monotonic() + 10
    while time.monotonic() < end and real() != [os.path.realpath(str(one))]:
        time.sleep(0.02)
    assert real() == [os.path.realpath(str(one))]
    link.unlink()
    link.symlink_to(two)
    k.tick()
    assert real() == [os.path.realpath(str(two))], real()


def test_an_entry_with_no_readable_location_flags_every_write(tmp_path, monkeypatch):
    """Task 3. A failed entry holding a `${...}` string (a reference nobody expanded) or a NUL names no place at all: it may hold any file, so EVERY write
    is flagged and unattributed (the guard raises), and a write to a clean entry beside it is flagged too, never attributed."""
    import os as _os

    from factlattice_engine import config, membership
    from factlattice_engine import store as S
    w = _world6(tmp_path, monkeypatch)
    f = tmp_path / "anywhere.lts"
    f.write_text("x", encoding="utf-8")
    for odd in ("${missing}/corpus", "/x/\x00/y"):
        st = S.LtsStore()
        st.add_partition(_part6(w, "a1"))
        st.fail("t1", "registry-root-unknown-key", None, False, "x.t1")
        monkeypatch.setattr(config, "default_corpora", lambda odd=odd: {"a1": w.registry["a1"], "t1": {"roots": [{"pth": odd}]}})
        with pytest.raises(RuntimeError):
            membership._untrusted_entry_guard(_os.path.realpath(str(f)), membership.snapshot(st), st)


def test_the_guard_matches_the_text_of_a_string_that_has_no_kept_real_path_and_the_typed_spelling(tmp_path, monkeypatch):
    """Task 3, the other legs of the candidate rule, on a hand-built view. (a) A string that has not answered (pending: no kept real path) still makes a file
    lexically under it a candidate. (b) A file whose REAL path is elsewhere but whose path AS TYPED lies under the string is a candidate. (c) A file under
    neither is not. The guard touches no disk."""
    from factlattice import rootview
    from factlattice_engine import config, membership
    from factlattice_engine import store as S
    w = _world6(tmp_path, monkeypatch)
    st = S.LtsStore()
    st.add_partition(_part6(w, "a1"))
    st.fail("t1", "registry-root-unknown-key", None, False, "x.t1")
    string = "/zz-no-such-drive/corpus"
    monkeypatch.setattr(config, "default_corpora", lambda: {"a1": w.registry["a1"], "t1": {"roots": [{"pth": string}]}})
    look = ([], {}, {"t1": {}}, {})

    def view(real_for_string=None, state="pending", typed=None):
        v = rootview.RootView([rootview.RootFact(string, real_for_string, state, True)])
        v.look, v.look_error = look, None
        v.typed = typed
        return v
    with pytest.raises(RuntimeError):
        membership._untrusted_entry_guard(string + "/sub/f.lts", view(), st)                           # (a) by text, no kept real path
    with pytest.raises(RuntimeError):
        membership._untrusted_entry_guard("/elsewhere/resolved.lts", view(typed=string + "/lnk/f.lts"), st)      # (b) by the typed spelling
    membership._untrusted_entry_guard("/elsewhere/resolved.lts", view(typed="/elsewhere/f.lts"), st)          # (c) neither


def test_a_registry_that_fails_to_read_at_the_confirm_stage_is_the_registry_stage_not_a_stale_answer(tmp_path, monkeypatch):
    """P2-6, the confirm stage. With a PENDING corpus and nothing failed, only the confirm stage reads the registry's entries. A registry that cannot be read
    there is the write's registry-stage result (it raises, so the write lands flagged), where it used to leave the served corpora's roots, and a read that
    failed is not read a second time."""
    from factlattice.registry import RegistryError
    from factlattice_engine import config, membership
    from factlattice_engine import store as S
    w = _world6(tmp_path, monkeypatch)
    st = S.LtsStore()
    st.add_partition(_part6(w, "a1"))
    st.declare_pending("b1", _pending_rec(w.registry["b1"]))
    reads = []
    monkeypatch.setattr(config, "default_corpora", lambda: (reads.append(1), (_ for _ in ()).throw(RegistryError("unreadable")))[1])
    f = str(w.root("a1") / "x.lts")
    view = membership.snapshot(st)
    view.typed = f
    with __import__("factlattice").rootview.use(view):
        with pytest.raises(RegistryError):
            membership.check(f, store=st)
        with pytest.raises(RegistryError):
            membership.check(f, store=st)                  # the same write asks again: the same answer, from the view
    assert len(reads) == 1, reads


@pytest.mark.parametrize("text,expected", [("/abs/path", True), ("  /abs/path  ", True), ("~/x", True), ("~", True), ("a/b", True), ("./x", True), ("../x", True),
                                           ("file:///x", True), ("path=/x", True), ("bad", False), (".", False), ("..", False), ("  bad ", False), ("", False), ("   ", False), ("${name}", False), (5, False), (None, False)])
def test_the_path_like_rule_is_pinned(text, expected):
    """(1) / SDLC [ENTRY-SHAPE-ONE-JUDGE]: after stripping, a string is path-like when it starts with `/` or `~` or holds a `/`. A bare word (`bad`, `.`, `..`)
    is not: it names no place without a working directory. One function (`config.is_path_like`) decides it for the probes, the guard and `unstated_location`."""
    from factlattice_engine import config
    assert config.is_path_like(text) is expected


@pytest.mark.parametrize("entry", [{"roots": [{"pth": "bad"}]}, {"roots": [{"pth": "."}]}, {"root": "{ODD}", "rooot": "bad"}, {"roots": [{"path": "{ODD}", "pth2": ["a", "b"]}]}])
def test_a_bare_word_in_a_key_failed_entry_is_unlocated_and_every_write_is_flagged_not_outside_every_root(tmp_path, monkeypatch, entry):
    """P2-2. A typo'd key holding a bare relative word (`pth: "bad"`) names a place the engine cannot state: the corpus fails on the key AND is unlocated, so a
    write anywhere lands flagged (`corpora_error`, `corpora` null) and none is called 'outside every root' (the fe465bc behaviour, which 90737fb lost).
    Control: the same typo with an ABSOLUTE path is not unlocated (its strings are probed instead), and an unrelated write is then attributed normally."""
    from factlattice_engine import authoring, config
    ok, odd = _route_world(tmp_path, monkeypatch, {**entry, "domain_suffix": "x.odd"})
    st = config.build_store()
    assert st.tables()[1]["oddc"]["code"] in ("registry-root-unknown-key", "registry-entry-unknown-key") and st.unlocated() == frozenset({"oddc"}), st.unlocated()
    for store in (None, st):
        w = authoring.add_property(str(ok / "a.lts"), "A-2", "k" + str(store is None), "v", store=store)
        assert w["written"] is True and w["root_membership"]["corpora"] == [] and w["root_membership"].get("registry_error") and "OUTSIDE EVERY" not in json.dumps(w), w["root_membership"]
    out = _cli_write(tmp_path, ok / "a.lts", "A-2", "kcli")
    assert "OUTSIDE EVERY REGISTERED" not in out and ("registry_error" in out or "corpora_error" in out), out[-500:]


def test_a_key_failed_entry_with_an_absolute_path_is_not_unlocated(tmp_path, monkeypatch):
    from factlattice_engine import authoring, config
    ok, odd = _route_world(tmp_path, monkeypatch, {"roots": [{"pth": "{ODD}"}], "domain_suffix": "x.odd"})
    st = config.build_store()
    assert "oddc" in st.tables()[1] and st.unlocated() == frozenset()
    w = authoring.add_property(str(ok / "a.lts"), "A-2", "k", "v", store=st)
    assert w["root_membership"]["corpora"] == ["okc"] and not w["root_membership"].get("registry_error"), w["root_membership"]


def _unlocated_engine(tmp_path, monkeypatch):
    from factlattice_engine import config
    from factlattice_engine.tools import LtsEngine
    ok, odd = _route_world(tmp_path, monkeypatch, {"roots": [{"path": 7}], "domain_suffix": "x.odd"})
    st = config.build_store()
    assert st.unlocated() == frozenset({"oddc"})
    return ok, odd, st, LtsEngine(st), tmp_path / "reg.json"


def test_the_flag_clears_when_the_registry_entry_is_fixed_and_registered(tmp_path, monkeypatch):
    """(1): 'fixed' is the registry file corrected and the corpus registered from it. Until then a refused register leaves the flag standing."""
    from factlattice_engine import tools as T
    ok, odd, st, eng, regfile = _unlocated_engine(tmp_path, monkeypatch)
    still = T.dispatch("lts_register_corpus", {"corpus_id": "oddc"}, tools=eng)
    assert still.get("success") is False and st.unlocated() == frozenset({"oddc"}), still
    reg = json.loads(regfile.read_text())
    reg["oddc"] = {"root": str(odd), "domain_suffix": "x.odd"}
    regfile.write_text(json.dumps(reg))
    out = T.dispatch("lts_register_corpus", {"corpus_id": "oddc"}, tools=eng)
    assert out.get("success") is True and st.unlocated() == frozenset(), out
    from factlattice_engine import authoring
    w = authoring.add_property(str(odd / "o.lts"), "O-2", "k", "v", store=st)
    assert w["root_membership"]["corpora"] == ["oddc"] and not w["root_membership"].get("registry_error")


def test_the_flag_clears_when_the_entry_is_unregistered(tmp_path, monkeypatch):
    """(1): `lts_unregister_corpus` of an entry the registry could not place ends the flag and drops its no-roots record, so a listing no longer names it and
    writes attribute again. Red on 90737fb (unregister refused it, `no such loaded corpus`, and every write stayed flagged)."""
    from factlattice_engine import authoring
    from factlattice_engine import tools as T
    ok, odd, st, eng, _reg = _unlocated_engine(tmp_path, monkeypatch)
    out = T.dispatch("lts_unregister_corpus", {"corpus_id": "oddc"}, tools=eng)
    assert out.get("success") is True and out.get("was_unlocated") is True, out
    assert st.unlocated() == frozenset() and "oddc" not in st.tables()[2] and "oddc" not in st.tables()[1]
    w = authoring.add_property(str(ok / "a.lts"), "A-2", "k", "v", store=st)
    assert w["root_membership"]["corpora"] == ["okc"] and not w["root_membership"].get("registry_error"), w["root_membership"]
    again = T.dispatch("lts_unregister_corpus", {"corpus_id": "oddc"}, tools=eng)
    assert again.get("success") is False


def test_the_flag_clears_when_the_corpus_is_registered_again_with_an_explicit_spec(tmp_path, monkeypatch):
    """(1): re-registering the id with a spec whose roots are readable takes it out of `unlocated` (the register reaches the seat, which serves the corpus)."""
    from factlattice_engine import tools as T
    ok, odd, st, eng, _reg = _unlocated_engine(tmp_path, monkeypatch)
    out = T.dispatch("lts_register_corpus", {"corpus_id": "oddc", "spec": {"root": str(odd), "domain_suffix": "x.odd"}}, tools=eng)
    assert out.get("success") is True and st.unlocated() == frozenset(), out


def test_the_kernel_gives_back_the_untrusted_strings_the_registry_no_longer_names(tmp_path):
    """P3-2. `retain_untrusted` keeps only the strings of entries that are still failed on a key: the others leave the set, their idle slots are dropped and the
    watcher stops asking. A string a corpus declared is never dropped. Red on 90737fb (`_untrusted` never shrank)."""
    from factlattice.rootview import lexical
    from factlattice_engine.kernel import FreshnessKernel
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for d in (a, b, c):
        d.mkdir()
    k = FreshnessKernel(root_deadline_s=5)
    k.declare_untrusted([str(a), str(b), str(c)])
    k.declare_roots([str(c)])
    end = time.monotonic() + 10
    while time.monotonic() < end and not all(f.state == "ok" for f in k.root_facts() if f.declared in {lexical(str(a)), lexical(str(b)), lexical(str(c))}):
        time.sleep(0.02)
    k.retain_untrusted([str(a)])
    assert k.untrusted_paths() == {lexical(str(a))}
    declared = {f.declared for f in k.root_facts()}
    assert lexical(str(a)) in declared and lexical(str(b)) not in declared and lexical(str(c)) in declared, declared


def test_a_register_or_unregister_gives_back_the_kernels_untrusted_strings(tmp_path, monkeypatch):
    """P3-2, through the engine. `_retain_untrusted` keeps only the strings of entries the store still lists as failed: after the entry is gone from the failed
    table the kernel drops its string."""
    from factlattice_engine import store as S
    from factlattice_engine.kernel import FreshnessKernel
    from factlattice_engine.tools import LtsEngine
    d = tmp_path / "typo"
    d.mkdir()
    st = S.LtsStore()
    st.kernel = FreshnessKernel(root_deadline_s=5)
    st.kernel.declare_untrusted([str(d)])
    st.fail("t1", "registry-root-unknown-key", None, False, "x.t1")
    eng = LtsEngine(st)
    eng._retain_untrusted({"t1": {"roots": [{"pth": str(d)}]}}, st.snapshot_all()[2])
    assert len(st.kernel.untrusted_paths()) == 1, "still failed: kept"
    st._failed_corpora.clear()                      # the entry was fixed and registered: it leaves the failed table
    eng._retain_untrusted({"t1": {"roots": [{"pth": str(d)}]}}, st.snapshot_all()[2])
    assert st.kernel.untrusted_paths() == set()


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
# ROUND 13 (read of 594ef8a): two-step state changes made one step; clear_all covers every store attribute
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def _watch_listing(eng, during, stop_after_s=3.0):
    """Run `during()` on a thread while this thread lists the corpora in a tight loop; return the set of observations `(ids, registry error shown)`."""
    seen = set()
    done = threading.Event()
    t = threading.Thread(target=lambda: (during(), done.set()), daemon=True)
    t.start()
    end = time.monotonic() + stop_after_s
    while time.monotonic() < end and not done.is_set():
        out = eng.list_corpora(heal=False)
        seen.add((tuple(c["corpus_id"] for c in out["corpora"]), bool((out.get("registry_choice") or {}).get("error"))))
    t.join(5)
    out = eng.list_corpora(heal=False)
    seen.add((tuple(c["corpus_id"] for c in out["corpora"]), bool((out.get("registry_choice") or {}).get("error"))))
    return seen


def _registry_error_world(tmp_path, monkeypatch):
    from factlattice_engine import store as S
    from factlattice_engine.tools import LtsEngine
    w = _world6(tmp_path, monkeypatch)
    st = S.LtsStore()
    st.add_partition(_part6(w, "a1"))
    eng = LtsEngine(st)
    eng._reg_state = ({"source": "env", "path": str(w.registry_path), "sha256_at_boot": "0" * 64, "resolved_at": "2026-01-01T00:00:00Z"}, frozenset(), None)
    return st, eng


def test_seating_the_registry_error_and_emptying_the_store_is_one_step_for_a_reader(tmp_path, monkeypatch):
    """F3. Registry-error mode empties the store and seats the engine's error together under the store's lock (`LtsStore.clear_all(also=)`), and the listing
    reads both under that lock. STAGED with a planted sleep inside the transition: every listing taken meanwhile is the state before (the corpus, no error) or
    after (nothing, the error), never the corpus beside the error, nor nothing without the error. The control runs the old two steps with the same sleep and
    the detector sees the half state, so the staging can see what it claims to. Red on 594ef8a (`_enter_registry_error` did not exist; the two steps)."""
    from factlattice.registry import RegistryError
    from factlattice_engine import server
    st, eng = _registry_error_world(tmp_path, monkeypatch)
    err = RegistryError("boom", source="env", persist="boom")
    real_seat = eng.seat_registry_error

    def slow_seat(e):
        time.sleep(0.4)                              # a planted sleep INSIDE the transition, BEFORE the error is seated (and the store already empty)
        real_seat(e)
        time.sleep(0.4)                              # ... and after it
    eng.seat_registry_error = slow_seat
    kernel = type("K", (), {"unseat_partition": lambda self, cid: True})()
    seen = _watch_listing(eng, lambda: server._enter_registry_error(eng, st, kernel, err))
    assert seen <= {(("a1",), False), ((), True)} and ((), True) in seen, seen
    # the control: the SAME sleep between two steps (seat, then clear) is seen as a half state
    st2, eng2 = _registry_error_world(tmp_path / "c", monkeypatch)

    def two_steps():
        eng2.seat_registry_error(err)
        time.sleep(0.6)
        st2.clear_all()
    seen2 = _watch_listing(eng2, two_steps)
    assert (("a1",), True) in seen2, ("the detector must see the old half state", seen2)


def test_settling_unserved_and_flagging_unlocated_is_one_step_for_a_reader(tmp_path, monkeypatch):
    """F5. A corpus that ends its read unserved with roots that cannot be read as paths moves to the no-roots table AND into `unlocated` in one store step
    (`settle(unlocated=True)`), and a reader takes the tables and the set together (`consistent()`). STAGED with a planted sleep inside the step: no
    reader finds it in the no-roots table without its flag, nor flagged while pending. The control (the old two calls with the same sleep) is seen half. Red on
    594ef8a (`settle` had no `unlocated`, and `add_unlocated` followed it)."""
    from factlattice_engine import store as S
    for name, two_step in (("one step", False), ("control: two steps", True)):
        st = S.LtsStore()
        st.declare_pending("zz", {"spec": {}, "private": False, "paths": [], "domain_suffix": "x", "since": 0.0, "stuck": False})
        if not two_step:
            st._pending_corpora = _SleepyPending(st._pending_corpora)        # a planted sleep at the START of the locked step, before any state changes
        seen = set()
        done = threading.Event()

        def writer():
            if two_step:
                st.settle("zz", no_roots={"spec": {}, "private": False})
                time.sleep(0.5)
                st.add_unlocated("zz")
            else:
                st.settle("zz", no_roots={"spec": {}, "private": False}, unlocated=True)
            done.set()
        threading.Thread(target=writer, daemon=True).start()
        end = time.monotonic() + 3
        while time.monotonic() < end and not done.is_set():
            look, unl = _snap_unl(st)
            seen.add(("pending" if "zz" in look[1] else "no-roots" if "zz" in look[3] else "none", "zz" in unl))
        look, unl = _snap_unl(st)
        seen.add(("pending" if "zz" in look[1] else "no-roots" if "zz" in look[3] else "none", "zz" in unl))
        if two_step:
            assert ("no-roots", False) in seen, (name, seen)
        else:
            assert seen <= {("pending", False), ("no-roots", True)} and ("no-roots", True) in seen, (name, seen)


class _SleepyPending(dict):
    """`_pending_corpora` whose membership test (the first thing `settle` does inside its locked step) sleeps: a planted pause with the lock held and nothing yet changed."""
    def __contains__(self, key):
        time.sleep(0.5)
        return super().__contains__(key)


def _snap_unl(st):
    """The tables and `unlocated` as a reader takes them together: under the store's one lock."""
    with st.consistent():
        return st.snapshot_all(), st.unlocated()


def _clear_all_gaps(store):
    """The names of the store's attributes `clear_all` leaves non-empty after each was filled. A name that is neither a known container nor exempt is a gap
    too: it must be classified."""
    exempt = {"kernel", "_pending_cv", "_sealed"}          # the process's kernel, the store's own lock (neither is state about corpora), and the mode flag `clear_all` SETS (the mode ends only by a restart)
    gaps, filled = [], []
    for name, val in list(vars(store).items()):
        if name in exempt:
            continue
        if isinstance(val, dict):
            val["__marker__"] = 1
        elif isinstance(val, list):
            val.append("__marker__")
        elif isinstance(val, set):
            val.add("__marker__")
        elif isinstance(val, frozenset):
            setattr(store, name, frozenset({"__marker__"}))
        else:
            gaps.append(name + " (unclassified)")
            continue
        filled.append(name)
    store.clear_all()
    gaps += [n for n in filled if getattr(store, n)]
    return sorted(gaps)


def test_clear_all_empties_every_store_attribute():
    """F9. Registry-error mode serves and lists NO corpus, so `clear_all` must empty EVERY piece of store state, not only the tables it knew when it was written.
    Each attribute of a fresh store is filled and `clear_all` run: none may be left non-empty. A NEW store attribute that `clear_all` does not clear fails here
    (the planted control proves the detector); an attribute that is neither a container nor exempt must be classified."""
    from factlattice_engine import store as S
    assert _clear_all_gaps(S.LtsStore()) == []

    class Planted(S.LtsStore):
        def __init__(self):
            super().__init__()
            self._planted_new_table = {}               # a store attribute added later, which clear_all does not know
    assert _clear_all_gaps(Planted()) == ["_planted_new_table"]

    class Unclassified(S.LtsStore):
        def __init__(self):
            super().__init__()
            self._counter = 0
    assert _clear_all_gaps(Unclassified()) == ["_counter (unclassified)"]


def test_a_writes_view_takes_the_tables_and_the_unlocated_set_in_one_step(tmp_path, monkeypatch):
    """F5, at the reader a write uses. `membership.snapshot` reads the tables and `unlocated` together under `store.consistent()`, so while a corpus settles unserved
    and flagged (a planted sleep inside the store's step) no view has it in the no-roots table without the flag, nor flagged while still pending. Red if the
    snapshot reads the two separately (the flag is then read first, or last, and misses the step)."""
    from factlattice_engine import membership
    from factlattice_engine import store as S
    st = S.LtsStore()
    st.declare_pending("zz", {"spec": {}, "private": False, "paths": [], "domain_suffix": "x", "since": 0.0, "stuck": False})
    st._pending_corpora = _SleepyPending(st._pending_corpora)            # a planted sleep at the START of the locked step, before any state changes
    seen, done = set(), threading.Event()

    def writer():
        st.settle("zz", no_roots={"spec": {}, "private": False}, unlocated=True)
        done.set()
    threading.Thread(target=writer, daemon=True).start()
    end = time.monotonic() + 3
    while time.monotonic() < end and not done.is_set():
        v = membership.snapshot(st)
        seen.add(("pending" if "zz" in v.look[1] else "no-roots" if "zz" in v.look[3] else "none", "zz" in v.unlocated))
    v = membership.snapshot(st)
    seen.add(("pending" if "zz" in v.look[1] else "no-roots" if "zz" in v.look[3] else "none", "zz" in v.unlocated))
    assert seen <= {("pending", False), ("no-roots", True)} and ("no-roots", True) in seen, seen


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
# ROUND 14 (read of 9b85439): the PRODUCTION call sites of the one-step fixes, through the entry points the system uses
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def test_the_engines_list_corpora_reads_the_tables_and_the_error_under_one_lock(tmp_path, monkeypatch):
    """P2-1, call site 1: `LtsEngine.list_corpora` (the real entry point). A thread that is descheduled between the listing's look at the store and its read of the
    registry-error state is STAGED with a sleep inside the look (the store's `snapshot_all` pauses after taking it), and registry-error mode begins meanwhile.
    With `consistent()` in `list_corpora` the transition waits for the listing, which shows the corpus and no error; without it the listing shows the corpus AND
    the error. Red when `list_corpora` is reverted to reading the two apart."""
    from factlattice.registry import RegistryError
    from factlattice_engine import server
    st, eng = _registry_error_world(tmp_path, monkeypatch)
    in_gap = threading.Event()
    real = st.snapshot_all
    first = []

    def paused():
        out = real()
        if not first:
            first.append(1)
            in_gap.set()
            time.sleep(0.5)                       # the planted sleep: the listing holds its look and has not yet read the error
        return out
    st.snapshot_all = paused
    kernel = type("K", (), {"unseat_partition": lambda self, cid: True})()
    t = threading.Thread(target=lambda: (in_gap.wait(5), server._enter_registry_error(eng, st, kernel, RegistryError("boom", source="env", persist="boom"))), daemon=True)
    t.start()
    out = eng.list_corpora(heal=False)
    t.join(5)
    shown = (tuple(c["corpus_id"] for c in out["corpora"]), bool((out.get("registry_choice") or {}).get("error")))
    assert shown in {(("a1",), False), ((), True)}, shown


def test_the_loaders_own_load_path_flags_an_unserved_corpus_in_the_step_that_moves_it(tmp_path, monkeypatch):
    """P2-1, call site 2: `StoreLoader._load_one` (the loader's real path for a corpus whose read ends with nothing to serve and roots that cannot be read as paths,
    `roots: [{path: 7}]`). A pause is planted where a thread could be descheduled between the move and a SECOND call that records the flag (`add_unlocated` sleeps
    first); a reader takes the tables and the set together the whole time. In the loader as built the flag is part of the move and the sleep is never reached; reverted
    to settle-then-add_unlocated, a reader finds the corpus in the no-roots table with no flag. Red on that revert."""
    from factlattice_engine import bootload
    from factlattice_engine import store as S
    st = S.LtsStore()
    loader = bootload.StoreLoader(st, {"zz": {"roots": [{"path": 7}], "domain_suffix": "x.zz"}}, kernel=None, retry=False, workers=1)
    loader.declare()
    real_add = st.add_unlocated
    st.add_unlocated = lambda cid: (time.sleep(0.5), real_add(cid))[1]
    seen, done = set(), threading.Event()
    threading.Thread(target=lambda: (loader._load_one("zz"), done.set()), daemon=True).start()
    end = time.monotonic() + 5
    while time.monotonic() < end and not done.is_set():
        look, unl = _snap_unl(st)
        seen.add(("pending" if "zz" in look[1] else "no-roots" if "zz" in look[3] else "none", "zz" in unl))
    look, unl = _snap_unl(st)
    seen.add(("pending" if "zz" in look[1] else "no-roots" if "zz" in look[3] else "none", "zz" in unl))
    assert ("no-roots", True) in seen and seen <= {("pending", False), ("no-roots", True)}, seen


def test_the_servers_first_pass_seats_the_registry_error_and_empties_the_store_in_one_step(tmp_path, monkeypatch):
    """P2-1, call site 3: `server.after_first_pass`, on the engine `build_tools(async_boot=True)` returns, booted from a registry whose second entry (a pinned file that is not a string beside a root that exists: x-boot-window's
    disk-needing class) only the loader meets. The engine's `seat_registry_error` is slowed (a pause before and after it takes effect) and a listing polls the whole time: once
    the healthy corpus has been seen served, no listing may show it beside the error or an empty store without the error. Red when `after_first_pass` is reverted
    to seat-then-clear (the corpus and the error are then on screen together for the length of the pause)."""
    from factlattice_engine import server, tools
    w = _world6(tmp_path, monkeypatch)
    reg = json.loads(w.registry_path.read_text())
    reg = {"a1": reg["a1"], "zz-bad": {"roots": [{"path": str(w.root("b1")), "only_file": 1}], "domain_suffix": "x.bad"}}      # a registry error only a ROOT READ can know (x-boot-window)
    w.registry_path.write_text(json.dumps(reg))
    real_seat = tools.LtsEngine.seat_registry_error

    def slow_seat(self, err):
        time.sleep(0.4)
        real_seat(self, err)
        time.sleep(0.4)
    monkeypatch.setattr(tools.LtsEngine, "seat_registry_error", slow_seat)
    eng = server.build_tools(watch=False, async_boot=True)
    seen_served, bad = False, []
    end = time.monotonic() + 20
    while time.monotonic() < end:
        out = eng.list_corpora(heal=False)
        ids = [c["corpus_id"] for c in out["corpora"]]
        err = bool((out.get("registry_choice") or {}).get("error"))
        if ids:
            seen_served = True
        if (ids and err) or (seen_served and not ids and not err):
            bad.append((ids, err))
        if err and not ids:
            break
        time.sleep(0.01)
    assert not bad, bad[:3]
    assert eng.registry_error is not None, "the registry error was seated"


def test_a_named_call_that_passed_the_registry_gate_before_the_mode_began_still_answers_registry_not_found(tmp_path, monkeypatch):
    """P3-1. The registry gate reads `registry_error` without a lock. A named find whose gate read happened just BEFORE registry-error mode began finds the emptied store
    in `_gate` and used to answer 'corpus not found ... NO public corpora are loaded'. STAGED: the gate's verdict is taken, the transition runs, and only then does the
    call go on. The answer is `registry-not-found` (the error was seated in the same locked step that emptied the store, and `_gate` re-checks after its look)."""
    from factlattice.registry import RegistryError
    from factlattice_engine import server
    from factlattice_engine import tools as T
    st, eng = _registry_error_world(tmp_path, monkeypatch)
    kernel = type("K", (), {"unseat_partition": lambda self, cid: True})()
    real_gate = T._registry_gate

    def gate_then_transition(t, name, a):
        verdict = real_gate(t, name, a)                  # passes: the mode has not begun
        assert verdict is None
        server._enter_registry_error(eng, st, kernel, RegistryError("boom", source="env", persist="boom"))
        return verdict
    monkeypatch.setattr(T, "_registry_gate", gate_then_transition)
    out = T.dispatch("lts_find", {"corpus": "a1", "prefix": "A"}, tools=eng)
    assert out.get("reason") == "registry-not-found" and "NO public corpora" not in json.dumps(out), out


def test_a_raising_also_leaves_the_store_as_it_was():
    """P3-2. `clear_all(also=)` runs `also` FIRST: if it raises, nothing was emptied and the served ids are not lost. Red when `also` runs after the store is emptied."""
    from factlattice_engine import store as S
    st = S.LtsStore()
    st.declare_pending("p", {"spec": {}, "private": False, "paths": [], "domain_suffix": "x", "since": 0.0, "stuck": False})
    st.add_unlocated("u")
    st.fail("f", "registry-entry-unknown-key", None, False, "x.f")
    st.set_order(["p", "f"])
    before = (st.snapshot_all()[1:], st.unlocated(), list(st._order))

    def boom():
        raise RuntimeError("seat failed")
    with pytest.raises(RuntimeError):
        st.clear_all(also=boom)
    assert (st.snapshot_all()[1:], st.unlocated(), list(st._order)) == before
    assert st.clear_all() == [] and not st.snapshot_all()[1] and st.unlocated() == frozenset()


def _consistent_scan(source: str):
    """Findings `(line, what)` for ONE module's source. Checks: (1) every call inside a `with <x>.consistent():` block is an allowed memory-only reader AND is called
    on a receiver whose text ends in `store` (so a look-alike name on another object is not accepted); (2) a `.consistent()` call anywhere but directly as a `with` item
    (held in a variable, passed to `ExitStack.enter_context`, returned) is a finding; (3) the callable passed as `also=` to `clear_all` (a lambda, or a nested def of the same module named in the call) may call only
    `seat_registry_error`, `seal` and `hasattr` (anything else, or a callable bound some other way, is REPORTED, not accepted). It cannot see INSIDE any call it allows: `seal()` is an attribute call on the kernel and its body is not read (nor is any allowed reader's). The `also=` rule matches those names by ATTRIBUTE
    NAME only: it does NOT see a `seal()` on any other receiver (a `seal` that blocks is accepted), a `for` or a `with` around `seal()` in the nested def (only Call nodes are read
    there), `hasattr` on an object with a blocking `__getattr__`, or a same-named def elsewhere in the module (the first def of that name wins); (4) inside the block a `for`, an `await`, a nested `with` whose manager is a name or attribute,
    and a second with-item that is not a call are findings (iteration and context managers can block without a Call node); (5) `c = store.consistent` (the method taken
    without being called) and `c = store.consistent()` entered later are findings. A LEXICAL TRIPWIRE for the listed forms, NOT A PROOF: other spellings exist. What it does not look at: it checks CALL nodes, `for`, `await` and `with` only, so a
    blocking attribute read (a property), an operator overload, a subscript or a decorator under the lock is invisible; a lock reached by `getattr`, `vars()` or an alias of
    `_pending_cv` in code that does not contain the text `_pending_cv`; a callee that blocks INSIDE an allowed name (the allowed readers are the store's own and are memory-only
    by their definitions); and a `with` whose context manager is something other than a call of a method named `consistent`."""
    import ast
    tree = ast.parse(source)
    out, as_with_item = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for i in node.items:
                c = i.context_expr
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "consistent":
                    as_with_item.add(id(c))
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call) and sub is not c:
                            f = sub.func
                            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "?")
                            recv = ast.unparse(f.value) if isinstance(f, ast.Attribute) else ""
                            if name not in CONSISTENT_ALLOWED:
                                out.append((sub.lineno, name))
                            elif recv and not recv.endswith("store"):
                                out.append((sub.lineno, name + " on " + recv))
    # inside a `consistent()` block: iteration, `await` and a `with` whose manager is not a call can all block without a Call node, so each is a finding
    for node in ast.walk(tree):
        if isinstance(node, ast.With) and any(isinstance(i.context_expr, ast.Call) and isinstance(i.context_expr.func, ast.Attribute) and i.context_expr.func.attr == "consistent"
                                              for i in node.items):
            for item in node.items:
                c = item.context_expr
                if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "consistent") and not isinstance(c, ast.Call):
                    out.append((c.lineno, "a with-item that is not a call, beside the lock"))
            for sub in ast.walk(node):
                if isinstance(sub, (ast.For, ast.AsyncFor)):
                    out.append((sub.lineno, "a `for` under the lock (iteration may block)"))
                elif isinstance(sub, ast.Await):
                    out.append((sub.lineno, "`await` under the lock"))
                elif isinstance(sub, (ast.With, ast.AsyncWith)) and sub is not node and any(not isinstance(i.context_expr, ast.Call) for i in sub.items):
                    out.append((sub.lineno, "a nested `with` of a name or attribute under the lock"))
    called_attrs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "consistent" and id(node) not in called_attrs:
            out.append((node.lineno, "`.consistent` taken without being called here (aliased): the lock may be entered anywhere"))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "consistent" and id(node) not in as_with_item:
                out.append((node.lineno, "consistent() not directly a with item"))
            if node.func.attr == "clear_all":
                for kw in node.keywords:
                    if kw.arg == "also":
                        target = kw.value
                        if isinstance(target, ast.Name):            # bound to a nested def in the same module: judged by ITS calls
                            target = next((d for d in ast.walk(tree) if isinstance(d, ast.FunctionDef) and d.name == target.id), target)
                        calls = [c for c in ast.walk(target) if isinstance(c, ast.Call)]
                        ok_names = {"seat_registry_error", "seal", "hasattr"}
                        if not isinstance(target, (ast.Lambda, ast.FunctionDef)) or any(
                                not ((isinstance(c.func, ast.Attribute) and c.func.attr in ok_names) or (isinstance(c.func, ast.Name) and c.func.id in ok_names)) for c in calls):
                            out.append((node.lineno, "clear_all(also=) with a callable whose calls are not only seat_registry_error / seal"))
    return sorted(out)


CONSISTENT_ALLOWED = {"snapshot_all", "unlocated", "_look", "tables"}      # memory-only reads of the store; nothing that waits, reads a disk or calls out


def test_nothing_does_blocking_work_under_the_stores_lock():
    """P3-2 / P3-4. `store.consistent()` hands out the store's one lock. `_consistent_scan` (a lexical tripwire for its listed forms, not a proof) checks every module of the engine:
    only memory-only readers inside the block, on a `...store` receiver; no `.consistent()` held in a variable or handed to an `ExitStack`; `clear_all(also=)` only with
    a lambda that seats the registry error; and NO module but store.py touches the raw lock `_pending_cv`. Each planted source is a control that the scan catches."""
    root = Path(__file__).resolve().parent.parent
    seen = 0
    for f in sorted((root / "factlattice_engine").rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        assert _consistent_scan(text) == [], (str(f.relative_to(root)), _consistent_scan(text))
        seen += text.count(".consistent()")
        if f.name != "store.py":
            assert "_pending_cv" not in text, (str(f.relative_to(root)), "the store's raw lock is the store's own")
    assert seen == 2, "the scan saw the real uses (list_corpora, membership.snapshot); a new use is a decision"
    planted = {
        "a disk call": "def f(store):\n    with store.consistent():\n        os.stat('/x')\n        store.snapshot_all()\n",
        "a look-alike name": "def f(store, other):\n    with store.consistent():\n        other.snapshot_all()\n",
        "a lock held in a variable": "def f(store):\n    lk = store.consistent()\n    with lk:\n        store.snapshot_all()\n",
        "an ExitStack": "def f(store, stack):\n    stack.enter_context(store.consistent())\n",
        "a blocking also": "def f(store):\n    store.clear_all(also=lambda: time.sleep(5))\n",
        "an also that is a name": "def f(store, g):\n    store.clear_all(also=g)\n",
        "a method alias": "def f(store):\n    c = store.consistent\n    with c():\n        store.snapshot_all()\n",
        "a bound lock entered later": "def f(store):\n    c = store.consistent()\n    with c:\n        pass\n",
        "a for under the lock": "def f(store, fh):\n    with store.consistent():\n        for line in fh:\n            pass\n",
        "await under the lock": "async def f(store, x):\n    with store.consistent():\n        await x\n",
        "a nested with of a name": "def f(store, lk):\n    with store.consistent():\n        with lk:\n            pass\n",
        "a second with-item that is a name": "def f(store, lk):\n    with store.consistent(), lk:\n        pass\n",
    }
    for what, src in planted.items():
        assert _consistent_scan(src), ("the scan must catch", what)
    assert _consistent_scan("def f(store, engine):\n    with store.consistent():\n        a = store.snapshot_all()\n    store.clear_all(also=lambda: engine.seat_registry_error(1))\n") == []


def _self_attrs_assigned_outside_init(source: str, cls: str):
    """The attributes of `self` that a method OTHER than `__init__` creates and `__init__` does not. A LEXICAL TRIPWIRE for the listed forms, NOT A PROOF: other spellings exist. The forms it looks at: plain, augmented and annotated assignment;
    tuple, list and starred targets; `for ... in` and `with ... as` targets; `setattr(self, "x", ...)` and `object.__setattr__(self, "x", ...)` (a non-constant name is
    reported as `<dynamic setattr>`); a subscript write through `self.__dict__[...]` or `vars(self)[...]`, and `self.__dict__.update/setdefault(...)` (all
    `<dynamic __dict__>`, which includes `self.__dict__.__setitem__(...)`); an assignment through `type(self).y` or `self.__class__.y` (`<class attribute: y>`);
    `setattr(type(self), ...)` (`<class attribute via setattr>`); `getattr(self, "__setattr__" | "__dict__")` (`<dynamic setattr>`); and a local alias of `self`
    (`s = self` ... `s.x = ...`). What it does not look at: an attribute created by code outside the class (`store.x = ...` in another module), a write through a
    `vars(...)` or `__dict__` that was first bound to a name, `getattr(self, name)` with a non-constant name, `exec`/`eval`/metaclass/descriptor tricks, and an alias made by
    anything but a plain `a = self` assignment."""
    import ast
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    init = next((m for m in node.body if isinstance(m, ast.FunctionDef) and m.name == "__init__"), None)

    def flat(t):
        if isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                yield from flat(e)
        elif isinstance(t, ast.Starred):
            yield from flat(t.value)
        else:
            yield t

    def targets(fn):
        selfs = {"self"}
        for sub in ast.walk(fn):                                          # local aliases of self
            if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Name) and sub.value.id in selfs:
                selfs |= {t.id for t in sub.targets if isinstance(t, ast.Name)}
        names = set()
        for sub in ast.walk(fn):
            ts = []
            if isinstance(sub, ast.Assign):
                ts = sub.targets
            elif isinstance(sub, (ast.AugAssign, ast.AnnAssign)):
                ts = [sub.target]
            elif isinstance(sub, (ast.For, ast.AsyncFor)):
                ts = [sub.target]
            elif isinstance(sub, (ast.With, ast.AsyncWith)):
                ts = [i.optional_vars for i in sub.items if i.optional_vars is not None]
            for t0 in ts:
                for t in flat(t0):
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id in selfs:
                        names.add(t.attr)
                    if isinstance(t, ast.Subscript) and ((isinstance(t.value, ast.Attribute) and t.value.attr == "__dict__")
                                                         or (isinstance(t.value, ast.Call) and isinstance(t.value.func, ast.Name) and t.value.func.id == "vars"
                                                             and t.value.args and isinstance(t.value.args[0], ast.Name) and t.value.args[0].id in selfs)):
                        names.add("<dynamic __dict__>")
                    if isinstance(t, ast.Attribute) and ((isinstance(t.value, ast.Call) and isinstance(t.value.func, ast.Name) and t.value.func.id == "type"
                                                          and t.value.args and isinstance(t.value.args[0], ast.Name) and t.value.args[0].id in selfs)
                                                         or (isinstance(t.value, ast.Attribute) and t.value.attr == "__class__" and isinstance(t.value.value, ast.Name)
                                                             and t.value.value.id in selfs)):
                        names.add("<class attribute: " + t.attr + ">")
            is_setattr = isinstance(sub, ast.Call) and ((isinstance(sub.func, ast.Name) and sub.func.id == "setattr")
                                                       or (isinstance(sub.func, ast.Attribute) and sub.func.attr == "__setattr__" and isinstance(sub.func.value, ast.Name)
                                                           and sub.func.value.id == "object"))
            if is_setattr and sub.args and isinstance(sub.args[0], ast.Call) and isinstance(sub.args[0].func, ast.Name) and sub.args[0].func.id == "type" \
                    and sub.args[0].args and isinstance(sub.args[0].args[0], ast.Name) and sub.args[0].args[0].id in selfs:
                names.add("<class attribute via setattr>")
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "getattr" and len(sub.args) > 1 and isinstance(sub.args[0], ast.Name) \
                    and sub.args[0].id in selfs and isinstance(sub.args[1], ast.Constant) and sub.args[1].value in ("__setattr__", "__dict__"):
                names.add("<dynamic setattr>")
            if is_setattr and sub.args and isinstance(sub.args[0], ast.Name) and sub.args[0].id in selfs:
                a1 = sub.args[1] if len(sub.args) > 1 else None
                names.add(a1.value if isinstance(a1, ast.Constant) and isinstance(a1.value, str) else "<dynamic setattr>")
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr in ("update", "setdefault", "__setitem__") and isinstance(sub.func.value, ast.Attribute) \
                    and sub.func.value.attr == "__dict__":
                names.add("<dynamic __dict__>")
        return names
    in_init = targets(init) if init else set()
    elsewhere = set()
    for m in node.body:
        if isinstance(m, ast.FunctionDef) and m.name != "__init__":
            elsewhere |= targets(m)
    return sorted(elsewhere - in_init)


def test_no_store_attribute_is_created_outside_init():
    """P3-3. `test_clear_all_empties_every_store_attribute` sees what `__init__` creates. An attribute created LAZILY in another method would escape it, so the
    source of `LtsStore` is scanned: every `self.<attr> =` in any method must name an attribute `__init__` already creates. The planted class is the control."""
    root = Path(__file__).resolve().parent.parent
    assert _self_attrs_assigned_outside_init((root / "factlattice_engine" / "store.py").read_text(encoding="utf-8"), "LtsStore") == []
    def cls(body):
        return "class LtsStore:\n    def __init__(self):\n        self.a = {}\n    def later(self):\n" + "".join("        " + ln + "\n" for ln in body)
    planted = {
        "plain": (["self.lazy = {}"], ["lazy"]),
        "tuple": (["self.a, self.t = {}, ()"], ["t"]),
        "list target": (["[self.l1, self.l2] = 1, 2"], ["l1", "l2"]),
        "starred": (["x, *self.rest = 1, 2, 3"], ["rest"]),
        "for target": (["for self.f in range(2):", "    pass"], ["f"]),
        "with target": (["with open('x') as self.w:", "    pass"], ["w"]),
        "setattr constant": (["setattr(self, 'sa', {})"], ["sa"]),
        "setattr dynamic": (["setattr(self, 'q' + 'r', {})"], ["<dynamic setattr>"]),
        "__dict__": (["self.__dict__['d'] = 1"], ["<dynamic __dict__>"]),
        "__dict__ update": (["self.__dict__.update(z=1)"], ["<dynamic __dict__>"]),
        "alias": (["s = self", "s.al = 1"], ["al"]),
        "vars(self)": (["vars(self)['v'] = 1"], ["<dynamic __dict__>"]),
        "object.__setattr__": (["object.__setattr__(self, 'o', 1)"], ["o"]),
        "object.__setattr__ dynamic": (["object.__setattr__(self, 'o' + 'p', 1)"], ["<dynamic setattr>"]),
        "type(self)": (["type(self).y = 1"], ["<class attribute: y>"]),
        "__class__": (["self.__class__.z = 1"], ["<class attribute: z>"]),
        "setattr(type(self))": (["setattr(type(self), 'k', 1)"], ["<class attribute via setattr>"]),
        "getattr __setattr__": (["getattr(self, '__setattr__')('g', 1)"], ["<dynamic setattr>"]),
        "__dict__.__setitem__": (["self.__dict__.__setitem__('h', 1)"], ["<dynamic __dict__>"]),
        "augmented": (["self.cnt += 1"], ["cnt"]),
        "clean": (["self.a = {}"], []),
    }
    real_src = (root / "factlattice_engine" / "store.py").read_text(encoding="utf-8")
    hook = "    def forget_unlocated(self, corpus_id: str) -> bool:"
    assert real_src.count(hook) == 1
    # the cold read's control, in the REAL source: a tuple-assigned lazy attribute in a new method
    assert _self_attrs_assigned_outside_init(real_src.replace(hook, "    def _p(self):\n        self._planted_a, self._planted_b = {}, {}\n\n" + hook), "LtsStore") == ["_planted_a", "_planted_b"]
    for what, (body, want) in planted.items():
        assert _self_attrs_assigned_outside_init(cls(body), "LtsStore") == want, (what, _self_attrs_assigned_outside_init(cls(body), "LtsStore"))


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
# ROUND 15 (read of fbb55c2): x-boot-window. Every registry-error verdict that needs no disk is reached in declare()
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
NO_DISK_REGISTRY_ERROR = [
    # reached in declare(): the entry's SHAPE alone decides (the 7 that always were, and the 21 the shape phase now runs before anything is read)
    "roots string", "roots int", "roots true", "roots dict", "roots null", "roots null + root", "root list", "root elem int", "root elem null", "root elem list",
    "root elem empty str", "root elem blank str", "root elem str ok", "path null", "path list", "root obj empty", "root obj area only", "root obj note only",
    "entry string", "entry list", "entry null", "entry int", "entry true", "root dict", "roots two shapes", "roots str + int", "root list, root ok", "roots dict-of-path"]
NEEDS_DISK_REGISTRY_ERROR = {
    "only_file int": "a pinned file that is not a string is joined onto a root only when the root EXISTS (the release raised there, and not for a missing root)",
    "only_file list": "the same",
    "domain_suffix list": "a suffix that cannot be a dict key raises when the corpus is FILED, which needs a corpus read from a root that exists",
}
MET_BEFORE_THE_LOADER = {"roots 5 + unknown": "the server's own expansion of the entries raises before the loader exists"}
# spec 39: a profile the loader refuses was a registry error in the release because the profile is read WITH its corpus (on the loader thread), never in `declare()`.
# It is now a per-corpus failure (REQ-NS-TRACE x-profile-guard); the shapes stay classified here, as "met on the loader thread".
PROFILE_REGISTRY_ERROR = {"profile collision: property key": "the profile's property_keys collides with a built-in key, found when the corpus's profile is read",
                          "profile collision: property key, roots list": "the same, for an entry with a roots list",
                          "profile invalid: property_keys": "the profile's property_keys is not a list, found when the corpus's profile is read",
                          "engine profile directory: layer disagreement": "two profiles in the ENGINE's own profile directory disagree about a layer: engine configuration, met when the engine reads it, still the whole registry (no corpus names it)"}


def test_the_registry_error_shapes_split_into_those_declare_reaches_and_those_that_need_a_disk(tmp_path, monkeypatch):
    """x-boot-window (1). Every shape the release answers with registry-error is in exactly one class, and the classes are pinned: (a) `declare()` raises (no disk
    is needed to know it: 28 shapes), (b) the shape needs a root read (3 shapes, each with its reason), (c) met before the loader (1). A shape that moves between
    classes fails here and must be classified. Red on fbb55c2 for the 21 shapes the loader met."""
    from factlattice_engine import bootload
    from factlattice_engine import store as S
    w = _world6(tmp_path, monkeypatch)
    ok, bad = w.root("a1"), w.root("b1")
    shapes = json.loads((Path(__file__).resolve().parent / "boot_verdicts" / "shapes.json").read_text())
    pinned = json.loads((Path(__file__).resolve().parent / "boot_verdicts" / "af4cfc2.json").read_text())

    def sub(v):
        if isinstance(v, str):
            return v.replace("{BAD}", str(bad)).replace("{OK}", str(ok))
        if isinstance(v, list):
            return [sub(x) for x in v]
        if isinstance(v, dict):
            return {sub(k): sub(x) for k, x in v.items()}
        return v
    at_declare, later = [], []
    for label, entry in shapes.items():
        if not pinned[label]["list"]["registry_error"]:
            continue
        loader = bootload.StoreLoader(S.LtsStore(), {"okc": {"root": str(ok)}, "badc": sub(entry)}, kernel=None, retry=False, workers=1)
        try:
            loader.declare()
            later.append(label)
        except Exception:  # noqa: BLE001
            at_declare.append(label)
    assert sorted(at_declare) == sorted(NO_DISK_REGISTRY_ERROR), (sorted(set(at_declare) ^ set(NO_DISK_REGISTRY_ERROR)))
    assert sorted(later) == sorted(set(NEEDS_DISK_REGISTRY_ERROR) | set(MET_BEFORE_THE_LOADER) | set(PROFILE_REGISTRY_ERROR)), later


def test_a_read_or_write_during_boot_cannot_see_a_served_corpus_for_a_shape_that_needs_no_disk(tmp_path):
    """x-boot-window (1), through a real stdio server. A second healthy corpus is BIG (6,000 nodes, so the first pass lasts long enough that the small healthy corpus, if it WERE read first, would be served for a visible
    moment) and the first things sent after `initialize` are a named find on it and a write to its file. For every shape that needs no disk the find answers
    `registry-not-found` and the first write is exactly the write sent 1.5 s later, after any boot is over (corpora, flag, `membership_partial`): nothing was
    served in between, because the verdict was reached in `declare()`. Red on fbb55c2 (the corpus was read first: the find answered `ok`, the write carried
    `membership_partial` and was attributed)."""
    from concurrent.futures import ThreadPoolExecutor
    shapes = json.loads((Path(__file__).resolve().parent / "boot_verdicts" / "shapes.json").read_text())
    big = ['---\nlts: "1.1"\ntitle: t\ndomain: x.big\ndoctype: SPEC\n---\n', "# R [BIG-ROOT]\n\n- summary: r\n"] + [f"\n## N{n} [BIG-N{n}]\n\n- topic: t\n" for n in range(6000)]

    def one(label):
        base = tmp_path / ("s" + str(abs(hash(label)))[:8])
        ok, bad, bigd = base / "ok", base / "bad", base / "big"
        ok.mkdir(parents=True)
        bad.mkdir()
        bigd.mkdir()
        (ok / "o.lts").write_text('---\nlts: "1.1"\ntitle: t\ndomain: x.ok\ndoctype: SPEC\n---\n\n# R [OK-ROOT]\n\n- summary: r\n', encoding="utf-8")
        (bigd / "g.lts").write_text("".join(big), encoding="utf-8")
        (bad / "b.lts").write_text("# R [BAD-ROOT]\n\n- summary: r\n", encoding="utf-8")

        def sub(v):
            if isinstance(v, str):
                return v.replace("{BAD}", str(bad)).replace("{OK}", str(ok))
            if isinstance(v, list):
                return [sub(x) for x in v]
            if isinstance(v, dict):
                return {sub(k): sub(x) for k, x in v.items()}
            return v
        (base / "reg.json").write_text(json.dumps({"okc": {"root": str(ok), "domain_suffix": "x.ok"}, "bigc": {"root": str(bigd), "domain_suffix": "x.big"}, "badc": sub(shapes[label])}))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTLATTICE_", "LTS_"))}
        env.update(PYTHONPATH=str(Path(__file__).resolve().parent.parent), FACTLATTICE_CORPUS_CONFIG=str(base / "reg.json"), FACTLATTICE_STATE_DIR=str(base / "st"),
                   LTS_ENGINE_STATE_DIR=str(base / "st"), FACTLATTICE_CODE_WATCH="0", FACTLATTICE_LEDGER_EPHEMERAL_ROOTS="", HOME=str(base), TMPDIR=str(base))
        proc = subprocess.Popen([sys.executable, "-m", "factlattice_engine.server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                cwd=str(base), env=env, text=True)
        n = [0]

        def rpc(method, params):
            n[0] += 1
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n[0], "method": method, "params": params}) + "\n")
            proc.stdin.flush()
            while True:
                o = json.loads(proc.stdout.readline())
                if o.get("id") == n[0]:
                    return json.loads(o["result"]["content"][0]["text"]) if "result" in o and "content" in o["result"] else o

        def write(key):
            out = rpc("tools/call", {"name": "lts_add_property", "arguments": {"path": str(ok / "o.lts"), "node_id": "OK-ROOT", "key": key, "value": "v"}})
            rm = out.get("root_membership") or {}
            return (rm.get("corpora"), bool(rm.get("registry_error")), rm.get("membership_partial"))
        try:
            rpc("initialize", {})
            find = rpc("tools/call", {"name": "lts_find", "arguments": {"corpus": "okc", "prefix": "OK"}})       # the FIRST thing after the handshake
            first = write("k1")                                                                                   # ... and the first write
            time.sleep(1.5)                                                                                       # the boot is over by now, window or not
            later = write("k2")
        finally:
            proc.stdin.close()
            proc.wait(timeout=30)
        return label, find.get("reason"), first, later
    with ThreadPoolExecutor(max_workers=6) as ex:
        got = list(ex.map(one, NO_DISK_REGISTRY_ERROR))
    bad = [g for g in got if g[1] != "registry-not-found" or g[2] != g[3]]       # the find is the mode's, and the first write is exactly what a write after the boot is
    assert not bad, bad[:5]


def test_a_corpus_omitted_call_that_passed_the_gate_before_the_mode_began_answers_registry_not_found(tmp_path, monkeypatch):
    """P3-3. A corpus-OMITTED find that passed the lock-free registry gate just before registry-error mode began read the emptied store and answered `success` with 0
    nodes. STAGED as for the named call: the gate's verdict, then the transition, then the call. The answer is the mode's refusal (`dispatch` checks after the call).
    Red on fbb55c2 (success, 0 nodes)."""
    from factlattice.registry import RegistryError
    from factlattice_engine import server
    from factlattice_engine import tools as T
    st, eng = _registry_error_world(tmp_path, monkeypatch)
    kernel = type("K", (), {"unseat_partition": lambda self, cid: True})()
    real_gate = T._registry_gate

    def gate_then_transition(t, name, a):
        verdict = real_gate(t, name, a)
        assert verdict is None
        server._enter_registry_error(eng, st, kernel, RegistryError("boom", source="env", persist="boom"))
        return verdict
    monkeypatch.setattr(T, "_registry_gate", gate_then_transition)
    out = T.dispatch("lts_find", {"prefix": "A"}, tools=eng)
    assert out.get("success") is False and out.get("reason") == "registry-not-found", out


def test_a_raising_first_pass_hook_is_logged_with_its_class_and_not_swallowed_silently(tmp_path, monkeypatch, caplog):
    """P3-2. The first-pass hook seats registry-error mode. It must not strand the loader, but a raise there leaves the store serving when the mode should have begun, so it
    is LOGGED at error level with the class name (never the message, which may carry a path). Red on fbb55c2 (`pass`)."""
    import logging

    from factlattice_engine import bootload
    from factlattice_engine import store as S
    w = _world6(tmp_path, monkeypatch)

    def hook(loader):
        raise RuntimeError("/secret/path/seat failed")
    loader = bootload.StoreLoader(S.LtsStore(), {"a1": w.registry["a1"]}, kernel=None, retry=False, workers=1, on_first_pass=hook)
    loader.declare()
    with caplog.at_level(logging.ERROR, logger="factlattice_engine.bootload"):
        loader.run_blocking()
    msgs = [r.getMessage() for r in caplog.records if "first-pass hook raised" in r.getMessage()]
    assert msgs and "RuntimeError" in msgs[0] and "/secret" not in msgs[0], msgs


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
# ROUND 16 (read of 60410b8)
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def test_the_post_call_recheck_set_is_stated_and_never_holds_a_writer_or_recorder():
    """P2-1. `LTS_POST_CALL_RECHECK_TOOL_NAMES` is the ONE stated set of tools whose answer registry-error mode may replace after the call. It is derived from the CLI
    door's fail-closed READ-ONLY allow-list (a new tool is not on it until it is proven to write nothing), holds no tool of `LTS_WRITE_TOOL_NAMES`, none the mode exempts, and every
    tool the server serves (`server.TOOLS`) is classified: re-checked, or one of write / exempt / selfcheck / semantic. A planted new tool is NOT re-checked."""
    from factlattice_engine import server
    from factlattice_engine import tools as T
    rc = set(T.LTS_POST_CALL_RECHECK_TOOL_NAMES)
    assert rc and not rc & set(T.LTS_WRITE_TOOL_NAMES) and not rc & set(T.REGISTRY_ERROR_EXEMPT_TOOLS), rc
    assert {"lts_report_miss", "lts_register_corpus", "lts_unregister_corpus", "lts_add_property", "lts_add_node", "lts_repair_property", "lts_rename_property",
            "lts_remove_property"}.isdisjoint(rc)
    served = {t["name"] for t in server.TOOLS}
    not_rechecked = set(T.LTS_WRITE_TOOL_NAMES) | set(T.REGISTRY_ERROR_EXEMPT_TOOLS) | {"lts_selfcheck", "lts_semantic_find"}
    assert served <= rc | not_rechecked, sorted(served - rc - not_rechecked)
    planted = {"success": True, "nodes": []}
    assert T._mode_began_meanwhile("lts_a_tool_added_tomorrow", type("E", (), {"registry_error": object()})(), planted) is planted


def test_a_miss_reported_and_a_corpus_registered_across_the_mode_change_reply_what_happened(tmp_path, monkeypatch):
    """P2-1, staged as the cold read staged it: registry-error mode begins AS THE CALL ENDS, after the record is made. `lts_report_miss` appends a line to miss-log.jsonl and
    `lts_register_corpus` seats a corpus: the reply is what happened (success) and the record is there. Red on 60410b8 for report_miss (the reply said `registry-not-found`
    after the line was written; a retry would meet the mode's refusal)."""
    from factlattice.registry import RegistryError
    from factlattice_engine import querylog, server
    from factlattice_engine import tools as T
    st, eng = _registry_error_world(tmp_path, monkeypatch)
    kernel = type("K", (), {"unseat_partition": lambda self, cid: True})()
    err = RegistryError("boom", source="env", persist="boom")

    def then_mode(real):
        def run(*a, **k):
            out = real(*a, **k)                                       # the record is made ...
            server._enter_registry_error(eng, st, kernel, err)       # ... and the mode begins as the call ends
            return out
        return run
    monkeypatch.setattr(eng, "report_miss", then_mode(eng.report_miss))
    miss_log = querylog.miss_log_path()
    before = miss_log.read_text().count("\n") if miss_log.exists() else 0
    out = T.dispatch("lts_report_miss", {"corpus": "a1", "query": "no such thing", "expected_ids": ["A1-N"], "why": "staged"}, tools=eng)
    assert eng.registry_error is not None
    assert out.get("success") is not False and out.get("reason") != "registry-not-found", out
    assert miss_log.exists() and miss_log.read_text().count("\n") == before + 1, "the record the reply speaks of exists"
    # the registry pair: the corpus is seated, then the mode begins
    st2, eng2 = _registry_error_world(tmp_path / "r", monkeypatch)
    monkeypatch.setattr(eng2, "register_corpus", then_mode_for(eng2, st2, kernel, err))
    out2 = T.dispatch("lts_register_corpus", {"corpus_id": "zz", "spec": {"root": _first_root(tmp_path), "domain_suffix": "x.zz"}}, tools=eng2)
    assert eng2.registry_error is not None and out2.get("success") is True and out2.get("reason") != "registry-not-found", out2
    # control: a READ in the same position IS replaced by the mode's refusal
    st3, eng3 = _registry_error_world(tmp_path / "q", monkeypatch)
    monkeypatch.setattr(eng3, "find", lambda *a, **k: (server._enter_registry_error(eng3, st3, kernel, err), {"success": True, "nodes": []})[1])
    assert T.dispatch("lts_find", {"corpus": "a1", "prefix": "A"}, tools=eng3).get("reason") == "registry-not-found"


def then_mode_for(eng, st, kernel, err):
    from factlattice_engine import server
    real = eng.register_corpus

    def run(*a, **k):
        out = real(*a, **k)
        server._enter_registry_error(eng, st, kernel, err)
        return out
    return run


def _first_root(tmp_path):
    d = tmp_path / "regroot"
    d.mkdir(exist_ok=True)
    (d / "r.lts").write_text('---\nlts: "1.1"\ntitle: t\ndomain: x.zz\ndoctype: SPEC\n---\n\n# R [ZZ-ROOT]\n\n- summary: r\n', encoding="utf-8")
    return str(d)


_AUDIT_ACTIVE: list = []                     # the events of the window being watched (empty list = no window)
_AUDIT_FS_PREFIXES = ("open", "os.", "shutil.", "glob.", "pathlib.", "tempfile.", "socket.", "subprocess.", "io.")
_AUDIT_INSTALLED = []


def _audit_hook(event, args):
    if _AUDIT_ACTIVE and event.startswith(_AUDIT_FS_PREFIXES):
        _AUDIT_ACTIVE[0].append((event, tuple(str(a)[:80] for a in args)[:2]))


@contextlib.contextmanager
def _fs_window():
    """Watch a window with `sys.addaudithook` (a hook cannot be removed, so it is installed once and listens only while a window is open) AND wrap the calls that emit NO audit
    event, by name: `os.stat`, `os.lstat`, `os.access`, `os.readlink`, `os.statvfs`, and `os.path.realpath/isdir/isfile/exists/islink`. Yields the list of what was seen."""
    import os as _os
    if not _AUDIT_INSTALLED:
        sys.addaudithook(_audit_hook)
        _AUDIT_INSTALLED.append(1)
    seen: list = []
    unaudited = [(_os, n) for n in ("stat", "lstat", "access", "readlink", "statvfs", "pathconf")] + [(_os.path, n) for n in ("realpath", "isdir", "isfile", "exists", "islink")]
    real = [(m, n, getattr(m, n)) for m, n in unaudited]
    for m, n, f in real:
        setattr(m, n, lambda *a, _n=n, _f=f, **k: (seen.append(("unaudited:" + _n, str(a[:1])[:80])), _f(*a, **k))[1])
    _AUDIT_ACTIVE.append(seen)
    try:
        yield seen
    finally:
        _AUDIT_ACTIVE.clear()
        for m, n, f in real:
            setattr(m, n, f)


def test_declare_makes_no_call_under_a_registered_root_and_one_process_level_call(tmp_path, monkeypatch):
    """P3-4 / F2, stated in x-boot-window's terms. `declare()` makes NO file-system call: it runs under `sys.addaudithook` and the test fails on ANY audit event of the file-system
    families (`open` from builtin `open()`, `io.open`, `Path.read_text`, `os.open`; `os.listdir`, `os.scandir`, `os.mkdir`, `os.utime`, ...), not on a list of names. Python emits NO
    audit event for `os.stat`, `os.lstat`, `os.access`, `os.readlink`, `os.statvfs` and the `os.path` predicates built on them, so THOSE (and only those) are covered by name,
    wrapped for the window. The one process-level call allowed is `os.getcwd`, which `os.path.abspath` makes for a relative or blank root (no audit event; recorded, asserted for
    exactly the relative and blank roots). A planted `open`, `io.open`, `Path.read_text`, `os.listdir`, `os.statvfs` and `os.pathconf` are each caught by the window (the controls). It runs with a REAL kernel (so `declare_roots` is inside). What the window does NOT see: a name bound BEFORE the window (`from os import stat as s`), a read done once per process and memoised (the warm-up `declare()` before the window absorbs it), the background probe `declare_untrusted` submits for a key-failed entry (its realpath runs on a pool worker by design, so no key-failed entry is run here), and any call that emits no audit event and is not in the by-name list."""
    import os as _os

    from factlattice_engine import bootload
    from factlattice_engine import store as S
    w = _world6(tmp_path, monkeypatch)
    real_cwd = _os.getcwd
    asked = []

    from factlattice_engine.kernel import FreshnessKernel

    def run(entry):
        # a REAL kernel, so `kernel.declare_roots` (the pending slots) is inside the window too. A key-failed entry would also call `declare_untrusted`, which submits a
        # background probe whose realpath runs on a pool WORKER by design; none of the entries here is key-failed (that probe is outside this claim).
        kernel = FreshnessKernel(root_deadline_s=5)
        loader = bootload.StoreLoader(S.LtsStore(), {"zz": entry, "a1": w.registry["a1"]}, kernel=kernel, retry=False, workers=1)
        try:
            loader.declare()                                       # warm: lazy imports (their `open` events) happen outside the window
        except Exception:  # noqa: BLE001
            pass
        loader = bootload.StoreLoader(S.LtsStore(), {"zz": entry, "a1": w.registry["a1"]}, kernel=kernel, retry=False, workers=1)
        _os.getcwd = lambda: (asked.append(entry), real_cwd())[1]
        try:
            with _fs_window() as seen:
                try:
                    loader.declare()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            _os.getcwd = real_cwd
        return seen
    absolute = {"root": str(w.root("b1"))}
    relative = [{"root": "relative/dir"}, {"root": "  "}, {"roots": [{"path": "also/relative"}]}]
    for entry in [absolute] + relative:
        assert run(entry) == [], (entry, run(entry))
    assert all(e in relative for e in asked) and {json.dumps(e) for e in asked} >= {json.dumps(e) for e in relative}, asked
    # the controls: each of these, done inside the window, is caught
    f = w.root("b1") / "b1.lts"
    plants = {"builtin open": lambda: open(f).close(), "io.open": lambda: open(f).close(), "Path.read_text": lambda: Path(f).read_text(), "os.listdir": lambda: _os.listdir(w.root("b1")),
              "os.scandir": lambda: list(_os.scandir(w.root("b1"))), "os.statvfs": lambda: _os.statvfs(w.root("b1")), "os.access": lambda: _os.access(f, _os.R_OK),
              "os.stat": lambda: _os.stat(f), "os.path.isdir": lambda: _os.path.isdir(f), "os.pathconf": lambda: _os.pathconf(w.root("b1"), "PC_NAME_MAX")}
    for what, act in plants.items():
        with _fs_window() as seen:
            act()
        assert seen, ("the window must catch", what)


def test_a_first_pass_hook_that_raised_is_reported_by_the_listing_with_its_class_only(tmp_path, monkeypatch):
    """P3-5. The first-pass hook seats registry-error mode; a raise there leaves a server serving when the mode should have begun. It is logged, and now ALSO shown to the
    agent: `lts_list_corpora` carries `first_pass_error` (the class name, never the message) and ABSENT when the hook did not raise. Red on 60410b8 (nothing in the listing)."""
    from factlattice_engine import bootload
    from factlattice_engine import store as S
    from factlattice_engine.tools import LtsEngine
    w = _world6(tmp_path, monkeypatch)

    def hook(loader):
        raise RuntimeError("/secret/path/seat failed")
    st = S.LtsStore()
    loader = bootload.StoreLoader(st, {"a1": w.registry["a1"]}, kernel=None, retry=False, workers=1, on_first_pass=hook)
    eng = LtsEngine(st)
    eng.boot_loader = loader
    loader.declare()
    clean = eng.list_corpora(heal=False)
    assert "first_pass_error" not in clean
    loader.run_blocking()
    out = eng.list_corpora(heal=False)
    assert out.get("first_pass_error") == "RuntimeError" and "/secret" not in json.dumps(out), out
    assert "registry-error verdict may not have been reached" in out.get("first_pass_error_note", ""), out         # F4: a fixed sentence says what the class name means, and not more
    st2 = S.LtsStore()
    ok_loader = bootload.StoreLoader(st2, {"a1": w.registry["a1"]}, kernel=None, retry=False, workers=1, on_first_pass=lambda l: None)
    eng2 = LtsEngine(st2)
    eng2.boot_loader = ok_loader
    ok_loader.declare()
    ok_loader.run_blocking()
    ok_out = eng2.list_corpora(heal=False)
    assert "first_pass_error" not in ok_out and "first_pass_error_note" not in ok_out


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
# ROUND 17 (read of dfcde83)
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("arg,env,expected", [(None, None, "off"), (None, "1", "on_miss"), (None, "on", "on_miss"), (None, "true", "on_miss"), (None, "no", "off"),
                                              ("on_miss", None, "on_miss"), ("off", "1", "off"), ("bogus", None, "bogus")])
def test_the_effective_semantic_mode_is_decided_in_one_place(monkeypatch, arg, env, expected):
    """F1. `lts_find` and the post-call check ask ONE function for the mode (the argument, else `LTS_SEMANTIC_ON_MISS`)."""
    from factlattice_engine import tools as T
    monkeypatch.delenv("LTS_SEMANTIC_ON_MISS", raising=False)
    if env is not None:
        monkeypatch.setenv("LTS_SEMANTIC_ON_MISS", env)
    assert T.effective_semantic_mode(arg) == expected


@pytest.mark.parametrize("how", ["argument", "environment"])
def test_a_find_that_runs_on_miss_is_not_replaced_after_the_call_but_a_plain_find_is(tmp_path, monkeypatch, how):
    """F1. A `lts_find` whose EFFECTIVE semantic mode is `on_miss` (by argument or by environment), on a zero-match named corpus, takes the path that persists a derived vector
    index. If registry-error mode began as it ended, the reply must be what happened, not `registry-not-found`. STAGED with a STUB for the index write (the model weights are
    missing on this machine, so the real path cannot run): `_did_you_mean` writes a file under the state dir, as the real one persists its index. Control: the same find
    with semantic off IS replaced."""
    from factlattice.registry import RegistryError
    from factlattice_engine import server
    from factlattice_engine import tools as T
    st, eng = _registry_error_world(tmp_path, monkeypatch)
    kernel = type("K", (), {"unseat_partition": lambda self, cid: True})()
    err = RegistryError("boom", source="env", persist="boom")
    state = Path(os.environ["FACTLATTICE_STATE_DIR"]) if os.environ.get("FACTLATTICE_STATE_DIR") else tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    marker = state / "semantic-index.stub"

    def stub(self, corpus, text, **kw):
        marker.write_text("derived index written")                       # the persistence, stubbed
        server._enter_registry_error(eng, st, kernel, err)               # ... and the mode begins as the call ends
        return {"suggestions": []}
    monkeypatch.setattr(T.LtsEngine, "_did_you_mean", stub)
    monkeypatch.setattr(T.LtsEngine, "_preface", lambda self, blk: {})
    args = {"corpus": "a1", "text": "zzz-no-such-text"}
    if how == "argument":
        args["semantic"] = "on_miss"
    else:
        monkeypatch.setenv("LTS_SEMANTIC_ON_MISS", "1")
    out = T.dispatch("lts_find", args, tools=eng)
    assert marker.exists() and eng.registry_error is not None
    assert out.get("success") is True and out.get("reason") != "registry-not-found" and "did_you_mean_semantic" in out, out
    # control: semantic off, the same find, the mode beginning as it ends
    marker.unlink()
    st2, eng2 = _registry_error_world(tmp_path / "off", monkeypatch)
    monkeypatch.delenv("LTS_SEMANTIC_ON_MISS", raising=False)
    monkeypatch.setattr(eng2, "find", lambda *a, **k: (server._enter_registry_error(eng2, st2, kernel, err), {"success": True, "nodes": []})[1])
    assert T.dispatch("lts_find", {"corpus": "a1", "text": "zzz"}, tools=eng2).get("reason") == "registry-not-found"


def _tree_digest(*roots):
    import hashlib
    out = {}
    for r in roots:
        for p in sorted(Path(r).rglob("*")):
            if p.is_file():
                st = p.stat()
                out[str(p)] = (st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            elif p.is_dir():
                out[str(p) + "/"] = None
    return out


RECHECK_TOOL_ARGS = {
    "lts_find": {"corpus": "a1", "text": "A1"},
    "lts_node": {"corpus": "a1", "node_id": "A1-N"},
    "lts_traverse": {"corpus": "a1", "start_node": "A1-N"},
    "lts_capabilities": {"corpus": "a1"},
    "lts_reverse": {"corpus": "a1", "node_id": "A1-N"},
    "lts_history": {"corpus": "a1", "node_id": "A1-N"},
    "lts_aggregate": {"op": "count", "corpus": "a1"},
    "lts_profile_audit": {"corpus": "a1"},
    "lts_flow": {"corpus": "a1", "start_node": "A1-N"},
    "lts_find_in_prose": {"corpus": "a1", "text": "A1"},
}


def test_every_tool_of_the_post_call_recheck_set_writes_nothing_to_the_filesystem(tmp_path, monkeypatch):
    """F1. Membership of the set does not prove "writes nothing", so the filesystem is WATCHED: each tool of `LTS_POST_CALL_RECHECK_TOOL_NAMES` is run, with its default
    arguments (semantic off), over a tmp tree that holds the corpus, the state dir (miss log, ledger, indexes) and the backup dir, and not one file is created, removed or
    changed (size, mtime, sha256). The query log is switched off (it is the one write every tool makes by design). Every tool of the set needs an entry in
    `RECHECK_TOOL_ARGS`, so a NEW tool cannot join the set unwatched. The control is a planted writing tool, which the watcher catches."""
    from factlattice_engine import tools as T
    w = _world6(tmp_path, monkeypatch)
    from factlattice_engine import config
    st = config.build_store()
    eng = T.LtsEngine(st)
    # the WATCHED dirs are the engine's own: both are set in the environment and then RESOLVED through the engine's functions (`querylog.state_dir`, `edit.backup_dir`), so what
    # the tools would write to is what is watched (an env var the engine does not read would watch an empty directory)
    from factlattice.authoring import edit as _edit
    from factlattice_engine import querylog as _ql
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "watched-state"))
    monkeypatch.setenv("LTS_ENGINE_STATE_DIR", str(tmp_path / "watched-state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "watched-bak"))
    state = Path(_ql.state_dir())
    bak = Path(_edit.backup_dir())
    assert state == tmp_path / "watched-state" and bak == tmp_path / "watched-bak", (state, bak)
    state.mkdir(parents=True, exist_ok=True)
    bak.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", "off")
    monkeypatch.delenv("LTS_SEMANTIC_ON_MISS", raising=False)
    assert set(RECHECK_TOOL_ARGS) == set(T.LTS_POST_CALL_RECHECK_TOOL_NAMES), set(RECHECK_TOOL_ARGS) ^ set(T.LTS_POST_CALL_RECHECK_TOOL_NAMES)
    roots = [w.base, state, bak]
    for name in T.LTS_POST_CALL_RECHECK_TOOL_NAMES:
        before = _tree_digest(*roots)
        T.dispatch(name, dict(RECHECK_TOOL_ARGS[name]), tools=eng)
        after = _tree_digest(*roots)
        assert after == before, (name, sorted(set(after.items()) ^ set(before.items()))[:4])
    # the control: a tool of the set that writes a file under the state dir (planted into `lts_capabilities`) is caught by the same watcher, run the same way
    real_cap = T.LtsEngine.capabilities
    for what, target in (("state dir", lambda: Path(_ql.state_dir()) / "planted.jsonl"),
                         ("backup dir", lambda: Path(_edit.backup_dir()) / "planted.bak")):
        monkeypatch.setattr(T.LtsEngine, "capabilities", lambda self, *a, _t=target, **k: (Path(_t()).write_text("a record"), real_cap(self, *a, **k))[1])
        before = _tree_digest(*roots)
        T.dispatch("lts_capabilities", dict(RECHECK_TOOL_ARGS["lts_capabilities"]), tools=eng)
        assert _tree_digest(*roots) != before, ("the watcher must see a write to the engine's real " + what)
        Path(target()).unlink()


def test_a_register_that_passed_the_gate_before_the_mode_began_seats_nothing_inside_the_step(tmp_path, monkeypatch):
    """F6. The registry gate is passed, registry-error mode begins, and only then does the register's seat run: the seat refuses INSIDE its locked step (`StoreSealed`), the
    reply is the mode's refusal (`registered: false`) and the store holds no corpus. Without the check the corpus was left in the store of a server in registry-error mode
    (listed by `lts_list_corpora`). Reads were already refused by the gate. Red on dfcde83."""
    from factlattice.registry import RegistryError
    from factlattice_engine import server
    from factlattice_engine import store as S
    from factlattice_engine import tools as T
    st, eng = _registry_error_world(tmp_path, monkeypatch)
    kernel = type("K", (), {"unseat_partition": lambda self, cid: True})()
    err = RegistryError("boom", source="env", persist="boom")
    real_gate = T._registry_gate
    fired = []

    def gate_then_mode(t, name, a):
        verdict = real_gate(t, name, a)
        if not fired:
            fired.append(1)
            server._enter_registry_error(eng, st, kernel, err)
        return verdict
    monkeypatch.setattr(T, "_registry_gate", gate_then_mode)
    out = T.dispatch("lts_register_corpus", {"corpus_id": "zz", "spec": {"root": _first_root(tmp_path), "domain_suffix": "x.zz"}}, tools=eng)
    assert out.get("success") is False and out.get("reason") == "registry-not-found" and out.get("registered") is False, out
    assert st.corpus_ids() == [] and eng.list_corpora(heal=False)["corpora"] == []
    assert st._sealed is True
    with pytest.raises(S.StoreSealed):
        st.add_partition_clearing(_part6(_world6(tmp_path / "x", monkeypatch), "a1"))


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
# ROUND 18 (read of 336eae5)
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("semantic,env,corpus,path,text,result,expected", [
    ("on_miss", None, "a1", None, "zz", None, True),                               # named, text, on_miss, no result yet: it could
    ("on_miss", None, "a1", None, "zz", {"success": True, "total_matched": 0}, True),
    ("on_miss", None, "a1", None, "zz", {"success": True, "total_matched": 3}, False),      # matches: no miss path
    ("on_miss", None, "a1", None, "zz", {"success": False, "error": "x"}, False),           # failed
    ("on_miss", None, None, None, "zz", {"success": True, "total_matched": 0}, False),      # corpus-omitted: only reports `skipped`
    ("on_miss", None, "a1", "/some/file.lts", "zz", {"success": True, "total_matched": 0}, False),   # path= forces the mode off
    ("on_miss", None, "a1", None, "", {"success": True, "total_matched": 0}, False),        # no text
    ("on_miss", None, "a1", None, "  ", {"success": True, "total_matched": 0}, True),       # whitespace is text, as `lts_find`'s own guard reads it (F-3)
    ("off", "1", "a1", None, "zz", {"success": True, "total_matched": 0}, False),           # the argument beats the environment
    (None, "1", "a1", None, "zz", {"success": True, "total_matched": 0}, True),             # the environment decides when no argument
    (None, None, "a1", None, "zz", {"success": True, "total_matched": 0}, False),
])
def test_one_function_decides_whether_a_find_could_persist_a_derived_index(monkeypatch, semantic, env, corpus, path, text, result, expected):
    """F1-a / F8. `could_persist_derived_index` is the ONE decision: effective mode on_miss (a path= find is forced off), a named corpus, text, and zero matches."""
    from factlattice_engine import tools as T
    monkeypatch.delenv("LTS_SEMANTIC_ON_MISS", raising=False)
    if env is not None:
        monkeypatch.setenv("LTS_SEMANTIC_ON_MISS", env)
    assert T.could_persist_derived_index(semantic, corpus, path, text, result) is expected


@pytest.mark.parametrize("args,result", [
    ({"text": "zz", "semantic": "on_miss"}, {"success": True, "total_matched": 0, "nodes": []}),                      # corpus-omitted, on_miss
    ({"corpus": "a1", "text": "A1", "semantic": "on_miss"}, {"success": True, "total_matched": 3, "nodes": [1, 2, 3]}),     # named, matches
    ({"corpus": "a1", "path": "/x.lts", "text": "zz", "semantic": "on_miss"}, {"success": True, "total_matched": 0, "nodes": []}),   # path=
])
def test_a_find_that_could_not_have_persisted_an_index_is_rechecked_even_when_on_miss_is_on(tmp_path, monkeypatch, args, result):
    """F1-a. The skip is exactly the call that could persist (named corpus, text, zero matches, no path=). A corpus-omitted find, a named find with matches and a path= find, with
    on_miss on, are re-checked: registry-error mode beginning as they end answers `registry-not-found`, not an empty success. Red on 336eae5 (every on_miss find was skipped)."""
    from factlattice.registry import RegistryError
    from factlattice_engine import server
    from factlattice_engine import tools as T
    st, eng = _registry_error_world(tmp_path, monkeypatch)
    kernel = type("K", (), {"unseat_partition": lambda self, cid: True})()
    err = RegistryError("boom", source="env", persist="boom")
    monkeypatch.setattr(eng, "find", lambda *a, **k: (server._enter_registry_error(eng, st, kernel, err), dict(result))[1])
    monkeypatch.setenv("LTS_SEMANTIC_ON_MISS", "1")
    out = T.dispatch("lts_find", dict(args), tools=eng)
    assert out.get("reason") == "registry-not-found", out


def _real_kernel_engine(tmp_path, monkeypatch):
    from factlattice_engine import store as S
    from factlattice_engine.kernel import FreshnessKernel
    from factlattice_engine.tools import LtsEngine
    w = _world6(tmp_path, monkeypatch)
    st = S.LtsStore()
    st.add_partition(_part6(w, "a1"))
    kernel = FreshnessKernel(root_deadline_s=5)
    st.kernel = kernel
    return st, kernel, LtsEngine(st)


@pytest.mark.parametrize("when", ["before the kernel's seat", "while the kernel reads the roots"])
def test_a_register_across_the_mode_change_seats_nothing_in_the_store_or_the_real_kernel(tmp_path, monkeypatch, when):
    """F6-a, on the REAL FreshnessKernel. The store's seat has succeeded; registry-error mode begins (a) before the kernel's seat, or (b) while the kernel reads the roots (a
    pause planted in the roots' wait). The kernel refuses inside its step (`KernelSealed`), the store's seat is undone, the reply is the mode's refusal (`registered: false`), and
    neither the store nor the kernel holds the corpus. Red on 336eae5 (the kernel served it and the reply said success)."""
    from factlattice.registry import RegistryError
    from factlattice_engine import server
    from factlattice_engine import tools as T
    st, kernel, eng = _real_kernel_engine(tmp_path, monkeypatch)
    err = RegistryError("boom", source="env", persist="boom")
    fired = []

    def mode():
        if not fired:
            fired.append(1)
            server._enter_registry_error(eng, st, kernel, err)
    if when == "before the kernel's seat":
        real_seat = kernel.seat_partition
        monkeypatch.setattr(kernel, "seat_partition", lambda part: (mode(), real_seat(part))[1])
    else:
        real_wait = kernel.roots.wait
        monkeypatch.setattr(kernel.roots, "wait", lambda *a, **k: (mode(), real_wait(*a, **k))[1])
    out = T.dispatch("lts_register_corpus", {"corpus_id": "zz", "spec": {"root": _first_root(tmp_path), "domain_suffix": "x.zz"}}, tools=eng)
    assert fired and eng.registry_error is not None
    assert out.get("success") is False and out.get("reason") == "registry-not-found" and out.get("registered") is False, out
    assert st.corpus_ids() == [] and "zz" not in kernel._parts and "zz" not in kernel._states and "zz" not in kernel._specs


def test_a_sealed_store_restores_no_record(tmp_path, monkeypatch):
    """F6-b. `remove_partition_restoring` removes the partition but puts no failed or no-roots record back into a sealed store (registry-error mode serves and lists nothing)."""
    from factlattice_engine import store as S
    st = S.LtsStore()
    st.clear_all()
    st.remove_partition_restoring("zz", {"code": "corpus-file-unreadable", "path": None, "private": False}, {"spec": {}, "private": False})
    assert st.snapshot_all()[2:] == ({}, {}) and st.unlocated() == frozenset()
    open_store = S.LtsStore()
    open_store.remove_partition_restoring("zz", {"code": "corpus-file-unreadable", "path": None, "private": False}, None)
    assert "zz" in open_store.snapshot_all()[2], "control: an unsealed store restores"


SEATING_METHODS_CHECK_SEALED = {
    "LtsStore": {"add_partition", "add_partition_clearing", "declare_pending", "settle", "remove_partition_restoring", "fail", "move_failed_to_pending", "add_unlocated"},
    "FreshnessKernel": {"seat_partition"},
}
SEATING_METHODS_EXEMPT = {
    "LtsStore": {
        "clear_all": "it SETS the seal", "clear_no_roots": "it removes records", "forget_pending": "it removes a record", "clear_unlocated": "it removes a flag",
        "forget_unlocated": "it removes records", "set_order": "it holds the registry's order, not a corpus; clear_all empties it", "remove_partition": "it removes",
    },
    "FreshnessKernel": {
        "seat_store": "it calls seat_partition, which checks", "declare_roots": "pending SLOTS for roots, no corpus is served by them",
        "declare_untrusted": "probe slots for strings, no corpus is served by them", "add_tick_hook": "a callback, not state about corpora",
    },
}


def _seating_methods(source: str, cls: str):
    import ast
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == cls)
    out = {}
    for m in node.body:
        if isinstance(m, ast.FunctionDef) and not m.name.startswith("_") and m.name.split("_")[0] in ("add", "declare", "seat", "settle", "fail", "move", "restore", "remove", "set", "forget", "clear"):
            out[m.name] = "_sealed" in ast.get_source_segment(source, m)
    return out


def test_every_seating_or_restoring_method_checks_the_seal_or_is_listed_exempt():
    """F6-b / SDLC rule. The store's and the kernel's methods that SEAT, DECLARE, SETTLE, FAIL, MOVE or RESTORE state (public, named add/declare/seat/settle/fail/move/restore/
    remove/set/forget/clear) are LISTED: each either mentions `_sealed` in its own body (and is in SEATING_METHODS_CHECK_SEALED) or is in SEATING_METHODS_EXEMPT with its reason.
    A NEW method with such a name fails here until it is classified. A lexical tripwire for those name prefixes, not a proof. What it does NOT see (the cold read's plants): a method
    with another prefix (`publish_`, `mark_`, `put_`, `rebind_`, ...) or a public mutator outside the list (`unseat_partition`, `rebind_partition`); `_sealed` mentioned only in a COMMENT
    or only AFTER the write (any mention anywhere in the body counts); and whether the check is the right one."""
    root = Path(__file__).resolve().parent.parent
    for cls, rel in (("LtsStore", "factlattice_engine/store.py"), ("FreshnessKernel", "factlattice_engine/kernel.py")):
        src = (root / rel).read_text(encoding="utf-8")
        found = _seating_methods(src, cls)
        checked, exempt = SEATING_METHODS_CHECK_SEALED[cls], SEATING_METHODS_EXEMPT[cls]
        assert set(found) == checked | set(exempt), (cls, sorted(set(found) ^ (checked | set(exempt))))
        assert all(found[m] for m in checked), (cls, [m for m in checked if not found[m]])
    planted = "class LtsStore:\n    def add_new_thing(self):\n        pass\n    def settle(self):\n        if self._sealed:\n            return\n"
    assert _seating_methods(planted, "LtsStore") == {"add_new_thing": False, "settle": True}


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
# ROUND 19 (read of 25c5e19)
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def test_a_named_on_miss_find_with_whitespace_text_still_reaches_the_suggestion_path_with_its_corpus(tmp_path, monkeypatch):
    """F-3. The predicate and the persisting code agree on blank text: a find that NAMES a corpus and has whitespace-only text hands the corpus to the suggestion path (as the
    tree before round 18 did), not `None` (which answered 'suggestions need a named corpus' although one was named). Red on 25c5e19."""
    from factlattice_engine import tools as T
    st, eng = _registry_error_world(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(T.LtsEngine, "_did_you_mean", lambda self, corpus, text, **kw: (seen.append(corpus), {"status": "ok", "suggestions": []})[1])
    monkeypatch.setattr(T.LtsEngine, "_preface", lambda self, blk: {})
    T.dispatch("lts_find", {"corpus": "a1", "text": "   ", "semantic": "on_miss"}, tools=eng)
    assert seen == ["a1"], seen


def _real_kernel_with_a1_seated(tmp_path, monkeypatch):
    """A store serving `a1` and a REAL kernel with `a1`'s root seated; the tests then seat a part (a re-seat of `a1`, or the new `b1`) whose root read is interrupted by registry-error mode (a pause in the roots' wait runs the mode's one step)."""
    from factlattice.registry import RegistryError
    from factlattice_engine import server
    from factlattice_engine import store as S
    from factlattice_engine.kernel import FreshnessKernel, KernelSealed
    from factlattice_engine.tools import LtsEngine
    w = _world6(tmp_path, monkeypatch)
    st = S.LtsStore()
    part_a = _part6(w, "a1")
    st.add_partition(part_a)
    kernel = FreshnessKernel(root_deadline_s=5)
    st.kernel = kernel
    kernel.seat_partition(part_a)
    eng = LtsEngine(st)
    return w, st, kernel, eng, part_a, RegistryError, server, KernelSealed


def test_a_refused_reseat_leaves_the_real_kernel_holding_nothing_for_the_corpus(tmp_path, monkeypatch):
    """F-1. A RE-seat of a served corpus (the heal's structural change) whose commit lands after registry-error mode began: the mode's unseat has already dropped the corpus, and the
    refused seat must not put its OLD specs back. Once sealed the kernel holds NOTHING for that corpus: no part, state, root hash, spec, wanted pin, users entry, observation
    or probe slot, and `tick()` makes no observation of its root. Staged on the REAL kernel. Red on 25c5e19 (`_specs` and `_wanted` kept the corpus; `tick()` probed its root)."""
    w, st, kernel, eng, part_a, RegistryError, server, KernelSealed = _real_kernel_with_a1_seated(tmp_path, monkeypatch)
    err = RegistryError("boom", source="env", persist="boom")
    root = os.path.abspath(str(w.root("a1")))
    fired = []
    real_wait = kernel.roots.wait

    def wait(*a, **k):
        if not fired:
            fired.append(1)
            server._enter_registry_error(eng, st, kernel, err)         # the mode's one step, including the unseat of the served corpus
        return real_wait(*a, **k)
    monkeypatch.setattr(kernel.roots, "wait", wait)
    with pytest.raises(KernelSealed):
        kernel.seat_partition(part_a)                                   # the re-seat
    assert fired
    for table in ("_parts", "_states", "_roots", "_specs"):
        assert "a1" not in getattr(kernel, table), table
    assert root not in kernel._wanted and root not in kernel._users and root not in kernel._obs, (kernel._wanted, kernel._users)
    assert root not in kernel.roots.root_paths()
    seen = []
    real_observe = kernel._observe
    kernel._observe = lambda path, wants: (seen.append(path), real_observe(path, wants))[1]
    kernel.tick()
    assert root not in [os.path.abspath(p) for p in seen], seen


def test_a_refused_fresh_seat_leaves_no_idle_probe_slot_in_the_real_kernel(tmp_path, monkeypatch):
    """F-6. The loader's shape: a part the store never held (so the mode's unseat never names it) is seated into a kernel that the mode seals while the roots are read. The refusal
    drops the probe slot the seat created for the NEW root (nothing else uses it); the slot of the corpus already seated is the mode's to drop and is dropped by its unseat."""
    w, st, kernel, eng, part_a, RegistryError, server, KernelSealed = _real_kernel_with_a1_seated(tmp_path, monkeypatch)
    err = RegistryError("boom", source="env", persist="boom")
    new = _part6(w, "b1")
    new_root = os.path.abspath(str(w.root("b1")))
    fired = []
    real_wait = kernel.roots.wait

    def wait(*a, **k):
        if not fired:
            fired.append(1)
            server._enter_registry_error(eng, st, kernel, err)
        return real_wait(*a, **k)
    monkeypatch.setattr(kernel.roots, "wait", wait)
    with pytest.raises(KernelSealed):
        kernel.seat_partition(new)
    assert fired and new_root not in kernel.roots.root_paths() and "b1" not in kernel._parts and "b1" not in kernel._specs
    assert kernel.roots.root_paths() == [] or all(os.path.abspath(p) != new_root for p in kernel.roots.root_paths())


def test_the_also_step_changes_everything_or_nothing(tmp_path, monkeypatch):
    """F-2. If the engine cannot seat the error, the kernel is NOT sealed and the store is untouched (it runs `also` before it empties itself); if the seal itself fails, the
    engine's state is put back. In neither case does any reply claim registry-error mode. Red on 25c5e19 (the kernel was sealed first)."""
    from factlattice.registry import RegistryError
    from factlattice_engine import server
    w, st, kernel, eng, part_a, _RE, _server, _KS = _real_kernel_with_a1_seated(tmp_path, monkeypatch)
    err = RegistryError("boom", source="env", persist="boom")
    # (1) seating the error raises
    monkeypatch.setattr(eng, "seat_registry_error", lambda e: (_ for _ in ()).throw(RuntimeError("seat failed")))
    with pytest.raises(RuntimeError):
        server._enter_registry_error(eng, st, kernel, err)
    assert kernel._sealed is False and st._sealed is False and eng.registry_error is None and st.corpus_ids() == ["a1"]
    # (2) the seal raises after the error was seated: the engine is put back
    monkeypatch.undo()
    w, st, kernel, eng, part_a, *_ = _real_kernel_with_a1_seated(tmp_path / "two", monkeypatch)
    monkeypatch.setattr(kernel, "seal", lambda: (_ for _ in ()).throw(RuntimeError("seal failed")))
    boot_before = eng.registry_boot
    with pytest.raises(RuntimeError):
        server._enter_registry_error(eng, st, kernel, err)
    assert eng.registry_error is None and eng.registry_boot == boot_before and st._sealed is False and st.corpus_ids() == ["a1"]


def test_a_first_pass_hook_that_cannot_seat_the_error_is_the_first_pass_error_and_no_one_is_sealed(tmp_path, monkeypatch):
    """F-2, through the server's own first pass. A registry whose second entry only the loader meets makes `after_first_pass` run `_enter_registry_error`; the error cannot be
    seated, so the hook raises, the first-pass error is recorded and shown by the listing, and the kernel and the store are not sealed (the server serves, and says so)."""
    from factlattice_engine import server, tools
    w = _world6(tmp_path, monkeypatch)
    reg = json.loads(w.registry_path.read_text())
    reg = {"a1": reg["a1"], "zz-bad": {"roots": [{"path": str(w.root("b1")), "only_file": 1}], "domain_suffix": "x.bad"}}
    w.registry_path.write_text(json.dumps(reg))
    monkeypatch.setattr(tools.LtsEngine, "seat_registry_error", lambda self, e: (_ for _ in ()).throw(RuntimeError("seat failed")))
    eng = server.build_tools(watch=False, async_boot=True)
    assert eng.boot_loader.first_pass_done.wait(30)
    out = eng.list_corpora(heal=False)
    assert out.get("first_pass_error") == "RuntimeError", out
    assert eng.store.kernel._sealed is False and eng.store._sealed is False and eng.registry_error is None
    assert "a1" in eng.store.corpus_ids()


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
# ROUND 20 (read of 1806c83)
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def _wait_slot_gone(kernel, root, timeout=10.0):
    """`RootRegistry.drop` keeps a slot whose job is still in flight and removes it when the job ends: wait for that (bounded) before reading the slot table."""
    end = time.monotonic() + timeout
    while time.monotonic() < end and root in kernel.roots.root_paths():
        time.sleep(0.02)


def _kernel_residue(kernel, root):
    seen = []
    real_observe = kernel._observe
    kernel._observe = lambda path, wants: (seen.append(path), real_observe(path, wants))[1]
    kernel.tick()
    return {"tables": [t for t in ("_parts", "_states", "_roots", "_specs") if "a1" in getattr(kernel, t)],
            "pinned": [t for t in ("_wanted", "_users", "_obs") if root in getattr(kernel, t)],
            "slot": root in kernel.roots.root_paths(), "probed": root in [os.path.abspath(p) for p in seen]}


@pytest.mark.parametrize("how", ["an unreadable member", "a root that did not answer"])
def test_a_FAILED_reseat_after_the_mode_began_leaves_the_real_kernel_holding_nothing(tmp_path, monkeypatch, how):
    """F-1b. The sibling of the refused commit: a re-seat that FAILS (a member that turns unreadable, or `RootNotAnswering`) while registry-error mode begins used to put the old
    specs back after the mode's unseat. Every exit of `seat_partition` goes through ONE helper (`_abandon_seat_locked`): once sealed the kernel holds nothing for the corpus. Staged on
    the REAL kernel. Red on 1806c83 (`_specs` and `_wanted` kept the corpus; `tick()` probed its root)."""
    from factlattice.registry import RegistryError
    from factlattice_engine import server
    from factlattice_engine.rootstate import RootNotAnswering
    w, st, kernel, eng, part_a, _RE, _srv, _KS = _real_kernel_with_a1_seated(tmp_path, monkeypatch)
    err = RegistryError("boom", source="env", persist="boom")
    root = os.path.abspath(str(w.root("a1")))
    fired = []

    def mode():
        if not fired:
            fired.append(1)
            server._enter_registry_error(eng, st, kernel, err)
    if how == "an unreadable member":
        real_reload = kernel._reload_root_full
        monkeypatch.setattr(kernel, "_reload_root_full", lambda stt: (mode(), (_ for _ in ()).throw(PermissionError("unreadable")))[1])
        expect = Exception
    else:
        def wait(*a, **k):
            mode()
            raise RootNotAnswering("did not answer")
        monkeypatch.setattr(kernel.roots, "wait", wait)
        expect = RootNotAnswering
    with pytest.raises(expect):
        kernel.seat_partition(part_a)
    assert fired
    _wait_slot_gone(kernel, root)
    assert _kernel_residue(kernel, root) == {"tables": [], "pinned": [], "slot": False, "probed": False}


def test_an_untrusted_string_keeps_its_slot_when_a_corpus_over_the_same_root_is_unseated(tmp_path, monkeypatch):
    """F-3b. `unseat_partition` drops the probe slot of a root nothing uses, but spares a root the registry still names as part of a failed entry (`_untrusted`): the slot stays
    while the entry names it, and goes when `retain_untrusted` gives the string back. Red when the `| set(self._untrusted)` sparing is reverted."""
    w, st, kernel, eng, part_a, *_ = _real_kernel_with_a1_seated(tmp_path, monkeypatch)
    root = os.path.abspath(str(w.root("a1")))
    kernel.declare_untrusted([root])
    end = time.monotonic() + 10
    while time.monotonic() < end and (kernel._real.get(root) is None or kernel.roots.slot(root, create=False).busy()):
        time.sleep(0.02)                                    # the string's own probe has ended: an idle slot, so only the sparing keeps it
    assert kernel.unseat_partition("a1") is True
    assert root in kernel.roots.root_paths(), "the failed entry still names this root: its probe slot stays"
    kernel.retain_untrusted([])
    _wait_slot_gone(kernel, root)
    assert root not in kernel.roots.root_paths(), "given back: the slot goes"


def test_registry_error_mode_retires_the_untrusted_strings(tmp_path, monkeypatch):
    """F-4c. A server in registry-error mode serves nothing, so the untrusted strings stop being probed: the mode's step (`kernel.seal`) retires them and drops their idle
    slots; `tick()` observes none of them. Red on 1806c83."""
    from factlattice.registry import RegistryError
    from factlattice_engine import server
    w, st, kernel, eng, part_a, *_ = _real_kernel_with_a1_seated(tmp_path, monkeypatch)
    d = tmp_path / "typo"
    d.mkdir()
    kernel.declare_untrusted([str(d)])
    end = time.monotonic() + 10
    while time.monotonic() < end and not any(f.untrusted and f.state == "ok" for f in kernel.root_facts()):
        time.sleep(0.02)
    server._enter_registry_error(eng, st, kernel, RegistryError("boom", source="env", persist="boom"))
    _wait_slot_gone(kernel, os.path.abspath(str(d)))
    assert kernel.untrusted_paths() == set() and os.path.abspath(str(d)) not in kernel.roots.root_paths()
    seen = []
    real_observe = kernel._observe
    kernel._observe = lambda path, wants: (seen.append(path), real_observe(path, wants))[1]
    kernel.tick()
    assert os.path.abspath(str(d)) not in [os.path.abspath(p) for p in seen]


def test_seat_registry_error_builds_its_state_completely_and_assigns_it_in_one_step(tmp_path, monkeypatch):
    """F-2a. The engine's registry state is one tuple assigned in ONE statement. A fault while the new state is BUILT (here the error's `path` raises on access, after `source`
    was read) leaves the old state whole, so `lts_list_corpora` never finds a boot dict without `sha256_at_boot`. Control: a normal error seats all three at once. Red on 1806c83
    when the state was three assignments (`registry_boot` could be left as the error dict with `registry_error` None). The single-field setters are gone altogether
    (`test_the_engines_registry_state_can_only_be_assigned_whole`), so the "fault between pieces" staging that needed one is deleted: it could no longer fail for the reason it named."""
    from factlattice.registry import RegistryError
    st, eng = _registry_error_world(tmp_path, monkeypatch)
    before = eng._reg_state

    class Faulty(RegistryError):
        armed = False

        @property
        def path(self):
            if self.armed:
                raise RuntimeError("fault while building the state")
            return self._p

        @path.setter
        def path(self, v):
            self._p = v
    bad = Faulty("boom", source="env", persist="boom")
    bad.armed = True
    with pytest.raises(RuntimeError):
        eng.seat_registry_error(bad)
    assert eng._reg_state is before and eng.registry_error is None
    assert "sha256_at_boot" in eng.list_corpora(heal=False)["registry_choice"]
    eng.seat_registry_error(RegistryError("boom", source="env", persist="boom"))
    assert eng.registry_error is not None and eng.registry_boot["error"].startswith("registry-not-found") and eng.registry_ids == frozenset()



def test_a_fault_while_unseating_after_the_mode_began_is_logged_and_the_mode_stays_entered(tmp_path, monkeypatch, caplog):
    """F-2b. "Everything or nothing" covers a raise in seating the error and in the seal's computation, and ends at `clear_all`. The unseat loop that follows is memory-only; a fault in it is logged (class only) and does NOT
    stop the loop or the first pass, so the listing does not claim the verdict "may not have been reached" for a mode that was entered. Red on 1806c83 (the loop raised into the hook)."""
    import logging

    from factlattice.registry import RegistryError
    from factlattice_engine import server
    w, st, kernel, eng, part_a, *_ = _real_kernel_with_a1_seated(tmp_path, monkeypatch)
    st.add_partition(_part6(w, "b1"))
    calls = []
    real = kernel.unseat_partition
    monkeypatch.setattr(kernel, "unseat_partition", lambda cid, *a, **k: (calls.append(cid), (_ for _ in ()).throw(RuntimeError("/secret/unseat failed")) if len(calls) == 1 else real(cid, *a, **k))[1])
    with caplog.at_level(logging.ERROR, logger="factlattice_engine.server"):
        server._enter_registry_error(eng, st, kernel, RegistryError("boom", source="env", persist="boom"))
    assert len(calls) == 2, "the loop went on past the fault"
    assert eng.registry_error is not None and st._sealed and kernel._sealed
    msgs = [r.getMessage() for r in caplog.records if "unseating a corpus" in r.getMessage()]
    assert msgs and "RuntimeError" in msgs[0] and "/secret" not in msgs[0], msgs


# ---------------------------------------------------------------------------------------------------------------------------------------------------------
# ROUND 21 (read of f2eff72)
# ---------------------------------------------------------------------------------------------------------------------------------------------------------
def test_a_seal_whose_computation_raises_changes_nothing_in_the_kernel_the_engine_or_the_store(tmp_path, monkeypatch):
    """R21-1. `seal()` COMPUTES what retiring the untrusted strings would drop first (`_untrusted_retirement`, which changes no state), then in ONE locked step assigns the retired
    set AND sets `_sealed`. A raise in the computation therefore changes NOTHING: the kernel is unsealed, the untrusted strings and their kept real paths are exactly what they were
    (the failed-entry guard reads them through `root_facts`), the engine's state is put back and the store is untouched. Only the computation is stubbed (the real `seal` runs).
    Red on 50c96ca, whose `seal` retired the strings first, so a raise afterwards left a server that stayed up without them."""
    from factlattice.registry import RegistryError
    from factlattice_engine import server
    w, st, kernel, eng, part_a, *_ = _real_kernel_with_a1_seated(tmp_path, monkeypatch)
    d = tmp_path / "typo"
    d.mkdir()
    kernel.declare_untrusted([str(d)])
    key = os.path.abspath(str(d))
    end = time.monotonic() + 10
    while time.monotonic() < end and kernel.root_real(key) is None:
        time.sleep(0.02)
    real_before = kernel.root_real(key)
    assert real_before is not None
    untrusted_before = kernel.untrusted_paths()
    monkeypatch.setattr(kernel, "_untrusted_retirement", lambda: (_ for _ in ()).throw(RuntimeError("computation failed")))
    before = eng._reg_state
    with pytest.raises(RuntimeError):
        server._enter_registry_error(eng, st, kernel, RegistryError("boom", source="env", persist="boom"))
    assert kernel._sealed is False and st._sealed is False and eng._reg_state is before and eng.registry_error is None and st.corpus_ids() == ["a1"]
    assert kernel.untrusted_paths() == untrusted_before and kernel.root_real(key) == real_before
    assert any(f.untrusted and f.real == real_before for f in kernel.root_facts()), "the failed-entry guard still sees the kept real path"


@pytest.mark.parametrize("exc_class", [ValueError, SystemExit])
@pytest.mark.parametrize("sealed", [True, False])
def test_every_exception_out_of_a_seat_takes_the_one_exit(tmp_path, monkeypatch, sealed, exc_class):
    """R21-2. An exception out of the seat's work takes `_abandon_seat_locked` and is re-raised AS IT IS, whatever its class: a `ValueError`, and a `BaseException` that is not an
    Exception (`SystemExit`: a job records it as its error and the seat re-raises it; on a loader thread it ends that thread only, SDLC `Injected-fault limits`; an in-process caller of the library that
    catches it survives, so the kernel is cleaned for it too). The one exception to "as it is" is an OSError out of a root job, which becomes `CorpusFileError` (corpus-file-unreadable): tested below.
    Unsealed FRESH seat: nothing is left half-seated (where the exit is observable). Unsealed RE-seat: keeps its old state. SEALED leg: the mode's unseat has already emptied the
    kernel before the exception, so this leg CANNOT fail on the tree before the one exit existed (it fails on f2eff72 only for the residue the mode itself leaves, which is none); it
    is kept as a regression guard for the sealed state, and the unsealed fresh-seat leg is the proof. Staged on the real kernel."""
    from factlattice.registry import RegistryError
    from factlattice_engine import server
    w, st, kernel, eng, part_a, *_ = _real_kernel_with_a1_seated(tmp_path, monkeypatch)
    root = os.path.abspath(str(w.root("a1")))
    old_specs = list(kernel._specs["a1"])
    fired = []

    def reload(stt):
        if sealed and not fired:
            fired.append(1)
            server._enter_registry_error(eng, st, kernel, RegistryError("boom", source="env", persist="boom"))
        raise exc_class("a job's own error")
    monkeypatch.setattr(kernel, "_reload_root_full", reload)
    with pytest.raises(exc_class):
        kernel.seat_partition(part_a)
    if sealed:
        _wait_slot_gone(kernel, root)
        assert _kernel_residue(kernel, root) == {"tables": [], "pinned": [], "slot": False, "probed": False}
    else:
        assert kernel._specs["a1"] == old_specs and "a1" in kernel._parts and "a1" in kernel._states, "a re-seat that fails keeps its old state"
        fresh = _part6(w, "b1")
        with pytest.raises(exc_class):
            kernel.seat_partition(fresh)
        assert "b1" not in kernel._parts and "b1" not in kernel._specs and "b1" not in kernel._states


def test_an_oserror_out_of_a_seat_becomes_a_coded_corpus_file_error_and_the_kernel_is_clean(tmp_path, monkeypatch):
    """R21-2(c). The exception to "re-raised as it is": an OSError out of a job (an unreadable member) leaves as `CorpusFileError` with the code `corpus-file-unreadable`
    (the original is dropped, `from None`). The kernel holds nothing half-seated."""
    w, st, kernel, eng, part_a, *_ = _real_kernel_with_a1_seated(tmp_path, monkeypatch)
    monkeypatch.setattr(kernel, "_reload_root_full", lambda stt: (_ for _ in ()).throw(PermissionError(13, "denied")))
    fresh = _part6(w, "b1")
    with pytest.raises(Exception) as ei:
        kernel.seat_partition(fresh)
    assert type(ei.value).__name__ == "CorpusFileError" and getattr(ei.value, "code", None) == "corpus-file-unreadable", ei.value
    assert "b1" not in kernel._parts and "b1" not in kernel._specs and "b1" not in kernel._states


def test_a_fault_before_the_roots_are_read_or_in_the_publish_step_takes_the_one_exit_too(tmp_path, monkeypatch):
    """R21-2(b). The one exit also covers a fault BEFORE the try (`_specs_of`, which builds the root specs) and in the publish step (`combine_roots`, computed before anything is
    assigned): a FRESH seat that faults there leaves nothing half-seated. Red on 50c96ca (both left `_parts` set; the second also `_specs` and `_states`)."""
    from factlattice_engine import kernel as K
    w, st, kernel, eng, part_a, *_ = _real_kernel_with_a1_seated(tmp_path, monkeypatch)
    fresh = _part6(w, "b1")
    monkeypatch.setattr(K.FreshnessKernel, "_specs_of", staticmethod(lambda part: (_ for _ in ()).throw(RuntimeError("specs_of failed"))))
    with pytest.raises(RuntimeError):
        kernel.seat_partition(fresh)
    assert "b1" not in kernel._parts and "b1" not in kernel._specs and "b1" not in kernel._states
    monkeypatch.undo()
    w, st, kernel, eng, part_a, *_ = _real_kernel_with_a1_seated(tmp_path / "two", monkeypatch)
    fresh = _part6(w, "b1")
    monkeypatch.setattr(K, "combine_roots", lambda per_root: (_ for _ in ()).throw(RuntimeError("combine failed")))
    with pytest.raises(RuntimeError):
        kernel.seat_partition(fresh)
    assert "b1" not in kernel._parts and "b1" not in kernel._specs and "b1" not in kernel._states and "b1" not in kernel._roots


def test_the_engines_registry_state_can_only_be_assigned_whole():
    """R20-4. `registry_boot`, `registry_ids` and `registry_error` are read-only properties over `_reg_state`: there is no single-field setter, so no code can leave the state half
    assigned. Red on f2eff72 (three setters)."""
    from factlattice_engine.tools import LtsEngine
    eng = LtsEngine(__import__("factlattice_engine.store", fromlist=["LtsStore"]).LtsStore())
    for name in ("registry_boot", "registry_ids", "registry_error"):
        with pytest.raises(AttributeError):
            setattr(eng, name, None)


_UNSEAT_LIMIT = ("A CORPUS WHOSE UNSEAT FAULTED MAY BE LEFT WITH PARTIAL BOOKKEEPING IN THE SEALED KERNEL (its tables may or may not still hold it), "
                 "AND `tick()` KEEPS PROBING ITS ROOT, UNTIL A RESTART; the store is already empty and sealed, so nothing is served from it.")
_LIMITS_HEAD = "Injected-fault limits"
_LIMITS_SENTENCES = (
    "The mode's step is all-or-nothing for a raise in `seat_registry_error` and for a raise in `seal()`'s computation (`_untrusted_retirement`); both are tested.",
    "A raise in memory-only bookkeeping AFTER the locked seal is NOT covered.",
    "A fault in the unseat loop is logged with its class name and does not stop the loop.",
    "On a loader thread (`bootload._worker` and `_load_one` catch only `Exception`) it ends that thread only: the server continues",
    "an OSError out of `_specs_of` or `combine_roots` propagates as an OSError after the one exit runs",
    "An OSError out of a root job becomes `CorpusFileError` (`corpus-file-unreadable`) (tested)",
)


def test_the_unseat_fault_limit_is_worded_identically_in_the_changelog_the_sdlc_and_the_docstring():
    """R21-5, R22-7, R23. The limit (a corpus whose unseat faulted after the mode began) is ONE sentence, including that `tick()` keeps probing its root and that the bookkeeping may be
    partial, in all three places it is stated; the SDLC `Injected-fault limits` paragraph carries the other limits, and the claims Round 23 retracted are not back (a lexical check
    for those phrases, not a proof)."""
    import inspect

    from factlattice_engine import server
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    doc = " ".join(inspect.getdoc(server._enter_registry_error).split())
    assert _UNSEAT_LIMIT in doc and "STAYS IN THE" not in doc
    texts = {name: " ".join(open(os.path.join(root, name), encoding="utf-8").read().split()) for name in ("CHANGELOG.md", "SDLC.md")}
    for name, text in texts.items():
        assert _UNSEAT_LIMIT in text, name
        assert "STAYS IN THE SEALED KERNEL" not in text, name
        assert "may already be retired" not in text and "probes, not state" not in text, name
        assert "no handler for a BaseException that is not an Exception and ends" not in text and "no handler for one that is not an Exception and ends" not in text, name
        assert "the step changes everything or nothing, and if the seal fails" not in text, name
    sdlc = texts["SDLC.md"]
    assert _LIMITS_HEAD in sdlc
    para = sdlc[sdlc.index("Injected-fault limits (added in Round 23"):]
    for s in _LIMITS_SENTENCES:
        assert s in para, s
    lines = open(os.path.join(root, "SDLC.md"), encoding="utf-8").read().split("\n")
    at = next(k for k, l in enumerate(lines) if l.lstrip().startswith("Injected-fault limits (added in Round 23"))
    assert lines[at - 1].startswith("- [ ] [STORE-TABLES-ONE-SNAPSHOT-ONE-STEP]"), "the paragraph sits directly under that rule"
    assert "(corrected in Round 22/23" in texts["CHANGELOG.md"] and "(narrowed in Round 23" in texts["CHANGELOG.md"]


def test_a_retry_that_meets_a_disk_needing_shape_error_enters_registry_error_mode_at_the_retry(tmp_path, monkeypatch):
    """Whole-slice read of 4384356, F1 (spec 38 x-boot-window, ruled 2026-11-06). A registry whose second entry has a pinned file that is not a string (a shape only a ROOT READ can
    know) beside a root that did NOT answer at boot: the first pass ends with that corpus stuck (pending), the server is not in registry-error mode, and the release would have
    refused the registry. When the root is back and the retry reads it, the shape error comes up AFTER the first pass, where nobody read `fatal`: the corpus was forgotten and
    was in NO table. Now the retry enters registry-error mode through the same step the first pass uses: the error is seated, the store is emptied (the served corpus too, as
    the release serves nothing) and the corpus is not left in any table. A second late verdict does not enter the mode again. Red on 4384356 (mode never entered; the healthy corpus
    still served; the stuck corpus in no table)."""
    from factlattice_engine import server
    w = _world6(tmp_path, monkeypatch)
    reg = json.loads(w.registry_path.read_text())
    reg = {"a1": reg["a1"], "zz-bad": {"roots": [{"path": str(w.root("b1")), "only_file": 5}], "domain_suffix": "x.bad"}}
    w.registry_path.write_text(json.dumps(reg))
    with RootStall(w.root("b1"), cap_s=60) as stall:
        eng = server.build_tools(watch=False, async_boot=True)
        _LOADERS.append(eng.boot_loader)
        assert eng.boot_loader.first_pass_done.wait(60)
        assert eng.registry_error is None, "the first pass met no verdict: the root had not answered"
        assert eng.store.corpus_ids() == ["a1"] and "zz-bad" in eng.store._pending_corpora
        stall.release()
        end = time.monotonic() + 30
        while time.monotonic() < end and eng.registry_error is None:
            eng.boot_loader.retry_stuck()
            time.sleep(0.1)
    assert eng.registry_error is not None, "the retry's verdict entered registry-error mode"
    assert eng.store.corpus_ids() == [] and not eng.store._pending_corpora and "zz-bad" not in eng.store._failed_corpora, "no corpus is left in any table"
    out = eng.list_corpora(heal=False)
    assert out["corpora"] == [] and (out.get("registry_choice") or {}).get("error")
    state = eng._reg_state
    eng.boot_loader._late_fatal()                      # a second late verdict (fatal is still set): does not enter the mode again
    assert eng._reg_state is state


# ------------------------------------------------------------------------------ round 25, from the cold read of 5f4b595
def _first_pass_window_world(tmp_path, monkeypatch):
    """a1 healthy, zz-bad = a pinned file that is not a string beside a root that EXISTS (a shape only a root read can know), c1 whose root is slow, so the first pass is still open
    after zz-bad's read has ended. Returns (workspace, registry)."""
    w = Workspace(tmp_path / "w", corpora=("a1", "b1", "c1"))
    _env(w, monkeypatch)
    reg = {"a1": w.registry["a1"], "zz-bad": {"roots": [{"path": str(w.root("b1")), "only_file": 5}], "domain_suffix": "x.bad"}, "c1": w.registry["c1"]}
    w.registry_path.write_text(json.dumps(reg))
    return w, reg


def test_a_corpus_whose_read_yields_the_registry_error_verdict_stays_pending_through_the_first_pass_and_is_in_exactly_one_table_at_every_point(tmp_path, monkeypatch):
    """Whole-slice read round 24, P1-1 and P3-2 (spec 38 x-boot-window: no corpus is ever in no table). A corpus whose read yields an entry-shape registry-error verdict
    is NOT dropped from the pending table when its read ends; it leaves it in the ONE locked step that seats the error, seals and empties. Sites that drop a corpus from
    pending on a load outcome, each with its census here: `settle` (one step: served, failed or no-roots, covered by the other tests of that method), the shape-error handler
    (this test), the abandoned first pass (`_join_bounded`, which skips this corpus, below), `clear_all` (the mode's step), unregister (a user act). The store's lock is
    wrapped to take a census at every point it is let go: zz-bad is in exactly one of served, pending, failed, no-roots, or in none while the store is sealed and the error is
    seated. During the window a write under its root does not land unflagged with corpora []. Red on 5f4b595 (in no table from the end of its read to the end of the first pass)."""
    from factlattice_engine import server
    from factlattice_engine import tools as T
    w, _reg = _first_pass_window_world(tmp_path, monkeypatch)
    census, holder = [], {}

    class Spy(threading.Condition):
        depth = 0

        def __enter__(self):
            Spy.depth += 1
            return super().__enter__()

        def __exit__(self, *a):
            Spy.depth -= 1
            if Spy.depth == 0 and holder.get("st") is not None:
                st = holder["st"]
                n = sum("zz-bad" in t for t in (st._by_corpus_id, st._pending_corpora, st._failed_corpora, st._no_roots_corpora))
                census.append((n, st._sealed, holder["eng"].registry_error is not None if holder.get("eng") is not None else False))
            return super().__exit__(*a)
    with RootStall(w.root("c1"), cap_s=60, slow_s=0.4):
        eng = server.build_tools(watch=False, async_boot=True)
        _LOADERS.append(eng.boot_loader)
        holder["st"], holder["eng"] = eng.store, eng
        eng.store._pending_cv = Spy()
        seen_pending, write = False, None
        end = time.monotonic() + 60
        while time.monotonic() < end and not eng.boot_loader.first_pass_done.is_set():
            lst = eng.list_corpora(heal=False)
            row = [c for c in lst["corpora"] if c["corpus_id"] == "zz-bad"]
            if row and row[0]["total_nodes"] is None and row[0]["root_health"]["status"] == "pending" and eng.boot_loader.fatal is not None:
                seen_pending = True                    # listed pending with null totals AFTER its read yielded the verdict (before it, it was pending on any tree)
                if write is None and "zz-bad" in eng.store._pending_corpora:
                    write = T.dispatch("lts_add_property", {"path": str(w.file("b1")), "node_id": "B1-N", "key": "k25", "value": "v"}, tools=eng)
            time.sleep(0.005)
        assert eng.boot_loader.first_pass_done.wait(60)
    assert seen_pending, "the bad corpus is listed as pending, with null totals, after its read yielded the verdict and before the first pass ends"
    assert eng.boot_loader.fatal is not None and eng.registry_error is not None, "the mode was entered where it was before"
    assert census, "setup: the census ran"
    assert all(n == 1 or (n == 0 and sealed and err) for n, sealed, err in census), [c for c in census if not (c[0] == 1 or (c[0] == 0 and c[1] and c[2]))][:5]
    assert write is not None, "setup: a write was issued in the window, after the verdict was reached and before the first pass ended"
    rm = write.get("root_membership") or {}
    assert not (rm.get("corpora") == [] and not rm.get("registry_error")), f"a write under its root landed unflagged with corpora []: {rm}"


def test_the_first_pass_that_is_abandoned_does_not_fail_a_corpus_that_stays_pending_for_the_registrys_verdict(tmp_path, monkeypatch):
    """Round 25. `_join_bounded` fails every corpus still pending when the loader is given up on (`corpus-root-unresponsive`). A corpus that stays pending only for the
    registry's verdict is not one of them: it keeps the registry's verdict, never a wrong code for a moment before the mode's step."""
    from factlattice_engine import bootload
    from factlattice_engine import store as S
    st = S.LtsStore()
    spec = {"roots": [{"path": str(tmp_path)}], "domain_suffix": "x.p"}
    loader = bootload.StoreLoader(st, {"p1": spec, "p2": spec}, kernel=None)
    for cid in ("p1", "p2"):
        st.declare_pending(cid, {"spec": spec, "private": False, "paths": [str(tmp_path)], "domain_suffix": "x.p", "since": 0.0, "stuck": False})
    loader._fatal_pending["p1"] = [str(tmp_path)]
    ts = [threading.Thread(target=lambda: time.sleep(5), daemon=True)]
    ts[0].start()
    loader.reg.deadline_s = 0.05
    loader._activity = loader.reg.clock() - 100.0
    loader._join_bounded(ts)
    assert "p1" in st._pending_corpora and "p1" not in st._failed_corpora, "stays pending"
    assert "p2" in st._failed_corpora and st._failed_corpora["p2"]["code"] == "corpus-root-unresponsive"


@pytest.mark.parametrize("point", ["bounded-read", "provisional-pin"])
def test_a_late_registry_error_pin_is_not_replaced_by_a_slow_first_pass_pin(tmp_path, monkeypatch, point):
    """Round 25, P3-1. The first-pass hook (no verdict) pins the booted ledger table: first a provisional pin, then (after a bounded read that can wait a deadline on a hung
    root) the final one. If a retry enters registry-error mode meanwhile and pins 'no table', neither hook pin may replace it. STAGED at each point: the late verdict
    runs just before the provisional pin, or inside the bounded read ('...#ledger-table-pin'). The last pin is the empty table. Red on 5f4b595 (the real table replaced it)."""
    from factlattice import ledger as _ledger
    from factlattice_engine import boundedfs, server
    w = Workspace(tmp_path / "w", corpora=("a1", "b1"))
    _env(w, monkeypatch)
    pins, real_pin, real_call = [], _ledger.pin_table, boundedfs.bounded_call
    monkeypatch.setattr(_ledger, "pin_table", lambda ctx, final=True: (pins.append((ctx, final)), real_pin(ctx, final=final))[1])
    state = {"calls": 0}

    def late_verdict():
        assert _wait(lambda: "eng" in state, 30)
        loader = state["eng"].boot_loader
        loader.fatal = RuntimeError("a shape verdict met by a retry")          # the hook found none; the retry's verdict arrives while the hook is mid-pin
        loader._late_fatal()                                                   # enters the mode (pins 'no table')

    def staged(key, fn, default, reg=None):
        if point == "bounded-read" and key.endswith("#ledger-table-pin") and not state.get("done"):
            state["done"] = True
            late_verdict()
        return real_call(key, fn, default, reg) if reg is not None else real_call(key, fn, default)
    monkeypatch.setattr(boundedfs, "bounded_call", staged)
    real_prov = server._pin_ledger_table_provisional

    def staged_prov(choice, guard=None):
        state["calls"] += 1
        if point == "provisional-pin" and state["calls"] == 2:                  # the first call is the declare-time pin; the second is the hook's
            late_verdict()
        return real_prov(choice, guard)
    monkeypatch.setattr(server, "_pin_ledger_table_provisional", staged_prov)
    try:
        eng = server.build_tools(watch=False, async_boot=True)
        state["eng"] = eng
        _LOADERS.append(eng.boot_loader)
        assert eng.boot_loader.first_pass_done.wait(60)
    finally:
        real_pin(None)
    assert eng.registry_error is not None
    assert pins[-1][0] == _ledger.TableContext(), "the mode's 'no table' pin is the last pin"


@pytest.mark.parametrize("entry,route", [({"pth": "bad"}, "key-failed"), ({"root": "${nosuch}/thing"}, "named-root"), ({"pth": "${nosuch}/thing"}, "named-root")])
def test_unregister_clears_the_write_flag_for_a_key_failed_entry_and_not_for_a_named_root_failure(tmp_path, monkeypatch, entry, route):
    """Round 25, P2-1. The flag on every write has two routes with different ends, keyed on where the flag comes from (round 26: `pth: \"${nosuch}/thing\"` is on the SECOND route). An entry the store records as unlocated (`pth: \"bad\"`): `lts_unregister_corpus` clears the
    flag (success, was_unlocated). A named-root failure (`${nosuch}/...`): the guard reads the registry, so unregister answers `no such loaded corpus`, the flag stays, and it ends
    when the registry file is fixed and the entry registered again (or at a restart). This pins each route, so the CHANGELOG and spec 38 have something under them."""
    from factlattice_engine import authoring, config
    from factlattice_engine import tools as T
    from factlattice_engine.tools import LtsEngine
    ok, odd = _route_world(tmp_path, monkeypatch, entry)
    st = config.build_store()
    eng = LtsEngine(st)
    out = T.dispatch("lts_unregister_corpus", {"corpus_id": "oddc"}, tools=eng)
    w = authoring.add_property(str(ok / "a.lts"), "A-2", "k", "v", store=st)
    rm = w["root_membership"]
    if route == "key-failed":
        assert out.get("success") is True and out.get("was_unlocated") is True, out
        assert rm["corpora"] == ["okc"] and not rm.get("registry_error"), rm
    else:
        assert out.get("success") is False, out
        assert rm["corpora"] == [] and rm.get("registry_error"), rm
        regfile = tmp_path / "reg.json"
        reg = json.loads(regfile.read_text())
        reg["oddc"] = {"root": str(odd), "domain_suffix": "x.odd"}
        regfile.write_text(json.dumps(reg))
        fixed = T.dispatch("lts_register_corpus", {"corpus_id": "oddc"}, tools=eng)
        assert fixed.get("success") is True, fixed
        w2 = authoring.add_property(str(ok / "a.lts"), "A-2", "k2", "v", store=st)
        assert w2["root_membership"]["corpora"] == ["okc"] and not w2["root_membership"].get("registry_error"), w2["root_membership"]


# A PRIVATE corpus id that cannot occur in a path, a temp-dir name or a hash. A short id ("b1", "c1", "z8") is a substring of the root path of a PUBLIC corpus that an
# answer carries whenever the temp dir's name happens to contain it (measured: TMPDIR=/tmp/zzq-ab1x/ failed `"b1" not in json.dumps(...)`), so a "never named" check that
# uses one is flaky by construction. These tests give the private corpus this id and assert it is absent from the WHOLE answer.
PRIV_ID = "zzprivcorpusq"


def _window_world(tmp_path, monkeypatch, with_slow_c1: bool):
    """Round 26. a1 healthy, zz-bad (a shape only a root read can know) on b1's root; optionally c1 hung so the first pass stays open."""
    w = Workspace(tmp_path / "w", corpora=("a1", "b1", "c1"))
    _env(w, monkeypatch)
    reg = {"a1": w.registry["a1"], "zz-bad": {"roots": [{"path": str(w.root("b1")), "only_file": 5}], "domain_suffix": "x.bad"}}
    if with_slow_c1:
        reg["c1"] = w.registry["c1"]
    w.registry_path.write_text(json.dumps(reg))
    return w


def _hold_first_pass_open(monkeypatch, seconds, slow_cid="c1"):
    """`slow_cid`'s (default c1's) load takes `seconds`, showing a sign of life every 0.1 s (a slow healthy corpus), so the first pass stays open that long and no root is hung."""
    from factlattice_engine import bootload
    real = bootload.StoreLoader._load_one

    def slow(self, cid):
        if cid == slow_cid:
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                self._alive()
                time.sleep(0.1)
        return real(self, cid)
    monkeypatch.setattr(bootload.StoreLoader, "_load_one", slow)


def _wait_verdict_held(eng, limit=30.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        rec = eng.store._pending_corpora.get("zz-bad")
        if rec is not None and eng.boot_loader.fatal is not None:                  # pending, and its read has yielded the verdict (the marker `verdict` is the fix's, so a tree without it can be asked)
            return True
        time.sleep(0.002)
    return False


def test_a_corpus_held_for_the_registry_verdict_is_answered_exactly_as_any_pending_corpus_with_the_true_cause(tmp_path, monkeypatch):
    """Round 27 (R26-P2-1). A corpus held pending for the registry-error verdict (a shape only a root read can show) is answered as ANY pending corpus: a named call waits up to
    one root deadline (0.5 s here) and, with the mode's step not yet come, is refused `corpus-verdict-pending`, which states the true cause and never says a root did not answer
    (the root answered). A corpus-omitted find leaves it out and declares it in `corpora_skipped` with that reason, not `root-pending`; the listing shows `root_health` status
    `pending` (unchanged) plus `reason: verdict-pending`, null totals. Red on 34e9668: the named find is refused `root-unresponsive`."""
    from factlattice_engine import server
    from factlattice_engine import tools as T
    w = _window_world(tmp_path, monkeypatch, True)
    _hold_first_pass_open(monkeypatch, 3.0)
    eng = server.build_tools(watch=False, async_boot=True)
    _LOADERS.append(eng.boot_loader)
    assert _wait_verdict_held(eng) and eng.registry_error is None, "setup: held, and the first pass is still open"
    t0 = time.monotonic()
    res = T.dispatch("lts_find", {"corpus": "zz-bad", "text": "lanternfish"}, tools=eng)
    waited = time.monotonic() - t0
    assert res.get("success") is False and res.get("reason") == "corpus-verdict-pending" and res.get("error_class") == "CorpusVerdictPending", res
    assert "did not answer" not in json.dumps(res) and (res.get("root_health") or {}).get("status") == "pending", res
    assert DEADLINE_S * 0.8 <= waited < DEADLINE_S + 1.5, f"about one root deadline: {waited}"
    un = T.dispatch("lts_find", {"text": "lanternfish", "tier": "digest"}, tools=eng)
    assert un.get("success") is True and un.get("complete") is False, un
    skipped = un["extent"]["corpora_skipped"]
    assert {"corpus": "zz-bad", "reason": "corpus-verdict-pending"} in skipped and {"corpus": "zz-bad", "reason": "root-pending"} not in skipped, skipped
    row = [c for c in eng.list_corpora(heal=False)["corpora"] if c["corpus_id"] == "zz-bad"][0]
    assert row["total_nodes"] is None and row["root_health"]["status"] == "pending" and row["root_health"]["reason"] == "verdict-pending", row
    assert eng.boot_loader.first_pass_done.wait(60) and eng.registry_error is not None


def test_a_write_under_a_corpus_held_for_the_verdict_waits_one_deadline_then_is_refused_corpus_verdict_pending_and_lands_after_the_mode_step(tmp_path, monkeypatch):
    """Round 27 (R26-P2-2, general rule). The write door answers a held corpus exactly as the read door: it waits ONE root deadline (never the first pass, which is bounded by progress
    and not by time), and if the mode's step has not come it is refused `corpus-verdict-pending` (retryable, nothing written, not `root-unresponsive`, not
    `membership-unconfirmed`). After the step a write lands by registry-error mode's rules. Red on 34e9668 (waited for the step, however long the first pass took)."""
    from factlattice_engine import server
    from factlattice_engine import tools as T
    w = _window_world(tmp_path, monkeypatch, True)
    _hold_first_pass_open(monkeypatch, 3.0)
    before = w.file("b1").read_bytes()
    eng = server.build_tools(watch=False, async_boot=True)
    _LOADERS.append(eng.boot_loader)
    assert _wait_verdict_held(eng) and eng.registry_error is None
    t0 = time.monotonic()
    out = T.dispatch("lts_add_property", {"path": str(w.file("b1")), "node_id": "B1-N", "key": "k27", "value": "v"}, tools=eng)
    waited = time.monotonic() - t0
    assert out.get("written") is False and out.get("reason") == "corpus-verdict-pending", out
    assert "root-unresponsive" not in json.dumps(out) and "did not answer" not in json.dumps(out), out
    assert DEADLINE_S * 0.8 <= waited < DEADLINE_S + 1.5, f"about one root deadline: {waited}"
    assert w.file("b1").read_bytes() == before, "nothing written"
    assert eng.boot_loader.first_pass_done.wait(60) and eng.registry_error is not None
    after = T.dispatch("lts_add_property", {"path": str(w.file("b1")), "node_id": "B1-N", "key": "k27", "value": "v"}, tools=eng)
    assert after.get("written") is not False and after.get("reason") != "corpus-verdict-pending", after
    assert w.file("b1").read_bytes() != before, "after the step it landed, by the mode's rules"


def test_a_write_that_meets_the_mode_step_inside_the_one_deadline_lands_by_the_modes_rules(tmp_path, monkeypatch):
    """Round 27. The first pass ends (the step comes) 0.3 s after the verdict, inside the 0.5 s deadline: the write waited for it, then landed by registry-error mode's rules."""
    from factlattice_engine import server
    from factlattice_engine import tools as T
    w = _window_world(tmp_path, monkeypatch, True)
    _hold_first_pass_open(monkeypatch, 0.4)
    before = w.file("b1").read_bytes()
    eng = server.build_tools(watch=False, async_boot=True)
    _LOADERS.append(eng.boot_loader)
    assert _wait_verdict_held(eng)
    out = T.dispatch("lts_add_property", {"path": str(w.file("b1")), "node_id": "B1-N", "key": "k27b", "value": "v"}, tools=eng)
    assert out.get("written") is not False and out.get("reason") not in ("corpus-verdict-pending", "root-unresponsive"), out
    assert eng.registry_error is not None and w.file("b1").read_bytes() != before


def test_unregistering_the_held_corpus_in_the_window_takes_it_out_of_every_table_and_the_mode_is_still_entered(tmp_path, monkeypatch):
    """Round 26, P3-2: what unregister means. The user's act forgets the corpus (pending, was_pending true); a write under its root then attributes to nothing; the mode is still
    entered from the file when the first pass ends. Stated here so it is not accidental."""
    from factlattice_engine import server
    from factlattice_engine import tools as T
    w = _window_world(tmp_path, monkeypatch, True)
    _hold_first_pass_open(monkeypatch, 2.0)
    if True:
        eng = server.build_tools(watch=False, async_boot=True)
        _LOADERS.append(eng.boot_loader)
        assert _wait_verdict_held(eng)
        out = T.dispatch("lts_unregister_corpus", {"corpus_id": "zz-bad"}, tools=eng)
        assert out.get("success") is True and out.get("was_pending") is True, out
        assert "zz-bad" not in eng.store._pending_corpora and "zz-bad" not in eng.store._failed_corpora
        wr = T.dispatch("lts_add_property", {"path": str(w.file("b1")), "node_id": "B1-N", "key": "k26u", "value": "v"}, tools=eng)
        assert (wr.get("root_membership") or {}).get("corpora") == [] or wr.get("written") is not None, wr
    assert eng.boot_loader.first_pass_done.wait(30)
    assert eng.registry_error is not None, "the mode is still entered from the file"


def test_a_write_under_a_held_corpus_does_not_hold_up_the_listing_behind_it_on_the_real_stdio_server(tmp_path):
    """Round 27 (R26-P2-2), through the REAL subprocess server under the stall harness. The server handles one request at a time, so a write that waits longer than one root deadline
    holds up every call behind it, `lts_list_corpora` included (spec 38 build-trap 5: the listing never waits; x-server: a write in the boot window waits as a named read does).
    c1 is a large corpus read on a slow disk (it keeps showing progress, so the first pass stays open for many seconds); zz-bad (`only_file: 5`) is held for the registry-error verdict.
    A write under its root and, sent right after it, a listing: the write is refused `corpus-verdict-pending` at about one deadline, the listing answers right behind it, and the first
    pass is still open. Red on 34e9668 (the write waited for the step: both calls took as long as the first pass)."""
    w = Workspace(tmp_path / "w", corpora=("a1", "b1", "c1"))
    for i in range(300):
        (w.root("c1") / f"x{i}.lts").write_text(f"---\nlts: \"1.1\"\ntitle: x{i}\ndomain: x.c1\ndoctype: SPEC\n---\n\n# R [C1-X{i}]\n\n- summary: r\n", encoding="utf-8")
    reg = {"a1": w.registry["a1"], "zz-bad": {"roots": [{"path": str(w.root("b1")), "only_file": 5}], "domain_suffix": "x.bad"}, "c1": w.registry["c1"]}
    w.registry_path.write_text(json.dumps(reg))
    before = w.file("b1").read_bytes()
    with RealServer(w, stall_root=w.root("c1"), engaged=True, slow_s=0.05, env=DEADLINES, confirm_s=60.0) as srv:
        srv.initialize(timeout=2.0)

        time.sleep(2.5)                              # zz-bad's own read ended long ago (its shape was met), while the slow large c1 keeps the first pass open
        lst0, _ = srv.call("lts_list_corpora", {"heal": False}, timeout=5.0)
        assert "error" not in (lst0.get("registry_choice") or {}), "setup: the first pass is still open"
        assert (entry(lst0, "zz-bad") or {}).get("root_health", {}).get("status") == "pending" and (entry(lst0, "c1") or {}).get("root_health", {}).get("status") == "pending", lst0
        t0 = time.monotonic()
        rid_w = srv.send_call("lts_add_property", {"path": str(w.file("b1")), "node_id": "B1-N", "key": "k27", "value": "v"})
        rid_l = srv.send_call("lts_list_corpora", {"heal": False})
        wr = srv.unwrap(srv.wait_reply(rid_w, 30.0))
        t_w = time.monotonic() - t0
        ls = srv.unwrap(srv.wait_reply(rid_l, 30.0))
        t_l = time.monotonic() - t0
        assert wr.get("written") is False and wr.get("reason") == "corpus-verdict-pending", wr
        assert 0.7 <= t_w < 4.0, f"the write waits about one root deadline (1 s), not the first pass: {t_w}"
        assert t_l - t_w < 1.5, f"the listing answers right behind the write: {t_l - t_w}"
        assert "error" not in (ls.get("registry_choice") or {}), "the first pass is still open: the mode has not begun"
        row = entry(ls, "zz-bad")
        assert row and row["root_health"]["status"] == "pending" and row["root_health"]["reason"] == "verdict-pending" and row["total_nodes"] is None, row
        assert w.file("b1").read_bytes() == before, "nothing written"


# ------------------------------------------------------------------------------ round 28, from the cold read of 6067f9b
def test_register_on_a_corpus_held_for_the_verdict_gets_the_true_cause_and_an_ordinary_pending_id_keeps_its_refusal(tmp_path, monkeypatch):
    """Round 28 (R27-P2-1). `lts_register_corpus` on a HELD id used to say "still being read (or its root has not answered yet) ... wait for it to load", with no code: both causes
    are false (its read ended, its root answered, and it will never load). It is refused `corpus-verdict-pending` with that code's true text, as the read and write doors do. An
    ordinarily pending id (c1, still being read) keeps today's refusal. Red on 6067f9b."""
    from factlattice_engine import server
    from factlattice_engine import tools as T
    w = _window_world(tmp_path, monkeypatch, True)
    _hold_first_pass_open(monkeypatch, 3.0)
    eng = server.build_tools(watch=False, async_boot=True)
    _LOADERS.append(eng.boot_loader)
    assert _wait_verdict_held(eng) and eng.registry_error is None
    held = T.dispatch("lts_register_corpus", {"corpus_id": "zz-bad"}, tools=eng)
    assert held.get("success") is False and held.get("reason") == "corpus-verdict-pending" and held.get("error_class") == "CorpusVerdictPending", held
    assert "still being read" not in json.dumps(held) and "did not answer" not in json.dumps(held) and "first read pass ends" not in json.dumps(held), held
    assert "registry-error mode" in held["error"], held
    ordinary = T.dispatch("lts_register_corpus", {"corpus_id": "c1"}, tools=eng)
    assert ordinary.get("success") is False and "still being read" in ordinary.get("error", "") and "reason" not in ordinary, ordinary
    assert eng.boot_loader.first_pass_done.wait(60)


def _private_unread_find(tmp_path, monkeypatch, held):
    """(engine, reply of a corpus-omitted find) with c1 (private) pending and, when `held`, zz-bad (private) held for the verdict."""
    from factlattice_engine import server
    from factlattice_engine import tools as T
    w = _window_world(tmp_path, monkeypatch, True)
    reg = json.loads(w.registry_path.read_text())
    reg[PRIV_ID] = {**reg.pop("c1"), "private": True}                 # the slow private corpus, under an id that cannot be in a path
    if held:
        reg["zz-bad"] = {**reg["zz-bad"], "private": True}
    else:
        del reg["zz-bad"]
    w.registry_path.write_text(json.dumps(reg))
    _hold_first_pass_open(monkeypatch, 3.0, slow_cid=PRIV_ID)
    eng = server.build_tools(watch=False, async_boot=True)
    _LOADERS.append(eng.boot_loader)
    if held:
        assert _wait_verdict_held(eng)
    return eng, T.dispatch("lts_find", {"text": "lanternfish", "tier": "digest"}, tools=eng)


@pytest.mark.parametrize("held", [False, True])
def test_a_corpus_omitted_find_that_leaves_out_only_private_unread_corpora_is_incomplete(tmp_path, monkeypatch, held):
    """Round 28 (R27-P3-2, ruled P2), the flag. A corpus-omitted search that leaves out a corpus, public or private, for one of this spec's not-read reasons (root-pending, root-unresponsive,
    corpus-verdict-pending, corpus-root-unresponsive) is `complete` false. Here the slow corpus (private) is pending, and zz-bad (private) is held when `held`; nothing is named. Red on 6067f9b: `complete: true`."""
    eng, un = _private_unread_find(tmp_path, monkeypatch, held)
    assert un.get("success") is True and un.get("complete") is False, un
    assert PRIV_ID not in json.dumps(un) and "zz-bad" not in json.dumps(un), "never named"
    assert eng.boot_loader.first_pass_done.wait(60)


@pytest.mark.parametrize("held", [False, True])
def test_each_left_out_private_unread_corpus_is_counted_exactly_once_in_the_anonymous_count(tmp_path, monkeypatch, held):
    """Round 28 (R27-P3-2), the count. Each left-out private corpus that is not read is counted ONCE, in the anonymous `private-corpus-failed` entry (the
    `private: name it explicitly` entry holds the private corpora that are served). Red on 6067f9b: each was counted twice, under both reasons."""
    eng, un = _private_unread_find(tmp_path, monkeypatch, held)
    skipped = un["extent"]["corpora_skipped"]
    assert {"reason": "private-corpus-failed", "count": 2 if held else 1} in skipped, skipped
    assert not [s for s in skipped if s.get("reason") == "private: name it explicitly"], ("counted once, not twice", skipped)
    assert eng.boot_loader.first_pass_done.wait(60)


def test_a_served_private_corpus_is_counted_once_by_design_and_does_not_make_the_answer_incomplete(tmp_path, monkeypatch):
    """Control for the previous test (green on both trees): a private corpus that is SERVED is left out of a corpus-omitted search by design."""
    from factlattice_engine import server
    from factlattice_engine import tools as T
    w = Workspace(tmp_path / "w", corpora=("a1", "b1"))
    _env(w, monkeypatch)
    reg = {"a1": w.registry["a1"], PRIV_ID: {**w.registry["b1"], "private": True}}
    w.registry_path.write_text(json.dumps(reg))
    eng = server.build_tools(watch=False, async_boot=True)
    _LOADERS.append(eng.boot_loader)
    assert eng.boot_loader.first_pass_done.wait(60) and sorted(eng.store.corpus_ids()) == sorted(["a1", PRIV_ID])
    un = T.dispatch("lts_find", {"text": "lanternfish", "tier": "digest"}, tools=eng)
    assert un.get("success") is True and un.get("complete") is True, un
    assert {"reason": "private: name it explicitly", "count": 1} in un["extent"]["corpora_skipped"] and PRIV_ID not in json.dumps(un), un["extent"]


def test_the_never_named_check_fires_when_the_private_id_is_planted_in_the_answer(tmp_path, monkeypatch):
    """Positive control for the test above: the check is on a distinctive id, so it FAILS when that id is in the answer (here planted into every lts_find reply)
    and does not depend on what the temp dir is called."""
    from factlattice_engine import tools as T
    real = T.dispatch

    def planting(name, args, tools=None, **kw):
        out = real(name, args, tools=tools, **kw)
        return {**out, "planted": PRIV_ID} if name == "lts_find" and isinstance(out, dict) else out
    monkeypatch.setattr(T, "dispatch", planting)
    with pytest.raises(AssertionError):
        test_a_served_private_corpus_is_counted_once_by_design_and_does_not_make_the_answer_incomplete(tmp_path, monkeypatch)


@pytest.mark.parametrize("private", [False, True])
def test_a_hop_from_a_served_corpus_into_the_held_corpus_is_declared_or_withheld(tmp_path, monkeypatch, private):
    """Round 28 (R27-P3-3), a pin. A hop from a served corpus into the corpus held for the verdict is declared in `routed_stale` with `corpus-verdict-pending` and `unserved: true`;
    when the held corpus is private it gets the withheld stub, and nothing names it."""
    from factlattice_engine import server
    from factlattice_engine import tools as T
    w = _window_world(tmp_path, monkeypatch, True)
    with open(w.file("a1"), "a", encoding="utf-8") as fh:
        fh.write("- depends-on: [x.bad:SPEC:B1-N]\n")
    if private:
        reg = json.loads(w.registry_path.read_text())
        reg["zz-bad"] = {**reg["zz-bad"], "private": True}
        w.registry_path.write_text(json.dumps(reg))
    _hold_first_pass_open(monkeypatch, 3.0)
    eng = server.build_tools(watch=False, async_boot=True)
    _LOADERS.append(eng.boot_loader)
    assert _wait_verdict_held(eng) and eng.registry_error is None
    assert _wait(lambda: "a1" in eng.store.corpus_ids(), 10)
    out = T.dispatch("lts_node", {"corpus": "a1", "node_id": "A1-N"}, tools=eng)
    assert out.get("success") is True, out
    if private:
        assert out.get("routed_withheld") and not out.get("routed_stale"), out
        assert "zz-bad" not in json.dumps(out) and "B1-N" not in json.dumps(out.get("routed_withheld")), "never named"
    else:
        stale = [x for x in out.get("routed_stale") or [] if x.get("corpus") == "zz-bad"]
        assert stale and stale[0]["reason"] == "corpus-verdict-pending" and stale[0].get("unserved") is True, out.get("routed_stale")
    assert eng.boot_loader.first_pass_done.wait(60)


def test_the_three_texts_of_corpus_verdict_pending_name_the_event_in_one_wording():
    """Round 28 (R27-P3-1). When the mode begins is true on every route: at the end of the first read pass, or at once when a later read meets the verdict. The read refusal, the
    write refusal and the ledger's fix text share ONE constant (`rootview.VERDICT_PENDING_WHEN`)."""
    from factlattice import ledger, rootview
    from factlattice_engine import tools as T
    when = rootview.VERDICT_PENDING_WHEN
    assert "or at once when a later read meets the verdict" in when
    assert when in ledger.fix_for(ledger.CORPUS_VERDICT_PENDING)
    assert when in T._verdict_pending_refusal("c", {"status": "pending", "roots": 1, "since": "x"})["error"]
    import inspect

    from factlattice_engine import authoring
    assert "_rv.VERDICT_PENDING_WHEN" in inspect.getsource(authoring.verdict_pending_refusal)
    assert "first read pass ends" not in inspect.getsource(authoring.verdict_pending_refusal) + inspect.getsource(T._verdict_pending_refusal)


_COMPLETE_PROBE = r'''
import json, os, sys
from factlattice_engine import config, tools as T
from factlattice_engine.tools import LtsEngine
st = config.build_store()
eng = LtsEngine(st)
args = {"text": "lanternfish"}
if os.environ.get("PROBE_TIER"):
    args["tier"] = os.environ["PROBE_TIER"]
out = T.dispatch("lts_find", args, tools=eng)
sk = [s for s in (out.get("extent") or {}).get("corpora_skipped") or []]
print("PKG", os.path.dirname(__import__("factlattice_engine").__file__), file=sys.stderr)
print(json.dumps({"complete": out.get("complete"), "skipped": sk}))
'''


@pytest.mark.parametrize("tier", ["digest", ""])
@pytest.mark.parametrize("failure", ["broken-link", "unknown-key"])
@pytest.mark.parametrize("private", [False, True])
def test_a_release_era_failed_corpus_leaves_complete_exactly_as_the_release_has_it(tmp_path, private, failure, tier):
    """Round 28 correction. In the release `complete` describes the result set, not corpus coverage: a corpus-omitted find answers the same `complete` whatever failed corpora sit in
    `corpora_skipped` (here a corpus-file-broken-link). Spec 38 owns `complete: false` only for a corpus left out because it is NOT READ (root-pending, root-unresponsive,
    corpus-verdict-pending, corpus-root-unresponsive). The branch and release af4cfc2 are asked the same question in subprocesses (the release through FACTLATTICE_RELEASE_REPO, as the
    verdict table's honesty check does) and must agree. Red on eed520a (it made `complete` false for any failure)."""
    import io
    import tarfile

    from tests.test_boot_verdict_table import RELEASE, _release_repo
    base = tmp_path / "w"
    ok, bad = base / "ok", base / "bad"
    ok.mkdir(parents=True)
    bad.mkdir()
    (ok / "a.lts").write_text('---\nlts: "1.1"\ntitle: a\ndomain: x.ok\ndoctype: SPEC\n---\n\n# R [OK-ROOT]\n\n- summary: r\n\n## N [OK-N]\n\n- topic: lanternfish\n', encoding="utf-8")
    if failure == "broken-link":
        os.symlink(str(base / "nowhere.lts"), str(bad / "broken.lts"))               # a member that is a link to nothing: corpus-file-broken-link
    else:
        (bad / "b.lts").write_text(ok.joinpath("a.lts").read_text(encoding="utf-8").replace("x.ok", "x.bad").replace("OK-", "BAD-"), encoding="utf-8")
    entry = {"root": str(bad), "domain_suffix": "x.bad"}
    if failure == "unknown-key":
        entry["bogus_key"] = 1                                                           # registry-entry-unknown-key: a key the registry does not know
    if private:
        entry["private"] = True
    reg = base / "reg.json"
    reg.write_text(json.dumps({"okc": {"root": str(ok), "domain_suffix": "x.ok"}, "badc": entry}))
    rel = tmp_path / "release"
    rel.mkdir()
    blob = subprocess.run(["git", "-C", str(_release_repo()), "archive", RELEASE], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        tf.extractall(rel, filter="data")
    answers = {}
    for name, tree in (("release", rel), ("branch", Path(__file__).resolve().parent.parent)):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTLATTICE_", "LTS_"))}
        env.update(PYTHONPATH=str(tree), FACTLATTICE_CORPUS_CONFIG=str(reg), FACTLATTICE_STATE_DIR=str(base / f"st-{name}"), LTS_ENGINE_STATE_DIR=str(base / f"st-{name}"),
                   FACTLATTICE_CODE_WATCH="0", HOME=str(base), TMPDIR=str(base), PROBE_TIER=tier)
        cwd = base / f"cwd-{name}"
        cwd.mkdir()
        p = subprocess.run([sys.executable, "-P", "-c", _COMPLETE_PROBE], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=300)
        assert p.returncode == 0, p.stderr[-600:]
        pkg = [ln for ln in p.stderr.splitlines() if ln.startswith("PKG ")]
        assert len(pkg) == 1 and Path(pkg[0][4:]).resolve().is_relative_to(tree.resolve()), (name, pkg)
        answers[name] = json.loads(p.stdout.strip().splitlines()[-1])
    reasons = [s.get("reason") for s in answers["release"]["skipped"]]
    assert any(r in ("corpus-file-broken-link", "registry-entry-unknown-key", "private-corpus-failed") for r in reasons), ("the scenario does fail the corpus at the release", answers["release"])
    assert answers["branch"]["complete"] == answers["release"]["complete"], answers


def _scoped_world(tmp_path, monkeypatch, private_out_of_scope):
    """a1 served; c1 pending and OUTSIDE the search's scope, private or public (the control)."""
    w = Workspace(tmp_path / "w", corpora=("a1", "c1"))
    _env(w, monkeypatch)
    reg = dict(w.registry)
    if private_out_of_scope:
        reg["c1"] = {**reg["c1"], "private": True}
    w.registry_path.write_text(json.dumps(reg))
    _hold_first_pass_open(monkeypatch, 3.0)
    return w


@pytest.mark.parametrize("private_out_of_scope", [True, False])
@pytest.mark.parametrize("how", ["corpora-list", "default-scope"])
def test_a_scoped_search_is_not_made_incomplete_by_an_unread_corpus_outside_its_scope(tmp_path, monkeypatch, how, private_out_of_scope):
    """Round 28 follow-up 3 (F-P2-1). The decision covers only corpora the search HAD IN SCOPE and left out. A scoped search (`corpora=[...]`, or a server with LTS_DEFAULT_CORPORA)
    that reads a1 is complete although c1 is pending outside its scope. The PUBLIC pending case is the control (it behaved so already); the private one is the fix. Red on 0ae6936
    for the private case (`complete: false`)."""
    from factlattice_engine import server
    from factlattice_engine import tools as T
    _scoped_world(tmp_path, monkeypatch, private_out_of_scope)
    args = {"text": "lanternfish", "tier": "digest"}
    if how == "default-scope":
        monkeypatch.setenv("LTS_DEFAULT_CORPORA", "a1")
    else:
        args["corpora"] = ["a1"]
    eng = server.build_tools(watch=False, async_boot=True)
    _LOADERS.append(eng.boot_loader)
    assert _wait(lambda: "a1" in eng.store.corpus_ids() and "c1" in eng.store._pending_corpora, 10), "setup: a1 served, c1 still pending"
    out = T.dispatch("lts_find", args, tools=eng)
    assert out.get("success") is True and out.get("complete") is True, out
    assert eng.boot_loader.first_pass_done.wait(60)


def test_the_flag_path_gets_the_right_complete_from_a_different_thread_than_the_extent(tmp_path, monkeypatch):
    """Round 28 follow-up 3 (F-P2-2). The private-unread decision is an explicit value the extent's producer returns and the caller hands to `_flag_root_skips`; no thread or
    context carries it. `find` runs on a pool thread and the flag runs on this one: `complete` is false (a context variable would have read its default here). Control: nothing unread
    gives true."""
    from concurrent.futures import ThreadPoolExecutor

    from factlattice_engine import server
    w = _window_world(tmp_path, monkeypatch, True)
    reg = json.loads(w.registry_path.read_text())
    reg["c1"] = {**reg["c1"], "private": True}
    w.registry_path.write_text(json.dumps(reg))
    _hold_first_pass_open(monkeypatch, 3.0)
    eng = server.build_tools(watch=False, async_boot=True)
    _LOADERS.append(eng.boot_loader)
    assert _wait_verdict_held(eng)
    unread: dict = {}
    with ThreadPoolExecutor(max_workers=1) as pool:
        res = pool.submit(eng.find, text="lanternfish", tier="digest", _unread=unread).result(timeout=30)
    assert unread.get("private_unread") is True, unread
    assert res.get("complete") is True, "before the flag"
    assert eng._flag_root_skips(res, unread.get("private_unread", False))["complete"] is False
    assert eng.boot_loader.first_pass_done.wait(60)
    done = {}
    with ThreadPoolExecutor(max_workers=1) as pool:
        res2 = pool.submit(eng.find, text="lanternfish", tier="digest", _unread=done).result(timeout=30)
    assert done.get("private_unread") is False and eng._flag_root_skips(res2, done["private_unread"]).get("complete") is not False, (done, res2.get("complete"))
