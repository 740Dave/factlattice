"""[UNKNOWN-ARGUMENT-REFUSED] a tool call carrying an argument name the tool's advertised inputSchema does not
declare is REFUSED (`success: false, refused: true, code: unknown_argument`), before anything is evaluated.

Before this rule a misspelt filter (`prefixx`) was dropped silently and the answer WIDENED: a wrong answer that
looked right. ONE choke point (`tools.dispatch`) serves every door: the MCP server, the `call` CLI, the reviewer
helpers and in-process library callers; the schema is `server.TOOLS`, the same one the tool advertises.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from factlattice_engine import tools
from factlattice_engine.server import TOOLS, Server, build_tools

REPO = Path(__file__).resolve().parent.parent
DOC = ("# Doc [P-DOC]\n\n## Alpha one [ITEM-AB-ONE]\n\n- status: active\n\nbody\n\n"
       "## Alpha two [ITEM-AB-TWO]\n\n- status: active\n\nbody\n\n## Other [OTHER-ONE]\n\n- status: active\n\nbody\n")


@pytest.fixture()
def world(tmp_path, monkeypatch):
    (tmp_path / "dir-pub").mkdir()
    (tmp_path / "dir-pub" / "00.lts").write_text(DOC, encoding="utf-8")
    cfg = tmp_path / "registry.json"
    cfg.write_text(json.dumps({"pubcorp": {"roots": [{"path": str(tmp_path / "dir-pub"), "area": "specs"}],
                                           "domain_suffix": "proj.pub"}}), encoding="utf-8")
    state = tmp_path / "state"
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(cfg))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(state))
    monkeypatch.setenv("LTS_ENGINE_STATE_DIR", str(state))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    return tmp_path


def _server_door(name, arguments):
    srv = Server(build_tools(watch=False))
    resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    res = resp["result"]
    return json.loads(res["content"][0]["text"]), res["isError"]


def test_misspelt_prefix_is_refused_not_widened(world):
    ok, _ = _server_door("lts_find", {"corpus": "pubcorp", "prefix": "ITEM-AB", "tier": "digest"})
    assert ok["success"] is True and ok["count"] == 2
    bad, is_err = _server_door("lts_find", {"corpus": "pubcorp", "prefixx": "ITEM-AB", "tier": "digest"})
    assert is_err is True
    assert bad["success"] is False and bad["refused"] is True and bad["code"] == "unknown_argument"
    assert bad["unknown"] == ["prefixx"]
    assert bad["nearest"] == {"prefixx": "prefix"}
    assert bad["reason"] == "unknown_argument"
    assert "count" not in bad and "nodes" not in bad            # nothing was evaluated


def test_two_unknown_keys_are_all_listed_with_their_nearest(world):
    bad, _ = _server_door("lts_find", {"corpus": "pubcorp", "corpra": "x", "prefixx": "ITEM", "zzqqxx": 1})
    assert bad["code"] == "unknown_argument"
    assert bad["unknown"] == ["corpra", "prefixx", "zzqqxx"]
    assert bad["nearest"]["corpra"] in ("corpus", "corpora")
    assert bad["nearest"]["prefixx"] == "prefix" and bad["nearest"]["zzqqxx"] is None


def test_value_is_never_echoed(world):
    bad, _ = _server_door("lts_find", {"corpus": "pubcorp", "prefixx": "CANARY-VALUE-123"})
    assert "CANARY-VALUE-123" not in json.dumps(bad)


@pytest.mark.parametrize("tool", [t["name"] for t in TOOLS])
def test_every_advertised_tool_refuses_an_unknown_key(world, tool, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("the call was evaluated")
    monkeypatch.setattr(tools, "_dispatch", _boom)
    res = tools.dispatch(tool, {"definitely_not_declared_zzq": "v"}, tools=None)
    assert res["success"] is False and res["refused"] is True and res["code"] == "unknown_argument"
    assert res["unknown"] == ["definitely_not_declared_zzq"]


@pytest.mark.parametrize("tool", [t["name"] for t in TOOLS])
def test_every_declared_argument_is_accepted(tool):
    """Negative control, built from the schemas: no declared name of any tool is refused (no false refusal)."""
    spec = next(t for t in TOOLS if t["name"] == tool)
    props = spec["inputSchema"].get("properties", {})
    args = {k: "x" for k in props}
    assert tools._refuse_unknown_arguments(tool, args) is None
    for k in props:                                             # and each one alone
        assert tools._refuse_unknown_arguments(tool, {k: "x"}) is None


def test_declared_arguments_reach_the_tool_through_dispatch(world):
    res = tools.dispatch("lts_find", {"corpus": "pubcorp", "prefix": "ITEM-AB", "limit": 1, "tier": "digest"},
                         tools=build_tools(watch=False))
    assert res.get("code") != "unknown_argument" and res["success"] is True


def test_unknown_tool_name_is_left_to_the_existing_path(world):
    res = tools.dispatch("lts_nope", {"anything": 1}, tools=build_tools(watch=False))
    assert res.get("code") != "unknown_argument"


def test_call_cli_refuses_too(world):
    env_py = [sys.executable, "-m", "factlattice_engine", "call", "lts_find", "--json", "--args-json",
              json.dumps({"corpus": "pubcorp", "prefixx": "ITEM-AB", "tier": "digest"})]
    p = subprocess.run(env_py, capture_output=True, text=True, cwd=str(REPO), timeout=120)
    env = json.loads(p.stdout)
    out = env["result"]
    assert p.returncode != 0 and env["success"] is False
    assert out["code"] == "unknown_argument" and out["refused"] is True
    assert out["unknown"] == ["prefixx"] and out["nearest"] == {"prefixx": "prefix"}
    assert "count" not in out


def test_the_query_log_persists_the_code(world):
    _server_door("lts_find", {"corpus": "pubcorp", "prefixx": "ITEM-AB"})
    recs = [json.loads(l) for l in (world / "ql.jsonl").read_text().splitlines() if l.strip()]
    mine = [r for r in recs if r.get("tool") == "lts_find"]
    assert mine and mine[-1].get("error_code") == "unknown_argument", mine
