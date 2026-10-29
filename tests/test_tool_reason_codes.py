"""[TOOL-REASON-IS-A-FIXED-CODE] over EVERY reply shape that carries a top-level `reason`, success:true included
(Diamond 3b16d4d P2-2). The old battery kept only `success is False` and sent no `text`, so it could not see
`lts_semantic_find`, which answers `success: true, status: unavailable, reason: "weights_missing: <absolute path>"`."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from factlattice_engine.reason_codes import TYPED_WRITERS  # noqa: E402

CODE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
PLANT = "zq it's \"x\" üñí秘 dir"


@pytest.fixture()
def w(tmp_path, monkeypatch):
    base = tmp_path / PLANT
    (base / "corp").mkdir(parents=True)
    (base / "corp" / "a.lts").write_text("# D [D-1]\n\n## N [N-1]\n\n- status: active\n- what: tile map\n", encoding="utf-8")
    reg = base / "reg.json"
    reg.write_text(json.dumps({"pubcorp": {"roots": [{"path": str(base / "corp"), "area": "specs"}], "domain_suffix": "p"}}))
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "st"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(reg))
    monkeypatch.chdir(tmp_path)
    return base


def _engine(w):
    from factlattice_engine import server
    from factlattice_engine.semantic import suggest as S
    eng = server.boot_engine(retry_delay_s=0.0, watch=False)
    # a model root that holds no weights: the deterministic "weights missing" state, whatever is in the real home
    eng.semantic = S.SemanticService(state_root=str(w.parent / "semstate"), model_root=str(w / "no models here"),
                                     build_mode="sync")
    return eng


def _shapes(w):
    bad = str(w / "no such.lts")
    return [{}, {"corpus": "pubcorp", "text": "tile map"}, {"corpus": "pubcorp", "text": f"{PLANT} tile map", "k": 3},
            {"corpus": "nope", "text": "tile map"}, {"text": f"{PLANT}"},
            {"corpus": "pubcorp", "limit": -1}, {"path": bad, "node_id": "N-1", "key": "k", "value": "v", "prefix": "N", "line": 1},
            {"corpus": "pubcorp", "profile": bad, "source": bad, "content": "# T [T-1]\n", "text": "zero"}]


def _replies(w):
    from factlattice_engine import server, tools
    eng = _engine(w)
    out = []
    for t in server.TOOLS:
        for a in _shapes(w):
            out.append((t["name"], a, tools.dispatch(t["name"], dict(a), tools=eng)))
    return out


def toplevel_reason_violation(name, res):
    """None, or why the TOP-LEVEL `reason` of `res` (a reply of tool `name`) is not acceptable. The ONLY skip: a TYPED WRITER's reply
    that carries a coded `evidence.refusal`, or says `refused: true` (its pinned sentence)."""
    if not isinstance(res, dict) or "reason" not in res or res["reason"] is None:
        return None
    ev = res.get("evidence")
    if name in TYPED_WRITERS and isinstance(ev, dict) and "refusal" in ev:
        return None if CODE_RE.fullmatch(str(ev["refusal"])) else f"evidence.refusal is not a code: {ev['refusal']!r}"
    if name in TYPED_WRITERS and res.get("refused") is True and not CODE_RE.fullmatch(str(res["reason"])):
        return None
    r = res["reason"]
    if not (isinstance(r, str) and CODE_RE.fullmatch(r)) or "/" in r or PLANT in r:
        return f"not a fixed code: {r!r}"
    return None


def test_every_top_level_reason_is_a_fixed_code_whatever_success_says(w):
    seen_true = seen_false = 0
    for name, args, res in _replies(w):
        assert toplevel_reason_violation(name, res) is None, (name, args, res.get("reason"))
        if isinstance(res, dict) and res.get("reason") is not None and not (name in TYPED_WRITERS and res.get("refused") is True):
            seen_true += res.get("success") is True
            seen_false += res.get("success") is False
    assert seen_false >= 5, "the battery must produce refusals that carry a reason"
    assert seen_true >= 1, "the battery must reach a success:true reply that carries a reason (lts_semantic_find unavailable)"


def test_the_battery_can_see_the_semantic_reply(w):
    """Negative control: the battery really reaches the unavailable reply, and a path-bearing reason would fail it."""
    from factlattice_engine import tools
    r = tools.dispatch("lts_semantic_find", {"corpus": "pubcorp", "text": "tile map"}, tools=_engine(w))
    assert r["success"] is True and r["status"] == "unavailable" and "reason" in r, r
    assert not CODE_RE.fullmatch("weights_missing: /x/y")             # the OLD reply would have been rejected by the predicate


def test_semantic_find_unavailable_has_a_code_and_the_path_only_in_a_live_only_field(w):
    from factlattice_engine import tools
    r = tools.dispatch("lts_semantic_find", {"corpus": "pubcorp", "text": "tile map"}, tools=_engine(w))
    assert r["success"] is True and r["status"] == "unavailable"
    assert r["reason"] == "weights-missing", r["reason"]
    assert str(w) in r["live_detail"] and "no models here" in r["live_detail"]          # the operator still sees the path, live
    assert "live" in r["live_detail_note"] and "NEVER persist" in r["live_detail_note"]
    for k, v in r.items():
        if k in ("live_detail", "query"):
            continue
        assert PLANT not in json.dumps(v, ensure_ascii=False), k


def test_the_query_log_carries_neither_the_code_as_an_error_nor_the_path(w, tmp_path):
    from factlattice_engine import querylog
    r = {"success": True, "status": "unavailable", "reason": "weights-missing", "live_detail": str(w), "candidates": []}
    rec = json.dumps(querylog.build_record("lts_semantic_find", {"corpus": "pubcorp"}, r, 1.0), ensure_ascii=False)
    assert PLANT not in rec and "error_code" not in rec


@pytest.mark.parametrize("make,code", [
    (lambda S: S.SemanticUnavailable("runtime_missing", "numpy not importable at /a/zq it's"), "runtime-missing"),
    (lambda S: S.SemanticUnavailable("pin_mismatch", "sha differs for /a/zq it's"), "pin-mismatch"),
    (lambda S: S.SemanticUnavailable("shared_embedder_unavailable", "socket /a/zq it's"), "shared-embedder-unavailable"),
])
def test_every_semantic_unavailable_status_becomes_a_code(w, monkeypatch, make, code):
    from factlattice_engine import tools
    from factlattice_engine.semantic import embedder
    eng = _engine(w)
    monkeypatch.setattr(eng.semantic, "_get_embedder", lambda: (_ for _ in ()).throw(make(embedder)))
    r = tools.dispatch("lts_semantic_find", {"corpus": "pubcorp", "text": "tile map"}, tools=eng)
    assert r["success"] is True and r["reason"] == code and "zq it's" in r["live_detail"] and "zq it's" not in r["reason"], r


def test_a_service_fault_is_a_code_with_the_class_and_a_live_only_detail(w, monkeypatch):
    from factlattice_engine import tools
    eng = _engine(w)
    monkeypatch.setattr(eng.semantic, "suggest", lambda *a, **k: (_ for _ in ()).throw(OSError(f"disk at {w}")))
    r = tools.dispatch("lts_semantic_find", {"corpus": "pubcorp", "text": "tile map"}, tools=eng)
    assert r["success"] is True and r["status"] == "unavailable" and r["reason"] == "semantic-service-fault", r
    assert r["error_class"] == "OSError" and str(w) in r["live_detail"] and str(w) not in r["reason"]


def test_the_sdlc_rule_covers_success_replies_and_names_the_live_only_field():
    sdlc = (Path(__file__).resolve().parent.parent / "SDLC.md").read_text(encoding="utf-8")
    row = next(l for l in sdlc.splitlines() if l.startswith("- [ ] [TOOL-REASON-IS-A-FIXED-CODE]"))
    assert "success: true" in row and "live_detail" in row and "text" in row


# --------------------------------------------------------------------------------------------- rounds 2-3: NESTED blocks
# RE-RULED (c): the TOP-LEVEL reason, and the reason of any refusal/fault block, is a fixed code (the typed writers excepted);
# a NESTED INFORMATIONAL reason is a code OR a registered constant sentence (reason_codes.info_reasons()).
def walk_reasons(obj, path="$"):
    """Every (path, dict) at ANY depth that carries a `reason` key."""
    if isinstance(obj, dict):
        if "reason" in obj:
            yield path, obj
        for k, v in obj.items():
            yield from walk_reasons(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from walk_reasons(v, f"{path}[{i}]")


def _is_fault_block(d):
    return d.get("success") is False or d.get("healed") is False or "error_class" in d or "live_detail" in d



def nested_reason_violations(res, tool=""):
    from factlattice_engine.reason_codes import TYPED_WRITERS, info_reasons, is_code
    info = info_reasons()
    bad = []
    for where, d in walk_reasons(res):
        r = d["reason"]
        if r is None or r == "":
            continue
        if where == "$" and tool in TYPED_WRITERS:
            ev = d.get("evidence")
            if d.get("refused") is True or (isinstance(ev, dict) and "refusal" in ev):
                continue                                          # the ONLY exemption: the typed writers, top level
        if is_code(r):
            continue
        if where != "$" and not _is_fault_block(d) and r in info:
            continue                                              # a registered constant informational sentence
        bad.append((where, r))
    return bad


def test_the_nested_predicate_can_fail():
    assert nested_reason_violations({"a": [{"b": {"reason": "OSError: disk at /x"}}]}) == [("$.a[0].b", "OSError: disk at /x")]
    assert nested_reason_violations({"a": {"reason": "weights-missing"}}) == []
    # the exemption is the typed writers only: the same refused sentence from any other tool is a violation
    refused = {"success": False, "refused": True, "reason": "a sentence about /x/y"}
    assert nested_reason_violations(refused, "lts_preview") == [("$", "a sentence about /x/y")]
    assert nested_reason_violations(refused, "lts_add_property") == []
    # a registered sentence is fine as an informational leaf, never in a fault block or at the top
    from factlattice_engine.reason_codes import NAMED_CORPUS_REASON
    assert nested_reason_violations({"x": {"reason": NAMED_CORPUS_REASON}}) == []
    assert nested_reason_violations({"x": {"reason": NAMED_CORPUS_REASON, "success": False}}) != []
    assert nested_reason_violations({"reason": NAMED_CORPUS_REASON}) != []
    # an UNREGISTERED sentence, and a registered one with something interpolated into it, are both violations
    assert nested_reason_violations({"x": {"reason": "private: name it explicitly /tmp/zq"}}) != []


@pytest.fixture()
def wide(w, tmp_path, monkeypatch):
    """A registry with a PRIVATE corpus (named -personality) next to the public one, and a way to make an index stale."""
    base = w
    (base / "priv").mkdir()
    (base / "priv" / "p.lts").write_text("# P [P-1]\n\n## N [PN-1]\n\n- status: active\n- what: tile map\n", encoding="utf-8")
    reg = base / "reg.json"
    reg.write_text(json.dumps({"pubcorp": {"roots": [{"path": str(base / "corp"), "area": "specs"}], "domain_suffix": "p"},
                               "zq-personality": {"roots": [{"path": str(base / "priv"), "area": "specs"}], "domain_suffix": "q"}}))
    return base


def _stale_and_heal_failing(w, eng, monkeypatch):
    """Make pubcorp stale (a file appears after boot) and make the healer raise: the heal block says it refused."""
    (w / "corp" / "late.lts").write_text("# L [L-1]\n\n## N [LN-1]\n\n- status: active\n", encoding="utf-8")
    if getattr(eng, "healer", None) is not None:
        monkeypatch.setattr(eng.healer, "_heal_locked", lambda *a, **k: (_ for _ in ()).throw(OSError(f"cannot read {w}/corp")))


def test_the_widened_battery_reaches_and_accepts_the_registered_informational_reasons(wide, monkeypatch):
    from factlattice_engine import server, tools
    from factlattice_engine.reason_codes import info_reasons
    eng = _engine(wide)
    _stale_and_heal_failing(wide, eng, monkeypatch)
    shapes = [{"corpus": "pubcorp", "text": "tile map"}, {"text": "tile map"}, {"text": "tile map", "semantic": "on_miss"},
              {"text": "no such phrase anywhere", "semantic": "on_miss"},                        # on_miss with NO corpus
              {"corpus": "zq-personality", "text": "tile map"}, {"corpus": "zq-personality"}, {"corpus": "pubcorp"}, {}]
    seen = set()
    for t in server.TOOLS:
        for a in shapes:
            res = tools.dispatch(t["name"], dict(a), tools=eng)
            assert nested_reason_violations(res, t["name"]) == [], (t["name"], a)
            assert heal_explanation_violations(res) == [], (t["name"], a)
            seen |= {d["reason"] for where, d in walk_reasons(res) if where != "$" and d["reason"] in info_reasons()}
            seen |= {d["reason"] for where, d in walk_reasons(res) if where == "$.heal"}
    from factlattice_engine.reason_codes import NAMED_CORPUS_REASON
    from factlattice_engine.privacy import PRIVATE_REASON
    assert NAMED_CORPUS_REASON in seen, "the battery must reach did_you_mean_semantic without a corpus"
    assert PRIVATE_REASON in seen, "the battery must reach extent.corpora_skipped (a private corpus skipped)"
    assert "self-heal-raised" in seen, "the battery must reach heal.reason (a stale index whose heal fails)"


def test_the_battery_fails_on_a_planted_interpolated_nested_reason(wide, monkeypatch):
    """Negative control: make a nested informational reason interpolate a path, and the battery must catch it."""
    from factlattice_engine import tools
    eng = _engine(wide)
    monkeypatch.setattr(tools, "_private_skips", lambda pc: [{"reason": f"private: {wide} name it explicitly", "count": 1}])
    seen = []
    for a in ({"text": "tile map"}, {"text": "tile map", "semantic": "on_miss"}):
        res = tools.dispatch("lts_find", dict(a), tools=eng)
        seen += nested_reason_violations(res, "lts_find")
    assert seen and str(wide) in seen[0][1], seen


def test_the_on_miss_fallback_reason_is_a_code_with_a_live_only_detail(w, monkeypatch):
    from factlattice_engine import tools
    eng = _faulty_engine(w, monkeypatch)
    r = tools.dispatch("lts_find", {"corpus": "pubcorp", "text": "no such phrase anywhere", "semantic": "on_miss"}, tools=eng)
    blk = r["did_you_mean_semantic"]
    assert blk["status"] == "unavailable" and blk["reason"] == "semantic-service-fault", blk
    assert blk["error_class"] == "OSError" and str(w) in blk["live_detail"] and "NEVER persist" in blk["live_detail_note"]
    assert nested_reason_violations(r, "lts_find") == []


def _faulty_engine(w, monkeypatch):
    eng = _engine(w)
    monkeypatch.setattr(eng.semantic, "suggest", lambda *a, **k: (_ for _ in ()).throw(OSError(f"disk at {w}/x.bin")))
    return eng


def test_the_battery_walks_nested_blocks_under_an_injected_fault(w, monkeypatch):
    from factlattice_engine import server, tools
    eng = _faulty_engine(w, monkeypatch)
    seen_nested = 0
    shapes = [{"corpus": "pubcorp", "text": f"{PLANT} nothing", "semantic": "on_miss"},
              {"corpus": "pubcorp", "text": "tile map", "semantic": "on_miss", "prefix": "NOPE-"},
              {"corpus": "pubcorp", "text": "tile map"}, {"corpus": "pubcorp"}, {}]
    for t in server.TOOLS:
        for a in shapes:
            res = tools.dispatch(t["name"], dict(a), tools=eng)
            seen_nested += sum(1 for where, _ in walk_reasons(res) if where != "$")
            assert nested_reason_violations(res, t["name"]) == [], (t["name"], a)
    assert seen_nested >= 1, "the battery must really reach a nested reason"


def test_the_sdlc_rule_states_the_re_ruled_wording():
    sdlc = (Path(__file__).resolve().parent.parent / "SDLC.md").read_text(encoding="utf-8")
    row = next(l for l in sdlc.splitlines() if l.startswith("- [ ] [TOOL-REASON-IS-A-FIXED-CODE]"))
    for needle in ("TOP-LEVEL", "refusal or fault block", "NESTED INFORMATIONAL", "info_reasons()", "CONSTANT sentence",
                   "never built from caller input", "typed writers only", "corpus id", "folder"):
        assert needle in row, needle
    assert "is the only exemption" not in row and "walked, and the writer exception below is the only" not in row


def test_the_top_level_battery_skips_refused_replies_for_the_typed_writers_only():
    """BEHAVIOURAL: feed the battery's own predicate a `refused: true` prose reply under different tool names."""
    prose = {"success": False, "refused": True, "reason": "refused: another writer held the lock for /x/zq dir"}
    for non_writer in ("lts_preview", "lts_find", "lts_lint", "lts_semantic_find", "some_future_tool"):
        assert toplevel_reason_violation(non_writer, prose) is not None, non_writer      # REPORTED
    for writer in sorted(TYPED_WRITERS):
        assert toplevel_reason_violation(writer, prose) is None, writer                  # the documented exception
    # a writer's refusal with a non-code evidence.refusal is still a violation
    assert toplevel_reason_violation("lts_add_node", {**prose, "evidence": {"refusal": "not a code!"}}) is not None
    assert nested_reason_violations(prose, "lts_preview") != []


def heal_explanation_violations(res):
    """Every dict that looks like a heal block must carry a `reason` code and an `explanation` that is one of the fixed sentences."""
    from factlattice_engine.selfheal import HEAL_REASONS
    return [(w, d.get("explanation")) for w, d in _walk_all(res) if "healed" in d and "explanation" in d
            and d["explanation"] not in HEAL_REASONS]


def _walk_all(obj, path="$"):
    if isinstance(obj, dict):
        yield path, obj
        for k, v in obj.items():
            yield from _walk_all(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk_all(v, f"{path}[{i}]")


def test_heal_explanation_is_a_fixed_sentence_and_the_interpolation_mutant_is_caught(wide, monkeypatch):
    from factlattice_engine import tools
    from factlattice_engine.selfheal import HealOutcome
    eng = _engine(wide)
    _stale_and_heal_failing(wide, eng, monkeypatch)
    r = tools.dispatch("lts_find", {"corpus": "pubcorp", "text": "tile map"}, tools=eng)
    assert r["heal"]["explanation"] and heal_explanation_violations(r) == []
    # the MUTANT: an explanation that interpolates the error (a path) is reported by the same predicate
    real = HealOutcome.to_dict

    def mutant(self):
        d = real(self)
        d["explanation"] = f"{self.reason}: {self.error}"
        return d
    monkeypatch.setattr(HealOutcome, "to_dict", mutant)
    r2 = tools.dispatch("lts_find", {"corpus": "pubcorp", "text": "tile map"}, tools=eng)
    assert heal_explanation_violations(r2) != [], r2["heal"]


def test_a_failed_heal_block_under_a_refusal_reply_has_a_code_and_the_sentence_in_explanation(wide, monkeypatch):
    from factlattice_engine import tools
    eng = _engine(wide)
    _stale_and_heal_failing(wide, eng, monkeypatch)
    r = tools.dispatch("lts_find", {"corpus": "pubcorp", "text": "tile map"}, tools=eng)
    assert r.get("success") is False and r["heal"]["healed"] is False, r
    assert CODE_RE.fullmatch(r["heal"]["reason"]), r["heal"]["reason"]
    assert r["heal"]["reason"] == "self-heal-raised" and "fail-closed" in r["heal"]["explanation"]
    assert nested_reason_violations(r, "lts_find") == []
    # the fault-block rule: a heal block that says healed=false is held to a code even though it has no success key of its own
    assert nested_reason_violations({"heal": {"healed": False, "reason": "self-heal raised; refusing (fail-closed)"}}, "lts_find") != []
