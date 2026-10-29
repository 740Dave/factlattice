"""[A-E3 / MINTING-TABLE-KEYS 0-5] `attaches-to-hook` and `applies-rule` mint graph edges.

  attaches-to-hook: REVPROC -> a REVHOOK node in the SAME corpus        edge ATTACHES_TO_HOOK
  applies-rule:     REVPROC -> REVRULE, and REVRULE -> an existing rule node   edge APPLIES_RULE
  ordering is an integer `order` PROPERTY, never an edge.

Step 0 (prior use over every registered root) is run by hand and recorded in the build report
(tools/key_prior_use.py); steps 1-5 are pinned here. Parser tests assert the EDGE IS IN THE PARSED GRAPH,
not merely that the property parsed."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from factlattice.parser import REL_PROPERTY_EDGES, parse_text
from factlattice_engine import binding
from factlattice_engine.lint import lint

ROOT = Path(__file__).resolve().parent.parent

DOC = """# Ext [SPEC:EXT]

## Hook [REVHOOK-E1-STRUCTURE]

- hook-contract-version: 1

## Rule base [RULE-EXISTING]

- definition: a rule that already exists in the corpus

## Rule [REVRULE-E1-1]
{rule_lines}

## Step [REVPROC-E1-1]
{proc_lines}
"""


def _doc(proc=("- attaches-to-hook: [REVHOOK-E1-STRUCTURE]", "- applies-rule: [REVRULE-E1-1]", "- order: 1"),
         rule=("- applies-rule: [RULE-EXISTING]",)):
    return DOC.format(proc_lines="\n".join(proc), rule_lines="\n".join(rule))


def _edges(text, typ):
    return [e for e in parse_text(text, "t.lts").edges if e.type == typ]


# ------------------------------------------------------------------ (4) the edge is in the PARSED GRAPH
def test_attaches_to_hook_mints_a_forward_edge_in_the_graph():
    (e,) = _edges(_doc(), "ATTACHES_TO_HOOK")
    assert (e.source, e.target) == ("REVPROC-E1-1", "REVHOOK-E1-STRUCTURE")
    assert e.layer == "semantic" and e.inference == "property"
    assert not [x for x in _edges(_doc(), "ATTACHES_TO_HOOK") if x.source == "REVHOOK-E1-STRUCTURE"]


def test_applies_rule_mints_both_hops_forward():
    es = _edges(_doc(), "APPLIES_RULE")
    assert {(e.source, e.target) for e in es} == {("REVPROC-E1-1", "REVRULE-E1-1"),
                                                  ("REVRULE-E1-1", "RULE-EXISTING")}
    assert all(e.layer == "semantic" for e in es)
    # forward only: neither target points back
    assert not [e for e in es if e.source in ("REVRULE-E1-1", "RULE-EXISTING") and e.target == "REVPROC-E1-1"]


def test_order_is_a_property_and_mints_no_edge():
    pf = parse_text(_doc(), "t.lts")
    node = next(n for n in pf.nodes if n.id == "REVPROC-E1-1")
    assert node.properties["order"] == "1"
    assert not [e for e in pf.edges if e.source == "REVPROC-E1-1" and e.type not in
                ("ATTACHES_TO_HOOK", "APPLIES_RULE", "CONTAINS")]


def test_repeated_target_behaves_exactly_like_the_other_property_keys():
    """Only `canonical-node` dedups a repeated target; every other property key mints one edge per
    occurrence. These keys follow the general rule (not deduping is not this change's to alter)."""
    t = "# D [SPEC:D]\n\n## A [D-A]\n\n- see-also: [D-B] [D-B]\n\n## B [D-B]\n\n- k: v\n"
    base = len([e for e in parse_text(t, "t").edges if e.type == "RELATED_TO"])
    got = [e for e in _edges(_doc(proc=("- applies-rule: [REVRULE-E1-1] [REVRULE-E1-1]",)), "APPLIES_RULE")
           if e.source == "REVPROC-E1-1"]
    assert len(got) == base


def test_a_list_of_hooks_mints_one_edge_each():
    text = _doc(proc=("- attaches-to-hook: [REVHOOK-E1-STRUCTURE], [RULE-EXISTING]",))
    assert len(_edges(text, "ATTACHES_TO_HOOK")) == 2


@pytest.mark.parametrize("bare", ["applies-to", "attaches-to", "applies", "hook", "applies_rule"])
def test_bare_or_look_alike_spellings_mint_nothing(bare):
    """`applies-to` is the REVRULE selector property (free text in the spec); it must stay prose."""
    text = _doc(proc=(f"- {bare}: [REVHOOK-E1-STRUCTURE]",))
    es = [e for e in parse_text(text, "t.lts").edges if e.source == "REVPROC-E1-1" and e.type != "CONTAINS"]
    assert not es, (bare, es)


# ------------------------------------------------------------------ (0)/(3) the table and its companions
def test_table_entries_are_final_and_forward():
    assert REL_PROPERTY_EDGES["attaches-to-hook"] == ("ATTACHES_TO_HOOK", "semantic", False)
    assert REL_PROPERTY_EDGES["applies-rule"] == ("APPLIES_RULE", "semantic", False)


def test_binding_minting_keys_stay_in_step():
    assert {"attaches-to-hook", "applies-rule"} <= set(binding.MINTING_KEYS)


def test_spec_1_3_has_a_line_for_each_key():
    spec = (ROOT / "SPEC.md").read_text()
    assert "| `attaches-to-hook` | ATTACHES_TO_HOOK | semantic | node → each referenced ID |" in spec
    assert "| `applies-rule` | APPLIES_RULE | semantic | node → each referenced ID |" in spec


def test_the_writer_receipt_says_the_key_binds(tmp_path):
    from factlattice_engine import authoring
    f = tmp_path / "c" / "x.lts"
    f.parent.mkdir()
    f.write_text(_doc())
    r = authoring.add_node(str(f), "REVPROC-E1-2", "Step two",
                           {"attaches-to-hook": "[REVHOOK-E1-STRUCTURE]", "order": "2"}, receipt="full")
    assert r["success"] and [e["type"] for e in r["edges_minted"]] == ["ATTACHES_TO_HOOK"]
    assert not r["references_seen_but_not_minted"]


# ------------------------------------------------------------------ (1) lint
def _lint(tmp_path, text):
    p = tmp_path / "c.lts"
    p.write_text(text)
    return lint(str(p))


def _kinds(res):
    return sorted(f.kind for f in res.findings if f.origin == "review-edges")


def test_clean_graph_has_no_review_edge_findings(tmp_path):
    assert _kinds(_lint(tmp_path, _doc())) == []


@pytest.mark.parametrize("value", ["see REVHOOK-E1-STRUCTURE", "REVHOOK-E1-STRUCTURE", "[revhook-e1]",
                                   "the hook is [REVHOOK-E1-STRUCTURE] really", "[ REVHOOK-E1-STRUCTURE ]"])
def test_value_must_be_refs_only(tmp_path, value):
    res = _lint(tmp_path, _doc(proc=(f"- attaches-to-hook: {value}",)))
    assert "review_edge_not_refs" in _kinds(res), (value, _kinds(res))
    assert all(f.severity == "warning" for f in res.findings if f.kind == "review_edge_not_refs")


def test_target_must_exist(tmp_path):
    res = _lint(tmp_path, _doc(proc=("- attaches-to-hook: [REVHOOK-E1-NOPE]",)))
    assert "review_edge_target_missing" in _kinds(res)


def test_wrong_shape_is_an_error(tmp_path):
    # attaches-to-hook must run REVPROC -> REVHOOK; applies-rule REVPROC -> REVRULE
    assert "review_edge_wrong_shape" in _kinds(_lint(tmp_path, _doc(proc=("- attaches-to-hook: [RULE-EXISTING]",))))
    assert "review_edge_wrong_shape" in _kinds(_lint(tmp_path, _doc(proc=("- applies-rule: [RULE-EXISTING]",))))
    assert "review_edge_wrong_shape" in _kinds(_lint(tmp_path, _doc(rule=("- attaches-to-hook: [REVHOOK-E1-STRUCTURE]",))))


def test_a_corpus_without_the_keys_gets_no_findings_from_the_new_origin(tmp_path):
    res = _lint(tmp_path, "# Doc [SPEC:D]\n\n## One [D-ONE]\n\n- see-also: [D-TWO]\n\n## Two [D-TWO]\n\n- kind: x\n")
    assert _kinds(res) == []


def test_review_edges_is_a_declared_origin():
    from factlattice_engine.lint.core import ORIGINS
    assert "review-edges" in ORIGINS


# ------------------------------------------------------------------ (2) selfcheck control
def test_selfcheck_control_has_the_cases():
    oracle = json.loads((ROOT / "factlattice/selfcheck/control.expected.json").read_text())
    ids = {c["case_id"] for c in oracle["cases"]}
    assert {"MINT-REV-ATTACHES", "MINT-REV-APPLIES", "NOMINT-REV-BARE"} <= ids


def test_selfcheck_passes_with_the_new_cases_and_digest():
    from factlattice_engine.selfcheck import run_selfcheck
    r = run_selfcheck()
    assert r["verdict"] == "PASS" and r["digest_ok"] is True, r.get("error") or [c for c in r["cases"] if c["result"] != "PASS"][:3]
    assert r["case_count"] == 58          # 56 -> 58 with the A-E4 reviewer-shape cases (slice 2)


@pytest.mark.parametrize("key", ["attaches-to-hook", "applies-rule"])
def test_negative_control_selfcheck_goes_red_without_the_table_entry(monkeypatch, key):
    """The control must FIRE when the key is not minting."""
    from factlattice import parser
    from factlattice_engine.selfcheck import run_selfcheck
    patched = {k: v for k, v in parser.REL_PROPERTY_EDGES.items() if k != key}
    monkeypatch.setattr(parser, "REL_PROPERTY_EDGES", patched)
    r = run_selfcheck()
    assert r["verdict"] == "FAIL" and r["digest_ok"] is True
    assert any(c["result"] == "FAIL" for c in r["cases"])


def test_checklist_row_text_names_this_change_in_the_ledger_of_keys():
    sdlc = (ROOT / "SDLC.md").read_text()
    assert "attaches-to-hook" in sdlc and "applies-rule" in sdlc


# ------------------------------------------------------------------ (5) the suggestion pools
def test_review_keys_are_never_offered_as_a_nearest_minting_alternative():
    """MEASURED on the registered corpora: without this, `preserves` and `relates-to-spec` on
    acme-console were told 'nearest minting alternative: applies-rule / attaches-to-hook'."""
    from factlattice import core_keys
    assert {"attaches-to-hook", "applies-rule"} <= core_keys.suggestion_excluded_keys()
    assert "canonical-node" in core_keys.suggestion_excluded_keys()
    text = "# D [SPEC:D]\n\n## A [D-A]\n\n- preserves: [D-B] settled\n- relates-to-spec: [D-B]\n\n## B [D-B]\n\n- k: v\n"
    from factlattice_engine.lint import lint
    import tempfile, pathlib
    p = pathlib.Path(tempfile.mkdtemp()) / "d.lts"
    p.write_text(text)
    msgs = " ".join(f.message for f in lint(str(p)).findings if f.kind == "unminted_reference_list")
    assert "Nearest minting alternative" in msgs
    assert "applies-rule" not in msgs and "attaches-to-hook" not in msgs


# ===================================================== post-review P2-5: shape and target are WARNINGS
def _sev(res, kind):
    return {f.severity for f in res.findings if f.kind == kind}


def test_wrong_shape_and_missing_target_are_warnings_not_errors(tmp_path):
    assert _sev(_lint(tmp_path, _doc(proc=("- attaches-to-hook: [RULE-EXISTING]",))), "review_edge_wrong_shape") == {"warning"}
    assert _sev(_lint(tmp_path, _doc(proc=("- attaches-to-hook: [REVHOOK-E1-NOPE]",))), "review_edge_target_missing") == {"warning"}


def test_no_review_edge_lint_is_ever_an_error_in_any_corpus(tmp_path):
    from factlattice_engine.lint.review_edges import REVIEW_EDGE_SEVERITY
    assert set(REVIEW_EDGE_SEVERITY.values()) == {"warning"}, REVIEW_EDGE_SEVERITY
    bad = _doc(proc=("- attaches-to-hook: see the hook", "- applies-rule: [REVRULE-E1-NOPE]"),
               rule=("- attaches-to-hook: [REVHOOK-E1-STRUCTURE]",))
    res = _lint(tmp_path, bad)
    assert [f for f in res.findings if f.origin == "review-edges"], "the rules must still SAY it"
    assert not [f for f in res.findings if f.origin == "review-edges" and f.severity == "error"]


def test_a_customer_corpus_with_free_text_under_the_generic_keys_adds_zero_errors(tmp_path):
    d = tmp_path / "cust"
    d.mkdir()
    (d / "a.lts").write_text(HOST)
    base = lint(str(d), dangle=True)
    (d / "b.lts").write_text("# Cust [SPEC:CUST]\n\n## Policy [CUST-POLICY]\n\n"
                             "- applies-rule: see the compliance rule document\n"
                             "- attaches-to-hook: the nightly job\n")
    withfree = lint(str(d), dangle=True)
    errs = lambda r: sorted((f.origin, f.kind) for f in r.findings if f.severity == "error")
    assert errs(withfree) == errs(base)
    assert "review_edge_not_refs" in {f.kind for f in withfree.findings}


HOST = "# Host [SPEC:HOST]\n\n## One [HOST-ONE]\n\n- status: operational\n- see-also: [HOST-TWO]\n\n## Two [HOST-TWO]\n\n- status: operational\n"
BAD_EXT = ("# Ext [SPEC:EXT]\n\n## Hook [REVHOOK-E9-STRUCTURE]\n\n- hook-contract-version: 1\n\n"
           "## Step [REVPROC-E9-1]\n\n- attaches-to-hook: [HOST-ONE]\n- applies-rule: [HOST-TWO]\n")


def test_a_misshapen_extension_inside_a_host_adds_zero_errors_to_the_host(tmp_path):
    d = tmp_path / "host"
    d.mkdir()
    (d / "host.lts").write_text(HOST)
    base = lint(str(d), dangle=True)
    (d / "ext.lts").write_text(BAD_EXT)
    withext = lint(str(d), dangle=True)
    errs = lambda r: sorted((f.origin, f.kind) for f in r.findings if f.severity == "error")
    assert errs(withext) == errs(base), [x for x in errs(withext) if x not in errs(base)]
    assert "review_edge_wrong_shape" in {f.kind for f in withext.findings}      # and it is still SAID


def test_a_missing_hook_target_adds_zero_errors_without_dangle_checking(tmp_path):
    d = tmp_path / "host"
    d.mkdir()
    (d / "host.lts").write_text(HOST)
    base = lint(str(d))
    (d / "ext.lts").write_text("# Ext [SPEC:EXT]\n\n## Step [REVPROC-E9-1]\n\n- attaches-to-hook: [REVHOOK-E9-NOPE]\n")
    errs = lambda r: sorted((f.origin, f.kind) for f in r.findings if f.severity == "error")
    assert errs(lint(str(d))) == errs(base)
