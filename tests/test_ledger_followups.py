"""Ledger follow-ups (the architect, agreed with the architect after go-live 16):
(b) a chain break is reported LOUDLY in the receipt and flagged on the record;
(d) whole-file writes keep the prior content durably, content-addressed, referenced by the record.
(a) the SDLC rules and (c) the conftest guard have their own checks at the bottom / in tests/conftest.py."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from factlattice.authoring.edit import set_property, write_whole_file
from tests.test_node_change_ledger import DOC, _recs, _sha, _wd_env, env  # noqa: F401  (env is a fixture)

REPO = Path(__file__).resolve().parents[1]
OOB = "OUT-OF-BAND EDIT"


def _hand_edit(f: Path) -> None:
    f.write_text(f.read_text() + "\nhand edit\n")


# ------------------------------------------------------------------------------------- (b)
def test_b_hand_edit_between_writes_warns_and_flags_the_record(env):
    set_property(env, "LG-ONE", "k0", "v0")
    _hand_edit(env)
    r = set_property(env, "LG-ONE", "k1", "v1")                     # still lands
    assert "k1" in env.read_text()
    (w,) = [x for x in r.warnings if OOB in x]
    first, second = _recs(env)
    assert str(os.path.realpath(env)) in w and "since ledger seq 1" in w and first["ts"] in w
    assert "not recorded" in w
    assert first["follows_out_of_band_edit"] is False and second["follows_out_of_band_edit"] is True


def test_b_clean_writes_have_no_warning_and_flag_false(env):
    r0 = set_property(env, "LG-ONE", "k0", "v0")
    r1 = set_property(env, "LG-ONE", "k1", "v1")
    assert not any(OOB in w for w in r0.warnings + r1.warnings)
    assert [x["follows_out_of_band_edit"] for x in _recs(env)] == [False, False]
    assert "out_of_band" not in json.dumps(r1.ledger)               # absent when not triggered


def test_b_a_file_that_predates_the_ledger_is_not_a_break(env):
    r = set_property(env, "LG-ONE", "k0", "v0")                     # first record, nothing to chain from
    assert not any(OOB in w for w in r.warnings)
    assert _recs(env)[0]["follows_out_of_band_edit"] is False


@pytest.mark.parametrize("receipt", ["compact", "full"])
def test_b_adapter_receipts_carry_the_warning(env, receipt):
    from factlattice_engine import authoring
    authoring.add_property(str(env), "LG-ONE", "k0", "v0", receipt=receipt)
    _hand_edit(env)
    out = authoring.add_property(str(env), "LG-ONE", "k1", "v1", receipt=receipt)
    assert out["written"] and any(OOB in w for w in out["warnings"])
    clean = authoring.add_property(str(env), "LG-ONE", "k2", "v2", receipt=receipt)
    assert not any(OOB in w for w in clean["warnings"])


def test_b_add_node_and_repair_also_warn(env):
    from factlattice.authoring.edit import add_node, repair_property
    set_property(env, "LG-ONE", "k0", "v0")
    _hand_edit(env)
    r = add_node(env, "LG-NEW", "New node", {"kind": "z"}, section=None)
    assert any(OOB in w for w in r.warnings)
    _hand_edit(env)
    lines = env.read_text().split("\n")
    n = next(i for i, l in enumerate(lines, 1) if "FLAGGED" in l)
    r2 = repair_property(env, "LG-ONE", n, "flagged")
    assert any(OOB in w for w in r2.warnings)


def test_b_write_document_warns(env, tmp_path):
    write_document, _doc_obj, _fake_preview = _wd_env()
    f = tmp_path / "corpus" / "w.lts"
    write_document(_doc_obj(), str(f), _fake_preview)
    _hand_edit(f)
    out = write_document(_doc_obj(), str(f), _fake_preview)
    assert out["written"] and any(OOB in w for w in out["warnings"])
    assert _recs(f)[-1]["follows_out_of_band_edit"] is True


# ------------------------------------------------------------------------------------- (d)
def _blob_dir(tmp_path) -> Path:
    return tmp_path / "state" / "ledger-blobs"


def test_d_prior_content_is_stored_content_addressed_and_referenced(env, tmp_path):
    old = env.read_bytes()
    sha = hashlib.sha256(old).hexdigest()
    write_whole_file(env, DOC.replace("- kind: y", "- kind: y\n- extra: e"))
    (rec,) = _recs(env)
    assert rec["prior_blob"] == sha == rec["file_sha256_before"]
    blob = _blob_dir(tmp_path) / sha
    assert blob.read_bytes() == old
    assert stat.S_IMODE(blob.stat().st_mode) == 0o600
    assert stat.S_IMODE(_blob_dir(tmp_path).stat().st_mode) == 0o700


def test_d_new_file_has_null_prior_blob_and_stores_nothing(env, tmp_path):
    p = tmp_path / "corpus" / "fresh.lts"
    write_whole_file(p, "# Fresh [SPEC:FR-ROOT]\n\n- kind: root\n")
    assert _recs(p)[0]["prior_blob"] is None
    assert not _blob_dir(tmp_path).exists() or not list(_blob_dir(tmp_path).iterdir())


def test_d_undo_write_document_from_record_plus_blob(env, tmp_path):
    write_document, _doc_obj, _fake_preview = _wd_env()
    f = tmp_path / "corpus" / "w.lts"
    write_document(_doc_obj(), str(f), _fake_preview)
    before = f.read_bytes()
    write_whole_file(f, DOC)                                        # overwrite
    rec = _recs(f)[-1]
    assert f.read_bytes() != before
    f.write_bytes((_blob_dir(tmp_path) / rec["prior_blob"]).read_bytes())
    assert _sha(f) == rec["file_sha256_before"]


def test_d_dedup_two_writes_from_the_same_prior_content_give_one_blob(env, tmp_path):
    other = tmp_path / "corpus" / "d.lts"
    other.write_bytes(env.read_bytes())                             # identical prior content
    write_whole_file(env, DOC + "\n- a: 1\n")
    write_whole_file(other, DOC + "\n- b: 2\n")
    blobs = list(_blob_dir(tmp_path).iterdir())
    assert len(blobs) == 1 and _recs(env)[0]["prior_blob"] == _recs(other)[0]["prior_blob"]
    assert not [p for p in _blob_dir(tmp_path).iterdir() if p.name.startswith(".")]   # no temp left


def test_d_typed_writers_store_no_blob_and_record_null(env, tmp_path):
    set_property(env, "LG-ONE", "k0", "v0")
    assert _recs(env)[0]["prior_blob"] is None
    assert not _blob_dir(tmp_path).exists()


def test_d_blob_never_lands_inside_a_registered_root(env, tmp_path):
    out = write_whole_file(env, DOC + "\n- z: 1\n", ledger_roots=[str(tmp_path / "state")])
    assert out.ledger["status"] == "failed" and "corpus root" in out.ledger["live_detail"]
    assert not (tmp_path / "state" / "ledger-blobs").exists()


# ------------------------------------------------------------------------------------- (a)
def test_a_sdlc_76_carries_the_two_rules():
    s = (REPO / "SDLC.md").read_text()
    assert "`write_whole_file` (as a library call or over MCP); both append a ledger record" in s and "chain_file_breaks" in s
    assert "UNDO EVIDENCE" in s and "ledger-blobs" in s and "47,762" in s


def test_spec_464_documents_the_new_fields():
    s = (REPO / "SPEC.md").read_text()
    i = s.index("#### 4.6.4")
    sect = s[i:s.index("### 4.7", i)]
    assert "follows_out_of_band_edit" in sect and "prior_blob" in sect and "OUT-OF-BAND EDIT" in sect
