"""[PERF-SUGGEST-INDEX] suggest_keys finds near-miss candidates through an exact symmetric-delete
index instead of scanning every declared key with Levenshtein (cold-build hot spot, measured by
The architect 2026-10-02: 5.2 of 8.2 s of build_store() in suggest_keys -> _levenshtein).

Exactness: if lev(a, b) <= k, deleting <= k characters from each of a and b reaches a common
string, so every true answer shares a <=k-delete with the key; each candidate is then VERIFIED
with the same _levenshtein, so the output is identical to the brute-force scan.
"""
from __future__ import annotations

import random
import string

from factlattice import core_keys as K


def _brute(key, declared, k=2):
    scored = sorted((K._levenshtein(key, c, k), c) for c in declared)
    return [c for d, c in scored if 0 < d <= k]


def _declared(n, seed=1):
    rng = random.Random(seed)
    alpha = string.ascii_lowercase + "-"
    out = set()
    while len(out) < n:
        out.add("".join(rng.choice(alpha) for _ in range(rng.randint(2, 24))).strip("-") or "x")
    return frozenset(out)


def test_levenshtein_calls_are_bounded_by_candidates_not_vocabulary(monkeypatch):
    declared = _declared(613)
    n = {"calls": 0}
    real = K._levenshtein

    def counting(a, b, cap):
        n["calls"] += 1
        return real(a, b, cap)
    monkeypatch.setattr(K, "_levenshtein", counting)
    K.suggest_keys.cache_clear()
    for key in ("zz-novel-key-one", "another-undeclared", "qq"):
        K.suggest_keys(key, declared)
    assert n["calls"] < 0.05 * 3 * len(declared), n


def test_output_identical_to_brute_force_scan():
    declared = _declared(613, seed=2)
    rng = random.Random(3)
    keys = sorted(declared)[:200]                 # sorted: frozenset order varies with PYTHONHASHSEED
    for base in keys[:200]:                       # near-misses: 1-2 random edits of real keys
        w = list(base)
        for _ in range(rng.randint(1, 2)):
            op = rng.randrange(3) if w else 1     # an emptied word can only gain a character
            i = rng.randrange(len(w) + (op == 1))
            if op == 0 and w: w[min(i, len(w) - 1)] = rng.choice(string.ascii_lowercase)
            elif op == 1: w.insert(i, rng.choice(string.ascii_lowercase))
            elif w: del w[min(i, len(w) - 1)]
        keys.append("".join(w))
    keys += ["", "a", "-", "x" * 103, "zz-novel"]
    K.suggest_keys.cache_clear()
    for key in keys:
        assert K.suggest_keys(key, declared) == _brute(key, declared), key


def test_other_distances_still_match_brute_force():
    declared = _declared(80, seed=4)
    K.suggest_keys.cache_clear()
    for key in sorted(declared)[:30] + ["abc", "zzzz"]:
        for k in (0, 1, 3):
            assert K.suggest_keys(key, declared, k) == [c for d, c in sorted(
                (K._levenshtein(key, c, k), c) for c in declared) if 0 < d <= k], (key, k)
