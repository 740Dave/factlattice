"""(c) The real-state-dir guard. Design: tests/_state_guard.py (read its docstring).

A guard that fails because ANOTHER session queried the engine is useless, so these tests pin both
directions: it must bite on what THIS test process writes, and it must stay quiet about what live
engines write (query-log, miss-log, write-locks, ledger records for real corpora)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from tests import _state_guard as G

REPO = Path(__file__).resolve().parents[1]


def _rec(path_real: str, seq: int = 1) -> bytes:
    return (json.dumps({"v": 1, "ledger_seq": seq, "path_real": path_real}) + "\n").encode()


def _fake_real(tmp_path) -> Path:
    d = tmp_path / "real"
    (d / "ledger").mkdir(parents=True)
    (d / "ledger" / "aaa.jsonl").write_bytes(_rec("/live/corpus/a.lts"))
    (d / "write-locks").mkdir()
    return d


def test_real_state_dir_ignores_FACTLATTICE_STATE_DIR(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert G.real_state_dir() == tmp_path / "xdg" / "factlattice"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert G.real_state_dir() == Path.home() / ".local" / "state" / "factlattice"


def test_guard_ignores_what_live_engines_write(tmp_path):
    d = _fake_real(tmp_path)
    snap = G.snapshot(d)
    (d / "query-log.jsonl").write_text("q\n")
    (d / "miss-log.jsonl").write_text("m\n")
    (d / "write-locks" / "x.lock").write_text("")
    with open(d / "ledger" / "aaa.jsonl", "ab") as fh:                 # a live engine's write to a real corpus
        fh.write(_rec("/live/corpus/a.lts", 2))
    (d / "ledger" / "bbb.jsonl").write_bytes(_rec("/live/corpus/b.lts"))
    assert G.leaked(d, snap) == []


def test_guard_bites_on_a_record_for_a_temp_path_appended_to_an_existing_ledger(tmp_path):
    d = _fake_real(tmp_path)
    snap = G.snapshot(d)
    with open(d / "ledger" / "aaa.jsonl", "ab") as fh:
        fh.write(_rec(str(tmp_path / "corpus" / "c.lts"), 2))
    (hit,) = G.leaked(d, snap)
    assert "aaa.jsonl" in hit and "c.lts" in hit


def test_guard_bites_on_a_new_ledger_file_for_a_temp_path(tmp_path):
    d = _fake_real(tmp_path)
    snap = G.snapshot(d)
    (d / "ledger" / "new.jsonl").write_bytes(_rec(str(tmp_path / "corpus" / "c.lts")))
    assert len(G.leaked(d, snap)) == 1


def test_guard_bites_when_the_ledger_dir_did_not_exist_at_snapshot(tmp_path):
    d = tmp_path / "real"
    d.mkdir()
    snap = G.snapshot(d)
    (d / "ledger").mkdir()
    (d / "ledger" / "n.jsonl").write_bytes(_rec(str(REPO / "x.lts")))   # the repo root is attributable too
    assert len(G.leaked(d, snap)) == 1


def test_guard_bites_on_a_new_blob_that_a_leaked_record_references(tmp_path):
    d = _fake_real(tmp_path)
    snap = G.snapshot(d)
    (d / "ledger-blobs").mkdir()
    (d / "ledger-blobs" / ("a" * 64)).write_bytes(b"x")
    rec = json.loads(_rec(str(tmp_path / "c.lts")))
    rec["prior_blob"] = "a" * 64
    (d / "ledger" / "n.jsonl").write_text(json.dumps(rec) + "\n")
    hits = G.leaked(d, snap)
    assert any("n.jsonl" in h for h in hits) and any("a" * 64 in h for h in hits)


def test_every_test_gets_a_private_state_dir_by_default():
    env = os.environ["FACTLATTICE_STATE_DIR"]
    assert Path(env) != G.real_state_dir()
    assert not (Path(env) / "ledger").exists()          # fresh per test, never inherited from another test


_PROBE = textwrap.dedent('''
    import os
    from factlattice import ledger
    def test_probe():
        os.environ.pop("FACTLATTICE_STATE_DIR", None)        # a test that "forgot" to redirect
        ledger.append({target!r}, {{"verb": "x", "file_sha256_before": None, "file_sha256_after": "z"}})
''')


def _run_probe(tmp_path, target: str):
    xdg = tmp_path / "xdg"
    probe = REPO / "tests" / f"_probe_state_guard_{os.getpid()}.py"
    probe.write_text(_PROBE.format(target=target))
    env = {**os.environ, "XDG_STATE_HOME": str(xdg)}
    env.pop("FACTLATTICE_STATE_DIR", None)
    try:
        return subprocess.run([sys.executable, "-m", "pytest", str(probe), "-q", "-p", "no:cacheprovider"],
                              cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    finally:
        probe.unlink()


def test_a_whole_session_FAILS_when_a_test_writes_into_the_real_state_dir(tmp_path):
    r = _run_probe(tmp_path, str(REPO / "nonexistent-probe-corpus" / "c.lts"))
    assert r.returncode != 0, r.stdout[-800:]
    assert "STATE GUARD" in r.stdout
    assert (tmp_path / "xdg" / "factlattice" / "ledger").exists()      # and it really did write there


def test_a_whole_session_passes_when_the_write_is_for_a_live_looking_path(tmp_path):
    r = _run_probe(tmp_path, "/nonexistent-live-corpus/a.lts")
    assert r.returncode == 0, r.stdout[-800:]


def test_foreign_temp_path_records_are_not_this_sessions_leak_but_are_counted(tmp_path):
    """Another process's tests (pytest-of-<user>/pytest-<other N>, foreign-preflight-* mkdtemps) write
    records for THEIR temp paths into the real ledger while we run. Not ours: no failure, but counted."""
    import tempfile
    d = _fake_real(tmp_path)
    snap = G.snapshot(d)
    foreign = [os.path.join(G.OS_TEMP, "pytest-of-someone", "pytest-99999", "t0", "c.lts"),
               os.path.join(G.OS_TEMP, "foreign-preflight-abc123", "c.lts")]
    with open(d / "ledger" / "aaa.jsonl", "ab") as fh:
        for i, f in enumerate(foreign, 2):
            fh.write(_rec(f, i))
    leaks, n_foreign = G.scan(d, snap)
    assert leaks == [] and n_foreign == 2
    # and an own-basetemp record in the same ledger is still a leak
    with open(d / "ledger" / "aaa.jsonl", "ab") as fh:
        fh.write(_rec(str(tmp_path / "mine.lts"), 9))
    leaks, n_foreign = G.scan(d, snap)
    assert len(leaks) == 1 and n_foreign == 2


def test_this_sessions_mkdtemp_dirs_land_under_its_basetemp(tmp_path_factory):
    import tempfile
    d = tempfile.mkdtemp()
    try:
        assert os.path.realpath(d).startswith(os.path.realpath(str(tmp_path_factory.getbasetemp())) + os.sep)
        assert G.attributable(os.path.join(d, "c.lts"))
    finally:
        os.rmdir(d)


def test_a_whole_session_passes_and_reports_foreign_records(tmp_path):
    r = _run_probe(tmp_path, os.path.join(os.path.realpath("/tmp"), "foreign-preflight-x", "c.lts"))
    assert r.returncode == 0, r.stdout[-800:]
    assert "1 foreign test-path record" in r.stdout and "not attributed to this session" in r.stdout
