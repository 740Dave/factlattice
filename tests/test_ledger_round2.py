"""review round 2 (PASS_WITH_NOTES at 3bd948c): F1 blob failure keeps the record and says so;
F2 a registered corpus root must not be ephemeral; F3/F6 doc wording."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from factlattice import ledger
from factlattice.authoring.edit import set_property, write_whole_file
from tests.test_node_change_ledger import DOC, _recs, _wd_env, env  # noqa: F401

REPO = Path(__file__).resolve().parents[1]
MSG = "WRITTEN AND LEDGERED, BUT THE PRIOR CONTENT WAS NOT PRESERVED"


def _break_blobs(monkeypatch):
    def boom(*a, **k):
        raise ledger.LedgerError("prior-content blob not stored: PermissionError: forced")
    monkeypatch.setattr(ledger, "store_blob", boom)


def _ledger_files(tmp_path):
    d = tmp_path / "state" / "ledger"
    return sorted(d.glob("*.jsonl")) if d.exists() else []


# ------------------------------------------------------------------------------------- F1
def test_f1_first_ever_write_with_a_blob_failure_still_records_with_the_gap_flagged(env, monkeypatch, tmp_path):
    _break_blobs(monkeypatch)
    out = write_whole_file(env, DOC + "\n- z: 1\n")
    (rec,) = _recs(env)                                   # exactly one record, on the file's FIRST write
    assert rec["prior_blob"] is None and "forced" in rec["undo_evidence_missing"]
    assert rec["ledger_seq"] == 1
    assert out.ledger["status"] == "ok_without_undo" and "forced" in out.ledger["live_detail"]
    assert out.ledger["seq"] == 1
    (f,) = _ledger_files(tmp_path)
    assert f.stat().st_size > 0


def test_f1_the_gap_lives_in_the_chain_not_only_in_one_receipt(env, monkeypatch, tmp_path):
    _break_blobs(monkeypatch)
    write_whole_file(env, DOC + "\n- z: 1\n")
    monkeypatch.undo()
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    write_whole_file(env, DOC + "\n- z: 2\n")
    first, second = _recs(env)
    assert first["undo_evidence_missing"] and second["undo_evidence_missing"] is None
    assert second["prior_blob"] and second["follows_out_of_band_edit"] is False   # chain intact


def test_f1_write_document_warns_loudly(env, monkeypatch, tmp_path):
    write_document, _doc_obj, _fake_preview = _wd_env()
    f = tmp_path / "corpus" / "w.lts"
    write_document(_doc_obj(), str(f), _fake_preview)
    _break_blobs(monkeypatch)
    out = write_document(_doc_obj(), str(f), _fake_preview)
    assert out["written"] and out["ledger"]["status"] == "ok_without_undo"
    assert any(MSG in w for w in out["warnings"])


def test_f1_a_clean_whole_file_write_has_no_gap_flag_and_status_ok(env, tmp_path):
    out = write_whole_file(env, DOC + "\n- z: 1\n")
    assert out.ledger["status"] == "ok" and _recs(env)[0]["undo_evidence_missing"] is None


def test_f1_no_empty_ledger_file_in_any_failure_path(env, monkeypatch, tmp_path):
    # path 1: the chain read refuses on a fresh file -> failed, and nothing is left behind
    def torn(fd):
        raise ledger.LedgerError("forced chain-read failure")
    monkeypatch.setattr(ledger, "_last_line", torn)
    r = set_property(env, "LG-ONE", "k0", "v0")
    assert r.ledger["status"] == "failed"
    assert _ledger_files(tmp_path) == []
    monkeypatch.undo()
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    # path 2: a registered-root refusal creates no file either
    r2 = set_property(env, "LG-ONE", "k1", "v1", ledger_roots=[str(tmp_path / "state")])
    assert r2.ledger["status"] == "failed"
    assert _ledger_files(tmp_path) == []
    # and no zero-byte ledger file anywhere under the state dir
    assert not [p for p in (tmp_path / "state").rglob("*.jsonl") if p.stat().st_size == 0]


# ------------------------------------------------------------------------------------- F2
def _engine(tmp_path):
    from factlattice_engine import config
    from factlattice_engine.tools import LtsEngine
    return LtsEngine(config.build_store({}))


@pytest.fixture(autouse=True)
def _os_tempdir(request, monkeypatch):
    """The session re-points tempfile.tempdir under pytest's basetemp (conftest); the F2 tests need the
    production default (the real OS temp dir)."""
    if request.node.name.startswith("test_f2"):
        import tempfile
        monkeypatch.setattr(tempfile, "tempdir", None)


def _scratch(tmp_path):
    root = tmp_path / "scratch"
    root.mkdir()
    (root / "a.lts").write_text(DOC)
    return root


def test_f2_an_ephemeral_root_registers_with_the_warning(tmp_path):
    root = _scratch(tmp_path)
    eng = _engine(tmp_path)
    res = eng.register_corpus("scratchy", spec={"root": str(root), "domain_suffix": "proj.s"})
    assert res["success"] is True and "scratchy" in eng.store.corpus_ids()
    (w,) = res["warnings"]
    assert w.startswith("EPHEMERAL ROOT: ") and str(root) in w or os.path.realpath(str(root)) in w
    assert "resolves under the OS temp dir; writes there are NOT ledgered (no history, no undo record)" in w


def test_f2_the_ledger_test_override_does_not_hide_the_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    res = _engine(tmp_path).register_corpus("scratchy", spec={"root": str(_scratch(tmp_path)),
                                                              "domain_suffix": "proj.s"})
    assert res["success"] is True and res["warnings"]


def test_f2_multi_root_spec_and_symlinked_root_warn(tmp_path):
    root = _scratch(tmp_path)
    link = REPO / f"_fl-root-link-{os.getpid()}"
    try:
        link.symlink_to(root)
        for i, spec in enumerate(({"roots": [{"path": str(root), "area": "specs"}], "domain_suffix": "proj.s"},
                                  {"root": str(link), "domain_suffix": "proj.s"})):
            res = _engine(tmp_path).register_corpus(f"scratchy{i}", spec=spec)
            assert res["success"] is True and len(res["warnings"]) == 1
    finally:
        link.unlink()


def test_f2_a_non_temp_root_registers_with_no_warnings_key(tmp_path, monkeypatch):
    # location-independent: inject the default ephemeral roots, so the test holds even when the
    # checkout itself lives under the OS temp dir (every agent scratchpad is under /private/tmp)
    monkeypatch.setattr(ledger, "default_ephemeral_roots", lambda: [str(tmp_path / "fake-os-temp")])
    root = tmp_path / "real-corpus"
    root.mkdir()
    (root / "a.lts").write_text(DOC)
    res = _engine(tmp_path).register_corpus("real", spec={"root": str(root), "domain_suffix": "proj.r"})
    assert res["success"] is True and "warnings" not in res


def test_f2_the_allow_hook_is_gone():
    assert "FACTLATTICE_ALLOW_EPHEMERAL_CORPUS_ROOTS" not in os.environ
    for rel in ("tests/conftest.py", "factlattice_engine/tools.py", "SPEC.md", "SDLC.md"):
        assert "FACTLATTICE_ALLOW_EPHEMERAL_CORPUS_ROOTS" not in (REPO / rel).read_text(), rel


# ------------------------------------------------------------------------------- F2/F3/F6 docs
def test_docs_f2_f3_f6():
    sdlc = (REPO / "SDLC.md").read_text()
    assert ("A PERMANENT corpus root (corpus-config.json or a long-lived registration) must not resolve under "
            "the OS temp dir: its writes would be unledgered. A temp sample corpus may be registered; its "
            "registration response warns that its writes are unledgered.") in sdlc
    assert "once the writers gain dry_run (design note v1.1 D3; not yet built)" in sdlc
    spec = (REPO / "SPEC.md").read_text()
    i = spec.index("#### 4.6.4")
    sect = spec[i:spec.index("### 4.7", i)]
    for needle in ("TEST hook", "disables their ledger", "ok_without_undo", "undo_evidence_missing",
                   "EPHEMERAL ROOT"):
        assert needle in sect
    assert "TEST hook" in sdlc and "disables their ledger" in sdlc


# ------------------------------------------------------------------- delta-review F-A
def test_fa_unlink_never_removes_a_ledger_that_gained_bytes(tmp_path, monkeypatch):
    """Delta review F-A: the no-empty-file cleanup must not delete a ledger file that is non-empty,
    even if a caller broke the lock contract and another appender wrote into it meanwhile."""
    state = tmp_path / "state2"
    (tmp_path / "corpus").mkdir()
    real = tmp_path / "corpus" / "x.lts"                # the ledger must live OUTSIDE the file's dir
    real.write_text("# D [DOC-X]\n")
    lp = ledger.ledger_path(os.path.realpath(real), state)
    real_write = os.write

    def interloper(fd, data):
        if data.startswith(b'{"'):                      # the ledger's record line
            with open(lp, "ab") as fh:                   # another appender, outside any lock
                fh.write(b'{"interloper":1}\n')
            raise OSError(28, "forced ENOSPC")
        return real_write(fd, data)

    monkeypatch.setattr(ledger.os, "write", interloper)
    with pytest.raises(ledger.LedgerError):
        ledger.append(real, {"verb": "set_property", "file_sha256_before": None}, state_dir=state)
    monkeypatch.undo()
    assert lp.exists() and lp.read_bytes() == b'{"interloper":1}\n'
