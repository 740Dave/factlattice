"""The procedures example (docs/procedures-example/, explained in docs/PROCEDURES-AS-GRAPH-NODES.md).

The guide claims three things that only code can keep true:
- its profile is in scope by directory auto-discovery, and its rules FIRE: a planted procedure missing a required
  property gets exactly that V3 finding (the control), while the example as shipped lints clean;
- `lts_find(prefix="PROC-")` returns exactly the example's procedures;
- `lts_flow` renders a procedure's steps, with the decision step as a branch.
"""
from __future__ import annotations

import os
import shutil

import pytest

from factlattice_engine import LtsEngine, LtsStore
from factlattice_engine.config import build_partition
from factlattice_engine.lint import lint

from .mermaid_check import assert_valid_mermaid

EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "procedures-example")

PLANTED = """---
lts: "1.1"
title: "Planted violations"
id: "SPEC:DOC-PLANTED"
domain: example.procedures
scope: "Controls for the procedures profile"
boundary: "Test only"
audience: agent
document_tag: SPEC
status: draft
---
# Planted violations [SPEC:DOC-PLANTED]

## [PROC-NO-OWNER] A procedure with no owner

- trigger: a test needs a violation
- status: active
- must-not: ship
- last-reviewed: 2026-10-29

### [STEP-NO-ACTION] A step with no action

- part-of: [PROC-NO-OWNER]
- actor: the test
"""


def _v3(result):
    return sorted((f.node_id, f.detail.get("property")) for f in result.findings
                  if f.origin == "profile" and f.detail.get("validator") == "V3")


@pytest.fixture
def example_copy(tmp_path):
    dst = tmp_path / "procedures-example"
    shutil.copytree(EXAMPLE, dst)
    return dst


def test_the_profile_is_found_by_auto_discovery_and_the_example_lints_clean(example_copy):
    r = lint(str(example_copy))
    res = r.profile_resolution or {}
    assert res.get("resolved_from") == "directory-auto-discovery" and res.get("available") is True, res
    assert r.profile_documents == ["procedures-profile.lts"]
    assert [f.to_dict() for f in r.findings] == []


def test_the_profile_rules_fire_on_a_planted_violation(example_copy):
    """The control: without it, a clean lint could mean the rules never ran."""
    (example_copy / "planted.lts").write_text(PLANTED, encoding="utf-8")
    assert _v3(lint(str(example_copy))) == [("PROC-NO-OWNER", "owner"), ("STEP-NO-ACTION", "action")]


def test_with_the_profile_moved_out_its_rules_are_inert_and_the_keys_are_undeclared(example_copy, tmp_path):
    (example_copy / "planted.lts").write_text(PLANTED, encoding="utf-8")
    os.rename(example_copy / "procedures-profile.lts", tmp_path / "moved-profile.lts")
    r = lint(str(example_copy))
    res = r.profile_resolution or {}
    assert res.get("resolved_from") == "none" and res.get("available") is False, res
    assert [f for f in r.findings if f.origin == "profile"] == []
    assert {f.to_dict().get("kind") for f in r.findings} == {"undeclared_key"}


def test_the_entry_points_are_the_only_orphans(example_copy):
    r = lint(str(example_copy), dangle=True)
    orphans = sorted(f.node_id for f in r.findings if f.origin == "dangle" and f.to_dict().get("kind") == "orphan")
    assert orphans == ["DOC-RELEASE-PROCEDURES", "MATRIX-WHICH-RELEASE-PROCEDURE", "STEP-RUN-TESTS"]
    assert {f.severity for f in r.findings} == {"warning"}


def _engine():
    store = LtsStore()
    store.add_partition(build_partition("ex", EXAMPLE, "example.procedures"))
    return LtsEngine(store)


def test_a_prefix_find_returns_exactly_the_procedures():
    res = _engine().find(corpus="ex", prefix="PROC-")
    assert sorted(n["id"] for n in res["nodes"]) == ["PROC-HOTFIX-RELEASE", "PROC-RELEASE-FROM-MAIN"]
    digest = _engine().find(corpus="ex", prefix="PROC-", tier="digest")
    assert digest.get("complete") is True, digest
    rows = digest.get("digest") or digest.get("nodes")
    assert sorted(r["id"] for r in rows) == ["PROC-HOTFIX-RELEASE", "PROC-RELEASE-FROM-MAIN"]
    assert all("properties" not in r for r in rows)          # names only, no bodies


def test_the_procedure_renders_as_a_flow_with_its_decision_step():
    res = _engine().flow(corpus="ex", start_node="PROC-RELEASE-FROM-MAIN")
    assert res["success"] is True and res["mermaid"], res
    assert_valid_mermaid(res["mermaid"])
    ids = {n["id"] for n in res["nodes"]}
    assert {"STEP-RUN-TESTS", "STEP-TESTS-GREEN", "STEP-SIGN-TAG", "STEP-PUBLISH", "STEP-FAILURE-REPORT"} <= ids
    assert "{" in res["mermaid"]                      # the step with a `condition` is drawn as a decision diamond
    assert 'subgraph' in res["mermaid"] and '"release maintainer"' in res["mermaid"]   # steps grouped by `actor`


def test_the_decision_names_the_procedure_that_implements_it():
    refs = _engine().reverse(corpus="ex", node_id="DEC-SIGNED-RELEASE-TAGS")["referrers"]
    assert ("PROC-RELEASE-FROM-MAIN", "edge:IMPLEMENTS") in {(r["node_id"], r["how"]) for r in refs}


def test_review_age_queries_find_the_procedure_most_in_need_of_a_once_over():
    e = _engine()
    oldest = e.find(corpus="ex", prefix="PROC-", order_by={"field": "last-reviewed", "direction": "asc"}, limit=1)
    assert [n["id"] for n in oldest["nodes"]] == ["PROC-HOTFIX-RELEASE"]
    active = e.find(corpus="ex", prefix="PROC-", where=[{"key": "status", "equals": "active"}],
                    order_by={"field": "last-reviewed", "direction": "asc"}, limit=1)
    assert [n["id"] for n in active["nodes"]] == ["PROC-RELEASE-FROM-MAIN"]
