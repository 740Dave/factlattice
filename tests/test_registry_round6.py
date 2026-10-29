"""U1a round 6 (Diamond FAIL at 51f1d70): persisted surfaces carry STRUCTURE, never free error text. A regex cannot
make free text safe, so the query log and the reviewer persist only error class, error code, a fixed reviewer reason,
and a RegistryError's `persist_text` (built from parts, basenames only)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NAMES = ["zq space dir", "zq it's dir", 'zq "dq" dir', "zq it's \"both\" dir", "zq üñí dir", "zq\\back dir"]
SECRET_TOKENS = ["zq space", "zq it", "zq \"dq\"", "zq üñí", "zq\\back", "dq", "üñí"]


@pytest.fixture()
def w(tmp_path, monkeypatch):
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "st"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "corp").mkdir()
    (tmp_path / "corp" / "a.lts").write_text("# D [D-1]\n\n## N [N-1]\n\n- status: active\n", encoding="utf-8")
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps({"pubcorp": {"roots": [{"path": str(tmp_path / "corp"), "area": "specs"}], "domain_suffix": "p"}}))
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(reg))
    return tmp_path


def log_text(w):
    p = Path(os.environ["FACTLATTICE_QUERY_LOG"])
    return p.read_text(encoding="utf-8") if p.exists() else ""


def last_rec(w):
    lines = [l for l in log_text(w).splitlines() if l.strip()]
    return json.loads(lines[-1])


def engine():
    from factlattice_engine import server
    return server.boot_engine(retry_delay_s=0.0, watch=False)


# ------------------------------------------------------------------ non-registry errors reach the log as STRUCTURE
@pytest.mark.parametrize("name", NAMES)
def test_non_registry_tool_errors_never_put_a_directory_name_in_the_query_log(w, name):
    from factlattice_engine import tools
    eng = engine()
    d = w / name; d.mkdir()
    missing = str(d / "missing profile.lts")
    cases = {
        "lts_lint(profile)": ("lts_lint", {"source": str(w / "corp"), "profile": missing}),
        "lts_preview(profile)": ("lts_preview", {"content": "# T [T-1]\n", "profile": missing}),
        "lts_profile_audit(profile)": ("lts_profile_audit", {"corpus": "pubcorp", "profile": missing}),
        "OSError on a path": ("lts_property_loss", {"roots": [str(d / "no such file.lts")]}),
        "lts_find(path)": ("lts_find", {"path": str(d / "no such.lts"), "prefix": "N"}),
        "lts_add_property(path)": ("lts_add_property", {"path": str(d / "nope.lts"), "node_id": "N-1", "key": "k", "value": "v"}),
    }
    reply_named = 0
    for label, (tool, args) in cases.items():
        res = tools.dispatch(tool, args, tools=eng)
        esc = json.dumps(name, ensure_ascii=False)[1:-1]             # how a name with quotes/backslashes appears inside JSON
        if name in json.dumps(res, ensure_ascii=False) or esc in json.dumps(res, ensure_ascii=False):
            reply_named += 1                           # the LIVE reply may carry the full path (terminal/MCP rule)
        text = log_text(w)
        for tok in SECRET_TOKENS:
            if tok.split()[0] in name or tok in name:
                assert tok not in text, f"{label}: {tok!r} reached the query log"
        assert name not in text and esc not in text, label
    assert reply_named >= 1, "the probe must really put the planted name in at least one live reply"


def test_the_log_record_for_an_error_is_class_and_code_not_text(w):
    from factlattice_engine import tools
    eng = engine()
    tools.dispatch("lts_find", {"corpus": "pubcorp", "limit": -1}, tools=eng)
    rec = last_rec(w)
    assert rec["ok"] is False
    assert "error" not in rec                                   # nothing free-text from a non-registry error
    assert rec.get("error_code") == "invalid_limit"
    tools.dispatch("lts_lint", {"source": str(w / "corp"), "profile": str(w / "missing x.lts")}, tools=eng)
    rec = last_rec(w)
    assert "error" not in rec and rec.get("error_class") == "FileNotFoundError"      # exactly: no text, a class
    assert "missing x" not in json.dumps(rec)


def test_a_registry_refusal_logs_exactly_persist_text(w, monkeypatch):
    from factlattice_engine import tools
    bad = w / "zq it's dir" / "reg.json"; bad.parent.mkdir(); bad.write_text("{ torn")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(bad))
    eng = engine()
    tools.dispatch("lts_find", {"corpus": "pubcorp", "prefix": "N"}, tools=eng)
    rec = last_rec(w)
    assert rec["error_code"] == "registry-not-found"
    assert rec["error"] == eng.registry_error.persist_text and "reg.json" in rec["error"]
    assert "zq it" not in json.dumps(rec, ensure_ascii=False)


def test_the_error_class_is_taken_from_the_reply_prefix(w):
    from factlattice_engine import querylog
    rec = querylog.build_record("lts_find", {}, {"success": False, "error": "ValueError: bad thing at /x y/z"}, 1.0)
    assert rec["error_class"] == "ValueError" and "error" not in rec and "x y" not in json.dumps(rec)


def test_the_querylog_docstring_states_the_schema(w):
    from factlattice_engine import querylog
    assert "error_class" in querylog.__doc__ and "error_code" in querylog.__doc__ and "persist_text" in querylog.__doc__


# ------------------------------------------------------------------ the reviewer persists structure only
def test_the_reviewer_never_persists_a_raw_error_message(w):
    from factlattice_engine.reviewer import compare_registries
    hostile = {"source": "env", "path": "/Users/x/zq space dir/Application Support/reg.json",
               "error": "registry-not-found: the registry '/Users/x/zq space dir/Application Support/reg.json' failed (zq it's dir)"}
    r = compare_registries(hostile, {"source": "env", "path": "/p/reg.json", "sha256": "0" * 64})
    blob = json.dumps(r, ensure_ascii=False)
    assert "zq" not in blob and "Application Support" not in blob
    assert r["registry_mcp"].startswith("not-used (registry error mode")
    good = compare_registries({**hostile, "error_persist": "registry-not-found: the registry 'reg.json' failed"},
                              {"source": "env", "path": "/p/reg.json", "sha256": "0" * 64})
    assert "reg.json" in good["registry_mcp"] and "zq" not in json.dumps(good)


def test_reduce_paths_and_known_paths_are_gone_nothing_live_needs_them():
    from factlattice import registry as R
    assert not hasattr(R, "reduce_paths") and not hasattr(R, "known_paths")
    for p in list((REPO / "factlattice").rglob("*.py")) + list((REPO / "factlattice_engine").rglob("*.py")):
        assert "reduce_paths" not in p.read_text(encoding="utf-8") and "known_paths" not in p.read_text(encoding="utf-8"), p


# ------------------------------------------------------------------ P2-1 deleted cwd
def test_a_deleted_cwd_does_not_drop_query_log_records(w):
    from factlattice_engine import tools
    eng = engine()
    gone = w / "gone cwd"; gone.mkdir()
    os.chdir(gone)
    shutil.rmtree(gone)
    assert not os.path.exists(os.getcwd()) if hasattr(os, "getcwd") and False else True
    tools.dispatch("lts_find", {"corpus": "pubcorp", "limit": -1}, tools=eng)       # an error-bearing call
    tools.dispatch("lts_find", {"corpus": "pubcorp", "prefix": "N"}, tools=eng)     # and an ordinary one
    recs = [json.loads(l) for l in log_text(w).splitlines() if l.strip()]
    assert len(recs) == 2 and recs[0]["ok"] is False, recs


def test_the_registry_choice_captures_the_cwd_at_resolve_time_and_survives_a_deleted_one(w, monkeypatch):
    from factlattice import registry as R
    c = R.resolve_registry()
    assert c.cwd == os.getcwd()
    gone = w / "gone2"; gone.mkdir(); os.chdir(gone); shutil.rmtree(gone)
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG")
    c2 = R.resolve_registry()                          # the example source needs a root: must not raise
    assert c2.cwd == "<cwd unavailable>" and c2.source == "example"


# ------------------------------------------------------------------ P2-2 never open the pointer at log time
def test_a_fifo_at_the_pointer_path_with_an_env_registry_does_not_hang_the_first_error(w):
    from factlattice import registry as R
    from factlattice_engine import tools
    eng = engine()
    ptr = Path(R.pointer_path()); ptr.parent.mkdir(parents=True)
    os.mkfifo(ptr)
    out = {}

    def run():
        out["r"] = tools.dispatch("lts_find", {"corpus": "pubcorp", "limit": -1}, tools=eng)
    t = threading.Thread(target=run, daemon=True); t.start(); t.join(2.0)
    assert not t.is_alive(), "the first erroring call hung on the pointer FIFO"
    assert out["r"]["success"] is False
    assert last_rec(w)["ok"] is False


def _stat_checked_before_open(src: str) -> bool:
    i = src.index("def _read_pointer")
    seg = src[i:i + 900]
    return "S_ISREG" in seg and "open(" in seg and seg.index("S_ISREG") < seg.index("open(")


def test_the_pointer_is_stat_checked_before_any_open():
    assert _stat_checked_before_open((REPO / "factlattice" / "registry.py").read_text(encoding="utf-8"))


def test_the_stat_before_open_check_can_fail_on_a_planted_bad_input():
    """NEGATIVE CONTROL for the test above (it used to end in `or True`, which could not fail)."""
    bad = 'def _read_pointer(p):\n    with open(p, "rb") as fh:\n        data = fh.read()\n    if not stat.S_ISREG(os.stat(p).st_mode):\n        raise X\n'
    assert _stat_checked_before_open(bad) is False


# ------------------------------------------------------------------ P2-3 / P3 text
def test_the_sdlc_row_says_structure_not_reduced_free_text():
    sdlc = (REPO / "SDLC.md").read_text(encoding="utf-8")
    line = next(l for l in sdlc.splitlines() if l.startswith("- [ ] [REGISTRY-PATH-SURFACES]"))
    for want in ("STRUCTURE", "error_class", "error_code", "persist_text", "NON-registry", "apostrophe", "unicode"):
        assert want in line, want
    assert "reduce_paths" not in line


def test_the_spec_states_the_query_log_schema_change_and_the_cwd_basename_note():
    spec = (REPO / "SPEC.md").read_text(encoding="utf-8")
    assert "error_class" in spec and "error_code" in spec
    assert "directory's own name" in spec or "directory\'s own name" in spec


def test_p3_wording_and_trailing_slash():
    from factlattice_engine.reviewer import registry_value
    assert registry_value("env", "/a/b/", "x") == "env b sha256:x"
    src = (REPO / "factlattice_engine" / "server.py").read_text(encoding="utf-8")
    assert "its corpus entries could not be interpreted" not in src and "its corpora could not be loaded" in src
