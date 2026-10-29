"""RULING C (the maintainer 2026-08-17) — the BARE-vs-NAMESPACE-QUALIFIED reference WARN channel.

The namespace analog of the edge-verb Case-mismatch channel: a bare reference that fails
to resolve but whose bare id matches a namespace-qualified DEFINITION is surfaced with a
"did you mean" WARN, and NEVER auto-equated (strict identity). One (single twin) vs
several (ambiguous) is distinguished; zero twins is an ordinary dangle.

⛔ A MEASURED-INERT NOTE THAT IS ITSELF A CONTROL. The current parser strips the
namespace/doctype segment from every node id, so `[SPEC:ACME-VISION]` is STORED bare and a
bare `[ACME-VISION]` already resolves to it — the channel therefore returns empty on every
real corpus today (0 across the acme corpora, against 80 forward-dangles). The pure
classifier is tested over CONSTRUCTED namespaced-id inputs, so the logic is proven for the
day a strict-namespace-identity id-model decision (the maintainer/architect's call) lands.
"""
from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

from factlattice_engine.lint.dangle import (
    ambiguous_reference_warn_line,
    classify_unqualified_references,
    unqualified_reference_targets,
    unqualified_warn_line,
)
from factlattice_engine.preview import preview
from factlattice_engine.tools import dispatch


def _mock_corpus(defined_ids, dangling_targets, source_file="a.lts"):
    diags = [SimpleNamespace(node_id=t, source_file=source_file) for t in dangling_targets]
    return SimpleNamespace(
        nodes=[SimpleNamespace(id=i) for i in defined_ids],
        diagnostics_of=lambda kind: diags if kind == "dangling" else [])


# --- the four ruled controls, over the pure classifier ----------------------
def test_single_namespaced_twin_is_a_did_you_mean():
    c = classify_unqualified_references(_mock_corpus(["SPEC:ACME-VISION"], ["ACME-VISION"]))
    assert c["unqualified_references"] == {"ACME-VISION": "SPEC:ACME-VISION"}
    assert c["ambiguous_references"] == {}
    assert unqualified_warn_line(c) == "Unqualified-reference {ACME-VISION -> SPEC:ACME-VISION}"


def test_two_namespaced_twins_are_ambiguous_not_a_single_guess():
    c = classify_unqualified_references(_mock_corpus(["SPEC:X", "DEC:X"], ["X"]))
    assert c["unqualified_references"] == {}
    assert c["ambiguous_references"] == {"X": ["DEC:X", "SPEC:X"]}
    assert unqualified_warn_line(c) is None
    assert ambiguous_reference_warn_line(c) == "Ambiguous-unqualified-reference {X -> [DEC:X | SPEC:X]}"


def test_no_namespaced_twin_is_an_ordinary_dangle():
    c = classify_unqualified_references(_mock_corpus(["FOO"], ["BAR"]))
    assert c["unqualified_references"] == {}
    assert c["ambiguous_references"] == {}
    assert unqualified_warn_line(c) is None


def test_a_resolving_reference_never_reaches_the_channel():
    # A namespaced ref target (has ':') and a target that resolves are both excluded.
    c = classify_unqualified_references(
        _mock_corpus(["SPEC:ACME-VISION", "ACME-VISION"], ["ACME-VISION", "SPEC:ACME-VISION"]))
    # ACME-VISION resolves (it is defined), SPEC:ACME-VISION is a namespaced ref -> neither.
    assert c["unqualified_references"] == {}
    assert c["ambiguous_references"] == {}


def test_dedup_across_repeated_dangling_occurrences():
    c = classify_unqualified_references(
        _mock_corpus(["SPEC:ACME-VISION"], ["ACME-VISION", "ACME-VISION", "ACME-VISION"]))
    assert c["unqualified_references"] == {"ACME-VISION": "SPEC:ACME-VISION"}


def test_targets_helper_names_both_channels():
    c = classify_unqualified_references(
        _mock_corpus(["SPEC:A", "SPEC:B", "DEC:B"], ["A", "B"]))
    assert unqualified_reference_targets(c) == frozenset({"A", "B"})


# --- the wiring is LIVE on both surfaces (present, correct, inert on real ids) -
def test_lint_surface_carries_the_report_and_it_is_empty_by_construction():
    with tempfile.TemporaryDirectory() as d:
        # `[SPEC:ACME-VISION]` is stored BARE, so `[ACME-VISION]` RESOLVES: no dangle, and
        # the channel is empty — the measured-inert proof, pinned so it cannot silently
        # start lying if the id model changes without this test being revisited.
        open(os.path.join(d, "a.lts"), "w").write(
            "# spine [SPEC:ACME-VISION]\n- k: v\n\n## [ACME-FOUNDATION] F\n- see-also: [ACME-VISION]\n")
        out = dispatch("lts_lint", {"source": d})
        assert "unqualified_references" in out
        assert out["unqualified_references"]["unqualified_references"] == {}
        assert out["unqualified_references"]["ambiguous_references"] == {}
        # and it did NOT dangle, precisely because bare resolves to the stripped id
        assert not any("dangl" in f["kind"] for f in out["findings"])


def test_preview_surface_carries_the_channel():
    doc = "# spine [SPEC:ACME-VISION]\n- k: v\n\n## [ACME-FOUNDATION] F\n- see-also: [ACME-VISION]\n"
    out = preview(doc, virtual_path="d.lts")
    assert "unqualified_references" in out
    assert "channel" in out["unqualified_references"]
    assert out["unqualified_references"]["unqualified_references"] == {}
