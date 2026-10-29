"""[CANONICAL-EDGE-CAUTION] Part B: a `caution` annotation on semantic suggestions and lts_node.

Spec: an internal design note, SPEC-canonical-edge-and-caution-2026-10-03  (B1-B4 plus the negative control).

The caution ANNOTATES, never changes rank, membership or scores. It is driven by the typed edge:
  * an outgoing CANONICAL edge  -> kind "legacy-label"
  * a node that is SUPERSEDED   -> kind "superseded"
Edge direction (measured, not assumed): `A supersedes: [B]` mints A->B; `B superseded-by: [A]` is
the REVERSED spelling and mints the SAME A->B edge. So "B is superseded" == B has an INBOUND
SUPERSEDES edge, for BOTH spellings (as the spec now states; its v0 wording said "outgoing").

Part A (go-live 14, b0f5fc3) mints `canonical-node` -> CANONICAL in the real parser; the fixture uses it
unpatched, and test_cross_import_pin binds caution.CANONICAL_EDGE_TYPE to that table entry.
"""
from __future__ import annotations

import copy
import json

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")

import factlattice.parser as P  # noqa: E402
from factlattice_engine import config  # noqa: E402
from factlattice_engine.semantic import suggest as S  # noqa: E402
from factlattice_engine.tools import LtsEngine  # noqa: E402
from tests.semantic_helpers import _tiny_model  # noqa: E402

BODY = "tile map version field hello world"
CORPUS = (
    "# Corpus [DOC-C]\n\n"
    f"## Canonical trust model [C-1]\n\n- status: active\n- what: {BODY} canonical\n\n"
    # B1: legacy label with a canonical-label
    f"## Legacy trust model [L-1]\n\n- status: active\n- canonical-node: [C-1]\n"
    f"- canonical-label: QX-3 here means Vendor QX2\n- what: {BODY} legacy\n\n"
    # legacy label WITHOUT a canonical-label: default text
    f"## Legacy unlabelled [L-2]\n\n- status: active\n- canonical-node: [C-1]\n- what: {BODY} unlabelled\n\n"
    # B2 spelling 1: superseded-by (reversed)
    f"## Old by key [S-1]\n\n- status: active\n- superseded-by: [N-2]\n- what: {BODY} old\n\n"
    f"## New two [N-2]\n\n- status: active\n- what: {BODY} new\n\n"
    # B2 spelling 2: the successor authors `supersedes` (inbound edge on S-3)
    f"## Old by inbound [S-3]\n\n- status: active\n- what: {BODY} older\n\n"
    f"## New three [N-3]\n\n- status: active\n- supersedes: [S-3]\n- what: {BODY} newer\n\n"
    # both: canonical wins
    f"## Both [B-1]\n\n- status: active\n- canonical-node: [C-1]\n- superseded-by: [N-2]\n- what: {BODY} both\n\n"
    # F1 (review 2026-10-03): self-edges must NOT produce a caution pointing at the node itself
    f"## Self legacy [L-SELF]\n\n- status: active\n- canonical-node: [L-SELF]\n- what: {BODY} selfleg\n\n"
    f"## Self superseded [S-SELF]\n\n- status: active\n- supersedes: [S-SELF]\n- what: {BODY} selfsup\n\n"
    # plain
    f"## Plain [P-1]\n\n- status: active\n- what: {BODY} plain\n"
)


@pytest.fixture()
def world(tmp_path, monkeypatch):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "00-c.lts").write_text(CORPUS, encoding="utf-8")
    pin = _tiny_model(str(tmp_path / "models"))
    reg = {"c": {"roots": [{"path": str(root), "area": "specs"}], "domain_suffix": "proj.c"}}
    svc = S.SemanticService(state_root=str(tmp_path / "state"), model_root=str(tmp_path / "models"),
                            pin=pin, build_mode="sync")
    eng = LtsEngine(config.build_store(reg))
    eng.semantic = svc
    return eng


def _cands(eng):
    r = eng.semantic_find(corpus="c", text="tile map version", k=50)
    assert r["success"] and r["status"] == "ok", r
    return {c["id"]: c for c in r["candidates"]}, r


def test_fixture_mints_the_canonical_edge(world):
    # guards the fixture itself: the edge exists, so a missing caution is the code's fault
    part = world._gate("c")[0]
    fwd, _rev = part.adjacency()
    assert any(e["type"] == "CANONICAL" and e["target"] == "C-1" for _l, e in fwd.get("L-1", ()))


def test_B1_legacy_label_caution_with_label_text(world):
    cs, _ = _cands(world)
    assert {"L-1", "C-1"} <= set(cs), sorted(cs)
    c = cs["L-1"]["caution"]
    assert c == {"kind": "legacy-label", "canonical_id": "C-1",
                 "canonical_title": "Canonical trust model",
                 "text": "QX-3 here means Vendor QX2"}
    assert "caution" not in cs["C-1"]
    assert "caution" not in cs["P-1"]


def test_B1_default_text_when_no_canonical_label(world):
    cs, _ = _cands(world)
    assert cs["L-2"]["caution"]["text"] == "canonical definition: Canonical trust model"


def test_B2_superseded_by_key_spelling(world):
    cs, _ = _cands(world)
    assert cs["S-1"]["caution"] == {"kind": "superseded", "canonical_id": "N-2",
                                    "canonical_title": "New two", "text": "superseded by: New two"}
    assert "caution" not in cs["N-2"]


def test_B2_inbound_supersedes_spelling(world):
    cs, _ = _cands(world)
    assert cs["S-3"]["caution"] == {"kind": "superseded", "canonical_id": "N-3",
                                    "canonical_title": "New three", "text": "superseded by: New three"}
    assert "caution" not in cs["N-3"]      # the SUPERSEDER is not itself cautioned


def test_canonical_edge_wins_over_supersession(world):
    cs, _ = _cands(world)
    c = cs["B-1"]["caution"]
    assert c["kind"] == "legacy-label" and c["canonical_id"] == "C-1"


def test_B3_ranks_and_scores_unchanged_by_the_caution_code(world, monkeypatch):
    from factlattice_engine import caution as CA     # the switch lives with the caution code
    on, _ = _cands(world)
    monkeypatch.setattr(CA, "CAUTION_ENABLED", False)
    off, _ = _cands(world)
    assert not any("caution" in c for c in off.values())      # the switch really switches it off
    strip = lambda d: {i: {k: v for k, v in c.items() if k != "caution"} for i, c in d.items()}  # noqa: E731
    assert json.dumps(strip(on), sort_keys=True) == json.dumps(off, sort_keys=True)
    assert [i for i, _ in sorted(on.items(), key=lambda kv: kv[1]["rank"])] == \
           [i for i, _ in sorted(off.items(), key=lambda kv: kv[1]["rank"])]
    assert any("caution" in c for c in on.values())


def test_B4_lts_node_carries_the_caution(world):
    n = world.node(corpus="c", node_id="L-1")
    assert n["caution"] == {"kind": "legacy-label", "canonical_id": "C-1",
                            "canonical_title": "Canonical trust model",
                            "text": "QX-3 here means Vendor QX2"}
    assert world.node(corpus="c", node_id="S-3")["caution"]["kind"] == "superseded"
    assert world.node(corpus="c", node_id="S-1")["caution"]["kind"] == "superseded"


def test_B4_non_triggering_node_is_unchanged(world):
    n = world.node(corpus="c", node_id="P-1")
    assert "caution" not in n
    # the strong form: only the new key distinguishes a triggering node's key set
    assert set(world.node(corpus="c", node_id="L-1")) - {"caution"} == set(n)


def test_exact_find_results_do_not_carry_it_v1(world):
    r = world.find(corpus="c", text="legacy", rank="relevance")
    assert r["nodes"] and "caution" not in json.dumps(r)


def test_on_miss_block_carries_the_caution_too(world):
    r = world.find(corpus="c", text="tile map version qqqxyz", semantic="on_miss")
    blk = r["did_you_mean_semantic"]
    kinds = {c["id"]: c["caution"]["kind"] for c in blk["candidates"] if "caution" in c}
    assert kinds, [c["id"] for c in blk["candidates"]]      # the on-miss block annotates too
    assert set(kinds.values()) <= {"legacy-label", "superseded"}
    assert all("caution" not in c for c in blk["candidates"] if c["id"] in ("C-1", "P-1", "N-2", "N-3"))


def test_tool_descriptions_name_the_caution():
    from factlattice_engine import server
    d = {t["name"]: t["description"] for t in server.TOOLS} if hasattr(server, "TOOLS") else {}
    assert d, "tool table not found"
    for name in ("lts_semantic_find", "lts_node"):
        assert "caution" in d[name] and "lts_find" in d[name]


def test_F1_self_edges_produce_no_caution(world):
    for nid in ("L-SELF", "S-SELF"):
        assert "caution" not in world.node(corpus="c", node_id=nid), nid
    cands, _ = _cands(world)
    for nid in ("L-SELF", "S-SELF"):
        if nid in cands:
            assert "caution" not in cands[nid], nid


def test_cross_import_pin_caution_edge_type_matches_the_parser():
    """The caution reads edges by TYPE; the parser mints that type from the `canonical-node` key.
    If either side is renamed alone, the caution silently stops firing. This pins them together."""
    from factlattice_engine import caution
    assert P.REL_PROPERTY_EDGES["canonical-node"][0] == caution.CANONICAL_EDGE_TYPE
    assert P.REL_PROPERTY_EDGES["canonical-node"][2] is False  # legacy -> canonical, outbound on the legacy node
    assert P.REL_PROPERTY_EDGES["superseded-by"][0] == caution.SUPERSEDES_EDGE_TYPE
