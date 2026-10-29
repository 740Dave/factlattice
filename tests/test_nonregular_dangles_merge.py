"""The reviewer `dangles` verb answers the coded corpus-file stop (rebase onto 88aa5d6; the Opus Diamond note of edb399f:
"the dangles verb needs `_corpus_file_stop` at rebase").

The verb landed on main (corpus-scoped dangles, `--own-corpus`) after this branch was cut. A corpus whose build failed on a corpus-file
fault is not served, so without the stop the verb said `corpus-not-served` (--corpus), or `not applicable` with exit 0
(--own-corpus: the failed corpus is not a "served" container, so the document looked like it belonged to no corpus), or
`lint-incomplete`. Each is a misreport of a corpus-file fault. Special files are planted only under pytest's tmp_path.
"""
from __future__ import annotations

import json
import os

import pytest

from factlattice.hashing import CORPUS_FILE_CODES
from factlattice_engine.reviewer.__main__ import CORPUS_FILE_EXPLANATIONS
from tests.test_nonregular_file_in_root import CLEAN, GOOD, KINDS, plant, sub_run, ws  # noqa: F401  (ws: the registry fixture)

KINDS_USED = ["fifo", "broken-symlink", "unreadable"]


def run(ws, *args):
    cp, _ = sub_run(["-m", "factlattice_engine.reviewer", "dangles", *args], ws["env"])
    return cp, (json.loads(cp.stdout) if cp.stdout.strip() else None)


def coded(ws, cp, body, kind):
    assert cp.returncode == 2, (cp.stdout, cp.stderr[-1000:])
    assert body["reason"] == KINDS[kind], body
    assert body["error_class"] == "CorpusFileError"
    assert body["explanation"] and str(ws["tmp"]) not in cp.stdout
    assert "registry-not-found" not in cp.stdout and "corpus-not-served" not in cp.stdout


@pytest.mark.parametrize("kind", KINDS_USED)
def test_dangles_corpus_that_failed_to_build_is_the_coded_stop(ws, kind):
    plant(kind, ws["bad"] / "sub")
    cp, body = run(ws, "--corpus", "badcorp", "--document", str(ws["bad"] / "regular.lts"))
    coded(ws, cp, body, kind)


@pytest.mark.parametrize("kind", KINDS_USED)
def test_dangles_document_in_a_failed_corpus_is_the_coded_stop(ws, kind):
    """The reviewed corpus is fine; the DOCUMENT sits in the corpus that failed."""
    plant(kind, ws["bad"] / "sub")
    cp, body = run(ws, "--corpus", "cleancorp", "--document", str(ws["bad"] / "regular.lts"))
    coded(ws, cp, body, kind)


@pytest.mark.parametrize("kind", KINDS_USED)
def test_dangles_own_corpus_in_a_failed_corpus_is_the_coded_stop_not_not_applicable(ws, kind):
    plant(kind, ws["bad"] / "sub")
    cp, body = run(ws, "--own-corpus", "--document", str(ws["bad"] / "regular.lts"))
    coded(ws, cp, body, kind)


def test_dangles_own_corpus_missing_document_in_a_failed_corpus_is_the_coded_stop(ws):
    """The corpus-file stop precedes the file check (the ambiguity decision still precedes both)."""
    plant("fifo", ws["bad"] / "sub")
    cp, body = run(ws, "--own-corpus", "--document", str(ws["bad"] / "nope.lts"))
    coded(ws, cp, body, "fifo")


def test_dangles_positive_control_a_healthy_corpus_beside_a_failed_one_still_answers(ws):
    """The stop must not fire for a document outside the failed corpus: the clean corpus's own document is linted."""
    plant("fifo", ws["bad"] / "sub")
    cp, body = run(ws, "--corpus", "cleancorp", "--document", str(ws["clean"] / "c.lts"))
    assert cp.returncode == 0, (cp.stdout, cp.stderr[-1000:])
    assert body["complete"] is True and body["true_dangle_count"] == 0 and "CorpusFileError" not in cp.stdout
    cp, body = run(ws, "--own-corpus", "--document", str(ws["clean"] / "c.lts"))
    assert cp.returncode == 0 and body["own_corpus"]["applicable"] is False     # no profile: the pre-existing answer, unchanged


def test_dangles_corpus_dir_unreadable_is_coded_in_all_three_modes(ws):
    """P3-1: a directory the process may not list is `corpus-dir-unreadable` (not in KINDS_USED: it is a directory, not a planted file)."""
    if os.geteuid() == 0:
        pytest.skip("running as root: chmod 0 does not make a directory unlistable")
    locked = ws["bad"] / "locked"
    locked.mkdir()
    (locked / "x.lts").write_text(CLEAN)
    locked.chmod(0)
    try:
        for args in (["--corpus", "badcorp"], ["--corpus", "cleancorp"], ["--own-corpus"]):
            cp, body = run(ws, *args, "--document", str(ws["bad"] / "regular.lts"))
            assert cp.returncode == 2 and body["reason"] == "corpus-dir-unreadable", (args, cp.stdout, cp.stderr[-500:])
            assert body["error_class"] == "CorpusFileError" and str(ws["tmp"]) not in cp.stdout
    finally:
        locked.chmod(0o755)


def test_every_corpus_file_code_the_engine_can_emit_has_an_explanation():
    """P3-1: a code without an explanation makes `_corpus_file_stop` return None, and the verb would lint the document as if the
    corpus were fine. The engine's code set is imported, never hand-listed."""
    assert set(CORPUS_FILE_CODES) == set(CORPUS_FILE_EXPLANATIONS)


def test_dangles_own_corpus_ambiguity_is_decided_before_the_corpus_file_stop(ws):
    """P2-3 (the Diamond's probe P-AMB): F failed (a FIFO in F/sub) and contains F/x; two SERVED corpora are both rooted at F/x.
    The ambiguity decision comes first, for an existing and for a missing document. Moving the stop ahead of it turns both into
    the corpus-file code (mutant `own-before-amb`)."""
    F = ws["base"] / "F"
    x = F / "x"
    x.mkdir(parents=True)
    (F / "sub").mkdir()
    (x / "doc.lts").write_text(CLEAN)
    (F / "regular.lts").write_text(GOOD)
    plant("fifo", F / "sub")
    reg = ws["base"] / "amb-reg.json"
    reg.write_text(json.dumps({
        "acorp": {"roots": [{"path": str(x), "area": "specs"}], "domain_suffix": "p.a"},
        "bcorp": {"roots": [{"path": str(x), "area": "decisions"}], "domain_suffix": "p.bb"},
        "fcorp": {"roots": [{"path": str(F), "area": "specs"}], "domain_suffix": "p.f"},
    }))
    amb = {**ws, "env": {**ws["env"], "FACTLATTICE_CORPUS_CONFIG": str(reg)}}
    for doc in ("doc.lts", "nope.lts"):
        cp, body = run(amb, "--own-corpus", "--document", str(x / doc))
        assert cp.returncode == 2 and body["reason"] == "own-corpus-ambiguous", (doc, cp.stdout, cp.stderr[-500:])
