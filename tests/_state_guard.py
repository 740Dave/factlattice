"""The real-state-dir guard (SDLC 7.6 / ledger follow-up (c)).

WHAT IT PROTECTS: a test that forgets to redirect FACTLATTICE_STATE_DIR writes its throwaway ledger
records into the developer's REAL ~/.local/state/factlattice/ledger (it happened 2026-10-04).

WHY NOT "SNAPSHOT THE DIRECTORY AND FAIL ON ANY CHANGE": that directory is shared with the live engines.
query-log.jsonl, miss-log.jsonl and write-locks/ change constantly from other sessions, and other
sessions' typed writes append ledger records for REAL corpora. A guard that fails because someone else
queried the engine would be switched off within a day.

THE DESIGN: watch only what this test process can cause, and attribute it.
  * only `ledger/*.jsonl` (name -> size) and `ledger-blobs/` (names) are snapshotted; query-log,
    miss-log, write-locks and everything else are not looked at;
  * a ledger file that is new or grew gets only its NEW bytes parsed (it is append-only);
  * a new record is a LEAK only if its `path_real` lies under THIS session's pytest basetemp
    (`tmp_path_factory.getbasetemp()`, pytest-of-<user>/pytest-<N> for this run; the conftest also points
    `tempfile.tempdir` there, so this session's mkdtemp dirs count) or this repo. A live engine's record
    for a real corpus path is ignored. A record for ANOTHER temp path (a concurrent pytest run, a
    foreign-preflight-* mkdtemp) is FOREIGN: not a failure, counted in the terminal summary. With the
    ephemeral skip live ((e), `ledger.is_ephemeral`) such a record can only come from an engine that
    predates it, so the line is now a signal about OLD ENGINES, not about new code;
  * a new blob is a LEAK only if a leaked record references it (`prior_blob`): a blob is only ever
    written together with its record.
LIMITS (stated, not hidden): a test that writes through the writers to a real corpus path leaves a
record this guard cannot tell from a live engine's; write-lock sidecars are not attributable (keyed on
a path hash); a record appended and then truncated away is invisible. The real dir is resolved with
FACTLATTICE_STATE_DIR IGNORED, the way `edit._lock_dir` resolves it with the variable unset.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OS_TEMP = tempfile.gettempdir()    # captured at import, before the conftest re-points tempfile.tempdir


REAL_HOME = os.path.expanduser("~")      # resolved at import (collection), before the per-test HOME isolation moves it


def real_state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state") / "factlattice"


_BASETEMP: list[str] = []          # this session's pytest basetemp (set by the conftest fixture)
FOREIGN = [0]                      # foreign test-path records seen at the last scan (for the summary line)


def set_session_basetemp(path) -> None:
    _BASETEMP[:] = [os.path.realpath(str(path))]


def attributable_roots() -> list[str]:
    """THIS session's own places only: its pytest basetemp (pytest-of-<user>/pytest-<N> for THIS run;
    this session's tempfile.mkdtemp dirs live under it, see conftest) and the repo root. NOT the shared
    temp dir: other processes' test runs (another pytest-<N>, foreign-preflight-* mkdtemps) write there."""
    return sorted({*_BASETEMP, os.path.realpath(REPO)})


def attributable(path: str, roots=None) -> bool:
    return _under(path, attributable_roots() if roots is None else roots)


def _is_temp(path: str) -> bool:
    return _under(path, {os.path.realpath(OS_TEMP), os.path.realpath("/tmp"),
                         os.path.realpath(os.environ.get("TMPDIR", "/tmp"))})


def _under(path: str, roots) -> bool:
    p = os.path.realpath(path)
    return any(p == r or p.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def snapshot(real: Path) -> dict:
    ledgers = {}
    ld = real / "ledger"
    if ld.is_dir():
        for p in ld.glob("*.jsonl"):
            try:
                ledgers[p.name] = p.stat().st_size
            except OSError:
                pass
    bd = real / "ledger-blobs"
    blobs = {p.name for p in bd.iterdir()} if bd.is_dir() else set()
    return {"ledger": ledgers, "blobs": blobs}


def leaked(real: Path, snap: dict, roots=None) -> list[str]:
    """Human-readable findings; [] means this session left nothing in the real state dir."""
    return scan(real, snap, roots)[0]


def scan(real: Path, snap: dict, roots=None) -> tuple[list[str], int]:
    """(leaks attributable to THIS session, count of FOREIGN temp-path records: informational only)."""
    roots = attributable_roots() if roots is None else roots
    out: list[str] = []
    foreign = 0
    new_blobs = {p.name for p in (real / "ledger-blobs").iterdir()} - snap["blobs"] \
        if (real / "ledger-blobs").is_dir() else set()
    ld = real / "ledger"
    if not ld.is_dir():
        return out, 0
    for p in sorted(ld.glob("*.jsonl")):
        old = snap["ledger"].get(p.name)
        try:
            data = p.read_bytes()
        except OSError:
            continue
        start = 0 if old is None or old > len(data) else old
        if old is not None and len(data) == old:
            continue
        for line in data[start:].splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            pr = rec.get("path_real")
            if isinstance(pr, str) and not _under(pr, roots) and _is_temp(pr):
                foreign += 1                  # someone else's test run, not ours
            if isinstance(pr, str) and _under(pr, roots):
                out.append(f"{p} gained a record (seq {rec.get('ledger_seq')}) for {pr}")
                if rec.get("prior_blob") in new_blobs:
                    out.append(f"{real / 'ledger-blobs' / rec['prior_blob']} created for that record")
    return out, foreign
