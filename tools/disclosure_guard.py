#!/usr/bin/env python3
"""DISCLOSURE GUARD — refuse to stage a line that names a deployment.

This repository is PUBLIC. The engine in it is domain-agnostic BY DESIGN: it loads a
domain's vocabulary from a profile at runtime, and that is its narrow waist. A real
domain id, a customer name, or an absolute path from somebody's laptop appearing in
engine source or in the public test suite is two failures at once — a disclosure, and
a fork across the seam the architecture is built on.

⛔ THE GUARD IS DOMAIN-AGNOSTIC TOO, AND FOR THE SAME REASON
    A denylist that SPELLS a customer's name discloses that customer. It is indexed by
    the public host's code search, it ships in every clone, and it is precisely the
    artifact the check exists to prevent — sitting inside the checker. So this file
    carries NO client term. It carries the MECHANISM, plus rules that are STRUCTURAL
    rather than identifying (an absolute home path names no product and no company).

    Client-specific terms load at runtime from a rules file OUTSIDE the public tree,
    defaulting to `private/disclosure-denylist.tsv` (`private/` is gitignored). Same
    narrow waist as the engine: mechanism public, vocabulary private. If that seems
    like a lot of ceremony for a grep — it is the difference between a check that can
    be published and one that cannot.

⭐ EVERY RULE SHIPS ITS OWN POSITIVE CONTROL — ENFORCED, NOT REQUESTED
    A rules row is `pattern <TAB> control <TAB> why [<TAB> name]`, and `control` MUST
    be a string the pattern matches. A rule that does not match its own control is
    REFUSED AT LOAD TIME, because such a rule will never fire and would otherwise sit
    in the file looking like protection. `--selftest` then runs every loaded rule
    against its control, so a green run proves the matchers EXECUTED.

⛔ NO RULES IS A FAILURE, NOT A PASS
    A missing rules file is reported loudly and makes `--selftest` exit non-zero.
    Otherwise "0 findings" would mean "0 rules" — the exact failure class this repo
    has spent two days measuring: the instrument that can only report success.

⛔ WHAT THIS CHECK CANNOT DO, STATED PLAINLY
    A denylist catches only the names somebody thought to add. It cannot catch a client
    named for the first time tomorrow, a codename, an internal hostname, or one
    customer's distinctive vocabulary — a fixture full of their jargon discloses them
    without ever spelling their name. A green run means "none of these N patterns
    appeared in the lines you changed". It does not mean the diff is safe to publish.

SCOPE — CHANGED LINES, NOT WHOLE FILES
    Only lines ADDED in the staged diff are inspected. Whole-file scanning would fail
    every future commit on pre-existing occurrences and be switched off within a day
    ([LAYER-LINT]: a gate that fires on legitimate content trains people to disable
    it). Pre-existing occurrences are not ignored — `--baseline` counts and prints
    them, so the grandfathering is VISIBLE rather than silent.

USAGE
    tools/disclosure_guard.py --selftest   # positive controls; non-zero if a rule is mute
    tools/disclosure_guard.py              # check staged added lines (what the hook runs)
    tools/disclosure_guard.py --baseline   # pre-existing tracked occurrences, counted
    tools/disclosure_guard.py --all        # selftest, then the staged check
    tools/disclosure_guard.py --tree [DIR] # scan EVERY file under DIR (default: repo root) not
                                           # named in PUBLISH-EXCLUDE (lenient: see R7 below)
    tools/disclosure_guard.py --publication [DIR] [--report FILE] [--map-manifest FILE]
                                           # the publication GATE: fails closed, always prints a scope line

    FACTLATTICE_DISCLOSURE_RULES=/path/rules.tsv   # override the rules-file location
    FACTLATTICE_PUBLISH_VOCAB=/path/vocab.tsv      # override the vocabulary-file location
    FACTLATTICE_NEUTRALITY_TERMS=/path/terms.json  # an optional GENERATED file of agent and corpus ids (or --neutrality-terms)
    FACTLATTICE_NEUTRALITY_CONTEXTS=/path/ctx.json # its private table of false-positive contexts (or --neutrality-contexts)
    tools/disclosure_guard.py --publication DIR --require-neutrality-terms   # refuse unless a usable terms file is named

NEUTRALITY TERMS (an optional, generated input; see the section of that name below)
    One JSON file of agent ids and corpus ids, written by another tool, becomes one rule per id (`neutrality-agent-id`,
    `neutrality-corpus-id`; the control string is the id, so the selftest proves each fires). The file is validated first
    (shape, sha256, freshness) and the first failure names its CAUSE, never a term; nothing is loaded from an unusable
    file. Absent: the lenient modes print one warning. `--publication` refuses a named-but-unusable file, and an absent
    one when `--require-neutrality-terms` is given. The matcher is a copy of the writer's; its table of false-positive
    contexts is private and is loaded from a file, never shipped. A generated agent id that EXACTLY equals the stem of an
    `agents/<stem>.md` file in the scanned tree (after PUBLISH-EXCLUDE) is exempt as a shipped product name and is printed by
    name. A generated CORPUS id is exempt only when its own files ship in the scanned tree (derived from the deployment registry,
    read through the engine's own loader, and the tree; see the section "shipped corpora" below) and the engine says it is not
    private; with no usable registry none is exempt, and one line says so. Exempt corpus ids are printed by name too.
    In tree and publication mode (`scan_tree`, through `_matches_outside`) one more thing is exempt: a rule the guard GENERATED
    from a neutrality agent id (`Rule.generated`, name `neutrality-agent-id`), whose id is only ASCII letters, digits and single hyphens (none leading or trailing),
    is applied to the line (or the path) with each occurrence of an exempt shipped agent name overwritten by filler of the same
    length, and it is a finding only if it still matches. So a shorter id that is part of a shipped name (`foo-bar` inside
    `acme-foo-bar`) is not reported there, and one that stands outside the name, or on another line, is. An occurrence is the
    exact stem, in its own case, with no word character, `-` or combining mark on either side. Every other rule is applied to
    the text as it is, whatever its name: a row of any rules file, a generated corpus-id rule, and a generated agent id with any
    other character in it. The staged check, the PR leak check and release notes do not apply this exemption (they use `_matches`),
    and with no exempt agent id it exempts nothing. Known limit: it is on unconditionally; there is no flag to turn it off.
    Trust: the private contexts table is the maintainers' input. A context fragment changes what an id rule matches, and the exemption trusts it.
    The selftest (`--selftest`, `--publication`) plants five rules inside a made-up exempt name, and each must be found: a rules-file row, a row
    named `neutrality-agent-id`, a hand-built non-generated rule of that name, a generated agent id with a `.`, and a generated corpus id with a plain
    id (the rows and generated ids are built by the real loaders). That control
    covers which rules may be masked. What counts as an occurrence of a name (case, boundaries, marks) is pinned by the `test_f2_` tests, not by the
    control. `--tree` alone runs no controls.
    `--expect-neutrality-terms-sha256` refuses a file that hashes differently.

TREE MODE AND THE VOCABULARY (publication)
    A second private file, `private/publish-vocabulary.tsv`, has the SAME row format as the
    denylist and is loaded in addition to it. The denylist names customers; the vocabulary
    names everything else that must not reach a public tree (product, corpus, agent and role
    names, home paths, test-identity values, emails, session-id shapes). `--tree` REQUIRES the
    vocabulary: no vocabulary is a failure, never a pass.

    `PUBLISH-EXCLUDE` (repo root) lists the paths that NEVER cross, one per line, each with a
    mandatory `# comment`. A path ending in `/` is a directory prefix. A line beginning
    `#PENDING ` is an undecided entry: it is NOT excluded (so it is scanned) until the
    maintainer promotes it. Excluded paths are skipped by `--tree` and by the staged check.

PUBLICATION MODE (`--publication [DIR]`; spec 42 R1, R2, R5, R6, D2)
    The gate the publication workflow calls. It is a mode of its own, and the only one that FAILS
    CLOSED: it exits non-zero (EXIT_INTEGRITY) when either private file (the vocabulary, the
    denylist) is missing, is empty (comments and blank lines are not rows), yields no usable rule,
    or has a row refused at load (malformed, bad regex, or a pattern that misses its own control);
    and when the selftest controls or the tree-mode control fail. The message names WHICH file and
    WHY (by label and line number) and never prints a pattern or a control. It then scans like
    `--tree`, except that it ALWAYS walks the directory on disk (never `git ls-files`; `.git` is left out;
    a symlink is a shipped file whose target string is scanned as a line, a dangling one is counted) and
    refuses a DIR that does not exist, is not a directory, or yields zero files shipped or scanned. It also
    refuses when the two private files are the same file or have the same sha256, and when PUBLISH-EXCLUDE
    cannot be read. It prints ONE scope line on every run: files scanned, exclude entries, files shipped,
    the scanner's own exempt files, each private file's rule count, row count and sha256, and the
    mapped-file count. The result line calls the scan a lexical tripwire over the named vocabulary
    and never says that nothing private is present (the tripwire wording is on the RED and REFUSED result
    lines too). A finding is `path:line  [rule]`; content-line text is never printed, and where the PATH
    itself matches a rule the matched span of the path is masked. Known limits: a zero-width (lookahead)
    rule leaves nothing to mask, and the rule NAME is printed by design (R5).
    `--report FILE` writes the scope line's fields, the findings and the exit code as JSON (no
    matched text). `--map-manifest FILE` (lines `target<TAB>source`, for files placed by the
    snapshot builder's folder map) makes the scope line count mapped files and makes a finding in
    a mapped target also name its source; a target that is not in the tree, or a malformed line,
    refuses.

LENIENT MODES SAY SO WHEN UNCONFIGURED (spec 42 R7)
    Staged, `--all`, `--selftest` and `--tree` do not fail closed (a stranger who clones the public
    repository has no private files and must not be blocked). But a run in which a private file is
    missing or empty never prints a zero-findings result: it says which file was not loaded and
    that its rules were NOT checked, and its output never equals that of a configured run.

INSTALL
    ln -sf ../../tools/pre-commit .git/hooks/pre-commit
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import stat
import subprocess
import sys
import unicodedata

DEFAULT_RULES_REL = ("private", "disclosure-denylist.tsv")
DEFAULT_VOCAB_REL = ("private", "publish-vocabulary.tsv")
EXCLUDE_FILE = "PUBLISH-EXCLUDE"


class Rule:
    """One rule. `flags` defaults to case-insensitive (every rules-file row). `generated` marks a rule made from a
    generated terms file: its vocabulary is not known when this file is written, so the legitimate-line controls
    (CONTROL_CLEAN) are not run against it; its own control is. `ident` is the generated id the rule was made from (None for a
    rules-file row): its control is that id only for an ordinary id, so a rule is selected by `ident`, never by `control`."""
    __slots__ = ("name", "rx", "control", "why", "source", "generated", "ident")

    def __init__(self, name, pattern, control, why, source, flags=re.IGNORECASE, generated=False, ident=None):
        self.ident = ident
        self.name = name
        self.rx = re.compile(pattern, flags)
        self.control = control
        self.why = why
        self.source = source
        self.generated = generated


# --- STRUCTURAL rules — they name nobody, so they can live in the public tree ----
#
# ⛔ DOCUMENTATION PLACEHOLDERS ARE EXEMPT. `/home/you/myproject` in a worked example
# is not a disclosure — it is the correct way to write documentation, and a gate that
# fires on it teaches authors to switch the gate off ([LAYER-LINT]). Measured: the
# first draft of this rule flagged SESSION-LIFECYCLE.md:453-454, which are exactly
# that. The exemption is pinned by its own negative controls below, so a later edit
# that widens the pattern back fails the selftest instead of silently nagging.
_PLACEHOLDER = r"(?!you/|user/|username/|youruser/|me/|<|\$)"
BUILTIN_RULES = [
    Rule("absolute-home-path", rf"/Users/{_PLACEHOLDER}[A-Za-z0-9_.\-]+/",
         '_ROOT = "/Users/jdoe/checkout"',
         "an absolute path from one machine. It publishes a filename inventory and "
         "cannot work for anyone else. Use a relative path, an env var, or tmp_path.",
         "builtin"),
    Rule("absolute-linux-home", rf"/home/{_PLACEHOLDER}[A-Za-z0-9_.\-]+/",
         'ROOT = "/home/jdoe/corpora"',
         "same as absolute-home-path, on Linux.",
         "builtin"),
]

# NEGATIVE controls: the SYNTHETIC forms this guard demands as the fix must not trip
# it, or the guard forbids its own remedy and gets switched off within a day.
CONTROL_CLEAN = [
    "- domain-id: acme.ds",
    "- namespaces: acme.ds, acme.eng",
    'LIVE = os.path.expanduser("~/corpora")',
    "assert _find_edge_type('acme.ds.GOVERNS [X]', _VERBS)",
    'path = tmp_path / "99-acme-ds-profile.yaml"',
    # documentation placeholders — see _PLACEHOLDER above
    "session_start.sh executed from /home/you/myproject",
    "clone it to /Users/you/factlattice",
    "export ROOT=/Users/<your-name>/checkout",
    "export ROOT=$HOME/checkout",
]

# EXACT paths, not prefixes: `tools/disclosure_guard.py.bak-x` and `tools/pre-commit-x` are scanned.
EXEMPT_PATHS = (
    "tools/disclosure_guard.py",   # the mechanism and its structural controls
    "tools/pre-commit",            # the hook that runs it
)


# --- rule loading -------------------------------------------------------------
def rules_path(repo_root: pathlib.Path) -> pathlib.Path:
    override = os.environ.get("FACTLATTICE_DISCLOSURE_RULES")
    return pathlib.Path(override) if override else repo_root.joinpath(*DEFAULT_RULES_REL)


def vocab_path(repo_root: pathlib.Path) -> pathlib.Path:
    override = os.environ.get("FACTLATTICE_PUBLISH_VOCAB")
    return pathlib.Path(override) if override else repo_root.joinpath(*DEFAULT_VOCAB_REL)


def _parse_rows(path: pathlib.Path, text: str):
    """The ONE row parser -> (rules, problems, rows). `problems` is a list of (line, short, verbose):
    `verbose` quotes the row (for the lenient modes, which run on the maintainer's own machine) and
    `short` never does (for the publication gate, whose log is shared). `rows` counts every
    non-blank, non-comment line, refused or not."""
    rules, problems, rows = [], [], 0
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        rows += 1
        parts = raw.split("\t")
        if len(parts) < 3:
            problems.append((n, "malformed row (expected pattern, control, why)",
                             f"{path}:{n}: expected 'pattern<TAB>control<TAB>why', got {raw!r}"))
            continue
        pattern, control, why = parts[0].strip(), parts[1], parts[2].strip()
        name = parts[3].strip() if len(parts) > 3 else f"rule-{n}"
        try:
            rule = Rule(name, pattern, control, why, f"{path.name}:{n}")
        except re.error as exc:
            problems.append((n, f"bad regex ({exc.msg})", f"{path}:{n}: bad regex {pattern!r} ({exc})"))
            continue
        if not rule.rx.search(control):
            problems.append((n, f"rule [{name}] does not match its own control (refused at load)",
                             f"{path}:{n}: rule {name!r} does NOT match its own control {control!r}. "
                             f"A rule with no working control will never fire; refused at load."))
            continue
        rules.append(rule)
    return rules, problems, rows


def _load_rows_ex(path: pathlib.Path):
    rules, problems, rows = _parse_rows(path, path.read_text(encoding="utf-8"))
    return rules, [v for _n, _s, v in problems], rows


def _load_rows(path: pathlib.Path):
    """Parse one rules file -> (rules, problems). A row whose pattern misses its own control is
    refused here, so a mute rule can never sit in the file looking like protection."""
    rules, problems, _rows = _load_rows_ex(path)
    return rules, problems


def load_rules(repo_root: pathlib.Path):
    """-> (rules, problems, missing). A malformed row is a PROBLEM, never a silently
    skipped line: a rules file that quietly drops half its rows is worse than no rules
    file, because it looks like coverage.

    `missing` is reported SEPARATELY from `problems` because the two demand different
    responses. A MISSING rules file is a configuration state — a stranger who clones
    this public repo has no customer to disclose, and blocking every commit they make
    would get the hook deleted on day one. A BROKEN RULE is an integrity failure: the
    guard would run and report success while checking nothing. Collapsing the two into
    one exit code forces a choice between a useless guard and a hostile one.

    Two files are loaded: the denylist and the publish vocabulary. `--tree` additionally
    insists the vocabulary exists (see `vocab_missing`).
    """
    rules, problems = list(BUILTIN_RULES), []
    missing = []
    path = rules_path(repo_root)
    if not path.is_file():
        missing.append(
            f"PRIVATE FILE NOT LOADED: the denylist at {path} is missing, so its rules were NOT "
            f"checked. Only the {len(BUILTIN_RULES)} structural builtin rule(s) are active; NO "
            f"client-specific term is being checked for. "
            f"Create it (pattern<TAB>control<TAB>why) or set FACTLATTICE_DISCLOSURE_RULES; "
            f"see tools/disclosure-denylist.example.tsv.")
    else:
        r, p, nrows = _load_rows_ex(path)
        rules += r
        problems += p
        if nrows == 0:
            missing.append(
                f"PRIVATE FILE NOT LOADED: the denylist at {path} is empty (no rows; comments and "
                f"blank lines are not rows), so its rules were NOT checked.")
    vpath = vocab_path(repo_root)
    if not vpath.is_file():
        missing.append(
            f"PRIVATE FILE NOT LOADED: the publish vocabulary at {vpath} is missing, so its rules "
            f"were NOT checked: product/corpus/agent names, paths and "
            f"test-identity values are NOT being checked for. Create it (same row format) "
            f"or set FACTLATTICE_PUBLISH_VOCAB.")
    else:
        r, p, nrows = _load_rows_ex(vpath)
        rules += r
        problems += p
        if nrows == 0:
            missing.append(
                f"PRIVATE FILE NOT LOADED: the publish vocabulary at {vpath} is empty (no rows; "
                f"comments and blank lines are not rows), so its rules were NOT checked.")
    return rules, problems, ("\n   ".join(missing) if missing else None)


def vocab_missing(repo_root: pathlib.Path) -> bool:
    return not vocab_path(repo_root).is_file()


# --- PUBLISH-EXCLUDE ------------------------------------------------------------
def load_excludes(root: pathlib.Path):
    """-> (excluded_prefixes, pending, problems). Every active line needs a `# comment`."""
    excl, pending, problems = [], [], []
    f = root / EXCLUDE_FILE
    if not f.is_file():
        return excl, pending, problems
    for n, raw in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        is_pending = line.startswith("#PENDING ")
        if line.startswith("#") and not is_pending:
            continue
        body = line[len("#PENDING "):] if is_pending else line
        path, sep, comment = body.partition("#")
        path = path.strip()
        if not path or not sep or not comment.strip():
            problems.append(f"{f.name}:{n}: every entry needs 'path   # why it never crosses' "
                            f"(missing path or comment): {raw!r}")
            continue
        (pending if is_pending else excl).append(path)
    return excl, pending, problems


def is_excluded(rel: str, excl) -> bool:
    for e in excl:
        if e.endswith("/"):
            if rel.startswith(e):
                return True
        elif rel == e:
            return True
    return False


def _repo_root() -> pathlib.Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    return pathlib.Path(out.stdout.strip() or ".")


def _matches(rules, line: str):
    """The ONE predicate: which rules match `line`. `_matches_outside` calls it too, on a masked copy for one rule family."""
    return [r for r in rules if r.rx.search(line)]


# --- neutrality terms: a GENERIC reader for a generated terms file ---------------------------------------------
#
# The deployment keeps its agent and corpus ids in a file that another tool GENERATES (its owner is named in the
# deployment's private notes, not here). This guard reads that file when one is named and turns each id into a rule, so
# there is one list and this repository keeps no copy of it. This code knows only the file's SHAPE:
#
#   one JSON object with exactly the top-level keys `_header`, `agent_ids`, `corpus_ids` and, optionally, `word_ids`;
#   the two lists are sorted, non-empty and hold non-empty strings;
#   `word_ids`, when present (it may be empty), is a list of strings in plain codepoint order, unique, each stored EXACTLY as it is
#   in `agent_ids` or `corpus_ids` (case included; membership is exact and case-sensitive, there is no case folding anywhere; it
#   marks ids that are ordinary words and never adds a term);
#   `_header.generated_at` is `YYYY-MM-DDTHH:MM:SSZ`; `_header.sources.hand_additions` is an object or the literal
#   "NONE (file absent)"; `_header.sha256` is the sha256 of the canonical JSON of {agent_ids, corpus_ids}, or of
#   {agent_ids, corpus_ids, word_ids} when the file has the `word_ids` key (sorted keys, no spaces).
#
# A file is USABLE only when it passes every check below, in this order, and the first failure is its CAUSE:
#   missing, unparsable, shape, sha256-mismatch, future-dated, stale, partial
# A missing or not strictly formed `generated_at` is "unparsable", never "stale". A cause is never accompanied by a
# term: a failure message names the cause and the field, not the vocabulary. Nothing is loaded from an unusable file.
#
# Matching is a COPY of the writer's scanner (`_term_regex` and `_term_alternative`), taken at source commit
# b83114100844959cea35b358332c5796ffa0b9d9 so this repository installs without the writer's tree. The word-id and short-id
# rules (see `_term_regex`) are written here from the writer's ruling and are NOT a copy: at writer commit
# 4da458ffe52ed9033ac7e45db3a81e0a3b26a429 the writer's own matcher does not carry them yet; its shared case table does.
# tests/test_neutrality_terms.py runs the copy over that table (tests/data/term_regex_cases.json, a byte-for-byte copy of
# the writer's file at commit 933ba98f72c9dda1ce53fc3853f3714314d6c9b4, its sha256 pinned there), which is the drift alarm. The writer's table of false-positive contexts
# (terms that are also ordinary words, skipped inside a listed phrase) is NOT in the terms file and NOT in this file:
# it is loaded from a private JSON file, because a table of "short words that are names here" would itself disclose them.
NEUTRALITY_ENV = "FACTLATTICE_NEUTRALITY_TERMS"
NEUTRALITY_CONTEXTS_ENV = "FACTLATTICE_NEUTRALITY_CONTEXTS"
DEFAULT_CONTEXTS_REL = ("private", "neutrality-term-contexts.json")
NEUTRALITY_LIST_KEYS = ("agent_ids", "corpus_ids")
NEUTRALITY_RULE_NAMES = {"agent_ids": "neutrality-agent-id", "corpus_ids": "neutrality-corpus-id"}
NEUTRALITY_WORD_KEY = "word_ids"
NEUTRALITY_TOP_KEYS = frozenset({"_header", "agent_ids", "corpus_ids", NEUTRALITY_WORD_KEY})
NEUTRALITY_SHORT_ID_MAX = 3      # a constant of the matcher, pinned by the shared case table, never a field of the file
NEUTRALITY_HAND_ABSENT = "NONE (file absent)"
NEUTRALITY_MAX_AGE_SECONDS = 21600
NEUTRALITY_CLOCK_SKEW_SECONDS = 300
NEUTRALITY_CAUSES = ("missing", "file-changed", "unparsable", "shape", "sha256-mismatch", "future-dated", "stale", "partial")
_GENERATED_AT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_I = re.IGNORECASE


def _term_alternative(term: str, contexts=None) -> str:
    return "".join((contexts or {}).get(term.lower(), ())) + re.escape(term)


_UPPER_EDGE = (r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])")
_SHORT_EDGE = (r"(?<![A-Za-z0-9])(?<![A-Z0-9]-)", r"(?![A-Za-z0-9])(?!-[A-Z0-9])")
_SEGMENT_EDGE = (r"(?<=/)", r"(?![A-Za-z0-9_-])")


def _is_short_id(term: str) -> bool:
    return len(term) <= NEUTRALITY_SHORT_ID_MAX


def _word_id_piece(term: str, contexts) -> str:
    """One word id: its UPPER-case form (case-sensitive; with the short-id edges when it is short), or the term as a whole path
    segment, written between slashes or before a dot (`/notice/`, `/notice.md`), in the id's own stored case. Never a bare word."""
    edge = _SHORT_EDGE if _is_short_id(term) else _UPPER_EDGE
    upper = edge[0] + _term_alternative(term.upper(), contexts) + edge[1]
    segment = _SEGMENT_EDGE[0] + _term_alternative(term, contexts) + _SEGMENT_EDGE[1]
    return upper + "|" + segment


def _term_regex(terms, contexts=None, word_ids=None):
    """Whole-token match (a letter or digit on either side is not a match; a hyphen or underscore is). A short
    all-capital acronym (five characters or fewer) is matched case-SENSITIVELY, every other term case-insensitively.
    A term with a listed false-positive context is not matched inside that context. -> ((pattern, flags), ...)

    Two further rules, taken from the writer's ruling and pinned by the shared case table:
      - a SHORT id (three characters or fewer) ALWAYS matches only in UPPER case, and a hyphen that joins it to another upper-case
        or digit run is not a token edge, so `X-QX` and `QX-2` are silent and a standalone `QX` is caught. This is a constant of
        the matcher; it does not depend on the terms file having a `word_ids` key;
      - a WORD id (listed in `word_ids`, which is None for a file without the key; an ordinary word that is also a name) matches
        only in its UPPER-case form, case-sensitively (`NOTICE`), or as a whole path segment (`/notice/`, `/notice.md`); never as a bare
        lower-case word. An id that is not listed is matched as before."""
    words = set(word_ids or ())          # exact and case-sensitive: no case folding anywhere
    ci, cs, special = [], [], []
    for t in terms:
        if t in words:
            special.append(_word_id_piece(t, contexts))
        elif _is_short_id(t):
            special.append(_SHORT_EDGE[0] + _term_alternative(t.upper(), contexts) + _SHORT_EDGE[1])
        elif t.isupper() and len(t) <= 5:
            cs.append(t)
        else:
            ci.append(t)
    out = []
    edge = (r"(?<![A-Za-z0-9])(?:", r")(?![A-Za-z0-9])")
    for group, flags in ((ci, _I), (cs, 0)):
        if group:
            out.append((edge[0] + "|".join(_term_alternative(t, contexts) for t in sorted(group, key=len, reverse=True)) + edge[1], flags))
    if special:
        out.append(("(?:" + "|".join(special) + ")", 0))
    return tuple(out)


def _control_for(term: str, word_ids) -> str:
    """The string a generated rule must match: the id itself, in the form the rule accepts (upper case for a short or listed word id)."""
    if _is_short_id(term) or term in set(word_ids or ()):
        return term.upper()
    return term


class NeutralityTerms:
    """The outcome of reading one terms file. `ok` is True only for a usable file; otherwise `cause` is one of
    NEUTRALITY_CAUSES and `detail` says which field, never a term."""
    __slots__ = ("ok", "cause", "detail", "agent_ids", "corpus_ids", "word_ids", "sha256", "generated_at", "file_sha256")

    def __init__(self, ok, cause=None, detail="", agent_ids=(), corpus_ids=(), sha256=None, generated_at=None, file_sha256=None,
                 word_ids=None):
        self.ok, self.cause, self.detail = ok, cause, detail
        self.agent_ids, self.corpus_ids = tuple(agent_ids), tuple(corpus_ids)
        self.word_ids = None if word_ids is None else tuple(word_ids)       # None: the file has no `word_ids` key
        self.sha256, self.generated_at, self.file_sha256 = sha256, generated_at, file_sha256

    def why(self) -> str:
        return f"{self.cause}: {self.detail}" if self.detail else str(self.cause)


def _neutrality_digest(agent_ids, corpus_ids, word_ids=None) -> str:
    """The writer's canonical form: the two lists, plus `word_ids` when the file has the key (so a file without it keeps today's digest)."""
    import hashlib
    import json
    doc = {"agent_ids": list(agent_ids), "corpus_ids": list(corpus_ids)}
    if word_ids is not None:
        doc[NEUTRALITY_WORD_KEY] = list(word_ids)
    body = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _list_problem(key, value):
    """-> a short detail naming the list (never a term), or None."""
    if not isinstance(value, list):
        return f"{key} is not a list"
    if not value:
        return f"{key} is empty"
    if not all(isinstance(t, str) and t.strip() and t == t.strip() for t in value):
        return f"{key} holds a value that is not a non-empty, trimmed string"
    if value != sorted(value):
        return f"{key} is not sorted"
    return None


def _word_ids_problem(doc):
    """-> a short detail naming the key (never an entry), or None. May be empty; trimmed strings in plain codepoint order, unique, each
    stored exactly as it is in agent_ids or corpus_ids (membership is exact and case-sensitive; there is no case folding)."""
    key, value = NEUTRALITY_WORD_KEY, doc[NEUTRALITY_WORD_KEY]
    if not isinstance(value, list):
        return f"{key} is not a list"
    if not all(isinstance(t, str) and t and t == t.strip() for t in value):
        return f"{key} holds a value that is not a non-empty, trimmed string"
    if value != sorted(value):
        return f"{key} is not sorted"
    if len(value) != len(set(value)):
        return f"{key} has a duplicate"
    known = set(doc["agent_ids"]) | set(doc["corpus_ids"])
    if any(t not in known for t in value):
        return f"{key} holds an entry that is in neither agent_ids nor corpus_ids"
    return None


def _header_problem(header):
    if not isinstance(header, dict):
        return "_header is not an object"
    sources = header.get("sources")
    if not isinstance(sources, dict):
        return "_header.sources is missing or not an object"
    hand = sources.get("hand_additions")
    if hand != NEUTRALITY_HAND_ABSENT and not isinstance(hand, dict):
        return "_header.sources.hand_additions is neither an object nor the absent marker"
    if not isinstance(header.get("sha256"), str):
        return "_header.sha256 is missing or not a string"
    return None


def _neutrality_shape_problem(doc):
    """-> a short detail, or None when the document has the contracted shape. Names fields and counts, never a value."""
    if not isinstance(doc, dict):
        return "the top level is not an object"
    unknown = [k for k in doc if k not in NEUTRALITY_TOP_KEYS]
    if unknown:
        return f"{len(unknown)} unknown top-level key(s)"
    absent = [k for k in ("_header",) + NEUTRALITY_LIST_KEYS if k not in doc]
    if absent:
        return "missing required field(s): " + ", ".join(absent)
    for key in NEUTRALITY_LIST_KEYS:
        problem = _list_problem(key, doc[key])
        if problem:
            return problem
    if NEUTRALITY_WORD_KEY in doc:
        problem = _word_ids_problem(doc)
        if problem:
            return problem
    return _header_problem(doc["_header"])


def _neutrality_time_problem(generated_at, now):
    """-> (cause, detail) or None. `generated_at` must be strictly formed; the freshness window is 21,600 s, the
    clock-skew allowance 300 s, applied to a stamp dated ahead of `now`."""
    import datetime
    if not isinstance(generated_at, str) or not _GENERATED_AT_RE.fullmatch(generated_at):
        return "unparsable", "_header.generated_at is missing or not YYYY-MM-DDTHH:MM:SSZ"
    try:
        stamp = datetime.datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
    except ValueError:
        return "unparsable", "_header.generated_at is not a real date and time"
    age = (now - stamp).total_seconds()
    if age < -NEUTRALITY_CLOCK_SKEW_SECONDS:
        return "future-dated", f"generated_at is {int(-age)} s ahead of this machine's clock"
    if age >= NEUTRALITY_MAX_AGE_SECONDS:
        return "stale", f"generated_at is {int(age)} s old; the limit is {NEUTRALITY_MAX_AGE_SECONDS} s"
    return None


def load_neutrality_terms(path, now=None, expect_sha256=None) -> NeutralityTerms:
    """Read and validate ONE terms file -> NeutralityTerms. Fails closed: any failure is a cause and no terms.
    `expect_sha256` (the sha256 of the file BYTES, as an earlier validation saw them) makes a file that now hashes
    differently the cause `file-changed`: the bytes read here are the bytes hashed and parsed, so there is no second read."""
    import datetime
    import hashlib
    import json
    p = pathlib.Path(path)
    if not p.is_file():
        return NeutralityTerms(False, "missing", "no regular file at the named path")
    try:
        data = p.read_bytes()
    except OSError as exc:
        return NeutralityTerms(False, "unparsable", f"not readable ({type(exc).__name__})")
    if expect_sha256 and hashlib.sha256(data).hexdigest() != expect_sha256:
        return NeutralityTerms(False, "file-changed", "the file's sha256 is not the one the caller validated")
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        return NeutralityTerms(False, "unparsable", f"not readable as one JSON document ({type(exc).__name__})")
    problem = _neutrality_shape_problem(doc)
    if problem:
        return NeutralityTerms(False, "shape", problem)
    agents, corpora, header = doc["agent_ids"], doc["corpus_ids"], doc["_header"]
    words = doc.get(NEUTRALITY_WORD_KEY)
    if _neutrality_digest(agents, corpora, words) != header["sha256"]:
        return NeutralityTerms(False, "sha256-mismatch", "_header.sha256 does not match the lists")
    now = now or datetime.datetime.now(datetime.UTC)
    timing = _neutrality_time_problem(header.get("generated_at"), now)
    if timing:
        return NeutralityTerms(False, timing[0], timing[1])
    if header["sources"]["hand_additions"] == NEUTRALITY_HAND_ABSENT:
        return NeutralityTerms(False, "partial", "the file was written without hand additions")
    return NeutralityTerms(True, None, "", agents, corpora, header["sha256"], header["generated_at"],
                           hashlib.sha256(data).hexdigest(), word_ids=words)


def _contexts_problem(doc):
    if not isinstance(doc, dict):
        return "the top level is not an object"
    for key, frags in doc.items():
        if not key or key != key.lower():
            return "a key is empty or not lower-case"
        if not isinstance(frags, list) or not frags or not all(isinstance(f, str) and f for f in frags):
            return "a value is not a non-empty list of non-empty strings"
        for frag in frags:
            try:
                re.compile(frag)
            except re.error:
                return "a context is not a valid regular expression"
    return None


def load_neutrality_contexts(path, expect_sha256=None):
    """Read the private table of false-positive contexts -> (contexts or None, problem or None, file sha256).
    The table maps a lower-case term to the regex fragments the writer's matcher puts in front of it."""
    import hashlib
    import json
    p = pathlib.Path(path)
    if not p.is_file():
        return None, "no regular file at the named path", None
    try:
        data = p.read_bytes()
    except OSError as exc:
        return None, f"not readable ({type(exc).__name__})", None
    if expect_sha256 and hashlib.sha256(data).hexdigest() != expect_sha256:
        return None, "the file's sha256 is not the one the caller validated", None
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        return None, f"not readable as one JSON document ({type(exc).__name__})", None
    problem = _contexts_problem(doc)
    if problem:
        return None, problem, None
    return {k: tuple(v) for k, v in doc.items()}, None, hashlib.sha256(data).hexdigest()


def neutrality_rules(terms: NeutralityTerms, contexts=None):
    """-> (rules, problem). One rule per id, named neutrality-agent-id or neutrality-corpus-id, whose control string is
    the id itself, so the selftest proves every generated rule fires. A rule that does not match its own control
    (a context table that swallows its own term) or does not compile is a PROBLEM and nothing is returned."""
    rules = []
    for key in NEUTRALITY_LIST_KEYS:
        family = "agent id" if key == "agent_ids" else "corpus id"
        why = f"a {family} from the generated neutrality terms; a public tree must not name it."
        for term in getattr(terms, key):
            control = _control_for(term, terms.word_ids)
            for pattern, flags in _term_regex([term], contexts, terms.word_ids):
                try:
                    rule = Rule(NEUTRALITY_RULE_NAMES[key], pattern, control, why, "neutrality-terms", flags=flags, generated=True, ident=term)
                except re.error:
                    return [], f"a {family} rule does not compile with the context table"
                if not rule.rx.search(control):
                    return [], f"a {family} rule does not match its own control (the context table swallows it)"
                rules.append(rule)
    return rules, None


def short_id_count(terms) -> int:
    return sum(1 for t in tuple(terms.agent_ids) + tuple(terms.corpus_ids) if _is_short_id(t))


def neutrality_narrowing_note(terms) -> str:
    """Counts only: whether `word_ids` was in force and how many ids it narrowed, and how many short ids are matched in upper case only."""
    words = ("word_ids in force, narrowing %d id(s) to upper case or a path segment" % len(terms.word_ids)
             if terms.word_ids is not None else "word_ids absent, no id narrowed as a word id")
    return f"neutrality terms: {words}; {short_id_count(terms)} short id(s) matched in upper case only"


class NeutralityLoad:
    """What main() and the publication gate get back: `state` is loaded | not-named | unusable, `rules` is empty
    unless loaded, `named` says whether an input was named at all, and `notes` are the lines to print."""
    __slots__ = ("state", "rules", "named", "terms", "problem", "contexts_sha256", "contexts_state", "exempt",
                 "exempt_corpora", "corpus_note", "corpus_why")

    def __init__(self, state, rules=(), named=False, terms=None, problem=None, contexts_sha256=None, contexts_state="none"):
        self.exempt = []
        self.exempt_corpora = []
        self.corpus_note = None       # why no corpus id could be exempt (no registry, unreadable, ...); None when the registry was read
        self.corpus_why = {}          # counts only, when the registry was read: generated ids that are not exempt, by reason
        self.state, self.rules, self.named, self.terms = state, list(rules), named, terms
        self.problem, self.contexts_sha256, self.contexts_state = problem, contexts_sha256, contexts_state

    def notes(self):
        if self.state == "loaded":
            t = self.terms
            out = [f"neutrality terms: loaded {len(t.agent_ids)} agent id(s) and {len(t.corpus_ids)} corpus id(s) "
                   f"(sha256 {t.sha256}, generated_at {t.generated_at})",
                   neutrality_narrowing_note(t)]
            if self.contexts_state != "loaded":
                out.append("neutrality term contexts: none loaded, so no false-positive context is applied "
                           "(this can only add hits, never remove one)")
            return out
        miss = ("the neutrality-agent-id and neutrality-corpus-id rules were NOT checked")
        if self.state == "not-named":
            return [f"⚠️  neutrality terms: NOT LOADED, no input named (set {NEUTRALITY_ENV} or pass --neutrality-terms); {miss}."]
        return [f"⚠️  neutrality terms: NOT LOADED, the input is unusable ({self.problem}); nothing was loaded from it, so {miss}."]


def _contexts_location(contexts_path, repo_root):
    """-> (path, named). A flag wins over the environment variable, which wins over the default under the repo root."""
    explicit = contexts_path or os.environ.get(NEUTRALITY_CONTEXTS_ENV)
    if explicit:
        return pathlib.Path(explicit), True
    return pathlib.Path(repo_root).joinpath(*DEFAULT_CONTEXTS_REL), False


def load_neutrality(terms_path, contexts_path, repo_root, now=None, expect_terms=None, expect_contexts=None) -> NeutralityLoad:
    """Resolve the terms input (flag, else FACTLATTICE_NEUTRALITY_TERMS) and read it. Never raises, never loads half a file."""
    path = terms_path or os.environ.get(NEUTRALITY_ENV)
    if not path:
        return NeutralityLoad("not-named")
    terms = load_neutrality_terms(path, now=now, expect_sha256=expect_terms)
    if not terms.ok:
        return NeutralityLoad("unusable", named=True, terms=terms, problem=terms.why())
    cpath, cnamed = _contexts_location(contexts_path, repo_root)
    contexts, cproblem, csha = (None, None, None)
    if cnamed or cpath.is_file():
        contexts, cproblem, csha = load_neutrality_contexts(cpath, expect_sha256=expect_contexts)
        if cproblem:
            return NeutralityLoad("unusable", named=True, terms=terms, problem=f"the context table is unusable: {cproblem}")
    rules, problem = neutrality_rules(terms, contexts)
    if problem:
        return NeutralityLoad("unusable", named=True, terms=terms, problem=problem)
    return NeutralityLoad("loaded", rules, True, terms, None, csha, "loaded" if contexts is not None else "none")


# --- shipped product names are not disclosures ----------------------------------------------------------------
# An agent name is public only if this product ships its definition. The generated list names every agent of the deployment,
# the product's own among them, so a scan of a tree exempts each generated agent id that EXACTLY equals the stem of an
# `agents/<stem>.md` REGULAR file (never a link) that is IN THE TREE BEING SCANNED and not excluded by its PUBLISH-EXCLUDE (so `foo.md` exempts `foo`,
# never `foo-bar`, `barfoo` or `Foo`). The list comes from the scanned tree itself, never from configuration. Corpus ids have their own
# rule, in the next section. The exempted ids are printed BY NAME in the run output and the report (a stray `.md` added to silence a
# hit must show); an id that is not exempt is never printed.
def _is_shipped_definition(root, rel) -> bool:
    """True only for a REGULAR file (lstat, so never a link of any kind: to a file outside the tree, dangling, to a directory, or even to
    a file inside it) directly under a REAL `agents/` directory of the tree. A link ships a path string, not a definition."""
    try:
        agents = os.lstat(os.path.join(root, "agents"))
        entry = os.lstat(os.path.join(root, rel))
    except OSError:
        return False
    return stat.S_ISDIR(agents.st_mode) and stat.S_ISREG(entry.st_mode)


def shipped_agent_stems(files, excl, root):
    """-> the set of stems of `agents/<stem>.md` among `files` (relative POSIX paths) that SHIP: not excluded, exactly one level under
    `agents/`, and a regular file in the tree at `root` (see `_is_shipped_definition`). The stem is taken as it is, never stripped."""
    out = set()
    for rel in files:
        parts = rel.split("/")
        if len(parts) == 2 and parts[0] == "agents" and parts[1].endswith(".md") and len(parts[1]) > 3 \
                and not is_excluded(rel, excl) and _is_shipped_definition(root, rel):
            out.add(parts[1][:-3])
    return out


def exempt_shipped_names(nload, files, excl, root) -> None:
    """Drop, from `nload.rules`, the agent-id rules of generated ids that are shipped product names, and the corpus-id rules of
    generated corpus ids that SHIP (see `shipped_corpus_ids`); record them (sorted) in `nload.exempt` and `nload.exempt_corpora`.
    Does nothing unless the terms are loaded."""
    if nload.state != "loaded":
        return
    stems = shipped_agent_stems(files, excl, root)
    nload.exempt = sorted(t for t in set(nload.terms.agent_ids) if t in stems)
    gone = set(nload.exempt)
    agent_rule = NEUTRALITY_RULE_NAMES["agent_ids"]
    nload.rules = [r for r in nload.rules if not (r.name == agent_rule and r.ident in gone)]
    nload.exempt_corpora, nload.corpus_note, nload.corpus_why = shipped_corpus_ids(nload.terms.corpus_ids, files, excl, root)
    gone_c = set(nload.exempt_corpora)
    corpus_rule = NEUTRALITY_RULE_NAMES["corpus_ids"]
    nload.rules = [r for r in nload.rules if not (r.name == corpus_rule and r.ident in gone_c)]


def exempt_note(nload) -> str:
    return (f"neutrality terms: {len(nload.exempt)} generated agent id(s) exempt as shipped product names"
            + (": " + ", ".join(nload.exempt) if nload.exempt else ""))


def corpus_exempt_note(nload) -> str:
    """The one line about corpus ids: the reason none could be exempt, or the names that were (shipped names are public by definition)."""
    if nload.corpus_note:
        return f"corpus exemption: {nload.corpus_note}; 0 corpus ids exempt"
    line = (f"neutrality terms: {len(nload.exempt_corpora)} generated corpus id(s) exempt as shipped corpora"
            + (": " + ", ".join(nload.exempt_corpora) if nload.exempt_corpora else ""))
    why = nload.corpus_why or {}
    if any(why.values()):
        # counts only: an id that is not exempt is never printed
        line += ("\ncorpus exemption: not exempt: " + "; ".join(
            f"{why[k]} {label}" for k, label in CORPUS_WHY_LABELS if why.get(k)))
    return line


# --- shipped corpora are not disclosures ----------------------------------------------------------------------
# A generated corpus id is exempt only when BOTH hold: (a) the corpus's own files ship in the scanned tree, and (b) the engine says it is
# not private. (a) is DERIVED here, from the tree and the deployment registry read through the engine's own loader (never re-parsed, and
# never a list kept by hand): each root the registry gives the corpus must lie inside the trunk checkout that holds the registry file
# (`<trunk>/private/<registry file>`), and the same path relative to the trunk must be, in the scanned tree, a REAL directory holding at
# least one non-excluded regular `.lts` file (or, for a root pinned by `only_file`, that one regular file). Every component is looked at with
# lstat, so a link anywhere on the path ships a path string and not a corpus. No registry, an unreadable one, the built-in example registry,
# a registry that names no trunk, or an id the registry does not hold: NO corpus id is exempt, and one line says why.
CORPUS_WHY_LABELS = (("absent", "not in the registry"), ("unserved", "private, or an entry the engine would not serve"),
                     ("unshipped", "not shipped in this tree"))


def _engine_for_corpus_exemption():
    """-> (registry module, config module, private_reason) from THIS checkout of the guard, or raises. The guard's own checkout is the
    engine that judges, never the scanned tree's copy."""
    here = str(pathlib.Path(__file__).resolve().parents[1])
    added = here not in sys.path
    if added:
        sys.path.insert(0, here)
    try:
        from factlattice import registry
        from factlattice_engine import config
        from factlattice_engine.privacy import private_reason
    finally:
        if added and sys.path and sys.path[0] == here:
            sys.path.pop(0)
    return registry, config, private_reason


def _trunk_of_registry(path):
    """The trunk checkout a registry file belongs to: the parent of the `private/` folder that holds it; None when it is elsewhere."""
    d = os.path.dirname(os.path.abspath(path))
    return os.path.dirname(d) if os.path.basename(d) == "private" else None


def _private_folder_is_link(path) -> bool:
    """True when the folder that holds the registry file is itself a link (the overlay layout). Fail closed: the two ways of naming a registry
    (the environment variable keeps the path as given, the per-user pointer resolves it) would otherwise disagree about the trunk."""
    return os.path.islink(os.path.dirname(os.path.abspath(path)))


def _real_path_inside(root, rel) -> bool:
    """True when every component of `rel` under `root` exists and is a real directory (lstat: never a link)."""
    cur = root
    for part in [p for p in rel.split("/") if p]:
        cur = os.path.join(cur, part)
        try:
            if not stat.S_ISDIR(os.lstat(cur).st_mode):
                return False
        except OSError:
            return False
    return True


def _root_ships(root_entry, trunk, files, excl, scanned) -> bool:
    """True when this registry root maps to a shipped path in the scanned tree (see the section comment)."""
    path = root_entry.get("path")
    if not isinstance(path, str) or not path:
        return False
    rel = os.path.relpath(os.path.normpath(os.path.abspath(path)), trunk)
    if rel == "." or rel == ".." or rel.startswith("../") or os.path.isabs(rel):
        return False
    rel = pathlib.PurePath(rel).as_posix()
    only = root_entry.get("only_file")
    if isinstance(only, str) and only:
        if "/" in only or only in (".", ".."):
            return False
        target = f"{rel}/{only}"
        return (target in set(files) and target.endswith(".lts") and not is_excluded(target, excl)
                and _real_path_inside(scanned, rel) and _is_regular(scanned, target))
    if is_excluded(rel, excl) or is_excluded(rel + "/", excl) or not _real_path_inside(scanned, rel):
        return False
    return any(f.startswith(rel + "/") and f.endswith(".lts") and not is_excluded(f, excl) and _is_regular(scanned, f)
               for f in files)


def _is_regular(scanned, rel) -> bool:
    try:
        return stat.S_ISREG(os.lstat(os.path.join(scanned, rel)).st_mode)
    except OSError:
        return False


def _served(cid, spec, registry, config, private_reason) -> bool:
    """True when the engine would SERVE this entry as a non-private corpus: no private verdict, no unknown-key problem (a `privat: true` typo
    makes the engine withhold the corpus, so it must not be named here), no named-root failure, a sound shape."""
    if not isinstance(spec, dict) or private_reason(cid, spec) is not None:
        return False
    if config.registry_key_failure(cid, spec) is not None or registry.named_failure(spec) is not None:
        return False
    config.check_entry_shape(spec)        # raises on a malformed entry
    return True


def shipped_corpus_ids(corpus_ids, files, excl, scanned):
    """-> (sorted exempt ids, note, why). `note` is None when the registry was read (even if nothing was exempt) and otherwise says why no corpus
    id can be exempt; `why` then counts the ids that were not exempt, by reason (never a name). Fails closed on ANY error, naming the class only."""
    try:
        registry, config, private_reason = _engine_for_corpus_exemption()
        choice = registry.resolve_registry()
    except Exception as exc:  # noqa: BLE001 -- fail closed on anything: a broken registry or engine exempts nothing
        return [], "registry unreadable (" + type(exc).__name__ + ")", {}
    if choice.path is None or choice.source == "example":
        return [], "no registry", {}
    out, why = [], {"absent": 0, "unserved": 0, "unshipped": 0}
    try:
        trunk = _trunk_of_registry(choice.path)
        if trunk is None:
            return [], "the registry is not under a trunk checkout's private/ folder", {}
        if _private_folder_is_link(choice.path):
            return [], "the registry's private/ folder is a link", {}
        for cid in sorted(set(corpus_ids)):
            spec = choice.corpora.get(cid)
            if not isinstance(spec, dict):
                why["absent"] += 1
                continue
            try:
                served = _served(cid, spec, registry, config, private_reason)
            except Exception:  # noqa: BLE001 -- an entry the engine cannot judge is not served
                served = False
            if not served:
                why["unserved"] += 1
                continue
            try:
                roots = config.declared_root_entries(spec)
                ships = bool(roots) and all(_root_ships(r, trunk, files, excl, scanned) for r in roots)
            except Exception:  # noqa: BLE001 -- e.g. a NUL in a path: this id ships nothing
                ships = False
            if ships:
                out.append(cid)
            else:
                why["unshipped"] += 1
    except Exception as exc:  # noqa: BLE001 -- nothing above should raise; if it does, no id is exempt
        return [], "exemption check failed (" + type(exc).__name__ + ")", {}
    return out, None, why


# --- the positive control -----------------------------------------------------
EXIT_OK, EXIT_INTEGRITY, EXIT_UNCONFIGURED = 0, 1, 2


def _control_failures(rules, echo=print, reveal=True):
    """The selftest's controls, shared with publication mode: every rule against its own control,
    the legitimate lines, and the tree-mode control. -> list of failure strings. `echo` receives the
    progress lines (None = silent). `reveal=False` keeps a rule's control text out of a message (the
    publication gate's log is shared)."""
    say = echo or (lambda *_a, **_k: None)
    failures = []
    for r in rules:
        if r.rx.search(r.control):
            say(f"  caught [{r.name}] on its own control   ({r.source})")
        else:
            shown = f" {r.control!r}" if reveal else ""
            failures.append(f"MUTE  [{r.name}] did not fire on its control{shown}")
    hand_written = [r for r in rules if not r.generated]
    for line in CONTROL_CLEAN:
        hit = [r.name for r in _matches(hand_written, line)]
        if hit:
            failures.append(f"FALSE POSITIVE on legitimate line {line!r} -> {hit}")
        else:
            say(f"  clean  {line!r}")
    failures += _tree_control(rules, echo=say)
    return failures


def selftest(rules, problems, missing) -> int:
    failures = list(problems) + _control_failures(rules)

    if failures:
        print("\n⛔ DISCLOSURE GUARD SELFTEST FAILED — INTEGRITY. A green run of the real"
              "\n   check would have proved NOTHING; only that nothing ran.\n")
        for f in failures:
            print(f"  {f}")
        return EXIT_INTEGRITY

    if missing:
        print(f"\n⚠️  selftest (PARTIAL): {len(rules)} loaded rule(s) each fired on their OWN control; "
              f"{len(CONTROL_CLEAN)} legitimate line(s) stayed clean. A private file was NOT loaded.")
    else:
        print(f"\n✓ selftest: {len(rules)} rule(s), each fired on its OWN control; "
              f"{len(CONTROL_CLEAN)} legitimate line(s) stayed clean.")
        print("  The matchers demonstrably RAN.")

    if missing:
        print(f"\n⚠️  UNCONFIGURED (exit {EXIT_UNCONFIGURED}, not a matcher failure):")
        print(f"   {missing}")
        print("   The structural rules above are real and did run. No IDENTITY rule from the file(s) above did.")
        return EXIT_UNCONFIGURED
    return EXIT_OK


def _exemption_control(rules, root: pathlib.Path, echo=print):
    """Control for the shipped-name exemption, through `scan_tree` (the path the real scans use). With a rule that `_is_exemptible` accepts,
    plant its control inside a made-up shipped name `zz-<control>-zz` (must NOT be found) and beside that name (MUST be found). -> failures.
    It prints the rule's name, never its control. No such rule: the exemption cannot apply, so nothing is run."""
    agent_rules = [r for r in rules if _is_exemptible(r)]
    if not agent_rules:
        return []
    probe = next((r for r in agent_rules if r.rx.search(f"zz-{r.control}-zz")), None)
    if probe is None:
        return ["EXEMPTION CONTROL: no neutrality-agent-id rule matches inside a probe name, so the exemption could not be exercised"]
    name = f"zz-{probe.control}-zz"
    (root / "kept" / "inside.txt").write_text(name + "\n", encoding="utf-8")
    (root / "kept" / "beside.txt").write_text(f"{name} {probe.control}\n", encoding="utf-8")
    found, _n = scan_tree(rules, root, [], exempt_names=(name,))
    hit = {f[0] for f in found if f[2] is probe}
    out = []
    if "kept/beside.txt" not in hit:
        out.append("EXEMPTION MUTE: a neutrality-agent-id control standing beside an exempt shipped name was not found")
    if "kept/inside.txt" in hit:
        out.append("EXEMPTION INERT: a neutrality-agent-id control inside an exempt shipped name was reported")
    if not out and echo:
        echo("  exemption control: a planted [neutrality-agent-id] inside an exempt shipped name ignored, beside it found")
    return out


def _exemption_wide_control(root: pathlib.Path, echo=print):
    """Control that the exemption is not too WIDE, through `scan_tree`, with made-up rules (so it needs no private file). The rules are built
    by the loaders the real scan uses: `_parse_rows` for the two rules-file rows, `neutrality_rules` for the two generated ids. Each is planted
    inside a made-up exempt shipped name and MUST be found. The five plants: (1) a rules-file row; (2) a rules-file row whose name column is
    `neutrality-agent-id` (plain pattern); (3) a hand-built, non-generated rule with that name and a plain `ident` (no loader makes one; it pins
    the `generated` test); (4) a generated agent id with a `.` in it; (5) a generated `neutrality-corpus-id` with a plain id.
    It covers WHICH rules may be masked and nothing more: what counts as an occurrence of a name (case, boundaries, combining marks) is
    not exercised here; the `test_f2_` tests pin it. -> failures."""
    rows, row_problems, _n = _parse_rows(pathlib.Path("control-rules.tsv"),
                                         f"dgctlrow\tdgctlrow\tw\ndgctlnamed\tdgctlnamed\tw\t{_AGENT_ID_RULE}\n")
    gen, gen_problem = neutrality_rules(NeutralityTerms(True, agent_ids=("dg.ctl",), corpus_ids=("dgctlcorp",)))
    if row_problems or gen_problem or len(rows) != 2 or len(gen) != 2:
        return ["EXEMPTION TOO WIDE: the control's own planted rules could not be built by the loaders"]
    by_name = {(r.name, r.control): r for r in rows + gen}
    plants = [("a rules-file row", by_name[("rule-1", "dgctlrow")], "dgctlrow"),
              ("a non-generated rule named neutrality-agent-id, with a plain id", Rule(_AGENT_ID_RULE, "dgctlhand", "dgctlhand", "w", "control", ident="dgctlhand"), "dgctlhand"),
              ("a rules-file row named neutrality-agent-id, with a plain pattern", by_name[(_AGENT_ID_RULE, "dgctlnamed")], "dgctlnamed"),
              ("a generated agent id that is not a plain id", by_name[(_AGENT_ID_RULE, "dg.ctl")], "dg.ctl"),
              ("a generated neutrality-corpus-id rule with a plain id", by_name[(NEUTRALITY_RULE_NAMES["corpus_ids"], "dgctlcorp")], "dgctlcorp")]
    (root / "wide").mkdir()
    names = []
    for i, (_what, _rule, token) in enumerate(plants):
        names.append(f"zz-{token}-zz")
        (root / "wide" / f"{i}.txt").write_text(names[-1] + "\n", encoding="utf-8")
    found, _n = scan_tree([p[1] for p in plants], root, [], exempt_names=tuple(names))
    out = []
    for i, (what, rule, _token) in enumerate(plants):
        if not any(f[0] == f"wide/{i}.txt" and f[2] is rule for f in found):
            out.append(f"EXEMPTION TOO WIDE: {what}, planted inside an exempt shipped name, was not found")
    if not out and echo:
        echo("  exemption control: a planted row, a row named neutrality-agent-id, a non-generated rule of that name, a non-plain agent id and a corpus id inside an exempt shipped name were found")
    return out


def _tree_control(rules, echo=print):
    """Tree-mode NEGATIVE control: plant a rule's own control string in a throwaway tree, once in
    an ordinary file and once under an excluded prefix. The first MUST be found and the second
    MUST NOT, or tree mode is mute (or ignores the exclusion) and a green `--tree` means nothing."""
    import tempfile
    candidates = [r for r in rules if r.source != "builtin"]
    probe = next((r for r in candidates if not r.generated), candidates[0] if candidates else (rules[0] if rules else None))
    if probe is None:
        return ["no rules to run the tree-mode control with"]
    with tempfile.TemporaryDirectory(prefix="dg-tree-") as d:
        root = pathlib.Path(d)
        (root / "kept").mkdir()
        (root / "skipped").mkdir()
        (root / "kept" / "planted.txt").write_text(probe.control + "\n", encoding="utf-8")
        (root / "skipped" / "planted.txt").write_text(probe.control + "\n", encoding="utf-8")
        found, _n = scan_tree(rules, root, ["skipped/"])
        out = _exemption_control(rules, root, echo)
        out += _exemption_wide_control(root, echo)
    paths = {f[0] for f in found}
    if "kept/planted.txt" not in paths:
        out.append(f"TREE MODE MUTE: a planted [{probe.name}] control in a non-excluded file was not found")
    if any(x.startswith("skipped/") for x in paths):
        out.append("TREE MODE IGNORES PUBLISH-EXCLUDE: a planted hit under an excluded prefix was reported")
    if not out:
        if echo:
            echo(f"  tree-mode control: planted [{probe.name}] found in kept/, ignored under excluded skipped/")
    return out


# --- tree mode ------------------------------------------------------------------
def _tree_files(root: pathlib.Path):
    """Tracked files when `root` is a git toplevel; otherwise every file on disk (a `git archive`
    export has no .git). Either way the list is of RELATIVE POSIX paths."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                             capture_output=True, text=True)
        if top.returncode == 0 and pathlib.Path(top.stdout.strip()).resolve() == root.resolve():
            out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True,
                                 text=True, check=True).stdout.split("\0")
            return sorted(f for f in out if f and (root / f).is_file())
    except (OSError, subprocess.CalledProcessError):
        pass
    files = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in (".git", "__pycache__")]
        for fn in fns:
            files.append(str(pathlib.Path(dp, fn).relative_to(root).as_posix()))
    return sorted(files)


def _is_exempt(rel: str) -> bool:
    return rel in EXEMPT_PATHS


def _exempt_spans(text: str, names):
    """-> [(start, end)] of each occurrence in `text` of an exempt shipped name: the exact name, in its own case, not touched on either
    side by a word character (any script), `-` or combining mark, so `x-NAME`, `NAME-2`, `éNAME` and `NAMÉ` (e + U+0301) are other names."""
    spans = []
    for name in names:
        for m in re.finditer(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text):
            s, e = m.start(), m.end()
            if (s and unicodedata.category(text[s - 1]).startswith("M")) or (e < len(text) and unicodedata.category(text[e]).startswith("M")):
                continue
            spans.append((s, e))
    return spans


_AGENT_ID_RULE = NEUTRALITY_RULE_NAMES["agent_ids"]
_FILLER = "\x00"
_PLAIN_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.IGNORECASE | re.ASCII)


def _is_exemptible(r) -> bool:
    """True only for a rule the guard generated from a plain agent id (ASCII letters, digits, single hyphens). It is decided by what the rule IS
    (`generated`, `name`, `ident`), so a row of any rules file is never exemptible, whatever its name or text says."""
    return bool(r.generated and r.name == _AGENT_ID_RULE and isinstance(r.ident, str) and _PLAIN_ID.fullmatch(r.ident))


def _matches_outside(rules, text: str, names):
    """`_matches`, except that a rule for which `_is_exemptible` holds is tested on a copy of `text` in which each occurrence of an exempt
    shipped name is overwritten by NULs of the same length: it is a finding only if it still matches there. Every other rule is tested
    on `text` itself. With no names, or none in `text`, it is `_matches`."""
    spans = _exempt_spans(text, names) if names else []
    if not spans:
        return _matches(rules, text)
    chars = list(text)
    for s, e in spans:
        chars[s:e] = _FILLER * (e - s)
    masked = "".join(chars)
    return [r for r in rules if _matches([r], masked if _is_exemptible(r) else text)]


def scan_tree(rules, root: pathlib.Path, excl, files=None, links=False, exempt_names=()):
    """-> (findings, scanned_files). Scans file CONTENT and the PATH itself. Unreadable bytes are
    decoded with replacement, never skipped: a file we could not read is not a file we cleared.
    `exempt_names` (shipped agent names) only change what a rule for which `_is_exemptible` holds reports: see `_matches_outside`."""
    findings, scanned = [], 0
    for rel in (_tree_files(root) if files is None else files):
        if _is_exempt(rel) or is_excluded(rel, excl):
            continue
        scanned += 1
        for r in _matches_outside(rules, rel, exempt_names):
            findings.append((rel, 0, r, f"(file name) {rel}"))
        try:
            if links and (root / rel).is_symlink():     # a symlink ships as its target STRING: scan that
                text = os.readlink(root / rel)
            else:
                text = (root / rel).read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            findings.append((rel, 0, Rule("unreadable", "x", "x", "", "builtin"), f"cannot read: {exc}"))
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for r in _matches_outside(rules, line, exempt_names):
                findings.append((rel, n, r, line.strip()))
    return findings, scanned


def check_tree(rules, problems, missing, root: pathlib.Path, exempt_names=()) -> int:
    excl, pending, xproblems = load_excludes(root)
    problems = list(problems) + xproblems
    for p in problems:
        print(f"⛔ disclosure_guard: {p}")
    if missing:
        print(f"⚠️  disclosure_guard --tree: {missing}")
    if vocab_missing(root):
        print(f"⛔ disclosure_guard --tree: NO PUBLISH VOCABULARY at {vocab_path(root)}. A tree scan "
              f"with no vocabulary would report 0 findings having checked nothing; refused.")
        return EXIT_INTEGRITY
    findings, scanned = scan_tree(rules, root, excl, exempt_names=exempt_names)
    if pending:
        print(f"  note: {len(pending)} #PENDING exclude entr{'y' if len(pending)==1 else 'ies'} "
              f"are NOT excluded (scanned): {', '.join(pending)}")
    if not findings:
        if missing:      # R7: an unconfigured run is never printed as a clean one
            print(f"⚠️  UNCONFIGURED RUN, NOT A CLEAN RESULT: {scanned} file(s) scanned ({len(excl)} exclude "
                  f"entr{'y' if len(excl)==1 else 'ies'}) against the {len(rules)} rule(s) that WERE loaded; "
                  f"the private file(s) named above were NOT loaded, so their rules were NOT checked.")
            return EXIT_INTEGRITY if problems else EXIT_OK
        print(f"✓ disclosure guard --tree: {scanned} file(s) scanned ({len(excl)} exclude entr"
              f"{'y' if len(excl)==1 else 'ies'}) against {len(rules)} rule(s), 0 findings.")
        print("  ⚠️  Names nobody thought to add pass untouched. Run --selftest alongside.")
        return EXIT_INTEGRITY if problems else EXIT_OK
    by_rule = {}
    for path, lineno, r, text in findings:
        print(f"  {path}:{lineno}  [{r.name}]")
        by_rule[r.name] = by_rule.get(r.name, 0) + 1
    print(f"\n⛔ {len(findings)} finding(s) in {len({f[0] for f in findings})} file(s) "
          f"from {len(rules)} rule(s). Per rule: {by_rule}")
    return EXIT_INTEGRITY


# --- publication mode (spec 42 R1, R2, R5, R6, D2) -------------------------------------------
def _mask(text: str, rules) -> str:
    """Hide, in a PATH about to be printed, the spans a rule matches: a file whose NAME is the hit
    would otherwise print the term in the log. Same-length `*` keeps the shape findable."""
    chars = list(text)
    for r in rules:
        for m in r.rx.finditer(text):
            for i in range(m.start(), m.end()):
                chars[i] = "*"
    return "".join(chars)


def _private_file_state(label: str, env: str, path: pathlib.Path):
    """Read one private file for the gate -> dict. Nothing in it (or in what is printed from it) quotes a
    pattern or a control: only the label, line numbers, short reasons, counts and the file's sha256."""
    st = {"label": label, "env": env, "state": "ok", "sha256": None, "rows": 0, "rules": 0,
          "refusals": [], "_rules": []}
    if not path.is_file():
        st["state"] = "missing"
        st["refusals"].append(f"{label}: file not found (set {env}, or place it at its default location)")
        return st
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        st["state"] = "unreadable"
        st["refusals"].append(f"{label}: cannot be read as UTF-8 text ({type(exc).__name__})")
        return st
    import hashlib
    st["sha256"] = hashlib.sha256(data).hexdigest()
    rules, problems, rows = _parse_rows(path, text)
    st["rows"], st["rules"], st["_rules"] = rows, len(rules), rules
    if rows == 0:
        st["state"] = "empty"
        st["refusals"].append(f"{label}: empty (no rows; comments and blank lines do not count)")
        return st
    for n, short, _verbose in problems:
        st["refusals"].append(f"{label}: line {n}: {short}")
    if not rules:
        st["refusals"].append(f"{label}: yields no usable rule")
    if st["refusals"]:
        st["state"] = "refused"
    return st


def _load_manifest(path: pathlib.Path, files, excl):
    """-> (mapping {target: source}, refusals). Every target must be a file in the tree."""
    refusals, mapping = [], {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return mapping, [f"map manifest: cannot be read ({type(exc).__name__})"]
    present = set(files)
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        parts = raw.split("\t")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            refusals.append(f"map manifest: line {n}: malformed (expected target<TAB>source)")
            continue
        target, source = parts[0].strip(), parts[1].strip()
        if target not in present:
            refusals.append(f"map manifest: line {n}: target is not a file in the tree")
        elif is_excluded(target, excl):
            refusals.append(f"map manifest: line {n}: target lies under a PUBLISH-EXCLUDE path")
        elif target in mapping:
            refusals.append(f"map manifest: line {n}: target listed twice")
        else:
            mapping[target] = source
    return mapping, refusals


def _publication_files(root: pathlib.Path):
    """-> (relative POSIX paths, refusals). The publication gate scans the DIRECTORY ON DISK, the thing that
    ships, never the git index (`git ls-files` would skip an untracked file and drop a deleted one). `.git` is
    left out. A symlink is a shipped file (a link to a directory is not followed), and a dangling one is
    counted. An error while listing is a refusal, never an empty list."""
    if not root.exists():
        return [], ["tree: the directory does not exist"]
    if not root.is_dir():
        return [], ["tree: the path is not a directory"]
    errors = []

    def onerror(exc):
        errors.append(f"tree: cannot list a directory ({type(exc).__name__})")

    files = []
    for dp, dns, fns in os.walk(root, onerror=onerror, followlinks=False):
        keep = []
        for d in dns:
            if d == ".git":
                continue
            if pathlib.Path(dp, d).is_symlink():
                files.append(pathlib.Path(dp, d).relative_to(root).as_posix())
            else:
                keep.append(d)
        dns[:] = keep
        for fn in fns:
            if fn == ".git":
                continue
            files.append(pathlib.Path(dp, fn).relative_to(root).as_posix())
    return sorted(files), sorted(set(errors))


def _neutrality_fields(nload) -> dict:
    t = nload.terms
    return {"state": nload.state, "named": nload.named,
            "agent_ids": len(t.agent_ids) if t and nload.state == "loaded" else 0,
            "corpus_ids": len(t.corpus_ids) if t and nload.state == "loaded" else 0,
            "sha256": t.sha256 if t and nload.state == "loaded" else None,
            "generated_at": t.generated_at if t and nload.state == "loaded" else None,
            "file_sha256": t.file_sha256 if t and nload.state == "loaded" else None,
            "word_ids": (len(t.word_ids) if t.word_ids is not None else None) if t and nload.state == "loaded" else None,
            "short_ids": short_id_count(t) if t and nload.state == "loaded" else 0,
            "contexts": nload.contexts_state, "contexts_sha256": nload.contexts_sha256,
            "exempt_agent_ids": list(nload.exempt),
            "exempt_corpus_ids": list(nload.exempt_corpora),
            "corpus_exemption_note": nload.corpus_note,
            "corpus_exemption_not_exempt": dict(nload.corpus_why),
            "cause": t.cause if t is not None and not t.ok else None}


def _neutrality_scope(f) -> str:
    """The scope-line fragment. Absent when no input was named (the line is then what it always was)."""
    n = f["neutrality_terms"]
    if n["state"] == "loaded":
        return (f"; neutrality-terms: agent-ids={n['agent_ids']} corpus-ids={n['corpus_ids']} sha256={n['sha256']} "
                f"generated_at={n['generated_at']} contexts={n['contexts']} exempt-agent-ids={len(n['exempt_agent_ids'])} "
                f"exempt-corpus-ids={len(n['exempt_corpus_ids'])} "
                f"word-ids={'absent' if n['word_ids'] is None else n['word_ids']} short-ids={n['short_ids']}")
    if n["named"]:
        return f"; neutrality-terms: NOT LOADED ({n['cause'] or 'unusable'})"
    return ""


def _scope_line(f) -> str:
    def priv(key):
        p = f["private_files"][key]
        if p["sha256"] is None:
            return f"{key}: NOT LOADED ({p['state']})"
        return f"{key}: rules={p['rules']} rows={p['rows']} sha256={p['sha256']}"
    run = lambda v: "not run" if v is None else v
    ex = ", ".join(f["exempt_unscanned"]) if f["exempt_unscanned"] else "none"
    return ("publication scope: "
            f"files-scanned={run(f['scanned'])}; exclude-entries-applied={f['exclude_entries']}; "
            f"files-shipped={f['shipped']}; shipped-not-scanned (the scanner's exempt files)=[{ex}]; "
            f"{priv('vocabulary')}; {priv('denylist')}; "
            f"mapped-files={f['mapped_files']}; builtin-rules={f['builtin_rules']}" + _neutrality_scope(f))


TRIPWIRE = "This is a lexical tripwire over the named vocabulary, not a statement that nothing private is present."


def _neutrality_gate(root, terms_path, contexts_path, require, expect_terms=None, expect_contexts=None):
    """-> (NeutralityLoad, list of refusals). In the publication gate an input that is NAMED (or required) and not usable
    REFUSES; an unnamed, unrequired one is only said so (the scope line and a warning), as in every lenient mode."""
    nload = load_neutrality(terms_path, contexts_path, root, None, expect_terms, expect_contexts)
    if nload.state == "loaded":
        return nload, []
    if nload.state == "unusable":
        return nload, [f"neutrality terms: {nload.problem}"]
    if require:
        return nload, [f"neutrality terms: required but no input named (set {NEUTRALITY_ENV} or pass --neutrality-terms)"]
    return nload, []


def _neutrality_warning(nload, require) -> None:
    """The visible line of a publication run that had no terms input and did not require one, or the names it exempted."""
    if nload.state == "not-named" and not require:
        print(nload.notes()[0])
    elif nload.state == "loaded":
        print(exempt_note(nload))
        print(corpus_exempt_note(nload))


def _neutrality_rule_sets(base, nload, files, excl, root):
    """-> (rules to scan with, rules to run the controls on). The controls cover EVERY generated rule, exempted ones included."""
    control = base + nload.rules
    exempt_shipped_names(nload, files, excl, root)
    return base + nload.rules, control


def check_publication(root: pathlib.Path, report=None, manifest=None, terms_path=None, contexts_path=None,
                      require_neutrality=False, expect_terms=None, expect_contexts=None) -> int:
    refusals = []
    excl, pending, xproblems = [], [], []
    try:
        excl, pending, xproblems = load_excludes(root)
    except (OSError, UnicodeDecodeError) as exc:
        refusals.append(f"PUBLISH-EXCLUDE: cannot be read as UTF-8 text ({type(exc).__name__})")
    states = [
        _private_file_state("vocabulary file", "FACTLATTICE_PUBLISH_VOCAB", vocab_path(root)),
        _private_file_state("denylist file", "FACTLATTICE_DISCLOSURE_RULES", rules_path(root)),
    ]
    refusals += [r for st in states for r in st["refusals"]]
    v, d = states
    if v["state"] != "missing" and d["state"] != "missing" and (
            os.path.realpath(vocab_path(root)) == os.path.realpath(rules_path(root))):
        refusals.append("the vocabulary file and the denylist file are the same file")
    elif v["sha256"] is not None and v["sha256"] == d["sha256"]:
        refusals.append("the vocabulary file and the denylist file have identical content (same sha256)")
    refusals += [f"PUBLISH-EXCLUDE: line {n}: entry needs 'path   # why it never crosses'"
                 for n in sorted({int(x.split(":")[1]) for x in xproblems})]

    files, lrefusals = _publication_files(root)
    refusals += lrefusals
    shipped = [x for x in files if not is_excluded(x, excl)]
    exempt = [x for x in shipped if _is_exempt(x)]
    if not lrefusals:
        if not shipped:
            refusals.append("tree: zero files shipped (nothing to scan; a result over nothing is refused)")
        elif len(shipped) - len(exempt) == 0:
            refusals.append("tree: zero files scanned (every shipped file is exempt)")
    mapping = {}
    if manifest is not None:
        mapping, mrefusals = _load_manifest(pathlib.Path(manifest), files, excl)
        refusals += mrefusals

    nload, nrefusals = _neutrality_gate(root, terms_path, contexts_path, require_neutrality, expect_terms, expect_contexts)
    refusals += nrefusals
    base = list(BUILTIN_RULES)
    for st in states:
        base += st["_rules"]
    rules, control_rules = _neutrality_rule_sets(base, nload, files, excl, root)

    fields = {
        "scanned": None, "exclude_entries": len(excl), "shipped": len(shipped),
        "exempt_unscanned": [_mask(x, rules) for x in exempt], "mapped_files": len(mapping),
        "builtin_rules": len(BUILTIN_RULES),
        "neutrality_terms": _neutrality_fields(nload),
        "private_files": {("vocabulary" if st["label"].startswith("vocab") else "denylist"):
                          {k: st[k] for k in ("state", "sha256", "rows", "rules")} for st in states},
    }

    if not refusals:
        # the existing controls first: any failure is red
        refusals += [f"control: {x}" for x in _control_failures(control_rules, echo=None, reveal=False)]

    findings = []
    if not refusals:
        raw, scanned = scan_tree(rules, root, excl, files=files, links=True, exempt_names=tuple(nload.exempt))
        fields["scanned"] = scanned
        if scanned != len(shipped) - len(exempt) or scanned == 0:
            refusals.append("tree: the scanned count disagrees with the shipped count (or is zero)")
        for path, lineno, r, _text in raw:          # the matched text is DROPPED here, on purpose
            item = {"path": _mask(path, rules), "line": lineno, "rule": r.name}
            if path in mapping:
                src = mapping[path]
                item["source"] = "(withheld: the source path matches a rule)" if _matches(rules, src) else src
            findings.append(item)

    scope = _scope_line(fields)
    if refusals:
        result, rc = "refused", EXIT_INTEGRITY
    elif findings:
        result, rc = "red", EXIT_INTEGRITY
    else:
        result, rc = "green", EXIT_OK

    if report is not None:      # the report is written BEFORE the result line is printed (F9)
        import json
        doc = {"schema": "disclosure-guard-publication-report/1", "result": result, "exit_code": rc,
               "scope_line": scope, "scope": fields, "refusals": refusals, "findings": findings,
               "mapped_files": [{"target": _mask(t, rules), "source": "(withheld)" if _matches(rules, s_) else s_}
                                for t, s_ in sorted(mapping.items())]}
        try:
            pathlib.Path(report).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            refusals.append(f"report: cannot be written ({type(exc).__name__})")
            rc = EXIT_INTEGRITY
            if result == "green":
                result = "refused"

    for x in refusals:
        print(f"⛔ publication gate refused: {x}")
    if pending:
        print(f"  note: {len(pending)} #PENDING exclude entr{'y' if len(pending)==1 else 'ies'} are "
              f"NOT excluded (scanned)")
    _neutrality_warning(nload, require_neutrality)
    print(scope)
    if result == "refused":
        print(f"RESULT: REFUSED. {len(refusals)} reason(s) above. The scan did not complete. {TRIPWIRE}")
    elif result == "red":
        by_rule = {}
        for it in findings:
            tail = f"  (mapped from {it['source']})" if "source" in it else ""
            print(f"  {it['path']}:{it['line']}  [{it['rule']}]{tail}")
            by_rule[it["rule"]] = by_rule.get(it["rule"], 0) + 1
        print(f"RESULT: RED. {len(findings)} finding(s) in {len({i['path'] for i in findings})} file(s). "
              f"Per rule: {by_rule}. The matched text is not printed. {TRIPWIRE}")
    else:
        print("RESULT: green. No rule of the named vocabulary matched in the files scanned. This is a "
              "lexical tripwire over the named vocabulary: a name nobody added passes untouched.")
    return rc


# --- the real check -----------------------------------------------------------
def _staged_added_lines():
    try:
        diff = subprocess.run(
            ["git", "diff", "--cached", "--unified=0", "--no-color", "--diff-filter=ACMR"],
            capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"disclosure_guard: cannot read the staged diff ({exc}).", file=sys.stderr)
        raise SystemExit(2)
    path, lineno = None, 0
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    for raw in diff.splitlines():
        if raw.startswith("+++ b/"):
            path = raw[6:]
        elif raw.startswith("@@"):
            m = hunk_re.match(raw)
            lineno = int(m.group(1)) if m else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            if path is not None:
                yield path, lineno, raw[1:]
            lineno += 1


def check_staged(rules, problems, missing) -> int:
    findings, scanned = [], 0
    excl, _pending, xproblems = load_excludes(_repo_root())
    problems = list(problems) + xproblems
    for path, lineno, text in _staged_added_lines():
        if _is_exempt(path) or is_excluded(path, excl):
            continue
        scanned += 1
        for r in _matches(rules, text):
            findings.append((path, lineno, r, text.strip()))

    for p in problems:
        print(f"⛔ disclosure_guard: {p}")
    if missing:
        print(f"⚠️  disclosure_guard: {missing}")

    if not findings:
        if missing:      # R7: an unconfigured run is never printed as a clean one
            print(f"⚠️  UNCONFIGURED RUN, NOT A CLEAN RESULT: {scanned} added line(s) were checked only "
                  f"against the {len(rules)} rule(s) that WERE loaded; the private file(s) named above "
                  f"were NOT loaded, so their rules were NOT checked.")
            return EXIT_INTEGRITY if problems else EXIT_OK
        print(f"✓ disclosure guard: {scanned} added line(s) scanned against "
              f"{len(rules)} rule(s), 0 findings.")
        print("  ⚠️  A DENYLIST CATCHES ONLY NAMES SOMEBODY THOUGHT TO ADD. This says none")
        print("     of those rules matched the lines you changed. It does NOT say the diff")
        print("     is safe to publish — a codename, a hostname, or one customer's")
        print("     distinctive vocabulary passes this check untouched.")
        return EXIT_INTEGRITY if problems else EXIT_OK

    print("⛔ DISCLOSURE GUARD: staged content names a deployment.\n")
    for path, lineno, r, text in findings:
        print(f"  {path}:{lineno}  [{r.name}]")
        print(f"    {text}")
        print(f"    -> {r.why}\n")
    print(f"{len(findings)} finding(s) from {len(rules)} rule(s). This repository is PUBLIC.\n")
    print("If a line legitimately must name a live corpus, it belongs in the gitignored")
    print("private/tests/ overlay, not in tests/. If the ENGINE needs it, the engine has")
    print("drifted across its own profile seam and the fix belongs in the engine.\n")
    print("Override (records the choice in the commit): git commit --no-verify")
    return EXIT_INTEGRITY


def baseline(rules, root: pathlib.Path) -> int:
    """Pre-existing tracked occurrences. NOT blocked — but COUNTED, so what is being
    grandfathered is a number somebody can look at rather than an unstated amnesty.

    ⛔ THIS SCANS IN PYTHON, THROUGH `_matches`, ON PURPOSE. The first version shelled
    out to `git grep -E "<every pattern OR'd together>"`, and reported a BASELINE OF
    ZERO over files that visibly contained matches. Cause: `git grep -E` is POSIX ERE
    and does not support the `(?!...)` negative lookahead the placeholder exemption
    needs — so the pattern matched nothing, and "nothing matched" printed exactly like
    "nothing is wrong". That is the failure class this whole tool exists to refuse, and
    it appeared INSIDE the tool. Two lessons, both kept: a measurement must run the
    SAME predicate as the check it reports on, and a zero must be corroborated before
    it is believed — which is what `--selftest` is for.
    """
    tracked = subprocess.run(["git", "ls-files", "-z"], capture_output=True, text=True,
                             cwd=root).stdout.split("\0")
    findings, scanned_files = [], 0
    for rel in tracked:
        if not rel or _is_exempt(rel):
            continue
        fp = root / rel
        try:
            text = fp.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue                       # binary or unreadable: not line content
        scanned_files += 1
        for n, line in enumerate(text.splitlines(), 1):
            for r in _matches(rules, line):
                findings.append((rel, n, r.name))

    by_file = {}
    for rel, _n, _name in findings:
        by_file[rel] = by_file.get(rel, 0) + 1
    print(f"PRE-EXISTING BASELINE: {len(findings)} tracked line(s) in {len(by_file)} "
          f"file(s), against {len(rules)} rule(s), over {scanned_files} scanned file(s).")
    for f, n in sorted(by_file.items(), key=lambda kv: -kv[1]):
        kinds = sorted({k for p, _l, k in findings if p == f})
        print(f"  {n:4d}  {f}   [{', '.join(kinds)}]")
    if not findings:
        print("  (none)")
    print("\nNOT blocked by the staged check — blocking them would fail every future")
    print("commit and the gate would be switched off. Printed so it stays visible.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true", help="run the positive controls")
    ap.add_argument("--baseline", action="store_true", help="count pre-existing occurrences")
    ap.add_argument("--all", action="store_true", help="selftest, then the staged check")
    ap.add_argument("--tree", nargs="?", const=".", metavar="DIR",
                    help="scan every file under DIR (default .) not named in PUBLISH-EXCLUDE")
    ap.add_argument("--publication", nargs="?", const=".", metavar="DIR",
                    help="the publication gate: fail closed, scan DIR (default .), always print a scope line")
    ap.add_argument("--report", metavar="FILE", help="with --publication: write the JSON report here")
    ap.add_argument("--map-manifest", metavar="FILE",
                    help="with --publication: lines of target<TAB>source for files placed by the folder map")
    ap.add_argument("--neutrality-terms", metavar="FILE",
                    help=f"a generated terms file (agent and corpus ids); default: ${NEUTRALITY_ENV}. Absent: a warning "
                         "in the lenient modes; named but unusable: nothing is loaded from it (and --publication refuses)")
    ap.add_argument("--neutrality-contexts", metavar="FILE",
                    help=f"the private table of false-positive contexts; default: ${NEUTRALITY_CONTEXTS_ENV}, else "
                         "private/neutrality-term-contexts.json under the scanned root")
    ap.add_argument("--expect-neutrality-terms-sha256", metavar="HEX",
                    help="the sha256 of the terms file's bytes as the caller validated it; a file that now hashes differently is refused")
    ap.add_argument("--expect-neutrality-contexts-sha256", metavar="HEX", help="the same, for the contexts table")
    ap.add_argument("--require-neutrality-terms", action="store_true",
                    help="with --publication: refuse unless a usable terms file is named")
    args = ap.parse_args()
    if args.publication is not None and (args.tree is not None or args.selftest or args.baseline or args.all):
        ap.error("--publication is a mode of its own; do not combine it with another mode")
    if args.publication is None and (args.report or args.map_manifest or args.require_neutrality_terms):
        ap.error("--report, --map-manifest and --require-neutrality-terms belong to --publication")
    if args.publication is not None:
        return check_publication(pathlib.Path(args.publication).resolve(), args.report, args.map_manifest,
                                 args.neutrality_terms, args.neutrality_contexts, args.require_neutrality_terms,
                                 args.expect_neutrality_terms_sha256, args.expect_neutrality_contexts_sha256)

    root = _repo_root()
    if args.tree is not None:
        root = pathlib.Path(args.tree).resolve()
    rules, problems, missing = load_rules(root)
    nload = load_neutrality(args.neutrality_terms, args.neutrality_contexts, root, None,
                            args.expect_neutrality_terms_sha256, args.expect_neutrality_contexts_sha256)
    if nload.state == "unusable":      # a named input that was not loaded is never a configured run (the selftest ends PARTIAL)
        missing = (missing + "\n   " if missing else "") + (
            f"PRIVATE FILE NOT LOADED: the neutrality terms input is unusable ({nload.problem}), so its rules were NOT checked.")
    rules_all = rules + nload.rules
    if args.selftest or args.baseline:
        rules_scan = rules_all
    else:
        exempt_shipped_names(nload, _tree_files(root), load_excludes(root)[0], root)
        rules_scan = rules + nload.rules
    for line in nload.notes():
        print(line)
    if nload.exempt:
        print(exempt_note(nload))
    if nload.state == "loaded" and not (args.selftest or args.baseline):
        print(corpus_exempt_note(nload))

    if args.tree is not None:
        return check_tree(rules_scan, problems, missing, root, exempt_names=tuple(nload.exempt))
    if args.selftest:
        return selftest(rules_all, problems, missing)
    if args.baseline:
        return baseline(rules_all, root)
    if args.all:
        rc = selftest(rules_all, problems, missing)
        if rc == EXIT_INTEGRITY:
            return rc
        return check_staged(rules_scan, problems, missing)
    return check_staged(rules_scan, problems, missing)


if __name__ == "__main__":
    sys.exit(main())
