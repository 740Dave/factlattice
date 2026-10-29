"""[LEC-FRESHNESS] — tests for the INCREMENTAL freshness kernel + the adjacency cache.

What these prove (the phase-2 acceptance bar):

  (a) a changed file flips freshness to STALE **without a full-corpus re-hash on the
      query path** — asserted with a per-file hash-op COUNTER (kernel `hash_ops` +
      a spy on the parser's `file_content_hash`), never timing;
  (b) the periodic full re-verify BACKSTOP catches a change the mtime-hint watch path
      structurally cannot observe (identical mtime AND size, different bytes);
  (c) DETERMINISM — an unchanged corpus yields a stable root and no false-stale, incl.
      a content-preserving `touch` (mtime bumped, bytes identical);
  (d) the ADJACENCY cache is reused while fresh and invalidated on a real content change.

Plus the O(0) benchmark: per-file hash operations on the query path over N queries is
ZERO with the kernel seated, versus N x corpus-file-count without it.
"""

from __future__ import annotations

import os

import pytest

import factlattice.hashing as _hashing
from factlattice import files_hash
from factlattice_engine import (
    FreshnessKernel, LtsEngine, LtsStore, combine_leaf_map, combine_roots,
    combined_files_hash,
)
from factlattice_engine.config import build_merged_partition, build_partition


# ---------------------------------------------------------------------------
# corpus fixtures (real files on disk — the kernel needs real roots)
# ---------------------------------------------------------------------------
def _lts(node_id: str, definition: str, extra: str = "") -> str:
    return (
        "---\ndomain: core.example\n---\n"
        f"# Test [SPEC:{node_id}-DOC]\n\n"
        f"### [{node_id}] Node {node_id}\n- definition: {definition}\n{extra}"
    )


GRAPH_LTS = """\
---
domain: core.example
---
# Graph [SYSTEM:G-DOC]

### [G-A] Alpha
- definition: alpha
- status: operational

### [G-B] Beta
- definition: beta
- status: draft

### [G-C] Gamma
- definition: gamma
- status: draft

## Relationships

### Semantic
- [G-A] DEPENDS_ON [G-B]
- [G-B] IMPLEMENTS [G-C]

### Temporal
- [G-A] BEFORE [G-C]
"""


@pytest.fixture
def corpus(tmp_path):
    """A single-root corpus of 3 .lts files (one of which carries a small graph)."""
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "graph.lts").write_text(GRAPH_LTS, encoding="utf-8")
    (root / "a.lts").write_text(_lts("A", "alpha"), encoding="utf-8")
    (root / "b.lts").write_text(_lts("B", "beta"), encoding="utf-8")
    return root


@pytest.fixture
def two_roots(tmp_path):
    """A MERGED two-root corpus (the config.py decisions/specs shape)."""
    a = tmp_path / "decisions"
    b = tmp_path / "specs"
    a.mkdir()
    b.mkdir()
    (a / "d1.lts").write_text(_lts("D1", "decision one"), encoding="utf-8")
    (a / "d2.lts").write_text(_lts("D2", "decision two"), encoding="utf-8")
    (b / "s1.lts").write_text(_lts("S1", "spec one"), encoding="utf-8")
    return a, b


def _seated(part):
    k = FreshnessKernel()
    k.seat_partition(part)
    return k


def _engine(part):
    """An LtsEngine over one partition using the DEFAULT (real) freshness provider,
    so the query path exercises `ContentHashFreshness` -> `Partition.freshness()`."""
    store = LtsStore()
    store.add_partition(part)
    return LtsEngine(store)


class _HashSpy:
    """Counts per-file CONTENT hash operations in the parser's hashing module.

    Patching `factlattice.hashing.file_content_hash` catches the FULL-corpus re-hash
    path (`files_hash` -> `file_hash_map` -> `file_content_hash`, resolved from module
    globals at call time) — i.e. exactly the O(corpus) tax this phase removes.
    """

    def __init__(self, monkeypatch):
        self.calls = 0
        real = _hashing.file_content_hash

        def counting(path):
            self.calls += 1
            return real(path)

        monkeypatch.setattr(_hashing, "file_content_hash", counting)

    def reset(self):
        self.calls = 0


# ===========================================================================
# Parity — the maintained root MUST be byte-identical to the builder's oracle
# ===========================================================================
def test_combine_leaf_map_matches_files_hash(corpus):
    """The incremental recombine reproduces `files_hash` EXACTLY from the leaf map."""
    live, fmap = files_hash(str(corpus))
    assert combine_leaf_map(fmap) == live


def test_seated_root_equals_index_hash_single_root(corpus):
    part = build_partition("c", str(corpus), "proj.c")
    k = _seated(part)
    assert k.current_root(part) == part.files_hash
    assert part.freshness().fresh is True


def test_seated_root_equals_index_hash_multi_root(two_roots):
    a, b = two_roots
    part = build_merged_partition(
        "m", [{"path": str(a), "area": "decisions"}, {"path": str(b), "area": "specs"}],
        "proj.m")
    k = _seated(part)
    # parity with BOTH the stored index hash and the original full-hash oracle
    assert k.current_root(part) == part.files_hash
    assert k.current_root(part) == combined_files_hash(part.root_paths)
    assert part.freshness().fresh is True


def test_combine_roots_degenerates_for_one_root():
    assert combine_roots({"/x": "sha256:abc"}) == "sha256:abc"


# ===========================================================================
# (a) O(0) on the query path + a change flips STALE without a full re-hash
# ===========================================================================
def test_query_path_does_zero_per_file_hashing_when_fresh(corpus, monkeypatch):
    """THE O(0) PROOF. With the kernel seated, running the query surface repeatedly
    performs ZERO per-file content hashes — the per-query O(corpus-bytes) tax is gone."""
    part = build_partition("c", str(corpus), "proj.c")
    kernel = _seated(part)
    eng = _engine(part)

    seat_cost = kernel.hash_ops
    assert seat_cost == 3, "seating hashes each .lts leaf exactly once"

    spy = _HashSpy(monkeypatch)
    kernel.hash_ops = 0

    for _ in range(25):
        assert eng.find(corpus="c", prefix="G-")["success"] is True
        assert eng.node(corpus="c", node_id="G-A")["success"] is True
        assert eng.traverse(corpus="c", start_node="G-A", max_depth=2)["success"] is True
        assert eng.capabilities(corpus="c", node_id="G-A")["success"] is True
        assert eng.list_corpora()["success"] is True

    assert kernel.hash_ops == 0, "the query path must not re-hash any leaf"
    assert spy.calls == 0, "the query path must not touch the full-corpus hash oracle"


def test_query_path_without_kernel_rehashes_whole_corpus(corpus, monkeypatch):
    """The BASELINE this phase replaces: with no kernel, EVERY query re-hashes every
    file. This is the number the O(0) proof above is measured against."""
    part = build_partition("c", str(corpus), "proj.c")
    eng = _engine(part)
    spy = _HashSpy(monkeypatch)

    n_queries = 10
    for _ in range(n_queries):
        eng.find(corpus="c", prefix="G-")

    assert spy.calls == n_queries * 3, "baseline: one full corpus re-hash per query"


def test_change_flips_stale_rehashing_only_the_changed_leaf(corpus):
    """(a) A changed file flips freshness to STALE, and the incremental update
    re-hashes ONLY that leaf — not the corpus."""
    part = build_partition("c", str(corpus), "proj.c")
    kernel = _seated(part)
    assert part.freshness().fresh is True

    kernel.hash_ops = 0
    (corpus / "a.lts").write_text(_lts("A", "alpha-EDITED"), encoding="utf-8")

    assert kernel.poll_once(part) is True
    assert kernel.hash_ops == 1, \
        "only the changed leaf may be re-hashed (corpus has 3 files)"

    fr = part.freshness()
    assert fr.fresh is False
    assert fr.index_hash == part.files_hash and fr.live_hash != part.files_hash


def test_stale_is_a_loud_marker_never_a_wrong_answer(corpus):
    """[INV-LTS-FRESH]: a stale corpus yields a LOUD stale marker through the tool
    surface, never a silently-wrong result."""
    part = build_partition("c", str(corpus), "proj.c")
    kernel = _seated(part)
    eng = _engine(part)
    assert eng.find(corpus="c", prefix="G-")["success"] is True

    (corpus / "graph.lts").write_text(GRAPH_LTS + "\n### [G-D] Delta\n- definition: d\n",
                                      encoding="utf-8")
    kernel.poll_once(part)

    for res in (eng.find(corpus="c", prefix="G-"),
                eng.node(corpus="c", node_id="G-A"),
                eng.traverse(corpus="c", start_node="G-A", max_depth=2),
                eng.capabilities(corpus="c", node_id="G-A")):
        assert res["success"] is False
        assert res["stale"] is True
        assert "STALE" in res["error"] and "INV-LTS-FRESH" in res["error"]


def test_incremental_update_catches_added_and_removed_files(corpus):
    part = build_partition("c", str(corpus), "proj.c")
    kernel = _seated(part)
    seated_root = kernel.current_root(part)

    (corpus / "new.lts").write_text(_lts("N", "new"), encoding="utf-8")
    assert kernel.poll_once(part) is True
    assert part.freshness().fresh is False

    os.unlink(corpus / "new.lts")
    assert kernel.poll_once(part) is True
    assert kernel.current_root(part) == seated_root, \
        "removing the added file restores the prior content set (and the root)"
    assert part.freshness().fresh is True


def test_incremental_update_in_non_first_root(two_roots):
    """The [ENH-3] guarantee survives incrementally: a change in the SECOND root of a
    merged partition still flips the maintained root."""
    a, b = two_roots
    part = build_merged_partition(
        "m", [{"path": str(a), "area": "decisions"}, {"path": str(b), "area": "specs"}],
        "proj.m")
    kernel = _seated(part)
    assert part.freshness().fresh is True

    kernel.hash_ops = 0
    (b / "s1.lts").write_text(_lts("S1", "spec one CHANGED"), encoding="utf-8")
    assert kernel.poll_once(part) is True
    assert kernel.hash_ops == 1, "only the one changed leaf, across BOTH roots"
    assert part.freshness().fresh is False


# ===========================================================================
# (b) the periodic full re-verify BACKSTOP
# ===========================================================================
def _rewrite_preserving_mtime_and_size(path: str, new_text: str) -> None:
    """Replace a file's bytes while restoring its exact mtime AND keeping its size —
    the change class an mtime/size hint is structurally blind to (`touch -r`, `cp -p`,
    clock skew, some restore tools)."""
    st = os.stat(path)
    old_size = st.st_size
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    assert os.stat(path).st_size == old_size, "test setup: sizes must match"
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.stat(path).st_mtime_ns == st.st_mtime_ns


def test_backstop_catches_change_the_watch_path_missed(corpus):
    """(b) A hint-invisible edit is MISSED by the fast poll but CAUGHT by the periodic
    full re-verify — so a missed/unavailable fs event can never leave the server
    silently serving stale."""
    part = build_partition("c", str(corpus), "proj.c")
    kernel = _seated(part)
    assert part.freshness().fresh is True

    path = str(corpus / "a.lts")
    _rewrite_preserving_mtime_and_size(path, _lts("A", "alphX"))  # same length as "alpha"

    # The fast, hint-driven path is structurally blind to this.
    assert kernel.poll_once(part) is False
    assert part.freshness().fresh is True, "documented bounded window before the backstop"

    # The backstop re-hashes CONTENT unconditionally and catches it.
    assert kernel.full_reverify(part) is True
    assert part.freshness().fresh is False


def test_backstop_runs_on_schedule_via_tick(corpus):
    """The watcher's tick loop runs the full re-verify every `backstop_every` ticks."""
    part = build_partition("c", str(corpus), "proj.c")
    kernel = FreshnessKernel(backstop_every=5)
    kernel.seat_partition(part)

    _rewrite_preserving_mtime_and_size(str(corpus / "a.lts"), _lts("A", "alphX"))

    for _ in range(4):          # ticks 1..4: fast polls only -> still (wrongly) fresh
        kernel.tick()
    assert part.freshness().fresh is True

    kernel.tick()               # tick 5: backstop fires
    assert part.freshness().fresh is False, "the periodic backstop must catch it"


def test_backstop_is_the_fallback_when_watch_is_unavailable(corpus, monkeypatch):
    """If the watch path is entirely unavailable (never called / raises), the backstop
    alone still catches an ordinary content change — never silently stale."""
    part = build_partition("c", str(corpus), "proj.c")
    kernel = _seated(part)

    def _broken_poll(_part):
        raise OSError("fs watch unavailable")

    monkeypatch.setattr(kernel, "poll_once", _broken_poll)
    (corpus / "b.lts").write_text(_lts("B", "beta-EDITED"), encoding="utf-8")

    assert part.freshness().fresh is True     # watch never observed it
    assert kernel.full_reverify(part) is True
    assert part.freshness().fresh is False


def test_unreadable_root_fails_closed(corpus):
    """An unverifiable root must report NOT-fresh (fail-closed), never silently fresh."""
    part = build_partition("c", str(corpus), "proj.c")
    kernel = _seated(part)
    assert part.freshness().fresh is True

    for name in ("graph.lts", "a.lts", "b.lts"):
        os.unlink(corpus / name)
    os.rmdir(corpus)

    kernel.poll_once(part)
    assert part.freshness().fresh is False


# ===========================================================================
# (c) DETERMINISM — stable root, no false-stale
# ===========================================================================
def test_unchanged_corpus_is_deterministic_and_never_false_stale(corpus):
    part = build_partition("c", str(corpus), "proj.c")
    kernel = _seated(part)
    root0 = kernel.current_root(part)

    for _ in range(10):
        assert kernel.poll_once(part) is False
        assert kernel.current_root(part) == root0
        assert part.freshness().fresh is True

    assert kernel.full_reverify(part) is False, "a full re-verify must not move the root"
    assert kernel.current_root(part) == root0


def test_two_independent_seats_agree(corpus):
    """Same corpus -> same root, regardless of which kernel instance seated it."""
    p1 = build_partition("c1", str(corpus), "proj.c")
    p2 = build_partition("c2", str(corpus), "proj.c")
    assert _seated(p1).current_root(p1) == _seated(p2).current_root(p2)


def test_mtime_touch_with_identical_content_is_not_stale(corpus):
    """(c) mtime is only a HINT. A content-preserving touch re-hashes that one leaf,
    finds the SAME content hash, and does NOT flip freshness — no false-stale."""
    part = build_partition("c", str(corpus), "proj.c")
    kernel = _seated(part)
    root0 = kernel.current_root(part)

    future = os.stat(corpus / "a.lts").st_mtime + 365 * 24 * 3600
    os.utime(corpus / "a.lts", (future, future))

    kernel.hash_ops = 0
    assert kernel.poll_once(part) is False, "content is unchanged -> root must not move"
    assert kernel.hash_ops == 1, "the hint sent exactly one leaf to the content oracle"
    assert kernel.current_root(part) == root0
    assert part.freshness().fresh is True


def test_full_reverify_is_also_immune_to_mtime(corpus):
    part = build_partition("c", str(corpus), "proj.c")
    kernel = _seated(part)
    root0 = kernel.current_root(part)
    future = os.stat(corpus / "b.lts").st_mtime + 1000
    os.utime(corpus / "b.lts", (future, future))
    assert kernel.full_reverify(part) is False
    assert kernel.current_root(part) == root0


def test_unseated_partition_falls_back_to_full_rehash(corpus):
    """A partition with a kernel attached but NOT seated (or no kernel at all) keeps the
    original full-re-hash behaviour — the fallback is never a false-fresh."""
    part = build_partition("c", str(corpus), "proj.c")
    assert part.freshness().fresh is True            # no kernel: full re-hash path
    (corpus / "a.lts").write_text(_lts("A", "changed"), encoding="utf-8")
    assert part.freshness().fresh is False


# ===========================================================================
# (d) the ADJACENCY cache
# ===========================================================================
def test_adjacency_is_built_once_and_reused_while_fresh(corpus):
    part = build_partition("c", str(corpus), "proj.c")
    _seated(part)

    assert part._adj_builds == 0
    fwd1, rev1 = part.adjacency()
    assert part._adj_builds == 1
    for _ in range(10):
        fwd2, rev2 = part.adjacency()
    assert part._adj_builds == 1, "adjacency must be reused while the root is unchanged"
    assert fwd2 is fwd1 and rev2 is rev1

    # and it is a real incidence map (G-DOC CONTAINS each section node)
    assert {e["target"] for _l, e in fwd1["G-A"]} == {"G-B", "G-C"}
    assert {e["source"] for _l, e in rev1["G-C"]} == {"G-B", "G-A", "G-DOC"}
    assert {e["target"] for _l, e in fwd1["G-DOC"]} == {"G-A", "G-B", "G-C"}


def test_adjacency_invalidates_on_a_real_content_change(corpus):
    part = build_partition("c", str(corpus), "proj.c")
    kernel = _seated(part)
    part.adjacency()
    assert part._adj_builds == 1

    (corpus / "a.lts").write_text(_lts("A", "alpha-EDITED"), encoding="utf-8")
    assert kernel.poll_once(part) is True     # the maintained root moved

    part.adjacency()
    assert part._adj_builds == 2, "a real content change must invalidate the cache"
    part.adjacency()
    assert part._adj_builds == 2, "and it is cached again at the new root"


def test_adjacency_not_invalidated_by_an_mtime_only_touch(corpus):
    part = build_partition("c", str(corpus), "proj.c")
    kernel = _seated(part)
    part.adjacency()
    assert part._adj_builds == 1

    future = os.stat(corpus / "a.lts").st_mtime + 5000
    os.utime(corpus / "a.lts", (future, future))
    kernel.poll_once(part)

    part.adjacency()
    assert part._adj_builds == 1, "an mtime-only touch is not a content change"


def test_traverse_via_adjacency_matches_a_bruteforce_walk(corpus):
    """The adjacency rewrite must not change traverse's ANSWERS: compare against a
    brute-force all-edges scan over the same partition."""
    part = build_partition("c", str(corpus), "proj.c")
    _seated(part)
    eng = _engine(part)

    for direction in ("forward", "reverse", "both"):
        for depth in (1, 2, 3):
            res = eng.traverse(corpus="c", start_node="G-A", direction=direction,
                               max_depth=depth)
            got = {n["id"] for n in res["nodes"]}
            assert got == _bruteforce_walk(part, "G-A", direction, depth)


def _bruteforce_walk(part, start, direction, max_depth):
    """Reference BFS that rescans EVERY edge per frontier node (the pre-cache shape)."""
    visited = {start}
    frontier = [(start, 0)]
    while frontier:
        nid, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        for _layer, ldata in part.layers.items():
            for edge in ldata.get("edges", []):
                fwd = edge["source"] == nid and direction in ("forward", "both")
                rev = edge["target"] == nid and direction in ("reverse", "both")
                if not (fwd or rev):
                    continue
                nb = edge["target"] if fwd else edge["source"]
                if nb in visited or not part.has_node(nb):
                    continue
                visited.add(nb)
                frontier.append((nb, depth + 1))
    return visited


def test_traverse_layer_filter_still_applies_through_the_cache(corpus):
    part = build_partition("c", str(corpus), "proj.c")
    _seated(part)
    eng = _engine(part)
    res = eng.traverse(corpus="c", start_node="G-A", layers=["temporal"],
                       direction="forward", max_depth=2)
    assert {n["id"] for n in res["nodes"]} == {"G-A", "G-C"}  # semantic G-B excluded


def test_analytics_degree_index_is_cached_and_invalidated(corpus):
    """The ANALYTICS layer's Tier-2 ops used to re-scan every edge per call (a TODO the
    layer flagged FOR this phase). They now read a freshness-keyed cached histogram."""
    from factlattice_engine.analytics import LtsAggregator

    part = build_partition("c", str(corpus), "proj.c")
    kernel = _seated(part)
    agg = LtsAggregator(_engine(part))

    r1 = agg.count_by_edge_type(corpus="c")
    assert r1["success"] is True
    builds_after_first = part._deg_builds
    assert builds_after_first == 1

    for _ in range(5):
        assert agg.count_by_edge_type(corpus="c")["resolved_edges"] == r1["resolved_edges"]
        assert agg.degree("G-A", corpus="c")["success"] is True
    assert part._deg_builds == 1, "histogram must be reused while the corpus is fresh"

    # a real change moves the root -> the histogram key is invalid
    (corpus / "a.lts").write_text(_lts("A", "alpha-EDITED"), encoding="utf-8")
    kernel.poll_once(part)
    part.degree_index()
    assert part._deg_builds == 2


def test_analytics_degree_index_matches_a_bruteforce_scan(corpus):
    """The cache must not change the ANSWERS: compare to the original per-call scan."""
    from factlattice_engine.analytics import LtsAggregator

    part = build_partition("c", str(corpus), "proj.c")
    _seated(part)
    di = part.degree_index()

    in_deg, out_deg, etypes, total, resolved = {}, {}, {}, 0, 0
    for e in part.iter_edges():
        total += 1
        if e.get("dangling") or "unresolved_target" in (e.get("flags") or ()):
            continue
        resolved += 1
        etypes[e.get("type") or "(untyped)"] = etypes.get(e.get("type") or "(untyped)", 0) + 1
        out_deg[e["source"]] = out_deg.get(e["source"], 0) + 1
        in_deg[e["target"]] = in_deg.get(e["target"], 0) + 1

    assert di["in_deg"] == in_deg and di["out_deg"] == out_deg
    assert di["edge_types"] == etypes
    assert di["total_edges"] == total and di["resolved_edges"] == resolved

    agg = LtsAggregator(_engine(part))
    assert agg.count_by_edge_type(corpus="c")["resolved_edges"] == resolved


def test_analytics_is_still_freshness_gated(corpus):
    """A STALE corpus must still produce the loud marker through the ANALYTICS layer."""
    from factlattice_engine.analytics import LtsAggregator

    part = build_partition("c", str(corpus), "proj.c")
    kernel = _seated(part)
    agg = LtsAggregator(_engine(part))
    assert agg.count_by_edge_type(corpus="c")["success"] is True

    (corpus / "a.lts").write_text(_lts("A", "alpha-EDITED"), encoding="utf-8")
    kernel.poll_once(part)
    res = agg.count_by_edge_type(corpus="c")
    assert res["success"] is False and res.get("stale") is True


# ===========================================================================
# store-level seating + the server wiring
# ===========================================================================
def test_seat_store_seats_every_partition(corpus, two_roots):
    a, b = two_roots
    p1 = build_partition("c", str(corpus), "proj.c")
    p2 = build_merged_partition(
        "m", [{"path": str(a), "area": "decisions"}, {"path": str(b), "area": "specs"}],
        "proj.m")
    store = LtsStore()
    store.add_partition(p1)
    store.add_partition(p2)

    kernel = FreshnessKernel()
    kernel.seat_store(store)
    for p in store.partitions():
        assert kernel.current_root(p) == p.files_hash
        assert p.freshness().fresh is True


def test_kernel_start_stop_is_safe(corpus):
    part = build_partition("c", str(corpus), "proj.c")
    kernel = FreshnessKernel(poll_interval=0.01, backstop_every=2)
    kernel.seat_partition(part)
    kernel.start()
    kernel.start()          # idempotent
    kernel.stop()
    assert part.freshness().fresh is True


def test_partition_with_no_roots_is_not_seated():
    from factlattice_engine import Partition
    part = Partition({"source_corpus": {"corpus_id": "syn", "domain_suffix": "s",
                                        "files_hash": "sha256:test", "root_paths": []},
                      "nodes": {}, "layers": {}, "stats": {}})
    kernel = FreshnessKernel()
    assert kernel.seat_partition(part) is None
    assert kernel.current_root(part) is None
    fr = part.freshness()
    assert fr.fresh is False and fr.reason == "no-root-paths"
