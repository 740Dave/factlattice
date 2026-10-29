"""`python -m factlattice_engine call <tool>` — the engine's command-line door (brief P1-P8).

The design brief (approved by the maintainer). Each test below is the proof of one property:

  P1  parity            test_parity_*            (every READ tool, through BOTH doors)
  P2  read-only default test_writer_*            (planted writer: refused, then allowed)
  P3  privacy           test_private_*           (same response as the server, byte for byte)
  P4  stated config     test_context_*           (registry path, corpus count, package, build)
  P5  machine-readable  test_exit_*              (an errored call never exits 0)
  P6  fail closed       test_exit_* / test_unknown_*
  P8  query log, ledger test_querylog_* / test_ledger_*
  checklist             test_every_server_tool_is_covered_by_call

⛔ THE SERVER SIDE OF EVERY COMPARISON IS DRIVEN THROUGH `Server.handle` (the JSON-RPC
handler, `tools/call`), NOT through `dispatch`: a divergence between the two DOORS (the
content envelope, `isError`, the engine each builds) is exactly what a dispatch-vs-dispatch
test cannot see.

⛔ NEGATIVE CONTROL: `test_parity_instrument_fires_on_a_planted_difference` proves the
comparison is not a check that can only report success.

⛔ IMPORTS OF THE NEW CODE ARE LAZY (inside tests/fixtures), so that before the feature
exists each test fails FOR THE MISSING FEATURE, one by one, not as one collection error.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from factlattice import ledger
from factlattice_engine import querylog
from factlattice_engine.server import TOOLS, Server, build_tools

REPO = Path(__file__).resolve().parent.parent

PUB, PRIV_A, PRIV_B = "pubcorp", "quokka-personality", "vaultzed"
DOC = ("# Doc [P-DOC]\n\n## Pub note [PUB-1]\n\n- status: active\n\nhello world body\n\n"
       "## Other note [PUB-2]\n\n- status: active\n- depends-on: [PUB-1]\n\nsecond\n")

WRITE_DOC = "# W [W-DOC]\n\n## Target [W-1]\n\n- status: active\n\nbody\n"


# --------------------------------------------------------------------------- the world
def _registry(tmp: Path, name="registry.json", pub_dir="dir-pub", with_private=True) -> Path:
    reg = {PUB: {"roots": [{"path": str(tmp / pub_dir), "area": "specs"}],
                 "domain_suffix": "proj.pub"}}
    if with_private:
        reg[PRIV_A] = {"roots": [{"path": str(tmp / "dir-quokka"), "area": "specs"}],
                       "domain_suffix": "proj.quokkaself"}
        reg[PRIV_B] = {"roots": [{"path": str(tmp / "dir-vaultzed"), "area": "specs"}],
                       "domain_suffix": "proj.hushbox", "private": True}
    p = tmp / name
    p.write_text(json.dumps(reg), encoding="utf-8")
    return p


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """A registry with one public and two private corpora, and every state dir sandboxed."""
    for d, body in (("dir-pub", DOC),
                    ("dir-quokka", "# Q [Q-DOC]\n\n## Self [Q-1]\n\n- status: active\n\nhello\n"),
                    ("dir-vaultzed", "# V [V-DOC]\n\n## Vault [V-1]\n\n- status: active\n\nhello\n")):
        (tmp_path / d).mkdir()
        (tmp_path / d / "00.lts").write_text(body, encoding="utf-8")
    cfg = _registry(tmp_path)
    state = tmp_path / "state"
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(cfg))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(state))
    monkeypatch.setenv("LTS_ENGINE_STATE_DIR", str(state))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    return tmp_path


# ------------------------------------------------------------------------ the two doors
def server_door(name, arguments, srv=None):
    """The MCP door: a real JSON-RPC `tools/call` through `Server.handle`. Returns the
    decoded payload and the envelope's isError flag."""
    srv = srv or Server(build_tools(watch=False))
    resp = srv.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    res = resp["result"]
    return json.loads(res["content"][0]["text"]), res["isError"]


def call_door(argv, capsys):
    """The command-line door, in process: `python -m factlattice_engine call ...`."""
    from factlattice_engine.__main__ import main
    capsys.readouterr()
    try:
        rc = main(["call", *argv])
    except SystemExit as e:                       # argparse usage errors
        rc = e.code if isinstance(e.code, int) else 2
    cap = capsys.readouterr()
    if rc == 2 and "invalid choice: 'call'" in cap.err:
        pytest.fail("MISSING FEATURE: the `call` subcommand does not exist: " + cap.err.strip().splitlines()[-1])
    return rc, cap.out, cap.err


def call_json(name, args, capsys, *extra):
    rc, out, err = call_door([name, "--args-json", json.dumps(args), "--json", *extra], capsys)
    return rc, json.loads(out), err


_VOLATILE_KEYS = {"elapsed_ms", "embedded_on_query"}


def scrub(o):
    """Remove ONLY what is legitimately different between two runs of the same tool: wall
    times, a per-run temp dir name, and the unembedded-count of a first semantic build."""
    if isinstance(o, dict):
        return {k: scrub(v) for k, v in o.items() if k not in _VOLATILE_KEYS}
    if isinstance(o, list):
        return [scrub(v) for v in o]
    if isinstance(o, str):
        s = re.sub(r"lts-preview-solo-\w+", "lts-preview-solo-X", o)
        return re.sub(r"\d+(\.\d+)? ?ms\b", "N ms", s)
    return o


# Arguments for every READ tool. `test_read_tool_list_tracks_the_server` makes a new tool
# a failure here until it has an entry.
def _read_args(tmp: Path) -> dict:
    return {
        "lts_find": {"corpus": PUB, "text": "hello"},
        "lts_find_in_prose": {"corpus": PUB, "text": "hello"},
        "lts_node": {"corpus": PUB, "node_id": "PUB-1"},
        "lts_traverse": {"corpus": PUB, "start_node": "PUB-2"},
        "lts_capabilities": {"corpus": PUB},
        "lts_list_corpora": {"heal": False},
        "lts_reverse": {"corpus": PUB, "node_id": "PUB-1"},
        "lts_history": {"corpus": PUB, "node_id": "PUB-1"},
        "lts_aggregate": {"op": "count", "corpus": PUB},
        "lts_profile_audit": {"corpus": PUB},
        "lts_lint": {"source": str(tmp / "dir-pub")},
        "lts_selfcheck": {},
        "lts_preview": {"content": "# X [X-1]\n\n- status: active\n"},
        "lts_flow": {"corpus": PUB, "start_node": "PUB-2"},
        "lts_property_loss": {"roots": [str(tmp / "dir-pub")]},
        "lts_semantic_find": {"corpus": PUB, "text": "hello"},
    }


READ_TOOLS = ["lts_find", "lts_find_in_prose", "lts_node", "lts_traverse", "lts_capabilities",
              "lts_list_corpora", "lts_reverse", "lts_history", "lts_aggregate",
              "lts_profile_audit", "lts_lint", "lts_selfcheck", "lts_preview", "lts_flow",
              "lts_property_loss", "lts_semantic_find"]
WRITE_TOOLS = ["lts_add_property", "lts_add_node", "lts_repair_property",
               "lts_rename_property", "lts_remove_property",
               "lts_register_corpus", "lts_unregister_corpus", "lts_report_miss"]


# ============================================================ checklist: coverage is total
def _coverage_problems():
    """Everything wrong with `call`'s classification of the tools the server serves RIGHT NOW.
    A function (not inline asserts) so that a negative control can feed it a planted tool."""
    from factlattice_engine import callcli
    from factlattice_engine.tools import LTS_READ_ONLY_TOOL_NAMES, LTS_WRITE_TOOL_NAMES
    served = {t["name"] for t in server_module().TOOLS}
    read, write = set(callcli.read_tool_names()), set(callcli.write_tool_names())
    out = []
    if read & write:
        out.append(f"both read and write: {sorted(read & write)}")
    if read | write != served:
        out.append(f"unclassified: {sorted(served - (read | write))}")
    if not read <= set(LTS_READ_ONLY_TOOL_NAMES):
        out.append(f"read but not on the engine's read allow-list: {sorted(read - set(LTS_READ_ONLY_TOOL_NAMES))}")
    # a served tool that is on NEITHER of the engine's lists was classified write by default:
    # that is the safe direction, but it must be a DELIBERATE choice, so it fails here
    deliberate = set(LTS_READ_ONLY_TOOL_NAMES) | set(LTS_WRITE_TOOL_NAMES)
    if served - deliberate:
        out.append(f"on neither engine list (defaulted to write): {sorted(served - deliberate)}")
    if write != set(LTS_WRITE_TOOL_NAMES) & served and not (served - deliberate):
        out.append("write set differs from tools.LTS_WRITE_TOOL_NAMES")
    return out


def server_module():
    import factlattice_engine.server as srv
    return srv


def test_every_server_tool_is_covered_by_call():
    """THE CHECKLIST RULE, ENFORCED, and able to FAIL: every served tool is exactly one of
    read / write, reads are on the engine's explicit read allow-list, and a tool on neither
    engine list is a failure here (it would be refused as a writer, never silently run)."""
    assert _coverage_problems() == []


def _planted_tool(monkeypatch, name="lts_planted_new_tool"):
    srv = server_module()
    spec = {"name": name, "description": "planted", "inputSchema": {"type": "object", "properties": {}}}
    monkeypatch.setattr(srv, "TOOLS", list(srv.TOOLS) + [spec])
    return name


def test_coverage_check_fires_on_an_unclassified_tool(monkeypatch):
    """F6 negative control: plant a tool the engine lists nowhere. The coverage check must
    report it (this is the in-suite proof; the build report also shows the mutation run)."""
    name = _planted_tool(monkeypatch)
    problems = _coverage_problems()
    assert problems and any(name in p for p in problems), problems


def test_an_unlisted_tool_is_refused_by_default_as_a_writer(world, capsys, monkeypatch):
    """F4 fail closed: classification is a READ ALLOW-LIST. A tool the engine has not
    explicitly declared read-only is refused without --allow-write (exit 3)."""
    name = _planted_tool(monkeypatch)
    rc, env, _ = call_json(name, {}, capsys)
    assert rc == 3 and env["refused"] is True and "--allow-write" in env["error"]
    from factlattice_engine import callcli
    assert name in callcli.write_tool_names() and name not in callcli.read_tool_names()


def test_read_allow_list_names_only_served_tools():
    from factlattice_engine.tools import LTS_READ_ONLY_TOOL_NAMES
    served = {t["name"] for t in server_module().TOOLS}
    assert set(LTS_READ_ONLY_TOOL_NAMES) <= served
    assert len(set(LTS_READ_ONLY_TOOL_NAMES)) == len(LTS_READ_ONLY_TOOL_NAMES)


def test_verdict_gate_table_names_only_real_tools_and_is_one_table():
    """F4: tool-specific verdict knowledge lives in ONE named table, and every entry names a
    real served tool (a renamed tool must not leave a dead gate behind)."""
    from factlattice_engine import callcli
    served = {t["name"] for t in server_module().TOOLS}
    assert set(callcli.VERDICT_GATES) and set(callcli.VERDICT_GATES) <= served
    assert {"lts_selfcheck", "lts_lint"} <= set(callcli.VERDICT_GATES)
    src = (REPO / "factlattice_engine" / "callcli.py").read_text(encoding="utf-8")
    body = src[src.index("def _gate"):src.index("def _emit")]
    assert "lts_selfcheck" not in body and "lts_lint" not in body   # no tool names left in the function


def test_verdict_gate_entry_is_applied(world, capsys, monkeypatch):
    """A planted gate entry on a real tool changes the exit status: the table is what decides."""
    from factlattice_engine import callcli
    monkeypatch.setitem(callcli.VERDICT_GATES, "lts_list_corpora",
                        ("planted_flag", lambda v: v is True, "planted verdict not true"))
    rc, env, _ = call_json("lts_list_corpora", {"heal": False}, capsys)
    assert rc == 1 and "planted verdict not true" in env["error"]


def test_read_tool_list_tracks_the_server():
    """This file's own tables must track the server: a tool added to server.TOOLS fails here
    until it is given parity arguments (read) or named a writer (write)."""
    from factlattice_engine import callcli
    served = {t["name"] for t in TOOLS}
    assert set(READ_TOOLS) == set(callcli.read_tool_names())
    assert set(WRITE_TOOLS) == set(callcli.write_tool_names())
    assert set(READ_TOOLS) | set(WRITE_TOOLS) == served
    assert set(_read_args(Path("/x"))) == set(READ_TOOLS)


def test_call_list_prints_every_covered_tool(capsys):
    rc, out, _ = call_door(["--list", "--json"], capsys)
    assert rc == 0
    listing = json.loads(out)
    assert set(listing["read"]) == set(READ_TOOLS)
    assert set(listing["write"]) == set(WRITE_TOOLS)
    assert "--allow-write" in listing["write_opt_in"]


# ===================================================================== P1: parity (reads)
@pytest.mark.parametrize("tool", READ_TOOLS)
def test_parity_read_tool_call_equals_the_server_door(tool, world, capsys):
    """Same inputs, same registry, same build: the payload `call` prints is the payload the
    MCP `tools/call` handler returns, for EVERY read tool."""
    args = _read_args(world)[tool]
    if tool == "lts_semantic_find":                     # warm the sidecar so neither door is "first"
        server_door(tool, args)
    srv_payload, srv_is_error = server_door(tool, args)
    rc, env, _ = call_json(tool, args, capsys)
    assert env["result"] is not None
    assert scrub(env["result"]) == scrub(srv_payload)
    # and the exit status agrees with the server's own verdict: 0 iff the tool succeeded
    assert srv_is_error is False
    assert rc == 0 and env["success"] is True and env["exit_code"] == 0


def test_parity_instrument_fires_on_a_planted_difference(world, capsys):
    """NEGATIVE CONTROL: change one body word in the corpus between the two doors and the
    comparison must see it, with the scrub in place."""
    args = {"corpus": PUB, "node_id": "PUB-1"}
    srv_payload, _ = server_door("lts_node", args)
    (world / "dir-pub" / "00.lts").write_text(DOC.replace("hello world body", "goodbye"), encoding="utf-8")
    rc, env, _ = call_json("lts_node", args, capsys)
    assert rc == 0
    assert scrub(env["result"]) != scrub(srv_payload)


def test_parity_default_output_is_the_bare_result_on_stdout(world, capsys):
    """Without --json, stdout is the tool's result (so it pipes to jq) and the configuration
    statement goes to stderr: a human sees both, a pipe gets clean JSON."""
    args = _read_args(world)["lts_node"]
    srv_payload, _ = server_door("lts_node", args)
    rc, out, err = call_door(["lts_node", "--args-json", json.dumps(args)], capsys)
    assert rc == 0
    assert scrub(json.loads(out)) == scrub(srv_payload)
    assert "registry" in err.lower() and "build" in err.lower()


def test_args_file_equals_args_json(world, capsys, tmp_path):
    args = _read_args(world)["lts_node"]
    f = tmp_path / "args.json"
    f.write_text(json.dumps(args))
    rc1, env1, _ = call_json("lts_node", args, capsys)
    rc2, out, _ = call_door(["lts_node", "--args-file", str(f), "--json"], capsys)
    assert rc1 == rc2 == 0
    assert scrub(json.loads(out)["result"]) == scrub(env1["result"])


# ===================================================================== P2: writers refused
def _writer_args(f: Path) -> dict:
    return {"path": str(f), "node_id": "W-1", "key": "flag", "value": "yes"}


@pytest.fixture()
def planted(tmp_path):
    f = tmp_path / "planted" / "w.lts"
    f.parent.mkdir()
    f.write_text(WRITE_DOC, encoding="utf-8")
    return f


def test_writer_is_refused_by_default_and_the_refusal_names_the_opt_in(world, planted, capsys):
    before = planted.read_bytes()
    rc, env, _ = call_json("lts_add_property", _writer_args(planted), capsys)
    assert rc != 0
    assert env["success"] is False and env["refused"] is True
    assert "--allow-write" in env["error"]
    assert planted.read_bytes() == before                    # nothing written
    assert not (world / "bak").exists() or not any((world / "bak").rglob("*"))
    assert ledger.read_lines(os.path.realpath(planted)) in (None, [])


@pytest.mark.parametrize("tool", WRITE_TOOLS)
def test_every_writer_is_refused_by_default(tool, world, capsys):
    """Not just the one planted: every tool in the engine's write list, with arguments that
    would otherwise be accepted or at least be tried."""
    rc, env, _ = call_json(tool, {"corpus_id": "x", "corpus": PUB, "query": "q",
                                  "expected_ids": ["PUB-1"], "why": "w"}, capsys)
    assert rc != 0 and env["refused"] is True
    assert "--allow-write" in env["error"]


def test_writer_runs_with_the_opt_in_and_is_ledgered(world, planted, capsys):
    rc, env, _ = call_json("lts_add_property", _writer_args(planted), capsys, "--allow-write")
    assert rc == 0, env
    assert env["success"] is True and env["refused"] is False
    assert "- flag: yes" in planted.read_text()
    recs = ledger.read_lines(os.path.realpath(planted))
    assert recs and len(recs) == 1
    assert env["call_context"]["allow_write"] is True


def test_allow_write_does_not_widen_a_read(world, capsys):
    """The opt-in is for writers; a read with the flag is the same read."""
    args = _read_args(world)["lts_node"]
    rc1, env1, _ = call_json("lts_node", args, capsys)
    rc2, env2, _ = call_json("lts_node", args, capsys, "--allow-write")
    assert rc1 == rc2 == 0 and scrub(env1["result"]) == scrub(env2["result"])


# ===================================================================== P3: privacy
def _strings(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(o, (list, tuple)):
        for v in o:
            yield from _strings(v)
    elif o is not None:
        yield str(o)


PRIVATE_TOKENS = [PRIV_A, PRIV_B, "proj.quokkaself", "proj.hushbox", "dir-quokka", "dir-vaultzed"]


def _leaks(o):
    return sorted({t for s in _strings(o) for t in PRIVATE_TOKENS if t in s})


FANOUT = [("lts_find", {"text": "hello"}),                       # corpus-omitted fan-out
          ("lts_find", {"corpus": PRIV_A[:-1], "text": "hello"}),   # a typo one char off a private id
          ("lts_node", {"corpus": PRIV_B[:-1], "node_id": "V-1"}),
          ("lts_list_corpora", {"heal": False}),
          ("lts_capabilities", {"corpus": PRIV_A[:-1]}),
          ("lts_traverse", {"corpus": "zzz-nothing", "start_node": "PUB-1"})]


@pytest.mark.parametrize("tool,args", FANOUT)
def test_private_corpus_reads_get_the_servers_response_and_leak_no_name(tool, args, world, capsys):
    """P3: a caller cannot reach (or learn the names of) a private corpus by using the
    command line instead of the server. The response is the server's, and the WHOLE
    envelope (call_context included) names no private corpus."""
    srv_payload, _ = server_door(tool, args)
    rc, env, err = call_json(tool, args, capsys)
    assert scrub(env["result"]) == scrub(srv_payload)
    # lts_list_corpora is the engine's explicit enumeration surface (test_private_names_never_leak
    # exempts it too): its RESULT is the server's own and is compared above; everything `call`
    # itself adds around it must still name nothing.
    scope = {k: v for k, v in env.items() if k != "result"} if tool == "lts_list_corpora" else env
    assert _leaks(scope) == [], _leaks(scope)
    assert _leaks(err) == []


def test_private_corpus_named_exactly_is_readable_exactly_as_by_the_server(world, capsys):
    """Naming a private corpus is how it is read (the rule is about fan-out and names, not a
    wall): the two doors agree there too, which is the point of one code path."""
    args = {"corpus": PRIV_A, "node_id": "Q-1"}
    srv_payload, _ = server_door("lts_node", args)
    rc, env, _ = call_json("lts_node", args, capsys)
    assert rc == 0 and scrub(env["result"]) == scrub(srv_payload)


def test_private_rule_is_not_reimplemented_in_the_cli_module():
    """P3 by construction: the CLI module does not carry a private-corpus rule of its own."""
    src = (REPO / "factlattice_engine" / "callcli.py").read_text(encoding="utf-8")
    for needle in ("-personality", "private_reason", "is_private_partition", "PRIVATE_SUFFIX"):
        assert needle not in src, needle


# ===================================================================== P4: stated config
def test_context_names_registry_package_and_build(world, capsys):
    rc, env, _ = call_json("lts_list_corpora", {"heal": False}, capsys)
    ctx = env["call_context"]
    assert ctx["corpus_config"]["env_var"] == "FACTLATTICE_CORPUS_CONFIG"
    assert ctx["corpus_config"]["path"] == str(world / "registry.json")
    assert ctx["corpus_config"]["loaded"] is True
    assert ctx["registry"]["corpus_count"] == 3
    assert ctx["engine_package"]["path"] == str(REPO / "factlattice_engine")
    build = ctx["build"]
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", build["source_sha256"])
    assert build["git_head"] is None or re.fullmatch(r"[0-9a-f]{40}", build["git_head"])
    assert build["git_dirty"] in (True, False, None)
    if build["git_head"] is None:
        assert "unknown" in build["git_note"]
    else:
        assert build["git_head"] == subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def test_context_a_wrong_registry_is_visible_in_the_output(world, capsys, monkeypatch):
    """A run against the wrong registry must be visible, not discovered later."""
    other = world / "other"
    other.mkdir()
    (other / "dir-pub").mkdir()
    (other / "dir-pub" / "00.lts").write_text("# O [O-DOC]\n\n## Only [O-1]\n\n- status: active\n", encoding="utf-8")
    alt = _registry(other, name="ALT-REGISTRY.json", with_private=False)
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(alt))
    rc, out, err = call_door(["lts_list_corpora", "--args-json", '{"heal": false}'], capsys)
    assert rc == 0
    assert str(alt) in err                                   # the human banner names it
    rc, env, _ = call_json("lts_list_corpora", {"heal": False}, capsys)
    assert env["call_context"]["corpus_config"]["path"] == str(alt)
    assert env["call_context"]["registry"]["corpus_count"] == 1
    ids = [c.get("corpus_id") or c.get("id") for c in env["result"]["corpora"]]
    assert ids == [PUB]


def test_context_config_path_set_but_missing_is_a_stop_and_never_a_fallback(world, capsys, monkeypatch):
    """SPEC-U1a CHANGED THIS PIN. `config.default_corpora()` used to fall back silently to its built-in
    EXAMPLE registry when the configured file did not exist (this test asserted the fallback was merely
    ANNOUNCED). That was the quiet wrong-registry case. It is now a stop: exit 2, `registry-not-found`,
    `NOT LOADED`, and the note must not claim the example registry is in use."""
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world / "no-such-registry.json"))
    rc, env, err = call_json("lts_selfcheck", {}, capsys)
    assert rc == 2 and env["success"] is False and "registry-not-found" in env["error"]
    cc = env["call_context"]["corpus_config"]
    assert cc["loaded"] is False
    assert cc["path"] == str(world / "no-such-registry.json")
    assert "NO fallback" in cc["note"] and "example registry is in use" not in cc["note"]
    assert "NOT LOADED" in call_door(["lts_selfcheck"], capsys)[2]


def test_context_no_registry_at_all_still_runs_store_free_tools(world, capsys, monkeypatch):
    """The newcomer's first run has no corpora: selfcheck and lint must still work."""
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG")
    rc, env, err = call_json("lts_selfcheck", {}, capsys)
    assert rc == 0, env
    cc = env["call_context"]["corpus_config"]
    assert cc["path"] is None and cc["loaded"] is False
    assert "built-in example registry" in cc["note"]
    assert env["call_context"]["registry"]["corpus_count"] is not None     # stated, whatever it is


# ===================================================================== P5/P6: exit status
def test_exit_unknown_tool_fails_closed_and_names_the_covered_tools(world, capsys):
    rc, env, _ = call_json("lts_nope", {}, capsys)
    assert rc == 2 and env["success"] is False
    assert "unknown tool" in env["error"] and "lts_find" in env["error"]
    assert "result" not in env or env["result"] is None


def test_exit_malformed_args_json(world, capsys):
    rc, out, err = call_door(["lts_find", "--args-json", "{not json", "--json"], capsys)
    assert rc == 2
    env = json.loads(out)
    assert env["success"] is False and "args" in env["error"].lower()


@pytest.mark.parametrize("bad", ['[1,2]', '"str"', '3', 'null'])
def test_exit_args_must_be_a_json_object(bad, world, capsys):
    rc, out, _ = call_door(["lts_find", "--args-json", bad, "--json"], capsys)
    assert rc == 2 and json.loads(out)["success"] is False


def test_exit_args_file_missing(world, capsys):
    rc, out, _ = call_door(["lts_find", "--args-file", str(world / "nope.json"), "--json"], capsys)
    assert rc == 2 and json.loads(out)["success"] is False


def test_exit_both_args_sources_is_an_error(world, capsys):
    f = world / "a.json"
    f.write_text("{}")
    rc, out, _ = call_door(["lts_find", "--args-json", "{}", "--args-file", str(f), "--json"], capsys)
    assert rc == 2 and json.loads(out)["success"] is False


def test_exit_tool_error_is_nonzero_and_shown(world, capsys):
    """The server's `isError: true` case (an unknown corpus). The result is still printed
    (it is the diagnosis), the exit is 1, and it is the server's own payload."""
    args = {"corpus": "zz-not-a-corpus", "node_id": "NOPE-1"}
    srv_payload, srv_is_error = server_door("lts_node", args)
    assert srv_is_error is True and srv_payload["success"] is False       # the premise
    rc, env, _ = call_json("lts_node", args, capsys)
    assert rc == 1 and env["success"] is False and env["exit_code"] == 1
    assert scrub(env["result"]) == scrub(srv_payload)
    assert env["error"] == srv_payload["error"]


def test_exit_missing_required_argument_is_nonzero(world, capsys):
    rc, env, _ = call_json("lts_node", {"corpus": PUB}, capsys)
    assert rc == 1 and env["success"] is False and env["exit_code"] == 1


def test_exit_an_exception_in_dispatch_never_exits_zero(world, capsys, monkeypatch):
    import factlattice_engine.callcli as cc

    def boom(*a, **k):
        raise RuntimeError("planted")
    monkeypatch.setattr(cc, "_dispatch", boom, raising=False)
    monkeypatch.setattr("factlattice_engine.tools.dispatch", boom)
    rc, env, _ = call_json("lts_find", {"corpus": PUB, "text": "x"}, capsys)
    assert rc != 0 and env["success"] is False and "planted" in env["error"]


def test_exit_selfcheck_that_fails_never_looks_like_a_pass(world, capsys, monkeypatch):
    """A verdict:FAIL is `success: true` at the transport (the tool ran). The command line must
    not exit 0 on it: an errored/failed check must never look like a passed one."""
    import factlattice_engine.tools as T
    real = T.dispatch

    def fail_selfcheck(name, arguments, tools=None):
        if name == "lts_selfcheck":
            return {"success": True, "verdict": "FAIL", "cases": []}
        return real(name, arguments, tools)
    monkeypatch.setattr(T, "dispatch", fail_selfcheck)
    monkeypatch.setattr("factlattice_engine.callcli._dispatch", fail_selfcheck, raising=False)
    rc, env, _ = call_json("lts_selfcheck", {}, capsys)
    assert rc == 1 and env["success"] is False and "FAIL" in env["error"]


def test_exit_selfcheck_pass_is_zero_and_lint_errors_are_nonzero(world, capsys, tmp_path):
    rc, env, _ = call_json("lts_selfcheck", {}, capsys)
    assert rc == 0 and env["result"]["verdict"] == "PASS"
    bad = tmp_path / "bad.lts"
    bad.write_text("not a document\n- [[[\n", encoding="utf-8")
    rc, env, _ = call_json("lts_lint", {"source": str(bad)}, capsys)
    assert env["result"]["exit_code"] == rc            # lint's own exit code, carried through


def test_exit_missing_engine_module_is_loud_and_nonzero(tmp_path):
    """P6, package missing: a module the call path needs cannot be imported. The process must
    say so and exit non-zero, in a subprocess so the import state is really broken."""
    code = ("import sys, factlattice_engine\n"
            "sys.modules['factlattice_engine.tools'] = None\n"
            "from factlattice_engine.__main__ import main\n"
            "sys.exit(main(['call', 'lts_selfcheck', '--json']))\n")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(REPO),
                       env={**os.environ, "FACTLATTICE_STATE_DIR": str(tmp_path / "s"),
                            "FACTLATTICE_QUERY_LOG": str(tmp_path / "q.jsonl"),
                            "LTS_ENGINE_STATE_DIR": str(tmp_path / "s"),
                            "FACTLATTICE_BACKUP_DIR": str(tmp_path / "b")})
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert "cannot import" in (p.stdout + p.stderr).lower()


def test_cli_end_to_end_as_a_real_subprocess(world):
    """`python -m factlattice_engine call` as a newcomer runs it: stdout JSON, exit 0."""
    p = subprocess.run([sys.executable, "-m", "factlattice_engine", "call", "lts_selfcheck", "--json"],
                       capture_output=True, text=True, cwd=str(REPO))
    assert p.returncode == 0, p.stderr
    env = json.loads(p.stdout)
    assert env["result"]["verdict"] == "PASS" and env["call_context"]["registry"]["corpus_count"] == 3
    q = subprocess.run([sys.executable, "-m", "factlattice_engine", "call", "lts_nope", "--json"],
                       capture_output=True, text=True, cwd=str(REPO))
    assert q.returncode == 2 and json.loads(q.stdout)["success"] is False


# ===================================================================== P8: log + ledger
def _log_records(tmp: Path) -> list:
    p = tmp / "ql.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


_LOG_VOLATILE = {"ts", "session", "elapsed_ms"}


def _norm_rec(r):
    return {k: v for k, v in r.items() if k not in _LOG_VOLATILE}


@pytest.mark.parametrize("tool", ["lts_find", "lts_node", "lts_aggregate", "lts_find_in_prose"])
def test_querylog_a_call_is_logged_exactly_as_the_server_logs_it(tool, world):
    args = _read_args(world)[tool]
    server_door(tool, args)
    after_server = _log_records(world)
    assert len(after_server) == 1
    # a REAL separate process, as a user runs it (in process the session id would be shared)
    p = subprocess.run([sys.executable, "-m", "factlattice_engine", "call", tool,
                        "--args-json", json.dumps(args), "--json"],
                       capture_output=True, text=True, cwd=str(REPO))
    assert p.returncode == 0, p.stderr
    recs = _log_records(world)
    assert len(recs) == 2
    assert _norm_rec(recs[1]) == _norm_rec(recs[0])
    assert recs[1]["session"] != recs[0]["session"]        # the stated difference: own process


def test_querylog_the_difference_is_stated_in_the_output(world, capsys):
    rc, env, _ = call_json("lts_find", _read_args(world)["lts_find"], capsys)
    ql = env["call_context"]["query_log"]
    assert ql["enabled"] is True and ql["path"] == str(world / "ql.jsonl")
    assert ql["logged_for_this_tool"] is True
    diffs = " ".join(env["call_context"]["differences_from_server"])
    assert "session" in diffs and "file watcher" in diffs.lower()


def test_querylog_a_disabled_log_is_stated(world, capsys, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", "0")
    rc, env, _ = call_json("lts_find", _read_args(world)["lts_find"], capsys)
    assert env["call_context"]["query_log"]["enabled"] is False


def test_ledger_a_call_write_is_ledgered_exactly_as_the_server_write(world, tmp_path, capsys):
    a = tmp_path / "A" / "w.lts"
    b = tmp_path / "B" / "w.lts"
    for f in (a, b):
        f.parent.mkdir()
        f.write_text(WRITE_DOC, encoding="utf-8")
    srv_payload, _ = server_door("lts_add_property", _writer_args(a))
    assert srv_payload["success"] is True
    rc, env, _ = call_json("lts_add_property", _writer_args(b), capsys, "--allow-write")
    assert rc == 0

    def recs(f):
        return [json.loads(x) for x in ledger.read_lines(os.path.realpath(f))]

    ra, rb = recs(a), recs(b)
    assert len(ra) == len(rb) == 1
    keep = ("verb", "key", "ledger_seq")
    assert {k: ra[0].get(k) for k in keep} == {k: rb[0].get(k) for k in keep}
    assert set(ra[0]) == set(rb[0])
    assert a.read_text() == b.read_text()
    q = _log_records(world)
    assert [r["tool"] for r in q] == ["lts_add_property", "lts_add_property"]
    assert env["call_context"]["ledger"]["ledgered_for_this_tool"] is True


# ============================================================ the rule and the docs page
def test_checklist_rule_and_docs_page_exist_and_are_wired():
    sdlc = (REPO / "SDLC.md").read_text(encoding="utf-8")
    for rule in ("[CLI-CALL-COVERAGE]", "[CLI-CALL-ONE-PATH]", "[CLI-CALL-STATES-ITS-CONFIG]"):
        assert rule in sdlc
    page = (REPO / "docs" / "TRY-BEFORE-YOU-DEPLOY.md").read_text(encoding="utf-8")
    for needle in ("call lts_selfcheck", "call lts_lint", "specs/40-domain-profiles.lts",
                   '"verdict": "PASS"', "exit status: 1", "--allow-write"):
        assert needle in page, needle
    assert (REPO / "specs" / "40-domain-profiles.lts").is_file()       # the file the page lints



# ======================================================= review remediation (F1, F2, F3)
STORE_FREE_SAMPLE = [("lts_selfcheck", {}),
                     ("lts_preview", {"content": "# X [X-1]\n\n- status: active\n"}),
                     ("lts_lint", None)]            # None -> {"source": <a real dir>}


def _zero_registry(world, monkeypatch, kind):
    if kind == "empty":
        p = world / "empty-registry.json"
        p.write_text("{}")
    else:                                            # roots that do not exist
        p = world / "ghost-registry.json"
        p.write_text(json.dumps({"ghost": {"roots": [{"path": str(world / "nope"), "area": "specs"}],
                                           "domain_suffix": "proj.ghost"}}))
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(p))
    return p


@pytest.mark.parametrize("kind", ["empty", "ghost-roots"])
@pytest.mark.parametrize("tool,args", [("lts_find", {"corpus": PUB, "text": "x"}),
                                       ("lts_list_corpora", {"heal": False}),
                                       ("lts_node", {"corpus": PUB, "node_id": "PUB-1"}),
                                       ("lts_aggregate", {"op": "count"})])
def test_F1_corpus_tool_on_an_explicit_registry_that_loads_zero_corpora_fails(kind, tool, args, world, capsys, monkeypatch):
    p = _zero_registry(world, monkeypatch, kind)
    rc, env, err = call_json(tool, args, capsys)
    assert rc != 0 and env["success"] is False
    assert "registry loaded 0 corpora" in env["error"]
    assert str(p) in env["error"] or env["call_context"]["corpus_config"]["path"] == str(p)
    assert env["call_context"]["registry"]["corpus_count"] == 0


@pytest.mark.parametrize("tool,args", STORE_FREE_SAMPLE)
def test_F1_store_free_tools_keep_their_result_but_carry_a_top_level_warning(tool, args, world, capsys, monkeypatch):
    p = _zero_registry(world, monkeypatch, "empty")
    args = args if args is not None else {"source": str(world / "dir-pub")}
    rc, env, _ = call_json(tool, args, capsys)
    assert rc == 0 and env["success"] is True and env["result"]["success"] is True
    assert any("registry loaded 0 corpora" in w for w in env["warnings"]), env.get("warnings")
    # and on stderr in the human mode
    rc, out, err = call_door([tool, "--args-json", json.dumps(args)], capsys)
    assert "WARNING" in err and "registry loaded 0 corpora" in err
    json.loads(out)                                   # stdout is still clean JSON


def test_F1_the_no_config_example_registry_path_is_unchanged(world, capsys, monkeypatch):
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG")
    from factlattice_engine import config
    monkeypatch.setenv("FACTLATTICE_CORPUS_ROOT", str(world / "empty-root"))   # example registry -> 0 corpora
    rc, env, err = call_json("lts_selfcheck", {}, capsys)
    assert rc == 0 and "warnings" not in env
    assert env["call_context"]["registry"]["corpus_count"] == 0


def test_F1_a_loaded_registry_has_no_warning(world, capsys):
    rc, env, err = call_json("lts_selfcheck", {}, capsys)
    assert rc == 0 and "warnings" not in env


def test_F1_table_of_corpus_free_tools_names_only_real_tools():
    from factlattice_engine import callcli
    served = {t["name"] for t in server_module().TOOLS}
    assert callcli.CORPUS_FREE_TOOLS and set(callcli.CORPUS_FREE_TOOLS) <= served
    assert {"lts_selfcheck", "lts_lint", "lts_preview"} <= set(callcli.CORPUS_FREE_TOOLS)


def test_F2_stamp_covers_the_core_package_and_names_both_import_paths(world, capsys, tmp_path):
    import shutil
    import factlattice
    import factlattice_engine
    from factlattice_engine import callcli
    eng, core = Path(factlattice_engine.__file__).parent, Path(factlattice.__file__).parent
    for name, d in (("eng", eng), ("core", core)):
        shutil.copytree(d, tmp_path / name / d.name, ignore=shutil.ignore_patterns("__pycache__"))
    base = callcli.build_info(str(tmp_path / "eng" / eng.name), str(tmp_path / "core" / core.name))["source_sha256"]
    with open(tmp_path / "core" / core.name / "__init__.py", "a") as fh:
        fh.write("\n# edited\n")
    after = callcli.build_info(str(tmp_path / "eng" / eng.name), str(tmp_path / "core" / core.name))["source_sha256"]
    assert base != after, "editing factlattice/__init__.py must move the stamp"
    rc, env, _ = call_json("lts_selfcheck", {}, capsys)
    ctx = env["call_context"]
    assert ctx["engine_package"]["path"] == str(eng)
    assert ctx["core_package"]["path"] == str(core)
    assert ctx["packages_same_checkout"] is True and "warnings" not in env


def test_F2_packages_from_different_checkouts_are_called_out_loudly(world, capsys, monkeypatch, tmp_path):
    from factlattice_engine import callcli
    real = callcli._package_dirs()
    other = tmp_path / "elsewhere" / "factlattice"
    other.mkdir(parents=True)
    monkeypatch.setattr(callcli, "_package_dirs", lambda: (real[0], str(other)))
    rc, env, _ = call_json("lts_selfcheck", {}, capsys)
    assert env["call_context"]["packages_same_checkout"] is False
    assert any("different checkouts" in w for w in env["warnings"])
    rc, out, err = call_door(["lts_selfcheck"], capsys)
    assert "WARNING" in err and "different checkouts" in err


def _statement_ok(env, path):
    cc = env["call_context"]
    assert cc["corpus_config"]["path"] == path and "loaded" in cc["corpus_config"]
    assert "corpus_count" in cc["registry"]          # an int, or None meaning "not read"
    assert cc["registry"].get("count_note") or isinstance(cc["registry"]["corpus_count"], int)


@pytest.mark.parametrize("argv,rc_expected", [
    (["lts_nope", "--args-json", "{}"], 2),
    (["lts_find", "--args-json", "{bad"], 2),
    (["lts_find", "--args-json", "[1]"], 2),
    (["lts_add_property", "--args-json", "{}"], 3),
])
def test_F3_early_exit_paths_still_state_the_registry(argv, rc_expected, world, capsys):
    rc, out, err = call_door([*argv, "--json"], capsys)
    assert rc == rc_expected
    _statement_ok(json.loads(out), str(world / "registry.json"))
    rc, out, err = call_door(argv, capsys)           # human mode: the banner is on stderr
    assert "registry" in err.lower() and str(world / "registry.json") in err


def test_F3_malformed_registry_file_exit_names_the_registry_and_the_reason(world, capsys, monkeypatch):
    bad = world / "malformed.json"
    bad.write_text("{ not json")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(bad))
    rc, env, err = call_json("lts_find", {"corpus": PUB, "text": "x"}, capsys)
    assert rc == 2 and "cannot build the engine" in env["error"]
    _statement_ok(env, str(bad))
    assert env["call_context"]["registry"]["corpus_count"] is None
    assert "not read" in env["call_context"]["registry"]["count_note"]
    assert "JSONDecodeError" in env["call_context"]["registry"]["count_note"] or "JSONDecodeError" in env["error"]
    rc, out, err = call_door(["lts_find", "--args-json", "{}"], capsys)
    assert str(bad) in err


def test_F3_a_missing_registry_file_is_stated_on_an_early_exit(world, capsys, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world / "gone.json"))
    rc, env, err = call_json("lts_nope", {}, capsys)
    assert rc == 2
    assert env["call_context"]["corpus_config"]["loaded"] is False
    assert "example" not in env["call_context"]["corpus_config"].get("source", "")   # no fallback is reported
    assert "NOT LOADED" in call_door(["lts_nope"], capsys)[2]


def test_F4_sdlc_carries_the_verdict_gate_and_read_allow_list_rules():
    sdlc = (REPO / "SDLC.md").read_text(encoding="utf-8")
    assert "[CLI-CALL-VERDICT-GATE]" in sdlc and "[CLI-CALL-READ-ALLOW-LIST]" in sdlc


# ============================================ delta review finding 1: the allow-list is a LITERAL
PINNED_READ_ONLY = sorted([
    "lts_aggregate", "lts_capabilities", "lts_find", "lts_find_in_prose", "lts_flow", "lts_history",
    "lts_lint", "lts_list_corpora", "lts_node", "lts_preview", "lts_profile_audit",
    "lts_property_loss", "lts_reverse", "lts_selfcheck", "lts_semantic_find", "lts_traverse"])


def test_read_allow_list_is_pinned_to_exactly_these_names():
    from factlattice_engine.tools import LTS_READ_ONLY_TOOL_NAMES
    assert sorted(LTS_READ_ONLY_TOOL_NAMES) == PINNED_READ_ONLY, (
        "tools.LTS_READ_ONLY_TOOL_NAMES changed. Adding a read tool is a DELIBERATE TWO-PLACE EDIT: "
        "the literal in tools.py AND PINNED_READ_ONLY in this test, in the same change, with the "
        "proof the tool writes nothing in any corpus (SDLC 7.6 [CLI-CALL-READ-ALLOW-LIST]).")


def test_read_allow_list_is_a_literal_not_derived_from_layer_tuples():
    import ast
    src = (REPO / "factlattice_engine" / "tools.py").read_text(encoding="utf-8")
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "LTS_READ_ONLY_TOOL_NAMES" for t in n.targets))
    assert isinstance(node.value, ast.Tuple) and node.value.elts and all(
        isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.value.elts), (
        "LTS_READ_ONLY_TOOL_NAMES must be a tuple of string literals, not an expression over other tuples")


def test_a_tool_added_to_an_existing_layer_tuple_is_still_refused_by_default(tmp_path):
    """The real hazard, end to end: copy both packages, append a new tool to the EXISTING
    `LTS_TOOL_NAMES` tuple and to `server.TOOLS`, and do NOT touch the allow-list. `call` must
    refuse it without --allow-write (exit 3). Derived, it became a read and ran (exit 1)."""
    import shutil
    for d in ("factlattice", "factlattice_engine"):
        shutil.copytree(REPO / d, tmp_path / d, ignore=shutil.ignore_patterns("__pycache__"))
    t = tmp_path / "factlattice_engine" / "tools.py"
    src = t.read_text(encoding="utf-8")
    old = '    "lts_list_corpora", "lts_reverse", "lts_history",\n)'
    assert old in src
    t.write_text(src.replace(old, '    "lts_list_corpora", "lts_reverse", "lts_history", "lts_planted_layer_tool",\n)', 1),
                 encoding="utf-8")
    s = tmp_path / "factlattice_engine" / "server.py"
    ssrc = s.read_text(encoding="utf-8")
    marker = "_apply_tool_prominence(TOOLS)\n\n\n# [REG-RUNTIME"
    assert marker in ssrc
    s.write_text(ssrc.replace(
        marker, "TOOLS.append({'name': 'lts_planted_layer_tool', 'description': 'p', "
                "'inputSchema': {'type': 'object', 'properties': {}}})\n" + marker, 1), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "FACTLATTICE_STATE_DIR": str(tmp_path / "st"),
           "LTS_ENGINE_STATE_DIR": str(tmp_path / "st"), "FACTLATTICE_BACKUP_DIR": str(tmp_path / "bk"),
           "FACTLATTICE_QUERY_LOG": str(tmp_path / "q.jsonl"), "FACTLATTICE_CODE_WATCH": "0"}
    env.pop("FACTLATTICE_CORPUS_CONFIG", None)
    p = subprocess.run([sys.executable, "-m", "factlattice_engine", "call", "lts_planted_layer_tool", "--json"],
                       capture_output=True, text=True, cwd=str(tmp_path), env=env)
    out = json.loads(p.stdout)
    assert p.returncode == 3 and out["refused"] is True and "--allow-write" in out["error"], (p.returncode, p.stdout[:300])
