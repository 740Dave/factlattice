"""THE `status` LIFECYCLE-TOKEN CONTRACT — `lifecycle.py` + the `lts_find` filter.

WHAT THIS FILE IS A RECEIPT FOR
-------------------------------
`lts_find(status=X)` matched `status` by EXACT STRING while the corpus authors
`<lifecycle-token> — <free-text qualifier>`. The session-bootstrap read implements its
DISPATCH GATE (no spec ships to a builder while an unratified `[DD-*]` remains in it) as
exactly that query, so the gate could not see its own blockers — including a node whose
status string reads "BLOCKS slice 2's scene wiring". A filter that returned 8 looked
exactly like a filter that worked.

STANDING RULE 3 governs every assertion here: EXACT COUNTS, never "non-zero" and never
"more than before". The synthetic corpus below is hand-counted; the qualified/untokenized
status STRINGS in it are copied VERBATIM from the live corpora that motivated the fix, so
this is a fixture in shape but real data in content.

The other half of the fix — lint rule V11, which makes the drift this filter now
tolerates VISIBLE instead of silently absorbed — is tested in
`test_profile_validation.py::test_v11_*`.
"""

from __future__ import annotations

import pytest

from factlattice_engine import (
    LtsEngine, LtsStore, Partition,
    is_bare_lifecycle_token, lifecycle_token, status_matches,
)


# ---------------------------------------------------------------------------
# THE TOKENIZER — what it normalizes, and what it deliberately REFUSES to
# ---------------------------------------------------------------------------
# Every string on the left is a REAL status value measured in the live corpora
# (acme `corpora/`), not an invented shape.
NORMALIZES = [
    ("proposed", "proposed"),
    ("proposed — awaiting ratify", "proposed"),
    ("proposed — flagged for the architect to ratify at Phase 2.", "proposed"),
    ("proposed — awaiting ratify (BLOCKS slice 2's scene wiring; does NOT block "
     "slices 0 or 1)", "proposed"),
    ("proposed — awaiting the census, then the maintainer's ratify (GATES slices 2, 3 and 4; "
     "does not gate", "proposed"),
    ("operational (2026-07-07)", "operational"),
    ("operational (2026-07-06).", "operational"),
    ("settled ([DEC-ACME-COMP-DDS-SETTLED])", "settled"),
    ("in-progress — core layout + composite model aligned", "in-progress"),
    ("open — blocked on a live-vehicle candump (the maintainer + the Fiat 500e)", "open"),
    ("open (target)", "open"),
    ("resolved; amended 2026-07-26 (freshest-source de-fork machine-green)", "resolved"),
    ("captured (the maintainer 2026-07-10); not build-ready.", "captured"),
    ("planned (spec) — build gated on the headless runtime", "planned"),
    # a hyphenated lifecycle is ONE token and normalizes to ITSELF, never to its head:
    ("resolved-pending-live-confirmation", "resolved-pending-live-confirmation"),
    ("surfaced-awaiting-reviewer", "surfaced-awaiting-reviewer"),
    ("  proposed  ", "proposed"),          # surrounding whitespace is not content
]

# "No token" beats a WRONG token. Each of these was NEVER queryable and does not become
# so; the point of the assertion is that none of them is silently mis-tokened into a
# lifecycle it does not have. Lint rule V11 reports every one of them.
YIELDS_NO_TOKEN = [
    "**operational (2026-07-07)**",                     # leading bold marker
    "**deferred to Pass 4** (Diagnosis)",               # leading bold marker
    "OPEN — flagged for the architect's ratify",        # upper-case lifecycle
    "UNCONFIRMED. Its author has explicitly downgraded the evidence",
    "Skia tier = **OPERATIONAL 2026-07-07** — C1–C5, C7 PASS",   # leading prose
    "spec'd; build queued",                             # `'` is not a boundary
    "classic-CYD pass = **OPERATIONAL 2026-07-07**",    # `-` is never a boundary
    "",
    "   ",
    None,
    42,
    ["proposed"],
]


@pytest.mark.parametrize("raw,token", NORMALIZES)
def test_lifecycle_token_normalizes_the_real_corpus_shapes(raw, token):
    assert lifecycle_token(raw) == token


@pytest.mark.parametrize("raw", YIELDS_NO_TOKEN)
def test_lifecycle_token_refuses_rather_than_guesses(raw):
    """A wrong lifecycle is worse than no lifecycle: `resolved-pending-live-confirmation`
    is NOT `resolved`, and `spec'd` is NOT `spec`."""
    assert lifecycle_token(raw) is None


def test_a_hyphenated_lifecycle_never_collapses_into_its_head():
    """The single most dangerous mis-normalization available, asserted directly."""
    assert lifecycle_token("resolved-pending-live-confirmation") != "resolved"
    assert not status_matches("resolved-pending-live-confirmation", "resolved")
    assert status_matches("resolved-pending-live-confirmation",
                          "resolved-pending-live-confirmation")


def test_is_bare_lifecycle_token_is_the_shared_predicate():
    """ONE definition of "bare", used by BOTH the filter (to decide a query is asking a
    lifecycle question) and by V11 (to decide a status is drift). Two definitions would
    eventually disagree, and the disagreement would be invisible."""
    assert is_bare_lifecycle_token("proposed")
    assert is_bare_lifecycle_token("in-progress")
    assert is_bare_lifecycle_token("  proposed  ")       # padding is not content
    assert not is_bare_lifecycle_token("proposed — awaiting ratify")
    assert not is_bare_lifecycle_token("**operational**")
    assert not is_bare_lifecycle_token("")
    assert not is_bare_lifecycle_token(None)


# ---------------------------------------------------------------------------
# THE MATCH PREDICATE
# ---------------------------------------------------------------------------
def test_exact_string_matching_is_preserved_unchanged():
    """Today's behaviour first and unconditionally — this fix only ADDS matches."""
    assert status_matches("proposed — awaiting ratify", "proposed — awaiting ratify")
    assert status_matches("**operational (2026-07-07)**", "**operational (2026-07-07)**")
    assert status_matches("OPEN — flagged", "OPEN — flagged")


def test_a_bare_query_asks_a_lifecycle_question():
    assert status_matches("proposed — awaiting ratify", "proposed")
    assert status_matches("operational (2026-07-07)", "operational")


def test_a_qualified_query_keeps_its_precision():
    """A caller who passes a full qualified string asked an EXACT question; widening it
    to the whole lifecycle would take away the precision the exact filter used to give.
    This clause is why `status_matches` tests `is_bare_lifecycle_token(query)`."""
    assert not status_matches("proposed", "proposed — awaiting ratify")
    assert not status_matches("proposed — flagged at Phase 2.", "proposed — awaiting ratify")


def test_an_absent_status_never_matches():
    """Same as the old `node.get("status") != status`."""
    assert not status_matches(None, "proposed")
    assert not status_matches("", "proposed")


def test_a_capitalized_query_matches_only_by_raw_string():
    """DOCUMENTED BOUNDARY, asserted rather than left undefined. The tokenizer is applied
    SYMMETRICALLY: `Proposed` is not a lifecycle token on either side, so it falls back to
    exact-string. Lifecycle tokens are lowercase — see `lifecycle.py`."""
    assert not status_matches("proposed — awaiting ratify", "Proposed")
    assert status_matches("Proposed", "Proposed")


# ---------------------------------------------------------------------------
# THE FILTER, END TO END — the shape of the real defect, with exact counts
# ---------------------------------------------------------------------------
class _AlwaysFresh:
    def evaluate(self, part):
        from factlattice_engine import FreshnessResult
        return FreshnessResult(part.corpus_id, True, part.files_hash, part.files_hash)


# A miniature of the real gate: four `[DD-*]` nodes, ONE bare-`proposed`, THREE qualified
# (verbatim live strings, including the one that says it BLOCKS a slice), plus two nodes
# on other lifecycles that must NOT be swept in.
_GATE_STATUSES = {
    "DD-BARE": "proposed",
    "DD-AWAITING": "proposed — awaiting ratify",
    "DD-HOST-SURFACE": ("proposed — awaiting ratify (BLOCKS slice 2's scene wiring; "
                        "does NOT block slices 0 or 1)"),
    "DD-PHASE-2": "proposed — flagged for the architect to ratify at Phase 2.",
    "DD-DONE": "operational (2026-07-07)",
    "DD-NOT-PROPOSED": "resolved",
    "DD-NO-STATUS": None,
}


@pytest.fixture
def gate_engine():
    nodes = {
        nid: {"id": nid, "title": nid, "domain": None, "status": st, "properties": {},
              "source_file": None, "line_range": [], "token_count": 1}
        for nid, st in _GATE_STATUSES.items()
    }
    index = {
        "source_corpus": {"corpus_id": "gate", "domain_suffix": "proj.gate",
                          "files_hash": "sha256:test", "root_paths": []},
        "nodes": nodes, "layers": {},
        "stats": {"total_nodes": len(nodes), "total_edges": 0},
    }
    store = LtsStore()
    store.add_partition(Partition(index))
    return LtsEngine(store, freshness_provider=_AlwaysFresh())


def test_the_dispatch_gate_sees_every_proposed_node(gate_engine):
    """THE DEFECT, in miniature. Under exact-string matching this returned 1 of 4 and
    looked like it worked. EXACT COUNT, and the ids by name."""
    r = gate_engine.find(corpus="gate", status="proposed")
    assert r["total_matched"] == 4
    assert sorted(n["id"] for n in r["nodes"]) == [
        "DD-AWAITING", "DD-BARE", "DD-HOST-SURFACE", "DD-PHASE-2"]


def test_the_gate_does_not_over_match(gate_engine):
    """The negative control. Widening must not sweep in other lifecycles or the
    status-less node — a filter that returns everything is as broken as one that
    returns too little."""
    assert gate_engine.find(corpus="gate", status="operational")["total_matched"] == 1
    assert gate_engine.find(corpus="gate", status="resolved")["total_matched"] == 1
    assert gate_engine.find(corpus="gate", status="withdrawn")["total_matched"] == 0


def test_the_raw_status_string_is_returned_untouched(gate_engine):
    """⛔ THE FILTER CHANGED, THE DATA DID NOT. The qualifier prose is real content —
    "BLOCKS slice 2's scene wiring" is the whole message — and a reader depends on
    getting it back verbatim, not normalized to a token."""
    r = gate_engine.find(corpus="gate", status="proposed")
    got = {n["id"]: n["status"] for n in r["nodes"]}
    assert got == {k: v for k, v in _GATE_STATUSES.items()
                   if k in {"DD-BARE", "DD-AWAITING", "DD-HOST-SURFACE", "DD-PHASE-2"}}
    assert "BLOCKS slice 2's scene wiring" in got["DD-HOST-SURFACE"]


def test_a_qualified_query_still_selects_exactly_one(gate_engine):
    r = gate_engine.find(corpus="gate", status="proposed — awaiting ratify")
    assert r["total_matched"] == 1
    assert r["nodes"][0]["id"] == "DD-AWAITING"


def test_status_composes_with_the_other_filters(gate_engine):
    """The lifecycle widening lives INSIDE `_node_matches`, so every other predicate
    still ANDs with it exactly as before."""
    assert gate_engine.find(corpus="gate", status="proposed",
                            prefix="DD-HOST")["total_matched"] == 1
    assert gate_engine.find(corpus="gate", status="proposed",
                            prefix="ZZZ-")["total_matched"] == 0
