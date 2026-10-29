"""MALFORMED-REF lint — Era 0, C1 (PARSER-ENGINEERING-SPEC.md rev 2 / SPEC A4).

Surfaces `factlattice.parser`'s own `ParseFinding(kind="malformed_ref")` — emitted at
every REF-EXPECTING POSITION (SPEC A4's closed list: minting-key property values,
Relationships operands, Causal operands, heading id-candidates) — as lint `Finding`s.

Reads `ParsedCorpus.property_findings`; NEVER re-derives the classification. The
classifier (`factlattice.parser.classify_bracket_token`) lives beside the grammar it
reads (`_UPPER_ID_RE`, `_BRACKET_RE`), for the same reason `factlattice.coverage`
reads the parser's own `LineCoverage` instead of re-deriving "would this have
parsed?" — a second copy of a classification rule is the drift this project keeps
naming (the domain-verb table read by a reporting tool while the parser used its own
hardcoded one, 18 days of silent disagreement).

SEVERITY: warning, at introduction — the SAME argument `coverage.py` and `key_loss.py`
make: this rule lands on corpora that have been authoring the shape for as long as
`.lts` has existed (review §1.4-U1: 164 measured instances in the Acme
deployment), and an error-severity introduction red-gates every one of them on day
one. Era 0's own compatibility contract is explicit: "findings only... nothing
breaks" (PARSER-ENGINEERING-SPEC.md §7) — normalized-with-warning / rejected-with-
error is C5's Era-1 ratchet (the maintainer's 2026-08-16 ruling), not this rule's to impose.
"""
from __future__ import annotations

from typing import Dict, List

import factlattice
from factlattice.parser import FINDING_MALFORMED_REF

ORIGIN = "malformed_ref"
KIND = FINDING_MALFORMED_REF

MALFORMED_REF_SEVERITY: Dict[str, str] = {KIND: "warning"}


def find_malformed_refs(corpus: "factlattice.ParsedCorpus") -> list:
    """Every `malformed_ref` ParseFinding in `corpus`, in parse order.

    ⛔ Reads `corpus.property_findings` for what it IS: `factlattice.corpus.parse()`'s
    own re-parse of the corpus, which (like `mixed_property_block` /
    `nested_mapping_block` beside it) does NOT thread a `profile`'s registered
    `property_edges`/`domain_verbs` — see the Era 0 build report's C6/C7 findings.
    That means a malformed ref inside a value that a profile-registered minting key
    would otherwise have consumed can still surface here even where a fully-wired
    engine would mint the well-formed refs beside it and only flag the ill-formed
    one — narrower, not wider, than the fully-wired reading; never silence.
    """
    return [f for f in getattr(corpus, "property_findings", ())
           if f.kind == FINDING_MALFORMED_REF]


def malformed_ref_stats(findings: list) -> Dict[str, int]:
    by_position: Dict[str, int] = {}
    for f in findings:
        pos = f.detail.get("position", "?")
        by_position[pos] = by_position.get(pos, 0) + 1
    return {"malformed_refs": len(findings), "by_position": by_position}


def summary_line(stats: Dict[str, int]) -> str:
    return (f"-- malformed refs: {stats['malformed_refs']} bracket token(s) are "
            f"id-SHAPED but not well-formed ids at a ref-expecting position "
            f"(SPEC A4 MALFORMED-REF) and became neither an edge nor a diagnostic "
            f"before this rule -- by position: {stats['by_position']}")
