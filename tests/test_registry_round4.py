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


# ---- round 8 (the architects' ruling of 2026-11-06): every malformed entry SHAPE is a registry error, however late a loader would meet it ----------
SHAPES = {
    "roots as a string": {"roots": "nope"},
    "roots as an int": {"roots": 5},
    "a root that is an int": {"roots": [7]},
    "a root that is a list": {"roots": [["x"]]},
    "a root object with an int path": {"roots": [{"path": 7}]},
    "string roots plus a typo'd privacy key": {"roots": "nope", "privat": True},
    "int path plus a typo'd privacy key": {"roots": [{"path": 7}], "privat": True},
    "private as a string": {"root": "/x", "private": "yes"},
    "private as an int": {"root": "/x", "private": 1},
    "profile as an int": {"root": "/x", "profile": 5},
    "external_id_prefixes as an int": {"root": "/x", "external_id_prefixes": 5},
    "external_id_prefixes with an int in it": {"root": "/x", "external_id_prefixes": ["A-", 3]},
    "the entry is a string": "/x",
    "the entry is a list": ["/x"],
    "the entry is null": None,
    "the entry is an int": 5,
    "a root object with an int area": {"roots": [{"path": "/x", "area": 3}]},
    "root as an int": {"root": 5},
    "domain_suffix as an int": {"root": "/x", "domain_suffix": 5},
}


# Round 12 (P2-3): the expectation for each shape is the RELEASE'S REAL SERVER, read from the pinned table (tests/boot_verdicts/af4cfc2.json, recorded by
# booting a real stdio server from a git archive of af4cfc2), never a guess: `registry-not-found` where the release server gives it, and otherwise the release's
# own listing (the entry's corpus in the same table) and NO `corpus-load-error` anywhere, so a load error cannot hide behind "the healthy corpus is served".
TABLE = {
    "roots as a string": "roots string", "roots as an int": "roots int", "a root that is an int": "root elem int", "a root that is a list": "root elem list",
    "a root object with an int path": "path int", "string roots plus a typo'd privacy key": "string roots + privat", "int path plus a typo'd privacy key": "int path + privat",
    "private as a string": "private yes", "private as an int": "private 1", "profile as an int": "profile int", "external_id_prefixes as an int": "prefixes int",
    "external_id_prefixes with an int in it": "prefixes list int", "the entry is a string": "entry string", "the entry is a list": "entry list",
    "the entry is null": "entry null", "the entry is an int": "entry int", "a root object with an int area": "area int", "root as an int": "root int",
    "domain_suffix as an int": "domain_suffix int",
}
# the shapes the registry cannot place a location for (the release does not serve them and their roots cannot be read as paths): every write lands flagged
FLAGGED = {"roots as a string", "roots as an int", "a root that is an int", "a root that is a list", "a root object with an int path",
           "string roots plus a typo'd privacy key", "int path plus a typo'd privacy key", "the entry is a string", "the entry is a list", "the entry is null",
           "the entry is an int", "root as an int"}


def test_every_shape_here_is_in_the_pinned_server_table():
    pinned = json.loads((REPO / "tests" / "boot_verdicts" / "af4cfc2.json").read_text())
    assert set(SHAPES) == set(TABLE) and all(t in pinned for t in TABLE.values())


@pytest.mark.parametrize("label", sorted(SHAPES))
def test_every_malformed_entry_shape_boots_as_the_release_server_does_and_a_write_is_flagged_only_when_the_registry_cannot_place_it(w, label):
    """Each shape beside a healthy corpus, over a real stdio server. The boot answers what release af4cfc2's server answered (the pinned table): registry-not-found
    for a find and a listing that carries the registry error and no corpus where the release does; otherwise the healthy corpus is served, the listing is the
    release's (ids and failed tables) and there is NO `corpus-load-error`. A write lands flagged (`registry_error` on the receipt, `corpora_error` on the
    ledger record, `corpora` null) exactly when the registry cannot state where the entry lives, and is attributed normally otherwise."""
    pinned = json.loads((REPO / "tests" / "boot_verdicts" / "af4cfc2.json").read_text())[TABLE[label]]
    good = w.tmp / "corp" / "a.lts"
    other = w.tmp / "elsewhere"
    other.mkdir()
    (other / "b.lts").write_text("# D [E-1]\n\n## N [E-2]\n\n- status: active\n", encoding="utf-8")
    badd = w.tmp / "badd"                       # the table's entries point at a REAL folder, so the release serves the ones it serves
    badd.mkdir()
    (badd / "x.lts").write_text("# D [X-1]\n\n## N [X-2]\n\n- status: active\n", encoding="utf-8")
    entry = json.loads(json.dumps(SHAPES[label]).replace('"/x"', json.dumps(str(badd))))
    reg = {IDS[0]: {"root": str(w.tmp / "corp"), "domain_suffix": "proj.a"}, IDS[1]: entry}
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "lts_find", "arguments": {"corpus": IDS[0], "prefix": "N"}}},
            # A call NAMING the entry's corpus waits for its first read (spec 38 x-server), so the listing below is taken after corpus b is settled. Without it the
            # listing could land in the boot window while b was still PENDING, and a pending PRIVATE corpus ("private" as a string reads as private) is counted, never named
            # (spec 38 x-list): tests/test_registry_round4_boot_window.py forces that ordering.
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "lts_find", "arguments": {"corpus": IDS[1], "prefix": "N"}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "lts_list_corpora", "arguments": {"heal": False}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "lts_add_property", "arguments": {
                "path": str(good), "node_id": "N-1", "key": "k", "value": "v"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "lts_add_property", "arguments": {
                "path": str(other / "b.lts"), "node_id": "E-2", "key": "k", "value": "v"}}}]
    p, by = stdio(w, json.dumps(reg).encode(), reqs)
    assert p.returncode == 0, (label, p.stderr[-400:])
    find = json.loads(by[3]["result"]["content"][0]["text"])
    lst = json.loads(by[6]["result"]["content"][0]["text"])
    assert "corpus-load-error" not in json.dumps(lst) and "corpus-load-error" not in json.dumps(find), (label, "a load error hid here")
    if pinned["find_okc"] == "registry-not-found":
        assert find["reason"] == "registry-not-found", (label, find)
        assert lst["corpora"] == [] and (lst["registry_choice"] or {}).get("error"), (label, "registry-error mode lists no corpus")
    else:
        assert find.get("success") is True, (label, find)
        want = sorted(i.replace("okc", IDS[0]).replace("badc", IDS[1]) for i in pinned["list"]["ids"])
        assert sorted(c["corpus_id"] for c in lst["corpora"]) == want, (label, lst["corpora"])
        failed = {c["corpus_id"]: c["code"] for c in lst.get("failed_corpora", [])}
        assert failed == {IDS[1]: codes[0][1] for codes in [pinned["list"]["tables"].get("failed_corpora", [])] if codes}, (label, failed)
    for rid in (4, 5):
        out = json.loads(by[rid]["result"]["content"][0]["text"])
        rm = out["root_membership"]
        assert out["written"] is True, (label, rid, out)
        if label in FLAGGED:
            assert rm["corpora"] == [] and rm["inside_corpus"] is None and rm.get("registry_error"), (label, rid, rm)
            assert IDS[0] not in json.dumps(rm) and IDS[1] not in json.dumps(rm), "no corpus is named"
        elif rid == 4:
            assert rm["corpora"] == [IDS[0]] and not rm.get("registry_error"), (label, rm)
        else:
            assert not rm.get("registry_error") and "OUTSIDE EVERY REGISTERED" in json.dumps(out), (label, rm)
    recs = [json.loads(x) for f in sorted((w.tmp / "st" / "ledger").glob("*.jsonl")) for x in f.read_text().splitlines() if x.strip()]
    assert len(recs) == 2, (label, recs)
    if label in FLAGGED:
        assert all(r.get("corpora") is None and r.get("corpora_error") for r in recs), (label, recs)
    else:
        assert not any(r.get("corpora_error") for r in recs), (label, recs)


# ---- round 9 -------------------------------------------------------------------------------------------------------------------------------------------
def _ledger_recs(w):
    d = w.tmp / "st" / "ledger"
    return [json.loads(x) for f in sorted(d.glob("*.jsonl")) for x in f.read_text().splitlines() if x.strip()] if d.is_dir() else []


def test_a_typod_root_key_fails_only_its_corpus_and_a_write_under_it_lands_flagged_attributed_to_none(w):
    """F2 (second symptom). An entry whose root key is typo'd (`pth`) fails ONLY its corpus (the unknown-key rule, ruled 2026-10-29): the server serves the
    healthy one. A write whose file lies under the typo'd entry's folder cannot be attributed and is not 'outside every root': it lands flagged
    (`registry_error`, ledger `corpora_error`, `corpora` null). A write under the healthy corpus is NOT touched: it attaches to it as before. Never
    "WRITTEN, BUT NOT LEDGERED". Red on 2b346c6 (the write read 'THIS FILE IS OUTSIDE EVERY REGISTERED CORPUS ROOT', unflagged)."""
    bad_dir = w.tmp / "bad"
    bad_dir.mkdir()
    (bad_dir / "b.lts").write_text("# D [B-1]\n\n## N [B-2]\n\n- status: active\n", encoding="utf-8")
    reg = {IDS[0]: {"root": str(w.tmp / "corp"), "domain_suffix": "proj.a"}, IDS[1]: {"roots": [{"pth": str(bad_dir)}], "domain_suffix": "proj.b"}}
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "lts_find", "arguments": {"corpus": IDS[0], "prefix": "N"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "lts_add_property", "arguments": {
                "path": str(bad_dir / "b.lts"), "node_id": "B-2", "key": "k", "value": "v"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "lts_add_property", "arguments": {
                "path": str(w.tmp / "corp" / "a.lts"), "node_id": "N-1", "key": "k", "value": "v"}}}]
    p, by = stdio(w, json.dumps(reg).encode(), reqs)
    assert p.returncode == 0, p.stderr[-400:]
    assert json.loads(by[3]["result"]["content"][0]["text"]).get("reason") != "registry-not-found", "the server serves the healthy corpus"
    bad = json.loads(by[4]["result"]["content"][0]["text"])
    assert bad["written"] is True and bad["root_membership"]["corpora"] == [] and bad["root_membership"].get("registry_error"), bad.get("root_membership")
    assert "NOT LEDGERED" not in json.dumps(bad) and "OUTSIDE EVERY REGISTERED" not in json.dumps(bad)
    ok = json.loads(by[5]["result"]["content"][0]["text"])
    assert ok["written"] is True and ok["root_membership"]["corpora"] == [IDS[0]] and not ok["root_membership"].get("registry_error"), ok.get("root_membership")
    recs = {r["path_real"].rsplit("/", 1)[-1]: r for r in _ledger_recs(w)}
    assert recs["b.lts"]["corpora"] is None and recs["b.lts"]["corpora_error"] and recs["a.lts"]["corpora"] == [IDS[0]]


def _launch_with_failing_look(w, registry_bytes, requests):
    """A REAL server (it has a kernel) whose membership look raises for any store: the launcher patches `membership._look` and runs the server's `__main__`."""
    f = w.tmp / "reg.json"
    f.write_bytes(registry_bytes)
    launcher = ("import runpy, factlattice_engine.membership as m\n"
                "real = m._look\n"
                "def bad(store):\n"
                "    if store is None:\n"
                "        return real(store)\n"
                "    raise RuntimeError('SECRET-EXCEPTION-TEXT')\n"
                "m._look = bad\n"
                "runpy.run_module('factlattice_engine.server', run_name='__main__')\n")
    env = {**os.environ, "PYTHONPATH": str(REPO), "FACTLATTICE_CORPUS_CONFIG": str(f)}
    p = subprocess.run([sys.executable, "-c", launcher], input="\n".join(json.dumps(r) for r in requests) + "\n", capture_output=True, text=True,
                       cwd=str(w.tmp), env=env, timeout=120)
    return p, {o["id"]: o for o in (json.loads(l) for l in p.stdout.splitlines() if l.strip()) if "id" in o}


@pytest.mark.parametrize("tool", ["lts_add_property", "lts_remove_property"])
def test_a_failed_store_look_ends_as_the_coded_refusal_on_a_server_with_a_kernel(w, tool):
    """F1 (P1). On a server (a store with a kernel) `await_pending` re-raised the failed look before `check` could see it, so the write ended as a raw
    dispatch error carrying the exception text. A failed look anywhere in a write's membership evaluation ends as `membership-unconfirmed`, with no
    exception text in the reply, and nothing is written. Over a real stdio server. Red on 2b346c6 (`RuntimeError: SECRET-EXCEPTION-TEXT`)."""
    good = w.tmp / "corp" / "a.lts"
    before = good.read_bytes()
    args = {"path": str(good), "node_id": "N-1", "key": "status"} if tool == "lts_remove_property" else {"path": str(good), "node_id": "N-1", "key": "k", "value": "v"}
    reg = {IDS[0]: {"root": str(w.tmp / "corp"), "domain_suffix": "proj.a"}}
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "lts_find", "arguments": {"corpus": IDS[0], "prefix": "N"}}},     # waits for the boot read
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}}]
    p, by = _launch_with_failing_look(w, json.dumps(reg).encode(), reqs)
    assert p.returncode == 0, p.stderr[-400:]
    text = by[2]["result"]["content"][0]["text"]
    out = json.loads(text)
    assert out.get("refused") is True and out.get("reason") == "membership-unconfirmed" and out.get("written") is False, out
    assert "SECRET-EXCEPTION-TEXT" not in text and "RuntimeError" not in text, "no exception text in the reply"
    assert "SECRET-EXCEPTION-TEXT" not in p.stderr, "and none was raised as a dispatch error"
    assert good.read_bytes() == before


def test_the_same_failed_look_without_a_kernel_ends_the_same_way(w, monkeypatch):
    """F1, the other half: the library path (no kernel). The same refusal for the same tools, the same absence of exception text."""
    from types import SimpleNamespace

    from factlattice_engine import authoring
    good = w.tmp / "corp" / "a.lts"
    before = good.read_bytes()
    store = SimpleNamespace(snapshot_all=lambda: (_ for _ in ()).throw(RuntimeError("SECRET-EXCEPTION-TEXT")))
    for fn, kw in ((authoring.add_property, dict(key="k", value="v")), (authoring.remove_property, dict(key="status"))):
        out = fn(str(good), "N-1", store=store, **kw)
        assert out.get("refused") is True and out.get("reason") == "membership-unconfirmed" and "SECRET-EXCEPTION-TEXT" not in json.dumps(out), out
    assert good.read_bytes() == before
