"""Property keys that MINT — the core additions and the profile-registrable capability.

⛔ NEGATIVE-CONTROL DISCIPLINE. Every test here was RUN AND OBSERVED FAILING against
the parser as it stood at c9267fe, BEFORE the fix that makes them pass. A check that has
only ever been observed passing is not a check (the epistemics rule rule-negative-control).

⛔ EVERY TEST ASSERTS THE EDGE IS IN THE PARSED GRAPH — never merely that the property
parsed. That distinction is the whole defect class: `relates-to` PARSED fine in 140 places
and minted nothing, while `lts_lint` called the corpus CLEAN.
"""
from __future__ import annotations

import pytest

from factlattice.parser import (
    FLOW_PROPERTY_EDGES,
    REL_PROPERTY_EDGES,
    parse_text,
)
from factlattice.profile import Profile, ProfileCollisionError, load_profile


def _edges(text: str, **kw):
    return parse_text(text, "t.lts", **kw).edges


def _edge_tuples(text: str, **kw):
    return {(e.source, e.type, e.target) for e in _edges(text, **kw)}


DOC = """# Doc [SPEC:DOC]

### [N-ALPHA] Alpha
- {key}: [N-BETA]

### [N-BETA] Beta
- definition: the other node
"""


# --------------------------------------------------------------------------
# HALF 1 + 2 — core, domain-INDEPENDENT keys added to REL_PROPERTY_EDGES
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key,etype,layer", [
    ("relates-to", "RELATED_TO", "semantic"),
    ("related-to", "RELATED_TO", "semantic"),
    ("spawns", "CAUSES", "causal"),
])
def test_forward_key_mints_edge_in_graph(key, etype, layer):
    """The natural spelling mints, in the FORWARD direction, into the parsed graph."""
    parsed = parse_text(DOC.format(key=key), "t.lts")
    matching = [e for e in parsed.edges
                if e.type == etype and e.inference == "property"]
    assert matching, f"`{key}` minted NO {etype} edge — the silent-loss class"
    e = matching[0]
    assert (e.source, e.target) == ("N-ALPHA", "N-BETA")
    assert e.layer == layer


@pytest.mark.parametrize("key,etype", [
    ("superseded-by", "SUPERSEDES"),
    ("verify", "TESTED_BY"),
    ("verified-by", "TESTED_BY"),
    ("follows", "BEFORE"),
])
def test_reverse_voice_key_mints_reversed_edge(key, etype):
    """⛔ The REVERSE-VOICE assertion. `superseded-by: [B]` on A means B SUPERSEDES A.

    Getting the direction wrong here is a SUBSTITUTION, not a loss — the graph would
    answer traversals confidently and backwards.
    """
    parsed = parse_text(DOC.format(key=key), "t.lts")
    matching = [e for e in parsed.edges if e.type == etype]
    assert matching, f"`{key}` minted NO {etype} edge"
    e = matching[0]
    assert (e.source, e.target) == ("N-BETA", "N-ALPHA"), (
        f"`{key}` minted {e.source}->{e.target}; the reverse voice must run "
        f"referenced_id -> node"
    )


REAL_CORPUS_VALUES = [
    # ⛔ COPIED VERBATIM OUT OF LIVE `.lts` FILES, not invented. A control written with a
    # synthetic uppercase target (`[SOME-FIXTURE]`) passes while the real corpus mints
    # nothing — a negative control sharing the defect's own blind spot.
    ("relates-to", "[ACME-ICP-S6-SCROLLDIR]", 1),
    ("relates-to", "[DEC-INSTANCE-KEYED-HOMES], [DEC-MEMORY-SCOPING-BY-CWD]", 2),
    ("verify", "[ACME-GV-VERIFY-DATA-PATH]", 1),
    ("spawns", "[DEC-RESUME-CWD-DISCOVERY-SCOPE], [DEC-MEMORY-SCOPING-BY-CWD]", 2),
    ("spawns", "[SPEC:SESSION-BOOTSTRAP-READ]", 1),
    ("superseded-by", "[DEC-CANVAS-TAB-RETIRED]", 1),
]


@pytest.mark.parametrize("key,value,expected", REAL_CORPUS_VALUES)
def test_real_corpus_values_actually_mint(key, value, expected):
    """The additions are proven against REAL authored values, including the multi-ref
    and `[DOCTYPE:ID]` forms the corpora actually use."""
    text = f"# Doc [SPEC:DOC]\n\n### [N-ALPHA] Alpha\n- {key}: {value}\n"
    minted = [e for e in parse_text(text, "t.lts").edges if e.inference == "property"]
    assert len(minted) == expected, (
        f"`{key}: {value}` minted {len(minted)} edges, expected {expected}")


def test_unresolvable_id_shape_is_a_SEPARATE_unfixed_loss():
    """⛔ THE CONTROL THAT STOPS A FALSE 'FIXED'.

    `re-derive-via` (103 uses in registered corpora) was deliberately NOT registered,
    because registering it would change NOTHING: its targets are shaped
    `[FIXTURE-registration-protocol-P1]`, and `_UPPER_ID_RE` (`^[A-Z][A-Z0-9_-]*$`)
    rejects a mixed-case segment, so `_resolve_ref` returns None. 111 of 112 bracketed
    targets do not resolve. `stage-evidence` (40 uses, `[COMMIT-<lowercase-sha>]`) is
    the same class.

    This test PINS that second loss as still-open. It fails the day someone widens
    `_UPPER_ID_RE` or normalizes the corpus — which is exactly when this comment, and
    the decision behind it, need re-reading.
    """
    from factlattice.parser import _extract_refs
    assert _extract_refs("[FIXTURE-registration-protocol-P1]") == []
    assert _extract_refs("committed -> [COMMIT-09b80e9]") == []
    # ... while a well-shaped id in the same position resolves fine:
    assert _extract_refs("[FIXTURE-ACME-SOURCE-BOUNDARY]") == [(None, "FIXTURE-ACME-SOURCE-BOUNDARY")]

    # And therefore: even if the key WERE registered, the corpus value mints nothing.
    text = ("# Doc [SPEC:DOC]\n\n### [N-ALPHA] Alpha\n"
            "- re-derive-via: [FIXTURE-registration-protocol-P1]\n")
    prof = load_profile({
        "domain": {"id": "d", "namespaces": ["d"]},
        "edge_types": [{"type": "RE_DERIVED_VIA", "property_key": "re-derive-via",
                        "layer": "semantic"}],
    })
    minted = [e for e in parse_text(text, "t.lts",
                                    property_edges=prof.property_edges).edges
              if e.inference == "property"]
    assert minted == [], (
        "registering the key appeared to fix re-derive-via — it does not; the id shape "
        "is the second, still-open half of that loss")


def test_relates_to_and_see_also_are_indistinguishable_in_the_graph():
    """The two spellings are synonyms; the graph must not be able to tell them apart."""
    a = _edge_tuples(DOC.format(key="relates-to"))
    b = _edge_tuples(DOC.format(key="see-also"))
    assert a == b, "relates-to and see-also must produce an identical edge set"


def test_prose_bearing_keys_still_mint_nothing():
    """⛔ THE ANTI-OVER-REACH CONTROL, and it is as load-bearing as the additions.

    `rationale`/`definition`/`requirement` cite node ids inside SENTENCES. Measured over
    the acme corpora: 0% of their occurrences are pure reference lists. Minting them
    would turn every inline citation into a graph edge and destroy traversal's meaning.
    """
    text = """# Doc [SPEC:DOC]

### [N-ALPHA] Alpha
- rationale: chosen because [N-BETA] already owns the debounce, so the leaf pays once
- definition: the widget described in [N-BETA]
- requirement: MUST NOT regress [N-BETA]

### [N-BETA] Beta
- definition: the other node
"""
    property_edges = [e for e in parse_text(text, "t.lts").edges
                      if e.inference == "property"]
    assert property_edges == [], (
        "a prose property minted an edge — inline citations are not relationships"
    )


# --------------------------------------------------------------------------
# HALF 3 — profile-REGISTRABLE edge-bearing property keys
# --------------------------------------------------------------------------

EXEMPLIFIES_PROFILE = {
    "domain": {"id": "lib.ops", "namespaces": ["lib.ops"]},
    "edge_types": [
        {"type": "EXEMPLIFIES", "property_key": "exemplifies",
         "layer": "semantic", "tier": "domain"},
    ],
}


def test_profile_registered_property_key_mints():
    """The maintainer's ruling, 2026-08-15: a domain profile CAN register its own edge keys.

    `exemplifies` is Acme domain vocabulary — it must NOT be hardcoded into the
    core parser (that would reverse 5d546a9's domain-agnosticism restoration). It is
    registrable instead.
    """
    prof = load_profile(EXEMPLIFIES_PROFILE)
    assert prof.property_edges["exemplifies"] == ("EXEMPLIFIES", "semantic", False)

    parsed = parse_text(DOC.format(key="exemplifies"), "t.lts",
                        property_edges=prof.property_edges)
    minted = [e for e in parsed.edges if e.type == "EXEMPLIFIES"]
    assert minted, "a profile-registered property key minted nothing"
    assert (minted[0].source, minted[0].target) == ("N-ALPHA", "N-BETA")


def test_profile_registered_key_is_inert_without_the_profile():
    """The NEGATIVE half: with no profile, the same document mints nothing.

    This is what proves the edge above came from the REGISTRATION and not from some
    other code path quietly harvesting brackets.
    """
    parsed = parse_text(DOC.format(key="exemplifies"), "t.lts")
    assert [e for e in parsed.edges if e.type == "EXEMPLIFIES"] == []


def test_profile_may_register_a_reversed_key():
    prof = load_profile({
        "domain": {"id": "d", "namespaces": ["d"]},
        "edge_types": [{"type": "GOVERNS", "property_key": "governed-by",
                        "layer": "semantic", "reversed": True}],
    })
    parsed = parse_text(DOC.format(key="governed-by"), "t.lts",
                        property_edges=prof.property_edges)
    e = [x for x in parsed.edges if x.type == "GOVERNS"][0]
    assert (e.source, e.target) == ("N-BETA", "N-ALPHA")


# --- the collision discipline: a profile must never be able to make the platform LIE ---

@pytest.mark.parametrize("builtin", ["part-of", "supersedes", "depends-on", "next-step"])
def test_profile_redefining_a_builtin_key_is_a_LOUD_REFUSAL(builtin):
    """⛔ THE SUBSTITUTION GUARD.

    `part-of` has 1351 uses. A profile silently remapping it to another type or
    direction would not lose data — it would make every existing traversal answer
    CONFIDENTLY AND WRONGLY, which is strictly the worst outcome in this codebase.
    Collision is refused at LOAD, naming the key and both definitions. Never last-wins.
    """
    with pytest.raises(ProfileCollisionError) as ei:
        load_profile({
            "domain": {"id": "d", "namespaces": ["d"]},
            "edge_types": [{"type": "MY_TYPE", "property_key": builtin,
                            "layer": "semantic"}],
        })
    msg = str(ei.value)
    assert builtin in msg, "the refusal must name the colliding key"
    assert "MY_TYPE" in msg, "the refusal must name the profile's definition"
    builtin_type = (REL_PROPERTY_EDGES.get(builtin) or FLOW_PROPERTY_EDGES[builtin])[0]
    assert builtin_type in msg, "the refusal must name the built-in definition"


def test_profile_may_not_even_redefine_a_builtin_identically():
    """A no-op redefinition is still refused — otherwise the rule has an exception
    whose boundary every future author must re-derive."""
    with pytest.raises(ProfileCollisionError):
        load_profile({
            "domain": {"id": "d", "namespaces": ["d"]},
            "edge_types": [{"type": "PART_OF", "property_key": "part-of",
                            "layer": "semantic"}],
        })


def test_cross_profile_collision_is_a_loud_refusal():
    """⛔ `corpora/acme-specs/` is a root of THREE registered corpora. Two profiles
    disagreeing about one key is a LIVE scenario, not a hypothetical."""
    from factlattice.profile import merge_property_edges

    a = load_profile({"domain": {"id": "a", "namespaces": ["a"]},
                      "edge_types": [{"type": "USES", "property_key": "uses",
                                      "layer": "semantic"}]})
    b = load_profile({"domain": {"id": "b", "namespaces": ["b"]},
                      "edge_types": [{"type": "CONSUMES", "property_key": "uses",
                                      "layer": "entity"}]})
    with pytest.raises(ProfileCollisionError) as ei:
        merge_property_edges([a, b])
    msg = str(ei.value)
    assert "uses" in msg and "USES" in msg and "CONSUMES" in msg
    assert "a" in msg and "b" in msg, "the refusal must name both profiles"


def test_cross_profile_agreement_is_allowed():
    """Two profiles registering the SAME key identically is not a conflict — the
    acme-specs shared root makes this the normal case, not an edge case."""
    from factlattice.profile import merge_property_edges

    spec = {"type": "USES", "property_key": "uses", "layer": "semantic"}
    a = load_profile({"domain": {"id": "a", "namespaces": ["a"]}, "edge_types": [spec]})
    b = load_profile({"domain": {"id": "b", "namespaces": ["b"]}, "edge_types": [spec]})
    assert merge_property_edges([a, b])["uses"] == ("USES", "semantic", False)


def test_profile_may_not_register_a_key_that_is_not_key_shaped():
    """A key that cannot parse as a property key would register a mint that can never
    fire — an inert registration that reads as a working one."""
    with pytest.raises(ProfileCollisionError):
        load_profile({
            "domain": {"id": "d", "namespaces": ["d"]},
            "edge_types": [{"type": "T", "property_key": "Not A Key",
                            "layer": "semantic"}],
        })


# --------------------------------------------------------------------------
# THE TRAP THE COORDINATOR NAMED — binding.MINTING_KEYS going partially inert
# --------------------------------------------------------------------------

def test_binding_minting_keys_still_equals_the_builtin_union():
    """The pre-existing invariant (tests/test_repair_and_binding.py:281) is UNCHANGED:
    the module constant is the BUILT-IN baseline."""
    from factlattice_engine import binding
    assert set(binding.MINTING_KEYS) == set(REL_PROPERTY_EDGES) | set(FLOW_PROPERTY_EDGES)


def test_binding_resolves_profile_registered_keys():
    """⛔ THE FIX MUST NOT REGENERATE THE DEFECT ONE LAYER UP.

    If MINTING_KEYS stayed static while registered keys became dynamic, the
    contradiction checker's check D would go inert for exactly the keys a profile
    added — the same silent shape as the `relates-to` defect this branch fixes.
    """
    from factlattice_engine import binding

    prof = load_profile(EXEMPLIFIES_PROFILE)
    keys = binding.minting_keys_for(prof)
    assert "exemplifies" in keys, "check D is INERT for profile-registered keys"
    assert keys["exemplifies"] == ("EXEMPLIFIES", "semantic", False)
    # and the built-ins are still all there
    assert set(REL_PROPERTY_EDGES) <= set(keys)


def test_binding_reports_a_profile_registered_key_as_binding():
    """The write receipt must say a registered key BINDS — otherwise the authoring
    surface still tells authors their edge did nothing."""
    from factlattice_engine import binding

    prof = load_profile(EXEMPLIFIES_PROFILE)
    res = binding.analyse("exemplifies", "[N-BETA]", node_id="N-ALPHA", profile=prof)
    assert res["key_mints"] is True
    assert res["edges_minted"], "a registered key reported zero minted edges"
    assert res["edges_minted"][0]["type"] == "EXEMPLIFIES"


def test_binding_without_profile_reports_the_key_as_non_minting():
    """The negative half of the receipt claim."""
    from factlattice_engine import binding
    res = binding.analyse("exemplifies", "[N-BETA]", node_id="N-ALPHA")
    assert res["key_mints"] is False
