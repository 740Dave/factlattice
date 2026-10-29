"""[ISSUE-ANCHOR-TRUNCATION] — the topic->anchor `[:5]` cap at
`LtsEngine.capabilities` silently dropped every 6th+ topic match with no signal: the
response's OWN `truncated`/`cursor` machinery only ever sees the 5 anchors that
survived the slice, so a caller asking for a topic with 8 matches got exactly the same
`truncated: false` a 3-match topic would produce. `anchor_matches_total` (the true
pre-cap count) and `anchors_truncated` (whether the cap actually bit) close that gap.

THIS TEST MUST FAIL AGAINST THE PRE-FIX CODE (base commit 5504567) and PASS AFTER THE
FIX — verified directly: running this exact file against the base commit raises
`KeyError: 'anchor_matches_total'` (the field does not exist there at all); against
the current tree it passes. See the build report for the recorded transcript of both
runs.
"""
from __future__ import annotations

import pytest

from factlattice_engine import FreshnessResult, LtsEngine, LtsStore, Partition


class _AlwaysFresh:
    def evaluate(self, part):
        return FreshnessResult(part.corpus_id, True, part.files_hash, part.files_hash)


def _mk_index(corpus_id, domain_suffix, nodes, edges):
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


def _node(nid, title=None):
    return {"id": nid, "title": title or nid, "domain": None, "status": None,
            "properties": {}, "source_file": None, "line_range": [], "token_count": 1}


def _edge(src, tgt, etype, layer="semantic"):
    return {"source": src, "target": tgt, "type": etype, "layer": layer,
            "target_domain": None, "dangling": False, "flags": []}


@pytest.fixture
def many_matches_store():
    """8 nodes whose title all contain "widget" (more than the `[:5]` cap), plus a HUB
    node with edges to 3 of them so `capabilities` has a real edge surface to walk."""
    nodes = {f"N-{i}": _node(f"N-{i}", title=f"widget {i}") for i in range(8)}
    nodes["HUB"] = _node("HUB", title="hub, unrelated title")
    edges = [_edge("HUB", f"N-{i}", "RELATED_TO") for i in range(3)]
    idx = _mk_index("proj-x", "proj.x", nodes, edges)
    store = LtsStore()
    store.add_partition(Partition(idx))
    return store


@pytest.fixture
def tools(many_matches_store):
    return LtsEngine(many_matches_store, freshness_provider=_AlwaysFresh())


def test_capabilities_reports_anchor_truncation_when_topic_matches_more_than_five(tools):
    res = tools.capabilities(corpus="proj-x", topic="widget")
    assert res["success"]
    assert res["anchor_matches_total"] == 8, "topic 'widget' matches all 8 N-* nodes"
    assert len(res["anchor_nodes"]) == 5, "the [:5] cap is UNCHANGED — this is additive"
    assert res["anchors_truncated"] is True, (
        "8 matches silently capped to 5 with no signal is exactly the defect this "
        "guards; pre-fix `capabilities()` returns neither `anchor_matches_total` nor "
        "`anchors_truncated` at all")


def test_capabilities_reports_no_anchor_truncation_for_a_node_id_anchor(tools):
    """A `node_id`/exact-`topic` anchor is never ambiguous — `anchor_matches_total`
    must read 1, not the size of some unrelated match set."""
    res = tools.capabilities(corpus="proj-x", node_id="HUB")
    assert res["anchor_matches_total"] == 1
    assert res["anchors_truncated"] is False


def test_capabilities_reports_no_truncation_when_topic_matches_fit_under_the_cap():
    a = _mk_index("proj-y", "proj.y",
                  {f"N-{i}": _node(f"N-{i}", title=f"gadget {i}") for i in range(3)}, [])
    store = LtsStore()
    store.add_partition(Partition(a))
    t = LtsEngine(store, freshness_provider=_AlwaysFresh())
    res = t.capabilities(corpus="proj-y", topic="gadget")
    assert res["anchor_matches_total"] == 3
    assert len(res["anchor_nodes"]) == 3
    assert res["anchors_truncated"] is False
