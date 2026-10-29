"""Acceptance for [KEY-LOSS-RETROSCAN] and the `key_loss` / `corpus_files` lint rules.

THE ONE TEST THAT DECIDES WHETHER ANY OF THIS IS REAL
    `test_lint_FIRES_on_a_deliberately_emphasised_key` and its paired
    `test_lint_is_SILENT_once_the_emphasis_is_removed`.

    A lint that has never failed is not a lint. This codebase has already shipped two
    counters structurally incapable of incrementing, and a key-loss rule that cannot fire
    is the identical shape one instrument over. The pair is a NEGATIVE CONTROL: the same
    file, one character class different, one FIRES and one is SILENT — which proves both
    that the rule works and that it is not simply warning on everything.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from factlattice_engine import key_loss as kl
from factlattice_engine.lint import lint
from factlattice_engine.lint.corpus_files import (
    KIND_PREFIX_COLLISION,
    KIND_SHADOW_COPY,
    find_corpus_file_issues,
)

# The measured case. Kept as an OPTIONAL path so the suite runs anywhere, but when the
# file is present the numbers are asserted EXACTLY — a scanner that reproduces the known
# measurement is the whole acceptance criterion.
# The measured file is machine-local: name it with FACTLATTICE_KNOWN_CASE_FILE. Unset => the known-case tests SKIP.
_KC = os.environ.get("FACTLATTICE_KNOWN_CASE_FILE")
KNOWN_CASE = Path(_KC) if _KC else Path("/nonexistent/known-case.lts")
KNOWN_AUTHORED = 46
KNOWN_MINTED = 27
KNOWN_STRESS_LINES = [99, 100, 101, 102, 103]


# ──────────────────────────────────────────────────────────────────────────────
# 1. THE KNOWN CASE — reproduce the measurement or the instrument is not working
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not KNOWN_CASE.exists(), reason="measured corpus not on this machine")
def test_reproduces_the_boards_overview_measurement():
    rep = kl.scan_file(KNOWN_CASE)
    assert rep.authored_bullets == KNOWN_AUTHORED
    assert rep.minted_properties == KNOWN_MINTED
    assert rep.loss == KNOWN_AUTHORED - KNOWN_MINTED == 19


@pytest.mark.skipif(not KNOWN_CASE.exists(), reason="measured corpus not on this machine")
def test_names_all_five_hal_stresses():
    """The five S-lines are the most load-bearing content in the fleet's only PCB record.
    A scan that reports the right TOTAL while missing them is the failure it exists to
    catch, so they are asserted by LINE NUMBER, not by count."""
    rep = kl.scan_file(KNOWN_CASE)
    found = {d.line_number for d in rep.dropped}
    assert set(KNOWN_STRESS_LINES) <= found
    for d in rep.dropped:
        if d.line_number in KNOWN_STRESS_LINES:
            assert d.confidence != kl.NONE
            assert "emphasis_label_lead" in d.shapes


@pytest.mark.skipif(not KNOWN_CASE.exists(), reason="measured corpus not on this machine")
def test_the_ratified_coverage_lint_shows_none_of_them():
    """THE REASON THIS MODULE EXISTS. `lts_lint` reported the file clean of this for a
    month. If this assertion ever fails because the ratified rule got wider, delete this
    module rather than keep two rules answering one question."""
    rep = kl.scan_file(KNOWN_CASE)
    assert sum(1 for d in rep.dropped if d.seen_by_ratified_lint) == 0


@pytest.mark.skipif(not KNOWN_CASE.exists(), reason="measured corpus not on this machine")
def test_falsifier_misses_the_known_case():
    """`property_loss.py` — the proposal-03 falsifier — is colon-split and therefore
    blind to the five stress lines. Asserted, not assumed: the two instruments must not
    be mistaken for each other."""
    from factlattice_engine.property_loss import scan_file as falsifier_scan

    hit = {f.line_number for f in falsifier_scan(KNOWN_CASE)}
    assert not (set(KNOWN_STRESS_LINES) & hit)


# ──────────────────────────────────────────────────────────────────────────────
# 2. THE NEGATIVE CONTROL — the lint FIRES, then does not
# ──────────────────────────────────────────────────────────────────────────────

_FRONTMATTER = """---
title: Negative control
id: NC-DOC
domain: TEST
author: acceptance
date: 2026-08-13
---

# Negative control [NC-DOC]

## Body

### [NC-NODE] A node used only to prove the rule can fire
"""

BROKEN = _FRONTMATTER + "- **definition:** an emphasised key mints nothing at all\n"
FIXED = _FRONTMATTER + "- definition: an emphasised key mints nothing at all\n"


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_lint_FIRES_on_a_deliberately_emphasised_key(tmp_path: Path):
    p = _write(tmp_path, "nc.lts", BROKEN)
    res = lint(str(p))
    hits = [f for f in res.findings if f.origin == "key_loss"]
    assert hits, "the key-loss lint did not fire on an emphasised key — it is inert"
    assert hits[0].kind == "key_loss_high"
    assert hits[0].source_line == 14
    assert "md_emphasis_key" in hits[0].detail["shapes"]
    assert hits[0].detail["normalised_key"] == "definition"
    # …and the parser agrees the property really is gone.
    assert res.key_loss["minted_properties"] == 0
    assert res.key_loss["lost_bullets"] == 1


def test_lint_is_SILENT_once_the_emphasis_is_removed(tmp_path: Path):
    """The other half of the control. Without this, a rule that warns on EVERY bullet
    would pass the firing test and be worthless."""
    p = _write(tmp_path, "nc.lts", FIXED)
    res = lint(str(p))
    assert [f for f in res.findings if f.origin == "key_loss"] == []
    assert res.key_loss["minted_properties"] == 1
    assert res.key_loss["lost_bullets"] == 0


def test_summary_counter_is_emitted_on_a_CLEAN_run(tmp_path: Path):
    """The counter must appear when there is nothing to report, or a reader cannot tell
    'this corpus keeps what it authors' from 'the rule was switched off'."""
    p = _write(tmp_path, "nc.lts", FIXED)
    res = lint(str(p))
    assert res.key_loss is not None
    assert "key loss:" in res.render_text()
    assert res.to_dict()["key_loss"]["authored_bullets"] == 1


@pytest.mark.parametrize("bullet,shape", [
    ("- **definition:** v", "md_emphasis_key"),
    ("- *definition*: v", "md_emphasis_key"),
    ("- ⛔ definition: v", "pictograph_key"),
    ("- mcu / brains: v", "slashed_key"),
    ("- Definition: v", "uppercase_key"),
    ("- 3-way split: v", "digit_initial_key"),
    ("– definition: v", "nonascii_bullet_marker"),
    ("- definition:v", "no_space_after_colon"),
    ("- **CAN transport — the tenth kind.** and then prose", "emphasis_label_lead"),
])
def test_every_named_shape_is_detected(tmp_path: Path, bullet: str, shape: str):
    p = _write(tmp_path, "nc.lts", _FRONTMATTER + bullet + "\n")
    rep = kl.scan_file(p)
    assert rep.loss == 1, f"{bullet!r} was not detected as a loss at all"
    assert shape in rep.dropped[0].shapes, f"{bullet!r} -> {rep.dropped[0].shapes}"


@pytest.mark.parametrize("bullet", [
    "- Internal bus is operational: publishing to `composer/tool/mode` changes state.",
    "- This follows Acme convention: state engines always have a value.",
    "- Duplicate visual code that diverges over time",
    "- **The subject of improvement is usually the ENVIRONMENT.** A repetitive task wins.",
])
def test_prose_bullets_do_NOT_fire(tmp_path: Path, bullet: str):
    """The precision half. Each of these is a real corpus line that a first cut flagged;
    a rule that cries wolf on prose is a rule switched off in a day."""
    p = _write(tmp_path, "nc.lts", _FRONTMATTER + bullet + "\n")
    res = lint(str(p))
    assert [f for f in res.findings if f.origin == "key_loss"] == [], bullet


def test_CAN_is_not_the_modal_verb(tmp_path: Path):
    """Regression. A case-insensitive clause-verb filter classified the single most
    load-bearing line of the acceptance case as prose, because `CAN` — the bus — matched
    `can`. The scan still looked right."""
    p = _write(tmp_path, "nc.lts",
               _FRONTMATTER + "- **(S1) CAN transport — the tenth kind.** and then prose\n")
    rep = kl.scan_file(p)
    assert rep.loss == 1
    assert rep.dropped[0].shapes == ("emphasis_label_lead",)
    assert rep.dropped[0].normalised_key == "can-transport"


def test_edge_triples_are_not_counted_as_lost_properties(tmp_path: Path):
    """A property-shaped check applied to the `## Relationships` section fires on every
    WELL-FORMED edge triple. That rule would be abandoned in a day."""
    text = _FRONTMATTER + "- definition: a node\n\n## Relationships\n\n- [NC-NODE] DEPENDS_ON [NC-DOC]\n"
    p = _write(tmp_path, "nc.lts", text)
    rep = kl.scan_file(p)
    assert rep.loss == 0


# ──────────────────────────────────────────────────────────────────────────────
# 3. CORPUS-FILE HYGIENE — the defects git cannot see
# ──────────────────────────────────────────────────────────────────────────────

def test_prefix_collision_FIRES_and_is_an_error(tmp_path: Path):
    for name in ("61-config-over-bus.lts", "61-fleet-console.lts", "62-other.lts"):
        _write(tmp_path, name, _FRONTMATTER)
    issues = find_corpus_file_issues([tmp_path])
    hits = [i for i in issues if i.kind == KIND_PREFIX_COLLISION]
    assert len(hits) == 1
    assert hits[0].detail["prefix"] == "61"
    res = lint(str(tmp_path))
    assert res.has_errors
    assert res.exit_code() == 1


def test_prefix_collision_is_SILENT_on_a_clean_directory(tmp_path: Path):
    for name in ("60-a.lts", "61-b.lts", "62-c.lts"):
        _write(tmp_path, name, _FRONTMATTER)
    assert [i for i in find_corpus_file_issues([tmp_path])
            if i.kind == KIND_PREFIX_COLLISION] == []


def test_zero_padding_does_not_hide_a_collision(tmp_path: Path):
    """`06-` and `6-` are the same ordinal to a human saying "spec 6", and the nastiest
    form of this defect is the one where the two strings differ."""
    _write(tmp_path, "06-a.lts", _FRONTMATTER)
    _write(tmp_path, "6-b.lts", _FRONTMATTER)
    assert [i for i in find_corpus_file_issues([tmp_path])
            if i.kind == KIND_PREFIX_COLLISION]


def test_subdirectories_number_independently(tmp_path: Path):
    """`acme-specs/roadmap/00-…` does not collide with `acme-specs/00-…` — the
    convention orders a LISTING, and grouping by tree root would invent collisions."""
    _write(tmp_path, "00-a.lts", _FRONTMATTER)
    (tmp_path / "roadmap").mkdir()
    _write(tmp_path / "roadmap", "00-b.lts", _FRONTMATTER)
    assert [i for i in find_corpus_file_issues([tmp_path])
            if i.kind == KIND_PREFIX_COLLISION] == []


def test_shadow_copy_is_reported_and_names_its_live_twin(tmp_path: Path):
    _write(tmp_path, "60-board-hardware-inventory.lts", _FRONTMATTER)
    _write(tmp_path, "60-board-hardware-inventory.lts.bak", _FRONTMATTER)
    hits = [i for i in find_corpus_file_issues([tmp_path]) if i.kind == KIND_SHADOW_COPY]
    assert len(hits) == 1
    assert hits[0].detail["twin"] == "60-board-hardware-inventory.lts"
    assert hits[0].detail["twin_is_live"] is True
