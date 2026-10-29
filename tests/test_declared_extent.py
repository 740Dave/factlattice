"""[INV-DECLARED-EXTENT] — A ZERO WITH AN EXTENT BLOCK IS A FACT; A BARE ZERO IS A
RESEARCH TASK.

THE DEFECT THIS PINS. [FL-RECALL-ZERO-IS-AMBIGUOUS] measured `lts_find` returning nothing
as consistent with FIVE distinct states — genuinely absent, wrong corpus id, different
spelling, different key, or outside every corpus root — with NO field in the response able
to discriminate between them. The cost is not a wrong answer; it is that the correct
reaction to any zero became "re-query three more ways and then grep", eleven times in one
session where the engine was the declared instrument.

⛔ AND WHY THE TESTS ASSERT ON A ZERO. Every one of the five states produces an EMPTY
`nodes` list, so a test that only exercises a hit cannot tell whether the discrimination
exists. The zero is the case.
"""
from __future__ import annotations

import os

import pytest

from factlattice_engine.extent import Extent, did_you_mean, unknown_corpus
from factlattice_engine.lifecycle import (
    LIFECYCLE_KEYS, lifecycle_matches, lifecycle_values,
)
from factlattice_engine.store import LtsStore, Partition
from factlattice_engine.tools import LtsEngine, dispatch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXTENT_FIELDS = ("corpora_consulted", "corpora_skipped", "nodes_in_scope",
                 "keys_consulted", "index")


def _index(corpus_id: str, nodes: dict, *, domain="proj.t", built_at="2026-08-13T00:00:00Z"):
    return {
        "generated_at": built_at,
        "source_corpus": {"corpus_id": corpus_id, "domain_suffix": domain,
                          "files_hash": None, "root_paths": [f"/tmp/{corpus_id}"],
                          "built_at": built_at},
        "nodes": nodes,
        "layers": {"semantic": {"edges": []}},
        "stats": {"total_nodes": len(nodes), "total_edges": 0},
    }


def _node(nid, **kw):
    base = {"id": nid, "title": kw.pop("title", nid), "domain": "proj.t",
            "status": kw.pop("status", None), "properties": kw.pop("properties", {}),
            "token_count": 10}
    base.update(kw)
    return base


class _AlwaysFresh:
    def evaluate(self, part):
        from factlattice_engine.store import FreshnessResult
        return FreshnessResult(corpus_id=part.corpus_id, fresh=True,
                               index_hash="h", live_hash="h")


def _engine(*partitions) -> LtsEngine:
    store = LtsStore()
    for p in partitions:
        store.add_partition(p)
    return LtsEngine(store, freshness_provider=_AlwaysFresh())


@pytest.fixture()
def two_corpus_engine():
    a = Partition(_index("alpha", {
        "DEC-ONE": _node("DEC-ONE", status="proposed"),
        "DEC-TWO": _node("DEC-TWO", status="operational (2026-07-07)"),
        # THE MEASURED CASE: a node whose lifecycle lives on `lifecycle-position`, not
        # `status`. `status="contingent"` returned 1 while `lifecycle-position:
        # contingent` held 3, in a different file, OVERLAP ZERO.
        "DEC-THREE": _node("DEC-THREE",
                           properties={"lifecycle-position": "contingent"}),
        "DEC-FOUR": _node("DEC-FOUR",
                          properties={"lifecycle_position": "contingent"}),
    }))
    b = Partition(_index("beta", {
        "SPEC-X": _node("SPEC-X", status="contingent"),
    }, domain="proj.b"))
    return _engine(a, b)


# ---------------------------------------------------------------------------
# 1. EVERY RESULT CARRIES ITS EXTENT — the keystone
# ---------------------------------------------------------------------------
def test_a_zero_from_find_still_declares_what_it_searched(two_corpus_engine):
    """THE headline case. Nothing found, and the response nevertheless names the corpora
    consulted, the node denominator, the keys read, and the index it read from."""
    res = two_corpus_engine.find(text="nothing-in-any-corpus-anywhere")
    assert res["nodes"] == [] and res["total_matched"] == 0

    ext = res["extent"]
    for f in EXTENT_FIELDS:
        assert f in ext, f"a zero that cannot say where it looked is a research task: {f}"
    assert ext["corpora_consulted"] == ["alpha", "beta"]
    assert ext["nodes_in_scope"] == 5            # THE DENOMINATOR
    assert ext["keys_consulted"] == ["id", "title", "properties.*"]
    assert ext["index"]["fresh"] is True
    assert all(ext["index"]["built_at"].values())


def test_the_extent_denominator_is_the_scope_not_the_result(two_corpus_engine):
    """`nodes_in_scope` counts what the predicate RAN AGAINST. A count of what came back
    is `count`, and conflating the two is how a claim of `clean` becomes indistinguishable
    from a check that inspected nothing."""
    hit = two_corpus_engine.find(prefix="DEC")
    assert hit["count"] == 4
    assert hit["extent"]["nodes_in_scope"] == 5


def test_scoping_to_one_corpus_narrows_the_declared_extent(two_corpus_engine):
    res = two_corpus_engine.find(corpus="beta", text="absent")
    assert res["extent"]["corpora_consulted"] == ["beta"]
    assert res["extent"]["nodes_in_scope"] == 1
    assert res["extent"]["corpora_available"] == ["alpha", "beta"]
    assert res["extent"]["scope"]["corpus"] == "beta"


@pytest.mark.parametrize("call", [
    lambda e: e.find(text="zzz"),
    lambda e: e.node(corpus="alpha", node_id="DEC-ONE"),
    lambda e: e.node(corpus="alpha", node_id="NOPE"),
    lambda e: e.traverse(corpus="alpha", start_node="DEC-ONE"),
    lambda e: e.traverse(corpus="alpha", start_node="NOPE"),
    lambda e: e.capabilities(corpus="alpha", topic="DEC-ONE"),
    lambda e: e.list_corpora(),
], ids=["find", "node", "node-miss", "traverse", "traverse-miss", "capabilities",
        "list_corpora"])
def test_every_read_surface_declares_its_extent(two_corpus_engine, call):
    """The cross-surface sweep, for the same reason `test_declared_reach.py` has one: the
    defect class is ONE layer sitting outside a contract the others honour, so a
    single-tool test re-creates the blind spot it is meant to close."""
    res = call(two_corpus_engine)
    assert "extent" in res, "surface answers without saying what it searched"
    for f in EXTENT_FIELDS:
        assert f in res["extent"]
    assert res["extent"]["scope"]["tool"]


def test_a_stale_corpus_is_named_as_skipped_not_silently_dropped():
    """[FL-RECALL-INVISIBLE-PRECONDITIONS] precondition-3: a node added since the last
    parse is invisible and its absence reads as "no such decision". The unscoped `find`
    dropped stale corpora into a sibling field; now the extent names them and flips
    `index.fresh`."""
    good = Partition(_index("good", {"A-ONE": _node("A-ONE")}))
    bad = Partition(_index("bad", {"B-ONE": _node("B-ONE")}, domain="proj.b"))

    class _StaleForBad:
        def evaluate(self, part):
            from factlattice_engine.store import FreshnessResult
            fresh = part.corpus_id != "bad"
            return FreshnessResult(corpus_id=part.corpus_id, fresh=fresh,
                                   index_hash="h", live_hash="h" if fresh else "other")

    store = LtsStore()
    store.add_partition(good)
    store.add_partition(bad)
    eng = LtsEngine(store, freshness_provider=_StaleForBad())

    res = eng.find(text="B-ONE")
    assert res["nodes"] == []                       # it IS in `bad`, and `bad` was skipped
    ext = res["extent"]
    assert ext["corpora_consulted"] == ["good"]
    assert ext["corpora_skipped"] == [{"corpus": "bad", "reason": "stale"}]
    assert ext["index"]["fresh"] is False
    assert ext["index"]["stale_corpora"] == ["bad"]


def test_declare_extent_emits_the_whole_vocabulary_or_none():
    """`Extent` exists so no layer can emit three of five and call it declared — the same
    argument `budget.declare()` makes one level down."""
    d = Extent(corpora_consulted=["b", "a"], nodes_in_scope=7,
               keys_consulted=["status"], index_fresh=True).to_dict()
    assert set(EXTENT_FIELDS) <= set(d)
    assert d["corpora_consulted"] == ["a", "b"]     # deterministic


# ---------------------------------------------------------------------------
# 2. LIFECYCLE QUERIES SWEEP THE UNION AND SAY SO
# ---------------------------------------------------------------------------
def test_a_lifecycle_query_sweeps_every_designated_key(two_corpus_engine):
    """THE MEASURED DEFECT. `status="contingent"` returned ONE node keyed on `status:`
    while `lifecycle-position: contingent` held more, in a different file, with the
    overlap ZERO — and the caller reads "1 contingent decision" as an answer. A contingent
    sweep exists to find UN-DISCHARGED OBLIGATIONS, so this is the worst surface in the
    engine to silently halve."""
    res = two_corpus_engine.find(status="contingent")
    got = {n["id"] for n in res["nodes"]}
    assert got == {"DEC-THREE", "DEC-FOUR", "SPEC-X"}, (
        "the lifecycle sweep did not span every designated key")


def test_the_lifecycle_sweep_reports_the_keys_it_consulted(two_corpus_engine):
    """A zero over `[status, lifecycle-position, lifecycle_position]` is a DIFFERENT fact
    from a zero over `[status]`, and the response must be able to say which it was."""
    res = two_corpus_engine.find(status="never-a-stage")
    assert res["nodes"] == []
    assert res["extent"]["keys_consulted"] == list(LIFECYCLE_KEYS)


def test_a_match_names_the_vocabulary_that_answered(two_corpus_engine):
    """`matched_via` hands the caller the curation finding for free: a corpus keyed on
    `lifecycle-position` stops looking like a corpus with one `status` hit."""
    res = two_corpus_engine.find(status="contingent")
    via = {n["id"]: n.get("matched_via") for n in res["nodes"]}
    assert via == {"DEC-THREE": "lifecycle-position",
                   "DEC-FOUR": "lifecycle_position",
                   "SPEC-X": "status"}
    assert "lifecycle sweep matched via" in res["extent"]["notes"][0]


def test_the_bare_token_widening_still_applies_across_every_key():
    """The union ADDS keys; it does not weaken the predicate. A bare-token query still
    matches the qualified forms the corpus actually authors, on `lifecycle-position` too,
    because all three keys go through the ONE `status_matches`."""
    node = _node("N", properties={"lifecycle-position": "contingent — pending the maintainer"})
    assert lifecycle_matches(node, "contingent") == "lifecycle-position"
    # ...and a QUALIFIED query stays exact, on every key.
    assert lifecycle_matches(node, "contingent — something else") is None


def test_the_lifecycle_key_set_is_shared_with_the_rule_that_validates_it():
    """One definition, two consumers. V6 validating three `lifecycle-position: contingent`
    nodes the query layer could not see is what two independent notions of "the lifecycle
    key" costs."""
    from factlattice_engine.lint import profile_rules
    from factlattice_engine import lifecycle as lc
    assert profile_rules.LIFECYCLE_PROPERTY is lc.LIFECYCLE_PROPERTY
    assert profile_rules.LIFECYCLE_PROPERTY_ALT is lc.LIFECYCLE_PROPERTY_ALT
    assert set(LIFECYCLE_KEYS) == {"status", lc.LIFECYCLE_PROPERTY,
                                   lc.LIFECYCLE_PROPERTY_ALT}


def test_lifecycle_values_reports_only_keys_the_node_carries():
    n = _node("N", status="proposed", properties={"lifecycle-position": ["a", "b"]})
    assert lifecycle_values(n) == {"status": ["proposed"],
                                   "lifecycle-position": ["a", "b"]}


# ---------------------------------------------------------------------------
# 4. DID-YOU-MEAN ON AN UNKNOWN CORPUS ID
# ---------------------------------------------------------------------------
def test_a_misspelled_corpus_is_distinguishable_from_an_empty_one(two_corpus_engine):
    """[FL-RECALL-ZERO-IS-AMBIGUOUS] state-2: "a wrong corpus id and a missing corpus
    PRESENT IDENTICALLY". They no longer do."""
    res = two_corpus_engine.find(corpus="aplha")
    assert res["success"] is False
    assert res["did_you_mean"] == ["alpha"]
    assert res["corpora_available"] == ["alpha", "beta"]


def test_the_suggestion_is_in_the_error_string_callers_actually_quote(two_corpus_engine):
    """A `did_you_mean` field beside a bare "corpus not found" is exactly the "12 errors
    sitting beside a prose caveat" failure. The field callers read must carry its own
    caveat."""
    res = two_corpus_engine.node(corpus="aplha", node_id="DEC-ONE")
    assert "did you mean 'alpha'" in res["error"]


def test_an_unknown_corpus_with_no_near_match_still_names_the_loaded_set(two_corpus_engine):
    res = two_corpus_engine.traverse(corpus="zzzzzzzz", start_node="X")
    assert res["did_you_mean"] == []
    assert "loaded corpora:" in res["error"]


def test_a_case_only_difference_is_always_offered():
    """`Acme-Data-Science` for `acme-data-science` is the single most likely typo, and a
    similarity cut has no business filtering it out."""
    assert did_you_mean("Acme-Data-Science", ["acme-data-science", "other"]) == ["acme-data-science"]


def test_with_no_corpora_loaded_the_error_says_so_rather_than_suggesting_nothing():
    out = unknown_corpus("anything", [])
    assert "NO public corpora are loaded" in out["error"]


def test_an_unknown_node_id_gets_the_same_treatment(two_corpus_engine):
    """A not-found NODE is the single-node form of the ambiguous zero."""
    res = two_corpus_engine.node(corpus="alpha", node_id="DEC-ON")
    assert res["node"] is None
    assert "DEC-ONE" in res["did_you_mean"]
    assert res["extent"]["nodes_in_scope"] == 4


# ---------------------------------------------------------------------------
# list_corpora: counts carry their freshness INLINE
# ---------------------------------------------------------------------------
def test_list_corpora_counts_carry_their_own_freshness(two_corpus_engine):
    """"Counts quoted from `list_corpora` should carry the freshness flag INLINE, not in
    a sibling field a caller may not read." — and `total_nodes` (off the index `stats`)
    is cross-checked against the live map, so two independent readings of one fact
    disagree VISIBLY."""
    res = two_corpus_engine.list_corpora()
    for c in res["corpora"]:
        assert "fresh" in c and "built_at" in c and "counts_are_as_of" in c
        assert c["counts_agree"] is True
        assert c["total_nodes"] == c["total_nodes_live"]
    assert res["extent"]["index"]["fresh"] is True
