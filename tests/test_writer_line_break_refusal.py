"""Every typed writer REFUSES the characters the parser's line splitting treats as a line break, in every
text input, and the writers' gate compares VALUES, not only keys.

THE DEFECT (maintainers' ruling of 2026-10-28). The writers refused LF in a value but accepted CR, VT, FF,
FS/GS/RS (U+001C-U+001E), NEL (U+0085), LINE SEPARATOR (U+2028) and PARAGRAPH SEPARATOR (U+2029). The parser
splits the file on all of them, so the stored property was cut at that character, the tail became a stray
prose line, the writer replied `success: true`, and the whole-file gate passed because it compared parsed KEY
sets and the parsed result was self-consistent.

HOW THIS FILE KEEPS ITS INSTRUMENT HONEST
  * The ground-truth set of line-break code points is measured HERE by parsing a document with one property
    line per code point (every scalar value, surrogates excluded) and reading back which values the parser cut.
    It does not call the module under test and it is not a hand list. A hand-listed expectation is only a
    sanity floor beside it.
  * The refusal set of the module under test is compared with that measurement. If the parser's splitting and
    the refusal set ever diverge, a test fails.
  * The differential tests run every code point of that set through every writer and every text input, and
    check that nothing was written: the file bytes, the ledger and the backup directory are unchanged.
  * The values gate is proven by switching the input check OFF and showing the gate still refuses.
  * Controls: an ordinary space, non-ASCII text and an emoji are accepted unchanged.

CRLF IS STATED EXPLICITLY: an input containing CR LF contains two line-break characters; it is refused and the
first one found (U+000D) is named. CRLF line endings of an existing FILE are not input and are not touched.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from factlattice import ledger
from factlattice.authoring import edit
from factlattice.authoring.edit import EditRefused, add_node, remove_property, rename_property, repair_property, set_property
from factlattice.parser import parse_text

CODE = "line_break_in_input"
MISMATCH = "values-gate"       # not a code: the VALUES gate's refusal is recognised by `evidence.requested_value`
PREFIX = "é🙂"          # two code points, both non-ASCII: the reported index is in code points, 0-based
IDX = 2

BASE = """# Doc [SPEC:LB-ROOT]

## Node one [LB-ONE]

- kind: root
- tag: a
- note: first

## Node two [LB-TWO]

- confidence: high
"""

REPAIR_BASE = BASE.replace("- note: first\n", "- note: first\n- ⛔ FLAGGED: something broken\n")


# --------------------------------------------------------------------------------------- the measurement
@pytest.fixture(scope="module")
def parser_breaks() -> frozenset:
    """Every code point the PARSER cuts a property value at, measured by parsing one `- kHEX: a<c>b` line per
    code point. A code point that splits the line leaves the value `a` (the tail `b` becomes a prose line)."""
    cps = [c for c in range(0x110000) if not 0xD800 <= c <= 0xDFFF]
    lines = ["# Doc [SPEC:SW-ROOT]", "", "## Node [SW-ONE]", ""]
    lines += [f"- k{c:x}: a{chr(c)}b" for c in cps]
    pf = parse_text("\n".join(lines) + "\n", "<sweep>")
    props = next(n for n in pf.nodes if n.id == "SW-ONE").properties
    cut = frozenset(c for c in cps if props.get(f"k{c:x}") != f"a{chr(c)}b")
    return cut


def test_the_measurement_itself_has_a_positive_and_a_negative_control(parser_breaks):
    """An instrument that can only say 'nothing' is not an instrument: it must find the known breaks and
    must NOT flag ordinary characters."""
    assert {0x0A, 0x0D, 0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029} <= parser_breaks
    assert not ({0x20, 0x09, 0xA0, 0xE9, 0x1F642, 0x200B, 0x2060, 0x3000} & parser_breaks)
    assert len(parser_breaks) == 10          # the measured set today; a parser change shows here first


# ----------------------------------------------------------------------------------------------- fixtures
@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    (tmp_path / "out").mkdir()
    return tmp_path


def _write(env, name, text):
    p = env / "out" / name
    p.write_bytes(text.encode("utf-8"))
    return p


def _snapshot(env, *paths):
    """Everything a write could touch: the file bytes, its ledger, and the backup directory."""
    snap = []
    for p in paths:
        p = Path(p)
        snap.append(p.read_bytes() if p.exists() else None)
        lp = ledger.ledger_path(os.path.realpath(p))
        snap.append(lp.read_bytes() if lp.exists() else None)
    bak = env / "bak"
    snap.append(sorted(str(x.relative_to(bak)) for x in bak.rglob("*")) if bak.exists() else [])
    return snap


def _bad(c: int) -> str:
    return f"{PREFIX}{chr(c)}tail"


def _verdict(call, env, paths, *, field, c, what, index=IDX, code=CODE):
    """None when the writer behaved; otherwise ("accepted" | "wrong-code" | "wrote-on-refusal", detail)."""
    before = _snapshot(env, *paths)
    try:
        call()
    except EditRefused as e:
        ev = e.evidence or {}
        if code == MISMATCH:
            # an APPEND is also value-checked by the whole-file multiset gate (`multiset_mismatch`), which predates this
            # change; either is a values gate. Every other path must be refused by the requested-value comparison.
            if "requested_value" not in ev and "multiset_mismatch" not in ev:
                return ("wrong-gate", f"{what} U+{c:04X}: refused, but not by the values gate: {ev} / {e}")
        elif ev.get("refusal") != code:
            return ("wrong-code", f"{what} U+{c:04X}: refused with {ev.get('refusal')!r}: {e}")
        if code == CODE:
            want = {"codepoint": f"U+{c:04X}", "index": index, "field": field}
            if {k: ev.get(k) for k in want} != want or f"U+{c:04X}" not in str(e) or str(index) not in str(e):
                return ("wrong-evidence", f"{what} U+{c:04X}: evidence {ev} / message {e}")
        if _snapshot(env, *paths) != before:
            return ("wrote-on-refusal", f"{what} U+{c:04X}")
        return None
    after = [Path(p).read_text(encoding="utf-8", errors="replace") if Path(p).exists() else None for p in paths]
    return ("accepted", f"{what} U+{c:04X}: the writer ACCEPTED it (success). File(s) now:\n"
            + "\n----\n".join(a or "<absent>" for a in after))


def _must_refuse(call, env, paths, **kw):
    v = _verdict(call, env, paths, **kw)
    if v:
        pytest.fail(f"[{v[0]}] {v[1]}")


# ------------------------------------------------------------------- every writer x every text input x every break
def _cases(env):
    """name -> (callable(c), field, files-to-snapshot). `c` is the code point under test."""
    f = _write(env, "a.lts", BASE)
    rf = _write(env, "r.lts", REPAIR_BASE)
    line = REPAIR_BASE.splitlines().index("- ⛔ FLAGGED: something broken") + 1
    n = [0]

    def fresh():
        n[0] += 1
        return env / "out" / f"new{n[0]}.lts"

    def cr(**kw):      # a create call with every argument ordinary unless overridden
        a = dict(node_id="LB-NEW", title="A new node", props={"kind": "x"}, doc_title="Doc title",
                 domain="dom", doctype="dt")
        a.update(kw)
        p = fresh()
        return p, (lambda: add_node(p, a["node_id"], a["title"], a["props"], create=True,
                                    doc_title=a["doc_title"], domain=a["domain"], doctype=a["doctype"]))

    def create_case(field, **mk):
        def run(c):
            kw = {k: (v(c) if callable(v) else v) for k, v in mk.items()}
            p, call = cr(**kw)
            return call, [p]
        return run

    return {
        "set value (add)": (lambda c: (lambda: set_property(f, "LB-ONE", "fresh", _bad(c))), "value", [f]),
        "set value (replace)": (lambda c: (lambda: set_property(f, "LB-ONE", "kind", _bad(c))), "value", [f]),
        "set value (append)": (lambda c: (lambda: set_property(f, "LB-ONE", "tag", _bad(c), mode="append")),
                               "value", [f]),
        "set key": (lambda c: (lambda: set_property(f, "LB-ONE", _bad(c), "v")), "key", [f]),
        "set node_id": (lambda c: (lambda: set_property(f, _bad(c), "fresh", "v")), "node_id", [f]),
        "repair value": (lambda c: (lambda: repair_property(rf, "LB-ONE", line, "flagged", _bad(c))), "value", [rf]),
        "repair key": (lambda c: (lambda: repair_property(rf, "LB-ONE", line, _bad(c), "v")), "key", [rf]),
        "rename new_key": (lambda c: (lambda: rename_property(f, "LB-ONE", "tag", _bad(c))), "new_key", [f]),
        "rename key": (lambda c: (lambda: rename_property(f, "LB-ONE", _bad(c), "zzz")), "key", [f]),
        "remove key": (lambda c: (lambda: remove_property(f, "LB-ONE", _bad(c))), "key", [f]),
        "remove expect_value": (lambda c: (lambda: remove_property(f, "LB-ONE", "note", expect_value=_bad(c))),
                                "expect_value", [f]),
        "add_node title": (lambda c: (lambda: add_node(f, "LB-NEW", _bad(c), {"kind": "x"})), "title", [f]),
        "add_node node_id": (lambda c: (lambda: add_node(f, _bad(c), "A new node", {"kind": "x"})),
                             "node_id", [f]),
        "add_node section": (lambda c: (lambda: add_node(f, "LB-NEW", "A new node", {"kind": "x"},
                                                         section=_bad(c))), "section", [f]),
        "add_node property key": (lambda c: (lambda: add_node(f, "LB-NEW", "A new node", {_bad(c): "x"})),
                                  "properties key", [f]),
        "add_node property value": (lambda c: (lambda: add_node(f, "LB-NEW", "A new node", {"kind": _bad(c)})),
                                    "properties[kind]", [f]),
        # create=True: the file must not exist afterwards
        "create title": (lambda c: create_case("title", title=_bad)(c)[0], "title", None),
        "create node_id": (lambda c: create_case("node_id", node_id=_bad)(c)[0], "node_id", None),
        "create doc_title": (lambda c: create_case("doc_title", doc_title=_bad)(c)[0], "doc_title", None),
        "create domain": (lambda c: create_case("domain", domain=_bad)(c)[0], "domain", None),
        "create doctype": (lambda c: create_case("doctype", doctype=_bad)(c)[0], "doctype", None),
        "create property key": (lambda c: create_case("k", props=lambda x: {_bad(x): "x"})(c)[0],
                                "properties key", None),
        "create property value": (lambda c: create_case("v", props=lambda x: {"kind": _bad(x)})(c)[0],
                                  "properties[kind]", None),
    }


#: LF in a PROPERTY VALUE of add_node already had a refusal whose reply text is PINNED byte-for-byte for the
#: non-create path (tests/test_add_node_noncreate_pin.py) and coded `create_property_invalid` on the create path.
#: It was never the defect (LF was refused before); it is still refused, and these two cases keep their old code.
LEGACY_LF = {"add_node property value": None, "create property value": "create_property_invalid"}


def test_every_writer_refuses_every_parser_line_break_in_every_text_input(env, parser_breaks):
    """Collects EVERY failure before failing, so a red run says how many cases were accepted (the defect) and
    how many were merely refused with the wrong code, instead of stopping at the first."""
    cases = _cases(env)
    checked, bad = 0, []
    for name, (mk, field, files) in cases.items():
        for c in sorted(parser_breaks):
            if c == 0x0A and name in LEGACY_LF:
                continue
            _write(env, "a.lts", BASE)              # every case starts from the same bytes, so an
            _write(env, "r.lts", REPAIR_BASE)       # accepted write cannot contaminate the next case
            if files is None:                       # a create: the target must stay absent
                call = mk(c)
                before = {str(p) for p in (env / "out").glob("new*.lts")}
                v = _verdict(call, env, [], field=field, c=c, what=name)
                after = {str(p) for p in (env / "out").glob("new*.lts")}
                if v is None and after != before:
                    v = ("wrote-on-refusal", f"{name} U+{c:04X}: a refused create left a file behind")
                if v and v[0] == "accepted":
                    v = ("accepted", f"{name} U+{c:04X}: the create ACCEPTED it")
            else:
                v = _verdict(mk(c), env, files, field=field, c=c, what=name)
            checked += 1
            if v:
                bad.append(v)
    kinds = {}
    for k, _ in bad:
        kinds[k] = kinds.get(k, 0) + 1
    assert checked == 23 * 10 - len(LEGACY_LF)
    assert not bad, f"{len(bad)} of {checked} cases failed {kinds}; first: {bad[0][1][:900]}"


def test_legacy_lf_in_an_add_node_property_value_is_still_refused_with_its_old_code(env):
    f = _write(env, "a.lts", BASE)
    with pytest.raises(EditRefused) as ei:
        add_node(f, "LB-NEW", "A new node", {"kind": "a\nb"})
    assert "multi-line value" in str(ei.value)
    p = env / "out" / "legacy.lts"
    with pytest.raises(EditRefused) as ei:
        add_node(p, "LB-NEW", "A new node", {"kind": "a\nb"}, create=True)
    assert (ei.value.evidence or {}).get("refusal") == "create_property_invalid" and not p.exists()


# ------------------------------------------------------------------------------- the first break is the one named
def test_crlf_is_refused_and_names_the_carriage_return_first(env):
    f = _write(env, "a.lts", BASE)
    for call in (lambda: set_property(f, "LB-ONE", "fresh", "a\r\nb"),
                 lambda: add_node(f, "LB-NEW", "t\r\nu title", {"kind": "x"})):
        with pytest.raises(EditRefused) as ei:
            call()
        ev = ei.value.evidence
        assert ev["refusal"] == CODE and ev["codepoint"] == "U+000D" and ev["index"] in (1,), ev
    assert f.read_text() == BASE


def test_the_first_break_is_named_and_the_index_counts_code_points(env):
    f = _write(env, "a.lts", BASE)
    with pytest.raises(EditRefused) as ei:
        set_property(f, "LB-ONE", "fresh", "🙂🙂 x y")
    assert ei.value.evidence["codepoint"] == "U+2029" and ei.value.evidence["index"] == 2
    assert f.read_text() == BASE


# -------------------------------------------------------------------------------------------------- controls
CONTROLS = ["alpha beta", "café", "emoji 🙂 ok", "tab\there", "nbsp here", "zwsp​here", "ideo　space",
            "colon: and [X-Y] and `code`", "  padded value  ", "x" * 3000]


@pytest.mark.parametrize("value", CONTROLS)
def test_ordinary_text_is_accepted_and_stored_unchanged(env, value):
    f = _write(env, "a.lts", BASE)
    r = set_property(f, "LB-ONE", "fresh", value)
    assert r.parsed_value == value.strip()
    r2 = set_property(f, "LB-ONE", "tag", value, mode="append")
    assert r2.action == "appended"
    r3 = set_property(f, "LB-ONE", "kind", value)
    assert r3.parsed_value == value.strip()
    n = add_node(f, "LB-NEW", "Title é 🙂 here", {"kind": value})
    assert n.properties["kind"] == value.strip()


def test_ordinary_space_in_a_key_is_still_accommodated(env):
    f = _write(env, "a.lts", BASE)
    r = set_property(f, "LB-ONE", "My Key", "v")
    assert r.key == "my-key" and r.coercions


def test_an_ordinary_repair_rename_remove_and_create_still_work(env):
    rf = _write(env, "r.lts", REPAIR_BASE)
    line = REPAIR_BASE.splitlines().index("- ⛔ FLAGGED: something broken") + 1
    r = repair_property(rf, "LB-ONE", line, "flagged", "café 🙂 value")
    assert r.parsed_value == "café 🙂 value"
    rename_property(rf, "LB-ONE", "tag", "label")
    remove_property(rf, "LB-ONE", "note", expect_value="first")
    p = env / "out" / "ok.lts"
    add_node(p, "LB-NEW", "Title é 🙂", {"kind": "café"}, create=True, doc_title="Doc é", domain="d", doctype="t")
    assert parse_text(p.read_text(), str(p)).nodes[0].properties["kind"] == "café"


# ---------------------------------------------------------------------- the set is DERIVED from the parser
def test_the_refusal_set_equals_the_set_the_parser_cuts_at(parser_breaks):
    from factlattice.authoring import linebreaks
    derived = frozenset(c for c in range(0x110000) if not 0xD800 <= c <= 0xDFFF
                        and linebreaks.is_line_break(chr(c)))
    assert derived == parser_breaks, (sorted(map(hex, derived - parser_breaks)),
                                      sorted(map(hex, parser_breaks - derived)))


def test_the_refusal_follows_the_parsers_own_splitting_function(monkeypatch):
    """The module calls the SAME function the parser calls, looked up at call time: change the parser's
    splitting and the refusal changes with it. A hand list could not do this."""
    from factlattice import parser
    from factlattice.authoring import linebreaks
    assert not linebreaks.is_line_break("§")
    real = parser.split_source_lines
    monkeypatch.setattr(parser, "split_source_lines", lambda text: [p for part in real(text) for p in part.split("§")])
    linebreaks.is_line_break.cache_clear() if hasattr(linebreaks.is_line_break, "cache_clear") else None
    assert linebreaks.is_line_break("§")
    assert linebreaks.first_line_break("ab§cd") == (2, "§")


def test_the_parser_reads_its_lines_through_the_function_the_refusal_uses(monkeypatch):
    """Companion to the above: patching the shared function changes what the PARSER does too, so the two
    cannot be reading different splitters."""
    from factlattice import parser
    monkeypatch.setattr(parser, "split_source_lines", lambda text: text.split("\n"))
    pf = parse_text("# D [SPEC:D-ROOT]\n\n## N [D-ONE]\n\n- k: a b\n", "<x>")
    assert next(n for n in pf.nodes if n.id == "D-ONE").properties["k"] == "a b"


# ------------------------------------------------------------------- defence in depth: the VALUES gate alone
@pytest.fixture()
def input_check_off(monkeypatch):
    """Switch the input refusal OFF everywhere it is called, so only the gate after the re-parse is left."""
    monkeypatch.setattr(edit, "refuse_line_breaks", lambda **kw: None)


def test_with_the_input_check_off_the_values_gate_still_refuses_every_cut(env, parser_breaks, input_check_off):
    f = _write(env, "a.lts", BASE)
    rf = _write(env, "r.lts", REPAIR_BASE)
    line = REPAIR_BASE.splitlines().index("- ⛔ FLAGGED: something broken") + 1
    n = 0
    for c in sorted(parser_breaks):
        if c == 0x0A:
            continue                                    # LF keeps its own older refusal; not the gate's case
        bad = f"alpha{chr(c)}beta"
        for what, call, files in (
                ("add", lambda: set_property(f, "LB-ONE", "fresh", bad), [f]),
                ("replace", lambda: set_property(f, "LB-ONE", "kind", bad), [f]),
                ("append", lambda: set_property(f, "LB-ONE", "tag", bad, mode="append"), [f]),
                ("repair", lambda: repair_property(rf, "LB-ONE", line, "flagged", bad), [rf]),
                ("add_node", lambda: add_node(f, "LB-NEW", "A new node", {"kind": bad}), [f])):
            _must_refuse(call, env, files, field="value", c=c, what=f"gate-only {what}", code=MISMATCH)
            n += 1
        p = env / "out" / f"gate{c}.lts"
        before = {str(x) for x in (env / "out").iterdir()}
        with pytest.raises(EditRefused) as ei:
            add_node(p, "LB-NEW", "A new node", {"kind": bad}, create=True)
        assert "requested_value" in (ei.value.evidence or {}), ei.value.evidence
        assert ei.value.evidence.get("refusal") == "create_gate_failed", ei.value.evidence   # F-R2-P1-1 / F-R2-P2-1
        assert {str(x) for x in (env / "out").iterdir()} == before
    assert n == 9 * 5


def test_the_values_gate_accepts_every_control_with_the_input_check_off(env, input_check_off):
    f = _write(env, "a.lts", BASE)
    for v in CONTROLS:
        assert set_property(f, "LB-ONE", "fresh", v).parsed_value == v.strip()
        assert set_property(f, "LB-ONE", "kind", v).parsed_value == v.strip()


def test_the_values_gate_does_not_refuse_a_replace_that_the_parser_extends_with_a_wrapped_tail(env):
    """Replacing a WRAPPED value keeps the old continuation lines attached to the new line (a known, older
    behaviour the round-6 note records). The gate must catch a CUT value and must not turn that into a refusal."""
    f = _write(env, "w.lts", "# D [SPEC:W-ROOT]\n\n## N [W-ONE]\n\n- k: first part\n  second part\n- j: z\n")
    r = set_property(f, "W-ONE", "k", "new value")
    assert r.parsed_value == "new value second part"


# -------------------------------------------------------------------------------------- the document writer
def _doc(**over):
    from factlattice.authoring.schema import Document, Node, Property
    from tests.test_sanctioned_surface import _fm
    value = over.pop("value", "measurement")
    title = over.pop("title", "An item with a long title")
    section = over.pop("section", "Body")
    fm = _fm()
    if "fm_title" in over:
        fm = fm.model_copy(update={"title": over.pop("fm_title")})
    return Document(frontmatter=fm, document_tag="SPEC", section=section,
                    nodes=[Node(node_id="D-ITEM", title=title, properties=[Property(key="kind", value=value)])])


def _preview(content, corpus=None):
    pf = parse_text(content, "draft.lts")
    return {"became": {"nodes": [{"id": n.id, "properties": dict(n.properties)} for n in pf.nodes],
                       "document_tag": pf.document_tag},
            "resolution": {"profile": {"available": True}}, "lost": []}


@pytest.mark.parametrize("where", ["value", "title", "section", "fm_title", "list value"])
@pytest.mark.parametrize("c", [0x0D, 0x0B, 0x0C, 0x1C, 0x85, 0x2028, 0x2029])
def test_write_document_refuses_a_line_break_in_any_text_it_renders(env, where, c):
    from factlattice.authoring.schema import Document, Node, Property, WriteRefused, write_document
    bad = f"abcdefgh{chr(c)}ij"
    if where == "list value":
        from tests.test_sanctioned_surface import _fm
        doc = Document(frontmatter=_fm(), document_tag="SPEC", section="Body",
                       nodes=[Node(node_id="D-ITEM", title="An item with a long title",
                                   properties=[Property(key="kind", value=["ok", bad])])])
    else:
        doc = _doc(**{where: bad})
    out = env / "out" / "doc.lts"
    with pytest.raises(WriteRefused) as ei:
        write_document(doc, str(out), _preview)
    assert f"U+{c:04X}" in str(ei.value)
    assert not out.exists()


def test_write_document_still_writes_ordinary_text(env):
    from factlattice.authoring.schema import write_document
    out = env / "out" / "doc.lts"
    write_document(_doc(value="café 🙂 ok"), str(out), _preview)
    assert "café 🙂 ok" in out.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------- the tool layer
def test_the_tool_layer_returns_a_coded_refusal_and_writes_nothing(env):
    from factlattice_engine import authoring as A
    f = _write(env, "a.lts", BASE)
    rf = _write(env, "r.lts", REPAIR_BASE)
    line = REPAIR_BASE.splitlines().index("- ⛔ FLAGGED: something broken") + 1
    calls = {
        "add_property": lambda: A.add_property(str(f), "LB-ONE", "fresh", "alpha beta"),
        "add_property append": lambda: A.add_property(str(f), "LB-ONE", "tag", "al pha", mode="append"),
        "repair_property": lambda: A.repair_property(str(rf), "LB-ONE", line, "flagged", "x\x0by"),
        "add_node": lambda: A.add_node(str(f), "LB-NEW", "A new node", {"kind": "x\x0cy"}),
        "add_node create": lambda: A.add_node(str(env / "out" / "c.lts"), "LB-NEW", "A title", {"kind": "x"},
                                              create=True),
        "add_node create doc_title": lambda: A.add_node(str(env / "out" / "c2.lts"), "LB-NEW", "A title",
                                                        {"kind": "x"}, create=True, doc_title="d\x85e"),
        "rename_property": lambda: A.rename_property(str(f), "LB-ONE", "tag", "la\x1cbel"),
        "remove_property": lambda: A.remove_property(str(f), "LB-ONE", "no\rte"),
    }
    for name, call in calls.items():
        before = _snapshot(env, f, rf)
        out = call()
        assert out["success"] is False and out["refused"] is True and out["written"] is False, (name, out)
        assert (out["evidence"] or {}).get("refusal") == CODE, (name, out["evidence"], out.get("reason"))
        assert _snapshot(env, f, rf) == before, name
    assert not (env / "out" / "c.lts").exists() and not (env / "out" / "c2.lts").exists()
    ok = A.add_property(str(f), "LB-ONE", "fresh", "alpha beta")
    assert ok["success"] is True and ok["value_as_parsed"] == "alpha beta"


# ------------------------------------------------------------------- round 2 (Opus Diamond on 376f32b)
def test_write_document_refusal_carries_the_same_code_and_evidence_as_the_edit_writers(env):
    """F-P1-1: SPEC 4.6.6 and SDLC [WRITER-LINE-BREAKS] say write_document refuses with code `line_break_in_input`
    and evidence naming the field, the code point and the index. The refusal must carry them, not only the text."""
    from factlattice.authoring.schema import WriteRefused, write_document
    out = env / "out" / "doc.lts"
    with pytest.raises(WriteRefused) as ei:
        write_document(_doc(value=PREFIX + " tail"), str(out), _preview)
    ev = getattr(ei.value, "evidence", None) or {}
    assert ev.get("refusal") == CODE, ev
    assert ev.get("codepoint") == "U+2028" and ev.get("index") == IDX, ev
    assert "value" in str(ev.get("field")), ev
    assert str(ei.value).startswith("NOT WRITTEN. "), str(ei.value)
    assert not out.exists()


def test_the_add_node_values_gate_refusal_is_coded_like_set_propertys(env, input_check_off):
    """F-P2-3: with the input check bypassed, the non-create add_node values gate still refuses; its refusal carries
    `evidence.refusal == "gate_failed"`, the code set_property's values gate uses, so no caller sees it uncoded."""
    f = _write(env, "a.lts", BASE)
    before = _snapshot(env, f)
    with pytest.raises(EditRefused) as ei:
        add_node(f, "LB-NEW", "A new node", {"kind": "alpha beta"})
    assert (ei.value.evidence or {}).get("refusal") == "gate_failed", ei.value.evidence
    assert "requested_value" in ei.value.evidence
    assert _snapshot(env, f) == before


def test_the_rename_remove_line_locator_splits_where_the_parser_splits(parser_breaks):
    """F-P3-1: `edit._split_keepends` locates lines for rename/remove; its line contents must be exactly the
    parser's lines for every character the parser breaks at (and for ordinary text)."""
    from factlattice.parser import split_source_lines
    texts = [f"a{chr(c)}b\nc" for c in sorted(parser_breaks)] + ["plain\nlines\n", "x\r\ny", "", "no eol"]
    for t in texts:
        contents, eols = edit._split_keepends(t)
        assert contents == split_source_lines(t), repr(t)
        assert "".join(c + e for c, e in zip(contents, eols)) == t, repr(t)


@pytest.mark.parametrize("tool", ["lts_add_property", "lts_add_node", "lts_repair_property"])
def test_the_writer_tool_descriptions_name_the_line_break_refusal(tool):
    """F-P2-2: the descriptions of the three writers that refuse a line break in a value name the refusal code.
    Control: rename/remove already list it through WRITER_REFUSAL_CODES."""
    from factlattice_engine import server
    desc = {t["name"]: t["description"] for t in server.TOOLS}
    assert CODE in desc["lts_rename_property"]
    assert CODE in desc[tool], tool


def test_write_document_refuses_an_indented_continuation_it_used_to_fold(env):
    """F-P2-1, disclosed in SPEC 4.6.6: earlier versions stored `"para one\\n  continued"` folded into one line; now it is
    refused like every other line break, with the code and evidence, and nothing is written."""
    from factlattice.authoring.schema import WriteRefused, write_document
    out = env / "out" / "doc.lts"
    with pytest.raises(WriteRefused) as ei:
        write_document(_doc(value="para one\n  continued"), str(out), _preview)
    ev = ei.value.evidence
    assert ev.get("refusal") == CODE and ev.get("codepoint") == "U+000A" and ev.get("index") == 8, ev
    assert not out.exists()


def test_the_tool_layer_reports_a_create_values_gate_refusal_as_create_gate_failed(env, input_check_off):
    """F-R2-P1-1: through the MCP adapter, a create whose values gate refuses must say `create_gate_failed`, not the
    adapter's fallback `create_refused`. SPEC 4.6.6 VALUES GATE names this code."""
    from factlattice_engine import authoring as A
    p = env / "out" / "c-gate.lts"
    out = A.add_node(str(p), "LB-NEW", "A title", {"kind": "alpha\u2028beta"}, create=True)
    assert out["success"] is False and out["refused"] is True, out
    assert (out["evidence"] or {}).get("refusal") == "create_gate_failed", (out["evidence"], out.get("reason"))
    assert not p.exists()
