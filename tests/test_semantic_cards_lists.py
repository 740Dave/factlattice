"""[SEM-CARDS-LIST-VALUES] a repeated property key parses as a LIST; every string element must reach
the head card (embedding + BM25), with the same exclusions as a scalar value."""
from __future__ import annotations

from factlattice_engine.semantic import cards as C
from factlattice_engine.semantic.lexrank import LexRank


def _node(**props):
    return {"id": "N-1", "title": "A plain title", "properties": props}


def test_every_list_element_reaches_the_head_card():
    h = C.head_card("N-1", _node(alias=["first stranger phrasing", "second stranger phrasing"],
                                 requirement=["the gate must refuse partial frames"]))
    assert "first stranger phrasing" in h and "second stranger phrasing" in h
    assert "the gate must refuse partial frames" in h


def test_list_elements_keep_the_scalar_exclusions():
    h = C.head_card("N-1", _node(**{"see-also": ["[A-1]", "[B-2], [C-3]", "real words here"],
                                    "decided-in-session": ["2026-10-02", "2026-10-03"]}))
    assert "[A-1]" not in h and "[B-2]" not in h      # reference-only elements dropped
    assert "real words here" in h                     # a non-reference element of the same key kept
    assert "2026-10-02" not in h                      # excluded key stays excluded


def test_non_string_elements_are_skipped():
    h = C.head_card("N-1", _node(alias=["kept words", {"nested": "x"}, 7]))
    assert "kept words" in h and "nested" not in h


def test_string_only_nodes_are_unchanged():
    n = _node(alias="one alias", what="a description")
    h = C.head_card("N-1", n)
    assert h == "N 1\nA plain title\none alias\na description\nalias what"


def test_schema_version_is_bumped():
    assert C.CARD_SCHEMA_VERSION == 2
    assert C.card_fingerprint([("head", "x")]).startswith("v2:")


def test_bm25_finds_a_word_that_lives_only_in_a_list_element():
    cards = {"N-1": [("head", C.head_card("N-1", _node(alias=["zyzzogeton calibration", "other"])))],
             "N-2": [("head", C.head_card("N-2", {"id": "N-2", "title": "unrelated", "properties": {}}))]}
    hits = LexRank(cards).search("zyzzogeton", 5)
    assert hits and hits[0][0] == "N-1"
