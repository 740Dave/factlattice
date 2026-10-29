"""Unit tests for the FactLattice .lts parser: nodes, 4 edge layers, flags."""

import os

import pytest

import factlattice
from factlattice.coverage import unaccounted_lines
from factlattice.parser import (
    BRANCH_ID_TABLE_ROW,
    BRANCH_PROP_CONT,
    BRANCH_PROP_KEY_ONLY,
    BRANCH_PROP_MAPPING_CONT,
    BRANCH_PROP_MAPPING_ENTRY,
    BRANCH_PROP_NESTED_ITEM,
    FINDING_MIXED_PROPERTY_BLOCK,
    FINDING_NESTED_MAPPING_BLOCK,
    parse_text,
)
from tests import snippets

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parse():
    return parse_text(snippets.FULL, "syn.lts")


def _edges_of(pf, layer=None, etype=None, inference=None):
    out = []
    for e in pf.edges:
        if layer and e.layer != layer:
            continue
        if etype and e.type != etype:
            continue
        if inference and e.inference != inference:
            continue
        out.append(e)
    return out


def test_document_tag_and_doc_node():
    pf = _parse()
    assert pf.document_tag == "SYSTEM"
    ids = {n.id for n in pf.nodes}
    # h1 doc id is addressable as a node so cross-refs to it resolve
    assert "SYN-DOC" in ids
    doc = next(n for n in pf.nodes if n.id == "SYN-DOC")
    assert doc.document_tag == "SYSTEM"


def test_nodes_and_type_prefix():
    pf = _parse()
    a = next(n for n in pf.nodes if n.id == "SYN-A")
    assert a.type_prefix == "SYN"
    assert a.title == "Alpha node"
    assert a.domain == "core.example"
    assert a.status == "operational"
    # line_range is 1-indexed and sane
    assert a.line_range[0] >= 1 and a.line_range[1] >= a.line_range[0]
    # content_hash present and deterministic
    assert a.content_hash.startswith("sha256:")


def test_property_formats_p1_p2_p3_p4():
    pf = _parse()
    a = next(n for n in pf.nodes if n.id == "SYN-A")
    assert a.properties["definition"] == "the first system"       # P1
    assert a.properties["role"] == "bold-key role value"           # P2 (bold stripped)
    b = next(n for n in pf.nodes if n.id == "SYN-B")
    assert a.properties["see-also"]                                # P3 multi-value present
    assert isinstance(b.properties["tags"], list)                  # P4 nested list
    assert b.properties["tags"] == ["one", "two"]


def test_parent_section_nesting():
    pf = _parse()
    a = next(n for n in pf.nodes if n.id == "SYN-A")
    # SYN-A sits under "## 1. Systems" which has no id -> ancestor node is the doc
    assert a.parent_section == "SYN-DOC"


def test_semantic_layer_edges():
    pf = _parse()
    sem = _edges_of(pf, layer="semantic")
    pairs = {(e.source, e.target, e.type) for e in sem}
    assert ("SYN-A", "SYN-C", "IMPLEMENTS") in pairs      # relationships (authored)
    assert ("SYN-A", "SYN-B", "DEPENDS_ON") in pairs      # property edge (property)
    # property-inference edge exists
    assert any(e.inference == "property" for e in sem)
    # authored-inference edge exists
    assert any(e.inference == "authored" for e in sem)


def test_temporal_layer_edge():
    pf = _parse()
    temp = _edges_of(pf, layer="temporal")
    assert any(e.type == "BEFORE" and e.source == "SYN-B" and e.target == "SYN-C"
               for e in temp)


def test_causal_layer_edges():
    pf = _parse()
    causal = _edges_of(pf, layer="causal")
    types = {(e.source, e.target, e.type) for e in causal}
    assert ("SYN-A", "SYN-C", "ENABLES") in types
    assert ("SYN-B", "SYN-A", "PRECONDITION_FOR") in types
    assert all(e.inference == "authored" for e in causal)


def test_entity_layer_edges_and_structural():
    pf = _parse()
    entity = _edges_of(pf, layer="entity")
    # USED_BY reverses direction: referenced -> node (SYN-A -> SYN-C via the rel line)
    used = [e for e in entity if e.type == "USED_BY"]
    assert used
    # structural CONTAINS edges emitted with inference=structural
    contains = _edges_of(pf, etype="CONTAINS", inference="structural")
    assert contains
    assert all(e.layer == "entity" for e in contains)


def test_used_by_property_reverses_direction():
    pf = _parse()
    # SYN-B has `used-by: [SYN-A]` -> edge SYN-A --USED_BY--> SYN-B (referenced->node)
    prop_used = [e for e in pf.edges
                 if e.type == "USED_BY" and e.inference == "property"]
    assert any(e.source == "SYN-A" and e.target == "SYN-B" for e in prop_used)


def test_unregistered_namespace_preserved_and_flagged():
    pf = _parse()
    gates = [e for e in pf.edges if e.type == "example.GATES"]
    assert gates, "unregistered-ns edge must be PRESERVED, not dropped"
    assert "unregistered_ns" in gates[0].flags


def test_cross_domain_ref_carries_domain():
    pf = _parse()
    ks = [e for e in pf.edges if e.target == "EXAMPLE-KEYSTORE"]
    assert ks and ks[0].target_domain == "gov.example"


def test_dangling_target_preserved_at_parse_time():
    # A see-also to [SYN-MISSING] which is not defined -> edge preserved; the
    # index builder marks dangling=True in the whole-corpus pass.
    pf = _parse()
    missing = [e for e in pf.edges if e.target == "SYN-MISSING"]
    assert missing, "edge to a missing target must be preserved, never dropped"


# --- list-item node-definition extraction + 3-way discrimination -----------
def _parse_li():
    return parse_text(snippets.LIST_ITEM, "li.lts")


def test_list_item_nodedef_extracted():
    pf = _parse_li()
    by_id = {n.id: n for n in pf.nodes}
    assert "INV-W-1" in by_id, "`- [INV-W-1] **bold**` must define a node"
    assert "PULSE-GAUGE" in by_id
    n = by_id["INV-W-1"]
    assert n.definition_form == "list_item"
    assert n.type_prefix == "INV"
    assert n.title == "Single-holder conn."   # title from the bold text
    assert n.line_range[0] == n.line_range[1]  # single-line def
    assert n.parent_section == "LI-DOC" or n.parent_section is not None


def test_header_nodes_marked_header_form():
    pf = _parse_li()
    anchor = next(n for n in pf.nodes if n.id == "LI-ANCHOR")
    assert anchor.definition_form == "header"


def test_multi_leading_id_nodedef_defines_each():
    pf = _parse_li()
    ids = {n.id for n in pf.nodes}
    assert "INV-W-4" in ids and "INV-W-8" in ids
    a = next(n for n in pf.nodes if n.id == "INV-W-4")
    assert a.definition_form == "list_item"
    assert a.properties.get("co_defined_with") == ["INV-W-8"]


def test_edge_line_does_not_become_node():
    # `- [LI-ANCHOR] DEPENDS_ON [INV-W-1]` is an EDGE, not a node-def.
    pf = _parse_li()
    li_nodes = [n for n in pf.nodes if n.definition_form == "list_item"]
    # the DEPENDS_ON edge exists...
    assert any(e.type == "DEPENDS_ON" and e.source == "LI-ANCHOR" for e in pf.edges)
    # ...and did NOT spawn a spurious node from the edge's source/target line
    assert all(n.line_range[0] != _line_of(snippets.LIST_ITEM, "DEPENDS_ON")
               for n in li_nodes)


def test_profile_edge_prose_target_does_not_become_node():
    # `- [LI-ANCHOR] example.GATES some capability` is an EDGE (unresolved prose
    # target), NOT a node-def. It must not appear as a list_item node.
    pf = _parse_li()
    gates = [e for e in pf.edges if e.type == "example.GATES"]
    assert gates and "unregistered_ns" in gates[0].flags
    li_ids = {n.id for n in pf.nodes if n.definition_form == "list_item"}
    assert "LI-ANCHOR" not in li_ids  # LI-ANCHOR is a header node, not list-def'd


def test_property_line_does_not_become_node():
    # `- see-also: [PULSE-GAUGE]` / `- depends-on: [...]` are PROPERTIES, not nodes.
    pf = _parse_li()
    li_ids = {n.id for n in pf.nodes if n.definition_form == "list_item"}
    # no node was created whose id is a property key
    assert not any(pid for pid in li_ids if pid.lower() in ("see-also", "depends-on"))


def _line_of(text, needle):
    for i, ln in enumerate(text.splitlines(), start=1):
        if needle in ln:
            return i
    return -1


# ===========================================================================
# P5 — the SPEC §8.1 / §8.2 / §7.1 YAML-MAPPING property form
# ===========================================================================
# §2.2 enumerated four property forms and a YAML mapping was not one of them, so
# `props[key] = items if items` dropped every §8.1-shaped block WHOLE: measured at 14
# `context_for_change` blocks across five corpora, 5 of them in this repo's own
# `specs/50-decisions.lts`. Foundational principle P2 ("Notation Freedom") says a YAML
# block IS a notation and "the parser extracts the same abstract graph regardless of
# which notation was used", and that P2 "cannot be overridden by any section below" —
# so P5 is a CONFORMANCE fix, not a new feature.
#
# Every count below is EXACT on purpose. A `>= 1` here would pass just as happily on a
# parser that read the first line of a block and dropped the rest, which is the precise
# shape of the bug being closed.

P5_MAPPING = """\
# P5 [SPEC:P5-DOC]

## [P5-A] a node carrying SPEC 8.1's documented shape

- change: "Diagrams are derived from the graph, never stored"
- context_for_change:
    prior_state: "embedded diagrams were static snapshots"
    reasoning: "a stored diagram is a second copy of a fact the graph holds"
    invalidating_assumption: "that the render path is available wherever the corpus is read"
- status: accepted
"""


def test_p5_mapping_parses_to_a_dict_with_exactly_its_authored_fields():
    pf = parse_text(P5_MAPPING, "p5.lts")
    node = next(n for n in pf.nodes if n.id == "P5-A")
    value = node.properties["context_for_change"]
    assert isinstance(value, dict)
    assert list(value) == ["prior_state", "reasoning", "invalidating_assumption"]
    assert len(value) == 3
    # THE point of the fix: the sub-field is a KEYED FIELD, not a substring of an item.
    assert value["invalidating_assumption"] == (
        '"that the render path is available wherever the corpus is read"')
    # the block did not swallow its neighbours
    assert node.properties["status"] == "accepted"
    assert node.properties["change"] == (
        '"Diagrams are derived from the graph, never stored"')
    assert pf.findings == []


def test_p5_mapping_consumes_every_line_it_read():
    """SPEC §2.5 — the new branch must not open a coverage hole. The head is consumed
    by the P4/P5 head branch and each entry by the mapping branch; nothing structural
    is left unaccounted."""
    pf = parse_text(P5_MAPPING, "p5.lts")
    consumed = pf.coverage.consumed
    head = _line_of(P5_MAPPING, "- context_for_change:") - 1
    assert consumed[head] == BRANCH_PROP_KEY_ONLY
    entries = [i for i, b in consumed.items() if b == BRANCH_PROP_MAPPING_ENTRY]
    assert entries == [head + 1, head + 2, head + 3]
    assert unaccounted_lines(pf.coverage) == []


P5_WRAPPED = """\
# P5W [SPEC:P5W-DOC]

## [P5W-A] a mapping whose values wrap across lines

- context_for_change:
    prior_state: "defects were captured ad hoc — a code comment, at best a one-off
      node. No queryable aggregate; no consistent recurrence edge."
    reasoning: "the two questions defects must answer have different shapes."
"""


def test_p5_folded_continuation_keeps_the_whole_value():
    """The corpora wrap long quoted values. Breaking the block at the wrap would mark
    the head CONSUMED while dropping the tail — converting a loud drop into a silent
    one, which is strictly worse than the bug being fixed."""
    pf = parse_text(P5_WRAPPED, "p5w.lts")
    value = next(n for n in pf.nodes if n.id == "P5W-A").properties["context_for_change"]
    assert len(value) == 2
    assert value["prior_state"] == (
        '"defects were captured ad hoc — a code comment, at best a one-off node. '
        'No queryable aggregate; no consistent recurrence edge."')
    cont = [i for i, b in pf.coverage.consumed.items() if b == BRANCH_PROP_MAPPING_CONT]
    assert len(cont) == 1
    assert unaccounted_lines(pf.coverage) == []


P4_UNCHANGED = """\
# P4 [SPEC:P4-DOC]

## [P4-A] the P4 nested-list form is untouched

- tags:
  - one
  - two
- see-also: [P4-DOC]
"""


def test_p4_nested_list_is_untouched_by_p5():
    pf = parse_text(P4_UNCHANGED, "p4.lts")
    node = next(n for n in pf.nodes if n.id == "P4-A")
    assert node.properties["tags"] == ["one", "two"]
    assert isinstance(node.properties["tags"], list)
    items = [i for i, b in pf.coverage.consumed.items() if b == BRANCH_PROP_NESTED_ITEM]
    assert len(items) == 2
    assert pf.findings == []


P5_MIXED = """\
# MIX [SPEC:MIX-DOC]

## [MIX-A] bullets and mapping entries under one head

- context_for_change:
  - prior_state: "a P4 bullet"
    reasoning: "and an unbulleted mapping entry, under the same key"
"""

P5_MIXED_MAPPING_FIRST = """\
# MIX2 [SPEC:MIX2-DOC]

## [MIX2-A] mapping entries then a bullet under one head

- context_for_change:
    prior_state: "an unbulleted mapping entry"
  - reasoning: "and then a P4 bullet, under the same key"
"""


@pytest.mark.parametrize("text,label,first,second", [
    (P5_MIXED, "mix.lts", "list", "mapping"),
    (P5_MIXED_MAPPING_FIRST, "mix2.lts", "mapping", "list"),
])
def test_mixed_block_is_a_finding_and_never_a_guess(text, label, first, second):
    """A sequence and a mapping at the same level is invalid YAML, and either reading
    silently discards the other half. So: EXACTLY one finding, the property is NOT set,
    and the head stays unconsumed so the coverage lint reports it too. Two independent
    alarms for one shape — the class of bug being closed was audible in neither."""
    pf = parse_text(text, label)
    node = pf.nodes[-1]
    assert "context_for_change" not in node.properties
    assert len(pf.findings) == 1
    (finding,) = pf.findings
    assert finding.kind == FINDING_MIXED_PROPERTY_BLOCK
    assert finding.key == "context_for_change"
    assert finding.node_id == node.id
    assert finding.source_file == label
    assert finding.source_line == _line_of(text, "- context_for_change:")
    assert finding.detail["form_seen_first"] == first
    assert finding.detail["form_seen_second"] == second
    # ...and the head is STILL a coverage finding, not silently absorbed
    assert [v.detail["variant"] for v in unaccounted_lines(pf.coverage)] == ["empty_block"]


P5_NESTED = """\
# NEST [SPEC:NEST-DOC]

## [NEST-A] an entry indented deeper than its siblings

- context_for_change:
    prior_state: "at the entry column"
      reasoning: "one column deeper — a mapping of mappings, which is not in the model"
"""


def test_deeper_entry_shaped_line_is_a_finding_not_a_nested_mapping():
    pf = parse_text(P5_NESTED, "nest.lts")
    node = next(n for n in pf.nodes if n.id == "NEST-A")
    assert "context_for_change" not in node.properties
    assert len(pf.findings) == 1
    assert pf.findings[0].kind == FINDING_NESTED_MAPPING_BLOCK
    assert pf.findings[0].detail["entry_indent"] == 4
    assert pf.findings[0].detail["line_indent"] == 6


def test_p5_mapping_mints_no_edges():
    """§8.2's `context_for_task` carries a `depends_on:` SUB-FIELD and §8.1's fields
    quote node ids in prose. Harvesting refs out of a mapping would mint edges no
    corpus authored — the five-corpus edge-count identity is the receipt."""
    text = """\
# E [SPEC:E-DOC]

## [E-A] a mapping whose sub-fields name other nodes

- context_for_task:
    depends_on: "[E-DOC]"
    origin: "see [E-DOC]"
"""
    pf = parse_text(text, "e.lts")
    assert next(n for n in pf.nodes if n.id == "E-A").properties["context_for_task"]
    # the ONLY edge is the structural CONTAINS from the h1 doc node
    assert [(e.source, e.target, e.type, e.inference) for e in pf.edges] == [
        ("E-DOC", "E-A", "CONTAINS", "structural")]


def test_p5_mapping_survives_to_graph_index_unchanged():
    """A nested dict in `properties` must serialize through `to_graph_index()` as the
    same dict — the index is what every consumer actually reads."""
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "p5.lts"), "w", encoding="utf-8") as fh:
            fh.write(P5_MAPPING)
        corpus = factlattice.parse(d)
        index = corpus.to_graph_index()
        value = index["nodes"]["P5-A"]["properties"]["context_for_change"]
        assert isinstance(value, dict) and len(value) == 3
        # and it round-trips through JSON, which is how the index is persisted
        assert json.loads(json.dumps(index))["nodes"]["P5-A"]["properties"][
            "context_for_change"] == value


# --- the dogfood corpus: this repo's own decision record --------------------
def test_repo_decision_corpus_has_no_dropped_context_for_change():
    """`specs/50-decisions.lts` is FactLattice's own dogfood corpus. Before P5, all FIVE
    of its `context_for_change` blocks — authored in §8.1's own documented shape, on the
    nodes recording why FactLattice is shaped as it is — were dropped whole."""
    specs = os.path.join(_REPO_ROOT, "specs")
    if not os.path.isdir(specs):
        pytest.skip("specs/ not present")
    corpus = factlattice.parse(specs)
    decisions = [n for n in corpus.nodes
                 if n.source_file.endswith("50-decisions.lts") and n.id.startswith("DEC-")]
    # 5 at P5; 6 since [DEC-SEMANTIC-ASSIST] (the maintainer-ratified 2026-10-02) amended [DEC-NO-LLM]
    assert len(decisions) == 6
    for node in decisions:
        value = node.properties["context_for_change"]
        assert isinstance(value, dict), node.id
        # every one of §8.1's seven fields, keyed and queryable
        assert set(value) == {
            "prior_state", "reasoning", "rejected_alternative", "counter_argument",
            "counter_response", "invalidating_assumption", "implication",
        }, node.id
        assert value["invalidating_assumption"].strip('"').startswith("That ")
    # and the file is now FULLY accounted for (SPEC §2.5) — it was 5 findings
    cov = next(c for c in corpus.coverage if c.source_file.endswith("50-decisions.lts"))
    assert unaccounted_lines(cov) == []
    assert corpus.property_findings == []


# ===========================================================================
# (WRAP) A PROPERTY VALUE THAT WRAPS ACROSS PHYSICAL LINES
# ===========================================================================
WRAPPED = """\
# WRAP [SPEC:WRAP-DOC]

## [WRAP-A] a scalar value and a list item both wrap

- requirement: a participant MUST be able to express its inbound interest ONCE, in
  terms that name no substrate, and MUST receive an answer stating what was
  achieved and WHERE
- scenario:
  - when: a device declares two patterns and is attached to an MQTT transport,
    and on another day to a CAN transport
  - then: the declared set is byte-identical in both configs
- see-also: [WRAP-DOC]

## [WRAP-B] furniture under a valued head is not a continuation

- definition: three of them, listed below
  - the sub-bullet is new structure
- note: a table closes the value too

| Layer | What it does |
|-------|--------------|
| prose | decoration |
"""


def test_wrapped_scalar_value_is_joined_not_truncated():
    """61 of 61 wrapped properties in `41-example-wrapped-values.lts`
    were truncated to their first physical line, and line accounting scored the tail as
    ignorable prose — so the instrument agreed with the defect."""
    pf = parse_text(WRAPPED, "wrap.lts")
    node = next(n for n in pf.nodes if n.id == "WRAP-A")
    assert node.properties["requirement"] == (
        "a participant MUST be able to express its inbound interest ONCE, in terms "
        "that name no substrate, and MUST receive an answer stating what was achieved "
        "and WHERE")
    cont = [i for i, b in pf.coverage.consumed.items() if b == BRANCH_PROP_CONT]
    assert len(cont) == 2          # exactly the two tail lines of `requirement`
    assert unaccounted_lines(pf.coverage) == []


def test_a_wrapped_list_item_no_longer_swallows_its_siblings():
    """The severe half of the same defect: the unrecognised continuation line CLOSED the
    P4 block, so `then` was not truncated — it was dropped entirely, along with every
    later item."""
    pf = parse_text(WRAPPED, "wrap.lts")
    node = next(n for n in pf.nodes if n.id == "WRAP-A")
    assert node.properties["scenario"] == [
        "when: a device declares two patterns and is attached to an MQTT transport, "
        "and on another day to a CAN transport",
        "then: the declared set is byte-identical in both configs",
    ]


def test_the_fold_stops_at_furniture_and_at_the_next_property():
    """A bullet, a table and a column-0 `- key:` each OPEN their own structure. Folding
    them would swallow the P4 sub-list idiom, which is everywhere in the corpora."""
    pf = parse_text(WRAPPED, "wrap.lts")
    a = next(n for n in pf.nodes if n.id == "WRAP-A")
    b = next(n for n in pf.nodes if n.id == "WRAP-B")
    assert a.properties["see-also"] == "[WRAP-DOC]"           # not folded into `scenario`
    assert b.properties["definition"] == "three of them, listed below"
    assert b.properties["note"] == "a table closes the value too"
    assert "id_table" not in b.properties                     # `| Layer |` is decoration


WRAPPED_FENCE = """\
# WRAPF [SPEC:WRAPF-DOC]

## [WRAPF-A] a fence under a valued head is not a continuation

- example: the code below is a fence, not a wrapped sentence
  ```python
  value = "- not: a property"
  ```
- status: active
"""


def test_the_fold_never_crosses_a_code_fence():
    pf = parse_text(WRAPPED_FENCE, "wrapf.lts")
    node = next(n for n in pf.nodes if n.id == "WRAPF-A")
    assert node.properties["example"] == "the code below is a fence, not a wrapped sentence"
    assert node.properties["status"] == "active"


# ===========================================================================
# (TBL) A TABLE WHOSE FIRST COLUMN IS AN IDENTIFIER COLUMN IS DATA
# ===========================================================================
TABLES = """\
# TBL [SPEC:TBL-DOC]

## [TBL-A] an acceptance contract authored as a table

| ID | Requirement | verify.machine |
|----|-------------|----------------|
| T-C1 | the superset invariant holds | test_superset |
| T-C2 | the report names the enforcement point | test_report |

- part-of: [TBL-DOC]

## [TBL-B] a decorative table is still decoration

| Layer | Where it lives | What it does |
|-------|----------------|--------------|
| Match rule | `bus.py:54` | prefix matching |
| Pattern grammar | `bus.py:42` | trailing glob only |

## [TBL-C] a first column that repeats is not an identifier column

| ID | Note |
|----|------|
| same | one |
| same | two |

## An untagged heading — the table below has no node of its own

| ID | Requirement |
|----|-------------|
| T-C3 | an orphan table ascends to its nearest [ID]-bearing ancestor |
"""


def test_id_table_rows_become_a_mapping_property_keyed_by_the_id_column():
    pf = parse_text(TABLES, "tbl.lts")
    node = next(n for n in pf.nodes if n.id == "TBL-A")
    assert node.properties["id_table"] == {
        "T-C1": "Requirement: the superset invariant holds; verify.machine: test_superset",
        "T-C2": "Requirement: the report names the enforcement point; verify.machine: test_report",
    }
    assert node.properties["part-of"] == "[TBL-DOC]"
    rows = [i for i, b in pf.coverage.consumed.items() if b == BRANCH_ID_TABLE_ROW]
    assert len(rows) == 3          # TBL-A's two, plus the orphan table's one


def test_a_decorative_table_is_not_promoted():
    """Prefer UNDER-claiming: a decorative table wrongly promoted pollutes the graph,
    which is worse than a data table left out."""
    pf = parse_text(TABLES, "tbl.lts")
    assert next(n for n in pf.nodes if n.id == "TBL-B").properties == {}


def test_a_repeated_first_cell_disqualifies_the_column():
    pf = parse_text(TABLES, "tbl.lts")
    assert next(n for n in pf.nodes if n.id == "TBL-C").properties == {}


def test_an_orphan_id_table_lands_on_its_nearest_id_bearing_ancestor():
    """A node body ends at the NEXT heading of any level, so a table under an untagged
    `## Compliance` sits in no node body. A real spec file authored a
    12-row acceptance table exactly this way and it reached the graph as nothing."""
    pf = parse_text(TABLES, "tbl.lts")
    root = next(n for n in pf.nodes if n.id == "TBL-DOC")
    assert root.properties["id_table"] == {
        "T-C3": "Requirement: an orphan table ascends to its nearest [ID]-bearing ancestor",
    }


def test_id_table_cells_mint_no_edges():
    """Same rule as a P5 mapping in `_property_refs`: cells quote ids inside prose, and
    harvesting them would mint edges no corpus authored."""
    pf = parse_text(TABLES, "tbl.lts")
    assert not [e for e in pf.edges if e.target == "TBL-DOC" and e.inference == "property"
                and e.source == "TBL-DOC"]
    assert all(e.type == "CONTAINS" or e.inference != "structural" for e in pf.edges)
    assert [e.target for e in pf.edges if e.type == "PART_OF"] == ["TBL-DOC"]
