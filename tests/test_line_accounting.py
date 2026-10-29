"""[FL-LINE-ACCOUNTING] — the silent-drop invariant.

    Every non-blank line of a `.lts` file is either CONSUMED by a parser branch, or
    CLASSIFIED as ignorable. Anything else is a lint warning.

The positive half of this file reproduces, hermetically, each silent-drop mechanism
found in the wild. The negative half is the more important half: it pins the shapes
that MUST stay silent. A rule that fires on legitimate prose, a nested property
block, a bold key, a multi-value line, or a well-formed edge triple is a rule that
gets switched off within a day, and then the invariant protects nothing.
"""
from __future__ import annotations

import os
import tempfile

import pytest

import factlattice
from factlattice.coverage import (
    IGNORABLE_KINDS,
    KIND_CONSUMED,
    KIND_NEAR_MISS_PROPERTY,
    KIND_NEAR_MISS_QUALIFIED_DASH,
    KIND_NEAR_MISS_QUALIFIED_PAREN,
    KIND_NEAR_MISS_TRIPLE,
    UNACCOUNTED_KINDS,
    account_file,
    unaccounted_lines,
)
from factlattice_engine.lint import coverage_stats, find_unaccounted, lint


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _cov(text: str):
    return factlattice.parse_text(text, "fixture.lts").coverage


def _findings(text: str):
    return unaccounted_lines(_cov(text))


def _variants(text: str):
    return sorted(v.detail.get("variant", v.kind) for v in _findings(text))


@pytest.fixture()
def corpus_dir():
    with tempfile.TemporaryDirectory(prefix="fl-lineacct-") as d:
        yield d


def _write(d: str, name: str, text: str) -> str:
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


# ---------------------------------------------------------------------------
# TOTALITY — the accounting must cover every line, exactly once, in one class
# ---------------------------------------------------------------------------
NEGATIVE_FIXTURE = """\
---
domain: test.demo
status: draft
---

# The Clean Fixture [SPEC:CLEAN]

- kind: fixture-root

## 1. Node A [CLEAN-A]

Free prose in a node body, with a colon: exactly the shape SPEC 2.3 permits, and
which is 96% of candidate lines on the surveyed corpora.

- status: active
- **bold-key**: a bold key, well formed
- see-also: [CLEAN-B], [CLEAN-C]
- nested:
  - first item
  - second item

> a blockquote callout: with a colon in it

| column | other |
|--------|-------|
| a      | b     |

```python
config = {"key": "value", "other": "thing"}
```

1. an ordered item: with a colon
2. another one

- a narrative bullet mentioning https://example.com/x and doc://coll/id and bus://a/b
- another narrative bullet, no key here at all

---

## 2. Node B [CLEAN-B]

- status: active

## 3. Node C [CLEAN-C]

- status: active

## Relationships

Prose introducing the block, with a colon: like so.

- [CLEAN-A] RELATED_TO [CLEAN-B]
- [CLEAN-B] DEPENDS_ON [CLEAN-C]

**Version** 1.0 · **Status** proposed
"""


def test_accounting_is_total_and_partitioned():
    cov = _cov(NEGATIVE_FIXTURE)
    verdicts = account_file(cov)
    assert len(verdicts) == len(cov.lines)
    assert [v.line_index for v in verdicts] == list(range(len(cov.lines)))
    for v in verdicts:
        assert (v.kind == KIND_CONSUMED
                or v.kind in IGNORABLE_KINDS
                or v.kind in UNACCOUNTED_KINDS), v
    # the three classes are disjoint by construction
    assert not (IGNORABLE_KINDS & UNACCOUNTED_KINDS)
    assert KIND_CONSUMED not in IGNORABLE_KINDS | UNACCOUNTED_KINDS


def test_negative_fixture_is_silent():
    """The load-bearing test. Prose, a nested block, a bold key, a multi-value line,
    a well-formed triple, tables, fences, ordered lists, blockquotes and a URL-bearing
    bullet must ALL stay quiet."""
    assert _findings(NEGATIVE_FIXTURE) == []


def test_negative_fixture_still_parses_its_properties():
    """Guards the guard: if the fixture stopped carrying real properties, the silence
    above would be vacuous."""
    pf = factlattice.parse_text(NEGATIVE_FIXTURE, "fixture.lts")
    a = next(n for n in pf.nodes if n.id == "CLEAN-A")
    assert a.properties["status"] == "active"
    assert a.properties["bold-key"] == "a bold key, well formed"
    assert a.properties["nested"] == ["first item", "second item"]
    assert "[CLEAN-B]" in a.properties["see-also"]
    assert len([e for e in pf.edges if e.inference == "authored"]) == 2


# ---------------------------------------------------------------------------
# MECHANISMS 1 and 2 — the two `## Relationships` triple drops.
#
# These are the mechanisms this invariant MEASURED and the companion parser fix
# REPAIRED (see tests/test_domain_verb_edges.py). The lint's job here is now the
# inverse of what it was: prove it goes QUIET on repaired content. A rule that keeps
# warning about something the parser now handles is a rule that gets muted.
# ---------------------------------------------------------------------------
MECH1 = """\
# M1 [SPEC:M1]

## [M1-A] A

- status: active

## [M1-B] B

- status: active

## Relationships

- [M1-A] USES [M1-B]
- [M1-A] DEPENDS_ON [M1-B]
[M1-B] RELATED_TO [M1-A]
"""


def test_repaired_relationship_triples_are_not_reported():
    pf = factlattice.parse_text(MECH1, "fixture.lts")
    authored = [e for e in pf.edges if e.inference == "authored"]
    assert [e.type for e in authored] == ["USES", "DEPENDS_ON", "RELATED_TO"]
    assert unaccounted_lines(pf.coverage) == []


# What is STILL dropped after the fix, and therefore still the lint's business: E2
# edge extraction only runs inside `## Relationships`, so an authored triple written
# anywhere else produces nothing at all.
TRIPLE_ADRIFT = """\
# TA [SPEC:TA]

## [TA-A] A

- status: active
- [TA-A] DEPENDS_ON [TA-B]

## [TA-B] B

- status: active
"""


def test_triple_outside_the_relationships_section_is_reported():
    pf = factlattice.parse_text(TRIPLE_ADRIFT, "fixture.lts")
    assert [e for e in pf.edges if e.inference == "authored"] == []
    found = unaccounted_lines(pf.coverage)
    assert [v.kind for v in found] == [KIND_NEAR_MISS_TRIPLE]
    assert found[0].detail["verb"] == "DEPENDS_ON"
    assert found[0].near_branch == "_parse_section_edges"
    assert "outside `## Relationships`" in found[0].detail["why"]


# An unregistered verb that does NOT sit directly before a bracketed target stays
# out of the parser's preserve-and-flag path (requiring the bracket is what stops
# prose minting edges), so the line is still dropped -- and still reported.
TRIPLE_INTERPOSED = """\
# TI [SPEC:TI]

## [TI-A] A

- status: active

## [TI-B] B

- status: active

## Relationships

- [TI-A] SUPPLANTS [TI-B]
"""


def test_unregistered_verb_before_a_bracketed_target_is_preserved_not_reported():
    pf = factlattice.parse_text(TRIPLE_INTERPOSED, "fixture.lts")
    (e,) = [x for x in pf.edges if x.inference == "authored"]
    assert e.type == "SUPPLANTS" and "unregistered_verb" in e.flags
    assert unaccounted_lines(pf.coverage) == []


# ---------------------------------------------------------------------------
# MECHANISM 3 — the near-miss property, all three measured classes
# ---------------------------------------------------------------------------
MECH3 = """\
# M3 [SPEC:M3]

## [M3-A] A

- Status: capitalised key, class C
- **pull-when: the wrapper opens before the key, class A**
- ⛔ invariant: a pictograph prefix, class B
- context_for_change:
  * prior_state: "a `*` bullet, which P4's `-`-anchored item regex does not read"
"""

# NOTE on the 4th case above. It USED to be authored as SPEC §8.1's YAML mapping
# (`    prior_state: "…"`, indented, unbulleted). That was a genuine `empty_block`
# then — the parser had no mapping form, so the block collected nothing and the whole
# property was dropped. P5 (SPEC §8.1/§8.2/§7.1, `test_p5_*` in test_parser.py) closes
# that, and the mapping shape is now CONSUMED. The `empty_block` variant is still real
# and still worth a positive control, so the case is re-authored with a `*` bullet —
# one of the three causes `_WHY["empty_block"]` actually names.


def test_mechanism_3_near_miss_property_classes():
    pf = factlattice.parse_text(MECH3, "fixture.lts")
    node = next(n for n in pf.nodes if n.id == "M3-A")
    for absent in ("status", "Status", "pull-when", "invariant", "context_for_change"):
        assert absent not in node.properties

    found = unaccounted_lines(pf.coverage)
    assert [v.kind for v in found] == [KIND_NEAR_MISS_PROPERTY] * 4
    assert [v.detail["variant"] for v in found] == [
        "case", "bold_wrap", "symbol_prefix", "empty_block",
    ]
    assert [v.detail["key"] for v in found] == [
        "Status", "pull-when", "invariant", "context_for_change",
    ]
    assert found[0].near_branch == "_PROP_SIMPLE_RE"
    assert found[1].near_branch == "_PROP_BOLD_RE"
    assert found[3].near_branch == "_PROP_KEY_ONLY_RE"


# ---------------------------------------------------------------------------
# MECHANISM 3b (QUAL) — SL-21 / SL-22: the KEY REGION carries a QUALIFIER.
#
# This is the four-line probe that reproduced the defect, verbatim. Before the
# detector existed the parse of this fixture was `{alpha, delta}` with `lts_lint` at 0
# findings and `lts_preview` at 100.0% coverage / 0 lost — the line was consumed by no
# branch, produced no property, and was CLASSIFIED AS PROSE, which line accounting
# counts as accounted for. Both instruments were green while half the properties were
# gone. `delta` is the EXCLUSION: a parenthesis in the VALUE is fine.
# ---------------------------------------------------------------------------
MECH3B = """\
# Probe [SPEC:M3B]

## [M3B-A] The four-line probe

- alpha: bare form
- beta (qualified): parenthetical form
- gamma — dashed: em-dash form
- delta: (leading paren in value) form
"""


def test_mechanism_3b_qualified_key_region_is_flagged_not_parsed():
    pf = factlattice.parse_text(MECH3B, "fixture.lts")
    node = next(n for n in pf.nodes if n.id == "M3B-A")

    # THE PARSE. `alpha` and `delta` land; `beta` and `gamma` land NOWHERE — and, the
    # point of the ruling, they do not land under a GUESSED key either. A substitution
    # is worse than a loss (SL-18), so the absence is asserted for BOTH spellings.
    assert node.properties == {
        "alpha": "bare form",
        "delta": "(leading paren in value) form",
    }
    for guessed in ("beta", "beta (qualified)", "gamma", "gamma — dashed"):
        assert guessed not in node.properties

    # THE FLAG. Two findings, two DISTINCT kinds — the classes are counted apart.
    found = unaccounted_lines(pf.coverage)
    assert [v.kind for v in found] == [
        KIND_NEAR_MISS_QUALIFIED_PAREN, KIND_NEAR_MISS_QUALIFIED_DASH,
    ]
    assert [v.detail["variant"] for v in found] == ["qualified_paren", "qualified_dash"]
    assert [v.detail["key"] for v in found] == ["beta", "gamma"]
    assert [v.detail["qualifier"] for v in found] == ["(qualified)", "— dashed"]
    assert [v.line_number for v in found] == [6, 7]
    # The WHY names the REPAIR, because the parser will not guess the key and the author
    # is the only one who can resolve the ambiguity.
    for v in found:
        assert "move the qualifier into the VALUE" in v.detail["why"]
        assert v.near_branch == "_PROP_SIMPLE_RE"


def test_mechanism_3b_a_paren_in_the_value_is_not_a_qualifier():
    """THE EXCLUSION, on its own, in the shapes that make it discriminating.

    Each line below puts a parenthesis and/or an em-dash — and a SECOND COLON — in the
    VALUE. Every one must parse to its exact authored string. This is what separates
    "the key region carries a qualifier" from "the line contains a parenthesis": an
    over-eager rule satisfies the positives above and destroys every line here.
    """
    text = """\
# Probe [SPEC:M3BX]

## [M3B-EX] Parens and dashes in the value

- delta: (leading paren in value) form
- epsilon: (leading paren) and a note (measured): still ONE property
- zeta: an em-dash inside a value — and a colon after it: both stay in the value
- eta: a hyphen-minus - qualifier: NOT in the class, deliberately
"""
    pf = factlattice.parse_text(text, "fixture.lts")
    node = next(n for n in pf.nodes if n.id == "M3B-EX")
    assert node.properties == {
        "delta": "(leading paren in value) form",
        "epsilon": "(leading paren) and a note (measured): still ONE property",
        "zeta": "an em-dash inside a value — and a colon after it: both stay in the value",
        "eta": "a hyphen-minus - qualifier: NOT in the class, deliberately",
    }
    assert unaccounted_lines(pf.coverage) == []


# ---------------------------------------------------------------------------
# MECHANISM 4 — found BY this invariant, not before it: an un-tagged sub-heading
# ends the enclosing node's body, orphaning every property under it.
# ---------------------------------------------------------------------------
MECH4 = """\
# M4 [SPEC:M4]

## 2. Palette Module [M4-PALETTE]

### Contract

- role: display the widget catalogue
- class: `PaletteModule`
"""


def test_mechanism_4_properties_orphaned_by_an_untagged_subheading():
    pf = factlattice.parse_text(MECH4, "fixture.lts")
    node = next(n for n in pf.nodes if n.id == "M4-PALETTE")
    assert node.properties == {}      # the whole Contract block is invisible

    found = unaccounted_lines(pf.coverage)
    assert [v.detail["variant"] for v in found] == ["outside_node_body"] * 2
    assert found[0].detail["orphaned_from"] == "no enclosing [ID]-bearing heading"
    assert "no `[ID]`" in found[0].detail["why"]


# ---------------------------------------------------------------------------
# SECTION AWARENESS — the rule must not fire on the corpus's own edge idiom
# ---------------------------------------------------------------------------
SECTION_AWARE = """\
# SA [SPEC:SA]

## [SA-A] A

- status: active

## [SA-B] B

- status: active

## Relationships

- note: the subheads below name the layer
- [SA-A] RELATED_TO [SA-B]

### Semantic

- [SA-B] DEPENDS_ON [SA-A]
"""


def test_relationships_section_skips_the_property_shape_check():
    """`- note: ...` inside `## Relationships` is prose there, not a dropped property.
    Dogfooding the un-guarded rule against the proposal that motivated it produced
    exactly one hit: the proposal's own edge triple."""
    assert _findings(SECTION_AWARE) == []


# ---------------------------------------------------------------------------
# PRECISION GUARDS — shapes the naive predicate gets wrong
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("body_line", [
    "- see the docs at https://example.com/a:b for detail",
    "- the asset resolves through bus://assets/icons/relay.png at runtime",
    "- (deferred: this parenthetical is prose, not a property)",
    "- `uri: \"doc://coll/id\"` is the literal we write",
    "- Do NOT confuse: this reads as a sentence, and the key would carry a space",
    "- 2026-08-10: a date-led log line is not a property key",
    "- a bullet with a mid-sentence colon: and a trailing clause",
])
def test_precision_guards_stay_silent(body_line):
    text = f"# G [SPEC:G]\n\n## [G-A] A\n\n{body_line}\n"
    assert _findings(text) == [], body_line


def test_repeated_and_multivalue_keys_are_consumed_not_flagged():
    """Four of the parser's five legitimate parsed-!=-written transforms in one node.
    An EQUALITY check drowns here; a COVERAGE check does not, because every one of
    them still consumes its source line."""
    text = (
        "# T [SPEC:T]\n\n## [T-A] A\n\n"
        "- and-when: first conjunct\n"
        "- and-when: second conjunct\n"
        "- see-also: [T-B], [T-C]\n"
        "- **bold**:   value with padding   \n"
        "- listy:\n  - one\n  - two\n"
    )
    pf = factlattice.parse_text(text, "fixture.lts")
    assert pf.nodes[1].properties["and-when"] == ["first conjunct", "second conjunct"]
    assert unaccounted_lines(pf.coverage) == []


def test_spec_8_1_yaml_mapping_is_accounted_for_not_a_finding():
    """REGRESSION LOCK for the mechanism this invariant surfaced and P5 closed.

    §8.1's documented `{change, context_for_change}` shape matched none of §2.2's four
    property forms, so the block collected nothing, the property was dropped whole, and
    the ONLY thing that said so was this invariant (`empty_block`, on the head line).
    P5 reads the mapping; the head, the entries and a wrapped tail are all consumed and
    the file is clean. If the mapping form ever regresses, this goes red before the
    corpus does."""
    text = (
        "# M [SPEC:M]\n\n## [M-A] A\n\n"
        "- change: \"the extractor changed\"\n"
        "- context_for_change:\n"
        "    prior_state: \"the limit was 12\"\n"
        "    invalidating_assumption: \"that the load distribution stays put,\n"
        "      which v4.1 is what moved\"\n"
    )
    pf = factlattice.parse_text(text, "fixture.lts")
    assert unaccounted_lines(pf.coverage) == []
    assert pf.findings == []
    value = pf.nodes[1].properties["context_for_change"]
    assert set(value) == {"prior_state", "invalidating_assumption"}


def test_frontmatter_continuation_is_reported():
    text = ("---\ndomain: test.demo\naudience: [a,\n  b]\n---\n\n"
            "# F [SPEC:F]\n\n## [F-A] A\n\n- status: active\n")
    found = _findings(text)
    assert [v.kind for v in found] == ["unaccounted_frontmatter"]
    assert found[0].line_number == 4


# ---------------------------------------------------------------------------
# THE PARSER OWNS THE RECORD — the linter must never re-derive the grammar
# ---------------------------------------------------------------------------
def test_every_consumed_index_names_a_real_parser_branch():
    from factlattice import parser as P
    known = {getattr(P, n) for n in dir(P) if n.startswith("BRANCH_")}
    cov = _cov(NEGATIVE_FIXTURE)
    assert cov.consumed
    assert set(cov.consumed.values()) <= known


def test_coverage_record_does_not_change_the_graph(corpus_dir):
    """The invariant is a LINT, not a parser behaviour change: turning it on must not
    move a single node or edge."""
    _write(corpus_dir, "a.lts", MECH1)
    _write(corpus_dir, "b.lts", NEGATIVE_FIXTURE)
    with_cov = lint(corpus_dir, coverage=True)
    without = lint(corpus_dir, coverage=False)
    assert with_cov.node_count == without.node_count
    assert with_cov.edge_count == without.edge_count
    assert ([f.to_dict() for f in with_cov.findings if f.origin != "coverage"]
            == [f.to_dict() for f in without.findings])
    assert with_cov.exit_code() == without.exit_code() == 0


# ---------------------------------------------------------------------------
# THE LINT ADAPTER
# ---------------------------------------------------------------------------
def test_lint_reports_coverage_findings_as_warnings(corpus_dir):
    _write(corpus_dir, "adrift.lts", TRIPLE_ADRIFT)
    _write(corpus_dir, "m3.lts", MECH3)
    result = lint(corpus_dir)
    cov_findings = result.by_origin("coverage")
    assert len(cov_findings) == 5
    assert {f.severity for f in cov_findings} == {"warning"}
    assert not result.has_errors and result.exit_code() == 0
    for f in cov_findings:
        assert f.source_file in ("adrift.lts", "m3.lts")
        assert f.source_line and f.detail["text"] and f.detail["near_branch"]
    rendered = result.render_text()
    assert "coverage.near_miss_triple" in rendered
    assert "adrift.lts:6" in rendered       # names file AND line
    assert "DEPENDS_ON" in rendered         # ...and the offending text
    assert "_parse_section_edges" in rendered   # ...and the branch that nearly took it


def test_coverage_severity_can_be_ratcheted_to_error(corpus_dir):
    _write(corpus_dir, "adrift.lts", TRIPLE_ADRIFT)
    result = lint(corpus_dir, coverage_severity={"near_miss_triple": "error"})
    assert result.has_errors and result.exit_code() == 1


def test_coverage_stats_account_for_every_line(corpus_dir):
    _write(corpus_dir, "clean.lts", NEGATIVE_FIXTURE)
    _write(corpus_dir, "m4.lts", MECH4)
    corpus = factlattice.parse(corpus_dir)
    s = coverage_stats(corpus)
    assert s["lines"] == sum(len(c.lines) for c in corpus.coverage)
    assert s["consumed"] + s["ignorable"] + s["unaccounted"] == s["lines"]
    assert s["unaccounted"] == len(find_unaccounted(corpus)) == 2
