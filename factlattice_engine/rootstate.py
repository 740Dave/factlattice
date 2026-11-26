"""[MECH-BR-PROBE] [ROOT-STATE-ONE-REGISTRY] the one place a registered root's state lives.

A root is in exactly one state: OK, PENDING (never yet read) or UNRESPONSIVE (a call under it overran its deadline).
This module is the ONLY place those words are defined and compared; the gate, the listing and the extent read them
through `RootRegistry` (`mark_for`, `state_of`, `stuck_workers`) and never keep a second copy. A new state is added here
and reports through the same calls.

THE MODEL. Each root has one `RootSlot` (its queue and state). ALL freshness disk work for the root (the kernel's listing and
hashing, the existence check of the root and its pinned file) is a job, and jobs run on daemon threads of one pool, in two classes:

  * BACKGROUND: the watcher's polls and backstop passes. They share a small pool: ONE worker for the ordinary load, growing to
    `pool_size` (default 4) once runnable work has waited `grow_after_s`, plus one more for each stuck job (up to one per marked
    root), so a hung root never takes a healthy root's place in the background either.
  * FOREGROUND: a job a call is waiting on. It never queues behind OTHER ROOTS' background work: a thread is started for it at once if none
    is free, at most one per root (a root has one running job). On its OWN root the queue is first in, first out: it waits for that root's
    running job and for any job already queued ahead of it, background ones included (measured: a foreground full pass waited 0.92 s behind
    a queued 0.8 s background poll, because a poll cannot answer a full pass and so is not joined). Once it is next, it runs at once: its
    urgency belongs to the job and survives every requeue. A background job still queued when a call needs the same answer is
    PROMOTED to foreground (a queued full pass answers a poll). A job already running is never joined (x-verify).

A worker leaves after `idle_exit_s` with nothing to do, so an idle server holds a few threads, not one per root. So:

  * at most ONE job per root runs at any moment, however long it runs: a root hung for days costs one stuck thread;
  * THE DEADLINE MEASURES TIME WITH NO PROGRESS, not elapsed time ([MECH-BR-PROBE] x-progress). A job reports progress as each
    disk call returns (`progress()`; the kernel calls it after each stat, each hashed 64 KiB chunk and each listed directory),
    and the clock counts from the job's start and is reset at every report, never from a waiter's queueing. A job stopped on
    one call for `deadline_s` marks its root UNRESPONSIVE (read lazily by whoever looks, so no timer thread). A slow big
    corpus that keeps making progress is never marked, however long its pass takes. A waiter that gives up marks nothing;
  * when the stuck call finally returns, its result is DISCARDED (it describes an old moment) and the root is re-evaluated by
    one FULL pass, which makes progress as it goes; when that completes the root is OK again, with no restart;
  * a request for a kind already queued and not yet started joins it. A request made while a job of that kind is RUNNING
    does not (it began before the request, possibly before a write the caller just made) unless the caller says it only needs
    a result begun after a moment the running job began after (`satisfied`). That is the x-verify rule.

WHAT THIS CANNOT DO. It cannot cancel a call stuck in the operating system. It stops waiting for it, stops issuing more
and says so (`stuck_workers`). The thread is abandoned, not ended, and goes when the call returns or the process exits.
Nothing here holds any lock but the slot's own tiny one while it waits, and no disk call is made under it.
"""
from __future__ import annotations

import errno
import os
import stat
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable

from factlattice.rootview import CODE_CORPUS_VERDICT_PENDING as _CODE_CORPUS_VERDICT_PENDING
from factlattice.rootview import CODE_ROOT_UNRESPONSIVE as _CODE_ROOT_UNRESPONSIVE

OK = "ok"
PENDING = "pending"
UNRESPONSIVE = "unresponsive"
STATES = (OK, PENDING, UNRESPONSIVE)

# the stable codes this feature adds ([ACC-BR] x-codes, pinned by tests/test_bounded_roots_probes.py, `STABLE_CODES`). Five codes are emitted as of slice 4 (root-unresponsive, corpus-root-unresponsive,
# corpus-entry-shape, corpus-load-error and the registry's own); `call-overran` and `engine-saturated` are slice 5's, pinned now so their names cannot drift before they are used.
CODE_ROOT_UNRESPONSIVE = _CODE_ROOT_UNRESPONSIVE      # defined once in factlattice.rootview, which the core ledger and writers also need
CODE_CALL_OVERRAN = "call-overran"
CODE_ENGINE_SATURATED = "engine-saturated"
CODE_CORPUS_ROOT_UNRESPONSIVE = "corpus-root-unresponsive"
CODE_CORPUS_LOAD_ERROR = "corpus-load-error"        # [MECH-BR-BOOT] x-load-error: an unexpected error while loading one corpus fails that corpus only
CODE_CORPUS_VERDICT_PENDING = _CODE_CORPUS_VERDICT_PENDING      # defined once in factlattice.rootview (the ledger and writers need it); a corpus held pending for the registry-error verdict
VERDICT_PENDING = "verdict-pending"                 # the `reason` in a held corpus's `root_health` (status stays `pending`) and the key of its skip reason
CODE_CORPUS_ENTRY_SHAPE = "corpus-entry-shape"      # [MECH-BR-BOOT] x-codes: a registry entry the engine cannot read (register, or a string inside roots that raises)
STABLE_CODES = (CODE_ROOT_UNRESPONSIVE, CODE_CALL_OVERRAN, CODE_ENGINE_SATURATED, CODE_CORPUS_ROOT_UNRESPONSIVE, CODE_CORPUS_LOAD_ERROR, CODE_CORPUS_ENTRY_SHAPE, CODE_CORPUS_VERDICT_PENDING)
#: the `corpora_skipped` reason for a corpus left out of a corpus-omitted read because of a marked root
SKIP_REASON = {PENDING: "root-pending", UNRESPONSIVE: CODE_ROOT_UNRESPONSIVE, VERDICT_PENDING: CODE_CORPUS_VERDICT_PENDING}


def skip_reason(mark: dict) -> str:
    """The `corpora_skipped` reason for a marked corpus: a corpus held for the registry's verdict says so (`corpus-verdict-pending`), else by the state of its roots."""
    return SKIP_REASON[VERDICT_PENDING] if mark.get("reason") == VERDICT_PENDING and mark["status"] == PENDING else SKIP_REASON[mark["status"]]

#: [MECH-BR-BOOT] x-library: the most workers a PROCESS may hold stuck, across every kernel, every `build_store` and `boundedfs` (one
#: registry per process, so this is one count). Past it a new read of a root is refused at once, with no new thread.
MAX_STUCK_WORKERS = 16

ROOT_DEADLINE_ENV = "LTS_ROOT_DEADLINE_S"
DEFAULT_ROOT_DEADLINE_S = 10.0
IDLE_EXIT_S = 5.0
POOL_SIZE = 4          # the most workers the pool grows to for healthy work
GROW_AFTER_S = 0.1     # runnable work that has waited this long (a slow job holds the one worker) lets the pool grow


def read_root_deadline() -> float:
    """LTS_ROOT_DEADLINE_S, default 10. Read by the caller ONCE (the kernel's constructor, at process start); a value that
    is not a positive number is the default."""
    raw = os.environ.get(ROOT_DEADLINE_ENV, "")
    try:
        v = float(raw)
    except ValueError:
        return DEFAULT_ROOT_DEADLINE_S
    return v if v > 0 else DEFAULT_ROOT_DEADLINE_S


class RootNotAnswering(Exception):
    """A bounded read of a root did not answer within the root deadline (or was refused at once because a worker of that root is still stuck, or
    the process cap holds). Raised by the bounded loaders ([MECH-BR-BOOT]); the engine turns it into `corpus-root-unresponsive`. Carries no
    path: the corpus may be private."""


class ProfileNotAnswering(RootNotAnswering):
    """The corpus's PROFILE file did not answer within the root deadline (it may live under a root that hangs). The corpus is failed
    `corpus-root-unresponsive` with `reason: profile`, in the server as in a library, and the watcher reads it again once the profile answers."""


def iso_utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


_tls = threading.local()


def progress() -> None:
    """Report that the disk call the current job was in has returned. Cheap, and a no-op outside a pool job. The deadline counts
    from the LAST report (or the job's start), so a job that keeps making progress is never marked."""
    j = getattr(_tls, "job", None)
    if j is not None:
        j.progress = j.clock()


# ---- realpath with a progress hook ---------------------------------------------------------------------------------------------------
# `_realpath_copy` below is CPython's own `posixpath.realpath` (the 3.13.2 source, Lib/posixpath.py) copied WHOLE, with ONE change: `progress()` is
# called after each `os.lstat` and each `os.readlink` returns (the lines ending `# HOOK`), so a job resolving a slow root with several components reports
# progress per disk call instead of once at its end. Same error handling, same loop detection, same strict semantics; the algorithm is not re-derived
# here (a derived version differed from the platform's in three places: a missing component followed by `..` and a later link, the 40-link count, and a
# lstat/readlink race that raised). tests/test_bounded_roots_writes.py proves the copy differs from the running interpreter's source only at the hook
# lines, so a Python upgrade shows up as a test failure. Copyright (c) Python Software Foundation; used under the PSF License Agreement
# (https://docs.python.org/3/license.html), "Python Software Foundation License Version 2": the licence permits copying and modification provided
# the copyright notice and this licence notice are kept.
# It is USED only when the running interpreter's own posixpath.realpath, with the hook lines removed, equals it line for line (checked at import,
# below). On any other interpreter `realpath_progress` is os.path.realpath under an ELAPSED BUDGET (the root deadline times the path's components plus
# a margin: `_realpath_fallback`), so a slow root of bounded depth is neither marked nor refused; the engine never resolves a root by a different
# algorithm from the one the writers use for files. `REALPATH_IMPL` says which is active; the fallback's limits are stated in spec 38 x-progress.
# --- begin realpath copy
def _realpath_copy(filename, *, strict=False):
    """Return the canonical path of the specified filename, eliminating any
symbolic links encountered in the path."""
    filename = os.fspath(filename)
    if isinstance(filename, bytes):
        sep = b'/'
        curdir = b'.'
        pardir = b'..'
        getcwd = os.getcwdb
    else:
        sep = '/'
        curdir = '.'
        pardir = '..'
        getcwd = os.getcwd

    # The stack of unresolved path parts. When popped, a special value of None
    # indicates that a symlink target has been resolved, and that the original
    # symlink path can be retrieved by popping again. The [::-1] slice is a
    # very fast way of spelling list(reversed(...)).
    rest = filename.split(sep)[::-1]

    # Number of unprocessed parts in 'rest'. This can differ from len(rest)
    # later, because 'rest' might contain markers for unresolved symlinks.
    part_count = len(rest)

    # The resolved path, which is absolute throughout this function.
    # Note: getcwd() returns a normalized and symlink-free path.
    path = sep if filename.startswith(sep) else getcwd()

    # Mapping from symlink paths to *fully resolved* symlink targets. If a
    # symlink is encountered but not yet resolved, the value is None. This is
    # used both to detect symlink loops and to speed up repeated traversals of
    # the same links.
    seen = {}

    while part_count:
        name = rest.pop()
        if name is None:
            # resolved symlink target
            seen[rest.pop()] = path
            continue
        part_count -= 1
        if not name or name == curdir:
            # current dir
            continue
        if name == pardir:
            # parent dir
            path = path[:path.rindex(sep)] or sep
            continue
        if path == sep:
            newpath = path + name
        else:
            newpath = path + sep + name
        try:
            st_mode = os.lstat(newpath).st_mode
            progress()  # HOOK
            if not stat.S_ISLNK(st_mode):
                if strict and part_count and not stat.S_ISDIR(st_mode):
                    raise OSError(errno.ENOTDIR, os.strerror(errno.ENOTDIR),
                                  newpath)
                path = newpath
                continue
            if newpath in seen:
                # Already seen this path
                path = seen[newpath]
                if path is not None:
                    # use cached value
                    continue
                # The symlink is not resolved, so we must have a symlink loop.
                if strict:
                    # Raise OSError(errno.ELOOP)
                    os.stat(newpath)
                path = newpath
                continue
            target = os.readlink(newpath)
            progress()  # HOOK
        except OSError:
            progress()  # HOOK
            if strict:
                raise
            path = newpath
            continue
        # Resolve the symbolic link
        seen[newpath] = None # not resolved symlink
        if target.startswith(sep):
            # Symlink target is absolute; reset resolved path.
            path = sep
        # Push the symlink path onto the stack, and signal its specialness by
        # also pushing None. When these entries are popped, we'll record the
        # fully-resolved symlink target in the 'seen' mapping.
        rest.append(newpath)
        rest.append(None)
        # Push the unresolved symlink target parts onto the stack.
        target_parts = target.split(sep)[::-1]
        rest.extend(target_parts)
        part_count += len(target_parts)

    return path
# --- end realpath copy


def _posixpath_realpath_lines():
    """The running interpreter's `posixpath.realpath` source, or None when it cannot be read (a build with no source)."""
    try:
        import posixpath
        with open(posixpath.__file__, encoding="utf-8") as fh:
            src = fh.read()
        i = src.index("def realpath(filename, *, strict=False):")
        return src[i:src.index("supports_unicode_filenames", i)].rstrip().splitlines()
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def _copy_lines_without_hooks():
    with open(__file__, encoding="utf-8") as fh:
        t = fh.read()
    k = t.index("def _realpath_copy(filename, *, strict=False):")
    lines = [ln for ln in t[k:t.index("# --- end realpath copy", k)].rstrip().splitlines() if not ln.rstrip().endswith("# HOOK")]
    lines[0] = lines[0].replace("_realpath_copy", "realpath")
    return lines


#: seconds added to the fallback's allowance (see `_realpath_fallback`): the fixed margin beyond root deadline x components
FALLBACK_MARGIN_S = 1.0


def _realpath_fallback(filename, *, strict=False):
    """os.path.realpath on an interpreter whose own algorithm differs from the copy. Its progress cannot be seen per call, so the job gets an ELAPSED
    BUDGET instead of the per-call deadline: the root deadline times the number of components of the path (counted lexically) plus FALLBACK_MARGIN_S.
    A slow root of bounded depth whose every call answers is then neither marked nor refused; a truly hung one still is, within that budget. The
    allowance applies to this resolve only: the rest of the job is held to the plain deadline again."""
    j, reg = getattr(_tls, "job", None), getattr(_tls, "reg", None)
    prev = None
    if j is not None and reg is not None:
        fs = os.fspath(filename)
        sep = b"/" if isinstance(fs, bytes) else "/"
        n = max(1, len([c for c in fs.split(sep) if c]))
        prev = j.allow
        j.allow = reg.deadline_s * n + FALLBACK_MARGIN_S
    progress()
    try:
        out = os.path.realpath(filename, **({"strict": True} if strict else {}))     # `strict` only where asked: it does not exist before Python 3.10
    finally:
        progress()          # the closing report, taken BEFORE the allowance is dropped and on the exception path too: the plain deadline is then measured from it
        if j is not None and reg is not None:
            j.allow = prev
    return out


def _choose_copy() -> bool:
    """True when the running interpreter's posixpath.realpath, hooks removed, equals the copy. ANY failure of the comparison (no `__file__`, an
    unreadable source, a frozen or embedded build) selects the fallback: the engine must start on every interpreter."""
    try:
        theirs, mine = _posixpath_realpath_lines(), _copy_lines_without_hooks()
        return theirs is not None and theirs == mine
    except Exception:  # noqa: BLE001
        return False


REALPATH_COPY = "cpython-copy"
REALPATH_FALLBACK = "os.path.realpath"
_same = _choose_copy()
#: which resolver `realpath_progress` is on this interpreter: the CPython copy (per-component progress), or os.path.realpath (progress when it returns)
REALPATH_IMPL = REALPATH_COPY if _same else REALPATH_FALLBACK
realpath_progress = _realpath_copy if _same else _realpath_fallback


class Job:
    """One unit of disk work for one root. `fn()` runs on a pool worker and returns a `commit` callable (or None) that publishes
    its result into memory; the worker calls it only if the job did not stall."""

    __slots__ = ("kind", "fn", "keep_on_overrun", "started", "progress", "stalled", "done", "overran", "error", "result", "clock", "fg", "settles", "allow")

    def __init__(self, kind: str, fn: Callable[[], Callable[[], None] | None], keep_on_overrun: bool = False,
                 clock: Callable[[], float] = time.monotonic, foreground: bool = False, settles: bool = True) -> None:
        self.kind, self.fn, self.keep_on_overrun, self.clock = kind, fn, keep_on_overrun, clock
        #: a job that returns in time moves a root to OK. A job that only RESOLVES a path (it did not read the root's content) does not settle a
        #: root that has never been read: it must not turn a PENDING root of the kernel into OK. It still marks UNRESPONSIVE when it overruns.
        self.settles = settles
        #: while set (seconds), the silence this job may keep before it counts as late, instead of the root deadline: see `_realpath_fallback`
        self.allow: float | None = None
        #: a call is waiting on this job. It lives WITH THE JOB, so it survives every requeue of its slot (the slot's running job ending,
        #: a recovery job inserted ahead of it): the slot is foreground whenever any job queued on it is
        self.fg = foreground
        self.started: float | None = None
        self.progress: float = 0.0
        self.stalled = False
        self.done = threading.Event()
        self.overran = False
        self.error: BaseException | None = None
        self.result = None


#: which finished kinds satisfy a request of a kind (a poll's result includes the observation; a full pass includes a poll)
_SATISFIED_BY = {"poll": ("poll", "full"), "observe": ("observe", "poll", "full"), "full": ("full",)}


class RootSlot:
    def __init__(self, path: str, reg: RootRegistry) -> None:
        self.path = path
        self._reg = reg
        self._queue: list[Job] = []
        self._running: Job | None = None
        self._queued_ready = False
        self._queued_fg = False
        self._dropped = False                  # nothing uses this root any more; removed once its last job ends
        self._gone = False                     # removed from the registry: a waiter for its first read stops waiting (the root is not going to be read)
        #: not a registered root: a state location, an old spelling or a root only some call has asked about. It shares the pool and its stuck
        #: threads count in `stuck_workers`, but it is never listed as a root (`root_paths`).
        self.aux = False
        self._ready_since = 0.0
        self._state = PENDING
        self._since = reg.wall()
        self._done_at: dict[str, float] = {}          # kind -> start time of the last job of that kind that published
        #: builds the recovery job (a FULL re-evaluation of this root); set by the kernel
        self.recover: Callable[[], Job] | None = None

    # ---- state (every method below takes the registry's one condition) -----------------------
    def _lapsed_locked(self) -> float:
        r = self._running
        if r is None or r.started is None:
            return 0.0
        return self._reg.clock() - r.progress

    def _late_locked(self) -> float:
        """How far past its allowance the running job's silence is (> 0: no progress for longer than it may). The allowance is the registry's root
        deadline, or the job's own `allow` while it is inside a resolve whose progress cannot be seen (`_realpath_fallback`)."""
        return self._lapsed_locked() - self._reg._allowed(self._running)

    def _lazy_locked(self) -> str:
        """A running job with no progress for the deadline IS the mark; whoever looks first sets it. Caller holds the lock."""
        late = self._late_locked()
        if late > 0 and self._state != UNRESPONSIVE:
            self._state = UNRESPONSIVE
            self._since = self._reg.wall() - late
            if self._running is not None:
                self._running.stalled = True
            self._reg._cv.notify_all()
        return self._state

    def state(self) -> str:
        with self._reg._cv:
            return self._lazy_locked()

    def info(self):
        """(state, since epoch seconds) read together."""
        with self._reg._cv:
            s = self._lazy_locked()
            return s, self._since

    def busy(self) -> bool:
        """A job is running or queued. A poll that finds the root busy skips it ([MECH-BR-PROBE] x-no-pile-up)."""
        with self._reg._cv:
            self._lazy_locked()
            return self._running is not None or bool(self._queue)

    def stuck(self) -> bool:
        with self._reg._cv:
            return self._running is not None and self._late_locked() > 0

    def satisfied(self, kind: str, since: float) -> bool:
        """Did a job that could answer a `kind` request begin at or after `since` and publish? (Then the caller needs no new one.)"""
        with self._reg._cv:
            return any(self._done_at.get(k, -1.0) >= since for k in _SATISFIED_BY.get(kind, (kind,)))

    def settle_ok(self) -> None:
        """A root read, hashed and seated by a fused job is settled here, where its seat job used to end: a PENDING root reads ok. Memory only. An UNRESPONSIVE root is never settled
        here: a job's overrun marked it and its recovery pass is queued, and only a probe begun after the mark clears it (x-mark-clears); the fused job that read the root finished before
        that mark, so it proves nothing about now. The lazy transition runs first: a pending root whose running job is already late is unresponsive and is not settled."""
        reg = self._reg
        with reg._cv:
            if self._lazy_locked() == PENDING and not self._gone:      # the lazy transition first: a root whose job is already late is unresponsive, not pending
                self._state = OK
                self._since = reg.wall()
            reg._cv.notify_all()

    def wait_not_pending(self, timeout: float) -> str:
        """Wait up to `timeout` for the first read to end. A pending root with no progress for the deadline is unresponsive."""
        reg = self._reg
        end = reg.clock() + timeout
        with reg._cv:
            while self._lazy_locked() == PENDING and not self._gone:
                left = end - reg.clock()
                if left <= 0:
                    break
                reg._spawn_locked()
                reg._cv.wait(min(left, 0.05))
            return self._lazy_locked()

    # ---- work ---------------------------------------------------------------------------------
    def submit(self, kind: str, fn: Callable[[], Callable[[], None] | None], *, coalesce: bool = True,
               keep_on_overrun: bool = False, foreground: bool = False, settles: bool = True) -> Job:
        """Queue a job. FOREGROUND means a call is waiting on it: it never waits behind background work for a pool thread (one is
        started for it at once, up to one per root), and a background job it joins is PROMOTED. A request joins a job still queued
        and not started whose kind would answer it (a full pass answers a poll, a poll answers an observation); one already running
        is never joined (x-verify)."""
        reg = self._reg
        with reg._cv:
            if coalesce:
                for j in self._queue:
                    if j.kind in _SATISFIED_BY.get(kind, (kind,)) and j.started is None:
                        j.fg = j.fg or foreground                   # a foreground waiter promotes a queued background job, for good
                        reg._enqueue_locked(self, foreground)
                        return j
            job = Job(kind, fn, keep_on_overrun, reg.clock, foreground, settles)
            self._queue.append(job)
            reg._enqueue_locked(self, foreground)
            return job


class RootRegistry:
    """Every registered root's slot, the pool that runs their jobs, and the words for what they report."""

    def __init__(self, deadline_s: float | None = None, *, pool_size: int = POOL_SIZE, grow_after_s: float = GROW_AFTER_S, idle_exit_s: float = IDLE_EXIT_S,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time) -> None:
        self.deadline_s = float(deadline_s) if deadline_s else read_root_deadline()
        self.pool_size = max(1, int(pool_size))
        self.grow_after_s = grow_after_s
        self.idle_exit_s = idle_exit_s
        self.clock, self.wall = clock, wall
        _lock = threading.Lock()
        self._cv = threading.Condition(_lock)      # state changes (waiters)
        self._work = threading.Condition(_lock)    # runnable work (workers): a separate condition so one new job wakes ONE worker, not all
        # ONE lock for every slot and the pool: held for microseconds, never across disk work
        self._slots: dict[str, RootSlot] = {}
        self._ready: deque[RootSlot] = deque()      # background work
        self._fg: deque[RootSlot] = deque()         # work a call is waiting on
        self._threads = 0
        self._idle = 0
        self._fg_running = 0
        self._bg_running = 0
        self._live: list[int] = []           # worker ids, oldest first: the first `cap` of them are the ones that take background work
        self._seq = 0
        self._trim = False                 # set by trim_idle: an idle worker leaves now instead of after idle_exit_s

    # ---- slots ----------------------------------------------------------------------------------
    def slot(self, path: str, create: bool = True, aux: bool = False) -> RootSlot | None:
        with self._cv:
            s = self._slots.get(path)
            if s is None and create:
                s = self._slots[path] = RootSlot(path, self)
                s.aux = aux
            elif s is not None and create:
                s._dropped = False                 # in use again
                if not aux:
                    s.aux = False                  # a registered root asked for a slot that began as a call's own: it is a root now
            return s

    def paths(self) -> list[str]:
        with self._cv:
            return list(self._slots)

    def root_paths(self) -> list[str]:
        """The registered roots' slots, without the auxiliary ones (state locations, old spellings, roots only a call asked about)."""
        with self._cv:
            return [p for p, s in self._slots.items() if not s.aux]

    def drop(self, path: str) -> None:
        """Forget a root nothing uses any more. A slot with a job in flight (or queued) is NOT removed: it stays registered, still
        reporting its state and still counted as stuck, and goes when its last job is done. So a corpus unregistered while its root is
        marked, and registered again, meets the mark instead of a fresh slot that knows nothing. NOT airtight: between a job ending (running set to
        None) and the recovery job of a stalled one being inserted, a drop sees an idle slot and removes it, and the recovery job then runs on a slot
        the registry no longer holds, beside a new one if the root is registered again at once. Known, recorded as P3, not fixed here."""
        with self._cv:
            s = self._slots.get(path)
            if s is None:
                return
            if s._running is not None or s._queue:
                s._dropped = True
            else:
                s._gone = True
                del self._slots[path]
                self._cv.notify_all()

    def state_of(self, path: str) -> str | None:
        s = self.slot(path, create=False)
        return s.state() if s is not None else None

    def _allowed(self, job: Job | None) -> float:
        a = getattr(job, "allow", None) if job is not None else None
        return a if a else self.deadline_s

    def mark_for(self, paths: Iterable[str]) -> dict | None:
        """None when none of `paths` is pending or unresponsive; else {status, roots, since}: `status` is unresponsive if any
        is (the worse state), `roots` counts the marked roots, `since` is the earliest mark (ISO UTC). No path is returned:
        a root of a private corpus must never reach a reply."""
        marked = []
        for p in dict.fromkeys(paths):
            s = self.slot(p, create=False)
            if s is None:
                continue
            st, since = s.info()
            if st != OK:
                marked.append((st, since))
        if not marked:
            return None
        status = UNRESPONSIVE if any(m[0] == UNRESPONSIVE for m in marked) else PENDING
        return {"status": status, "roots": len(marked), "since": iso_utc(min(m[1] for m in marked))}

    def stuck_workers(self) -> int:
        with self._cv:
            return sum(1 for s in list(self._slots.values())
                       if s._running is not None and s._late_locked() > 0)

    def last_progress(self) -> float:
        """The latest progress report of any RUNNING job (this registry's clock), or 0.0 when none runs. Memory only."""
        with self._cv:
            return max([s._running.progress for s in list(self._slots.values()) if s._running is not None] or [0.0])

    def admit(self, paths: Iterable[str]) -> str | None:
        """[MECH-BR-BOOT] x-library: why a NEW read of these roots must not start, or None. `"root-stuck"`: a root of them is unresponsive (a worker is
        still in its stuck call, or the recovery pass has not ended), so the one worker that root may have is not replaced. `"process-cap"`: the process
        already holds MAX_STUCK_WORKERS stuck workers. Either way the caller fails the corpus at once and starts NO thread, so a caller that builds
        repeatedly, per request, during a stall cannot pile threads up. Memory only."""
        with self._cv:
            stuck = 0
            for s in list(self._slots.values()):
                if s._running is not None and s._late_locked() > 0:
                    stuck += 1
            if stuck >= MAX_STUCK_WORKERS:
                return "process-cap"
            for p in paths:
                s = self._slots.get(p)
                if s is not None and s._lazy_locked() == UNRESPONSIVE:
                    return "root-stuck"
        return None

    def thread_count(self) -> int:
        with self._cv:
            return self._threads

    # ---- the pool ---------------------------------------------------------------------------------
    def _enqueue_locked(self, slot: RootSlot, foreground: bool = False) -> None:
        """Make `slot` runnable if it has a queued job and none running; start a worker if none is free. A slot already waiting as
        background work is moved to the foreground when a call starts waiting on it."""
        foreground = foreground or any(j.fg for j in slot._queue)       # urgency is read from the queued jobs, not from who last called
        if slot._running is None and slot._queue:
            if not slot._queued_ready:
                slot._queued_ready = True
                slot._queued_fg = foreground
                slot._ready_since = self.clock()
                (self._fg if foreground else self._ready).append(slot)
                self._work.notify()
            elif foreground and not slot._queued_fg:
                try:
                    self._ready.remove(slot)
                except ValueError:
                    pass
                slot._queued_fg = True
                self._fg.append(slot)
                self._work.notify()
        self._spawn_locked()

    def _spawn_locked(self) -> None:
        """Start workers. FOREGROUND work (a call is waiting) never queues behind background work: while it outnumbers the idle
        workers a thread is started for it at once; there is one running job per root, so that is at most one thread per root. BACKGROUND
        work gets one worker for the ordinary load (more threads only fight over the interpreter lock: 4.3 ms of CPU per sweep of 72
        roots on one worker, 11.3 ms on four), growing to `pool_size` only once runnable work has waited `grow_after_s`, plus ONE extra for
        each stuck job (up to one per marked root), so a hung root never takes a healthy root's place. A worker leaves after
        `idle_exit_s` with nothing to do, so an idle server holds a few threads."""
        # A burst (a listing probes 72 roots at once) must not become 72 threads: up to `pool_size` foreground jobs start at once, and
        # more only as the oldest has waited `grow_after_s` (the waiters nudge this every few milliseconds)
        burst_ok = self._fg and self.clock() - self._fg[0]._ready_since > self.grow_after_s
        while len(self._fg) > self._idle and (self._fg_running + self._idle < self.pool_size or burst_ok):
            self._start_worker_locked()
        if not self._ready:
            return
        cap = self._bg_cap_locked()
        if self._bg_running < cap:
            if self._idle == 0 and len(self._live) < cap:
                self._start_worker_locked()
            else:
                self._work.notify()                 # a worker that was not allowed to take background work may be now
        return

    def _bg_cap_locked(self) -> int:
        """How many background jobs may run at once: ONE, `pool_size` once runnable background work has waited `grow_after_s`, and one
        more for each stuck job (up to one per marked root)."""
        stuck = sum(1 for s in list(self._slots.values())
                    if s._running is not None and s._late_locked() > 0)
        grown = bool(self._ready) and self.clock() - self._ready[0]._ready_since > self.grow_after_s
        return (self.pool_size if grown else 1) + stuck

    def _start_worker_locked(self) -> None:
        self._seq += 1
        self._threads += 1
        self._live.append(self._seq)
        threading.Thread(target=self._worker, args=(self._seq,), name=f"lts-root-pool-{self._seq}", daemon=True).start()
        self._idle += 1                     # counts as soon-to-be idle, so one burst of work does not over-spawn

    def trim_idle(self, wait_s: float = 1.0) -> None:
        """Let the idle workers leave NOW (not after `idle_exit_s`) and wait, up to `wait_s`, until they have. A busy or stuck worker is untouched. For a
        library caller (`build_store`) that has no kernel to keep the pool warm: after it returns, only a stuck worker may remain."""
        with self._cv:
            self._trim = True
            self._work.notify_all()
        end = time.monotonic() + wait_s
        try:
            while time.monotonic() < end:
                with self._cv:
                    if self._idle <= 0:
                        break
                time.sleep(0.002)
        finally:
            with self._cv:
                self._trim = False

    def rebalance(self) -> None:
        """A waiter's nudge: a worker stuck since the last look is replaced now, not at the next submission."""
        with self._cv:
            self._spawn_locked()

    def wait(self, slot: RootSlot, job: Job) -> bool:
        """Wait for `job` to finish. Ends when it is done, or when its root has made no progress for the deadline (marked). A
        waiter's own patience marks nothing: only the job's lack of progress does. True iff the job finished and published."""
        step = max(0.01, min(0.05, self.deadline_s / 4))
        while True:
            if job.done.wait(step):
                return not job.overran
            self.rebalance()
            if slot.state() == UNRESPONSIVE:
                return job.done.is_set() and not job.overran

    def _worker(self, me: int) -> None:
        # `_idle` was incremented by the spawner for this thread; the loop keeps the count honest from here on
        idle_since = self.clock()
        try:
            while True:
                with self._cv:
                    while True:
                        if self._fg:
                            from_fg = True
                            break
                        # background work goes to the OLDEST `cap` workers only, so the extra threads a burst started stay idle and leave,
                        # instead of every thread taking a turn at a one-worker load and never going idle
                        if self._ready and self._bg_running < self._bg_cap_locked() \
                                and self._live.index(me) < self._bg_cap_locked():
                            from_fg = False
                            break
                        # idle time is counted from when THIS thread last worked, not from its last wake-up: a notify that another
                        # worker wins would otherwise restart the clock, and under steady polling no thread would ever leave
                        left = 0.0 if self._trim else self.idle_exit_s - (self.clock() - idle_since)
                        if left <= 0:
                            self._idle -= 1
                            return
                        self._work.wait(min(left, 0.25) if self._ready else left)
                    slot = (self._fg if from_fg else self._ready).popleft()
                    slot._queued_ready = slot._queued_fg = False
                    self._idle -= 1
                    if from_fg:
                        self._fg_running += 1
                    else:
                        self._bg_running += 1
                    job = slot._queue.pop(0)
                    job.started = job.progress = self.clock()
                    slot._running = job
                self._run_job(slot, job)
                with self._cv:
                    self._idle += 1
                    if from_fg:
                        self._fg_running -= 1
                    else:
                        self._bg_running -= 1
                    idle_since = self.clock()
        finally:
            with self._cv:
                self._threads -= 1
                try:
                    self._live.remove(me)
                except ValueError:
                    pass
                self._cv.notify_all()

    def _run_job(self, slot: RootSlot, job: Job) -> None:
        _tls.job = job
        _tls.reg = self
        commit = None
        try:
            commit = job.fn()
        except BaseException as exc:  # noqa: BLE001 — recorded on the job; the worker must outlive a bad job
            job.error = exc
        finally:
            _tls.job = None
        recover = None
        with self._cv:
            now = self.clock()
            job.overran = (job.stalled or now - job.progress > self.deadline_s) and not job.keep_on_overrun
            slot._running = None
            if job.overran:
                commit, job.error = None, None            # an old moment: not published
                if slot._state != UNRESPONSIVE:
                    slot._state = UNRESPONSIVE
                    slot._since = self.wall()
                recover = slot.recover
        if recover is not None:
            try:
                j2 = recover()
            except BaseException:  # noqa: BLE001
                j2 = None
            if j2 is not None:
                with self._cv:
                    slot._queue.insert(0, j2)          # the root's NEXT job, ahead of anything queued
        if not job.overran and commit is not None:
            try:
                commit()
            except BaseException as exc:  # noqa: BLE001
                job.error = job.error or exc
        with self._cv:
            if not job.overran:
                if job.error is None and not getattr(job.fn, "errors", None):   # only a job that read AND published answers later requests (a raised one did not)
                    slot._done_at[job.kind] = max(slot._done_at.get(job.kind, -1.0), job.started)
                if slot._state != OK and job.settles:
                    slot._state = OK
                    slot._since = self.wall()
            self._enqueue_locked(slot)
            if slot._dropped and slot._running is None and not slot._queue and self._slots.get(slot.path) is slot:
                slot._gone = True
                del self._slots[slot.path]
            elif (slot.aux and slot._state == OK and slot._running is None and not slot._queue and not slot._queued_ready
                  and self._slots.get(slot.path) is slot):
                del self._slots[slot.path]         # an auxiliary slot that is idle and answering holds nothing: no growth with the keys asked about
            self._cv.notify_all()
        job.done.set()


# ---- ONE registry per PROCESS ------------------------------------------------------------------------------------------------------------
# [MECH-BR-BOOT] x-library: "root state is a fact about a disk, not about a kernel". Every kernel, every `build_store` and `boundedfs` use THIS
# registry, so a root has one slot and one running job whatever asked, at most one worker of a root is ever stuck, and the cap is one count. The
# deadline is read once, when the registry is first made (LTS_ROOT_DEADLINE_S). Nothing else constructs a RootRegistry for a root.
_PROC_LOCK = threading.Lock()
_PROC: RootRegistry | None = None


def process_registry() -> RootRegistry:
    """The process's one registry, made on first use (the deadline is read from the environment then, once)."""
    global _PROC
    with _PROC_LOCK:
        if _PROC is None:
            _PROC = RootRegistry()
        return _PROC


def reset_process_registry(deadline_s: float | None = None, **kw) -> RootRegistry | None:
    """FOR TESTS (and nothing else): forget every slot and start the registry over with this deadline. With no deadline and no option the registry is
    dropped and the next use makes a new one, reading the environment THEN (so a test that sets LTS_ROOT_DEADLINE_S after the reset gets it). Threads
    stuck in the old one stay stuck, as they would. This is the ONE helper a test that needs another deadline calls; `boundedfs._reset` calls it."""
    global _PROC
    with _PROC_LOCK:
        _PROC = RootRegistry(deadline_s, **kw) if (deadline_s is not None or kw) else None
        return _PROC
