"""PARSER SELF-VERIFICATION CONTROL — the tests that guard the control itself.

The control's job is to be asked of a RUNNING process. These tests guard the thing that
does the asking, and they answer three separate questions:

  (a) IS IT GREEN HERE?        run_selfcheck() returns PASS on this checkout, every case.
                               -> test_selfcheck_passes_on_head
                                  test_every_case_reports_a_result

  (b) CAN IT GO RED?           a control that cannot fail is not a control. The
                               discriminating power is proven in the report by REVERTING a
                               real parser fix; here we prove the harness's own failure
                               path produces a localised, named result.
                               -> test_case_failure_names_the_case_and_both_values

  (c) IS THE CONTROL INTACT?   one byte in either file of the PAIR trips the digest, and
                               CONTROL_MODIFIED is distinguishable from a case failure —
                               never a silent pass, never confused with a parser result.
                               -> test_mutated_corpus_fires_control_modified
                                  test_mutated_oracle_fires_control_modified
                                  test_control_modified_is_distinguishable_from_case_failure

⛔ Nothing here writes control.expected.json, and nothing here should ever be given the
ability to. See the sidecar header.
"""
from __future__ import annotations

import copy
import json
import shutil

import pytest

from factlattice_engine import selfcheck
from factlattice_engine.selfcheck import (
    CONTROL_DIGEST,
    VERDICT_CONTROL_MODIFIED,
    VERDICT_FAIL,
    VERDICT_PASS,
    compute_control_digest,
    render_text,
    run_selfcheck,
)


# ---------------------------------------------------------------------------
# (a) green on this checkout
# ---------------------------------------------------------------------------

def _case_with(oracle, assert_kind):
    """The case carrying `assert_kind`, by SEARCH — never by position."""
    for c in oracle["cases"]:
        if any(a.get("assert") == assert_kind for a in c.get("assertions", [])):
            return c
    raise AssertionError(f"no control case asserts {assert_kind!r}")


def test_selfcheck_passes_on_head():
    r = run_selfcheck()
    assert r["verdict"] == VERDICT_PASS, render_text(r)
    assert r["success"] is True
    assert r["failed"] == 0
    assert r["digest_ok"] is True


def test_every_case_reports_a_result():
    """Per-case, not just an aggregate — a verdict with no case table cannot localise."""
    r = run_selfcheck()
    assert r["case_count"] == len(r["cases"]) >= 20
    for c in r["cases"]:
        assert c["result"] in (VERDICT_PASS, VERDICT_FAIL)
        assert c["case_id"] and c["title"]
        # No case may be vacuous: a case that asserts nothing passes for free.
        assert c["checks"] > 0, f"case {c['case_id']} performed no checks"


def test_the_named_silent_loss_classes_all_have_a_case():
    """Rule 4: a cleanliness claim carries the class list it was measured against. The
    classes the control claims to cover must actually appear in the case table."""
    r = run_selfcheck()
    covered = {sl for c in r["cases"] for sl in (c.get("sl_classes") or [])}
    assert {"SL-1", "SL-2", "SL-10", "SL-11", "SL-13", "SL-14", "SL-18"} <= covered


def test_verdict_carries_the_receipt_fields():
    r = run_selfcheck()
    assert r["control_digest"] == CONTROL_DIGEST
    assert r["measured_against"].startswith("silent-loss-classes v")
    assert "nineteenth" in r["limit"], "the verdict must state its own limit"
    text = render_text(r)
    assert CONTROL_DIGEST in text and "PASS" in text


# ---------------------------------------------------------------------------
# (b) it can go red, and the red names itself
# ---------------------------------------------------------------------------
def test_case_failure_names_the_case_and_both_values(monkeypatch):
    """A deliberately wrong EXPECTATION (in memory only — no file is written) must
    produce a FAIL that carries the case id, what was expected and what was produced."""
    real_load = json.load

    def wrong_load(fh):
        oracle = copy.deepcopy(real_load(fh))
        oracle["graph"]["node_count"] = 999
        # ⛔ SEARCH, DO NOT INDEX. This was `cases[-1]`, which silently assumed the
        # LAST case carries the edge_count assertion. Appending any case to the
        # control then mutates nothing and the test PASSES while measuring
        # nothing — a positional coupling that fails in the safe-looking direction.
        for a in _case_with(oracle, "edge_count")["assertions"]:
            if a.get("assert") == "node_count":
                a["value"] = 999
        return oracle

    monkeypatch.setattr(selfcheck.json, "load", wrong_load)
    r = run_selfcheck()

    assert r["verdict"] == VERDICT_FAIL
    assert r["digest_ok"] is True, "the pair is intact; only the expectation was wrong"
    bad = [c for c in r["cases"] if c["result"] == VERDICT_FAIL]
    assert len(bad) == 1 and bad[0]["case_id"] == "GRAPH-TOTALS"
    f = bad[0]["failures"][0]
    assert f["check"] == "node_count"
    assert f["expected"] == 999
    # 23 -> 27 with oracle v2's four appended case nodes (CASE-WRAPPED-VALUE,
    # CASE-WRAPPED-EXCLUSION, CASE-ID-TABLE, CASE-PLAIN-TABLE).
    # 27 -> 29 with oracle v3's two (CASE-QUAL-PAREN, CASE-QUAL-DASH).
    # 29 -> 30 with oracle v4's ONE (CASE-CITED-NOT-DECLARED, SL-23's id hijack) — and
    # note the node is named for the CITATION, because that is the defect.
    # 30 -> 35 with oracle v5's FIVE property-edge minting cases (SL-24..SL-28):
    # CASE-RELATES-TO, CASE-SUPERSEDED-BY, CASE-VERIFY, CASE-SPAWNS, CASE-PROSE-CITES.
    # 35 -> 36 with Era 0's ONE malformed-ref case (SL-33, CASE-MALFORMED-REF).
    # 36 -> 38 with SL-38 (CASE-CANONICAL, CASE-CANONICAL-BARE).
    # 38 -> 42 with SL-39 (CASE-REV-HOOK, CASE-REV-PROC, CASE-REV-RULE, CASE-REV-BARE).
    assert f["produced"] == 42


def test_a_dropped_line_from_the_oracle_is_itself_a_failure(monkeypatch):
    """The partition check: you cannot make a failing line disappear by deleting it from
    the oracle — the deletion is the failure."""
    real_load = json.load

    def holey_load(fh):
        oracle = copy.deepcopy(real_load(fh))
        for case in oracle["cases"]:
            if 109 in case.get("lines", []):
                case["lines"].remove(109)
        return oracle

    monkeypatch.setattr(selfcheck.json, "load", holey_load)
    r = run_selfcheck()
    assert r["verdict"] == VERDICT_FAIL
    partition = next(c for c in r["cases"] if c["case_id"] == "ORACLE-PARTITION")
    assert partition["result"] == VERDICT_FAIL
    assert partition["failures"][0]["produced"] == [109]


# ---------------------------------------------------------------------------
# (c) the control digest — a tripwire on the PAIR
# ---------------------------------------------------------------------------
def _staged_pair(tmp_path):
    d = tmp_path / "control"
    d.mkdir()
    src = selfcheck.control_dir()
    for name in ("control.lts", "control.expected.json"):
        shutil.copy(f"{src}/{name}", d / name)
    return d


def test_untouched_copy_still_matches_the_digest(tmp_path):
    """No false fire: a byte-identical copy in another directory hashes the same, so the
    digest tracks CONTENT, not location or mtime."""
    d = _staged_pair(tmp_path)
    assert compute_control_digest(str(d)) == CONTROL_DIGEST
    assert run_selfcheck(str(d))["verdict"] == VERDICT_PASS


@pytest.mark.parametrize("target", ["control.lts", "control.expected.json"])
def test_one_mutated_byte_in_either_file_fires_control_modified(tmp_path, target):
    """Both halves of the PAIR are covered. Hashing only the oracle would let the corpus
    drift — changing what is MEASURED without touching what is CHECKED."""
    d = _staged_pair(tmp_path)
    p = d / target
    raw = p.read_bytes()
    # One byte, in the trailing region, chosen so the file stays syntactically whole:
    # the point is that ANY difference trips it, not that the mutation is meaningful.
    p.write_bytes(raw + b" ")

    r = run_selfcheck(str(d))
    assert r["verdict"] == VERDICT_CONTROL_MODIFIED
    assert r["success"] is False
    assert r["digest_ok"] is False
    assert r["control_digest"] != CONTROL_DIGEST
    assert r["expected_digest"] == CONTROL_DIGEST
    assert "CONTROL_DIGEST" in r["error"]


def test_control_modified_is_distinguishable_from_a_case_failure(tmp_path, monkeypatch):
    """The two red states must never be confusable: one says the PARSER is wrong, the
    other says the CONTROL moved and therefore nothing was measured at all."""
    d = _staged_pair(tmp_path)
    (d / "control.lts").write_bytes((d / "control.lts").read_bytes() + b"\n")
    modified = run_selfcheck(str(d))

    real_load = json.load

    def wrong_load(fh):
        oracle = copy.deepcopy(real_load(fh))
        # ⛔ SEARCH, DO NOT INDEX. This was `cases[-1]`, which silently assumed the
        # LAST case carries the edge_count assertion. Appending any case to the
        # control then mutates nothing and the test PASSES while measuring
        # nothing — a positional coupling that fails in the safe-looking direction.
        for a in _case_with(oracle, "edge_count")["assertions"]:
            if a.get("assert") == "edge_count":
                a["value"] = 1
        return oracle

    monkeypatch.setattr(selfcheck.json, "load", wrong_load)
    case_failure = run_selfcheck()

    assert modified["verdict"] != case_failure["verdict"]
    assert modified["verdict"] == VERDICT_CONTROL_MODIFIED
    assert case_failure["verdict"] == VERDICT_FAIL
    # CONTROL_MODIFIED runs NO cases — a partial result would invite reading it as one.
    assert modified["cases"] == [] and modified["case_count"] == 0
    assert case_failure["cases"] and case_failure["digest_ok"] is True
    assert "⛔" in render_text(modified)


def test_missing_control_pair_fails_loud_not_silent(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    r = run_selfcheck(str(empty))
    assert r["verdict"] == VERDICT_CONTROL_MODIFIED
    assert r["success"] is False
    assert "unreadable" in r["error"]


# ---------------------------------------------------------------------------
# tool surface — a running server must be ASKABLE
# ---------------------------------------------------------------------------
def test_lts_selfcheck_is_on_the_tool_surface_and_dispatches():
    from factlattice_engine import LTS_ALL_TOOL_NAMES, LTS_SELFCHECK_TOOL_NAMES
    from factlattice_engine.server import TOOLS
    from factlattice_engine.tools import dispatch

    assert "lts_selfcheck" in LTS_SELFCHECK_TOOL_NAMES
    assert "lts_selfcheck" in LTS_ALL_TOOL_NAMES
    schema = next(t for t in TOOLS if t["name"] == "lts_selfcheck")
    assert schema["inputSchema"]["type"] == "object"

    # Like lts_lint, it must answer with NO store seated — the server whose corpora
    # failed to load is exactly the one you want to interrogate.
    out = dispatch("lts_selfcheck", {}, tools=None)
    assert out["verdict"] == VERDICT_PASS
    assert out["control_digest"] == CONTROL_DIGEST
