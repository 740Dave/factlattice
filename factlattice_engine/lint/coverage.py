"""The LINE-ACCOUNTING lint over a parsed FactLattice corpus [FL-LINE-ACCOUNTING].

    INVARIANT: every non-blank line of a `.lts` file is either CONSUMED by a parser
    branch, or CLASSIFIED as ignorable. Anything else is a warning.

This is the control that makes silent content loss impossible to ship. Before it, a
corpus could drop 68% of its authored relationship triples and every property under
an un-tagged sub-heading while `lts_lint` reported `0 errors` -- because every rule
in the linter was a rule about content the parser HAD read.

WHAT THIS LAYER DOES AND DOES NOT DO
------------------------------------
It does NOT parse, and it does NOT decide what would have parsed. Both of those live
in `factlattice`:

  * `factlattice.parser` writes the consumption record (`ParsedFile.coverage`) at the
    exact points its branches commit -- so the record cannot drift from the grammar;
  * `factlattice.coverage.classify_line` owns the ignorable classifier, because the
    "one enumerated decoration away from SPEC 2.2" question is defined BY the parser's
    regexes and must live beside them.

This module is the adapter: it walks `ParsedCorpus.coverage`, keeps the unaccounted
verdicts, and dresses them as lint `Finding`s. That split is not ceremony. A second
copy of the grammar living in the linter is exactly the drift that produced the
domain-verb silent drop -- there, a profile's `relationship_verbs` list was consumed
only by a reporting tool while the parser used its own hardcoded table, and the two
disagreed in silence from the parser's first commit.

SEVERITY: warning, always, and deliberately.
-------------------------------------------
Dropped content is a real defect, but this rule lands on corpora that have been
accumulating the condition for months (165 findings on this repository's own
`domain-profiles/`, 882 across the Acme corpora at introduction). Shipping it
as an error would red-gate every existing corpus on day one, and a gate everyone
disables is worth less than a warning everyone reads. Promote it per-corpus via
`lint(coverage_severity={"near_miss_property": "error"})` once a corpus is clean --
that is the ratchet.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import factlattice
from factlattice.coverage import LineVerdict, account_file

ORIGIN = "coverage"

# Per-kind severity. All warnings at introduction -- see the module docstring.
COVERAGE_SEVERITY: Dict[str, str] = {
    "near_miss_property": "warning",
    "near_miss_triple": "warning",
    "near_miss_causal": "warning",
    "unaccounted_frontmatter": "warning",
    # An ID table with no `[ID]`-bearing ANCESTOR anywhere above it. The ordinary
    # untagged-heading case ascends and is captured; this fires only for a table in a
    # file whose headings carry no id at all, which is zero occurrences across the four
    # surveyed corpora today. Registered anyway, because an unregistered kind would be
    # reported with no severity the day it first appears.
    "near_miss_id_table": "warning",
    # (QUAL) SL-21 / SL-22 — a qualifier in the KEY REGION. Warning for the same reason
    # as everything above it: these land on corpora that have been authoring the shape
    # for months, and an error-severity introduction red-gates them all on day one. The
    # ratchet is the same — promote per-corpus once clean.
    "near_miss_qualified_key_paren": "warning",
    "near_miss_qualified_key_dash": "warning",
    # [C8] Era 0 -- the maintainer's 2026-08-11 `- [ID]: description` ruling (review V4).
    # Warning at introduction, same reasoning as every kind above it.
    "near_miss_id_colon": "warning",
}


def find_unaccounted(corpus: "factlattice.ParsedCorpus",
                     kinds: Optional[List[str]] = None) -> List[LineVerdict]:
    """Every structure-shaped line in `corpus` that no parser branch consumed.

    Deterministic: files in parse order, lines in file order. `kinds` optionally
    narrows to a subset of `factlattice.coverage.UNACCOUNTED_KINDS`.
    """
    keep = set(kinds) if kinds else None
    out: List[LineVerdict] = []
    for cov in corpus.coverage:
        for v in account_file(cov):
            if not v.unaccounted:
                continue
            if keep is not None and v.kind not in keep:
                continue
            out.append(v)
    return out


def coverage_stats(corpus: "factlattice.ParsedCorpus") -> Dict[str, int]:
    """Whole-corpus line accounting: how many lines were read, ignored, or lost.

    Reported alongside the findings because the RATIO is the honest framing -- a rule
    whose ignorable set silently grew to swallow everything would show up here as
    findings going to zero while `ignorable` climbs, and that is worth being able to
    see at a glance.
    """
    total = consumed = ignorable = unaccounted = 0
    for cov in corpus.coverage:
        for v in account_file(cov):
            total += 1
            if v.kind == "consumed":
                consumed += 1
            elif v.unaccounted:
                unaccounted += 1
            else:
                ignorable += 1
    return {"lines": total, "consumed": consumed,
            "ignorable": ignorable, "unaccounted": unaccounted}
