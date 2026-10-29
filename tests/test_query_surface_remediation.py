"""[FIND-QUERY-SURFACE] review remediation (P1-1, P2-1, P2-2, P2-3) at 6dc4a95.

Each test here is RED at 6dc4a95 (output kept with the internal build logs).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from factlattice_engine import config, tools
from tests import qs_fixture as F

ROOT = Path(__file__).resolve().parents[1]
CANARY = "CaNaRyZx91Qw"


@pytest.fixture()
def eng(tmp_path):
    e, _ = F.engine_a(tmp_path)
    return e


def _refusals(c):
    """Every refusal path that can see a caller value, each carrying the canary."""
    return [
        dict(all_of=[{"term": c, "match": "bogus"}]),
        dict(all_of=[{"term": c + " " + c, "match": "prefix"}]),
        dict(all_of=[c], match=c),
        dict(all_of=[c], match=[c]),
        dict(all_of=[{"term": c, c: c}]),
        dict(all_of=[{"term": [c]}]),
        dict(all_of=[[c]]),
        dict(all_of=c),
        dict(any_of=[{"term": "x", "match": c}]),
        dict(where=[{"key": c, "has": True}]),
        dict(where=[{"key": "ok", c: "x"}]),
        dict(where=[{"key": "ok", "has": c}]),
        dict(where=[{"key": "ok", "lacks": [c]}]),
        dict(where=[{"key": "ok", "equals": [c]}]),
        dict(where=[{"key": "ok", "equals": {"a": c}}]),
        dict(where=[{"key": "ok", "not_equals": {"a": c}}]),
        dict(where=[{"key": "ok", "one_of": [c, [c]]}]),
        dict(where=[{"key": "ok", "not_one_of": c}]),
        dict(where=[c]),
        dict(where=c),
        dict(fields=[c, 3]),
        dict(fields=[3, c]),
        dict(fields=c),
        dict(source_file=c),
        dict(source_file=[c]),
        dict(document_tag=[c]),
    ]


def _assert_clean(text):
    t = text.lower()
    assert CANARY.lower() not in t, text


@pytest.mark.parametrize("i", range(len(_refusals(CANARY))))
def test_refusal_text_and_log_never_carry_a_caller_value(eng, tmp_path, monkeypatch, i):
    log = tmp_path / "ql.jsonl"
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(log))
    monkeypatch.delenv("FACTLATTICE_QUERY_LOG_LITERALS", raising=False)
    kw = _refusals(CANARY)[i]
    for tool, extra in (("lts_find", {"semantic": "off"}), ("lts_find_in_prose", {"text": "status"}),
                        ("lts_aggregate", {"op": "count"})):
        r = tools.dispatch(tool, {"corpus": "qs", **extra, **kw}, eng)
        assert r["success"] is False, (tool, kw, r)
        _assert_clean(r["error"])
    _assert_clean(log.read_text())


def test_successful_calls_with_canary_values_leave_no_literal_in_the_log(eng, tmp_path, monkeypatch):
    log = tmp_path / "ql.jsonl"
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(log))
    monkeypatch.delenv("FACTLATTICE_QUERY_LOG_LITERALS", raising=False)
    tools.dispatch("lts_find", {"corpus": "qs", "semantic": "off", "document_tag": CANARY,
                                "all_of": [CANARY], "none_of": [{"term": CANARY}],
                                "where": [{"key": "kind", "equals": CANARY}]}, eng)
    tools.dispatch("lts_find", {"corpus": "qs", "semantic": "off", "source_file": "a.lts",
                                "where": [{"key": "kind", "has": True}]}, eng)
    txt = log.read_text()
    _assert_clean(txt)
    recs = [json.loads(l) for l in txt.splitlines()]
    assert recs[0]["filters"]["document_tag"]["len"] == len(CANARY) and "sha" in recs[0]["filters"]["document_tag"]
    assert recs[1]["filters"]["source_file"] == {"sha": recs[1]["filters"]["source_file"]["sha"], "len": 5}
    assert "a.lts" not in txt


def test_literals_flag_restores_document_tag_and_source_file(eng, tmp_path, monkeypatch):
    log = tmp_path / "ql.jsonl"
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(log))
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG_LITERALS", "1")
    tools.dispatch("lts_find", {"corpus": "qs", "semantic": "off", "document_tag": "SPEC", "source_file": "a.lts"}, eng)
    f = json.loads(log.read_text().splitlines()[-1])["filters"]
    assert f["document_tag"]["value"] == "SPEC" and f["source_file"]["value"] == "a.lts"


def test_refusal_messages_still_name_parameter_index_type_and_length(eng):
    r = eng.find(corpus="qs", semantic="off", all_of=["ok", {"term": "abc def ghi jk", "match": "prefix"}])
    assert r["error"] == "all_of[1]: prefix terms must be one word (got a 4-word str, 14 chars)"
    r = eng.find(corpus="qs", semantic="off", where=[{"key": "k", "equals": 5}])
    assert "where[0].equals" in r["error"] and "a int" in r["error"]


# ---------------------------------------------------------------- P2-1
def test_misspelled_key_warns_when_the_other_filters_match_nothing(eng):
    for kw in (dict(prefix="NOPE-"), dict(text="zzzz-no-such-text"), dict(since="2999-01-01"),
               dict(all_of=["zzzz-no-such-term"])):
        for cond in ({"key": "tierr", "lacks": True}, {"key": "tierr", "has": True},
                     {"key": "tierr", "equals": "x"}):
            r = eng.find(corpus="qs", semantic="off", where=[cond], **kw)
            assert r["total_matched"] == 0, kw
            assert r["warnings"] == ["no node passes the other filters, so nothing in scope carries "
                                     "'tierr'; check the filters and the spelling"], (kw, cond)
            assert r["query_interpreted"]["where"][0]["key_present_in_scope"] == 0


def test_a_real_key_with_an_empty_population_also_says_the_population_is_empty(eng):
    r = eng.find(corpus="qs", semantic="off", prefix="NOPE-", where=[{"key": "tier", "has": True}])
    assert "no node passes the other filters" in r["warnings"][0]


def test_non_empty_scope_warnings_unchanged(eng):
    r = eng.find(corpus="qs", semantic="off", prefix="PROC-", where=[{"key": "tierr", "lacks": True}])
    assert r["warnings"] == ["no node in scope carries 'tierr'; `lacks` therefore matches every node in "
                             "scope, so check the spelling"]


def test_empty_scope_warning_reaches_prose_and_aggregate(eng):
    p = eng.find_in_prose(text="status", corpus="qs", prefix="NOPE-", where=[{"key": "tierr", "has": True}])
    assert "no node passes the other filters" in p["warnings"][0]
    a = tools.dispatch("lts_aggregate", {"op": "count", "corpus": "qs", "prefix": "NOPE-",
                                         "where": [{"key": "tierr", "has": True}]}, eng)
    assert "no node passes the other filters" in a["warnings"][0]


# ---------------------------------------------------------------- P2-2
def _same_label_roots(tmp_path, areas=("decisions", "decisions")):
    r0, r1 = tmp_path / "r0", tmp_path / "r1"
    r0.mkdir()
    r1.mkdir()
    (r0 / "same.lts").write_text("# D0 [SPEC:S-D0]\n\n- status: operational\n\n## One [S-ONE]\n\n- status: operational\n", encoding="utf-8")
    (r1 / "same.lts").write_text("# D1 [SPEC:S-D1]\n\n- status: operational\n\n## Two [S-TWO]\n\n- status: operational\n", encoding="utf-8")
    cfg = {"s": {"roots": [{"path": str(r0), "area": areas[0]}, {"path": str(r1), "area": areas[1]}]}}
    return tools.LtsEngine(config.build_store(cfg))


def test_source_file_is_ambiguous_when_two_roots_share_the_area_label(tmp_path):
    e = _same_label_roots(tmp_path)
    for area in (None, "decisions"):
        kw = {"area": area} if area else {}
        r = e.find(corpus="s", source_file="same.lts", semantic="off", **kw)
        assert r["success"] is False and "ambiguous" in r["error"], (area, r)
        assert "same.lts" not in r["error"]


def test_source_file_still_resolves_when_area_leaves_one_root(tmp_path):
    e = _same_label_roots(tmp_path, areas=("decisions", "specs"))
    r = e.find(corpus="s", source_file="same.lts", area="decisions", semantic="off")
    assert r["success"] is True
    assert sorted(n["id"] for n in r["nodes"]) == ["S-D0", "S-ONE"]
    r = e.find(corpus="s", source_file="same.lts", area="specs", semantic="off")
    assert sorted(n["id"] for n in r["nodes"]) == ["S-D1", "S-TWO"]


# ---------------------------------------------------------------- P2-3
def test_sdlc_has_rules_for_the_three_uncovered_extension_points():
    s = (ROOT / "SDLC.md").read_text(encoding="utf-8")
    for tag in ("[FIND-QUERY-SURFACE-5]", "[FIND-QUERY-SURFACE-6]", "[FIND-QUERY-SURFACE-7]"):
        assert tag in s, tag
    rule = lambda n: [l for l in s.splitlines() if f"[FIND-QUERY-SURFACE-{n}]" in l][0]
    assert "word_fallback=False" in rule(5)
    assert "_freshness_key" in rule(6) and "LRU" in rule(6) and "count-bounded" in rule(6)
    assert "LTS_FIND_WORD_FALLBACK" in rule(7) and "the maintainer ruling" in rule(7) and "default off" in rule(7)
    assert "refusal" in rule(4) or "refusal" in s.split("[FIND-QUERY-SURFACE-4]")[1].split("\n")[0]


def test_every_internal_find_caller_passes_word_fallback_false():
    """The rule of [FIND-QUERY-SURFACE-5], enforced: every `.find(` call in the engine package that
    is not the tool dispatch passes word_fallback=False (so the env flag can never reach it)."""
    import re
    offenders = []
    for py in (ROOT / "factlattice_engine").rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        for m in re.finditer(r"(?:self|tools|t|eng)\.find\(", src):
            call = src[m.start(): m.start() + 400]
            if 'corpus=a.get("corpus")' in call[:40]:
                continue                                  # the tool dispatch door
            if "word_fallback=False" not in call:
                offenders.append(f"{py.name}: {call[:80]!r}")
    assert not offenders, offenders
