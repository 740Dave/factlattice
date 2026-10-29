"""[QUERY-REPORT-MISS] `lts_report_miss`: an agent files a known-item miss (the query it asked, the
node it knows exists, why), and the engine captures the evidence NOW (ranks under exact and semantic)
in <state_dir>/miss-log.jsonl. Hermetic: the tiny model, a temp corpus, FACTLATTICE_STATE_DIR in tmp."""
from __future__ import annotations

import hashlib
import json
import os

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")

from factlattice_engine import build_stamp, config, querylog, server  # noqa: E402
from factlattice_engine.semantic import suggest as S  # noqa: E402
from factlattice_engine.tools import LTS_ALL_TOOL_NAMES, LtsEngine, dispatch  # noqa: E402
from tests.semantic_helpers import _tiny_model  # noqa: E402
from tests.test_semantic_find import _corpus  # noqa: E402

Q = "tile map version"
HIT, MISSING = "XL-LEDGER-PROTOBUF", "N-HELLO"


@pytest.fixture()
def w(tmp_path, monkeypatch):
    for v in ("LTS_SEMANTIC_ON_MISS", "LTS_SEMANTIC_HIGH_LABEL", "XDG_STATE_HOME"):
        monkeypatch.delenv(v, raising=False)
    state = tmp_path / "state-log"
    monkeypatch.setenv(querylog.ENV_DIR, str(state))
    monkeypatch.setenv(querylog.ENV_LOG, str(tmp_path / "q.jsonl"))
    _corpus(tmp_path / "corpus")
    pin = _tiny_model(str(tmp_path / "models"))
    reg = {"c": {"roots": [{"path": str(tmp_path / "corpus"), "area": "specs"}], "domain_suffix": "proj.c"}}
    e = LtsEngine(config.build_store(reg))
    e.semantic = S.SemanticService(state_root=str(tmp_path / "state"), model_root=str(tmp_path / "models"),
                                   pin=pin, build_mode="sync")
    return e, state / "miss-log.jsonl", tmp_path


def _recs(path):
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def _file(e, **kw):
    a = dict(corpus="c", query=Q, expected_ids=[HIT], why="I needed the exact spec, not memory")
    a.update(kw)
    return e.report_miss(**a)


def _tree_hash(root):
    h = hashlib.sha256()
    for dp, _d, fs in sorted(os.walk(root)):
        for f in sorted(fs):
            h.update(f.encode() + open(os.path.join(dp, f), "rb").read())
    return h.hexdigest()


# --- acceptance-1 -----------------------------------------------------------------------------
def test_acc1_a_valid_report_appends_one_record_with_the_evidence(w):
    e, log, _ = w
    r = _file(e, found_via="lts_node by id from memory", tool_used="lts_find_in_prose")
    assert r["success"] is True and r["unknown_ids"] == []
    recs = _recs(log)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["report_id"] == r["report_id"] and rec["ranks"] == r["ranks"]
    assert rec["query"] == Q and rec["why"] == "I needed the exact spec, not memory"      # literal
    assert rec["corpus"] == "c" and rec["expected_ids"] == [HIT] and rec["unknown_ids"] == []
    assert rec["tool_used"] == "lts_find_in_prose" and rec["found_via"] == "lts_node by id from memory"
    assert rec["session"] == querylog.SESSION_ID and rec["ts"]
    assert rec["engine_build_stamp"] == build_stamp.build_stamp()
    sem = e.semantic_find(corpus="c", text=Q, k=50)
    assert rec["index_version"] == sem["index_version"] and rec["index_version"]
    assert rec["ranks"][HIT]["exact"] == 1                                                  # 1-based position
    assert rec["ranks"][HIT]["semantic"] == [c["id"] for c in sem["candidates"]].index(HIT) + 1


def test_acc1_an_id_absent_from_a_result_has_a_null_rank(w):
    e, log, _ = w
    r = _file(e, expected_ids=[MISSING])                    # "hello world" is not an exact hit for Q
    assert r["ranks"][MISSING]["exact"] is None
    assert _recs(log)[0]["ranks"][MISSING]["exact"] is None


def test_acc1_the_exact_rank_is_the_relevance_position_of_the_digest(w):
    e, log, _ = w
    ex = e.find(corpus="c", text="map", rank="relevance", semantic="off", tier="digest")
    ids = [d["id"] for d in ex["digest"]]
    assert len(ids) >= 2
    r = _file(e, query="map", expected_ids=[ids[1]])
    assert r["ranks"][ids[1]]["exact"] == 2


def test_acc1_a_semantic_failure_records_null_and_a_status_and_never_fails_the_report(w):
    e, log, _ = w

    class Boom:
        def suggest(self, *a, **k):
            raise RuntimeError("model absent")
    e.semantic = Boom()
    r = _file(e)
    assert r["success"] is True and r["ranks"][HIT]["semantic"] is None and r["ranks"][HIT]["exact"] == 1
    rec = _recs(log)[0]
    assert rec["semantic_status"] == "unavailable" and rec["index_version"] is None
    assert rec["ranks"][HIT]["semantic"] is None


# --- acceptance-2 -----------------------------------------------------------------------------
def test_acc2_an_unknown_id_is_reported_not_dropped(w):
    e, log, _ = w
    r = _file(e, expected_ids=[HIT, "NOPE-1"])
    assert r["success"] and r["unknown_ids"] == ["NOPE-1"] and set(r["ranks"]) == {HIT}
    rec = _recs(log)[0]
    assert rec["unknown_ids"] == ["NOPE-1"] and rec["expected_ids"] == [HIT, "NOPE-1"]


def test_acc2_all_unknown_is_refused_with_a_reason_and_writes_nothing(w):
    e, log, _ = w
    r = _file(e, expected_ids=["NOPE-1", "NOPE-2"])
    assert r["success"] is False and "NOPE-1" in r["error"] and r["unknown_ids"] == ["NOPE-1", "NOPE-2"]
    assert not log.exists()


# --- acceptance-3 -----------------------------------------------------------------------------
@pytest.mark.parametrize("kw,needle", [
    (dict(query=None), "query"), (dict(query="  "), "query"),
    (dict(expected_ids=[]), "expected_ids"), (dict(expected_ids=None), "expected_ids"),
    (dict(expected_ids="XL-LEDGER-PROTOBUF"), "expected_ids"), (dict(expected_ids=[HIT, 5]), "expected_ids"),
    (dict(expected_ids=[HIT] + [f"X{i}" for i in range(10)]), "10"),
    (dict(why=None), "why"), (dict(why=""), "why"), (dict(why="x" * 2001), "2,000"),
    (dict(corpus=None), "corpus"), (dict(corpus="no-such-corpus"), "no-such-corpus"),
])
def test_acc3_malformed_reports_are_refused_loudly_and_write_nothing(w, kw, needle):
    e, log, _ = w
    r = _file(e, **kw)
    assert r["success"] is False and needle in r["error"], r
    assert not log.exists()


def test_acc3_exactly_ten_ids_and_a_2000_char_why_are_accepted(w):
    e, log, _ = w
    r = _file(e, expected_ids=[HIT] + [f"X{i}" for i in range(9)], why="y" * 2000)
    assert r["success"] and len(r["unknown_ids"]) == 9


def test_acc3_duplicate_ids_are_collapsed_in_order(w):
    e, log, _ = w
    r = _file(e, expected_ids=[HIT, HIT])
    assert r["success"] and _recs(log)[0]["expected_ids"] == [HIT]


# --- acceptance-4 -----------------------------------------------------------------------------
def test_acc4_the_log_is_in_the_state_dir_never_under_a_corpus_root(w):
    e, log, tmp = w
    _file(e)
    assert log.exists() and log.name == "miss-log.jsonl"
    root = str(tmp / "corpus")
    assert not os.path.realpath(str(log)).startswith(os.path.realpath(root) + os.sep)
    assert not [f for f in os.listdir(root) if "miss" in f]


def test_acc4_a_state_dir_inside_a_corpus_root_is_refused(w, monkeypatch):
    e, log, tmp = w
    inside = tmp / "corpus" / "state"
    monkeypatch.setenv(querylog.ENV_DIR, str(inside))
    r = _file(e)
    assert r["success"] is False and "corpus root" in r["error"]
    assert not inside.exists()


def test_acc4_an_unwritable_log_is_success_false_with_a_reason_never_an_exception(w, monkeypatch, tmp_path):
    e, log, tmp = w
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a dir")
    monkeypatch.setenv(querylog.ENV_DIR, str(blocker / "sub"))
    r = _file(e)
    assert r["success"] is False and r.get("reason") and r["report_id"]


# --- acceptance-5 -----------------------------------------------------------------------------
def test_acc5_corpus_files_are_untouched(w):
    e, log, tmp = w
    before = _tree_hash(tmp / "corpus")
    _file(e)
    _file(e, expected_ids=["NOPE"])
    assert _tree_hash(tmp / "corpus") == before


# --- acceptance-6 -----------------------------------------------------------------------------
def test_acc6_advertised_dispatched_and_in_the_surface_tuple(w):
    e, log, _ = w
    assert "lts_report_miss" in LTS_ALL_TOOL_NAMES
    spec = [t for t in server.TOOLS if t["name"] == "lts_report_miss"]
    assert len(spec) == 1
    sch = spec[0]["inputSchema"]
    assert set(sch["required"]) == {"corpus", "query", "expected_ids", "why"}
    assert set(sch["properties"]) >= {"corpus", "query", "expected_ids", "why", "tool_used", "found_via"}
    assert sch["properties"]["expected_ids"]["type"] == "array"
    r = dispatch("lts_report_miss", {"corpus": "c", "query": Q, "expected_ids": [HIT], "why": "w"}, tools=e)
    assert r["success"] and len(_recs(log)) == 1


def test_acc6_it_is_in_the_query_log_without_the_free_text(w, tmp_path):
    e, log, _ = w
    assert "lts_report_miss" in querylog._LOGGED_TOOLS
    dispatch("lts_report_miss", {"corpus": "c", "query": "SECRET-QUERY-WORDS", "expected_ids": [HIT],
                                 "why": "SECRET-WHY-WORDS"}, tools=e)
    qlog = (tmp_path / "q.jsonl").read_text()
    rec = json.loads(qlog.splitlines()[-1])
    assert rec["tool"] == "lts_report_miss" and rec["filters"]["corpus"] == "c"
    assert "SECRET-QUERY-WORDS" not in qlog and "SECRET-WHY-WORDS" not in qlog


def test_acc6_it_is_documented_in_the_tool_description_as_read_only_evidence():
    d = [t for t in server.TOOLS if t["name"] == "lts_report_miss"][0]["description"]
    assert "read-only" in d.lower() and "miss" in d.lower()


# --- review of 2fcc25a ------------------------------------------------------------------------
def _world(tmp_path, monkeypatch, private_root=None):
    for v in ("LTS_SEMANTIC_ON_MISS", "LTS_SEMANTIC_HIGH_LABEL", "XDG_STATE_HOME"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv(querylog.ENV_DIR, str(tmp_path / "state-log"))
    monkeypatch.setenv(querylog.ENV_LOG, str(tmp_path / "q.jsonl"))
    _corpus(tmp_path / "corpus")
    reg = {"c": {"roots": [{"path": str(tmp_path / "corpus"), "area": "specs"}], "domain_suffix": "proj.c"}}
    if private_root is not None:
        _corpus(private_root)
        reg["hush-private"] = {"roots": [{"path": str(private_root), "area": "specs"}],
                               "domain_suffix": "proj.p", "private": True}
    e = LtsEngine(config.build_store(reg))

    class NoModel:
        def suggest(self, *a, **k):
            raise RuntimeError("no model in this test")
    e.semantic = NoModel()
    return e, tmp_path / "state-log" / "miss-log.jsonl"


def test_p21_exact_status_is_in_the_response_and_the_record_when_ok(w):
    e, log, _ = w
    r = _file(e)
    assert r["exact_status"] == "ok" and _recs(log)[0]["exact_status"] == "ok"


def test_p21_a_failed_exact_search_is_visible_not_a_plain_not_found(w, monkeypatch):
    e, log, _ = w
    monkeypatch.setattr(e, "find", lambda **k: {"success": False, "error": "digest ceiling: 1000 nodes"})
    r = _file(e)
    assert r["success"] and r["ranks"][HIT]["exact"] is None
    assert r["exact_status"].startswith("error") and "digest ceiling" in r["exact_status"]
    assert _recs(log)[0]["exact_status"] == r["exact_status"]

    def boom(**k):
        raise RuntimeError("find fault")
    monkeypatch.setattr(e, "find", boom)
    st = _file(e)["exact_status"]
    assert st == "error: RuntimeError" and "find fault" not in st


def test_p21_a_broad_query_over_a_big_corpus_reports_the_real_ceiling_status(tmp_path, monkeypatch):
    e, log = _world(tmp_path, monkeypatch)
    big = tmp_path / "big"
    big.mkdir()
    (big / "00-b.lts").write_text("# Big [DOC-B]\n\n" + "".join(
        f"## Node {i} [BIG-{i}]\n\n- what: common shared words everywhere\n\n" for i in range(1100)), encoding="utf-8")
    e2 = LtsEngine(config.build_store({"b": {"roots": [{"path": str(big), "area": "specs"}], "domain_suffix": "p.b"}}))
    e2.semantic = e.semantic
    direct = e2.find(corpus="b", text="common", rank="relevance", semantic="off", tier="digest")
    r = e2.report_miss(corpus="b", query="common", expected_ids=["BIG-5"], why="broad")
    assert r["success"]
    if direct.get("success") is False:                         # the digest ceiling bites: it must be visible
        assert r["exact_status"].startswith("error")
    else:
        assert r["exact_status"] == "ok"


def test_p22_the_refusal_carries_no_path_and_no_corpus_name(tmp_path, monkeypatch):
    priv = tmp_path / "very-secret-root"
    e, log = _world(tmp_path, monkeypatch, private_root=priv)
    inside = priv / "inner-state"
    monkeypatch.setenv(querylog.ENV_DIR, str(inside))
    r = e.report_miss(corpus="c", query=Q, expected_ids=[HIT], why="w")
    assert r["success"] is False and "registered corpus root" in r["error"]
    blob = json.dumps(r)
    for leak in (str(inside), "very-secret-root", "hush-private", "inner-state", str(tmp_path)):
        assert leak not in blob, leak
    assert not inside.exists()


def test_p23_a_case_variant_of_a_root_cannot_bypass_the_guard(tmp_path, monkeypatch):
    e, log = _world(tmp_path, monkeypatch)
    variant = tmp_path / "CORPUS"
    if not variant.exists():
        pytest.skip("case-sensitive filesystem: a case variant is a different directory")
    monkeypatch.setenv(querylog.ENV_DIR, str(variant / "state"))
    r = e.report_miss(corpus="c", query=Q, expected_ids=[HIT], why="w")
    assert r["success"] is False and "registered corpus root" in r["error"]
    assert not (tmp_path / "corpus" / "state").exists()


def test_p23_a_symlink_into_a_root_cannot_bypass_the_guard(tmp_path, monkeypatch):
    e, log = _world(tmp_path, monkeypatch)
    link = tmp_path / "linkdir"
    os.symlink(tmp_path / "corpus", link)
    monkeypatch.setenv(querylog.ENV_DIR, str(link / "state"))
    r = e.report_miss(corpus="c", query=Q, expected_ids=[HIT], why="w")
    assert r["success"] is False and "registered corpus root" in r["error"]


@pytest.mark.parametrize("kw,needle", [
    (dict(query="q" * 2001), "query"), (dict(tool_used="t" * 501), "tool_used"),
    (dict(found_via="f" * 501), "found_via"), (dict(tool_used=5), "tool_used"),
    (dict(found_via=["x"]), "found_via"),
])
def test_p24_the_caps_are_enforced_and_write_nothing(w, kw, needle):
    e, log, _ = w
    r = _file(e, **kw)
    assert r["success"] is False and needle in r["error"] and not log.exists()


def test_p24_the_caps_accept_their_limits(w):
    e, log, _ = w
    assert _file(e, query="q" * 2000, tool_used="t" * 500, found_via="f" * 500)["success"]


def test_p24_the_miss_log_ignores_the_query_log_off_switch(w, monkeypatch):
    e, log, _ = w
    monkeypatch.setenv(querylog.ENV_LOG, "0")
    assert _file(e)["success"] and len(_recs(log)) == 1


def test_p24_a_named_private_corpus_is_accepted(tmp_path, monkeypatch):
    e, log = _world(tmp_path, monkeypatch, private_root=tmp_path / "priv-root")
    r = e.report_miss(corpus="hush-private", query=Q, expected_ids=[HIT], why="my own self")
    assert r["success"] and _recs(log)[0]["corpus"] == "hush-private"


def test_p3_a_non_hashable_corpus_is_a_refusal_not_a_typeerror(w):
    e, log, _ = w
    for bad in (["c"], {"c": 1}, 5):
        r = _file(e, corpus=bad)
        assert r["success"] is False and "corpus" in r["error"]
    assert not log.exists()


def test_p3_the_description_documents_the_caps_and_the_log_behaviour():
    d = [t for t in server.TOOLS if t["name"] == "lts_report_miss"][0]["description"]
    for needle in ("2,000", "500", "FACTLATTICE_QUERY_LOG", "miss log", "exact_status"):
        assert needle in d, needle


def test_p3_the_checklist_has_the_state_dir_and_evidence_status_rules():
    txt = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "SDLC.md")).read()
    sec = txt[txt.index("### 7.6"):txt.index("## 8. Stage 5")]
    for needle in ("root-guarded", "normalised", "never written inside a corpus root", "each search's status",
                   "fixed vocabulary", "empty or blank root", "fails closed"):
        assert needle in sec, needle


# --- land condition: fail closed + sanitised statuses ------------------------------------------
REFUSAL = "cannot verify the miss log is outside every corpus root: corpus roots unknown; nothing written"


@pytest.mark.parametrize("how", ["empty", "none-source", "raises", "no-root-paths"])
def test_fc_unknown_roots_fail_closed_and_write_nothing(w, monkeypatch, how):
    e, log, tmp = w
    from factlattice_engine import membership

    def boom(*a, **k):
        raise RuntimeError(f"registry exploded at {tmp}/corpus")
    if how == "empty":
        monkeypatch.setattr(membership, "registered_roots", lambda store=None: ({}, "store"))
    elif how == "none-source":
        monkeypatch.setattr(membership, "registered_roots", lambda store=None: ({}, "none"))
    elif how == "no-root-paths":
        monkeypatch.setattr(membership, "registered_roots", lambda store=None: ({"c": []}, "store"))
    else:
        monkeypatch.setattr(membership, "registered_roots", boom)
    r = _file(e)
    assert r["success"] is False and REFUSAL in r["error"]
    blob = json.dumps(r)
    assert str(tmp) not in blob and "corpus" not in {k for k in r if k == "corpus"}
    assert "state-log" not in blob and "miss-log" not in blob
    assert not log.exists() and not log.parent.exists()


def test_fc_known_roots_still_write(w):
    e, log, _ = w
    assert _file(e)["success"] and log.exists()


def _leaky(tmp, where):
    secret = f"{tmp}/corpus/very-private-subdir/file.lts"

    def raiser(*a, **k):
        raise PermissionError(f"[Errno 13] Permission denied: '{secret}'")
    return secret, raiser


def test_st_a_find_exception_with_a_path_is_sanitised_everywhere(w, monkeypatch):
    e, log, tmp = w
    secret, raiser = _leaky(tmp, "find")
    monkeypatch.setattr(e, "find", raiser)
    r = _file(e)
    assert r["success"] and r["exact_status"] == "error: PermissionError"
    assert secret not in json.dumps(r) and "very-private-subdir" not in json.dumps(r)
    assert secret not in log.read_text() and "very-private-subdir" not in log.read_text()
    assert _recs(log)[0]["exact_status"] == r["exact_status"]


def test_st_a_find_error_response_with_a_path_is_sanitised(w, monkeypatch):
    e, log, tmp = w
    secret = f"{tmp}/corpus/very-private-subdir"
    monkeypatch.setattr(e, "find", lambda **k: {"success": False, "error": f"cannot read {secret}"})
    r = _file(e)
    assert secret not in json.dumps(r) and r["exact_status"] == "error: FindFailed"
    assert "very-private-subdir" not in log.read_text()


def test_st_the_digest_ceiling_maps_to_the_fixed_phrase(w, monkeypatch):
    e, log, _ = w
    monkeypatch.setattr(e, "find", lambda **k: {"success": False, "error": "digest tier is limited to 1000 nodes; digest ceiling"})
    assert _file(e)["exact_status"] == "error: digest ceiling exceeded"


def test_st_a_semantic_exception_or_status_with_a_path_is_sanitised(w, monkeypatch):
    e, log, tmp = w
    secret, raiser = _leaky(tmp, "semantic")
    monkeypatch.setattr(e, "semantic_find", raiser)
    r = _file(e)
    assert r["success"] and r["semantic_status"] == "error: PermissionError"
    assert secret not in json.dumps(r) and secret not in log.read_text()
    monkeypatch.setattr(e, "semantic_find", lambda **k: {"success": True, "status": f"broken at {secret}"})
    r = _file(e)
    assert secret not in json.dumps(r) and r["semantic_status"] == "unavailable"
    assert secret not in log.read_text()


def test_st_the_normal_vocabulary_is_unchanged(w):
    e, log, _ = w
    r = _file(e)
    assert r["exact_status"] == "ok" and r["semantic_status"] in ("ok", "unavailable", "index_building")


def test_st_a_write_failure_reason_carries_no_path(w, monkeypatch, tmp_path):
    e, log, tmp = w
    blocker = tmp_path / "blocker-secret-name"
    blocker.write_text("a file, not a dir")
    monkeypatch.setenv(querylog.ENV_DIR, str(blocker / "sub"))
    r = _file(e)
    assert r["success"] is False and r["reason"] and r["report_id"]
    assert "blocker-secret-name" not in json.dumps(r) and str(tmp_path) not in json.dumps(r)


# --- truly fixed status vocabulary + blank-root fail-closed --------------------------------------
VOCAB = __import__("re").compile(
    r"^(ok|unavailable|index_building|error: digest ceiling exceeded|error: [A-Za-z_][A-Za-z0-9_]*)$")
LEAKS = ["corpora/hush-private/x.lts", "/tmp/My Corpus/x", "~bob/notes", "hush-private", "My Corpus"]


@pytest.mark.parametrize("exc", [RuntimeError, ValueError, PermissionError])
def test_fv_exception_text_never_reaches_the_response_or_the_record(w, monkeypatch, exc):
    e, log, tmp = w
    msg = "failed on corpora/hush-private/x.lts then /tmp/My Corpus/x and ~bob/notes"

    def raiser(*a, **k):
        raise exc(msg)
    monkeypatch.setattr(e, "find", raiser)
    monkeypatch.setattr(e, "semantic_find", raiser)
    r = _file(e)
    assert r["success"]
    for text in (json.dumps(r), log.read_text()):
        for leak in LEAKS:
            assert leak not in text, leak
    assert r["exact_status"] == f"error: {exc.__name__}" == r["semantic_status"]
    assert _recs(log)[0]["exact_status"] == r["exact_status"]


def test_fv_error_responses_with_leaky_text_collapse_to_the_fixed_token(w, monkeypatch):
    e, log, _ = w
    leaky = "cannot read corpora/hush-private/x.lts and /tmp/My Corpus/x and ~bob/notes"
    monkeypatch.setattr(e, "find", lambda **k: {"success": False, "error": leaky})
    monkeypatch.setattr(e, "semantic_find", lambda **k: {"success": True, "status": leaky})
    r = _file(e)
    for text in (json.dumps(r), log.read_text()):
        for leak in LEAKS:
            assert leak not in text, leak
    assert VOCAB.match(r["exact_status"]) and VOCAB.match(r["semantic_status"])


def test_fv_every_status_in_every_scenario_is_in_the_fixed_vocabulary(w, monkeypatch):
    e, log, _ = w
    seen = [_file(e)]
    monkeypatch.setattr(e, "find", lambda **k: {"success": False, "reason": "digest_ceiling_exceeded", "error": "x"})
    seen.append(_file(e))
    for st in ("ok", "unavailable", "index_building", "index_failed", "stale"):
        monkeypatch.setattr(e, "semantic_find", lambda st=st, **k: {"success": True, "status": st})
        seen.append(_file(e))
    for r in seen:
        assert VOCAB.match(r["exact_status"]), r["exact_status"]
        assert VOCAB.match(r["semantic_status"]), r["semantic_status"]
    for rec in _recs(log):
        assert VOCAB.match(rec["exact_status"]) and VOCAB.match(rec["semantic_status"])


def test_fv_a_write_failure_reports_only_the_exception_class(w, monkeypatch, tmp_path):
    e, log, _ = w
    blocker = tmp_path / "My Blocker"
    blocker.write_text("file")
    monkeypatch.setenv(querylog.ENV_DIR, str(blocker / "sub"))
    r = _file(e)
    assert r["success"] is False
    assert r["reason"] == "miss-log-write-failed"
    assert __import__("re").fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", r["error_class"]), r["error_class"]
    assert r["error"] == f"could not write the miss log: {r['error_class']}"
    assert "My Blocker" not in json.dumps(r) and "/" not in r["error_class"]


def test_fv_detail_goes_to_the_stderr_log_only(w, monkeypatch, caplog):
    e, log, _ = w

    def raiser(*a, **k):
        raise RuntimeError("detail-for-the-operator")
    monkeypatch.setattr(e, "find", raiser)
    with caplog.at_level("WARNING"):
        r = _file(e)
    assert "detail-for-the-operator" not in json.dumps(r) and "detail-for-the-operator" not in log.read_text()
    assert "detail-for-the-operator" in caplog.text


@pytest.mark.parametrize("bad_roots", [[""], ["  "], ["\t"], [None], [5]])
def test_p32_a_blank_or_invalid_root_fails_closed_never_normalising_to_cwd(w, monkeypatch, bad_roots):
    e, log, tmp = w
    from factlattice_engine import membership
    monkeypatch.setattr(membership, "registered_roots",
                        lambda store=None: ({"c": [str(tmp / "corpus")] + bad_roots}, "store"))
    r = _file(e)
    assert r["success"] is False and REFUSAL in r["error"]
    assert not log.exists() and not log.parent.exists()


# --- delta-3 review P1: an exception CLASS NAME can carry data -------------------------------
HOSTILE_NAMES = ("/abs/path/leak", "a\nb", "Err ~bob/notes", "privcorp-NAME-xyz/q")


def _hostile(name):
    return type(name, (Exception,), {})


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_d3_hostile_class_name_in_find_never_reaches_response_or_log(w, monkeypatch, name):
    e, log, _ = w
    cls = _hostile(name)

    def boom(**k):
        raise cls("x")
    monkeypatch.setattr(e, "find", boom)
    r = _file(e)
    assert r["exact_status"] == "error: Exception"
    assert name not in json.dumps(r) and name not in log.read_text()


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_d3_hostile_class_name_in_semantic_never_reaches_response_or_log(w, monkeypatch, name):
    e, log, _ = w
    cls = _hostile(name)

    def boom(**k):
        raise cls("x")
    monkeypatch.setattr(e, "semantic_find", boom)
    r = _file(e)
    assert r["semantic_status"] == "error: Exception"
    assert name not in json.dumps(r) and name not in log.read_text()


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_d3_hostile_class_name_in_write_failure_never_reaches_response(w, monkeypatch, name):
    e, log, _ = w
    import factlattice_engine.tools as T
    cls = _hostile(name)

    def bad_open(*a, **k):
        raise cls("x")
    monkeypatch.setattr(T, "open", bad_open, raising=False)
    r = _file(e)
    assert r["success"] is False and r["reason"] == "miss-log-write-failed" and r["error_class"] == "Exception"
    assert r["error"] == "could not write the miss log: Exception"
    assert name not in json.dumps(r)


def test_d3_an_ordinary_class_name_is_still_reported():
    from factlattice_engine.tools import _exc_status
    assert _exc_status(PermissionError("x")) == "error: PermissionError"


@pytest.mark.parametrize("bad", ["/x/y", "zz", "abc\n", "ABCDEF12", "", None, 7])
def test_p3_index_version_must_be_a_hex_digest_or_null(w, monkeypatch, bad):
    e, log, _ = w
    monkeypatch.setattr(e, "semantic_find", lambda **k: {"status": "ok", "candidates": [], "index_version": bad})
    r = _file(e)
    assert _recs(log)[-1]["index_version"] is None
