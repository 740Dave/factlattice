"""THE EDGE-TYPE NAMESPACE — collision discipline, one level up from property keys.

⛔ WHAT THIS CLOSES. 3f9a2c1 made property KEYS profile-registrable and guarded them with
load-time refusals, and its author flagged its own hole rather than hiding it: "I applied
collision discipline to property KEYS only, not to the edge-TYPE namespace." Measured
against that commit, before any of this existed:

  * `{type: PART_OF, property_key: contained-by, layer: causal}` LOADED CLEAN, and the
    resulting graph carried PART_OF edges in TWO layers at once (printed below in
    `test_core_shadow_produced_a_two_layer_graph_BEFORE_the_guard`, which reproduces the
    old damage through the parser rather than asserting it from memory);
  * two profiles could declare `GOVERNS` at different layers through DIFFERENT property
    keys, so the key-level guard never fired and nothing refused;
  * `domain_profiles.load_profile_edge_config` resolved that same conflict FIRST-WINS,
    by filename sort order, with a note in `.ambiguities` that nothing reads.

⛔ NEGATIVE-CONTROL DISCIPLINE. Every refusal here was RUN AND OBSERVED NOT FIRING
against the code as it stood at 3f9a2c1 before the guard was written. A guard observed
only refusing is not a guard — it cannot be told apart from a function that refuses
everything, which is why every refusal test in this module is paired with an ACCEPTANCE
test on the nearest legitimate neighbour (the epistemics rule rule-negative-control).
"""
from __future__ import annotations

import os

import pytest

from factlattice.parser import CANONICAL_LAYERS, CORE_EDGE_LAYER, parse_text
from factlattice.profile import (
    ProfileCollisionError,
    _EDGE_TYPE_RE,
    load_profile,
    merge_edge_types,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED_PROFILE_DIR = os.path.join(REPO_ROOT, "domain-profiles")


def _profile(domain_id, *edge_types, **extra):
    body = {"domain": {"id": domain_id, "namespaces": [domain_id]},
            "edge_types": list(edge_types)}
    body.update(extra)
    return body


# ==========================================================================
# R3 — THE NAME SHAPE, DERIVED FROM THE PARSER RATHER THAN COMPOSED FROM MEMORY
# ==========================================================================
# Each candidate is fed through the REAL parse path — a `## Relationships` triple with
# the name registered exactly the way a profile registers one — and we compare what the
# parser PRODUCED against what `_EDGE_TYPE_RE` PERMITS. That is what makes the regex a
# derivation instead of a guess: if someone widens `_TOKEN_RE` or `_UNREG_VERB_RE`, this
# test goes red rather than the regex quietly becoming wrong.
#
# `expect_produced` is the edge type the parser ACTUALLY emits. Note the four entries
# where it is neither the candidate nor None: those are SUBSTITUTIONS, the [SL-18] class,
# and they are the strongest argument for refusing the name outright.
NAME_CASES = [
    # (candidate,                  edge type the parser produces)
    ("EXEMPLIFIES",                "EXEMPLIFIES"),
    ("GOVERNS",                    "GOVERNS"),
    ("A",                          "A"),
    ("A1",                         "A1"),
    ("HAS_2_PARTS",                "HAS_2_PARTS"),
    ("clinical.CONTRAINDICATES",   "clinical.CONTRAINDICATES"),
    ("acme.ds.GOVERNS",            "acme.ds.GOVERNS"),
    # --- INERT: the parser finds no verb at all -------------------------------
    ("CONTRA-INDICATES",           None),
    ("_LEADING",                   None),
    ("1STEP",                      None),
    ("clinical.contraindicates",   None),
    ("clinical.Governs",           None),
    ("clinical.",                  None),
    # --- ⛔ SUBSTITUTED: the parser emits a DIFFERENT type, unflagged ----------
    (".GOVERNS",                   "GOVERNS"),
    ("clinical..GOVERNS",          "GOVERNS"),
    ("clin-ical.GOVERNS",          "ical.GOVERNS"),
    ("clinical.GOV-ERNS",          "clinical.GOV"),
]


def _parse_triple(candidate):
    """Parse `- [N-A] <candidate> [N-B]` with `candidate` registered as a domain verb.

    ⛔ The verb set is built the way THE PROFILE PIPELINE builds it — through
    `_domain_verbs_from_raw`, which UPPER-CASES — and not by hand. A hand-built set
    containing a lowercase token would make a lowercase bare name appear reachable when
    no profile can actually produce one, and the test would certify a shape the platform
    cannot deliver.
    """
    from factlattice.profile import _domain_verbs_from_raw

    verbs = _domain_verbs_from_raw(
        {"relationship_verbs": {"domain": [candidate.split(".")[-1]]}})
    text = (f"# Doc [SPEC:DOC]\n\n### [N-A] A\n- definition: a\n\n"
            f"### [N-B] B\n- definition: b\n\n## Relationships\n\n"
            f"- [N-A] {candidate} [N-B]\n")
    parsed = parse_text(text, "t.lts", domain_verbs=verbs,
                        registered_ns=frozenset({"core", "clinical", "acme", "clin"}))
    rel = [e for e in parsed.edges if e.inference != "structural"]
    return rel[0].type if rel else None


@pytest.mark.parametrize("candidate,expect_produced", NAME_CASES)
def test_edge_type_regex_admits_exactly_what_the_parser_recognises(candidate,
                                                                   expect_produced):
    produced = _parse_triple(candidate)
    assert produced == expect_produced, (
        f"the parser's behaviour on {candidate!r} moved; this table is the DERIVATION "
        f"the refusal regex is built on, so re-derive the regex before editing this line")
    permitted = bool(_EDGE_TYPE_RE.match(candidate))
    assert permitted == (produced == candidate), (
        f"_EDGE_TYPE_RE {'admits' if permitted else 'refuses'} {candidate!r}, but the "
        f"parser produces {produced!r}. The regex must admit a name IFF the parser "
        f"round-trips it — admitting more registers something inert or SUBSTITUTED, "
        f"admitting less refuses something that works.")


@pytest.mark.parametrize("bad", [c for c, p in NAME_CASES if p != c])
def test_non_conforming_edge_type_name_is_a_LOUD_REFUSAL(bad):
    with pytest.raises(ProfileCollisionError) as ei:
        load_profile(_profile("d", {"type": bad, "layer": "semantic"}))
    assert bad in str(ei.value), "the refusal must name the offending declaration"


@pytest.mark.parametrize("good", [c for c, p in NAME_CASES if p == c])
def test_conforming_edge_type_name_loads(good):
    """The acceptance half. Without it, a regex of `^$` would pass every refusal test."""
    prof = load_profile(_profile("d", {"type": good, "layer": "semantic"}))
    assert prof.edge_types[good] == "semantic"


def test_a_substituted_name_really_is_substituted_not_merely_rejected():
    """⛔ The measurement the R3 refusal rests on, kept executable.

    `clin-ical.GOVERNS` is not ignored — the parser emits an edge typed
    `ical.GOVERNS`, with no flag and no finding. A wrong edge is worse than a missing
    one, and line accounting is blind to it because the line WAS consumed.
    """
    assert _parse_triple("clin-ical.GOVERNS") == "ical.GOVERNS"
    assert _parse_triple(".GOVERNS") == "GOVERNS"


# ==========================================================================
# R1 — A PROFILE MUST NOT REDEFINE A CORE EDGE TYPE, EVEN IDENTICALLY
# ==========================================================================
@pytest.mark.parametrize("core_type", ["PART_OF", "SUPERSEDES", "DEPENDS_ON",
                                       "RELATED_TO", "CAUSES", "BEFORE", "TESTED_BY",
                                       "ISSUES", "USED_BY", "RESOLVES",
                                       "CONTRAST_WITH", "IMPLEMENTS"])
def test_profile_redefining_a_core_edge_type_is_refused(core_type):
    with pytest.raises(ProfileCollisionError) as ei:
        load_profile(_profile("rogue", {"type": core_type, "layer": "causal",
                                        "property_key": "contained-by"}))
    msg = str(ei.value)
    assert core_type in msg, "the refusal must name the colliding type"
    assert CORE_EDGE_LAYER[core_type] in msg, "the refusal must name the CORE definition"


def test_profile_may_not_redefine_a_core_edge_type_even_IDENTICALLY():
    """⛔ Same answer as the property-key path (`test_profile_may_not_even_redefine_a
    _builtin_identically`). Two collision rules that disagree with each other would be a
    fork of their own, and every future author would have to re-derive which one governs.
    """
    with pytest.raises(ProfileCollisionError):
        load_profile(_profile("d", {"type": "PART_OF", "layer": "semantic"}))


def test_core_shadow_produced_a_two_layer_graph_BEFORE_the_guard():
    """⛔ THE DAMAGE, REPRODUCED THROUGH THE PARSER — not asserted from memory.

    This is what `{type: PART_OF, layer: causal, property_key: contained-by}` did on
    3f9a2c1: bypass the loader (which now refuses it) and hand the parser the map the
    loader used to return. Two PART_OF edges, two different layers, one graph.
    `lts_traverse(layers=['semantic'])` returns one of them and reports success.
    """
    text = ("# Doc [SPEC:DOC]\n\n### [N-A] A\n- part-of: [N-B]\n"
            "- contained-by: [N-B]\n\n### [N-B] B\n- definition: b\n")
    poisoned = {"contained-by": ("PART_OF", "causal", True)}
    layers = {e.layer for e in parse_text(text, "t.lts", property_edges=poisoned).edges
              if e.type == "PART_OF"}
    assert layers == {"semantic", "causal"}, (
        "the reproduction stopped reproducing — re-derive why the guard is needed "
        "before weakening it")
    # ...and the loader now refuses to be the thing that hands that map over.
    with pytest.raises(ProfileCollisionError):
        load_profile(_profile("d", {"type": "PART_OF", "layer": "causal",
                                    "property_key": "contained-by"}))


# --- the ACCEPTANCE neighbours: what R1 must NOT refuse -------------------
@pytest.mark.parametrize("namespaced,layer", [
    ("dec.SUPERSEDES", "temporal"),   # core SUPERSEDES is semantic — deliberately differs
    ("art.SUPERSEDES", "temporal"),
    ("swe.DEPENDS_ON", "causal"),     # core DEPENDS_ON is semantic
    ("dec.RESOLVES", "causal"),       # core RESOLVES is semantic
    ("dec.PART_OF", "semantic"),
])
def test_a_NAMESPACED_type_may_disagree_with_core_and_that_is_the_point(namespaced, layer):
    """⛔ R1 KEYS ON THE BARE NAME, AND THAT IS NOT A LOOPHOLE.

    All five of these are SHIPPED declarations in `domain-profiles/`. A rule keyed on
    the bare SUFFIX would refuse every one of them while protecting nothing:
    `dec.SUPERSEDES` and `SUPERSEDES` are distinct types in the graph and no traversal
    can confuse them. It is the BARE name that is indistinguishable from core.
    """
    bare = namespaced.split(".")[-1]
    assert bare in CORE_EDGE_LAYER and CORE_EDGE_LAYER[bare] != layer or layer == "semantic"
    prof = load_profile(_profile("d", {"type": namespaced, "layer": layer}))
    assert prof.edge_types[namespaced] == layer


def test_restating_a_core_verb_under_relationship_verbs_is_allowed():
    """A `relationship_verbs` entry attaches NO semantics — `_find_edge_type` takes the
    UNION of core and domain verbs, so restating `PART_OF` there is a genuine no-op. The
    boundary is stated here because it is the one a future author will re-derive."""
    prof = load_profile({"domain": {"id": "d"},
                         "relationship_verbs": {"core": ["PART_OF", "SUPERSEDES"]}})
    assert prof.edge_types == {}
    assert "PART_OF" in prof.domain_verbs


def test_the_SHIPPED_profiles_still_load():
    """The blast-radius control. If R1/R3 refused anything that ships, this is red.
    (The published tree ships five profiles; a maintainer checkout carries one more.)"""
    import glob
    paths = sorted(glob.glob(os.path.join(SHIPPED_PROFILE_DIR, "*.lts")))
    assert len(paths) >= 5, "the shipped profile set moved; re-measure before trusting"
    for p in paths:
        load_profile(p)  # must not raise


def test_the_LIVE_profile_still_loads_if_present():
    live = os.environ.get("FACTLATTICE_LIVE_PROFILE", "")  # machine-local; unset => skip
    if not live or not os.path.isfile(live):
        pytest.skip("live acme profile not present")
    prof = load_profile(live)
    want = os.environ.get("FACTLATTICE_LIVE_PROFILE_DOMAIN")
    assert prof.domain_id == want if want else prof.domain_id
    assert len(prof.domain_verbs) >= 17


# ==========================================================================
# R1b — a non-canonical LAYER is inert while reading as functional
# ==========================================================================
def test_non_canonical_layer_is_refused():
    with pytest.raises(ProfileCollisionError) as ei:
        load_profile(_profile("d", {"type": "GOVERNS", "layer": "sematic"}))  # typo
    assert "sematic" in str(ei.value)


@pytest.mark.parametrize("layer", CANONICAL_LAYERS)
def test_every_canonical_layer_is_accepted(layer):
    assert load_profile(_profile("d", {"type": "GOVERNS",
                                       "layer": layer})).edge_types["GOVERNS"] == layer


def test_an_omitted_layer_defaults_to_semantic_and_is_not_refused():
    assert load_profile(_profile("d", {"type": "GOVERNS"})).edge_types == {
        "GOVERNS": "semantic"}


# ==========================================================================
# R2 — TWO PROFILES, ONE EDGE TYPE
# ==========================================================================
def test_cross_profile_edge_type_disagreement_is_a_loud_refusal():
    """⛔ `corpora/acme-specs/` is a root of THREE registered corpora, so this is a
    live scenario. Note the two profiles use DIFFERENT property keys — which is exactly
    why the key-level guard from 3f9a2c1 stayed silent on it."""
    a = load_profile(_profile("team-a", {"type": "GOVERNS", "property_key": "governs",
                                         "layer": "semantic"}))
    b = load_profile(_profile("team-b", {"type": "GOVERNS",
                                         "property_key": "governed-under",
                                         "layer": "entity"}))
    with pytest.raises(ProfileCollisionError) as ei:
        merge_edge_types([a, b])
    msg = str(ei.value)
    assert "team-a" in msg and "team-b" in msg, "must name BOTH profiles"
    assert "semantic" in msg and "entity" in msg, "must name BOTH definitions"
    assert "GOVERNS" in msg


def test_cross_profile_edge_type_AGREEMENT_merges():
    """The same answer the property-key path gives (`test_cross_profile_agreement_is
    _allowed`). Two profiles over a shared root declaring the same type identically is
    the ORDINARY case; refusing it would make a shared corpus root unusable."""
    spec = {"type": "EXEMPLIFIES", "layer": "semantic"}
    a = load_profile(_profile("team-a", spec))
    b = load_profile(_profile("team-b", spec))
    assert merge_edge_types([a, b]) == {"EXEMPLIFIES": "semantic"}


def test_merge_is_order_independent_in_its_verdict():
    """A refusal that depended on argument order would be first-wins wearing a hat."""
    a = load_profile(_profile("a", {"type": "GOVERNS", "layer": "semantic"}))
    b = load_profile(_profile("b", {"type": "GOVERNS", "layer": "causal"}))
    for pair in ([a, b], [b, a]):
        with pytest.raises(ProfileCollisionError):
            merge_edge_types(pair)


def test_active_and_passive_voice_of_one_type_is_NOT_a_disagreement():
    """⛔ `reversed` is a property of the KEY, not of the TYPE, and core proves it:
    `supersedes` and `superseded-by` both mint SUPERSEDES with opposite `reversed`.
    Refusing two profiles for declaring the two voices would refuse what core does."""
    a = load_profile(_profile("a", {"type": "GOVERNS", "property_key": "governs",
                                    "layer": "semantic"}))
    b = load_profile(_profile("b", {"type": "GOVERNS", "property_key": "governed-by",
                                    "layer": "semantic", "reversed": True}))
    assert merge_edge_types([a, b]) == {"GOVERNS": "semantic"}


def test_a_profile_contradicting_ITSELF_is_refused():
    with pytest.raises(ProfileCollisionError) as ei:
        load_profile(_profile("d",
                              {"type": "GOVERNS", "layer": "semantic"},
                              {"type": "GOVERNS", "layer": "causal"}))
    assert "semantic" in str(ei.value) and "causal" in str(ei.value)


# ==========================================================================
# R2 ON THE LIVE ENGINE PATH — domain_profiles.load_profile_edge_config
# ==========================================================================
# `merge_edge_types` mirrors `merge_property_edges`, and like it has no production
# caller yet. THIS one is on the live build path: `config.build_partition` feeds
# `edge_layers` straight into `parser._layer_for_type` for every corpus loaded.
_ROW = "| {t} | {l} | a declared type |\n"


def _write_profile(tmp_path, name, rows):
    p = tmp_path / name
    body = ["# Profile [SPEC:P]\n", "\n", "## Edge Types\n", "\n",
            "| Type | Layer | Description |\n", "|---|---|---|\n"]
    body += [_ROW.format(t=t, l=l) for t, l in rows]
    p.write_text("".join(body), encoding="utf-8")
    return str(p)


def test_two_profiles_disagreeing_on_a_layer_now_REFUSE_instead_of_first_winning(tmp_path):
    from factlattice.domain_profiles import load_profile_edge_config

    _write_profile(tmp_path, "a-first.lts", [("dom.GOVERNS", "semantic")])
    _write_profile(tmp_path, "b-second.lts", [("dom.GOVERNS", "causal")])
    with pytest.raises(ProfileCollisionError) as ei:
        load_profile_edge_config(str(tmp_path))
    msg = str(ei.value)
    assert "dom.GOVERNS" in msg
    assert "a-first.lts" in msg and "b-second.lts" in msg, "must name BOTH profiles"
    assert "semantic" in msg and "causal" in msg, "must name BOTH layers"


def test_the_old_FIRST_WINS_behaviour_is_gone(tmp_path):
    """⛔ THE NEGATIVE CONTROL ON THE FIX ITSELF.

    The defect was not "no error" — it was a CONFIDENT WRONG ANSWER: the config came
    back populated, `edge_layers['dom.GOVERNS'] == 'semantic'` purely because `a-` sorts
    before `b-`, and the disagreement sat in `.ambiguities` where nothing reads it. This
    asserts the old ANSWER can no longer be produced, which a `pytest.raises` on its own
    does not (a function that raised for an unrelated reason would still pass that).
    """
    from factlattice.domain_profiles import load_profile_edge_config

    _write_profile(tmp_path, "a-first.lts", [("dom.GOVERNS", "semantic")])
    _write_profile(tmp_path, "b-second.lts", [("dom.GOVERNS", "causal")])
    try:
        cfg = load_profile_edge_config(str(tmp_path))
    except ProfileCollisionError:
        return
    pytest.fail(
        f"first-wins is back: edge_layers={cfg.edge_layers}, "
        f"ambiguities={cfg.ambiguities}")


def test_identical_declarations_across_profiles_still_merge(tmp_path):
    from factlattice.domain_profiles import load_profile_edge_config

    _write_profile(tmp_path, "a.lts", [("dom.GOVERNS", "semantic")])
    _write_profile(tmp_path, "b.lts", [("dom.GOVERNS", "semantic"),
                                       ("dom.OWNS", "entity")])
    cfg = load_profile_edge_config(str(tmp_path))
    assert cfg.edge_layers == {"dom.GOVERNS": "semantic", "dom.OWNS": "entity"}
    assert cfg.ambiguities == []


def test_the_SHIPPED_profile_dir_loads_clean():
    """Measured 2026-08-15 BEFORE the guard: 72 types, six namespaces, ZERO conflicts.
    The guard was installed while the corpus was clean, which is the only time a guard
    can be installed without also being a migration."""
    from factlattice.domain_profiles import load_profile_edge_config

    cfg = load_profile_edge_config(SHIPPED_PROFILE_DIR)
    assert len(cfg.edge_layers) >= 45      # the published set measures 45; a maintainer checkout carries more
    assert cfg.ambiguities == []
    assert all("." in t for t in cfg.edge_layers), (
        "a BARE type reached the layer table — R1's premise (only namespaced types "
        "arrive here) no longer holds")


# ==========================================================================
# ITEM 4 — lts_profile_audit must SEE the registered vocabulary
# ==========================================================================
AUDIT_PROFILE = {
    "domain": {"id": "lib.ops", "namespaces": ["lib.ops"]},
    "relationship_verbs": {"domain": ["EXEMPLIFIES"]},
    "edge_types": [
        {"type": "EXEMPLIFIES", "property_key": "exemplifies", "layer": "semantic",
         "tier": "domain"},
        {"type": "plt.GOVERNS", "layer": "causal"},
    ],
}


def test_audit_adapter_exposes_the_loaded_profile():
    """⛔ THE REGRESSION THIS BLOCK IS BUILT ON.

    `_property_edges_section` reads `getattr(adapter, "profile", None)`. The adapter
    stored the profile only as `_profile`, so that getattr returned None on EVERY call
    and the section reported `profile_registered: {}` for a profile that registers a
    key. It failed in the safe-looking direction — a well-formed zero, identical to the
    answer for a profile that registers nothing. Measured against 3f9a2c1.
    """
    from factlattice_engine.analytics.profile_audit import (
        YamlProfileAdapter, _property_edges_section)

    ad = YamlProfileAdapter(AUDIT_PROFILE)
    assert getattr(ad, "profile", None) is not None
    sec = _property_edges_section(ad, [])
    assert sec["profile_registered_count"] == 1
    assert sec["profile_registered"]["exemplifies"] == ["EXEMPLIFIES", "semantic", False]


def test_audit_declared_edge_types_include_edge_types_declarations():
    """A namespaced type appears ONLY in `edge_types[]` — never in `relationship_verbs`.
    Omitting that union made the audit accuse the corpus of drift against a vocabulary
    the profile had declared, AND hid a registered-but-unused type. Both diffs wrong,
    in opposite directions, from one missing union."""
    from factlattice_engine.analytics.profile_audit import YamlProfileAdapter

    declared = YamlProfileAdapter(AUDIT_PROFILE).edge_types()
    assert "plt.GOVERNS" in declared, "an edge_types[]-only type was not declared"
    assert "EXEMPLIFIES" in declared


def test_audit_registration_block_attributes_and_flags():
    from factlattice_engine.analytics.profile_audit import (
        YamlProfileAdapter, _edge_type_registration)

    reg = _edge_type_registration(YamlProfileAdapter(AUDIT_PROFILE),
                                  used_edges={"EXEMPLIFIES", "PART_OF"})
    assert reg["profile_registered"] == {"EXEMPLIFIES": "semantic",
                                         "plt.GOVERNS": "causal"}
    # the registered-but-unused finding this block exists for:
    assert reg["registered_but_unused"] == ["plt.GOVERNS"]
    # and the guard's own negative control, read from the other side:
    assert reg["core_shadowed"] == []


def test_audit_registration_block_is_inert_without_a_profile():
    """The negative half — proves the numbers above came from the REGISTRATION."""
    from factlattice_engine.analytics.profile_audit import (
        NullProfileAdapter, _edge_type_registration)

    reg = _edge_type_registration(NullProfileAdapter(), used_edges={"PART_OF"})
    assert reg["profile_registered"] == {}
    assert reg["profile_registered_count"] == 0
    assert reg["core"], "the CORE side is reported even with no profile loaded"
