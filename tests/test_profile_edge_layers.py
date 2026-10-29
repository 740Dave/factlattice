"""D5/D6 — profile-driven edge-LAYER + NAMESPACE resolution.

Proves the engine change:
  * a `dec.DEPENDS_ON` edge with a profile declaring it `causal` lands on `causal`
    (not the old `semantic` default) and is NOT flagged `unregistered_ns` (D5+D6);
  * an `art.*` / `temporal` case behaves the same;
  * an UNDECLARED namespaced type (`zzz.FOO`) still resolves `semantic` and IS flagged
    `unregistered_ns` — the preserve-and-flag guard still works;
  * with NO profile the parser resolves exactly as before (semantic default, {core}).
"""

import os

from factlattice import build_index
from factlattice.domain_profiles import (
    load_profile_edge_config,
    read_profile_edges,
)
from factlattice.parser import DEFAULT_REGISTERED_NS, parse_text
from factlattice_engine.config import build_merged_partition, engine_profile_config


# A minimal profile carrying exactly the §6 edge-type table shape the real profiles use.
_PROFILE = """\
---
title: "Test Profile"
id: "DOMAIN:TST"
---
# Test Profile [DOMAIN:TST]

## 6. Domain-Specific Edge Types

### 6.1 Decision Edges

| Type | Layer | Description |
|------|-------|-------------|
| dec.DEPENDS_ON | causal | This decision is only valid while another holds |
| dec.SUPERSEDES | temporal | Replaces an earlier decision |

### 6.2 Artifact Edges

| Type | Layer | Description |
|------|-------|-------------|
| art.IN_SESSION | temporal | Produced in a specific session |
| art.PRODUCED_BY | entity | Produced by an actor |
"""

# A corpus whose Relationships edges use namespaced types under a NON-LAYER subhead,
# so the layer is derived from the edge type (profile), not from the subhead name.
_CORPUS = """\
---
domain: core.example
---
# Decisions [DECISION-GRAPH:DG-DOC]

## 1. Nodes

### [DEC-A] Alpha decision
- decision: choose A

### [DEC-B] Beta decision
- decision: choose B

### [ART-X] An artifact
- kind: report

## Relationships

### Cross-Decision
- [DEC-A] dec.DEPENDS_ON [DEC-B]
- [DEC-A] dec.SUPERSEDES [DEC-B]
- [ART-X] art.IN_SESSION [DEC-A]
- [DEC-A] zzz.FOO [DEC-B]
"""


def _edge(parsed, etype):
    for e in parsed.edges:
        if e.type == etype:
            return e
    raise AssertionError(f"edge {etype} not emitted")


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return str(p)


# --- the reader ------------------------------------------------------------------
def test_reader_extracts_type_layer_and_namespaces(tmp_path):
    prof_dir = tmp_path / "domain-profiles"
    prof_dir.mkdir()
    _write(prof_dir, "test-profile.lts", _PROFILE)
    cfg = load_profile_edge_config(str(prof_dir))
    assert cfg.edge_layers == {
        "dec.DEPENDS_ON": "causal",
        "dec.SUPERSEDES": "temporal",
        "art.IN_SESSION": "temporal",
        "art.PRODUCED_BY": "entity",
    }
    assert cfg.namespaces == frozenset({"dec", "art"})
    assert cfg.registered_ns == frozenset({"core", "dec", "art"})
    assert cfg.ambiguities == []


def test_reader_flags_non_canonical_layer_without_failing(tmp_path):
    bad = _PROFILE + "\n| dec.WOBBLE | sideways | not a real layer |\n"
    path = _write(tmp_path, "bad-profile.lts", bad)
    edges, amb = read_profile_edges(path)
    assert "dec.WOBBLE" not in edges          # skipped, not guessed
    assert "dec.DEPENDS_ON" in edges          # the good rows still parse
    assert any(a["kind"] == "non_canonical_layer" and a["type"] == "dec.WOBBLE"
               for a in amb)


# --- resolution at the parser seam ------------------------------------------------
def test_profile_layers_drive_resolution():
    cfg_layers = {"dec.DEPENDS_ON": "causal", "dec.SUPERSEDES": "temporal",
                  "art.IN_SESSION": "temporal", "art.PRODUCED_BY": "entity"}
    reg = frozenset({"core", "dec", "art"})
    parsed = parse_text(_CORPUS, "dg.lts", registered_ns=reg, profile_layers=cfg_layers)

    dep = _edge(parsed, "dec.DEPENDS_ON")
    assert dep.layer == "causal"
    assert "unregistered_ns" not in dep.flags

    art = _edge(parsed, "art.IN_SESSION")
    assert art.layer == "temporal"
    assert "unregistered_ns" not in art.flags

    # an UNdeclared namespaced type still defaults semantic AND stays flagged.
    foo = _edge(parsed, "zzz.FOO")
    assert foo.layer == "semantic"
    assert "unregistered_ns" in foo.flags


def test_no_profile_is_unchanged_semantic_and_flagged():
    parsed = parse_text(_CORPUS, "dg.lts")  # defaults: no profile, {core}
    dep = _edge(parsed, "dec.DEPENDS_ON")
    assert dep.layer == "semantic"                 # old default
    assert "unregistered_ns" in dep.flags          # dec not registered
    art = _edge(parsed, "art.IN_SESSION")
    assert art.layer == "semantic"
    assert "unregistered_ns" in art.flags


# --- the engine partition-build path (end-to-end via config) ----------------------
def test_engine_build_applies_profile(tmp_path, monkeypatch):
    prof_dir = tmp_path / "domain-profiles"
    prof_dir.mkdir()
    _write(prof_dir, "test-profile.lts", _PROFILE)
    corpus = tmp_path / "decisions"
    corpus.mkdir()
    _write(corpus, "dg.lts", _CORPUS)

    monkeypatch.setenv("FACTLATTICE_PROFILE_DIR", str(prof_dir))
    profile = engine_profile_config(str(prof_dir))   # bypass module cache deterministically
    part = build_merged_partition(
        "tst", [{"path": str(corpus), "area": "decisions"}], "proj.tst",
        profile=profile)
    assert part is not None

    causal_edges = {(e["source"], e["target"], e["type"])
                    for e in part.index["layers"]["causal"]["edges"]}
    temporal_edges = {(e["source"], e["target"], e["type"])
                      for e in part.index["layers"]["temporal"]["edges"]}
    semantic_edges = {(e["source"], e["target"], e["type"])
                      for e in part.index["layers"]["semantic"]["edges"]}

    assert ("DEC-A", "DEC-B", "dec.DEPENDS_ON") in causal_edges
    assert ("DEC-A", "DEC-B", "dec.SUPERSEDES") in temporal_edges
    assert ("ART-X", "DEC-A", "art.IN_SESSION") in temporal_edges
    # undeclared namespaced type stays semantic
    assert ("DEC-A", "DEC-B", "zzz.FOO") in semantic_edges

    # unregistered flag: dec/art registered (unflagged), zzz still flagged.
    flags_by_type = {}
    for ld in part.index["layers"].values():
        for e in ld["edges"]:
            flags_by_type[e["type"]] = e["flags"]
    assert "unregistered_ns" not in flags_by_type["dec.DEPENDS_ON"]
    assert "unregistered_ns" not in flags_by_type["art.IN_SESSION"]
    assert "unregistered_ns" in flags_by_type["zzz.FOO"]


def test_shipped_profiles_declare_agentic_layers():
    """The real shipped domain-profiles resolve dec.*/art.* to their declared layers."""
    cfg = engine_profile_config()
    assert cfg.edge_layers.get("dec.DEPENDS_ON") == "causal"
    assert cfg.edge_layers.get("dec.SUPERSEDES") == "temporal"
    assert cfg.edge_layers.get("art.IN_SESSION") == "temporal"
    assert cfg.edge_layers.get("art.DERIVED_FROM") == "causal"
    assert {"dec", "art"}.issubset(cfg.registered_ns)
    assert cfg.registered_ns.issuperset({"core"})
    # default DEFAULT_REGISTERED_NS is untouched (back-compat sentinel).
    assert DEFAULT_REGISTERED_NS == frozenset({"core"})


def test_build_index_default_has_no_profile(tmp_path):
    """A bare build_index (no profile args) resolves dec.* semantic + flagged — the
    back-compat guarantee that the engine wiring, not the core default, applies D5/D6."""
    root = tmp_path / "c"
    root.mkdir()
    _write(root, "dg.lts", _CORPUS)
    idx = build_index(corpus_root=str(root), built_at="X")
    dep = next(e for ld in idx["layers"].values() for e in ld["edges"]
               if e["type"] == "dec.DEPENDS_ON")
    assert dep["layer"] == "semantic"
    assert "unregistered_ns" in dep["flags"]
