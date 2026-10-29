"""Suite-wide hygiene.

The query-log isolation fixture lives in the REPO-ROOT `conftest.py` (it must cover the private
overlay as well). This file adds two things for `tests/`:

1. a per-test default FACTLATTICE_STATE_DIR (a fresh tmp dir), so a test that does not set its own
   never touches a dir shared with another test or with the developer's real state;
2. the session guard on the REAL state dir (`~/.local/state/factlattice`): the session FAILS if this
   run left a ledger record for a temp/repo path there. Design and limits: `tests/_state_guard.py`.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from tests import _state_guard as _G

_REAL = _G.real_state_dir()          # resolved at import, before any test can move HOME / XDG


@pytest.fixture(autouse=True, scope="session")
def _real_state_dir_guard(tmp_path_factory):
    base = tmp_path_factory.getbasetemp()
    _G.set_session_basetemp(base)
    # This session's tempfile.mkdtemp()/mkstemp() dirs land under ITS basetemp, so they are attributable
    # (and cleaned with it). In-process only: subprocesses keep the OS temp dir.
    mk = base / "mkdtemp"
    mk.mkdir(exist_ok=True)
    old_tempdir = tempfile.tempdir
    tempfile.tempdir = str(mk)
    snap = _G.snapshot(_REAL)
    yield
    tempfile.tempdir = old_tempdir
    found, _G.FOREIGN[0] = _G.scan(_REAL, snap)
    if found:
        pytest.fail("STATE GUARD: this test run wrote into the REAL state dir "
                    f"{_REAL} (a test forgot to set FACTLATTICE_STATE_DIR):\n  " + "\n  ".join(found),
                    pytrace=False)


@pytest.fixture(autouse=True)
def _default_state_dir(monkeypatch, tmp_path_factory):
    """A private state dir per test; a test or fixture that sets its own simply overrides it."""
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path_factory.mktemp("state")))


def pytest_terminal_summary(terminalreporter):
    n = _G.FOREIGN[0]
    if n:
        terminalreporter.write_line(
            f"state guard: {n} foreign test-path record{'s' if n != 1 else ''} appeared in the real ledger "
            f"during this run, not attributed to this session")


@pytest.fixture(autouse=True)
def _ledger_nothing_ephemeral(monkeypatch):
    """pytest's tmp_path lives under the OS temp dir, which the ledger treats as EPHEMERAL (skipped).
    Existing ledger tests must keep exercising the ledger, so by default nothing is ephemeral; the
    tests in test_ledger_ephemeral.py undo this to test the default."""
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")

