"""Round 4 of registry-key validation (cold check r3 on aabc347, the tree merged with the nested-private routing fix).

P1-1  a document inside a NESTED corpus whose registry entry carries an unknown key (a misspelt `privat`, a root-level
      `private`, `Private`) is refused with the key code when ANOTHER corpus is named; it never routes to a shared root
      (the default, the named corpus's override or its profile root). Every container's entry is validated, not only the
      named one's: an entry with an unknown key is untrusted wholesale: every string in it is a candidate root (r4).
"""
from __future__ import annotations

import pytest

from tests.test_registry_key_validation import ENTRY_CODE, ROOT_CODE


def _site(root, listed):
    from factlattice_engine.reviewer import SiteConfig
    return SiteConfig("ok", "ref", str(root), list(listed), [], [])


@pytest.fixture
def nest(tmp_path):
    outer, out = tmp_path / "outer", tmp_path / "shared-out"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    out.mkdir()
    return {"outer": outer, "inner": inner, "out": out, "doc": str(inner / "x.lts")}


def _run(nest, inner_spec):
    from factlattice_engine.reviewer import resolve_output
    outer_spec = {"roots": [{"path": str(nest["outer"])}]}
    corpora = {"outer": outer_spec, "inner": inner_spec}
    return resolve_output("outer", nest["doc"], outer_spec, None, _site(nest["out"], ["outer"]), all_corpora=corpora)


def test_controls_public_inner_routes_and_private_inner_refuses(nest):
    pub = _run(nest, {"roots": [{"path": str(nest["inner"])}]})
    assert pub.rule_used == "default" and pub.root, pub.to_dict()          # the route the typo must not reach
    priv = _run(nest, {"roots": [{"path": str(nest["inner"])}], "private": True})
    assert priv.root is None and priv.reason == "document-in-private-corpus", priv.to_dict()


@pytest.mark.parametrize("inner_spec_fn, code", [
    (lambda p: {"roots": [{"path": p}], "privat": True}, ENTRY_CODE),
    (lambda p: {"roots": [{"path": p}], "Private": True}, ENTRY_CODE),
    (lambda p: {"roots": [{"path": p, "private": True}]}, ROOT_CODE),
])
def test_a_nested_container_with_an_unknown_key_refuses_with_its_code(nest, inner_spec_fn, code):
    r = _run(nest, inner_spec_fn(str(nest["inner"])))
    assert r.root is None and r.rule_used == "no-output-root" and r.reason == code, r.to_dict()
    assert "inner" not in r.terminal_only, "the refusal must not name the (possibly private) container"


# ---------------------------------------------------------------- r4 P1-R4-1: a bad entry is untrusted wholesale
@pytest.mark.parametrize("inner_spec_fn, code", [
    (lambda p: {"roots": [{"pth": p}], "private": True}, ROOT_CODE),        # the root path key itself misspelt
    (lambda p: {"rots": [{"path": p}], "private": True}, ENTRY_CODE),       # `roots` misspelt
    (lambda p: {"roots": [], "privat": True, "x_dir": {"deep": [p]}}, ENTRY_CODE),
])
def test_a_bad_entry_whose_root_keys_are_misspelt_still_refuses(nest, inner_spec_fn, code):
    r = _run(nest, inner_spec_fn(str(nest["inner"])))
    assert r.root is None and r.reason == code, r.to_dict()


def test_a_bad_entry_elsewhere_refuses_too(nest, tmp_path):
    """Changed by named roots S1: a failed entry's location cannot be trusted however it is spelt, so it stops routing for every document."""
    from factlattice_engine.reviewer import resolve_output
    other = tmp_path / "elsewhere"
    other.mkdir()
    outer_spec = {"roots": [{"path": str(nest["outer"])}]}
    corpora = {"outer": outer_spec, "bad": {"rots": [{"path": str(other)}], "privat": True, "domain_suffix": "proj.bad"}}
    r = resolve_output("outer", nest["doc"], outer_spec, None, _site(nest["out"], ["outer"]), all_corpora=corpora)
    assert r.root is None and r.reason == "registry-entry-unknown-key", r.to_dict()


# ---------------------------------------------------------------- r5 P2-R5-1 and the NUL case
def test_a_trailing_space_on_a_misspelt_root_still_refuses(nest):
    r = _run(nest, {"roots": [{"pth": str(nest["inner"]) + " "}], "private": True})
    assert r.root is None and r.reason == ROOT_CODE, r.to_dict()


def test_a_nul_in_a_bad_entry_does_not_raise(nest):
    r = _run(nest, {"roots": [{"pth": str(nest["inner"])}], "privat": True, "x_note_bad": "a\x00b"})
    assert r.root is None and r.reason in (ENTRY_CODE, ROOT_CODE), r.to_dict()


# ---------------------------------------------------------------- r6 P2-R6-1: a NUL in the ONLY candidate
@pytest.mark.parametrize("tail", ["\x00", "\x00junk"])
def test_a_nul_in_the_only_candidate_still_refuses(nest, tail):
    r = _run(nest, {"roots": [{"pth": str(nest["inner"]) + tail}], "private": True})
    assert r.root is None and r.reason == ROOT_CODE, r.to_dict()


# ---------------------------------------------------------------- r7 P2-R7-1: any NUL in a bad entry refuses outright
@pytest.mark.parametrize("make", [
    lambda p: "\x00" + p,                      # leading NUL
    lambda p: p[:-2] + "\x00" + p[-2:],        # NUL inside the last component
    lambda p: p + "\x00",
])
def test_any_nul_in_a_bad_entry_refuses(nest, make):
    r = _run(nest, {"roots": [{"pth": make(str(nest["inner"]))}], "private": True})
    assert r.root is None and r.reason == ROOT_CODE, r.to_dict()


def test_a_clean_entry_elsewhere_is_unaffected_by_the_nul_rule(nest):
    r = _run(nest, {"roots": [{"path": str(nest["inner"])}]})       # public, clean: routes as before
    assert r.rule_used == "default" and r.root, r.to_dict()


# ---------------------------------------------------------------- r8 P1-R8-1: the path as a key or as components
# (an entry with no recoverable path at all is a stated limit in SPEC section 4.6: nothing can locate it)
@pytest.mark.parametrize("make", [
    lambda p: {"roots": [{p: "docs"}], "private": True},                  # the path written as a root key
    lambda p: {p: {"x": 1}, "roots": [], "private": True},                 # the path written as an entry key
    lambda p: {"roots": [{"pth": p.strip("/").split("/")}], "private": True},  # the path split into components
])
def test_a_bad_private_entry_with_its_path_in_an_odd_place_refuses(nest, make):
    r = _run(nest, make(str(nest["inner"])))
    assert r.root is None and r.reason in (ENTRY_CODE, ROOT_CODE), r.to_dict()


def test_a_bad_public_entry_elsewhere_refuses_too(nest, tmp_path):
    """Changed by named roots S1, as above."""
    from factlattice_engine.reviewer import resolve_output
    other = tmp_path / "elsewhere"
    other.mkdir()
    outer_spec = {"roots": [{"path": str(nest["outer"])}]}
    corpora = {"outer": outer_spec, "bad": {"roots": [{str(other): "docs"}]}}       # bad key, public, does not contain the doc
    r = resolve_output("outer", nest["doc"], outer_spec, None, _site(nest["out"], ["outer"]), all_corpora=corpora)
    assert r.root is None and r.reason == "registry-root-unknown-key", r.to_dict()
