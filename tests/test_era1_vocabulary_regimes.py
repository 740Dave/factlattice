"""ERA 1 — the format-version marker (A1) and the vocabulary regimes (A2/A3).

⛔ THE REGRESSION BAR IS THE FIRST TEST IN THIS FILE AND IT IS ABSOLUTE. Zero of the 94
live corpus files carried a version marker when A1 landed, so ABSENCE is the state of the
entire installed base. If a legacy document ever parses differently, this era has broken
every corpus that exists, and no other property of it matters.
"""
from __future__ import annotations

import pytest

from factlattice.core_keys import core_keys, is_private_key, suggest_keys
from factlattice.parser import MODE_LEGACY, MODE_STRICT, parse_text
from factlattice.parser_config import ParserConfig

DOC = """# D [SPEC:E1-DOC]

## [E1-A] a node
- definition: a core key
- see-also: [E1-B]
- lifecycle-position: a profile-declared key
- x-scratch: an explicitly private key
- see-alos: a typo of a core key
- totally-made-up: near nothing declared

## [E1-B] b
"""
STAMPED = '---\nlts: "1.1"\n---\n\n' + DOC


def _vocab(text, **cfg_kw):
    pf = parse_text(text, "t.lts", config=ParserConfig.from_kwargs(**cfg_kw))
    return pf, {f.kind for f in pf.vocabulary_findings}


# --------------------------------------------------------------------------- #
# A1 — THE MARKER
# --------------------------------------------------------------------------- #
def test_THE_REGRESSION_BAR_the_marker_never_changes_the_graph():
    """⛔ THE ONE ASSERTION THE WHOLE ERA RESTS ON. The same bytes, one frontmatter
    field apart, MUST produce the same nodes, the same properties and the same edges.
    If the marker changed the graph, strictness would stop being stageable and a corpus
    could no longer migrate one document at a time — which is the only reason the field
    exists at all."""
    legacy, _ = _vocab(DOC)
    strict, _ = _vocab(STAMPED)
    assert legacy.mode == MODE_LEGACY and strict.mode == MODE_STRICT
    assert ([(n.id, sorted((n.properties or {}).items())) for n in legacy.nodes]
            == [(n.id, sorted((n.properties or {}).items())) for n in strict.nodes])
    assert ([(e.source, e.type, e.target, e.layer, sorted(e.flags)) for e in legacy.edges]
            == [(e.source, e.type, e.target, e.layer, sorted(e.flags)) for e in strict.edges])


def test_an_unstamped_document_is_legacy_and_is_never_refused():
    pf, kinds = _vocab(DOC)
    assert pf.format_version is None and pf.mode == MODE_LEGACY
    assert "legacy_document" in kinds
    assert pf.nodes, "a legacy document still parses fully — absence is not an error"


@pytest.mark.parametrize("raw,version", [
    ('lts: "1.1"', "1.1"), ("lts: '1.1'", "1.1"), ("lts: 1.1", "1.1")])
def test_quoting_is_not_semantic(raw, version):
    """A format whose meaning depended on YAML quoting style would itself be a
    silent-loss mechanism."""
    pf, _ = _vocab(f"---\n{raw}\n---\n\n# D [SPEC:Q]\n\n## [Q-A] a\n")
    assert pf.format_version == version and pf.mode == MODE_STRICT


def test_an_unknown_version_is_flagged_and_parsed_STRICTLY_not_degraded():
    """⛔ NOT legacy. `lts: "9.9"` is an author asking for a contract this parser does
    not implement; reading it under the older, looser rules would answer confidently
    under rules the author never asked for."""
    pf, kinds = _vocab('---\nlts: "9.9"\n---\n\n# D [SPEC:U]\n\n## [U-A] a\n')
    assert pf.mode == MODE_STRICT
    assert "format_version_unsupported" in kinds


def test_an_empty_marker_is_reported_not_silently_defaulted():
    """`lts:` with nothing after it is an author reaching for the field and losing the
    value. Defaulting silently would swallow the attempt."""
    _, kinds = _vocab("---\nlts:\n---\n\n# D [SPEC:E]\n\n## [E-A] a\n")
    assert "format_version_empty" in kinds


# --------------------------------------------------------------------------- #
# A2 — KEYS: DECLARED-OR-MARKED
# --------------------------------------------------------------------------- #
def test_the_three_tiers_discriminate():
    """core parses / profile-declared parses / `x-` parses — only the genuinely
    undeclared are named. A rule that flagged the core key or the marked key would be
    switched off within a day."""
    pf, _ = _vocab(DOC, declared_property_keys=["lifecycle-position"])
    named = {f.key for f in pf.vocabulary_findings if f.kind == "undeclared_key"}
    assert named == {"see-alos", "totally-made-up"}


def test_the_declared_tier_is_NOT_INERT():
    """⛔ THE TEST THAT WOULD HAVE CAUGHT V5 ONE TIER OVER. Remove the declaration and
    the key must be flagged; supply it and the finding must vanish. Without this arm, a
    profile could declare `property_keys` and have it read by nothing — 'inert while
    reading as functional', which is §1.3.1 constraint 3's own words and the exact
    failure this era exists to close."""
    without, _ = _vocab(DOC)
    with_, _ = _vocab(DOC, declared_property_keys=["lifecycle-position"])
    def named(pf):
        return {f.key for f in pf.vocabulary_findings if f.kind == "undeclared_key"}
    assert "lifecycle-position" in named(without)
    assert "lifecycle-position" not in named(with_)


def test_the_marker_moves_severity_and_ONLY_severity():
    legacy, _ = _vocab(DOC)
    strict, _ = _vocab(STAMPED)
    def sev(pf):
        return {f.detail["severity"] for f in pf.vocabulary_findings
                if f.kind == "undeclared_key"}
    assert sev(legacy) == {"warning"}
    assert sev(strict) == {"error"}


def test_edit_distance_suggests_and_an_EMPTY_suggestion_is_meaningful():
    """`see-alos` is one edit from a DECLARED name. `totally-made-up` is near nothing,
    and the rule must NOT guess — it says declare/mark/rewrite instead."""
    pf, _ = _vocab(DOC)
    sugg = {f.key: f.detail["suggestions"] for f in pf.vocabulary_findings
            if f.kind == "undeclared_key"}
    assert sugg["see-alos"] == ["see-also"]
    assert sugg["totally-made-up"] == []


def test_x_marker_requires_something_after_it():
    """A bare `x-` declares privacy about nothing; letting it buy the exemption would
    hand a typo the marker's whole benefit."""
    assert is_private_key("x-anything")
    assert not is_private_key("x-")
    assert not is_private_key("xylophone"), "the marker is `x-`, not the letter x"


def test_core_keys_are_read_from_the_parser_never_restated():
    """A second copy of the minting table would drift, and the drift would present as a
    CORRECT key reported undeclared — a false positive on the one diagnostic whose whole
    value is precision."""
    from factlattice.parser import FLOW_PROPERTY_EDGES, REL_PROPERTY_EDGES
    assert set(REL_PROPERTY_EDGES) <= core_keys()
    assert set(FLOW_PROPERTY_EDGES) <= core_keys()


def test_suggestions_are_deterministic():
    """A diagnostic that reorders between runs cannot be asserted on."""
    d = frozenset({"see-also", "see-alsa", "definition"})
    assert suggest_keys("see-alos", d) == suggest_keys("see-alos", d)


# --------------------------------------------------------------------------- #
# A3 — VERBS
# --------------------------------------------------------------------------- #
VERB_DOC = """# D [SPEC:V-DOC]

## [V-A] a

## [V-B] b

## Relationships
### Semantic
- [V-A] BUILDS_ON the earlier design, described in prose
"""


def test_the_prose_target_line_no_longer_vanishes_silently():
    """⛔ THE HALF OF A3 THAT MATTERS. This line is inside `## Relationships`, so its
    author unambiguously meant an edge — and before this diagnostic the WHOLE LINE was
    discarded with no edge, no flag, no finding and no line-accounting record, because
    the line WAS consumed. §2.4's 'preserved but flagged' covers the bracket-anchored
    case only; this one was never specified at all."""
    pf, kinds = _vocab(VERB_DOC)
    assert "unrecognized_verb_line" in kinds
    assert not [e for e in pf.edges if e.type == "BUILDS_ON"], (
        "it must still NOT mint — fabricating an unknown verb's type would be a wrong "
        "edge, which is worse than a missing one")


def test_registering_the_verb_silences_it_THE_NEGATIVE_CONTROL():
    """Without this arm a rule that flagged every Relationships line would score green
    above. It also proves `domain_verbs` actually reaches the parse — the V5b hook."""
    _, kinds = _vocab(VERB_DOC, domain_verbs=frozenset({"BUILDS_ON"}))
    assert "unrecognized_verb_line" not in kinds


def test_a_genuinely_prose_relationships_line_is_silent():
    """No UPPER_SNAKE token, no finding. Otherwise this fires on every explanatory line
    in the section and gets switched off within a day."""
    _, kinds = _vocab("""# D [SPEC:P-DOC]

## [P-A] a

## Relationships
### Semantic
- [P-A] is the reason we stopped doing it the old way
""")
    assert "unrecognized_verb_line" not in kinds
