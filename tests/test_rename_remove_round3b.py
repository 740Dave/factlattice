"""Round 3b: registry_unavailable FAILS CLOSED for a key that could be an edge key (outside_every_corpus stays complete)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

DOC = """# Doc [SPEC:T-ROOT]

## One [T-ONE]

- xref: [T-TWO]
- see-also: [T-TWO]
- note: plain

## Two [T-TWO]

- kind: scope
"""


@pytest.fixture()
def nof(tmp_path, monkeypatch):
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    (tmp_path / "corpus").mkdir()
    f = tmp_path / "corpus" / "c.lts"
    f.write_text(DOC, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path, f


def call(tool, f, **kw):
    from factlattice_engine import authoring
    fn = authoring.remove_property if tool == "remove" else authoring.rename_property
    return fn(str(f), "T-ONE", **kw)


def break_registry(monkeypatch, tmp_path, how):
    p = tmp_path / "reg.json"
    if how == "missing":
        monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(tmp_path / "no-such-registry.json"))
    else:
        p.write_text("{ not json", encoding="utf-8")
        monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(p))


@pytest.mark.parametrize("how", ["missing", "notjson"])
@pytest.mark.parametrize("tool,key,extra", [("remove", "xref", {}), ("rename", "xref", {"new_key": "xref2"}),
                                            ("rename", "note", {"new_key": "depends-on"})])
def test_no_registry_refuses_an_edge_capable_key(nof, monkeypatch, how, tool, key, extra):
    tmp, f = nof
    break_registry(monkeypatch, tmp, how)
    out = call(tool, f, key=key, **extra)
    assert out["refused"] is True and out["evidence"]["refusal"] == "edge-context-incomplete", out
    assert out["evidence"]["corpora"] == [{"corpus": None, "code": "registry_unavailable"}]
    assert out["reason"] == "edge-context-incomplete" and "registry_unavailable" in out["explanation"] and str(tmp) not in out["explanation"]
    assert f.read_text(encoding="utf-8") == DOC


def test_no_registry_a_detected_builtin_edge_move_keeps_its_more_specific_refusal(nof, monkeypatch):
    tmp, f = nof
    break_registry(monkeypatch, tmp, "missing")
    out = call("remove", f, key="see-also")
    assert out["refused"] and out["evidence"]["refusal"] == "edge_change_refused"
    assert out["evidence"]["edges_removed"] and "registry_unavailable" in out["evidence"]["edge_check"]


@pytest.mark.parametrize("tool,extra", [("remove", {}), ("rename", {"new_key": "xref2"})])
def test_no_registry_with_allow_edge_change_proceeds(nof, monkeypatch, tool, extra):
    tmp, f = nof
    break_registry(monkeypatch, tmp, "missing")
    out = call(tool, f, key="xref", allow_edge_change=True, **extra)
    assert out["written"] is True and "registry_unavailable" in out["edge_check"]


@pytest.mark.parametrize("tool,extra", [("remove", {}), ("rename", {"new_key": "note2"})])
def test_no_registry_plain_key_proceeds_with_note_and_warning(nof, monkeypatch, tool, extra):
    tmp, f = nof
    break_registry(monkeypatch, tmp, "missing")
    out = call(tool, f, key="note", **extra)
    assert out["written"] is True
    assert out["edge_check"] == "built-in tables only (no profile applied: registry_unavailable)"
    assert any("registry_unavailable" in w for w in out["warnings"])


@pytest.mark.parametrize("tool,extra", [("remove", {}), ("rename", {"new_key": "xref2"})])
def test_outside_every_corpus_is_still_complete_and_does_not_refuse(nof, monkeypatch, tool, extra):
    tmp, f = nof
    reg = tmp / "reg.json"
    reg.write_text(json.dumps({"other": {"roots": [{"path": str(tmp / "elsewhere"), "area": "specs"}],
                                         "domain_suffix": "x"}}), encoding="utf-8")
    (tmp / "elsewhere").mkdir()
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(reg))
    out = call(tool, f, key="xref", **extra)
    assert out["written"] is True
    assert out["edge_check"] == "built-in tables only (no profile applied: outside_every_corpus)"


def test_spec_and_descriptions_state_registry_unavailable_fails_closed():
    repo = Path(__file__).resolve().parent.parent
    spec = (repo / "SPEC.md").read_text(encoding="utf-8")
    sec = spec[spec.index("#### 4.6.6"):spec.index("### 4.7")]
    assert "registry_unavailable" in sec and "corpus: null" in sec and "outside_every_corpus" in sec
    from factlattice_engine.server import TOOLS
    for t in TOOLS:
        if t["name"] in ("lts_rename_property", "lts_remove_property"):
            assert "no registry" in t["description"] and "`registry_unavailable`" in t["description"] \
                and "FAIL CLOSED" in t["description"]
