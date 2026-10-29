"""`factlattice_engine.reviewer.resolve_output` (Spec A v4 addendum C3, C4(d), v3 section 4.4).

WHERE A REVIEW IS WRITTEN IS CODE, NOT MODEL JUDGEMENT. A model that chooses the output root is a model that
can be told to write somewhere else by the document it is reviewing (C5), and a site whose overrides silently
went missing writes VaultSvc-class reviews into the general folder. So the routing is a pure function,
tested here for the precedence, the private rule (e), the fail-closed lookup, the folder-name rules and the
path guards, with spelling variants. The reviewer OBEYS its answer; the deployment's go-live check script
(not in this repository) calls the same code through the read-only CLI tested at the bottom.

PRECEDENCE (C3): config overrides keyed `corpus[:path-prefix]` > the corpus profile's `review-output` > the
config `default-output-root`, the last only for corpora on `default-root-corpora`, and NEVER for a corpus the
engine treats as private (`privacy.private_reason`). Anything else: `no-output-root`, with a named reason.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from factlattice.profile import load_profile
from factlattice_engine.reviewer import (
    Override,
    Resolution,
    SiteConfig,
    check_extension_paths,
    lookup_site_config,
    output_folder,
    parse_site_config,
    resolve_output,
)


def _site(**kw):
    props = {}
    if "default" in kw:
        props["default-output-root"] = kw["default"]
    if "corpora" in kw:
        props["default-root-corpora"] = kw["corpora"]
    if "overrides" in kw:
        props["corpus-output-overrides"] = kw["overrides"]
    return parse_site_config(props, source="conv:REVIEWER-SITE-CONFIG@abc123")


@pytest.fixture
def w(tmp_path):
    """A reviewed corpus `docs` with two subtrees, and three candidate output roots."""
    corpus = tmp_path / "corpora" / "docs"
    (corpus / "alpha").mkdir(parents=True)
    (corpus / "beta").mkdir(parents=True)
    for n in ("alpha/a.lts", "beta/b.lts", "top.lts"):
        (corpus / n).write_text("# x\n")
    out = {n: tmp_path / "out" / n for n in ("general", "special", "profile")}
    for p in out.values():
        p.mkdir(parents=True)
    spec = {"roots": [{"path": str(corpus), "area": "docs"}], "domain_suffix": "x"}
    return {"tmp": tmp_path, "corpus": corpus, "out": out, "spec": spec,
            "doc": lambda rel: str(corpus / rel)}


def _prof(tmp_path, out_root):
    p = tmp_path / "p-profile.lts"
    p.write_text("---\ntitle: p\ndocument_tag: PROFILE\n---\n# p [PROFILE:P]\n\n## [PROFILE-DOMAIN] d\n\n"
                 f"- domain-id: p\n- review-output: {out_root}\n")
    return load_profile(str(p))


# ---------------------------------------------------------------- precedence
def test_override_beats_profile_and_default(w):
    site = _site(default=str(w["out"]["general"]), corpora="docs",
                 overrides=[f"docs -> {w['out']['special']}"])
    r = resolve_output("docs", w["doc"]("top.lts"), w["spec"], _prof(w["tmp"], w["out"]["profile"]), site)
    assert (r.root, r.rule_used) == (os.path.realpath(w["out"]["special"]), "override")


def test_unpacks_as_a_pair():
    r = resolve_output("docs", "/nope", None, None, SiteConfig.none())
    root, rule = r
    assert (root, rule) == (None, "no-output-root") and isinstance(r, Resolution)


def test_profile_beats_default(w):
    site = _site(default=str(w["out"]["general"]), corpora="docs")
    r = resolve_output("docs", w["doc"]("top.lts"), w["spec"], _prof(w["tmp"], w["out"]["profile"]), site)
    assert (r.root, r.rule_used) == (os.path.realpath(w["out"]["profile"]), "profile")


def test_default_only_for_listed_corpora(w):
    site = _site(default=str(w["out"]["general"]), corpora="docs")
    r = resolve_output("docs", w["doc"]("top.lts"), w["spec"], None, site)
    assert (r.root, r.rule_used) == (os.path.realpath(w["out"]["general"]), "default")
    r2 = resolve_output("docs", w["doc"]("top.lts"), w["spec"], None,
                        _site(default=str(w["out"]["general"]), corpora="other"))
    assert r2.root is None and r2.rule_used == "no-output-root"
    assert r2.reason == "corpus-not-on-default-root-list" and "default-root-corpora" in r2.explanation


def test_default_with_no_corpora_list_applies_to_nobody(w):
    r = resolve_output("docs", w["doc"]("top.lts"), w["spec"], None, _site(default=str(w["out"]["general"])))
    assert r.rule_used == "no-output-root"


def test_no_site_config_and_no_profile_output_is_no_output_root(w):
    r = resolve_output("docs", w["doc"]("top.lts"), w["spec"], None, SiteConfig.none())
    assert (r.root, r.rule_used) == (None, "no-output-root")
    assert r.site_config == "none" and r.site_overrides == "MISSING"


def test_no_site_config_profile_review_output_still_works(w):
    r = resolve_output("docs", w["doc"]("top.lts"), w["spec"], _prof(w["tmp"], w["out"]["profile"]),
                       SiteConfig.none())
    assert (r.rule_used) == "profile"


def test_the_shipped_empty_config_supplies_nothing(w):
    site = parse_site_config({"default-output-root": "none", "corpus-output-overrides": "none",
                              "default-root-corpora": "none"}, source="factlattice-reviewer:REVIEWER-CONFIG@x")
    assert site.default_output_root is None and site.overrides == () and site.default_root_corpora == ()
    assert resolve_output("docs", w["doc"]("top.lts"), w["spec"], None, site).rule_used == "no-output-root"


# ---------------------------------------------------------------- overrides: keys, prefixes, ties
def test_path_prefix_override_applies_only_under_that_subtree(w):
    site = _site(default=str(w["out"]["general"]), corpora="docs",
                 overrides=[f"docs:alpha/ -> {w['out']['special']}"])
    a = resolve_output("docs", w["doc"]("alpha/a.lts"), w["spec"], None, site)
    b = resolve_output("docs", w["doc"]("beta/b.lts"), w["spec"], None, site)
    assert (a.rule_used, a.root) == ("override", os.path.realpath(w["out"]["special"]))
    assert (b.rule_used, b.root) == ("default", os.path.realpath(w["out"]["general"]))


def test_prefix_is_matched_by_component_not_by_string(w):
    (w["corpus"] / "alphabet").mkdir()
    (w["corpus"] / "alphabet" / "c.lts").write_text("# x\n")
    site = _site(default=str(w["out"]["general"]), corpora="docs",
                 overrides=[f"docs:alpha -> {w['out']['special']}"])
    r = resolve_output("docs", w["doc"]("alphabet/c.lts"), w["spec"], None, site)
    assert r.rule_used == "default"


def test_longest_prefix_wins_and_a_corpus_wide_key_is_the_shortest(w):
    other = w["tmp"] / "out" / "deep"
    other.mkdir()
    site = _site(overrides=[f"docs -> {w['out']['general']}", f"docs:alpha/ -> {w['out']['special']}",
                            f"docs:alpha/sub/ -> {other}"])
    (w["corpus"] / "alpha" / "sub").mkdir()
    (w["corpus"] / "alpha" / "sub" / "s.lts").write_text("# x\n")
    r = resolve_output("docs", w["doc"]("alpha/sub/s.lts"), w["spec"], None, site)
    assert r.root == os.path.realpath(other)
    r = resolve_output("docs", w["doc"]("alpha/a.lts"), w["spec"], None, site)
    assert r.root == os.path.realpath(w["out"]["special"])
    r = resolve_output("docs", w["doc"]("top.lts"), w["spec"], None, site)
    assert r.root == os.path.realpath(w["out"]["general"])


def test_a_prefixed_override_cannot_match_a_document_outside_the_corpus_roots(w):
    site = _site(overrides=[f"docs:alpha/ -> {w['out']['special']}"])
    r = resolve_output("docs", str(w["tmp"] / "elsewhere" / "alpha" / "x.lts"), w["spec"], None, site)
    assert r.rule_used == "no-output-root"


def test_two_overrides_with_the_same_key_and_different_roots_fail_closed(w):
    site = _site(overrides=[f"docs -> {w['out']['special']}", f"docs -> {w['out']['general']}"])
    r = resolve_output("docs", w["doc"]("top.lts"), w["spec"], None, site)
    assert r.root is None and r.rule_used == "no-output-root"
    # round 7: a reason never echoes an output root (a path); it says how many differ
    assert r.reason == "conflicting-overrides" and "different output roots" in r.explanation and str(w["out"]["special"]) not in r.reason + r.explanation


def test_override_arrow_spellings_and_a_malformed_item(w):
    site = _site(overrides=[f"docs → {w['out']['special']}", "this has no arrow"])
    assert [o.corpus for o in site.overrides] == ["docs"]
    assert site.unparsed_overrides == ("this has no arrow",)
    assert resolve_output("docs", w["doc"]("top.lts"), w["spec"], None, site).rule_used == "override"


def test_tilde_in_a_root_is_expanded(w, monkeypatch):
    monkeypatch.setenv("HOME", str(w["tmp"]))
    (w["tmp"] / "reviews").mkdir()
    site = _site(overrides=["docs -> ~/reviews"])
    assert resolve_output("docs", w["doc"]("top.lts"), w["spec"], None, site).root == \
        os.path.realpath(w["tmp"] / "reviews")


def test_the_returned_root_is_the_realpath_not_the_spelling(w):
    link = w["tmp"] / "out" / "via-link"
    os.symlink(w["out"]["special"], link)
    site = _site(overrides=[f"docs -> {link}/"])
    r = resolve_output("docs", w["doc"]("top.lts"), w["spec"], None, site)
    assert r.root == os.path.realpath(w["out"]["special"]) and r.root != str(link)


def test_a_relative_root_is_refused(w):
    site = _site(overrides=["docs -> relative/dir"])
    r = resolve_output("docs", w["doc"]("top.lts"), w["spec"], None, site)
    assert r.root is None and r.reason == "override-root-not-absolute" and "absolute" in r.explanation


# ---------------------------------------------------------------- C3(e): private corpora never default
@pytest.mark.parametrize("cid,spec_extra", [("zed-personality", {}), ("plain", {"private": True})])
def test_a_private_corpus_never_uses_the_default_root_even_if_listed(w, cid, spec_extra):
    spec = {**w["spec"], **spec_extra}
    site = _site(default=str(w["out"]["general"]), corpora=cid)
    r = resolve_output(cid, w["doc"]("top.lts"), spec, None, site)
    assert r.root is None and r.rule_used == "no-output-root"
    assert r.reason == "private-corpus-needs-override" and "private" in r.explanation


def test_a_private_corpus_may_use_an_override_or_its_own_profile_output(w):
    spec = {**w["spec"]}
    site = _site(default=str(w["out"]["general"]), corpora="zed-personality",
                 overrides=[f"zed-personality -> {w['out']['special']}"])
    assert resolve_output("zed-personality", w["doc"]("top.lts"), spec, None, site).rule_used == "override"
    r = resolve_output("zed-personality", w["doc"]("top.lts"), spec,
                       _prof(w["tmp"], w["out"]["profile"]), SiteConfig.none())
    assert r.rule_used == "profile"


def test_private_false_in_the_registry_lifts_the_suffix_default(w):
    spec = {**w["spec"], "private": False}
    site = _site(default=str(w["out"]["general"]), corpora="zed-personality")
    assert resolve_output("zed-personality", w["doc"]("top.lts"), spec, None, site).rule_used == "default"


def test_the_privacy_rule_is_the_engines_not_a_second_copy():
    import factlattice_engine.reviewer as m
    src = open(m.__file__, encoding="utf-8").read()
    assert "private_reason" in src and '"-personality"' not in src and "'-personality'" not in src


# ---------------------------------------------------------------- C3(a): the lookup fails closed on 0 and 2+
def _find_returning(nodes):
    def find(**kw):
        assert kw.get("prefix") == "REVIEWER-SITE-CONFIG" and "corpus" not in kw
        return {"success": True, "nodes": nodes}
    return find


def _node(corpus, props=None, h="h1"):
    return {"id": "REVIEWER-SITE-CONFIG", "corpus": corpus, "content_hash": h,
            "properties": props or {"default-output-root": "/x"}}


def test_lookup_zero_is_no_site_config():
    s = lookup_site_config(_find_returning([]))
    assert s.status == "none" and s.ref == "none"


def test_lookup_exactly_one_is_the_config_with_its_hash_in_the_ref():
    s = lookup_site_config(_find_returning([_node("conv", h="sha256:abc")]))
    assert s.status == "ok" and s.ref == "conv:REVIEWER-SITE-CONFIG@sha256:abc"


def test_lookup_two_is_ambiguous_and_names_both(w):
    s = lookup_site_config(_find_returning([_node("conv-a"), _node("conv-b")]))
    assert s.status == "ambiguous"
    r = resolve_output("docs", w["doc"]("top.lts"), w["spec"], None, s)
    assert r.root is None and r.rule_used == "no-output-root"
    assert r.reason == "ambiguous-site-config" and "conv-a" not in r.reason + r.explanation
    assert any("conv-a" in x for x in r.terminal_only["site_config_found"]) and any("conv-b" in x for x in r.terminal_only["site_config_found"])


def test_ambiguity_blocks_even_a_profile_output(w):
    s = lookup_site_config(_find_returning([_node("conv-a"), _node("conv-b")]))
    r = resolve_output("docs", w["doc"]("top.lts"), w["spec"], _prof(w["tmp"], w["out"]["profile"]), s)
    assert r.rule_used == "no-output-root"


def test_lookup_an_unsuccessful_find_fails_closed():
    s = lookup_site_config(lambda **kw: {"success": False, "error": "boom"})
    assert s.status == "unreadable"


def test_lookup_a_find_that_raises_fails_closed():
    def boom(**kw):
        raise RuntimeError("x")
    assert lookup_site_config(boom).status == "unreadable"


# ---------------------------------------------------------------- C3(b): missing overrides are visible
def test_default_root_with_overrides_missing_adds_the_errored_check(w):
    site = _site(default=str(w["out"]["general"]), corpora="docs")
    r = resolve_output("docs", w["doc"]("top.lts"), w["spec"], None, site)
    assert r.rule_used == "default" and r.site_overrides == "MISSING"
    assert [c["name"] for c in r.checks] == ["site-overrides-missing"]
    assert r.checks[0]["outcome"] == "errored"


def test_default_root_with_overrides_loaded_adds_no_check(w):
    site = _site(default=str(w["out"]["general"]), corpora="docs",
                 overrides=[f"other:x/ -> {w['out']['special']}"])
    r = resolve_output("docs", w["doc"]("top.lts"), w["spec"], None, site)
    assert r.rule_used == "default" and r.site_overrides == "1 loaded" and r.checks == []
    assert r.site_config == "conv:REVIEWER-SITE-CONFIG@abc123"


def test_an_unreadable_lookup_with_default_unavailable_says_so(w):
    s = lookup_site_config(lambda **kw: {"success": False})
    r = resolve_output("docs", w["doc"]("top.lts"), w["spec"], _prof(w["tmp"], w["out"]["profile"]), s)
    assert r.site_overrides == "MISSING" and r.site_config == "none"


# ---------------------------------------------------------------- path guards (canonicalise first)
def _inside_variants(w):
    """Spellings of 'a root inside the reviewed corpus', each a different way to defeat a string-prefix check."""
    corpus = w["corpus"]
    link = w["tmp"] / "link-into-corpus"
    os.symlink(corpus / "alpha", link)
    yield "plain", str(corpus / "alpha")
    yield "dotdot", str(w["tmp"] / "out" / ".." / "corpora" / "docs" / "alpha")
    yield "trailing-slash", str(corpus / "alpha") + "/"
    yield "symlink", str(link)
    yield "the corpus root itself", str(corpus)
    yield "doubled separators", str(corpus).replace("/corpora/", "//corpora//") + "//alpha"
    yield "case variant", str(corpus).replace("corpora", "CORPORA").replace("docs", "DoCs") + "/alpha"


def test_an_output_root_inside_the_reviewed_corpus_is_refused_in_every_spelling(w):
    case_insensitive = os.path.exists(str(w["corpus"]).replace("corpora", "CORPORA").replace("docs", "DoCs"))
    for label, root in _inside_variants(w):
        if label == "case variant" and not case_insensitive:
            continue                      # a case-sensitive volume has no such alias
        for via in ("override", "profile", "default"):
            if via == "override":
                site, prof = _site(overrides=[f"docs -> {root}"]), None
            elif via == "profile":
                site, prof = SiteConfig.none(), _prof(w["tmp"], root)
            else:
                site, prof = _site(default=root, corpora="docs"), None
            r = resolve_output("docs", w["doc"]("top.lts"), w["spec"], prof, site)
            assert r.root is None and r.rule_used == "no-output-root", (label, via)
            assert r.reason.endswith("-root-inside-corpus") and "inside the reviewed corpus" in r.explanation, (label, via)


def test_the_guard_also_covers_root_plus_the_corpus_folder(w):
    """root = the corpus's PARENT, folder = the corpus id: root/folder IS the corpus root."""
    site = _site(overrides=[f"docs -> {w['corpus'].parent}"])
    r = resolve_output("docs", w["doc"]("top.lts"), w["spec"], None, site)
    assert r.root is None and r.reason.endswith("-root-inside-corpus") and "inside the reviewed corpus" in r.explanation


def test_the_negative_control_a_root_beside_the_corpus_is_accepted(w):
    site = _site(overrides=[f"docs -> {w['out']['special']}"])
    assert resolve_output("docs", w["doc"]("top.lts"), w["spec"], None, site).rule_used == "override"
    # a sibling directory whose NAME merely starts with the corpus directory's name
    sib = w["corpus"].parent / "docs-reviews"
    sib.mkdir()
    assert resolve_output("docs", w["doc"]("top.lts"), w["spec"], None,
                          _site(overrides=[f"docs -> {sib}"])).rule_used == "override"


def test_extension_path_equal_to_the_reviewed_document_is_refused_in_every_spelling(w):
    doc = w["corpus"] / "alpha" / "a.lts"
    link = w["tmp"] / "doc-link.lts"
    os.symlink(doc, link)
    for spelling in (str(doc), str(w["corpus"] / "alpha" / ".." / "alpha" / "a.lts"), str(link),
                     str(doc).replace("//", "/")):
        found = check_extension_paths([spelling], str(doc))
        assert [f["kind"] for f in found] == ["extension_is_the_reviewed_document"], spelling
    other = w["corpus"] / "beta" / "b.lts"
    assert check_extension_paths([str(other)], str(doc)) == []


def test_extension_inside_a_nonexistent_document_path_is_still_compared_canonically(w):
    missing = str(w["corpus"] / "alpha" / "nope.lts")
    assert check_extension_paths([missing], str(w["corpus"] / "alpha" / ".." / "alpha" / "nope.lts")) != []


# ---------------------------------------------------------------- C4(d): folder names
@pytest.mark.parametrize("given,folder", [
    ("acme-docs", "acme-docs"),
    ("Sample Corpus_LTS", "sample-corpus-lts"),
    ("a/b", "a-b"),
    ("..", "_no-corpus"),
    ("../../etc", "etc"),
    ("", "_no-corpus"),
    (None, "_no-corpus"),
    ("---", "_no-corpus"),
    ("été", "t"),
    ("x" * 300, "x" * 120),
])
def test_output_folder(given, folder):
    assert output_folder(given) == folder


def test_an_unregistered_corpus_and_a_missing_document_still_route(w):
    site = _site(overrides=[f"ghost -> {w['out']['special']}"])
    r = resolve_output("ghost", "/no/such/doc.lts", None, None, site)
    assert (r.rule_used, r.folder) == ("override", "ghost")


def test_an_unregistered_corpus_is_not_private_unless_its_id_says_so(w):
    site = _site(default=str(w["out"]["general"]), corpora="ghost,spooky-personality")
    assert resolve_output("ghost", "/x.lts", None, None, site).rule_used == "default"
    assert resolve_output("spooky-personality", "/x.lts", None, None, site).rule_used == "no-output-root"


def test_no_corpus_given_resolves_through_the_same_function(w):
    site = _site(overrides=[f"docs -> {w['out']['special']}"])
    r = resolve_output(None, "/x.lts", None, None, site)
    assert r.folder == "_no-corpus" and r.rule_used == "no-output-root"


# ---------------------------------------------------------------- purity
def test_resolve_output_writes_nothing(w):
    before = sorted(str(p) for p in w["tmp"].rglob("*"))
    site = _site(default=str(w["out"]["general"]), corpora="docs")
    resolve_output("docs", w["doc"]("top.lts"), w["spec"], None, site)
    assert sorted(str(p) for p in w["tmp"].rglob("*")) == before


def test_resolve_output_is_deterministic(w):
    site = _site(default=str(w["out"]["general"]), corpora="docs", overrides=[f"docs:alpha/ -> {w['out']['special']}"])
    a = resolve_output("docs", w["doc"]("alpha/a.lts"), w["spec"], None, site)
    b = resolve_output("docs", w["doc"]("alpha/a.lts"), w["spec"], None, site)
    assert tuple(a) == tuple(b) and a.reason == b.reason


# ---------------------------------------------------------------- the read-only CLI
def _registry(tmp, w, extra_conv=1, conv_props=None):
    """A scratch registry: the reviewed corpus `docs`, and `extra_conv` corpora each holding one
    REVIEWER-SITE-CONFIG node routing `docs:alpha/` to the special root and defaulting `docs` to general."""
    reg = {"docs": {"roots": [{"path": str(w["corpus"]), "area": "docs"}], "domain_suffix": "x"}}
    for i in range(extra_conv):
        d = tmp / f"conv{i}"
        d.mkdir()
        (d / "conv.lts").write_text(
            "---\nlts: \"1.1\"\ntitle: conv\n---\n# conv [SPEC:CONV]\n\n## [REVIEWER-SITE-CONFIG] site\n\n"
            f"- default-output-root: {w['out']['general']}\n- default-root-corpora: docs\n"
            "- corpus-output-overrides:\n"
            f"  - docs:alpha/ -> {w['out']['special']}\n")
        reg[f"conv{i}"] = {"roots": [{"path": str(d), "area": "conv"}], "domain_suffix": "x"}
    cfg = tmp / "registry.json"
    cfg.write_text(json.dumps(reg))
    return cfg


def _cli(cfg, *args, state):
    env = {**os.environ, "FACTLATTICE_CORPUS_CONFIG": str(cfg), "FACTLATTICE_STATE_DIR": str(state),
           "LTS_ENGINE_STATE_DIR": str(state), "FACTLATTICE_BACKUP_DIR": str(state / "bk"),
           "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}
    p = subprocess.run([sys.executable, "-m", "factlattice_engine.reviewer", "resolve-output", *args],
                       capture_output=True, text=True, env=env, timeout=120)
    return p


def test_cli_override_and_default_and_read_only(w, tmp_path):
    cfg = _registry(tmp_path, w)
    tree_before = sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in tmp_path.rglob("*") if p.is_file())
    st = tmp_path / "st"
    st.mkdir()
    p = _cli(cfg, "--corpus", "docs", "--document", w["doc"]("alpha/a.lts"), state=st)
    out = json.loads(p.stdout)
    assert p.returncode == 0, p.stderr
    assert out["rule_used"] == "override" and out["root"] == os.path.realpath(w["out"]["special"])
    assert out["site_config"].startswith("conv0:REVIEWER-SITE-CONFIG@") and out["site_overrides"] == "1 loaded"
    p2 = _cli(cfg, "--corpus", "docs", "--document", w["doc"]("beta/b.lts"), state=st)
    assert json.loads(p2.stdout)["rule_used"] == "default"
    tree_after = sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in tmp_path.rglob("*")
                        if p.is_file() and not str(p).startswith(str(st)))
    assert tree_after == [t for t in tree_before if not t[0].startswith(str(st))]


def test_cli_with_no_site_config_fails_closed_with_exit_1(w, tmp_path):
    cfg = _registry(tmp_path, w, extra_conv=0)
    st = tmp_path / "st"
    st.mkdir()
    p = _cli(cfg, "--corpus", "docs", "--document", w["doc"]("alpha/a.lts"), state=st)
    out = json.loads(p.stdout)
    assert p.returncode == 1 and out["rule_used"] == "no-output-root" and out["root"] is None


def test_cli_two_site_configs_is_ambiguous(w, tmp_path):
    cfg = _registry(tmp_path, w, extra_conv=2)
    st = tmp_path / "st"
    st.mkdir()
    p = _cli(cfg, "--corpus", "docs", "--document", w["doc"]("alpha/a.lts"), state=st)
    out = json.loads(p.stdout)
    assert p.returncode == 1 and out["reason"] == "ambiguous-site-config" and out["error_class"] == "InBand"
    assert "conv0" not in json.dumps({k: v for k, v in out.items() if k != "terminal_only"})
    assert "conv0" in json.dumps(out["terminal_only"]) and "conv1" in json.dumps(out["terminal_only"])


def test_cli_has_no_write_flag_and_refuses_unknown_commands(tmp_path):
    p = subprocess.run([sys.executable, "-m", "factlattice_engine.reviewer", "resolve-output", "--help"],
                       capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))})
    assert "--allow-write" not in p.stdout and "--corpus" in p.stdout and "--document" in p.stdout
    q = subprocess.run([sys.executable, "-m", "factlattice_engine.reviewer", "write-output"],
                       capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))})
    assert q.returncode == 2
