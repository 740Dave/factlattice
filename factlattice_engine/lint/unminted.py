"""UNMINTED-REFERENCE-LIST lint — Era 0, C2 (PARSER-ENGINEERING-SPEC.md rev 2).

    A property value that IS a pure reference list — bracketed refs joined by
    separators, at most one short trailing gloss — whose KEY is NOT in the minting
    table, is a WARNING: the references are real, authored, well-formed, and
    invisible to every traversal. `lts_lint` reports the corpus clean; the graph
    the author thinks they wrote does not exist.

WHY A SEPARATE MODULE FROM `binding.py`
----------------------------------------
`binding.analyse()` answers this question for ONE property AT WRITE TIME (the
`lts_add_node` / `lts_add_property` receipt) — it can only ever see what is written
THROUGH it. This module asks the SAME question CORPUS-WIDE, over every property of
every ALREADY-PARSED node: the retrospective instrument the write-time receipt has
no way to be. `binding.is_pure_reference_list` is the ONE shared predicate; a second
copy of it here would be exactly the drift this project keeps re-finding (the
domain-verb table read by a reporting tool while the parser used its own — 18 days
of silent disagreement).

WHAT THIS DOES NOT DO
----------------------
It does not decide the key SHOULD be added to the minting table — `grounds-claim`,
`re-derive-via`, `documents`, `upholds`, `uses` are Acme DOMAIN vocabulary
(parser.py's REL_PROPERTY_EDGES comment block: "Hardcoding them here would reverse
5d546a9 ('the engine stops naming a deployment')"). It reports the SHAPE — a pure
reference list under a non-minting key — and leaves the "should this be a profile
registration, a rename onto an existing minting key, or left exactly as it is (an
author may want the descriptive property AND a `## Relationships` declaration, per
`binding.analyse`'s own summary text)" call to the author. `nearest_minting_alternatives`
is carried on every finding for that reason.

PREDICTED ≈186 (PARSER-ENGINEERING-SPEC.md rev 2 §6, C2, review §S4): grounds-claim 54,
re-derive-via 45, documents 28, upholds 14, uses 13, tail 32 — measured against the
ACME deployment corpora (all 8 live root sets), NOT this engine repo's own
fixtures/tests. See the Era 0 build report for what THIS repo's own corpus measures,
and why a small number here is a fact about scope, not a broken instrument.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import factlattice

from ..binding import (
    classify_edge_key,
    is_pure_reference_list,
    minting_keys_for,
    nearest_minting_alternatives,
)

ORIGIN = "unminted"
KIND = "unminted_reference_list"
# (the maintainer 2026-08-17, ruling 2a) A miscased declared verb is its OWN warning, distinct
# from the genuinely-undeclared alert — so the author sees "fix the casing", never
# "unknown verb", and a case typo can never inflate the register-this-verb worklist.
KIND_CASE_MISMATCH = "edge_verb_case_mismatch"

UNMINTED_SEVERITY: Dict[str, str] = {KIND: "warning", KIND_CASE_MISMATCH: "warning"}

# Parser-injected / derived keys: never author-declared vocabulary, never a candidate.
# `id_table` in particular is a DICT (mapping), which `is_pure_reference_list` already
# refuses (it only reads str/list-of-str) — excluded here anyway so the reason is a
# NAME, not "it happened not to match the shape test".
_EXCLUDED_KEYS = frozenset({"aliases", "co_defined_with", "id_table"})

# An edge-shaped body line `- KEY: VALUE`, used ONLY to reach keys that never became a
# parsed property because they broke the `^[a-z][a-z0-9_-]*$` key grammar — i.e. the
# UPPERCASE case-variant (`- Depends-On: [X]`). It is not a second copy of the minting
# grammar: it harvests a candidate KEY and hands it to the ONE classifier
# (`binding.classify_edge_key`), which alone decides declared / case-variant / undeclared.
_EDGE_LINE_RE = re.compile(r"^\s*-\s+([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(\S.*)$")


@dataclass(frozen=True)
class UnmintedRef:
    """One (node, key) whose value is a pure reference list under a non-minting key.

    `line_number` is the node's HEADING line, not the individual property line — the
    already-parsed `Node`/`properties` this module reads carries no per-property line
    number (that provenance exists only inside `LineCoverage`, which `key_loss.py`
    re-scans the raw text FOR; this module instead reuses `corpus.nodes` directly, at
    the cost of that precision). Reported honestly in the Era 0 build report.
    """

    source_file: str
    line_number: int
    node_id: str
    key: str
    value: str
    alternatives: Tuple[str, ...]

    def message(self) -> str:
        alt = (f" Nearest minting alternative(s): {', '.join(self.alternatives)}."
               if self.alternatives else " No close minting alternative found.")
        return (f"`{self.key}` on [{self.node_id}] is a pure reference list "
                f"({self.value[:80]!r}) but is not a minting key, so its bracketed "
                f"reference(s) are stored as text and no edge exists for them."
                + alt + " Keeping the key is legitimate (it may carry meaning no bare "
                "edge can) — but then declare the relationship in `## Relationships` "
                "as well, or the graph is missing it (`binding.analyse`'s own advice, "
                "applied retrospectively).")


def find_unminted_reference_lists(corpus: "factlattice.ParsedCorpus",
                                  profile: Any = None) -> List[UnmintedRef]:
    """Every (node, key) whose value IS a pure reference list under a NON-minting key.

    `profile` defaults to `corpus.profile` — so a corpus whose profile registers a key
    via `property_edges` correctly STOPS flagging it (that key now mints), matching
    `binding.minting_keys_for`'s own contract. ⛔ Read the Era 0 build report's C6/C7
    findings before trusting that default on a corpus served through the LIVE MCP
    query engine: `corpus.profile` here is the `factlattice.corpus.parse()` profile,
    and registered `property_edges` do not reach that entry point's actual edge build
    either — this predicate is about SHAPE, not about whether the registration is
    live, and a `profile` explicitly passed always wins.
    """
    prof = profile if profile is not None else getattr(corpus, "profile", None)
    minting = minting_keys_for(prof)
    out: List[UnmintedRef] = []
    for node in corpus.nodes:
        for key, value in (node.properties or {}).items():
            if key in minting or key in _EXCLUDED_KEYS:
                continue
            if not is_pure_reference_list(value):
                continue
            # ⛔ A CASE-VARIANT of a declared verb (`depends_on` for `depends-on`) is NOT
            # a genuinely-undeclared verb — it is a trivially-fixable CASE error (the maintainer's
            # ruling 2a). It is reported on its OWN channel (`edge_verb_channels`) and
            # MUST NOT land in this per-occurrence undeclared-reference alert.
            if classify_edge_key(key, prof)[0] == "case_variant":
                continue
            if isinstance(value, str):
                rendered = value
            else:
                rendered = "; ".join(v for v in value if isinstance(v, str))
            out.append(UnmintedRef(
                source_file=node.source_file, line_number=node.line_range[0],
                node_id=node.id, key=key, value=rendered,
                alternatives=tuple(nearest_minting_alternatives(key)),
            ))
    return out


def edge_verb_channels(corpus: "factlattice.ParsedCorpus",
                       profile: Any = None) -> Dict[str, Any]:
    """The THREE de-duplicated edge-verb channels of the maintainer's 2026-08-17 rulings, off the
    ONE shared classifier so lint (V12) and preview (V12b) cannot disagree:

      `undeclared_edge_verbs`      (ruling 2b) — distinct genuinely-unknown verbs: a
                                   pure reference list under a key no declared verb
                                   matches even by case. The POSITIVE ALERT list an
                                   author/architect uses to decide register-vs-typo.
      `case_mismatch_edge_verbs`   (ruling 2a) — distinct keys that ARE a case/separator
                                   variant of a declared verb (`Depends-On`,`depends_on`
                                   for `depends-on`). A WARN, never a silent normalise-
                                   and-mint: the author fixes the casing and it mints
                                   through the existing path. `case_mismatch_canonical`
                                   names what each should be fixed TO.

    Declared verbs (exact case, core or corpus-registered) mint and appear in NEITHER.

    Two non-overlapping input sources feed the one classifier: PARSED properties (keys
    that satisfied the `^[a-z][a-z0-9_-]*$` grammar — every undeclared key and the
    lowercase separator variants) and UNCONSUMED raw lines (the uppercase variants that
    never became a property, e.g. `- Depends-On: [X]`, which the grammar rejected).
    """
    prof = profile if profile is not None else getattr(corpus, "profile", None)
    undeclared: set = set()
    case_map: Dict[str, str] = {}   # miscased key -> canonical declared verb

    # (1) PARSED properties — lowercase keys that reached `node.properties`.
    for node in corpus.nodes:
        for key, value in (node.properties or {}).items():
            if key in _EXCLUDED_KEYS or not is_pure_reference_list(value):
                continue
            kind, canon = classify_edge_key(key, prof)
            if kind == "case_variant":
                case_map[key] = canon
            elif kind == "undeclared":
                undeclared.add(key)

    # (2) UNCONSUMED raw lines — reach the uppercase case-variant the key grammar
    # rejected (so it is neither a property nor an edge, only an unaccounted line).
    for cov in getattr(corpus, "coverage", None) or []:
        lines = getattr(cov, "lines", None) or []
        fenced = getattr(cov, "fenced", None) or set()
        node_body = getattr(cov, "node_body", None) or {}
        try:
            unconsumed = cov.unconsumed_indices()
        except Exception:
            unconsumed = []
        for idx in unconsumed:
            if idx < 0 or idx >= len(lines) or idx in fenced:
                continue
            # Only lines inside a NODE BODY are candidates — an edge-shaped line in
            # free prose or a `## Relationships` block is not a miscased property.
            if idx not in node_body:
                continue
            m = _EDGE_LINE_RE.match(lines[idx])
            if not m:
                continue
            key, value = m.group(1), m.group(2).strip()
            if key in _EXCLUDED_KEYS or not is_pure_reference_list(value):
                continue
            # Only case-variants are claimed from the raw scan: an uppercase KNOWN verb
            # is unambiguously a miscased declared verb, whereas an uppercase UNKNOWN
            # word before a bracket is as likely prose as an intended edge, so it is left
            # to the existing near-miss / line-accounting channels rather than over-claimed.
            kind, canon = classify_edge_key(key, prof)
            if kind == "case_variant" and canon:
                case_map.setdefault(key, canon)

    return {
        "undeclared_edge_verbs": sorted(undeclared),
        "case_mismatch_edge_verbs": sorted(case_map),
        "case_mismatch_canonical": dict(sorted(case_map.items())),
    }


def edge_verb_warn_line(channels: Dict[str, Any]) -> Optional[str]:
    """The WARN string the maintainer specified for the case channel: `Case-mismatch {a, b, ...}`
    — the de-duplicated miscased verbs, so the author knows what to fix. `None` when the
    channel is empty (nothing to warn about)."""
    verbs = channels.get("case_mismatch_edge_verbs") or []
    if not verbs:
        return None
    return "Case-mismatch {" + ", ".join(verbs) + "}"


def unminted_stats(findings: List[UnmintedRef]) -> Dict[str, Any]:
    """Emitted on EVERY run, clean or not — same doctrine as `key_loss_stats`: a
    findings list alone cannot distinguish "this corpus has none of this shape" from
    "the rule was switched off"."""
    by_key: Dict[str, int] = {}
    for f in findings:
        by_key[f.key] = by_key.get(f.key, 0) + 1
    return {"unminted_reference_lists": len(findings),
            "distinct_keys": len(by_key),
            "by_key": dict(sorted(by_key.items(), key=lambda kv: (-kv[1], kv[0])))}


def summary_line(stats: Dict[str, Any]) -> str:
    return (f"-- unminted reference lists: {stats['unminted_reference_lists']} "
            f"propert{'y' if stats['unminted_reference_lists'] == 1 else 'ies'} across "
            f"{stats['distinct_keys']} key(s) carry only well-formed references and "
            f"mint no edge")
