"""Spec 39: normative content, source statements and confirmations.

One test (or a small group) per scenario of specs/39-normative-and-source-statements.lts. Each was run
and observed failing on the spec-only commit before any engine code existed (the land record quotes the
failing lines). Parser tests assert the minted graph, not merely that a property parsed.

SCEN-NS-COUNT-EXCLUDES stays pending until scenario detection (rule N4) exists, and
SCEN-NS-AUDIT-READS-STATEMENTS is checked by a spec audit, not by this file.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from factlattice import core_keys
from factlattice.parser import CORE_EDGE_LAYER, REL_PROPERTY_EDGES, parse_text
from factlattice.profile import ProfileCollisionError, load_profile
from factlattice_engine import authoring, binding, config
from factlattice_engine.lint import lint
from factlattice_engine.tools import LtsEngine

ROOT = Path(__file__).resolve().parent.parent

NEW_KEYS = ("normative", "source-text", "source-locator", "source-kind", "source-role",
            "source-option-author", "restates", "confirmation-of", "checked-against",
            "confirmation-verdict", "confirmation-by", "confirmation-date",
            "confirmation-locator", "revised-per")


def doc(body: str, *, strict: bool = True, doctype: str = "SPEC", fm: str = "") -> str:
    head = "---\n" + ('lts: "1.1"\n' if strict else "") + "title: T\n" + fm + "---\n\n"
    return head + f"# T [{doctype}:T-ROOT]\n\n" + body


def stmt(nid, text="the words said here", extra=""):
    return (f"## Statement [{nid}]\n\n"
            + f"- source-text: {text}\n- source-locator: transcript line 1\n"
              f"- source-kind: typed\n- source-role: stakeholder\n{extra}\n")


def node(nid, *lines, head="##"):
    return f"{head} Node [{nid}]\n\n" + "".join(f"- {ln}\n" for ln in lines) + "\n"


def conf(nid, target, checked, verdict="confirmed", date="2026-11-15", extra=""):
    return (f"## Confirmation [{nid}]\n\n- confirmation-of: [{target}]\n- checked-against: {checked}\n"
            f"- confirmation-verdict: {verdict}\n- confirmation-by: reviewer\n"
            f"- confirmation-date: {date}\n- confirmation-locator: transcript line 9\n{extra}\n")


def props(text, source="t.lts"):
    return {n.id: dict(n.properties) for n in parse_text(text, source).nodes}


def eff(text):
    return {k: v.get("normative_effective") for k, v in props(text).items()}


def lint_text(tmp_path, text, name="d.lts"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return lint(str(p))


def found(res, kind, node_id=None):
    return [f for f in res.findings if f.kind == kind and (node_id is None or f.node_id == node_id)]


def engine(tmp_path, text):
    root = tmp_path / "corp"
    root.mkdir(exist_ok=True)
    (root / "00-d.lts").write_text(text, encoding="utf-8")
    return LtsEngine(config.build_store({"c": {"roots": [{"path": str(root)}]}}))


# ----------------------------------------------------------------- R1 the mark and the state
def test_absent_default_every_node_is_normative():  # SCEN-NS-ABSENT-DEFAULT
    e = eff(doc(node("N-A", "status: x") + node("N-B", "status: y")))
    assert e and set(e.values()) == {"true"}
    assert "T-ROOT" in e


def test_inherit_down_the_section_chain_and_find_filters(tmp_path):  # SCEN-NS-INHERIT
    body = (node("SEC-OUT", "normative: false")
            + node("SEC-MID", "status: a", head="###")
            + node("SEC-DEEP", "status: b", head="####")
            + node("SEC-SIB", "status: c"))
    text = doc(body)
    e = eff(text)
    assert (e["SEC-OUT"], e["SEC-MID"], e["SEC-DEEP"], e["SEC-SIB"]) == ("false", "false", "false", "true")
    eng = engine(tmp_path, text)
    got = eng.find(corpus="c", where=[{"key": "normative_effective", "equals": "false"}])
    assert sorted(n["id"] for n in got["nodes"]) == ["SEC-DEEP", "SEC-MID", "SEC-OUT"]
    n = eng.node(corpus="c", node_id="SEC-MID")["node"]
    assert n["properties"]["normative_effective"] == "false"


def test_no_override_strict_error_legacy_warning(tmp_path):  # SCEN-NS-NO-OVERRIDE
    body = node("SEC-OUT", "normative: false") + node("SEC-KID", "normative: true", head="###")
    for strict, sev in ((True, "error"), (False, "warning")):
        text = doc(body, strict=strict)
        assert eff(text)["SEC-KID"] == "false"
        fs = found(lint_text(tmp_path, text, f"o{strict}.lts"), "normative_override", "SEC-KID")
        assert [f.severity for f in fs] == [sev]


def test_bad_value_is_named_and_marks_nothing(tmp_path):  # SCEN-NS-BAD-VALUE
    text = doc(node("N-A", "normative: maybe") + node("N-B", "normative: true"))
    assert eff(text)["N-A"] == "true"
    fs = found(lint_text(tmp_path, text), "normative_bad_value")
    assert [(f.node_id, f.severity) for f in fs] == [("N-A", "error")]
    assert "maybe" in fs[0].message


def test_legacy_free_text_still_parses(tmp_path):  # SCEN-NS-LEGACY-FREE-TEXT
    text = doc(node("N-A", "normative: Needs a rewrite before the next release"), strict=False)
    assert props(text)["N-A"]["normative"] == "Needs a rewrite before the next release"
    assert eff(text)["N-A"] == "true"
    res = lint_text(tmp_path, text)
    fs = found(res, "normative_bad_value", "N-A")
    assert [f.severity for f in fs] == ["warning"] and "Needs a rewrite" in fs[0].message
    assert not [f for f in res.findings if f.severity == "error"]


def test_mode_changes_severity_and_nothing_else(tmp_path):  # SCEN-NS-MODE-SAME-GRAPH
    body = (node("SEC-OUT", "normative: false") + node("SEC-KID", "normative: true", head="###")
            + stmt("Q-ONE", extra="") + node("N-GOAL", "restates: [Q-ONE]")
            + conf("C-ONE", "Q-ONE", "[N-GOAL]"))
    a, b = parse_text(doc(body, strict=True), "t.lts"), parse_text(doc(body, strict=False), "t.lts")
    assert [(n.id, n.properties) for n in a.nodes] == [(n.id, n.properties) for n in b.nodes]
    key = lambda pf: sorted((e.source, e.target, e.type, e.layer) for e in pf.edges)
    assert key(a) == key(b)
    sev = lambda pf: {f.detail["severity"] for f in pf.vocabulary_findings if f.kind == "normative_override"}
    assert sev(a) == {"error"} and sev(b) == {"warning"}


def test_frontmatter_normative_is_an_error_and_marks_nothing(tmp_path):  # SCEN-NS-FRONTMATTER
    for strict, sev in ((True, "error"), (False, "warning")):
        text = doc(node("N-A", "status: x"), strict=strict, fm="normative: false\n")
        assert set(eff(text).values()) == {"true"}
        fs = found(lint_text(tmp_path, text, f"f{strict}.lts"), "normative_in_frontmatter")
        assert [f.severity for f in fs] == [sev]


def test_authored_effective_line_is_an_error_and_minted_value_kept(tmp_path):  # SCEN-NS-RESERVED-EFFECTIVE
    body = node("SEC-OUT", "normative: false") + node("N-KID", "normative_effective: true", head="###")
    text = doc(body)
    assert eff(text)["N-KID"] == "false"
    fs = found(lint_text(tmp_path, text), "reserved_key_authored", "N-KID")
    assert [f.severity for f in fs] == ["error"] and "normative_effective" in fs[0].message
    legacy = found(lint_text(tmp_path, doc(body, strict=False), "l.lts"), "reserved_key_authored")
    assert [f.severity for f in legacy] == ["warning"]


# ----------------------------------------------------------------- R2 source statements
def test_statement_is_non_normative_inside_a_normative_section():  # SCEN-NS-STATEMENT-IN-NORMATIVE
    e = eff(doc(stmt("Q-ONE") + node("N-SIB", "status: x")))
    assert (e["Q-ONE"], e["N-SIB"]) == ("false", "true")


def _missing(tmp_path, drop):
    lines = {"source-locator": "source-locator: l", "source-kind": "source-kind: typed",
             "source-role": "source-role: stakeholder"}
    keep = [v for k, v in lines.items() if k != drop]
    return lint_text(tmp_path, doc(node("Q-X", "source-text: words", *keep)), f"m-{drop}.lts")


def test_statement_missing_required_key_is_an_error(tmp_path):  # SCEN-NS-STATEMENT-INCOMPLETE
    for k in ("source-locator", "source-kind", "source-role"):
        fs = found(_missing(tmp_path, k), "source_statement_missing_key", "Q-X")
        assert [(f.severity, f.detail["key"]) for f in fs] == [("error", k)], k
        assert k in fs[0].message
    sel = doc(node("Q-SEL", "source-text: w", "source-locator: l", "source-kind: selected",
                   "source-role: stakeholder"))
    fs = found(lint_text(tmp_path, sel, "sel.lts"), "source_option_author_missing")
    assert [f.severity for f in fs] == ["warning"]
    typed = doc(stmt("Q-TYP"))
    res = lint_text(tmp_path, typed, "typ.lts")
    assert not found(res, "source_option_author_missing")
    assert not found(res, "source_statement_missing_key")


def test_source_kind_outside_the_values_is_named(tmp_path):  # SCEN-NS-KIND-VALUE
    text = doc(node("Q-X", "source-text: w", "source-locator: l", "source-kind: heard", "source-role: r"))
    fs = found(lint_text(tmp_path, text), "source_kind_bad_value", "Q-X")
    assert [f.severity for f in fs] == ["error"] and "heard" in fs[0].message


# ----------------------------------------------------------------- R5 restates
def test_restates_mints_an_edge_walkable_both_ways(tmp_path):  # SCEN-NS-RESTATES-EDGE
    text = doc(stmt("Q-ONE") + node("N-GOAL", "restates: [Q-ONE]"))
    es = [e for e in parse_text(text, "t.lts").edges if e.type == "RESTATES"]
    assert [(e.source, e.target, e.layer, e.inference) for e in es] == [("N-GOAL", "Q-ONE", "semantic", "property")]
    eng = engine(tmp_path, text)
    t = eng.traverse(corpus="c", start_node="N-GOAL", edge_types=["RESTATES"], direction="forward")
    assert "Q-ONE" in [n["id"] for n in t["nodes"]]
    r = eng.reverse(node_id="Q-ONE", corpus="c")
    assert ("N-GOAL", "edge:RESTATES") in [(x["node_id"], x["how"]) for x in r["referrers"]]


def test_restates_aimed_at_a_non_statement_is_warned(tmp_path):  # SCEN-NS-RESTATES-NOT-A-STATEMENT
    text = doc(node("N-PLAIN", "status: x") + node("N-GOAL", "restates: [N-PLAIN]"))
    fs = found(lint_text(tmp_path, text), "restates_target_not_statement", "N-GOAL")
    assert [f.severity for f in fs] == ["warning"]


def test_restates_on_a_non_normative_node_is_warned(tmp_path):  # SCEN-NS-RESTATES-FROM-NONNORMATIVE
    body = (stmt("Q-ONE") + node("SEC-OUT", "normative: false")
            + node("N-KID", "restates: [Q-ONE]", head="###")
            + stmt("Q-TWO", extra="- restates: [Q-ONE]\n"))
    res = lint_text(tmp_path, doc(body))
    got = sorted(f.node_id for f in found(res, "restates_from_non_normative"))
    assert got == ["N-KID", "Q-TWO"]
    assert {f.severity for f in found(res, "restates_from_non_normative")} == {"warning"}


def _prof(*types, key=None):
    recs = [{"type": t, "layer": lay} for t, lay in types]
    if key:
        recs.append({"type": "ns.GOVERNS", "layer": "semantic", "property_key": key})
    return {"domain": {"id": "d"}, "edge_types": recs}


def test_profile_that_declares_a_reserved_name_is_refused():  # SCEN-NS-PROFILE-REFUSES
    for t, lay in (("RESTATES", "causal"), ("RESTATES", "semantic"),
                   ("CONFIRMS", "semantic"), ("CHECKED_AGAINST", "semantic")):
        with pytest.raises(ProfileCollisionError) as ei:
            load_profile(_prof((t, lay)))
        assert t in str(ei.value)
    for k in ("restates", "confirmation-of", "checked-against"):
        with pytest.raises(ProfileCollisionError) as ei:
            load_profile(_prof(key=None) | {"property_keys": [k]}).declared_property_keys
        assert k in str(ei.value)
        with pytest.raises(ProfileCollisionError) as ei:
            load_profile({"domain": {"id": "d"}, "edge_types": [
                {"type": "ns.X", "layer": "semantic", "property_key": k}]})
        assert k in str(ei.value)
    load_profile(_prof(("ns.RESTATES", "semantic")))          # a namespaced twin still loads
    load_profile(_prof(("ns.CONFIRMS", "semantic")))
    load_profile(_prof(("ns.UNRELATED", "semantic")))         # the control


# ----------------------------------------------------------------- R4 quote tripwire
NINE = '"one two three four five six seven eight nine"'
SEVEN = '"one two three four five six seven"'


def test_long_quotation_in_normative_scope_is_flagged_and_controls_are_not(tmp_path):  # SCEN-NS-QUOTE-FLAGGED
    body = (node("N-PLANT", f"definition: he said {NINE} then left")
            + node("N-SEVEN", f"definition: he said {SEVEN} then left")
            + node("N-XKEY", f"x-note: he said {NINE} then left")
            + stmt("Q-ONE", text=f"he said {NINE} then left")
            + node("SEC-OUT", "normative: false")
            + node("N-NONNORM", f"definition: he said {NINE}", head="###"))
    fs = found(lint_text(tmp_path, doc(body)), "normative_quote_run")
    assert [(f.node_id, f.severity) for f in fs] == [("N-PLANT", "warning")]


def test_quote_check_does_not_run_outside_spec_documents(tmp_path):  # SCEN-NS-QUOTE-DOCTYPE
    body = node("N-PLANT", f"definition: he said {NINE} then left")
    assert not found(lint_text(tmp_path, doc(body, doctype="DECISION"), "dec.lts"), "normative_quote_run")
    assert found(lint_text(tmp_path, doc(body), "spec.lts"), "normative_quote_run", "N-PLANT")


# ----------------------------------------------------------------- R6 confirmations
def test_confirmation_append_moves_no_other_node(tmp_path, monkeypatch):  # SCEN-NS-CONFIRM-APPEND
    p = tmp_path / "w.lts"
    p.write_text(doc(stmt("Q-ONE") + node("N-GOAL", "restates: [Q-ONE]") + node("SEC-N", "status: x")), "utf-8")
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    before = p.read_text("utf-8")
    out = authoring.add_node(str(p), "C-ONE", "Confirmation", {
        "confirmation-of": "[Q-ONE]", "checked-against": "[N-GOAL]", "confirmation-verdict": "confirmed",
        "confirmation-by": "reviewer", "confirmation-date": "2026-11-15T21:40:01Z",
        "confirmation-locator": "transcript line 9"}, receipt="full")
    assert out["success"], out
    assert list(out["delta"]) == ["C-ONE"], out["delta"]
    assert before.rstrip("\n") in p.read_text("utf-8")        # nothing before was touched
    assert eff(p.read_text("utf-8"))["C-ONE"] == "false"
    assert not found(lint(str(p)), "confirmation_missing_key")


def test_confirmation_with_a_missing_required_key_is_an_error(tmp_path):  # SCEN-NS-CONFIRM-NO-LOCATOR
    full = {"confirmation-verdict": "confirmed", "confirmation-by": "reviewer",
            "confirmation-date": "2026-11-15", "confirmation-locator": "line 9",
            "checked-against": "[N-GOAL]"}
    base = stmt("Q-ONE") + node("N-GOAL", "restates: [Q-ONE]")
    for strict, sev in ((True, "error"), (False, "warning")):
        for drop in full:
            lines = ["confirmation-of: [Q-ONE]"] + [f"{k}: {v}" for k, v in full.items() if k != drop]
            res = lint_text(tmp_path, doc(base + node("C-X", *lines), strict=strict), f"n{strict}{drop}.lts")
            fs = found(res, "confirmation_missing_key", "C-X")
            assert [(f.severity, f.detail["key"]) for f in fs] == [(sev, drop)], (strict, drop)
    ok = ["confirmation-of: [Q-ONE]"] + [f"{k}: {v}" for k, v in full.items()]
    assert not found(lint_text(tmp_path, doc(base + node("C-X", *ok)), "ok.lts"), "confirmation_missing_key")


def test_confirmation_aimed_at_the_wrong_kind_of_node_is_warned(tmp_path):  # SCEN-NS-CONFIRM-TARGETS
    common = ["confirmation-by: r", "confirmation-locator: l"]
    body = (stmt("Q-ONE") + node("N-PLAIN", "status: x") + node("SEC-OUT", "normative: false")
            + node("N-NN", "status: y", head="###")
            + node("C-T1", "confirmation-of: [N-PLAIN]", "checked-against: [N-PLAIN]", "confirmation-verdict: confirmed",
                   "confirmation-date: 2026-11-15", *common)
            + node("C-T2", "confirmation-of: [Q-ONE]", "checked-against: [N-NN]", "confirmation-verdict: confirmed",
                   "confirmation-date: 2026-11-15", *common)
            + node("C-V", "confirmation-of: [Q-ONE]", "checked-against: [N-PLAIN]", "confirmation-verdict: heard",
                   "confirmation-date: 2026-11-15", *common)
            + node("C-D", "confirmation-of: [Q-ONE]", "checked-against: [N-PLAIN]", "confirmation-verdict: confirmed",
                   "confirmation-date: 15 Nov 2026", *common)
            + node("C-M", "confirmation-of: [Q-ONE] [N-PLAIN]", "checked-against: [N-PLAIN]",
                   "confirmation-verdict: confirmed", "confirmation-date: 2026-11-15", *common)
            + node("C-BOTH", "source-text: w", "confirmation-of: [Q-ONE]", "source-locator: l",
                   "source-kind: typed", "source-role: r", "checked-against: [N-PLAIN]",
                   "confirmation-verdict: confirmed", "confirmation-date: 2026-11-15", *common))
    res = lint_text(tmp_path, doc(body))
    pick = lambda kind: [(f.node_id, f.severity) for f in found(res, kind)]
    assert ("C-T1", "warning") in pick("confirmation_of_target_not_statement")
    assert ("C-T2", "warning") in pick("checked_against_non_normative")
    assert pick("confirmation_bad_value").count(("C-V", "error")) == 1
    assert ("C-D", "error") in pick("confirmation_bad_value")
    assert ("C-M", "error") in pick("confirmation_bad_value")
    assert pick("node_is_both_kinds") == [("C-BOTH", "error")]


def test_confirmation_is_not_a_requirement_and_promotes_nothing():  # SCEN-NS-CONFIRM-NOT-REQUIREMENT
    text = doc(stmt("Q-ONE") + node("N-GOAL", "restates: [Q-ONE]")
               + conf("C-ONE", "Q-ONE", "[N-GOAL]"))
    e = eff(text)
    assert (e["C-ONE"], e["Q-ONE"], e["N-GOAL"]) == ("false", "false", "true")


# ----------------------------------------------------------------- R7 states and the correction warning
def _norm_date(d):
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        d += "T00:00:00Z"
    base, _, frac = d[:-1].partition(".")
    return (base, frac.ljust(9, "0"))


def states(eng, s):
    """The recipe of REQ-NS-CONFIRM-QUERY, run through the engine's own tools."""
    refs = eng.reverse(node_id=s, corpus="c")["referrers"]
    confs = [r["node_id"] for r in refs if r["how"] == "edge:CONFIRMS"]
    if not confs:
        return "unconfirmed", []
    rows = {c: eng.node(corpus="c", node_id=c)["node"]["properties"] for c in confs}
    top = max(_norm_date(p["confirmation-date"]) for p in rows.values())
    tied = [c for c, p in rows.items() if _norm_date(p["confirmation-date"]) == top]
    corrected = [c for c in tied if rows[c]["confirmation-verdict"] == "corrected"]
    if not corrected:
        return "confirmed", tied
    decider = corrected[0]
    for t in eng.traverse(corpus="c", start_node=decider, edge_types=["CHECKED_AGAINST"],
                          direction="forward")["nodes"]:
        if t["id"] == decider:
            continue
        if not any(f"[{c}]" in str(t["properties"].get("revised-per", "")) for c in corrected):
            return "pending", corrected
    return "awaiting", corrected


def _lines(*pairs):
    return list(pairs)


def test_recipe_finds_a_confirmed_node_and_an_unconfirmed_quote(tmp_path):  # SCEN-NS-CONFIRM-RECIPE
    text = doc(stmt("Q-ONE") + stmt("Q-TWO") + node("N-GOAL", "restates: [Q-ONE]")
               + node("N-OTHER", "status: x") + conf("C-ONE", "Q-ONE", "[N-GOAL]"))
    eng = engine(tmp_path, text)
    assert states(eng, "Q-ONE") == ("confirmed", ["C-ONE"])
    assert states(eng, "Q-TWO") == ("unconfirmed", [])
    cs = eng.find(corpus="c", where=[{"key": "confirmation-verdict", "equals": "confirmed"}])
    assert [n["id"] for n in cs["nodes"]] == ["C-ONE"]
    walked = eng.traverse(corpus="c", start_node="C-ONE", edge_types=["CHECKED_AGAINST"], direction="forward")
    assert "N-GOAL" in [n["id"] for n in walked["nodes"]] and "N-OTHER" not in [n["id"] for n in walked["nodes"]]
    stmts = eng.find(corpus="c", where=[{"key": "source-text", "has": True}])
    assert sorted(n["id"] for n in stmts["nodes"]) == ["Q-ONE", "Q-TWO"]


def test_corrected_verdict_warns_until_revised_per_names_it(tmp_path):  # SCEN-NS-CONFIRM-CORRECTED
    base = stmt("Q-ONE") + conf("C-ONE", "Q-ONE", "[N-GOAL]", verdict="corrected")
    first = doc(base + node("N-GOAL", "restates: [Q-ONE]"))
    fs = found(lint_text(tmp_path, first, "a.lts"), "quote_corrected_unrevised", "N-GOAL")
    assert [f.severity for f in fs] == ["warning"] and "corrected the quote this node restates" in fs[0].message
    second = doc(base + node("N-GOAL", "restates: [Q-ONE]", "revised-per: [C-ONE]"))
    assert not found(lint_text(tmp_path, second, "b.lts"), "quote_corrected_unrevised")


def test_revised_quote_awaits_reconfirmation_then_moves_to_confirmed(tmp_path, monkeypatch):  # SCEN-NS-CONFIRM-AWAITING
    head = stmt("Q-ONE") + node("N-GOAL", "restates: [Q-ONE]", "revised-per: [C-ONE]")
    first = doc(head + conf("C-ONE", "Q-ONE", "[N-GOAL]", verdict="corrected", date="2026-11-15T10:00:00Z"))
    assert states(engine(tmp_path, first), "Q-ONE")[0] == "awaiting"
    assert not found(lint_text(tmp_path, first, "a.lts"), "quote_corrected_unrevised")
    p = tmp_path / "corp" / "00-d.lts"
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    out = authoring.add_node(str(p), "C-TWO", "Re-confirmation", {
        "confirmation-of": "[Q-ONE]", "checked-against": "[N-GOAL]", "confirmation-verdict": "confirmed",
        "confirmation-by": "reviewer", "confirmation-date": "2026-11-16T09:00:00Z",
        "confirmation-locator": "transcript line 20"})
    assert out["success"], out
    assert states(engine(tmp_path, p.read_text("utf-8")), "Q-ONE") == ("confirmed", ["C-TWO"])


def _two(tmp_path, d1, v1, d2, v2, revised=False):
    extra = ["revised-per: [C-A]"] if revised else []
    text = doc(stmt("Q-ONE") + node("N-GOAL", "restates: [Q-ONE]", *extra)
               + conf("C-A", "Q-ONE", "[N-GOAL]", verdict=v1, date=d1)
               + conf("C-B", "Q-ONE", "[N-GOAL]", verdict=v2, date=d2))
    return text, engine(tmp_path, text)


def test_same_day_reconfirmation_decides_by_time(tmp_path):  # SCEN-NS-CONFIRM-SAME-DAY
    _, e1 = _two(tmp_path, "2026-11-15T10:00:00Z", "corrected", "2026-11-15T16:30:00Z", "confirmed")
    assert states(e1, "Q-ONE")[0] == "confirmed"
    _, e2 = _two(tmp_path, "2026-11-15T10:00:00Z", "corrected", "2026-11-15T10:00:00Z", "confirmed")
    assert states(e2, "Q-ONE")[0] == "pending"
    _, e3 = _two(tmp_path, "2026-11-15", "corrected", "2026-11-15", "confirmed")
    assert states(e3, "Q-ONE")[0] == "pending"


def test_latest_confirmation_decides_and_a_superseded_correction_raises_no_warning(tmp_path):  # SCEN-NS-CONFIRM-LATEST
    t1, e1 = _two(tmp_path, "2026-11-01", "confirmed", "2026-11-02", "corrected")
    assert states(e1, "Q-ONE")[0] == "pending"
    t3, e3 = _two(tmp_path, "2026-11-01", "corrected", "2026-11-02", "confirmed")
    assert states(e3, "Q-ONE")[0] == "confirmed"
    # the closing audit's LOW 3: a superseded correction raises no f13
    assert not found(lint_text(tmp_path, t3, "sup.lts"), "quote_corrected_unrevised")
    assert found(lint_text(tmp_path, t1, "live.lts"), "quote_corrected_unrevised", "N-GOAL")
    # equal timestamps, both corrected: revised-per naming either clears it
    eq = doc(stmt("Q-ONE") + node("N-GOAL", "restates: [Q-ONE]", "revised-per: [C-B]")
             + conf("C-A", "Q-ONE", "[N-GOAL]", verdict="corrected", date="2026-11-02")
             + conf("C-B", "Q-ONE", "[N-GOAL]", verdict="corrected", date="2026-11-02"))
    assert not found(lint_text(tmp_path, eq, "eq.lts"), "quote_corrected_unrevised")


def test_revised_per_raises_no_unminted_finding(tmp_path):  # SCEN-NS-REVISED-PER-NO-UNMINTED
    text = doc(node("N-A", "revised-per: [C-ONE]", "x-other: [C-ONE]", "notes-ref: [C-ONE]")
               + node("C-ONE", "status: x"))
    res = lint_text(tmp_path, text)
    keys = sorted(f.detail.get("key") for f in res.findings if f.kind == "unminted_reference_list")
    assert "revised-per" not in keys and "notes-ref" in keys


# ----------------------------------------------------------------- the file keeps its own rule
def test_this_spec_keeps_its_own_rule():  # SCEN-NS-SELF-APPLIED
    path = ROOT / "specs" / "39-normative-and-source-statements.lts"
    pf = parse_text(path.read_text("utf-8"), str(path))
    words = props(path.read_text("utf-8"))["SRC-NS-REQUEST"]["source-text"]
    carriers = [n.id for n in pf.nodes if words[:60] in "".join(str(v) for v in n.properties.values())]
    assert carriers == ["SRC-NS-REQUEST"]
    assert pf.nodes and all(n.properties["normative_effective"] == "false" for n in pf.nodes if n.id == "SRC-NS-REQUEST")
    for g in ("GOAL-NS-SEPARATE",):
        assert "[SRC-NS-REQUEST]" in props(path.read_text("utf-8"))[g]["restates"]


def test_spec_39_lints_with_no_undeclared_key_errors(tmp_path):
    res = lint(str(ROOT / "specs" / "39-normative-and-source-statements.lts"))
    assert not found(res, "undeclared_key")


# ----------------------------------------------------------------- tables, writers, SPEC text
def test_each_new_key_is_core_and_the_edges_are_registered_once():
    core = core_keys.core_keys()
    for k in NEW_KEYS + ("normative_effective",):
        assert k in core, k
    assert REL_PROPERTY_EDGES["restates"] == ("RESTATES", "semantic", False)
    assert REL_PROPERTY_EDGES["confirmation-of"] == ("CONFIRMS", "semantic", False)
    assert REL_PROPERTY_EDGES["checked-against"] == ("CHECKED_AGAINST", "semantic", False)
    assert not {"RESTATES", "CONFIRMS", "CHECKED_AGAINST"} & set(CORE_EDGE_LAYER)
    assert binding.MINTING_KEYS.keys() >= {"restates", "confirmation-of", "checked-against"}
    from factlattice.parser import RESERVED_EDGE_TYPES
    assert RESERVED_EDGE_TYPES == {v[0] for v in REL_PROPERTY_EDGES.values()} - set(CORE_EDGE_LAYER)


def test_writers_refuse_normative_effective_and_write_nothing(tmp_path, monkeypatch):  # SDLC rule 7
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    p = tmp_path / "w.lts"
    p.write_text(doc(node("N-A", "status: x")), "utf-8")
    before = p.read_bytes()
    outs = [authoring.add_property(str(p), "N-A", "normative_effective", "false"),
            authoring.add_property(str(p), "N-A", "Normative_Effective", "false"),
            authoring.add_node(str(p), "N-B", "B", {"normative_effective": "false"}),
            authoring.rename_property(str(p), "N-A", "status", "normative_effective")]
    for o in outs:
        assert o["success"] is False and "normative_effective" in str(o), o
    assert p.read_bytes() == before
    ok = authoring.add_property(str(p), "N-A", "normative", "false")      # an ordinary new key still writes
    assert ok["success"], ok


def test_writing_normative_false_on_a_section_passes_the_gate(tmp_path, monkeypatch):
    """The minted value of every descendant moves when a section is marked; the gate must not read that as a change."""
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    p = tmp_path / "w.lts"
    p.write_text(doc(node("SEC-A", "status: x") + node("N-KID", "status: y", head="###")), "utf-8")
    ok = authoring.add_property(str(p), "SEC-A", "normative", "false")
    assert ok["success"], ok
    assert eff(p.read_text("utf-8"))["N-KID"] == "false"


def test_spec_md_states_the_keys_the_edges_and_the_precedence():  # SCEN-NS-PRECEDENCE-TEXT
    text = (ROOT / "SPEC.md").read_text("utf-8")
    missing = [w for w in NEW_KEYS + ("normative_effective", "RESTATES", "CONFIRMS", "CHECKED_AGAINST")
               if w not in text]
    assert not missing, missing
    assert re.search(r"^#{1,6} .*\[NORMATIVE-PRECEDENCE\]", text, re.M), "no node NORMATIVE-PRECEDENCE"
    stripped = re.sub(r"^(#{1,6} .*\[NORMATIVE-PRECEDENCE\].*)$", r"## removed", text, flags=re.M)
    assert not re.search(r"^#{1,6} .*\[NORMATIVE-PRECEDENCE\]", stripped, re.M)   # the test has teeth


def test_semantic_cards_leave_the_minted_key_out():
    from factlattice_engine.semantic.cards import head_card
    plain = head_card("N-A", {"title": "T", "properties": {"status": "x"}})
    minted = head_card("N-A", {"title": "T", "properties": {"status": "x", "normative_effective": "true"}})
    assert plain == minted
    assert "the words said" in head_card("Q-1", {"title": "S", "properties": {
        "source-text": "the words said", "normative_effective": "false"}})


@pytest.mark.skip(reason="SCEN-NS-COUNT-EXCLUDES is pending: no scenario detection (rule N4) exists yet")
def test_count_excludes_non_normative_scenarios():  # SCEN-NS-COUNT-EXCLUDES
    raise AssertionError("pending")


# ----------------------------------------------------------------- the key is `confirmation-of`; `confirms` is left alone
def test_bare_confirms_is_not_a_core_key_and_a_profile_may_declare_it():
    """A live profile declares `confirms` as a plain key (and a corpus uses it as free text). The spec key is
    `confirmation-of`, so `confirms` stays an ordinary, undeclared key: no edge, no state, and no collision."""
    assert "confirms" not in core_keys.core_keys() and "confirms" not in REL_PROPERTY_EDGES
    text = doc(node("N-A", "confirms: [N-B]") + node("N-B", "status: x"))
    assert not [e for e in parse_text(text, "t.lts").edges if e.type == "CONFIRMS"]
    assert eff(text)["N-A"] == "true"
    pr = load_profile({"domain": {"id": "d"}, "property_keys": ["confirms"]})
    assert "confirms" in pr.declared_property_keys


# ----------------------------------------------------------------- SCEN-NS-PROFILE-FAILS-PER-CORPUS
def _registry(tmp_path, profile_body):
    good = {}
    for cid in ("good-one", "good-two"):
        r = tmp_path / cid
        r.mkdir()
        (r / "00-d.lts").write_text(doc(node(f"N-{cid.upper().replace('-', '')}", "status: x")), "utf-8")
        good[cid] = {"roots": [{"path": str(r)}]}
    r = tmp_path / "bad-one"
    r.mkdir()
    (r / "00-d.lts").write_text(doc(node("N-BAD", "status: x")), "utf-8")
    prof = tmp_path / "bad-profile.json"
    import json
    prof.write_text(json.dumps(profile_body), "utf-8")
    return {"good-one": good["good-one"], "bad-one": {"roots": [{"path": str(r)}], "profile": str(prof)},
            "good-two": good["good-two"]}


COLLIDING = [          # a NAME the engine reserves or owns: reason profile-collision, with `names`
    ("property key, reserved", {"domain": {"id": "d"}, "property_keys": ["restates"]}),
    ("property key, a built-in minting key", {"domain": {"id": "d"}, "property_keys": ["see-also"]}),
    ("edge-type property key, a built-in minting key", {"domain": {"id": "d"}, "edge_types": [{"type": "ns.X", "property_key": "see-also"}]}),
    ("edge type, reserved", {"domain": {"id": "d"}, "edge_types": [{"type": "CHECKED_AGAINST", "layer": "semantic"}]}),
    ("edge type, core", {"domain": {"id": "d"}, "edge_types": [{"type": "PART_OF", "layer": "semantic"}]}),
    ("edge type layer disagrees with the engine's own profile config",
     {"domain": {"id": "d"}, "edge_types": [{"type": "dec.SUPERSEDES", "layer": "semantic"}]}),
]

INVALID = [            # a MALFORMED profile: reason profile-invalid, with a short fixed `detail` and no `names`
    ("property_keys is not a list", {"property_keys": "notalist"}, "property-keys-not-a-list"),
    ("property key cannot parse", {"property_keys": ["Bad Key"]}, "property-key-cannot-parse"),
    ("edge_types property_key with no edge type", {"edge_types": [{"property_key": "my-key"}]}, "property-key-without-edge-type"),
    ("edge_types property_key cannot parse", {"edge_types": [{"type": "ns.X", "property_key": "Bad Key"}]}, "property-key-cannot-parse"),
    ("property_key declared twice, disagreeing",
     {"edge_types": [{"type": "ns.A", "property_key": "my-k"}, {"type": "ns.B", "property_key": "my-k"}]}, "property-key-declared-twice"),
    ("edge type name cannot parse", {"edge_types": [{"type": "clin-ical.GOVERNS"}]}, "edge-type-name-cannot-parse"),
    ("layer not canonical", {"edge_types": [{"type": "ns.X", "layer": "bogus"}]}, "layer-not-canonical"),
    ("edge type declared twice, layers disagree",
     {"edge_types": [{"type": "ns.X", "layer": "semantic"}, {"type": "ns.X", "layer": "causal"}]}, "edge-type-declared-twice"),
]


def _one_failed(tmp_path, body):
    store = config.build_store(_registry(tmp_path, {"domain": {"id": "d"}, **body} if "domain" not in body else body))
    assert sorted(store.corpus_ids()) == ["good-one", "good-two"], "the clean corpora serve and the registry loads"
    failed = store._failed_corpora
    assert list(failed) == ["bad-one"], failed
    rec = failed["bad-one"]
    assert rec["code"] == "corpus-load-error" and rec["error"] == "ProfileCollisionError"
    assert str(tmp_path) not in str(rec), "no path in the record"
    eng = LtsEngine(store)
    assert eng.find(corpus="good-one", text="status")["success"] is True
    return rec, eng.node(corpus="bad-one", node_id="N-BAD"), eng.list_corpora()["failed_corpora"]


@pytest.mark.parametrize("label,body", COLLIDING, ids=[c[0] for c in COLLIDING])
def test_a_colliding_profile_fails_only_its_own_corpora_and_says_why(tmp_path, label, body):
    """One registry, one profile that declares a name the engine reserves or owns, two clean corpora: the corpus is in failed_corpora
    with reason profile-collision and the colliding name(s); the refusal says it is a collision; the registry loads."""
    rec, no, listed = _one_failed(tmp_path, body)
    assert rec["reason"] == "profile-collision" and rec["names"] and "detail" not in rec, rec
    assert no["success"] is False and no["reason"] == "corpus-load-error", no
    assert no["profile_failure"] == "profile-collision" and no["names"] == rec["names"], no
    assert "collid" in no["error"].lower() or "reserves or owns" in no["error"]
    assert [f["corpus_id"] for f in listed] == ["bad-one"]
    assert listed[0]["reason"] == "profile-collision" and listed[0]["names"] == rec["names"] and "detail" not in listed[0]


@pytest.mark.parametrize("label,body,detail", INVALID, ids=[c[0] for c in INVALID])
def test_a_malformed_profile_fails_only_its_own_corpora_and_is_not_called_a_collision(tmp_path, label, body, detail):
    """The same registry with a MALFORMED profile (no name collided): the record says profile-invalid with a short fixed detail and NO names,
    and the refusal does not claim a reserved name. The exception class is shared with the collisions; the reason must not be."""
    rec, no, listed = _one_failed(tmp_path, body)
    assert rec["reason"] == "profile-invalid" and rec["detail"] == detail and "names" not in rec, rec
    assert no["success"] is False and no["reason"] == "corpus-load-error", no
    assert no["profile_failure"] == "profile-invalid" and no["detail"] == detail and "names" not in no, no
    assert "reserve" not in no["error"].lower() and "collision" not in no["error"].lower() and "invalid" in no["error"].lower(), no["error"]
    assert listed[0]["reason"] == "profile-invalid" and listed[0]["detail"] == detail and "names" not in listed[0]


def test_a_clean_registry_with_a_clean_profile_is_unchanged_control(tmp_path):
    """Control for the test above: the same registry with a profile that collides with nothing loads all three."""
    store = config.build_store(_registry(tmp_path, {"domain": {"id": "d"}, "property_keys": ["an-unrelated-key"]}))
    assert sorted(store.corpus_ids()) == ["bad-one", "good-one", "good-two"] and not store._failed_corpora


# ----------------------------------------------------------------- round 3: the spec states the stakeholder's words, what is built, and the new fields
SPEC39 = ROOT / "specs" / "39-normative-and-source-statements.lts"
import os

TRANSCRIPT = Path(os.environ.get("FACTLATTICE_SPEC39_TRANSCRIPT", "/nonexistent"))      # a path on one machine, named by the runner, never in this file


def _spec_props():
    return props(SPEC39.read_text("utf-8"), str(SPEC39))


def _spec_edges():
    return parse_text(SPEC39.read_text("utf-8"), str(SPEC39)).edges


def test_spec_carries_the_two_directions_as_source_statements_and_the_goals_restate_them():
    """M1: the words behind G2, G3 and R6 are quoted in non-normative source statements (typed, product owner), and the normative nodes link to them."""
    p = _spec_props()
    stmts = sorted(n for n, v in p.items() if "source-text" in v)
    assert len(stmts) == 3 and "SRC-NS-REQUEST" in stmts, stmts
    new = [n for n in stmts if n != "SRC-NS-REQUEST"]
    for n in new:
        v = p[n]
        assert v["source-kind"] == "typed" and v["source-role"] == "product owner" and v["normative_effective"] == "false", (n, v)
        assert v["source-locator"].startswith("session transcript, line ") and "2026-11-15T" in v["source-locator"], v["source-locator"]
    lines = sorted(int(p[n]["source-locator"].split("line ")[1].split(",")[0]) for n in new)
    assert lines == [21297, 22696], lines
    rs = {(e.source, e.target) for e in _spec_edges() if e.type == "RESTATES"}
    by_line = {int(p[n]["source-locator"].split("line ")[1].split(",")[0]): n for n in new}
    a, b = by_line[21297], by_line[22696]
    assert {("GOAL-NS-KEEP-WORDS", a), ("GOAL-NS-KEEP-WORDS", b), ("GOAL-NS-NORMATIVE-GOVERNS", a), ("REQ-NS-CONFIRM", b)} <= rs, rs
    assert "ruled outside this repository" not in p["GOAL-NS-KEEP-WORDS"]["x-whose"]
    assert "not shippable" not in p["GOAL-NS-KEEP-WORDS"]["x-extension"]


def test_no_statement_text_appears_in_a_normative_node():
    """INV-NS-WORDS-ONLY-IN-STATEMENTS on this file: each statement's words sit in exactly one node, and the quote tripwire finds nothing in normative scope."""
    p = _spec_props()
    for n, v in p.items():
        if "source-text" not in v:
            continue
        probe = v["source-text"][:60]
        carriers = [m for m, w in p.items() if any(probe in str(x) for x in (w.values()))]
        assert carriers == [n], (n, carriers)
    res = lint(str(SPEC39))
    assert not found(res, "normative_quote_run") and not res.has_errors


@pytest.mark.skipif(not TRANSCRIPT.exists(), reason="set FACTLATTICE_SPEC39_TRANSCRIPT to the stakeholder's transcript (one machine only) to compare the spec's two new statements with it byte for byte")
def test_the_two_new_statements_equal_the_transcript_byte_for_byte():
    import json
    p = _spec_props()
    want = {21297: ("I have been noting the pattern of quoting me verbatim", "removing ambiguity downstream."),
            22696: ("Suggest that the non-normative section.", "Again, honest Claude.")}
    got = {}
    with open(TRANSCRIPT, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if i in want:
                m = json.loads(line)["message"]["content"]
                text = m if isinstance(m, str) else "".join(b.get("text", "") for b in m if isinstance(b, dict))
                a, z = want[i]
                got[i] = text[text.index(a): text.index(z, text.index(a)) + len(z)]
    spec = {int(v["source-locator"].split("line ")[1].split(",")[0]): v["source-text"] for v in p.values() if "source-text" in v and "21378" not in v["source-locator"]}
    assert spec == got, "a source statement differs from the record"


STALE = ("carry none of it yet", "No engine code exists", "Today's guard does not give it", "so today a profile that declares RESTATES would load",
         "This file is the first such document", "None of it.")


def test_the_spec_does_not_say_nothing_is_built():
    """S2: the file lands WITH the code, so no line says the parser, lint or writers lack it."""
    p = _spec_props()
    offenders = [(n, k) for n, v in p.items() for k, x in v.items() if isinstance(x, str) and any(s in x for s in STALE)]
    assert not offenders, offenders
    assert "x-not-built" not in p["FL-NORMATIVE-SOURCE"] and "x-not-run" not in p["ACC-NS"]
    assert "tests/test_normative_source.py" in p["ACC-NS"]["x-run"] and "SCEN-NS-COUNT-EXCLUDES" in p["ACC-NS"]["x-run"]


def test_the_spec_and_spec_md_name_the_new_failure_fields():
    """S3: the failed record and the refusal fields are in the spec and in SPEC.md, with an absence test."""
    g = _spec_props()["REQ-NS-TRACE"]["x-profile-guard"]
    for w in ("profile-collision", "profile-invalid", "names", "detail", "profile_failure"):
        assert w in g, w
    text = (ROOT / "SPEC.md").read_text("utf-8")
    missing = [w for w in ("profile-collision", "profile-invalid", "profile_failure", "`names`", "`detail`") if w not in text]
    assert not missing, missing
    sc = _spec_props()["SCEN-NS-PROFILE-FAILS-PER-CORPUS"]
    assert "profile-invalid" in sc["then"] and "profile-collision" in sc["then"] and "malformed" in sc["given"]


def test_the_spec_records_the_prior_use_of_the_later_keys():
    """§7.6 MINTING-TABLE-KEYS step 0 for confirmation-of, checked-against and revised-per: counts only, private corpora queried by name."""
    v = _spec_props()["DD-NS-NAMESPACED-KEYS"]
    s = " ".join(str(x) for x in v.values())
    for w in ("confirmation-of", "checked-against", "revised-per", "16 private"):
        assert w in s, w


def test_the_spec_says_a_quotes_section_is_the_usual_form():
    """AM1: one sentence."""
    assert "usual form" in _spec_props()["REQ-NS-SOURCE"]["x-rule"]


# ----------------------------------------------------------------- round 3: the Diamond's findings
def doc_fm(body: str, *, doctype: str = "SPEC", h1_tag: str = "", strict: bool = True) -> str:
    """A document whose type is declared in FRONTMATTER (`doctype:`), as the create writer and specs 36-39 declare it; the h1 carries a plain id
    unless `h1_tag` is given."""
    head = "---\n" + ('lts: "1.1"\n' if strict else "") + f"title: T\ndoctype: {doctype}\n---\n\n"
    return head + (f"# T [{h1_tag}:T-ROOT]\n\n" if h1_tag else "# T [T-ROOT]\n\n") + body


EIGHT = '"one two three four five six seven eight"'


def test_quote_tripwire_runs_on_a_frontmatter_doctype_spec(tmp_path):
    """F1: SPEC is declared by `doctype: SPEC` in frontmatter, with a plain id on the h1. The tripwire must run there."""
    body = node("N-PLANT", f"definition: he said {NINE} then left")
    fs = found(lint_text(tmp_path, doc_fm(body), "fm.lts"), "normative_quote_run")
    assert [(f.node_id, f.severity) for f in fs] == [("N-PLANT", "warning")]
    # the nearest negatives: another frontmatter doctype, and the h1 tag winning over frontmatter in either direction
    assert not found(lint_text(tmp_path, doc_fm(body, doctype="DECISION"), "fm2.lts"), "normative_quote_run")
    assert not found(lint_text(tmp_path, doc_fm(body, doctype="SPEC", h1_tag="DECISION"), "fm3.lts"), "normative_quote_run")
    assert found(lint_text(tmp_path, doc_fm(body, doctype="DECISION", h1_tag="SPEC"), "fm4.lts"), "normative_quote_run")
    # and the exemptions still hold in a frontmatter-doctype document
    exempt = (stmt("Q-ONE", text=f"he said {NINE} then left") + node("N-XKEY", f"x-note: he said {NINE}"))
    assert not found(lint_text(tmp_path, doc_fm(exempt), "fm5.lts"), "normative_quote_run")


def test_quote_tripwire_fires_on_spec_39_itself_when_a_quote_is_planted(tmp_path):
    """F1 positive control on the file that follows its own rule: a nine-word quote planted in a normative node of a copy of spec 39 is flagged,
    and the unplanted file is not."""
    text = SPEC39.read_text("utf-8")
    assert not found(lint_text(tmp_path, text, "orig.lts"), "normative_quote_run")
    anchor = "## Requirement: the normative content is the default, and the source can be asked [REQ-NS-PRECEDENCE]\n\n"
    assert text.count(anchor) == 1
    planted = text.replace(anchor, anchor + f"- definition: the architect said {NINE} and left\n")
    fs = found(lint_text(tmp_path, planted, "planted.lts"), "normative_quote_run", "REQ-NS-PRECEDENCE")
    assert [f.severity for f in fs] == ["warning"]


def test_the_eight_word_boundary_is_pinned(tmp_path):
    """F7: a run of exactly eight words is flagged and seven is not (a threshold of nine would survive a test that plants nine and seven)."""
    eight = found(lint_text(tmp_path, doc(node("N-A", f"definition: said {EIGHT} end")), "e8.lts"), "normative_quote_run")
    seven = found(lint_text(tmp_path, doc(node("N-A", f"definition: said {SEVEN} end")), "e7.lts"), "normative_quote_run")
    assert len(eight) == 1 and not seven


def test_normative_true_on_a_source_statement_or_a_confirmation_is_the_override_error(tmp_path):
    """F7: f2 on a statement and on a confirmation (a mutant that only checked the section chain survives the section-only test)."""
    body = (stmt("Q-ONE", extra="- normative: true\n") + node("N-GOAL", "restates: [Q-ONE]")
            + conf("C-ONE", "Q-ONE", "[N-GOAL]", extra="- normative: true\n"))
    res = lint_text(tmp_path, doc(body))
    got = sorted((f.node_id, f.severity) for f in found(res, "normative_override"))
    assert got == [("C-ONE", "error"), ("Q-ONE", "error")], got
    assert eff(doc(body))["Q-ONE"] == "false" and eff(doc(body))["C-ONE"] == "false"


def test_a_numeric_offset_confirmation_date_is_refused(tmp_path):
    """F7: the spec says the zone is always Z, no numeric offset."""
    for bad in ("2026-11-15T21:40:01+01:00", "2026-11-15T21:40:01-0500", "2026-11-15T21:40Z", "2026-11-15t21:40:01z"):
        text = doc(stmt("Q-ONE") + node("N-GOAL", "restates: [Q-ONE]") + conf("C-ONE", "Q-ONE", "[N-GOAL]", date=bad))
        fs = [f for f in found(lint_text(tmp_path, text, "d.lts"), "confirmation_bad_value") if f.detail.get("key") == "confirmation-date"]
        assert [f.severity for f in fs] == ["error"], bad
    ok = doc(stmt("Q-ONE") + node("N-GOAL", "restates: [Q-ONE]") + conf("C-ONE", "Q-ONE", "[N-GOAL]", date="2026-11-15T21:40:01.123Z"))
    assert not found(lint_text(tmp_path, ok, "ok.lts"), "confirmation_bad_value")


def test_lint_says_corrected_wins_a_tie_and_confirmed_does_not(tmp_path):
    """F7: f13 reached by LINT (not the test's own states() helper): a corrected and a confirmed confirmation on exactly equal dates leave the node
    warned; a later confirmed one clears it; an earlier corrected one under a later confirmed raises nothing."""
    def txt(d1, v1, d2, v2):
        return doc(stmt("Q-ONE") + node("N-GOAL", "restates: [Q-ONE]") + conf("C-A", "Q-ONE", "[N-GOAL]", verdict=v1, date=d1)
                   + conf("C-B", "Q-ONE", "[N-GOAL]", verdict=v2, date=d2))
    tie = found(lint_text(tmp_path, txt("2026-11-15", "confirmed", "2026-11-15", "corrected"), "t1.lts"), "quote_corrected_unrevised", "N-GOAL")
    tie2 = found(lint_text(tmp_path, txt("2026-11-15T10:00:00Z", "corrected", "2026-11-15T10:00:00Z", "confirmed"), "t2.lts"), "quote_corrected_unrevised", "N-GOAL")
    later = found(lint_text(tmp_path, txt("2026-11-15", "corrected", "2026-11-16", "confirmed"), "t3.lts"), "quote_corrected_unrevised")
    assert len(tie) == 1 and len(tie2) == 1 and not later


def _plain(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    p = tmp_path / "w.lts"
    p.write_text(doc(node("N-A", "status: x", "- ⛔ FLAGGED: broken line".lstrip("- ")) if False else node("N-A", "status: x")) + "\n", "utf-8")
    return p


def test_every_single_writer_site_refuses_normative_effective_on_its_own(tmp_path, monkeypatch):
    """F7: each refusal site, reached WITHOUT the other layer to catch what it drops (the pydantic schema and the edit layer each refuse; a mutant
    that deletes one site survives a test that goes through both)."""
    from factlattice.authoring import edit
    from factlattice.authoring.schema import Property
    p = _plain(tmp_path, monkeypatch)
    before = p.read_bytes()
    with pytest.raises(edit.EditRefused) as e:
        edit.set_property(p, "N-A", "normative_effective", "false")
    assert e.value.evidence["refusal"] == "reserved_key"
    with pytest.raises(edit.EditRefused) as e:
        edit.set_property(p, "N-A", "Normative_Effective", "false", mode="append")
    assert e.value.evidence["refusal"] == "reserved_key"
    with pytest.raises(edit.EditRefused) as e:
        edit.rename_property(p, "N-A", "status", "normative_effective")
    assert e.value.evidence["refusal"] == "reserved_key"
    with pytest.raises(edit.EditRefused) as e:
        edit.add_node(p, "N-B", "B", {"normative_effective": "false"})
    assert "reserved" in str(e.value)
    with pytest.raises(ValueError):
        Property(key="normative_effective", value="x")
    assert p.read_bytes() == before, "every refusal wrote nothing"


def test_repair_refuses_normative_effective_on_a_prose_line(tmp_path, monkeypatch):
    from factlattice.authoring import edit
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    p = tmp_path / "r.lts"
    text = doc(node("N-A", "status: x") + "- ⛔ FLAGGED 2026-08-10: a broken line\n")
    p.write_text(text, "utf-8")
    ln = next(i for i, l in enumerate(text.split("\n"), 1) if "FLAGGED" in l)
    before = p.read_bytes()
    with pytest.raises(edit.EditRefused) as e:
        edit.repair_property(p, "N-A", line=ln, key="normative_effective")
    assert "reserved" in str(e.value).lower()
    assert p.read_bytes() == before


def test_an_authored_normative_effective_line_can_be_removed_by_the_typed_writer(tmp_path, monkeypatch):
    """F5: lint f9 tells the author to remove the line; lts_remove_property must be able to (a repair), and removing an ordinary key still works."""
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    p = tmp_path / "rm.lts"
    p.write_text(doc(node("N-A", "status: x", "normative_effective: true")), "utf-8")
    assert found(lint(str(p)), "reserved_key_authored", "N-A")
    out = authoring.remove_property(str(p), "N-A", "normative_effective")
    assert out["success"], out
    assert "normative_effective: true" not in p.read_text("utf-8") and "status: x" in p.read_text("utf-8")
    assert not found(lint(str(p)), "reserved_key_authored")
    assert eff(p.read_text("utf-8"))["N-A"] == "true"                       # the minted value is still there
    ctl = authoring.remove_property(str(p), "N-A", "status")
    assert ctl["success"], ctl


def test_text_search_does_not_match_the_minted_state(tmp_path):
    """Diamond P2 (recall): `normative_effective` is on every node, so a text search for "true" or "false" would match every node. It is filterable
    through `where` and visible in lts_node, and nothing else."""
    text = doc(node("N-A", "status: x") + node("SEC-OUT", "normative: false") + node("N-KID", "status: y", head="###"))
    eng = engine(tmp_path, text)
    # "true" is authored nowhere: 0. "false" is authored on SEC-OUT only (`normative: false`): that node, and NOT its descendant that merely carries the minted state
    for word, want in (("true", []), ("false", ["SEC-OUT"])):
        for kw in ({"text": word}, {"all_of": [word]}, {"any_of": [word, "zzzz"]}):
            got = eng.find(corpus="c", semantic="off", **kw)
            assert sorted(x["id"] for x in got["nodes"]) == want, (word, kw, got["total_matched"])
    w = eng.find(corpus="c", where=[{"key": "normative_effective", "equals": "false"}], semantic="off")
    assert sorted(n["id"] for n in w["nodes"]) == ["N-KID", "SEC-OUT"]
    assert eng.node(corpus="c", node_id="N-KID")["node"]["properties"]["normative_effective"] == "false"
    assert eng.find(corpus="c", text="x", semantic="off")["total_matched"] >= 1      # control: authored values still search


def test_register_corpus_gives_the_same_reason_as_boot_for_a_refused_profile(tmp_path):
    """Diamond P2 (F4): the runtime door answers a refused profile with the boot reason (profile-collision or profile-invalid) and registers nothing."""
    store = config.build_store(_registry(tmp_path, {"domain": {"id": "d"}, "property_keys": ["an-unrelated-key"]}))
    eng = LtsEngine(store)
    import json
    for label, body, want in (("collision", {"domain": {"id": "d"}, "property_keys": ["see-also"]}, "profile-collision"),
                              ("invalid", {"domain": {"id": "d"}, "edge_types": [{"type": "ns.X", "layer": "bogus"}]}, "profile-invalid")):
        r = tmp_path / f"rt-{label}"
        r.mkdir()
        (r / "00.lts").write_text(doc(node(f"N-RT{label.upper()}", "status: x")), "utf-8")
        pf = tmp_path / f"rt-{label}.json"
        pf.write_text(json.dumps(body), "utf-8")
        out = eng.register_corpus(f"rt-{label}", {"root": str(r), "domain_suffix": f"x.{label}", "profile": str(pf)})
        assert out["success"] is False and out["reason"] == "corpus-load-error" and out["profile_failure"] == want, out
        assert out["error_class"] == "CorpusLoadError" and str(tmp_path) not in str(out)
        assert f"rt-{label}" not in store.corpus_ids()
    assert eng.register_corpus("rt-ok", {"root": str(tmp_path / "rt-invalid"), "domain_suffix": "x.ok"})["success"] is True    # control: no profile registers


def test_a_write_by_path_into_a_corpus_failed_on_its_profile_behaves_like_any_other_failed_corpus(tmp_path, monkeypatch):
    """F2: no refusal. A write by path into a corpus that failed for its PROFILE lands, with the same receipt shape as a write into a corpus that failed
    for another reason (a broken-link member), on the release as well. The receipt does not flag it (an OPEN item in the spec)."""
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    reg = _registry(tmp_path, {"domain": {"id": "d"}, "property_keys": ["see-also"]})
    broken = tmp_path / "broken-one"
    broken.mkdir()
    (broken / "00.lts").write_text(doc(node("N-BRK", "status: x")), "utf-8")
    (broken / "dead.lts").symlink_to(tmp_path / "nowhere.lts")
    reg["broken-one"] = {"roots": [{"path": str(broken)}]}
    store = config.build_store(reg)
    assert sorted(store._failed_corpora) == ["bad-one", "broken-one"], store._failed_corpora
    outs = {}
    for cid, target in (("bad-one", tmp_path / "bad-one" / "00-d.lts"), ("broken-one", broken / "00.lts")):
        nid = "N-BAD" if cid == "bad-one" else "N-BRK"
        outs[cid] = authoring.add_property(str(target), nid, "note", "written", store=store)
    assert all(o["success"] and o["written"] and not o["refused"] for o in outs.values()), outs
    assert set(outs["bad-one"]) == set(outs["broken-one"])
    mem = {c: (o["root_membership"] or {}).get("inside_corpus") for c, o in outs.items()}
    assert mem["bad-one"] == mem["broken-one"], mem          # whatever the release does for a failed corpus, a profile-failed one does the same


# ----------------------------------------------------------------- round 4: the delta audit and the delta Diamond
import re as _re


def test_a_layer_disagreement_in_the_engines_own_profile_directory_still_stops_the_registry(tmp_path, monkeypatch):
    """D1: the engine's profile directory (FACTLATTICE_PROFILE_DIR) is engine configuration, not a corpus's profile: it stays a registry-wide failure, as before
    this spec. No corpus names it, so no corpus can be failed for it. The raise site names the type (kills the mutant that drops `names=` there)."""
    from factlattice.domain_profiles import load_profile_edge_config
    d = tmp_path / "pdir"
    d.mkdir()
    (d / "a.lts").write_text("| zz.THING | semantic | one |\n", "utf-8")
    (d / "b.lts").write_text("| zz.THING | causal | two |\n", "utf-8")
    with pytest.raises(ProfileCollisionError) as e:
        load_profile_edge_config(str(d))
    assert e.value.names == ("zz.THING",)
    monkeypatch.setenv("FACTLATTICE_PROFILE_DIR", str(d))
    reg = _registry(tmp_path, {"domain": {"id": "d"}, "property_keys": ["an-unrelated-key"]})
    with pytest.raises(ProfileCollisionError):
        config.build_store(reg)                    # the registry does not load, though no entry names a profile that is refused


def test_the_other_raise_sites_carry_their_name_or_detail(tmp_path):
    """D3: the raise sites no registry reaches (`merge_*` are called only by the selfcheck; `ParserConfig.merge` by a caller that merges two configs) still pass
    `detail=` or `names=`, so a later caller that maps them gets the right reason."""
    from factlattice.parser_config import ParserConfig
    from factlattice.profile import merge_edge_types, merge_property_edges
    a = load_profile({"domain": {"id": "a"}, "edge_types": [{"type": "ns.X", "layer": "semantic"}]})
    b = load_profile({"domain": {"id": "b"}, "edge_types": [{"type": "ns.X", "layer": "causal"}]})
    with pytest.raises(ProfileCollisionError) as e:
        merge_edge_types([a, b])
    assert e.value.detail == "edge-type-profiles-disagree" and not e.value.names
    pa = load_profile({"domain": {"id": "a"}, "edge_types": [{"type": "ns.A", "property_key": "my-k"}]})
    pb = load_profile({"domain": {"id": "b"}, "edge_types": [{"type": "ns.B", "property_key": "my-k"}]})
    with pytest.raises(ProfileCollisionError) as e:
        merge_property_edges([pa, pb])
    assert e.value.detail == "property-key-profiles-disagree" and not e.value.names
    with pytest.raises(ProfileCollisionError) as e:
        ParserConfig.from_kwargs(profile_layers={"ns.X": "semantic"}).merge(ParserConfig.from_kwargs(profile_layers={"ns.X": "causal"}))
    assert e.value.names == ("ns.X",)
    from factlattice_engine import config as ec
    assert ec.profile_failure_fields(e.value) == {"reason": "profile-collision", "names": ["ns.X"]}


def test_the_register_refusal_says_nothing_was_registered(tmp_path):
    """D3: the refusal carries `registered: false` (a mutant that said true survived a test that looked only at the store)."""
    import json
    store = config.build_store(_registry(tmp_path, {"domain": {"id": "d"}, "property_keys": ["an-unrelated-key"]}))
    r = tmp_path / "rt-flag"
    r.mkdir()
    (r / "00.lts").write_text(doc(node("N-RTFLAG", "status: x")), "utf-8")
    pf = tmp_path / "rt-flag.json"
    pf.write_text(json.dumps({"domain": {"id": "d"}, "property_keys": ["see-also"]}), "utf-8")
    out = LtsEngine(store).register_corpus("rt-flag", {"root": str(r), "domain_suffix": "x.flag", "profile": str(pf)})
    assert out["registered"] is False and out["success"] is False and out["corpus_id"] == "rt-flag", out


def test_the_spec_states_the_engine_profile_directory_is_outside_the_rule():
    g = _spec_props()["REQ-NS-TRACE"]["x-profile-guard"]
    assert "profile directory" in g and "FACTLATTICE_PROFILE_DIR" in g and "stops the registry" in g


def test_the_before_sentence_is_the_measured_one():
    """S5/D2: the account of what happened BEFORE this spec is the measured table: the release and the trunk, both entry forms, and which behaviour changed.
    No sentence about the past without a measurement behind it."""
    g = _spec_props()["REQ-NS-TRACE"]["x-profile-guard"]
    assert "ignored silently, the corpus being served without it; this spec changes both" not in g
    for w in ("af4cfc2", "c3d7242", "roots list", "single root", "property_keys", "edge_types", "8 raise sites"):
        assert w in g, w
    cl = (ROOT / "CHANGELOG.md").read_text("utf-8")
    assert "every other refused profile was ignored silently" not in cl
    entry = cl[cl.index("Normative content, source statements and confirmations"):]
    entry = entry[:entry.index("\n")]
    for w in ("af4cfc2", "c3d7242", "roots list", "single `root:`", "edge_types"):
        assert w in entry, w


def test_the_spec_file_states_the_text_search_exclusion_and_the_removal():
    """S4: the two build behaviours of the minted key are in the spec, not only in SPEC.md."""
    p = _spec_props()
    assert "text search" in p["REQ-NS-MARK"]["x-effective"] and "does not read" in p["REQ-NS-MARK"]["x-effective"]
    assert "remove" in p["REQ-NS-MARK"]["x-reserved"] and "authored" in p["REQ-NS-MARK"]["x-reserved"]


def test_precedence_wording_follows_his_words():
    """A1: the stakeholder said the normative part is the DEFAULT WHEN IN DOUBT and that a reader may ask him to confirm; the spec does not say it governs
    on ANY difference, and G2's attribution to him does not include the authors' design (where and when)."""
    p = _spec_props()
    for nid, key in (("GOAL-NS-NORMATIVE-GOVERNS", "x-goal"), ("REQ-NS-PRECEDENCE", "x-rule"), ("REQ-NS-CONFIRM", "x-precedence-unchanged")):
        v = p[nid][key]
        assert "any difference" not in v and "default" in v and "in doubt" in v, (nid, key, v)
    assert "each with where and when" not in p["GOAL-NS-KEEP-WORDS"]["x-whose"]
    assert "authors' design" in p["GOAL-NS-KEEP-WORDS"]["x-whose"]
    assert "default" in (ROOT / "SPEC.md").read_text("utf-8").split("[NORMATIVE-PRECEDENCE]")[1][:900]


def test_a_source_text_may_be_a_contiguous_excerpt():
    """AM3: one sentence in REQ-NS-SOURCE."""
    s = _spec_props()["REQ-NS-SOURCE"]["x-source-text"]
    assert "excerpt" in s and "locator names the record" in s


def test_every_fixed_detail_phrase_is_listed_in_spec_md():
    """D5: the `detail` phrases are strings a caller may match on; each one the code can raise is listed in SPEC.md."""
    src = (ROOT / "factlattice" / "profile.py").read_text("utf-8")
    phrases = set(_re.findall(r'detail="([a-z-]+)"', src)) | {"profile-rejected"}
    assert len(phrases) >= 10, phrases
    text = (ROOT / "SPEC.md").read_text("utf-8")
    assert not [p for p in sorted(phrases) if p not in text], [p for p in sorted(phrases) if p not in text]


def test_the_checklist_has_the_rules_for_the_round_4_extension_points():
    """D5: rules for the doctype reader, the detail phrases, the shape keys and the raise sites in all three files."""
    s = (ROOT / "SDLC.md").read_text("utf-8")
    for w in ("[DOCTYPE-ONE-READER]", "_is_spec_document", "[PROFILE-FAILURE-DETAIL-PHRASES]", "[BOOT-SHAPE-KEYS]", "$profile_dir",
              "parser_config.py", "domain_profiles.py"):
        assert w in s, w


def test_the_tool_descriptions_name_the_new_reply_fields():
    """D6 / F2: an agent that reads the tool's own description learns the new failure fields, IN CONTEXT: the values of `reason`, `names` beside the
    collision, `detail` beside the invalid profile, `profile_failure` with its two values, that it fails only the one corpus (not the registry), and that
    `lts_register_corpus` registers nothing and says so. (Two bare words that were already in the description proved nothing.)"""
    from factlattice_engine import server
    by = {t["name"]: t["description"] for t in server.TOOLS}
    d = by["lts_list_corpora"]
    for s in ("fails ONLY that corpus (never the registry)", "reason: profile-collision, names}", "reason: profile-invalid, detail}",
              "profile_failure: profile-collision | profile-invalid, names | detail}", "a short fixed phrase, no `names`"):
        assert s in d, s
    assert "fails the whole registry" not in d and "ProfileCollisionError" in d
    r = by["lts_register_corpus"]
    for s in ("registers NOTHING", "registered: false", "profile_failure: profile-collision | profile-invalid, names | detail"):
        assert s in r, s


def test_the_fallback_detail_phrase_is_pinned_and_every_phrase_file_is_read():
    """F3: a raise site that passes neither `names=` nor `detail=` maps to profile-invalid with the fixed fallback phrase; the phrase test reads EVERY file that
    defines a phrase (profile.py, parser_config.py, domain_profiles.py) and the fallback in config.py."""
    from factlattice_engine import config as ec
    assert ec.profile_failure_fields(ProfileCollisionError("x")) == {"reason": "profile-invalid", "detail": "profile-rejected"}
    assert ec.profile_failure_fields(ProfileCollisionError("x", detail="layer-not-canonical")) == {"reason": "profile-invalid", "detail": "layer-not-canonical"}
    text = (ROOT / "SPEC.md").read_text("utf-8")
    phrases = set()
    for rel in ("factlattice/profile.py", "factlattice/parser_config.py", "factlattice/domain_profiles.py"):
        phrases |= set(_re.findall(r'detail="([a-z-]+)"', (ROOT / rel).read_text("utf-8")))
    fb = _re.findall(r'"detail", None\) or "([a-z-]+)"', (ROOT / "factlattice_engine" / "config.py").read_text("utf-8"))
    assert fb == ["profile-rejected"], fb
    phrases |= set(fb)
    assert len(phrases) >= 10
    assert not [p for p in sorted(phrases) if "`" + p + "`" not in text], [p for p in sorted(phrases) if "`" + p + "`" not in text]


def test_titles_follow_his_words_too():
    """A1 / F1: the wording is carried to node TITLES and to acceptance criteria, not only to property values. The node ids stay."""
    pf = parse_text(SPEC39.read_text("utf-8"), str(SPEC39))
    title = {n.id: n.title for n in pf.nodes}
    assert "governs" not in title["GOAL-NS-NORMATIVE-GOVERNS"] and "any difference" not in title["GOAL-NS-NORMATIVE-GOVERNS"]
    assert "in doubt" in title["GOAL-NS-NORMATIVE-GOVERNS"] and "default" in title["GOAL-NS-NORMATIVE-GOVERNS"]
    assert "governs" not in title["REQ-NS-PRECEDENCE"] and "default" in title["REQ-NS-PRECEDENCE"]
    then = _spec_props()["SCEN-NS-CONFIRM-NOT-REQUIREMENT"]["then"]
    assert "still governs" not in then and "default" in then
    # and no title or value anywhere keeps the retracted phrase
    bad = [(n.id, "title") for n in pf.nodes if "governs on any difference" in n.title]
    bad += [(n.id, k) for n in pf.nodes for k, v in n.properties.items() if isinstance(v, str) and "governs on any difference" in v]
    assert not bad, bad


def test_the_checklist_has_the_closing_audit_rules():
    s = (ROOT / "SDLC.md").read_text("utf-8")
    key = s[s.index("[KEY-MINTED-EDGE-ONE-REGISTRATION]"):]
    key = key[:key.index("\n- [ ]")]
    assert "FACTLATTICE_PROFILE_DIR" in key and "engine configuration" in key and "stops the registry" in key
    for w in ("[QUOTE-CHECKS-ACCEPT-EXCERPTS]", "contiguous excerpt", "[PAST-BEHAVIOUR-CITES-A-MEASUREMENT]", "[TOOL-DESCRIPTION-NAMES-REPLY-FIELDS]"):
        assert w in s, w
