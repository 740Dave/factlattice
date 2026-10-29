"""`canonical-node: [ID]` mints a CANONICAL edge (legacy node -> canonical node), plus its lint,
selfcheck control and spec/checklist text.

NEGATIVE-CONTROL DISCIPLINE. Every test here was RUN AND OBSERVED FAILING on 62db7e6
(no `canonical` key) before the change. Parser tests assert the EDGE IS IN THE PARSED
GRAPH, not merely that the property parsed.

The edge NAME `CANONICAL` is FINAL: The architect's Part B (`factlattice_engine/caution.py`
`CANONICAL_EDGE_TYPE`) keys on it; `test_edge_name_is_final` pins it.
"""
from __future__ import annotations

from pathlib import Path

from factlattice.parser import REL_PROPERTY_EDGES, parse_text
from factlattice_engine import binding
from factlattice_engine.lint import lint

ROOT = Path(__file__).resolve().parent.parent

DOC = """# Doc [SPEC:DOC]

### [N-LEGACY] Legacy
{lines}

### [N-CANON] Canonical
- definition: the canonical one

### [N-OTHER] Other
- definition: another
"""


def _doc(*lines):
    return DOC.format(lines="\n".join(lines))


def _lint(tmp_path, text):
    p = tmp_path / "c.lts"
    p.write_text(text)
    return lint(str(p))


def _kinds(res, origin="canonical"):
    return sorted(f.kind for f in res.findings if f.origin == origin)


def test_edge_name_is_final():
    assert REL_PROPERTY_EDGES["canonical-node"] == ("CANONICAL", "semantic", False)


def test_binding_minting_keys_stay_in_step():
    assert "canonical-node" in binding.MINTING_KEYS


def test_canonical_mints_forward_edge_in_graph():
    edges = parse_text(_doc("- canonical-node: [N-CANON]"), "t.lts").edges
    got = [e for e in edges if e.type == "CANONICAL"]
    assert got, "`canonical` minted NO CANONICAL edge"
    e = got[0]
    assert (e.source, e.target, e.layer, e.inference) == (
        "N-LEGACY", "N-CANON", "semantic", "property")
    # forward only: nothing runs canonical -> legacy
    assert not [x for x in got if x.source == "N-CANON"]


def test_canonical_is_not_superseded_by():
    edges = parse_text(_doc("- canonical-node: [N-CANON]"), "t.lts").edges
    assert not [e for e in edges if e.type == "SUPERSEDES"]


def test_lint_clean_on_a_well_formed_canonical(tmp_path):
    res = _lint(tmp_path, _doc("- canonical-node: [N-CANON]"))
    assert _kinds(res) == []


def test_lint_same_target_twice_is_not_multiple(tmp_path):
    res = _lint(tmp_path, _doc("- canonical-node: [N-CANON] [N-CANON]"))
    assert _kinds(res) == []


def test_lint_missing_target(tmp_path):
    res = _lint(tmp_path, _doc("- canonical-node: [N-NOWHERE]"))
    assert _kinds(res) == ["canonical_target_missing"]
    assert all(f.severity == "error" for f in res.findings if f.origin == "canonical")


def test_lint_self_canonical(tmp_path):
    res = _lint(tmp_path, _doc("- canonical-node: [N-LEGACY]"))
    assert _kinds(res) == ["canonical_self"]


def test_lint_two_targets_is_refusal_grade(tmp_path):
    res = _lint(tmp_path, _doc("- canonical-node: [N-CANON] [N-OTHER]"))
    assert _kinds(res) == ["canonical_multiple"]
    assert all(f.severity == "error" for f in res.findings if f.origin == "canonical")


def test_lint_two_cycle(tmp_path):
    text = _doc("- canonical-node: [N-CANON]").replace(
        "- definition: the canonical one", "- canonical-node: [N-LEGACY]")
    res = _lint(tmp_path, text)
    assert "canonical_cycle" in _kinds(res)


def test_lint_chain_is_not_a_cycle(tmp_path):
    text = _doc("- canonical-node: [N-CANON]").replace(
        "- definition: the canonical one", "- canonical-node: [N-OTHER]")
    assert _kinds(_lint(tmp_path, text)) == []


def test_spec_distinguishes_canonical_from_superseded_by():
    text = (ROOT / "specs" / "10-abstract-model.lts").read_text()
    assert "[MODEL-CANONICAL]" in text
    body = text.split("[MODEL-CANONICAL]", 1)[1]
    assert "superseded-by" in body and "content stays valid" in body
    spec = (ROOT / "SPEC.md").read_text()
    assert "| `canonical-node` | CANONICAL | semantic | node → each referenced ID |" in spec


def test_checklist_rule_for_new_edge_keys():
    sdlc = (ROOT / "SDLC.md").read_text()
    sec = sdlc.split("### 7.6 FACTLATTICE-SPEC-CHECKLIST", 1)[1].split("\n## 8.", 1)[0]
    assert "[MINTING-TABLE-KEYS]" in sec
    for needle in ("REL_PROPERTY_EDGES", "binding.MINTING_KEYS", "selfcheck control",
                   "caution.py", "CANONICAL_EDGE_TYPE"):
        assert needle in sec, needle


def test_selfcheck_control_mints_canonical():
    import json
    oracle = json.loads((ROOT / "factlattice/selfcheck/control.expected.json").read_text())
    ids = {c["case_id"] for c in oracle["cases"]}
    assert "MINT-CANONICAL" in ids


def test_negative_control_selfcheck_goes_red_without_the_table_entry(monkeypatch):
    """The control must FIRE when the key is not minting: delete the table entry and the
    selfcheck verdict is FAIL, naming the SL-38 line (not a digest problem)."""
    from factlattice import parser
    from factlattice_engine.selfcheck import run_selfcheck
    assert run_selfcheck()["verdict"] == "PASS"
    patched = {k: v for k, v in parser.REL_PROPERTY_EDGES.items() if k != "canonical-node"}
    monkeypatch.setattr(parser, "REL_PROPERTY_EDGES", patched)
    r = run_selfcheck()
    assert r["verdict"] == "FAIL" and r["digest_ok"] is True
    assert any(c["result"] == "FAIL" for c in r["cases"])


# ---------------- round 2 (review F1/F2/F3/F7) ----------------

def test_bare_canonical_key_must_not_mint():
    """F1: `canonical` is already free-text status prose on 10 acme-docs nodes. It must
    stay a plain property: no edge, even with a bracket in the sentence."""
    assert "canonical" not in REL_PROPERTY_EDGES
    assert "canonical" not in binding.MINTING_KEYS
    text = _doc("- canonical: false -- NOT yet canonical/live; see [N-CANON]")
    edges = parse_text(text, "t.lts").edges
    assert not [e for e in edges if e.type == "CANONICAL"]
    assert not [e for e in edges if e.inference == "property"]


def test_edge_type_literal_pin():
    """F3: The architect's caution.py CANONICAL_EDGE_TYPE must equal this literal (it adds the
    cross-import pin on its branch)."""
    assert REL_PROPERTY_EDGES["canonical-node"][0] == "CANONICAL"


def test_same_target_twice_mints_one_edge():
    """F7: an identical repeated target is one edge."""
    edges = parse_text(_doc("- canonical-node: [N-CANON] [N-CANON]"), "t.lts").edges
    assert len([e for e in edges if e.type == "CANONICAL"]) == 1


import pytest


@pytest.mark.parametrize("value", [
    "see N-CANON",                 # prose, no brackets
    "N-CANON",                     # unbracketed
    "[n-canon]",                   # lowercase: prose bracket
    "[ N-CANON ]",                 # padded
    "[Mixed-Case]",                # malformed ref
    "the one is [N-CANON] really", # prose that cites
    "[N-CANON] (the real one)",    # trailing gloss: refs only
])
def test_lint_value_must_be_refs_only(tmp_path, value):
    """F2: a `canonical-node:` value must be refs only. Prose or a malformed ref is an
    ERROR, so the silent 'stored as text, lint clean' case cannot happen."""
    res = _lint(tmp_path, _doc(f"- canonical-node: {value}"))
    assert "canonical_node_not_refs" in _kinds(res), (value, _kinds(res))
    assert all(f.severity == "error" for f in res.findings
               if f.origin == "canonical" and f.kind == "canonical_node_not_refs")


@pytest.mark.parametrize("value", ["[N-CANON]", "[N-CANON], [N-OTHER]", "[N-CANON]; [N-OTHER]"])
def test_lint_refs_only_values_pass_the_shape_rule(tmp_path, value):
    assert "canonical_node_not_refs" not in _kinds(_lint(tmp_path, _doc(f"- canonical-node: {value}")))


def test_prior_use_scanner_finds_free_text_use_of_a_key(tmp_path):
    """§7.6 land-time step: tools/key_prior_use.py scans roots READ-ONLY for every
    existing use of a candidate minting key and says whether each value is refs-only."""
    import subprocess, sys, json
    d = tmp_path / "c"
    d.mkdir()
    (d / "a.lts").write_text(_doc("- canonical: false -- not yet; see [N-CANON]"))
    before = (d / "a.lts").read_bytes()
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "key_prior_use.py"),
                          "canonical", str(d)], capture_output=True, text=True)
    assert out.returncode == 1, out.stdout + out.stderr      # collision found
    rep = json.loads(out.stdout)
    assert rep["key"] == "canonical" and rep["uses"] == 1 and rep["not_refs_only"] == 1
    assert (d / "a.lts").read_bytes() == before
    out2 = subprocess.run([sys.executable, str(ROOT / "tools" / "key_prior_use.py"),
                           "canonical-node", str(d)], capture_output=True, text=True)
    assert out2.returncode == 0 and json.loads(out2.stdout)["uses"] == 0


def test_checklist_requires_prior_use_check():
    sdlc = (ROOT / "SDLC.md").read_text()
    sec = sdlc.split("[MINTING-TABLE-KEYS]", 1)[1].split("\n\n", 1)[0]
    assert "key_prior_use.py" in sec and "PRIOR-USE" in sec
    assert "CANONICAL_EDGE_TYPE" in sec and "must equal" in sec


def test_oracle_measured_against_names_sl38():
    import json
    o = json.loads((ROOT / "factlattice/selfcheck/control.expected.json").read_text())
    assert "SL-1 .. SL-40" in o["measured_against"]            # moved 39 -> 40 with the A-E4 oracle change
    assert "SL-38 is the `canonical-node` minting class" in o["measured_against"]


# ---------------- delta review fix round (F1/F2/F3) ----------------

def test_f1_canonical_keys_are_not_suggestion_candidates_binding():
    """F1: a legacy->canonical key must never be auto-suggested as a 'nearest minting
    alternative': following it would mint a semantically wrong edge. Choice: CANONICAL-minting
    keys are EXCLUDED from the fuzzy candidate pools (binding and undeclared_key), derived from
    the table, not much-closer-match."""
    for k in ("canonical-home", "canonical-core", "skill-policy-nodes", "contains", "canonical-nodes"):
        assert "canonical-node" not in binding.nearest_minting_alternatives(k), k


def test_f1_canonical_keys_are_not_suggestion_candidates_undeclared_key():
    from factlattice.core_keys import suggestion_excluded_keys, suggest_keys, core_keys
    # 2026-10-07: the review-extension verbs join the pool exclusion (tests/test_review_edge_verbs.py)
    assert suggestion_excluded_keys() == frozenset({"canonical-node", "attaches-to-hook", "applies-rule"})
    text = "---\ntitle: t\nid: \"SPEC:DOC\"\nlts: \"1.1\"\n---\n" + _doc("- canonical-home: [N-CANON]")
    parsed = parse_text(text, "t.lts")
    f = [x for x in parsed.vocabulary_findings if getattr(x, "key", None) == "canonical-home"]
    assert f, "fixture must raise undeclared_key for canonical-home"
    assert "canonical-node" not in f[0].detail["suggestions"]
    assert "canonical-node" not in f[0].message


def test_f1_unminted_lint_message_does_not_name_canonical_node(tmp_path):
    res = _lint(tmp_path, _doc("- skill-policy-nodes: [N-CANON]"))
    msgs = [f.message for f in res.findings if f.origin == "unminted"]
    assert msgs and not any("canonical-node" in m for m in msgs)


def test_f2_duplicate_canonical_triples_are_not_deduped_and_that_is_documented():
    """F2: `[A] CANONICAL [B]` in `## Relationships` already mints on base (unknown verbs mint
    as-is). Duplicated triples stay duplicated, like every other verb; only the KEY dedups."""
    text = _doc("- canonical-node: [N-CANON]") + (
        "\n## Relationships\n\n- [N-OTHER] CANONICAL [N-CANON]\n- [N-OTHER] CANONICAL [N-CANON]\n")
    edges = [e for e in parse_text(text, "t.lts").edges
             if e.type == "CANONICAL" and e.source == "N-OTHER"]
    assert len(edges) == 2
    src = (ROOT / "factlattice" / "parser.py").read_text()
    assert "reachable by a second route" in src and "NOT deduped" in src


def test_f3_empty_canonical_node_value_is_a_lint_finding(tmp_path):
    for line in ("- canonical-node:", "- canonical-node:   "):
        res = _lint(tmp_path, _doc(line))
        assert "canonical_node_empty" in _kinds(res), repr(line)
        assert all(f.severity == "error" for f in res.findings if f.kind == "canonical_node_empty")


def test_f3_populated_and_nested_values_are_not_flagged_empty(tmp_path):
    assert "canonical_node_empty" not in _kinds(_lint(tmp_path, _doc("- canonical-node: [N-CANON]")))
    assert "canonical_node_empty" not in _kinds(
        _lint(tmp_path, _doc("- canonical-node:", "  - [N-CANON]")))


def test_f3_empty_key_in_the_second_of_two_nodes_is_attributed_correctly(tmp_path):
    text = _doc("- canonical-node: [N-CANON]").replace(
        "- definition: another", "- canonical-node:")
    res = _lint(tmp_path, text)
    f = [x for x in res.findings if x.kind == "canonical_node_empty"]
    assert [x.node_id for x in f] == ["N-OTHER"]


def test_sdlc_requires_the_lint_message_diff():
    sdlc = (ROOT / "SDLC.md").read_text()
    sec = sdlc.split("[MINTING-TABLE-KEYS]", 1)[1].split("\n\n", 1)[0]
    assert "lint-message diff" in sec and "suggestion" in sec
