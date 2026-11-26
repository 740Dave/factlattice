"""Round 29: the boot-speed changes of slice 4, and the bench's read-only rule.

  ADAPTIVE LOADER (specs/38 x-server, INV-BR-ONE-ROOT-ONE-COST). The SERVER's first pass starts with ONE loader and adds one (up to 4) only when a corpus read has been running without
  finishing for `rootstate.GROW_AFTER_S`; the blocking `build_store` keeps 4 from the start (x-library).
  FUSED JOB. One job per root reads, hashes and seats it (it was a build job and a seat job); the publish into the store is still one locked store step.
  BENCH. `--registry` is read-only: a write call is refused before it is sent.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.stall_harness import RealServer, Workspace

REPO = Path(__file__).resolve().parent.parent
HUNG_DEADLINE_S = "4"


def _healthy_world(tmp_path, monkeypatch, n=6, deadline="5"):
    w = Workspace(tmp_path / "w", corpora=tuple(f"c{i:02d}" for i in range(n)))
    for k, v in w.env({"LTS_ROOT_DEADLINE_S": deadline}).items():
        monkeypatch.setenv(k, v)
    from factlattice_engine import boundedfs
    boundedfs._reset()
    return w


def test_a_hung_root_at_boot_costs_only_its_own_corpus_through_the_real_stdio_server(tmp_path):
    """(i) x-server, INV-BR-ONE-ROOT-ONE-COST. Twelve corpora; the FIRST one's root hangs for the 4 s root deadline. The loader starts with one worker, which sits on the hung read, so the
    healthy corpora behind it are served only because the loader grows after GROW_AFTER_S (0.1 s): the last healthy corpus answers a named find within a second and a half of the process
    start, never one root deadline. Red with the growth rule disabled (the mutant, below: they wait the whole deadline)."""
    ids = tuple(f"c{i:02d}" for i in range(12))
    w = Workspace(tmp_path / "w", corpora=ids)
    t0 = time.perf_counter()
    with RealServer(w, stall_root=w.root(ids[0]), engaged=True, env={"LTS_ROOT_DEADLINE_S": HUNG_DEADLINE_S}, cap_s=30, confirm_s=60.0) as srv:
        srv.initialize(timeout=10)
        r, _dt = srv.call("lts_find", {"corpus": ids[-1], "text": "lanternfish", "tier": "digest"}, timeout=20)
        took = time.perf_counter() - t0
        assert r.get("success") is True and r.get("count") == 1, r
        lst, _ = srv.call("lts_list_corpora", {"heal": False}, timeout=5)
        status = {c["corpus_id"]: (c.get("total_nodes"), (c.get("root_health") or {}).get("status")) for c in lst["corpora"]}
        assert all(status[c][0] is not None for c in ids[1:]), ("every healthy corpus is served", status)
        assert status[ids[0]][0] is None and status[ids[0]][1] == "pending", ("the hung one stays pending", status[ids[0]])
    assert took < 1.5 + 0.5, f"the healthy corpora behind a hung root were served at {took:.2f}s (root deadline {HUNG_DEADLINE_S} s)"


def test_a_healthy_boot_starts_one_loader_and_adds_none(tmp_path, monkeypatch):
    """The growth rule only fires for a read that runs long: a healthy boot (reads of a few ms) adds nothing. ONE loader thread is started, `_nworkers` stays 1, and no corpus took a
    second loader. Red when the loader starts all four at once (the mutant, below)."""
    from factlattice_engine import server
    _healthy_world(tmp_path, monkeypatch, n=8)
    eng = server.build_tools(watch=False, async_boot=True)
    assert eng.boot_loader.first_pass_done.wait(60) and len(eng.store.corpus_ids()) == 8
    ld = eng.boot_loader
    assert ld.adaptive is True and ld._nworkers == 1 and len(ld._threads) == 1 and not ld._grown_for, (ld._nworkers, len(ld._threads), ld._grown_for)


def test_the_blocking_build_keeps_four_loaders_from_the_start(tmp_path, monkeypatch):
    """(ii) x-library is unchanged: `build_store` (the blocking loader) starts min(4, corpora) loaders at once and is not adaptive, so up to four hung roots cost one root deadline (ceil(K/4) from four stuck reads on)
    (`test_three_hung_roots_out_of_four...` in test_bounded_roots_boot.py is the timing form and stays green unchanged)."""
    from factlattice_engine import bootload, config
    _healthy_world(tmp_path, monkeypatch, n=8)
    names = []
    real = bootload.StoreLoader._worker

    def spy(self):
        names.append((threading.current_thread().name, self.adaptive))
        return real(self)
    monkeypatch.setattr(bootload.StoreLoader, "_worker", spy)
    store = config.build_store()
    assert len(store.corpus_ids()) == 8
    assert sorted(n for n, _a in names)[:4] == [f"lts-build-load-{i}" for i in range(4)] and not any(a for _n, a in names), names


def test_the_fused_job_is_the_only_job_a_root_gets_while_its_corpus_loads(tmp_path, monkeypatch):
    """(iii) A server's root is read, hashed and seated by ONE job on its worker: one `build` job per root during the first pass and NO `seat` job (it was two jobs per root). The
    progress per disk call and the one-running-job-per-root rule are the existing no-pile-up and progress tests, which stay green unchanged."""
    from factlattice_engine import rootstate, server
    w = _healthy_world(tmp_path, monkeypatch, n=6)
    jobs = []
    real = rootstate.RootSlot.submit

    def spy(self, kind, fn, **kw):
        jobs.append((self.path, kind))
        return real(self, kind, fn, **kw)
    monkeypatch.setattr(rootstate.RootSlot, "submit", spy)
    eng = server.build_tools(watch=False, async_boot=True)
    assert eng.boot_loader.first_pass_done.wait(60) and len(eng.store.corpus_ids()) == 6
    first_pass = [j for j in jobs if j[0].startswith(str(w.base))]
    assert not [j for j in first_pass if j[1] == "seat"], first_pass
    per_root = {}
    for p, k in first_pass:
        per_root.setdefault(p, []).append(k)
    assert all(v.count("build") == 1 for v in per_root.values()) and len(per_root) == 6, per_root
    # the kernel really holds what the seat job used to give it: the leaves of every root
    for cid in eng.store.corpus_ids():
        part = [p for p in eng.store.partitions() if p.corpus_id == cid][0]
        assert eng.store.kernel.leaves_snapshot(part), cid


# ------------------------------------------------------------------------------------------------ the bench (iv)
def _bench():
    import importlib.util
    spec = importlib.util.spec_from_file_location("bounded_reads_bench", REPO / "tools" / "bounded_reads_bench.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_bench_refuses_any_write_call_under_a_registry_before_it_is_sent(tmp_path):
    """(iv) With `--registry` the bench is READ-ONLY by construction: only the readiness find and lts_list_corpora(heal=false) may be sent. A planted write call is refused BEFORE it is sent
    (no process is even started). Control: the allowed calls pass the guard."""
    b = _bench()
    srv = b.Srv(REPO, tmp_path / "base", tmp_path / "reg.json", read_only=True)
    for name, args in (("lts_add_property", {"path": "x", "node_id": "N", "key": "k", "value": "v"}), ("lts_add_node", {}), ("lts_register_corpus", {"corpus_id": "c"}),
                       ("lts_list_corpora", {}), ("lts_list_corpora", {"heal": True}), ("lts_unregister_corpus", {"corpus_id": "c"})):
        with pytest.raises(b.ReadOnlyViolation):
            srv.call(name, args)
    assert srv.proc is None, "nothing was sent: no server was started"
    srv2 = b.Srv(REPO, tmp_path / "base2", tmp_path / "reg.json", read_only=True)
    with pytest.raises((RuntimeError, AttributeError)) as ei:         # passes the guard, then fails only because no server is running
        srv2.call("lts_list_corpora", {"heal": False})
    assert not isinstance(ei.value, b.ReadOnlyViolation)
    # the state dirs of a read-only server are its own temporary ones, both variables
    assert srv.env["LTS_ENGINE_STATE_DIR"] == srv.env["FACTLATTICE_STATE_DIR"] and str(tmp_path) in srv.env["FACTLATTICE_STATE_DIR"] and srv.env["HOME"].startswith(str(tmp_path))
    out = subprocess.run([sys.executable, str(REPO / "tools" / "bounded_reads_bench.py"), "--registry", str(tmp_path / "reg.json"), "--old", str(REPO), "--new", str(REPO)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode != 0 and "--boot-study" in out.stderr, "--registry is for --boot-study only"


def test_the_readiness_check_marks_a_run_invalid_when_the_find_did_not_wait():
    """(b) A READY that did not wait must never count: complete false, fewer corpora consulted than the registry holds, or anything skipped makes the run invalid."""
    b = _bench()
    ok = {"complete": True, "consulted": 48, "skipped": []}
    assert b.readiness_valid(ok, 48)[0] is True
    assert b.readiness_valid({**ok, "complete": False}, 48)[0] is False
    assert b.readiness_valid({**ok, "consulted": 47}, 48)[0] is False
    assert b.readiness_valid({**ok, "skipped": [{"corpus": "c1", "reason": "root-pending"}]}, 48)[0] is False
    assert b.readiness_valid(None, 48)[0] is False


def test_the_bench_prints_its_guarantee_and_the_readiness_check_for_a_registry(tmp_path):
    """The guarantee is the first thing printed under `--registry`, and each run prints its readiness check. One pair on a small synthetic registry, the same tree twice."""
    b = _bench()
    reg = b.gen(tmp_path / "gen", corpora=4, two_root=1, files=1, nodes=3)
    out = subprocess.run([sys.executable, str(REPO / "tools" / "bounded_reads_bench.py"), "--boot-study", "--registry", str(reg), "--old", str(REPO), "--new", str(REPO), "--pairs", "1"],
                         capture_output=True, text=True, timeout=300)
    lines = out.stdout.splitlines()
    assert lines and lines[0].startswith("READ-ONLY against --registry"), out.stdout[:300] + out.stderr[-300:]
    assert any("readiness find: complete=True consulted 4/4" in ln and "valid" in ln for ln in lines), out.stdout[-1500:]


def test_a_run_whose_only_skip_is_the_private_by_design_entry_with_the_right_count_is_valid():
    """The live registry's unnamed readiness find consults every PUBLIC corpus and declares the private ones in ONE by-design entry (the engine's `PRIVATE_REASON`) whose count is the registry's
    private corpora. That is a valid run; a wrong count, a second entry or any other skip reason (private-corpus-failed, root-pending, stale, ...) voids it. Red on 18a3979 (any skip voided it)."""
    from factlattice_engine.privacy import PRIVATE_REASON
    b = _bench()
    base = {"complete": True, "consulted": 37, "skipped": [{"reason": PRIVATE_REASON, "count": 12}]}
    assert b.readiness_valid(base, 37, 12)[0] is True
    assert b.readiness_valid({**base, "skipped": [{"reason": PRIVATE_REASON, "count": 11}]}, 37, 12)[0] is False, "wrong count"
    assert b.readiness_valid({**base, "skipped": [{"reason": PRIVATE_REASON, "count": 13}]}, 37, 12)[0] is False, "wrong count"
    assert b.readiness_valid({**base, "skipped": [{"reason": PRIVATE_REASON, "count": 12}, {"reason": "private-corpus-failed", "count": 1}]}, 37, 12)[0] is False
    assert b.readiness_valid({**base, "skipped": [{"reason": "private-corpus-failed", "count": 12}]}, 37, 12)[0] is False
    for other in ("root-pending", "root-unresponsive", "stale", "corpus-verdict-pending", "corpus-root-unresponsive"):
        assert b.readiness_valid({**base, "skipped": [{"reason": PRIVATE_REASON, "count": 12}, {"corpus": "c1", "reason": other}]}, 37, 12)[0] is False, other
    assert b.readiness_valid({**base, "complete": False}, 37, 12)[0] is False
    assert b.readiness_valid({**base, "consulted": 36}, 37, 12)[0] is False
    assert b.readiness_valid({**base, "consulted": 38}, 37, 12)[0] is False, "equals, not at least"
    assert b.readiness_valid({"complete": True, "consulted": 37, "skipped": []}, 37, 0)[0] is True, "no private corpus, nothing skipped"
    assert b.readiness_valid({"complete": True, "consulted": 37, "skipped": []}, 37, 12)[0] is False, "the registry has private corpora and the find declared none"
    import inspect
    assert "PRIVATE_REASON" in inspect.getsource(b.readiness_valid) and "private: name it explicitly" not in inspect.getsource(b.readiness_valid), "the engine's constant, not a copied literal"


def test_the_expected_private_count_follows_the_engines_own_privacy_rule(tmp_path):
    """`expected_private` counts private entries by the engine's `private_reason` (the flag, a non-boolean flag, or the `-personality` suffix), so it can match what the find declares."""
    b = _bench()
    reg = tmp_path / "r.json"
    reg.write_text(json.dumps({"a": {"root": "/x"}, "b-personality": {"root": "/y"}, "c": {"root": "/z", "private": True}, "d-personality": {"root": "/w", "private": False}, "named_roots": {}}))
    assert b.expected_private(reg) == 2 and b.expected_consulted(reg) == 2


def test_the_find_write_and_list_modes_refuse_to_measure_before_ready(tmp_path, monkeypatch):
    """Round 29b (the bench's own error). The persistent servers of the find, write and list modes were started with the handshake only. The release's handshake waits for every corpus; the new tree's
    returns at once, so the warm-up and the first measured finds ran INSIDE the new tree's first pass and read as a +1 ms p95 'regression'. They are now started with `start_ready()` and refused
    (`NotReady`) before any measurement unless the readiness find is valid. Planted: a start that returns after the handshake only is refused. Red on d326bff (no such function)."""
    b = _bench()
    reg = b.gen(tmp_path / "gen", corpora=3, two_root=1, files=1, nodes=3)
    trees = {"old": REPO, "new": REPO}
    bases = {"old": tmp_path / "gen", "new": tmp_path / "gen"}
    regs = {"old": reg, "new": reg}
    lines = []
    srv = b.start_ready_servers(trees, bases, regs, say=lines.append)
    try:
        assert set(srv) == {"old", "new"} and all(s.last_check and s.last_check["complete"] is True and s.last_check["consulted"] == 3 for s in srv.values()), [s.last_check for s in srv.values()]
        assert len(lines) == 2 and all("READY after" in ln and "before warm-up and measurement" in ln and ln.rstrip().endswith("valid") for ln in lines), lines
    finally:
        for s in srv.values():
            s.close()
    # planted: a start that waits for the handshake only (no readiness find ever ran)
    real_start = b.Srv.start

    def handshake_only(self):
        self.start()
        return 0.0, 0.0
    monkeypatch.setattr(b.Srv, "start_ready", handshake_only)
    planted = b.Srv.start
    with pytest.raises(b.NotReady):
        b.start_ready_servers(trees, bases, regs, say=lambda *_a: None)
    assert planted is real_start


def test_the_bench_find_mode_starts_its_servers_through_the_ready_gate():
    """The find, write and list modes get their servers from `start_ready_servers` and from nowhere else: no `s.start()` on a measured server remains in `main`."""
    import inspect
    b = _bench()
    src = inspect.getsource(b.main)
    assert "start_ready_servers(" in src
    after = src[src.index("start_ready_servers("):]
    assert ".start()" not in after, "a measured server must not be started with the handshake only"
    assert "--boot-window-finds" in src, "the optional report is wired"


def test_the_find_study_refuses_to_measure_before_ready(tmp_path, monkeypatch):
    """The find STUDY (interleaved, identical-code control, planted delay) starts every server through the READY gate: a start that returns after the handshake only is refused with `NotReady` before
    any call is measured."""
    b = _bench()

    def handshake_only(self):
        self.start()
        return 0.0, 0.0
    monkeypatch.setattr(b.Srv, "start_ready", handshake_only)
    with pytest.raises(b.NotReady):
        b.find_pairs(REPO, REPO, 1, 5, "t", tmp_path)


def test_an_undetected_plant_makes_the_find_study_undecidable_and_a_detected_one_follows_the_interval_rule():
    """The reading: the PLANT must be caught (whole interval above zero, point at least half the planted delay) or the verdict is UNDECIDABLE whatever the real interval says; with the plant
    caught, PASS only if the whole real interval is under the 2 ms limit, FAIL only if the whole interval is over it, else UNDECIDABLE. Planted cases for each."""
    b = _bench()
    ident = (-0.05, 0.0, 0.05)
    caught = (0.7, 1.0, 1.3)
    assert b.find_reading(ident, (-0.2, 0.1, 0.4), (0.1, 0.2, 0.3), 1.0)[0] == "UNDECIDABLE", "plant straddles zero"
    assert b.find_reading(ident, (0.0, 0.1, 0.2), (0.1, 0.2, 0.3), 1.0)[0] == "UNDECIDABLE", "plant above zero but far below the planted delay"
    assert b.find_reading(ident, (-0.1, 1.0, 1.2), (0.1, 0.2, 0.3), 1.0)[0] == "UNDECIDABLE", "plant's lower end below zero"
    word, lines = b.find_reading(ident, (-0.2, 0.1, 0.4), (0.1, 0.2, 0.3), 1.0)
    assert any("NOT DETECTED" in ln for ln in lines) and any("UNDECIDABLE" in ln and "planted delay was not detected" in ln for ln in lines)
    assert b.find_reading(ident, caught, (0.1, 0.2, 0.3), 1.0)[0] == "PASS", "whole interval under 2 ms"
    assert b.find_reading(ident, caught, (2.1, 2.5, 3.0), 1.0)[0] == "FAIL", "whole interval over 2 ms"
    assert b.find_reading(ident, caught, (1.4, 2.0, 2.6), 1.0)[0] == "UNDECIDABLE", "interval straddles 2 ms"
    assert b.find_reading(ident, caught, (0.1, 1.99, 2.0), 1.0)[0] == "PASS", "the interval's top may equal the limit"
    lines = b.find_reading(ident, caught, (0.1, 0.2, 0.3), 1.0)[1]
    assert any("noise floor (IDENTICAL code)" in ln for ln in lines)


def test_the_find_delta_interval_and_the_planted_delay_are_real(tmp_path):
    """The delta interval centres on a known difference, and the planted copy sleeps in `_find_exact`, the named-find path (every `lts_find` runs it)."""
    b = _bench()
    import random
    rnd = random.Random(3)
    old = [1.0 + rnd.uniform(0, 0.2) for _ in range(300)]
    new = [x + 1.0 for x in old]
    lo, mid, hi = b.find_delta_ci(old, new, 0.95)
    assert 0.95 < mid < 1.05 and lo <= mid <= hi
    dest = b.plant_find_delay(REPO, 1.0, tmp_path / "pl")
    src = (dest / "factlattice_engine" / "tools.py").read_text(encoding="utf-8")
    assert "_plant.sleep(0.001)" in src and src.count("_plant.sleep(") == 1 and "def _find_exact_unplanted" in src


def test_settle_ok_settles_a_pending_root_and_never_clears_an_unresponsive_one(tmp_path, monkeypatch):
    """Round 29c (x-mark-clears). A fused job's seat settles its root where the seat job used to end, but only from PENDING. A root that a job's overrun marked UNRESPONSIVE, with its recovery
    pass still queued, is cleared only by a probe begun after the mark: `settle_ok` must not do it, or a root shared by two corpora (or one that stalls during another corpus's merge) reads
    ok for up to a deadline. Red on 88c8f3b (it settled any state but ok)."""
    from factlattice_engine import rootstate
    _healthy_world(tmp_path, monkeypatch, n=2)
    reg = rootstate.process_registry()
    slot = reg.slot(str(tmp_path / "settle-root"), aux=True)
    with reg._cv:
        slot._state = rootstate.UNRESPONSIVE
        slot._since = reg.wall()
    slot.settle_ok()
    assert slot._state == rootstate.UNRESPONSIVE, "an unresponsive root is not settled by a seat"
    with reg._cv:
        slot._state = rootstate.PENDING
    slot.settle_ok()
    assert slot._state == rootstate.OK, "a pending root is"
    slot.settle_ok()
    assert slot._state == rootstate.OK


def test_a_seat_landing_after_a_roots_overrun_leaves_the_root_unresponsive(tmp_path, monkeypatch):
    """Round 29c, through a real boot. The root of c01 is marked unresponsive (as an overrun does) just before its corpus's seat. After the first pass the root still reads unresponsive: the seat
    settled nothing. (A write under it is refused as for any unresponsive root, and a named read's verify poll decides; neither can attach by an unconfirmed ok.)"""
    from factlattice_engine import kernel as K
    from factlattice_engine import rootstate, server
    w = _healthy_world(tmp_path, monkeypatch, n=3)
    real_seat = K.FreshnessKernel.seat_partition

    def marked_seat(self, part):
        if part.corpus_id == "c01":
            slot = self.roots.slot(os.path.abspath(str(w.root("c01"))), create=False)
            with self.roots._cv:
                slot._state = rootstate.UNRESPONSIVE
                slot._since = self.roots.wall()
        return real_seat(self, part)
    monkeypatch.setattr(K.FreshnessKernel, "seat_partition", marked_seat)
    eng = server.build_tools(watch=False, async_boot=True)
    assert eng.boot_loader.first_pass_done.wait(60)
    assert eng.store.kernel.roots.state_of(os.path.abspath(str(w.root("c01")))) == rootstate.UNRESPONSIVE
    assert eng.store.kernel.roots.state_of(os.path.abspath(str(w.root("c00")))) == rootstate.OK, "control: another root settled as before"
    # Slice 5 (a delta read's P3-1): the root RECOVERS. Queue the recovery pass the kernel builds for a stuck root (a full probe begun now, so after the mark) and let a worker run it:
    # a probe begun after the mark is what clears an unresponsive root (x-mark-clears), so the root goes back to ok and its corpus is still served.
    c01 = os.path.abspath(str(w.root("c01")))
    slot = eng.store.kernel.roots.slot(c01, create=False)
    assert slot is not None and slot.recover is not None, "setup: the kernel owns the recovery of this root"
    job = slot.submit("full", slot.recover().fn, coalesce=False)
    assert job.done.wait(30), "the recovery pass ran"
    assert eng.store.kernel.roots.state_of(c01) == rootstate.OK, "a recovery pass begun after the mark returns the root to ok"
    assert "c01" in eng.store.corpus_ids()


def test_registry_is_accepted_with_the_find_study_and_its_servers_stay_read_only(tmp_path, monkeypatch):
    """Slice 5: `--registry` is accepted with `--find-study` (it was refused: boot study only), is passed through to `find_study`, and every server `find_pairs` starts for it is READ-ONLY
    (`Srv(read_only=True)`), the per-call guard of the test above. Control: `--registry` with no study flag is still refused."""
    b = _bench()
    got = {}
    monkeypatch.setattr(b, "find_study", lambda *a, **k: got.update(args=a, kwargs=k) or 0)
    reg = tmp_path / "reg.json"
    reg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["bench", "--registry", str(reg), "--find-study", "--old", str(REPO), "--new", str(REPO)])
    assert b.main() == 0 and got["kwargs"].get("registry") == reg, got
    monkeypatch.setattr(sys, "argv", ["bench", "--registry", str(reg), "--old", str(REPO), "--new", str(REPO)])
    with pytest.raises(SystemExit) as ei:
        b.main()
    assert ei.value.code == 2, "control: --registry with no study is refused"
    seen = []

    class _Stop(Exception):
        pass

    class _Srv:
        def __init__(self, code_root, base, registry, read_only=False, env_extra=None):
            seen.append(read_only)

        def start_ready(self):
            raise _Stop

    monkeypatch.setattr(b, "Srv", _Srv)
    reg.write_text(json.dumps({"c": {"roots": [{"path": str(tmp_path)}]}}), encoding="utf-8")
    with pytest.raises(_Stop):
        b.find_pairs(REPO, REPO, 1, 1, "t", tmp_path / "fp", reg)
    assert seen == [True], "a find-study server against --registry is read-only"
    seen.clear()
    with pytest.raises(_Stop):
        b.find_pairs(REPO, REPO, 1, 1, "t", tmp_path / "fp2")
    assert seen == [False], "control: with no registry the bench's own synthetic registry is used and is not read-only"
