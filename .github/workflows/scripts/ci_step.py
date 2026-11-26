#!/usr/bin/env python3
"""The one definition of the required checks, and the failure-cause tag (see the data contract in ci.yml's header).

    ci_step.py run NAME [--tree DIR] [--timeout SECONDS]   run required check NAME in DIR; exit 0 / non-zero; 124 on timeout
    ci_step.py run checks --skip-suite                     the `checks` table WITHOUT its pytest step (a PR labelled `queue`; see below)
    ci_step.py run checks --retry-flaky                    the one-time retry of failed tests (the nightly leg; the merge queue calls run_check itself; see below)
    ci_step.py report NAME                                 (run on failure) write the cause tag to the job summary AND an annotation

CAUSE TAG (mechanical, never a guess): `timeout` when a step exceeded its timeout (this script kills the process group and
records it; exit 124), otherwise `unclassified`. Whether a red was real, environmental or a flake is decided later by a person
or agent with a PR label `cause:real` / `cause:env` / `cause:flake`.
The cause is kept in <cause dir>/cause-NAME (CI_CAUSE_DIR, else RUNNER_TEMP, else the system temp dir).

PR-CI YIELDS TO THE MERGE QUEUE (SDLC.md 7.6 CI-PRCI-YIELDS-TO-QUEUE): for a PR labelled `queue`, a PR-level workflow may run `run checks --skip-suite`: every step of the
table but the pytest suite (the lint gate, lock sources, pip-audit), so a serial runner is free for the queue's own full run. The default is the whole table; the merge
queue never passes the flag. A check with no suite step refuses the flag (a flag that skips nothing is not a silent green).

ONE-TIME RETRY OF FAILED TESTS (maintainers' ruling of 2026-11-20 for the merge queue, extended to a nightly leg by the maintainers' ruling of 2026-11-23:
the same rule in both places): run_check(..., retry_flaky=True) re-runs the pytest step ONCE, for the test ids that failed on attempt 1 and nothing else, when
and only when attempt 1 ended with rc 1 and its junit file shows failures or errors on test cases (no collection error, a readable file, at most MAX_RETRY_IDS
ids). A pass on retry is reported through `retry_info` (the queue turns it into `queue-flake:` lines). Lint, lock sources, pip-audit, gitleaks and smoke never
retry. Two callers pass the flag: the merge queue (in process) and a nightly workflow (`ci_step.py run checks --retry-flaky`); a PR-level run never does: the
default is off.

THE NIGHTLY RECORD (a green after a retry is not silent): when `run --retry-flaky` ran the retry, it prints one `nightly-flake: <test id> run=<run url>` line per id
that failed on attempt 1 and passed on the retry (the url from GITHUB_SERVER_URL / GITHUB_REPOSITORY / GITHUB_RUN_ID, else `run=local`), and one
`nightly-still-failing: <test id> run=<url>` line per id still red after it (the step stays red). It writes the same lines plus a count line to $GITHUB_STEP_SUMMARY,
and the flake lines, as one JSON list on one line, to $GITHUB_OUTPUT as `flake_lines=`: a nightly workflow publishes that as a job output and its closing job hands it
to an issue script, which comments the lines on the issue it closes (or only prints them when none is open).
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

# name -> [(argv, timeout seconds), ...]: the steps of one required check, run in order; the first red step stops the check (a cheap step first).
# The NAME is the stable GitHub job name of the same check in ci.yml. FactLattice: every command runs the interpreter of the tree's own
# `uv sync --locked --extra test --extra semantic` venv (venv_python(): run_argv then also proves the venv exists), never a bare `python`.
# The timeouts are what merge_queue.py's time-budget invariant is computed from (test_the_time_budget_fits_the_job_timeout): change one and the
# budget moves with it, or the test goes red. They are set from measurements on a development machine and to be reset from the first
# real run on the hosted runner.
VENV_PY = ".venv/bin/python"
JUNIT = "{JUNIT}"  # placeholder in an argv: the path of this run's junit file (see skip_summary)
GATE = "{GATE}"  # placeholder in an argv: the GATE checkout, i.e. the default branch's copy in the merge queue (run_check's gate_dir), else the tree itself
PIP_AUDIT_VERSION = (
    "2.10.1"  # pip-audit is run through `uv tool run --from pip-audit==<this>`; it is NOT in uv.lock (see tools/ci_pip_audit.py)
)
CHECKS = {
    "checks": [
        # every package source in uv.lock is the default PyPI index (or the one named root project); stdlib only
        ([VENV_PY, "tools/ci_lock_sources.py"], 120),
        # the lint and complexity gate (SDLC.md 7.6): ruff's selection with the ignore list, the C901 ratchet, ruff format on added files. It runs the GATE's
        # copy (the default branch's, from the queue's driver checkout) on the tree under test, so a change cannot loosen the gate that judges it
        ([VENV_PY, GATE + "/tools/ci_lint.py", "all"], 600),
        # the installed locked environment has no known vulnerability (needs network access to the advisory database)
        ([VENV_PY, "tools/ci_pip_audit.py"], 900),
        # the FactLattice suite (tests/); the junit file feeds the skipped-test summary
        # junit_family=xunit1 puts the file path on every testcase: the failed test ids for the one-time retry are read from it, never scraped from the log
        ([VENV_PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit1", f"--junitxml={JUNIT}", "tests"], 3600),
    ],
    # secrets scan only; NO private-term or disclosure scan here (that runs at publication). --require-history: the PR's commits
    # (BASE_REF..HEAD) are scanned too, and the check is red when BASE_REF is not set or not resolvable.
    "gitleaks": [([VENV_PY, "tools/ci_gitleaks.py", "--require-history"], 900)],
    # package imports on the pinned Python, and lts_selfcheck through `python -m factlattice_engine call` on the built-in example registry
    "smoke": [([VENV_PY, "tools/ci_smoke.py"], 600)],
}
TIMEOUT_RC = 124
RETRY_TIMEOUT_S = (
    900  # the one retry runs only the failed ids; this bound is part of merge_queue.WORST_CANDIDATE_S (the time-budget invariant)
)
COLLECT_TIMEOUT_S = (
    300  # the `--collect-only` run that proves attempt 1 ran every collected test; also part of merge_queue.WORST_CANDIDATE_S
)
MAX_RETRY_IDS = 50  # more failing ids than this is a broken change, not flakes: no retry
PYTEST_FAILED_RC = (
    1  # pytest's "some tests failed"; 2 interrupted (collection errors), 3 internal error, 4 usage, 5 no tests: never retried
)


def cause_dir():
    d = os.environ.get("CI_CAUSE_DIR") or os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    os.makedirs(d, exist_ok=True)
    return d


def cause_path(name):
    return os.path.join(cause_dir(), f"cause-{name}")


def isolated_env(env=None):
    """The environment of a check: the caller's, with HOME and every engine state/backup dir pointed at a fresh scratch folder and every
    FACTLATTICE_* variable dropped (that is where a registry pointer would come from; the registry variables are not named here, the
    registry guard in tests/ allows only factlattice/registry.py to name them), so a check never reads or writes the runner's real
    registry or state (the same isolation the suite itself sets up). The scratch folder is made under the cause
    dir and left in place."""
    out = dict(os.environ if env is None else env)
    out = {k: v for k, v in out.items() if not k.startswith("FACTLATTICE_")}
    scratch = tempfile.mkdtemp(prefix="ci-home-", dir=cause_dir())
    for var, sub in (("HOME", "home"), ("FACTLATTICE_STATE_DIR", "st"), ("FACTLATTICE_BACKUP_DIR", "bk"), ("LTS_ENGINE_STATE_DIR", "lt")):
        out[var] = os.path.join(scratch, sub)
        os.makedirs(out[var], exist_ok=True)
    out["PYTHONDONTWRITEBYTECODE"] = "1"
    return out


STARTED_PGIDS = []  # the process group (= session) id of every check this process started: each runs in a session of its own, so the merge driver's reap needs them


def run_argv(name, argv, tree, timeout, env=None):
    """Run argv in tree with a hard timeout; record the cause. Returns the exit status (124 = timeout)."""
    open(cause_path(name), "w").write("unclassified\n")  # a non-zero exit is red; the tag below refines it only mechanically
    started = time.time()
    print(f"[ci_step] {name}: {' '.join(argv)}  (cwd {tree}, timeout {timeout}s)", flush=True)
    if not os.path.exists(os.path.join(tree, argv[0])) and "/" in argv[0]:
        print(f"[ci_step] {name}: FAIL: {argv[0]} does not exist in the tree under test (a missing check is never a pass)", flush=True)
        return 127
    p = subprocess.Popen(argv, cwd=tree, env=env, start_new_session=True)
    STARTED_PGIDS.append(p.pid)
    try:
        rc = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGTERM)
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
        open(cause_path(name), "w").write("timeout\n")
        print(f"[ci_step] {name}: TIMEOUT after {timeout}s (cause=timeout)", flush=True)
        return TIMEOUT_RC
    print(f"[ci_step] {name}: exit {rc} in {time.time() - started:.1f}s", flush=True)
    return rc


def venv_python(tree, env=None):
    """The interpreter of the tree's locked venv. CI keeps the venv OUTSIDE the tree (UV_PROJECT_ENVIRONMENT): a venv inside the tree is
    scanned by tests/_registry_guard.py (it skips no `.venv`), which fails test_p2_1_guard_the_real_tree_is_clean_by_ast on every run."""
    root = (os.environ if env is None else env).get("UV_PROJECT_ENVIRONMENT") or os.path.join(tree, ".venv")
    return os.path.join(root, "bin", "python")


def junit_cases(junit_path):
    """-> [(test id, state)] for every testcase of a junit file written with junit_family=xunit1; state is `passed`, `failed`, `skipped` or
    `collection` (a collection error: no class and an error element). The id is the pytest node id (`file::Class::name`), rebuilt from the
    `file` attribute and the dotted classname. A case with no `file` has id None. Raises OSError / ParseError on an unreadable file."""
    import xml.etree.ElementTree as ET

    out = []
    for c in ET.parse(junit_path).getroot().iter("testcase"):
        err = c.find("error") is not None
        failed = err or c.find("failure") is not None
        classname, name, path = c.get("classname") or "", c.get("name") or "", c.get("file")
        if failed and err and not classname and "collection" in (c.find("error").get("message") or ""):
            out.append((None, "collection"))
            continue
        state = "failed" if failed else "skipped" if c.find("skipped") is not None else "passed"
        tid = None
        if path:
            mod = path[:-3].replace("/", ".") if path.endswith(".py") else None
            rest = classname[len(mod) + 1 :] if mod and classname.startswith(mod + ".") else ""
            tid = (
                "::".join([path] + ([p for p in rest.split(".") if p] if rest else []) + [name])
                if (mod and (classname == mod or rest))
                else None
            )
        out.append((tid, state))
    return out


def retryable_failures(rc, junit_path):
    """-> (ids, reason). ids is the sorted list of failed test ids when a retry is allowed, else None and the reason says why not (printed: a retry
    that was refused is never silent). Allowed only when rc == 1 AND the junit file shows failed test cases, every one with a usable id, none a
    collection error, and no more than MAX_RETRY_IDS distinct ids."""
    if rc != PYTEST_FAILED_RC:
        return (
            None,
            f"pytest exit code {rc} is not {PYTEST_FAILED_RC} (timeout, collection error, internal error and usage error never retry)",
        )
    try:
        cases = junit_cases(junit_path)
    except Exception as e:  # noqa: BLE001: any unreadable file is a refusal, never a guess
        return None, f"no readable junit file ({type(e).__name__})"
    if any(state == "collection" for _, state in cases):
        return None, "the junit file shows a collection error"
    failed = [tid for tid, state in cases if state == "failed"]
    if not failed:
        return None, "rc 1 but no failed test case in the junit file"
    if any(tid is None for tid in failed):
        return None, "a failed test case has no usable test id"
    ids = sorted(set(failed))
    if len(ids) > MAX_RETRY_IDS:
        return None, f"{len(ids)} failed test ids (more than {MAX_RETRY_IDS}): not a flake"
    return ids, ""


COLLECTED_RE = re.compile(r"^(\d+)(?:/\d+)? tests? collected")


def collect_ids(argv, tree, env):
    """-> (ids, reason): the test ids a `--collect-only -q` of the same pytest command lists, run BEFORE attempt 1 (collection runs no test, so
    nothing can stop it early, and a test of attempt 1 cannot have changed the tree it reads). ids is None, with the reason, on any doubt: the
    collect-only run fails, does not finish within COLLECT_TIMEOUT_S, lists no id, or its ids do not match its `N tests collected` line."""
    cargv = [a for a in argv[:-1] if not a.startswith("--junitxml=")] + ["--collect-only", "tests"]
    try:
        p = subprocess.Popen(cargv, cwd=tree, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        STARTED_PGIDS.append(p.pid)
    except OSError as e:
        return None, f"the collect-only run did not start ({type(e).__name__})"
    try:
        out, _ = p.communicate(timeout=COLLECT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        return None, f"the collect-only run exceeded {COLLECT_TIMEOUT_S}s"
    if p.returncode != 0:
        return None, f"the collect-only run exited {p.returncode}"
    lines = out.splitlines()
    ids = {ln.strip() for ln in lines if "::" in ln and not ln.startswith((" ", "<", "="))}
    counts = [int(m.group(1)) for m in (COLLECTED_RE.match(ln.strip()) for ln in lines) if m]
    if not ids or len(counts) != 1 or counts[0] != len(ids):
        return None, f"the collect-only output does not add up ({len(ids)} ids listed, count lines {counts})"
    return ids, ""


def ran_everything(collected, junit_path):
    """-> (ok, reason). Attempt 1 stopped early when the tree's own options (-x, --maxfail, addopts) or a test (pytest.exit) cut it short with rc 1:
    the failures in its junit file are then only the ones that RAN. The retry is allowed only when every test in `collected` (collect_ids, run
    before attempt 1) has a testcase in attempt 1's junit file. Any doubt: not ok."""
    if not collected:
        return False, "no collected ids (the collect-only run before attempt 1 gave none)"
    try:
        seen = {tid for tid, _ in junit_cases(junit_path) if tid}
    except Exception as e:  # noqa: BLE001
        return False, f"no readable junit file ({type(e).__name__})"
    missing = sorted(collected - seen)
    if missing:
        return False, f"attempt 1 did not run {len(missing)} of {len(collected)} collected test(s) (stopped early?), e.g. {missing[0]}"
    return True, ""


def retry_failed_tests(argv, tree, timeout, env, ids, name):
    """The one retry: pytest on the failed ids only, in a fresh scratch home. -> (rc, passed ids). A retry that exits 0 but cannot show every id
    as passed in its own junit file is red (the flake record must be complete)."""
    junit2 = os.path.join(cause_dir(), f"junit-{name}-retry-{os.getpid()}-{time.time_ns()}.xml")
    head = argv[:-1]  # everything before the `tests` target
    head = [f"--junitxml={junit2}" if a.startswith("--junitxml=") else a for a in head]
    print(f"[ci_step] {name}: RETRY (once): {len(ids)} failed test id(s) from attempt 1", flush=True)
    rc = run_argv(name, head + ids, tree, min(timeout, RETRY_TIMEOUT_S) if timeout else RETRY_TIMEOUT_S, isolated_env(env))
    try:
        passed = sorted(tid for tid, state in junit_cases(junit2) if state == "passed" and tid in ids)
    except Exception:  # noqa: BLE001
        passed = []
    if rc == 0 and passed != ids:
        print(f"[ci_step] {name}: the retry exited 0 but its junit file does not show every id passed: red (fail closed)", flush=True)
        rc = 1
    return rc, passed


def _skip_suite_is_noop(name, skip_suite):
    """True (after saying so) when --skip-suite was asked for a check that has no pytest step: the flag must never be a silent no-op."""
    if skip_suite and not any(any(JUNIT in a for a in argv) for argv, _ in CHECKS[name]):
        print(
            f"[ci_step] {name}: --skip-suite: check `{name}` has no suite step; refusing (a flag that skips nothing is not a pass)",
            flush=True,
        )
        return True
    return False


def _checks_skip_summary(name, skip_suite, suite_ran, junit):
    """The end of run_check for `checks`: print the skipped-test summary, unless the suite was skipped on purpose (said so above; no junit file) or never ran."""
    if name != "checks" or skip_suite:
        return
    if suite_ran:
        skip_summary(junit)  # also after a red suite: the count of what did not run is part of the result
    else:
        print("[ci_step] checks: skipped-test summary not printed: the suite did not run (an earlier step was red)")


def run_check(name, tree, timeout=None, env=None, retry_flaky=False, retry_info=None, gate_dir=None, skip_suite=False):
    """Run every step of check NAME in order; stop at the first red one. -> that step's exit status (0 when all are green).
    `timeout`, when given, replaces each step's own limit (the CLI's --timeout).
    `retry_flaky` (the merge queue and `run --retry-flaky` pass True; the default is off): re-run the pytest step ONCE for its failed ids, see the module docstring.
    `retry_info`, a dict, is filled when a retry ran: {"retries": 1, "failed_ids": [...], "flaked_ids": [ids that passed on retry]}.
    `gate_dir`: a checkout of the DEFAULT branch (the merge queue passes its driver checkout). The lint gate step runs from there, against `tree`. When it
    is None (ci.yml, a local run) the gate is the tree itself: the change's own copy, which is author feedback only, never the merge gate.
    `skip_suite` (ci.yml passes it for a PR labelled `queue`; the default is off and the merge queue never passes it): leave out the pytest step, run
    the rest. A check that has no pytest step is refused with exit 2, so the flag can never be a silent no-op."""
    if _skip_suite_is_noop(name, skip_suite):
        return 2
    base_env = env
    env = isolated_env(base_env)
    # a junit file of THIS run only (F-F): a fresh name each call, so a file left by an earlier candidate in the shared cause dir is never read
    junit = os.path.join(cause_dir(), f"junit-{name}-{os.getpid()}-{time.time_ns()}.xml")
    rc, suite_started = 0, False
    for argv, default_timeout in CHECKS[name]:
        is_suite = any(JUNIT in a for a in argv)
        if is_suite and skip_suite:
            print(
                f"[ci_step] {name}: the suite was skipped (--skip-suite: the PR is labelled `queue`; the merge queue runs it on the merged result)",
                flush=True,
            )
            _summary_append(
                "### checks: suite skipped\n- suite not run on this head: skipped because the PR was queued; push a commit for a suite result\n- the merge queue runs the full suite on the merged result\n"
            )
            continue
        suite_started = suite_started or is_suite
        argv = [junit if a == JUNIT else a.replace(JUNIT, junit).replace(GATE, gate_dir or tree) for a in argv]
        if argv[0] == VENV_PY:
            argv = [venv_python(tree, env)] + argv[1:]
        collected = (None, "")
        if retry_flaky and is_suite and argv[-1] == "tests":  # BEFORE attempt 1: see collect_ids
            collected = collect_ids(argv, tree, env)
        rc = run_argv(name, argv, tree, timeout or default_timeout, env)
        if rc and retry_flaky and is_suite:  # ONLY the pytest step; lint, lock sources, pip-audit, gitleaks and smoke are deterministic
            ids, why = retryable_failures(rc, junit)
            if ids is None:
                print(f"[ci_step] {name}: no retry: {why}", flush=True)
            elif argv[-1] != "tests":
                print(f"[ci_step] {name}: no retry: the pytest command has no `tests` target to replace", flush=True)
            elif collected[0] is None:
                print(f"[ci_step] {name}: no retry: {collected[1]}", flush=True)
            elif not (full := ran_everything(collected[0], junit))[0]:
                print(f"[ci_step] {name}: no retry: {full[1]}", flush=True)
            else:
                rc, passed = retry_failed_tests(argv, tree, timeout, base_env, ids, name)
                if retry_info is not None:
                    retry_info.update(retries=1, failed_ids=ids, flaked_ids=passed)
        if rc:
            break
    _checks_skip_summary(name, skip_suite, suite_started or rc == 0, junit)
    if name == "checks" and rc == 0 and suite_started:
        rc = apply_skip_pin(name, junit, gate_dir, tree)
    return rc


def run_url(env=None):
    """The URL of this Actions run from GITHUB_SERVER_URL / GITHUB_REPOSITORY / GITHUB_RUN_ID, or `local` when any of the three is absent."""
    e = os.environ if env is None else env
    parts = [e.get("GITHUB_SERVER_URL"), e.get("GITHUB_REPOSITORY"), e.get("GITHUB_RUN_ID")]
    return f"{parts[0].rstrip('/')}/{parts[1]}/actions/runs/{parts[2]}" if all(parts) else "local"


def flake_lines(flaked_ids, still_failing_ids, env=None):
    """-> the fixed, searchable record lines: `nightly-flake: <id> run=<url>` per flaked id, then `nightly-still-failing: <id> run=<url>` per id that stayed red."""
    url = run_url(env)
    return [f"nightly-flake: {i} run={url}" for i in sorted(flaked_ids)] + [
        f"nightly-still-failing: {i} run={url}" for i in sorted(still_failing_ids)
    ]


def record_nightly_retry(retry_info, out=print):
    """After `run --retry-flaky`: nothing when no retry ran. Else print the lines, append them and a count line to $GITHUB_STEP_SUMMARY, and (flaked ids only)
    append `flake_lines=<json list>` to $GITHUB_OUTPUT (one line, so no heredoc delimiter is needed)."""
    if not retry_info.get("retries"):
        return
    flaked = list(retry_info.get("flaked_ids", []))
    still = [i for i in retry_info.get("failed_ids", []) if i not in flaked]
    lines = flake_lines(flaked, still)
    count = f"nightly-retry: {len(retry_info.get('failed_ids', []))} test id(s) failed on attempt 1; {len(flaked)} passed on the one retry (flaked), {len(still)} still failing"
    for ln in lines:
        out(ln)
    out(f"[ci_step] {count}")
    # the lines go in a fence, each verbatim on its own line (a bullet prefix would change the searchable line; a bare line would be merged into one paragraph)
    _summary_append(f"### nightly retry\n- {count}\n\n```\n" + "".join(f"{ln}\n" for ln in lines) + "```\n")
    path = os.environ.get("GITHUB_OUTPUT")
    if path and flaked:
        with open(path, "a") as fh:
            fh.write("flake_lines=" + json.dumps(flake_lines(flaked, [])) + "\n")


def skip_summary(junit_path, out=print):
    """The maintainers' ruling of 2026-11-18: every run of `checks` prints, and writes to the job summary, how many tests were skipped and
    how many test FILES were skipped at module level (a collection skip, e.g. importorskip), with the standing reason for the onnx gap.
    Read from the junit file pytest wrote; a missing or unreadable file is said so, never a silent zero. -> (skipped, module_level) or None."""
    import xml.etree.ElementTree as ET

    try:
        cases = list(ET.parse(junit_path).getroot().iter("testcase"))
    except (OSError, ET.ParseError) as e:
        line = f"skipped-test count NOT AVAILABLE: no readable junit file ({type(e).__name__}); the suite may not have started"
        out(f"[ci_step] checks: {line}")
        _summary_append(f"- **{line}**\n")
        return None
    skipped = [
        c for c in cases if c.find("skipped") is not None and c.find("skipped").get("type") != "pytest.xfail"
    ]  # an xfail is not a skip
    module_level = [c for c in skipped if not c.get("classname")]  # a collection skip has no class: its name is the module
    # pytest writes a collection skip as message="collection skipped" and the reason ("could not import 'onnx'") in the element's TEXT
    onnx = [c for c in module_level if "onnx" in ((c.find("skipped").get("message") or "") + (c.find("skipped").text or ""))]
    lines = [
        f"tests run: {len(cases) - len(skipped)}; skipped: {len(skipped)}; test files skipped at module level: {len(module_level)}",
    ]
    if module_level:
        lines.append(
            f"{len(onnx)} of those {len(module_level)} files import `onnx`, which no extra of pyproject provides: the semantic tests do not "
            "run here (known gap; adding onnx to the test extra is a separate follow-up PR)"
            if onnx
            else f"{len(module_level)} test files were skipped at module level (see the junit reasons)"
        )
    for ln in lines:
        out(f"[ci_step] checks: {ln}")
    _summary_append("### checks: skipped tests\n" + "".join(f"- {ln}\n" for ln in lines))
    return len(skipped), len(module_level)


DECLARED_SKIPS = os.path.join(
    ".github", "workflows", "declared-skips.json"
)  # in the GATE checkout (the default branch's copy), never the PR's


def read_skips(junit_path):
    """-> [{"file", "name", "reason"}] for every skipped test case of a junit file (an xfail is not a skip; a collection skip carries its reason in the element's text), or None
    when the file is not readable."""
    import xml.etree.ElementTree as ET

    try:
        cases = list(ET.parse(junit_path).getroot().iter("testcase"))
    except (OSError, ET.ParseError):
        return None
    out = []
    for c in cases:
        s = c.find("skipped")
        if s is None or s.get("type") == "pytest.xfail":
            continue
        msg = s.get("message") or ""
        reason = msg
        if msg == "collection skipped":  # the text is a tuple repr: (path, line, 'Skipped: <reason>')
            text = s.text or ""
            m = re.search(r"Skipped: (.*?)[\"']\)\s*$", text, re.S)
            reason = m.group(1) if m else text
        out.append({"file": c.get("file") or c.get("name") or "", "name": c.get("name") or "", "reason": reason})
    return out


def load_declared(path):
    """The declared skips: a JSON list of {file, reason (a regex searched in the skip's reason), max (how many skips it covers), why}. Raises ValueError for anything else: a top level
    that is not a list, an entry of the wrong shape, an invalid regex."""
    entries = json.load(open(path))
    if not isinstance(entries, list):
        raise ValueError(f"the top level is a {type(entries).__name__}, not a list of entries")
    for e in entries:
        if not (
            isinstance(e, dict)
            and isinstance(e.get("file"), str)
            and isinstance(e.get("reason"), str)
            and type(e.get("max")) is int
            and e.get("why")
        ):
            raise ValueError(f"declared skip without file, reason, max and why: {e!r}")
        try:
            re.compile(e["reason"])
        except re.error as err:
            raise ValueError(f"invalid reason regex {e['reason']!r}: {err}") from err
    return entries


def classify_skips(skips, declared):
    """-> (problems, unseen). `problems` is [(skip, kind)] for every skip no declared entry covers, with kind one of `file-not-declared` (no entry names the file),
    `reason-unmatched` (the file is declared, no entry's reason regex is found in the skip's reason) or `over-count` (an entry matches but has used up its `max`); `unseen` is
    the declared entries nothing matched (a stale declaration). A skip is covered by the first entry with the same file whose reason regex is found in its reason and that has not
    used up its `max`."""
    used = [0] * len(declared)
    problems = []
    for s in skips:
        over = False
        for i, e in enumerate(declared):
            if e["file"] == s["file"] and re.search(e["reason"], s["reason"], re.S):
                if used[i] < e["max"]:
                    used[i] += 1
                    break
                over = True
        else:
            kind = "over-count" if over else ("reason-unmatched" if any(e["file"] == s["file"] for e in declared) else "file-not-declared")
            problems.append((s, kind))
    return problems, [e for i, e in enumerate(declared) if not used[i]]


def undeclared_skips(skips, declared):
    """-> the skips no declared entry covers (see classify_skips)."""
    return [s for s, _ in classify_skips(skips, declared)[0]]


SKIP_PIN_MODES = ("report", "enforce")


def skip_pin_mode(value):
    """MQ_ENFORCE_DECLARED_SKIPS: `enforce` fails the check on a problem; `report` only reports it. -> (mode, known): any other value is reported ONLY (known False; the caller says so)."""
    return (value, True) if value in SKIP_PIN_MODES else ("report", False)


def enforce_declared_skips(name, junit, gate_dir, tree, out=print, mode="enforce"):
    """In a merge-queue cell of this repository, after a green `checks` suite: compare every skip with the gate checkout's declared-skips.json. -> the exit status to add:
    in `enforce` mode 1 when anything is undeclared (the check goes red, the skips are listed), and 1 when the list or the junit file cannot be read or compared (the pin is not
    silently off); in `report` mode ALWAYS 0, whatever happens: the full comparison (undeclared files, unmatched reasons, over-counts, declarations nothing matched) is printed as
    `::warning` lines and a one-line job-summary entry, and ANY failure to load, parse or compare (an invalid regex, a list of the wrong shape, an unexpected exception) is a
    `::warning` naming the exception class and the cause. The cell's result is never changed by report mode."""
    report = mode != "enforce"
    tag = "::warning title=declared-skips::" if report else ""
    fail = 0 if report else 1
    declared_path = os.path.join(gate_dir or tree, DECLARED_SKIPS)
    try:
        skips = read_skips(junit)
        if skips is None:
            out(f"{tag}[ci_step] {name}: declared-skip pin ({mode}): no readable junit file; not checked")
            _summary_append(f"### {name}: skips\n- declared-skip pin ({mode}): no readable junit file; not checked\n")
            return fail
        declared = load_declared(declared_path)
        problems, unseen = classify_skips(skips, declared)
    except Exception as e:  # report mode must never change the cell's result: ANY exception becomes a message
        why = f"{type(e).__name__}: {e}"
        out(f"{tag}[ci_step] {name}: declared-skip pin ({mode}): cannot compare ({declared_path}): {why}")
        _summary_append(f"### {name}: skips\n- declared-skip pin ({mode}): cannot compare: {why}\n")
        return fail
    line = f"declared-skip pin ({mode}): {len(skips)} skipped, {len(skips) - len(problems)} declared, {len(problems)} undeclared, {len(unseen)} declaration(s) nothing matched"
    out(f"{tag}[ci_step] {name}: {line}")
    _summary_append(
        f"### {name}: skips\n- {line}\n"
        + "".join(f"- UNDECLARED ({k}): `{s['file']}` {s['name']}: {s['reason'][:160]}\n" for s, k in problems[:50])
    )
    for s, k in problems[:50]:
        out(
            f"{tag}[ci_step] {name}: UNDECLARED SKIP ({k}) {s['file']}::{s['name']}: {s['reason'][:200]} (declare it in {DECLARED_SKIPS} with its reason, or fix the test)"
        )
    for e in unseen[:50]:
        out(f"{tag}[ci_step] {name}: declared skip nothing matched: {e['file']} /{e['reason'][:80]}/ (max {e['max']})")
    return 1 if (problems and not report) else 0


def apply_skip_pin(name, junit, gate_dir, tree):
    """A queue cell of a repository that declares its skips (MQ_ENFORCE_DECLARED_SKIPS set; the list is read from the gate): report or enforce. -> the exit status to return.
    `enforce` applies only to a cell that ran with a seed (MQ_UV_SEED set: a self-hosted runner), because the list was measured with the release-gate tests running; a cell without
    one (a hosted runner) gets a notice and no check. A value that is neither mode is an `::error` annotation and a summary line, and is treated as `report` (it never fails a cell)."""
    pin = os.environ.get("MQ_ENFORCE_DECLARED_SKIPS")
    if not pin:
        return 0
    mode, known = skip_pin_mode(pin)
    if not known:
        msg = f"MQ_ENFORCE_DECLARED_SKIPS={pin!r} is not `report` or `enforce`: reporting only (this is a typo to fix; it does not fail the cell)"
        print(f"::error title=declared-skips::{msg}", flush=True)
        _summary_append(f"### {name}: skips\n- ERROR: {msg}\n")
    if mode == "enforce" and not os.environ.get("MQ_UV_SEED"):
        print(
            f"::notice title=declared-skips::[ci_step] {name}: declared-skip pin (enforce) not applied: this cell ran without a uv seed (a hosted runner), where the list was not measured",
            flush=True,
        )
        _summary_append(f"### {name}: skips\n- declared-skip pin (enforce) not applied: no uv seed in this cell\n")
        return 0
    try:
        rc = enforce_declared_skips(name, junit, gate_dir, tree, mode=mode)
    except Exception as e:  # belt and braces: nothing here may change a report-mode cell
        if mode == "enforce":
            raise
        print(f"::warning title=declared-skips::[ci_step] {name}: declared-skip pin (report) failed: {type(e).__name__}: {e}", flush=True)
        return 0
    if rc:
        open(cause_path(name), "w").write("unclassified\n")
    return rc


def _summary_append(text):
    s = os.environ.get("GITHUB_STEP_SUMMARY")
    if s:
        with open(s, "a") as fh:
            fh.write(text)


def read_cause(name):
    try:
        return open(cause_path(name)).read().strip() or "unclassified"
    except OSError:
        return "unclassified"  # the job failed before the check ran (setup, checkout): still red, still not guessed


def report(name):
    cause = read_cause(name)
    line = f"cause={cause} job={name}"
    print(f"::error title=failure-cause::{line}")
    s = os.environ.get("GITHUB_STEP_SUMMARY")
    if s:
        with open(s, "a") as fh:
            fh.write(
                f"### FAILED: `{name}`\n- failure-cause: **{cause}**\n"
                "- reclassify by labelling the PR `cause:real`, `cause:env` or `cause:flake`\n"
            )
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("name", choices=sorted(CHECKS))
    r.add_argument("--tree", default=".")
    r.add_argument("--timeout", type=int, default=None)
    r.add_argument("--skip-suite", action="store_true", help="run the check without its pytest step (a PR labelled `queue`)")
    r.add_argument(
        "--retry-flaky",
        action="store_true",
        help="re-run the failed tests ONCE and record the flakes (the nightly leg; PR-level runs never pass it)",
    )
    q = sub.add_parser("report")
    q.add_argument("name")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        if a.retry_flaky:
            info = {}
            rc = run_check(
                a.name,
                os.path.abspath(a.tree),
                a.timeout,
                retry_flaky=True,
                retry_info=info,
                **({"skip_suite": True} if a.skip_suite else {}),
            )
            record_nightly_retry(info)
            return rc
        if a.skip_suite:
            return run_check(a.name, os.path.abspath(a.tree), a.timeout, skip_suite=True)
        return run_check(a.name, os.path.abspath(a.tree), a.timeout)
    return report(a.name)


if __name__ == "__main__":
    sys.exit(main())
