"""[LEC-FRESHNESS] — the INCREMENTAL, event-driven freshness kernel.

============================================================================
The problem this solves ([SPEC:LTS-ENGINE-CONSOLIDATION] [LEC-FRESHNESS], the #1
perf finding): the base engine re-hashes the WHOLE corpus on EVERY query
(`Partition.freshness()` -> `combined_files_hash(root_paths)` -> `files_hash()` over
every `.lts`). On a long-running MCP stdio server that is an O(corpus-bytes) I/O tax
on every single call.

This kernel maintains freshness INCREMENTALLY so a query does ~O(0) validation while
KEEPING the content hash as the sole correctness oracle:

  * content-hash LEAVES         — the mtime-immune per-file content hash (`file_content_hash`)
                                  stays the oracle; a leaf hash NEVER derives from mtime/size.
  * an incrementally-maintained ROOT — on a change event only the affected leaf/leaves are
                                  re-hashed and the root recombined (Merkle-style). Unchanged
                                  files are NOT re-read. `Partition.freshness()` becomes an
                                  O(1) compare of the maintained root against the index hash.
  * SEAT on startup/reconnect   — one full re-hash establishes the leaf maps + root.
  * a filesystem WATCH          — a dependency-light STDLIB polling watcher (mtime, size, mode,
                                  inode and ctime are a
                                  HINT for WHICH leaves to re-check; the content hash is the
                                  verdict). No third-party dep (the watcher adds none by design;
                                  `watchdog` is NOT a declared dependency).
  * ONE PROBE PER ROOT           — [MECH-BR-PROBE] all disk work for a root (listing, stat hints, content hashes, the
                                  existence check of the root and its pinned file) is a job on THAT ROOT'S OWN worker
                                  (`rootstate.RootSlot`), run on a COPY of the root's state with NO kernel lock held. The lock
                                  guards only the in-memory results a finished job publishes. A root that does not answer
                                  inside LTS_ROOT_DEADLINE_S is marked unresponsive and costs one stuck thread, not one
                                  per poll; the public calls below (`poll_once`, `full_reverify`, `tick`, `seat_partition`)
                                  keep their synchronous contract by WAITING on the workers, never by touching a disk.
  * a periodic full RE-VERIFY   — the bounded backstop: `full_reverify()` re-hashes every leaf
                                  unconditionally so a MISSED or mtime-invisible change (e.g. a
                                  content-preserving-mtime edit) can NEVER leave the server
                                  silently serving stale.

MTIME IS A HINT, CONTENT IS THE ORACLE. `poll_once()` uses the stat hint only to pick which
leaves to re-hash; it then re-hashes their CONTENT and only flips the root if the content
hash actually changed (so a `touch` with identical bytes yields NO false-stale). Because the
hint carries ctime and the inode, which userland cannot set back, an edit that restores mtime
and size is still seen by the next poll. What the hint can still miss (a filesystem
whose ctime cannot be trusted, a clock set backwards to the same ctime) is closed by
`full_reverify()`, the periodic backstop.
============================================================================
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import factlattice.hashing as _hashing
from factlattice import CorpusFileError, file_content_hash, list_lts_files
from factlattice.hashing import CORPUS_FILE_UNREADABLE

from .rootstate import (
    CODE_CORPUS_ROOT_UNRESPONSIVE,
    PENDING,
    UNRESPONSIVE,
    Job,
    RootNotAnswering,
    process_registry,
    reset_process_registry,
)
from .rootstate import progress as _progress
from .rootstate import realpath_progress as _realpath_progress

# NOTE: set when this module is first imported (every process that seats a kernel imports it), process-wide, and left set. A hasher or
# lister that runs before that reports nothing, which only means no job is timing it. Library callers with no kernel see the default (None).
_hashing.progress_hook = _progress         # the hashing and listing report progress to the deadline of the job running them


# ---------------------------------------------------------------------------
# Leaf recombine — reproduce `factlattice.hashing.files_hash` +
# `store.combined_files_hash` EXACTLY, but from an in-memory leaf map WITHOUT
# re-reading unchanged files. Parity with those two functions is load-bearing:
# the maintained root is compared against the index's stored `files_hash`, so it
# must be byte-identical to what the builder stamped. (tests assert this parity.)
# ---------------------------------------------------------------------------
def combine_leaf_map(fmap: dict[str, str]) -> str:
    """SHA-256 over the sorted, newline-joined 'relpath\\x00filehash' records.

    Byte-identical to `factlattice.hashing.files_hash(root)[0]` for the same
    {relpath: file_content_hash} map — but computed from the cached map, so
    unchanged files are never re-read.
    """
    joined = "\n".join(f"{rel}\x00{fmap[rel]}" for rel in sorted(fmap))
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


def combine_roots(per_root: dict[str, str]) -> str:
    """Combine per-root hashes into the partition root.

    Byte-identical to `store.combined_files_hash(root_paths)`: a single root is
    returned as-is (degenerate case); multiple roots are SHA-256 over the sorted,
    newline-joined 'root\\x00roothash' records.
    """
    if len(per_root) == 1:
        return next(iter(per_root.values()))
    parts = [f"{root}\x00{per_root[root]}" for root in sorted(per_root)]
    return "sha256:" + hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
@dataclass
class RootState:
    """Maintained state for ONE corpus root: the content-hash leaves + the mtime/size
    HINT snapshot used to decide which leaves to re-check. `hints` is NEVER consulted
    as a freshness verdict — only to narrow the set of files re-hashed by the oracle."""
    root_path: str
    leaves: dict[str, str] = field(default_factory=dict)          # rel -> content hash
    hints: dict[str, tuple[float, int, int, int, int]] = field(default_factory=dict)  # rel -> (mtime, size, mode, inode, ctime_ns)
    # (code, rel, hint-of-the-bad-member) while a member is a corpus-file-* failure; leaves are EMPTY then, so the root diverges (STALE)
    fault: tuple[str, str, tuple[float, int, int, int, int]] | None = None
    # bumped whenever the published state changes by any route; a finished probe publishes only if it still holds the version it copied
    ver: int = 0

    def root_hash(self) -> str:
        return combine_leaf_map(self.leaves)


class _ProbeFn:
    """The job body for one root: `kernel._probe(path, kind, errors)`. A job is shared by every waiter that joined it, so the
    per-corpus failures a waiter must see ride on the callable (`errors`: corpus_id -> exception), not on the job."""

    def __init__(self, kernel: FreshnessKernel, path: str, kind: str) -> None:
        self.kernel, self.path, self.kind = kernel, path, kind
        self.errors: dict[str, BaseException] = {}

    def __call__(self):
        return self.kernel._probe(self.path, self.kind, self.errors)


class _SeatFn:
    """The first full read of a root for a corpus being (re)seated: fills PRIVATE RootState objects nothing else can see yet."""

    def __init__(self, kernel: FreshnessKernel, path: str, states: list[RootState], wants: set) -> None:
        self.kernel, self.path, self.states, self.wants = kernel, path, states, wants

    def __call__(self):
        k = self.kernel
        for st in self.states:                      # an OSError here is the job's error; the seat re-raises it coded
            k._reload_root_full(st)
        obs, real = k._observe(self.path, self.wants)

        def commit() -> None:
            with k._lock:
                if k._path_in_use(self.path):
                    k._real[self.path] = real
                    if obs:
                        k._obs.setdefault(self.path, {}).update(obs)
        return commit


class KernelSealed(RuntimeError):
    """The kernel is sealed (registry-error mode): nothing is seated into it any more. Raised inside the step that would seat or publish a corpus."""


class FreshnessKernel:
    """Per-store incremental freshness. Seat once, then maintain via poll/backstop.

    Instrumentation: `hash_ops` counts every per-file CONTENT hash performed. The O(0)
    proof asserts this counter does NOT advance on the query path when the corpus is
    fresh (a query reads the cached root, never a file).
    """

    def __init__(self, *,
                 file_hasher: Callable[[str], str] = file_content_hash,
                 lister: Callable[[str], list[str]] = list_lts_files,
                 poll_interval: float = 1.0,
                 backstop_every: int = 30,
                 root_deadline_s: float | None = None,
                 pool_size: int | None = None) -> None:
        self._file_hasher = file_hasher
        self._lister = lister
        self.poll_interval = poll_interval
        # After `backstop_every` fast polls, one full re-verify runs (the missed-event
        # backstop). Bounds the worst-case stale window for a hint-invisible change.
        self.backstop_every = max(1, backstop_every)
        self._states: dict[str, dict[str, RootState]] = {}   # corpus_id -> {root_path: RootState}
        self._roots: dict[str, str] = {}                     # corpus_id -> maintained root
        # [MECH-BR-PROBE] guards ONLY these in-memory maps. NO file-system call is ever made while it is held.
        self._lock = threading.RLock()
        self._ticks = 0
        self.hash_ops = 0                                     # proof instrument
        self._ops_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._parts: dict[str, object] = {}                # corpus_id -> Partition (weak use)
        # [ROOT-STATE-ONE-REGISTRY] [MECH-BR-BOOT] x-library: the PROCESS's one registry, whose deadline is read once (LTS_ROOT_DEADLINE_S). A kernel
        # does not own one: root state is a fact about a disk. `root_deadline_s` / `pool_size` are the TEST seam: they start the process registry over
        # with those values (`rootstate.reset_process_registry`), so a test that needs another deadline says so here, in the one place.
        if root_deadline_s is None and pool_size is None:
            self.roots = process_registry()
        else:
            self.roots = reset_process_registry(root_deadline_s, **({} if pool_size is None else {"pool_size": pool_size}))
        self._declared: set = set()                          # root paths this kernel declared PENDING (and may give back)
        self._sealed = False                                 # registry-error mode: set by `seal()`, never cleared (the mode ends only by a restart); `seat_partition` refuses
        self._untrusted: set = set()                         # [MECH-BR-BOOT] x-load-error (UNTRUSTED ROOTS): path-like strings of entries that FAILED on a key; probed, never seated
        self._tick_hooks: list[Callable[[], None]] = []        # run at the start of each watcher cycle (the boot loader's retry of a stuck corpus)
        self._tl = threading.local()                          # verified_scope: when the current thread's prefetch began
        self._obs: dict[str, dict[str | None, bool]] = {}  # root path -> {only_file pin or None -> root (and pin) present}
        self._specs: dict[str, list[tuple[str, str | None]]] = {}   # corpus_id -> declared (root path, only_file) pairs
        # abs root path -> [(corpus_id, root_path as seated)] and -> the pinned files asked about it: rebuilt (under the lock, no disk) whenever
        # the seated or declared roots change, so a probe or a tick finds its targets without scanning every corpus (72 roots x 48 corpora)
        self._users: dict[str, list[tuple[str, str]]] = {}
        self._wanted: dict[str, set] = {}
        self._owed_full: set = set()                          # roots whose backstop pass was skipped because they were busy
        # [MECH-BR-WRITES] x-membership: root path -> the real path its LAST answered probe resolved. Written only by a probe that
        # answered (a stuck one publishes nothing), replaced by every later answer, so it is never older than the last answer: a
        # link swapped under a root is seen by the next poll. Read by membership and the ledger instead of resolving a root (the one resolve a write makes is the attach confirm, `boundedfs.confirm_real`).
        self._real: dict[str, str] = {}

    # --- hashing (the ONLY place a file's content is read) --------------------
    def _hash_file(self, abs_path: str, rel: str | None = None) -> str:
        with self._ops_lock:
            self.hash_ops += 1
        try:
            h = self._file_hasher(abs_path)
            _progress()
            return h
        except CorpusFileError as exc:                    # a refusal names the ROOT-RELATIVE path, never the absolute one
            if rel is not None:
                exc.set_rel(rel)
            raise
        except OSError:
            # [Opus Diamond P2-2] any other I/O error at hash time (EIO, a vanished file) is coded, so every caller of the kernel
            # (poll, full re-verify, the self-heal) fails CLOSED with a precise code instead of leaking a raw error
            raise CorpusFileError(CORPUS_FILE_UNREADABLE, rel if rel is not None else os.path.basename(abs_path)) from None

    def _hash_stable(self, abs_path: str, rel: str):
        """(content hash, stat hint) of a member, the hint taken BEFORE the hash and RE-CHECKED after it. [Opus Diamond P2-A] A
        write landing between the hash and a hint taken afterwards would be recorded as 'unchanged' and served old-as-fresh until
        the backstop; with the hint taken first it is detected by the next poll, and here the member is simply re-hashed. A member
        that never settles (three moves in a row) is UNVERIFIABLE: it raises a coded corpus-file-unreadable, so the root fails
        closed (STALE) instead of recording a hash that may not match the bytes."""
        for _ in range(3):
            before = self._safe_hint(abs_path)
            h = self._hash_file(abs_path, rel)
            if self._safe_hint(abs_path) == before:
                return h, before
        raise CorpusFileError(CORPUS_FILE_UNREADABLE, rel)

    @staticmethod
    def _stat_hint(abs_path: str) -> tuple[float, int, int, int, int]:
        st = os.stat(abs_path)
        # the permission bits ride in the hint: a `chmod` changes neither mtime nor size, and an unreadable member must be
        # noticed by the fast poll (not only by the periodic backstop) -- [DEC-LTS-NONREGULAR-FILE-IN-ROOT-...] F2
        # The inode and ctime ride too: no ordinary tool can set ctime (utime, `touch -r` and `cp -p` all move it to now)
        # and an atomic rename makes a new inode, so an edit that restores mtime and keeps the size is seen by the next poll
        # instead of waiting for the backstop (measured: ~30 s of confident stale answers before). The backstop stays, for
        # filesystems whose ctime cannot be trusted.
        return (st.st_mtime, st.st_size, st.st_mode, st.st_ino, st.st_ctime_ns)

    def _list(self, root_path: str) -> list[str]:
        out = self._lister(root_path)
        _progress()
        return out

    # --- slots ----------------------------------------------------------------
    def _slot(self, path: str):
        slot = self.roots.slot(path)
        if getattr(slot.recover, "_owner", None) is not self:
            # the latest kernel to ask owns the recovery of a stuck root (a process has one kernel; a test may make several in turn)
            def recover(p=path, c=self.roots.clock):
                return Job("full", _ProbeFn(self, p, "full"), clock=c)
            recover._owner = self
            slot.recover = recover
        return slot

    def seal(self) -> None:
        """Registry-error mode: from now on `seat_partition` refuses (`KernelSealed`) INSIDE its locked steps, and a seat already in flight is refused at its commit, where the kernel is left holding NOTHING for that corpus
        (fresh seat or re-seat). Called by the mode's one step (`clear_all(also=)`), after the engine seated the error and before the store is emptied, so a corpus seated before it is unseated by the caller and one seated after it is refused."""
        # A raise in the slot drop below, after the locked step, propagates with the kernel already sealed: a stated limit (SDLC `Injected-fault limits`).
        slots = self._untrusted_retirement()    # the computation comes FIRST and changes nothing; a raise here leaves the kernel exactly as it was ...
        with self._lock:
            self._untrusted = set()             # ... then, in ONE locked step, the untrusted strings are retired AND the seal is set ...
            self._sealed = True
        for p in slots:                         # ... and only then are the idle probe slots given back (`RootRegistry.drop`; a server in registry-error mode stops probing the strings)
            self.roots.drop(p)

    def declare_roots(self, paths) -> None:
        """[MECH-BR-BOOT] x-server: make a slot for each root a corpus that is not read yet will need, PENDING, before anything is read. Memory only.
        A write or a listing that arrives in the boot window then finds these roots pending instead of finding no root at all (which would read
        as 'outside every registered root')."""
        for p in dict.fromkeys(os.path.abspath(x) for x in paths):
            self._slot(p)
            self._declared.add(p)

    def declare_untrusted(self, paths) -> None:
        """[MECH-BR-BOOT] x-load-error (UNTRUSTED ROOTS): a corpus that FAILED on an unknown registry key has a location nobody can trust, so each path-like string of its
        entry gets a slot and ONE background probe on the process's own registry (the machinery a declared root has), which keeps the real path it
        resolves to. Memory only here; the probe runs on a pool worker and no writer waits on it. Never seated, never read, never hashed."""
        from factlattice.rootview import lexical
        new = []
        for p in dict.fromkeys(lexical(x) for x in paths):
            with self._lock:
                if p in self._untrusted:
                    continue
                self._untrusted.add(p)
            self._slot(p)
            new.append(p)
        for p in new:
            self._submit(p, "observe", foreground=False)

    def retain_untrusted(self, active_paths) -> None:
        """Give back the untrusted strings the registry no longer names as part of a failed entry (the entry was fixed, registered or unregistered): the slot
        is dropped and the watcher stops asking. A probe call still stuck on a hung disk stays counted by `stuck_workers` (`RootRegistry.drop` keeps a slot with a
        job in flight); that count is a truly stuck call, not a leftover."""
        from factlattice.rootview import lexical
        keep = {lexical(x) for x in active_paths}
        with self._lock:
            gone = [p for p in self._untrusted if p not in keep]
            for p in gone:
                self._untrusted.discard(p)
        for p in gone:
            if not self._path_in_use(p) and p not in self._declared:
                self.roots.drop(p)

    def _untrusted_retirement(self) -> list:
        """What retiring EVERY untrusted string would drop: the idle probe slots of the strings nothing else uses. Computes only; changes no state."""
        with self._lock:
            return [p for p in sorted(self._untrusted) if not self._path_in_use_excluding_untrusted(p) and p not in self._declared]

    def _path_in_use_excluding_untrusted(self, path: str) -> bool:
        with self._lock:
            return path in self._users or path in self._wanted

    def untrusted_paths(self) -> set:
        with self._lock:
            return set(self._untrusted)

    def release_roots(self, paths, needed) -> None:
        """Give back the PENDING slots a corpus declared and will not seat, unless another corpus (`needed`: root paths) or a seated one still uses
        them. A slot with a job in flight stays, still reporting its state (`RootRegistry.drop`)."""
        for p in paths:
            # only a slot THIS kernel declared: the registry is the process's, and another kernel's pending root is not ours to give back
            if p in self._declared and p not in needed and not self._path_in_use(p):
                self._declared.discard(p)
                self.roots.drop(p)

    def add_tick_hook(self, fn: Callable[[], None]) -> None:
        self._tick_hooks.append(fn)

    @staticmethod
    def _specs_of(part) -> list[tuple[str, str | None]]:
        ci = getattr(part, "corpus_index", None)
        return [(os.path.abspath(sp["path"]), sp.get("only_file") or None) for sp in (getattr(ci, "root_specs", None) or []) if isinstance(sp.get("path"), (str, os.PathLike))]      # a path that is not a string names no place (the release read it as a missing root); a falsy pin is no pin, as `live_roots` always read it

    def _paths_for(self, corpus_id: str) -> list[str]:
        """Every root path this corpus's verdict depends on: the seated ones and the declared ones (a declared root that is not
        seated yet is how a root that APPEARS is noticed)."""
        with self._lock:
            return list(dict.fromkeys([os.path.abspath(rp) for rp in self._states.get(corpus_id, {})]
                                      + [p for p, _of in self._specs.get(corpus_id, [])]))

    def _observe(self, path: str, wants) -> tuple[dict[str | None, bool], str]:
        """`(present, real)`: whether the root (and each pinned file asked for) is present, and where the root REALLY is (its path
        may pass through a link). DISK, on the root's own worker; both answers are kept and every reader of 'does the registered
        root set still match' or 'where is this root' reads the kept answer, never the disk ([MECH-BR-WRITES] x-membership)."""
        real = _realpath_progress(path)
        if not wants:
            return {}, real
        isdir = os.path.isdir(path)
        _progress()
        out: dict[str | None, bool] = {None: bool(isdir)}                    # the root itself, always
        for of in wants:
            if of is not None:
                out[of] = bool(isdir and os.path.isfile(os.path.join(path, of)))   # the root with its pinned file
                _progress()
        return out, real

    def _probe(self, path: str, kind: str, errors: dict[str, BaseException]):
        """One probe of one root, ON ITS WORKER: copy the state of every corpus that uses the root (a moment under the lock),
        do ALL the disk work on the copies with no lock held, and return the `commit` that publishes the results. The commit
        runs only if the probe did not overrun (rootstate), and a copy is published only if its RootState is still the
        seated one and unchanged since it was copied."""
        with self._lock:
            targets = [] if kind == "observe" else [
                (cid, st, st.ver, RootState(st.root_path, dict(st.leaves), dict(st.hints), st.fault))
                for cid, rp in self._users.get(path, ()) for st in (self._states[cid][rp],)]
            wants = set(self._wanted.get(path, ()))
        obs, real = self._observe(path, wants)
        outs = []
        for cid, st, ver, scratch in targets:
            before = dict(scratch.leaves)
            try:
                if kind == "full":
                    self._reload_root_full(scratch)
                    changed = scratch.leaves != before
                else:
                    changed = self._poll_root(scratch)
                outs.append((cid, st, ver, scratch, changed))
            except BaseException as exc:  # noqa: BLE001 — per corpus: one bad member does not hide the others' result
                errors[cid] = exc

        def commit() -> None:
            with self._lock:
                if self._path_in_use(path):                # a corpus unseated while this probe ran must not recreate what it dropped
                    self._real[path] = real
                    if obs:
                        self._obs.setdefault(path, {}).update(obs)
                moved = set()
                for cid, st, ver, scratch, changed in outs:
                    states = self._states.get(cid)
                    if states is None or states.get(st.root_path) is not st or st.ver != ver:
                        continue
                    st.leaves, st.hints, st.fault = scratch.leaves, scratch.hints, scratch.fault
                    st.ver += 1
                    if changed:
                        moved.add(cid)
                for cid in moved:
                    self._recompute_root(cid, self._states[cid])
        return commit

    def _reindex_locked(self) -> None:
        users: dict[str, list[tuple[str, str]]] = {}
        for cid, sts in self._states.items():
            for rp in sts:
                users.setdefault(os.path.abspath(rp), []).append((cid, rp))
        wanted: dict[str, set] = {}
        for sp in self._specs.values():
            for p, of in sp:
                wanted.setdefault(p, set()).add(of)
        self._users, self._wanted = users, wanted
        for gone in [p for p in self._real if p not in users and p not in wanted and p not in self._untrusted]:
            del self._real[gone]                              # a root nothing uses any more keeps no real path

    def _path_in_use(self, path: str) -> bool:
        with self._lock:
            return path in self._users or path in self._wanted or path in self._untrusted

    def _submit(self, path: str, kind: str, foreground: bool = True):
        """Foreground by default: every caller but the watcher's tick is a call waiting on the answer."""
        slot = self._slot(path)
        return slot, slot.submit(kind, _ProbeFn(self, path, kind), foreground=foreground)

    def _await(self, jobs, cid: str | None = None):
        """Wait for the jobs: each ends when it is done or its root has made no progress for the deadline (then the root is marked
        by that lack of progress, not by this wait). Waiting is concurrent: every job is already submitted. Returns the first error
        for corpus `cid`, if any."""
        first = None
        for slot, job in jobs:
            if not self.roots.wait(slot, job):
                continue
            errs = getattr(job.fn, "errors", None) or {}
            if cid is not None and cid in errs and first is None:
                first = errs[cid]
        return first

    def _sync(self, part, kind: str, reuse: bool = False) -> None:
        cid = part.corpus_id
        paths = self._paths_for(cid)
        if not paths:
            return
        since = getattr(self._tl, "since", None) if reuse else None
        jobs = []
        for p in paths:
            # a root already marked unresponsive is NOT asked again and NOT waited on: its stuck worker re-evaluates it when the call
            # returns, and the gate refuses its corpus from memory meanwhile
            if self.roots.state_of(p) == UNRESPONSIVE:
                continue
            if since is not None and self._slot(p).satisfied(kind, since):
                continue                       # a probe begun after the prefetch began has already answered this caller
            jobs.append(self._submit(p, kind))
        err = self._await(jobs, cid)
        if err is not None:
            raise err

    @contextlib.contextmanager
    def verified_scope(self, parts):
        """A corpus-omitted read gates many corpora one after another. Submit ONE probe per root of all of them AT ONCE, wait for
        them together (K hung roots then cost one deadline, not K), and let the gates that follow, inside this scope, reuse a probe
        that began after the scope did. The reuse rule is x-verify's: only a probe begun after this call began, so a write made
        before the call is in it. Only `verify` reuses; the self-heal's own confirming `poll_once` never does."""
        t0 = self.roots.clock()
        jobs = []
        seen = set()
        for part in parts:
            if part.corpus_id not in self._states:
                continue
            for p in self._paths_for(part.corpus_id):
                if p in seen or self.roots.state_of(p) == UNRESPONSIVE:
                    continue
                seen.add(p)
                jobs.append(self._submit(p, "poll"))
        self._await(jobs)
        prev = getattr(self._tl, "since", None)
        self._tl.since = t0
        try:
            yield
        finally:
            self._tl.since = prev

    def refresh_observations(self, part) -> None:
        """Ask each root's worker whether the registered roots are present NOW (a probe begun after this call), waiting until it
        answers or its root stops making progress. Nothing is read from a disk on this thread."""
        try:
            self._sync(part, "observe")
        except Exception:  # noqa: BLE001 — an observation that cannot be had leaves the last one; the gate refuses a marked corpus
            pass

    # --- reading the roots' state (all in memory) -------------------------------
    def root_mark(self, part) -> dict | None:
        """{status: pending|unresponsive, roots: count, since: ISO UTC} when a root this corpus depends on is marked, else None.
        Names no path. Touches no disk."""
        return self.roots.mark_for(self._paths_for(part.corpus_id))

    def root_mark_paths(self, paths) -> dict | None:
        """`root_mark` for a list of root paths not (yet) belonging to a seated corpus, such as a registration. Pure string work."""
        return self.roots.mark_for([os.path.abspath(p) for p in paths])

    def await_ready(self, part) -> None:
        """A call naming a corpus with a root not yet read waits for it, up to the root deadline ([MECH-BR-BOOT] x-server)."""
        for p in self._paths_for(part.corpus_id):
            slot = self.roots.slot(p, create=False)
            if slot is not None and slot.state() == PENDING:
                slot.wait_not_pending(self.roots.deadline_s)

    def stuck_workers(self) -> int:
        return self.roots.stuck_workers()

    def observe(self, path: str, only_file: str | None) -> bool | None:
        """The kept answer to 'is this registered root (and its pinned file) present', or None if never observed. No disk."""
        if not isinstance(path, (str, os.PathLike)):
            return None                                       # names no place (an int path): never observed, as the release read it
        ob = self._obs.get(os.path.abspath(path))
        return None if ob is None else ob.get(only_file)

    def root_real(self, path: str) -> str | None:
        """Where the root registered at `path` really was at its last answered probe, or None if it never answered. No disk."""
        with self._lock:
            return self._real.get(os.path.abspath(path))

    def root_facts(self):
        """`[RootFact]` for every root the kernel holds a slot for: its declared (absolute) path, the real path from its last
        answer, and its state (ok, pending, unresponsive). No disk: this is the reading a write takes at the start of a call."""
        from factlattice.rootview import RootFact
        out = []
        for p in self.roots.root_paths():
            slot = self.roots.slot(p, create=False)
            if slot is None:
                continue
            st = slot.state()
            with self._lock:
                real = self._real.get(p)
                untrusted = p in self._untrusted and p not in self._users and p not in self._wanted and p not in self._declared      # a root some corpus declared is a root
            out.append(RootFact(p, real, st, untrusted))
        return out

    def live_root_paths(self, part) -> set | None:
        """The declared roots that were present at their last probe, as absolute paths; None if any has never been observed.
        This replaces the query path's own isdir/isfile of every registered root ([ROOT-SET-CHANGE] keeps working: a root
        that appears or vanishes changes the next probe's answer)."""
        ci = getattr(part, "corpus_index", None)
        if ci is None or not hasattr(ci, "root_specs"):
            return None
        out = set()
        for sp in ci.root_specs:
            if not isinstance(sp.get("path"), (str, os.PathLike)):
                continue                                  # names no place: a missing root, as the release read it
            seen = self.observe(sp["path"], sp.get("only_file") or None)
            if seen is None:
                return None
            if seen:
                out.add(os.path.abspath(sp["path"]))
        return out

    # --- seating (startup / reconnect) ----------------------------------------
    def prepare_root(self, path: str, n_states: int, wants: set):
        """The seat's work for ONE root, done by the caller on that root's worker (the boot loader's fused job: read, hash, seat): `(states, commit)`. `states` are `n_states`
        private RootState objects, filled by a full read of the root; `commit` records the root's real path and observation and is called by `seat_partition` AFTER it has
        registered the corpus (the observation is kept only for a path in use). An OSError is the job's error, as in the seat job; the caller codes it. Takes no kernel lock."""
        states = [RootState(root_path=path) for _ in range(n_states)]
        return states, _SeatFn(self, path, states, wants)()

    def seat_partition(self, part) -> str | None:
        """One full re-hash to establish the leaf maps + root for a partition.

        `part._prepared_seat` ({abs root path: (states, commit)} from `prepare_root`, set by the boot loader): its fused job already read and hashed those roots, so no seat job is
        submitted for them; their states are used as they are, their `commit` is applied here once the corpus is registered, and each such root is SETTLED here (at the point its
        seat job used to end). A root with no entry in it (or whose count of states does not match this partition's) is seated by a seat job as before.

        A partition with no `root_paths` (e.g. a synthetic in-memory index) is NOT
        seated (returns None); its `freshness()` falls back to the fail-closed path.

        Each root's read runs on that root's own worker; this thread waits with NO kernel lock held, so a slow or stuck
        root can never block a poll of any other corpus. (The wait itself is not yet bounded: that is the boot slice.)
        """
        if not getattr(part, "root_paths", None):
            return None
        cid = part.corpus_id
        prepared = getattr(part, "_prepared_seat", None)
        part._prepared_seat = None
        with self._lock:
            if self._sealed:
                raise KernelSealed("registry-error mode")      # refused before any disk work
            was_seated = cid in self._states          # a RE-seat (the heal's structural change) keeps its old state on failure
            part.attach_kernel(self)
            self._parts[cid] = part
            old_specs = self._specs.get(cid)
            try:
                specs = self._specs_of(part)
                self._specs[cid] = specs
                self._reindex_locked()
            except BaseException:
                self._abandon_seat_locked(cid, part, was_seated, old_specs)       # the same one exit, for a fault before the roots are read
                raise
        states: dict[str, RootState] = {}
        try:
            by_path: dict[str, list[RootState]] = {}
            occurrences: dict[str, int] = {}
            for root_path in part.root_paths:
                occurrences[os.path.abspath(root_path)] = occurrences.get(os.path.abspath(root_path), 0) + 1
            fused = {p: prepared[p] for p in (prepared or {}) if len(prepared[p][0]) == occurrences.get(p, 0)}         # roots whose seat work is DONE
            handed = {p: list(fused[p][0]) for p in fused}
            for root_path in part.root_paths:
                p_abs = os.path.abspath(root_path)
                st = handed[p_abs].pop(0) if handed.get(p_abs) else RootState(root_path=root_path)
                states[root_path] = st
                by_path.setdefault(p_abs, []).append(st)
            wants: dict[str, set] = {}
            for p, of in specs:
                wants.setdefault(p, set()).add(of)
            for p in fused:
                fused[p][1]()                   # the root's observation, kept now that the corpus is registered (`_path_in_use`)
                fslot = self.roots.slot(p, create=False)
                if fslot is not None:
                    fslot.settle_ok()           # where the seat job used to end: the root reads ok, the corpus is published a moment later
            jobs = []
            for p in dict.fromkeys(list(by_path) + [p for p, _of in specs]):
                if p in fused:
                    continue
                slot = self._slot(p)
                jobs.append((slot, slot.submit("seat", _SeatFn(self, p, by_path.get(p, []), wants.get(p, set())),
                                               coalesce=False, keep_on_overrun=True, foreground=True)))
            first = None
            unanswered = False
            for slot, job in jobs:
                # [MECH-BR-BOOT] bounded: the wait ends when the job is done or its root has made no progress for the deadline (then the root is
                # marked by that silence). A seat that did not finish is not seated: the corpus is not served on a read nobody saw.
                self.roots.wait(slot, job)
                if not job.done.is_set():
                    unanswered = True
                elif job.error is not None and first is None:
                    first = job.error
            if unanswered:
                raise RootNotAnswering("a registered root did not answer while the corpus was being seated")
            if first is not None:
                raise first
        except (OSError, RootNotAnswering) as exc:
            # [Opus Diamond P1-A] a member that never settles, or turns unreadable, WHILE the corpus is being seated: leave
            # NOTHING half-seated (no kernel binding, no state) and raise ONE coded error. The caller contains it per
            # corpus (`seat_store` marks that corpus failed; `register_corpus` rolls the registration back).
            with self._lock:
                self._abandon_seat_locked(cid, part, was_seated, old_specs)
            if isinstance(exc, (CorpusFileError, RootNotAnswering)):
                raise
            raise CorpusFileError(CORPUS_FILE_UNREADABLE, ".") from None
        except BaseException:
            # EVERY other exception out of the seat's work, BaseException included (a job records a SystemExit or KeyboardInterrupt as its error and the seat re-raises it; an
            # in-process library caller that catches it survives it), takes the same exit and is re-raised as it is (when the exit completes): sealed, the kernel holds nothing for the corpus; unsealed,
            # a re-seat keeps its old state and a fresh seat leaves nothing. (An OSError is the exception to "as it is": the handler above turns it into CorpusFileError.)
            with self._lock:
                self._abandon_seat_locked(cid, part, was_seated, old_specs)
            raise
        with self._lock:
            if self._sealed:
                # the mode began while the roots were read: this seat is refused AT ITS COMMIT, in the step that would publish it. Once sealed the kernel holds NOTHING for this
                # corpus, whatever it held before (a fresh seat or a RE-seat alike): no part, state, root hash, spec, wanted pin, observation, and no probe slot of its own that
                # nothing else uses. The mode's unseat may already have run, and restoring the old specs after it would put them back (a re-seat's old specs).
                self._abandon_seat_locked(cid, part, was_seated, old_specs)
                raise KernelSealed("registry-error mode")
            try:
                root_hash = combine_roots({rp: st.root_hash() for rp, st in states.items()})       # computed FIRST: a fault here publishes nothing
                self._states[cid] = states
                self._reindex_locked()
                self._roots[cid] = root_hash
            except BaseException:                   # a fault anywhere in the publish step takes the one exit
                self._abandon_seat_locked(cid, part, was_seated, old_specs)
                raise
            return self._roots[cid]

    def _abandon_seat_locked(self, cid: str, part, was_seated: bool, old_specs) -> None:
        """THE ONE EXIT of a seat that does not publish (refused by the seal at its commit, failed on an unreadable member, or a root that did not answer). Caller holds the
        lock. Sealed: the kernel holds NOTHING for the corpus, whatever it held before (the mode's unseat may already have run; putting the old specs back would resurrect
        them, and `tick()` would probe a root of a server that serves nothing). Not sealed: a fresh seat leaves nothing, a RE-seat keeps its old state."""
        if self._sealed:
            part.attach_kernel(None)
            self.unseat_partition(cid)                  # the same forgetting as the unregister (the lock is re-entrant)
            return
        if not was_seated:
            self._parts.pop(cid, None)
            self._specs.pop(cid, None)
            part.attach_kernel(None)
        elif old_specs is not None:
            self._specs[cid] = old_specs
        self._reindex_locked()

    def seat_store(self, store) -> None:
        """Seat every partition in a store and attach the kernel to each. [Opus Diamond P1-A] A corpus that cannot be seated
        (`CorpusFileError`) is REMOVED from the store and recorded in `store.failed_corpora` with its code: one corpus fails, the
        server and the other corpora come up."""
        from .privacy import is_private_partition
        from .store import failed_record
        for part in list(store.partitions()):
            try:
                self.seat_partition(part)
            except CorpusFileError as exc:
                store.remove_partition(part.corpus_id, failed=failed_record(exc.code, exc.rel_path, is_private_partition(part), part.domain_suffix))
            except RootNotAnswering:
                # [MECH-BR-BOOT] x-library: a root that did not answer while seating is a corpus that is not served, with its own code and no path
                store.remove_partition(part.corpus_id, failed=failed_record(CODE_CORPUS_ROOT_UNRESPONSIVE, None, is_private_partition(part), part.domain_suffix))

    # --- unseating (runtime unregister, [REG-RUNTIME-CORPUS]) -----------------
    def unseat_partition(self, corpus_id: str, only_if: object = None) -> bool:
        """Drop a corpus's maintained leaves/root/watch-membership — the INVERSE of
        `seat_partition`. Returns True iff the corpus was tracked (a no-op unseat of an
        unseated/unknown id returns False and never raises).

        After this the kernel no longer polls or backstops this corpus_id (`tick()`
        iterates `_parts`, which no longer carries it), and `current_root()` reverts to
        returning None for it — exactly the "never seated" state, so a caller cannot
        tell an unseat apart from a corpus that was never registered, which is the point:
        there must be no residual half-tracked state for a runtime-unregistered corpus.
        """
        with self._lock:
            if only_if is not None and self._parts.get(corpus_id) is not only_if:
                return False               # a newer registration of this id owns the state now: not ours to drop
            tracked = corpus_id in self._states or corpus_id in self._roots \
                or corpus_id in self._parts
            mine = set(os.path.abspath(rp) for rp in self._states.get(corpus_id, {})) \
                | {p for p, _of in self._specs.get(corpus_id, [])}
            self._states.pop(corpus_id, None)
            self._roots.pop(corpus_id, None)
            self._parts.pop(corpus_id, None)
            self._specs.pop(corpus_id, None)
            self._reindex_locked()
            still = set(self._users) | set(self._wanted) | set(self._untrusted)       # a string the registry names as part of a failed entry keeps its probe slot
            for p in mine - still:
                self._obs.pop(p, None)
                self.roots.drop(p)
            return tracked

    def _reload_root_full(self, st: RootState) -> None:
        """Re-hash EVERY leaf under a root unconditionally (content oracle)."""
        leaves: dict[str, str] = {}
        hints: dict[str, tuple[float, int, int, int, int]] = {}
        for rel in self._list(st.root_path):
            ap = os.path.join(st.root_path, rel)
            leaves[rel], hints[rel] = self._hash_stable(ap, rel)       # hint BEFORE the hash, re-checked after (P2-A)
        st.leaves = leaves
        st.hints = hints
        st.fault = None                                  # a full re-hash SUCCEEDED: any earlier fault is obsolete

    # --- the O(1) query-path read (NO hashing) --------------------------------
    def current_root(self, part) -> str | None:
        """The maintained root for a partition, or None if unseated. O(1), no I/O.

        DELIBERATELY LOCK-FREE: this is THE query-path read, so it must not contend with
        the watcher thread (which holds `_lock` while re-hashing). A single dict `.get`
        is atomic under the GIL and `_recompute_root` REBINDS the entry in one statement,
        so a concurrent reader sees either the previous root or the new one — never a
        torn value. Both are self-consistent content hashes, so the freshness verdict is
        correct either way; a change observed a few microseconds later is exactly the
        bounded-latency behaviour the watcher already implies.
        """
        return self._roots.get(getattr(part, "corpus_id", None))

    # --- [LEC-SELF-HEAL] the leaf map, as the re-index's change oracle ---------
    def leaves_snapshot(self, part) -> dict[str, dict[str, str]] | None:
        """{root_path: {rel: content_hash}} for a seated partition, or None.

        This is the SAME content-hash leaf map the freshness verdict is derived from, so
        the self-heal re-index and the freshness gate can never disagree about WHICH
        files changed. Returned as a copy taken under the lock (the watcher mutates the
        live maps); the caller diffs it against the parse cache's own certified leaves.
        """
        with self._lock:
            states = self._states.get(getattr(part, "corpus_id", None))
            if states is None:
                return None
            return {rp: dict(st.leaves) for rp, st in states.items()}

    def tracked_roots(self, part) -> list[str] | None:
        """The root paths this kernel is maintaining for a partition (or None)."""
        with self._lock:
            states = self._states.get(getattr(part, "corpus_id", None))
            return list(states) if states is not None else None

    def rebind_partition(self, part) -> None:
        """Point the kernel's per-corpus state at `part` without re-hashing.

        Used after a self-heal that rebuilt a partition's CONTENT in place: the leaves and
        root are already authoritative for that content, so re-seating (a full re-hash)
        would be pure waste. Keeps the O(0) query path intact after a heal.
        """
        with self._lock:
            if part.corpus_id in self._states:
                self._parts[part.corpus_id] = part
                part.attach_kernel(self)

    # --- incremental update (fast path — mtime/size HINT) ---------------------
    def poll_once(self, part) -> bool:
        """Re-hash ONLY leaves whose (mtime,size) hint changed / were added / removed.

        Returns True iff the maintained root actually changed. Unchanged files are
        stat()'d (cheap) but never re-read. A content-preserving `touch` re-hashes the
        one leaf, finds the SAME content hash, and does NOT flip the root (no false-stale).

        [MECH-BR-PROBE] x-verify: this WAITS for a probe BEGUN AFTER THIS CALL on each of the corpus's roots, on that root's own
        worker, for at most the root deadline. A probe already running began before the call, possibly before a write the
        caller just made, so it is never joined; a probe still queued is. A root that does not answer is marked and the call
        returns (False for it) -- the gate refuses a marked corpus, so a stale root is never read as a fresh one.
        """
        return self._poll(part, reuse=False)

    def verify(self, part) -> bool:
        """`poll_once` for the freshness PROVIDER: the same wait for a probe begun after the call, except that inside a
        `verified_scope` a probe begun after the scope did already counts (it was begun after the read began)."""
        return self._poll(part, reuse=True)

    def _poll(self, part, reuse: bool) -> bool:
        cid = part.corpus_id
        if cid not in self._states:
            return False
        old = self._roots.get(cid)
        self._sync(part, "poll", reuse=reuse)
        return self._roots.get(cid) != old

    def _poll_root(self, st: RootState) -> bool:
        try:
            current = set(self._list(st.root_path))
        except OSError:
            # An inaccessible root is UNVERIFIABLE. Force the leaves empty so the root
            # diverges from the index hash -> the freshness gate fails closed (STALE),
            # never silently fresh.
            if st.leaves:
                st.leaves.clear()
                st.hints.clear()
                return True
            return False
        if st.fault is not None:
            # A member is a corpus-file-* failure and the leaves were EMPTIED (the root diverges: STALE, never fresh).
            # [Opus Diamond F1] The fault must NEVER outlive its cause, or later edits of OTHER members would be hidden: it is
            # cleared (a) when the leaves were repopulated by a successful full re-hash (self-heal / backstop), (b) when the
            # bad member left the listing, (c) when a hash of the bad member now SUCCEEDS (the cause can leave mtime, size
            # and mode untouched: an ACL, a policy layer). While it stands the leaves stay empty, so the root stays STALE;
            # this probe hashes ONE file per tick, not the whole corpus.
            _code, bad_rel, _bad_hint = st.fault
            if st.leaves or bad_rel not in current:
                st.fault = None
            else:
                try:
                    self._hash_file(os.path.join(st.root_path, bad_rel), bad_rel)
                except OSError:
                    return False                             # still unreadable: leaves stay EMPTY, the root stays STALE
                st.fault = None
        prev = set(st.leaves)
        changed = False
        cur = ""
        try:
            for rel in sorted(current - prev):               # added file
                cur = rel
                ap = os.path.join(st.root_path, rel)
                st.leaves[rel], st.hints[rel] = self._hash_stable(ap, rel)      # hint BEFORE the hash (P2-A)
                changed = True
            for rel in prev - current:                       # removed file
                st.leaves.pop(rel, None)
                st.hints.pop(rel, None)
                changed = True
            for rel in sorted(current & prev):               # possibly modified
                cur = rel
                ap = os.path.join(st.root_path, rel)
                hint = self._safe_hint(ap)
                if st.hints.get(rel) == hint:
                    continue                                 # hint says unchanged -> skip re-read
                new_hash, hint = self._hash_stable(ap, rel)  # CONTENT is the verdict; hint taken before, re-checked after
                st.hints[rel] = hint                         # (recorded only once the hash succeeded)
                if new_hash != st.leaves[rel]:
                    st.leaves[rel] = new_hash
                    changed = True
        except OSError as exc:
            # [DEC-LTS-NONREGULAR-FILE-IN-ROOT-HANGS-OR-MISREPORTS] a member that turned unreadable (or a new one that is) is
            # an UNVERIFIABLE root, exactly like an unlistable one: empty the leaves so the root diverges from the index hash
            # and the gate fails closed (STALE, never fresh over old content). A raw OSError that is not already coded (EIO,
            # a vanished file) is the same: [Opus Diamond P2-2] fail CLOSED as `corpus-file-unreadable`.
            code = exc.code if isinstance(exc, CorpusFileError) else CORPUS_FILE_UNREADABLE
            rel_p = exc.rel_path if isinstance(exc, CorpusFileError) and exc.rel_path in current else cur
            st.fault = (code, rel_p, self._safe_hint(os.path.join(st.root_path, rel_p)) if rel_p else (0.0, -1, 0, 0, 0))
            st.leaves.clear()
            st.hints.clear()
            return True
        return changed

    def _safe_hint(self, abs_path: str) -> tuple[float, int, int, int, int]:
        try:
            return self._stat_hint(abs_path)
        except OSError:
            return (0.0, -1, 0, 0, 0)
        finally:
            _progress()

    # --- the backstop (missed / hint-invisible change) ------------------------
    def full_reverify(self, part) -> bool:
        """Re-hash EVERY leaf unconditionally (content oracle) and recompute the root.

        The bounded periodic backstop: catches any change the hint-based `poll_once`
        could miss. Since the hint carries the inode and ctime, that is a change on a
        filesystem whose ctime cannot be trusted, or one that lands within the ctime
        granularity of the previous hint (`touch -r` and `cp -p` are now seen by the poll).
        Returns True iff the maintained root changed as a result. A member that cannot be read RAISES (the self-heal fails
        closed on it), exactly as before.
        """
        cid = part.corpus_id
        if cid not in self._states:
            return False
        old = self._roots.get(cid)
        self._sync(part, "full")
        return self._roots.get(cid) != old

    def _recompute_root(self, corpus_id: str, states: dict[str, RootState]) -> None:
        self._roots[corpus_id] = combine_roots(
            {rp: st.root_hash() for rp, st in states.items()})

    def _mark_unverifiable(self, part) -> None:
        """A full re-verify could not read the corpus: empty its leaves so the maintained root diverges (STALE, fail closed)."""
        with self._lock:
            states = self._states.get(part.corpus_id)
            if not states:
                return
            for st in states.values():
                st.leaves.clear()
                st.hints.clear()
                st.ver += 1                              # a probe copied before this must not publish over it
            self._recompute_root(part.corpus_id, states)

    # --- the background watcher loop ------------------------------------------
    def tick(self) -> None:
        """One watcher cycle: ask every root's worker for a fast poll, plus a full re-verify of each every `backstop_every`
        ticks (the missed-event backstop), and WAIT for what it asked for (concurrently, at most the root deadline).

        A root whose worker is still busy is SKIPPED, never queued behind: so a hung root is asked once, however many ticks
        pass ([MECH-BR-PROBE] x-no-pile-up). A backstop pass skipped for that reason is owed and runs as soon as the worker is
        free. ONE bad corpus never stops the others (each is its own job and its own error)."""
        for hook in list(self._tick_hooks):
            try:
                hook()                      # a hook never waits on a disk (it hands work to its own thread) and never stops the cycle
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._ticks += 1
            if self._ticks % self.backstop_every == 0:
                self._owed_full |= set(self._users)
            paths = list(dict.fromkeys(list(self._users) + list(self._wanted) + sorted(self._untrusted)))
            owed = set(self._owed_full)
        jobs = []
        for p in paths:
            slot = self._slot(p)
            if slot.busy():
                continue
            kind = "full" if p in owed else "poll"
            if kind == "full":
                with self._lock:
                    self._owed_full.discard(p)
            jobs.append((p, kind) + self._submit(p, kind, foreground=False))      # the watcher's own work is background
        self._await([(slot, job) for _p, _k, slot, job in jobs])
        for p, kind, _slot_, job in jobs:
            if kind == "full" and job.done.is_set() and not job.overran:
                for cid in list(getattr(job.fn, "errors", None) or {}):
                    part = self._parts.get(cid)
                    if part is not None:             # the leaves are left empty: the NEXT heal re-reads and reports it
                        self._mark_unverifiable(part)

    def start(self) -> None:
        """Start the daemon polling watcher (deployment path). Idempotent."""
        if self._thread is not None:
            return
        self._stop.clear()

        def _run() -> None:
            while not self._stop.wait(self.poll_interval):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 — a watcher fault must not kill the server
                    pass

        self._thread = threading.Thread(target=_run, name="lts-freshness-watch",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=self.poll_interval * 2)
        self._thread = None
