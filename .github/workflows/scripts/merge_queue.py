#!/usr/bin/env python3
"""merge-queue driver, in three phases that run in different jobs (see merge-queue.yml's header for the design and the data contract).

    merge_queue.py test  --repo R --base B --candidates JSON --driver-dir D --verdict-file F
        job merge-queue-test (contents: read). Takes the planned candidates (oldest queued first). For each it fetches the CURRENT base
        tip and the PR head, builds the merge commit LOCALLY (`git merge --no-ff` of the head onto the base tip, in a worktree; GitHub's
        refs/pull/N/merge is never used: GitHub does not rebuild it promptly when the base moves), runs the required checks on that tree
        with NO token in their environment, and writes the verdict file (base SHA and head SHA tested). A merge conflict is a verdict
        `conflict`. Stops at the first green PR (a merge changes the base, so later results would be stale).
    merge_queue.py merge --repo R --verdict-dir D [--dry-run] [--result-file F]
        job merge-queue (write permissions, runs NO PR code). Dequeues the red and conflicting PRs; for the green one: re-checks head and
        base, posts the commit status `merge-queue/tested` on the tested head, re-reads the base SHA right before the merge call, merges
        with sha=head (so GitHub's merge has the parents that were tested), then removes the `queue` label from the merged PR.
Planning (which PRs are queued, in which order) is plan_bases.py, which uses queued_prs() below.

Verdict `result` values: green, red, conflict (the PR conflicts with the base), stale (the head moved since planning).
Every loop here has a bound: candidates (MAX_CANDIDATES), a test-time budget, result pages
(gh_api.MAX_PAGES). A PR is tested at most once per sweep, and a red PR is dequeued once, never tested again in the same sweep.
"""

import argparse
import base64
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ci_step  # noqa: E402
import uv_seed  # noqa: E402
from bases import QUEUE_BASES  # noqa: E402
from gh_api import Api  # noqa: E402

REQUIRED = ("checks", "gitleaks", "smoke")  # stable job names in ci.yml; the same names ci_step.CHECKS runs
QUEUE_LABEL = "queue"
STATUS_CONTEXT = "merge-queue/tested"  # the commit status the ruleset will require (after the bootstrap merges)
MERGE_METHOD = "merge"  # matches the repo's history ("Merge pull request #N"); all three methods are allowed by the ruleset
MAX_RETRY_IDS_KEPT = 50  # test ids kept from a verdict file
MAX_CANDIDATES = 3  # PRs planned, tested and handled per base per sweep
SHA_RE = re.compile(r"[0-9a-f]{40}")  # a sha as the merge job accepts it from anything a PR's code could have written
KNOWN_CAUSES = ("timeout", "unclassified")  # the only cause tags that reach a comment or an annotation (ci_step.py writes exactly these)
TEST_JOB_RE = re.compile(
    r"merge-queue-test \(pr-([0-9]{1,9}) ([0-9a-f]{40})\)"
)  # the name of a test cell: the PR and its planned head, set by the plan job


def test_job_name(n, head):
    """The displayed name of the test job for one planned candidate (merge-queue.yml builds the same string with format())."""
    return f"merge-queue-test (pr-{n} {head})"


WORKFLOW_FILE = "merge-queue.yml"  # the file the merge job re-dispatches
MAX_HOPS = 3  # a chain of self re-dispatches ends here: each dispatch carries hop+1, the run at MAX_HOPS never dispatches
MERGED_OUTCOMES = ("merged", "merged-after-5xx", "already-in-base")  # the PR is in the base: counts for the outputs and for the re-dispatch
NOT_MERGED_OUTCOMES = (  # every other outcome a sweep can report, declared HERE so a new outcome must be classed (SDLC 7.6 CI-QUEUE-OUTCOMES-CLASSED)
    "deferred",
    "dequeued-red",
    "red-not-dequeued",
    "dequeued-conflict",
    "conflict-not-dequeued",
    "dequeued-workflow-update-needed",
    "dequeued-refused",
    "dequeued-fork-head",
    "refused-not-dequeued",
    "dry-run-would-merge",
)
HISTORY_SCAN = 30  # base commits read to name the merge commit of a PR that landed without a 200
GIT_TIMEOUT_S = 300  # every git subprocess; a timeout is a failure with cause `timeout`
FETCH_TIMEOUT_S = 600
UV_SYNC_TIMEOUT_S = 1200  # `uv sync --locked`: a timeout is a failure with cause `timeout`
# THE TIME-BUDGET INVARIANT. A candidate may START only while the budget is not used up, so the worst case is
# (budget) + (one candidate with every bound at its limit) and it must fit the test job's timeout-minutes with a margin:
#     TEST_BUDGET_S + WORST_CANDIDATE_S + MARGIN_S == JOB_TIMEOUT_S      (test_the_time_budget_fits_the_job_timeout)
# WORST_CANDIDATE_S is computed from the real bounds: setup (two fetches, nine other git calls, the uv sync; the calls are counted by a test) plus the timeout of every step of
# every required check in ci_step.CHECKS. Raise a check's timeout and the budget shrinks by itself; the test fails if the budget would go
# negative or if merge-queue.yml's test-job `timeout-minutes` stops being JOB_TIMEOUT_S / 60.
JOB_TIMEOUT_S = 240 * 60  # merge-queue.yml, job `test`: timeout-minutes: 240
MARGIN_S = 10 * 60
FETCH_CALLS = 2  # per candidate: the base and the PR head
GIT_CALLS = 9  # per candidate, every other git call (rev-parse x4, worktree add x2, merge, worktree remove x2): COUNTED BY A TEST that runs the real
#               Tester against a recording fake (test_the_setup_budget_counts_every_call_a_candidate_makes): a new call breaks it
SETUP_BUDGET_S = FETCH_CALLS * FETCH_TIMEOUT_S + GIT_CALLS * GIT_TIMEOUT_S + UV_SYNC_TIMEOUT_S
# the one retry of the failed tests (ci_step.RETRY_TIMEOUT_S) is part of the worst candidate: it runs inside the same job
WORST_CANDIDATE_S = (
    SETUP_BUDGET_S + sum(t for steps in ci_step.CHECKS.values() for _, t in steps) + ci_step.RETRY_TIMEOUT_S + ci_step.COLLECT_TIMEOUT_S
)
TEST_BUDGET_S = JOB_TIMEOUT_S - WORST_CANDIDATE_S - MARGIN_S  # no new candidate is started after this much test time
GIT_VERBS = ("fetch", "worktree", "merge", "rev-parse", "diff")  # named in a timeout failure
DEQUEUE_OK = (200, 204, 404)  # label removed, or already gone
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTHORIZATION|AUTH_SOCK|API_KEY|PRIVATE", re.I)


def child_env(environ=None, **extra):
    """The environment for any process that runs PR code: the caller's, without GITHUB_TOKEN/GH_TOKEN or any secret-looking name."""
    src = os.environ if environ is None else environ
    env = {
        k: v for k, v in src.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN", "MQ_UV_SEED") and not SECRET_NAME.search(k)
    }  # MQ_UV_SEED: the warm cache's path is the driver's, not PR code's
    env.update(cell_env(src.get("MQ_CELL_DIR"), src.get("MQ_PYDIR")))
    env.update(extra)
    return env


def cell_env(cell_dir, pydir=None):
    """The per-cell home of PR code (the workflow sets MQ_CELL_DIR to a fresh folder under RUNNER_TEMP): HOME, XDG_CACHE_HOME, XDG_CONFIG_HOME, XDG_DATA_HOME,
    UV_CACHE_DIR and TMPDIR inside it, and no user site (PYTHONNOUSERSITE), so nothing a PR's tests wrote to the runner user's home or caches by accident is read by the
    next cell. This removes ACCIDENTAL carry-over. It is not isolation from a hostile PR: that needs an ephemeral runner (see merge-queue.yml). {} when MQ_CELL_DIR is unset
    (hosted runs, tests).
    Why each variable: HOME is where uv, git, pip and setuptools look for config and caches, and the default of every XDG directory; the three XDG_* directories are set too
    only so that a runner service that exports them cannot send PR code to a persistent location (they are build-tool caches, config and data; the engine's own tests pin
    their HOME and XDG_CONFIG_HOME per test); UV_CACHE_DIR is uv's cache (a fresh one per cell: on a self-hosted runner a clone of the warm seed, uv_seed.py; elsewhere empty); TMPDIR keeps a PR's temp files in
    the cell; PYTHONNOUSERSITE=1 closes the user-site hook (a usercustomize.py); UV_PYTHON_INSTALL_DIR names the trusted Python the setup step installed.
    XDG_STATE_HOME is deliberately NOT set: the engine and its tests resolve their state directory from it ($FACTLATTICE_STATE_DIR, else $XDG_STATE_HOME/factlattice, else
    ~/.local/state/factlattice), and a test that moves HOME expects the third rule; with HOME already fresh, the default state directory is a fresh one without it."""
    if not cell_dir:
        return {}
    dirs = {
        "HOME": "home",
        "XDG_CACHE_HOME": "xdg-cache",
        "XDG_CONFIG_HOME": "xdg-config",
        "XDG_DATA_HOME": "xdg-data",
        "UV_CACHE_DIR": "uv-cache",
        "TMPDIR": "tmp",
    }
    env = {k: os.path.join(cell_dir, v) for k, v in dirs.items()}
    for d in env.values():
        os.makedirs(d, exist_ok=True)
    env["PYTHONNOUSERSITE"] = "1"
    if pydir:  # uv's own Python, installed by the trusted setup step before the HOME above existed
        env["UV_PYTHON_INSTALL_DIR"] = pydir
    return env


def git_env(**extra):
    """child_env() with git's own configuration isolated: the runner's global and system config (merge.renames, rerere, ...) must not
    change the merge that is tested."""
    return child_env(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", **extra)


def is_workflow_refusal(st, msg):
    """True for the 403 GitHub answers when an App token would create or update a workflow file without `workflows` permission: the message
    must name that permission (`workflows` permission), not merely mention a workflow."""
    return st == 403 and isinstance(msg, str) and re.search(r"\bworkflows\b`?\s+permission", msg.lower()) is not None


def failure_cause(failure):
    """The cause tag of a recorded failure: one of KNOWN_CAUSES. A missing tag, and any other text (a verdict file is written by the job that ran the
    PR's code), is `unclassified`, never a guess and never printed."""
    cause = failure.get("cause")
    return cause if cause in KNOWN_CAUSES else "unclassified"


SETUP_CHECKS = (  # the driver's own setup failures (default_run_checks): fixed strings, shown to the author because a lock mismatch is the commonest red
    "setup(git worktree add)",
    "setup(uv sync --locked --extra test --extra semantic)",
)


def known_check(name):
    """A failed check's name as the merge job lets it be printed: a required check, `test-step`, or `git <verb>` of a timeout failure; anything else is `unknown-check`."""
    if name in (*REQUIRED, "test-step") or name in SETUP_CHECKS:
        return name
    m = re.fullmatch(r"git ([a-z-]+)", name) if isinstance(name, str) else None
    return name if m and m.group(1) in (*GIT_VERBS, "command") else "unknown-check"


# ---- reading the queue (plan job, read-only) -----------------------------------------------------------------------------------
class Failures(list):
    """[(check, cause)] of the red checks of one candidate, plus `.retry`: {} or what ci_step.run_check recorded of the one retry."""

    def __init__(self, failures=(), retry=None):
        super().__init__(failures)
        self.retry = dict(retry or {})


def flake_line(test_id, pr, run_url):
    """The fixed, searchable record of one flake (maintainers' ruling of 2026-11-20): a test id that failed on attempt 1 and passed on the retry.
    The test id may contain spaces; ` pr=` after it is what ends it."""
    return f"queue-flake: {test_id} pr={pr} run={run_url}"


MAX_LISTED_IDS = 20  # failing ids named in a comment (the verdict file keeps them all)


def retry_lines(retry, pr, run_url):
    """The lines a queue comment carries when a retry happened: the count, what stayed red, and the fixed queue-flake lines in a fenced block
    (each line starts with `queue-flake:`). [] when there was no retry."""
    if not retry:
        return []
    failed, flaked = retry.get("failed_ids", []), retry.get("flaked_ids", [])
    still = [i for i in failed if i not in flaked]
    out = [
        f"- retries: {retry.get('retries', 1)} (the failed tests of the `checks` pytest step were re-run once; {len(flaked)} of {len(failed)} passed on retry)"
    ]
    if still:
        out.append(
            f"- still failing after the retry ({len(still)}): "
            + ", ".join(f"`{i}`" for i in still[:MAX_LISTED_IDS])
            + (" ..." if len(still) > MAX_LISTED_IDS else "")
        )
    if flaked:
        out += ["```"] + [flake_line(i, pr, run_url) for i in flaked] + ["```"]
    return out


def queued_at(api, pr):
    """When the `queue` label was last applied (all pages of the issue events), else the PR's creation time."""
    events = api.pages(f"/issues/{pr['number']}/events")
    times = [e["created_at"] for e in events if e.get("event") == "labeled" and e.get("label", {}).get("name") == QUEUE_LABEL]
    return max(times) if times else pr["created_at"]


def is_fork_head(api, p):
    """True when the PR's head is not in the repository the queue runs in (a fork, or a deleted fork: head.repo is null). Read from the PR list, i.e. GitHub's record."""
    return ((p.get("head") or {}).get("repo") or {}).get("full_name") != api.repo


def queued_forks(api, base):
    """Numbers of open, non-draft PRs into `base` that carry `queue` and whose head is not in this repository. The queue never plans or tests them (their code
    would run on the queue's runner); the merge job dequeues them with a comment, bound to exactly these numbers."""
    return sorted(
        p["number"]
        for p in api.pages(f"/pulls?state=open&base={base}")
        if not p.get("draft") and QUEUE_LABEL in [lb["name"] for lb in p.get("labels", [])] and is_fork_head(api, p)
    )


def queued_prs(api, base):
    """Open, non-draft, in-repo PRs into `base` that carry `queue`, oldest queued first (ties by number)."""
    out = []
    for p in api.pages(f"/pulls?state=open&base={base}"):
        if p.get("draft") or QUEUE_LABEL not in [lb["name"] for lb in p.get("labels", [])] or is_fork_head(api, p):
            continue
        out.append((queued_at(api, p), p["number"], p))
    out.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in out]


DRAFT_MARKER = "<!-- merge-queue:skipped-draft -->"  # hidden in the comment body: its presence on the PR is the whole idempotence state (no file, no label)
DRAFT_COMMENT = "The merge queue skips this PR while it is a draft. Mark it ready for review (it keeps its `queue` label) and the next sweep will pick it up."


def queued_drafts(api, base):
    """Open DRAFT PRs into `base` that carry `queue`, by number. The queue never plans them (queued_prs); this list is only for the flag."""
    out = []
    for p in api.pages(f"/pulls?state=open&base={base}"):
        if p.get("draft") and QUEUE_LABEL in [lb["name"] for lb in p.get("labels", [])]:
            out.append(p["number"])
    return sorted(out)


def flag_queued_drafts(api, bases, log=print, dry_run=False):
    """SDLC 7.6 CI-QUEUE-FLAGS-QUEUED-DRAFT: a draft that carries `queue` is flagged, never silently skipped. ONE comment per PR, found again by
    DRAFT_MARKER among the PR's own comments (stateless: nothing is stored, so a second sweep, or a second queue run, posts nothing). Read-only
    otherwise: the draft is not tested, merged, relabelled or un-drafted. Never fatal: a failed read or post is a log line and the sweep goes on.
    If the comments cannot be read it posts nothing (it cannot tell whether the flag is already there, and a duplicate is worse than a late one).
    -> the PR numbers a comment was posted on."""
    posted = []
    try:
        drafts = [n for b in bases for n in queued_drafts(api, b)]
    except (Exception, SystemExit) as e:  # gh_api raises SystemExit on a network error
        log(f"::warning title=queued-draft::could not list the open PRs ({e}); no draft was flagged")
        return posted
    for n in drafts:
        try:
            comments = api.pages(f"/issues/{n}/comments")
            if not isinstance(comments, list):
                raise ValueError(f"comments answered {type(comments).__name__}")
            if any(DRAFT_MARKER in (c.get("body") or "") for c in comments if isinstance(c, dict)):
                continue
            if dry_run:
                log(f"  dry-run: WOULD flag queued draft #{n}")
                continue
            st, _ = api.request("POST", f"/issues/{n}/comments", {"body": f"{DRAFT_MARKER}\n**merge-queue**: {DRAFT_COMMENT}"})
        except (Exception, SystemExit) as e:
            log(f"::warning title=queued-draft::could not flag draft #{n} ({e}); the sweep goes on")
            continue
        if st in (200, 201):
            posted.append(n)
            log(f"flagged queued draft #{n}")
        else:
            log(f"::warning title=queued-draft::the comment on draft #{n} answered HTTP {st}; the sweep goes on")
    return posted


# ---- git access of the test job (contents: read; no REST call) -----------------------------------------------------------------
class GitRepo:
    """The driver checkout, used for fetching only. The token goes to the git command line (never to PR code, never into .git/config)."""

    def __init__(self, directory, token=None, remote="origin", run=subprocess.run):
        self.dir, self.token, self.remote, self.run = directory, token, remote, run
        self.merge_trees = {}  # PR number -> its merge worktree

    def auth_args(self):
        if not self.token:
            return []
        cred = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        return ["-c", f"http.extraheader=AUTHORIZATION: basic {cred}"]

    def git(self, *args, check=True, timeout=GIT_TIMEOUT_S):
        cmd = ["git", "-C", self.dir, *self.auth_args(), *args]
        return self.run(cmd, check=check, capture_output=True, text=True, env=git_env(), timeout=timeout)

    def git_in(self, tree, *args, check=True):
        """A git command in a worktree: no token, no configured identity beyond the fixed one, isolated config, bounded time."""
        cmd = ["git", "-C", tree, *args]
        return self.run(cmd, check=check, capture_output=True, text=True, env=git_env(), timeout=GIT_TIMEOUT_S)

    def rev(self, ref):
        return self.git("rev-parse", ref).stdout.strip()

    def tree_of(self, sha):
        """The tree sha of a commit (recorded in the verdict; the merge job compares it with GitHub's merge commit)."""
        return self.rev(f"{sha}^{{tree}}")

    def fetch_base(self, base):
        """Force-refresh origin/<base> and return its sha (the checks' BASE_REF is origin/<base>, so it must not be stale)."""
        tracking = f"refs/remotes/{self.remote}/{base}"
        self.git("fetch", "--no-tags", self.remote, f"+refs/heads/{base}:{tracking}", timeout=FETCH_TIMEOUT_S)
        return self.rev(tracking)

    def fetch_head(self, n):
        """Fetch the PR head (refs/pull/N/head) and return its sha. refs/pull/N/merge is never read: GitHub does not rebuild it
        promptly when the base moves (measured live, 2026-11-19), so it cannot be trusted to be on the current base."""
        self.git("fetch", "--no-tags", self.remote, f"+refs/pull/{n}/head:refs/mq/{n}/head", timeout=FETCH_TIMEOUT_S)
        return self.rev(f"refs/mq/{n}/head")

    def merge_local(self, n, base_sha, head_sha):
        """Build the merge of head onto base LOCALLY: a detached worktree at base_sha, then `git merge --no-ff --no-edit head_sha`.
        -> the merge commit's sha, or None when the merge conflicts. Any other failure of git raises (loud, never read as a conflict).
        The worktree is remembered per PR; drop_merge_tree(n) removes it."""
        tree = tree_path(self.dir, n, base_sha)
        self.merge_trees[n] = tree
        self.git("worktree", "add", "--detach", tree, base_sha)
        ident = ["-c", "user.name=merge-queue", "-c", "user.email=merge-queue@users.noreply.github.com", "-c", "commit.gpgsign=false"]
        merged = self.git_in(tree, *ident, "merge", "--no-ff", "--no-edit", head_sha, check=False)
        if merged.returncode == 0:
            return self.git_in(tree, "rev-parse", "HEAD").stdout.strip()
        unmerged = self.git_in(tree, "diff", "--name-only", "--diff-filter=U").stdout.split()
        if merged.returncode == 1 and unmerged:
            return None
        raise subprocess.CalledProcessError(merged.returncode, "git merge", merged.stdout, merged.stderr)

    def drop_merge_tree(self, n):
        """Remove the merge worktree of PR n (a no-op when there is none). Called from a `finally`, so it never raises."""
        tree = self.merge_trees.pop(n, None)
        if tree:
            remove_worktree(self.dir, tree, self.run)


def remove_worktree(driver_dir, tree, run=None):
    """`git worktree remove --force` of a worktree this run made; never raises (a leftover must not hide the real result)."""
    run = run or subprocess.run
    try:
        run(
            ["git", "-C", driver_dir, "worktree", "remove", "--force", tree],
            check=False,
            capture_output=True,
            env=git_env(),
            timeout=GIT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


def tree_path(driver_dir, n, sha, exists=os.path.exists):
    """A worktree path that does not exist yet (a retry on the same merge sha must not hit `worktree add` on an existing path)."""
    first = os.path.abspath(os.path.join(driver_dir, "..", f"tree-{n}-{sha[:12]}"))
    for k in range(100):
        p = first if k == 0 else f"{first}-{k}"
        if not exists(p):
            return p
    raise SystemExit(f"merge-queue: no free worktree path for #{n} {sha[:12]} (100 tried)")


def default_run_checks(driver_dir, base):
    """-> run(n, merge_sha): a detached worktree of the PR's merge commit, every required check run in it with NO token in the
    environment. Returns the [(check, cause)] of the red ones as a Failures
    (a list that also carries `.retry`, the info of the one retry when it ran; a setup failure returns a plain list)."""

    def run(n, merge_sha):
        tree = tree_path(driver_dir, n, merge_sha)
        try:
            try:
                subprocess.run(
                    ["git", "-C", driver_dir, "worktree", "add", "--detach", tree, merge_sha],
                    check=True,
                    env=git_env(),
                    timeout=GIT_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired:
                return [("setup(git worktree add)", "timeout")]
            env = git_env(
                BASE_REF=f"origin/{base}", UV_PROJECT_ENVIRONMENT=tree + "-venv"
            )  # the venv beside the worktree, never inside it (ci_step.venv_python)
            try:  # --locked: red when pyproject.toml and uv.lock disagree
                if subprocess.run(
                    ["uv", "sync", "--locked", "--extra", "test", "--extra", "semantic"], cwd=tree, env=env, timeout=UV_SYNC_TIMEOUT_S
                ).returncode:
                    return [("setup(uv sync --locked --extra test --extra semantic)", "unclassified")]
            except subprocess.TimeoutExpired:
                return [("setup(uv sync --locked --extra test --extra semantic)", "timeout")]
            # the lint gate (tools/ci_lint.py, pyproject.toml's ruff settings, tools/ci_complexity_baseline.json) is the default branch's: the driver checkout
            failures, retry = [], {}
            for name in REQUIRED:  # every check runs, whatever an earlier one did: a red lint, scan or smoke is reported with the rest
                if (
                    name == "checks"
                ):  # ONLY the pytest step inside `checks` may retry (once, its failed ids only); gitleaks and smoke never do
                    red = ci_step.run_check(name, tree, env=env, retry_flaky=True, retry_info=retry, gate_dir=driver_dir)
                else:
                    red = ci_step.run_check(name, tree, env=env, gate_dir=driver_dir)
                if red:
                    failures.append((name, ci_step.read_cause(name)))
            return Failures(failures, retry)
        finally:
            remove_worktree(driver_dir, tree)

    return run


# ---- phase: test ---------------------------------------------------------------------------------------------------------------
class Tester:
    def __init__(self, git, base, run_checks, log=print, clock=time.monotonic, budget_s=TEST_BUDGET_S, run_url=""):
        self.git, self.base, self.run_checks, self.log, self.clock, self.budget_s = git, base, run_checks, log, clock, budget_s
        self.run_url = run_url
        self.tests = []

    def verdict(self):
        return {"base": self.base, "tests": self.tests}

    def settle(self, n, planned_head):
        """Fetch the current base tip and the PR head and build their merge locally.
        -> (state, head, merge sha, base sha); state is `ok`, `stale` (the head moved since planning) or `conflict`."""
        base = self.git.fetch_base(self.base)
        head = self.git.fetch_head(n)
        if head != planned_head:
            return "stale", head, None, base
        merge = self.git.merge_local(n, base, head)
        return ("ok" if merge else "conflict"), head, merge, base

    def test_one(self, cand):
        n = cand["n"]
        t = {"pr": n, "head": cand["head"], "base_sha": None, "merge_sha": None, "tree_sha": None, "result": "red", "failures": []}
        try:
            state, head, merge, base = self.settle(n, cand["head"])
            t.update(
                base_sha=base, merge_sha=merge, result=state if state != "ok" else "green"
            )  # `head` stays the PLANNED head (a `stale` verdict is checked against the API's head)
            if state == "ok":
                t["tree_sha"] = self.git.tree_of(merge)
                res = self.run_checks(n, merge)
                failures, retry = res, getattr(res, "retry", {})
                if retry:
                    t["retry"] = retry
                    self.record_flakes(n, retry)
                t["failures"] = [{"check": a, "cause": b} for a, b in failures]
                t["result"] = "red" if failures else "green"
        except subprocess.TimeoutExpired as e:  # a hung git is a failure with a mechanical cause, never a pass
            t.update(result="red", failures=[{"check": "git " + next((w for w in e.cmd if w in GIT_VERBS), "command"), "cause": "timeout"}])
        finally:
            self.git.drop_merge_tree(n)
        self.log(f"  #{n}: {t['result']}")
        return t

    def record_flakes(self, n, retry):
        """A pass on retry is logged: one `queue-flake:` line per test id that failed on attempt 1 and passed on the retry, in the log and the job
        summary (the PR's queue comment gets the same lines from the Merger)."""
        self.log(
            f"  #{n}: retried once: {len(retry.get('failed_ids', []))} failed test id(s), {len(retry.get('flaked_ids', []))} passed on retry"
        )
        lines = [flake_line(i, n, self.run_url) for i in retry.get("flaked_ids", [])]
        for ln in lines:
            self.log(ln)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
                fh.write(f"### merge-queue: #{n} retried\n" + "\n".join(retry_lines(retry, n, self.run_url)) + "\n")

    def run(self, candidates):
        start = self.clock()
        for cand in candidates[:MAX_CANDIDATES]:
            if self.tests and self.clock() - start > self.budget_s:
                self.log(f"  test budget of {self.budget_s}s used; the rest wait for the next sweep")
                break
            self.tests.append(self.test_one(cand))
            if self.tests[-1]["result"] == "green":
                break  # a merge moves the base: any later result would be stale
        return self.verdict()


# ---- phase: merge --------------------------------------------------------------------------------------------------------------
class Merger:
    def __init__(self, api, run_url="", dry_run=False, log=print):
        self.api, self.run_url, self.dry, self.log = api, run_url, dry_run, log
        self.tree_mismatch = []  # PRs whose merged tree differs from the tested one (job goes red)
        self.tested = None  # the verdict on_green is handling
        self.red = set()  # PRs that went red in this sweep: never merged in it, whatever else the verdicts say
        self.marked = {}  # head sha -> the last non-success status this sweep put on it
        self.status_failed = []  # PRs on whose head a success may still stand because the overwrite itself failed: the merge job goes red (cmd_merge)

    def run(self, verdicts):
        results = []
        for v in verdicts:
            for t in v["tests"]:
                res = self.handle(v["base"], t)
                results.append(res)
                self.log(f"#{t['pr']} -> {res['outcome']}")
                if (
                    res["outcome"] not in MERGED_OUTCOMES and t["head"] not in self.marked
                ):  # every unmerged head ends the sweep with a non-success (a success may be left from an earlier sweep)
                    failed = res["outcome"].startswith(("dequeued", "refused", "red", "conflict"))
                    self.mark(t, "failure" if failed else "pending", "not merged; re-test queued")
        return results

    def handle(self, base, t):
        n = t["pr"]
        if n in self.red:  # not a retry: a red PR is never merged in this sweep and must not keep the chain going
            return {"pr": n, "outcome": "deferred", "retry": False, "reason": "went red earlier in this sweep; never merged in it"}
        if t["result"] == "red":
            return self.on_red(base, t)
        if t["result"] == "green":
            return self.on_green(base, t)
        if t["result"] == "conflict":
            return self.on_conflict(n, f"the PR head `{t['head'][:9]}` conflicts with `{base}` `{t['base_sha'][:9]}` (local merge)")
        st, pr = self.api.get(
            f"/pulls/{n}"
        )  # `stale` is the test driver's word (a verdict file): believe it only if the API's head differs from the planned head
        if st == 200 and isinstance(pr, dict) and (pr.get("head") or {}).get("sha") == t["head"]:
            return self.on_red(
                base, {**t, "result": "red", "merge_sha": None, "failures": [{"check": "test-step", "cause": "unclassified"}]}
            )
        why = (
            f"test result `{t['result']}` (the head moved since planning); stays queued"
            if st == 200
            else f"test result `{t['result']}`; could not read the PR's head (HTTP {st}); stays queued"
        )
        return {"pr": n, "outcome": "deferred", "retry": True, "reason": why}  # the author's push starts no queue run: retry it

    # -- dequeue
    def dequeue(self, n, lines):
        """Remove `queue` (the DELETE status is checked) and comment. -> True when the label is gone."""
        if self.dry:
            self.log(f"  dry-run: WOULD remove `queue` from #{n} and comment:\n    " + "\n    ".join(lines))
            return True
        st, _ = self.api.request("DELETE", f"/issues/{n}/labels/{QUEUE_LABEL}")
        ok = st in DEQUEUE_OK
        head = (
            "**merge-queue**: removed `queue`."
            if ok
            else f"**merge-queue**: could NOT remove `queue` (HTTP {st}); #{n} stays queued and a later sweep tests it again "
            "unless a person removes the label."
        )
        tail = (
            f"\n\nRun: {self.run_url}\nFix it and apply `queue` again. "
            "The cause tag is mechanical (`timeout` or `unclassified`); reclassify with a `cause:real|env|flake` label."
        )
        self.api.request("POST", f"/issues/{n}/comments", {"body": head + "\n\n" + "\n".join(lines) + tail})
        return ok

    def on_red(self, base, t):
        n = t["pr"]
        if t["merge_sha"]:
            what = f"merge `{t['merge_sha'][:9]}` of the PR onto `{base}` `{t['base_sha'][:9]}`"
        elif any(f["check"] == "test-step" for f in t["failures"]):
            what = "its test job failed before it recorded a result (see the job's log: a head that is not in this repository, a failed API call, a setup or infrastructure error, or a crash of the test driver)"
        else:
            what = "a git step of the test"
        self.mark(
            t, "failure", "the merged result failed the required checks"
        )  # no success may stand on a red head (an older sweep may have left one)
        lines = [f"- stage: `merged-result` ({what})"]
        lines += [f"- failed: `{f['check']}`, cause: `{failure_cause(f)}`" for f in t["failures"]]
        lines += retry_lines(t.get("retry"), n, self.run_url)
        removed = self.dequeue(n, lines)
        self.red.add(n)
        for f in t["failures"]:
            print(f"::error title=failure-cause::cause={failure_cause(f)} job=merge-queue pr={n} check={f['check']}")
        res = {
            "pr": n,
            "outcome": "dequeued-red" if removed else "red-not-dequeued",
            "stage": "merged-result",
            "failures": [{"check": f["check"], "cause": failure_cause(f), "url": ""} for f in t["failures"]],
        }
        if t.get("retry"):
            res["retries"] = t["retry"].get("retries", 1)
        return res

    def on_conflict(self, n, why):
        removed = self.dequeue(
            n,
            [
                f"- conflict or refusal: {why}",
                *(retry_lines(self.tested.get("retry"), n, self.run_url) if self.tested and self.tested["pr"] == n else []),
            ],
        )
        return {"pr": n, "outcome": "dequeued-conflict" if removed else "conflict-not-dequeued", "reason": why}

    # -- merge
    def base_sha(self, base):
        st, d = self.api.get(f"/git/ref/heads/{base}")
        if st != 200:
            raise SystemExit(f"merge-queue: cannot read base {base}: HTTP {st}")
        return d["object"]["sha"]

    def deferred(self, n, why, retry=True):
        """The PR stays queued. `retry` says whether another sweep can act on it soon (the redispatch reads it)."""
        self.log(f"  deferred (stays queued): {why}")
        t = self.tested
        lines = retry_lines(t.get("retry"), n, self.run_url) if t and t["pr"] == n else []
        if lines and not self.dry:  # a candidate that needed the retry says so even when it is not merged this sweep
            self.api.request(
                "POST",
                f"/issues/{n}/comments",
                {
                    "body": f"**merge-queue**: #{n} passed after a one-time retry of failed tests and may not have merged: {why}; the next sweep checks.\n\n"
                    + "\n".join(lines)
                    + f"\n\nRun: {self.run_url}"
                },
            )
        return {"pr": n, "outcome": "deferred", "retry": retry, "reason": why}

    def on_green(self, base, t):
        n, head = t["pr"], t["head"]
        self.tested = t  # deferred() and dequeue_refused() read the retry info of the candidate in hand
        st, pr = self.api.get(f"/pulls/{n}")
        if st != 200:
            raise SystemExit(f"merge-queue: GET pull {n}: HTTP {st}")
        if pr.get("state") != "open" or QUEUE_LABEL not in [lb["name"] for lb in pr.get("labels", [])]:
            return self.deferred(n, "no longer open and queued; nothing merged", retry=False)
        if pr["base"]["ref"] != base:
            return self.deferred(n, f"the PR was retargeted to `{pr['base']['ref']}` after planning (tested on `{base}`); nothing merged")
        if pr["head"]["sha"] != head:
            return self.deferred(n, f"the head moved after the test ({head[:9]} -> {pr['head']['sha'][:9]}); tested again next sweep")
        head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")  # null for a deleted fork
        if head_repo != self.api.repo:  # in-repo heads only: re-read here, before any status or merge, from GitHub's own record of the PR
            self.log(f"  #{n}: head is not in this repository; dequeued, nothing merged")
            removed = self.dequeue(
                n,
                [
                    "- refused: this PR's head is not in this repository; only in-repo heads are tested and merged by the queue.",
                    *retry_lines(t.get("retry"), n, self.run_url),
                ],
            )
            return {
                "pr": n,
                "outcome": "dequeued-fork-head" if removed else "refused-not-dequeued",
                "reason": "head repository is not this repository",
            }
        if self.dry:
            return {
                "pr": n,
                "outcome": "dry-run-would-merge",
                "head_sha": head,
                "base_sha": t["base_sha"],
                "tested_merge_sha": t["merge_sha"],
            }
        state, _why = self.head_in_base(base, head)  # a read: a merge that landed without a 200 earlier (merge() below, 5xx) is found here
        if state == "in":
            return self.landed(base, t, "An earlier merge call probably failed after GitHub merged it.", "already-in-base")
        cur = self.base_sha(base)  # BEFORE any success status: a status says "this head, on this base, passed"
        if cur != t["base_sha"]:
            self.mark(t, "pending", "base moved; re-test queued")
            return self.deferred(
                n, f"{base} moved {t['base_sha'][:9]} -> {cur[:9]} after the test; the tested result is not what would merge"
            )
        self.post_status(t)
        try:
            return self.merge(base, t)
        except BaseException:  # a failed base read, update-branch or dequeue call after the success was posted: the head is not merged, so no success may stand on it
            self.mark(t, "pending", "interrupted before the merge was confirmed; re-test queued")
            raise

    def mark(self, t, state, why):
        """Overwrite what `merge-queue/tested` says on this head (GitHub shows the newest status of a context). A failed overwrite is NOT a warning: a success may
        still stand on a head that is not merged, so the PR is recorded and the merge job exits non-zero at the end of the sweep (cmd_merge)."""
        if self.dry:
            return
        body = {"state": state, "context": STATUS_CONTEXT, "description": why[:140]}
        if self.run_url:
            body["target_url"] = self.run_url
        try:
            st, _ = self.api.request("POST", f"/statuses/{t['head']}", body)
        except SystemExit as e:
            st = str(e)[:100]
        if st == 201:
            self.marked[t["head"]] = state
        else:
            self.status_failed.append(t["pr"])
            print(
                f"::error title=status-not-overwritten::could not post `{state}` on the head of #{t['pr']} (HTTP {st}): a success may still stand there"
            )

    def head_in_base(self, base, head):
        """Does `base` now contain the exact PR head sha? One read: GET /compare/{base}...{head}. status `behind` or `identical` means the
        head has no commit that the base lacks, i.e. the head is the base or one of its ancestors (a merge commit lists the head as a
        parent, so it counts). `ahead` or `diverged` means the head still has commits of its own. Chosen over reading the base tip's parents
        because the tip is only the merge commit when nothing else landed since; the compare answers for any later tip too.
        -> ("in", None) | ("not", None) | ("error", why); an error never reads as `not` silently: the callers say why they deferred."""
        try:
            st, d = self.api.get(f"/compare/{base}...{head}")
        except SystemExit as e:
            return "error", str(e)
        if st != 200 or not isinstance(d, dict):
            return "error", f"compare answered HTTP {st}"
        status = d.get("status")
        if status in ("behind", "identical"):
            return "in", None
        if status in ("ahead", "diverged"):
            return "not", None
        return "error", f"compare answered an unknown status {status!r}"

    def find_merge_commit(self, base, head):
        """-> (sha, parent shas) of the commit among the newest HISTORY_SCAN of `base` that has `head` as a parent, else None."""
        try:
            st, commits = self.api.get(f"/commits?sha={base}&per_page={HISTORY_SCAN}")
        except SystemExit:
            return None
        if st != 200 or not isinstance(commits, list):
            return None
        for c in commits:
            parents = [p["sha"] for p in c.get("parents", [])]
            if head in parents:
                return c["sha"], parents
        return None

    def landed(self, base, t, how, outcome="merged-after-5xx"):
        """The PR head is in the base though the merge call did not answer 200 (a 500 after GitHub had merged the PR, which left it open).
        Treat it as merged: label off, the tree check of a normal merge on the commit found, ONE comment (it carries the retry lines when
        the candidate needed the one-time retry: SDLC 7.6 CI-QUEUE-RETRIES-TESTS-ONCE).
        `outcome` is `merged-after-5xx` when THIS sweep's merge call failed (the comment says the merge succeeded despite the error) or
        `already-in-base` when the sweep only found the head in the base and no merge call failed (the queue did not merge it)."""
        n, head = t["pr"], t["head"]
        found = self.find_merge_commit(base, head)
        sha = found[0] if found else None
        # the tested tree is the merge onto t.base_sha: comparable only when the commit found has that base as its first parent
        tree = self.check_tree(n, sha, t) if found and found[1][0] == t["base_sha"] else "unverified"
        removed = self.remove_label(n)
        what = f"merge commit `{sha[:9]}`" if sha else f"no merge commit found among the newest {HISTORY_SCAN} of `{base}`"
        if outcome == "merged-after-5xx":
            said = f"#{n} is merged: {how}. The merge succeeded despite the error: {what}"
        else:
            said = f"this PR's head is already in `{base}` ({what}); the queue did not merge it in this run. {how.rstrip('.')}"
        lines = retry_lines(t.get("retry"), n, self.run_url)
        closing = "" if tree == "differs" else " Nothing more is needed."  # a tree-differs comment has just said "Check what landed"
        self.api.request(
            "POST",
            f"/issues/{n}/comments",
            {
                "body": f"**merge-queue**: {said}"
                f"{'' if removed else '; removing `queue` failed, remove it by hand'}.{closing}"
                + ("\n\n" + "\n".join(lines) if lines else "")
                + f"\n\nRun: {self.run_url}"
            },
        )
        self.log(f"  {outcome}: #{n} head {head[:9]} is in {base} ({what})")
        return {
            "pr": n,
            "outcome": outcome,
            "label_removed": removed,
            "tree_check": tree,
            "merged_sha": sha,
            "head_sha": head,
            "base_sha_tested": t["base_sha"],
            "tested_merge_sha": t["merge_sha"],
            "merge_method": MERGE_METHOD,
            "reason": how,
            **({"retries": t["retry"].get("retries", 1)} if t.get("retry") else {}),
        }

    def deferred_after_error(self, base, t, why):
        """The merge call ended in a 5xx, a timeout or a connection error: it may have landed. Look at the base before deferring."""
        state, err = self.head_in_base(base, t["head"])
        if state == "in":
            return self.landed(base, t, f"{why}, but the base now contains the tested head")
        tail = "stays queued and is tested again next sweep"
        if state == "error":
            return self.deferred(t["pr"], f"{why}; could not check whether it landed ({err}); {tail}")
        return self.deferred(t["pr"], f"{why}; the head is not in {base}; {tail}")

    def post_status(self, t):
        body = {
            "state": "success",
            "context": STATUS_CONTEXT,
            "description": f"merged result {t['merge_sha'][:9]} (head + base {t['base_sha'][:9]}) passed the required checks",
        }
        if self.run_url:
            body["target_url"] = self.run_url
        st, d = self.api.request("POST", f"/statuses/{t['head']}", body)
        if st != 201:
            raise SystemExit(
                f"merge-queue: could not post status {STATUS_CONTEXT} on {t['head'][:9]}: HTTP {st} {str(d)[:200]} (nothing merged)"
            )

    def merge(self, base, t):
        n, head = t["pr"], t["head"]
        for attempt in (1, 2):
            cur = self.base_sha(base)  # re-read immediately before the merge call
            if cur != t["base_sha"]:
                return self.deferred(
                    n, f"{base} moved {t['base_sha'][:9]} -> {cur[:9]} after the test; the tested result is not what would merge"
                )
            try:  # gh_api never re-sends this call; a 5xx or a network error ends this PR's merge, the next sweep tests it again
                st, d = self.api.request("PUT", f"/pulls/{n}/merge", {"sha": head, "merge_method": MERGE_METHOD})
            except SystemExit as e:
                return self.deferred_after_error(base, t, f"the merge call failed ({e})")
            if st == 200:
                self.comment_retry(n, t)
                return {
                    "pr": n,
                    "outcome": "merged",
                    "label_removed": self.remove_label(n),
                    "tree_check": self.check_tree(n, d["sha"], t),
                    "merged_sha": d["sha"],
                    "head_sha": head,
                    "base_sha_tested": t["base_sha"],
                    "tested_merge_sha": t["merge_sha"],
                    "merge_method": MERGE_METHOD,
                    **({"retries": t["retry"].get("retries", 1)} if t.get("retry") else {}),
                }
            msg = d.get("message") if isinstance(d, dict) else d
            self.log(f"  attempt {attempt}: merge HTTP {st}: {json.dumps(d)[:300]}")
            if st >= 500:
                return self.deferred_after_error(base, t, f"merge call answered HTTP {st} (not re-sent)")
            if st in (405, 409):
                return self.catch_up(base, t, st, msg)
            if is_workflow_refusal(st, msg):  # a second try meets the same rule: end here with the specific comment
                return self.dequeue_workflow_update_needed(n, msg)
        return self.dequeue_refused(n, st, msg)

    def comment_retry(self, n, t):
        """The PR's queue comment for a candidate that needed the retry and went on to merge (a red one gets the same lines in its dequeue comment).
        A failed comment is logged, never fatal: the merge is done."""
        lines = retry_lines(t.get("retry"), n, self.run_url)
        if not lines:
            return
        st, _ = self.api.request(
            "POST",
            f"/issues/{n}/comments",
            {
                "body": f"**merge-queue**: #{n} passed, after a one-time retry of failed tests.\n\n"
                + "\n".join(lines)
                + f"\n\nRun: {self.run_url}"
            },
        )
        if st not in (200, 201):
            self.log(f"  merged #{n}, but the retry comment answered HTTP {st}")

    def check_tree(self, n, merged_sha, t):
        """After a merge: GitHub's merge commit must have the tree that was tested. It cannot be undone, but a difference is never silent:
        a comment on the PR, an error annotation, and the job goes red (cmd_merge). -> `same`, `differs` or `unverified`."""
        want = t.get("tree_sha")
        st, d = self.api.get(f"/git/commits/{merged_sha}")
        got = d.get("tree", {}).get("sha") if st == 200 and isinstance(d, dict) else None
        if not want or not got:
            print(f"::warning title=merged-tree-unverified::could not compare the merged tree of #{n} (tested tree {want}, HTTP {st})")
            return "unverified"
        if want == got:
            return "same"
        self.tree_mismatch.append(n)
        print(f"::error title=merged-tree-differs::merged tree differs from tested tree (pr={n} tested={want[:12]} merged={got[:12]})")
        self.api.request(
            "POST",
            f"/issues/{n}/comments",
            {
                "body": f"**merge-queue**: merged tree differs from tested tree. #{n} is merged (`{merged_sha[:9]}`); "
                f"tested tree `{want[:12]}`, "
                f"merged tree `{got[:12]}`. Check what landed. Run: {self.run_url}"
            },
        )
        return "differs"

    def remove_label(self, n):
        """A merged PR leaves the queue: remove `queue` (200/204, or 404 when already gone).
        A failure is logged, never fatal: the merge is done
        and a merged PR is not listed as open, so it cannot be planned again."""
        st, _ = self.api.request("DELETE", f"/issues/{n}/labels/{QUEUE_LABEL}")
        if st not in DEQUEUE_OK:
            self.log(f"  merged #{n}, but removing `queue` answered HTTP {st}")
        return st in DEQUEUE_OK

    def catch_up(self, base, t, st, msg):
        """405/409: the head or base moved, or the ruleset wants the branch up to date. Behind -> update it (no PR code runs here);
        the next sweep tests the new head, and it stays queued. NOT behind -> nothing to catch up: dequeue with the HTTP status."""
        n, head = t["pr"], t["head"]
        try:
            cst, cmp_ = self.api.get(f"/compare/{base}...{head}")
        except SystemExit as e:  # a failed read is a failed check: defer, never crash the merge step
            return self.deferred(n, f"merge refused (HTTP {st}: {msg}) and the comparison with {base} failed ({e}); stays queued")
        if cst == 200 and not isinstance(cmp_, dict):  # a 200 whose body is not an object is a failed check too (no .get on it)
            cst = f"200 with an unreadable body ({type(cmp_).__name__})"
        if cst == 200 and cmp_.get("status") in ("behind", "identical"):  # the head is already in the base: update-branch would conflict
            why = f"The merge call was refused (HTTP {st}) because the head is already in the base."
            return self.landed(base, t, why, "already-in-base")
        if cst == 200 and cmp_.get("behind_by"):
            ust, ud = self.api.request("PUT", f"/pulls/{n}/update-branch", {"expected_head_sha": head})
            if ust == 202:
                return self.deferred(n, f"merge refused (HTTP {st}) and the PR is behind {base}: updated it; tested again next sweep")
            if ust == 422 and "conflict" in json.dumps(ud).lower():
                return self.on_conflict(n, f"update-branch refused: {ud.get('message') if isinstance(ud, dict) else ud}")
            umsg = ud.get("message") if isinstance(ud, dict) else ud
            if is_workflow_refusal(ust, umsg):  # the queue's own update-branch meets the same workflow rule
                return self.dequeue_workflow_update_needed(n, f"update-branch: {umsg}")
            return self.dequeue_refused(n, ust, f"update-branch: {str(ud)[:200]}")
        if cst != 200:
            return self.deferred(n, f"merge refused (HTTP {st}: {msg}) and the comparison with {base} failed (HTTP {cst}); stays queued")
        return self.dequeue_refused(n, st, msg)

    def dequeue_workflow_update_needed(self, n, msg):
        """HTTP 403 naming the `workflows` permission: the PR changes a workflow file and its branch is behind the base, so the merge
        commit's workflow file is a new 3-way result that this job's GITHUB_TOKEN may not create. The queue cannot cure it: its own
        update-branch uses the same token and meets the same rule. Dequeue with ONE comment: why, and what to do (and the retry lines
        when the candidate needed the one-time retry)."""
        repo = getattr(self.api, "repo", "<owner>/<repo>")
        removed = self.dequeue(
            n,
            [
                f"- merge refused: HTTP 403: {msg}",
                "- why: this PR changes a workflow file (`.github/workflows/`) and its branch is behind the base, so the merged "
                "workflow file would be new content that the queue's token may not create. A merge whose workflow files equal "
                "the PR head's succeeds.",
                f"- fix: update the branch with a user's token (`gh api -X PUT repos/{repo}/pulls/{n}/update-branch`, "
                'or "Update branch" in the web UI), wait for the checks, then apply `queue` again.',
                *(retry_lines(self.tested.get("retry"), n, self.run_url) if self.tested and self.tested["pr"] == n else []),
            ],
        )
        return {
            "pr": n,
            "outcome": "dequeued-workflow-update-needed" if removed else "refused-not-dequeued",
            "reason": f"merge refused HTTP 403 (workflow file, branch behind): {msg}",
        }

    def dequeue_refused(self, n, st, msg):
        """A refusal that catching up cannot cure (405/409 while not behind; any other 4xx still there after one retry): dequeue with the
        HTTP status and message in the comment."""
        removed = self.dequeue(
            n,
            [
                f"- merge refused: HTTP {st}: {msg}",
                "- a maintainer should look at this PR: the queue did not merge it and does not know why",
                *(retry_lines(self.tested.get("retry"), n, self.run_url) if self.tested and self.tested["pr"] == n else []),
            ],
        )
        return {
            "pr": n,
            "outcome": "dequeued-refused" if removed else "refused-not-dequeued",
            "reason": f"merge refused HTTP {st}: {msg}",
        }


# ---- the self re-dispatch (merge job) ------------------------------------------------------------------------------------------
def has_queued(api, base):
    """True when an open, non-draft PR into `base` carries `queue` (the labels as listed; no label-event reads)."""
    prs = api.pages(f"/pulls?state=open&base={base}")
    return any(not p.get("draft") and QUEUE_LABEL in [lb["name"] for lb in p.get("labels", [])] for p in prs)


def redispatch(api, results, hop, bases, log=print, dry_run=False):
    """After a sweep: start the next one with ONE workflow_dispatch of merge-queue.yml on the default branch (a GITHUB_TOKEN dispatch
    creates a run, unlike every other token-triggered event; merge-queue.yml header). Only when this sweep MERGED a PR or DEFERRED one
    that another sweep can act on (`retry`), AND some PR on any served base still carries `queue`. A sweep that only went red, or only
    dequeued, never dispatches. Bounded: the run at hop MAX_HOPS never dispatches (the daily cron is the backstop). A refused dispatch is
    logged, never fatal. -> True when a dispatch was made."""
    if dry_run or hop >= MAX_HOPS:
        return False
    if not any(r["outcome"] in MERGED_OUTCOMES or (r["outcome"] == "deferred" and r.get("retry")) for r in results):
        return False
    if not any(has_queued(api, b) for b in bases):
        return False
    st, repo = api.get("")
    if st != 200 or not isinstance(repo, dict) or not repo.get("default_branch"):
        log(f"::warning title=redispatch::could not read the default branch (HTTP {st}); not re-dispatching")
        return False
    body = {"ref": repo["default_branch"], "inputs": {"hop": str(hop + 1)}}
    try:  # gh_api never re-sends this call; a network error ends it here, never the merge job (the sweep's work is done)
        st, d = api.request("POST", f"/actions/workflows/{WORKFLOW_FILE}/dispatches", body)
    except SystemExit as e:
        log(f"::warning title=redispatch::workflow_dispatch failed ({e}); the daily sweep is the backstop")
        return False
    if st != 204:
        log(f"::warning title=redispatch::workflow_dispatch answered HTTP {st} {str(d)[:200]}; the daily sweep is the backstop")
        return False
    log(f"re-dispatched {WORKFLOW_FILE} on {repo['default_branch']} (hop {hop + 1} of {MAX_HOPS})")
    return True


def hop_of(environ=None):
    """The hop number of this run (workflow input, via MQ_HOP); anything unreadable counts as MAX_HOPS: no further dispatch."""
    try:
        return max(0, int((os.environ if environ is None else environ).get("MQ_HOP") or "0"))
    except ValueError:
        return MAX_HOPS


# ---- CLI -----------------------------------------------------------------------------------------------------------------------
def summary(results, base):
    lines = [f"### merge-queue sweep ({base})", "", "| PR | outcome | detail |", "|---|---|---|"]
    for r in results:
        detail = r.get("merged_sha") or r.get("reason") or ", ".join(f"{f['check']}={f['cause']}" for f in r.get("failures", [])) or ""
        if r["outcome"] in MERGED_OUTCOMES and not r.get("merged_sha"):  # merged, but kept out of the outputs (no sha): a human must see it
            detail = f"merged (merge commit not found among the newest {HISTORY_SCAN})"
        if r.get("retries"):
            detail += f" (retries: {r['retries']})"
        lines.append(f"| #{r['pr']} | {r['outcome']} | {detail} |")
    if not results:
        lines.append("| (none) | nothing queued | |")
    return "\n".join(lines) + "\n"


def run_url_of(repo):
    if not os.environ.get("GITHUB_RUN_ID"):
        return ""
    return f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"


def reap_group(pgid=None, grace_s=1.0, extra_pgids=None):
    """Kill the processes a PR's tests left behind: every process of this process group (the driver's own descendants: `uv sync`, git) and of every session ci_step
    started a check in (ci_step.STARTED_PGIDS: each check runs in a session of its own), except this process (SIGTERM, which this process ignores, then SIGKILL of what
    is left, found with `ps -A -o pid=,pgid=` so the same call works on macOS and Linux). A process that starts yet another session (a daemon) is outside this: a stated
    limit, not a defence against a hostile PR."""
    groups = {pgid or os.getpgrp(), *(ci_step.STARTED_PGIDS if extra_pgids is None else extra_pgids)}
    me = os.getpid()
    old = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        for g in groups:
            try:
                os.killpg(g, signal.SIGTERM)
            except OSError:
                pass
        time.sleep(grace_s)
        out = subprocess.run(["ps", "-A", "-o", "pid=,pgid="], capture_output=True, text=True, check=False, timeout=30).stdout.splitlines()
        for line in out:
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and int(parts[1]) in groups and int(parts[0]) != me:
                try:
                    os.kill(int(parts[0]), signal.SIGKILL)
                except OSError:
                    pass
    finally:
        signal.signal(signal.SIGTERM, old)


def cmd_test(a):
    cell = bool(os.environ.get("MQ_CELL_DIR"))  # a workflow cell: its own process group, reaped after the driver
    if cell and os.getpid() != os.getpgrp():
        os.setpgrp()
    if cell and os.environ.get(
        "MQ_UV_SEED"
    ):  # BEFORE any process that runs PR code: the cell's uv cache is a clone of the warm one (a missing source is an error)
        try:
            uv_seed.seed_cell(os.environ["MQ_UV_SEED"], os.path.join(os.environ["MQ_CELL_DIR"], "uv-cache"))
        except SystemExit as e:
            print(f"::error title=uv-seed::{e}")  # the job log names the missing source; nothing of the PR has run
            raise
    git = GitRepo(os.path.abspath(a.driver_dir), token=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    tester = Tester(git, a.base, default_run_checks(os.path.abspath(a.driver_dir), a.base), run_url=run_url_of(a.repo))
    os.makedirs(os.path.dirname(os.path.abspath(a.verdict_file)), exist_ok=True)
    try:
        tester.run(json.loads(a.candidates))
    finally:  # whatever was tested is written, even when the driver crashes
        with open(a.verdict_file, "w") as fh:
            json.dump(tester.verdict(), fh, indent=1)
        if cell:
            reap_group()
    # The job's CONCLUSION is the result the merge job reads, and each cell tests ONE candidate. What this guarantees is narrow: no PR's verdict FILE decides anything,
    # and every merge needs its own planned job's conclusion. It does NOT isolate one PR's code from another's on a persistent runner (a deliberately persisting PR can
    # affect later cells: that needs ephemeral runners), and a PR can always pass its own tests. So the step exits non-zero unless its one candidate is green.
    if not tester.tests or tester.tests[-1]["result"] != "green":
        raise SystemExit(f"merge-queue-test: {tester.tests[-1]['result'] if tester.tests else 'nothing tested'}")


def parse_candidates(text):
    """The plan job's `candidates` output: {base: [{n, head}]} -> {base: [(n, head)]}. Entries that are not (int, 40-hex) are dropped; fail loud on a non-mapping."""
    d = json.loads(text)
    if not isinstance(d, dict):
        raise SystemExit("merge-queue: --candidates is not a JSON object (the plan job's output)")
    return {
        b: [
            (c["n"], c["head"])
            for c in cs
            if isinstance(c, dict) and type(c.get("n")) is int and isinstance(c.get("head"), str) and SHA_RE.fullmatch(c["head"])
        ]
        for b, cs in d.items()
        if isinstance(b, str) and isinstance(cs, list)
    }


def read_conclusions(api, run_id, attempt):
    """GitHub's record of the test jobs of THIS run and attempt: {(pr, head): conclusion}. The jobs API returns names, not matrix values, so a cell is
    identified by its name `merge-queue-test (pr-<n> <40-hex head>)` (set by the plan job's matrix, never by PR code); a name that does not parse counts for
    nothing; a name seen twice is `ambiguous`. A job that is not completed has conclusion None. An API failure is fatal: nothing is decided without the record."""
    out = {}
    for page in range(1, 51):
        st, d = api.get(f"/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}")
        if st != 200 or not isinstance(d, dict) or not isinstance(d.get("jobs"), list):
            raise SystemExit(f"merge-queue: GET jobs of run {run_id} attempt {attempt}: HTTP {st}")
        for j in d["jobs"]:
            m = TEST_JOB_RE.fullmatch(j.get("name") or "")
            if not m:
                continue
            key = (int(m.group(1)), m.group(2))
            out[key] = "ambiguous" if key in out else (j.get("conclusion") if j.get("status") == "completed" else None)
        if len(d["jobs"]) < 100:
            return out
    raise SystemExit("merge-queue: more than 50 pages of jobs (fail loud)")


def read_metadata(verdict_dir, n, head):
    """The verdict file of cell PR n (the artifact `mq-verdict-<n>`, named by the workflow) as METADATA: the matching test record or None. Never decides a result."""
    try:
        d = json.load(open(os.path.join(verdict_dir, f"mq-verdict-{n}", "verdict.json")))
        for t in d["tests"]:
            if t["pr"] == n and t["head"] == head and isinstance(t, dict):
                return t
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def clean_failures(meta):
    out = []
    for f in (meta.get("failures") if isinstance(meta.get("failures"), list) else [])[:10]:
        if isinstance(f, dict):
            out.append({"check": known_check(f.get("check")), "cause": failure_cause(f)})
    return out


def clean_retry(retry):
    """The retry record, with every test id restricted to a plain id shape (an id comes from the PR's own junit file). The shape still allows `[]()` and `:`, so the
    ids are NOT mapped to known text: they are inert because retry_lines renders them only inside backticks or a fenced block, and the charset excludes the backtick
    (test_retry_ids_reach_a_comment_only_inside_a_fenced_block)."""
    if not isinstance(retry, dict):
        return {}
    ok = lambda ids: [
        i
        for i in (ids if isinstance(ids, list) else [])
        if isinstance(i, str) and re.fullmatch(r"[A-Za-z0-9_./:\[\]()+=,@#~* -]{1,300}", i)
    ][:MAX_RETRY_IDS_KEPT]  # noqa: E731
    r = retry.get("retries")
    return {
        "retries": r if type(r) is int and 0 <= r < 10 else 1,
        "failed_ids": ok(retry.get("failed_ids")),
        "flaked_ids": ok(retry.get("flaked_ids")),
    }


def settle_candidate(n, head, conclusion, meta, log=print):
    """The Merger's record of one planned candidate, decided by the job's CONCLUSION (GitHub's record of ITS OWN cell); the verdict file only adds metadata. -> dict or None (nothing acted on)."""
    if conclusion not in ("success", "failure", "timed_out"):
        log(f"  #{n}: no usable conclusion for its test job in this attempt ({conclusion!r}); not merged, stays queued (deferred)")
        return None
    meta = meta if isinstance(meta, dict) else {}
    hexed = lambda v: v if isinstance(v, str) and SHA_RE.fullmatch(v) else None  # noqa: E731
    t = {
        "pr": n,
        "head": head,
        "base_sha": hexed(meta.get("base_sha")),
        "merge_sha": hexed(meta.get("merge_sha")),
        "tree_sha": hexed(meta.get("tree_sha")),
        "failures": [],
    }
    if isinstance(meta.get("retry"), dict) and meta["retry"]:
        t["retry"] = clean_retry(meta["retry"])
    if conclusion == "success":
        if not t["base_sha"]:  # a merge needs the base the merge was tested on, which only the verdict file records
            log(
                f"  #{n}: its test job passed, but its verdict file gives no usable tested base; not merged, tested again next sweep (deferred)"
            )
            return None
        t["merge_sha"] = t["merge_sha"] or "unknown"
        t["result"] = "green"
        return t
    if conclusion == "failure" and meta.get("result") in ("conflict", "stale") and (meta["result"] == "stale" or t["base_sha"]):
        t["result"] = meta["result"]  # the driver exited non-zero for these by design; a PR can only dequeue or defer itself this way
        return t
    if not t["base_sha"]:
        t["merge_sha"] = (
            None  # a record with a merge sha but no base would break the comment's wording; say "a git step of the test" instead
        )
    t["result"] = "red"
    t["failures"] = (clean_failures(meta) if meta.get("result") == "red" else []) or [
        {"check": "test-step", "cause": "timeout" if conclusion == "timed_out" else "unclassified"}
    ]
    return t


def conclude(candidates, conclusions, verdict_dir, log=print, undecided=None):
    """-> [{base, tests}] for the Merger: one record per planned candidate, in plan order (oldest queued first), decided by `conclusions`. A planned candidate that
    could not be decided (no job in this attempt, no usable conclusion, no usable tested base) is appended to `undecided` as (pr, planned head)."""
    verdicts = []
    for base, cands in candidates.items():
        tests = []
        for n, head in cands:
            if (n, head) not in conclusions:
                log(f"  #{n}: planned, but no test job named for it ran in this attempt; not merged (deferred)")
                if undecided is not None:
                    undecided.append((n, head))
                continue
            t = settle_candidate(n, head, conclusions[(n, head)], read_metadata(verdict_dir, n, head), log)
            if t:
                tests.append(t)
            elif undecided is not None:
                undecided.append((n, head))
        if tests:
            verdicts.append({"base": base, "tests": tests})
    return verdicts


def parse_forks(text):
    """The plan job's `forks` output: a JSON list of PR numbers whose head is not in this repository. Only ints are kept."""
    d = json.loads(text or "[]")
    return [n for n in d if type(n) is int] if isinstance(d, list) else []


def dequeue_forks(merger, numbers):
    """Remove `queue` from each fork-head PR the plan job listed and say why. Bound to those numbers: nothing else is touched."""
    out = []
    for n in numbers:
        removed = merger.dequeue(
            n, ["- refused: this PR's head is not in this repository; only in-repo heads are tested and merged by the queue."]
        )
        out.append(
            {
                "pr": n,
                "outcome": "dequeued-fork-head" if removed else "refused-not-dequeued",
                "reason": "head repository is not this repository (dropped at planning)",
            }
        )
    return out


def sweep_lines(candidates, verdicts, results):
    """Per base: how many candidates were planned and tested, how many merged, and which greens were left unused (green by conclusion, but not merged because an
    earlier merge moved the base): the cost of testing every planned candidate, in the sweep's own summary."""
    by_pr = {r["pr"]: r for r in results}
    lines = ["", "| base | planned | tested | merged | greens left unused |", "|---|---|---|---|---|"]
    for base, cands in candidates.items():
        tests = next((v["tests"] for v in verdicts if v["base"] == base), [])
        merged = [t["pr"] for t in tests if by_pr.get(t["pr"], {}).get("outcome") in MERGED_OUTCOMES]
        unused = (
            [t["pr"] for t in tests if t["result"] == "green" and by_pr.get(t["pr"], {}).get("outcome") == "deferred"] if merged else []
        )
        lines.append(f"| {base} | {len(cands)} | {len(tests)} | {len(merged)} | {', '.join('#' + str(n) for n in unused) or 'none'} |")
    return "\n".join(lines) + "\n"


def cmd_merge(a):
    candidates = parse_candidates(a.candidates)
    run_id, attempt = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if not (run_id.isdigit() and attempt.isdigit()):
        raise SystemExit(
            "merge-queue: GITHUB_RUN_ID / GITHUB_RUN_ATTEMPT are not set: the jobs of this run cannot be read, so nothing is decided"
        )
    merger = Merger(Api(a.repo), run_url=run_url_of(a.repo), dry_run=a.dry_run)
    # a dispatch needs `actions: write`, which the queue App does not have: the workflow passes the job's GITHUB_TOKEN as MQ_DISPATCH_TOKEN; the same token
    # reads the jobs of this run (the job's own `actions: write` includes read; the App's token has no `actions` permission)
    dispatch_token = os.environ.get("MQ_DISPATCH_TOKEN") or None
    dispatch_api = Api(a.repo, token=dispatch_token) if dispatch_token else merger.api
    conclusions = read_conclusions(dispatch_api, run_id, attempt)
    planned = [(n, h) for cs in candidates.values() for n, h in cs]
    stall = None
    if planned and not any(
        k in conclusions for k in planned
    ):  # a name-format mismatch (or no test job at all) must not stall the queue silently
        stall = f"{len(planned)} planned candidate(s), but no test job of this attempt has a planned (pr, head) name ({len(conclusions)} test-job name(s) parsed): nothing was decided"
    undecided = []
    verdicts = conclude(candidates, conclusions, a.verdict_dir, undecided=undecided)
    for n, h in undecided:  # a success left on a planned head by an earlier sweep must not outlive a sweep that could not decide it
        merger.mark({"pr": n, "head": h}, "pending", "no usable test result; re-test queued")
    results = merger.run(verdicts)
    results += dequeue_forks(merger, parse_forks(a.forks))
    text = summary(results, ", ".join(v["base"] for v in verdicts) or "none") + sweep_lines(candidates, verdicts, results)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(text)
    # STRICT on purpose: the outputs (merged_sha, merged_prs) are read by consumers that expect a sha, so a merged PR whose merge commit was
    # not found is left out of them; summary() lists it as "merged (merge commit not found ...)" so a person sees it.
    merged = [r for r in results if r["outcome"] in MERGED_OUTCOMES and r.get("merged_sha")]
    if a.result_file:
        with open(a.result_file, "w") as fh:
            json.dump({"run_url": merger.run_url, "merged": merged, "results": results}, fh, indent=1)
    if os.environ.get("GITHUB_OUTPUT") and merged:
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"merged_sha={merged[-1]['merged_sha']}\nmerged_prs={','.join(str(r['pr']) for r in merged)}\n")
    redispatch(dispatch_api, results, hop_of(), QUEUE_BASES, dry_run=a.dry_run)  # once per sweep, after the outputs are written
    flag_queued_drafts(merger.api, QUEUE_BASES, dry_run=a.dry_run)  # the merger's own Api (its PR-comment credentials); never fatal
    if merger.tree_mismatch:  # after the results and outputs are written: the job is red, the merge stands
        raise SystemExit(f"merge-queue: merged tree differs from tested tree for PR(s) {merger.tree_mismatch}")
    if merger.status_failed:
        raise SystemExit(
            f"merge-queue: could not overwrite merge-queue/tested on the head of PR(s) {sorted(set(merger.status_failed))}: a success may still stand there"
        )
    if stall:  # red, with the reason: after everything else is done
        raise SystemExit(f"merge-queue: {stall}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("test")
    t.add_argument("--repo", required=True)
    t.add_argument("--base", required=True)
    t.add_argument("--candidates", required=True)
    t.add_argument("--driver-dir", default=".")
    t.add_argument("--verdict-file", required=True)
    m = sub.add_parser("merge")
    m.add_argument("--repo", required=True)
    m.add_argument("--verdict-dir", required=True)
    m.add_argument("--candidates", required=True, help="the plan job's `candidates` output (JSON); a merge is decided only for these")
    m.add_argument(
        "--forks", default="[]", help="the plan job's `forks` output (JSON list of PR numbers whose head is not in this repository)"
    )
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--result-file", default=None)
    a = ap.parse_args(argv)
    (cmd_test if a.cmd == "test" else cmd_merge)(a)


if __name__ == "__main__":
    main()
