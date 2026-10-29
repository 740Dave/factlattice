"""[LTS-REVERSE-LOOKUP] item 4 — `lts_aggregate`'s `top_k`/`top_by_degree` gain an
explicit `truncated` boolean, computed through the ONE shared `budget.declare()`
contract rather than a second hand-rolled `len(buckets) < total` spelling. Additive:
every existing field is unchanged, this is a field ADDED to the response.
"""
from __future__ import annotations

from factlattice_engine import FreshnessResult, LtsEngine, LtsStore, Partition
from factlattice_engine.analytics import LtsAggregator


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


def _node(nid, prefix):
    return {"id": nid, "title": nid, "domain": None, "status": None,
            "properties": {}, "source_file": None, "line_range": [], "token_count": 1}


def _edge(src, tgt):
    return {"source": src, "target": tgt, "type": "RELATED_TO", "layer": "semantic",
            "target_domain": None, "dangling": False, "flags": []}


def _agg():
    # 7 distinct id-prefix groups (A0..A6 as prefixes "A0".."A6"), so a top_k(k=3) is
    # certainly a bounded slice of a larger distinct set.
    nodes = {f"P{i}-N": _node(f"P{i}-N", f"P{i}") for i in range(7)}
    idx = _mk_index("proj-z", "proj.z", nodes, [_edge("P0-N", "P1-N")])
    store = LtsStore()
    store.add_partition(Partition(idx))
    tools = LtsEngine(store, freshness_provider=_AlwaysFresh())
    return LtsAggregator(tools)


def test_top_k_by_count_declares_truncated_true_when_k_is_smaller_than_distinct():
    agg = _agg()
    res = agg.top_k("id", k=3, by="count")
    assert res["distinct"] == 7
    assert len(res["buckets"]) == 3
    assert res["truncated"] is True


def test_top_k_by_count_declares_truncated_false_when_k_covers_everything():
    agg = _agg()
    res = agg.top_k("id", k=100, by="count")
    assert len(res["buckets"]) == res["distinct"] == 7
    assert res["truncated"] is False


def test_top_by_degree_declares_truncated():
    agg = _agg()
    res = agg.top_by_degree(k=2)
    assert res["total"] == 7
    assert len(res["buckets"]) == 2
    assert res["truncated"] is True

    res_all = agg.top_by_degree(k=100)
    assert len(res_all["buckets"]) == res_all["total"]
    assert res_all["truncated"] is False
