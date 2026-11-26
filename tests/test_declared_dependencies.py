"""The required dependencies pyproject declares are enough to import the MCP server and the typed writers.

2026-11-15: pyproject declared `dependencies = []` while factlattice/authoring/schema.py imports pydantic at top
level, so a plain install could not start the server or write. This test runs offline: it blocks every third-party
module the packages use that pyproject does NOT declare as required, then imports the entry points. A module listed
only in an optional extra (numpy, onnxruntime, yaml) must stay optional: the entry points import without it.
"""
import pathlib
import subprocess
import sys
import tomllib

REPO = pathlib.Path(__file__).resolve().parents[1]
# distribution name (as pyproject writes it) -> top-level module it provides
DIST_TO_MODULE = {"pydantic": "pydantic", "pyyaml": "yaml", "numpy": "numpy", "onnxruntime": "onnxruntime"}
THIRD_PARTY = set(DIST_TO_MODULE.values())
ENTRY_POINTS = ("factlattice_engine.server", "factlattice.authoring.edit", "factlattice_engine.tools", "factlattice")

_BLOCK_AND_IMPORT = r"""
import sys, importlib, importlib.abc
repo, blocked, mods = sys.argv[1], set(sys.argv[2].split(",")) - {""}, sys.argv[3].split(",")
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in blocked:
            raise ModuleNotFoundError("blocked (not a declared required dependency): " + name, name=name)
        return None
sys.meta_path.insert(0, Block())
sys.path.insert(0, repo)
for m in mods:
    importlib.import_module(m)
print("imported", ",".join(mods))
"""


def _project():
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _dist(req: str) -> str:
    for sep in "<>=!~[; ":
        req = req.split(sep)[0]
    return req.strip().lower()


def test_every_third_party_module_is_declared_somewhere():
    proj = _project()
    declared = {_dist(r) for r in proj.get("dependencies", [])}
    for reqs in proj.get("optional-dependencies", {}).values():
        declared |= {_dist(r) for r in reqs}
    missing = [d for d in DIST_TO_MODULE if d not in declared]
    assert not missing, f"used by the packages but declared nowhere in pyproject: {missing}"


def test_required_dependencies_are_enough_for_the_server_and_the_writers():
    required = {DIST_TO_MODULE[_dist(r)] for r in _project().get("dependencies", []) if _dist(r) in DIST_TO_MODULE}
    blocked = sorted(THIRD_PARTY - required)
    r = subprocess.run([sys.executable, "-I", "-c", _BLOCK_AND_IMPORT, str(REPO), ",".join(blocked), ",".join(ENTRY_POINTS)],
                       capture_output=True, text=True, timeout=300, cwd=str(REPO.parent))
    assert r.returncode == 0, (f"with only the declared required dependencies ({sorted(required)}), "
                               f"an entry point failed to import:\n{r.stderr[-2000:]}")


def test_the_blocker_is_live():
    """Positive control: blocking pydantic itself must make the writers fail to import."""
    r = subprocess.run([sys.executable, "-I", "-c", _BLOCK_AND_IMPORT, str(REPO), "pydantic", "factlattice.authoring.edit"],
                       capture_output=True, text=True, timeout=300, cwd=str(REPO.parent))
    assert r.returncode != 0 and "blocked (not a declared required dependency): pydantic" in r.stderr


def test_module_list_matches_the_import_walk():
    """This test's list of third-party modules is the import walk's (test_parser_config_py311.THIRD_PARTY_DEPS), so a new
    dependency added there must be mapped here too."""
    sys.path.insert(0, str(REPO / "tests"))
    try:
        import test_parser_config_py311 as walk
    finally:
        sys.path.pop(0)
    assert THIRD_PARTY == set(walk.THIRD_PARTY_DEPS)
