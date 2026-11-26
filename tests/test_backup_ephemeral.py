"""Writer backups: NO backup for a file under an ephemeral root.

The rule: `_write` makes no pre-edit copy when the target's realpath is under an ephemeral root, decided by the LEDGER's own
predicate (`ledger.is_ephemeral`, the one that makes the node-change ledger skip the same write). A scratch file is deleted with
its directory, so its backup is nobody's undo record, and it outlives it (a backup folder reached 94 GB this way).
The receipt says so: `backup_path` is null and `backup` is {"status": "skipped", "reason": "backup-skipped-ephemeral"}; a receipt
for a write that DID back up carries no `backup` key. `FACTLATTICE_BACKUP_EPHEMERAL=keep` restores the backup for a scratch file.

The suite's autouse fixture sets FACTLATTICE_LEDGER_EPHEMERAL_ROOTS="" (nothing ephemeral). These tests name their own
ephemeral root through that same variable (the predicate's documented override), so tmp_path files are or are not
ephemeral by the ledger's own rule; one test uses the production default (the OS temp dir) for real.

Every test points FACTLATTICE_BACKUP_DIR and FACTLATTICE_STATE_DIR at its own tmp_path.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from factlattice import ledger
from factlattice.authoring.edit import add_node, remove_property, rename_property, repair_property, set_property, write_whole_file
from factlattice_engine import authoring

SKIP = {"status": "skipped", "reason": "backup-skipped-ephemeral"}
LEDGER_SKIP = {"status": "skipped", "reason": "ledger-skipped-ephemeral"}
DOC = "# Doc [SPEC:BE-ROOT]\n\n## Node [BE-N]\n\n- kind: x\n- old-key: v\n- ⛔ FLAGGED: broken\n"
BAD_LINE = 7  # the malformed `- ⛔ FLAGGED: broken` line


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    monkeypatch.delenv("FACTLATTICE_BACKUP_KEEP", raising=False)
    monkeypatch.delenv("FACTLATTICE_BACKUP_EPHEMERAL", raising=False)
    (tmp_path / "eph").mkdir()
    (tmp_path / "keep").mkdir()
    # the ledger's own override: ONLY tmp_path/eph is an ephemeral root
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", str(tmp_path / "eph"))
    return tmp_path


def _file(d: Path, name: str = "a.lts") -> Path:
    f = d / name
    f.write_text(DOC, encoding="utf-8")
    return f


def _baks(env_dir: Path) -> list[str]:
    return sorted(p.name for p in (env_dir / "bak").iterdir()) if (env_dir / "bak").exists() else []


# ---- the writers: library (every one of the five typed writers, and the whole-file writer) ---------------------------
WRITERS = {
    "set_property": lambda f: set_property(f, "BE-N", "k1", "v1"),
    "repair_property": lambda f: repair_property(f, "BE-N", BAD_LINE, "flagged"),
    "add_node": lambda f: add_node(f, "BE-M", "More", {"k2": "v2"}),
    "rename_property": lambda f: rename_property(f, "BE-N", "old-key", "new-key"),
    "remove_property": lambda f: remove_property(f, "BE-N", "old-key"),
}


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_a_write_under_an_ephemeral_root_makes_no_backup_and_the_receipt_says_why(env, name):
    f = _file(env / "eph")
    before = f.read_text(encoding="utf-8")
    r = WRITERS[name](f)
    assert _baks(env) == [], f"{name}: a backup was written for an ephemeral file: {_baks(env)}"
    assert r.backup_path is None, r.backup_path
    assert r.backup == SKIP, r.backup
    assert f.read_text(encoding="utf-8") != before  # the write itself is unchanged
    assert r.ledger == LEDGER_SKIP  # and the ledger skipped the same write


def test_write_whole_file_over_an_existing_ephemeral_file_makes_no_backup(env):
    f = _file(env / "eph")
    out = write_whole_file(f, DOC + "- extra: 1\n")
    assert _baks(env) == [] and out.backup == SKIP and out.ledger == LEDGER_SKIP
    assert "extra" in f.read_text(encoding="utf-8")


def test_a_write_outside_any_ephemeral_root_still_backs_up_and_its_receipt_has_no_backup_key(env):
    f = _file(env / "keep")
    r = set_property(f, "BE-N", "k1", "v1")
    assert r.backup is None and r.backup_path is not None
    assert [Path(r.backup_path).name] == _baks(env)
    assert Path(r.backup_path).read_text(encoding="utf-8") == DOC  # the backup is the pre-edit file
    d = authoring.add_property(str(f), "BE-N", "k2", "v2", store=None)
    assert "backup" not in d and d["backup_path"], d
    full = authoring.add_property(str(f), "BE-N", "k3", "v3", store=None, receipt="full")
    assert "backup" not in full and full["backup_path"] and f"backup {full['backup_path']}" in full["report"]


def test_the_retention_cap_still_applies_outside_an_ephemeral_root(env, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_BACKUP_KEEP", "3")
    f = _file(env / "keep")
    made = [Path(set_property(f, "BE-N", f"k{i}", f"v{i}").backup_path).name for i in range(8)]
    assert _baks(env) == sorted(made[-3:])


def test_an_ephemeral_write_prunes_nothing_and_never_touches_another_files_backups(env, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_BACKUP_KEEP", "1")
    kept = _file(env / "keep")
    set_property(kept, "BE-N", "k0", "v0")
    set_property(kept, "BE-N", "k1", "v1")
    mine = _baks(env)
    assert len(mine) == 1
    e = _file(env / "eph")
    for i in range(3):
        set_property(e, "BE-N", f"e{i}", "v")
    assert _baks(env) == mine


# ---- the predicate is the ledger's own --------------------------------------------------------------------------------
def test_the_backup_decision_and_the_ledger_decision_agree_for_a_temp_path_and_a_non_temp_path(env):
    cases = {"eph": True, "keep": False}
    for sub, expect in cases.items():
        f = _file(env / sub, f"agree-{sub}.lts")
        n0 = len(_baks(env))
        r = set_property(f, "BE-N", "k", "v")
        made = len(_baks(env)) - n0
        # the ledger's predicate, asked directly and independently of the receipt fields
        assert ledger.is_ephemeral(f) is expect
        assert (made == 0) is ledger.is_ephemeral(f), (sub, made)
        # and the two receipts agree with each other
        assert (r.backup == SKIP) is (r.ledger == LEDGER_SKIP) is expect


def test_the_backup_path_reuses_the_ledger_predicate_not_a_copy_of_it(env, monkeypatch):
    """Negative control: make the ledger's predicate say the opposite and the backup decision follows it."""
    f = _file(env / "keep")
    monkeypatch.setattr(ledger, "is_ephemeral", lambda p, roots=None: True)
    assert set_property(f, "BE-N", "k", "v").backup == SKIP and _baks(env) == []


def test_the_production_default_roots_the_os_temp_dir_skip_the_backup_for_real(env, monkeypatch):
    monkeypatch.delenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS")  # the production default: the OS temp dir, /tmp
    with tempfile.TemporaryDirectory(prefix="fltest-be-", dir=tempfile.gettempdir()) as d:
        f = _file(Path(d))
        assert ledger.is_ephemeral(f)
        r = set_property(f, "BE-N", "k", "v")
        assert r.backup == SKIP and r.backup_path is None and _baks(env) == []


# ---- the opt-in ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("val", ["keep", "KEEP", " keep "])
def test_the_opt_in_restores_the_backup_for_an_ephemeral_file(env, monkeypatch, val):
    monkeypatch.setenv("FACTLATTICE_BACKUP_EPHEMERAL", val)
    f = _file(env / "eph")
    r = set_property(f, "BE-N", "k", "v")
    assert r.backup is None and r.backup_path and Path(r.backup_path).read_text(encoding="utf-8") == DOC
    assert _baks(env) == [Path(r.backup_path).name]
    assert r.ledger == LEDGER_SKIP  # the opt-in changes the backup only, never the ledger


@pytest.mark.parametrize("val", ["", "0", "no", "all", "true"])
def test_any_other_value_of_the_opt_in_is_the_default_skip(env, monkeypatch, val):
    monkeypatch.setenv("FACTLATTICE_BACKUP_EPHEMERAL", val)
    assert set_property(_file(env / "eph"), "BE-N", "k", "v").backup == SKIP and _baks(env) == []


def test_a_retention_cap_applies_to_opted_in_ephemeral_backups_too(env, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_BACKUP_EPHEMERAL", "keep")
    monkeypatch.setenv("FACTLATTICE_BACKUP_KEEP", "2")
    f = _file(env / "eph")
    for i in range(5):
        set_property(f, "BE-N", f"k{i}", "v")
    assert len(_baks(env)) == 2


# ---- the engine adapter: compact and full receipts, text report, every writer -------------------------------------
@pytest.mark.parametrize("rcpt", ["compact", "full"])
@pytest.mark.parametrize("name", ["add_property", "add_node", "repair_property", "rename_property", "remove_property"])
def test_every_adapter_receipt_names_the_skip_plainly(env, name, rcpt):
    f = _file(env / "eph")
    fn = {
        "add_property": authoring.add_property,
        "add_node": authoring.add_node,
        "repair_property": authoring.repair_property,
        "rename_property": authoring.rename_property,
        "remove_property": authoring.remove_property,
    }[name]
    args = {
        "add_property": (str(f), "BE-N", "k1", "v1"),
        "add_node": (str(f), "BE-M", "More", {"k2": "v2"}),
        "repair_property": (str(f), "BE-N", BAD_LINE, "flagged"),
        "rename_property": (str(f), "BE-N", "old-key", "new-key"),
        "remove_property": (str(f), "BE-N", "old-key"),
    }[name]
    out = fn(*args, store=None, receipt=rcpt)
    assert out["written"] is True, out
    assert out["backup_path"] is None and out["backup"] == SKIP, {k: out.get(k) for k in ("backup_path", "backup")}
    assert _baks(env) == []
    if rcpt == "full":
        assert "no backup: the file is under an ephemeral root" in out["report"], out["report"][-300:]
        assert "backup None" not in out["report"]


def test_every_writer_tool_description_names_the_ephemeral_skip_and_the_opt_in():
    from factlattice_engine import server

    names = ("lts_add_property", "lts_add_node", "lts_repair_property", "lts_rename_property", "lts_remove_property")
    descs = {t["name"]: t["description"] for t in server.TOOLS if t["name"] in names}
    assert set(descs) == set(names)
    for n, d in descs.items():
        assert "NO BACKUP UNDER AN EPHEMERAL ROOT" in d and "FACTLATTICE_BACKUP_EPHEMERAL=keep" in d, n
        assert "backup-skipped-ephemeral" in d, n


def test_sdlc_has_the_row_naming_the_ledger_predicate():
    text = (Path(__file__).resolve().parents[1] / "SDLC.md").read_text(encoding="utf-8")
    row = [ln for ln in text.splitlines() if "[WRITER-BACKUP-SKIPS-LEDGER-EPHEMERAL]" in ln]
    assert len(row) == 1 and "ledger.is_ephemeral" in row[0] and "FACTLATTICE_BACKUP_EPHEMERAL=keep" in row[0]


def test_a_file_outside_temp_whose_roots_state_is_under_temp_keeps_its_backup_while_the_ledger_skips(env, monkeypatch):
    """The reverse of the agreement does NOT hold: the ledger also skips when the named root's state folder is under an
    ephemeral root (`Location.ephemeral_state`). The file itself is not ephemeral, so it keeps its backup. The location is
    the real `locate` answer with that one flag set (a registry-driven state is covered by the ledger's own tests)."""
    import dataclasses

    orig = ledger.locate
    monkeypatch.setattr(ledger, "locate", lambda real, **kw: dataclasses.replace(orig(real, **kw), ephemeral_state=True))
    f = _file(env / "keep")
    assert ledger.is_ephemeral(f) is False
    r = set_property(f, "BE-N", "k", "v")
    assert r.ledger == LEDGER_SKIP, r.ledger
    assert r.backup is None and r.backup_path and len(_baks(env)) == 1
