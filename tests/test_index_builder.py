"""Index-builder tests: schema shape, reverse_adjacency, dangling/collision
flagging, atomic write, idempotence."""

import json
import os

from factlattice.index_builder import (
    build_index, compute_snapshot_hash, write_index_atomic,
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


def test_index_schema_shape(tmp_path):
    root = _corpus(tmp_path, {"full.lts": snippets.FULL})
    idx = build_index(root, corpus_id="example", domain_suffix="gov.example",
                      built_at="2026-07-19T00:00:00Z")
    assert idx["version"] == "1.0"
    sc = idx["source_corpus"]
    for key in ("corpus_id", "domain_suffix", "root_paths", "files_hash",
                "node_count", "edge_count", "built_at", "factlattice_profile_version",
                "commit_ref", "snapshot_hash", "parent_snapshot_version",
                "parent_snapshot_hash"):
        assert key in sc, f"source_corpus missing {key}"
    # provenance placeholders carried as null (mechanism = later phase)
    assert sc["commit_ref"] is None
    assert sc["parent_snapshot_hash"] is None
    for layer in ("semantic", "temporal", "causal", "entity"):
        assert layer in idx["layers"]
        assert "adjacency" in idx["layers"][layer]
        assert "reverse_adjacency" in idx["layers"][layer]


def test_reverse_adjacency_is_populated(tmp_path):
    root = _corpus(tmp_path, {"full.lts": snippets.FULL})
    idx = build_index(root, built_at="t")
    sem_rev = idx["layers"]["semantic"]["reverse_adjacency"]
    # SYN-C is IMPLEMENTS-target of SYN-A -> reverse maps SYN-C -> [.., SYN-A]
    assert "SYN-C" in sem_rev
    assert "SYN-A" in sem_rev["SYN-C"]


def test_dangling_edges_flagged_not_dropped(tmp_path):
    root = _corpus(tmp_path, {"d.lts": snippets.DANGLING})
    idx = build_index(root, built_at="t")
    all_edges = [e for layer in idx["layers"].values() for e in layer["edges"]]
    dangs = [e for e in all_edges if e["dangling"]]
    assert any(e["target"] == "DNG-DOES-NOT-EXIST" for e in dangs)
    assert idx["stats"]["dangling_edges"] >= 1


def test_id_collisions_preserved(tmp_path):
    root = _corpus(tmp_path, {"c1.lts": snippets.COLLIDE_1,
                              "c2.lts": snippets.COLLIDE_2})
    idx = build_index(root, built_at="t")
    assert idx["stats"]["id_collisions"] == 1
    coll = idx["id_collisions"][0]
    assert coll["id"] == "DUP-ID"
    # first definition wins node_map; collision recorded, not dropped
    assert idx["nodes"]["DUP-ID"]["source_file"] == coll["kept_source_file"]
    assert coll["collision_source_file"] != coll["kept_source_file"]


def test_atomic_write_and_readback(tmp_path):
    root = _corpus(tmp_path, {"full.lts": snippets.FULL})
    idx = build_index(root, built_at="t")
    out = str(tmp_path / "out" / "graph_index.json")
    write_index_atomic(idx, out)
    assert os.path.exists(out)
    # no temp files left behind
    leftovers = [f for f in os.listdir(os.path.dirname(out))
                 if f.startswith(".graph_index.")]
    assert leftovers == []
    with open(out, encoding="utf-8") as fh:
        loaded = json.load(fh)
    assert loaded["stats"]["total_nodes"] == idx["stats"]["total_nodes"]


def test_atomic_write_overwrites_in_place(tmp_path):
    root = _corpus(tmp_path, {"full.lts": snippets.FULL})
    out = str(tmp_path / "graph_index.json")
    write_index_atomic(build_index(root, built_at="t"), out)
    first = os.path.getsize(out)
    write_index_atomic(build_index(root, built_at="t"), out)  # idempotent overwrite
    assert os.path.getsize(out) == first


def test_build_is_deterministic(tmp_path):
    root = _corpus(tmp_path, {"full.lts": snippets.FULL})
    a = build_index(root, built_at="fixed")
    b = build_index(root, built_at="fixed")
    # snapshot hash excludes itself and is stable across identical builds
    assert compute_snapshot_hash(a) == compute_snapshot_hash(b)
