"""`lts_rename_property` and `lts_remove_property` -- the two typed writers that were missing.

WHY: the writers could ADD or REPLACE a property (`lts_add_property`) and REPAIR a line that did not
parse (`lts_repair_property`, which REFUSES a line that already parses). Nothing renamed or removed a
line that parses. The real case: 35 `- confidence: high` lines in a corpus must become
`- confidence-qualitative: high` with `confidence` absent. The only path was a hand edit, which is the
act the typed writers exist to prevent.

THE CONTRACT UNDER TEST
  rename: ONE key region on ONE line of ONE node changes; the value is byte-for-byte untouched; the
          whole-file parsed delta is EXACTLY {node: key loses that one value, new_key gains the
          identical value}.
  remove: ONE property line (and its folded continuation) goes; the whole-file parsed delta is
          EXACTLY that one value gone from that node.
  Both: per-file lock, atomic write with compare-and-swap, a backup, ONE ledger record, a refusal
          writes nothing and carries a fixed code in `evidence.refusal`.

⛔ IMPORTS OF THE NEW CODE ARE LAZY, so that before the feature exists each test fails FOR THE MISSING
FEATURE, one by one, and not as one collection error.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from factlattice import ledger
from factlattice.parser import parse_text

REPO = Path(__file__).resolve().parent.parent
CODE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")

NOTE = "Mixed Case SHA 0123456789abcdef0123456789ABCDEF ⛔ 🙂 colon: here: and https://x.y/z:1  "
DOC = f"""# Doc [SPEC:RR-ROOT]

- kind: root

## Node one [RR-ONE]

- confidence: high
- note: {NOTE}
- tag: a
- tag: b
- tag: c
- see-also: [RR-TWO]
- xref: [RR-TWO]
- nested:
  - x
  - y
- cont: first part
  second part

## Node two [RR-TWO]

- confidence: high
- kind: y
- tag: z

## Node three [RR-THREE]

- confidence: low
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    (tmp_path / "corpus").mkdir()
    f = tmp_path / "corpus" / "c.lts"
    f.write_text(DOC, encoding="utf-8")
    return f


def edit():
    from factlattice.authoring import edit as e
    return e


def rename(f, node, key, new_key, **kw):
    return edit().rename_property(f, node, key, new_key, **kw)


def remove(f, node, key, **kw):
    return edit().remove_property(f, node, key, **kw)


def props(f) -> dict:
    return {n.id: dict(n.properties) for n in parse_text(Path(f).read_bytes().decode("utf-8"), str(f)).nodes}


def line_of(f, text) -> int:
    for i, ln in enumerate(Path(f).read_text(encoding="utf-8").splitlines(), 1):
        if ln == text:
            return i
    raise AssertionError(text)


def recs(f) -> list:
    p = ledger.ledger_path(os.path.realpath(f))
    return [json.loads(x) for x in p.read_bytes().splitlines()] if p.exists() else []


def refusal_code(exc) -> str:
    return (exc.evidence or {}).get("refusal")


def assert_refused(f, code, fn, tmp_path):
    """A refusal: the named code, the file byte-identical, no backup, no ledger record."""
    before = Path(f).read_bytes()
    with pytest.raises(edit().EditRefused) as ei:
        fn()
    assert refusal_code(ei.value) == code, (refusal_code(ei.value), str(ei.value))
    assert CODE_RE.fullmatch(refusal_code(ei.value))
    assert Path(f).read_bytes() == before, "a refusal must write nothing"
    assert recs(f) == [], "a refusal must append no ledger record"
    bak = tmp_path / "bak"
    assert not bak.exists() or not any(bak.iterdir()), "a refusal must leave no backup"
    return ei.value


# ============================================================================== rename: the happy path
def test_rename_a_single_valued_key_moves_exactly_that_one_value(env):
    before_bytes = env.read_bytes().decode("utf-8")
    before = props(env)
    r = rename(env, "RR-TWO", "confidence", "confidence-qualitative")
    after = props(env)
    assert r.delta == {"RR-TWO": {"added": ["confidence-qualitative"], "removed": ["confidence"]}}
    assert "confidence" not in after["RR-TWO"] and after["RR-TWO"]["confidence-qualitative"] == "high"
    for nid in before:                                   # NOTHING else moved on any node
        if nid != "RR-TWO":
            assert after[nid] == before[nid], nid
    assert {k: v for k, v in after["RR-TWO"].items() if k != "confidence-qualitative"} == \
           {k: v for k, v in before["RR-TWO"].items() if k != "confidence"}
    # exactly ONE physical line changed, and only its key region
    a, b = before_bytes.splitlines(), env.read_bytes().decode("utf-8").splitlines()
    assert len(a) == len(b)
    assert [i for i in range(len(a)) if a[i] != b[i]] == [r.line_number - 1]
    assert r.line_before == "- confidence: high" and r.line_after == "- confidence-qualitative: high"


def test_rename_a_list_valued_key_needs_and_honours_line(env):
    ln = line_of(env, "- tag: b")
    r = rename(env, "RR-ONE", "tag", "label", line=ln)
    after = props(env)["RR-ONE"]
    assert after["tag"] == ["a", "c"] and after["label"] == "b"
    assert r.delta["RR-ONE"]["added"] == ["label"] and r.delta["RR-ONE"]["removed"] == []
    assert r.delta["RR-ONE"]["changed"]["tag"] == {"removed": ["b"], "added": []}
    assert list(r.delta) == ["RR-ONE"]
    assert r.line_number == ln


def test_rename_the_ambiguous_case_is_refused_and_names_the_lines(env, tmp_path):
    ex = assert_refused(env, "key_ambiguous", lambda: rename(env, "RR-ONE", "tag", "label"), tmp_path)
    lines = ex.evidence["lines"]
    assert lines == [line_of(env, "- tag: a"), line_of(env, "- tag: b"), line_of(env, "- tag: c")]
    for n in lines:
        assert str(n) in str(ex)


def test_rename_with_a_line_that_is_not_that_keys_line_is_refused(env, tmp_path):
    assert_refused(env, "line_not_key_line",
                   lambda: rename(env, "RR-ONE", "tag", "label", line=line_of(env, "- confidence: high")), tmp_path)
    # a real `tag` line, but on a DIFFERENT node
    assert_refused(env, "line_not_key_line",
                   lambda: rename(env, "RR-ONE", "tag", "label", line=line_of(env, "- tag: z")), tmp_path)


def test_rename_onto_an_existing_key_is_refused_no_silent_merge(env, tmp_path):
    ex = assert_refused(env, "new_key_exists", lambda: rename(env, "RR-TWO", "kind", "confidence"), tmp_path)
    assert "merge" in str(ex).lower()


def test_rename_a_key_that_heads_a_nested_block_is_refused(env, tmp_path):
    ex = assert_refused(env, "key_nested_block", lambda: rename(env, "RR-ONE", "nested", "nested-list"), tmp_path)
    assert "nested" in str(ex).lower()


def test_rename_a_missing_key_or_node_is_refused_with_its_own_code(env, tmp_path):
    assert_refused(env, "key_absent", lambda: rename(env, "RR-TWO", "nope", "other"), tmp_path)
    assert_refused(env, "node_not_found", lambda: rename(env, "RR-NOPE", "confidence", "other"), tmp_path)
    assert_refused(env, "not_a_file", lambda: rename(env.parent / "missing.lts", "RR-TWO", "confidence", "o"), tmp_path)


def test_rename_to_the_same_key_after_normalisation_is_refused(env, tmp_path):
    assert_refused(env, "new_key_same", lambda: rename(env, "RR-TWO", "confidence", "confidence"), tmp_path)
    assert_refused(env, "new_key_same", lambda: rename(env, "RR-TWO", "confidence", "**Confidence**"), tmp_path)


def test_a_value_with_mixed_case_sha_pictographs_and_colons_stays_byte_identical(env):
    before = env.read_bytes().decode("utf-8")
    r = rename(env, "RR-ONE", "note", "remark")
    after = env.read_bytes().decode("utf-8")
    assert after == before.replace(f"- note: {NOTE}", f"- remark: {NOTE}", 1)
    assert after.count(NOTE) == 1 and f"- remark: {NOTE}" in after          # incl. the two trailing spaces
    assert props(env)["RR-ONE"]["remark"] == props_text_value(before, "RR-ONE", "note")
    assert r.delta == {"RR-ONE": {"added": ["remark"], "removed": ["note"]}}


def props_text_value(text, nid, key):
    return {n.id: n.properties for n in parse_text(text, "x").nodes}[nid][key]


def test_a_folded_continuation_value_is_kept_whole_on_rename(env):
    rename(env, "RR-ONE", "cont", "cont2")
    t = env.read_text(encoding="utf-8")
    assert "- cont2: first part\n  second part\n" in t
    assert props(env)["RR-ONE"]["cont2"] == "first part second part"


def test_new_key_coercion_is_recorded(env):
    r = rename(env, "RR-TWO", "confidence", "Confidence Qualitative")
    assert r.key == "confidence" and r.new_key == "confidence-qualitative"
    assert r.coercions, "the coercions must be recorded in the receipt"
    assert r.new_key_as_authored == "Confidence Qualitative"
    assert props(env)["RR-TWO"]["confidence-qualitative"] == "high"


def test_a_digit_initial_new_key_is_refused_it_needs_an_invented_prefix(env, tmp_path):
    ex = assert_refused(env, "new_key_illegal", lambda: rename(env, "RR-TWO", "confidence", "003-conf"), tmp_path)
    assert "DIGIT" in str(ex)


# ----------------------------------------------------------------------------- rename: the edge rule
def test_renaming_a_minting_key_away_is_refused_without_the_flag_and_listed_with_it(env, tmp_path):
    assert any(e.type == "RELATED_TO" and e.source == "RR-ONE" and e.target == "RR-TWO"
               for e in parse_text(env.read_text(encoding="utf-8"), "x").edges)
    ex = assert_refused(env, "edge_change_refused",
                        lambda: rename(env, "RR-ONE", "see-also", "see-also-note"), tmp_path)
    assert ex.evidence["edges_removed"] and not ex.evidence["edges_added"]
    assert ex.evidence["edges_removed"][0]["type"] == "RELATED_TO"
    assert "allow_edge_change" in str(ex)
    r = rename(env, "RR-ONE", "see-also", "see-also-note", allow_edge_change=True)
    assert [(e["source"], e["type"], e["target"]) for e in r.edges_removed] == [("RR-ONE", "RELATED_TO", "RR-TWO")]
    assert r.edges_added == []
    assert not any(e.type == "RELATED_TO" and e.source == "RR-ONE"
                   for e in parse_text(env.read_text(encoding="utf-8"), "x").edges)


def test_renaming_a_plain_key_into_a_minting_key_is_refused_without_the_flag(env, tmp_path):
    ex = assert_refused(env, "edge_change_refused", lambda: rename(env, "RR-ONE", "xref", "depends-on"), tmp_path)
    assert ex.evidence["edges_added"] and not ex.evidence["edges_removed"]
    r = rename(env, "RR-ONE", "xref", "depends-on", allow_edge_change=True)
    assert [(e["source"], e["type"], e["target"]) for e in r.edges_added] == [("RR-ONE", "DEPENDS_ON", "RR-TWO")]


def test_renaming_between_two_minting_keys_that_mint_the_same_edge_changes_no_edge(env):
    # see-also and relates-to both mint RELATED_TO: the edge multiset is unchanged, so no flag is needed
    r = rename(env, "RR-ONE", "see-also", "relates-to")
    assert r.edges_added == [] and r.edges_removed == []
    assert props(env)["RR-ONE"]["relates-to"] == "[RR-TWO]"


# ============================================================================== remove
def test_remove_a_single_valued_key(env):
    before = props(env)
    r = remove(env, "RR-THREE", "confidence")
    after = props(env)
    assert r.delta == {"RR-THREE": {"added": [], "removed": ["confidence"]}}
    assert "confidence" not in after["RR-THREE"]
    for nid in before:
        if nid != "RR-THREE":
            assert after[nid] == before[nid]
    assert r.removed_lines == ["- confidence: low"]
    assert r.line_number == 1 + DOC.splitlines().index("- confidence: low")


def test_remove_expect_value_matching_and_mismatching(env, tmp_path):
    assert_refused(env, "expect_value_mismatch",
                   lambda: remove(env, "RR-TWO", "confidence", expect_value="low"), tmp_path)
    assert_refused(env, "expect_value_mismatch",
                   lambda: remove(env, "RR-TWO", "confidence", expect_value="HIGH"), tmp_path)
    assert_refused(env, "expect_value_mismatch",
                   lambda: remove(env, "RR-TWO", "confidence", expect_value="hig"), tmp_path)
    r = remove(env, "RR-TWO", "confidence", expect_value="high")
    assert "confidence" not in props(env)["RR-TWO"] and r.value_removed == "high"


def test_remove_a_list_element_needs_line_and_removes_exactly_that_one(env, tmp_path):
    assert_refused(env, "key_ambiguous", lambda: remove(env, "RR-ONE", "tag"), tmp_path)
    ln = line_of(env, "- tag: b")
    r = remove(env, "RR-ONE", "tag", line=ln, expect_value="b")
    assert props(env)["RR-ONE"]["tag"] == ["a", "c"]
    assert r.delta == {"RR-ONE": {"added": [], "removed": [], "changed": {"tag": {"removed": ["b"], "added": []}}}}


def test_remove_expect_value_is_checked_against_the_chosen_line_not_the_key(env, tmp_path):
    assert_refused(env, "expect_value_mismatch",
                   lambda: remove(env, "RR-ONE", "tag", line=line_of(env, "- tag: b"), expect_value="a"), tmp_path)


def test_remove_takes_the_folded_continuation_with_it(env):
    r = remove(env, "RR-ONE", "cont")
    assert "second part" not in env.read_text(encoding="utf-8")
    assert r.removed_lines == ["- cont: first part", "  second part"]
    assert "cont" not in props(env)["RR-ONE"]
    assert r.value_removed == "first part second part"


def test_remove_a_nested_block_head_is_refused(env, tmp_path):
    assert_refused(env, "key_nested_block", lambda: remove(env, "RR-ONE", "nested"), tmp_path)


def test_remove_refusals_missing_and_wrong_line(env, tmp_path):
    assert_refused(env, "key_absent", lambda: remove(env, "RR-TWO", "nope"), tmp_path)
    assert_refused(env, "line_not_key_line",
                   lambda: remove(env, "RR-TWO", "confidence", line=line_of(env, "- confidence: low")), tmp_path)
    assert_refused(env, "node_not_found", lambda: remove(env, "RR-NOPE", "confidence"), tmp_path)


def test_remove_a_minting_key_is_refused_without_the_flag_and_listed_with_it(env, tmp_path):
    ex = assert_refused(env, "edge_change_refused", lambda: remove(env, "RR-ONE", "see-also"), tmp_path)
    assert [e["type"] for e in ex.evidence["edges_removed"]] == ["RELATED_TO"]
    r = remove(env, "RR-ONE", "see-also", allow_edge_change=True)
    assert [(e["source"], e["target"]) for e in r.edges_removed] == [("RR-ONE", "RR-TWO")]


# ============================================================================== the gate catches a wrong line
def _wrong_line_locator(real):
    """A MUTANT of the locator: finds the line in the NEIGHBOUR node that carries the same key."""
    def mutant(lines, start0, end0, key, **kw):
        for i, ln in enumerate(lines):
            if ln.startswith(f"- {key}:") and not (start0 < i < end0):
                return [(i, False)]
        return []
    return mutant


@pytest.mark.parametrize("op", ["rename", "remove"])
def test_a_mutant_that_edits_the_neighbour_nodes_line_is_caught_by_the_gate(env, monkeypatch, op):
    """The planted neighbour: RR-ONE and RR-TWO both carry `confidence: high`. With the locator mutated to
    return the NEIGHBOUR's line, the parsed delta is on the wrong node, and the gate must refuse."""
    e = edit()
    assert hasattr(e, "_key_lines"), "the locator seam must exist for the mutant to patch"
    before = env.read_bytes()

    def call():
        if op == "rename":
            return rename(env, "RR-ONE", "confidence", "confidence-qualitative")
        return remove(env, "RR-ONE", "confidence")
    with monkeypatch.context() as m:
        m.setattr(e, "_key_lines", _wrong_line_locator(e))
        with pytest.raises(e.EditRefused) as ei:
            call()
    assert refusal_code(ei.value) == "gate_failed", str(ei.value)
    assert env.read_bytes() == before and recs(env) == []
    # and the SAME call without the mutant lands on the right node, leaving the neighbour alone
    call()
    after = props(env)
    assert after["RR-TWO"]["confidence"] == "high"                         # the neighbour did NOT move
    assert "confidence" not in after["RR-ONE"]
    assert len(recs(env)) == 1


def test_a_mutant_that_corrupts_the_value_is_caught_by_the_gate(env, monkeypatch):
    """A rewrite that changes the value as well as the key must not pass: patch the key-region splice."""
    e = edit()
    assert hasattr(e, "_splice_key"), "the splice seam must exist for the mutant to patch"
    orig = e._splice_key
    monkeypatch.setattr(e, "_splice_key", lambda content, new_key: orig(content, new_key) + "X")
    with pytest.raises(e.EditRefused) as ei:
        rename(env, "RR-TWO", "confidence", "confidence-qualitative")
    assert refusal_code(ei.value) == "gate_failed"


# ============================================================================== bytes: nothing else moves
def test_crlf_and_exotic_line_separators_survive_byte_for_byte(env):
    """The existing writers read with universal newlines and re-join with LF, which would rewrite every CRLF
    in the file, and `str.splitlines` also splits on U+2028, U+000C and U+0085 -- re-joined as LF, a prose
    line containing one is silently changed. The new writers edit one line and keep every other byte."""
    text = ("# Doc [SPEC:RR-ROOT]\r\n\r\n## Node [RR-ONE]\r\n\r\n- confidence: high\r\n- note: a\r\n\r\n"
            "prose with a line separator and a form\x0cfeed and a next\x85line\r\n\r\n## Two [RR-TWO]\r\n\r\n- k: v\r\n")
    env.write_bytes(text.encode("utf-8"))
    rename(env, "RR-ONE", "confidence", "confidence-qualitative")
    after = env.read_bytes().decode("utf-8")
    assert after == text.replace("- confidence: high", "- confidence-qualitative: high", 1)
    remove(env, "RR-TWO", "k")
    assert env.read_bytes().decode("utf-8") == after.replace("- k: v\r\n", "", 1)


def test_a_file_without_a_trailing_newline_keeps_none(env):
    env.write_bytes(b"# D [SPEC:RR-ROOT]\n\n## N [RR-ONE]\n\n- confidence: high")
    rename(env, "RR-ONE", "confidence", "confidence-qualitative")
    assert env.read_bytes() == b"# D [SPEC:RR-ROOT]\n\n## N [RR-ONE]\n\n- confidence-qualitative: high"


# ============================================================================== ledger, backup, receipt
def test_one_ledger_record_per_rename_with_before_and_after_lines(env):
    r = rename(env, "RR-TWO", "confidence", "confidence-qualitative",
               proposer="P", approver="A", reason="DS convention", via="direct")
    (rec,) = recs(env)
    assert rec["verb"] == "rename_property" and rec["node_id"] == "RR-TWO" and rec["key"] == "confidence"
    assert [d["line"] for d in rec["discarded"]] == ["- confidence: high"]
    assert rec["discarded"][0]["line_number"] == r.line_number
    assert rec["inserted"] == {"line_number": r.line_number, "lines": ["- confidence-qualitative: high"]}
    assert rec["delta"] == r.delta
    assert (rec["proposer"], rec["approver"], rec["reason"], rec["via"]) == ("P", "A", "DS convention", "direct")
    assert rec["backup_path"] == r.backup_path
    assert rec["file_sha256_before"] != rec["file_sha256_after"]
    assert r.ledger["status"] == "ok"


def test_one_ledger_record_per_remove_with_the_removed_lines(env):
    r = remove(env, "RR-ONE", "cont")
    (rec,) = recs(env)
    assert rec["verb"] == "remove_property" and rec["node_id"] == "RR-ONE" and rec["key"] == "cont"
    assert rec["discarded"][0]["line"] == "- cont: first part\n  second part"
    assert rec["discarded"][0]["line_number"] == r.line_number
    assert rec["inserted"] is None and rec["delta"] == r.delta


def test_the_new_verbs_are_known_to_the_ledger_so_undo_does_not_refuse_them():
    assert {"rename_property", "remove_property"} <= set(ledger.KNOWN_VERBS)
    for v in ("rename_property", "remove_property"):
        ledger.check_undoable({"verb": v})


def test_the_backup_is_the_pre_edit_file(env):
    before = env.read_bytes()
    r = rename(env, "RR-TWO", "confidence", "confidence-qualitative")
    assert Path(r.backup_path).read_bytes() == before
    before2 = env.read_bytes()
    r2 = remove(env, "RR-THREE", "confidence")
    assert Path(r2.backup_path).read_bytes() == before2 and r2.backup_path != r.backup_path


# ============================================================================== lock, CAS, atomicity
def test_a_busy_lock_refuses_both_writers_and_writes_nothing(env, tmp_path, monkeypatch):
    e = edit()
    monkeypatch.setenv("FACTLATTICE_WRITE_LOCK_TIMEOUT", "0.2")
    got, release = threading.Event(), threading.Event()

    def holder():
        with e._file_lock(Path(os.path.realpath(env))):
            got.set()
            release.wait(10)
    t = threading.Thread(target=holder)
    t.start()
    assert got.wait(5)
    try:
        for fn in (lambda: rename(env, "RR-TWO", "confidence", "c2"), lambda: remove(env, "RR-THREE", "confidence")):
            before = env.read_bytes()
            with pytest.raises(e.EditRefused) as ei:
                fn()
            assert refusal_code(ei.value) == "lock_busy"
            assert env.read_bytes() == before
    finally:
        release.set()
        t.join()


@pytest.mark.parametrize("op", ["rename", "remove"])
def test_a_writer_outside_the_lock_between_read_and_replace_is_not_overwritten(env, monkeypatch, op):
    """The compare-and-swap, reached through the new writers: a hand edit landing after the candidate was
    computed must make the write refuse, not be lost."""
    e = edit()

    def intruder(real):
        Path(real).write_bytes(Path(real).read_bytes() + b"\n- hand: edit\n")
    monkeypatch.setattr(e, "_BEFORE_REPLACE_HOOK", intruder)
    with pytest.raises(e.EditRefused) as ei:
        rename(env, "RR-TWO", "confidence", "c2") if op == "rename" else remove(env, "RR-THREE", "confidence")
    assert "changed on disk" in str(ei.value)
    assert env.read_bytes().endswith(b"- hand: edit\n")
    assert props(env)["RR-TWO"]["confidence"] == "high" and "c2" not in props(env)["RR-TWO"]


_CHILD = textwrap.dedent("""
    import json, sys, time
    sys.path.insert(0, {repo!r})
    from factlattice.authoring import edit
    path, op, start = sys.argv[1], sys.argv[2], float(sys.argv[3])
    while time.time() < start:
        pass
    try:
        if op == "rename":
            edit.rename_property(path, "RR-ONE", "confidence", "confidence-qualitative")
        elif op == "remove":
            edit.remove_property(path, "RR-TWO", "kind")
        else:
            edit.set_property(path, "RR-THREE", "added", "yes")
        print(json.dumps({{"ok": True}}))
    except edit.EditRefused as e:
        print(json.dumps({{"ok": False, "refused": str(e)[:200]}}))
""")


def test_three_processes_rename_remove_and_add_from_the_same_base_lose_no_update(tmp_path):
    lost = []
    for i in range(8):
        d = tmp_path / f"round{i}"
        (d / "corpus").mkdir(parents=True)
        f = d / "corpus" / "c.lts"
        f.write_text(DOC, encoding="utf-8")
        envv = dict(os.environ, FACTLATTICE_BACKUP_DIR=str(d / "bak"), FACTLATTICE_STATE_DIR=str(d / "state"),
                    LTS_ENGINE_STATE_DIR=str(d / "state"))
        start = time.time() + 1.5
        procs = [subprocess.Popen([sys.executable, "-c", _CHILD.format(repo=str(REPO)), str(f), op, str(start)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=envv)
                 for op in ("rename", "remove", "add")]
        outs = [json.loads(p.communicate(timeout=60)[0].strip().splitlines()[-1]) for p in procs]
        p = props(f)
        ok = (p["RR-ONE"].get("confidence-qualitative") == "high" and "confidence" not in p["RR-ONE"]
              and "kind" not in p["RR-TWO"] and p["RR-THREE"].get("added") == "yes")
        if not ok or not all(o["ok"] for o in outs):
            lost.append((i, outs, p))
    assert not lost, f"lost/refused updates in {len(lost)}/8 rounds: {lost[:1]}"


# ============================================================================== the MCP adapter + dispatch
def dispatch(name, args):
    from factlattice_engine import tools
    return tools.dispatch(name, args, tools=None)


def test_adapter_rename_compact_receipt_keeps_the_proof(env):
    out = dispatch("lts_rename_property", {"path": str(env), "node_id": "RR-TWO", "key": "confidence",
                                           "new_key": "Confidence Qualitative"})
    assert out["success"] is True and out["written"] is True and out["receipt"] == "compact"
    assert out["key"] == "confidence" and out["new_key"] == "confidence-qualitative"
    assert out["new_key_as_authored"] == "Confidence Qualitative" and out["coercions"]
    assert out["line_before"] == "- confidence: high" and out["line_after"] == "- confidence-qualitative: high"
    assert out["line_number"] == line_of(env, "- confidence-qualitative: high")
    assert out["delta"] == {"RR-TWO": {"added": ["confidence-qualitative"], "removed": ["confidence"]}}
    assert out["nodes_checked"] == 4 and out["nodes_changed"] == 1
    assert out["edges_removed"] == [] and out["edges_added"] == []
    assert out["value_as_parsed"] == "high"
    assert Path(out["backup_path"]).exists() and out["ledger"]["status"] == "ok"
    assert "report" not in out and "properties_before" not in out and "root_membership" in out


def test_adapter_rename_full_receipt_has_the_report_and_both_lines(env):
    out = dispatch("lts_rename_property", {"path": str(env), "node_id": "RR-TWO", "key": "confidence",
                                           "new_key": "confidence-qualitative", "receipt": "full"})
    assert out["receipt"] == "full"
    assert "properties_before" in out and "properties_after" in out
    rep = out["report"]
    assert "-  - confidence: high" in rep and "+  - confidence-qualitative: high" in rep
    assert str(out["line_number"]) in rep and "backup" in rep


def test_adapter_remove_compact_and_full(env):
    cont_line = line_of(env, "- cont: first part")
    out = dispatch("lts_remove_property", {"path": str(env), "node_id": "RR-ONE", "key": "cont",
                                           "expect_value": "first part second part"})
    assert out["success"] is True and out["receipt"] == "compact"
    assert out["removed_lines"] == ["- cont: first part", "  second part"]
    assert out["value_removed"] == "first part second part"
    assert out["line_number"] == cont_line
    assert out["delta"] == {"RR-ONE": {"added": [], "removed": ["cont"]}}
    assert out["edges_removed"] == [] and out["ledger"]["status"] == "ok"
    full = dispatch("lts_remove_property", {"path": str(env), "node_id": "RR-THREE", "key": "confidence",
                                            "receipt": "full"})
    assert full["receipt"] == "full" and "report" in full and "- confidence: low" in full["report"]


@pytest.mark.parametrize("receipt", ["compact", "full"])
def test_adapter_refusal_is_never_compacted_and_carries_a_fixed_code(env, receipt):
    before = env.read_bytes()
    out = dispatch("lts_rename_property", {"path": str(env), "node_id": "RR-ONE", "key": "tag",
                                           "new_key": "label", "receipt": receipt})
    assert out["success"] is False and out["refused"] is True and out["written"] is False
    assert out["file_unchanged"] is True and out["evidence"]["refusal"] == "key_ambiguous"
    assert CODE_RE.fullmatch(out["evidence"]["refusal"])
    assert out["evidence"]["lines"] and "report" in out and out["reason"]
    assert str(env) not in out["reason"], "a refusal's reason must not carry a path"
    assert env.read_bytes() == before


def test_adapter_allow_edge_change_flows_through_dispatch(env):
    out = dispatch("lts_rename_property", {"path": str(env), "node_id": "RR-ONE", "key": "see-also",
                                           "new_key": "see-also-note"})
    assert out["refused"] is True and out["evidence"]["refusal"] == "edge_change_refused"
    out = dispatch("lts_rename_property", {"path": str(env), "node_id": "RR-ONE", "key": "see-also",
                                           "new_key": "see-also-note", "allow_edge_change": True})
    assert out["written"] is True
    assert [(e["source"], e["type"], e["target"]) for e in out["edges_removed"]] == [("RR-ONE", "RELATED_TO", "RR-TWO")]


def test_adapter_bad_arguments_are_coded_refusals_not_crashes(env):
    for args, code in [
        ({"path": str(env), "node_id": "RR-TWO", "key": "confidence", "new_key": "c2", "line": True}, "bad_argument"),
        ({"path": str(env), "node_id": "RR-TWO", "key": "confidence", "new_key": "c2", "line": "7"}, "bad_argument"),
        ({"path": str(env), "node_id": "RR-TWO", "key": 5, "new_key": "c2"}, "bad_argument"),
        ({"path": str(env), "node_id": "RR-TWO", "key": "confidence", "new_key": "c2", "allow_edge_change": "yes"},
         "bad_argument"),
    ]:
        out = dispatch("lts_rename_property", args)
        assert out["refused"] is True and out["evidence"]["refusal"] == code, (args, out)
    out = dispatch("lts_remove_property", {"path": str(env), "node_id": "RR-TWO", "key": "confidence", "expect_value": 3})
    assert out["refused"] is True and out["evidence"]["refusal"] == "bad_argument"
    out = dispatch("lts_remove_property", {"path": str(env), "node_id": "RR-TWO", "key": "confidence", "line": 0})
    assert out["refused"] is True and out["evidence"]["refusal"] in ("bad_argument", "line_not_key_line")


def test_dispatch_threads_the_ledger_metadata(env):
    dispatch("lts_rename_property", {"path": str(env), "node_id": "RR-TWO", "key": "confidence",
                                     "new_key": "c2", "proposer": "P", "approver": "A", "reason": "R", "via": "direct"})
    dispatch("lts_remove_property", {"path": str(env), "node_id": "RR-THREE", "key": "confidence",
                                     "proposer": "P2", "reason": "R2"})
    got = [(x["verb"], x["proposer"], x["approver"], x["reason"]) for x in recs(env)]
    assert got == [("rename_property", "P", "A", "R"), ("remove_property", "P2", None, "R2")]
    out = dispatch("lts_remove_property", {"path": str(env), "node_id": "RR-ONE", "key": "cont", "reason": "x" * 2001})
    assert out["refused"] is True and len(recs(env)) == 2


# ============================================================================== the tool surface
def test_both_tools_are_served_with_the_house_style_schema():
    from factlattice_engine.server import TOOLS
    by = {t["name"]: t for t in TOOLS}
    rn, rm = by["lts_rename_property"], by["lts_remove_property"]
    assert set(rn["inputSchema"]["required"]) == {"path", "node_id", "key", "new_key"}
    assert set(rm["inputSchema"]["required"]) == {"path", "node_id", "key"}
    for spec, extra in ((rn, ("new_key", "line", "allow_edge_change", "receipt")),
                        (rm, ("line", "expect_value", "allow_edge_change", "receipt"))):
        p = spec["inputSchema"]["properties"]
        for k in ("path", "node_id", "key", "proposer", "approver", "reason", "via") + extra:
            assert k in p and p[k].get("description"), (spec["name"], k)
        assert len(spec["description"]) > 600
    assert "lts_add_property" in rn["description"] and "lts_repair_property" in rn["description"]
    assert "byte" in rn["description"].lower() and "REFUSES when" in rn["description"]
    assert "expect_value" in rm["description"] and "REFUSES when" in rm["description"]
    assert "never compacted" in rn["inputSchema"]["properties"]["receipt"]["description"].lower()


def test_both_tools_are_classified_everywhere_a_new_writer_must_be():
    from factlattice_engine import callcli, querylog, tools
    for n in ("lts_rename_property", "lts_remove_property"):
        assert n in tools.LTS_AUTHORING_TOOL_NAMES and n in tools.LTS_WRITE_TOOL_NAMES
        assert n in tools.LTS_ALL_TOOL_NAMES and n in tools.LTS_WRITE_TOOL_NAMES
        assert n in tools.REGISTRY_ERROR_EXEMPT_TOOLS and n in callcli.CORPUS_FREE_TOOLS
        assert n not in tools.LTS_READ_ONLY_TOOL_NAMES and n in callcli.write_tool_names()
        assert n in querylog._LOGGED_TOOLS


def test_cli_call_runs_the_writers_only_with_allow_write(env, tmp_path):
    envv = {**os.environ, "PYTHONPATH": str(REPO), "FACTLATTICE_STATE_DIR": str(tmp_path / "state"),
            "LTS_ENGINE_STATE_DIR": str(tmp_path / "state"), "FACTLATTICE_BACKUP_DIR": str(tmp_path / "bak"),
            "FACTLATTICE_QUERY_LOG": str(tmp_path / "q.jsonl"), "FACTLATTICE_CODE_WATCH": "0",
            "FACTLATTICE_LEDGER_EPHEMERAL_ROOTS": ""}
    envv.pop("FACTLATTICE_CORPUS_CONFIG", None)
    args = json.dumps({"path": str(env), "node_id": "RR-TWO", "key": "confidence", "new_key": "confidence-qualitative"})
    before = env.read_bytes()
    p = subprocess.run([sys.executable, "-m", "factlattice_engine", "call", "lts_rename_property",
                        "--args-json", args, "--json"], capture_output=True, text=True, cwd=str(tmp_path), env=envv)
    out = json.loads(p.stdout)
    assert p.returncode == 3 and out["refused"] is True and "--allow-write" in out["error"]
    assert env.read_bytes() == before
    p = subprocess.run([sys.executable, "-m", "factlattice_engine", "call", "lts_rename_property",
                        "--args-json", args, "--json", "--allow-write"], capture_output=True, text=True,
                       cwd=str(tmp_path), env=envv)
    out = json.loads(p.stdout)
    assert p.returncode == 0 and out["success"] is True, p.stdout[:400]
    assert props(env)["RR-TWO"]["confidence-qualitative"] == "high"
    rargs = json.dumps({"path": str(env), "node_id": "RR-THREE", "key": "confidence", "expect_value": "low"})
    p = subprocess.run([sys.executable, "-m", "factlattice_engine", "call", "lts_remove_property",
                        "--args-json", rargs, "--json", "--allow-write"], capture_output=True, text=True,
                       cwd=str(tmp_path), env=envv)
    assert p.returncode == 0, p.stdout[:400]
    assert "confidence" not in props(env)["RR-THREE"]
    led = ledger.ledger_path(os.path.realpath(env), tmp_path / "state")
    assert [json.loads(x)["verb"] for x in led.read_bytes().splitlines()] == ["rename_property", "remove_property"]


# ============================================================================== tool-reason battery (extension)
PLANT = "zq space dir"


def _all_refusal_replies(tmp_path):
    f = tmp_path / PLANT / "c.lts"
    f.parent.mkdir()
    f.write_text(DOC, encoding="utf-8")
    ghost = str(tmp_path / PLANT / "no such.lts")
    cases = []
    for tool, base in (("lts_rename_property", {"new_key": "label"}), ("lts_remove_property", {})):
        for extra in (
            {"path": ghost, "node_id": "RR-ONE", "key": "tag"},
            {"path": str(f), "node_id": "RR-NOPE", "key": "tag"},
            {"path": str(f), "node_id": "RR-ONE", "key": "absent"},
            {"path": str(f), "node_id": "RR-ONE", "key": "tag"},
            {"path": str(f), "node_id": "RR-ONE", "key": "nested"},
            {"path": str(f), "node_id": "RR-ONE", "key": "tag", "line": 1},
            {"path": str(f), "node_id": "RR-ONE", "key": "see-also"},
            {"path": str(f), "node_id": "RR-ONE", "key": "tag", "line": "x"},
            {"path": str(f), "node_id": "RR-ONE", "key": "tag", "reason": "r" * 2001},
        ):
            cases.append((tool, {**base, **extra}))
    return f, [(t, a, dispatch(t, dict(a))) for t, a in cases]


def test_every_refusal_of_the_new_tools_carries_a_fixed_code_and_no_path(env, tmp_path):
    from factlattice_engine import querylog
    f, replies = _all_refusal_replies(tmp_path)
    codes = set()
    for tool, args, res in replies:
        assert res["success"] is False and res["refused"] is True, (tool, args, res)
        code = (res.get("evidence") or {}).get("refusal")
        assert isinstance(code, str) and CODE_RE.fullmatch(code), (tool, args, res.get("evidence"), res["reason"])
        assert PLANT not in str(res["reason"]), (tool, args, res["reason"])
        rec = querylog.build_record(tool, {}, res, 1.0)
        assert rec["error_code"] == code and PLANT not in json.dumps(rec, ensure_ascii=False), (tool, rec)
        codes.add(code)
    assert {"not_a_file", "node_not_found", "key_absent", "key_ambiguous", "key_nested_block",
            "line_not_key_line", "edge_change_refused", "bad_argument"} <= codes, codes
    assert f.read_text(encoding="utf-8") == DOC


def test_the_battery_predicate_fires_on_a_refusal_without_a_code():
    """Negative control for the battery above: a reply whose code is missing or is message text fails it."""
    bad = {"success": False, "refused": True, "reason": f"{PLANT} is busy", "evidence": {}}
    assert not CODE_RE.fullmatch(str((bad["evidence"] or {}).get("refusal")))
    bad2 = {"evidence": {"refusal": f"{PLANT} is busy: another writer"}}
    assert not CODE_RE.fullmatch(bad2["evidence"]["refusal"])


# ============================================================================== the realistic run
def _confidence_corpus(n=35):
    out = ["# Roadmap [SPEC:RM-ROOT]\n", "\n- kind: roadmap\n"]
    for i in range(n):
        conf = ("high", "medium", "low")[i % 3]
        out.append(f"\n## Claim {i} [RM-C{i}]\n\n- statement: Claim {i}: score >= 0.6170 on set 12 (n={i * 7})\n"
                   f"- confidence: {conf}\n- evidence: [RM-C{(i + 1) % n}]\n- tag: x{i}\n- tag: y{i}\n")
    return "".join(out)


def test_the_35_line_confidence_rename_changes_exactly_what_the_spec_says(tmp_path, monkeypatch):
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    f = tmp_path / "corpus" / "roadmap.lts"
    f.parent.mkdir()
    text = _confidence_corpus()
    f.write_text(text, encoding="utf-8")
    before = props(f)
    n = 0
    for nid in list(before):
        if "confidence" in before[nid]:
            r = rename(f, nid, "confidence", "confidence-qualitative", proposer="DS-convention")
            assert r.delta == {nid: {"added": ["confidence-qualitative"], "removed": ["confidence"]}}
            n += 1
    assert n == 35
    after = props(f)
    for nid, p in before.items():
        want = {("confidence-qualitative" if k == "confidence" else k): v for k, v in p.items()}
        assert after[nid] == want, nid
        assert "confidence" not in after[nid]
    assert f.read_text(encoding="utf-8") == text.replace("- confidence:", "- confidence-qualitative:")
    assert len(recs(f)) == 35 and {r["verb"] for r in recs(f)} == {"rename_property"}


# ============================================================================== shapes the parser accepts
SHAPES = """# Doc [SPEC:SH-ROOT]

- kind: root
- **bold**: yes
- fenced-holder: x

## One [SH-ONE]

- a: 1

prose between the property lines

- late: after prose
- tag: t1

```
- fenced: inside a code fence
```

- tag: t2
"""


def test_a_bold_key_keeps_its_emphasis_and_the_value_is_untouched(env):
    env.write_text(SHAPES, encoding="utf-8")
    rename(env, "SH-ROOT", "bold", "strong")
    assert "- **strong**: yes\n" in env.read_text(encoding="utf-8")
    assert props(env)["SH-ROOT"]["strong"] == "yes" and "bold" not in props(env)["SH-ROOT"]


def test_the_document_root_node_can_be_edited(env):
    env.write_text(SHAPES, encoding="utf-8")
    r = rename(env, "SH-ROOT", "kind", "doc-kind")
    assert r.delta == {"SH-ROOT": {"added": ["doc-kind"], "removed": ["kind"]}}
    remove(env, "SH-ROOT", "fenced-holder", expect_value="x")
    assert "fenced-holder" not in props(env)["SH-ROOT"]


def test_a_property_after_prose_is_renamed_like_any_other(env):
    env.write_text(SHAPES, encoding="utf-8")
    rename(env, "SH-ONE", "late", "later")
    assert props(env)["SH-ONE"]["later"] == "after prose" and "late" not in props(env)["SH-ONE"]


def test_a_property_line_inside_a_code_fence_is_what_the_parser_says_it_is(env, tmp_path):
    """The parser reads `- key: v` inside a fence as a property (measured), so the locator does too, and the
    gate -- not a guess about fences -- is what proves the edit moved the right thing."""
    env.write_text(SHAPES, encoding="utf-8")
    assert props(env)["SH-ONE"]["fenced"] == "inside a code fence"
    r = rename(env, "SH-ONE", "fenced", "fenced2")
    assert props(env)["SH-ONE"]["fenced2"] == "inside a code fence" and r.delta["SH-ONE"]["removed"] == ["fenced"]


def test_a_list_key_split_around_prose_and_a_fence_still_maps_line_to_value(env):
    env.write_text(SHAPES, encoding="utf-8")
    t2 = line_of(env, "- tag: t2")
    remove(env, "SH-ONE", "tag", line=t2, expect_value="t2")
    assert props(env)["SH-ONE"]["tag"] == "t1"
    assert env.read_text(encoding="utf-8").count("- tag:") == 1


def test_remove_of_the_last_list_element_leaves_a_scalar_and_the_gate_agrees(env):
    for want in ("b", "c"):
        remove(env, "RR-ONE", "tag", line=line_of(env, f"- tag: {want}"))
    assert props(env)["RR-ONE"]["tag"] == "a"
    r = remove(env, "RR-ONE", "tag")
    assert "tag" not in props(env)["RR-ONE"] and r.delta == {"RR-ONE": {"added": [], "removed": ["tag"]}}


def test_a_value_that_looks_like_a_reference_but_is_not_a_minting_key_moves_no_edge(env):
    r = rename(env, "RR-ONE", "xref", "xref2")
    assert r.edges_added == [] and r.edges_removed == []
    assert props(env)["RR-ONE"]["xref2"] == "[RR-TWO]"
