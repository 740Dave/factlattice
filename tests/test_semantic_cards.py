"""[SEM-CARDS] S1: what text represents a node to the embedder (and to the BM25 assist).

Design section 2: one HEAD card (id with hyphens spaced + title + descriptive STRING property
values + property KEYS as bare words) plus up to MAX_CHUNKS BODY chunks of prose (heading and
property lines removed). Pure functions of (node dict, source lines): no model, no I/O beyond
what the caller hands in, deterministic.
"""
from __future__ import annotations

from factlattice_engine.semantic import cards as C


def _node(**props):
    return {"id": "XL-LEDGER-PROTOBUF", "title": "Ledger format (protobuf schema)",
            "properties": props}


def test_head_card_carries_id_title_values_and_keys():
    h = C.head_card("XL-LEDGER-PROTOBUF", _node(alias="which extractor made this Ledger",
                                                  **{"field-1": "sw_version"}))
    assert "XL LEDGER PROTOBUF" in h                    # hyphens spaced
    assert "Ledger format (protobuf schema)" in h
    assert "which extractor made this Ledger" in h      # an alias value
    assert "sw_version" in h
    assert "field 1" in h and "alias" in h               # keys as bare words


def test_head_card_excludes_lifecycle_dates_hashes_and_pure_reference_lists():
    h = C.head_card("X-1", {"id": "X-1", "title": "T", "properties": {
        "status": "active", "decided-in-session": "2026-10-02", "content-hash": "sha256:abc",
        "depends-on": "[A-1], [B-2]", "summary": "keeps this"}})
    assert "keeps this" in h
    for gone in ("active", "2026-10-02", "sha256:abc", "[A-1]"):
        assert gone not in h, gone


def test_non_string_property_values_are_ignored_not_crashed():
    h = C.head_card("X-1", {"id": "X-1", "title": "T", "properties": {"n": 3, "l": ["a"], "s": "ok"}})
    assert "ok" in h


def test_body_chunks_drop_heading_and_property_lines_and_cap():
    lines = ["## Heading [X-1]", "", "- status: active", "- what: a property line"] + \
            [f"prose word{i} " * 20 for i in range(200)]
    chunks = C.body_chunks(lines)
    assert 1 <= len(chunks) <= C.MAX_CHUNKS
    joined = " ".join(chunks)
    assert "Heading" not in joined and "status" not in joined and "property line" not in joined
    assert all(len(c.split()) <= C.CHUNK_WORDS for c in chunks)


def test_body_chunks_overlap_and_empty_body():
    words = " ".join(f"w{i}" for i in range(C.CHUNK_WORDS + 10))
    chunks = C.body_chunks([words])
    assert len(chunks) == 2
    assert chunks[1].split()[0] == f"w{C.CHUNK_WORDS - C.CHUNK_OVERLAP}"
    assert C.body_chunks(["## only a heading", "- k: v"]) == []


def test_cards_are_deterministic_and_fingerprinted():
    nd = _node(alias="x")
    a = C.node_cards("XL-LEDGER-PROTOBUF", nd, ["some prose here"])
    b = C.node_cards("XL-LEDGER-PROTOBUF", nd, ["some prose here"])
    assert a == b and a[0][0] == "head" and a[1][0] == "chunk"
    assert C.card_fingerprint(a) == C.card_fingerprint(b)
    c = C.node_cards("XL-LEDGER-PROTOBUF", nd, ["some OTHER prose"])
    assert C.card_fingerprint(a) != C.card_fingerprint(c)
    assert C.card_fingerprint(a).startswith(f"v{C.CARD_SCHEMA_VERSION}:")


def test_body_filter_uses_the_parsers_property_grammar_not_a_guess():
    """A bullet with a colon is a PROPERTY only if the parser says so. `- fresh (manifest
    current): Step 10` is PROSE to the parser (its 'key' is not [a-z][a-z0-9_-]*), so it is
    body text; `- status: active`, `- **bold-key**: v` and a key-only nested-block head are
    properties and stay out. Indented mapping children are not string properties (the head
    card skips them), so they stay in the body rather than vanishing from both."""
    lines = ["- fresh (manifest current, CSV exists): Step 10",
             "- status: active", "- **bold-key**: v", "- context_for_change:",
             "    reasoning: indented mapping child text",
             "- Capitalised Key: also prose to the parser"]
    joined = " ".join(C.body_chunks(lines))
    assert "fresh (manifest current, CSV exists): Step 10" in joined
    assert "indented mapping child text" in joined
    assert "also prose to the parser" in joined
    for gone in ("status: active", "bold-key", "context_for_change"):
        assert gone not in joined, gone
