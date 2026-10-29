"""[INV-DECLARED-REACH] — no surface may under-report without saying so.

    A bound is legitimate. A SILENT bound is not. Any response carrying fewer items
    than the engine found declares the difference in ONE shared vocabulary —
    `count` / `total_matched` / `truncated` / `cursor` — and any summary travelling
    beside it counts the CORPUS, not the response.

THE DEFECT THIS PINS. `lts_lint` was the only collection-returning surface outside the
token-budget contract: it returned every finding, unbounded, and its payload carried
none of the four fields. Measured at 667 findings / ~203k tokens on one project's
acme-specs — a whole context window in a single tool call, from the one layer that
could not describe its own extent. The cause was a SCOPE CLAUSE, not a coding slip:
[ENGINE-BUDGET] read "every layer that returns NODES", lint's collection is called
`findings`, and the slicer lived as a private method on a class lint never constructs.
Code and spec agreed with each other and both were wrong.

WHY THESE TESTS USE LIVE CORPORA. The controls that get fitted to a shape their author
already handled are why this took a day to find. `domain-profiles/` is a real corpus in
this repository that really produces 108 findings across several files, several origins
and a wide spread of `detail` payload sizes. A hand-built fixture of N identical
findings would pass against a slicer that only works on identical findings.

AND WHY THE TABLE IS PARAMETRIZED OVER EVERY TOOL. The failure was one layer sitting
outside a contract the others honoured, so a lint-only test would re-create exactly the
blind spot it is meant to close: the NEXT layer added would fall out the same way. The
sweep asserts the vocabulary over every collection-returning tool at once, so any
current-or-future surface that omits it fails here by construction.
"""
from __future__ import annotations

import os

import pytest

from factlattice_engine.budget import budget_slice, declare, resolve_budget
from factlattice_engine.lint import lint
from factlattice_engine.tools import DEFAULT_TOKEN_BUDGET, dispatch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# A LIVE corpus in this repo, not a fixture: several files, several finding origins,
# and `detail` payloads that vary by an order of magnitude between findings.
LIVE_CORPUS = os.path.join(REPO, "domain-profiles")
# A live corpus that is CLEAN. The negative control: the contract must be present and
# coherent on a zero-finding answer too, because "no fields" and "nothing to report"
# must never look alike.
CLEAN_CORPUS = os.path.join(REPO, "specs")

DECLARATION_FIELDS = ("count", "total_matched", "truncated", "cursor")


def _live_total() -> int:
    return len(lint(LIVE_CORPUS).findings)


# ---------------------------------------------------------------------------
# THE ENFORCING TEST — fails on the old behaviour
# ---------------------------------------------------------------------------
def test_lint_declares_its_reach_when_it_truncates():
    """A document set producing more findings than the budget carries MUST report the
    TRUE total and flag the truncation. Before the fix the payload had no `truncated`,
    no `total_matched` and no `cursor` at all, so this fails at the first assert."""
    total = _live_total()
    assert total > 20, "control corpus stopped producing enough findings to bound"

    # A budget deliberately far under the corpus, so the bound certainly bites.
    res = dispatch("lts_lint", {"source": LIVE_CORPUS, "token_budget": 500})

    for f in DECLARATION_FIELDS:
        assert f in res, f"lts_lint under-reports without declaring: missing {f!r}"
    assert res["truncated"] is True
    assert res["count"] == len(res["findings"]) < total
    assert res["total_matched"] == total          # the TRUE total, not the page
    assert res["cursor"] == res["count"]


def test_lint_summary_counts_the_corpus_not_the_response():
    """THE summary question. A summary folded over a truncated list is not a summary of
    the document, it is a summary of the response — and it reports a state the system is
    not in while looking exactly like a measurement. `summary.total` is the corpus."""
    total = _live_total()
    res = dispatch("lts_lint", {"source": LIVE_CORPUS, "token_budget": 500})

    assert res["truncated"] is True
    assert res["summary"]["total"] == total == res["total_matched"]
    assert res["summary"]["total"] > res["count"]
    # ...and the per-kind histogram is folded over the corpus too, not just `total`:
    # a page-scoped histogram would sum to `count`.
    kinds = {k: v for k, v in res["summary"].items()
             if k not in ("errors", "warnings", "total", "errors_observed",
                          "warnings_observed", "suppressed_because")}
    assert sum(kinds.values()) == total
    # ⛔ THE VERDICT PAIR IS NULLED WHEN A RULE WAS INERT — [FL-RECALL-SCOPE-VERDICT] /
    # proposal item 3. `domain-profiles/` resolves NO profile, so V1-V11 never ran, and a
    # bare `errors`/`warnings` out of that run answers a different question than the one
    # its name implies. The raw numbers survive as `*_observed`, BEHIND `rules_inert`, so
    # the caveat is upstream of the number instead of beside it — the whole finding being
    # that "a 12 sitting beside a prose caveat travels as a 12".
    if res["rules_inert"]:
        assert res["summary"]["errors"] is None and res["summary"]["warnings"] is None
        assert res["summary"]["suppressed_because"]
        e, w = res["summary"]["errors_observed"], res["summary"]["warnings_observed"]
    else:
        e, w = res["summary"]["errors"], res["summary"]["warnings"]
    assert e + w == total


def test_lint_cursor_drains_the_whole_corpus_exactly_once():
    """The bound is PAGINATION, not a filter ([ENGINE-BUDGET]): the cursor is always
    sufficient to retrieve the remainder. Every finding appears exactly once across the
    pages, and every page reports the same corpus-wide total."""
    total = _live_total()
    seen, offset, pages, totals = [], 0, 0, set()
    while True:
        page = dispatch("lts_lint", {"source": LIVE_CORPUS,
                                     "token_budget": 500, "offset": offset})
        assert page["count"] > 0, "cursor stopped making progress"
        seen.extend((f["source_file"], f["source_line"], f["origin"], f["kind"],
                     f["message"]) for f in page["findings"])
        totals.add(page["total_matched"])
        pages += 1
        assert pages < 500, "cursor is looping"
        if not page["truncated"]:
            assert page["cursor"] is None
            break
        offset = page["cursor"]

    assert pages > 1                              # the bound really bit
    assert len(seen) == total                     # nothing dropped, nothing doubled
    assert totals == {total}                      # every page names the same corpus


def test_lint_declares_reach_even_when_whole():
    """An absent field is indistinguishable from a whole answer. The four fields are
    present on an untruncated response too, or a caller has to know which mode it got."""
    res = dispatch("lts_lint", {"source": LIVE_CORPUS, "token_budget": 10_000_000})
    for f in DECLARATION_FIELDS:
        assert f in res
    assert res["truncated"] is False and res["cursor"] is None
    assert res["count"] == res["total_matched"] == res["summary"]["total"]


# ---------------------------------------------------------------------------
# THE CLEAN-CORPUS CLAIM, DECLARED — the same discipline, applied to this test
# ---------------------------------------------------------------------------
# ⛔ THIS TEST USED TO ASSERT `findings == []`, AND THAT ASSERTION IS INVALIDATED BY ANY
# PARALLEL BRANCH THAT ADDS A RULE. It passed alone and failed integrated the day the
# KEY-LOSS rule landed and found 9 real findings in `specs/` — a corpus that had never
# been clean with respect to a rule nobody had yet written. The failure surfaces at
# integration, where it looks exactly like a landing bug.
#
# ⇒ ASSERT BY ORIGIN. Declare the RULE-SET the corpus is clean with respect to, and the
#   exceptions with their reason. A new rule then appears as a NEW UNDECLARED ORIGIN — a
#   one-line, obviously-correct amendment naming what the new rule found — instead of a
#   red suite pointing at the wrong commit. This is [INV-DECLARED-EXTENT] turned on the
#   engine's own tests: a `clean` with no denominator is indistinguishable from a check
#   that inspected nothing, and `findings == []` was exactly that claim.
CLEAN_ORIGINS = frozenset({
    "structure", "dangle", "coverage", "corpus_files", "profile", "id_hijack",
    # Added when the claim-shaped-identifier rule landed. ⭐ THE MECHANISM WORKING AS
    # DESIGNED: the new rule surfaced here as an UNDECLARED ORIGIN naming exactly what to
    # read, rather than as a bare `findings != []` pointing at the wrong commit. `specs/`
    # is genuinely clean of it (0 of 60 nodes argue in their id), so it is a clean origin
    # and not an exception.
    "claim_id",
})
# origin -> why this corpus is NOT clean with respect to it, and what the findings ARE.
DECLARED_EXCEPTIONS = {
    "key_loss": (
        "9 findings, ALL `key_loss_choice`/warning, measured 2026-08-13. They are the "
        "`- **Label.** prose` enumeration idiom in three narrative lists (`FactLattice "
        "is three things…`, `Properties fall into three functional classes…`, `Three "
        "guarantees make an aggregate trustworthy…`). They are REAL by the rule's own "
        "definition — authored bullets in node bodies that minted no property — and the "
        "kind is literally named `_choice`: 'needs an authoring decision (a rename), not "
        "a fix'. ⛔ NOT SILENCED BY EDITING THE SPEC. Rewriting FactLattice's own "
        "normative prose to quiet a warning would be the tool wagging the document, and "
        "`key_loss` ships at `warning` precisely so a corpus may carry a reasoned "
        "exception. The exception is declared here, dated, with a count that must not "
        "GROW."),
}
# Era 0 (PARSER-ENGINEERING-SPEC.md rev 2, C1/C2) landed 2026-08-16: two new origins,
# `malformed_ref` and `unminted`. Measured against `specs/` on landing: BOTH ZERO -- this
# repo's own spec corpus authors no id-shaped-but-ill-formed bracket ref and no pure
# reference list under a non-minting key, so both are genuinely clean origins here (the
# predicted ~164/~186 populations are measured against the ACME deployment corpora,
# a different corpus entirely -- see the Era 0 build report). Added to CLEAN_ORIGINS, not
# DECLARED_EXCEPTIONS, because there is nothing here to except.
CLEAN_ORIGINS = CLEAN_ORIGINS | frozenset({"malformed_ref", "unminted"})

# canonical-edge (2026-10-03): `canonical` lint origin. `specs/` authors no `canonical:` key
# (MODEL-CANONICAL describes it in prose), so it is a genuinely clean origin.
CLEAN_ORIGINS = CLEAN_ORIGINS | frozenset({"canonical"})

# review-extension edges (2026-10-07, A-E3): `review-edges` lint origin. `specs/` authors neither
# `attaches-to-hook` nor `applies-rule`, so it is a genuinely clean origin.
CLEAN_ORIGINS = CLEAN_ORIGINS | frozenset({"review-edges"})

# Era 1 (PARSER-ENGINEERING-SPEC.md rev 2, A1/A2/A3) landed 2026-08-16: one new origin,
# `vocabulary`. ⛔ IT IS A DECLARED EXCEPTION AND NOT A CLEAN ORIGIN, and the reason is
# the whole point of the era: `legacy_document` fires on EVERY document with no `lts:`
# marker, and at Era-1 landing that is EVERY DOCUMENT IN EXISTENCE — stamping is Era 2,
# deliberately not done here. A corpus cannot be "clean" of a finding whose entire job is
# to count the un-migrated tail; it can only burn the number down. Measured on landing.
DECLARED_EXCEPTIONS = {
    **DECLARED_EXCEPTIONS,
    "vocabulary": (
        "53 findings on `specs/`, measured 2026-08-16 at Era-1 landing, ALL warning: 6 "
        "`legacy_document` (one per file — no `lts: \"1.1\"` marker, which is correct, "
        "because Era 1 ships the CAPABILITY and Era 2 does the stamping) and 47 "
        "`undeclared_key` (this repo's own spec corpus uses descriptive keys like "
        "`importance` that no profile declares — exactly the A2 population, and exactly "
        "the worklist A2 exists to make visible). ⛔ NOT SILENCED by stamping the specs "
        "or by declaring the keys: doing either here would be Era 2 work smuggled into "
        "Era 1, and the count is the honest starting measurement. It must FALL, and it "
        "must not grow."),
}


def test_lint_declares_reach_on_a_clean_corpus():
    """Zero ERROR findings, every finding from a DECLARED origin, and the four fields
    present. "No fields" and "nothing to report" must not look alike."""
    res = dispatch("lts_lint", {"source": CLEAN_CORPUS})
    for f in DECLARATION_FIELDS:
        assert f in res
    assert res["truncated"] is False and res["cursor"] is None
    assert res["count"] == res["total_matched"] == res["summary"]["total"]

    origins = {f["origin"] for f in res["findings"]}
    unexpected = origins & CLEAN_ORIGINS
    assert not unexpected, (
        f"{CLEAN_CORPUS} is DECLARED clean with respect to {sorted(CLEAN_ORIGINS)} and "
        f"produced findings from {sorted(unexpected)}. Either the corpus regressed or "
        f"the declaration is stale — fix one, do not widen the other silently.")
    undeclared = origins - CLEAN_ORIGINS - set(DECLARED_EXCEPTIONS)
    assert not undeclared, (
        f"NEW RULE-SET, UNDECLARED VERDICT: origins {sorted(undeclared)} did not exist "
        f"when this corpus was declared clean. This is not a landing bug — it is a rule "
        f"that has never been run against this corpus. Read its findings, then add the "
        f"origin to CLEAN_ORIGINS or to DECLARED_EXCEPTIONS with the reason.")
    assert not [f for f in res["findings"] if f["severity"] == "error"]


def test_the_declared_clean_exception_is_bounded_and_cannot_quietly_grow():
    """An exception with no number is an exemption. `key_loss` on `specs/` is 9 findings,
    all `_choice`, and it may FALL — it may not grow without a reader deciding to."""
    res = dispatch("lts_lint", {"source": CLEAN_CORPUS})
    key_loss = [f for f in res["findings"] if f["origin"] == "key_loss"]
    assert len(key_loss) <= 9, (
        "the declared key-loss exception on the spec corpus GREW. Read the new findings: "
        "a new one is either a real field the specs should be keying, or a rule "
        "regression. It is not a number to re-baseline.")
    assert {f["kind"] for f in key_loss} == {"key_loss_choice"}, (
        "a key-loss finding on the spec corpus escalated out of the `_choice` band. "
        "`_high` means 'mechanical, coerces to a legal key, no authoring decision "
        "required' — if one of these is now HIGH it is a one-line fix, not an exception.")


def test_every_lint_origin_is_named_in_the_rule_set_registry():
    """`lint.ORIGINS` is the denominator the clean-corpus claim is measured against. A
    rule whose origin is missing from it is a rule that can produce a finding no
    declaration can account for — which is how `findings == []` broke."""
    from factlattice_engine.lint.core import ORIGINS
    assert CLEAN_ORIGINS | set(DECLARED_EXCEPTIONS) == set(ORIGINS), (
        "this test's declared origin partition has drifted from the linter's own "
        "rule-set registry")
    res = dispatch("lts_lint", {"source": LIVE_CORPUS, "dangle": True,
                                "token_budget": 10_000_000})
    assert {f["origin"] for f in res["findings"]} <= set(ORIGINS)


def test_the_mcp_lint_surface_is_bounded_by_default():
    """NOT opt-in. The caller who forgets to pass a budget is exactly the one with a
    context window to destroy, so the default must bound.

    Asserted against a MEASURED cost rather than a corpus that happens to be big: the
    repo's own control corpus is comfortably under the default today, and a test that
    only fires on an oversized corpus would go quietly green the day the corpus shrank
    — which is the same class of silence this whole file exists to close. Instead: pass
    a budget the corpus DOES exceed and confirm the default path applies a finite bound
    at all, then pin the default's actual value as the one applied.
    """
    total = _live_total()
    cost = sum(f.token_count() for f in lint(LIVE_CORPUS).findings)
    assert cost > 0

    # No `token_budget` argument at all — the default path.
    default_run = dispatch("lts_lint", {"source": LIVE_CORPUS})
    # ...must be identical to explicitly asking for DEFAULT_TOKEN_BUDGET. If dispatch
    # passed None/unbounded (the old behaviour) these diverge the moment the corpus
    # outgrows the default, and the assert below proves the equivalence NOW.
    explicit = dispatch("lts_lint",
                        {"source": LIVE_CORPUS, "token_budget": DEFAULT_TOKEN_BUDGET})
    assert default_run["count"] == explicit["count"]
    assert default_run["truncated"] == explicit["truncated"]

    # And the bound genuinely bounds: below the corpus cost, it bites and declares.
    bounded = dispatch("lts_lint", {"source": LIVE_CORPUS, "token_budget": cost // 2})
    assert bounded["truncated"] is True
    assert bounded["count"] < total == bounded["total_matched"]


def test_the_library_lint_stays_whole_for_its_terminal_consumer():
    """The CLI's consumer is a terminal, not a context window. `to_dict()` with no
    budget carries everything — and still declares, via the same code path."""
    res = lint(LIVE_CORPUS).to_dict()
    total = _live_total()
    assert len(res["findings"]) == res["count"] == res["total_matched"] == total
    assert res["truncated"] is False and res["cursor"] is None


# ---------------------------------------------------------------------------
# THE CROSS-SURFACE SWEEP — the de-fork, enforced
# ---------------------------------------------------------------------------
# One layer sitting outside a contract the others honour IS the defect class. Naming
# the tools in a table means the next surface added is either in this list or visibly
# missing from it.
COLLECTION_TOOLS = [
    ("lts_find", {}, "nodes"),
    ("lts_traverse", {"start_node": None}, "nodes"),
    ("lts_capabilities", {}, "capabilities"),
    ("lts_lint", {"source": LIVE_CORPUS}, "findings"),
    # [LTS-REVERSE-LOOKUP] — the newest collection-returning surface, in the sweep the
    # day it landed rather than found missing from it later.
    ("lts_reverse", {"node_id": None}, "referrers"),
]


@pytest.mark.parametrize("tool,extra,collection",
                         COLLECTION_TOOLS, ids=[t[0] for t in COLLECTION_TOOLS])
def test_every_collection_tool_advertises_the_budget_contract(tool, extra, collection):
    """Schema-level: every collection-returning tool takes `token_budget`/`offset`, and
    shares the ONE `_BUDGET` schema object. A tool with its own budget schema text is a
    tool whose contract has already begun to fork."""
    from factlattice_engine import server
    spec = next(t for t in server.TOOLS if t["name"] == tool)
    props = spec["inputSchema"]["properties"]
    assert "token_budget" in props, f"{tool} returns {collection} but takes no budget"
    assert "offset" in props, f"{tool} can truncate but offers no way to resume"
    assert props["token_budget"] is server._BUDGET, f"{tool} forked the budget schema"


def test_a_declared_cursor_always_has_a_resume_path():
    """The sweep above CAUGHT this one: `lts_capabilities` returned a `cursor` and
    advertised (in the shared `_BUDGET` text) resuming "via `offset`", while accepting
    no `offset` argument at all — a truncation declared with no way to act on it, which
    is under-reporting wearing the costume of honesty. Behavioural, not schema-level:
    the argument must actually MOVE the window."""
    from factlattice_engine.tools import LtsEngine
    import inspect
    for method in (LtsEngine.find, LtsEngine.traverse,
                   LtsEngine.capabilities, LtsEngine.flow, LtsEngine.reverse):
        params = inspect.signature(method).parameters
        assert "token_budget" in params
        assert "offset" in params, (
            f"{method.__name__} can truncate but cannot resume — its cursor is decorative")


def test_lint_is_in_the_budgeted_schema_set():
    """The existing `test_advertised_schema_default_is_derived_not_restated` asserts the
    budgeted tools share one schema object but never asserted WHICH tools are in the
    set — so lint's absence from it was invisible to the suite. This names it."""
    from factlattice_engine import server
    budgeted = {t["name"] for t in server.TOOLS
                if "token_budget" in t["inputSchema"].get("properties", {})}
    assert "lts_lint" in budgeted


def test_there_is_exactly_one_budget_slicer():
    """The de-fork, pinned. `LtsEngine._budget_slice` must DELEGATE, not re-implement:
    a second copy is how the QUERY layer and the LINT layer diverge next time. Asserted
    behaviourally — a sentinel the shared function would honour and a private copy would
    not — rather than by reading the source."""
    from factlattice_engine.tools import LtsEngine

    calls = []
    real = budget_slice

    import factlattice_engine.tools as tools_mod
    items = [{"token_count": 10} for _ in range(5)]

    def spy(*a, **kw):
        calls.append(a)
        return real(*a, **kw)

    tools_mod.budget_slice = spy
    try:
        eng = LtsEngine.__new__(LtsEngine)
        out = eng._budget_slice(items, 0, 25, keyfn=lambda x: x)
    finally:
        tools_mod.budget_slice = real
    assert calls, "LtsEngine._budget_slice re-implements the slice instead of delegating"
    assert out[0] == items[:2] and out[1] is True and out[2] == 2 and out[3] == 5


# ---------------------------------------------------------------------------
# the shared primitives, directly
# ---------------------------------------------------------------------------
def test_declare_emits_all_four_fields_or_none():
    """`declare()` exists so no layer can emit three of four and call it declared."""
    d = declare([1, 2], True, 2, 7)
    assert set(d) == set(DECLARATION_FIELDS)
    assert d == {"count": 2, "total_matched": 7, "truncated": True, "cursor": 2}
    whole = declare([1, 2, 3], False, None, 3)
    assert whole["cursor"] is None and whole["truncated"] is False


def test_an_over_budget_item_still_makes_progress():
    """Items are never split, and a single item larger than the whole budget is still
    emitted — otherwise the cursor stalls and pagination deadlocks."""
    items = [{"token_count": 10_000}, {"token_count": 1}]
    emitted, truncated, cursor, total = budget_slice(
        items, 0, 5, keyfn=lambda x: x)
    assert len(emitted) == 1 and truncated is True and cursor == 1 and total == 2


def test_an_explicit_budget_wins_in_both_directions():
    """[DEF-BUDGET-TOO-SMALL]'s ruling, now shared rather than restated — so the LINT
    layer inherited it instead of re-deciding it differently."""
    assert resolve_budget(None, 32768) == 32768
    assert resolve_budget(100, 32768) == 100
    assert resolve_budget(200_000, 32768) == 200_000
    assert resolve_budget("garbage", 32768) == 32768


def test_a_finding_and_a_node_are_priced_by_the_same_ruler():
    """The estimator is imported, not twinned. Two estimators that agree today are two
    estimators, and the budget they share would drift the day one changed."""
    import factlattice
    from factlattice_engine import budget as budget_mod
    assert budget_mod.token_estimate is factlattice.token_estimate
    assert factlattice.token_estimate is factlattice.parser.token_estimate
