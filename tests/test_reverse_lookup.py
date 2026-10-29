"""[LTS-REVERSE-LOOKUP] — target -> referrers, the completeness path.

Synthetic in-memory indexes, same pattern `test_engine.py` uses (no disk, no example
dependency): a two/three-corpus store built by hand so cross-corpus edges, domain
collisions and staleness are all exact and controllable, rather than incidental
properties of whatever a real corpus happens to contain today.

Covers, per the build brief:
  * POSITIVE — a cross-corpus typed edge AND a non-minting property-value reference
    both surface, each with the correct `how`.
  * NEGATIVE — a zero-referrer node returns an explicit clean zero (no false hit);
    an id COLLISION across two corpora produces no false cross-corpus hit.
  * FRESHNESS — mutating a source after the index was built is picked up on the next
    call (never silently stale); a corpus that cannot be verified fresh is reported,
    never silently omitted-and-called-complete.
  * `include_reverse_digest` — the [R5] non-completeness-by-construction digest on
    `lts_find`/`lts_node`.
  * REGRESSION — `lts_find`/`lts_node` with `include_reverse_digest` OMITTED is
    byte-identical to the pre-change engine (verified independently against the base
    commit 5504567 via a cross-worktree probe; the additive-construction argument for
    WHY is pinned here behaviourally).
"""
from __future__ import annotations

import pytest

from factlattice_engine import (
    FreshnessResult, LtsEngine, LtsStore, Partition, ReverseIndex,
    build_domain_index, dispatch,
)


class _AlwaysFresh:
    def evaluate(self, part):
        return FreshnessResult(part.corpus_id, True, part.files_hash, part.files_hash)


class _SelectivelyStale:
    """Fresh for every corpus EXCEPT the ones named — the [INV-LTS-FRESH] control for
    "a referrer's HOME corpus could not be verified", independent of whether the
    QUERY's own target corpus is fresh."""

    def __init__(self, stale_ids):
        self.stale_ids = set(stale_ids)

    def evaluate(self, part):
        fresh = part.corpus_id not in self.stale_ids
        return FreshnessResult(part.corpus_id, fresh,
                               part.files_hash, "CHANGED" if not fresh else part.files_hash,
                               None if fresh else "test-forced-stale")


def _mk_index(corpus_id, domain_suffix, nodes, edges, files_hash="sha256:test"):
    layers = {}
    for e in edges:
        layers.setdefault(e["layer"], {"edges": []})["edges"].append(e)
    return {
        "source_corpus": {"corpus_id": corpus_id, "domain_suffix": domain_suffix,
                          "files_hash": files_hash, "root_paths": []},
        "nodes": nodes, "layers": layers,
        "stats": {"total_nodes": len(nodes), "total_edges": len(edges),
                  "dangling_edges": 0, "unresolved_target_edges": 0},
    }


def _node(nid, title=None, **props):
    return {"id": nid, "title": title or nid, "domain": None, "status": None,
            "properties": props, "source_file": None, "line_range": [], "token_count": 1}


def _edge(src, tgt, etype, layer="semantic", target_domain=None):
    return {"source": src, "target": tgt, "type": etype, "layer": layer,
            "target_domain": target_domain, "dangling": False, "flags": []}


@pytest.fixture
def cross_corpus_store():
    """proj-a:DEC-1 is the TARGET. proj-a:DEC-2 cites it via a NON-MINTING property
    (`tracks`, not in REL_PROPERTY_EDGES/FLOW_PROPERTY_EDGES). proj-b:DEC-9 cites it via
    a real CROSS-CORPUS typed edge (`SUPERSEDES`, `target_domain="proj.a"`). proj-a:
    DEC-3 references NOTHING and nothing references IT — the clean-zero control.
    proj-b ALSO defines a node literally named `DEC-1` (id collision, [INV-LTS-CORPUS-
    ISOLATION]'s classic trap) with NO reference to anything, so a false cross-corpus
    hit would show up as a phantom referrer of proj-b's OWN unrelated DEC-1.
    """
    a = _mk_index("proj-a", "proj.a", {
        "DEC-1": _node("DEC-1", title="alpha handover"),
        "DEC-2": _node("DEC-2", title="beta", tracks="[DEC-1]"),
        "DEC-3": _node("DEC-3", title="gamma, unreferenced"),
    }, [])
    b = _mk_index("proj-b", "proj.b", {
        "DEC-9": _node("DEC-9", title="B supersedes A's DEC-1"),
        "DEC-1": _node("DEC-1", title="B's OWN unrelated DEC-1"),
    }, [_edge("DEC-9", "DEC-1", "SUPERSEDES", target_domain="proj.a")])
    store = LtsStore()
    store.add_partition(Partition(a))
    store.add_partition(Partition(b))
    return store


@pytest.fixture
def tools(cross_corpus_store):
    return LtsEngine(cross_corpus_store, freshness_provider=_AlwaysFresh())


# ---------------------------------------------------------------------------
# POSITIVE controls
# ---------------------------------------------------------------------------
def test_cross_corpus_typed_edge_surfaces_as_referrer(tools):
    res = tools.reverse(corpus="proj-a", node_id="DEC-1")
    assert res["success"]
    hows = {(r["corpus"], r["node_id"], r["how"]) for r in res["referrers"]}
    assert ("proj-b", "DEC-9", "edge:SUPERSEDES") in hows


def test_non_minting_property_value_surfaces_as_referrer(tools):
    res = tools.reverse(corpus="proj-a", node_id="DEC-1")
    hows = {(r["corpus"], r["node_id"], r["how"]) for r in res["referrers"]}
    assert ("proj-a", "DEC-2", "property:tracks") in hows


def test_both_channels_union_and_declare_reach(tools):
    res = tools.reverse(corpus="proj-a", node_id="DEC-1")
    assert res["count"] == 2
    assert res["total_matched"] == 2
    assert res["truncated"] is False
    assert res["cursor"] is None
    assert res["complete"] is True
    assert res["stale_corpora"] == []


# ---------------------------------------------------------------------------
# NEGATIVE controls
# ---------------------------------------------------------------------------
def test_zero_referrers_is_an_explicit_clean_zero(tools):
    res = tools.reverse(corpus="proj-a", node_id="DEC-3")
    assert res["success"]
    assert res["referrers"] == []
    assert res["count"] == 0
    assert res["total_matched"] == 0
    assert res["truncated"] is False
    assert res["complete"] is True


def test_id_collision_produces_no_false_cross_corpus_hit(tools):
    """proj-a:DEC-2's `[DEC-1]` reference (no domain qualifier) must resolve to
    proj-a's OWN DEC-1 — never to proj-b's same-named, unrelated DEC-1."""
    res = tools.reverse(corpus="proj-b", node_id="DEC-1")
    assert res["success"]
    assert res["referrers"] == [], (
        "an internal same-corpus reference in proj-a leaked into proj-b's "
        "same-named node — exactly the id-collision false hit this index must not make")


def test_edge_types_filter_narrows_to_the_named_types(tools):
    res = tools.reverse(corpus="proj-a", node_id="DEC-1", edge_types=["DEPENDS_ON"])
    assert res["referrers"] == [], "no DEPENDS_ON edge points at DEC-1 in this fixture"
    res2 = tools.reverse(corpus="proj-a", node_id="DEC-1", edge_types=["SUPERSEDES"])
    assert len(res2["referrers"]) == 1
    assert res2["referrers"][0]["how"] == "edge:SUPERSEDES"


def test_verbs_filter_narrows_to_the_named_property_keys(tools):
    res = tools.reverse(corpus="proj-a", node_id="DEC-1", verbs=["tracks"])
    assert len(res["referrers"]) == 1
    assert res["referrers"][0]["how"] == "property:tracks"
    res2 = tools.reverse(corpus="proj-a", node_id="DEC-1", verbs=["nonexistent-key"])
    assert res2["referrers"] == []


# ---------------------------------------------------------------------------
# domain resolution: ambiguous / unresolved is FILED, never guessed
# ---------------------------------------------------------------------------
def test_ambiguous_domain_reference_is_filed_not_guessed():
    """Two partitions sharing ONE domain_suffix: a cross-corpus reference naming that
    suffix cannot be resolved to either — it must be reported as unresolved, never
    silently attached to whichever partition happened to load first."""
    a1 = _mk_index("proj-a1", "shared.dom", {"X": _node("X")}, [])
    a2 = _mk_index("proj-a2", "shared.dom", {"X": _node("X")}, [])
    c = _mk_index("proj-c", "proj.c", {
        "SRC": _node("SRC"),
    }, [_edge("SRC", "X", "RELATED_TO", target_domain="shared.dom")])
    store = LtsStore()
    store.add_partition(Partition(a1))
    store.add_partition(Partition(a2))
    store.add_partition(Partition(c))
    t = LtsEngine(store, freshness_provider=_AlwaysFresh())
    res1 = t.reverse(corpus="proj-a1", node_id="X")
    res2 = t.reverse(corpus="proj-a2", node_id="X")
    assert res1["referrers"] == [] and res2["referrers"] == []
    assert res1["unresolved_domain_refs"] == 1
    assert res2["unresolved_domain_refs"] == 1


# ---------------------------------------------------------------------------
# FRESHNESS controls
# ---------------------------------------------------------------------------
def test_a_real_content_change_is_picked_up_not_served_stale(tools, cross_corpus_store):
    """The cache must bust on the SAME `_freshness_key()` mechanism `adjacency()`/
    `degree_index()` already use — proven by mutating a partition's content AND its
    freshness key directly (simulating a real re-index) and observing the NEXT call
    reflect it, rather than replaying the cached snapshot."""
    before = tools.reverse(corpus="proj-a", node_id="DEC-3")
    assert before["referrers"] == []

    part_a = cross_corpus_store.get_partition("proj-a")
    part_a.nodes["DEC-4"] = _node("DEC-4", title="delta", tracks="[DEC-3]")
    part_a.files_hash = "sha256:changed"   # a real re-index moves this

    after = tools.reverse(corpus="proj-a", node_id="DEC-3")
    hows = {(r["corpus"], r["node_id"], r["how"]) for r in after["referrers"]}
    assert ("proj-a", "DEC-4", "property:tracks") in hows, (
        "the ReverseIndex cache did not invalidate on a real content/freshness-key "
        "change — it would be silently serving a stale snapshot")


def test_a_stale_referrer_source_is_declared_not_silently_omitted(cross_corpus_store):
    """proj-b (the home of the cross-corpus SUPERSEDES referrer) cannot be verified
    fresh. The TARGET corpus (proj-a) itself is fine, so the call must still ANSWER —
    but it must say the answer may be incomplete, never present a partial union as if
    it were the whole one."""
    t = LtsEngine(cross_corpus_store, freshness_provider=_SelectivelyStale({"proj-b"}))
    res = t.reverse(corpus="proj-a", node_id="DEC-1")
    assert res["success"]
    assert "proj-b" in res["stale_corpora"]
    assert res["complete"] is False
    # The property-channel referrer (same-corpus, proj-a itself, which IS fresh) must
    # still be there — staleness elsewhere must not blank the whole answer.
    hows = {(r["corpus"], r["node_id"], r["how"]) for r in res["referrers"]}
    assert ("proj-a", "DEC-2", "property:tracks") in hows
    # The cross-corpus edge referrer, whose home (proj-b) is unverifiable, must NOT be
    # silently asserted as absent — it is a hole in the union the stale flag names.
    assert ("proj-b", "DEC-9", "edge:SUPERSEDES") not in hows


def test_the_target_corpus_itself_stale_refuses_exactly_like_node_and_traverse():
    """Consistency with the rest of the engine: a STALE target corpus is the ordinary
    [INV-LTS-FRESH] refusal, not a special reverse-lookup behaviour."""
    a = _mk_index("proj-a", "proj.a", {"DEC-1": _node("DEC-1")}, [])
    store = LtsStore()
    store.add_partition(Partition(a))
    t = LtsEngine(store, freshness_provider=_SelectivelyStale({"proj-a"}))
    res = t.reverse(corpus="proj-a", node_id="DEC-1")
    assert res["success"] is False
    assert res.get("stale") is True


# ---------------------------------------------------------------------------
# `include_reverse_digest` — [R5] non-completeness by construction
# ---------------------------------------------------------------------------
def test_find_digest_flag_is_omitted_by_default_and_byte_identical(tools):
    res_default = tools.find(corpus="proj-a")
    res_explicit_false = tools.find(corpus="proj-a", include_reverse_digest=False)
    assert res_default == res_explicit_false
    for nd in res_default["nodes"]:
        assert "reverse_digest" not in nd


def test_find_digest_flag_attaches_a_capped_non_complete_preview(tools):
    res = tools.find(corpus="proj-a", include_reverse_digest=True)
    by_id = {nd["id"]: nd for nd in res["nodes"]}
    dg = by_id["DEC-1"]["reverse_digest"]
    assert dg["total_referrers"] == 2
    assert dg["returned"] == len(dg["sample"]) == 2
    assert dg["omitted"] == 0
    assert dg["truncated"] is False
    assert "lts_reverse" in dg["see_also"]
    # A node with zero referrers still carries the (empty, non-truncated) shape —
    # "no fields" and "nothing to report" must never look alike.
    dg3 = by_id["DEC-3"]["reverse_digest"]
    assert dg3 == {
        "total_referrers": 0, "returned": 0, "omitted": 0, "truncated": False,
        "sample": [], "complete": True, "stale_corpora": [],
        "see_also": dg3["see_also"],
    }


def test_node_digest_flag_omitted_is_byte_identical(tools):
    res_default = tools.node(corpus="proj-a", node_id="DEC-1")
    res_explicit_false = tools.node(corpus="proj-a", node_id="DEC-1",
                                    include_reverse_digest=False)
    assert res_default == res_explicit_false
    assert "reverse_digest" not in res_default


def test_node_digest_flag_attaches_the_same_shape(tools):
    res = tools.node(corpus="proj-a", node_id="DEC-1", include_reverse_digest=True)
    dg = res["reverse_digest"]
    assert dg["total_referrers"] == 2
    assert dg["truncated"] is False


def test_digest_sample_is_capped_and_declares_the_truncation():
    """The digest is a PREVIEW, not the answer — [R5] "non-completeness by
    construction". 8 referrers of one target must cap the `sample` at
    `REVERSE_DIGEST_SAMPLE_CAP` while `total_referrers` still reports the true count."""
    referrers = {f"R-{i}": _node(f"R-{i}", tracks="[TARGET]") for i in range(8)}
    referrers["TARGET"] = _node("TARGET")
    idx = _mk_index("proj-many", "proj.many", referrers, [])
    store = LtsStore()
    store.add_partition(Partition(idx))
    t = LtsEngine(store, freshness_provider=_AlwaysFresh())
    dg = t._reverse_digest("proj-many", "TARGET")
    assert dg["total_referrers"] == 8
    assert dg["returned"] == t.REVERSE_DIGEST_SAMPLE_CAP == len(dg["sample"])
    assert dg["omitted"] == 8 - t.REVERSE_DIGEST_SAMPLE_CAP
    assert dg["truncated"] is True
    # The complete list is still one call away, and the digest says so by name.
    full = t.reverse(corpus="proj-many", node_id="TARGET")
    assert full["total_matched"] == 8
    assert full["truncated"] is False  # `lts_reverse` itself paged the whole thing


# ---------------------------------------------------------------------------
# MCP dispatch wiring
# ---------------------------------------------------------------------------
def test_dispatch_lts_reverse(tools):
    res = dispatch("lts_reverse", {"corpus": "proj-a", "node_id": "DEC-1"}, tools=tools)
    assert res["success"]
    assert res["total_matched"] == 2


def test_dispatch_lts_find_include_reverse_digest_flag(tools):
    res = dispatch("lts_find", {"corpus": "proj-a", "include_reverse_digest": True},
                   tools=tools)
    by_id = {nd["id"]: nd for nd in res["nodes"]}
    assert "reverse_digest" in by_id["DEC-1"]


def test_dispatch_lts_find_without_the_flag_carries_no_digest(tools):
    res = dispatch("lts_find", {"corpus": "proj-a"}, tools=tools)
    for nd in res["nodes"]:
        assert "reverse_digest" not in nd


# ---------------------------------------------------------------------------
# [INV-LTS-CORPUS-ISOLATION] direct on the module
# ---------------------------------------------------------------------------
def test_reverse_index_never_merges_partitions(cross_corpus_store):
    parts = cross_corpus_store.partitions()
    idx = ReverseIndex.build(parts, build_domain_index(parts))
    # proj-b's own DEC-1 (id collision) has zero referrers of its own.
    assert idx.referrers("proj-b", "DEC-1") == []
    # proj-a's DEC-1 has exactly the two real referrers.
    assert len(idx.referrers("proj-a", "DEC-1")) == 2
