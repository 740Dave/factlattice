"""[FIND-QUERY-SURFACE] the lts_find query surface: terms, match modes, property conditions, document
filters, the echo, the phrase-zero fallback, the schema/description/doc/log surface (spec §10).

Every NEW-behaviour test here is RED at main 64ee63f for the defect (the parameter / behaviour does not
exist); the parity/characterisation tests live in test_query_surface_parity.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from factlattice_engine import querylog, readprofile, server, tools
try:                                   # absent at the pre-change commit: the tests that need it then FAIL
    from factlattice_engine import query_surface as qs
except ImportError:                     # (the module is the feature), the rest fail on behaviour
    qs = None
from tests import qs_fixture as F

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def eng(tmp_path):
    e, _root = F.engine_a(tmp_path)
    return e


def ids(r):
    assert r.get("success") is True, r
    return sorted(n["id"] for n in (r.get("nodes") or r.get("digest") or []))


def find(eng, **kw):
    kw.setdefault("semantic", "off")
    return eng.find(corpus="qs", **kw)


# ============================================================ match modes (item 2)
def test_phrase_is_the_default_and_is_a_substring_of_one_field(eng):
    assert ids(find(eng, all_of=["qa"])) == ["PLAN-QATAR", "PROC-ALANTERN", "PROC-LANTERN-ARCH"]
    assert ids(find(eng, all_of=["qa"], match="phrase")) == ids(find(eng, all_of=["qa"]))


def test_word_mode_whole_words_only(eng):
    assert ids(find(eng, all_of=["qa"], match="word")) == ["PROC-LANTERN-ARCH"]    # 'qa' is not 'Aqaba'/'Qatar'
    assert ids(find(eng, all_of=["borr"], match="word")) == []                    # no stemming / partial word
    assert ids(find(eng, all_of=["borr"])) == ["XL-M1", "XL-M2"]                  # phrase would hit


def test_word_mode_is_contiguous_and_in_order_in_one_field(eng):
    assert ids(find(eng, all_of=["card contents"], match="word")) == ["XL-M1", "XL-M2"]
    assert ids(find(eng, all_of=["contents card"], match="word")) == []             # order matters
    # words in TWO different fields of XL-M2 (title 'Borrower notes', description 'card contents differ')
    assert ids(find(eng, all_of=["borrower card"], match="word")) == ["XL-M1"]
    assert ids(find(eng, all_of=["notes card"], match="word")) == []


def test_prefix_mode_whole_word_starts_with(eng):
    assert ids(find(eng, all_of=["lantern"], match="prefix")) == ["PROC-LANTERN-ARCH"]     # not 'alantern'
    assert ids(find(eng, all_of=["lantern"])) == ["PROC-ALANTERN", "PROC-LANTERN-ARCH"]     # phrase hits both
    assert ids(find(eng, all_of=["qa"], match="prefix")) == ["PLAN-QATAR", "PROC-LANTERN-ARCH"]


def test_word_mode_hyphen_is_contiguous_words(eng):
    assert ids(find(eng, all_of=["go-live"], match="word")) == ["PROC-LIVE", "PROC-LIVE2"]    # 'go live' too
    assert "PROC-LIVE3" not in ids(find(eng, all_of=["go-live"], match="word"))               # 'go to live' no
    assert ids(find(eng, all_of=["go-live"], match="phrase")) == ["PROC-LIVE"]               # literal hyphen
    assert ids(find(eng, all_of=["XL-M1"], match="word")) == ["XL-M1"]                       # a node id is zc,m1


def test_per_term_match_overrides_the_call_default(eng):
    r = find(eng, all_of=[{"term": "lantern", "match": "prefix"}, {"term": "qa", "match": "phrase"}],
             prefix="PROC-")
    assert ids(r) == ["PROC-LANTERN-ARCH"]
    assert r["query_interpreted"]["filters"]["prefix"] == "PROC-"
    modes = [t["match"] for t in r["query_interpreted"]["all_of"]]
    assert modes == ["prefix", "phrase"]


def test_case_insensitive_terms(eng):
    assert ids(find(eng, all_of=["BORROWER CARD"])) == ids(find(eng, all_of=["borrower card"])) == ["XL-M1"]
    assert ids(find(eng, all_of=["BORROWER"], match="word")) == ["XL-M1", "XL-M2"]


# ============================================================ all / any / none (item 3)
def test_all_any_none_combinations(eng):
    assert ids(find(eng, all_of=["go", "live"])) == ["PROC-LIVE", "PROC-LIVE2", "PROC-LIVE3"]
    assert ids(find(eng, any_of=["qatar", "aqaba"])) == ["PLAN-QATAR", "PROC-ALANTERN"]
    assert ids(find(eng, prefix="PROC-", none_of=["live"])) == ["PROC-ALANTERN", "PROC-LANTERN-ARCH"]
    assert ids(find(eng, all_of=["go"], any_of=["live now", "to live"], none_of=["to live"])) == ["PROC-LIVE2"]
    # combined with `text` and the other filters by AND
    assert ids(find(eng, text="live", all_of=["go"], status="operational", prefix="PROC-")) == \
        ["PROC-LIVE", "PROC-LIVE2", "PROC-LIVE3"]


def test_none_of_alone_is_refused_but_with_a_positive_filter_is_fine(eng):
    r = find(eng, none_of=["live"])
    assert r["success"] is False and "none_of" in r["error"]
    assert find(eng, none_of=["live"], prefix="PROC-")["success"] is True
    assert find(eng, none_of=["live"], where=[{"key": "status", "has": True}])["success"] is True


@pytest.mark.parametrize("kw,param", [
    (dict(all_of=[]), "all_of"), (dict(any_of=[]), "any_of"), (dict(none_of=[], prefix="P"), "none_of"),
    (dict(all_of=[""]), "all_of[0]"), (dict(any_of=["ok", "  "]), "any_of[1]"),
    (dict(all_of=[{"term": ""}]), "all_of[0]"),
    (dict(all_of=["lantern architect"], match="prefix"), "all_of[0]"),     # multi-word prefix
    (dict(all_of=["g"], match="prefix"), "all_of[0]"),                     # under 2 characters
    (dict(all_of=["---"], match="word"), "all_of[0]"),                     # no word characters
    (dict(all_of=["x"], match="glob"), "match"),
    (dict(all_of=[{"term": "x", "match": "regex"}]), "all_of[0]"),
    (dict(all_of=[{"term": "x", "bogus": 1}]), "all_of[0]"),
    (dict(all_of="lantern"), "all_of"), (dict(all_of=[3]), "all_of[0]"),
    (dict(where=[]), "where"), (dict(where=[{"key": "Bad Key", "has": True}]), "where[0]"),
    (dict(where=[{"key": "kind"}]), "where[0]"),                           # zero operators
    (dict(where=[{"key": "kind", "has": True, "lacks": True}]), "where[0]"),  # two operators
    (dict(where=[{"key": "kind", "like": "x"}]), "where[0]"),
    (dict(where=[{"key": "kind", "has": False}]), "where[0]"),
    (dict(where=[{"key": "kind", "one_of": []}]), "where[0]"),
    (dict(where=[{"key": "kind", "equals": ""}]), "where[0]"),
    (dict(where="kind"), "where"), (dict(fields=[]), "fields"), (dict(fields=["title", ""]), "fields"),
])
def test_refusals_name_the_parameter(eng, kw, param):
    kw = dict(kw)
    kw.setdefault("prefix", "PROC-") if "none_of" in kw else None
    r = find(eng, **kw)
    assert r["success"] is False, r
    assert param in r["error"], (param, r["error"])


@pytest.mark.parametrize("bad", [3, 3.5, True, None, ["a"], {"x": 1}])
def test_where_values_must_be_strings(eng, bad):
    for op in ("equals", "not_equals"):
        r = find(eng, where=[{"key": "ykey", op: bad}])
        assert r["success"] is False and "where[0]" in r["error"] and "string" in r["error"]
    r = find(eng, where=[{"key": "ykey", "one_of": [bad]}])
    assert r["success"] is False and "where[0]" in r["error"]


# ============================================================ where (item 4)
def test_planted_not_one_of_list_case(eng):
    """[ZBA] not_one_of {a, b}: Y=[a,x] MUST be returned; Y=[a,b] must NOT; absent must NOT;
    scalar x returned; scalar a not. (A mapping carries no string value: not matched.)"""
    r = find(eng, prefix="Y-", where=[{"key": "ykey", "not_one_of": ["a", "b"]}])
    assert ids(r) == ["Y-LIST-AX", "Y-SCALAR-X"]
    assert "Y-LIST-AB" not in ids(r) and "Y-ABSENT" not in ids(r) and "Y-SCALAR-A" not in ids(r)
    assert "Y-MAP" not in ids(r)


def test_where_operators_on_strings_lists_and_mappings(eng):
    W = lambda **c: ids(find(eng, prefix="Y-", where=[{"key": "ykey", **c}]))
    assert W(has=True) == ["Y-LIST-AB", "Y-LIST-AX", "Y-SCALAR-A", "Y-SCALAR-X"]
    assert W(lacks=True) == ["Y-ABSENT", "Y-MAP"]
    assert W(equals="a") == ["Y-LIST-AB", "Y-LIST-AX", "Y-SCALAR-A"]            # ANY list element
    assert W(one_of=["x", "zzz"]) == ["Y-LIST-AX", "Y-SCALAR-X"]
    assert W(not_equals="a") == ["Y-LIST-AB", "Y-LIST-AX", "Y-SCALAR-X"]       # missing never matches
    # mapping: `has` only
    M = lambda **c: ids(find(eng, prefix="Y-", where=[{"key": "ymap", **c}]))
    assert M(has=True) == ["Y-MAP"]
    assert M(equals="a") == [] and M(one_of=["a"]) == [] and M(not_equals="zzz") == []


def test_where_equality_is_case_sensitive_and_trimmed(eng):
    assert ids(find(eng, where=[{"key": "kind", "equals": "note"}])) == ["PROC-LANTERN-ARCH", "UNT-NODE"]
    assert ids(find(eng, where=[{"key": "kind", "equals": "Note"}])) == ["PROC-ALANTERN"]
    # surrounding whitespace is trimmed on BOTH sides of the comparison (unit-level)
    assert qs.Cond("k", "equals", value="a").test({"k": "  a "}) is True
    assert qs.Cond("k", "one_of", values=frozenset({"a"})).test({"k": ["  a ", "q"]}) is True
    assert qs.Cond("k", "not_equals", value="a").test({"k": ["a ", " a"]}) is False


def test_where_is_anded_and_status_in_where_is_plain_equality(eng):
    r = find(eng, where=[{"key": "tier", "has": True}, {"key": "kind", "lacks": True}])
    assert ids(r) == ["PROC-LIVE2"]
    # the `status` FILTER widens a bare token over a qualified value; `where` equality does not
    assert "Q-QUAL" in ids(find(eng, status="proposed"))
    assert ids(find(eng, where=[{"key": "status", "equals": "proposed"}])) == []
    assert ids(find(eng, where=[{"key": "status", "equals": "proposed — awaiting review"}])) == ["Q-QUAL"]
    echo = find(eng, where=[{"key": "status", "equals": "x"}])["query_interpreted"]["where"][0]
    assert "no lifecycle widening" in echo["note"]


def test_misspelled_key_warns_and_reports_scope_count(eng):
    r = find(eng, prefix="PROC-", where=[{"key": "tierr", "lacks": True}])
    assert ids(r) == ["PROC-ALANTERN", "PROC-LANTERN-ARCH", "PROC-LIVE", "PROC-LIVE2", "PROC-LIVE3"]
    w = r["query_interpreted"]["where"][0]
    assert w["key_present_in_scope"] == 0
    assert r["warnings"] == ["no node in scope carries 'tierr'; `lacks` therefore matches every node in "
                             "scope, so check the spelling"]
    ok = find(eng, prefix="PROC-", where=[{"key": "tier", "lacks": True}])
    assert ok["query_interpreted"]["where"][0]["key_present_in_scope"] == 3
    assert "warnings" not in ok


def test_a_misspelled_key_with_has_warns_that_nothing_can_match(eng):
    r = find(eng, where=[{"key": "tierr", "has": True}])
    assert ids(r) == [] and "matches no node" in r["warnings"][0]


def test_key_present_in_scope_counts_only_nodes_passing_the_other_filters(eng):
    r = find(eng, prefix="Y-", where=[{"key": "ykey", "equals": "x"}])
    assert r["query_interpreted"]["where"][0]["key_present_in_scope"] == 4        # AX, AB, SCALAR-X, SCALAR-A
    r = find(eng, since="2026-09-10", where=[{"key": "ykey", "has": True}])
    assert r["query_interpreted"]["where"][0]["key_present_in_scope"] == 0       # the date window is an 'other filter'


def test_where_only_over_everything_keeps_the_true_total_when_truncated(eng):
    n = len(eng._gate("qs")[0].nodes)
    r = find(eng, where=[{"key": "nosuchkey", "lacks": True}], token_budget=40)
    assert r["total_matched"] == n and r["truncated"] is True and r["count"] < n
    assert r["cursor"] is not None


# ============================================================ fields (item 5)
def test_fields_restricts_terms_not_text(eng):
    assert ids(find(eng, all_of=["lantern"], fields=["alias"])) == ["PROC-LANTERN-ARCH"]
    assert ids(find(eng, all_of=["lantern"], fields=["title"])) == ["PROC-ALANTERN", "PROC-LANTERN-ARCH"]
    assert ids(find(eng, all_of=["lantern"], fields=["tier"])) == []
    assert ids(find(eng, all_of=["aqaba"], fields=["id"])) == []
    assert ids(find(eng, all_of=["aqaba"], fields=["id", "title"])) == ["PROC-ALANTERN"]
    assert ids(find(eng, all_of=["qa"], match="word", fields=["alias"])) == ["PROC-LANTERN-ARCH"]
    assert ids(find(eng, all_of=["lant"], match="prefix", fields=["alias"])) == ["PROC-LANTERN-ARCH"]
    # `text` ignores `fields` (and a fields-without-terms call says so)
    r = find(eng, text="lantern", fields=["alias"])
    assert ids(r) == ["PROC-ALANTERN", "PROC-LANTERN-ARCH"]
    assert any("`fields` has no effect" in w for w in r["warnings"])


def test_fields_and_terms_appear_in_keys_consulted(eng):
    r = find(eng, all_of=["lantern"], fields=["alias", "title"], where=[{"key": "tier", "has": True}])
    kc = r["extent"]["keys_consulted"]
    assert "properties.alias" in kc and "title" in kc and "properties.tier" in kc
    kc2 = find(eng, all_of=["lantern"])["extent"]["keys_consulted"]
    assert kc2 == ["id", "title", "properties.*"]


# ============================================================ source_file / document_tag (item 5a)
def test_source_file_exact_and_refusals(eng, tmp_path):
    assert ids(find(eng, source_file="b.lts")) == ["UNT-NODE", "UNT-ROOT"]
    assert len(ids(find(eng, source_file="a.lts"))) == 16
    r = find(eng, source_file="nope.lts")
    assert r["success"] is False and "source_file" in r["error"] and "nope.lts" not in r["error"]
    r = eng.find(source_file="a.lts", semantic="off")           # no corpus
    assert r["success"] is False and "corpus" in r["error"]
    assert "source_file" in find(eng, source_file="b.lts")["extent"]["keys_consulted"]


def test_source_file_ambiguous_multiroot_refused_without_area(tmp_path):
    m = F.engine_multiroot(tmp_path)
    r = m.find(corpus="m", source_file="same.lts", semantic="off")
    assert r["success"] is False and "ambiguous" in r["error"] and "same.lts" not in r["error"]
    r = m.find(corpus="m", source_file="same.lts", area="decisions", semantic="off")
    assert r["success"] is True and "M-ONE" in ids(r) and "M-TWO" not in ids(r)
    r = m.find(corpus="m", source_file="only1.lts", semantic="off")        # exists under ONE root: fine
    assert r["success"] is True and "M-THREE" in ids(r)


def test_document_tag_case_sensitive_and_untagged_never_match(eng):
    r = find(eng, document_tag="SPEC")
    assert "UNT-ROOT" not in ids(r) and "UNT-NODE" not in ids(r) and len(ids(r)) == 16
    assert ids(find(eng, document_tag="spec")) == []
    echo = r["query_interpreted"]["filters"]
    assert echo["document_tag"] == "SPEC" and echo["document_tag_untagged_in_scope"] == 2
    assert "document_tag" in r["extent"]["keys_consulted"]


# ============================================================ echo + warnings (item 6)
def test_echo_present_iff_a_new_parameter_is_used(eng):
    assert "query_interpreted" not in find(eng, text="lantern")
    assert "query_interpreted" not in find(eng, prefix="PROC-", status="operational")
    for kw in (dict(all_of=["x"]), dict(any_of=["x"]), dict(none_of=["x"], prefix="P"), dict(match="word"),
               dict(where=[{"key": "k", "has": True}]), dict(fields=["id"]), dict(source_file="a.lts"),
               dict(document_tag="SPEC"), dict(explain=True)):
        assert "query_interpreted" in find(eng, **kw), kw
    assert "query_interpreted" not in find(eng, text="lantern", explain=False)


def test_explain_adds_the_echo_to_a_plain_text_call_without_changing_the_rest(eng):
    plain = find(eng, text="borrower card contents")
    ex = find(eng, text="borrower card contents", explain=True)
    q = ex.pop("query_interpreted")
    assert ex == plain
    assert q["text"] == {"value": "borrower card contents", "mode": qs.TEXT_MODE}
    assert "phrase" in q["text"]["mode"]


def test_echo_shows_what_was_evaluated(eng):
    r = find(eng, text="note", all_of=[{"term": "go-live", "match": "word"}], any_of=["a"], none_of=["z"],
             fields=["title"], prefix="PROC-", status="operational", since="2026-01-01", match="phrase",
             where=[{"key": "kind", "one_of": ["b", "a"]}])
    q = r["query_interpreted"]
    assert q["text"]["value"] == "note"
    assert q["all_of"] == [{"term": "go-live", "match": "word", "words": ["go", "live"], "fields": ["title"]}]
    assert q["any_of"][0]["match"] == "phrase" and "words" not in q["any_of"][0]
    assert q["none_of"][0]["term"] == "z"
    assert q["where"] == [{"key": "kind", "op": "one_of", "values": ["a", "b"], "key_present_in_scope": 0}]
    assert q["filters"] == {"prefix": "PROC-", "status": "operational", "since": "2026-01-01",
                            "date_property": "decided-in-session"}
    assert "AND" in q["combine"]


def test_warnings_present_iff_a_warning_fires(eng):
    assert "warnings" not in find(eng, all_of=["lantern"])
    assert "warnings" not in find(eng, where=[{"key": "tier", "has": True}])
    assert "warnings" in find(eng, all_of=["lantern"], match=None, fields=None, where=[{"key": "zz", "has": True}])
    assert any("`match` has no effect" in w for w in find(eng, match="word", text="x")["warnings"])


def test_an_empty_scope_says_so_in_the_misspelling_warning(eng):
    r = find(eng, prefix="NOPE-", where=[{"key": "tier", "has": True}])
    assert "no node passes the other filters" in r["warnings"][0]


def test_prose_and_aggregate_carry_the_echo_and_warnings(eng):
    p = eng.find_in_prose(text="status", corpus="qs", prefix="PROC-", where=[{"key": "tierr", "lacks": True}])
    assert p["query_interpreted"]["where"][0]["key_present_in_scope"] == 0
    assert p["warnings"] and "BODY" in p["query_interpreted"]["text"]["mode"]
    assert "query_interpreted" not in eng.find_in_prose(text="status", corpus="qs")
    a = tools.dispatch("lts_aggregate", {"op": "count", "corpus": "qs", "where": [{"key": "tierr", "has": True}]}, eng)
    assert a["success"] and a["total"] == 0 and a["warnings"]
    assert "query_interpreted" not in tools.dispatch("lts_aggregate", {"op": "count", "corpus": "qs"}, eng)
    bad = tools.dispatch("lts_aggregate", {"op": "count", "corpus": "qs", "all_of": []}, eng)
    assert bad["success"] is False and "all_of" in bad["error"]


def test_dispatch_passes_the_new_parameters_through(eng):
    r = tools.dispatch("lts_find", {"corpus": "qs", "semantic": "off", "all_of": ["lantern"], "match": "prefix"}, eng)
    assert ids(r) == ["PROC-LANTERN-ARCH"]
    r = tools.dispatch("lts_find", {"corpus": "qs", "none_of": ["x"]}, eng)
    assert r["success"] is False


def test_semantic_on_miss_keep_filter_honours_the_new_conditions(eng, monkeypatch):
    """The semantic suggestion `keep` filter applies EVERY filter of the exact call (F10): it must
    include the new conditions so suggestions cannot leak nodes the conditions exclude."""
    seen = {}

    class Fake:
        def suggest(self, part, text, k=5, keep=None):
            seen["kept"] = sorted(i for i, nd in part.nodes.items() if keep(i, nd))
            return {"mode": "semantic", "kind": "semantic-suggestions-not-matches", "is_assertion": False,
                    "status": "ok", "candidates": []}

    eng.semantic = Fake()
    r = eng.find(corpus="qs", text="zzzz-nothing", prefix="PROC-", none_of=["live"], semantic="on_miss")
    assert r["total_matched"] == 0 and "did_you_mean_semantic" in r
    assert seen["kept"] == ["PROC-ALANTERN", "PROC-LANTERN-ARCH"]


# ============================================================ fallback (item 8)
@pytest.fixture()
def fb_on(monkeypatch):
    monkeypatch.setenv("LTS_FIND_WORD_FALLBACK", "on")


def test_fallback_flag_off_or_unset_is_byte_identical(eng, monkeypatch):
    monkeypatch.delenv("LTS_FIND_WORD_FALLBACK", raising=False)
    base = {t: find(eng, text=t) for t in ("borrower card contents", "borrower contents", "zzz yyy")}
    monkeypatch.setenv("LTS_FIND_WORD_FALLBACK", "off")
    for t, b in base.items():
        assert find(eng, text=t) == b and "matched_all_words" not in b
    assert "matched_all_words" not in eng.find(text="borrower contents", semantic="off")      # no corpus


def test_fallback_with_a_phrase_hit_is_identical_to_off_and_key_absent(eng, monkeypatch):
    monkeypatch.setenv("LTS_FIND_WORD_FALLBACK", "off")
    off = find(eng, text="borrower card contents")
    monkeypatch.setenv("LTS_FIND_WORD_FALLBACK", "on")
    on = find(eng, text="borrower card contents")
    assert on == off and "matched_all_words" not in on


def test_fallback_phrase_zero_lists_word_and_matches(eng, monkeypatch):
    monkeypatch.setenv("LTS_FIND_WORD_FALLBACK", "off")
    off = find(eng, text="borrower contents")
    monkeypatch.setenv("LTS_FIND_WORD_FALLBACK", "on")
    on = find(eng, text="borrower contents")
    blk = on.pop("matched_all_words")
    assert on == off and off["total_matched"] == 0                      # exact fields untouched
    assert blk["kind"] == "word-and-fallback" and blk["is_exact_match"] is False and blk["is_assertion"] is False
    assert blk["query_words"] == ["borrower", "contents"] and blk["total"] == 2 and blk["capped"] is False
    assert sorted(n["id"] for n in blk["nodes"]) == ["XL-M1", "XL-M2"]
    assert [n["rank"] for n in blk["nodes"]] == [1, 2]
    assert set(blk["nodes"][0]) == {"rank", "id", "title"} and "note" in blk


def test_fallback_words_anywhere_across_alias_list_lines(fb_on, eng):
    blk = find(eng, text="checklist alias")["matched_all_words"]
    assert [n["id"] for n in blk["nodes"]] == ["PROC-LANTERN-ARCH"]      # one word per alias line


def test_fallback_has_no_stemming_and_drops_stop_words(fb_on, eng):
    assert find(eng, text="borrowers contents")["matched_all_words"]["total"] == 0      # no stemming
    blk = find(eng, text="the borrower of contents")["matched_all_words"]
    assert blk["query_words"] == ["borrower", "contents"] and blk["total"] == 2


def test_fallback_applies_every_other_filter(fb_on, eng):
    assert find(eng, text="borrower contents", prefix="XL-M2")["matched_all_words"]["total"] == 1
    assert find(eng, text="borrower contents", status="draft")["matched_all_words"]["total"] == 0
    assert find(eng, text="borrower contents", none_of=["notes"])["matched_all_words"]["total"] == 1
    assert find(eng, text="borrower contents", where=[{"key": "input", "has": True}])["matched_all_words"]["total"] == 1


def test_fallback_trigger_edges(fb_on, eng):
    assert "matched_all_words" not in find(eng, text="FINDING-FOO-BAR")                 # id-shaped
    assert "matched_all_words" not in find(eng, text="FINDING-NOT-A-NODE-X")
    assert "matched_all_words" not in find(eng, text="zzzqqq")                          # one word
    assert "matched_all_words" not in find(eng, text="the of")                          # < 2 words after stop-words
    blk = find(eng, text="zorblax qwertyuiop")["matched_all_words"]                    # runs, finds nothing
    assert blk["total"] == 0 and blk["nodes"] == []
    assert "matched_all_words" not in eng.find(text="borrower contents", semantic="off")  # no corpus
    assert "matched_all_words" not in find(eng, text="borrower contents", word_fallback=False)   # internal


def test_fallback_applies_for_every_tier(fb_on, eng):
    for tier in (None, "digest", "page", "full"):
        r = find(eng, text="borrower contents", tier=tier) if tier else find(eng, text="borrower contents")
        assert r["matched_all_words"]["total"] == 2, tier


def test_fallback_unknown_flag_value_warns_once_and_means_off(eng, monkeypatch, capsys):
    qs._WARNED_FALLBACK.clear()
    monkeypatch.setenv("LTS_FIND_WORD_FALLBACK", "maybe")
    for _ in range(3):
        assert "matched_all_words" not in find(eng, text="borrower contents")
    err = capsys.readouterr().err
    assert err.count("LTS_FIND_WORD_FALLBACK='maybe'") == 1


def test_fallback_is_logged_apart_from_result_ids(fb_on, eng):
    res = find(eng, text="borrower contents")
    rec = querylog.build_record("lts_find", {"corpus": "qs", "text": "borrower contents"}, res, 1.0)
    assert rec["word_fallback"]["total"] == 2 and rec["word_fallback"]["capped"] is False
    assert sorted(rec["word_fallback"]["ids"]) == ["XL-M1", "XL-M2"] and rec["result_ids"] == []
    plain = querylog.build_record("lts_find", {"corpus": "qs", "text": "borrower card contents"},
                                  find(eng, text="borrower card contents"), 1.0)
    assert "word_fallback" not in plain


def test_internal_report_miss_never_uses_the_fallback(fb_on, eng):
    calls = []
    real = eng.find

    def spy(*a, **k):
        calls.append(k)
        return real(*a, **k)

    eng.find = spy
    r = eng.report_miss(corpus="qs", query="borrower contents", expected_ids=["XL-M1"], why="test")
    assert r.get("success") is True, r
    assert calls and all(c.get("word_fallback") is False for c in calls)


def test_fallback_description_sentence_only_when_the_server_starts_with_the_flag():
    code = ("from factlattice_engine import server;"
            "d=[t for t in server.TOOLS if t['name']=='lts_find'][0]['description'];"
            "print('matched_all_words' in d)")
    out = {}
    for val in ("on", "off", None):
        env = {k: v for k, v in os.environ.items() if k != "LTS_FIND_WORD_FALLBACK"}
        env["PYTHONPATH"] = str(ROOT)
        if val:
            env["LTS_FIND_WORD_FALLBACK"] = val
        out[val] = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                                  cwd=str(ROOT)).stdout.strip()
    assert out == {"on": "True", "off": "False", None: "False"}


# ============================================================ schema / description / docs surface
NEW = ["all_of", "any_of", "none_of", "match", "where", "fields", "source_file", "document_tag", "explain"]


def _tool(name):
    return next(t for t in server.TOOLS if t["name"] == name)


@pytest.mark.parametrize("tool", ["lts_find", "lts_find_in_prose", "lts_aggregate"])
def test_new_parameters_are_in_the_input_schema(tool):
    props = _tool(tool)["inputSchema"]["properties"]
    for p in NEW:
        assert p in props and props[p].get("description"), (tool, p)
    assert props["match"]["enum"] == ["phrase", "word", "prefix"]


def test_profile_audit_required_scope_schema_allows_an_object():
    rs = _tool("lts_profile_audit")["inputSchema"]["properties"]["required_scope"]
    assert {"type": "object"} in rs["oneOf"]


def test_description_sentences():
    d = _tool("lts_find")["description"]
    assert "where is AND-only: for OR, make two calls" in d
    assert "not_one_of/not_equals never match a node lacking the key: ask `lacks` separately" in d
    assert "TERMS (text, all_of, any_of, none_of) are case-insensitive" in d
    assert "`where` equality and `document_tag` are case-sensitive" in d
    pre = _tool("lts_find")["inputSchema"]["properties"]["prefix"]["description"]
    assert "trailing hyphen ('PROC-')" in pre and "'PROCESS-" in pre
    assert "NOT word by word" in d                    # the phrase sentence of FIND-PHRASE-SEMANTICS is intact


def test_spec_md_documents_the_new_response_keys():
    spec = (ROOT / "SPEC.md").read_text(encoding="utf-8")
    for k in ("query_interpreted", "warnings", "matched_all_words", "key_present_in_scope", "word_fallback"):
        assert k in spec, k


def test_sdlc_md_carries_the_four_checklist_rules():
    s = (ROOT / "SDLC.md").read_text(encoding="utf-8")
    assert "_FIND_FILTER_KEYS" in s and "_node_matches" in s
    assert "ABSENT when not triggered" in s and "absence test" in s
    assert "match mode" in s and "parity test" in s
    assert "literal-or-digest" in s
    for n in (1, 2, 3, 4):
        assert f"[FIND-QUERY-SURFACE-{n}]" in s


# ============================================================ query log (§9a)
def test_querylog_digests_new_values_by_default_and_logs_structure_literally(monkeypatch):
    monkeypatch.delenv("FACTLATTICE_QUERY_LOG_LITERALS", raising=False)
    a = {"corpus": "qs", "all_of": ["secret-term", {"term": "other", "match": "prefix"}], "match": "word",
         "none_of": ["zz"], "fields": ["title"], "document_tag": "SPEC", "explain": True,
         "where": [{"key": "ykey", "equals": "classified"}, {"key": "tier", "one_of": ["p", "q"]},
                   {"key": "kind", "lacks": True}]}
    rec = querylog.build_record("lts_find", a, {"success": True, "nodes": []}, 1.0)
    blob = json.dumps(rec)
    for secret in ("secret-term", "other", "classified", '"zz"'):
        assert secret not in blob, secret
    f = rec["filters"]
    assert f["all_of"][0]["match"] == "word" and f["all_of"][1]["match"] == "prefix"
    assert f["all_of"][0]["len"] == len("secret-term") and "sha" in f["all_of"][0]
    assert f["match"] == "word" and f["fields"] == ["title"]
    assert "SPEC" not in blob and f["document_tag"]["len"] == 4
    assert f["where"][0]["key"] == "ykey" and f["where"][0]["op"] == "equals" and "sha" in f["where"][0]
    assert f["where"][1]["op"] == "one_of" and len(f["where"][1]["values"]) == 2
    assert f["where"][2] == {"key": "kind", "op": "lacks"}


def test_querylog_literals_only_under_the_literals_flag(monkeypatch):
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG_LITERALS", "1")
    rec = querylog.build_record("lts_find", {"corpus": "qs", "all_of": ["secret-term"],
                                             "where": [{"key": "ykey", "equals": "classified"}]},
                                {"success": True}, 1.0)
    assert rec["filters"]["all_of"][0]["value"] == "secret-term"
    assert rec["filters"]["where"][0]["value"] == "classified"


def test_querylog_survives_malformed_arguments():
    rec = querylog.build_record("lts_find", {"all_of": 5, "where": "x", "fields": 3, "source_file": "/a/b/c.lts"},
                                {"success": False, "error": "refused"}, 1.0)
    assert rec["ok"] is False and rec["filters"]["source_file"]["len"] == len("/a/b/c.lts")
    assert "c.lts" not in json.dumps(rec)


def test_readprofile_treats_where_only_calls_as_distinct_questions(monkeypatch):
    monkeypatch.delenv("FACTLATTICE_QUERY_LOG_LITERALS", raising=False)
    mk = lambda **a: querylog.build_record("lts_find", {"corpus": "qs", **a}, {"success": True}, 1.0)
    k1 = readprofile._question_key(mk(where=[{"key": "k", "equals": "a"}]))
    k2 = readprofile._question_key(mk(where=[{"key": "k", "equals": "b"}]))
    k3 = readprofile._question_key(mk(prefix="P-"))
    assert len({k1, k2, k3}) == 3
    assert readprofile._question_key(mk(prefix="P-")) == "lts_find|prefix=P-"        # old key unchanged


def test_query_is_logged_end_to_end_through_dispatch(eng, tmp_path, monkeypatch):
    log = tmp_path / "ql.jsonl"
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(log))
    tools.dispatch("lts_find", {"corpus": "qs", "semantic": "off", "where": [{"key": "tier", "has": True}]}, eng)
    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["filters"]["where"] == [{"key": "tier", "op": "has"}] and rec["total_matched"] == 3


# ============================================================ compile unit
def test_compile_returns_none_when_no_new_parameter_is_used():
    assert qs.compile_query() is None
    assert qs.compile_query(explain=False) is None
    assert qs.compile_query(explain=True) is not None


def test_description_headline_names_both_calls():
    """The architect 2026-10-05: an agent reads the top of a long description and stops, so the FIRST
    sentences must name the one call for a text question AND the one call for 'all of these words'."""
    from factlattice_engine import server
    head = next(t for t in server.TOOLS if t["name"] == "lts_find")["description"][:400]
    assert "FOR A TEXT QUESTION, CALL: lts_find(corpus=..., text=..., rank='relevance')" in head
    assert "`text` is ONE phrase" in head
    assert "FOR 'ALL OF THESE WORDS': lts_find(corpus=..., all_of=['w1', 'w2'], match='word')" in head
    assert "LTS_SEMANTIC_ON_MISS" in head          # the on_miss default's source stays in the lead too
