"""[SEM-LEXRANK] The deterministic BM25 half of the hybrid suggestions (no model, no numpy).

It must reproduce the S0-measured ranker exactly (stop-words, light suffix stemming, head
counted twice, k1=1.2, b=0.75, ties by node id), because the S0 decision was made on it.
"""
from __future__ import annotations

import math
import subprocess
import sys

from factlattice_engine.semantic import lexrank as L


def test_tokenisation_is_pinned():
    assert L.tokens("The Versions of Ledgers were stored", stem=True) == ["version", "ledger", "stor"]
    assert L.tokens("The Versions of Ledgers", stem=False) == ["versions", "ledgers"]
    assert L.tokens("what is it?", stem=True) == []           # all stop-words


def test_bm25_score_matches_the_formula_by_hand():
    cards = {"A": [("head", "alpha beta")], "B": [("head", "gamma")]}
    r = L.LexRank(cards, stem=False)
    # doc A tokens (head x2): alpha beta alpha beta -> len 4; B: gamma gamma -> len 2; avg 3
    n, df = 2, 1
    idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
    tf, k1, b, dl, avg = 2, 1.2, 0.75, 4, 3.0
    want = idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avg))
    (nid, score), = r.search("alpha", 5)
    assert nid == "A" and abs(score - want) < 1e-12


def test_head_outranks_an_incidental_body_mention_and_ties_break_by_id():
    cards = {"BODY": [("head", "unrelated title"), ("chunk", "ledger version appears here")],
             "HEAD": [("head", "ledger version"), ("chunk", "other words entirely")],
             "TIE-A": [("head", "tiebreak")], "TIE-B": [("head", "tiebreak")]}
    r = L.LexRank(cards)
    assert [n for n, _ in r.search("ledger version", 2)] == ["HEAD", "BODY"]
    assert [n for n, _ in r.search("tiebreak", 2)] == ["TIE-A", "TIE-B"]


def test_matched_unmatched_terms_and_corpus_knowledge():
    r = L.LexRank({"N": [("head", "tile map version field")], "M": [("head", "other")]})
    m, u = r.term_coverage("tile map smart tv", "N")
    assert m == ["map", "tile"] and u == ["smart", "tv"]
    assert r.unknown_terms("tile map smart tv") == ["smart", "tv"]
    assert r.search("nothing matches here at all", 5) == []


def test_lexrank_needs_no_model_stack():
    code = ("import sys; import factlattice_engine.semantic.lexrank as L; "
            "L.LexRank({'A':[('head','x y')]}).search('x', 1); "
            "bad=[m for m in ('numpy','onnxruntime') if m in sys.modules]; sys.exit(1 if bad else 0)")
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
