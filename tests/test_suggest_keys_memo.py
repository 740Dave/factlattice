"""[PERF-SUGGEST-KEYS] The undeclared-key did-you-mean must cost per DISTINCT key, not per occurrence.

Measured 2026-10-02 (the architect, on a scratch copy of the 5,062-line decision-log .lts, live code):
authoring.add_node took ~2.8 s PER NODE, and 7.9 of 8.4 profiled seconds were
`_check_key_vocabulary -> suggest_keys -> _levenshtein` (1.9M calls). The gate re-parses the
whole file 3x per write, and every parse re-scored every OCCURRENCE of every undeclared key
against the whole vocabulary, although the answer depends only on (key, vocabulary).

These tests count work instead of timing it: deterministic, and they fail on the pre-change code.
"""
from __future__ import annotations

import factlattice
from factlattice import core_keys


def _corpus(tmp_path, n_nodes=40, keys=("answerd-by", "questoin", "zz-custom")):
    lines = ["# Doc [DOC-1]", ""]
    for i in range(n_nodes):
        lines += [f"## Node {i} [N-{i}]", ""] + [f"- {k}: v{i}" for k in keys] + [""]
    p = tmp_path / "c.lts"
    p.write_text("\n".join(lines), encoding="utf-8")
    return tmp_path


def test_levenshtein_work_scales_with_distinct_keys_not_occurrences(tmp_path, monkeypatch):
    core_keys.suggest_keys.cache_clear() if hasattr(core_keys.suggest_keys, "cache_clear") else None
    calls = {"n": 0}
    real = core_keys._levenshtein

    def counting(a, b, cap):
        calls["n"] += 1
        return real(a, b, cap)
    monkeypatch.setattr(core_keys, "_levenshtein", counting)
    root = _corpus(tmp_path)
    factlattice.parse(str(root))
    vocab = len(core_keys.core_keys())
    distinct_undeclared = 3
    # one full scoring per distinct (key, vocabulary): <= distinct x vocab (+ slack for a
    # profile-extended vocabulary). The pre-change code does 40 nodes x 3 keys x vocab.
    assert calls["n"] <= distinct_undeclared * (vocab + 50), calls["n"]


def test_suggestions_are_unchanged_and_callers_cannot_poison_the_cache():
    declared = frozenset(core_keys.core_keys())
    a = core_keys.suggest_keys("see-alos", declared)
    assert "see-also" in a
    a.append("POISON")                          # a caller mutating its result...
    b = core_keys.suggest_keys("see-alos", declared)
    assert "POISON" not in b and "see-also" in b  # ...must not change the next answer
    assert core_keys.suggest_keys("totally-unrelated-key-xyz", declared) == []


def test_a_changed_vocabulary_is_a_cache_miss_never_a_stale_answer():
    """review P3 (audit-PERF-SUGGEST-KEYS-2026-10-02): the count test alone would also pass
    with a cache that answered from an old vocabulary. Pin that it cannot."""
    base = frozenset(core_keys.core_keys())
    assert "answered-by" not in core_keys.suggest_keys("answerd-by", base)
    extended = base | {"answered-by"}
    assert core_keys.suggest_keys("answerd-by", extended) == ["answered-by"]
    assert "answered-by" not in core_keys.suggest_keys("answerd-by", base)   # and back again
