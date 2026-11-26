"""What the merge job trusts (a reviewer's probes, made into tests; all against the fake GitHub API of tests/test_merge_queue.py, with the REAL 40-hex shapes).

WHAT THIS GUARANTEES, stated narrowly. No PR's verdict FILE decides anything: green or red comes from GitHub's record of the PR's own planned test job (the jobs API of
this run and attempt, found by the cell name `merge-queue-test (pr-<n> <40-hex head>)`), so a merge needs its own planned job's conclusion. Only planned (int pr,
40-hex head) candidates are acted on; unplanned and fork-head PRs never merge (fork heads are dropped at planning and refused again in on_green, before any status or
merge). A failure's cause and check are mapped to known names before they reach a comment or an annotation. The base is re-read before any `merge-queue/tested`
success, and a head that ends a sweep unmerged carries no success.
WHAT IT DOES NOT CLAIM. Stopping a PR from passing its own tests. Isolation of one PR's code from another's on a persistent self-hosted runner: a deliberately persisting
PR (its home, caches, user site, a surviving process, the runner's own folders) can affect later cells; that needs ephemeral runners. The per-cell fresh HOME, XDG_CACHE|CONFIG|DATA_HOME,
UV_CACHE_DIR and TMPDIR, no user site and the reap after the driver (its own process group and the session each check runs in) remove accidental carry-over only; a
process that starts yet another session (a daemon) is outside the reap.
What only a live run proves: the jobs API's field values on GitHub (name, status, conclusion) and the artifact folder layout.
"""

import json
import os
import subprocess
import sys

import pytest

from tests.queue_fakes import FakeGH

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, ".github", "workflows", "scripts"))
import merge_queue as mq  # noqa: E402

BASE = "b" * 40


def head(n):
    return str(n) * 40


def world(*numbers):
    """A fake GitHub whose queued PRs have 40-hex heads (n repeated) and whose base is BASE."""
    gh = FakeGH()
    gh.base = BASE
    for n in numbers:
        gh.add(n)
        gh.force_push(n, head(n))
    return gh


class Sweep:
    """One merge-job run: the plan's candidates, GitHub's job record, and each cell's verdict artifact."""

    def __init__(self, tmp_path, monkeypatch, capsys, gh, attempt="1"):
        self.tmp, self.mp, self.capsys, self.gh = tmp_path, monkeypatch, capsys, gh
        self.cands = {}
        monkeypatch.setenv("GITHUB_RUN_ID", "77")
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", attempt)
        monkeypatch.delenv("MQ_HOP", raising=False)
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        monkeypatch.setattr(mq, "Api", lambda repo: gh)

    def plan(self, *numbers, base="main"):
        self.cands.setdefault(base, []).extend({"n": n, "head": head(n)} for n in numbers)
        return self

    def job(self, n, conclusion, name=None, status="completed", h=None):
        self.gh.jobs.append({"name": name or mq.test_job_name(n, h or head(n)), "status": status, "conclusion": conclusion})
        return self

    def verdict(self, n, result="green", base_sha=BASE, failures=(), pr=None, h=None, directory=None, **extra):
        hd = h or head(n)
        merge_sha = f"M[{hd}+{base_sha}]"
        t = {
            "pr": n if pr is None else pr,
            "head": hd,
            "base_sha": base_sha,
            "merge_sha": merge_sha,
            "tree_sha": f"T[{merge_sha}]",
            "result": result,
            "failures": list(failures),
        }
        t.update(extra)
        d = self.tmp / "v" / (directory or f"mq-verdict-{n}")
        d.mkdir(parents=True, exist_ok=True)
        (d / "verdict.json").write_text(json.dumps({"base": "main", "tests": [t]}))
        return self

    def merge(self, candidates=None):
        mq.main(
            [
                "merge",
                "--repo",
                "o/r",
                "--verdict-dir",
                str(self.tmp / "v"),
                "--candidates",
                json.dumps(self.cands if candidates is None else candidates),
            ]
        )
        return self.capsys.readouterr().out

    def stalls(self):
        """The sweep in which no planned name is found among the jobs of this attempt: the merge job goes red with the reason, after acting on nothing."""
        with pytest.raises(SystemExit, match="no test job of this attempt has a planned"):
            self.merge()
        return self.capsys.readouterr().out

    def writes(self):
        """Every write call except a status POST: a head that is not merged gets a `pending` or `failure` status (see successes() for the success ones)."""
        return [c for c in self.gh.calls if c[0] in ("PUT", "POST", "DELETE") and not c[1].startswith("/statuses/")]


def successes(gh):
    return [(sha, s) for sha, s in gh.statuses if s["state"] == "success"]


def sweep(tmp_path, monkeypatch, capsys, *numbers, attempt="1"):
    (tmp_path / "v").mkdir(exist_ok=True)
    return Sweep(tmp_path, monkeypatch, capsys, world(*numbers), attempt)


# ---- the merge still works
def test_a_planned_candidate_whose_job_passed_merges(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "success").verdict(1)
    out = s.merge()
    assert s.gh.merged == [1] and "dequeued" not in out, out
    assert s.gh.jobs_reads == [("77", "1")]  # this run and this attempt, nothing else


def test_two_green_candidates_on_one_base_merge_one_and_defer_the_other_to_the_next_sweep(tmp_path, monkeypatch, capsys):
    """Each was tested on BASE; the first merge moves the base, so the second's tested base is stale: on_green's base check defers it (it stays queued)."""
    s = sweep(tmp_path, monkeypatch, capsys, 1, 2).plan(1, 2).job(1, "success").verdict(1).job(2, "success").verdict(2)
    s.merge()
    assert s.gh.merged == [1] and "queue" in s.gh.prs[2]["labels"]


# ---- the job's conclusion decides, not the verdict file
def test_a_forged_green_verdict_for_a_pr_whose_job_failed_is_dequeued_not_merged(tmp_path, monkeypatch, capsys):
    """Two candidates on one base. B (#1, first in the plan) has a FAILED test job; a green verdict for B was written (in B's own folder, and again inside A's
    artifact, as PR A's code could). A (#2) passed. The conclusion decides: B is dequeued as red, A merges."""
    s = sweep(tmp_path, monkeypatch, capsys, 1, 2).plan(1, 2).job(1, "failure").verdict(1, result="green").job(2, "success").verdict(2)
    forged = json.loads((tmp_path / "v" / "mq-verdict-1" / "verdict.json").read_text())["tests"][0]
    own = json.loads((tmp_path / "v" / "mq-verdict-2" / "verdict.json").read_text())["tests"][0]
    (tmp_path / "v" / "mq-verdict-2" / "verdict.json").write_text(
        json.dumps({"base": "main", "tests": [own, forged]})
    )  # A's artifact also claims B is green
    out = s.merge()
    assert s.gh.merged == [2], (s.gh.merged, out)
    assert "queue" not in s.gh.prs[1]["labels"]  # B dequeued as red, with a comment
    assert any(n == 1 and "test-step" in b for n, b in s.gh.comments), s.gh.comments
    assert "dequeued-red" in out


def test_a_pr_that_exits_non_zero_and_writes_a_green_verdict_is_dequeued_not_merged(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "failure").verdict(1, result="green")
    out = s.merge()
    assert s.gh.merged == [] and successes(s.gh) == [] and "queue" not in s.gh.prs[1]["labels"]
    assert "dequeued-red" in out


def test_a_job_that_passed_merges_even_when_its_verdict_file_says_red(tmp_path, monkeypatch, capsys):
    """The conclusion decides. A forged red verdict (here: failures with free text) does not drop a PR whose job passed; the tested base is still taken from the file."""
    s = (
        sweep(tmp_path, monkeypatch, capsys, 1)
        .plan(1)
        .job(1, "success")
        .verdict(1, result="red", failures=[{"check": "checks", "cause": "x"}])
    )
    s.merge()
    assert s.gh.merged == [1]


@pytest.mark.parametrize("bad_base", [None, "", "abc", "G" * 40, "b" * 39, "b" * 41, ("b" * 40).upper(), 7])
def test_a_job_that_passed_with_no_usable_tested_base_is_deferred_not_merged_and_not_silent(tmp_path, monkeypatch, capsys, bad_base):
    """A merge needs the base the merge was tested on, which only the verdict file records. Without a usable one the PR is NOT merged and stays queued, and the log says so."""
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "success").verdict(1, base_sha=bad_base)
    out = s.merge()
    assert s.gh.merged == [] and s.writes() == [] and "queue" in s.gh.prs[1]["labels"]
    assert "#1" in out and "deferred" in out


def test_a_missing_or_garbled_verdict_file_is_deferred_not_merged_and_not_silent(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1, 2).plan(1, 2).job(1, "success").job(2, "success")
    (tmp_path / "v" / "mq-verdict-2").mkdir()
    (tmp_path / "v" / "mq-verdict-2" / "verdict.json").write_text("{not json")
    out = s.merge()
    assert s.gh.merged == [] and s.writes() == []
    assert "#1" in out and "#2" in out and out.count("deferred") >= 2


def test_a_planned_candidate_with_no_job_in_this_attempt_is_deferred_and_listed(tmp_path, monkeypatch, capsys):
    s = (
        sweep(tmp_path, monkeypatch, capsys, 1, 2).plan(1, 2).verdict(1).job(2, "success").verdict(2)
    )  # #1: a verdict file, but GitHub has no job for it in this attempt
    out = s.merge()
    assert (
        s.gh.merged == [2] and "queue" in s.gh.prs[1]["labels"] and "#1: planned, but no test job named for it ran in this attempt" in out
    )


def test_only_the_jobs_of_this_run_and_attempt_are_read(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1, attempt="2").plan(1).job(1, "success").verdict(1)
    s.merge()
    assert s.gh.jobs_reads == [("77", "2")]


@pytest.mark.parametrize(
    "status,conclusion",
    [("in_progress", None), ("completed", "cancelled"), ("completed", "skipped"), ("completed", None), ("queued", None)],
)
def test_a_job_without_a_usable_conclusion_is_not_merged_and_not_dequeued(tmp_path, monkeypatch, capsys, status, conclusion):
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, conclusion, status=status).verdict(1)
    s.merge()
    assert s.gh.merged == [] and s.writes() == [] and "queue" in s.gh.prs[1]["labels"]


def test_two_jobs_with_the_same_name_decide_nothing(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "success").job(1, "success").verdict(1)
    s.merge()
    assert s.gh.merged == [] and s.writes() == []


# ---- (2) unplanned, fork heads, wrong heads, malformed identities
def test_a_forged_verdict_and_job_for_a_pr_that_was_never_planned_merge_nothing(tmp_path, monkeypatch, capsys):
    s = (
        sweep(tmp_path, monkeypatch, capsys, 1, 9).plan(1).job(1, "success").verdict(1).job(9, "success").verdict(9)
    )  # #9 is queued: GitHub would let the queue merge it; the plan did not name it
    s.merge()
    assert s.gh.merged == [1] and [sha for sha, _ in s.gh.statuses] == [head(1)] and "queue" in s.gh.prs[9]["labels"], s.gh.calls


def test_a_planned_pr_whose_head_is_in_a_fork_is_refused_before_any_status_or_merge(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "success").verdict(1)
    s.gh.prs[1]["head_repo"] = "stranger/fork"
    out = s.merge()
    assert s.gh.merged == [] and successes(s.gh) == []
    assert [c for c in s.gh.calls if c[0] == "PUT" and c[1].endswith("/merge")] == []
    assert "queue" not in s.gh.prs[1]["labels"] and any("not in this repository" in b for _, b in s.gh.comments), s.gh.comments
    assert "dequeued-fork-head" in out


def test_a_head_repository_that_is_null_is_a_fork_head_too(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "success").verdict(1)
    real = s.gh.pr_json
    s.gh.pr_json = lambda p: {**real(p), "head": {"sha": p["head"], "repo": None}}  # a deleted fork
    s.merge()
    assert s.gh.merged == [] and successes(s.gh) == []


def test_a_job_named_for_another_head_than_the_planned_one_counts_for_nothing(tmp_path, monkeypatch, capsys):
    s = (
        sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "success", h="9" * 40).verdict(1)
    )  # the job names pr-1 with a head the plan never had
    out = s.stalls()
    assert s.gh.merged == [] and s.writes() == [] and "#1: planned, but no test job named for it ran" in out


@pytest.mark.parametrize(
    "name",
    [
        "merge-queue-test (pr-1 " + "1" * 39 + ")",
        "merge-queue-test (pr-1 " + "G" * 40 + ")",
        "merge-queue-test (pr-1/../2 " + "1" * 40 + ")",
        "merge-queue-test (pr-1 " + "1" * 40 + ") x",
        "merge-queue-test",
        "x merge-queue-test (pr-1 " + "1" * 40 + ")",
    ],
)
def test_a_job_name_that_does_not_parse_counts_for_nothing(tmp_path, monkeypatch, capsys, name):
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "success", name=name).verdict(1)
    s.stalls()  # jobs exist but none parses as a planned name: red with the reason, not a silent stall
    assert s.gh.merged == [] and s.writes() == []


@pytest.mark.parametrize("pr", ["1", "12/../../pulls/13", 1.0, True, None, [1]])
def test_a_candidate_that_is_not_an_int_is_dropped_and_reaches_no_write_path(tmp_path, monkeypatch, capsys, pr):
    s = sweep(tmp_path, monkeypatch, capsys, 1).job(1, "failure").verdict(1, result="red")
    s.merge(candidates={"main": [{"n": pr, "head": head(1)}]})
    assert s.gh.merged == [] and s.writes() == []


@pytest.mark.parametrize("bad", ["abc", "G" * 40, "1" * 39, "1" * 41, "A" * 40, "", None, 7])
def test_a_candidate_whose_head_is_not_40_hex_is_dropped(tmp_path, monkeypatch, capsys, bad):
    s = sweep(tmp_path, monkeypatch, capsys, 1).job(1, "success").verdict(1)
    s.merge(candidates={"main": [{"n": 1, "head": bad}]})
    assert s.gh.merged == [] and s.writes() == []


def test_the_merge_command_refuses_to_run_without_the_plans_candidates_or_the_run_ids(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1).job(1, "success").verdict(1)
    with pytest.raises(SystemExit):
        mq.main(["merge", "--repo", "o/r", "--verdict-dir", str(tmp_path / "v")])  # no --candidates: fail closed
    monkeypatch.delenv("GITHUB_RUN_ID")
    with pytest.raises(SystemExit, match="GITHUB_RUN_ID"):
        s.plan(1).merge()  # the jobs of this run cannot be read: nothing is decided
    assert s.gh.merged == []


# ---- (3) free text
def test_a_free_text_cause_or_check_is_mapped_and_never_printed_or_commented(tmp_path, monkeypatch, capsys):
    evil = "x` [click](https://example.invalid) @someone `\n::set-output name=merged::true"
    s = (
        sweep(tmp_path, monkeypatch, capsys, 1)
        .plan(1)
        .job(1, "failure")
        .verdict(1, result="red", failures=[{"check": evil, "cause": evil}])
    )
    out = s.merge()
    assert s.gh.comments
    for _, body in s.gh.comments:
        assert "example.invalid" not in body and "someone" not in body and "set-output" not in body, body
        assert "failed: `unknown-check`, cause: `unclassified`" in body, body
    assert "example.invalid" not in out and "set-output" not in out
    assert "::error title=failure-cause::cause=unclassified job=merge-queue pr=1 check=unknown-check" in out, out


def test_the_known_causes_and_checks_pass_through(tmp_path, monkeypatch, capsys):
    s = (
        sweep(tmp_path, monkeypatch, capsys, 1)
        .plan(1)
        .job(1, "failure")
        .verdict(1, result="red", failures=[{"check": "checks", "cause": "timeout"}])
    )
    out = s.merge()
    assert "cause=timeout job=merge-queue pr=1 check=checks" in out and "cause: `timeout`" in s.gh.comments[0][1]


def test_a_free_text_retry_id_is_dropped(tmp_path, monkeypatch, capsys):
    retry = {
        "retries": 1,
        "failed_ids": ["tests/a.py::t", "x`@someone`\n::set-output"],
        "flaked_ids": ["tests/a.py::t", "[click](https://example.invalid)"],
    }
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "success").verdict(1, retry=retry)
    s.merge()
    blob = json.dumps(s.gh.comments)
    assert "someone" not in blob and "set-output" not in blob and "tests/a.py::t" in blob


# ---- a head that is not merged carries no success status
def test_two_greens_in_one_sweep_merge_one_and_leave_no_success_on_the_other_head(tmp_path, monkeypatch, capsys):
    """The reviewer's probe p1: #1 and #2 are green on BASE; #1 merges and moves the base. #2's head must not end the sweep with `merge-queue/tested` = success."""
    s = sweep(tmp_path, monkeypatch, capsys, 1, 2).plan(1, 2).job(1, "success").verdict(1).job(2, "success").verdict(2)
    out = s.merge()
    assert s.gh.merged == [1]
    on_2 = [st for sha, st in s.gh.statuses if sha == head(2)]
    assert on_2 and all(st["state"] != "success" for st in on_2), on_2  # nothing but a pending "base moved; re-test queued"
    assert on_2[-1]["state"] == "pending" and "base moved" in on_2[-1]["description"]
    assert [st["state"] for sha, st in s.gh.statuses if sha == head(1)] == ["success"]  # the merged head keeps its success
    assert "| main | 2 | 2 | 1 | #2 |" in out, out  # the sweep summary: planned, tested, merged, greens left unused


def test_a_red_head_gets_a_failure_status_and_a_refused_merge_leaves_no_success(tmp_path, monkeypatch, capsys):
    s = (
        sweep(tmp_path, monkeypatch, capsys, 1)
        .plan(1)
        .job(1, "failure")
        .verdict(1, result="red", failures=[{"check": "checks", "cause": "timeout"}])
    )
    s.merge()
    assert [st["state"] for _, st in s.gh.statuses] == ["failure"]
    (tmp_path / "b").mkdir()
    s = sweep(tmp_path / "b", monkeypatch, capsys, 1).plan(1).job(1, "success").verdict(1)
    s.gh.merge_status[1] = (422, "not mergeable")
    s.merge()
    assert s.gh.merged == [] and [st["state"] for _, st in s.gh.statuses] == [
        "success",
        "failure",
    ]  # the success was overwritten when the merge was refused


def test_a_deferred_merge_after_a_5xx_leaves_pending_not_success(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "success").verdict(1)
    s.gh.merge_status[1] = (502, "bad gateway")
    s.merge()
    assert s.gh.merged == [] and [st["state"] for _, st in s.gh.statuses][-1] == "pending"


# ---- a stale verdict is believed only if the API's head differs from the planned head
def test_a_stale_verdict_defers_only_when_the_api_head_moved(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "failure").verdict(1, result="stale")
    out = s.merge()
    assert "dequeued-red" in out and "queue" not in s.gh.prs[1]["labels"]  # the head did not move: `stale` was the verdict file's word only
    (tmp_path / "b").mkdir()
    s = sweep(tmp_path / "b", monkeypatch, capsys, 1).plan(1).job(1, "failure").verdict(1, result="stale")
    s.gh.prs[1]["head"] = "f" * 40  # the author pushed: now it is stale for real
    out = s.merge()
    assert "dequeued" not in out and "queue" in s.gh.prs[1]["labels"]


# ---- the driver's own setup names, and the crash wording
@pytest.mark.parametrize(
    "name", ["setup(uv sync --locked --extra test --extra semantic)", "setup(git worktree add)", "checks", "git fetch", "test-step"]
)
def test_the_drivers_own_check_names_reach_the_comment(name):
    assert mq.known_check(name) == name


@pytest.mark.parametrize("name", ["setup(rm -rf /)", "setup(uv sync)", "git push", "x", None, 7])
def test_any_other_check_name_is_unknown(name):
    assert mq.known_check(name) == "unknown-check"


def test_a_failed_job_with_no_recorded_result_says_what_it_is(tmp_path, monkeypatch, capsys):
    s = (
        sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "failure")
    )  # no verdict file at all: a crash or a setup error before the driver wrote one
    s.merge()
    body = s.gh.comments[0][1]
    assert "failed before it recorded a result" in body and "git step" not in body, body


# ---- per-cell environment, reaped process group
CELL_KEYS = ("HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "UV_CACHE_DIR", "TMPDIR")  # XDG_STATE_HOME is not one of them (see cell_env)


def run_default_checks(tmp_path, monkeypatch, cell):
    seen = {}

    def fake_run(cmd, **kw):
        if cmd[0] == "git" and "worktree" in cmd and "add" in cmd:
            seen["worktree add"] = kw.get("env")
        if cmd[0] == "uv":
            seen["uv sync"] = kw.get("env")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def fake_check(name, tree, env=None, **kw):
        seen[name] = env  # ci_step.run_check hands this to isolated_env, which keeps every other variable
        return []

    monkeypatch.setattr(mq.subprocess, "run", fake_run)
    monkeypatch.setattr(mq.ci_step, "run_check", fake_check)
    monkeypatch.setenv("GITHUB_TOKEN", "tok-secret")
    for k in (*CELL_KEYS, "PYTHONNOUSERSITE", "MQ_CELL_DIR", "MQ_PYDIR"):
        monkeypatch.delenv(k, raising=False)
    if cell:
        monkeypatch.setenv("MQ_CELL_DIR", str(cell))
        monkeypatch.setenv("MQ_PYDIR", "/trusted/uv-python")
    driver = tmp_path / "driver"
    driver.mkdir()
    mq.default_run_checks(str(driver), "main")(1, "a" * 40)
    return seen


def test_the_per_cell_environment_reaches_uv_sync_and_every_check(tmp_path, monkeypatch):
    cell = tmp_path / "cell"
    seen = run_default_checks(tmp_path, monkeypatch, cell)
    assert set(seen) == {"worktree add", "uv sync", "checks", "gitleaks", "smoke"}, seen.keys()
    for call, env in seen.items():
        for k in CELL_KEYS:
            assert env[k].startswith(str(cell) + os.sep) and os.path.isdir(env[k]), (call, k, env.get(k))
        assert env["PYTHONNOUSERSITE"] == "1" and env["UV_PYTHON_INSTALL_DIR"] == "/trusted/uv-python", call
        assert "GITHUB_TOKEN" not in env and "tok-secret" not in json.dumps(env), call  # the token is still stripped
    assert len({env["HOME"] for env in seen.values()}) == 1  # one fresh home for the whole cell


def test_without_a_cell_folder_the_environment_is_untouched(tmp_path, monkeypatch):
    seen = run_default_checks(tmp_path, monkeypatch, None)
    assert all("PYTHONNOUSERSITE" not in env and env.get("HOME") == os.environ.get("HOME") for env in seen.values())


def test_the_process_group_is_reaped_after_the_driver(tmp_path):
    """A child left in the driver's group (what a PR's tests may leave behind) is killed; the driver itself survives. A child that starts its own session is outside it (stated)."""
    code = (
        "import os, subprocess, sys, time\n"
        f"sys.path.insert(0, {os.path.join(ROOT, '.github', 'workflows', 'scripts')!r})\n"
        "import merge_queue as mq\n"
        "os.setpgrp()\n"
        "kid = subprocess.Popen(['sleep', '60'])\n"
        "other = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
        "mq.reap_group(grace_s=0.3)\n"
        "time.sleep(0.3)\n"
        "print('kid', kid.poll() is not None, 'other', other.poll() is None, flush=True)\n"
        "other.kill()\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "kid True other True", (out.stdout, out.stderr)


# ---- contracts between the workflow and the script, by parsing the YAML
def workflow_yaml():
    import yaml

    return yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "merge-queue.yml")))


def test_each_cell_gets_exactly_its_own_candidate_from_the_plan_matrix():
    steps = workflow_yaml()["jobs"]["test"]["steps"]
    env = next(s["env"] for s in steps if "merge_queue.py test" in str(s.get("run", "")))
    expr = env["MQ_CANDIDATES"]
    assert (
        "matrix.pr" in expr and "matrix.head" in expr and "candidates" not in expr and "matrix.base" not in expr and "toJSON" not in expr
    ), expr
    assert env["MQ_BASE"] == "${{ matrix.base }}"


def test_the_artifact_the_test_job_uploads_is_the_one_the_merge_job_reads(tmp_path):
    wf = workflow_yaml()
    up = next(s for s in wf["jobs"]["test"]["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@"))
    assert up["with"]["name"] == "mq-verdict-${{ matrix.pr }}"
    dl = next(s for s in wf["jobs"]["queue"]["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact@"))
    assert dl["with"]["pattern"] == "mq-verdict-*" and dl["with"]["path"] == "verdicts"
    # the folder the merge job reads for PR 7 is the one the upload name yields for matrix.pr = 7
    name = up["with"]["name"].replace("${{ matrix.pr }}", "7")
    d = tmp_path / name
    d.mkdir()
    rec = {"pr": 7, "head": head(7), "base_sha": BASE}
    (d / "verdict.json").write_text(json.dumps({"base": "main", "tests": [rec]}))
    assert mq.read_metadata(str(tmp_path), 7, head(7)) == rec
    # and the merge step's --verdict-dir is the download path
    merge = next(s for s in wf["jobs"]["queue"]["steps"] if s.get("id") == "merge")
    assert "--verdict-dir verdicts" in merge["run"] and "--candidates" in merge["run"] and "--forks" in merge["run"]


def test_read_metadata_takes_only_the_record_of_that_pr_and_head(tmp_path):
    d = tmp_path / "mq-verdict-1"
    d.mkdir()
    other = {"pr": 2, "head": head(2), "base_sha": BASE}
    mine = {"pr": 1, "head": head(1), "base_sha": BASE, "merge_sha": "x"}
    (d / "verdict.json").write_text(json.dumps({"base": "main", "tests": [other, {"pr": 1, "head": head(9), "base_sha": BASE}]}))
    assert mq.read_metadata(str(tmp_path), 1, head(1)) is None  # nothing in this cell's file is about (1, head(1))
    (d / "verdict.json").write_text(json.dumps({"base": "main", "tests": [other, mine]}))
    assert mq.read_metadata(str(tmp_path), 1, head(1)) == mine


def test_the_cell_step_sets_the_fresh_home_and_no_user_site():
    steps = workflow_yaml()["jobs"]["test"]["steps"]
    texts = [str(s.get("run", "")) for s in steps]
    assert any("MQ_CELL_DIR=$(mktemp -d" in t for t in texts) and any("MQ_PYDIR=$(uv python dir)" in t for t in texts)
    run = next(s for s in steps if "merge_queue.py test" in str(s.get("run", "")))
    assert run["env"]["PYTHONNOUSERSITE"] == "1"
    assert max(i for i, t in enumerate(texts) if "MQ_CELL_DIR=" in t) < steps.index(run)  # set before the driver runs


# ---- jobs: status, pages
def test_a_job_that_is_not_completed_decides_nothing_even_with_a_conclusion_set():
    class Api:
        def get(self, path):
            return 200, {"jobs": [{"name": mq.test_job_name(1, head(1)), "status": "in_progress", "conclusion": "success"}]}

    assert mq.read_conclusions(Api(), "77", "1") == {(1, head(1)): None}


def test_a_job_on_the_second_page_of_jobs_is_found():
    pages = {
        1: [{"name": f"other {i}", "status": "completed", "conclusion": "success"} for i in range(100)],
        2: [{"name": mq.test_job_name(5, head(5)), "status": "completed", "conclusion": "success"}],
    }

    class Api:
        def get(self, path):
            return 200, {"jobs": pages[int(path.rsplit("page=", 1)[1])]}

    assert mq.read_conclusions(Api(), "77", "1") == {(5, head(5)): "success"}


# ---- fork heads are dropped at planning
def test_the_plan_never_plans_a_fork_head_and_lists_it_for_the_merge_job():
    import plan_bases

    gh = world(1, 2, 3)
    gh.prs[2]["head_repo"] = "stranger/fork"
    gh.prs[3]["head_repo"] = None
    cells = plan_bases.plan(gh, ["main"])
    assert [c["n"] for c in cells[0]["candidates"]] == [1]
    assert plan_bases.plan_forks(gh, ["main"]) == [2, 3]


def test_the_plan_writes_the_forks_output(tmp_path, monkeypatch):
    import plan_bases

    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    plan_bases.write_outputs([], False, [3, 2])
    text = out.read_text()
    assert "forks=[2, 3]" in text and "has_forks=true" in text
    out.write_text("")
    plan_bases.write_outputs([], False, [])
    assert "forks=[]" in out.read_text() and "has_forks=false" in out.read_text()


def test_the_merge_job_dequeues_the_planned_forks_with_a_comment_and_touches_nothing_else(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1, 2, 3).plan(
        2
    )  # #2 planned only so the sweep has something; #1 and #3 are queued but not in the forks list
    s.job(2, "success").verdict(2)
    s.gh.prs[1]["head_repo"] = "stranger/fork"
    mq.main(
        [
            "merge",
            "--repo",
            "o/r",
            "--verdict-dir",
            str(tmp_path / "v"),
            "--candidates",
            json.dumps(s.cands),
            "--forks",
            json.dumps([1, "3", 3.5, True]),
        ]
    )
    assert "queue" not in s.gh.prs[1]["labels"] and any(n == 1 and "not in this repository" in b for n, b in s.gh.comments)
    assert "queue" in s.gh.prs[3]["labels"]  # "3", 3.5 and True are not ints: bound to the plan's numbers, nothing else is touched
    assert s.gh.merged == [2]


def test_the_queue_job_runs_for_a_sweep_that_only_has_forks():
    wf = workflow_yaml()
    assert "needs.plan.outputs.has_forks == 'true'" in wf["jobs"]["queue"]["if"]
    assert {"forks", "has_forks"} <= set(wf["jobs"]["plan"]["outputs"])


def test_retry_ids_reach_a_comment_only_inside_a_fenced_block(tmp_path, monkeypatch, capsys):
    retry = {"retries": 1, "failed_ids": ["[click](https://example.invalid)"], "flaked_ids": ["[click](https://example.invalid)"]}
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "success").verdict(1, retry=retry)
    s.merge()
    body = next(b for _, b in s.gh.comments if "example.invalid" in b)
    fenced = body.split("```")
    assert len(fenced) == 3 and "example.invalid" in fenced[1] and "example.invalid" not in fenced[0] + fenced[2], body


def test_a_green_that_is_deferred_without_any_merge_is_not_listed_as_an_unused_green(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "success").verdict(1)
    s.gh.merge_status[1] = (502, "bad gateway")
    out = s.merge()
    assert "| main | 1 | 1 | 0 | none |" in out, out


class FakeTester:
    def __init__(self, *a, **k):
        self.tests = [{"result": "green"}]

    def run(self, cands):
        if FakeTester.boom:
            raise RuntimeError("driver crash")

    def verdict(self):
        return {"base": "main", "tests": self.tests}

    boom = False


@pytest.mark.parametrize("cell,boom,reaped", [(True, False, 1), (True, True, 1), (False, False, 0)])
def test_the_driver_reaps_its_process_group_in_a_cell_even_when_it_crashes(tmp_path, monkeypatch, cell, boom, reaped):
    calls = []
    monkeypatch.setattr(mq, "reap_group", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(mq.os, "setpgrp", lambda: None)  # never move the test runner's own group
    monkeypatch.setattr(mq, "Tester", FakeTester)
    monkeypatch.setattr(mq, "GitRepo", lambda *a, **k: object())
    monkeypatch.setattr(mq, "default_run_checks", lambda *a, **k: None)
    FakeTester.boom = boom
    if cell:
        monkeypatch.setenv("MQ_CELL_DIR", str(tmp_path / "cell"))
    else:
        monkeypatch.delenv("MQ_CELL_DIR", raising=False)
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
        str(tmp_path / "out" / "verdict.json"),
    ]
    if boom:
        with pytest.raises(RuntimeError):
            mq.main(argv)
    else:
        mq.main(argv)
    assert calls.count(1) == reaped and (tmp_path / "out" / "verdict.json").exists()  # the verdict is written first, whatever happened


# ---- no success stays on a head that was not merged: failure windows (the delta review's probes q1-q5)
def statuses_on(gh, sha):
    return [st["state"] for s, st in gh.statuses if s == sha]


def test_q1_a_failed_base_read_after_the_success_was_posted_leaves_pending_and_the_job_red(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "success").verdict(1)
    real = s.gh.get_
    reads = []

    def get_(path, num):
        if path.startswith("/git/ref/heads/"):
            reads.append(path)
            if len(reads) == 2:  # the first read is on_green's own; the second is merge()'s, right before the call
                return 502, {"message": "bad gateway"}
        return real(path, num)

    s.gh.get_ = get_
    with pytest.raises(SystemExit):
        s.merge()
    assert s.gh.merged == [] and statuses_on(s.gh, head(1)) == ["success", "pending"]  # the success was overwritten on the way out


def test_q2_a_failed_update_branch_after_the_success_was_posted_leaves_pending(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "success").verdict(1)
    s.gh.merge_status[1] = (405, "Head branch is out of date")
    real = s.gh.get_
    s.gh.get_ = lambda path, num: (
        (200, {"status": "diverged", "ahead_by": 1, "behind_by": 1}) if path.startswith("/compare/") else real(path, num)
    )

    def boom(p, body):
        raise SystemExit("gh_api: PUT update-branch: network down")

    s.gh.update_branch = boom
    with pytest.raises(SystemExit):
        s.merge()
    assert statuses_on(s.gh, head(1)) == ["success", "pending"]


def test_q3_a_failed_overwrite_turns_the_merge_job_red_not_a_warning(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "success").verdict(1)
    s.gh.merge_status[1] = (422, "not mergeable")
    real = s.gh.request

    def request(method, path, body=None, **kw):
        if method == "POST" and path.startswith("/statuses/") and body["state"] != "success":
            s.gh.calls.append((method, path))
            return 500, {}
        return real(method, path, body, **kw)

    s.gh.request = request
    with pytest.raises(
        SystemExit, match=r"could not overwrite merge-queue/tested on the head of PR\(s\) \[1\]: a success may still stand there"
    ):
        s.merge()
    assert (
        "::error title=status-not-overwritten::could not post `failure`" in capsys.readouterr().out
    )  # annotated, then the job exits non-zero
    assert statuses_on(s.gh, head(1)) == ["success"]  # nothing could be written: that is exactly why the job is red


@pytest.mark.parametrize("conclusion,status", [("cancelled", "completed"), (None, "in_progress")])
def test_q4_a_success_left_by_an_earlier_sweep_is_cleared_when_this_sweep_cannot_decide(tmp_path, monkeypatch, capsys, conclusion, status):
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, conclusion, status=status).verdict(1)
    s.gh.statuses.append((head(1), {"state": "success", "context": mq.STATUS_CONTEXT}))
    s.merge()
    assert statuses_on(s.gh, head(1)) == ["success", "pending"] and "queue" in s.gh.prs[1]["labels"]


def test_q4b_a_planned_candidate_with_no_job_is_also_cleared(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1, 2).plan(1, 2).job(2, "success").verdict(2)  # #1 has no job in this attempt
    s.gh.statuses.append((head(1), {"state": "success", "context": mq.STATUS_CONTEXT}))
    s.merge()
    assert statuses_on(s.gh, head(1)) == ["success", "pending"]


def test_q5_a_conflict_dequeue_clears_an_earlier_success(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "failure").verdict(1, result="conflict")
    s.gh.statuses.append((head(1), {"state": "success", "context": mq.STATUS_CONTEXT}))
    out = s.merge()
    assert "dequeued-conflict" in out and statuses_on(s.gh, head(1)) == ["success", "failure"]


def test_a_dequeued_fork_head_and_a_head_that_moved_also_end_without_a_success(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "success").verdict(1)
    s.gh.statuses.append((head(1), {"state": "success", "context": mq.STATUS_CONTEXT}))
    s.gh.prs[1]["head_repo"] = "stranger/fork"
    s.merge()
    assert statuses_on(s.gh, head(1))[-1] == "failure"


def test_the_must_allow_control_a_merged_head_keeps_its_success(tmp_path, monkeypatch, capsys):
    s = sweep(tmp_path, monkeypatch, capsys, 1).plan(1).job(1, "success").verdict(1)
    s.merge()
    assert s.gh.merged == [1] and statuses_on(s.gh, head(1)) == ["success"]


# ---- the plan side of "one cell per candidate"
def test_the_plan_emits_one_matrix_cell_per_planned_candidate_on_every_base():
    import plan_bases

    cells = [
        {"base": "main", "candidates": [{"n": n, "head": head(n)} for n in (1, 2, 3)]},
        {"base": "other", "candidates": [{"n": 4, "head": head(4)}]},
    ]
    assert plan_bases.matrix_of(cells) == {
        "include": [
            {"base": "main", "pr": 1, "head": head(1)},
            {"base": "main", "pr": 2, "head": head(2)},
            {"base": "main", "pr": 3, "head": head(3)},
            {"base": "other", "pr": 4, "head": head(4)},
        ]
    }


def test_the_plan_of_three_queued_prs_makes_three_cells():
    import plan_bases

    gh = world(1, 2, 3)
    cells = plan_bases.plan(gh, ["main"])
    assert [(c["pr"], c["head"]) for c in plan_bases.matrix_of(cells)["include"]] == [(1, head(1)), (2, head(2)), (3, head(3))]


def test_the_cell_name_the_workflow_builds_parses_with_the_scripts_pattern():
    import re

    expr = workflow_yaml()["jobs"]["test"]["name"]
    fmt = re.search(r"format\('([^']+)'", expr).group(1)
    assert mq.TEST_JOB_RE.fullmatch(fmt.format(7, head(7))) and fmt.format(7, head(7)) == mq.test_job_name(7, head(7))


# ---- the reap: the group guard, the SIGKILL leg, and the sessions ci_step started
def run_harness(tmp_path, body, new_session=True):
    """Run `body` in its own python, in its own session (so a mistaken killpg can only hit that session, never the test runner)."""
    script = (
        f"import os, signal, subprocess, sys, time\nsys.path.insert(0, {os.path.join(ROOT, '.github', 'workflows', 'scripts')!r})\nimport merge_queue as mq\nimport ci_step\n"
        + body
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=90,
        start_new_session=new_session,
        env={**os.environ, "CI_CAUSE_DIR": str(tmp_path)},
    )


def test_cmd_test_in_a_cell_moves_itself_into_its_own_group_so_the_reap_spares_its_siblings(tmp_path):
    """The guard before the reap: a driver that is NOT its group's leader (the normal case under a step's shell) must leave that group first, or the reap signals the step's
    shell and the runner's other processes. A sibling in the driver's original group must survive, and the driver must exit 0."""
    out = run_harness(
        tmp_path,
        """
sibling = subprocess.Popen(['sleep', '60'])  # in this (leader) process's group
child_code = (
    "import sys, os, tempfile\\n"
    f"sys.path.insert(0, {sys.path[0]!r})\\n"
    "import merge_queue as mq\\n"
    "class T:\\n"
    "    def __init__(self, *a, **k): self.tests = [{'result': 'green'}]\\n"
    "    def run(self, c): pass\\n"
    "    def verdict(self): return {'base': 'main', 'tests': self.tests}\\n"
    "mq.Tester = T; mq.GitRepo = lambda *a, **k: object(); mq.default_run_checks = lambda *a, **k: None\\n"
    "assert os.getpid() != os.getpgrp()\\n"
    "mq.main(['test', '--repo', 'o/r', '--base', 'main', '--candidates', '[]', '--driver-dir', '.', '--verdict-file', os.environ['VF']])\\n"
    "assert os.getpid() == os.getpgrp()\\n"  # it became its own leader
)
env = dict(os.environ, MQ_CELL_DIR=os.path.join(os.environ['CI_CAUSE_DIR'], 'cell'), VF=os.path.join(os.environ['CI_CAUSE_DIR'], 'v.json'))
rc = subprocess.run([sys.executable, '-c', child_code], env=env).returncode
time.sleep(0.5)
print('rc', rc, 'sibling alive', sibling.poll() is None, flush=True)
sibling.kill()
""",
    )
    assert out.returncode == 0 and out.stdout.strip() == "rc 0 sibling alive True", (out.stdout, out.stderr)


def test_the_reap_kills_a_process_that_ignores_sigterm(tmp_path):
    out = run_harness(
        tmp_path,
        """
if os.getpgrp() != os.getpid():
    os.setpgrp()  # the harness runs in its own session, so it is already its group's leader; Linux refuses setpgrp for a session leader (EPERM)
kid = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
time.sleep(0.5)  # let the child install its handler
mq.reap_group(grace_s=0.3, extra_pgids=[])
time.sleep(0.3)
print('kid dead', kid.poll() is not None, flush=True)
""",
    )
    assert out.returncode == 0 and out.stdout.strip() == "kid dead True", (out.stdout, out.stderr)


def test_the_reap_also_kills_what_a_check_left_in_its_own_session(tmp_path):
    out = run_harness(
        tmp_path,
        """
if os.getpgrp() != os.getpid():
    os.setpgrp()  # the harness runs in its own session, so it is already its group's leader; Linux refuses setpgrp for a session leader (EPERM)
pidfile = os.path.join(os.environ['CI_CAUSE_DIR'], 'leftover.pid')
rc = ci_step.run_argv('probe', ['sh', '-c', 'sleep 60 & echo $! > ' + pidfile], os.getcwd(), 30)  # the check exits, its background process stays (in the check's session)
pid = int(open(pidfile).read())
time.sleep(0.3)
alive_before = subprocess.run(['kill', '-0', str(pid)], capture_output=True).returncode == 0
mq.reap_group(grace_s=0.3)
time.sleep(0.3)
alive_after = subprocess.run(['kill', '-0', str(pid)], capture_output=True).returncode == 0
print('rc', rc, 'before', alive_before, 'after', alive_after, 'recorded', len(ci_step.STARTED_PGIDS), flush=True)
""",
    )
    assert out.returncode == 0 and out.stdout.strip().splitlines()[-1] == "rc 0 before True after False recorded 1", (
        out.stdout,
        out.stderr,
    )


# ---- every process that runs PR code gets child_env
def test_every_subprocess_in_the_pr_code_path_gets_its_environment_from_child_env():
    """Scope: the call sites of `subprocess.*` inside `default_run_checks` and `remove_worktree` (merge_queue.py), the `env=` of each `ci_step.run_check` call there, and
    that `ci_step.run_argv` and `collect_ids` pass their `env` parameter on to Popen. An `env=` must be a call to child_env/git_env or a name assigned from one in the
    same function. Not covered: subprocess calls elsewhere (the driver's own git and API work is not PR code)."""
    import ast

    tree = ast.parse(open(os.path.join(ROOT, ".github", "workflows", "scripts", "merge_queue.py")).read())
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def is_child_call(node):
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("child_env", "git_env")

    checked = 0
    for fname in ("default_run_checks", "remove_worktree"):
        fn = fns[fname]
        derived = {
            t.id for a in ast.walk(fn) if isinstance(a, ast.Assign) and is_child_call(a.value) for t in a.targets if isinstance(t, ast.Name)
        }
        for call in (c for c in ast.walk(fn) if isinstance(c, ast.Call)):
            f = call.func
            is_sub = (
                isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name)
                and f.value.id == "subprocess"
                and f.attr in ("run", "Popen", "check_output", "call", "check_call")
            ) or (isinstance(f, ast.Name) and f.id == "run")  # remove_worktree's injectable `run = run or subprocess.run`
            is_check = isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "ci_step" and f.attr == "run_check"
            if not (is_sub or is_check):
                continue
            env = next((k.value for k in call.keywords if k.arg == "env"), None)
            ok = env is not None and (is_child_call(env) or (isinstance(env, ast.Name) and env.id in derived))
            assert ok, f"{fname}: {ast.unparse(call)[:100]} has no env from child_env/git_env"
            checked += 1
    assert checked == 5, checked  # worktree add, uv sync, two run_check calls (checks, and the loop's other), and remove_worktree's own run
    cs = ast.parse(open(os.path.join(ROOT, ".github", "workflows", "scripts", "ci_step.py")).read())
    cfns = {n.name: n for n in ast.walk(cs) if isinstance(n, ast.FunctionDef)}
    for fname in ("run_argv", "collect_ids"):
        pops = [c for c in ast.walk(cfns[fname]) if isinstance(c, ast.Call) and ast.unparse(c.func) == "subprocess.Popen"]
        assert pops and all(any(k.arg == "env" and ast.unparse(k.value) == "env" for k in p.keywords) for p in pops), fname


def test_the_pr_code_path_check_goes_red_on_a_subprocess_without_the_cell_env(tmp_path):
    import ast

    bad = "import subprocess\ndef default_run_checks(d, b):\n    subprocess.run(['uv', 'sync'])\n"
    fn = ast.parse(bad).body[1]
    calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "run"]
    assert calls and not any(k.arg == "env" for k in calls[0].keywords)  # the shape the walk above rejects
