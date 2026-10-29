"""Does the property BIND? — the one field the write receipt was still missing.

[FL-RECALL-PROPOSALS] proposal-5b, measured live on 2026-08-13:

    ⛔ `addresses`, carrying TWO bracketed node references, minted ZERO edges and
       returned `warnings: []`.

    ⇒ "The write surface now guarantees a property LANDS; it does not yet say that the
       property BINDS NOTHING."

That single field would have prevented [FLCD-CONNECTIVITY], its independent Acme
replication three days later, and 584 unregistered property keys in one corpus. It is a
lookup against a fifteen-entry table on a tool that already does whole-file parse-delta
gating — **the hard half is built and the easy half was open.**

WHY IT IS SILENT TODAY, AND WHY THAT IS NOT A BUG IN THE PARSER
    The minting table (`parser.REL_PROPERTY_EDGES` + `parser.FLOW_PROPERTY_EDGES`) is a
    fixed list of names. A property key not in it is a perfectly legal DESCRIPTIVE
    property, and bracketed ids inside a descriptive value are perfectly legal prose —
    §8.1's fields quote node ids in prose on purpose, and harvesting them would mint
    edges nobody authored. The parser is right to say nothing.

    The AUTHOR is the one who cannot tell. `stage-evidence` was used 21 times in one
    corpus and 63 in another, minting nothing, while `used-by` and `tested-by` — which DO
    mint — sat at zero uses. Nobody chose that. The expressive name is the one that
    carries meaning to a reader, so it is the one an author reaches for, and the minting
    table is not discoverable from the authoring surface.

SO THIS IS A RECEIPT FIELD, NOT A RULE. It never refuses, never rewrites, and never
guesses. It reports three things about the value the parser produced:

    edges_minted                     the edges this property actually created
    references_seen_but_not_minted   bracketed ids in the value that became nothing,
                                     WITH the reason
    nearest_minting_alternatives     keys that WOULD have minted, ranked by name
                                     similarity then by table order

⛔ THE DIRECTIONAL TRAP IS NAMED EXPLICITLY. `supersedes` mints and `superseded-by` does
not; `used-by` mints and `uses` does not. The author's choice of VOICE decides whether
the edge exists, and two independent teams hit that in three days. When a near-miss is
the INVERSE VOICE of a minting key, the receipt says so in as many words, because
"nearest alternative: used-by" is not the same help as "you wrote the passive voice of a
key that mints".
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional, Tuple

from factlattice.parser import (
    FLOW_PROPERTY_EDGES,
    REL_PROPERTY_EDGES,
    _BRACKET_RE,
    _property_refs,
    classify_bracket_token,
    effective_domain_verbs,
    rel_property_edge_map,
)

# Every key that mints, with what it mints. ONE derived table, read from the parser's own
# two tables — a third hand-written copy here is how a receipt starts telling authors
# about a vocabulary the parser stopped having.
MINTING_KEYS: Dict[str, Tuple[str, str, bool]] = {
    **{k: (v[0], v[1], v[2]) for k, v in REL_PROPERTY_EDGES.items()},
    **{k: (v[0], v[1], v[2]) for k, v in FLOW_PROPERTY_EDGES.items()},
}

# The measured trap, by name. Left is what authors write; right is what mints. Derived
# where it can be (`X-by` <-> `X`) and enumerated where the pairing is lexical rather
# than morphological. Used ONLY to sharpen the explanation — a key not here still gets
# its similarity ranking.
INVERSE_VOICE: Dict[str, str] = {
    # "superseded-by" REMOVED 2026-08-15 — it now MINTS (reversed SUPERSEDES).
    "resolved-by": "resolves",
    "implemented-by": "implements",
    "uses": "used-by",
    "tests": "tested-by",
    "depended-on-by": "depends-on",
    "required-by": "depends-on",
    "part-of-by": "part-of",
    "contains": "part-of",
    "issued-by": "issuer",
}

# ⛔ SEMANTIC NEIGHBOURS, BECAUSE STRING SIMILARITY IS THE WRONG RANKER FOR A SEMANTIC
# QUESTION. Measured: `relates-to` ranks `resolves`/`converges-to`/`depends-on` on edit
# distance and the right answer is `see-also` (RELATED_TO), which scores near zero.
#
# Every entry is an OBSERVED key from a live corpus, not an invention:
#   `relates-to`      — `authoring/schema.py`'s own docstring: "on 2026-08-12 I used
#                       `relates-to` in the very node documenting that relationship-shaped
#                       properties mint nothing… Converting to `see-also` fixed it."
#   `stage-evidence`  — 21 nodes / 33 refs in one corpus, 63 uses in another, minting
#                       nothing: the DOMINANT term in both independent measurements.
#   `addresses`, `re-derive-via`, `born-from` — named in the same `used_but_unregistered`
#                       drift list (584 keys, `declared_but_unused: 0`).
#
# ⛔ IT IS A SUGGESTION, NEVER A RENAME. `stage-evidence` in particular should usually be
# KEPT — it carries WHICH STAGE, which no bare edge key can — and the ruled remedy is the
# DUAL FORM: keep the key AND declare the relationship in `## Relationships`. The summary
# says exactly that; this table only stops the receipt from being silent.
SEMANTIC_NEIGHBOURS: Dict[str, Tuple[str, ...]] = {
    # ⛔ `relates-to` and `related-to` WERE here, pointing at `see-also`. Both became
    # MINTING KEYS on 2026-08-15, so they can never reach the suggestion branch again;
    # leaving them would be a dead entry that reads as live guidance. `superseded-by`
    # left `INVERSE_VOICE` for the same reason. When a key graduates from "near miss"
    # to "mints", it must be REMOVED from these tables, or the receipt starts advising
    # authors to rename a key that already works.
    "see": ("see-also",),
    "stage-evidence": ("tested-by", "see-also"),
    "evidence": ("tested-by", "see-also"),
    "addresses": ("resolves", "see-also"),
    "answers": ("resolves",),
    "fixes": ("resolves",),
    "re-derive-via": ("see-also",),
    "born-from": ("depends-on", "see-also"),
    "derived-from": ("depends-on",),
    "blocks": ("depends-on",),
    "blocked-by": ("depends-on",),
    "realizes": ("implements",),
    "satisfies": ("implements",),
    "owner": ("issuer",),
    "owned-by": ("issuer",),
}

_SUGGESTION_CUTOFF = 0.45
_MAX_SUGGESTIONS = 3


def minting_keys_for(profile: Any = None) -> Dict[str, Tuple[str, str, bool]]:
    """The EFFECTIVE minting table for a profile — built-ins plus its registered keys,
    on EITHER registration surface.

    ⛔ THIS FUNCTION EXISTS SO THE FIX DOES NOT REGENERATE THE DEFECT ONE LAYER UP.
    `MINTING_KEYS` is the static BUILT-IN baseline (and stays exactly
    `set(REL_PROPERTY_EDGES) | set(FLOW_PROPERTY_EDGES)`, as
    `tests/test_repair_and_binding.py:281` requires). Once the maintainer's 2026-08-15 ruling let
    profiles register their own edge keys, every consumer that kept reading the static
    constant would go PARTIALLY INERT for exactly the keys a profile added — the
    contradiction checker's check D would stop firing, silently, which is the same
    failure shape as the `relates-to` defect this module was written about.

    [FL-VERB-PARITY] 2026-09-23 UNIFY: a profile has TWO ways to register a verb —
    `relationship_verbs` (`Profile.domain_verbs`, the triple surface) and
    `edge_types[].property_key` (`Profile.property_edges`, the property surface). The
    parser now derives the property surface from the FULL, unified verb set
    (`parser.effective_domain_verbs` + `parser.rel_property_edge_map`) at E4 — this
    function must read the SAME derivation, or a verb registered only via
    `relationship_verbs` would mint in the graph while `is_minting_key`/
    `classify_edge_key` (and therefore the write receipt and the contradiction
    checker's check D) kept calling its property spelling "undeclared". Explicit
    `property_edges` registrations are applied LAST, matching the parser's own
    precedence, so a non-kebab spelling a profile chose explicitly still wins.

    So: read the constant for "what does the platform ship", call THIS for "what mints
    in this corpus".

    ⛔ [2026-09-23 cold-audit correction] An earlier version of
    this comment claimed `minting_keys_for(None)` now differs from `MINTING_KEYS` because
    `rel_property_edge_map()` derives a kebab property key for EVERY core verb
    unconditionally. That derivation was a HIGH-severity over-recognition defect (a
    common-English-word core verb used in prose, e.g. `- sequence: [X] describes a
    generic display device...`, minted a spurious edge) and has been REMOVED — see
    `parser.rel_property_edge_map`'s own docstring and the comment above
    `parser.TRIPLE_ONLY_VERBS`. A bare core verb now gets a property surface ONLY via an
    explicit `REL_PROPERTY_EDGES` / `REL_PROPERTY_ALIASES` entry, so
    `minting_keys_for(None)` is once again (for bare core verbs) the same set
    `MINTING_KEYS` names; the two can still diverge when a PROFILE is passed, because a
    profile's registered verbs (`domain_verbs` / `property_edges`) still get an
    unconditional surface — a profile author registering a verb for this corpus IS the
    vetting act, unlike a bare core verb nobody chose here. A caller wanting the static
    built-in table (e.g. the `set(REL_PROPERTY_EDGES) | set(FLOW_PROPERTY_EDGES)`
    invariant test) still reads `MINTING_KEYS` directly.
    """
    domain_verbs: frozenset = frozenset()
    registered: Dict[str, Tuple[str, str, bool]] = {}
    profile_layers: Optional[Dict[str, str]] = None
    if profile is not None:
        domain_verbs = frozenset(getattr(profile, "domain_verbs", None) or ())
        registered = dict(getattr(profile, "property_edges", None) or {})
        profile_layers = getattr(profile, "edge_types", None) or None
    unified_verbs = effective_domain_verbs(domain_verbs, registered)
    keys: Dict[str, Tuple[str, str, bool]] = {
        k: (v[0], v[1], v[2]) for k, v in FLOW_PROPERTY_EDGES.items()
    }
    keys.update(rel_property_edge_map(unified_verbs, profile_layers))
    keys.update(registered)
    return keys


def is_minting_key(key: str, profile: Any = None) -> bool:
    return key in minting_keys_for(profile)


# ---------------------------------------------------------------------------
# [V12b / the maintainer's rulings 2026-08-17] The edge-verb CLASSIFIER — the ONE place the
# case-variant-vs-genuinely-undeclared split is decided, so lint (V12) and preview
# (V12b) can never disagree about which channel a key belongs in.
# ---------------------------------------------------------------------------
def canonicalize_edge_key(key: str) -> str:
    """Casefold + unify separators, so `Depends-On`, `DEPENDS_ON` and `depends_on` all
    collapse onto the SAME canonical form as the declared `depends-on`.

    This is the ONE normalisation the case-vs-undeclared split is decided by; a second
    copy of it living in the linter and another in the preview surface is exactly the
    drift the shared predicate exists to prevent.
    """
    return key.strip().casefold().replace("_", "-")


def classify_edge_key(key: str, profile: Any = None) -> Tuple[str, Optional[str]]:
    """Classify an edge-shaped property KEY against the EFFECTIVE minting vocabulary —
    core §1.3 relationship verbs UNION this profile's registered `property_edges`
    (The maintainer's ruling 1: "core verbs as defined in the spec, UNION any corpus-defined
    extensions"). Returns `(kind, canonical)`:

      ("declared",     key)          the key mints as written; belongs in NEITHER alert.
      ("case_variant", declared_key) the key differs from a declared verb ONLY by case
                                     or separator (`Depends-On`/`depends_on` ->
                                     `depends-on`) — a trivially-fixable CASE error
                                     (ruling 2a). It gets its OWN low-noise channel and
                                     MUST NOT land in the unknown-verb alert. Per the maintainer's
                                     2026-08-17 amendment it is WARNed, not silently
                                     minted, so the author corrects the source.
      ("undeclared",   None)         no declared verb matches even under the canonical
                                     form: a genuinely-unknown verb (ruling 2b), the
                                     `undeclared_edge_verbs` positive alert.
    """
    declared = minting_keys_for(profile)
    if key in declared:
        return ("declared", key)
    canon = canonicalize_edge_key(key)
    for d in declared:
        if canonicalize_edge_key(d) == canon:
            return ("case_variant", d)
    return ("undeclared", None)


def nearest_minting_alternatives(key: str, *, limit: int = _MAX_SUGGESTIONS) -> List[str]:
    """Minting keys closest to `key`, closest first, inverse-voice partner FIRST.

    The inverse-voice partner is promoted unconditionally rather than left to the ratio
    cut: `superseded-by` vs `supersedes` scores well, but `contains` vs `part-of` scores
    zero and is exactly the substitution an author makes on purpose.
    """
    from factlattice.core_keys import suggestion_excluded_keys
    _excluded = suggestion_excluded_keys()     # CANONICAL keys are never auto-suggested (F1)
    ranked = difflib.get_close_matches(key, sorted(set(MINTING_KEYS) - _excluded), n=limit,
                                       cutoff=_SUGGESTION_CUTOFF)
    # Semantic neighbours FIRST, then the inverse-voice partner ahead of even those:
    # a directional trap is the sharpest, most specific thing the receipt can say.
    for neighbour in reversed(SEMANTIC_NEIGHBOURS.get(key, ())):
        if neighbour in MINTING_KEYS and neighbour not in _excluded:
            ranked = [neighbour] + [r for r in ranked if r != neighbour]
    partner = INVERSE_VOICE.get(key)
    if partner and partner in MINTING_KEYS:
        ranked = [partner] + [r for r in ranked if r != partner]
    return ranked[:limit]


# ---------------------------------------------------------------------------
# [C2] is_pure_reference_list — the ONE predicate `lint/unminted.py` is built on.
# ---------------------------------------------------------------------------
# THE DISCRIMINATOR THIS PROJECT ALREADY USED BY HAND (REL_PROPERTY_EDGES's own
# comment block in `factlattice/parser.py`, applied to seven keys on 2026-08-15),
# made reusable. "The value contains a bracketed id" is NOT the test -- `rationale`
# (96 occurrences), `definition` (94), `requirement` (58) all carry bracketed ids
# and are ~0% pure: they cite node ids INSIDE SENTENCES. The test is whether the
# VALUE IS (near enough) NOTHING BUT references.
_MAX_GLOSS_CHARS = 40
# A separator segment: whitespace, commas/semicolons/slashes/ampersands, "and"/"or".
# Matches "", ", ", " and ", "; " ... anything a list of refs is plausibly joined by.
_SEPARATOR_RE = re.compile(r"^[\s,;/&]*(?:and|or)?[\s,;/&]*$", re.IGNORECASE)


def is_pure_reference_list(value: Any) -> bool:
    """Is this property VALUE a PURE REFERENCE LIST -- one or more bracketed refs
    joined only by separators, with at most ONE short trailing gloss -- as opposed to
    PROSE THAT CITES a node id inside a sentence?

    `[A], [B]`, `[A] and [B] (see note)`, `[FIXTURE-acme-source-boundary]` (a
    MALFORMED-REF -- see below) are all pure. `as stated in [A], we require ...` is
    not: the text BEFORE the first bracket is neither a separator nor a short trailing
    gloss, so the value fails on its first segment.

    A MALFORMED-REF-shaped bracket (`classify_bracket_token(...) == "MALFORMED-REF"`)
    counts as a "reference" here ON PURPOSE: `re-derive-via`'s
    `[FIXTURE-acme-source-boundary]` targets are exactly this shape, and they are
    exactly the class C2 exists to surface -- an author who wrote what READS as a
    pure reference list, in a key nobody minted. A PROSE-BRACKET anywhere in the
    value disqualifies the WHOLE value: a value that mixes a genuine/attempted ref
    with an ordinary prose bracket is prose that cites, not a list.

    Deliberately a SHAPE predicate, not a semantic one: it says nothing about
    whether the key SHOULD mint (that is `binding.is_minting_key`'s question) or
    whether the refs RESOLVE (that is the corpus's business, elsewhere). Two
    orthogonal questions asked by two functions is how this project avoids the
    single-giant-heuristic failure mode every other near-miss rule here warns about.
    """
    items = value if isinstance(value, list) else [value]
    items = [i for i in items if isinstance(i, str) and i.strip()]
    if not items:
        return False
    saw_ref = False
    for item in items:
        last_end = 0
        segments: List[str] = []
        matched = False
        for m in _BRACKET_RE.finditer(item):
            segments.append(item[last_end:m.start()])
            last_end = m.end()
            cls = classify_bracket_token(m.group(1))
            if cls in ("REF", "MALFORMED-REF"):
                matched = True
                saw_ref = True
            else:
                return False   # a PROSE-BRACKET anywhere disqualifies the value
        segments.append(item[last_end:])
        if not matched:
            return False       # no bracket at all in this item: not a reference list
        last_i = len(segments) - 1
        for i, seg in enumerate(segments):
            s = seg.strip(" \t")
            if not s or _SEPARATOR_RE.match(s):
                continue
            if i == last_i and len(s) <= _MAX_GLOSS_CHARS:
                continue        # one short trailing gloss, tolerated
            return False
    return saw_ref


def _format_ref(domain: Optional[str], node_id: str) -> str:
    return f"{domain}:{node_id}" if domain else node_id


def analyse(key: str, parsed_value: Any, *, node_id: str = "<node>",
            profile: Any = None) -> Dict[str, Any]:
    """The binding half of a write receipt for ONE property.

    `parsed_value` is the value AS THE PARSER PRODUCED IT — never the submitted string —
    so the reference extraction here runs the same `_property_refs` the edge minter runs,
    over the same object. A second reference grammar would be a second thing that can
    disagree with the graph.

    `profile` supplies the corpus's registered property-edge keys. Omit it and the
    receipt answers for the BUILT-INS ONLY — which, for a corpus whose profile registers
    keys, is a confidently wrong "this binds nothing".
    """
    refs = _property_refs(parsed_value)
    minted = minting_keys_for(profile).get(key)

    if minted is not None:
        etype, layer, reversed_dir = minted
        edges = []
        for domain, tid in refs:
            src, tgt = ((tid, node_id) if reversed_dir else (node_id, tid))
            edges.append({"source": src, "target": tgt, "type": etype, "layer": layer,
                          "target_domain": domain, "inference": "property"})
        return {
            "key_mints": True,
            "edges_minted": edges,
            "references_seen_but_not_minted": [],
            "reason": None,
            "nearest_minting_alternatives": [],
            "binding_summary": (
                f"`{key}` is a relationship-bearing key: it minted {len(edges)} "
                f"{etype} edge(s) on the {layer} layer."
                if edges else
                f"`{key}` is a relationship-bearing key but the value carries no "
                f"bracketed [ID] reference, so it minted no edge."),
        }

    unminted = [_format_ref(d, t) for d, t in refs]
    alternatives = nearest_minting_alternatives(key) if unminted else []
    partner = INVERSE_VOICE.get(key)
    reason = None
    summary = f"`{key}` is a DESCRIPTIVE property: stored and queryable, minting no edge."
    if unminted:
        # `reason` is a fixed CODE (it used to interpolate the caller's key); the prose lives in `binding_summary`
        reason = "key-not-in-minting-table"
        explanation = (f"the key `{key}` is not in the minting table "
                       f"(parser.REL_PROPERTY_EDGES / parser.FLOW_PROPERTY_EDGES), so every "
                       f"bracketed id in its value is stored as text and the graph never "
                       f"sees the relationship")
        summary = (f"⚠ `{key}` carries {len(unminted)} bracketed reference(s) and minted "
                   f"ZERO edges — {explanation}.")
        if partner and partner in MINTING_KEYS:
            summary += (f" ⛔ `{key}` is the INVERSE VOICE of `{partner}`, which DOES "
                        f"mint: the choice of voice, not the meaning, is what decides "
                        f"whether the edge exists.")
        if alternatives:
            summary += (" Keys that would have minted: "
                        + ", ".join(f"`{a}`" for a in alternatives) + ".")
        summary += (" Keeping this key is legitimate — it may carry information no bare "
                    "edge can — but then declare the relationship in `## Relationships` "
                    "as well, or the graph is missing it.")
    return {
        "key_mints": False,
        "edges_minted": [],
        "references_seen_but_not_minted": unminted,
        "reason": reason,
        "nearest_minting_alternatives": alternatives,
        "inverse_voice_of": partner if partner in MINTING_KEYS else None,
        "binding_summary": summary,
    }


def analyse_many(properties: Dict[str, Any], *, node_id: str) -> Dict[str, Any]:
    """`analyse` folded over a whole property map — the `lts_add_node` receipt.

    The per-key detail is kept (an author needs to know WHICH key lost the reference),
    and the roll-up carries the two numbers a reader will quote.
    """
    per_key = {k: analyse(k, v, node_id=node_id) for k, v in properties.items()}
    minted = [e for r in per_key.values() for e in r["edges_minted"]]
    unminted = sorted({f"{k}: {ref}" for k, r in per_key.items()
                       for ref in r["references_seen_but_not_minted"]})
    return {
        "edges_minted": minted,
        "references_seen_but_not_minted": unminted,
        "by_key": per_key,
        "binding_summary": (
            f"{len(minted)} edge(s) minted across {len(properties)} propert(ies)"
            + (f"; ⚠ {len(unminted)} bracketed reference(s) minted NOTHING"
               if unminted else "")),
    }


__all__ = ["MINTING_KEYS", "INVERSE_VOICE", "analyse", "analyse_many",
           "is_minting_key", "nearest_minting_alternatives", "is_pure_reference_list",
           "canonicalize_edge_key", "classify_edge_key", "minting_keys_for"]
