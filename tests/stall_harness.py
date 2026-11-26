"""Stall harness: a MODEL of a hung disk under one named root. [ACC-BR] x-instrument.

WHAT IT IS. A way to make disk calls the engine makes under ONE root block until the test releases them, while calls
under any other path run at full speed. Importable by another product's suite (stdlib only; it imports nothing from this
repo's tests package or from the engine).

WHAT IT IS NOT. A hung drive. It blocks the Python-level calls listed below. A real hung mount blocks in the kernel,
below Python, and may block calls this harness does not wrap. Every result that uses it is a result about this model, and
no real hung drive was run (the spec's x-falsifies: a real stall the harness did not model sends the design back).

FOR ANOTHER PRODUCT'S SUITE (spec 38 x-render; the consumers of `config.build_store`). The module imports the standard library only, so it needs neither the
engine nor this repo's `tests` package on the path of the suite that uses it:

    sys.path.insert(0, "<engine checkout>/tests")      # or copy this one file; nothing else is needed for the in-process form
    from stall_harness import RootStall, run_limited, Workspace
    with RootStall(corpus_root, cap_s=60) as stall:    # every stat/open/listdir/... under corpus_root now blocks until stall.release()
        out = run_limited(lambda: your_code_under_test(), 8.0)      # never hangs your suite: out.done False means it was still blocked
        assert out.done and <your failure assertion>
        stall.release()                                # in `finally`, always

The engine under test must be a tree that has the bound (a build_store that returns inside LTS_ROOT_DEADLINE_S, default 10 s: set it small in the
test's environment BEFORE the first build_store of the process, because it is read once per process). `tests/test_bounded_roots_boot.py` has the worked
examples: the renderer's flow (`_consumer_expand`) and a test of the harness itself under `python -I` with the engine absent. The real-server form
(`RealServer`) needs the engine checkout as `code_root=`. Both are a MODEL of a hang, not a hung drive.

TWO FORMS
  * in-process:   `with RootStall(root) as stall:` wraps the calls in THIS process; `stall.release()` lets blocked calls go.
  * real server:  `RealServer(...)` runs the engine's own `python -m factlattice_engine.server` with a test-only
                  `sitecustomize.py` (tests/stall_site/) first on PYTHONPATH. It installs the block at interpreter start-up,
                  records that it did, and the engine is unmodified: no seam, and no variable the ENGINE reads (the
                  variable is read by the interpreter-level hook). Because the environment survives os.execv, a server that
                  RE-EXECS on a code change (build_stamp) comes back with the block reinstalled; `generation` in the
                  confirmation file counts the installs, so a test can see that. Control files are per server (a fresh
                  directory), the confirmation must carry the child's own pid and the names asked for, and
                  `RealServer.start()` raises HarnessError otherwise. A child that dies raises HarnessError, so a block that
                  silently did not apply cannot read as a red record.

CALL FORMS WRAPPED (by replacing the module attribute, so late lookups `os.stat(...)` are caught):
  by path:   stat, lstat, scandir, listdir, walk (a generator, gated when first advanced), open (builtins.open AND io.open;
             pathlib's Path.open calls io.open), io.FileIO (a subclass; direct constructions only), os.open,
             os.replace / os.link / os.rename (gate on EITHER path), os.path.realpath / isdir / isfile / exists
             (explicit, so they stall at the call), mkdir, rmdir, unlink, remove, access, utime.
  by descriptor (fstat, fsync, fchmod, fcntl.flock): the harness keeps a map descriptor -> path for every descriptor it saw
             opened under the root (os.open, open, FileIO) while installed, checked against (st_dev, st_ino) before each
             use so a reused descriptor number never stalls an unrelated file. A file object is mapped through fileno().
  Path forms reach these through the stdlib: .exists/.is_file/.is_dir/.stat through os.stat or os.path.*, .open/.read_text/
  .read_bytes through io.open, .iterdir through os.listdir, .glob/.rglob through glob._StringGlobber (swept, see below),
  .resolve through os.path.realpath, .mkdir through os.mkdir, .unlink through os.unlink. The tests plant one site per form.
  REBINDING SWEEP: a module that did `from os import stat` holds the ORIGINAL function. On install, every attribute of every
  already-imported module under `sweep_prefixes` (factlattice, factlattice_engine, __main__) and of `glob` that IS an original
  is replaced by the wrapper, and put back on exit. Wrappers are also added to `os.supports_*` where the original was a member.

MATCHING. A path matches when, made absolute (no disk access), it equals the root or lies under it, for the root as given
AND for its real path (resolved at install). A path that does not match by spelling is also resolved with the real
`realpath` (cached) and matched again, so a symlink spelled outside the root that points into it stalls. LEFT UNMATCHED:
a symlink retargeted after its cache entry was made (the cache is cleared on engage and release); case-variant spellings on
a case-insensitive volume; hard links and bind mounts into the root; calls made with `dir_fd`; descriptors opened BEFORE
install (they have no recorded path); bytes paths that are not valid in the filesystem encoding.

NOT BLOCKABLE / NOT WRAPPED (a site that uses only these is not stalled; the inventory test is the cross-check):
  os.DirEntry methods (is_dir, is_file, stat, inode): a syscall on a cached listing, with no Python name to wrap, short of
  wrapping scandir's entries, which this does not do; read, write, ftruncate and close on an open descriptor; os.readlink,
  os.symlink, os.chmod, os.truncate, os.makedirs's own work beyond the os.mkdir it calls, shutil/tempfile internals that bind
  their own references, the stdlib's bare bindings outside the swept modules; C extensions that call libc; any other process
  (the real-server form installs the block once, in the child, at start). The planted-site checks (one per call name and per Path
  form) were run on CPython 3.9.23, 3.11.11, 3.12.13, 3.13.12 and 3.14.6, and all passed; 3.10 and every other patch release
  were not run, and `requires-python` is now >=3.11. A Python that routes a Path form differently fails its planted site.
"""
from __future__ import annotations

import builtins
import fcntl
import io
import json
import os
import posixpath
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any


class HarnessError(RuntimeError):
    """The instrument itself failed: the block did not apply, the child died, or a control failed. NOT an AssertionError,
    so it can never be absorbed as an expected failure (xfail raises=AssertionError)."""


#: stdlib modules that hold a bound original the swap would miss (glob._StringGlobber.scandir/lstat on 3.13, which is how
#: Path.glob and Path.rglob list a directory; pathlib._NormalAccessor on 3.9, which binds os.stat, os.mkdir and the rest). Add a module here when a Path form stops stalling.
_SWEEP_STDLIB = frozenset({"glob", "pathlib"})

#: the call names this harness can block; the order is the order of the positive controls
CALL_NAMES: tuple[str, ...] = (
    "stat", "lstat", "scandir", "listdir", "walk", "open", "os.open", "os.replace", "realpath", "isdir", "isfile", "exists",
    "link", "rename", "mkdir", "rmdir", "unlink", "remove", "access", "utime", "FileIO", "fstat", "fsync", "fchmod", "flock",
)
FD_NAMES = frozenset({"fstat", "fsync", "fchmod", "flock"})
HITS_KEPT = 1000

# originals, captured at import, before anything can be wrapped. The harness itself only ever uses these.
_REAL = {
    "os.stat": os.stat, "os.lstat": os.lstat, "os.scandir": os.scandir, "os.listdir": os.listdir, "os.walk": os.walk,
    "builtins.open": builtins.open, "io.open": io.open, "os.open": os.open, "os.replace": os.replace,
    "os.link": os.link, "os.rename": os.rename, "os.mkdir": os.mkdir, "os.rmdir": os.rmdir, "os.unlink": os.unlink,
    "os.remove": os.remove, "os.access": os.access, "os.utime": os.utime, "os.fstat": os.fstat, "os.fsync": os.fsync,
    "os.fchmod": os.fchmod, "fcntl.flock": fcntl.flock, "io.FileIO": io.FileIO,
    "os.path.realpath": posixpath.realpath, "os.path.isdir": posixpath.isdir, "os.path.isfile": posixpath.isfile,
    "os.path.exists": posixpath.exists,
}
_MONO = time.monotonic
_SLEEP = time.sleep
_REAL_STAT = os.stat
_REAL_FSTAT = os.fstat
_REAL_OPEN = builtins.open
_REAL_REALPATH = posixpath.realpath


def _fs(p: Any) -> str | None:
    """The path as str, or None for a descriptor / something that is not a path."""
    if isinstance(p, int) or p is None:
        return None
    try:
        s = os.fspath(p)
    except TypeError:
        return None
    if isinstance(s, bytes):
        try:
            s = os.fsdecode(s)
        except Exception:                                # noqa: BLE001
            return None
    return s


class RootStall:
    """Block disk calls under `root` until released. One stall is installed at a time per process."""

    _active: RootStall | None = None
    _active_lock = threading.Lock()

    def __init__(self, root: os.PathLike[str] | str, *, names: Iterable[str] = CALL_NAMES,
                 cap_s: float = 120.0, engaged_fn: Callable[[], bool] | None = None,
                 released_fn: Callable[[], bool] | None = None, log_fn: Callable[[dict], None] | None = None,
                 sweep_prefixes: Sequence[str] = ("factlattice", "factlattice_engine", "__main__"),
                 extra_roots: Sequence[str] = (), slow_s: float = 0.0, alias_match: bool = True) -> None:
        """`cap_s` is a hard ceiling on any single block: a stalled thread always ends. `slow_s` > 0 turns the model from
        HUNG to SLOW: each matching call sleeps `slow_s` and then proceeds."""
        self.root = _fs(root) or ""
        self._prefixes = tuple(dict.fromkeys(
            [posixpath.normpath(posixpath.abspath(self.root)), _REAL_REALPATH(self.root)]
            + [posixpath.normpath(x) for x in extra_roots]))
        bad = set(names) - set(CALL_NAMES)
        if bad:
            raise ValueError(f"unknown call names {sorted(bad)}; known: {CALL_NAMES}")
        self.names = frozenset(names)
        self.cap_s, self.slow_s, self._alias = float(cap_s), float(slow_s), alias_match
        self._released = threading.Event()
        self._engaged = threading.Event()
        self._engaged.set()
        self._engaged_fn, self._released_fn, self._log_fn = engaged_fn, released_fn, log_fn
        self._sweep_prefixes = tuple(sweep_prefixes)
        self.hits: list[tuple[str, str, str]] = []       # the FIRST HITS_KEPT gated calls (call name, path, thread)
        self.hit_count = 0                               # every gated call
        self._names_seen: set = set()
        self._hits_lock = threading.Lock()
        self._tl = threading.local()
        self._alias_cache: dict[str, str] = {}
        self._last_state: tuple[bool, bool] | None = None
        self._fds: dict[int, tuple[str, tuple[int, int]]] = {}
        self._undo: list[Callable[[], None]] = []
        self.rebound: list[tuple[str, str]] = []
        self.wrapped: dict[str, bool] = {}
        self.installed = False

    # ---- control -------------------------------------------------------------------------------
    def release(self) -> None:
        self._released.set()
        self._alias_cache.clear()

    def engage(self) -> None:
        self._engaged.set()
        self._alias_cache.clear()

    def disengage(self) -> None:
        self._engaged.clear()

    def hit_names(self) -> list[str]:
        with self._hits_lock:
            return sorted(self._names_seen)

    # ---- matching ------------------------------------------------------------------------------
    def _spelled_under(self, s: str) -> bool:
        return any(s == r or s.startswith(r.rstrip("/") + "/") for r in self._prefixes)

    def under_root(self, p: Any) -> bool:
        s = _fs(p)
        if not s:
            return False
        if not s.startswith("/"):
            s = posixpath.join(os.getcwd(), s)
        s = posixpath.normpath(s)
        if self._spelled_under(s):
            return True
        if not self._alias:
            return False
        rp = self._alias_cache.get(s)
        if rp is None:
            self._tl.busy = True                         # the real realpath calls the (wrapped) lstat: do not gate it
            try:
                rp = _REAL_REALPATH(s)
            except Exception:                            # noqa: BLE001
                rp = s
            finally:
                self._tl.busy = False
            if len(self._alias_cache) > 4096:
                self._alias_cache.clear()
            self._alias_cache[s] = rp
        return rp != s and self._spelled_under(rp)

    def _fd_path(self, fd: int) -> str | None:
        rec = self._fds.get(fd)
        if rec is None:
            return None
        try:
            st = _REAL_FSTAT(fd)
        except OSError:
            self._fds.pop(fd, None)
            return None
        if (st.st_dev, st.st_ino) != rec[1]:             # the number was reused for another file
            self._fds.pop(fd, None)
            return None
        return rec[0]

    def _target(self, x: Any) -> str | None:
        if isinstance(x, int) and not isinstance(x, bool):
            return self._fd_path(x)
        if x is None:
            return None
        if isinstance(x, (str, bytes, os.PathLike)):
            return _fs(x)
        fn = getattr(x, "fileno", None)
        if callable(fn):
            try:
                return self._fd_path(fn())
            except Exception:                            # noqa: BLE001
                return None
        return None

    def _remember(self, path: Any, fd: Any) -> None:
        """Record descriptor -> path for a file opened under the root (called after a successful open)."""
        if getattr(self._tl, "busy", False):
            return
        try:
            s = _fs(path)
            if s is None or not self.under_root(s):
                return
            if not isinstance(fd, int):
                fd = fd.fileno()
            st = _REAL_FSTAT(fd)
            self._fds[fd] = (s, (st.st_dev, st.st_ino))
        except Exception:                                # noqa: BLE001
            pass

    # ---- the gate ------------------------------------------------------------------------------
    def _is_engaged(self) -> bool:
        return bool(self._engaged_fn()) if self._engaged_fn is not None else self._engaged.is_set()

    def _is_released(self) -> bool:
        return bool(self._released_fn is not None and self._released_fn()) or self._released.is_set()

    def _gate(self, name: str, *things: Any) -> None:
        if getattr(self._tl, "busy", False) or name not in self.names or not self._is_engaged():
            return
        state = (self._is_engaged(), self._is_released())
        if state != self._last_state:                    # engage/release may come from a file the other process wrote
            self._last_state = state
            self._alias_cache.clear()
        hit = None
        for t in things:
            s = self._target(t)
            if s is not None and self.under_root(s):
                hit = s
                break
        if hit is None:
            return
        th = threading.current_thread().name
        with self._hits_lock:
            self.hit_count += 1
            self._names_seen.add(name)
            if len(self.hits) < HITS_KEPT:
                self.hits.append((name, hit, th))
        if self._log_fn is not None:
            self._log_fn({"call": name, "path": hit, "thread": th, "t": time.time()})
        if self.slow_s > 0:
            _SLEEP(self.slow_s)
            return
        end = _MONO() + self.cap_s
        while self._is_engaged() and not self._is_released() and _MONO() < end:
            _SLEEP(0.01)

    # ---- wrappers ------------------------------------------------------------------------------
    def _w1(self, name: str, fn: Callable) -> Callable:
        def wrapper(path=".", *a, **k):
            self._gate(name, path)
            return fn(path, *a, **k)
        return self._tag(wrapper, fn)

    def _w2(self, name: str, fn: Callable) -> Callable:
        def wrapper(src, dst, *a, **k):
            self._gate(name, src, dst)
            return fn(src, dst, *a, **k)
        return self._tag(wrapper, fn)

    def _wfd(self, name: str, fn: Callable) -> Callable:
        def wrapper(fd, *a, **k):
            self._gate(name, fd)
            return fn(fd, *a, **k)
        return self._tag(wrapper, fn)

    def _wopen(self, fn: Callable) -> Callable:
        def wrapper(file, *a, **k):
            self._gate("open", file)
            r = fn(file, *a, **k)
            self._remember(file, r)
            return r
        return self._tag(wrapper, fn)

    def _wosopen(self, fn: Callable) -> Callable:
        def wrapper(path, *a, **k):
            self._gate("os.open", path)
            fd = fn(path, *a, **k)
            self._remember(path, fd)
            return fd
        return self._tag(wrapper, fn)

    def _wwalk(self, fn: Callable) -> Callable:
        def wrapper(top, *a, **k):
            self._gate("walk", top)
            yield from fn(top, *a, **k)
        return self._tag(wrapper, fn)

    def _fileio(self, orig: type) -> type:
        stall = self

        class FileIO(orig):                              # type: ignore[misc, valid-type]
            def __init__(self, file, *a, **k):
                stall._gate("FileIO", file)
                super().__init__(file, *a, **k)
                stall._remember(file, self)
        FileIO.__name__ = "FileIO"
        return FileIO

    @staticmethod
    def _tag(wrapper: Callable, fn: Callable) -> Callable:
        wrapper.__wrapped__ = fn                         # type: ignore[attr-defined]
        wrapper.__name__ = getattr(fn, "__name__", "wrapper")
        return wrapper

    # ---- install / uninstall -------------------------------------------------------------------
    def install(self) -> RootStall:
        with RootStall._active_lock:
            if RootStall._active is not None:
                raise RuntimeError("a RootStall is already installed; uninstall it first")
            RootStall._active = self
        R = _REAL
        plan = [   # (owner, attribute, key into _REAL, wrapper)
            (os, "stat", "os.stat", self._w1("stat", R["os.stat"])),
            (os, "lstat", "os.lstat", self._w1("lstat", R["os.lstat"])),
            (os, "scandir", "os.scandir", self._w1("scandir", R["os.scandir"])),
            (os, "listdir", "os.listdir", self._w1("listdir", R["os.listdir"])),
            (os, "walk", "os.walk", self._wwalk(R["os.walk"])),
            (builtins, "open", "builtins.open", self._wopen(R["builtins.open"])),
            (io, "open", "io.open", None),                # same object as builtins.open: shares its wrapper
            (os, "open", "os.open", self._wosopen(R["os.open"])),
            (os, "replace", "os.replace", self._w2("os.replace", R["os.replace"])),
            (os, "link", "os.link", self._w2("link", R["os.link"])),
            (os, "rename", "os.rename", self._w2("rename", R["os.rename"])),
            (os, "mkdir", "os.mkdir", self._w1("mkdir", R["os.mkdir"])),
            (os, "rmdir", "os.rmdir", self._w1("rmdir", R["os.rmdir"])),
            (os, "unlink", "os.unlink", self._w1("unlink", R["os.unlink"])),
            (os, "remove", "os.remove", self._w1("remove", R["os.remove"])),
            (os, "access", "os.access", self._w1("access", R["os.access"])),
            (os, "utime", "os.utime", self._w1("utime", R["os.utime"])),
            (os, "fstat", "os.fstat", self._wfd("fstat", R["os.fstat"])),
            (os, "fsync", "os.fsync", self._wfd("fsync", R["os.fsync"])),
            (os, "fchmod", "os.fchmod", self._wfd("fchmod", R["os.fchmod"])),
            (fcntl, "flock", "fcntl.flock", self._wfd("flock", R["fcntl.flock"])),
            (io, "FileIO", "io.FileIO", self._fileio(R["io.FileIO"])),
            (posixpath, "realpath", "os.path.realpath", self._w1("realpath", R["os.path.realpath"])),
            (posixpath, "isdir", "os.path.isdir", self._w1("isdir", R["os.path.isdir"])),
            (posixpath, "isfile", "os.path.isfile", self._w1("isfile", R["os.path.isfile"])),
            (posixpath, "exists", "os.path.exists", self._w1("exists", R["os.path.exists"])),
        ]
        by_orig: dict[int, Callable] = {id(R[k]): w for _o, _a, k, w in plan if w is not None}
        for owner, attr, key, _w in plan:
            wrapper = by_orig[id(R[key])]
            setattr(owner, attr, wrapper)
            self._undo.append(lambda o=owner, a=attr, v=R[key]: setattr(o, a, v))
            self.wrapped[key] = getattr(owner, attr) is not R[key]
        for st in (os.supports_dir_fd, os.supports_fd, os.supports_follow_symlinks, os.supports_effective_ids):
            for _o, _a, key, _w in plan:
                if R[key] in st:
                    w = by_orig[id(R[key])]
                    st.add(w)
                    self._undo.append(lambda s=st, x=w: s.discard(x))
        # the rebinding sweep: module attributes, and staticmethods on classes defined in a swept module
        for mname, mod in list(sys.modules.items()):
            if mod is None or not (mname in _SWEEP_STDLIB or any(
                    mname == p or mname.startswith(p + ".") for p in self._sweep_prefixes)):
                continue
            try:
                items = list(vars(mod).items())
            except TypeError:
                continue
            for attr, val in items:
                w = by_orig.get(id(val))
                if w is not None and val is not w:
                    setattr(mod, attr, w)
                    self.rebound.append((mname, attr))
                    self._undo.append(lambda m=mod, a=attr, v=val: setattr(m, a, v))
                elif isinstance(val, type) and getattr(val, "__module__", None) == mname:
                    for k, raw in list(vars(val).items()):
                        fn = raw.__func__ if isinstance(raw, staticmethod) else (raw if callable(raw) else None)
                        w2 = by_orig.get(id(fn)) if fn is not None else None
                        if w2 is not None:
                            setattr(val, k, staticmethod(w2))
                            self.rebound.append((f"{mname}.{attr}", k))
                            self._undo.append(lambda c=val, a=k, v=raw: setattr(c, a, v))
        self.installed = True
        return self

    def uninstall(self) -> None:
        for u in reversed(self._undo):
            try:
                u()
            except Exception:                            # noqa: BLE001 — restoring must not stop at one failure
                pass
        self._undo.clear()
        self._fds.clear()
        self.installed = False
        with RootStall._active_lock:
            if RootStall._active is self:
                RootStall._active = None

    def __enter__(self) -> RootStall:
        return self.install()

    def __exit__(self, *exc) -> None:
        self.release()                                   # a blocked thread always ends before the wrappers go
        self.uninstall()


# ---- run a call with a hard limit ---------------------------------------------------------------------
class Outcome:
    """What `run_limited` saw: `done` False means the call was still blocked at the limit."""
    __slots__ = ("done", "value", "error", "elapsed", "thread")

    def __init__(self) -> None:
        self.done = False
        self.value: Any = None
        self.error: BaseException | None = None
        self.elapsed = 0.0
        self.thread: threading.Thread | None = None


def run_limited(fn: Callable[[], Any], limit_s: float, name: str = "stall-call") -> Outcome:
    """Run `fn` on a daemon thread and wait at most `limit_s`. Never raises, never hangs the caller."""
    out = Outcome()
    t0 = _MONO()

    def _run() -> None:
        try:
            out.value = fn()
        except BaseException as exc:                     # noqa: BLE001 — recorded, not raised
            out.error = exc
        out.elapsed = _MONO() - t0
        out.done = True

    th = threading.Thread(target=_run, name=name, daemon=True)
    out.thread = th
    th.start()
    th.join(limit_s)
    if not out.done:
        out.elapsed = _MONO() - t0
    return out


# ---- a two-root workspace, sibling temp dirs ----------------------------------------------------------
_DOC = ('---\nlts: "1.1"\ntitle: {c}\ndomain: x.{c}\ndoctype: SPEC\n---\n\n# R [{u}-ROOT]\n\n- summary: r\n\n'
        '## N [{u}-N]\n\n- topic: lanternfish\n')


class Workspace:
    """Sibling dirs under `base`: h (HOME), s (state), tmp, c/<id> (corpus roots), ctl (control files), r.json."""

    def __init__(self, base: os.PathLike[str] | str, corpora: Sequence[str] = ("a1", "b1")) -> None:
        self.base = Path(base)
        self.home, self.state, self.ctl = self.base / "h", self.base / "s", self.base / "ctl"
        self.registry_path = self.base / "r.json"
        self.corpora = tuple(corpora)
        for d in (self.home, self.state, self.ctl, self.base / "tmp"):
            d.mkdir(parents=True, exist_ok=True)
        reg = {}
        for c in self.corpora:
            root = self.base / "c" / c
            root.mkdir(parents=True, exist_ok=True)
            (root / f"{c}.lts").write_text(_DOC.format(c=c, u=c.upper()), encoding="utf-8")
            reg[c] = {"roots": [{"path": str(root)}], "domain_suffix": f"x.{c}"}
        self.registry = reg
        self.registry_path.write_text(json.dumps(reg), encoding="utf-8")

    def root(self, cid: str) -> Path:
        return self.base / "c" / cid

    def file(self, cid: str) -> Path:
        return self.root(cid) / f"{cid}.lts"

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """The child/in-process environment: nothing of the real HOME, state dir or registry."""
        e = {"HOME": str(self.home), "FACTLATTICE_STATE_DIR": str(self.state), "TMPDIR": str(self.base / "tmp"),
             "FACTLATTICE_CORPUS_CONFIG": str(self.registry_path), "FACTLATTICE_LEDGER_EPHEMERAL_ROOTS": ""}
        e.update(extra or {})
        return e


# ---- the real-server form ----------------------------------------------------------------------------
class NoReply(Exception):
    """The server did not answer within the limit (the call is still running)."""


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


SPEC_ENV = "FL_STALL_HARNESS_SPEC"
SITE_DIR = Path(__file__).resolve().parent / "stall_site"


class RealServer:
    """The engine's own server module, with the block installed by a start-up hook. See the module docstring.

    The child imports the engine from `code_root` (default: this checkout) through PYTHONPATH, with a working directory that
    holds no package, so `-m` cannot shadow it (when comparing two trees, print which package was imported):
    `imported_package()` asks that in the same environment.
    """

    def __init__(self, ws: Workspace, *, stall_root: os.PathLike[str] | str | None = None, engaged: bool = True,
                 env: dict[str, str] | None = None, code_root: Path | None = None, slow_s: float = 0.0,
                 names: Sequence[str] | None = None, cap_s: float = 120.0, install: bool = True,
                 confirm_s: float = 20.0) -> None:
        """`install=False` starts the plain server although a stall was asked for. It exists ONLY so a test can show that the
        harness refuses to continue when the block is not confirmed."""
        self.ws = ws
        self.code_root = Path(code_root) if code_root else repo_root()
        import uuid
        self.ctl = ws.ctl / f"srv-{uuid.uuid4().hex[:10]}"     # per server: no file of an earlier server can be mistaken for ours
        self.ctl.mkdir(parents=True)
        self.release_file, self.engage_file = self.ctl / "release", self.ctl / "engage"
        self.log_file, self.err_file = self.ctl / "hits.jsonl", self.ctl / "stderr.txt"
        self.installed_file, self.count_file, self.owner_file = self.ctl / "installed.json", self.ctl / "hits.count", self.ctl / "owner"
        self.expects_block = stall_root is not None
        self.confirm_s = confirm_s
        if engaged:
            self.engage_file.write_text("1")
        self.spec = None
        if stall_root is not None:
            self.spec = {"root": str(stall_root), "release_file": str(self.release_file), "engage_file": str(self.engage_file),
                         "log_file": str(self.log_file), "installed_file": str(self.installed_file), "count_file": str(self.count_file),
                         "owner_file": str(self.owner_file), "cap_s": cap_s,
                         "slow_s": slow_s, "names": list(names or CALL_NAMES)}
        e = dict(os.environ)
        for k in ("FACTLATTICE_CORPUS_CONFIG", "FACTLATTICE_STATE_DIR"):
            e.pop(k, None)
        self._install = self.spec is not None and install
        pp = [str(self.code_root)]
        if self._install:
            pp.insert(0, str(SITE_DIR))                    # first, so OUR sitecustomize is the one the interpreter finds
        e.update(ws.env({"PYTHONPATH": os.pathsep.join(pp), "PYTHONDONTWRITEBYTECODE": "1",
                         "FACTLATTICE_CODE_WATCH": "0", "PYTHONUNBUFFERED": "1"}))
        e.update(env or {})
        e.pop(SPEC_ENV, None)
        self.env = e                                       # the environment WITHOUT the spec: what a side question runs under
        self.child_env = dict(e)
        if self._install:
            self.child_env[SPEC_ENV] = json.dumps(self.spec)
        self.cmd = [sys.executable, "-m", "factlattice_engine.server"]
        self.proc: subprocess.Popen | None = None
        self._q: queue.Queue[dict | None] = queue.Queue()
        self._next_id = 0
        self._stash: dict[Any, dict] = {}
        self.spawned_at = 0.0

    # lifecycle
    def start(self) -> RealServer:
        self._errf = _REAL_OPEN(self.err_file, "wb")
        self.spawned_at = _MONO()
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._errf, cwd=str(self.ws.base), env=self.child_env)
        threading.Thread(target=self._pump, name="mcp-reader", daemon=True).start()
        if self.expects_block:
            try:
                self._confirm_block()
            except BaseException:
                self.close()
                raise
        return self

    def _confirm_block(self) -> None:
        end = _MONO() + self.confirm_s
        while True:
            if self.installed_file.exists():
                break
            if self.proc is not None and self.proc.poll() is not None:
                raise HarnessError(f"the server child exited (code {self.proc.returncode}) before the stall block was confirmed "
                                   f"installed; this is a harness failure, not a result. stderr tail: {self.stderr_text()[-400:]!r}")
            if _MONO() > end:
                raise HarnessError(f"the stall block was not confirmed installed within {self.confirm_s}s: the stall did not apply, "
                                   f"so any result from this server says nothing about a stalled root")
            _SLEEP(0.02)
        info = json.loads(self.installed_file.read_text())
        if info.get("pid") != self.proc.pid:
            raise HarnessError(f"the confirmation is for pid {info.get('pid')}, not this server's child {self.proc.pid}: "
                               f"a stale or foreign confirmation is not a confirmation")
        missing = sorted(k for k, v in info.get("wrapped", {}).items() if not v)
        if missing or not info.get("wrapped"):
            raise HarnessError(f"the child confirmed the block but these wrappers are not in place: {missing or 'none reported'}")
        asked = sorted(self.spec["names"])
        if sorted(info.get("names", [])) != asked:
            raise HarnessError(f"the block installed covers {sorted(info.get('names', []))}, not the names asked for {asked}")

    def generation(self) -> int:
        """How many times the block has been installed in this server's pid (2 after one re-exec)."""
        try:
            return int(json.loads(self.installed_file.read_text()).get("generation", 0))
        except (OSError, ValueError):
            return 0

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for raw in self.proc.stdout:
            try:
                self._q.put(json.loads(raw))
            except ValueError:
                continue
        self._q.put(None)

    def release(self) -> None:
        self.release_file.write_text("1")

    def engage(self) -> None:
        self.engage_file.write_text("1")

    def stderr_text(self) -> str:
        try:
            return self.err_file.read_text(errors="replace")
        except OSError:
            return ""

    def hits(self) -> list[dict]:
        try:
            return [json.loads(x) for x in self.log_file.read_text().splitlines() if x.strip()]
        except OSError:
            return []

    def hit_names(self) -> list[str]:
        return sorted({h["call"] for h in self.hits()})

    def hit_total(self) -> int:
        """EVERY call the block gated, from a counter the child keeps; `hits()` is only the first 400 detailed lines, so a
        full log never means the server stopped touching the root."""
        try:
            return int(self.count_file.read_text() or 0)
        except (OSError, ValueError):
            return 0

    def require_hit(self, why_none: str | None = None, wait_s: float = 10.0) -> None:
        """Every real-server test calls this: at least one call must have reached the block, or the test must say why none
        is expected. A run where nothing was blocked proves nothing about a blocked root. It waits up to `wait_s` for the first hit: a server
        on a loaded machine can take longer than a test's own short window to reach the root, and that is a late hit, not no hit."""
        end = _MONO() + wait_s
        while not (self.hit_total() or self.hits() or why_none):
            if _MONO() > end:
                break
            _SLEEP(0.05)
        if self.hit_total() or self.hits() or why_none:
            return
        raise HarnessError("no call was ever blocked: the stall was never reached, so this result is about a healthy root")

    def close(self) -> None:
        """Release the stall, close stdin, then terminate and kill. Safe to call twice."""
        try:
            self.release()
        except OSError:
            pass
        p = self.proc
        if p is not None:
            try:
                if p.stdin:
                    p.stdin.close()
            except OSError:
                pass
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
                    try:
                        p.wait(timeout=30)
                    except subprocess.TimeoutExpired:   # a SIGKILLed child that has not been reaped in 30 s (a loaded machine): the teardown does not raise
                        pass                            # over the result of the test it belongs to
        try:
            self._errf.close()
        except Exception:                                # noqa: BLE001
            pass

    def __enter__(self) -> RealServer:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # protocol
    def _send(self, obj: dict) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        try:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode())
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise HarnessError(f"the server child is gone (code {self.proc.poll()}); stderr tail: {self.stderr_text()[-400:]!r}") from None

    def request(self, method: str, params: dict | None = None, timeout: float = 5.0) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return self.wait_reply(rid, timeout)

    def send_call(self, name: str, args: dict) -> int:
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": self._next_id, "method": "tools/call", "params": {"name": name, "arguments": args}})
        return self._next_id

    def wait_reply(self, rid: int, timeout: float) -> dict:
        end = _MONO() + timeout
        while True:
            if rid in self._stash:
                return self._stash.pop(rid)
            left = end - _MONO()
            if left <= 0:
                self._raise_if_dead()
                raise NoReply(f"no reply to request {rid} within {timeout:.1f}s")
            try:
                msg = self._q.get(timeout=left)
            except queue.Empty:
                self._raise_if_dead()
                raise NoReply(f"no reply to request {rid} within {timeout:.1f}s") from None
            if msg is None:
                self._raise_if_dead(wait=True)
                raise HarnessError(f"the server closed its output before replying to {rid}")
            if "id" in msg:
                self._stash[msg["id"]] = msg

    def _raise_if_dead(self, wait: bool = False) -> None:
        p = self.proc
        if p is None:
            return
        if wait:
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if p.poll() is not None:
            raise HarnessError(f"the server child exited (code {p.returncode}): a dead server is a harness failure, never a "
                               f"'blocked' result. stderr tail: {self.stderr_text()[-400:]!r}")

    def initialize(self, timeout: float = 5.0) -> dict:
        r = self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                        "clientInfo": {"name": "stall-harness", "version": "0"}}, timeout)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return r

    def call(self, name: str, args: dict | None = None, timeout: float = 5.0) -> tuple[dict, float]:
        """(the tool's own JSON result, seconds). Raises NoReply on overrun, HarnessError if the child is dead."""
        t0 = _MONO()
        r = self.wait_reply(self.send_call(name, args or {}), timeout)
        return self.unwrap(r), _MONO() - t0

    @staticmethod
    def unwrap(reply: dict) -> dict:
        if "error" in reply:
            return {"success": False, "rpc_error": reply["error"]}
        try:
            return json.loads(reply["result"]["content"][0]["text"])
        except (KeyError, IndexError, ValueError):
            return {"success": False, "unparsed": reply}

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def imported_package(self) -> str:
        out = subprocess.run([sys.executable, "-c", "import factlattice,sys;sys.stdout.write(factlattice.__file__)"],
                             cwd=str(self.ws.base), env=self.env, capture_output=True, text=True, timeout=30)
        return out.stdout.strip()


# ---- the child side ---------------------------------------------------------------------------------
def install_server_hook(spec_json: str) -> RootStall:
    """Install the block in THIS process and record that it is installed. Control is by files the test process writes: the
    stall is ON while `engage_file` exists and `release_file` does not. Called by `site_install` at interpreter start-up."""
    spec = json.loads(spec_json)
    eng_f, rel_f, log_f, cnt_f = spec.get("engage_file"), spec.get("release_file"), spec.get("log_file"), spec.get("count_file")

    def exists(p: str | None) -> bool:
        if not p:
            return False
        try:
            _REAL_STAT(p)
            return True
        except OSError:
            return False

    state = {"n": 0, "lines": 0}
    log_lock = threading.Lock()

    def log(rec: dict) -> None:
        with log_lock:
            state["n"] += 1
            if cnt_f:                                     # the TOTAL, always current, whatever the detailed log holds
                # written whole and put in place by one rename: a reader that opened the file between a truncating "w" and the write read "" as 0
                # and raised "no call was ever blocked" on a run that had been blocked (c11_shape, about 1 run in 12)
                with _REAL_OPEN(cnt_f + ".part", "w") as fh:
                    fh.write(str(state["n"]))
                _REAL["os.replace"](cnt_f + ".part", cnt_f)
            if log_f and state["lines"] < 400:           # bounded: a polling thread would write forever
                state["lines"] += 1
                with _REAL_OPEN(log_f, "a") as fh:
                    fh.write(json.dumps(rec) + "\n")

    stall = RootStall(spec["root"], names=spec.get("names") or CALL_NAMES, cap_s=float(spec.get("cap_s", 120)),
                      engaged_fn=(lambda: exists(eng_f)) if eng_f else None,
                      released_fn=(lambda: exists(rel_f)) if rel_f else None, log_fn=log,
                      slow_s=float(spec.get("slow_s", 0.0)))
    stall.install()
    if spec.get("installed_file"):
        gen = 1
        try:
            with _REAL_OPEN(spec["installed_file"]) as fh:
                gen = int(json.load(fh).get("generation", 0)) + 1
        except (OSError, ValueError):
            pass
        tmp = spec["installed_file"] + ".part"
        with _REAL_OPEN(tmp, "w") as fh:
            json.dump({"pid": os.getpid(), "wrapped": stall.wrapped, "names": sorted(stall.names), "generation": gen}, fh)
        _REAL["os.replace"](tmp, spec["installed_file"])
    return stall


def site_install(spec_json: str) -> None:
    """Called from tests/stall_site/sitecustomize.py in every interpreter that inherits the spec variable. Only the process
    that first claims the owner file installs (the server; the same pid again after an exec). A subprocess it spawns does not."""
    spec = json.loads(spec_json)
    owner = spec["owner_file"]
    try:
        fd = _REAL["os.open"](owner, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        with _REAL_OPEN(owner) as fh:
            if fh.read().strip() != str(os.getpid()):
                return
    install_server_hook(spec_json)
