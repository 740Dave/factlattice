"""[MECH-BUILD-STAMP] — a running server must not silently serve from stale CODE.

============================================================================
THE HOLE THIS CLOSES ([RISK-STALE-CODE], the sibling of [RISK-MISSED-EVENT]).
============================================================================
Every guarantee in [SPEC:FL-FRESHNESS] — never serve stale, the self-heal that makes a
stale DATA verdict transient — assumes the process is EXECUTING THE CURRENT ENGINE AND CORE-PACKAGE CODE.
A long-running MCP stdio server does not reload: it runs the python it imported at process
start, forever. So when the engine or core library on disk is UPDATED under a running server — as
the [MECH-SELFHEAL] self-heal itself was, propagated to a server that had been up since
before it existed — the running process keeps executing the OLD snapshot. The fix "already
landed" on disk and the live process is simultaneously, silently, broken. A manual bounce
"fixes" it, which is the tell: the source was already correct and the LIVE PROCESS is the
stale artifact.

[RISK-MISSED-EVENT] is this exact failure one layer down (a missed FILE change), and it is
retired by a periodic re-verify [MECH-BACKSTOP] — NOT by trusting the watcher. This module
is the same move for a missed CODE change: stamp the loaded build (engine and core package), compare it to the
on-disk build periodically, and RECONCILE on divergence — never trust that "the process is
current" just because it is running.

THE STAMP. A content hash over the `.py` source bytes of BOTH packages the server runs: the
engine package (`factlattice_engine`) and the core package (`factlattice`: the parser, the
authoring writers and so on), each file named in the hash by its package-prefixed path
(mtime-immune, same discipline as the corpus content-hash oracle). A stamp over the engine
package alone would let a change that touches only the core package go unseen: no server would
ever re-exec. Taken over the FILES ON DISK, it yields
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
import importlib
import logging
import os
import re
import secrets
import stat
import sys
import threading
import time
from collections.abc import Callable

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


# The two packages a server runs, and the prefix each one's files carry in the stamp so a path
# can never be read as belonging to the other root.
ENGINE_PACKAGE = "factlattice_engine"
CORE_PACKAGE = "factlattice"
# Every top-level package the server imports must be named here (SDLC.md, BUILD-STAMP-ROOTS): a
# change that touches only an unlisted package never moves the stamp, so no server relaunches.
# This tuple IS the list of roots the stamp walks (`_stamp_or_error`), in this order.
STAMP_PACKAGES = (ENGINE_PACKAGE, CORE_PACKAGE)


def engine_package_dir() -> str:
    """The directory this module sits in: the engine package as this process imported it."""
    return os.path.dirname(os.path.abspath(__file__))


def package_dir_of(name: str) -> str:
    """The directory of top-level package `name` as THIS process imports it (never a fixed path)."""
    if name == ENGINE_PACKAGE:
        return engine_package_dir()
    return os.path.dirname(os.path.abspath(importlib.import_module(name).__file__))


def core_package_dir() -> str:
    """The core package's directory as THIS process imports it (never a fixed path)."""
    return package_dir_of(CORE_PACKAGE)


def _walk_error(exc: OSError) -> None:
    raise exc                                       # os.walk swallows these by default


def _source_records(root: str, prefix: str) -> list[str]:
    """One record per `.py` file under `root`: `<prefix>/<rel path>\\x00<sha256 of its bytes>`.
    Raises OSError on any unreadable directory or file (the caller turns that into None).
    Skipped, because neither is source: an editor lock file (a name starting `.#`, which is a
    dangling link to a pid) and a dangling symlink."""
    recs = []
    for dirpath, _dirs, files in os.walk(root, onerror=_walk_error):
        for name in sorted(files):
            if not name.endswith(".py") or name.startswith(".#"):
                continue
            ap = os.path.join(dirpath, name)
            if os.path.islink(ap) and not os.path.exists(ap):
                continue
            rel = os.path.relpath(ap, root).replace(os.sep, "/")
            with open(ap, "rb") as fh:
                recs.append(prefix + "/" + rel + "\x00" +
                            hashlib.sha256(fh.read()).hexdigest())
    return recs


def _fold(recs: list[str]) -> str:
    return "sha256:" + hashlib.sha256("\n".join(sorted(recs)).encode("utf-8")).hexdigest()


def package_stamp(root: str, prefix: str) -> str | None:
    """The same hash over ONE package root (`prefix` names it in the hash), or None on error."""
    try:
        return _fold(_source_records(root, prefix))
    except (OSError, ValueError):                   # ValueError: an unencodable file name
        return None


def build_stamp(package_dir: str | None = None, core_dir: str | None = None) -> str | None:
    """A content hash over the `.py` source on disk of the engine package AND the core package,
    or None on error. `package_dir` / `core_dir` default to the directories this process
    imports them from.

    Mtime-immune (hashes bytes, never mtime/size), deterministic (sorted, package-prefixed
    rel paths), and fail-safe: any OS error (or a core package that cannot be located) yields
    None so the sentinel cannot fire on a false divergence.
    """
    return _stamp_or_error(package_dir, core_dir)[0]


def _stamp_or_error(package_dir: str | None, core_dir: str | None) -> tuple[str | None, str | None]:
    """(stamp, None) or (None, the error CLASS NAME). The name only: a message can carry a path."""
    try:
        given = {ENGINE_PACKAGE: package_dir, CORE_PACKAGE: core_dir}
        roots = [(name, given.get(name) or package_dir_of(name)) for name in STAMP_PACKAGES]
        recs: list[str] = []
        for prefix, root in roots:
            recs.extend(_source_records(root, prefix))
        return _fold(recs), None
    except (OSError, ImportError, TypeError, ValueError) as exc:    # ValueError: an unencodable file name
        return None, type(exc).__name__


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
_after_carry_open_hook: Callable[[], None] | None = None   # test seam (TOCTOU probe)


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
    logger.warning("[lts-engine] stale ENGINE or CORE-PACKAGE CODE detected on a running server "
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
    """Detects that the running server's LOADED build (engine + core package) != the on-disk build and
    reconciles. Symmetric to the freshness kernel's [MECH-BACKSTOP], but for CODE.

    `check_once()` is the pure, testable unit: it returns True and calls `reconcile` iff the
    on-disk build diverges from the build seated at construction. `start()` runs it on a
    daemon poll loop (the deployment path). A None on-disk stamp (unreadable tree) is treated
    as "cannot prove divergence" and never fires.
    """

    def __init__(self, *, package_dir: str | None = None,
                 core_dir: str | None = None,
                 reconcile: Callable[[], None] | None = None,
                 interval: float | None = None,
                 seated_stamp: str | None = None) -> None:
        self._package_dir = package_dir or os.path.dirname(os.path.abspath(__file__))
        self._core_dir = core_dir            # None: resolved from the import at each stamp
        self._blind = False                  # the last read gave None (logged once per change of state)
        self.seated_stamp = seated_stamp
        if self.seated_stamp is None:
            self.seated_stamp, err = _stamp_or_error(self._package_dir, self._core_dir)
            if self.seated_stamp is None:
                self._blind = True
                logger.warning(
                    "[lts-engine] code-currency sentinel cannot stamp the build at start (%s); it cannot see "
                    "a code change until a stamp reads, and will then adopt that read as its baseline", err)
        self.reconcile = reconcile if reconcile is not None else _default_reconcile()
        try:
            self.interval = float(os.environ.get(_ENV_INTERVAL, "5") or 5.0)
        except ValueError:
            self.interval = 5.0
        self.divergences = 0                       # observable: times a divergence fired
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def current_on_disk_stamp(self) -> str | None:
        return build_stamp(self._package_dir, self._core_dir)

    def check_once(self) -> bool:
        """True (and `reconcile` invoked) iff the on-disk build diverges from the seated one.

        A None read never fires (it cannot prove a divergence) but is never silent: the first
        None after a good read is logged (the error class only, never a path), and so is the
        first good read after it. A sentinel seated on None adopts the first good read as its
        baseline (the loaded code was never proved, so there is nothing to compare it with)."""
        live, err = _stamp_or_error(self._package_dir, self._core_dir)
        if live is None:
            if not self._blind:
                self._blind = True
                logger.warning(
                    "[lts-engine] code-currency sentinel cannot stamp the on-disk build (%s); no code change "
                    "can be detected until it reads again", err)
            return False                            # unprovable -> fail-safe, never fire
        if self.seated_stamp is None:
            self.seated_stamp, self._blind = live, False
            logger.warning("[lts-engine] code-currency sentinel can stamp the build now; adopting it as the "
                           "baseline (a change made before this read cannot be detected)")
            return False
        if self._blind:
            self._blind = False
            logger.warning("[lts-engine] code-currency sentinel can stamp the on-disk build again")
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
            "[lts-engine] stale ENGINE or CORE-PACKAGE CODE on a running server ([RISK-STALE-CODE]); "
            "relaunch required — this process will keep serving its start-time snapshot")
    return reconcile_by_reexec


__all__ = ["build_stamp", "package_stamp", "STAMP_PACKAGES", "package_dir_of", "core_package_dir", "engine_package_dir",
           "ENGINE_PACKAGE", "CORE_PACKAGE", "reexec_argv", "reconcile_by_reexec",
           "CodeCurrencySentinel", "SERVING_LOCK"]
