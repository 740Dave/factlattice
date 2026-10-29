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
