"""A domain profile is VOCABULARY, never CONTENT — and the two signals must agree.

Covers, in order:

  1. the intrinsic signal      — `document_tag: PROFILE` is what excludes, and it
                                 excludes IDENTICALLY through every content fold
                                 (`build_index`, `parse(source=)`, `parse(paths=)`,
                                 `RootIndex.assemble`);
  2. the non-regression        — the profile is still fully PARSED and still fully
                                 LOADABLE as a profile. An implementation that made it
                                 unloadable would break the vocabulary for every corpus,
                                 so that is asserted directly, not assumed;
  3. the exclusion is LOUD     — every skipped file is named on the index
                                 (`profile_documents`) and counted in `stats`;
  4. the agreement check       — BOTH directions, on hand-built fixtures where the
                                 answer is known by construction. A rule that reports
                                 zero looks exactly like a rule that works, so each
                                 direction is proven to fire AND proven not to fire on
                                 the clean arrangement.
"""
from __future__ import annotations

import json
import os

import pytest

import factlattice
from factlattice import build_index, is_profile_document, parse_file
from factlattice.profile import load_profile
from factlattice_engine.analytics.profile_registration import (
    KIND_REGISTERED_NOT_TAGGED,
    KIND_REGISTERED_PROFILE_MISSING,
    KIND_UNREGISTERED_PROFILE_DOC,
    audit_profile_registration,
    document_tag_of,
)
from factlattice_engine.config import build_merged_partition
from factlattice_engine.incremental import RootIndex
from factlattice_engine.lint import lint
from factlattice_engine.store import LtsStore

# --------------------------------------------------------------------------- #
# fixture text
# --------------------------------------------------------------------------- #
# 4 nodes: the h1 document node + three declaration nodes. Authored with CORE
# notation only, exactly as the bootstrapping invariant requires of a real profile.
PROFILE_LTS = """\
---
title: "fixture domain profile"
id: "PROFILE:PROFILE-FIXDOM"
domain: fix.dom
scope: "s"
audience: "a"
boundary: "b"
---
# fixture domain profile [PROFILE:PROFILE-FIXDOM]

## [PROFILE-DOMAIN] Domain identity

- domain-id: fix.dom
- namespaces: fix.dom

## [PREFIX-WIDGET] Widget nodes

- registers-prefix: WIDGET-
- definition: widgets

## [DOCTAG-SPEC] Spec documents

- document-tag: SPEC
- definition: a specification
"""

PROFILE_NODE_IDS = {"PROFILE-FIXDOM", "PROFILE-DOMAIN", "PREFIX-WIDGET", "DOCTAG-SPEC"}

DOC_LTS = """\
---
title: "a doc"
id: "SPEC:WIDGET-DOC"
scope: "s"
audience: "a"
boundary: "b"
---
# a doc [SPEC:WIDGET-DOC]

## [WIDGET-ONE] first

- note: content, and it stays content
"""

DOC_NODE_IDS = {"WIDGET-DOC", "WIDGET-ONE"}

# The same declarations, but the h1 says SPEC. This is the mis-tagged file the
# agreement check's first direction is about — registered as a profile, tagged as content.
MISTAGGED_LTS = PROFILE_LTS.replace("# fixture domain profile [PROFILE:PROFILE-FIXDOM]",
                                    "# fixture domain profile [SPEC:PROFILE-FIXDOM]") \
                           .replace('id: "PROFILE:PROFILE-FIXDOM"',
                                    'id: "SPEC:PROFILE-FIXDOM"')


@pytest.fixture()
def corpus(tmp_path):
    """One corpus dir: one content doc + one PROFILE-tagged profile, co-located."""
    root = tmp_path / "c"
    root.mkdir()
    (root / "doc.lts").write_text(DOC_LTS, encoding="utf-8")
    prof = root / "fixdom-profile.lts"
    prof.write_text(PROFILE_LTS, encoding="utf-8")
    return root, prof


# =========================================================================== #
# 1. THE INTRINSIC SIGNAL
# =========================================================================== #
def test_the_signal_is_the_files_own_doctag(corpus):
    root, prof = corpus
    assert is_profile_document(parse_file(str(prof))) is True
    assert is_profile_document(parse_file(str(root / "doc.lts"))) is False
    # and it answers at node level too — the parser replicates document_tag onto
    # every node, which is why the same predicate serves both.
    assert all(is_profile_document(n) for n in parse_file(str(prof)).nodes)


def test_build_index_excludes_the_profile_from_the_content_graph(corpus):
    root, _prof = corpus
    idx = build_index(str(root), built_at="t")
    assert set(idx["nodes"]) == DOC_NODE_IDS
    assert PROFILE_NODE_IDS.isdisjoint(idx["nodes"])
    assert idx["stats"]["total_nodes"] == 2


def test_the_exclusion_is_recorded_never_silent(corpus):
    """A dropped file that nothing reports is indistinguishable from a bug."""
    root, _prof = corpus
    idx = build_index(str(root), built_at="t")
    assert idx["profile_documents"] == ["fixdom-profile.lts"]
    assert idx["stats"]["profile_documents"] == 1


def test_source_and_paths_forms_exclude_identically(corpus):
    """Call-form parity: the same corpus asked two ways gives the same content."""
    root, prof = corpus
    by_dir = factlattice.parse(str(root))
    by_files = factlattice.parse(paths=[str(root / "doc.lts"), str(prof)])
    assert {n.id for n in by_dir.nodes} == DOC_NODE_IDS
    assert {n.id for n in by_files.nodes} == DOC_NODE_IDS
    assert set(by_dir.to_graph_index()["nodes"]) == set(by_files.to_graph_index()["nodes"])


def test_incremental_assemble_matches_build_index_with_a_profile_present(corpus):
    """The self-heal fold and the cold build must agree — including on the skip."""
    root, _prof = corpus
    ri = RootIndex(str(root))
    ri.full_build()
    cold = build_index(str(root), corpus_id="c", domain_suffix="proj.c", built_at="t")
    got = ri.assemble()
    for key in ("nodes", "layers", "alias_index", "id_collisions", "parse_errors",
                "profile_documents", "stats"):
        assert got[key] == cold[key], key


def test_the_parse_cache_still_holds_the_profile_and_certifies_its_hash(corpus):
    """EXCLUDED FROM CONTENT, NOT UNPARSED. The freshness leaf map must still cover
    the profile, or a corpus whose only change is its profile reads as fresh."""
    root, _prof = corpus
    ri = RootIndex(str(root))
    ri.full_build()
    assert set(ri.leaves) == {"doc.lts", "fixdom-profile.lts"}
    assert ri.leaves["fixdom-profile.lts"].startswith("sha256:")


def test_retagging_moves_a_file_across_the_boundary_in_both_directions(corpus):
    """The classification is re-evaluated on every parse. A one-way set would leave a
    file stuck on whichever side it was first seen."""
    root, prof = corpus
    ri = RootIndex(str(root))
    ri.full_build()
    assert ri.assemble()["profile_documents"] == ["fixdom-profile.lts"]

    # PROFILE -> SPEC: the declarations become content
    prof.write_text(MISTAGGED_LTS, encoding="utf-8")
    ri.full_build()
    assembled = ri.assemble()
    assert assembled["profile_documents"] == []
    assert PROFILE_NODE_IDS <= set(assembled["nodes"])

    # SPEC -> PROFILE: and back out again
    prof.write_text(PROFILE_LTS, encoding="utf-8")
    ri.full_build()
    assembled = ri.assemble()
    assert assembled["profile_documents"] == ["fixdom-profile.lts"]
    assert PROFILE_NODE_IDS.isdisjoint(assembled["nodes"])


# =========================================================================== #
# 2. THE REGRESSION THAT MATTERS MOST — the profile still WORKS
# =========================================================================== #
def test_the_profile_is_still_fully_loadable_as_a_profile(corpus):
    """If excluding it from content made it unloadable, the vocabulary would be gone
    for every corpus that registers it. That is the failure this asserts against."""
    _root, prof = corpus
    p = load_profile(str(prof))
    assert p.domain_id == "fix.dom"
    assert "WIDGET-" in p.prefix_set
    assert "SPEC" in p.document_tags


def test_lint_still_resolves_and_reports_the_co_located_profile(corpus):
    """Auto-discovery reads the file off DISK, not out of the content graph, so
    excluding it from content must not switch the profile-driven rules off."""
    root, prof = corpus
    res = lint(source=str(root))
    assert res.profile_resolution["available"] is True
    assert res.profile_resolution["path"] == str(prof)
    assert res.profile_domain == "fix.dom"


def test_line_accounting_still_covers_the_profile(corpus):
    """`coverage` is a statement about the SOURCE, not about the graph. Dropping it for
    profiles would create a file class in which silent line loss is invisible — the one
    thing [FL-LINE-ACCOUNTING] exists to prevent."""
    root, _prof = corpus
    parsed = factlattice.parse(str(root))
    assert {c.source_file for c in parsed.coverage} == {"doc.lts", "fixdom-profile.lts"}
    # ...while the graph still excludes it, so the two facts really are independent
    assert parsed.profile_documents == ["fixdom-profile.lts"]
    assert PROFILE_NODE_IDS.isdisjoint({n.id for n in parsed.nodes})


def test_the_profiles_own_declaration_nodes_no_longer_orphan(corpus):
    """A registry's declaration nodes are cited by nothing — content does not cite its
    dictionary. 88 permanent warnings on the live corpus; zero here, by construction."""
    root, _prof = corpus
    res = lint(source=str(root), dangle=True, coverage=False, profile_rules=False)
    orphans = [f for f in res.findings if f.origin == "dangle" and f.kind == "orphan"]
    assert [f for f in orphans if f.source_file == "fixdom-profile.lts"] == []
    assert PROFILE_NODE_IDS.isdisjoint({f.node_id for f in orphans})


# =========================================================================== #
# 3. THE AGREEMENT CHECK — both directions, answers known by construction
# =========================================================================== #
def _config(**corpora):
    return corpora


def _store_for(root, corpus_id="c", profile_path=None):
    st = LtsStore()
    part = build_merged_partition(corpus_id, [{"path": str(root), "area": "specs"}],
                                  f"proj.{corpus_id}")
    if part is not None:
        st.add_partition(part)
    return st


def _kinds(result):
    return sorted(f["kind"] for f in result["findings"])


def test_agreement_clean_arrangement_reports_nothing(corpus):
    """The control. A PROFILE-tagged file that IS registered is not a finding — this is
    what makes the two positive cases below mean something."""
    root, prof = corpus
    st = _store_for(root)
    res = audit_profile_registration(st, config=_config(
        c={"roots": [{"path": str(root)}], "profile": str(prof)}))
    assert res["findings"] == []
    assert res["scanned_partitions"] is True
    # ...and the check really did SEE both sides, so this is not clean-by-blindness
    assert res["registered_profiles"][0]["document_tag"] == "PROFILE"
    assert list(res["excluded_profile_documents"]) == [str(prof)]


def test_agreement_direction_A_registered_but_not_tagged_PROFILE(tmp_path):
    """Config says 'this is my profile'; the file says 'I am a SPEC'. The file wins on
    content (its declarations ARE in the graph) — and the disagreement is reported."""
    root = tmp_path / "a"
    root.mkdir()
    (root / "doc.lts").write_text(DOC_LTS, encoding="utf-8")
    bad = root / "fixdom-profile.lts"
    bad.write_text(MISTAGGED_LTS, encoding="utf-8")

    assert document_tag_of(str(bad)) == "SPEC"
    st = _store_for(root, "a")
    res = audit_profile_registration(st, config=_config(
        a={"roots": [{"path": str(root)}], "profile": str(bad)}))

    assert _kinds(res) == [KIND_REGISTERED_NOT_TAGGED]
    f = res["findings"][0]
    assert f["document_tag"] == "SPEC"
    assert f["corpora"] == ["a"]
    # the consequence the finding claims is real: it IS still content
    assert PROFILE_NODE_IDS <= set(st.get_partition("a").nodes)
    assert res["excluded_profile_documents"] == {}


def test_agreement_direction_B_PROFILE_tagged_but_registered_by_nobody(tmp_path):
    """A profile sitting in a corpus root that no corpus registers. Correctly kept out
    of content by the intrinsic signal — and inert, because nothing is judged by it."""
    root = tmp_path / "b"
    root.mkdir()
    (root / "doc.lts").write_text(DOC_LTS, encoding="utf-8")
    stray = root / "stray-profile.lts"
    stray.write_text(PROFILE_LTS, encoding="utf-8")

    st = _store_for(root, "b")
    res = audit_profile_registration(st, config=_config(
        b={"roots": [{"path": str(root)}], "profile": None}))

    assert _kinds(res) == [KIND_UNREGISTERED_PROFILE_DOC]
    f = res["findings"][0]
    assert f["path"] == str(stray)
    assert f["corpora"] == ["b"]
    # the consequence the finding claims is real: it is out of content...
    assert PROFILE_NODE_IDS.isdisjoint(st.get_partition("b").nodes)
    # ...and no corpus registers it
    assert res["registered_profiles"] == []


def test_agreement_both_directions_at_once(tmp_path):
    """Two corpora, one wrong each way. The two rules are independent and both fire."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "doc.lts").write_text(DOC_LTS, encoding="utf-8")
    bad = a / "mis-profile.lts"
    bad.write_text(MISTAGGED_LTS, encoding="utf-8")
    (b / "doc.lts").write_text(DOC_LTS.replace("WIDGET-DOC", "WIDGET-DOC2")
                                      .replace("WIDGET-ONE", "WIDGET-TWO"),
                               encoding="utf-8")
    stray = b / "stray-profile.lts"
    stray.write_text(PROFILE_LTS, encoding="utf-8")

    st = LtsStore()
    for cid, root in (("a", a), ("b", b)):
        part = build_merged_partition(cid, [{"path": str(root), "area": "specs"}],
                                      f"proj.{cid}")
        st.add_partition(part)

    res = audit_profile_registration(st, config=_config(
        a={"roots": [{"path": str(a)}], "profile": str(bad)},
        b={"roots": [{"path": str(b)}], "profile": None}))
    assert _kinds(res) == sorted([KIND_REGISTERED_NOT_TAGGED,
                                  KIND_UNREGISTERED_PROFILE_DOC])
    assert res["finding_counts"] == {KIND_REGISTERED_NOT_TAGGED: 1,
                                     KIND_UNREGISTERED_PROFILE_DOC: 1}


def test_agreement_registered_profile_that_does_not_exist(tmp_path):
    """A third disagreement class that falls out for free: the registry names a file
    that is not there. Reported as its own kind, never mislabelled 'not tagged'."""
    root = tmp_path / "c"
    root.mkdir()
    (root / "doc.lts").write_text(DOC_LTS, encoding="utf-8")
    res = audit_profile_registration(_store_for(root), config=_config(
        c={"roots": [{"path": str(root)}], "profile": str(tmp_path / "nope.lts")}))
    assert _kinds(res) == [KIND_REGISTERED_PROFILE_MISSING]


def test_agreement_says_when_it_could_not_scan_the_intrinsic_side(tmp_path):
    """Without a store the check can only see the registered half, and it must SAY so
    rather than report a clean intrinsic side it never looked at."""
    root = tmp_path / "d"
    root.mkdir()
    prof = root / "fixdom-profile.lts"
    prof.write_text(PROFILE_LTS, encoding="utf-8")
    res = audit_profile_registration(None, config=_config(
        d={"roots": [{"path": str(root)}], "profile": str(prof)}))
    assert res["scanned_partitions"] is False
    assert res["excluded_profile_documents"] == {}


def test_the_check_rides_out_on_lts_profile_audit(corpus, tmp_path, monkeypatch):
    """It has to surface where an operator already looks, or it will not be read."""
    from factlattice_engine.analytics import LtsAggregator
    from factlattice_engine.analytics.profile_audit import profile_audit
    from factlattice_engine.tools import LtsEngine

    root, prof = corpus
    cfg = tmp_path / "corpus-config.json"
    cfg.write_text(json.dumps({"c": {"roots": [{"path": str(root), "area": "specs"}],
                                     "profile": str(prof),
                                     "domain_suffix": "proj.c"}}), encoding="utf-8")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(cfg))

    out = profile_audit(LtsAggregator(LtsEngine(_store_for(root))), corpus="c")
    assert out["registration"]["success"] is True
    assert out["registration"]["findings"] == []


# =========================================================================== #
# 5. BOTH BUILDERS, ANY CWD: a single-`root:` entry is built by `build_partition`, a `roots:` entry by
#    `build_merged_partition`. The agreement check must read the same absolute path from either, whatever the
#    process's working directory. (Every test above builds through the merged path only; the single-root path
#    recorded root-relative names, which the audit resolved against the cwd, so a REGISTERED profile was reported
#    as registered by nobody. Found 2026-10-29 through the procedures guide's `call` example.)
# =========================================================================== #
@pytest.mark.parametrize("shape", ["root", "roots"])
def test_a_registered_profile_agrees_from_either_builder_and_any_cwd(corpus, tmp_path, monkeypatch, shape):
    from factlattice_engine.config import build_store
    root, prof = corpus
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    entry = {"root": str(root)} if shape == "root" else {"roots": [{"path": str(root)}]}
    cfg = _config(c={**entry, "domain_suffix": "proj.c", "profile": str(prof)})
    st = build_store(cfg)
    assert st.get_partition("c").profile_documents == [str(prof)]
    res = audit_profile_registration(st, config=cfg)
    assert res["findings"] == []
    assert list(res["excluded_profile_documents"]) == [str(prof)]


@pytest.mark.parametrize("shape", ["root", "roots"])
def test_an_unregistered_profile_is_named_by_its_real_path_from_either_builder(corpus, tmp_path, monkeypatch, shape):
    """The control for the test above: with no `profile` key the finding must still fire, and name the file
    where it is, never a path under the cwd."""
    from factlattice_engine.config import build_store
    root, prof = corpus
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    entry = {"root": str(root)} if shape == "root" else {"roots": [{"path": str(root)}]}
    cfg = _config(c={**entry, "domain_suffix": "proj.c"})
    res = audit_profile_registration(build_store(cfg), config=cfg)
    assert _kinds(res) == [KIND_UNREGISTERED_PROFILE_DOC]
    assert res["findings"][0]["path"] == str(prof)


@pytest.mark.parametrize("force_full", [False, True], ids=["incremental", "full"])
@pytest.mark.parametrize("shape", ["root", "roots", "two-roots"])
def test_a_heal_carries_the_profile_list_onto_the_partition(tmp_path, monkeypatch, shape, force_full):
    """A PROFILE file added (or removed) after the build must reach the audit through the next heal, not only
    after a restart: the heal rebuilds the list in its corpus index, and publish must carry it onto the
    partition the audit reads."""
    from factlattice_engine.config import build_store
    from factlattice_engine.selfheal import SelfHealer
    root = tmp_path / "h"
    root.mkdir()
    (root / "doc.lts").write_text(DOC_LTS, encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    (other / "other.lts").write_text(DOC_LTS.replace("WIDGET-", "GADGET-"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    entry = ({"root": str(root)} if shape == "root" else
             {"roots": [{"path": str(root)}]} if shape == "roots" else
             {"roots": [{"path": str(root)}, {"path": str(other)}]})
    cfg = _config(h={**entry, "domain_suffix": "proj.h"})
    st = build_store(cfg)
    assert audit_profile_registration(st, config=cfg)["findings"] == []

    stray = root / "stray-profile.lts"
    stray.write_text(PROFILE_LTS, encoding="utf-8")
    assert SelfHealer(st, force_full=force_full).heal(st.get_partition("h")).healed is True
    assert st.get_partition("h").profile_documents == [str(stray)]
    assert st.get_partition("h").stats["profile_documents"] == 1
    res = audit_profile_registration(st, config=cfg)
    assert _kinds(res) == [KIND_UNREGISTERED_PROFILE_DOC] and res["findings"][0]["path"] == str(stray)

    stray.unlink()
    assert SelfHealer(st, force_full=force_full).heal(st.get_partition("h")).healed is True
    assert st.get_partition("h").profile_documents == []
    assert st.get_partition("h").stats["profile_documents"] == 0
    assert audit_profile_registration(st, config=cfg)["findings"] == []


# =========================================================================== #
# 4. A REGISTERED PROFILE THAT IS NOT `.lts` HAS NO DOCUMENT TAG, AND IS NEVER CONTENT
# =========================================================================== #
# `.yaml`/`.yml`/`.json` profiles are a supported shape (`load_profile`), but only `.lts` files are parsed as
# corpus content, so "STILL PARSED AS CONTENT" is false for them and `registered_profile_not_tagged_profile`
# must not fire. `registered_profile_missing` applies to every extension; the `.lts` control above
# (direction A) must keep firing.
_NON_LTS_PROFILES = {
    ".yaml": "domain:\n  id: fix.dom\n",
    ".yml": "domain:\n  id: fix.dom\n",
    ".json": '{"domain": {"id": "fix.dom"}}\n',
}


def _non_lts_profile(root, suffix):
    prof = root / f"fixdom-profile{suffix}"
    prof.write_text(_NON_LTS_PROFILES[suffix], encoding="utf-8")
    return prof


@pytest.mark.parametrize("suffix", sorted(_NON_LTS_PROFILES))
def test_a_registered_non_lts_profile_that_loads_raises_no_disagreement(tmp_path, suffix):
    root = tmp_path / "y"
    root.mkdir()
    (root / "doc.lts").write_text(DOC_LTS, encoding="utf-8")
    prof = _non_lts_profile(root, suffix)
    # the control for "that loads": the registered file really is a usable profile
    if suffix == ".json" or pytest.importorskip("yaml"):
        assert load_profile(str(prof)).raw["domain"]["id"] == "fix.dom"
    res = audit_profile_registration(_store_for(root, "y"), config=_config(
        y={"roots": [{"path": str(root)}], "profile": str(prof)}))
    assert res["findings"] == [], res["findings"]
    # ...and the check SAW the registration, so this is not clean-by-blindness
    assert [(r["path"], r["exists"]) for r in res["registered_profiles"]] == [(str(prof), True)]


def test_a_registered_lts_profile_without_the_profile_tag_still_raises_the_disagreement(tmp_path):
    """The control: the extension exemption is for non-.lts files only."""
    root = tmp_path / "z"
    root.mkdir()
    (root / "doc.lts").write_text(DOC_LTS, encoding="utf-8")
    bad = root / "fixdom-profile.lts"
    bad.write_text(MISTAGGED_LTS, encoding="utf-8")
    res = audit_profile_registration(_store_for(root, "z"), config=_config(
        z={"roots": [{"path": str(root)}], "profile": str(bad)}))
    assert _kinds(res) == [KIND_REGISTERED_NOT_TAGGED]


@pytest.mark.parametrize("suffix", sorted(_NON_LTS_PROFILES))
def test_a_missing_registered_non_lts_profile_still_raises_registered_profile_missing(tmp_path, suffix):
    root = tmp_path / "m"
    root.mkdir()
    (root / "doc.lts").write_text(DOC_LTS, encoding="utf-8")
    res = audit_profile_registration(_store_for(root, "m"), config=_config(
        m={"roots": [{"path": str(root)}], "profile": str(tmp_path / f"nope{suffix}")}))
    assert _kinds(res) == [KIND_REGISTERED_PROFILE_MISSING]


def _list_corpora_stderr(tmp_path, registry):
    """`python -m factlattice_engine call lts_list_corpora` through the real command line, in a scratch HOME,
    from a cwd with no package in it; returns (returncode, stderr). The `package :` line is asserted so an
    old-versus-new comparison cannot silently import the wrong tree."""
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
           "FACTLATTICE_CORPUS_CONFIG": str(registry), "PYTHONPATH": str(repo)}
    r = subprocess.run([sys.executable, "-m", "factlattice_engine", "call", "lts_list_corpora"],
                       cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120)
    assert f"package  : {repo}" in r.stderr, r.stderr
    return r.returncode, r.stderr



def test_the_command_line_prints_no_disagreement_for_a_yaml_profile_and_one_for_a_mistagged_lts(tmp_path):
    ry = tmp_path / "ry"
    ry.mkdir()
    (ry / "doc.lts").write_text(DOC_LTS, encoding="utf-8")
    yml = _non_lts_profile(ry, ".yaml")
    rl = tmp_path / "rl"
    rl.mkdir()
    (rl / "doc.lts").write_text(DOC_LTS, encoding="utf-8")
    bad = rl / "fixdom-profile.lts"
    bad.write_text(MISTAGGED_LTS, encoding="utf-8")

    reg_y = tmp_path / "yaml.json"
    reg_y.write_text(json.dumps({"cy": {"roots": [{"path": str(ry), "area": "specs"}],
                                        "domain_suffix": "proj.cy", "profile": str(yml)}}))
    reg_l = tmp_path / "lts.json"
    reg_l.write_text(json.dumps({"cl": {"roots": [{"path": str(rl), "area": "specs"}],
                                        "domain_suffix": "proj.cl", "profile": str(bad)}}))
    (tmp_path / "l").mkdir()
    (tmp_path / "y").mkdir()
    rc_l, err_l = _list_corpora_stderr(tmp_path / "l", reg_l)
    rc_y, err_y = _list_corpora_stderr(tmp_path / "y", reg_y)
    # the control first: the mis-tagged .lts case prints the line, so the yaml silence means something
    assert rc_l == 0 and "PROFILE REGISTRATION DISAGREEMENT (registered_profile_not_tagged_profile)" in err_l, err_l
    assert rc_y == 0 and "DISAGREEMENT" not in err_y, err_y

def test_an_lts_named_symlink_to_a_yaml_profile_still_raises_the_disagreement(tmp_path):
    """Review F1: a registered `.lts` symlink inside the root is walked as content by its own name, whatever it points
    at, so the not-tagged finding must still fire even though the real path ends `.yaml`."""
    root = tmp_path / "s"
    root.mkdir()
    (root / "doc.lts").write_text(DOC_LTS, encoding="utf-8")
    target = tmp_path / "fixdom-profile.yaml"
    target.write_text(_NON_LTS_PROFILES[".yaml"], encoding="utf-8")
    link = root / "fixdom-profile.lts"
    link.symlink_to(target)
    res = audit_profile_registration(_store_for(root, "s"), config=_config(
        s={"roots": [{"path": str(root)}], "profile": str(link)}))
    assert _kinds(res) == [KIND_REGISTERED_NOT_TAGGED]


@pytest.mark.parametrize("name", ["fixdom-profile", "fixdom-profile.lts.bak", "fixdom-profile.txt"])
def test_a_registered_profile_with_no_data_extension_still_raises_the_disagreement(tmp_path, name):
    """Review F2: only `.yaml`, `.yml` and `.json` are exempt. A registered profile with no extension, `.lts.bak` or
    any other suffix keeps the boot signal instead of booting silent."""
    root = tmp_path / "n"
    root.mkdir()
    (root / "doc.lts").write_text(DOC_LTS, encoding="utf-8")
    prof = tmp_path / name
    prof.write_text(_NON_LTS_PROFILES[".yaml"], encoding="utf-8")
    res = audit_profile_registration(_store_for(root, "n"), config=_config(
        n={"roots": [{"path": str(root)}], "profile": str(prof)}))
    assert _kinds(res) == [KIND_REGISTERED_NOT_TAGGED]
