"""[MECH-BUILD-STAMP] / [INV-RECONCILE-SERVES] — the reconcile must leave a SERVING successor.

test_code_currency.py proves the sentinel DETECTS divergence (a,b,c) — but every one of its
cases injects a fake `reconcile` callback, so the ONE reconcile that actually ships,
`reconcile_by_reexec`, had zero coverage. It could not work: under `python -m pkg.mod`,
`sys.argv[0]` is the module's FILE PATH, so `[sys.executable] + sys.argv` re-ran server.py as
a top-level script, where `from . import config` raises ImportError and the process exits 1.
The client saw stdout EOF and logged "connection closed (cleanly)" — a silent, permanent drop
of the lts-engine transport on every engine edit.

The acceptance bar this file adds, which the old bar was missing:

  (d) THE SUCCESSOR SERVES — after a code change lands under a LIVE stdio server, the process
                             is still ALIVE, still answering on the SAME pipe, and running the
                             NEW code.
                             -> test_reexec_argv_reenters_the_module_entrypoint  (unit)
                             -> test_live_stdio_server_survives_a_code_reconcile (end-to-end)

  (e) NO DROPPED REQUEST  — the relaunch happens BETWEEN requests, never mid-dispatch.
                             -> test_reconcile_waits_for_the_in_flight_request

The end-to-end case is deliberately a REAL subprocess over REAL pipes: the defect lives
entirely in how the process was launched, so any test that imports the module in-process
shares the blind spot and certifies the wrong conclusion.
"""
from __future__ import annotations

import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time

import pytest



import factlattice as _fl
import factlattice_engine as _fe

# Resolve the package by IMPORT, not by a path relative to this test file: the same test
# must run from tests/ and from the repo root, and must copy the package that is actually
# on sys.path (the one a deployed server would launch).
ENGINE_DIR = pathlib.Path(_fe.__file__).resolve().parent
# `factlattice_engine.server` imports `from factlattice import ...` — the sandbox's
# PYTHONPATH must resolve THAT SAME sibling package too, or the subprocess falls through
# to whatever `factlattice` happens to be installed system-wide (a stale editable install
# from a different checkout), silently exercising code this worktree never wrote. Measured:
# with only `factlattice_engine` copied into the sandbox, `python -c "import factlattice"`
# under the sandbox's env resolved to another checkout's `factlattice/__init__.py`
# — not this worktree — which is exactly the "which factlattice answered" ambiguity
# `test_live_stdio_server_survives_a_code_reconcile` cannot afford to have.
CORE_DIR = pathlib.Path(_fl.__file__).resolve().parent


# ---------------------------------------------------------------- (d) unit

def test_reexec_argv_reenters_the_module_entrypoint(monkeypatch):
    """A `-m` launch must re-exec as `-m`, not as a bare script path.

    This is the whole defect in one assertion. `sys.argv[0]` for `python -m pkg.mod` is
    `.../pkg/mod.py`; re-running THAT loses the package context and the module cannot import
    its own siblings. Python records the true entrypoint on `__main__.__spec__`.
    """
    class _Spec:
        name = "factlattice_engine.server"

    class _Main:
        __spec__ = _Spec()

    monkeypatch.setitem(sys.modules, "__main__", _Main())
    monkeypatch.setattr(sys, "argv", ["/abs/path/factlattice_engine/server.py"])

    from factlattice_engine.build_stamp import reexec_argv
    assert reexec_argv() == [sys.executable, "-m", "factlattice_engine.server"], (
        "re-exec argv did not re-enter the module entrypoint; a `-m` server that re-execs "
        "its own file path dies on ImportError and takes the MCP transport with it"
    )


def test_reexec_argv_falls_back_for_a_plain_script(monkeypatch):
    """A genuine `python foo.py` launch has no module spec — re-exec the script as-is."""
    class _Main:
        __spec__ = None

    monkeypatch.setitem(sys.modules, "__main__", _Main())
    monkeypatch.setattr(sys, "argv", ["/abs/foo.py", "--flag"])
    from factlattice_engine.build_stamp import reexec_argv
    assert reexec_argv() == [sys.executable, "/abs/foo.py", "--flag"]


# ------------------------------------------------------- (d) end-to-end

class _LiveServer:
    """A real `python -m factlattice_engine.server` over real pipes — the deployed shape.

    `rpc()` is DEADLINE-BOUNDED: stdout is drained by a reader thread into a queue, so a
    reply that never arrives FAILS (naming the request id and the wait) instead of blocking
    `readline()` forever. Defect ISSUE-UNBOUNDED-READ: under CPU
    contention the old unbounded read hung the suite for 13 min, and the test's own 25 s
    deadline loop never regained control. On failure the child is killed — no orphan.
    """

    def __init__(self, tmp_engine_root: pathlib.Path, cmd=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(tmp_engine_root)
        env["FACTLATTICE_CODE_WATCH_INTERVAL"] = "1"
        env["FACTLATTICE_CODE_RECONCILE"] = "restart"
        env["FACTLATTICE_CORPUS_ROOT"] = str(tmp_engine_root)
        env["ACME_ROOT"] = str(tmp_engine_root)
        env["FACTLATTICE_CORPUS_CONFIG"] = str(tmp_engine_root / "corpus-config.json")
        env["FACTLATTICE_STATE_DIR"] = str(tmp_engine_root / "state")   # carry lives here
        self.proc = subprocess.Popen(
            cmd or [sys.executable, "-m", "factlattice_engine.server"],
            cwd=str(tmp_engine_root), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self._replies: "queue.Queue" = queue.Queue()
        self.stderr_lines: list = []
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=lambda: [self.stderr_lines.append(l) for l in
                                         iter(self.proc.stderr.readline, b"")],
                         daemon=True).start()

    def _pump(self):
        for line in iter(self.proc.stdout.readline, b""):
            self._replies.put(line)
        self._replies.put(None)  # EOF sentinel

    def rpc(self, obj, timeout=15.0):
        rid = obj.get("id")
        t0 = time.monotonic()
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        self.proc.stdin.flush()
        try:
            line = self._replies.get(timeout=timeout)
        except queue.Empty:
            waited = time.monotonic() - t0
            self.close()  # no orphaned server
            raise AssertionError(
                f"no reply to request id={rid!r} ({obj.get('method')}) after waiting "
                f"{waited:.1f}s (deadline {timeout}s); server pid {self.proc.pid} killed"
            ) from None
        if line is None:
            raise AssertionError(
                f"server closed the pipe instead of answering request id={rid!r}")
        return json.loads(line)

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def test_rpc_deadline_fails_loudly_and_reaps_the_child(engine_sandbox):
    """NEGATIVE CONTROL for the bound: a child that reads and never answers.

    Before the fix this blocked forever in readline(); now it must raise within the
    deadline, name the request id and the wait, and leave no live child.
    """
    stub = [sys.executable, "-c", "import sys\nfor _ in sys.stdin: pass"]
    srv = _LiveServer(engine_sandbox, cmd=stub)
    t0 = time.monotonic()
    with pytest.raises(AssertionError) as ei:
        srv.rpc({"jsonrpc": "2.0", "id": 77, "method": "initialize"}, timeout=1.5)
    assert time.monotonic() - t0 < 10
    msg = str(ei.value)
    assert "id=77" in msg and "after waiting" in msg, msg
    assert srv.proc.poll() is not None, "child left running after a missed reply"


def test_live_stdio_server_survives_a_code_reconcile(engine_sandbox):
    """THE ENFORCING CASE — exactly ONE transport, surviving a code reconcile, same pipe.

    Counts asserted, because "it reconnected eventually" is what the defect looked like:
      * connections opened by the test .............. 1
      * connections that survive the reconcile ...... 1  (same pid: exec, not respawn)
      * responses lost .............................. 0
    And the successor must be running the NEW code, proven by a value the edit changed.
    """
    srv = _LiveServer(engine_sandbox)
    try:
        pid_before = srv.proc.pid
        before = srv.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert before["result"]["serverInfo"]["version"] == "0.1.0"

        # A code update lands on disk UNDER the live server — the build agent's edit.
        server_py = engine_sandbox / "factlattice_engine" / "server.py"
        server_py.write_text(
            server_py.read_text().replace('"version": "0.1.0"', '"version": "9.9.9-reloaded"')
        )

        # Give the sentinel its tick plus the successor's start-up.
        deadline = time.time() + 25
        while time.time() < deadline:
            if srv.proc.poll() is not None:
                raise AssertionError(
                    "the code reconcile KILLED the server: process exited "
                    f"{srv.proc.returncode}. A reconcile that leaves no serving successor is "
                    "the [RISK-STALE-CODE] fix causing the outage it was meant to prevent.\n"
                    "stderr:\n" + b"".join(srv.stderr_lines).decode()[-2000:]
                )
            after = srv.rpc({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
            if after["result"]["serverInfo"]["version"] == "9.9.9-reloaded":
                break
            time.sleep(1)
        else:
            raise AssertionError("server never picked up the on-disk code change")

        assert srv.proc.pid == pid_before, (
            "the successor is a DIFFERENT process — the transport was dropped and re-dialled "
            "rather than re-exec'd in place"
        )
        # Still serving real work on the same pipe.
        tools = srv.rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
        assert tools["result"]["tools"], "successor answers initialize but serves no tools"
    finally:
        srv.close()


# ------------------------------------------------------------- (e) quiesce

def test_reconcile_waits_for_the_in_flight_request():
    """The relaunch must happen BETWEEN requests, never mid-dispatch.

    Without this, the fixed re-exec trades a loud recurring drop for a rare SILENT hang: a
    request whose response was never written, and a client waiting out its timeout.
    """
    from factlattice_engine import build_stamp
    from factlattice_engine.build_stamp import SERVING_LOCK

    execed = threading.Event()
    released = threading.Event()

    def fake_execv(_path, _argv):
        execed.set()

    orig = build_stamp.os.execv
    build_stamp.os.execv = fake_execv
    try:
        with SERVING_LOCK:                       # stand in for a request being dispatched
            t = threading.Thread(target=build_stamp.reconcile_by_reexec, daemon=True)
            t.start()
            assert not execed.wait(1.0), (
                "reconcile re-exec'd while a request was in flight — that response is lost "
                "and the client hangs to timeout"
            )
            released.set()
        assert execed.wait(5.0), "reconcile never fired after the request completed"
        t.join(timeout=5)
    finally:
        build_stamp.os.execv = orig


@pytest.fixture()
def engine_sandbox(tmp_path):
    """A private copy of the engine package, so the test's edits touch no real tree.

    Copies `factlattice` ALONGSIDE `factlattice_engine` (same layout a deployed server
    sees): the subprocess's PYTHONPATH is exactly `tmp_path`, so if `factlattice` is not
    also rooted there, the import falls through to whatever `factlattice` happens to be
    installed system-wide — a DIFFERENT checkout, silently — and the test would exercise
    code this worktree never wrote while reporting on this worktree's behalf.
    """
    import shutil
    dst = tmp_path / "factlattice_engine"
    shutil.copytree(ENGINE_DIR, dst, ignore=shutil.ignore_patterns("__pycache__"))
    core_dst = tmp_path / "factlattice"
    shutil.copytree(CORE_DIR, core_dst, ignore=shutil.ignore_patterns("__pycache__"))
    corpus = tmp_path / "corpora" / "sandbox"
    corpus.mkdir(parents=True)
    (corpus / "sandbox.lts").write_text(
        "# Sandbox\n\n## [NODE-ONE] A node\n\n- status: draft\n\nBody text.\n")
    (tmp_path / "corpus-config.json").write_text(json.dumps({
        "sandbox": {"roots": [{"path": str(corpus), "area": "specs"}],
                    "domain_suffix": "proj.sandbox"}}))
    return tmp_path



# ------------------------------------- (e2) a request must not be consumed before the lock

def test_request_is_not_consumed_from_the_pipe_while_a_reconcile_holds_the_lock(monkeypatch):
    """DEFECT (2) of ISSUE-UNBOUNDED-READ, deterministic form.

    The reconcile re-exec runs while holding SERVING_LOCK. If `serve()` has already READ a
    request off the pipe when that exec lands, the request is destroyed with the process
    image and no reply is ever written (measured ~5% of live reconciles under CPU load).
    So: while the lock is held by someone else, a request written to the pipe must still be
    UNREAD in the kernel pipe (it survives exec) — and must be answered once the lock frees.
    """
    import io
    import select
    from factlattice_engine import server as srv_mod
    from factlattice_engine.build_stamp import SERVING_LOCK

    r, w = os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(r, "r", encoding="utf-8"))
    out = io.StringIO()
    t = threading.Thread(target=srv_mod.serve, kwargs={"stdout": out}, daemon=True)
    try:
        with SERVING_LOCK:                       # stand in for the reconcile mid-exec
            t.start()
            time.sleep(0.3)                      # let serve() reach its read
            os.write(w, b'{"jsonrpc":"2.0","id":5,"method":"initialize","params":{}}\n')
            time.sleep(0.5)                      # window in which exec would land
            unread, _, _ = select.select([r], [], [], 0)
            assert unread, (
                "serve() consumed the request from the pipe BEFORE taking SERVING_LOCK: a "
                "re-exec landing now would destroy it and the client would wait forever"
            )
        deadline = time.time() + 5
        while time.time() < deadline and '"id": 5' not in out.getvalue():
            time.sleep(0.05)
        assert '"id": 5' in out.getvalue(), "request never answered after the lock was freed"
    finally:
        os.close(w)
        t.join(timeout=5)


# ----------------------------------------------- (f) the stdin-carry protocol

def _edit_version(sandbox, tag):
    sp = sandbox / "factlattice_engine" / "server.py"
    import re
    sp.write_text(re.sub(r'"version": "[^"]*"', f'"version": "{tag}"', sp.read_text(), count=1))


def _wait_reexecs(srv, n, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        if sum(b"re-execing" in l for l in srv.stderr_lines) >= n:
            return
        time.sleep(0.1)
    raise AssertionError(f"fewer than {n} re-execs seen; stderr={b''.join(srv.stderr_lines)[-800:]!r}")


def _send_and_sync(srv, complete_id, partial: bytes):
    """Write ONE complete request plus a PARTIAL in a single write, then wait for the complete
    one's reply. serve() answers a line only after the read that delivered it, and that same
    read delivered the partial too — so the reply is an OBSERVABLE proof the partial is off
    the pipe and in user space (no sleeping and hoping)."""
    line = json.dumps({"jsonrpc": "2.0", "id": complete_id, "method": "initialize",
                       "params": {}}).encode() + b"\n"
    srv.proc.stdin.write(line + partial)
    srv.proc.stdin.flush()
    got = json.loads(srv._replies.get(timeout=20))
    assert got["id"] == complete_id, got


_HEAD = b'{"jsonrpc":"2.0","id":9,"method":"init'
_TAIL = b'ialize","params":{}}\n'


def test_partial_line_across_a_reconcile_gets_exactly_one_reply(engine_sandbox):
    srv = _LiveServer(engine_sandbox)
    try:
        srv.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, timeout=30)
        _send_and_sync(srv, 8, _HEAD)
        _edit_version(engine_sandbox, "9.9.9-reloaded")
        _wait_reexecs(srv, 1)
        srv.proc.stdin.write(_TAIL)
        srv.proc.stdin.flush()
        first = json.loads(srv._replies.get(timeout=20))
        assert first["id"] == 9, first
        assert first["result"]["serverInfo"]["version"] == "9.9.9-reloaded"
        second = srv.rpc({"jsonrpc": "2.0", "id": 10, "method": "initialize", "params": {}})
        assert second["id"] == 10, f"duplicate/extra reply after the partial: {second}"
    finally:
        srv.close()


def test_partial_survives_two_reconciles_with_one_reply(engine_sandbox):
    """Double reconcile: the carry is re-published and re-handed on each exec."""
    srv = _LiveServer(engine_sandbox)
    try:
        srv.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, timeout=30)
        _send_and_sync(srv, 8, _HEAD)
        _edit_version(engine_sandbox, "9.9.1")
        _wait_reexecs(srv, 1)
        # An edit landing before the successor took its baseline is absorbed, so keep editing
        # (unique tags) until a SECOND re-exec is actually observed.
        end, k = time.time() + 40, 0
        while sum(b"re-execing" in l for l in srv.stderr_lines) < 2 and time.time() < end:
            k += 1
            _edit_version(engine_sandbox, f"9.9.{k + 1}-again")
            time.sleep(1.5)
        _wait_reexecs(srv, 2, timeout=5)
        srv.proc.stdin.write(_TAIL)
        srv.proc.stdin.flush()
        first = json.loads(srv._replies.get(timeout=20))
        assert first["id"] == 9 and first["result"]["serverInfo"]["version"].startswith("9.9."), first
        second = srv.rpc({"jsonrpc": "2.0", "id": 10, "method": "initialize", "params": {}})
        assert second["id"] == 10, f"duplicate reply after a double reconcile: {second}"
    finally:
        srv.close()


def test_one_megabyte_partial_survives_a_reconcile_with_one_reply(engine_sandbox):
    """A 1 MB request split across a reconcile: exactly one reply. (The old env-var carry hit
    E2BIG above ~400 KB on macOS.)"""
    srv = _LiveServer(engine_sandbox)
    try:
        srv.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, timeout=30)
        pad = "x" * (1024 * 1024)
        body = ('{"jsonrpc":"2.0","id":9,"method":"initialize","params":{"pad":"' + pad
                + '"}}\n').encode()
        cut = len(body) - 50                       # everything but the last 50 bytes
        writer = threading.Thread(
            target=lambda: (srv.proc.stdin.write(body[:cut]), srv.proc.stdin.flush()),
            daemon=True)
        writer.start()
        time.sleep(1.0)                            # let the server drain the pipe into the carry
        _edit_version(engine_sandbox, "9.9.9-reloaded")
        _wait_reexecs(srv, 1)
        writer.join(timeout=20)
        srv.proc.stdin.write(body[cut:])
        srv.proc.stdin.flush()
        first = json.loads(srv._replies.get(timeout=30))
        assert first["id"] == 9, first
        assert first["result"]["serverInfo"]["version"] == "9.9.9-reloaded"
        second = srv.rpc({"jsonrpc": "2.0", "id": 10, "method": "initialize", "params": {}})
        assert second["id"] == 10, second
    finally:
        srv.close()


def test_unterminated_final_line_at_eof_is_still_answered(engine_sandbox):
    """Base behaviour (P2-2): `printf '{...}' | server` answers a last line with no newline."""
    srv = _LiveServer(engine_sandbox)
    try:
        srv.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, timeout=30)
        srv.proc.stdin.write(_HEAD + _TAIL.rstrip(b"\n"))   # NO trailing newline
        srv.proc.stdin.close()
        got = json.loads(srv._replies.get(timeout=20))
        assert got["id"] == 9, got
        assert srv.proc.wait(timeout=15) == 0
    finally:
        srv.close()


def test_eof_mid_partial_exits_cleanly_without_a_reply(engine_sandbox):
    """EOF in the middle of a truncated (unparseable) line: no hang, no reply, exit 0."""
    srv = _LiveServer(engine_sandbox)
    try:
        srv.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, timeout=30)
        srv.proc.stdin.write(_HEAD)
        srv.proc.stdin.close()
        assert srv.proc.wait(timeout=15) == 0
        assert srv._replies.get(timeout=5) is None, "a reply was produced for a truncated line"
    finally:
        srv.close()


def test_unterminated_valid_line_carried_across_a_reconcile_then_eof_is_answered_once(
        engine_sandbox):
    srv = _LiveServer(engine_sandbox)
    try:
        srv.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, timeout=30)
        _send_and_sync(srv, 8, _HEAD + _TAIL.rstrip(b"\n"))
        _edit_version(engine_sandbox, "9.9.9-reloaded")
        _wait_reexecs(srv, 1)
        srv.proc.stdin.close()
        got = json.loads(srv._replies.get(timeout=20))
        assert got["id"] == 9 and got["result"]["serverInfo"]["version"] == "9.9.9-reloaded", got
        assert srv._replies.get(timeout=10) is None, "extra reply after EOF"
    finally:
        srv.close()


# ---- carry transport, unit level (deterministic; fake execv).
# Hardening contract (the architect ruling, delta review P2-A): the carry lives ONLY in
# <state root>/carry/ (0700, ours, never TMPDIR); every check is made on an OPENED FD, never a
# path; a value is accepted only with the per-write random token that travels beside it.

@pytest.fixture()
def carry_env(monkeypatch, tmp_path):
    from factlattice_engine import build_stamp as bs
    state = tmp_path / "state"
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(state))
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir))
    monkeypatch.delenv(bs.STDIN_CARRY_ENV, raising=False)
    monkeypatch.delenv("FACTLATTICE_STDIN_CARRY_TOKEN", raising=False)
    bs.set_stdin_carry(b"")
    yield bs, state / "carry", tmpdir
    bs.set_stdin_carry(b"")
    os.environ.pop(bs.STDIN_CARRY_ENV, None)
    os.environ.pop("FACTLATTICE_STDIN_CARRY_TOKEN", None)


def _planted(bs, monkeypatch, data=b"half a request"):
    """Run a real reconcile (fake execv) so the PREDECESSOR writes a genuine carry; return
    (path, token). The envs are left set, exactly as a successor would inherit them."""
    bs.set_stdin_carry(data)
    monkeypatch.setattr(bs.os, "execv", lambda *_a: None)
    bs.reconcile_by_reexec()
    return os.environ[bs.STDIN_CARRY_ENV], os.environ["FACTLATTICE_STDIN_CARRY_TOKEN"]


def _refused(bs, caplog, expect_fragment=None):
    import logging
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        got = bs.take_inherited_stdin_carry()
    assert got == b"", f"a bad carry was ACCEPTED: {got[:40]!r}"
    msgs = [r.getMessage() for r in caplog.records if "refus" in r.getMessage().lower()]
    assert msgs, "refused silently (no log line)"
    if expect_fragment:
        assert any(expect_fragment in m for m in msgs), (expect_fragment, msgs)
    assert bs.STDIN_CARRY_ENV not in os.environ
    assert "FACTLATTICE_STDIN_CARRY_TOKEN" not in os.environ
    return msgs


def test_carry_travels_by_private_file_in_the_engine_state_dir(carry_env, monkeypatch):
    bs, root, tmpdir = carry_env
    old_umask = os.umask(0)                      # hostile umask: modes must still be exact
    try:
        big = os.urandom(1024 * 1024)
        path, token = _planted(bs, monkeypatch, big)
    finally:
        os.umask(old_umask)
    assert len(path) < 4096 and len(token) == 32
    assert os.path.dirname(path) == str(root), "carry written outside <state>/carry"
    assert os.path.basename(path) == f"carry-{token}"
    assert (os.lstat(root).st_mode & 0o777) == 0o700
    assert (os.lstat(path).st_mode & 0o777) == 0o600
    assert list(tmpdir.iterdir()) == [], "the carry touched TMPDIR"
    assert bs.take_inherited_stdin_carry() == big
    assert not os.path.exists(path), "the carry file was not unlinked"
    assert bs.STDIN_CARRY_ENV not in os.environ


def test_failed_execv_restores_env_and_filesystem(carry_env, monkeypatch):
    bs, root, tmpdir = carry_env
    bs.set_stdin_carry(b"half a request")

    def boom(_p, _a):
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr(bs.os, "execv", boom)
    with pytest.raises(OSError):
        bs.reconcile_by_reexec()
    assert bs.STDIN_CARRY_ENV not in os.environ, "a failed exec left the env var set"
    assert "FACTLATTICE_STDIN_CARRY_TOKEN" not in os.environ
    leftover = list(root.iterdir()) if root.exists() else []
    assert leftover == [] and list(tmpdir.iterdir()) == [], leftover
    import subprocess as sp
    assert sp.run([sys.executable, "-c", "pass"]).returncode == 0     # env not poisoned
    assert bs._stdin_carry == b"half a request", "the old process lost its own carry"


def test_old_process_keeps_serving_and_completes_the_partial_after_a_failed_exec(
        carry_env, monkeypatch):
    import io
    bs, root, tmpdir = carry_env
    from factlattice_engine import server as srv_mod
    r, w = os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(r, "r", encoding="utf-8"))
    out = io.StringIO()
    t = threading.Thread(target=srv_mod.serve, kwargs={"stdout": out}, daemon=True)
    t.start()
    try:
        os.write(w, b'{"jsonrpc":"2.0","id":8,"method":"initialize","params":{}}\n' + _HEAD)
        end = time.time() + 10
        while '"id": 8' not in out.getvalue() and time.time() < end:
            time.sleep(0.02)
        assert '"id": 8' in out.getvalue()                       # partial is now in the carry
        monkeypatch.setattr(bs.os, "execv", lambda *_: (_ for _ in ()).throw(OSError(7, "E2BIG")))
        with pytest.raises(OSError):
            bs.reconcile_by_reexec()
        os.write(w, _TAIL)
        end = time.time() + 10
        while '"id": 9' not in out.getvalue() and time.time() < end:
            time.sleep(0.02)
        assert out.getvalue().count('"id": 9') == 1, out.getvalue()
        assert (list(root.iterdir()) if root.exists() else []) == []
    finally:
        os.close(w)
        t.join(timeout=5)


# ---- one test per guard: deleting ANY guard must fail exactly its own test.

def test_guard_symlinked_carry_dir_refused(carry_env, monkeypatch, caplog):
    bs, root, _ = carry_env
    path, _tok = _planted(bs, monkeypatch)
    real = root.with_name("carry.real")
    root.rename(real)
    root.symlink_to(real)                          # attacker swaps the dir for a symlink
    _refused(bs, caplog)
    assert (real / os.path.basename(path)).exists(), "followed a symlinked dir and consumed it"


def test_guard_carry_dir_not_0700_refused(carry_env, monkeypatch, caplog):
    bs, root, _ = carry_env
    path, _tok = _planted(bs, monkeypatch)
    os.chmod(root, 0o755)
    _refused(bs, caplog)
    assert os.path.exists(path)


def test_guard_carry_dir_other_owner_refused(carry_env, monkeypatch, caplog):
    bs, root, _ = carry_env
    path, _tok = _planted(bs, monkeypatch)
    real = os.getuid()
    calls = iter([real + 1])                       # ONLY the dir check sees a foreign owner
    monkeypatch.setattr(bs, "_uid", lambda: next(calls, real), raising=False)
    _refused(bs, caplog, "carry dir owned")
    assert os.path.exists(path)


def test_guard_symlinked_file_refused(carry_env, monkeypatch, caplog, tmp_path):
    bs, root, _ = carry_env
    path, _tok = _planted(bs, monkeypatch)
    victim = tmp_path / "victim"
    victim.write_bytes(b"secret")
    os.chmod(victim, 0o600)
    os.unlink(path)
    os.symlink(victim, path)
    _refused(bs, caplog)
    assert victim.read_bytes() == b"secret"


def test_guard_hardlinked_file_refused(carry_env, monkeypatch, caplog):
    bs, root, _ = carry_env
    path, _tok = _planted(bs, monkeypatch)
    os.link(path, root / "alias")                  # nlink 2: the file is also reachable elsewhere
    _refused(bs, caplog)
    assert os.path.exists(path) and (root / "alias").exists()


def test_guard_loose_mode_file_refused(carry_env, monkeypatch, caplog):
    bs, root, _ = carry_env
    path, _tok = _planted(bs, monkeypatch)
    os.chmod(path, 0o666)
    _refused(bs, caplog)
    assert os.path.exists(path)


def test_guard_file_other_owner_refused(carry_env, monkeypatch, caplog):
    bs, root, _ = carry_env
    path, _tok = _planted(bs, monkeypatch)
    real = os.getuid()
    calls = iter([real])                           # 1st call: the DIR check passes; then 'other'
    monkeypatch.setattr(bs, "_uid", lambda: next(calls, real + 1), raising=False)
    _refused(bs, caplog)
    assert os.path.exists(path)


def test_guard_non_regular_file_refused_without_hanging(carry_env, monkeypatch, caplog):
    bs, root, _ = carry_env
    path, _tok = _planted(bs, monkeypatch)
    os.unlink(path)
    os.mkfifo(path, 0o600)                         # a blocking open would hang forever
    _refused(bs, caplog)


def test_guard_path_outside_the_carry_root_refused(carry_env, monkeypatch, caplog, tmp_path):
    bs, root, _ = carry_env
    path, tok = _planted(bs, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    moved = elsewhere / os.path.basename(path)
    import shutil
    shutil.copy2(path, moved)                      # the real file STAYS in the root: a guard
    os.environ[bs.STDIN_CARRY_ENV] = str(moved)    # that only compared basenames would accept
    _refused(bs, caplog, "not directly inside")
    assert moved.exists() and os.path.exists(path)


def test_guard_unnormalised_path_refused(carry_env, monkeypatch, caplog):
    bs, root, _ = carry_env
    path, _tok = _planted(bs, monkeypatch)
    os.environ[bs.STDIN_CARRY_ENV] = str(root / ".." / "carry" / os.path.basename(path))
    _refused(bs, caplog, "not directly inside")
    assert os.path.exists(path)


def test_guard_token_mismatch_refused(carry_env, monkeypatch, caplog):
    bs, root, _ = carry_env
    path, _tok = _planted(bs, monkeypatch)
    os.environ["FACTLATTICE_STDIN_CARRY_TOKEN"] = "0" * 32
    _refused(bs, caplog)
    assert os.path.exists(path)


def test_guard_missing_token_refused(carry_env, monkeypatch, caplog):
    bs, root, _ = carry_env
    path, _tok = _planted(bs, monkeypatch)
    os.environ.pop("FACTLATTICE_STDIN_CARRY_TOKEN")
    _refused(bs, caplog)
    assert os.path.exists(path)


def test_guard_well_formed_preset_for_a_file_the_engine_did_not_write(carry_env, caplog):
    """A hostile launcher pre-sets a perfectly shaped value pointing at a 0600 file of ours."""
    bs, root, _ = carry_env
    root.mkdir(parents=True, mode=0o700)
    forged = root / ("carry-" + "ab" * 16)
    forged.write_bytes(b'{"jsonrpc":"2.0","id":4242,"method":"initialize"}\n')
    os.chmod(forged, 0o600)
    os.environ[bs.STDIN_CARRY_ENV] = str(forged)             # no token var at all
    _refused(bs, caplog)
    assert forged.exists(), "a file the engine did not write was consumed"


def test_guard_validation_is_on_the_fd_not_the_path_toctou(carry_env, monkeypatch):
    """Swap a 0666 file over the path BETWEEN open and read: the fd (the original, 0600) is
    what was validated and read; the swapped file is neither read nor unlinked."""
    bs, root, _ = carry_env
    path, _tok = _planted(bs, monkeypatch, b"the original")

    def swap():
        os.rename(path, str(root / "moved-original"))      # keeps the original at nlink 1
        with open(path, "wb") as fh:
            fh.write(b"attacker payload")
        os.chmod(path, 0o666)

    monkeypatch.setattr(bs, "_after_carry_open_hook", swap, raising=False)
    got = bs.take_inherited_stdin_carry()
    assert got == b"the original", got
    assert os.path.exists(path) and open(path, "rb").read() == b"attacker payload", \
        "the swapped-in file was unlinked (unlink must be by identity)"


def test_a_refused_value_is_logged_as_length_and_hash_never_raw(carry_env, caplog):
    bs, root, _ = carry_env
    marker = "ATTACKER-CONTROLLED-MARKER-TEXT"
    os.environ[bs.STDIN_CARRY_ENV] = "/nonexistent/" + marker
    msgs = _refused(bs, caplog)
    joined = " ".join(msgs)
    assert marker not in joined, "raw attacker text was logged"
    assert "len=" in joined and "sha256=" in joined


def test_startup_sweep_removes_only_stale_regular_files_we_own(carry_env, monkeypatch):
    bs, root, _ = carry_env
    _planted(bs, monkeypatch)                      # creates the root; consume the fresh one below
    for k in list(os.environ):
        if k.startswith("FACTLATTICE_STDIN_CARRY"):
            os.environ.pop(k)
    old = time.time() - 3600
    def mk(name, age=old):
        f = root / name
        f.write_bytes(b"x")
        os.chmod(f, 0o600)
        os.utime(f, (age, age))
        return f
    stale = mk("carry-" + "1" * 32)
    fresh = mk("carry-" + "2" * 32, age=time.time())
    other = mk("not-a-carry-file")
    sub = root / ("carry-" + "3" * 32 + "-dir")
    sub.mkdir()
    os.utime(sub, (old, old))
    link = root / ("carry-" + "4" * 32 + "-link")
    link.symlink_to(fresh)
    os.utime(link, (old, old), follow_symlinks=False)
    bs.sweep_stale_carry(max_age_s=600)
    assert not stale.exists(), "stale carry not swept"
    assert fresh.exists() and other.exists() and sub.exists() and link.is_symlink()


def test_sweep_leaves_other_owners_files_alone(carry_env, monkeypatch):
    bs, root, _ = carry_env
    _planted(bs, monkeypatch)
    for k in list(os.environ):
        if k.startswith("FACTLATTICE_STDIN_CARRY"):
            os.environ.pop(k)
    f = root / ("carry-" + "5" * 32)
    f.write_bytes(b"x")
    os.chmod(f, 0o600)
    os.utime(f, (time.time() - 3600,) * 2)
    real = os.getuid()
    calls = iter([real])                           # dir check passes, file looks foreign
    monkeypatch.setattr(bs, "_uid", lambda: next(calls, real + 1), raising=False)
    bs.sweep_stale_carry(max_age_s=600)
    assert f.exists()


def test_sweep_does_nothing_through_a_symlinked_dir(carry_env, monkeypatch):
    bs, root, _ = carry_env
    _planted(bs, monkeypatch)
    for k in list(os.environ):
        if k.startswith("FACTLATTICE_STDIN_CARRY"):
            os.environ.pop(k)
    real = root.with_name("carry.real")
    root.rename(real)
    root.symlink_to(real)
    f = real / ("carry-" + "6" * 32)
    f.write_bytes(b"x")
    os.chmod(f, 0o600)
    os.utime(f, (time.time() - 3600,) * 2)
    bs.sweep_stale_carry(max_age_s=600)
    assert f.exists(), "the sweep followed a symlinked carry dir"


def test_serve_sweeps_stale_carry_at_startup(carry_env, monkeypatch):
    import io
    bs, root, _ = carry_env
    _planted(bs, monkeypatch)
    for k in list(os.environ):
        if k.startswith("FACTLATTICE_STDIN_CARRY"):
            os.environ.pop(k)
    f = root / ("carry-" + "7" * 32)
    f.write_bytes(b"x")
    os.chmod(f, 0o600)
    os.utime(f, (time.time() - 3600,) * 2)
    from factlattice_engine import server as srv_mod
    r, w = os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(r, "r", encoding="utf-8"))
    os.close(w)
    srv_mod.serve(stdout=io.StringIO())
    assert not f.exists(), "serve() did not sweep stale carry files at startup"


def test_the_inherited_carry_is_taken_before_the_sentinel_thread_starts(monkeypatch):
    """Structural order assertion (P2-3): a reconcile firing between build_tools() (which starts
    the sentinel) and the first read would see an empty carry and discard the inherited one."""
    from factlattice_engine import server as srv_mod, build_stamp as bs
    order = []
    real_take, real_tools = bs.take_inherited_stdin_carry, srv_mod.build_tools
    monkeypatch.setattr(bs, "take_inherited_stdin_carry",
                        lambda: (order.append("take"), real_take())[1])
    monkeypatch.setattr(srv_mod, "build_tools",
                        lambda: (order.append("tools"), real_tools())[1])
    r, w = os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(r, "r", encoding="utf-8"))
    os.close(w)                                   # immediate EOF
    srv_mod.serve(stdout=__import__("io").StringIO())
    assert order[:2] == ["take", "tools"], order
