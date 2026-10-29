"""The single-reader GUARD for the corpus registry ([REGISTRY-SINGLE-READER], [GUARD-RESOLVES-CONSTANTS]).

`factlattice/registry.py` is the ONLY code that may know the registry variables' names or open the
per-user pointer file. This module is the scanner; `tests/test_registry_lookup.py` runs it over the
real tree (must be clean) and over a planted tree (must FIRE: the negative control).

WHY IT SEARCHES NAMES, NOT `os.environ` CALLS. The first draft of this rule was "no module outside the
resolver calls `os.environ.get('FACTLATTICE_CORPUS_CONFIG')`". The audit measured that two of the three
real readers did not: they read a module CONSTANT (`CORPUS_CONFIG_ENV`, `ENV_CONFIG`), so a search for the
literal call would have passed over the very code it exists to find. So the rule is on the STRINGS: the
name of either variable appearing in ANY non-test `.py` outside the resolver (a constant, a docstring, an
error message) is a finding. A name in prose is cheap to reword; a name in a constant is a second reader
waiting to happen. The resolver exports the names (`registry.ENV_CONFIG`) for every legitimate user.

It also folds `"A" + "B"` constants (so splitting the name does not evade a text search) and flags the
tokens that open the pointer file (`XDG_CONFIG_HOME`, the `.config` directory, the pointer's file name).
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import List, Tuple

NAMES = ("FACTLATTICE_CORPUS_CONFIG", "FACTLATTICE_CORPUS_ROOT")
# What opening the pointer file needs: the XDG variable, or the default directory, or its file name pair.
POINTER_TOKENS = ("XDG_CONFIG_HOME", '".config"', "'.config'", "/.config/", "factlattice/config.json")
ALLOWED = ("factlattice/registry.py",)
SCAN_DIRS = ("factlattice", "factlattice_engine", "tools")


def _folded_strings(tree: ast.AST) -> List[str]:
    """Every string constant, with `"a" + "b"` chains folded."""
    out: List[str] = []

    def fold(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            return n.value
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            a, b = fold(n.left), fold(n.right)
            if a is not None and b is not None:
                return a + b
        return None

    for n in ast.walk(tree):
        v = fold(n)
        if v:
            out.append(v)
    return out


def scan_tree(root: Path, scan_dirs=SCAN_DIRS, allowed=ALLOWED) -> List[Tuple[str, str]]:
    """`[(relative path, what was found)]` for every non-test `.py` under `scan_dirs` outside `allowed`."""
    root = Path(root)
    found: List[Tuple[str, str]] = []
    for d in scan_dirs:
        for path in sorted((root / d).rglob("*.py")) if (root / d).is_dir() else []:
            rel = path.relative_to(root).as_posix()
            if rel in allowed or "/tests/" in "/" + rel or os.path.basename(rel).startswith("test_"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for name in NAMES:
                if name in text:
                    found.append((rel, name))
            for tok in POINTER_TOKENS:
                if tok in text:
                    found.append((rel, tok))
            try:
                folded = _folded_strings(ast.parse(text))
            except SyntaxError:
                continue
            for s in folded:
                for name in NAMES:
                    if name in s and name not in text:      # reachable ONLY by folding: a split name
                        found.append((rel, name + " (folded constant)"))
    return sorted(set(found))


# ====================================================================================== ROUND 3: the AST guard
# The name search above sees the NAMES. The Diamond (b475061) showed it misses every form that BUILDS the name or
# reaches the resolver's helpers: join, f-string, %-format, chr(), reversed strings, `os.environ.get(registry.ENV_CONFIG)`,
# `open(registry.pointer_path())`, an env scan with `.items()`, and any `.py` outside the three scanned directories.
#
# WHAT THIS CHECK DOES, over EVERY `.py` in the tree except tests, `private/`, build dirs and the resolver:
#   1. flags any reference to `ENV_CONFIG`, `ENV_ROOT`, `pointer_path`, `read_registry_file` (attribute, name or import);
#   2. flags any os.environ / os.getenv / environ read whose key does not evaluate to a plain string (a module-level
#      string constant in the same module counts as plain, and is then checked against rule 3);
#   3. flags any expression that EVALUATES (constant folding: +, %, f-string, "".join, chr, map(chr), [::-1],
#      os.path.join, str.format) to either registry variable's name or to "factlattice/config.json";
#   4. flags iteration / copy of the whole environment (`os.environ.items()/keys()/values()/copy()`, `dict(os.environ)`).
#
# KNOWN GAPS the delta Diamond (5fea5c4) showed, NOT closed here (filed as a follow-up ticket; words only):
# os.environ aliases (`e = os.environ; e.get(k)`), AugAssign-built constants (`K += "..."`), `{**os.environ}` and
# `os.environ | {...}` and tuple()/map() over the environment, os.environb / os.getenvb, Path-built pointers
# (`Path.home() / ".config" / "factlattice" / "config.json"`, `.joinpath`), the display helpers as pointer paths
# (`POINTER_DISPLAY` / `pointer_display()`), and the skipped directories and files (tests/, private/, build/, dist/,
# egg-info, `test_*.py` and `conftest.py` even INSIDE the packages, `.pyi`, extensionless scripts).
# WHAT IT CANNOT SEE (words only; stated, not hidden): a name assembled from data that is not constant in the module
# (read from a file, the network, another module's runtime value, `getattr` on a computed string, `eval`/`exec`,
# `importlib`), a read done by a subprocess or a shell script, a `.pth`/sitecustomize hook, and non-Python files.
# Those are held by review and by the single-reader row in SDLC 7.6, not by this test.
FORBIDDEN_ATTRS = ("ENV_CONFIG", "ENV_ROOT", "pointer_path", "read_registry_file")
TARGET_STRINGS = ("FACTLATTICE_CORPUS_CONFIG", "FACTLATTICE_CORPUS_ROOT", "factlattice/config.json")
SKIP_DIRS = {"tests", "private", ".git", "build", "dist", "__pycache__", "factlattice.egg-info"}
SKIP_FILES = {"conftest.py"}


def _consts(tree: ast.AST) -> dict:
    out = {}
    for n in getattr(tree, "body", []):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            v = _ev(n.value, out)
            if isinstance(v, str):
                out[n.targets[0].id] = v
    return out


def _ev(n, env):
    """Constant-fold a node to a str (or list of str / int), else None."""
    if isinstance(n, ast.Constant):
        return n.value if isinstance(n.value, (str, int)) else None
    if isinstance(n, ast.Name):
        return env.get(n.id)
    if isinstance(n, (ast.List, ast.Tuple)):
        vals = [_ev(e, env) for e in n.elts]
        return vals if all(v is not None for v in vals) else None
    if isinstance(n, ast.BinOp):
        a, b = _ev(n.left, env), _ev(n.right, env)
        if isinstance(n.op, ast.Add) and isinstance(a, str) and isinstance(b, str):
            return a + b
        if isinstance(n.op, ast.Mod) and isinstance(a, str):
            try:
                return a % (tuple(b) if isinstance(b, list) else b)
            except Exception:       # noqa: BLE001
                return None
        return None
    if isinstance(n, ast.JoinedStr):
        parts = []
        for v in n.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                x = _ev(v.value, env)
                if x is None:
                    return None
                parts.append(str(x))
        return "".join(parts)
    if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Slice):
        s = _ev(n.value, env)
        st = n.slice
        step = _ev(st.step, env) if st.step is not None else None
        if isinstance(s, str) and st.lower is None and st.upper is None and step == -1:
            return s[::-1]
        return None
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
        v = _ev(n.operand, env)
        return -v if isinstance(v, int) else None
    if isinstance(n, ast.Call):
        f = n.func
        if isinstance(f, ast.Name) and f.id == "chr" and len(n.args) == 1:
            v = _ev(n.args[0], env)
            return chr(v) if isinstance(v, int) else None
        if isinstance(f, ast.Attribute) and f.attr == "join":
            if isinstance(f.value, ast.Attribute) and f.value.attr == "path":          # os.path.join(a, b, ...)
                vals = [_ev(a, env) for a in n.args]
                run = []
                for v in reversed(vals):               # the constant TAIL still names the file even if the head is dynamic
                    if not isinstance(v, str):
                        break
                    run.insert(0, v)
                return "/".join(run) if run else None
            sep = _ev(f.value, env)
            if isinstance(sep, str) and len(n.args) == 1:
                arg = n.args[0]
                if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "map" \
                        and len(arg.args) == 2 and isinstance(arg.args[0], ast.Name) and arg.args[0].id == "chr":
                    xs = _ev(arg.args[1], env)
                    return sep.join(chr(x) for x in xs) if isinstance(xs, list) and all(isinstance(x, int) for x in xs) else None
                xs = _ev(arg, env)
                if isinstance(xs, list):
                    if all(isinstance(x, str) for x in xs):
                        return sep.join(xs)
                    if all(isinstance(x, int) for x in xs):
                        return None
                if isinstance(arg, ast.GeneratorExp) or isinstance(arg, ast.ListComp):
                    e = arg.elt
                    if isinstance(e, ast.Call) and isinstance(e.func, ast.Name) and e.func.id == "chr" \
                            and len(arg.generators) == 1:
                        xs = _ev(arg.generators[0].iter, env)
                        return sep.join(chr(x) for x in xs) if isinstance(xs, list) and all(isinstance(x, int) for x in xs) else None
            return None
        if isinstance(f, ast.Attribute) and f.attr == "format":
            base = _ev(f.value, env)
            args = [_ev(a, env) for a in n.args]
            if isinstance(base, str) and all(a is not None for a in args):
                try:
                    return base.format(*args)
                except Exception:   # noqa: BLE001
                    return None
    return None


def _is_environ(n) -> bool:
    return (isinstance(n, ast.Attribute) and n.attr == "environ") or (isinstance(n, ast.Name) and n.id == "environ")


# Generic env helpers that take the variable NAME as a parameter, and a child-process env copy. They are reviewed
# exceptions, each with its compensating control: rule 3 still sees any CALLER that passes the registry names as a
# constant/folded string, so such a helper cannot be handed the registry variable without the guard firing there.
ALLOWED_DYNAMIC = {
    ("factlattice_engine/build_stamp.py", "_env_flag"): "generic flag reader; callers pass literals (rule 3 covers them)",
    ("factlattice_engine/selfheal.py", "_env_flag"): "generic flag reader; callers pass literals (rule 3 covers them)",
    ("factlattice_engine/semantic/embedd.py", "_spawn"): "copies the environment for a child process (pass-through, no read)",
    ("factlattice_engine/semantic/embedd.py", "_env_interval"): "generic interval reader; callers pass literals",
}


def _enclosing(tree):
    owner = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                owner[id(n)] = fn.name
    return owner


def scan_ast_tree(root: Path, allowed=ALLOWED) -> List[Tuple[str, int, str]]:
    """`[(relative path, line, what)]` for every `.py` under `root` outside tests/private/build and the resolver."""
    root = Path(root)
    found: List[Tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        parts = set(rel.split("/")[:-1])
        if rel in allowed or parts & SKIP_DIRS or os.path.basename(rel) in SKIP_FILES \
                or os.path.basename(rel).startswith("test_") or any(p.endswith(".egg-info") for p in parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        env = _consts(tree)
        owner = _enclosing(tree)
        for n in ast.walk(tree):
            line = getattr(n, "lineno", 0)
            exempt = (rel, owner.get(id(n))) in ALLOWED_DYNAMIC          # exempts ONLY the generic env-read rules below
            if isinstance(n, ast.Attribute) and n.attr in FORBIDDEN_ATTRS:
                found.append((rel, line, f"reference to {n.attr}"))
            elif isinstance(n, ast.Name) and n.id in FORBIDDEN_ATTRS:
                found.append((rel, line, f"reference to {n.id}"))
            elif isinstance(n, ast.ImportFrom) and any(a.name in FORBIDDEN_ATTRS for a in n.names):
                found.append((rel, line, "import of a resolver helper"))
            if isinstance(n, (ast.Constant, ast.BinOp, ast.JoinedStr, ast.Call, ast.Subscript)):
                v = _ev(n, env)
                if isinstance(v, str):
                    for t in TARGET_STRINGS:
                        if t in v:
                            found.append((rel, line, f"evaluates to {t}"))
            # environment reads
            if isinstance(n, ast.Call):
                f = n.func
                key = None
                is_read = False
                if isinstance(f, ast.Attribute) and f.attr in ("get", "pop", "setdefault") and _is_environ(f.value):
                    is_read, key = True, n.args[0] if n.args else None
                elif isinstance(f, ast.Attribute) and f.attr == "getenv" or isinstance(f, ast.Name) and f.id == "getenv":
                    is_read, key = True, n.args[0] if n.args else None
                if is_read:
                    kv = _ev(key, env) if key is not None else None
                    if not isinstance(kv, str) and not exempt:
                        found.append((rel, line, "environment read with a key that is not a plain string"))
                if isinstance(f, ast.Attribute) and f.attr in ("items", "keys", "values", "copy") and _is_environ(f.value):
                    found.append((rel, line, "whole-environment scan"))
                if isinstance(f, ast.Name) and f.id in ("dict", "list", "sorted") and n.args and _is_environ(n.args[0]) \
                        and not exempt:
                    found.append((rel, line, "whole-environment copy"))
            if isinstance(n, ast.Subscript) and _is_environ(n.value) and not isinstance(n.slice, ast.Slice):
                if not isinstance(_ev(n.slice, env), str):
                    found.append((rel, line, "environment read with a key that is not a plain string"))
            if isinstance(n, (ast.For, ast.comprehension)) and _is_environ(n.iter):
                found.append((rel, line, "whole-environment scan"))
    return sorted(set(found))
