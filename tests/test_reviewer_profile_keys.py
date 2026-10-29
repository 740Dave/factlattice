"""A-E5 (factlattice-reviewer Spec A, slice 2): the profile's REVIEW keys.

A profile can REGISTER a review extension (a `.lts` document that adds review processes and rules for its
domain) and can name the per-corpus review OUTPUT root. Two additions, each in BOTH serialisations:

  * the `.lts` node kind `REVEXTREG-<id>` with properties `path` and `ext-id` (YAML/JSON: `review_extensions:
    [{path, ext_id}]`). NOT `REVEXT-`: that is the extension DOCUMENT's own root node (audit delta N1);
  * the profile property `review-output` (YAML/JSON: `domain.review_output`).

THE FAILURE THIS FILE EXISTS FOR (audit D4): the normative `.lts` loader projects only the node kinds it
knows and SKIPS the rest silently, so an extension registration under a kind nobody projected is a node that
parses, lints clean and registers NOTHING. Hence (1) a projection test per serialisation, (2) a validator,
and (3) `unprojected_review_extension` in `lts_profile_audit`, which names a node that LOOKS like a
registration (carries `path` and `ext-id`) under a kind the loader does not know. Every test that expects
"nothing found" is paired with one that makes the thing fire (an instrument that can only say "fine" is not
an instrument).
"""
from __future__ import annotations

import os
import textwrap

import pytest
import yaml

from factlattice.profile import (
    ReviewExtensionReg,
    load_profile,
    unprojected_review_extensions,
    validate_review_extensions,
)

LTS_PROFILE = """\
---
title: "Acme profile"
id: "PROFILE:ACME-REV-PROFILE"
domain: acme.rev
document_tag: PROFILE
status: draft
---
# Acme profile [PROFILE:ACME-REV-PROFILE]

## [PROFILE-DOMAIN] Domain identity

- domain-id: acme.rev
- domain-name: Acme review
- review-output: {out}

## [PREFIX-DEC] Decisions

- registers-prefix: DEC-
- document: decisions

## [REVEXTREG-ONE] First extension

- path: {ext1}
- ext-id: ONE

## [REVEXTREG-TWO] Second extension

- path: {ext2}
- ext-id: TWO
"""

EXT_DOC = """\
# Extension {ext} [SPEC:REVEXT-{ext}]

## [REVEXT-{ext}] Extension {ext}

- applies-to: acme
- version: 1
- authority: acme
- hook-contract-version: 1
"""


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def world(tmp_path):
    ext1 = _write(tmp_path / "exts" / "one.lts", EXT_DOC.format(ext="ONE"))
    ext2 = _write(tmp_path / "exts" / "two.lts", EXT_DOC.format(ext="TWO"))
    out = tmp_path / "reviews"
    out.mkdir()
    prof = _write(tmp_path / "acme-profile.lts",
                  LTS_PROFILE.format(out=out, ext1=ext1, ext2=ext2))
    return {"tmp": tmp_path, "ext1": ext1, "ext2": ext2, "out": out, "profile": prof}


# ------------------------------------------------------------------ both serialisations
def test_lts_projects_extension_registrations_and_review_output(world):
    p = load_profile(str(world["profile"]))
    assert p.review_extensions == [
        ReviewExtensionReg(path=str(world["ext1"]), ext_id="ONE"),
        ReviewExtensionReg(path=str(world["ext2"]), ext_id="TWO"),
    ]
    assert p.review_output == str(world["out"])


def test_yaml_projects_the_same_surface(world):
    y = {"domain": {"id": "acme.rev", "name": "Acme review", "review_output": str(world["out"])},
         "prefixes": [{"prefix": "DEC-", "document": "decisions"}],
         "review_extensions": [{"path": str(world["ext1"]), "ext_id": "ONE"},
                               {"path": str(world["ext2"]), "ext_id": "TWO"}]}
    yp = world["tmp"] / "99-acme-profile.yaml"
    yp.write_text(yaml.safe_dump(y), encoding="utf-8")
    jp = world["tmp"] / "acme.json"
    import json
    jp.write_text(json.dumps(y), encoding="utf-8")
    lts = load_profile(str(world["profile"]))
    for other in (load_profile(str(yp)), load_profile(str(jp)), load_profile(dict(y))):
        assert other.review_extensions == lts.review_extensions
        assert other.review_output == lts.review_output


def test_absent_means_no_extensions_and_no_output(tmp_path):
    """His 62133 ruling: with nothing registered the generic review runs. Total accessors, never an error."""
    p = load_profile({"domain": {"id": "x"}})
    assert p.review_extensions == [] and p.review_output is None
    lp = _write(tmp_path / "x-profile.lts",
                "---\ntitle: x\ndocument_tag: PROFILE\n---\n# x [PROFILE:X]\n\n## [PROFILE-DOMAIN] d\n\n- domain-id: x\n")
    q = load_profile(str(lp))
    assert q.review_extensions == [] and q.review_output is None
    assert validate_review_extensions(q) == []


def test_review_keys_are_not_enum_candidates(world):
    p = load_profile(str(world["profile"]))
    assert p.enum_values("review_extensions") is None


def test_a_node_with_a_path_but_no_ext_id_registers_nothing(tmp_path, world):
    txt = LTS_PROFILE.format(out=world["out"], ext1=world["ext1"], ext2=world["ext2"]) + (
        "\n## [REVEXTREG-HALF] half\n\n- path: /x/y.lts\n")
    lp = _write(tmp_path / "half-profile.lts", txt)
    assert [r.ext_id for r in load_profile(str(lp)).review_extensions] == ["ONE", "TWO"]


# ------------------------------------------------------------------ the validator
def test_a_good_registration_validates_clean(world):
    assert validate_review_extensions(load_profile(str(world["profile"]))) == []


def test_missing_path_is_named(world):
    world["ext2"].unlink()
    f = validate_review_extensions(load_profile(str(world["profile"])))
    assert [(x["kind"], x["ext_id"]) for x in f] == [("ext_path_unresolved", "TWO")]
    assert "two.lts" in f[0]["message"] or str(world["ext2"]) in f[0]["message"]


def test_ext_id_must_exist_in_that_file(world):
    _write(world["ext2"], EXT_DOC.format(ext="OTHER"))
    f = validate_review_extensions(load_profile(str(world["profile"])))
    assert [(x["kind"], x["ext_id"]) for x in f] == [("ext_id_not_in_file", "TWO")]


def test_the_full_node_id_spelling_is_accepted(world, tmp_path):
    txt = (world["profile"].read_text().replace("- ext-id: ONE", "- ext-id: REVEXT-ONE"))
    lp = _write(tmp_path / "full-profile.lts", txt)
    assert validate_review_extensions(load_profile(str(lp))) == []


def test_duplicate_ext_id_names_both_paths(world, tmp_path):
    txt = world["profile"].read_text().replace("- ext-id: TWO", "- ext-id: ONE")
    lp = _write(tmp_path / "dup-profile.lts", txt)
    f = validate_review_extensions(load_profile(str(lp)))
    dup = [x for x in f if x["kind"] == "ext_id_duplicate"]
    assert len(dup) == 1
    assert str(world["ext1"]) in dup[0]["message"] and str(world["ext2"]) in dup[0]["message"]


def test_path_is_realpath_resolved_so_a_symlink_spelling_is_the_same_file(world, tmp_path):
    link = tmp_path / "link.lts"
    os.symlink(world["ext1"], link)
    txt = world["profile"].read_text().replace(str(world["ext2"]), str(link)).replace(
        "- ext-id: TWO", "- ext-id: ONE")
    lp = _write(tmp_path / "ln-profile.lts", txt)
    f = validate_review_extensions(load_profile(str(lp)))
    dup = [x for x in f if x["kind"] == "ext_id_duplicate"]
    # same ext-id AND the same realpath: still reported (the same file registered twice), naming the
    # REALPATH so the spelling variant is visible
    assert dup and dup[0]["message"].count(os.path.realpath(world["ext1"])) == 2
    assert str(link) not in dup[0]["message"]


def test_relative_paths_resolve_against_the_profile_directory(world, tmp_path):
    txt = world["profile"].read_text().replace(str(world["ext1"]), "exts/one.lts")
    lp = _write(tmp_path / "rel-profile.lts", txt)
    assert validate_review_extensions(load_profile(str(lp))) == []
    # a dict profile has no directory to resolve against: a relative path is unresolvable, loudly
    p = load_profile({"domain": {"id": "x"},
                      "review_extensions": [{"path": "exts/one.lts", "ext_id": "ONE"}]})
    assert [x["kind"] for x in validate_review_extensions(p)] == ["ext_path_unresolved"]


def test_a_non_lts_extension_path_is_refused(world, tmp_path):
    md = _write(tmp_path / "ext.md", "x")
    txt = world["profile"].read_text().replace(str(world["ext2"]), str(md))
    lp = _write(tmp_path / "md-profile.lts", txt)
    kinds = [x["kind"] for x in validate_review_extensions(load_profile(str(lp)))]
    assert kinds == ["ext_path_unresolved"]


# ------------------------------------------------------------------ C2: registering never breaks the host
STRICT_EXT = '---\nlts: "1.1"\ntitle: strict\n---\n' + EXT_DOC.format(ext="STRICT")


def _host_world(tmp_path, ext_text, declared_keys=()):
    host = tmp_path / "host"
    ext = _write(host / "reviews-ext" / "strict.lts", ext_text)
    keys = "".join(f"\n## [PROPKEY-{i}] k\n\n- property-key: {k}\n" for i, k in enumerate(declared_keys))
    prof = _write(tmp_path / "host-profile.lts",
                  "---\ntitle: h\ndocument_tag: PROFILE\n---\n# h [PROFILE:H]\n\n## [PROFILE-DOMAIN] d\n\n"
                  "- domain-id: h\n\n## [REVEXTREG-STRICT] r\n\n"
                  f"- path: {ext}\n- ext-id: STRICT\n" + keys)
    return host, ext, load_profile(str(prof))


def test_strict_extension_inside_host_lacking_the_keys_is_extension_invalid(tmp_path):
    host, ext, prof = _host_world(tmp_path, STRICT_EXT)
    f = validate_review_extensions(prof, corpus_roots=[str(host)])
    assert [x["kind"] for x in f] == ["ext_strict_in_host_missing_keys"]
    assert "would make" in f[0]["message"] and "fail its own structure gate" in f[0]["message"]
    assert "drop the lts marker or declare the keys" in f[0]["message"]
    assert set(f[0]["keys"]) >= {"applies-to", "version", "authority", "hook-contract-version"}


def test_strict_extension_outside_the_host_roots_may_be_strict(tmp_path):
    host, ext, prof = _host_world(tmp_path, STRICT_EXT)
    assert validate_review_extensions(prof, corpus_roots=[str(tmp_path / "elsewhere")]) == []


def test_legacy_extension_inside_the_host_is_fine(tmp_path):
    host, ext, prof = _host_world(tmp_path, EXT_DOC.format(ext="STRICT"))
    assert validate_review_extensions(prof, corpus_roots=[str(host)]) == []


def test_strict_extension_inside_host_that_declares_every_key_is_fine(tmp_path):
    keys = ["applies-to", "version", "authority", "hook-contract-version"]
    host, ext, prof = _host_world(tmp_path, STRICT_EXT, declared_keys=keys)
    assert validate_review_extensions(prof, corpus_roots=[str(host)]) == []


# ------------------------------------------------------------------ the unknown-kind check
def test_a_look_alike_under_an_unknown_kind_is_reported(world, tmp_path):
    txt = world["profile"].read_text().replace("[REVEXTREG-TWO]", "[REVEXT-TWO]")
    lp = _write(tmp_path / "typo-profile.lts", txt)
    found = unprojected_review_extensions(str(lp))
    assert [(x["node_id"], x["kind"]) for x in found] == [("REVEXT-TWO", "REVEXT")]
    # and the registration really is not projected: the typo is silent to the loader, which is the defect
    assert [r.ext_id for r in load_profile(str(lp)).review_extensions] == ["ONE"]


def test_a_correctly_spelled_profile_reports_none(world):
    assert unprojected_review_extensions(str(world["profile"])) == []


def test_only_a_node_carrying_both_path_and_ext_id_is_a_look_alike(world, tmp_path):
    txt = world["profile"].read_text() + "\n## [NOTE-X] x\n\n- path: /a/b.lts\n\n## [NOTE-Y] y\n\n- ext-id: Q\n"
    lp = _write(tmp_path / "n-profile.lts", txt)
    assert unprojected_review_extensions(str(lp)) == []


def test_a_known_kind_carrying_path_and_ext_id_is_not_reported(world, tmp_path):
    # PREFIX is a known kind; it merely happens to carry two more properties
    txt = world["profile"].read_text() + "\n## [PREFIX-ZZ] z\n\n- registers-prefix: ZZ-\n- path: /a/b.lts\n- ext-id: Q\n"
    lp = _write(tmp_path / "k-profile.lts", txt)
    assert unprojected_review_extensions(str(lp)) == []


def test_non_lts_profiles_have_no_unknown_kinds(tmp_path):
    yp = tmp_path / "99-x-profile.yaml"
    yp.write_text("domain:\n  id: x\n")
    assert unprojected_review_extensions(str(yp)) == []


# ------------------------------------------------------------------ lts_profile_audit
class _Fresh:
    def evaluate(self, part):
        from factlattice_engine.store import FreshnessResult
        return FreshnessResult(part.corpus_id, True, "h", "h")


def _audit(profile_path, corpus_roots=()):
    from factlattice_engine.analytics import LtsAggregator
    from factlattice_engine.store import LtsStore, Partition
    from factlattice_engine.tools import LtsEngine
    index = {"source_corpus": {"corpus_id": "hermetic", "domain_suffix": "acme.rev", "files_hash": "h",
                               "root_paths": list(corpus_roots)},
             "nodes": {"DEC-1": {"id": "DEC-1", "type_prefix": "DEC", "title": "t", "domain": "D",
                                 "status": None, "area": "a", "token_count": 1, "source_file": None,
                                 "line_range": [], "properties": {}}},
             "layers": {"semantic": {"edges": []}}, "stats": {"total_nodes": 1, "total_edges": 0}}
    store = LtsStore()
    store.add_partition(Partition(index))
    agg = LtsAggregator(LtsEngine(store, freshness_provider=_Fresh()))
    return agg.profile_audit(corpus="hermetic", profile=str(profile_path))


def test_profile_audit_reports_registered_extensions_and_output(world):
    r = _audit(world["profile"])
    sec = r["review_extensions"]
    assert sec["registered_count"] == 2
    assert [e["ext_id"] for e in sec["registered"]] == ["ONE", "TWO"]
    assert sec["review_output"]["value"] == str(world["out"])
    assert sec["findings"] == [] and sec["unprojected_review_extension"] == []


def test_profile_audit_fires_unprojected_review_extension_on_a_misspelled_kind(world, tmp_path):
    txt = world["profile"].read_text().replace("[REVEXTREG-TWO]", "[REVEXT-TWO]")
    lp = _write(tmp_path / "typo-profile.lts", txt)
    sec = _audit(lp)["review_extensions"]
    assert [x["node_id"] for x in sec["unprojected_review_extension"]] == ["REVEXT-TWO"]
    assert sec["registered_count"] == 1


def test_profile_audit_surfaces_validator_findings(world):
    world["ext1"].unlink()
    sec = _audit(world["profile"])["review_extensions"]
    assert [x["kind"] for x in sec["findings"]] == ["ext_path_unresolved"]


def test_the_audit_section_is_ABSENT_when_not_triggered(tmp_path):
    """SDLC 7.6 response-key rule: no profile, and a profile that registers nothing, leave the answer unchanged."""
    from factlattice_engine.analytics import LtsAggregator
    from factlattice_engine.store import LtsStore, Partition
    from factlattice_engine.tools import LtsEngine
    index = {"source_corpus": {"corpus_id": "h2", "domain_suffix": "x", "files_hash": "h", "root_paths": []},
             "nodes": {}, "layers": {"semantic": {"edges": []}}, "stats": {"total_nodes": 0, "total_edges": 0}}
    store = LtsStore()
    store.add_partition(Partition(index))
    agg = LtsAggregator(LtsEngine(store, freshness_provider=_Fresh()))
    assert "review_extensions" not in agg.profile_audit(corpus="h2")
    assert "review_extensions" not in agg.profile_audit(corpus="h2", profile={"domain": {"id": "x"}})
    p = _write(tmp_path / "plain-profile.lts",
               "---\ntitle: x\ndocument_tag: PROFILE\n---\n# x [PROFILE:X]\n\n## [PROFILE-DOMAIN] d\n\n- domain-id: x\n")
    assert "review_extensions" not in agg.profile_audit(corpus="h2", profile=str(p))
    # ... and PRESENT the moment any one trigger appears (the control that the absence is not the code path being dead)
    q = _write(tmp_path / "out-profile.lts", p.read_text() + "- review-output: /somewhere\n")
    assert agg.profile_audit(corpus="h2", profile=str(q))["review_extensions"]["review_output"]["value"] == "/somewhere"


# ------------------------------------------------------------------ SPEC section 10
def test_spec_section_10_documents_both_keys():
    spec = open(os.path.join(os.path.dirname(__file__), "..", "SPEC.md"), encoding="utf-8").read()
    a = spec.index("## 10. Domain Profile System")
    b = spec.index("## 11. Event Streams")
    sec = spec[a:b]
    for needle in ("REVEXTREG", "ext-id", "review-output", "review_extensions", "review_output",
                   "unprojected_review_extension"):
        assert needle in sec, needle
