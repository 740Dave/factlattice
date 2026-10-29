"""THE core `lint()` -- invocation-agnostic: one core function over the factlattice
parse tree, N thin adapters. `__main__.py` (this layer's CLI) is one adapter; the
`lts_lint` MCP tool is another -- both `import` and call this exact function rather
than re-derive any of it, which is the whole point of keeping this a library call and
not just an argparse `main()`.

Consumes `factlattice` (the shared parser) as a LIBRARY: `factlattice.parse()` ->
`ParsedCorpus{nodes, edges, diagnostics}`. This module does NOT parse `.lts` itself
and does NOT re-implement any extraction or dangling rule -- it only (a) unifies
factlattice's own `Diagnostic`s into severity-tagged `Finding`s (severity is linter
POLICY, deliberately left out of the neutral parser per `factlattice.corpus.Diagnostic`'s
own docstring), and (b) adds the orphan half of the dangle-lint (`dangle.py`) that the
parser has no reason to compute itself (it is a lint-level "what does nobody point at"
query, not a parse-time concern). Because it reuses the engine's own parser, a corpus
that lints clean is a corpus that ingests clean -- one code path, no drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import factlattice

from ..budget import budget_slice, declare, token_estimate
from .corpus_files import (
    CORPUS_FILE_SEVERITY,
    ORIGIN as CORPUS_FILES_ORIGIN,
    find_corpus_file_issues,
)
from .coverage import COVERAGE_SEVERITY, ORIGIN as COVERAGE_ORIGIN, find_unaccounted
from .claim_id import (
    CLAIM_GENRE_PREFIXES, CLAIM_ID_SEVERITY, ORIGIN as CLAIM_ID_ORIGIN, census as claim_id_census,
    census_line as claim_id_census_line, find_claim_ids,
    find_read_suppressing_names,
)
from .dangle import (
    DIR_AMBIGUOUS_REF,
    DIR_UNQUALIFIED_REF,
    ambiguous_reference_warn_line,
    classify_unqualified_references,
    find_dangles,
    unqualified_reference_targets,
    unqualified_warn_line,
)
from .id_hijack import (
    ID_HIJACK_SEVERITY, ORIGIN as ID_HIJACK_ORIGIN, find_id_hijacks,
)
from .key_loss import (
    KEY_LOSS_SEVERITY,
    KIND_SUMMARY as KEY_LOSS_SUMMARY,
    ORIGIN as KEY_LOSS_ORIGIN,
    _KIND_FOR,
    _message as _key_loss_message,
    find_key_losses,
    key_loss_stats,
    summary_line as key_loss_summary_line,
)
from .vocabulary import (
    ORIGIN as VOCABULARY_ORIGIN,
    find_vocabulary_findings,
    severity_for as vocabulary_severity_for,
    summary_line as vocabulary_summary_line,
    vocabulary_stats,
)
from .canonical import (
    CANONICAL_SEVERITY,
    ORIGIN as CANONICAL_ORIGIN,
    find_canonical_findings,
)
from .review_edges import (
    ORIGIN as REVIEW_EDGES_ORIGIN,
    REVIEW_EDGE_SEVERITY,
    find_review_edge_findings,
)
from .malformed_ref import (
    MALFORMED_REF_SEVERITY,
    ORIGIN as MALFORMED_REF_ORIGIN,
    find_malformed_refs,
    malformed_ref_stats,
    summary_line as malformed_ref_summary_line,
)
from .profile_rules import (
    PROFILE_SEVERITY,
    ORIGIN as PROFILE_ORIGIN,
    find_profile_violations,
    lifecycle_report,
    profile_is_loaded,
)
from .unminted import (
    KIND as KIND_UNMINTED,
    KIND_CASE_MISMATCH,
    UNMINTED_SEVERITY,
    ORIGIN as UNMINTED_ORIGIN,
    edge_verb_channels,
    edge_verb_warn_line,
    find_unminted_reference_lists,
    summary_line as unminted_summary_line,
    unminted_stats,
)

# --- severity policy (linter's call, not the parser's; override via the lint()
# kwargs below) -----------------------------------------------------------------
# parse_error / collisions: real corpus damage (an unreadable file; a duplicate
# id silently shadowing a node) -> error, fails a CI gate.
# dangling / unregistered_ns / unresolved_target (generic structure lint):
# "preserve and flag" signals that are common mid-authoring and not automatically
# corpus-breaking -> warning by default.
STRUCTURE_SEVERITY: Dict[str, str] = {
    "parse_error": "error",
    "collisions": "error",
    "dangling": "warning",
    "unregistered_ns": "warning",
    "unresolved_target": "warning",
}

# The dedicated dangle-lint (capability 2) is the "genuinely useful" one per the
# build brief -- so its FORWARD class defaults to error (a reference to nothing
# is an unambiguous bug and this mode exists specifically to catch it); ORPHAN
# defaults to warning (an unreferenced-but-defined node is often legitimate --
# a fresh node, a document root, an entry point -- so it should not fail CI by
# default). Flagged for the maintainer: promote/demote either default via
# `dangle_severity=` if this call is wrong for how the graph is actually used.
DANGLE_SEVERITY: Dict[str, str] = {
    "forward": "error",
    "orphan": "warning",
}

# `to_dict(token_budget=None)` means "carry everything" — expressed as a budget nothing
# can exceed rather than as a branch around the slicer, so the UNBOUNDED path runs the
# SAME code as the bounded one and cannot drift from it. A bypass would be a second
# implementation wearing an `if`.
_UNBOUNDED = 1 << 62

# ⛔ THE RULE-SET, NAMED. Every `origin` this linter can emit, in one place.
#
# A verdict is only meaningful AGAINST A RULE-SET, and until this existed there was no way
# to say which one. The concrete cost: `test_lint_declares_reach_on_a_clean_corpus`
# asserted `findings == []` on `specs/`, which passed for as long as the rule-set happened
# not to grow — and then the KEY-LOSS rule landed on a parallel branch, found 9 real
# findings in that corpus, and the assertion failed AT INTEGRATION, where it looks exactly
# like a landing bug rather than like a corpus that was never clean with respect to a rule
# nobody had written yet.
#
# ⇒ A CLEAN-CORPUS CLAIM DECLARES THE ORIGINS IT IS CLEAN WITH RESPECT TO. Then a new rule
#   shows up as a NEW UNDECLARED ORIGIN — a one-line, obviously-correct amendment — instead
#   of a red suite. This is [INV-DECLARED-EXTENT] applied to the engine's own tests: a
#   `clean` with no denominator is indistinguishable from a check that inspected nothing.
ORIGINS: Tuple[str, ...] = (
    "structure", "dangle", COVERAGE_ORIGIN, KEY_LOSS_ORIGIN, CORPUS_FILES_ORIGIN,
    PROFILE_ORIGIN, ID_HIJACK_ORIGIN, CLAIM_ID_ORIGIN,
    # Era 0 (PARSER-ENGINEERING-SPEC.md rev 2, C1/C2) — additive, per the module's
    # own [INV-DECLARED-EXTENT] doctrine two lines up: a new rule is a new origin
    # here, a reviewed one-line diff, never a silent re-baseline.
    MALFORMED_REF_ORIGIN, UNMINTED_ORIGIN,
    # Era 1 (A1/A2/A3) — the vocabulary regimes. Same doctrine.
    VOCABULARY_ORIGIN,
    # canonical-edge (2026-10-03) -- `canonical:` lint; same doctrine.
    CANONICAL_ORIGIN,
    # review-extension edges (2026-10-07, Spec A A-E3) -- `attaches-to-hook` / `applies-rule` lint.
    REVIEW_EDGES_ORIGIN,
)


@dataclass
class Finding:
    """One reportable line, unifying a structure Diagnostic and a dangle Finding
    behind one shape so rendering / exit-code logic doesn't care which produced it."""

    origin: str            # "structure" | "dangle"
    kind: str               # Diagnostic.kind, or DangleFinding.direction
    severity: str            # "error" | "warning"
    message: str
    source_file: Optional[str]
    source_line: Optional[int]
    node_id: Optional[str] = None
    detail: dict = field(default_factory=dict)

    def location(self) -> str:
        f = self.source_file or "<corpus>"
        return f"{f}:{self.source_line}" if self.source_line else f

    def render(self) -> str:
        """`file:line: message` -- deterministic, zero-token, grep-able."""
        return f"{self.location()}: [{self.severity}] {self.origin}.{self.kind}: {self.message}"

    def token_count(self) -> int:
        """What this finding costs a caller's context ([ENGINE-BUDGET]).

        Uses `factlattice.token_estimate` — THE estimator `Node.token_count` is produced
        with — so a finding and a node are priced by the same ruler against the same
        budget. `detail` is included because it is served, and a cost that omits part of
        the payload is a budget that overspends by however much it forgot: `detail`
        carries the offending line's `text` for every coverage finding, which is the
        bulkiest thing in the response.
        """
        return token_estimate(
            f"{self.origin}{self.kind}{self.severity}{self.message}"
            f"{self.source_file}{self.node_id}{self.detail}")

    def to_dict(self) -> dict:
        return {
            "origin": self.origin, "kind": self.kind, "severity": self.severity,
            "message": self.message, "source_file": self.source_file,
            "source_line": self.source_line, "node_id": self.node_id, "detail": self.detail,
        }


@dataclass
class LintResult:
    findings: List[Finding] = field(default_factory=list)
    node_count: int = 0
    edge_count: int = 0
    profile_domain: Optional[str] = None
    profile_path: Optional[str] = None
    # ⛔ THE VERDICT NAMES ITS OWN VOCABULARY. `{resolved_from, path, domain_id,
    # available, corpus_root[, detail, conflict, warning]}` — how the profile got into
    # scope, not just which one. A lint result that reports only findings cannot
    # distinguish "this corpus is clean" from "the profile-driven rules were switched
    # off", and that ambiguity is what let a landed, parsing, 32-prefix profile sit
    # wired to nothing while lint reported confidently for weeks. `available: False`
    # carries an explicit `warning` string saying V1-V6 were inert.
    profile_resolution: Optional[dict] = None
    # V6's DELIVERABLE, not a side effect: the decision-lifecycle view the profile's
    # `decision_lifecycle` block declared and nothing ever served -- including
    # `made_but_not_implemented`, the "decisions made but never implemented" query.
    # None when the corpus loads no profile. It rides out on `to_dict()`, so the
    # `lts_lint` MCP tool serves it without any change to the tool.
    lifecycle: Optional[dict] = None
    # `.lts` domain profiles found in the linted set and excluded from the content
    # graph (`document_tag: PROFILE`). See `factlattice.parser.is_profile_document`.
    profile_documents: List[str] = field(default_factory=list)
    # ⛔ EMITTED ON EVERY RUN, INCLUDING A CLEAN ONE. authored-vs-minted for the whole
    # linted set. A findings list alone cannot distinguish "this corpus keeps what it
    # authors" from "the key-loss rule was switched off", and the second reading is the
    # exact condition that let `overview.lts` sit 19 fields short for a month
    # while lint reported clean. Same reasoning as `profile_line()`.
    key_loss: Optional[dict] = None
    # ⛔ EMITTED ON EVERY RUN, INCLUDING A CLEAN ONE — same doctrine as `key_loss`,
    # Era 0's C2 (PARSER-ENGINEERING-SPEC.md rev 2). A findings list alone cannot
    # distinguish "this corpus authors no pure reference lists under a non-minting
    # key" from "the rule was switched off".
    unminted: Optional[dict] = None
    unqualified_references: Optional[dict] = None
    # ⛔ EMITTED ON EVERY RUN. Era 0's C1 counterpart to `unminted` — same reasoning.
    malformed_ref: Optional[dict] = None
    # ⛔ EMITTED ON EVERY RUN. The claim-shaped-identifier CENSUS — a trend with a
    # DENOMINATOR, per prefix. The findings are a warning stream; this is the number that
    # can be watched and that can FALL, and it is deliberately not a rename worklist.
    claim_ids: Optional[dict] = None
    # ⛔ EMITTED ON EVERY RUN. Era 1's A1/A2/A3 census. `legacy_documents` is the
    # MIGRATION DENOMINATOR — the count of documents with no `lts:` marker, i.e. the
    # un-migrated tail Era 2 burns down. Reported at zero for the same reason every
    # other census here is: so "nothing to migrate" is distinguishable from "off".
    vocabulary: Optional[dict] = None
    # ⛔ THE SCOPE THE RUN WAS POINTED AT, AS A FIRST-CLASS FIELD — [FL-RECALL-SCOPE-VERDICT].
    # Measured 2026-08-13: one spec file linted ALONE returned 12 errors and
    # `profile.resolved_from: none`; the SAME file inside its two-root corpus returned 0.
    # All 12 were phantom dangles — 10 resolve against a sibling root, 2 against another.
    # The file-scope response DID carry an honest prose warning, and it did not help:
    # ⇒ THE ERROR COUNT IS THE FIELD CALLERS QUOTE, AND A 12 SITTING BESIDE A PROSE
    #   CAVEAT TRAVELS AS A 12.
    # And the inverse is worse: a caller who lints at file scope and sees 0 errors on a
    # file whose profile-driven rules were ALL inert has a clean report that checked
    # almost nothing. Same shape as `lost: 0` at 100% coverage not being a gate.
    scope: Optional[dict] = None
    # Checks that did NOT run, by name. When this is non-empty the machine report emits
    # `errors: null` rather than a number — see `to_dict`.
    rules_inert: List[str] = field(default_factory=list)
    corpus: Optional["factlattice.ParsedCorpus"] = field(default=None, repr=False)

    @property
    def has_errors(self) -> bool:
        return any(f.severity == "error" for f in self.findings)

    def exit_code(self) -> int:
        """0 if clean or warnings-only; 1 if any error-severity finding."""
        return 1 if self.has_errors else 0

    def by_origin(self, origin: str) -> List[Finding]:
        return [f for f in self.findings if f.origin == origin]

    def summary(self) -> Dict[str, int]:
        s: Dict[str, int] = {}
        for f in self.findings:
            key = f"{f.origin}.{f.kind}"
            s[key] = s.get(key, 0) + 1
        s["errors"] = sum(1 for f in self.findings if f.severity == "error")
        s["warnings"] = sum(1 for f in self.findings if f.severity == "warning")
        s["total"] = len(self.findings)
        return s

    def by_validator(self) -> Dict[str, int]:
        """`{'V1': 1, 'V3': 12, …}` over the profile-validation findings only --
        the triage worklist the severity policy exists to make survivable."""
        counts: Dict[str, int] = {}
        for f in self.findings:
            v = f.detail.get("validator") if f.origin == PROFILE_ORIGIN else None
            if v:
                counts[str(v)] = counts.get(str(v), 0) + 1
        return counts

    def to_dict(self, token_budget: Optional[int] = None, offset: int = 0) -> dict:
        """The machine report. [INV-DECLARED-REACH]: whatever slice of `findings` this
        carries, the response DECLARES it.

        `token_budget=None` (the default, and what the CLI passes) carries every
        finding — the CLI's consumer is a terminal, not a context window. The MCP
        surface passes the engine default, because ITS consumer is an Intelligence and
        an unbounded lint over a real corpus is the largest response this engine can
        produce (measured: 667 findings / ~203k tokens over one project's specs). Either
        way `count`/`total_matched`/`truncated`/`cursor` are ALWAYS present, so a caller
        never has to know which mode it got — an absent field is indistinguishable from
        a whole answer, which is the failure mode this closes.

        ⛔ `summary` IS FOLDED OVER EVERY FINDING, NEVER OVER THE CARRIED PAGE. Note the
        order below: the summary is computed BEFORE the slice. `lts_lint` answers "is
        this CORPUS clean"; a summary folded over one page answers "what is in this
        response" under the same name, and reports a state the system is not in while
        looking exactly like a measurement. `summary["total"] == total_matched` always.
        """
        # BEFORE the slice, and deliberately so — see the docstring.
        summary = self.summary()
        # ⛔ AN INERT-RULE RUN EMITS `errors: null`, NOT A NUMBER — [FL-RECALL-SCOPE-VERDICT]
        # / proposal item 3. A headline computed with profile-driven rules switched off is
        # not a smaller headline, it is a headline about a different question, and the
        # only way to stop it travelling without its caveat is to make it UNQUOTABLE.
        # `errors_observed` keeps the raw number for a caller who wants it and has read
        # `rules_inert` to get there — the caveat is now upstream of the number instead of
        # beside it.
        if self.rules_inert:
            summary = {**summary, "errors_observed": summary["errors"],
                       "warnings_observed": summary["warnings"],
                       "errors": None, "warnings": None,
                       "suppressed_because": (
                           "these checks did not run: " + ", ".join(self.rules_inert)
                           + ". A count computed with them inert answers a DIFFERENT "
                             "question and must not travel as the verdict.")}
        emitted, truncated, cursor, total = budget_slice(
            self.findings, offset,
            token_budget if token_budget is not None else _UNBOUNDED,
            keyfn=lambda f: {"token_count": f.token_count()})
        out = {
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "profile_domain": self.profile_domain,
            "profile_path": self.profile_path,
            "profile": self.profile_resolution,
            # Files in the linted set that are VOCABULARY, not content: `.lts` domain
            # profiles, parsed in full but excluded from the graph. Reported because a
            # reader who sees a lower node_count deserves to know which files the
            # linter did not judge, and why.
            "profile_documents": list(self.profile_documents),
            "summary": summary,
            "findings": [f.to_dict() for f in emitted],
            **declare(emitted, truncated, cursor, total),
            # [INV-DECLARED-EXTENT]: what this verdict was computed OVER, beside what it
            # found. `scope` names the run; `rules_inert` names what did not run.
            "scope": self.scope,
            "rules_inert": list(self.rules_inert),
            "extent": {
                "scope": self.scope,
                "rules_inert": list(self.rules_inert),
                "nodes_in_scope": self.node_count,
                "edges_in_scope": self.edge_count,
                "files_in_scope": (len(self.corpus.coverage)
                                   if self.corpus is not None else None),
                "profile": self.profile_resolution,
                "keys_consulted": ["<every parsed line>"],
            },
        }
        if self.key_loss is not None:
            out["key_loss"] = self.key_loss
        if self.unminted is not None:
            out["unminted"] = self.unminted
        if self.unqualified_references is not None:
            out["unqualified_references"] = self.unqualified_references
        if self.malformed_ref is not None:
            out["malformed_ref"] = self.malformed_ref
        if self.claim_ids is not None:
            out["claim_ids"] = self.claim_ids
        if self.vocabulary is not None:
            out["vocabulary"] = self.vocabulary
        by_validator = self.by_validator()
        if by_validator:
            out["profile_validators"] = by_validator
        if self.lifecycle is not None:
            out["lifecycle"] = self.lifecycle
        return out

    def profile_line(self) -> str:
        """One grep-able line naming the vocabulary this verdict was judged against.

        Printed on EVERY run, clean or not — "profile: none (…)" is the single most
        important line the linter can emit, because it is the one that tells a reader
        the rules were off rather than satisfied.
        """
        r = self.profile_resolution or {}
        how = r.get("resolved_from", "unknown")
        if not r.get("available"):
            extra = f" [{r['detail']}]" if r.get("detail") else ""
            return (f"-- profile: NONE IN SCOPE (resolved_from={how}{extra}) -- "
                    "profile-driven rules V1-V6 were INERT; a clean result here means "
                    "nothing was checked")
        line = (f"-- profile: {r.get('domain_id') or 'unnamed'} "
                f"(resolved_from={how}, path={r.get('path')})")
        if r.get("detail"):
            line += f" [{r['detail']}]"
        if r.get("conflict"):
            line += f" ⚠ CONFLICTING REGISTRATIONS: {r['conflict']}"
        return line

    def excluded_line(self) -> Optional[str]:
        """Names the `.lts` domain profiles in the linted set that were parsed but kept
        OUT of the content graph. Printed whenever there is one, so a reader can never
        wonder why the node count is lower than the file count suggests."""
        if not self.profile_documents:
            return None
        return ("-- excluded from content (domain profiles, document_tag=PROFILE): "
                + ", ".join(sorted(self.profile_documents)))

    def vocabulary_line(self) -> Optional[str]:
        """The Era-1 vocabulary census, on every run — see `vocabulary`."""
        if self.vocabulary is None:
            return None
        return vocabulary_summary_line(self.vocabulary)

    def claim_id_line(self) -> Optional[str]:
        """The census, on every run — see `claim_ids`."""
        if self.claim_ids is None:
            return None
        return claim_id_census_line(self.claim_ids)

    def key_loss_line(self) -> Optional[str]:
        """The counter that must be able to FALL. Printed on every run — see `key_loss`."""
        if self.key_loss is None:
            return None
        return key_loss_summary_line(self.key_loss)

    def unminted_line(self) -> Optional[str]:
        """Printed on every run — see `unminted`."""
        if self.unminted is None:
            return None
        return unminted_summary_line(self.unminted)

    def malformed_ref_line(self) -> Optional[str]:
        """Printed on every run — see `malformed_ref`."""
        if self.malformed_ref is None:
            return None
        return malformed_ref_summary_line(self.malformed_ref)

    def render_text(self) -> str:
        if not self.findings:
            lines = [f"lint clean -- {self.node_count} nodes, {self.edge_count} edges",
                     self.profile_line()]
        else:
            lines = [f.render() for f in self.findings]
            s = self.summary()
            lines.append(f"-- {s['total']} finding(s): {s['errors']} error(s), "
                         f"{s['warnings']} warning(s)")
            lines.append(self.profile_line())
        kl = self.key_loss_line()
        if kl:
            lines.append(kl)
        um = self.unminted_line()
        if um:
            lines.append(um)
        mr = self.malformed_ref_line()
        if mr:
            lines.append(mr)
        vl = self.vocabulary_line()
        if vl:
            lines.append(vl)
        ci = self.claim_id_line()
        if ci:
            lines.append(ci)
        excluded = self.excluded_line()
        if excluded:
            lines.append(excluded)
        return "\n".join(lines)


def lint(source=None, *, paths=None, profile=None,
          structure: bool = True, dangle: bool = False, coverage: bool = True,
          profile_rules: bool = True, key_loss: bool = True, corpus_files: bool = True,
          id_hijack: bool = True, claim_id: bool = True,
          unminted: bool = True, malformed_ref: bool = True,
          vocabulary: bool = True, canonical: bool = True, review_edges: bool = True,
          structure_severity: Optional[Dict[str, str]] = None,
          dangle_severity: Optional[Dict[str, str]] = None,
          coverage_severity: Optional[Dict[str, str]] = None,
          profile_severity: Optional[Dict[str, str]] = None,
          key_loss_severity: Optional[Dict[str, str]] = None,
          corpus_file_severity: Optional[Dict[str, str]] = None,
          id_hijack_severity: Optional[Dict[str, str]] = None,
          claim_id_severity: Optional[Dict[str, str]] = None,
          unminted_severity: Optional[Dict[str, str]] = None,
          malformed_ref_severity: Optional[Dict[str, str]] = None,
          canonical_severity: Optional[Dict[str, str]] = None,
          review_edges_severity: Optional[Dict[str, str]] = None,
          claim_genre_prefixes: Optional[Iterable[str]] = None) -> LintResult:
    """Lint a FactLattice corpus. Mirrors `factlattice.parse()`'s own calling
    convention 1:1 (`source` XOR `paths`, `profile` pass-through) so a caller who
    already knows how to call `factlattice.parse()` already knows how to call this.

    `source`   -- a directory (the whole corpus, recursively, matching what
                  `factlattice`/lts-engine's `build_index` would ingest) OR a single
                  `.lts` file.
    `paths`    -- an explicit list of `.lts` files instead of `source` (file-set
                  mode -- see the CAVEAT below).
    `profile`  -- path/dict/`factlattice.Profile`/None. When None it is RESOLVED
                  (see below); the outcome is always REPORTED.

    ------------------------------------------------------------------------
    THE RESULT DECLARES ITS PROFILE  (`result.profile_resolution` / `["profile"]`)
    ------------------------------------------------------------------------
    Every result names the vocabulary it judged against: the resolved PATH, the
    domain id, and HOW it was resolved --
        `explicit-argument` | `corpus-config` | `directory-auto-discovery` | `none`
    `none` is a first-class REPORTED outcome carrying an explicit warning, because a
    verdict that does not say what vocabulary it used is not a verdict: a caller
    cannot tell "clean" from "the rules were switched off". That ambiguity is exactly
    how a landed, parsing domain profile stayed wired to nothing while lint kept
    reporting confidently.

    ⛔ `source=` AND `paths=` RESOLVE THE SAME PROFILE. They are the same question
    asked two ways and they now get the same answer: `factlattice.parse` derives the
    file-set's `common_root` (the SAME helper the staging layout uses) and resolves
    against it. Before this, `paths=` skipped resolution entirely, so 288 profile
    findings over one corpus appeared or vanished purely on whether the caller named
    a directory or listed its files. If a future change makes them legitimately
    differ, it must SAY so in `profile_resolution`, not diverge quietly.

    `structure=True`  -- capability 1: run the generic format/structure lint,
                          surfacing EVERY factlattice `Diagnostic` (parse_error,
                          collisions, dangling, unregistered_ns, unresolved_target)
                          severity-tagged per `structure_severity`.
    `coverage=True`   -- capability 3 (DEFAULT ON): the LINE-ACCOUNTING invariant
                          ([FL-LINE-ACCOUNTING], `coverage.py`) -- every non-blank
                          line is CONSUMED by a parser branch or CLASSIFIED as
                          ignorable; anything else is a warning naming the file, the
                          line, the text, and the branch that nearly took it. ON by
                          default on purpose: every other rule here is a rule about
                          content the parser DID read, so an opt-in flag would
                          reproduce the exact failure mode this rule exists to close
                          (nobody knew to ask). All findings are warnings, so no
                          exit code and no CI gate changes; ratchet a clean corpus
                          up with `coverage_severity=`.
    `profile_rules=True` -- capability 4 (DEFAULT ON): PROFILE-DRIVEN VALIDATION,
                          V1-V11 (`profile_rules.py`) -- registered prefixes, document
                          tags, required/recommended properties per node family, enum
                          values, decision-lifecycle stages, §7.1 confidence blocks,
                          §8.1 `invalidating_assumption`, §1.5 frontmatter, §12.1
                          density. Reads the TYPED `Profile` accessors, never
                          `profile.raw`. INERT (zero findings, no crash) for a corpus
                          that loads no profile -- the pass is opt-in by authoring one.
                          Every finding lands at `warning`; ratchet per validator with
                          `profile_severity=`.
    `dangle=True`     -- capability 2: ALSO run the bidirectional dangle-lint
                          (`dangle.find_dangles`): forward (a `[PREFIX-NAME]` ref
                          defined nowhere) + orphan (a defined node nothing
                          references). When BOTH `structure` and `dangle` are on,
                          the generic `dangling` diagnostic is suppressed from the
                          structure section (it reappears as `dangle.forward` with
                          clearer forward/orphan framing) so the same fact is never
                          printed twice under two different names.

    CAVEAT (file-set mode / `paths=` or a glob of individual files): factlattice
    resolves dangling refs over EXACTLY the given file set. A ref that targets a
    node defined in a sibling file OUTSIDE that set will read as dangling even
    though the full corpus resolves it cleanly -- this is correct, documented
    behavior (you asked to lint exactly those files), not a bug, but it means a
    partial glob is the wrong tool for a "is the WHOLE corpus dangle-free" question
    -- pass the corpus directory (`source=`) for that.
    """
    ssev = {**STRUCTURE_SEVERITY, **(structure_severity or {})}
    dsev = {**DANGLE_SEVERITY, **(dangle_severity or {})}
    csev = {**COVERAGE_SEVERITY, **(coverage_severity or {})}
    psev = {**PROFILE_SEVERITY, **(profile_severity or {})}
    klsev = {**KEY_LOSS_SEVERITY, **(key_loss_severity or {})}
    cfsev = {**CORPUS_FILE_SEVERITY, **(corpus_file_severity or {})}
    ihsev = {**ID_HIJACK_SEVERITY, **(id_hijack_severity or {})}
    cisev = {**CLAIM_ID_SEVERITY, **(claim_id_severity or {})}
    umsev = {**UNMINTED_SEVERITY, **(unminted_severity or {})}
    mrsev = {**MALFORMED_REF_SEVERITY, **(malformed_ref_severity or {})}
    casev = {**CANONICAL_SEVERITY, **(canonical_severity or {})}
    resev = {**REVIEW_EDGE_SEVERITY, **(review_edges_severity or {})}

    corpus = factlattice.parse(source, paths=paths, profile=profile)
    findings: List[Finding] = []

    # RULING C (2026-08-17): a BARE dangling ref with a NAMESPACE-QUALIFIED twin is its
    # OWN channel (Unqualified-reference / Ambiguous), never a plain dangle. Computed off
    # the SAME resolution the dangle stream uses (corpus.diagnostics_of('dangling')), so
    # lint and preview cannot disagree. Inert on the current id model (namespaces are
    # stripped from ids) — measured 0; see `classify_unqualified_references`.
    unqualified_report: Optional[dict] = None
    _unq_targets = frozenset()
    if structure:
        unqualified_report = classify_unqualified_references(corpus)
        _unq_targets = unqualified_reference_targets(unqualified_report)

    if structure:
        for d in corpus.diagnostics:
            if dangle and d.kind == "dangling":
                continue  # reported as dangle.forward instead; see docstring
            if d.kind == "dangling" and d.node_id in _unq_targets:
                continue  # RULING C: routed to the Unqualified-reference channel, not here
            findings.append(Finding(
                origin="structure", kind=d.kind, severity=ssev.get(d.kind, "warning"),
                message=d.message, source_file=d.source_file, source_line=d.source_line,
                node_id=d.node_id, detail=d.detail,
            ))

    if dangle:
        for df in find_dangles(corpus):
            if df.direction == "forward" and df.node_id in _unq_targets:
                continue  # RULING C channel, not a plain forward-dangle
            findings.append(Finding(
                origin="dangle", kind=df.direction, severity=dsev.get(df.direction, "warning"),
                message=df.message, source_file=df.source_file, source_line=df.source_line,
                node_id=df.node_id, detail=df.detail,
            ))

    if unqualified_report:
        for bare, ns in unqualified_report["unqualified_references"].items():
            findings.append(Finding(
                origin="dangle", kind=DIR_UNQUALIFIED_REF, severity="warning",
                message=(f"bare reference [{bare}] does not resolve, but exactly one "
                         f"namespace-qualified definition matches: did you mean [{ns}]? "
                         f"(strict identity kept — the engine does not equate them)"),
                source_file=None, source_line=None, node_id=bare,
                detail={"suggested_target": ns},
            ))
        for bare, cands in unqualified_report["ambiguous_references"].items():
            findings.append(Finding(
                origin="dangle", kind=DIR_AMBIGUOUS_REF, severity="warning",
                message=(f"bare reference [{bare}] does not resolve and matches "
                         f"{len(cands)} namespace-qualified definitions "
                         f"({', '.join(cands)}) — genuinely ambiguous, qualify it"),
                source_file=None, source_line=None, node_id=bare,
                detail={"candidates": cands},
            ))

    if coverage:
        for v in find_unaccounted(corpus):
            findings.append(Finding(
                origin=COVERAGE_ORIGIN, kind=v.kind,
                severity=csev.get(v.kind, "warning"), message=v.message(),
                source_file=v.source_file, source_line=v.line_number,
                node_id=v.detail.get("node_id"),
                detail={**v.detail, "text": v.text, "near_branch": v.near_branch},
            ))

    key_loss_report: Optional[dict] = None
    if key_loss:
        for d in find_key_losses(corpus):
            findings.append(Finding(
                origin=KEY_LOSS_ORIGIN, kind=_KIND_FOR[d.confidence],
                severity=klsev.get(_KIND_FOR[d.confidence], "warning"),
                message=_key_loss_message(d),
                source_file=d.source_file, source_line=d.line_number, node_id=d.node_id,
                detail={"shapes": list(d.shapes), "key_region": d.key_region,
                        "normalised_key": d.normalised_key,
                        "key_parsed_elsewhere": d.key_parsed_elsewhere,
                        "ratified_kind": d.ratified_kind,
                        "seen_by_ratified_lint": d.seen_by_ratified_lint,
                        "text": d.text},
            ))
        key_loss_report = key_loss_stats(corpus)

    unminted_report: Optional[dict] = None
    if unminted:
        um_findings = find_unminted_reference_lists(corpus)
        for u in um_findings:
            findings.append(Finding(
                origin=UNMINTED_ORIGIN, kind=KIND_UNMINTED,
                severity=umsev.get(KIND_UNMINTED, "warning"), message=u.message(),
                source_file=u.source_file, source_line=u.line_number, node_id=u.node_id,
                detail={"key": u.key, "value": u.value, "alternatives": list(u.alternatives)},
            ))
        unminted_report = unminted_stats(um_findings)
        # (the maintainer 2026-08-17) The THREE de-duplicated edge-verb channels off the ONE
        # shared classifier, carried on the report AND — for the case channel — as a
        # loud WARN so the author corrects the casing rather than being told "unknown".
        channels = edge_verb_channels(corpus)
        unminted_report["undeclared_edge_verbs"] = channels["undeclared_edge_verbs"]
        unminted_report["case_mismatch_edge_verbs"] = channels["case_mismatch_edge_verbs"]
        unminted_report["case_mismatch_canonical"] = channels["case_mismatch_canonical"]
        warn = edge_verb_warn_line(channels)
        if warn:
            findings.append(Finding(
                origin=UNMINTED_ORIGIN, kind=KIND_CASE_MISMATCH,
                severity=umsev.get(KIND_CASE_MISMATCH, "warning"),
                message=(warn + " — each is a case/separator variant of a declared verb "
                         "and mints nothing as written; fix the casing to the canonical "
                         "verb and it mints through the existing path (never normalised "
                         "silently, so the source gets corrected)."),
                source_file=None, source_line=None,
                detail={"case_mismatch_canonical": channels["case_mismatch_canonical"]},
            ))

    if canonical:
        for cf in find_canonical_findings(corpus):
            findings.append(Finding(
                origin=CANONICAL_ORIGIN, kind=cf["kind"],
                severity=casev.get(cf["kind"], "error"), message=cf["message"],
                source_file=cf["source_file"], source_line=cf["source_line"],
                node_id=cf["node_id"], detail=cf["detail"],
            ))

    if review_edges:
        for rf in find_review_edge_findings(corpus):
            findings.append(Finding(
                origin=REVIEW_EDGES_ORIGIN, kind=rf["kind"],
                severity=resev.get(rf["kind"], "error"), message=rf["message"],
                source_file=rf["source_file"], source_line=rf["source_line"],
                node_id=rf["node_id"], detail=rf["detail"],
            ))

    malformed_ref_report: Optional[dict] = None
    if malformed_ref:
        mr_findings = find_malformed_refs(corpus)
        for f in mr_findings:
            findings.append(Finding(
                origin=MALFORMED_REF_ORIGIN, kind=f.kind,
                severity=mrsev.get(f.kind, "warning"), message=f.message,
                source_file=f.source_file, source_line=f.source_line,
                node_id=f.node_id, detail={**f.detail, "key": f.key},
            ))
        malformed_ref_report = malformed_ref_stats(mr_findings)

    if id_hijack:
        for hf in find_id_hijacks(corpus):
            findings.append(Finding(
                origin=ID_HIJACK_ORIGIN, kind=hf.kind,
                severity=ihsev.get(hf.kind, "error"), message=hf.message,
                source_file=hf.source_file, source_line=hf.source_line,
                node_id=hf.node_id, detail=hf.detail,
            ))

    vocabulary_report: Optional[dict] = None
    if vocabulary:
        vfs = find_vocabulary_findings(corpus)
        for vf in vfs:
            findings.append(Finding(
                # ⛔ severity comes from the PARSER, which read it off the document's
                # own `lts:` marker. See vocabulary.py for why a lint-side severity
                # table here would silently un-ratify amendment A1.
                origin=VOCABULARY_ORIGIN, kind=vf.kind,
                severity=vocabulary_severity_for(vf), message=vf.message,
                source_file=vf.source_file, source_line=vf.source_line,
                node_id=vf.node_id, detail=vf.detail,
            ))
        vocabulary_report = vocabulary_stats(vfs)

    claim_id_report: Optional[dict] = None
    if claim_id:
        genre = (set(claim_genre_prefixes) if claim_genre_prefixes is not None
                 else CLAIM_GENRE_PREFIXES)
        # ⛔ BOTH HARM MODELS (amended 2026-08-16, the maintainer). `find_claim_ids` is
        # CITATION LEAKAGE and takes the genre licence; `find_read_suppressing_names` is
        # READ SUPPRESSION and deliberately takes NO genre licence — see
        # `claim_id`'s module docstring for the measurement that separates them.
        for cf in list(find_claim_ids(corpus, genre)) + \
                 list(find_read_suppressing_names(corpus)):
            findings.append(Finding(
                origin=CLAIM_ID_ORIGIN, kind=cf.kind,
                severity=cisev.get(cf.kind, "warning"), message=cf.message,
                source_file=cf.source_file, source_line=cf.source_line,
                node_id=cf.node_id, detail=cf.detail,
            ))
        claim_id_report = claim_id_census(corpus, genre)

    if corpus_files:
        # Filename-level, so it runs off the CALLER'S paths — `corpus.coverage`'s
        # source_file is a basename inside a temp dir in file-set mode and cannot be
        # used to find the sibling files this check is entirely about.
        roots = [source] if source is not None else list(paths or [])
        for ff in find_corpus_file_issues(roots):
            findings.append(Finding(
                origin=CORPUS_FILES_ORIGIN, kind=ff.kind,
                severity=cfsev.get(ff.kind, "warning"), message=ff.message,
                source_file=ff.source_file, source_line=None, detail=ff.detail,
            ))

    lifecycle: Optional[dict] = None
    if profile_rules:
        for pf in find_profile_violations(corpus):
            findings.append(Finding(
                origin=PROFILE_ORIGIN, kind=pf.kind,
                severity=psev.get(pf.kind, "warning"), message=pf.message,
                source_file=pf.source_file, source_line=pf.source_line,
                node_id=pf.node_id, detail={**pf.detail, "validator": pf.validator},
            ))
        if profile_is_loaded(corpus):
            lifecycle = lifecycle_report(corpus)

    findings.sort(key=lambda f: (f.source_file or "", f.source_line or 0, f.origin, f.kind))

    # --- [FL-RECALL-SCOPE-VERDICT]: the run declares WHAT IT RAN OVER and WHAT DID NOT RUN
    import os as _os
    if source is not None:
        kind = ("directory" if _os.path.isdir(_os.path.expanduser(str(source)))
                else "single_file")
        named = [str(source)]
    else:
        kind, named = "file_set", [str(p) for p in (paths or [])]
    resolution = corpus.profile_resolution.to_dict()
    scope = {
        "kind": kind,
        "source": named if kind == "file_set" else named[0],
        "file_count": len(corpus.coverage) if corpus.coverage else len(named),
        "corpus_root": resolution.get("corpus_root"),
        "caveat": (
            "SINGLE-FILE SCOPE. Reference integrity is resolved over THIS FILE ONLY, so "
            "a ref to a node defined in a sibling file reads as DANGLING even though the "
            "full corpus resolves it. Measured: 12 errors at file scope, 0 at corpus "
            "scope, all 12 phantom. Pass the corpus DIRECTORY for a whole-corpus verdict."
            if kind == "single_file" else
            "FILE-SET SCOPE. Dangles are resolved over exactly the listed files; a "
            "partial glob is the wrong tool for a whole-corpus question."
            if kind == "file_set" else None),
    }
    rules_inert: List[str] = []
    if profile_rules and not resolution.get("available"):
        rules_inert.append(
            "profile-driven validation V1-V11 (no profile in scope: "
            f"resolved_from={resolution.get('resolved_from')})")
    if not profile_rules:
        rules_inert.append("profile-driven validation V1-V11 (profile_rules=False)")
    if not structure:
        rules_inert.append("structural diagnostics (structure=False)")
    if not coverage:
        rules_inert.append("line accounting [FL-LINE-ACCOUNTING] (coverage=False)")
    if not key_loss:
        rules_inert.append("key-loss accounting (key_loss=False)")
    if not id_hijack:
        rules_inert.append("id-hijack detection (id_hijack=False)")
    if not corpus_files:
        rules_inert.append("corpus-file hygiene (corpus_files=False)")
    if not unminted:
        rules_inert.append("unminted-reference-list detection (unminted=False)")
    if not canonical:
        rules_inert.append("canonical-edge lint (canonical=False)")
    if not review_edges:
        rules_inert.append("review-edge lint (review_edges=False)")
    if not malformed_ref:
        rules_inert.append("malformed-ref detection (malformed_ref=False)")
    # ⛔ `dangle=False` IS DELIBERATELY NOT IN `rules_inert`, and this is the load-bearing
    # restraint. It is the DEFAULT, so listing it would make `rules_inert` non-empty on
    # every ordinary run, null the headline on every ordinary run, and turn a signal into
    # a constant. A suppression that fires always is a suppression nobody reads — the same
    # argument `key_loss` makes for shipping at `warning`. The narrowing is real and is
    # recorded where a narrowing belongs: in `scope`.
    scope["orphan_check"] = bool(dangle)
    if not dangle:
        scope["narrowing"] = ("the ORPHAN half of the dangle lint did not run (default). "
                              "The FORWARD half did, as `structure.dangling`. Do not read "
                              "this result as 'no orphans'.")

    return LintResult(
        findings=findings,
        claim_ids=claim_id_report,
        vocabulary=vocabulary_report,
        scope=scope,
        rules_inert=rules_inert,
        node_count=len(corpus.nodes),
        edge_count=len(corpus.edges),
        profile_domain=corpus.profile.domain_id,
        profile_path=corpus.profile.path,
        profile_resolution=corpus.profile_resolution.to_dict(),
        lifecycle=lifecycle,
        key_loss=key_loss_report,
        unminted=unminted_report,
        unqualified_references=unqualified_report,
        malformed_ref=malformed_ref_report,
        profile_documents=corpus.profile_documents,
        corpus=corpus,
    )
