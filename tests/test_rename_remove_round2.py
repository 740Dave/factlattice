"""Round 2 of `lts_rename_property` / `lts_remove_property`: the Diamond at deaf7d2 (F1..F11).

  F1  the edge rule must read the PROFILE's property_edges of the file's corpus, or say it did not
  F3  the locator uses the PARSER's own line grammar (a `- **k:** v` line is not a property line)
  F4  repair_property's "already parses" text is the old text, exactly
  F5  duplicate node ids are refused, naming both headings
  F6  a property line inside a fence is a property exactly when the parser says so
  F7  a lock failure's `reason` is a code; the state-dir path moves to `live_detail`
  F11 every refusal code is listed in both tool descriptions and in SPEC 4.6.6
  F2  SPEC 4.6.6 says the old and new writers differ on line endings
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from factlattice.parser import parse_text

REPO = Path(__file__).resolve().parent.parent
CODE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")

PROFILE = {"domain": {"id": "cmp", "namespaces": ["cmp"]},
           "edge_types": [{"type": "DOCUMENTS", "property_key": "documents", "layer": "semantic"}]}
BAD_PROFILE = {"domain": {"id": "bad", "namespaces": ["bad"]},      # shadows a built-in key: refused at load
               "edge_types": [{"type": "MY_TYPE", "property_key": "see-also", "layer": "semantic"}]}

DOC = """# Doc [SPEC:CP-ROOT]

## Target mode [CP-ONE]

- kind: widget
- documents: [CP-TWO]
- xref: [CP-TWO]
- note: plain

## Scope [CP-TWO]

- kind: scope
- note: plain

## Third [CP-THREE]

- xref: [CP-TWO]
"""


@pytest.fixture()
def world(tmp_path, monkeypatch):
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    prof = tmp_path / "cmp-profile.json"
    prof.write_text(json.dumps(PROFILE), encoding="utf-8")
    bad = tmp_path / "bad-profile.json"
    bad.write_text(json.dumps(BAD_PROFILE), encoding="utf-8")
    for d in ("withprof", "noprof", "badprof", "missprof", "outside"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "c.lts").write_text(DOC, encoding="utf-8")
    reg = {
        "withprof": {"roots": [{"path": str(tmp_path / "withprof"), "area": "specs"}], "domain_suffix": "a",
                     "profile": str(prof)},
        "noprof": {"roots": [{"path": str(tmp_path / "noprof"), "area": "specs"}], "domain_suffix": "b"},
        "badprof": {"roots": [{"path": str(tmp_path / "badprof"), "area": "specs"}], "domain_suffix": "c",
                    "profile": str(bad)},
        "missprof": {"roots": [{"path": str(tmp_path / "missprof"), "area": "specs"}], "domain_suffix": "d",
                     "profile": str(tmp_path / "does-not-exist.json")},
    }
    rp = tmp_path / "reg.json"
    rp.write_text(json.dumps(reg), encoding="utf-8")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(rp))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def dispatch(name, args):
    from factlattice_engine import tools
    return tools.dispatch(name, args, tools=None)


def props(f):
    return {n.id: dict(n.properties) for n in parse_text(Path(f).read_text(encoding="utf-8"), str(f)).nodes}


def prof_edges(text):
    from factlattice.profile import load_profile
    p = load_profile(PROFILE)
    return {(e.source, e.type, e.target) for e in parse_text(text, "x", property_edges=p.property_edges).edges
            if e.inference == "property"}


# ============================================================================ F1
def test_the_profile_edge_exists_only_under_the_profile(world):
    """Instrument check: the fixture's `documents` edge is real under the profile and absent without it."""
    assert ("CP-ONE", "DOCUMENTS", "CP-TWO") in prof_edges(DOC)
    assert not any(e.type == "DOCUMENTS" for e in parse_text(DOC, "x").edges)


@pytest.mark.parametrize("op", ["remove", "rename"])
def test_a_profile_registered_edge_is_refused_without_the_flag(world, op):
    f = world / "withprof" / "c.lts"
    before = f.read_bytes()
    args = {"path": str(f), "node_id": "CP-ONE", "key": "documents"}
    tool = "lts_remove_property"
    if op == "rename":
        tool, args = "lts_rename_property", {**args, "new_key": "documents-note"}
    out = dispatch(tool, args)
    assert out["refused"] is True and out["evidence"]["refusal"] == "edge_change_refused", out
    assert [(e["source"], e["type"], e["target"]) for e in out["evidence"]["edges_removed"]] == \
           [("CP-ONE", "DOCUMENTS", "CP-TWO")]
    assert "cmp-profile.json" in out["evidence"]["edge_check"]
    assert f.read_bytes() == before


def test_a_profile_registered_edge_is_listed_with_the_flag_and_the_receipt_names_the_profile(world):
    f = world / "withprof" / "c.lts"
    for receipt in ("compact", "full"):
        f.write_text(DOC, encoding="utf-8")
        out = dispatch("lts_remove_property", {"path": str(f), "node_id": "CP-ONE", "key": "documents",
                                               "allow_edge_change": True, "receipt": receipt})
        assert out["written"] is True
        assert [(e["source"], e["type"], e["target"]) for e in out["edges_removed"]] == [("CP-ONE", "DOCUMENTS", "CP-TWO")]
        assert out["edge_check"].startswith("profile applied: cmp-profile.json") and "withprof" in out["edge_check"]
    assert "documents" not in props(f)["CP-ONE"]


def test_renaming_a_plain_key_into_a_profile_key_adds_an_edge_and_is_refused(world):
    f = world / "withprof" / "c.lts"
    out = dispatch("lts_rename_property", {"path": str(f), "node_id": "CP-THREE", "key": "xref", "new_key": "documents"})
    assert out["refused"] is True and out["evidence"]["refusal"] == "edge_change_refused"
    assert [(e["source"], e["type"], e["target"]) for e in out["evidence"]["edges_added"]] == \
           [("CP-THREE", "DOCUMENTS", "CP-TWO")]
    assert out["evidence"]["edges_removed"] == []
    out = dispatch("lts_rename_property", {"path": str(f), "node_id": "CP-TWO", "key": "note",
                                           "new_key": "documents"})       # value has no [ID]: no edge moves
    assert out["written"] is True and out["edges_added"] == []


def test_a_corpus_that_registers_no_profile_says_so_and_the_key_is_plain_there(world):
    f = world / "noprof" / "c.lts"
    out = dispatch("lts_remove_property", {"path": str(f), "node_id": "CP-ONE", "key": "documents"})
    assert out["written"] is True and out["edges_removed"] == []
    assert out["edge_check"] == "built-in tables only (no profile applied: corpus_registers_no_profile)"


def test_a_file_outside_every_corpus_gets_the_explicit_note(world):
    f = world / "outside" / "c.lts"
    out = dispatch("lts_remove_property", {"path": str(f), "node_id": "CP-ONE", "key": "note"})
    assert out["written"] is True
    assert out["edge_check"] == "built-in tables only (no profile applied: outside_every_corpus)"
    assert any("edge_check" in w and "outside_every_corpus" in w for w in out["warnings"])


@pytest.mark.parametrize("corpus,code", [("badprof", "profile_load_failed"), ("missprof", "profile_file_missing")])
def test_a_profile_that_cannot_load_is_never_a_silent_narrower_check(world, corpus, code):
    f = world / corpus / "c.lts"
    out = dispatch("lts_rename_property", {"path": str(f), "node_id": "CP-ONE", "key": "note", "new_key": "note2",
                                           "receipt": "full"})
    assert out["written"] is True
    assert out["edge_check"] == f"built-in tables only (no profile applied: {code})"
    assert any(code in w for w in out["warnings"])
    assert "edge_check" in out["report"] and code in out["report"]


def test_an_unresolvable_registry_is_named_in_the_note(world, monkeypatch):
    f = world / "outside" / "c.lts"
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world / "no-such-registry.json"))
    out = dispatch("lts_remove_property", {"path": str(f), "node_id": "CP-ONE", "key": "note"})
    assert out["written"] is True
    assert out["edge_check"] == "built-in tables only (no profile applied: registry_unavailable)"


def test_the_core_writer_without_a_resolved_profile_states_the_limit_too(world):
    from factlattice.authoring import edit
    r = edit.remove_property(world / "outside" / "c.lts", "CP-ONE", "note")
    assert r.edge_check.startswith("built-in tables only (no profile applied:")


def test_the_receipt_binding_of_a_renamed_profile_key_knows_it_mints(world):
    f = world / "withprof" / "c.lts"
    out = dispatch("lts_rename_property", {"path": str(f), "node_id": "CP-THREE", "key": "xref",
                                           "new_key": "documents", "allow_edge_change": True, "receipt": "full"})
    assert out["written"] is True and out["key_mints"] is True


# ============================================================================ F3 + F6: the parser's own line grammar
SHAPE_LINES = ["- k: v", "- k:v", "- **k**: v", "- **k:** v", "- k :  v", "-  k: v", "-\tk: v", "- k:", "- **k**:",
               "- k: ", "- K: v", "* k: v", "- k-2: v", "- 2k: v", "- **k**:v", "  - k: v"]


@pytest.mark.parametrize("line", SHAPE_LINES)
@pytest.mark.parametrize("where", ["plain", "fence", "tilde", "after_prose"])
def test_the_locator_sees_a_line_exactly_when_the_parser_holds_the_key(tmp_path, line, where):
    from factlattice.authoring import edit
    body = {"plain": [line], "fence": ["```", line, "```"], "tilde": ["~~~", line, "~~~"],
            "after_prose": ["prose here", "", line]}[where]
    text = "# D [SPEC:SH-ROOT]\n\n## N [SH-ONE]\n\n- other: x\n" + "\n".join(body) + "\n"
    parsed_has = any(k in {"k", "k-2"} for k in props_text(text)["SH-ONE"])
    lines = text.splitlines()
    found = edit._key_lines(lines, 2, len(lines), "k") + edit._key_lines(lines, 2, len(lines), "k-2")
    # a parser-held key must be found; a found line must be a property to the parser or a (possibly empty) block head
    assert (not parsed_has or found) and all(nested or parsed_has for _i, nested in found), (line, where, found)


def props_text(text):
    return {n.id: dict(n.properties) for n in parse_text(text, "x").nodes}


def test_a_bold_colon_inside_the_wrapper_is_key_absent_not_gate_failed(tmp_path, monkeypatch):
    from factlattice.authoring import edit
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "st"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    (tmp_path / "corpus").mkdir()
    f = tmp_path / "corpus" / "c.lts"
    f.write_text("# D [SPEC:SH-ROOT]\n\n## N [SH-ONE]\n\n- **confidence:** high\n", encoding="utf-8")
    before = f.read_bytes()
    for fn in (lambda: edit.rename_property(f, "SH-ONE", "confidence", "c2"),
               lambda: edit.remove_property(f, "SH-ONE", "confidence")):
        with pytest.raises(edit.EditRefused) as ei:
            fn()
        assert ei.value.evidence["refusal"] == "key_absent", str(ei.value)
    assert f.read_bytes() == before


# ============================================================================ F4
def test_repair_property_already_parses_text_is_the_old_text_exactly():
    src = (REPO / "factlattice/authoring/edit.py").read_text(encoding="utf-8")
    assert "nothing to repair. Use `lts_add_property` to change its value or key. " in src
    assert "lts_rename_property` to change its key" not in src


# ============================================================================ F5
def test_a_duplicate_node_id_is_refused_naming_both_headings(tmp_path, monkeypatch):
    from factlattice.authoring import edit
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "st"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    (tmp_path / "corpus").mkdir()
    f = tmp_path / "corpus" / "c.lts"
    text = "# D [SPEC:DU-ROOT]\n\n## A [DU-ONE]\n\n- confidence: first\n\n## B [DU-ONE]\n\n- confidence: second\n"
    f.write_text(text, encoding="utf-8")
    for fn in (lambda: edit.rename_property(f, "DU-ONE", "confidence", "c2"),
               lambda: edit.remove_property(f, "DU-ONE", "confidence")):
        with pytest.raises(edit.EditRefused) as ei:
            fn()
        assert ei.value.evidence["refusal"] == "duplicate_node_id"
        assert ei.value.evidence["lines"] == [3, 7] and "3" in str(ei.value) and "7" in str(ei.value)
    assert f.read_text(encoding="utf-8") == text


# ============================================================================ F7
def test_a_lock_failure_reason_is_a_code_and_the_path_moves_to_live_detail(world, monkeypatch):
    f = world / "withprof" / "c.lts"
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(world / "withprof" / "state-inside-corpus"))
    out = dispatch("lts_rename_property", {"path": str(f), "node_id": "CP-ONE", "key": "note", "new_key": "note2"})
    assert out["refused"] is True and out["evidence"]["refusal"] == "lock_unavailable"
    assert out["reason"] == "lock_unavailable"
    assert "state-inside-corpus" not in out["reason"] and "state-inside-corpus" not in out["report"]
    assert "state-inside-corpus" in out["live_detail"] and "never persisted" in out["live_detail_note"]
    from factlattice_engine import querylog
    rec = json.dumps(querylog.build_record("lts_rename_property", {}, out, 1.0))
    assert "state-inside-corpus" not in rec and "error_code" in rec


# ============================================================================ F11, F2, missing arguments
def _spec_table_codes():
    spec = (REPO / "SPEC.md").read_text(encoding="utf-8")
    sec = spec[spec.index("#### 4.6.6"):spec.index("### 4.7")]
    return set(re.findall(r"`([a-z][a-z0-9]*(?:[_-][a-z0-9]+)+)`", sec))


def test_every_refusal_code_is_listed_in_both_descriptions_and_the_spec():
    from factlattice.authoring import edit
    from factlattice_engine.server import TOOLS
    codes = set(edit.WRITER_REFUSAL_CODES)
    src = (REPO / "factlattice/authoring/edit.py").read_text(encoding="utf-8")
    raised = set(re.findall(r'_refuse\(\s*"([a-z_-]+)"', src)) | {"lock_busy", "lock_unavailable", "write_refused"}
    assert raised <= codes, raised - codes
    by = {t["name"]: t["description"] for t in TOOLS}
    for tool in ("lts_rename_property", "lts_remove_property"):
        missing = [c for c in codes if f"`{c}`" not in by[tool]]
        assert not missing, (tool, missing)
    assert codes <= _spec_table_codes(), codes - _spec_table_codes()


def test_the_descriptions_state_exactly_what_the_edge_check_covers():
    from factlattice_engine.server import TOOLS
    for t in TOOLS:
        if t["name"] in ("lts_rename_property", "lts_remove_property"):
            d = t["description"]
            assert "edge_check" in d and "property_edges" in d and "outside_every_corpus" in d


def test_spec_and_sdlc_state_the_edge_check_and_the_line_ending_difference():
    spec = (REPO / "SPEC.md").read_text(encoding="utf-8")
    sec = spec[spec.index("#### 4.6.6"):spec.index("### 4.7")]
    assert "edge_check" in sec and "property_edges" in sec and "profile_load_failed" in sec
    assert "line ending" in sec and "normalis" in sec and "lts_add_property" in sec
    sdlc = (REPO / "SDLC.md").read_text(encoding="utf-8")
    row = next(l for l in sdlc.splitlines() if l.startswith("- [ ] [RENAME-REMOVE-PROPERTY]"))
    assert "edge_check" in row and "property_edges" in row


@pytest.mark.parametrize("tool,args", [("lts_rename_property", {"path": "x.lts", "node_id": "A", "key": "k"}),
                                       ("lts_remove_property", {"node_id": "A", "key": "k"})])
def test_a_missing_required_argument_is_a_coded_refusal(world, tool, args):
    out = dispatch(tool, args)
    assert out["refused"] is True and out["evidence"]["refusal"] == "bad_argument" and out["success"] is False
