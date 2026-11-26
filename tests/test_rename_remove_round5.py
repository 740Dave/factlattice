"""Round 5 (Opus Diamond FAIL, F1 P0): `remove_property` took body lines the parser never attached.

F1  the removal extent was a writer-side `_INDENTED` walk; the parser folds a value's tail by
    `_fold_continuation`, which stops at bullets, ordered items, quotes, tables, rules, headings and fences.
    Every line in that gap was deleted, and the property-only gate could not see it.
GATE the whole-file gate compares PARSED PROPERTIES, so a vanished body line is invisible to it. Round 5 adds a
    BYTE-LEVEL check: the new file must equal the old file minus EXACTLY the lines the parser attributes to the
    target (remove), or with only the key span changed (rename). Its negative control is below.
SIBLINGS (round 5 changed the three `set_property` insertion walks; round 6 REVERTED them to 88aa5d6 because the
    change moved a new key between a key-only head and its unparsed block, see test_rename_remove_round6.py).
"""
from __future__ import annotations

import itertools
import random
import re
import sys
from pathlib import Path

import pytest

from factlattice.parser import BRANCH_PROP_CONT, parse_text

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture()
def w(tmp_path, monkeypatch):
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "st"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "corpus"
    d.mkdir()
    return d


def put(d, text, name="c.lts", newline=None):
    f = d / name
    with open(f, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)
    return f


def rm(f, **kw):
    from factlattice.authoring import edit
    return edit.remove_property(str(f), "RR-ONE", "k", **kw)


def doc(*body, eol="\n"):
    return eol.join(["# Doc [SPEC:RR-ROOT]", "", "## One [RR-ONE]", "", *body, "", "## Two [RR-TWO]", "", "- k: other", ""])


FURNITURE = {
    "sub-bullet": ["  - sub bullet"],
    "ordered": ["  1. ordered item"],
    "quote": ["  > a quote"],
    "table": ["  | a | b |", "  |---|---|"],
    "rule": ["  ---"],
    "heading-shaped": ["  # not a node"],
    "fence": ["  ```", "  code", "  ```"],
    "fence-with-language": ["  ```python", "  x = 1", "  ```"],
}


# ------------------------------------------------------------------ F1: furniture under a removed property
@pytest.mark.parametrize("shape", sorted(FURNITURE))
@pytest.mark.parametrize("wrap", [False, True])
def test_furniture_indented_under_a_removed_property_survives(w, shape, wrap):
    head = ["- k: draft"] + (["  wrapped tail"] if wrap else [])
    body = head + FURNITURE[shape] + ["- owner: ds"]
    original = doc(*body)
    f = put(w, original)
    rm(f, expect_value="draft wrapped tail" if wrap else "draft")
    expected = doc(*FURNITURE[shape], "- owner: ds")
    assert f.read_text() == expected, "only the head and its PARSER-FOLDED tail may leave the file"


@pytest.mark.parametrize("shape", sorted(FURNITURE))
def test_furniture_survives_crlf(w, shape):
    body = ["- k: draft", "  wrapped"] + FURNITURE[shape] + ["- owner: ds"]
    original = doc(*body, eol="\r\n")
    f = put(w, original, newline="")
    assert b"\r\n" in f.read_bytes()
    rm(f)
    got = f.read_bytes().decode()
    assert got == doc(*FURNITURE[shape], "- owner: ds", eol="\r\n")


# ------------------------------------------------------------------ the generated probe (the Diamond's shape)
HEADINGS = ["## One [RR-ONE]", "# One [RR-ONE]", "###### One [RR-ONE]", "### [RR-ONE] One", "## SPEC:[RR-ONE] One"]
TAILS = {"none": [], "wrap": ["  wrapped"], "sub-bullet": ["  - sub"], "prose": ["", "prose here"],
         "late-key": ["", "prose", "- late: x"], "table": ["  | a | b |"],
         "u2028": ["  wrap more"], "fenced-key": ["  ```", "- k: in-fence", "  ```"]}
SHAPES = {"simple": "- k: draft", "bold": "- **k**: draft", "tab": "-\tk: draft", "id": "- k: [RR-TWO]"}
EOLS = ["\n", "\r\n", "\r"]


def _expected_after_remove(text: str, head_idx: int) -> str:
    """Oracle: the PARSER's own consumption record (head + the BRANCH_PROP_CONT lines directly under it)."""
    cov = parse_text(text, "x").coverage
    end = head_idx + 1
    while cov.consumed.get(end) == BRANCH_PROP_CONT:
        end += 1
    segs = text.splitlines(keepends=True)
    return "".join(segs[:head_idx] + segs[end:])


def test_generated_remove_probe_matches_the_parser_record_byte_for_byte(w):
    from factlattice.authoring import edit
    cases = list(itertools.product(HEADINGS[:3], TAILS, SHAPES, EOLS, [True, False]))
    random.Random(5).shuffle(cases)
    cases = cases[:160]       # a seeded sample of the 576-cell product, bounded for the suite
    done = skipped = 0
    for i, (heading, tail, shape, eol, final_nl) in enumerate(cases):
        lines = ["# Doc [SPEC:RR-ROOT]", "", heading, "", SHAPES[shape], *TAILS[tail], "- owner: ds"]
        text = eol.join(lines) + (eol if final_nl else "")
        f = put(w, text, name=f"g{i}.lts", newline="")
        pf = parse_text(text, "x")
        node = next((n for n in pf.nodes if n.id == "RR-ONE"), None)
        if node is None or "k" not in node.properties or isinstance(node.properties["k"], list):
            skipped += 1
            continue
        head_idx = next(j for j, ln in enumerate(text.splitlines()) if re.match(r"^-[ \t]+(\*\*)?k", ln))
        want = _expected_after_remove(text, head_idx)
        try:
            edit.remove_property(str(f), "RR-ONE", "k")
        except edit.EditRefused:
            skipped += 1
            continue
        assert f.read_bytes().decode() == want, (heading, tail, shape, repr(eol), final_nl)
        done += 1
    assert done >= 100 and skipped < done


# ------------------------------------------------------------------ the BYTE GATE and its negative controls
def _old_extent(lines, idx0, end0=None, **_):
    """The 696206c loop, verbatim in effect: every indented non-blank line."""
    end = idx0 + 1
    while end < len(lines) and re.match(r"^[ \t]+\S", lines[end]):
        end += 1
    return end


def test_negative_control_the_byte_gate_refuses_the_old_extent(w, monkeypatch):
    """The gate must FIRE on the F1 case when the extent walk is the old one: the property-only gate passed it."""
    from factlattice.authoring import edit
    original = doc("- k: draft", "  - reviewer said: keep this", "- owner: ds")
    f = put(w, original)
    monkeypatch.setattr(edit, "_removal_end", _old_extent)
    with pytest.raises(edit.EditRefused) as ei:
        rm(f)
    assert ei.value.evidence["refusal"] == "gate_failed"
    assert "byte" in str(ei.value).lower()
    assert f.read_text() == original, "a refusal writes nothing"


def test_negative_control_the_byte_gate_refuses_a_whitespace_change_the_property_gate_cannot_see(w, monkeypatch):
    from factlattice.authoring import edit
    original = doc("- k: draft", "- owner: ds", "", "prose line that is not a property")
    f = put(w, original)
    real_join = edit._join

    def bad_join(contents, eols):
        return real_join([c + " " if c.startswith("prose") else c for c in contents], eols)

    monkeypatch.setattr(edit, "_join", bad_join)
    with pytest.raises(edit.EditRefused) as ei:
        rm(f)
    assert ei.value.evidence["refusal"] == "gate_failed"
    assert "byte gate" in str(ei.value)           # the byte gate's OWN message: the property gate also emits gate_failed
    assert f.read_text() == original
    with pytest.raises(edit.EditRefused) as ei:
        edit.rename_property(str(f), "RR-ONE", "owner", "boss")
    assert ei.value.evidence["refusal"] == "gate_failed"
    assert "byte gate" in str(ei.value)
    assert f.read_text() == original


def test_negative_control_rename_byte_gate_refuses_a_value_whitespace_change(w, monkeypatch):
    from factlattice.authoring import edit
    original = doc("- k: draft   ", "  tail")
    f = put(w, original)
    real = edit._splice_key
    monkeypatch.setattr(edit, "_splice_key", lambda content, new: real(content, new).rstrip())
    with pytest.raises(edit.EditRefused) as ei:
        edit.rename_property(str(f), "RR-ONE", "k", "kk")
    assert ei.value.evidence["refusal"] == "gate_failed"
    assert "byte gate" in str(ei.value)
    assert f.read_text() == original


def test_gate_is_green_on_an_honest_remove_and_rename(w):
    from factlattice.authoring import edit
    f = put(w, doc("- k: a", "  wrap", "  - sub", "- owner: ds"))
    edit.rename_property(str(f), "RR-ONE", "owner", "boss")
    rm(f)
    assert f.read_text() == doc("  - sub", "- boss: ds")


# ------------------------------------------------------------------ the five claims (F2) and the checklist rule (F7)
def test_the_five_overclaims_are_gone_and_the_checklist_rule_is_there():
    spec = (REPO / "SPEC.md").read_text()
    sdlc = (REPO / "SDLC.md").read_text()
    edit_src = (REPO / "factlattice/authoring/edit.py").read_text()
    srv = (REPO / "factlattice_engine/server.py").read_text()
    adapter = (REPO / "factlattice_engine/authoring.py").read_text()
    old = "the indented continuation the parser folds into its value"
    for name, text in (("SPEC", spec), ("edit.py", edit_src), ("server.py", srv), ("authoring.py", adapter)):
        assert old not in text, name
    assert "(and its folded continuation) left the file" not in adapter
    assert "EXACTLY the wrapped tail the parser attributes to its value" in spec and "BYTE-LEVEL gate" in spec
    assert "the lines the parser attributes to that value" in adapter
    assert "A WRITER MAY REMOVE ONLY THE LINES THE PARSER ATTRIBUTES TO THE TARGET, checked at BYTE LEVEL" in sdlc
    assert "ONE element is edited" not in sdlc


@pytest.mark.skipif(not (REPO / "docs" / "build-logs").is_dir(),
                    reason="docs/build-logs/ is not in a published snapshot (PUBLISH-EXCLUDE removes it)")
def test_the_build_report_no_longer_carries_the_overclaim_about_editing_one_element():
    # split out of test_the_five_overclaims_are_gone_and_the_checklist_rule_is_there, so the rest of that test still
    # runs in a snapshot; the assertion is unchanged
    report = (REPO / "docs/build-logs/BUILD-REPORT-rename-remove-property-2026-10-08.md").read_text()
    assert "edit ONE element and join: every other byte is the original's." not in report


# ------------------------------------------------------------------ F3: a failed lock release after the write
def test_a_lock_release_failure_after_the_write_is_not_reported_as_a_refusal(w, monkeypatch):
    from factlattice.authoring import edit
    f = put(w, doc("- k: a", "- owner: ds"))
    real_close = edit.os.close
    state = {"n": 0}

    def close(fd):
        real_close(fd)
        if sys._getframe(1).f_code.co_name == "_file_lock":     # only the lock release, nothing else
            state["n"] += 1
            raise OSError(4, "Interrupted system call")         # EINTR, raised AFTER the descriptor is released

    monkeypatch.setattr(edit.os, "close", close)
    r = rm(f)                                           # must return, not raise EditRefused/OSError
    monkeypatch.undo()
    assert state["n"] == 1
    assert r.value_removed == "a"
    assert f.read_text() == doc("- owner: ds")
