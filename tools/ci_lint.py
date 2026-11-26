#!/usr/bin/env python3
"""The lint and complexity gate (a required step of the `checks` job; rule in SDLC.md 7.6). One entry point for CI and for a local run.

    python tools/ci_lint.py all          # every part below; runs them all, exits 1 if any is red
    python tools/ci_lint.py ruff         # ruff check with the gate's selection: every selected code that has no findings is gated hard
    python tools/ci_lint.py noqa-file    # red on any file-level `# ruff: noqa` / `# flake8: noqa` (PGH004 covers the blanket forms)
    python tools/ci_lint.py ignore-list  # red if the ignore list holds a code that now has no findings (the list can only shrink)
    python tools/ci_lint.py ratchet      # complexity ratchet (tools/ci_complexity_ratchet.py): C901 only
    python tools/ci_lint.py format       # `ruff format --check` on the .py files the change ADDS (not the 300-odd existing files)

    --tree DIR   the tree under test (default: the current directory)
    --gate DIR   the checkout that holds the gate: pyproject.toml (selection, ignore list, line length, max-complexity), the baseline
                 tools/ci_complexity_baseline.json and this script. Default: the checkout this script lives in.

GATE vs TREE. In the merge queue the script, the pyproject.toml and the baseline are the DEFAULT branch's (the queue's driver checkout) and
the tree is the merged result, so a change cannot loosen the gate that judges it: ci_step.py runs `{GATE}/tools/ci_lint.py` from the driver.
In ci.yml (author feedback only) there is no separate gate, so GATE is TREE and the change's own copy runs. ruff reads the GATE's
pyproject.toml through --config, never the tree's, and is run with the tree as its working directory.

EFFECTIVE LISTS. When the gate is a different checkout from the tree (the merge queue), the ignore list the run judges by is the
INTERSECTION of the gate's list and the tree's (a code stays ignored only if both ignore it), and each baseline entry is the LOWER of
the gate's and the tree's score (an entry missing from either side is absent). The tree's pyproject.toml and baseline are read as data,
never as ruff configuration. So a change can only tighten the gate, and a change that does (fixes the last finding of a code and drops
it from its list) is green. When the gate is the tree, both are the gate's own. A baseline entry with no matching function fails: the
baseline only shrinks. Ruff's ignore files are not honoured.

What is gated: pyproject.toml selects E, F, W, I, B, UP, C90 and PGH004 (a blanket `# noqa` or `# ruff: noqa`); the `noqa-file`
step also refuses every file-level `# ruff: noqa` with codes. The files judged are the ones git lists for the tree (tracked, plus
untracked and not ignored) minus the gate's own excludes, passed to ruff as explicit paths: ruff's default excludes (_build, venv,
...) and ignore files never hide a file. Codes that still have findings are on the ignore list there, each with its count; every
other selected code is gated with no baseline. C901 is excluded from the plain ruff run (--extend-ignore C901) and is held by the
ratchet instead. noqa comments are honoured by the plain run and ignored by the ratchet (a `# noqa: C901` cannot hide a function).
The format check needs BASE_REF (default origin/main) to resolve: a missing base is a red step, never "no files in scope".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tokenize
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ci_complexity_ratchet  # noqa: E402
import ci_ruff_support as rs  # noqa: E402

GATE_DEFAULT = Path(__file__).resolve().parent.parent
BASE_UNRESOLVED_RC = 3


def config_args(gate: Path) -> list[str]:
    return ["--config", str(gate / "pyproject.toml")]


def _ruff_lint_table(root: Path) -> dict:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return data.get("tool", {}).get("ruff", {}).get("lint", {})


def ignore_list(gate: Path) -> list[str]:
    """The codes in [tool.ruff.lint] ignore of the GATE's pyproject.toml."""
    return list(_ruff_lint_table(gate).get("ignore", []))


def select_list(gate: Path) -> list[str]:
    """The rule selection in the GATE's pyproject.toml."""
    return list(_ruff_lint_table(gate).get("select", []))


def effective_ignore(tree: Path, gate: Path) -> list[str]:
    """The ignore list the run judges by. When the gate is the tree (a local run, ci.yml) it is the gate's list. In the merge
    queue (the gate is the default branch's checkout, the tree the merged result) a code stays ignored only if BOTH lists ignore
    it: a change can shrink the list (and pass) and can never lengthen it. The tree's pyproject.toml is read as data, never loaded
    as ruff configuration. A missing or unreadable one ignores nothing, so every finding is then gated hard and the run is red.
    """
    mine = ignore_list(gate)
    if tree.resolve() == gate.resolve():
        return mine
    try:
        theirs = set(ignore_list(tree))
    except (OSError, ValueError):
        print("ci_lint: the tree's pyproject.toml is missing or unreadable: it ignores no code", file=sys.stderr)
        theirs = set()
    return [c for c in mine if c in theirs]


def step_ruff(tree: Path, gate: Path, min_files: int = rs.MIN_FILES_DEFAULT) -> int:
    n = rs.require_scanned_files(tree, gate, min_files)
    eff = effective_ignore(tree, gate)
    print(f"ci_lint ruff: {n} files in scope; ignore list {len(eff)} code(s) (gate {len(ignore_list(gate))}); C901 is held by the ratchet")
    # a later --select resets the config file's rule set, so the gate's selection and the effective ignore list are both given explicitly
    files = rs.files_in_scope(tree, gate)
    rules = ["--select", ",".join(select_list(gate)), *(["--ignore", ",".join(eff)] if eff else [])]
    proc = subprocess.run(
        rs.ruff_cmd() + ["check", "--no-cache", "--no-respect-gitignore", *config_args(gate), *rules, "--extend-ignore", "C901", *files],
        cwd=tree,
    )
    if proc.returncode not in (0, 1):
        print(f"ci_lint ruff: FAIL: ruff itself failed (exit {proc.returncode})", file=sys.stderr)
        return 2
    return proc.returncode


def count_ignored(tree: Path, gate: Path) -> dict[str, int]:
    """{code: findings} for every code on the EFFECTIVE ignore list (see effective_ignore), with the list itself switched off."""
    codes = effective_ignore(tree, gate)
    if not codes:
        return {}
    findings = rs.run_ruff_json(tree, gate, [*config_args(gate), "--config", "lint.ignore = []", "--select", ",".join(codes)])
    counts = dict.fromkeys(codes, 0)
    for f in findings:
        counts[f["code"]] = counts.get(f["code"], 0) + 1
    return counts


def step_ignore_list(tree: Path, gate: Path, min_files: int = rs.MIN_FILES_DEFAULT) -> int:
    rs.require_scanned_files(tree, gate, min_files)
    counts = count_ignored(tree, gate)
    stale = sorted(c for c, n in counts.items() if n == 0)
    print("ci_lint ignore-list: " + ", ".join(f"{c}={n}" for c, n in counts.items()))
    if stale:
        print("ci_lint ignore-list: FAIL: no findings left for " + ", ".join(stale), file=sys.stderr)
        print("  remove these codes from [tool.ruff.lint] ignore in pyproject.toml: from now on they are gated hard.", file=sys.stderr)
        return 1
    return 0


FILE_NOQA = re.compile(r"^#\s*(ruff|flake8)\s*:\s*noqa", re.IGNORECASE)


def file_level_noqa(path: Path) -> list[int]:
    """Line numbers of standalone `# ruff: noqa` / `# flake8: noqa` comments (with or without codes) in one file; comment tokens only, so a
    string that merely contains the text is not one."""
    hits = []
    with open(path, "rb") as fh:
        try:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type == tokenize.COMMENT and FILE_NOQA.match(tok.string) and not tok.line[: tok.start[1]].strip():
                    hits.append(tok.start[0])
        except (tokenize.TokenError, SyntaxError, IndentationError):
            pass  # a file that does not tokenize is a syntax error ruff reports (E902) in the plain run
    return hits


def step_noqa_file(tree: Path, gate: Path, min_files: int = rs.MIN_FILES_DEFAULT) -> int:
    """A file-level `# ruff: noqa` switches rules off for the whole file, outside every list and baseline. PGH004 (selected)
    refuses the blanket form; this refuses every form, coded ones included.
    """
    files = rs.files_in_scope(tree, gate)
    bad = [(f, n) for f in files for n in file_level_noqa(tree / f)]
    if bad:
        for f, n in bad:
            print(
                f"ci_lint noqa-file: FAIL: {f}:{n}: file-level `# ruff: noqa` is not allowed",
                file=sys.stderr,
            )
        return 1
    print(f"ci_lint noqa-file: {len(files)} files, no file-level noqa")
    return 0


def step_ratchet(tree: Path, gate: Path, min_files: int = rs.MIN_FILES_DEFAULT) -> int:
    return ci_complexity_ratchet.main(["check", "--root", str(tree), "--gate", str(gate), "--min-files", str(min_files)])


def added_py_files(tree: Path) -> list[str] | None:
    """The .py files the change adds relative to BASE_REF (default origin/main); None when the base cannot be resolved."""
    ref = os.environ.get("BASE_REF") or "origin/main"
    if subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=tree, capture_output=True).returncode:
        return None
    mb = subprocess.run(["git", "merge-base", ref, "HEAD"], cwd=tree, capture_output=True, text=True)
    if mb.returncode:
        return None
    out = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=A", mb.stdout.strip(), "HEAD", "--", "*.py"],
        cwd=tree,
        capture_output=True,
        text=True,
        check=True,
    )
    return [f for f in out.stdout.splitlines() if f and (tree / f).is_file()]


def step_format(tree: Path, gate: Path, min_files: int = 0, files: list[str] | None = None) -> int:
    if files is None:
        files = added_py_files(tree)
        if files is None:
            print(
                f"ci_lint format: FAIL: base ref {os.environ.get('BASE_REF') or 'origin/main'!r} does not resolve (set BASE_REF; fetch it)",
                file=sys.stderr,
            )
            return BASE_UNRESOLVED_RC
    scope = set(rs.files_in_scope(tree, gate))
    files = [f for f in files if f in scope]  # the gate's own file set (its excludes applied by us, not by ruff's defaults)
    if not files:
        print("ci_lint format: no .py files added by this change (nothing to check)")
        return 0
    print(f"ci_lint format: {len(files)} added file(s)")
    return subprocess.run(
        rs.ruff_cmd() + ["format", "--check", "--no-cache", "--no-respect-gitignore", *config_args(gate), *files],
        cwd=tree,
    ).returncode


STEPS = {"ruff": step_ruff, "noqa-file": step_noqa_file, "ignore-list": step_ignore_list, "ratchet": step_ratchet, "format": step_format}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("part", nargs="?", default="all", choices=["all", *STEPS])
    ap.add_argument("--tree", type=Path, default=Path.cwd())
    ap.add_argument("--gate", type=Path, default=GATE_DEFAULT)
    ap.add_argument(
        "--min-files",
        type=int,
        default=rs.MIN_FILES_DEFAULT,
        help="fail if ruff would scan fewer .py files than this (a fixture tree in a test lowers it)",
    )
    ap.add_argument("--json", action="store_true", help="with `all`: print {part: exit status} as the last line")
    a = ap.parse_args(argv)
    tree, gate = a.tree.resolve(), a.gate.resolve()
    if not (gate / "pyproject.toml").is_file() or not (gate / "tools" / "ci_complexity_baseline.json").is_file():
        print(
            f"ci_lint: FAIL: {gate} has no pyproject.toml or tools/ci_complexity_baseline.json: a missing gate is never a pass",
            file=sys.stderr,
        )
        return 2
    results = {}
    for name in STEPS if a.part == "all" else [a.part]:
        print(f"== ci_lint {name} (tree {tree}, gate {gate})", flush=True)
        results[name] = STEPS[name](tree, gate, a.min_files)
    red = {k: v for k, v in results.items() if v}
    if a.json:
        print(json.dumps(results))
    print("ci_lint: " + ("RED: " + ", ".join(f"{k} (exit {v})" for k, v in red.items()) if red else "green: " + ", ".join(results)))
    if a.part != "all":
        return results[a.part]  # one part: its own exit status (the format part's 3 = base ref unresolved)
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())
