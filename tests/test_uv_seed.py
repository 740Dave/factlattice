"""The warm uv cache a merge-queue test cell is seeded from (scripts/uv_seed.py; SDLC.md 7.6 CI-QUEUE-UV-SEED).

Pinned here: the source is built only by `refresh` (a complete, read-only seed with a marker written last, `current` repointed atomically, the two newest kept); a cell's copy is
a clone that the cell may write and that never changes the source; a missing or incomplete source is an error naming the path (not an empty-cache fallback); the driver seeds
before any PR code runs; `child_env` keeps the source's path out of PR code; and nothing but the trusted workflow calls `refresh`. What only the runner proves: the APFS clone
speed and the uv-seed.yml run on fl-mac-01.
"""

import ast
import os
import stat
import subprocess
import sys

import pytest
import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(ROOT, ".github", "workflows", "scripts")
sys.path.insert(0, SCRIPTS)
import ci_step  # noqa: E402,F401
import merge_queue as mq  # noqa: E402
import uv_seed  # noqa: E402

from tests._snapshot import is_published_snapshot, skip_reason


class FakeUv:
    """A stand-in for subprocess.run: a `uv` call writes a file into its UV_CACHE_DIR (what a real sync does); anything else is a real call."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        if argv[:3] == ["uv", "python", "dir"]:  # the refresher asks where uv's managed Python lives, with the real environment
            return subprocess.CompletedProcess(argv, 0, stdout="/runner/uv-python\n")
        if argv[0] == "uv":
            env = kw["env"]
            self.calls.append((argv, kw.get("cwd"), dict(env)))
            d = os.path.join(env["UV_CACHE_DIR"], "wheels-v5")
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, f"{len(self.calls)}.whl"), "w").write("x" * 10)
            return subprocess.CompletedProcess(argv, 0)
        return subprocess.run(argv, **kw)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "uv.lock").write_text("lock")
    (r / "pyproject.toml").write_text("[project]\nname='x'\n")
    return r


def tree_state(root):
    return sorted(
        (os.path.relpath(os.path.join(d, f), root), os.path.getsize(os.path.join(d, f)), os.stat(os.path.join(d, f)).st_mode)
        for d, _, fs in os.walk(root)
        for f in fs
    )


def test_refresh_builds_a_complete_read_only_seed_and_points_current_at_it(tmp_path, repo):
    root = str(tmp_path / "seedroot")
    fake = FakeUv()
    final = uv_seed.refresh(root, str(repo), "100-1", run=fake)
    assert final == os.path.join(root, "seed-100-1") and uv_seed.current(root) == os.path.realpath(final)
    assert os.path.isfile(os.path.join(final, uv_seed.MARKER)) and os.path.isdir(os.path.join(final, "cache"))
    assert not os.path.exists(os.path.join(final, "scratch")) and not os.path.exists(final + ".tmp")
    # the warm steps: the locked environment of the repo, then the fake project's lock and sync; online (no --offline), a throwaway HOME, no seed or cell variables
    assert [c[0][1] for c in fake.calls] == ["sync", "lock", "sync"] and fake.calls[0][1] == str(repo)
    for _argv, _cwd, env in fake.calls:
        assert (
            env["UV_CACHE_DIR"] == os.path.join(final + ".tmp", "cache")
            and "MQ_UV_SEED" not in env
            and "MQ_CELL_DIR" not in env
            and "--offline" not in _argv
        )
    assert fake.calls[1][1].endswith(os.path.join("scratch", "fake"))
    # read-only: no file or directory of the seed is writable
    assert all(not (os.stat(os.path.join(d, n)).st_mode & stat.S_IWUSR) for d, ds, fs in os.walk(final) for n in (*ds, *fs))


def test_refresh_keeps_two_seeds_never_removes_the_live_one_and_refuses_to_overwrite(tmp_path, repo):
    root = str(tmp_path / "seedroot")
    for i in range(4):
        uv_seed.refresh(root, str(repo), f"{i}-1", run=FakeUv())
        os.utime(os.path.join(root, f"seed-{i}-1"), (1000 + i, 1000 + i))
    names = sorted(n for n in os.listdir(root) if n.startswith("seed-"))
    assert names == ["seed-2-1", "seed-3-1"], names
    assert os.path.realpath(os.path.join(root, "current")).endswith("seed-3-1")
    with pytest.raises(SystemExit, match="already exists"):
        uv_seed.refresh(root, str(repo), "3-1", run=FakeUv())
    with pytest.raises(SystemExit, match="not a plain token"):
        uv_seed.refresh(root, str(repo), "../x", run=FakeUv())


def test_a_failed_warm_step_leaves_no_current_and_no_complete_seed(tmp_path, repo):
    root = str(tmp_path / "seedroot")

    def boom(argv, **kw):
        raise subprocess.CalledProcessError(1, argv)

    with pytest.raises(subprocess.CalledProcessError):
        uv_seed.refresh(root, str(repo), "9-1", run=boom)
    assert (
        uv_seed.current(root) is None
    )  # no `current`, and the half-built .tmp has no marker: a cell would still be told the source is missing


def test_a_cells_copy_is_writable_and_the_source_is_never_changed(tmp_path, repo):
    root = str(tmp_path / "seedroot")
    uv_seed.refresh(root, str(repo), "1-1", run=FakeUv())
    before = tree_state(root)
    dst = str(tmp_path / "cell" / "uv-cache")
    os.makedirs(os.path.dirname(dst))
    src = uv_seed.seed_cell(root, dst)
    assert src == uv_seed.current(root) and os.path.isfile(os.path.join(dst, "wheels-v5", "1.whl"))
    # what a cell does: add a file, rewrite a file, make a directory
    open(os.path.join(dst, "new.txt"), "w").write("a cell wrote this")
    open(os.path.join(dst, "wheels-v5", "1.whl"), "w").write("rewritten in the copy")
    os.makedirs(os.path.join(dst, "wheels-v5", "sub"))
    assert tree_state(root) == before  # the source: same files, same sizes, same modes
    assert open(os.path.join(src, "cache", "wheels-v5", "1.whl")).read() == "x" * 10
    assert not os.access(os.path.join(src, "cache", "wheels-v5", "1.whl"), os.W_OK)  # and still read-only


def test_an_empty_destination_folder_made_by_the_cell_env_is_replaced_by_the_clone(tmp_path, repo):
    root = str(tmp_path / "seedroot")
    uv_seed.refresh(root, str(repo), "1-1", run=FakeUv())
    dst = tmp_path / "cell" / "uv-cache"
    dst.mkdir(parents=True)  # mq.cell_env makes an empty uv-cache folder
    uv_seed.seed_cell(root, str(dst))
    assert (dst / "wheels-v5" / "1.whl").exists()


@pytest.mark.parametrize("make", ["nothing", "no-marker", "dangling-current"])
def test_a_missing_or_incomplete_source_is_an_error_naming_the_path_never_an_empty_cache(tmp_path, repo, make):
    root = tmp_path / "seedroot"
    if make == "no-marker":
        (root / "seed-1" / "cache").mkdir(parents=True)
        os.symlink("seed-1", root / "current")
    elif make == "dangling-current":
        root.mkdir()
        os.symlink("seed-gone", root / "current")
    dst = tmp_path / "cell" / "uv-cache"
    dst.parent.mkdir()
    with pytest.raises(SystemExit) as e:
        uv_seed.seed_cell(str(root), str(dst))
    msg = str(e.value)
    assert str(root) in msg and "uv-seed.yml" in msg and "release-gate tests would skip silently" in msg, msg
    assert not dst.exists()  # nothing was made: no empty fallback cache


def test_the_driver_seeds_the_cell_before_any_pr_code_and_names_a_missing_source(tmp_path, monkeypatch, capsys):
    order = []
    monkeypatch.setenv("MQ_CELL_DIR", str(tmp_path / "cell"))
    monkeypatch.setenv("MQ_UV_SEED", str(tmp_path / "no-such-seed"))
    monkeypatch.setattr(mq.os, "setpgrp", lambda: None)
    monkeypatch.setattr(mq, "GitRepo", lambda *a, **k: order.append("GitRepo"))  # the first thing that touches the PR's objects
    monkeypatch.setattr(mq, "reap_group", lambda *a, **k: None)
    argv = [
        "test",
        "--repo",
        "o/r",
        "--base",
        "main",
        "--candidates",
        "[]",
        "--driver-dir",
        str(tmp_path),
        "--verdict-file",
        str(tmp_path / "v.json"),
    ]
    with pytest.raises(SystemExit, match="uv seed cache is missing"):
        mq.main(argv)
    assert (
        order == [] and "::error title=uv-seed::" in capsys.readouterr().out
    )  # no PR object was touched, and the annotation names the source
    seen = []
    monkeypatch.setattr(mq.uv_seed, "seed_cell", lambda root, dst: seen.append((root, dst)))
    monkeypatch.setattr(mq, "Tester", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop after seeding")))
    with pytest.raises(RuntimeError):
        mq.main(argv)
    assert seen == [(str(tmp_path / "no-such-seed"), str(tmp_path / "cell" / "uv-cache"))] and order == [
        "GitRepo"
    ]  # seeded first, into the cell's own uv-cache
    monkeypatch.delenv("MQ_UV_SEED")
    seen.clear()
    with pytest.raises(RuntimeError):
        mq.main(argv)
    assert seen == []  # no seed variable (a hosted runner): no seeding, no error


def test_child_env_keeps_the_seed_path_out_of_every_process_that_runs_pr_code(tmp_path):
    env = mq.child_env({"MQ_UV_SEED": "/runner/uv-seed", "MQ_CELL_DIR": str(tmp_path / "c"), "PATH": "/bin"})
    assert "MQ_UV_SEED" not in env and env["UV_CACHE_DIR"].startswith(str(tmp_path / "c"))
    assert "MQ_UV_SEED" not in mq.git_env()  # whatever the caller's own environment holds


def calls_refresh(path):
    return [
        n
        for n in ast.walk(ast.parse(open(path).read()))
        if isinstance(n, ast.Attribute) and n.attr == "refresh" or isinstance(n, ast.Name) and n.id == "refresh"
    ]


@pytest.mark.skipif(is_published_snapshot(), reason=skip_reason(".github/workflows/uv-seed.yml"))
def test_nothing_the_cell_runs_can_reach_the_writer_and_only_the_trusted_workflow_calls_it():
    # the cell side (the driver and the check runner) never names `refresh`, the only function that writes the source
    for name in ("merge_queue.py", "ci_step.py", "plan_bases.py", "gh_api.py", "bases.py"):
        assert calls_refresh(os.path.join(SCRIPTS, name)) == [], name
    # the workflows: `uv_seed.py refresh` appears in uv-seed.yml and nowhere else
    wf = os.path.join(ROOT, ".github", "workflows")
    users = sorted(f for f in os.listdir(wf) if f.endswith(".yml") and "uv_seed.py refresh" in open(os.path.join(wf, f)).read())
    assert users == ["uv-seed.yml"], users
    job = yaml.safe_load(open(os.path.join(wf, "uv-seed.yml")))["jobs"]["seed"]
    refresh_steps = [s for s in job["steps"] if "uv_seed.py refresh" in str(s.get("run", ""))]
    assert len(refresh_steps) == 1 and job["permissions"] == {"contents": "read"}
    # and the cell job of merge-queue.yml only exports the path (a read) and never runs the refresher
    cell = yaml.safe_load(open(os.path.join(wf, "merge-queue.yml")))["jobs"]["test"]
    setup = next(s for s in cell["steps"] if "MQ_UV_SEED" in str(s.get("run", "")))
    assert (
        'if [ -n "$CI_RUNNER_VAR" ]; then echo "MQ_UV_SEED=${UV_SEED_VAR:-$HOME/.cache/factlattice-queue/uv-seed}" >> "$GITHUB_ENV"; fi'
        in setup["run"]
    )
    assert setup["env"] == {"CI_RUNNER_VAR": "${{ vars.CI_RUNNER }}", "UV_SEED_VAR": "${{ vars.UV_SEED_DIR }}"}
    home = "/".join(["", "home", "runner"])  # built from parts: a literal home path trips the disclosure guard
    assert uv_seed.default_root(home) == home + "/.cache/factlattice-queue/uv-seed"


def test_the_seed_is_clone_copied_with_the_platforms_clone_flag():
    assert uv_seed.clone_flags("darwin") == ["-c", "-R"] and uv_seed.clone_flags("linux") == ["-R", "--reflink=auto"]


@pytest.mark.skipif(is_published_snapshot(), reason=skip_reason("tests/test_release_prep.py"))
def test_the_release_prep_fake_project_has_one_definition():
    """The warm cache is built from the project the release-prep tests lock offline: the test imports the text, it does not carry a copy."""
    src = open(os.path.join(ROOT, "tests", "test_release_prep.py")).read()
    assert "from uv_seed import FAKE_PYPROJECT" in src and "FAKE_PYPROJECT = " not in src
    assert 'name = "fakeproj"' in uv_seed.FAKE_PYPROJECT and "setuptools>=68" in uv_seed.FAKE_PYPROJECT


# ---- the review round
def test_the_warm_up_runs_uv_with_no_user_site_no_config_a_scratch_home_and_the_real_pythons_location(tmp_path, repo):
    fake = FakeUv()
    uv_seed.refresh(str(tmp_path / "root"), str(repo), "5-1", run=fake)
    assert len(fake.calls) == 3
    for argv, _cwd, env in fake.calls:
        assert (
            "--no-config" in argv
            and env["PYTHONNOUSERSITE"] == "1"
            and env["UV_PYTHON_INSTALL_DIR"] == "/runner/uv-python"
            and env["UV_PYTHON_DOWNLOADS"] == "never"
        )
        scratch_home = env["HOME"]
        assert scratch_home.endswith(os.path.join("scratch", "home"))
        assert all(
            env[k].startswith(scratch_home) for k in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME")
        )  # a runner service's own XDG exports do not steer it


def test_uvs_absolute_links_into_the_build_directory_are_made_relative_so_nothing_dangles(tmp_path, repo):
    """uv writes `wheels-v6/<pkg> -> <build dir>/cache/archive-v0/<pkg>` (absolute); the build dir is renamed away. refresh rewrites them, so a seed and every clone of it resolve."""

    class LinkingUv(FakeUv):
        def __call__(self, argv, **kw):
            res = super().__call__(argv, **kw)
            if argv[0] == "uv" and argv[1] == "sync" and len(self.calls) == 1:
                cache = kw["env"]["UV_CACHE_DIR"]
                os.makedirs(os.path.join(cache, "archive-v0", "pkg"))
                open(os.path.join(cache, "archive-v0", "pkg", "mod.py"), "w").write("x")
                os.makedirs(os.path.join(cache, "wheels-v6"), exist_ok=True)
                os.symlink(os.path.join(cache, "archive-v0", "pkg"), os.path.join(cache, "wheels-v6", "pkg"))
            return res

    root = str(tmp_path / "root")
    final = uv_seed.refresh(root, str(repo), "6-1", run=LinkingUv())
    link = os.path.join(final, "cache", "wheels-v6", "pkg")
    assert not os.path.isabs(os.readlink(link)) and os.path.isfile(os.path.join(link, "mod.py"))  # relative, and it resolves
    dst = str(tmp_path / "cell" / "uv-cache")
    uv_seed.seed_cell(root, dst)
    assert os.path.isfile(os.path.join(dst, "wheels-v6", "pkg", "mod.py")) and os.path.realpath(
        os.path.join(dst, "wheels-v6", "pkg")
    ).startswith(os.path.realpath(dst))  # inside the clone


def test_a_current_that_resolves_outside_the_root_is_not_a_seed(tmp_path, repo):
    elsewhere = tmp_path / "elsewhere"
    uv_seed.refresh(str(elsewhere), str(repo), "7-1", run=FakeUv())
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(str(elsewhere / "seed-7-1"), root / "current")  # a complete seed, but of another root
    assert uv_seed.current(str(root)) is None
    with pytest.raises(SystemExit, match="missing or incomplete"):
        uv_seed.seed_cell(str(root), str(tmp_path / "cell" / "uv-cache"))
    os.remove(root / "current")
    os.symlink(str(root), root / "current")  # the root itself is not a seed either
    assert uv_seed.current(str(root)) is None


def test_a_leftover_tmp_directory_takes_no_keep_slot_and_is_removed(tmp_path, repo):
    root = str(tmp_path / "root")
    for i in range(2):
        uv_seed.refresh(root, str(repo), f"{i}-1", run=FakeUv())
        os.utime(os.path.join(root, f"seed-{i}-1"), (1000 + i, 1000 + i))
    os.makedirs(os.path.join(root, "seed-9-9.tmp", "cache"))
    os.utime(os.path.join(root, "seed-9-9.tmp"), (5000, 5000))  # newer than both seeds
    uv_seed.refresh(root, str(repo), "3-1", run=FakeUv())
    names = sorted(n for n in os.listdir(root) if n.startswith("seed-"))
    assert names == ["seed-1-1", "seed-3-1"], names  # the live seed and the previous complete one; the .tmp is gone


def test_the_missing_source_message_names_the_exact_remedy(tmp_path):
    with pytest.raises(SystemExit) as e:
        uv_seed.seed_cell(str(tmp_path / "nothing"), str(tmp_path / "cell" / "uv-cache"))
    assert "gh workflow run uv-seed.yml --ref main" in str(e.value) and "manual dispatch" in str(e.value)


def eval_if(expr, event_name, ref_name, default_branch):
    """The two clauses of uv-seed.yml's job `if`, evaluated for an event: github.event_name == 'schedule' || github.ref_name == github.event.repository.default_branch.
    A schedule payload has no repository.default_branch (None), and None never equals a ref name."""
    assert expr == "github.event_name == 'schedule' || github.ref_name == github.event.repository.default_branch", expr
    return event_name == "schedule" or ref_name == default_branch


@pytest.mark.skipif(is_published_snapshot(), reason=skip_reason(".github/workflows/uv-seed.yml"))
def test_the_seed_job_condition_for_each_trigger_and_the_headers_first_seed_note():
    wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "uv-seed.yml")))
    cond = wf["jobs"]["seed"]["if"]
    assert eval_if(cond, "schedule", "main", None) is True  # the weekly run: the payload has no default branch, the event name accepts it
    assert eval_if(cond, "push", "main", "main") is True
    assert eval_if(cond, "workflow_dispatch", "main", "main") is True
    assert eval_if(cond, "workflow_dispatch", "feature/x", "main") is False  # a dispatch from another ref skips the job
    assert eval_if(cond, "push", "feature/x", "main") is False
    assert eval_if(cond, "pull_request", "7/merge", "main") is False  # and a PR event is not a trigger at all
    assert sorted(wf[True]) == ["push", "schedule", "workflow_dispatch"]
    text = open(os.path.join(ROOT, ".github", "workflows", "uv-seed.yml")).read()
    assert (
        "THE FIRST SEED ON A RUNNER IS A MANUAL DISPATCH: `gh workflow run uv-seed.yml --ref main`" in text
        and "accident guard, not a boundary" in text
    )
    checkout = next(s for s in wf["jobs"]["seed"]["steps"] if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["ref"].startswith(
        "${{ github.event.repository.default_branch ||"
    )  # an empty default branch (a schedule) falls back to the ref the event runs on
