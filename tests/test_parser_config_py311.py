"""ParserConfig must import on every supported Python, including 3.11.

Python 3.11's dataclasses refuse an unhashable default ("mutable default <class
'mappingproxy'> ... use default_factory"); 3.12 made mappingproxy hashable and 3.9/3.10
do not check, so the defect showed only on 3.11. pyproject now says requires-python >= 3.11.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from factlattice.parser_config import _EMPTY_MAP, ParserConfig

REPO = Path(__file__).resolve().parents[1]


def test_defaults_are_the_shared_empty_map():
    cfg = ParserConfig()
    assert cfg.profile_layers is _EMPTY_MAP
    assert cfg.property_edges is _EMPTY_MAP
    with pytest.raises(TypeError):
        cfg.profile_layers["x"] = "y"  # still read-only


def _find(version):
    # The interpreter running this suite comes first for its own version: it is the one whose
    # environment has the dependencies installed (a CI venv), which PATH lookups would miss.
    cands = []
    if version == "%d.%d" % sys.version_info[:2]:
        cands.append(sys.executable)
    cands += [shutil.which("python" + version), "/opt/homebrew/bin/python" + version]
    import pwd  # the real home: the suite may set a neutral HOME
    uv = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/share/uv/python"
    if uv.is_dir():
        cands += sorted(str(p) for p in uv.glob("cpython-%s*/bin/python%s" % (version, version)))
    for cand in cands:
        if cand and os.path.exists(cand):
            return cand
    return None


# Walk EVERY module of both packages, not a chosen few: a narrow import list stayed green
# while render/html.py (an f-string backslash, a SyntaxError before 3.12) and
# authoring/schema.py (enum.StrEnum, 3.11+) failed on supported versions.
# A module whose import fails only because a THIRD-PARTY dependency (e.g. mcp, pydantic) is
# not installed in that interpreter is reported as skipped; any other failure fails the test.
# The packages' third-party imports, the ONLY modules whose absence may skip a module.
# test_optional_deps_list_is_complete re-derives this set from the code, so a new
# third-party import must be added here deliberately. A skip here is NOT a verdict that the
# dependency is optional: pydantic is required by factlattice.authoring (and so by the
# server) and is declared in pyproject (tests/test_declared_dependencies.py pins that).
THIRD_PARTY_DEPS = ("numpy", "onnxruntime", "pydantic", "yaml")

_WALK = r"""
import importlib, json, pkgutil, sys, traceback
sys.path.insert(0, sys.argv[1])
own = ("factlattice", "factlattice_engine")
optional = set(sys.argv[2].split(","))
def third_party(e):
    # A skip is allowed only for a NAMED module on the allow-list of the packages'
    # third-party dependencies (THIRD_PARTY_DEPS below). Anything else that is missing, a
    # standard-library module removed in a newer Python included, is a failure.
    top = (e.name or "").split(".")[0]
    return bool(top) and top in optional
bad, skipped, ok = [], [], 0
for top in own:
    try:
        pkg = importlib.import_module(top)
    except Exception as e:
        bad.append((top, "".join(traceback.format_exception_only(type(e), e)).strip())); continue
    def onerror(name):
        e = sys.exc_info()[1]
        if not (isinstance(e, ModuleNotFoundError) and third_party(e)):
            bad.append((name, "".join(traceback.format_exception_only(type(e), e)).strip()))
    for m in pkgutil.walk_packages(pkg.__path__, top + ".", onerror=onerror):
        try:
            importlib.import_module(m.name); ok += 1
        except ModuleNotFoundError as e:
            if third_party(e):
                skipped.append((m.name, e.name))
            else:
                bad.append((m.name, repr(e)))
        except Exception as e:
            bad.append((m.name, "".join(traceback.format_exception_only(type(e), e)).strip()))
print(json.dumps({"ok": ok, "bad": bad, "skipped": skipped}))
"""


def _floor():
    import re
    text = (REPO / "pyproject.toml").read_text()
    m = re.search(r'requires-python\s*=\s*">=\s*3\.(\d+)"', text)
    assert m, "requires-python floor not found in pyproject.toml"
    return int(m.group(1))


# Every minor version from the declared floor up is checked; a version below the floor is
# not claimed, so it is not tested. Raising or lowering the floor changes what is checked.
@pytest.mark.parametrize("minor", [9, 10, 11, 12, 13, 14])
def test_package_imports_on_supported_python(minor):
    if minor < _floor():
        pytest.skip("below requires-python")
    version = "3.%d" % minor
    py = _find(version)
    if py is None:
        pytest.skip("python%s not installed here" % version)
    r = subprocess.run([py, "-I", "-c", _WALK, str(REPO), ",".join(THIRD_PARTY_DEPS)], capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-1500:]
    out = __import__("json").loads(r.stdout.strip().splitlines()[-1])
    assert out["ok"] > 20, out
    assert out["bad"] == [], out["bad"]
    _WALKED[version] = out
    if out["skipped"]:
        import warnings
        warnings.warn("python%s skipped %d module(s) for missing third-party deps: %s"
                      % (version, len(out["skipped"]), sorted({d for _, d in out["skipped"]})))


_WALKED = {}


def test_some_supported_python_walks_every_module():
    """A skip is not a pass: at least one interpreter from the floor up must import EVERY
    module, so the modules behind third-party deps are checked somewhere."""
    for minor in range(_floor(), 15):
        version = "3.%d" % minor
        if version not in _WALKED:
            py = _find(version)
            if py is None:
                continue
            r = subprocess.run([py, "-I", "-c", _WALK, str(REPO), ",".join(THIRD_PARTY_DEPS)], capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                continue
            _WALKED[version] = __import__("json").loads(r.stdout.strip().splitlines()[-1])
    full = [v for v, o in _WALKED.items() if not o["bad"] and not o["skipped"]]
    assert full, {v: sorted({d for _, d in o["skipped"]}) for v, o in _WALKED.items()}


def test_optional_deps_list_is_complete():
    """THIRD_PARTY_DEPS equals the set of non-stdlib, non-own top-level modules the packages import."""
    import ast
    own = {"factlattice", "factlattice_engine"}
    std = set(sys.stdlib_module_names)
    found = set()
    for pkg in own:
        for p in (REPO / pkg).rglob("*.py"):
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                mods = [a.name for a in n.names] if isinstance(n, ast.Import) else (
                    [n.module] if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module else [])
                found |= {m.split(".")[0] for m in mods} - own - std
    assert found == set(THIRD_PARTY_DEPS), found
