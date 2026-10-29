"""A repeated key parses to a LIST (parser._maybe_multivalue) and every search path used to SKIP it.

MEASURED 2026-10-03 (a corpus editor, acme-docs): node CONJECTURE-ACME-UPLOAD-FILE-PATH has
two `- alias:` lines. `lts_find(text="CONJECTURE-UPLOAD-1")` returned 0 — the FIRST alias value
too — while a single-line alias on a neighbour was found; `lts_find_in_prose` called the hit
`body-only`. The parser kept every value (see test_parser_keeps_every_repeated_value); the QUERY
layer dropped them: `_structure_hit_in` read `isinstance(v, str)` only, and the semantic head card
(which feeds BM25 `rank="relevance"` AND the embedding index) does the same STILL — out of scope
here, owned by the architect (semantic/cards.py); not touched by this change.

Fixture = the real shape: one node with a two-line alias, one with a single-line alias.
"""
from __future__ import annotations

import pytest

from factlattice import build_index
from factlattice.parser import parse_text
from factlattice_engine import LtsEngine, LtsStore, Partition

CORPUS = """---
domain: PLT.DEMO
---

# Alias demo [SPEC:AL-ROOT]

- status: operational

## Two-line alias node [AL-TWO]

- status: operational
- alias: first-alias-zorblax
- alias: second-alias-quuxling

Body prose.

## Single-line alias node [AL-ONE]

- status: operational
- alias: solo-alias-frobnitz

Body prose.

## Neighbour [AL-NEIGH]

- status: operational
- function: alpha
- function: beta
"""


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    root = tmp_path_factory.mktemp("alias-corpus")
    (root / "a.lts").write_text(CORPUS, encoding="utf-8")
    idx = build_index(str(root), corpus_id="alias-demo", domain_suffix="plt.demo", built_at="t")
    store = LtsStore()
    store.add_partition(Partition(idx, source_path=str(root)))
    return LtsEngine(store)


def _ids(r, key="nodes"):
    return {n["id"] for n in (r.get(key) or [])}


def test_parser_keeps_every_repeated_value_in_order():
    node = next(n for n in parse_text(CORPUS, "a.lts").nodes if n.id == "AL-TWO")
    assert node.properties["alias"] == ["first-alias-zorblax", "second-alias-quuxling"]


def test_positive_control_single_line_alias_is_found(engine):
    assert _ids(engine.find(corpus="alias-demo", text="frobnitz")) == {"AL-ONE"}


@pytest.mark.parametrize("term", ["first-alias-zorblax", "second-alias-quuxling", "QUUXLING"])
def test_every_element_of_a_list_valued_property_is_found_by_text(engine, term):
    assert _ids(engine.find(corpus="alias-demo", text=term)) == {"AL-TWO"}


def test_find_reports_which_property_matched_in_find_in_prose(engine):
    res = engine.find_in_prose(corpus="alias-demo", text="zorblax")
    hit = next(h for h in res["hits"] if h["id"] == "AL-TWO")
    assert hit["finding"] == "also-in-structure"
    assert hit["structure_hit_in"] == ["properties.alias"]


def test_absent_term_still_zero(engine):
    assert _ids(engine.find(corpus="alias-demo", text="zzzabsentzzz")) == set()


def test_function_filter_matches_any_element(engine):
    assert _ids(engine.find(corpus="alias-demo", function="beta")) == {"AL-NEIGH"}
    assert _ids(engine.find(corpus="alias-demo", function="alpha")) == {"AL-NEIGH"}


def test_relevance_rank_sees_list_values(engine):
    r = engine.find(corpus="alias-demo", text="quuxling", rank="relevance")
    assert _ids(r) == {"AL-TWO"} and r["rank_applied"]["by"] == "bm25"
