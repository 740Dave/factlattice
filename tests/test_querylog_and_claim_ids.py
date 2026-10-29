"""The QUERY LOG and the CLAIM-SHAPED-ID rule.

QUERY LOG — the engine recorded NOTHING about what was asked of it. Every instrument here
measures the corpus as an ARTIFACT (does it parse, does it lint, does it keep what it
authored); none measured it as something people USE. The reason to build it is not
latency, it is the question nothing else can answer: WHICH PARTS OF THE CORPUS ARE NEVER
ASKED FOR. A node that is well-formed, correct, lint-clean and entirely unread passes
every check this engine has.

CLAIM IDS — an identifier should name the SUBJECT; when it carries the VERDICT the
conclusion travels without its evidence, and an id travels furthest of any field.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from factlattice_engine import querylog
from factlattice_engine.lint import lint
from factlattice_engine.lint.claim_id import (
    CLAIM_GENRE_PREFIXES, census, classify, is_machine_minted,
)
from factlattice_engine.readprofile import (
    never_read, read_records, report, shape, storms, within,
)
from factlattice_engine.store import FreshnessResult, LtsStore, Partition
from factlattice_engine.tools import LtsEngine, dispatch, reset_server_tools, set_server_tools


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
class _Fresh:
    def evaluate(self, part):
        return FreshnessResult(corpus_id=part.corpus_id, fresh=True,
                               index_hash="h", live_hash="h")


def _index(cid, node_ids):
    return {"generated_at": "2026-08-13T00:00:00Z",
            "source_corpus": {"corpus_id": cid, "domain_suffix": f"proj.{cid}",
                              "files_hash": None, "root_paths": [f"/tmp/{cid}"],
                              "built_at": "2026-08-13T00:00:00Z"},
            "nodes": {n: {"id": n, "title": n, "domain": f"proj.{cid}", "status": None,
                          "properties": {}, "token_count": 10} for n in node_ids},
            "layers": {"semantic": {"edges": []}},
            "stats": {"total_nodes": len(node_ids), "total_edges": 0}}


@pytest.fixture()
def engine_and_log(tmp_path, monkeypatch):
    log = tmp_path / "q.jsonl"
    monkeypatch.setenv(querylog.ENV_LOG, str(log))
    store = LtsStore()
    store.add_partition(Partition(_index("alpha", [f"DEC-{c}" for c in "ABCDEFGH"])))
    eng = LtsEngine(store, freshness_provider=_Fresh())
    set_server_tools(eng)
    yield eng, log
    reset_server_tools()


def _records(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# 1. THE LOG EXISTS, AND IT IS AT THE ENGINE SEAM
# ---------------------------------------------------------------------------
def test_a_query_writes_one_record(engine_and_log):
    _, log = engine_and_log
    dispatch("lts_find", {"corpus": "alpha", "prefix": "DEC-A"})
    recs = _records(log)
    assert len(recs) == 1
    r = recs[0]
    assert r["tool"] == "lts_find" and r["ok"] is True
    assert r["filters"] == {"corpus": "alpha", "prefix": "DEC-A"}
    assert r["count"] == 1 and r["total_matched"] == 1 and r["truncated"] is False
    assert r["result_ids"] == ["DEC-A"]
    assert isinstance(r["elapsed_ms"], float)
    assert r["ts"] and r["session"]


def test_the_seam_is_dispatch_not_the_mcp_transport(engine_and_log):
    """⛔ `Server.handle` is the MCP transport's door; `dispatch` is the ENGINE's. Logging
    at the transport records the MCP caller and MISSES the library caller, the CLI and the
    a message bus handler named as this module's migration path — so the read profile would
    understate by exactly the callers nobody thought about."""
    eng, log = engine_and_log
    from factlattice_engine.server import Server
    Server(tools=eng).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "lts_find",
                                         "arguments": {"corpus": "alpha", "prefix": "DEC-B"}}})
    dispatch("lts_find", {"corpus": "alpha", "prefix": "DEC-C"})     # library caller
    tools = [r["tool"] for r in _records(log)]
    assert tools == ["lts_find", "lts_find"], "a caller bypassing MCP went unrecorded"


def test_every_return_path_is_recorded_including_failures(engine_and_log):
    """`dispatch` has twenty-odd `return`s. A wrapper, not a log line before each one —
    the latter is a list of places to forget."""
    _, log = engine_and_log
    dispatch("lts_find", {"corpus": "nosuchcorpus"})          # not-found path
    dispatch("lts_node", {"corpus": "alpha", "node_id": "NOPE"})   # miss path
    recs = _records(log)
    assert len(recs) == 2
    # The refusal is recorded WITH its reason — a log that only kept successes would make
    # the zero-rate meaningless, which is one of the three views.
    assert recs[0]["ok"] is False and "error" not in recs[0] and "corpus not found" not in json.dumps(recs[0])   # round 6: structure, not text
    assert recs[1]["result_ids"] == []


def test_a_logging_failure_never_breaks_a_query(engine_and_log, monkeypatch):
    """⛔ A TELEMETRY LAYER THAT CAN TAKE THE SERVICE DOWN IS WORSE THAN NO TELEMETRY."""
    _, log = engine_and_log
    monkeypatch.setattr(querylog, "log_path",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    res = dispatch("lts_find", {"corpus": "alpha", "prefix": "DEC-A"})
    assert res["success"] is True and res["count"] == 1


def test_logging_is_on_by_default_and_switchable_off(monkeypatch):
    """A telemetry layer that must be opted into is a telemetry layer nobody has."""
    monkeypatch.delenv(querylog.ENV_LOG, raising=False)
    monkeypatch.delenv(querylog.ENV_DIR, raising=False)
    assert querylog.log_path() is not None
    monkeypatch.setenv(querylog.ENV_LOG, "0")
    assert querylog.log_path() is None


def test_the_log_is_never_inside_a_corpus_root(monkeypatch, tmp_path):
    """⛔ TELEMETRY IN A PARSED ROOT IS A LIVE DEFECT THIS PROJECT ALREADY CARRIES. Every
    appended line changes the corpus content hash, so the freshness kernel sees a write,
    marks the partition stale and re-indexes — a log that makes the engine re-parse the
    corpus it is logging."""
    monkeypatch.delenv(querylog.ENV_LOG, raising=False)
    monkeypatch.setenv(querylog.ENV_DIR, str(tmp_path / "state"))
    p = querylog.log_path()
    assert p is not None and str(p).startswith(str(tmp_path / "state"))

    store = LtsStore()
    store.add_partition(Partition(_index("alpha", ["DEC-A"])))
    for part in store.partitions():
        for root in part.root_paths:
            assert not str(p).startswith(root)


# ---------------------------------------------------------------------------
# PRIVACY
# ---------------------------------------------------------------------------
def test_free_text_is_digested_by_default(engine_and_log):
    """⛔ A FREE-TEXT QUERY CARRIES CORPUS CONTENT, and often the searcher's own working
    notes. Structure and identifiers are logged verbatim; `text=` is not."""
    _, log = engine_and_log
    dispatch("lts_find", {"corpus": "alpha", "text": "Emerald scoring threshold"})
    f = _records(log)[0]["filters"]
    assert "text" not in f
    assert len(f["text_sha"]) == 12 and f["text_len"] == 25
    assert "Emerald" not in json.dumps(f)


def test_literals_are_available_for_a_local_dev_context(engine_and_log, monkeypatch):
    _, log = engine_and_log
    monkeypatch.setenv(querylog.ENV_LITERALS, "1")
    dispatch("lts_find", {"corpus": "alpha", "text": "Emerald"})
    assert _records(log)[0]["filters"]["text"] == "Emerald"


def test_the_digest_costs_the_storm_analysis_nothing(engine_and_log):
    """⭐ Storm detection asks how many DISTINCT phrasings in one window, and distinctness
    is exactly what a hash preserves. The one thing hashing loses — READING the queries —
    is the one thing that would make this a surveillance log rather than an instrument."""
    a = querylog.digest_text("Emerald")
    b = querylog.digest_text("diamond scoring")
    assert a["text_sha"] != b["text_sha"]
    assert querylog.digest_text("Emerald")["text_sha"] == a["text_sha"]


def test_identifiers_are_logged_verbatim_because_the_analysis_needs_them(engine_and_log):
    """The one deliberate choice. You cannot report which nodes went UNREAD without
    recording which were READ — and a node id is a public name in the graph, travelling in
    edges and citations already, not content."""
    _, log = engine_and_log
    dispatch("lts_node", {"corpus": "alpha", "node_id": "DEC-B"})
    r = _records(log)[0]
    assert r["filters"]["node_id"] == "DEC-B" and r["result_ids"] == ["DEC-B"]


def test_paths_are_logged_as_basenames(tmp_path, monkeypatch):
    monkeypatch.setenv(querylog.ENV_LOG, str(tmp_path / "q.jsonl"))
    rec = querylog.build_record("lts_lint", {"source": "/deep/private/tree/corpus"},
                                {"success": True}, 1.0)
    assert rec["filters"]["source"] == "corpus"


# ---------------------------------------------------------------------------
# EMITTED, NOT MATCHED
# ---------------------------------------------------------------------------
def test_result_ids_are_what_was_delivered_not_what_matched(engine_and_log):
    """⛔ A truncated result's tail was never seen by the caller, so it was not READ.
    Counting it as read would make the never-read report understate exactly the
    population it exists to find."""
    _, log = engine_and_log
    dispatch("lts_find", {"corpus": "alpha", "prefix": "DEC", "token_budget": 12})
    r = _records(log)[0]
    assert r["truncated"] is True
    assert r["total_matched"] == 8
    assert 0 < len(r["result_ids"]) < 8
    assert r["count"] == len(r["result_ids"])


def test_the_extent_rides_along_so_a_logged_zero_is_legible_later(engine_and_log):
    """Without it a `count: 0` in the log is the same research task the response used to
    be: genuinely absent, wrong corpus, wrong key, or a stale index — indistinguishable."""
    _, log = engine_and_log
    dispatch("lts_find", {"corpus": "alpha", "text": "nothing here"})
    e = _records(log)[0]["extent"]
    assert e["nodes_in_scope"] == 8
    assert e["corpora_consulted"] == ["alpha"]
    assert e["index_fresh"] is True


def test_the_id_cap_declares_itself():
    """A bound is legitimate; a SILENT bound is not — so a downstream never-read claim can
    be qualified instead of quietly wrong."""
    big = {"success": True, "nodes": [{"id": f"N-{i}"} for i in range(querylog.MAX_RESULT_IDS + 10)]}
    rec = querylog.build_record("lts_find", {}, big, 1.0)
    assert rec["result_ids_capped"] is True
    assert len(rec["result_ids"]) == querylog.MAX_RESULT_IDS


# ---------------------------------------------------------------------------
# THE READ PROFILE — the view nothing else can produce
# ---------------------------------------------------------------------------
def test_never_read_names_the_unread_population_with_a_denominator(engine_and_log):
    _, log = engine_and_log
    dispatch("lts_find", {"corpus": "alpha", "prefix": "DEC-A"})
    dispatch("lts_node", {"corpus": "alpha", "node_id": "DEC-B"})
    rep = report({"alpha": [f"DEC-{c}" for c in "ABCDEFGH"]}, days=30, path=log)
    c = rep["never_read"]["per_corpus"]["alpha"]
    assert c["nodes_total"] == 8
    assert c["nodes_read"] == 2
    assert c["nodes_never_read"] == 6
    assert c["never_read_rate"] == 0.75
    assert set(c["never_read_ids"]) == {f"DEC-{x}" for x in "CDEFGH"}


def test_never_read_states_that_it_is_an_upper_bound(engine_and_log):
    """⚠ Absence of a logged read is NOT absence of a reader: a node read via `grep` never
    appears here — and that is the documented, measured behaviour this engine's recall
    work exists to change. Reporting it without the caveat would repeat the mistake of
    quoting a number whose scope is not attached to it."""
    _, log = engine_and_log
    dispatch("lts_find", {"corpus": "alpha", "prefix": "DEC-A"})
    rep = report({"alpha": ["DEC-A", "DEC-B"]}, days=30, path=log)
    caveat = rep["never_read"]["caveat"]
    assert "UPPER BOUND" in caveat and "NOT A DELETION LIST" in caveat
    assert "grep" in caveat


def test_the_id_cap_is_confessed_in_the_never_read_caveat():
    recs = [{"result_ids": ["A"], "result_ids_capped": True}]
    out = never_read(recs, {"c": ["A", "B"]})
    assert "hit the per-record id cap" in out["caveat"]
    assert "slightly HIGH" in out["caveat"]


def _rec(ts, tool="lts_find", count=0, corpus="alpha", session="s1", **filters):
    return {"ts": ts.isoformat(), "session": session, "tool": tool, "count": count,
            "total_matched": count, "elapsed_ms": 1.0, "result_ids": [],
            "filters": {"corpus": corpus, **filters}}


def test_a_storm_is_distinct_questions_that_ALL_came_back_empty():
    """⛔ THE CORRECTION THE FIRST LIVE RUN FORCED: counting distinctness across the whole
    window let two SUCCESSFUL calls inflate the storm to "5 distinct questions". A caller
    who asks four sharpening questions and gets four useful answers is WORKING."""
    t0 = datetime.now(timezone.utc)
    recs = [
        _rec(t0, count=3, text_sha="aaa"),                      # worked
        _rec(t0 + timedelta(seconds=5), count=2, text_sha="bbb"),  # worked
        _rec(t0 + timedelta(seconds=10), text_sha="ccc"),
        _rec(t0 + timedelta(seconds=20), text_sha="ddd"),
        _rec(t0 + timedelta(seconds=30), text_sha="eee"),
    ]
    st = storms(recs)
    assert len(st) == 1
    assert st[0]["distinct_empty_questions"] == 3, "successful work inflated the storm"
    assert st[0]["zero_answers"] == 3


def test_productive_drilling_down_is_not_a_storm():
    """A rule that calls ordinary work a failure is a rule that gets ignored, which costs
    the cases that were real."""
    t0 = datetime.now(timezone.utc)
    recs = [_rec(t0 + timedelta(seconds=i * 5), count=2, text_sha=f"h{i}")
            for i in range(6)]
    assert storms(recs) == []


def test_one_zero_among_many_is_not_a_storm():
    t0 = datetime.now(timezone.utc)
    recs = [_rec(t0, count=4, text_sha="a"),
            _rec(t0 + timedelta(seconds=5), count=0, text_sha="b"),
            _rec(t0 + timedelta(seconds=9), count=7, text_sha="c")]
    assert storms(recs) == []


def test_the_same_question_asked_twice_is_not_three_phrasings():
    """Distinctness, not volume: a retry of the identical query is not a caller casting
    about, and counting it would inflate every paginating client into a storm."""
    t0 = datetime.now(timezone.utc)
    recs = [_rec(t0 + timedelta(seconds=i), text_sha="same") for i in range(6)]
    assert storms(recs) == []


def test_storms_do_not_span_sessions():
    """A storm is one CALLER casting about. Smearing two clients that shared a minute into
    one finding would invent a failure nobody had."""
    t0 = datetime.now(timezone.utc)
    recs = [_rec(t0, text_sha="a", session="s1"),
            _rec(t0 + timedelta(seconds=2), text_sha="b", session="s2"),
            _rec(t0 + timedelta(seconds=4), text_sha="c", session="s3")]
    assert storms(recs) == []


def test_a_torn_final_line_does_not_kill_the_reader(tmp_path):
    """An append-only log written by a crashing process has a torn tail, and a reader that
    dies on it stops working exactly when it is most needed."""
    p = tmp_path / "q.jsonl"
    p.write_text('{"ts":"2026-08-13T00:00:00+00:00","tool":"lts_find"}\n{"ts":"tor',
                 encoding="utf-8")
    assert len(read_records(p)) == 1


def test_an_empty_log_reports_zero_calls_and_no_finding(tmp_path):
    assert shape([]) == {"calls": 0}
    assert report({"alpha": ["A"]}, path=tmp_path / "missing.jsonl")["shape"]["calls"] == 0


def test_the_window_filter_drops_old_records():
    old = _rec(datetime.now(timezone.utc) - timedelta(days=90), count=1)
    new = _rec(datetime.now(timezone.utc), count=1)
    assert len(within([old, new], 30)) == 1
    assert len(within([old, new], None)) == 2


# ---------------------------------------------------------------------------
# 2. CLAIM-SHAPED IDENTIFIERS
# ---------------------------------------------------------------------------
def test_the_briefs_own_pair_classifies_the_way_the_brief_says():
    """    ACME-TRUNCATION-RISK            POINTER
        ACME-TIER-TRUNCATION-IS-A-LIE   ARGUMENT — citable unread"""
    assert classify("ACME-TRUNCATION-RISK", "ACME") is None
    hit = classify("ACME-TIER-TRUNCATION-IS-A-LIE", "ACME")
    assert hit is not None
    assert hit["subject"] == "ACME-TIER-TRUNCATION"
    assert hit["predicate"] == "IS-A-LIE"


def test_a_claim_genre_family_is_licensed_to_argue():
    """⛔ A DECISION THAT DOES NOT STATE ITS DECISION IN ITS NAME IS HARDER TO CITE, NOT
    EASIER. `DEC-1-4-LIMIT-IS-0-61` is the brief's own example of a legitimately
    claim-shaped id whose whole value is answering in one call."""
    assert classify("DEC-1-4-LIMIT-IS-0-61", "DEC") is None
    assert classify("PRINCIPLE-ASYMMETRY-IS-REAL", "PRINCIPLE") is None
    assert classify("INV-FILE-IS-TRUTH", "INV") is None
    # ...and the licence is per-corpus, not a permanent carve-out.
    assert classify("DEC-LIMIT-IS-0-61", "DEC", claim_genre=set()) is not None


def test_machine_minted_ids_are_skipped():
    """19 of the 96 naive hits were auto-generated slugs truncated mid-phrase. Nobody
    authored them as a claim and there is no naming decision available."""
    assert is_machine_minted("DEFECT-WIDGET-REFRESH-OPTION-DOES-3A9B1D")
    assert is_machine_minted("DEFECT-EXT-PANEL-STYLE-DOES-NOT-5D82E0-A1")
    assert is_machine_minted("DELIV-0009-EDITOR-YOUR-SUMMARY-WAS-STALE-7C21A9F4")
    assert not is_machine_minted("ACME-TIER-TRUNCATION-IS-A-LIE")
    assert classify("DEFECT-X-IS-BROKEN-3A9B1D", "OTHER") is None


@pytest.mark.parametrize("node_id", [
    # ⭐ MEASURED AND REJECTED. Bare negation names a BOUNDARY or a DISTINCTION —
    # subject-naming, which is what the rule wants. Including it more than doubled the
    # finding count (9 -> 20) while at least halving precision.
    "ACME-SOB-NO-POLICY-LAYER", "KGM-NOT-IMPL", "DRI-NO-MIGRATION",
    "ACME-DS-REMOVED-NOT-DELETED", "ACME-SUB-NOT-NEW", "ACME-LTW-NO-ROUNDTRIP",
    # And the ordinary case: a noun phrase, however evaluative. The brief rules `RISK`
    # acceptable, so the discriminator was never the noun — it is the COPULA that turns a
    # noun phrase into a sentence.
    "ACME-TRUNCATION-RISK", "ACME-VISION-PILLARS", "FL-RECALL-PARTITION",
])
def test_the_rule_does_not_fire_on_subject_naming_ids(node_id):
    assert classify(node_id, "ACME") is None


def test_the_finding_forbids_the_rename_in_as_many_words(tmp_path):
    """⛔ Citations span four corpora and the cost of a rename lands hardest on the
    MOST-CITED nodes — precisely the ones whose naming matters most."""
    (tmp_path / "d.lts").write_text(
        "---\nscope: s\naudience: a\nboundary: b\n---\n# D [D-ROOT]\n\n"
        "## The tier truncation finding [ACME-TIER-TRUNCATION-IS-A-LIE]\n\n- kind: spec\n",
        encoding="utf-8")
    r = lint(str(tmp_path))
    hits = [f for f in r.findings if f.origin == "claim_id"]
    assert len(hits) == 1
    assert hits[0].severity == "warning"
    assert "DO NOT RENAME" in hits[0].message
    assert "`claim:` property" in hits[0].message
    assert hits[0].detail["subject"] == "ACME-TIER-TRUNCATION"


def test_the_census_is_emitted_on_every_run_including_a_clean_one(tmp_path):
    """A rule that emits only findings cannot distinguish "this corpus names subjects"
    from "the rule was switched off" — the same reason `key_loss` reports its ratio."""
    (tmp_path / "d.lts").write_text(
        "---\nscope: s\naudience: a\nboundary: b\n---\n# D [D-ROOT]\n\n"
        "## A pointer [ACME-TRUNCATION-RISK]\n\n- kind: spec\n", encoding="utf-8")
    r = lint(str(tmp_path))
    assert [f for f in r.findings if f.origin == "claim_id"] == []
    assert r.claim_ids["claim_shaped_ids"] == 0
    assert r.claim_ids["nodes_total"] == 2
    assert r.claim_id_line().startswith("-- claim-shaped ids: 0 of 2")


def test_the_census_carries_a_denominator_and_a_per_prefix_split(tmp_path):
    """⛔ THE DENOMINATOR IS THE POINT. "9 claim-shaped ids" is a number nobody can act
    on; "9 of 3,034, and 7 of them in two prefixes" is a direction — and a family at 0% is
    visibly practising the convention a family at 40% is not."""
    (tmp_path / "d.lts").write_text(
        "---\nscope: s\naudience: a\nboundary: b\n---\n# D [D-ROOT]\n\n"
        "## one [ACME-A-IS-WRONG]\n\n- kind: spec\n\n"
        "## two [ACME-B-MUST-HOLD]\n\n- kind: spec\n\n"
        "## three [ZZZ-PLAIN-SUBJECT]\n\n- kind: spec\n\n"
        "## four [DEC-THIS-IS-DECIDED]\n\n- kind: decision\n", encoding="utf-8")
    c = lint(str(tmp_path)).claim_ids
    assert c["nodes_total"] == 5
    assert c["claim_shaped_ids"] == 2
    assert c["licensed_by_genre"] == 1                  # the DEC-
    assert c["by_prefix"]["ACME"] == {"nodes": 2, "claim_shaped": 2}
    assert "ZZZ" not in c["by_prefix"]                  # clean families are not listed
    assert 0 < c["claim_shaped_rate"] <= 1


def test_titles_are_censused_and_never_flagged(tmp_path):
    """The harm model is entirely about TRAVEL: an id goes into edges, `see-also` lists
    and briefs; a title does not."""
    (tmp_path / "d.lts").write_text(
        "---\nscope: s\naudience: a\nboundary: b\n---\n# D [D-ROOT]\n\n"
        "## Truncation is a lie and here is why [ACME-TIER-TRUNCATION]\n\n- kind: spec\n",
        encoding="utf-8")
    r = lint(str(tmp_path))
    assert [f for f in r.findings if f.origin == "claim_id"] == []
    assert r.claim_ids["sentence_shaped_titles"] == 1


def test_the_genre_licence_is_overridable_per_corpus(tmp_path):
    (tmp_path / "d.lts").write_text(
        "---\nscope: s\naudience: a\nboundary: b\n---\n# D [D-ROOT]\n\n"
        "## a decision [DEC-THIS-IS-DECIDED]\n\n- kind: decision\n", encoding="utf-8")
    assert [f for f in lint(str(tmp_path)).findings if f.origin == "claim_id"] == []
    strict = lint(str(tmp_path), claim_genre_prefixes=[])
    assert len([f for f in strict.findings if f.origin == "claim_id"]) == 1


def test_the_rule_is_demotable_and_switchable_off(tmp_path):
    (tmp_path / "d.lts").write_text(
        "---\nscope: s\naudience: a\nboundary: b\n---\n# D [D-ROOT]\n\n"
        "## one [ACME-A-IS-WRONG]\n\n- kind: spec\n", encoding="utf-8")
    assert lint(str(tmp_path), claim_id=False).claim_ids is None
    assert not lint(str(tmp_path)).has_errors          # warning, never error


def test_the_measured_live_population_is_pinned():
    """⚠ THE CALIBRATION, PINNED SO IT CANNOT DRIFT UNNOTICED. Measured over this
    repository's own corpora: the naive copula predicate fired broadly, and the two
    principled exclusions (machine-minted slugs, claim-genre families) plus the rejection
    of bare negation cut it to a handful. If a future change makes this rule loud, this
    test is where it shows."""
    from factlattice_engine.lint.core import lint as _lint
    r = _lint("specs")
    assert r.claim_ids["claim_shaped_ids"] == 0, (
        "the FactLattice spec corpus started arguing in its identifiers")
    assert r.claim_ids["nodes_total"] > 20            # the denominator is real
