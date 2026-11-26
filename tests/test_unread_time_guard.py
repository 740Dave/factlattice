"""The typed writers refuse an UNQUOTED placeholder clock time (`04:2xZ`), with the code `unread_time_placeholder`.

THE SHARED GUARD. One shared module (`factlattice/authoring/unread_time_shared.py`: detector, quoted-span rule and refusal text) is kept as a
byte-identical copy in each repository that calls it; the shared fixture `tests/data/unread_time_cases.json` is the one list of cases both
sides run. This file (1) runs the fixture through the detector, (2) pins the detector file's hash so an edit to
either copy turns a test red, (3) pins the caller's quoting rule, and (4) drives EVERY typed writer, through the adapter
and through the tool surface (`tools.dispatch`), to show each one refuses an unquoted token and writes nothing, and
accepts the same token quoted, and a real time.

A refusal is shown by bytes: the file is compared before and after, and the ledger folder must stay empty. A control
in the same setup writes, so the empty-ledger check can fail.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from factlattice.authoring import edit
from factlattice.authoring import unread_time_guard as guard
from factlattice.authoring import unread_time_shared as sh
from factlattice_engine import authoring
from factlattice_engine.tools import dispatch

REPO = Path(__file__).resolve().parent.parent
CASES = json.loads((REPO / "tests" / "data" / "unread_time_cases.json").read_text(encoding="utf-8"))
CODE = "unread_time_placeholder"
TOK = "04:2xZ"

#: sha256 of factlattice/authoring/unread_time_shared.py. The other repository's copy must hash the same. An edit to
#: the detector is a change to both halves: change both files and this pin together.
DETECTOR_SHA256 = "eaf250185838e26052e7217760b24a9195a164373cc1bddb9f224d1e2998e1be"


# ---------------------------------------------------------------------------------- the shared fixture, the detector
def test_the_shared_fixture_is_the_agreed_size():
    assert len(CASES["must_hit"]) == 24 and len(CASES["must_not_hit"]) == 24 and len(CASES["must_exempt"]) == 7


@pytest.mark.parametrize("text,expected", CASES["must_hit"], ids=[c[0] for c in CASES["must_hit"]])
def test_detector_must_hit(text, expected):
    assert [t for _, _, t in sh.placeholder_tokens(text)] == [expected]


@pytest.mark.parametrize("text", CASES["must_not_hit"], ids=CASES["must_not_hit"])
def test_detector_must_not_hit(text):
    assert sh.placeholder_tokens(text) == []


@pytest.mark.parametrize("text,expected", CASES["must_hit"], ids=[c[0] for c in CASES["must_hit"]])
def test_the_caller_reports_every_must_hit_unquoted(text, expected):
    assert [h.token for h in sh.find_unread_times(text)] == [expected]


@pytest.mark.parametrize("text", CASES["must_not_hit"], ids=CASES["must_not_hit"])
def test_the_caller_reports_no_must_not_hit(text):
    assert sh.find_unread_times(text) == []


def test_the_shared_file_is_pinned_byte_for_byte():
    got = hashlib.sha256((REPO / "factlattice/authoring/unread_time_shared.py").read_bytes()).hexdigest()
    assert got == DETECTOR_SHA256, "the shared detector changed: its twin in the other repository must change with it"


def test_the_shared_file_is_standard_library_only_and_keeps_the_regex():
    src = (REPO / "factlattice/authoring/unread_time_shared.py").read_text(encoding="utf-8")
    assert [l for l in src.splitlines() if l.startswith(("import ", "from "))] == ["import re", "from typing import NamedTuple"]
    assert "(?<=\\d{4}-\\d\\d-\\d\\d[Tt])" in src and "(?![\\w:])" in src


def test_the_quoting_exemption_is_the_callers_not_the_detectors():
    assert [t for _, _, t in sh.placeholder_tokens(f"`{TOK}`")] == [TOK]       # the detector still sees it
    assert sh.find_unread_times(f"`{TOK}`") == []                             # the caller exempts it


# ------------------------------------------------------------------------------------------- the quoting rule
@pytest.mark.parametrize("q", ["`", '"', "'"], ids=["backtick", "double", "single"])
def test_a_token_inside_a_pair_of_its_own_kind_is_exempt(q):
    assert sh.find_unread_times(f"landed at {q}{TOK}{q} ok") == []


@pytest.mark.parametrize("text", [
    f"landed at {TOK} ok",                         # in neither
    f"`other` and {TOK}",                          # an unquoted token beside a quoted one
    f"`{TOK}` and {TOK}",                          # the same, with the quoted one a token too
    f"`broken {TOK}",                              # an unpaired backtick before the token opens no span
    f"{TOK} trailing `x",                          # an unpaired quote after it
    f"`split\n{TOK}`",                             # a pair split over two lines
    f"\"{TOK}'",                                   # mismatched kinds do not pair
    f"({TOK})",                                    # parentheses are not a quote form
    f"```\n{TOK}\n```",                            # a code fence is not a quote form
])
def test_these_are_refused(text):
    assert [h.token for h in sh.find_unread_times(text)] == [TOK], text


def test_quotes_pair_left_to_right_per_kind():
    # one backtick pair, then a token after it: refused. A token in the SECOND pair of backticks: exempt.
    assert [h.token for h in sh.find_unread_times(f"`a` {TOK}")] == [TOK]
    assert sh.find_unread_times(f"`a` `{TOK}`") == []
    # a double-quote pair inside an unpaired backtick still exempts (each kind pairs on its own); a backtick does not close it
    assert sh.find_unread_times(f"`a \"{TOK}\" b") == []
    assert [h.token for h in sh.find_unread_times(f"\"a `{TOK} b\" {TOK}")] == [TOK]


# ------------------------------------------------------------------ an apostrophe inside a word is not a quote
def test_an_apostrophe_inside_a_word_pairs_with_nothing():
    # the two apostrophes would pair under a plain left-to-right rule and exempt the token between them
    assert sh.quoted_spans("Ann's note at 04:2xZ, it's done") == []
    assert [h.token for h in sh.find_unread_times("Ann's note at 04:2xZ, it's done")] == [TOK]


def test_a_backticked_token_stays_exempt_beside_word_apostrophes():
    assert sh.find_unread_times(f"Ann's note at `{TOK}`, it's done") == []


def test_a_token_in_a_plain_phrase_quote_stays_exempt():
    assert sh.find_unread_times(f"'like {TOK} here'") == []
    assert sh.find_unread_times(f"'like {TOK} here' and don't worry") == []   # a word apostrophe after a real pair


def test_a_pair_of_edge_quotes_pairs_around_a_word_apostrophe_but_the_plain_pairing_still_refuses():
    # the word-aware spans: only the two outer quotes pair; the "it's" inside is part of a word and is skipped
    assert sh.quoted_spans("'it's 04:2xZ'") == [(0, 12)]
    # but the plain pairing ends the span at "it's", so the token is refused: a documented limit (the word rule only adds refusals)
    assert [h.token for h in sh.find_unread_times("'it's 04:2xZ'")] == [TOK]
    assert [h.token for h in sh.find_unread_times(f"it's fine: 'like {TOK} here' and don't worry")] == [TOK]


def test_the_pinned_possessive_plural_case_is_a_hit():
    # "cats'" ends in an apostrophe with a space after it, so it is NOT inside a word and counts as a quote; "it's" is inside a
    # word and does not. One quote is left, nothing pairs, and the token is refused. (Before the rule, the two paired and exempted it.)
    assert sh.quoted_spans("cats' 04:2xZ and it's") == []
    assert [h.token for h in sh.find_unread_times("cats' 04:2xZ and it's")] == [TOK]


def test_the_remaining_limit_a_possessive_plural_and_a_later_quote_still_pair():
    # the KNOWN LIMIT in the shared file's header: both quotes are outside a word, so they pair and exempt the token
    assert sh.find_unread_times("cats' 04:2xZ and the boss' notes") == []


@pytest.mark.parametrize("text", ["5'10 at 04:2xZ'", "foo_'bar 04:2xZ baz'_qux"])
def test_the_word_class_is_w_so_digits_and_underscores_count(text):
    assert sh.quoted_spans(text) == []


def test_line_start_and_end_are_not_word_characters():
    assert sh.quoted_spans("'a b'") == [(0, 4)]


def test_double_quote_and_backtick_have_no_word_exception():
    assert sh.find_unread_times(f'say"{TOK}"now') == []
    assert sh.find_unread_times(f"say`{TOK}`now") == []


# ------------------------------------------------------------- the word rule can only add refusals (the old code is the bound)
def _plain_exempt(line):
    """The pre-rule behaviour, written out independently of the module: every quote of a kind counts."""
    spans = []
    for q in ("`", '"', "'"):
        at = [i for i, c in enumerate(line) if c == q]
        spans.extend(zip(at[0::2], at[1::2]))
    return {s for s, e, _ in sh.placeholder_tokens(line) if any(a < s and e <= b for a, b in spans)}


def _refused_starts(line):
    return {h.column for h in sh.find_unread_times(line)}


def test_a_word_apostrophe_cannot_shift_the_pairing_so_that_a_refused_token_is_exempted():
    line = f"the cats' note isn't done at {TOK}, the boss' call"
    assert _plain_exempt(line) == set()                      # the old pairing refuses it
    assert [h.token for h in sh.find_unread_times(line)] == [TOK]
    # the word-aware pairing alone WOULD exempt it: this is what the second pairing is for
    start = line.index(TOK)
    assert any(a < start and start + len(TOK) <= b for a, b in sh.quoted_spans(line))


def _generated_lines(n=4000, seed=20261125):
    import random
    rnd = random.Random(seed)
    pieces = ["'", "'", "'", "a", "s", "Ann", "it", " ", " ", "\u00e9", "\u65e5", "1", "_", "-", ".", "`", '"', TOK, TOK, "04:3?Z"]
    return ["".join(rnd.choice(pieces) for _ in range(rnd.randint(2, 18))) for _ in range(n)]


def test_new_refusals_are_a_superset_of_the_old_ones_over_a_seeded_set():
    lines = _generated_lines()
    with_token = [l for l in lines if sh.placeholder_tokens(l)]
    assert len(with_token) > 800                            # the set is not vacuous
    gained = 0
    for line in with_token:
        old_refused = {s for s, _, _ in sh.placeholder_tokens(line)} - _plain_exempt(line)
        new_refused = _refused_starts(line)
        assert old_refused <= new_refused, line
        gained += len(new_refused - old_refused)
    assert gained > 0                                        # the rule does refuse more somewhere


def test_the_differential_can_fail_a_word_aware_pairing_alone_loses_refusals():
    # the negative control: judging by quoted_spans alone (the first version of the rule) drops refusals on this same set
    lost = 0
    for line in (l for l in _generated_lines() if sh.placeholder_tokens(l)):
        old_refused = {s for s, _, _ in sh.placeholder_tokens(line)} - _plain_exempt(line)
        spans = sh.quoted_spans(line)
        word_refused = {s for s, e, _ in sh.placeholder_tokens(line) if not any(a < s and e <= b for a, b in spans)}
        lost += len(old_refused - word_refused)
    assert lost > 0


@pytest.mark.parametrize("text,refused", [
    ("'it's 04:2xZ'", True),            # one word apostrophe before the token: the plain pairing ends the span before it
    ("'04:2xZ it's'", False),          # none before it
    ("'don't, can't 04:2xZ'", False),  # two before it: the plain pairs (open, don't) and (can't, close)
], ids=["odd-before-refused", "none-before-exempt", "even-before-exempt"])
def test_a_phrase_quote_holding_word_apostrophes_is_refused_only_with_an_odd_number_before_the_token(text, refused):
    assert bool(sh.find_unread_times(text)) is refused


@pytest.mark.parametrize("text", CASES["must_exempt"], ids=CASES["must_exempt"])
def test_the_quoted_aware_call_exempts_every_must_exempt_row(text):
    assert [t for _, _, t in sh.placeholder_tokens(text)] != []         # the detector sees a token, so the row is a real control
    assert sh.find_unread_times(text) == []


# ------------------------------------------------ the word class is Unicode \w, and a line start is not a word character
@pytest.mark.parametrize("word", ["\u00e9", "\u00df", "\u65e5", "x", "7", "_"])
def test_a_unicode_word_character_on_both_sides_skips_the_apostrophe(word):
    line = f"'{word}'{word} 04:2xZ'"
    assert sh.quoted_spans(line) == [(0, len(line) - 1)]


def test_punctuation_beside_an_apostrophe_is_not_a_word_character():
    line = ".'04:2xZ'"
    assert sh.quoted_spans(line) == [(1, 8)]
    assert sh.find_unread_times(line) == []


def test_a_quote_at_line_start_opens_a_span_even_when_the_last_character_is_a_word_character():
    line = f"'{TOK}' ok"
    assert sh.quoted_spans(line) == [(0, 7)]
    assert sh.find_unread_times(line) == []


def test_the_refusal_advice_about_single_quotes_is_true():
    assert sh.find_unread_times(f"at'{TOK}'") != []                      # inside a word: they do not exempt
    advice = sh.refusal_text(sh.find_unread_times(TOK))
    assert "single quotes can fail, for example when an apostrophe inside a word (as in it's) comes before the token on its line" in advice
    assert "double or single quotes" not in advice
    assert sh.find_unread_times(f"it's 'like {TOK} here'") != []         # odd before the phrase: refused, as the advice says
    assert sh.find_unread_times(f"'{TOK} it's'") == []                    # none before: exempt


def test_a_span_never_crosses_a_line_and_positions_are_one_based_line_zero_based_column():
    hits = sh.find_unread_times(f"clean line\nat {TOK} and 21:3?Z")
    assert [(h.token, h.line, h.column) for h in hits] == [(TOK, 2, 3), ("21:3?Z", 2, 14)]


def test_the_quoting_exemption_can_be_switched_off_for_a_shell():
    assert [h.token for h in sh.find_unread_times(f"echo \"{TOK}\"", exempt_quoted=False)] == [TOK]


def test_the_refusal_text_names_tokens_and_both_remedies_and_is_itself_clean():
    hits = sh.find_unread_times(f"at {TOK} and 21:3?Z", "value")
    text = sh.refusal_text(hits)
    for must in (TOK, "21:3?Z", "line 1", "column 3", "`date -u`", "time unread", "backticks"):
        assert must in text, must
    assert sh.find_unread_times(text) == [], "the refusal text must not itself hold an unquoted placeholder"
    shell = sh.refusal_text(hits, shell=True)
    assert "Edit tool" in shell and "`date -u`" in shell and "time unread" in shell and "backticks" not in shell


# ------------------------------------------------------------------------------------------------ the writers
BASE = "# Doc [SPEC:UT-ROOT]\n\n## Node one [UT-ONE]\n\n- summary: first\n- note: first slip " + TOK + "\n"
BROKEN = BASE + "- ⛔ FLAGGED: kept\n"
BROKEN_LINE = BROKEN.splitlines().index("- ⛔ FLAGGED: kept") + 1


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home, state, corpus, out = (tmp_path / n for n in ("home", "state", "corpus", "out"))
    for d in (home, state, corpus, out):
        d.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(state))
    return tmp_path


def _doc(env, text=BROKEN, name="doc.lts"):
    f = env / "corpus" / name
    f.write_text(text, encoding="utf-8")
    return f


def _ledger(env):
    d = env / "state" / "ledger"
    return sorted(p.name for p in d.glob("*")) if d.exists() else []


def _snapshot(env):
    return {str(p.relative_to(env)): p.read_bytes() for p in env.rglob("*")
            if p.is_file() and "state" not in p.relative_to(env).parts[:1]} | {"ledger": _ledger(env)}


def _assert_refused(r, field_prefix):
    assert r["success"] is False and r["refused"] is True and r["written"] is False, r
    assert r["evidence"]["refusal"] == CODE, r
    assert r["evidence"]["field"].startswith(field_prefix), r["evidence"]
    assert r["evidence"]["hits"][0]["token"] == TOK and r["evidence"]["hits"][0]["line"] == 1
    assert TOK in r["reason"] or r["reason"] == CODE            # legacy writers carry the sentence, the new ones the code
    assert "date -u" in (r.get("explanation") or r["reason"])


def _cases(env):
    """name -> (call(value), field prefix). Each call builds on a fresh state, so every case sees the same file."""
    def f(): return str(_doc(env))
    def newfile(n): return str(env / "out" / n)
    return {
        "add_property": (lambda v: authoring.add_property(f(), "UT-ONE", "fresh", v), "value"),
        "add_property append": (lambda v: authoring.add_property(f(), "UT-ONE", "alias", v, mode="append"), "value"),
        "add_property replace": (lambda v: authoring.add_property(f(), "UT-ONE", "summary", v), "value"),
        "add_node title": (lambda v: authoring.add_node(f(), "UT-NEW", v, {"k": "x"}), "title"),
        "add_node property": (lambda v: authoring.add_node(f(), "UT-NEW", "A node", {"k": "ok", "kind": v}), "properties[kind]"),
        "add_node create title": (lambda v: authoring.add_node(newfile("a.lts"), "UT-NEW", v, {"k": "x"}, create=True), "title"),
        "add_node create property": (lambda v: authoring.add_node(newfile("b.lts"), "UT-NEW", "A node", {"kind": v}, create=True), "properties[kind]"),
        "add_node create doc_title": (lambda v: authoring.add_node(newfile("c.lts"), "UT-NEW", "A node", {"k": "x"}, create=True, doc_title=v), "doc_title"),
        "add_node create domain": (lambda v: authoring.add_node(newfile("d.lts"), "UT-NEW", "A node", {"k": "x"}, create=True, domain=v), "domain"),
        "add_node create doctype": (lambda v: authoring.add_node(newfile("e.lts"), "UT-NEW", "A node", {"k": "x"}, create=True, doctype=v), "doctype"),
        "repair_property": (lambda v: authoring.repair_property(f(), "UT-ONE", BROKEN_LINE, "flagged", v), "value"),
    }


def _names():
    return list(_cases(Path("/nonexistent")).keys())      # the lambdas are not called: only the case names are read


@pytest.mark.parametrize("name", _names())
def test_each_writer_refuses_an_unquoted_placeholder_and_writes_nothing(env, name):
    call, field = _cases(env)[name]
    _doc(env)
    before = _snapshot(env)
    r = call(f"landed at {TOK}")
    _assert_refused(r, field)
    assert _snapshot(env) == before, "a refused call changed a file or left a ledger record"


@pytest.mark.parametrize("q", ["`", '"', "'"], ids=["backtick", "double", "single"])
@pytest.mark.parametrize("name", [n for n in _names() if n not in ("add_node create domain", "add_node create doctype")])
def test_each_writer_accepts_the_token_quoted_in_each_quote_kind(env, name, q):
    call, _ = _cases(env)[name]
    _doc(env)
    r = call(f"landed at {q}{TOK}{q}")
    assert r["success"] is True and r["written"] is True, r
    assert _ledger(env), "control: a write leaves a ledger record, so the refusal checks above can fail"


@pytest.mark.parametrize("name", _names())
def test_each_writer_accepts_a_real_time(env, name):
    call, _ = _cases(env)[name]
    _doc(env)
    r = call("landed at 04:20Z" if name not in ("add_node create domain", "add_node create doctype") else "plt.t")
    assert r["success"] is True, r


def test_a_placeholder_in_a_key_or_id_cannot_exist_by_grammar(env):
    """Keys must match `[a-z][a-z0-9_-]*` and ids `[A-Z][A-Z0-9_-]*`: neither can hold a colon, so neither can hold a
    time. A key or id spelled with one is refused by the grammar's own check (never by this guard, and never written)."""
    f = _doc(env)
    before = _snapshot(env)
    r = authoring.add_property(str(f), "UT-ONE", "at 05:2xZ", "ok")
    assert r["success"] is False and (r["evidence"] or {}).get("refusal") != CODE, r
    r = authoring.add_node(str(f), "UT-N:2X", "A node")
    assert r["success"] is False and (r["evidence"] or {}).get("refusal") != CODE, r
    assert _snapshot(env) == before


# --------------------------------------------------------------------- delta only: text already in the file is not refused
def test_repair_with_no_value_keeps_an_old_slip_on_the_line_and_is_not_refused(env):
    f = _doc(env, BASE + f"- ⛔ FLAGGED: was {TOK}\n")
    r = authoring.repair_property(str(f), "UT-ONE", BROKEN_LINE, "flagged")
    assert r["success"] is True, r
    assert f"- flagged: was {TOK}" in f.read_text(encoding="utf-8")


def test_an_append_beside_an_old_slip_is_not_refused(env):
    f = _doc(env)
    r = authoring.add_property(str(f), "UT-ONE", "alias", "clean value", mode="append")
    assert r["success"] is True, r
    assert f"first slip {TOK}" in f.read_text(encoding="utf-8")       # the old slip stays; the guard never edits it


def test_a_whole_value_of_a_node_is_checked_line_by_line_and_a_clean_node_beside_an_old_slip_is_added(env):
    f = _doc(env)
    assert authoring.add_node(str(f), "UT-NEW", "Clean", {"k": "v"})["success"] is True


# ------------------------------------------------------------------------------- the document writer, and the surface
def _document(**over):
    from factlattice.authoring.schema import Document, Node, Property
    from tests.test_sanctioned_surface import _fm
    value = over.pop("value", "measurement")
    title = over.pop("title", "An item with a long title")
    return Document(frontmatter=_fm(**over), document_tag="SPEC", section="Body",
                    nodes=[Node(node_id="D-ITEM", title=title, properties=[Property(key="kind", value=value)])])


def _preview(content, corpus=None):
    from factlattice.parser import parse_text
    pf = parse_text(content, "draft.lts")
    return {"became": {"nodes": [{"id": n.id, "properties": dict(n.properties)} for n in pf.nodes],
                       "document_tag": pf.document_tag},
            "resolution": {"profile": {"available": True}}, "lost": []}


@pytest.mark.parametrize("where", ["value", "title", "fm_title"])
def test_write_document_refuses_an_unquoted_placeholder(env, where):
    from factlattice.authoring.schema import WriteRefused, write_document
    out = env / "out" / "doc.lts"
    kw = {"value": {"value": f"at {TOK}"}, "title": {"title": f"At {TOK}"}, "fm_title": {"title": f"At {TOK}"}}[where]
    doc = _document(**kw) if where != "fm_title" else _document(title=f"At {TOK}")
    with pytest.raises(WriteRefused) as ei:
        write_document(doc, str(out), _preview)
    assert ei.value.evidence["refusal"] == CODE and not out.exists()


def test_write_document_accepts_a_quoted_token_and_a_real_time(env):
    from factlattice.authoring.schema import write_document
    write_document(_document(value=f"at `{TOK}` and 04:20Z"), str(env / "out" / "ok.lts"), _preview)
    assert (env / "out" / "ok.lts").exists()


@pytest.mark.parametrize("tool,args_of", [
    ("lts_add_property", lambda f, v: {"path": f, "node_id": "UT-ONE", "key": "fresh", "value": v}),
    ("lts_add_node", lambda f, v: {"path": f, "node_id": "UT-NEW", "title": v}),
    ("lts_add_node", lambda f, v: {"path": f, "node_id": "UT-NEW", "title": "T", "properties": {"kind": v}}),
    ("lts_repair_property", lambda f, v: {"path": f, "node_id": "UT-ONE", "line": BROKEN_LINE, "key": "flagged", "value": v}),
], ids=["add_property", "add_node title", "add_node property", "repair_property"])
def test_the_refusal_is_reachable_through_the_tool_surface(env, tool, args_of):
    f = _doc(env)
    before = _snapshot(env)
    r = dispatch(tool, args_of(str(f), f"at {TOK}"))
    assert r["success"] is False and r["evidence"]["refusal"] == CODE, r
    assert _snapshot(env) == before
    ok = dispatch(tool, args_of(str(f), f"at `{TOK}`"))
    assert ok["success"] is True, ok


def test_the_code_is_known_on_both_writer_paths_and_documented():
    from factlattice_engine.server import TOOLS
    assert CODE in edit.WRITER_REFUSAL_CODES and CODE in edit.KNOWN_CREATE_CODES
    assert edit.UNREAD_TIME_CODE == CODE == guard.CODE
    by = {t["name"]: t["description"] for t in TOOLS}
    for tool in ("lts_add_property", "lts_add_node", "lts_repair_property", "lts_rename_property", "lts_remove_property"):
        assert f"`{CODE}`" in by[tool], tool
    spec = (REPO / "SPEC.md").read_text(encoding="utf-8")
    assert f"`{CODE}`" in spec[spec.index("#### 4.6.6"):spec.index("### 4.7")]


# ------------------------------------------------------------------ the shared file holds the shared logic, and only that
def test_the_shared_file_is_generic_and_the_caller_is_thin():
    src = (REPO / "factlattice/authoring/unread_time_shared.py").read_text(encoding="utf-8")
    import re
    assert not re.search(r"\b[A-Z]{2,}-[A-Z0-9][A-Z0-9-]+\b", src), "an id-shaped token in the shared file"
    for word in ("reference", "factlattice", "engine", "spec "):
        assert word not in src.lower(), word          # ships publicly and is identical in both repositories
    for name in ("placeholder_tokens", "quoted_spans", "find_unread_times", "refusal_text"):
        assert f"def {name}(" in src
    caller = (REPO / "factlattice/authoring/unread_time_guard.py").read_text(encoding="utf-8")
    assert "re.compile" not in caller and '"`"' not in caller and "date -u" not in caller, "logic leaked into the caller"


def test_a_shell_has_no_quote_exemption_and_its_text_names_the_edit_tool_not_backticks():
    hits = sh.find_unread_times(f"git commit -m \"landed at {TOK}\"", exempt_quoted=False)
    assert [h.token for h in hits] == [TOK]
    text = sh.refusal_text(hits, shell=True)
    assert "Edit tool" in text and "backticks" not in text and "date -u" in text and "time unread" in text


def test_a_set_that_resends_an_identical_slip_is_refused_because_a_set_replaces_the_value_so_all_of_it_is_added_text(env):
    f = _doc(env)                               # `note` already holds an old slip
    before = _snapshot(env)
    r = authoring.add_property(str(f), "UT-ONE", "note", f"first slip {TOK}")
    _assert_refused(r, "value")
    assert _snapshot(env) == before


# ------------------------------------------------------------- ledger metadata: reason, proposer, approver (every writer)
META = ["reason", "proposer", "approver", "via"]


def _meta_calls(env, **meta):
    f = str(_doc(env))
    return {
        "add_property": lambda: authoring.add_property(f, "UT-ONE", "fresh", "ok", **meta),
        "add_property append": lambda: authoring.add_property(f, "UT-ONE", "alias", "ok", mode="append", **meta),
        "add_node": lambda: authoring.add_node(f, "UT-NEW", "A node", {"k": "v"}, **meta),
        "add_node create": lambda: authoring.add_node(str(env / "out" / "m.lts"), "UT-NEW", "A node", {"k": "v"}, create=True, **meta),
        "repair_property": lambda: authoring.repair_property(f, "UT-ONE", BROKEN_LINE, "flagged", **meta),
        "rename_property": lambda: authoring.rename_property(f, "UT-ONE", "summary", "headline", **meta),
        "remove_property": lambda: authoring.remove_property(f, "UT-ONE", "summary", **meta),
    }


@pytest.mark.parametrize("field", META)
@pytest.mark.parametrize("writer", ["add_property", "add_property append", "add_node", "add_node create", "repair_property",
                                    "rename_property", "remove_property"])
def test_ledger_metadata_with_an_unquoted_placeholder_is_refused_and_nothing_is_written(env, writer, field):
    call = _meta_calls(env, **{field: (f"clerk:done at {TOK}" if field == "via" else f"done at {TOK}")})[writer]
    before = _snapshot(env)
    r = call()
    assert r["success"] is False and r["written"] is False and r["evidence"]["refusal"] == CODE, r
    assert r["evidence"]["field"] == field
    assert _snapshot(env) == before


@pytest.mark.parametrize("field", META)
def test_ledger_metadata_accepts_a_quoted_token_and_a_real_time(env, field):
    r = _meta_calls(env, **{field: (f"clerk:cites `{TOK}`" if field == "via" else f"cites `{TOK}` and 04:20Z")})["add_property"]()
    assert r["success"] is True, r


@pytest.mark.parametrize("meta", [{"reason": f"at {TOK}"}, {"proposer": f"at {TOK}"}, {"approver": f"at {TOK}"},
                                  {"via": f"clerk:q at {TOK}"}], ids=["reason", "proposer", "approver", "via"])
def test_write_whole_file_refuses_a_placeholder_in_its_ledger_metadata(env, meta):
    f = _doc(env)
    before = _snapshot(env)
    with pytest.raises(edit.EditRefused) as ei:
        edit.write_whole_file(f, BASE + "- extra: 1\n", **meta)
    assert ei.value.evidence["refusal"] == CODE and _snapshot(env) == before


# -------------------------------------------------------------------------------------------------- lts_report_miss
@pytest.fixture()
def miss(tmp_path, monkeypatch):
    """A real engine over a tiny corpus, as tests/test_report_miss.py builds one; the miss log path is returned."""
    from factlattice_engine import config as _config
    from factlattice_engine import querylog
    from factlattice_engine.tools import LtsEngine
    state = tmp_path / "state-log"
    monkeypatch.setenv(querylog.ENV_DIR, str(state))
    monkeypatch.setenv(querylog.ENV_LOG, str(tmp_path / "q.jsonl"))
    corpus = tmp_path / "corpus-r"
    corpus.mkdir()
    (corpus / "d.lts").write_text(BASE.replace(TOK, "04:20Z"), encoding="utf-8")
    reg = {"c": {"roots": [{"path": str(corpus), "area": "specs"}], "domain_suffix": "proj.c"}}
    return LtsEngine(_config.build_store(reg)), state / "miss-log.jsonl"


@pytest.mark.parametrize("field", ["query", "why", "found_via", "tool_used"])
def test_report_miss_refuses_an_unquoted_placeholder_in_each_free_text_field_and_writes_no_log(miss, field):
    e, log = miss
    kw = {"corpus": "c", "query": "first slip", "expected_ids": ["UT-ONE"], "why": "needed it"}
    kw[field] = f"at {TOK}"
    r = e.report_miss(**kw)
    assert r["success"] is False and r["reason"] == CODE and r["evidence"]["refusal"] == CODE, r
    assert r["evidence"]["field"] == field and TOK in r["error"]
    assert not log.exists(), "a refused report must not reach the miss log"


def test_report_miss_control_clean_and_quoted_text_is_filed(miss):
    e, log = miss
    r = e.report_miss(corpus="c", query="first slip", expected_ids=["UT-ONE"], why=f"cites `{TOK}` at 04:20Z")
    assert r["success"] is True, r
    assert log.exists() and f"`{TOK}`" in log.read_text(encoding="utf-8"), "control: a filed report reaches the log"


# --------------------------------------------------- the check runs on the arguments, BEFORE the lock and before any read
def _lock_files():
    d = edit._lock_dir()
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


@pytest.mark.parametrize("name,call", [
    ("add_property", lambda p: authoring.add_property(p, "UT-ONE", "fresh", f"at {TOK}")),
    ("add_property append", lambda p: authoring.add_property(p, "UT-ONE", "alias", f"at {TOK}", mode="append")),
    ("add_node title", lambda p: authoring.add_node(p, "UT-NEW", f"At {TOK}", {"k": "v"})),
    ("add_node property", lambda p: authoring.add_node(p, "UT-NEW", "T", {"k": f"at {TOK}"})),
    ("repair_property", lambda p: authoring.repair_property(p, "UT-ONE", 3, "flagged", f"at {TOK}")),
    ("rename_property metadata", lambda p: authoring.rename_property(p, "UT-ONE", "a", "b", reason=f"at {TOK}")),
    ("remove_property metadata", lambda p: authoring.remove_property(p, "UT-ONE", "a", via=f"clerk:at {TOK}")),
])
def test_a_refused_call_to_a_missing_path_returns_the_unread_code_and_leaves_no_lock_file(env, name, call):
    missing = str(env / "corpus" / "missing.lts")
    r = call(missing)
    assert r["success"] is False and r["evidence"]["refusal"] == CODE, r      # not lock_unavailable, not node_not_found
    assert _lock_files() == [], "the refusal came after the lock was taken"
    assert not (env / "corpus" / "missing.lts").exists()


def test_control_a_clean_call_to_a_missing_path_does_leave_a_lock_file(env):
    """The instrument can fire: without the early refusal the same call takes the lock (and then fails on the missing file)."""
    r = authoring.add_property(str(env / "corpus" / "missing.lts"), "UT-ONE", "fresh", "clean")
    assert r["success"] is False and (r.get("evidence") or {}).get("refusal") != CODE
    assert _lock_files() != []


# ------------------------------------------------- a line ends at every character the parser ends one at (shared file)
def test_the_shared_line_break_set_is_exactly_the_one_the_writers_refuse():
    from factlattice.authoring import linebreaks
    measured = {c for c in range(0x110000) if not 0xD800 <= c <= 0xDFFF and linebreaks.is_line_break(chr(c))}
    assert measured == {ord(c) for c in sh.LINE_BREAKS}


@pytest.mark.parametrize("c", list(sh.LINE_BREAKS), ids=[f"U+{ord(c):04X}" for c in sh.LINE_BREAKS])
def test_a_quote_pair_split_by_any_line_break_character_does_not_exempt(c):
    for q in ("`", '"', "'"):
        hits = sh.find_unread_times(f"{q}a{c}{TOK}{q}")
        assert [(h.token, h.line, h.column) for h in hits] == [(TOK, 2, 0)], (q, hex(ord(c)))


def test_crlf_is_one_line_break_and_line_numbers_follow_the_same_split():
    hits = sh.find_unread_times(f"a\r\nb\u2028c {TOK}\x0bd {TOK}")
    assert [(h.line, h.column) for h in hits] == [(3, 2), (4, 2)]
    assert sh.find_unread_times(f"`a {TOK}`\r\n`b {TOK}`") == []        # a pair on one line still exempts


def test_lowercase_z_and_lowercase_iso_t_are_placeholders_and_a_one_digit_hour_is_a_stated_limit():
    assert [h.token for h in sh.find_unread_times("at 04:2xz and 2026-11-21t04:2xZ")] == ["04:2xz", "04:2xZ"]
    assert sh.find_unread_times("04:20z 2026-11-21t04:20Z 3:2x speedup") == []
    assert sh.find_unread_times("at 4:2x") == [], "STATED LIMIT: a one-digit hour is not caught"


# ============================================================ FAIL OPEN: a fault in the detector never blocks a write
# The maintainer's ruling: a bug in the guard lets the write through and is recorded. A real hit is still refused.
FAULT = "unread_time_guard_fault"
SECRET = "SECRET-VALUE-04:20Z-in-the-message"       # a detector message may hold a value: it must reach no receipt, no ledger


class _DetectorBug(RuntimeError):
    pass


def _plant_faulty_detector(monkeypatch):
    def boom(*a, **k):
        raise _DetectorBug(SECRET)
    monkeypatch.setattr(sh, "find_unread_times", boom)       # the one call the guard makes into the shared detector


def _ledger_records(env):
    out = []
    for p in sorted((env / "state" / "ledger").glob("*")):
        out += [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


def _fault_records(env):
    return [r for r in _ledger_records(env) if r["verb"] == FAULT]


def _fault_warning(r):
    ws = [w for w in r.get("warnings", []) if FAULT in w]
    assert len(ws) == 1, r
    return ws[0]


def test_a_detector_fault_lets_the_write_land_with_a_receipt_code_and_one_ledger_record(env, monkeypatch, capsys):
    f = _doc(env)
    _plant_faulty_detector(monkeypatch)
    r = authoring.add_property(str(f), "UT-ONE", "fresh", f"landed at {TOK}")
    assert r["success"] is True and r["written"] is True, r
    assert f"fresh: landed at {TOK}" in f.read_text(encoding="utf-8"), "the write must land"
    w = _fault_warning(r)
    assert "_DetectorBug" in w and SECRET not in w and "SECRET" not in json.dumps(r), "class name only, never the message"
    recs = _ledger_records(env)
    faults = _fault_records(env)
    assert len(faults) == 1, recs
    rec = faults[0]
    assert [x["verb"] for x in recs] == ["set_property", FAULT], "the write's own record, then ONE fault record"
    assert rec["delta"] == {"exception_class": "_DetectorBug"} and SECRET not in json.dumps(rec)
    assert rec["path_real"] == str(f.resolve()) and rec["ts"].endswith("+00:00")
    assert rec["ledger_seq"] == recs[0]["ledger_seq"] + 1 and rec["prev_sha256"] is not None
    assert rec["file_sha256_before"] == rec["file_sha256_after"] == recs[0]["file_sha256_after"]
    assert rec["follows_out_of_band_edit"] is False
    with capsys.disabled():
        print("\nFAULT RECORD JSON: " + json.dumps(rec, ensure_ascii=False, sort_keys=False))


@pytest.mark.parametrize("name", _names())
def test_a_detector_fault_lets_every_writer_land_and_say_so(env, monkeypatch, name):
    call, _ = _cases(env)[name]
    _doc(env)
    _plant_faulty_detector(monkeypatch)
    r = call(f"landed at {TOK}" if name not in ("add_node create domain", "add_node create doctype") else "plt.t")
    assert r["success"] is True and r["written"] is True, r
    assert "_DetectorBug" in _fault_warning(r)
    assert len(_fault_records(env)) == 1


@pytest.mark.parametrize("writer", ["add_property", "add_node", "repair_property", "rename_property", "remove_property"])
def test_a_detector_fault_in_the_metadata_check_lets_the_write_land(env, monkeypatch, writer):
    call = _meta_calls(env, reason=f"done at {TOK}")[writer]
    _plant_faulty_detector(monkeypatch)
    r = call()
    assert r["success"] is True and "_DetectorBug" in _fault_warning(r), r
    assert len(_fault_records(env)) == 1, "one record per write, however many fields the detector faulted on"


def test_a_detector_fault_in_write_document_lets_it_land_and_is_recorded(env, monkeypatch):
    from factlattice.authoring.schema import write_document
    out = env / "out" / "doc.lts"
    _plant_faulty_detector(monkeypatch)
    r = write_document(_document(value=f"at {TOK}"), str(out), _preview)
    assert out.exists() and "_DetectorBug" in _fault_warning(r), r
    assert len(_fault_records(env)) == 1


def test_a_detector_fault_in_report_miss_files_the_report_and_says_so(miss, monkeypatch):
    e, log = miss
    _plant_faulty_detector(monkeypatch)
    r = e.report_miss(corpus="c", query=f"at {TOK}", expected_ids=["UT-ONE"], why="needed it")
    assert r["success"] is True and log.exists(), r
    assert "_DetectorBug" in _fault_warning(r) and SECRET not in json.dumps(r)


def test_a_detector_fault_reaches_the_receipt_through_the_tool_surface(env, monkeypatch):
    f = _doc(env)
    _plant_faulty_detector(monkeypatch)
    r = dispatch("lts_add_property", {"path": str(f), "node_id": "UT-ONE", "key": "fresh", "value": f"at {TOK}"})
    assert r["success"] is True and "_DetectorBug" in _fault_warning(r), r


def test_a_fault_does_not_leak_into_the_next_write(env, monkeypatch):
    f = _doc(env)
    with monkeypatch.context() as m:
        _plant_faulty_detector(m)
        authoring.add_property(str(f), "UT-ONE", "one", "x")
    r = authoring.add_property(str(f), "UT-ONE", "two", "y")
    assert r["success"] is True and not [w for w in r.get("warnings", []) if FAULT in w], r
    assert len(_fault_records(env)) == 1


def test_a_real_hit_is_still_refused_with_nothing_written_and_no_fault_record(env):
    f = _doc(env)
    before = _snapshot(env)
    r = authoring.add_property(str(f), "UT-ONE", "fresh", f"landed at {TOK}")
    _assert_refused(r, "value")
    assert _snapshot(env) == before
    assert _fault_records(env) == [] and guard.pending() == ()


def test_a_refusal_raised_on_purpose_is_not_caught_as_a_fault_even_when_a_second_field_faults(env, monkeypatch):
    f = _doc(env)
    real, calls = sh.find_unread_times, []

    def flaky(text, name="text", **kw):
        calls.append(name)
        if name != "value":
            raise _DetectorBug(SECRET)            # the other fields fault; the real hit in `value` must still refuse
        return real(text, name, **kw)
    monkeypatch.setattr(sh, "find_unread_times", flaky)
    before = _snapshot(env)
    r = authoring.add_property(str(f), "UT-ONE", "fresh", f"landed at {TOK}", reason="why")
    assert calls and r["success"] is False and r["evidence"]["refusal"] == CODE, r
    assert _snapshot(env) == before and _fault_records(env) == []


def test_the_fault_path_catches_nothing_raised_before_or_after_the_detector_call():
    """Structural: the guard has ONE try block, it holds exactly the one statement that calls the shared detector, and
    nothing else (no other call, no raise) is inside it; the refusal is built after it."""
    import ast
    tree = ast.parse((REPO / "factlattice/authoring/unread_time_guard.py").read_text(encoding="utf-8"))
    tries = [n for n in ast.walk(tree) if isinstance(n, ast.Try)]
    assert len(tries) == 1, "the guard must have exactly one try block, around the detector call"
    body = tries[0].body
    assert len(body) == 1 and isinstance(body[0], ast.Assign)
    calls = [n for n in ast.walk(body[0]) if isinstance(n, ast.Call)]
    assert len(calls) == 1 and calls[0].func.attr == "find_unread_times" and calls[0].func.value.id == "_shared"
    assert not [n for n in ast.walk(body[0]) if isinstance(n, ast.Raise)]
    assert len(tries[0].handlers) == 1 and not tries[0].orelse and not tries[0].finalbody
    assert ast.unparse(tries[0].handlers[0].type) == "Exception"          # not BaseException, not a bare except



# ----------------------------------------------------- the receipt says only what happened (a record, or plainly none)
RECORD_MADE = "a record was made in the write ledger"
NO_RECORD = "no ledger record was made, so this fault will not reach the maintainer's alert"


def test_the_receipt_says_a_record_was_made_when_one_was(env, monkeypatch):
    _plant_faulty_detector(monkeypatch)
    r = authoring.add_property(str(_doc(env)), "UT-ONE", "fresh", "x")
    w = _fault_warning(r)
    assert RECORD_MADE in w and NO_RECORD not in w and len(_fault_records(env)) == 1


def test_the_receipt_says_no_record_was_made_for_report_miss(miss, monkeypatch):
    e, log = miss
    _plant_faulty_detector(monkeypatch)
    r = e.report_miss(corpus="c", query="q", expected_ids=["UT-ONE"], why="needed it")
    w = _fault_warning(r)
    assert NO_RECORD in w and RECORD_MADE not in w


def test_the_receipt_says_no_record_was_made_when_the_ledger_skips_the_write(env, monkeypatch):
    f = _doc(env)
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", str(env / "corpus"))      # the target counts as scratch
    _plant_faulty_detector(monkeypatch)
    r = authoring.add_property(str(f), "UT-ONE", "fresh", "x")
    assert r["success"] is True and r["ledger"]["status"] == "skipped", r
    w = _fault_warning(r)
    assert NO_RECORD in w and RECORD_MADE not in w
    assert _ledger_records(env) == []


def test_the_receipt_says_no_record_was_made_when_the_fault_append_itself_fails(env, monkeypatch):
    from factlattice import ledger
    real_append = ledger.append

    def failing(real_path, fields, **kw):
        if fields.get("verb") == FAULT:
            raise ledger.LedgerError("planted")
        return real_append(real_path, fields, **kw)
    monkeypatch.setattr(ledger, "append", failing)
    _plant_faulty_detector(monkeypatch)
    r = authoring.add_property(str(_doc(env)), "UT-ONE", "fresh", "x")
    assert r["success"] is True and r["ledger"]["status"] == "ok", r
    w = _fault_warning(r)
    assert NO_RECORD in w and RECORD_MADE not in w
    assert _fault_records(env) == []


# --------------------------------------------------------------- the per-write fault list is cleared at entry and in finally
def test_a_fault_plus_a_refusal_in_one_call_does_not_leak_into_the_next_clean_write(env, monkeypatch):
    """The leak needs a fault AND a refusal in the same call: the refusal leaves the fault list unconsumed, and only the
    clears at entry and in `finally` stop the next write from inheriting it."""
    f = _doc(env)
    real = sh.find_unread_times

    def flaky(text, name="text", **kw):
        if name != "value":
            raise _DetectorBug(SECRET)
        return real(text, name, **kw)
    with monkeypatch.context() as m:
        m.setattr(sh, "find_unread_times", flaky)
        r = authoring.add_property(str(f), "UT-ONE", "fresh", f"landed at {TOK}", reason="why")
        assert r["success"] is False and r["evidence"]["refusal"] == CODE, r
    assert guard.pending() == (), "the refused call left a fault behind"
    r2 = authoring.add_property(str(f), "UT-ONE", "two", "clean")
    assert r2["success"] is True and not [w for w in r2.get("warnings", []) if FAULT in w], r2
    assert _fault_records(env) == []


# ------------------------------------------------------------------------------ the new verb in the ledger's vocabulary
def test_the_fault_verb_is_a_known_verb_and_not_an_edit_and_undo_refuses_it():
    from factlattice import ledger
    assert FAULT in ledger.KNOWN_VERBS and FAULT in ledger.NOT_AN_EDIT_VERBS
    with pytest.raises(ledger.LedgerError) as ei:
        ledger.check_undoable({"verb": FAULT, "ledger_seq": 2, "path_real": "/x.lts"})
    assert "not an edit" in str(ei.value) and "unknown ledger verb" not in str(ei.value)
    ledger.check_undoable({"verb": "set_property", "ledger_seq": 1, "path_real": "/x.lts"})     # an ordinary record still passes


# ------------------------------------------------------------------------------------------------------ the off switch
SWITCH = "FACTLATTICE_UNREAD_TIME_GUARD"


def _slip_write(env):
    return authoring.add_property(str(_doc(env)), "UT-ONE", "fresh", f"landed at {TOK}", reason="why")


def test_switch_off_lets_a_placeholder_write_land_and_the_receipt_says_off(env, monkeypatch):
    monkeypatch.setenv(SWITCH, "off")
    r = _slip_write(env)
    assert r["success"] is True and r["written"] is True, r
    assert r["unread_time_guard"] == "off", r
    assert f"fresh: landed at {TOK}" in (env / "corpus" / "doc.lts").read_text(encoding="utf-8")


def test_switch_off_is_on_the_receipt_of_every_typed_writer_and_through_the_tool_surface(env, monkeypatch):
    monkeypatch.setenv(SWITCH, "off")
    f = _doc(env)
    for name, r in {
        "rename": authoring.rename_property(str(f), "UT-ONE", "summary", "headline"),
        "remove": authoring.remove_property(str(f), "UT-ONE", "headline"),
        "add_node": authoring.add_node(str(f), "UT-NEW", f"A {TOK} node", {"k": "v"}),
        "dispatch": dispatch("lts_add_property", {"path": str(f), "node_id": "UT-ONE", "key": "x2", "value": f"at {TOK}"}),
    }.items():
        assert r["success"] is True and r["unread_time_guard"] == "off", (name, r)
    from factlattice.authoring.schema import write_document
    out = env / "out" / "doc.lts"
    assert write_document(_document(value=f"at {TOK}"), str(out), _preview)["unread_time_guard"] == "off"


def test_switch_off_does_not_stamp_a_receipt_when_it_is_on_or_unset(env, monkeypatch):
    for v in (None, "on", ""):
        if v is None:
            monkeypatch.delenv(SWITCH, raising=False)
        else:
            monkeypatch.setenv(SWITCH, v)
        r = authoring.add_property(str(_doc(env)), "UT-ONE", "fresh", "clean")
        assert "unread_time_guard" not in r and not [w for w in r.get("warnings", []) if "unread_time_guard" in w], r


@pytest.mark.parametrize("value", [None, "on", "ON", ""])
def test_switch_on_or_unset_still_refuses_the_placeholder(env, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(SWITCH, raising=False)
    else:
        monkeypatch.setenv(SWITCH, value)
    before = _snapshot(env)
    f = _doc(env)
    before = _snapshot(env)
    r = authoring.add_property(str(f), "UT-ONE", "fresh", f"landed at {TOK}")
    _assert_refused(r, "value")
    assert _snapshot(env) == before
    assert "unread_time_guard" not in r and not [w for w in r.get("warnings", []) if "unread_time_guard" in w], r


@pytest.mark.parametrize("value", ["of", "false", "0", "disabled", "offf", "no"])
def test_a_switch_typo_stays_refused_and_warns_naming_the_value(env, monkeypatch, value):
    monkeypatch.setenv(SWITCH, value)
    f = _doc(env)
    before = _snapshot(env)
    r = authoring.add_property(str(f), "UT-ONE", "fresh", f"landed at {TOK}")
    _assert_refused(r, "value")
    assert _snapshot(env) == before, "a typo must never let the write land"
    ws = [w for w in r.get("warnings", []) if "unread_time_guard" in w]
    assert len(ws) == 1 and repr(value) in ws[0] and "stays ON" in ws[0], r
    assert "unread_time_guard" not in r, "the state key says off only when the check was really off"


def test_a_switch_typo_warns_on_a_clean_write_too_and_report_miss_honours_the_switch(env, miss, monkeypatch):
    monkeypatch.setenv(SWITCH, "ofs")
    r = authoring.add_property(str(_doc(env)), "UT-ONE", "fresh", "clean")
    assert r["success"] is True and any("'ofs'" in w for w in r["warnings"]), r
    e, log = miss
    r = e.report_miss(corpus="c", query=f"at {TOK}", expected_ids=["UT-ONE"], why="needed it")
    assert r["success"] is False and r["reason"] == CODE and any("'ofs'" in w for w in r["warnings"]), r
    monkeypatch.setenv(SWITCH, "off")
    r = e.report_miss(corpus="c", query=f"at {TOK}", expected_ids=["UT-ONE"], why="needed it")
    assert r["success"] is True and r["unread_time_guard"] == "off" and log.exists(), r


def test_the_switch_is_read_with_a_literal_key_in_one_place_only():
    """Structural: the env-read guard needs a literal key, so the one read is os.environ.get("<literal>"), and no other
    module under factlattice/ or factlattice_engine/ reads the variable."""
    import ast
    src = (REPO / "factlattice/authoring/unread_time_guard.py").read_text(encoding="utf-8")
    reads = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr in ("get", "getenv") and ast.unparse(n.func.value) in ("os.environ", "os")]
    assert len(reads) == 1 and isinstance(reads[0].args[0], ast.Constant) and reads[0].args[0].value == SWITCH, \
        [ast.unparse(r) for r in reads]
    assert guard.ENV_NAME == SWITCH
    for p in sorted(list((REPO / "factlattice").rglob("*.py")) + list((REPO / "factlattice_engine").rglob("*.py"))):
        if p.name != "unread_time_guard.py":
            assert SWITCH not in p.read_text(encoding="utf-8", errors="replace") or p.name == "server.py", p


def test_the_switch_is_documented_in_the_spec_the_rule_and_every_writer_description():
    spec = (REPO / "SPEC.md").read_text(encoding="utf-8")
    sec = spec[spec.index("#### 4.6.6"):spec.index("### 4.7")]
    assert SWITCH in sec and "unread_time_guard: off" in sec
    sdlc = (REPO / "SDLC.md").read_text(encoding="utf-8")
    assert "[UNREAD-TIME-OFF-SWITCH-IS-ENV-ONLY-AND-VISIBLE]" in sdlc
    from factlattice_engine import server
    src = (REPO / "factlattice_engine/server.py").read_text(encoding="utf-8")
    assert src.count(SWITCH) == 6, "the five writer descriptions and lts_report_miss each name the switch once"
    for name in ("lts_add_node", "lts_add_property", "lts_repair_property", "lts_rename_property",
                 "lts_remove_property", "lts_report_miss"):
        desc = next(t["description"] for t in server.TOOLS if t["name"] == name)
        assert SWITCH in desc and "unread_time_guard" in desc, name
    assert "unread_time_guard: off" in spec


# ------------------------------------------------ write_whole_file clears the fault list at entry (the reviewer's sequence)
def test_write_whole_file_clears_the_fault_list_at_entry_and_when_refused_before_its_inner_finally(env, monkeypatch):
    f1, f2 = _doc(env, BASE.replace(TOK, "04:20Z"), "one.lts"), _doc(env, BASE.replace(TOK, "04:20Z"), "two.lts")
    real = sh.find_unread_times

    def fault_on_reason(text, name="", **kw):
        if name == "reason":
            raise _DetectorBug(SECRET)
        return real(text, name, **kw)

    def refuse(_real):
        raise edit.EditRefused("planted location refusal. Nothing written.", evidence={"refusal": "planted"})
    with monkeypatch.context() as m:
        m.setattr(sh, "find_unread_times", fault_on_reason)
        m.setattr(edit, "_check_location", refuse)
        with pytest.raises(edit.EditRefused):
            edit.write_whole_file(str(f1), f1.read_text(encoding="utf-8") + "- note: a\n", reason="why")
    assert guard.pending() == (), "the refused call left a fault pending"
    edit.write_whole_file(str(f2), f2.read_text(encoding="utf-8") + "- note: b\n")
    assert _fault_records(env) == [], "a fault from the refused call reached the next write"
    # entry clear, on its own: a stale fault already pending when a call starts is not this write's
    guard._faults().append("Stale")
    w = edit.write_whole_file(str(f2), f2.read_text(encoding="utf-8") + "- note: c\n")
    assert _fault_records(env) == [] and guard.pending() == (), w.ledger


def test_write_document_keeps_its_own_faults_through_the_nested_write_whole_file(env, monkeypatch):
    """Control for the nesting rule: write_document's checks run before it calls write_whole_file, and the entry clear of
    the inner call must not wipe them (the receipt and the one fault record still happen)."""
    from factlattice.authoring.schema import write_document
    out = env / "out" / "doc2.lts"
    _plant_faulty_detector(monkeypatch)
    r = write_document(_document(value=f"at {TOK}"), str(out), _preview)
    assert "_DetectorBug" in _fault_warning(r) and len(_fault_records(env)) == 1, r


def test_spec_464_says_undo_and_verify_tools_refuse_the_note_verb_not_skip_it():
    spec = (REPO / "SPEC.md").read_text(encoding="utf-8")
    sec = spec[spec.index("#### 4.6.4 "):spec.index("#### 4.6.4a")]
    assert "undo and verify tools REFUSE it" in sec and "undo and verify tools skip it" not in sec


# ------------------------------------------------------------------------------ contracts shared with Gnomon (SDLC 7.6)
def test_the_fault_record_is_found_by_verb_and_exception_class_in_the_per_file_ledger_path(env, monkeypatch):
    """The reader outside this repository finds the record by `verb`, reads `delta.exception_class`, and finds the file
    at <state_dir>/ledger/<sha256(realpath)[:32]>.jsonl. A real fault, no planted record."""
    import os
    f = _doc(env)
    _plant_faulty_detector(monkeypatch)
    authoring.add_property(str(f), "UT-ONE", "fresh", "x")
    want = env / "state" / "ledger" / (hashlib.sha256(os.path.realpath(f).encode("utf-8")).hexdigest()[:32] + ".jsonl")
    assert want.is_file(), sorted(p.name for p in (env / "state" / "ledger").glob("*"))
    recs = [json.loads(l) for l in want.read_text(encoding="utf-8").splitlines() if l.strip()]
    faults = [r for r in recs if r["verb"] == "unread_time_guard_fault"]
    assert len(faults) == 1 and faults[0]["delta"]["exception_class"] == "_DetectorBug", recs


def test_the_shared_contracts_are_named_in_the_spec_and_the_rule():
    spec = (REPO / "SPEC.md").read_text(encoding="utf-8")
    sec = spec[spec.index("#### 4.6.6"):spec.index("### 4.7")]
    sdlc = (REPO / "SDLC.md").read_text(encoding="utf-8")
    assert "[ENGINE-CONTRACTS-SHARED-WITH-GNOMON]" in sdlc
    for name in ("unread_time_shared.py", "FACTLATTICE_UNREAD_TIME_GUARD", "unread_time_guard_fault", "delta.exception_class",
                 DETECTOR_SHA256):
        assert name in sec, name
    assert sec.count("shared with Gnomon") >= 3
    assert DETECTOR_SHA256[:8] in sdlc[sdlc.index("[ENGINE-CONTRACTS-SHARED-WITH-GNOMON]"):][:2500]
