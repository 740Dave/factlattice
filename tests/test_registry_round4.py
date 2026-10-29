"""U1a round 4 (delta Diamond at 5fea5c4, PASS_WITH_NOTES): P2-3 every exception while interpreting a registry is
registry-error mode; P2-4 rows/SPEC; P3-1..3. Rulings: the echoed pointer VALUE is allowed (the registry path the user
typed); guard completeness (P2-2) is a follow-up and is only DOCUMENTED here."""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _registry_guard as G

REPO = Path(__file__).resolve().parent.parent
IDS = ("zz-planted-a", "zz-planted-b")


@pytest.fixture()
def w(tmp_path, monkeypatch):
    st = tmp_path / "st"
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(st))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    (tmp_path / "corp").mkdir()
    (tmp_path / "corp" / "a.lts").write_text("# D [D-1]\n\n## N [N-1]\n\n- status: active\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    p = type("W", (), {})()
    p.tmp = tmp_path
    return p


def valid(w):
    return {IDS[0]: {"roots": [{"path": str(w.tmp / "corp"), "area": "specs"}], "domain_suffix": "proj.a"},
            IDS[1]: {"root": str(w.tmp / "corp"), "domain_suffix": "proj.b"}}


def stdio(w, registry_bytes, requests):
    f = w.tmp / "reg.json"
    f.write_bytes(registry_bytes)
    env = {**os.environ, "PYTHONPATH": str(REPO), "FACTLATTICE_CORPUS_CONFIG": str(f)}
    p = subprocess.run([sys.executable, "-m", "factlattice_engine.server"],
                       input="\n".join(json.dumps(r) for r in requests) + "\n", capture_output=True, text=True,
                       cwd=str(w.tmp), env=env, timeout=120)
    return p, {o["id"]: o for o in (json.loads(l) for l in p.stdout.splitlines() if l.strip()) if "id" in o}


REQS = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "lts_find", "arguments": {"corpus": IDS[0], "prefix": "N"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "lts_list_corpora", "arguments": {}}}]


def classes(w):
    deep = ("[" * 200000).encode()
    return {
        "wrong-shape entries": json.dumps({IDS[0]: "a string", IDS[1]: None}).encode(),
        "roots as a string": json.dumps({IDS[0]: {"roots": "nope"}}).encode(),
        "int where a path belongs": json.dumps({IDS[0]: {"roots": [{"path": 7}]}}).encode(),
        "NaN and duplicate keys": ('{"%s": NaN, "%s": {"roots": 5}}' % (IDS[0], IDS[0])).encode(),
        "deep nesting": deep,
        "well-formed, roots do not exist": json.dumps(
            {IDS[0]: {"roots": [{"path": "/nonexistent/zz-a"}]}, IDS[1]: {"root": "/nonexistent/zz-b"}}).encode(),
    }


@pytest.mark.parametrize("label", ["wrong-shape entries", "roots as a string", "int where a path belongs",
                                   "NaN and duplicate keys", "deep nesting", "well-formed, roots do not exist"])
def test_p2_3_each_class_boots_in_error_mode_over_real_stdio(w, label):
    from factlattice_engine.server import TOOLS
    p, by = stdio(w, classes(w)[label], REQS)
    assert p.returncode == 0, (label, p.stderr[-500:])
    assert [t["name"] for t in by[2]["result"]["tools"]] == [t["name"] for t in TOOLS], label
    body = json.loads(by[3]["result"]["content"][0]["text"])
    assert by[3]["result"]["isError"] is True and body["reason"] == "registry-not-found", (label, body)
    assert not body.get("nodes")
    lc = json.loads(by[4]["result"]["content"][0]["text"])
    assert set(lc["registry_choice"]) == {"source", "path", "error", "error_persist"} and lc["count"] == 0
    for surf in (body["error"], lc["registry_choice"]["error"], p.stderr):
        assert not any(i in surf for i in IDS), (label, surf[:300])
    assert "restart" in body["error"].lower()


def test_p2_3_the_reason_text_names_class_and_registry_path_only(w):
    p, by = stdio(w, classes(w)["wrong-shape entries"], REQS)
    err = json.loads(by[3]["result"]["content"][0]["text"])["error"]
    assert str(w.tmp / "reg.json") in err and "could not be loaded" in err
    assert "TypeError" in err or "AttributeError" in err or "ValueError" in err
    assert "a string" not in err


def test_p2_3_zero_loadable_corpora_says_so(w):
    p, by = stdio(w, classes(w)["well-formed, roots do not exist"], REQS)
    err = json.loads(by[3]["result"]["content"][0]["text"])["error"]
    assert "none of its corpora could be loaded" in err and str(w.tmp / "reg.json") in err


def test_p2_3_some_corpora_loading_is_unchanged_missing_roots_are_skipped_silently(w, monkeypatch):
    from factlattice_engine import server
    reg = valid(w)
    reg["zz-planted-missing"] = {"root": "/nonexistent/zz"}
    f = w.tmp / "reg.json"; f.write_text(json.dumps(reg))
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(f))
    eng = server.boot_engine(retry_delay_s=0.0, watch=False)
    assert eng.registry_error is None and sorted(eng.store.corpus_ids()) == sorted(IDS)


def _mutations(w, n=100, seed=20261008):
    rng = random.Random(seed)
    base = valid(w)
    junk = [None, 7, 3.5, "x", [], {}, [1, 2], {"k": "v"}, True, [[]], {"roots": 5}, float("nan")]
    out = []
    for i in range(n):
        d = json.loads(json.dumps(base))
        kind = rng.choice(["swap-entry", "swap-field", "drop-field", "extra-id", "truncate", "dup", "nest", "bytes"])
        cid = rng.choice(IDS)
        if kind == "swap-entry":
            d[cid] = rng.choice(junk)
        elif kind == "swap-field":
            e = d[cid] if isinstance(d[cid], dict) else {}
            e[rng.choice(["roots", "root", "domain_suffix", "profile", "private", "area"])] = rng.choice(junk)
            d[cid] = e
        elif kind == "drop-field":
            d[cid].pop(rng.choice(list(d[cid])), None)
        elif kind == "extra-id":
            d["zz-planted-extra"] = rng.choice(junk)
        text = json.dumps(d)
        raw = text.encode()
        if kind == "truncate":
            raw = raw[: rng.randrange(1, len(raw))]
        elif kind == "dup":
            raw = raw.replace(b'"zz-planted-a"', b'"zz-planted-a": NaN, "zz-planted-a"', 1)
        elif kind == "nest":
            raw = (b"[" * rng.choice([50, 5000, 150000])) + raw
        elif kind == "bytes":
            pos = rng.randrange(len(raw)); raw = raw[:pos] + bytes([rng.randrange(128, 256)]) + raw[pos + 1:]
        out.append((i, kind, raw))
    return out


def test_p2_3_fuzz_100_seeded_mutations_never_crash_boot_and_never_leak_an_id(w, monkeypatch):
    from factlattice_engine import server, tools
    f = w.tmp / "fuzz.json"
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(f))
    errors = 0
    for i, kind, raw in _mutations(w):
        f.write_bytes(raw)
        try:
            eng = server.boot_engine(retry_delay_s=0.0, watch=False)
        except BaseException as exc:                      # noqa: BLE001 -- the property: NOTHING escapes boot
            pytest.fail(f"mutation {i} ({kind}) crashed boot: {type(exc).__name__}")
        payloads = [json.dumps(tools.dispatch("lts_list_corpora", {}, tools=eng), default=str),
                    json.dumps(tools.dispatch("lts_find", {"corpus": IDS[0], "prefix": "N"}, tools=eng), default=str)]
        if eng.registry_error is not None:
            errors += 1
            for surf in payloads + [str(eng.registry_error)]:
                assert not any(i_ in surf for i_ in IDS), (i, kind, surf[:300])
                assert "zz-planted-extra" not in surf
    assert errors >= 30, f"the fuzz must really exercise error mode (only {errors} of 100 did)"


# ---------------------------------------------------------------------------------------------- P2-4 rows and SPEC
def test_p2_4_sdlc_rows_for_the_round_3_extension_points():
    sdlc = (REPO / "SDLC.md").read_text(encoding="utf-8")
    for row, words in (("REGISTRY-MISMATCH-IS-THE-HASH", ("sha256", "source", "path", "recorded")),
                       ("GUARD-ALLOW-LIST-AND-DISPLAY-HELPERS", ("ALLOWED_DYNAMIC", "env_var_name", "pointer_display", "entries_from")),
                       ("REGISTRY-HASH-INPUTS", ("FACTLATTICE_PROFILE_DIR", "cwd", "HOME")),
                       ("REGISTRY-ERROR-MODE-COVERS-INTERPRETATION", ("shape", "zero", "class"))):
        line = next((l for l in sdlc.splitlines() if l.startswith(f"- [ ] [{row}]")), None)
        assert line, row
        for wd in words:
            assert wd.lower() in line.lower(), (row, wd)


def test_p2_4_spec_names_profile_dir_in_the_hash_and_the_privacy_line():
    spec = (REPO / "SPEC.md").read_text(encoding="utf-8")
    assert "FACTLATTICE_PROFILE_DIR" in spec
    assert "rror text may name the env var, the pointer path and the registry path the user configured, never file CONTENT" in spec


# ---------------------------------------------------------------------------------------------------- P3
def test_p3_1_the_tools_comment_states_the_measured_behaviour_and_try_says_call_exits_2():
    src = (REPO / "factlattice_engine" / "tools.py").read_text(encoding="utf-8")
    assert "Without it, it works on its path and answers" not in src and "Without a `corpus` it works on its path and answers" not in src
    assert "only `lts_selfcheck` and `lts_list_corpora` answer" in src
    try_doc = (REPO / "docs" / "TRY-BEFORE-YOU-DEPLOY.md").read_text(encoding="utf-8")
    assert "With a broken registry, `call` exits 2" in try_doc


def test_p3_1_measured_in_error_mode_only_selfcheck_and_list_corpora_answer(w, monkeypatch):
    from factlattice_engine import server, tools
    f = w.tmp / "torn.json"; f.write_text("{ torn")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(f))
    eng = server.boot_engine(retry_delay_s=0.0, watch=False)
    ans = {n: tools.dispatch(n, a, tools=eng).get("success") for n, a in
           (("lts_selfcheck", {}), ("lts_list_corpora", {}), ("lts_lint", {"source": str(w.tmp / "corp")}),
            ("lts_preview", {"content": "# T [T-1]\n"}))}
    assert ans == {"lts_selfcheck": True, "lts_list_corpora": True, "lts_lint": False, "lts_preview": False}


def test_p3_2_bom_is_refused_and_documented(w, monkeypatch):
    from factlattice import registry as R
    f = w.tmp / "bom.json"; f.write_bytes(b"\xef\xbb\xbf" + json.dumps(valid(w)).encode())
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(f))
    with pytest.raises(R.RegistryError):
        R.resolve_registry()
    assert "BOM" in (REPO / "docs" / "TRY-BEFORE-YOU-DEPLOY.md").read_text(encoding="utf-8")
    assert "BOM" in (REPO / "SPEC.md").read_text(encoding="utf-8")


def test_p3_3_the_allow_list_exempts_only_the_generic_env_read_rule(tmp_path):
    d = tmp_path / "factlattice_engine" / "semantic"; d.mkdir(parents=True)
    (d / "embedd.py").write_text('import os\ndef _env_interval(name, default):\n    raw = os.environ.get(name)\n'
                                 '    return os.environ.get("FACTLATTICE_CORPUS_CONFIG")\n')
    got = G.scan_ast_tree(tmp_path)
    assert any("FACTLATTICE_CORPUS_CONFIG" in w_ for _, _, w_ in got), got            # the planted literal is caught
    assert not any("not a plain string" in w_ for _, _, w_ in got)                      # the generic read stays exempt


def test_p2_2_the_guards_words_only_list_names_the_forms_the_diamond_showed_missed():
    src = (REPO / "tests" / "_registry_guard.py").read_text(encoding="utf-8")
    for form in ("os.environ aliases", "AugAssign", "{**os.environ}", "os.environb", "joinpath",
                 "pointer_display", "POINTER_DISPLAY", "skipped directories"):
        assert form in src, form


# ================================================== addenda: PERSISTED surfaces carry basenames, never directories
PRIVATE_DIR = "zz-private-folder"


@pytest.fixture()
def priv(w, monkeypatch):
    d = w.tmp / PRIVATE_DIR
    d.mkdir()
    f = d / "reg.json"
    f.write_text(json.dumps(valid(w)))
    p = type("P", (), {})()
    p.dir, p.file = d, f
    p.w = w
    return p


def test_addendum_registry_value_is_source_basename_hash(priv, monkeypatch):
    from factlattice import registry as R
    from factlattice_engine.reviewer import registry_value
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(priv.file))
    c = R.resolve_registry()
    v = registry_value(c.source, c.path, c.sha256)
    assert v == f"env reg.json sha256:{c.sha256}" and PRIVATE_DIR not in v


def test_addendum_review_and_unreviewed_documents_contain_reg_json_and_never_the_directory(priv, monkeypatch):
    from factlattice import registry as R
    from factlattice_engine import tools
    from factlattice_engine.reviewer import compare_registries
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(priv.file))
    c = R.resolve_registry()
    mcp = {"source": c.source, "path": c.path, "sha256_at_boot": c.sha256, "sha256_now": c.sha256, "resolved_at": "t"}
    ok = compare_registries(mcp, c.statement())
    bad = compare_registries({**mcp, "sha256_now": "b" * 64, "sha256_at_boot": "b" * 64}, c.statement())
    err = compare_registries({"source": "env", "path": c.path,
                              "error": f"registry-not-found: the registry '{c.path}' could not be loaded (TypeError)"}, c.statement())
    out = priv.w.tmp / "reviews"; out.mkdir()
    for kind, rec, reason in (("REVIEW", ok, None), ("UNREVIEWED", bad, bad["reason"]), ("UNREVIEWED", err, err["registry_mcp"])):
        f = out / f"{kind}-{len(list(out.iterdir()))}.lts"
        props = {"registry-mcp": rec["registry_mcp"], "registry-cli": rec["registry_cli"]}
        if reason:
            props["stop-reason"] = reason
        r = tools.dispatch("lts_add_node", {"path": str(f), "node_id": f"{kind}-X", "title": "t", "properties": props,
                                           "create": True, "create_parent": True, "doctype": "review"}, tools=None)
        assert r.get("success") is True or r.get("written") is True, r
        text = f.read_text()
        assert PRIVATE_DIR not in text and "reg.json" in text or kind == "UNREVIEWED" and err["registry_mcp"] in text, text
        assert PRIVATE_DIR not in text
    assert "reg.json" in ok["registry_mcp"] and "reg.json" in ok["registry_cli"] and bad["reason"] == "registry-mismatch" and "reg.json" in bad["registry_mcp"]
    assert PRIVATE_DIR not in json.dumps([ok, bad, err])


def test_addendum_decisions_are_unchanged_by_the_reduction(priv, monkeypatch):
    from factlattice_engine.reviewer import compare_registries
    cli = {"source": "env", "path": str(priv.file), "sha256": "a" * 64}
    m = {"source": "user-config", "path": "/elsewhere/reg.json", "sha256_at_boot": "a" * 64, "sha256_now": "a" * 64, "resolved_at": "t"}
    assert compare_registries(m, cli)["mismatch"] is False
    assert compare_registries({**m, "sha256_now": "c" * 64, "sha256_at_boot": "c" * 64}, cli)["mismatch"] is True
    st = compare_registries({**m, "sha256_at_boot": "d" * 64}, cli)
    assert st["stale"] is True and st["mismatch"] is False and st["mcp_used"] is False


def test_addendum_the_query_log_line_never_holds_the_directory(priv, monkeypatch):
    from factlattice_engine import server, tools
    log = priv.w.tmp / "ql2.jsonl"
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(log))
    priv.file.write_text("{ torn")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(priv.file))
    eng = server.boot_engine(retry_delay_s=0.0, watch=False)
    res = tools.dispatch("lts_find", {"corpus": IDS[0], "prefix": "N"}, tools=eng)
    assert PRIVATE_DIR in res["error"]                                    # the live reply keeps the FULL path (ruled)
    text = log.read_text()
    assert text.strip(), "no query-log line was written"
    assert PRIVATE_DIR not in text and "reg.json" in text


def test_addendum_spec_lists_the_surfaces_and_sdlc_has_the_row():
    spec = (REPO / "SPEC.md").read_text(encoding="utf-8")
    for w_ in ("FULL paths: terminal output", "STRUCTURE only: the query log",
               "CONTENT never, on any surface"):
        assert w_ in spec, w_
    sdlc = (REPO / "SDLC.md").read_text(encoding="utf-8")
    line = next((l for l in sdlc.splitlines() if l.startswith("- [ ] [REGISTRY-PATH-SURFACES]")), None)
    assert line and all(x in line for x in ("basename", "query log", "terminal", "CONTENT"))


def test_addendum_the_procedure_describes_the_recorded_form():
    core = (REPO / "factlattice_engine/reviewer/shipped/reviewer-core.lts").read_text(encoding="utf-8")
    assert "<source> <file name> sha256:<hex>" in core
