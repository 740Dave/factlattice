"""Unit tests for factlattice_engine (lts-engine): store isolation, router routing,
the 4 query tools, the since/until time filter, and list_corpora.

Uses synthetic in-memory indexes (no disk, no example dependency) so the engine's
logic is proven in isolation; the REAL-data acceptance run is done against a deployment's own
corpora, outside this repository.
"""

from __future__ import annotations

import pytest

from factlattice_engine import (
    ContentHashFreshness, LtsStore, LtsEngine, Partition, Router,
    AllowAllAcl, dispatch,
)


# --- a freshness provider that always says fresh (synthetic indexes have no disk root) --
class _AlwaysFresh:
    def evaluate(self, part):
        from factlattice_engine import FreshnessResult
        return FreshnessResult(part.corpus_id, True, part.files_hash, part.files_hash)


def _mk_index(corpus_id, domain_suffix, nodes, edges):
    """Assemble a minimal graph_index.json-shaped dict."""
    layers = {}
    for e in edges:
        layers.setdefault(e["layer"], {"edges": []})["edges"].append(e)
    return {
        "source_corpus": {"corpus_id": corpus_id, "domain_suffix": domain_suffix,
                          "files_hash": "sha256:test", "root_paths": []},
        "nodes": nodes, "layers": layers,
        "stats": {"total_nodes": len(nodes), "total_edges": len(edges),
                  "dangling_edges": 0, "unresolved_target_edges": 0},
    }


def _node(nid, **props):
    date = props.pop("decided", None)
    p = dict(props)
    if date:
        p["decided-in-session"] = date
    return {"id": nid, "title": props.get("title", nid), "domain": props.get("domain"),
            "status": props.get("status"), "properties": p,
            "source_file": None, "line_range": [], "token_count": 1}


def _edge(src, tgt, etype, layer="semantic", target_domain=None):
    return {"source": src, "target": tgt, "type": etype, "layer": layer,
            "target_domain": target_domain, "dangling": False, "flags": []}


@pytest.fixture
def two_corpus_store():
    a = _mk_index("proj-a", "proj.a",
                  {"DEC-1": _node("DEC-1", decided="2026-07-19", title="alpha handover"),
                   "DEC-2": _node("DEC-2", decided="2026-07-21", title="beta"),
                   "DEC-3": _node("DEC-3", decided="2026-07-22", title="gamma handover")},
                  [_edge("DEC-1", "DEC-2", "DEPENDS_ON"),
                   _edge("DEC-2", "DEC-3", "SUPERSEDES")])
    b = _mk_index("proj-b", "proj.b",
                  {"DEC-1": _node("DEC-1", decided="2026-07-20", title="B's own DEC-1")},
                  [])
    store = LtsStore()
    store.add_partition(Partition(a))
    store.add_partition(Partition(b))
    return store


@pytest.fixture
def tools(two_corpus_store):
    return LtsEngine(two_corpus_store, freshness_provider=_AlwaysFresh())


# --- store partition isolation ------------------------------------------------
def test_store_registers_both_partitions(two_corpus_store):
    assert set(two_corpus_store.corpus_ids()) == {"proj-a", "proj-b"}


def test_store_colliding_id_resolves_per_corpus(two_corpus_store):
    # DEC-1 exists in BOTH corpora but must resolve to DIFFERENT nodes (isolation).
    a = two_corpus_store.get_node("proj-a", "DEC-1")
    b = two_corpus_store.get_node("proj-b", "DEC-1")
    assert a["title"] == "alpha handover"
    assert b["title"] == "B's own DEC-1"
    assert a is not b


def test_store_never_merges_no_global_lookup(two_corpus_store):
    assert two_corpus_store.get_node("proj-a", "DEC-3") is not None
    assert two_corpus_store.get_node("proj-b", "DEC-3") is None  # scoped, never global


def test_store_add_duplicate_corpus_raises(two_corpus_store):
    with pytest.raises(ValueError):
        two_corpus_store.add_partition(Partition(_mk_index("proj-a", "proj.a", {}, [])))


# --- router routing -----------------------------------------------------------
def test_router_resolves_by_domain(two_corpus_store):
    r = Router(two_corpus_store, freshness_provider=_AlwaysFresh())
    rr = r.route("proj.b", "DEC-1")
    assert rr.ok and rr.corpus_id == "proj-b" and rr.node["title"] == "B's own DEC-1"


def test_router_unresolved_corpus(two_corpus_store):
    r = Router(two_corpus_store, freshness_provider=_AlwaysFresh())
    rr = r.route("proj.nonexistent", "DEC-1")
    assert not rr.ok and rr.status == "unresolved_corpus"


def test_router_not_found_node(two_corpus_store):
    r = Router(two_corpus_store, freshness_provider=_AlwaysFresh())
    rr = r.route("proj.b", "NO-SUCH")
    assert not rr.ok and rr.status == "not_found"


def test_router_allow_all_acl_is_default(two_corpus_store):
    r = Router(two_corpus_store, freshness_provider=_AlwaysFresh())
    assert isinstance(r.acl, AllowAllAcl)


def test_router_stale_partition_raises(two_corpus_store):
    class _Stale:
        def evaluate(self, part):
            from factlattice_engine import FreshnessResult
            return FreshnessResult(part.corpus_id, False, "a", "b", "changed")
    from factlattice_engine import StalePartitionError
    r = Router(two_corpus_store, freshness_provider=_Stale())
    with pytest.raises(StalePartitionError):
        r.route("proj.b", "DEC-1")


# --- find ---------------------------------------------------------------------
def test_find_text_scoped(tools):
    res = tools.find(corpus="proj-a", text="handover")
    ids = {n["id"] for n in res["nodes"]}
    assert res["success"] and ids == {"DEC-1", "DEC-3"}


def test_find_all_corpora_when_corpus_omitted(tools):
    res = tools.find(text="handover")
    # DEC-1(alpha) + DEC-3(gamma) from proj-a; proj-b's DEC-1 has no 'handover'.
    corpora = {n["corpus"] for n in res["nodes"]}
    assert corpora == {"proj-a"}
    assert res["total_matched"] == 2


def test_find_missing_corpus_plain_not_found(tools):
    res = tools.find(corpus="ghost", text="x")
    assert res["success"] is False and "not found" in res["error"]


def test_find_prefix(tools):
    res = tools.find(corpus="proj-a", prefix="DEC-")
    assert res["count"] == 3


# --- since/until time filter --------------------------------------------------
def test_find_since_filter(tools):
    res = tools.find(corpus="proj-a", since="2026-07-21")
    ids = {n["id"] for n in res["nodes"]}
    assert ids == {"DEC-2", "DEC-3"}  # DEC-1 is 07-19, excluded


def test_find_until_filter(tools):
    res = tools.find(corpus="proj-a", until="2026-07-20")
    assert {n["id"] for n in res["nodes"]} == {"DEC-1"}


def test_find_since_until_window(tools):
    res = tools.find(corpus="proj-a", since="2026-07-20", until="2026-07-21")
    assert {n["id"] for n in res["nodes"]} == {"DEC-2"}


def test_find_date_property_strips_suffix():
    idx = _mk_index("c", "c.d",
                    {"N": _node("N", decided="2026-07-22 (c14)", title="q")}, [])
    store = LtsStore()
    store.add_partition(Partition(idx))
    t = LtsEngine(store, freshness_provider=_AlwaysFresh())
    res = t.find(corpus="c", since="2026-07-22")
    assert {n["id"] for n in res["nodes"]} == {"N"}  # '(c14)' suffix stripped


# --- node ---------------------------------------------------------------------
def test_node_returns_edges(tools):
    res = tools.node(corpus="proj-a", node_id="DEC-2")
    assert res["success"] and res["node"]["id"] == "DEC-2"
    # DEC-2 has forward SUPERSEDES->DEC-3 and reverse DEPENDS_ON from DEC-1
    fwd = res["edges"]["semantic"]["forward"]
    rev = res["edges"]["semantic"]["reverse"]
    assert "DEC-3" in fwd and "DEC-1" in rev


def test_node_absent_node(tools):
    res = tools.node(corpus="proj-a", node_id="NOPE")
    assert res["success"] and res["node"] is None


# --- traverse -----------------------------------------------------------------
def test_traverse_finds_neighbors(tools):
    res = tools.traverse(corpus="proj-a", start_node="DEC-1", max_depth=1)
    ids = {n["id"] for n in res["nodes"]}
    assert "DEC-1" in ids and "DEC-2" in ids


def test_traverse_edge_type_filter(tools):
    res = tools.traverse(corpus="proj-a", start_node="DEC-2",
                         edge_types=["SUPERSEDES"], direction="forward", max_depth=1)
    ids = {n["id"] for n in res["nodes"]}
    assert ids == {"DEC-2", "DEC-3"}


def test_traverse_since_filters_discovered(tools):
    # from DEC-1 (07-19) depth 2, since 07-21 keeps start + DEC-3 (07-22), drops DEC-2 (07-21? kept)
    res = tools.traverse(corpus="proj-a", start_node="DEC-1", max_depth=2,
                         since="2026-07-22")
    ids = {n["id"] for n in res["nodes"]}
    assert "DEC-1" in ids  # start always kept
    assert "DEC-2" not in ids  # 07-21 < 07-22
    assert "DEC-3" in ids  # 07-22


# --- capabilities -------------------------------------------------------------
def test_capabilities_around_node(tools):
    res = tools.capabilities(corpus="proj-a", node_id="DEC-2")
    cap_ids = {c["id"] for c in res["capabilities"]}
    assert res["success"] and cap_ids  # DEC-1 and/or DEC-3 connect to DEC-2


# --- list_corpora -------------------------------------------------------------
def test_list_corpora(tools):
    res = tools.list_corpora()
    ids = {c["corpus_id"] for c in res["corpora"]}
    assert res["count"] == 2 and ids == {"proj-a", "proj-b"}
    a = next(c for c in res["corpora"] if c["corpus_id"] == "proj-a")
    assert a["total_nodes"] == 3 and a["total_edges"] == 2


# --- dispatch (transport-agnostic) --------------------------------------------
def test_dispatch_routes_to_tools(tools):
    res = dispatch("lts_find", {"corpus": "proj-a", "text": "handover"}, tools=tools)
    assert res["success"] and res["count"] == 2


def test_dispatch_list_corpora(tools):
    res = dispatch("lts_list_corpora", {}, tools=tools)
    assert res["count"] == 2


def test_dispatch_unknown_tool(tools):
    res = dispatch("lts_bogus", {}, tools=tools)
    assert res["success"] is False and "unknown" in res["error"]


def test_dispatch_missing_required_arg(tools):
    res = dispatch("lts_node", {"corpus": "proj-a"}, tools=tools)  # no node_id
    assert res["success"] is False and "missing required argument" in res["error"]


# --- the token-budget CONTRACT ([DEF-BUDGET-TOO-SMALL]) -----------------------
# The default was 8000 — BELOW the size of the routine boot-read slices it had to
# carry, so `find`'s honest truncation declaration fired on every boot and a caller
# deriving a statistic from `nodes` got a wrong answer that looked right. These pin
# the three properties that make the fix a fix and not a removal of the bound.
def _budget_store(n_nodes: int, per_node_tokens: int):
    nodes = {}
    for i in range(n_nodes):
        nd = _node(f"DEC-{i:03d}", title=f"node {i}")
        nd["token_count"] = per_node_tokens
        nodes[nd["id"]] = nd
    store = LtsStore()
    store.add_partition(Partition(_mk_index("big", "proj.big", nodes, [])))
    return store


@pytest.fixture
def big_tools():
    """100 nodes x 1000 tokens = 100_000 tokens: comfortably OVER any sane default."""
    return LtsEngine(_budget_store(100, 1000), freshness_provider=_AlwaysFresh())


def test_default_token_budget_carries_a_routine_slice():
    """The measured boot slices top out at ~9.7k tokens; the default must clear them
    with headroom. Pinned as a FLOOR, not an equality — raising it further is fine,
    dropping it back under a routine read is the regression."""
    from factlattice_engine.tools import DEFAULT_TOKEN_BUDGET
    assert DEFAULT_TOKEN_BUDGET >= 32768


def test_default_budget_does_not_truncate_a_boot_sized_slice():
    """18 nodes / ~9.7k tokens — the largest measured boot slice — must come back
    WHOLE, with `truncated` false and no cursor."""
    tools = LtsEngine(_budget_store(18, 540), freshness_provider=_AlwaysFresh())
    res = tools.find(corpus="big")
    assert res["count"] == res["total_matched"] == 18
    assert res["truncated"] is False and res.get("cursor") is None


def test_the_bound_still_bounds(big_tools):
    """A 100k-token result at the default MUST still truncate, declare it, and hand
    back a usable cursor. An unbounded `find` is a WORSE failure than truncation: the
    query succeeds and destroys the caller's context."""
    res = big_tools.find(corpus="big")
    assert res["truncated"] is True
    assert res["count"] < res["total_matched"] == 100
    assert res["cursor"] == res["count"]
    page2 = big_tools.find(corpus="big", offset=res["cursor"])
    ids1 = {n["id"] for n in res["nodes"]}
    ids2 = {n["id"] for n in page2["nodes"]}
    assert page2["count"] > 0 and not (ids1 & ids2)   # the cursor makes PROGRESS


def test_explicit_budget_overrides_smaller(big_tools):
    res = big_tools.find(corpus="big", token_budget=2500)
    assert res["count"] == 2 and res["truncated"] is True   # 2 x 1000 fits, 3rd doesn't


def test_explicit_budget_overrides_LARGER(big_tools):
    """The default is a DEFAULT, not a ceiling. This previously clamped with
    `min(default, tb)`, so a caller that explicitly asked for more got the default's
    truncated page back with no signal its argument had been ignored."""
    res = big_tools.find(corpus="big", token_budget=200_000)
    assert res["count"] == res["total_matched"] == 100
    assert res["truncated"] is False and res.get("cursor") is None


def test_advertised_schema_default_is_derived_not_restated():
    """A schema that advertises one default while the engine applies another is
    unfalsifiable from the outside. Every budget-taking tool shares ONE schema object
    whose text is f-stringed from the constant, so they cannot drift."""
    from factlattice_engine import server
    from factlattice_engine.tools import DEFAULT_TOKEN_BUDGET
    schemas = [t["inputSchema"]["properties"]["token_budget"] for t in server.TOOLS
               if "token_budget" in t["inputSchema"].get("properties", {})]
    assert len(schemas) >= 5
    assert all(s is schemas[0] for s in schemas)           # one object, not N copies
    assert f"default {DEFAULT_TOKEN_BUDGET}" in schemas[0]["description"]
