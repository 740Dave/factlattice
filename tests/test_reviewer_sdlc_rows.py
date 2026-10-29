"""SDLC 7.6 rows the factlattice-reviewer build adds (Spec A v3 section 11; slice 2). A new extension point adds its
rule to the checklist in the same commit (the checklist's own preamble); this pins that each row exists, carries
its name and the load-bearing words, and sits inside section 7.6 (not elsewhere in the file)."""
from __future__ import annotations

import os

import pytest

SDLC = open(os.path.join(os.path.dirname(__file__), "..", "SDLC.md"), encoding="utf-8").read()
A = SDLC.index("### 7.6 FACTLATTICE-SPEC-CHECKLIST")
B = SDLC.index("## 8. Stage 5: Deploy")
SEC = SDLC[A:B]

ROWS = {
    "VERSIONED-CONTRACTS": ("X1/X4", "hook-contract-version", "review-format-version", "bumps", "test_reviewer_contracts"),
    "RESERVED-PREFIXES": ("X3", "prior-use", "every registered", "REVEXTREG-", "REVIEWER-"),
    "PROFILE-KEY-PROJECTION": ("X5", "both loaders", "validator", "SPEC", "unprojected_review_extension"),
    "SHIPPED-ISSUE-PACKAGE-DATA": ("X8", "package data", "wheel", "negative control"),
    "REVIEWS-PROFILE-IN-SCOPE": ("X9", "registered", "reviewer profile", "profile:"),
    "HELD-RULES-HAVE-OWNERS": ("X11", "words", "owner", "release event"),
    "REVIEWER-CONFIG-KEYS": ("REVIEWER-SITE-CONFIG", "declared in the reviewer profile", "pinned by a test"),
    "REVIEW-KEY-GRAMMAR": ("<CORPUSSLUG>", "uniqueness", "exclusive create", "pinned by a test"),
}


@pytest.mark.parametrize("name", sorted(ROWS))
def test_row_exists_in_section_7_6_with_its_words(name):
    assert f"[{name}]" in SEC, f"{name}: no row in 7.6"
    start = SEC.index(f"[{name}]")
    row = SEC[start:SEC.find("\n- [", start + 1) if SEC.find("\n- [", start + 1) != -1 else len(SEC)]
    for w in ROWS[name]:
        assert w.lower() in row.lower(), f"{name}: row lacks {w!r}"


def test_rows_are_checkbox_items_like_the_rest():
    for name in ROWS:
        i = SEC.index(f"[{name}]")
        line_start = SEC.rfind("\n", 0, i) + 1
        assert SEC[line_start:i].lstrip().startswith("- [ ]"), name


def test_the_rows_the_first_slice_added_are_still_there():
    for name in ("CREATE-WRITER", "FIND-PATH-SOURCE", "LEDGER-VERB-VOCAB", "PORTED-DEF-INVARIANTS", "DSD-SELECTION-CITATION"):
        assert f"[{name}]" in SEC
