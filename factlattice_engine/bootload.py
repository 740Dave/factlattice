"""[MECH-BR-BOOT] the bounded read of a registry's corpora: `config.build_store` and the server's boot are this one loader.

WHAT IT REPLACES. `config.build_store` read every root on the calling thread with no limit, and the server did that before it read a byte of
stdin, so one hung root stopped the handshake (spec 38 x-server) and a library caller (a consumer that renders agent definitions, a saved decision
answer) hung with it (x-library). Here the DISK half of a corpus's build (is the root there, list it, hash and parse each file: `CorpusIndex.read_root`)
is a job on that root's own worker in the PROCESS's one registry (`rootstate.process_registry`), under the same no-progress deadline as a probe. The
caller of a job waits for it with `RootRegistry.wait`, which ends when the job is done or its root has made no progress for the deadline.

TWO MODES, ONE CODE PATH.
  * blocking (`run_blocking`, `build_store`): the corpora are read by up to LOADER_WORKERS threads, so K hung roots cost one root deadline for K up to LOADER_WORKERS and ceil(K / LOADER_WORKERS) root deadlines from four stuck reads on, not K.
    A corpus whose root does not answer goes into `store.failed_corpora` with code `corpus-root-unresponsive` and NO path. The caller gets its
    store back inside about one deadline while three or fewer reads are stuck, ceil(K/4) deadlines from four on. No kernel: nothing is seated.
  * server (`start`, with a kernel): the store is returned at once with every corpus PENDING (`store.pending_corpora`) and the loader reads them
    in the background; each leaves `pending_corpora` in ONE step (`LtsStore.settle`) when its read ends. A corpus whose root did not answer stays
    pending with `stuck` set, its root reads UNRESPONSIVE, and the kernel's watcher cycle (`retry_stuck`) loads it again once the stuck call has
    returned and the root is back to OK: no restart.

THE CAP. A process holds at most one stuck worker per root (one running job per slot, ever) and MAX_STUCK_WORKERS in all. `RootRegistry.admit`
refuses a read of a root that already has a stuck worker, or past the cap, BEFORE any job is queued: a caller that builds again and again during a
stall adds no thread and no queued job. Its transient overshoot is bounded by what this loader has in flight (LOADER_WORKERS corpora at a time).

THE PROFILE. A corpus's registered profile file may live under a root that hangs. It is read like a root read (`_bounded_profile`: a job on the
process registry, keyed by the profile's own path), so a hung one fails ITS corpus with `corpus-root-unresponsive` and holds no loader thread. The
blocking caller's wait for the loader threads is bounded too (`_join_bounded`: one root deadline after the last sign of life), so no unforeseen call
of the loader can keep `build_store` past it.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

import factlattice.hashing as _hashing
from factlattice import CorpusFileError
from factlattice.hashing import CORPUS_FILE_UNREADABLE
from factlattice.profile import ProfileCollisionError
from factlattice.rootview import lexical as _lexical

from . import config
from .incremental import CorpusIndex
from .privacy import private_reason
from .rootstate import (
    CODE_CORPUS_LOAD_ERROR,
    CODE_CORPUS_ROOT_UNRESPONSIVE,
    GROW_AFTER_S,
    UNRESPONSIVE,
    Job,
    ProfileNotAnswering,
    RootNotAnswering,
    RootRegistry,
    process_registry,
)
from .rootstate import progress as _progress
from .store import LtsStore, Partition, failed_record

logger = logging.getLogger(__name__)

# the hashing and listing report progress to the deadline of the job running them (also set by kernel.py; either import suffices)
_hashing.progress_hook = _progress

#: corpora read at once. A stuck read holds its loader thread for one deadline and no longer, so K hung corpora cost ceil(K / LOADER_WORKERS)
#: deadlines at most for the healthy ones queued behind them; a root already known stuck is skipped without waiting at all.
LOADER_WORKERS = 4        # the MOST loaders. The blocking loader starts this many; the server's (adaptive) starts one and grows to it, one at a time, when a read runs long (GROW_AFTER_S)

#: the least silence `_join_bounded` tolerates, whatever the root deadline: a thread of a healthy build that a loaded machine starves for a moment is slow, not hung
JOIN_FLOOR_S = 1.0


def _generic_recover(reg: RootRegistry) -> Callable[[], Job]:
    """A root with no kernel to re-read it: the stuck call returning IS the answer. The slot goes back to OK, so the next build proceeds."""
    def recover() -> Job:
        return Job("full", lambda: None, clock=reg.clock)
    recover._owner = None
    return recover


class EntryShapeError(Exception):
    """Raised by `_read` for an exception that came from the SHAPE of an entry (its cause is the original); the loader treats it as the registry's, not the corpus's."""


class StoreLoader:
    def __init__(self, store: LtsStore, corpora: dict[str, dict], *, profile=None, kernel=None, reg: RootRegistry | None = None,
                 workers: int = LOADER_WORKERS, retry: bool = False, adaptive: bool = False,
                 on_first_pass: Callable[[StoreLoader], None] | None = None,
                 on_late_fatal: Callable[[StoreLoader], None] | None = None) -> None:
        self.store = store
        self.corpora = corpora
        self.profile = profile if profile is not None else config.engine_profile_config()
        self.kernel = kernel
        self.reg = reg or (kernel.roots if kernel is not None else process_registry())
        self.workers = max(1, workers)
        self.retry = retry                              # a corpus whose root did not answer stays pending and is loaded again when the root is back
        #: the SERVER's first pass starts with ONE loader and adds one (up to `workers`) only when a corpus read has been running without finishing for GROW_AFTER_S, the pool's own
        #: rule; the blocking `build_store` keeps `workers` from the start. The bound is the one at LOADER_WORKERS above and is unchanged by this rule: K stuck reads hold the healthy
        #: corpora about GROW_AFTER_S each while K < `workers`, and ceil(K / `workers`) root deadlines from then on.
        self.adaptive = adaptive
        self._taken_at: dict[str, float] = {}           # cid -> the registry clock when a loader took it, while it is in flight
        self._grown_for: set[str] = set()               # corpora whose long read has already added a loader
        self._nworkers = 0
        self._grow_stop = threading.Event()             # set when the first pass has nothing left to grow for
        self._prep: dict[str, dict] = {}                # cid -> {root path: (states, commit)}: each root's seat work, done by its one fused job and handed to `seat_partition`
        self.on_first_pass = on_first_pass
        self.on_late_fatal = on_late_fatal              # a server's: an entry-shape verdict met AFTER the first pass began (a retry load) enters registry-error mode at once
        self.first_pass_error: str | None = None      # the CLASS NAME of an exception the first-pass hook raised (it seats registry-error mode): set once, never cleared
        self._fatal_pending: dict[str, list[str]] = {}   # cid -> its root paths, for a corpus whose read yielded the registry-error verdict: it STAYS PENDING until the one step that seats the error
        self.fatal: BaseException | None = None      # an exception raised by the SHAPE of an entry on a loader thread: the registry could not be loaded (as in af4cfc2)
        self.first_pass_done = threading.Event()
        self._first_pass_started = False
        self._lock = threading.Lock()
        self._queue: list[str] = []
        self._active = 0
        self._inflight: set[str] = set()
        self._stuck: set[str] = set()
        self._retry_failed: dict[str, str] = {}         # cid -> profile key: corpora failed because their PROFILE did not answer, read again when it does
        self._threads: list[threading.Thread] = []
        self._started = False
        self._activity = 0.0                            # the loader's last sign of life (a corpus taken or ended), on the registry's clock
        self._registry_order = list(corpora)

    # ---- no disk -----------------------------------------------------------------------------------------------------------------------
    def declare(self) -> None:
        """Memory only. Every corpus not refused by its registry entry becomes PENDING in the store, and (with a kernel) each root it names gets
        its PENDING slot, so a write or a listing in the boot window finds these roots instead of finding none. A malformed entry raises here,
        as it always did in `build_store`."""
        self.store.set_order(self._registry_order)
        all_paths: list[str] = []
        for cid, spec in self.corpora.items():
            key_failure = config.registry_key_failure(cid, spec)
            if key_failure is not None:           # an unknown registry key fails THIS corpus only, exactly like an unreadable member
                self.store.fail(cid, key_failure["code"], key_failure.get("path"), key_failure.get("private", False),
                                spec.get("domain_suffix", f"proj.{cid}") if isinstance(spec, dict) else None,
                                **{k: v for k, v in key_failure.items() if k not in ("code", "path", "private")})
                if config.unstated_location(spec):
                    self.store.add_unlocated(cid)         # an entry the release does not serve and whose roots cannot be read as paths: every write lands flagged
                if self.kernel is not None and isinstance(spec, dict):
                    self.kernel.declare_untrusted(config.untrusted_path_strings(spec))      # probed off every thread a writer is on; keeps each string's real path
                continue
            self._shape_phase(cid, spec, None)             # deliberately NOT defensive: every registry-error verdict that needs no disk is reached HERE, before any corpus is served ([MECH-BR-BOOT] x-boot-window)
            paths = list(dict.fromkeys(os.path.abspath(r["path"]) for r in config.declared_root_entries(spec)
                                       if isinstance(r.get("path"), str)))
            self.store.declare_pending(cid, {"spec": spec, "private": private_reason(cid, spec) is not None, "paths": paths,
                                             "domain_suffix": spec.get("domain_suffix", f"proj.{cid}"), "since": time.time(), "stuck": False})
            all_paths += paths
            self._queue.append(cid)
        if self.kernel is not None:
            self.kernel.declare_roots(all_paths)

    # ---- running ------------------------------------------------------------------------------------------------------------------------
    def start(self) -> None:
        """Read the declared corpora in the background and return at once."""
        with self._lock:
            if self._started:
                return
            self._started = True
            todo = list(self._queue)
            n = min(1 if self.adaptive else self.workers, len(todo))
            self._nworkers = n
        if self.kernel is not None and self.retry:
            self.kernel.add_tick_hook(self.retry_stuck)
        if not todo:
            self._first_pass_over()
            return
        for i in range(n):
            t = threading.Thread(target=self._worker, name=f"lts-boot-load-{i}", daemon=True)
            self._threads.append(t)
            t.start()
        if self.adaptive and self.workers > n:
            threading.Thread(target=self._grow_monitor, name="lts-boot-grow", daemon=True).start()

    def _grow_monitor(self) -> None:
        """ADAPTIVE LOADER: add a loader (up to `workers`) when a corpus read has been running without finishing for GROW_AFTER_S. It wakes only to look, never per corpus:
        it sleeps until the soonest moment an in-flight read would reach GROW_AFTER_S, so a healthy boot (reads of a few ms) adds nothing and a hung read adds one loader
        about GROW_AFTER_S after it began, and the healthy corpora behind it proceed. It ends with the first pass."""
        wait = GROW_AFTER_S
        while not self._grow_stop.wait(wait):
            spawn = 0
            with self._lock:
                if self._first_pass_started or not self._queue:
                    return
                now = self.reg.clock()
                wait = GROW_AFTER_S / 2
                for cid, t0 in self._taken_at.items():
                    age = now - t0
                    if age >= GROW_AFTER_S:
                        if cid not in self._grown_for and self._nworkers < self.workers:
                            self._grown_for.add(cid)
                            self._nworkers += 1
                            spawn += 1
                    else:
                        wait = min(wait, max(0.001, GROW_AFTER_S - age))
            for _ in range(spawn):
                t = threading.Thread(target=self._worker, name="lts-boot-load-grown", daemon=True)
                self._threads.append(t)
                t.start()

    def run_blocking(self) -> None:
        """Read the declared corpora and return when each has ended, read or refused. At most about one root deadline per hung root, since every
        root is read at once. An exception from the SHAPE of an entry (`EntryShapeError`: what release af4cfc2's build raised) is re-raised here, after every corpus has ended; any other unexpected error in one corpus's load fails that corpus (`corpus-load-error`) and is never raised here. The wait for the loader threads is BOUNDED:
        see `_join_bounded` (no call of the loader may keep a caller past the root deadline of silence)."""
        with self._lock:
            self._started = True
            todo = list(self._queue)
            self._queue = list(todo)
            self._activity = self.reg.clock()
        try:
            if todo:
                ts = [threading.Thread(target=self._worker, name=f"lts-build-load-{i}", daemon=True) for i in range(min(self.workers, len(todo)))]
                for t in ts:
                    t.start()
                self._join_bounded(ts)
        finally:
            if self.kernel is None:
                self.reg.trim_idle()                  # a library caller keeps no idle pool thread behind it: only a stuck worker may remain
        if self.fatal is not None:
            raise self.fatal                          # an entry's SHAPE met on a loader thread: the registry could not be loaded, as `build_store` always raised it

    def _join_bounded(self, ts: list[threading.Thread]) -> None:
        """Wait for the loader threads, but never past the root deadline (and never less than JOIN_FLOOR_S) of SILENCE. Signs of life: the loader taking or
        ending a corpus, each root job ending, the merge (it reports every BEAT_EVERY items), and the progress of any job the registry runs. A big
        healthy corpus keeps giving them, so slow is not read as hung; a thread stopped in a call nobody bounded gives none, and is given up on one
        deadline after the last. A CPU-starved healthy thread is covered by the floor, not by a promise that it never pauses. Every corpus not settled
        by then is failed `corpus-root-unresponsive`; the abandoned thread is a daemon and its late result is discarded (`settle` refuses a corpus no
        longer pending)."""
        reg = self.reg
        while any(t.is_alive() for t in ts):
            for t in ts:
                t.join(0.05)
            if reg.clock() - max(self._activity, reg.last_progress()) > max(reg.deadline_s, JOIN_FLOOR_S) and any(t.is_alive() for t in ts):
                with self._lock:
                    self._queue = []
                with self._lock:
                    fatal_cids = set(self._fatal_pending)
                    pending_now = dict(self.store.tables()[0])           # one snapshot of the pending table, taken with the verdict record
                for cid, rec in pending_now.items():
                    with self._lock:
                        if cid in fatal_cids or cid in self._fatal_pending:
                            continue                      # its verdict is the registry's: it stays pending until the mode's step, never failed `corpus-root-unresponsive`
                    self._failed(cid, rec, CODE_CORPUS_ROOT_UNRESPONSIVE, None)
                return

    def _alive(self) -> None:
        self._activity = self.reg.clock()

    def _take(self) -> str | None:
        with self._lock:
            if self._queue:
                cid = self._queue.pop(0)
                self._active += 1
                self._activity = self._taken_at[cid] = self.reg.clock()
                return cid
            return None

    def _worker(self) -> None:
        while True:
            cid = self._take()
            if cid is None:
                break
            try:
                self._load_one(cid)
            finally:
                with self._lock:
                    self._active -= 1
                    self._taken_at.pop(cid, None)
                    self._activity = self.reg.clock()
        with self._lock:
            last = self._active == 0 and not self._queue
        if last:
            self._grow_stop.set()
            self._first_pass_over()

    def _first_pass_over(self) -> None:
        with self._lock:
            if self._first_pass_started:
                return
            self._first_pass_started = True
        try:
            if self.on_first_pass is not None:
                try:
                    self.on_first_pass(self)
                except Exception as exc:  # noqa: BLE001 — a hook's failure must not strand the loader, and must not be SILENT: the registry-error step runs here, and
                    # a raise leaves the store as it was (served) when the mode should have begun. The class name only, never the message (it may carry a path).
                    self.first_pass_error = type(exc).__name__          # `lts_list_corpora` reports it (`first_pass_error`), so the server does not look healthy to its agent
                    logger.error("[lts-engine] the first-pass hook raised %s; the registry-error verdict may not have been reached", type(exc).__name__)
        finally:
            self.first_pass_done.set()           # AFTER the hook: whoever waits for the first pass sees what the hook decided

    # ---- one corpus ---------------------------------------------------------------------------------------------------------------------
    def _slot(self, path: str):
        # With a kernel the root is a REGISTERED root (its slot is listed, its state reported). Without one (a library caller) the read is a call's own:
        # an AUXILIARY slot, which counts as stuck while it is, is never listed as a registered root, and goes when it is idle and answering.
        slot = self.kernel._slot(path) if self.kernel is not None else self.reg.slot(path, aux=True)
        if slot.recover is None:
            slot.recover = _generic_recover(self.reg)
        return slot

    def _load_one(self, cid: str) -> None:
        rec = self.store.lookup_all(cid)[1]
        if rec is None:                                   # unregistered while it waited
            return
        with self._lock:
            if cid in self._inflight:
                return
            self._inflight.add(cid)
        try:
            self._load_locked(cid, rec)
        except EntryShapeError as exc:
            # NO ENTRY SHAPE'S BOOT VERDICT CHANGES from release af4cfc2 (spec 38 x-load-error, third amendment): an exception raised while the loader reads the
            # SHAPE of an entry (before any root job is submitted) is what `build_store` raised there: the registry cannot be loaded. It is not a load error.
            # THE CORPUS STAYS PENDING (spec 38 x-boot-window, ruled 2026-11-07 after the read of 5f4b595, P1-1): there is no intermediate drop. It leaves the pending table in the
            # ONE locked store step that seats the error, seals and empties (`clear_all(also=)`), so a reader never finds it in no table: until then the listing shows it pending
            # and a write under its root meets the pending rule. Its root slots are given back after that step (`_release_fatal`).
            # The verdict and the record that this corpus waits for it are ONE step under the loader's lock (`_join_bounded` reads both under it). The root ANSWERED, so its slot is
            # given back at once (it must not read pending, and after a deadline unresponsive, for a root that is fine); the corpus is marked as held for the verdict in the store.
            # A call naming it is answered EXACTLY as for any pending corpus: it waits up to one root deadline for the mode's step, then meets the mode's rules or is refused
            # `corpus-verdict-pending` (`membership.await_pending`, `tools._pending_refusal`), which states the true cause.
            with self._lock:
                self.fatal = self.fatal or exc.__cause__ or exc
                self._fatal_pending[cid] = list(rec["paths"])
            self.store.hold_for_verdict(cid)
            self._release(rec["paths"], cid)
            self._late_fatal()
        except Exception as exc:  # noqa: BLE001 — [MECH-BR-BOOT] x-load-error: an unexpected error while loading ONE corpus fails that corpus only
            # (one store step, pending -> failed), on the first pass and after it; the class name rides in the record, never the message or a path
            self._failed(cid, rec, CODE_CORPUS_LOAD_ERROR, None, error=type(exc).__name__)
            if self.store.lookup_all(cid)[0] is not None:
                # it is SERVED already (a stale pending record the seat's own check keeps out): `settle` refused the move to failed and dropped the stale record.
                # It stays served, because failing a corpus that is answering would take it away; the failure is RECORDED here (class name only, no id, no message).
                logger.warning("[lts-engine] a corpus that is already served had a pending load that raised %s; it stays served", type(exc).__name__)
        finally:
            with self._lock:
                self._inflight.discard(cid)

    def _late_fatal(self) -> None:
        """An entry-shape verdict met once the first pass had begun (a corpus whose root did not answer at boot and is read on a retry, so its disk-needing shape
        error shows only now): nobody reads `fatal` after the first pass, so without this its corpus would be in no table. The server's hook enters registry-error
        mode through the same step the first pass uses (spec 38 x-boot-window). A raise is recorded as the first-pass hook's is (class name only)."""
        if self.on_late_fatal is None:
            return
        with self._lock:
            late = self._first_pass_started
        if not late:
            return                                    # the first pass will read `fatal` itself
        try:
            self.on_late_fatal(self)
        except Exception as exc:  # noqa: BLE001
            self.first_pass_error = self.first_pass_error or type(exc).__name__
            logger.error("[lts-engine] the late registry-error hook raised %s; the registry-error verdict may not have been reached", type(exc).__name__)

    def _failed(self, cid: str, rec: dict, code: str, path: str | None, **extra) -> None:
        self.store.settle(cid, failed=failed_record(code, path, rec["private"], rec.get("domain_suffix"), **extra))
        self._release(rec["paths"], cid)

    def _stuck_now(self, cid: str, rec: dict) -> None:
        """A root of this corpus did not answer. A blocking loader FAILS the corpus (`corpus-root-unresponsive`, no path); a server's keeps it pending
        (`stuck`), its root reading UNRESPONSIVE, to be loaded again by `retry_stuck` when the root is back."""
        if self.retry:
            rec["stuck"] = True
            with self._lock:
                self._stuck.add(cid)
            return
        self._failed(cid, rec, CODE_CORPUS_ROOT_UNRESPONSIVE, None)

    def _load_locked(self, cid: str, rec: dict) -> None:
        spec = rec["spec"]
        reason = self.reg.admit(rec["paths"])
        if reason is not None:                            # a worker of one of its roots is still stuck, or the process cap holds: NO new thread
            self._stuck_now(cid, rec)
            return
        try:
            part, all_missing = self._read(cid, spec, rec)
        except ProfileNotAnswering:
            # a corpus whose profile does not answer is FAILED after one deadline (server and library alike), never left pending to cost every
            # reader a deadline and to blame a root that answered; `reason` says why. The server reads it again when the profile answers.
            self._failed(cid, rec, CODE_CORPUS_ROOT_UNRESPONSIVE, None, reason="profile")
            if self.retry:
                with self._lock:
                    self._retry_failed[cid] = "profile:" + str(spec.get("profile"))
            return
        except RootNotAnswering:
            self._stuck_now(cid, rec)
            return
        except ProfileCollisionError as exc:
            # [spec 39 REQ-NS-TRACE x-profile-guard] a profile the loader refuses fails ONLY the corpora whose entry names it, loudly, never the registry. TWO
            # cases share the exception class and the record says which: a NAME the engine reserves or owns (`reason: profile-collision`, `names`), or a MALFORMED
            # profile where no name collided (`reason: profile-invalid`, `detail`, a short fixed phrase). Never the message and never a path. Every other corpus serves
            self._failed(cid, rec, CODE_CORPUS_LOAD_ERROR, None, error=type(exc).__name__, **config.profile_failure_fields(exc))
            return
        except CorpusFileError as exc:
            # [DEC-LTS-NONREGULAR-FILE-IN-ROOT-HANGS-OR-MISREPORTS] ONE bad member fails ONLY its own corpus, loudly
            self._failed(cid, rec, exc.code, exc.rel_path)
            return
        if part is None or not part.nodes:               # only a corpus that parsed >=1 node is served
            unl = config.unstated_location(spec)          # not served, and its roots cannot be read as paths (a corpus the release serves is never recorded)
            if all_missing:
                self.store.settle(cid, no_roots={"spec": spec, "private": rec["private"]}, unlocated=unl)       # the move and the flag are ONE store step
            else:
                self.store.settle(cid, unlocated=unl)
            self._release(rec["paths"], cid)
            return
        try:
            hash(part.domain_suffix)                       # the store files a partition under its suffix: one that cannot be a key is a malformed ENTRY, as release af4cfc2 met it there
        except TypeError as exc:
            raise EntryShapeError("TypeError") from exc
        part.private_reason = private_reason(cid, spec)   # [INV-PRIVATE-FANOUT] stamped from the registry entry
        if self.kernel is not None:
            try:
                part._prepared_seat = self._prep.pop(cid, None)        # the roots' seat work, done by their fused jobs; `seat_partition` takes it (the signature is unchanged)
                self.kernel.seat_partition(part)
            except RootNotAnswering:
                self._stuck_now(cid, rec)
                return
            except CorpusFileError as exc:
                self._failed(cid, rec, exc.code, exc.rel_path)
                return
        if not self.store.settle(cid, partition=part):
            if self.kernel is not None:                   # unregistered while it was being seated: leave nothing behind, but only OUR registration
                self.kernel.unseat_partition(cid, only_if=part)
            return
        with self._lock:
            self._stuck.discard(cid)

    def _bounded_profile(self, spec: dict):
        """The corpus's registered profile, read like a root read: on a worker of the process registry (key: the profile's own path) under the same
        no-progress deadline, so a profile that lives under a hung root fails ITS corpus (`RootNotAnswering`) and never holds a loader thread. A key
        already stuck answers at once with no new thread. None / no profile named costs nothing."""
        path = spec.get("profile") if isinstance(spec, dict) else None
        if not path or not isinstance(path, str):
            return None
        from .boundedfs import bounded_call
        miss = object()
        got = bounded_call("profile:" + path, lambda: config.registered_profile(spec), miss, self.reg)
        if got is miss:
            raise ProfileNotAnswering("the corpus's profile did not answer")
        return got

    def _shape_phase(self, cid: str, spec: dict, registered):
        """The SHAPE phase of reading an entry: everything that reads the entry and touches no disk, in the order release af4cfc2's build met it. It raises what that
        build raised (a malformed `roots`, a root that is not a string or object, a missing `path`, a `root` that cannot be a path, ...). `declare()` runs it for
        every entry BEFORE any corpus is read, so the registry-error verdict of every such shape is reached before one corpus is served (x-boot-window); `_read` runs
        it again on the loader thread (the entry has not changed) with the corpus's registered profile. `registered` is called where the profile is first needed, so a
        profile that does not answer raises where it always did; `declare` passes None, and then no parser configuration is built at all (the profile affects the parser's
        configuration, not the entry's shape). Returns `(kind, val, suffix, ci, results, groups, root_abs)`; `ci`, `results` and `groups` belong to the `roots` form, `root_abs` to `root`."""
        kind, val = config.served_root_entries(spec)
        suffix = spec.get("domain_suffix", f"proj.{cid}")
        norm = os.path.abspath             # a relative or blank root asks the PROCESS for its working directory (`getcwd`): the one process-level call `declare()` makes
        if kind == "roots":
            # `registered is None` is `declare()`'s call: no parser configuration is built there (it needs the registered profile, and declare is memory-only and calls
            # nothing it does not own); the root specs' shape is what the constructor checks, and it checks it with the default configuration
            ci = CorpusIndex(cid, suffix, val, config=None if registered is None else config.parser_config_for(spec, self.profile, registered=registered()))
            ci.registered_profile = spec.get("profile")
            results: list[str | None] = [None] * len(ci.root_specs)
            groups: dict[str, list[int]] = {}
            for i, sp in enumerate(ci.root_specs):
                p = sp["path"]                            # KeyError when absent, as in af4cfc2
                if isinstance(p, int):
                    continue                               # os.path.isdir(7) is False: a missing root, as in af4cfc2
                groups.setdefault(norm(p), []).append(i)
            return kind, val, suffix, ci, results, groups, None
        root = val
        return kind, val, suffix, None, None, None, (None if (not root or isinstance(root, int)) else norm(root))      # a list raises here, as af4cfc2's isdir did

    def _read(self, cid: str, spec: dict, rec: dict) -> tuple[Partition | None, bool]:
        """The bounded read. (partition or None, every declared root missing). Raises RootNotAnswering, CorpusFileError, or the job's own error."""
        settles = self.kernel is None                     # a server's root is settled by its SEAT (`seat_partition` settles each root the fused job prepared), which is what makes the corpus visible
        jobs: list[tuple[object, Job]] = []
        prepared: dict[str, tuple] = {}                   # a server's FUSED job per root: it reads, hashes and seats (below), so there is no second job for the root
        self._prep.pop(cid, None)
        try:                                              # the SHAPE of the entry: nothing below this try reads a disk or submits a job
            kind, val, suffix, ci, results, groups, root_abs = self._shape_phase(cid, spec, lambda: self._bounded_profile(spec))
            root = val
        except (ProfileNotAnswering, RootNotAnswering, CorpusFileError, ProfileCollisionError):
            raise                                         # a profile that collides with a reserved name is not an entry SHAPE: it fails its corpora only
        except Exception as exc:  # noqa: BLE001
            raise EntryShapeError(type(exc).__name__) from exc
        self._alive()
        if kind == "roots":
            for abs_path, idxs in groups.items():
                slot = self._slot(abs_path)

                def fn(idxs=idxs, abs_path=abs_path):
                    for i in idxs:
                        of = ci.root_specs[i].get("only_file")
                        if of and not isinstance(of, (str, bytes, os.PathLike)) and os.path.isdir(abs_path):
                            raise EntryShapeError("TypeError")     # af4cfc2 raised when it joined a pinned file that is not a string onto a root that exists
                        results[i] = ci.read_root(ci.root_specs[i])
                    _progress()
                    if self.kernel is not None:
                        live_n = sum(1 for i in idxs if results[i])
                        if live_n:                         # the seat's work for this root, in THIS job: one job per root, progress reported by each disk call as before
                            wants = {(ci.root_specs[i].get("only_file") or None) for i in idxs}
                            prepared[abs_path] = self._prepare_root(abs_path, live_n, wants)
                    return None
                jobs.append((slot, slot.submit("build", fn, coalesce=False, foreground=True, settles=settles)))
        else:
            box: dict[str, Partition | None] = {}
            if root_abs:
                slot = self._slot(root_abs)

                def fn1():
                    box["part"] = (config.build_partition(cid, root, suffix, profile=self.profile, spec=spec)
                                   if os.path.isdir(root) else None)
                    _progress()
                    if self.kernel is not None and box["part"] is not None and getattr(box["part"], "root_paths", None):
                        built = box["part"]
                        counts: dict[str, int] = {}
                        for rp in built.root_paths:
                            counts[os.path.abspath(rp)] = counts.get(os.path.abspath(rp), 0) + 1
                        specs_of = self.kernel._specs_of(built)
                        for p_abs, n in counts.items():
                            prepared[p_abs] = self._prepare_root(p_abs, n, {of for sp, of in specs_of if sp == p_abs})
                    return None
                jobs.append((slot, slot.submit("build", fn1, coalesce=False, foreground=True, settles=settles)))
        unanswered = False
        for slot, job in jobs:
            if not self.reg.wait(slot, job):
                unanswered = True
            self._alive()
        if unanswered:
            raise RootNotAnswering("a registered root did not answer when its corpus was read")
        for _slot, job in jobs:
            if job.error is not None:
                raise job.error
        if prepared:
            self._prep[cid] = prepared
        declared = config.declared_root_entries(spec)
        if kind == "roots":
            live = [(ci.root_specs[i], results[i]) for i in range(len(results)) if results[i]]
            all_missing = bool(declared) and not live
            if not live:
                return None, all_missing
            nodes, layers, root_paths, files_hash = ci.finish_cold_build(live, self._alive)
            self._alive()
            return config.partition_from_build(cid, suffix, ci, nodes, layers, root_paths, files_hash), False
        part = box.get("part")
        return part, bool(declared) and part is None and bool(val)

    def _prepare_root(self, abs_path: str, n: int, wants: set) -> tuple:
        """The seat's work for one root, run inside that root's fused job (`FreshnessKernel.prepare_root`). An OSError becomes the coded error the seat would have raised."""
        try:
            return self.kernel.prepare_root(abs_path, n, wants)
        except CorpusFileError:
            raise
        except OSError:
            raise CorpusFileError(CORPUS_FILE_UNREADABLE, ".") from None

    def _release(self, paths: list[str], cid: str) -> None:
        """A corpus that will not be seated gives back the PENDING slots it declared, unless another corpus still needs them. Without this a missing
        or empty root would read pending for ever."""
        if self.kernel is None:
            return
        needed = {p for c, r in self.store.tables()[0].items() if c != cid for p in r["paths"]}
        self.kernel.release_roots(paths, needed)

    # ---- a stuck corpus, once its root is back -----------------------------------------------------------------------------------
    def retry_stuck(self) -> None:
        self.retry_profile_failed()
        self._retry_stuck_roots()

    def _retry_stuck_roots(self) -> None:
        """Called at the start of each watcher cycle. For each stuck corpus whose roots are all back to OK and idle, load it again on its own
        short-lived thread. Memory only on the watcher's thread."""
        with self._lock:
            stuck = [c for c in self._stuck if c not in self._inflight]
        for cid in stuck:
            rec = self.store.lookup_all(cid)[1]
            if rec is None:
                with self._lock:
                    self._stuck.discard(cid)
                continue
            slots = [self.reg.slot(p, create=False) for p in rec["paths"]]
            if any(s is None or s.busy() or s.state() == UNRESPONSIVE for s in slots):
                continue
            rec["stuck"] = False
            with self._lock:
                self._stuck.discard(cid)
            threading.Thread(target=self._load_one, args=(cid,), name="lts-boot-retry", daemon=True).start()

    def retry_profile_failed(self) -> None:
        """Part of the watcher cycle: a corpus failed because its profile did not answer is declared pending again, and read again on its own thread,
        once the profile's slot is no longer stuck (the call returned). Memory only on the watcher's thread."""
        from .boundedfs import key_state
        with self._lock:
            todo = [(c, k) for c, k in self._retry_failed.items() if c not in self._inflight]
        for cid, key in todo:
            st = key_state(key, self.reg)
            slot = self.reg.slot("op:" + _lexical(key), create=False)
            if st == UNRESPONSIVE or (slot is not None and slot.busy()):
                continue
            rec = self.corpora.get(cid)
            with self._lock:
                self._retry_failed.pop(cid, None)
            if rec is None:
                continue
            paths = list(dict.fromkeys(os.path.abspath(r["path"]) for r in config.declared_root_entries(rec) if isinstance(r.get("path"), str)))

            def pending_from(old, rec=rec, paths=paths):          # built from the record that is MOVED, inside the step
                return {"spec": rec, "private": bool(old.get("private")), "paths": paths,
                        "domain_suffix": old.get("domain_suffix"), "since": time.time(), "stuck": False}
            if not self.store.move_failed_to_pending(cid, pending_from, only_reason="profile"):
                continue                                      # no longer failed (unregistered, or registered again), or not a profile failure now
            if self.kernel is not None:
                self.kernel.declare_roots(paths)
            threading.Thread(target=self._load_one, args=(cid,), name="lts-boot-retry", daemon=True).start()

    def stuck_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._stuck)


def build_blocking(corpora: dict[str, dict], profile=None) -> LtsStore:
    """`config.build_store`'s body: a store holding what was read, with every corpus that could not be read in `failed_corpora` or `no_roots_corpora`."""
    store = LtsStore()
    loader = StoreLoader(store, corpora, profile=profile)
    loader.declare()
    loader.run_blocking()
    return store
