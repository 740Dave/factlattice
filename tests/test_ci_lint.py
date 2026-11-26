"""The lint and complexity gate (tools/ci_lint.py, tools/ci_complexity_ratchet.py, pyproject.toml's [tool.ruff]): each part is shown
red on a planted case and green on the real tree, with the same command CI runs. Fixture trees are small, so they run with
--min-files 1; the real-tree tests do not.

ruff must be available at the pinned version (the locked venv has it; a bare interpreter falls back to `uvx`); if it is not, the
helper exits and these tests FAIL rather than skip: a gate test that did not run is not a pass.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, ".github", "workflows", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import ci_lint  # noqa: E402
import ci_ruff_support as rs  # noqa: E402
import ci_step  # noqa: E402
import merge_queue as mq  # noqa: E402

TOOLS = ("ci_lint.py", "ci_ruff_support.py", "ci_complexity_ratchet.py", "ci_complexity_baseline.json")
BASE_PYPROJECT = """[tool.ruff]
line-length = 140

[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP", "C90", "PGH004"]
ignore = [%s]

[tool.ruff.lint.mccabe]
max-complexity = 15
"""


def complex_function(name, score):
    """A function whose McCabe score is exactly `score` (1 + one `if` per extra point)."""
    body = "".join(f"    if x == {i}:\n        return {i}\n" for i in range(score - 1))
    return f"def {name}(x):\n{body}    return -1\n"


def make_gate(path, ignore="", baseline=None):
    """A gate checkout: this repo's own tools, a pyproject with the given ignore list and a baseline ({} by default)."""
    os.makedirs(os.path.join(path, "tools"))
    for f in TOOLS:
        shutil.copy(os.path.join(ROOT, "tools", f), os.path.join(path, "tools", f))
    with open(os.path.join(path, "pyproject.toml"), "w") as fh:
        fh.write(BASE_PYPROJECT % ignore)
    with open(os.path.join(path, "tools", "ci_complexity_baseline.json"), "w") as fh:
        json.dump({"threshold": 15, "functions": baseline or {}}, fh)
    return str(path)


def make_tree(path, files):
    os.makedirs(path)
    for name, text in files.items():
        with open(os.path.join(path, name), "w") as fh:
            fh.write(text)
    return str(path)


def lint(gate, tree, part, **env):
    """Run the gate's own script on the tree, the way ci_step runs it: cwd = tree, script from the gate."""
    e = {**os.environ, **env}
    for name in (
        "pyproject.toml",
        os.path.join("tools", "ci_complexity_baseline.json"),
    ):  # a bare fixture tree carries the gate's own copies
        if not os.path.exists(os.path.join(tree, name)):
            os.makedirs(os.path.dirname(os.path.join(tree, name)), exist_ok=True)
            shutil.copy(os.path.join(gate, name), os.path.join(tree, name))
    return subprocess.run(
        [sys.executable, os.path.join(gate, "tools", "ci_lint.py"), part, "--min-files", "1"],
        cwd=tree,
        capture_output=True,
        text=True,
        env=e,
    )


# ---- (a), (b): the complexity ratchet -------------------------------------------------------------------------------------------------


def test_a_new_function_scoring_16_is_red_and_15_is_green(tmp_path):
    gate = make_gate(tmp_path / "gate")
    ok = lint(gate, make_tree(tmp_path / "t15", {"m.py": complex_function("f", 15)}), "ratchet")
    assert ok.returncode == 0, ok.stderr
    red = lint(gate, make_tree(tmp_path / "t16", {"m.py": complex_function("f", 16)}), "ratchet")
    assert red.returncode == 1
    assert "NEW function over 15: m.py::f = 16" in red.stderr


def test_a_baselined_function_that_gets_more_complex_is_red_and_a_simpler_one_is_green(tmp_path):
    gate = make_gate(tmp_path / "gate", baseline={"m.py::f": 18})
    same = lint(gate, make_tree(tmp_path / "t18", {"m.py": complex_function("f", 18)}), "ratchet")
    assert same.returncode == 0, same.stderr
    less = lint(gate, make_tree(tmp_path / "t17", {"m.py": complex_function("f", 17)}), "ratchet")
    assert less.returncode == 0, less.stderr
    more = lint(gate, make_tree(tmp_path / "t19", {"m.py": complex_function("f", 19)}), "ratchet")
    assert more.returncode == 1
    assert "INCREASED: m.py::f 18 -> 19" in more.stderr


def test_a_noqa_comment_cannot_hide_a_complex_function_from_the_ratchet(tmp_path):
    gate = make_gate(tmp_path / "gate")
    src = complex_function("f", 16).replace("def f(x):", "def f(x):  # noqa: C901")
    red = lint(gate, make_tree(tmp_path / "t", {"m.py": src}), "ratchet")
    assert red.returncode == 1 and "NEW function over 15: m.py::f" in red.stderr


# ---- (c): a hard-gated code ----------------------------------------------------------------------------------------------------------


def test_a_new_violation_of_a_code_with_no_findings_is_red(tmp_path):
    gate = make_gate(tmp_path / "gate")
    unsorted_imports = "import sys\nimport os\n\nprint(os, sys)\n"
    red = lint(gate, make_tree(tmp_path / "bad", {"m.py": unsorted_imports}), "ruff")
    assert red.returncode == 1 and "I001" in red.stdout
    clean = lint(gate, make_tree(tmp_path / "good", {"m.py": "import os\nimport sys\n\nprint(os, sys)\n"}), "ruff")
    assert clean.returncode == 0, clean.stdout


def test_c901_is_left_to_the_ratchet_and_the_plain_ruff_run_does_not_report_it(tmp_path):
    gate = make_gate(tmp_path / "gate")
    res = lint(gate, make_tree(tmp_path / "t", {"m.py": complex_function("f", 30)}), "ruff")
    assert res.returncode == 0 and "is too complex" not in res.stdout


def test_an_ignored_code_is_not_reported_by_the_ruff_run(tmp_path):
    src = "import sys\nimport os\n\nprint(os, sys)\n"
    strict = make_gate(tmp_path / "strict")
    loose = make_gate(tmp_path / "loose", ignore='"I001"')
    assert lint(strict, make_tree(tmp_path / "t1", {"m.py": src}), "ruff").returncode == 1
    assert lint(loose, make_tree(tmp_path / "t2", {"m.py": src}), "ruff").returncode == 0


# ---- (d): the ignore list can only shrink --------------------------------------------------------------------------------------------


def test_an_ignored_code_with_no_findings_left_is_red_and_one_with_findings_is_green(tmp_path):
    tree = make_tree(tmp_path / "t", {"m.py": "import sys\nimport os\n\nprint(os, sys)\n"})  # has I001 and no E711
    gate = make_gate(tmp_path / "g", ignore='"I001", "E711"')
    red = lint(gate, tree, "ignore-list")
    assert red.returncode == 1
    assert "no findings left for E711" in red.stderr and "I001" not in red.stderr.split("no findings left for")[1]
    green = lint(make_gate(tmp_path / "g2", ignore='"I001"'), tree, "ignore-list")
    assert green.returncode == 0, green.stderr


def test_the_real_ignore_list_holds_only_codes_that_still_have_findings():
    """THE PRUNING TEST: when an ignored code's last finding is fixed, this fails until the code is removed from the ignore list."""
    counts = ci_lint.count_ignored(Path(ROOT), Path(ROOT))
    assert counts, "the ignore list is empty or unreadable: update this test only if the list was emptied on purpose"
    stale = sorted(c for c, n in counts.items() if n == 0)
    assert not stale, f"remove {stale} from [tool.ruff.lint] ignore in pyproject.toml: they have no findings left and are now gated hard"


def test_the_ignore_list_never_holds_c901_which_belongs_to_the_ratchet():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        cfg = tomllib.load(fh)["tool"]["ruff"]
    assert "C901" not in cfg["lint"]["ignore"]
    assert cfg["lint"]["select"] == ["E", "F", "W", "I", "B", "UP", "C90", "PGH004"] and cfg["line-length"] == 140
    assert cfg["lint"]["mccabe"]["max-complexity"] == 15


# ---- the real tree -------------------------------------------------------------------------------------------------------------------


def test_the_real_tree_is_green_on_ruff_the_ignore_list_and_the_ratchet():
    """The exact command ci_step runs, minus the format part (that needs a base ref): from the repo root, the gate being the repo itself."""
    for part in ("ruff", "ignore-list", "ratchet"):
        res = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "ci_lint.py"), part], cwd=ROOT, capture_output=True, text=True)
        assert res.returncode == 0, (part, res.stdout[-2000:], res.stderr[-2000:])


def test_the_real_baseline_is_the_65_functions_over_15_and_every_entry_is_real():
    with open(os.path.join(ROOT, "tools", "ci_complexity_baseline.json")) as fh:
        data = json.load(fh)
    assert data["threshold"] == 15 and all(n > 15 for n in data["functions"].values())
    assert all(os.path.isfile(os.path.join(ROOT, k.split("::")[0])) for k in data["functions"]), "an entry names a file that is gone"


def test_the_pinned_ruff_version_is_the_same_in_the_tool_the_group_and_the_lock():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        dev = tomllib.load(fh)["dependency-groups"]["dev"]
    assert dev == [f"ruff=={rs.RUFF_VERSION}"]
    with open(os.path.join(ROOT, "uv.lock"), encoding="utf-8") as fh:
        lock = fh.read()
    assert re.search(rf'name = "ruff"\nversion = "{re.escape(rs.RUFF_VERSION)}"', lock)


def test_a_ruff_of_another_version_is_refused(monkeypatch):
    monkeypatch.setattr(rs, "RUFF_VERSION", "0.0.1")
    with pytest.raises(SystemExit) as e:
        rs.ruff_cmd()
    assert "0.0.1 is not available" in str(e.value)


# ---- the format part: added files only, and a missing base is red --------------------------------------------------------------------


def git(tree, *args):
    return subprocess.run(
        ["git", "-C", tree, "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True, text=True
    )


def test_format_checks_added_files_only_and_a_missing_base_is_red(tmp_path):
    gate = make_gate(tmp_path / "gate")
    tree = make_tree(tmp_path / "t", {"old.py": "x  =  1\n"})  # not formatted, but it is not added by the change
    git(tree, "init", "-q", "-b", "main")
    git(tree, "add", ".")
    git(tree, "commit", "-q", "-m", "base")
    with open(os.path.join(tree, "new.py"), "w") as fh:
        fh.write("y  =  2\n")
    git(tree, "add", ".")
    git(tree, "commit", "-q", "-m", "change")
    red = lint(gate, tree, "format", BASE_REF="HEAD~1")
    assert red.returncode == 1 and "new.py" in red.stdout + red.stderr and "old.py" not in red.stdout + red.stderr
    with open(os.path.join(tree, "new.py"), "w") as fh:
        fh.write("y = 2\n")
    git(tree, "commit", "-q", "-a", "-m", "format")
    assert lint(gate, tree, "format", BASE_REF="HEAD~2").returncode == 0
    missing = lint(gate, tree, "format", BASE_REF="no-such-ref")
    assert missing.returncode == ci_lint.BASE_UNRESOLVED_RC and "does not resolve" in missing.stderr


# ---- the gate is main's copy: the change under test cannot loosen it -------------------------------------------------------------------


def test_a_change_that_edits_the_ignore_list_and_the_baseline_does_not_loosen_the_gates_run(tmp_path):
    """The tree carries the loosening (an ignore of I001, a baseline that admits the new function); the gate dir is stricter."""
    src = "import sys\nimport os\n\n" + complex_function("f", 16) + "\nprint(os, sys)\n"
    main_gate = make_gate(tmp_path / "main")
    pr_gate = make_gate(tmp_path / "pr", ignore='"I001"', baseline={"m.py::f": 16})
    tree = make_tree(tmp_path / "t", {"m.py": src})
    for part in ("ruff", "ratchet"):
        assert lint(pr_gate, tree, part).returncode == 0, part  # the PR's own copy of the gate passes its own change: feedback only
        assert lint(main_gate, tree, part).returncode == 1, part  # main's copy, run on the same tree, does not


def test_the_gate_step_runs_the_gate_dirs_copy_when_the_queue_gives_one_and_the_trees_own_otherwise(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setenv("CI_CAUSE_DIR", str(tmp_path))
    monkeypatch.setattr(ci_step, "run_argv", lambda name, argv, tree, timeout, env=None: seen.append(argv) or 0)
    ci_step.run_check("checks", "/the/tree", env={"UV_PROJECT_ENVIRONMENT": "/v"}, gate_dir="/the/driver")
    ci_step.run_check("checks", "/the/tree", env={"UV_PROJECT_ENVIRONMENT": "/v"})
    lint_argvs = [a for a in seen if any("ci_lint.py" in x for x in a)]
    assert [a[1] for a in lint_argvs] == ["/the/driver/tools/ci_lint.py", "/the/tree/tools/ci_lint.py"]
    assert all("{GATE}" not in x for a in seen for x in a)


def test_the_queue_passes_its_driver_checkout_as_the_gate(monkeypatch, tmp_path):
    calls = []

    class Done:
        returncode = 0

    monkeypatch.setattr(mq.subprocess, "run", lambda cmd, **kw: Done())
    monkeypatch.setattr(mq, "remove_worktree", lambda *a, **k: None)
    monkeypatch.setattr(
        ci_step, "run_check", lambda name, tree, timeout=None, env=None, gate_dir=None, **kw: calls.append((name, gate_dir)) or 0
    )
    mq.default_run_checks(str(tmp_path / "driver"), "main")(7, "a" * 40)
    assert calls and all(g == str(tmp_path / "driver") for _, g in calls), calls
    assert [n for n, _ in calls] == list(mq.REQUIRED)


# ---- the queue shrinks only: effective list = gate AND tree, baseline = lower of the two (F1) ----------------------------------------


def pr_tree(path, files, ignore="", baseline=None):
    """The merged tree of a change: its sources plus ITS pyproject.toml and baseline (the change's own, read by the gate as data)."""
    tree = make_tree(path, files)
    with open(os.path.join(tree, "pyproject.toml"), "w") as fh:
        fh.write(BASE_PYPROJECT % ignore)
    os.makedirs(os.path.join(tree, "tools"))
    with open(os.path.join(tree, "tools", "ci_complexity_baseline.json"), "w") as fh:
        json.dump({"threshold": 15, "functions": baseline or {}}, fh)
    return tree


UNSORTED = "import sys\nimport os\n\nprint(os, sys)\n"  # one I001 finding
GEN_FIXED = "def gen(xs):\n    yield from xs\n"
GEN_LOOP = "def gen(xs):\n    for x in xs:\n        yield x\n"  # one UP028 finding


def test_in_the_queue_a_change_that_fixes_the_last_finding_of_a_code_and_drops_it_from_its_list_is_green(tmp_path):
    main_gate = make_gate(tmp_path / "main", ignore='"I001", "UP028"')
    tree = pr_tree(tmp_path / "t", {"m.py": UNSORTED + GEN_FIXED}, ignore='"I001"')
    for part in ("ruff", "ignore-list"):
        res = lint(main_gate, tree, part)
        assert res.returncode == 0, (part, res.stdout, res.stderr)
    # the same fix, list NOT shrunk: red, with the old message (the pruning rule applies to the effective list)
    keep = pr_tree(tmp_path / "t2", {"m.py": UNSORTED + GEN_FIXED}, ignore='"I001", "UP028"')
    red = lint(main_gate, keep, "ignore-list")
    assert red.returncode == 1 and "no findings left for UP028" in red.stderr


def test_in_the_queue_a_code_added_to_the_changes_list_is_still_red(tmp_path):
    main_gate = make_gate(tmp_path / "main", ignore='"I001"')
    src = UNSORTED + "x = 1 \n"  # I001 (listed by both) and W291 (listed only by the change)
    tree = pr_tree(tmp_path / "t", {"m.py": src}, ignore='"I001", "W291"')
    red = lint(main_gate, tree, "ruff")
    assert red.returncode == 1 and "W291" in red.stdout and "I001" not in red.stdout
    assert lint(main_gate, tree, "ignore-list").returncode == 0  # nothing on the effective list is stale


def test_in_the_queue_a_raised_baseline_score_is_still_red_and_a_lowered_one_is_green(tmp_path):
    main_gate = make_gate(tmp_path / "main", baseline={"m.py::f": 18})
    raised = pr_tree(tmp_path / "up", {"m.py": complex_function("f", 19)}, baseline={"m.py::f": 20})
    red = lint(main_gate, raised, "ratchet")
    assert red.returncode == 1 and "INCREASED: m.py::f 18 -> 19" in red.stderr  # the change's 20 does not count: the lower, main's 18, does
    lowered = pr_tree(tmp_path / "down", {"m.py": complex_function("f", 16)}, baseline={"m.py::f": 16})
    assert lint(main_gate, lowered, "ratchet").returncode == 0
    regrown = pr_tree(tmp_path / "regrown", {"m.py": complex_function("f", 17)}, baseline={"m.py::f": 16})
    red2 = lint(main_gate, regrown, "ratchet")
    assert red2.returncode == 1 and "INCREASED: m.py::f 16 -> 17" in red2.stderr  # the change's lower score binds


def test_in_the_queue_a_baseline_entry_missing_from_either_side_is_absent(tmp_path):
    main_gate = make_gate(tmp_path / "main", baseline={"m.py::f": 18})
    only_main = pr_tree(tmp_path / "a", {"m.py": complex_function("f", 17)})  # the change dropped the entry
    assert "NEW function over 15: m.py::f = 17" in lint(main_gate, only_main, "ratchet").stderr
    only_tree = pr_tree(
        tmp_path / "b", {"m.py": complex_function("g", 17)}, baseline={"m.py::g": 17}
    )  # the change added an entry main lacks
    red = lint(main_gate, only_tree, "ratchet")
    assert red.returncode == 1 and "NEW function over 15: m.py::g = 17" in red.stderr


def test_a_tree_with_no_readable_pyproject_or_baseline_ignores_and_budgets_nothing(tmp_path):
    main_gate = make_gate(tmp_path / "main", ignore='"I001"', baseline={"m.py::f": 18})
    tree = make_tree(tmp_path / "t", {"m.py": UNSORTED + complex_function("f", 17)})
    for name, text in (("pyproject.toml", "not = [valid"), (os.path.join("tools", "ci_complexity_baseline.json"), "{broken")):
        os.makedirs(os.path.dirname(os.path.join(tree, name)), exist_ok=True)
        with open(os.path.join(tree, name), "w") as fh:
            fh.write(text)
    assert lint(main_gate, tree, "ruff").returncode == 1
    assert lint(main_gate, tree, "ratchet").returncode == 1


# ---- an ignore file cannot hide a file from the gate (F2) ----------------------------------------------------------------------------


@pytest.mark.parametrize("ignore_file", [".gitignore", ".ignore"])
def test_an_ignore_file_entry_does_not_hide_a_file_from_ruff_or_the_ratchet(tmp_path, ignore_file):
    gate = make_gate(tmp_path / "gate")
    tree = make_tree(tmp_path / "t", {"m.py": UNSORTED + complex_function("f", 17), "ok.py": "x = 1\n"})
    git(tree, "init", "-q", "-b", "main")
    git(tree, "add", ".")  # m.py is tracked source; the change then adds an ignore entry that matches it
    with open(os.path.join(tree, ignore_file), "w") as fh:
        fh.write("m.py\n")
    assert lint(gate, tree, "ruff").returncode == 1
    red = lint(gate, tree, "ratchet")
    assert red.returncode == 1 and "NEW function over 15: m.py::f = 17" in red.stderr


# ---- noqa cannot silence the gate (F3) ---------------------------------------------------------------------------------------------


def test_the_real_selection_has_pgh004_and_the_real_tree_has_no_blanket_or_file_level_noqa():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        assert "PGH004" in tomllib.load(fh)["tool"]["ruff"]["lint"]["select"]
    assert not rs.run_ruff_json(Path(ROOT), Path(ROOT), ["--select", "PGH004"])
    res = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "ci_lint.py"), "noqa-file"], cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


@pytest.mark.parametrize(
    "src",
    ["import os  # noqa\n", "# ruff: noqa\nimport os\n", "# flake8: noqa\nimport os\n"],
    ids=["line-blanket", "file-ruff-blanket", "file-flake8-blanket"],
)
def test_a_blanket_noqa_is_red_in_the_plain_ruff_step(tmp_path, src):
    gate = make_gate(tmp_path / "gate")
    red = lint(gate, make_tree(tmp_path / "t", {"m.py": src}), "ruff")
    assert red.returncode == 1 and "PGH004" in red.stdout


def test_a_file_level_noqa_with_codes_is_refused_by_the_noqa_file_step(tmp_path):
    gate = make_gate(tmp_path / "gate")
    src = "# ruff: noqa: F401\nimport os\n"
    tree = make_tree(tmp_path / "t", {"m.py": src})
    assert lint(gate, tree, "ruff").returncode == 0  # PGH004 does not see this form: the ruff step is green, which is why the step exists
    red = lint(gate, tree, "noqa-file")
    assert red.returncode == 1 and "m.py:1: file-level `# ruff: noqa` is not allowed" in red.stderr
    text_only = make_tree(tmp_path / "s", {"m.py": 'DOC = "# ruff: noqa: F401"\nprint(DOC)\n'})
    assert lint(gate, text_only, "noqa-file").returncode == 0  # a string is not a comment


# ---- a baseline entry with no function is stale (F4) -----------------------------------------------------------------------------------


def test_a_baseline_entry_with_no_matching_function_is_red_and_one_with_a_function_is_green(tmp_path):
    gate = make_gate(tmp_path / "gate", baseline={"m.py::f": 16})
    gone = make_tree(tmp_path / "gone", {"m.py": "def g(x):\n    return x\n"})
    red = lint(gate, gone, "ratchet")
    assert red.returncode == 1 and "STALE: m.py::f" in red.stderr and "remove it" in red.stderr
    improved = make_tree(tmp_path / "improved", {"m.py": complex_function("f", 5)})  # still there, simpler: not stale
    assert lint(gate, improved, "ratchet").returncode == 0


# ---- the file set comes from git, not from ruff's directory walk and its default excludes (D1) ------------------------------------------


def tracked_tree(path, files, force=()):
    """A git repository holding FILES (relative path -> text), all added; names in FORCE are added with -f. .gitignore lists venv/."""
    tree = make_tree(path, {})
    with open(os.path.join(tree, ".gitignore"), "w") as fh:
        fh.write("venv/\n")
    for rel, text in files.items():
        os.makedirs(os.path.dirname(os.path.join(tree, rel)) or tree, exist_ok=True)
        with open(os.path.join(tree, rel), "w") as fh:
            fh.write(text)
    git(tree, "init", "-q", "-b", "main")
    git(tree, "add", ".")
    for rel in force:
        git(tree, "add", "-f", rel)
    return tree


@pytest.mark.parametrize(
    "where", ["pkg/_build/x.py", "pkg/build/x.py", "pkg/dist/x.py", "pkg/node_modules/x.py", "venv/x.py", ".venv/x.py"]
)
def test_a_tracked_file_under_a_ruff_default_excluded_name_is_still_judged(tmp_path, where):
    gate = make_gate(tmp_path / "gate")
    tree = tracked_tree(tmp_path / "t", {where: UNSORTED + complex_function("f", 17), "ok.py": "x = 1\n"}, force=[where])
    assert where in rs.files_in_scope(Path(tree), Path(gate))
    red = lint(gate, tree, "ruff")
    assert red.returncode == 1 and "I001" in red.stdout and where in red.stdout, red.stdout
    ratchet = lint(gate, tree, "ratchet")
    assert ratchet.returncode == 1 and f"NEW function over 15: {where}::f = 17" in ratchet.stderr
    noqa = tracked_tree(tmp_path / "n", {where: "# ruff: noqa: F401\nimport os\n"}, force=[where])
    assert lint(gate, noqa, "noqa-file").returncode == 1


def test_the_scope_is_git_listed_files_minus_the_gates_own_excludes_and_nothing_else(tmp_path):
    gate = make_gate(tmp_path / "gate")
    with open(os.path.join(gate, "pyproject.toml"), "a") as fh:
        fh.write('\n[tool.ruff]\nextend-exclude = ["case-studies", "tests/skip_me.py"]\n')
    # the fixture pyproject has its own [tool.ruff] table: put the exclude into it instead of a duplicate table
    text = open(os.path.join(gate, "pyproject.toml")).read().replace("\n[tool.ruff]\nextend-exclude", "\nextend-exclude", 1)
    text = text.replace("line-length = 140\n", 'line-length = 140\nextend-exclude = ["case-studies", "tests/skip_me.py"]\n', 1).replace(
        '\nextend-exclude = ["case-studies", "tests/skip_me.py"]\n\n[tool.ruff.lint.mccabe]', "\n[tool.ruff.lint.mccabe]"
    )
    with open(os.path.join(gate, "pyproject.toml"), "w") as fh:
        fh.write(text)
    files = {
        "a.py": "x = 1\n",
        "case-studies/c.py": "x = 1\n",
        "tests/skip_me.py": "x = 1\n",
        "tests/keep.py": "x = 1\n",
        "n.txt": "not python\n",
    }
    tree = tracked_tree(tmp_path / "t", files)
    with open(os.path.join(tree, "untracked.py"), "w") as fh:  # untracked and not ignored: judged too
        fh.write("y = 2\n")
    os.makedirs(os.path.join(tree, "venv"))
    with open(os.path.join(tree, "venv", "u.py"), "w") as fh:  # untracked AND git-ignored: not part of the change
        fh.write("y = 2\n")
    assert rs.files_in_scope(Path(tree), Path(gate)) == ["a.py", "tests/keep.py", "untracked.py"]


def test_the_scope_of_a_tree_that_is_not_a_repository_is_every_py_file_under_it(tmp_path):
    gate = make_gate(tmp_path / "gate")
    tree = make_tree(tmp_path / "t", {"a.py": "x = 1\n"})
    os.makedirs(os.path.join(tree, "_build"))
    with open(os.path.join(tree, "_build", "b.py"), "w") as fh:
        fh.write("x = 1\n")
    assert rs.files_in_scope(Path(tree), Path(gate)) == ["_build/b.py", "a.py"]
