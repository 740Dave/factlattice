"""[LEC-SELF-HEAL] — tests that a STALE verdict is a TRANSIENT state, not a terminal one.

The acceptance bar, and where each item is proven:

  (a) AVAILABILITY   — edit a file, and the NEXT query returns the FRESH answer (not a
                       refusal), reflecting the edit.
                       -> test_edit_then_query_serves_the_fresh_answer

  (b) O(0) PRESERVED — after the heal, steady-state queries do ZERO per-file hashing
                       (phase-2's property survives), asserted with the kernel's
                       `hash_ops` counter and a spy on the parser's `file_content_hash`.
                       -> test_o0_property_is_restored_after_the_heal

  (c) FAIL CLOSED    — a rebuild that raises still yields the LOUD stale marker, never
                       stale data, and the process survives.
                       -> test_rebuild_failure_fails_closed_*

  (d) NO STORMS      — N gated calls across ONE root change trigger exactly ONE rebuild;
                       concurrent callers COALESCE onto it.
                       -> test_n_gated_calls_across_one_change_rebuild_once
                          test_concurrent_callers_coalesce_onto_one_rebuild

  (e) DETERMINISM    — a healed index answers IDENTICALLY to a cold-start index over the
                       same content, including after id add/remove.
                       -> test_healed_index_matches_cold_start_*

  SURGICAL           — the re-index re-parses ONLY the changed file(s), asserted with the
                       `parse_ops` counter (the parse-side analogue of `hash_ops`).
                       -> test_content_edit_reparses_only_the_changed_file

  CROSS-FILE         — removing / adding an id in file A correctly re-resolves edges that
                       live in untouched file B.
                       -> test_removed_node_re_dangles_edges_from_untouched_files
                          test_added_node_resolves_a_previously_dangling_edge

  WATCH LAG          — the hole this phase found and closed: phase-2's gate compares
                       against the MAINTAINED root, which only advances on a watcher
                       poll, so a write landing between polls was answered FRESH over
                       PRE-WRITE content. `PollVerifiedFreshness` (wired by
                       `server.build_tools`) verifies with a stat-only hint pass first.
                       -> test_phase2_gate_alone_serves_a_write_that_lands_between_polls
                          test_verify_on_query_closes_the_lag_window
                          test_verify_on_query_still_does_zero_content_hashing_when_fresh
"""

from __future__ import annotations

import os
import threading

import pytest

import factlattice.hashing as _hashing
from factlattice import build_index
from factlattice_engine import (
    ContentHashFreshness, FreshnessKernel, LtsEngine, LtsStore,
    PollVerifiedFreshness, SelfHealer,
)
from factlattice_engine.config import build_merged_partition, build_partition
from factlattice_engine.incremental import CorpusIndex, CorpusMoved, RootIndex


# ===========================================================================
# fixtures — real files on disk (the whole point is re-reading them)
# ===========================================================================
def _lts(node_id: str, definition: str, extra: str = "") -> str:
    return (
        "---\ndomain: core.example\n---\n"
        f"# Test [SPEC:{node_id}-DOC]\n\n"
        f"### [{node_id}] Node {node_id}\n- definition: {definition}\n{extra}"
    )


@pytest.fixture
def corpus(tmp_path):
    """A single-root corpus of 6 .lts files."""
    root = tmp_path / "corpus"
    root.mkdir()
    for i in range(6):
        (root / f"n{i}.lts").write_text(_lts(f"N{i}", f"node {i}"), encoding="utf-8")
    return root


@pytest.fixture
def project(tmp_path):
    """The DEPLOYMENT shape: a merged corpus of a pinned decision graph + a specs dir.

    This is the shape that actually goes dark today — every `decision_add` moves the
    decisions root and blinds every query against the project.
    """
    dec = tmp_path / "decisions"
    specs = tmp_path / "specs"
    dec.mkdir()
    specs.mkdir()
    (dec / "decision-graph-p.lts").write_text(
        "---\ndomain: core.p\n---\n# D [SYSTEM:D-DOC]\n\n"
        "### [DEC-1] One\n- definition: one\n", encoding="utf-8")
    (dec / "decision-graph-other.lts").write_text(
        "---\ndomain: core.o\n---\n# O [SYSTEM:O-DOC]\n\n"
        "### [OTH-1] Other\n- definition: other\n", encoding="utf-8")
    for i in range(12):
        (specs / f"s{i}.lts").write_text(_lts(f"SPC{i}", f"spec {i}"), encoding="utf-8")
    return dec, specs


def _roots(dec, specs):
    return [{"path": str(dec), "area": "decisions",
             "only_file": "decision-graph-p.lts"},
            {"path": str(specs), "area": "specs"}]


def _healing_engine(part):
    """Store + seated kernel + healer + engine — the `server.build_tools()` wiring."""
    store = LtsStore()
    store.add_partition(part)
    kernel = FreshnessKernel()
    kernel.seat_store(store)
    store.kernel = kernel
    healer = SelfHealer(store, kernel=kernel)
    return LtsEngine(store, healer=healer), kernel, healer


class _HashSpy:
    """Counts per-file CONTENT hashes on whatever path invokes them (phase-2's instrument)."""

    def __init__(self, monkeypatch):
        self.calls = 0
        real = _hashing.file_content_hash

        def counting(path: str) -> str:
            self.calls += 1
            return real(path)

        monkeypatch.setattr(_hashing, "file_content_hash", counting)


# ===========================================================================
# (a) AVAILABILITY — the headline: stale is transient
# ===========================================================================
def test_edit_then_query_serves_the_fresh_answer(project):
    """A write to the decision graph must NOT blind the writer: the next query returns
    the fresh answer, including the node just written."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    eng, kernel, healer = _healing_engine(part)

    assert [n["id"] for n in eng.find(corpus="p", prefix="DEC-")["nodes"]] == ["DEC-1"]

    # the write (what `decision_add` does)
    with open(dec / "decision-graph-p.lts", "a", encoding="utf-8") as fh:
        fh.write("\n### [DEC-2] Two\n- definition: two\n- depends-on: [DEC-1]\n")
    kernel.poll_once(part)
    assert part.freshness().fresh is False, "phase-2 must still detect the change"

    res = eng.find(corpus="p", prefix="DEC-")
    assert res["success"] is True and "stale" not in res
    assert [n["id"] for n in res["nodes"]] == ["DEC-1", "DEC-2"]
    assert healer.rebuilds == 1 and healer.healed == 1


def test_every_gated_surface_heals_not_just_find(project):
    """The choke point is `_gate`, so node/traverse/capabilities inherit the heal."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    eng, kernel, healer = _healing_engine(part)
    eng.find(corpus="p")

    with open(dec / "decision-graph-p.lts", "a", encoding="utf-8") as fh:
        fh.write("\n### [DEC-2] Two\n- definition: two\n- depends-on: [DEC-1]\n")
    kernel.poll_once(part)

    for res in (eng.node(corpus="p", node_id="DEC-2"),
                eng.traverse(corpus="p", start_node="DEC-2", max_depth=2),
                eng.capabilities(corpus="p", node_id="DEC-2"),
                eng.find(corpus="p", prefix="DEC-")):
        assert res["success"] is True, res
        assert res.get("stale") is not True
    assert eng.node(corpus="p", node_id="DEC-2")["node"]["title"] == "Two"


def test_analytics_layer_heals_through_the_same_choke_point(project):
    """ANALYTICS folds over `find`, so it inherits the heal with no code of its own."""
    from factlattice_engine.analytics import LtsAggregator

    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    eng, kernel, healer = _healing_engine(part)
    agg = LtsAggregator(eng)
    assert agg.count(corpus="p", prefix="DEC-")["total"] == 1

    with open(dec / "decision-graph-p.lts", "a", encoding="utf-8") as fh:
        fh.write("\n### [DEC-2] Two\n- definition: two\n")
    kernel.poll_once(part)

    res = agg.count(corpus="p", prefix="DEC-")
    assert res["success"] is True and res["total"] == 2
    assert healer.rebuilds == 1


def test_unscoped_find_heals_every_stale_corpus(project, corpus):
    """The multi-corpus arm of `find` goes through the SAME gate — a stale corpus is
    healed, not silently dropped into `stale_corpora` forever."""
    dec, specs = project
    p1 = build_merged_partition("p", _roots(dec, specs), "proj.p")
    p2 = build_partition("c", str(corpus), "proj.c")
    store = LtsStore()
    store.add_partition(p1)
    store.add_partition(p2)
    kernel = FreshnessKernel()
    kernel.seat_store(store)
    store.kernel = kernel
    eng = LtsEngine(store, healer=SelfHealer(store, kernel=kernel))

    (corpus / "n0.lts").write_text(_lts("N0", "node 0 EDITED"), encoding="utf-8")
    with open(dec / "decision-graph-p.lts", "a", encoding="utf-8") as fh:
        fh.write("\n### [DEC-2] Two\n- definition: two\n")
    kernel.tick()

    res = eng.find()
    assert res["stale_corpora"] == []
    ids = {n["id"] for n in res["nodes"]}
    assert "DEC-2" in ids and "N0" in ids


# ===========================================================================
# SURGICAL — only the changed file is re-parsed
# ===========================================================================
def test_content_edit_reparses_only_the_changed_file(project):
    """The `decision_update` shape: properties edited, node ids unchanged. Exactly ONE
    of the corpus's 15 files may be re-parsed."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    eng, kernel, healer = _healing_engine(part)
    ci = part.corpus_index
    total_files = ci.cached_file_count
    assert total_files == 14, "2 decision files + 12 specs"

    ci.reset_parse_ops()
    (specs / "s7.lts").write_text(_lts("SPC7", "spec 7 REVISED"), encoding="utf-8")
    kernel.poll_once(part)

    res = eng.find(corpus="p", prefix="SPC7")
    assert res["success"] is True
    assert res["nodes"][0]["properties"]["definition"] == "spec 7 REVISED"
    assert ci.parse_ops == 1, \
        f"only the changed file may be re-parsed (corpus has {total_files})"
    assert ci.incremental_rebuilds == 1 and ci.full_rebuilds == 0


def test_added_and_removed_files_are_surgical_too(project):
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    eng, kernel, healer = _healing_engine(part)
    ci = part.corpus_index

    ci.reset_parse_ops()
    (specs / "new.lts").write_text(_lts("SPCNEW", "brand new"), encoding="utf-8")
    kernel.poll_once(part)
    got = {n["id"] for n in eng.find(corpus="p", prefix="SPCNEW")["nodes"]}
    assert got == {"SPCNEW", "SPCNEW-DOC"}
    assert ci.parse_ops == 1, "an ADDED file costs one parse, not a corpus re-parse"

    ci.reset_parse_ops()
    os.unlink(specs / "new.lts")
    kernel.poll_once(part)
    assert eng.find(corpus="p", prefix="SPCNEW")["count"] == 0
    assert ci.parse_ops == 0, "a REMOVED file costs zero parses"


# ===========================================================================
# CROSS-FILE re-resolution — the failure mode that must never go silent
# ===========================================================================
def _linked_corpus(tmp_path):
    """File A defines a target; file B points at it. Editing A must correctly re-resolve
    B's edge even though B is never re-parsed."""
    root = tmp_path / "linked"
    root.mkdir()
    (root / "a.lts").write_text(
        "---\ndomain: core.l\n---\n# A [SYSTEM:A-DOC]\n\n"
        "### [L-TARGET] Target\n- definition: target\n", encoding="utf-8")
    (root / "b.lts").write_text(
        "---\ndomain: core.l\n---\n# B [SYSTEM:B-DOC]\n\n"
        "### [L-SOURCE] Source\n- definition: source\n- depends-on: [L-TARGET]\n",
        encoding="utf-8")
    return root


def _edge(part, source, target):
    for layer in part.layers.values():
        for e in layer["edges"]:
            if e["source"] == source and e["target"] == target:
                return e
    return None


def test_removed_node_re_dangles_edges_from_untouched_files(tmp_path):
    """A node REMOVED in file A leaves file B's edge pointing at nothing. The heal must
    re-flag it dangling even though B was not re-parsed — the silent-wrongness case."""
    root = _linked_corpus(tmp_path)
    part = build_partition("l", str(root), "proj.l")
    eng, kernel, healer = _healing_engine(part)
    part.corpus_index.cold_build(as_rebuild=False)          # seed so the heal is surgical
    assert _edge(part, "L-SOURCE", "L-TARGET")["dangling"] is False

    part.corpus_index.reset_parse_ops()
    (root / "a.lts").write_text(
        "---\ndomain: core.l\n---\n# A [SYSTEM:A-DOC]\n\n"
        "### [L-OTHER] Other\n- definition: other\n", encoding="utf-8")
    kernel.poll_once(part)
    assert eng.find(corpus="l")["success"] is True          # heals

    assert part.corpus_index.parse_ops == 1, "only A re-parsed"
    edge = _edge(part, "L-SOURCE", "L-TARGET")
    assert edge is not None and edge["dangling"] is True, \
        "B's edge must be re-flagged dangling although B was never re-parsed"
    cold = build_index(corpus_root=str(root), corpus_id="l", domain_suffix="proj.l",
                       built_at="t")
    assert part.nodes == cold["nodes"] and part.layers == cold["layers"]


def test_added_node_resolves_a_previously_dangling_edge(tmp_path):
    """The mirror case: an id ADDED in A must un-dangle B's edge."""
    root = tmp_path / "linked"
    root.mkdir()
    (root / "a.lts").write_text(
        "---\ndomain: core.l\n---\n# A [SYSTEM:A-DOC]\n\n"
        "### [L-OTHER] Other\n- definition: other\n", encoding="utf-8")
    (root / "b.lts").write_text(
        "---\ndomain: core.l\n---\n# B [SYSTEM:B-DOC]\n\n"
        "### [L-SOURCE] Source\n- definition: source\n- depends-on: [L-TARGET]\n",
        encoding="utf-8")
    part = build_partition("l", str(root), "proj.l")
    eng, kernel, healer = _healing_engine(part)
    part.corpus_index.cold_build(as_rebuild=False)
    assert _edge(part, "L-SOURCE", "L-TARGET")["dangling"] is True

    part.corpus_index.reset_parse_ops()
    with open(root / "a.lts", "a", encoding="utf-8") as fh:
        fh.write("\n### [L-TARGET] Target\n- definition: target\n")
    kernel.poll_once(part)
    assert eng.find(corpus="l")["success"] is True

    assert part.corpus_index.parse_ops == 1
    assert _edge(part, "L-SOURCE", "L-TARGET")["dangling"] is False, \
        "B's edge must become resolved although B was never re-parsed"


# ===========================================================================
# (e) DETERMINISM — healed == cold start
# ===========================================================================
def test_root_index_assemble_matches_build_index_exactly(corpus):
    """The parity that makes the whole splice safe: the incremental fold reproduces the
    parser's own `build_index` byte-for-byte."""
    ri = RootIndex(str(corpus))
    ri.full_build()
    cold = build_index(corpus_root=str(corpus), corpus_id="c",
                       domain_suffix="proj.c", built_at="t")
    got = ri.assemble()
    for key in ("nodes", "layers", "alias_index", "id_collisions", "parse_errors",
                "stats"):
        assert got[key] == cold[key], key
    from factlattice import files_hash
    assert ri.root_hash() == files_hash(str(corpus))[0]


@pytest.mark.parametrize("mutation", ["content", "add_id", "remove_id", "add_file"])
def test_healed_index_matches_cold_start_after_any_mutation(project, mutation):
    """(e) A healed index answers IDENTICALLY to a cold-start index over the same
    content — the safety net for the incremental splice, across every mutation class."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    eng, kernel, healer = _healing_engine(part)

    graph = dec / "decision-graph-p.lts"
    if mutation == "content":
        graph.write_text("---\ndomain: core.p\n---\n# D [SYSTEM:D-DOC]\n\n"
                         "### [DEC-1] One\n- definition: one REVISED\n", encoding="utf-8")
    elif mutation == "add_id":
        with open(graph, "a", encoding="utf-8") as fh:
            fh.write("\n### [DEC-2] Two\n- definition: two\n- supersedes: [DEC-1]\n")
    elif mutation == "remove_id":
        (specs / "s3.lts").write_text(
            "---\ndomain: core.example\n---\n# Test [SPEC:X-DOC]\n\n"
            "### [SPCX] Replaced\n- definition: replaced\n- see-also: [SPC4]\n",
            encoding="utf-8")
    else:
        (specs / "s99.lts").write_text(_lts("SPC99", "ninety nine"), encoding="utf-8")
    kernel.poll_once(part)

    healed = eng.find(corpus="p", token_budget=100000)
    assert healed["success"] is True

    cold_part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    cold_eng, _k, _h = _healing_engine(cold_part)
    cold = cold_eng.find(corpus="p", token_budget=100000)

    assert healed == cold
    assert part.nodes == cold_part.nodes
    assert part.layers == cold_part.layers
    assert part.files_hash == cold_part.files_hash
    for nid in ("DEC-1", "SPC4"):
        if nid in cold_part.nodes:
            assert eng.node(corpus="p", node_id=nid) == \
                cold_eng.node(corpus="p", node_id=nid)
            assert eng.traverse(corpus="p", start_node=nid, max_depth=3) == \
                cold_eng.traverse(corpus="p", start_node=nid, max_depth=3)


# ===========================================================================
# (b) O(0) PRESERVED
# ===========================================================================
def test_o0_property_is_restored_after_the_heal(project, monkeypatch):
    """Phase-2's headline property must survive the heal: once healed, a steady-state
    fresh corpus does ZERO per-file hashing on the query path."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    eng, kernel, healer = _healing_engine(part)

    with open(dec / "decision-graph-p.lts", "a", encoding="utf-8") as fh:
        fh.write("\n### [DEC-2] Two\n- definition: two\n")
    kernel.poll_once(part)
    assert eng.find(corpus="p", prefix="DEC-")["count"] == 2      # heals

    spy = _HashSpy(monkeypatch)
    kernel.hash_ops = 0
    for i in range(25):
        assert eng.find(corpus="p", prefix="DEC-")["success"] is True
        eng.node(corpus="p", node_id="DEC-1")
        eng.traverse(corpus="p", start_node="DEC-1", max_depth=2)
    assert kernel.hash_ops == 0, "the healed partition must be O(0) on the query path"
    assert spy.calls == 0, "no per-file content hash may occur on a fresh query path"


def test_heal_does_not_re_seat_the_kernel_with_a_full_rehash(project):
    """The heal REBINDS the kernel rather than re-seating it: the leaves it already holds
    are authoritative for the content just indexed, so a second full re-hash is waste."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    eng, kernel, healer = _healing_engine(part)
    n_files = part.corpus_index.cached_file_count

    (specs / "s2.lts").write_text(_lts("SPC2", "spec 2 EDITED"), encoding="utf-8")
    kernel.poll_once(part)
    kernel.hash_ops = 0
    assert eng.find(corpus="p", prefix="SPC2")["success"] is True
    # ONE authoritative full_reverify pass (n_files) + the changed-file re-hashes done by
    # the parse cache's before/after certification + the confirming stat-only poll.
    assert kernel.hash_ops <= n_files + 2, \
        f"a heal must cost about one content pass, not several ({kernel.hash_ops})"


# ===========================================================================
# (c) FAIL CLOSED
# ===========================================================================
def test_rebuild_failure_fails_closed_and_the_server_survives(project, monkeypatch):
    """A rebuild that RAISES must still yield the loud stale marker, must not publish a
    partial index, and must not propagate."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    eng, kernel, healer = _healing_engine(part)
    before_nodes = dict(part.nodes)
    before_hash = part.files_hash

    with open(dec / "decision-graph-p.lts", "a", encoding="utf-8") as fh:
        fh.write("\n### [DEC-2] Two\n- definition: two\n")
    kernel.poll_once(part)

    def boom(self, *a, **kw):
        raise RuntimeError("simulated corrupt corpus")

    monkeypatch.setattr(CorpusIndex, "incremental_build", boom)
    monkeypatch.setattr(CorpusIndex, "cold_build", boom)

    res = eng.find(corpus="p", prefix="DEC-")
    assert res["success"] is False and res["stale"] is True
    assert "INV-LTS-FRESH" in res["error"]
    assert res["heal"]["attempted"] is True and res["heal"]["healed"] is False
    assert "simulated corrupt corpus" in res["heal"]["error"]
    assert part.nodes == before_nodes and part.files_hash == before_hash, \
        "a failed rebuild must publish NOTHING"
    assert healer.failures == 1


def test_unreadable_root_fails_closed(project):
    """A root that vanishes mid-session is UNVERIFIABLE — refuse, never serve."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    eng, kernel, healer = _healing_engine(part)

    for f in specs.iterdir():
        f.unlink()
    specs.rmdir()
    kernel.poll_once(part)

    res = eng.find(corpus="p", prefix="DEC-")
    assert res["success"] is False and res["stale"] is True


def test_corpus_moving_under_the_reindex_fails_closed(project, monkeypatch):
    """A file rewritten WHILE it is being re-parsed must abort the heal (CorpusMoved),
    not cache a parse it cannot attribute to any content hash."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    eng, kernel, healer = _healing_engine(part)
    before_hash = part.files_hash

    target = specs / "s1.lts"
    target.write_text(_lts("SPC1", "spec 1 EDITED"), encoding="utf-8")
    kernel.poll_once(part)

    # Rewrite the file DURING `RootIndex._parse`, between its before/after hashes: the
    # first hash of s1.lts triggers a further write, so the after-hash cannot match.
    real_hash = _hashing.file_content_hash
    seen = {"n": 0}

    def moving(path: str) -> str:
        h = real_hash(path)
        if path.endswith("s1.lts"):
            seen["n"] += 1
            if seen["n"] == 1:
                target.write_text(_lts("SPC1", "spec 1 MOVED AGAIN"), encoding="utf-8")
        return h

    monkeypatch.setattr(_hashing, "file_content_hash", moving)
    res = eng.find(corpus="p", prefix="SPC1")
    monkeypatch.undo()

    assert res["success"] is False and res["stale"] is True
    assert "changed while being re-indexed" in res["heal"]["error"], res["heal"]
    assert res["heal"]["healed"] is False
    assert part.files_hash == before_hash, "nothing published while the corpus moved"
    # and the engine recovers on the next call, once the corpus has settled
    kernel.poll_once(part)
    assert eng.find(corpus="p", prefix="SPC1")["success"] is True


def test_no_rebuild_recipe_keeps_the_phase_2_refusal(corpus):
    """A partition with no rebuild recipe (a raw loaded index) cannot heal — it must keep
    phase-2's loud refusal rather than pretend."""
    part = build_partition("c", str(corpus), "proj.c")
    part.corpus_index = None
    eng, kernel, healer = _healing_engine(part)

    (corpus / "n1.lts").write_text(_lts("N1", "node 1 EDITED"), encoding="utf-8")
    kernel.poll_once(part)

    res = eng.find(corpus="c")
    assert res["success"] is False and res["stale"] is True
    assert res["heal"]["attempted"] is False
    assert res["heal"]["reason"] == "no-rebuild-recipe" and "no rebuild recipe" in res["heal"]["explanation"]
    assert healer.rebuilds == 0


# ===========================================================================
# (d) NO REBUILD STORMS
# ===========================================================================
def test_n_gated_calls_across_one_change_rebuild_once(project):
    """(d) N gated calls spanning ONE root change trigger exactly ONE rebuild."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    eng, kernel, healer = _healing_engine(part)

    with open(dec / "decision-graph-p.lts", "a", encoding="utf-8") as fh:
        fh.write("\n### [DEC-2] Two\n- definition: two\n")
    kernel.poll_once(part)

    for _ in range(40):
        assert eng.find(corpus="p", prefix="DEC-")["success"] is True
        assert eng.node(corpus="p", node_id="DEC-2")["success"] is True
    assert healer.rebuilds == 1, "at most ONE rebuild per detected root change"
    assert part.corpus_index.parse_ops >= 1


def test_failed_rebuild_is_not_retried_until_the_content_moves(project, monkeypatch):
    """The storm guard's teeth: a rebuild that FAILS is attempted once per content state,
    not once per call. When the content moves again, exactly one more attempt is made."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    eng, kernel, healer = _healing_engine(part)

    def boom(self, *a, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(CorpusIndex, "incremental_build", boom)

    with open(dec / "decision-graph-p.lts", "a", encoding="utf-8") as fh:
        fh.write("\n### [DEC-2] Two\n- definition: two\n")
    kernel.poll_once(part)
    for _ in range(25):
        assert eng.find(corpus="p", prefix="DEC-")["success"] is False
    assert healer.rebuilds == 1 and healer.suppressed == 24

    with open(dec / "decision-graph-p.lts", "a", encoding="utf-8") as fh:
        fh.write("\n### [DEC-3] Three\n- definition: three\n")
    kernel.poll_once(part)
    for _ in range(10):
        eng.find(corpus="p", prefix="DEC-")
    assert healer.rebuilds == 2, "a NEW root change earns exactly one new attempt"


def test_concurrent_callers_coalesce_onto_one_rebuild(project):
    """(d) 12 threads hitting the gate across one root change must produce ONE rebuild;
    the losers find the partition already fresh and do no work."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    eng, kernel, healer = _healing_engine(part)

    with open(dec / "decision-graph-p.lts", "a", encoding="utf-8") as fh:
        fh.write("\n### [DEC-2] Two\n- definition: two\n")
    kernel.poll_once(part)

    start = threading.Barrier(12)
    results = []

    def worker():
        start.wait()
        results.append(eng.find(corpus="p", prefix="DEC-"))

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == 12
    assert all(r["success"] is True for r in results)
    assert all([n["id"] for n in r["nodes"]] == ["DEC-1", "DEC-2"] for r in results)
    assert healer.rebuilds == 1, "concurrent callers must coalesce, never stampede"


def test_cooldown_knob_suppresses_a_rapid_second_attempt(project, monkeypatch):
    """The optional wall-clock floor (for a pathologically continuous writer). Refusing
    during the cooldown is fail-closed-safe."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    store = LtsStore()
    store.add_partition(part)
    kernel = FreshnessKernel()
    kernel.seat_store(store)
    store.kernel = kernel
    now = [1000.0]
    healer = SelfHealer(store, kernel=kernel, min_interval=5.0, clock=lambda: now[0])
    eng = LtsEngine(store, healer=healer)

    def boom(self, *a, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(CorpusIndex, "incremental_build", boom)
    for i in (2, 3):
        with open(dec / "decision-graph-p.lts", "a", encoding="utf-8") as fh:
            fh.write(f"\n### [DEC-{i}] N{i}\n- definition: n{i}\n")
        kernel.poll_once(part)
        eng.find(corpus="p")
    assert healer.rebuilds == 1 and healer.suppressed == 1

    now[0] += 10.0
    eng.find(corpus="p")
    assert healer.rebuilds == 2, "past the cooldown a new content state is retried"


# ===========================================================================
# the FULL re-index fallback — exercised and observable
# ===========================================================================
def test_first_heal_of_an_unseeded_partition_falls_back_to_full(corpus):
    """`build_partition` builds through the parser's own `build_index` (it wants the full
    index schema), so there is no parse cache to inherit: the FIRST heal is a full
    re-index — observable as mode="full" — and every heal after it is surgical."""
    part = build_partition("c", str(corpus), "proj.c")
    eng, kernel, healer = _healing_engine(part)
    ci = part.corpus_index
    assert ci.seeded is False

    (corpus / "n1.lts").write_text(_lts("N1", "node 1 EDITED"), encoding="utf-8")
    kernel.poll_once(part)
    assert eng.find(corpus="c", prefix="N1")["success"] is True
    assert ci.full_rebuilds == 1 and ci.incremental_rebuilds == 0
    assert ci.parse_ops == 6, "the fallback re-parses the whole corpus"

    ci.reset_parse_ops()
    (corpus / "n2.lts").write_text(_lts("N2", "node 2 EDITED"), encoding="utf-8")
    kernel.poll_once(part)
    assert eng.find(corpus="c", prefix="N2")["success"] is True
    assert ci.incremental_rebuilds == 1 and ci.parse_ops == 1, \
        "after the fallback seeds the cache, heals are surgical again"


def test_force_full_env_knob_selects_the_full_reindex(project):
    """The escape hatch: `force_full` pins the fallback for the whole session."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    store = LtsStore()
    store.add_partition(part)
    kernel = FreshnessKernel()
    kernel.seat_store(store)
    store.kernel = kernel
    healer = SelfHealer(store, kernel=kernel, force_full=True)
    eng = LtsEngine(store, healer=healer)

    (specs / "s1.lts").write_text(_lts("SPC1", "spec 1 EDITED"), encoding="utf-8")
    kernel.poll_once(part)
    assert eng.find(corpus="p", prefix="SPC1")["success"] is True
    assert part.corpus_index.full_rebuilds == 1
    assert part.corpus_index.incremental_rebuilds == 0


def test_a_new_root_appearing_is_a_structural_change_handled_by_reseating(tmp_path):
    """A registered-but-empty root that later APPEARS changes the freshness root SET.
    The heal re-seats the kernel over the new root set rather than pretending."""
    dec = tmp_path / "decisions"
    specs = tmp_path / "specs"
    dec.mkdir()
    (dec / "decision-graph-p.lts").write_text(
        "---\ndomain: core.p\n---\n# D [SYSTEM:D-DOC]\n\n"
        "### [DEC-1] One\n- definition: one\n", encoding="utf-8")
    roots = [{"path": str(dec), "area": "decisions",
              "only_file": "decision-graph-p.lts"},
             {"path": str(specs), "area": "specs"}]
    part = build_merged_partition("p", roots, "proj.p")
    eng, kernel, healer = _healing_engine(part)
    assert part.root_paths == [str(dec)]

    specs.mkdir()
    (specs / "s0.lts").write_text(_lts("SPC0", "spec zero"), encoding="utf-8")
    # the new root is invisible to the seated oracle, so force a heal via the decisions root
    with open(dec / "decision-graph-p.lts", "a", encoding="utf-8") as fh:
        fh.write("\n### [DEC-2] Two\n- definition: two\n")
    kernel.poll_once(part)

    res = eng.find(corpus="p", token_budget=100000)
    assert res["success"] is True
    ids = {n["id"] for n in res["nodes"]}
    assert {"DEC-1", "DEC-2", "SPC0"} <= ids
    assert set(part.root_paths) == {str(dec), str(specs)}
    assert part.freshness().fresh is True


# ===========================================================================
# the phase-2 gate is INTACT when no healer is wired
# ===========================================================================
def test_without_a_healer_the_refusal_is_still_terminal(project):
    """[LEC-FRESHNESS] is not weakened: an engine with no healer refuses exactly as it
    did in phase 2, forever."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    store = LtsStore()
    store.add_partition(part)
    kernel = FreshnessKernel()
    kernel.seat_store(store)
    eng = LtsEngine(store)          # <- no healer
    assert eng.healer is None

    with open(dec / "decision-graph-p.lts", "a", encoding="utf-8") as fh:
        fh.write("\n### [DEC-2] Two\n- definition: two\n")
    kernel.poll_once(part)

    for _ in range(3):
        res = eng.find(corpus="p", prefix="DEC-")
        assert res["success"] is False and res["stale"] is True
        assert "heal" not in res


def test_build_tools_wires_the_healer(tmp_path, monkeypatch):
    """The server composition root must wire self-heal ON (and honour the kill switch)."""
    from factlattice_engine import server

    root = tmp_path / "proj"
    root.mkdir()
    (root / "x.lts").write_text(_lts("X1", "x one"), encoding="utf-8")
    cfg = tmp_path / "corpora.json"
    cfg.write_text('{"proj": {"roots": [{"path": "%s", "area": "specs"}], '
                   '"domain_suffix": "proj.p"}}' % root, encoding="utf-8")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(cfg))

    tools = server.build_tools(watch=False)
    assert isinstance(tools.healer, SelfHealer)
    assert isinstance(tools.freshness_provider, PollVerifiedFreshness)

    monkeypatch.setenv("FACTLATTICE_SELF_HEAL", "0")
    monkeypatch.setenv("FACTLATTICE_VERIFY_ON_QUERY", "0")
    rebuilt = server.build_tools(watch=False)
    assert rebuilt.healer is None
    assert isinstance(rebuilt.freshness_provider, ContentHashFreshness)


# ===========================================================================
# the WATCH-LAG hole — a write between polls must not be served as fresh
# ===========================================================================
def _server_shaped_engine(part):
    """Exactly `server.build_tools`'s wiring: verify-on-query + self-heal."""
    store = LtsStore()
    store.add_partition(part)
    kernel = FreshnessKernel()
    kernel.seat_store(store)
    store.kernel = kernel
    provider = PollVerifiedFreshness()
    healer = SelfHealer(store, kernel=kernel, freshness_provider=provider)
    return LtsEngine(store, healer=healer, freshness_provider=provider), kernel, healer


def test_phase2_gate_alone_serves_a_write_that_lands_between_polls(project):
    """DOCUMENTS THE HOLE this phase closes. With the pure phase-2 provider, the gate
    compares against the MAINTAINED root, which only advances when the watcher polls —
    so a write between polls is answered FRESH over PRE-WRITE content."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    store = LtsStore()
    store.add_partition(part)
    kernel = FreshnessKernel()
    kernel.seat_store(store)
    store.kernel = kernel
    eng = LtsEngine(store, freshness_provider=ContentHashFreshness(),
                    healer=SelfHealer(store, kernel=kernel))

    with open(dec / "decision-graph-p.lts", "a", encoding="utf-8") as fh:
        fh.write("\n### [DEC-2] Two\n- definition: two\n")
    res = eng.find(corpus="p", prefix="DEC-")               # NO poll in between
    assert res["success"] is True
    assert [n["id"] for n in res["nodes"]] == ["DEC-1"], \
        "this is the pre-write answer — the lag window the server wiring must close"


def test_verify_on_query_closes_the_lag_window(project):
    """The server wiring: a write followed IMMEDIATELY by a query — no watcher tick, no
    manual poll — returns the POST-write answer."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    eng, kernel, healer = _server_shaped_engine(part)
    assert [n["id"] for n in eng.find(corpus="p", prefix="DEC-")["nodes"]] == ["DEC-1"]

    with open(dec / "decision-graph-p.lts", "a", encoding="utf-8") as fh:
        fh.write("\n### [DEC-2] Two\n- definition: two\n")
    res = eng.find(corpus="p", prefix="DEC-")               # NO poll in between
    assert res["success"] is True
    assert [n["id"] for n in res["nodes"]] == ["DEC-1", "DEC-2"]
    assert healer.rebuilds == 1


def test_verify_on_query_still_does_zero_content_hashing_when_fresh(project, monkeypatch):
    """The cost of closing the window is a stat-only HINT pass: on an unchanged corpus the
    query path performs ZERO per-file content hashes and reads ZERO corpus bytes, so
    phase-2's O(0) claim (stated in hashes and bytes) is untouched."""
    dec, specs = project
    part = build_merged_partition("p", _roots(dec, specs), "proj.p")
    eng, kernel, healer = _server_shaped_engine(part)
    eng.find(corpus="p")

    spy = _HashSpy(monkeypatch)
    kernel.hash_ops = 0
    for _ in range(30):
        assert eng.find(corpus="p", prefix="DEC-")["success"] is True
    assert kernel.hash_ops == 0 and spy.calls == 0
