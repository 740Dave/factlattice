"""The two `## Relationships` triple drops, and the profile hook that closes them.

Mechanisms, as measured by the line-accounting lint before this fix landed:

  1. `_find_edge_type` accepted a BARE verb only if it was in `CORE_EDGE_LAYER`.
     Every verb a domain profile registered under `relationship_verbs.domain`
     therefore fell through to `return None` and its whole line was discarded with
     no diagnostic of any class. `relationship_verbs` was read by EXACTLY ONE file
     in this repository -- the reporting-only `analytics/profile_audit.py` -- and
     never by the parser, so verb registration was decorative from the parser's
     first commit. Never worked, never regressed.
  2. `_REL_SRC_RE` was anchored `^-[ \\t]+\\[`, requiring a leading list bullet.
     The surveyed corpora author the majority of their triples WITHOUT one; those
     lines never reached the verb matcher at all -- including triples using CORE
     verbs the parser knows perfectly, so this half was never a verb problem.

Both are silent by construction, which is why the invariant had to come first.
"""
from __future__ import annotations

import factlattice
from factlattice.parser import DEFAULT_DOMAIN_VERBS
from factlattice.profile import _domain_verbs_from_raw, load_profile

CIO_VERBS = frozenset({"DEPENDS_ON", "RELATED_TO", "USES", "EXEMPLIFIES",
                       "REALIZES", "UPHOLDS", "DOCUMENTS"})

PROFILE = {
    "domain": {"id": "lib.eng", "namespaces": ["lib.eng", "lib.ops"]},
    "relationship_verbs": {
        "core": ["DEPENDS_ON", "RELATED_TO"],
        "domain": ["USES", "EXEMPLIFIES", "REALIZES", "UPHOLDS", "DOCUMENTS"],
    },
}

FIXTURE = """\
# Fixture [SPEC:DV]

## [DV-A] A

- status: active

## [DV-B] B

- status: active

## Relationships

- [DV-A] USES [DV-B]
[DV-A] DEPENDS_ON [DV-B]
[DV-B] REALIZES [DV-A]
- [DV-A] MUST be reused wherever the shape recurs
"""


def _authored(pf):
    return [(e.type, e.source, e.target, tuple(e.flags))
            for e in pf.edges if e.inference == "authored"]


# ---------------------------------------------------------------------------
# the profile hook
# ---------------------------------------------------------------------------
def test_relationship_verbs_project_onto_the_parser_hook():
    assert _domain_verbs_from_raw(PROFILE) == CIO_VERBS
    assert load_profile(PROFILE).domain_verbs == CIO_VERBS


def test_a_profile_without_relationship_verbs_yields_the_empty_hook():
    assert _domain_verbs_from_raw({"domain": {"id": "x"}}) == DEFAULT_DOMAIN_VERBS
    assert load_profile({"domain": {"id": "x"}}).domain_verbs == frozenset()


# ---------------------------------------------------------------------------
# mechanism 1 — a profile-registered bare verb
# ---------------------------------------------------------------------------
def test_registered_domain_verb_produces_a_clean_edge():
    pf = factlattice.parse_text(FIXTURE, "f.lts", domain_verbs=CIO_VERBS)
    assert ("USES", "DV-A", "DV-B", ()) in _authored(pf)
    assert ("REALIZES", "DV-B", "DV-A", ()) in _authored(pf)


def test_unregistered_verb_is_preserved_and_flagged_never_dropped():
    """[LTS-PARSER] / SPEC 2.4 preserve-and-flag. Without the profile, USES is not
    recognised -- but it sits directly before a BRACKETED target, so it is kept and
    flagged rather than vanishing."""
    pf = factlattice.parse_text(FIXTURE, "f.lts")
    edges = _authored(pf)
    assert ("USES", "DV-A", "DV-B", ("unregistered_verb",)) in edges
    assert ("REALIZES", "DV-B", "DV-A", ("unregistered_verb",)) in edges


def test_prose_does_not_mint_an_edge():
    """`- [DV-A] MUST be reused ...` has no `[` after MUST. Requiring the bracket is
    the entire safety argument for the preserve-and-flag path."""
    for dv in (CIO_VERBS, DEFAULT_DOMAIN_VERBS):
        pf = factlattice.parse_text(FIXTURE, "f.lts", domain_verbs=dv)
        assert not [e for e in _authored(pf) if e[0] == "MUST"]


# ---------------------------------------------------------------------------
# mechanism 2 — the optional bullet
# ---------------------------------------------------------------------------
def test_unbulleted_triple_with_a_core_verb_now_parses():
    pf = factlattice.parse_text(FIXTURE, "f.lts")
    assert ("DEPENDS_ON", "DV-A", "DV-B", ()) in _authored(pf)


def test_unbulleted_prose_beginning_with_a_ref_still_yields_nothing():
    text = ("# P [SPEC:P]\n\n## [P-A] A\n\n- status: active\n\n"
            "## Relationships\n\n"
            "[P-A] is discussed at length in the narrative above.\n")
    pf = factlattice.parse_text(text, "f.lts")
    assert _authored(pf) == []


# ---------------------------------------------------------------------------
# the FABRICATION the old positional scan produced
# ---------------------------------------------------------------------------
def test_unknown_verb_no_longer_matches_a_core_type_inside_the_target_id():
    """Before the fix, `[DD-X] DOCUMENTS [ACME-TERM-SUPERSEDES-55]` skipped the
    unrecognised DOCUMENTS, matched `SUPERSEDES` as a SUBSTRING inside the TARGET's
    node id, and emitted an edge to the literal string "-55]". Anchoring the
    unregistered-verb match at the start of the remainder takes the real verb."""
    text = ("# F [SPEC:F]\n\n## [DD-X] X\n\n- status: active\n\n"
            "## Relationships\n\n"
            "- [DD-X] DOCUMENTS [ACME-TERM-SUPERSEDES-55]\n")
    pf = factlattice.parse_text(text, "f.lts")
    (etype, src, tgt, flags), = _authored(pf)
    assert (etype, src, tgt) == ("DOCUMENTS", "DD-X", "ACME-TERM-SUPERSEDES-55")
    assert flags == ("unregistered_verb",)


# ---------------------------------------------------------------------------
# the no-profile guarantee
# ---------------------------------------------------------------------------
def test_no_profile_parse_of_core_only_content_is_byte_identical():
    """`DEFAULT_DOMAIN_VERBS` is EMPTY, so a corpus that only ever used core verbs in
    the bulleted form parses to exactly what it did before this change."""
    text = ("# C [SPEC:C]\n\n## [C-A] A\n\n- status: active\n\n"
            "## [C-B] B\n\n- status: active\n\n"
            "## Relationships\n\n### Semantic\n\n"
            "- [C-A] DEPENDS_ON [C-B]\n- [C-B] RELATED_TO [C-A]\n")
    pf = factlattice.parse_text(text, "f.lts")
    assert _authored(pf) == [("DEPENDS_ON", "C-A", "C-B", ()),
                             ("RELATED_TO", "C-B", "C-A", ())]


def test_corpus_parse_threads_the_profile_verbs_end_to_end(tmp_path):
    (tmp_path / "f.lts").write_text(FIXTURE, encoding="utf-8")
    corpus = factlattice.parse(str(tmp_path), profile=PROFILE)
    kinds = {e.type for e in corpus.edges if e.inference == "authored"}
    assert {"USES", "DEPENDS_ON", "REALIZES"} <= kinds
    assert not [e for e in corpus.edges if "unregistered_verb" in e.flags]
    # ...and the same fact reaches the serialized graph index, not just the AST.
    index = corpus.to_graph_index()
    types = {e["type"] for layer in index["layers"].values() for e in layer["edges"]}
    assert {"USES", "REALIZES"} <= types


# ---------------------------------------------------------------------------
# [SL-18] A MULTI-SEGMENT domain namespace must survive on the edge type.
#
# `_TOKEN_RE` allowed ONE optional dotted segment, so `acme.ds.GOVERNS` tokenized as
# TWO tokens -- `acme.ds` (skipped: lowercase suffix) and `GOVERNS` (matched by bare
# membership). The emitted edge was `GOVERNS` with the NAMESPACE SILENTLY STRIPPED,
# making a domain's verb indistinguishable from any other domain's bare verb of the
# same name. That is the exact collision P3 namespacing exists to prevent, and it
# happened with no flag: preserve-and-flag cannot fire because by the time
# `_type_flags` runs there is no namespace left to be unregistered.
#
# NOT a silent LOSS -- the line was consumed and an edge was emitted. A silent
# SUBSTITUTION, which line accounting is structurally blind to.
#
# TWO-SEGMENT namespaces of this shape are routinely declared (the defect was found
# in a live deployed corpus, 2026-08, and `lib.eng` has the same shape), so this was
# latent for every such domain alike; zero live instances only because each happened
# to author bare verbs. The synthetic `acme.ds` below reproduces the defect exactly:
# what makes it bite is the SHAPE -- a dotted namespace with a lowercase trailing
# segment -- not which domain owns it.
# ---------------------------------------------------------------------------
from factlattice.parser import _find_edge_type, _TOKEN_RE  # noqa: E402

_VERBS = frozenset({"GOVERNS", "ROUTES_TO"})


def test_multi_segment_namespace_survives_on_the_edge_type():
    assert _find_edge_type("acme.ds.GOVERNS [X]", _VERBS) == ("acme.ds.GOVERNS", 15, True)
    assert _find_edge_type("lib.eng.ROUTES_TO [X]", _VERBS) == ("lib.eng.ROUTES_TO", 17, True)


def test_single_segment_namespace_is_unchanged():
    """The pre-existing case must not regress -- it was always correct."""
    assert _find_edge_type("acme.GOVERNS [X]", _VERBS) == ("acme.GOVERNS", 12, True)
    assert _find_edge_type("example.GATES [X]", frozenset()) == ("example.GATES", 13, True)


def test_a_bare_namespace_is_still_not_an_edge_type():
    """`acme.ds` has a lowercase trailing segment: it is a DOMAIN QUALIFIER, not a type.
    Widening the tokenizer must not turn one into the other."""
    assert _find_edge_type("acme.ds [X]", _VERBS) is None
    assert _find_edge_type("gov.example [X]", frozenset()) is None


def test_bare_and_core_verbs_are_unchanged():
    assert _find_edge_type("GOVERNS [X]", _VERBS) == ("GOVERNS", 7, True)
    assert _find_edge_type("DEPENDS_ON [X]", frozenset()) == ("DEPENDS_ON", 10, True)


def test_the_tokenizer_keeps_a_dotted_run_whole():
    """The regression is visible one level down: the branch that handles namespaced
    types was always correct (`tok.split('.')[-1]`); it simply never received the
    whole token."""
    assert [m.group(0) for m in _TOKEN_RE.finditer("acme.ds.GOVERNS [X]")][0] == "acme.ds.GOVERNS"
