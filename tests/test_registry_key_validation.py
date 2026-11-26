"""Registry-key validation: an unknown registry key fails THAT CORPUS ONLY, loudly (maintainers' ruling of 2026-10-29).

The loader used to accept any key in a registry entry. A misspelt `private` left a corpus public, `profiles` loaded no profile,
`domain_sufix` fell back to a default suffix, `rooots` served nothing: each is a setting that is accepted and quietly absent.
Now the one schema (`factlattice.registry.REGISTRY_ENTRY_KEYS` / `REGISTRY_ROOT_KEYS`) is enforced where the engine builds a
corpus, exactly the way an unreadable member file already fails its corpus (`store._failed_corpora`, `lts_list_corpora`,
`extent.corpora_skipped`, a coded refusal on a corpus-scoped call). Any key ending `_note` is free text and is never read.

Every scenario that boots an engine runs in a CHILD process against a registry written under pytest's tmp_path.
Fixture families are invented (alpha/beta words only).
"""
from __future__ import annotations

import json
import os

import pytest

from tests.test_nonregular_file_in_root import REPO, sub_json

# Captured at import: the suite's autouse fixtures remove FACTLATTICE_* from a test's environment.
_LIVE_REGISTRY = os.environ.get("FACTLATTICE_TEST_LIVE_REGISTRY")

ENTRY_CODE, ROOT_CODE = "registry-entry-unknown-key", "registry-root-unknown-key"
GOOD = "# D [D-1]\n\n## N [N-1]\n\n- status: active\n"


@pytest.fixture
def rk(tmp_path, monkeypatch):
    base = tmp_path / "w"
    names = ("sibling", "notes", "tprivat", "tprofiles", "tsufix", "trooots", "tpth", "zzsecret")
    roots = {n: base / n for n in names}
    for n, r in roots.items():
        r.mkdir(parents=True)
        (r / "x.lts").write_text(f"# {n.upper()} [{n.upper()}-1]\n\n## Node [{n.upper()}-N]\n\n- status: active\n")

    def rt(n, **extra):
        return {"path": str(roots[n]), "area": "specs", **extra}

    reg = {
        "sibling": {"roots": [rt("sibling")], "domain_suffix": "p.s"},
        # control: note keys at entry level, an only_file / area root
        "notes": {"roots": [rt("notes", only_file="x.lts")], "domain_suffix": "p.n",
                  "profile_note": "free text", "placement_note": "more free text", "x_note": {"any": ["json"]}},
        "tprivat": {"roots": [rt("tprivat")], "domain_suffix": "p.a", "privat": True},
        "tprofiles": {"roots": [rt("tprofiles")], "domain_suffix": "p.b", "profiles": "/nonexistent/p.lts"},
        "tsufix": {"roots": [rt("tsufix")], "domain_sufix": "p.c"},
        "trooots": {"rooots": [rt("trooots")], "domain_suffix": "p.d"},
        "tpth": {"roots": [{"pth": str(roots["tpth"]), "area": "specs"}], "domain_suffix": "p.e"},
        "zzsecret-personality": {"roots": [rt("zzsecret")], "domain_suffix": "p.z", "profiles": "/nonexistent/p.lts"},
    }
    regfile = base / "reg.json"
    regfile.write_text(json.dumps(reg))
    state = tmp_path / "st"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTLATTICE_", "LTS_ENGINE_"))}
    env.update({"PYTHONPATH": str(REPO), "FACTLATTICE_STATE_DIR": str(state), "LTS_ENGINE_STATE_DIR": str(state),
                "FACTLATTICE_BACKUP_DIR": str(tmp_path / "bak"), "FACTLATTICE_CODE_WATCH": "0",
                "FACTLATTICE_QUERY_LOG": str(tmp_path / "ql.jsonl"), "FACTLATTICE_CORPUS_CONFIG": str(regfile),
                "FACTLATTICE_PROFILE_DIR": str(tmp_path / "noprofiles")})
    monkeypatch.chdir(tmp_path)
    return {"base": base, "roots": roots, "env": env, "reg": regfile, "tmp": tmp_path}


PROBE = """
import json, sys
from factlattice_engine import tools
from factlattice_engine.server import build_tools
eng = build_tools(watch=False)
D = lambda n, a: tools.dispatch(n, a, tools=eng)
out = {"package": __import__("factlattice").__file__}
out["list"] = D("lts_list_corpora", {})
for c in ("sibling", "notes", "tprivat", "tprofiles", "tsufix", "trooots", "tpth", "zzsecret-personality"):
    out["find_" + c] = D("lts_find", {"corpus": c, "text": "Node"})
out["preview_tprofiles"] = D("lts_preview", {"corpus": "tprofiles", "content": "# T [T-1]\\n"})
out["omitted"] = D("lts_find", {"text": "Node"})
out["aggregate"] = D("lts_aggregate", {"op": "count"})
out["profile_audit"] = D("lts_profile_audit", {})
print(json.dumps(out, default=str))
"""


@pytest.fixture
def probe(rk):
    out, _ = sub_json(PROBE, rk["env"], timeout=60)
    assert out["package"].startswith(str(REPO)), out["package"]          # the child imported THIS tree
    return out


# ---------------------------------------------------------------- planted typos: each fails only its corpus
TYPOS = [  # corpus, code, key, nearest
    ("tprivat", ENTRY_CODE, "privat", "private"),
    ("tprofiles", ENTRY_CODE, "profiles", "profile"),
    ("tsufix", ENTRY_CODE, "domain_sufix", "domain_suffix"),
    ("trooots", ENTRY_CODE, "rooots", "roots"),
    ("tpth", ROOT_CODE, "pth", "path"),
]


@pytest.mark.parametrize("corpus,code,key,nearest", TYPOS)
def test_a_planted_typo_fails_only_its_corpus_and_names_the_nearest_key(probe, corpus, code, key, nearest):
    r = probe["find_" + corpus]
    assert r["success"] is False and r["reason"] == code, r
    assert corpus in r["error"] and repr(key) in r["error"] and repr(nearest) in r["error"], r["error"]
    assert r.get("key") == key and r.get("nearest") == nearest, r
    # the sibling in the SAME registry still serves, as do the note-key control and the other healthy corpora
    assert probe["find_sibling"]["success"] is True and probe["find_sibling"]["count"] >= 1, probe["find_sibling"]
    assert probe["find_notes"]["success"] is True and probe["find_notes"]["count"] >= 1, probe["find_notes"]
    listed = [c["corpus_id"] for c in probe["list"]["corpora"]]
    assert "sibling" in listed and corpus not in listed, listed


def test_the_public_typos_are_listed_with_their_codes(probe):
    failed = {f["corpus_id"]: f for f in probe["list"]["failed_corpora"]}
    assert set(failed) == {"tprofiles", "tsufix", "trooots", "tpth"}, failed       # the private ones are NOT named here
    assert failed["tprofiles"]["code"] == ENTRY_CODE and failed["tprofiles"]["key"] == "profiles", failed
    assert failed["tpth"]["code"] == ROOT_CODE and failed["tpth"]["nearest"] == "path", failed
    skips = probe["list"]["extent"]["corpora_skipped"]
    for cid in failed:
        assert {"corpus": cid, "reason": failed[cid]["code"]} in skips, (cid, skips)


def test_note_keys_are_accepted_and_the_corpus_serves(probe):
    assert "notes" in [c["corpus_id"] for c in probe["list"]["corpora"]]
    assert all(f["corpus_id"] != "notes" for f in probe["list"].get("failed_corpora", []))   # a control: also true before the change


# ---------------------------------------------------------------- privacy: counted, never named
def test_a_private_corpus_with_an_unknown_key_is_counted_and_never_named(probe):
    lc = probe["list"]
    assert lc["private_failed"] == 2, lc                 # zzsecret-personality (private by id) and tprivat (`privat` ~ `private`)
    assert {"reason": "private-corpus-failed", "count": 2} in lc["extent"]["corpora_skipped"], lc["extent"]
    for name in ("list", "omitted", "aggregate", "profile_audit"):
        blob = json.dumps(probe[name])
        assert "zzsecret" not in blob, f"{name} names the failed private corpus"
        assert "tprivat" not in blob, f"{name} names the corpus whose `privat` key may have meant private"
    # named by the caller: the caller already knows the name, so the code is answered
    assert probe["find_zzsecret-personality"]["reason"] == ENTRY_CODE
    assert probe["find_tprivat"]["reason"] == ENTRY_CODE


def test_a_corpus_omitted_call_names_the_public_failures_and_still_serves_the_rest(probe):
    skipped = (probe["omitted"].get("extent") or {}).get("corpora_skipped") or []
    assert {"corpus": "tsufix", "reason": ENTRY_CODE} in skipped, skipped
    assert probe["omitted"]["success"] is True and probe["omitted"]["count"] >= 2, probe["omitted"]


def test_preview_on_a_failed_corpus_is_the_coded_refusal(probe):
    assert probe["preview_tprofiles"]["reason"] == ENTRY_CODE, probe["preview_tprofiles"]


# ---------------------------------------------------------------- lts_register_corpus
REGISTER = """
import json, sys, os
from factlattice_engine import tools
from factlattice_engine.server import build_tools
eng = build_tools(watch=False)
D = lambda n, a: tools.dispatch(n, a, tools=eng)
good = sys.argv[1]
out = {}
out["from_registry"] = D("lts_register_corpus", {"corpus_id": "tsufix"})                       # the entry on disk is still broken
out["bad_entry_spec"] = D("lts_register_corpus", {"corpus_id": "adhoc1", "spec": {"roots": [{"path": good}], "privat": True}})
out["bad_root_spec"] = D("lts_register_corpus", {"corpus_id": "adhoc2", "spec": {"roots": [{"pth": good}]}})
out["note_spec"] = D("lts_register_corpus", {"corpus_id": "adhoc3", "spec": {"roots": [{"path": good, "area": "a"}], "why_note": "x"}})
out["after"] = D("lts_list_corpora", {})
# the remedy: fix the entry on disk, then re-register by id (the failed entry goes)
reg = json.load(open(sys.argv[2]))
reg["tsufix"] = {"roots": reg["tsufix"]["roots"], "domain_suffix": "p.c"}
json.dump(reg, open(sys.argv[2], "w"))
out["fixed"] = D("lts_register_corpus", {"corpus_id": "tsufix"})
out["final"] = D("lts_list_corpora", {})
print(json.dumps(out, default=str))
"""


def test_register_corpus_refuses_unknown_keys_and_the_remedy_works(rk):
    from tests.test_nonregular_file_in_root import sub_run
    cp, _ = sub_run(["-c", REGISTER, str(rk["roots"]["sibling"]), str(rk["reg"])], rk["env"], timeout=60)
    assert cp.returncode == 0, cp.stderr[-2500:]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert r["from_registry"]["success"] is False and r["from_registry"]["reason"] == ENTRY_CODE, r["from_registry"]
    assert "domain_suffix" in r["from_registry"]["error"], r["from_registry"]
    assert r["bad_entry_spec"]["success"] is False and r["bad_entry_spec"]["reason"] == ENTRY_CODE, r["bad_entry_spec"]
    assert "'private'" in r["bad_entry_spec"]["error"], r["bad_entry_spec"]
    assert r["bad_root_spec"]["success"] is False and r["bad_root_spec"]["reason"] == ROOT_CODE, r["bad_root_spec"]
    assert r["note_spec"]["success"] is True, r["note_spec"]                       # a `_note` key is never refused
    ids = [c["corpus_id"] for c in r["after"]["corpora"]]
    assert "adhoc3" in ids and "adhoc1" not in ids and "adhoc2" not in ids, ids      # a refusal registers NOTHING
    assert any(f["corpus_id"] == "tsufix" for f in r["after"]["failed_corpora"])     # and does not erase the failed entry
    assert r["fixed"]["success"] is True, r["fixed"]
    assert all(f["corpus_id"] != "tsufix" for f in r["final"].get("failed_corpora", [])), r["final"].get("failed_corpora")


# ---------------------------------------------------------------- the reviewer behaves the same (it reads the same entries)
REVIEWER = """
import io, json, sys
from contextlib import redirect_stdout
from factlattice_engine.server import build_tools
from factlattice_engine.reviewer.__main__ import _failed_container_stop
from factlattice import registry
eng = build_tools(watch=False)
corpora = registry.resolve_registry().corpora
buf = io.StringIO()
with redirect_stdout(buf):
    rc = _failed_container_stop(eng, corpora, sys.argv[1])
rc_ok = _failed_container_stop(eng, corpora, sys.argv[2])
from factlattice_engine.reviewer.__main__ import _corpus_file_stop
buf2 = io.StringIO()
with redirect_stdout(buf2):                       # the NAMED-corpus route: the engine gate's refusal for the reviewed corpus
    rc_named = _corpus_file_stop(eng._gate("tsufix")[1])
print(json.dumps({"rc": rc, "stdout": buf.getvalue(), "rc_ok": rc_ok, "rc_named": rc_named, "named_stdout": buf2.getvalue()}))
"""


def test_the_reviewer_stops_with_the_precise_code_and_names_no_corpus(rk):
    from tests.test_nonregular_file_in_root import sub_run
    doc = rk["roots"]["tprivat"] / "x.lts"
    ok = rk["roots"]["sibling"] / "x.lts"
    cp, _ = sub_run(["-c", REVIEWER, str(doc), str(ok)], rk["env"], timeout=60)
    assert cp.returncode == 0, cp.stderr[-2500:]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert r["rc"] == 2 and r["rc_ok"] is None, r
    reply = json.loads(r["stdout"])
    assert reply["reason"] == ENTRY_CODE, reply
    assert "tprivat" not in r["stdout"], "a path-mode stop must not name the (possibly private) corpus"
    assert r["rc_named"] == 2 and json.loads(r["named_stdout"])["reason"] == ENTRY_CODE, r     # the named-corpus route stops the same way


# ---------------------------------------------------------------- the `call` CLI and the semantic CLI read the same entries
def _cli(rk, *argv):
    from tests.test_nonregular_file_in_root import sub_run
    cp, _ = sub_run(["-m", "factlattice_engine", *argv], rk["env"], timeout=90)
    return cp


def test_the_call_cli_answers_the_failed_corpus_with_its_code_and_serves_the_sibling(rk):
    bad = _cli(rk, "call", "lts_find", "--args-json", json.dumps({"corpus": "tsufix", "text": "Node"}), "--json")
    ok = _cli(rk, "call", "lts_find", "--args-json", json.dumps({"corpus": "sibling", "text": "Node"}), "--json")
    b, g = json.loads(bad.stdout), json.loads(ok.stdout)
    assert (b.get("result") or b).get("reason") == ENTRY_CODE, b
    assert (g.get("result") or g).get("success") is True, g


def test_the_semantic_cli_answers_the_failed_corpus_with_its_code(rk):
    cp = _cli(rk, "semantic", "Node", "--corpus", "tsufix", "--json")
    assert cp.returncode != 0 and ENTRY_CODE in cp.stdout, (cp.returncode, cp.stdout[-600:], cp.stderr[-600:])


def test_build_store_is_the_one_place_the_schema_is_enforced(tmp_path):
    """Every builder (server boot, call, reviewer, semantic CLI, read-profile, analytics) goes through `config.build_store`."""
    from factlattice import registry
    from factlattice_engine import config
    d = tmp_path / "r"
    d.mkdir()
    (d / "x.lts").write_text(GOOD)
    store = config.build_store({"fine": {"roots": [{"path": str(d)}], "n_note": "x"},
                                "typo": {"roots": [{"path": str(d)}], "domain_sufix": "p.x"}})
    assert [p.corpus_id for p in store.partitions()] == ["fine"]
    f = store._failed_corpora["typo"]
    assert f["code"] == registry.ENTRY_KEY_CODE and f["private"] is False and f["problems"][0]["nearest"] == "domain_suffix", f
    assert store._failed_corpora.keys() == {"typo"}


# ---------------------------------------------------------------- the schema: one place, one object
def test_the_checker_imports_the_registry_schema_it_does_not_define_its_own():
    from factlattice import registry
    from factlattice.authoring import contradiction
    assert contradiction.REGISTRY_ENTRY_KEYS is registry.REGISTRY_ENTRY_KEYS
    assert registry.REGISTRY_ENTRY_KEYS == frozenset(
        {"roots", "root", "domain_suffix", "profile", "private", "external_id_prefixes"})
    assert registry.REGISTRY_ROOT_KEYS == frozenset({"path", "area", "only_file"})
    assert contradiction.EXTERNAL_PREFIXES_KEY in registry.REGISTRY_ENTRY_KEYS


def test_the_helper_is_the_single_definition_of_what_a_problem_is():
    from factlattice import registry
    assert registry.entry_key_problems({"roots": [{"path": "/x"}], "a_note": 1, "_note": 2}) == []
    assert registry.entry_key_problems("not a dict") == []
    p = registry.entry_key_problems({"roots": [{"pth": "/x"}, "/plain/string/root"], "privat": True, "zzzz": 1})
    assert [(x["scope"], x["key"], x["nearest"]) for x in p] == [
        ("entry", "privat", "private"), ("entry", "zzzz", None), ("root", "pth", "path")], p
    assert {x["code"] for x in p} == {ENTRY_CODE, ROOT_CODE}


# ---------------------------------------------------------------- the live registry: 0 refusals, read-only, never copied here
@pytest.mark.skipif(not _LIVE_REGISTRY or not os.path.isfile(_LIVE_REGISTRY or ""),
                    reason="set FACTLATTICE_TEST_LIVE_REGISTRY to a registry file to check it read-only")
def test_a_real_deployment_registry_has_zero_refusals():
    from factlattice import registry
    with open(_LIVE_REGISTRY, "rb") as fh:                      # read-only; nothing is written or copied
        data = json.loads(fh.read().decode("utf-8"))
    assert data, "an empty registry would pass vacuously"
    bad = {cid: registry.entry_key_problems(e) for cid, e in data.items() if registry.entry_key_problems(e)}
    assert bad == {}, f"{len(bad)} entries refused"


def test_the_live_check_fires_on_a_planted_typo_in_a_copy_of_the_same_shape(tmp_path):
    """Negative control for the test above: the same helper, on a registry shaped like a deployment's, refuses a planted typo."""
    from factlattice import registry
    data = {"c1": {"roots": [{"path": "/a", "area": "specs", "only_file": "f.lts"}], "domain_suffix": "p.1", "profile": "/p",
                   "profile_note": "n"}}
    assert all(not registry.entry_key_problems(e) for e in data.values())
    data["c1"]["profil"] = "/p2"
    assert [x["key"] for x in registry.entry_key_problems(data["c1"])] == ["profil"]
