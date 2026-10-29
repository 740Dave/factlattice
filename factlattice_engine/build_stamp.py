"""[MECH-BUILD-STAMP] — a running server must not silently serve from stale CODE.

============================================================================
THE HOLE THIS CLOSES ([RISK-STALE-CODE], the sibling of [RISK-MISSED-EVENT]).
============================================================================
Every guarantee in [SPEC:FL-FRESHNESS] — never serve stale, the self-heal that makes a
stale DATA verdict transient — assumes the process is EXECUTING THE CURRENT ENGINE CODE.
A long-running MCP stdio server does not reload: it runs the python it imported at process
start, forever. So when the engine library on disk is UPDATED under a running server — as
the [MECH-SELFHEAL] self-heal itself was, propagated to a server that had been up since
before it existed — the running process keeps executing the OLD snapshot. The fix "already
landed" on disk and the live process is simultaneously, silently, broken. A manual bounce
"fixes" it, which is the tell: the source was already correct and the LIVE PROCESS is the
stale artifact.

[RISK-MISSED-EVENT] is this exact failure one layer down (a missed FILE change), and it is
retired by a periodic re-verify [MECH-BACKSTOP] — NOT by trusting the watcher. This module
is the same move for a missed CODE change: stamp the loaded engine build, compare it to the
on-disk build periodically, and RECONCILE on divergence — never trust that "the process is
current" just because it is running.

THE STAMP. A content hash over the engine package's own `.py` source bytes (mtime-immune,
same discipline as the corpus content-hash oracle). Taken over the FILES ON DISK, it yields
the on-disk build; taken once at seat and remembered, it yields the LOADED build. Divergence
== the running process predates a code change it never imported.

RECONCILE, NOT REFUSE-FOREVER. On divergence the sentinel invokes an injectable
`reconcile` callback. The honest default is an AUTOMATIC, invisible relaunch (re-exec), so
the very bounce the old spec prescribed as a manual step happens by itself — satisfying the
[SPEC:FL-FRESHNESS] bar that staleness is never a manual operator chore. The callback is
injectable so tests can spy it and so a deployment can choose a softer strategy (refuse the
query loudly / alert) — see `reconcile_by_reexec` vs a no-op. FAIL-SAFE: a stamp error
never raises into serving; an unreadable tree returns None and the sentinel simply does not
fire (it cannot manufacture a false divergence).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import stat
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Deployment knobs (default: watch ON, reconcile by automatic relaunch).
_ENV_DISABLE = "FACTLATTICE_CODE_WATCH"          # "0"/"false" disables the sentinel
_ENV_STRATEGY = "FACTLATTICE_CODE_RECONCILE"     # "restart" (default) | "refuse" | "off"
_ENV_INTERVAL = "FACTLATTICE_CODE_WATCH_INTERVAL"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def build_stamp(package_dir: Optional[str] = None) -> Optional[str]:
    """A content hash over the engine package's `.py` source on disk, or None on error.

    Mtime-immune (hashes bytes, never mtime/size), deterministic (sorted rel paths), and
    fail-safe: any OS error yields None so the sentinel cannot fire on a false divergence.
    """
    root = package_dir or os.path.dirname(os.path.abspath(__file__))
    try:
        recs = []
        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                ap = os.path.join(dirpath, name)
                rel = os.path.relpath(ap, root).replace(os.sep, "/")
                with open(ap, "rb") as fh:
                    recs.append(rel + "\x00" +
                                hashlib.sha256(fh.read()).hexdigest())
        recs.sort()
    except OSError:
        return None
    return "sha256:" + hashlib.sha256("\n".join(recs).encode("utf-8")).hexdigest()


# [INV-RECONCILE-SERVES] The relaunch must leave a LIVE, SERVING successor on the SAME pipe.
# A stdio MCP server is not a daemon it can afford to drop: its stdin/stdout ARE the client's
# transport. `os.execv` is the right primitive precisely because it PRESERVES those fds and
# the pid — the client never sees an EOF, so the bounce is genuinely invisible. Two things
# must hold for that to be true, and both were previously unstated and untested:
#   (1) the successor must re-enter the SAME ENTRYPOINT (see `reexec_argv`);
#   (2) the exec must happen BETWEEN requests (see `SERVING_LOCK`), or an in-flight response
#       is never written and the client hangs to its timeout.
SERVING_LOCK = threading.RLock()

# [INV-RECONCILE-SERVES] the PARTIAL-LINE CARRY. `serve()` reads stdin under SERVING_LOCK and
# processes every COMPLETE line before releasing it, so the only request bytes that can sit in
# user space across a release are an INCOMPLETE line (a client that sent half a request and
# paused). Those bytes are already off the pipe, so a re-exec would destroy them.
#
# TRANSPORT (hardened, the architect ruling after the delta review P2-A). The reconcile writes
# them to <engine state root>/carry/ (created 0700, owned by us, NEVER under TMPDIR) as
# `carry-<token>`, `<token>` = 32 random hex chars, mode 0600, O_CREAT|O_EXCL, via a dir fd.
# The successor inherits the PATH in FACTLATTICE_STDIN_CARRY and the TOKEN in
# FACTLATTICE_STDIN_CARRY_TOKEN (written by the same predecessor); never the bytes (env size
# limits: E2BIG at ~400 KB macOS). It accepts the carry only if ALL of these hold, every check
# made on an OPEN FD and never on a path:
#   * the path's directory is EXACTLY the carry root (so no `..`, relative or foreign path), and its name is exactly
#     `carry-<token>` with the token from the second env var (a value we did not write has no
#     matching token);
#   * the carry dir, opened O_DIRECTORY|O_NOFOLLOW, is a directory, ours, mode 0700;
#   * the file, opened relative to that dir fd with O_NOFOLLOW|O_NONBLOCK, is a regular file,
#     ours, mode 0600, st_nlink == 1.
# It then unlinks that exact name through the dir fd, only if it is still the same inode.
# Anything else is REFUSED with a log line of the value's length and a short hash (never the
# raw text). A failed exec removes the file and both vars (the old process keeps serving).
# serve() sweeps stale (>10 min) carry files at startup. Mutated only under SERVING_LOCK.
STDIN_CARRY_ENV = "FACTLATTICE_STDIN_CARRY"
STDIN_CARRY_TOKEN_ENV = "FACTLATTICE_STDIN_CARRY_TOKEN"
_CARRY_SUBDIR = "carry"
_CARRY_PREFIX = "carry-"
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
STALE_CARRY_SECONDS = 600
_stdin_carry = b""
_after_carry_open_hook: Optional[Callable[[], None]] = None   # test seam (TOCTOU probe)


def set_stdin_carry(data: bytes) -> None:
    global _stdin_carry
    _stdin_carry = bytes(data)


def _uid() -> int:
    return os.getuid()


def carry_root() -> str:
    from .querylog import state_dir
    return os.path.join(str(state_dir()), _CARRY_SUBDIR)


_O_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) \
    | getattr(os, "O_CLOEXEC", 0)


def _check_dir_fd(dfd: int) -> None:
    st = os.fstat(dfd)
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError("carry dir is not a directory")
    if st.st_uid != _uid():
        raise ValueError("carry dir owned by another user")
    if (st.st_mode & 0o777) != 0o700:
        raise ValueError(f"carry dir mode {st.st_mode & 0o777:o} is not 0700")


def _open_carry_dir(create: bool) -> int:
    root = carry_root()
    if create:
        os.makedirs(os.path.dirname(root), exist_ok=True)
        try:
            os.mkdir(root, 0o700)
        except FileExistsError:
            pass
    dfd = os.open(root, _O_DIR)                       # O_NOFOLLOW: a symlinked dir fails here
    try:
        if create:
            st = os.fstat(dfd)
            if stat.S_ISDIR(st.st_mode) and st.st_uid == _uid() and (st.st_mode & 0o777) != 0o700:
                os.fchmod(dfd, 0o700)                 # we own it: tighten rather than refuse
        _check_dir_fd(dfd)
    except BaseException:
        os.close(dfd)
        raise
    return dfd


def _write_carry_file(data: bytes):
    """Create the private carry file; return (path, token). Raises on failure (caller cleans)."""
    token = secrets.token_hex(16)
    name = _CARRY_PREFIX + token
    dfd = _open_carry_dir(create=True)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=dfd)
        try:
            os.fchmod(fd, 0o600)                      # defeat any umask quirk
            with os.fdopen(fd, "wb", closefd=False) as fh:
                fh.write(data)
        except BaseException:
            try:
                os.unlink(name, dir_fd=dfd)
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    return os.path.join(carry_root(), name), token


def _remove_carry_name(name: str) -> None:
    try:
        dfd = _open_carry_dir(create=False)
    except (OSError, ValueError):
        return
    try:
        try:
            os.unlink(name, dir_fd=dfd)
        except OSError:
            pass
    finally:
        os.close(dfd)


def _digest(raw: str) -> str:
    return f"len={len(raw)} sha256={hashlib.sha256(raw.encode('utf-8', 'replace')).hexdigest()[:12]}"


def take_inherited_stdin_carry() -> bytes:
    """Successor side: consume (and clear) the carry the previous image left us.

    Every guard is documented at the top of this section. Anything that fails is REFUSED
    with a warning naming the value only by length and hash, and is never followed.
    """
    raw = os.environ.pop(STDIN_CARRY_ENV, "")
    token = os.environ.pop(STDIN_CARRY_TOKEN_ENV, "")
    data = b""
    if raw:
        dfd = fd = -1
        try:
            root = carry_root()
            if os.path.dirname(raw) != root:        # exact: also rejects `..` and relative forms
                raise ValueError("path is not directly inside the carry root")
            if not _TOKEN_RE.match(token) or os.path.basename(raw) != _CARRY_PREFIX + token:
                raise ValueError("token missing or does not match the carry name")
            dfd = _open_carry_dir(create=False)
            name = os.path.basename(raw)
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
                         dir_fd=dfd)
            if _after_carry_open_hook is not None:
                _after_carry_open_hook()
            st = os.fstat(fd)                          # validate the OPEN file, not the path
            if not stat.S_ISREG(st.st_mode):
                raise ValueError("not a regular file")
            if st.st_uid != _uid():
                raise ValueError("file owned by another user")
            if (st.st_mode & 0o777) != 0o600:
                raise ValueError(f"file mode {st.st_mode & 0o777:o} is not 0600")
            if st.st_nlink != 1:
                raise ValueError(f"file has {st.st_nlink} links")
            with os.fdopen(fd, "rb", closefd=False) as fh:
                data = fh.read()
            try:                                       # unlink only the SAME inode, by name
                now = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                if (now.st_dev, now.st_ino) == (st.st_dev, st.st_ino):
                    os.unlink(name, dir_fd=dfd)
            except OSError:
                pass
        except (OSError, ValueError) as exc:
            logger.warning("[lts-engine] refusing inherited %s (%s): %s; ignoring it",
                           STDIN_CARRY_ENV, _digest(raw), exc)
            data = b""
        finally:
            if fd >= 0:
                os.close(fd)
            if dfd >= 0:
                os.close(dfd)
    elif token:
        logger.warning("[lts-engine] refusing inherited %s without %s (%s); ignoring it",
                       STDIN_CARRY_TOKEN_ENV, STDIN_CARRY_ENV, _digest(token))
    set_stdin_carry(data)
    return data


def sweep_stale_carry(max_age_s: float = STALE_CARRY_SECONDS) -> int:
    """Remove carry files older than `max_age_s` left by a crash between write and exec (or a
    successor that died before reading them). Only REGULAR files we own whose name is
    `carry-*`, only via the validated dir fd, only the exact name. Returns the count."""
    try:
        dfd = _open_carry_dir(create=False)
    except (OSError, ValueError):
        return 0                                       # no dir / not ours / symlink: do nothing
    removed = 0
    try:
        cutoff = time.time() - max_age_s
        for name in os.listdir(dfd):
            if not name.startswith(_CARRY_PREFIX):
                continue
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                if (stat.S_ISREG(st.st_mode) and st.st_uid == _uid() and st.st_mtime < cutoff):
                    os.unlink(name, dir_fd=dfd)
                    removed += 1
            except OSError:
                continue
    finally:
        os.close(dfd)
    if removed:
        logger.info("[lts-engine] swept %d stale stdin-carry file(s)", removed)
    return removed


def reexec_argv() -> list:
    """The argv that re-enters THIS process's entrypoint.

    `sys.argv[0]` is the module's FILE PATH even when the process was launched as
    `python -m pkg.mod` — so the naive `[sys.executable] + sys.argv` re-runs that file as a
    TOP-LEVEL SCRIPT, where `__package__` is empty and the module's own
    `from . import config` raises ImportError. The process then exits 1 and the MCP client
    sees stdout EOF: "connection closed (cleanly)". Python records the true entrypoint on
    `__main__.__spec__`; a `-m` launch must be re-entered as a `-m` launch.
    """
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    module = getattr(spec, "name", None)
    if module:
        return [sys.executable, "-m", module] + sys.argv[1:]
    return [sys.executable] + sys.argv


def reconcile_by_reexec() -> None:
    """The default reconcile: re-exec this process so it reloads the current code.

    `os.execv` replaces the process image IN PLACE — same pid, same inherited stdio fds — so
    a stdio MCP peer's transport survives the reload and the client is never disconnected.
    Waits for any in-flight request to finish first. Kept behind the sentinel's env strategy
    so a deployment can opt into a softer response.
    """
    logger.warning("[lts-engine] stale ENGINE CODE detected on a running server "
                   "([RISK-STALE-CODE]); re-execing to reload the current build")
    argv = reexec_argv()
    with SERVING_LOCK:
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001 — a flush fault must not strand a stale process
            pass
        carry_name = None
        try:
            if _stdin_carry:
                path, token = _write_carry_file(_stdin_carry)
                carry_name = os.path.basename(path)
                os.environ[STDIN_CARRY_ENV] = path
                os.environ[STDIN_CARRY_TOKEN_ENV] = token
            else:
                os.environ.pop(STDIN_CARRY_ENV, None)
                os.environ.pop(STDIN_CARRY_TOKEN_ENV, None)
            os.execv(argv[0], argv)
        except BaseException:
            # exec failed (E2BIG, ENOMEM, ...): the old process keeps serving, so leave NO
            # trace — no vars to poison later subprocesses, no file holding request bytes.
            os.environ.pop(STDIN_CARRY_ENV, None)
            os.environ.pop(STDIN_CARRY_TOKEN_ENV, None)
            if carry_name:
                _remove_carry_name(carry_name)
            raise


class CodeCurrencySentinel:
    """Detects that the running server's LOADED engine build != the on-disk build and
    reconciles. Symmetric to the freshness kernel's [MECH-BACKSTOP], but for CODE.

    `check_once()` is the pure, testable unit: it returns True and calls `reconcile` iff the
    on-disk build diverges from the build seated at construction. `start()` runs it on a
    daemon poll loop (the deployment path). A None on-disk stamp (unreadable tree) is treated
    as "cannot prove divergence" and never fires.
    """

    def __init__(self, *, package_dir: Optional[str] = None,
                 reconcile: Optional[Callable[[], None]] = None,
                 interval: Optional[float] = None,
                 seated_stamp: Optional[str] = None) -> None:
        self._package_dir = package_dir or os.path.dirname(os.path.abspath(__file__))
        self.seated_stamp = seated_stamp or build_stamp(self._package_dir)
        self.reconcile = reconcile if reconcile is not None else _default_reconcile()
        try:
            self.interval = float(os.environ.get(_ENV_INTERVAL, "5") or 5.0)
        except ValueError:
            self.interval = 5.0
        self.divergences = 0                       # observable: times a divergence fired
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def current_on_disk_stamp(self) -> Optional[str]:
        return build_stamp(self._package_dir)

    def check_once(self) -> bool:
        """True (and `reconcile` invoked) iff the on-disk build diverges from the seated one."""
        live = self.current_on_disk_stamp()
        if live is None or self.seated_stamp is None:
            return False                            # unprovable -> fail-safe, never fire
        if live == self.seated_stamp:
            return False
        self.divergences += 1
        try:
            self.reconcile()
        except Exception:  # noqa: BLE001 — a reconcile fault must not kill the server
            logger.exception("[lts-engine] code-currency reconcile hook failed")
        return True

    def start(self) -> None:
        """Run the sentinel on a daemon poll loop. Idempotent. Disabled by env off-switch."""
        if self._thread is not None or not _env_flag(_ENV_DISABLE, True):
            return
        self._stop.clear()

        def _run() -> None:
            while not self._stop.wait(self.interval):
                try:
                    if self.check_once():
                        return              # reconcile (restart) fired; stop polling
                except Exception:  # noqa: BLE001
                    pass

        self._thread = threading.Thread(target=_run, name="lts-code-currency", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=self.interval * 2)
        self._thread = None


def _default_reconcile() -> Callable[[], None]:
    """Pick the reconcile strategy from the env (default: automatic re-exec)."""
    strategy = (os.environ.get(_ENV_STRATEGY) or "restart").strip().lower()
    if strategy in ("off", "none", "noop"):
        return lambda: None
    if strategy == "refuse":
        # Loud, but leaves the process up — a deployment that would rather alert than bounce.
        return lambda: logger.error(
            "[lts-engine] stale ENGINE CODE on a running server ([RISK-STALE-CODE]); "
            "relaunch required — this process will keep serving its start-time snapshot")
    return reconcile_by_reexec


__all__ = ["build_stamp", "reexec_argv", "reconcile_by_reexec",
           "CodeCurrencySentinel", "SERVING_LOCK"]
