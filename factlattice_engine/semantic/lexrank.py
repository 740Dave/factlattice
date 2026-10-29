"""[SEM-LEXRANK] Deterministic BM25 over a corpus's semantic cards: the lexical half of the
hybrid suggestions, and the source of every candidate's matched/unmatched term evidence.

Pure Python, no model and no numpy, so it works where the optional `semantic` extra is absent.
It reproduces the ranker the S0 bake-off measured (lts-semantic-recall-v1): the same stop-words,
light suffix stemming, head card counted twice (a BM25F-lite field boost: the author-chosen head
outranks an incidental body mention), k1=1.2, b=0.75, ties broken by node id. One deliberate
difference: query terms are summed in SORTED order (the S0 spike iterated a `set`, whose order
varies per process), so scores are bit-reproducible.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, List, Tuple

STOP = frozenset(
    "a an the and or of to in on for is are was were be by with at as it its this that "
    "what which how do does did i my we our you your can could should would will from "
    "into about when where who why there their them they he she not no".split())
_SUFFIXES = ("ing", "ies", "es", "ed", "ly", "s")
_WORD = re.compile(r"[a-z0-9]+")


def _stem(t: str) -> str:
    for suf in _SUFFIXES:
        if len(t) > len(suf) + 2 and t.endswith(suf):
            return t[: -len(suf)] + ("y" if suf == "ies" else "")
    return t


def tokens(text: str, stem: bool = True) -> List[str]:
    out = [t for t in _WORD.findall(text.lower()) if t not in STOP]
    return [_stem(t) for t in out] if stem else out


class LexRank:
    def __init__(self, cards: Dict[str, List[Tuple[str, str]]], stem: bool = True,
                 k1: float = 1.2, b: float = 0.75):
        self.stem, self.k1, self.b = stem, k1, b
        self.ids: List[str] = sorted(cards)
        self._tf: List[Counter] = []
        self._len: List[int] = []
        postings: Dict[str, List[Tuple[int, int]]] = {}
        for i, nid in enumerate(self.ids):
            toks: List[str] = []
            for kind, text in cards[nid]:
                toks.extend(tokens(text, stem) * (2 if kind == "head" else 1))
            tf = Counter(toks)
            self._tf.append(tf)
            self._len.append(len(toks))
            for t, f in tf.items():
                postings.setdefault(t, []).append((i, f))
        self._postings = postings
        self._avg = (sum(self._len) / len(self._len)) if self._len else 0.0
        n = len(self.ids)
        self.idf = {t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in postings.items()}
        self._index_of = {nid: i for i, nid in enumerate(self.ids)}

    def search(self, query: str, k: int = 10) -> List[Tuple[str, float]]:
        scores: Dict[int, float] = {}
        for t in sorted(set(tokens(query, self.stem))):
            for i, f in self._postings.get(t, ()):
                norm = self.k1 * (1 - self.b + self.b * self._len[i] / self._avg)
                scores[i] = scores.get(i, 0.0) + self.idf[t] * f * (self.k1 + 1) / (f + norm)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], self.ids[kv[0]]))
        return [(self.ids[i], s) for i, s in ranked[:k]]

    def unknown_terms(self, query: str) -> List[str]:
        """Query terms the corpus never uses (a query asking about something it may not have)."""
        return sorted({t for t in tokens(query, self.stem) if t not in self.idf})

    def term_coverage(self, query: str, node_id: str) -> Tuple[List[str], List[str]]:
        """(matched, unmatched) query terms for one candidate: the per-candidate evidence."""
        have = self._tf[self._index_of[node_id]] if node_id in self._index_of else {}
        q = sorted(set(tokens(query, self.stem)))
        return [t for t in q if t in have], [t for t in q if t not in have]
