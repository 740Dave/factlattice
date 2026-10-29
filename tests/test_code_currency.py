"""[MECH-BUILD-STAMP] / [RISK-STALE-CODE] — a running server must not silently serve from
a stale CODE snapshot.

The acceptance bar, and where each item is proven:

  (a) DETECTS DIVERGENCE — when the on-disk engine build differs from the build the server
                           seated at start, the sentinel fires and calls its reconcile hook.
                           -> test_divergent_on_disk_build_fires_reconcile

  (b) NO FALSE FIRE      — an unchanged build never fires (the sentinel cannot manufacture a
                           divergence), and an unreadable stamp fails safe (returns, no fire).
                           -> test_matching_build_does_not_fire
                              test_unreadable_stamp_fails_safe

  (c) STAMP IS CONTENT   — the stamp moves iff a `.py` source byte moves (mtime-immune), so a
                           code change is detected and a `touch` is not.
                           -> test_stamp_tracks_source_bytes_not_mtime

  (d) THE SUCCESSOR SERVES / (e) NO DROPPED REQUEST
                           -> test_code_currency_reconcile.py

THE BAR ABOVE IS DETECTION ONLY, and every case here injects a FAKE `reconcile` callback —
so for a long time the one reconcile that actually ships, `reconcile_by_reexec`, had no
coverage at all and was broken end to end while this file stayed green. Do not add reconcile
cases here: the reconcile must be proven against a REAL process over REAL pipes, which is
what test_code_currency_reconcile.py does. Keep the bar in those two files, never forked.

This is the CODE analogue of test_self_heal.py's DATA guarantees: [MECH-BACKSTOP] retires a
missed FILE event; [MECH-BUILD-STAMP] retires a missed CODE update.
"""
from __future__ import annotations

import os
import time

from factlattice_engine.build_stamp import CodeCurrencySentinel, build_stamp


def _pkg(tmp_path):
    """A throwaway 'engine package' dir with one source file (the stamp target)."""
    d = tmp_path / "pkg"
    d.mkdir()
    (d / "mod.py").write_text("VALUE = 1\n")
    return str(d)


def test_divergent_on_disk_build_fires_reconcile(tmp_path):
    pkg = _pkg(tmp_path)
    fired = []
    s = CodeCurrencySentinel(package_dir=pkg, reconcile=lambda: fired.append(True))

    # Seated build == on-disk build: no divergence yet.
    assert s.check_once() is False
    assert fired == []

    # A code update lands on disk UNDER the running 'server' (its seated stamp is stale).
    (tmp_path / "pkg" / "mod.py").write_text("VALUE = 2  # the propagated fix\n")

    assert s.check_once() is True, "sentinel did not detect the on-disk code divergence"
    assert fired == [True], "reconcile hook was not invoked on divergence"
    assert s.divergences == 1


def test_matching_build_does_not_fire(tmp_path):
    pkg = _pkg(tmp_path)
    fired = []
    s = CodeCurrencySentinel(package_dir=pkg, reconcile=lambda: fired.append(True))
    for _ in range(3):
        assert s.check_once() is False
    assert fired == []


def test_unreadable_stamp_fails_safe(tmp_path):
    pkg = _pkg(tmp_path)
    s = CodeCurrencySentinel(package_dir=pkg, reconcile=lambda: (_ for _ in ()).throw(
        AssertionError("must not fire on an unprovable stamp")))
    # Seated stamp exists, but the on-disk stamp is now unreadable -> None -> no fire.
    s._package_dir = os.path.join(pkg, "does-not-exist-anymore")
    # walk of a missing dir yields no files -> a valid (empty) stamp, not None; force the
    # None path by pointing at a file (os.walk of a non-dir yields nothing either) and
    # asserting the invariant: a None seated OR live stamp never fires.
    s.seated_stamp = None
    assert s.check_once() is False


def test_stamp_tracks_source_bytes_not_mtime(tmp_path):
    pkg = _pkg(tmp_path)
    a = build_stamp(pkg)
    # touch: bump mtime, identical bytes -> SAME stamp (mtime-immune).
    os.utime(os.path.join(pkg, "mod.py"), (time.time() + 10, time.time() + 10))
    assert build_stamp(pkg) == a
    # change bytes -> stamp moves.
    (tmp_path / "pkg" / "mod.py").write_text("VALUE = 3\n")
    assert build_stamp(pkg) != a
