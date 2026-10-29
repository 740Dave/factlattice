"""[TOOL-REASON-IS-A-FIXED-CODE] the typed writers' SUCCESS reply carries a nested `ledger` block. Its `reason` is a code; the exception
class is `error_class` and the text (it can hold a path) is `live_detail`, beside its NEVER-persist note. The writers' TOP-LEVEL refusal
sentence stays as pinned (tests/test_add_node_noncreate_pin.py)."""
from __future__ import annotations

import json

from factlattice import ledger
from factlattice.authoring.edit import write_whole_file
from tests.test_node_change_ledger import DOC, _engine, _wd_env, env  # noqa: F401
from tests.test_tool_reason_codes import nested_reason_violations


def _assert_ledger_block(blk, code):
    assert blk["reason"] == code, blk
    assert blk["live_detail_note"].endswith("NEVER persist it"), blk


def test_a_failed_ledger_append_is_a_code_with_the_text_live_only(env, tmp_path, monkeypatch):
    from factlattice_engine import authoring
    sub = tmp_path / "corpus" / "sub"
    sub.mkdir()
    f = sub / "c.lts"
    f.write_text(DOC, encoding="utf-8")
    e = _engine(tmp_path)
    inside = tmp_path / "corpus" / "state"
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(inside))
    out = authoring.add_property(str(f), "LG-ONE", "k", "v", store=e.store)
    assert out["written"] is True and out["ledger"]["status"] == "failed"
    _assert_ledger_block(out["ledger"], "ledger-write-failed")
    assert out["ledger"]["error_class"] == "LedgerError" and "corpus root" in out["ledger"]["live_detail"]
    assert nested_reason_violations(out, "lts_add_property") == []


def test_an_unexpected_ledger_exception_is_a_code_with_its_class(env, monkeypatch):
    monkeypatch.setattr(ledger, "append", lambda *a, **k: (_ for _ in ()).throw(OSError(f"disk at {env}")))
    out = write_whole_file(env, DOC + "\n- z: 1\n")
    assert out.ledger["status"] == "failed"
    _assert_ledger_block(out.ledger, "ledger-write-failed")
    assert out.ledger["error_class"] == "OSError" and str(env) in out.ledger["live_detail"] and str(env) not in out.ledger["reason"]


def test_a_blob_gap_is_a_code(env, monkeypatch):
    from tests.test_ledger_round2 import _break_blobs
    _break_blobs(monkeypatch)
    out = write_whole_file(env, DOC + "\n- z: 1\n")
    assert out.ledger["status"] == "ok_without_undo"
    _assert_ledger_block(out.ledger, "undo-blob-not-stored")
    assert "forced" in out.ledger["live_detail"]


def test_the_ephemeral_skip_is_a_code():
    assert ledger.EPHEMERAL_SKIP["reason"] == "ledger-skipped-ephemeral" and ledger.EPHEMERAL_SKIP["status"] == "skipped"
    assert nested_reason_violations({"ledger": dict(ledger.EPHEMERAL_SKIP)}, "lts_add_property") == []


def test_the_warnings_still_show_the_operator_the_text():
    from factlattice.authoring.edit import oob_warnings
    w = oob_warnings({"status": "ok_without_undo", "reason": "undo-blob-not-stored", "live_detail": "OSError: forced"}, "p")
    assert any("undo-blob-not-stored" in x and "forced" in x for x in w), w


# ---------------------------------------------------------------- round 5: the operator text on EVERY route (Diamond d91a7b3 P1-1)
def _l8(env, tmp_path, monkeypatch, **kw):
    """The REAL path through factlattice_engine/authoring.py (the MCP typed-writer adapter): a ledger append that fails."""
    from factlattice_engine import authoring
    sub = tmp_path / "corpus" / "sub"
    sub.mkdir(exist_ok=True)
    f = sub / "c.lts"
    f.write_text(DOC, encoding="utf-8")
    e = _engine(tmp_path)
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "corpus" / "state"))
    return authoring.add_property(str(f), "LG-ONE", "k", "v", store=e.store, **kw)


def _assert_operator_text(out):
    msg = out["warnings"][0]
    assert msg.startswith("WRITTEN, BUT NOT LEDGERED (ledger-write-failed): "), msg
    assert "corpus root" in msg, msg                                       # the WHY, not only the code
    assert out["report"].startswith(msg), out["report"][:200]


def test_the_mcp_typed_writer_warning_and_report_say_why_the_ledger_failed(env, tmp_path, monkeypatch):
    out = _l8(env, tmp_path, monkeypatch, receipt="full")
    assert out["ledger"]["status"] == "failed"
    _assert_operator_text(out)


def test_the_compact_receipt_keeps_the_text_too(env, tmp_path, monkeypatch):
    out = _l8(env, tmp_path, monkeypatch, receipt="compact")
    assert "corpus root" in json.dumps(out["warnings"]) and out["ledger"]["live_detail"]


def test_a_mutant_that_reverts_the_authoring_py_warning_line_is_caught(env, tmp_path, monkeypatch):
    """Revert `_apply_ledger`'s message to the round-4 form (the code only): the real-path assertion above must FAIL."""
    from factlattice_engine import authoring

    def reverted(out, ledger, skipped=None):
        out["ledger"] = ledger
        if ledger and ledger.get("status") == "failed":
            msg = f"WRITTEN, BUT NOT LEDGERED: {ledger.get('reason')}"
            out["warnings"] = [msg] + list(out.get("warnings") or [])
            out["report"] = msg + "\n\n" + out["report"]
    monkeypatch.setattr(authoring, "_apply_ledger", reverted)
    out = _l8(env, tmp_path, monkeypatch, receipt="full")
    import pytest
    with pytest.raises(AssertionError):
        _assert_operator_text(out)


def test_the_library_routes_format_the_same_way(env):
    from factlattice.authoring.edit import oob_warnings
    blk = ledger.live_block("ok_without_undo", "undo-blob-not-stored", "OSError: forced")
    assert oob_warnings(blk, "p")[0].endswith("(undo-blob-not-stored): OSError: forced")
    assert ledger.describe(ledger.live_block("failed", "ledger-write-failed", OSError("zq"))) == "(ledger-write-failed): OSError: zq" \
        or ledger.describe(ledger.live_block("failed", "ledger-write-failed", OSError("zq"))) == "(ledger-write-failed): zq"
    assert ledger.describe({"status": "failed", "reason": "ledger-write-failed"}) == "(ledger-write-failed)"


def test_no_other_consumer_formats_a_bare_ledger_reason():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("factlattice_engine/authoring.py", "factlattice/authoring/edit.py", "factlattice/authoring/schema.py"):
        t = (root / rel).read_text(encoding="utf-8")
        assert "NOT LEDGERED: {ledger.get('reason')}" not in t, rel
        assert "ledger.get('reason')}" not in t.replace("ledger.describe(ledger)", ""), rel


def test_list_corpora_heal_error_keeps_the_sentence_not_the_code():
    t = (__import__("pathlib").Path(__file__).resolve().parent.parent / "factlattice_engine/tools.py").read_text(encoding="utf-8")
    assert 'heal.get("explanation")' in t


def test_the_docs_match_the_codes():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    spec = (root / "SPEC.md").read_text(encoding="utf-8")
    for needle in ("ledger-write-failed", "ledger-skipped-ephemeral", "undo-blob-not-stored", "live_detail"):
        assert needle in spec, needle
    assert 'reason: "ephemeral: target is under' not in spec and "{status: \"failed\", reason}`" not in spec
    assert "heal_error" in spec and "self-heal-raised" in spec          # the disclosed change of the display string
    sdlc = (root / "SDLC.md").read_text(encoding="utf-8")
    row = next(l for l in sdlc.splitlines() if l.startswith("- [ ] [TOOL-REASON-IS-A-FIXED-CODE]"))
    assert "`checks[].reason` carries" not in row and "site_config" in row
    assert "HEAL_REASON_CODES" in row and "fixed sentence for the ledger" not in row
