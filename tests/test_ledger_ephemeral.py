"""(e) A write whose target REALPATH is under the OS temp dir is EPHEMERAL: no ledger record, no blob,
and the receipt says so (`ledger: {status: "skipped", reason: ...}`), never silently.

The suite's autouse fixture sets FACTLATTICE_LEDGER_EPHEMERAL_ROOTS="" (nothing ephemeral) so the
other ledger tests, whose files live under pytest's tmp_path (inside the OS temp dir), keep testing
the ledger. These tests undo that override where they test the default."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from factlattice import ledger
from factlattice.authoring.edit import set_property, write_whole_file
from tests.test_node_change_ledger import DOC, _recs, _wd_env, env  # noqa: F401

REPO = Path(__file__).resolve().parents[1]
SKIPPED = {"status": "skipped", "reason": "ledger-skipped-ephemeral"}
ENVVAR = "FACTLATTICE_LEDGER_EPHEMERAL_ROOTS"


@pytest.fixture()
def default_roots(monkeypatch):
    """The production default: the OS temp dir (not this session's re-pointed one), /tmp, /private/tmp."""
    monkeypatch.delenv(ENVVAR, raising=False)
    monkeypatch.setattr(tempfile, "tempdir", None)
    return tempfile.gettempdir()


def _os_temp_unresolved(default_roots) -> str:
    return default_roots


# ---------------------------------------------------------------------------- is_ephemeral
def test_is_ephemeral_is_a_module_level_function_and_default_covers_the_os_temp_dir(default_roots):
    assert ledger.is_ephemeral(os.path.join(default_roots, "x.lts")) is True
    # a fixed system path is never temp, wherever the repo is checked out (trials run clones under /private/tmp)
    assert ledger.is_ephemeral("/usr/lib/fltest-never-temp.lts") is False


@pytest.mark.parametrize("spelling", ["unresolved", "resolved"])
def test_var_folders_spellings_of_the_same_temp_file_are_both_ephemeral(default_roots, spelling):
    p = os.path.join(default_roots, "x.lts")
    assert ledger.is_ephemeral(p if spelling == "unresolved" else os.path.realpath(p))


@pytest.mark.parametrize("p", ["/tmp/x.lts", "/private/tmp/x.lts"])
def test_tmp_and_private_tmp_spellings_are_both_ephemeral(default_roots, p):
    assert ledger.is_ephemeral(p)


def test_no_string_prefix_matching(default_roots):
    assert not ledger.is_ephemeral("/tmpfoo/x.lts") and not ledger.is_ephemeral("/private/tmpfoo/x.lts")
    assert not ledger.is_ephemeral(default_roots + "foo/x.lts")


def test_a_symlink_in_temp_pointing_at_a_non_temp_file_is_NOT_ephemeral(default_roots, tmp_path):
    link = Path(default_roots) / f"fltest-link-{os.getpid()}.lts"
    try:
        link.symlink_to("/usr/lib/fltest-never-temp.lts")                       # need not exist: realpath resolves the link text
        assert not ledger.is_ephemeral(str(link))             # realpath is outside tmp
    finally:
        link.unlink()


def test_a_symlink_outside_temp_pointing_at_a_temp_file_IS_ephemeral(default_roots, tmp_path):
    target = Path(default_roots) / f"fltest-target-{os.getpid()}.lts"
    link = REPO / f"_fltest-link-{os.getpid()}.lts"
    try:
        target.write_text("x")
        link.symlink_to(target)
        assert ledger.is_ephemeral(str(link))                 # realpath is inside tmp
    finally:
        link.unlink()
        target.unlink()


def test_override_env_replaces_the_roots_and_empty_means_nothing_is_ephemeral(monkeypatch, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setenv(ENVVAR, f"{a}{os.pathsep}{b}")
    assert ledger.is_ephemeral(str(a / "x.lts")) and ledger.is_ephemeral(str(b / "d" / "x.lts"))
    assert not ledger.is_ephemeral(str(tmp_path / "other" / "x.lts"))
    assert not ledger.is_ephemeral("/tmp/x.lts")               # the override REPLACES the defaults
    monkeypatch.setenv(ENVVAR, "")
    assert not ledger.is_ephemeral("/tmp/x.lts") and not ledger.is_ephemeral(str(a / "x.lts"))


# ---------------------------------------------------------------------------- the writers
def test_typed_write_to_a_temp_file_is_skipped_loudly(env, default_roots, tmp_path):
    r = set_property(env, "LG-ONE", "k0", "v0")
    assert "k0" in env.read_text()                              # the write itself landed
    assert r.ledger == SKIPPED
    assert not (tmp_path / "state" / "ledger").exists()


def test_adapter_receipts_carry_the_skip(env, default_roots):
    from factlattice_engine import authoring
    for mode in ("compact", "full"):
        out = authoring.add_property(str(env), "LG-ONE", f"k{mode}", "v", receipt=mode)
        assert out["written"] and out["ledger"] == SKIPPED


def test_whole_file_write_to_a_temp_file_stores_no_record_and_no_blob(env, default_roots, tmp_path):
    out = write_whole_file(env, DOC + "\n- z: 1\n")
    assert out.ledger == SKIPPED
    assert not (tmp_path / "state" / "ledger").exists() and not (tmp_path / "state" / "ledger-blobs").exists()


def test_write_document_returns_the_skip(env, default_roots, tmp_path):
    write_document, _doc_obj, _fake_preview = _wd_env()
    out = write_document(_doc_obj(), str(tmp_path / "corpus" / "w.lts"), _fake_preview)
    assert out["written"] and out["ledger"] == SKIPPED


def test_a_non_ephemeral_path_is_ledgered(env, monkeypatch, tmp_path):
    monkeypatch.setenv(ENVVAR, str(tmp_path / "some-other-root"))   # tmp_path/corpus is NOT under it
    r = set_property(env, "LG-ONE", "k0", "v0")
    assert r.ledger["status"] == "ok" and len(_recs(env)) == 1


def test_a_non_temp_file_reached_through_a_temp_symlink_is_ledgered(env, monkeypatch, tmp_path):
    eph = tmp_path / "eph"
    eph.mkdir()
    monkeypatch.setenv(ENVVAR, str(eph))
    link = eph / "c.lts"
    link.symlink_to(env)                                        # real file is outside the ephemeral root
    r = set_property(link, "LG-ONE", "k0", "v0")
    assert r.ledger["status"] == "ok"


def test_the_suite_override_keeps_the_ledger_exercised(env):
    """The autouse fixture sets the override to empty: a write under pytest's tmp_path IS ledgered."""
    assert os.environ[ENVVAR] == ""
    r = set_property(env, "LG-ONE", "k0", "v0")
    assert r.ledger["status"] == "ok" and len(_recs(env)) == 1


def test_docs_carry_the_ephemeral_rule():
    spec = (REPO / "SPEC.md").read_text()
    i = spec.index("#### 4.6.4")
    assert "EPHEMERAL" in spec[i:spec.index("### 4.7", i)] and "is_ephemeral" in spec[i:spec.index("### 4.7", i)]
    sdlc = (REPO / "SDLC.md").read_text()
    assert "LEDGER-SCRATCH-COPIES" in sdlc and "dry_run" in sdlc
