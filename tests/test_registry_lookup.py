"""U1a: ONE registry lookup for every route, with NO silent fallback (SPEC-U1a v3 + the v3.1 amendment).

THE DEFECT (measured at 6099483): `config.default_corpora()` silently returned the built-in EXAMPLE registry
when the registry variable was unset AND when it was set to a file that did not exist; four other readers
(`profile.load_corpus_config`, `registered_profile_for_root`, `callcli.corpus_config_info`, `config.CORPUS_ROOT`)
read the environment themselves. In an ordinary agent shell every CLI route therefore read a different
registry from the MCP server, and a clean-looking `check-registrations` could come from the WRONG registry.

THE PROPERTIES (the architect's P1-P3, quoted in the spec):
  P1  the helpers and `call` use the SAME registry the MCP server uses, or stop with a named reason; never
      a silent example answer for a corpus that is not in the example registry (`registry-not-found`).
  P2  a review records which registry file each route used, with its hash; a mismatch stops the run.

⛔ IMPORTS OF THE NEW CODE ARE LAZY (inside each test) so that before the feature exists each test fails
FOR ITS OWN MISSING BEHAVIOUR, one by one, not as one collection error. Tests that exercise today's public
entry points (`call`, `default_corpora`, `check-registrations`) fail on BEHAVIOUR, not on an import.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from tests import _registry_guard as G

REPO = Path(__file__).resolve().parent.parent
EXAMPLE_IDS = {"alpha", "beta", "gamma"}
NOT_FOUND_TEXT = ("no corpus registry is configured (set FACTLATTICE_CORPUS_CONFIG or create "
                  "~/.config/factlattice/config.json); the built-in example registry is in use and has no corpus '{id}'")

DOC = "# Doc [P-DOC]\n\n## Pub note [PUB-1]\n\n- status: active\n\nhello world body\n"


# ------------------------------------------------------------------------------------------- the world
@pytest.fixture()
def world(tmp_path, monkeypatch):
    """A tiny real corpus, a registry for it, a pointer location, and every state dir sandboxed."""
    corpus = tmp_path / "dir-pub"
    corpus.mkdir()
    (corpus / "00.lts").write_text(DOC, encoding="utf-8")
    secret = tmp_path / "dir-secret"
    secret.mkdir()
    (secret / "00.lts").write_text("# S [S-DOC]\n\n## Vault [V-1]\n\n- status: active\n\nhi\n", encoding="utf-8")
    cfg = tmp_path / "registry.json"
    cfg.write_text(json.dumps({
        "pubcorp": {"roots": [{"path": str(corpus), "area": "specs"}], "domain_suffix": "proj.pub"},
        "vaultzed": {"roots": [{"path": str(secret), "area": "specs"}], "domain_suffix": "proj.v",
                     "private": True}}), encoding="utf-8")
    state = tmp_path / "state"
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(state))
    monkeypatch.setenv("LTS_ENGINE_STATE_DIR", str(state))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    monkeypatch.chdir(tmp_path)
    w = type("World", (), {})()
    w.tmp, w.cfg, w.corpus = tmp_path, cfg, corpus
    w.pointer = Path(os.environ["XDG_CONFIG_HOME"]) / "factlattice" / "config.json"
    return w


def set_pointer(w, target, **extra):
    w.pointer.parent.mkdir(parents=True, exist_ok=True)
    w.pointer.write_text(json.dumps({"corpus_config": str(target), **extra}), encoding="utf-8")
    return w.pointer


def sha_file_and_root(path: Path, root: str) -> str:
    """The world's registries are ABSOLUTE-ONLY, so the hash is over the bytes alone (round 2; see
    tests/test_registry_hash_scope.py for when cwd, root and HOME join it). `root` is kept for call-site clarity."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def call_door(argv, capsys):
    from factlattice_engine.__main__ import main
    capsys.readouterr()
    try:
        rc = main(["call", *argv])
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 2
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def call_json(name, args, capsys):
    rc, out, err = call_door([name, "--args-json", json.dumps(args), "--json"], capsys)
    return rc, json.loads(out), err


def reviewer_main(argv, capsys):
    from factlattice_engine.reviewer.__main__ import main
    capsys.readouterr()
    rc = main(argv)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


# =================================================================== (1) the resolver: sources and order
def test_sources_are_a_closed_set():
    from factlattice import registry
    assert registry.SOURCES == ("env", "user-config", "example")
    assert registry.POINTER_KEYS == ("corpus_config",)
    assert registry.CODE == "registry-not-found"


def test_nothing_configured_is_the_example_registry(world):
    from factlattice import registry
    c = registry.resolve_registry()
    assert c.source == "example" and c.path is None
    assert set(c.corpora) == EXAMPLE_IDS
    assert c.root == os.getcwd()                         # R-2: the example's root is the working directory
    assert len(c.sha256) == 64


def test_env_set_to_a_missing_file_raises_and_never_falls_back(world, monkeypatch):
    from factlattice import registry
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.tmp / "gone.json"))
    with pytest.raises(registry.RegistryError) as e:
        registry.resolve_registry()
    assert e.value.code == "registry-not-found" and str(world.tmp / "gone.json") in str(e.value)


def test_env_set_to_an_unparseable_file_raises_naming_the_parse_error(world, monkeypatch):
    from factlattice import registry
    bad = world.tmp / "bad.json"
    bad.write_text("{ not json")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(bad))
    with pytest.raises(registry.RegistryError) as e:
        registry.resolve_registry()
    assert "JSONDecodeError" in str(e.value)


@pytest.mark.parametrize("body", ["[]", '"x"', "3", "null"])
def test_env_file_with_a_non_object_top_level_raises(world, monkeypatch, body):
    from factlattice import registry
    f = world.tmp / "shape.json"
    f.write_text(body)
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(f))
    with pytest.raises(registry.RegistryError):
        registry.resolve_registry()


def test_an_empty_env_var_counts_as_unset(world, monkeypatch):
    """Legacy semantics kept (`if cfg_file and ...`): `export X=` is not a path."""
    from factlattice import registry
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", "")
    assert registry.resolve_registry().source == "example"


def test_pointer_file_supplies_the_registry(world):
    from factlattice import registry
    set_pointer(world, world.cfg)
    c = registry.resolve_registry()
    assert c.source == "user-config"
    assert c.path == os.path.realpath(world.cfg)
    assert set(c.corpora) == {"pubcorp", "vaultzed"}
    assert c.root == os.path.dirname(os.path.realpath(world.cfg))
    assert c.sha256 == sha_file_and_root(world.cfg, c.root)


def test_env_beats_the_pointer(world, monkeypatch):
    from factlattice import registry
    other = world.tmp / "other.json"
    other.write_text(json.dumps({"only": {"root": str(world.corpus)}}))
    set_pointer(world, world.cfg)
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(other))
    c = registry.resolve_registry()
    assert c.source == "env" and set(c.corpora) == {"only"} and c.path == str(other)


def test_a_broken_env_is_not_rescued_by_a_good_pointer(world, monkeypatch):
    from factlattice import registry
    set_pointer(world, world.cfg)
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.tmp / "gone.json"))
    with pytest.raises(registry.RegistryError):
        registry.resolve_registry()


def _bad_pointer_cases(w):
    d = w.tmp / "adir"
    d.mkdir()
    badreg = w.tmp / "badreg.json"
    badreg.write_text("{nope")
    return [
        ("missing-target", json.dumps({"corpus_config": str(w.tmp / "nowhere.json")})),
        ("target-is-a-directory", json.dumps({"corpus_config": str(d)})),
        ("target-unparseable", json.dumps({"corpus_config": str(badreg)})),
        ("relative-path", json.dumps({"corpus_config": "registry.json"})),
        ("unknown-key", json.dumps({"corpus_config": str(w.cfg), "extra": 1})),
        ("not-a-string", json.dumps({"corpus_config": 7})),
        ("empty-value", json.dumps({"corpus_config": ""})),
        ("null-value", json.dumps({"corpus_config": None})),
        ("no-key", json.dumps({})),
        ("not-an-object", json.dumps([str(w.cfg)])),
        ("bad-json", "{ not json"),
    ]


def test_every_broken_pointer_is_registry_not_found_naming_the_pointer(world):
    from factlattice import registry
    world.pointer.parent.mkdir(parents=True)
    for label, body in _bad_pointer_cases(world):
        world.pointer.write_text(body, encoding="utf-8")
        with pytest.raises(registry.RegistryError) as e:
            registry.resolve_registry()
        assert e.value.code == "registry-not-found", label
        assert str(world.pointer) in str(e.value), (label, str(e.value))


def test_a_pointer_that_is_a_directory_or_owned_by_someone_else_is_refused(world, monkeypatch):
    from factlattice import registry
    world.pointer.mkdir(parents=True)                    # the pointer path is itself a directory
    with pytest.raises(registry.RegistryError) as e:
        registry.resolve_registry()
    assert "regular file" in str(e.value)
    world.pointer.rmdir()
    set_pointer(world, world.cfg)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(world.pointer).st_uid + 1)
    with pytest.raises(registry.RegistryError) as e:
        registry.resolve_registry()
    assert "owned by" in str(e.value)


def test_the_pointer_value_is_tilde_expanded_then_realpathed(world):
    from factlattice import registry
    real = world.tmp / "real.json"
    real.write_text(json.dumps({"a": {"root": str(world.corpus)}}))
    link = Path(os.environ["HOME"]) / "link.json"
    link.symlink_to(real)
    world.pointer.parent.mkdir(parents=True)
    world.pointer.write_text(json.dumps({"corpus_config": "~/link.json"}))
    c = registry.resolve_registry()
    assert c.path == os.path.realpath(real) and c.source == "user-config"


def test_xdg_default_is_dot_config_under_home(world, monkeypatch):
    from factlattice import registry
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert registry.pointer_path() == os.path.join(os.environ["HOME"], ".config", "factlattice", "config.json")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")   # the XDG spec: a relative value is ignored
    assert registry.pointer_path() == os.path.join(os.environ["HOME"], ".config", "factlattice", "config.json")


def test_root_default_per_source_and_the_hash_covers_it(world, monkeypatch):
    """R-2: env/user-config -> the registry file's directory; example -> cwd; the variable beats both."""
    from factlattice import registry
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.cfg))
    a = registry.resolve_registry()
    assert a.root == str(world.tmp)
    monkeypatch.setenv("FACTLATTICE_CORPUS_ROOT", str(world.corpus))
    b = registry.resolve_registry()
    assert b.root == str(world.corpus) and b.sha256 == a.sha256 == sha_file_and_root(world.cfg, b.root)   # absolute-only: root is not in the hash
    assert a.corpora == b.corpora                          # the same bytes; only the root differs
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG")
    monkeypatch.setenv("FACTLATTICE_CORPUS_ROOT", str(world.corpus))
    ex = registry.resolve_registry()
    assert ex.source == "example" and ex.root == str(world.corpus)
    assert ex.corpora["alpha"]["roots"][0]["path"].startswith(str(world.corpus))


def test_the_statement_is_corpus_free(world, monkeypatch):
    from factlattice import registry
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.cfg))
    st = registry.resolve_registry().statement()
    assert set(st) == {"source", "path", "sha256"}
    assert "pubcorp" not in json.dumps(st) and "vaultzed" not in json.dumps(st)


# ============================================================ (3) the guard: one reader, constants included
def test_guard_the_real_tree_has_exactly_one_reader():
    found = G.scan_tree(REPO)
    assert found == [], ("the registry variables' NAMES or the pointer path appear outside "
                         "factlattice/registry.py: " + "; ".join(f"{p}: {n}" for p, n in found))


def test_guard_fires_on_a_planted_constant_based_read(tmp_path):
    """NEGATIVE CONTROL. The read that slipped past the first draft of this rule: a module CONSTANT."""
    (tmp_path / "factlattice").mkdir()
    (tmp_path / "factlattice" / "registry.py").write_text('ENV = "FACTLATTICE_CORPUS_CONFIG"\n')   # allowed
    (tmp_path / "factlattice" / "planted.py").write_text(
        'import os\nCFG_ENV = "FACTLATTICE_CORPUS_CONFIG"\ndef f():\n    return os.environ.get(CFG_ENV)\n')
    got = G.scan_tree(tmp_path)
    assert ("factlattice/planted.py", "FACTLATTICE_CORPUS_CONFIG") in got
    assert not any(p.endswith("registry.py") for p, _ in got)


def test_guard_fires_on_a_split_name_the_root_variable_and_the_pointer_path(tmp_path):
    (tmp_path / "factlattice_engine").mkdir()
    (tmp_path / "factlattice_engine" / "a.py").write_text('K = "FACTLATTICE_CORPUS_" + "CONFIG"\n')
    (tmp_path / "factlattice_engine" / "b.py").write_text('R = "FACTLATTICE_CORPUS_ROOT"\n')
    (tmp_path / "factlattice_engine" / "c.py").write_text(
        'import os\np = os.path.join(os.environ["XDG_CONFIG_HOME"], "factlattice", "config.json")\n')
    got = G.scan_tree(tmp_path)
    assert ("factlattice_engine/a.py", "FACTLATTICE_CORPUS_CONFIG (folded constant)") in got
    assert ("factlattice_engine/b.py", "FACTLATTICE_CORPUS_ROOT") in got
    assert ("factlattice_engine/c.py", "XDG_CONFIG_HOME") in got


def test_guard_ignores_tests_and_the_resolver(tmp_path):
    (tmp_path / "factlattice").mkdir()
    (tmp_path / "factlattice" / "test_x.py").write_text('K = "FACTLATTICE_CORPUS_CONFIG"\n')
    assert G.scan_tree(tmp_path) == []


# ============================================================ (2) every caller routes through it
def test_default_corpora_is_a_thin_wrapper_over_the_resolver(world, monkeypatch):
    from factlattice import registry
    from factlattice_engine import config
    planted = registry.RegistryChoice("env", "/x/planted.json", "0" * 64, "/x", {"planted": {"root": "/x"}})
    monkeypatch.setattr(registry, "resolve_registry", lambda: planted)
    assert config.default_corpora() == {"planted": {"root": "/x"}}


def test_default_corpora_raises_for_a_missing_env_file(world, monkeypatch):
    from factlattice import registry
    from factlattice_engine import config
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.tmp / "gone.json"))
    with pytest.raises(registry.RegistryError):
        config.default_corpora()


def test_config_has_no_import_time_root_read():
    from factlattice_engine import config
    assert not hasattr(config, "CORPUS_ROOT"), "config.py:48's import-time root read must be gone (R-2)"


def test_load_corpus_config_raises_on_a_broken_registry_instead_of_returning_empty(world, monkeypatch):
    from factlattice import registry
    from factlattice.profile import load_corpus_config
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.tmp / "gone.json"))
    with pytest.raises(registry.RegistryError):
        load_corpus_config()


def test_load_corpus_config_reads_the_pointer(world):
    from factlattice.profile import load_corpus_config
    set_pointer(world, world.cfg)
    assert set(load_corpus_config()) == {"pubcorp", "vaultzed"}


def test_an_explicit_path_is_strict_too(world):
    from factlattice import registry
    from factlattice.profile import load_corpus_config
    assert set(load_corpus_config(str(world.cfg))) == {"pubcorp", "vaultzed"}
    with pytest.raises(registry.RegistryError):
        load_corpus_config(str(world.tmp / "gone.json"))


def test_registered_profile_for_root_follows_the_pointer(world):
    """With ONLY a pointer file (the acceptance condition) the profile registration must be seen."""
    from factlattice.profile import registered_profile_for_root
    prof = world.tmp / "p.lts"
    prof.write_text("# P [PROFILE-X]\n")
    reg = world.tmp / "withprofile.json"
    reg.write_text(json.dumps({"pubcorp": {"roots": [{"path": str(world.corpus)}], "profile": str(prof)}}))
    set_pointer(world, reg)
    got = registered_profile_for_root(str(world.corpus))
    assert got is not None and got[0] == "pubcorp" and got[1] == str(prof)


def test_profile_audit_resolves_a_registered_profile_through_the_pointer(world):
    from factlattice_engine.analytics.profile_audit import resolve_profile_adapter
    prof = world.tmp / "p2.lts"
    prof.write_text("# P [PROFILE-Y]\n")
    reg = world.tmp / "withprofile2.json"
    reg.write_text(json.dumps({"pubcorp": {"roots": [{"path": str(world.corpus)}], "profile": str(prof)}}))
    set_pointer(world, reg)
    ad = resolve_profile_adapter(None, "pubcorp")
    assert getattr(ad, "resolved_from", None) == "corpus-config"


def test_profile_audit_surfaces_a_broken_registry_as_a_named_failure(world, monkeypatch):
    from factlattice import registry
    from factlattice_engine.analytics.profile_audit import resolve_profile_adapter
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.tmp / "gone.json"))
    with pytest.raises(registry.RegistryError):
        resolve_profile_adapter(None, "pubcorp")


def test_profile_registration_audit_does_not_report_ok_over_a_broken_registry(world, monkeypatch):
    from factlattice import registry
    from factlattice_engine.analytics.profile_registration import audit_profile_registration
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.tmp / "gone.json"))
    with pytest.raises(registry.RegistryError):
        audit_profile_registration(None)


def test_corpus_parse_does_not_silently_ignore_a_broken_registry(world, monkeypatch):
    """`factlattice.corpus.parse` resolves a profile through the registry: a broken one is a failure."""
    from factlattice import registry
    from factlattice.corpus import parse
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.tmp / "gone.json"))
    with pytest.raises(registry.RegistryError):
        parse(str(world.corpus))


def test_check_registrations_FAILS_on_a_broken_pointer_it_does_not_report_ok_with_zero(world, capsys):
    """R-3's RED: a broken pointer made `check-registrations` pass with zero registrations."""
    set_pointer(world, world.tmp / "nowhere.json")
    rc, out, _ = reviewer_main(["check-registrations", "--corpus", "pubcorp"], capsys)
    assert rc == 2, out
    body = json.loads(out)
    assert body.get("ok") is False and body.get("reason") == "registry-not-found"


def test_membership_propagates_a_registry_error_instead_of_swallowing_it(world, monkeypatch):
    from factlattice import registry
    from factlattice_engine import membership
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.tmp / "gone.json"))
    with pytest.raises(registry.RegistryError):
        membership.registered_roots(None)


def test_a_write_receipt_survives_a_broken_registry_and_says_so(world, monkeypatch):
    """Membership propagates, but a write that LANDED must not be reported as an error after the fact."""
    from factlattice_engine import membership
    (world.tmp / "wdir").mkdir()
    target = world.tmp / "wdir" / "w.lts"      # NOT beside the state dir: the write lock refuses a lock dir inside the corpus dir
    target.write_text("# W [W-DOC]\n\n## T [W-1]\n\n- status: active\n", encoding="utf-8")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.tmp / "gone.json"))
    from factlattice_engine import tools
    res = tools.dispatch("lts_add_property", {"path": str(target), "node_id": "W-1", "key": "note",
                                              "value": "x"}, tools=None)
    assert res.get("success") is True, res
    assert "x" in target.read_text()
    rm = res.get("root_membership") or {}
    assert rm.get("registry_error") and "registry-not-found" in rm["registry_error"]
    assert any("registry-not-found" in w for w in res.get("warnings") or [])


def test_register_corpus_refuses_with_the_code_on_a_broken_registry(world, monkeypatch):
    from factlattice_engine.server import build_tools
    from factlattice_engine import tools
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.cfg))
    eng = build_tools(watch=False)
    world.cfg.write_text("{ torn")
    res = tools.dispatch("lts_register_corpus", {"corpus_id": "later"}, tools=eng)
    assert res["success"] is False and res.get("reason") == "registry-not-found"
    assert "registry-not-found" in res["error"]


def test_corpus_config_info_states_source_path_and_hash(world, monkeypatch):
    from factlattice_engine import callcli
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.cfg))
    info = callcli.corpus_config_info()
    assert info["source"] == "env" and info["path"] == str(world.cfg) and info["loaded"] is True
    assert info["sha256"] == sha_file_and_root(world.cfg, str(world.tmp))


def test_call_context_over_a_pointer_states_the_pointer_registry(world, capsys):
    set_pointer(world, world.cfg)
    rc, env, err = call_json("lts_selfcheck", {}, capsys)
    cc = env["call_context"]["corpus_config"]
    assert rc == 0 and cc["source"] == "user-config" and cc["path"] == os.path.realpath(world.cfg)
    assert env["call_context"]["registry"]["corpus_count"] == 2       # both entries loaded (a private one is counted, never named)
    assert env["call_context"]["registry"]["registry_entries"] == 2


# ====================================== (4) registry-not-found for a named corpus under the example source
def test_call_names_a_corpus_under_the_example_registry_is_registry_not_found_exit_2(world, capsys):
    rc, env, err = call_json("lts_find", {"corpus": "zz-private-x", "text": "a"}, capsys)
    assert rc == 2, env
    res = env["result"] if env.get("result") else env
    msg = env.get("error") or res.get("error")
    assert msg.startswith("registry-not-found: ") or NOT_FOUND_TEXT.format(id="zz-private-x") in msg, msg
    assert NOT_FOUND_TEXT.format(id="zz-private-x") in msg
    assert "corpus not found" not in msg                   # never the generic unknown-corpus error
    assert "did you mean" not in msg and "loaded corpora" not in msg


def test_the_mcp_door_gives_the_same_refusal(world):
    from factlattice_engine.server import Server, build_tools
    srv = Server(build_tools(watch=False, require_corpora=False))
    resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "lts_node", "arguments": {"corpus": "zz-x", "node_id": "A"}}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is True and body["reason"] == "registry-not-found"
    assert NOT_FOUND_TEXT.format(id="zz-x") in body["error"]


def test_an_unknown_corpus_under_a_configured_registry_keeps_the_generic_error(world, monkeypatch, capsys):
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.cfg))
    rc, env, _ = call_json("lts_find", {"corpus": "zz-typo", "text": "a"}, capsys)
    assert rc == 1                                         # tool error, not the configuration class
    assert "corpus not found" in json.dumps(env)
    assert "registry-not-found" not in json.dumps(env)


def test_no_private_corpus_id_in_any_registry_error_text(world, monkeypatch, capsys):
    from factlattice import registry
    # 1) a broken registry that names a private corpus in its own bytes
    reg = world.tmp / "priv.json"
    reg.write_text('{"quokka-personality": {"root": "/x"}, ')      # torn mid-write
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(reg))
    with pytest.raises(registry.RegistryError) as e:
        registry.resolve_registry()
    assert "quokka" not in str(e.value)
    # 2) the example-registry refusal names only the id the CALLER typed
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG")
    rc, env, _ = call_json("lts_find", {"corpus": "zz-x", "text": "a"}, capsys)
    blob = json.dumps(env)
    for private in ("vaultzed", "quokka"):
        assert private not in blob
    # 3) the same, through a pointer to a registry that holds a private corpus, then removed
    set_pointer(world, world.cfg)
    world.cfg.write_text("{ torn")
    rc, env, err = call_json("lts_find", {"corpus": "zz-x", "text": "a"}, capsys)
    assert rc == 2 and "vaultzed" not in json.dumps(env) and "vaultzed" not in err


def test_a_corpus_named_in_the_example_registry_but_not_loaded_is_not_registry_not_found(world, capsys):
    rc, env, _ = call_json("lts_find", {"corpus": "alpha", "text": "a"}, capsys)
    assert "registry-not-found" not in json.dumps(env)


@pytest.mark.parametrize("argv", [
    ["resolve-output", "--corpus", "zz-x", "--document", "/nonexistent/doc.lts"],
    ["check-registrations", "--corpus", "zz-x"],
    ["frame", "--corpus", "zz-x", "--source-file", "a.lts"],
])
def test_reviewer_helpers_exit_2_on_registry_not_found_under_the_example_source(world, capsys, argv):
    rc, out, err = reviewer_main(argv, capsys)
    assert rc == 2, (out, err)
    assert "registry-not-found" in out + err


def test_reviewer_helpers_exit_2_when_the_env_registry_is_missing(world, monkeypatch, capsys):
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.tmp / "gone.json"))
    rc, out, err = reviewer_main(["resolve-output", "--corpus", "pubcorp", "--document", "/x/d.lts"], capsys)
    assert rc == 2 and "registry-not-found" in out


def test_the_call_door_exits_2_for_a_missing_env_registry_with_no_example_fallback(world, monkeypatch, capsys):
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.tmp / "gone.json"))
    rc, env, err = call_json("lts_selfcheck", {}, capsys)
    assert rc == 2 and env["success"] is False
    assert "registry-not-found" in env["error"]
    cc = env["call_context"]["corpus_config"]
    assert cc["loaded"] is False and cc["path"] == str(world.tmp / "gone.json")
    assert "built-in example registry is in use" not in cc["note"]       # it did NOT fall back
    assert "NOT LOADED" in cc["note"]


# ========================================================= (5) lts_list_corpora.registry_choice
def _engine(monkeypatch, cfg):
    from factlattice_engine.server import build_tools
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(cfg))
    return build_tools(watch=False)


def _list(eng):
    from factlattice_engine import tools
    return tools.dispatch("lts_list_corpora", {"heal": False}, tools=eng)


def test_registry_choice_shape_and_boot_versus_now(world, monkeypatch):
    eng = _engine(monkeypatch, world.cfg)
    rc = _list(eng)["registry_choice"]
    assert set(rc) == {"source", "path", "sha256_at_boot", "sha256_now", "resolved_at"}
    assert rc["source"] == "env" and rc["path"] == str(world.cfg)
    assert rc["sha256_at_boot"] == rc["sha256_now"] == sha_file_and_root(world.cfg, str(world.tmp))
    assert "pubcorp" not in json.dumps(rc) and "vaultzed" not in json.dumps(rc)       # corpus-free
    world.cfg.write_text(world.cfg.read_text() + "\n")                                  # an edit after boot
    rc2 = _list(eng)["registry_choice"]
    assert rc2["sha256_at_boot"] == rc["sha256_at_boot"] and rc2["sha256_now"] != rc2["sha256_at_boot"]
    assert rc2["resolved_at"] == rc["resolved_at"]                                       # captured ONCE


def test_registry_choice_under_the_example_source_is_readable(world):
    from factlattice_engine.server import build_tools
    eng = build_tools(watch=False, require_corpora=False)
    rc = _list(eng)["registry_choice"]
    assert rc["source"] == "example" and rc["path"] is None and rc["sha256_at_boot"] == rc["sha256_now"]


def test_registry_choice_schema_is_declared_and_matches(world, monkeypatch):
    from factlattice import registry
    from factlattice_engine.server import TOOLS
    eng = _engine(monkeypatch, world.cfg)
    rc = _list(eng)["registry_choice"]
    schema = registry.REGISTRY_CHOICE_SCHEMA
    assert set(schema["required"]) == set(rc)
    assert set(schema["properties"]) >= set(rc) | {"error"}
    desc = next(t for t in TOOLS if t["name"] == "lts_list_corpora")["description"]
    assert "registry_choice" in desc and "sha256_at_boot" in desc


def test_spec_md_documents_registry_choice():
    spec = (REPO / "SPEC.md").read_text(encoding="utf-8")
    assert "registry_choice" in spec and "sha256_at_boot" in spec and "registry-not-found" in spec


def test_a_server_built_without_the_composition_root_omits_the_field_rather_than_inventing_it():
    """An engine seated by hand (a unit test) never resolved a registry: the field is ABSENT, which is
    exactly what the reviewer reads as 'server too old'. It is never a made-up value."""
    from factlattice_engine.store import LtsStore
    from factlattice_engine.tools import LtsEngine
    assert "registry_choice" not in LtsEngine(LtsStore()).list_corpora(heal=False)


def test_extent_carries_registry_source_for_a_corpus_omitted_read_under_example_only(world, monkeypatch):
    from factlattice_engine import tools
    from factlattice_engine.server import build_tools
    eng = build_tools(watch=False, require_corpora=False)
    res = tools.dispatch("lts_find", {"prefix": "REVIEWER-SITE-CONFIG"}, tools=eng)
    assert res["extent"]["registry_source"] == "example"
    eng2 = _engine(monkeypatch, world.cfg)
    res2 = tools.dispatch("lts_find", {"prefix": "P"}, tools=eng2)
    assert "registry_source" not in res2["extent"]
    res3 = tools.dispatch("lts_find", {"corpus": "pubcorp", "prefix": "PUB"}, tools=eng2)
    assert "registry_source" not in res3["extent"]


# ===================================== (6) REGISTRY-ERROR MODE (v3.1): boot, list, refuse, zero data, restart
def _broken_boot(world, monkeypatch, body="{ torn"):
    from factlattice_engine import server
    world.cfg.write_text(body)
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.cfg))
    return server.boot_engine(retry_delay_s=0.01, watch=False)


def _handle(eng, method, params=None):
    from factlattice_engine.server import Server
    resp = Server(eng).handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    if method == "tools/call":
        return json.loads(resp["result"]["content"][0]["text"]), resp["result"]["isError"]
    return resp["result"], None


def test_a_broken_registry_boots_in_error_mode_and_lists_every_tool(world, monkeypatch):
    from factlattice_engine.server import TOOLS
    eng = _broken_boot(world, monkeypatch)
    listed, _ = _handle(eng, "tools/list")
    assert [t["name"] for t in listed["tools"]] == [t["name"] for t in TOOLS]


def test_error_mode_list_corpora_reports_source_path_and_error(world, monkeypatch):
    eng = _broken_boot(world, monkeypatch)
    body, is_err = _handle(eng, "tools/call", {"name": "lts_list_corpora", "arguments": {}})
    rc = body["registry_choice"]
    assert set(rc) == {"source", "path", "error", "error_persist"} and rc["source"] == "env" and rc["path"] == str(world.cfg)
    assert "registry-not-found" in rc["error"]
    assert body["count"] == 0 and body["corpora"] == []


CORPUS_SCOPED_ARGS = {
    "lts_find": {"corpus": "pubcorp", "text": "hello"},
    "lts_find_in_prose": {"corpus": "pubcorp", "text": "hello"},
    "lts_node": {"corpus": "pubcorp", "node_id": "PUB-1"},
    "lts_traverse": {"corpus": "pubcorp", "start_node": "PUB-1"},
    "lts_reverse": {"corpus": "pubcorp", "node_id": "PUB-1"},
    "lts_history": {"corpus": "pubcorp", "node_id": "PUB-1"},
    "lts_flow": {"corpus": "pubcorp", "start_node": "PUB-1"},
    "lts_capabilities": {"corpus": "pubcorp"},
    "lts_aggregate": {"op": "count", "corpus": "pubcorp"},
    "lts_profile_audit": {"corpus": "pubcorp"},
    "lts_semantic_find": {"corpus": "pubcorp", "text": "hello"},
    "lts_report_miss": {"corpus": "pubcorp", "query": "q", "expected_ids": ["PUB-1"], "why": "w"},
    "lts_register_corpus": {"corpus_id": "pubcorp"},
    "lts_unregister_corpus": {"corpus_id": "pubcorp"},
}
CORPUS_FREE = {"lts_lint", "lts_preview", "lts_selfcheck", "lts_list_corpora", "lts_add_property",
               "lts_add_node", "lts_repair_property", "lts_property_loss",
               "lts_rename_property", "lts_remove_property"}


def test_every_served_tool_is_classified_for_error_mode():
    from factlattice_engine.server import TOOLS
    from factlattice_engine.tools import REGISTRY_ERROR_EXEMPT_TOOLS
    names = {t["name"] for t in TOOLS}
    assert set(REGISTRY_ERROR_EXEMPT_TOOLS) == CORPUS_FREE
    assert names == set(CORPUS_SCOPED_ARGS) | CORPUS_FREE, (
        "a tool was added or removed: classify it as corpus-scoped (refused in registry-error mode) or "
        "corpus-free here, and in tools.REGISTRY_ERROR_EXEMPT_TOOLS")


def test_in_error_mode_every_corpus_scoped_tool_refuses_with_zero_data(world, monkeypatch):
    eng = _broken_boot(world, monkeypatch)
    for name, args in CORPUS_SCOPED_ARGS.items():
        body, is_err = _handle(eng, "tools/call", {"name": name, "arguments": args})
        assert is_err is True and body["success"] is False, (name, body)
        assert body["reason"] == "registry-not-found", (name, body)
        for data_key in ("nodes", "node", "edges", "results", "candidates", "corpora", "digest", "diagram"):
            assert not body.get(data_key), (name, data_key)
        assert "restart" in body["error"].lower(), name


def test_error_mode_ends_only_by_restart_even_after_the_file_is_fixed(world, monkeypatch):
    eng = _broken_boot(world, monkeypatch)
    good = {"pubcorp": {"roots": [{"path": str(world.corpus), "area": "specs"}], "domain_suffix": "proj.pub"}}
    world.cfg.write_text(json.dumps(good))                                  # the operator fixes the file
    body, is_err = _handle(eng, "tools/call", {"name": "lts_find", "arguments": {"corpus": "pubcorp", "prefix": "PUB"}})
    assert is_err is True and body["reason"] == "registry-not-found" and not body.get("nodes")
    assert "restart" in body["error"].lower()
    from factlattice_engine import server                                    # ...and a RESTART serves it
    fresh = server.boot_engine(retry_delay_s=0.01, watch=False)
    body, is_err = _handle(fresh, "tools/call", {"name": "lts_find", "arguments": {"corpus": "pubcorp", "prefix": "PUB"}})
    assert is_err is False and body["success"] is True and body["nodes"]


def test_error_mode_still_answers_the_store_free_tools(world, monkeypatch):
    eng = _broken_boot(world, monkeypatch)
    body, is_err = _handle(eng, "tools/call", {"name": "lts_selfcheck", "arguments": {}})
    assert body["success"] is True


def test_a_parse_error_at_boot_is_retried_once_and_a_partial_then_complete_file_boots_normally(world, monkeypatch):
    from factlattice_engine import server
    good = json.dumps({"pubcorp": {"roots": [{"path": str(world.corpus), "area": "specs"}],
                                   "domain_suffix": "proj.pub"}})
    world.cfg.write_text(good[: len(good) // 2])                            # an editor mid-save
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.cfg))

    def finish():
        time.sleep(0.15)
        world.cfg.write_text(good)

    t = threading.Thread(target=finish)
    t.start()
    eng = server.boot_engine(retry_delay_s=0.6, watch=False)
    t.join()
    assert getattr(eng, "registry_error", None) is None
    body, is_err = _handle(eng, "tools/call", {"name": "lts_find", "arguments": {"corpus": "pubcorp", "prefix": "PUB"}})
    assert is_err is False and body["nodes"]


def test_the_boot_retries_exactly_once_then_enters_error_mode(world, monkeypatch):
    from factlattice import registry
    from factlattice_engine import server
    calls = []
    real = registry.resolve_registry

    def counting():
        calls.append(time.monotonic())
        return real()

    monkeypatch.setattr(registry, "resolve_registry", counting)
    world.cfg.write_text("{ torn")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.cfg))
    eng = server.boot_engine(retry_delay_s=0.2, watch=False)
    assert len(calls) == 2 and calls[1] - calls[0] >= 0.19
    assert eng.registry_error is not None


# ================================================================================= (7) the reviewer
def test_registry_not_found_is_the_sixth_and_last_no_document_stop():
    from factlattice_engine import reviewer
    assert len(reviewer.NO_DOCUMENT_STOPS) == 6 and reviewer.NO_DOCUMENT_STOPS[-1] == "registry-not-found"
    assert reviewer.NO_DOCUMENT_STOPS[:5] == ("no-route", "no-output-root", "server-too-old", "no-corpus-given",
                                              "first-create-refused-or-unknown")
    assert reviewer.no_document_reply("registry-not-found") == "NO-REVIEW-DOCUMENT: registry-not-found"


def test_review_format_version_is_2_and_the_two_registry_fields_are_required():
    from factlattice_engine import reviewer
    from tests import reviewer_contract as C
    assert reviewer.REVIEW_FORMAT_VERSION == C.REVIEW_FORMAT_VERSION == 2
    for node in ("REVIEW", "UNREVIEWED"):
        assert "registry-mcp" in C.REQUIRED[node] and "registry-cli" in C.REQUIRED[node]


def _choice(**kw):
    base = {"source": "env", "path": "/r/reg.json", "sha256_at_boot": "a" * 64, "sha256_now": "a" * 64,
            "resolved_at": "2026-10-07T00:00:00Z"}
    base.update(kw)
    return base


CLI = {"source": "env", "path": "/r/reg.json", "sha256": "a" * 64}


def test_compare_registries_match_records_both_routes():
    from factlattice_engine.reviewer import compare_registries
    r = compare_registries(_choice(), CLI)
    assert r["mcp_used"] is True and r["mismatch"] is False and r["stale"] is False and r["reason"] is None
    assert r["registry_mcp"] == r["registry_cli"] == "env reg.json sha256:" + "a" * 64


def test_compare_registries_a_hash_difference_stops_with_the_reason():
    from factlattice_engine.reviewer import compare_registries
    mcp = _choice(sha256_now="b" * 64, sha256_at_boot="b" * 64)         # not stale: the server loaded exactly that
    r = compare_registries(mcp, CLI)
    assert r["mismatch"] is True and r["mcp_used"] is True
    assert r["reason"] == "registry-mismatch" and "sha256 differs" in r["explanation"]


@pytest.mark.parametrize("field,value", [("source", "user-config"), ("path", "/other/reg.json")])
def test_compare_registries_source_and_path_are_recorded_never_decisive(field, value):
    """Round 3 (P1-1): only the hash decides; source and path are the audit trail."""
    from factlattice_engine.reviewer import compare_registries
    r = compare_registries(_choice(**{field: value}), CLI)
    assert r["mismatch"] is False and r["mcp_used"] is True and r["reason"] is None
    assert os.path.basename(value) in r["registry_mcp"]


def test_compare_registries_a_stale_server_is_not_a_mismatch_and_its_reads_are_not_used():
    from factlattice_engine.reviewer import compare_registries
    r = compare_registries(_choice(sha256_at_boot="c" * 64), CLI)        # boot != now: it serves an OLDER registry
    assert r["stale"] is True and r["mismatch"] is False and r["mcp_used"] is False
    assert r["registry_mcp"].startswith("not-used (stale server")
    assert r["registry_cli"] == "env reg.json sha256:" + "a" * 64


def test_compare_registries_an_old_server_without_the_field_is_not_used_and_cannot_mismatch():
    """E4 / R-9: the field ABSENT is 'server too old', never 'nothing to report'."""
    from factlattice_engine.reviewer import compare_registries
    r = compare_registries(None, CLI)
    assert r["mcp_used"] is False and r["mismatch"] is False
    assert r["registry_mcp"] == "not-used (server too old)"


def test_compare_registries_an_error_mode_server_is_not_used():
    from factlattice_engine.reviewer import compare_registries
    r = compare_registries({"source": "env", "path": "/r/reg.json", "error": "registry-not-found: x"}, CLI)
    assert r["mcp_used"] is False and r["mismatch"] is False
    assert r["registry_mcp"].startswith("not-used (registry error mode")


def test_compare_registries_with_no_mcp_route_at_all():
    from factlattice_engine.reviewer import compare_registries
    r = compare_registries(False, CLI)
    assert r["registry_mcp"] == "not-used (no MCP route)" and r["mismatch"] is False


def test_registry_check_command_reports_match_mismatch_and_not_found(world, monkeypatch, capsys):
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.cfg))
    from factlattice import registry
    c = registry.resolve_registry()
    mcp = {"source": c.source, "path": c.path, "sha256_at_boot": c.sha256, "sha256_now": c.sha256,
           "resolved_at": "t"}
    rc, out, _ = reviewer_main(["registry-check", "--mcp-json", json.dumps(mcp)], capsys)
    body = json.loads(out)
    assert rc == 0 and body["mismatch"] is False and body["registry_cli"].endswith("sha256:" + c.sha256)
    mcp["sha256_now"] = mcp["sha256_at_boot"] = "d" * 64
    rc, out, _ = reviewer_main(["registry-check", "--mcp-json", json.dumps(mcp)], capsys)
    assert rc == 1 and json.loads(out)["reason"].startswith("registry-mismatch")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world.tmp / "gone.json"))
    rc, out, _ = reviewer_main(["registry-check"], capsys)
    assert rc == 2 and json.loads(out)["reason"] == "registry-not-found"


def test_the_reviewer_procedure_and_agent_definition_use_the_registry_check():
    core = (REPO / "factlattice_engine/reviewer/shipped/reviewer-core.lts").read_text(encoding="utf-8")
    agent = (REPO / "agents/factlattice-reviewer.md").read_text(encoding="utf-8")
    for text in (core,):
        assert "registry-check" in text and "registry-mcp" in text and "registry-cli" in text
        assert "registry-not-found" in text and "registry-mismatch" in text
    assert "registry-not-found" in agent or "registry-check" in agent


# ========================================================================= (9) checklist rows and docs
ROWS = ("REGISTRY-SINGLE-READER", "POINTER-FILE-FORMAT", "ERROR-CODE-REGISTRY-NOT-FOUND",
        "CAPABILITY-REGISTRY-CHOICE", "REGISTRY-SOURCES", "REGISTRY-SOURCE-EXTENT-KEY",
        "GUARD-RESOLVES-CONSTANTS", "TEST-CONFIG-ISOLATION")


@pytest.mark.parametrize("row", ROWS)
def test_sdlc_carries_the_registry_checklist_row(row):
    sdlc = (REPO / "SDLC.md").read_text(encoding="utf-8")
    line = next((ln for ln in sdlc.splitlines() if ln.startswith("- [ ] [" + row + "]")), None)
    assert line, f"SDLC.md section 7.6 has no row [{row}]"
    assert len(line) > 120


def test_the_conftest_isolates_the_per_user_config():
    """TEST-CONFIG-ISOLATION: the autouse fixture is what makes the other tests safe on a machine that HAS a pointer."""
    import pwd
    login_home = pwd.getpwuid(os.getuid()).pw_dir
    assert os.environ["HOME"] != login_home, "HOME is the developer's real home: the isolation fixture did not run"
    assert os.environ["XDG_CONFIG_HOME"].startswith(os.environ["HOME"])
    assert "FACTLATTICE_CORPUS_CONFIG" not in os.environ and "FACTLATTICE_CORPUS_ROOT" not in os.environ
    assert not (Path(os.environ["XDG_CONFIG_HOME"]) / "factlattice" / "config.json").exists()


def test_the_try_before_you_deploy_banner_is_the_one_the_code_prints(world, capsys):
    doc = (REPO / "docs/TRY-BEFORE-YOU-DEPLOY.md").read_text(encoding="utf-8")
    rc, out, err = call_door(["lts_selfcheck"], capsys)
    banner_line = next(ln for ln in err.splitlines() if ln.strip().startswith("registry :"))
    assert banner_line.strip() in doc, banner_line


# ============================================ end to end: the REAL stdio server, and the other doors
def _stdio(world, env_extra, requests, timeout=60):
    import subprocess
    import sys
    env = {**os.environ, "PYTHONPATH": str(REPO), **env_extra}
    p = subprocess.run([sys.executable, "-m", "factlattice_engine.server"], input="\n".join(
        json.dumps(r) for r in requests) + "\n", capture_output=True, text=True, cwd=str(world.tmp), env=env,
        timeout=timeout)
    outs = [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()]
    return p, {o["id"]: o for o in outs if "id" in o}


def test_the_real_stdio_server_boots_in_error_mode_lists_tools_and_refuses_with_zero_data(world):
    """The whole v3.1 claim through the process an MCP client actually starts: broken registry -> it BOOTS
    (exit 0 at EOF, not a crash), lists every tool, and a corpus-scoped call is `registry-not-found`."""
    from factlattice_engine.server import TOOLS
    world.cfg.write_text("{ torn")
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "lts_find", "arguments": {"corpus": "pubcorp", "prefix": "PUB"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "lts_list_corpora", "arguments": {}}}]
    p, by_id = _stdio(world, {"FACTLATTICE_CORPUS_CONFIG": str(world.cfg), "FACTLATTICE_CODE_WATCH": "0"}, reqs)
    assert p.returncode == 0, p.stderr[-800:]
    assert [t["name"] for t in by_id[2]["result"]["tools"]] == [t["name"] for t in TOOLS]
    found = json.loads(by_id[3]["result"]["content"][0]["text"])
    assert by_id[3]["result"]["isError"] is True and found["reason"] == "registry-not-found" and not found.get("nodes")
    lc = json.loads(by_id[4]["result"]["content"][0]["text"])
    assert set(lc["registry_choice"]) == {"source", "path", "error", "error_persist"} and lc["count"] == 0
    assert "REGISTRY-ERROR MODE" in p.stderr                      # and the operator is told on stderr, not only the client


def test_the_real_stdio_server_with_a_good_registry_reports_registry_choice(world):
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "lts_list_corpora", "arguments": {"heal": False}}}]
    p, by_id = _stdio(world, {"FACTLATTICE_CORPUS_CONFIG": str(world.cfg), "FACTLATTICE_CODE_WATCH": "0"}, reqs)
    lc = json.loads(by_id[2]["result"]["content"][0]["text"])
    assert lc["registry_choice"]["source"] == "env" and lc["registry_choice"]["sha256_at_boot"] == lc["registry_choice"]["sha256_now"]


def test_mcp_and_call_in_the_same_environment_report_the_same_source_path_and_hash(world, monkeypatch, capsys):
    """P2 for the route pair, in one environment: what the server says it loaded == what `call` resolves."""
    set_pointer(world, world.cfg)
    eng = __import__("factlattice_engine.server", fromlist=["build_tools"]).build_tools(watch=False)
    rc_mcp = _list(eng)["registry_choice"]
    rc, env, _ = call_json("lts_selfcheck", {}, capsys)
    cc = env["call_context"]["corpus_config"]
    assert (rc_mcp["source"], rc_mcp["path"], rc_mcp["sha256_now"]) == (cc["source"], cc["path"], cc["sha256"])
    from factlattice_engine.reviewer import compare_registries
    from factlattice import registry
    cmp = compare_registries(rc_mcp, registry.resolve_registry().statement())
    assert cmp["mcp_used"] and not cmp["mismatch"] and cmp["registry_mcp"] == cmp["registry_cli"]


def test_the_semantic_subcommand_stops_on_registry_not_found(world, capsys):
    from factlattice_engine.__main__ import main
    rc = main(["semantic", "a question", "--corpus", "zz-x"])
    err = capsys.readouterr().err
    assert rc == 2 and "registry-not-found" in err and "has no corpus 'zz-x'" in err


def test_a_runtime_registration_does_not_wedge_the_registry_check(world, monkeypatch):
    """R-8: an edit of the registry file after boot is a STALE server (its reads are not used), never a mismatch
    stop -- a legitimate runtime registration must not make every later review UNREVIEWED."""
    from factlattice import registry
    from factlattice_engine.reviewer import compare_registries
    eng = _engine(monkeypatch, world.cfg)
    world.cfg.write_text(world.cfg.read_text() + "\n")
    cmp = compare_registries(_list(eng)["registry_choice"], registry.resolve_registry().statement())
    assert cmp["stale"] is True and cmp["mismatch"] is False and cmp["mcp_used"] is False
