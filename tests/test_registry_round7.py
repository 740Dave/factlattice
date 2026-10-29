"""U1a round 7: no reviewer helper hands out raw error text in its `reason`; a tool's top-level `reason` is a fixed code."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CODE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
PLANT = "zq space dir"


@pytest.fixture()
def w(tmp_path, monkeypatch):
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "st"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "corp").mkdir()
    (tmp_path / "corp" / "a.lts").write_text("# D [D-1]\n\n## N [N-1]\n\n- status: active\n", encoding="utf-8")
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps({"pubcorp": {"roots": [{"path": str(tmp_path / "corp"), "area": "specs"}], "domain_suffix": "p"}}))
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(reg))
    return tmp_path


def run(argv, capsys):
    from factlattice_engine.reviewer.__main__ import main
    capsys.readouterr()
    rc = main(argv)
    return rc, capsys.readouterr().out


def parse(out):
    try:
        return json.loads(out)
    except ValueError:
        return {}


# ---------------------------------------------------------------- P2-1: check-registrations and every helper
def test_check_registrations_reason_is_a_fixed_code_not_the_raw_audit_error(w, monkeypatch, capsys):
    from factlattice_engine import tools
    real = tools.dispatch

    def fake(name, args, tools=None):
        if name == "lts_profile_audit":
            return {"success": False, "error": f"PermissionError: [Errno 13] Permission denied: '/x/{PLANT}/p.lts'"}
        return real(name, args, tools=tools)
    monkeypatch.setattr(tools, "dispatch", fake)
    rc, out = run(["check-registrations", "--corpus", "pubcorp"], capsys)
    body = parse(out)
    assert body["ok"] is False
    assert body["reason"] == "registration-audit-failed" and PLANT not in body["reason"]
    assert body.get("error_class") == "PermissionError"
    assert "raw_error" in body and "NEVER persist" in body.get("raw_error_note", "")      # the raw text is kept, labelled


HELPERS = [
    ("gen6-scan", ["--path", "{f}"], "gen6_scan"),
    ("scan-secrets", ["--document", "{f}"], "scan_secrets"),
    ("verdict", ["--args-json", "{{}}"], "decide_verdict"),
    ("evidence", ["--args-json", '{{"node":"N","key":"k","line":1,"text":"t"}}'], "evidence"),
    ("self-gate", ["--file", "{f}"], "self_gate"),
    ("lint-groups", ["--document", "{f}"], "lint_groups"),
    ("dangles", ["--corpus", "pubcorp", "--document", "{f}"], "dangle_check"),
    ("resolve-output", ["--corpus", "pubcorp", "--document", "{f}"], "resolve_output"),
]


@pytest.mark.parametrize("cmd,argv,fn", HELPERS, ids=[h[0] for h in HELPERS])
def test_every_helper_failure_has_a_fixed_code_reason_and_no_path(w, monkeypatch, capsys, cmd, argv, fn):
    from factlattice_engine import reviewer
    f = w / "corp" / "a.lts"

    def boom(*a, **k):
        raise OSError(f"[Errno 2] No such file or directory: '/x/{PLANT}/y.lts'")
    monkeypatch.setattr(reviewer, fn, boom)
    rc, out = run([cmd] + [x.format(f=f) for x in argv], capsys)
    body = parse(out)
    assert rc != 0 and isinstance(body, dict) and body, (cmd, out[:200])
    assert PLANT not in str(body.get("reason", "")), (cmd, body)
    assert CODE_RE.fullmatch(str(body.get("reason"))), (cmd, body.get("reason"))
    assert body.get("error_class") == "OSError", (cmd, body)


def test_resolve_output_reasons_do_not_echo_site_config_paths():
    from factlattice_engine import reviewer
    from factlattice_engine.reviewer import Override, SiteConfig, resolve_output
    sc = SiteConfig("ok", "ref", f"{PLANT}/relative-root", ["c"], [], [])
    r = resolve_output("c", "/d/x.lts", None, None, sc)
    assert r.root is None and PLANT not in r.reason, r.reason
    sc2 = SiteConfig("ok", "ref", None, [], [Override("c", "", f"/a/{PLANT}/one"), Override("c", "", f"/b/{PLANT}/two")], [])
    r2 = resolve_output("c", "/d/x.lts", None, None, sc2)
    assert r2.root is None and PLANT not in r2.reason, r2.reason

    class P:
        review_output = f"{PLANT}/rel"
        def resolve_path(self, x): return None
    r3 = resolve_output("c", "/d/x.lts", None, P(), SiteConfig.none())
    assert r3.root is None and PLANT not in r3.reason, r3.reason


def test_the_procedure_says_helper_raw_errors_are_never_persisted():
    core = (REPO / "factlattice_engine/reviewer/shipped/reviewer-core.lts").read_text(encoding="utf-8")
    assert "raw_error" in core and "never persist" in core.lower()
    assert "registration-audit-failed" in core


# ---------------------------------------------------------------- P2-2: a tool's top-level reason is a fixed code
# NOTE: this battery keeps only `success is False` replies and sends no `text`. It is NOT the whole rule: the battery in
# tests/test_tool_reason_codes.py covers every reply that carries a `reason`, `success: true` included, with `text`.
def _battery(w):
    from factlattice_engine import server, tools
    eng = server.boot_engine(retry_delay_s=0.0, watch=False)
    bad = str(w / PLANT / "no such.lts")
    shapes = [{}, {"corpus": "nope"}, {"corpus": "pubcorp", "limit": -1}, {"corpus": "pubcorp", "node_id": "NOPE", "start_node": "NOPE"},
              {"path": bad, "node_id": "N-1", "key": "k", "value": "v", "new_key": "k2", "prefix": "N", "line": 1,
               "roots": [bad], "source": bad},
              {"corpus": "pubcorp", "profile": bad, "source": bad, "content": "# T [T-1]\n"}]
    out = []
    for t in server.TOOLS:
        for a in shapes:
            out.append((t["name"], a, tools.dispatch(t["name"], dict(a), tools=eng)))
    return out


def test_every_tool_refusal_reason_is_a_fixed_code_without_a_path(w):
    seen = 0
    for name, args, res in _battery(w):
        if not isinstance(res, dict) or res.get("success") is not False or "reason" not in res:
            continue
        seen += 1
        reason = res["reason"]
        if reason is None:                             # absent, not a code: nothing to persist
            continue
        ev = res.get("evidence")
        if isinstance(ev, dict) and "refusal" in ev:   # the typed writers: a sentence in `reason`, the CODE in evidence.refusal
            assert isinstance(ev["refusal"], str) and CODE_RE.fullmatch(ev["refusal"]), (name, args, ev)
            continue
        if res.get("refused") is True and name in ("lts_add_node", "lts_add_property", "lts_repair_property") \
                and not CODE_RE.fullmatch(str(reason)):
            # A typed writer's UNCODED refusal keeps its pinned reply shape (tests/test_add_node_noncreate_pin.py pins it
            # byte-for-byte), so `reason` is a sentence that may name a path. It is a LIVE-reply field only: the query log
            # must persist neither the sentence nor the path.
            from factlattice_engine import querylog
            rec = json.dumps(querylog.build_record(name, {}, res, 1.0), ensure_ascii=False)
            assert PLANT not in rec, (name, rec[:200])
            assert str(reason)[:20] not in rec and "error_code" not in rec, (name, rec[:200])
            continue
        assert isinstance(reason, str) and CODE_RE.fullmatch(reason) and "/" not in reason, (name, args, reason)
    assert seen >= 5, "the battery must really produce refusals that carry a reason"


def test_the_battery_reaches_the_rename_and_remove_writers_and_their_refusals_are_coded(w):
    """[RENAME-REMOVE-PROPERTY] The battery iterates `server.TOOLS`, but a shape that lacks a tool's required
    argument never gets past the missing-argument error, so a new writer could be in the table and not be
    tested. Shape 5 carries `new_key`; this proves the two new writers really refused through it, with a code."""
    got = {}
    for name, args, res in _battery(w):
        if name in ("lts_rename_property", "lts_remove_property") and isinstance(res, dict) and res.get("refused"):
            got[name] = res
    assert set(got) == {"lts_rename_property", "lts_remove_property"}, got
    for name, res in got.items():
        assert CODE_RE.fullmatch(res["evidence"]["refusal"]) and PLANT not in str(res["reason"]), (name, res)


def test_the_battery_can_fail_on_a_planted_bad_reply():
    """Negative control: a reply whose reason is message text would be caught by the same predicate."""
    bad = {"success": False, "reason": f"[Errno 2] No such file: '/x/{PLANT}/y'"}
    assert not CODE_RE.fullmatch(bad["reason"])


def test_the_query_log_does_not_persist_a_writers_sentence_as_a_code():
    from factlattice_engine import querylog
    rec = querylog.build_record("lts_add_property", {}, {"success": False, "refused": True, "reason": "refused",
                                                          "evidence": {"refusal": "lock_busy"}}, 1.0)
    assert rec["error_code"] == "lock_busy"
    rec2 = querylog.build_record("lts_add_property", {}, {"success": False, "refused": True,
                                                           "reason": f"{PLANT} is busy: another writer held its lock"}, 1.0)
    assert "error_code" not in rec2 and PLANT not in json.dumps(rec2)       # a sentence is never taken as the code


def test_the_sdlc_rule_and_the_comment_and_spec_note():
    sdlc = (REPO / "SDLC.md").read_text(encoding="utf-8")
    line = next((l for l in sdlc.splitlines() if l.startswith("- [ ] [TOOL-REASON-IS-A-FIXED-CODE]")), None)
    assert line and "fixed code" in line and "refused" in line and "evidence.refusal" in line
    ql = (REPO / "factlattice_engine/querylog.py").read_text(encoding="utf-8")
    assert "is reduced to its last component BEFORE" not in ql
    spec = (REPO / "SPEC.md").read_text(encoding="utf-8")
    assert "environment variable or the pointer names a directory" in spec or "an env or pointer path that is itself a directory" in spec


# ---------------------------------------------------------------- round 7b: no unlabelled raw text under `error`
def _only_raw_error_names_the_plant(body, label):
    assert "error" not in body, (label, sorted(body))
    for k, v in body.items():
        if k == "raw_error":
            continue
        assert PLANT not in json.dumps(v, ensure_ascii=False), (label, k)


@pytest.mark.parametrize("cmd,argv,fn", HELPERS, ids=[h[0] for h in HELPERS])
def test_7b_helper_failure_json_has_no_error_key_and_the_plant_only_in_raw_error(w, monkeypatch, capsys, cmd, argv, fn):
    from factlattice_engine import reviewer
    f = w / "corp" / "a.lts"
    monkeypatch.setattr(reviewer, fn, lambda *a, **k: (_ for _ in ()).throw(OSError(f"No such file: '/x/{PLANT}/y.lts'")))
    rc, out = run([cmd] + [x.format(f=f) for x in argv], capsys)
    body = parse(out)
    assert body.get("raw_error") and PLANT in body["raw_error"] and "NEVER persist" in body["raw_error_note"], (cmd, body)
    _only_raw_error_names_the_plant(body, cmd)


def test_7b_registry_stop_detail_is_persist_text_and_the_live_text_is_raw_error(w, monkeypatch, capsys):
    d = w / PLANT; d.mkdir()
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(d / "gone.json"))
    for argv in (["resolve-output", "--corpus", "c", "--document", "/x/d.lts"], ["registry-check"],
                 ["check-registrations", "--corpus", "c"]):
        rc, out = run(argv, capsys)
        body = parse(out)
        assert rc == 2 and body["reason"] == "registry-not-found", (argv, body)
        assert PLANT in body["raw_error"] and "NEVER persist" in body["raw_error_note"], (argv, body)       # the live text keeps the full path
        assert body["detail"].startswith("registry-not-found:") and "gone.json" in body["detail"], (argv, body)
        _only_raw_error_names_the_plant(body, argv[0])


def test_7b_registry_check_failure_paths_follow_the_same_shape(w, monkeypatch, capsys):
    from factlattice_engine import reviewer
    monkeypatch.setattr(reviewer, "compare_registries", lambda *a, **k: (_ for _ in ()).throw(OSError(f"/x/{PLANT}")))
    rc, out = run(["registry-check"], capsys)
    _only_raw_error_names_the_plant(parse(out), "registry-check")


def test_7b_no_consumer_reads_the_helper_error_key():
    for p in [REPO / "factlattice_engine/reviewer/shipped/reviewer-core.lts", REPO / "agents/factlattice-reviewer.md"]:
        t = p.read_text(encoding="utf-8")
        assert "its `error`" not in t and "the `error` field" not in t, p
