"""The three other read-only commands of `python -m factlattice_engine.reviewer` through a real subprocess
(resolve-output has its own tests): new-key, check-extension-paths, self-gate. Each exists so that a decision the
procedure depends on is code; each must be read-only and must FAIL when it should."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import pytest

from factlattice_engine import reviewer
from tests.test_reviewer_review_document import KEY3, NOW_KEY, world  # noqa: F401  (the fixture builds real reviews)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cli(tmp_path, *args):
    st = tmp_path / "cli-state"
    st.mkdir(exist_ok=True)
    reg = tmp_path / "empty-registry.json"
    reg.write_text("{}")
    env = {**os.environ, "PYTHONPATH": ROOT, "FACTLATTICE_CORPUS_CONFIG": str(reg), "FACTLATTICE_STATE_DIR": str(st),
           "LTS_ENGINE_STATE_DIR": str(st), "FACTLATTICE_BACKUP_DIR": str(st / "bk")}
    return subprocess.run([sys.executable, "-m", "factlattice_engine.reviewer", *args], capture_output=True, text=True,
                          env=env, timeout=180)


def test_new_key_prints_a_valid_key_and_two_calls_differ(tmp_path):
    a = cli(tmp_path, "new-key", "--corpus", "Some Corpus")
    b = cli(tmp_path, "new-key", "--corpus", "Some Corpus")
    assert a.returncode == 0 and reviewer.REVIEW_KEY_RE.match(a.stdout.strip()) and a.stdout.startswith("SOME-CORPUS-")
    assert a.stdout != b.stdout
    assert cli(tmp_path, "new-key").stdout.startswith("NO-CORPUS-")


def test_check_extension_paths_exit_codes(tmp_path):
    doc = tmp_path / "d.lts"
    doc.write_text("# d [SPEC:D]\n")
    other = tmp_path / "e.lts"
    other.write_text("# e [SPEC:E]\n")
    ok = cli(tmp_path, "check-extension-paths", "--document", str(doc), "--extension", str(other))
    assert ok.returncode == 0 and json.loads(ok.stdout) == {"findings": []}
    bad = cli(tmp_path, "check-extension-paths", "--document", str(doc), "--extension", str(other),
              "--extension", str(tmp_path / "x" / ".." / "d.lts"))
    assert bad.returncode == 1 and json.loads(bad.stdout)["terminal_only"]["findings"][0]["kind"] == "extension_is_the_reviewed_document"


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def test_self_gate_passes_a_complete_review_and_fails_a_stripped_one_and_never_writes(world):  # noqa: F811
    tmp = world["tmp"]
    good, unrev = world["pass"], world["unrev"]
    before = (_sha(good), _sha(unrev))
    r = cli(tmp, "self-gate", "--file", str(good))
    out = json.loads(r.stdout)
    assert r.returncode == 0 and out["passed"] is True, out
    assert cli(tmp, "self-gate", "--file", str(unrev)).returncode == 0
    from tests.test_reviewer_review_document import write_review
    bad = write_review(world["folder"], "REVIEWED-20261007T150000Z-0C0C", drop=("route",), complete=False)
    r2 = cli(tmp, "self-gate", "--file", str(bad))
    out2 = json.loads(r2.stdout)
    assert r2.returncode == 1 and out2["passed"] is False
    assert [c["name"] for c in out2["checks"] if not c["ok"]] == ["no-missing-required"]
    assert (_sha(good), _sha(unrev)) == before, "the gate wrote to a file it read"


def test_self_gate_of_a_missing_file_fails_closed(tmp_path):
    r = cli(tmp_path, "self-gate", "--file", str(tmp_path / "REVIEW-NOPE.lts"))
    assert r.returncode in (1, 2) and json.loads(r.stdout)["passed"] is False
