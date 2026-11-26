"""Writer backup retention: `_write` keeps the newest N backups of THE SOURCE FILE it just wrote.

Before this, `$TMPDIR/factlattice-backups` (or `FACTLATTICE_BACKUP_DIR`) was never pruned: one heavily written file
left 4100 full copies in 2 days. The rule under test: after a successful write, prune that source's older backups
down to N (`FACTLATTICE_BACKUP_KEEP`, default 20, `all` = no prune); never the backup this write made; never another
source's; a prune failure is a receipt warning, never a write failure.

Every test points FACTLATTICE_BACKUP_DIR and FACTLATTICE_STATE_DIR at its own tmp_path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from factlattice.authoring import edit
from factlattice.authoring.edit import set_property

DEFAULT_KEEP = 20


def _doc() -> str:
    return "# Doc [SPEC:BR-ROOT]\n\n## Node [BR-N]\n\n- kind: x\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("FACTLATTICE_BACKUP_KEEP", raising=False)
    (tmp_path / "corpus").mkdir()
    return tmp_path


def _file(env_dir: Path, name: str = "a.lts", sub: str = "corpus") -> Path:
    d = env_dir / sub
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_text(_doc(), encoding="utf-8")
    return f


def _write(f: Path, i: int):
    return set_property(f, "BR-N", f"k{i}", f"v{i}")


def _baks(env_dir: Path) -> list[str]:
    return sorted(p.name for p in (env_dir / "bak").iterdir()) if (env_dir / "bak").exists() else []


def _mine(env_dir: Path, name: str) -> list[str]:
    """Backups whose name is `<name>.<stamp>...` with the stamp's digits: exact, never a prefix."""
    import re

    return [b for b in _baks(env_dir) if re.match(re.escape(name) + r"\.\d{8}T\d{6}\.\d{6}\.", b)]


def test_n_plus_5_writes_leave_exactly_n_backups_the_newest_including_the_last(env, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_BACKUP_KEEP", "3")
    f = _file(env)
    made = [Path(_write(f, i).backup_path).name for i in range(8)]  # N + 5 writes
    left = _mine(env, "a.lts")
    assert left == sorted(made[-3:]), (left, made)
    assert made[-1] in left


def test_default_keep_is_twenty(env):
    f = _file(env)
    made = [Path(_write(f, i).backup_path).name for i in range(DEFAULT_KEEP + 5)]
    assert _mine(env, "a.lts") == sorted(made[-DEFAULT_KEEP:])


def test_another_files_backups_are_untouched_including_a_shared_prefix(env, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_BACKUP_KEEP", "2")
    a = _file(env, "a.lts")
    old = _file(env, "a.lts.old.lts")  # its backup name starts with a.lts.
    ab = _file(env, "ab.lts")
    twin = _file(env, "a.lts", sub="elsewhere")  # same basename, other directory: another source
    for i in range(4):
        _write(old, i), _write(ab, i), _write(twin, i)
    keep_before = {n for n in _baks(env)}
    for i in range(6):
        _write(a, i)
    after = set(_baks(env))
    assert keep_before <= after, sorted(keep_before - after)  # nothing of the others went
    assert len(_mine(env, "a.lts")) == 2 + 2  # a.lts's own 2, plus the twin's 2: same name, other source
    assert len(_mine(env, "a.lts.old.lts")) == 2 and len(_mine(env, "ab.lts")) == 2


def test_keep_all_keeps_every_backup(env, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_BACKUP_KEEP", "all")
    f = _file(env)
    for i in range(DEFAULT_KEEP + 5):
        r = _write(f, i)
        assert not [w for w in r.warnings if "BACKUP_KEEP" in w]
    assert len(_mine(env, "a.lts")) == DEFAULT_KEEP + 5


@pytest.mark.parametrize("bad", ["abc", "-3", "2.5", "1e3", "all-of-it"])
def test_an_invalid_value_means_the_default_with_a_warning(env, monkeypatch, bad):
    monkeypatch.setenv("FACTLATTICE_BACKUP_KEEP", bad)
    f = _file(env)
    results = [_write(f, i) for i in range(DEFAULT_KEEP + 3)]
    assert len(_mine(env, "a.lts")) == DEFAULT_KEEP
    assert any("FACTLATTICE_BACKUP_KEEP" in w for w in results[-1].warnings), results[-1].warnings
    assert not any(bad in w for w in results[-1].warnings), results[-1].warnings  # the value is never echoed


@pytest.mark.parametrize("unset", [None, "", " ", "0", "00", "000"])
def test_zero_or_unset_means_the_default_without_a_warning(env, monkeypatch, unset):
    if unset is not None:
        monkeypatch.setenv("FACTLATTICE_BACKUP_KEEP", unset)
    f = _file(env)
    results = [_write(f, i) for i in range(DEFAULT_KEEP + 3)]
    assert len(_mine(env, "a.lts")) == DEFAULT_KEEP
    assert not any("BACKUP_KEEP" in w for r in results for w in r.warnings)


def test_a_prune_failure_is_a_warning_and_the_write_still_succeeds(env, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_BACKUP_KEEP", "2")
    f = _file(env)
    for i in range(4):
        _write(f, i)
    real_unlink = os.unlink

    def deny(p, *a, **k):
        if str(p).endswith(".bak"):
            raise PermissionError(13, "SECRET-DETAIL-NOT-FOR-THE-RECEIPT")
        return real_unlink(p, *a, **k)

    monkeypatch.setattr(os, "unlink", deny)
    r = _write(f, 99)
    assert r.warnings and any("PermissionError" in w for w in r.warnings), r.warnings
    assert not any("SECRET-DETAIL" in w for w in r.warnings)  # the error class only
    assert "k99" in f.read_text(encoding="utf-8")  # the write landed
    assert Path(r.backup_path).exists()
    assert len(_mine(env, "a.lts")) == 3  # nothing pruned: the 2 kept earlier + this one


def test_an_unreadable_backup_dir_during_prune_is_a_warning_not_a_failure(env, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_BACKUP_KEEP", "2")
    f = _file(env)
    real_scandir = os.scandir

    def deny(p="."):
        if str(p) == str(env / "bak"):
            raise PermissionError(13, "nope")
        return real_scandir(p)

    _write(f, 0)
    monkeypatch.setattr(os, "scandir", deny)
    r = _write(f, 1)
    assert any("PermissionError" in w for w in r.warnings), r.warnings
    assert "k1" in f.read_text(encoding="utf-8")


def test_a_vanished_backup_during_prune_is_not_an_error(env, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_BACKUP_KEEP", "1")
    f = _file(env)
    for i in range(3):
        _write(f, i)
    real_unlink = os.unlink

    def race(p, *a, **k):  # another pruner got there first
        if str(p).endswith(".bak"):
            real_unlink(p, *a, **k)
        return real_unlink(p, *a, **k)  # second delete raises FileNotFoundError

    monkeypatch.setattr(os, "unlink", race)
    r = _write(f, 3)
    assert not r.warnings, r.warnings
    assert len(_mine(env, "a.lts")) == 1


def test_the_backup_this_write_made_survives_even_when_its_stamp_ranks_oldest(env, monkeypatch):
    """N=1, and a stored backup of this source carries a LATER stamp (as after a clock step back): this write's stamp
    ranks oldest by the name, and its own backup must still survive. Only that is claimed: ranking is by the stamp in
    the name, so after a step back the older-stamped backups of the repeated hour can be pruned first."""
    monkeypatch.setenv("FACTLATTICE_BACKUP_KEEP", "1")
    f = _file(env)
    r0 = _write(f, 0)
    # plant a backup of THIS source with a later stamp: it ranks newest by name, this write's ranks oldest
    future = Path(r0.backup_path).name.replace(".2026", ".2999")
    assert future != Path(r0.backup_path).name
    (env / "bak" / future).write_bytes(b"x")
    r1 = _write(f, 1)
    assert Path(r1.backup_path).exists()
    assert Path(r1.backup_path).name in _baks(env)


@pytest.mark.parametrize("extra", [".keep", ".bak", "~", ".1", ".bak.keep", ".0"])
def test_files_whose_names_extend_a_real_backup_name_are_never_pruned(env, monkeypatch, extra):
    """Pins `fullmatch` in `_prune_backups`: a name that merely STARTS like a backup of this source is not one."""
    monkeypatch.setenv("FACTLATTICE_BACKUP_KEEP", "1")
    f = _file(env)
    real = Path(_write(f, 0).backup_path).name
    planted = real + extra
    (env / "bak" / planted).write_bytes(b"not a backup")
    for i in range(1, 4):
        _write(f, i)
    assert planted in _baks(env)
    assert len(_mine(env, "a.lts")) == 2  # 1 real kept + the planted file (its name still starts like one)


def test_n_equal_one_keeps_exactly_the_last_backup(env, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_BACKUP_KEEP", "1")
    f = _file(env)
    for i in range(5):
        r = _write(f, i)
    assert _mine(env, "a.lts") == [Path(r.backup_path).name]


def test_every_writer_tool_description_names_the_pruning_and_the_variable():
    from factlattice_engine import server

    names = ("lts_add_property", "lts_add_node", "lts_repair_property", "lts_rename_property", "lts_remove_property")
    descs = {t["name"]: t["description"] for t in server.TOOLS if t["name"] in names}
    assert set(descs) == set(names)
    for n, d in descs.items():
        assert "FACTLATTICE_BACKUP_KEEP" in d and "newest N backups" in d, n
