"""tools/run_bounded.py: commit-if-green's wall-clock bound. A hung suite (2026-10-03: a test blocked
13 min on an unbounded stdout read) must end as a loud refusal, killing ONLY the process group it
started, grandchildren included."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "tools" / "run_bounded.py"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_fast_command_passes_its_exit_status_through():
    assert subprocess.run([sys.executable, str(TOOL), "10", "--", sys.executable, "-c", "raise SystemExit(3)"]).returncode == 3
    assert subprocess.run([sys.executable, str(TOOL), "10", "--", sys.executable, "-c", "pass"]).returncode == 0


def test_a_hung_command_and_its_grandchild_are_killed_with_124(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    child = (
        "import subprocess, sys, time\n"
        f"g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(g.pid))\n"
        "time.sleep(600)\n")
    t = time.monotonic()
    r = subprocess.run([sys.executable, str(TOOL), "2", "--", sys.executable, "-c", child],
                       capture_output=True, text=True)
    assert r.returncode == 124 and time.monotonic() - t < 20
    assert "exceeded" in r.stderr
    gpid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while _alive(gpid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(gpid), "the grandchild (a hung server) must die with the group"


def test_commit_if_green_uses_the_bound():
    src = (TOOL.parent / "commit-if-green").read_text()
    assert "run_bounded.py" in src and "CIG_TIMEOUT_S" in src


# --- review of ec15d6a: F1-F4, P3 -------------------------------------------------------------
def test_F1_a_sigterm_ignoring_grandchild_is_still_killed(tmp_path):
    pidfile = tmp_path / "g.pid"
    child = (
        "import subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(g.pid))\n"
        "time.sleep(600)\n")
    try:
        # DEVNULL, not a pipe: a surviving grandchild would hold a pipe open and hang the test itself
        r = subprocess.run([sys.executable, str(TOOL), "2", "--", sys.executable, "-c", child],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40)
        assert r.returncode == 124
        gpid = int(pidfile.read_text())
        deadline = time.monotonic() + 10
        while _alive(gpid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(gpid), "a SIGTERM-ignoring grandchild must still get SIGKILL"
    finally:                                    # clean up only the grandchild THIS test started
        if pidfile.exists():
            g = int(pidfile.read_text())
            if _alive(g):
                os.kill(g, signal.SIGKILL)


def test_F2_non_finite_or_non_positive_bounds_are_refused():
    for bad in ("nan", "inf", "-inf", "0", "-5", "abc", ""):
        r = subprocess.run([sys.executable, str(TOOL), bad, "--", sys.executable, "-c", "pass"],
                           capture_output=True, text=True)
        assert r.returncode == 125, (bad, r.returncode)
        assert "bound" in r.stderr


def _mini_repo(tmp_path, test_body):
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "tests").mkdir()
    for f in ("commit-if-green", "run_bounded.py"):
        src = TOOL.parent / f
        (repo / "tools" / f).write_text(src.read_text())
        (repo / "tools" / f).chmod(0o755)
    (repo / "tests" / "test_x.py").write_text(test_body)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "base"]):
        subprocess.run(cmd, cwd=repo, check=True, env=env)
    (repo / "change.txt").write_text("x")
    subprocess.run(["git", "add", "change.txt"], cwd=repo, check=True)
    return repo, env


def _commits(repo):
    return int(subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=repo, capture_output=True,
                              text=True).stdout.strip())


def test_F4_a_timeout_refuses_and_makes_no_commit(tmp_path):   # regression pin (passes before the fixes)
    repo, env = _mini_repo(tmp_path, "import time\ndef test_hang():\n    time.sleep(600)\n")
    r = subprocess.run(["tools/commit-if-green", str(repo), "-qm", "should not land"], cwd=repo,
                       capture_output=True, text=True, env=dict(env, CIG_TIMEOUT_S="3"))
    assert r.returncode == 1 and "exceeded" in r.stdout
    assert _commits(repo) == 1, "a timed-out suite must never commit"


def test_F2_commit_if_green_refuses_an_unbounded_timeout(tmp_path):
    repo, env = _mini_repo(tmp_path, "def test_ok():\n    pass\n")
    r = subprocess.run(["tools/commit-if-green", str(repo), "-qm", "x"], cwd=repo,
                       capture_output=True, text=True, env=dict(env, CIG_TIMEOUT_S="inf"))
    assert r.returncode == 1 and "CIG_TIMEOUT_S" in r.stdout
    assert _commits(repo) == 1


def test_F3_relative_invocation_works(tmp_path):
    repo, env = _mini_repo(tmp_path, "def test_ok():\n    pass\n")
    # invoked by a RELATIVE path from OUTSIDE the tree: dirname "$0" must be resolved before `cd`
    r = subprocess.run(["repo/tools/commit-if-green", "repo", "-qm", "lands"], cwd=tmp_path,
                       capture_output=True, text=True, env=dict(env, CIG_TIMEOUT_S="60"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _commits(repo) == 2


def test_P3_a_terminated_wrapper_takes_its_group_with_it(tmp_path):
    pidfile = tmp_path / "c.pid"
    child = f"import os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(600)\n"
    w = subprocess.Popen([sys.executable, str(TOOL), "600", "--", sys.executable, "-c", child])
    deadline = time.monotonic() + 10
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    cpid = int(pidfile.read_text())
    w.terminate(); w.wait(timeout=15)
    deadline = time.monotonic() + 10
    while _alive(cpid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(cpid), "SIGTERM to the wrapper must reach its pytest group"
