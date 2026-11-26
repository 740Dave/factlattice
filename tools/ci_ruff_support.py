"""Shared helpers for tools/ci_lint.py and tools/ci_complexity_ratchet.py. Adapted from a sibling repository's ruff support module (the
baseline-key helpers it has for a per-finding baseline are not needed here: only C901 is baselined).

Stdlib only. Runs ruff at exactly RUFF_VERSION (also pinned in pyproject's dev group and so in uv.lock) and maps findings to stable keys.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

RUFF_VERSION = "0.17.5"  # the same pin as pyproject's dev group; the gate refuses to report on any other ruff


def _version_of(cmd: list[str]) -> str | None:
    probe = subprocess.run(cmd + ["--version"], capture_output=True, text=True)
    return probe.stdout.split()[-1] if probe.returncode == 0 and probe.stdout.split() else None


def ruff_cmd() -> list[str]:
    """`python -m ruff` in the current interpreter (the locked uv venv), else a ruff on PATH, else
    `uvx --from ruff==RUFF_VERSION` (an environment with no locked venv, such as a bare interpreter running the test suite).
    The version must equal RUFF_VERSION; otherwise a loud stop: a gate never reports a clean result from a ruff it did not pin."""
    candidates = [[sys.executable, "-m", "ruff"]]
    if shutil.which("ruff"):
        candidates.append([shutil.which("ruff")])
    if shutil.which("uvx"):
        candidates.append([shutil.which("uvx"), "--from", f"ruff=={RUFF_VERSION}", "ruff"])
    seen = []
    for cmd in candidates:
        v = _version_of(cmd)
        if v == RUFF_VERSION:
            return cmd
        seen.append(f"{' '.join(cmd)} -> {v}")
    sys.exit(
        f"ruff {RUFF_VERSION} is not available (run `uv sync --locked`); tried: {'; '.join(seen)}. "
        "Refusing to report a clean result without running it"
    )


MIN_FILES_DEFAULT = 200  # the real tree has ~350 .py files; far fewer means ruff scanned the wrong thing (a broad exclude, a wrong root)


def _excluded(rel: str, patterns: list[str]) -> bool:
    """Is the root-relative posix path REL named by one of the gate's exclude patterns (a file, a directory, or a glob)?"""
    parts = rel.split("/")
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    for pat in patterns:
        pat = pat.strip().rstrip("/").lstrip("./")
        if pat and any(pre == pat or fnmatch.fnmatch(pre, pat) or fnmatch.fnmatch(pre.rsplit("/", 1)[-1], pat) for pre in prefixes):
            return True
    return False


def gate_excludes(gate: Path) -> list[str]:
    """The gate's own `exclude` and `extend-exclude` from its pyproject.toml. Ruff's built-in default excludes (_build, build, dist,
    venv, .venv, node_modules, ...) are NOT applied: they hide tracked files by directory name."""
    ruff = tomllib.loads((gate / "pyproject.toml").read_text(encoding="utf-8")).get("tool", {}).get("ruff", {})
    return [*ruff.get("exclude", []), *ruff.get("extend-exclude", [])]


def files_in_scope(root: Path, gate: Path) -> list[str]:
    """The .py files the gate judges, root-relative and sorted: what git lists for ROOT (tracked, plus untracked files that are not
    git-ignored; a tracked file under an ignored or default-excluded name such as venv/ or _build/ is still listed) minus the gate's
    own excludes. Outside a git repository every .py file under ROOT. The set is handed to ruff as explicit paths, so no ruff
    default can drop a file from it."""
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.py"], cwd=root, capture_output=True, text=True
    )
    if proc.returncode == 0:
        names = [n for n in proc.stdout.split("\0") if n]
    else:  # not a git repository (a fixture tree in a test): every .py file
        names = [
            os.path.relpath(os.path.join(d, f), root)
            for d, ds, fs in os.walk(root)
            if ".git" not in d.split(os.sep)
            for f in fs
            if f.endswith(".py")
        ]
    pats = gate_excludes(gate)
    return sorted({Path(n).as_posix() for n in names if (root / n).is_file() and not _excluded(Path(n).as_posix(), pats)})


def require_scanned_files(root: Path, gate: Path, min_files: int) -> int:
    """Fail loud unless at least MIN_FILES files are in scope in ROOT (a gate that sees zero files is green for the wrong reason).
    -> the count."""
    n = len(files_in_scope(root, gate))
    if n == 0 or n < min_files:
        sys.exit(f"FAIL: only {n} file(s) in scope under {root} (minimum {min_files}): refusing to report a gate result on that")
    return n


def run_ruff_json(root: Path, gate: Path, extra_args: list[str]) -> list[dict]:
    """Run `ruff check` in ROOT on files_in_scope(ROOT, GATE) and return findings with `filename` made root-relative (posix). Fails
    loud on a ruff error."""
    cmd = ruff_cmd() + [
        "check",
        "--output-format",
        "json",
        "--no-cache",
        "--no-respect-gitignore",
        "--exit-zero",
        *extra_args,
        *files_in_scope(root, gate),
    ]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:  # --exit-zero: anything non-zero is ruff itself failing (bad config, crash)
        sys.exit(f"ruff failed (exit {proc.returncode}):\n{proc.stderr[-2000:]}")
    try:
        findings = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        sys.exit(f"ruff produced unparseable JSON ({exc}):\n{proc.stdout[:500]}")
    real_root = os.path.realpath(root)
    for f in findings:
        f["filename"] = Path(os.path.relpath(os.path.realpath(f["filename"]), real_root)).as_posix()
    return findings


def _scopes(tree: ast.AST) -> list[tuple[int, int, str, int]]:
    """(start, end, qualname, def_lineno) for every function/class, in source order."""
    out: list[tuple[int, int, str, int]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qn = f"{prefix}{child.name}"
                out.append((child.lineno, child.end_lineno or child.lineno, qn, child.lineno))
                walk(child, qn + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


class FileInfo:
    """Source lines plus a line -> innermost enclosing qualified scope map for one file."""

    def __init__(self, path: Path):
        text = path.read_text(encoding="utf-8", errors="replace")
        self.lines = text.splitlines()
        try:
            self.scopes = _scopes(ast.parse(text))
        except SyntaxError:
            self.scopes = []

    def _build(self) -> None:
        self._line_scope: dict[int, str] = {}
        # outer scopes first, so inner (smaller) scopes overwrite them: the innermost wins
        for start, end, qn, _ in sorted(self.scopes, key=lambda s: s[0] - s[1]):
            for row in range(start, end + 1):
                self._line_scope[row] = qn
        self._defs = {lineno: qn for _, _, qn, lineno in self.scopes}

    def scope_at(self, row: int) -> str:
        if not hasattr(self, "_defs"):
            self._build()
        return self._line_scope.get(row, "<module>")

    def def_at(self, row: int) -> str | None:
        """Qualified name of the function/class whose `def`/`class` line is ROW, else None."""
        if not hasattr(self, "_defs"):
            self._build()
        return self._defs.get(row)

    def line_text(self, row: int) -> str:
        return re.sub(r"\s+", " ", self.lines[row - 1]).strip() if 1 <= row <= len(self.lines) else ""


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def dump_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
