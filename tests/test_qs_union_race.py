"""[words/union race, internal ticket] _Entry must never expose `_words` without `_union`.

The defect: words() assigned `_words` BEFORE building `_union`, so a reader arriving in that window
saw `_words` set, skipped the build, and got `_union = None` -> every caller raised TypeError.
The window is too narrow to hit reliably with threads, so this test forces it deterministically: it
pauses the builder right after the words are computed and runs a second reader at that moment.
"""
from __future__ import annotations

import threading

from factlattice_engine import query_surface as Q


def test_reader_during_build_never_sees_union_none(monkeypatch):
    """Pause the builder exactly between `_words` being assigned and `_union` being assigned (the
    defect window), by shadowing the module's `frozenset` (the call that publishes `_union`)."""
    ent = Q._Entry((("title", "tile map version field"), ("id", "xl-m1")))
    in_window = threading.Event()
    release = threading.Event()
    real_frozenset = frozenset
    first = {"done": False}

    def slow_frozenset(*a):
        if not first["done"]:
            first["done"] = True
            in_window.set()                 # the builder is here: `_words` published, `_union` not
            release.wait(5)
        return real_frozenset(*a)

    monkeypatch.setattr(Q, "frozenset", slow_frozenset, raising=False)
    results = {}

    def builder():
        results["builder"] = ent.union()

    t = threading.Thread(target=builder)
    t.start()
    assert in_window.wait(5)
    try:
        results["reader"] = ent.union()     # a second reader arrives inside the window
    finally:
        release.set()
        t.join(5)
    assert results["reader"] is not None, "reader saw _union None: the publish-order race"
    assert results["reader"] == results["builder"] == real_frozenset({"tile", "map", "version", "field", "xl", "m1"})


def test_many_threads_one_cold_entry_no_exception():
    """CHARACTERISATION GUARD (review F6): green at the pre-fix base at the default switch interval,
    because the window is too narrow to hit by chance. A tiny switch interval makes it able to catch the
    defect (the review saw ~3,000 bad results per run on the old code at 1e-6); the deterministic tests
    above are the real proof."""
    import sys
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        _many_threads_body()
    finally:
        sys.setswitchinterval(old)


def _many_threads_body():
    errs = []
    for _ in range(200):
        ent = Q._Entry((("title", "alpha beta gamma delta"),))
        def go():
            try:
                u = ent.union()
                assert u is not None and "gamma" in u
            except Exception as ex:   # noqa: BLE001
                errs.append(repr(ex))
        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
    assert errs == []


def test_two_racing_builders_count_bytes_once(monkeypatch):
    """review F1 (54c8f9c): the publisher check must be atomic with the publish, so two builders that
    race on one cold entry grow the owning cache's byte estimate exactly ONCE, not twice."""
    grows = []

    class _FC:
        def _grow(self, n):
            grows.append(n)

    fc = _FC()
    ent = Q._Entry((("title", "alpha beta gamma"),), fc=fc)
    in_window = threading.Event()
    release = threading.Event()
    real_frozenset = frozenset
    first = {"done": False}

    def slow_frozenset(*a):
        if not first["done"]:                # pause builder A after its words are computed
            first["done"] = True
            in_window.set()
            release.wait(5)
        return real_frozenset(*a)

    monkeypatch.setattr(Q, "frozenset", slow_frozenset, raising=False)
    t = threading.Thread(target=ent.union)
    t.start()
    assert in_window.wait(5)
    try:
        ent.union()                          # builder B runs to completion while A is paused
    finally:
        release.set()
        t.join(5)
    assert len(grows) == 1, f"bytes counted {len(grows)} times for one entry"
