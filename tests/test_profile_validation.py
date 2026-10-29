"""PROFILE-DRIVEN VALIDATION V1-V11 — the consumer `[PROFILE-CONTRACT]` admitted was
missing ("profile-driven validation … is specified here but **not yet enforced**").

THREE RULES GOVERN EVERY ASSERTION BELOW
----------------------------------------
1. POSITIVE CONTROL PER VALIDATOR, ASSERTING AN EXACT COUNT. A rule that reports zero
   looks exactly like a rule that works. Every `test_vN_*` below asserts
   `_count(result, "VN") == <a specific integer>` over a hermetic fixture whose
   violations were counted by hand, and every validator also has a NEGATIVE control
   proving it stays silent on the legitimate shape. A validator that fires on
   legitimate content trains authors to switch the gate off ([LAYER-LINT]).

2. THE PROFILE IS AUTHORED AS `.lts`, NOT YAML. The fixture profile below is the
   `[PROFILE-SELFHOST]` serialization the ecosystem is migrating TO. Every validator
   reads it through the same typed accessors, so this file is also the receipt that
   nothing in `profile_rules.py` is coupled to the YAML dict shape — the coupling the
   build brief §3 forbids. (The two serializations' equivalence is asserted
   separately, by fixture, in `test_profile_accessors.py`.)

3. THE DARK PARTS ARE ASSERTED, NOT PAPERED OVER. `test_v8_yaml_mapping_form_is_dark`
   and `test_v7_yaml_mapping_confidence_is_invisible` author SPEC §8.1's and §7.1's
   own documented YAML-mapping shape and assert that the parser drops it whole, so V8
   reports `form=absent` and V7 reports nothing at all. That is a limitation of the
   parser, not of these validators; closing it is parser conformance work. Recording
   it as an executable assertion is how it stops being folklore.

SEVERITY is `warning` for all eleven, and `test_every_profile_finding_is_a_warning`
pins it. Eleven new validators against never-checked corpora produce a large day-one
count; landing at `error` fails every gate immediately and the gate gets switched
off, which is precisely the failure `[LAYER-LINT]` warns about.

V11 (`status` is a bare lifecycle token) is the AGREEMENT CHECK for the engine-side
`lts_find(status=…)` lifecycle-token fix; the filter half is tested in
`test_lifecycle_status.py`, and `test_v11_agrees_with_the_filter_it_describes` asserts
the two halves cannot drift apart.
"""
from __future__ import annotations

import os
import tempfile

import pytest

import factlattice
from factlattice_engine.lint import (
    PROFILE_SEVERITY,
    VALIDATORS,
    find_profile_violations,
    lint,
    profile_findings_by_validator,
    profile_is_loaded,
)
from factlattice_engine.lint.profile_rules import (
    CORE_DOCUMENT_TAGS,
    REQUIRED_FRONTMATTER,
)
from factlattice_engine.lifecycle import (
    is_bare_lifecycle_token,
    lifecycle_token,
    status_matches,
)

# ⛔ NO LIVE CORPUS IS READ FROM THIS FILE. The receipts taken against a real
# deployed corpus (its domain id, its path, its pinned finding counts) moved to the
# gitignored `private/tests/` overlay on 2026-08-13: they name a deployment, and this
# is a public suite. Everything below is hermetic — a synthetic profile and fixtures
# written into tmp_path. See `conftest.py` at the repo root for how the overlay is
# collected when it is present.


# ---------------------------------------------------------------------------
# the hermetic domain profile ([PROFILE-SELFHOST] `.lts` serialization)
# ---------------------------------------------------------------------------
TEST_PROFILE_LTS = """\
# Test Domain Profile [SPEC:TEST-PROFILE]

## [PROFILE-DOMAIN] A hermetic domain for the V1-V11 fixtures
- domain-id: test.dom
- domain-name: Test Domain
- namespaces: test.dom

## [PREFIX-DEC] Decision nodes
- registers-prefix: DEC-
- document: decisions
- definition: Load-bearing decisions

## [PREFIX-DOC] Document roots
- registers-prefix: DOC-
- definition: Document-level root nodes

## [DOCTAG-RUNBOOK] Operational runbook
- document-tag: RUNBOOK
- definition: A domain-registered doctype, outside SPEC 1.7's core set

## [ENUM-CONFIDENTIALITY] Confidentiality
- enum-key: confidentiality
- values:
  - public
  - internal-only

## [ENUM-READINESS] Artifact readiness
- enum-key: artifact_readiness
- values:
  - educational
  - system-design

## [NODEFAMILY-DECISION] Decision node schema
- section: decision_nodes
- families: DEC-
- required:
  - rationale
  - lifecycle-position
- recommended:
  - importance

## [LIFECYCLE-DECISION] Decision lifecycle
- section: decision_lifecycle
- pipeline:
  - proposed
  - committed
  - validated
- off-pipeline:
  - superseded
  - withdrawn
"""

FRONTMATTER = """\
---
scope: The hermetic fixture corpus
audience: The V1-V11 validators
boundary: Nothing outside this temp directory
---
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def workspace():
    """`(corpus_dir, profile_path)` — the profile lives OUTSIDE the corpus dir on
    purpose: dropped inside it, `list_lts_files` would ingest the profile as a corpus
    document and its own PROFILE-/PREFIX-/ENUM- nodes would land as V1 findings."""
    with tempfile.TemporaryDirectory(prefix="fl-profile-val-") as root:
        corpus = os.path.join(root, "corpus")
        os.makedirs(corpus)
        profile_path = os.path.join(root, "test-profile.lts")
        with open(profile_path, "w", encoding="utf-8") as fh:
            fh.write(TEST_PROFILE_LTS)
        yield corpus, profile_path


def _write(corpus_dir: str, name: str, text: str) -> None:
    with open(os.path.join(corpus_dir, name), "w", encoding="utf-8") as fh:
        fh.write(text)


def _lint(workspace, **files):
    corpus_dir, profile_path = workspace
    for name, text in files.items():
        _write(corpus_dir, name.replace("_", "-") + ".lts", text)
    return lint(corpus_dir, profile=profile_path)


def _count(result, validator: str) -> int:
    return sum(1 for f in result.findings
               if f.origin == "profile" and f.detail.get("validator") == validator)


def _of(result, validator: str):
    return [f for f in result.findings
            if f.origin == "profile" and f.detail.get("validator") == validator]


# ---------------------------------------------------------------------------
# THE GATE — a corpus with no profile is SILENT, not broken
# ---------------------------------------------------------------------------
def test_no_profile_means_zero_profile_findings_and_no_crash(workspace):
    """Acme's corpora and this repo's own `specs/` load no profile. The pass must
    be INERT for them — zero findings, no exception, and no lifecycle block."""
    corpus_dir, _profile = workspace
    _write(corpus_dir, "doc.lts", "# Bare [WIDGETRY:ZZZ-ROOT]\n\n## [ZZZ-ONE] Node\n- note: n\n")
    result = lint(corpus_dir)            # <- no profile= and none discoverable
    assert result.profile_domain is None
    assert result.by_origin("profile") == []
    assert result.lifecycle is None
    assert result.exit_code() == 0


def test_this_repos_own_specs_corpus_is_silent():
    """`specs/` loads no profile. Every validator must stay silent RATHER THAN CRASH —
    the proof that the gate is a gate and not an accident of the fixtures."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    specs = os.path.join(here, "specs")
    if not os.path.isdir(specs):
        pytest.skip("specs/ not present")
    result = lint(specs)
    assert result.profile_domain is None
    assert result.by_origin("profile") == []
    assert result.lifecycle is None


def test_profile_is_loaded_distinguishes_default_from_real(workspace):
    corpus_dir, profile_path = workspace
    _write(corpus_dir, "doc.lts", FRONTMATTER + "# D [SPEC:DOC-R]\n")
    assert profile_is_loaded(factlattice.parse(corpus_dir, profile=profile_path))
    assert not profile_is_loaded(factlattice.parse(corpus_dir))


def test_profile_rules_can_be_switched_off(workspace):
    corpus_dir, profile_path = workspace
    _write(corpus_dir, "doc.lts", "# D [SPEC:WIDGET-R]\n\n## [WIDGET-ONE] x\n- note: n\n")
    on = lint(corpus_dir, profile=profile_path)
    off = lint(corpus_dir, profile=profile_path, profile_rules=False)
    assert on.by_origin("profile") and off.by_origin("profile") == []
    assert off.lifecycle is None


# ---------------------------------------------------------------------------
# V1 — registered node-id prefixes
# ---------------------------------------------------------------------------
V1_FIXTURE = FRONTMATTER + """\
# Prefix fixture [SPEC:DOC-V1]

## [DEC-REGISTERED] A registered family
- rationale: it is in the profile
- lifecycle-position: committed
- importance: high

## [WIDGET-ONE] An unregistered family
- note: WIDGET- is registered by no profile

## [WIDGET-TWO] Another one
- note: two distinct nodes, one unregistered prefix
"""


def test_v1_flags_exactly_the_unregistered_prefixes(workspace):
    result = _lint(workspace, v1=V1_FIXTURE)
    assert _count(result, "V1") == 2
    assert sorted(f.node_id for f in _of(result, "V1")) == ["WIDGET-ONE", "WIDGET-TWO"]
    assert all(f.source_file == "v1.lts" and f.source_line for f in _of(result, "V1"))


def test_v1_is_silent_when_every_prefix_is_registered(workspace):
    text = FRONTMATTER + ("# Clean [SPEC:DOC-CLEAN]\n\n## [DEC-A] a\n- rationale: r\n"
                          "- lifecycle-position: committed\n- importance: high\n")
    assert _count(_lint(workspace, v1clean=text), "V1") == 0


# ---------------------------------------------------------------------------
# V2 — document tag is core (SPEC 1.7) or profile-registered
# ---------------------------------------------------------------------------
def test_v2_flags_one_finding_per_file_not_per_node(workspace):
    """`document_tag` is a FILE-level fact the parser replicates onto every node; a
    per-node report would multiply one authoring mistake by the file's node count."""
    text = FRONTMATTER + """\
# Bad tag [WIDGETRY:DOC-V2]

## [DEC-V2-A] a
- rationale: r
- lifecycle-position: committed
- importance: high

## [DEC-V2-B] b
- rationale: r
- lifecycle-position: committed
- importance: high
"""
    result = _lint(workspace, v2=text)
    assert _count(result, "V2") == 1
    (finding,) = _of(result, "V2")
    assert finding.detail["document_tag"] == "WIDGETRY"
    assert finding.source_file == "v2.lts"


def test_v2_accepts_core_and_profile_registered_tags(workspace):
    core = FRONTMATTER + "# Core tag [GLOSSARY:DOC-V2C]\n\n## [DEC-V2C] a\n- rationale: r\n- lifecycle-position: committed\n- importance: high\n"
    domain = FRONTMATTER + "# Domain tag [RUNBOOK:DOC-V2D]\n\n## [DEC-V2D] a\n- rationale: r\n- lifecycle-position: committed\n- importance: high\n"
    result = _lint(workspace, v2core=core, v2domain=domain)
    assert _count(result, "V2") == 0
    assert "GLOSSARY" in CORE_DOCUMENT_TAGS and "RUNBOOK" not in CORE_DOCUMENT_TAGS


# ---------------------------------------------------------------------------
# V3 / V4 — required + recommended properties per node family
# ---------------------------------------------------------------------------
V3_FIXTURE = FRONTMATTER + """\
# Required-property fixture [SPEC:DOC-V3]

## [DEC-MISSING-BOTH] no rationale and no stage
- importance: high

## [DEC-MISSING-ONE] no rationale
- lifecycle-position: committed
- importance: high

## [DEC-COMPLETE] carries both
- rationale: present
- lifecycle-position: validated
- importance: high

## [WIDGET-NOT-A-DECISION] a family the profile declares no schema for
- note: V3 must not invent requirements for an undeclared family
"""


def test_v3_flags_exactly_one_finding_per_missing_required_property(workspace):
    result = _lint(workspace, v3=V3_FIXTURE)
    assert _count(result, "V3") == 3
    assert sorted((f.node_id, f.detail["property"]) for f in _of(result, "V3")) == [
        ("DEC-MISSING-BOTH", "lifecycle-position"),
        ("DEC-MISSING-BOTH", "rationale"),
        ("DEC-MISSING-ONE", "rationale"),
    ]


def test_v4_flags_exactly_the_missing_recommended_properties(workspace):
    text = FRONTMATTER + """\
# Recommended fixture [SPEC:DOC-V4]

## [DEC-NO-IMPORTANCE] a
- rationale: r
- lifecycle-position: committed

## [DEC-HAS-IMPORTANCE] b
- rationale: r
- lifecycle-position: committed
- importance: high
"""
    result = _lint(workspace, v4=text)
    assert _count(result, "V4") == 1
    (finding,) = _of(result, "V4")
    assert (finding.node_id, finding.detail["property"]) == ("DEC-NO-IMPORTANCE", "importance")


def test_v3_and_v4_do_not_fire_on_a_family_with_no_declared_schema(workspace):
    text = FRONTMATTER + "# X [SPEC:DOC-V34]\n\n## [WIDGET-FREE] no schema declared\n- note: n\n"
    result = _lint(workspace, v34=text)
    assert _count(result, "V3") == 0 and _count(result, "V4") == 0


# ---------------------------------------------------------------------------
# V5 — enum-valued properties (node properties AND frontmatter)
# ---------------------------------------------------------------------------
def test_v5_flags_undeclared_enum_values_in_properties_and_frontmatter(workspace):
    text = """\
---
scope: s
audience: a
boundary: b
confidentiality: sekrit
---
# Enum fixture [SPEC:DOC-V5]

## [DEC-V5-BAD] a
- rationale: r
- lifecycle-position: committed
- importance: high
- artifact-readiness: telepathic

## [DEC-V5-GOOD] b
- rationale: r
- lifecycle-position: committed
- importance: high
- artifact-readiness: educational
- confidentiality: public
"""
    result = _lint(workspace, v5=text)
    assert _count(result, "V5") == 2
    got = sorted((f.detail["where"], f.detail["property"], f.detail["value"])
                 for f in _of(result, "V5"))
    assert got == [("frontmatter", "confidentiality", "sekrit"),
                   ("property", "artifact-readiness", "telepathic")]


def test_v5_does_not_invent_enums_for_undeclared_keys(workspace):
    """`enum_values` returns None for a key the domain does not declare — the
    difference between 'no such enum, skip' and 'declared empty, nothing is valid'."""
    text = FRONTMATTER + ("# X [SPEC:DOC-V5N]\n\n## [DEC-V5N] a\n- rationale: r\n"
                          "- lifecycle-position: committed\n- importance: high\n"
                          "- some-free-text-key: anything at all\n")
    assert _count(_lint(workspace, v5n=text), "V5") == 0


# ---------------------------------------------------------------------------
# V6 — lifecycle-position ∈ pipeline ∪ off_pipeline, AND the query it unlocks
# ---------------------------------------------------------------------------
V6_FIXTURE = FRONTMATTER + """\
# Lifecycle fixture [SPEC:DOC-V6]

## [DEC-V6-PROPOSED] a
- rationale: r
- lifecycle-position: proposed
- importance: high

## [DEC-V6-COMMITTED] b
- rationale: r
- lifecycle-position: committed
- importance: high

## [DEC-V6-VALIDATED] c
- rationale: r
- lifecycle-position: validated
- importance: high

## [DEC-V6-SUPERSEDED] d
- rationale: r
- lifecycle-position: superseded
- importance: high

## [DEC-V6-BOGUS] e
- rationale: r
- lifecycle-position: shipped
- importance: high

## [DEC-V6-UNSTAGED] f
- rationale: r
- importance: high
"""


def test_v6_flags_exactly_the_undeclared_stage(workspace):
    result = _lint(workspace, v6=V6_FIXTURE)
    assert _count(result, "V6") == 1
    (finding,) = _of(result, "V6")
    assert finding.node_id == "DEC-V6-BOGUS"
    assert finding.detail["value"] == "shipped"
    assert set(finding.detail["declared"]) == {
        "proposed", "committed", "validated", "superseded", "withdrawn"}


def test_v6_lifecycle_report_answers_made_but_never_implemented(workspace):
    """THE DELIVERABLE, not a side effect. `decision_lifecycle` declared this query and
    nothing ever served it: *"show me decisions made but never implemented."*"""
    result = _lint(workspace, v6=V6_FIXTURE)
    report = result.lifecycle
    assert report is not None
    assert report["stages"]["pipeline"] == ["proposed", "committed", "validated"]
    assert report["terminal_stage"] == "validated"
    assert report["made_but_not_implemented"] == ["DEC-V6-COMMITTED", "DEC-V6-PROPOSED"]
    assert report["by_stage"]["validated"] == ["DEC-V6-VALIDATED"]
    assert report["by_stage"]["superseded"] == ["DEC-V6-SUPERSEDED"]
    assert report["unknown"] == {"shipped": ["DEC-V6-BOGUS"]}
    # `unstaged` is derived from the profile's OWN `required` list, not from a guess
    # about which prefixes are decisions.
    assert report["unstaged"] == ["DEC-V6-UNSTAGED"]
    assert report["counts"]["made_but_not_implemented"] == 2


def test_lifecycle_report_rides_out_on_to_dict_so_the_mcp_tool_serves_it(workspace):
    """`lts_lint` serializes `LintResult.to_dict()`. The query has to arrive THERE, or
    it is checkable but not queryable — which is the state this work exists to end."""
    result = _lint(workspace, v6=V6_FIXTURE)
    payload = result.to_dict()
    assert payload["lifecycle"]["made_but_not_implemented"] == [
        "DEC-V6-COMMITTED", "DEC-V6-PROPOSED"]
    assert payload["profile_validators"]["V6"] == 1


# ---------------------------------------------------------------------------
# V7 — confidence block conforms (SPEC 7.1)
# ---------------------------------------------------------------------------
V7_FIXTURE = FRONTMATTER + """\
# Confidence fixture [SPEC:DOC-V7]

## [DEC-V7-OUT-OF-RANGE] a
- rationale: r
- lifecycle-position: committed
- importance: high
- confidence:
  - value: 1.4
  - blast_radius: high

## [DEC-V7-NOT-A-FLOAT] b
- rationale: r
- lifecycle-position: committed
- importance: high
- confidence:
  - value: high
  - blast_radius: enormous

## [DEC-V7-CONFORMS] c
- rationale: r
- lifecycle-position: committed
- importance: high
- confidence:
  - value: 0.85
  - blast_radius: medium

## [DEC-V7-INLINE] d
- rationale: r
- lifecycle-position: committed
- importance: high
- confidence: 0.9
"""


def test_v7_flags_exactly_the_nonconforming_confidence_fields(workspace):
    result = _lint(workspace, v7=V7_FIXTURE)
    assert _count(result, "V7") == 3
    assert sorted((f.node_id, f.detail["problem"]) for f in _of(result, "V7")) == [
        ("DEC-V7-NOT-A-FLOAT", "blast_radius_unknown"),
        ("DEC-V7-NOT-A-FLOAT", "value_not_float"),
        ("DEC-V7-OUT-OF-RANGE", "value_out_of_range"),
    ]


def test_v7_yaml_mapping_confidence_reaches_the_graph_but_not_yet_v7(workspace):
    """SPEC §7.1's own example is a YAML MAPPING.

    ⛔ THIS TEST CHANGED WHEN P5 LANDED, AND THE HALF THAT MOVED IS THE POINT.
    It used to assert `confidence` was absent from `node.properties` — the parser had
    no mapping form, so §7.1's documented shape collected nothing and the property was
    dropped whole. P5 (SPEC §2.2 conformance, see `test_p5_*` in test_parser.py) closes
    the PARSER half: the mapping is now a keyed dict on the node, so `value` and
    `blast_radius` are queryable fields.

    The LINTER half is still open, and asserted here rather than left as folklore. V7
    branches on `isinstance(raw, (list, tuple))` to decide the form and reads sub-fields
    via `_subfield()`, neither of which can see into a dict. A dict therefore falls
    through `_values()`'s `str(value)` fallback and V7 lints the dict's REPR: it reports
    `value_not_float` about a confidence whose `value` is a perfectly good float, and
    never reaches the two things that ARE wrong here (`9.9` is out of range,
    `catastrophic` is not in the enum). That is a visible, locatable false positive
    rather than the previous silence, and closing it is a dict branch in
    `factlattice_engine/lint/profile_rules.py` — out of scope for the parser change,
    asserted here so the follow-on cannot be forgotten."""
    text = FRONTMATTER + """\
# Dark confidence [SPEC:DOC-V7D]

## [DEC-V7-DARK] a
- rationale: r
- lifecycle-position: committed
- importance: high
- confidence:
    value: 9.9
    blast_radius: catastrophic
"""
    corpus_dir, profile_path = workspace
    _write(corpus_dir, "v7d.lts", text)
    corpus = factlattice.parse(corpus_dir, profile=profile_path)
    node = next(n for n in corpus.nodes if n.id == "DEC-V7-DARK")
    # PARSER half — closed by P5.
    assert node.properties["confidence"] == {"value": "9.9",
                                             "blast_radius": "catastrophic"}
    # LINTER half — still open: V7 lints the dict's repr, so it misses BOTH real
    # violations and raises one false `value_not_float` instead.
    result = lint(corpus_dir, profile=profile_path)
    # LINTER half — NOW CLOSED. `_subfield` reads a P5 mapping by KEY, and `_values`
    # no longer stringifies a dict into a scalar. V7 therefore finds the TWO REAL
    # violations this fixture carries and raises NO false `value_not_float`:
    #   value: 9.9          -> out of range (SPEC 7.1 says 0.0-1.0)
    #   blast_radius: catastrophic -> not in {low, medium, high}
    assert _count(result, "V7") == 2
    problems = sorted(f.detail["problem"] for f in _of(result, "V7"))
    assert problems == ["blast_radius_unknown", "value_out_of_range"]
    assert all(f.detail["form"] == "block" for f in _of(result, "V7")), \
        "a P5 mapping is a BLOCK, not an inline scalar"
    assert not any(f.detail["problem"] == "value_not_float" for f in _of(result, "V7")), \
        "the dict repr must never be linted as the value again"


# ---------------------------------------------------------------------------
# V8 — change => context_for_change carries invalidating_assumption (SPEC 8.1)
# ---------------------------------------------------------------------------
V8_FIXTURE = FRONTMATTER + """\
# Change fixture [SPEC:DOC-V8]

## [DEC-V8-COMPLETE] a
- rationale: r
- lifecycle-position: committed
- importance: high
- change: The limit moved from 12 to 15
- context_for_change:
  - prior_state: the limit was 12
  - invalidating_assumption: that the load distribution stays where v2 put it

## [DEC-V8-NO-ASSUMPTION] b
- rationale: r
- lifecycle-position: committed
- importance: high
- change: The extractor changed
- context_for_change:
  - prior_state: 4.1
  - reasoning: 4.2 ships

## [DEC-V8-NO-CONTEXT] c
- rationale: r
- lifecycle-position: committed
- importance: high
- change: Something changed and nothing says when to reopen it

## [DEC-V8-NO-CHANGE] d
- rationale: r
- lifecycle-position: committed
- importance: high
"""


def test_v8_flags_exactly_the_changes_with_no_invalidating_assumption(workspace):
    result = _lint(workspace, v8=V8_FIXTURE)
    assert _count(result, "V8") == 2
    assert sorted((f.node_id, f.detail["form"]) for f in _of(result, "V8")) == [
        ("DEC-V8-NO-ASSUMPTION", "block"),
        ("DEC-V8-NO-CONTEXT", "absent"),
    ]


def test_v8_nested_subfield_is_a_string_not_a_queryable_field(workspace):
    """The P4-nested form parses to an ARRAY OF STRINGS with the sub-field name INSIDE
    the string. V8 reads it out of the item text, and says so on the finding
    (`queryable: False`) rather than implying the graph can be queried for it."""
    corpus_dir, profile_path = workspace
    _write(corpus_dir, "v8.lts", V8_FIXTURE)
    corpus = factlattice.parse(corpus_dir, profile=profile_path)
    node = next(n for n in corpus.nodes if n.id == "DEC-V8-COMPLETE")
    value = node.properties["context_for_change"]
    assert isinstance(value, list)
    assert value == ["prior_state: the limit was 12",
                     "invalidating_assumption: that the load distribution stays "
                     "where v2 put it"]
    result = lint(corpus_dir, profile=profile_path)
    assert all(f.detail["queryable"] is False for f in _of(result, "V8"))


def test_v8_yaml_mapping_form_reaches_the_graph_but_not_yet_v8(workspace):
    """⛔ THE LIMITATION, ASSERTED — AND HALF OF IT NOW CLOSED.

    SPEC §8.1 documents `context_for_change` as a YAML MAPPING. This test used to
    assert the parser dropped that shape whole, making a node with a full seven-field
    block INDISTINGUISHABLE from a node with none (V8: `form=absent`, `dark=True`), and
    it said "closing this is parser conformance work". P5 did that work: the mapping is
    now a keyed dict and `invalidating_assumption` is a queryable field.

    What remains is LINTER work, and it is no longer dark — it is merely unfinished.
    V8 branches on `isinstance(raw, (list, tuple))` to pick `form`, and reads sub-fields
    via `_subfield()`, which cannot see into a dict. So V8 sees the property (no more
    `form=absent` guessing), classifies it as `inline`, fails to find the
    `invalidating_assumption` that IS right there, and reports a FALSE POSITIVE. That is
    a strictly better failure than the old one — it is visible, and it is one dict
    branch in `factlattice_engine/lint/profile_rules.py` (`_values` / `_subfield` /
    V8's `form` choice) away from correct. Out of scope for the parser change; asserted
    here so the follow-on cannot be forgotten."""
    text = FRONTMATTER + """\
# Dark change [SPEC:DOC-V8D]

## [DEC-V8-DARK] authored exactly as SPEC 8.1 documents it
- rationale: r
- lifecycle-position: committed
- importance: high
- change: "Diagrams are derived from the graph, never stored"
- context_for_change:
    prior_state: "Documents embedded diagrams as static snapshots"
    reasoning: "A stored diagram is a second copy of a fact the graph already holds"
    invalidating_assumption: "That the render path is available wherever the corpus is read"
"""
    corpus_dir, profile_path = workspace
    _write(corpus_dir, "v8d.lts", text)
    corpus = factlattice.parse(corpus_dir, profile=profile_path)
    node = next(n for n in corpus.nodes if n.id == "DEC-V8-DARK")
    assert "change" in node.properties
    # PARSER half — closed by P5. The sub-field is KEYED, not a substring of an item.
    value = node.properties["context_for_change"]
    assert isinstance(value, dict)
    assert set(value) == {"prior_state", "reasoning", "invalidating_assumption"}
    assert value["invalidating_assumption"] == (
        '"That the render path is available wherever the corpus is read"')
    # LINTER half — NOW CLOSED. `_subfield` reads the mapping by KEY, so V8 sees the
    # `invalidating_assumption` that is plainly there and raises NOTHING. This node is
    # authored EXACTLY as SPEC 8.1 documents the primitive; a finding here would be a
    # false positive against the spec's own worked example.
    result = lint(corpus_dir, profile=profile_path)
    assert _count(result, "V8") == 0, (
        "a node authored in SPEC 8.1's documented shape, carrying the load-bearing "
        "field, must not be reported as missing it")


# ---------------------------------------------------------------------------
# V9 — required frontmatter (SPEC 1.5); `audience` ratified required
# ---------------------------------------------------------------------------
def test_v9_flags_one_finding_per_missing_frontmatter_key(workspace):
    """One finding PER KEY: the triage unit is "this document needs an `audience`",
    and a single collapsed finding per file would hide two of the three."""
    missing_all = "# No frontmatter [SPEC:DOC-V9A]\n\n## [DEC-V9A] a\n- rationale: r\n- lifecycle-position: committed\n- importance: high\n"
    missing_one = ("---\nscope: s\nboundary: b\n---\n"
                   "# Partial [SPEC:DOC-V9B]\n\n## [DEC-V9B] a\n- rationale: r\n"
                   "- lifecycle-position: committed\n- importance: high\n")
    complete = FRONTMATTER + "# Complete [SPEC:DOC-V9C]\n\n## [DEC-V9C] a\n- rationale: r\n- lifecycle-position: committed\n- importance: high\n"
    result = _lint(workspace, v9a=missing_all, v9b=missing_one, v9c=complete)
    assert _count(result, "V9") == 4
    assert sorted((f.source_file, f.detail["key"]) for f in _of(result, "V9")) == [
        ("v9a.lts", "audience"), ("v9a.lts", "boundary"), ("v9a.lts", "scope"),
        ("v9b.lts", "audience"),
    ]
    assert REQUIRED_FRONTMATTER == ("scope", "audience", "boundary")


# ---------------------------------------------------------------------------
# V11 — `status` is a bare lifecycle token (the ENGINE agreement check)
# ---------------------------------------------------------------------------
# V11 is not a style rule. `lts_find(status=…)` was widened (`lifecycle.status_matches`)
# so the DISPATCH GATE could see the qualified `proposed — awaiting ratify` nodes it had
# been silently missing. Widening the filter ALONE would have moved the invisibility one
# layer down: the gate would start working and nobody would ever learn the status field
# had stopped being a queryable enum. V11 is the half that keeps the drift visible.
#
# Every status string below is copied VERBATIM from the live corpora. `DOC-` nodes are
# used deliberately: the fixture profile declares no required/recommended properties for
# that family, so this file adds V11 findings and NOTHING else to the surface receipt.
V11_FIXTURE = FRONTMATTER + """\
# Status shapes [SPEC:DOC-V11]

## [DOC-V11-BARE] a bare token
- status: proposed
- note: the queryable shape; V11 must stay silent

## [DOC-V11-HYPHENATED] a hyphenated lifecycle is still ONE bare token
- status: in-progress
- note: `in-progress` is a token, not a token plus prose

## [DOC-V11-COMPOUND] a long hyphenated lifecycle, still bare
- status: resolved-pending-live-confirmation
- note: normalizes to itself, never to `resolved`

## [DOC-V11-QUALIFIED] token plus qualifier prose
- status: proposed — awaiting ratify
- note: findable now, but the field is no longer an enum

## [DOC-V11-DATED] token plus a parenthetical date
- status: operational (2026-07-07)
- note: the single most common drift shape in the corpus

## [DOC-V11-SEMICOLON] token, semicolon, amendment prose
- status: resolved; amended 2026-07-26 (freshest-source de-fork machine-green)
- note: `;` is a boundary, so the token is still derivable

## [DOC-V11-BOLD] leading bold marker, no derivable token
- status: **operational (2026-07-07)**
- note: reachable ONLY by exact string

## [DOC-V11-SHOUTED] leading capital, no derivable token
- status: UNCONFIRMED. Its author has explicitly downgraded the evidence
- note: reachable ONLY by exact string

## [DOC-V11-APOSTROPHE] a non-boundary character after the word
- status: spec'd; build queued
- note: `spec` would be a WRONG lifecycle, so the tokenizer refuses

## [DOC-V11-NONE] no status at all
- note: absence is V3/V4's question, not V11's
"""


def test_v11_flags_exactly_the_drifted_statuses(workspace):
    """POSITIVE CONTROL, EXACT COUNT: 6 of the 10 nodes — 3 qualified + 3 untokenized.
    The 3 bare tokens and the status-less node are the negative control, in the same
    fixture, so a rule that fired on everything would fail here too."""
    result = _lint(workspace, v11=V11_FIXTURE)
    assert _count(result, "V11") == 6
    by_id = {f.node_id: f for f in _of(result, "V11")}
    assert sorted(by_id) == [
        "DOC-V11-APOSTROPHE", "DOC-V11-BOLD", "DOC-V11-DATED",
        "DOC-V11-QUALIFIED", "DOC-V11-SEMICOLON", "DOC-V11-SHOUTED"]
    assert {i: f.detail["shape"] for i, f in by_id.items()} == {
        "DOC-V11-QUALIFIED": "qualified",
        "DOC-V11-DATED": "qualified",
        "DOC-V11-SEMICOLON": "qualified",
        "DOC-V11-BOLD": "untokenized",
        "DOC-V11-SHOUTED": "untokenized",
        "DOC-V11-APOSTROPHE": "untokenized",
    }


def test_v11_is_silent_on_every_bare_token(workspace):
    """NEGATIVE CONTROL, named. A validator that fires on the legitimate shape trains
    authors to switch the gate off ([LAYER-LINT])."""
    result = _lint(workspace, v11=V11_FIXTURE)
    flagged = {f.node_id for f in _of(result, "V11")}
    for clean in ("DOC-V11-BARE", "DOC-V11-HYPHENATED", "DOC-V11-COMPOUND",
                  "DOC-V11-NONE"):
        assert clean not in flagged


def test_v11_names_the_token_the_node_is_findable_by(workspace):
    """The finding has to be ACTIONABLE: for a qualified value it names the lifecycle
    token that now finds the node; for an untokenized one it says so, so the reader
    knows exact-string is the only handle."""
    result = _lint(workspace, v11=V11_FIXTURE)
    by_id = {f.node_id: f for f in _of(result, "V11")}
    assert by_id["DOC-V11-DATED"].detail["lifecycle_token"] == "operational"
    assert by_id["DOC-V11-DATED"].detail["status"] == "operational (2026-07-07)"
    assert "lts_find(status='operational')" in by_id["DOC-V11-DATED"].message
    assert by_id["DOC-V11-BOLD"].detail["lifecycle_token"] is None
    assert "ONLY by an exact-string query" in by_id["DOC-V11-BOLD"].message


def test_v11_agrees_with_the_filter_it_describes(workspace):
    """THE AGREEMENT CHECK ITSELF. A detector that disagreed with the system it
    describes would be worse than no detector: for every node V11 flags as `qualified`,
    the widened filter must actually find it by the token V11 named — and for every node
    V11 leaves alone, an exact-string query must still work."""
    result = _lint(workspace, v11=V11_FIXTURE)
    for f in _of(result, "V11"):
        token = f.detail["lifecycle_token"]
        if f.detail["shape"] == "qualified":
            assert status_matches(f.detail["status"], token)
        else:
            assert token is None
            assert lifecycle_token(f.detail["status"]) is None
            assert status_matches(f.detail["status"], f.detail["status"])
    flagged = {f.node_id for f in _of(result, "V11")}
    for node in result.corpus.nodes:
        if node.status and node.id not in flagged:
            assert is_bare_lifecycle_token(node.status)


# ---------------------------------------------------------------------------
# SEVERITY + the whole-surface receipt
# ---------------------------------------------------------------------------
ALL_FIXTURES = {
    "v1": V1_FIXTURE, "v3": V3_FIXTURE, "v6": V6_FIXTURE, "v7": V7_FIXTURE,
    "v8": V8_FIXTURE, "v11": V11_FIXTURE,
}


def test_every_profile_finding_is_a_warning(workspace):
    """NON-NEGOTIABLE at introduction. [LAYER-LINT]: a gate that fires on legitimate
    content "would train authors to disable the gate"."""
    result = _lint(workspace, **ALL_FIXTURES)
    profile_findings = result.by_origin("profile")
    assert profile_findings
    assert {f.severity for f in profile_findings} == {"warning"}
    assert not result.has_errors and result.exit_code() == 0
    assert set(PROFILE_SEVERITY.values()) == {"warning"}


def test_severity_can_be_ratcheted_per_validator(workspace):
    """Promotion to `error` is a later PER-VALIDATOR decision with a recorded reason.
    The mechanism has to exist now, or the ratchet is a promise instead of a knob."""
    result = _lint(workspace, v1=V1_FIXTURE)
    assert result.exit_code() == 0
    corpus_dir, profile_path = workspace
    ratcheted = lint(corpus_dir, profile=profile_path,
                     profile_severity={"unregistered_prefix": "error"})
    assert ratcheted.has_errors and ratcheted.exit_code() == 1


def test_the_whole_validator_surface_reports_its_exact_counts(workspace):
    """The receipt: one corpus, every validator, every count pinned. A regression in
    ANY validator moves exactly one number here."""
    result = _lint(workspace, **ALL_FIXTURES)
    assert result.by_validator() == {
        "V1": 3,     # WIDGET-ONE, WIDGET-TWO (v1) + WIDGET-NOT-A-DECISION (v3)
        "V3": 4,     # DEC-MISSING-BOTH x2, DEC-MISSING-ONE x1, DEC-V6-UNSTAGED x1
        "V6": 1,     # DEC-V6-BOGUS
        "V7": 3,     # out-of-range, not-a-float, blast_radius
        "V8": 2,     # no-assumption (block), no-context (absent)
        "V11": 6,    # 3 qualified + 3 untokenized statuses (4 clean nodes in the same file)
    }
    # V2 / V4 / V5 / V9 are ABSENT from that dict, and each absence is a NEGATIVE
    # control with a reason: every fixture file carries a core doctype, every decision
    # carries `importance`, no fixture authors an undeclared enum value, and every
    # fixture carries the three required frontmatter keys. Their positive controls are
    # the dedicated tests above; a rule that reports zero everywhere would be caught
    # there, not hidden here.
    assert result.by_validator().keys() == {"V1", "V3", "V6", "V7", "V8", "V11"}
    assert [v for (v, _k, _s) in VALIDATORS] == [
        "V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V9", "V11"]


def test_findings_carry_their_validator_number_for_triage(workspace):
    result = _lint(workspace, **ALL_FIXTURES)
    known = {v for (v, _k, _s) in VALIDATORS}
    for f in result.by_origin("profile"):
        assert f.detail["validator"] in known
    assert profile_findings_by_validator(
        find_profile_violations(result.corpus))["V1"] == 3


def test_profile_rules_do_not_change_the_graph_or_the_other_layers(workspace):
    """This is a LINT, not a parser change: turning it on must not move a node, an
    edge, or any other layer's finding."""
    corpus_dir, profile_path = workspace
    for name, text in ALL_FIXTURES.items():
        _write(corpus_dir, name + ".lts", text)
    on = lint(corpus_dir, profile=profile_path)
    off = lint(corpus_dir, profile=profile_path, profile_rules=False)
    assert on.node_count == off.node_count and on.edge_count == off.edge_count
    assert ([f.to_dict() for f in on.findings if f.origin != "profile"]
            == [f.to_dict() for f in off.findings])
