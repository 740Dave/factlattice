"""[FL-BOUNDED-ROOTS] slice 2: probes off the kernel lock, the gate, and the report ([MECH-BR-PROBE], [MECH-BR-CALLS] x-gate,
[MECH-BR-REPORT] x-list and x-codes). Tests of what the slice DOES, as opposed to the red records of what the spec still
requires (tests/test_bounded_roots.py).

EVERY RESULT HERE IS ABOUT THE STALL MODEL (tests/stall_harness.py), never a real hung drive. In the real-server tests the
check that matters most is `no_request_thread_hit`: every disk call the model gated came from a pool worker, none from the
server's request thread, so the request path read nothing under the hung root. The harness records the calling thread of
each gated call, and a negative control (`test_the_request_thread_check_sees_a_request_thread_hit`) shows the check fires.

WHAT EACH TEST IS (round 2 of slice 2, after a cold read found 9 of the first 23 failed on the old tree only for a signature):
  BEHAVIOUR, shown red on bd18604 through the real-server or in-process engine form, where the old engine hangs or answers wrong for the
  reason the test names: the c4/x-verify/ROOT-SET-CHANGE/c7 server tests, the gate, stale_count and several-roots tests, the cross-corpus
  heal test, the K-hung-roots test, the failed-corpus branch test.
  PIN, labelled `pin` in its name or docstring and NOT claimed red-first: the stable-code pin, the one-registry tripwire, the SPEC absence
  test, the deadline-from-environment test, the root registry unit tests, the kernel unit tests that need the new kernel's API.
  GUARDS AGAINST A REGRESSION TO A WRONG RULE, shown red against a MUTANT of the new code rather than the old tree (the old engine has no
  marks to get wrong): the x-progress tests (a deadline measured from the job's start, or from a waiter's queueing, fails them) and the
  concurrency test.

NOT COVERED HERE (unrun, and named so): boot and build_store with a hung root (slice 4), writes and membership (slice 3),
calls on workers, call-overran and the re-exec rule (slice 5), a real drive (c10), render time (c12), a load run (c11).
"""
from __future__ import annotations

import ast
import json
import os
import threading
import time
from pathlib import Path

import pytest

from factlattice_engine import rootstate as RS
from factlattice_engine.kernel import FreshnessKernel
from tests.resolver_mode import needs_per_component_resolver
from tests.stall_harness import _DOC, HarnessError, NoReply, RealServer, RootStall, Workspace
from tests.test_bounded_roots import (
    CODE_UNRESPONSIVE,
    DEADLINES,
    SETTLE_S,
    Asker,
    Checks,
    _boot_then_stall,
    entry,
    ledger_records,
)

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    return Workspace(tmp_path / "w")


def request_thread_hits(srv: RealServer) -> list[dict]:
    """Gated calls made by anything but a pool worker: the request thread reading under the hung root."""
    return [h for h in srv.hits() if not str(h["thread"]).startswith("lts-root-pool")]


def assert_held(ask, ck) -> None:
    """The stall must have HELD, not merely been reached: the engine itself says b1's root is unresponsive. `require_hit` only proves a
    gated call was made; a harness whose gate logged the hit and returned without holding (mutant h1) left the tests that used it green."""
    listing, _ = ask("hold check: list", "lts_list_corpora", {"heal": False})
    b1 = entry(listing, "b1")
    ck.ok(b1 is not None and (b1.get("root_health") or {}).get("status") == "unresponsive",
          f"the stall did not HOLD: b1 is not reported unresponsive ({b1 and b1.get('root_health')})")


def test_the_request_thread_check_sees_a_request_thread_hit(ws):
    """NEGATIVE CONTROL for `request_thread_hits`: a call that reads under the hung root on the request thread must show up as a hit from
    MainThread. If this ever stops showing one, the check below can no longer tell a request-thread read from a worker's. It was a write to
    a healthy file (membership resolved every root, slice 2's tip); slice 3 closed that, so it is now `lts_lint` of the hung root's
    directory, which lists it on the request thread until slice 5 bounds calls."""
    srv = _boot_then_stall(ws)
    try:
        srv.engage()
        time.sleep(SETTLE_S)
        rid = srv.send_call("lts_lint", {"source": str(ws.root("b1"))})
        with pytest.raises(NoReply):
            srv.wait_reply(rid, 2.0)
        assert any(h["thread"] == "MainThread" for h in srv.hits()), srv.hits()[:5]
    finally:
        srv.close()


# ======================================================================================================================
# c4  release
# ======================================================================================================================
@needs_per_component_resolver
def test_c4_release_returns_the_root_to_ok_with_no_restart(ws):
    ck = Checks()
    srv = _boot_then_stall(ws)
    try:
        pid = srv.proc.pid
        ask = Asker(srv, ck)
        before = ws.file("b1").read_bytes()
        srv.engage()
        time.sleep(SETTLE_S)
        listing, _ = ask("list while stalled", "lts_list_corpora", {"heal": False})
        b1 = entry(listing, "b1")
        ck.ok(b1 is not None and (b1.get("root_health") or {}).get("status") == "unresponsive", f"b1 is unresponsive: {b1}")
        ck.ok(b1 is not None and b1.get("total_nodes") is None and b1.get("total_nodes_live") is None and b1.get("fresh") is False,
              "b1 carries null totals and fresh false")
        ck.ok(listing is not None and listing.get("stuck_workers") == 1, f"stuck_workers is 1: {listing and listing.get('stuck_workers')}")
        r, dt = ask("named b1 while stalled", "lts_find", {"corpus": "b1", "text": "lanternfish"})
        ck.ok(r is not None and r.get("success") is False and r.get("reason") == CODE_UNRESPONSIVE, f"refused root-unresponsive: {r}")
        # the file system itself is fine; only the CALL hangs. An edit made now is seen after release (the full re-evaluation).
        # The listing after the release heals (the default), so the corpus is served fresh over the edit, not merely unmarked.
        with open(ws.file("b1"), "a", encoding="utf-8") as fh:
            fh.write("- edited-during-stall: afterwards\n")
        srv.release()
        recovered = None
        end = time.monotonic() + 8
        while time.monotonic() < end and not ask.wedged:
            listing, _ = ask("list after release", "lts_list_corpora", {})
            b1 = entry(listing, "b1")
            if b1 and b1.get("total_nodes") is not None and not b1.get("root_health") and b1.get("fresh") is True:
                recovered = b1
                break
            time.sleep(0.2)
        ck.ok(recovered is not None, f"b1 returned to ok after the release, served fresh, with its node count: {b1}")
        r, _ = ask("named b1 after release", "lts_find", {"corpus": "b1", "text": "afterwards", "tier": "digest"})
        ck.ok(r is not None and r.get("success") and r.get("count") == 1, "b1 serves the edit made during the stall")
        ck.ok(listing is not None and "stuck_workers" not in listing, "no stuck worker is reported once the call returned")
        ck.ok(srv.alive() and srv.proc.pid == pid and srv.generation() == 1, "same process, no restart")
        ck.ok(ledger_records(ws) == [], "no ledger record appeared or was lost")
        ck.ok(ws.file("b1").read_bytes().startswith(before), "the file kept its bytes")
        srv.require_hit()
    finally:
        srv.close()
    ck.done("c4 (release)")


# ======================================================================================================================
# x-verify, ROOT-SET-CHANGE, private  (real server)
# ======================================================================================================================
@needs_per_component_resolver
def test_x_verify_a_named_read_after_an_edit_returns_it_during_another_roots_stall(ws):
    """x-verify and x-build-traps (4). The edit is made by the test directly: a TYPED write during a stall still stalls on
    membership (slice 3, see test_c2_write_during_stall_then_read), so the write-then-read pair cannot be asked through the
    server yet. What this proves is the read half: a named read of an ok root verifies on its own root's worker and starts
    from a probe begun after the edit, with another root hung, and the request thread reads nothing under the hung root."""
    ck = Checks()
    srv = _boot_then_stall(ws)
    try:
        ask = Asker(srv, ck)
        srv.engage()
        time.sleep(SETTLE_S)
        assert_held(ask, ck)
        for i in range(4):
            tok = f"verifytoken{i}"
            with open(ws.file("a1"), "a", encoding="utf-8") as fh:
                fh.write(f"- note{i}: {tok}\n")
            r, dt = ask(f"find {tok}", "lts_find", {"corpus": "a1", "text": tok, "tier": "digest"})
            ck.ok(r is not None and r.get("success") and r.get("count") == 1, f"a named find right after the edit returns it ({tok}): {r and r.get('count')}")
            n, _ = ask(f"node {i}", "lts_node", {"corpus": "a1", "node_id": "A1-N"})
            ck.ok(n is not None and tok in json.dumps(n), f"a named node read right after the edit returns it ({tok})")
        srv.require_hit()
        bad = request_thread_hits(srv)
        ck.ok(not bad, f"the request thread read under the hung root: {bad[:3]}")
    finally:
        srv.close()
    ck.done("x-verify (read after an edit, another root hung)")


@needs_per_component_resolver
def test_root_set_change_is_still_seen_during_a_stall(ws):
    """[ROOT-SET-CHANGE] keeps working with the root-set check moved onto the probes: a registered root that APPEARS while
    another root is hung makes its corpus not fresh, the corpus heals and serves the new root, and the control before the root
    existed returned nothing from it."""
    late = ws.base / "c" / "a1late"
    reg = dict(ws.registry)
    reg["a1"] = {"roots": [{"path": str(ws.root("a1"))}, {"path": str(late)}], "domain_suffix": "x.a1"}
    ws.registry_path.write_text(json.dumps(reg), encoding="utf-8")
    ck = Checks()
    srv = _boot_then_stall(ws)
    try:
        ask = Asker(srv, ck)
        r, _ = ask("control: zebrafish before the root exists", "lts_find", {"corpus": "a1", "text": "zebrafish", "tier": "digest"})
        ck.ok(r is not None and r.get("success") and r.get("count") == 0, "control: nothing from the absent root")
        srv.engage()
        time.sleep(SETTLE_S)
        assert_held(ask, ck)
        late.mkdir()
        (late / "late.lts").write_text(_DOC.format(c="a1", u="A1L").replace("lanternfish", "zebrafish"), encoding="utf-8")
        r, dt = ask("zebrafish after the root appeared", "lts_find", {"corpus": "a1", "text": "zebrafish", "tier": "digest"}, 8.0)
        ck.ok(r is not None and r.get("success") and r.get("count") == 1,
              f"the root that appeared during the stall is served (not fresh, healed): {r and (r.get('count'), r.get('reason'))}")
        srv.require_hit()
        bad = request_thread_hits(srv)
        ck.ok(not bad, f"the request thread read under the hung root: {bad[:3]}")
    finally:
        srv.close()
    ck.done("ROOT-SET-CHANGE during a stall")


@needs_per_component_resolver
def test_c7_a_private_root_hung_names_nothing(ws):
    """c7 for what this slice touches: the gate refusal, the listing, the corpus-omitted read, and the engine's log and state
    folder. The control inside it: the same scan finds the PUBLIC root's path where the listing gives it, so the scan can see
    a path at all. The claim is: no private id and no path reaches a caller who did NOT name the corpus. A call that names it echoes the id the caller typed (whole-slice
    read of 4384356, F5), as at the release for a healthy private corpus; this test asks only the unnamed surfaces."""
    reg = dict(ws.registry)
    reg["b1"] = {**reg["b1"], "private": True}
    ws.registry_path.write_text(json.dumps(reg), encoding="utf-8")
    ck = Checks()
    srv = _boot_then_stall(ws)
    try:
        ask = Asker(srv, ck)
        secret = str(ws.root("b1"))
        srv.engage()
        time.sleep(SETTLE_S)
        unnamed, _ = ask("unnamed find", "lts_find", {"text": "lanternfish", "tier": "digest"})
        named, _ = ask("named b1 find", "lts_find", {"corpus": "b1", "text": "lanternfish"})
        listing, _ = ask("list", "lts_list_corpora", {"heal": False})
        ck.ok(unnamed is not None and unnamed.get("success") and '"b1"' not in json.dumps(unnamed) and secret not in json.dumps(unnamed),
              "the corpus-omitted read names neither the private corpus nor its root")
        ck.ok(named is not None and named.get("reason") == CODE_UNRESPONSIVE and secret not in json.dumps(named),
              "the named refusal carries the code and no path")
        ck.ok(listing is not None and secret not in json.dumps(listing), "the listing gives no private root path")
        b1 = entry(listing, "b1")
        ck.ok(b1 is not None and b1.get("root_paths") == [] and (b1.get("root_health") or {}).get("status") == "unresponsive",
              f"the private entry has the status and no root paths: {b1}")
        ck.ok(listing is not None and str(ws.root("a1")) in json.dumps(listing),
              "CONTROL: the scan sees the public root's path where the listing gives it")
        srv.require_hit()
        leaked = [str(f) for d in (ws.state, ws.home) for f in Path(d).rglob("*")
                  if f.is_file() and secret.encode() in f.read_bytes()]
        ck.ok(not leaked, f"the private root path is in the engine's state folder: {leaked[:3]}")
        ck.ok(secret not in srv.stderr_text(), "the private root path is in the engine's log")
    finally:
        srv.close()
    ck.done("c7 (private root hung)")


# ======================================================================================================================
# in-process: the gate, the listing, a multi-root corpus
# ======================================================================================================================
def _engine(ws: Workspace, monkeypatch, registry=None, deadline="0.4"):
    if registry is not None:
        ws.registry_path.write_text(json.dumps(registry), encoding="utf-8")
    for k, v in ws.env({"LTS_ROOT_DEADLINE_S": deadline}).items():
        monkeypatch.setenv(k, v)
    from factlattice_engine.server import build_tools
    try:
        # a server reads LTS_ROOT_DEADLINE_S once, at start, for the kernel's probes AND for the bounded machinery of `boundedfs`; the latter is
        # process-wide and created at first use, so a test process that used it before this env was set would keep the earlier deadline (the
        # 10 s default) and a test that waits on it would see 10 s, not this one. Restart it with the same deadline, as a new server would have.
        from factlattice_engine import boundedfs
        boundedfs._reset(float(deadline))
    except ImportError:                                  # absent on the tree before slice 3
        pass
    return build_tools(watch=False, require_corpora=True)


@needs_per_component_resolver
def test_gate_refuses_a_marked_corpus_without_touching_its_disk_and_serves_the_rest(ws, monkeypatch):
    from factlattice_engine import tools as T
    eng = _engine(ws, monkeypatch)
    ok0 = T.dispatch("lts_find", {"corpus": "b1", "text": "lanternfish", "tier": "digest"}, tools=eng)
    assert ok0["success"] and ok0["count"] == 1, "control: b1 reads before the stall"
    with RootStall(ws.root("b1"), cap_s=20) as stall:
        try:
            t0 = time.monotonic()
            first = T.dispatch("lts_find", {"corpus": "b1", "text": "lanternfish"}, tools=eng)
            waited = time.monotonic() - t0
            assert first["success"] is False and first["reason"] == CODE_UNRESPONSIVE, first
            assert 0.3 <= waited < 3, f"the first call waits for the root's deadline (0.4 s) and then refuses: {waited:.2f}s"
            assert stall.hit_count > 0
            hits = stall.hit_count
            t0 = time.monotonic()
            again = T.dispatch("lts_find", {"corpus": "b1", "text": "lanternfish"}, tools=eng)
            node = T.dispatch("lts_node", {"corpus": "b1", "node_id": "B1-N"}, tools=eng)
            listing = T.dispatch("lts_list_corpora", {"heal": False}, tools=eng)
            elapsed = time.monotonic() - t0
            assert again["reason"] == CODE_UNRESPONSIVE and node["reason"] == CODE_UNRESPONSIVE
            assert elapsed < 0.25, f"later calls read the mark from memory: {elapsed:.2f}s"
            assert stall.hit_count == hits, "a marked corpus's calls and the listing made no disk call under its root"
            assert again["root_health"]["status"] == "unresponsive" and again["root_health"]["roots"] == 1
            assert "error" in again and "clears" in again["error"] and str(ws.root("b1")) not in json.dumps(again)
            b1 = entry(listing, "b1")
            assert b1["total_nodes"] is None and b1["total_nodes_live"] is None and b1["fresh"] is False
            assert b1["root_health"]["status"] == "unresponsive" and set(b1["root_health"]) == {"status", "roots", "since"}
            assert listing["stuck_workers"] == 1 and "b1" in listing["stale_corpora"]
            assert any(s.get("corpus") == "b1" and s.get("reason") == "root-unresponsive" for s in listing["extent"]["corpora_skipped"])
            a1 = entry(listing, "a1")
            assert a1["total_nodes"] == 2 and a1["fresh"] is True and "root_health" not in a1, a1
            ok1 = T.dispatch("lts_find", {"corpus": "a1", "text": "lanternfish", "tier": "digest"}, tools=eng)
            assert ok1["success"] and ok1["count"] == 1
            un = T.dispatch("lts_find", {"text": "lanternfish", "tier": "digest"}, tools=eng)
            assert un["success"] and un["complete"] is False
            assert {"corpus": "b1", "reason": "root-unresponsive"} in un["extent"]["corpora_skipped"]
            assert un["extent"]["index"]["fresh"] is False
            legacy = T.dispatch("lts_find", {"text": "lanternfish"}, tools=eng)
            assert legacy["success"] and legacy["complete"] is False
            prose = T.dispatch("lts_find_in_prose", {"text": "lanternfish"}, tools=eng)
            assert prose["success"] and prose["complete"] is False
            assert stall.hit_count == hits, "a1 reads and the corpus-omitted reads made no disk call under the hung root"
        finally:
            stall.release()
            time.sleep(0.3)


def test_stale_count_in_search_scope_counts_the_marked_corpus(ws, monkeypatch):
    from factlattice_engine import tools as T
    monkeypatch.setenv("LTS_DEFAULT_CORPORA", "a1,b1")
    eng = _engine(ws, monkeypatch)
    with RootStall(ws.root("b1"), cap_s=20) as stall:
        try:
            T.dispatch("lts_find", {"corpus": "b1", "text": "x"}, tools=eng)       # marks it
            res = T.dispatch("lts_find", {"text": "lanternfish", "tier": "digest"}, tools=eng)
            assert res["success"], res
            assert res["extent"]["search_scope"].get("stale_count") == 1, res["extent"]["search_scope"]
            assert res["count"] == 1
        finally:
            stall.release()
            time.sleep(0.3)


def test_a_corpus_with_several_roots_is_affected_if_any_root_is_marked(ws, monkeypatch):
    from factlattice_engine import tools as T
    second = ws.base / "c" / "a1b"
    second.mkdir()
    (second / "a1b.lts").write_text(_DOC.format(c="a1", u="A1B"), encoding="utf-8")
    reg = dict(ws.registry)
    reg["a1"] = {"roots": [{"path": str(ws.root("a1"))}, {"path": str(second)}], "domain_suffix": "x.a1"}
    eng = _engine(ws, monkeypatch, registry=reg)
    assert T.dispatch("lts_find", {"corpus": "a1", "text": "lanternfish", "tier": "digest"}, tools=eng)["count"] == 2, "control: both roots serve"
    with RootStall(second, cap_s=20) as stall:
        try:
            T.dispatch("lts_find", {"corpus": "a1", "text": "x"}, tools=eng)            # waits to the deadline and marks
            r = T.dispatch("lts_find", {"corpus": "a1", "text": "lanternfish"}, tools=eng)
            assert r["success"] is False and r["reason"] == CODE_UNRESPONSIVE, "a partial answer would read as whole"
            assert r["root_health"]["roots"] == 1
            ok = T.dispatch("lts_find", {"corpus": "b1", "text": "lanternfish", "tier": "digest"}, tools=eng)
            assert ok["success"] and ok["count"] == 1
        finally:
            stall.release()
            time.sleep(0.3)


# ======================================================================================================================
# the kernel: no disk under the lock; one probe; recovery; x-verify
# ======================================================================================================================
def _lts(tag: str) -> str:
    return f'---\nlts: "1.1"\ntitle: t\ndomain: x.k\ndoctype: SPEC\n---\n\n# R [K-ROOT]\n\n- summary: r\n\n## N [K-{tag}]\n\n- topic: {tag}\n'


def _kernel_world(tmp_path, *, lister=None, hasher=None, deadline=0.3):
    from factlattice_engine import config
    root = tmp_path / "k"
    root.mkdir(parents=True)
    (root / "k.lts").write_text(_lts("one"), encoding="utf-8")
    store = config.build_store({"k": {"roots": [{"path": str(root)}], "domain_suffix": "x.k"}})
    kw = {}
    if lister:
        kw["lister"] = lister
    if hasher:
        kw["file_hasher"] = hasher
    k = FreshnessKernel(root_deadline_s=deadline, **kw)
    k.seat_store(store)
    store.kernel = k
    return k, store.get_partition("k"), root


def _free_lock_probe(tmp_path, make_lister):
    """Seat a kernel with a lister that blocks, start a poll on another thread, and ask whether the kernel lock can be taken."""
    from factlattice import list_lts_files
    gate = threading.Event()
    holder = {}
    armed = threading.Event()

    def lister(root):
        if armed.is_set():
            make_lister(holder["k"], gate)
        return list_lts_files(root)

    k, part, _root = _kernel_world(tmp_path, lister=lister, deadline=3.0)
    holder["k"] = k
    armed.set()
    t = threading.Thread(target=lambda: k.poll_once(part), daemon=True)
    t.start()
    time.sleep(0.2)                                # the probe is now inside the lister, on its worker
    got = k._lock.acquire(timeout=0.5)
    if got:
        k._lock.release()
    gate.set()
    t.join(5)
    return got


def test_the_kernel_lock_is_free_while_a_probe_blocks_in_a_listing(tmp_path):
    assert _free_lock_probe(tmp_path / "ok", lambda k, gate: gate.wait(5)) is True


def test_negative_control_a_lister_that_holds_the_lock_is_seen_holding_it(tmp_path):
    def hold(k, gate):
        k._lock.acquire()
        try:
            gate.wait(5)
        finally:
            k._lock.release()
    # the same observation as above on the OLD shape: the lock is NOT free, so the check can tell the two apart
    assert _free_lock_probe(tmp_path / "held", hold) is False


def test_at_most_one_probe_per_root_across_many_polls_and_a_stuck_one_costs_one_thread(tmp_path):
    from factlattice import list_lts_files
    gate = threading.Event()
    calls = []
    armed = threading.Event()

    def lister(root):
        if armed.is_set():
            calls.append(threading.current_thread().name)
            gate.wait(10)
        return list_lts_files(root)

    k, part, _root = _kernel_world(tmp_path, lister=lister, deadline=0.2)
    before = {t.ident for t in threading.enumerate()}
    armed.set()
    for _ in range(60):
        k.tick()
    new = [t for t in threading.enumerate() if t.ident not in before]
    assert len(calls) == 1, f"one probe was ever started, however many polls: {len(calls)}"
    assert len(new) <= 1, [t.name for t in new]
    assert k.stuck_workers() == 1
    assert k.root_mark(part)["status"] == "unresponsive"
    gate.set()
    end = time.monotonic() + 5
    while k.root_mark(part) is not None and time.monotonic() < end:
        time.sleep(0.02)
    assert k.root_mark(part) is None and k.stuck_workers() == 0, "the root is ok again with no restart"


def test_when_the_stuck_call_returns_the_root_is_re_evaluated_in_full(tmp_path):
    from factlattice import list_lts_files
    gate = threading.Event()
    armed = threading.Event()

    def lister(root):
        if armed.is_set() and not gate.is_set():
            gate.wait(10)
        return list_lts_files(root)

    k, part, root = _kernel_world(tmp_path, lister=lister, deadline=0.2)
    old = k.current_root(part)
    armed.set()
    k.tick()
    assert k.root_mark(part)["status"] == "unresponsive"
    (root / "k.lts").write_text(_lts("two"), encoding="utf-8")        # changed while the call was stuck
    gate.set()
    end = time.monotonic() + 5
    while k.root_mark(part) is not None and time.monotonic() < end:
        time.sleep(0.02)
    assert k.root_mark(part) is None
    assert k.current_root(part) != old, "the full re-evaluation saw the edit made during the stall; the stuck probe's old view was not published"


def test_x_verify_a_poll_never_joins_a_probe_that_began_before_the_write(tmp_path):
    """x-verify, with TWO files so that joining the running probe cannot hide: probe P1 has already passed a.lts and is held inside b.lts
    when a.lts is REWRITTEN. A poll that joins P1 returns P1's view of a.lts (the pre-write one); a poll that starts a probe after the
    write sees it. (With one file, `_hash_stable` re-hashes the same file after the write and the join is masked: the first version of
    this test stayed green under a mutant that joins the running job.)"""
    from factlattice import file_content_hash
    from factlattice_engine import config
    gate, armed, reading = threading.Event(), threading.Event(), threading.Event()

    def hasher(path):
        h = file_content_hash(path)
        if armed.is_set() and not gate.is_set() and path.endswith("b.lts"):
            reading.set()
            gate.wait(10)
        return h

    root = tmp_path / "xv"
    root.mkdir()
    (root / "a.lts").write_text(_lts("A1"), encoding="utf-8")
    (root / "b.lts").write_text(_lts("B1"), encoding="utf-8")
    store = config.build_store({"k": {"roots": [{"path": str(root)}], "domain_suffix": "x.k"}})
    k = FreshnessKernel(root_deadline_s=5.0, file_hasher=hasher)
    k.seat_store(store)
    part = store.get_partition("k")
    old = k.current_root(part)
    for f in ("a.lts", "b.lts"):
        os.utime(root / f, (1, 1))                 # the next poll re-hashes both
    armed.set()
    first = threading.Thread(target=lambda: k.poll_once(part), daemon=True)
    first.start()                                   # P1 passes a.lts, then holds inside b.lts
    assert reading.wait(5), "P1 never reached b.lts"
    (root / "a.lts").write_text(_lts("A2"), encoding="utf-8")        # THE WRITE: P1 already passed a.lts
    threading.Timer(0.4, gate.set).start()
    changed = k.poll_once(part)                     # a read after the write: must start a probe after it
    first.join(5)
    assert changed is True and k.current_root(part) != old, "a read after a write returned the write"


# ======================================================================================================================
# rootstate: the registry on its own
# ======================================================================================================================
def test_pin_rootstate_coalescing_rule_queued_joins_running_does_not():
    reg = RS.RootRegistry(5.0)
    s = reg.slot("/r")
    gate = threading.Event()
    started = threading.Event()

    def slow():
        started.set()
        gate.wait(5)

    j1 = s.submit("poll", slow)
    assert started.wait(2)
    j2 = s.submit("poll", lambda: None)             # j1 is RUNNING: it began before this request, so this one queues
    j3 = s.submit("poll", lambda: None)             # j2 is queued, not started: joins it
    assert j2 is not j1 and j3 is j2
    gate.set()
    assert j2.done.wait(2) and j1.done.is_set()


def test_pin_rootstate_overrun_marks_discards_and_recovers():
    reg = RS.RootRegistry(0.15)
    s = reg.slot("/r")
    s.recover = lambda: RS.Job("full", lambda: committed.append("recovery") or (lambda: committed.append("recovery-commit")))
    committed: list[str] = []
    gate = threading.Event()
    job = s.submit("poll", lambda: (gate.wait(5), (lambda: committed.append("stuck-commit")))[1])
    time.sleep(0.4)
    assert s.state() == RS.UNRESPONSIVE and reg.stuck_workers() == 1
    mark = reg.mark_for(["/r"])
    assert mark["status"] == "unresponsive" and mark["roots"] == 1 and set(mark) == {"status", "roots", "since"}
    gate.set()
    assert job.done.wait(2) and job.overran
    end = time.monotonic() + 3
    while s.state() != RS.OK and time.monotonic() < end:
        time.sleep(0.01)
    assert s.state() == RS.OK and reg.stuck_workers() == 0
    assert "stuck-commit" not in committed, "the overrun probe's result describes an old moment and is not published"
    assert "recovery-commit" in committed


def test_pin_rootstate_a_pending_root_is_not_ok_until_its_first_read_and_pending_past_the_deadline_is_unresponsive():
    reg = RS.RootRegistry(0.15)
    s = reg.slot("/r")
    assert s.state() == RS.PENDING and reg.mark_for(["/r"])["status"] == "pending"
    gate = threading.Event()
    s.submit("seat", lambda: gate.wait(5), coalesce=False, keep_on_overrun=True)
    time.sleep(0.4)
    assert s.state() == RS.UNRESPONSIVE
    gate.set()
    end = time.monotonic() + 2
    while s.state() != RS.OK and time.monotonic() < end:
        time.sleep(0.01)
    assert s.state() == RS.OK, "a seat read is kept even when it ran long (nothing older to protect) and the root is then ok"


def test_pin_the_deadline_is_read_once_from_the_environment_and_a_bad_value_is_the_default(monkeypatch):
    # [MECH-BR-BOOT] ONE registry per PROCESS: the deadline is read once, when it is first made. A test that needs another value starts the
    # process registry over through the one helper (conftest does so between tests), as a new process would.
    monkeypatch.setenv(RS.ROOT_DEADLINE_ENV, "2.5")
    RS.reset_process_registry()
    assert FreshnessKernel().roots.deadline_s == 2.5
    monkeypatch.setenv(RS.ROOT_DEADLINE_ENV, "7")
    assert FreshnessKernel().roots.deadline_s == 2.5, "read once per process: a later change of the variable is not seen"
    for bad in ("", "abc", "0", "-3"):
        monkeypatch.setenv(RS.ROOT_DEADLINE_ENV, bad)
        RS.reset_process_registry()
        assert FreshnessKernel().roots.deadline_s == RS.DEFAULT_ROOT_DEADLINE_S
    monkeypatch.delenv(RS.ROOT_DEADLINE_ENV)
    assert RS.DEFAULT_ROOT_DEADLINE_S == 10.0


# ======================================================================================================================
# codes, one registry
# ======================================================================================================================
def test_pin_the_five_stable_codes_and_that_they_are_fixed_codes():
    from factlattice_engine.reason_codes import is_code
    assert RS.STABLE_CODES == ("root-unresponsive", "call-overran", "engine-saturated", "corpus-root-unresponsive", "corpus-load-error", "corpus-entry-shape", "corpus-verdict-pending")
    assert all(is_code(c) for c in RS.STABLE_CODES)
    assert RS.STATES == ("ok", "pending", "unresponsive")
    assert RS.SKIP_REASON == {"pending": "root-pending", "unresponsive": "root-unresponsive", "verdict-pending": "corpus-verdict-pending"}


_STATE_WORDS = {"pending", "unresponsive", "root-unresponsive", "root-pending", "call-overran", "engine-saturated", "corpus-root-unresponsive", "verdict-pending", "corpus-verdict-pending"}
#: the one place besides rootstate.py that may hold one word: the core cannot import the engine, so the code its ledger and writers need is
#: defined there and rootstate takes it. Named by its FULL module path, so a file of the same name elsewhere is not exempt.
_STATE_WORD_EXEMPT = {("factlattice/rootview.py", "root-unresponsive"), ("factlattice/rootview.py", "corpus-verdict-pending")}


def _state_word_offenders(repo: Path):
    offenders = []
    for pkg in ("factlattice", "factlattice_engine"):
        for f in (repo / pkg).rglob("*.py"):
            rel = f.relative_to(repo).as_posix()
            if rel == "factlattice_engine/rootstate.py":
                continue
            for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in _STATE_WORDS:
                    if (rel, n.value) in _STATE_WORD_EXEMPT:
                        continue
                    offenders.append((rel, n.lineno, n.value))
    return offenders


def test_pin_root_state_words_live_in_one_module():
    """[ROOT-STATE-ONE-REGISTRY] no other engine module compares against, or builds, the state or skip words as a literal; they
    take them from rootstate. A TRIPWIRE (it reads string constants, not names built at run time)."""
    assert not _state_word_offenders(REPO), _state_word_offenders(REPO)
    from factlattice import rootview
    assert RS.CODE_ROOT_UNRESPONSIVE is rootview.CODE_ROOT_UNRESPONSIVE, "one definition of the word, not two that agree"


def test_the_exemption_is_by_full_module_path_not_by_file_name(tmp_path):
    """A second definition of the code in a file that merely shares the exempt file's NAME is an offender; the real one is not."""
    import shutil
    for pkg in ("factlattice", "factlattice_engine"):
        shutil.copytree(REPO / pkg, tmp_path / pkg, ignore=shutil.ignore_patterns("__pycache__"))
    assert _state_word_offenders(tmp_path) == []
    (tmp_path / "factlattice_engine" / "rootview.py").write_text('X = "root-unresponsive"\n', encoding="utf-8")
    assert _state_word_offenders(tmp_path) == [("factlattice_engine/rootview.py", 1, "root-unresponsive")]
    (tmp_path / "factlattice_engine" / "rootview.py").unlink()
    (tmp_path / "factlattice" / "rootview.py").write_text('X = "pending"\n', encoding="utf-8")
    assert _state_word_offenders(tmp_path) == [("factlattice/rootview.py", 1, "pending")], "and only that one word is exempt there"


def test_the_registry_tripwire_goes_red_on_a_planted_literal(tmp_path):
    planted = tmp_path / "x.py"
    planted.write_text('def f(s):\n    return s == "unresponsive"\n', encoding="utf-8")
    hits = [n for n in ast.walk(ast.parse(planted.read_text())) if isinstance(n, ast.Constant) and n.value == "unresponsive"]
    assert len(hits) == 1


def test_pin_spec_md_states_the_states_the_codes_and_the_fields():
    text = (REPO / "SPEC.md").read_text(encoding="utf-8")
    for needle in ("root-unresponsive", "root_health", "stuck_workers", "LTS_ROOT_DEADLINE_S", "pending", "unresponsive"):
        assert needle in text, f"SPEC.md does not state {needle!r}"
    # absence: the sentence that says what the slice does NOT yet do must stay until its slice lands, so it cannot be dropped silently
    assert "calls are not yet run on workers" in text


# ======================================================================================================================
# the failed-corpus path that assumed a file path (x-build-traps 6)
# ======================================================================================================================
def test_a_failed_corpus_with_the_build_root_code_has_its_own_branch_and_no_path(ws, monkeypatch):
    """Nothing emits `corpus-root-unresponsive` yet (the build_store slice does). A planted failure record, with NO `path` key
    as that slice will write it, must reach every reader of `failed_corpora` that used to assume a path. Control: a planted
    corpus-file-* failure still reads its path."""
    from factlattice_engine import tools as T
    eng = _engine(ws, monkeypatch)
    eng.store._failed_corpora["gone"] = {"code": RS.CODE_CORPUS_ROOT_UNRESPONSIVE, "private": False}
    eng.store._failed_corpora["gone-personality"] = {"code": RS.CODE_CORPUS_ROOT_UNRESPONSIVE, "private": True}
    eng.store._failed_corpora["badfile"] = {"code": "corpus-file-unreadable", "path": "x.lts", "private": False}
    named = T.dispatch("lts_find", {"corpus": "gone", "text": "x"}, tools=eng)
    assert named["success"] is False and named["reason"] == "corpus-root-unresponsive" and named["corpus"] == "gone"
    assert "path" not in named and "member" not in named["error"] and "Fix or remove that file" not in named["error"]
    assert "restart" in named["error"] or "lts_register_corpus" in named["error"]
    pathmode = T._corpus_file_refusal(None, eng.store._failed_corpora["gone"])
    assert pathmode["reason"] == "corpus-root-unresponsive" and "corpus" not in pathmode and "gone" not in json.dumps(pathmode)
    listing = T.dispatch("lts_list_corpora", {"heal": False}, tools=eng)
    assert {"corpus_id": "gone", "code": "corpus-root-unresponsive"} in listing["failed_corpora"]
    assert listing["private_failed"] == 1 and "gone-personality" not in json.dumps(listing["failed_corpora"])
    assert listing["extent"]["index"]["fresh"] is False
    un = T.dispatch("lts_find", {"text": "lanternfish", "tier": "digest"}, tools=eng)
    assert un["success"] and "gone-personality" not in json.dumps(un)
    assert un["extent"]["index"]["fresh"] is False, "a corpus that failed on a root is a declared hole, never a clean answer"
    ctl = T.dispatch("lts_find", {"corpus": "badfile", "text": "x"}, tools=eng)
    assert ctl["reason"] == "corpus-file-unreadable" and ctl["path"] == "x.lts", "CONTROL: a member-file failure still names its file"


# ======================================================================================================================
# concurrency: probes, polls, full re-verifies and edits at once end in the disk's state
# ======================================================================================================================
def test_concurrent_polls_ticks_reverifies_and_edits_end_in_the_state_of_the_disk(tmp_path):
    """The commit rule (a copy is published only if its RootState is still the seated one and unchanged since it was copied) under
    load: four threads poll, tick and fully re-verify a kernel while the test edits a member, with a second corpus sharing nothing.
    When they stop, one more poll must leave the maintained root equal to a FRESH kernel's seat of the same bytes. The control:
    the comparison is against a different root before the last edit, so it can tell equal from unequal."""
    from factlattice_engine import config
    root = tmp_path / "s"
    root.mkdir()
    for i in range(6):
        (root / f"{i}.lts").write_text(_lts(f"n{i}"), encoding="utf-8")
    reg = {"s": {"roots": [{"path": str(root)}], "domain_suffix": "x.k"}}
    store = config.build_store(reg)
    k = FreshnessKernel(root_deadline_s=5.0, backstop_every=3)
    k.seat_store(store)
    store.kernel = k
    part = store.get_partition("s")
    errors: list[BaseException] = []
    stop = threading.Event()

    def worker(fn):
        def run():
            while not stop.is_set():
                try:
                    fn()
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                    return
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    ts = [worker(lambda: k.poll_once(part)), worker(lambda: k.poll_once(part)), worker(k.tick), worker(lambda: k.full_reverify(part))]
    first = k.current_root(part)
    for i in range(25):
        (root / f"{i % 6}.lts").write_text(_lts(f"edit{i}"), encoding="utf-8")
        time.sleep(0.01)
    stop.set()
    for t in ts:
        t.join(10)
    assert not errors, errors[:2]
    k.poll_once(part)
    k.full_reverify(part)
    mid = k.current_root(part)
    (root / "0.lts").write_text(_lts("final"), encoding="utf-8")
    k.poll_once(part)
    store2 = config.build_store(reg)
    k2 = FreshnessKernel(root_deadline_s=5.0)
    k2.seat_store(store2)
    assert k.current_root(part) == k2.current_root(store2.get_partition("s")), "the maintained root is the disk's state"
    assert mid != k.current_root(part) and first != mid, "CONTROL: the comparison can tell different roots apart"


# ======================================================================================================================
# round 2: the heal choke point, x-progress, the shared pool, K hung roots
# ======================================================================================================================
def _edge_world(ws: Workspace) -> None:
    """a1's node A1-N gets an edge into corpus b1 (cross-corpus reference by domain)."""
    with open(ws.file("a1"), "a", encoding="utf-8") as fh:
        fh.write("- depends-on: [x.b1:SPEC:B1-N]\n")


@needs_per_component_resolver
def test_a_routed_hop_into_a_hung_stale_corpus_is_declared_and_never_healed(ws):
    """P1 F1. A (healthy) has an edge to B; B is stale (its file changed and the watcher saw it) AND its root then hangs. The router healed B
    through the self-heal, which re-lists B's root; the heal choke point now refuses a marked corpus, whoever calls it. A's named
    read and traverse answer, the hop is DECLARED in routed_stale with the root reason, and the request thread read nothing under B."""
    _edge_world(ws)
    ck = Checks()
    srv = _boot_then_stall(ws)
    try:
        ask = Asker(srv, ck)
        ctl, _ = ask("control: node before the stall", "lts_node", {"corpus": "a1", "node_id": "A1-N"})
        ck.ok(ctl is not None and ctl.get("success") and "B1-N" in json.dumps(ctl.get("routed_nodes")),
              f"CONTROL: before the stall the hop into b1 is served: {ctl and list(ctl)}")
        with open(ws.file("b1"), "a", encoding="utf-8") as fh:
            fh.write("- changed-before-the-hang: yes\n")
        time.sleep(2.2)                                                  # the watcher polls: b1's maintained root moves, b1 is STALE
        srv.engage()
        time.sleep(SETTLE_S)                                             # ... and now its root hangs and is marked
        n, dt = ask("node a1", "lts_node", {"corpus": "a1", "node_id": "A1-N"}, 3.0)
        ck.ok(n is not None and n.get("success"), "A's node read answers while B is hung and stale")
        ck.ok(n is not None and any(r.get("corpus") == "b1" and r.get("reason") == CODE_UNRESPONSIVE
                                    for r in (n.get("routed_stale") or [])), f"the hop is declared withheld: {n and n.get('routed_stale')}")
        t, _ = ask("traverse a1", "lts_traverse", {"corpus": "a1", "start_node": "A1-N", "max_depth": 1}, 3.0)
        ck.ok(t is not None and t.get("success") and "root-unresponsive" in json.dumps(t.get("routed_stale")),
              f"the traverse answers and declares the hop: {t and t.get('routed_stale')}")
        srv.require_hit()
        bad = request_thread_hits(srv)
        ck.ok(not bad, f"the request thread read under the hung root: {bad[:3]}")
    finally:
        srv.close()
    ck.done("heal choke point (routed hop into a hung stale corpus)")


def test_pin_every_caller_of_heal_goes_through_the_marked_check():
    """The check is IN `SelfHealer.heal`, not at its callers; this pins that the callers that exist today are the gate and the router
    (a third one is a review finding) and that heal refuses a marked corpus before it takes any lock or reads a root."""
    import inspect

    from factlattice_engine import router, selfheal, tools
    callers = [m.__name__ for m in (router, tools) if ".heal(" in inspect.getsource(m)]
    assert sorted(callers) == ["factlattice_engine.router", "factlattice_engine.tools"]
    src = inspect.getsource(selfheal.SelfHealer.heal)
    assert src.index("self._marked(part)") < src.index("_lock_for")


def _slow_world(tmp_path, n_files, per_call_s, deadline):
    """A kernel whose hasher takes `per_call_s` per file: many SMALL calls, each returning (progress), a pass of n*per_call_s."""
    from factlattice import file_content_hash
    from factlattice_engine import config
    root = tmp_path / "slow"
    root.mkdir(parents=True)
    for i in range(n_files):
        (root / f"{i:02d}.lts").write_text(_lts(f"s{i}"), encoding="utf-8")
    slow = threading.Event()

    def hasher(path):
        if slow.is_set():
            time.sleep(per_call_s)
        return file_content_hash(path)

    store = config.build_store({"s": {"roots": [{"path": str(root)}], "domain_suffix": "x.k"}})
    k = FreshnessKernel(root_deadline_s=deadline, file_hasher=hasher)
    k.seat_store(store)
    store.kernel = k
    return k, store.get_partition("s"), root, slow


def test_x_progress_a_full_pass_longer_than_the_deadline_with_many_small_calls_is_never_marked(tmp_path):
    """x-progress (scaled: spec 1.5 s on a 1 s deadline). A full pass of 20 files at 40 ms each takes 0.8 s on a 0.5 s deadline;
    every call returns inside the deadline, so the root is never marked at any moment and the pass publishes. Guards against a
    deadline counted from the job's start. (Shown red against that mutant, not against the old tree: the old engine marks nothing.)"""
    k, part, root, slow = _slow_world(tmp_path, 20, 0.04, 0.5)
    old = k.current_root(part)
    for i in range(20):
        (root / f"{i:02d}.lts").write_text(_lts(f"changed{i}"), encoding="utf-8")
    slow.set()
    marks = []
    done = threading.Event()

    def watch():
        while not done.is_set():
            marks.append(k.root_mark(part))
            time.sleep(0.01)

    w = threading.Thread(target=watch, daemon=True)
    w.start()
    t0 = time.monotonic()
    changed = k.full_reverify(part)
    took = time.monotonic() - t0
    done.set()
    w.join(2)
    assert took > 0.6, f"the pass must outlast the deadline for this test to mean anything: {took:.2f}s"
    assert all(m is None for m in marks), [m for m in marks if m][:2]
    assert changed and k.current_root(part) != old, "the long pass published its result"


def test_x_progress_a_waiter_queued_behind_a_slow_probe_marks_nothing(tmp_path):
    """x-progress (scaled: spec 1.2 s probe on a 2 s deadline with a queue). Full passes of about 1 s on a 0.9 s deadline (16 files at 60 ms); a second
    caller arrives while the first runs, so its own wait (about 1.9 s from its request) outlasts the deadline, yet no call ever
    stopped: never marked, and both return. Guards against a waiter that marks when ITS patience runs out (red against that mutant)."""
    k, part, root, slow = _slow_world(tmp_path, 16, 0.06, 0.9)
    slow.set()
    marks, spans = [], []
    done = threading.Event()

    def watch():
        while not done.is_set():
            marks.append(k.root_mark(part))
            time.sleep(0.01)

    def full():
        t = time.monotonic()
        k.full_reverify(part)
        spans.append(time.monotonic() - t)

    w = threading.Thread(target=watch, daemon=True)
    w.start()
    t1 = threading.Thread(target=full)
    t1.start()
    time.sleep(0.1)
    t2 = threading.Thread(target=full)
    t2.start()
    t1.join(10)
    t2.join(10)
    done.set()
    w.join(2)
    assert len(spans) == 2 and max(spans) > 1.5, f"the second caller must wait longer than the deadline for this to mean anything: {spans}"
    assert all(m is None for m in marks), [m for m in marks if m][:2]


def test_x_progress_a_job_stopped_on_one_call_is_marked_one_deadline_after_it_stopped(tmp_path):
    """The other half of the rule: no progress for the deadline IS the mark. 5 files read quickly, then the hasher sticks on one call."""
    from factlattice import file_content_hash
    gate = threading.Event()
    stick = threading.Event()
    seen = []

    def hasher(path):
        if stick.is_set() and len(seen) >= 3:
            gate.wait(10)
        seen.append(path)
        return file_content_hash(path)

    from factlattice_engine import config
    root = tmp_path / "r"
    root.mkdir()
    for i in range(6):
        (root / f"{i}.lts").write_text(_lts(f"p{i}"), encoding="utf-8")
    store = config.build_store({"s": {"roots": [{"path": str(root)}], "domain_suffix": "x.k"}})
    k = FreshnessKernel(root_deadline_s=0.4, file_hasher=hasher)
    k.seat_store(store)
    part = store.get_partition("s")
    for i in range(6):
        (root / f"{i}.lts").write_text(_lts(f"q{i}"), encoding="utf-8")
    seen.clear()
    stick.set()
    t = threading.Thread(target=lambda: k.full_reverify(part), daemon=True)
    t0 = time.monotonic()
    t.start()
    while k.root_mark(part) is None and time.monotonic() - t0 < 3:
        time.sleep(0.01)
    marked_after = time.monotonic() - t0
    gate.set()
    t.join(5)
    assert 0.35 <= marked_after < 1.5, f"marked about one deadline (0.4 s) after the call stopped, not at once and not never: {marked_after:.2f}s"


def test_the_hash_of_one_big_file_reports_progress_per_64k_chunk(tmp_path):
    """What progress means inside a hash of one big file: it is read in 64 KiB chunks, and each chunk read reports progress. So a
    big file on a slow disk is marked only if a single 64 KiB read stalls for the deadline. The limit: one call is the grain."""
    import factlattice.hashing as H
    f = tmp_path / "big.lts"
    f.write_bytes(b"x" * (64 * 1024 * 5 + 10))
    calls = []
    prev = H.progress_hook
    H.progress_hook = lambda: calls.append(1)
    try:
        H.file_content_hash(str(f))
    finally:
        H.progress_hook = prev
    # 5 full chunks and the final empty read = 6 chunk reports, and (slice 4) one report each after the lstat, the open and the fstat that precede the
    # read: a read that goes call by call (0.3 s per call, under the deadline) must not look silent between them
    assert len(calls) == 6 + 3, len(calls)


def test_pin_an_idle_server_with_many_roots_holds_a_few_pool_threads(tmp_path):
    """x-no-pile-up: 30 roots, polled, hold at most the pool (4 by default) of worker threads, not one per root. (A pin of the pool
    size; the cost it guards was measured on the real server, see the bench.)"""
    from factlattice_engine import config
    reg = {}
    for i in range(30):
        r = tmp_path / f"c{i}"
        r.mkdir()
        (r / "a.lts").write_text(_lts(f"I{i}"), encoding="utf-8")
        reg[f"c{i}"] = {"roots": [{"path": str(r)}], "domain_suffix": f"x.c{i}"}
    store = config.build_store(reg)
    k = FreshnessKernel(root_deadline_s=5.0)
    k.seat_store(store)
    for _ in range(3):
        k.tick()
    assert 1 <= k.roots.thread_count() <= 4, k.roots.thread_count()     # this kernel's own pool (threads of earlier tests may still be leaving)
    assert not any("c0" in t.name or "c1" in t.name for t in threading.enumerate()), "thread names carry no root name"


def test_a_stuck_job_is_replaced_so_healthy_roots_keep_being_polled_and_threads_stay_bounded(tmp_path):
    from factlattice import list_lts_files
    from factlattice_engine import config
    gate = threading.Event()
    armed = threading.Event()
    hung = {str(tmp_path / f"c{i}") for i in range(3)}

    def lister(root):
        if armed.is_set() and root in hung:
            gate.wait(10)
        return list_lts_files(root)

    reg = {}
    for i in range(10):
        r = tmp_path / f"c{i}"
        r.mkdir()
        (r / "a.lts").write_text(_lts(f"I{i}"), encoding="utf-8")
        reg[f"c{i}"] = {"roots": [{"path": str(r)}], "domain_suffix": f"x.c{i}"}
    store = config.build_store(reg)
    k = FreshnessKernel(root_deadline_s=0.3, lister=lister, pool_size=2)
    k.seat_store(store)
    armed.set()
    try:
        for _ in range(4):
            k.tick()
        # 3 roots hung with a pool of 2: without a replacement per stuck job the 7 healthy roots would never be polled
        (tmp_path / "c9" / "a.lts").write_text(_lts("edited"), encoding="utf-8")
        before = k.current_root(store.get_partition("c9"))
        k.tick()
        deadline = time.monotonic() + 3
        while k.current_root(store.get_partition("c9")) == before and time.monotonic() < deadline:
            k.tick()
        assert k.current_root(store.get_partition("c9")) != before, "a healthy root was polled while three others hung"
        assert k.stuck_workers() == 3
        assert k.roots.thread_count() <= 2 + 3 + 1, k.roots.thread_count()
    finally:
        gate.set()


def test_k_hung_roots_cost_one_deadline_not_k(tmp_path):
    """P2: a first unnamed search with K hung roots took K deadlines (the gates waited one after another). Probes are submitted
    together first; here K=4 on a 0.5 s deadline answer in about one deadline."""
    from factlattice import list_lts_files
    from factlattice_engine import config
    from factlattice_engine.tools import LtsEngine
    from factlattice_engine.verdict import PollVerifiedFreshness
    gate = threading.Event()
    armed = threading.Event()
    hung = {str(tmp_path / f"c{i}") for i in range(4)}

    def lister(root):
        if armed.is_set() and root in hung:
            gate.wait(10)
        return list_lts_files(root)

    reg = {}
    for i in range(8):
        r = tmp_path / f"c{i}"
        r.mkdir()
        (r / "a.lts").write_text(_lts(f"I{i}"), encoding="utf-8")
        reg[f"c{i}"] = {"roots": [{"path": str(r)}], "domain_suffix": f"x.c{i}"}
    store = config.build_store(reg)
    k = FreshnessKernel(root_deadline_s=0.5, lister=lister)
    k.seat_store(store)
    store.kernel = k
    eng = LtsEngine(store, freshness_provider=PollVerifiedFreshness())
    armed.set()
    try:
        t0 = time.monotonic()
        res = eng.find(text="I5", tier="digest")
        took = time.monotonic() - t0
        assert res["success"] and res["count"] == 1
        assert len(res["stale_corpora"]) == 4 and res["extent"]["index"]["fresh"] is False
        assert took < 1.2, f"four hung roots took {took:.2f}s on a 0.5 s deadline (K x deadline would be 2 s)"
    finally:
        gate.set()


def test_a_probe_finishing_after_its_corpus_was_unseated_does_not_recreate_what_unseat_dropped(tmp_path):
    """P3: unseat drops a corpus's kept observations; a probe that was already running published its observation afterwards and
    recreated the entry. The commit now checks the path is still in use."""
    from factlattice import list_lts_files
    gate = threading.Event()
    inside = threading.Event()
    armed = threading.Event()

    def lister(root):
        if armed.is_set():
            inside.set()
            gate.wait(5)
        return list_lts_files(root)

    k, part, root = _kernel_world(tmp_path, lister=lister, deadline=5.0)
    path = os.path.abspath(str(root))
    assert path in k._obs, "control: the seat kept an observation"
    armed.set()
    t = threading.Thread(target=lambda: k.poll_once(part), daemon=True)
    t.start()
    assert inside.wait(5)
    assert k.unseat_partition("k") is True
    gate.set()
    t.join(5)
    time.sleep(0.2)
    assert path not in k._obs, "a late commit recreated the observation of an unseated corpus"


# ======================================================================================================================
# round 3: foreground and background are separate; a call never waits behind the watcher's work
# ======================================================================================================================
def _many(tmp_path, n, *, hung=(), slow=(), deadline=1.0, backstop_every=30, slow_s=0.04, files=6):
    """n corpora of one root each; roots in `hung` block in the lister once armed; roots in `slow` take 40 ms per hashed file (many
    small calls, so they progress and are never marked)."""
    from factlattice import file_content_hash, list_lts_files
    from factlattice_engine import config
    gate, armed = threading.Event(), threading.Event()
    hung = {str(tmp_path / f"c{i}") for i in hung}
    slow = {str(tmp_path / f"c{i}") for i in slow}
    reg = {}
    for i in range(n):
        r = tmp_path / f"c{i}"
        r.mkdir()
        for f in range(files):
            (r / f"{f}.lts").write_text(_lts(f"I{i}"), encoding="utf-8")
        reg[f"c{i}"] = {"roots": [{"path": str(r)}], "domain_suffix": f"x.c{i}"}

    def lister(root):
        if armed.is_set() and root in hung:
            gate.wait(20)
        return list_lts_files(root)

    def hasher(path):
        if armed.is_set() and os.path.dirname(path) in slow:
            time.sleep(slow_s)
        return file_content_hash(path)

    store = config.build_store(reg)
    k = FreshnessKernel(root_deadline_s=deadline, lister=lister, file_hasher=hasher, backstop_every=backstop_every)
    k.seat_store(store)
    store.kernel = k
    return k, store, armed, gate


def _timed_read(k, part):
    t0 = time.monotonic()
    k.verify(part)
    return time.monotonic() - t0


def test_a_named_read_of_a_healthy_root_is_not_queued_behind_twenty_hung_roots(tmp_path):
    """P1-1. With K hung roots the watcher's jobs hold the pool and a healthy named read used to wait ceil(K/4) deadlines (4.9 s at K=20
    on a 1 s deadline). A call's job is foreground: it starts on a thread of its own at once."""
    k, store, armed, gate = _many(tmp_path, 21, hung=range(20), deadline=1.0)
    healthy = store.get_partition("c20")
    armed.set()
    try:
        t = threading.Thread(target=k.tick, daemon=True)
        t.start()                                   # the watcher asks every root; twenty block
        time.sleep(0.3)
        took = _timed_read(k, healthy)
        assert took < 0.3, f"a healthy named read took {took:.2f}s with 20 roots hung (deadline 1 s)"
        assert k.roots.thread_count() <= 4 + 20 + 2
    finally:
        gate.set()
        t.join(10)


def test_a_named_read_of_a_fifth_root_is_not_queued_behind_four_slow_full_passes(tmp_path):
    """P1-2. Four slow (but progressing) passes held every pool thread; a fifth root's named read waited 2.8 s."""
    k, store, armed, gate = _many(tmp_path, 6, slow=range(4), deadline=3.0, backstop_every=1, slow_s=0.2)
    armed.set()
    sweep = threading.Thread(target=k.tick, daemon=True)     # the backstop: a full pass for each root, in the background; four are slow
    sweep.start()
    time.sleep(0.35)                                          # past the pool's growth delay: every pool thread is inside a slow pass
    took = _timed_read(k, store.get_partition("c5"))
    sweep_running = sweep.is_alive()
    assert took < 0.3, f"a named read of a fifth root took {took:.2f}s while four slow passes ran (each about 1.2 s)"
    assert sweep_running, "the slow passes must still be running for this to mean anything"
    sweep.join(30)


def test_a_named_read_during_a_backstop_sweep_starts_at_once_and_promotes_the_queued_job(tmp_path):
    """A backstop tick queues a full pass for every root at once. A named read of one of them joins that queued job (a full pass
    answers a poll), PROMOTES it, and runs ahead of the other forty rather than after them."""
    k, store, armed, gate = _many(tmp_path, 41, slow=range(40), deadline=5.0, backstop_every=1, slow_s=0.04)
    armed.set()
    t = threading.Thread(target=k.tick, daemon=True)      # backstop_every=1: every root gets a full pass, background, 40 of them slow
    t.start()
    time.sleep(0.1)
    took = _timed_read(k, store.get_partition("c39"))     # a slow root at the BACK of the queue
    sweep_still_running = t.is_alive()
    assert took < 1.0, f"the named read waited {took:.2f}s: it was not promoted ahead of the queue"
    assert sweep_still_running, "the sweep must still be running for this to mean anything"
    t.join(60)


def test_pin_idle_threads_come_back_to_the_ordinary_pool_after_a_burst(tmp_path):
    from factlattice_engine.rootstate import RootRegistry
    reg = RootRegistry(1.0, idle_exit_s=0.3)
    slots = [reg.slot(f"/r{i}") for i in range(12)]
    gate = threading.Event()
    jobs = [s.submit("poll", lambda: gate.wait(5), foreground=True) for s in slots]
    assert reg.thread_count() <= 4, "a burst starts at most pool_size threads at once"
    end = time.monotonic() + 1.0
    while time.monotonic() < end:
        reg.rebalance()                          # the waiters' nudge: foreground work that has waited gets a thread of its own
        time.sleep(0.02)
    assert reg.thread_count() >= 12, "twelve calls that keep waiting end up with twelve threads"
    gate.set()
    for j in jobs:
        assert j.done.wait(3)
    time.sleep(0.8)
    assert reg.thread_count() == 0, reg.thread_count()


def test_a_job_queued_behind_a_progressing_one_is_not_marked_for_the_time_it_queued(tmp_path):
    """Queue time is not progress time. One worker (no growth), root A running a long job that keeps reporting progress, root B's job
    queued behind it for several deadlines: B has no running job, so B is never marked, and neither is A. Guards against a lapse
    counted from submission (shown red against that mutant)."""
    reg = RS.RootRegistry(0.2, pool_size=1, grow_after_s=1000)
    a, b = reg.slot("/a"), reg.slot("/b")
    t_end = time.monotonic() + 0.9

    def long_but_moving():
        while time.monotonic() < t_end:
            time.sleep(0.05)
            RS.progress()

    ja = a.submit("poll", long_but_moving)
    jb = b.submit("poll", lambda: None)
    seen = set()
    while not jb.done.is_set() and time.monotonic() < t_end + 2:
        seen.add((a.state(), b.state()))
        time.sleep(0.02)
    assert jb.done.is_set() and ja.done.is_set()
    assert all(RS.UNRESPONSIVE not in pair for pair in seen), seen
    assert (RS.OK, RS.PENDING) in seen or (RS.PENDING, RS.PENDING) in seen, "control: B really did wait while A ran"


def test_list_lts_files_reports_progress_per_directory(tmp_path):
    """Pin: the listing reports progress once per directory it enters, not only when it returns, so a tree of many empty directories on
    a slow disk is not one silent call."""
    import factlattice.hashing as H
    for i in range(7):
        (tmp_path / f"d{i}" / "sub").mkdir(parents=True)
    calls = []
    prev = H.progress_hook
    H.progress_hook = lambda: calls.append(1)
    try:
        H.list_lts_files(str(tmp_path))
    finally:
        H.progress_hook = prev
    assert len(calls) >= 15, len(calls)       # 1 root + 7 dirs + 7 subdirs


def test_registering_a_corpus_on_a_marked_root_is_refused_from_memory(ws, monkeypatch):
    from factlattice_engine import tools as T
    eng = _engine(ws, monkeypatch)
    with RootStall(ws.root("b1"), cap_s=20) as stall:
        try:
            T.dispatch("lts_find", {"corpus": "b1", "text": "x"}, tools=eng)           # marks b1
            hits = stall.hit_count
            t0 = time.monotonic()
            r = T.dispatch("lts_register_corpus", {"corpus_id": "again", "spec": {"roots": [{"path": str(ws.root("b1"))}],
                                                                                    "domain_suffix": "x.again"}}, tools=eng)
            assert r["success"] is False and r["reason"] == CODE_UNRESPONSIVE, r
            assert time.monotonic() - t0 < 0.3 and stall.hit_count == hits, "refused from memory, no disk call"
            assert "again" not in eng.store.corpus_ids()
            ok = T.dispatch("lts_register_corpus", {"corpus_id": "fine", "spec": {"roots": [{"path": str(ws.root("a1"))}],
                                                                                    "domain_suffix": "x.fine"}}, tools=eng)
            assert ok["success"], ok            # CONTROL: a root that answers still registers
        finally:
            stall.release()
            time.sleep(0.3)


def test_a_burst_of_quick_foreground_jobs_does_not_become_a_thread_per_root_and_threads_leave_under_steady_polling():
    """A listing probes 72 roots at once. It must not start 72 threads, and threads must leave although a steady stream of notifies
    keeps waking them (the idle clock is per thread, from its last work)."""
    reg = RS.RootRegistry(1.0, idle_exit_s=0.4)
    slots = [reg.slot(f"/r{i}") for i in range(72)]
    jobs = [s.submit("poll", lambda: time.sleep(0.001), foreground=True) for s in slots]
    peak = reg.thread_count()
    for j in jobs:
        assert j.done.wait(5)
    assert peak <= 6, peak
    # steady polling of a few roots in the background, while the others' threads should leave
    end = time.monotonic() + 1.2
    while time.monotonic() < end:
        for s in slots[:3]:
            s.submit("poll", lambda: None)
        time.sleep(0.01)
    assert reg.thread_count() <= 4, reg.thread_count()       # the pool, not the 72 threads of the burst
    time.sleep(1.0)
    assert reg.thread_count() == 0, "and every thread leaves once the stream stops"


# ======================================================================================================================
# round 4
# ======================================================================================================================
def test_a_named_read_of_a_root_whose_background_pass_is_running_keeps_its_urgency_through_the_requeue(tmp_path):
    """P1-1. A call's job queued BEHIND a running background job on its own root used to be re-queued as background when that job ended
    (the flag was passed at submit and not kept), so the read waited for the whole sweep. 48 roots x 20 files at 10 ms (a 0.2 s pass each),
    a sweep in the background, and a named read of c0 at +0.05 s, while c0's pass is RUNNING: it must wait for that pass only. The read of
    c47 (not running, so promoted from the queue) is the control. Red on d4efa5b for this reason (it waited for the sweep), not by signature."""
    k, store, armed, gate = _many(tmp_path, 48, slow=range(48), slow_s=0.01, files=20, backstop_every=1)
    armed.set()
    sweep = threading.Thread(target=k.tick, daemon=True)
    sweep.start()
    time.sleep(0.05)
    assert k.roots.slot(os.path.abspath(str(tmp_path / "c0"))).busy(), "c0's background pass must be running for this to mean anything"
    took0 = _timed_read(k, store.get_partition("c0"))
    still = sweep.is_alive()
    took47 = _timed_read(k, store.get_partition("c47"))
    assert still, "the sweep must still be running when the read returns"
    sweep.join(60)
    assert took0 < 1.0, f"a read of c0, whose background pass was running, took {took0:.2f}s (one pass is 0.2 s; the sweep takes several seconds)"
    assert took47 < 1.0, f"control: the read of c47 took {took47:.2f}s"


def test_a_waiter_queued_behind_a_stalled_job_runs_at_once_after_the_recovery_job_not_behind_other_roots_background_work():
    """Every path that requeues: root /a has a STALLED job running (so a recovery job is inserted at the head of its queue when it ends) and
    a foreground waiter queued behind it, while four other roots' background jobs (0.5 s each) occupy the one background worker. When the
    stalled call returns, /a must run its recovery job and then the waiter on a foreground thread, ahead of those four; the recovery job
    itself is background work and must not carry the waiter's urgency. Red under mutant M1 (the requeue ignores the jobs' own urgency):
    the slot then waits its turn behind the other roots, about 2 s."""
    reg = RS.RootRegistry(0.1, pool_size=1, grow_after_s=1000, idle_exit_s=0.5)
    a = reg.slot("/a")
    order, gate = [], threading.Event()
    recovered = []

    def make_recovery():
        j = RS.Job("full", lambda: order.append("recovery"), clock=reg.clock)
        recovered.append(j)
        return j

    a.recover = make_recovery
    stuck = a.submit("poll", lambda: gate.wait(10), foreground=True)        # runs on a foreground thread, stalls
    time.sleep(0.4)                                                          # no progress for 4 deadlines: marked, still running
    assert a.state() == RS.UNRESPONSIVE
    waiter = a.submit("observe", lambda: order.append("waiter"), foreground=True)       # queued behind the running stalled job
    others = [reg.slot(f"/o{i}") for i in range(4)]
    for o in others:
        o.submit("poll", lambda: time.sleep(0.5))                           # the single background worker is busy for 2 s
    time.sleep(0.1)
    t0 = time.monotonic()
    gate.set()
    assert waiter.done.wait(5)
    took = time.monotonic() - t0
    assert stuck.overran and order == ["recovery", "waiter"], order
    assert recovered and recovered[0].fg is False, "the recovery job is background work; it takes no one's urgency"
    assert took < 0.8, f"the waiter ran {took:.2f}s after the stalled call returned: it queued behind the other roots' background work"


def test_a_foreground_request_that_joins_a_queued_background_job_while_the_roots_job_runs_makes_the_slot_foreground():
    """Join while RUNNING (reg5 shape). /a has a job running; the watcher's background poll is queued behind it; four other roots' background
    jobs occupy the single background worker. A waiter that JOINS the queued poll must promote it for good (the job's own flag), so that
    when the running job ends the slot runs at once. Red under mutant M2 (a join does not set the job's urgency): the slot is requeued as
    background and waits about 2 s behind the others."""
    reg = RS.RootRegistry(30, pool_size=1, grow_after_s=1000, idle_exit_s=0.5)
    a = reg.slot("/a")
    ev = threading.Event()
    a.submit("poll", lambda: ev.wait(10), foreground=True)                   # a's running job, on a foreground thread
    time.sleep(0.1)
    q = a.submit("poll", lambda: None, coalesce=False)                       # the watcher's poll, queued behind it (background)
    assert q.fg is False
    for i in range(4):
        reg.slot(f"/o{i}").submit("poll", lambda: time.sleep(0.5))
    time.sleep(0.05)
    j = a.submit("observe", lambda: None, foreground=True)                   # a call joins the queued poll
    assert j is q and j.fg is True, "the join must set the job's urgency"
    t0 = time.monotonic()
    ev.set()
    assert j.done.wait(6)
    took = time.monotonic() - t0
    assert took < 0.8, f"the joined job ran {took:.2f}s after the running one ended: behind the other roots' background work"


def test_a_slot_dropped_while_its_job_runs_and_registered_again_is_not_removed_when_that_job_ends():
    """reg6 shape. A corpus unregistered and registered again while the root's (healthy, slow) job runs is in use again; the slot must
    survive that job's end. Red under mutant M5 (slot() does not clear the dropped flag)."""
    reg = RS.RootRegistry(30, pool_size=2, grow_after_s=0.1, idle_exit_s=0.5)
    s = reg.slot("/r")
    ev = threading.Event()
    s.submit("poll", lambda: ev.wait(5), foreground=True)
    time.sleep(0.1)
    reg.drop("/r")
    assert reg.slot("/r") is s
    ev.set()
    time.sleep(0.4)
    assert "/r" in reg.paths(), "the slot of a root in use again was removed when the old job ended"


def test_a_dropped_slot_is_removed_only_when_its_last_queued_job_is_done():
    """Removal waits for the QUEUE too, not just the running job. Red under mutant M6 (removal ignores a non-empty queue): the registry
    stops reporting the root while a job for it is still waiting, and a stuck-or-marked root would read as unknown."""
    reg = RS.RootRegistry(30, pool_size=2, grow_after_s=0.1, idle_exit_s=0.5)
    s = reg.slot("/r")
    first, second = threading.Event(), threading.Event()
    s.submit("poll", lambda: first.wait(5), foreground=True)
    time.sleep(0.1)
    s.submit("full", lambda: second.wait(5), foreground=True)                # queued behind the running one
    reg.drop("/r")
    first.set()
    time.sleep(0.3)                                                          # the first job is done, the second is running
    assert "/r" in reg.paths(), "removed while a queued job was still to run"
    second.set()
    end = time.monotonic() + 3
    while "/r" in reg.paths() and time.monotonic() < end:
        time.sleep(0.02)
    assert "/r" not in reg.paths(), "and removed once the last job is done"


def test_a_stale_reference_to_a_removed_slot_does_not_remove_its_replacement():
    """The identity guard on removal is reachable: the kernel looks a slot up and submits to it in two steps, so a caller can hold a slot
    that was dropped and removed meanwhile, with a NEW slot registered at the same path. When the stale slot's job ends it must not delete
    the new one. Red under mutant M9 (removal without the identity check)."""
    reg = RS.RootRegistry(30, pool_size=2, grow_after_s=0.1, idle_exit_s=0.5)
    old = reg.slot("/r")
    ev = threading.Event()
    old.submit("poll", lambda: ev.wait(5), foreground=True)
    time.sleep(0.1)
    reg.drop("/r")                                                           # busy: flagged dropped, kept
    ev.set()
    end = time.monotonic() + 3
    while "/r" in reg.paths() and time.monotonic() < end:
        time.sleep(0.02)
    assert "/r" not in reg.paths(), "control: the dropped slot was removed when its job ended"
    new = reg.slot("/r")                                                     # a corpus is registered on the root again
    done = old.submit("poll", lambda: None, foreground=True)                 # a stale holder of the OLD slot submits (still flagged dropped)
    assert done.done.wait(3)
    time.sleep(0.1)
    assert reg.slot("/r", create=False) is new, "the replacement slot was deleted by the old slot's job ending"


def _hung_registration_world(tmp_path):
    from factlattice import list_lts_files
    from factlattice_engine import config
    from factlattice_engine.selfheal import SelfHealer
    from factlattice_engine.tools import LtsEngine
    from factlattice_engine.verdict import PollVerifiedFreshness
    r = tmp_path / "m"
    r.mkdir()
    (r / "f.lts").write_text(_lts("M1"), encoding="utf-8")
    hang, release = threading.Event(), threading.Event()

    def lister(p):
        if hang.is_set() and os.path.abspath(p) == os.path.abspath(str(r)):
            release.wait(20)
        return list_lts_files(p)

    store = config.build_store({"m": {"root": str(r), "domain_suffix": "proj.m"}})
    k = FreshnessKernel(lister=lister, poll_interval=100.0, root_deadline_s=0.4)
    k.seat_store(store)
    store.kernel = k
    prov = PollVerifiedFreshness()
    eng = LtsEngine(store, healer=SelfHealer(store, kernel=k, freshness_provider=prov), freshness_provider=prov)
    return eng, k, r, hang, release


def test_unregistering_a_marked_corpus_does_not_forget_the_mark_and_a_re_registration_is_refused_until_the_call_returns(tmp_path):
    """P2-2. unseat dropped the root's slot into a list the mark lookup did not read, so re-registering the same hung root listed it
    on the request thread (blocked more than 3 s). The mark now lives with the slot until its stuck call returns. Sequence: mark,
    unregister, register (refused, promptly, nothing registered), release, register (served), unregister: no slot left behind."""
    eng, k, r, hang, release = _hung_registration_world(tmp_path)
    path = os.path.abspath(str(r))
    try:
        hang.set()
        first = eng.find(corpus="m", text="M1")                         # waits for the deadline, marks
        assert first["success"] is False and first["reason"] == "root-unresponsive", first
        assert eng.unregister_corpus("m")["success"]
        assert k.stuck_workers() == 1 and k.roots.state_of(path) == RS.UNRESPONSIVE, "the registry still knows the root is stuck"
        t0 = time.monotonic()
        out = []
        t = threading.Thread(target=lambda: out.append(eng.register_corpus("m", {"root": str(r), "domain_suffix": "proj.m"})), daemon=True)
        t.start()
        t.join(3)
        assert out, "register_corpus blocked on the hung root"
        assert out[0]["success"] is False and out[0]["reason"] == "root-unresponsive" and time.monotonic() - t0 < 1.0, out[0]
        assert "m" not in eng.store.corpus_ids()
        release.set()
        end = time.monotonic() + 5
        while k.stuck_workers() and time.monotonic() < end:
            time.sleep(0.02)
        end = time.monotonic() + 5
        while path in k.roots.paths() and time.monotonic() < end:
            time.sleep(0.02)
        assert path not in k.roots.paths(), "no slot left behind for a root nothing uses (no orphan)"
        ok = eng.register_corpus("m", {"root": str(r), "domain_suffix": "proj.m"})
        assert ok["success"], ok                                         # after the release the root is served again
        assert eng.unregister_corpus("m")["success"]
        assert path not in k.roots.paths() and k.stuck_workers() == 0
    finally:
        release.set()


def test_onset_red_record_note_is_in_the_record():
    """Pin: the xfail below is the record of the case slice 5 has to close (see its reason); this makes its removal a deliberate act."""
    assert "test_onset_a_healthy_read_waits_behind_a_named_read_of_a_not_yet_marked_hung_root" in open(__file__, encoding="utf-8").read()


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="[slice 5, x-first-hang] the server has one request thread: a NAMED read of a hung root that no "
                   "poll has marked yet waits for the root deadline on that thread, and every healthy read queued behind it waits too (measured "
                   "2.71 s on a 3 s deadline; about 10 s at the default is an extrapolation, not run; once per hang onset). Every other real-server test sleeps SETTLE_S "
                   "first and so never reaches this window. Calls on workers close it.")
@needs_per_component_resolver
def test_onset_a_healthy_read_waits_behind_a_named_read_of_a_not_yet_marked_hung_root(ws):
    ck = Checks()
    srv = RealServer(ws, stall_root=ws.root("b1"), engaged=False, env={**DEADLINES, "LTS_ROOT_DEADLINE_S": "3"}).start()
    try:
        srv.initialize(timeout=30)
        ctl, _ = srv.call("lts_find", {"corpus": "a1", "text": "lanternfish", "tier": "digest"}, timeout=10)
        if ctl.get("count") != 1:
            raise HarnessError(f"control read failed: {ctl}")
        srv.engage()                                         # NO settle: nothing has marked b1
        srv.send_call("lts_find", {"corpus": "b1", "text": "lanternfish"})     # a call that names the hung root, then
        time.sleep(0.3)
        t0 = time.monotonic()
        rid = srv.send_call("lts_find", {"corpus": "a1", "text": "lanternfish", "tier": "digest"})
        reply = srv.unwrap(srv.wait_reply(rid, 15))
        waited = time.monotonic() - t0
        ck.ok(reply.get("success") and waited < 1.0, f"a healthy read sent 0.3 s after a named read of a hung root waited {waited:.2f}s "
                                                      f"(load {os.getloadavg()[0]:.1f}); a deadline of 3 s")
        srv.require_hit()
    finally:
        srv.close()
    ck.done("onset (head-of-line blocking on the request thread)")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="[slice 5, x-first-hang] the CORPUS-OMITTED form of the same case: an unnamed lts_find probes every "
                   "root first, and a root no poll has marked yet holds the request thread for the deadline (0.43 s on a 0.4 s deadline, measured "
                   "in process; here a real server on a 3 s deadline). Calls on workers close both forms.")
@needs_per_component_resolver
def test_onset_an_unnamed_find_waits_for_the_deadline_on_a_not_yet_marked_hung_root(ws):
    ck = Checks()
    srv = RealServer(ws, stall_root=ws.root("b1"), engaged=False, env={**DEADLINES, "LTS_ROOT_DEADLINE_S": "3"}).start()
    try:
        srv.initialize(timeout=30)
        ctl, _ = srv.call("lts_find", {"text": "lanternfish", "tier": "digest"}, timeout=10)
        if ctl.get("count") != 2:
            raise HarnessError(f"control read failed: {ctl}")
        srv.engage()                                         # NO settle: nothing has marked b1
        t0 = time.monotonic()
        rid = srv.send_call("lts_find", {"text": "lanternfish", "tier": "digest"})
        reply = srv.unwrap(srv.wait_reply(rid, 15))
        waited = time.monotonic() - t0
        ck.ok(reply.get("success") and waited < 1.0, f"an unnamed find at hang onset waited {waited:.2f}s (load {os.getloadavg()[0]:.1f}); a deadline of 3 s")
        srv.require_hit()
    finally:
        srv.close()
    ck.done("onset, corpus-omitted form")
