"""Round 4 (integration after fix/reviewer-helper-reasons landed): the new writers FOLLOW the reason-code rule.

  * top-level `reason` is the fixed code (== evidence.refusal); the path-free sentence is `explanation`; a path goes to
    `live_detail` with its NEVER-persist note; the two tools are NOT on the legacy exemption list
  * the ledger-failure warning is the landed `ledger.describe()` form, through the real adapter path
  * F-1 precedence, F-2 the code's real rule, F-3 the RED count, F-4 the incomplete-code set is pinned, F-5 multi-corpus wording
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.test_node_change_ledger import DOC as LG_DOC, _engine, env  # noqa: F401
from tests.test_tool_reason_codes import toplevel_reason_violation

REPO = Path(__file__).resolve().parent.parent
CODE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
PLANT = "zq it's \"x\" dir"
DOC = "# Doc [SPEC:RR-ROOT]\n\n## One [RR-ONE]\n\n- tag: a\n- tag: b\n- nested:\n  - x\n- see-also: [RR-TWO]\n- note: n\n\n## Two [RR-TWO]\n\n- kind: y\n"


@pytest.fixture()
def w(tmp_path, monkeypatch):
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "st"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.chdir(tmp_path)
    d = tmp_path / PLANT
    d.mkdir()
    f = d / "c.lts"
    f.write_text(DOC, encoding="utf-8")
    return d, f


def dispatch(name, args):
    from factlattice_engine import tools
    return tools.dispatch(name, args, tools=None)


def refusal_replies(d, f):
    ghost = str(d / "no such.lts")
    cases = []
    for tool, base in (("lts_rename_property", {"new_key": "label"}), ("lts_remove_property", {})):
        for extra in ({"path": ghost, "node_id": "RR-ONE", "key": "tag"},
                      {"path": str(f), "node_id": "RR-NOPE", "key": "tag"},
                      {"path": str(f), "node_id": "RR-ONE", "key": "absent"},
                      {"path": str(f), "node_id": "RR-ONE", "key": "tag"},
                      {"path": str(f), "node_id": "RR-ONE", "key": "nested"},
                      {"path": str(f), "node_id": "RR-ONE", "key": "see-also"},
                      {"path": str(f), "node_id": "RR-ONE", "key": "tag", "line": "x"},
                      {"path": str(f), "node_id": "RR-ONE", "key": "tag", "reason": "r" * 2001},
                      {"node_id": "RR-ONE", "key": "tag"}):                       # a missing `path`
            cases.append((tool, {**base, **extra}))
    return [(t, a, dispatch(t, dict(a))) for t, a in cases]


# ============================================================================ the rule
def test_the_new_writers_are_not_on_the_legacy_exemption_list():
    from factlattice_engine.reason_codes import TYPED_WRITERS
    assert TYPED_WRITERS == {"lts_add_node", "lts_add_property", "lts_repair_property"}


def test_every_refusal_reason_is_the_code_and_the_sentence_is_the_explanation(w):
    d, f = w
    codes = set()
    for tool, args, res in refusal_replies(d, f):
        assert res["success"] is False and res["refused"] is True, (tool, args, res)
        code = res["evidence"]["refusal"]
        assert res["reason"] == code and CODE_RE.fullmatch(code), (tool, args, res["reason"])
        assert isinstance(res["explanation"], str) and res["explanation"].strip() and res["explanation"] != code
        assert PLANT not in res["explanation"] and PLANT not in res["reason"], (tool, res["explanation"])
        assert toplevel_reason_violation(tool, res) is None, (tool, toplevel_reason_violation(tool, res))
        codes.add(code)
    assert {"bad_argument", "not_a_file", "node_not_found", "key_absent", "key_ambiguous", "key_nested_block",
            "edge_change_refused", "metadata_invalid"} <= codes, codes


def test_the_missing_argument_reply_is_a_code_with_an_explanation(w):
    out = dispatch("lts_rename_property", {"path": "x.lts", "node_id": "A", "key": "k"})
    assert out["reason"] == "bad_argument" and "new_key" in out["explanation"] and out["evidence"]["refusal"] == "bad_argument"


@pytest.mark.parametrize("cause", ["lock_unavailable", "write_refused"])
def test_a_path_bearing_refusal_has_a_fixed_explanation_and_the_text_in_live_detail(w, monkeypatch, cause):
    d, f = w
    if cause == "lock_unavailable":
        monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(d / "state-inside"))
    else:
        from factlattice.authoring import edit
        monkeypatch.setattr(edit, "_BEFORE_REPLACE_HOOK", lambda real: Path(real).write_bytes(Path(real).read_bytes() + b"\n"))
    out = dispatch("lts_rename_property", {"path": str(f), "node_id": "RR-ONE", "key": "note", "new_key": "note2"})
    assert out["refused"] and out["reason"] == out["evidence"]["refusal"] == cause
    assert PLANT not in out["explanation"] and PLANT not in out["reason"]
    assert ("state-inside" if cause == "lock_unavailable" else "changed on disk") in out["live_detail"]
    assert "never persisted" in out["live_detail_note"]
    from factlattice_engine import querylog
    assert PLANT not in json.dumps(querylog.build_record("lts_rename_property", {}, out, 1.0), ensure_ascii=False)


def test_the_landed_battery_accepts_the_new_writers_replies(w):
    from factlattice_engine import server, tools
    d, f = w
    bad = str(d / "no such.lts")
    for t in server.TOOLS:
        if t["name"] in ("lts_rename_property", "lts_remove_property"):
            for a in ({}, {"path": bad, "node_id": "N-1", "key": "k", "value": "v", "line": 1},
                      {"path": bad, "node_id": "N-1", "key": "k", "new_key": "k2"}):
                res = tools.dispatch(t["name"], dict(a), tools=None)
                assert toplevel_reason_violation(t["name"], res) is None, (t["name"], a, res.get("reason"))


# ============================================================================ the ledger-failure warning, real adapter path
@pytest.mark.parametrize("tool,args", [
    ("rename", {"node_id": "LG-ONE", "key": "kind", "new_key": "kind2"}),
    ("remove", {"node_id": "LG-ONE", "key": "kind"})])
@pytest.mark.parametrize("receipt", ["full", "compact"])
def test_the_ledger_failure_warning_is_the_landed_describe_form(env, tmp_path, monkeypatch, tool, args, receipt):
    from factlattice_engine import authoring
    sub = tmp_path / "corpus" / "sub"
    sub.mkdir(exist_ok=True)
    f = sub / "c.lts"
    f.write_text(LG_DOC, encoding="utf-8")
    e = _engine(tmp_path)
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "corpus" / "state"))     # a ledger inside a corpus root: refused
    fn = authoring.rename_property if tool == "rename" else authoring.remove_property
    out = fn(str(f), args["node_id"], args["key"], **({"new_key": args["new_key"]} if tool == "rename" else {}),
             store=e.store, receipt=receipt)
    assert out["written"] is True and out["ledger"]["status"] == "failed"
    msg = out["warnings"][0]
    assert msg.startswith("WRITTEN, BUT NOT LEDGERED (ledger-write-failed): "), msg
    assert "corpus root" in msg
    if receipt == "full":
        assert out["report"].startswith(msg)


# ============================================================================ F-1 .. F-5
def _sec():
    spec = (REPO / "SPEC.md").read_text(encoding="utf-8")
    return spec[spec.index("#### 4.6.6"):spec.index("### 4.7")]


def _descs():
    from factlattice_engine.server import TOOLS
    return {t["name"]: t["description"] for t in TOOLS if t["name"] in ("lts_rename_property", "lts_remove_property")}


def test_f1_precedence_and_f2_the_real_rule_are_stated():
    for text in [_sec(), *_descs().values()]:
        text = " ".join(text.split())
        assert "takes precedence over `edge-context-incomplete`" in text, text[:80]
        assert "built-in minting key in `key` or `new_key`" in text and "`[ID]` reference in the value" in text
        assert "edge-capable" not in text


def test_f5_the_multi_corpus_wording_is_correct():
    sec = _sec()
    assert "all five degrade" not in sec and "NOT applied: <corpus> (<code>)" in sec
    assert "other corpora's profiles are still applied" in sec


def test_f4_the_incomplete_code_set_is_pinned():
    from factlattice_engine import authoring
    src = (REPO / "factlattice_engine/authoring.py").read_text(encoding="utf-8")
    used = set(re.findall(r'"code": "([a-z_]+)"', src)) | set(re.findall(r'_EDGE_BUILTIN\.format\("([a-z_]+)"\)', src)) \
        | set(re.findall(r'builtin\("([a-z_]+)"\)', src))
    assert used == set(authoring.EDGE_CHECK_CODES), used ^ set(authoring.EDGE_CHECK_CODES)
    assert set(authoring.EDGE_INCOMPLETE_CODES) <= set(authoring.EDGE_CHECK_CODES)
    assert set(authoring.EDGE_CHECK_CODES) - set(authoring.EDGE_INCOMPLETE_CODES) == {"outside_every_corpus", "corpus_registers_no_profile"}
    sec = _sec()
    for c in authoring.EDGE_CHECK_CODES:
        assert f"`{c}`" in sec, c
        for name, d in _descs().items():
            assert f"`{c}`" in d, (name, c)


@pytest.mark.skipif(not (REPO / "docs" / "build-logs").is_dir(),
                    reason="docs/build-logs/ is not in a published snapshot (PUBLISH-EXCLUDE removes it)")
def test_f3_the_round_3b_red_count_is_corrected_in_the_report_and_the_log():
    rep = (REPO / "docs/build-logs/BUILD-REPORT-rename-remove-property-2026-10-08.md").read_text(encoding="utf-8")
    red = (REPO / "docs/build-logs/rename-remove-property-round3b-RED-2026-10-08.txt").read_text(encoding="utf-8")
    assert "7 fail, 7 pass" in rep and "7 failed, 7 passed" in red
    assert "added after the RED run" in rep
