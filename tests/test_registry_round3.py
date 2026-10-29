"""U1a round 3 (Diamond FAIL at b475061): P1-1 hash-only mismatch, P1-2 no content in error text, P2-1 AST guard,
P2-2 no-registry boot is error mode, P2-3 exempt tools gated, P3s."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests import _registry_guard as G

REPO = Path(__file__).resolve().parent.parent
IDS = ("zz-planted-a", "zz-planted-b")
REGISTRY_WITH_IDS = {IDS[0]: {"root": "/nonexistent/a"}, IDS[1]: {"root": "/nonexistent/b"}}


@pytest.fixture()
def w(tmp_path, monkeypatch):
    st = tmp_path / "st"
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(st))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    for d in ("c1", "c2", "x"):
        (tmp_path / d).mkdir()
    (tmp_path / "x" / "a.lts").write_text("# D [D-1]\n\n## N [N-1]\n\n- status: active\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    p = type("W", (), {})()
    p.tmp = tmp_path
    p.pointer = Path(os.environ["XDG_CONFIG_HOME"]) / "factlattice" / "config.json"
    return p


def put_pointer(w, body):
    w.pointer.parent.mkdir(parents=True, exist_ok=True)
    w.pointer.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")


# ============================================================================ P1-1: the mismatch is the hash
def _mcp(sha, source="env", path="/r/x.json"):
    return {"source": source, "path": path, "sha256_at_boot": sha, "sha256_now": sha, "resolved_at": "t"}


def test_p1_1_a_env_symlink_vs_pointer_realpath_same_bytes_is_a_match(w, monkeypatch):
    from factlattice import registry as R
    from factlattice_engine.reviewer import compare_registries
    real = w.tmp / "real"; real.mkdir()
    f = real / "reg.json"
    f.write_text(json.dumps({"a": {"root": str(w.tmp / "x")}}))
    link = w.tmp / "link.json"
    link.symlink_to(f)
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(link))
    env_side = R.resolve_registry()
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG")
    put_pointer(w, {"corpus_config": str(f)})
    cli = R.resolve_registry()
    assert env_side.source == "env" and cli.source == "user-config"          # differ in source
    assert env_side.path != cli.path                                           # ...and in path
    mcp = _mcp(env_side.sha256, env_side.source, env_side.path)
    r = compare_registries(mcp, cli.statement())
    assert r["mcp_used"] is True and r["mismatch"] is False and r["reason"] is None
    assert os.path.basename(env_side.path) in r["registry_mcp"] and os.path.basename(cli.path) in r["registry_cli"]   # still RECORDED (as a file name)


def test_p1_1_env_to_X_vs_pointer_to_X_is_a_match():
    from factlattice_engine.reviewer import compare_registries
    r = compare_registries(_mcp("a" * 64, "env", "/r/x.json"), {"source": "user-config", "path": "/r/x.json", "sha256": "a" * 64})
    assert r["mismatch"] is False and r["mcp_used"] is True
    assert r["registry_mcp"].startswith("env ") and r["registry_cli"].startswith("user-config ")


def test_p1_1_one_byte_difference_is_a_mismatch(w, monkeypatch):
    from factlattice import registry as R
    from factlattice_engine.reviewer import compare_registries
    a, b = w.tmp / "a.json", w.tmp / "b.json"
    a.write_text(json.dumps({"a": {"root": str(w.tmp / "x")}}))
    b.write_text(json.dumps({"a": {"root": str(w.tmp / "x")}}) + " ")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(a)); ca = R.resolve_registry()
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(b)); cb = R.resolve_registry()
    r = compare_registries(_mcp(ca.sha256, "env", ca.path), cb.statement())
    assert r["mismatch"] is True and "sha256" in r["explanation"] and r["reason"] == "registry-mismatch"


def test_p1_1_b_same_source_and_path_relative_root_different_cwd_is_a_mismatch(w, monkeypatch):
    from factlattice import registry as R
    from factlattice_engine.reviewer import compare_registries
    f = w.tmp / "rel.json"
    f.write_text(json.dumps({"a": {"roots": [{"path": "x"}]}}))
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(f))
    monkeypatch.chdir(w.tmp / "c1"); one = R.resolve_registry()
    monkeypatch.chdir(w.tmp / "c2"); two = R.resolve_registry()
    assert (one.source, one.path) == (two.source, two.path)                     # same source, same path
    r = compare_registries(_mcp(one.sha256, one.source, one.path), two.statement())
    assert r["mismatch"] is True and "sha256" in r["explanation"] and r["reason"] == "registry-mismatch"


def test_p1_1_a_relative_env_path_is_stored_as_its_abspath(w, monkeypatch):
    from factlattice import registry as R
    f = w.tmp / "c1" / "reg.json"
    f.write_text(json.dumps({"a": {"root": str(w.tmp / "x")}}))
    monkeypatch.chdir(w.tmp / "c1")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", "reg.json")
    c = R.resolve_registry()
    assert c.path == str(f) and os.path.isabs(c.path)


def test_p1_1_the_registry_check_command_matches_env_vs_pointer(w, monkeypatch, capsys):
    from factlattice import registry as R
    from factlattice_engine.reviewer.__main__ import main
    f = w.tmp / "reg.json"
    f.write_text(json.dumps({"a": {"root": str(w.tmp / "x")}}))
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(f)); e = R.resolve_registry()
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG"); put_pointer(w, {"corpus_config": str(f)})
    capsys.readouterr()
    rc = main(["registry-check", "--mcp-json", json.dumps(_mcp(e.sha256, "env", e.path))])
    assert rc == 0 and json.loads(capsys.readouterr().out)["mismatch"] is False


# ============================================================================ P1-2: no file content in any error
def _cases(w):
    d = w.tmp / "adir"; d.mkdir()
    reg_json = json.dumps(REGISTRY_WITH_IDS)
    other = w.tmp / "owned_elsewhere.json"
    return [
        ("env->missing", "env", str(w.tmp / "gone-1.json"), None),
        ("env->bad json", "env", "FILE", '{"zz-planted-a": 1, "zz-planted-b": '),
        ("env->non-object", "env", "FILE", json.dumps(["zz-planted-a", "zz-planted-b"])),
        ("env->directory", "env", str(d), None),
        ("pointer->bad json", "pointer", None, '{"corpus_config": "zz-planted-a", "zz-planted-b": '),
        ("pointer->unknown keys", "pointer", None, json.dumps({"corpus_config": "/x", IDS[0]: 1, IDS[1]: 2})),
        ("pointer->relative target", "pointer", None, json.dumps({"corpus_config": "zz-planted-a/zz-planted-b.json"})),
        ("pointer->missing target", "pointer", None, json.dumps({"corpus_config": str(w.tmp / "gone-2.json")})),
        ("pointer->target is a directory", "pointer", None, json.dumps({"corpus_config": str(d)})),
        ("pointer->not an object", "pointer", None, json.dumps([IDS[0], IDS[1]])),
        ("registry placed AT the pointer", "pointer", None, reg_json),
        ("registry of objects placed AT the pointer", "pointer", None,
         json.dumps({IDS[0]: {"roots": [{"path": "/a"}]}, IDS[1]: {"roots": [{"path": "/b"}]}})),
        ("pointer->non-utf8", "pointer-bytes", None, b'{"zz-planted-a": "\xff\xfezz-planted-b"}'),
        ("env->non-utf8", "env-bytes", "FILE", b'\xff\xfe{"zz-planted-a":1,"zz-planted-b":2}'),
    ]


def _arm(w, monkeypatch, kind, target, content):
    if kind == "env":
        if target == "FILE":
            f = w.tmp / "env-reg.json"; f.write_text(content, encoding="utf-8"); target = str(f)
        monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", target)
    elif kind == "env-bytes":
        f = w.tmp / "env-reg.json"; f.write_bytes(content)
        monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(f))
    elif kind == "pointer-bytes":
        w.pointer.parent.mkdir(parents=True, exist_ok=True); w.pointer.write_bytes(content)
    else:
        put_pointer(w, content)


CASE_IDS = [c[0] for c in _cases(type("W", (), {"tmp": Path("/tmp/zz-x")})())] if False else None


@pytest.mark.parametrize("idx", range(14))
def test_p1_2_no_planted_id_in_any_output_surface(w, monkeypatch, capsys, idx):
    from factlattice import registry as R
    from factlattice_engine import server, tools
    from factlattice_engine.__main__ import main as engine_main
    from factlattice_engine.reviewer import compare_registries
    from factlattice_engine.reviewer.__main__ import main as rev_main
    label, kind, target, content = _cases(w)[idx]
    _arm(w, monkeypatch, kind, target, content)
    surfaces = {}
    # 1. the RegistryError text
    with pytest.raises(R.RegistryError) as e:
        R.resolve_registry()
    surfaces["RegistryError"] = str(e.value) + repr(e.value.reason)
    # 2. the call CLI (stdout, stderr, json envelope)
    capsys.readouterr()
    for argv in (["lts_selfcheck", "--json"], ["lts_list_corpora"], ["lts_find", "--args-json", '{"corpus": "zz-planted-a", "text": "q"}']):
        try:
            engine_main(["call", *argv])
        except SystemExit:
            pass
        cap = capsys.readouterr()
        surfaces["call " + argv[0] + " stdout"] = cap.out
        surfaces["call " + argv[0] + " stderr"] = cap.err
    # 3. MCP error mode: every tool's payload
    eng = server.boot_engine(retry_delay_s=0.0, watch=False)
    assert eng.registry_error is not None, label
    for t in server.TOOLS:
        name = t["name"]
        for args in ({}, {"corpus": "zz-planted-a"}, {"corpus": "zz-planted-a", "node_id": "N", "start_node": "N", "text": "q"}):
            res = tools.dispatch(name, dict(args), tools=eng)
            surfaces[f"mcp {name} {sorted(args)}"] = json.dumps(res, default=str)
    # 4. lts_capabilities explicitly, and list_corpora
    surfaces["capabilities"] = json.dumps(tools.dispatch("lts_capabilities", {"corpus": "zz-planted-a"}, tools=eng))
    surfaces["list_corpora"] = json.dumps(tools.dispatch("lts_list_corpora", {}, tools=eng))
    # 5. reviewer helpers and the UNREVIEWED reason text
    for argv in (["resolve-output", "--corpus", "zz-planted-a", "--document", "/x/d.lts"],
                 ["check-registrations", "--corpus", "zz-planted-a"], ["registry-check"]):
        capsys.readouterr()
        rev_main(argv)
        cap = capsys.readouterr()
        surfaces["reviewer " + argv[0]] = cap.out + cap.err
    cmp = compare_registries({"source": "env", "path": "/p", "error": surfaces["list_corpora"]}, {"source": "env", "path": "/p", "sha256": "0"})
    surfaces["reviewer registry-mcp record"] = json.dumps(cmp)
    leaks = {k: [i for i in IDS if i in v] for k, v in surfaces.items() if any(i in v for i in IDS)}
    # the ONLY legitimate echo is an id the CALLER typed (the `corpus` argument of the probes above)
    typed = {k for k in leaks if "zz-planted-a" in leaks[k] and "zz-planted-b" not in leaks[k]
             and ("corpus" in k or "capabilities" in k or "resolve-output" in k or "check-registrations" in k
                  or "call lts_find" in k)}
    real = {k: v for k, v in leaks.items() if k not in typed}
    assert not real, f"{label}: planted ids leaked into {real}"
    for k in typed:                       # a caller-typed id may come back, but only as THEIR id, never from the file
        assert "zz-planted-b" not in surfaces[k], (label, k)


def test_p1_2_negative_control_the_old_unknown_key_text_would_be_caught():
    """The assertion style above must be able to FAIL: the b475061 text echoed the keys."""
    old = "the per-user registry pointer /p has an unknown key (zz-planted-a, zz-planted-b); the only key is 'corpus_config'"
    assert any(i in old for i in IDS)


def test_p1_2_unknown_key_text_counts_keys_and_says_what_it_looks_like(w):
    from factlattice import registry as R
    put_pointer(w, {IDS[0]: {"root": "/a"}, IDS[1]: {"root": "/b"}})
    with pytest.raises(R.RegistryError) as e:
        R.resolve_registry()
    s = str(e.value)
    assert "has 2 unknown key(s); the only key is 'corpus_config'" in s
    assert "this looks like a corpus registry, not a pointer to one" in s
    put_pointer(w, {"corpus_config": "/x", "extra": 1})
    with pytest.raises(R.RegistryError) as e:
        R.resolve_registry()
    assert "has 1 unknown key(s)" in str(e.value) and "looks like a corpus registry" not in str(e.value)


def test_p1_2_error_text_for_bad_json_names_the_class_only(w, monkeypatch):
    from factlattice import registry as R
    f = w.tmp / "b.json"; f.write_text('{"zz-planted-a": 1, ')
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(f))
    with pytest.raises(R.RegistryError) as e:
        R.resolve_registry()
    assert "JSONDecodeError" in str(e.value) and "Expecting" not in str(e.value) and "line 1" not in str(e.value)


# ============================================================================ P2-1: the AST guard
def _plant(tmp_path, body, name="planted.py", sub="factlattice_engine"):
    d = tmp_path / sub; d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body)
    return tmp_path


PLANTED = {
    "join": 'K = "_".join(["FACTLATTICE", "CORPUS", "CONFIG"])\n',
    "fstring": 'P = "FACTLATTICE"\nK = f"{P}_CORPUS_CONFIG"\n',
    "percent": 'K = "FACTLATTICE_%s_CONFIG" % "CORPUS"\n',
    "chr": 'K = "".join(chr(c) for c in [70,65,67,84,76,65,84,84,73,67,69,95,67,79,82,80,85,83,95,67,79,78,70,73,71])\n',
    "chr-map": 'K = "".join(map(chr, [70,65,67,84,76,65,84,84,73,67,69,95,67,79,82,80,85,83,95,67,79,78,70,73,71]))\n',
    "reversed": 'K = "%s"[::-1]\n' % "FACTLATTICE_CORPUS_CONFIG"[::-1],
    "format": 'K = "FACTLATTICE_{}_CONFIG".format("CORPUS")\n',
    "attr": 'import os\nfrom factlattice import registry\nv = os.environ.get(registry.ENV_CONFIG)\n',
    "root-attr": 'from factlattice import registry\nr = registry.ENV_ROOT\n',
    "pointer_path": 'from factlattice import registry\nf = open(registry.pointer_path())\n',
    "read_registry_file": 'from factlattice.registry import read_registry_file\n',
    "pointer-join": 'import os\np = os.path.join(os.path.expanduser("~"), ".config", "factlattice", "config.json")\n',
    "pointer-literal": 'P = "factlattice/" + "config.json"\n',
    "computed-key": 'import os\ndef f(k):\n    return os.environ[k]\n',
    "computed-get": 'import os\nx = os.environ.get(compute())\n',
    "environ-scan": 'import os\nfor k, v in os.environ.items():\n    if k.endswith("_CONFIG"):\n        pass\n',
    "environ-copy": 'import os\nd = dict(os.environ)\n',
    "getenv": 'import os\nx = os.getenv(name_from_somewhere())\n',
}


@pytest.mark.parametrize("form", sorted(PLANTED))
def test_p2_1_guard_catches_each_planted_form(tmp_path, form):
    root = _plant(tmp_path, PLANTED[form])
    got = G.scan_ast_tree(root)
    assert got and all(r[0] == "factlattice_engine/planted.py" for r in got), (form, got)


@pytest.mark.parametrize("sub", ["tools", "scripts", "widgetkit", "."])
def test_p2_1_guard_reaches_every_directory_not_only_the_package_roots(tmp_path, sub):
    root = _plant(tmp_path, PLANTED["fstring"], sub=sub) if sub != "." else _plant(tmp_path, PLANTED["fstring"], sub="")
    assert G.scan_ast_tree(root), sub


def test_p2_1_guard_negative_controls_clean_code_and_literal_env_reads_pass(tmp_path):
    _plant(tmp_path, 'import os\nA = "FACTLATTICE_STATE_DIR"\nx = os.environ.get(A)\ny = os.environ.get("HOME")\n'
                      'z = os.environ["XDG_STATE_HOME"]\nm = "a" + "b"\n')
    assert G.scan_ast_tree(tmp_path) == []
    _plant(tmp_path, PLANTED["join"], name="test_something.py")          # tests are exempt
    (tmp_path / "factlattice").mkdir(); (tmp_path / "factlattice" / "registry.py").write_text(PLANTED["attr"])
    assert G.scan_ast_tree(tmp_path) == []                                 # the resolver is exempt


def test_p2_1_guard_the_real_tree_is_clean_by_ast():
    got = G.scan_ast_tree(REPO)
    assert got == [], "\n".join(f"{p}:{l}: {w}" for p, l, w in got)


# ============================================================================ P2-2 and P2-3: error mode
def _stdio(w, env_extra, requests, cwd):
    env = {**os.environ, "PYTHONPATH": str(REPO), **env_extra}
    env.pop("FACTLATTICE_CORPUS_CONFIG", None)
    p = subprocess.run([sys.executable, "-m", "factlattice_engine.server"],
                       input="\n".join(json.dumps(r) for r in requests) + "\n", capture_output=True, text=True,
                       cwd=str(cwd), env=env, timeout=90)
    return p, {o["id"]: o for o in (json.loads(l) for l in p.stdout.splitlines() if l.strip()) if "id" in o}


def test_p2_2_no_registry_and_an_empty_cwd_boots_in_error_mode_over_real_stdio(w):
    from factlattice_engine.server import TOOLS
    empty = w.tmp / "emptycwd"; empty.mkdir()
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "lts_find", "arguments": {"corpus": "anything", "text": "q"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "lts_list_corpora", "arguments": {}}}]
    p, by = _stdio(w, {k: os.environ[k] for k in ("HOME", "XDG_CONFIG_HOME", "FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR",
                                                  "FACTLATTICE_BACKUP_DIR", "FACTLATTICE_QUERY_LOG", "FACTLATTICE_CODE_WATCH")},
                    reqs, empty)
    assert p.returncode == 0 and "RuntimeError" not in p.stderr, p.stderr[-600:]
    assert [t["name"] for t in by[2]["result"]["tools"]] == [t["name"] for t in TOOLS]
    body = json.loads(by[3]["result"]["content"][0]["text"])
    assert body["reason"] == "registry-not-found" and by[3]["result"]["isError"] is True
    want = ("no corpus registry is configured (set FACTLATTICE_CORPUS_CONFIG or create ~/.config/factlattice/config.json) "
            "and the built-in example found no files under " + str(empty))
    assert want in body["error"], body["error"]
    lc = json.loads(by[4]["result"]["content"][0]["text"])
    assert set(lc["registry_choice"]) == {"source", "path", "error", "error_persist"} and lc["registry_choice"]["source"] == "example"


def test_p2_3_error_mode_gates_every_exempt_tool_that_names_a_corpus(w, monkeypatch):
    from factlattice_engine import server, tools
    f = w.tmp / "torn.json"; f.write_text("{ torn")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(f))
    eng = server.boot_engine(retry_delay_s=0.0, watch=False)
    for name in tools.REGISTRY_ERROR_EXEMPT_TOOLS:
        if name == "lts_list_corpora":
            continue
        res = tools.dispatch(name, {"corpus": "zz-planted-a", "source": str(w.tmp / "x"), "content": "# T [T-1]\n"}, tools=eng)
        assert res["success"] is False and res.get("reason") == "registry-not-found", (name, res)
        assert "ValueError" not in json.dumps(res)
    # no corpus named: the parser-only tools answer. (`lts_lint` over a directory resolves its profile THROUGH the registry,
    # so with a broken registry it refuses with the named reason rather than judging against an inert profile.)
    assert tools.dispatch("lts_selfcheck", {}, tools=eng)["success"] is True
    lint = tools.dispatch("lts_lint", {"source": str(w.tmp / "x")}, tools=eng)
    assert lint["success"] is False and lint.get("reason") == "registry-not-found"


# ============================================================================ P3
def test_p3_callcli_docstring_no_longer_states_the_retracted_hash():
    src = (REPO / "factlattice_engine" / "callcli.py").read_text()
    assert "registry bytes + resolved root" not in src


def test_p3_profile_dir_joins_the_hash_when_set(w, monkeypatch):
    from factlattice import registry as R
    f = w.tmp / "abs.json"; f.write_text(json.dumps({"a": {"root": str(w.tmp / "x")}}))
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(f))
    monkeypatch.delenv("FACTLATTICE_PROFILE_DIR", raising=False)
    base = R.resolve_registry().sha256
    monkeypatch.setenv("FACTLATTICE_PROFILE_DIR", str(w.tmp / "c1"))
    a = R.resolve_registry().sha256
    monkeypatch.setenv("FACTLATTICE_PROFILE_DIR", str(w.tmp / "c2"))
    b = R.resolve_registry().sha256
    assert len({base, a, b}) == 3
    monkeypatch.setenv("FACTLATTICE_PROFILE_DIR", str(w.tmp / "c1"))
    assert R.resolve_registry().sha256 == a


def test_u1b_deferral_is_stated_and_no_atomic_writer_promise_remains():
    reg = (REPO / "factlattice" / "registry.py").read_text()
    sdlc = (REPO / "SDLC.md").read_text()
    assert "no registry or pointer writer exists in U1a; atomic-write is a U1b (init) requirement" in reg
    assert "no registry or pointer writer exists in U1a; atomic-write is a U1b (init) requirement" in sdlc
    assert "by temp-file-and-rename" not in reg
