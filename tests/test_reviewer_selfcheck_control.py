"""A-E4: the selfcheck CONTROL holds the review and extension shapes (factlattice-reviewer Spec A).

The control gains two cases whose subject is the REVIEWER PROFILE, not the parser:

  REVIEWER-SHAPES-VALID     one valid node of each of the nine shapes (REVIEW, UNREVIEWED, REVFINDING, REVCHECK,
                            REVPROV, REVEXT, REVHOOK, REVPROC, REVRULE), judged under the SHIPPED reviewer
                            profile: zero V3 (required-property) findings and zero undeclared-key findings;
  REVIEWER-SHAPES-STRIPPED  the same nine nodes with every required property removed: EXACTLY the expected V3
                            findings, listed in the ORACLE (read by `json`, never by the parser under test).

WHY A "STRIPPED TWIN": a profile whose `required` lists were silently emptied would still give "0 V3 findings"
on the valid document. The twin makes the lists observable: it must give N findings, the N the oracle names.
This is also the reason the expected list lives in the oracle file and in this test as a literal, and not in the
profile it checks.

WHY NOT lint ERRORS: every V1-V10 finding is severity WARNING (F3), so a self-gate that wants required fields
enforced counts V3 itself. The control therefore asserts the V3 list, not a lint exit code.
"""
from __future__ import annotations

import json
import os
import shutil

import pytest

from factlattice_engine import selfcheck as sc
from factlattice_engine.selfcheck import VERDICT_FAIL, VERDICT_PASS, run_selfcheck
from tests.reviewer_contract import REQUIRED

PREV_DIGEST = "sha256:903121e781495992bad6ccd1c1e920a08fc16d68ffb3ca4166ef21f660d73969"      # the digest this slice moved FROM (A-E4)
CASES = ("REVIEWER-SHAPES-VALID", "REVIEWER-SHAPES-STRIPPED")
NINE = ("REVIEW", "UNREVIEWED", "REVFINDING", "REVCHECK", "REVPROV", "REVEXT", "REVHOOK", "REVPROC", "REVRULE")


def _oracle():
    with open(os.path.join(sc.control_dir(), "control.expected.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _case(r, cid):
    return next(c for c in r["cases"] if c["case_id"] == cid)


def test_the_control_passes_and_carries_the_two_reviewer_cases():
    r = run_selfcheck()
    assert r["verdict"] == VERDICT_PASS and r["digest_ok"] is True, r.get("error")
    assert r["case_count"] == r["passed"] == 58          # 56 -> 58
    for cid in CASES:
        c = _case(r, cid)
        assert c["result"] == VERDICT_PASS and c["checks"] >= 4, c


def test_the_digest_moved_and_says_why():
    assert sc.CONTROL_DIGEST != PREV_DIGEST
    assert sc.compute_control_digest() == sc.CONTROL_DIGEST
    src = open(sc.__file__, encoding="utf-8").read()
    assert PREV_DIGEST[:15] in src                       # "was: ..." recorded beside the constant
    assert "REVIEWER-SHAPES" in src and "A-E4" in src


def test_oracle_expected_findings_are_exactly_the_specs_required_lists():
    """The ORACLE's stripped expectation == the spec's required lists, written out literally in this test."""
    (a,) = [a for a in next(c for c in _oracle()["cases"] if c["case_id"] == "REVIEWER-SHAPES-STRIPPED")["assertions"]]
    want = sorted([f"{node}:{key}" for node, key in
                   ((n, k) for n, keys in a["given_node_prefixes"].items() for k in REQUIRED[keys])])
    got = sorted(f"{n}:{k}" for n, k in a["expect_v3"])
    assert got == want
    assert sorted(a["given_node_prefixes"].values()) == sorted(NINE)


def test_valid_document_has_one_node_of_each_shape():
    a = next(c for c in _oracle()["cases"] if c["case_id"] == "REVIEWER-SHAPES-VALID")["assertions"][0]
    assert sorted(a["given_node_prefixes"].values()) == sorted(NINE)
    assert a["expect_v3"] == [] and a["expect_undeclared_keys"] == []


def _fail_names(r, cid):
    return [(f["check"], f.get("expected"), f.get("produced")) for f in _case(r, cid)["failures"]]


@pytest.fixture
def swap_profile(monkeypatch, tmp_path):
    """Run the control against an EDITED COPY of the shipped profile (the instrument must be able to fail)."""
    real = sc.reviewer_profile_path()

    def go(edit):
        text = open(real, encoding="utf-8").read()
        new = edit(text)
        assert new != text, "the edit changed nothing"
        p = tmp_path / "edited-profile.lts"
        p.write_text(new, encoding="utf-8")
        monkeypatch.setattr(sc, "reviewer_profile_path", lambda: str(p))
        return run_selfcheck()
    return go


def test_negative_control_a_required_key_dropped_from_the_profile_fails_the_stripped_twin(swap_profile):
    def drop(text):
        # remove 'verdict' from REVIEW's required list wherever the profile spells it
        out, n = [], 0
        lines = text.splitlines(keepends=True)
        in_review = False
        for ln in lines:
            if ln.startswith("#") and "[NODEFAMILY-REVIEW]" in ln:
                in_review = True
            elif ln.startswith("#"):
                in_review = False
            if in_review and ln.strip() == "- verdict":
                n += 1
                continue
            out.append(ln)
        assert n == 1, n
        return "".join(out)
    r = swap_profile(drop)
    assert r["verdict"] == VERDICT_FAIL and r["digest_ok"] is True
    assert _case(r, "REVIEWER-SHAPES-STRIPPED")["result"] == VERDICT_FAIL
    v3 = [(e, p) for c, e, p in _fail_names(r, "REVIEWER-SHAPES-STRIPPED") if c.startswith("V3")]
    assert len(v3) == 1
    expected, produced = v3[0]
    missing = [x for x in expected if x not in produced]
    assert len(missing) == 1 and missing[0][0].startswith("REVIEW-") and missing[0][1] == "verdict"


def test_negative_control_an_extra_required_key_fails_the_valid_document(swap_profile):
    def add(text):
        i = text.index("## [NODEFAMILY-REVHOOK]")
        j = text.index("  - hook-contract-version\n", i)
        return text[:j] + "  - zzz-extra-required\n" + text[j:]
    r = swap_profile(add)
    assert r["verdict"] == VERDICT_FAIL
    assert _case(r, "REVIEWER-SHAPES-VALID")["result"] == VERDICT_FAIL


def test_negative_control_an_undeclared_key_fails_the_valid_document(swap_profile):
    """lts: "1.1" makes an undeclared key an ERROR for a real document; the control must see it."""
    def undeclare(text):
        out, n, in_keys = [], 0, False
        for ln in text.splitlines(keepends=True):
            if ln.startswith("#"):
                in_keys = "[PROPKEY-" in ln
            if in_keys and ln.strip() == "- creation-ledger-state":
                n += 1
                continue
            out.append(ln)
        assert n == 1, n
        return "".join(out)
    r = swap_profile(undeclare)
    assert r["verdict"] == VERDICT_FAIL
    assert _case(r, "REVIEWER-SHAPES-VALID")["result"] == VERDICT_FAIL


def test_negative_control_no_profile_at_all_is_a_failure_not_a_pass(monkeypatch, tmp_path):
    """A profile that cannot be loaded makes find_profile_violations return [] (V1-V9 inert). The valid case
    would pass vacuously; the stripped twin must catch that."""
    monkeypatch.setattr(sc, "reviewer_profile_path", lambda: str(tmp_path / "does-not-exist-profile.lts"))
    r = run_selfcheck()
    assert r["verdict"] == VERDICT_FAIL
    assert _case(r, "REVIEWER-SHAPES-STRIPPED")["result"] == VERDICT_FAIL


def test_modified_oracle_is_control_modified(tmp_path):
    d = tmp_path / "ctl"
    shutil.copytree(sc.control_dir(), d, ignore=shutil.ignore_patterns("__pycache__"))
    with open(d / "control.expected.json", "a") as fh:
        fh.write(" ")
    r = run_selfcheck(str(d))
    assert r["verdict"] == "CONTROL_MODIFIED"


def test_try_before_you_deploy_page_states_the_new_case_count():
    page = open(os.path.join(os.path.dirname(__file__), "..", "docs", "TRY-BEFORE-YOU-DEPLOY.md"),
                encoding="utf-8").read()
    assert '"case_count": 58' in page and '"passed": 58' in page
