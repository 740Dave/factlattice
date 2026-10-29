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
                                           # named in PUBLISH-EXCLUDE: the publication check

    FACTLATTICE_DISCLOSURE_RULES=/path/rules.tsv   # override the rules-file location
    FACTLATTICE_PUBLISH_VOCAB=/path/vocab.tsv      # override the vocabulary-file location

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

INSTALL
    ln -sf ../../tools/pre-commit .git/hooks/pre-commit
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
import sys

DEFAULT_RULES_REL = ("private", "disclosure-denylist.tsv")
DEFAULT_VOCAB_REL = ("private", "publish-vocabulary.tsv")
EXCLUDE_FILE = "PUBLISH-EXCLUDE"


class Rule:
    __slots__ = ("name", "rx", "control", "why", "source")

    def __init__(self, name, pattern, control, why, source):
        self.name = name
        self.rx = re.compile(pattern, re.IGNORECASE)
        self.control = control
        self.why = why
        self.source = source


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

EXEMPT_PREFIXES = (
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


def _load_rows(path: pathlib.Path):
    """Parse one rules file -> (rules, problems). A row whose pattern misses its own control is
    refused here, so a mute rule can never sit in the file looking like protection."""
    rules, problems = [], []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 3:
            problems.append(
                f"{path}:{n}: expected 'pattern<TAB>control<TAB>why', got {raw!r}")
            continue
        pattern, control, why = parts[0].strip(), parts[1], parts[2].strip()
        name = parts[3].strip() if len(parts) > 3 else f"rule-{n}"
        try:
            rule = Rule(name, pattern, control, why, f"{path.name}:{n}")
        except re.error as exc:
            problems.append(f"{path}:{n}: bad regex {pattern!r} ({exc})")
            continue
        if not rule.rx.search(control):
            problems.append(
                f"{path}:{n}: rule {name!r} does NOT match its own control {control!r}. "
                f"A rule with no working control will never fire; refused at load.")
            continue
        rules.append(rule)
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
            f"NO PRIVATE RULES FILE at {path} — only the {len(BUILTIN_RULES)} structural "
            f"builtin rule(s) are active, so NO client-specific term is being checked for. "
            f"Create it (pattern<TAB>control<TAB>why) or set FACTLATTICE_DISCLOSURE_RULES; "
            f"see tools/disclosure-denylist.example.tsv.")
    else:
        r, p = _load_rows(path)
        rules += r
        problems += p
    vpath = vocab_path(repo_root)
    if not vpath.is_file():
        missing.append(
            f"NO PUBLISH VOCABULARY at {vpath} — product/corpus/agent names, paths and "
            f"test-identity values are NOT being checked for. Create it (same row format) "
            f"or set FACTLATTICE_PUBLISH_VOCAB.")
    else:
        r, p = _load_rows(vpath)
        rules += r
        problems += p
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
    """The ONE predicate. The selftest and the real check both call it, so a selftest
    cannot pass against a matcher the real path does not use."""
    return [r for r in rules if r.rx.search(line)]


# --- the positive control -----------------------------------------------------
EXIT_OK, EXIT_INTEGRITY, EXIT_UNCONFIGURED = 0, 1, 2


def selftest(rules, problems, missing) -> int:
    failures = list(problems)
    for r in rules:
        if r.rx.search(r.control):
            print(f"  caught [{r.name}] on its own control   ({r.source})")
        else:
            failures.append(f"MUTE  [{r.name}] did not fire on its control {r.control!r}")
    for line in CONTROL_CLEAN:
        hit = [r.name for r in _matches(rules, line)]
        if hit:
            failures.append(f"FALSE POSITIVE on legitimate line {line!r} -> {hit}")
        else:
            print(f"  clean  {line!r}")

    failures += _tree_control(rules)

    if failures:
        print("\n⛔ DISCLOSURE GUARD SELFTEST FAILED — INTEGRITY. A green run of the real"
              "\n   check would have proved NOTHING; only that nothing ran.\n")
        for f in failures:
            print(f"  {f}")
        return EXIT_INTEGRITY

    print(f"\n✓ selftest: {len(rules)} rule(s), each fired on its OWN control; "
          f"{len(CONTROL_CLEAN)} legitimate line(s) stayed clean.")
    print("  The matchers demonstrably RAN.")

    if missing:
        print(f"\n⚠️  UNCONFIGURED (exit {EXIT_UNCONFIGURED}, not a matcher failure):")
        print(f"   {missing}")
        print("   The structural rules above are real and did run. No IDENTITY rule did.")
        return EXIT_UNCONFIGURED
    return EXIT_OK


def _tree_control(rules):
    """Tree-mode NEGATIVE control: plant a rule's own control string in a throwaway tree, once in
    an ordinary file and once under an excluded prefix. The first MUST be found and the second
    MUST NOT, or tree mode is mute (or ignores the exclusion) and a green `--tree` means nothing."""
    import tempfile
    probe = next((r for r in rules if r.source != "builtin"), rules[0] if rules else None)
    if probe is None:
        return ["no rules to run the tree-mode control with"]
    with tempfile.TemporaryDirectory(prefix="dg-tree-") as d:
        root = pathlib.Path(d)
        (root / "kept").mkdir()
        (root / "skipped").mkdir()
        (root / "kept" / "planted.txt").write_text(probe.control + "\n", encoding="utf-8")
        (root / "skipped" / "planted.txt").write_text(probe.control + "\n", encoding="utf-8")
        found, _n = scan_tree(rules, root, ["skipped/"])
    paths = {f[0] for f in found}
    out = []
    if "kept/planted.txt" not in paths:
        out.append(f"TREE MODE MUTE: a planted [{probe.name}] control in a non-excluded file was not found")
    if any(x.startswith("skipped/") for x in paths):
        out.append("TREE MODE IGNORES PUBLISH-EXCLUDE: a planted hit under an excluded prefix was reported")
    if not out:
        print(f"  tree-mode control: planted [{probe.name}] found in kept/, ignored under excluded skipped/")
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


def scan_tree(rules, root: pathlib.Path, excl):
    """-> (findings, scanned_files). Scans file CONTENT and the PATH itself. Unreadable bytes are
    decoded with replacement, never skipped: a file we could not read is not a file we cleared."""
    findings, scanned = [], 0
    for rel in _tree_files(root):
        if rel.startswith(EXEMPT_PREFIXES) or is_excluded(rel, excl):
            continue
        scanned += 1
        for r in _matches(rules, rel):
            findings.append((rel, 0, r, f"(file name) {rel}"))
        try:
            text = (root / rel).read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            findings.append((rel, 0, Rule("unreadable", "x", "x", "", "builtin"), f"cannot read: {exc}"))
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for r in _matches(rules, line):
                findings.append((rel, n, r, line.strip()))
    return findings, scanned


def check_tree(rules, problems, missing, root: pathlib.Path) -> int:
    excl, pending, xproblems = load_excludes(root)
    problems = list(problems) + xproblems
    for p in problems:
        print(f"⛔ disclosure_guard: {p}")
    if vocab_missing(root):
        print(f"⛔ disclosure_guard --tree: NO PUBLISH VOCABULARY at {vocab_path(root)}. A tree scan "
              f"with no vocabulary would report 0 findings having checked nothing; refused.")
        return EXIT_INTEGRITY
    findings, scanned = scan_tree(rules, root, excl)
    if pending:
        print(f"  note: {len(pending)} #PENDING exclude entr{'y' if len(pending)==1 else 'ies'} "
              f"are NOT excluded (scanned): {', '.join(pending)}")
    if not findings:
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
        if path.startswith(EXEMPT_PREFIXES) or is_excluded(path, excl):
            continue
        scanned += 1
        for r in _matches(rules, text):
            findings.append((path, lineno, r, text.strip()))

    for p in problems:
        print(f"⛔ disclosure_guard: {p}")
    if missing:
        print(f"⚠️  disclosure_guard: {missing}")

    if not findings:
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
        if not rel or rel.startswith(EXEMPT_PREFIXES):
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
    args = ap.parse_args()

    root = _repo_root()
    if args.tree is not None:
        root = pathlib.Path(args.tree).resolve()
    rules, problems, missing = load_rules(root)

    if args.tree is not None:
        return check_tree(rules, problems, missing, root)
    if args.selftest:
        return selftest(rules, problems, missing)
    if args.baseline:
        return baseline(rules, root)
    if args.all:
        rc = selftest(rules, problems, missing)
        if rc == EXIT_INTEGRITY:
            return rc
        return check_staged(rules, problems, missing)
    return check_staged(rules, problems, missing)


if __name__ == "__main__":
    sys.exit(main())
