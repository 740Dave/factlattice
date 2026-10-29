"""The WRITE half — repair, binding, root membership, and the writer's own defects.

Four separately-argued closures, all in the sanctioned authoring path:

  REPAIR   `lts_add_property` ADDS; it cannot REPAIR. Every measured loss is a malformed
           EXISTING line, and adding a second correct property beside the broken prose
           line is not a fix — it makes the file carry the fact twice, once queryable and
           once not. On the one occasion an author had the surface loaded, a live finding
           in hand and every intention of using it, the operation was not there.

  BINDING  the receipt guaranteed a property LANDS and never said whether it BINDS.
           `addresses` carrying two bracketed refs minted ZERO edges and returned
           `warnings: []`.

  ROOTS    a document outside every registered root is invisible, with no warning at
           author time, at lint time, or ever.

  WRITER   the sanctioned whole-document writer could not emit a document the sanctioned
           linter accepts, could not express a doctype at all, MANUFACTURED the
           content-parsed-profile defect when asked for a PROFILE, and raised
           AttributeError on its own success path.
"""
from __future__ import annotations

import os

import pytest

from factlattice.authoring.edit import (
    EditRefused, backup_dir, repair_property, set_property,
)
from factlattice.parser import detect_id_hijack, parse_text
from factlattice_engine import binding, membership
from factlattice_engine.tools import dispatch

# The measured shape, verbatim in form: a glyph in the key region makes the WHOLE line
# parse as prose, and the value — which was never wrong — goes with it.
BROKEN = """---
scope: s
audience: a
boundary: b
---
# Control [D-ROOT]

## A node [N-ONE]

- kind: measurement
- ⛔ FLAGGED 2026-08-10: the ratified finding this supersedes
- see-also: [N-TWO]

## Another node [N-TWO]

- kind: measurement
"""

BROKEN_LINE = 11          # 1-based, the `- ⛔ FLAGGED …` line


@pytest.fixture()
def doc(tmp_path):
    p = tmp_path / "control.lts"
    p.write_text(BROKEN, encoding="utf-8")
    return p


def _props(path, node_id):
    pf = parse_text(path.read_text(encoding="utf-8"), str(path))
    return next(dict(n.properties) for n in pf.nodes if n.id == node_id)


# ---------------------------------------------------------------------------
# 6. REPAIR — the operation the whole series is about
# ---------------------------------------------------------------------------
def test_the_broken_line_really_is_lost_before_the_repair(doc):
    """The negative control. Without it, a passing repair test proves nothing about
    whether there was anything to repair."""
    assert "flagged" not in _props(doc, "N-ONE")
    assert set(_props(doc, "N-ONE")) == {"kind", "see-also"}


def test_repair_rewrites_the_line_in_place_and_the_field_appears(doc):
    r = repair_property(str(doc), "N-ONE", BROKEN_LINE, "flagged")
    assert r.line_before.startswith("- ⛔ FLAGGED")
    assert r.line_after == "- flagged: the ratified finding this supersedes"
    assert r.value_source == "derived_from_line"
    assert _props(doc, "N-ONE")["flagged"] == "the ratified finding this supersedes"


def test_repair_does_not_add_a_second_property_beside_the_broken_one(doc):
    """⛔ THE DISTINCTION FROM `add_property`, ASSERTED. Adding leaves the file carrying
    the fact twice — once queryable, once as prose — and the next reader cannot tell
    which is authoritative."""
    repair_property(str(doc), "N-ONE", BROKEN_LINE, "flagged")
    text = doc.read_text(encoding="utf-8")
    assert "⛔ FLAGGED" not in text, "the malformed line survived beside the repair"
    assert text.count("the ratified finding this supersedes") == 1


def test_repair_does_not_re_render_the_document(doc):
    """⛔ EXACTLY ONE LINE MOVES. Canonical output over hand-authored prose makes a diff
    large enough to hide a real loss inside it — which is the opposite of what a repair
    is for."""
    before = BROKEN.splitlines()
    repair_property(str(doc), "N-ONE", BROKEN_LINE, "flagged")
    after = doc.read_text(encoding="utf-8").splitlines()
    assert len(before) == len(after)
    differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert differing == [BROKEN_LINE - 1]


def test_repair_gates_on_the_whole_file_parsed_delta(doc):
    """The SAME gate as `add_property`: exactly this change, per node, across the whole
    file — not the narrower "did my key parse"."""
    r = repair_property(str(doc), "N-ONE", BROKEN_LINE, "flagged")
    assert r.delta == {"N-ONE": {"added": ["flagged"], "removed": []}}
    assert r.nodes_checked == 3          # D-ROOT, N-ONE, N-TWO


def test_repair_refuses_a_line_outside_the_named_nodes_body(doc):
    """Repairing a line into the wrong node is a SUBSTITUTION — it looks well-formed and
    it is wrong, which this project has ruled worse than a loss."""
    with pytest.raises(EditRefused) as exc:
        repair_property(str(doc), "N-TWO", BROKEN_LINE, "flagged")
    assert "not inside [N-TWO]'s body" in str(exc.value)
    assert "flagged" not in _props(doc, "N-TWO")
    assert doc.read_text(encoding="utf-8") == BROKEN       # nothing written


def test_repair_refuses_a_line_that_already_parses(doc):
    with pytest.raises(EditRefused) as exc:
        repair_property(str(doc), "N-ONE", 10, "kind")     # `- kind: measurement`
    assert "ALREADY parses as a property" in str(exc.value)
    assert doc.read_text(encoding="utf-8") == BROKEN


def test_repair_refuses_when_the_node_already_carries_the_target_key(doc):
    """Two authored sources for one field, of which the parser keeps one, is the shape a
    repair exists to remove — not to create."""
    with pytest.raises(EditRefused) as exc:
        repair_property(str(doc), "N-ONE", BROKEN_LINE, "kind")
    assert "ALREADY carries a parsed property" in str(exc.value)
    assert doc.read_text(encoding="utf-8") == BROKEN


def test_repair_refuses_a_line_number_outside_the_file(doc):
    with pytest.raises(EditRefused):
        repair_property(str(doc), "N-ONE", 9999, "flagged")
    assert doc.read_text(encoding="utf-8") == BROKEN


def test_repair_refuses_to_invent_a_value_it_cannot_locate(tmp_path):
    """⛔ NO "TAKE THE FIRST N WORDS" FALLBACK. Guessing where a key ends INVENTS a value,
    and a wrong value is a substitution. The tool asks instead."""
    p = tmp_path / "x.lts"
    p.write_text("---\nscope: s\naudience: a\nboundary: b\n---\n# D [D-ROOT]\n\n"
                 "## N [N-ONE]\n\n- kind: k\n- just some prose with no key at all\n",
                 encoding="utf-8")
    with pytest.raises(EditRefused) as exc:
        repair_property(str(p), "N-ONE", 11, "note")
    assert "Pass `value` explicitly" in str(exc.value)
    # ...and it succeeds the moment the caller supplies one.
    r = repair_property(str(p), "N-ONE", 11, "note", "just some prose with no key at all")
    assert r.value_source == "caller"


def test_repair_handles_the_bold_lead_in_shape_and_warns_the_emphasis_is_gone(tmp_path):
    """The `overview.lts` shape — `- **(S1) CAN transport — …** rest`. The repair
    works and SAYS the emphasis did not survive, because an accommodation that does not
    announce itself is a transformation that can be wrong silently."""
    p = tmp_path / "b.lts"
    p.write_text("---\nscope: s\naudience: a\nboundary: b\n---\n# D [D-ROOT]\n\n"
                 "## N [N-ONE]\n\n- kind: k\n"
                 "- **CAN transport** the tenth kind, a TRANSPORT not a peripheral\n",
                 encoding="utf-8")
    r = repair_property(str(p), "N-ONE", 11, "can-transport")
    assert r.line_after.startswith("- can-transport: the tenth kind")
    assert any("emphasis" in w for w in r.warnings)


def test_repair_over_the_mcp_surface_returns_both_lines_in_the_receipt(doc):
    """A repair receipt that showed only the result would be unreviewable: the whole
    claim is that ONE line changed and nothing else did.

    [AUTH-RECEIPT-COMPACT]: BOTH lines survive in the COMPACT default too (that is the
    entire load-bearing proof of a repair — see test_authoring_receipt_compact.py). This
    test pins the FULL rendering's `report` prose specifically, so it opts into
    `receipt="full"` explicitly."""
    res = dispatch("lts_repair_property",
                   {"path": str(doc), "node_id": "N-ONE", "line": BROKEN_LINE,
                    "key": "flagged", "receipt": "full"})
    assert res["success"] is True and res["written"] is True
    assert res["line_before"].startswith("- ⛔ FLAGGED")
    assert res["line_after"].startswith("- flagged:")
    assert "-  - ⛔ FLAGGED" in res["report"] and "+  - flagged:" in res["report"]
    assert res["value_as_parsed"] == "the ratified finding this supersedes"


def test_a_repair_refusal_is_a_result_not_a_crash(doc):
    res = dispatch("lts_repair_property",
                   {"path": str(doc), "node_id": "N-TWO", "line": BROKEN_LINE,
                    "key": "flagged"})
    assert res["success"] is False and res["refused"] is True
    assert res["file_unchanged"] is True
    assert doc.read_text(encoding="utf-8") == BROKEN


# ---------------------------------------------------------------------------
# 5b. `edges_minted` / `references_seen_but_not_minted`
# ---------------------------------------------------------------------------
def test_a_non_minting_key_carrying_refs_says_it_minted_nothing(doc):
    """⛔ THE MEASURED CASE, VERBATIM: `addresses` carrying TWO bracketed node references
    minted ZERO edges and returned `warnings: []`.

    [AUTH-RECEIPT-COMPACT]: `key_mints`/`references_seen_but_not_minted`/`binding` are
    full-only diagnostics (compact keeps only `edges_minted` + `warnings` from the
    binding channel), so this test opts into `receipt="full"`."""
    res = dispatch("lts_add_property",
                   {"path": str(doc), "node_id": "N-ONE", "key": "addresses",
                    "value": "[N-TWO] and [D-ROOT]", "receipt": "full"})
    assert res["success"] is True
    assert res["key_mints"] is False
    assert res["edges_minted"] == []
    assert res["references_seen_but_not_minted"] == ["N-TWO", "D-ROOT"]
    assert res["binding"]["reason"] == "key-not-in-minting-table" and "not in the minting table" in res["binding"]["binding_summary"]
    assert res["warnings"], "a property that binds NOTHING must not return warnings: []"


def test_the_receipt_names_the_nearest_minting_alternatives(doc):
    """⛔ STRING SIMILARITY IS THE WRONG RANKER FOR A SEMANTIC QUESTION. `addresses`
    ranks nothing useful on edit distance; the right answer is `resolves`, which scores
    near zero.

    ⛔ AMENDED 2026-08-15: this test used to exercise `relates-to` — the measured
    194-occurrence silent loss. `relates-to` IS NOW A MINTING KEY, so it no longer
    reaches the suggestion branch at all. The ranker behaviour is re-pinned on
    `addresses`, the OTHER measured instance from this module's own docstring
    ([FL-RECALL-PROPOSALS] proposal-5b), and the closure is asserted below.
    """
    res = dispatch("lts_add_property",
                   {"path": str(doc), "node_id": "N-ONE", "key": "addresses",
                    "value": "[N-TWO]", "receipt": "full"})
    assert res["nearest_minting_alternatives"][0] == "resolves"


def test_relates_to_is_now_a_minting_key_not_a_suggestion(doc):
    """The positive record: the defect this branch exists to fix, asserted closed at the
    AUTHORING surface as well as in the graph."""
    res = dispatch("lts_add_property",
                   {"path": str(doc), "node_id": "N-ONE", "key": "relates-to",
                    "value": "[N-TWO]", "receipt": "full"})
    assert res["key_mints"] is True
    assert res["binding"]["edges_minted"][0]["type"] == "RELATED_TO"


def test_the_dominant_measured_non_minting_key_gets_a_useful_receipt(doc):
    """`stage-evidence` — 21 nodes / 33 refs in one corpus, 63 uses in another, minting
    nothing — returned NO suggestion at all under pure similarity. And the advice must be
    the ruled DUAL FORM, not a rename: the key carries WHICH STAGE, which no bare edge key
    can, so it should be KEPT and the relationship declared as well."""
    res = dispatch("lts_add_property",
                   {"path": str(doc), "node_id": "N-ONE", "key": "stage-evidence",
                    "value": "proved by [N-TWO]", "receipt": "full"})
    assert res["nearest_minting_alternatives"]
    assert "Keeping this key is legitimate" in res["binding"]["binding_summary"]
    assert "## Relationships" in res["binding"]["binding_summary"]


def test_the_directional_trap_is_named_in_as_many_words():
    """`used-by` mints and `uses` does not. The author's choice of VOICE decides whether
    the edge exists, and two independent teams hit that in three days. "nearest
    alternative: used-by" is not the same help as "you wrote the passive voice of a key
    that mints".

    ⛔ AMENDED 2026-08-15: this test used to exercise `superseded-by`, which was then a
    non-minting trap. `superseded-by` NOW MINTS (reversed SUPERSEDES), so it no longer
    reaches the near-miss branch — the trap it demonstrated is closed, not broken. The
    receipt behaviour is re-pinned on a trap that is still live, and the closure of the
    old one is asserted directly below so this change cannot read as a silent deletion.
    """
    r = binding.analyse("uses", "[OTHER-NODE]", node_id="N")
    assert r["inverse_voice_of"] == "used-by"
    assert "INVERSE VOICE" in r["binding_summary"]
    assert r["nearest_minting_alternatives"][0] == "used-by"

    r2 = binding.analyse("contains", "[OTHER-NODE]", node_id="N")
    assert r2["inverse_voice_of"] == "part-of"      # scores 0.0 on similarity alone


def test_the_superseded_by_trap_is_now_CLOSED_not_merely_unasserted():
    """The positive record of what the amendment above changed."""
    r = binding.analyse("superseded-by", "[OTHER-NODE]", node_id="N")
    assert r["key_mints"] is True
    assert r["edges_minted"][0]["type"] == "SUPERSEDES"
    # reversed: the referenced node supersedes THIS one
    assert (r["edges_minted"][0]["source"], r["edges_minted"][0]["target"]) \
        == ("OTHER-NODE", "N")


def test_a_minting_key_reports_the_edges_it_actually_minted(doc):
    res = dispatch("lts_add_property",
                   {"path": str(doc), "node_id": "N-ONE", "key": "depends-on",
                    "value": "[N-TWO]", "receipt": "full"})
    assert res["key_mints"] is True
    assert res["edges_minted"] == [{"source": "N-ONE", "target": "N-TWO",
                                    "type": "DEPENDS_ON", "layer": "semantic",
                                    "target_domain": None, "inference": "property"}]
    assert res["references_seen_but_not_minted"] == []


def test_a_reversed_key_reports_the_edge_in_the_direction_it_mints(doc):
    res = dispatch("lts_add_property",
                   {"path": str(doc), "node_id": "N-ONE", "key": "used-by",
                    "value": "[N-TWO]"})
    e = res["edges_minted"][0]
    assert (e["source"], e["target"]) == ("N-TWO", "N-ONE")


def test_the_minting_table_is_read_from_the_parser_not_copied():
    """A third hand-written copy here is how a receipt starts telling authors about a
    vocabulary the parser stopped having."""
    from factlattice.parser import FLOW_PROPERTY_EDGES, REL_PROPERTY_EDGES
    assert set(binding.MINTING_KEYS) == set(REL_PROPERTY_EDGES) | set(FLOW_PROPERTY_EDGES)


def test_add_node_rolls_the_binding_up_across_every_property(doc):
    res = dispatch("lts_add_node",
                   {"path": str(doc), "node_id": "N-THREE", "title": "A third node",
                    "properties": {"see-also": "[N-ONE]",
                                   "stage-evidence": "proved by [N-TWO]"},
                    "receipt": "full"})
    assert res["success"] is True
    assert len(res["edges_minted"]) == 1
    assert res["references_seen_but_not_minted"] == ["stage-evidence: N-TWO"]
    assert res["warnings"]


# ---------------------------------------------------------------------------
# 7. ROOT MEMBERSHIP AT AUTHOR TIME
# ---------------------------------------------------------------------------
def test_a_write_outside_every_registered_root_warns_and_still_lands(doc, monkeypatch):
    """⛔ A WARNING, NOT A REFUSAL. `must-accept-arbitrary-paths` is normative: corpus-
    scoped decision files live with their datasets, outside corpus roots, and those are
    the files that most need the tool. Refusing would push the author back to the raw
    text edit — the failure this layer exists to invert."""
    monkeypatch.setattr(membership, "registered_roots",
                        lambda store=None: ({"alpha": ["/nowhere/alpha"]}, "config"))
    res = dispatch("lts_add_property",
                   {"path": str(doc), "node_id": "N-ONE", "key": "note", "value": "x"})
    assert res["success"] is True and res["written"] is True
    assert res["root_membership"]["inside_corpus"] is False
    assert any("OUTSIDE EVERY REGISTERED CORPUS ROOT" in w for w in res["warnings"])
    assert "/nowhere/alpha" in res["root_membership"]["warning"]


def test_a_write_inside_a_registered_root_says_which_corpus(doc, monkeypatch):
    monkeypatch.setattr(membership, "registered_roots",
                        lambda store=None: ({"alpha": [str(doc.parent)]}, "store"))
    res = dispatch("lts_add_property",
                   {"path": str(doc), "node_id": "N-ONE", "key": "note", "value": "x"})
    assert res["root_membership"]["inside_corpus"] is True
    assert res["root_membership"]["corpora"] == ["alpha"]
    assert res["root_membership"]["warning"] is None


def test_membership_names_the_source_of_the_roots_it_checked(doc, monkeypatch):
    """The live store and the declared registry can legitimately differ — a corpus
    registered but not yet built is in one and not the other — so the answer names its
    own source rather than presenting the union as one fact."""
    monkeypatch.setattr(membership, "registered_roots", lambda store=None: ({}, "none"))
    res = dispatch("lts_add_property",
                   {"path": str(doc), "node_id": "N-ONE", "key": "note", "value": "x",
                    "receipt": "full"})
    assert res["root_membership"]["roots_source"] == "none"
    assert "UNVERIFIABLE" in res["root_membership"]["warning"]


def test_a_sibling_named_root_does_not_count_as_containment(tmp_path):
    """`/a/corpus-old` is not inside `/a/corpus`. A `startswith` says it is."""
    (tmp_path / "corpus").mkdir()
    (tmp_path / "corpus-old").mkdir()
    v = membership.check(str(tmp_path / "corpus-old" / "f.lts"))
    assert True                                     # smoke: no crash on a missing file
    from factlattice_engine.membership import _contains
    assert _contains(str(tmp_path / "corpus"), str(tmp_path / "corpus" / "f.lts"))
    assert not _contains(str(tmp_path / "corpus"), str(tmp_path / "corpus-old" / "f.lts"))


# ---------------------------------------------------------------------------
# A2. THE BACKUP IS NOT THE ARTIFACT THE LINTER EXISTS TO CATCH
# ---------------------------------------------------------------------------
def test_the_backup_does_not_land_beside_the_document(doc, monkeypatch, tmp_path):
    """⛔ `lint/corpus_files.py::shadow_copy` flags `x.lts.bak` as "a second copy of a LIVE
    document with nothing marking which is authoritative… invisible to the tooling and
    fully visible to a human grepping the directory". Every `lts_add_property` call used
    to manufacture one."""
    scratch = tmp_path.parent / "fl-backups-scratch"
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(scratch))
    r = set_property(str(doc), "N-ONE", "note", "x")
    assert not (doc.parent / "control.lts.bak").exists()
    assert os.path.isfile(r.backup_path)
    assert not str(r.backup_path).startswith(str(doc.parent))
    # ...and no `.bak` of any name landed in the document's directory at all.
    assert not list(doc.parent.glob("*.bak"))


def test_the_linter_would_have_flagged_the_old_backup_location(doc):
    """The rule that makes the previous test load-bearing, asserted directly — otherwise
    "don't put it there" is an assertion about taste."""
    from factlattice_engine.lint.corpus_files import find_corpus_file_issues
    (doc.parent / "control.lts.bak").write_text("stale", encoding="utf-8")
    kinds = {f.kind for f in find_corpus_file_issues([str(doc.parent)])}
    assert "shadow_copy" in kinds


def test_a_second_edit_does_not_overwrite_the_only_pre_edit_copy(doc, monkeypatch, tmp_path):
    """The receipt promises "a reversal must not require reconstructing the original from
    memory". Under the old fixed `.bak` name that held for exactly one edit."""
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "backups"))
    a = set_property(str(doc), "N-ONE", "note", "one")
    b = set_property(str(doc), "N-ONE", "other", "two")
    assert a.backup_path != b.backup_path
    assert "⛔ FLAGGED" in open(a.backup_path, encoding="utf-8").read()


def test_backup_dir_is_overridable_and_outside_any_corpus(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "elsewhere"))
    assert backup_dir() == tmp_path / "elsewhere"
    assert backup_dir().is_dir()


# ---------------------------------------------------------------------------
# Ruling 1 (2026-08-17): the engine's CORE minting allowlist must equal the SPEC
# §1.3 relationship-bearing table, verb for verb — SPEC is the authority.
# ---------------------------------------------------------------------------
def test_engine_core_minting_allowlist_matches_the_spec_1_3_table():
    import re
    from factlattice.parser import REL_PROPERTY_EDGES
    spec = open(os.path.join(os.path.dirname(__file__), "..", "SPEC.md")).read()
    block = spec.split("**Relationship-bearing properties**")[1].split(
        "`see-also` / `relates-to`")[0]
    rows = re.findall(r"^\| `([^`]+)` \| (\w+) \| (\w+) \| (.+?) \|", block, re.M)
    spec_tbl = {}
    for key, etype, layer, direction in rows:
        reversed_dir = "referenced ID → node" in direction
        spec_tbl[key] = (etype, layer, reversed_dir)
    assert spec_tbl, "failed to parse the SPEC §1.3 table"
    assert set(spec_tbl) == set(REL_PROPERTY_EDGES), (
        "engine core verbs drifted from SPEC §1.3: "
        f"spec-only={set(spec_tbl) - set(REL_PROPERTY_EDGES)}, "
        f"engine-only={set(REL_PROPERTY_EDGES) - set(spec_tbl)}")
    for k in spec_tbl:
        assert spec_tbl[k] == REL_PROPERTY_EDGES[k], f"{k} differs from SPEC"


# ---------------------------------------------------------------------------
# Ruling 2 (2026-08-17): the ONE shared classifier — declared / case_variant /
# undeclared — that lint (V12) and preview (V12b) both read.
# ---------------------------------------------------------------------------
def test_classify_edge_key_splits_declared_case_variant_and_undeclared():
    from factlattice_engine.binding import classify_edge_key
    # (2c) declared core verb — mints, neither alert
    assert classify_edge_key("see-also") == ("declared", "see-also")
    assert classify_edge_key("depends-on") == ("declared", "depends-on")
    # (2a) case / separator variants of a declared verb
    assert classify_edge_key("Depends-On") == ("case_variant", "depends-on")
    assert classify_edge_key("DEPENDS_ON") == ("case_variant", "depends-on")
    assert classify_edge_key("depends_on") == ("case_variant", "depends-on")
    assert classify_edge_key("See-Also") == ("case_variant", "see-also")
    # (2b) genuinely undeclared
    assert classify_edge_key("frobnicate") == ("undeclared", None)
    assert classify_edge_key("documents") == ("undeclared", None)  # until a profile registers it


def test_classify_edge_key_honours_a_profile_registered_verb_ruling_1_union():
    """Ruling 1's UNION: a corpus-registered property_edge is DECLARED, so it mints and
    is in neither alert — and a MISCASE of it is a case_variant, not undeclared."""
    from types import SimpleNamespace
    from factlattice_engine.binding import classify_edge_key
    prof = SimpleNamespace(property_edges={"documents": ("DOCUMENTS", "semantic", False)})
    assert classify_edge_key("documents", prof) == ("declared", "documents")
    assert classify_edge_key("Documents", prof) == ("case_variant", "documents")
    # without the profile it is undeclared — proving the profile is what moved it
    assert classify_edge_key("documents") == ("undeclared", None)


def test_edge_verb_channels_dedup_and_route(tmp_path):
    import factlattice
    from factlattice_engine.lint.unminted import (
        edge_verb_channels, edge_verb_warn_line, find_unminted_reference_lists)
    doc = ("---\nlts: 1.0\n---\n\n## [AA-ONE] One\n"
           "- see-also: [AA-TWO]\n"
           "- Depends-On: [AA-TWO]\n"
           "- depends_on: [AA-TWO]\n"
           "- frobnicate: [AA-TWO]\n"
           "- frobnicate: [AA-TWO]\n\n"
           "## [AA-TWO] Two\n- k: v\n")
    (tmp_path / "t.lts").write_text(doc, encoding="utf-8")
    c = factlattice.corpus.parse(source=str(tmp_path))
    ch = edge_verb_channels(c)
    assert ch["undeclared_edge_verbs"] == ["frobnicate"]                 # deduped once
    assert set(ch["case_mismatch_edge_verbs"]) == {"Depends-On", "depends_on"}
    assert ch["case_mismatch_canonical"]["Depends-On"] == "depends-on"
    assert edge_verb_warn_line(ch) == "Case-mismatch {Depends-On, depends_on}"
    # a case variant must NOT leak into the per-occurrence undeclared alert
    assert {u.key for u in find_unminted_reference_lists(c)} == {"frobnicate"}


def test_lint_surface_carries_both_channels_and_the_case_warn(tmp_path):
    doc = ("---\nlts: 1.0\n---\n\n## [AA-ONE] One\n"
           "- Depends-On: [AA-TWO]\n"
           "- frobnicate: [AA-TWO]\n\n"
           "## [AA-TWO] Two\n- k: v\n")
    (tmp_path / "t.lts").write_text(doc, encoding="utf-8")
    out = dispatch("lts_lint", {"source": str(tmp_path)})
    um = out["unminted"]
    assert um["undeclared_edge_verbs"] == ["frobnicate"]
    assert um["case_mismatch_edge_verbs"] == ["Depends-On"]
    kinds = {f["kind"] for f in out["findings"]}
    assert "edge_verb_case_mismatch" in kinds
    warn = next(f for f in out["findings"] if f["kind"] == "edge_verb_case_mismatch")
    assert "Case-mismatch {Depends-On}" in warn["message"]
