#!/usr/bin/env python3
"""Edit the disclosure guard's private rules files BLIND: structured instructions in, counts, line numbers and a safe name out.

    tools/vocab_edit.py append --pattern P --control C --why W --name N [--file F] [--dry-run]
    tools/vocab_edit.py add-lookbehind --where-matches PROBE --text T [--file F] [--dry-run]
    tools/vocab_edit.py remove --name N [--file F] [--dry-run]
    tools/vocab_edit.py count [--file F]

WHY. The rules files (`private/publish-vocabulary.tsv`, `private/disclosure-denylist.tsv`; row format in
`tools/disclosure-denylist.example.tsv`: pattern TAB control TAB why [TAB name]) name what must never be published. Every
miss should become a rule quickly, but the agent that maintains the files must not read them: anything in an agent's context
can leak into what it writes. So this tool reads and writes the file itself. It prints counts, line numbers and the text of a
row's FOURTH column (the name) when that text is slug-shaped (letters, digits and single hyphens, either case), no loaded rule or
builtin matches it, and the guard loads the row; anything else there is withheld. A stray tab in ANY field (pattern, control or
reason) shifts row text into the fourth column, so keep tabs out of every field; the tool refuses tabs in what it writes itself.
An error message holds no row text (an internal error prints its class name only, never its text, because a regex error's text
can quote the pattern).

NAMES ARE PRINTED. A rule name is the one piece of a row this tool shows, so a name must never carry a term: do not name a rule
after a client or a firm. The tool refuses a new name that any loaded rule (or the guard's builtins) matches, and it withholds, in
what it prints, any existing name that is not slug-shaped (`^[a-z0-9]+(?:-[a-z0-9]+)*$`, ASCII, either case), that a loaded
rule (or builtin) matches, or that sits in a row the guard refuses to load (`_row_info` prints only rows in the set of rules
`_parse_rows` loaded): it shows `(name withheld, line N)` instead, whatever the row's column count. A stray tab moves later
text into the fourth column, and a slug-shaped token there that no rule matches still prints; the tool cannot tell it from a
real name.

THE ONE EXCEPTION: the arguments you type on the command line (P, C, W, N, PROBE, T) are yours. The shell, your terminal
and your history show them, and the tool does not repeat them in its own output. Do not paste a rule into a place where a
log or another reader would see it.

OPERATIONS (one per call; the first three are write operations):
  append          add one row at the end. Refused when C does not match P (case-insensitively, as the guard compiles it), when the
                  name is already used in the file, or when the guard's own row parser would not load the row back as written.
  add-lookbehind  for every row whose compiled pattern matches the string PROBE, rewrite the pattern to `(?<!T)(?:<old>)` with T
                  regex-escaped, so the rule stops firing where it is preceded by T. Then it CHECKS the rewrite: T placed
                  directly before the row's own match in PROBE must no longer match from inside that match (a pattern with an
                  optional or variable-width start, such as `\\w*x`, can still match from a later start). PROBE is the text the
                  rule fired on, not a whole line, and the check does not try other text before the match. Refused, for the whole operation and with nothing written, when no row
                  matches PROBE, when a row starts with a global inline flag other than a leading `(?i)` (it cannot be wrapped), when a
                  rewritten row's own control no longer matches, or when a rewritten row still matches the probe (the line
                  numbers are printed, never the control).
                  A leading `(?i)` (exactly those four characters) is dropped, not refused: the guard compiles every rule
                  case-insensitively already, so the rewrite is `(?<!T)(?:<rest>)`; `(?s)`, `(?x)`, `(?im)`, `(?a)` etc. are refused.
  remove          remove the row or rows with that name (a row without a name column is called `rule-<line number>`, as the guard
                  calls it). Refused when no row has the name, and when it would leave the file with no row (the guard treats an
                  empty vocabulary as a failure).
  count           print the row count and the number of rows per name. Writes nothing.

EVERY WRITE OPERATION
  - takes --file (default: the file the guard reads, see below) and --dry-run (report, write nothing);
  - refuses text holding a tab or any character at which the guard's parser ends a line;
  - changes only the bytes of the rows it names: line endings, a final newline (or the lack of one), comments, blank lines and
    row order are untouched. (An append to a file with no final newline first adds the line ending the file already uses.)
  - writes atomically (a temporary file in the same folder, then a rename). Before the write it takes a backup: a full copy of
    the private file, in a folder of its own under $TMPDIR. A write REFUSES TO RUN when TMPDIR is unset or not a
    writable folder (a dry run needs no TMPDIR): there is no fallback to a shared default folder. The backup is kept only while it is needed: it is deleted (by its own path)
    after a write whose check passed, and after a restore that was verified byte for byte. It is kept, and its path printed,
    only when the original was NOT restored;
  - then loads the file back with the guard's OWN loader and runs the guard's own controls (every rule against its own
    control, the legitimate-line controls). If anything fails, it restores the original bytes and then COMPARES the file's bytes
    with the original's.

EXIT CODES, three outcomes for a write (plus bad usage):
  0  done (or, with --dry-run, would be done).
  1  refused, or the check failed and the original was restored (verified byte for byte): the file is as it was before the call.
  3  ORIGINAL NOT RESTORED. The write happened, and then the check failed (or the tool crashed) and the restore failed or did not
     reproduce the original. The file may hold the edit. The message says so and prints the backup's path, the only copy of
     the original: restore it by hand, then delete it. (Exit 2 is argparse's own and means bad usage, so the code for this
     outcome is 3, not 2.)
  2  bad usage (nothing was read or written).
A value that starts with `-` needs the `--option=value` form. Only the file given by --file is read: a name used in the
other rules file is not checked. --file defaults to the file the guard reads: $FACTLATTICE_PUBLISH_VOCAB when set, otherwise
private/publish-vocabulary.tsv under the repository root (pass the denylist, or $FACTLATTICE_DISCLOSURE_RULES's file, with --file).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disclosure_guard as dg  # noqa: E402  (the guard's own row parser, loader and controls: nothing is re-derived here)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_FILE = REPO_ROOT.joinpath(*dg.DEFAULT_VOCAB_REL)  # the guard's default; default_file() also honours its env variable
EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_NOT_RESTORED = 0, 1, 2, 3
# every character at which str.splitlines (the guard's line splitter) ends a line. The field separator (a tab) is not here: it
# is refused separately, in _bad_text.
_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"


def default_file() -> pathlib.Path:
    """The file the guard itself reads as the publish vocabulary: its env override, else the default under the repo root."""
    return dg.vocab_path(REPO_ROOT)


class Refused(Exception):
    """A refusal. The message is written by this tool and never contains rule text."""


class NotRestored(Exception):
    """The edit was written, the check failed (or the tool crashed), and the original could not be put back and verified."""

    def __init__(self, backup: pathlib.Path):
        super().__init__("original not restored")
        self.backup = backup


def _bad_text(s: str) -> bool:
    return not s or "\t" in s or any(c in s for c in _BREAKS)


def _need_clean(label: str, s: str) -> None:
    if _bad_text(s):
        raise Refused(f"{label} is empty or holds a tab or a line break")


# --- reading the file with the guard's parser ------------------------------------------------------------------------------
def _read(path: pathlib.Path) -> tuple[bytes, str]:
    if not path.is_file():
        raise Refused("the file does not exist or is not a regular file")
    raw = path.read_bytes()
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("the file is not valid UTF-8") from None


def _line_of(rule) -> int:
    return int(rule.source.rsplit(":", 1)[1])


def _row_names(text: str) -> dict[int, str]:
    """line number -> name of every row, using the guard's naming (4th column, else `rule-<line>`). A row the guard refuses
    to load still has a name here, so a name is never reused by accident."""
    out = {}
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) >= 3:
            out[n] = parts[3].strip() if len(parts) > 3 else f"rule-{n}"
    return out


_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.IGNORECASE | re.ASCII)


def _row_info(path: pathlib.Path, text: str) -> dict[int, tuple[str, bool]]:
    """line number -> (name, printable). A name is PRINTABLE only when its row is loaded by the guard, it is slug-shaped (ASCII letters and digits in
    hyphen-joined runs) AND no loaded rule, and no guard builtin, matches the name itself. Anything else (a space, so the tail of a
    reason that held a tab; a non-slug character; a term a rule matches) is withheld, whatever the row's column count."""
    parsed = dg._parse_rows(path, text)[0]
    loaded = {_line_of(r) for r in parsed}  # a row the guard refuses to load is never printed by name
    rules = [*dg.BUILTIN_RULES, *parsed]
    out = {}
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        name = parts[3].strip() if len(parts) > 3 else f"rule-{n}"
        out[n] = (name, n in loaded and bool(_SLUG.fullmatch(name)) and not any(r.rx.search(name) for r in rules))
    return out


def _shown(info: dict[int, tuple[str, bool]], n: int) -> str:
    name, ok = info[n]
    return name if ok else f"(name withheld, line {n})"


# --- the selftest: the guard's own loader and controls -------------------------------------------------------------------
def _verdict(path: pathlib.Path, text: str | None = None) -> dict:
    """Run the guard's loader and controls over one rules file. `text` None = load the file from disk through the guard's loader
    (`_load_rows_ex`; every row that misses its own control is refused there, so it is a problem line, never a rule); otherwise parse that text with the guard's `_parse_rows` (a dry run). Only counts and names come back."""
    if text is None:
        rules, problems, rows = dg._load_rows_ex(path)
        problem_lines = [int(m.group(1)) for p in problems for m in [re.search(rf"{re.escape(path.name)}:(\d+):", p)] if m]
    else:
        rules, probs, rows = dg._parse_rows(path, text)
        problem_lines = [n for n, _s, _v in probs]
        problems = probs
    allrules = list(dg.BUILTIN_RULES) + rules
    failures = dg._control_failures(allrules, echo=None, reveal=False)
    info = _row_info(path, path.read_text(encoding="utf-8") if text is None else text)
    clean_hit = sorted({_shown(info, _line_of(r)) for r in rules if any(r.rx.search(line) for line in dg.CONTROL_CLEAN)})
    return {
        "ok": not failures and not problems,
        "failures": len(failures),
        "problem_lines": problem_lines,
        "clean_hit": clean_hit,
        "rules": len(rules),
        "rows": rows,
    }


def _verdict_text(v: dict) -> list[str]:
    head = f"selftest: {'PASS' if v['ok'] else 'FAIL'} rules={v['rules']} (+{len(dg.BUILTIN_RULES)} builtin) rows={v['rows']}"
    if v["ok"]:
        return [head]
    lines = [head + f" failures={v['failures']} refused-rows={len(v['problem_lines'])}"]
    if v["problem_lines"]:
        lines.append("  rows the guard refuses to load, at lines: " + ", ".join(map(str, v["problem_lines"])))
    if v["clean_hit"]:
        lines.append("  rules that fire on a legitimate-line control: " + ", ".join(v["clean_hit"]))
    return lines


# --- the planned edit (pure: bytes in, bytes out) ------------------------------------------------------------------------
class Plan:
    def __init__(self, new_text: str, changed: list[int], names: list[str]):
        self.new_text, self.changed, self.names = new_text, changed, names


def _line_ending(text: str) -> str:
    for line in reversed(text.splitlines(keepends=True)):
        stripped = line.rstrip("\r\n")
        if stripped != line:
            return line[len(stripped) :]
    return "\n"


def plan_append(text: str, path: pathlib.Path, pattern: str, control: str, why: str, name: str) -> Plan:
    for label, s in (("pattern", pattern), ("control", control), ("why", why), ("name", name)):
        _need_clean(label, s)
    try:
        rule = dg.Rule(name, pattern, control, why, "x:0")
    except re.error as exc:
        raise Refused(f"the pattern does not compile (position {exc.pos})") from None
    if not rule.rx.search(control):
        raise Refused("the control does not match the pattern (case-insensitive, as the guard compiles it)")
    if name in _row_names(text).values():
        raise Refused("the name is already used in the file")
    if any(r.rx.search(name) for r in (*dg.BUILTIN_RULES, *dg._parse_rows(path, text)[0], rule)):
        raise Refused("a loaded rule matches the name (a name is printed, so it must not carry a term)")
    row = "\t".join((pattern, control, why, name))
    rules, problems, _rows = dg._parse_rows(path, row)
    if problems or len(rules) != 1 or (rules[0].rx.pattern, rules[0].control, rules[0].why, rules[0].name) != (pattern, control, why, name):
        raise Refused(
            "the guard's row parser would not load the row back exactly as given (a comment mark, edge whitespace or a short row)"
        )
    eol = _line_ending(text)
    head = text if not text or text.endswith(tuple(_BREAKS)) else text + eol
    new = head + row + eol
    return Plan(new, [len(new.splitlines())], [name])


_GLOBAL_FLAG = re.compile(r"\(\?[aiLmsux]+\)")


_LEADING_I = "(?i)"


def _rest_is_ignorecase(rest: str, rule) -> bool:
    """True when the guard's own loader would compile `rest` (the pattern without its leading `(?i)`) case-insensitively. Built exactly
    as `_parse_rows` builds a rules-file rule (`dg.Rule(name, pattern, control, why, source)`, default flags; disclosure_guard.py:161-164
    and :243), so it is false if the guard stops defaulting to IGNORECASE. Not `rule.rx.flags`: the leading `(?i)` sets that flag itself."""
    try:
        return bool(dg.Rule(rule.name, rest, rule.control, rule.why, rule.source).rx.flags & re.IGNORECASE)
    except re.error:
        return False


def _body_for_wrap(rule) -> str | None:
    """The pattern text to wrap, or None when it starts with a global inline flag that cannot be wrapped. A leading `(?i)` (exactly
    those four characters) is dropped when the guard already compiles the rule case-insensitively (disclosure_guard.Rule, default
    `flags=re.IGNORECASE`, applied by `re.compile(pattern, flags)`), so it is redundant; the rest must not start with another flag group."""
    pat = rule.rx.pattern
    if pat.startswith(_LEADING_I) and _rest_is_ignorecase(pat[len(_LEADING_I) :], rule):
        pat = pat[len(_LEADING_I) :]
    return None if _GLOBAL_FLAG.match(pat) else pat


def _compile_rewrite(rule, text: str):
    return dg.Rule(rule.name, f"(?<!{re.escape(text)})(?:{_body_for_wrap(rule)})", rule.control, rule.why, rule.source)


def plan_lookbehind(text: str, path: pathlib.Path, probe: str, lb_text: str) -> Plan:
    _need_clean("--where-matches", probe)
    _need_clean("--text", lb_text)
    rules, _problems, _rows = dg._parse_rows(path, text)
    hits = [(r, r.rx.search(probe)) for r in rules]
    hits = [(r, m) for r, m in hits if m]
    if not hits:
        raise Refused("no row's pattern matches the probe")
    info = _row_info(path, text)
    lines = text.splitlines(keepends=True)
    flagged, broken, still, changed, names = [], [], [], [], []
    for r, m in hits:
        n = _line_of(r)
        if _body_for_wrap(r) is None:
            flagged.append(n)
            continue
        try:
            new_rule = _compile_rewrite(r, lb_text)
        except re.error:
            broken.append(n)
            continue
        if not new_rule.rx.search(r.control):
            broken.append(n)
            continue
        # the check that the rewrite worked: the row's own match in the probe, preceded by T, must not match from inside
        # that match (a later match elsewhere in the probe is another occurrence, and is fine)
        after = new_rule.rx.search(lb_text + probe[m.start() :])
        if after and after.start() < len(lb_text) + (m.end() - m.start()):
            still.append(n)
            continue
        body = lines[n - 1].rstrip("\r\n")
        eol = lines[n - 1][len(body) :]
        parts = body.split("\t")
        parts[0] = new_rule.rx.pattern
        lines[n - 1] = "\t".join(parts) + eol
        changed.append(n)
        names.append(_shown(info, n))
    problems = []
    if flagged:
        problems.append(
            "a row starts with a global inline flag other than a leading (?i), which cannot be wrapped in a lookbehind, at lines: "
            + ", ".join(map(str, sorted(flagged)))
        )
    if broken:
        problems.append(
            "a rewritten row would no longer match its own control (or would not compile), at lines: " + ", ".join(map(str, sorted(broken)))
        )
    if still:
        problems.append(
            "a rewritten row still matches the probe when T precedes it (a pattern with an optional or variable-width start), at lines: "
            + ", ".join(map(str, sorted(still)))
        )
    if problems:
        raise Refused("; ".join(problems))
    return Plan("".join(lines), sorted(changed), names)


def plan_remove(text: str, path: pathlib.Path, name: str) -> Plan:
    _need_clean("--name", name)
    gone = sorted(n for n, nm in _row_names(text).items() if nm == name)
    if not gone:
        raise Refused("no row has that name")
    lines = text.splitlines(keepends=True)
    kept = [line for i, line in enumerate(lines, 1) if i not in gone]
    if kept and len(lines) in gone and lines[-1] == lines[-1].splitlines()[0]:
        # the file had no line ending after its last row and that row is going: the new last line must not gain one either
        kept[-1] = kept[-1].splitlines()[0] if kept[-1].splitlines() else ""
    keep = "".join(kept)
    if dg._parse_rows(path, keep)[2] == 0:
        raise Refused("removing it would leave the file with no row")
    info = _row_info(path, text)
    return Plan(keep, gone, list(dict.fromkeys(_shown(info, n) for n in gone)))


# --- writing -------------------------------------------------------------------------------------------------------------
def _atomic_write(path: pathlib.Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".vocab-edit-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _tmp_root() -> pathlib.Path:
    """$TMPDIR, or a refusal: a backup of a private file must not land in a shared default folder."""
    td = os.environ.get("TMPDIR")
    if not td:
        raise Refused("TMPDIR is not set (a backup of a private rules file is never put in a default shared folder); nothing was written")
    root = pathlib.Path(td)
    if not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
        raise Refused("TMPDIR is not a writable folder; nothing was written")
    return root


def _backup(path: pathlib.Path, raw: bytes) -> pathlib.Path:
    folder = pathlib.Path(tempfile.mkdtemp(prefix="vocab-edit-DO-NOT-READ-", dir=str(_tmp_root())))  # 0700
    dest = folder / "private-rules-file-original"
    try:
        dest.write_bytes(raw)
        os.chmod(dest, 0o600)
    except BaseException:
        _drop_backup(dest)
        raise
    return dest


def _drop_backup(dest: pathlib.Path) -> bool:
    """Delete the backup file and its own folder, each by its own path. -> True when both are gone."""
    try:
        if dest.exists():
            os.unlink(dest)
        os.rmdir(dest.parent)
        return True
    except OSError:
        return False


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _restore_verified(path: pathlib.Path, raw: bytes) -> bool:
    """Write the original bytes back and COMPARE the file with them. -> True only when the file now holds exactly `raw`."""
    try:
        _atomic_write(path, raw)
        return path.read_bytes() == raw
    except BaseException:  # a failed restore is reported by the caller, never allowed to hide the first failure
        return False


def _apply(path: pathlib.Path, raw: bytes, plan: Plan) -> tuple[dict, bool, pathlib.Path | None]:
    """Back up, write, load back through the guard, and put the original back when the check fails.
    -> (verdict, restored, backup left behind or None). Raises NotRestored (the backup's path inside) when the file may hold
    the edit and the original could not be put back and verified; re-raises whatever stopped the check once the file is back."""
    backup = _backup(path, raw)
    wrote, failure, verdict = False, None, None
    try:
        if _sha(path.read_bytes()) != _sha(raw):
            raise Refused("the file changed while the edit was being planned; nothing was written")
        wrote = True
        _atomic_write(path, plan.new_text.encode("utf-8"))
        verdict = _verdict(path)
    except BaseException as exc:
        failure = exc
    if not wrote:
        left = None if _drop_backup(backup) else backup
        if left:
            print(f"vocab_edit: the backup copy could not be deleted; it is a full copy of a private file: {left}", file=sys.stderr)
        raise failure
    if failure is None and verdict["ok"]:
        return verdict, False, None if _drop_backup(backup) else backup
    if not _restore_verified(path, raw):
        raise NotRestored(backup)
    left = None if _drop_backup(backup) else backup
    if failure is not None:
        if left:
            print(f"vocab_edit: the backup copy could not be deleted; it is a full copy of a private file: {left}", file=sys.stderr)
        raise failure
    return verdict, True, left


# --- the operations ------------------------------------------------------------------------------------------------------
def _summary(op: str, path: pathlib.Path, before: int, after: int, plan: Plan, dry: bool) -> list[str]:
    return [
        f"vocab_edit: op={op} file={path.name}{' (dry run: nothing written)' if dry else ''}",
        f"rows: {before} -> {after}",
        "changed lines: " + (", ".join(map(str, plan.changed)) or "none"),
        "affected rules: " + (", ".join(plan.names) or "none"),
    ]


def run_write(op: str, path: pathlib.Path, dry: bool, planner) -> int:
    path = path.resolve()
    if not dry:
        _tmp_root()  # refuse before anything is read or planned when there is nowhere private to put the backup
    raw, text = _read(path)
    plan = planner(text, path)
    before_rows = dg._parse_rows(path, text)[2]
    after_rows = dg._parse_rows(path, plan.new_text)[2]
    out = _summary(op, path, before_rows, after_rows, plan, dry)
    before = _verdict(path, text)
    out.append("selftest before the edit: " + ("PASS" if before["ok"] else "FAIL"))
    if dry:
        v = _verdict(path, plan.new_text)
        print("\n".join(out + _verdict_text(v)))
        return EXIT_OK if v["ok"] else EXIT_REFUSED
    v, restored, left = _apply(path, raw, plan)
    out += _verdict_text(v)
    if restored:
        out.append("ROLLED BACK: the original bytes are back in place (compared byte for byte); the file is unchanged.")
    out.append(
        "backup: none kept (it is deleted once the check has passed or the original is verified back)"
        if left is None
        else f"backup NOT deleted (a full copy of a private file; delete it yourself): {left}"
    )
    print("\n".join(out))
    return EXIT_REFUSED if restored else EXIT_OK


def run_count(path: pathlib.Path) -> int:
    path = path.resolve()
    _raw, text = _read(path)
    info = _row_info(path, text)
    shown = [name for name, ok in info.values() if ok]
    withheld = [n for n, (_name, ok) in info.items() if not ok]
    print(f"vocab_edit: op=count file={path.name}")
    print(f"rows: {dg._parse_rows(path, text)[2]}")
    for nm in sorted(set(shown)):
        print(f"  {nm}: {shown.count(nm)}")
    if withheld:
        print(
            "rows whose name is withheld (not slug-shaped, or a loaded rule or a builtin matches it, or the guard refuses the row), at lines: "
            + ", ".join(map(str, withheld))
        )
    return EXIT_OK


# --- command line --------------------------------------------------------------------------------------------------------
class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse messages can quote a value; say only what kind of mistake it was
        kind = "a required argument is missing" if "required" in message else "bad usage"
        print(f"vocab_edit: {kind} (run with --help). No value is echoed here.", file=sys.stderr)
        sys.exit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(
        description=__doc__.splitlines()[0],
        epilog="Prints counts, line numbers and the text of a row's fourth column when that text is slug-shaped (letters, digits and "
        "single hyphens, either case), no loaded rule or builtin matches it and the guard loads the row; anything else there is withheld. "
        "A stray tab in any field (pattern, control or reason) shifts row text into the fourth column: keep tabs out of every field. "
        "Arguments you type are your own and may show in your shell. "
        "See the module docstring (tools/vocab_edit.py) for the full contract.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    sub = ap.add_subparsers(dest="op", required=True, parser_class=_Parser)

    def common(p, write=True):
        p.add_argument(
            "--file",
            default=None,
            help="the rules file (default: the file the guard reads: $FACTLATTICE_PUBLISH_VOCAB, else private/publish-vocabulary.tsv under the repo root)",
        )
        if write:
            p.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")

    a = sub.add_parser("append", help="add one row")
    for opt in ("pattern", "control", "why", "name"):
        a.add_argument(
            f"--{opt}", required=True, help="the rule's name: it is PRINTED, so it must not carry a term" if opt == "name" else None
        )
    common(a)
    lb = sub.add_parser("add-lookbehind", help="exclude a preceding text from every rule that matches a probe (a leading (?i) is dropped as redundant; other global flags are refused)")
    lb.add_argument("--where-matches", required=True, metavar="PROBE")
    lb.add_argument("--text", required=True, metavar="T")
    common(lb)
    rm = sub.add_parser("remove", help="remove the row(s) with a name")
    rm.add_argument("--name", required=True, help="the rule's name (names are printed; one that holds a term is withheld)")
    common(rm)
    common(sub.add_parser("count", help="row count and rows per name"), write=False)
    return ap


def _dispatch(args) -> int:
    path = pathlib.Path(args.file) if args.file else default_file()
    if args.op == "count":
        return run_count(path)
    if args.op == "append":
        return run_write("append", path, args.dry_run, lambda t, p: plan_append(t, p, args.pattern, args.control, args.why, args.name))
    if args.op == "add-lookbehind":
        return run_write("add-lookbehind", path, args.dry_run, lambda t, p: plan_lookbehind(t, p, args.where_matches, args.text))
    return run_write("remove", path, args.dry_run, lambda t, p: plan_remove(t, p, args.name))


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except Refused as exc:
        print(f"vocab_edit: REFUSED: {exc}. Nothing was changed.", file=sys.stderr)
        return EXIT_REFUSED
    except NotRestored as exc:
        print(
            "vocab_edit: ORIGINAL NOT RESTORED. The file may hold the edit. The only copy of the original is "
            f"{exc.backup} (a full copy of a private file: put it back by hand, then delete it).",
            file=sys.stderr,
        )
        return EXIT_NOT_RESTORED
    except Exception as exc:  # never print the message: a regex or parse error can quote a rule
        print(f"vocab_edit: internal error ({type(exc).__name__}); the message is withheld because it could quote a rule.", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
