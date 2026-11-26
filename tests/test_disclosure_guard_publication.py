"""disclosure_guard --publication: the fail-closed publication mode (spec 42: R1, R2, R5, R6, R7, D2).

Checks: T1a (a missing, empty or comment-only private file is red), T1b (a row that misses its own
control is red), T2 (a planted term is red and never printed; green under an excluded path), T3 (a clean
tree is green and its scope line is exact), T6 (an unconfigured lenient run differs from a configured one),
and the map manifest.

The fixture vocabulary is synthetic (`zorblax`, `frumpkin`). Every test builds its tree and BOTH private
files under tmp_path and points both environment variables at them, so the maintainer's real private files
can never leak into (or mask) a result.

Two knobs exist so these tests can be run against an OLDER copy of the guard, to show them red first:
  DG_GUARD_UNDER_TEST   path of the guard to run (default: tools/disclosure_guard.py of this checkout)
  DG_PUBLICATION_FLAG   the flag that stands for publication mode (default `--publication`; `--tree` runs
                        the same tests against the old tree mode, where the new flag does not exist)
"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

GUARD = pathlib.Path(os.environ.get("DG_GUARD_UNDER_TEST") or
                     pathlib.Path(__file__).resolve().parents[1] / "tools" / "disclosure_guard.py")
FLAG = os.environ.get("DG_PUBLICATION_FLAG", "--publication")
T = "\t"
TERM_V, TERM_D = "zorblax", "frumpkin"
VOCAB_OK = (T.join([TERM_V, f"made by {TERM_V} inc", "a vendor name", "vocab-vendor"]) + "\n"
            + T.join([r"gribble\d+", "gribble42", "a ticket shape", "vocab-ticket"]) + "\n")
DENY_OK = "# customers\n" + T.join([TERM_D, f"{TERM_D} ltd", "a customer", "deny-customer"]) + "\n"
ABSENT = None


def _write_private(base, name, content):
    if content is ABSENT:
        return str(base / ("absent-" + name))
    p = base / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def run(tree, *args, vocab=VOCAB_OK, deny=DENY_OK, flag=None):
    """Run the guard. `flag` None = publication mode on `tree`; `--tree`/`--publication` take `tree` as
    their directory; any other flag (`--selftest`) or "" (the staged check) runs with cwd = `tree`."""
    env = dict(os.environ)
    priv = tree.parent / (tree.name + "-private")
    priv.mkdir(exist_ok=True)
    env["FACTLATTICE_PUBLISH_VOCAB"] = _write_private(priv, "vocab.tsv", vocab)
    env["FACTLATTICE_DISCLOSURE_RULES"] = _write_private(priv, "deny.tsv", deny)
    flag = FLAG if flag is None else flag
    takes_dir = flag in ("--publication", "--tree")
    argv = [sys.executable, str(GUARD)] + ([flag] if flag else []) + ([str(tree)] if takes_dir else []) + list(args)
    return subprocess.run(argv, cwd=str(tree.parent if takes_dir else tree), env=env,
                          capture_output=True, text=True)


def out(r):
    return r.stdout + r.stderr


@pytest.fixture
def tree(tmp_path):
    t = tmp_path / "snap"
    (t / "src").mkdir(parents=True)
    (t / "docs" / "internal").mkdir(parents=True)
    (t / "src" / "ok.py").write_text("print('library loans')\n", encoding="utf-8")
    (t / "PUBLISH-EXCLUDE").write_text("docs/internal/   # build logs never cross\n", encoding="utf-8")
    return t


# --- T1a: a missing / empty / comment-only private file is red --------------------------------------
@pytest.mark.parametrize("label,kwargs,which", [
    ("vocabulary missing", dict(vocab=ABSENT), "vocabulary file"),
    ("denylist missing (the case --tree passes today)", dict(deny=ABSENT), "denylist file"),
    ("vocabulary present but empty", dict(vocab=""), "vocabulary file"),
    ("denylist present but empty", dict(deny=""), "denylist file"),
    ("vocabulary comment-only", dict(vocab="# nothing yet\n\n   \n# still nothing\n"), "vocabulary file"),
    ("denylist comment-only", dict(deny="# nothing yet\n\n"), "denylist file"),
])
def test_t1a_an_unusable_private_file_is_red_and_named(tree, label, kwargs, which):
    r = run(tree, **kwargs)
    assert r.returncode == 1, f"{label}: exit {r.returncode}\n{out(r)}"
    assert which in r.stdout, f"{label}: the message must name WHICH file\n{out(r)}"


def test_t1a_both_files_unusable_names_both(tree):
    r = run(tree, vocab=ABSENT, deny="")
    assert r.returncode == 1
    assert "vocabulary file" in r.stdout and "denylist file" in r.stdout


def test_t1a_a_refusal_still_prints_the_scope_line_and_writes_the_report(tree, tmp_path):
    rep = tmp_path / "r.json"
    r = run(tree, "--report", str(rep), deny=ABSENT)
    assert r.returncode == 1
    assert "publication scope:" in r.stdout and "denylist: NOT LOADED" in r.stdout, out(r)
    doc = json.loads(rep.read_text(encoding="utf-8"))
    assert doc["exit_code"] == 1 and doc["result"] == "refused" and doc["refusals"], doc


# --- T1b: a row that misses its own control is red --------------------------------------------------
@pytest.mark.parametrize("which", ["vocab", "deny"])
def test_t1b_a_row_that_misses_its_own_control_is_red_and_prints_neither_pattern_nor_control(tree, which):
    mute = T.join(["zorblaxpattern", "nothing to see in this control", "why", "mute-row"]) + "\n"
    kw = {"vocab": VOCAB_OK + mute} if which == "vocab" else {"deny": DENY_OK + mute}
    r = run(tree, **kw)
    assert r.returncode == 1, out(r)
    assert "mute-row" in r.stdout and "own control" in r.stdout, out(r)
    assert "zorblaxpattern" not in out(r) and "nothing to see in this control" not in out(r), out(r)


def test_t1b_a_malformed_row_is_red_and_prints_no_row_text(tree):
    r = run(tree, vocab=VOCAB_OK + "justonefield-secretword\n")
    assert r.returncode == 1, out(r)
    assert "malformed" in r.stdout and "line 3" in r.stdout, out(r)
    assert "secretword" not in out(r)


def test_t1b_a_bad_regex_is_red_and_prints_no_pattern(tree):
    r = run(tree, vocab=VOCAB_OK + T.join(["(unclosedsecret", "x", "why", "bad"]) + "\n")
    assert r.returncode == 1, out(r)
    assert "bad regex" in r.stdout and "unclosedsecret" not in out(r), out(r)


# --- T2: a planted term is red and unnamed in the log; green under an excluded path -----------------
def test_t2_a_planted_term_in_a_kept_file_is_red_and_the_term_is_not_printed(tree):
    (tree / "src" / "leak.py").write_text(f"x = 1\n# written for {TERM_V}\n", encoding="utf-8")
    r = run(tree)
    assert r.returncode == 1, out(r)
    assert re.search(r"src/leak\.py:2  \[vocab-vendor\]", r.stdout), out(r)
    assert TERM_V not in out(r), out(r)


def test_t2_a_hit_in_a_file_name_does_not_print_the_name_or_the_old_file_name_text(tree):
    (tree / "src" / f"{TERM_V}_notes.py").write_text("x = 1\n", encoding="utf-8")
    r = run(tree)
    assert r.returncode == 1, out(r)
    assert TERM_V not in out(r), "the matched part of a path must not be printed\n" + out(r)
    assert "(file name)" not in out(r)
    assert "[vocab-vendor]" in r.stdout


def test_t2_a_denylist_term_is_found_too_and_not_printed(tree):
    (tree / "src" / "c.py").write_text(f"# {TERM_D} asked for this\n", encoding="utf-8")
    r = run(tree)
    assert r.returncode == 1 and "src/c.py:1  [deny-customer]" in r.stdout, out(r)
    assert TERM_D not in out(r)


def test_t2_the_report_holds_path_line_and_rule_and_no_matched_text(tree, tmp_path):
    (tree / "src" / "leak.py").write_text(f"# written for {TERM_V} and gribble77\n", encoding="utf-8")
    (tree / "src" / f"{TERM_V}.txt").write_text("x\n", encoding="utf-8")
    rep = tmp_path / "rep.json"
    r = run(tree, "--report", str(rep))
    assert r.returncode == 1, out(r)
    text = rep.read_text(encoding="utf-8")
    for secret in (TERM_V, "gribble77", f"{TERM_V} inc"):
        assert secret not in text and secret not in out(r), secret
    doc = json.loads(text)
    assert doc["exit_code"] == 1
    assert {"path": "src/leak.py", "line": 1, "rule": "vocab-vendor"} in doc["findings"]
    assert all(set(f) <= {"path", "line", "rule", "source"} for f in doc["findings"])


def test_t2_the_same_term_only_under_an_excluded_path_is_green(tree):
    (tree / "docs" / "internal" / "log.md").write_text(f"# written for {TERM_V}\n", encoding="utf-8")
    r = run(tree)
    assert r.returncode == 0, out(r)
    assert "RESULT: green" in r.stdout


def test_t2_the_exclusion_is_by_path_prefix_not_substring(tree):
    (tree / "docs" / "internal-notes").mkdir()
    (tree / "docs" / "internal-notes" / "x.md").write_text(f"{TERM_V}\n", encoding="utf-8")
    r = run(tree)
    assert r.returncode == 1 and "docs/internal-notes/x.md:1" in r.stdout, out(r)


# --- T3: a clean tree is green and the scope line is exact -------------------------------------------
def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def snapshot(tmp_path):
    """A committed fixture repository (the 'trunk') and a snapshot directory built from it by the TEST:
    one exempt file (tools/pre-commit), one excluded directory, one mapped file."""
    trunk = tmp_path / "trunk"
    files = {
        "README.md": "a library\n",
        "src/a.py": "print('hello')\n",
        "src/b.py": "print('bye')\n",
        "tools/pre-commit": "#!/bin/sh\nexit 0\n",                       # the scanner's exempt file
        "docs/internal/log.md": f"a build log naming {TERM_V}\n",        # excluded: never crosses
        "docs/internal/more.md": "more\n",
        "publish/github/workflows/ci.yml": "name: ci\non: [push]\n",       # the map's source folder
        "PUBLISH-EXCLUDE": "docs/internal/   # build logs never cross\n",
    }
    for rel, body in files.items():
        p = trunk / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    _git(trunk, "init", "-q")
    _git(trunk, "config", "user.email", "t@example.com")
    _git(trunk, "config", "user.name", "t")
    _git(trunk, "add", "-A")
    _git(trunk, "commit", "-q", "-m", "fixture")

    # the independent count: from `git ls-tree` of the revision, by the spec's two equations
    export = [x for x in _git(trunk, "ls-tree", "-r", "--name-only", "HEAD").splitlines() if x]
    excluded_prefix, map_src, map_dst = "docs/internal/", "publish/github/workflows/", ".github/workflows/"
    exempt = ["tools/pre-commit"]
    kept = [x for x in export if not x.startswith(excluded_prefix) and not x.startswith(map_src)]
    placed = [map_dst + x[len(map_src):] for x in export if x.startswith(map_src)]
    shipped_expected = len(kept) + len(placed)          # export - excluded - map source + map targets
    scanned_expected = shipped_expected - len(exempt)   # shipped - exempt

    # the snapshot: the export with the map applied (the excluded files are left in place, so that the
    # scanner's own PUBLISH-EXCLUDE handling is what keeps them out of the count)
    snap = tmp_path / "snapshot"
    snap.mkdir()
    for rel in export:
        dst = snap / ((map_dst + rel[len(map_src):]) if rel.startswith(map_src) else rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(trunk / rel, dst)
    manifest = tmp_path / "map.tsv"
    manifest.write_text(".github/workflows/ci.yml\tpublish/github/workflows/ci.yml\n", encoding="utf-8")
    return dict(snap=snap, manifest=manifest, shipped=shipped_expected, scanned=scanned_expected,
                exempt=exempt)


def test_t3_a_clean_tree_is_green(tree):
    r = run(tree)
    assert r.returncode == 0, out(r)


def test_t3_the_scope_line_carries_every_field_and_the_counts_equal_the_two_equations(snapshot, tmp_path):
    rep = tmp_path / "rep.json"
    r = run(snapshot["snap"], "--map-manifest", str(snapshot["manifest"]), "--report", str(rep))
    assert r.returncode == 0, out(r)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("publication scope:")]
    assert len(lines) == 1, "the scope line is ONE line\n" + out(r)
    scope = lines[0]
    # counts computed by the test from git ls-tree (see the fixture), not by the guard
    assert int(re.search(r"files-shipped=(\d+)", scope).group(1)) == snapshot["shipped"], scope
    assert int(re.search(r"files-scanned=(\d+)", scope).group(1)) == snapshot["scanned"], scope
    assert "exclude-entries-applied=1" in scope, scope
    assert "mapped-files=1" in scope, scope
    # the exempt file is named by path, in the 'shipped but not scanned' field
    m = re.search(r"shipped-not-scanned[^=]*=\[([^\]]*)\]", scope)
    assert m and snapshot["exempt"][0] in m.group(1), scope
    # each private file: rule count, row count and the sha256 of its bytes (computed here)
    priv = tmp_path / "snapshot-private"
    for key, name, rules, rows in (("vocabulary", "vocab.tsv", 2, 2), ("denylist", "deny.tsv", 1, 1)):
        sha = hashlib.sha256((priv / name).read_bytes()).hexdigest()
        assert f"{key}: rules={rules} rows={rows} sha256={sha}" in scope, (key, scope)
    # the report carries the same fields
    doc = json.loads(rep.read_text(encoding="utf-8"))
    assert doc["exit_code"] == 0 and doc["scope_line"] == scope
    assert doc["scope"]["shipped"] == snapshot["shipped"] and doc["scope"]["scanned"] == snapshot["scanned"]
    assert doc["scope"]["private_files"]["vocabulary"]["sha256"] == hashlib.sha256(
        (priv / "vocab.tsv").read_bytes()).hexdigest()


def test_t3_the_result_line_is_a_tripwire_statement_and_never_claims_nothing_private_remains(snapshot):
    r = run(snapshot["snap"], "--map-manifest", str(snapshot["manifest"]))
    assert r.returncode == 0, out(r)
    result = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT:")]
    assert len(result) == 1, out(r)
    low = result[0].lower()
    assert "lexical tripwire over the named vocabulary" in low
    for claim in ("0 findings", "nothing private", "no private", "clean", "safe to publish", "all clear"):
        assert claim not in low, (claim, result[0])
    assert "0 findings" not in out(r).lower()


def test_t3_the_exempt_file_is_not_scanned_even_when_it_holds_a_term(snapshot):
    (snapshot["snap"] / "tools" / "pre-commit").write_text(f"# {TERM_V}\n", encoding="utf-8")
    r = run(snapshot["snap"], "--map-manifest", str(snapshot["manifest"]))
    assert r.returncode == 0, out(r)
    assert "tools/pre-commit" in r.stdout


# --- the map manifest -------------------------------------------------------------------------------
def test_manifest_a_finding_in_a_mapped_target_names_target_and_source(snapshot):
    ci = snapshot["snap"] / ".github" / "workflows" / "ci.yml"
    ci.write_text(f"name: ci\n# {TERM_V}\n", encoding="utf-8")
    r = run(snapshot["snap"], "--map-manifest", str(snapshot["manifest"]))
    assert r.returncode == 1, out(r)
    line = next(ln for ln in r.stdout.splitlines() if ".github/workflows/ci.yml:2" in ln)
    assert "[vocab-vendor]" in line and "publish/github/workflows/ci.yml" in line, line
    assert TERM_V not in out(r)


def test_manifest_without_it_the_mapped_count_is_zero_and_a_finding_has_no_source(snapshot):
    (snapshot["snap"] / ".github" / "workflows" / "ci.yml").write_text(f"{TERM_V}\n", encoding="utf-8")
    r = run(snapshot["snap"])
    assert r.returncode == 1 and "mapped-files=0" in r.stdout, out(r)
    assert "publish/github" not in r.stdout


def test_manifest_naming_a_missing_target_refuses(snapshot, tmp_path):
    bad = tmp_path / "bad.tsv"
    bad.write_text(".github/workflows/nope.yml\tpublish/github/workflows/nope.yml\n", encoding="utf-8")
    r = run(snapshot["snap"], "--map-manifest", str(bad))
    assert r.returncode == 1, out(r)
    assert "map manifest" in r.stdout and "not a file in the tree" in r.stdout, out(r)
    assert "RESULT: REFUSED" in r.stdout


def test_manifest_a_malformed_line_refuses(snapshot, tmp_path):
    bad = tmp_path / "bad.tsv"
    bad.write_text("only-one-field\n", encoding="utf-8")
    r = run(snapshot["snap"], "--map-manifest", str(bad))
    assert r.returncode == 1 and "malformed" in r.stdout, out(r)


def test_manifest_a_missing_manifest_file_refuses(snapshot, tmp_path):
    r = run(snapshot["snap"], "--map-manifest", str(tmp_path / "no-such.tsv"))
    assert r.returncode == 1 and "map manifest" in r.stdout, out(r)


def test_publication_cannot_be_combined_with_another_mode(tree):
    r = run(tree, "--selftest")
    assert r.returncode != 0 and "mode of its own" in out(r), out(r)


# --- T6 and R7: an unconfigured lenient run never looks like a clean one -----------------------------
@pytest.fixture
def staged_repo(tmp_path):
    t = tmp_path / "g"
    t.mkdir()
    _git(t, "init", "-q")
    _git(t, "config", "user.email", "t@example.com")
    _git(t, "config", "user.name", "t")
    (t / "a.py").write_text("print('library loans')\n", encoding="utf-8")
    _git(t, "add", "a.py")
    return t


def test_t6_an_unconfigured_staged_run_and_a_configured_one_differ(staged_repo):
    unconfigured = run(staged_repo, flag="", vocab=ABSENT, deny=ABSENT)
    configured = run(staged_repo, flag="")
    assert unconfigured.returncode == 0 and configured.returncode == 0, out(unconfigured) + out(configured)
    assert out(unconfigured) != out(configured)
    # the configured run found nothing and says so ...
    assert "0 findings" in configured.stdout
    # ... the unconfigured one does not; it says which files were not loaded and that their rules were not checked
    u = unconfigured.stdout
    assert "0 findings" not in u, u
    assert "denylist" in u and "publish vocabulary" in u and "NOT LOADED" in u, u
    assert "rules were NOT checked" in u, u
    assert "UNCONFIGURED RUN, NOT A CLEAN RESULT" in u


@pytest.mark.parametrize("kw,which", [
    (dict(deny=""), "denylist"),
    (dict(vocab="# nothing\n"), "publish vocabulary"),
    (dict(deny=ABSENT), "denylist"),
])
def test_r7_a_missing_or_empty_private_file_alone_is_never_a_zero_findings_result(staged_repo, kw, which):
    r = run(staged_repo, flag="", **kw)
    assert r.returncode == 0, out(r)
    assert "0 findings" not in r.stdout, r.stdout
    assert which in r.stdout and "rules were NOT checked" in r.stdout, r.stdout


def test_r7_tree_mode_with_the_denylist_missing_or_empty_is_not_a_zero_findings_result(tree):
    for kw in (dict(deny=ABSENT), dict(deny="")):
        r = run(tree, flag="--tree", **kw)
        assert r.returncode == 0, out(r)
        assert "0 findings" not in r.stdout, r.stdout
        assert "denylist" in r.stdout and "rules were NOT checked" in r.stdout, r.stdout
    configured = run(tree, flag="--tree")
    assert configured.returncode == 0 and "0 findings" in configured.stdout, out(configured)


def test_r7_selftest_with_an_empty_private_file_says_it_was_not_loaded(tree):
    r = run(tree, flag="--selftest", deny="")
    assert "denylist" in r.stdout and "rules were NOT checked" in r.stdout, r.stdout
    assert r.returncode == 2, out(r)           # unconfigured, as for a missing file
    ok = run(tree, flag="--selftest")
    assert ok.returncode == 0 and "The matchers demonstrably RAN" in ok.stdout, out(ok)


def test_r7_a_finding_in_an_unconfigured_run_is_still_reported(staged_repo):
    (staged_repo / "b.py").write_text(f"# {TERM_V}\n", encoding="utf-8")
    _git(staged_repo, "add", "b.py")
    r = run(staged_repo, flag="", deny=ABSENT)
    assert r.returncode == 1 and "b.py:1" in r.stdout, out(r)


# =====================================================================================================
# Fix round 1 (review of 8015e71): F1 F2 F3 F4 F5 F6 F7 F9
# =====================================================================================================
def _run_env(args, env_extra, cwd):
    env = {k: v for k, v in os.environ.items() if not k.startswith("FACTLATTICE_")}
    env.update(env_extra)
    return subprocess.run([sys.executable, str(GUARD), *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True)


# --- F1: a tree the gate never read is never green ---------------------------------------------------
def test_f1_a_directory_that_does_not_exist_is_refused(tmp_path):
    r = run(tmp_path / "no-such-dir")
    assert r.returncode == 1, out(r)
    assert "does not exist" in r.stdout and "RESULT: REFUSED" in r.stdout, out(r)
    assert "RESULT: green" not in r.stdout


def test_f1_a_path_that_is_a_file_is_refused(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x\n", encoding="utf-8")
    r = run(f)
    assert r.returncode == 1 and "not a directory" in r.stdout, out(r)


def test_f1_an_empty_directory_is_refused_for_zero_files(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    r = run(d)
    assert r.returncode == 1 and "zero files shipped" in r.stdout, out(r)
    assert "RESULT: green" not in r.stdout


def test_f1_a_directory_whose_every_file_is_excluded_is_refused(tmp_path):
    d = tmp_path / "allx"
    (d / "docs").mkdir(parents=True)
    (d / "docs" / "a.md").write_text("x\n", encoding="utf-8")
    (d / "PUBLISH-EXCLUDE").write_text("docs/   # never crosses\nPUBLISH-EXCLUDE   # nor this\n", encoding="utf-8")
    r = run(d)
    assert r.returncode == 1 and "zero files shipped" in r.stdout, out(r)


def test_f1_a_directory_holding_only_the_exempt_files_is_refused_for_zero_scanned(tmp_path):
    d = tmp_path / "onlyexempt"
    (d / "tools").mkdir(parents=True)
    (d / "tools" / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    r = run(d)
    assert r.returncode == 1 and "zero files scanned" in r.stdout, out(r)


# --- F2: an unreadable PUBLISH-EXCLUDE is a named refusal, not a traceback ---------------------------
def test_f2_an_undecodable_publish_exclude_is_a_named_refusal_and_the_scope_line_still_prints(tree):
    (tree / "PUBLISH-EXCLUDE").write_bytes(b"docs/   # why\n\xff\xfe\x00bad\n")
    r = run(tree)
    assert r.returncode == 1, out(r)
    assert "Traceback" not in out(r), out(r)
    assert "PUBLISH-EXCLUDE: cannot be read" in r.stdout, out(r)
    assert "publication scope:" in r.stdout and "RESULT: REFUSED" in r.stdout, out(r)


# --- F3: the exempt paths in the scope line are masked like any path ---------------------------------
def test_f3_the_exempt_paths_in_the_scope_line_are_masked_when_a_rule_matches_them(tree):
    (tree / "tools").mkdir()
    (tree / "tools" / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    vocab = VOCAB_OK + T.join(["commit", "pre-commit", "a word that is in an exempt path", "commit-word"]) + "\n"
    r = run(tree, vocab=vocab)
    assert r.returncode == 0, out(r)
    scope = next(ln for ln in r.stdout.splitlines() if ln.startswith("publication scope:"))
    assert "tools/pre-******" in scope and "commit" not in scope.split("shipped-not-scanned")[1].split(";")[0], scope


# --- F4: the gate scans the directory on disk, never the git index; symlinks ship -------------------
def test_f4_an_untracked_file_in_a_git_toplevel_is_still_scanned(tmp_path):
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-q", "-m", "c")
    (repo / "untracked.py").write_text(f"# {TERM_V}\n", encoding="utf-8")      # on disk, not in the index
    r = run(repo)
    assert r.returncode == 1 and "untracked.py:1  [vocab-vendor]" in r.stdout, out(r)
    assert "files-shipped=2;" in r.stdout and "files-scanned=2;" in r.stdout, out(r)


def test_f4_the_git_directory_itself_is_not_scanned_or_counted(tmp_path):
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / ".git" / "note").write_text(f"{TERM_V}\n", encoding="utf-8")
    r = run(repo)
    assert r.returncode == 0 and "files-shipped=1;" in r.stdout, out(r)


def test_f4_a_dangling_symlink_is_counted_and_its_target_string_is_scanned(tree):
    os.symlink(f"/nowhere/{TERM_V}/target", tree / "src" / "dangling")
    os.symlink("ok.py", tree / "src" / "fine")
    r = run(tree)
    assert r.returncode == 1, out(r)
    assert "src/dangling:1  [vocab-vendor]" in r.stdout, out(r)
    assert TERM_V not in out(r)
    # ok.py, PUBLISH-EXCLUDE, dangling, fine (docs/internal/ is empty): the links count as shipped files
    assert "files-shipped=4;" in r.stdout and "files-scanned=4;" in r.stdout, out(r)


def test_f4_a_symlink_to_a_directory_is_one_shipped_file_and_is_not_followed(tree, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text(f"{TERM_V}\n", encoding="utf-8")
    # A RELATIVE link target: an absolute one is scanned as text (a symlink ships as its target), and where the temp dir sits under a home folder (a CI runner's
    # RUNNER_TEMP) the absolute-home-path rule would fire on the test's own scratch path, not on anything the link points at.
    os.symlink(os.path.relpath(outside, tree / "src"), tree / "src" / "linkdir")
    r = run(tree)
    assert r.returncode == 0, out(r)
    assert "files-shipped=3;" in r.stdout, out(r)


# --- F5: the exemption is two EXACT paths -----------------------------------------------------------
@pytest.mark.parametrize("name", ["tools/disclosure_guard.py.bak-x", "tools/pre-commit-x", "tools/pre-commit.d/hook"])
def test_f5_a_path_that_merely_starts_like_an_exempt_path_is_scanned(tree, name):
    p = tree / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# {TERM_V}\n", encoding="utf-8")
    r = run(tree)
    assert r.returncode == 1 and f"{name}:1  [vocab-vendor]" in r.stdout, out(r)


def test_f5_the_two_exact_paths_stay_exempt(tree):
    for name in ("tools/disclosure_guard.py", "tools/pre-commit"):
        p = tree / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {TERM_V}\n", encoding="utf-8")
    r = run(tree)
    assert r.returncode == 0, out(r)
    assert "tools/disclosure_guard.py, tools/pre-commit" in r.stdout


# --- F6: two private files that are one file are refused ---------------------------------------------
def test_f6_identical_content_in_the_two_private_files_is_refused(tree):
    r = run(tree, vocab=VOCAB_OK, deny=VOCAB_OK)
    assert r.returncode == 1 and "identical content" in r.stdout, out(r)


def test_f6_both_environment_variables_naming_the_same_path_is_refused(tree, tmp_path):
    one = tmp_path / "one.tsv"
    one.write_text(VOCAB_OK, encoding="utf-8")
    link = tmp_path / "link.tsv"
    os.symlink(one, link)
    for other in (one, link):                       # the same path, and a symlink to it
        r = _run_env(["--publication", str(tree)],
                     {"FACTLATTICE_PUBLISH_VOCAB": str(one), "FACTLATTICE_DISCLOSURE_RULES": str(other)}, tmp_path)
        assert r.returncode == 1 and "same file" in r.stdout, out(r)


# --- F7: the tripwire wording is on every result line ------------------------------------------------
def test_f7_the_red_and_refused_result_lines_carry_the_tripwire_wording(tree):
    (tree / "src" / "leak.py").write_text(f"# {TERM_V}\n", encoding="utf-8")
    red = run(tree)
    refused = run(tree, deny=ABSENT)
    for r, word in ((red, "RED"), (refused, "REFUSED")):
        line = next(ln for ln in r.stdout.splitlines() if ln.startswith(f"RESULT: {word}"))
        assert "lexical tripwire over the named vocabulary" in line, line
        assert "nothing private" not in line.replace("not a statement that nothing private", "")


# --- F9: the result line is printed after the report is written -------------------------------------
def test_f9_a_failed_report_write_ends_refused_names_the_failure_and_never_prints_green(tree, tmp_path):
    r = run(tree, "--report", str(tmp_path / "no-such-dir" / "r.json"))
    assert r.returncode == 1, out(r)
    assert "report: cannot be written" in r.stdout, out(r)
    assert "RESULT: green" not in r.stdout and "RESULT: REFUSED" in r.stdout, out(r)
    lines = r.stdout.splitlines()
    assert max(i for i, ln in enumerate(lines) if "cannot be written" in ln) < \
        next(i for i, ln in enumerate(lines) if ln.startswith("RESULT:")), r.stdout


def test_f9_a_failed_report_write_on_a_red_run_stays_red_and_names_the_failure(tree, tmp_path):
    (tree / "src" / "leak.py").write_text(f"# {TERM_V}\n", encoding="utf-8")
    r = run(tree, "--report", str(tmp_path / "no-such-dir" / "r.json"))
    assert r.returncode == 1 and "RESULT: RED" in r.stdout and "report: cannot be written" in r.stdout, out(r)
