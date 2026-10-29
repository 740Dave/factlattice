"""Tests for the net-new §7 wrapper: parse() -> ParsedCorpus, first-class
diagnostics (§3 taxonomy), to_graph_index() parity with build_index, and the §5
profile hook (explicit + auto-discovered)."""

import json

import factlattice
from factlattice import (
    Diagnostic, ParsedCorpus, build_index, load_profile, parse,
)
from factlattice.corpus import (
    DIAG_COLLISIONS, DIAG_DANGLING, DIAG_UNREGISTERED_NS, DIAG_UNRESOLVED_TARGET,
)
from tests import snippets


def _corpus(tmp_path, files):
    root = tmp_path / "corpus"
    root.mkdir()
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
    return str(root)


# --- import + basic shape ---------------------------------------------------
def test_import_and_parse_returns_parsed_corpus(tmp_path):
    root = _corpus(tmp_path, {"full.lts": snippets.FULL})
    pc = parse(root, built_at="t")
    assert isinstance(pc, ParsedCorpus)
    assert pc.nodes and pc.edges
    ids = {n.id for n in pc.nodes}
    assert "SYN-A" in ids and "SYN-DOC" in ids
    # edges group into the four canonical layers
    ebl = pc.edges_by_layer()
    assert set(ebl) >= {"semantic", "temporal", "causal", "entity"}
    assert any(e.type == "IMPLEMENTS" for e in ebl["semantic"])


def test_source_corpus_block_present(tmp_path):
    root = _corpus(tmp_path, {"full.lts": snippets.FULL})
    pc = parse(root, corpus_id="syn", domain_suffix="test.dom", built_at="t")
    sc = pc.source_corpus
    assert sc["corpus_id"] == "syn"
    assert sc["domain_suffix"] == "test.dom"
    assert sc["files_hash"].startswith("sha256:")


# --- to_graph_index() parity with build_index -------------------------------
def test_to_graph_index_equals_build_index_dir(tmp_path):
    root = _corpus(tmp_path, {"full.lts": snippets.FULL})
    pc = parse(root, corpus_id="example", domain_suffix="gov.example", built_at="fixed")
    direct = build_index(root, corpus_id="example", domain_suffix="gov.example",
                         built_at="fixed")
    # Byte-identical JSON serialization: the wrapper adds NOTHING to the index shape.
    assert json.dumps(pc.to_graph_index(), sort_keys=True) == json.dumps(direct, sort_keys=True)


def test_to_graph_index_has_reverse_adjacency(tmp_path):
    root = _corpus(tmp_path, {"full.lts": snippets.FULL})
    gi = parse(root, built_at="t").to_graph_index()
    for layer in ("semantic", "temporal", "causal", "entity"):
        assert "adjacency" in gi["layers"][layer]
        assert "reverse_adjacency" in gi["layers"][layer]


# --- diagnostics classification (§3 taxonomy) -------------------------------
def test_diagnostics_dangling(tmp_path):
    root = _corpus(tmp_path, {"d.lts": snippets.DANGLING})
    pc = parse(root, built_at="t")
    dang = pc.diagnostics_of(DIAG_DANGLING)
    assert any(d.node_id == "DNG-DOES-NOT-EXIST" for d in dang)
    assert all(isinstance(d, Diagnostic) for d in pc.diagnostics)


def test_diagnostics_collisions(tmp_path):
    root = _corpus(tmp_path, {"c1.lts": snippets.COLLIDE_1,
                              "c2.lts": snippets.COLLIDE_2})
    pc = parse(root, built_at="t")
    coll = pc.diagnostics_of(DIAG_COLLISIONS)
    assert len(coll) == 1
    assert coll[0].node_id == "DUP-ID"
    # normalized name: the diagnostic kind is 'collisions' (not 'id_collisions')
    assert coll[0].kind == "collisions"


def test_diagnostics_unregistered_ns_and_unresolved_target(tmp_path):
    root = _corpus(tmp_path, {"full.lts": snippets.FULL})
    pc = parse(root, built_at="t")
    uns = pc.diagnostics_of(DIAG_UNREGISTERED_NS)
    assert any("example.GATES" in d.message for d in uns)
    # snippets.FULL's example.GATES line has a [SYN-B] node target, so it is NOT an
    # unresolved_target; the LIST_ITEM snippet carries the prose-target case.


def test_diagnostics_unresolved_target(tmp_path):
    root = _corpus(tmp_path, {"li.lts": snippets.LIST_ITEM})
    pc = parse(root, built_at="t")
    ut = pc.diagnostics_of(DIAG_UNRESOLVED_TARGET)
    assert ut, "the `example.GATES some capability` prose target must flag unresolved_target"
    assert all(d.kind == "unresolved_target" for d in ut)
    # an unresolved_target is NOT also double-counted as a dangling diagnostic
    ut_lines = {(d.source_file, d.source_line) for d in ut}
    dang_lines = {(d.source_file, d.source_line) for d in pc.diagnostics_of(DIAG_DANGLING)}
    assert ut_lines.isdisjoint(dang_lines)


def test_diagnostic_kinds_are_contract_classes(tmp_path):
    root = _corpus(tmp_path, {"full.lts": snippets.FULL, "d.lts": snippets.DANGLING,
                              "c1.lts": snippets.COLLIDE_1, "c2.lts": snippets.COLLIDE_2})
    pc = parse(root, built_at="t")
    allowed = set(factlattice.CONTRACT_DIAGNOSTIC_CLASSES) | {"parse_error"}
    assert {d.kind for d in pc.diagnostics} <= allowed


# --- single-file / paths mode ----------------------------------------------
def test_parse_single_file(tmp_path):
    p = tmp_path / "one.lts"
    p.write_text(snippets.FULL, encoding="utf-8")
    pc = parse(str(p), built_at="t")
    assert {n.id for n in pc.nodes} >= {"SYN-A", "SYN-B", "SYN-C"}
    # to_graph_index still works and is a valid index dict in file mode
    gi = pc.to_graph_index()
    assert gi["stats"]["total_nodes"] == len(gi["nodes"])


def test_parse_paths_list_matches_dir_build(tmp_path):
    # A file-set of exactly the corpus files should produce the same graph structure
    # (node/edge counts) as a dir build over those files — proving the temp-staging
    # path reuses build_index without divergence.
    root = _corpus(tmp_path, {"full.lts": snippets.FULL})
    files = [str(tmp_path / "corpus" / "full.lts")]
    pc_paths = parse(paths=files, built_at="t")
    gi_dir = build_index(root, built_at="t")
    assert pc_paths.to_graph_index()["stats"]["total_nodes"] == gi_dir["stats"]["total_nodes"]
    assert pc_paths.to_graph_index()["stats"]["total_edges"] == gi_dir["stats"]["total_edges"]


# --- §5 profile hook --------------------------------------------------------
PROFILE_YAML = """\
domain:
  id: "lib.eng"
  name: "Test profile"
  namespaces:
    - "lib.eng"
    - "lib.ops"
prefixes:
  - { prefix: "SYN-", document: "x" }
"""


def test_load_profile_yaml(tmp_path):
    p = tmp_path / "99-test-profile.yaml"
    p.write_text(PROFILE_YAML, encoding="utf-8")
    prof = load_profile(str(p))
    assert prof.domain_id == "lib.eng"
    assert "core" in prof.registered_ns
    assert "lib.eng" in prof.registered_ns and "lib.ops" in prof.registered_ns


def test_load_profile_dict():
    prof = load_profile({"domain": {"id": "d", "namespaces": ["d.ns"]}})
    assert "d.ns" in prof.registered_ns
    assert "core" in prof.registered_ns


def test_profile_auto_discovery(tmp_path):
    # A corpus carrying a NN-*-profile.yaml is auto-discovered (no explicit profile=).
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "full.lts").write_text(snippets.FULL, encoding="utf-8")
    (root / "99-test-profile.yaml").write_text(PROFILE_YAML, encoding="utf-8")
    pc = parse(str(root), built_at="t")
    assert pc.profile.domain_id == "lib.eng"
    assert "lib.eng" in pc.profile.registered_ns


def test_explicit_profile_supplies_registered_ns(tmp_path):
    # A profile that registers the `example` namespace makes example.GATES REGISTERED,
    # so it no longer flags unregistered_ns — proving the profile drives the core hook.
    root = _corpus(tmp_path, {"full.lts": snippets.FULL})
    prof = {"domain": {"id": "gov.example", "namespaces": ["example"]}}
    pc = parse(root, profile=prof, built_at="t")
    assert not pc.diagnostics_of(DIAG_UNREGISTERED_NS), \
        "registering the `example` namespace via profile must clear the unregistered_ns flag"


def test_no_profile_defaults_to_core_only(tmp_path):
    root = _corpus(tmp_path, {"full.lts": snippets.FULL})
    pc = parse(root, built_at="t")
    assert pc.profile.registered_ns == factlattice.DEFAULT_REGISTERED_NS


# ---------------------------------------------------------------------------
# [SL-16] paths= mode must resolve a collision the SAME way a directory build does.
#
# REGRESSION: `_build_over_files` used to stage an explicit path list into a flat
# temp dir keyed by os.path.basename(), which destroyed the directory component
# BEFORE build_index's sort-then-first-wins collision rule ran. Because '-' (0x2D)
# sorts before '.' (0x2E), `x-STAGING.lts` beat `x.lts` in flat mode while
# `proposals/x-STAGING.lts` lost to `x.lts` in directory mode — one rule, two input
# strings, two different winners. lts_lint therefore reported a `kept_source_file`
# the engine did not serve, in BOTH argument orders.
#
# This is the AGREEMENT CHECK (standing rule 16): a positive control proves a rule
# FIRES; it does not prove the rule describes the same object the engine serves.
# The collision detector fired 14x with a correct count and named the wrong file.
# ---------------------------------------------------------------------------
def _dup_id_corpus(tmp_path):
    """A root file and a subdirectory draft that define the SAME node id.

    Named so the subdirectory copy wins a BASENAME sort and loses a PATH sort —
    the exact shape that made the two modes disagree.
    """
    root = tmp_path / "corpus"
    (root / "proposals").mkdir(parents=True)
    ratified = root / "decision-graph.lts"
    draft = root / "proposals" / "decision-graph-STAGING.lts"
    ratified.write_text(
        "---\ntitle: t\n---\n# Doc [SYSTEM:DOC]\n\n"
        "## [DEC-X] ratified\n- status: accepted\n", encoding="utf-8")
    draft.write_text(
        "---\ntitle: t\n---\n# Draft [SYSTEM:DRAFT]\n\n"
        "## [DEC-X] draft\n- status: proposed\n", encoding="utf-8")
    return root, ratified, draft


def _kept(source):
    c = factlattice.parse(source)
    colls = [d for d in c.diagnostics if d.kind == DIAG_COLLISIONS]
    return colls, {d.detail.get("kept_source_file") for d in colls}


def test_collision_winner_is_identical_in_dir_and_paths_modes(tmp_path):
    root, ratified, draft = _dup_id_corpus(tmp_path)

    dir_colls, dir_kept = _kept(str(root))
    fwd_colls, fwd_kept = _kept([str(ratified), str(draft)])
    rev_colls, rev_kept = _kept([str(draft), str(ratified)])

    # exact count, not "non-zero" (standing rule 3)
    assert len(dir_colls) == 1
    assert len(fwd_colls) == 1
    assert len(rev_colls) == 1

    # the ratified root file wins, and ARGUMENT ORDER MUST NOT CHANGE IT
    assert dir_kept == {"decision-graph.lts"}
    assert fwd_kept == dir_kept, "paths= mode disagreed with directory mode"
    assert rev_kept == dir_kept, "collision winner depended on argument order"


def test_paths_mode_keeps_directory_structure_in_source_labels(tmp_path):
    """Flattening also degraded provenance: the label the collision finding prints
    became a bare basename, so a reader could not tell `proposals/x` from `x`."""
    root, ratified, draft = _dup_id_corpus(tmp_path)
    c = factlattice.parse([str(ratified), str(draft)])
    labels = {n.source_file for n in c.nodes}
    assert "proposals/decision-graph-STAGING.lts" in labels
    assert "decision-graph.lts" in labels


def test_paths_mode_agrees_with_dir_mode_on_which_node_is_served(tmp_path):
    """The graph itself — not just the report — must match. build_index runs over the
    staged tree, so a flattened stage produced a genuinely different graph.

    NOTE: this asserts against `to_graph_index()`, NOT `.nodes`. `.nodes` retains the
    collision LOSER as a parsed node ([SL-17] — `node_count` counts what was indexed,
    not what is served), so reading a winner off `.nodes` silently reports whichever
    copy happened to be appended last. The index is the served graph.
    """
    root, ratified, draft = _dup_id_corpus(tmp_path)
    served_dir = factlattice.parse(str(root)).to_graph_index()["nodes"]["DEC-X"]
    served_paths = (factlattice.parse([str(ratified), str(draft)])
                    .to_graph_index()["nodes"]["DEC-X"])
    assert served_dir["properties"].get("status") == "accepted"
    assert served_paths["source_file"] == served_dir["source_file"]
    assert served_paths["properties"] == served_dir["properties"]
