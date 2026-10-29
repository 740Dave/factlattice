"""Unit tests for the 3 lts-engine enhancements built on top of the base engine:

  [ENH-1] normalize_date        — robust date-property normalization (tools.py)
  [ENH-2] area-scope filter     — find/traverse `area=` param (tools.py + config.py)
  [ENH-3] multi-root freshness  — Partition.freshness() hashes ALL root_paths (store.py)
                                   + config.py's merged 3-labeled-root corpus build

Synthetic in-memory indexes / tmp_path real files (no example dependency) so this
logic is proven in isolation; the REAL-data acceptance run is done against a deployment's own
corpora, outside this repository.
"""

from __future__ import annotations

import pytest

from factlattice_engine import (
    LtsStore, LtsEngine, Partition, combined_files_hash, dispatch, normalize_date,
)
from factlattice_engine.config import build_merged_partition


# ============================================================================
# [ENH-1] normalize_date
# ============================================================================
@pytest.mark.parametrize("raw, expected", [
    ("2026-07-20", "2026-07-20"),                      # full ISO date
    ("2026-07-21 (c14)", "2026-07-21"),                 # trailing (...) session marker
    ("2026-07 (prose)", "2026-07-01"),                  # partial month -> 1st-of-month
    ("2026-07", "2026-07-01"),                          # partial month, no marker
    ("  2026-07-20  ", "2026-07-20"),                   # whitespace-tolerant
    ("2026-07-20   (c14)", "2026-07-20"),                # extra whitespace before marker
    ("2026", "2026-01-01"),                             # bare year -> Jan 1st
    ("2026 (rough)", "2026-01-01"),                     # bare year + marker
])
def test_normalize_date_real_forms(raw, expected):
    assert normalize_date(raw) == expected


@pytest.mark.parametrize("garbage", [
    "TBD", "", "   ", "unknown", "next sprint", None, 42, 3.14, ["2026-07-20"],
])
def test_normalize_date_garbage_returns_none(garbage):
    assert normalize_date(garbage) is None


def test_normalize_date_does_not_crash_on_anything():
    # Never raises — an unparseable value is EXCLUDED from a date-filtered query,
    # not a crash.
    for v in [object(), {}, "()", "(c14)", "2026-13-99", "-- --"]:
        normalize_date(v)  # no exception


# ============================================================================
# [ENH-2] area-scope filter
# ============================================================================
class _AlwaysFresh:
    def evaluate(self, part):
        from factlattice_engine import FreshnessResult
        return FreshnessResult(part.corpus_id, True, part.files_hash, part.files_hash)


def _node(nid, area=None, title=None, decided=None):
    props = {}
    if decided:
        props["decided-in-session"] = decided
    return {"id": nid, "title": title or nid, "domain": None, "status": None,
            "properties": props, "source_file": None, "line_range": [],
            "token_count": 1, "area": area}


def _edge(src, tgt, etype, layer="semantic"):
    return {"source": src, "target": tgt, "type": etype, "layer": layer,
            "target_domain": None, "dangling": False, "flags": []}


@pytest.fixture
def area_store():
    nodes = {
        "DEC-1": _node("DEC-1", area="decisions", title="decision one"),
        "DEC-2": _node("DEC-2", area="decisions", title="decision two"),
        "SPEC-1": _node("SPEC-1", area="specs", title="spec one"),
        "SPEC-2": _node("SPEC-2", area="specs", title="spec two"),
        "ART-1": _node("ART-1", area="artifacts", title="artifact one"),
    }
    edges = [
        _edge("DEC-1", "SPEC-1", "DEPENDS_ON"),   # cross-area edge
        _edge("SPEC-1", "SPEC-2", "DEPENDS_ON"),  # in-area edge
    ]
    layers = {}
    for e in edges:
        layers.setdefault(e["layer"], {"edges": []})["edges"].append(e)
    index = {
        "source_corpus": {"corpus_id": "proj-x", "domain_suffix": "proj.x",
                          "files_hash": "sha256:test", "root_paths": []},
        "nodes": nodes, "layers": layers,
        "stats": {"total_nodes": len(nodes), "total_edges": len(edges)},
    }
    store = LtsStore()
    store.add_partition(Partition(index))
    return store


@pytest.fixture
def area_tools(area_store):
    return LtsEngine(area_store, freshness_provider=_AlwaysFresh())


def test_find_area_decisions_excludes_specs(area_tools):
    res = area_tools.find(corpus="proj-x", area="decisions")
    ids = {n["id"] for n in res["nodes"]}
    assert ids == {"DEC-1", "DEC-2"}
    assert res["filters"]["area"] == "decisions"


def test_find_area_specs_excludes_decisions(area_tools):
    res = area_tools.find(corpus="proj-x", area="specs")
    ids = {n["id"] for n in res["nodes"]}
    assert ids == {"SPEC-1", "SPEC-2"}


def test_find_area_artifacts_isolated(area_tools):
    res = area_tools.find(corpus="proj-x", area="artifacts")
    assert {n["id"] for n in res["nodes"]} == {"ART-1"}


def test_find_no_area_spans_all(area_tools):
    res = area_tools.find(corpus="proj-x")
    assert res["total_matched"] == 5


def test_find_area_combines_with_text(area_tools):
    res = area_tools.find(corpus="proj-x", area="decisions", text="two")
    assert {n["id"] for n in res["nodes"]} == {"DEC-2"}


def test_traverse_area_restricts_discovered_nodes(area_tools):
    # DEC-1 -> SPEC-1 -> SPEC-2 (2 hops). area='specs' should drop DEC-1's cross-area
    # neighbor from the DISCOVERED set... but DEC-1 is the start node, always kept.
    res = area_tools.traverse(corpus="proj-x", start_node="DEC-1", max_depth=2,
                              area="specs")
    ids = {n["id"] for n in res["nodes"]}
    assert "DEC-1" in ids  # start node always kept regardless of area
    assert "SPEC-1" in ids and "SPEC-2" in ids  # discovered + area-matching
    assert res["filters"]["area"] == "specs"


def test_traverse_area_excludes_non_matching_discovered(area_tools):
    res = area_tools.traverse(corpus="proj-x", start_node="SPEC-1", max_depth=1,
                              direction="forward", area="decisions")
    ids = {n["id"] for n in res["nodes"]}
    # SPEC-1 is the start (kept); SPEC-2 is discovered (forward) but area='specs' !=
    # 'decisions', so it's dropped from the emitted set.
    assert ids == {"SPEC-1"}


def test_dispatch_find_area(area_tools):
    res = dispatch("lts_find", {"corpus": "proj-x", "area": "decisions"}, tools=area_tools)
    assert {n["id"] for n in res["nodes"]} == {"DEC-1", "DEC-2"}


def test_dispatch_traverse_area(area_tools):
    res = dispatch("lts_traverse",
                   {"corpus": "proj-x", "start_node": "DEC-1", "max_depth": 2,
                    "area": "specs"}, tools=area_tools)
    assert {n["id"] for n in res["nodes"]} == {"DEC-1", "SPEC-1", "SPEC-2"}


# ============================================================================
# [ENH-3] multi-root freshness fix
# ============================================================================
def _write_lts(path, node_id, definition):
    path.write_text(
        "---\ndomain: core.example\n---\n"
        f"# Test [SPEC:{node_id}-DOC]\n\n"
        f"### [{node_id}] Node\n- definition: {definition}\n"
    )


@pytest.fixture
def two_root_dirs(tmp_path):
    root_a = tmp_path / "decisions"
    root_b = tmp_path / "specs"
    root_a.mkdir()
    root_b.mkdir()
    _write_lts(root_a / "a.lts", "A", "alpha")
    _write_lts(root_b / "b.lts", "B", "beta")
    return root_a, root_b


def test_combined_files_hash_degenerates_for_one_root(tmp_path):
    from factlattice import files_hash
    root = tmp_path / "solo"
    root.mkdir()
    _write_lts(root / "s.lts", "S", "solo")
    plain, _ = files_hash(str(root))
    assert combined_files_hash([str(root)]) == plain


def test_combined_files_hash_changes_when_either_root_changes(two_root_dirs):
    root_a, root_b = two_root_dirs
    h0 = combined_files_hash([str(root_a), str(root_b)])
    _write_lts(root_a / "a.lts", "A", "alpha-EDITED")
    h1 = combined_files_hash([str(root_a), str(root_b)])
    assert h0 != h1
    _write_lts(root_b / "b.lts", "B", "beta-EDITED")
    h2 = combined_files_hash([str(root_a), str(root_b)])
    assert h2 != h1


def test_partition_freshness_detects_change_in_non_first_root(two_root_dirs):
    """The bug this fixes: freshness() used to hash ONLY root_paths[0]. Prove a
    content change in the SECOND root now flips freshness from True to False."""
    root_a, root_b = two_root_dirs
    part = build_merged_partition(
        "proj-y",
        [{"path": str(root_a), "area": "decisions"},
         {"path": str(root_b), "area": "specs"}],
        "proj.y",
    )
    assert part is not None
    assert part.root_paths == [str(root_a), str(root_b)]
    fr = part.freshness()
    assert fr.fresh is True

    # Mutate ONLY the second root (root_paths[1], not [0]).
    _write_lts(root_b / "b.lts", "B", "beta-CHANGED")
    fr2 = part.freshness()
    assert fr2.fresh is False, "freshness() must catch a change in a NON-first root"


def test_partition_freshness_detects_change_in_first_root(two_root_dirs):
    root_a, root_b = two_root_dirs
    part = build_merged_partition(
        "proj-z",
        [{"path": str(root_a), "area": "decisions"},
         {"path": str(root_b), "area": "specs"}],
        "proj.z",
    )
    assert part.freshness().fresh is True
    _write_lts(root_a / "a.lts", "A", "alpha-CHANGED")
    assert part.freshness().fresh is False


def test_build_merged_partition_tags_area_per_root(two_root_dirs):
    root_a, root_b = two_root_dirs
    part = build_merged_partition(
        "proj-w",
        [{"path": str(root_a), "area": "decisions"},
         {"path": str(root_b), "area": "specs"}],
        "proj.w",
    )
    assert part.get_node("A")["area"] == "decisions"
    assert part.get_node("B")["area"] == "specs"


def test_build_merged_partition_skips_missing_root(tmp_path, two_root_dirs):
    root_a, root_b = two_root_dirs
    missing = tmp_path / "does-not-exist"
    part = build_merged_partition(
        "proj-v",
        [{"path": str(root_a), "area": "decisions"},
         {"path": str(missing), "area": "artifacts"}],
        "proj.v",
    )
    assert part is not None
    assert str(missing) not in part.root_paths
    assert part.get_node("A") is not None


def test_build_merged_partition_only_file_excludes_siblings(tmp_path):
    """The co-located-siblings mitigation: a root pinned with `only_file` admits ONLY
    that file's nodes, even though its directory contains other .lts files too."""
    shared = tmp_path / "decisions"
    shared.mkdir()
    _write_lts(shared / "mine.lts", "MINE", "my project's decision")
    _write_lts(shared / "sibling.lts", "SIBLING", "someone else's decision")
    part = build_merged_partition(
        "proj-u",
        [{"path": str(shared), "area": "decisions", "only_file": "mine.lts"}],
        "proj.u",
    )
    assert part is not None
    assert part.get_node("MINE") is not None
    assert part.get_node("SIBLING") is None


def test_build_merged_partition_returns_none_when_all_roots_missing(tmp_path):
    missing = tmp_path / "nope"
    part = build_merged_partition(
        "proj-none",
        [{"path": str(missing), "area": "decisions"}],
        "proj.none",
    )
    assert part is None
