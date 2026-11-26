"""S1 of specs/36-named-roots.lts: the named_roots table, ${name}, resolved_entries, the root path fixes, root health.

Every check here runs with sibling directories for home, corpus and registry, and the end-to-end ones run in a fresh process
with only the registry variable set: the shape a real caller has. The names and paths are invented ([INV-NR-NO-DEPLOYMENT-NAMES]).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE_TREE = str(Path(__file__).resolve().parent.parent)


@pytest.fixture()
def w(tmp_path, monkeypatch):
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "st"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.delenv("FACTLATTICE_CORPUS_ROOT", raising=False)
    for d in ("home", "corpora", "cfg", "elsewhere"):
        (tmp_path / d).mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path / "elsewhere")
    return tmp_path


def corpus(dirpath: Path, node="ALPHA-ONE") -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "a.lts").write_text(f'---\nlts: "1.1"\ntitle: T\n---\n\n# One [{node}]\n\n- summary: s\n', encoding="utf-8")
    return dirpath


def write_registry(w: Path, data, monkeypatch=None, name="reg.json") -> Path:
    f = w / "cfg" / name
    f.write_text(json.dumps(data), encoding="utf-8")
    if monkeypatch is not None:
        monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(f))
    return f


def engine():
    from factlattice_engine import config
    from factlattice_engine.tools import LtsEngine
    return LtsEngine(config.build_store())


def listed(eng) -> dict:
    return eng.list_corpora(heal=False)


def entry_of(reply: dict, cid: str):
    return next((c for c in reply["corpora"] if c["corpus_id"] == cid), None)


# ------------------------------------------------------------------------------------- [GRAM-NR-TABLE] x-reference
def test_a_reference_expands_in_every_location_form_and_leaves_the_rest_of_the_entry_alone(w, monkeypatch):
    from factlattice import registry as R
    base = str(w / "corpora")
    write_registry(w, {
        "named_roots": {"data": {"path": base}},
        "many": {"roots": [{"path": "${data}/many", "area": "specs", "only_file": "x.lts"}], "domain_suffix": "d.many",
                 "profile": "${data}/p.lts", "private": True},
        "bare": {"roots": ["${data}/bare"]},
        "single": {"root": "${data}"},
    }, monkeypatch)
    got = R.resolve_registry().corpora
    assert got["many"] == {"roots": [{"path": base + "/many", "area": "specs", "only_file": "x.lts"}],
                           "domain_suffix": "d.many", "profile": base + "/p.lts", "private": True}
    assert got["bare"]["roots"] == [base + "/bare"]
    assert got["single"]["root"] == base


def test_the_table_is_stripped_and_every_reader_gets_the_same_expanded_entries(w, monkeypatch):
    from factlattice import registry as R
    from factlattice.profile import load_corpus_config
    f = write_registry(w, {"named_roots": {"data": {"path": str(w / "corpora")}}, "c": {"roots": [{"path": "${data}/c"}]}}, monkeypatch)
    want = {"c": {"roots": [{"path": str(w / "corpora" / "c")}]}}
    assert R.resolve_registry().corpora == want
    assert R.entries_from(str(f)) == want
    assert R.read_registry_file(str(f))[0] == want
    assert R.resolved_entries(str(f)) == want
    assert load_corpus_config(str(f)) == want
    assert load_corpus_config() == want


def test_a_registry_with_no_table_comes_back_exactly_as_written(w, monkeypatch):
    from factlattice import registry as R
    raw = {"a": {"roots": [{"path": str(w / "corpora" / "a") + "/", "area": "specs"}], "domain_suffix": "d.a",
                 "profile": "relative-profile.lts", "x_note": "free text"},
           "b": {"root": str(w / "corpora" / "b")}}
    f = write_registry(w, raw, monkeypatch)
    assert R.resolve_registry().corpora == raw
    assert R.resolved_entries(str(f)) == raw


def test_the_form_is_recognised_only_at_the_start_of_the_value(w, monkeypatch):
    """A `${` further along is an ordinary character of an ordinary path: the corpus is built from it, with no failure."""
    from factlattice import registry as R
    literal = corpus(w / "corpora" / "${data}" / "x")
    write_registry(w, {"named_roots": {"data": {"path": str(w / "corpora")}}, "c": {"roots": [{"path": str(literal)}]}}, monkeypatch)
    entry = R.resolve_registry().corpora["c"]
    assert entry["roots"][0]["path"] == str(literal) and R.NAMED_FAILURE_KEY not in entry
    reply = listed(engine())
    assert "failed_corpora" not in reply and entry_of(reply, "c")["total_nodes"] == 1


def test_a_table_path_is_relative_to_the_registry_directory_and_tilde_to_home(w, monkeypatch):
    from factlattice import registry as R
    write_registry(w, {"named_roots": {"rel": {"path": "sub/dir"}, "home": {"path": "~"}, "hs": {"path": "~/hs/"}},
                       "a": {"root": "${rel}/x"}, "b": {"root": "${home}/y"}, "c": {"root": "${hs}"}}, monkeypatch)
    got = R.resolve_registry().corpora
    assert got["a"]["root"] == str(w / "cfg" / "sub" / "dir" / "x")
    assert got["b"]["root"] == str(w / "home" / "y")
    assert got["c"]["root"] == str(w / "home" / "hs")


def test_converting_a_registry_to_references_gives_byte_equal_entries(w, monkeypatch):
    """The rollout's own check ([MECH-NR-ROLLOUT] x-order 2 and 3), through the public function."""
    from factlattice import registry as R
    c = str(w / "corpora")
    plain = write_registry(w, {"a": {"roots": [{"path": c + "/a", "area": "specs"}], "domain_suffix": "d.a"}}, name="plain.json")
    conv = write_registry(w, {"named_roots": {"data": {"path": c}},
                              "a": {"roots": [{"path": "${data}/a", "area": "specs"}], "domain_suffix": "d.a"}}, name="conv.json")
    assert json.dumps(R.resolved_entries(str(plain))) == json.dumps(R.resolved_entries(str(conv)))


# ------------------------------------------------------------------------------------- [GRAM-NR-TABLE] x-refusals
def test_an_unknown_name_fails_that_corpus_only_and_says_so(w, monkeypatch):
    corpus(w / "corpora" / "ok")
    write_registry(w, {"named_roots": {"data": {"path": str(w / "corpora")}},
                       "ok": {"roots": [{"path": "${data}/ok"}]},
                       "bad": {"roots": [{"path": "${nope}/x"}]}}, monkeypatch)
    eng = engine()
    reply = listed(eng)
    assert entry_of(reply, "ok")["total_nodes"] == 1
    assert entry_of(reply, "bad") is None
    assert reply["failed_corpora"] == [{"corpus_id": "bad", "code": "named-root-unknown", "name": "nope"}]
    node = eng.find(corpus="bad", text="x")
    assert node["success"] is False and node["reason"] == "named-root-unknown" and node["name"] == "nope"


def test_no_table_at_all_with_a_reference_is_an_unknown_name(w, monkeypatch):
    write_registry(w, {"bad": {"root": "${nope}"}}, monkeypatch)
    assert listed(engine())["failed_corpora"][0]["code"] == "named-root-unknown"


@pytest.mark.parametrize("value", ["${data}foo", "${data", "${}", "${Data}/x", "${data}x/y"])
def test_anything_that_starts_with_the_form_and_is_not_it_fails_rather_than_reading_as_a_relative_path(w, monkeypatch, value):
    write_registry(w, {"named_roots": {"data": {"path": str(w / "corpora")}}, "bad": {"root": value}}, monkeypatch)
    assert listed(engine())["failed_corpora"][0]["code"] == "named-root-unknown"


MALFORMED = {
    "table-not-object": lambda w: ["x"],
    "root-not-object": lambda w: {"data": "/abs"},
    "bad-name": lambda w: {"Data": {"path": "/abs"}},
    "path-missing": lambda w: {"data": {"was": ["/old"]}},
    "path-bad": lambda w: {"data": {"path": "${other}/x"}, "other": {"path": "/abs"}},
    "path-user-tilde": lambda w: {"data": {"path": "~someone/x"}},
    "unknown-key": lambda w: {"data": {"path": "/abs", "privat": True}},
    "was-bad": lambda w: {"data": {"path": "/abs", "was": ["relative/old"]}},
    "was-not-list": lambda w: {"data": {"path": "/abs", "was": "/old"}},
    "was-equals-own-path": lambda w: {"data": {"path": "/abs/", "was": ["/abs"]}},
    "was-equals-other-path": lambda w: {"data": {"path": "/abs", "was": ["/old"]}, "b": {"path": "/old"}},
    "was-equals-home-spelled-path": lambda w: {"data": {"path": "~/x", "was": ["$HOME/x"]}},
    "duplicate-across-roots": lambda w: {"data": {"path": "/abs", "was": ["/old"]}, "b": {"path": "/abs2", "was": ["/old/"]}},
    "duplicate-paths": lambda w: {"data": {"path": "/abs"}, "b": {"path": "/abs"}},
    "local-not-in-was": lambda w: {"data": {"path": "/abs", "was": ["/old"], "local": ["/other"]}},
    "state-bad": lambda w: {"data": {"path": "/abs", "state": 5}},
    "state-unknown-name": lambda w: {"data": {"path": "/abs", "state": "${nope}/s"}},
    "private-not-bool": lambda w: {"data": {"path": "/abs", "private": "true"}},
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_a_malformed_table_fails_every_corpus_that_uses_a_name_and_no_other(w, monkeypatch, case):
    plain = corpus(w / "corpora" / "plain")
    write_registry(w, {"named_roots": MALFORMED[case](w),
                       "user": {"root": "${data}/x"},
                       "plain": {"roots": [{"path": str(plain)}]}}, monkeypatch)
    eng = engine()
    reply = listed(eng)
    assert entry_of(reply, "plain")["total_nodes"] == 1
    bad = reply["failed_corpora"]
    assert [b["corpus_id"] for b in bad] == ["user"] and bad[0]["code"] == "named-roots-malformed", case
    assert bad[0]["detail"]
    assert eng.find(corpus="user", text="x")["reason"] == "named-roots-malformed"


def test_a_well_formed_table_with_every_optional_key_and_note_keys_passes(w, monkeypatch):
    """The control for the malformed cases above: the same corpus shape, a valid table, and it serves."""
    data = corpus(w / "corpora" / "x")
    write_registry(w, {"named_roots": {
        "data": {"path": str(w / "corpora"), "was": ["/old/data", "~/data", "$HOME/data2"], "local": ["~/data"],
                 "state": "${st}/data", "private": True, "why_note": "free text"},
        "st": {"path": str(w / "corpora" / "st"), "was": ["/old/st"]},
        "section_note": "free text at the table level",
    }, "user": {"root": "${data}/x"}}, monkeypatch)
    reply = listed(engine())
    assert "failed_corpora" not in reply and entry_of(reply, "user")["root_paths"] == [str(data)]


def test_a_was_spelling_that_only_resolves_to_the_current_path_on_this_machine_is_not_malformed(w, monkeypatch):
    """[GRAM-NR-TABLE] x-local ignores such a spelling here; it is not a table error, or a registry would be valid or not by machine."""
    corpus(w / "home" / "x")
    write_registry(w, {"named_roots": {"data": {"path": str(w / "home" / "x"), "was": ["~/x"]}}, "user": {"root": "${data}"}}, monkeypatch)
    assert "failed_corpora" not in listed(engine())


def test_a_failed_private_corpus_is_counted_and_not_named(w, monkeypatch):
    write_registry(w, {"secret": {"root": "${nope}", "private": True}}, monkeypatch)
    reply = listed(engine())
    assert "failed_corpora" not in reply and reply["private_failed"] == 1
    assert "secret" not in json.dumps(reply) and "nope" not in json.dumps(reply)


def test_the_codes_and_the_public_functions_are_pinned(w):
    from factlattice import registry as R
    assert R.NAMED_ROOT_UNKNOWN == "named-root-unknown"
    assert R.NAMED_ROOTS_MALFORMED == "named-roots-malformed"
    assert R.NAMED_ROOTS_KEY == "named_roots"
    assert callable(R.resolved_entries)


def test_the_other_readers_of_an_entry_see_the_failure_through_the_one_definition(w, monkeypatch):
    from factlattice import registry as R
    from factlattice.authoring.contradiction import external_prefixes_from_registry
    write_registry(w, {"bad": {"root": "${nope}/x"}}, monkeypatch)
    corpora = R.resolve_registry().corpora
    assert R.entry_key_problems(corpora["bad"])[0]["code"] == "named-root-unknown"
    with pytest.raises(ValueError, match="named-root-unknown"):
        external_prefixes_from_registry(corpora, ["bad"])


def test_a_reader_of_an_entry_gets_the_expanded_path_for_membership(w, monkeypatch):
    from factlattice_engine.membership import check
    data = corpus(w / "corpora" / "m")
    write_registry(w, {"named_roots": {"data": {"path": str(w / "corpora")}}, "m": {"root": "${data}/m"}}, monkeypatch)
    assert check(str(data / "a.lts"))["corpora"] == ["m"]


# ------------------------------------------------------------------------------------- [MECH-NR-REGISTRY] x-digest
def sha(w, monkeypatch, data, *, cwd="elsewhere", root=None, home="home"):
    from factlattice import registry as R
    write_registry(w, data, monkeypatch)
    (w / cwd).mkdir(exist_ok=True)
    (w / home).mkdir(exist_ok=True)
    monkeypatch.chdir(w / cwd)
    monkeypatch.setenv("HOME", str(w / home))
    if root:
        monkeypatch.setenv("FACTLATTICE_CORPUS_ROOT", str(w / root))
    else:
        monkeypatch.delenv("FACTLATTICE_CORPUS_ROOT", raising=False)
    return R.resolve_registry().sha256


def test_the_hash_of_an_absolute_table_with_references_is_the_bytes_alone(w, monkeypatch):
    """A `${name}` value is not a relative path: if it were counted as one, a server and a plain shell would report different
    hashes for one registry and the reviewer would stop."""
    data = {"named_roots": {"data": {"path": "/abs/data", "was": ["/abs/old"]}}, "a": {"roots": [{"path": "${data}/a"}]}}
    one = sha(w, monkeypatch, data, cwd="c1", root="r1", home="h1")
    two = sha(w, monkeypatch, data, cwd="c2", root=None, home="h2")
    assert one == two == hashlib.sha256((w / "cfg" / "reg.json").read_bytes()).hexdigest()


@pytest.mark.parametrize("table", [
    {"data": {"path": "/abs", "was": ["~/old"]}},
    {"data": {"path": "/abs", "was": ["$HOME/old"]}},
    {"data": {"path": "/abs", "was": ["/x"], "local": ["/x"], "state": "~/st"}},
    {"data": {"path": "~/d"}},
])
def test_a_home_spelling_in_the_table_adds_home_and_not_the_working_directory(w, monkeypatch, table):
    data = {"named_roots": table, "a": {"root": "/abs/a"}}
    assert sha(w, monkeypatch, data, home="h1") != sha(w, monkeypatch, data, home="h2")
    assert sha(w, monkeypatch, data, cwd="c1", root="r1", home="h1") == sha(w, monkeypatch, data, cwd="c2", root="r2", home="h1")


def test_a_relative_table_path_is_hashed_with_its_base(w, monkeypatch):
    data = {"named_roots": {"data": {"path": "rel"}}, "a": {"root": "${data}/a"}}
    assert sha(w, monkeypatch, data, root="r1") != sha(w, monkeypatch, data, root="r2")


def test_the_hash_of_a_registry_with_no_table_is_unchanged(w, monkeypatch):
    data = {"a": {"root": "/abs/a"}}
    assert sha(w, monkeypatch, data) == hashlib.sha256((w / "cfg" / "reg.json").read_bytes()).hexdigest()


# ------------------------------------------------------------------------------------- [MECH-NR-REGISTRY] x-fix-tilde, x-fix-relative
def real_caller(w: Path, registry: Path, code: str, *, cwd: Path, home: Path, extra=None):
    """A fresh process with only the registry variable and a fake HOME: no harness environment, no monkeypatch."""
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "FACTLATTICE_CORPUS_CONFIG": str(registry),
           "FACTLATTICE_STATE_DIR": str(w / "st"), "PYTHONPATH": ENGINE_TREE, **(extra or {})}
    return subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120)


COUNT = ("import json; from factlattice_engine import config; from factlattice_engine.tools import LtsEngine;"
         "r=LtsEngine(config.build_store()).list_corpora(heal=False);"
         "print(json.dumps({c['corpus_id']: c['total_nodes'] for c in r['corpora']}))")


def test_a_tilde_root_loads_in_a_real_callers_process(w):
    corpus(w / "home" / "corp")
    f = write_registry(w, {"t": {"roots": [{"path": "~/corp"}]}})
    p = real_caller(w, f, COUNT, cwd=w / "elsewhere", home=w / "home")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout.splitlines()[-1]) == {"t": 1}


def test_a_relative_root_resolves_against_the_registry_file_and_not_the_working_directory(w):
    corpus(w / "cfg" / "rel")                                    # the one that must be found
    corpus(w / "elsewhere" / "rel", node="WRONG-ONE")            # the one a working-directory lookup would find
    f = write_registry(w, {"r": {"roots": [{"path": "rel"}]}, "s": {"root": "rel"}})
    p = real_caller(w, f, ("import json; from factlattice_engine import config; from factlattice_engine.tools import LtsEngine;"
                           "e=LtsEngine(config.build_store());"
                           "print(json.dumps([e.find(corpus=c, prefix='').get('nodes', [{}])[0].get('id') for c in ('r','s')]))"),
                    cwd=w / "elsewhere", home=w / "home")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout.splitlines()[-1]) == ["ALPHA-ONE", "ALPHA-ONE"]


def test_the_root_variable_still_beats_the_registry_directory(w, monkeypatch):
    from factlattice import registry as R
    write_registry(w, {"r": {"roots": [{"path": "rel"}]}}, monkeypatch)
    monkeypatch.setenv("FACTLATTICE_CORPUS_ROOT", str(w / "elsewhere"))
    assert R.resolve_registry().corpora["r"]["roots"][0]["path"] == str(w / "elsewhere" / "rel")


def test_the_example_registry_keeps_its_own_rule(w, monkeypatch):
    from factlattice import registry as R
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(w / "nothing"))
    choice = R.resolve_registry()
    assert choice.source == "example"
    assert choice.corpora["alpha"]["roots"][1]["path"].startswith(str(w / "elsewhere"))


def test_a_relative_profile_path_is_left_for_the_profile_lookup(w, monkeypatch):
    """x-fix-relative covers ROOT paths. A relative profile keeps its existing rule, read by the profile lookup."""
    from factlattice import registry as R
    write_registry(w, {"r": {"root": str(w / "corpora"), "profile": "p.lts"}}, monkeypatch)
    assert R.resolve_registry().corpora["r"]["profile"] == "p.lts"


# ------------------------------------------------------------------------------------- lts_register_corpus takes the same forms
def test_register_takes_a_reference_and_a_tilde_through_the_same_function(w, monkeypatch):
    data = corpus(w / "corpora" / "late")
    corpus(w / "home" / "hc")
    write_registry(w, {"named_roots": {"data": {"path": str(w / "corpora")}}}, monkeypatch)
    eng = engine()
    got = eng.register_corpus("late", {"roots": [{"path": "${data}/late"}]})
    assert got["success"] is True, got
    assert entry_of(listed(eng), "late")["root_paths"] == [str(data)]
    got = eng.register_corpus("hc", {"root": "~/hc"})
    assert got["success"] is True, got


def test_register_refuses_an_unknown_name_and_registers_nothing(w, monkeypatch):
    write_registry(w, {"named_roots": {"data": {"path": str(w / "corpora")}}}, monkeypatch)
    eng = engine()
    got = eng.register_corpus("late", {"root": "${nope}/late"})
    assert got["success"] is False and got["reason"] == "named-root-unknown"
    assert entry_of(listed(eng), "late") is None


# ------------------------------------------------------------------------------------- [MECH-NR-REGISTRY] x-health
def test_a_corpus_whose_every_root_is_missing_is_listed_not_vanished(w, monkeypatch):
    ok = corpus(w / "corpora" / "ok")
    gone = str(w / "corpora" / "gone")
    write_registry(w, {"ok": {"root": str(ok)},
                       "many": {"roots": [{"path": gone, "area": "specs"}, {"path": gone + "2", "area": "decisions"}],
                                "domain_suffix": "d.many"},
                       "single": {"root": gone + "3"}}, monkeypatch)
    reply = listed(engine())
    many = entry_of(reply, "many")
    assert many is not None, "the corpus vanished"
    assert many["root_health"]["status"] == "no-roots"
    assert [m["path"] for m in many["root_health"]["missing_roots"]] == [gone, gone + "2"]
    assert many["total_nodes"] == 0 and many["fresh"] is False and many["domain_suffix"] == "d.many"
    assert entry_of(reply, "single")["root_health"]["status"] == "no-roots"
    assert reply["count"] == 3
    assert reply["stale_corpora"] == []                                  # it is not stale: it has nothing to be stale about
    assert "many" not in reply["extent"]["corpora_consulted"]
    assert "root_health" not in entry_of(reply, "ok")


def test_every_root_missing_includes_an_absent_only_file_pin(w, monkeypatch):
    d = w / "corpora" / "pin"
    d.mkdir()
    write_registry(w, {"pin": {"roots": [{"path": str(d), "only_file": "absent.lts"}]}}, monkeypatch)
    assert entry_of(listed(engine()), "pin")["root_health"]["missing_roots"][0]["reason"] == "only-file-absent"


def test_an_existing_empty_root_is_not_no_roots(w, monkeypatch):
    """The control: a registered-but-empty project is not an error, and the status must not fire on it."""
    d = w / "corpora" / "empty"
    d.mkdir()
    write_registry(w, {"empty": {"roots": [{"path": str(d)}]}}, monkeypatch)
    assert entry_of(listed(engine()), "empty") is None


def test_a_private_corpus_with_no_roots_is_counted_and_not_named(w, monkeypatch):
    write_registry(w, {"secret": {"root": str(w / "gone"), "private": True}}, monkeypatch)
    reply = listed(engine())
    assert reply["private_failed"] == 1 and entry_of(reply, "secret") is None and "secret" not in json.dumps(reply)


def test_a_root_that_returns_stops_reading_as_no_roots_and_can_be_registered(w, monkeypatch):
    d = w / "corpora" / "back"
    write_registry(w, {"back": {"root": str(d)}}, monkeypatch)
    eng = engine()
    assert entry_of(listed(eng), "back")["root_health"]["status"] == "no-roots"
    corpus(d)
    health = entry_of(listed(eng), "back")["root_health"]
    assert health["status"] == "degraded" and [u["path"] for u in health["unindexed_roots"]] == [str(d)]
    got = eng.register_corpus("back")
    assert got["success"] is True, got
    after = entry_of(listed(eng), "back")
    assert after["total_nodes"] == 1 and "root_health" not in after


def test_a_registered_profile_at_a_missing_path_is_declared(w, monkeypatch):
    missing = str(w / "corpora" / "no-such-profile.lts")
    good = w / "corpora" / "good"
    corpus(good)
    write_registry(w, {"c": {"root": str(good), "profile": missing}}, monkeypatch)
    eng = engine()
    health = entry_of(listed(eng), "c")["root_health"]
    assert health["status"] == "degraded"
    assert {"path": missing, "area": None, "reason": "profile-missing"} in health["missing_roots"]
    notes = eng.find(corpus="c", prefix="")["extent"]["notes"]
    assert any("profile-missing" in n and missing in n for n in notes)


def test_a_registered_profile_that_exists_is_not_declared_missing(w, monkeypatch):
    good = corpus(w / "corpora" / "good2")
    prof = w / "corpora" / "profile.lts"
    prof.write_text('---\nlts: "1.1"\ntitle: P\ndoctype: PROFILE\n---\n', encoding="utf-8")
    write_registry(w, {"c": {"root": str(good), "profile": str(prof)}}, monkeypatch)
    assert "root_health" not in entry_of(listed(engine()), "c")


# ------------------------------------------------------------------------------------- python -m factlattice_engine registry resolved
def cli(w: Path, *args, registry_env=None):
    env = {"PATH": os.environ["PATH"], "HOME": str(w / "home"), "PYTHONPATH": ENGINE_TREE}
    return subprocess.run([sys.executable, "-m", "factlattice_engine", "registry", "resolved", *args],
                          cwd=str(w / "elsewhere"), env=env, capture_output=True, text=True, timeout=120)


def test_registry_resolved_prints_what_resolved_entries_returns(w):
    from factlattice import registry as R
    f = write_registry(w, {"named_roots": {"data": {"path": str(w / "corpora")}}, "a": {"roots": [{"path": "${data}/a"}]}})
    p = cli(w, "--file", str(f))
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout) == R.resolved_entries(str(f)) == {"a": {"roots": [{"path": str(w / "corpora" / "a")}]}}


def test_registry_resolved_exits_nonzero_when_an_entry_failed_and_still_prints_it(w):
    f = write_registry(w, {"bad": {"root": "${nope}"}, "ok": {"root": "/abs"}})
    p = cli(w, "--file", str(f))
    assert p.returncode == 1
    got = json.loads(p.stdout)
    assert got["ok"] == {"root": "/abs"} and got["bad"]["named_root_failure"]["code"] == "named-root-unknown"
    assert "bad" in p.stderr


def test_registry_resolved_reports_a_malformed_table_nothing_uses(w):
    f = write_registry(w, {"named_roots": {"Bad": {"path": "/abs"}}, "ok": {"root": "/abs"}})
    p = cli(w, "--file", str(f))
    assert p.returncode == 1 and "named-roots-malformed" in p.stderr


def test_registry_resolved_stops_on_a_missing_file(w):
    p = cli(w, "--file", str(w / "cfg" / "absent.json"))
    assert p.returncode == 2 and "registry-not-found" in p.stderr and p.stdout == ""


# ------------------------------------------------------------------------------------- the reviewer stops the same way the engine does
FAILED_ENTRIES = {
    "unknown-name": lambda data: ({"named_roots": {"data": {"path": data}}, "sec": {"roots": [{"path": "${dta}/priv"}], "private": True}},
                                  "named-root-unknown"),
    "malformed-table": lambda data: ({"named_roots": {"Data": {"path": data}}, "sec": {"roots": [{"path": "${data}/priv"}], "private": True}},
                                     "named-roots-malformed"),
    "not-marked-private": lambda data: ({"named_roots": {"data": {"path": data}}, "sec": {"roots": [{"path": "${dta}/priv"}]}},
                                        "named-root-unknown"),
}


@pytest.mark.parametrize("case", sorted(FAILED_ENTRIES))
def test_an_entry_whose_location_is_unknowable_refuses_routing_for_any_document(w, monkeypatch, case):
    """A document inside a nested private corpus, named under the public corpus that contains it. An entry that failed on a named
    root has no location to test the document against, so it may hold this document: nothing is routed to the shared root."""
    from factlattice import registry as R
    from factlattice_engine.reviewer import RESOLVE_REFUSALS, SiteConfig, resolve_output
    pub = corpus(w / "corpora" / "pub")
    doc = corpus(pub / "priv", node="SECRET-ONE") / "a.lts"
    table, code = FAILED_ENTRIES[case](str(w / "corpora"))
    write_registry(w, {**table, "pub": {"root": str(pub)}}, monkeypatch)
    corpora = R.resolve_registry().corpora
    site = SiteConfig("ok", "ref", str(w / "out"), ["pub"], [], [])
    got = resolve_output("pub", str(doc), corpora["pub"], None, site, all_corpora=corpora)
    assert got.root is None and got.reason == code and code in RESOLVE_REFUSALS
    named = resolve_output("sec", str(doc), corpora["sec"], None, site, all_corpora=corpora)
    assert named.root is None and named.reason == code


@pytest.mark.parametrize("private", [True, False])
def test_the_same_document_is_routed_or_refused_as_before_when_the_name_resolves(w, monkeypatch, private):
    """The control for the case above: the same layout and a good name. Public: the shared root. Private: the existing refusal."""
    from factlattice import registry as R
    from factlattice_engine.reviewer import SiteConfig, resolve_output
    pub = corpus(w / "corpora" / "pub")
    doc = corpus(pub / "priv", node="SECRET-ONE") / "a.lts"
    (w / "out").mkdir()
    write_registry(w, {"named_roots": {"data": {"path": str(w / "corpora")}},
                       "sec": {"roots": [{"path": "${data}/pub/priv"}], "private": private}, "pub": {"root": str(pub)}}, monkeypatch)
    corpora = R.resolve_registry().corpora
    got = resolve_output("pub", str(doc), corpora["pub"], None, SiteConfig("ok", "ref", str(w / "out"), ["pub"], [], []), all_corpora=corpora)
    if private:
        assert got.root is None and got.reason == "document-in-private-corpus"
    else:
        assert got.root == os.path.realpath(str(w / "out")), got


def test_the_reviewer_helper_names_the_right_error_class(w):
    from factlattice_engine.reviewer import __main__ as M
    assert M.REGISTRY_KEY_EXPLANATIONS["named-root-unknown"]
    assert "named-root-unknown" in M.HELPER_REASON_CODES and "named-roots-malformed" in M.HELPER_REASON_CODES
    assert M._error_class_for("named-root-unknown") == "NamedRootError"
    assert M._error_class_for("named-roots-malformed") == "NamedRootError"
    assert M._error_class_for("registry-entry-unknown-key") == "RegistryEntryKeyError"
    assert M._error_class_for("corpus-file-unreadable") == "CorpusFileError"


def test_with_no_corpus_id_the_refusal_names_no_corpus_and_no_name(w):
    from factlattice_engine.tools import _corpus_file_refusal
    failed = {"code": "named-root-unknown", "path": None, "private": True,
              "problems": [{"scope": "entry", "key": "secretname", "name": "secretname", "nearest": None, "code": "named-root-unknown"}]}
    got = _corpus_file_refusal(None, failed)
    assert got["reason"] == "named-root-unknown" and got["error_class"] == "NamedRootError"
    assert "secretname" not in json.dumps(got) and "corpus" not in got


# ------------------------------------------------------------------------------------- round 2, from the first cold read
@pytest.mark.parametrize("value", ["${data}//etc/zz", "${data}//", "${data}/../up", "${data}/a/../../up", "${data}/..", "${data}\n", "${data}/x\n"])
def test_a_reference_cannot_leave_its_root(w, monkeypatch, value):
    from factlattice import registry as R
    f = write_registry(w, {"named_roots": {"data": {"path": str(w / "corpora")}}, "bad": {"root": value}}, monkeypatch)
    got = R.resolved_entries(str(f))["bad"]
    assert got["root"] == value and got[R.NAMED_FAILURE_KEY] == {"code": "named-root-unknown", "detail": "bad-reference"}


@pytest.mark.parametrize("value", ["${Data}/x", "${data}foo", "${data", "${}"])
def test_a_reference_that_is_not_the_form_reports_no_part_of_its_value(w, monkeypatch, value):
    write_registry(w, {"bad": {"root": value}}, monkeypatch)
    assert listed(engine())["failed_corpora"] == [{"corpus_id": "bad", "code": "named-root-unknown", "detail": "bad-reference"}]


def test_a_table_name_with_a_trailing_newline_is_malformed(w, monkeypatch):
    from factlattice import registry as R
    f = write_registry(w, {"named_roots": {"bad\n": {"path": "/abs"}}}, monkeypatch)
    assert R.read_table(str(f))["problem"]["detail"] == "bad-name"


def test_a_tilde_profile_expands(w, monkeypatch):
    from factlattice import registry as R
    write_registry(w, {"r": {"root": str(w / "corpora"), "profile": "~/p.lts"}}, monkeypatch)
    assert R.resolve_registry().corpora["r"]["profile"] == str(w / "home" / "p.lts")


def test_building_from_the_boot_registry_reads_the_file_once(w, monkeypatch):
    """[INV-NR-ONE-TABLE]: an entry that already failed is not expanded again from a second read of the file."""
    from factlattice import registry as R
    from factlattice_engine import config
    write_registry(w, {"bad": {"root": "${nope}"}, "worse": {"root": "${no}"}}, monkeypatch)
    corpora = R.resolve_registry().corpora
    reads = []
    real = R._read_raw
    monkeypatch.setattr(R, "_read_raw", lambda *a, **k: (reads.append(a), real(*a, **k))[1])
    store = config.build_store(corpora)
    assert reads == [] and sorted(store._failed_corpora) == ["bad", "worse"]


def test_a_no_roots_corpus_named_directly_says_why(w, monkeypatch):
    gone = str(w / "corpora" / "gone")
    write_registry(w, {"nr": {"root": gone}}, monkeypatch)
    got = engine().find(corpus="nr", text="x")
    assert got["success"] is False and got["reason"] == "no-roots" and got["corpus"] == "nr"
    assert got["missing_roots"][0]["path"] == gone


def test_private_no_roots_does_not_change_the_freshness_a_public_one_leaves_alone(w, monkeypatch):
    """One listing, one rule: neither kind is a stale or failed corpus for `extent.index.fresh`."""
    write_registry(w, {"pubnr": {"root": str(w / "a")}, "secnr": {"root": str(w / "b"), "private": True}}, monkeypatch)
    reply = listed(engine())
    assert reply["private_failed"] == 1 and reply["extent"]["index"]["fresh"] is True


# ------------------------------------------------------------------------------------- round 3: one test for "this entry's location cannot be read"
UNKNOWABLE = {
    "misspelt-root-key": {"pth": "${data}/pub/priv", "private": True},
    "misspelt-root-key-in-roots": {"roots": [{"pth": "${data}/pub/priv"}], "private": True},
    "misspelt-roots-key": {"rots": [{"path": "${data}/pub/priv"}], "private": True},
    "misspelt-private-too": {"pth": "${data}/pub/priv", "privat": True},
    "unknown-name": {"root": "${dta}/pub/priv", "private": True},
}


@pytest.mark.parametrize("case", sorted(UNKNOWABLE))
def test_any_failed_entry_that_writes_a_location_as_a_reference_refuses_routing(w, monkeypatch, case):
    from factlattice import registry as R
    from factlattice_engine.reviewer import SiteConfig, resolve_output
    pub = corpus(w / "corpora" / "pub")
    doc = corpus(pub / "priv", node="SECRET-ONE") / "a.lts"
    write_registry(w, {"named_roots": {"data": {"path": str(w / "corpora")}}, "sec": UNKNOWABLE[case], "pub": {"root": str(pub)}}, monkeypatch)
    corpora = R.resolve_registry().corpora
    got = resolve_output("pub", str(doc), corpora["pub"], None, SiteConfig("ok", "ref", str(w / "out"), ["pub"], [], []), all_corpora=corpora)
    assert got.root is None and got.reason


def dangles(w: Path, registry: Path, *args):
    env = {"PATH": os.environ["PATH"], "HOME": str(w / "home"), "FACTLATTICE_CORPUS_CONFIG": str(registry),
           "FACTLATTICE_STATE_DIR": str(w / "st"), "PYTHONPATH": ENGINE_TREE}
    return subprocess.run([sys.executable, "-m", "factlattice_engine.reviewer", "dangles", *args], cwd=str(w / "elsewhere"),
                          env=env, capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("sec,code", [({"root": "${dta}/pub/priv", "private": True}, "named-root-unknown"),
                                      ({"pth": "${data}/pub/priv", "private": True}, "registry-entry-unknown-key")])
@pytest.mark.parametrize("how", [("--own-corpus",), ("--corpus", "pub")])
def test_dangles_stops_with_the_code_for_an_entry_whose_location_cannot_be_read(w, sec, code, how):
    pub = corpus(w / "corpora" / "pub")
    doc = corpus(pub / "priv", node="SECRET-ONE") / "a.lts"
    f = write_registry(w, {"named_roots": {"data": {"path": str(w / "corpora")}}, "sec": sec, "pub": {"root": str(pub)}})
    p = dangles(w, f, *how, "--document", str(doc))
    assert p.returncode == 2, p.stdout + p.stderr
    assert json.loads(p.stdout)["reason"] == code
    assert "sec" not in json.loads(p.stdout).get("explanation", "")


def test_dangles_control_with_a_good_name_runs(w):
    pub = corpus(w / "corpora" / "pub")
    doc = corpus(pub / "priv", node="SECRET-ONE") / "a.lts"
    f = write_registry(w, {"named_roots": {"data": {"path": str(w / "corpora")}}, "sec": {"root": "${data}/other"}, "pub": {"root": str(pub)}})
    corpus(w / "corpora" / "other", node="OTHER-ONE")
    p = dangles(w, f, "--corpus", "pub", "--document", str(doc))
    got = json.loads(p.stdout)                      # the lint ran and reported on the document: it was not stopped by a code
    assert "reason" not in got and got["complete"] is True, p.stdout + p.stderr


def test_a_private_no_roots_reply_carries_no_path_and_a_public_one_does(w, monkeypatch):
    gone = str(w / "corpora" / "gone")
    write_registry(w, {"sec": {"root": gone, "private": True}, "pub": {"root": gone + "2"}}, monkeypatch)
    eng = engine()
    secret = eng.find(corpus="sec", text="x")
    assert secret["reason"] == "no-roots" and "missing_roots" not in secret and gone not in json.dumps(secret)
    assert eng.find(corpus="pub", text="x")["missing_roots"][0]["path"] == gone + "2"


def test_preview_names_a_no_roots_corpus_as_no_roots(w, monkeypatch):
    from factlattice_engine import tools
    write_registry(w, {"nr": {"root": str(w / "gone")}}, monkeypatch)
    got = tools.dispatch("lts_preview", {"corpus": "nr", "content": "# X [X-ONE]\n"}, tools=engine())
    assert got.get("reason") == "no-roots", got


@pytest.mark.parametrize("value", ["${data}/a\rb", "${data}/a\x00b", "${data}/a\x7fb", "${data}/a\tb"])
def test_a_reference_with_a_control_character_is_not_the_form(w, monkeypatch, value):
    from factlattice import registry as R
    f = write_registry(w, {"named_roots": {"data": {"path": str(w / "corpora")}}, "bad": {"root": value}}, monkeypatch)
    assert R.resolved_entries(str(f))["bad"][R.NAMED_FAILURE_KEY]["detail"] == "bad-reference"


def test_the_refusal_explains_that_the_entry_may_hold_the_document(w):
    from factlattice_engine.reviewer import RESOLVE_REFUSALS
    for code in ("named-root-unknown", "named-roots-malformed"):
        assert "may hold this document" in RESOLVE_REFUSALS[code]


# ------------------------------------------------------------------------------------- round 4: routing does not test a failed entry's location at all
READABLE_ONLY_TO_ITS_AUTHOR = {
    "relative-to-registry": {"pth": "../corpora/pub/priv", "private": True},
    "home-spelled": {"pth": "$HOME/pub/priv", "private": True},
    "zero-width-prefixed-reference": {"pth": "\u200b${data}/pub/priv", "private": True},
    "bom-prefixed-reference": {"pth": "\ufeff${data}/pub/priv", "private": True},
    "quoted-reference": {"pth": "'${data}/pub/priv'", "private": True},
    "file-url": {"pth": "file:///nowhere/pub/priv", "private": True},
    "no-braces": {"pth": "$data/pub/priv", "private": True},
    "misspelt-private-only": {"root": "/nowhere/else", "privat": True},
    "unrelated-public-typo": {"root": "/nowhere/else", "oops": 1},
}


@pytest.mark.parametrize("case", sorted(READABLE_ONLY_TO_ITS_AUTHOR))
def test_routing_refuses_while_any_entry_failed_however_its_location_is_spelt(w, monkeypatch, case):
    """A scan of an unknown key's strings cannot be complete; each round found the next spelling. Routing a review to a shared root
    is the act that cannot be undone, so no spelling is consulted: a failed entry stops it for every document."""
    from factlattice import registry as R
    from factlattice_engine.reviewer import SiteConfig, resolve_output
    pub = corpus(w / "corpora" / "pub")
    doc = corpus(pub / "priv", node="SECRET-ONE") / "a.lts"
    plain = pub / "a.lts"
    write_registry(w, {"named_roots": {"data": {"path": str(w / "corpora")}}, "sec": READABLE_ONLY_TO_ITS_AUTHOR[case], "pub": {"root": str(pub)}}, monkeypatch)
    corpora = R.resolve_registry().corpora
    site = SiteConfig("ok", "ref", str(w / "out"), ["pub"], [], [])
    for d in (doc, plain):
        got = resolve_output("pub", str(d), corpora["pub"], None, site, all_corpora=corpora)
        assert got.root is None and got.reason == "registry-entry-unknown-key", (d, got)


def test_routing_control_with_no_failed_entry_routes(w, monkeypatch):
    from factlattice import registry as R
    from factlattice_engine.reviewer import SiteConfig, resolve_output
    pub = corpus(w / "corpora" / "pub")
    (w / "out").mkdir()
    write_registry(w, {"sec": {"root": "/nowhere/else", "a_note": "${x} in a note on a healthy entry"}, "pub": {"root": str(pub)}}, monkeypatch)
    corpora = R.resolve_registry().corpora
    got = resolve_output("pub", str(pub / "a.lts"), corpora["pub"], None, SiteConfig("ok", "ref", str(w / "out"), ["pub"], [], []), all_corpora=corpora)
    assert got.root == os.path.realpath(str(w / "out")), got


def test_all_four_codes_say_the_failed_entry_may_hold_the_document(w):
    from factlattice_engine.reviewer import RESOLVE_REFUSALS
    for code in ("named-root-unknown", "named-roots-malformed", "registry-entry-unknown-key", "registry-root-unknown-key"):
        assert "may hold this document" in RESOLVE_REFUSALS[code], code


def test_dangles_scan_sees_a_reference_behind_leading_whitespace(w):
    pub = corpus(w / "corpora" / "pub")
    doc = corpus(pub / "priv", node="SECRET-ONE") / "a.lts"
    f = write_registry(w, {"sec": {"pth": "  ${data}/pub/priv", "private": True}, "pub": {"root": str(pub)}})
    p = dangles(w, f, "--corpus", "pub", "--document", str(doc))
    assert p.returncode == 2 and json.loads(p.stdout)["reason"] == "registry-entry-unknown-key", p.stdout


def test_the_miss_log_guard_fails_closed_while_a_failed_entry_location_is_unknown(w, monkeypatch):
    """The state folder may lie inside a failed entry's folder, which `registered_roots` cannot see."""
    from factlattice_engine import tools
    pub = corpus(w / "corpora" / "pub")
    write_registry(w, {"sec": {"root": "${dta}/x", "private": True}, "pub": {"root": str(pub)}}, monkeypatch)
    got = tools.dispatch("lts_report_miss", {"corpus": "pub", "query": "q", "expected_ids": ["ALPHA-ONE"], "why": "w"}, tools=engine())
    assert got["success"] is False and got["reason"] == "corpus-roots-unknown", got
    assert not (w / "st" / "miss-log.jsonl").exists()


def test_the_miss_log_guard_control_with_no_failed_entry_writes(w, monkeypatch):
    from factlattice_engine import tools
    pub = corpus(w / "corpora" / "pub")
    write_registry(w, {"pub": {"root": str(pub)}}, monkeypatch)
    got = tools.dispatch("lts_report_miss", {"corpus": "pub", "query": "q", "expected_ids": ["ALPHA-ONE"], "why": "w"}, tools=engine())
    assert got["success"] is True, got


# ------------------------------------------------------------------------------------- round 5, from the cold read of 8c77da1
NOT_WHERE_MEANT = {
    "home-literal": lambda w: {"root": "$HOME/pub/priv", "private": True},
    "leading-space-reference": lambda w: {"root": " ${data}/pub/priv", "private": True},
    "wrong-table-path": lambda w: {"root": "${gone}/priv", "private": True},
    "typo-in-absolute-root": lambda w: {"roots": [{"path": str(w / "corpora" / "pubb" / "priv")}], "private": True},
    "no-root-at-all": lambda w: {"domain_suffix": "d.sec", "private": True},
    "empty-root": lambda w: {"root": "", "private": True},
    "personality-id-missing-root": lambda w: {"root": str(w / "gone")},
}


@pytest.mark.parametrize("case", sorted(NOT_WHERE_MEANT))
def test_a_private_entry_with_no_readable_location_stops_routing(w, monkeypatch, case):
    """A clean entry whose root is not where its author meant: the engine declares it, and the document it was meant to hold would
    otherwise be judged by the public corpus around it."""
    from factlattice import registry as R
    from factlattice_engine.reviewer import SiteConfig, resolve_output
    pub = corpus(w / "corpora" / "pub")
    doc = corpus(pub / "priv", node="SECRET-ONE") / "a.lts"
    sec = "sec-personality" if case.startswith("personality") else "sec"
    write_registry(w, {"named_roots": {"data": {"path": str(w / "corpora")}, "gone": {"path": str(w / "nowhere")}},
                       sec: NOT_WHERE_MEANT[case](w), "pub": {"root": str(pub)}}, monkeypatch)
    corpora = R.resolve_registry().corpora
    got = resolve_output("pub", str(doc), corpora["pub"], None, SiteConfig("ok", "ref", str(w / "out"), ["pub"], [], []), all_corpora=corpora)
    assert got.root is None and got.reason == "private-corpus-location-unknown", got


def test_a_private_entry_whose_root_exists_elsewhere_does_not_stop_routing(w, monkeypatch):
    from factlattice import registry as R
    from factlattice_engine.reviewer import SiteConfig, resolve_output
    pub = corpus(w / "corpora" / "pub")
    sec = corpus(w / "corpora" / "sec")
    (w / "out").mkdir()
    write_registry(w, {"sec": {"root": str(sec), "private": True}, "pub": {"root": str(pub)}}, monkeypatch)
    corpora = R.resolve_registry().corpora
    got = resolve_output("pub", str(pub / "a.lts"), corpora["pub"], None, SiteConfig("ok", "ref", str(w / "out"), ["pub"], [], []), all_corpora=corpora)
    assert got.root == os.path.realpath(str(w / "out")), got


def test_an_empty_document_does_not_skip_the_refusal(w, monkeypatch):
    from factlattice import registry as R
    from factlattice_engine.reviewer import SiteConfig, resolve_output
    pub = corpus(w / "corpora" / "pub")
    write_registry(w, {"bad": {"root": "/x", "oops": 1}, "pub": {"root": str(pub)}}, monkeypatch)
    corpora = R.resolve_registry().corpora
    got = resolve_output("pub", None, corpora["pub"], None, SiteConfig("ok", "ref", str(w / "out"), ["pub"], [], []), all_corpora=corpora)
    assert got.root is None and got.reason == "registry-entry-unknown-key", got


def test_the_miss_log_guard_fails_closed_for_an_unknown_key_too(w, monkeypatch):
    from factlattice_engine import tools
    pub = corpus(w / "corpora" / "pub")
    write_registry(w, {"sec": {"root": str(w / "corpora" / "x"), "oops": 1}, "pub": {"root": str(pub)}}, monkeypatch)
    got = tools.dispatch("lts_report_miss", {"corpus": "pub", "query": "q", "expected_ids": ["ALPHA-ONE"], "why": "w"}, tools=engine())
    assert got["success"] is False and got["reason"] == "corpus-roots-unknown", got


def test_the_miss_log_guard_fails_closed_for_a_corpus_file_fault_too(w, monkeypatch):
    from factlattice_engine import tools
    pub = corpus(w / "corpora" / "pub")
    fault = w / "corpora" / "fault"
    fault.mkdir()
    os.mkfifo(fault / "x.lts")
    write_registry(w, {"fault": {"root": str(fault)}, "pub": {"root": str(pub)}}, monkeypatch)
    eng = engine()
    assert "fault" in eng.store._failed_corpora
    got = tools.dispatch("lts_report_miss", {"corpus": "pub", "query": "q", "expected_ids": ["ALPHA-ONE"], "why": "w"}, tools=eng)
    assert got["success"] is False and got["reason"] == "corpus-roots-unknown", got


def test_dangles_scan_contains_a_document_under_a_misspelt_absolute_root(w):
    """The containment half of the scan, reached with no `${` in sight: `_path_in_untrusted_entry` is what stops this call."""
    pub = corpus(w / "corpora" / "pub")
    doc = corpus(pub / "priv", node="SECRET-ONE") / "a.lts"
    f = write_registry(w, {"sec": {"pth": str(pub / "priv")}, "pub": {"root": str(pub)}})
    p = dangles(w, f, "--corpus", "pub", "--document", str(doc))
    assert p.returncode == 2 and json.loads(p.stdout)["reason"] == "registry-entry-unknown-key", p.stdout


def test_dangles_scan_passes_a_document_outside_a_misspelt_root(w):
    pub = corpus(w / "corpora" / "pub")
    doc = pub / "a.lts"
    f = write_registry(w, {"sec": {"pth": str(w / "corpora" / "other")}, "pub": {"root": str(pub)}})
    got = json.loads(dangles(w, f, "--corpus", "pub", "--document", str(doc)).stdout)
    assert "reason" not in got and got["complete"] is True, got


def test_dangles_scan_refuses_every_document_for_an_entry_holding_a_nul(w):
    pub = corpus(w / "corpora" / "pub")
    f = write_registry(w, {"sec": {"pth": "/x\x00y"}, "pub": {"root": str(pub)}})
    p = dangles(w, f, "--corpus", "pub", "--document", str(pub / "a.lts"))
    assert p.returncode == 2 and json.loads(p.stdout)["reason"] == "registry-entry-unknown-key", p.stdout


def test_the_named_private_corpus_is_not_stopped_by_its_own_missing_root(w, monkeypatch):
    """The rule is about ANOTHER corpus that may hold the document. Naming the private corpus itself keeps the existing refusal."""
    from factlattice import registry as R
    from factlattice_engine.reviewer import SiteConfig, resolve_output
    doc = corpus(w / "corpora" / "d") / "a.lts"
    write_registry(w, {"sec": {"root": str(w / "gone"), "private": True}}, monkeypatch)
    corpora = R.resolve_registry().corpora
    got = resolve_output("sec", str(doc), corpora["sec"], None, SiteConfig("ok", "ref", str(w / "out"), ["sec"], [], []), all_corpora=corpora)
    assert got.reason != "private-corpus-location-unknown", got


def test_one_readable_root_of_a_private_entry_does_not_stop_routing(w, monkeypatch):
    """The stated limit: a mistyped second root is not detected."""
    from factlattice import registry as R
    from factlattice_engine.reviewer import SiteConfig, resolve_output
    pub = corpus(w / "corpora" / "pub")
    sec = corpus(w / "corpora" / "sec")
    (w / "out").mkdir()
    write_registry(w, {"sec": {"roots": [{"path": str(sec)}, {"path": str(w / "typo")}], "private": True}, "pub": {"root": str(pub)}}, monkeypatch)
    corpora = R.resolve_registry().corpora
    got = resolve_output("pub", str(pub / "a.lts"), corpora["pub"], None, SiteConfig("ok", "ref", str(w / "out"), ["pub"], [], []), all_corpora=corpora)
    assert got.root == os.path.realpath(str(w / "out")), got


def test_a_private_entry_of_a_shape_nothing_can_read_counts_as_unreadable(w):
    from factlattice_engine.reviewer import RESOLVE_REFUSALS, _private_location_unreadable
    assert _private_location_unreadable("x-personality", {"roots": 5}) is True
    assert _private_location_unreadable("pub", {"roots": 5}) is False
    assert "may hold this document" in RESOLVE_REFUSALS["private-corpus-location-unknown"]
