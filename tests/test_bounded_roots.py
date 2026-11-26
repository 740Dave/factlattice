"""[FL-BOUNDED-ROOTS] slice 1: RED RECORDS. Each test states a behaviour specs/38-bounded-root-reads.lts requires, is run
against the engine as it is TODAY, and is marked xfail(strict=True) with the reason it fails: the suite stays green, and each
test flips LOUDLY (XPASS strict = failure) the day its slice makes it true, at which point the builder removes the marker.

WHAT THESE ARE NOT
  * They run a MODEL of a hung disk (tests/stall_harness.py), never a real hung drive; every result is a result about that model.
  * They are written to the spec's words, and where the spec's words conflict or leave a case open the test takes the
    narrowest reading and says so in its docstring (see c1 and c5).
  * A red test fails because the behaviour is absent. Preconditions that could fail for a setup reason raise HarnessError
    (a RuntimeError), which xfail(raises=AssertionError) does NOT absorb, so a broken setup cannot pass as a red record.
  * `--runxfail` shows the failure each marker records.

TIME. Every blocked call is asked with a hard limit, every stall is released in `finally`, every child process is killed in
`finally`. In the red state the file runs in well under 60 s. When a slice makes a test green, its window is the slice
owner's to size: c2's 60 s window is a parameter (FL_STALL_WINDOW_S, spec value 60, default here 4).

DEADLINES. The children get LTS_ROOT_DEADLINE_S=1, LTS_CALL_DEADLINE_S=2 and LTS_CALL_WORKERS=1 so a conforming engine answers
fast; today the engine reads none of them.

SLICE 2 (probes, gate, report) turned c2's reads and c5's 200-poll test green and removed their markers; c2's write half was its own
red record until SLICE 3 (membership and the write rules), which turned it and c3's healthy-file and marked-root test green and removed
their markers. SLICE 4 (boot and build_store) turned c1 and c6 green and removed their markers. What is left red here is slice 5. Its own tests, including c4 (release), c7 (privacy, for what it touches) and ROOT-SET-CHANGE during a stall, are in
tests/test_bounded_roots_probes.py.

NOT COVERED BY ANY TEST HERE OR THERE (unrun, and named so): c8 (no slower; measured by tools/bounded_reads_bench.py, not a test),
c10 (a real drive), c11 under the deployment's real load, c12 (render time), the re-exec deferral half of c3, the consumer-side render
banner half of c6 (it is tests/test_bounded_roots_boot.py's).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent

from tests.resolver_mode import needs_per_component_resolver
from tests.stall_harness import HarnessError, NoReply, RealServer, RootStall, Workspace, run_limited

ROOT_DEADLINE_S = 1
CALL_DEADLINE_S = 2
DEADLINES = {"LTS_ROOT_DEADLINE_S": str(ROOT_DEADLINE_S), "LTS_CALL_DEADLINE_S": str(CALL_DEADLINE_S),
             "LTS_CALL_WORKERS": "1"}
HANDSHAKE_S = 1.0          # c1: "handshake under 1 s"
CALL_OK_S = 2.0            # "normal time" for a call on a healthy corpus: measured ~1 ms here, so this is generous
SETTLE_S = 2.3             # a poll (1 s interval) plus the root deadline plus slack: long enough to be marked
JOIN_WINDOW_S = float(os.environ.get("FL_C11_JOIN_S", "40"))   # c11 shape: how long a root answering 0.3 s per call may take to join (measured, recorded beside the result)
WINDOW_S = float(os.environ.get("FL_STALL_WINDOW_S", "4"))   # c2 spec value: 60

CODE_UNRESPONSIVE = "root-unresponsive"
CODE_OVERRAN, CODE_SATURATED, CODE_BUILD = "call-overran", "engine-saturated", "corpus-root-unresponsive"


def red(reason: str):
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason)


class Checks:
    """Collect every failed expectation, so one red run shows all the missing behaviour, not only the first."""

    def __init__(self) -> None:
        self.fail: list[str] = []

    def ok(self, cond: Any, msg: str) -> bool:
        if not cond:
            self.fail.append(msg)
        return bool(cond)

    def done(self, context: str = "") -> None:
        assert not self.fail, (context + "\n  - " if context else "\n  - ") + "\n  - ".join(self.fail)


class Asker:
    """Ask a real server with a limit. The server answers one request at a time, so after the first NoReply nothing
    behind it can be asked; later asks say so instead of waiting again."""

    def __init__(self, srv: RealServer, checks: Checks) -> None:
        self.srv, self.checks, self.wedged = srv, checks, None

    def __call__(self, label: str, name: str, args: dict, limit: float = CALL_OK_S):
        if self.wedged:
            self.checks.fail.append(f"{label}: not asked; the server gave no reply to {self.wedged!r} and serves one request at a time")
            return None, None
        try:
            return self.srv.call(name, args, timeout=limit)
        except NoReply:
            self.wedged = label
            seen = sorted({(h["call"], h["thread"]) for h in self.srv.hits()})[:4]
            self.checks.fail.append(f"{label}: no reply within {limit:.1f}s (stall reached by {seen})")
            return None, None


def has_code(res: dict | None, code: str) -> bool:
    """The code appears anywhere in the reply. Shape-agnostic on purpose: the spec fixes the code names, not the field."""
    return res is not None and code in json.dumps(res)


def find_key(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def entry(listing: dict | None, cid: str) -> dict | None:
    for c in (listing or {}).get("corpora") or []:
        if c.get("corpus_id") == cid:
            return c
    return None


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    return Workspace(tmp_path / "w")


def ledger_records(w: Workspace) -> list[dict]:
    out: list[dict] = []
    d = w.state / "ledger"
    if d.is_dir():
        for f in sorted(d.glob("*.jsonl")):
            out += [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
    return out


def _boot_then_stall(w: Workspace, **kw) -> RealServer:
    """A server that boots healthy (the model idle), answers one control call, and is then ENGAGED."""
    srv = RealServer(w, stall_root=w.root("b1"), engaged=False, env=DEADLINES, **kw).start()
    try:
        srv.initialize(timeout=30)
        res, _ = srv.call("lts_find", {"corpus": "b1", "text": "lanternfish", "tier": "digest"}, timeout=10)
    except NoReply as exc:
        srv.close()
        raise HarnessError(f"the control server did not come up healthy: {exc}") from None
    if res.get("count") != 1:
        srv.close()
        raise HarnessError(f"control read before the stall failed: {res}")
    return srv


# ======================================================================================================================
# c1  hung at boot
# ======================================================================================================================
def test_c1_hung_at_boot(ws):
    """SLICE 4 TURNED THIS GREEN (the red record: serve() built the store, every root read, before it read stdin).

    ORDER CHANGED, ASSERTIONS NOT: the listing that must show b1 PENDING now comes first, straight after the handshake. In the red record it came
    after the unnamed find, but x-server says a corpus-omitted read WAITS (up to the root deadline) for an unread root and then declares it, so
    by the time that find returned the deadline had passed and b1 was, correctly, unresponsive. The listing never waits; asked before any
    waiting call it shows pending, and after the deadline it shows unresponsive."""
    ck = Checks()
    with RealServer(ws, stall_root=ws.root("b1"), engaged=True, env=DEADLINES) as srv:
        ask = Asker(srv, ck)
        t_hs = time.monotonic()
        try:
            srv.initialize(timeout=HANDSHAKE_S)
        except NoReply:
            seen = sorted({(h["call"], h["thread"]) for h in srv.hits()})[:4]
            ck.fail.append(f"handshake: no initialize reply within {HANDSHAKE_S}s of the request (boot blocked in {seen})")
            ask.wedged = "initialize"
        listing, _ = ask("list (before the deadline)", "lts_list_corpora", {"heal": False})
        b1 = entry(listing, "b1")
        ck.ok(b1 is not None and b1.get("total_nodes") is None and (b1.get("root_health") or {}).get("status") == "pending",
              f"the first listing shows b1 pending with null totals (got {b1 and {k: b1.get(k) for k in ('total_nodes','fresh','root_health')}})")
        res, _ = ask("a1 named find", "lts_find", {"corpus": "a1", "text": "lanternfish", "tier": "digest"})
        ck.ok(res and res.get("success") and res.get("count") == 1, f"a1 named find while b1 hangs: {res and res.get('success')}")
        res, _ = ask("unnamed find", "lts_find", {"text": "lanternfish", "tier": "digest"}, ROOT_DEADLINE_S + CALL_OK_S)
        ck.ok(res and res.get("success"), "unnamed find answers (without b1) while b1 hangs")
        skipped = (((res or {}).get("extent") or {}).get("corpora_skipped")) or []
        ck.ok(res and res.get("complete") is False and any(x.get("corpus") == "b1" and x.get("reason") in ("root-pending", CODE_UNRESPONSIVE) for x in skipped),
              f"the unnamed find leaves b1 out and DECLARES it (complete false, corpora_skipped names b1): {skipped}")
        # x-c1: a call naming the hung corpus WAITS up to the root deadline, then refuses root-unresponsive
        res, _ = ask("b1 named find", "lts_find", {"corpus": "b1", "text": "lanternfish"}, ROOT_DEADLINE_S + CALL_OK_S)
        ck.ok(res and res.get("success") is False and has_code(res, CODE_UNRESPONSIVE),
              "a call naming b1 waits up to the root deadline, then is refused root-unresponsive")
        listing, _ = ask("list (past the deadline)", "lts_list_corpora", {"heal": False})
        b1 = entry(listing, "b1")
        ck.ok(b1 is not None and b1.get("total_nodes") is None and (b1.get("root_health") or {}).get("status") == "unresponsive",
              f"past the deadline the listing shows b1 unresponsive with null totals (got {b1 and {k: b1.get(k) for k in ('total_nodes','root_health')}})")
        srv.require_hit()
    ck.done("c1 (hung at boot)")


# ======================================================================================================================
# c2  hung after boot
# ======================================================================================================================
@needs_per_component_resolver
def test_c2_hung_after_boot(ws):
    """SLICE 2 TURNED THE READ HALF OF THIS GREEN (the red record: the poll held the kernel lock while listing the hung root).
    The WRITE half moved, unchanged, to `test_c2_write_during_stall_then_read` below, which stays red until slice 3: a write
    resolves every registered root in membership, so it still stalls, and that is not this slice's. Nothing was loosened."""
    ck = Checks()
    srv = _boot_then_stall(ws, **{})
    try:
        ask = Asker(srv, ck)
        # the negative control for the probes themselves: all five answer before the stall
        for name, args in PROBES:
            r, _ = srv.call(name, args(ws), timeout=5)
            if not r.get("success"):
                raise HarnessError(f"probe {name} fails on a healthy server: {r}")
        srv.engage()
        time.sleep(SETTLE_S)
        t_end = time.monotonic() + WINDOW_S
        rounds = 0
        while True:
            for name, args in PROBES:
                r, dt = ask(f"{name} (round {rounds})", name, args(ws))
                ck.ok(r is None or r.get("success"), f"{name} on the healthy corpus failed: {r}")
            rounds += 1
            if ask.wedged or time.monotonic() >= t_end:
                break
            time.sleep(1.0)
        r, _ = ask("unnamed find", "lts_find", {"text": "lanternfish", "tier": "digest"})
        if r is not None:
            ck.ok(r.get("success") and r.get("complete") is False, "unnamed search declares the omission with complete false")
            skipped = json.dumps((r.get("extent") or {}).get("corpora_skipped"))
            ck.ok("b1" in skipped and CODE_UNRESPONSIVE in skipped, f"corpora_skipped names b1 with {CODE_UNRESPONSIVE}: {skipped}")
        srv.require_hit()
    finally:
        srv.close()
    ck.done(f"c2 (hung after boot; window {WINDOW_S}s of the spec's 60)")


def test_c2_write_during_stall_then_read(ws):
    ck = Checks()
    srv = _boot_then_stall(ws, **{})
    try:
        ask = Asker(srv, ck)
        srv.engage()
        time.sleep(SETTLE_S)
        w, _ = ask("write to a healthy file", "lts_add_property",
                   {"path": str(ws.file("a1")), "node_id": "A1-N", "key": "stamp", "value": "after-stall"})
        ck.ok(w is None or w.get("success"), f"a write to the healthy file succeeds: {w and w.get('refusal') or w}")
        r, _ = ask("named read after the write", "lts_node", {"corpus": "a1", "node_id": "A1-N"})
        ck.ok(r is not None and "after-stall" in json.dumps(r), "a named read after the write returns the write")
        srv.require_hit()
    finally:
        srv.close()
    ck.done("c2 (write, then read, during a stall)")


PROBES = [
    ("lts_find", lambda w: {"corpus": "a1", "text": "lanternfish", "tier": "digest"}),
    ("lts_find_in_prose", lambda w: {"corpus": "a1", "text": "nothing"}),
    ("lts_node", lambda w: {"corpus": "a1", "node_id": "A1-N", "tier": "digest"}),
    ("lts_reverse", lambda w: {"corpus": "a1", "node_id": "A1-N"}),
    ("lts_list_corpora", lambda w: {"heal": False}),
]


# ======================================================================================================================
# c3  writes
# ======================================================================================================================
@needs_per_component_resolver
def test_c3_writes_healthy_file_and_marked_root(ws):
    ck = Checks()
    srv = _boot_then_stall(ws)
    try:
        ask = Asker(srv, ck)
        srv.engage()
        time.sleep(SETTLE_S)
        w, _ = ask("write to a healthy file", "lts_add_property",
                   {"path": str(ws.file("a1")), "node_id": "A1-N", "key": "stamp", "value": "healthy"})
        if w is not None:
            ck.ok(w.get("success") and w.get("written"), f"the healthy write succeeds: {w.get('success')}")
            mp = find_key(w, "membership_partial")
            ck.ok(isinstance(mp, dict) and mp.get("marked_count") == 1, f"the receipt carries membership_partial {{marked_count: 1}}: {mp}")
            recs = [r for r in ledger_records(ws) if str(r.get("path_real", "")).endswith("a1.lts")]
            ck.ok(len(recs) == 1, f"one ledger record for the healthy file ({len(recs)})")
            ck.ok(recs and isinstance(recs[-1].get("membership_partial"), dict), "the ledger record carries membership_partial")
        before = ws.file("b1").read_bytes()
        w2, _ = ask("write into the marked root", "lts_add_property",
                    {"path": str(ws.file("b1")), "node_id": "B1-N", "key": "stamp", "value": "into-hung"})
        ck.ok(w2 is not None and w2.get("success") is False and has_code(w2, CODE_UNRESPONSIVE),
              "a write to a file under the marked root is refused root-unresponsive")
        srv.release()
        time.sleep(0.5)
        ck.ok(ws.file("b1").read_bytes() == before, "the refused write left the file's bytes unchanged")
        srv.require_hit()
    finally:
        srv.close()
    ck.done("c3 (writes)")


@red("c3: a first-hang write (the call reaches the root before any poll marked it) blocks the server's only request thread; "
     "spec: it replies call-overran with outcome_unknown true after the call deadline and the server takes the next request")
def test_c3_first_hang_write_replies_call_overran(ws, record_property):
    ck = Checks()
    srv = _boot_then_stall(ws)
    try:
        srv.engage()                                    # no settle: the next call IS the first to meet the hang
        rid = srv.send_call("lts_add_property", {"path": str(ws.file("b1")), "node_id": "B1-N", "key": "stamp", "value": "first-hang"})
        reply = None
        try:
            reply = srv.unwrap(srv.wait_reply(rid, CALL_DEADLINE_S + 2.0))
        except NoReply:
            seen = sorted({(h["call"], h["thread"]) for h in srv.hits()})[:3]
            ck.fail.append(f"no reply within {CALL_DEADLINE_S + 2.0}s of a call deadline of {CALL_DEADLINE_S}s (stall reached by {seen})")
        if reply is not None:
            ck.ok(reply.get("success") is False and has_code(reply, CODE_OVERRAN), f"the reply is {CODE_OVERRAN}: {json.dumps(reply)[:200]}")
            ck.ok(find_key(reply, "outcome_unknown") is True, "the reply says outcome_unknown true")
        srv.release()
        time.sleep(1.0)
        # After release the file and the ledger may disagree: the spec allows a landed write with no ledger record (a cut-off
        # between the replace and the append). That is a legitimate outcome, so it is RECORDED, not asserted either way.
        in_file = "first-hang" in ws.file("b1").read_text()
        in_ledger = any(str(r.get("path_real", "")).endswith("b1.lts") for r in ledger_records(ws))
        record_property("first_hang_write_landed_in_file", str(in_file))
        record_property("first_hang_write_has_ledger_record", str(in_ledger))
        srv.require_hit()
    finally:
        srv.close()
    ck.done("c3 (first-hang write)")


# ======================================================================================================================
# c5  no pile-up, saturation
# ======================================================================================================================
def _inproc_engine(w: Workspace, monkeypatch):
    for k, v in w.env(DEADLINES).items():
        monkeypatch.setenv(k, v)
    from factlattice_engine.server import build_tools  # imported after the env, as a process would
    return build_tools(watch=False, require_corpora=True)  # no watcher thread, no code sentinel: the test drives the polls


def engine_threads_since(before_idents) -> list[threading.Thread]:
    """Threads alive now that were not alive at `before_idents`. The TEST starts none in c5, so these are the engine's."""
    return [t for t in threading.enumerate() if t.ident not in before_idents]


def test_c5_thread_count_detector_counts_a_planted_pile_up():
    """Negative control for the c5 counter: five planted blocked threads are five new threads; none is none."""
    before = {t.ident for t in threading.enumerate()}
    assert engine_threads_since(before) == []
    gate = threading.Event()
    ts = [threading.Thread(target=gate.wait, daemon=True) for _ in range(5)]
    try:
        for t in ts:
            t.start()
        assert len(engine_threads_since(before)) == 5
    finally:
        gate.set()
        for t in ts:
            t.join(5)


@needs_per_component_resolver
def test_c5_no_pile_up_across_200_polls(ws, monkeypatch):
    """Dependencies on engine names, stated: `kernel.tick` (counted by wrapping the instance attribute the watcher calls),
    `kernel.start/stop/poll_interval`, and `kernel.current_root(part)` (what a reader of freshness sees). A slice that renames
    them updates this test; the edit-seen check is what keeps an engine that only counts polls from passing it."""
    eng = _inproc_engine(ws, monkeypatch)
    kernel = eng.store.kernel
    kernel.poll_interval = 0.005
    ticks = {"n": 0}
    real_tick = kernel.tick

    def counting():
        ticks["n"] += 1
        return real_tick()

    kernel.tick = counting                              # the watcher calls self.tick, so this counts every poll it starts
    ck = Checks()
    kernel.start()                                      # the ENGINE's own watcher thread; the test starts no thread of its own
    try:
        end = time.monotonic() + 3
        while ticks["n"] < 3 and time.monotonic() < end:
            time.sleep(0.01)
        if ticks["n"] < 3:
            raise HarnessError("the watcher did not poll a healthy corpus: the control failed")
        before = {t.ident for t in threading.enumerate()}
        start_ticks = ticks["n"]
        a1 = next(p for p in eng.store.partitions() if p.corpus_id == "a1")
        root_before = kernel.current_root(a1)
        with RootStall(ws.root("b1"), cap_s=30) as stall:
            try:
                # a poll that only counts itself is not a poll: a plain file edit in the HEALTHY corpus, made while b1 hangs,
                # must show up in the kernel's maintained root for a1 within 3 s (about 600 polls at this interval)
                with open(ws.file("a1"), "a", encoding="utf-8") as fh:
                    fh.write("- seen-during-stall: yes\n")
                end = time.monotonic() + 4
                seen_at = None
                while time.monotonic() < end:
                    if seen_at is None and kernel.current_root(a1) != root_before:
                        seen_at = ticks["n"] - start_ticks
                    if ticks["n"] - start_ticks >= 200 and seen_at is not None:
                        break
                    time.sleep(0.01)
                ck.ok(seen_at is not None, "an edit to the healthy corpus's file, made during the stall, was never seen by the polls")
                ran = ticks["n"] - start_ticks
                new = engine_threads_since(before)
                # a SECOND edit, made after the polls above: a watcher that saw only the first is stuck behind the hung root
                root_mid = kernel.current_root(a1)
                with open(ws.file("a1"), "a", encoding="utf-8") as fh:
                    fh.write("- seen-late-in-the-stall: yes\n")
                end2 = time.monotonic() + 1.5
                while kernel.current_root(a1) == root_mid and time.monotonic() < end2:
                    time.sleep(0.01)
                ck.ok(kernel.current_root(a1) != root_mid, "a second edit to the healthy corpus, made late in the stall, was never seen")
                ck.ok(ran >= 200, f"only {ran} of 200 polls started within 4s while the root hung (the watcher is stuck in one)")
                ck.ok(len(new) <= 1, f"the engine's thread count rose by {len(new)} (limit 1): {[t.name for t in new]}")
                # the block is installed in-process by the context manager, so no hit means the polls never touched the hung root
                ck.ok(stall.hit_count > 0, "no poll ever touched the hung root: an engine that polls nothing")
                # the stall must have HELD, not merely been reached: the engine's own registry says b1 is not answering (a harness gate that
                # logged the hit and returned without holding, mutant h1, left this test green without it)
                b1p = next(p for p in eng.store.partitions() if p.corpus_id == "b1")
                mark = kernel.root_mark(b1p)
                ck.ok(mark is not None and mark["status"] == "unresponsive", f"the stall did not hold: b1's mark is {mark}")
            finally:
                stall.release()
    finally:
        kernel.stop()
    ck.done("c5 (200 polls, one hung root)")


@red("c5: one stuck call stops the whole server (calls run on the request thread), so abandoned calls cannot be counted or "
     "capped; spec: calls with no registered root count toward a total cap of 16 abandoned calls, the 17th refuses "
     "engine-saturated, and it clears when they return")
def test_c5_abandoned_calls_are_capped(ws):
    free = ws.base / "free"                             # a directory no registry entry names
    free.mkdir()
    (free / "x.lts").write_text(Path(ws.file("a1")).read_text().replace("A1", "FR"), encoding="utf-8")
    ck = Checks()
    with RealServer(ws, stall_root=free, engaged=False, env=DEADLINES) as srv:
        srv.initialize(timeout=30)
        res, _ = srv.call("lts_add_property", {"path": str(free / "x.lts"), "node_id": "FR-N", "key": "ctl", "value": "1"}, timeout=10)
        if not res.get("success"):
            raise HarnessError(f"control write outside the stall failed: {res}")
        srv.engage()
        ask = Asker(srv, ck)
        codes = []
        for i in range(17):
            r, _ = ask(f"stalled write {i + 1}", "lts_add_property",
                       {"path": str(free / "x.lts"), "node_id": "FR-N", "key": f"k{i}", "value": "v"}, CALL_DEADLINE_S + 2.0)
            if ask.wedged:
                break
            codes.append(CODE_SATURATED if has_code(r, CODE_SATURATED) else CODE_OVERRAN if has_code(r, CODE_OVERRAN) else "other")
        else:
            ck.ok(codes[:16] == [CODE_OVERRAN] * 16 and codes[16] == CODE_SATURATED, f"first 16 call-overran then engine-saturated: {codes}")
            srv.release()
            time.sleep(0.5)
            r, _ = ask("write after release", "lts_add_property", {"path": str(free / "x.lts"), "node_id": "FR-N", "key": "after", "value": "1"})
            ck.ok(r and r.get("success"), "the refusal clears once the abandoned calls return")
        srv.require_hit()
    ck.done("c5 (abandoned-call cap)")


# ======================================================================================================================
# c6  build_store with a hung root
# ======================================================================================================================
def test_c6_build_store_with_a_hung_root(ws, monkeypatch):
    for k, v in ws.env(DEADLINES).items():
        monkeypatch.setenv(k, v)
    from factlattice_engine import config
    from factlattice_engine.tools import LtsEngine
    ck = Checks()
    base_threads = threading.active_count()
    with RootStall(ws.root("b1"), cap_s=30) as stall:
        outs = []
        try:
            first = run_limited(lambda: config.build_store(ws.registry), ROOT_DEADLINE_S + 2.0, name="build-1")
            outs.append(first)
            if not first.done:
                ck.fail.append(f"build_store did not return within {ROOT_DEADLINE_S + 2.0}s (deadline {ROOT_DEADLINE_S}s); blocked in {stall.hit_names()}")
            elif first.error is not None:
                raise HarnessError(f"build_store raised {type(first.error).__name__}")
            else:
                store = first.value
                f = store._failed_corpora.get("b1") or {}
                ck.ok(f.get("code") == CODE_BUILD, f"failed_corpora['b1'].code is {CODE_BUILD}: {f}")
                ck.ok("a1" in store.corpus_ids() and "b1" not in store.corpus_ids(), "a1 is served, b1 is not")
                hits_before = stall.hit_count
                eng = LtsEngine(store)
                got = run_limited(lambda: eng.find(corpus="b1", text="lanternfish"), 2.0, name="find-b1")
                ck.ok(got.done and got.value and got.value.get("success") is False, "find on the failed corpus returns success false")
                ck.ok(stall.hit_count == hits_before, "find on it made no disk call under the hung root")
                for i in range(5):
                    outs.append(run_limited(lambda: config.build_store(ws.registry), ROOT_DEADLINE_S + 1.0, name=f"build-r{i}"))
                grown = threading.active_count() - base_threads
                ck.ok(grown <= 1, f"repeated build_store calls during the stall grew the thread count by {grown} (cap: one stuck worker per root)")
        finally:
            stall.release()
            for o in outs:
                if o.thread is not None:
                    o.thread.join(10)
    ck.done("c6 (build_store, hung root)")


# ======================================================================================================================
# c9  link swap  (GREEN TODAY: a guard for slice 3, which caches each root's real path)
# ======================================================================================================================
_LDOC = '---\nlts: "1.1"\ntitle: t\ndomain: x.l\ndoctype: SPEC\n---\n\n# R [L-ROOT]\n\n- summary: r\n\n## N [L-N]\n\n- topic: {}\n'


def _link_swap(ws: Workspace, monkeypatch, *, through_link: bool) -> dict[str, Any]:
    base = ws.base
    A, B = base / "dirA", base / "dirB"
    for d, topic in ((A, "alphaword"), (B, "bravoword")):
        d.mkdir(exist_ok=True)
        (d / "l.lts").write_text(_LDOC.format(topic), encoding="utf-8")
    link = base / "link"
    link.symlink_to(A)
    registered = link if through_link else A            # control: a root that is NOT a link cannot follow a swap
    ws.registry_path.write_text(json.dumps({"l": {"roots": [{"path": str(registered)}], "domain_suffix": "x.l"}}))
    for k, v in ws.env().items():
        monkeypatch.setenv(k, v)
    from factlattice_engine import tools as T
    from factlattice_engine.server import build_tools
    eng = build_tools(watch=False, require_corpora=True)
    find = lambda t: T.dispatch("lts_find", {"corpus": "l", "text": t, "tier": "digest"}, tools=eng).get("count")
    out: dict[str, Any] = {"before": (find("alphaword"), find("bravoword"))}
    tmp = base / "swap"
    tmp.symlink_to(B)
    os.replace(tmp, link)                               # the swap, atomic
    eng.store.kernel.tick()                             # ONE poll
    out["after_one_poll"] = (find("alphaword"), find("bravoword"))
    w = T.dispatch("lts_add_property", {"path": str(link / "l.lts"), "node_id": "L-N", "key": "stamp", "value": "mid"}, tools=eng)
    out["write_ok"] = bool(w.get("success"))
    out["in_A"], out["in_B"] = "stamp: mid" in (A / "l.lts").read_text(), "stamp: mid" in (B / "l.lts").read_text()
    out["ledger_paths"] = [r.get("path_real") for r in ledger_records(ws)]
    out["B_real"] = os.path.realpath(B / "l.lts")
    return out


def test_c9_link_swap_is_seen_within_one_poll_and_the_write_lands_by_its_own_real_path(ws, monkeypatch):
    """GREEN TODAY, deliberately not a red record: the engine resolves nothing it caches, so a swapped root link is seen at
    the next poll. It guards slice 3, which keeps each root's real path from the last probe (x-membership) and so can regress
    this. The negative control runs the same observation on a root that is NOT a link: there the swap is (correctly) unseen,
    which proves the observation can tell 'seen' from 'not seen'."""
    ctl = _link_swap(ws, monkeypatch, through_link=False)
    assert ctl["before"] == (1, 0)
    assert ctl["after_one_poll"] == (1, 0), f"control: with no link there is nothing to follow; got {ctl['after_one_poll']}"
    real = Workspace(ws.base.parent / "w9")
    got = _link_swap(real, monkeypatch, through_link=True)
    assert got["before"] == (1, 0)
    assert got["after_one_poll"] == (0, 1), f"the swap was not seen after one poll: {got['after_one_poll']}"
    assert got["write_ok"] and got["in_B"] and not got["in_A"], got
    assert got["ledger_paths"] == [got["B_real"]], f"the ledger record names the file's own real path: {got['ledger_paths']} vs {got['B_real']}"


# ======================================================================================================================
# c11 (shape)  a SLOW root is not a hung one
# ======================================================================================================================
def false_marks(listing: dict | None) -> list[str]:
    """Corpora a listing reports UNRESPONSIVE. The c11 detector: on a root that answers, this must stay empty.

    SLICE 4 CHANGED WHAT COUNTS: it also counted `pending`, but x-server makes pending the DECLARED state of every corpus until its read ends
    (a slow corpus is pending and then joins), so a listing in the boot window must show it. A mark is a root that went a deadline with no
    progress; pending is not one. The detector still fires on a planted unresponsive entry (the negative control below)."""
    out = []
    for c in (listing or {}).get("corpora") or []:
        st = (c.get("root_health") or {}).get("status")
        if st == "unresponsive":
            out.append(f"{c.get('corpus_id')}:{st}")
    return out


def test_c11_detector_fires_on_a_planted_mark_and_stays_quiet_on_a_clean_listing():
    """The negative control for the detector the next test uses (the detector itself, not the engine)."""
    clean = {"corpora": [{"corpus_id": "a1", "total_nodes": 2}, {"corpus_id": "b1", "total_nodes": 2, "root_health": {"status": "ok"}}]}
    planted = {"corpora": [{"corpus_id": "a1", "total_nodes": 2}, {"corpus_id": "b1", "total_nodes": None, "root_health": {"status": "unresponsive"}}]}
    assert false_marks(clean) == []
    assert false_marks(planted) == ["b1:unresponsive"]
    assert false_marks(None) == []


# What c11 claims is that a root which ANSWERS, slowly, is never reported pending/unresponsive for want of an answer, and that a read of a healthy corpus is
# never refused. It does not claim that a listing which covers the slow root is quick: that listing verifies the slow root synchronously, and every disk call
# under it costs `slow_s`. The cold read of 90737fb measured 7 calls per verify and two verifies in the first listing after the corpus joins (2.1 s each, 4.3 s
# the first), and the test's old flat 3.0 s limit tripped on that cost, not on a mark (13 of 13 reads: no mark, ever). So the reply limit is TIED TO THE SLOW
# ROOT'S OWN PACING: the verify's calls times the per-call delay, for the two verifies a listing can make, plus headroom for the machine. The assertion that no
# `unresponsive` (or `pending` after the join) mark appears is unchanged.
C11_SLOW_S = 0.3
C11_VERIFY_CALLS = 7
C11_VERIFIES_PER_LISTING = 2
C11_HEADROOM_S = 3.0
C11_LIST_LIMIT_S = C11_VERIFY_CALLS * C11_VERIFIES_PER_LISTING * C11_SLOW_S + C11_HEADROOM_S      # 7.2 s


def test_c11_goes_red_when_the_deadline_measures_elapsed_time_not_progress(tmp_path):
    """The proof that c11's wider reply limit did not make it vacuous (round 13, in place of a test that compared the limit's constants with each other). A
    copy of this tree has the root deadline planted to measure ELAPSED TIME since the job started (`job.started`) where it measures time with NO PROGRESS
    (`job.progress`): the engine then marks the slow root that is still answering, and c11 must FAIL with its "reported pending/unresponsive" message. The
    reply limit bounds only how long a listing may take; the no-mark assertion is untouched."""
    import shutil
    plant = tmp_path / "plant"
    shutil.copytree(REPO, plant, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "private"))
    src = plant / "factlattice_engine" / "rootstate.py"
    text = src.read_text(encoding="utf-8")
    old = "job.overran = (job.stalled or now - job.progress > self.deadline_s) and not job.keep_on_overrun"
    assert text.count(old) == 1, "the plant did not apply"
    src.write_text(text.replace(old, old.replace("now - job.progress", "now - job.started")), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTLATTICE_", "LTS_", "PYTEST"))}
    env.update(HOME=str(tmp_path / "home"), TMPDIR=str(tmp_path))
    (tmp_path / "home").mkdir()
    p = subprocess.run([sys.executable, "-m", "pytest", "tests/test_bounded_roots.py::test_c11_shape_a_slow_root_is_never_marked_hung", "-q", "-p", "no:cacheprovider", "-x"],
                       cwd=str(plant), env=env, capture_output=True, text=True, timeout=300)
    assert p.returncode != 0 and "reported pending/unresponsive" in (p.stdout + p.stderr), (p.returncode, (p.stdout + p.stderr)[-800:])


def test_c11_shape_a_slow_root_is_never_marked_hung(ws, record_property):
    burners = [subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(min(2, os.cpu_count() or 1))]
    ck = Checks()
    try:
        load = {"loadavg_before": os.getloadavg(), "cpus": os.cpu_count(), "burners": len(burners), "slow_s_per_call": C11_SLOW_S, "list_limit_s": C11_LIST_LIMIT_S}
        # confirm_s: the child installs the block at interpreter start, and this test runs two CPU burners beside it on a machine at load 10 or more;
        # the harness's 20 s confirmation window was shorter than that start-up in about 3 runs of 14 (also on the tree before slice 3). Giving it
        # 90 s makes the test deterministic without accepting a harness failure as a red result.
        with RealServer(ws, stall_root=ws.root("b1"), engaged=True, slow_s=C11_SLOW_S, env=DEADLINES, confirm_s=90.0) as srv:
            ask = Asker(srv, ck)
            try:
                srv.initialize(timeout=HANDSHAKE_S)
            except NoReply:
                ck.fail.append(f"handshake: no initialize reply within {HANDSHAKE_S}s while the only problem is a SLOW root "
                               f"(boot is reading it first; stall model hit {len(srv.hits())} times)")
                ask.wedged = "initialize"
            marks: list[str] = []
            joined = False
            t_join = time.monotonic()
            end = t_join + JOIN_WINDOW_S
            while time.monotonic() < end and not ask.wedged:
                listing, _ = ask("list", "lts_list_corpora", {"heal": False}, C11_LIST_LIMIT_S)
                marks += false_marks(listing)
                b1 = entry(listing, "b1")
                if b1 and b1.get("total_nodes") == 2 and (b1.get("root_health") or {}).get("status") not in ("pending", "unresponsive"):
                    joined = True
                    load["join_after_s"] = round(time.monotonic() - t_join, 2)
                    break
                time.sleep(0.3)
            if not ask.wedged:
                ck.ok(joined, "the slow corpus joins (serves with its nodes) without a restart")
            ck.ok(not marks, f"a root that answers was reported pending/unresponsive: {sorted(set(marks))}")
            r, _ = ask("a1 named find", "lts_find", {"corpus": "a1", "text": "lanternfish", "tier": "digest"})
            ck.ok(r and r.get("success") and not has_code(r, CODE_UNRESPONSIVE), "a1 reads are never refused for a root-* reason")
            srv.require_hit()
        load["loadavg_after"] = os.getloadavg()
        record_property("load", json.dumps(load))
    finally:
        for b in burners:
            b.kill()
            b.wait(timeout=5)
    ck.done("c11 shape (slow root, CPU load)")
