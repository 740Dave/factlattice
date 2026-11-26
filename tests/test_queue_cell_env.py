"""The merge queue gives every process that runs PR code a per-cell environment (merge_queue.cell_env, through child_env and git_env), and the `checks` step runs this suite under
it (ci_step.isolated_env on top). A test that depends on the caller's HOME or XDG defaults fails ONLY under that environment, which is how it was found: a CI runner whose
temp folder sits under a home folder, and an exported state directory.

What is pinned here: (1) cell_env does not set XDG_STATE_HOME (the engine's state directory is not ours to move) and does not touch the caller's value; (2) the suite files that
read HOME, XDG_* and the temp folder pass under the EXACT environment the queue builds, including a cell folder that is not under the OS temp dir. The two files named below
failed under it before the fix. The whole suite under that environment is a manual step of the fix (see the report); this test runs the files that depend on it.
"""

import os
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, ".github", "workflows", "scripts"))
import ci_step  # noqa: E402
import merge_queue as mq  # noqa: E402

CELL_FILES = ("tests/test_named_roots_s2.py", "tests/test_disclosure_guard_publication.py", "tests/test_writer_non_string_values.py")


def queue_env(monkeypatch, cell, runner_temp):
    """The environment the queue's `checks` step hands to pytest: git_env() of the driver's environment with MQ_CELL_DIR set, then ci_step.isolated_env on top."""
    monkeypatch.setenv("MQ_CELL_DIR", str(cell))
    monkeypatch.setenv("MQ_PYDIR", str(runner_temp / "uv-python"))
    monkeypatch.setenv(
        "RUNNER_TEMP", str(runner_temp)
    )  # ci_step.isolated_env makes its scratch HOME and state dirs under the cause dir = RUNNER_TEMP
    monkeypatch.delenv("CI_CAUSE_DIR", raising=False)
    for k in ("XDG_STATE_HOME", "FACTLATTICE_STATE_DIR"):
        monkeypatch.delenv(k, raising=False)
    return ci_step.isolated_env(mq.git_env(BASE_REF="origin/main", UV_PROJECT_ENVIRONMENT=str(runner_temp / "venv")))


def test_the_cell_environment_sets_the_cache_config_and_data_homes_but_never_the_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "/callers/own/state")
    env = mq.cell_env(str(tmp_path / "cell"), "/trusted/uv-python")
    assert "XDG_STATE_HOME" not in env  # not set by the cell, and the caller's own value is not read, copied or removed by cell_env
    assert os.environ["XDG_STATE_HOME"] == "/callers/own/state"
    for k in ("HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "UV_CACHE_DIR", "TMPDIR"):
        assert env[k].startswith(str(tmp_path / "cell") + os.sep) and os.path.isdir(env[k]), k
    assert env["PYTHONNOUSERSITE"] == "1" and env["UV_PYTHON_INSTALL_DIR"] == "/trusted/uv-python"
    assert mq.child_env({"MQ_CELL_DIR": str(tmp_path / "c2"), "PATH": "/bin"})["PATH"] == "/bin"


def test_the_environment_the_queue_builds_is_the_one_this_test_runs_under(tmp_path, monkeypatch):
    """Pin the shape of the EXACT environment: the cell variables, the scratch HOME and the state dirs of ci_step.isolated_env, no inherited XDG_STATE_HOME."""
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    env = queue_env(monkeypatch, runner_temp / "cell.X", runner_temp)
    assert "XDG_STATE_HOME" not in env and env["PYTHONNOUSERSITE"] == "1"
    assert env["XDG_CACHE_HOME"].startswith(str(runner_temp / "cell.X")) and env["TMPDIR"].startswith(str(runner_temp / "cell.X"))
    assert env["HOME"].startswith(str(runner_temp)) and "ci-home-" in env["HOME"]  # isolated_env's scratch HOME wins over the cell's
    assert env["FACTLATTICE_STATE_DIR"].startswith(str(runner_temp)) and "GITHUB_TOKEN" not in env


@pytest.mark.parametrize("variant", ["state-home-exported-by-the-caller"])
def test_the_suite_files_that_read_home_and_xdg_pass_under_the_exact_cell_environment(tmp_path, monkeypatch, variant):
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    env = queue_env(monkeypatch, runner_temp / "cell.X", runner_temp)
    # a runner service that exports a state home: the one test that relies on the HOME-relative state default pins it itself (monkeypatch.delenv), so nothing may land here
    env["XDG_STATE_HOME"] = str(runner_temp / "exported-state")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", *CELL_FILES],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-1000:]
    assert " passed" in p.stdout and "error" not in p.stdout.lower().split("passed")[-1], p.stdout[-400:]
    assert not (runner_temp / "exported-state" / "factlattice").exists()  # nothing was written into the exported state home
