"""Round 6 (Opus Diamond on c9b31f4, N1 P0): round 5 changed where the LIVE `lts_add_property` inserts.

On a node whose last property is a key-only head (`- meta:`) over an indented block the parser does NOT parse, the
round-5 walk (`_property_block_end` stopping at the head) put the new key BETWEEN the head and its block; the new
value then swallowed the block's first line (`'1 one: 1 two: 2'`) and `remove_property` could delete the authored
lines with both gates green. Round 6: (1) the three set_property insertion walks are 88aa5d6's again; (2) the add
gate checks the stored value AS PARSED equals the requested one (`edit._value_check`).
"""
from __future__ import annotations

import pytest

from factlattice.parser import BRANCH_PROP_BOLD, BRANCH_PROP_CONT, BRANCH_PROP_KEY_ONLY, BRANCH_PROP_SIMPLE, parse_text
from factlattice.parser import BRANCH_PROP_ITEM_CONT, BRANCH_PROP_MAPPING_CONT, BRANCH_PROP_MAPPING_ENTRY, BRANCH_PROP_NESTED_ITEM

# The three helpers below (scratch-dir fixture and two builders) are defined locally so this file stands alone.


@pytest.fixture()
def w(tmp_path, monkeypatch):
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "st"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "corpus"
    d.mkdir()
    return d


def put(d, text, name="c.lts", newline=None):
    f = d / name
    with open(f, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)
    return f


def doc(*body, eol="\n"):
    return eol.join(["# Doc [SPEC:RR-ROOT]", "", "## One [RR-ONE]", "", *body, "", "## Two [RR-TWO]", "", "- k: other", ""])

# key-only head + a block the parser refuses / ignores (Diamond's shapes). `meta` is the LAST property of the node.
UNPARSED = {
    "deeper-map": ["- owner: ds", "- meta:", "  one: 1", "    two: 2"],
    "star-bullets": ["- owner: ds", "- meta:", "  * a", "  * b"],
    "mixed": ["- owner: ds", "- meta:", "  - a", "  b: c"],
    "map-then-bullet": ["- owner: ds", "- meta:", "  b: c", "  - d"],
    "blank-items": ["- owner: ds", "- meta:", "", "  - a", "  - b"],
    "plain-text": ["- owner: ds", "- meta:", "  just some text", "  more text"],
}


def block_of(body):
    i = body.index("- meta:")
    return body[i:]


def intact(text, body):
    """The head and its block are contiguous and in order, exactly as authored."""
    block = block_of(body)
    lines = text.splitlines()
    i = lines.index("- meta:")
    return lines[i:i + len(block)] == block


@pytest.mark.parametrize("shape", sorted(UNPARSED))
def test_add_lands_after_the_unparsed_block_and_parses_as_requested(w, shape):
    from factlattice.authoring import edit
    f = put(w, doc(*UNPARSED[shape]))
    r = edit.set_property(str(f), "RR-ONE", "zed", "1")
    text = f.read_text()
    assert r.parsed_value == "1"
    assert intact(text, UNPARSED[shape]), text
    node = next(n for n in parse_text(text, "x").nodes if n.id == "RR-ONE")
    assert node.properties["zed"] == "1"


@pytest.mark.parametrize("shape", sorted(UNPARSED))
def test_append_of_an_absent_key_lands_after_the_unparsed_block(w, shape):
    from factlattice.authoring import edit
    f = put(w, doc(*UNPARSED[shape]))
    r = edit.set_property(str(f), "RR-ONE", "zed", "1", mode="append")
    assert r.parsed_value == "1" and intact(f.read_text(), UNPARSED[shape])


@pytest.mark.parametrize("shape", sorted(UNPARSED))
def test_rename_after_an_add_leaves_the_unparsed_block_alone(w, shape):
    from factlattice.authoring import edit
    f = put(w, doc(*UNPARSED[shape]))
    edit.set_property(str(f), "RR-ONE", "zed", "1")
    edit.rename_property(str(f), "RR-ONE", "zed", "zee")
    text = f.read_text()
    assert intact(text, UNPARSED[shape]) and "- zee: 1" in text


@pytest.mark.parametrize("shape", sorted(UNPARSED))
def test_remove_after_an_add_leaves_the_authored_block_in_the_file(w, shape):
    """The Diamond's chain: add then remove. On c9b31f4 the remove deleted the block's lines with both gates green."""
    from factlattice.authoring import edit
    f = put(w, doc(*UNPARSED[shape]))
    original = f.read_text()
    edit.set_property(str(f), "RR-ONE", "zed", "1")
    edit.remove_property(str(f), "RR-ONE", "zed", expect_value="1")
    assert f.read_text() == original


@pytest.mark.parametrize("shape", sorted(UNPARSED))
def test_rename_and_remove_of_a_flat_key_beside_the_block_keep_it_byte_identical(w, shape):
    from factlattice.authoring import edit
    f = put(w, doc(*UNPARSED[shape]))
    edit.rename_property(str(f), "RR-ONE", "owner", "boss")
    assert intact(f.read_text(), UNPARSED[shape])
    edit.remove_property(str(f), "RR-ONE", "boss")
    assert intact(f.read_text(), UNPARSED[shape])


def test_the_key_only_head_itself_is_refused_by_rename_and_remove(w):
    from factlattice.authoring import edit
    original = doc(*UNPARSED["deeper-map"])
    f = put(w, original)
    for fn in (lambda: edit.rename_property(str(f), "RR-ONE", "meta", "metadata"),
               lambda: edit.remove_property(str(f), "RR-ONE", "meta")):
        with pytest.raises(edit.EditRefused):
            fn()
    assert f.read_text() == original


# ------------------------------------------------------------------ 88aa5d6's placement is back (pins)
def test_add_after_furniture_lands_after_it_as_on_88aa5d6(w):
    from factlattice.authoring import edit
    f = put(w, doc("- owner: ds", "  | a | b |"))
    edit.set_property(str(f), "RR-ONE", "zed", "1")
    assert f.read_text() == doc("- owner: ds", "  | a | b |", "- zed: 1")


def test_flat_append_after_a_sub_bullet_lands_after_it_as_on_88aa5d6(w):
    from factlattice.authoring import edit
    f = put(w, doc("- k: a", "  wrapped", "  - sub bullet", "- owner: ds"))
    edit.set_property(str(f), "RR-ONE", "k", "b", mode="append")
    assert f.read_text() == doc("- k: a", "  wrapped", "  - sub bullet", "- k: b", "- owner: ds")


def test_nested_list_append_before_a_table_row_is_refused_as_on_88aa5d6(w):
    from factlattice.authoring import edit
    original = doc("- k:", "  - x", "  - y", "  | a | b |", "- owner: ds")
    f = put(w, original)
    with pytest.raises(edit.EditRefused):
        edit.set_property(str(f), "RR-ONE", "k", "z", mode="append")
    assert f.read_text() == original


# ------------------------------------------------------------------ the add-gate value check, and its RED
_BODY = frozenset({BRANCH_PROP_CONT, BRANCH_PROP_NESTED_ITEM, BRANCH_PROP_MAPPING_ENTRY, BRANCH_PROP_MAPPING_CONT,
                   BRANCH_PROP_ITEM_CONT})


def _round5_block_end(lines, start0, end0):
    """The round-5 `_property_block_end`, verbatim in effect: an indented line extends the block only when the parser
    recorded it under a property-body branch (so an unparsed block is NOT skipped)."""
    from factlattice.authoring import edit
    consumed = parse_text("\n".join(lines) + "\n", "<edit>").coverage.consumed
    last, in_prop = start0, False
    for i in range(start0 + 1, end0):
        line = lines[i]
        if edit._PROP_ANY.match(line):
            last, in_prop = i, True
        elif in_prop and consumed.get(i) in _BODY:
            last = i
        elif not line.strip():
            continue
        else:
            in_prop = False
    return last


@pytest.mark.parametrize("mode", ["set", "append"])
@pytest.mark.parametrize("shape", ["deeper-map", "plain-text", "map-then-bullet"])
def test_negative_control_value_check_refuses_the_round5_insertion(w, monkeypatch, shape, mode):
    """Patch the round-5 insertion back in through the seam: the gate alone passed it (`'1 one: 1 two: 2'` stored);
    the value check must refuse it, naming both values, writing nothing."""
    from factlattice.authoring import edit
    original = doc(*UNPARSED[shape])
    f = put(w, original)
    monkeypatch.setattr(edit, "_property_block_end", _round5_block_end)
    with pytest.raises(edit.EditRefused) as ei:
        edit.set_property(str(f), "RR-ONE", "zed", "1", mode=mode)
    assert "not the requested one" in str(ei.value)
    assert ei.value.evidence["requested_as_parsed"] == "1"
    assert ei.value.evidence["parsed_value"] != "1"
    assert f.read_text() == original


def test_the_value_check_alone_is_what_stops_it(w, monkeypatch):
    """Control for the control: with the round-5 walk AND the value check disabled the same write goes through and
    stores the absorbed value, so the refusal above is the value check's, not some other gate's."""
    from factlattice.authoring import edit
    f = put(w, doc(*UNPARSED["deeper-map"]))
    monkeypatch.setattr(edit, "_property_block_end", _round5_block_end)
    monkeypatch.setattr(edit, "_value_check", lambda *a, **k: None)
    r = edit.set_property(str(f), "RR-ONE", "zed", "1")
    assert r.parsed_value == "1 one: 1 two: 2"


def test_value_check_on_replace_and_append_values(w):
    from factlattice.authoring import edit
    f = put(w, doc("- owner: ds", "- k: a"))
    assert edit.set_property(str(f), "RR-ONE", "owner", "  padded value  ").parsed_value == "padded value"
    assert edit.set_property(str(f), "RR-ONE", "k", "b", mode="append").parsed_value == ["a", "b"]
    assert edit.set_property(str(f), "RR-ONE", "bold-ok", "x: y [RR-TWO]").parsed_value == "x: y [RR-TWO]"


def test_value_check_passes_a_nested_list_append(w):
    """The nested-block append writes a bullet (`  - x`), which ALONE is not a property: the check must compare against
    the flat form of the requested line, not the written one (found by the first differential run)."""
    from factlattice.authoring import edit
    f = put(w, doc("- owner: ds", "- alias:", "  - a1", "  - a2"))
    r = edit.set_property(str(f), "RR-ONE", "alias", "a9", mode="append")
    assert r.parsed_value == ["a1", "a2", "a9"]


def test_replace_is_not_value_checked_so_set_replace_stays_88aa5d6(w):
    """Ruling: lts_add_property must be byte-identical to 88aa5d6. Replacing a WRAPPED value keeps the old tail
    attached (a pre-existing defect, reported separately); the value check does not touch `replaced`."""
    from factlattice.authoring import edit
    f = put(w, doc("- k: old", "  old tail", "- owner: ds"))
    r = edit.set_property(str(f), "RR-ONE", "k", "new")
    assert r.action == "replaced" and r.parsed_value == "new old tail"
