"""`normative: yes` and `normative: no` are aliases of `true` and `false` (spec 39 DD-NS-YES-NO-ALIAS): normalised where normative_effective is minted, accepted in any
case, no per-node warning, at most ONE info-level line per FILE suggesting the canonical spelling. Any other free text keeps today's finding. Of the ten tests first written, 8 were run and
observed FAILING on 720285d (no alias) and 2 passed there (the invariance pins: free text keeps its finding, the authored line stays as authored); the tests added after
state their own RED or are mutant killers, as their docstrings say."""
from __future__ import annotations

from pathlib import Path

from factlattice.parser import parse_text
from tests.test_normative_source import NINE, doc, eff, found, lint_text, node, props

ROOT = Path(__file__).resolve().parent.parent
SPEC39 = ROOT / "specs" / "39-normative-and-source-statements.lts"


def test_no_marks_a_section_non_normative_exactly_like_false():
    body = (node("SEC-OUT", "normative: no") + node("SEC-MID", "status: a", head="###") + node("SEC-DEEP", "status: b", head="####")
            + node("SEC-SIB", "status: c"))
    e = eff(doc(body))
    assert (e["SEC-OUT"], e["SEC-MID"], e["SEC-DEEP"], e["SEC-SIB"]) == ("false", "false", "false", "true")
    # the same graph as the canonical spelling
    canon = eff(doc(body.replace("normative: no", "normative: false")))
    assert e == canon


def test_yes_behaves_exactly_like_true_including_the_override_error_inside_a_no_section(tmp_path):
    body = node("SEC-OUT", "normative: no") + node("SEC-KID", "normative: yes", head="###") + node("N-PLAIN", "normative: yes")
    for strict, sev in ((True, "error"), (False, "warning")):
        text = doc(body, strict=strict)
        assert eff(text)["SEC-KID"] == "false" and eff(text)["N-PLAIN"] == "true"
        fs = found(lint_text(tmp_path, text, f"o{strict}.lts"), "normative_override", "SEC-KID")
        assert [f.severity for f in fs] == [sev]
        assert not found(lint_text(tmp_path, text, f"p{strict}.lts"), "normative_override", "N-PLAIN")       # yes outside a non-normative section is plain `true`


def test_the_alias_is_accepted_in_any_case(tmp_path):
    body = node("SEC-A", "normative: NO") + node("N-A1", "status: x", head="###") + node("SEC-B", "normative: No") + node("SEC-C", "normative: Yes")
    text = doc(body)
    e = eff(text)
    assert (e["SEC-A"], e["N-A1"], e["SEC-B"], e["SEC-C"]) == ("false", "false", "false", "true")
    assert not found(lint_text(tmp_path, text), "normative_bad_value")


def test_no_per_node_warning_and_strict_has_no_error_for_the_alias(tmp_path):
    body = "".join(node(f"N-{i}", f"normative: {v}") for i, v in enumerate(("no", "yes", "no", "yes")))
    for strict in (True, False):
        res = lint_text(tmp_path, doc(body, strict=strict), f"a{strict}.lts")
        assert not found(res, "normative_bad_value") and not res.has_errors
        assert not [f for f in res.findings if f.severity in ("error", "warning") and f.kind.startswith("normative")]


def test_exactly_one_info_line_per_file_however_many_nodes_use_the_alias(tmp_path):
    mixed = doc(node("S-A", "normative: no") + node("S-B", "normative: yes") + node("S-C", "normative: NO") + node("S-D", "normative: false") + node("S-E", "normative: true"))
    for strict in (True, False):
        fs = found(lint_text(tmp_path, doc("".join(node(f"S-{c}", f"normative: {v}") for c, v in zip("ABCDE", ("no", "yes", "NO", "false", "true"))), strict=strict),
                             f"m{strict}.lts"), "normative_alias_spelling")
        assert len(fs) == 1 and fs[0].severity == "info", [(f.node_id, f.severity) for f in fs]
        assert "false" in fs[0].message and "true" in fs[0].message
    # a file with only canonical spellings, and a file with free text, raise none
    assert not found(lint_text(tmp_path, doc(node("S-A", "normative: false") + node("S-B", "normative: true")), "c.lts"), "normative_alias_spelling")
    assert not found(lint_text(tmp_path, doc(node("S-A", "normative: maybe")), "f.lts"), "normative_alias_spelling")
    assert not found(lint_text(tmp_path, doc(node("S-A", "normative: True") + node("S-B", "normative: FALSE")), "u.lts"), "normative_alias_spelling")   # another case of true/false is no alias
    # and one file does not carry another's line
    two = tmp_path / "two"
    two.mkdir()
    (two / "a.lts").write_text(mixed, "utf-8")
    (two / "b.lts").write_text(mixed, "utf-8")
    from factlattice_engine.lint import lint
    res = lint(str(two))
    assert len(found(res, "normative_alias_spelling")) == 2


def test_free_text_still_gets_todays_finding(tmp_path):
    text = doc(node("N-A", "normative: maybe") + node("N-B", "normative: Needs a rewrite") + node("N-C", "normative: falsey"))
    fs = found(lint_text(tmp_path, text), "normative_bad_value")
    assert sorted(f.node_id for f in fs) == ["N-A", "N-B", "N-C"] and {f.severity for f in fs} == {"error"}
    legacy = found(lint_text(tmp_path, doc(node("N-A", "normative: maybe"), strict=False), "l.lts"), "normative_bad_value")
    assert [f.severity for f in legacy] == ["warning"]
    assert eff(text) == {**eff(text), "N-A": "true", "N-B": "true", "N-C": "true"}


def test_a_no_quotes_section_is_non_normative_and_its_quotes_raise_no_tripwire(tmp_path):
    """The reason for the alias: every spec to date marks its quotes section `no`."""
    quote = f'a stakeholder said {NINE} and left'
    body = node("SEC-Q", "normative: no") + node("Q-ONE", f"definition: {quote}", head="###") + node("N-NORM", f"definition: {quote}")
    fs = found(lint_text(tmp_path, doc(body)), "normative_quote_run")
    assert [f.node_id for f in fs] == ["N-NORM"], [f.node_id for f in fs]       # the control: the same run in a normative node is flagged
    assert eff(doc(body))["Q-ONE"] == "false"


def test_the_authored_line_stays_as_authored_and_strict_and_legacy_mint_the_same():
    body = node("SEC-A", "normative: no") + node("N-A", "status: x", head="###")
    a, b = props(doc(body, strict=True)), props(doc(body, strict=False))
    assert a == b and a["SEC-A"]["normative"] == "no"


def test_the_spec_states_the_alias_and_its_reason_and_has_a_scenario():
    p = props(SPEC39.read_text("utf-8"), str(SPEC39))
    v = p["REQ-NS-MARK"]["x-value-check"]
    for w in ("yes", "no", "alias", "any case", "one info"):
        assert w in v, w
    d = p["DD-NS-YES-NO-ALIAS"]
    assert "x-decision" in d and "all but one use" in d["rationale"].lower() and "migration" in d["rationale"] and "strictness" in d["rationale"]
    sc = p["SCEN-NS-YES-NO-ALIAS"]
    assert "no" in sc["given"] and "exactly one" in sc["then"]
    assert "[SCEN-NS-YES-NO-ALIAS]" in p["REQ-NS-MARK"]["verified-by"]
    text = (ROOT / "SPEC.md").read_text("utf-8")
    assert "`yes`" in text and "`no`" in text and "alias" in text


def test_the_checklist_has_the_alias_rule():
    s = (ROOT / "SDLC.md").read_text("utf-8")
    assert "[NORMATIVE-ALIAS-ONE-PLACE]" in s and "normative_alias_spelling" in s


# ----------------------------------------------------------------- the tripwire is strict-mode only (an EXCEPTION to "mode changes severity only", DD-NS-STRICT-ENFORCES)
def test_the_quote_tripwire_fires_only_in_strict_spec_documents(tmp_path):
    """A legacy document gets no tripwire finding when this lands; it gets the tripwire when it upgrades (adds the `lts:` marker)."""
    from tests.test_normative_source import doc_fm
    body = node("N-PLANT", f"definition: he said {NINE} then left")
    for label, make in (("tag form", lambda s: doc(body, strict=s)), ("frontmatter form", lambda s: doc_fm(body, strict=s))):
        legacy = found(lint_text(tmp_path, make(False), f"l-{label[:3]}.lts"), "normative_quote_run")
        strict = found(lint_text(tmp_path, make(True), f"s-{label[:3]}.lts"), "normative_quote_run")
        assert not legacy, (label, "legacy", [(f.node_id, f.severity) for f in legacy])
        assert [(f.node_id, f.severity) for f in strict] == [("N-PLANT", "warning")], (label, "strict")


def test_the_tripwire_still_catches_a_planted_quote_in_spec_39_itself(tmp_path):
    """Spec 39 is a strict document: the control that the tripwire is alive."""
    text = SPEC39.read_text("utf-8")
    assert 'lts: "1.1"' in text.split("---")[1]
    anchor = "## Requirement: the normative content is the default, and the source can be asked [REQ-NS-PRECEDENCE]\n\n"
    assert text.count(anchor) == 1
    planted = text.replace(anchor, anchor + f"- definition: the architect said {NINE} and left\n")
    assert [f.severity for f in found(lint_text(tmp_path, planted, "p.lts"), "normative_quote_run", "REQ-NS-PRECEDENCE")] == ["warning"]


def test_the_spec_and_the_docs_state_the_tripwire_is_strict_only():
    p = props(SPEC39.read_text("utf-8"), str(SPEC39))
    for nid, key in (("REQ-NS-LINT", "x-a"), ("MECH-NS-LINT", "x-f6")):
        v = p[nid][key]
        assert "strict" in v.lower() and "legacy" in v and "DD-NS-STRICT-ENFORCES" in v and "exception" in v, (nid, key, v)
    assert "strict" in (ROOT / "SPEC.md").read_text("utf-8").split("**Quote tripwire.**")[1][:700]
    cl = (ROOT / "CHANGELOG.md").read_text("utf-8")
    assert "strict-mode" in cl[cl.index("`normative: yes` and `normative: no` are aliases"):][:2500]
    s = (ROOT / "SDLC.md").read_text("utf-8")
    assert "strict" in s[s.index("[LEXICAL-LINT-STATES-ITS-LIMITS]"):][:1500]


# ----------------------------------------------------------------- the delta audit of fbdcb1f (A1 to A7)
def test_true_and_false_are_accepted_in_any_case_like_the_aliases(tmp_path):
    """A6: ONE case rule for all four values. `False` marks like `false`, `TRUE` is `true`; no finding for either."""
    body = node("SEC-A", "normative: False") + node("N-A1", "status: x", head="###") + node("SEC-B", "normative: FALSE") + node("N-T", "normative: TRUE") + node("N-U", "normative: True")
    for strict in (True, False):
        text = doc(body, strict=strict)
        e = eff(text)
        assert (e["SEC-A"], e["N-A1"], e["SEC-B"], e["N-T"], e["N-U"]) == ("false", "false", "false", "true", "true"), strict
        res = lint_text(tmp_path, text, f"c{strict}.lts")
        assert not found(res, "normative_bad_value") and not res.has_errors
    # `True` inside a non-normative section is the override error, like `true`
    ov = doc(node("SEC-A", "normative: no") + node("SEC-K", "normative: TRUE", head="###"))
    assert found(lint_text(tmp_path, ov, "ov.lts"), "normative_override", "SEC-K")


def test_the_spec_wording_after_the_delta_audit():
    p = props(SPEC39.read_text("utf-8"), str(SPEC39))
    fix = "no value finding and no per-node spelling finding; the override finding applies to yes exactly as to true"
    assert fix in p["REQ-NS-MARK"]["x-value-check"] and fix in p["MECH-NS-LINT"]["x-f1"]                       # A1
    assert "(or yes)" in p["MECH-NS-LINT"]["x-f2"]
    for k in ("x-value-check", "x-f1"):
        assert "raises no finding on its node" not in p["REQ-NS-MARK" if k == "x-value-check" else "MECH-NS-LINT"][k]
    d = p["DD-NS-YES-NO-ALIAS"]                                                                                  # A2
    assert "authors' design" in d["x-decision"] and "maintainers' ruling" not in d["x-decision"] and "not the stakeholder's" in d["x-decision"]
    assert "forced migration" in d["x-decision"] and "constrained resource" in d["x-decision"]
    assert "its alias no" in p["REQ-NS-MARK"]["x-effective"] and "normalised" in p["MECH-NS-PARSER"]["x-keys"]    # A3
    assert "normative_alias_spelling" in p["MECH-NS-LINT"]["x-legacy-severity"] and "info in both modes" in p["MECH-NS-LINT"]["x-legacy-severity"]   # A4
    assert "only to other free-text" in p["MECH-NS-LINT"]["x-f1"]
    r = p["DD-NS-STRICT-ENFORCES"]["rationale"]                                                                  # A5
    assert r.startswith("This land adds no error-severity finding to any legacy document: in a legacy document every finding spec 39 defines is reported as a warning or an info line, and the quote tripwire does not run there at all. Upgrading a document to strict mode is where those findings become errors, by §1.5.1's design."), r
    assert "gains only" not in r and "adds only:" not in r and "none gains warnings" not in r
    assert "any case" in p["REQ-NS-MARK"]["x-value-check"] and "true, false, yes and no" in p["REQ-NS-MARK"]["x-value-check"]   # A6
    for nid, key in (("FL-NORMATIVE-SOURCE", "x-built"), ("ACC-NS", "x-run")):                                   # A7
        assert "tests/test_normative_alias.py" in p[nid][key], (nid, key)
    assert "The nine" in p["PLAN-NS"]["x-sdlc"] and "The eight:" not in p["PLAN-NS"]["x-sdlc"]
    spec_md = (ROOT / "SPEC.md").read_text("utf-8")
    assert "its alias `no`" in spec_md and "no value finding and no per-node spelling finding" in spec_md
    assert fix in (ROOT / "SDLC.md").read_text("utf-8") and fix in (ROOT / "CHANGELOG.md").read_text("utf-8")


def test_the_two_checklist_rules_from_the_delta_audit():
    s = (ROOT / "SDLC.md").read_text("utf-8")
    for w in ("[PER-FILE-FINDING-SEVERITY-IS-STATED]", "[VALUE-ALIASES-ONE-TABLE]"):
        assert w in s, w


# ----------------------------------------------------------------- the Diamond of fbdcb1f (P2-1, P2-3)
def test_free_text_that_begins_with_an_alias_word_is_not_an_alias(tmp_path):
    """P2-1: `no way`, `yes please`, `n` and `y` are free text: they keep today's finding, mark nothing, and are NOT counted as alias spellings."""
    for value in ("no way", "yes please", "n", "y", "nope", "yesterday", "no -- see the note"):
        text = doc(node("SEC-A", f"normative: {value}") + node("N-KID", "status: x", head="###"))
        fs = found(lint_text(tmp_path, text, "ft.lts"), "normative_bad_value", "SEC-A")
        assert [f.severity for f in fs] == ["error"], value
        assert eff(text)["SEC-A"] == "true" and eff(text)["N-KID"] == "true", value          # marks nothing
        assert not found(lint_text(tmp_path, text, "ft2.lts"), "normative_alias_spelling"), value


def test_yes_on_a_source_statement_or_a_confirmation_raises_the_override_finding_like_true(tmp_path):
    from tests.test_normative_source import conf, stmt
    for word in ("true", "yes", "Yes"):
        body = (stmt("Q-ONE", extra=f"- normative: {word}\n") + node("N-GOAL", "restates: [Q-ONE]") + conf("C-ONE", "Q-ONE", "[N-GOAL]", extra=f"- normative: {word}\n"))
        got = sorted((f.node_id, f.severity) for f in found(lint_text(tmp_path, doc(body), "ov.lts"), "normative_override"))
        assert got == [("C-ONE", "error"), ("Q-ONE", "error")], (word, got)
        assert eff(doc(body))["Q-ONE"] == "false" and eff(doc(body))["C-ONE"] == "false"


def test_the_info_line_counts_the_alias_uses_in_the_file(tmp_path):
    """The one info line says how many nodes spell the alias (a canonical `false` or `true` is not counted)."""
    body = "".join(node(f"S-{c}", f"normative: {v}") for c, v in zip("ABCDEF", ("no", "yes", "NO", "Yes", "false", "true")))
    msg = found(lint_text(tmp_path, doc(body), "n.lts"), "normative_alias_spelling")[0].message
    assert msg.startswith("4 node(s) spell"), msg
    one = found(lint_text(tmp_path, doc(node("S-A", "normative: no")), "n1.lts"), "normative_alias_spelling")[0].message
    assert one.startswith("1 node(s) spell"), one


def test_scen_ns_yes_no_alias_built_exactly_as_the_scenario_is_written(tmp_path):
    """P2-3: the scenario's given, when and then, as written in the spec, including a source statement under the `no` section."""
    q = f'a stakeholder said {NINE} and left'
    quotes = (node("SEC-Q", "normative: no")
              + f"### Statement [Q-ONE]\n\n- source-text: {q}\n- source-locator: transcript line 1\n- source-kind: typed\n- source-role: stakeholder\n\n"
              + node("SEC-KID", "normative: yes", head="###") + node("N-YES", "normative: Yes") + node("N-NO", "normative: NO")
              + node("N-RUN", f"definition: {q}"))
    other = node("O-A", "normative: false") + node("O-B", "normative: true") + node("O-C", "normative: maybe")
    for strict, sev in ((True, "error"), (False, "warning")):
        text = doc(quotes, strict=strict)
        e = eff(text)
        assert (e["SEC-Q"], e["Q-ONE"], e["SEC-KID"], e["N-YES"], e["N-NO"], e["N-RUN"]) == ("false", "false", "false", "true", "false", "true"), (strict, e)
        res = lint_text(tmp_path, text, f"sc{strict}.lts")
        assert [(f.node_id, f.severity) for f in found(res, "normative_override")] == [("SEC-KID", sev)]
        assert [f.node_id for f in found(res, "normative_quote_run")] == ([] if not strict else ["N-RUN"])          # strict-only tripwire; the statement and the no section raise none
        assert not found(res, "normative_bad_value")
        info = found(res, "normative_alias_spelling")
        assert len(info) == 1 and info[0].severity == "info"
        second = lint_text(tmp_path, doc(other, strict=strict), f"sd{strict}.lts")
        assert not found(second, "normative_alias_spelling")
        assert [(f.node_id) for f in found(second, "normative_bad_value")] == ["O-C"]            # free text still gets its finding


# ----------------------------------------------------------------- the one exception to "mode changes severity only" (B1, B2, B3, R1-R4)
EXC = "Mode changes severity only, with one exception: the quote tripwire (f6) runs only in strict-mode SPEC documents."


def test_what_a_legacy_document_gains_is_exactly_what_the_decision_says(tmp_path):
    """B1, checked against behaviour: in a LEGACY document the land adds NO error-severity finding, and only an info line for yes/no, a warning for other free text,
    the override warning; the quote tripwire does not run. The strict twin of the same bytes does get errors (that is where findings become errors)."""
    body = (node("SEC-OUT", "normative: no") + node("SEC-KID", "normative: yes", head="###") + node("N-FREE", "normative: maybe")
            + node("N-RUN", f"definition: he said {NINE} then left"))
    kinds = lambda res: sorted({(f.kind, f.severity) for f in res.findings if f.kind.startswith("normative")})
    legacy = lint_text(tmp_path, doc(body, strict=False), "lg.lts")
    assert kinds(legacy) == [("normative_alias_spelling", "info"), ("normative_bad_value", "warning"), ("normative_override", "warning")], kinds(legacy)
    assert not [f for f in legacy.findings if f.severity == "error" and f.kind.startswith("normative")] and not found(legacy, "normative_quote_run")
    strict = lint_text(tmp_path, doc(body, strict=True), "st.lts")
    assert ("normative_bad_value", "error") in kinds(strict) and ("normative_override", "error") in kinds(strict) and found(strict, "normative_quote_run")


def test_a_legacy_source_statement_missing_its_locator_gets_a_warning_not_an_error(tmp_path):
    """The B1 sentence about the other new keys, proved: the same bytes are a warning in legacy and an error in strict."""
    body = node("SRC-A", "source-text: he said it", "source-kind: typed", "source-role: owner")          # no source-locator
    sev = lambda strict: [f.severity for f in found(lint_text(tmp_path, doc(body, strict=strict), f"s{strict}.lts"), "source_statement_missing_key", "SRC-A")]
    assert sev(False) == ["warning"] and sev(True) == ["error"]


def test_the_one_exception_to_mode_changes_severity_only_is_stated_where_the_spec_says_it():
    p = props(SPEC39.read_text("utf-8"), str(SPEC39))
    for nid, key in (("DD-NS-STRICT-ENFORCES", "x-decision"), ("MECH-NS-LINT", "x-legacy-severity"), ("MECH-NS-PARSER", "x-modes"), ("SCEN-NS-MODE-SAME-GRAPH", "then")):
        assert "one exception" in p[nid][key] and "quote tripwire (f6)" in p[nid][key] and "strict-mode SPEC documents" in p[nid][key], (nid, key)
    d = p["DD-NS-STRICT-ENFORCES"]["x-decision"]
    assert _missing_terms(d) == [], _missing_terms(d)
    xa = p["REQ-NS-LINT"]["x-a"]
    assert "[DD-NS-STRICT-ENFORCES]" in xa and "D5 of" not in xa
    assert "strict-mode" in p["SCEN-NS-QUOTE-FLAGGED"]["given"]                                   # R4
    then = p["SCEN-NS-YES-NO-ALIAS"]["then"]                                                       # B3
    assert "legacy run raises no quote warning" in then and "the strict run" in then
    assert "Every spec authored to date" not in p["DD-NS-YES-NO-ALIAS"]["rationale"] and "all but one use" in p["DD-NS-YES-NO-ALIAS"]["rationale"].lower()   # R3
    s = (ROOT / "SDLC.md").read_text("utf-8")
    row = s[s.index("[VALUE-ALIASES-ONE-TABLE]"):][:900]
    assert "no value finding and no per-node spelling finding" in row and "never writes a per-node finding" not in row            # R1
    assert "[ONE-MODE-LINT-IS-A-NAMED-EXCEPTION]" in s
    cl = (ROOT / "CHANGELOG.md").read_text("utf-8")
    main = cl[cl.index("- Normative content, source statements and confirmations"):]
    assert "strict-mode SPEC documents" in main[:main.index("\n")]                                  # R2


def test_the_alias_table_is_read_by_exactly_one_function_and_the_rules_say_so():
    """P2-1: `_mint_normative_state` used to read NORMATIVE_ALIASES itself. Now only `normative_spelling` reads either table."""
    import ast
    src = (ROOT / "factlattice" / "parser.py").read_text("utf-8")
    readers = set()

    class V(ast.NodeVisitor):
        stack: list = []

        def visit_FunctionDef(self, n):
            self.stack.append(n.name); self.generic_visit(n); self.stack.pop()

        def visit_Name(self, n):
            if n.id in ("NORMATIVE_ALIASES", "_NORMATIVE_VALUES") and isinstance(n.ctx, ast.Load) and self.stack:
                readers.add(self.stack[-1])
    V.stack = []
    V().visit(ast.parse(src))
    assert readers == {"normative_spelling"}, readers
    from factlattice.parser import normative_spelling, normative_value
    assert [normative_spelling(x) for x in ("No", "TRUE", "yes", "maybe")] == [("false", True), ("true", False), ("true", True), (None, False)]
    assert [normative_value(x) for x in ("No", "maybe")] == ["false", None]
    s = (ROOT / "SDLC.md").read_text("utf-8")
    assert "the ONLY function that reads" in s[s.index("[NORMATIVE-ALIAS-ONE-PLACE]"):][:700] and "parser.normative_spelling" in s


def test_the_spec_says_where_the_uses_are_exactly():
    """P1-B: 83 of the 84 uses are in one product's public specs (66 no, 17 yes); the 84th, free text, is in a different corpus and product."""
    p = props(SPEC39.read_text("utf-8"), str(SPEC39))["DD-NS-YES-NO-ALIAS"]
    for key in ("x-decision", "rationale"):
        assert "83 of the 84" in p[key] and "different corpus and product" in p[key], key
    assert "all 84 uses are in the public specs of one product" not in p["x-decision"] and "84 uses of normative, all in the public specs of one product" not in p["rationale"]


def _missing_terms(d):
    """L2: each term is checked on its own (the old line mixed and/or, so one `or` made the rest optional)."""
    need = [EXC, "authors' design", "chief architect", "1,311", "720285d", "burying"]
    return [w for w in need if w not in d]


def test_the_term_checker_fires_when_one_term_is_removed():
    """L2's negative control: take the real text and remove each required term in turn; the checker must name exactly that term."""
    d = props(SPEC39.read_text("utf-8"), str(SPEC39))["DD-NS-STRICT-ENFORCES"]["x-decision"]
    assert _missing_terms(d) == []
    for term in ("1,311", "720285d", "chief architect", "burying", "authors' design", EXC):
        assert _missing_terms(d.replace(term, "")) == [term], term


def test_every_spec_39_finding_in_a_legacy_document_is_a_warning_or_info_and_the_strict_twin_has_errors(tmp_path):
    """The proof the general B1 sentence needs: ONE legacy document that triggers every spec 39 finding kind there is, including f8 in frontmatter.
    Zero errors, no normative_quote_run; the same bytes in strict give errors and the tripwire."""
    from factlattice_engine.lint.normative import K_CHECKED_NONNORM, K_CONFIRMS_TARGET, K_CORRECTED, K_RESTATES_FROM, K_RESTATES_TARGET
    from factlattice_engine.lint.vocabulary import NORMATIVE_KINDS
    from tests.test_normative_source import conf
    body = (stmt_ok() + node("Q-BAD", "source-text: w", "source-locator: l", "source-kind: heard", "source-role: r")
            + node("Q-SEL", "source-text: w", "source-locator: l", "source-kind: selected", "source-role: r")
            + node("Q-MISS", "source-text: w")
            + node("SEC-OUT", "normative: no") + node("N-KID", "normative: true", "restates: [Q-ONE]", head="###")
            + node("N-FREE", "normative: maybe") + node("N-RES", "normative_effective: true") + node("N-PLAIN", "status: x")
            + node("N-RF", "restates: [N-PLAIN]") + node("N-GOAL", "restates: [Q-ONE]")
            + node("N-RUN", f"definition: he said {NINE} then left")
            + node("C-MISS", "confirmation-of: [Q-ONE]")
            + conf("C-BAD", "Q-ONE", "[N-PLAIN]", verdict="heard")
            + conf("C-T1", "N-PLAIN", "[N-PLAIN]") + conf("C-T2", "Q-ONE", "[N-KID]")
            + conf("C-CORR", "Q-ONE", "[N-GOAL]", verdict="corrected")
            + node("C-BOTH", "source-text: w", "confirmation-of: [Q-ONE]"))
    want = (set(NORMATIVE_KINDS) | {K_CHECKED_NONNORM, K_CONFIRMS_TARGET, K_CORRECTED, K_RESTATES_FROM, K_RESTATES_TARGET}) - {"normative_quote_run"}
    legacy = lint_text(tmp_path, doc(body, strict=False, fm="normative: false\n"), "lg.lts")
    ours = [f for f in legacy.findings if f.kind in want | {"normative_quote_run"}]
    assert {f.kind for f in ours} == want, (sorted(want - {f.kind for f in ours}), sorted({f.kind for f in ours} - want))   # every kind fired: the positive control
    assert not [f for f in ours if f.severity == "error"], [(f.kind, f.node_id) for f in ours if f.severity == "error"]
    assert not found(legacy, "normative_quote_run")
    assert {f.severity for f in ours} <= {"warning", "info"}
    strict = lint_text(tmp_path, doc(body, strict=True, fm="normative: false\n"), "st.lts")
    errs = {f.kind for f in strict.findings if f.kind in want and f.severity == "error"}
    assert {"normative_bad_value", "normative_override", "normative_in_frontmatter", "reserved_key_authored", "source_statement_missing_key",
            "source_kind_bad_value", "confirmation_missing_key", "confirmation_bad_value", "node_is_both_kinds"} <= errs, errs
    assert found(strict, "normative_quote_run")


def stmt_ok():
    from tests.test_normative_source import stmt
    return stmt("Q-ONE")


def test_the_mode_sentences_carry_the_named_exception_or_point_to_it():
    """L1, L3, L4."""
    CL = "except the named exceptions a spec states (spec 39: the quote tripwire"
    p = props(SPEC39.read_text("utf-8"), str(SPEC39))
    assert CL in p["INV-NS-ONE-STATE"]["x-rule"] if "INV-NS-ONE-STATE" in p else any(CL in v.get("x-rule", "") for v in p.values())
    titles = {n.id: n.title for n in parse_text(SPEC39.read_text("utf-8"), str(SPEC39)).nodes}
    assert "except" in titles["SCEN-NS-MODE-SAME-GRAPH"] and "quote tripwire" in titles["SCEN-NS-MODE-SAME-GRAPH"]
    s = (ROOT / "SPEC.md").read_text("utf-8")
    assert CL in s[s.index("(§1.5.1: the marker changes severity only"):][:300]
    inv = s[s.index("THE INVARIANT THAT MAKES THE MARKER SAFE"):][:600]
    assert CL in inv and "SAME nodes" not in inv.replace("the same nodes", "SAME nodes") or CL in inv
    src = (ROOT / "factlattice" / "parser.py").read_text("utf-8")
    assert "a legacy document only parses, so it gains nothing" not in src and src.count(CL) >= 1 and 'the one exception to "mode changes severity only"' in src


def test_a_non_string_normative_value_is_never_an_alias_spelling_and_gets_the_free_text_finding(tmp_path):
    """P3 mutant killer: a repeated key (a list) is not a spelling of `no`: it gets normative_bad_value and no alias info line; a nested block is never a value at all.
    Limits, accepted: this and the single-reader test do not catch a second reader of the alias table in ANOTHER module, or a read through a
    module-level copy of the table (the single-reader test scans parser.py for the two names only)."""
    for label, lines, bad in (("list", ("normative: no", "normative: no"), ["error"]),
                              ("nested", ("normative:", "  - no"), [])):     # a nested block is not stored as a property at all today (measured), so there is no value to judge
        res = lint_text(tmp_path, doc(node("N-ODD", *lines)), f"{label}.lts")
        assert [f.severity for f in found(res, "normative_bad_value", "N-ODD")] == bad, label
        assert not found(res, "normative_alias_spelling"), label
        assert eff(doc(node("N-ODD", *lines)))["N-ODD"] == "true", label
