"""tools/vocab_edit.py: the blind editor of the disclosure guard's private rules files.

Every file here is planted in tmp_path, with synthetic terms (an imaginary firm, `zephyrquill`). The real private files are
never read. The tool runs as a subprocess with TMPDIR pointed at tmp_path, so its backups land there too. Each test that runs
the tool checks that neither stream carries a planted pattern, control or reason: the tool's whole contract is that it
prints counts, line numbers and rule names only.

`VOCAB_EDIT_UNDER_TEST` names another copy of the tool (a mutant that prints a pattern, to show these tests can fail).
"""

import os
import pathlib
import re
import subprocess
import sys

import pytest

TOOLS = pathlib.Path(__file__).resolve().parents[1] / "tools"
TOOL = pathlib.Path(os.environ.get("VOCAB_EDIT_UNDER_TEST") or TOOLS / "vocab_edit.py")
T = "\t"

# planted secrets: three tokens per row, each distinctive, so a leak of any one is caught by a plain substring test
ROW_A = T.join([r"zephyrquill\d+", "zephyrquill42", "why-text-alpha-9c1", "firm-name"])
ROW_B = T.join([r"quartzmoor", "A Quartzmoor Order", "why-text-beta-4d2", "site-name"])
SECRETS = ["zephyrquill", "why-text-alpha-9c1", "Quartzmoor", "quartzmoor", "why-text-beta-4d2"]
CONTENT = "# planted vocabulary\n" + ROW_A + "\n\n" + ROW_B + "\n# trailing comment\n"


def run(tmp_path, *args, env_extra=None, tmpdir=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("FACTLATTICE_") and k != "TMPDIR"}
    if tmpdir:
        env["TMPDIR"] = str(tmp_path / "tmpdir")
        (tmp_path / "tmpdir").mkdir(exist_ok=True)
    env.update(env_extra or {})
    r = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, env=env, timeout=60, cwd=str(tmp_path))
    return r


def planted(tmp_path, content=CONTENT, raw=None):
    f = tmp_path / "vocab.tsv"
    f.write_bytes(raw if raw is not None else content.encode("utf-8"))
    return f


def load_tool(monkeypatch, tmp_path):
    """The tool's module, in-process, with TMPDIR pointed at tmp_path/tmpdir (for the paths a subprocess cannot reach)."""
    sys.path.insert(0, str(TOOLS))
    try:
        import disclosure_guard as dg
        import vocab_edit
    finally:
        sys.path.remove(str(TOOLS))
    (tmp_path / "tmpdir").mkdir(exist_ok=True)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmpdir"))
    return vocab_edit, dg


def assert_blind(r, extra=()):
    """No planted row text, and no text the caller typed as a rule, in either stream."""
    for secret in (*SECRETS, *extra):
        assert secret not in r.stdout, f"stdout leaked {secret!r}"
        assert secret not in r.stderr, f"stderr leaked {secret!r}"


# --- each operation works ------------------------------------------------------------------------------------------------
def test_count_prints_rows_and_names_only(tmp_path):
    f = planted(tmp_path)
    r = run(tmp_path, "count", "--file", str(f))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rows: 2" in r.stdout and "firm-name: 1" in r.stdout and "site-name: 1" in r.stdout
    assert_blind(r)


def test_append_adds_one_row_and_loads_back(tmp_path):
    f = planted(tmp_path)
    r = run(
        tmp_path,
        "append",
        "--pattern",
        r"gale\w+port",
        "--control",
        "GaleHarbourPort",
        "--why",
        "why-text-gamma-7e3",
        "--name",
        "port-name",
        "--file",
        str(f),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rows: 2 -> 3" in r.stdout and "changed lines: 6" in r.stdout and "affected rules: port-name" in r.stdout
    assert "selftest: PASS" in r.stdout and "rules=3" in r.stdout
    assert f.read_text(encoding="utf-8") == CONTENT + T.join([r"gale\w+port", "GaleHarbourPort", "why-text-gamma-7e3", "port-name"]) + "\n"
    assert_blind(r, ["gale", "GaleHarbourPort", "why-text-gamma-7e3"])


def test_remove_drops_only_the_named_row(tmp_path):
    f = planted(tmp_path)
    r = run(tmp_path, "remove", "--name", "site-name", "--file", str(f))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rows: 2 -> 1" in r.stdout and "changed lines: 4" in r.stdout and "affected rules: site-name" in r.stdout
    assert f.read_text(encoding="utf-8") == CONTENT.replace(ROW_B + "\n", "")
    assert_blind(r)


def test_add_lookbehind_rewrites_a_matching_row(tmp_path):
    f = planted(tmp_path)
    r = run(tmp_path, "add-lookbehind", "--where-matches", "zephyrquill7", "--text", "ex-", "--file", str(f))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "changed lines: 2" in r.stdout and "affected rules: firm-name" in r.stdout
    new = f.read_text(encoding="utf-8")
    assert new == CONTENT.replace(ROW_A, T.join([r"(?<!ex\-)(?:zephyrquill\d+)", "zephyrquill42", "why-text-alpha-9c1", "firm-name"]))
    assert_blind(r)


def test_add_lookbehind_on_a_probe_matching_two_rows_changes_exactly_those_two(tmp_path):
    three = CONTENT + T.join([r"unrelatedword", "unrelatedword", "why-text-delta-1a8", "other-name"]) + "\n"
    f = planted(tmp_path, three)
    # the probe contains a string both planted rows match (case-insensitively)
    r = run(tmp_path, "add-lookbehind", "--where-matches", "zephyrquill7 and quartzmoor", "--text", "ex-", "--file", str(f))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "changed lines: 2, 4" in r.stdout and "affected rules: firm-name, site-name" in r.stdout
    old_lines, new_lines = three.splitlines(), f.read_text(encoding="utf-8").splitlines()
    changed = [i + 1 for i, (a, b) in enumerate(zip(old_lines, new_lines, strict=True)) if a != b]
    assert changed == [2, 4] and len(old_lines) == len(new_lines)
    assert_blind(r)


# --- other bytes are untouched -------------------------------------------------------------------------------------------
def test_append_keeps_every_other_byte_crlf_and_no_final_newline(tmp_path):
    raw = ("# c\r\n" + ROW_A + "\r\n\r\n" + ROW_B).encode("utf-8")  # CRLF, and no final line ending
    f = planted(tmp_path, raw=raw)
    r = run(tmp_path, "append", "--pattern", "emberfall", "--control", "Emberfall", "--why", "w", "--name", "e-name", "--file", str(f))
    assert r.returncode == 0, r.stdout + r.stderr
    new = f.read_bytes()
    assert new.startswith(raw + b"\r\n")  # the old bytes, then the line ending the file uses
    assert new.endswith(b"emberfall\tEmberfall\tw\te-name\r\n")
    assert_blind(r)


def test_lookbehind_and_remove_keep_every_other_byte(tmp_path):
    raw = ("# c\r\n" + ROW_A + "\r\n\r\n" + ROW_B + "\r\n# end").encode("utf-8")  # no final newline, a trailing comment
    f = planted(tmp_path, raw=raw)
    r = run(tmp_path, "add-lookbehind", "--where-matches", "zephyrquill1", "--text", "x", "--file", str(f))
    assert r.returncode == 0, r.stdout + r.stderr
    new = f.read_bytes()
    assert new.replace(b"(?<!x)(?:zephyrquill\\d+)", b"zephyrquill\\d+") == raw
    r2 = run(tmp_path, "remove", "--name", "site-name", "--file", str(f))
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert f.read_bytes() == new.replace((ROW_B + "\r\n").encode(), b"")
    assert_blind(r)
    assert_blind(r2)


@pytest.mark.parametrize("eol", ["\n", "\r\n", "\r"])
def test_removing_an_unterminated_last_row_leaves_the_file_unterminated(tmp_path, eol):
    raw = ("# c" + eol + ROW_A + eol + ROW_B).encode("utf-8")  # the last row has no line ending
    f = planted(tmp_path, raw=raw)
    r = run(tmp_path, "remove", "--name", "site-name", "--file", str(f))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f.read_bytes() == ("# c" + eol + ROW_A).encode("utf-8")
    assert_blind(r)


def test_removing_a_terminated_last_row_keeps_the_final_line_ending(tmp_path):
    raw = ("# c\r\n" + ROW_A + "\r\n" + ROW_B + "\r\n").encode("utf-8")
    f = planted(tmp_path, raw=raw)
    r = run(tmp_path, "remove", "--name", "site-name", "--file", str(f))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f.read_bytes() == ("# c\r\n" + ROW_A + "\r\n").encode("utf-8")


def test_the_backup_is_deleted_after_a_write_whose_check_passed(tmp_path):
    f = planted(tmp_path)
    r = run(tmp_path, "remove", "--name", "firm-name", "--file", str(f))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "backup: none kept" in r.stdout and "private" not in r.stdout.split("backup:")[1].split("(")[0]
    assert not list((tmp_path / "tmpdir").iterdir()), "no backup folder may be left in TMPDIR after a passing write"
    assert not list(tmp_path.glob(".vocab-edit-*")), "no temp file may be left beside the rules file"


def test_the_backup_is_taken_under_tmpdir_before_the_write_and_holds_the_original(tmp_path, monkeypatch):
    vocab_edit, _dg = load_tool(monkeypatch, tmp_path)
    f = planted(tmp_path)
    seen = {}
    real = vocab_edit._atomic_write

    def spy(path, data):
        folders = list((tmp_path / "tmpdir").iterdir())
        seen["folders"] = folders
        seen["bytes"] = [x.read_bytes() for d in folders for x in d.iterdir()]
        return real(path, data)

    monkeypatch.setattr(vocab_edit, "_atomic_write", spy)
    assert vocab_edit.main(["remove", "--name", "firm-name", "--file", str(f)]) == 0
    assert len(seen["folders"]) == 1 and seen["bytes"] == [CONTENT.encode("utf-8")]
    assert "DO-NOT-READ" in seen["folders"][0].name
    assert not list((tmp_path / "tmpdir").iterdir())


@pytest.mark.parametrize("how", ["unset", "empty", "missing", "file"])
def test_an_unusable_tmpdir_is_refused_with_nothing_written_and_no_fallback(tmp_path, how):
    f = planted(tmp_path)
    extra = {"TMPDIR": {"empty": "", "missing": str(tmp_path / "nope"), "file": str(f)}.get(how, "")}
    r = run(tmp_path, "remove", "--name", "firm-name", "--file", str(f), tmpdir=False, env_extra=None if how == "unset" else extra)
    assert r.returncode == 1 and "TMPDIR" in r.stderr and "REFUSED" in r.stderr
    assert f.read_bytes() == CONTENT.encode("utf-8")
    assert_blind(r)


def test_a_dry_run_needs_no_tmpdir(tmp_path):
    f = planted(tmp_path)
    r = run(tmp_path, "remove", "--name", "firm-name", "--dry-run", "--file", str(f), tmpdir=False)
    assert r.returncode == 0, r.stdout + r.stderr


# --- refusals leave the file unchanged, and stay blind ---------------------------------------------------------------------
REFUSALS = [
    (["append", "--pattern", "typedfoo", "--control", "typedbar", "--why", "typedwhy", "--name", "typedn1"], "control does not match"),
    (["append", "--pattern", "typedfoo", "--control", "typedfoo", "--why", "typedwhy", "--name", "firm-name"], "name already used"),
    (["append", "--pattern", "typedfoo(", "--control", "typedfoo", "--why", "typedwhy", "--name", "typedn1"], "does not compile"),
    (["append", "--pattern", "#typedfoo", "--control", "#typedfoo", "--why", "typedwhy", "--name", "typedn1"], "parser would not load"),
    (["append", "--pattern", "typedfoo", "--control", "typedfoo", "--why", "typedwhy", "--name", "typedn1 "], "parser would not load"),
    (
        ["append", "--pattern", "typedfoo", "--control", "typedfoo", "--why", "typedwhy", "--name", "zephyrquill9"],
        "a loaded rule matches the name",
    ),
    (
        ["append", "--pattern", "typedfoo", "--control", "typedfoo", "--why", "typedwhy", "--name", "typedfoo-name"],
        "the new rule matches its name",
    ),
    (["append", "--pattern", "typed\tfoo", "--control", "typed\tfoo", "--why", "typedwhy", "--name", "typedn1"], "tab in pattern"),
    (["append", "--pattern", "typedfoo", "--control", "typed\nfoo", "--why", "typedwhy", "--name", "typedn1"], "line break in control"),
    (
        ["append", "--pattern", "typedfoo", "--control", "typedfoo", "--why", "typed\u2028why", "--name", "typedn1"],
        "unicode line break in why",
    ),
    (["append", "--pattern", "typedfoo", "--control", "typedfoo", "--why", "typedwhy", "--name", "typed\tn1"], "tab in name"),
    (["add-lookbehind", "--where-matches", "typedprobe nothing", "--text", "typedtext"], "no row matches"),
    (["add-lookbehind", "--where-matches", "zephyrquill1", "--text", "typed\tx"], "tab in text"),
    (["add-lookbehind", "--where-matches", "zephyrquill1\r", "--text", "typedtext"], "line break in probe"),
    (["remove", "--name", "typedno-such-name"], "no such row"),
]
TYPED = ("typedfoo", "typedbar", "typedwhy", "typedn1", "typedprobe", "typedtext", "typedno-such-name")


@pytest.mark.parametrize("args, why", REFUSALS, ids=[w for _a, w in REFUSALS])
def test_a_bad_instruction_is_refused_with_the_file_unchanged(tmp_path, args, why):
    f = planted(tmp_path)
    r = run(tmp_path, *args, "--file", str(f))
    assert r.returncode == 1, (why, r.stdout, r.stderr)
    assert f.read_bytes() == CONTENT.encode("utf-8")
    assert "REFUSED" in r.stderr
    assert_blind(r, TYPED)  # the planted secrets AND everything the caller typed, in both streams


def test_a_pattern_that_does_not_compile_is_refused_without_quoting_it_or_the_regex_error(tmp_path):
    f = planted(tmp_path)
    r = run(
        tmp_path, "append", "--pattern", "typedbad(", "--control", "typedbad", "--why", "typedwhy", "--name", "typedn1", "--file", str(f)
    )
    assert r.returncode == 1 and "does not compile" in r.stderr
    for text in ("typedbad", "missing )", "unterminated", "typedwhy"):
        assert text not in r.stdout + r.stderr


def test_removing_the_last_row_is_refused(tmp_path):
    f = planted(tmp_path, "# c\n" + ROW_A + "\n")
    r = run(tmp_path, "remove", "--name", "firm-name", "--file", str(f))
    assert r.returncode == 1 and f.read_text(encoding="utf-8") == "# c\n" + ROW_A + "\n"


def test_a_missing_file_is_refused_not_created(tmp_path):
    r = run(tmp_path, "count", "--file", str(tmp_path / "absent.tsv"))
    assert r.returncode == 1 and not (tmp_path / "absent.tsv").exists()


@pytest.mark.parametrize(
    "args",
    [
        ["typedcmdsecret"],  # an invalid subcommand: stock argparse quotes it ("invalid choice: 'typedcmdsecret'")
        ["count", "typedextrasecret"],  # an extra positional: stock argparse quotes it ("unrecognized arguments: ...")
        ["append", "--pattern", "typedvalsecret", "--bogus", "typedboguslist"],
        ["append", "--pattern"],  # a missing value
    ],
    ids=["invalid-subcommand", "extra-positional", "unknown-option", "missing-value"],
)
def test_bad_usage_is_blind_too(tmp_path, args):
    r = run(tmp_path, *args)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "No value is echoed here" in r.stderr, "the tool's own parser must have answered, not stock argparse"
    for text in ("typedcmdsecret", "typedextrasecret", "typedvalsecret", "typedboguslist", "invalid choice", "unrecognized"):
        assert text not in r.stdout + r.stderr


def test_an_internal_error_prints_its_class_name_only(tmp_path, monkeypatch, capsys):
    """A crash whose message quotes a rule: the handler must print the class name and nothing of the message (M2)."""
    vocab_edit, _dg = load_tool(monkeypatch, tmp_path)

    def boom(*_a, **_k):
        raise ValueError("bad rule typedquoted-pattern and why-text-alpha-9c1")

    monkeypatch.setattr(vocab_edit, "_dispatch", boom)
    rc = vocab_edit.main(["count", "--file", str(planted(tmp_path))])
    out = capsys.readouterr()
    assert rc == 1 and "internal error (ValueError)" in out.err
    assert "typedquoted-pattern" not in out.out + out.err and "why-text-alpha-9c1" not in out.out + out.err


def test_a_crash_inside_the_check_restores_the_file_and_prints_the_class_name_only(tmp_path, monkeypatch, capsys):
    vocab_edit, dg = load_tool(monkeypatch, tmp_path)
    f = planted(tmp_path)

    real, calls = dg._control_failures, []

    def boom_after_the_write(*a, **k):
        calls.append(1)
        if len(calls) > 1:  # the first call is the check on the file as it was, before the edit
            raise ValueError("a rule said typedquoted-pattern")
        return real(*a, **k)

    monkeypatch.setattr(dg, "_control_failures", boom_after_the_write)
    rc = vocab_edit.main(["remove", "--name", "site-name", "--file", str(f)])
    out = capsys.readouterr()
    assert rc == 1 and "internal error (ValueError)" in out.err and "typedquoted-pattern" not in out.out + out.err
    assert len(calls) == 2, "the crash must really have happened after the write"
    assert f.read_bytes() == CONTENT.encode("utf-8")
    assert not list((tmp_path / "tmpdir").iterdir()), "the restore was verified, so the backup is gone"


def test_a_lookbehind_that_breaks_a_control_is_refused_naming_the_line(tmp_path):
    # the control sits right after the text the lookbehind forbids, so the rewritten row no longer matches it
    content = T.join([r"zephyr\d+", "ex-zephyr1", "why-text-eps-2b6", "firm-name"]) + "\n" + ROW_B + "\n"
    f = planted(tmp_path, content)
    r = run(tmp_path, "add-lookbehind", "--where-matches", "zephyr5", "--text", "ex-", "--file", str(f))
    assert r.returncode == 1 and "at lines: 1" in r.stderr
    assert f.read_text(encoding="utf-8") == content
    assert_blind(r, ["ex-zephyr1", "why-text-eps-2b6", "zephyr1"])


def test_a_lookbehind_whose_rewrite_does_not_compile_is_refused_without_the_regex_error(tmp_path, monkeypatch, capsys):
    """The re.error handler of the rewrite (M15): the line number is printed, the error's text never is."""
    vocab_edit, _dg = load_tool(monkeypatch, tmp_path)
    f = planted(tmp_path)

    def bad(rule, text):
        raise re.error("typedquoted-regex( at position 3")

    monkeypatch.setattr(vocab_edit, "_compile_rewrite", bad)
    rc = vocab_edit.main(["add-lookbehind", "--where-matches", "zephyrquill7", "--text", "ex-", "--file", str(f)])
    out = capsys.readouterr()
    assert rc == 1 and "at lines: 2" in out.err and "typedquoted-regex" not in out.out + out.err
    assert f.read_bytes() == CONTENT.encode("utf-8")


@pytest.mark.parametrize(
    "pattern, control, probe",
    [(r"\w*zibbet", "bigzibbet", "bigzibbet"), (r"(?:x)?zibbet", "xzibbet", "xzibbet"), (r"z?ibbet", "zibbet", "zibbet")],
)
def test_a_lookbehind_that_would_still_fire_on_the_probe_is_refused_whole_with_nothing_written(tmp_path, pattern, control, probe):
    """A pattern with an optional or variable-width start finds a later start: the rewrite would not stop the false positive."""
    content = CONTENT + T.join([pattern, control, "why-text-eta-3c9", "opt-rule"]) + "\n"
    f = planted(tmp_path, content)
    r = run(tmp_path, "add-lookbehind", "--where-matches", probe, "--text", "q-", "--file", str(f))
    assert r.returncode == 1 and "still matches the probe" in r.stderr and "at lines: 6" in r.stderr
    assert f.read_bytes() == content.encode("utf-8")
    assert_blind(r, ["why-text-eta-3c9", "zibbet"])


def test_a_lookbehind_that_stops_the_false_positive_reports_so_and_a_second_occurrence_is_fine(tmp_path):
    f = planted(tmp_path)
    # the probe holds the rule's match twice; the second one (not preceded by T) is another occurrence and does not matter
    r = run(tmp_path, "add-lookbehind", "--where-matches", "zephyrquill7 zephyrquill8", "--text", "ex-", "--file", str(f))
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("flags", ["(?s)", "(?x)", "(?im)", "(?a)", "(?i)(?s)"])
def test_a_row_starting_with_a_global_inline_flag_other_than_a_leading_i_is_refused_with_a_clear_message(tmp_path, flags):
    content = CONTENT + T.join([flags + r"zibbet\d+", "zibbet1", "why-text-theta-6d4", "flag-rule"]) + "\n"
    f = planted(tmp_path, content)
    r = run(tmp_path, "add-lookbehind", "--where-matches", "zibbet5", "--text", "ex-", "--file", str(f))
    assert r.returncode == 1 and "global inline flag" in r.stderr and "at lines: 6" in r.stderr
    assert f.read_bytes() == content.encode("utf-8")
    assert_blind(r, ["why-text-theta-6d4", "zibbet"])


def test_a_leading_i_is_dropped_as_redundant_and_the_control_still_matches(tmp_path):
    # the guard compiles every rule with re.IGNORECASE (disclosure_guard.Rule), so `(?i)` adds nothing and cannot sit inside the wrap
    content = CONTENT + T.join([r"(?i)zibbet\d+", "ZIBBET1", "why-text-theta-6d4", "flag-rule"]) + "\n"
    f = planted(tmp_path, content)
    r = run(tmp_path, "add-lookbehind", "--where-matches", "zibbet5", "--text", "ex-", "--file", str(f))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "changed lines: 6" in r.stdout and "selftest: PASS" in r.stdout
    got = f.read_text(encoding="utf-8").splitlines()
    assert got[5] == T.join([r"(?<!ex\-)(?:zibbet\d+)", "ZIBBET1", "why-text-theta-6d4", "flag-rule"])
    assert got[:5] == content.splitlines()[:5]
    assert_blind(r, ["why-text-theta-6d4", "zibbet", "ZIBBET1"])


def test_a_leading_i_is_kept_and_the_row_refused_when_the_guard_would_compile_the_rest_case_sensitively(tmp_path, monkeypatch, capsys):
    """Pins the dependency on how the guard compiles a rules-file row (Rule's default flags): the (?i) is dropped only when the rest
    compiled the guard's way is case-insensitive. A guard that compiled rows case-sensitively makes the drop unsafe: refuse, write nothing."""
    # a lowercase control: it still matches after a wrong drop of the (?i), so only this condition (not the post-rewrite check) can refuse
    vocab_edit, dg = load_tool(monkeypatch, tmp_path)
    content = CONTENT + T.join([r"(?i)zibbet\d+", "zibbet1", "why-text-theta-6d4", "flag-rule"]) + "\n"
    f = planted(tmp_path, content)

    class CaseSensitiveRule(dg.Rule):
        __slots__ = ()

        def __init__(self, name, pattern, control, why, source, flags=0, generated=False, ident=None):
            super().__init__(name, pattern, control, why, source, flags, generated, ident)

    monkeypatch.setattr(dg, "Rule", CaseSensitiveRule)
    rc = vocab_edit.main(["add-lookbehind", "--where-matches", "zibbet5", "--text", "ex-", "--file", str(f)])
    out = capsys.readouterr()
    assert rc == 1 and "global inline flag" in out.err and "at lines: 6" in out.err
    assert f.read_bytes() == content.encode("utf-8")


def test_a_leading_i_row_still_goes_through_the_post_rewrite_check(tmp_path):
    # `(?i)\w*x` rewritten to `(?<!q-)(?:\w*x)` can still match from a later start: refused, nothing written
    content = CONTENT + T.join([r"(?i)\w*x", "zibbetx", "why-text-theta-6d4", "flag-rule"]) + "\n"
    f = planted(tmp_path, content)
    r = run(tmp_path, "add-lookbehind", "--where-matches", "zibbetx", "--text", "q-", "--file", str(f))
    assert r.returncode == 1 and "still matches the probe" in r.stderr
    assert f.read_bytes() == content.encode("utf-8")


def test_the_leading_i_rewrite_matches_exactly_what_the_old_pattern_matched_when_t_is_absent(tmp_path, monkeypatch):
    import random

    vocab_edit, dg = load_tool(monkeypatch, tmp_path)
    pats = [r"(?i)zibbet\d+", r"(?i)(?:foo|bar)baz", r"(?i)\bk[a-z]+n\b", r"(?i)s\w{1,3}t", r"(?i)a.?b"]
    controls = ["zibbet1", "foobaz", "kzzn", "sat", "ab"]
    content = "".join(T.join([pt, c, "why", f"r{i}"]) + "\n" for i, (pt, c) in enumerate(zip(pats, controls)))
    f = planted(tmp_path, content)
    old_rules = dg._parse_rows(f, content)[0]
    plan = vocab_edit.plan_lookbehind(content, f, "zibbet5 foobaz kzzn sat ab", "ex-")
    assert plan.changed == [1, 2, 3, 4, 5]
    new_rules = dg._parse_rows(f, plan.new_text)[0]
    assert [r.rx.pattern for r in new_rules] == [f"(?<!ex\\-)(?:{pt[4:]})" for pt in pats]
    rng = random.Random(20261125)
    pieces = ["zibbet", "ZIBBET", "Zibbet", "foo", "BAR", "baz", "k", "K", "n", "N", "s", "S", "\u017f", "t", "T", "a", "b", "A", "B",
              "1", "99", " ", "-", "e", "x", "_", "\u0130", "\u212a", "\n", "."]
    n = fired = 0
    while n < 6000:
        sample = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 14)))
        if "ex-" in sample.lower():
            continue
        n += 1
        for o, nw in zip(old_rules, new_rules):
            fired += bool(o.rx.search(sample))
            assert [m.span() for m in o.rx.finditer(sample)] == [m.span() for m in nw.rx.finditer(sample)], (o.name, sample)
    assert fired > 200, fired  # the fuzz is only a proof if the rules fire on a fair share of it


def test_the_fuzz_alphabet_does_fire_the_rules(tmp_path, monkeypatch):
    # negative control for the equivalence test: the same kind of strings DO match, so equal results are not both-empty
    vocab_edit, dg = load_tool(monkeypatch, tmp_path)
    rule = dg.Rule("r", r"(?i)zibbet\d+", "zibbet1", "why", "x:0")
    assert rule.rx.search("a ZIBBET99 b") and not rule.rx.search("zibbe 99")


def test_a_leading_i_row_rewrite_dry_run_prints_counts_and_names_only(tmp_path):
    content = CONTENT + T.join([r"(?i)zibbet\d+", "ZIBBET1", "why-text-theta-6d4", "flag-rule"]) + "\n"
    f = planted(tmp_path, content)
    r = run(tmp_path, "add-lookbehind", "--where-matches", "zibbet5", "--text", "ex-", "--file", str(f), "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "changed lines: 6" in r.stdout and "affected rules: flag-rule" in r.stdout
    assert f.read_bytes() == content.encode("utf-8")
    assert_blind(r, ["why-text-theta-6d4", "zibbet", "ZIBBET1", "ex-", "(?i)", "(?<!"])


# --- names are printed, so the tool withholds the ones that could carry a term ------------------------------------------------
def test_count_withholds_a_name_that_a_rule_matches_or_that_sits_in_a_five_column_row(tmp_path):
    rows = (
        T.join([r"quartzmoor", "A Quartzmoor Order", "why-text-beta-4d2", "named-for-quartzmoor"])
        + "\n"  # a rule matches this name
        + T.join(["plainword", "plainword", "why part one", "why part two zephyrquill", "fifth"])
        + "\n"  # a reason with a tab
        + T.join(["otherword", "otherword", "why-text-iota-1b2", "fine-name"])
        + "\n"
    )
    f = planted(tmp_path, rows)
    r = run(tmp_path, "count", "--file", str(f))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "fine-name: 1" in r.stdout and "at lines: 1, 2" in r.stdout
    assert_blind(r, ["named-for-quartzmoor", "why part two", "why part one", "zephyrquill"])


TABBED_ROW = T.join(["tabword", "tabword", "whyalpha part one", "whyalpha part two"])  # a 3-column row whose reason holds a tab


def test_a_name_that_is_not_slug_shaped_is_withheld_in_count_even_in_a_four_column_row(tmp_path):
    f = planted(tmp_path, TABBED_ROW + "\n" + ROW_B + "\n")
    r = run(tmp_path, "count", "--file", str(f))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "site-name: 1" in r.stdout and "at lines: 1" in r.stdout
    assert_blind(r, ["whyalpha", "part two", "part one", "tabword"])


def test_a_four_column_row_with_a_tab_in_its_reason_is_withheld_in_every_summary(tmp_path):
    f = planted(tmp_path, TABBED_ROW + "\n" + ROW_B + "\n")
    r = run(tmp_path, "add-lookbehind", "--where-matches", "tabword", "--text", "ex-", "--file", str(f), "--dry-run")
    assert r.returncode == 0 and "(name withheld, line 1)" in r.stdout, r.stdout + r.stderr
    assert_blind(r, ["whyalpha", "part two"])
    r = run(tmp_path, "remove", "--name", "whyalpha part two", "--file", str(f), "--dry-run")
    assert r.returncode == 0 and "(name withheld, line 1)" in r.stdout, r.stdout + r.stderr
    assert_blind(r, ["whyalpha", "part two"])


def test_a_four_column_row_with_a_tab_in_its_reason_is_withheld_in_the_selftest_lines(tmp_path, monkeypatch, capsys):
    vocab_edit = widened_controls(monkeypatch, tmp_path)
    f2 = planted(tmp_path, TABBED_ROW.replace("tabword", "emberfall") + "\n" + ROW_B + "\n")
    rc = vocab_edit.main(
        ["append", "--pattern", "zzqx", "--control", "zzqx", "--why", "w", "--name", "q-name", "--file", str(f2), "--dry-run"]
    )
    out = capsys.readouterr()
    assert "fire on a legitimate-line control: (name withheld, line 1)" in out.out and "whyalpha" not in out.out


@pytest.mark.parametrize(
    ("name", "printed"),
    [
        ("fine-name", True),
        ("Fine-Name-2", True),
        ("a", True),
        ("has space", False),
        ("under_score", False),
        ("dot.ted", False),
        ("-lead", False),
        ("trail-", False),
        ("a--b", False),
        ("na\u00efve", False),
        ("\u212aelvin", False),  # the Kelvin sign case-folds to k: ASCII-only matching must not let it through
    ],
)
def test_only_a_slug_shaped_name_is_ever_printed(tmp_path, name, printed):
    f = planted(tmp_path, T.join(["plainone", "plainone", "why-text-kappa-3a1", name]) + "\n" + ROW_B + "\n")
    r = run(tmp_path, "count", "--file", str(f))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (f"  {name}: 1" in r.stdout) is printed, r.stdout
    assert printed or (name not in r.stdout and "name is withheld" in r.stdout and "at lines: 1" in r.stdout), r.stdout


def test_remove_and_lookbehind_withhold_an_unprintable_name_in_their_summary(tmp_path):
    rows = T.join([r"quartzmoor", "A Quartzmoor Order", "why-text-beta-4d2", "named-for-quartzmoor"]) + "\n" + ROW_A + "\n"
    f = planted(tmp_path, rows)
    r = run(tmp_path, "add-lookbehind", "--where-matches", "quartzmoor", "--text", "ex-", "--file", str(f), "--dry-run")
    assert r.returncode == 0 and "(name withheld, line 1)" in r.stdout
    assert_blind(r, ["named-for-quartzmoor"])
    r = run(tmp_path, "remove", "--name", "named-for-quartzmoor", "--file", str(f), "--dry-run")
    assert r.returncode == 0 and "(name withheld, line 1)" in r.stdout
    assert_blind(r, ["named-for-quartzmoor"])


# --- the guard's selftest gates every write, and a failure rolls the original bytes back ------------------------------------
def widened_controls(monkeypatch, tmp_path):
    """Load the tool in-process with one guard control widened: a rule that fires on a legitimate-line control makes the guard's
    own controls fail AFTER the new row is on disk, which must roll back."""
    vocab_edit, dg = load_tool(monkeypatch, tmp_path)
    monkeypatch.setattr(dg, "CONTROL_CLEAN", [*dg.CONTROL_CLEAN, "a legitimate line about emberfall"])
    return vocab_edit


APPEND_E = ["append", "--pattern", "emberfall", "--control", "emberfall", "--why", "reason-typed", "--name", "e-name"]


def test_a_selftest_failure_after_the_write_restores_the_original_bytes(tmp_path, monkeypatch, capsys):
    vocab_edit = widened_controls(monkeypatch, tmp_path)
    f = planted(tmp_path)
    rc = vocab_edit.main([*APPEND_E, "--file", str(f)])
    out = capsys.readouterr()
    assert rc == 1 and "ROLLED BACK" in out.out and "compared byte for byte" in out.out
    assert f.read_bytes() == CONTENT.encode("utf-8")
    assert not list(tmp_path.glob(".vocab-edit-*"))
    assert not list((tmp_path / "tmpdir").iterdir()), "the restore was verified, so the backup copy is deleted"


def test_the_rollback_report_names_the_rule_and_prints_no_rule_text(tmp_path, monkeypatch, capsys):
    vocab_edit = widened_controls(monkeypatch, tmp_path)
    f = planted(tmp_path)
    vocab_edit.main([*APPEND_E, "--file", str(f)])
    out = capsys.readouterr()
    assert "ROLLED BACK" in out.out and "selftest: FAIL" in out.out and "e-name" in out.out
    for text in (*SECRETS, "reason-typed", "a legitimate line about emberfall"):
        assert text not in out.out + out.err


def _fail_restore(vocab_edit, monkeypatch, mode):
    """Let the first write (the edit) through; make the second (the restore) raise, or write other bytes without raising."""
    real, calls = vocab_edit._atomic_write, []

    def flaky(path, data):
        calls.append(data)
        if len(calls) == 1:
            return real(path, data)
        if mode == "raise":
            raise OSError(28, "No space left on device")
        return real(path, data + b"# corrupted by the restore\n")

    monkeypatch.setattr(vocab_edit, "_atomic_write", flaky)


@pytest.mark.parametrize("mode", ["raise", "wrong-bytes"])
def test_a_restore_that_fails_or_does_not_reproduce_the_original_is_reported_plainly(tmp_path, monkeypatch, capsys, mode):
    """M11: the restore's bytes are compared with the original's; 'no exception' is not 'restored'."""
    vocab_edit = widened_controls(monkeypatch, tmp_path)
    _fail_restore(vocab_edit, monkeypatch, mode)
    f = planted(tmp_path)
    rc = vocab_edit.main([*APPEND_E, "--file", str(f)])
    out = capsys.readouterr()
    assert rc == vocab_edit.EXIT_NOT_RESTORED == 3 and rc not in (0, 1, 2)
    assert "ORIGINAL NOT RESTORED" in out.err and "may hold the edit" in out.err
    folders = list((tmp_path / "tmpdir").iterdir())
    assert len(folders) == 1, "the backup is kept when the original was not restored"
    backup = folders[0] / "private-rules-file-original"
    assert str(backup) in out.err and backup.read_bytes() == CONTENT.encode("utf-8")
    assert f.read_bytes() != CONTENT.encode("utf-8")  # the file really was left edited
    for text in (*SECRETS, "reason-typed", "emberfall"):
        assert text not in out.out + out.err


def test_a_crash_after_the_write_with_a_failed_restore_is_also_not_restored(tmp_path, monkeypatch, capsys):
    vocab_edit, dg = load_tool(monkeypatch, tmp_path)
    _fail_restore(vocab_edit, monkeypatch, "raise")
    real, calls = dg._control_failures, []

    def crash_on_the_post_write_check(*a, **k):
        calls.append(1)
        if len(calls) > 1:  # the first call is the check on the file as it was, before the edit
            raise ValueError("quotes typedquoted-pattern")
        return real(*a, **k)

    monkeypatch.setattr(dg, "_control_failures", crash_on_the_post_write_check)
    f = planted(tmp_path)
    rc = vocab_edit.main([*APPEND_E, "--file", str(f)])
    out = capsys.readouterr()
    assert rc == 3 and "ORIGINAL NOT RESTORED" in out.err and "typedquoted-pattern" not in out.out + out.err


def test_a_file_already_failing_its_controls_is_reported_and_not_made_worse(tmp_path):
    bad = CONTENT + T.join([r"nevermatches", "something else", "why-text-zeta-5f0", "mute-rule"]) + "\n"
    f = planted(tmp_path, bad)
    r = run(tmp_path, "append", "--pattern", "emberfall", "--control", "Emberfall", "--why", "w", "--name", "e-name", "--file", str(f))
    assert r.returncode == 1 and "ROLLED BACK" in r.stdout and "selftest before the edit: FAIL" in r.stdout
    assert "refuses to load, at lines: 6" in r.stdout
    assert f.read_bytes() == bad.encode("utf-8")
    assert_blind(r, ["nevermatches", "something else", "why-text-zeta-5f0"])


# --- --dry-run -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "args",
    [
        ["append", "--pattern", "emberfall", "--control", "Emberfall", "--why", "w", "--name", "e-name"],
        ["add-lookbehind", "--where-matches", "zephyrquill1", "--text", "x"],
        ["remove", "--name", "site-name"],
    ],
)
def test_dry_run_reports_and_writes_nothing(tmp_path, args):
    f = planted(tmp_path)
    before = f.stat().st_mtime_ns
    r = run(tmp_path, *args, "--dry-run", "--file", str(f))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "dry run" in r.stdout and "changed lines:" in r.stdout and "selftest: PASS" in r.stdout and "backup:" not in r.stdout
    assert f.read_bytes() == CONTENT.encode("utf-8") and f.stat().st_mtime_ns == before
    assert not list((tmp_path / "tmpdir").glob("*")), "a dry run makes no backup"
    assert_blind(r)


# --- the default file ----------------------------------------------------------------------------------------------------
def test_the_default_file_is_private_publish_vocabulary_under_the_repo_root():
    sys.path.insert(0, str(TOOLS))
    try:
        import vocab_edit
    finally:
        sys.path.remove(str(TOOLS))
    assert vocab_edit.DEFAULT_FILE == TOOLS.parent / "private" / "publish-vocabulary.tsv"


def test_the_default_file_honours_the_guards_env_variable(tmp_path):
    f = planted(tmp_path)
    r = run(tmp_path, "count", env_extra={"FACTLATTICE_PUBLISH_VOCAB": str(f)})
    assert r.returncode == 0 and "file=vocab.tsv" in r.stdout and "rows: 2" in r.stdout, r.stdout + r.stderr
    r = run(tmp_path, "remove", "--name", "site-name", "--dry-run", env_extra={"FACTLATTICE_PUBLISH_VOCAB": str(f)})
    assert r.returncode == 0 and "affected rules: site-name" in r.stdout, r.stdout + r.stderr


def test_a_row_the_guard_refuses_to_load_never_has_its_name_printed(tmp_path):
    # a slug-shaped name in a row whose regex does not compile (the guard refuses it): withheld, though nothing else flags it
    bad = T.join(["cobalt(vane", "cobaltvane", "why-text-lambda-8e5", "cobaltvane-client"])
    f = planted(tmp_path, bad + "\n" + ROW_B + "\n")
    r = run(tmp_path, "count", "--file", str(f))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cobaltvane-client" not in r.stdout and "site-name: 1" in r.stdout and "at lines: 1" in r.stdout, r.stdout
    assert_blind(r, ["cobaltvane", "why-text-lambda-8e5"])
