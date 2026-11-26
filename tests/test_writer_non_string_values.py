"""A typed writer stores text. A value that is not a string is refused, never turned into its Python spelling.

THE DEFECT. `lts_add_node` ran `str(v)` over every property value, so the JSON list ["a", "b"] was stored as the
one line `- prop: ['a', 'b']`, and None as `None`. The gate compared the parsed value with `str(want)`: the same
transformation on both sides, so it passed, and the writer replied success. The other writers raised a raw
AttributeError at `value.strip()`.

Each case calls the adapter functions under the MCP layer (factlattice_engine.authoring), plus `tools` dispatch where
it reshapes an argument. File bytes and the ledger folder are compared before and after, so a refusal that wrote
something fails. The string control runs in the same setup.

`properties` itself must be a mapping: `dict(properties)` used to turn ["ab"] into `- a: b`, and a falsy value
into a node with no properties, on the non-create path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from factlattice.authoring import edit
from factlattice_engine import authoring
from factlattice_engine.tools import dispatch

CODE = "non_string_value"
BASE = "# Doc [SPEC:NS-ROOT]\n\n## Node one [NS-ONE]\n\n- summary: first\n"
VALUES = [["alpha", "beta"], [], {"a": 1}, 3, 2.5, True, False, None]
IDS = ["list", "empty-list", "dict", "int", "float", "true", "false", "none"]


@pytest.fixture()
def doc(tmp_path, monkeypatch):
    home, state, corpus = tmp_path / "home", tmp_path / "state", tmp_path / "corpus"
    for d in (home, state, corpus):
        d.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(state))
    f = corpus / "doc.lts"
    f.write_text(BASE, encoding="utf-8")
    return f


def _ledger_files(f: Path):
    d = f.parent.parent / "state" / "ledger"
    return sorted(p.name for p in d.glob("*")) if d.exists() else []


def _assert_refused(reply, f: Path, before: bytes, field: str):
    assert reply["success"] is False and reply["written"] is False
    assert reply["evidence"]["refusal"] == CODE, reply
    assert reply["evidence"]["field"] == field
    assert "AttributeError" not in str(reply)
    assert f.read_bytes() == before
    assert _ledger_files(f) == []


def test_controls_a_string_still_writes(doc):
    r = authoring.add_node(str(doc), "NS-TWO", "Two", {"prop": "['text that looks like a list']", "n": "3"})
    assert r["success"] is True, r
    r = authoring.add_property(str(doc), "NS-ONE", "other", "None")
    assert r["success"] is True, r
    text = doc.read_text(encoding="utf-8")
    assert "- prop: ['text that looks like a list']" in text and "- other: None" in text
    assert _ledger_files(doc), "control: a write leaves a ledger here, so the empty-ledger checks below can fail"


@pytest.mark.parametrize("value", VALUES, ids=IDS)
def test_add_node_refuses(doc, value):
    before = doc.read_bytes()
    r = authoring.add_node(str(doc), "NS-TWO", "Two", {"good": "ok", "prop": value})
    _assert_refused(r, doc, before, "prop")
    assert "NS-TWO" not in doc.read_text(encoding="utf-8")


def test_add_node_names_every_bad_key_at_once(doc):
    before = doc.read_bytes()
    r = authoring.add_node(str(doc), "NS-TWO", "Two", {"p1": ["a"], "good": "ok", "p2": None})
    assert r["evidence"]["refusal"] == CODE and r["evidence"]["keys"] == ["p1", "p2"]
    assert doc.read_bytes() == before


def test_add_node_create_refuses_and_leaves_no_file(doc):
    new = doc.parent / "new.lts"
    r = authoring.add_node(str(new), "NS-TWO", "Two", {"prop": ["a"]}, create=True)
    assert r["success"] is False and r["evidence"]["refusal"] == CODE, r
    assert not new.exists()


def test_add_node_create_parent_refuses_and_makes_no_folder(doc):
    new = doc.parent / "sub" / "new.lts"
    r = authoring.add_node(str(new), "NS-TWO", "Two", {"prop": None}, create=True, create_parent=True)
    assert r["success"] is False and r["evidence"]["refusal"] == CODE, r
    assert not new.parent.exists()


BAD_MAPPINGS = [["ab"], [["k", "v"]], [], False, 0, "", "xy", 3]
BAD_IDS = ["list-of-str", "list-of-pair", "empty-list", "false", "zero", "empty-str", "str", "int"]


@pytest.mark.parametrize("props", BAD_MAPPINGS, ids=BAD_IDS)
def test_add_node_properties_must_be_a_mapping(doc, props):
    before = doc.read_bytes()
    r = authoring.add_node(str(doc), "NS-TWO", "Two", props)
    assert r["success"] is False and r["evidence"]["refusal"] == "bad_argument", r
    assert r["evidence"]["field"] == "properties"
    assert doc.read_bytes() == before and _ledger_files(doc) == []


@pytest.mark.parametrize("props", BAD_MAPPINGS, ids=BAD_IDS)
def test_tool_dispatch_does_not_turn_a_falsy_properties_into_none(doc, props):
    before = doc.read_bytes()
    r = dispatch("lts_add_node", {"path": str(doc), "node_id": "NS-TWO", "title": "Two", "properties": props})
    assert r["success"] is False and r["evidence"]["refusal"] == "bad_argument", r
    assert doc.read_bytes() == before


@pytest.mark.parametrize("flags", [{"create": True}, {"create": True, "create_parent": True}], ids=["create", "parent"])
@pytest.mark.parametrize("props", BAD_MAPPINGS, ids=BAD_IDS)
def test_create_through_dispatch_refuses_a_properties_that_is_no_mapping(doc, props, flags):
    new = doc.parent / "sub" / "new.lts" if flags.get("create_parent") else doc.parent / "new.lts"
    r = dispatch("lts_add_node", {"path": str(new), "node_id": "NS-TWO", "title": "Two", "properties": props, **flags})
    assert r["success"] is False and r["evidence"]["refusal"] == "create_argument_invalid", r
    assert not new.exists() and not (doc.parent / "sub").exists()


@pytest.mark.parametrize("props", [None, {}], ids=["null", "empty"])
def test_create_through_dispatch_with_no_properties_still_creates(doc, props):
    new = doc.parent / "new.lts"
    r = dispatch("lts_add_node", {"path": str(new), "node_id": "NS-TWO", "title": "Two", "properties": props, "create": True})
    assert r["success"] is True and new.exists(), r


@pytest.mark.parametrize("props", [None, {}], ids=["null", "empty"])
def test_controls_no_properties_is_still_a_node(doc, props):
    r = authoring.add_node(str(doc), "NS-TWO", "Two", props)
    assert r["success"] is True, r


@pytest.mark.parametrize("value", VALUES, ids=IDS)
@pytest.mark.parametrize("mode", ["set", "append"])
def test_add_property_refuses(doc, value, mode):
    before = doc.read_bytes()
    r = authoring.add_property(str(doc), "NS-ONE", "prop", value, mode=mode)
    _assert_refused(r, doc, before, "value")


@pytest.mark.parametrize("value", [v for v in VALUES if v is not None],
                         ids=[i for i in IDS if i != "none"])
def test_repair_refuses_a_caller_value_that_is_not_text(doc, value):
    doc.write_text(BASE + "- ⛔ FLAGGED: kept\n", encoding="utf-8")
    before = doc.read_bytes()
    r = authoring.repair_property(str(doc), "NS-ONE", 6, "flagged", value=value)
    _assert_refused(r, doc, before, "value")


def test_repair_with_no_value_still_derives_it_from_the_line(doc):
    doc.write_text(BASE + "- ⛔ FLAGGED: kept\n", encoding="utf-8")
    r = authoring.repair_property(str(doc), "NS-ONE", 6, "flagged")
    assert r["success"] is True, r
    assert "- flagged: kept" in doc.read_text(encoding="utf-8")


def test_the_code_is_stable_and_known_on_both_paths():
    assert CODE in edit.WRITER_REFUSAL_CODES and CODE in edit.KNOWN_CREATE_CODES
