"""`resolve_output` must check EVERY registered corpus that contains the document for privacy, not only the named one.

THE DEFECT: `resolve-output --corpus OUTER --document <file inside a private corpus nested in OUTER's root>` consulted
only OUTER's privacy and answered `rule_used=default`: the shared review root. A private document could have its review
(or an UNREVIEWED record) written there, however the inner corpus spells private.

THE RULE (decided here): before any rule may choose a root, every registered corpus whose roots contain the document's
realpath is found. For each such corpus that is private (`privacy.private_reason`: the registry flag AND the
`-personality` suffix) and is NOT the named corpus, the document is routed only by an explicit override keyed to THAT
private corpus; otherwise `document-in-private-corpus`. A private corpus named directly keeps its existing behaviour
(its own override or profile; never the default root).
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from factlattice_engine.reviewer import RESOLVE_REFUSALS, output_folder, resolve_output
from tests.test_reviewer_resolve_output import _cli, _site

REPO = Path(__file__).resolve().parent.parent
INNER_FLAG = "zq-inner-flagged"
INNER_SUFFIX = "zq-inner-personality"


def _spec(root, **extra):
    return {"roots": [{"path": str(root), "area": "docs"}], "domain_suffix": "x", **extra}


@pytest.fixture
def n(tmp_path):
    """OUTER corpus `docs`, with a corpus root `inner` NESTED in it, a sibling `inner2` (a string-prefix trap), and
    three output roots."""
    outer = tmp_path / "corpora" / "docs"
    inner = outer / "inner"
    inner2 = outer / "inner2"
    for d in (inner, inner2):
        d.mkdir(parents=True)
    (outer / "top.lts").write_text("# x\n")
    (inner / "secret.lts").write_text("# x\n")
    (inner2 / "plain.lts").write_text("# x\n")
    out = {k: tmp_path / "out" / k for k in ("general", "special", "other")}
    for p in out.values():
        p.mkdir(parents=True)
    return {"tmp": tmp_path, "outer": outer, "inner": inner, "inner2": inner2, "out": out,
            "doc_in": str(inner / "secret.lts"), "doc_top": str(outer / "top.lts"),
            "doc_sib": str(inner2 / "plain.lts")}


def _corpora(n, inner_id, **inner_extra):
    return {"docs": _spec(n["outer"]), inner_id: _spec(n["inner"], **inner_extra)}


def _res(n, corpus, doc, corpora, site, spec=None):
    return resolve_output(corpus, os.path.realpath(doc), (corpora or {}).get(corpus) if spec is None else spec,
                          None, site, all_corpora=corpora)


def _default_site(n, listed="docs"):
    return _site(default=str(n["out"]["general"]), corpora=listed)


# ------------------------------------------------------------------ a private corpus nested in the named one, two spellings of private
@pytest.mark.parametrize("inner_id,extra", [(INNER_FLAG, {"private": True}), (INNER_SUFFIX, {})],
                         ids=["registry-flag", "personality-suffix"])
def test_a_private_corpus_nested_in_the_named_one_is_refused_not_routed_to_the_default(n, inner_id, extra):
    corpora = _corpora(n, inner_id, **extra)
    r = _res(n, "docs", n["doc_in"], corpora, _default_site(n))
    assert r.root is None and r.rule_used == "no-output-root"
    assert r.reason == "document-in-private-corpus" and r.explanation == RESOLVE_REFUSALS[r.reason]


def test_the_refusal_names_no_private_corpus_anywhere_in_its_reply(n):
    r = _res(n, "docs", n["doc_in"], _corpora(n, INNER_SUFFIX), _default_site(n))
    blob = json.dumps(r.to_dict()) + json.dumps(RESOLVE_REFUSALS["document-in-private-corpus"])
    assert "zq-inner" not in blob and "personality" not in blob.replace(output_folder("docs"), "")


def test_with_no_corpus_named_a_private_document_is_still_refused(n):
    r = _res(n, None, n["doc_in"], _corpora(n, INNER_SUFFIX), _default_site(n))
    assert r.root is None and r.reason == "document-in-private-corpus"


def test_the_outer_corpus_override_and_profile_cannot_route_a_private_document(n):
    site = _site(default=str(n["out"]["general"]), corpora="docs", overrides=[f"docs -> {n['out']['special']}"])
    r = _res(n, "docs", n["doc_in"], _corpora(n, INNER_SUFFIX), site)
    assert r.root is None and r.reason == "document-in-private-corpus"


# ------------------------------------------------------------------ the symlink route
def test_a_symlink_inside_the_outer_root_that_points_into_the_private_root_is_refused(n):
    link = n["outer"] / "link.lts"
    os.symlink(n["doc_in"], link)
    r = _res(n, "docs", str(link), _corpora(n, INNER_SUFFIX), _default_site(n))
    assert r.root is None and r.reason == "document-in-private-corpus"


def test_a_symlinked_directory_into_the_private_root_is_refused(n):
    os.symlink(n["inner"], n["outer"] / "viadir")
    r = _res(n, "docs", str(n["outer"] / "viadir" / "secret.lts"), _corpora(n, INNER_FLAG, private=True), _default_site(n))
    assert r.root is None and r.reason == "document-in-private-corpus"


def test_a_not_yet_written_file_under_the_private_root_is_refused(n):
    r = _res(n, "docs", str(n["inner"] / "new" / "later.lts"), _corpora(n, INNER_SUFFIX), _default_site(n))
    assert r.root is None and r.reason == "document-in-private-corpus"


# ------------------------------------------------------------------ the one way through: the private corpus's own override
def test_an_explicit_override_for_the_private_corpus_routes_it_to_that_root_only(n):
    site = _site(default=str(n["out"]["general"]), corpora="docs",
                 overrides=[f"docs -> {n['out']['other']}", f"{INNER_SUFFIX} -> {n['out']['special']}"])
    r = _res(n, "docs", n["doc_in"], _corpora(n, INNER_SUFFIX), site)
    assert r.rule_used == "override" and r.root == os.path.realpath(n["out"]["special"])
    assert INNER_SUFFIX not in json.dumps(r.to_dict())       # the folder is the NAMED corpus's, so no private id is echoed


def test_a_path_prefix_override_for_the_private_corpus_applies_only_under_its_prefix(n):
    (n["inner"] / "sub").mkdir()
    (n["inner"] / "sub" / "d.lts").write_text("# x\n")
    site = _site(default=str(n["out"]["general"]), corpora="docs", overrides=[f"{INNER_SUFFIX}:sub/ -> {n['out']['special']}"])
    corpora = _corpora(n, INNER_SUFFIX)
    assert _res(n, "docs", str(n["inner"] / "sub" / "d.lts"), corpora, site).root == os.path.realpath(n["out"]["special"])
    r = _res(n, "docs", n["doc_in"], corpora, site)             # in the private corpus, but not under `sub/`
    assert r.root is None and r.reason == "document-in-private-corpus"


def test_two_private_containers_need_agreeing_overrides(n):
    deep = n["inner"] / "deep"
    deep.mkdir()
    (deep / "d.lts").write_text("# x\n")
    corpora = {**_corpora(n, INNER_SUFFIX), "zq-deep-personality": _spec(deep)}
    doc = str(deep / "d.lts")
    only_one = _site(default=str(n["out"]["general"]), corpora="docs", overrides=[f"{INNER_SUFFIX} -> {n['out']['special']}"])
    assert _res(n, "docs", doc, corpora, only_one).reason == "document-in-private-corpus"
    differ = _site(overrides=[f"{INNER_SUFFIX} -> {n['out']['special']}", f"zq-deep-personality -> {n['out']['other']}"])
    assert _res(n, "docs", doc, corpora, differ).root is None
    agree = _site(overrides=[f"{INNER_SUFFIX} -> {n['out']['special']}", f"zq-deep-personality -> {n['out']['special']}"])
    assert _res(n, "docs", doc, corpora, agree).root == os.path.realpath(n["out"]["special"])


def test_a_private_override_root_inside_the_private_corpus_is_still_refused(n):
    site = _site(overrides=[f"{INNER_SUFFIX} -> {n['inner']}"])
    r = _res(n, "docs", n["doc_in"], _corpora(n, INNER_SUFFIX), site)
    assert r.root is None and r.reason == "override-root-inside-corpus"


# ------------------------------------------------------------------ controls: nothing else changes
def test_control_a_nested_non_private_inner_resolves_as_before(n):
    r = _res(n, "docs", n["doc_in"], _corpora(n, "zq-inner-public"), _default_site(n))
    assert r.rule_used == "default" and r.root == os.path.realpath(n["out"]["general"])


def test_control_private_false_in_the_registry_lifts_the_suffix(n):
    r = _res(n, "docs", n["doc_in"], _corpora(n, INNER_SUFFIX, private=False), _default_site(n))
    assert r.rule_used == "default"


def test_control_a_document_in_the_outer_root_but_outside_the_private_one_is_unaffected(n):
    r = _res(n, "docs", n["doc_top"], _corpora(n, INNER_SUFFIX), _default_site(n))
    assert r.rule_used == "default" and r.root == os.path.realpath(n["out"]["general"])


def test_control_containment_is_by_component_not_by_string_prefix(n):
    """`inner2` starts with the string `inner`; a private corpus rooted at `inner` does not contain it."""
    r = _res(n, "docs", n["doc_sib"], _corpora(n, INNER_SUFFIX), _default_site(n))
    assert r.rule_used == "default"


def test_control_a_private_corpus_named_directly_keeps_its_behaviour(n):
    corpora = _corpora(n, INNER_SUFFIX)
    # no override: the default root is still refused for it, with the EXISTING code
    r = _res(n, INNER_SUFFIX, n["doc_in"], corpora, _site(default=str(n["out"]["general"]), corpora=INNER_SUFFIX))
    assert r.root is None and r.reason == "private-corpus-needs-override"
    # its own override still routes it
    site = _site(default=str(n["out"]["general"]), corpora=INNER_SUFFIX, overrides=[f"{INNER_SUFFIX} -> {n['out']['special']}"])
    r2 = _res(n, INNER_SUFFIX, n["doc_in"], corpora, site)
    assert r2.rule_used == "override" and r2.root == os.path.realpath(n["out"]["special"])
    assert r2.folder == output_folder(INNER_SUFFIX)


def test_control_with_no_other_corpora_the_old_answers_stand(n):
    r = resolve_output("docs", n["doc_in"], _spec(n["outer"]), None, _default_site(n), all_corpora={})
    assert r.rule_used == "default"


# ------------------------------------------------------------------ the callers
def test_all_corpora_is_required_keyword_only_and_none_is_refused(n):
    """The guard is the signature: an omitted registry is a TypeError at the call, not a silent skip."""
    import inspect
    prm = inspect.signature(resolve_output).parameters["all_corpora"]
    assert prm.kind is inspect.Parameter.KEYWORD_ONLY and prm.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        resolve_output("docs", n["doc_in"], _spec(n["outer"]), None, _default_site(n))
    with pytest.raises(TypeError):
        resolve_output("docs", n["doc_in"], _spec(n["outer"]), None, _default_site(n), all_corpora=None)
    with pytest.raises(TypeError):
        resolve_output("docs", n["doc_in"], _spec(n["outer"]), None, _default_site(n), all_corpora=[])


def _scan(src: str):
    """Names every reference to `resolve_output` in `src` that is not a direct call passing a non-None `all_corpora`.
    Catches: omission, an explicit None, an aliased import, functools.partial / any other bare reference, and getattr
    with a constant that spells (part of) the name."""
    tree = ast.parse(src)
    names = {"resolve_output"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) or isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[-1] == "resolve_output" and a.asname:
                    names.add(a.asname)
    ok_funcs, bad = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            nm = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if nm in names:
                ok_funcs.add(id(f))                 # judged here as a call; never ALSO a bare reference
                kw = [k for k in node.keywords if k.arg == "all_corpora"]
                if not (kw and not (isinstance(kw[0].value, ast.Constant) and kw[0].value.value is None)):
                    bad.append((node.lineno, "call-without-registry"))
            if isinstance(f, ast.Name) and f.id == "getattr" and len(node.args) >= 2:
                for sub in ast.walk(node.args[1]):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and len(sub.value) >= 5 and (
                            "resolve_output".startswith(sub.value) or "resolve_output".endswith(sub.value)):
                        bad.append((node.lineno, "getattr"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Name, ast.Attribute)) and id(node) not in ok_funcs:
            nm = node.id if isinstance(node, ast.Name) else node.attr
            if nm in names and isinstance(getattr(node, "ctx", None), ast.Load):
                bad.append((node.lineno, "bare-reference"))
    return sorted(set(bad))


@pytest.mark.parametrize("src,expect", [
    ("resolve_output(a, b, c, d, e, all_corpora={})", []),
    ("m.resolve_output(a, b, c, d, e, all_corpora=reg)", []),
    ("resolve_output(a, b, c, d, e)", [(1, "call-without-registry")]),
    ("resolve_output(a, b, c, d, e, all_corpora=None)", [(1, "call-without-registry")]),
    ("from x import resolve_output as ro\nro(a, b, c, d, e)", [(2, "call-without-registry")]),
    ("import functools\nfunctools.partial(resolve_output, a)", [(2, "bare-reference")]),
    ("f = getattr(m, 'resolve_' + 'output')\nf(a)", [(1, "getattr")]),
    ("getattr(m, 'resolve_output')(a)", [(1, "getattr")]),
])
def test_the_caller_scan_catches_every_planted_shape(src, expect):
    assert _scan(src) == expect


def _scan_tree(root: Path, skip_name: str = ""):
    """Every .py under `root` goes through the AST scan: NO text prefilter (a prefilter on the literal name would skip
    a file that builds the name by concatenation, which is exactly the shape the scan has a control for)."""
    sites, scanned = [], 0
    for p in root.rglob("*.py"):
        rel = p.relative_to(root)
        if ".git" in rel.parts or p.name == skip_name or rel.parts[:1] == ("docs",):
            continue
        try:
            txt = p.read_text(encoding="utf-8")
            ast.parse(txt)
        except (UnicodeDecodeError, SyntaxError):
            sites.append((str(rel), 0, "unparseable"))
            continue
        scanned += 1
        sites += [(str(rel), ln, kind) for ln, kind in _scan(txt)]
    return sites, scanned


def test_no_call_in_the_repo_omits_the_registry_a_backstop_to_the_signature():
    sites, scanned = _scan_tree(REPO, Path(__file__).name)
    assert scanned >= 100, "the scan saw almost nothing: it is blind"
    assert not sites, sites


def test_the_tree_scan_has_no_prefilter_a_planted_concatenated_getattr_is_caught(tmp_path):
    (tmp_path / "plant.py").write_text("import m\nf = getattr(m, 'resolve_' + 'output')\nf(1)\n")
    (tmp_path / "plain.py").write_text("x = 1\n")
    sites, scanned = _scan_tree(tmp_path)
    assert scanned == 2 and sites == [("plant.py", 2, "getattr")]


def _nested_registry(n, inner_id, **extra):
    d = n["tmp"] / "conv"
    d.mkdir()
    (d / "conv.lts").write_text(
        "---\nlts: \"1.1\"\ntitle: conv\n---\n# conv [SPEC:CONV]\n\n## [REVIEWER-SITE-CONFIG] site\n\n"
        f"- default-output-root: {n['out']['general']}\n- default-root-corpora: docs\n")
    reg = {"docs": _spec(n["outer"], domain_suffix="d1"),
           inner_id: {**_spec(n["inner"], domain_suffix="d2"), **extra},
           "conv": {"roots": [{"path": str(d), "area": "conv"}], "domain_suffix": "c"}}
    cfg = n["tmp"] / "registry.json"
    cfg.write_text(json.dumps(reg))
    return cfg


@pytest.mark.parametrize("inner_id,extra", [(INNER_FLAG, {"private": True}), (INNER_SUFFIX, {})],
                         ids=["registry-flag", "personality-suffix"])
def test_cli_resolve_output_refuses_the_nested_private_document(n, inner_id, extra):
    cfg = _nested_registry(n, inner_id, **extra)
    st = n["tmp"] / "st"
    st.mkdir()
    p = _cli(cfg, "--corpus", "docs", "--document", n["doc_in"], state=st)
    out = json.loads(p.stdout)
    assert p.returncode == 1, (p.stdout, p.stderr)
    assert out["reason"] == "document-in-private-corpus" and out["root"] is None and out["error_class"] == "InBand"
    assert "zq-inner" not in p.stdout and "zq-inner" not in p.stderr


def test_cli_control_the_same_registry_with_a_public_inner_and_an_outside_document_still_resolve(n):
    cfg = _nested_registry(n, "zq-inner-public")
    st = n["tmp"] / "st"
    st.mkdir()
    p = _cli(cfg, "--corpus", "docs", "--document", n["doc_in"], state=st)
    assert p.returncode == 0 and json.loads(p.stdout)["rule_used"] == "default", (p.stdout, p.stderr)


def test_cli_a_document_outside_the_private_corpus_is_unaffected_in_a_registry_that_holds_one(n):
    cfg = _nested_registry(n, INNER_SUFFIX)
    st = n["tmp"] / "st"
    st.mkdir()
    p = _cli(cfg, "--corpus", "docs", "--document", n["doc_top"], state=st)
    assert p.returncode == 0 and json.loads(p.stdout)["rule_used"] == "default", (p.stdout, p.stderr)


# ------------------------------------------------------------------ every registered root shape
def test_a_single_root_private_corpus_nested_in_the_named_one_is_refused(n):
    corpora = {"docs": _spec(n["outer"]), INNER_SUFFIX: {"root": str(n["inner"]), "domain_suffix": "x"}}
    r = _res(n, "docs", n["doc_in"], corpora, _default_site(n))
    assert r.root is None and r.reason == "document-in-private-corpus"
    corpora2 = {"docs": _spec(n["outer"]), INNER_FLAG: {"root": str(n["inner"]), "private": True}}
    assert _res(n, "docs", n["doc_in"], corpora2, _default_site(n)).reason == "document-in-private-corpus"


def test_the_reviewer_reads_roots_through_the_engines_own_helper(n):
    from factlattice_engine import config, membership
    in_, out_ = os.path.realpath(n["inner"]), os.path.realpath(n["outer"])
    both = {"root": str(n["inner"]), "roots": [{"path": str(n["outer"])}]}
    # declared = the superset (roots AND root); served = exactly what the engine serves (roots if present, else root)
    assert sorted(membership.declared_root_paths(both)) == sorted([in_, out_])
    assert config.served_root_paths(both) == [out_]
    assert config.served_root_paths({"root": str(n["inner"])}) == [in_]
    assert config.served_root_paths({"roots": [str(n["inner"])]}) == [in_]
    assert config.served_root_paths({"roots": []}) == [] and config.served_root_paths(None) == []
    # the declared-registry reader and the reviewer's container search use the one declared helper
    import inspect
    assert "declared_root_paths" in inspect.getsource(membership.registered_roots)


def _single_root_registry(n, inner_id, **extra):
    cfg = _nested_registry(n, inner_id)
    reg = json.loads(cfg.read_text())
    reg[inner_id] = {"root": str(n["inner"]), "domain_suffix": "d2", **extra}
    cfg.write_text(json.dumps(reg))
    return cfg


@pytest.mark.parametrize("inner_id,extra", [(INNER_FLAG, {"private": True}), (INNER_SUFFIX, {})],
                         ids=["registry-flag", "personality-suffix"])
def test_cli_refuses_a_nested_private_corpus_registered_with_a_single_root(n, inner_id, extra):
    cfg = _single_root_registry(n, inner_id, **extra)
    st = n["tmp"] / "st"
    st.mkdir()
    p = _cli(cfg, "--corpus", "docs", "--document", n["doc_in"], state=st)
    out = json.loads(p.stdout)
    assert p.returncode == 1, (p.stdout, p.stderr)
    assert out["reason"] == "document-in-private-corpus" and "zq-inner" not in p.stdout + p.stderr


# ------------------------------------------------------------------ the not-under-the-named-corpus guard keeps its place
def test_a_document_not_under_the_named_corpus_is_refused_first_whatever_other_corpora_override(n):
    elsewhere = n["tmp"] / "corpora" / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "p.lts").write_text("# x\n")
    corpora = {"docs": _spec(n["outer"]), "zq-else-personality": _spec(elsewhere)}
    site = _site(default=str(n["out"]["general"]), corpora="docs",
                 overrides=[f"zq-else-personality -> {n['out']['special']}"])
    r = _res(n, "docs", str(elsewhere / "p.lts"), corpora, site)
    assert r.root is None and r.reason == "document-not-under-corpus-root"


# ------------------------------------------------------------------ a public corpus inside a private one
def test_a_public_corpus_nested_inside_a_private_one_is_refused_when_the_public_one_is_named(n):
    corpora = {"zq-outer-personality": _spec(n["outer"]), "docs": _spec(n["inner"])}
    site = _site(default=str(n["out"]["general"]), corpora="docs")
    r = _res(n, "docs", n["doc_in"], corpora, site)
    assert r.root is None and r.reason == "document-in-private-corpus"
    assert "zq-outer" not in json.dumps(r.to_dict())


# ------------------------------------------------------------------ two readers, two jobs
def test_a_document_under_the_extra_root_of_a_mixed_entry_is_not_under_the_named_corpus(n):
    """The engine serves `roots` when present and ignores `root`: a document under B is not in the named corpus."""
    corpora = {"docs": {"roots": [{"path": str(n["inner2"])}], "root": str(n["outer"])}}
    r = _res(n, "docs", n["doc_top"], corpora, _default_site(n))
    assert r.root is None and r.reason == "document-not-under-corpus-root"


def test_a_private_corpus_declared_only_through_the_extra_root_is_still_a_container(n):
    corpora = {"docs": _spec(n["outer"]),
               INNER_SUFFIX: {"roots": [{"path": str(n["inner2"])}], "root": str(n["inner"])}}
    r = _res(n, "docs", n["doc_in"], corpora, _default_site(n))
    assert r.root is None and r.reason == "document-in-private-corpus"


# ------------------------------------------------------------------ the single-root NAMED corpus guard
def test_a_single_root_named_corpus_gets_the_not_under_root_and_inside_root_guards(n):
    corpora = {"docs": {"root": str(n["inner"])}}
    site = _site(default=str(n["out"]["general"]), corpora="docs")
    assert _res(n, "docs", n["doc_top"], corpora, site).reason == "document-not-under-corpus-root"
    assert _res(n, "docs", n["doc_in"], corpora, site).rule_used == "default"
    bad = _site(overrides=[f"docs -> {n['inner']}"])
    assert _res(n, "docs", n["doc_in"], corpora, bad).reason == "override-root-inside-corpus"


# ------------------------------------------------------------------ the frame verb and the single reader rule
def test_frame_exclude_source_works_for_a_single_root_corpus(n):
    d = n["tmp"] / "single"
    d.mkdir()
    (d / "d.lts").write_text("# D [SPEC:DD]\n\n## [DD-1] a\n\n- note: x\n")
    (d / "e.lts").write_text("# E [SPEC:EE]\n\n## [EE-1] b\n\n- note: x\n")
    cfg = n["tmp"] / "single-registry.json"
    cfg.write_text(json.dumps({"c": {"root": str(d), "domain_suffix": "x"}}))
    st = n["tmp"] / "st2"
    st.mkdir()
    env = {**os.environ, "PYTHONPATH": str(REPO), "FACTLATTICE_CORPUS_CONFIG": str(cfg), "FACTLATTICE_STATE_DIR": str(st),
           "LTS_ENGINE_STATE_DIR": str(st), "FACTLATTICE_BACKUP_DIR": str(st / "bk")}

    def frame(*extra):
        p = subprocess.run([sys.executable, "-m", "factlattice_engine.reviewer", "frame", "--corpus", "c",
                            "--source-file", "e.lts", *extra], capture_output=True, text=True, env=env, timeout=180)
        return json.loads(p.stdout)
    assert frame()["excluded_count"] == 0 and frame()["nodes_total"] >= 1          # control: nothing excluded unasked
    out = frame("--exclude-source", os.path.realpath(d / "e.lts"))
    assert out["excluded_count"] >= 1 and out["nodes_total"] == frame()["nodes_total"] - out["excluded_count"]


def test_reviewer_code_reads_registry_roots_only_through_the_two_helpers():
    """No `roots` / `root` key read in the reviewer package: a third reader is how the readers came to disagree."""
    hits = []
    for p in (REPO / "factlattice_engine" / "reviewer").glob("*.py"):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
                    and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in ("roots", "root"):
                hits.append((p.name, node.lineno))
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and node.slice.value in ("roots", "root"):
                hits.append((p.name, node.lineno))
    assert not hits, hits


# ------------------------------------------------------------------ a named corpus that serves no roots
@pytest.mark.parametrize("spec_extra", [{"roots": [], "root": "OUTER"}, {"roots": None, "root": "OUTER"}],
                         ids=["empty-roots", "null-roots"])
def test_a_named_corpus_that_serves_nothing_has_no_document_under_it(n, spec_extra):
    """The engine serves no roots for such an entry (`roots` is present, `root` is ignored), so no document is under it."""
    spec = {k: (str(n["outer"]) if v == "OUTER" else v) for k, v in spec_extra.items()}
    r = _res(n, "docs", n["doc_top"], {"docs": spec}, _default_site(n))
    assert r.root is None and r.reason == "document-not-under-corpus-root"


def test_a_spec_that_declares_no_roots_at_all_keeps_its_older_behaviour(n):
    site = _site(overrides=[f"docs -> {n['out']['special']}"])
    assert _res(n, "docs", n["doc_top"], {"docs": {"roots": []}}, site).rule_used == "override"


def test_cli_a_named_corpus_that_serves_nothing_refuses(n):
    cfg = _nested_registry(n, "zq-inner-public")
    reg = json.loads(cfg.read_text())
    reg["docs"] = {"roots": [], "root": str(n["outer"]), "domain_suffix": "d1"}
    cfg.write_text(json.dumps(reg))
    st = n["tmp"] / "st"
    st.mkdir()
    p = _cli(cfg, "--corpus", "docs", "--document", n["doc_top"], state=st)
    assert p.returncode == 1 and json.loads(p.stdout)["reason"] == "document-not-under-corpus-root", (p.stdout, p.stderr)


# ------------------------------------------------------------------ a mixed named entry: the extra root is not the corpus
def test_a_private_document_under_the_extra_root_of_a_mixed_named_entry_is_refused_even_with_its_override(n):
    corpora = {"docs": {"roots": [{"path": str(n["inner2"])}], "root": str(n["outer"])},
               INNER_SUFFIX: _spec(n["inner"])}
    site = _site(default=str(n["out"]["general"]), corpora="docs", overrides=[f"{INNER_SUFFIX} -> {n['out']['special']}"])
    r = _res(n, "docs", n["doc_in"], corpora, site)
    assert r.root is None and r.reason == "document-not-under-corpus-root"
