"""A-E6 / X8 SHIPPED-ISSUE-PACKAGE-DATA: the reviewer corpus is in the WHEEL, not only in the checkout.

The reviewer's fetch order is (1) the engine serves `factlattice-reviewer`, else (2) a file read of the
shipped copy at `<package>/reviewer/`. Step (2) is the whole point of shipping it: a customer who installed the
wheel has no checkout. A `.lts` file that is on disk in the repo and absent from the wheel passes every test
that runs in the repo and fails exactly where it is needed. So this builds the wheel and reads its member
list.

NEGATIVE CONTROL INSIDE THE TEST: the same build with the package-data declaration removed must NOT contain
the files, otherwise "the wheel contains them" would hold whatever pyproject said (setuptools can pick up
files for other reasons, e.g. a stale MANIFEST or an egg-info SOURCES list).
"""
from __future__ import annotations

import fnmatch
import glob
import os
import re
import shutil
import subprocess
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "factlattice_engine", "reviewer")

# The shipped corpus: every file the reviewer procedure reads at runtime, plus the fixtures its rules point at.
EXPECTED_CORPUS_FILES = (
    "shipped/reviewer-profile.lts",
    "shipped/reviewer-core.lts",
    "shipped/reviewer-hooks.lts",
    "shipped/reviewer-config.lts",
    "shipped/reviewer-rules.lts",
)


def _pyproject():
    return open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()


def test_the_package_is_declared_and_its_data_is_declared():
    t = _pyproject()
    assert '"factlattice_engine.reviewer"' in t
    m = re.search(r'"factlattice_engine\.reviewer"\s*=\s*\[([^\]]*)\]', t)
    assert m, "no package-data entry for factlattice_engine.reviewer"
    globs = re.findall(r'"([^"]+)"', m.group(1))
    assert "shipped/*.lts" in globs and "fixtures/*.lts" in globs


def test_every_shipped_lts_on_disk_is_matched_by_a_package_data_glob():
    t = _pyproject()
    m = re.search(r'"factlattice_engine\.reviewer"\s*=\s*\[([^\]]*)\]', t)
    globs = re.findall(r'"([^"]+)"', m.group(1))
    on_disk = [os.path.relpath(p, PKG) for p in glob.glob(os.path.join(PKG, "**", "*.lts"), recursive=True)]
    assert on_disk, "the reviewer package ships no .lts at all"
    unmatched = [p for p in on_disk if not any(fnmatch.fnmatch(p, g) for g in globs)]
    assert unmatched == [], f"shipped .lts files no package-data glob matches: {unmatched}"
    for name in EXPECTED_CORPUS_FILES:
        assert name in on_disk, name


def _build_wheel(tmp_path, pyproject_text=None):
    """Build a wheel from a COPY of the tree (never leave build/ or egg-info in the worktree)."""
    src = tmp_path / ("src-" + str(len(list(tmp_path.glob("src-*")))))
    src.mkdir()
    ign = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", "private", "*.egg-info", "build")
    for name in ("factlattice", "factlattice_engine"):
        shutil.copytree(os.path.join(ROOT, name), src / name, ignore=ign)
    shutil.copy(os.path.join(ROOT, "README.md"), src / "README.md")
    shutil.copy(os.path.join(ROOT, "LICENSE"), src / "LICENSE")
    (src / "pyproject.toml").write_text(pyproject_text if pyproject_text is not None else _pyproject())
    out = tmp_path / ("wheel-" + src.name)
    out.mkdir()
    code = ("import setuptools.build_meta as b, sys; print(b.build_wheel(sys.argv[1]))")
    p = subprocess.run([sys.executable, "-c", code, str(out)], cwd=str(src), capture_output=True, text=True,
                       timeout=300)
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-2000:]
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1, wheels
    return zipfile.ZipFile(wheels[0]).namelist()


@pytest.mark.skipif(not shutil.which(sys.executable), reason="no interpreter")
def test_the_built_wheel_contains_the_reviewer_corpus_and_fixtures(tmp_path):
    pytest.importorskip("setuptools")
    names = _build_wheel(tmp_path)
    for rel in EXPECTED_CORPUS_FILES:
        assert f"factlattice_engine/reviewer/{rel}" in names, rel
    fixtures = [os.path.relpath(p, PKG) for p in glob.glob(os.path.join(PKG, "fixtures", "*.lts"))]
    assert fixtures, "no fixtures shipped"
    for rel in fixtures:
        assert f"factlattice_engine/reviewer/{rel}" in names, rel
    assert "factlattice_engine/reviewer/__init__.py" in names and "factlattice_engine/reviewer/__main__.py" in names


def test_negative_control_without_the_declaration_the_wheel_lacks_them(tmp_path):
    pytest.importorskip("setuptools")
    stripped = re.sub(r'"factlattice_engine\.reviewer"\s*=\s*\[[^\]]*\]\n', "", _pyproject())
    assert stripped != _pyproject(), "the control did not remove the declaration"
    names = _build_wheel(tmp_path, stripped)
    assert not [n for n in names if n.startswith("factlattice_engine/reviewer/") and n.endswith(".lts")], \
        "the .lts files are in the wheel WITHOUT the package-data declaration: this test cannot detect a missing one"


def test_no_shipped_lts_is_git_ignored():
    """The repo's .gitignore has a generic `corpus/` rule ("the public repo ships no corpus content"). A shipped
    directory with that name was silently ignored: every test passed in the checkout that had the files and a fresh
    clone would have had none. Found by `git add`, not by a test, hence this one."""
    if not os.path.isdir(os.path.join(ROOT, ".git")) and not os.path.isfile(os.path.join(ROOT, ".git")):
        pytest.skip("not a git work tree")
    files = [os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(PKG, "**", "*.lts"), recursive=True)]
    assert len(files) >= 14
    p = subprocess.run(["git", "check-ignore", "-v", *files], cwd=ROOT, capture_output=True, text=True)
    assert p.returncode == 1 and p.stdout == "", f"git ignores shipped files:\n{p.stdout}"
