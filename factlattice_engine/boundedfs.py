"""[MECH-BR-WRITES] x-build-traps 2: the bound for a path, or a process, that has no probe to lean on.

A server keeps each registered root's real path from that root's own probe (`kernel.root_facts`). What it still has to ask the disk, a root
only this call knows about (a corpus that failed to build, a state location, an old spelling) or one root confirmed fresh for an attach, goes
through here, on a `RootRegistry`: the same machinery the kernel's probes use (one job per path at a time, a deadline that counts time with no
progress, a stuck thread replaced and never piled up).

ONE REGISTRY PER PROCESS ([MECH-BR-BOOT] x-library). `rootstate.process_registry()` is the process's only registry: every kernel, every
`build_store` and this module use it, so a root has one slot and one running job whatever asked, no root gets two stuck threads, and
`stuck_workers` is the true count. `registry()` here returns it; `reg=` is still accepted (a kernel passes its own, which is the same object).
A path that is not a registered root gets an AUXILIARY slot (it shares the pool, counts as stuck, and is never listed as a root).

  * `resolve_roots(paths)`: where each root really is and whether it answered. Concurrent, so K hung roots cost one deadline. A root that does
    not answer is marked and keeps the last real path it gave (None if never). On a registered root's slot the job does not settle a root that
    was never read.
  * `bounded_call(key, fn, default)`: one stat or read under a state location or an old spelling, or `default` if it did not answer in time. A
    key that is already marked answers `default` at once and starts no thread.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable

from factlattice.rootview import OK, RootFact, lexical

from .rootstate import UNRESPONSIVE, Job, RootRegistry, process_registry, progress, realpath_progress, reset_process_registry

_lock = threading.Lock()
_REAL: dict[str, str] = {}


def registry() -> RootRegistry:
    """The process's one registry (`rootstate.process_registry`)."""
    return process_registry()


def _reset(deadline_s: float | None = None) -> RootRegistry | None:
    """For tests: forget every slot and kept answer, and start the PROCESS registry over with this deadline. Threads stuck in the old one stay
    stuck. The one reset helper (`rootstate.reset_process_registry`) does the registry; this also clears the kept real paths."""
    with _lock:
        _REAL.clear()
    return reset_process_registry(deadline_s)


def _resolve_job(path: str) -> Callable[[], Callable[[], None]]:
    def fn():
        real = os.path.realpath(path)
        progress()

        def commit() -> None:
            _REAL[path] = real
        return commit
    return fn


def resolve_roots(paths: Iterable[str], reg: RootRegistry | None = None, untrusted: Iterable[str] = ()) -> list[RootFact]:
    """A `RootFact` per distinct path (lexical), each resolved on its own worker and waited for together. `untrusted` names the paths (lexical) that are
    strings of a failed entry rather than declared roots: their facts say so ([MECH-BR-BOOT] x-load-error (UNTRUSTED ROOTS))."""
    reg = reg or registry()
    keys = list(dict.fromkeys(lexical(p) for p in paths))
    waits = []
    for k in keys:
        slot = reg.slot(k, create=False) or reg.slot(k, aux=True)
        if slot.recover is None:
            slot.recover = lambda k=k, reg=reg, st=slot.aux: Job("full", _resolve_job(k), clock=reg.clock, settles=st)
        if slot.state() == UNRESPONSIVE:
            waits.append((slot, None))                 # a stuck root is not asked again and nothing queues behind its stuck job
        else:
            # a root whose job is merely running (a poll) queues this one behind it: one running job per root, ever
            waits.append((slot, slot.submit("resolve", _resolve_job(k), foreground=True, settles=slot.aux)))
    for slot, job in waits:
        if job is not None:
            reg.wait(slot, job)
    bad = {lexical(p) for p in untrusted}
    out = []
    for k, (slot, _job) in zip(keys, waits):
        out.append(RootFact(k, _REAL.get(k), slot.state(), k in bad))
    return out


_flight_lock = threading.Lock()
_FLIGHT: dict[tuple, tuple] = {}          # (registry id, lexical root) -> (the confirm Job, its result box): ONE confirm per root in flight, joined by the rest


def confirm_real(path: str, reg: RootRegistry | None = None, not_before: float | None = None) -> str | None:
    """The fresh real path of ONE registered root, for the attach decision of a write ([MECH-BR-WRITES] x-membership).

    It never queues behind root work: it runs on its OWN auxiliary slot ("confirm:<path>") with the foreground class, separate from the root's probe
    slot. AT MOST ONE confirm per root is in flight (they serialise). A write may use a confirm's answer only if that confirm job STARTED after
    `not_before` (the moment the write's own file resolve returned, `time.monotonic()`): a write that finds one in flight that started earlier, or one
    that finished before it began, waits for it to end (bounded as below) and then joins, or starts, one that began after. A confirm that began after
    the resolve is JOINED by every write that finds it running, with no second thread.
    The wait is bounded by NO-PROGRESS time per disk call (the job reports progress after each component of the path), so the total wait for one
    confirm is at most the number of its calls times the root deadline; a write that waits out a stale confirm and then a new one waits for at most two.
    None means no answer for this write (a confirm did not answer in time, or its slot is stuck from an earlier overrun whose call has not returned):
    the caller counts the root as unconfirmed and refuses the write. A confirm NEVER marks a root: the root stays ok for its probes to judge. A hung
    root costs one stuck thread for the confirm, however many writes ask, and the slot recovers when the stuck call returns."""
    reg = reg or registry()
    k = lexical(path)
    key = (id(reg), k)
    slot = reg.slot("confirm:" + k, aux=True)
    if slot.recover is None:
        slot.recover = lambda reg=reg: Job("full", lambda: None, clock=reg.clock)
    for _round in range(3):
        mine = False
        with _flight_lock:
            cur = _FLIGHT.get(key)
            if cur is not None:
                job, box = cur
                started = box.get("started")
                stale = not_before is not None and started is not None and started <= not_before
                if stale and job.done.is_set():
                    del _FLIGHT[key]                    # finished before this write's resolve: nobody may read it, and it is not in flight
                    cur = None
            if cur is None:
                if slot.state() == UNRESPONSIVE:        # an earlier confirm's call has not returned: no second thread
                    return None
                box = {}

                def fn(box=box):
                    box["started"] = time.monotonic()
                    box["v"] = realpath_progress(k)
                    return None
                job = slot.submit("confirm", fn, coalesce=False, foreground=True)
                _FLIGHT[key] = (job, box)
                mine = True
                stale = False
        try:
            answered = reg.wait(slot, job) and job.error is None
        finally:
            if mine:
                with _flight_lock:
                    if _FLIGHT.get(key, (None,))[0] is job:
                        del _FLIGHT[key]
        if not answered:
            return None
        if stale:
            continue                                    # it has ended: the next round starts or joins one that began after this write's resolve
        return box.get("v")
    return None


def resolve_one(path: str, reg: RootRegistry | None = None) -> str:
    """The real path of one root, bounded. A path that did not answer reads as its lexical spelling (nothing was read from it)."""
    f = resolve_roots([path], reg)[0]
    return f.real or f.declared


def key_state(key: str, reg: RootRegistry | None = None) -> str | None:
    """The state of the slot a `bounded_call` with this key runs on (`ok`, `pending`, `unresponsive`), or None when it has none (nothing is stuck there)."""
    reg = reg or registry()
    slot = reg.slot("op:" + lexical(key), create=False)
    return slot.state() if slot is not None else None


def bounded_call(key: str, fn: Callable[[], object], default: object, reg: RootRegistry | None = None) -> object:
    """`fn()` on a worker with the deadline, or `default` if the worker made no progress for it. An exception `fn` raises is raised
    here, as a direct call would. `key` names what is being read (a state location, an old spelling): one stuck read marks that key
    and every later call for it answers `default` at once."""
    reg = reg or registry()
    slot = reg.slot("op:" + lexical(key), aux=True)
    if slot.recover is None:
        # the stuck call returning IS the answer for a single read: the key serves again, with no restart
        slot.recover = lambda reg=reg: Job("full", lambda: None, clock=reg.clock)
    if slot.state() == UNRESPONSIVE:
        return default
    box: dict[str, object] = {}

    def job_fn():
        box["v"] = fn()
        progress()
        return None
    job = slot.submit("full", job_fn, coalesce=False, foreground=True)
    if not reg.wait(slot, job):
        return default
    if job.error is not None:
        raise job.error
    return box.get("v", default)


__all__ = ["registry", "resolve_roots", "resolve_one", "confirm_real", "bounded_call", "key_state", "OK"]
