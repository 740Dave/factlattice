"""[MECH-SEMANTIC-EMBEDD] One shared embedding service per user, behind LTS_SEMANTIC_EMBEDDER=shared.

Measured (the architect S7, 2026-10-02): every engine process that serves one semantic call loads its own
pinned model, 1.0-2.4 GB peak RSS. This module shares ONLY "texts -> vectors": a per-user
service (`lts-embedd`) holds the model and tokenizer, and engines keep their corpora, sidecars,
BM25 and vector search local. The exact path never touches it.

* Socket: <state_root>/run/embedd-<material hash>.sock. The run dir is 0700 (chmod after mkdir,
  so the umask cannot loosen it) and must be a real dir owned by this euid; a symlinked or
  foreign-owned run dir, socket or pidfile is refused. Peers are checked by kernel credentials
  (LOCAL_PEERCRED / SO_PEERCRED): another euid is refused; no credential means refused.
* Version safety: the socket name carries a hash of the full vector-shaping material
  (index._version_material), and the `hello` handshake compares the material itself. Any
  mismatch is refused (`embedder_version_mismatch`); two vector spaces are never mixed.
* Parity: the service runs the engine's own Embedder.plan() batches, so vectors are
  byte-identical to in-process ones.
* Scheduling: two lanes on the one model session: QUERY_THREADS threads serve query batches and
  one thread serves build batches, concurrently (as in-process inference is), so a query never
  waits for a running build batch. Measured before: p95 2.6 s, max 11 s during a rebuild.
* Failure: every frame has a client deadline; a dead/stalled/refused service surfaces as
  SemanticUnavailable (the caller reports `status: unavailable`), never a hang. EOF mid-call:
  reconnect (respawn if needed) once and retry the idempotent call. Vectors are returned only
  when every row has arrived: never partial.
* Lifecycle: the first engine that needs it spawns it (a flock'd spawn guard: exactly one);
  the service holds an O_EXCL|O_NOFOLLOW pidfile, and exits after LTS_EMBEDD_IDLE_S idle.
* Always-on [MECH-EMBEDD-ALWAYS-ON]: `keep` runs the same Server with idle exit off under a
  LaunchAgent (`launchagent install|uninstall|status`), follows the live code (exit 75 on a source
  change), waits rather than fights when an on-demand service holds the pidfile, and stops
  restarting itself after a crash loop.
* Privacy: the service logs lifecycle lines only, never text, and never learns corpus names.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import heapq
import itertools
import json
import os
import secrets
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from typing import List, Optional

from .embedder import ModelPin, SemanticUnavailable

PROTO = 1
MAX_HEADER = 256 << 20
MAX_PAYLOAD = 1 << 30
SOCK_PATH_MAX = 100                         # AF_UNIX sun_path is 104 bytes on macOS
QUERY_THREADS = 2                           # query lane; builds get one thread of their own
INTERACTIVE_MAX = 256                       # an "interactive" doc embed above this goes to the build lane
BUILD_S_PER_TEXT = 0.5                      # a build's total cap: max(startup_s, this x texts)


def _heartbeat_s() -> float:
    return float(os.environ.get("LTS_EMBEDD_HEARTBEAT_S", "2"))


class ProtocolError(ValueError):
    """A frame that is malformed, oversized, from another request, or names bad rows."""


class Unsafe(Exception):
    """A run dir / socket / pidfile that is a symlink, not ours, or the wrong kind of file."""


def _frame_deadline() -> float:
    return float(os.environ.get("LTS_EMBEDD_DEADLINE_S", "10"))


def _startup_deadline() -> float:
    return float(os.environ.get("LTS_EMBEDD_STARTUP_S", "60"))


# --- material ---------------------------------------------------------------------------------
class _Spec:
    """What index._version_material reads off an embedder, without loading the model."""

    def __init__(self, pin: ModelPin, max_len: int):
        self.pin, self.max_len = pin, max_len


def model_id_of(pin: ModelPin) -> str:
    return f"{pin.name}@{pin.onnx_sha256[:12]}"


def material(pin: ModelPin, max_len: int) -> str:
    from .index import _version_material
    return _version_material(model_id_of(pin), _Spec(pin, max_len))


def material_hash(mat: str) -> str:
    return hashlib.sha256(mat.encode()).hexdigest()[:16]


# --- filesystem safety ------------------------------------------------------------------------
def _check_owned(path: str, kind) -> None:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise Unsafe(f"{path} is a symlink")
    if not kind(st.st_mode):
        raise Unsafe(f"{path} is not the expected file type")
    if st.st_uid != os.geteuid():
        raise Unsafe(f"{path} is owned by uid {st.st_uid}, not {os.geteuid()}")


def run_dir(state_root: str) -> str:
    """<state_root>/run, created 0700 whatever the umask, and verified ours."""
    d = os.path.join(state_root, "run")
    os.makedirs(state_root, exist_ok=True)
    _check_owned(state_root, stat.S_ISDIR)       # the state root itself: not a symlink, ours (P3-4)
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    _check_owned(d, stat.S_ISDIR)
    os.chmod(d, 0o700)
    return d


def pidfiles(state_root: str) -> List[str]:
    d = os.path.join(state_root, "run")
    try:
        return sorted(os.path.join(d, n) for n in os.listdir(d) if n.startswith("embedd-") and n.endswith(".pid"))
    except OSError:
        return []


def wait_for_socket(path: str, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if os.path.exists(path):
            return True
        time.sleep(0.02)
    return False


# --- framing ----------------------------------------------------------------------------------
def _send(sock, obj: dict, payload: bytes = b"") -> None:
    h = json.dumps(obj).encode()
    sock.sendall(struct.pack(">II", len(h), len(payload)) + h + payload)


def _recvn(sock, n: int, end: Optional[float] = None) -> bytes:
    """Read exactly n bytes. With `end` (time.monotonic), the WHOLE read must finish by then:
    the socket timeout is re-armed to the time left before every recv, so a peer trickling a
    byte at a time cannot stretch it (review P1-4: one byte per 0.5 s held a 2 s call 10 s)."""
    buf = bytearray()
    while len(buf) < n:
        if end is not None:
            left = end - time.monotonic()
            if left <= 0:
                raise socket.timeout("deadline passed")
            sock.settimeout(left)
        chunk = sock.recv(min(n - len(buf), 1 << 20))
        if not chunk:
            raise EOFError("peer closed")
        buf += chunk
    return bytes(buf)


def _recv(sock, end: Optional[float] = None):
    hl, pl = struct.unpack(">II", _recvn(sock, 8, end))
    if hl == 0 or hl > MAX_HEADER or pl > MAX_PAYLOAD:
        raise ProtocolError(f"bad frame sizes {hl}/{pl}")
    h = json.loads(_recvn(sock, hl, end))
    if not isinstance(h, dict):
        raise ProtocolError("frame header is not an object")
    return h, (_recvn(sock, pl, end) if pl else b"")


def _peer_uid(conn) -> Optional[int]:
    """The connecting process's euid from the kernel, or None when the platform can't say."""
    try:
        if sys.platform == "darwin":
            raw = conn.getsockopt(0, 0x001, 4 + 4 + 2 + 2 + 4 * 16)   # SOL_LOCAL, LOCAL_PEERCRED: xucred
            return struct.unpack_from("=II", raw, 0)[1]
        if hasattr(socket, "SO_PEERCRED"):
            raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", raw)[1]
    except OSError:
        return None
    return None


# --- the service ------------------------------------------------------------------------------
class Server:
    def __init__(self, state_root: str, pin: ModelPin, model_root: Optional[str], max_len: int = 512,
                 idle_s: Optional[float] = None, idle_exit: bool = True):
        self.pin, self.model_root, self.max_len = pin, model_root, max_len
        self.material = material(pin, max_len)
        self.hash = material_hash(self.material)
        self.run = run_dir(state_root)
        self.sock_path = os.path.join(self.run, f"embedd-{self.hash}.sock")
        self.pid_path = os.path.join(self.run, f"embedd-{self.hash}.pid")
        if len(self.sock_path) > SOCK_PATH_MAX:
            raise Unsafe(f"socket path longer than {SOCK_PATH_MAX} bytes: {self.sock_path}")
        self.idle_s = float(os.environ.get("LTS_EMBEDD_IDLE_S", "600")) if idle_s is None else idle_s
        # [MECH-EMBEDD-ALWAYS-ON] False = never exit on idle (`keep`). idle_s still times out an idle
        # CONNECTION (P2-1), so a stale client socket is dropped and the client reconnects.
        self.idle_exit = idle_exit
        self._lanes = {0: [], 1: []}             # 0 = query batches, 1 = build batches (FIFO each)
        self._seq = itertools.count()
        self._cv = threading.Condition()
        self._conns = 0
        self._last = time.monotonic()
        self._stop = threading.Event()
        self._listener: Optional[socket.socket] = None
        self._embedder = None
        self._pid_fd: Optional[int] = None
        self._bound = False
        # Test seam only (P3-3): set on an in-process Server by a test. Never read from the
        # environment, so no deployment setting can slow the production inference loop.
        self.test_batch_delay_s = 0.0
        self.on_ready = None     # [MECH-EMBEDD-ALWAYS-ON] called once bound and serving (keep clears its tripped marker)

    # lifecycle ----------------------------------------------------------------------------
    def acquire_pidfile(self) -> bool:
        """Exactly one service per material. Liveness is an flock HELD on the pidfile for the
        service's whole life, never `kill -0` of the pid in it: a recycled pid (review P2-2a)
        or a zombie cannot fake a live service, and the kernel drops the lock when the holder
        dies. Opened O_NOFOLLOW, verified a regular file owned by this euid; never unlinked, so
        two starters can never delete each other's file (P2-2b). False = a live service holds it."""
        try:
            fd = os.open(self.pid_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as ex:
            if ex.errno == errno.ELOOP:
                raise Unsafe(f"{self.pid_path} is a symlink") from None
            raise
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
                raise Unsafe(f"{self.pid_path} is not a regular file owned by this user")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return False
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
            self._pid_fd = fd
            return True
        except BaseException:
            if self._pid_fd != fd:
                os.close(fd)
            raise

    def _bind(self) -> None:
        if os.path.lexists(self.sock_path):
            _check_owned(self.sock_path, stat.S_ISSOCK)
            probe = socket.socket(socket.AF_UNIX)
            try:
                probe.connect(self.sock_path)
                probe.close()
                raise Unsafe("another service answers on this socket")
            except (ConnectionRefusedError, FileNotFoundError):
                os.unlink(self.sock_path)                     # stale socket of ours
            finally:
                probe.close()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.sock_path)
        self._bound = True
        os.chmod(self.sock_path, 0o600)
        s.listen(64)
        s.settimeout(0.2)
        self._listener = s

    def _cleanup(self) -> None:
        """Remove only what THIS process made: its own bound socket; release (not unlink) the
        pidfile lock. A start refused at bind must not delete a live service's files (P2-2c)."""
        if self._bound:
            try:
                if os.path.lexists(self.sock_path):
                    _check_owned(self.sock_path, stat.S_ISSOCK)
                    os.unlink(self.sock_path)
            except (OSError, Unsafe):
                pass
        if self._pid_fd is not None:
            try:
                os.ftruncate(self._pid_fd, 0)
            except OSError:
                pass
            os.close(self._pid_fd)                       # releases the flock
            self._pid_fd = None

    def shutdown(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()

    def serve_forever(self) -> None:
        if self._pid_fd is None and not self.acquire_pidfile():
            return
        try:
            from .embedder import load_embedder
            from .index import _version_material
            self._embedder = load_embedder(self.pin, self.model_root, self.max_len)
            if _version_material(self._embedder.model_id, self._embedder) != material(self.pin, self.max_len):
                raise SemanticUnavailable("embedder_version_mismatch", "loaded model drifted from its pin")
            self._bind()
            if self.on_ready is not None:
                self.on_ready()
            for i in range(QUERY_THREADS):
                threading.Thread(target=self._infer_loop, args=(0,), name=f"embedd-query-{i}", daemon=True).start()
            threading.Thread(target=self._infer_loop, args=(1,), name="embedd-build", daemon=True).start()
            while not self._stop.is_set():
                try:
                    conn, _ = self._listener.accept()
                except socket.timeout:
                    with self._cv:
                        idle = (self.idle_exit and self._conns == 0 and not any(self._lanes.values())
                                and time.monotonic() - self._last > self.idle_s)
                    if idle:
                        break
                    continue
                with self._cv:
                    self._conns += 1
                threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()
        finally:
            self._stop.set()
            with self._cv:
                self._cv.notify_all()
            if self._listener is not None:
                self._listener.close()
            self._cleanup()

    # inference ----------------------------------------------------------------------------
    def _infer_loop(self, lane: int) -> None:
        delay = self.test_batch_delay_s
        q = self._lanes[lane]
        while True:
            with self._cv:
                while not q and not self._stop.is_set():
                    self._cv.wait(0.5)
                if self._stop.is_set() and not q:
                    return
                _seq, job = heapq.heappop(q)
            if job.get("cancelled"):                 # its client is gone (P2-4): don't spend the model on it
                job["done"].set()
                continue
            try:
                if delay:
                    time.sleep(delay)
                job["vec"] = self._embedder.run_batch(job["ids"], job["mask"])
            except Exception as ex:   # noqa: BLE001 -- reported to the waiting connection
                job["error"] = f"{type(ex).__name__}: {ex}"
            job["done"].set()

    def _serve_conn(self, conn) -> None:
        jobs: list = []
        try:
            conn.settimeout(self.idle_s)            # an idle client's socket is closed (P2-1), so
            if _peer_uid(conn) != os.geteuid():     # idle-exit can fire; the client reconnects
                _send(conn, {"ok": False, "error": "peer refused: not the owner"})
                return
            h, _ = _recv(conn)
            if h.get("op") != "hello" or h.get("proto") != PROTO:
                _send(conn, {"ok": False, "error": "protocol"})
                return
            if h.get("material") != self.material:
                _send(conn, {"ok": False, "error": "embedder_version_mismatch", "material": self.material})
                return
            _send(conn, {"ok": True, "material": self.material, "pid": os.getpid()})
            while not self._stop.is_set():
                h, _ = _recv(conn)
                rid = h.get("rid")
                texts, query = h.get("texts"), bool(h.get("query"))
                lane, batch = h.get("lane", 0 if query else 1), h.get("batch") or 32
                if (h.get("op") != "embed" or not isinstance(texts, list)
                        or not all(isinstance(t, str) for t in texts) or lane not in (0, 1)
                        or type(batch) is not int or not 1 <= batch <= 256):
                    _send(conn, {"rid": rid, "error": "bad request"})
                    continue
                plan = self._embedder.plan(texts, query, batch)
                jobs = []
                with self._cv:
                    for idx, ids, mask in plan:
                        job = {"idx": idx, "ids": ids, "mask": mask, "done": threading.Event()}
                        heapq.heappush(self._lanes[lane], (next(self._seq), job))
                        jobs.append(job)
                    self._cv.notify_all()
                hb, last_sent = _heartbeat_s(), time.monotonic()
                for job in jobs:
                    while not job["done"].wait(min(0.5, hb)):
                        if self._stop.is_set():
                            return
                        if time.monotonic() - last_sent >= hb:     # queued behind other work: say so,
                            _send(conn, {"rid": rid, "wait": True})  # so a build is not timed out (P2-R1)
                            last_sent = time.monotonic()
                    last_sent = time.monotonic()
                    if "error" in job:
                        _send(conn, {"rid": rid, "error": job["error"]})
                        break
                    _send(conn, {"rid": rid, "idx": job["idx"]}, job["vec"].tobytes())
                else:
                    _send(conn, {"rid": rid, "done": True, "n": len(texts)})
                jobs = []
                with self._cv:
                    self._last = time.monotonic()
        except (EOFError, OSError):
            pass                                     # the client went away: routine
        except ProtocolError as ex:
            print(f"lts-embedd {os.getpid()} refused a malformed request: {ex}", file=sys.stderr, flush=True)
        except Exception:   # noqa: BLE001 -- an internal fault: close this connection, keep the service, log it (R6)
            import traceback
            print(f"lts-embedd {os.getpid()} internal error in a connection:", file=sys.stderr, flush=True)
            traceback.print_exc(file=sys.stderr)
        finally:
            for job in jobs:                         # the client vanished mid-request (P2-4)
                job["cancelled"] = True
            conn.close()
            with self._cv:
                self._conns -= 1
                self._last = time.monotonic()


# --- the client -------------------------------------------------------------------------------
_LAUNCHER = (
    "import json, os, subprocess, sys\n"
    "a = json.loads(sys.argv[1])\n"
    "fd = os.open(a['log'], os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)\n"
    "subprocess.Popen(a['argv'], stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, "
    "start_new_session=True, close_fds=True)\n")

class SharedEmbedder:
    """Drop-in for Embedder.embed() that asks the shared service. Raises SemanticUnavailable on
    any failure (the caller labels it `unavailable`); never hangs, never returns partial rows."""

    supports_interactive = True     # SemanticIndex.refresh(interactive=True) may route to the query lane

    def __init__(self, pin: ModelPin, model_root: Optional[str] = None, max_len: int = 512,
                 state_root: Optional[str] = None, frame_deadline_s: Optional[float] = None,
                 startup_s: Optional[float] = None):
        self.pin, self.model_root, self.max_len = pin, model_root, max_len
        self.model_id = model_id_of(pin)
        self.state_root = state_root or os.environ.get("LTS_ENGINE_STATE_DIR") or os.path.expanduser("~/.lts-engine")
        self.material = material(pin, max_len)
        self.material_hash = material_hash(self.material)
        self.frame_deadline_s = frame_deadline_s if frame_deadline_s is not None else _frame_deadline()
        self.startup_s = startup_s if startup_s is not None else _startup_deadline()
        self.server_pid: Optional[int] = None
        # One connection PER THREAD: an engine's background index worker and its query threads
        # each talk to the service on their own socket, so the service's priority queue (query
        # before build) actually applies. A single shared connection serialised them: measured
        # 2026-10-02, a query waited 268.9 s behind a acme-docs rebuild (acceptance-3).
        self._local = threading.local()

    # connection ---------------------------------------------------------------------------
    def _paths(self):
        run = run_dir(self.state_root)
        base = os.path.join(run, f"embedd-{self.material_hash}")
        if len(base + ".sock") > SOCK_PATH_MAX:
            raise Unsafe(f"socket path longer than {SOCK_PATH_MAX} bytes")
        return run, base + ".sock", base + ".spawn"

    def _try_connect(self, path: str, end: float) -> Optional[socket.socket]:
        if not os.path.lexists(path):
            return None
        _check_owned(path, stat.S_ISSOCK)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(max(0.01, end - time.monotonic()))
        try:
            s.connect(path)
            return s
        except (ConnectionRefusedError, FileNotFoundError):
            s.close()
            return None

    def _spawn(self, run: str, end: float) -> None:
        """Start the service as a GRANDCHILD: a short-lived launcher starts it in its own session
        and exits, so launchd/init adopts and reaps it. A service that is an engine's own child
        becomes that engine's zombie when killed, and os.kill(pid, 0) then calls it alive
        (measured 2026-10-02: every respawn refused for the 60 s startup deadline). The wait for
        the launcher is bounded by the caller's budget (review P1-4: it was a fixed 30 s)."""
        pkg_parent = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env = dict(os.environ, PYTHONPATH=pkg_parent + os.pathsep + os.environ.get("PYTHONPATH", ""))
        argv = [sys.executable, "-m", "factlattice_engine.semantic.embedd", "serve",
                "--state-root", self.state_root, "--pin", json.dumps(asdict(self.pin)),
                "--max-len", str(self.max_len)] + (["--model-root", self.model_root] if self.model_root else [])
        log = os.path.join(run, f"embedd-{self.material_hash}.log")
        launcher = subprocess.Popen(
            [sys.executable, "-c", _LAUNCHER, json.dumps({"argv": argv, "log": log})],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
            close_fds=True)
        try:
            launcher.wait(timeout=max(0.05, end - time.monotonic()))
        except subprocess.TimeoutExpired:
            launcher.kill()                          # our own child: stop it, the caller answers unavailable
            launcher.wait()

    def _connect(self, end: float) -> socket.socket:
        """Connect, starting the service if none answers. Every wait (the spawn lock, the
        launcher, the service coming up, the hello) ends by `end`: a query's budget is its whole
        deadline, so it is answered `unavailable` rather than held while a service starts; a
        build may wait for a cold model load. A spawn carries on, and the next call finds it."""
        if getattr(self._local, "sock", None) is not None:
            return self._local.sock
        run, sock_path, spawn_path = self._paths()
        s = self._try_connect(sock_path, end)
        if s is None:
            fd = os.open(spawn_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                if os.fstat(fd).st_uid != os.geteuid():
                    raise Unsafe(f"{spawn_path} is not ours")
                locked = False
                while s is None and time.monotonic() < end:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # exactly one spawner
                        locked = True
                        break
                    except BlockingIOError:                  # another engine is spawning: wait for it
                        time.sleep(0.05)
                        s = self._try_connect(sock_path, end)
                if locked:
                    try:
                        s = self._try_connect(sock_path, end)
                        if s is None:
                            self._spawn(run, end)
                            while s is None and time.monotonic() < end:
                                time.sleep(0.05)
                                s = self._try_connect(sock_path, end)
                    finally:
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            if s is None:
                raise SemanticUnavailable("shared_embedder_unavailable",
                                          "the embedding service did not answer within the call's budget")
        try:
            _send(s, {"op": "hello", "proto": PROTO, "material": self.material})
            h, _ = _recv(s, end)
        except BaseException:
            s.close()
            raise
        if not h.get("ok"):
            s.close()
            if h.get("error") == "embedder_version_mismatch":
                raise SemanticUnavailable("embedder_version_mismatch",
                                          "the shared service runs a different vector space; refused")
            raise SemanticUnavailable("shared_embedder_refused", str(h.get("error")))
        if h.get("material") != self.material:
            s.close()
            raise SemanticUnavailable("embedder_version_mismatch", "handshake material differs; refused")
        self.server_pid = h.get("pid")
        self._local.sock = s
        return s

    def _close(self) -> None:
        s = getattr(self._local, "sock", None)
        if s is not None:
            try:
                s.close()
            finally:
                self._local.sock = None

    # the call -----------------------------------------------------------------------------
    def _request(self, s, texts: List[str], query: bool, batch: int, lane: int,
                 call_end: Optional[float]):
        """Send one request and assemble its rows. Every frame must carry THIS request's id
        (a stale frame from an earlier request is refused: review P1-2), name in-range rows not
        seen before, and carry exactly their bytes; `done` is accepted only when every row has
        arrived (a duplicate row cannot pass as complete: P1-3). `call_end` bounds the whole call
        (urgent); without it each frame must arrive within the frame deadline (a build)."""
        import numpy as np
        dim, n = self.pin.dim, len(texts)
        out = np.zeros((n, dim), dtype=np.float32)
        covered = np.zeros(n, dtype=bool)
        rid = secrets.randbits(62)
        _send(s, {"op": "embed", "rid": rid, "texts": texts, "query": query, "batch": batch, "lane": lane})
        build_end = None if call_end is not None else time.monotonic() + max(self.startup_s, BUILD_S_PER_TEXT * n)
        while True:
            frame_end = call_end if call_end is not None else min(build_end, time.monotonic() + self.frame_deadline_s)
            h, p = _recv(s, frame_end)
            if h.get("rid") != rid:
                raise ProtocolError("frame from another request")
            if h.get("wait") is True and set(h) == {"rid", "wait"} and not p:
                # a heartbeat: the request is queued behind other work. It renews a BUILD's
                # per-frame deadline but never its total cap, and never an urgent call's budget.
                continue
            if "error" in h:
                raise SemanticUnavailable("shared_embedder_error", str(h["error"]))
            if h.get("done") is True:
                if not covered.all():
                    raise SemanticUnavailable("shared_embedder_error", "incomplete result; refused")
                return out
            idx = h.get("idx")
            if (not isinstance(idx, list) or not idx or not all(type(i) is int for i in idx)
                    or min(idx) < 0 or max(idx) >= n or len(set(idx)) != len(idx) or covered[idx].any()):
                raise ProtocolError("bad, out-of-range or duplicate rows")
            if len(p) != len(idx) * dim * 4:
                raise ProtocolError("payload size does not match its rows")
            out[idx] = np.frombuffer(p, dtype=np.float32).reshape(len(idx), dim)
            covered[idx] = True

    def embed(self, texts: List[str], query: bool, batch: int = 32, interactive: bool = False):
        """`interactive`: a document embed someone is waiting on (a query's synchronous top-up
        or build) takes the query lane and the query budget, up to INTERACTIVE_MAX texts; larger
        ones go to the build lane so they cannot crowd out other engines' queries (P2-3).
        Any failure, including a malformed reply, closes this thread's socket and raises
        SemanticUnavailable: never another exception, never a hang (review P1-1/P1-4)."""
        import numpy as np
        texts = list(texts)
        if not texts:
            return np.zeros((0, self.pin.dim), dtype=np.float32)
        urgent = query or (interactive and len(texts) <= INTERACTIVE_MAX)
        start = time.monotonic()
        call_end = start + self.frame_deadline_s if urgent else None      # one budget for the call
        for attempt in (0, 1):
            try:
                connect_end = call_end if urgent else time.monotonic() + self.startup_s
                return self._request(self._connect(connect_end), texts, query, batch,
                                     0 if urgent else 1, call_end)
            except socket.timeout:
                self._close()
                raise SemanticUnavailable(
                    "shared_embedder_timeout",
                    f"no complete reply within {self.frame_deadline_s:.1f} s; refused rather than wait") from None
            except Unsafe as ex:
                self._close()
                raise SemanticUnavailable("shared_embedder_unsafe", str(ex)) from None
            except SemanticUnavailable:
                self._close()
                raise
            except (EOFError, ConnectionError) as ex:
                self._close()
                if attempt == 1 or (call_end is not None and time.monotonic() >= call_end):
                    raise SemanticUnavailable("shared_embedder_unavailable",
                                              f"{type(ex).__name__}: {ex}") from None
            except Exception as ex:   # noqa: BLE001 -- malformed replies, OS errors: never into the caller
                self._close()
                raise SemanticUnavailable("shared_embedder_protocol", f"{type(ex).__name__}: {ex}") from None
        raise SemanticUnavailable("shared_embedder_unavailable", "unreachable")


# --- always-on: `keep` and the LaunchAgent [MECH-EMBEDD-ALWAYS-ON] ---------------------------
LAUNCHD_LABEL = "io.factlattice.lts-embedd"
# An earlier release installed this agent under another reverse-DNS prefix. A leftover is recognised by
# SHAPE, never by a spelled-out name, and is only MIGRATED when it is the plist this install would itself
# write, apart from its Label (see _legacy_agents and launchagent). A same-module agent that differs (another
# checkout or state dir) is left alone unless named with --adopt-legacy.
LEGACY_LABEL_SUFFIX = ".lts-embedd"
LEGACY_PROGRAM_MARKER = "factlattice_engine.semantic.embedd"
EXIT_RESTART = 75                           # EX_TEMPFAIL: "restart me on the new code" (launchd KeepAlive)
# Obligation [MECH-EMBEDD-ALWAYS-ON install-environment]: any file added to factlattice_engine/semantic
# that shapes vectors MUST join this tuple in the same change, or a go-live that edits it will not
# restart the always-on service.
KEEP_SOURCE_FILES = ("embedd.py", "embedder.py", "index.py", "tokenizer.py", "cards.py",
                     "suggest.py", "__init__.py")      # suggest.py: pin resolution; __init__.py: package wiring
KEEP_WATCH_S, KEEP_TAKEOVER_S = 30.0, 5.0
KEEP_CRASH_LIMIT, KEEP_CRASH_WINDOW_S = 5, 600.0
KEEP_STARTS_FILE = "embedd-keep-starts.json"
KEEP_TRIPPED_FILE = "embedd-keep-tripped.json"


class KeepConfig:
    """What `keep` serves: resolved exactly as an engine's SharedEmbedder resolves it."""

    def __init__(self, pin: ModelPin, state_root: str, model_root: Optional[str] = None, max_len: int = 512):
        self.pin, self.state_root, self.model_root, self.max_len = pin, state_root, model_root, max_len


def keep_config_from_env() -> KeepConfig:
    """Same sources as SemanticService.__init__ + _pin_from_env: LTS_SEMANTIC_PIN_FILE, else
    LTS_SEMANTIC_MODEL (default pin); LTS_ENGINE_STATE_DIR else ~/.lts-engine; model_root None
    (load_embedder then reads LTS_SEMANTIC_MODEL_DIR); max_len 512. One source, so the socket
    name (the material hash) is the one engines connect to."""
    from .suggest import _pin_from_env
    state_root = os.environ.get("LTS_ENGINE_STATE_DIR") or os.path.expanduser("~/.lts-engine")
    return KeepConfig(_pin_from_env(), state_root, None, 512)


def source_fingerprint(pkg_dir: Optional[str] = None) -> str:
    """sha256 over the vector-shaping source files, in a fixed order; a missing file is a distinct
    marker, so deleting one also changes the fingerprint."""
    d = pkg_dir or os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256()
    for name in KEEP_SOURCE_FILES:
        h.update(name.encode() + b"\0")
        try:
            with open(os.path.join(d, name), "rb") as fh:
                h.update(fh.read())
        except OSError:
            h.update(b"<missing>")
        h.update(b"\0")
    return h.hexdigest()


def _tmp_name(path: str) -> str:
    """A unique temp name (pid + random): a stale file left under a reused pid can never collide
    with an O_EXCL create and make every start uncountable."""
    return f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"


def _dist_version(name: str) -> str:
    """Installed distribution version, read FRESH from disk (importlib.metadata scans dist-info each
    call), so a pip upgrade after this process started is seen. Test seam."""
    import importlib.metadata as md
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return "<absent>"


def _interpreter_stat(path: str):
    return os.stat(path)


def _interpreter_identity() -> str:
    """realpath + mtime + size of the running interpreter: a Python change also changes the Unicode
    table (unicodedata.unidata_version), which is part of the material."""
    real = os.path.realpath(sys.executable)
    try:
        st = _interpreter_stat(real)
        return f"{real}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        return f"{real}|<unreadable>"


def watch_fingerprint(pkg_dir: Optional[str] = None) -> str:
    """What `keep` watches: everything that shapes the material but is not inside the running
    process's memory: the source files, the onnxruntime and numpy versions, the interpreter, and the
    content of LTS_SEMANTIC_PIN_FILE. Any change -> exit 75 -> launchd restarts onto the new
    material, so exactly one resident model serves the version engines compute (never two)."""
    h = hashlib.sha256(source_fingerprint(pkg_dir).encode())
    for dist in ("onnxruntime", "numpy"):
        h.update(f"|{dist}={_dist_version(dist)}".encode())
    h.update(f"|interp={_interpreter_identity()}".encode())
    pf = os.environ.get("LTS_SEMANTIC_PIN_FILE")
    if pf:
        try:
            with open(pf, "rb") as fh:
                h.update(b"|pin=" + hashlib.sha256(fh.read()).digest())
        except OSError:
            h.update(b"|pin=<unreadable>")
    return h.hexdigest()


def _read_starts(path: str, now: float, window_s: float) -> list:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as fh:
            raw = json.load(fh)
        if isinstance(raw, list):
            return [t for t in raw if type(t) in (int, float) and t <= now and now - t < window_s]
    except (OSError, ValueError):
        pass
    return []


def _record_start(path: str, now: float, window_s: float) -> int:
    """Append this start to the record, drop starts older than the window (the 'reset after quiet'),
    write 0600 atomically, and return how many starts are in the window INCLUDING this one. An
    unreadable or corrupt record counts as empty (it must never stop the service from starting)."""
    prior = _read_starts(path, now, window_s)
    recent = prior + [now]
    tmp = _tmp_name(path)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(recent, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return len(recent)


def _write_private_json(path: str, obj) -> None:
    """Atomic 0600 write that never follows a symlink at `path` (os.replace swaps the link itself)."""
    tmp = _tmp_name(path)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_tripped(run: str) -> Optional[dict]:
    """None = no marker. A marker that exists but is unreadable or malformed comes back as
    {"corrupt": True}: a stopped service must never be reported as healthy because its marker is bad."""
    try:
        fd = os.open(os.path.join(run, KEEP_TRIPPED_FILE), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        return {"corrupt": True}
    try:
        with os.fdopen(fd) as fh:
            m = json.load(fh)
        t = m["time"]
        if type(t) not in (int, float) or not (t == t) or abs(t) > 4e12:
            raise ValueError("bad time")
        time.localtime(t)
        return m
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return {"corrupt": True}


def _count_start(state_root: str, now: float, window_s: float, limit: int, log, pid: int):
    """Count this start BEFORE anything that can refuse, so a refusal is counted too (launchd
    restarts any non-zero exit every ThrottleInterval, forever). Returns (exit_code_or_None, n):
    a code means "stop now" (exit 0: the limit tripped, or the start cannot be counted at all)."""
    try:
        run = run_dir(state_root)
    except (OSError, Unsafe) as ex:                # nowhere safe to count or to leave a marker
        log(f"lts-embedd-keep {pid} cannot record its start ({type(ex).__name__}: {ex}); "
            "exiting 0 so launchd stops restarting it (engines fall back to on-demand spawning)")
        return 0, 0
    try:
        n = _record_start(os.path.join(run, KEEP_STARTS_FILE), now, window_s)
    except OSError as ex:
        log(f"lts-embedd-keep {pid} cannot record its start ({type(ex).__name__}: {ex}); "
            "exiting 0 so launchd stops restarting it (engines fall back to on-demand spawning)")
        try:                                       # best effort: let status say why always-on stopped
            _write_private_json(os.path.join(run, KEEP_TRIPPED_FILE),
                                {"time": now, "starts": 0, "window_s": window_s,
                                 "reason": f"uncountable: {type(ex).__name__}: {ex}"})
        except OSError:
            pass
        return 0, 0
    if n >= limit:
        log(f"lts-embedd-keep {pid} crash-loop limit: {n} starts within {window_s:.0f} s; "
            "exiting 0 so launchd stops restarting it (engines fall back to on-demand spawning)")
        try:
            _write_private_json(os.path.join(run, KEEP_TRIPPED_FILE),
                                {"time": now, "starts": n, "window_s": window_s,
                                 "reason": f"crash-loop limit: {n} starts within {window_s:.0f} s"})
        except OSError as ex:
            log(f"lts-embedd-keep {pid} could not write the tripped marker: {type(ex).__name__}: {ex}")
        return 0, n
    return None, n


def keep(cfg: KeepConfig, *, stop: Optional[threading.Event] = None, log=None, clock=time.time,
         fingerprint_fn=None, watch_s: float = KEEP_WATCH_S, takeover_s: float = KEEP_TAKEOVER_S,
         crash_limit: int = KEEP_CRASH_LIMIT, crash_window_s: float = KEEP_CRASH_WINDOW_S,
         install_signals: bool = False) -> int:
    """Run the SAME Server with idle exit disabled. Returns the process exit code:
    0 = stopped, or the crash-loop limit was reached (launchd must not restart); 75 = the source
    fingerprint changed and the service shut down cleanly (launchd restarts it on the new code);
    2 = refused (as `serve`). Every interval, limit and clock is injectable for tests."""
    log = log or (lambda m: print(m, flush=True))
    stop = stop or threading.Event()
    fingerprint_fn = fingerprint_fn or watch_fingerprint
    pid = os.getpid()
    code, n = _count_start(cfg.state_root, clock(), crash_window_s, crash_limit, log, pid)
    if code is not None:
        return code
    try:
        srv = Server(cfg.state_root, cfg.pin, cfg.model_root, cfg.max_len, idle_exit=False)
    except (SemanticUnavailable, Unsafe) as ex:
        log(f"lts-embedd-keep {pid} refused: {ex}")
        return 2

    def _ready():                                  # bound and serving: the tripped state is over
        try:
            os.unlink(os.path.join(srv.run, KEEP_TRIPPED_FILE))
        except OSError:
            pass
    srv.on_ready = _ready
    log(f"lts-embedd-keep {pid} start material={srv.hash} start {n} of {crash_limit} in the window")
    if install_signals:
        def _term(*_):
            stop.set()
            srv.shutdown()
        signal.signal(signal.SIGTERM, _term)
    base = fingerprint_fn()
    changed = threading.Event()
    done = threading.Event()

    def watch():
        while not done.wait(watch_s):
            if stop.is_set():
                srv.shutdown()
                return
            try:
                now_fp = fingerprint_fn()
            except Exception as ex:   # noqa: BLE001 -- a fingerprint that cannot be read must not kill the service
                log(f"lts-embedd-keep {pid} fingerprint unreadable: {type(ex).__name__}: {ex}")
                continue
            if now_fp != base:
                changed.set()
                srv.shutdown()
                return
    threading.Thread(target=watch, name="embedd-keep-watch", daemon=True).start()
    try:
        polls = 0
        while not srv.acquire_pidfile():          # an on-demand service of this material is live: do not fight
            if polls == 0:
                log(f"lts-embedd-keep {pid} waiting: the pidfile is held by a live service; "
                    f"polling every {takeover_s:g} s")
            polls += 1
            if stop.wait(takeover_s):
                return 0
            if changed.is_set():
                log(f"lts-embedd-keep {pid} source changed while waiting; exit {EXIT_RESTART}")
                return EXIT_RESTART
        log(f"lts-embedd-keep {pid} serving material={srv.hash}")
        try:
            srv.serve_forever()
        except (SemanticUnavailable, Unsafe) as ex:
            log(f"lts-embedd-keep {pid} refused: {ex}")
            return 2
        if changed.is_set():
            log(f"lts-embedd-keep {pid} source changed; clean shutdown done, exit {EXIT_RESTART}")
            return EXIT_RESTART
        log(f"lts-embedd-keep {pid} exit")
        return 0
    finally:
        done.set()
        srv._cleanup()


def _default_runner(argv):
    return subprocess.run(argv, capture_output=True, text=True)


SMOKE = "import onnxruntime, numpy, factlattice_engine.semantic.embedd"


def _default_probe(python: str, env: dict):
    """Run the interpreter the way launchd will (only the plist's env): can it import the model
    stack and this package? Returns (ok, reason)."""
    try:
        r = subprocess.run([python, "-c", SMOKE], env=dict(env), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return False, f"{type(ex).__name__}: {ex}"
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [f"exit {r.returncode}"]
        return False, tail[0]
    return True, ""


def build_plist(*, python: str, env: dict, log_path: str) -> dict:
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [python, "-m", "factlattice_engine.semantic.embedd", "keep"],
        "EnvironmentVariables": dict(env),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }


DEFAULT_MCP_CONFIG = os.path.expanduser("~/.mcp.json")
MCP_ENV_KEYS = ("PYTHONPATH", "LTS_SEMANTIC_MODEL", "LTS_SEMANTIC_PIN_FILE", "LTS_ENGINE_STATE_DIR",
                "LTS_SEMANTIC_MODEL_DIR")


class InstallRefused(Exception):
    """The LaunchAgent cannot be built to match the engines; nothing was written."""


def _mcp_install_env(mcp_config: str, path: Optional[str], pythonpath: Optional[str]):
    """(interpreter, EnvironmentVariables) from the lts-engine MCP server entry [install-environment].
    The interpreter is that entry's `command` resolved by shutil.which against `path` (default: the
    PATH of this process). Raises InstallRefused with a reason."""
    import shutil
    try:
        with open(mcp_config) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as ex:
        raise InstallRefused(f"cannot read the MCP config {mcp_config}: {type(ex).__name__}: {ex}") from None
    entry = (cfg.get("mcpServers") or {}).get("lts-engine") if isinstance(cfg, dict) else None
    if not isinstance(entry, dict):
        raise InstallRefused(f"the MCP config {mcp_config} has no mcpServers.lts-engine entry")
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        raise InstallRefused(f"the lts-engine entry in {mcp_config} has no `command`")
    python = shutil.which(command, path=path)
    if not python:
        raise InstallRefused(f"cannot resolve the lts-engine command {command!r} to an absolute path "
                             "against this process's PATH")
    python = os.path.abspath(python)
    src = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    env = {k: src[k] for k in MCP_ENV_KEYS if isinstance(src.get(k), str) and src[k]}
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    if "PYTHONPATH" not in env:
        raise InstallRefused(f"the lts-engine entry in {mcp_config} sets no PYTHONPATH")
    return python, env


def _legacy_agents(plist_path: str):
    """(candidates, unclassifiable) among `*.lts-embedd.plist` files next to `plist_path`. Read-only.
    candidates: [(label, file, plist dict)] named `<label>.plist`, Label ending LEGACY_LABEL_SUFFIX and not
    the current label, ProgramArguments containing LEGACY_PROGRAM_MARKER. unclassifiable: [(file, reason)]
    for a symlink, a non-regular file (a FIFO would block a plain open), an unreadable or unparseable file.
    The file is opened O_NOFOLLOW|O_NONBLOCK and fstat'ed before any read."""
    import glob
    import plistlib
    import stat
    cands, bad = [], []
    for f in sorted(glob.glob(os.path.join(os.path.dirname(plist_path), "*" + LEGACY_LABEL_SUFFIX + ".plist"))):
        if os.path.abspath(f) == os.path.abspath(plist_path):
            continue
        try:
            if stat.S_ISLNK(os.lstat(f).st_mode):
                bad.append((f, "a symlink"))
                continue
            fd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as ex:
            bad.append((f, f"unreadable ({type(ex).__name__})"))
            continue
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                bad.append((f, "not a regular file"))
                continue
            with os.fdopen(fd, "rb") as fh:
                fd = -1
                d = plistlib.load(fh)
        except (OSError, ValueError, plistlib.InvalidFileException) as ex:
            bad.append((f, f"unreadable or not a plist ({type(ex).__name__})"))
            continue
        finally:
            if fd >= 0:
                os.close(fd)
        label = d.get("Label") if isinstance(d, dict) else None
        args = d.get("ProgramArguments") if isinstance(d, dict) else None
        if (isinstance(label, str) and label != LAUNCHD_LABEL and label.endswith(LEGACY_LABEL_SUFFIX)
                and os.path.basename(f) == label + ".plist"
                and isinstance(args, list) and LEGACY_PROGRAM_MARKER in args):
            cands.append((label, f, d))
    return cands, bad


def _same_but_label(a: dict, b: dict) -> bool:
    return ({k: v for k, v in a.items() if k != "Label"} == {k: v for k, v in b.items() if k != "Label"})


def launchagent(action: str, *, runner=None, plist_path: Optional[str] = None, state_root: Optional[str] = None,
                pythonpath: Optional[str] = None, uid: Optional[int] = None, pin: Optional[ModelPin] = None,
                out=None, mcp_config: Optional[str] = None, path: Optional[str] = None, probe=None,
                now: Optional[float] = None, adopt_legacy: Optional[List[str]] = None) -> int:
    """install | uninstall | status. launchctl is reached ONLY through `runner(argv)` (an object
    with returncode/stdout/stderr), so tests never touch the real launchd. Returns an exit code.
    install/status read the engines' environment from the lts-engine entry of `mcp_config`
    (default ~/.mcp.json); `pythonpath`, when given, overrides that entry's PYTHONPATH."""
    import plistlib
    runner = runner or _default_runner
    probe = probe or _default_probe
    out = out or sys.stdout
    mcp_config = mcp_config or DEFAULT_MCP_CONFIG
    plist_path = plist_path or os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")
    state_root = state_root or os.environ.get("LTS_ENGINE_STATE_DIR") or os.path.expanduser("~/.lts-engine")
    uid = os.getuid() if uid is None else uid
    domain, target = f"gui/{uid}", f"gui/{uid}/{LAUNCHD_LABEL}"
    if action == "install":
        try:
            python, env = _mcp_install_env(mcp_config, path, pythonpath)
        except InstallRefused as ex:
            print(f"install REFUSED (nothing written): {ex}", file=out)
            return 1
        ok, why = probe(python, env)
        if not ok:
            print(f"install REFUSED (nothing written): the interpreter {python} cannot import the model "
                  f"stack with the plist's environment: {why}", file=out)
            return 1
        # the log lives in the run dir of the state root the SERVICE will use, created 0600
        log_path = os.path.join(run_dir(env.get("LTS_ENGINE_STATE_DIR") or state_root), "embedd-keep.log")
        cands, bad = _legacy_agents(plist_path)
        if bad:
            for f, why in bad:
                print(f"install REFUSED (no agent or plist written): {f} looks like an earlier-release agent but is {why}; "
                      "cannot tell whether it is loaded. Remove or fix it, then retry.", file=out)
            return 1
        want = build_plist(python=python, env=env, log_path=log_path)
        adopt = set(adopt_legacy or ())
        legacy = [(l, f) for l, f, d in cands if _same_but_label(d, want) or l in adopt]
        for l, f, d in cands:
            if (l, f) not in legacy:
                print(f"left alone: {l} ({f}) runs this module but differs from this install; "
                      f"pass --adopt-legacy {l} to migrate it", file=out)
        stopped = []                                # old agents we actually stopped (to put back on failure)

        def restore_old():
            todo = list(stopped)
            stopped.clear()                         # each stopped agent is restored at most once
            for label, f in todo:
                rr = runner(["launchctl", "bootstrap", domain, f])
                if rr.returncode == 0:
                    print(f"restored the earlier agent {label}", file=out)
                else:
                    print(f"COULD NOT restore the earlier agent {label}: launchctl bootstrap exited "
                          f"{rr.returncode}: {(rr.stderr or rr.stdout or '').strip()}; its plist is {f}", file=out)

        try:
            with open(plist_path, "rb") as fh:
                previous = fh.read()
        except OSError:
            previous = None

        def rollback_new():
            if previous is None:
                try:
                    os.unlink(plist_path)
                except FileNotFoundError:
                    pass
            else:                                   # put the previous plist back, byte for byte, 0600
                tmp2 = _tmp_name(plist_path)
                fd2 = os.open(tmp2, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd2, "wb") as fh:
                    fh.write(previous)
                os.replace(tmp2, plist_path)
        # From the FIRST bootout to the new bootstrap, any exception puts the stopped old agents back.
        try:
            for label, f in legacy:                 # stop the old agent first: never two services at once
                r = runner(["launchctl", "bootout", f"{domain}/{label}"])
                if r.returncode == 0:
                    stopped.append((label, f))
                elif runner(["launchctl", "print", f"{domain}/{label}"]).returncode == 0:
                    print(f"install REFUSED: the earlier agent {label} is still loaded after launchctl bootout "
                          f"exited {r.returncode}: {(r.stderr or r.stdout or '').strip()}; no new agent was "
                          "installed", file=out)
                    restore_old()
                    return 1
                # else: it was not loaded; nothing to stop and nothing to restore
            os.close(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600))
            os.makedirs(os.path.dirname(plist_path), exist_ok=True)
            tmp = _tmp_name(plist_path)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as fh:
                plistlib.dump(want, fh)
            os.replace(tmp, plist_path)
            r = runner(["launchctl", "bootstrap", domain, plist_path])
        except BaseException:                       # any failure in the window: the old agents come back
            try:
                rollback_new()
            except Exception:
                pass
            restore_old()
            raise
        if r.returncode != 0:
            rollback_new()
            restore_old()
            print(f"install FAILED: launchctl bootstrap exited {r.returncode}: "
                  f"{(r.stderr or r.stdout or '').strip()}", file=out)
            return 1
        rc = 0
        for label, f in legacy:                     # the new agent is up: drop the old one for good
            try:
                os.unlink(f)
                print(f"migrated: removed the earlier agent {label}", file=out)
            except OSError as ex:
                rc = 1
                print(f"COULD NOT remove the earlier agent's plist {f} ({type(ex).__name__}: {ex}); it is "
                      f"booted out but would load again at next login beside {LAUNCHD_LABEL}: remove it by hand",
                      file=out)
        print(f"installed {LAUNCHD_LABEL}: {plist_path}", file=out)
        return rc
    if action == "uninstall":
        r = runner(["launchctl", "bootout", target])
        if r.returncode != 0:
            print(f"not loaded (launchctl bootout exited {r.returncode}: "
                  f"{(r.stderr or r.stdout or '').strip()})", file=out)
        try:
            os.unlink(plist_path)
        except FileNotFoundError:
            pass
        print(f"uninstalled {LAUNCHD_LABEL}", file=out)
        return 0
    if action == "status":
        if pin is None:
            pin = keep_config_from_env().pin
        r = runner(["launchctl", "print", target])
        loaded = r.returncode == 0
        print(f"loaded: {'yes' if loaded else 'no'}", file=out)
        cands, bad = _legacy_agents(plist_path)
        for f, why in bad:
            print(f"legacy-shaped file {f}: cannot classify, it is {why}; install will refuse until it is "
                  "removed or fixed", file=out)
        try:
            spy, senv = _mcp_install_env(mcp_config, path, pythonpath)
            swant = build_plist(python=spy, env=senv, log_path=os.path.join(
                run_dir(senv.get("LTS_ENGINE_STATE_DIR") or state_root), "embedd-keep.log"))
        except (InstallRefused, OSError, Unsafe):
            swant = None
        for label, f, d in cands:
            lr = runner(["launchctl", "print", f"{domain}/{label}"])
            same = swant is not None and _same_but_label(d, swant)
            print(f"legacy label {label}: installed ({f}), loaded: {'yes' if lr.returncode == 0 else 'no'}; "
                  + ("run `launchagent install` to migrate" if same else
                     f"differs from this install (left alone; `launchagent install --adopt-legacy {label}` "
                     "migrates it)"), file=out)
        if loaded:
            import re
            m = re.search(r"\bpid = (\d+)", r.stdout or "")
            print(f"pid: {m.group(1) if m else 'none'}", file=out)
            if not m:
                print("loaded but not running", file=out)
        print(f"material: {material_hash(material(pin, 512))}", file=out)
        try:
            with open(plist_path, "rb") as fh:
                installed = plistlib.load(fh)
        except OSError:
            print("plist: absent", file=out)
            return 0
        try:
            python, env = _mcp_install_env(mcp_config, path, pythonpath)
        except InstallRefused as ex:
            print(f"drift: cannot compare with the MCP config: {ex}", file=out)
            return 0
        drift = []
        have_py = (installed.get("ProgramArguments") or [None])[0]
        if have_py != python:
            drift.append(f"interpreter {have_py} != {python}")
        have_env = installed.get("EnvironmentVariables") or {}
        for k in MCP_ENV_KEYS:
            if have_env.get(k) != env.get(k):
                drift.append(f"{k}: installed {have_env.get(k)!r}, MCP config {env.get(k)!r}")
        if have_py:
            ok, why = probe(have_py, have_env)
            if not ok:
                drift.append(f"interpreter cannot import the model stack ({why})")
        print("drift: " + "; ".join(drift) if drift else "in sync", file=out)
        try:
            srun = run_dir(have_env.get("LTS_ENGINE_STATE_DIR") or state_root)
        except (OSError, Unsafe) as ex:
            print(f"run dir unusable: {type(ex).__name__}: {ex}", file=out)
            return 0
        starts = _read_starts(os.path.join(srun, KEEP_STARTS_FILE), time.time() if now is None else now,
                              KEEP_CRASH_WINDOW_S)
        mk = _read_tripped(srun)
        if mk is not None and mk.get("corrupt"):
            print("crash-loop: tripped (marker unreadable)", file=out)
        elif mk is not None:                 # durable: shown until the next successful start clears it
            try:
                when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mk["time"]))
                line = (f"crash-loop: tripped at {when}: {mk.get('starts', '?')} starts in "
                        f"{float(mk.get('window_s', KEEP_CRASH_WINDOW_S)):.0f} s; engines use on-demand "
                        f"({mk.get('reason', 'no reason recorded')})")
            except (ValueError, TypeError, OverflowError, OSError):
                line = "crash-loop: tripped (marker unreadable)"
            print(line, file=out)
        elif len(starts) >= KEEP_CRASH_LIMIT:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(max(starts)))
            print(f"crash-loop: tripped at {when}: {len(starts)} starts in {KEEP_CRASH_WINDOW_S:.0f} s; "
                  "engines use on-demand", file=out)
        return 0
    raise ValueError(f"unknown launchagent action {action!r}")


def _env_interval(name: str, default: float) -> float:
    """A positive, finite number of seconds from the environment, else ValueError naming the variable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = float(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number") from None
    if not (v > 0) or v == float("inf"):
        raise ValueError(f"{name}={raw!r} must be a positive finite number of seconds")
    return v


def _main(argv: List[str], runner=None, probe=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="lts-embedd")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--state-root", required=True)
    sv.add_argument("--pin", required=True)
    sv.add_argument("--max-len", type=int, default=512)
    sv.add_argument("--model-root")
    sub.add_parser("keep")                      # always-on: env-resolved, no idle exit [MECH-EMBEDD-ALWAYS-ON]
    la = sub.add_parser("launchagent")
    la.add_argument("action", choices=["install", "uninstall", "status"])
    la.add_argument("--plist-path")
    la.add_argument("--adopt-legacy", action="append", default=[])   # migrate a differing earlier-release label
    la.add_argument("--state-root")
    la.add_argument("--pythonpath")
    la.add_argument("--mcp-config")             # default ~/.mcp.json
    la.add_argument("--path")                   # PATH used to resolve the MCP `command` (default: process PATH)
    a = ap.parse_args(argv)
    if a.cmd == "launchagent":
        return launchagent(a.action, runner=runner, plist_path=a.plist_path, state_root=a.state_root,
                           pythonpath=a.pythonpath, mcp_config=a.mcp_config, path=a.path, probe=probe,
                           adopt_legacy=a.adopt_legacy)
    if a.cmd == "keep":
        try:
            cfg = keep_config_from_env()
            watch_s = _env_interval("LTS_EMBEDD_KEEP_WATCH_S", KEEP_WATCH_S)
            takeover_s = _env_interval("LTS_EMBEDD_KEEP_TAKEOVER_S", KEEP_TAKEOVER_S)
        except (KeyError, OSError, ValueError, TypeError) as ex:
            # refused, and COUNTED: an uncounted non-zero exit is restarted by launchd forever
            print(f"lts-embedd-keep {os.getpid()} refused: cannot resolve its configuration: "
                  f"{type(ex).__name__}: {ex}", flush=True)
            code, _n = _count_start(os.environ.get("LTS_ENGINE_STATE_DIR") or os.path.expanduser("~/.lts-engine"),
                                    time.time(), KEEP_CRASH_WINDOW_S, KEEP_CRASH_LIMIT,
                                    lambda m: print(m, flush=True), os.getpid())
            return 2 if code is None else code
        return keep(cfg, install_signals=True, watch_s=watch_s, takeover_s=takeover_s)
    pin = ModelPin(**json.loads(a.pin))
    srv = Server(a.state_root, pin, a.model_root, a.max_len)
    signal.signal(signal.SIGTERM, lambda *_: srv.shutdown())
    print(f"lts-embedd {os.getpid()} start material={srv.hash}", flush=True)
    try:
        srv.serve_forever()
    except (SemanticUnavailable, Unsafe) as ex:
        print(f"lts-embedd {os.getpid()} refused: {ex}", flush=True)
        return 2
    print(f"lts-embedd {os.getpid()} exit", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
