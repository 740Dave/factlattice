"""RED for [ENGINE-BUDGET] "an over-budget item is skipped whole and reported" (defect found 2026-10-27 by
a cold findability probe: a two-word lts_find in a team-operations corpus returned a 111,576-token ledger node
whole on page 1, overflowing the client, and pushed the one useful match to page 2).
Today budget_slice ALWAYS emits the first item ("so a single over-budget item still makes progress"), contradicting the
spec's own rule. Expected: the oversize node is skipped whole and REPORTED; the page fills with what fits; the reach stays
declared (count < total_matched -> truncated true)."""
from __future__ import annotations

from factlattice_engine import LtsEngine, LtsStore, Partition
from tests.test_response_tiers import _AlwaysFresh, _mk_index, _node


def _engine():
    nodes = {"AAA-GIANT-LEDGER": _node("AAA-GIANT-LEDGER", title="ledger of slides runs", tokens=120_000),
             "PROC-MAKE-SLIDES": _node("PROC-MAKE-SLIDES", title="make slides from the template", tokens=2_000)}
    store = LtsStore(); store.add_partition(Partition(_mk_index("ops", "proj.ops", nodes, [])))
    return LtsEngine(store, freshness_provider=_AlwaysFresh())

def test_oversize_node_is_skipped_whole_and_reported_and_the_page_fills():
    r = _engine().find(corpus="ops", text="slides", token_budget=8_000)
    ids = [n["id"] for n in r["nodes"]]
    assert "AAA-GIANT-LEDGER" not in ids                 # skipped whole: never carried
    assert "PROC-MAKE-SLIDES" in ids                     # the page fills with what fits
    rep = r.get("oversize_skipped") or []
    assert [o["id"] for o in rep] == ["AAA-GIANT-LEDGER"] and rep[0]["token_count"] == 120_000   # and reported
    assert r["total_matched"] == 2 and r["count"] == 1 and r["truncated"] is True              # reach declared

def test_a_budget_that_fits_carries_the_node_as_before():
    r = _engine().find(corpus="ops", text="slides", token_budget=200_000)
    assert sorted(n["id"] for n in r["nodes"]) == ["AAA-GIANT-LEDGER", "PROC-MAKE-SLIDES"]
    assert not r.get("oversize_skipped")

def test_analytics_still_counts_an_oversize_node():
    """Axis-1 guard: [LAYER-ANALYTICS] folds over find's pages; skipping an oversize match must never shorten a count."""
    from factlattice_engine.analytics import LtsAggregator
    r = LtsAggregator(_engine()).count(corpus="ops", text="slides")
    assert r.get("success") is True, r
    assert r.get("total") == 2, r


def test_page_tier_complete_stays_cursor_derived_beside_a_skip():
    """R3-2 / SPEC 4.6.2: `complete` is derived from the cursor alone; a skip is declared by oversize_skipped and
    truncated, never by complete=false (this line flipped once on the branch; pin it)."""
    r = _engine().find(corpus="ops", text="slides", token_budget=8_000, tier="page")
    assert r.get("oversize_skipped") and r["truncated"] is True
    assert r["cursor"] is None and r["complete"] is True
