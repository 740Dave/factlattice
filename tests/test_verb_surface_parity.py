"""[FL-VERB-PARITY] Every relationship verb the vocabulary registers, × every position.

⭐ THE FINDING THIS FILE ENCODES IS NOT THE BUG — IT IS WHY THE BUG SURVIVED.

Measured 2026-08-13 against the shipped `control.expected.json`:

    see-also     2 occurrences as a property key  -> works
    depends-on   1 occurrence  as a property key  -> works
    part-of / relates-to / uses / upholds / documents / implements : ZERO

Its own case names admit the sampling — "an edge-bearing key from REL_PROPERTY_EDGES
(see-also)" and "a SECOND REL_PROPERTY_EDGES key (depends-on)". The author hand-listed two
members of a table the engine itself enumerates. Both worked, so the suite was green
forever, while across 80 files of a live deployment `relates-to` (342 refs), `documents`
(44), `upholds` (17), `uses` (14) and `exemplifies` (6) parsed CLEANLY as properties and
minted ZERO edges — with `lts_lint` and `lts_preview` both reporting clean, because a
property that parses is not an error.

"Is there a nineteenth silent-loss class?" is genuinely unknowable. ⛔ "Does every verb the
vocabulary registers actually mint in every position it has?" is FINITE, DERIVABLE FROM A
FILE, and nobody asked it.

⛔ THEREFORE THIS FILE HAND-LISTS NOTHING. Verbs come from `parser.CORE_EDGE_LAYER` and
from a profile's `relationship_verbs` read off disk through `load_profile`; property
spellings come from `parser.rel_property_edge_map()`, the SAME derivation E4 runs. The
cells come from `factlattice.conformance`, so the next construct family is a generator
registration, not another bespoke test.

⛔ A CELL THAT DOES NOT MINT IS A FAILURE, NEVER A SKIP. The only sanctioned gap is an
entry in `parser.TRIPLE_ONLY_VERBS` carrying its reason — the DELIBERATELY_UNMAPPED
discipline: the gap and the justification arrive in the same reviewable diff.
"""
from __future__ import annotations

import glob
import os

import pytest

from factlattice import conformance
from factlattice.conformance import POSITIONS, check_cell, enumerate_cells
from factlattice.parser import (
    CORE_EDGE_LAYER,
    REL_PROPERTY_ALIASES,
    REL_PROPERTY_EDGES,
    TRIPLE_ONLY_VERBS,
    effective_domain_verbs,
    parse_text,
    rel_property_edge_map,
    verb_property_key,
)
from factlattice.profile import load_profile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PARITY_PROFILE = os.path.join(HERE, "fixtures", "profile-verb-parity.lts")


def _profile_vocabularies():
    """(label, domain_verbs, profile_layers) for every profile on disk that registers verbs.

    The fixture profile guarantees the family has teeth in a bare checkout; every shipped
    `domain-profiles/*.lts` is added automatically, so a deployment that starts registering
    verbs starts being checked WITHOUT anyone editing this file. That last property is the
    whole design: the enumeration must not have an author.
    """
    out = []
    paths = [PARITY_PROFILE] + sorted(glob.glob(os.path.join(REPO, "domain-profiles", "*.lts")))
    extra = os.environ.get("FACTLATTICE_PARITY_PROFILES", "")
    paths += [p for p in extra.split(os.pathsep) if p]
    for path in paths:
        try:
            prof = load_profile(path)
        except Exception:                                    # noqa: BLE001 — a broken
            continue                                          # profile is another test's job
        verbs = getattr(prof, "domain_verbs", frozenset())
        if verbs:
            # `Profile.edge_types` is the `{edge_type -> layer}` projection (the name
            # changed from an earlier `edge_layers` before this fix landed; reading the
            # wrong attribute silently returned None forever via `getattr(..., None)`,
            # which is exactly the class of bug this file exists to catch one layer up).
            out.append((os.path.basename(path), frozenset(verbs),
                        getattr(prof, "edge_types", None) or None))
    return out


VOCABULARIES = _profile_vocabularies()
CELLS = enumerate_cells(profiles=VOCABULARIES)


# ---------------------------------------------------------------------------
# the enumeration itself must be non-degenerate
# ---------------------------------------------------------------------------
def test_the_fixture_profile_registers_verbs_that_reach_the_parser_hook():
    """If this ever returns empty, every parameterised cell below silently vanishes and
    the suite goes green by having nothing to say. Guard the guard."""
    labels = {label for label, _, _ in VOCABULARIES}
    assert "profile-verb-parity.lts" in labels
    verbs = next(v for label, v, _ in VOCABULARIES if label == "profile-verb-parity.lts")
    assert len(verbs) >= 5
    assert "APPLIES_AT_PHASE" in verbs, "the multi-word kebab shape must be exercised"


def test_the_cross_product_is_complete_by_construction():
    """cells == verbs × positions, for every vocabulary, with aliases adding spellings.

    A cell count that is merely 'large' proves nothing; it must be the product."""
    assert CELLS, "no cells generated"
    core_verbs = set(CORE_EDGE_LAYER)
    for position in POSITIONS:
        got = {c.construct for c in CELLS if c.position == position}
        assert core_verbs <= got, f"{position} misses {sorted(core_verbs - got)}"
    for label, verbs, _ in VOCABULARIES:
        for position in POSITIONS:
            got = {c.construct for c in CELLS
                   if c.position == position and c.domain_verbs == verbs}
            assert verbs <= got, f"{label}/{position} misses {sorted(verbs - got)}"


def test_no_cell_is_a_skip():
    """Every cell either expects an edge or carries a written reason it cannot."""
    for cell in CELLS:
        assert cell.expect or cell.exemption, f"{cell.id} expects nothing and explains nothing"
        if cell.exemption is not None:
            assert len(cell.exemption.strip()) > 40, (
                f"{cell.id} is exempt with a reason too short to be one")


# ---------------------------------------------------------------------------
# THE CROSS-PRODUCT — one test per cell, so a failure names the cell
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cell", CELLS, ids=lambda c: c.id)
def test_conformance_cell(cell):
    result = check_cell(cell)
    assert result.ok, f"{cell.id} (enumerated from {cell.enumerated_from}): {result.detail}"


# ---------------------------------------------------------------------------
# the specific regressions, named, so the report can point at them
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key,expected_type", [
    ("relates-to", "RELATED_TO"),   # 342 refs / 11 files, minted 0 before this fix
    ("related-to", "RELATED_TO"),   # the mechanical kebab, absent from the curated table
    ("uses", "USES"),               # 14 refs / 2 files
    ("upholds", "UPHOLDS"),         # 17 refs / 6 files
    ("documents", "DOCUMENTS"),     # 44 refs / 7 files
    ("exemplifies", "EXEMPLIFIES"),  # 6 refs / 3 files
])
def test_the_measured_house_idiom_mints(key, expected_type):
    """The four verbs the probe named, plus the two the sweep added. Kept as explicit
    cases ALONGSIDE the cross-product — not instead of it — so the report can cite a
    named failure, and so a future refactor that quietly narrows the enumeration still
    trips something."""
    verbs = frozenset({"USES", "UPHOLDS", "DOCUMENTS", "REALIZES", "EXEMPLIFIES"})
    in_force = rel_property_edge_map(verbs)
    assert key in in_force, f"`{key}` mints nothing"
    assert in_force[key][0] == expected_type


def test_every_exemption_names_a_real_verb_and_gives_a_reason():
    for verb, reason in TRIPLE_ONLY_VERBS.items():
        assert verb in CORE_EDGE_LAYER, f"{verb} is exempted but is not a verb"
        assert len(reason.strip()) > 80, f"{verb}'s exemption is an assertion, not a reason"
        assert verb_property_key(verb) not in rel_property_edge_map(), (
            f"{verb} is exempted from the property surface yet has a key")


def test_aliases_resolve_to_verbs_the_vocabulary_actually_has():
    for key, (verb, _rev) in REL_PROPERTY_ALIASES.items():
        assert verb in CORE_EDGE_LAYER, f"alias `{key}` -> unknown verb {verb}"


def test_the_curated_table_still_wins_over_the_derivation():
    """`used-by`/`issuer`/`tested-by` carry REVERSED directions the kebab derivation
    cannot know. If the derivation ever overwrote them the direction would silently flip,
    which is a worse defect than the one being fixed here."""
    derived = rel_property_edge_map()
    for key, curated in REL_PROPERTY_EDGES.items():
        assert derived[key] == curated, f"derivation clobbered the curated `{key}`"


# ---------------------------------------------------------------------------
# the census — reported, and asserted only where it is a hard invariant
# ---------------------------------------------------------------------------
def test_the_warrant_is_warranted_and_states_its_own_extent():
    """The verdict must ENUMERATE, not assert. `'we validate the core language'` is the
    old caveat wearing a confident voice; `core constructs: N of N` is not."""
    w = conformance.warrant(profiles=VOCABULARIES)
    assert w["verdict"] == "WARRANTED", w["failures"]
    assert w["exemptions_without_a_reason"] == []
    assert " of " in w["core_constructs"] and " of " in w["registered_extensions"]
    assert w["exemptions"], "an exemption list of zero would mean the discipline is untested"
    print(f"\n[warrant] {w['verdict']} — core {w['core_constructs']}, "
          f"registered extensions {w['registered_extensions']}, "
          f"exemptions {len(w['exemptions'])}, cells {w['cells']}")


def test_the_warrant_is_usage_blind():
    """⛔ THE PROPERTY THAT WOULD HAVE CAUGHT THIS BUG BEFORE AN AUTHOR DID.

    The input is THE DECLARED SET, ENTIRE. A verb used once in a million documents carries
    exactly the obligation of one used everywhere: once declared, tested — until
    undeclared. Usage is not an input, not a filter, and not a priority order, and there is
    deliberately no `declared_but_unused` list to consult, because a list is an
    intermediate snapshot that can go stale — the same failure shape as the two hand-copied
    verbs that caused the defect this file exists for. THE PROFILE IS THE ENUMERATION.

    Asserted by declaring a verb that appears in NO corpus anywhere and requiring a passing
    cell for it in every position."""
    never_authored = frozenset({"ZZ_NEVER_AUTHORED_ANYWHERE"})
    cells = enumerate_cells(profiles=[("synthetic", never_authored, None)])
    mine = [c for c in cells if c.construct == "ZZ_NEVER_AUTHORED_ANYWHERE"]
    assert {c.position for c in mine} == set(POSITIONS), (
        "a declared-but-never-authored verb must be checked in EVERY position")
    for cell in mine:
        assert check_cell(cell).ok, check_cell(cell).detail


def test_no_usage_measurement_reaches_the_warrants_input_path():
    """⛔ STRUCTURAL, NOT ASPIRATIONAL — asserted on the IMPORT GRAPH.

    The governance census lives in `factlattice.enumeration_gap`, which imports
    `conformance` and never the reverse. That direction is the guarantee: the warrant
    cannot consult a usage measurement, because it cannot reach one. A comment saying
    'usage-blind' decays; an import that would have to be added does not."""
    import inspect
    from factlattice import enumeration_gap
    src = inspect.getsource(conformance)
    assert "enumeration_gap" not in src, (
        "factlattice/conformance.py reached for the governance census — the warrant's "
        "input path must contain no usage measurement of any kind")
    for banned in ("profile_audit", "analytics", "control_corpus_text", "Counter"):
        assert banned not in src, f"`{banned}` appears in the warrant's module"
    assert "conformance" in inspect.getsource(enumeration_gap), (
        "the dependency must run gap -> conformance, never conformance -> gap")


def test_frequency_can_shrink_the_vocabulary_but_never_the_warrant():
    """A verb the control corpus never exercises still gets its full cell set. The census
    is allowed to NOTICE the shortfall; it is not allowed to act on it."""
    from factlattice import enumeration_gap
    declared = frozenset({"USES", "UPHOLDS"})
    census = enumeration_gap.verb_census(declared)
    assert set(census.missing) & declared, "expected these to be unexercised by control.lts"
    covered = {c.construct for c in enumerate_cells(profiles=[("g", declared, None)])}
    assert declared <= covered, "an unexercised declared verb was dropped from the warrant"


def test_the_control_corpus_census_is_reportable():
    """Not a pass/fail on coverage — a standing measurement, so the gap has a number.

    The control corpus is a RECALL control; it was never meant to be a conformance
    enumeration, and holding it to one would be unfair. What is not fair is letting its
    honest 'cannot prove there is no nineteenth' disclaimer cover for the completable
    half. This test makes the shortfall a printed number instead of an inference."""
    from factlattice import enumeration_gap
    verbs = frozenset({"USES", "UPHOLDS", "DOCUMENTS", "REALIZES", "EXEMPLIFIES"})
    census = enumeration_gap.verb_census(verbs)
    assert census.defined > 0
    assert census.cells_generated >= census.defined
    print(f"\n[census] relationship-verb: {census.defined} verbs defined, "
          f"{census.exercised_by_control} exercised by control.lts, gap {census.gap}; "
          f"{census.cells_generated} conformance cells generated "
          f"({census.cells_defined} verb×position minimum)")


# ---------------------------------------------------------------------------
# 2026-09-23 UNIFY — the maintainer ruling: "a relationship is a relationship regardless of
# authoring surface; double-registration IS the silent-loss trap." The tests above
# (and `rel_property_edge_map` itself) close ONE direction: a verb registered on the
# TRIPLE surface (`relationship_verbs` / `Profile.domain_verbs`) gets a property key for
# free. These tests close the OTHER direction: a verb registered ONLY on the PROPERTY
# surface (`edge_types[].property_key` / `Profile.property_edges`) must be recognised as
# a bare `## Relationships` triple too — before this pass a profile author who declared
# only one surface silently lost the other, which is the double-registration trap named
# in the ruling.
# ---------------------------------------------------------------------------
def _doc(body: str) -> str:
    return ('---\ntitle: "d"\nid: "SPEC:D"\n---\n# D [SPEC:D]\n\n'
            '## [FLV-A] Source\n\n- status: ok\n\n'
            '## [FLV-B] Target\n\n- status: ok\n\n' + body)


def test_a_property_edges_only_registration_becomes_a_recognised_bare_triple():
    """A verb registered ONLY through `property_edges` (no `relationship_verbs` entry
    anywhere) must parse as an unflagged, recognised bare triple. Before the 2026-09-23
    unify pass this verb reached `_find_edge_type` with `domain_verbs=frozenset()` and
    came back `recognised=False` — i.e. preserved-and-flagged `unregistered_verb` — even
    though the SAME profile's property surface minted it cleanly."""
    property_edges = {"handles": ("HANDLES", "semantic", False)}
    text = _doc("## Relationships\n\n[FLV-A] HANDLES [FLV-B]\n")
    pf = parse_text(text, "t.lts", domain_verbs=frozenset(), property_edges=property_edges)
    edges = [e for e in pf.edges if e.type == "HANDLES"]
    assert edges, "property-edges-only verb did not mint as a bare triple"
    assert edges[0].source == "FLV-A" and edges[0].target == "FLV-B"
    assert edges[0].inference == "authored"
    assert not edges[0].flags, f"triple came back flagged: {edges[0].flags}"


def test_the_same_property_edges_only_registration_still_mints_its_own_property_key():
    """The companion half of the previous test, so the fixture proves BOTH surfaces of
    the SAME registration in one place — a regression that broke only one direction
    would otherwise still show green on whichever test ran second."""
    property_edges = {"handles": ("HANDLES", "semantic", False)}
    text = _doc("- handles: [FLV-B]\n")
    pf = parse_text(text, "t.lts", domain_verbs=frozenset(), property_edges=property_edges)
    edges = [e for e in pf.edges if e.type == "HANDLES"]
    assert edges and edges[0].inference == "property"


def test_a_namespaced_property_edges_type_is_not_folded_into_bare_triple_recognition():
    """`effective_domain_verbs` folds BARE edge types only. A namespaced type
    (`dom.HANDLES`) is already recognised on the triple surface by its own dotted-segment
    rule (`_find_edge_type`'s namespaced branch never consults `domain_verbs`), so folding
    it in would be inert — and a BARE `HANDLES` triple must stay UNRECOGNISED (preserved
    and flagged, per SPEC §2.4, never silently dropped): a namespaced registration does
    not accidentally grant the bare form recognition too."""
    property_edges = {"handles": ("dom.HANDLES", "semantic", False)}
    unified = effective_domain_verbs(frozenset(), property_edges)
    assert "dom.HANDLES" not in unified and "HANDLES" not in unified
    text = _doc("## Relationships\n\n[FLV-A] HANDLES [FLV-B]\n")
    pf = parse_text(text, "t.lts", domain_verbs=frozenset(), property_edges=property_edges)
    edges = [e for e in pf.edges if e.type == "HANDLES"]
    assert edges, "the bare verb must be preserved (SPEC §2.4), never dropped"
    assert edges[0].flags == ["unregistered_verb"], (
        "a namespaced-only registration must NOT recognise the bare form")
    assert any(f.kind == "unregistered_verb" for f in pf.vocabulary_findings), (
        pf.vocabulary_findings)


def test_a_relationship_verbs_only_registration_shows_up_in_the_write_receipt():
    """[FL-VERB-PARITY] the mirror of `test_the_minting_table_follows_a_profiles_registered_verbs`
    (test_repair_and_binding.py), asserted directly against `rel_property_edge_map` here
    so this file's own claim ("the receipt reads the SAME derivation E4 runs") is
    falsifiable in one place. A verb registered ONLY under `relationship_verbs` (no
    `edge_types[].property_key` at all) must still resolve a kebab property key."""
    in_force = rel_property_edge_map(frozenset({"UPHOLDS"}))
    assert in_force["upholds"] == ("UPHOLDS", "semantic", False)


def test_profile_registered_only_via_property_edges_round_trips_through_load_profile():
    """End-to-end through the real `load_profile(dict)` path (not a hand-built tuple),
    proving the unify also holds for a profile author who only ever writes
    `edge_types: [{type, property_key}]` and never touches `relationship_verbs` at all —
    the exact shape of the double-registration trap the ruling names."""
    prof = load_profile({
        "domain": {"id": "cf.unify-check"},
        "edge_types": [{"type": "HANDLES", "property_key": "handles", "layer": "semantic"}],
    })
    assert prof.domain_verbs == frozenset()          # nothing under relationship_verbs
    assert prof.property_edges == {"handles": ("HANDLES", "semantic", False)}
    unified = effective_domain_verbs(prof.domain_verbs, prof.property_edges)
    assert "HANDLES" in unified
    text = _doc("## Relationships\n\n[FLV-A] HANDLES [FLV-B]\n")
    pf = parse_text(text, "t.lts", domain_verbs=prof.domain_verbs,
                    profile_layers=prof.edge_types, property_edges=prof.property_edges)
    assert any(e.type == "HANDLES" and not e.flags for e in pf.edges)


def test_binding_minting_keys_for_reflects_a_relationship_verbs_only_profile():
    """[FL-VERB-PARITY] `binding.minting_keys_for` — the write-receipt's own idea of
    "what mints" — must agree with the parser for a verb registered ONLY on the triple
    surface, or the receipt calls a key "undeclared" that the graph actually minted."""
    from factlattice_engine import binding
    prof = load_profile({"domain": {"id": "cf.unify-check-2"},
                         "relationship_verbs": {"domain": ["HANDLES"]}})
    assert prof.property_edges == {}                 # nothing under edge_types
    keys = binding.minting_keys_for(prof)
    assert keys["handles"] == ("HANDLES", "semantic", False)
    assert binding.is_minting_key("handles", prof)


def test_binding_minting_keys_for_matches_the_parser_even_with_no_profile():
    """[2026-09-23 cold-audit correction] This test used to
    assert the DEFECT: that `rel_property_edge_map()` derives a kebab property key for
    EVERY core verb unconditionally (bar the two structural exemptions), including
    common-English-word verbs like `is-a` and `holds`. A cold audit reproduced spurious
    edges from exactly that grant 3/3 in real corpora (`- sequence: [X] describes a
    generic display device...` minted a false SEQUENCE edge) — granting a property
    surface is a claim of pure-reference usage, and it was never vetted for these verbs.

    The no-profile baseline is now: a bare core verb gets a property key ONLY if it was
    explicitly vetted into `REL_PROPERTY_EDGES` / `REL_PROPERTY_ALIASES` (the same bar
    USES/UPHOLDS/DOCUMENTS cleared). `MINTING_KEYS` (the static pre-parity table) stays
    as the ten-entry historical baseline on purpose (`tests/test_repair_and_binding.py:
    281`); `minting_keys_for(None)` must agree with it for every BARE core verb, since no
    profile is in force to register anything new."""
    from factlattice_engine import binding
    keys = binding.minting_keys_for(None)
    # Vetted core verbs (already in REL_PROPERTY_EDGES under their own mechanical
    # spelling) still mint — this is the recovery that must survive the fix.
    assert "depends-on" in keys and keys["depends-on"][0] == "DEPENDS_ON"
    assert "part-of" in keys and keys["part-of"][0] == "PART_OF"
    assert "used-by" in keys and keys["used-by"] == ("USED_BY", "entity", True)
    assert "tested-by" in keys and keys["tested-by"] == ("TESTED_BY", "entity", True)
    # Unvetted common-English-word core verbs must NOT mint — this is the fix.
    for verb in TRIPLE_ONLY_VERBS:
        assert verb_property_key(verb) not in keys, (
            f"{verb} is triple-only and must not appear in the receipt's minting table")
    assert "is-a" not in keys, "IS_A is an unvetted common-word verb; must not mint"
    assert "holds" not in keys, "HOLDS is an unvetted common-word verb; must not mint"
    assert "sequence" not in keys, (
        "SEQUENCE is the reproduced over-recognition case; must not mint")
