"""Two aggregates that returned confident, well-formed WRONG or USELESS answers.

⛔ NEGATIVE-CONTROL DISCIPLINE: both behaviours below were reproduced against live corpus
data BEFORE the fix, with the measured numbers recorded in each test.
"""
from __future__ import annotations

from factlattice_engine.analytics.core import strip_inline_commentary
from factlattice_engine.analytics.exemplifies import _diagnose


# --------------------------------------------------------------------------
# group_by — trailing inline commentary split 14 fixtures across 71 buckets
# --------------------------------------------------------------------------

REAL_RE_DERIVE_VIA_VALUES = [
    # verbatim from corpora/, docs/ and defects/ .lts files
    "[FIXTURE-registration-protocol-P6]",
    "[FIXTURE-registration-protocol-P6]   # the light from outside the self",
    "[FIXTURE-registration-protocol-P6]   # measured, not guessed",
    "[FIXTURE-registration-protocol-P1]",
    "[FIXTURE-registration-protocol-P1]   # derivation lives where the input lands",
    "[FIXTURE-continuity-handoff-process]   # structure survives the boundary, prose leaks",
    "[FIXTURE-continuity-handoff-process]",
]


def test_inline_commentary_no_longer_splits_the_bucket():
    """Measured 2026-08-15 over corpora/ + docs/ + defects/: the raw grouping produced
    71 buckets for 16 real fixtures. `FIXTURE-registration-protocol-P6` reported 9
    against a true 27; `-P1` reported 1 against a true 25 — the second most-cited
    fixture reading as almost absent.

    ⛔ The cause is CAUSAL, not incidental: the fixtures reasoned about most carefully
    attracted the most inline commentary, and commentary is what splits the bucket. So
    emphasis is INVERSELY correlated with visibility — the third measured instance of
    that shape in this codebase.
    """
    raw = {v for v in REAL_RE_DERIVE_VIA_VALUES}
    normalized = {strip_inline_commentary(v) for v in REAL_RE_DERIVE_VIA_VALUES}
    assert len(raw) == 7, "the raw values are genuinely distinct strings"
    assert normalized == {
        "[FIXTURE-registration-protocol-P6]",
        "[FIXTURE-registration-protocol-P1]",
        "[FIXTURE-continuity-handoff-process]",
    }, "normalization must collapse commentary and NOTHING else"


def test_normalization_does_not_eat_a_legitimate_hash():
    """A `#` that is not a trailing gloss must survive — otherwise the fix trades a
    split bucket for a corrupted value, which is the worse failure."""
    for keep in ["issue #42", "#ff0000", "the C# binding", "a#b"]:
        assert strip_inline_commentary(keep) == keep


def test_normalization_requires_whitespace_on_both_sides():
    assert strip_inline_commentary("[A]   # gloss") == "[A]"
    assert strip_inline_commentary("[A] # gloss") == "[A]"
    assert strip_inline_commentary("[A]#notagloss") == "[A]#notagloss"


# --------------------------------------------------------------------------
# exemplifies_coverage — a zero that could not be diagnosed
# --------------------------------------------------------------------------

def test_zero_with_no_edges_says_it_cannot_distinguish_never_authored_from_never_minted():
    msg = _diagnose(total=206, edge_count=0, edges_seen=0, off_target={},
                    non_decision_sources=0)
    assert "NO EXEMPLIFIES EDGE EXISTS" in msg
    assert "mints no edge" in msg, (
        "the report must name the property-key hypothesis; a bare zero cannot "
        "distinguish 'never authored' from 'authored as a non-minting property'")


def test_zero_with_off_target_edges_names_the_target_family():
    """Measured on acme-console: 17 EXEMPLIFIES edges existed, none targeting a
    PRINCIPLE-* node, and the report said only `coverage_ratio: 0.0`."""
    msg = _diagnose(total=206, edge_count=0, edges_seen=17,
                    off_target={"DX": 9, "ACME": 8}, non_decision_sources=0)
    assert "17 EXEMPLIFIES edge(s) EXIST" in msg
    assert "DX=9" in msg and "ACME=8" in msg
    assert "different target family" in msg


def test_non_zero_coverage_reports_no_diagnosis():
    assert _diagnose(total=10, edge_count=3, edges_seen=3, off_target={},
                     non_decision_sources=0) is None


def test_no_decisions_is_undefined_not_zero():
    msg = _diagnose(total=0, edge_count=0, edges_seen=0, off_target={},
                    non_decision_sources=0)
    assert "undefined" in msg
