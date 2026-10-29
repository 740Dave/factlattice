"""[FIND-PHRASE-SEMANTICS] lts_find's `text` is ONE phrase per field, and its description says so.

Measured 2026-10-05 (the architect): two agent definitions and several reports described lts_find as
"token-AND"; the engine has matched the whole query as a substring of a single field since its
first commit. The description was silent on HOW it matches, which is where the wrong belief came
from. These tests pin the description (new) and the behaviour it describes (unchanged).
"""
from __future__ import annotations

from factlattice_engine import config, server
from factlattice_engine.tools import LtsEngine


def _desc(name):
    return next(t for t in server.TOOLS if t["name"] == name)["description"]


def test_description_states_whole_text_is_one_substring():
    assert "ONE case-insensitive substring" in _desc("lts_find")


def test_description_states_single_field_and_which_fields():
    d = _desc("lts_find")
    assert "within a single field" in d
    assert "the id, the title, or one property value" in d


def test_description_states_not_word_by_word_with_the_example():
    d = _desc("lts_find")
    assert "NOT word by word" in d
    assert "'borrower card contents' can match where 'borrower contents' does not" in d


def test_description_states_list_elements_count():
    assert "list elements such as repeated `alias` lines count" in _desc("lts_find")


def test_description_states_text_is_not_trimmed():
    assert "not trimmed or whitespace-normalised" in _desc("lts_find")


def _engine(tmp_path):
    root = tmp_path / "c"
    root.mkdir()
    (root / "00-c.lts").write_text(
        "# C [DOC-C]\n\n"
        "## Init [XL-M1]\n\n- input: Borrower card CONTENTS (required)\n\n"
        "## Aliased [N-AL]\n\n- alias: tile map container\n- alias: encrypted envelope\n", encoding="utf-8")
    return LtsEngine(config.build_store({"c": {"roots": [{"path": str(root)}]}}))


def _ids(eng, text):
    return sorted(n["id"] for n in eng.find(corpus="c", text=text, semantic="off").get("nodes") or [])


def test_behaviour_the_description_states(tmp_path):
    eng = _engine(tmp_path)
    assert _ids(eng, "borrower card contents") == ["XL-M1"]      # verbatim phrase, case-insensitive
    assert _ids(eng, "card CONTENTS") == ["XL-M1"]              # any contiguous substring
    assert _ids(eng, "borrower contents") == []                 # words present, phrase absent: ZERO
    assert _ids(eng, "encrypted envelope") == ["N-AL"]         # the SECOND alias line (a list element) is read
    assert _ids(eng, "init borrower") == []                     # spanning title + property: no single field holds it
    assert _ids(eng, "XL-M1") == ["XL-M1"]                     # the id is a field
    assert _ids(eng, " borrower card") == []                     # not trimmed: the value starts "Borrower", so a leading space cannot match
    assert _ids(eng, "borrower card") == ["XL-M1"]               # (control: the same needle without the space hits)
