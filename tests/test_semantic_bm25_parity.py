"""BM25 parity regression (followups item 6; S1-S4 review P3-8): the engine's LexRank must rank
exactly as the S0 control ranker that the A4 gate numbers were measured against.

The reference below is the S0 spike's `bm25.BM25` scoring loop and tokenizer, frozen in logic
(brute-force over every document; no postings; it sums query terms in set order where LexRank
sums in sorted order, and the 1e-9 tolerance absorbs that), so a change to LexRank's tokenizer, stop
list, stemmer, head boost, idf or length normalisation fails here. Corpus: this repo's own
specs/*.lts through the engine's real card builder (real-shaped, committed, deterministic).
Queries: every node title, plus fixed multi-word probes and edge cases.
Provenance: the S0 original is preserved read-only at
an internal measurement report (not shipped), reference-rankers/
bm25.py sha256 70ea27cde917fcb003c83d2f7feb5867f0e370408b73b368fbfa0f3ef46c6f2d. A4 used ENGINE
cards (S0_CARDS=engine); with bm25.py's own default spike cards the rankings differ by design.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter

from factlattice_engine import config
from factlattice_engine.semantic.index import partition_cards
from factlattice_engine.semantic.lexrank import LexRank

_STOP = set("a an the and or of to in on for is are was were be by with at as it its this that "
            "what which how do does did i my we our you your can could should would will from "
            "into about when where who why there their them they he she not no".split())


def _stem(t):
    for suf in ("ing", "ies", "es", "ed", "ly", "s"):
        if len(t) > len(suf) + 2 and t.endswith(suf):
            return t[: -len(suf)] + ("y" if suf == "ies" else "")
    return t


def _toks(text, stem):
    out = [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP]
    return [_stem(t) for t in out] if stem else out


class _ReferenceBM25:
    def __init__(self, cards, stem=True, k1=1.2, b=0.75):
        self.stem, self.k1, self.b = stem, k1, b
        docs = {nid: [t for kind, text in cs for t in _toks(text, stem) * (2 if kind == "head" else 1)]
                for nid, cs in cards.items()}
        self.ids = list(docs)
        self.tf = [Counter(docs[i]) for i in self.ids]
        self.len = [len(docs[i]) for i in self.ids]
        self.avg = sum(self.len) / max(len(self.len), 1)
        df = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(self.ids)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query, k=10):
        q = set(_toks(query, self.stem))
        scores = []
        for i, c in enumerate(self.tf):
            s = 0.0
            for t in q:
                f = c.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
            if s > 0:
                scores.append((s, self.ids[i]))
        scores.sort(key=lambda x: (-x[0], x[1]))
        return [(i, s) for s, i in scores[:k]]


PROBES = ["semantic suggestions are not matches", "freshness self heal stale corpus",
          "domain profile vocabulary", "which decision amends no llm", "edge layers reverse lookup",
          "index version refused never mixed", "property key grammar parse prose"]


def _cards():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    store = config.build_store({"specs": {"roots": [{"path": os.path.join(repo, "specs"), "area": "specs"}],
                                          "domain_suffix": "factlattice.specs"}})
    part = store.get_partition("specs")
    return part, partition_cards(part)


def test_lexrank_matches_the_s0_reference_ranker_ordered_top10():
    part, cards = _cards()
    assert len(cards) >= 30, "the real-shaped corpus shrank: the parity check would be thin"
    eng, ref = LexRank(cards), _ReferenceBM25(cards)
    queries = sorted({(part.nodes[n].get("title") or "") for n in part.nodes} - {""}) + PROBES
    nonempty = 0
    for q in queries:
        a, b = eng.search(q, 10), ref.search(q, 10)
        assert [i for i, _ in a] == [i for i, _ in b], q
        for (_, sa), (_, sb) in zip(a, b):
            assert math.isclose(sa, sb, rel_tol=1e-9), q
        nonempty += bool(a)
    assert nonempty >= 0.9 * len(queries)


def test_unstemmed_variant_also_matches():
    _, cards = _cards()
    eng, ref = LexRank(cards, stem=False), _ReferenceBM25(cards, stem=False)
    for q in PROBES:
        assert [i for i, _ in eng.search(q, 10)] == [i for i, _ in ref.search(q, 10)], q


def test_exact_score_ties_break_by_id_ascending_like_the_reference():
    """The real corpus has no top-10 ties, so the tie rule is pinned on identical documents."""
    cards = {nid: [("head", "tile map version")] for nid in ("N-C", "N-A", "N-B")}
    cards["N-D"] = [("head", "unrelated words")]
    assert [i for i, _ in LexRank(cards).search("tile map", 10)] == ["N-A", "N-B", "N-C"]
    assert [i for i, _ in _ReferenceBM25(cards).search("tile map", 10)] == ["N-A", "N-B", "N-C"]


EDGE = ["", "   ", "the of and what is", "zzqxv nonexistentterm", "Zéro-map ΟΔΟΣ versión", "a-b_c/d.e"]


def test_edge_case_queries_match_the_reference_including_empty_results():
    _, cards = _cards()
    eng, ref = LexRank(cards), _ReferenceBM25(cards)
    for q in EDGE:
        a, b = eng.search(q, 10), ref.search(q, 10)
        assert [i for i, _ in a] == [i for i, _ in b], repr(q)
    assert eng.search("", 10) == [] and eng.search("the of and what is", 10) == []
    assert eng.search("zzqxv nonexistentterm", 10) == []
