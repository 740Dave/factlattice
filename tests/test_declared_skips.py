"""The declared-skip pin of a merge-queue cell (ci_step.enforce_declared_skips; SDLC.md 7.6 CI-QUEUE-DECLARED-SKIPS).

Under the cell env a silent skip is a coverage loss nobody sees (44 release-gate tests did exactly that when the cell's uv cache was empty). So `checks` fails on any skip that
`.github/workflows/declared-skips.json` does not declare, with its reason. Pinned here: what counts as declared, the real list's shape (every entry has a reason and a `why`;
test_release_prep.py has NO entry, so it must pass with 0 skipped), and the pin turning RED on an added undeclared skip: through the comparer, through the check runner, and
through a real pytest session whose junit file is read the way the queue reads it.
"""

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, ".github", "workflows", "scripts"))
import ci_step  # noqa: E402

from tests._snapshot import is_published_snapshot, skip_reason

DECLARED = os.path.join(ROOT, ".github", "workflows", "declared-skips.json")


def entry(file, reason, mx=1, why="a reason"):
    return {"file": file, "reason": reason, "max": mx, "why": why}


def skip(file, reason, name="test_x"):
    return {"file": file, "name": name, "reason": reason}


def test_a_skip_is_declared_by_file_reason_and_count():
    d = [entry("tests/a.py", "needs a corpus", 2), entry("tests/b.py", r"snapshot .*/\.lts-engine/x", 1)]
    ok = [
        skip("tests/a.py", "needs a corpus on this machine"),
        skip("tests/a.py", "needs a corpus"),
        skip("tests/b.py", "snapshot " + "/".join(["", "Users", "x", ".lts-engine", "x"]) + " missing"),
    ]
    assert ci_step.undeclared_skips(ok, d) == []
    assert ci_step.undeclared_skips(ok + [skip("tests/a.py", "needs a corpus", "third")], d) == [
        skip("tests/a.py", "needs a corpus", "third")
    ]  # beyond the count
    assert ci_step.undeclared_skips([skip("tests/c.py", "needs a corpus")], d) == [
        skip("tests/c.py", "needs a corpus")
    ]  # a file no entry names
    assert ci_step.undeclared_skips([skip("tests/a.py", "something else")], d) == [
        skip("tests/a.py", "something else")
    ]  # a reason no entry matches


@pytest.mark.skipif(is_published_snapshot(), reason=skip_reason(".github/workflows/declared-skips.json"))
def test_the_real_list_is_well_formed_and_declares_no_release_prep_skip():
    declared = ci_step.load_declared(DECLARED)
    assert declared and all(e["why"].strip() and e["max"] >= 1 for e in declared)
    assert [
        e for e in declared if e["file"] == "tests/test_release_prep.py"
    ] == []  # the release gate runs, with 0 skipped, under the cell env
    homes = [e for e in declared if "~/.lts-engine" in e["why"]]
    assert sorted((e["file"], e["max"]) for e in homes) == [
        ("tests/test_find_cache_byte_ceiling.py", 3),
        ("tests/test_query_surface_parity.py", 7),
    ]  # the 10 HOME-relative skips
    assert all("HOME" in e["why"] for e in homes)
    assert ci_step.undeclared_skips(
        [skip("tests/test_release_prep.py", "uv could not make the fake project's lock file offline")], declared
    )  # a release-prep skip is undeclared


def test_a_malformed_entry_is_refused(tmp_path):
    p = tmp_path / "d.json"
    for bad in (
        [{"file": "t.py", "reason": "r", "max": 1}],
        [{"file": "t.py", "reason": "(", "max": 1, "why": "w"}],
        [{"file": "t.py", "reason": "r", "max": "1", "why": "w"}],
    ):
        p.write_text(json.dumps(bad))
        with pytest.raises((ValueError, Exception)):
            ci_step.load_declared(str(p))


def mini_session(tmp_path, extra_test=True):
    """A real pytest session with one declared-looking skip and (optionally) one ADDED undeclared skip; -> its junit path (xunit1, as the queue writes it)."""
    t = tmp_path / "mini"
    t.mkdir()
    (t / "test_decl.py").write_text("import pytest\ndef test_a():\n    pytest.skip('needs a corpus on this machine')\n")
    if extra_test:
        (t / "test_new.py").write_text("import pytest\ndef test_added():\n    pytest.skip('a brand new silent skip')\n")
        (t / "test_mod.py").write_text("import pytest\npytest.importorskip('no_such_module_for_the_pin')\n")
    junit = tmp_path / "j.xml"
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit1", f"--junitxml={junit}", "."],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(t),  # the rootdir: junit file attributes are relative to it, like `tests/test_x.py` in the real suite
    )
    assert r.returncode == 0, r.stdout
    return str(junit)


def test_read_skips_reads_test_level_and_collection_skips(tmp_path):
    skips = ci_step.read_skips(mini_session(tmp_path))
    assert sorted((s["file"], s["reason"][:30]) for s in skips if not s["file"].endswith("test_mod.py")) == [
        ("test_decl.py", "needs a corpus on this machine"),
        ("test_new.py", "a brand new silent skip"),
    ]
    assert any("no_such_module_for_the_pin" in s["reason"] for s in skips)  # a collection skip carries its reason in the element's text
    assert ci_step.read_skips(str(tmp_path / "missing.xml")) is None


def declared_file(tmp_path, entries):
    gate = tmp_path / "gate"
    (gate / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (gate / ".github" / "workflows" / "declared-skips.json").write_text(json.dumps(entries))
    return str(gate)


def test_the_pin_goes_red_on_an_added_undeclared_skip_and_green_without_it(tmp_path):
    """The mutation: the same session with one test added that skips. Declared: the one skip the base had."""
    gate = declared_file(tmp_path, [entry("test_decl.py", "needs a corpus", 1)])
    lines = []
    # base: only the declared skip
    (tmp_path / "base").mkdir()
    base_junit = mini_session(tmp_path / "base", extra_test=False)
    for s in ci_step.read_skips(base_junit):
        s["file"] = "test_decl.py"  # the mini session's relative path
    skips_base = [dict(s, file="test_decl.py") for s in ci_step.read_skips(base_junit)]
    assert ci_step.undeclared_skips(skips_base, ci_step.load_declared(os.path.join(gate, ci_step.DECLARED_SKIPS))) == []
    # mutant: a new skipping test and a new skipping module
    (tmp_path / "mut").mkdir()
    mut_junit = mini_session(tmp_path / "mut", extra_test=True)
    rc = ci_step.enforce_declared_skips("checks", mut_junit, gate, str(tmp_path), out=lines.append)
    assert rc == 1, lines
    joined = "\n".join(lines)
    assert "UNDECLARED SKIP" in joined and "a brand new silent skip" in joined and "no_such_module_for_the_pin" in joined, joined
    print("RED pin:", [ln for ln in lines if "UNDECLARED" in ln][0][:200])


def test_a_missing_or_malformed_list_or_junit_is_red_not_silently_off(tmp_path):
    junit = mini_session(tmp_path, extra_test=False)
    out = []
    assert (
        ci_step.enforce_declared_skips("checks", junit, str(tmp_path / "no-gate"), str(tmp_path), out=out.append) == 1
        and "cannot compare" in out[0]
    )
    gate = declared_file(tmp_path, [{"file": "x.py"}])
    assert ci_step.enforce_declared_skips("checks", junit, gate, str(tmp_path), out=out.append) == 1
    assert (
        ci_step.enforce_declared_skips(
            "checks", str(tmp_path / "nope.xml"), declared_file(tmp_path / "g2", []), str(tmp_path), out=out.append
        )
        == 1
    )


def test_the_check_runner_applies_the_pin_only_in_a_queue_cell_that_asks_for_it(tmp_path, monkeypatch):
    """run_check end to end with a fake suite step that writes a junit with an undeclared skip: red only when MQ_ENFORCE_DECLARED_SKIPS is set, and the cause is recorded."""
    junit_text = '<testsuite><testcase classname="t" name="test_new" file="tests/test_new.py"><skipped type="pytest.skip" message="a brand new silent skip">x</skipped></testcase></testsuite>'

    def fake_run_argv(name, argv, tree, timeout, env=None):
        path = next(a.split("=", 1)[1] for a in argv if a.startswith("--junitxml="))
        open(path, "w").write(junit_text)
        return 0

    monkeypatch.setattr(ci_step, "run_argv", fake_run_argv)
    monkeypatch.setattr(ci_step, "CHECKS", {"checks": [(["suite", "--junitxml={JUNIT}"], 10)]})
    monkeypatch.setenv("CI_CAUSE_DIR", str(tmp_path))
    monkeypatch.setenv("MQ_UV_SEED", str(tmp_path / "seed"))  # enforce applies to a seeded cell
    gate = declared_file(tmp_path, [entry("tests/other.py", "x", 1)])
    monkeypatch.delenv("MQ_ENFORCE_DECLARED_SKIPS", raising=False)
    assert (
        ci_step.run_check("checks", str(tmp_path), env={"PATH": "/usr/bin"}, gate_dir=gate) == 0
    )  # not a cell that asks: the suite's own result stands
    monkeypatch.setenv("MQ_ENFORCE_DECLARED_SKIPS", "enforce")
    assert ci_step.run_check("checks", str(tmp_path), env={"PATH": "/usr/bin"}, gate_dir=gate) == 1
    assert ci_step.read_cause("checks") == "unclassified"
    gate_ok = declared_file(tmp_path / "ok", [entry("tests/test_new.py", "silent skip", 1)])
    assert ci_step.run_check("checks", str(tmp_path), env={"PATH": "/usr/bin"}, gate_dir=gate_ok) == 0


# ---- report mode: the first deployment, before the list has been measured on the runner
def test_classify_names_what_kind_of_problem_each_skip_is():
    d = [entry("tests/a.py", "needs a corpus", 1), entry("tests/gone.py", "never seen", 1)]
    skips = [
        skip("tests/a.py", "needs a corpus"),
        skip("tests/a.py", "needs a corpus", "again"),
        skip("tests/a.py", "other reason", "r"),
        skip("tests/new.py", "x", "n"),
    ]
    problems, unseen = ci_step.classify_skips(skips, d)
    assert [(s["name"], k) for s, k in problems] == [("again", "over-count"), ("r", "reason-unmatched"), ("n", "file-not-declared")]
    assert unseen == [d[1]]  # a declaration nothing matched (a stale entry)


def test_report_mode_prints_the_full_comparison_as_warnings_and_never_returns_a_failure(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    junit = mini_session(tmp_path, extra_test=True)
    gate = declared_file(tmp_path, [entry("test_decl.py", "needs a corpus", 1), entry("test_stale.py", "never", 1)])
    lines = []
    assert ci_step.enforce_declared_skips("checks", junit, gate, str(tmp_path), out=lines.append, mode="report") == 0
    assert lines and all(ln.startswith("::warning title=declared-skips::") for ln in lines), lines
    joined = "\n".join(lines)
    assert "(report): 3 skipped, 1 declared, 2 undeclared, 1 declaration(s) nothing matched" in joined
    assert (
        "UNDECLARED SKIP (file-not-declared) test_new.py::test_added" in joined and "declared skip nothing matched: test_stale.py" in joined
    )
    text = summary.read_text()
    assert (
        "declared-skip pin (report): 3 skipped, 1 declared, 2 undeclared" in text and "UNDECLARED (file-not-declared)" in text
    )  # the one-line summary and the list
    print("REPORT:", lines[0][:200])
    # the same inputs in enforce mode fail
    assert ci_step.enforce_declared_skips("checks", junit, gate, str(tmp_path), out=[].append, mode="enforce") == 1


def test_report_mode_does_not_fail_on_an_unreadable_list_or_junit_either(tmp_path):
    junit = mini_session(tmp_path, extra_test=False)
    out = []
    assert (
        ci_step.enforce_declared_skips("checks", junit, str(tmp_path / "no-gate"), str(tmp_path), out=out.append, mode="report") == 0
        and "::warning" in out[0]
        and "cannot compare" in out[0]
    )
    assert (
        ci_step.enforce_declared_skips(
            "checks", str(tmp_path / "nope.xml"), declared_file(tmp_path / "g", []), str(tmp_path), out=out.append, mode="report"
        )
        == 0
    )
    assert (
        ci_step.enforce_declared_skips(
            "checks", str(tmp_path / "nope.xml"), declared_file(tmp_path / "g", []), str(tmp_path), out=out.append, mode="enforce"
        )
        == 1
    )


def test_report_mode_never_changes_the_check_runners_exit_code(tmp_path, monkeypatch, capsys):
    """run_check end to end: a fake suite with an undeclared skip. report -> 0 and no cause file; enforce -> 1; an unknown value -> report, with a warning; unset -> no check."""
    junit_text = '<testsuite><testcase classname="t" name="test_new" file="tests/test_new.py"><skipped type="pytest.skip" message="brand new">x</skipped></testcase></testsuite>'

    def fake_run_argv(name, argv, tree, timeout, env=None):
        open(next(a.split("=", 1)[1] for a in argv if a.startswith("--junitxml=")), "w").write(junit_text)
        return 0

    monkeypatch.setattr(ci_step, "run_argv", fake_run_argv)
    monkeypatch.setattr(ci_step, "CHECKS", {"checks": [(["suite", "--junitxml={JUNIT}"], 10)]})
    monkeypatch.setenv("CI_CAUSE_DIR", str(tmp_path))
    monkeypatch.setenv("MQ_UV_SEED", str(tmp_path / "seed"))  # enforce applies to a seeded cell
    gate = declared_file(tmp_path, [entry("tests/other.py", "x", 1)])
    results = {}
    for value in (None, "report", "enforce", "1"):
        monkeypatch.delenv("MQ_ENFORCE_DECLARED_SKIPS", raising=False)
        if value:
            monkeypatch.setenv("MQ_ENFORCE_DECLARED_SKIPS", value)
        (tmp_path / "cause-checks").unlink(missing_ok=True)
        capsys.readouterr()
        results[value] = (
            ci_step.run_check("checks", str(tmp_path), env={"PATH": "/usr/bin"}, gate_dir=gate),
            capsys.readouterr().out,
            (tmp_path / "cause-checks").read_text() if (tmp_path / "cause-checks").exists() else "",
        )
    assert results[None][0] == 0 and "declared-skip pin" not in results[None][1]  # no variable: no check
    assert (
        results["report"][0] == 0
        and "::warning title=declared-skips::[ci_step] checks: UNDECLARED SKIP (file-not-declared)" in results["report"][1]
        and results["report"][2] == ""
    )
    assert results["enforce"][0] == 1 and "UNDECLARED SKIP" in results["enforce"][1] and results["enforce"][2].strip() == "unclassified"
    assert (
        results["1"][0] == 0 and "is not `report` or `enforce`: reporting only" in results["1"][1]
    )  # an unknown value reports, it never fails a cell


def internal_queue_pin_setting():
    """-> (the driver step's env value for MQ_ENFORCE_DECLARED_SKIPS, every `run:` string of the workflow), parsed from the internal merge-queue.yml."""
    import yaml

    wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "merge-queue.yml")))
    driver = next(s for s in wf["jobs"]["test"]["steps"] if "merge_queue.py test" in str(s.get("run", "")))
    runs = [str(s.get("run", "")) for job in wf["jobs"].values() for s in job.get("steps", [])]
    return driver.get("env", {}).get("MQ_ENFORCE_DECLARED_SKIPS"), runs


@pytest.mark.skipif(is_published_snapshot(), reason=skip_reason("the internal .github/workflows/merge-queue.yml"))
def test_the_internal_queue_enforces_the_pin_unless_the_kill_switch_variable_says_report():
    """The exact expression: the repository variable MQ_SKIP_PIN_MODE, falling back to `enforce`, as an env: value of the driver step and NEVER inside run: text. The variable is the
    kill switch (`report` restores report mode with no PR: the gate's workflow reads it at run time); unset means enforce."""
    import re

    value, runs = internal_queue_pin_setting()
    assert value == "${{ vars.MQ_SKIP_PIN_MODE || 'enforce' }}", value
    assert not [r for r in runs if "MQ_ENFORCE_DECLARED_SKIPS" in r or "MQ_SKIP_PIN_MODE" in r]  # not in any run: text
    var_name, fallback = re.fullmatch(r"\$\{\{ vars\.(\w+) \|\| '(\w+)' \}\}", value).groups()
    assert (var_name, fallback) == ("MQ_SKIP_PIN_MODE", "enforce")
    for variable, expect in ((None, "enforce"), ("", "enforce"), ("report", "report"), ("enforce", "enforce")):
        resolved = variable or fallback  # GitHub's `a || b`: an unset or empty variable is falsy
        assert ci_step.skip_pin_mode(resolved) == (expect, True), variable
    assert ci_step.skip_pin_mode("report") == ("report", True) and ci_step.skip_pin_mode("enforce") == ("enforce", True)
    # an unknown value still reports, and says so (the ::error annotation and the summary line are tested below)
    assert ci_step.skip_pin_mode("Enforce") == ("report", False) and ci_step.skip_pin_mode("1") == ("report", False)


def test_the_public_queue_has_no_pin():
    import yaml

    # The trunk keeps the public variant under publish/; the release's folder map places it at .github/workflows/ in a snapshot. Read whichever exists (publish/ first: in the
    # trunk, .github/workflows/merge-queue.yml is the INTERNAL variant, which has the pin). Neither existing is a failure, never a skip.
    candidates = [os.path.join(ROOT, "publish", "github", "workflows", "merge-queue.yml"), os.path.join(ROOT, ".github", "workflows", "merge-queue.yml")]
    found = [c for c in candidates if os.path.isfile(c)]
    assert found, f"the public merge-queue.yml is in neither place: {candidates}"
    text = open(found[0]).read()
    assert "MQ_ENFORCE_DECLARED_SKIPS" not in text and "MQ_SKIP_PIN_MODE" not in text and "declared-skips" not in text
    assert yaml.safe_load(text)["jobs"]["test"]  # (the public set is unchanged by this switch)


# ---- the review round: report mode can never change a cell, whatever the list holds
BAD_LISTS = {
    "invalid-regex": json.dumps([{"file": "tests/test_a.py", "reason": "needs (a", "max": 1, "why": "w"}]),
    "top-level-int": "5",
    "top-level-null": "null",
    "top-level-dict": json.dumps({"file": "x"}),
    "entry-is-a-list": json.dumps([["tests/test_a.py", "r", 1, "w"]]),
    "max-is-a-string": json.dumps([{"file": "tests/test_a.py", "reason": "r", "max": "1", "why": "w"}]),
    "not-json": "[{",
}
ONE_SKIP = '<testsuites><testsuite><testcase classname="tests.test_a" name="test_x" file="tests/test_a.py" line="1"><skipped type="pytest.skip" message="needs a corpus">x</skipped></testcase></testsuite></testsuites>'


@pytest.mark.parametrize("shape", sorted(BAD_LISTS))
def test_report_mode_turns_every_bad_list_into_a_warning_naming_the_exception_and_never_fails(tmp_path, monkeypatch, shape):
    """The review's probe shapes. Before the fix these raised out of apply_skip_pin (a crashed cell, dequeued red); now report returns 0 with the class and the cause."""
    junit = tmp_path / "j.xml"
    junit.write_text(ONE_SKIP)
    gate = tmp_path / "gate"
    (gate / ".github" / "workflows").mkdir(parents=True)
    (gate / ".github" / "workflows" / "declared-skips.json").write_text(BAD_LISTS[shape])
    monkeypatch.setenv("CI_CAUSE_DIR", str(tmp_path))
    monkeypatch.setenv("MQ_UV_SEED", str(tmp_path / "seed"))
    monkeypatch.setenv("MQ_ENFORCE_DECLARED_SKIPS", "report")
    lines = []
    assert ci_step.enforce_declared_skips("checks", str(junit), str(gate), str(tmp_path), out=lines.append, mode="report") == 0
    assert len(lines) == 1 and lines[0].startswith("::warning title=declared-skips::") and "cannot compare" in lines[0], lines
    assert any(name in lines[0] for name in ("ValueError", "JSONDecodeError")), lines[0]  # the exception class is named
    assert ci_step.apply_skip_pin("checks", str(junit), str(gate), str(tmp_path)) == 0  # through the entry point the check runner uses
    monkeypatch.setenv("MQ_ENFORCE_DECLARED_SKIPS", "enforce")
    out = []
    assert (
        ci_step.enforce_declared_skips("checks", str(junit), str(gate), str(tmp_path), out=out.append, mode="enforce") == 1
        and "cannot compare" in out[0]
        and not out[0].startswith("::warning")
    )
    assert ci_step.apply_skip_pin("checks", str(junit), str(gate), str(tmp_path)) == 1
    print("REPORT:", lines[0][:170])


def test_report_mode_survives_an_unexpected_exception_from_the_comparison(tmp_path, monkeypatch):
    junit = tmp_path / "j.xml"
    junit.write_text(ONE_SKIP)
    monkeypatch.setattr(ci_step, "classify_skips", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    gate = declared_file(tmp_path, [entry("tests/test_a.py", "x", 1)])
    lines = []
    assert (
        ci_step.enforce_declared_skips("checks", str(junit), gate, str(tmp_path), out=lines.append, mode="report") == 0
        and "RuntimeError: boom" in lines[0]
    )


def test_enforce_applies_only_to_a_cell_that_ran_with_a_seed(tmp_path, monkeypatch, capsys):
    junit = tmp_path / "j.xml"
    junit.write_text(ONE_SKIP)
    gate = declared_file(tmp_path, [entry("tests/other.py", "x", 1)])  # the skip is undeclared
    monkeypatch.setenv("CI_CAUSE_DIR", str(tmp_path))
    monkeypatch.setenv("MQ_ENFORCE_DECLARED_SKIPS", "enforce")
    monkeypatch.delenv("MQ_UV_SEED", raising=False)
    assert ci_step.apply_skip_pin("checks", str(junit), gate, str(tmp_path)) == 0  # a hosted cell: no check
    out = capsys.readouterr().out
    assert "::notice title=declared-skips::" in out and "without a uv seed" in out
    monkeypatch.setenv("MQ_UV_SEED", str(tmp_path / "seed"))
    assert ci_step.apply_skip_pin("checks", str(junit), gate, str(tmp_path)) == 1  # a seeded cell: enforced
    monkeypatch.setenv("MQ_ENFORCE_DECLARED_SKIPS", "report")
    monkeypatch.delenv("MQ_UV_SEED")
    assert (
        ci_step.apply_skip_pin("checks", str(junit), gate, str(tmp_path)) == 0 and "undeclared" in capsys.readouterr().out
    )  # report mode still reports without a seed


@pytest.mark.parametrize("value", ["Enforce", "enforce ", "ENFORCE", "enforced", "1", "true"])
def test_an_unknown_mode_is_an_error_annotation_and_a_summary_line_and_never_fails_the_cell(tmp_path, monkeypatch, capsys, value):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    junit = tmp_path / "j.xml"
    junit.write_text(ONE_SKIP)
    gate = declared_file(tmp_path, [entry("tests/other.py", "x", 1)])
    monkeypatch.setenv("CI_CAUSE_DIR", str(tmp_path))
    monkeypatch.setenv("MQ_ENFORCE_DECLARED_SKIPS", value)
    assert ci_step.apply_skip_pin("checks", str(junit), gate, str(tmp_path)) == 0  # reported, not failed
    assert f"::error title=declared-skips::MQ_ENFORCE_DECLARED_SKIPS={value!r} is not `report` or `enforce`" in capsys.readouterr().out
    assert "ERROR: MQ_ENFORCE_DECLARED_SKIPS=" in summary.read_text()


@pytest.mark.skipif(is_published_snapshot(), reason=skip_reason("the internal .github/workflows/merge-queue.yml"))
def test_the_queue_workflows_fallback_is_one_of_the_two_modes():
    value, _ = internal_queue_pin_setting()
    fallback = value.split("||")[1].strip().strip("}").strip().strip("'")
    assert fallback in ci_step.SKIP_PIN_MODES, (
        fallback
    )  # a typo in the fallback (the value used when the kill switch is unset) is caught here, not silently reported
