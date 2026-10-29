"""disclosure_guard --tree: scan every tracked-or-present file not named in PUBLISH-EXCLUDE.

The fixture vocabulary is synthetic (an imaginary vendor, `quillworks`). Nothing here names a real
deployment, so this file is itself publishable and passes the guard it tests.

Each test builds a throwaway tree and runs the guard as a subprocess with BOTH rule-file locations
pointed at temp files, so the maintainer's real private files can never leak into (or mask) a result.
"""
import os
import pathlib
import subprocess
import sys

import pytest

GUARD = pathlib.Path(__file__).resolve().parents[1] / "tools" / "disclosure_guard.py"
T = "\t"
VOCAB_ROW = T.join([r"quillworks", "built by quillworks inc", "a vendor name; use a neutral one", "vendor-name"])


def run(tree, *args, vocab=VOCAB_ROW, denylist=None, vocab_name="vocab.tsv"):
    env = dict(os.environ)
    env.pop("FACTLATTICE_PUBLISH_VOCAB", None)
    env.pop("FACTLATTICE_DISCLOSURE_RULES", None)
    scratch = tree.parent / (tree.name + "-rules")
    scratch.mkdir(exist_ok=True)
    if vocab is not None:
        vp = scratch / vocab_name
        vp.write_text(vocab + "\n", encoding="utf-8")
        env["FACTLATTICE_PUBLISH_VOCAB"] = str(vp)
    else:
        env["FACTLATTICE_PUBLISH_VOCAB"] = str(scratch / "does-not-exist.tsv")
    dp = scratch / "deny.tsv"
    dp.write_text((denylist or "") , encoding="utf-8")
    env["FACTLATTICE_DISCLOSURE_RULES"] = str(dp)
    return subprocess.run([sys.executable, str(GUARD), *args], cwd=str(tree), env=env,
                          capture_output=True, text=True)


@pytest.fixture
def tree(tmp_path):
    t = tmp_path / "repo"
    (t / "src").mkdir(parents=True)
    (t / "docs" / "internal").mkdir(parents=True)
    (t / "src" / "ok.py").write_text("print('library loans')\n", encoding="utf-8")
    (t / "PUBLISH-EXCLUDE").write_text("docs/internal/   # build logs never cross\n", encoding="utf-8")
    return t


def test_clean_tree_passes(tree):
    r = run(tree, "--tree", ".")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 findings" in r.stdout


def test_planted_hit_in_a_non_excluded_file_fails(tree):
    (tree / "src" / "leak.py").write_text("# written for quillworks\n", encoding="utf-8")
    r = run(tree, "--tree", ".")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "src/leak.py:1" in r.stdout and "vendor-name" in r.stdout


def test_the_same_hit_in_an_excluded_path_passes(tree):
    (tree / "docs" / "internal" / "log.md").write_text("# written for quillworks\n", encoding="utf-8")
    r = run(tree, "--tree", ".")
    assert r.returncode == 0, r.stdout + r.stderr


def test_exclusion_is_by_path_prefix_not_substring(tree):
    # `docs/internal/` must not exempt a sibling that merely contains the same words
    (tree / "docs" / "internal-notes").mkdir()
    (tree / "docs" / "internal-notes" / "x.md").write_text("quillworks\n", encoding="utf-8")
    r = run(tree, "--tree", ".")
    assert r.returncode == 1 and "docs/internal-notes/x.md" in r.stdout


def test_a_hit_in_a_file_name_is_found(tree):
    (tree / "src" / "quillworks_notes.py").write_text("x = 1\n", encoding="utf-8")
    r = run(tree, "--tree", ".")
    assert r.returncode == 1 and "quillworks_notes.py" in r.stdout


def test_a_pending_entry_does_not_exclude(tree):
    (tree / "PUBLISH-EXCLUDE").write_text(
        "docs/internal/   # build logs never cross\n"
        "#PENDING src/unfinished/   # maintainer decides; until then it is scanned\n", encoding="utf-8")
    (tree / "src" / "unfinished").mkdir()
    (tree / "src" / "unfinished" / "m.py").write_text("# quillworks\n", encoding="utf-8")
    r = run(tree, "--tree", ".")
    assert r.returncode == 1 and "src/unfinished/m.py" in r.stdout


def test_an_exclude_line_without_a_comment_is_an_integrity_failure(tree):
    (tree / "PUBLISH-EXCLUDE").write_text("docs/internal/\n", encoding="utf-8")
    r = run(tree, "--tree", ".")
    assert r.returncode == 1 and "comment" in r.stdout.lower()


def test_no_vocabulary_is_a_failure_in_tree_mode_not_a_pass(tree):
    r = run(tree, "--tree", ".", vocab=None)
    assert r.returncode != 0, r.stdout
    assert "vocabulary" in r.stdout.lower()


def test_a_vocabulary_row_that_misses_its_own_control_is_refused(tree):
    bad = T.join([r"quillworks", "nothing to see", "why", "mute-row"])
    r = run(tree, "--tree", ".", vocab=bad)
    assert r.returncode == 1 and "mute-row" in r.stdout


def test_selftest_runs_every_vocabulary_row_against_its_control(tree):
    r = run(tree, "--selftest")
    assert "caught [vendor-name] on its own control" in r.stdout, r.stdout
    assert r.returncode in (0, 2), r.stdout


def test_selftest_proves_tree_mode_fires_and_honours_exclusion(tree):
    # the built-in tree negative control: a planted file is caught, the same file under an excluded prefix is not
    r = run(tree, "--selftest")
    assert "tree-mode control" in r.stdout, r.stdout


def test_vocabulary_rows_also_gate_the_staged_check(tmp_path):
    t = tmp_path / "g"
    t.mkdir()
    env = {**os.environ}
    subprocess.run(["git", "init", "-q"], cwd=t, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=t, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=t, check=True)
    (t / "a.py").write_text("# quillworks\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=t, check=True)
    r = run(t)
    assert r.returncode == 1 and "a.py" in r.stdout
