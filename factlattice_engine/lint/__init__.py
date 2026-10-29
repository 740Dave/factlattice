"""factlattice_engine.lint -- the LINT layer of lts-engine: a DETERMINISTIC
FactLattice (`.lts`) linter built on the shared `factlattice` parser as a CONSUMER.
This layer imports `factlattice`'s parse tree / diagnostics and does NOT parse `.lts`
itself -- so a corpus that LINTS clean is a corpus that INGESTS clean, by construction
(one code path, no second parser to drift from the engine's own). This is the
structural ⟺ ingest equivalence the engine guarantees.

Three lint capabilities (see `core.lint`'s docstring for the full contract):

    format/structure lint   -- factlattice's own `Diagnostic`s (parse_error,
                                collisions, dangling, unregistered_ns,
                                unresolved_target), severity-tagged.
    bidirectional dangle-lint  -- forward (a `[PREFIX-NAME]` ref defined nowhere)
                                + orphan (a defined node nothing references),
                                over a single `.lts` file or a whole corpus directory.
    profile-driven validation -- V1-V9 + V11, DEFAULT ON (`profile_rules.py`): registered
                                prefixes / document tags / required + recommended
                                properties / enum values / decision-lifecycle stages
                                / §7.1 confidence / §8.1 invalidating_assumption /
                                §1.5 frontmatter. Read off the TYPED
                                `Profile` accessors, never `profile.raw`. INERT for a
                                corpus with no profile. All findings `warning`.

    line-accounting lint    -- [FL-LINE-ACCOUNTING], DEFAULT ON. Every non-blank line
                                is CONSUMED by a parser branch or CLASSIFIED as
                                ignorable; anything else is authored content the
                                structured view dropped in SILENCE. The other two
                                capabilities are rules about content the parser DID
                                read; this is the only one that can see what it
                                did not.

    key-loss lint           -- [KEY-LOSS-RETROSCAN], DEFAULT ON (`key_loss.py`). An
                                authored top-level bullet in a NODE BODY that MINTED NO
                                PROPERTY, and whose shape says the author expected one
                                (markdown emphasis, a pictograph, a spaced/slashed key,
                                a [NODE-REF] key, no space after the colon, a bold
                                lead-in label). Line accounting asks "was this line
                                READ?"; this asks "did it MINT the field its shape
                                promised?" — measured 2026-08-13, 76% of real key losses
                                are `list_prose` to the line-accounting rule and
                                therefore invisible. Warnings; the SUMMARY COUNTER
                                prints on every run, clean or not.

    corpus-file hygiene     -- DEFAULT ON (`corpus_files.py`). Defects in the FILENAMES
                                that no parser reads: two `.lts` files in one directory
                                sharing a numeric prefix (`61-config-over-bus.lts` +
                                `61-fleet-console.lts`) — an ERROR, because the prefix is
                                a cross-reference identifier and git cannot flag the
                                collision; and `.bak`/`.orig` shadow copies of live
                                documents — a warning.

    from factlattice_engine.lint import lint
    result = lint("path/to/corpus", dangle=True)
    result.has_errors   # bool
    result.exit_code()  # 0 | 1
    print(result.render_text())

CLI::

    python3 -m factlattice_engine.lint lint <path-or-glob> [--dangle] [--no-coverage] \\
        [--profile PATH] [--json]
    python3 -m factlattice_engine.lint --selftest
"""
from __future__ import annotations

from .core import Finding, LintResult, STRUCTURE_SEVERITY, DANGLE_SEVERITY, lint
from .corpus_files import CORPUS_FILE_SEVERITY, find_corpus_file_issues
from .coverage import COVERAGE_SEVERITY, coverage_stats, find_unaccounted
from .key_loss import KEY_LOSS_SEVERITY, find_key_losses, key_loss_stats
from .dangle import DangleFinding, DIR_FORWARD, DIR_ORPHAN, find_dangles
from .profile_rules import (
    PROFILE_SEVERITY,
    VALIDATORS,
    ProfileFinding,
    find_profile_violations,
    lifecycle_report,
    profile_findings_by_validator,
    profile_is_loaded,
)

__all__ = [
    "lint",
    "LintResult",
    "Finding",
    "STRUCTURE_SEVERITY",
    "DANGLE_SEVERITY",
    "COVERAGE_SEVERITY",
    "PROFILE_SEVERITY",
    "KEY_LOSS_SEVERITY",
    "CORPUS_FILE_SEVERITY",
    "find_key_losses",
    "key_loss_stats",
    "find_corpus_file_issues",
    "VALIDATORS",
    "find_dangles",
    "find_unaccounted",
    "coverage_stats",
    "find_profile_violations",
    "profile_findings_by_validator",
    "profile_is_loaded",
    "lifecycle_report",
    "ProfileFinding",
    "DangleFinding",
    "DIR_FORWARD",
    "DIR_ORPHAN",
]
