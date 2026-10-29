"""Typed `Profile` accessors + the `.lts` profile backend — one interface, two
serializations ([ACME-NARROW-WAIST] applied to the profile; `[PROFILE-SELFHOST]`).

WHAT THIS FILE IS DEFENDING
---------------------------
Before this change, `Profile` exposed `registered_ns`, `domain_verbs`, `domain_id`,
`raw`, `path`. Its own docstring named a consumer that was never written ("richer
consumers (a linter) read prefixes / relationship_verbs / enums / decision-node
schema off `raw` directly"), and `[PROFILE-CONTRACT]` admitted the consequence:
profile-driven validation is "specified here but not yet enforced". A deployed
profile declares 10 top-level keys; exactly 2 were consumed.

Two rules govern every assertion below, both learned the hard way on this project:

  1. POSITIVE CONTROL PER ACCESSOR, ASSERTING AN EXACT COUNT. A rule that reports
     zero looks exactly like a rule that works. `assert len(...) == 7` fails when
     the projection silently drops a record; `assert prefixes` does not.
  2. EQUIVALENCE BY FIXTURE, NOT BY ASSURANCE. `test_lts_and_yaml_accessor_surfaces_are_equal`
     compares the WHOLE accessor surface of two files that encode the same domain in
     two serializations. If the `.lts` backend ever drifts from the YAML backend, the
     surface stops being equal and this fails — no reviewer judgement required.

  3. THE FIXTURE IS PURPOSE-MADE, NOT COPIED. `acme.ds` is a synthetic domain
     constructed to exercise the accessors — it is not, and must never become, a
     reproduction of any deployed corpus's profile. An earlier revision of this file
     was "a faithful copy of the live profile" of a real deployment, which coupled a
     PUBLIC test to a customer artifact three ways at once: it disclosed that
     customer's vocabulary, it could only be updated by reading their private data,
     and it failed whenever THEIR corpus changed for reasons that had nothing to do
     with this engine. A fixture's job is to exercise an interface; borrowing
     someone's data to do it buys realism the assertions never actually use.

     What the fixture DOES have to reproduce is SHAPE, and it reproduces every shape
     the accessors branch on: two serializations of one domain; a multi-segment
     dotted namespace (`acme.ds`); two prefixes pointing at ONE document; a prefix
     (`MUT-`) claimed by TWO node-family blocks, which is what makes the union rule
     in `required_properties` observable; a flat enum and a core/extended tiered one;
     a lifecycle split into pipeline and off-pipeline. Shape is what the engine
     reads. Identity is what it must not need.

     ⛔ The engine is domain-agnostic BY DESIGN — it loads vocabulary from a profile
     at runtime, and that is its narrow waist. A real domain id appearing in engine
     source or in this suite is a fork across that waist, not a cosmetic blemish.
     Live-corpus tests, which necessarily DO name a deployment, live in the
     gitignored `private/tests/` overlay and are collected from there when present.
"""
from __future__ import annotations

import pytest

import factlattice
from factlattice.profile import (
    DEFAULT_PROFILE,
    LifecycleStages,
    PrefixSpec,
    Profile,
    discover_profile,
    load_profile,
)

# --- the receipt: what the SYNTHETIC profile projects onto ----------------------
# These two are the ONLY fields the parser core consumes; they must be byte-identical
# across both serializations, or the accessor work changed BEHAVIOUR.
ACME_REGISTERED_NS = frozenset({"core", "acme.ds"})
ACME_DOMAIN_VERBS = frozenset({
    "DEPENDS_ON", "RELATED_TO", "SUPERSEDES", "PART_OF", "RESOLVES",
    "GOVERNS", "VALIDATES", "PREVENTS_RECURRENCE_OF", "INVALIDATES",
    "MUTATES", "BOUNDS", "CONTRADICTS", "MEASURED_ON",
})

# --- serialization A: the legacy `99-{domain}-profile.yaml` ---------------------
ACME_YAML = """\
domain:
  id: "acme.ds"
  name: "Acme Analytics — Data Science"
  version: "1.0"
  conventions_source: "catalogue.lts [REGISTRY:CATALOGUE] + ds-state-fingerprint.lts"
  namespaces:
    - "acme.ds"

prefixes:
  - { prefix: "DEC-",   document: "decision-graph-analyst",  note: "Load-bearing DS decisions, session-keyed to when DECIDED" }
  - { prefix: "DELIV-", document: "deliverable-ledger-analyst", note: "Artifact ledger; `pull-when` is the retrieval trigger" }
  - { prefix: "CAT-",   document: "catalogue",                      note: "Catalogue registry records, one per corpus under the dataset catalogue" }
  - { prefix: "MUT-",   document: "catalogue",                      note: "Corpus MUTATION records — the only change history for corpora that have no version control" }
  - { prefix: "FP-",    document: "ds-state-fingerprint",           note: "Dataset state-fingerprint spec nodes" }
  - { prefix: "LB-",    document: "corpus roots",                   note: "Document-level roots (LB-DECISIONS, LB-DELIV)" }
  - { prefix: "INV-",   document: "cross-cutting",                  note: "Invariants (INV-LB-*, INV-EXCL-*, INV-SUBSTRATE-*)" }

document_tags:
  - { tag: "SPEC",     note: "Decision graph and specification documents" }
  - { tag: "LEDGER",   note: "Deliverable ledger" }
  - { tag: "REGISTRY", note: "Catalogue registry (and the MUT records it roots)" }

relationship_verbs:
  core:
    - DEPENDS_ON
    - RELATED_TO
    - SUPERSEDES
    - PART_OF
    - RESOLVES
  domain:
    - GOVERNS
    - VALIDATES
    - PREVENTS_RECURRENCE_OF
    - INVALIDATES
    - MUTATES
    - BOUNDS
    - CONTRADICTS
    - MEASURED_ON

confidentiality:
  - public
  - nda-shareable
  - internal-only

artifact_readiness:
  core:
    - educational
    - system-design
    - code-generation
  extended:
    - design-review
    - operational

decision_nodes:
  families:
    - "DEC-"
    - "MUT-"
  required:
    - rationale
    - lifecycle-position
  recommended:
    - importance
    - empirical-support
    - stage-evidence

decision_lifecycle:
  pipeline:
    - proposed
    - committed
    - validated
  off_pipeline:
    - superseded
    - contingent
    - deferred
    - withdrawn
  decision_prefixes: [DEC, MUT]
  session_key_property: decided-in-session

mutation_nodes:
  families:
    - "MUT-"
  required:
    - depends-on
    - written-before-execution
    - invalidates
    - executed
  recommended:
    - exclusion-class
    - basis-of-change
    - continuity-effect
    - registry-updated
    - verification

linter_guidance:
  notes:
    - "Edge-bearing property names come from parser.py REL_PROPERTY_EDGES PLUS any this profile registers on an edge type (the maintainer 2026-08-15). A profile MAY add its own; it may NOT redefine a built-in — that collision is refused at load."
    - "⛔ A `- key: value` property is ONE PHYSICAL LINE. Continuation lines are silently discarded (SPEC §2.2 defines no continuation form for inline values). Use `- key:` + an indented `- item` block if you need multiple values."
    - "⛔ A property key must match ^[a-z][a-z0-9_-]*$ — a leading emoji (`- ⛔ invariant:`) or an unclosed bold marker (`- **pull-when:`) makes the whole line parse as PROSE and the field disappears from `properties`."
"""

# --- serialization B: the `[PROFILE-SELFHOST]` `{domain}-profile.lts` ------------
# THE SAME CONTENT, core notation ONLY (no `## Relationships` triples, no namespaced
# edge types) — the bootstrapping invariant made concrete: a profile cannot use the
# notations it registers, or the profile would be needed in order to parse the profile.
ACME_LTS = """\
---
title: "Acme Analytics Data Science — domain profile"
id: "PROFILE:ACME-DS-PROFILE"
domain: acme.ds
scope: "The acme.ds domain's registered vocabulary, encoded as a .lts document"
boundary: "Registers vocabulary. Registers no behaviour."
status: draft
---
# Acme Analytics Data Science — domain profile [PROFILE:ACME-DS-PROFILE]

## [PROFILE-DOMAIN] Domain identity

- domain-id: acme.ds
- domain-name: Acme Analytics — Data Science
- domain-version: 1.0
- conventions-source: catalogue.lts [REGISTRY:CATALOGUE] + ds-state-fingerprint.lts
- namespaces: acme.ds

## Registered node-id prefixes

### [PREFIX-DEC] Decision nodes

- registers-prefix: DEC-
- document: decision-graph-analyst
- definition: Load-bearing DS decisions, session-keyed to when DECIDED

### [PREFIX-DELIV] Deliverable ledger nodes

- registers-prefix: DELIV-
- document: deliverable-ledger-analyst
- definition: Artifact ledger; `pull-when` is the retrieval trigger

### [PREFIX-CAT] Catalogue registry records

- registers-prefix: CAT-
- document: catalogue
- definition: Catalogue registry records, one per corpus under the dataset catalogue

### [PREFIX-MUT] Corpus mutation records

- registers-prefix: MUT-
- document: catalogue
- definition: Corpus MUTATION records — the only change history for corpora that have no version control

### [PREFIX-FP] Dataset state fingerprints

- registers-prefix: FP-
- document: ds-state-fingerprint
- definition: Dataset state-fingerprint spec nodes

### [PREFIX-DS] Document roots

- registers-prefix: LB-
- document: corpus roots
- definition: Document-level roots (LB-DECISIONS, LB-DELIV)

### [PREFIX-INV] Invariants

- registers-prefix: INV-
- document: cross-cutting
- definition: Invariants (INV-LB-*, INV-EXCL-*, INV-SUBSTRATE-*)

## Registered document tags

### [DOCTAG-SPEC] Specification documents

- document-tag: SPEC
- definition: Decision graph and specification documents

### [DOCTAG-LEDGER] Deliverable ledger

- document-tag: LEDGER
- definition: Deliverable ledger

### [DOCTAG-REGISTRY] Catalogue registry

- document-tag: REGISTRY
- definition: Catalogue registry (and the MUT records it roots)

## Registered relationship verbs

### [EDGETYPE-DEPENDS-ON] Core dependency

- edge-type: DEPENDS_ON
- tier: core

### [EDGETYPE-RELATED-TO] Core association

- edge-type: RELATED_TO
- tier: core

### [EDGETYPE-SUPERSEDES] Core supersession

- edge-type: SUPERSEDES
- tier: core

### [EDGETYPE-PART-OF] Core containment

- edge-type: PART_OF
- tier: core

### [EDGETYPE-RESOLVES] Core resolution

- edge-type: RESOLVES
- tier: core

### [EDGETYPE-GOVERNS] A constrains or sets the frame for B

- edge-type: GOVERNS
- tier: domain
- definition: A constrains or sets the frame for B (decision -> decision)

### [EDGETYPE-VALIDATES] Empirical confirmation

- edge-type: VALIDATES
- tier: domain
- definition: A is empirical confirmation of B

### [EDGETYPE-PREVENTS-RECURRENCE-OF] Structural guard

- edge-type: PREVENTS_RECURRENCE_OF
- tier: domain
- definition: A is a structural guard against B happening again

### [EDGETYPE-INVALIDATES] Result invalidation

- edge-type: INVALIDATES
- tier: domain
- definition: A makes B's result no longer valid (mutation -> claim)

### [EDGETYPE-MUTATES] Corpus mutation

- edge-type: MUTATES
- tier: domain
- definition: A changes the state of corpus B (MUT -> CAT)

### [EDGETYPE-BOUNDS] Scope discipline

- edge-type: BOUNDS
- tier: domain
- definition: A limits the scope B may be read at (scope discipline)

### [EDGETYPE-CONTRADICTS] Open collision

- edge-type: CONTRADICTS
- tier: domain
- definition: A and B cannot both be true — an open collision, not a supersession

### [EDGETYPE-MEASURED-ON] Measurement basis

- edge-type: MEASURED_ON
- tier: domain
- definition: A was computed against corpus/basis B (pairs with `validated-against`)

## Enums

### [ENUM-CONFIDENTIALITY] Confidentiality classes

- enum-key: confidentiality
- values:
  - public
  - nda-shareable
  - internal-only

### [ENUM-ARTIFACT-READINESS] Artifact readiness classes

- enum-key: artifact_readiness
- values:
  - educational
  - system-design
  - code-generation
- extended-values:
  - design-review
  - operational

## Node-family schemas

### [NODEFAMILY-DECISION] Decision-node schema

- section: decision_nodes
- families: DEC-, MUT-
- required:
  - rationale
  - lifecycle-position
- recommended:
  - importance
  - empirical-support
  - stage-evidence

### [NODEFAMILY-MUTATION] Mutation-node schema

- section: mutation_nodes
- families: MUT-
- required:
  - depends-on
  - written-before-execution
  - invalidates
  - executed
- recommended:
  - exclusion-class
  - basis-of-change
  - continuity-effect
  - registry-updated
  - verification

## [LIFECYCLE-DECISION] Decision-lifecycle stages

- section: decision_lifecycle
- pipeline:
  - proposed
  - committed
  - validated
- off-pipeline:
  - superseded
  - contingent
  - deferred
  - withdrawn
- decision-prefixes: DEC, MUT
- session-key-property: decided-in-session

## Linter guidance

### [GUIDANCE-PROPERTY-EDGES] Edge-bearing property names are core PLUS profile-registered

- note: Edge-bearing property names come from parser.py REL_PROPERTY_EDGES PLUS any this profile registers on an edge type (the maintainer 2026-08-15). A profile MAY add its own; it may NOT redefine a built-in — that collision is refused at load.

### [GUIDANCE-ONE-PHYSICAL-LINE] A property is one physical line

- note: ⛔ A `- key: value` property is ONE PHYSICAL LINE. Continuation lines are silently discarded (SPEC §2.2 defines no continuation form for inline values). Use `- key:` + an indented `- item` block if you need multiple values.

### [GUIDANCE-KEY-REGEX] A property key must match the key regex

- note: ⛔ A property key must match ^[a-z][a-z0-9_-]*$ — a leading emoji (`- ⛔ invariant:`) or an unclosed bold marker (`- **pull-when:`) makes the whole line parse as PROSE and the field disappears from `properties`.
"""


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def yaml_profile(tmp_path) -> Profile:
    path = tmp_path / "99-acme-ds-profile.yaml"
    path.write_text(ACME_YAML, encoding="utf-8")
    return load_profile(str(path))


@pytest.fixture
def lts_profile(tmp_path) -> Profile:
    path = tmp_path / "acme-ds-profile.lts"
    path.write_text(ACME_LTS, encoding="utf-8")
    return load_profile(str(path))


def _surface(p: Profile) -> dict:
    """The WHOLE typed surface of a Profile, as comparable values.

    Deliberately excludes `raw` and `path`: `raw` is the serialization-specific
    dict the accessors exist to hide, and demanding the two backends produce an
    identical `raw` would re-couple the test to the shape we are decoupling from.
    What must be equal is what a validator can SEE.
    """
    return {
        "registered_ns": p.registered_ns,
        "domain_verbs": p.domain_verbs,
        "domain_id": p.domain_id,
        "namespaces": p.namespaces,
        "prefixes": p.prefixes,
        "prefix_set": p.prefix_set,
        "document_tags": p.document_tags,
        "confidentiality": p.enum_values("confidentiality"),
        "artifact_readiness": p.enum_values("artifact_readiness"),
        "undeclared_enum": p.enum_values("nope"),
        "required_decision_nodes": p.required_properties("decision_nodes"),
        "required_DEC": p.required_properties("DEC-"),
        "recommended_decision_nodes": p.recommended_properties("decision_nodes"),
        "required_mutation_nodes": p.required_properties("mutation_nodes"),
        "recommended_mutation_nodes": p.recommended_properties("mutation_nodes"),
        "lifecycle_stages": p.lifecycle_stages,
        "guidance_notes": p.guidance_notes,
    }


# ---------------------------------------------------------------------------
# Task A — the two fields that existed before must not move ("pure interface")
# ---------------------------------------------------------------------------
def test_yaml_core_hooks_are_byte_identical_to_the_pre_change_receipt(yaml_profile):
    # The ONLY two projections the parser core consumes. Any drift here means the
    # accessor work changed BEHAVIOUR, which it is explicitly not allowed to do.
    assert yaml_profile.registered_ns == ACME_REGISTERED_NS
    assert yaml_profile.domain_verbs == ACME_DOMAIN_VERBS
    assert yaml_profile.domain_id == "acme.ds"
    assert len(yaml_profile.registered_ns) == 2
    assert len(yaml_profile.domain_verbs) == 13


def test_a_dotted_multi_segment_domain_id_survives_the_projection(yaml_profile):
    """The SHAPE that matters, pinned on its own rather than left implicit.

    `acme.ds` is two segments with a LOWERCASE trailing one. That shape is what
    broke the edge tokenizer in [SL-18] (`acme.ds.GOVERNS` split into two tokens and
    the namespace was silently stripped), and it is the shape deployed domains
    actually use. A fixture with a single-segment id would have exercised none of it.
    """
    assert "." in yaml_profile.domain_id
    assert yaml_profile.domain_id.split(".")[-1].islower()
    assert yaml_profile.namespaces == ["acme.ds"]
    # `core` is added by the loader; the domain contributes exactly its own.
    assert yaml_profile.registered_ns - {"core"} == frozenset({"acme.ds"})


# ---------------------------------------------------------------------------
# Task A — a positive control PER ACCESSOR, asserting an EXACT count
# ---------------------------------------------------------------------------
def test_prefixes_exact_count_and_records(yaml_profile):
    prefixes = yaml_profile.prefixes
    assert len(prefixes) == 7
    assert prefixes[0] == PrefixSpec(
        prefix="DEC-",
        document="decision-graph-analyst",
        note="Load-bearing DS decisions, session-keyed to when DECIDED",
    )
    # Order is DECLARATION order, not sorted: a profile's prefix list is authored
    # most-load-bearing-first and a report that reorders it is harder to read.
    assert [p.prefix for p in prefixes] == [
        "DEC-", "DELIV-", "CAT-", "MUT-", "FP-", "LB-", "INV-"]
    assert yaml_profile.prefix_set == frozenset(
        {"DEC-", "DELIV-", "CAT-", "MUT-", "FP-", "LB-", "INV-"})
    # TWO prefixes, ONE document — a real profile shape, and the reason `prefixes`
    # is a LIST of records rather than a dict keyed by document.
    assert [p.prefix for p in prefixes if p.document == "catalogue"] == ["CAT-", "MUT-"]


def test_document_tags_exact_count(yaml_profile):
    assert yaml_profile.document_tags == frozenset({"SPEC", "LEDGER", "REGISTRY"})
    assert len(yaml_profile.document_tags) == 3


def test_enum_values_flat_and_tiered_shapes(yaml_profile):
    # flat list shape
    assert yaml_profile.enum_values("confidentiality") == frozenset(
        {"public", "nda-shareable", "internal-only"})
    assert len(yaml_profile.enum_values("confidentiality")) == 3
    # tiered mapping shape — core|extended are an authoring convenience, so the
    # accessor unions them; a validator asking "is this value declared?" must not
    # have to know the domain split its own enum in two.
    assert yaml_profile.enum_values("artifact_readiness") == frozenset(
        {"educational", "system-design", "code-generation",
         "design-review", "operational"})
    assert len(yaml_profile.enum_values("artifact_readiness")) == 5


def test_enum_values_distinguishes_undeclared_from_empty(yaml_profile):
    # None == "this domain declares no such enum" -> the validator SKIPS the check.
    assert yaml_profile.enum_values("severity") is None
    # A structural key is never reinterpreted as an enum, or `enum_values("prefixes")`
    # would return a set of nothing useful and a validator would reject every value.
    assert yaml_profile.enum_values("prefixes") is None
    assert yaml_profile.enum_values("domain") is None
    assert yaml_profile.enum_values("") is None
    # An empty declared enum is a DIFFERENT answer: nothing is valid.
    empty = load_profile({"severity": []})
    assert empty.enum_values("severity") == frozenset()


def test_required_and_recommended_properties_by_section_and_by_family(yaml_profile):
    assert yaml_profile.required_properties("decision_nodes") == frozenset(
        {"rationale", "lifecycle-position"})
    assert len(yaml_profile.required_properties("decision_nodes")) == 2
    assert yaml_profile.recommended_properties("decision_nodes") == frozenset(
        {"importance", "empirical-support", "stage-evidence"})
    assert len(yaml_profile.recommended_properties("decision_nodes")) == 3

    assert yaml_profile.required_properties("mutation_nodes") == frozenset(
        {"depends-on", "written-before-execution", "invalidates", "executed"})
    assert len(yaml_profile.required_properties("mutation_nodes")) == 4
    assert len(yaml_profile.recommended_properties("mutation_nodes")) == 5

    # A caller holding a parsed node has its `type_prefix` ("DEC"), not the profile's
    # punctuation ("DEC-"). Both resolve, so no consumer re-derives the hyphen rule.
    assert yaml_profile.required_properties("DEC-") == frozenset(
        {"rationale", "lifecycle-position"})
    assert yaml_profile.required_properties("DEC") == frozenset(
        {"rationale", "lifecycle-position"})
    # MUT- is a member of BOTH families lists; section lookup stays unambiguous
    # because an exact section-name match wins over a family scan.
    # `MUT-` is claimed by BOTH decision_nodes.families AND mutation_nodes.families,
    # so its requirements UNION. Resolving to the first block in dict order silently
    # dropped mutation_nodes' four -- including `written-before-execution` and
    # `executed`, which ARE the write-before-mutate gate. A node must satisfy every
    # schema that claims it; union can only check more, first-match loses silently.
    assert yaml_profile.required_properties("MUT-") == frozenset(
        {"rationale", "lifecycle-position",
         "depends-on", "written-before-execution", "invalidates", "executed"})
    assert len(yaml_profile.required_properties("MUT-")) == 6
    assert len(yaml_profile.recommended_properties("MUT-")) == 8
    # a family claimed by exactly one block is unaffected by the union
    assert yaml_profile.required_properties("DEC-") == frozenset(
        {"rationale", "lifecycle-position"})


def test_lifecycle_stages_exact_counts_and_order(yaml_profile):
    stages = yaml_profile.lifecycle_stages
    assert isinstance(stages, LifecycleStages)
    assert stages.pipeline == ["proposed", "committed", "validated"]
    assert stages.off_pipeline == ["superseded", "contingent", "deferred", "withdrawn"]
    assert len(stages.pipeline) == 3
    assert len(stages.off_pipeline) == 4
    assert len(stages.all_stages) == 7
    # Tuple-unpackable, per the brief's `(pipeline, off_pipeline)` signature.
    pipeline, off_pipeline = stages
    assert pipeline == stages.pipeline and off_pipeline == stages.off_pipeline


def test_guidance_notes_exact_count_and_content(yaml_profile):
    notes = yaml_profile.guidance_notes
    assert len(notes) == 3
    # The punchline of the inventory: these are hand-authored silent-drop warnings
    # written INTO a field nothing read. This assertion is that field's first reader.
    # ⛔ AMENDED 2026-08-15: this asserted the RETIRED ruling ("...are fixed by
    # parser.py REL_PROPERTY_EDGES and are NOT profile-configurable"). The maintainer
    # overturned it — profiles register their own edge keys — so the note now
    # states the NEW contract. Left as an assertion rather than deleted: a test
    # that encodes a retired ruling actively defends the wrong behaviour, but an
    # unasserted guidance note drifts unnoticed.
    assert notes[0].startswith("Edge-bearing property names come from parser.py")
    assert "may NOT redefine a built-in" in notes[0]
    assert "ONE PHYSICAL LINE" in notes[1]
    assert "^[a-z][a-z0-9_-]*$" in notes[2]


def test_every_accessor_is_total_over_an_empty_profile():
    """Missing sections yield EMPTY collections and never raise — otherwise every
    validator grows a try/except and the first domain without a `decision_lifecycle`
    block takes the linter down."""
    for profile in (DEFAULT_PROFILE, load_profile({}), load_profile({"domain": {"id": "x"}})):
        assert profile.prefixes == []
        assert profile.prefix_set == frozenset()
        assert profile.document_tags == frozenset()
        assert profile.enum_values("confidentiality") is None
        assert profile.required_properties("decision_nodes") == frozenset()
        assert profile.recommended_properties("DEC-") == frozenset()
        assert profile.lifecycle_stages == LifecycleStages([], [])
        assert profile.guidance_notes == []


def test_malformed_records_are_skipped_not_materialised():
    """A prefix record with no `prefix` key must NOT become the EMPTY prefix — that
    would register a prefix matching every node id and turn V1 into a no-op."""
    profile = load_profile({
        "prefixes": [{"document": "d", "note": "n"}, {"prefix": "OK-"}, "BARE-"],
        "document_tags": [{"note": "no tag here"}, "PLAIN", {"tag": "REC"}],
    })
    assert profile.prefix_set == frozenset({"OK-", "BARE-"})
    assert len(profile.prefixes) == 2
    assert profile.document_tags == frozenset({"PLAIN", "REC"})
    assert len(profile.document_tags) == 2


# ---------------------------------------------------------------------------
# Task B — the `.lts` backend, behind the SAME accessors
# ---------------------------------------------------------------------------
def test_lts_profile_loads_at_all(lts_profile):
    """Before this change `_read_profile_file` raised `ValueError: unsupported
    profile extension` for every `.lts`, so `[PROFILE-SELFHOST]` — "a domain profile
    is a `.lts` document parsed by the parser it configures" — was unimplementable."""
    assert lts_profile.domain_id == "acme.ds"
    assert lts_profile.registered_ns == ACME_REGISTERED_NS
    assert lts_profile.domain_verbs == ACME_DOMAIN_VERBS
    assert lts_profile.path and lts_profile.path.endswith("acme-ds-profile.lts")


def test_lts_accessors_exact_counts(lts_profile):
    # The same positive controls as the YAML backend, asserted independently so a
    # failure names WHICH accessor the projection dropped, not just "surfaces differ".
    assert len(lts_profile.prefixes) == 7
    assert len(lts_profile.document_tags) == 3
    assert len(lts_profile.enum_values("confidentiality")) == 3
    assert len(lts_profile.enum_values("artifact_readiness")) == 5
    assert len(lts_profile.required_properties("decision_nodes")) == 2
    assert len(lts_profile.recommended_properties("decision_nodes")) == 3
    assert len(lts_profile.required_properties("mutation_nodes")) == 4
    assert len(lts_profile.recommended_properties("mutation_nodes")) == 5
    assert len(lts_profile.lifecycle_stages.pipeline) == 3
    assert len(lts_profile.lifecycle_stages.off_pipeline) == 4
    assert len(lts_profile.guidance_notes) == 3
    assert len(lts_profile.domain_verbs) == 13


def test_lts_and_yaml_accessor_surfaces_are_equal(yaml_profile, lts_profile):
    """ROUND-TRIP EQUIVALENCE — the whole point of ordering Task A before Task B.

    Two files, two serializations, one domain. Every accessor must agree, so a
    validator written against the interface cannot tell which one it was handed.
    Compared field-by-field first so a failure names the accessor that drifted.
    """
    y, l = _surface(yaml_profile), _surface(lts_profile)
    for key in y:
        assert y[key] == l[key], f"accessor {key!r} differs between YAML and .lts"
    assert y == l


def test_lts_inline_list_is_comma_split_and_nested_list_is_verbatim(tmp_path):
    """The ONE list rule of the encoding, pinned: a P1 inline value comma-splits
    (short registry lines), a P4 nested block does not (so a stage name or a note
    may legally contain a comma)."""
    path = tmp_path / "x-profile.lts"
    path.write_text("""\
# X [PROFILE:X]

## [PROFILE-DOMAIN] d

- domain-id: x.dom
- namespaces: x.dom, x.other

## [LIFECYCLE-X] stages

- pipeline:
  - drafted, unreviewed
  - shipped
""", encoding="utf-8")
    profile = load_profile(str(path))
    assert profile.namespaces == ["x.dom", "x.other"]
    assert profile.registered_ns == frozenset({"core", "x.dom", "x.other"})
    assert profile.lifecycle_stages.pipeline == ["drafted, unreviewed", "shipped"]
    assert len(profile.lifecycle_stages.pipeline) == 2


def test_lts_namespaced_edge_type_becomes_an_edge_type_declaration(tmp_path):
    """A NAMESPACED `edge-type` is an edge_types[] declaration (it carries a layer);
    a BARE one is a relationship VERB (the parser's `domain_verbs` hook, which is
    the only thing that makes `[A] GOVERNS [B]` a recognised triple). The two are
    different mechanisms and the encoding must not conflate them."""
    path = tmp_path / "x-profile.lts"
    path.write_text("""\
# X [PROFILE:X]

## [PROFILE-DOMAIN] d

- domain-id: clinical.medical
- namespaces: clinical

## [EDGETYPE-CONTRAINDICATES] Contraindication

- edge-type: clinical.CONTRAINDICATES
- layer: semantic
- definition: Medication or diagnosis contradiction

## [EDGETYPE-ESCALATES] Escalation

- edge-type: ESCALATES
- tier: domain
""", encoding="utf-8")
    profile = load_profile(str(path))
    assert profile.domain_verbs == frozenset({"ESCALATES"})
    assert len(profile.domain_verbs) == 1
    assert profile.raw["edge_types"] == [{
        "type": "clinical.CONTRAINDICATES",
        "layer": "semantic",
        "description": "Medication or diagnosis contradiction",
    }]


def test_lts_prose_and_unknown_kinds_are_skipped_not_guessed(tmp_path):
    """A `.lts` profile is a normal document: an h1 doc node, section dividers, and
    prose nodes coexist with the declarations. None of them may leak into the
    projection as a half-formed record."""
    path = tmp_path / "x-profile.lts"
    path.write_text("""\
# X [PROFILE:X-DOC]

## [X-RATIONALE] Why this domain exists

Prose. No declaration properties at all.

## [PROFILE-NOT-THE-DOMAIN] Looks like the domain node, declares nothing

- status: draft

## [PREFIX-NOPRefix] A prefix node with no registers-prefix

- document: nowhere

## [PROFILE-DOMAIN] The real one

- domain-id: x.dom
- namespaces: x.dom
""", encoding="utf-8")
    profile = load_profile(str(path))
    assert profile.domain_id == "x.dom"
    assert profile.prefixes == []
    assert profile.document_tags == frozenset()
    assert profile.guidance_notes == []


# --- ⛔ the bootstrapping invariant ------------------------------------------
def test_lts_profile_using_its_own_bare_verb_is_rejected(tmp_path):
    """⛔ A profile MUST be parseable with CORE notation only.

    `GOVERNS` is a verb this very profile registers. Parsed core-only (which is all
    the loader CAN do — the profile is what would supply the domain settings) that
    triple flags `unregistered_verb`, meaning the document parses DIFFERENTLY before
    and after it is loaded. We reject rather than project a half-understood profile.
    """
    path = tmp_path / "x-profile.lts"
    path.write_text("""\
# X [PROFILE:X]

## [PROFILE-DOMAIN] d

- domain-id: x.dom
- namespaces: x.dom

## [EDGETYPE-GOVERNS] g

- edge-type: GOVERNS
- tier: domain

## Relationships

- [PROFILE-DOMAIN] GOVERNS [EDGETYPE-GOVERNS]
""", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_profile(str(path))
    assert "BOOTSTRAPPING INVARIANT" in str(exc.value)
    assert "unregistered_verb" in str(exc.value)


def test_lts_profile_using_its_own_namespace_is_rejected(tmp_path):
    """Same invariant, the namespaced half: `x.GOVERNS` is registered BY this file,
    so core-only parsing flags it `unregistered_ns`."""
    path = tmp_path / "x-profile.lts"
    path.write_text("""\
# X [PROFILE:X]

## [PROFILE-DOMAIN] d

- domain-id: x.dom
- namespaces: x

## [EDGETYPE-GOVERNS] g

- edge-type: x.GOVERNS
- layer: semantic

## Relationships

- [PROFILE-DOMAIN] x.GOVERNS [EDGETYPE-GOVERNS]
""", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_profile(str(path))
    assert "BOOTSTRAPPING INVARIANT" in str(exc.value)
    assert "unregistered_ns" in str(exc.value)


def test_lts_profile_may_use_CORE_notation_freely(tmp_path):
    """The invariant bans the domain's OWN notations, not structure. A profile may
    carry core edges (`DEPENDS_ON`) — every parser understands those without it."""
    path = tmp_path / "x-profile.lts"
    path.write_text("""\
# X [PROFILE:X]

## [PROFILE-DOMAIN] d

- domain-id: x.dom
- namespaces: x.dom

## [PREFIX-REQ] Requirements

- registers-prefix: REQ-
- depends-on: [PROFILE-DOMAIN]

## Relationships

- [PREFIX-REQ] DEPENDS_ON [PROFILE-DOMAIN]
""", encoding="utf-8")
    profile = load_profile(str(path))
    assert profile.prefix_set == frozenset({"REQ-"})
    assert len(profile.prefixes) == 1


# --- discovery ---------------------------------------------------------------
def test_discover_profile_finds_an_lts_profile(tmp_path):
    (tmp_path / "acme-ds-profile.lts").write_text(ACME_LTS, encoding="utf-8")
    found = discover_profile(str(tmp_path))
    assert found is not None and found.endswith("acme-ds-profile.lts")
    assert load_profile(found).domain_id == "acme.ds"


def test_discover_profile_still_finds_the_legacy_yaml(tmp_path):
    # Deployed profiles are LIVE in this form; discovery must not regress for them.
    (tmp_path / "99-acme-ds-profile.yaml").write_text(ACME_YAML, encoding="utf-8")
    found = discover_profile(str(tmp_path))
    assert found is not None and found.endswith("99-acme-ds-profile.yaml")


def test_discover_profile_prefers_lts_when_a_corpus_carries_both(tmp_path):
    # A corpus mid-migration. `.lts` is the form the maintainer ruled profiles move TO, so it
    # wins deterministically rather than by glob-ordering accident.
    (tmp_path / "99-acme-ds-profile.yaml").write_text(ACME_YAML, encoding="utf-8")
    (tmp_path / "acme-ds-profile.lts").write_text(ACME_LTS, encoding="utf-8")
    assert discover_profile(str(tmp_path)).endswith("acme-ds-profile.lts")


def test_discover_profile_ignores_ordinary_lts_documents(tmp_path):
    # `*-profile.lts`, NOT `*.lts` — otherwise every document in a corpus would be a
    # candidate profile and discovery would load the alphabetically-first one.
    (tmp_path / "10-architecture.lts").write_text("# A [SPEC:A]\n", encoding="utf-8")
    (tmp_path / "agentic_decisions.lts").write_text("# B [SPEC:B]\n", encoding="utf-8")
    assert discover_profile(str(tmp_path)) is None


def test_unsupported_extension_still_raises(tmp_path):
    path = tmp_path / "profile.txt"
    path.write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_profile(str(path))
    assert "unsupported profile extension" in str(exc.value)
    assert ".lts" in str(exc.value)


def test_public_api_exports_the_typed_surface():
    # A consumer in another package must be able to `from factlattice import PrefixSpec`
    # rather than reach into the private module.
    assert factlattice.PrefixSpec is PrefixSpec
    assert factlattice.LifecycleStages is LifecycleStages
    assert factlattice.PROFILE_GLOB_LTS == "*-profile.lts"
