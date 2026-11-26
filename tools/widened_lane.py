#!/usr/bin/env python3
"""The widened lane: the test files to run after a change to the engine, computed from the tree (no list to go stale).

A lane of "files that import a changed module" misses tests that read repo SOURCE or DOCUMENT TEXT without importing anything (the docs, inventory and fan-out gates,
SDLC/SPEC readers). The widened lane is the union of
  1. IMPORTERS: every tests/test_*.py that imports (by `import` / `from ... import`, including a class the package `__init__` re-exports, such as
     `from factlattice_engine import LtsEngine`) a module a change touched OR a package module that imports one, directly or through others (the closure over the
     packages' own imports), or names a runtime route to the engine (`factlattice_engine.server`, `factlattice_engine.kernel`, `stall_harness`, `python -m factlattice_engine`);
  1b. ALWAYS: every test that imports `factlattice.registry` or `factlattice.rootview` (`ALWAYS_IMPORTERS`), whatever changed.
  2. READERS: every tests/test_*.py that opens or reads files (`read_text`, `read_bytes`, `open(`, `glob(`, `rglob(`, `os.walk`, `iterdir`) AND refers to the repo
     (`__file__`, `REPO`, `ROOT`, `SDLC`, `SPEC.md`, `CHANGELOG`, `specs/`, `.parent.parent`, `git_root`, `repo_root`).
The importers are those of the CHANGED modules: `--changed a.py b.py` names them, else they are the `.py` files under factlattice/ and factlattice_engine/ that
`git diff --name-only <base>` reports (`--base REV`, default HEAD~1) plus the working tree's. The runtime routes always count.

`--transitive` also takes the tests that import a package module which itself imports a changed one, through any chain (for `tools.py` or `config.py` that is
nearly every test: use it for a full-suite decision, not for a lane).

usage: python tools/widened_lane.py [--changed factlattice_engine/store.py ...] [--base REV] [--transitive] [--run [pytest args...]]
Prints one test file per line; `--run` runs them in ONE pytest process (set TMPDIR, and FACTLATTICE_RELEASE_REPO for the verdict table's honesty check).
The lane runs under the machine's load: state `uptime` beside the result."""
import ast
import glob
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = ("factlattice_engine.server", "factlattice_engine.kernel", "stall_harness", "python -m factlattice_engine", '-m", "factlattice_engine')
# The registry's own modules: a change anywhere in the boot, the config or the tools touches how a registry is read, so every test that imports them is in the lane
# whatever else changed (the registry-error boot tests, the registry hash scope, the external-prefix contradiction test).
ALWAYS_IMPORTERS = {"factlattice.registry", "factlattice.rootview"}
READ = re.compile(r"(read_text|read_bytes|\bopen\(|glob\(|rglob\(|os\.walk|iterdir)")
REPOREF = re.compile(r"(__file__|\bREPO\b|\bROOT\b|SDLC|SPEC\.md|CHANGELOG|specs/|\.parent\.parent|git_root|repo_root)")


def _git_changed(base):
    names = set()
    for cmd in (["git", "diff", "--name-only", base], ["git", "diff", "--name-only"], ["git", "ls-files", "--others", "--exclude-standard"]):
        r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
        names |= {x for x in r.stdout.split() if x.endswith(".py") and x.startswith(("factlattice/", "factlattice_engine/"))}
    return sorted(names)


def _modules(changed):
    return {c[:-3].replace("/", ".").replace(os.sep, ".").removesuffix(".__init__") for c in changed}


def _pkg_modules():
    """{dotted module name: path} for every module of the two packages."""
    out = {}
    for pkg in ("factlattice", "factlattice_engine"):
        for f in glob.glob(os.path.join(REPO, pkg, "**", "*.py"), recursive=True):
            rel = os.path.relpath(f, REPO)[:-3].replace(os.sep, ".")
            out[rel.removesuffix(".__init__")] = f
    return out


def _deps(name, path, known):
    """The package modules that module `name` imports (absolute, relative, `from pkg import mod`, and a name the package's __init__ re-exports)."""
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (OSError, SyntaxError):
        return set()
    is_pkg = path.endswith("__init__.py")
    base = name if is_pkg else name.rpartition(".")[0]
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            if n.level:
                parts = base.split(".")
                parts = parts[: len(parts) - (n.level - 1)]
                mod = ".".join(parts + ([n.module] if n.module else []))
            else:
                mod = n.module or ""
            if mod in known:
                out.add(mod)
            for a in n.names:
                sub = mod + "." + a.name
                if sub in known:
                    out.add(sub)
                elif mod in _REEXPORT and a.name in _REEXPORT[mod]:
                    out.add(_REEXPORT[mod][a.name])
        elif isinstance(n, ast.Import):
            out |= {a.name for a in n.names if a.name in known}
    return out


_REEXPORT = {}


def _closure(changed_mods):
    """The changed modules plus every package module that imports one of them, directly or through others."""
    known = _pkg_modules()
    for pkg in ("factlattice", "factlattice_engine"):          # names a package's __init__ re-exports: `from factlattice_engine import LtsEngine` reaches `tools`
        init = os.path.join(REPO, pkg, "__init__.py")
        if os.path.exists(init):
            m = {}
            for n in ast.walk(ast.parse(open(init, encoding="utf-8").read())):
                if isinstance(n, ast.ImportFrom) and n.level == 1 and n.module:
                    for a in n.names:
                        m[a.asname or a.name] = pkg + "." + n.module
            _REEXPORT[pkg] = m
    graph = {name: _deps(name, path, known) for name, path in known.items()}
    hit = set(changed_mods)
    grew = True
    while grew:
        grew = False
        for name, deps in graph.items():
            if name not in hit and deps & hit:
                hit.add(name)
                grew = True
    return hit


def _git_changed(base):
    names = set()
    for cmd in (["git", "diff", "--name-only", base], ["git", "diff", "--name-only"], ["git", "ls-files", "--others", "--exclude-standard"]):
        r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
        names |= {x for x in r.stdout.split() if x.endswith(".py") and x.startswith(("factlattice/", "factlattice_engine/"))}
    return sorted(names)


def lane(changed=(), transitive=False):
    changed = list(changed)
    mods = (_closure(_modules(changed)) if transitive else _modules(changed)) | ALWAYS_IMPORTERS
    _closure(set())                                   # fills _REEXPORT (what the packages' __init__ re-export)
    out = []
    for f in sorted(glob.glob(os.path.join(REPO, "tests", "test_*.py"))):
        src = open(f, encoding="utf-8").read()
        hit = any(x in src for x in RUNTIME)
        try:
            tree = ast.parse(src)
        except SyntaxError:
            tree = None
        for n in (ast.walk(tree) if tree else ()):
            if isinstance(n, ast.ImportFrom):
                m = n.module or ""
                if m in mods or any(m.startswith(x + ".") for x in mods):
                    hit = True
                if m in ("factlattice", "factlattice_engine"):
                    for a in n.names:
                        if m + "." + a.name in mods or _REEXPORT.get(m, {}).get(a.name) in mods:
                            hit = True
            elif isinstance(n, ast.Import):
                hit = hit or any(a.name in mods or any(a.name.startswith(x + ".") for x in mods) for a in n.names)
        if hit or (READ.search(src) and REPOREF.search(src)):
            out.append(os.path.relpath(f, REPO))
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    changed = []
    if "--changed" in args:
        i = args.index("--changed")
        j = next((k for k in range(i + 1, len(args)) if args[k].startswith("--")), len(args))
        changed = args[i + 1:j]
        args = args[:i] + args[j:]
    base = "HEAD~1"
    if "--base" in args:
        i = args.index("--base")
        base = args[i + 1]
        args = args[:i] + args[i + 2:]
    transitive = "--transitive" in args
    args = [a for a in args if a != "--transitive"]
    files = lane(changed or _git_changed(base), transitive)
    if "--run" in args:
        extra = args[args.index("--run") + 1:]
        sys.exit(subprocess.call([sys.executable, "-m", "pytest", *files, "-q", "-p", "no:cacheprovider", *extra], cwd=REPO))
    print("\n".join(files))
