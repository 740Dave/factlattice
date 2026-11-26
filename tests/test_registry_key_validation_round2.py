"""Round 2 of registry-key validation (Diamond on a5a0a43: P1-1, P2-1, P2-3, P3-5).

P1-1  reviewer `resolve-output` applies the same key validation as `build_store`: a corpus whose entry or roots carry an unknown
      key resolves to NO output root with the same code, and a near-miss of `private` never routes to the shared default root.
P2-1  the contradiction checker uses the registry's validation (entry AND root level), not its own loop.
P2-3  a private-shaped key on a ROOT object makes the corpus private for naming purposes and fails it with the root code.
P3-5  a key ending `_NOTE` / `_Note` is refused with the hint "comment keys end in lowercase `_note`".
"""
from __future__ import annotations

import json

import pytest

from tests.test_nonregular_file_in_root import sub_json, sub_run
from tests.test_registry_key_validation import ENTRY_CODE, ROOT_CODE, rk  # noqa: F401


# ---------------------------------------------------------------- P1-1 reviewer resolve-output
def _site(root, listed):
    from factlattice_engine.reviewer import SiteConfig
    return SiteConfig("ok", "ref", str(root), list(listed), [], [])


def test_resolve_output_refuses_a_privat_corpus_instead_of_routing_it_to_the_default_root(tmp_path):
    from factlattice_engine.reviewer import resolve_output
    corp, out = tmp_path / "corp", tmp_path / "shared-out"
    corp.mkdir()
    out.mkdir()
    doc = str(corp / "x.lts")
    site = _site(out, ["c"])
    # controls, same site config: a clean corpus takes the default root; `private: true` needs an override
    spec0 = {"roots": [{"path": str(corp)}]}
    clean = resolve_output("c", doc, spec0, None, site, all_corpora={"c": spec0})
    assert clean.rule_used == "default" and clean.root, clean.to_dict()
    spec1 = {"roots": [{"path": str(corp)}], "private": True}
    assert resolve_output("c", doc, spec1, None, site, all_corpora={"c": spec1}).reason == \
        "private-corpus-needs-override"
    for spec, code in (({"roots": [{"path": str(corp)}], "privat": True}, ENTRY_CODE),                     # the Diamond case
                       ({"roots": [{"path": str(corp)}], "domain_sufix": "p"}, ENTRY_CODE),
                       ({"roots": [{"pth": str(corp), "path": str(corp)}]}, ROOT_CODE),
                       ({"roots": [{"path": str(corp), "private": True}]}, ROOT_CODE)):
        r = resolve_output("c", doc, spec, None, site, all_corpora={"c": spec})
        assert r.root is None and r.rule_used == "no-output-root" and r.reason == code, (spec, r.to_dict())
        assert r.explanation and "c" not in r.terminal_only


def test_resolve_output_accepts_note_keys(tmp_path):
    from factlattice_engine.reviewer import resolve_output
    corp, out = tmp_path / "corp", tmp_path / "shared-out"
    corp.mkdir()
    out.mkdir()
    spec = {"roots": [{"path": str(corp), "w_note": "x"}], "p_note": "y"}
    r = resolve_output("c", str(corp / "x.lts"), spec, None, _site(out, ["c"]), all_corpora={"c": spec})
    assert r.rule_used == "default", r.to_dict()


def test_the_resolve_output_cli_stops_with_the_code_and_names_no_corpus_on_a_document_only_call(rk):  # noqa: F811
    doc = rk["roots"]["tprivat"] / "x.lts"
    ok = rk["roots"]["sibling"] / "x.lts"
    cp, _ = sub_run(["-m", "factlattice_engine.reviewer", "resolve-output", "--document", str(doc)], rk["env"], timeout=90)
    out = json.loads(cp.stdout)
    assert out["reason"] == ENTRY_CODE and out["root"] is None, out
    assert "tprivat" not in cp.stdout, "an unnamed path must not name the (possibly private) corpus"
    cp2, _ = sub_run(["-m", "factlattice_engine.reviewer", "resolve-output", "--corpus", "tsufix", "--document", str(ok)],
                     rk["env"], timeout=90)
    assert json.loads(cp2.stdout)["reason"] == ENTRY_CODE, cp2.stdout
    # Changed by named roots S1: a healthy corpus's document is refused too while another entry has failed; nothing names that entry.
    cp3, _ = sub_run(["-m", "factlattice_engine.reviewer", "resolve-output", "--corpus", "sibling", "--document", str(ok)],
                     rk["env"], timeout=90)
    assert json.loads(cp3.stdout)["reason"] in (ENTRY_CODE, ROOT_CODE) and "tprivat" not in cp3.stdout, cp3.stdout


# ---------------------------------------------------------------- P2-1 the checker uses the registry's validation
PLANTED = [  # id, entry, scope
    ("privat", {"roots": [{"path": "/x"}], "privat": True}, "entry"),
    ("profiles", {"roots": [{"path": "/x"}], "profiles": "/p"}, "entry"),
    ("domain_sufix", {"roots": [{"path": "/x"}], "domain_sufix": "p"}, "entry"),
    ("rooots", {"rooots": [{"path": "/x"}]}, "entry"),
    ("pth", {"roots": [{"pth": "/x"}]}, "root"),
    ("root-private", {"roots": [{"path": "/x", "private": True}]}, "root"),
    ("profile_NOTE", {"roots": [{"path": "/x"}], "profile_NOTE": "x"}, "entry"),
    ("where_Note", {"roots": [{"path": "/x", "where_Note": "x"}]}, "root"),
]
CLEAN = [
    {"roots": [{"path": "/x", "area": "a", "only_file": "f.lts", "w_note": 1}], "domain_suffix": "p", "profile": "/p", "p_note": 1,
     "external_id_prefixes": ["ACME-"], "private": False},
    {"root": "/x"},
]


@pytest.mark.parametrize("name,entry,scope", PLANTED, ids=[p[0] for p in PLANTED])
def test_the_checker_and_the_engine_refuse_the_same_planted_typos(tmp_path, name, entry, scope):
    from factlattice import registry
    from factlattice.authoring import contradiction as C
    from factlattice_engine import config
    d = tmp_path / "r"
    d.mkdir()
    (d / "x.lts").write_text("# D [D-1]\n\n## N [N-1]\n\n- status: active\n")
    e = json.loads(json.dumps(entry).replace('"/x"', json.dumps(str(d))))
    assert registry.entry_key_problems(e), name                                     # the one definition
    with pytest.raises(ValueError):
        C.external_prefixes_from_registry({"c": e})                                 # the checker
    assert "c" in config.build_store({"c": e})._failed_corpora, name                 # the engine


@pytest.mark.parametrize("entry", CLEAN)
def test_the_checker_and_the_engine_accept_the_same_clean_entries(tmp_path, entry):
    from factlattice import registry
    from factlattice.authoring import contradiction as C
    assert registry.entry_key_problems(entry) == []
    C.external_prefixes_from_registry({"c": entry})


def test_the_checker_keeps_no_key_loop_of_its_own():
    import inspect

    from factlattice.authoring import contradiction as C
    src = inspect.getsource(C.external_prefixes_from_registry)
    assert "entry_key_problems" in src and "for k in entry" not in src


# ---------------------------------------------------------------- P2-3 private-shaped key on a root object
ROOTPRIV = """
import json, sys
from factlattice_engine import tools
from factlattice_engine.server import build_tools
eng = build_tools(watch=False)
D = lambda n, a: tools.dispatch(n, a, tools=eng)
out = {"list": D("lts_list_corpora", {}), "omitted": D("lts_find", {"text": "Node"}), "agg": D("lts_aggregate", {"op": "count"}),
       "named1": D("lts_find", {"corpus": "trootone", "text": "Node"}), "named2": D("lts_find", {"corpus": "troottwo", "text": "Node"})}
print(json.dumps(out, default=str))
"""


def test_a_private_shaped_key_on_a_root_object_is_never_named_and_fails_the_corpus(rk):  # noqa: F811
    reg = json.loads(rk["reg"].read_text())
    p = lambda n, **kw: {"roots": [{"path": str(rk["roots"][n]), "area": "specs", **kw}], "domain_suffix": "p." + n}
    reg = {"sibling": reg["sibling"], "trootone": p("tprivat", private=True), "troottwo": p("tsufix", privat=True)}
    rk["reg"].write_text(json.dumps(reg))
    out, _ = sub_json(ROOTPRIV, rk["env"], timeout=60)
    assert out["list"]["private_failed"] == 2 and not out["list"].get("failed_corpora"), out["list"]
    for name in ("list", "omitted", "agg"):
        blob = json.dumps(out[name])
        assert "trootone" not in blob and "troottwo" not in blob, f"{name} names a private-shaped corpus"
    assert out["named1"]["reason"] == ROOT_CODE and out["named2"]["reason"] == ROOT_CODE, (out["named1"], out["named2"])
    assert out["omitted"]["success"] is True and out["omitted"]["count"] >= 1          # the sibling still serves


# ---------------------------------------------------------------- P3-5 `_NOTE` hint
def test_a_wrong_case_note_key_is_refused_with_the_lowercase_hint(tmp_path):
    from factlattice.authoring import contradiction as C
    from factlattice.registry import entry_key_problems
    from factlattice_engine import config
    from factlattice_engine.tools import _registry_key_refusal
    e = {"roots": [{"path": str(tmp_path)}], "profile_NOTE": "x"}
    (p,) = entry_key_problems(e)
    assert p["nearest"] is None and p.get("note_case") is True, p
    f = config.registry_key_failure("c", e)
    msg = _registry_key_refusal("c", f)["error"]
    assert "comment keys end in lowercase `_note`" in msg and "nearest valid key" not in msg, msg
    with pytest.raises(ValueError, match="comment keys end in lowercase `_note`"):
        C.external_prefixes_from_registry({"c": e})
    # control: a real near-miss still gets its nearest key, and the exact suffix is still accepted
    assert entry_key_problems({"roots": [], "profiles": 1})[0]["nearest"] == "profile"
    assert entry_key_problems({"roots": [], "x_note": 1}) == []
