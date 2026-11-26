"""[MECH-BUILD-STAMP] — the stamp must cover the core package as well as the engine package.

The sentinel re-execs a running server when the stamp changes. A stamp over `factlattice_engine/`
alone never moves for a change that touches only `factlattice/` (the parser, the authoring
writers, ...), so no server relaunched and every session kept running old core code with no
signal.

Most cases run the stamp in a SUBPROCESS over a private copy of BOTH packages, with the default
arguments, because the thing under test is "the stamp, as the server imports it": a test that
hands `build_stamp` its directories would share the blind spot of a build_stamp that ignores them.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

import factlattice as _fl
import factlattice_engine as _fe
from factlattice_engine import build_stamp as bs

ENGINE_DIR = pathlib.Path(_fe.__file__).resolve().parent
CORE_DIR = pathlib.Path(_fl.__file__).resolve().parent

_PROBE = (
    "import factlattice, factlattice_engine, os\n"
    "from factlattice_engine.build_stamp import build_stamp\n"
    "print(os.path.dirname(os.path.abspath(factlattice.__file__)))\n"
    "print(os.path.dirname(os.path.abspath(factlattice_engine.__file__)))\n"
    "print(build_stamp())\n"
)


@pytest.fixture()
def tree(tmp_path):
    """A private copy of both packages, side by side, as a deployed server sees them."""
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(ENGINE_DIR, tmp_path / "tree" / "factlattice_engine", ignore=ignore)
    shutil.copytree(CORE_DIR, tmp_path / "tree" / "factlattice", ignore=ignore)
    (tmp_path / "cwd").mkdir()
    return tmp_path / "tree"


def _stamp(tree: pathlib.Path, *, eng_parent: pathlib.Path | None = None):
    """build_stamp() with default arguments, imported in a fresh process. The core package is
    imported from `tree`; the engine package from `eng_parent` (default: `tree` too, the usual
    side-by-side layout). Also asserts which copy of each package answered."""
    eng_parent = eng_parent or tree
    path = os.pathsep.join(dict.fromkeys([str(eng_parent), str(tree)]))
    env = dict(os.environ, PYTHONPATH=path)
    p = subprocess.run([sys.executable, "-c", _PROBE], cwd=str(tree.parent / "cwd"), env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-2000:]
    core, eng, stamp = p.stdout.split("\n")[:3]
    assert core == str(tree / "factlattice") and eng == str(eng_parent / "factlattice_engine"), (
        "the probe imported a package from outside the copy",
        core,
        eng,
    )
    return None if stamp == "None" else stamp


def test_the_core_package_is_found_by_import_when_it_is_not_the_engines_sibling(tmp_path):
    """Engine and core in DIFFERENT import paths (a checkout engine over an installed core, say):
    a core directory taken as the engine's sibling does not exist here, so the stamp would be None
    and the sentinel blind. The stamp must still be a value, and a core-only edit must move it."""
    ignore = shutil.ignore_patterns("__pycache__")
    eng_parent, core_parent = tmp_path / "eng_path", tmp_path / "core_path"
    shutil.copytree(ENGINE_DIR, eng_parent / "factlattice_engine", ignore=ignore)
    shutil.copytree(CORE_DIR, core_parent / "factlattice", ignore=ignore)
    (tmp_path / "cwd").mkdir()
    before = _stamp(core_parent, eng_parent=eng_parent)
    assert before and before.startswith("sha256:"), "no stamp when the core is not the engine's sibling"
    with open(core_parent / "factlattice" / "parser.py", "a") as fh:
        fh.write("\n# a core-only edit in the split layout\n")
    assert _stamp(core_parent, eng_parent=eng_parent) != before


def test_the_stamp_roots_are_the_top_level_packages_pyproject_ships():
    """SDLC.md BUILD-STAMP-ROOTS: a package the server imports that is not a stamp root would
    never relaunch a server on its own change. Both lists are held against each other."""
    import tomllib

    pyproject = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"
    pkgs = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["setuptools"]["packages"]
    assert {p.split(".")[0] for p in pkgs} == set(bs.STAMP_PACKAGES), (sorted({p.split(".")[0] for p in pkgs}), bs.STAMP_PACKAGES)
    assert bs.STAMP_PACKAGES == (bs.ENGINE_PACKAGE, bs.CORE_PACKAGE)


def test_a_change_only_under_the_core_package_moves_the_stamp(tree):
    before = _stamp(tree)
    assert before and before.startswith("sha256:")
    with open(tree / "factlattice" / "parser.py", "a") as fh:
        fh.write("\n# a core-only edit\n")
    assert _stamp(tree) != before, "a change only under factlattice/ did not move the stamp"


def test_a_change_under_the_engine_package_still_moves_the_stamp(tree):
    before = _stamp(tree)
    with open(tree / "factlattice_engine" / "server.py", "a") as fh:
        fh.write("\n# an engine edit\n")
    assert _stamp(tree) != before


def test_a_new_py_file_in_a_core_subpackage_moves_the_stamp(tree):
    before = _stamp(tree)
    (tree / "factlattice" / "authoring" / "added_later.py").write_text("X = 1\n")
    assert _stamp(tree) != before


@pytest.mark.parametrize("pkg", ["factlattice", "factlattice_engine"])
def test_a_non_py_change_does_not_move_the_stamp(tree, pkg):
    before = _stamp(tree)
    (tree / pkg / "notes.txt").write_text("not source\n")
    (tree / pkg / "README.md").write_text("# not source\n")
    with open(tree / pkg / "py.typed", "a") as fh:
        fh.write("\n")
    assert _stamp(tree) == before


def test_the_stamp_is_repeatable_and_independent_of_mtime(tree):
    a = _stamp(tree)
    assert _stamp(tree) == a
    for pkg, name in (("factlattice", "parser.py"), ("factlattice_engine", "server.py")):
        f = tree / pkg / name
        st = f.stat()
        os.utime(f, (st.st_atime + 5000, st.st_mtime + 5000))
    assert _stamp(tree) == a, "an mtime-only change moved the stamp"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
@pytest.mark.parametrize("pkg", ["factlattice", "factlattice_engine"])
def test_an_unreadable_file_in_either_package_gives_none(tree, pkg):
    victim = tree / pkg / "never_imported_probe.py"  # not imported, so only the stamp reads it
    victim.write_text("X = 1\n")
    assert _stamp(tree) is not None
    victim.chmod(0)
    try:
        assert _stamp(tree) is None, "an unreadable source file must fail safe to None"
    finally:
        victim.chmod(0o644)


# ---- in-process, with the roots handed in (the sentinel and the helpers) ----------------------


def _two(tmp_path, eng_src="E = 1\n", core_src="C = 1\n"):
    eng, core = tmp_path / "e" / "factlattice_engine", tmp_path / "c" / "factlattice"
    eng.mkdir(parents=True)
    core.mkdir(parents=True)
    (eng / "mod.py").write_text(eng_src)
    (core / "mod.py").write_text(core_src)
    return str(eng), str(core)


def test_the_same_relative_path_in_the_two_roots_is_not_ambiguous(tmp_path):
    """`mod.py` under the engine root and `mod.py` under the core root are different files:
    moving a source file from one package to the other must move the stamp."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / "eng").mkdir(parents=True)
    (a / "core").mkdir()
    (b / "eng").mkdir(parents=True)
    (b / "core").mkdir()
    (a / "eng" / "mod.py").write_text("SAME = 1\n")  # the file lives in the engine root
    (b / "core" / "mod.py").write_text("SAME = 1\n")  # the same bytes live in the core root
    sa = bs.build_stamp(str(a / "eng"), str(a / "core"))
    sb = bs.build_stamp(str(b / "eng"), str(b / "core"))
    assert sa and sb and sa != sb, "an engine-root file and a core-root file hash alike"


def test_the_core_root_is_found_by_import_not_by_a_fixed_path():
    assert bs.core_package_dir() == str(CORE_DIR)
    assert bs.engine_package_dir() == str(ENGINE_DIR)
    assert bs.build_stamp() == bs.build_stamp(str(ENGINE_DIR), str(CORE_DIR))


def test_a_missing_root_gives_none(tmp_path):
    eng, core = _two(tmp_path)
    assert bs.build_stamp(eng, core)
    assert bs.build_stamp(str(tmp_path / "gone"), core) is None
    assert bs.build_stamp(eng, str(tmp_path / "gone")) is None


def test_the_sentinel_fires_on_a_core_only_change(tmp_path):
    eng, core = _two(tmp_path)
    fired = []
    s = bs.CodeCurrencySentinel(package_dir=eng, core_dir=core, reconcile=lambda: fired.append(1))
    assert s.check_once() is False and fired == []
    (pathlib.Path(core) / "mod.py").write_text("C = 2  # the propagated core fix\n")
    assert s.check_once() is True and fired == [1] and s.divergences == 1


def test_the_sentinel_fails_safe_when_a_root_goes_unreadable(tmp_path):
    eng, core = _two(tmp_path)
    s = bs.CodeCurrencySentinel(
        package_dir=eng, core_dir=core, reconcile=lambda: (_ for _ in ()).throw(AssertionError("must not fire on an unprovable stamp"))
    )
    s._core_dir = str(tmp_path / "gone")
    assert s.current_on_disk_stamp() is None
    assert s.check_once() is False


def test_the_package_halves_are_what_the_build_info_reports(tmp_path):
    from factlattice_engine import callcli

    eng, core = _two(tmp_path)
    info = callcli.build_info(eng, core)
    assert info["source_sha256"] == bs.build_stamp(eng, core)
    assert info["engine_sha256"] == bs.package_stamp(eng, bs.ENGINE_PACKAGE)
    assert info["core_sha256"] == bs.package_stamp(core, bs.CORE_PACKAGE)
    assert len({info["source_sha256"], info["engine_sha256"], info["core_sha256"]}) == 3


# ---- a stamp that cannot be read: logged, never a path, and the sentinel recovers ---------------

_NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")


def _unreadable_core(tmp_path):
    eng, core = _two(tmp_path)
    victim = pathlib.Path(core) / "locked.py"
    victim.write_text("L = 1\n")
    victim.chmod(0)
    return eng, core, victim


@_NOT_ROOT
def test_a_sentinel_started_blind_logs_adopts_a_baseline_when_it_reads_and_then_fires(tmp_path, caplog):
    import logging

    eng, core, victim = _unreadable_core(tmp_path)
    fired = []
    try:
        with caplog.at_level(logging.INFO, logger="factlattice_engine.build_stamp"):
            s = bs.CodeCurrencySentinel(package_dir=eng, core_dir=core, reconcile=lambda: fired.append(1))
            assert s.seated_stamp is None
            start_msgs = [r.getMessage() for r in caplog.records]
            assert len(start_msgs) == 1 and "PermissionError" in start_msgs[0] and "at start" in start_msgs[0], start_msgs
            assert str(tmp_path) not in start_msgs[0], "the log line names a path"
            caplog.clear()
            for _ in range(3):  # still unreadable: no repeat of the same state
                assert s.check_once() is False
            assert caplog.records == [], [r.getMessage() for r in caplog.records]
            victim.chmod(0o644)  # someone fixes it
            assert s.check_once() is False and fired == []  # adopts the read; nothing to compare yet
            assert s.seated_stamp is not None
            assert any("adopting it as the baseline" in r.getMessage() for r in caplog.records)
            (pathlib.Path(core) / "mod.py").write_text("C = 2  # a core fix, after the recovery\n")
            assert s.check_once() is True and fired == [1]
    finally:
        victim.chmod(0o644)


@_NOT_ROOT
def test_a_sentinel_that_goes_blind_after_seating_logs_once_and_fires_after_it_recovers(tmp_path, caplog):
    import logging

    eng, core = _two(tmp_path)
    victim = pathlib.Path(core) / "locked.py"
    victim.write_text("L = 1\n")
    fired = []
    s = bs.CodeCurrencySentinel(package_dir=eng, core_dir=core, reconcile=lambda: fired.append(1))
    seated = s.seated_stamp
    assert seated
    try:
        victim.chmod(0)
        with caplog.at_level(logging.INFO, logger="factlattice_engine.build_stamp"):
            assert [s.check_once() for _ in range(3)] == [False, False, False]
        msgs = [r.getMessage() for r in caplog.records]
        assert len(msgs) == 1 and "PermissionError" in msgs[0] and str(tmp_path) not in msgs[0], msgs
        victim.chmod(0o644)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="factlattice_engine.build_stamp"):
            assert s.check_once() is False and s.seated_stamp == seated  # same bytes: no fire, baseline kept
        assert any("again" in r.getMessage() for r in caplog.records)
        (pathlib.Path(core) / "mod.py").write_text("C = 3\n")
        assert s.check_once() is True and fired == [1]
    finally:
        victim.chmod(0o644)


def test_a_missing_root_is_logged_by_class_name_only(tmp_path, caplog):
    import logging

    eng, core = _two(tmp_path)
    gone = str(tmp_path / "gone-dir-name")
    with caplog.at_level(logging.INFO, logger="factlattice_engine.build_stamp"):
        bs.CodeCurrencySentinel(package_dir=eng, core_dir=gone, reconcile=lambda: None)
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 1 and "FileNotFoundError" in msgs[0] and "gone-dir-name" not in msgs[0], msgs


def test_an_editor_lock_file_and_a_dangling_symlink_are_not_source(tmp_path):
    eng, core = _two(tmp_path)
    clean = bs.build_stamp(eng, core)
    assert clean
    os.symlink("nobody@host.1234:1700000000", os.path.join(core, ".#mod.py"))  # an Emacs lock file
    os.symlink(str(tmp_path / "no-such-target.py"), os.path.join(core, "dangling.py"))
    assert bs.build_stamp(eng, core) == clean, "a lock file or a dangling link produced None or moved the stamp"
    (pathlib.Path(core) / "mod.py").write_text("C = 9\n")  # real source still counts
    assert bs.build_stamp(eng, core) not in (None, clean)


# ------------------------------------------------------------------ round 3: the list drives the roots
def test_every_listed_package_is_a_walked_root_and_an_unlisted_one_is_not(tmp_path, monkeypatch):
    """STAMP_PACKAGES is the list of roots the stamp walks, not a declaration beside it: a package
    added to the tuple moves the stamp when its source changes, and the same package left off the
    tuple does not. A third package is the case the SDLC rule [BUILD-STAMP-ROOTS] is for."""
    name = "stamp_extra_pkg"
    pkg = tmp_path / "extra" / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "extra"))
    monkeypatch.delitem(sys.modules, name, raising=False)
    try:
        listed = bs.STAMP_PACKAGES + (name,)
        before_unlisted = bs.build_stamp()
        monkeypatch.setattr(bs, "STAMP_PACKAGES", listed)
        before_listed = bs.build_stamp()
        assert before_listed is not None and before_listed != before_unlisted
        (pkg / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
        assert bs.build_stamp() != before_listed  # listed: its change moves the stamp
        monkeypatch.setattr(bs, "STAMP_PACKAGES", listed[:-1])
        assert bs.build_stamp() == before_unlisted  # unlisted: its change is not seen
    finally:
        sys.modules.pop(name, None)


def test_dropping_a_package_from_the_list_drops_it_from_the_stamp(monkeypatch):
    full = bs.build_stamp()
    monkeypatch.setattr(bs, "STAMP_PACKAGES", (bs.ENGINE_PACKAGE,))
    engine_only = bs.build_stamp()
    assert full is not None and engine_only is not None and engine_only != full
    assert engine_only == bs.package_stamp(str(ENGINE_DIR), bs.ENGINE_PACKAGE)


def test_an_editor_lock_file_is_skipped_even_when_it_is_a_regular_file(tree):
    eng, core = str(tree / "factlattice_engine"), str(tree / "factlattice")
    base = bs.build_stamp(eng, core)
    (tree / "factlattice" / ".#parser.py").write_text("not source\n", encoding="utf-8")
    assert bs.build_stamp(eng, core) == base  # `.#` name, a regular file: skipped
    (tree / "factlattice" / "zz_control.py").write_text("X = 1\n", encoding="utf-8")
    assert bs.build_stamp(eng, core) != base  # control: an ordinary new file moves it


def test_package_stamp_returns_none_on_an_unencodable_name(monkeypatch):
    def boom(root, prefix):
        raise ValueError("surrogates not allowed")

    monkeypatch.setattr(bs, "_source_records", boom)
    assert bs.package_stamp(str(ENGINE_DIR), bs.ENGINE_PACKAGE) is None
