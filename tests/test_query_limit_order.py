"""[QUERY-LIMIT-ORDER] `limit` + `order_by` on `lts_find` / `lts_traverse`.

Every `test_scen_*` below is one `[SCEN-*]` acceptance node from
`specs/20-engine-layers.lts`'s `[QUERY-LIMIT-ORDER]` section, named after it so the
correspondence is checkable by eye:

    test_scen_engine_never_defaults_limit           <- [SCEN-ENGINE-NEVER-DEFAULTS-LIMIT]
    test_scen_order_of_ops_limit_applies_last        <- [SCEN-ORDER-OF-OPS-LIMIT-APPLIES-LAST]
    test_scen_null_order_field_sorts_last            <- [SCEN-NULL-ORDER-FIELD-SORTS-LAST]
    test_scen_digest_limit_bounded_no_error          <- [SCEN-DIGEST-LIMIT-BOUNDED-NO-ERROR]
    test_scen_digest_limit_rescues_oversized_corpus  <- [SCEN-DIGEST-LIMIT-RESCUES-OVERSIZED-CORPUS]
    test_scen_limit_exceeds_digest_ceiling           <- [SCEN-LIMIT-EXCEEDS-DIGEST-CEILING]
    test_scen_page_cursor_never_escapes_limit        <- [SCEN-PAGE-CURSOR-NEVER-ESCAPES-LIMIT]
    test_scen_full_limit_still_respects_budget       <- [SCEN-FULL-LIMIT-STILL-RESPECTS-BUDGET]
    test_scen_result_metadata_distinguishes_limit_from_complete
                                                      <- [SCEN-RESULT-METADATA-DISTINGUISHES-LIMIT-FROM-COMPLETE]
    test_scen_backward_compat_no_params              <- [SCEN-BACKWARD-COMPAT-NO-PARAMS]

Plus the two the brief asks for beyond the ten: a byte-identical no-param regression
over `find`/`traverse` at various offsets (`test_backward_compat_byte_identical_*`),
and a determinism test for [SPEC-NULL-ORDERING]'s tiebreak
(`test_null_ordering_is_deterministic_run_to_run`).

⛔ P0 REGRESSION (gap-analyzer, post-ship): `complete` and `bound_reason` derive from
`truncated`/`count-vs-windowed_total`, neither of which is offset-aware — both
falsely read "more to fetch" on the exhausted last page of a limited window, and
`complete` falsely read `false` on a SUCCESSFUL `full`+`limit` call, because a
successful `full` always has `cursor is None` regardless of whether `limit` narrowed
it below the raw match count. `test_p0_*` below pin the two reproduced cases;
`test_p2_*` covers the unorderable-count-under-limit windowed-scoping boundary
(deviation #1) that shipped verified-faithful but untested.
"""

from __future__ import annotations

import pytest

from factlattice_engine import LtsEngine, LtsStore, Partition
from factlattice_engine.tiers import DIGEST_MAX_NODES


class _AlwaysFresh:
    def evaluate(self, part):
        from factlattice_engine import FreshnessResult
        return FreshnessResult(part.corpus_id, True, part.files_hash, part.files_hash)


def _node(nid, *, title=None, tokens=10, area=None, decided=None, props=None):
    p = dict(props or {})
    if decided:
        p["decided-in-session"] = decided
    return {"id": nid, "title": title or f"title of {nid}", "domain": None,
            "status": "operational", "area": area, "properties": p,
            "source_file": f"{nid.lower()}.lts", "line_range": [1, 5],
            "content_hash": f"sha256:{nid.lower()}", "token_count": tokens}


def _edge(src, tgt, etype="DEPENDS_ON", layer="semantic"):
    return {"source": src, "target": tgt, "type": etype, "layer": layer,
            "target_domain": None, "dangling": False, "flags": []}


def _mk_index(corpus_id, domain_suffix, nodes, edges):
    layers = {}
    for e in edges:
        layers.setdefault(e["layer"], {"edges": []})["edges"].append(e)
    return {
        "source_corpus": {"corpus_id": corpus_id, "domain_suffix": domain_suffix,
                          "files_hash": "sha256:test", "root_paths": []},
        "nodes": nodes, "layers": layers,
        "stats": {"total_nodes": len(nodes), "total_edges": len(edges)},
    }


def _engine(nodes, edges=(), corpus_id="proj-a"):
    store = LtsStore()
    store.add_partition(Partition(_mk_index(corpus_id, "proj.a", dict(nodes), list(edges))))
    return LtsEngine(store, freshness_provider=_AlwaysFresh())


# =====================================================================
# [SCEN-ENGINE-NEVER-DEFAULTS-LIMIT]
# =====================================================================
def test_scen_engine_never_defaults_limit():
    """Omitting `limit` is unbounded, not "30" — 45 matching nodes, no limit given,
    the response carries all 45 subject only to the tier's own pre-existing ceiling."""
    nodes = {f"N{i}": _node(f"N{i}") for i in range(45)}
    eng = _engine(nodes)

    r = eng.find(corpus="proj-a", tier="digest")
    assert r["success"] is True
    assert r["count"] == r["total_matched"] == 45
    assert "limit_applied" not in r

    r2 = eng.find(corpus="proj-a")
    assert r2["count"] == 45
    assert "limit_applied" not in r2


# =====================================================================
# [SCEN-ORDER-OF-OPS-LIMIT-APPLIES-LAST]
# =====================================================================
def test_scen_order_of_ops_limit_applies_last():
    """`area="decisions"`, `order_by="recency"`, `limit=5` — the 5 most recent
    decisions, not the 5 most recent nodes overall. Filtering happens before ordering
    and limiting, never after."""
    nodes = {}
    # 5 "other" nodes, all NEWER than every decision — if limit/order_by ran before
    # the area filter, these would leak into the result.
    for i in range(5):
        nodes[f"OTHER{i}"] = _node(f"OTHER{i}", area="specs",
                                   decided=f"2026-09-{20 + i:02d}")
    # 10 decisions, oldest to newest; the 5 most recent are D5..D9.
    for i in range(10):
        nodes[f"D{i}"] = _node(f"D{i}", area="decisions",
                               decided=f"2026-01-{i + 1:02d}")
    eng = _engine(nodes)

    r = eng.find(corpus="proj-a", area="decisions", order_by="recency", limit=5)
    assert r["success"] is True
    ids = {n["id"] for n in r["nodes"]}
    assert ids == {"D5", "D6", "D7", "D8", "D9"}
    assert r["total_matched"] == 10  # the area-filtered count, not 15


# =====================================================================
# [SCEN-NULL-ORDER-FIELD-SORTS-LAST]
# =====================================================================
def test_scen_null_order_field_sorts_last():
    """3 of 40 matched nodes carry no `decided-in-session`; `order_by="recency"` puts
    those 3 LAST (after all 37 dated nodes, newest-first), tiebroken by id ascending,
    and `order_applied.unorderable_count` reports 3."""
    nodes = {}
    for i in range(37):
        nodes[f"D{i:02d}"] = _node(f"D{i:02d}", decided=f"2026-01-{(i % 28) + 1:02d}")
    for nid in ("ZNODATE", "ANODATE", "MNODATE"):
        nodes[nid] = _node(nid)  # no `decided-in-session` at all
    eng = _engine(nodes)

    r = eng.find(corpus="proj-a", order_by="recency")
    assert r["success"] is True
    assert r["order_applied"]["unorderable_count"] == 3
    tail = [n["id"] for n in r["nodes"][-3:]]
    assert tail == ["ANODATE", "MNODATE", "ZNODATE"]  # id ascending tiebreak


# =====================================================================
# [SCEN-DIGEST-LIMIT-BOUNDED-NO-ERROR]
# =====================================================================
def test_scen_digest_limit_bounded_no_error():
    """The headline case: newest 30 of (comfortably-under-ceiling) matches, complete
    and predictably sized."""
    nodes = {f"D{i:03d}": _node(f"D{i:03d}", decided=f"2026-{(i % 12) + 1:02d}-01")
             for i in range(100)}
    eng = _engine(nodes)

    r = eng.find(corpus="proj-a", order_by="recency", limit=30, tier="digest")
    assert r["success"] is True
    assert r["count"] == 30
    assert r["total_matched"] == 100
    assert r["truncated"] is True
    assert r["limit_applied"] == 30
    assert r["windowed_total"] == 30
    assert r["bound_reason"] == "none"


# =====================================================================
# [SCEN-DIGEST-LIMIT-RESCUES-OVERSIZED-CORPUS]
# =====================================================================
def test_scen_digest_limit_rescues_oversized_corpus():
    """A match set over `DIGEST_MAX_NODES` refuses unbounded; `limit` rescues it by
    re-basing the ceiling check on `len(windowed)`."""
    n = DIGEST_MAX_NODES + 800
    nodes = {f"N{i:05d}": _node(f"N{i:05d}", decided=f"2026-01-{(i % 28) + 1:02d}")
             for i in range(n)}
    eng = _engine(nodes)

    unbounded = eng.find(corpus="proj-a", tier="digest")
    assert unbounded["success"] is False
    assert unbounded["reason"] == "digest_ceiling_exceeded"

    rescued = eng.find(corpus="proj-a", order_by="recency", limit=30, tier="digest")
    assert rescued["success"] is True
    assert rescued["count"] == 30
    assert rescued["total_matched"] == n


# =====================================================================
# [SCEN-LIMIT-EXCEEDS-DIGEST-CEILING]
# =====================================================================
def test_scen_limit_exceeds_digest_ceiling():
    """A `limit` bigger than the match set has no clamping effect; if the (unclamped)
    windowed set still exceeds the ceiling, the call refuses exactly as an unbounded
    call would, naming `len(windowed)`."""
    n = DIGEST_MAX_NODES + 800
    nodes = {f"N{i:05d}": _node(f"N{i:05d}") for i in range(n)}
    eng = _engine(nodes)

    r = eng.find(corpus="proj-a", tier="digest", limit=5000)
    assert r["success"] is False
    assert r["reason"] == "digest_ceiling_exceeded"
    assert r["total"] == n  # limit(5000) > match set(n) => no clamping


# =====================================================================
# [SCEN-PAGE-CURSOR-NEVER-ESCAPES-LIMIT]
# =====================================================================
def test_scen_page_cursor_never_escapes_limit():
    """`limit=30` against 373-ish matched nodes, with a `token_budget` small enough to
    return 10 nodes per call: the third call's `next_offset` is `null` — nothing more
    to page to — even though `total_matched` says the corpus holds far more."""
    nodes = {f"D{i:03d}": _node(f"D{i:03d}", tokens=10,
                                decided=f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}")
             for i in range(373)}
    eng = _engine(nodes)

    call = lambda off: eng.find(corpus="proj-a", order_by="recency", limit=30,
                                tier="page", token_budget=100, offset=off)
    r0 = call(0)
    assert r0["count"] == 10 and r0["next_offset"] == 10
    r1 = call(10)
    assert r1["count"] == 10 and r1["next_offset"] == 20
    r2 = call(20)
    assert r2["count"] == 10
    assert r2["next_offset"] is None  # the window (30) is exhausted
    assert r2["total_matched"] == 373  # ...even though the corpus holds far more


# =====================================================================
# [SCEN-FULL-LIMIT-STILL-RESPECTS-BUDGET]
# =====================================================================
def test_scen_full_limit_still_respects_budget():
    """`limit=10` narrows a 40-node match set to the 10 newest, but those 10 nodes'
    combined `token_count` exceeds the caller's `token_budget`: `full` still refuses,
    naming the 10-node `total_token_count`."""
    nodes = {f"D{i:03d}": _node(f"D{i:03d}", tokens=1000,
                                decided=f"2026-{(i % 12) + 1:02d}-01")
             for i in range(40)}
    eng = _engine(nodes)

    r = eng.find(corpus="proj-a", order_by="recency", limit=10, tier="full",
                token_budget=5000)  # 10 nodes * 1000 tokens > 5000
    assert r["success"] is False
    assert r["reason"] == "full_ceiling_exceeded"
    assert r["total"] == 10
    assert r["total_token_count"] == 10000


# =====================================================================
# [SCEN-RESULT-METADATA-DISTINGUISHES-LIMIT-FROM-COMPLETE]
# =====================================================================
def test_scen_result_metadata_distinguishes_limit_from_complete():
    """"Newest 30 of 108" is never mistakable for "all 30 that exist"."""
    big_nodes = {f"B{i:03d}": _node(f"B{i:03d}", decided=f"2026-01-{(i % 28) + 1:02d}")
                for i in range(108)}
    small_nodes = {f"S{i:03d}": _node(f"S{i:03d}") for i in range(30)}
    eng_big = _engine(big_nodes, corpus_id="big")
    eng_small = _engine(small_nodes, corpus_id="small")

    limited = eng_big.find(corpus="big", order_by="recency", limit=30)
    complete = eng_small.find(corpus="small")

    assert len(limited["nodes"]) == len(complete["nodes"]) == 30
    assert limited["total_matched"] == 108 and limited["truncated"] is True
    assert limited["windowed_total"] == 30
    assert complete["total_matched"] == 30 and complete["truncated"] is False
    assert "limit_applied" not in complete and "windowed_total" not in complete


# =====================================================================
# [SCEN-BACKWARD-COMPAT-NO-PARAMS] + the byte-identical regression the brief asks for
# =====================================================================
def test_scen_backward_compat_no_params():
    """An unmodified caller sees a byte-identical response — field for field,
    ordering included — whether or not this feature shipped."""
    nodes = {f"N{i:03d}": _node(f"N{i:03d}", decided=f"2026-{(i % 12) + 1:02d}-01")
             for i in range(50)}
    eng = _engine(nodes)

    a = eng.find(corpus="proj-a")
    b = eng.find(corpus="proj-a", limit=None, order_by=None)
    assert a == b
    for r in (a, b):
        for key in ("limit_applied", "order_applied", "windowed_total", "bound_reason"):
            assert key not in r


def test_backward_compat_byte_identical_find_across_offsets():
    """[SPEC-BACKWARD-COMPAT] enumerated: the default order, the tier ceilings, and
    the four new fields' absence hold at every `offset`, not only `offset=0` — the
    edge this implementation has to get right without a dedicated SCEN node."""
    nodes = {f"N{i:03d}": _node(f"N{i:03d}", tokens=100) for i in range(37)}
    eng = _engine(nodes)
    for off in (0, 5, 20, 36, 37, 100):
        before = eng.find(corpus="proj-a", token_budget=250, offset=off)
        after = eng.find(corpus="proj-a", token_budget=250, offset=off,
                         limit=None, order_by=None)
        assert before == after, f"diverged at offset={off}"


def test_backward_compat_byte_identical_traverse():
    nodes = {"ROOT": _node("ROOT", tokens=100)}
    edges = []
    for i in range(1, 8):
        nid = f"C{i}"
        nodes[nid] = _node(nid, tokens=100)
        edges.append(_edge("ROOT", nid))
    eng = _engine(nodes, edges)
    for off in (0, 2, 8):
        before = eng.traverse(corpus="proj-a", start_node="ROOT", token_budget=250,
                              offset=off)
        after = eng.traverse(corpus="proj-a", start_node="ROOT", token_budget=250,
                             offset=off, limit=None, order_by=None)
        assert before == after, f"diverged at offset={off}"


# =====================================================================
# [SPEC-NULL-ORDERING] determinism
# =====================================================================
def test_null_ordering_is_deterministic_run_to_run():
    """Two runs against an unchanged corpus return byte-identical order — never
    nondeterministic — including the unorderable tiebreak."""
    nodes = {}
    for i in range(20):
        nodes[f"D{i:02d}"] = _node(f"D{i:02d}", decided="2026-06-15")  # all TIES
    for nid in ("ZNODATE", "ANODATE"):
        nodes[nid] = _node(nid)
    eng = _engine(nodes)

    r1 = eng.find(corpus="proj-a", order_by="recency")
    r2 = eng.find(corpus="proj-a", order_by="recency")
    assert [n["id"] for n in r1["nodes"]] == [n["id"] for n in r2["nodes"]]
    # genuine ties among comparable values ALSO tiebreak by id ascending
    dated_ids = [n["id"] for n in r1["nodes"] if n["id"].startswith("D")]
    assert dated_ids == sorted(dated_ids)


# =====================================================================
# Boundary / error cases named in the brief
# =====================================================================
def test_invalid_order_direction_is_refused_loudly():
    nodes = {f"N{i}": _node(f"N{i}") for i in range(5)}
    eng = _engine(nodes)
    r = eng.find(corpus="proj-a", order_by={"field": "decided-in-session",
                                            "direction": "sideways"})
    assert r["success"] is False
    assert r["reason"] == "invalid_order_direction"


def test_typo_field_name_is_not_an_error_everything_unorderable():
    nodes = {f"N{i}": _node(f"N{i}", decided="2026-01-01") for i in range(10)}
    eng = _engine(nodes)
    r = eng.find(corpus="proj-a", order_by={"field": "deicded-in-sesion"})  # typo
    assert r["success"] is True
    assert r["order_applied"]["unorderable_count"] == r["total_matched"] == 10


def test_dominant_type_excludes_a_stray_string_in_a_numeric_field():
    nodes = {
        "N1": _node("N1", props={"score": 5}),
        "N2": _node("N2", props={"score": 3}),
        "N3": _node("N3", props={"score": 9}),
        "N4": _node("N4", props={"score": "not-a-number"}),  # stray type
    }
    eng = _engine(nodes)
    r = eng.find(corpus="proj-a", order_by={"field": "score", "direction": "asc"})
    assert r["success"] is True
    ids = [n["id"] for n in r["nodes"]]
    assert ids == ["N2", "N1", "N3", "N4"]  # numeric ascending, stray string LAST
    assert r["order_applied"]["unorderable_count"] == 1


def test_limit_zero_is_legal_empty_result_true_total_matched():
    nodes = {f"N{i}": _node(f"N{i}") for i in range(12)}
    eng = _engine(nodes)
    r = eng.find(corpus="proj-a", limit=0)
    assert r["success"] is True
    assert r["nodes"] == []
    assert r["count"] == 0
    assert r["total_matched"] == 12
    assert r["limit_applied"] == 0
    assert r["windowed_total"] == 0


def test_negative_limit_is_refused():
    nodes = {"N1": _node("N1")}
    eng = _engine(nodes)
    r = eng.find(corpus="proj-a", limit=-1)
    assert r["success"] is False
    assert r["reason"] == "invalid_limit"


def test_p3_bool_limit_is_refused_not_silently_coerced():
    """`bool` is a Python `int` subclass (`int(True) == 1`) — a `limit=True` must be
    refused, never silently accepted as `limit=1`."""
    nodes = {"N1": _node("N1")}
    eng = _engine(nodes)
    r = eng.find(corpus="proj-a", limit=True)
    assert r["success"] is False
    assert r["reason"] == "invalid_limit"


def test_p3_non_whole_float_limit_is_refused_not_silently_truncated():
    """`limit=3.7` must be refused, never silently truncated to `3` with no signal a
    fraction was dropped ([PARAM-LIMIT]'s "never silently coerce" ethos)."""
    nodes = {f"N{i}": _node(f"N{i}") for i in range(5)}
    eng = _engine(nodes)
    r = eng.find(corpus="proj-a", limit=3.7)
    assert r["success"] is False
    assert r["reason"] == "invalid_limit"


def test_p3_whole_float_limit_is_still_accepted():
    """A whole-number float (`30.0`) is unambiguous and still accepted — only a
    FRACTIONAL float is refused."""
    nodes = {f"N{i}": _node(f"N{i}") for i in range(5)}
    eng = _engine(nodes)
    r = eng.find(corpus="proj-a", limit=3.0)
    assert r["success"] is True
    assert r["limit_applied"] == 3


# =====================================================================
# `traverse` gets the same two parameters ([SPEC-COVERAGE-SCOPE])
# =====================================================================
def test_traverse_limit_and_recency_order_by():
    nodes = {"ROOT": _node("ROOT")}
    edges = []
    for i in range(1, 11):
        nid = f"C{i}"
        nodes[nid] = _node(nid, decided=f"2026-01-{i:02d}")
        edges.append(_edge("ROOT", nid))
    eng = _engine(nodes, edges)

    r = eng.traverse(corpus="proj-a", start_node="ROOT", max_depth=1,
                     order_by="recency", limit=3)
    assert r["success"] is True
    ids = {n["id"] for n in r["nodes"]}
    # ROOT has no `decided-in-session` -> unorderable -> sorts last -> excluded by
    # the top-3 window; the 3 newest children (C8, C9, C10) win.
    assert ids == {"C8", "C9", "C10"}
    assert r["limit_applied"] == 3
    assert r["total"] == 11


# =====================================================================
# ⛔ P0 (gap-analyzer, post-ship): `complete`/`bound_reason` must be OFFSET-AWARE
# (derived from `cursor`, the window-exhaustion signal), never from the GLOBAL
# `truncated`/`count-vs-windowed_total` reading — those two are correct only at
# offset=0 and go wrong on a paged call's exhausted last page.
# =====================================================================
def test_p0_successful_full_with_limit_reads_complete_true():
    """(a) `find(order_by="recency", limit=10, tier="full")` that FITS its budget is
    a successful, non-fractional response — `complete` MUST be `true` and
    `bound_reason` MUST be `"none"`, even though `limit` narrowed the response below
    `total_matched` (so `truncated` legitimately stays `true` at the same time)."""
    nodes = {f"D{i:03d}": _node(f"D{i:03d}", tokens=200,
                                decided=f"2026-{(i % 12) + 1:02d}-01")
             for i in range(50)}
    eng = _engine(nodes)

    r = eng.find(corpus="proj-a", order_by="recency", limit=10, tier="full",
                token_budget=5000)  # 10 nodes * 200 tokens = 2000, well under budget
    assert r["success"] is True
    assert r["count"] == r["windowed_total"] == 10
    assert r["cursor"] is None
    assert r["complete"] is True          # <- was False before the P0 fix
    assert r["bound_reason"] == "none"    # <- was "token_budget" before the P0 fix
    assert r["truncated"] is True         # still legitimately true (10 of 50)


def test_p0_last_page_of_paged_limited_window_reads_complete_true():
    """(b) the LAST page of a `limit`-bounded, budget-paged `find` call: the window
    is exhausted (`next_offset is None`) so `complete` and `bound_reason` MUST say
    so, even though the corpus (and `total_matched`) is far larger than the window."""
    nodes = {f"N{i:03d}": _node(f"N{i:03d}", tokens=10,
                                decided=f"2026-{(i % 12) + 1:02d}-01")
             for i in range(50)}
    eng = _engine(nodes)

    r0 = eng.find(corpus="proj-a", order_by="recency", limit=30, tier="page",
                  token_budget=100, offset=0)
    assert r0["next_offset"] == 10
    assert r0["complete"] is False and r0["bound_reason"] == "token_budget"

    r_last = eng.find(corpus="proj-a", order_by="recency", limit=30, tier="page",
                      token_budget=100, offset=20)
    assert r_last["count"] == 10
    assert r_last["next_offset"] is None      # window exhausted
    assert r_last["complete"] is True         # <- was False before the P0 fix
    assert r_last["bound_reason"] == "none"   # <- was "token_budget" before the fix
    assert r_last["total_matched"] == 50      # the corpus is still far larger


def test_p0_last_page_of_paged_limited_traverse_reads_complete_true():
    """The traverse-side twin of the `find` case above ([SPEC-COVERAGE-SCOPE]:
    `traverse` gets the identical `limit`/`order_by` treatment)."""
    nodes = {"ROOT": _node("ROOT")}
    edges = []
    for i in range(1, 21):
        nid = f"C{i:02d}"
        nodes[nid] = _node(nid, tokens=10, decided=f"2026-01-{i:02d}")
        edges.append(_edge("ROOT", nid))
    eng = _engine(nodes, edges)

    r_last = eng.traverse(corpus="proj-a", start_node="ROOT", max_depth=1,
                          order_by="recency", limit=10, tier="page",
                          token_budget=50, offset=5)
    assert r_last["next_offset"] is None
    assert r_last["complete"] is True
    assert r_last["bound_reason"] == "none"


# =====================================================================
# P2: unorderable_count under a `limit` — windowed scoping, not raw scoping
# (deviation #1: shipped verified-faithful, previously untested)
# =====================================================================
def test_p2_unorderable_count_scoped_to_window_not_raw_match_set():
    """5 comparable (dated) + 5 unorderable (no date) = 10 matched. `limit=7` takes
    all 5 comparable plus 2 of the 5 unorderable ones (id-ascending tiebreak):
    `unorderable_count` must report `2` (the window's own count), NOT `5` (the raw
    match set's count) — the boundary that distinguishes windowed vs raw scoping."""
    nodes = {}
    for i in range(5):
        nodes[f"D{i}"] = _node(f"D{i}", decided=f"2026-01-{i + 1:02d}")
    for nid in ("U5", "U4", "U3", "U2", "U1"):  # 5 unorderable, deliberately out of
        nodes[nid] = _node(nid)                 # id order to prove the tiebreak sorts them
    eng = _engine(nodes)

    r = eng.find(corpus="proj-a", order_by="recency", limit=7)
    assert r["success"] is True
    assert r["count"] == 7
    assert r["total_matched"] == 10
    ids = [n["id"] for n in r["nodes"]]
    assert ids[:5] == ["D4", "D3", "D2", "D1", "D0"]  # 5 comparable, newest first
    assert ids[5:] == ["U1", "U2"]                    # 2 of 5 unorderable, id ascending
    assert r["order_applied"]["unorderable_count"] == 2   # NOT 5
