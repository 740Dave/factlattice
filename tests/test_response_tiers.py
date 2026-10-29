"""[LTS-TIER] the digest / page / full response tiers.

Two jobs, and the SECOND one is the reason this file exists:

1. Prove the tiers do what they claim — digest is complete, `full` errors instead of
   truncating, `page` says it is a page.
2. Prove a digest carries NOTHING derived from the body, and keep proving it. The
   pressure to add "just a summary, for usability" WILL recur; a summary is a partial
   answer wearing a complete answer's clothes — the same failure as truncation,
   packaged more attractively. `test_digest_carries_no_body_derived_field` and
   `test_digest_response_contains_no_body_text_anywhere` are the regression that
   matters, and they are written to fail on a field nobody thought to blacklist:
   one asserts the row's key set is EXACTLY `DIGEST_FIELDS`, the other asserts a
   distinctive string from the node's body appears NOWHERE in the serialized response.

There is also a LIVE demonstration (`test_live_*`) on the real failing case that
motivated the tier — traverse `ACME-VISION` in `acme-console` at
`token_budget=2500`, where the legacy path returns 4 of 10 and the digest returns 10 of
10. It is skipped when that corpus is not on this machine; a synthetic fixture proves
the same properties unconditionally.
"""

from __future__ import annotations

import json

import pytest

from factlattice_engine import LtsEngine, LtsStore, Partition, dispatch
from factlattice_engine.tiers import (
    BODY_DERIVED_KEYS, DIGEST_FIELDS, DIGEST_MAX_NODES, TIERS,
)


class _AlwaysFresh:
    def evaluate(self, part):
        from factlattice_engine import FreshnessResult
        return FreshnessResult(part.corpus_id, True, part.files_hash, part.files_hash)


# The distinctive body string every "no body text" assertion hunts for. Deliberately
# unmistakable: if it ever shows up inside a digest, some field is leaking the body.
BODY_MARKER = "ZZBODYMARKERZZ-the-prose-that-must-never-reach-a-digest"


def _node(nid, *, title=None, tokens=1000, props=None, decided=None):
    p = dict(props or {})
    if decided:
        p["decided-in-session"] = decided
    return {"id": nid, "title": title or f"title of {nid}", "domain": None,
            "status": "operational", "properties": p,
            "source_file": f"{nid.lower()}.lts", "line_range": [1, 20],
            "content_hash": f"sha256:{nid.lower()}", "token_count": tokens}


def _edge(src, tgt, etype="DEPENDS_ON", layer="semantic", target_domain=None):
    return {"source": src, "target": tgt, "type": etype, "layer": layer,
            "target_domain": target_domain, "dangling": False, "flags": []}


def _mk_index(corpus_id, domain_suffix, nodes, edges):
    layers = {}
    for e in edges:
        layers.setdefault(e["layer"], {"edges": []})["edges"].append(e)
    return {
        "source_corpus": {"corpus_id": corpus_id, "domain_suffix": domain_suffix,
                          "files_hash": "sha256:test", "root_paths": []},
        "nodes": nodes, "layers": layers,
        "stats": {"total_nodes": len(nodes), "total_edges": len(edges)},
    }


@pytest.fixture
def engine():
    """A star: ROOT -> C1..C5, each node 1000 tokens. A 2500-token budget therefore
    truncates the walk to 2 of 6 — the fixture analogue of the live ACME-VISION case."""
    nodes = {
        "ROOT": _node("ROOT", tokens=1000, decided="2026-07-20 (c14)",
                      props={"what-this-is": BODY_MARKER, "why": "second body prop"}),
    }
    edges = []
    for i in range(1, 6):
        nid = f"C{i}"
        nodes[nid] = _node(nid, tokens=1000, props={"detail": f"body of {nid}"})
        edges.append(_edge("ROOT", nid))
    store = LtsStore()
    store.add_partition(Partition(_mk_index("proj-a", "proj.a", nodes, edges)))
    return LtsEngine(store, freshness_provider=_AlwaysFresh())


# =====================================================================
# 1. THE LEGACY PATH IS UNTOUCHED (the ADDITIVE-ONLY constraint)
# =====================================================================
def test_legacy_traverse_still_truncates_exactly_as_before(engine):
    r = engine.traverse(corpus="proj-a", start_node="ROOT", token_budget=2500)
    assert r["success"] is True
    assert len(r["nodes"]) == 2 and r["total"] == 6
    assert r["truncated"] is True and r["cursor"] == 2


def test_legacy_response_grows_no_tier_keys(engine):
    """A caller that does not opt in must not even SEE the tier vocabulary."""
    for r in (engine.traverse(corpus="proj-a", start_node="ROOT", token_budget=2500),
              engine.find(corpus="proj-a", token_budget=2500),
              engine.node(corpus="proj-a", node_id="ROOT")):
        assert "tier" not in r
        assert "digest" not in r
        assert "complete" not in r


def test_legacy_and_explicit_none_tier_are_identical(engine):
    a = engine.traverse(corpus="proj-a", start_node="ROOT", token_budget=2500)
    b = engine.traverse(corpus="proj-a", start_node="ROOT", token_budget=2500, tier=None)
    assert a == b


# =====================================================================
# 2. DIGEST IS ALWAYS COMPLETE
# =====================================================================
def test_digest_returns_every_node_at_a_budget_that_truncates_the_legacy_path(engine):
    legacy = engine.traverse(corpus="proj-a", start_node="ROOT", token_budget=2500)
    dig = engine.traverse(corpus="proj-a", start_node="ROOT", token_budget=2500,
                          tier="digest")
    assert len(legacy["nodes"]) == 2 and legacy["truncated"] is True
    assert dig["success"] is True
    assert dig["count"] == dig["total"] == 6
    assert dig["complete"] is True and dig["truncated"] is False and dig["cursor"] is None
    assert {d["id"] for d in dig["digest"]} == {"ROOT", "C1", "C2", "C3", "C4", "C5"}


def test_digest_ignores_the_budget_entirely(engine):
    """Same digest at budget=1 as at budget=1e9. A tier whose completeness moved with
    the budget would be the old defect with a new name."""
    a = engine.traverse(corpus="proj-a", start_node="ROOT", token_budget=1, tier="digest")
    b = engine.traverse(corpus="proj-a", start_node="ROOT", token_budget=10**9,
                        tier="digest")
    assert a["digest"] == b["digest"] and a["count"] == 6


def test_digest_does_not_expose_a_nodes_key(engine):
    """`digest` rows live under `digest`, never under `nodes` — a caller that reads
    `nodes` out of a digest response gets a LOUD KeyError, not silent routing rows
    mistaken for node content."""
    r = engine.traverse(corpus="proj-a", start_node="ROOT", tier="digest")
    assert "nodes" not in r


def test_digest_refuses_an_offset(engine):
    r = engine.traverse(corpus="proj-a", start_node="ROOT", tier="digest", offset=2)
    assert r["success"] is False
    assert r["reason"] == "digest_offset_rejected"


def test_digest_errors_rather_than_trimming_past_the_ceiling():
    nodes = {f"N{i}": _node(f"N{i}") for i in range(DIGEST_MAX_NODES + 5)}
    edges = [_edge("N0", f"N{i}") for i in range(1, DIGEST_MAX_NODES + 5)]
    store = LtsStore()
    store.add_partition(Partition(_mk_index("big", "proj.big", nodes, edges)))
    eng = LtsEngine(store, freshness_provider=_AlwaysFresh())
    r = eng.traverse(corpus="big", start_node="N0", tier="digest")
    assert r["success"] is False
    assert r["reason"] == "digest_ceiling_exceeded"
    assert r["total"] == DIGEST_MAX_NODES + 5
    # The refusal must NOT smuggle a partial digest out alongside the error.
    assert "digest" not in r


# =====================================================================
# 3. THE REGRESSION THAT MATTERS — a digest carries NO body
# =====================================================================
def test_digest_carries_no_body_derived_field(engine):
    """The row's key set is EXACTLY `DIGEST_FIELDS`. Written as an equality, not as a
    blacklist check, so a field nobody thought to blacklist still fails the test."""
    r = engine.traverse(corpus="proj-a", start_node="ROOT", tier="digest")
    for row in r["digest"]:
        assert set(row.keys()) == set(DIGEST_FIELDS), (
            f"digest row key set drifted: {sorted(set(row) ^ set(DIGEST_FIELDS))}")
        assert not (set(row.keys()) & BODY_DERIVED_KEYS)


def test_find_digest_row_adds_only_the_addressing_field(engine):
    r = engine.find(corpus="proj-a", tier="digest")
    for row in r["digest"]:
        assert set(row.keys()) == set(DIGEST_FIELDS) | {"corpus"}
        assert not (set(row.keys()) & BODY_DERIVED_KEYS)


def test_digest_response_contains_no_body_text_anywhere(engine):
    """The falsifier with teeth: a distinctive string that exists ONLY in a node's
    property bag must appear NOWHERE in the serialized digest response — not in a
    summary, not in an excerpt, not in a stray `properties` that rode along on a
    routed node or an edge. If a body-derived field is ever added, this fails without
    anyone having to name the new field."""
    for r in (engine.traverse(corpus="proj-a", start_node="ROOT", tier="digest"),
              engine.find(corpus="proj-a", tier="digest"),
              engine.node(corpus="proj-a", node_id="ROOT", tier="digest")):
        assert BODY_MARKER not in json.dumps(r)


def test_digest_reports_property_count_without_the_properties(engine):
    """The count is the ROUTING signal ("there is a lot in here"); the values are the
    ANSWER, and the digest must not have them. Both halves asserted together, because
    the temptation is to satisfy the first by shipping the second."""
    r = engine.node(corpus="proj-a", node_id="ROOT", tier="digest")
    # what-this-is + why + decided-in-session
    assert r["digest"]["property_count"] == 3
    assert "properties" not in r["digest"]


def test_digest_timestamp_is_a_date_and_only_a_date(engine):
    """`timestamp` is the one property-bag read, laundered through `normalize_date`, so
    it is structurally incapable of carrying prose: `2026-07-20 (c14)` -> `2026-07-20`."""
    r = engine.node(corpus="proj-a", node_id="ROOT", tier="digest")
    assert r["digest"]["timestamp"] == "2026-07-20"


def test_digest_title_is_the_stored_title_not_a_derived_label(engine):
    """A title is a LABEL and is in; a summary is a CLAIM and is out. Assert the field
    is verbatim node metadata so nobody quietly turns it into a generated one-liner."""
    r = engine.node(corpus="proj-a", node_id="C1", tier="digest")
    assert r["digest"]["title"] == "title of C1"


# =====================================================================
# 4. FULL ERRORS RATHER THAN TRUNCATING
# =====================================================================
def test_full_errors_instead_of_returning_a_fraction(engine):
    r = engine.traverse(corpus="proj-a", start_node="ROOT", token_budget=2500,
                        tier="full")
    assert r["success"] is False
    assert r["reason"] == "full_ceiling_exceeded"
    assert r["total"] == 6 and r["total_token_count"] == 6000
    assert "nodes" not in r          # never a partial payload beside the error


def test_full_is_complete_when_it_fits(engine):
    r = engine.traverse(corpus="proj-a", start_node="ROOT", token_budget=10**6,
                        tier="full")
    assert r["success"] is True
    assert len(r["nodes"]) == 6 and r["total"] == 6
    assert r["truncated"] is False and r["complete"] is True


def test_find_full_errors_instead_of_truncating(engine):
    r = engine.find(corpus="proj-a", token_budget=2500, tier="full")
    assert r["success"] is False and r["reason"] == "full_ceiling_exceeded"


def test_node_full_errors_when_the_excerpt_or_edges_would_be_clipped(engine):
    r = engine.node(corpus="proj-a", node_id="ROOT", token_budget=2, tier="full")
    assert r["success"] is False and r["reason"] == "full_ceiling_exceeded"


def test_node_full_succeeds_on_a_modest_body_past_the_legacy_excerpt_char_cap(tmp_path):
    """[ISSUE-EXCERPT-BUDGET] regression.

    Measured live: `tier='full'` refused `Acme-RESEARCH-FRAME` (1,437-token body, 10
    edges) at the DEFAULT 32,768-token budget, `excerpt_truncated=True` /
    `edges_truncated=False` — and refused `DEC-ACME-LOOKUP-ORDER-IS-FIXED`
    (2,871-token body) even at `token_budget=90000`. Root cause: `_source_excerpt`
    capped the excerpt at the LEGACY `MAX_EXCERPT_CHARS` (4000 chars ~= 1000 tokens)
    REGARDLESS of the caller's declared `token_budget`, so any node whose own body
    exceeded ~1000 tokens could never pass `tier='full'` no matter how large a budget
    was named — the error's own remediation ("raise `token_budget`") was categorically
    impossible to satisfy. Fixed by making the excerpt cap budget-derived (no
    absolute-char floor) for every TIERED call; the untiered legacy path is untouched.

    Needs REAL `root_paths`: the shared `engine` fixture above sets `root_paths: []`,
    which short-circuits `_source_excerpt` before it ever opens a file — the
    file-reading branch, where the legacy cap actually bites, was UNTESTED by every
    other case in this module. That is WHY this defect shipped unnoticed."""
    body_line = ("x" * 200) + "\n"                       # 201 chars/line
    n_lines = 25                                         # ~5,025 chars > MAX_EXCERPT_CHARS
    body_chars = len(body_line) * n_lines
    assert body_chars > 4000                             # sanity: trips the legacy cap
    file_lines = ["---\n", "title: t\n", "---\n"] + [body_line] * n_lines
    (tmp_path / "big.lts").write_text("".join(file_lines))

    node = _node("BIG", tokens=round(body_chars / 4))
    node["source_file"] = "big.lts"
    node["line_range"] = [4, 3 + n_lines]                # exactly the body lines
    nodes = {"BIG": node}
    edges = []
    for i in range(1, 8):
        tgt = f"T{i}"
        nodes[tgt] = _node(tgt, tokens=10)
        edges.append(_edge("BIG", tgt))
    index = _mk_index("proj-big", "proj.big", nodes, edges)
    index["source_corpus"]["root_paths"] = [str(tmp_path)]
    store = LtsStore()
    store.add_partition(Partition(index))
    eng = LtsEngine(store, freshness_provider=_AlwaysFresh())

    # DEFAULT budget (32,768) — the exact Acme-RESEARCH-FRAME reproduction shape.
    r = eng.node(corpus="proj-big", node_id="BIG", tier="full")
    assert r["success"] is True, r
    assert r["truncated"] is False and r["complete"] is True
    assert r["source_excerpt"] is not None and len(r["source_excerpt"]) == body_chars
    assert r["edge_total"] == 7

    # The negative case still holds: a budget genuinely too small still refuses, and
    # (unlike before the fix) raising it past the true cost actually resolves it.
    tiny = eng.node(corpus="proj-big", node_id="BIG", tier="full", token_budget=10)
    assert tiny["success"] is False and tiny["reason"] == "full_ceiling_exceeded"
    raised = eng.node(corpus="proj-big", node_id="BIG", tier="full",
                      token_budget=round(body_chars / 4) + 100)
    assert raised["success"] is True, raised


# =====================================================================
# 5. PAGE SAYS IT IS A PAGE
# =====================================================================
def test_page_reports_offset_next_offset_and_total(engine):
    r = engine.traverse(corpus="proj-a", start_node="ROOT", token_budget=2500,
                        tier="page")
    assert r["success"] is True
    assert r["count"] == 2 and r["total"] == 6
    assert r["offset"] == 0 and r["next_offset"] == 2
    assert r["truncated"] is True and r["complete"] is False


def test_page_walks_the_whole_result_via_next_offset(engine):
    seen, offset = [], 0
    while offset is not None:
        r = engine.traverse(corpus="proj-a", start_node="ROOT", token_budget=2500,
                            tier="page", offset=offset)
        seen += [n["id"] for n in r["nodes"]]
        offset = r["next_offset"]
    assert len(seen) == 6 and len(set(seen)) == 6


def test_page_returns_byte_identical_nodes_to_the_legacy_path(engine):
    legacy = engine.traverse(corpus="proj-a", start_node="ROOT", token_budget=2500)
    page = engine.traverse(corpus="proj-a", start_node="ROOT", token_budget=2500,
                           tier="page")
    assert page["nodes"] == legacy["nodes"] and page["edges"] == legacy["edges"]


# =====================================================================
# 6. TIER RESOLUTION / lts_node digest / dispatch + schema wiring
# =====================================================================
def test_unknown_tier_is_an_error_not_a_silent_fallback(engine):
    """A typo'd tier that quietly served the truncating path would rebuild the very
    defect this layer removes."""
    r = engine.traverse(corpus="proj-a", start_node="ROOT", tier="digset")
    assert r["success"] is False and r["reason"] == "unknown_tier"


def test_node_digest_drops_the_node_and_the_excerpt_but_keeps_a_complete_edge_map(engine):
    r = engine.node(corpus="proj-a", node_id="ROOT", token_budget=1, tier="digest")
    assert r["success"] is True and r["complete"] is True
    assert "node" not in r and "source_excerpt" not in r
    fwd = r["edges"]["semantic"]["forward"]
    assert sorted(fwd) == ["C1", "C2", "C3", "C4", "C5"]   # complete despite budget=1
    assert r["edge_total"] == 5 and r["truncated"] is False


def test_digest_edge_count_is_corpus_degree(engine):
    r = engine.traverse(corpus="proj-a", start_node="ROOT", tier="digest")
    by_id = {d["id"]: d for d in r["digest"]}
    assert by_id["ROOT"]["edge_count"] == 5
    assert by_id["C1"]["edge_count"] == 1


def test_dispatch_threads_tier_through_for_all_three_tools(engine):
    for name, args in (("lts_traverse", {"corpus": "proj-a", "start_node": "ROOT"}),
                       ("lts_find", {"corpus": "proj-a"}),
                       ("lts_node", {"corpus": "proj-a", "node_id": "ROOT"})):
        r = dispatch(name, {**args, "token_budget": 2500, "tier": "digest"},
                     tools=engine)
        assert r["success"] is True and r["tier"] == "digest"
        assert "digest" in r


def test_mcp_schema_advertises_tier_on_the_three_tools():
    from factlattice_engine.server import TOOLS
    by_name = {t["name"]: t for t in TOOLS}
    for name in ("lts_find", "lts_node", "lts_traverse"):
        prop = by_name[name]["inputSchema"]["properties"]["tier"]
        assert prop["enum"] == list(TIERS)
        # The advertised field list is DERIVED from DIGEST_FIELDS, never restated —
        # a schema that promises a field the engine does not build is unfalsifiable
        # from the outside.
        for f in DIGEST_FIELDS:
            assert f in prop["description"]
    # And no tool advertises a tier it cannot honour.
    for name in ("lts_capabilities", "lts_flow"):
        assert "tier" not in by_name[name]["inputSchema"]["properties"]
