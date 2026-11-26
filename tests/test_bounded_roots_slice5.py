"""Slice 5, small items from a delta cold read of 3a3e17e (specs/38 [MECH-BR-PROBE] x-states, x-mark-clears)."""
from __future__ import annotations

import threading
import time

from factlattice_engine import rootstate as RS


def test_settle_ok_applies_the_lazy_mark_first_so_a_late_pending_root_is_not_settled_ok():
    """P3-2. A PENDING root whose running job has made no progress for longer than the deadline IS unresponsive, whoever looks first sets the mark (`_lazy_locked`). `settle_ok`
    read `_state` raw, so a seat landing on such a root, before anyone had looked, set it ok for an instant. Here nobody looks between the stall and the settle, and the RAW state
    is read straight after: it must be unresponsive, never ok. Red on 8c0eb80 (raw state ok)."""
    reg = RS.RootRegistry(0.1)
    slot = reg.slot("/late-pending")
    gate = threading.Event()
    job = slot.submit("full", lambda: gate.wait(10), foreground=True)       # a first read that stops answering
    try:
        deadline = time.monotonic() + 3
        while job.started is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert job.started is not None, "setup: the job is running"
        time.sleep(0.4)                                                      # four deadlines of silence; no reader has asked, so nothing has marked it yet
        with reg._cv:
            assert slot._state == RS.PENDING, "setup: nobody has applied the lazy transition"
            assert slot._late_locked() > 0, "setup: the running job is already late"
        slot.settle_ok()
        with reg._cv:
            raw = slot._state                                                # the raw field, not state(): that one is lazy and would hide it
        assert raw == RS.UNRESPONSIVE, f"a late pending root was settled ok for an instant (raw state {raw!r})"
        assert slot.state() == RS.UNRESPONSIVE
    finally:
        gate.set()
        job.done.wait(5)


def test_settle_ok_still_settles_a_pending_root_whose_job_is_on_time():
    """Control for the test above: a pending root with no late job is still settled ok."""
    reg = RS.RootRegistry(5)
    slot = reg.slot("/on-time")
    slot.settle_ok()
    with reg._cv:
        assert slot._state == RS.OK
