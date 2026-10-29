"""THE SECOND HARM MODEL — read-suppression (the maintainer's amendment, 2026-08-16, Era 1).

`lts_lint` previously counted sentence-shaped titles and explicitly declined to flag
them, asserting that citation-travel "is the entire harm model". It is not. A name that
reads as SELF-EVIDENT suppresses reading the body that carries the real guidance, and
that harm needs nothing to travel anywhere — it lands in the reader, while skimming.

⛔ EVERY POSITIVE HERE IS PAIRED WITH THE NEGATIVE THAT WOULD MAKE IT MEANINGLESS. A
naming rule is the easiest kind of instrument to build wrong: it is trivial to write one
that fires on everything (and gets disabled within a day) or on nothing (and reports a
clean corpus forever). The non-firing cases below are therefore not padding — they are
the measurement that the rule DISCRIMINATES.
"""
from __future__ import annotations

from factlattice_engine.lint.claim_id import (
    KIND_READ_SUPPRESSING,
    classify,
    directive_opener,
    directive_title,
)


# --------------------------------------------------------------------------- #
# THE SIGNAL FIRES
# --------------------------------------------------------------------------- #
def test_davids_own_example_is_caught():
    """`ALWAYS-QUERY-BROADLY` — the shape the maintainer named. Easy to skip when busy."""
    assert directive_opener("PRINCIPLE-ALWAYS-QUERY-BROADLY", "PRINCIPLE") == "ALWAYS"


def test_a_directive_past_a_sub_family_code_and_an_ordinal_still_counts():
    """`ACME-BOOT-CORE-30-NEVER-TRUST-…` — requiring position 0 exactly would miss every
    namespaced family, which is most of the corpus."""
    assert directive_opener("ACME-BOOT-CORE-30-NEVER-TRUST-AN-UNGUARDED-ZERO",
                            "ACME") == "NEVER"


def test_an_imperative_title_is_in_scope():
    """⛔ THE AMENDMENT ITSELF. The old exemption said a title does not travel into edges
    — true, and irrelevant: a title travels into every find-result row and digest
    listing, which is exactly where skimming happens."""
    assert directive_title("Always cold with the bootstrap, vs resume-if-available")
    assert directive_title("Prefer a SELF-PROVING RECEIPT to a verified claim")


# --------------------------------------------------------------------------- #
# THE NEGATIVE CONTROLS — the rule must NOT fire
# --------------------------------------------------------------------------- #
def test_a_subject_naming_id_is_silent():
    """The convention working. If this ever fires, the rule flags compliance."""
    assert directive_opener("ACME-TRUNCATION-RISK", "ACME") is None
    assert directive_opener("DEC-MODEL-ROUTING", "DEC") is None


def test_a_descriptive_title_is_silent():
    assert directive_title("The console runs its data layer IN-PROCESS") is None
    assert directive_title("State Injector") is None, (
        "`State` is a NOUN here. Admitting bare tier-2 verbs on initial position alone "
        "measured ~50% precision — this is the case that forced the determiner rule.")


def test_an_interrogative_is_not_a_directive():
    """`Do …?` ASKS rather than instructs, so it suppresses no reading — it invites it.
    This was the single false positive in the 28-title survey."""
    assert directive_title(
        "Do the asset GET and PUT routes constrain the path the way listing does?") is None


def test_a_tier_two_verb_without_an_object_phrase_is_silent():
    """`Build pipeline order` is a NOUN PHRASE; `Build a linter` is a SENTENCE. One
    token of grammar, and it is what moved precision from ~50% to 100%."""
    assert directive_title("Build pipeline order") is None
    assert directive_title("Build a DETERMINISTIC FactLattice linter") == "BUILD"


def test_tier_two_verbs_are_never_admitted_from_an_ID():
    """⛔ MEASURED: admitting them produced 12 findings of the form `ACME-BUILD-PHASE-0`,
    `ACME-BUILD-PLAN`, `SBR-READ-VS-REASON` — every one a NOUN naming a subject, i.e.
    exactly what the naming convention asks for. An id carries no determiner to
    disambiguate with, so tier 2 is title-only."""
    for nid in ("ACME-BUILD-PHASE-0", "ACME-BUILD-PLAN", "ACME-BUILD-AGENTS",
                "SBR-READ-VS-REASON", "LEC-READ-PATH-VANISHED-ROOT"):
        assert directive_opener(nid, nid.split("-")[0]) is None, nid


# --------------------------------------------------------------------------- #
# THE TWO HARMS ARE INDEPENDENT — the load-bearing claim
# --------------------------------------------------------------------------- #
def test_the_genre_licence_is_INVERTED_between_the_two_rules():
    """⭐ THE DESIGN ARGUMENT, AS AN ASSERTION.

    Rule 1 (citation leakage) LICENSES claim-genre families: a PRINCIPLE that does not
    assert something is a principle that does not exist. Rule 2 (read suppression) must
    NOT license them — a principle whose name is a finished instruction is the single
    most skippable artifact in the corpus, and the maintainer's own example is principle-shaped.

    If a future edit gives rule 2 a genre licence "for consistency", this test is what
    says no."""
    nid, prefix = "STRATEGY-DONT-LEAP-BEFORE-LOOKING", "STRATEGY"
    assert classify(nid, prefix) is None, "rule 1 licenses the claim genre — unchanged"
    assert directive_opener(nid, prefix) == "DONT", (
        "rule 2 must NOT inherit that licence, or it goes silent on its best findings")


def test_the_two_rules_select_disjoint_populations_on_the_real_corpus():
    """MEASURED 2026-08-16 over 1,900 live nodes: 24 read-suppression findings, 7
    claim-shaped ids, and ZERO overlap. That disjointness is why widening rule 1 could
    never have found this class."""
    import os

    import pytest

    root = os.environ.get("FACTLATTICE_LIVE_CORPORA_ROOT", "")  # machine-local; unset => skip
    if not root or not os.path.isdir(root):
        pytest.skip("deployment corpora not present on this machine")

    from factlattice_engine.lint.core import lint
    r = lint(source=root)
    rs = {f.node_id for f in r.findings if f.kind == KIND_READ_SUPPRESSING}
    cs = {f.node_id for f in r.findings if f.kind == "claim_shaped_id"}
    assert rs, "the rule found nothing at all — suspicious, not clean"
    assert not (rs & cs), f"the two harms overlapped on {sorted(rs & cs)}"


def test_the_census_no_longer_claims_travel_is_the_entire_harm_model():
    """The corrected sentence must not be ASSERTED — but it is deliberately QUOTED.

    ⛔ THE DISTINCTION IS THE POINT, and getting it wrong is how a correction becomes
    invisible. Deleting the old claim would leave a reader unable to tell that the harm
    model ever changed, or why 454 titles were previously waved through. So the note
    carries the old sentence INSIDE its own supersession, exactly as SPEC's ⛔AMENDED
    blocks do ("old text superseded, not deleted"). This test therefore asserts the
    quote is bound to its retraction, not that it is absent."""
    from factlattice_engine.lint.claim_id import census

    class _C:
        nodes = []
    note = census(_C())["note"]
    assert "TWO HARM MODELS" in note
    assert "READ SUPPRESSION" in note
    quote = "which is the entire harm model"
    assert quote in note, "the superseded claim is quoted, so the change is legible"
    tail = note.split(quote, 1)[1]
    assert "INCOMPLETE" in tail and "superseded" in tail, (
        "the old claim appears WITHOUT its retraction — that reads as the note still "
        "asserting it")
