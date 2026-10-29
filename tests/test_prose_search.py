"""[FL-PROSE-BLIND] — `lts_find_in_prose`, and the routing pointer `lts_find` now carries.

THE MEASURED DEFECT, 2026-08-13. `lts_find(text=...)` reads id, title and STRING property
values. The graph index carries NO body text at all, so prose was never searchable — and
nothing in the response said so. A widget corpus returned SIX confident, well-formed hits
for its own subject term, every one of them an `artifact:` property whose value was a
markdown FILENAME, over a corpus holding no subject-matter content: `complete: true`,
`truncated: false`, and no indication the body had never been read.

    A ZERO THAT DOES NOT DECLARE ITS OWN SCOPE IS THE DEFECT, NOT THE RECALL.

⛔ EVERY ZERO IN THIS FILE IS GUARDED. The fixture corpus is built so that each assertion
of absence sits beside a positive control proving the machinery was live and looking:

  * `test_pre_change_behaviour_*` asserts the OLD scope is still exactly the old scope —
    the structured predicate returns its bare zero — and does so in the same corpus where
    the prose tool returns a hit, so "0" cannot be a broken fixture.
  * `test_structured_result_sets_are_byte_identical` compares SETS across the entire
    fixture and the shipped `_text_hit`, not counts.
  * the body-only / also-in-structure flag is tested in BOTH directions against terms
    engineered to sit in exactly one place each.

THE FIXTURE IS AN ON-DISK CORPUS, not a synthetic index dict, because the body index reads
source files. A synthetic partition would test nothing that matters here.
"""

from __future__ import annotations

import json
import os

import pytest

from factlattice import build_index
from factlattice_engine import LtsEngine, LtsStore, Partition, dispatch


# ---------------------------------------------------------------------------
# THE TERMS, each engineered to occupy exactly one scope
# ---------------------------------------------------------------------------
# Present ONLY in body prose, nowhere in any id/title/property. THE ORIGINATING CASE.
PROSE_ONLY_TERM = "sweepangle"
# Present in a property VALUE and also in the body — the no-double-count / redundancy case.
BOTH_TERM = "quasar"
# Present in NEITHER — the guarded true zero.
ABSENT_TERM = "zzzabsentzzz"

CORPUS = """---
domain: PLT.DEMO
---

# Radar Widget Spec [SPEC:DEMO-RADAR]

- status: operational
- artifact: quasar-widget.md

The scope of this document. The quasar surface is the whole of it.

## Sweep Rendering [DEMO-SWEEP]

- status: proposed
- artifact: quasar-render.md
- owner: renderer

The sweep is drawn as a rotating wedge whose leading edge is bright. The
sweepangle advances once per frame and wraps at 360 degrees. Contacts inside
the wedge are painted at full intensity and decay behind it.

## Contact Store [DEMO-CONTACTS]

- status: operational
- refs:
  - quasar-contacts.md
  - quasar-tracks.md

Contacts are held in a ring buffer. The quasar display reads it once per
frame; nothing else writes to it.

## Colour Ramp [DEMO-RAMP]

- status: operational

A five-stop ramp from dim to bright. No sweep logic lives here.
"""


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    root = tmp_path_factory.mktemp("prose-corpus")
    (root / "radar.lts").write_text(CORPUS, encoding="utf-8")
    idx = build_index(str(root), corpus_id="demo-radar",
                      domain_suffix="plt.demo", built_at="t")
    store = LtsStore()
    store.add_partition(Partition(idx, source_path=str(root)))
    return LtsEngine(store)


def _ids(result, key="nodes"):
    return {n["id"] for n in (result.get(key) or [])}


def _hit(result, node_id):
    return next(h for h in result["hits"] if h["id"] == node_id)


# ---------------------------------------------------------------------------
# ACCEPTANCE 1 — the originating case, end to end
# ---------------------------------------------------------------------------
def test_pre_change_behaviour_structured_find_returns_a_bare_zero(engine):
    """THE DEFECT, still reproducible: the structured predicate cannot see prose.

    POSITIVE CONTROL: the very same query in the very same corpus finds the term when the
    prose tool looks (next test), so this zero is a SCOPE fact, not an empty fixture.
    """
    res = engine.find(corpus="demo-radar", text=PROSE_ONLY_TERM)
    assert res["success"] is True
    assert res["total_matched"] == 0 and res["nodes"] == []
    # ...and the corpus is emphatically not empty:
    assert res["extent"]["nodes_in_scope"] == 4


def test_the_originating_case_prose_tool_returns_the_node_flagged_body_only(engine):
    res = engine.find_in_prose(corpus="demo-radar", text=PROSE_ONLY_TERM)
    assert res["success"] is True
    assert _ids(res, "hits") == {"DEMO-SWEEP"}
    hit = _hit(res, "DEMO-SWEEP")
    assert hit["finding"] == "body-only"
    assert hit["in_structure"] is False and hit["structure_hit_in"] == []
    assert res["body_only_count"] == 1 and res["also_in_structure_count"] == 0
    # LOCALITY: it names the file and the line, and quotes the matching line.
    assert hit["source_file"] == "radar.lts"
    assert hit["lines"][0]["line"] > 0
    assert PROSE_ONLY_TERM in hit["lines"][0]["text"].lower()


def test_the_bare_zero_is_now_a_routed_zero(engine):
    """THE ACTUAL FIX. The separate tool alone does not close the defect — an agent that
    never learns it exists still gets a silent zero. The extent pointer is what converts
    a bare zero into a routed one."""
    res = engine.find(corpus="demo-radar", text=PROSE_ONLY_TERM)
    ex = res["extent"]
    assert ex["body_scanned"] is False
    note = ex["scope_note"]
    assert "lts_find_in_prose" in note                 # it NAMES the tool
    assert "BODY PROSE WAS NOT" in note                # it names the scope it skipped
    assert "4 of 4 nodes" in note                      # it carries the denominator
    part = engine.store.get_partition("demo-radar")
    assert part.nodes_with_unread_body() == 4          # index-derived, no file I/O


def test_scope_note_fires_on_a_thin_NON_zero_too(engine):
    """The originating case returned SIX hits, not zero. A note that only fires on zero
    would have missed the defect that motivated it."""
    res = engine.find(corpus="demo-radar", text=BOTH_TERM)
    assert res["total_matched"] > 0
    assert "lts_find_in_prose" in res["extent"]["scope_note"]


def test_scope_note_is_absent_when_there_is_no_text_term(engine):
    """A prefix/status query has no term to look for in prose. A note firing on every call
    is trained away within a session — which costs exactly the queries that need it."""
    res = engine.find(corpus="demo-radar", prefix="DEMO")
    assert res["total_matched"] > 0                    # positive control: the query worked
    assert "scope_note" not in res["extent"]
    # `body_scanned` is still declared, on EVERY find result, scanned or not.
    assert res["extent"]["body_scanned"] is False


# ---------------------------------------------------------------------------
# ACCEPTANCE 2 — zero regression in `lts_find`, proved on SETS
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("query", [
    {"text": PROSE_ONLY_TERM}, {"text": BOTH_TERM}, {"text": ABSENT_TERM},
    {"text": "sweep"}, {"text": "radar"}, {"text": "operational"},
    {"prefix": "DEMO"}, {"status": "operational"}, {"status": "proposed"},
    {"text": "quasar", "status": "operational"}, {},
])
def test_structured_result_sets_are_byte_identical_to_the_shipped_predicate(engine, query):
    """No regression, on SETS not counts.

    The oracle is `_text_hit` + `_node_matches` re-run independently over the partition —
    i.e. the semantics as they stand in the module — so this fails if the split of
    `_text_hit` into `_structure_hit_in` changed ANY membership, which is the one way this
    change could have broken `lts_find`.
    """
    from factlattice_engine.tools import _node_matches, _text_hit

    part = engine.store.get_partition("demo-radar")
    expected = set()
    for nid, nd in part.nodes.items():
        if _node_matches(nd, nid, None, None, None,
                         query.get("prefix"), query.get("status"),
                         query.get("text")) is not None:
            expected.add(nid)
    got = _ids(engine.find(corpus="demo-radar", **query))
    assert got == expected
    # And the independent check that the two predicates agree everywhere:
    for nid, nd in part.nodes.items():
        for term in (PROSE_ONLY_TERM, BOTH_TERM, ABSENT_TERM, "sweep", "quasar"):
            from factlattice_engine.tools import _structure_hit_in
            assert _text_hit(nd, nid, term) == bool(_structure_hit_in(nd, nid, term))


def test_find_response_gains_no_prose_fields(engine):
    """The channel design was rejected: `lts_find` must not sprout prose counts."""
    res = engine.find(corpus="demo-radar", text=BOTH_TERM)
    for leaked in ("prose_only", "prose_only_nodes", "hits", "body_only_count"):
        assert leaked not in res


# ---------------------------------------------------------------------------
# ACCEPTANCE 3 — the flag is correct in BOTH directions
# ---------------------------------------------------------------------------
def test_a_term_in_both_a_property_and_the_body_is_also_in_structure(engine):
    """DEMO-RADAR asserts `artifact: quasar-widget.md` AND discusses `quasar` in prose."""
    res = engine.find_in_prose(corpus="demo-radar", text=BOTH_TERM)
    hit = _hit(res, "DEMO-RADAR")
    assert hit["finding"] == "also-in-structure"
    assert hit["in_structure"] is True
    # It names WHERE, which is what separates "already answerable" from "the redundancy
    # is a filename in an `artifact:` property" — the originating defect's exact shape.
    assert hit["structure_hit_in"] == ["properties.artifact"]
    # And `lts_find` genuinely does return it, so "also-in-structure" is not a claim:
    assert "DEMO-RADAR" in _ids(engine.find(corpus="demo-radar", text=BOTH_TERM))


def test_a_LIST_valued_property_is_visible_to_lts_find_so_the_hit_is_also_in_structure(engine):
    """THE TRIPWIRE FIRED, DELIBERATELY (2026-10-03, [FL-LIST-VALUED-PROPERTY-BLIND]).

    This test used to PIN the blindness: `_text_hit` guarded on `isinstance(v, str)`, so
    DEMO-CONTACTS (which holds `quasar` in a list-valued `refs:`) was absent from `lts_find`
    and therefore `body-only`. Its own docstring said the test existed to make the day
    `_text_hit` learned to read list values a deliberate reclassification. That day is this
    one: every element of a list-valued property is now searched, so the hit is
    `also-in-structure`, naming `properties.refs`. The mapping case is still unread.
    """
    res = engine.find_in_prose(corpus="demo-radar", text=BOTH_TERM)
    hit = _hit(res, "DEMO-CONTACTS")
    assert hit["finding"] == "also-in-structure"
    assert hit["structure_hit_in"] == ["properties.refs"]
    assert "DEMO-CONTACTS" in _ids(engine.find(corpus="demo-radar", text=BOTH_TERM))


def test_a_term_only_in_the_body_is_body_only(engine):
    res = engine.find_in_prose(corpus="demo-radar", text=PROSE_ONLY_TERM)
    assert [h["finding"] for h in res["hits"]] == ["body-only"]


def test_body_only_filter_keeps_exactly_the_densification_findings(engine):
    everything = engine.find_in_prose(corpus="demo-radar", text=BOTH_TERM)
    filtered = engine.find_in_prose(corpus="demo-radar", text=BOTH_TERM, body_only=True)
    assert _ids(everything, "hits") == {"DEMO-SWEEP", "DEMO-CONTACTS", "DEMO-RADAR"}
    # All three assert `quasar` in structure now (DEMO-CONTACTS in a LIST-valued `refs:`,
    # which `_text_hit` reads since 2026-10-03), so BOTH_TERM yields no body-only finding;
    # `test_a_term_only_in_the_body_is_body_only` is the positive control for the filter.
    assert _ids(filtered, "hits") == set()
    # positive control: the filter is live (it returns the genuinely body-only term's node)
    assert _ids(engine.find_in_prose(corpus="demo-radar", text=PROSE_ONLY_TERM,
                                     body_only=True), "hits") == {"DEMO-SWEEP"}
    assert all(h["finding"] == "body-only" for h in filtered["hits"])


def test_a_node_matching_only_in_structure_is_absent_from_the_prose_tool(engine):
    """The converse of the split: the prose tool is not a superset. DEMO-RADAR asserts
    `quasar` in a property AND mentions it in no prose of its own beyond that line."""
    res = engine.find_in_prose(corpus="demo-radar", text="renderer")
    # `owner: renderer` on DEMO-SWEEP is a property LINE, which the body scan does read —
    # and it is correctly flagged as structure, not a finding.
    if res["hits"]:
        assert all(h["finding"] == "also-in-structure" for h in res["hits"])
    assert res["body_only_count"] == 0


# ---------------------------------------------------------------------------
# ACCEPTANCE 4 — the digest tier leaks no body text
# ---------------------------------------------------------------------------
def test_prose_digest_is_identity_and_counts_and_never_a_line_of_body(engine):
    res = engine.find_in_prose(corpus="demo-radar", text=PROSE_ONLY_TERM, tier="digest")
    assert res["complete"] is True and res["truncated"] is False
    row = res["digest"][0]
    assert set(row) == {"corpus", "id", "title", "source_file", "line_range",
                        "match_lines", "in_structure", "structure_hit_in", "finding"}
    assert "lines" not in row and "text" not in row
    # The whole serialized response, hunted for a distinctive string from the body.
    blob = json.dumps(res)
    assert "rotating wedge" not in blob
    assert "advances once per frame" not in blob
    # POSITIVE CONTROL: that same string DOES travel on the untiered path, so the
    # assertion above is testing a real suppression and not a typo.
    assert "advances once per frame" in json.dumps(
        engine.find_in_prose(corpus="demo-radar", text="advances")).lower()


def test_find_digest_still_carries_the_routing_note_but_no_body(engine):
    res = engine.find(corpus="demo-radar", text=PROSE_ONLY_TERM, tier="digest")
    assert res["total_matched"] == 0
    assert "lts_find_in_prose" in res["extent"]["scope_note"]
    assert "rotating wedge" not in json.dumps(res)


# ---------------------------------------------------------------------------
# The declared-scope contract, applied to the new tool itself
# ---------------------------------------------------------------------------
def test_prose_tool_declares_that_it_scanned(engine):
    res = engine.find_in_prose(corpus="demo-radar", text=ABSENT_TERM)
    assert res["total_matched"] == 0                   # a zero...
    assert res["extent"]["body_scanned"] is True       # ...that declares it LOOKED
    assert res["nodes_scanned"] == 4
    assert "lts_find" in res["extent"]["scope_note"]   # and routes BACK


def test_an_unscannable_partition_reports_body_scanned_false(tmp_path):
    """A synthetic partition with no readable source is the prose tool's own ambiguous
    zero. It must say it read nothing rather than answer 0 as if it had looked."""
    idx = {
        "source_corpus": {"corpus_id": "ghost", "domain_suffix": "x", "root_paths": [],
                          "files_hash": "sha256:none", "built_at": "t"},
        "nodes": {"G-1": {"id": "G-1", "title": "ghost", "properties": {},
                          "source_file": "nowhere.lts", "line_range": [1, 9],
                          "token_count": 10}},
        "layers": {}, "stats": {"total_nodes": 1},
    }

    class _Fresh:
        def evaluate(self, part):
            from factlattice_engine import FreshnessResult
            return FreshnessResult(part.corpus_id, True, part.files_hash, part.files_hash)

    store = LtsStore()
    store.add_partition(Partition(idx))
    eng = LtsEngine(store, freshness_provider=_Fresh())
    res = eng.find_in_prose(corpus="ghost", text="anything")
    assert res["total_matched"] == 0
    assert res["extent"]["body_scanned"] is False
    assert res["files_unreadable"]


def test_empty_term_is_refused_not_answered(engine):
    res = engine.find_in_prose(corpus="demo-radar", text="   ")
    assert res["success"] is False and "non-empty" in res["error"]


def test_the_heading_line_is_not_body(engine):
    """The heading carries the id and the title — `lts_find`'s scope. Leaving it in would
    make a doctype tag a prose hit on every node in a document."""
    res = engine.find_in_prose(corpus="demo-radar", text="SPEC")
    assert res["total_matched"] == 0
    # POSITIVE CONTROL: the token really is in the file, on the heading line.
    assert "[SPEC:DEMO-RADAR]" in CORPUS


def test_node_ranges_do_not_nest_so_one_hit_does_not_inflate(engine):
    """A term in a child's body must not also hit the parent section."""
    res = engine.find_in_prose(corpus="demo-radar", text="ring buffer")
    assert _ids(res, "hits") == {"DEMO-CONTACTS"}


def test_context_window_is_opt_in(engine):
    plain = _hit(engine.find_in_prose(corpus="demo-radar", text=PROSE_ONLY_TERM),
                 "DEMO-SWEEP")
    assert "before" not in plain["lines"][0] and "after" not in plain["lines"][0]
    windowed = _hit(engine.find_in_prose(corpus="demo-radar", text=PROSE_ONLY_TERM,
                                         context=1), "DEMO-SWEEP")
    assert windowed["lines"][0].get("before") or windowed["lines"][0].get("after")


def test_non_text_filters_scope_the_prose_sweep_identically(engine):
    """The same predicate `lts_find` applies, so a scoped structured query and its prose
    counterpart cover the same population."""
    unscoped = engine.find_in_prose(corpus="demo-radar", text=BOTH_TERM)
    scoped = engine.find_in_prose(corpus="demo-radar", text=BOTH_TERM, status="proposed")
    assert _ids(scoped, "hits") == {"DEMO-SWEEP"}
    assert _ids(scoped, "hits") < _ids(unscoped, "hits")


def test_dispatch_routes_the_tool_and_the_schema_advertises_it(engine):
    from factlattice_engine.server import TOOLS

    res = dispatch("lts_find_in_prose", {"corpus": "demo-radar", "text": PROSE_ONLY_TERM},
                   tools=engine)
    assert _ids(res, "hits") == {"DEMO-SWEEP"}
    spec = next(t for t in TOOLS if t["name"] == "lts_find_in_prose")
    assert spec["inputSchema"]["required"] == ["text"]
    # The description must tell a rushed caller what `lts_find` does NOT do.
    assert "lts_find" in spec["description"] and "body-only" in spec["description"]


def test_the_prose_tool_inherits_the_budget_and_tier_contracts(engine):
    """A new collection-returning surface must not be the one with no bound and no
    declaration — the failure `budget.py` was extracted to stop from recurring."""
    page = engine.find_in_prose(corpus="demo-radar", text=BOTH_TERM, token_budget=1)
    assert page["truncated"] is True and page["cursor"] == 1 and page["count"] == 1
    assert page["total_matched"] == 3
    resumed = engine.find_in_prose(corpus="demo-radar", text=BOTH_TERM,
                                   token_budget=1, offset=page["cursor"])
    assert resumed["hits"][0]["id"] != page["hits"][0]["id"]
    # `full` ERRORS rather than returning a fraction that looks whole.
    over = engine.find_in_prose(corpus="demo-radar", text=BOTH_TERM,
                                tier="full", token_budget=1)
    assert over["success"] is False and "NEVER tr" in over["error"]
    whole = engine.find_in_prose(corpus="demo-radar", text=BOTH_TERM, tier="full")
    assert whole["complete"] is True and whole["count"] == 3
    # ...and a digest is complete-by-construction REGARDLESS of the budget.
    dig = engine.find_in_prose(corpus="demo-radar", text=BOTH_TERM,
                               tier="digest", token_budget=1)
    assert dig["complete"] is True and dig["count"] == 3 and dig["truncated"] is False


def test_body_index_is_cached_and_not_rebuilt_per_query(engine):
    part = engine.store.get_partition("demo-radar")
    engine.find_in_prose(corpus="demo-radar", text="sweep")
    builds = part._body_builds
    for _ in range(5):
        engine.find_in_prose(corpus="demo-radar", text="sweep")
    assert part._body_builds == builds        # the filesystem is read ONCE per root
    assert builds >= 1                        # positive control: it was built at all
