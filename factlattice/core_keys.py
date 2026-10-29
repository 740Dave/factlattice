"""SPEC §1.3 + §1.5's property-key tables, SHIPPED AS DATA — Era 1, C11 (amendment A2).

⛔ ONE DEFINITION, IMPORTED BY PARSER, LINTER AND WRITER. This module exists for the
reason `constants.REQUIRED_FRONTMATTER` exists, generalized: the measured failure was
two surfaces holding DISJOINT ideas of the same vocabulary while each stayed internally
consistent, so neither could see the disagreement (the sanctioned writer could not emit
a document the sanctioned linter would accept, and no test could fail because no test
compared them). A second copy of a vocabulary is a silent-drop mechanism.

⛔ THE MINTING KEYS ARE READ FROM THE PARSER, NEVER RESTATED. `REL_PROPERTY_EDGES` and
`FLOW_PROPERTY_EDGES` are the operative tables; a hand-maintained duplicate here would
drift the day someone adds a key to one and not the other, and the drift would present
as "a correct key is reported undeclared" — a false positive on the ONE diagnostic
whose whole value is precision.

WHAT "CORE" MEANS HERE, precisely (amendment A2 point i): the union of §1.3's four
tables (relationship-bearing, descriptive, classification, structural) and §1.5's
frontmatter table. A2 makes these tables NORMATIVE-CLOSED rather than illustrative —
that is the identity change, and it is the reason each entry below carries its SPEC
anchor: a reader must be able to check this list against the document it claims to
mirror without trusting this comment.
"""

from __future__ import annotations

import functools
from typing import FrozenSet

# --- SPEC §1.3 "Descriptive properties" -------------------------------------
DESCRIPTIVE_KEYS: FrozenSet[str] = frozenset({
    "definition", "role", "scope", "status", "rationale", "tags", "alias",
    "full-name", "problem-statement", "key-insight", "implication", "exception",
    "constraint",
    # Scenario nodes (§5.5), listed in the same §1.3 table
    "given", "when", "then", "and-when", "and-then",
})

# --- SPEC §1.3 "Classification properties" (document-level metadata) --------
CLASSIFICATION_KEYS: FrozenSet[str] = frozenset({
    "artifact-readiness", "announcement-status", "product-type", "confidentiality",
})

# --- SPEC §1.3 "Structural properties" --------------------------------------
STRUCTURAL_KEYS: FrozenSet[str] = frozenset({
    "child-document", "source-file", "line-range",
    # read by the RENDER layer (§4.9); descriptive, mint no edge
    "actor", "condition",
})

# --- SPEC §1.5 Frontmatter ---------------------------------------------------
# Frontmatter keys are also legal as node properties (§1.5: "Classification properties
# in frontmatter are inherited by all nodes in the document unless overridden inline" —
# an override is a node property with the same key, so the key must be legal there).
FRONTMATTER_KEYS: FrozenSet[str] = frozenset({
    "scope", "audience", "boundary", "not-this", "parent", "depends-on",
    "confidentiality", "announcement-status", "product-type", "artifact-readiness",
    "version-scope", "supersedes-version", "breaking-changes", "domain",
    # amendment A1 — the format-version marker itself
    "lts",
})

# --- SPEC §5 Flow / Scenario extended fields (§5.2-§5.4) ---------------------
FLOW_KEYS: FrozenSet[str] = frozenset({
    "impl", "impl-stale", "actors", "preconditions", "postconditions",
})

#: The A2 private-key marker. `x-` and not `_`, and the reason is measurable rather
#: than aesthetic: the key grammar is `^[a-z][a-z0-9_-]*$`, which REQUIRES a leading
#: letter — `_private` is itself an ILLEGAL KEY, so `_` would force a second amendment
#: to the grammar while `x-` stays inside it. (OpenAPI/HTTP precedent, secondary.)
PRIVATE_KEY_PREFIX = "x-"


def is_private_key(key: str) -> bool:
    """True iff `key` is an explicitly-marked private key (A2 point iii).

    ⛔ The marker must be followed by SOMETHING. A bare `x-` declares privacy about
    nothing; treating it as marked would let a typo (`x-` alone) buy the exemption the
    marker is supposed to make deliberate.
    """
    return key.startswith(PRIVATE_KEY_PREFIX) and len(key) > len(PRIVATE_KEY_PREFIX)


def minting_keys() -> FrozenSet[str]:
    """Every built-in edge-minting property key, READ FROM THE PARSER'S OWN TABLES."""
    from .parser import FLOW_PROPERTY_EDGES, REL_PROPERTY_EDGES
    return frozenset(REL_PROPERTY_EDGES) | frozenset(FLOW_PROPERTY_EDGES)


#: Edge types whose minting keys are never offered as a fuzzy suggestion. CANONICAL (a legacy node's
#: pointer to its canonical node) and, since 2026-10-07, the review-extension verbs ATTACHES_TO_HOOK /
#: APPLIES_RULE: measured on the registered corpora, adding `applies-rule` / `attaches-to-hook` rewrote the
#: "Nearest minting alternative(s)" text of two UNRELATED findings (`preserves`, `relates-to-spec`) on
#: acme-console, recommending a review-extension key for a decision-graph property.
SUGGESTION_EXCLUDED_EDGE_TYPES: FrozenSet[str] = frozenset({"CANONICAL", "ATTACHES_TO_HOOK", "APPLIES_RULE"})


def suggestion_excluded_keys() -> FrozenSet[str]:
    """Minting keys that must NEVER be offered as a fuzzy "did you mean" / "nearest minting
    alternative": those whose edge type is in `SUGGESTION_EXCLUDED_EDGE_TYPES` (CANONICAL: legacy -> canonical; the review-extension verbs). Following such a
    suggestion mints a semantically wrong edge, and adding the key must not rewrite the advice
    on unrelated existing findings (delta review F1). Derived from the table, never restated.
    """
    from .parser import REL_PROPERTY_EDGES
    return frozenset(k for k, v in REL_PROPERTY_EDGES.items()
                     if v[0] in SUGGESTION_EXCLUDED_EDGE_TYPES)


def core_keys() -> FrozenSet[str]:
    """The full core key vocabulary — SPEC §1.3's four tables + §1.5 + §5."""
    return (minting_keys() | DESCRIPTIVE_KEYS | CLASSIFICATION_KEYS
            | STRUCTURAL_KEYS | FRONTMATTER_KEYS | FLOW_KEYS)


# ---------------------------------------------------------------------------
# Edit-distance suggestion (A2 point 2)
# ---------------------------------------------------------------------------
# ⛔ WHY THIS IS PRECISE HERE AND WAS NOT BEFORE. A shape-heuristic "does this prose
# line look like a property?" check measured 96% FALSE POSITIVE on real corpora,
# because prose and properties are genuinely shape-identical. Membership in a CLOSED
# vocabulary is a different predicate entirely: `see-alos` is not near-a-property-shape,
# it is one character from a DECLARED NAME. Closure is what buys the precision; that is
# the argument for A2 stated as an engineering consequence rather than a preference.

_MAX_SUGGEST_DISTANCE = 2


def _levenshtein(a: str, b: str, cap: int) -> int:
    """Levenshtein distance, short-circuiting above `cap` (the answer is then `cap+1`).

    The cap is not just an optimisation: a distance of 7 and a distance of 40 are the
    same answer to the only question asked here ("is this a near-miss?"), and computing
    it exactly would invite a caller to rank far-apart keys as if the number meant
    something.
    """
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def suggest_keys(key: str, declared: FrozenSet[str],
                 max_distance: int = _MAX_SUGGEST_DISTANCE) -> list:
    """Declared keys within `max_distance` of `key`, nearest first then alphabetical.

    [PERF-SUGGEST-KEYS] Memoised on (key, declared, max_distance): the answer depends on
    nothing else, and a large corpus repeats the same few hundred undeclared keys tens of
    thousands of times across the authoring gate's re-parses (measured 2026-10-02: 1.9M
    `_levenshtein` calls, 94% of a 2.8 s add_node). Each call returns a FRESH list, so a
    caller mutating its result cannot poison the cache.
    """
    return list(_suggest_keys_cached(key, frozenset(declared), max_distance))


@functools.lru_cache(maxsize=65536)
def _suggest_keys_cached(key: str, declared: FrozenSet[str], max_distance: int) -> tuple:
    return tuple(_suggest_keys_uncached(key, declared, max_distance))


def _cache_clear():
    _suggest_keys_cached.cache_clear()
    _delete_index.cache_clear()


suggest_keys.cache_clear = _cache_clear   # for tests / reloads


def _suggest_keys_uncached(key: str, declared: FrozenSet[str],
                           max_distance: int = _MAX_SUGGEST_DISTANCE) -> list:
    """Declared keys within `max_distance` of `key`, nearest first then alphabetical.

    Returns `[]` when nothing is near — and an EMPTY result is meaningful, not a
    failure: it says "this is not a typo of anything we know", which is the case where
    the right advice is `declare it, mark it x-, or rewrite it` rather than a guess.
    Deterministic ordering, because a diagnostic that reorders between runs cannot be
    asserted on.
    """
    if max_distance <= _INDEX_MAX_DISTANCE and len(declared) >= _INDEX_MIN_VOCABULARY:
        index = _delete_index(declared, max_distance)
        pool = set()
        for d in _deletes(key, max_distance):
            pool.update(index.get(d, ()))
    else:                                # small vocabulary: the scan is cheaper
        pool = declared
    scored = []
    for cand in pool:
        d = _levenshtein(key, cand, max_distance)
        if d <= max_distance and d > 0:
            scored.append((d, cand))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [c for _, c in scored]


# [PERF-SUGGEST-INDEX] Exact symmetric-delete candidate index. If lev(a, b) <= k, deleting <= k
# characters from each of a and b reaches a common string (drop the substituted positions from
# both, the inserted ones from b, the deleted ones from a). So every true answer shares a
# <=k-delete with the key, and verifying each candidate with _levenshtein gives EXACTLY the
# brute-force answer while touching only the few keys that share a delete. Built once per
# declared vocabulary (a corpus has a handful). Above k=2 the delete sets grow too fast to pay.
_INDEX_MAX_DISTANCE = 2
# Below this many declared keys the scan beats generating the key's deletes. Measured
# 2026-10-02 on 2,000 real trace keys: scan wins at 64 and 128 keys, the index at 200+
# (613 keys: 0.225 s vs 0.999 s); real vocabularies today are 63, 64 and 613 keys.
_INDEX_MIN_VOCABULARY = 200


def _deletes(s: str, k: int) -> set:
    """`s` with every combination of up to `k` characters removed (including `s` itself)."""
    out, frontier = {s}, {s}
    for _ in range(k):
        frontier = {w[:i] + w[i + 1:] for w in frontier for i in range(len(w))}
        out |= frontier
    return out


@functools.lru_cache(maxsize=64)
def _delete_index(declared: FrozenSet[str], k: int) -> dict:
    index: dict = {}
    for cand in declared:
        for d in _deletes(cand, k):
            index.setdefault(d, []).append(cand)
    return index


__all__ = [
    "DESCRIPTIVE_KEYS", "CLASSIFICATION_KEYS", "STRUCTURAL_KEYS",
    "FRONTMATTER_KEYS", "FLOW_KEYS", "PRIVATE_KEY_PREFIX",
    "is_private_key", "minting_keys", "core_keys", "suggest_keys",
    "suggestion_excluded_keys",
]
