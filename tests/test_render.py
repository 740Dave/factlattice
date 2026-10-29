"""[LEC-RENDER] Tests for the RENDER layer — the flow walk, the generic Mermaid
emitter, the new branch vocabulary in the parser, and the freshness gate.

Every emitted diagram goes through `assert_valid_mermaid` (structural invariants
always; the REAL `mermaid.parse()` grammar when FACTLATTICE_MERMAID_NODE_MODULES is
set) — the acceptance bar is "actually parses", not "string-matches".
"""

from __future__ import annotations

import pytest

from factlattice import parse_text
from factlattice.parser import DECISION_PROPERTY, FLOW_PROPERTY_EDGES, REL_PROPERTY_EDGES
from factlattice_engine import LtsEngine, LtsStore, Partition, dispatch
from factlattice_engine.render import (
    FLOW_EDGE_TYPES, escape_label, render_mermaid, safe_node_id, walk_flow,
)

from .mermaid_check import assert_valid_mermaid, check_structure, real_parse_available


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
class _AlwaysFresh:
    def evaluate(self, part):
        from factlattice_engine import FreshnessResult
        return FreshnessResult(part.corpus_id, True, part.files_hash, part.files_hash)


class _AlwaysStale:
    def evaluate(self, part):
        from factlattice_engine import FreshnessResult
        return FreshnessResult(part.corpus_id, False, "sha256:indexed",
                               "sha256:live", "content moved")


def _mk_index(corpus_id, nodes, edges, domain_suffix="flow.test"):
    layers = {}
    for e in edges:
        layers.setdefault(e["layer"], {"edges": []})["edges"].append(e)
    return {
        "source_corpus": {"corpus_id": corpus_id, "domain_suffix": domain_suffix,
                          "files_hash": "sha256:test", "root_paths": []},
        "nodes": nodes, "layers": layers,
        "stats": {"total_nodes": len(nodes), "total_edges": len(edges)},
    }


def _node(nid, title=None, **props):
    return {"id": nid, "title": title or nid, "domain": None, "status": None,
            "properties": dict(props), "source_file": None, "line_range": [],
            "token_count": 1}


def _edge(src, tgt, etype="BEFORE", layer="temporal", label=None):
    e = {"source": src, "target": tgt, "type": etype, "layer": layer,
         "target_domain": None, "dangling": False, "flags": []}
    if label is not None:
        e["label"] = label
    return e


def _engine(index, freshness=None):
    store = LtsStore()
    store.add_partition(Partition(index))
    return LtsEngine(store, freshness_provider=freshness or _AlwaysFresh())


def _lines(text):
    return [l.strip() for l in text.split("\n") if l.strip()]


# ---------------------------------------------------------------------------
# 1. Parser — the new branch vocabulary (additive + back-compatible)
# ---------------------------------------------------------------------------
FLOW_DOC = """---
domain: flow.test
---
# Deploy Flow [SPEC:FLOW-DOC]

## Deploy [FLOW-DEPLOY]
- definition: The deployment flow

### Build [STEP-BUILD]
- part-of: [FLOW-DEPLOY]
- actor: CI
- next-step: [STEP-GATE]

### Freshness gate [STEP-GATE]
- part-of: [FLOW-DEPLOY]
- actor: CI
- condition: is the corpus fresh?
- if-true: [STEP-SHIP]
- if-false: [STEP-HEAL]

### Ship [STEP-SHIP]
- part-of: [FLOW-DEPLOY]
- actor: Operator
- next-step: [STEP-DONE]

### Self-heal [STEP-HEAL]
- part-of: [FLOW-DEPLOY]
- actor: CI
- converges-to: [STEP-DONE]

### Done [STEP-DONE]
- part-of: [FLOW-DEPLOY]
- actor: Operator
"""


def _flow_edges(parsed, etype="BEFORE"):
    return [e for e in parsed.edges if e.type == etype]


def test_next_step_generates_a_temporal_before_edge():
    parsed = parse_text(FLOW_DOC, "flow.lts")
    e = next(e for e in _flow_edges(parsed)
             if e.source == "STEP-BUILD" and e.target == "STEP-GATE")
    assert e.type == "BEFORE" and e.layer == "temporal"
    assert e.inference == "property" and e.label is None


def test_if_true_if_false_generate_labelled_edges():
    parsed = parse_text(FLOW_DOC, "flow.lts")
    labels = {(e.source, e.target): e.label for e in _flow_edges(parsed)}
    assert labels[("STEP-GATE", "STEP-SHIP")] == "yes"
    assert labels[("STEP-GATE", "STEP-HEAL")] == "no"


def test_condition_is_descriptive_and_generates_no_edge():
    parsed = parse_text(FLOW_DOC, "flow.lts")
    gate = next(n for n in parsed.nodes if n.id == "STEP-GATE")
    assert gate.properties[DECISION_PROPERTY] == "is the corpus fresh?"
    assert DECISION_PROPERTY not in FLOW_PROPERTY_EDGES
    assert DECISION_PROPERTY not in REL_PROPERTY_EDGES
    # STEP-GATE's ONLY outgoing sequence edges are the two authored branches — the
    # `condition` prose minted nothing of its own.
    out = sorted((e.target, e.label) for e in _flow_edges(parsed)
                 if e.source == "STEP-GATE")
    assert out == [("STEP-HEAL", "no"), ("STEP-SHIP", "yes")]
    # ...and a `condition` whose prose HAPPENS to contain a ref is still inert.
    inert = parse_text(
        "# D [SPEC:D]\n\n## G [STEP-G]\n- condition: has [STEP-H] finished?\n\n## H [STEP-H]\n",
        "c.lts")
    assert [e for e in inert.edges if e.layer == "temporal"] == []


def test_branches_multiway_labels_each_edge():
    doc = """# D [SPEC:D]

## Router [STEP-ROUTE]
- condition: what colour is the light?
- branches:
    - green: [STEP-GO]
    - amber: [STEP-SLOW]
    - red: [STEP-STOP]

## Go [STEP-GO]
## Slow [STEP-SLOW]
## Stop [STEP-STOP]
"""
    parsed = parse_text(doc, "b.lts")
    got = {(e.target, e.label) for e in _flow_edges(parsed) if e.source == "STEP-ROUTE"}
    assert got == {("STEP-GO", "green"), ("STEP-SLOW", "amber"), ("STEP-STOP", "red")}


def test_branches_inline_form_is_unlabelled_not_guessed():
    doc = "# D [SPEC:D]\n\n## R [STEP-R]\n- branches: [STEP-A], [STEP-B]\n\n## A [STEP-A]\n## B [STEP-B]\n"
    parsed = parse_text(doc, "b.lts")
    labels = {e.target: e.label for e in _flow_edges(parsed) if e.source == "STEP-R"}
    assert labels == {"STEP-A": None, "STEP-B": None}


def test_next_is_NOT_registered_backcompat_guard():
    """`next` is live prose in surveyed corpora and its prose CONTAINS refs.
    Registering it would retroactively mint temporal edges in existing files."""
    assert "next" not in FLOW_PROPERTY_EDGES and "next" not in REL_PROPERTY_EDGES
    doc = ("# D [SPEC:D]\n\n## N [DEC-1]\n"
           "- next: the browser tier arrives with ([DEC-2]) later\n\n## M [DEC-2]\n")
    parsed = parse_text(doc, "n.lts")
    assert [e for e in parsed.edges if e.layer == "temporal"] == []


def test_edge_to_dict_omits_label_when_absent_byte_identical_index():
    doc = "# D [SPEC:D]\n\n## A [DEC-1]\n- depends-on: [DEC-2]\n\n## B [DEC-2]\n"
    parsed = parse_text(doc, "d.lts")
    for e in parsed.edges:
        assert "label" not in e.to_dict()


def test_existing_corpus_shapes_parse_identically():
    """Every pre-existing property/edge form still yields exactly what it used to."""
    doc = ("# D [SPEC:D]\n\n## A [DEC-1]\n- depends-on: [DEC-2]\n- supersedes: [DEC-3]\n"
           "- part-of: [SPEC:D]\n- used-by: [DEC-2]\n\n## B [DEC-2]\n## C [DEC-3]\n")
    parsed = parse_text(doc, "d.lts")
    got = {(e.source, e.target, e.type) for e in parsed.edges}
    assert ("DEC-1", "DEC-2", "DEPENDS_ON") in got
    assert ("DEC-1", "DEC-3", "SUPERSEDES") in got
    assert ("DEC-1", "D", "PART_OF") in got
    assert ("DEC-2", "DEC-1", "USED_BY") in got
    assert not any(e.type == "BEFORE" for e in parsed.edges)


# ---------------------------------------------------------------------------
# 2. Escaping — the most likely source of silent breakage
# ---------------------------------------------------------------------------
NASTY = 'He said "go" (fast) [now] {maybe} A→B & C <tag> #hash'


def test_escape_label_neutralises_every_dangerous_char():
    out = escape_label(NASTY)
    assert '"' not in out
    assert "&" not in out and "<" not in out and ">" not in out
    assert "#quot;" in out and "#amp;" in out and "#lt;" in out and "#gt;" in out
    assert "#35;hash" in out              # the pre-existing '#' escaped FIRST
    assert "→" in out                     # unicode survives inside a quoted label
    assert "(fast)" in out and "[now]" in out  # quoting is what makes these safe


def test_escape_label_collapses_newlines_and_tabs():
    assert escape_label("a\nb\tc\r\nd") == "a b c d"


def test_render_with_nasty_titles_is_valid_mermaid():
    nodes = [_node("STEP-A", NASTY), _node("STEP-B", 'trailing " quote'),
             _node("STEP-C", "cond node", condition='if x > 1 && y "high"')]
    edges = [_edge("STEP-A", "STEP-B", label=NASTY), _edge("STEP-B", "STEP-C")]
    d = render_mermaid(nodes, edges)
    assert_valid_mermaid(d.text)
    # Every `"` in the output is a DELIMITER we emitted, never one from the corpus:
    # each label span opens and closes exactly one pair, so the count is even and no
    # label body contains a quote.
    for line in _lines(d.text)[1:]:
        assert line.count('"') % 2 == 0
    assert "#quot;" in d.text and "→" in d.text


def test_safe_node_id_maps_hyphens_and_disambiguates():
    used = {}
    a = safe_node_id("STEP-A", used); used[a] = "STEP-A"
    b = safe_node_id("STEP_A", used); used[b] = "STEP_A"
    assert a == "STEP_A" and b == "STEP_A__2" and a != b


def test_hyphenated_ids_never_emit_a_bare_dash():
    d = render_mermaid([_node("A-1"), _node("A-2")], [_edge("A-1", "A-2")])
    assert_valid_mermaid(d.text)
    assert d.node_ids == {"A-1": "A_1", "A-2": "A_2"}


# ---------------------------------------------------------------------------
# 3. (a) linear flow
# ---------------------------------------------------------------------------
@pytest.fixture
def linear_engine():
    nodes = {f"STEP-{i}": _node(f"STEP-{i}", f"Step {i}") for i in (1, 2, 3)}
    edges = [_edge("STEP-1", "STEP-2"), _edge("STEP-2", "STEP-3")]
    return _engine(_mk_index("lin", nodes, edges))


def test_linear_flow_renders_ordered_mermaid(linear_engine):
    res = linear_engine.flow(corpus="lin", start_node="STEP-1")
    assert res["success"] is True
    assert_valid_mermaid(res["mermaid"])
    assert _lines(res["mermaid"]) == [
        "flowchart TD",
        'STEP_1["Step 1"]',
        'STEP_2["Step 2"]',
        'STEP_3["Step 3"]',
        "STEP_1 --> STEP_2",
        "STEP_2 --> STEP_3",
    ]
    # the STRUCTURED half of the answer, for the agent
    assert [n["id"] for n in res["nodes"]] == ["STEP-1", "STEP-2", "STEP-3"]
    assert [(e["source"], e["target"]) for e in res["edges"]] == [
        ("STEP-1", "STEP-2"), ("STEP-2", "STEP-3")]


def test_flow_layout_direction_is_honoured(linear_engine):
    res = linear_engine.flow(corpus="lin", start_node="STEP-1", layout="LR")
    assert res["mermaid"].splitlines()[0] == "flowchart LR"
    assert_valid_mermaid(res["mermaid"])


def test_flow_missing_start_node_is_a_plain_note(linear_engine):
    res = linear_engine.flow(corpus="lin", start_node="NOPE")
    assert res["success"] is True and res["mermaid"] is None
    assert "not found" in res["note"]


def test_flow_missing_corpus_is_plain_not_found(linear_engine):
    res = linear_engine.flow(corpus="ghost", start_node="STEP-1")
    assert res["success"] is False and "not found" in res["error"]


# ---------------------------------------------------------------------------
# 4. (b) binary branch -> diamond + two labelled edges
# ---------------------------------------------------------------------------
@pytest.fixture
def branch_engine():
    nodes = {
        "STEP-GATE": _node("STEP-GATE", "Freshness gate",
                           condition="is the corpus fresh?"),
        "STEP-SHIP": _node("STEP-SHIP", "Ship"),
        "STEP-HEAL": _node("STEP-HEAL", "Self-heal"),
    }
    edges = [_edge("STEP-GATE", "STEP-SHIP", label="yes"),
             _edge("STEP-GATE", "STEP-HEAL", label="no")]
    return _engine(_mk_index("br", nodes, edges))


def test_binary_branch_renders_diamond_and_two_labelled_edges(branch_engine):
    res = branch_engine.flow(corpus="br", start_node="STEP-GATE")
    text = res["mermaid"]
    assert_valid_mermaid(text)
    lines = _lines(text)
    # a `condition` node is a DIAMOND labelled with the question it asks
    assert 'STEP_GATE{"is the corpus fresh?"}' in lines
    assert 'STEP_SHIP["Ship"]' in lines
    assert 'STEP_GATE -->|"yes"| STEP_SHIP' in lines
    assert 'STEP_GATE -->|"no"| STEP_HEAL' in lines
    assert res["diagram"]["edge_count"] == 2


# ---------------------------------------------------------------------------
# 5. (c) multi-way `branches` -> N labelled edges
# ---------------------------------------------------------------------------
def test_multiway_branches_render_n_labelled_edges():
    nodes = {"STEP-ROUTE": _node("STEP-ROUTE", "Router",
                                 condition="what colour is the light?")}
    edges = []
    for label, tgt in (("green", "STEP-GO"), ("amber", "STEP-SLOW"),
                       ("red", "STEP-STOP"), ("blue", "STEP-ODD")):
        nodes[tgt] = _node(tgt, tgt.title())
        edges.append(_edge("STEP-ROUTE", tgt, label=label))
    res = _engine(_mk_index("mw", nodes, edges)).flow(corpus="mw",
                                                      start_node="STEP-ROUTE")
    text = res["mermaid"]
    assert_valid_mermaid(text)
    lines = _lines(text)
    assert 'STEP_ROUTE{"what colour is the light?"}' in lines
    for label, tgt in (("green", "STEP_GO"), ("amber", "STEP_SLOW"),
                       ("red", "STEP_STOP"), ("blue", "STEP_ODD")):
        assert f'STEP_ROUTE -->|"{label}"| {tgt}' in lines
    assert res["diagram"]["edge_count"] == 4


# ---------------------------------------------------------------------------
# 6. (d) convergence — two paths into one node
# ---------------------------------------------------------------------------
def test_convergence_two_paths_into_one_node():
    nodes = {n: _node(n, n) for n in
             ("STEP-GATE", "STEP-SHIP", "STEP-HEAL", "STEP-DONE")}
    nodes["STEP-GATE"]["properties"]["condition"] = "fresh?"
    edges = [_edge("STEP-GATE", "STEP-SHIP", label="yes"),
             _edge("STEP-GATE", "STEP-HEAL", label="no"),
             _edge("STEP-SHIP", "STEP-DONE"),
             _edge("STEP-HEAL", "STEP-DONE")]
    res = _engine(_mk_index("cv", nodes, edges)).flow(corpus="cv",
                                                      start_node="STEP-GATE")
    text = res["mermaid"]
    assert_valid_mermaid(text)
    lines = _lines(text)
    assert "STEP_SHIP --> STEP_DONE" in lines
    assert "STEP_HEAL --> STEP_DONE" in lines
    # STEP-DONE is declared EXACTLY once despite two arrows into it
    assert sum(1 for l in lines if l.startswith('STEP_DONE["')) == 1
    assert sum(1 for l in lines if l.endswith(" STEP_DONE")) == 2


def test_convergence_via_converges_to_property_end_to_end():
    """The authored `converges-to` vocabulary produces the same convergence."""
    parsed = parse_text(FLOW_DOC, "flow.lts")
    heal = [(e.source, e.target) for e in parsed.edges
            if e.source == "STEP-HEAL" and e.type == "BEFORE"]
    ship = [(e.source, e.target) for e in parsed.edges
            if e.source == "STEP-SHIP" and e.type == "BEFORE"]
    assert heal == [("STEP-HEAL", "STEP-DONE")]
    assert ship == [("STEP-SHIP", "STEP-DONE")]


# ---------------------------------------------------------------------------
# 7. (e) `actor` grouping -> subgraph swimlanes
# ---------------------------------------------------------------------------
def test_actor_grouping_emits_subgraphs():
    nodes = {"STEP-1": _node("STEP-1", "Build", actor="CI"),
             "STEP-2": _node("STEP-2", "Approve", actor="Operator"),
             "STEP-3": _node("STEP-3", "Deploy", actor="CI")}
    edges = [_edge("STEP-1", "STEP-2"), _edge("STEP-2", "STEP-3")]
    res = _engine(_mk_index("sw", nodes, edges)).flow(corpus="sw",
                                                      start_node="STEP-1")
    text = res["mermaid"]
    assert_valid_mermaid(text)
    lines = _lines(text)
    assert 'subgraph grp_0["CI"]' in lines
    assert 'subgraph grp_1["Operator"]' in lines
    assert lines.count("end") == 2
    assert res["groups"] == ["CI", "Operator"]
    # both CI steps land in the FIRST lane, in walk order
    ci = lines[lines.index('subgraph grp_0["CI"]') + 1:lines.index("end")]
    assert ci == ['STEP_1["Build"]', 'STEP_3["Deploy"]']


def test_grouping_is_skipped_when_no_node_carries_the_property():
    nodes = {"STEP-1": _node("STEP-1", "a"), "STEP-2": _node("STEP-2", "b")}
    res = _engine(_mk_index("ng", nodes, [_edge("STEP-1", "STEP-2")])).flow(
        corpus="ng", start_node="STEP-1")
    assert "subgraph" not in res["mermaid"]
    assert res["diagram"]["group_by"] is None
    assert_valid_mermaid(res["mermaid"])


def test_group_by_can_be_pointed_at_any_property():
    nodes = {"A": _node("A", "a", tier="edge"), "B": _node("B", "b", tier="core")}
    res = _engine(_mk_index("gb", nodes, [_edge("A", "B")])).flow(
        corpus="gb", start_node="A", group_by="tier")
    assert 'subgraph grp_0["edge"]' in _lines(res["mermaid"])
    assert_valid_mermaid(res["mermaid"])


# ---------------------------------------------------------------------------
# 8. membership walk + container handling
# ---------------------------------------------------------------------------
def test_flow_walks_part_of_membership_from_a_container():
    nodes = {"FLOW-D": _node("FLOW-D", "Deploy")}
    edges = []
    for i in (1, 2, 3):
        nodes[f"STEP-{i}"] = _node(f"STEP-{i}", f"Step {i}")
        edges.append({"source": f"STEP-{i}", "target": "FLOW-D", "type": "PART_OF",
                      "layer": "semantic", "target_domain": None,
                      "dangling": False, "flags": []})
    edges += [_edge("STEP-1", "STEP-2"), _edge("STEP-2", "STEP-3")]
    res = _engine(_mk_index("mem", nodes, edges)).flow(corpus="mem",
                                                       start_node="FLOW-D")
    assert res["container"] == "FLOW-D"
    assert "FLOW_D" not in res["mermaid"]          # a pure container is not drawn
    assert [n["id"] for n in res["nodes"]][0] == "FLOW-D"   # but IS in the walk
    assert len(res["membership_edges"]) == 3
    assert_valid_mermaid(res["mermaid"])
    assert _lines(res["mermaid"]) == [
        "flowchart TD", 'STEP_1["Step 1"]', 'STEP_2["Step 2"]', 'STEP_3["Step 3"]',
        "STEP_1 --> STEP_2", "STEP_2 --> STEP_3"]


def test_include_container_draws_it():
    nodes = {"FLOW-D": _node("FLOW-D", "Deploy"), "STEP-1": _node("STEP-1", "S1")}
    edges = [{"source": "FLOW-D", "target": "STEP-1", "type": "CONTAINS",
              "layer": "entity", "target_domain": None, "dangling": False, "flags": []}]
    res = _engine(_mk_index("ic", nodes, edges)).flow(
        corpus="ic", start_node="FLOW-D", include_container=True)
    assert 'FLOW_D["Deploy"]' in _lines(res["mermaid"])
    assert_valid_mermaid(res["mermaid"])


def test_max_depth_bounds_the_walk():
    nodes = {f"S{i}": _node(f"S{i}", f"s{i}") for i in range(6)}
    edges = [_edge(f"S{i}", f"S{i+1}") for i in range(5)]
    eng = _engine(_mk_index("md", nodes, edges))
    res = eng.flow(corpus="md", start_node="S0", max_depth=2)
    assert [n["id"] for n in res["nodes"]] == ["S0", "S1", "S2"]
    assert res["max_depth_reached"] is True


def test_token_budget_truncates_and_induces_edges_on_survivors():
    nodes = {f"S{i}": _node(f"S{i}", f"s{i}") for i in range(5)}
    edges = [_edge(f"S{i}", f"S{i+1}") for i in range(4)]
    res = _engine(_mk_index("tb", nodes, edges)).flow(
        corpus="tb", start_node="S0", token_budget=3)
    assert res["truncated"] is True and res["total"] == 5
    ids = {n["id"] for n in res["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in res["edges"])
    assert_valid_mermaid(res["mermaid"])


# ---------------------------------------------------------------------------
# 9. (f) STALE corpus -> the loud marker, NEVER a diagram
# ---------------------------------------------------------------------------
def test_stale_corpus_returns_marker_and_no_diagram():
    nodes = {"STEP-1": _node("STEP-1", "a"), "STEP-2": _node("STEP-2", "b")}
    eng = _engine(_mk_index("st", nodes, [_edge("STEP-1", "STEP-2")]),
                  freshness=_AlwaysStale())
    res = eng.flow(corpus="st", start_node="STEP-1")
    assert res["success"] is False and res["stale"] is True
    assert "INV-LTS-FRESH" in res["error"]
    assert "mermaid" not in res and "nodes" not in res


def test_stale_corpus_marker_also_via_dispatch():
    nodes = {"STEP-1": _node("STEP-1", "a")}
    eng = _engine(_mk_index("st2", nodes, []), freshness=_AlwaysStale())
    res = dispatch("lts_flow", {"corpus": "st2", "start_node": "STEP-1"}, tools=eng)
    assert res["success"] is False and res["stale"] is True and "mermaid" not in res


def test_stale_corpus_self_heals_then_renders():
    """[LEC-SELF-HEAL] composes: flow routes through the SAME `_gate()`, so an
    injected healer makes a stale corpus TRANSIENT for the render layer too."""
    nodes = {"STEP-1": _node("STEP-1", "a"), "STEP-2": _node("STEP-2", "b")}
    idx = _mk_index("sh", nodes, [_edge("STEP-1", "STEP-2")])

    class _HealsOnce:
        def __init__(self):
            self.calls = 0

        def evaluate(self, part):
            from factlattice_engine import FreshnessResult
            fresh = self.calls > 0
            return FreshnessResult(part.corpus_id, fresh, "a", "a" if fresh else "b",
                                   None if fresh else "moved")

    class _Healer:
        def __init__(self, provider):
            self.provider = provider
            self.healed = 0

        def heal(self, part):
            self.provider.calls += 1
            self.healed += 1
            class _O:
                def to_dict(self_inner):
                    return {"rebuilt": True}
            return _O()

    provider = _HealsOnce()
    healer = _Healer(provider)
    store = LtsStore()
    store.add_partition(Partition(idx))
    eng = LtsEngine(store, freshness_provider=provider, healer=healer)
    res = eng.flow(corpus="sh", start_node="STEP-1")
    assert healer.healed == 1
    assert res["success"] is True and res["mermaid"]
    assert_valid_mermaid(res["mermaid"])


# ---------------------------------------------------------------------------
# 10. (g) the emitter is REUSABLE on a non-flow subgraph
# ---------------------------------------------------------------------------
def test_emitter_renders_a_supersession_chain_directly():
    nodes = [_node("DEC-1", "Original ruling"), _node("DEC-2", "Revised ruling"),
             _node("DEC-3", "Current ruling")]
    edges = [_edge("DEC-2", "DEC-1", etype="SUPERSEDES", layer="semantic"),
             _edge("DEC-3", "DEC-2", etype="SUPERSEDES", layer="semantic")]
    d = render_mermaid(nodes, edges, direction="LR")
    assert_valid_mermaid(d.text)
    lines = _lines(d.text)
    # no flow vocabulary anywhere: the edge TYPE auto-labels the arrow
    assert 'DEC_2 -->|"SUPERSEDES"| DEC_1' in lines
    assert 'DEC_3 -->|"SUPERSEDES"| DEC_2' in lines
    assert all("{" not in l for l in lines)  # nothing is a decision diamond


def test_flow_tool_renders_a_supersession_chain_via_edge_types():
    """The SAME gated tool, pointed at a different relation family."""
    nodes = {n: _node(n, t) for n, t in
             (("DEC-1", "Original"), ("DEC-2", "Revised"), ("DEC-3", "Current"))}
    edges = [_edge("DEC-1", "DEC-2", etype="SUPERSEDES", layer="semantic"),
             _edge("DEC-2", "DEC-3", etype="SUPERSEDES", layer="semantic")]
    res = _engine(_mk_index("ss", nodes, edges)).flow(
        corpus="ss", start_node="DEC-1", edge_types=["SUPERSEDES"], layout="LR")
    assert_valid_mermaid(res["mermaid"])
    assert [n["id"] for n in res["nodes"]] == ["DEC-1", "DEC-2", "DEC-3"]
    assert 'DEC_1 -->|"SUPERSEDES"| DEC_2' in _lines(res["mermaid"])


def test_flow_tool_renders_a_dependency_tree_via_edge_types():
    nodes = {n: _node(n, n) for n in ("SVC-A", "SVC-B", "SVC-C")}
    edges = [_edge("SVC-A", "SVC-B", etype="DEPENDS_ON", layer="semantic"),
             _edge("SVC-A", "SVC-C", etype="DEPENDS_ON", layer="semantic")]
    res = _engine(_mk_index("dt", nodes, edges)).flow(
        corpus="dt", start_node="SVC-A", edge_types=["DEPENDS_ON"])
    assert_valid_mermaid(res["mermaid"])
    assert res["diagram"]["edge_count"] == 2


def test_emitter_drops_edges_whose_endpoint_is_absent():
    d = render_mermaid([_node("A")], [_edge("A", "GHOST")])
    assert_valid_mermaid(d.text)          # would declare a phantom node otherwise
    assert d.edge_count == 0 and len(d.skipped_edges) == 1


def test_walk_flow_is_pure_and_reusable_without_a_corpus():
    """The walk takes plain callables + adjacency maps — no store, no I/O."""
    nodes = {"A": _node("A"), "B": _node("B")}
    e = _edge("A", "B")
    fwd = {"A": [("temporal", e)]}
    rev = {"B": [("temporal", e)]}
    walk = walk_flow(start_node="A", get_node=nodes.get,
                     has_node=lambda n: n in nodes, fwd_adj=fwd, rev_adj=rev)
    assert walk.node_ids() == ["A", "B"] and walk.edges == [e]
    assert "BEFORE" in FLOW_EDGE_TYPES and "AFTER" not in FLOW_EDGE_TYPES


# ---------------------------------------------------------------------------
# 11. dispatch / tool-surface wiring
# ---------------------------------------------------------------------------
def test_dispatch_lts_flow(linear_engine):
    res = dispatch("lts_flow", {"corpus": "lin", "start_node": "STEP-1"},
                   tools=linear_engine)
    assert res["success"] and res["mermaid"].startswith("flowchart TD")


def test_dispatch_lts_flow_missing_arg(linear_engine):
    res = dispatch("lts_flow", {"corpus": "lin"}, tools=linear_engine)
    assert res["success"] is False and "missing required argument" in res["error"]


def test_lts_flow_is_in_the_advertised_tool_surface():
    from factlattice_engine import LTS_ALL_TOOL_NAMES, LTS_RENDER_TOOL_NAMES
    from factlattice_engine.server import TOOLS
    assert "lts_flow" in LTS_RENDER_TOOL_NAMES
    assert "lts_flow" in LTS_ALL_TOOL_NAMES
    schema = next(t for t in TOOLS if t["name"] == "lts_flow")
    assert schema["inputSchema"]["required"] == ["corpus", "start_node"]


# ---------------------------------------------------------------------------
# 12. the validator itself must be able to FAIL (else it proves nothing)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    'flowchart TD\n    A["unterminated',
    'flowchart TD\n    A["ok"]\n    A --> GHOST',
    'flowchart TD\n    subgraph g0["x"]\n    A["ok"]',
    'graph TD\n    A["ok"]',
    'flowchart TD\n    A["bare # hash"]',
    'flowchart TD\n    A["mismatch")',
])
def test_structure_checker_rejects_broken_diagrams(bad):
    from .mermaid_check import MermaidStructureError
    with pytest.raises(MermaidStructureError):
        check_structure(bad)


@pytest.mark.skipif(not real_parse_available(),
                    reason="set FACTLATTICE_MERMAID_NODE_MODULES to an "
                           "`npm i mermaid jsdom` project to run the real grammar")
def test_real_mermaid_parser_actually_rejects_broken_diagrams():
    """Proves the real-parse leg is not a no-op: if it accepted everything, every
    `assert_valid_mermaid` above would be structural-only."""
    from .mermaid_check import real_parse
    results = real_parse(['flowchart TD\n    A["ok"]',
                          'flowchart TD\n    A["unterminated',
                          'flowchart TD\n    A -- >> B'])
    assert [r["ok"] for r in results] == [True, False, False]


def test_report_which_validation_level_ran():
    # Not an assertion about the engine — it makes the report honest about whether
    # the REAL mermaid grammar was exercised in this run.
    print(f"\n[LEC-RENDER] real mermaid.parse() available: {real_parse_available()}")
