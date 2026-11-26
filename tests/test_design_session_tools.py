"""The design-session skills and tools, shipped in the repository as their single source of truth (SDLC.md 4.4).

What is pinned here:

* the two tools' own self-tests, with their case counts: the extractor's 16 checks and the verifier's 9 assertions
  (the counts the original tools printed; a changed count is a changed control and must be changed here on purpose);
* the sentinel phrase is DEFINED ONCE, in `tools/design-session/extract_design_inputs.py`, and every other file only
  shows it: a drift check reads the constants from the extractor and fails if a skill's line differs, with a planted
  drifted copy proving the check can fail;
* the tools run from a clone with inputs given by argument (an end-to-end run on a transcript written to a temp
  directory, with a fabricated quote that the verifier must catch);
* the shipped files carry no home path, no private tree and no person's name (a scan with a planted positive control);
* the overlay path is gitignored and excluded from publication, and SDLC.md names the design session and the
  install rule.
"""
import importlib.util
import json
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools" / "design-session"
EXTRACTOR = TOOLS / "extract_design_inputs.py"
VERIFIER = TOOLS / "verify_dsd_citations.py"
EXAMPLE = TOOLS / "design-session.local.example.md"
SKILLS = {
    "begin": ROOT / "skills" / "begin-design-session" / "SKILL.md",
    "end": ROOT / "skills" / "end-design-session" / "SKILL.md",
}
STEPS = ROOT / "docs" / "SPEC-AUDIT-STEPS.md"

# PUBLISH-EXCLUDE is trunk-only (it removes itself from a publication), so a snapshot has no such file. A test that
# NEEDS it skips there with this reason, and only the part of a test that needs it does: the other assertions of the same
# test run in a snapshot too (spec 42 R10; SDLC 7.6 PUBLIC-TEST-SET-SKIPS-NAME-THEIR-PATH). On the trunk the file exists
# and everything runs.
needs_publish_exclude = pytest.mark.skipif(
    not (ROOT / "PUBLISH-EXCLUDE").exists(),
    reason="PUBLISH-EXCLUDE is not in a published snapshot (it is trunk-only)")
OVERLAY = "private/design-session.local.md"

SHIPPED = [EXTRACTOR, VERIFIER, EXAMPLE, STEPS, *SKILLS.values()]

# the case counts the two tools print. Do not change these to make a run pass.
EXTRACTOR_CHECKS = 16
VERIFIER_ASSERTIONS = 9


def _load_extractor():
    spec = importlib.util.spec_from_file_location("extract_design_inputs_under_test", EXTRACTOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


def _run(*args, cwd=None):
    return subprocess.run([sys.executable, "-I", *map(str, args)], capture_output=True, text=True, cwd=cwd)


# --- the tools' own self-tests, with their counts -------------------------------------------------------------------

def test_extractor_self_test_runs_all_its_checks():
    r = _run(EXTRACTOR, "--self-test")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"self-test: {EXTRACTOR_CHECKS} checks, 0 failures" in r.stdout, r.stdout


def test_verifier_self_test_runs_all_its_assertions():
    r = _run(VERIFIER, "--self-test")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"self-test: {VERIFIER_ASSERTIONS} assertions, 0 failures" in r.stdout, r.stdout


# --- the sentinel is defined once -------------------------------------------------------------------------------------

def sentinel_lines(text):
    """Every marker-shaped line in a text: the glyph-bracketed names, however they are spelled."""
    return re.findall(r"[⌜⌞][A-Z-]+[⌝⌟]", text)


def drift(text, constants):
    """The marker-shaped tokens in `text` that are not one of the extractor's two constants."""
    return [t for t in sentinel_lines(text) if t not in constants]


def test_the_extractor_defines_both_sentinels_and_prints_them():
    ext = _load_extractor()
    r = _run(EXTRACTOR, "--print-sentinels")
    assert r.returncode == 0
    assert r.stdout.split() == [ext.SENTINEL_BEGIN, ext.SENTINEL_END]


def test_each_skill_shows_exactly_the_extractors_sentinel_in_a_fenced_line():
    ext = _load_extractor()
    constants = {ext.SENTINEL_BEGIN, ext.SENTINEL_END}
    want = {"begin": ext.SENTINEL_BEGIN, "end": ext.SENTINEL_END}
    for which, path in SKILLS.items():
        text = path.read_text(encoding="utf-8")
        assert drift(text, constants) == [], f"{path.name}: a sentinel that is not the extractor's"
        fenced = re.search(r"```\n(" + re.escape(want[which]) + r" feature: <feature-name>)\n```", text)
        assert fenced, f"{path}: no fenced line that carries the extractor's {which} sentinel"


ASSIGNMENT = re.compile(r"\bSENTINEL_(?:BEGIN|END)\b\s*[:=]")


def tracked_text_files(root):
    """Every file git tracks under root (all of them, whatever the extension), falling back to a walk without .git."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
        names = [n for n in out.decode("utf-8", "replace").split("\0") if n]
        files = [pathlib.Path(root) / n for n in names]
    except (OSError, subprocess.CalledProcessError):
        files = [f for f in pathlib.Path(root).rglob("*") if f.is_file() and ".git" not in f.parts]
    return [f for f in files if f.is_file()]


def sentinel_assignments(files, allowed):
    """Files, other than the allowed ones, that assign either sentinel constant, whatever their type."""
    hits = []
    for f in files:
        if f.resolve() in allowed:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary
        if ASSIGNMENT.search(text):
            hits.append(f)
    return hits


def test_the_sentinel_is_assigned_in_one_file_only():
    """No tracked file of any type (.py, .md, a skill, a doc) other than the extractor, and this file that holds the
    pattern, assigns a sentinel constant; the skills and docs only show the phrase."""
    allowed = {EXTRACTOR.resolve(), pathlib.Path(__file__).resolve()}
    files = tracked_text_files(ROOT)
    assert EXTRACTOR in files and SKILLS["begin"] in files  # the scan sees the places a copy could hide
    assert [str(f.relative_to(ROOT)) for f in sentinel_assignments(files, allowed)] == []


def test_the_one_definition_scan_goes_red_on_a_planted_copy_in_a_md_or_a_skill_helper(tmp_path):
    """Negative control for the blind spot the first version had: a second definition in a .md file, or in a helper
    under skills/, stayed green because only .py files under four directories were read."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "skills" / "x").mkdir(parents=True)
    md = tmp_path / "docs" / "note.md"
    md.write_text('SENTINEL_BEGIN = "<copy>"\n', encoding="utf-8")
    helper = tmp_path / "skills" / "x" / "helper.py"
    helper.write_text("SENTINEL_END: str = 'x'\n", encoding="utf-8")
    clean = tmp_path / "docs" / "clean.md"
    clean.write_text("the phrase is shown here, never assigned\n", encoding="utf-8")
    hits = sentinel_assignments([md, helper, clean], allowed=set())
    assert sorted(h.name for h in hits) == ["helper.py", "note.md"]


def test_the_drift_check_can_fail():
    """Negative control: a skill whose line differs by one character is caught by the same check."""
    ext = _load_extractor()
    constants = {ext.SENTINEL_BEGIN, ext.SENTINEL_END}
    good = SKILLS["begin"].read_text(encoding="utf-8")
    assert drift(good, constants) == []
    drifted = good.replace(ext.SENTINEL_BEGIN, ext.SENTINEL_BEGIN.replace("BEGIN", "START"))
    assert drifted != good
    assert drift(drifted, constants) != []


# --- the tools run from a clone, inputs by argument ------------------------------------------------------------------

def _transcript(path):
    ext = _load_extractor()
    quote = ("the retention window is ninety days and every site keeps its own copy "
             "of the roster without exception")
    rows = [
        {"type": "user", "message": {"role": "user", "content": "Before the session."}},
        {"type": "user", "message": {"role": "user", "content": "Let's design the roster view."}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": f"{ext.SENTINEL_BEGIN} feature: roster-view\n\nOpened."}]}},
        {"type": "user", "message": {"role": "user", "content": quote}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": f"{ext.SENTINEL_END} feature: roster-view\n\nClosed."}]}},
        {"type": "user", "message": {"role": "user", "content": "After the session."}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return quote


UNKNOWN = "origin unknown: this transcript predates origin marking; a turn's speaker is not verified"
PARTIAL = "origin unknown: records before the first origin-marked record; their speaker is not verified"

PEER_TEXT = "The retention quota is seven days and the peer agent insists every site must keep a full copy"
NOTIF_TEXT = "Background job finished and the notification says the retention window is thirty days everywhere"
HUMAN_TEXT = "the roster view shows ten players per page and the storage service holds an unbounded population"


def _u(text, origin=None, **extra):
    rec = {"type": "user", "message": {"role": "user", "content": text}}
    if origin is not None:
        rec["origin"] = {"kind": origin}
    rec.update(extra)
    return rec


def _marker_row(kind, feature):
    """An assistant record that ASSERTS a design-session sentinel for `feature` (marker alone at the head of a line,
    with `feature: <name>`): the form the extractor's state machine reads. kind is 'begin' or 'end'."""
    ext = _load_extractor()
    marker = ext.SENTINEL_BEGIN if kind == "begin" else ext.SENTINEL_END
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": f"{marker} feature: {feature}\n\n{kind}."}]}}


def _bracketed(feature, rows):
    """`rows` wrapped in one feature's bracket, the BEGIN first and the END last, so every row is inside it (a
    `--feature` run refuses a transcript with no bracket for that feature). The BEGIN is transcript line 1, so each
    row's line number is its old one plus 1."""
    return [_marker_row("begin", feature), *rows, _marker_row("end", feature)]


def _origin_transcript(path):
    """A synthetic transcript in the origin-marked format. No real content."""
    ext = _load_extractor()
    rows = [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": f"{ext.SENTINEL_BEGIN} feature: roster-view\n\nOpened."}]}},
        _u(HUMAN_TEXT, "human"),
        _u(PEER_TEXT, "peer", isMeta=True),
        _u(NOTIF_TEXT, "task-notification"),
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": f"{ext.SENTINEL_END} feature: roster-view\n\nClosed."}]}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _extract(path, tmp_path):
    r = _run(EXTRACTOR, path, "--feature", "roster-view", "--json", cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


# --- the stakeholder is the human-origin turn and nothing else --------------------------------------------------------

def test_a_peer_turn_is_not_counted_as_the_stakeholders(tmp_path):
    t = tmp_path / "s.jsonl"
    _origin_transcript(t)
    out = _extract(t, tmp_path)
    texts = [s["text"] for s in out["statements"]]
    assert texts == [HUMAN_TEXT]
    assert out["origin"]["mode"] == "marked" and out["origin"]["warning"] is None
    assert out["origin"]["excluded_by_kind"]["peer"] == 1
    assert out["reconciliation"]["balances"] is True


def test_a_task_notification_turn_is_not_counted_as_the_stakeholders(tmp_path):
    t = tmp_path / "s.jsonl"
    _origin_transcript(t)
    out = _extract(t, tmp_path)
    assert NOTIF_TEXT not in [s["text"] for s in out["statements"]]
    kinds = out["origin"]["excluded_by_kind"]
    assert kinds["task-notification"] == 1
    assert kinds["no-origin:tool-result"] == 1
    assert out["origin"]["excluded_total"] == sum(kinds.values()) == 3


def test_a_transcript_with_no_origin_field_falls_back_and_says_so_loudly(tmp_path):
    t = tmp_path / "s.jsonl"
    quote = _transcript(t)
    out = _extract(t, tmp_path)
    assert quote in [s["text"] for s in out["statements"]]
    assert out["origin"]["mode"] == "unmarked"
    assert out["origin"]["warning"] == UNKNOWN
    assert out["origin"]["excluded_by_kind"] == {}
    r = _run(EXTRACTOR, t, "--feature", "roster-view", cwd=tmp_path)
    assert UNKNOWN in r.stdout, r.stdout


def test_the_text_output_reports_the_excluded_turns_by_kind(tmp_path):
    t = tmp_path / "s.jsonl"
    _origin_transcript(t)
    r = _run(EXTRACTOR, t, "--feature", "roster-view", cwd=tmp_path)
    assert "excluded 3 user turns" in r.stdout and "peer 1" in r.stdout and "task-notification 1" in r.stdout, r.stdout
    assert UNKNOWN not in r.stdout


def test_a_queued_record_counts_only_when_a_human_origin_turn_carries_the_same_words(tmp_path):
    ext = _load_extractor()
    t = tmp_path / "s.jsonl"
    queued = {"type": "queue-operation", "operation": "enqueue", "content": PEER_TEXT}
    mine = {"type": "queue-operation", "operation": "enqueue", "content": HUMAN_TEXT}
    rows = [queued, mine, _u("an unrelated ordinary human turn about the layout of the page", "human"),
            _u(HUMAN_TEXT, "human")]
    t.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    stmts, _, rc = ext.collect(t)
    texts = [s.text for s in stmts]
    assert PEER_TEXT not in texts
    assert texts.count(HUMAN_TEXT) == 1
    assert rc.excluded_by_kind == {"queued-no-origin": 1}
    assert rc.balances(len(stmts))


def _verify_one(tmp_path, transcript, quote, line):
    spec = tmp_path / "spec.lts"
    spec.write_text("## The roster view [SPEC:WIDGET-ROSTER]\n\n- kind: spec\n", encoding="utf-8")
    d = tmp_path / "d.md"
    d.write_text(f"- **Stakeholder said** (line {line}): *\"{quote}\"*\n\nCites [WIDGET-ROSTER].\n", encoding="utf-8")
    return _run(VERIFIER, d, "--transcript", transcript, "--spec", spec, cwd=tmp_path)


def test_the_verifier_does_not_verify_a_peer_or_notification_quote_as_the_stakeholders(tmp_path):
    t = tmp_path / "s.jsonl"
    _origin_transcript(t)
    lines = t.read_text(encoding="utf-8").splitlines()
    for text, kind in ((PEER_TEXT, "peer"), (NOTIF_TEXT, "task-notification")):
        n = next(i for i, raw in enumerate(lines, 1) if text in raw)
        r = _verify_one(tmp_path, t, text, n)
        assert r.returncode == 1, r.stdout
        assert "NOT-STAKEHOLDER" in r.stdout and kind in r.stdout, r.stdout
        assert "citations resolve" not in r.stdout
    n = next(i for i, raw in enumerate(lines, 1) if HUMAN_TEXT in raw)
    ok = _verify_one(tmp_path, t, HUMAN_TEXT, n)
    assert ok.returncode == 0, ok.stdout


def test_extractor_brackets_a_session_given_only_a_transcript_argument(tmp_path):
    t = tmp_path / "session.jsonl"
    quote = _transcript(t)
    r = _run(EXTRACTOR, t, "--feature", "roster-view", "--json", cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert [b["kind"] for b in out["boundaries"]] == ["begin", "end"]
    texts = [s["text"] for s in out["statements"]]
    assert quote in texts
    assert "Before the session." not in texts and "After the session." not in texts
    assert [s["role"] for s in out["statements"]][0] == "trigger"
    assert out["reconciliation"]["balances"] is True


# --- `--feature` brackets per feature and refuses an unbracketed one (the extractor's contract since 2026-11-24) ----------

def _feature_rows(*items):
    """('u', text) is a human-origin turn, ('b'|'e', feature) an assistant BEGIN or END marker for that feature."""
    out = []
    for kind, val in items:
        out.append(_u(val, "human") if kind == "u" else _marker_row("begin" if kind == "b" else "end", val))
    return out


def _feature_run(tmp_path, rows, *args, name="s.jsonl"):
    t = tmp_path / name
    _write_rows(t, rows)
    return _run(EXTRACTOR, t, *args, "--json", cwd=tmp_path)


def _texts(r):
    return [s["text"] for s in json.loads(r.stdout)["statements"]]


# two sessions, one after the other, as in the original report: fleet-tools, then velo5x0-linux-board-support
FLEET = ("the fleet tools should report each host once and never twice in the same listing for the operator",
         "the fleet tools must keep their logs for a week on the host that produced them and no longer")
VELO = ("the board support package must boot the linux image from the first partition of the card",
        "the board support package should expose the serial console on the second header of the board")
TWO_SESSIONS = _feature_rows(
    ("u", "Let's design the fleet tools."), ("b", "fleet-tools"), ("u", FLEET[0]), ("u", FLEET[1]),
    ("e", "fleet-tools"),
    ("u", "Now let's design the linux board support."), ("b", "velo5x0-linux-board-support"), ("u", VELO[0]),
    ("u", VELO[1]), ("e", "velo5x0-linux-board-support"), ("u", "After both sessions."))


def test_an_unbracketed_feature_is_refused_with_exit_2_naming_the_bracketed_features_and_writes_nothing(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    t = tmp_path / "s.jsonl"
    _write_rows(t, TWO_SESSIONS)
    before = sorted(p.name for p in work.iterdir())
    for extra in ([], ["--json"]):
        r = _run(EXTRACTOR, t, "--feature", "no-such-feature", *extra, cwd=work)
        assert r.returncode == 2, (extra, r.stdout, r.stderr)
        assert r.stdout == "", (extra, "a refusal prints no extraction", r.stdout)
        assert "REFUSED" in r.stderr and "'no-such-feature'" in r.stderr, r.stderr
        assert "'fleet-tools'" in r.stderr and "'velo5x0-linux-board-support'" in r.stderr, r.stderr
    assert sorted(p.name for p in work.iterdir()) == before == [], "a refusal must write no file"
    # a transcript with no bracket at all says so, rather than listing nothing as if that were an answer
    bare = tmp_path / "bare.jsonl"
    _write_rows(bare, [_u(HUMAN_TEXT, "human")])
    r = _run(EXTRACTOR, bare, "--feature", "fleet-tools", cwd=work)
    assert r.returncode == 2 and r.stdout == "" and "features with a bracket in this transcript: none" in r.stderr, r
    # the negative control: the same command with a feature that IS bracketed succeeds
    ok = _run(EXTRACTOR, t, "--feature", "fleet-tools", "--json", cwd=work)
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_a_feature_name_is_matched_exactly_not_as_a_substring(tmp_path):
    rows = _feature_rows(("u", "Design x-two."), ("b", "x-two"), ("u", HUMAN_TEXT), ("e", "x-two"))
    r = _feature_run(tmp_path, rows, "--feature", "x")
    assert r.returncode == 2 and "'x-two'" in r.stderr and r.stdout == "", r
    assert HUMAN_TEXT in _texts(_feature_run(tmp_path, rows, "--feature", "x-two"))


def test_two_sequential_brackets_give_each_feature_only_its_own_span(tmp_path):
    fleet = _feature_run(tmp_path, TWO_SESSIONS, "--feature", "fleet-tools")
    velo = _feature_run(tmp_path, TWO_SESSIONS, "--feature", "velo5x0-linux-board-support")
    assert fleet.returncode == 0 and velo.returncode == 0, (fleet.stderr, velo.stderr)
    # the triggering turn (the human turn just before the BEGIN) is included and labelled, then the span
    assert _texts(fleet) == ["Let's design the fleet tools.", *FLEET]
    assert _texts(velo) == ["Now let's design the linux board support.", *VELO]
    assert [s["role"] for s in json.loads(fleet.stdout)["statements"]][0] == "trigger"
    for r, other in ((fleet, VELO), (velo, FLEET)):
        assert not set(other) & set(_texts(r)), "a feature's slice must hold none of the other feature's turns"
        assert "After both sessions." not in _texts(r)
        j = json.loads(r.stdout)
        assert j["reconciliation"]["boundaries_seen"] == 4 and j["reconciliation"]["balances"] is True
    assert "fleet-tools" in json.loads(fleet.stdout)["bounds"]
    assert "velo5x0-linux-board-support" in json.loads(velo.stdout)["bounds"]


NEST = _feature_rows(
    ("u", "Design A."), ("b", "feat-a"), ("u", "A one."), ("u", "Design B."), ("b", "feat-b"), ("u", "B one."),
    ("e", "feat-b"), ("u", "A two."), ("e", "feat-a"), ("u", "After both."))


def test_interleaved_brackets_are_tracked_per_feature_and_the_outer_slice_holds_the_inner_turns(tmp_path):
    """Pins what the code defines: a BEGIN for F opens F's bracket even while another feature's is open; an END closes
    only its own feature; the outer feature's slice is by transcript position, so it contains the inner session's turns."""
    inner = _feature_run(tmp_path, NEST, "--feature", "feat-b")
    outer = _feature_run(tmp_path, NEST, "--feature", "feat-a")
    assert _texts(inner) == ["Design B.", "B one."]          # trigger + its own span, nothing of A's
    assert _texts(outer) == ["Design A.", "A one.", "Design B.", "B one.", "A two."]
    for r in (inner, outer):
        rc = json.loads(r.stdout)["reconciliation"]
        assert rc["boundaries_seen"] == 4 and rc["sentinel_ignored_reopen"] == 0 and rc["sentinel_ignored_unopened"] == 0
    # with no --feature the FIRST bracket is used (the outer one here)
    plain = _feature_run(tmp_path, NEST)
    assert _texts(plain) == _texts(outer)
    # crossed brackets (A, B, END A, END B): each END closes its own feature
    crossed = _feature_rows(("u", "Design A."), ("b", "feat-a"), ("u", "A one."), ("b", "feat-b"), ("u", "B one."),
                            ("e", "feat-a"), ("u", "B two."), ("e", "feat-b"), ("u", "After."))
    assert _texts(_feature_run(tmp_path, crossed, "--feature", "feat-a")) == ["Design A.", "A one.", "B one."]
    assert _texts(_feature_run(tmp_path, crossed, "--feature", "feat-b")) == ["A one.", "B one.", "B two."]


def test_a_repeated_begin_or_a_stray_end_is_counted_not_acted_on_even_while_another_feature_is_open(tmp_path):
    rows = _feature_rows(("u", "Design A."), ("b", "feat-a"), ("u", "A one."), ("b", "feat-a"), ("e", "feat-z"),
                         ("e", "feat-a"))
    r = _feature_run(tmp_path, rows, "--feature", "feat-a")
    rc = json.loads(r.stdout)["reconciliation"]
    assert _texts(r) == ["Design A.", "A one."]
    assert (rc["boundaries_seen"], rc["sentinel_ignored_reopen"], rc["sentinel_ignored_unopened"]) == (2, 1, 1)


def test_from_match_still_bounds_an_unbracketed_transcript_when_a_feature_is_named(tmp_path):
    rows = _feature_rows(("u", "Before the talk."), ("u", "Let's design the roster view now."), ("u", HUMAN_TEXT),
                         ("u", "After."))
    r = _feature_run(tmp_path, rows, "--feature", "roster-view", "--from-match", "design the roster")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _texts(r) == ["Let's design the roster view now.", HUMAN_TEXT, "After."]
    assert "first human statement matching /design the roster/i" in json.loads(r.stdout)["bounds"]
    # without --feature as well
    assert _texts(_feature_run(tmp_path, rows, "--from-match", "design the roster")) == _texts(r)
    # a --from-match that matches nothing is an empty slice that says so, still not the whole transcript
    none = _feature_run(tmp_path, rows, "--feature", "roster-view", "--from-match", "zzz-no-such-phrase")
    assert none.returncode == 0 and _texts(none) == [] and "NO human statement matched" in json.loads(none.stdout)["bounds"]
    # a bracket for the named feature wins over --from-match (the tool's documented limit 3)
    bracketed = _feature_rows(("u", "Design it."), ("b", "roster-view"), ("u", HUMAN_TEXT), ("e", "roster-view"))
    b = _feature_run(tmp_path, bracketed, "--feature", "roster-view", "--from-match", "Design it")
    assert "sentinel bracket" in json.loads(b.stdout)["bounds"]


def test_verifier_accepts_a_true_quote_and_rejects_a_fabricated_one(tmp_path):
    t = tmp_path / "session.jsonl"
    quote = _transcript(t)
    # the quote's real line, read the way the verifier reads it (the extractor's index is the same 1-based line)
    line = next(i for i, raw in enumerate(t.read_text(encoding="utf-8").splitlines(), 1) if quote in raw)
    spec = tmp_path / "spec.lts"
    spec.write_text("## The roster view [SPEC:WIDGET-ROSTER]\n\n- kind: spec\n", encoding="utf-8")
    fake = ("every device shall replicate the full population to every site without any "
            "exception whatsoever at all")
    good = tmp_path / "good.md"
    good.write_text(f"- **Stakeholder said** (line {line}): *\"{quote}\"*\n\nCites [WIDGET-ROSTER].\n", encoding="utf-8")
    bad = tmp_path / "bad.md"
    bad.write_text(f"- **Stakeholder said** (line {line}): *\"{fake}\"*\n\nCites [WIDGET-ROSTER].\n", encoding="utf-8")
    ok = _run(VERIFIER, good, "--transcript", t, "--spec", spec, cwd=tmp_path)
    # an unmarked transcript: the citations resolve but the speaker is not verified, so exit 3, never 0
    assert ok.returncode == 3, ok.stdout + ok.stderr
    assert "citations resolve; SPEAKER NOT VERIFIED (origin unknown)" in ok.stdout
    no = _run(VERIFIER, bad, "--transcript", t, "--spec", spec, cwd=tmp_path)
    assert no.returncode == 1, no.stdout
    assert "FABRICATED" in no.stdout


# --- neutrality: no private tree, home path or person's name ---------------------------------------------------------

def _neutrality_findings(text):
    # Every pattern is built from fragments so that this file does not itself spell the terms it forbids.
    def j(*parts):
        return "".join(parts)

    forbidden = [
        j(r"/Us", r"ers/"), j(r"/ho", r"me/\w"), r"~/\.", j(r"~/[a", r"-z]"),
        j(r"(?i:comp", r"oseio)"),
        j(r"\bDa", r"vid\b"), j(r"\bDa", r"ve\b"), j(r"\bH", r"oP\b"), j(r"\bBurn", r"ett\b"),
        j(r"\bPL", r"B-"), j(r"\bG", r"M-[A-Z]"), j(r"\bd", r"sd-1a"), j(r"\bCX", r"C-"), j(r"\bz", r"b-[a-z]"),
        j(r"\bZ", r"B\b"),
    ]
    return [p for p in forbidden if re.search(p, text)]


def test_shipped_files_name_no_person_no_private_tree_and_no_home_path():
    bad = {}
    for path in SHIPPED:
        hits = _neutrality_findings(path.read_text(encoding="utf-8"))
        if hits:
            bad[str(path.relative_to(ROOT))] = hits
    assert bad == {}


def test_the_neutrality_scan_can_fail():
    """Negative control: each kind of leak, planted, is found."""
    planted = ["see /Us" + "ers/x/tools", "cd ~/." + "claude/skills", "the Comp" + "oseIO tree", "Da" + "vid said",
               "[PL" + "B-X-1]", "z" + "b-team-operating", "a Z" + "B corpus", "Da" + "ve typed"]
    for text in planted:
        assert _neutrality_findings(text), text
    assert _neutrality_findings("the stakeholder said; tools/design-session/extract_design_inputs.py") == []


def test_no_shipped_file_carries_an_absolute_path_argument():
    for path in (EXTRACTOR, VERIFIER):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"""["']/(?:Users|home|private|tmp)/""", text), path.name
        assert "expanduser" not in text, path.name


# --- the overlay and the rules ---------------------------------------------------------------------------------------

@needs_publish_exclude
def test_the_overlay_is_named_in_publish_exclude():
    excluded = [l.split()[0] for l in (ROOT / "PUBLISH-EXCLUDE").read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.startswith("#")]
    assert OVERLAY in excluded


def test_the_overlay_is_ignored_and_an_example_ships():
    assert any(l.strip() == "/" + OVERLAY for l in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
    assert EXAMPLE.exists()
    for path in SKILLS.values():
        assert OVERLAY in path.read_text(encoding="utf-8"), path


def test_sdlc_names_the_design_session_and_the_install_rule():
    s = (ROOT / "SDLC.md").read_text(encoding="utf-8")
    assert "### 4.4 The Design Session" in s
    assert "goals" in s and "source statements" in s
    assert "symlink only into the tracked paths of the live checkout (whatever branch it serves), never into a worktree or a second checkout" in s
    assert "never a branch checkout" not in s
    assert "Known second copies" in s and "hand-kept second copies" in s and "its own copy of the two tools" in s


def test_the_audit_steps_name_the_four_directions_and_carry_a_brief_template():
    s = STEPS.read_text(encoding="utf-8")
    for word in ("MISSING", "ALTERED", "INVENTED", "AMBIGUOUS", "different model", "mechanically", "frame", "Auditor brief template"):
        assert word in s, word


# --- finding the repository root through a symlinked skill directory --------------------------------------------------

ONE_LINER = re.compile(r"python3 -c '([^']+)'")


@pytest.mark.parametrize("which", ["begin", "end"])
def test_a_skill_finds_the_repository_root_through_a_symlinked_skill_directory(which, tmp_path):
    """The skills document how to find the root: realpath of the SKILL.md, then `git -C <its dir> rev-parse
    --show-toplevel`. Run the documented one-liner itself, through a symlink in a temp dir."""
    skill_text = SKILLS[which].read_text(encoding="utf-8")
    one_liner = ONE_LINER.search(skill_text)
    assert one_liner, "the skill documents no realpath one-liner"
    assert "rev-parse --show-toplevel" in skill_text
    repo = tmp_path / "checkout"
    skill_dir = repo / "skills" / SKILLS[which].parent.name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(skill_text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    home_skills = tmp_path / "home" / "skills"
    home_skills.mkdir(parents=True)
    link = home_skills / SKILLS[which].parent.name
    link.symlink_to(skill_dir, target_is_directory=True)
    given = link / "SKILL.md"                      # the path as a harness would hand it over: through the link
    real = subprocess.run([sys.executable, "-c", one_liner.group(1), str(given)], capture_output=True, text=True, check=True)
    real_path = pathlib.Path(real.stdout.strip())
    assert real_path == (skill_dir / "SKILL.md").resolve()
    top = subprocess.run(["git", "-C", str(real_path.parent), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
    assert pathlib.Path(top.stdout.strip()).resolve() == repo.resolve()
    # negative control: the symlink's own parent is not inside the repository, so a root guessed from where the link
    # sits finds nothing; the documented steps are what make the difference
    guess = subprocess.run(["git", "-C", str(home_skills), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    assert guess.returncode != 0


def test_an_unmarked_transcript_never_ends_in_a_plain_pass(tmp_path):
    t = tmp_path / "s.jsonl"
    quote = _transcript(t)
    line = next(i for i, raw in enumerate(t.read_text(encoding="utf-8").splitlines(), 1) if quote in raw)
    r = _verify_one(tmp_path, t, quote, line)
    assert r.returncode == 3, r.stdout
    assert "VERDICT: citations resolve; SPEAKER NOT VERIFIED (origin unknown)" in r.stdout


def test_a_queue_record_that_extends_or_truncates_a_human_turn_is_not_the_stakeholders(tmp_path):
    ext = _load_extractor()
    tail = "also drop the audit table today without taking any backup first at all"
    for label, queued in (("extends", HUMAN_TEXT + " " + tail), ("prefix", HUMAN_TEXT[:40]),
                          ("prefix-long", HUMAN_TEXT[:-8])):
        t = tmp_path / (label + ".jsonl")
        rows = [_u("a first human turn that marks the transcript as origin-marked from here on", "human"), {"type": "queue-operation", "operation": "enqueue", "content": queued},
                _u(HUMAN_TEXT, "human")]
        t.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        stmts, _, rc = ext.collect(t)
        assert HUMAN_TEXT in [s.text for s in stmts] and len(stmts) == 2, label
        assert rc.excluded_by_kind == {"queued-no-origin": 1}, label
        assert rc.balances(len(stmts)), label


def test_a_queue_record_equal_after_whitespace_normalisation_is_credited(tmp_path):
    ext = _load_extractor()
    t = tmp_path / "s.jsonl"
    spaced = "  " + HUMAN_TEXT.replace(" ", "  ", 3) + "\n"
    rows = [_u("a first human turn that marks the transcript as origin-marked from here on", "human"), {"type": "queue-operation", "operation": "enqueue", "content": spaced},
            _u(HUMAN_TEXT, "human")]
    t.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    stmts, _, rc = ext.collect(t)
    # credited (not excluded); the extractor's own dedup is exact-string, so the spaced copy may stay beside it
    assert rc.admitted_queued == 1 and rc.excluded_by_kind == {}


def test_the_verifier_does_not_verify_the_extending_tail_of_a_queue_record(tmp_path):
    t = tmp_path / "s.jsonl"
    tail = "also drop the audit table today without taking any backup first at all"
    rows = [_u(HUMAN_TEXT, "human"),
            {"type": "queue-operation", "operation": "enqueue", "content": HUMAN_TEXT + " " + tail},
            _u(HUMAN_TEXT + " " + tail, "peer", isMeta=True)]
    t.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    r = _verify_one(tmp_path, t, tail, 2)
    assert r.returncode == 1 and "NOT-STAKEHOLDER" in r.stdout and "citations resolve" not in r.stdout, r.stdout


def test_a_quote_cited_at_an_excluded_line_fails_even_when_a_human_turn_has_the_words(tmp_path):
    t = tmp_path / "s.jsonl"
    rows = [_u(HUMAN_TEXT, "human"),
            _u(PEER_TEXT, "peer", isMeta=True),
            _u(NOTIF_TEXT, "task-notification"),
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}]
    t.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    for line, kind in ((2, "peer"), (3, "task-notification"), (4, "no-origin:tool-result")):
        r = _verify_one(tmp_path, t, HUMAN_TEXT, line)
        assert r.returncode == 1, (line, r.stdout)
        assert "NOT-STAKEHOLDER" in r.stdout and kind in r.stdout, (line, r.stdout)
        assert "citations resolve" not in r.stdout


def test_records_from_before_origin_marking_fall_back_loudly_and_are_counted_separately(tmp_path):
    ext = _load_extractor()
    old = "An early prompt typed before the harness marked turns, long enough to read as a statement."
    t = tmp_path / "s.jsonl"
    rows = _bracketed("roster-view", [_u(old), {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
            _u(HUMAN_TEXT, "human"), _u("a later unmarked peer note without any origin field at all", None,
                                         isMeta=True)])
    t.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = _extract(t, tmp_path)
    assert [s["text"] for s in out["statements"]] == [old, HUMAN_TEXT]
    o = out["origin"]
    assert o["mode"] == "marked" and o["before_first_marked_fallback"] == 1 and o["warning"] == PARTIAL
    assert o["excluded_by_kind"] == {"no-origin:tool-result": 1, "no-origin:meta": 1}
    assert out["reconciliation"]["balances"] is True
    r = _run(EXTRACTOR, t, "--feature", "roster-view", cwd=tmp_path)
    assert PARTIAL in r.stdout and "before the first origin-marked record" in r.stdout, r.stdout
    assert "origin     : verified" not in r.stdout, r.stdout   # N5: no line that contradicts the warning
    assert "origin.kind == human counts from the first origin-marked record" in out["origin"]["rule"]
    d = tmp_path / "d.md"
    d.write_text("nothing quoted here\n", encoding="utf-8")
    v = _run(VERIFIER, d, "--transcript", t, cwd=tmp_path)
    assert PARTIAL in v.stdout, v.stdout


def _early_peer_transcript(path, wrapped):
    """A peer message queued BEFORE the first origin-marked record, then the same words as a peer-origin turn."""
    body = "the peer says every site must keep a full copy of the roster for ninety days without exception"
    queued = f'<cross-session-message from="peer-x">{body}</cross-session-message>' if wrapped else body
    rows = [_u("an early prompt typed before any record carried an origin field at all, long enough"),
            {"type": "queue-operation", "operation": "enqueue", "content": queued},
            _u(NOTIF_TEXT, "task-notification"),
            _u(HUMAN_TEXT, "human"),
            _u(body, "peer", isMeta=True)]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return body


def test_a_peer_message_queued_before_the_first_marked_record_is_not_the_stakeholders(tmp_path):
    ext = _load_extractor()
    for wrapped in (False, True):
        t = tmp_path / f"early-{wrapped}.jsonl"
        body = _early_peer_transcript(t, wrapped)
        stmts, _, rc = ext.collect(t)
        assert not any(body in s.text for s in stmts), wrapped
        assert rc.excluded_by_kind.get("queued-no-origin") == 1, (wrapped, rc.excluded_by_kind)
        assert rc.balances(len(stmts))
        r = _verify_one(tmp_path, t, body, 2)
        assert r.returncode == 1 and "NOT-STAKEHOLDER" in r.stdout, (wrapped, r.stdout)
        assert "citations resolve" not in r.stdout


def test_a_queue_record_before_the_first_marked_record_counts_only_when_equal_to_an_admitted_user_turn(tmp_path):
    ext = _load_extractor()
    early = "an early prompt typed before any record carried an origin field at all, long enough"
    t = tmp_path / "s.jsonl"
    rows = [_u(early), {"type": "queue-operation", "operation": "enqueue", "content": "  " + early + "\n"},
            {"type": "queue-operation", "operation": "enqueue", "content": early + " and then some extra words nobody typed"},
            _u(HUMAN_TEXT, "human")]
    t.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    stmts, _, rc = ext.collect(t)
    assert [s.text for s in stmts] == [early, HUMAN_TEXT]
    assert rc.excluded_by_kind == {"queued-no-origin": 1}
    assert rc.balances(len(stmts))


def test_a_quote_resolving_only_through_a_fallback_record_exits_3(tmp_path):
    early = "an early prompt typed before any record carried an origin field at all, long enough"
    t = tmp_path / "s.jsonl"
    rows = [_u(early), _u(NOTIF_TEXT, "task-notification"), _u(HUMAN_TEXT, "human")]
    t.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    r = _verify_one(tmp_path, t, early, 1)
    assert r.returncode == 3, r.stdout
    assert "citations resolve; SPEAKER NOT VERIFIED (origin unknown)" in r.stdout
    ok = _verify_one(tmp_path, t, HUMAN_TEXT, 3)      # resolved through a human-origin turn: a plain pass
    assert ok.returncode == 0 and "SPEAKER NOT VERIFIED" not in ok.stdout, ok.stdout


def test_every_place_the_verifier_command_is_given_explains_exit_3():
    for path in (SKILLS["end"], STEPS):
        text = path.read_text(encoding="utf-8")
        i = text.index("python3 tools/design-session/verify_dsd_citations.py <")
        assert "exit 3" in text[i:i + 1500].lower() or "exits 3" in text[i:i + 1500].lower(), path
    assert "exit 3" in (ROOT / "SDLC.md").read_text(encoding="utf-8").split("verify_dsd_citations.py")[1][:400].lower()


MID_TEXT = "while you are working on the roster please also keep the audit table and take a backup before any change"


def _att(prompt, origin="human", human_turn=True, **extra):
    a = {"type": "queued_command", "prompt": prompt, "source_uuid": "u-1", "commandMode": "prompt"}
    if origin is not None:
        a["origin"] = {"kind": origin}
    if human_turn and origin == "human":
        a["humanTurn"] = True
    a.update(extra)
    return {"type": "attachment", "attachment": a}


def _write_rows(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_a_human_mid_turn_message_is_the_stakeholders_and_verifies_at_its_attachment_line(tmp_path):
    ext = _load_extractor()
    t = tmp_path / "s.jsonl"
    rows = [_u(HUMAN_TEXT, "human"),
            {"type": "queue-operation", "operation": "enqueue", "content": MID_TEXT},
            {"type": "queue-operation", "operation": "remove", "content": MID_TEXT},
            _att(MID_TEXT)]
    _write_rows(t, rows)
    stmts, _, rc = ext.collect(t)
    assert [s.text for s in stmts].count(MID_TEXT) == 1
    assert rc.excluded_by_kind == {} and rc.balances(len(stmts))
    r = _verify_one(tmp_path, t, MID_TEXT, 4)
    assert r.returncode == 0, r.stdout
    assert "citations resolve" in r.stdout and "NOT-STAKEHOLDER" not in r.stdout


def test_a_human_mid_turn_message_with_no_queue_record_and_a_list_prompt_counts(tmp_path):
    ext = _load_extractor()
    t = tmp_path / "s.jsonl"
    rows = [_u(HUMAN_TEXT, "human"),
            _att([{"type": "text", "text": MID_TEXT}, {"type": "image", "source": "x"}])]
    _write_rows(t, rows)
    stmts, _, rc = ext.collect(t)
    assert [s.text for s in stmts] == [HUMAN_TEXT, MID_TEXT]
    assert [s.channel for s in stmts][-1] == "queued"
    assert rc.admitted_queued_command == 1


def test_a_queued_command_with_a_non_human_origin_is_not_the_stakeholders(tmp_path):
    ext = _load_extractor()
    for origin, label in (("peer", "queued_command:peer"), ("task-notification", "queued_command:task-notification"),
                          (None, "queued_command:no-origin")):
        t = tmp_path / f"s-{origin}.jsonl"
        rows = [_u(HUMAN_TEXT, "human"),
                {"type": "queue-operation", "operation": "enqueue", "content": PEER_TEXT},
                _att(PEER_TEXT, origin)]
        _write_rows(t, rows)
        stmts, _, rc = ext.collect(t)
        assert PEER_TEXT not in [s.text for s in stmts], origin
        assert rc.excluded_by_kind.get(label) == 1, (origin, rc.excluded_by_kind)
        assert rc.balances(len(stmts)), origin
        r = _verify_one(tmp_path, t, PEER_TEXT, 3)
        assert r.returncode == 1 and "NOT-STAKEHOLDER" in r.stdout and "citations resolve" not in r.stdout, (origin, r.stdout)


def test_a_lone_queued_command_is_judged_at_its_own_line_by_its_origin(tmp_path):
    t = tmp_path / "s.jsonl"
    _write_rows(t, [_u(HUMAN_TEXT, "human"), _att(MID_TEXT), _att(PEER_TEXT, "peer")])
    ok = _verify_one(tmp_path, t, MID_TEXT, 2)
    assert ok.returncode == 0 and "NOT-STAKEHOLDER" not in ok.stdout, ok.stdout
    no = _verify_one(tmp_path, t, PEER_TEXT, 3)
    assert no.returncode == 1 and "NOT-STAKEHOLDER" in no.stdout and "queued_command:peer" in no.stdout, no.stdout
    # the human message cited at the peer attachment's line fails too
    wrong = _verify_one(tmp_path, t, MID_TEXT, 3)
    assert wrong.returncode == 1 and "NOT-STAKEHOLDER" in wrong.stdout, wrong.stdout


def test_the_json_and_text_counts_name_the_queued_command_attachments(tmp_path):
    t = tmp_path / "s.jsonl"
    _write_rows(t, _bracketed("roster-view", [_u(HUMAN_TEXT, "human"), _att(MID_TEXT), _att(PEER_TEXT, "peer")]))
    out = _extract(t, tmp_path)
    assert out["reconciliation"]["admitted_queued_command"] == 1
    assert out["origin"]["excluded_by_kind"] == {"queued_command:peer": 1}
    r = _run(EXTRACTOR, t, "--feature", "roster-view", cwd=tmp_path)
    assert "queued_command attachments" in r.stdout and "queued_command:peer" in r.stdout, r.stdout


def test_the_help_skill_and_audit_steps_count_a_queued_command_attachment_by_its_origin():
    h = _run(EXTRACTOR, "--help").stdout
    assert "queued_command" in h and "attachment.origin.kind" in h
    for path in (SKILLS["end"], STEPS):
        text = path.read_text(encoding="utf-8")
        assert "queued_command" in text and "attachment.origin.kind" in text, path
    steps = STEPS.read_text(encoding="utf-8")
    block = steps[steps.index("**Step 1a"):steps.index("**Step 2")]
    assert "queued_command" in block


# --- round 5: the zone boundary, the fallback breadth, and the quoted verdicts ----------------------------------------

EARLY = "an early prompt typed before any record carried an origin field at all, long enough to read as a statement"
EARLY_LONG = ("an early prompt typed before any record carried an origin field at all and it goes on for a good "
              "many more words so that a shortened quotation of it is still a long verbatim run")


def _verify_quote_at(tmp_path, rows, quote, line, name="s.jsonl"):
    t = tmp_path / name
    _write_rows(t, rows)
    return _verify_one(tmp_path, t, quote, line), t


def test_a_peer_message_queued_after_the_first_marked_record_never_borrows_the_early_credit(tmp_path):
    ext = _load_extractor()
    # the same words sit in an early no-origin user record AND are queued later (after marking began)
    rows = [_u(EARLY), _u(NOTIF_TEXT, "task-notification"), _u(HUMAN_TEXT, "human"),
            {"type": "queue-operation", "operation": "enqueue", "content": EARLY},
            _u(EARLY, "peer", isMeta=True)]
    r, t = _verify_quote_at(tmp_path, rows, EARLY, 4)
    assert r.returncode == 1 and "NOT-STAKEHOLDER" in r.stdout and "queued-no-origin" in r.stdout, r.stdout
    stmts, _, rc = ext.collect(t)
    assert rc.excluded_by_kind.get("queued-no-origin") == 1, rc.excluded_by_kind
    assert rc.balances(len(stmts))


def test_only_a_no_origin_user_record_before_the_first_marked_record_is_an_early_turn(tmp_path):
    ext = _load_extractor()
    meta = {"type": "user", "isMeta": True, "message": {"role": "user", "content": EARLY}}
    tool = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": EARLY}]}}
    later = _u(EARLY)       # a no-origin user record AFTER the first marked record
    for label, rows, queue_line in (
            ("meta", [meta, {"type": "queue-operation", "operation": "enqueue", "content": EARLY},
                      _u(NOTIF_TEXT, "task-notification"), _u(HUMAN_TEXT, "human")], 2),
            ("after", [{"type": "queue-operation", "operation": "enqueue", "content": EARLY},
                       _u(NOTIF_TEXT, "task-notification"), _u(HUMAN_TEXT, "human"), later], 1)):
        r, t = _verify_quote_at(tmp_path, rows, EARLY, queue_line, f"{label}.jsonl")
        assert r.returncode == 1 and "NOT-STAKEHOLDER" in r.stdout, (label, r.stdout)
        stmts, _, rc = ext.collect(t)
        assert rc.excluded_by_kind.get("queued-no-origin") == 1, (label, rc.excluded_by_kind)


def test_the_verifier_and_the_extractor_agree_that_an_early_queue_line_equal_to_an_early_user_turn_counts(tmp_path):
    ext = _load_extractor()
    rows = [_u(EARLY), {"type": "queue-operation", "operation": "enqueue", "content": EARLY},
            _u(NOTIF_TEXT, "task-notification"), _u(HUMAN_TEXT, "human")]
    r, t = _verify_quote_at(tmp_path, rows, EARLY, 2)
    # credited, but it is a FALLBACK line: exit 3 (speaker not verified), never 0 and never NOT-STAKEHOLDER
    assert r.returncode == 3, r.stdout
    assert "NOT-STAKEHOLDER" not in r.stdout and "SPEAKER NOT VERIFIED (origin unknown)" in r.stdout, r.stdout
    assert "resolves only through a record before the first origin-marked record" in r.stdout, r.stdout
    stmts, _, rc = ext.collect(t)
    assert rc.admitted_queued == 1 and rc.excluded_by_kind.get("queued-no-origin") is None


def test_a_shortened_quote_resolved_only_through_a_fallback_record_exits_3(tmp_path):
    rows = [_u(EARLY_LONG), _u(NOTIF_TEXT, "task-notification"), _u(HUMAN_TEXT, "human")]
    quote = " ".join(EARLY_LONG.split()[:10]) + " plus four invented closing words"
    r, _ = _verify_quote_at(tmp_path, rows, quote, 1)
    assert "'elided': 1" in r.stdout, r.stdout
    assert r.returncode == 3, r.stdout
    # the same shortened quote of a HUMAN turn is a plain pass
    ok, _ = _verify_quote_at(tmp_path, rows, " ".join(HUMAN_TEXT.split()[:12]) + " plus four invented closing words", 3, "h.jsonl")
    assert "'elided': 1" in ok.stdout and ok.returncode == 0, ok.stdout


def test_the_banner_does_not_say_verified_next_to_the_fallback_warning(tmp_path):
    rows = [_u(EARLY), _u(NOTIF_TEXT, "task-notification"), _u(HUMAN_TEXT, "human")]
    r, _ = _verify_quote_at(tmp_path, rows, EARLY, 1)
    head = r.stdout.split("DSD ")[0]
    assert PARTIAL in head and "origin: verified" not in head, head
    assert "origin: partial" in head
    marked_only = tmp_path / "m.jsonl"
    _origin_transcript(marked_only)
    d = tmp_path / "d.md"
    d.write_text("nothing quoted here\n", encoding="utf-8")
    assert "origin: verified" in _run(VERIFIER, d, "--transcript", marked_only, cwd=tmp_path).stdout


def _verifier_verdicts():
    ns = {}
    spec = importlib.util.spec_from_file_location("verify_dsd_citations_under_test", VERIFIER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.VERDICTS


def test_every_verdict_the_skill_and_the_steps_quote_is_one_the_verifier_can_print():
    verdicts = _verifier_verdicts()
    quoted_total = 0
    for path in (SKILLS["end"], STEPS):
        text = path.read_text(encoding="utf-8")
        for q in re.findall(r'"(citations resolve[^"]*)"', text):
            quoted_total += 1
            assert q in verdicts, (path.name, q)
    assert quoted_total >= 1
    skill = SKILLS["end"].read_text(encoding="utf-8")
    assert 'with its verdict "citations resolve; SPEAKER NOT VERIFIED (origin unknown)", then exits 3' in skill


def test_the_verdict_scan_can_fail():
    assert [q for q in ["citations resolve; SPEAKER NOT VERIFIED (origin unknown In a marked"] if q not in _verifier_verdicts()] != []


def test_the_help_and_the_docs_state_the_queue_rule_as_the_code_applies_it():
    h = _run(EXTRACTOR, "--help").stdout
    flat = " ".join(h.split())
    for text in (flat, " ".join(SKILLS["end"].read_text(encoding="utf-8").split()),
                 " ".join(STEPS.read_text(encoding="utf-8").split())):
        assert "an admitted early user turn" in text and "fallback record" in text
        assert "No prefix or extension credit, in any zone" in text or "no prefix or extension credit" in text.lower()
        assert "that holds in every zone" not in text and "falls back to the old rule and says" not in text.replace(
            "falls back to the old rule and says \"origin", "")
    steps = STEPS.read_text(encoding="utf-8")
    block = steps[steps.index("**Step 1a"):steps.index("**Step 2")]
    assert "before marking began" not in block and "admitted early user turn" in block


def test_no_stray_comma_in_the_residual_limit_sentences():
    for path in (SKILLS["end"], STEPS):
        assert "on any user record, falls back" not in path.read_text(encoding="utf-8"), path


# --- round 6: the verifier reads list prompts; the harness-text tripwire ----------------------------------------------

HARNESS_LINES = {
    "[Gnomon]": "[Gnomon] the harness typed this notice into the session and it is long enough to read as a statement",
    "[GNOMON NOTICE]": "[GNOMON NOTICE] another harness line long enough to read as a statement of the stakeholder",
    "[DSD ANSWERED]": "[DSD ANSWERED] a decision notice typed by the harness, long enough to read as a statement",
    "[scheduled:": "[scheduled: morning-report by an agent at the usual hour] run the report and write it somewhere",
}


def test_the_verifier_reads_a_human_mid_turn_message_whose_prompt_is_a_list_of_blocks(tmp_path):
    t = tmp_path / "s.jsonl"
    _write_rows(t, [_u(HUMAN_TEXT, "human"),
                    _att([{"type": "text", "text": MID_TEXT}, {"type": "image", "source": "x"}])])
    r = _verify_one(tmp_path, t, MID_TEXT, 2)
    assert r.returncode == 0 and "'verified': 1" in r.stdout and "FABRICATED" not in r.stdout, r.stdout
    # the same list prompt under a peer origin is NOT-STAKEHOLDER, so the list is read in both directions
    t2 = tmp_path / "p.jsonl"
    _write_rows(t2, [_u(HUMAN_TEXT, "human"), _att([{"type": "text", "text": PEER_TEXT}], "peer")])
    r2 = _verify_one(tmp_path, t2, PEER_TEXT, 2)
    assert r2.returncode == 1 and "NOT-STAKEHOLDER" in r2.stdout, r2.stdout


def test_a_human_marked_record_that_begins_with_a_harness_prefix_is_excluded(tmp_path):
    ext = _load_extractor()
    for prefix, text in HARNESS_LINES.items():
        label = "harness-injection:" + prefix
        for form in ("user", "attachment"):
            t = tmp_path / f"{abs(hash((prefix, form)))}.jsonl"
            row = _u("   " + text, "human") if form == "user" else _att(text)
            _write_rows(t, [_u(HUMAN_TEXT, "human"), row,
                            {"type": "queue-operation", "operation": "enqueue", "content": text}])
            stmts, _, rc = ext.collect(t)
            assert [s.text for s in stmts] == [HUMAN_TEXT], (prefix, form, [s.text[:20] for s in stmts])
            # the marked record and its queue-operation echo are both excluded under the same kind
            assert rc.excluded_by_kind == {label: 2}, (prefix, form, rc.excluded_by_kind)
            assert rc.balances(len(stmts)), (prefix, form)
            r = _verify_one(tmp_path, t, text.lstrip(), 2)
            assert r.returncode == 1 and "NOT-STAKEHOLDER" in r.stdout and label in r.stdout, (prefix, form, r.stdout)


def test_an_ordinary_human_line_that_merely_contains_a_harness_prefix_is_still_admitted(tmp_path):
    ext = _load_extractor()
    line = "when the harness prints [Gnomon] at the start of a line please ignore it and keep the roster as it is"
    t = tmp_path / "s.jsonl"
    _write_rows(t, [_u(HUMAN_TEXT, "human"), _u(line, "human"), _att("also this one mentions [DSD ANSWERED] midway through the sentence")])
    stmts, _, rc = ext.collect(t)
    assert line in [s.text for s in stmts] and len(stmts) == 3, [s.text[:30] for s in stmts]
    assert not any(k.startswith("harness-injection") for k in rc.excluded_by_kind)
    r = _verify_one(tmp_path, t, line, 2)
    assert r.returncode == 0 and "NOT-STAKEHOLDER" not in r.stdout, r.stdout


def test_the_harness_prefix_tripwire_also_applies_to_an_unmarked_transcript(tmp_path):
    ext = _load_extractor()
    t = tmp_path / "s.jsonl"
    _write_rows(t, [_u(HUMAN_TEXT), _u(HARNESS_LINES["[Gnomon]"])])
    stmts, _, rc = ext.collect(t)
    assert [s.text for s in stmts] == [HUMAN_TEXT] and rc.excluded_by_kind == {"harness-injection:[Gnomon]": 1}


def test_the_help_the_skill_and_the_steps_state_the_tripwire_and_its_limits():
    texts = [" ".join(_run(EXTRACTOR, "--help").stdout.split()),
             " ".join(SKILLS["end"].read_text(encoding="utf-8").split()),
             " ".join(STEPS.read_text(encoding="utf-8").split())]
    for text in texts:
        assert "lexical tripwire" in text.lower() and "harness-injection:<prefix>" in text
        assert "only the fixed prefixes" in text and "cannot see harness text that lacks one" in text
        assert "operator" in text and "would be excluded" in text and "harness owner" in text
        assert '"[Gnomon]"' in text and '"[DSD ANSWERED]"' in text and '"[scheduled:"' in text


# --- round 7: the harness-prefix list is data: a public default plus the deployment's private overlay -----------------

SITE_LINE = "[SiteBot@2026-01-01 10:00] a site-specific harness notice long enough to read as a statement of the stakeholder"
SITE_LINE_2 = "[SiteBot] a second site-specific harness notice long enough to read as a statement of the stakeholder"
DEFAULT_PREFIXES = ["[GNOMON NOTICE]", "[GNOMON", "[Gnomon@", "[Gnomon]", "[DSD ANSWERED]", "[scheduled:"]


def _site_transcript(path, feature="x"):
    """The three-turn site transcript inside a bracket for `feature` (its BEGIN is line 1, so SITE_LINE is line 3)."""
    _write_rows(path, _bracketed(feature, [_u(HUMAN_TEXT, "human"), _u(SITE_LINE, "human"), _att(SITE_LINE_2)]))


def _overlay(path, *prefixes, extra=""):
    body = "# overlay\n\n## Auditor agent\n\n- auditor-agent: none\n\n## Harness prefixes\n\n- harness-prefixes:\n"
    path.write_text(body + "".join(f"  - {x}\n" for x in prefixes) + extra, encoding="utf-8")
    return path


def test_the_shipped_default_holds_only_the_public_harness_prefixes_and_no_site_vocabulary():
    ext = _load_extractor()
    assert list(ext.DEFAULT_HARNESS_PREFIXES) == DEFAULT_PREFIXES
    assert "SiteBot" not in EXTRACTOR.read_text(encoding="utf-8")
    assert _shipped_files_naming_the_site() == []


def _site_name_pattern():
    # built in pieces so this file does not itself carry the site name; case-insensitive,
    # tolerating a space, hyphen or underscore inside the name
    return re.compile("zero" + r"[\s_-]*" + "dash" + "board", re.I)


def _publish_excluded_dirs():
    """The directories PUBLISH-EXCLUDE removes from a publication. In a published snapshot there is no PUBLISH-EXCLUDE
    and every such directory is already absent, so there is nothing to skip: the walk below sees only what ships."""
    if not (ROOT / "PUBLISH-EXCLUDE").exists():
        return []
    out = []
    for line in (ROOT / "PUBLISH-EXCLUDE").read_text(encoding="utf-8").splitlines():
        tok = line.split("#")[0].split()
        if tok and tok[0].endswith("/"):
            out.append(tok[0])
    return out


def _shipped_files_naming_the_site(extra_files=(), root=None):
    """Every shipped file under tools/design-session/, skills/ and docs/ that names the site: a directory walk
    that reads EVERY file decoding as UTF-8, whatever its suffix, and skips binaries (paths PUBLISH-EXCLUDE removes
    from a publication are not shipped), plus this test file when `root` is the repository. Scope: ONLY this one
    deployment name. Any other deployment name, product or person is the disclosure guard's job, run with the
    private rules and vocabulary loaded; this test does not stand in for it."""
    pat = _site_name_pattern()
    skip = _publish_excluded_dirs()
    base = ROOT if root is None else pathlib.Path(root)
    files = list(extra_files) + ([pathlib.Path(__file__)] if root is None else [])
    for top in ("tools/design-session", "skills", "docs"):
        for f in sorted((base / top).rglob("*")):
            rel = f.relative_to(base).as_posix()
            if f.is_file() and not any(rel.startswith(d) for d in skip):
                files.append(f)
    hits = []
    for f in files:
        try:
            text = f.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue                    # a binary
        if pat.search(text):
            hits.append(f)
    return hits


def test_the_site_name_scan_catches_case_and_spacing_variants_and_walks_the_tree(tmp_path):
    pat = _site_name_pattern()
    for variant in ("Zero" + "Dashboard", "zero" + "dashboard", "ZERO" + "DASHBOARD", "zero " + "dashboard",
                    "Zero-" + "Dashboard", "zero_" + "dash" + "board"):
        assert pat.search(f"x {variant} y"), variant
    planted = tmp_path / "planted.md"
    planted.write_text("see the zero " + "dash" + "board notes\n", encoding="utf-8")
    assert _shipped_files_naming_the_site(extra_files=[planted]) == [planted]
    walked = {p.relative_to(ROOT).as_posix() for p in (ROOT / "skills").rglob("*.md")}
    assert SKILLS["end"].relative_to(ROOT).as_posix() in walked and len(walked) >= 2


def test_a_site_prefixed_record_is_admitted_without_an_overlay_and_excluded_with_one(tmp_path):
    t = tmp_path / "s.jsonl"
    _site_transcript(t)
    off = _run(EXTRACTOR, t, "--feature", "x", "--no-overlay", "--json", cwd=tmp_path)
    assert off.returncode == 0, off.stdout + off.stderr
    o = json.loads(off.stdout)
    assert [x["text"] for x in o["statements"]] == [HUMAN_TEXT, SITE_LINE, SITE_LINE_2]
    assert o["harness_prefixes"]["source"] == "default" and o["harness_prefixes"]["warning"] is None
    ov = _overlay(tmp_path / "ov.md", "[SiteBot@", "[SiteBot]")
    on = _run(EXTRACTOR, t, "--feature", "x", "--overlay", ov, "--json", cwd=tmp_path)
    assert on.returncode == 0, on.stdout + on.stderr
    n = json.loads(on.stdout)
    assert [x["text"] for x in n["statements"]] == [HUMAN_TEXT]
    assert n["origin"]["excluded_by_kind"] == {"harness-injection:[SiteBot@": 1, "harness-injection:[SiteBot]": 1}
    assert n["harness_prefixes"]["source"] == "default+overlay"
    assert n["harness_prefixes"]["prefixes"][-2:] == ["[SiteBot@", "[SiteBot]"]
    txt = _run(EXTRACTOR, t, "--feature", "x", "--overlay", ov, cwd=tmp_path).stdout
    assert "harness prefixes" in txt and "default plus overlay" in txt, txt
    # the verifier: the same overlay makes a quote of the site line NOT-STAKEHOLDER; without it, a plain pass
    spec = tmp_path / "spec.lts"
    spec.write_text("## The roster view [SPEC:WIDGET-ROSTER]\n\n- kind: spec\n", encoding="utf-8")
    d = tmp_path / "d.md"
    d.write_text(f"- **Stakeholder said** (line 3): *\"{SITE_LINE}\"*\n\nCites [WIDGET-ROSTER].\n", encoding="utf-8")
    v_on = _run(VERIFIER, d, "--transcript", t, "--spec", spec, "--overlay", ov, cwd=tmp_path)
    assert v_on.returncode == 1 and "NOT-STAKEHOLDER" in v_on.stdout and "harness-injection:[SiteBot@" in v_on.stdout, v_on.stdout
    assert "default plus overlay" in v_on.stdout
    v_off = _run(VERIFIER, d, "--transcript", t, "--spec", spec, "--no-overlay", cwd=tmp_path)
    assert v_off.returncode == 0 and "NOT-STAKEHOLDER" not in v_off.stdout, v_off.stdout


def test_a_missing_or_unreadable_overlay_is_a_loud_warning_and_the_default_still_applies(tmp_path):
    t = tmp_path / "s.jsonl"
    _site_transcript(t)
    bad_bytes = tmp_path / "bin.md"
    bad_bytes.write_bytes(b"\xff\xfe\x00 not utf-8 \xff")
    a_dir = tmp_path / "adir.md"
    a_dir.mkdir()
    for label, path in (("missing", tmp_path / "nope.md"), ("binary", bad_bytes), ("directory", a_dir)):
        r = _run(EXTRACTOR, t, "--feature", "x", "--overlay", path, "--json", cwd=tmp_path)
        assert r.returncode == 0, (label, r.stdout + r.stderr)
        h = json.loads(r.stdout)["harness_prefixes"]
        assert h["source"] == "default" and h["warning"] and "overlay" in h["warning"].lower(), (label, h)
        assert "unreadable" in h["warning"].lower() or "not found" in h["warning"].lower(), (label, h)
        txt = _run(EXTRACTOR, t, "--feature", "x", "--overlay", path, cwd=tmp_path)
        assert "OVERLAY" in txt.stdout and "default" in txt.stdout and "OVERLAY" in txt.stderr, (label, txt.stdout, txt.stderr)
        d = tmp_path / "d.md"
        d.write_text("nothing quoted here\n", encoding="utf-8")
        v = _run(VERIFIER, d, "--transcript", t, "--overlay", path, cwd=tmp_path)
        assert "OVERLAY" in v.stdout, (label, v.stdout)


def test_an_unreadable_overlay_keeps_the_default_list_in_the_json_and_in_the_exclusion(tmp_path):
    bad = tmp_path / "bin.md"
    bad.write_bytes(b"\xff\xfe\x00 not utf-8 \xff")
    a_dir = tmp_path / "adir.md"
    a_dir.mkdir()
    harness_row = tmp_path / "hrow.jsonl"
    _write_rows(harness_row, _bracketed("x", [_u(HUMAN_TEXT, "human"), _u(HARNESS_LINES["[Gnomon]"], "human")]))
    for label, path in (("binary", bad), ("directory", a_dir), ("missing", tmp_path / "nope.md")):
        j = json.loads(_run(EXTRACTOR, harness_row, "--feature", "x", "--overlay", path, "--json", cwd=tmp_path).stdout)
        h = j["harness_prefixes"]
        assert h["warning"], label
        assert h["prefixes"] == DEFAULT_PREFIXES, (label, h["prefixes"])
        assert j["origin"]["excluded_by_kind"] == {"harness-injection:[Gnomon]": 1}, (label, j["origin"])


def test_an_overlay_entry_that_cannot_be_a_prefix_is_ignored_with_a_warning(tmp_path):
    t = tmp_path / "s.jsonl"
    _site_transcript(t)
    ov = _overlay(tmp_path / "ov.md", "[", "<a placeholder from the example>", "[SiteBot]")
    r = _run(EXTRACTOR, t, "--feature", "x", "--overlay", ov, "--json", cwd=tmp_path)
    h = json.loads(r.stdout)["harness_prefixes"]
    assert h["prefixes"][-1] == "[SiteBot]" and "[" not in h["prefixes"][len(DEFAULT_PREFIXES):-1]
    assert h["warning"] and "too short" in h["warning"]
    # an overlay with no harness-prefixes entry is fine: the default, with the source named
    ok = tmp_path / "plain.md"
    ok.write_text("# overlay\n\n- auditor-agent: none\n", encoding="utf-8")
    h2 = json.loads(_run(EXTRACTOR, t, "--feature", "x", "--overlay", ok, "--json", cwd=tmp_path).stdout)["harness_prefixes"]
    assert h2["source"] == "default" and h2["warning"] is None


def test_the_help_the_skill_and_the_steps_say_where_the_prefix_list_comes_from():
    texts = [" ".join(_run(EXTRACTOR, "--help").stdout.split()),
             " ".join(SKILLS["end"].read_text(encoding="utf-8").split()),
             " ".join(STEPS.read_text(encoding="utf-8").split()),
             " ".join(EXAMPLE.read_text(encoding="utf-8").split())]
    for text in texts:
        assert "harness-prefixes:" in text and "private/design-session.local.md" in text, text[:80]
    for text in texts[:3]:
        assert "default plus overlay" in text and "lexical tripwire" in text.lower()
        assert "cannot see harness text that lacks one" in text


def test_a_readable_overlay_whose_entry_does_not_parse_is_a_loud_warning(tmp_path):
    t = tmp_path / "s.jsonl"
    _site_transcript(t)
    # one phrase per case: the warning must NAME its case, not just start with "OVERLAY"
    shapes = {
        "unindented bullets": ("# o\n\n- harness-prefixes:\n- [SiteBot]\n- [SiteBot@\n", "bullets are not indented under the key"),
        "misspelled key": ("# o\n\n- harness-prefix:\n  - [SiteBot]\n", "the key must be spelled exactly"),
        "key with no items": ("# o\n\n- harness-prefixes:\n\n## Next section\n", "has no items"),
        "key then another entry": ("# o\n\n- harness-prefixes:\n- auditor-agent: none\n", "has no items"),
        "item after a blank line": ("# o\n\n- harness-prefixes:\n  - [SiteBot]\n\n  - [SiteBot@\n", "after a blank line"),
        "inline value": ("# o\n\n- harness-prefixes: [SiteBot], [SiteBot@\n", "not on the key's own line"),
    }
    for label, (body, phrase) in shapes.items():
        ov = tmp_path / "ov.md"
        ov.write_text(body, encoding="utf-8")
        r = _run(EXTRACTOR, t, "--feature", "x", "--overlay", ov, "--json", cwd=tmp_path)
        assert r.returncode == 0, (label, r.stdout + r.stderr)
        h = json.loads(r.stdout)["harness_prefixes"]
        assert h["warning"] and h["warning"].startswith("OVERLAY"), (label, h)
        assert phrase in h["warning"], (label, phrase, h["warning"])
        # J2: whatever the warning, the shipped default list still applies, in the JSON and in the exclusion
        assert h["prefixes"][:len(DEFAULT_PREFIXES)] == DEFAULT_PREFIXES, (label, h["prefixes"])
        harness_row = tmp_path / "hrow.jsonl"
        _write_rows(harness_row, _bracketed("x", [_u(HUMAN_TEXT, "human"), _u(HARNESS_LINES["[Gnomon]"], "human")]))
        hj = json.loads(_run(EXTRACTOR, harness_row, "--feature", "x", "--overlay", ov, "--json", cwd=tmp_path).stdout)
        assert hj["origin"]["excluded_by_kind"] == {"harness-injection:[Gnomon]": 1}, (label, hj["origin"])
        txt = _run(EXTRACTOR, t, "--feature", "x", "--overlay", ov, cwd=tmp_path)
        assert "OVERLAY" in txt.stdout and "OVERLAY" in txt.stderr, (label, txt.stdout, txt.stderr)
        d = tmp_path / "d.md"
        d.write_text("nothing quoted here\n", encoding="utf-8")
        v = _run(VERIFIER, d, "--transcript", t, "--overlay", ov, cwd=tmp_path)
        assert "OVERLAY" in v.stdout, (label, v.stdout)
    # the control: a well-formed entry gives no warning
    ok = _overlay(tmp_path / "ok.md", "[SiteBot]")
    assert json.loads(_run(EXTRACTOR, t, "--feature", "x", "--overlay", ok, "--json", cwd=tmp_path).stdout)["harness_prefixes"]["warning"] is None


def test_a_valid_entry_followed_by_another_top_level_entry_does_not_warn(tmp_path):
    t = tmp_path / "s.jsonl"
    _site_transcript(t)
    for label, tail in (("key", "- auditor-agent: none\n"), ("heading", "\n## Where decision documents go\n\n- x: y\n"),
                        ("blank then key", "\n- decision-document-channel: a folder\n")):
        ov = tmp_path / "ov.md"
        ov.write_text("# o\n\n- harness-prefixes:\n  - [SiteBot@\n  - [SiteBot]\n" + tail, encoding="utf-8")
        h = json.loads(_run(EXTRACTOR, t, "--feature", "x", "--overlay", ov, "--json", cwd=tmp_path).stdout)["harness_prefixes"]
        assert h["warning"] is None and h["prefixes"][-2:] == ["[SiteBot@", "[SiteBot]"], (label, h)
        assert h["source"] == "default+overlay"


def test_the_site_name_scan_reads_every_utf8_file_whatever_its_suffix_and_skips_binaries(tmp_path):
    for sub_ in ("tools/design-session", "skills/x", "docs"):
        (tmp_path / sub_).mkdir(parents=True)
    name = "zero " + "dash" + "board"
    (tmp_path / "tools/design-session/conf.yml").write_text(f"note: {name}\n", encoding="utf-8")
    (tmp_path / "skills/x/page.html").write_text(f"<p>{name}</p>\n", encoding="utf-8")
    (tmp_path / "docs/NOTES").write_text(f"{name}\n", encoding="utf-8")                  # no suffix
    (tmp_path / "docs/blob.bin").write_bytes(b"\xff\xfe\x00" + name.encode() + b"\xff")      # not UTF-8: skipped
    (tmp_path / "docs/clean.md").write_text("nothing here\n", encoding="utf-8")
    hits = sorted(p.relative_to(tmp_path).as_posix() for p in _shipped_files_naming_the_site(root=tmp_path))
    assert hits == ["docs/NOTES", "skills/x/page.html", "tools/design-session/conf.yml"], hits


def test_the_docs_state_the_general_overlay_claim_and_name_no_enumerated_cases():
    texts = {"help": " ".join(_run(EXTRACTOR, "--help").stdout.split())}
    for name, path in (("skill", SKILLS["end"]), ("steps", STEPS), ("example", EXAMPLE)):
        texts[name] = " ".join(path.read_text(encoding="utf-8").split())
    claim = ("Only the one layout above is read. Some departures from it warn; any other may be read in part, wrongly, "
             "or not at all, without a warning. After any overlay edit, check the parsed list itself: run the extractor "
             "with --json and read harness_prefixes.prefixes (the counts line shows only how many were added).")
    for name, text in texts.items():
        assert claim in text, name
        # the one-layout description above it is kept
        assert "The one layout that is read:" in text and "no blank line inside" in text, name
        # no enumeration of warned cases, and no older wording, remains in these four texts
        for gone in ("The cases named here warn", "a bullet not indented", "near-miss spelling",
                     "an indented prefix item after a blank line", "a key with no items", "a value on the key's own line",
                     "shorter than 3 characters", "WITHOUT a warning", "check the counts block",
                     "a second `harness-prefixes:` key", "a heading in place of the bullet", "known limits, not warned"):
            assert gone not in text, (name, gone)
    for name in ("skill", "steps"):    # the layout text sits after the sentence it once interrupted
        text = texts[name]
        assert "applied; the checker calls a quote of it NOT-STAKEHOLDER. The one layout" in text, name


def test_the_help_has_no_overlong_line_in_its_harness_paragraph():
    lines = _run(EXTRACTOR, "--help").stdout.splitlines()
    start = next(i for i, l in enumerate(lines) if "2. HARNESS TEXT" in l)
    end = next(i for i, l in enumerate(lines) if "3. --from-match" in l)
    assert max(len(l) for l in lines[start:end]) <= 80, [len(l) for l in lines[start:end]]


def test_the_overlay_parser_docstring_carries_the_same_wording():
    ext = _load_extractor()
    doc = " ".join(ext._overlay_entries.__doc__.split())
    assert "Only the one layout above is read. Some departures from it warn; any other may be read in part, wrongly, or not at all, without a warning." in doc
    assert "harness_prefixes.prefixes" in doc and "a misspelled key" not in doc and "an unindented bullet" not in doc


def test_a_non_human_record_that_starts_with_a_harness_prefix_gets_the_same_kind_from_both_tools(tmp_path):
    ext = _load_extractor()
    cases = [("peer", _u("[Gnomon] " + PEER_TEXT, "peer", isMeta=True), "peer"),
             ("task-notification", _u("[DSD ANSWERED] " + NOTIF_TEXT, "task-notification"), "task-notification"),
             ("peer attachment", _att("[Gnomon@2026-01-01 10:00] " + PEER_TEXT, "peer"), "queued_command:peer")]
    for label, row, kind in cases:
        t = tmp_path / f"{abs(hash(label))}.jsonl"
        _write_rows(t, [_u(HUMAN_TEXT, "human"), row])
        _, _, rc = ext.collect(t)
        assert rc.excluded_by_kind == {kind: 1}, (label, rc.excluded_by_kind)
        text = row["message"]["content"] if "message" in row else row["attachment"]["prompt"]
        r = _verify_one(tmp_path, t, text, 2)
        assert r.returncode == 1 and "NOT-STAKEHOLDER" in r.stdout, (label, r.stdout)
        assert kind in r.stdout and "harness-injection" not in r.stdout, (label, r.stdout)
    # a human-marked record with the prefix keeps the harness kind in both
    t = tmp_path / "h.jsonl"
    _write_rows(t, [_u(HUMAN_TEXT, "human"), _u("[Gnomon] a human-marked harness line long enough to read as a statement", "human")])
    _, _, rc = ext.collect(t)
    assert rc.excluded_by_kind == {"harness-injection:[Gnomon]": 1}


def test_the_audit_steps_say_a_harness_reminder_counts_where_the_stakeholder_wrote_it():
    block = STEPS.read_text(encoding="utf-8")
    block = block[block.index("**Step 1a"):block.index("**Step 2")]
    flat = " ".join(block.split())
    assert "[Gnomon] reminder:" in flat and "where he wrote them, not where the harness replays them" in flat


def test_the_audit_steps_independent_count_is_by_origin():
    text = STEPS.read_text(encoding="utf-8")
    i = text.index("**Step 1a")
    block = text[i:text.index("**Step 2")]
    assert "origin.kind == human" in block and "peer" in block and "not a missed statement" in block
    assert "covering ordinary user records and the queue records" not in block



# --- the known limits are stated where a user meets them --------------------------------------------------------------

def test_the_extractor_help_states_the_origin_rule_and_the_residual_limit():
    r = _run(EXTRACTOR, "--help")
    assert r.returncode == 0
    assert "Only a turn marked origin.kind == human counts as the stakeholder's" in r.stdout
    assert UNKNOWN in r.stdout
    assert "another agent's message can be counted as the stakeholder's" not in r.stdout
    assert "--from-match is ignored when a sentinel bracket exists" in r.stdout


def test_the_checker_states_the_rule_in_its_docstring_and_its_output_banner(tmp_path):
    text = VERIFIER.read_text(encoding="utf-8")
    assert "ONLY A TURN MARKED origin.kind == human COUNTS AS THE STAKEHOLDER'S" in text
    assert "KNOWN LIMIT: ANOTHER AGENT'S MESSAGE CAN BE COUNTED" not in text
    d = tmp_path / "d.md"
    d.write_text("nothing quoted here\n", encoding="utf-8")
    old, new = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
    _transcript(old)
    _origin_transcript(new)
    r_old = _run(VERIFIER, d, "--transcript", old, cwd=tmp_path)
    assert UNKNOWN in r_old.stdout, r_old.stdout
    r_new = _run(VERIFIER, d, "--transcript", new, cwd=tmp_path)
    assert UNKNOWN not in r_new.stdout, r_new.stdout
    assert "origin.kind == human only" in r_new.stdout and "peer 1" in r_new.stdout, r_new.stdout


def test_the_end_skill_and_the_audit_steps_state_the_rule_and_the_residual_limit():
    text = SKILLS["end"].read_text(encoding="utf-8")
    assert "another agent's message can be counted as the stakeholder's" not in text
    assert "only a turn marked `origin.kind == human` counts as the stakeholder's" in text
    assert UNKNOWN in text
    assert text.index("--from-match") < text.index("only a turn marked `origin.kind == human`")
    steps = STEPS.read_text(encoding="utf-8")
    assert "does not yet tell them apart" not in steps and "Interim rule" not in steps
    assert "only a turn marked `origin.kind == human` counts as the stakeholder's" in steps.lower()
    assert UNKNOWN in steps


# --- nothing is quoted as the stakeholder's words that was not checked against a transcript ---------------------------

UNCHECKED_STRINGS = [
    "grep your transaction log for rbac",
    "my intent was that if I say",
    "engaging in 'feature definition",
    "I'm fine with (person_id, finger)",
    "For the initial build, let's have tabs for",
    "tabs for the things that are discreet",
    "Quoted strings below are verbatim",
]


def test_the_extractor_quotes_no_unchecked_string_as_the_stakeholders_words_and_vouches_for_none():
    text = EXTRACTOR.read_text(encoding="utf-8")
    present = [x for x in UNCHECKED_STRINGS if x in text]
    assert present == []
    assert "not his words" in text  # the relabelled form is there


def test_the_unchecked_string_check_can_fail():
    assert [x for x in UNCHECKED_STRINGS if x in 'he said "grep your transaction log for rbac".'] != []
