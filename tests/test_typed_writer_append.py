"""[TYPED-WRITER-APPEND] — the value-blind receipt, and the append mode for multi-valued keys.

MEASURED 2026-10-03 (a corpus editor): `lts_add_property` could not add a SECOND line with
the same key. allow_replace=false refused; the default REPLACED the value, and the receipt said
`delta {}` / nodes changed 0, because the per-node delta compared KEY SETS — a replace changes
no key. A value replacement therefore reported as "no change". (On main the same replace on a
2-valued key silently destroyed an element; aaa3d7e's fail-loud refusal of that is cherry-picked
under this branch. What aaa3d7e did NOT close, and this file pins, is the RECEIPT: a scalar
replace still reported delta {}.)
"""
from __future__ import annotations

import pytest

from factlattice import build_index
from factlattice.authoring.edit import EditRefused, _gate, _parsed_props, set_property
from factlattice.parser import parse_text
from factlattice_engine import LtsEngine, LtsStore, Partition, authoring
from factlattice_engine.tools import dispatch

DOC = """# Doc [SPEC:TW-ROOT]

- kind: root

## One [TW-ONE]

- kind: x
- alias: a1
- alias: a2
- status: old
- note: tail

## Two [TW-TWO]

- kind: y
- alias: solo

## Three [TW-THREE]

- kind: z
- alias: w1
  folded continuation of w1
- other: o

## Four [TW-FOUR]

- kind: nested
- alias:
  - n1
  - n2
"""


@pytest.fixture()
def doc(tmp_path):
    p = tmp_path / "t.lts"
    p.write_text(DOC, encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _backups(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))


def props(path):
    return {n.id: dict(n.properties) for n in parse_text(path.read_text(encoding="utf-8"), str(path)).nodes}


def lines_of(path):
    return path.read_text(encoding="utf-8").splitlines()


# ------------------------------------------------------------------ (1) value-aware receipt
def test_scalar_replace_is_VISIBLE_in_the_receipt_not_delta_empty(doc):
    r = set_property(doc, "TW-ONE", "status", "new")
    assert r.action == "replaced" and props(doc)["TW-ONE"]["status"] == "new"
    assert r.delta == {"TW-ONE": {"added": [], "removed": [],
                                  "changed": {"status": {"removed": ["old"], "added": ["new"]}}}}


def test_adapter_counts_the_replace_in_nodes_changed(doc):
    out = authoring.add_property(str(doc), "TW-ONE", "status", "new", receipt="full")  # compact drops `report`
    assert out["success"] and out["nodes_changed"] == 1
    assert out["delta"]["TW-ONE"]["changed"]["status"] == {"removed": ["old"], "added": ["new"]}
    assert "nodes changed: 1" in out["report"]


def test_idempotent_replace_is_still_an_empty_delta(doc):
    r = set_property(doc, "TW-ONE", "status", "old")
    assert r.delta == {}


def test_pure_key_add_delta_shape_is_unchanged(doc):
    r = set_property(doc, "TW-TWO", "fresh", "v")
    assert r.delta == {"TW-TWO": {"added": ["fresh"], "removed": []}}


def test_replace_on_a_multivalued_key_is_still_refused_and_writes_nothing(doc):
    before = doc.read_text()
    with pytest.raises(EditRefused):
        set_property(doc, "TW-ONE", "alias", "a3")
    assert doc.read_text() == before


def test_gate_refuses_an_unintended_value_change_on_ANOTHER_node(doc):
    """negative control for the value-aware gate: the candidate also edits TW-TWO's kind."""
    orig = DOC
    cand = orig.replace("- alias: a2\n", "- alias: a2\n- alias: a3\n").replace("- kind: y", "- kind: Y")
    with pytest.raises(EditRefused) as e:
        _gate(orig, cand, "x", value_exact={("TW-ONE", "alias"): ([], ["a3"])})
    assert "TW-TWO" in str(e.value)


def test_gate_refuses_when_appended_multiset_grows_by_more_than_one(doc):
    cand = DOC.replace("- alias: a2\n", "- alias: a2\n- alias: a3\n- alias: a4\n")
    with pytest.raises(EditRefused):
        _gate(DOC, cand, "x", value_exact={("TW-ONE", "alias"): ([], ["a3"])})


# ------------------------------------------------------------------ (2) append mode
def test_append_adds_a_third_value_right_after_the_last_alias_line(doc):
    r = set_property(doc, "TW-ONE", "alias", "a3", mode="append")
    assert r.action == "appended"
    assert props(doc)["TW-ONE"]["alias"] == ["a1", "a2", "a3"]
    ls = lines_of(doc)
    i = ls.index("- alias: a3")
    assert ls[i - 1] == "- alias: a2" and ls[i + 1] == "- status: old"
    assert r.delta == {"TW-ONE": {"added": [], "removed": [],
                                  "changed": {"alias": {"removed": [], "added": ["a3"]}}}}


def test_append_changes_exactly_one_line_of_the_file(doc):
    before = lines_of(doc)
    set_property(doc, "TW-ONE", "alias", "a3", mode="append")
    after = lines_of(doc)
    assert len(after) == len(before) + 1
    assert [l for l in after if l not in before] == ["- alias: a3"]
    assert [l for l in before if l not in after] == []


def test_append_to_a_single_valued_key_makes_a_two_element_list(doc):
    set_property(doc, "TW-TWO", "alias", "second", mode="append")
    assert props(doc)["TW-TWO"]["alias"] == ["solo", "second"]


def test_append_on_an_absent_key_is_a_plain_add(doc):
    r = set_property(doc, "TW-TWO", "tag", "t1", mode="append")
    assert r.action == "added" and props(doc)["TW-TWO"]["tag"] == "t1"


def test_append_lands_after_a_folded_continuation_not_inside_it(doc):
    set_property(doc, "TW-THREE", "alias", "w2", mode="append")
    ls = lines_of(doc)
    i = ls.index("- alias: w2")
    assert ls[i - 1] == "  folded continuation of w1" and ls[i + 1] == "- other: o"
    assert props(doc)["TW-THREE"]["alias"] == ["w1 folded continuation of w1", "w2"]


def test_append_duplicate_value_is_refused_and_writes_nothing(doc):
    before = doc.read_text()
    with pytest.raises(EditRefused) as e:
        set_property(doc, "TW-ONE", "alias", "a2", mode="append")
    assert "already" in str(e.value) and doc.read_text() == before


def test_append_to_a_nested_LIST_block_adds_one_bullet_at_the_blocks_indent(doc):
    """the shape the real corpora use (acme-docs TERM-PROBE's `alias`)."""
    r = set_property(doc, "TW-FOUR", "alias", "n3", mode="append")
    assert r.action == "appended" and props(doc)["TW-FOUR"]["alias"] == ["n1", "n2", "n3"]
    ls = lines_of(doc)
    assert ls[-2:] == ["  - n2", "  - n3"] or ls[ls.index("  - n3") - 1] == "  - n2"
    assert r.delta["TW-FOUR"]["changed"]["alias"] == {"removed": [], "added": ["n3"]}


def test_append_duplicate_into_nested_list_is_refused(doc):
    before = doc.read_text()
    with pytest.raises(EditRefused):
        set_property(doc, "TW-FOUR", "alias", "n2", mode="append")
    assert doc.read_text() == before


@pytest.mark.parametrize("body", [
    "- alias:\n  k1: v1\n  k2: v2\n",                  # mapping block
    "- alias: flat\n- alias:\n  - n1\n",                 # flat line + nested block
])
def test_append_refuses_a_nested_block_that_is_not_a_plain_list(tmp_path, body):
    p = tmp_path / "m.lts"
    p.write_text("# D [SPEC:M-ROOT]\n\n## N [M-N]\n\n- kind: x\n" + body + "- tail: t\n")
    before = p.read_text()
    with pytest.raises(EditRefused):
        set_property(p, "M-N", "alias", "new", mode="append")
    assert p.read_text() == before


def test_append_ignores_allow_replace_false(doc):
    set_property(doc, "TW-ONE", "alias", "a3", mode="append", allow_replace=False)
    assert props(doc)["TW-ONE"]["alias"] == ["a1", "a2", "a3"]


def test_unknown_mode_is_refused(doc):
    with pytest.raises(EditRefused):
        set_property(doc, "TW-ONE", "alias", "a3", mode="prepend")


def test_whole_file_nothing_else_moved_after_append(doc):
    b = props(doc)
    set_property(doc, "TW-ONE", "alias", "a3", mode="append")
    a = props(doc)
    b["TW-ONE"]["alias"] = ["a1", "a2", "a3"]
    assert a == b


def test_adapter_and_dispatch_expose_mode(doc):
    out = dispatch("lts_add_property", {"path": str(doc), "node_id": "TW-ONE", "key": "alias",
                                        "value": "a3", "mode": "append"}, tools=None)
    assert out["success"] and out["action"] == "appended" and out["nodes_changed"] == 1
    assert out["value_as_parsed"] == ["a1", "a2", "a3"]
    bad = dispatch("lts_add_property", {"path": str(doc), "node_id": "TW-ONE", "key": "alias",
                                        "value": "zz", "mode": "bogus"}, tools=None)
    assert bad["refused"] is True


def test_server_schema_declares_mode():
    from factlattice_engine.server import TOOLS
    t = next(t for t in TOOLS if t["name"] == "lts_add_property")
    assert "mode" in t["inputSchema"]["properties"]


def test_appended_alias_is_findable_by_text_and_shown_in_node(doc, tmp_path):
    dispatch("lts_add_property", {"path": str(doc), "node_id": "TW-TWO", "key": "alias",
                                  "value": "zorblax-second", "mode": "append"}, tools=None)
    root = tmp_path / "corp"; root.mkdir(); (root / "t.lts").write_text(doc.read_text())
    idx = build_index(str(root), corpus_id="tw", domain_suffix="plt.tw", built_at="t")
    store = LtsStore(); store.add_partition(Partition(idx, source_path=str(root)))
    eng = LtsEngine(store)
    assert {n["id"] for n in eng.find(corpus="tw", text="zorblax-second")["nodes"]} == {"TW-TWO"}
    assert {n["id"] for n in eng.find(corpus="tw", text="solo")["nodes"]} == {"TW-TWO"}   # first value kept
    assert eng.node(corpus="tw", node_id="TW-TWO")["node"]["properties"]["alias"] == ["solo", "zorblax-second"]
    node = eng.node(corpus="tw", node_id="TW-TWO")
    assert "zorblax-second" in str(node)


# ------------------------------------------------------------------ review fix round
MAPDOC = "# D [SPEC:MP-ROOT]\n\n## N [MP-N]\n\n- kind: x\n- spec:\n  a: 1\n  b: 2\n- tail: t\n"


@pytest.mark.parametrize("allow", [True, False])
def test_F1_set_on_a_MAPPING_block_is_refused_and_writes_nothing(tmp_path, allow):
    p = tmp_path / "m.lts"; p.write_text(MAPDOC)
    assert props(p)["MP-N"]["spec"] == {"a": "1", "b": "2"}      # control: it IS a mapping
    with pytest.raises(EditRefused) as e:
        set_property(p, "MP-N", "spec", "scalar", allow_replace=allow)
    if allow:
        assert "mapping" in str(e.value).lower() or "nested" in str(e.value).lower()
    assert p.read_text() == MAPDOC


def test_F1_set_on_a_scalar_still_works(tmp_path):
    p = tmp_path / "m.lts"; p.write_text(MAPDOC)
    set_property(p, "MP-N", "kind", "y")
    assert props(p)["MP-N"]["kind"] == "y"


def test_F3_append_gate_refuses_a_reorder_of_existing_values():
    cand = DOC.replace("- alias: a1\n- alias: a2\n", "- alias: a2\n- alias: a1\n- alias: a3\n")
    with pytest.raises(EditRefused):
        _gate(DOC, cand, "x", value_exact={("TW-ONE", "alias"): ([], ["a3"])})


def test_F3_append_gate_accepts_the_in_order_append():
    cand = DOC.replace("- alias: a2\n", "- alias: a2\n- alias: a3\n")
    _gate(DOC, cand, "x", value_exact={("TW-ONE", "alias"): ([], ["a3"])})


def test_F4_F6_tool_description_states_the_limits():
    from factlattice_engine.server import TOOLS
    t = next(t for t in TOOLS if t["name"] == "lts_add_property")
    d = t["description"] + t["inputSchema"]["properties"]["mode"]["description"]
    assert "status" in d and "status filter" in d
    assert "`-` bullet" in d and "`*`" in d
