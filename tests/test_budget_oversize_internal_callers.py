"""Internal callers of find that read results in code must not lose an oversize node silently (ruling condition 5).
Each test is proven RED against the caller without its fix."""
from __future__ import annotations

from factlattice_engine import LtsEngine, LtsStore, Partition
from factlattice_engine.reviewer import SITE_CONFIG_NODE, lookup_site_config, self_gate
from tests.test_response_tiers import _AlwaysFresh, _mk_index, _node


def test_site_config_lookup_never_reads_an_oversize_config_as_none():
    nodes = {SITE_CONFIG_NODE: _node(SITE_CONFIG_NODE, tokens=200_000, props={"default-output-root": "out"})}
    store = LtsStore(); store.add_partition(Partition(_mk_index("site", "proj.site", nodes, [])))
    eng = LtsEngine(store, freshness_provider=_AlwaysFresh())
    cfg = lookup_site_config(lambda **kw: eng.find(**kw))
    assert cfg.status != "none", "an oversize site config was skipped and read as 'no config'"


def test_self_gate_fails_loud_when_a_path_find_skips_an_oversize_review_node(tmp_path):
    review = tmp_path / "r.lts"; review.write_text("# r\n")
    def call(tool, args):
        if tool == "lts_find":
            r = {"success": True, "file_source": {"sha256": "x"}, "nodes": [],
                 "extent": {"scope": {"corpus": "(one file, by path)"}}}
            if args.get("prefix") == "REVIEW-":
                r["oversize_skipped"] = [{"id": "REVIEW-BIG", "token_count": 200_000}]
                r["truncated"] = True
            return r
        return {"success": True}
    out = self_gate(str(review), call)
    proof = [c for c in out["checks"] if c["name"] == "path-find-proof"]
    assert proof and proof[0]["ok"] is False, "a REVIEW node skipped as oversize must fail the gate, not drop its checks"


def test_analytics_fails_loud_if_find_ever_reports_a_skip():
    """Reaches analytics' fail-loud branch: even with its unbounded budget, a reported skip must refuse the fold."""
    from factlattice_engine.analytics import LtsAggregator
    class FakeTools:
        def find(self, **kw):
            return {"success": True, "nodes": [{"id": "A", "properties": {}}], "truncated": True, "cursor": None,
                    "oversize_skipped": [{"id": "BIG", "token_count": 10}]}
    r = LtsAggregator(FakeTools()).count(corpus="c")
    assert r.get("success") is False and r.get("reason") == "analytics-incomplete-oversize", r


# --- N2 (delta Diamond): each half of each caller fix pinned on its own ---------------------------------

def test_site_config_oversize_reads_ok_through_the_real_engine():
    """The unbounded-budget half: an oversize config is READ (ok), not merely refused."""
    nodes = {SITE_CONFIG_NODE: _node(SITE_CONFIG_NODE, tokens=200_000, props={"default-output-root": "out"})}
    store = LtsStore(); store.add_partition(Partition(_mk_index("site", "proj.site", nodes, [])))
    eng = LtsEngine(store, freshness_provider=_AlwaysFresh())
    assert lookup_site_config(lambda **kw: eng.find(**kw)).status == "ok"


def test_site_config_reported_skip_fails_closed():
    """The fail-closed half: if find still reports a skip, the lookup is unreadable, never 'none'."""
    fake = lambda **kw: {"success": True, "nodes": [], "truncated": True, "cursor": None,
                         "oversize_skipped": [{"id": SITE_CONFIG_NODE, "token_count": 10}]}
    assert lookup_site_config(fake).status == "unreadable"


def test_self_gate_treats_a_truncated_path_find_as_incomplete(tmp_path):
    """The truncation half: a path find that reports more pages is not proof of a complete read."""
    review = tmp_path / "r.lts"; review.write_text("# r\n")
    def call(tool, args):
        if tool == "lts_find":
            return {"success": True, "file_source": {"sha256": "x"}, "nodes": [], "truncated": True, "cursor": 1,
                    "extent": {"scope": {"corpus": "(one file, by path)"}}}
        return {"success": True}
    proof = [c for c in self_gate(str(review), call)["checks"] if c["name"] == "path-find-proof"]
    assert proof and proof[0]["ok"] is False


def test_self_gate_reads_a_large_review_node_whole_through_dispatch(tmp_path):
    """The unbounded-budget half, through the real engine door: a review node over the default budget is read
    whole, so the proof holds and severities-match is evaluated (not dropped, not failed for size)."""
    from factlattice_engine import dispatch
    big = "x " * 140_000                                    # ~70k tokens of property text
    review = tmp_path / "review.lts"
    review.write_text("# Review [REVIEW-BIG]\n\n- findings-by-severity: error 0, warning 0, info 0\n"
                      f"- notes: {big}\n")
    eng = LtsEngine(LtsStore(), freshness_provider=_AlwaysFresh())
    out = self_gate(str(review), lambda t, a: dispatch(t, a, tools=eng))
    names = {c["name"]: c["ok"] for c in out["checks"]}
    assert names.get("path-find-proof") is True, out["checks"]
    assert "severities-match" in names, out["checks"]


def test_self_gate_fails_on_a_reported_skip_even_without_truncated(tmp_path):
    """R3-1: the oversize_skipped half of the path-find proof, on its own (a skip reported, truncated absent)."""
    review = tmp_path / "r.lts"; review.write_text("# r\n")
    def call(tool, args):
        if tool == "lts_find":
            return {"success": True, "file_source": {"sha256": "x"}, "nodes": [], "truncated": False,
                    "oversize_skipped": [{"id": "REVIEW-BIG", "token_count": 10}],
                    "extent": {"scope": {"corpus": "(one file, by path)"}}}
        return {"success": True}
    proof = [c for c in self_gate(str(review), call)["checks"] if c["name"] == "path-find-proof"]
    assert proof and proof[0]["ok"] is False
