"""Reviewer helpers (U1a follow-up, Diamond 3b16d4d P2-1, P2-3, P3-1): the failure contract of EVERY helper verb.

THE RULE. On ANY failure, raised OR in-band, a helper's JSON carries `reason` (a fixed code from a CLOSED list, kebab-case)
and `error_class` (never null). It carries NO `error` key. Raw text appears only in `raw_error`, beside the NEVER-persist
note; a finding list that holds full paths is labelled `terminal_only`. Prose that explains a code lives in `explanation`
and is a fixed sentence (no interpolation), so it is path-free by CONSTRUCTION, not by a pattern over free text.

This file drives the failures for real (a missing file, an unknown corpus, a document outside its corpus, a registry
that differs) and only ADDITIONALLY by making a function raise. An enumerating test fails when a new verb is not
in COVERAGE.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CODE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
# directory names with spaces, both quote kinds, unicode, and all of them at once
PLANTS = {
    "space": "zq space dir",
    "apos": "zq it's",
    "dquote": 'zq "dq" dir',
    "unicode": "zq üñí秘 dir",
    "all": "zq all it's \"x\" üñí秘",
    "newline": "zqnl\nline dir",
    "backslash": "zqbs\\back dir",
    "codeshaped": "registry-not-found-zqrc",
}
MARKERS = ("zqnl", "zqbs", "zqrc", "zq space dir", "zq it's", 'zq "dq" dir', "zq üñí秘 dir", "zq all", "üñí秘", "it's", "dq")
TERMINAL_KEYS = ("raw_error", "terminal_only")


@pytest.fixture(params=sorted(PLANTS), ids=sorted(PLANTS))
def ws(request, tmp_path, monkeypatch):
    base = tmp_path / PLANTS[request.param]
    corp, out = base / "corp", base / "out"
    corp.mkdir(parents=True)
    out.mkdir()
    (corp / "a.lts").write_text("# D [D-1]\n\n## N [N-1]\n\n- status: active\n", encoding="utf-8")
    (base / "outside.lts").write_text("# O [O-1]\n", encoding="utf-8")
    reg = base / "reg.json"
    reg.write_text(json.dumps({"pubcorp": {"roots": [{"path": str(corp), "area": "specs"}], "domain_suffix": "p"}}))
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "st"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(reg))
    monkeypatch.chdir(tmp_path)
    return {"base": base, "corp": corp, "doc": corp / "a.lts", "outside": base / "outside.lts", "reg": reg,
            "missing": base / "no such" / "gone.lts", "tmp": tmp_path}


def run(argv, capsys):
    from factlattice_engine.reviewer.__main__ import main
    capsys.readouterr()
    rc = main(argv)
    cap = capsys.readouterr()
    try:
        body = json.loads(cap.out)
    except ValueError:
        body = None
    return rc, body, cap


def assert_failure_shape(body, label, expect_reason=None):
    assert isinstance(body, dict), (label, "stdout is not a JSON object")
    assert "error" not in body, (label, "an `error` key", sorted(body))
    reason = body.get("reason")
    assert isinstance(reason, str) and CODE_RE.fullmatch(reason), (label, "reason is not a fixed code", reason)
    assert isinstance(body.get("error_class"), str) and body["error_class"], (label, "error_class is null", body)
    if expect_reason is not None:
        assert reason == expect_reason, (label, reason)
    for k, v in body.items():
        if k in TERMINAL_KEYS:
            continue
        text = json.dumps(v, ensure_ascii=False)
        for m in MARKERS:
            assert m not in text, (label, k, m)
    if "raw_error" in body:
        assert "NEVER persist" in body.get("raw_error_note", ""), (label, body)
    if "terminal_only" in body:
        assert "terminal" in body.get("terminal_only_note", "") and "NEVER persist" in body["terminal_only_note"], (label, body)


# --------------------------------------------------------------------------------------------- the cases
def _mcp_mismatch(ws):
    return json.dumps({"source": "env", "path": str(ws["reg"]), "sha256_at_boot": "a" * 64, "sha256_now": "a" * 64})


def _bad_profile_registry(ws):
    """A registered profile whose raw text declares an `ext-id` the loader does not project as a registration."""
    prof = ws["base"] / "p-profile.lts"
    prof.write_text("---\ntitle: p\ndocument_tag: PROFILE\n---\n# p [PROFILE:P]\n\n## [PROFILE-DOMAIN] d\n\n"
                    "- domain-id: p\n\nprose that mentions\n- ext-id: GHOST\n", encoding="utf-8")
    ws["reg"].write_text(json.dumps({"pubcorp": {"roots": [{"path": str(ws["corp"]), "area": "specs"}],
                                                  "domain_suffix": "p", "profile": str(prof)}}))


def _gone_registry(ws, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(ws["base"] / "gone.json"))


# (verb, label, mode, builder(ws, monkeypatch) -> argv, expected rc, expected reason or None)
#   mode "real"    the failure is produced by real input; nothing is patched
#   mode "patched" a function is made to raise (reaches only `main`'s except clause)
CASES = [
    ("resolve-output", "document-outside-corpus", "real",
     lambda ws, mp: ["resolve-output", "--corpus", "pubcorp", "--document", str(ws["outside"])], 1,
     "document-not-under-corpus-root"),
    ("resolve-output", "registry-missing", "real",
     lambda ws, mp: (_gone_registry(ws, mp), ["resolve-output", "--corpus", "pubcorp", "--document", str(ws["doc"])])[1], 2,
     "registry-not-found"),
    ("resolve-output", "function-raises", "patched",
     lambda ws, mp: ["resolve-output", "--corpus", "pubcorp", "--document", str(ws["doc"])], 2, "helper-error"),
    ("check-extension-paths", "extension-is-the-document", "real",
     lambda ws, mp: ["check-extension-paths", "--document", str(ws["doc"]), "--extension", str(ws["doc"])], 1,
     "extension-is-the-reviewed-document"),
    ("new-key", "function-raises", "patched", lambda ws, mp: ["new-key", "--corpus", "pubcorp"], 2, "helper-error"),
    ("self-gate", "file-missing", "real", lambda ws, mp: ["self-gate", "--file", str(ws["missing"])], 1, "self-gate-failed"),
    ("self-gate", "registry-missing", "real",
     lambda ws, mp: (_gone_registry(ws, mp), ["self-gate", "--file", str(ws["doc"])])[1], 2, "registry-not-found"),
    ("check-registrations", "unknown-corpus", "real", lambda ws, mp: ["check-registrations", "--corpus", "nope"], 1,
     "registration-audit-failed"),
    ("check-registrations", "not-projected", "real",
     lambda ws, mp: (_bad_profile_registry(ws), ["check-registrations", "--corpus", "pubcorp"])[1], 1,
     "registrations-not-projected"),
    ("check-registrations", "registry-missing", "real",
     lambda ws, mp: (_gone_registry(ws, mp), ["check-registrations", "--corpus", "pubcorp"])[1], 2, "registry-not-found"),
    ("gen6-scan", "file-missing", "real", lambda ws, mp: ["gen6-scan", "--path", str(ws["missing"])], 2, "helper-error"),
    ("scan-secrets", "file-missing", "real", lambda ws, mp: ["scan-secrets", "--document", str(ws["missing"])], 2,
     "helper-error"),
    ("lint-groups", "document-missing", "real", lambda ws, mp: ["lint-groups", "--document", str(ws["missing"])], 2,
     "lint-incomplete"),
    ("dangles", "document-missing", "real",
     lambda ws, mp: ["dangles", "--corpus", "pubcorp", "--document", str(ws["missing"])], 2, "lint-incomplete"),
    ("dangles", "unknown-corpus", "real",
     lambda ws, mp: ["dangles", "--corpus", "nope", "--document", str(ws["doc"])], 2, "corpus-not-served"),
    ("dangles", "registry-missing", "real",
     lambda ws, mp: (_gone_registry(ws, mp), ["dangles", "--corpus", "pubcorp", "--document", str(ws["doc"])])[1], 2,
     "registry-not-found"),
    ("frame", "unknown-corpus", "real",
     lambda ws, mp: ["frame", "--corpus", "nope", "--source-file", "a.lts"], 2, "frame-read-incomplete"),
    ("frame", "registry-missing", "real",
     lambda ws, mp: (_gone_registry(ws, mp), ["frame", "--corpus", "pubcorp", "--source-file", "a.lts"])[1], 2,
     "registry-not-found"),
    ("verdict", "bad-json", "real", lambda ws, mp: ["verdict", "--args-json", "{"], 2, "helper-error"),
    ("evidence", "bad-json", "real", lambda ws, mp: ["evidence", "--args-json", "{"], 2, "helper-error"),
    ("registry-check", "mismatch", "real", lambda ws, mp: ["registry-check", "--mcp-json", _mcp_mismatch(ws)], 1,
     "registry-mismatch"),
    ("registry-check", "bad-json", "real", lambda ws, mp: ["registry-check", "--mcp-json", "{"], 2, "helper-error"),
    ("registry-check", "registry-missing", "real",
     lambda ws, mp: (_gone_registry(ws, mp), ["registry-check"])[1], 2, "registry-not-found"),
    ("no-document-reply", "not-a-stop", "real", lambda ws, mp: ["no-document-reply", "--reason", f"zq {ws['base']}"], 2,
     "invalid-argument"),
]

# every verb of the parser must be listed here: a NEW verb that is missing fails test_every_verb_is_covered
COVERAGE = {
    "resolve-output", "check-extension-paths", "new-key", "self-gate", "check-registrations", "gen6-scan", "scan-secrets",
    "lint-groups", "frame", "verdict", "evidence", "registry-check", "no-document-reply", "dangles",
}
# a verb whose only failure surface is "its function raised" (it reads nothing): the one allowed exception to "a real case"
PATCHED_ONLY = {"new-key"}


def uncovered(verbs, covered):
    return sorted(set(verbs) - set(covered))


def test_every_verb_is_covered():
    from factlattice_engine.reviewer.__main__ import verbs
    v = verbs()
    assert len(v) >= 14, v
    assert uncovered(v, COVERAGE) == [], "a helper verb with no failure coverage: add it to COVERAGE and CASES"
    assert uncovered(COVERAGE, v) == [], "COVERAGE names a verb that does not exist"
    assert uncovered(v, {c[0] for c in CASES}) == [], "every verb needs at least one CASE"


def test_the_enumeration_can_fail_on_a_new_verb():
    """Negative control: a verb added to the parser and not to COVERAGE is reported."""
    assert uncovered(["resolve-output", "brand-new-verb"], COVERAGE) == ["brand-new-verb"]


def test_every_verb_has_a_real_failure_case_unless_it_has_no_input_to_fail_on():
    real = {c[0] for c in CASES if c[2] == "real"}
    assert uncovered(COVERAGE - PATCHED_ONLY, real) == [], "these verbs are only tested by making a function raise"


def test_every_verb_has_a_driven_in_band_failure_where_one_exists():
    in_band = {"resolve-output", "check-extension-paths", "self-gate", "check-registrations", "lint-groups", "frame",
               "dangles", "registry-check", "no-document-reply"}
    reals = {c[0]: c for c in CASES if c[2] == "real" and c[5] not in ("helper-error", "registry-not-found")}
    assert uncovered(in_band, reals) == []


@pytest.mark.parametrize("case", CASES, ids=[f"{c[0]}:{c[1]}" for c in CASES])
def test_helper_failure_shape(ws, monkeypatch, capsys, case):
    verb, label, mode, build, rc_want, reason_want = case
    if mode == "patched":
        from factlattice_engine import reviewer
        fn = {"resolve-output": "resolve_output", "new-key": "review_key"}[verb]
        monkeypatch.setattr(reviewer, fn, lambda *a, **k: (_ for _ in ()).throw(
            OSError(f"[Errno 2] No such file: '{ws['base']}/y.lts'")))
    argv = build(ws, monkeypatch)
    rc, body, cap = run(argv, capsys)
    assert rc == rc_want, (verb, label, rc, cap.out[:300], cap.err[:300])
    assert_failure_shape(body, f"{verb}:{label}", reason_want)


def test_every_reason_emitted_is_in_the_closed_list():
    from factlattice_engine.reviewer.__main__ import HELPER_REASON_CODES
    assert all(CODE_RE.fullmatch(c) for c in HELPER_REASON_CODES)
    wanted = {c[5] for c in CASES}
    assert wanted <= set(HELPER_REASON_CODES), sorted(wanted - set(HELPER_REASON_CODES))


# --------------------------------------------------------------------------------------------- terminal-only findings
def test_check_extension_paths_labels_its_path_findings_terminal_only(ws, capsys):
    rc, body, _ = run(["check-extension-paths", "--document", str(ws["doc"]), "--extension", str(ws["doc"])], capsys)
    assert rc == 1 and "findings" not in body                         # the path-bearing list is not under a plain name
    assert body["count"] == 1 and body["kinds"] == ["extension_is_the_reviewed_document"]
    tf = body["terminal_only"]["findings"]
    assert tf and tf[0]["path"] == str(ws["doc"])                    # the terminal still shows the operator the paths
    assert "terminal" in body["terminal_only_note"] and "NEVER persist" in body["terminal_only_note"]
    assert_failure_shape(body, "cep")


def test_check_extension_paths_success_is_unchanged(ws, capsys):
    rc, body, _ = run(["check-extension-paths", "--document", str(ws["doc"]), "--extension", str(ws["outside"])], capsys)
    assert rc == 0 and body == {"findings": []}


# --------------------------------------------------------------------------------------------- resolve-output: codes + explanation
def _sc(**kw):
    from factlattice_engine.reviewer import SiteConfig
    return SiteConfig(**kw) if kw else SiteConfig.none()


def _refusals(plant):
    """Every refusal branch of resolve_output, each driven with `plant` inside the input that the OLD prose echoed."""
    from factlattice_engine.reviewer import Override, SiteConfig, resolve_output
    out = []
    # ambiguous: found names
    out.append(resolve_output("c", "/d/x.lts", None, None, SiteConfig.ambiguous([f"{plant}-a:X", f"{plant}-b:X"])))
    # relative default root, then an absolute root inside the corpus, then conflicting overrides
    out.append(resolve_output("c", "/d/x.lts", None, None, SiteConfig("ok", "ref", f"{plant}/relative", ["c"], [], [])))
    out.append(resolve_output("c", f"/z/{plant}/x.lts", {"roots": [{"path": f"/z/{plant}"}]}, None,
                              SiteConfig("ok", "ref", f"/z/{plant}/out", ["c"], [], [])))
    out.append(resolve_output("c", "/d/x.lts", None, None,
                              SiteConfig("ok", "ref", None, [], [Override("c", "", f"/a/{plant}/one"), Override("c", "", f"/b/{plant}/two")], [])))

    class P:
        review_output = f"{plant}/rel"
        def resolve_path(self, x): return None
    out.append(resolve_output("c", "/d/x.lts", None, P(), SiteConfig.none()))
    out.append(resolve_output("c", "/d/x.lts", None, None, SiteConfig("ok", "ref", f"/q/{plant}", [], [], [])))   # not listed
    out.append(resolve_output("c", "/d/x.lts", None, None, SiteConfig.none()))                                     # nothing at all
    out.append(resolve_output("c", "/elsewhere/x.lts", None, None,
                              SiteConfig("ok", "ref", None, [], [Override("c", f"sub/{plant}", "/o")], [])))      # outside + prefix overrides
    out.append(resolve_output("c", "/elsewhere/x.lts", {"roots": [{"path": f"/z/{plant}"}]}, None, SiteConfig.none()))  # outside the root
    return out


@pytest.mark.parametrize("plant", sorted(PLANTS.values()), ids=sorted(PLANTS))
def test_resolve_output_reasons_are_codes_and_explanations_are_fixed_sentences(plant):
    from factlattice_engine.reviewer import RESOLVE_REFUSALS
    seen = set()
    for r in _refusals(plant):
        assert r.root is None, r.reason
        assert CODE_RE.fullmatch(r.reason), r.reason
        assert r.reason in RESOLVE_REFUSALS, r.reason
        # path-free BY CONSTRUCTION: the explanation is the table's own fixed sentence, nothing interpolated into it
        assert r.explanation == RESOLVE_REFUSALS[r.reason], (r.reason, r.explanation)
        for m in MARKERS + (plant,):
            assert m not in r.reason and m not in r.explanation, (r.reason, m)
        d = r.to_dict()
        assert d["reason"] == r.reason and d["explanation"] == r.explanation
        seen.add(r.reason)
    assert len(seen) >= 8, sorted(seen)          # the branches really are distinct; a collapsed table would fail here


def test_the_explanations_table_has_no_interpolation_and_no_path_characters():
    from factlattice_engine.reviewer import RESOLVE_REFUSALS
    for code, text in RESOLVE_REFUSALS.items():
        assert CODE_RE.fullmatch(code)
        assert "{" not in text and "/" not in text and "\\" not in text and "'" not in text and '"' not in text, (code, text)


def test_ambiguous_site_config_names_go_to_terminal_only_not_reason(tmp_path):
    from factlattice_engine.reviewer import SiteConfig, resolve_output
    r = resolve_output("c", "/d/x.lts", None, None, SiteConfig.ambiguous(["conv-a:X", "conv-b:X"]))
    d = r.to_dict()
    assert r.reason == "ambiguous-site-config" and "conv-a" not in json.dumps({k: v for k, v in d.items() if k != "terminal_only"})
    assert d["terminal_only"] == {"site_config_found": ["conv-a:X", "conv-b:X"]} and "NEVER persist" in d["terminal_only_note"]


def test_a_resolved_root_has_no_reason_and_no_error_class(ws, capsys):
    from factlattice_engine.reviewer import Override, SiteConfig, resolve_output
    r = resolve_output("pubcorp", str(ws["doc"]), {"roots": [{"path": str(ws["corp"])}]}, None,
                       SiteConfig("ok", "ref", str(ws["base"] / "out"), ["pubcorp"], [], []))
    assert r.root and r.reason == "" and r.explanation == ""


# --------------------------------------------------------------------------------------------- other in-band prose
def test_registry_mismatch_reason_is_a_code_and_the_prose_is_a_fixed_explanation(ws, capsys):
    rc, body, _ = run(["registry-check", "--mcp-json", _mcp_mismatch(ws)], capsys)
    assert rc == 1 and body["mismatch"] is True and body["reason"] == "registry-mismatch"
    assert body["explanation"] and "/" not in body["explanation"] and "sha256" in body["explanation"]
    assert_failure_shape(body, "registry-check")


def test_registry_check_ok_has_no_reason_class_or_explanation(ws, capsys):
    rc, body, _ = run(["registry-check"], capsys)
    assert rc == 0 and body["mismatch"] is False and body["reason"] is None and "error_class" not in body


def test_not_projected_explanation_is_fixed_and_counts_are_fields(ws, monkeypatch, capsys):
    _bad_profile_registry(ws)
    rc, body, _ = run(["check-registrations", "--corpus", "pubcorp"], capsys)
    assert rc == 1 and body["reason"] == "registrations-not-projected" and body["raw_count"] >= 1 and body["reported"] == 0
    assert isinstance(body["explanation"], str) and not any(ch.isdigit() for ch in body["explanation"])
    assert_failure_shape(body, "not-projected")


def test_error_class_is_never_null_for_tool_text_failures_and_never_caller_text(ws, capsys):
    for argv in (["check-registrations", "--corpus", "ZqSentinelError: x"], ["frame", "--corpus", "ZqSentinelError: x", "--source-file", "a.lts"]):
        rc, body, _ = run(argv, capsys)
        assert body["error_class"] == "ToolError", (argv, body)
        assert "ZqSentinel" not in json.dumps({k: v for k, v in body.items() if k != "raw_error"})


def test_a_class_prefix_is_taken_only_from_the_exception_classes_that_exist():
    from factlattice_engine.reviewer.__main__ import _fail
    assert json.loads(_fail("PermissionError: [Errno 13] x", "registration-audit-failed"))["error_class"] == "PermissionError"
    assert json.loads(_fail("ZqSentinelError: x", "registration-audit-failed"))["error_class"] == "ToolError"
    assert json.loads(_fail("corpus not found: c", "frame-read-incomplete"))["error_class"] == "ToolError"
    assert json.loads(_fail(OSError("x")))["error_class"] == "OSError"


def test_lint_groups_incomplete_carries_a_code_and_the_class(ws, capsys):
    rc, body, _ = run(["lint-groups", "--document", str(ws["missing"])], capsys)
    assert rc == 2 and body["complete"] is False and body["reason"] == "lint-incomplete" and body["error_class"] == "InBand"


def test_lint_groups_ok_has_no_reason(ws, capsys):
    rc, body, _ = run(["lint-groups", "--document", str(ws["doc"])], capsys)
    assert body["complete"] is True and "reason" not in body and "error_class" not in body


def test_self_gate_failure_is_in_band_with_a_code(ws, capsys):
    rc, body, _ = run(["self-gate", "--file", str(ws["missing"])], capsys)
    assert rc == 1 and body["passed"] is False and body["reason"] == "self-gate-failed" and body["error_class"] == "InBand"
    assert body["checks"]                                              # the gate's own checks stay


def test_no_document_reply_failure_is_json_on_stdout_not_prose_on_stderr(ws, capsys):
    rc, body, cap = run(["no-document-reply", "--reason", "not-a-stop"], capsys)
    assert rc == 2 and body["reason"] == "invalid-argument" and body["error_class"] == "ValueError"
    assert cap.err == ""
    rc, ok, _ = run(["no-document-reply", "--reason", "no-route"], capsys)
    assert rc == 0 and ok is None                                     # the success reply stays the plain NO-REVIEW-DOCUMENT line


# --------------------------------------------------------------------------------------------- P3-1: error_persist
def _reply(persist):
    r = {"success": False, "reason": "registry-not-found",
         "error": "registry-not-found: the registry '/x/zq space/reg.json' does not exist"}
    if persist is not None:
        r["error_persist"] = persist
    return r


@pytest.mark.parametrize("verb,argv,tool", [
    ("check-registrations", ["check-registrations", "--corpus", "pubcorp"], "lts_profile_audit"),
    ("frame", ["frame", "--corpus", "pubcorp", "--source-file", "a.lts"], "lts_find"),
], ids=["check-registrations", "frame"])
def test_a_registry_stop_from_an_mcp_reply_prefers_the_replys_error_persist(ws, monkeypatch, capsys, verb, argv, tool):
    from factlattice_engine import tools
    real = tools.dispatch
    persist = "registry-not-found: the registry 'reg.json' does not exist"

    def fake(name, args, tools=None):
        return _reply(persist) if name == tool else real(name, args, tools=tools)
    monkeypatch.setattr(tools, "dispatch", fake)
    rc, body, _ = run(argv, capsys)
    assert rc == 2 and body["reason"] == "registry-not-found"
    assert body["detail"] == persist, body                            # the reply's own text, diagnosis kept
    assert "zq space" not in body["detail"] and "zq space" in body["raw_error"]
    assert body["error_class"] == "RegistryError"


@pytest.mark.parametrize("verb,argv,tool", [
    ("check-registrations", ["check-registrations", "--corpus", "pubcorp"], "lts_profile_audit"),
    ("frame", ["frame", "--corpus", "pubcorp", "--source-file", "a.lts"], "lts_find"),
], ids=["check-registrations", "frame"])
@pytest.mark.parametrize("persist", [None, 5, ""], ids=["absent", "not-a-string", "empty"])
def test_a_registry_stop_without_a_usable_error_persist_falls_back_to_the_fixed_sentence(ws, monkeypatch, capsys, verb, argv, tool, persist):
    from factlattice_engine import tools
    real = tools.dispatch

    def fake(name, args, tools=None):
        return _reply(persist) if name == tool else real(name, args, tools=tools)
    monkeypatch.setattr(tools, "dispatch", fake)
    rc, body, _ = run(argv, capsys)
    assert rc == 2 and body["detail"] == "registry-not-found: the registry could not be resolved (details are on the live reply only)", body
    assert "zq space" not in body["detail"]


# --------------------------------------------------------------------------------------------- the texts that state the rule
def test_the_texts_state_exactly_what_the_code_does():
    from factlattice_engine.reviewer.__main__ import HELPER_REASON_CODES
    core = (REPO / "factlattice_engine/reviewer/shipped/reviewer-core.lts").read_text(encoding="utf-8")
    sdlc = (REPO / "SDLC.md").read_text(encoding="utf-8")
    row = next(l for l in sdlc.splitlines() if l.startswith("- [ ] [TOOL-REASON-IS-A-FIXED-CODE]"))
    # the procedure names every code a helper can emit, so an agent is never handed a code its text does not know
    for code in HELPER_REASON_CODES:
        assert f"`{code}`" in core, code
    for needle in ("in-band", "InBand", "explanation", "terminal_only", "closed list", "NO `error` key"):
        assert needle in core, ("procedure", needle)
    for needle in ("in-band", "InBand", "explanation", "terminal_only", "HELPER_REASON_CODES", "success: true", "live_detail"):
        assert needle in row, ("SDLC row", needle)
    # the old over-wide sentences are gone
    assert "Every helper's `reason` is a FIXED CODE (`helper-error`" not in core
    assert "Every reviewer helper's `reason` is a fixed code with an `error_class`;" not in row
    assert "registry-mismatch: ..." not in core
    assert "begins `registry-mismatch`" not in core
    assert "`extension-invalid:registrations-not-projected`" in core        # the stop-reason the agent WRITES stays namespaced


# --------------------------------------------------------------------------------------------- negative controls
def test_the_shape_assertion_can_fail_on_planted_bad_bodies():
    """The shape predicate used by every case above must FIRE on a body that breaks the rule, or a green run proves nothing."""
    good = {"reason": "helper-error", "error_class": "OSError", "raw_error": "x zq space dir", "raw_error_note": "NEVER persist"}
    assert_failure_shape(good, "good")
    for bad in ({**good, "error": "x"}, {**good, "reason": "helper error: x"}, {**good, "error_class": None},
                {**good, "explanation": "the file zq space dir is missing"}, {**good, "detail": "it's here"},
                {"reason": "helper-error", "error_class": "OSError", "raw_error": "x"},
                {**good, "terminal_only": {"findings": [1]}}):
        with pytest.raises(AssertionError):
            assert_failure_shape(bad, "bad")


# --------------------------------------------------------------------------------------------- round 2: usage errors and crashes
USAGE_ARGVS = [["frame", "--corpus", "{p}"], ["registry-check", "--bogus", "{p}"], ["no-such-verb", "{p}"], [],
               ["resolve-output", "--document"], ["verdict", "--frame-exceeded", "{p}"]]


@pytest.mark.parametrize("argv", USAGE_ARGVS, ids=["missing-arg", "unknown-flag", "unknown-verb", "no-verb", "flag-no-value", "stray-positional"])
def test_a_usage_error_is_the_json_contract_on_stdout_with_nothing_on_stderr(ws, capsys, argv):
    plant = str(ws["base"])
    rc, body, cap = run([a.format(p=plant) for a in argv], capsys)
    assert rc == 2 and cap.err == "", (rc, cap.err[:200])
    assert_failure_shape(body, "usage", "usage-error")
    assert body["error_class"] == "UsageError" and body["raw_error"] and "NEVER persist" in body["raw_error_note"]


def test_help_is_not_a_failure(ws, capsys):
    from factlattice_engine.reviewer.__main__ import main
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0


def test_a_crash_at_the_final_print_is_the_contract_with_a_distinct_rc(ws, capsys):
    # a lone surrogate reaches the final print(json.dumps(..., ensure_ascii=False)) OUTSIDE the try (real input)
    rc, body, cap = run(["evidence", "--args-json", json.dumps({"node": "N", "key": "k", "line": 1, "text": "/x/zqnl \ud800 dir"}).replace("\\ud800", "\\ud800")], capsys)
    assert rc == 3, (rc, cap.out[:200], cap.err[:200])
    assert_failure_shape(body, "crash", "helper-crashed")
    assert body["error_class"] == "UnicodeEncodeError"
    assert cap.out.isascii(), "the crash JSON must be ensure_ascii so it cannot fail to encode"


def test_a_crash_in_gen6_scan_is_not_exit_1_which_means_hits(ws, monkeypatch, capsys):
    from factlattice_engine import reviewer
    monkeypatch.setattr(reviewer, "gen6_scan", lambda p: {"count": 1, "hits": ["\ud800"]})
    rc, body, _ = run(["gen6-scan", "--path", str(ws["doc"])], capsys)
    assert rc == 3 and body["reason"] == "helper-crashed", (rc, body)
    monkeypatch.setattr(reviewer, "gen6_scan", lambda p: {"count": 1, "hits": ["a plain hit"]})
    rc, body, _ = run(["gen6-scan", "--path", str(ws["doc"])], capsys)
    assert rc == 1 and body["count"] == 1                              # hits found stays 1


def test_any_exception_escaping_a_verb_branch_is_caught_by_main(ws, monkeypatch, capsys):
    from factlattice_engine.reviewer import __main__ as m
    monkeypatch.setattr(m, "_resolve", lambda *a, **k: (_ for _ in ()).throw(KeyError("zqnl boom")))
    # _resolve is wrapped by its own handler, so break something outside every handler: the final print's json.dumps
    monkeypatch.setattr(m, "_main", lambda argv=None: (_ for _ in ()).throw(MemoryError("zqnl")))
    rc, body, _ = run(["new-key"], capsys)
    assert rc == 3 and body["reason"] == "helper-crashed" and body["error_class"] == "MemoryError"


def test_the_new_codes_and_the_exit_statuses_are_documented():
    from factlattice_engine.reviewer import __main__ as m
    assert "usage-error" in m.HELPER_REASON_CODES and "helper-crashed" in m.HELPER_REASON_CODES
    assert "3" in m.__doc__ and "crash" in m.__doc__.lower()
    core = (REPO / "factlattice_engine/reviewer/shipped/reviewer-core.lts").read_text(encoding="utf-8")
    assert "helper-output-unreadable" in core and "not parseable JSON" in core and "exit status 3" in core
    for exempt in ("gen6-scan", "scan-secrets", "lint-groups"):
        assert exempt in core[core.index("helper-output-unreadable") - 900: core.index("helper-output-unreadable") + 900]


def test_main_has_no_silent_fall_through_to_resolve_output():
    src = (REPO / "factlattice_engine/reviewer/__main__.py").read_text(encoding="utf-8")
    assert 'args.cmd != "resolve-output"' in src and "has no branch" in src


# --------------------------------------------------------------------------------------------- round 3: the wrapper, exactly
def test_the_usage_error_print_is_guarded_like_the_crash_print(ws, monkeypatch, capsys):
    from factlattice_engine.reviewer import __main__ as m
    monkeypatch.setattr(m, "_fail", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broken _fail")))
    for argv in (["frame", "--corpus", "x"], ["new-key"]):
        if argv == ["new-key"]:
            monkeypatch.setattr(m, "_main", lambda a=None: (_ for _ in ()).throw(ValueError("boom")))
        rc = m.main(argv)                                              # must not raise a traceback
        assert rc in (2, 3)
    assert rc == 3


def test_a_closed_stdout_object_does_not_turn_a_usage_error_into_a_traceback(ws, monkeypatch):
    import io
    from factlattice_engine.reviewer import __main__ as m
    dead = io.StringIO()
    dead.close()
    monkeypatch.setattr("sys.stdout", dead)
    assert m.main(["frame", "--corpus", "x"]) == 2
    assert m.main(["frame"]) == 2


@pytest.mark.parametrize("raiser,cls", [
    (lambda: (_ for _ in ()).throw(SystemExit(1)), "SystemExit"),
    (lambda: (_ for _ in ()).throw(SystemExit("text exit")), "SystemExit"),
    (lambda: (_ for _ in ()).throw(GeneratorExit()), "GeneratorExit"),
])
def test_system_exit_nonzero_and_other_base_exceptions_are_crashes(ws, monkeypatch, capsys, raiser, cls):
    from factlattice_engine.reviewer import __main__ as m
    monkeypatch.setattr(m, "_main", lambda a=None: raiser())
    rc = m.main(["new-key"])
    body = json.loads(capsys.readouterr().out)
    assert rc == 3 and body["reason"] == "helper-crashed" and body["error_class"] == cls


def test_keyboard_interrupt_and_help_are_not_swallowed(ws, monkeypatch):
    from factlattice_engine.reviewer import __main__ as m
    monkeypatch.setattr(m, "_main", lambda a=None: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        m.main(["new-key"])
    monkeypatch.setattr(m, "_main", lambda a=None: (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        m.main(["new-key"])


def test_the_crash_reply_is_ensure_ascii_a_mutant_without_it_is_caught(ws, monkeypatch, capsys):
    """A raw lone surrogate in the exception TEXT must come out as parseable ASCII JSON, rc 3. With ensure_ascii=False the
    print itself fails to encode and stdout is empty: the mutant below proves this test can see that."""
    from factlattice_engine.reviewer import __main__ as m
    monkeypatch.setattr(m, "_main", lambda a=None: (_ for _ in ()).throw(ValueError("bad \ud800 text \u00fc\u79d8")))
    rc = m.main(["new-key"])
    out = capsys.readouterr().out
    assert rc == 3 and out.isascii() and json.loads(out)["reason"] == "helper-crashed"
    real_dumps = json.dumps
    monkeypatch.setattr(m.json, "dumps", lambda o, **k: real_dumps(o, **{**k, "ensure_ascii": False}))
    rc = m.main(["new-key"])
    out2 = capsys.readouterr().out
    assert rc == 3 and out2 == "", "the ensure_ascii=False mutant must print NOTHING (the test above would then fail)"


def test_the_main_docstring_says_exactly_what_holds():
    from factlattice_engine.reviewer import __main__ as m
    d = m.main.__doc__
    for needle in ("KeyboardInterrupt", "--help", "import-time", "closed or broken pipe", "helper-output-unreadable", "BaseException"):
        assert needle in d, needle
    assert "never lets" not in d and "Nothing escapes" not in d
    sdlc = (REPO / "SDLC.md").read_text(encoding="utf-8")
    assert "does NOT cover KeyboardInterrupt" in sdlc and "never lets an exception" not in sdlc


# --------------------------------------------------------------------------------------------- round 4
def test_the_site_overrides_missing_check_reason_is_a_code_with_the_ids_in_their_own_fields():
    from factlattice_engine.reviewer import SiteConfig, resolve_output
    plant = "zq it's \"x\" üñí秘"
    sc = SiteConfig("ok", f"{plant}-corp:REVIEWER-SITE-CONFIG@sha256:deadbeef", f"/o/{plant}", ["c"], [], [])
    r = resolve_output("c", "/d/x.lts", None, None, sc)
    assert r.root and r.rule_used == "default"
    (chk,) = r.checks
    assert chk["name"] == "site-overrides-missing" and chk["outcome"] == "errored"
    assert CODE_RE.fullmatch(chk["reason"]), chk["reason"]
    assert chk["site_config"] == sc.ref                                   # the id and the hash are a FIELD, not part of `reason`
    assert plant not in chk["reason"] and "deadbeef" not in chk["reason"]


def test_an_exception_whose_str_raises_still_gets_the_contract(ws, monkeypatch, capsys):
    from factlattice_engine.reviewer import __main__ as m

    class Hostile(Exception):
        def __str__(self):
            raise RuntimeError("no str for you")
    monkeypatch.setattr(m, "_main", lambda a=None: (_ for _ in ()).throw(Hostile()))
    rc = m.main(["new-key"])
    out = capsys.readouterr().out
    body = json.loads(out)
    assert rc == 3 and body["reason"] == "helper-crashed" and body["error_class"] == "Hostile" and out.isascii()
    assert "Hostile" in body["raw_error"]
