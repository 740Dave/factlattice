"""Round 3 (Diamond at b4f7acb): the edge context FAILS CLOSED.

  F-A  a file in two corpora: an unloadable EARLIER profile must not drop a LATER valid one
  F-C  a corpus seated in the process but absent from the registry file is not "registers no profile"
  F-B  the two-corpora cases are tested (both valid; one broken, both orders)
When any corpus that contains the file has an undeterminable profile, a key that COULD be an edge key is refused
(`edge-context-incomplete`, naming the corpora); a plain key proceeds with the receipt note and a warning.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from factlattice.parser import parse_text

CODE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
DOCS = {"domain": {"id": "cmp", "namespaces": ["cmp"]},
        "edge_types": [{"type": "DOCUMENTS", "property_key": "documents", "layer": "semantic"}]}
GOVS = {"domain": {"id": "gv", "namespaces": ["gv"]},
        "edge_types": [{"type": "GOVERNS", "property_key": "governs", "layer": "semantic"}]}
DOC = """# Doc [SPEC:T-ROOT]

## One [T-ONE]

- kind: widget
- documents: [T-TWO]
- governs: [T-TWO]
- xref: [T-TWO]
- note: plain
- see-also-text: plain words
- tag: a

## Two [T-TWO]

- kind: scope
"""


@pytest.fixture()
def w(tmp_path, monkeypatch):
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    (tmp_path / "docs.json").write_text(json.dumps(DOCS), encoding="utf-8")
    (tmp_path / "govs.json").write_text(json.dumps(GOVS), encoding="utf-8")
    (tmp_path / "broken.json").write_text(json.dumps({"domain": {"id": "b", "namespaces": ["b"]}, "edge_types": [
        {"type": "X", "property_key": "see-also", "layer": "semantic"}]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def setup_registry(w, monkeypatch, entries):
    """entries: {corpus_id: profile_path_or_None}; every corpus has the SAME root, so the file is in all of them."""
    root = w / "shared"
    root.mkdir(exist_ok=True)
    f = root / "c.lts"
    f.write_text(DOC, encoding="utf-8")
    reg = {}
    for i, (cid, prof) in enumerate(entries.items()):
        spec = {"roots": [{"path": str(root), "area": "specs"}], "domain_suffix": f"s{i}"}
        if prof is not None:
            spec["profile"] = str(prof)
        reg[cid] = spec
    rp = w / "reg.json"
    rp.write_text(json.dumps(reg), encoding="utf-8")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(rp))
    return f


def call(tool, f, store=None, **kw):
    from factlattice_engine import authoring
    fn = authoring.remove_property if tool == "remove" else authoring.rename_property
    return fn(str(f), "T-ONE", store=store, **kw)


def props(f):
    return {n.id: dict(n.properties) for n in parse_text(Path(f).read_text(encoding="utf-8"), "x").nodes}


def ghost_store(w, cid="ghost"):
    root = str(w / "shared")
    part = SimpleNamespace(corpus_id=cid, root_paths=[root])
    return SimpleNamespace(partitions=lambda: [part])


# ============================================================================ F-B: two corpora, both valid
@pytest.mark.parametrize("order", [("aaa", "bbb"), ("bbb", "aaa")])
def test_a_file_in_two_corpora_is_checked_under_both_profiles(w, monkeypatch, order):
    reg = {order[0]: w / "docs.json", order[1]: w / "govs.json"}
    f = setup_registry(w, monkeypatch, reg)
    for key, etype in (("documents", "DOCUMENTS"), ("governs", "GOVERNS")):
        out = call("remove", f, key=key)
        assert out["refused"] is True and out["evidence"]["refusal"] == "edge_change_refused", (key, out)
        assert [e["type"] for e in out["evidence"]["edges_removed"]] == [etype]
    assert f.read_text(encoding="utf-8") == DOC
    out = call("remove", f, key="documents", allow_edge_change=True)
    assert out["written"] and "docs.json" in out["edge_check"] and "govs.json" in out["edge_check"], out["edge_check"]


# ============================================================================ F-A: an earlier broken profile
@pytest.mark.parametrize("bad", ["missing", "unloadable"])
def test_an_earlier_broken_profile_does_not_drop_a_later_valid_one(w, monkeypatch, bad):
    badp = w / "nope.json" if bad == "missing" else w / "broken.json"
    f = setup_registry(w, monkeypatch, {"aaa": badp, "bbb": w / "docs.json"})     # aaa sorts first
    out = call("remove", f, key="documents")
    assert out["refused"] is True and out["evidence"]["refusal"] == "edge_change_refused", out
    assert [e["type"] for e in out["evidence"]["edges_removed"]] == ["DOCUMENTS"]       # bbb's profile WAS applied
    assert "docs.json" in out["evidence"]["edge_check"]
    assert f.read_text(encoding="utf-8") == DOC


@pytest.mark.parametrize("bad,code", [("missing", "profile_file_missing"), ("unloadable", "profile_load_failed")])
@pytest.mark.parametrize("tool", ["remove", "rename"])
def test_incomplete_context_refuses_a_key_that_could_be_an_edge_key(w, monkeypatch, bad, code, tool):
    badp = w / "nope.json" if bad == "missing" else w / "broken.json"
    f = setup_registry(w, monkeypatch, {"aaa": badp, "bbb": w / "docs.json"})
    extra = {"new_key": "xref2"} if tool == "rename" else {}
    out = call(tool, f, key="xref", **extra)          # `xref: [T-TWO]` mints nothing under docs.json, but could elsewhere
    assert out["refused"] is True and out["evidence"]["refusal"] == "edge-context-incomplete", out
    assert CODE_RE.fullmatch(out["evidence"]["refusal"])
    assert out["evidence"]["corpora"] == [{"corpus": "aaa", "code": code}]
    assert out["reason"] == "edge-context-incomplete" and "aaa" in out["explanation"] and str(w) not in out["explanation"]
    assert f.read_text(encoding="utf-8") == DOC


def test_incomplete_context_lets_a_plain_key_proceed_with_the_note_and_a_warning(w, monkeypatch):
    f = setup_registry(w, monkeypatch, {"aaa": w / "nope.json", "bbb": w / "docs.json"})
    out = call("remove", f, key="note")
    assert out["written"] is True
    assert "NOT applied: aaa (profile_file_missing)" in out["edge_check"] and "docs.json" in out["edge_check"]
    assert any("edge_check" in x for x in out["warnings"])
    out = call("rename", f, key="tag", new_key="tag2")
    assert out["written"] is True
    assert "tag2" in props(f)["T-ONE"]


def test_a_builtin_minting_key_is_refused_even_with_a_plain_value(w, monkeypatch):
    f = setup_registry(w, monkeypatch, {"aaa": w / "nope.json"})
    f.write_text(DOC.replace("- see-also-text: plain words", "- see-also: plain words"), encoding="utf-8")
    out = call("remove", f, key="see-also")
    assert out["refused"] and out["evidence"]["refusal"] == "edge-context-incomplete"
    out = call("rename", f, key="note", new_key="depends-on")
    assert out["refused"] and out["evidence"]["refusal"] == "edge-context-incomplete"     # the NEW key could mint


def test_allow_edge_change_accepts_an_incomplete_context(w, monkeypatch):
    f = setup_registry(w, monkeypatch, {"aaa": w / "nope.json"})
    out = call("remove", f, key="xref", allow_edge_change=True)
    assert out["written"] is True and "profile_file_missing" in out["edge_check"]


# ============================================================================ F-C: seated but not in the registry file
@pytest.mark.parametrize("tool", ["remove", "rename"])
def test_a_seated_corpus_absent_from_the_registry_is_incomplete_not_no_profile(w, monkeypatch, tool):
    f = setup_registry(w, monkeypatch, {"other": None})
    store = ghost_store(w)
    extra = {"new_key": "xref2"} if tool == "rename" else {}
    out = call(tool, f, store=store, key="xref", **extra)
    assert out["refused"] is True and out["evidence"]["refusal"] == "edge-context-incomplete", out
    assert {"corpus": "ghost", "code": "corpus_not_in_registry"} in out["evidence"]["corpora"]
    out = call("remove", f, store=store, key="note")
    assert out["written"] is True
    assert out["edge_check"] == "built-in tables only (no profile applied: corpus_not_in_registry)"
    assert any("corpus_not_in_registry" in x for x in out["warnings"])


# ============================================================================ the documented no-profile case is unchanged
def test_a_corpus_that_registers_no_profile_is_still_a_note_not_a_refusal(w, monkeypatch):
    f = setup_registry(w, monkeypatch, {"plain": None})
    out = call("remove", f, key="xref")
    assert out["written"] is True
    assert out["edge_check"] == "built-in tables only (no profile applied: corpus_registers_no_profile)"
    assert not any("edge_check" in x for x in out["warnings"])


# ============================================================================ F-D / F-E wording pins
def test_sdlc_names_the_code_list_as_an_extension_point():
    sdlc = (Path(__file__).resolve().parent.parent / "SDLC.md").read_text(encoding="utf-8")
    row = next(l for l in sdlc.splitlines() if l.startswith("- [ ] [RENAME-REMOVE-PROPERTY]"))
    assert "WRITER_REFUSAL_CODES" in row and "edge-context-incomplete" in row and "never returns early" in row


def test_the_descriptions_state_the_fail_closed_rule_and_the_no_warning_exception():
    from factlattice_engine.server import TOOLS
    for t in TOOLS:
        if t["name"] in ("lts_rename_property", "lts_remove_property"):
            d = t["description"]
            assert "edge-context-incomplete" in d and "corpus_not_in_registry" in d and "except for `corpus_registers_no_profile`" in d
