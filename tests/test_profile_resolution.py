"""A lint verdict must NAME the vocabulary it judged against, and `source=` and
`paths=` must name the SAME one.

WHAT THIS PINS, and why it is worth a test file of its own
----------------------------------------------------------
A landed, parsing, fully-populated domain profile can be wired to NOTHING, and
nothing in a lint result will say so. That is not hypothetical: it is the measured
state this suite was written to close. Over one corpus, with no explicit `profile`
argument, three phrasings of the same question gave three answers —

    lint(source="<dir>")             -> auto-discovered a LEGACY profile   -> 505 findings
    lint(paths=[the same files])     -> NO profile at all                  -> 225 findings
    lint(paths=[...], profile=<lts>) -> the intended profile               -> 464 findings

280 findings appeared or vanished on whether the caller named a directory or listed
its files, and the result said nothing about which vocabulary was in scope or that
none was. A reader could not distinguish "this corpus is clean" from "the rules were
switched off", which is precisely how the drift survived weeks of confident linting.

The two invariants pinned here:
  1. RESOLUTION IS DECLARED — every result carries `{resolved_from, path, domain_id,
     available, corpus_root}`, and `resolved_from="none"` carries an explicit warning.
  2. RESOLUTION IS CALL-FORM INDEPENDENT — a file list resolves against its derived
     `common_root`, so it gets the same profile a directory build of that root gets.
"""
from __future__ import annotations

import json
import os

import pytest

import factlattice
from factlattice.profile import (
    RESOLVED_AUTO_DISCOVERY,
    RESOLVED_CORPUS_CONFIG,
    RESOLVED_EXPLICIT,
    RESOLVED_NONE,
    registered_profile_for_root,
    resolve_profile_ex,
)
from factlattice_engine.lint import lint

PROFILE_LTS = """\
---
title: "test domain profile"
id: "PROFILE:PROFILE-TESTDOM"
scope: "test"
audience: "test"
boundary: "test"
---
# test domain profile [PROFILE:PROFILE-TESTDOM]

## [PROFILE-DOMAIN] Domain identity

- domain-id: test.dom
- namespaces: test.dom

## [PREFIX-WIDGET] Widget nodes

- registers-prefix: WIDGET-
- definition: widgets
"""

DOC = """\
---
title: "a doc"
id: "SPEC:WIDGET-DOC"
scope: "s"
audience: "a"
boundary: "b"
---
# a doc [SPEC:WIDGET-DOC]

## [WIDGET-ONE] first

- note: registered under the test profile

## [GADGET-ONE] second

- note: NOT registered -- this is the finding the profile is supposed to produce
"""


@pytest.fixture()
def corpus(tmp_path):
    """A corpus dir with a nested file (so `commonpath` has real work to do) and a
    co-located `.lts` profile for the auto-discovery leg."""
    root = tmp_path / "mycorpus"
    (root / "sub").mkdir(parents=True)
    (root / "a.lts").write_text(DOC, encoding="utf-8")
    (root / "sub" / "b.lts").write_text(
        DOC.replace("WIDGET-DOC", "WIDGET-DOC2")
           .replace("WIDGET-ONE", "WIDGET-TWO")
           .replace("GADGET-ONE", "GADGET-TWO"), encoding="utf-8")
    prof = root / "testdom-profile.lts"
    prof.write_text(PROFILE_LTS, encoding="utf-8")
    return root, prof


def _files(root):
    """EVERY `.lts` under `root` -- including the co-located profile document, because
    a directory build ingests it too. Excluding it here would make the two forms
    disagree on CONTENT, and this file is about them agreeing on VOCABULARY."""
    return sorted(str(p) for p in root.rglob("*.lts"))


def _unregistered(result):
    return sum(1 for f in result.findings if f.kind == "unregistered_prefix")


# --------------------------------------------------------------------------- #
# 1. THE VERDICT NAMES ITS VOCABULARY
# --------------------------------------------------------------------------- #
def test_result_declares_the_profile_and_how_it_was_resolved(corpus, monkeypatch):
    root, prof = corpus
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG", raising=False)

    res = lint(source=str(root))
    p = res.profile_resolution
    assert p["resolved_from"] == RESOLVED_AUTO_DISCOVERY
    assert p["path"] == str(prof)
    assert p["domain_id"] == "test.dom"
    assert p["available"] is True
    assert p["detail"] == "testdom-profile.lts"
    # the same block rides out on the MCP-facing dict, under `profile`
    assert res.to_dict()["profile"] == p


def test_explicit_argument_is_reported_as_such(corpus, monkeypatch):
    root, prof = corpus
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG", raising=False)
    res = lint(source=str(root), profile=str(prof))
    assert res.profile_resolution["resolved_from"] == RESOLVED_EXPLICIT


def test_no_profile_is_a_REPORTED_outcome_not_a_silent_default(tmp_path, monkeypatch):
    """`none` must be loud. A caller reading a clean result has to be able to tell
    that nothing was checked -- the whole failure mode in one assertion."""
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG", raising=False)
    root = tmp_path / "bare"
    root.mkdir()
    (root / "a.lts").write_text(DOC, encoding="utf-8")

    res = lint(source=str(root))
    p = res.profile_resolution
    assert p["resolved_from"] == RESOLVED_NONE
    assert p["available"] is False
    assert p["path"] is None
    assert "INERT" in p["warning"]
    # and it is impossible to read the rendered report without seeing it
    assert "NONE IN SCOPE" in res.profile_line()
    assert "NONE IN SCOPE" in res.render_text()
    # V1 really is off -- GADGET- goes unflagged, which is exactly why the warning matters
    assert _unregistered(res) == 0


def test_profile_line_is_printed_even_when_the_corpus_is_clean(tmp_path, monkeypatch):
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG", raising=False)
    root = tmp_path / "empty"
    root.mkdir()
    res = lint(source=str(root))
    assert "lint clean" in res.render_text()
    assert "profile:" in res.render_text()


# --------------------------------------------------------------------------- #
# 2. CALL-FORM PARITY -- the 280-finding bug
# --------------------------------------------------------------------------- #
def test_source_and_paths_resolve_the_same_profile_and_agree_on_every_count(
        corpus, monkeypatch):
    root, prof = corpus
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG", raising=False)

    by_dir = lint(source=str(root))
    by_files = lint(paths=_files(root))

    assert by_files.profile_resolution["path"] == by_dir.profile_resolution["path"]
    assert by_files.profile_resolution["resolved_from"] == \
        by_dir.profile_resolution["resolved_from"]
    assert by_files.profile_domain == by_dir.profile_domain == "test.dom"
    assert by_files.summary() == by_dir.summary()
    # ...and the profile is actually DOING something, so this is not parity-at-zero:
    # GADGET-ONE and GADGET-TWO are unregistered under it and DEC-/PREFIX- are not.
    assert _unregistered(by_dir) >= 2


def test_paths_form_derives_the_same_root_the_staging_layout_uses(corpus):
    """One derivation, two readers -- a file-set must be PROFILED against the root it
    is LABELLED against, or the same bug returns wearing a different coat."""
    root, _prof = corpus
    from factlattice.corpus import common_root
    assert common_root(_files(root)) == str(root)

    parsed = factlattice.parse(paths=_files(root))
    assert parsed.profile_resolution.root == str(root)
    # the staging labels agree with that root -- same derivation, same answer
    assert {"a.lts", "sub/b.lts"} <= {n.source_file for n in parsed.nodes}


def test_single_file_resolves_against_its_directory(corpus, monkeypatch):
    root, prof = corpus
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG", raising=False)
    res = lint(paths=[str(root / "a.lts")])
    assert res.profile_resolution["path"] == str(prof)


def test_files_with_no_common_root_report_none_rather_than_invent_one(tmp_path):
    from factlattice.corpus import common_root
    assert common_root([]) is None


# --------------------------------------------------------------------------- #
# 3. THE CORPUS REGISTRY -- a profile is REGISTERED, not merely co-located
# --------------------------------------------------------------------------- #
def _write_config(tmp_path, entries):
    cfg = tmp_path / "corpus-config.json"
    cfg.write_text(json.dumps(entries), encoding="utf-8")
    return cfg


def test_corpus_config_registration_beats_auto_discovery(corpus, tmp_path, monkeypatch):
    """The registry declares what a corpus is judged against; a co-located file is an
    accident of layout. When they disagree the declaration wins -- and SAYS it did."""
    root, prof = corpus
    other = tmp_path / "elsewhere-profile.lts"
    other.write_text(PROFILE_LTS.replace("test.dom", "other.dom")
                                .replace("WIDGET-", "GADGET-"), encoding="utf-8")
    cfg = _write_config(tmp_path, {
        "mine": {"roots": [{"path": str(root), "area": "specs"}],
                 "profile": str(other), "domain_suffix": "proj.mine"}})
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(cfg))
    monkeypatch.delenv("FACTLATTICE_CORPUS_ROOT", raising=False)

    res = lint(source=str(root))
    assert res.profile_resolution["resolved_from"] == RESOLVED_CORPUS_CONFIG
    assert res.profile_resolution["path"] == str(other)
    assert res.profile_resolution["detail"] == "mine"
    assert res.profile_domain == "other.dom"
    # ...and the file list agrees, which is the point of the whole exercise
    assert lint(paths=_files(root)).profile_resolution == res.profile_resolution


def test_registration_applies_to_a_root_the_profile_does_not_live_in(tmp_path, monkeypatch):
    """The load-bearing case: the profile sits in corpus A, and corpus B's spec root --
    a DIFFERENT directory with no profile file in it -- is judged by it anyway."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    prof = a / "testdom-profile.lts"
    prof.write_text(PROFILE_LTS, encoding="utf-8")
    (b / "doc.lts").write_text(DOC, encoding="utf-8")

    cfg = _write_config(tmp_path, {
        "both": {"roots": [{"path": str(a), "area": "decisions"},
                           {"path": str(b), "area": "specs"}],
                 "profile": str(prof), "domain_suffix": "proj.both"}})
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(cfg))
    monkeypatch.delenv("FACTLATTICE_CORPUS_ROOT", raising=False)

    res = lint(source=str(b))
    assert res.profile_resolution["resolved_from"] == RESOLVED_CORPUS_CONFIG
    assert res.profile_domain == "test.dom"
    assert _unregistered(res) == 1  # GADGET-ONE


def test_two_corpora_claiming_one_root_with_different_profiles_is_REPORTED(
        tmp_path, monkeypatch):
    """A shared root judged by two vocabularies is a registry bug. Pick deterministically
    so nothing is nondeterministic, but never pick QUIETLY."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "doc.lts").write_text(DOC, encoding="utf-8")
    p1 = tmp_path / "one-profile.lts"
    p2 = tmp_path / "two-profile.lts"
    p1.write_text(PROFILE_LTS, encoding="utf-8")
    p2.write_text(PROFILE_LTS.replace("test.dom", "two.dom"), encoding="utf-8")

    cfg = _write_config(tmp_path, {
        "alpha": {"roots": [{"path": str(shared)}], "profile": str(p1)},
        "beta": {"roots": [{"path": str(shared)}], "profile": str(p2)}})
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(cfg))
    monkeypatch.delenv("FACTLATTICE_CORPUS_ROOT", raising=False)

    res = lint(source=str(shared))
    assert res.profile_resolution["resolved_from"] == RESOLVED_CORPUS_CONFIG
    assert sorted(res.profile_resolution["conflict"]) == sorted([str(p1), str(p2)])
    assert "CONFLICTING REGISTRATIONS" in res.profile_line()


def test_a_registered_profile_that_does_not_exist_is_loud_not_a_fallthrough(
        corpus, tmp_path, monkeypatch):
    """⛔ It must NOT quietly fall back to whatever file the directory happens to hold.
    A broken registration that silently resolves to something else is the original bug."""
    root, prof = corpus
    cfg = _write_config(tmp_path, {
        "mine": {"roots": [{"path": str(root)}],
                 "profile": str(tmp_path / "gone.lts")}})
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(cfg))
    monkeypatch.delenv("FACTLATTICE_CORPUS_ROOT", raising=False)

    res = lint(source=str(root))
    assert res.profile_resolution["resolved_from"] == RESOLVED_NONE
    assert res.profile_resolution["available"] is False
    assert "does not exist" in res.profile_resolution["detail"]
    assert res.profile_resolution["path"] != str(prof)


def test_the_most_specific_registered_root_wins(tmp_path, monkeypatch):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (inner / "doc.lts").write_text(DOC, encoding="utf-8")
    p_outer = tmp_path / "outer-profile.lts"
    p_inner = tmp_path / "inner-profile.lts"
    p_outer.write_text(PROFILE_LTS.replace("test.dom", "outer.dom"), encoding="utf-8")
    p_inner.write_text(PROFILE_LTS.replace("test.dom", "inner.dom"), encoding="utf-8")

    cfg = _write_config(tmp_path, {
        "o": {"roots": [{"path": str(outer)}], "profile": str(p_outer)},
        "i": {"roots": [{"path": str(inner)}], "profile": str(p_inner)}})
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(cfg))
    monkeypatch.delenv("FACTLATTICE_CORPUS_ROOT", raising=False)

    got = registered_profile_for_root(str(inner))
    assert got is not None and got[0] == "i" and got[1] == str(p_inner)


def test_a_sibling_root_with_a_shared_name_prefix_does_not_match(tmp_path, monkeypatch):
    """Containment is path-SEGMENT aware: `.../specs-old` is not inside `.../specs`."""
    specs = tmp_path / "specs"
    old = tmp_path / "specs-old"
    specs.mkdir()
    old.mkdir()
    prof = tmp_path / "x-profile.lts"
    prof.write_text(PROFILE_LTS, encoding="utf-8")
    cfg = _write_config(tmp_path, {
        "s": {"roots": [{"path": str(specs)}], "profile": str(prof)}})
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(cfg))
    monkeypatch.delenv("FACTLATTICE_CORPUS_ROOT", raising=False)

    assert registered_profile_for_root(str(specs)) is not None
    assert registered_profile_for_root(str(old)) is None


def test_a_corpus_with_no_profile_key_falls_through_to_auto_discovery(
        corpus, tmp_path, monkeypatch):
    """Registering roots without a `profile` must change NOTHING -- every deployment
    that predates this key keeps its exact prior behaviour."""
    root, prof = corpus
    cfg = _write_config(tmp_path, {"mine": {"roots": [{"path": str(root)}]}})
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(cfg))
    monkeypatch.delenv("FACTLATTICE_CORPUS_ROOT", raising=False)

    res = lint(source=str(root))
    assert res.profile_resolution["resolved_from"] == RESOLVED_AUTO_DISCOVERY
    assert res.profile_resolution["path"] == str(prof)


def test_an_unreadable_registry_is_a_named_failure_never_an_empty_registration(
        corpus, tmp_path, monkeypatch):
    """SPEC-U1a R-3 CHANGED THIS CONTRACT. It used to read "degrades to no registration, never an
    exception": a registry that could not be read became `{}`, and a lint or `check-registrations` over
    the WRONG (empty) registry came back clean. A configured registry that is broken now raises
    `RegistryError` (`registry-not-found`); the MCP tool and `call` turn it into a named refusal. With no
    registry configured the same lint still resolves by auto-discovery."""
    from factlattice.registry import RegistryError
    root, prof = corpus
    bad = tmp_path / "broken.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(bad))
    monkeypatch.delenv("FACTLATTICE_CORPUS_ROOT", raising=False)

    with pytest.raises(RegistryError) as e:
        lint(source=str(root))
    assert e.value.code == "registry-not-found" and str(bad) in str(e.value)
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG")                  # ...and nothing configured still works
    assert lint(source=str(root)).profile_resolution["resolved_from"] == RESOLVED_AUTO_DISCOVERY


def test_relative_profile_paths_resolve_under_the_corpus_root_env(
        tmp_path, monkeypatch):
    base = tmp_path / "base"
    (base / "c").mkdir(parents=True)
    (base / "c" / "doc.lts").write_text(DOC, encoding="utf-8")
    (base / "p-profile.lts").write_text(PROFILE_LTS, encoding="utf-8")
    cfg = _write_config(tmp_path, {
        "c": {"roots": [{"path": "c"}], "profile": "p-profile.lts"}})
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(cfg))
    monkeypatch.setenv("FACTLATTICE_CORPUS_ROOT", str(base))

    res = resolve_profile_ex(None, str(base / "c"))
    assert res.source == RESOLVED_CORPUS_CONFIG
    assert res.profile.domain_id == "test.dom"


# --------------------------------------------------------------------------- #
# 4. back-compat: the old shim still answers the old question
# --------------------------------------------------------------------------- #
def test_resolve_profile_shim_still_returns_a_bare_profile(corpus, monkeypatch):
    root, prof = corpus
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG", raising=False)
    from factlattice.profile import resolve_profile
    got = resolve_profile(None, str(root))
    assert got.path == str(prof) and got.domain_id == "test.dom"
