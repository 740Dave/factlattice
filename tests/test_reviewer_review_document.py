"""The review DOCUMENT as the reviewer writes it, read back as its consumers read it (Spec A v3 sections 4.2, 4.3,
4.5; addendum C4(b), C4(c), C6.7, C6.8, ruling-(B) condition 2).

Every document here is written by the REAL typed writer (`lts_add_node`, create then append, `lts_add_property`
for the verdict) so that what is queried is what the reviewer produces, not a hand-made look-alike.

  4.3   a finding to its node and back: split `about-node` at the first colon, lts_node, compare content hashes
        (forward); lts_find where about-node equals (reverse); the gate query (severity one_of + prefix);
  C4(b) an interrupted document never reads as complete: killed between two writes it stays `verdict: in-progress`
        and the pinned "passed reviews" query does not return it;
  C4(c) an UNREVIEWED document never satisfies "passed";
  4.5   the self-gate counts V3 itself: a review missing a required key FAILS it although lint reports no error.
"""
from __future__ import annotations

from tests._shapes import j as _j
import os
import shutil

import pytest

from factlattice import ledger
from factlattice_engine import config, reviewer
from factlattice_engine.tools import LtsEngine, dispatch
from tests import reviewer_contract as C

NOW_KEY = "REVIEWED-20261007T120000Z-AB12"
KEY2 = "REVIEWED-20261007T130000Z-CD34"
KEY3 = "REVIEWED-20261007T140000Z-EF56"
VAL = {
    "review-format-version": "2", "verdict": "in-progress", "reviewed-corpus": "reviewed",
    "reviewed-document": "/example/doc.lts", "reviewed-document-hash": "sha256:0", "node-count-reviewed": "3",
    "route": "mcp", "engine-build": "git example", "query-surface": "present", "core-proc-source": "engine",
    "core-proc-hash": "sha256:1", "extensions-applied": "none", "findings-by-severity": "info=0 minor=0 major=0 blocker=0",
    "not-examined": "judgement rules ran by reading and are not deterministic", "caller-context": "example",
    "write-root-caveat": "example", "reviewer-model": "example", "creation-ledger-state": "recorded",
    "site-config": "none", "site-overrides": "MISSING", "registry-mcp": "env /example/registry.json sha256:0",
    "registry-cli": "env /example/registry.json sha256:0", "stop-reason": "structure-gate:lost-properties",
    "gate-output": "lost=2", "rule-reliability": "validated", "severity": "major", "kind": "finding",
    "hook": "REVHOOK-DETERMINISTIC", "evidence": "x", "evidence-lines": "3-3", "outcome": "passed",
    "reason": "example", "source": "engine", "hash": "sha256:2", "loaded-via": "lts_node", "rule-hash": "sha256:3",
    "rule": "factlattice-reviewer:REVRULE-GEN-3", "about-node": "reviewed:HOST-A", "about-node-hash": "sha256:4",
}


def _props(fam, key, **over):
    p = {k: VAL[k] for k in C.REQUIRED[fam] if k != "part-of"}
    if "part-of" in C.REQUIRED[fam]:
        p["part-of"] = f"[REVIEW-{key}]"
    p.update(over)
    return p


def _add(path, nid, title, props, **kw):
    return dispatch("lts_add_node", {"path": str(path), "node_id": nid, "title": title, "properties": props, **kw},
                    tools=None)


def write_review(folder, key, *, findings=(), complete=True, drop=(), stop=None, load_rules=True, hash_override=None):
    """What the reviewer's WRITE step does: create (verdict in-progress), append, set the verdict LAST."""
    path = folder / (f"UNREVIEWED-{key}.lts" if stop else f"REVIEW-{key}.lts")
    if stop:
        r = _add(path, f"UNREVIEWED-{key}", "Not reviewed", _props("UNREVIEWED", key), create=True, create_parent=True,
                 doctype="review", doc_title="not reviewed")
        assert r["success"] and r["created"] is True, r
        return path
    sev = {s: 0 for s in reviewer.SEVERITIES}
    for _, s, _ in findings:
        sev[s] += 1
    props = _props("REVIEW", key, **{"findings-by-severity": " ".join(f"{k}={v}" for k, v in sev.items())})
    for d in drop:
        props.pop(d, None)
    r = _add(path, f"REVIEW-{key}", "Review", props, create=True, create_parent=True, doctype="review",
             doc_title="review")
    assert r["success"] and r["created"] is True and r["ledger_state"] == "recorded", r
    for i, (about, severity, rule) in enumerate(findings, 1):
        extra = {"rule-hash": hash_override} if hash_override else {}
        assert _add(path, f"REVFINDING-{key}-{i}", "Finding",
                    _props("REVFINDING", key, **{"about-node": about, "severity": severity, "rule": rule, **extra}))["success"]
    if load_rules:      # a REVPROV of kind `rule` per rule LOADED in this run (what the self-gate's rule check reads)
        for j, rule in enumerate(sorted({r for _, _, r in findings}), 1):
            assert _add(path, f"REVPROV-{key}-R{j}", "Rule loaded",
                        _props("REVPROV", key, kind="rule", source=rule, hash=VAL["rule-hash"]))["success"]
    assert _add(path, f"REVCHECK-{key}-1", "Check", _props("REVCHECK", key))["success"]
    assert _add(path, f"REVPROV-{key}-1", "Provenance", _props("REVPROV", key))["success"]
    if complete:
        v = dispatch("lts_add_property", {"path": str(path), "node_id": f"REVIEW-{key}", "key": "verdict",
                                          "value": "pass-with-findings" if findings else "pass"}, tools=None)
        assert v["success"] and v["action"] == "replaced", v
    return path


@pytest.fixture()
def world(tmp_path):
    reviewed = tmp_path / "reviewed"
    reviewed.mkdir()
    (reviewed / "doc.lts").write_text("# Doc [SPEC:HOSTDOC]\n\n## [HOST-A] A claim\n\n- claim: it works\n\n"
                                      "## [HOST-B] Another\n\n- claim: it also works\n- source: a test\n")
    reviews = tmp_path / "lts-reports"
    reviews.mkdir()                       # the OUTPUT ROOT exists (go-live makes it); create_parent makes only the corpus folder
    folder = reviews / "reviewed"
    p_pass = write_review(folder, NOW_KEY, findings=[("reviewed:HOST-A", "major", "factlattice-reviewer:REVRULE-GEN-3"),
                                                      ("reviewed:HOST-B", "minor", "factlattice-reviewer:REVRULE-GEN-4")])
    p_inprog = write_review(folder, KEY2, findings=[("reviewed:HOST-A", "minor", "factlattice-reviewer:REVRULE-GEN-4")],
                            complete=False)
    p_unrev = write_review(folder, KEY3, stop=True)
    eng = LtsEngine(config.build_store({
        "reviewed": {"roots": [{"path": str(reviewed), "area": None}], "domain_suffix": "r"},
        "lts-reports": {"roots": [{"path": str(reviews), "area": None}], "domain_suffix": "rv",
                        "profile": reviewer.PROFILE_PATH}}))
    return {"eng": eng, "tmp": tmp_path, "reviewed": reviewed, "reviews": reviews, "folder": folder,
            "pass": p_pass, "inprog": p_inprog, "unrev": p_unrev}


def find(world, **kw):
    r = dispatch("lts_find", {"corpus": "lts-reports", **kw}, tools=world["eng"])
    assert r["success"], r
    return r


# ------------------------------------------------------------------------------ 4.3 forward, reverse, gate
def test_forward_a_finding_to_its_node_and_its_hash_freshness(world):
    f = find(world, prefix=f"REVFINDING-{NOW_KEY}-1")["nodes"][0]
    corpus, nid = f["properties"]["about-node"].split(":", 1)
    assert (corpus, nid) == ("reviewed", "HOST-A")
    node = dispatch("lts_node", {"corpus": corpus, "node_id": nid}, tools=world["eng"])
    assert node["success"] is True
    got = node.get("content_hash") or node["node"]["content_hash"]
    assert got and got.startswith("sha256:")
    # the fixture's recorded hash is a placeholder, so this finding is STALE by the comparison the spec defines
    assert f["properties"]["about-node-hash"] != got


def test_forward_a_recorded_hash_equal_to_the_nodes_is_fresh_and_a_change_makes_it_stale(world):
    node = dispatch("lts_node", {"corpus": "reviewed", "node_id": "HOST-A"}, tools=world["eng"])
    h = node.get("content_hash") or node["node"]["content_hash"]
    path = world["folder"] / f"REVIEW-{NOW_KEY}.lts"
    r = dispatch("lts_add_property", {"path": str(path), "node_id": f"REVFINDING-{NOW_KEY}-1",
                                      "key": "about-node-hash", "value": h}, tools=None)
    assert r["success"]
    eng = LtsEngine(config.build_store({"reviewed": {"roots": [{"path": str(world["reviewed"]), "area": None}], "domain_suffix": "r"},
                                        "lts-reports": {"roots": [{"path": str(world["reviews"]), "area": None}], "domain_suffix": "rv"}}))
    f = dispatch("lts_find", {"corpus": "lts-reports", "prefix": f"REVFINDING-{NOW_KEY}-1"}, tools=eng)["nodes"][0]
    assert f["properties"]["about-node-hash"] == h
    (world["reviewed"] / "doc.lts").write_text((world["reviewed"] / "doc.lts").read_text().replace("it works", "it fails"))
    eng2 = LtsEngine(config.build_store({"reviewed": {"roots": [{"path": str(world["reviewed"]), "area": None}], "domain_suffix": "r"}}))
    n2 = dispatch("lts_node", {"corpus": "reviewed", "node_id": "HOST-A"}, tools=eng2)
    assert (n2.get("content_hash") or n2["node"]["content_hash"]) != h


def test_reverse_every_finding_about_a_node(world):
    r = find(world, where=[{"key": "about-node", "equals": "reviewed:HOST-A"}])
    assert sorted(n["id"] for n in r["nodes"]) == sorted([f"REVFINDING-{NOW_KEY}-1", f"REVFINDING-{KEY2}-1"])
    one = find(world, prefix=f"REVFINDING-{NOW_KEY}-", where=[{"key": "about-node", "equals": "reviewed:HOST-A"}])
    assert [n["id"] for n in one["nodes"]] == [f"REVFINDING-{NOW_KEY}-1"]


def test_gate_findings_at_or_above_a_severity_in_one_review(world):
    r = find(world, prefix=f"REVFINDING-{NOW_KEY}-", where=[{"key": "severity", "one_of": ["major", "blocker"]}])
    assert [n["id"] for n in r["nodes"]] == [f"REVFINDING-{NOW_KEY}-1"]
    none = find(world, prefix=f"REVFINDING-{KEY2}-", where=[{"key": "severity", "one_of": ["major", "blocker"]}])
    assert none["nodes"] == []


# ------------------------------------------------------------------------------ C4(b)/(c): the pinned "passed" query
PASSED = dict(prefix="REVIEW-", where=[{"key": "verdict", "one_of": ["pass", "pass-with-findings"]}])


def test_the_pinned_passed_query_returns_only_the_completed_review(world):
    ids = [n["id"] for n in find(world, **PASSED)["nodes"]]
    assert ids == [f"REVIEW-{NOW_KEY}"]


def test_a_run_killed_between_two_writes_leaves_in_progress_and_is_not_passed(world):
    text = world["inprog"].read_text()
    assert "- verdict: in-progress" in text and text.count("[REVFINDING-") == 1
    assert f"REVIEW-{KEY2}" not in [n["id"] for n in find(world, **PASSED)["nodes"]]
    ip = find(world, prefix="REVIEW-", where=[{"key": "verdict", "equals": "in-progress"}])
    assert [n["id"] for n in ip["nodes"]] == [f"REVIEW-{KEY2}"]


def test_an_unreviewed_document_is_never_passed_and_carries_no_verdict(world):
    n = find(world, prefix=f"UNREVIEWED-{KEY3}")["nodes"][0]
    assert "verdict" not in n["properties"]
    # not by prefix, and not by a verdict query that forgets the prefix
    assert all(not x["id"].startswith("UNREVIEWED-") for x in find(world, **PASSED)["nodes"])
    loose = find(world, where=[{"key": "verdict", "one_of": ["pass", "pass-with-findings"]}])
    assert all(not x["id"].startswith("UNREVIEWED-") for x in loose["nodes"])


def test_a_failed_self_gate_never_reaches_a_passing_verdict(world, tmp_path):
    """C6.7: a document whose gate fails is left in-progress (the procedure's own rule); the gate says so."""
    bad = write_review(world["folder"], "REVIEWED-20261007T150000Z-0A0A", drop=("route",), complete=False)
    res = reviewer.self_gate(str(bad), lambda t, a: dispatch(t, a, tools=world["eng"]))
    assert res["passed"] is False
    assert "- verdict: in-progress" in bad.read_text()


# ------------------------------------------------------------------------------ 4.5 self-gate
def _gate(world, path):
    return reviewer.self_gate(str(path), lambda t, a: dispatch(t, a, tools=world["eng"]))


def test_a_complete_review_passes_the_gate(world):
    g = _gate(world, world["pass"])
    assert g["passed"] is True, g
    assert [c["name"] for c in g["checks"]] == ["profile-available", "lint-errors-zero", "no-missing-required",
                                                "no-lost-properties", "path-find-proof", "severities-match",
                                                "findings-have-loaded-rules", "no-secret-in-review"]


def test_the_unreviewed_document_passes_the_same_gate(world):
    g = _gate(world, world["unrev"])
    assert g["passed"] is True, g
    assert "severities-match" not in [c["name"] for c in g["checks"]]


def test_a_review_missing_a_required_key_fails_the_gate_although_lint_reports_no_error(world):
    bad = write_review(world["folder"], "REVIEWED-20261007T150000Z-0B0B", drop=("route", "engine-build"), complete=False)
    lint = dispatch("lts_lint", {"source": str(bad), "dangle": True, "profile": reviewer.PROFILE_PATH}, tools=world["eng"])
    assert lint["profile"]["available"] is True
    assert lint["summary"].get("errors", 0) == 0, "lint itself calls this CLEAN (V3 is a warning)"
    g = _gate(world, bad)
    assert g["passed"] is False
    chk = {c["name"]: c for c in g["checks"]}
    assert chk["lint-errors-zero"]["ok"] is True and chk["no-missing-required"]["ok"] is False
    assert "route" in chk["no-missing-required"]["detail"] and "engine-build" in chk["no-missing-required"]["detail"]


def test_a_findings_by_severity_that_disagrees_with_the_findings_present_fails(world):
    path = world["pass"]
    r = dispatch("lts_add_property", {"path": str(path), "node_id": f"REVIEW-{NOW_KEY}", "key": "findings-by-severity",
                                      "value": "info=0 minor=0 major=0 blocker=0"}, tools=None)
    assert r["success"]
    g = _gate(world, path)
    assert g["passed"] is False and {c["name"]: c["ok"] for c in g["checks"]}["severities-match"] is False


def test_an_undeclared_key_in_a_strict_review_fails_the_gate_by_lint_error(world):
    path = world["pass"]
    r = dispatch("lts_add_property", {"path": str(path), "node_id": f"REVIEW-{NOW_KEY}", "key": "zzz-undeclared",
                                      "value": "x"}, tools=None)
    assert r["success"]
    g = _gate(world, path)
    assert g["passed"] is False and {c["name"]: c["ok"] for c in g["checks"]}["lint-errors-zero"] is False


def test_a_lost_property_in_the_review_fails_the_gate(world):
    """A line the parser read as PROSE (an emphasised key) is a property that vanished: lint reports it clean."""
    path = world["pass"]
    with open(path, "a") as fh:
        fh.write("- **Lost Key**: this property did not survive\n")
    g = _gate(world, path)
    chk = {c["name"]: c for c in g["checks"]}
    assert g["passed"] is False and chk["no-lost-properties"]["ok"] is False, g


def test_the_gate_fails_closed_when_the_profile_is_not_in_force(world):
    g = reviewer.self_gate(str(world["pass"]), lambda t, a: dispatch(t, a, tools=world["eng"]),
                           profile_path=str(world["tmp"] / "no-such-profile.lts"))
    assert g["passed"] is False and {c["name"]: c["ok"] for c in g["checks"]}["profile-available"] is False


def test_the_gate_fails_closed_against_an_old_server_that_ignores_path(world):
    """Stale-server guard 2: a server that predates `path` answers across every corpus, with no file_source."""
    def old(tool, args):
        a = dict(args)
        if tool == "lts_find":
            a.pop("path", None)                       # the old server ignores it
            a["corpus"] = "lts-reports"
        return dispatch(tool, a, tools=world["eng"])
    g = reviewer.self_gate(str(world["pass"]), old)
    assert g["passed"] is False and {c["name"]: c["ok"] for c in g["checks"]}["path-find-proof"] is False


def test_severity_counts_parser():
    assert reviewer.parse_severity_counts("info=0 minor=1 major=2 blocker=0") == {"info": 0, "minor": 1, "major": 2, "blocker": 0}
    for bad in ("none", "info=0 minor=1", "info=0 minor=1 major=2 blocker=0 info=1", "", None):
        assert reviewer.parse_severity_counts(bad) is None


def test_the_review_is_connected_by_part_of_so_lts_traverse_can_walk_it(world):
    ids = [n["id"] for n in find(world, prefix=f"REV", where=[{"key": "part-of", "has": True}])["nodes"]]
    assert f"REVFINDING-{NOW_KEY}-1" in ids and f"REVCHECK-{NOW_KEY}-1" in ids and f"REVPROV-{NOW_KEY}-1" in ids


# ------------------------------------------------------------------------------ creation-ledger-state & ledger
def test_creation_ledger_state_is_recorded_for_a_normal_create_and_the_ledger_has_one_record(world):
    path = world["pass"]
    assert "- creation-ledger-state: recorded" in path.read_text()
    import json
    recs = [json.loads(x) for x in ledger.read_lines(os.path.realpath(path))]
    assert recs[0]["verb"] == "add_node" and recs[0]["file_sha256_before"] is None   # the create: exactly one record
    assert [r["verb"] for r in recs] == ["add_node"] * 7 + ["set_property"]          # create, 2 findings, 2 rule provs, check, prov, verdict
    assert [r["file_sha256_before"] is None for r in recs].count(True) == 1         # one record says "the file did not exist"



# ------------------------------------------------------------------------------ a finding with no loaded rule behind it
def test_a_finding_citing_a_rule_that_was_never_loaded_fails_the_gate(world):
    bad = write_review(world["folder"], "REVIEWED-20261007T160000Z-1A1A", complete=False, load_rules=False,
                       findings=[("reviewed:HOST-A", "major", "factlattice-reviewer:REVRULE-GEN-99")])
    g = _gate(world, bad)
    chk = {c["name"]: c for c in g["checks"]}
    assert g["passed"] is False and chk["findings-have-loaded-rules"]["ok"] is False
    assert "REVRULE-GEN-99" in chk["findings-have-loaded-rules"]["detail"] and "never loaded" in chk["findings-have-loaded-rules"]["detail"]
    assert all(c["ok"] for n, c in chk.items() if n != "findings-have-loaded-rules"), "only this class fails"


def test_a_finding_citing_a_loaded_rule_with_the_wrong_hash_fails_the_gate(world):
    bad = write_review(world["folder"], "REVIEWED-20261007T160000Z-1B1B", complete=False, hash_override="sha256:WRONG",
                       findings=[("reviewed:HOST-A", "major", "factlattice-reviewer:REVRULE-GEN-3")])
    chk = {c["name"]: c for c in _gate(world, bad)["checks"]}
    assert chk["findings-have-loaded-rules"]["ok"] is False and "hash" in chk["findings-have-loaded-rules"]["detail"]


def test_a_finding_citing_a_rule_whose_validation_errored_fails_the_gate(world):
    path = write_review(world["folder"], "REVIEWED-20261007T160000Z-1C1C", complete=False,
                        findings=[("reviewed:HOST-A", "major", "host:REVRULE-ONE-1")])
    r = _add(path, "REVCHECK-REVIEWED-20261007T160000Z-1C1C-2", "Check",
             _props("REVCHECK", "REVIEWED-20261007T160000Z-1C1C", rule="host:REVRULE-ONE-1", outcome="errored"))
    assert r["success"]
    chk = {c["name"]: c for c in _gate(world, path)["checks"]}
    assert chk["findings-have-loaded-rules"]["ok"] is False and "errored" in chk["findings-have-loaded-rules"]["detail"]


def test_an_engine_lint_finding_needs_no_loaded_rule_but_a_loaded_rule_with_a_matching_hash_passes(world):
    ok = write_review(world["folder"], "REVIEWED-20261007T160000Z-1D1D", complete=False,
                      findings=[("reviewed:HOST-A", "minor", "engine:lts_lint:dangle.orphan"),
                                ("reviewed:HOST-B", "major", "factlattice-reviewer:REVRULE-GEN-3")])
    chk = {c["name"]: c for c in _gate(world, ok)["checks"]}
    assert chk["findings-have-loaded-rules"]["ok"] is True, chk["findings-have-loaded-rules"]


# ------------------------------------------------------------------------------ ruling: the gate scans the finished review
def test_a_review_with_a_planted_secret_in_an_evidence_value_fails_the_gate_without_printing_it(world):
    bad = write_review(world["folder"], "REVIEWED-20261007T170000Z-2A2A", complete=False,
                       findings=[("reviewed:HOST-A", "major", "factlattice-reviewer:REVRULE-GEN-3")])
    r = dispatch("lts_add_property", {"path": str(bad), "node_id": "REVFINDING-REVIEWED-20261007T170000Z-2A2A-1",
                                      "key": "evidence", "value": _j('char', 'ge u', 'sed ', 'sk_l', 'ive_', '51Ha', 'bcde', 'fghi', 'jklm', 'nop ', 'toda', 'y')}, tools=None)
    assert r["success"]
    g = _gate(world, bad)
    chk = {c["name"]: c for c in g["checks"]}
    assert g["passed"] is False and chk["no-secret-in-review"]["ok"] is False
    assert "REVFINDING-REVIEWED-20261007T170000Z-2A2A-1" in chk["no-secret-in-review"]["detail"]
    assert "sk_live_" not in json.dumps(g), "the gate must not repeat the value"
    assert all(c["ok"] for n, c in chk.items() if n != "no-secret-in-review")


def test_the_reviews_own_legitimate_values_do_not_trip_the_scan(world):
    path = world["pass"]
    for key, val in (("evidence", "see DEC-ACME-ARCHIVE-FILES-ARE-READ-ONLY-2027-03-14 and commit 09b80e9"),
                     ("about-node-hash", "sha256:" + "ab" * 32), ("hash", "sha256:" + "cd" * 32),
                     ("reviewed-document", "/Users/you/projdata/reports/some_long_directory_name_2026-09-12/doc.lts"),
                     ("evidence-lines", "12-14"),
                     ("evidence", "value withheld (looks like aws-access-key): node DOC-9, key claim, line 41")):
        assert dispatch("lts_add_property", {"path": str(path), "node_id": f"REVFINDING-{NOW_KEY}-1", "key": key, "value": val},
                        tools=None)["success"]
    chk = {c["name"]: c for c in _gate(world, path)["checks"]}
    assert chk["no-secret-in-review"]["ok"] is True, chk["no-secret-in-review"]


import json  # noqa: E402
