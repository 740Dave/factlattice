"""VOCABULARY lint — Era 1 (SPEC amendments A1/A2/A3).

Surfaces `factlattice.parser`'s own `ParsedFile.vocabulary_findings` as lint `Finding`s:

  * `legacy_document`              — A1/§1.5.1: no `lts:` marker, parsed in legacy mode
  * `format_version_*`             — A1: the marker is present but empty/malformed/unknown
  * `undeclared_key`               — A2/§1.3.3: neither core, nor profile-declared, nor `x-`
  * `unregistered_verb`            — A3/§2.4.1: an unregistered verb in a strict document
  * `unrecognized_verb_line`       — A3/§2.4.1: a Relationships line whose verb resolved
                                     to nothing and which therefore minted NOTHING,
                                     silently, before this diagnostic existed

⛔ IT NEVER RE-DERIVES A CLASSIFICATION. Every finding here was produced by the parser,
beside the grammar that made the judgement, for the reason `malformed_ref.py` and
`coverage.py` state: a second copy of a rule is the drift this project keeps naming.
⛔ AND IT NEVER OVERRIDES THE PARSER'S SEVERITY. The whole content of amendment A1 is
that the DOCUMENT's `lts:` marker decides whether a vocabulary finding is a warning or
an error. A linter that applied its own severity table would silently take that decision
back, and the marker would become decorative — inert while reading as functional, which
is the exact failure (§1.3.1 constraint 3) this era exists to close. So the severity is
read from `finding.detail["severity"]`, which the parser set from the document's mode;
the map below is a FALLBACK for a finding that somehow carries none, never a policy.
"""
from __future__ import annotations

from typing import Dict, List

import factlattice

ORIGIN = "vocabulary"

KIND_LEGACY_DOCUMENT = "legacy_document"
KIND_UNDECLARED_KEY = "undeclared_key"
KIND_UNREGISTERED_VERB = "unregistered_verb"
KIND_UNRECOGNIZED_VERB_LINE = "unrecognized_verb_line"

VOCABULARY_KINDS = frozenset({
    KIND_LEGACY_DOCUMENT, KIND_UNDECLARED_KEY, KIND_UNREGISTERED_VERB,
    KIND_UNRECOGNIZED_VERB_LINE,
    "format_version_empty", "format_version_malformed", "format_version_unsupported",
})

# FALLBACK ONLY — see the module docstring. The document's mode is the authority.
VOCABULARY_SEVERITY: Dict[str, str] = {k: "warning" for k in VOCABULARY_KINDS}


def find_vocabulary_findings(corpus: "factlattice.ParsedCorpus") -> List:
    """Every vocabulary ParseFinding in `corpus`, in parse order."""
    return [f for f in getattr(corpus, "vocabulary_findings", ())
            if f.kind in VOCABULARY_KINDS]


def severity_for(finding) -> str:
    """The severity the PARSER assigned, per the document's own `lts:` mode."""
    return (finding.detail or {}).get("severity") or \
        VOCABULARY_SEVERITY.get(finding.kind, "warning")


def vocabulary_stats(findings: List) -> Dict[str, object]:
    by_kind: Dict[str, int] = {}
    strict_files = set()
    legacy_files = set()
    for f in findings:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
        mode = (f.detail or {}).get("mode")
        if mode == "strict":
            strict_files.add(f.source_file)
        elif f.kind == KIND_LEGACY_DOCUMENT:
            legacy_files.add(f.source_file)
    return {
        "vocabulary_findings": len(findings),
        "by_kind": dict(sorted(by_kind.items())),
        # ⛔ THE MIGRATION DENOMINATOR, and the reason `legacy_document` fires on every
        # unstamped file rather than being suppressed as noise: this count IS the
        # un-migrated tail, and Era 2 burns it down one document at a time. Reported
        # even at zero, so "nothing to migrate" is distinguishable from "the rule is off".
        "legacy_documents": len(legacy_files),
        "strict_documents_with_findings": len(strict_files),
    }


def summary_line(stats: Dict[str, object]) -> str:
    return (f"-- vocabulary: {stats['vocabulary_findings']} finding(s) "
            f"({stats['legacy_documents']} document(s) still in LEGACY mode — no "
            f"`lts: \"1.1\"` marker; SPEC §1.5.1) -- by kind: {stats['by_kind']}")
