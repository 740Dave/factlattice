"""tools/retitle_gated.py: a gated RETITLE of one node's heading text through the ledgered
write_whole_file, until the engineer's `retitle_node` verb exists (design note v1.1 §1).

The gate: exactly one line differs (that node's heading line); the parsed node-id list, every
node's properties, every other node's title and the edge list are identical; the target's title
parses to exactly the requested text. Dry run is the default; --apply writes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "retitle_gated.py"
sys.path.insert(0, str(REPO / "tools"))

DOC = (
    "# Doc [DOC-A]\n\n"
    "## Old title, neither of which contains biometric data [N-1]\n\n"
    "- kind: claim\n- see-also: [N-2]\n\nSome prose about N-1.\n\n"
    "### [SPEC:N-2] An id-first heading\n\n- kind: other\n"
)


@pytest.fixture()
def f(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    p = tmp_path / "corpus" / "d.lts"
    p.parent.mkdir()
    p.write_text(DOC, encoding="utf-8")
    return p


def _run(*args, env=None):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def test_dry_run_is_the_default_and_writes_nothing(f):
    before = f.read_bytes()
    r = _run(str(f), "N-1", "Corrected title")
    assert r.returncode == 0, r.stderr
    assert f.read_bytes() == before
    out = json.loads(r.stdout)
    assert out["applied"] is False and out["gate"] == "pass"
    assert out["before"].startswith("## Old title") and out["after"] == "## Corrected title [N-1]"


def test_apply_retitles_id_last_heading_and_is_ledgered(f):
    import retitle_gated as RG
    res = RG.retitle(f, "N-1", "Corrected title", apply=True, reason="false claim")
    assert res["gate"] == "pass" and res["applied"] is True
    assert "## Corrected title [N-1]" in f.read_text()
    assert res["ledger"]["status"] == "ok"


def test_id_first_heading_with_doctype_prefix_keeps_its_bracket(f):
    import retitle_gated as RG
    res = RG.retitle(f, "N-2", "A renamed heading", apply=True)
    assert res["gate"] == "pass"
    assert "### [SPEC:N-2] A renamed heading" in f.read_text()


def test_REFUSES_when_another_change_rides_along(f, monkeypatch):
    """The architect's required control: a property edited alongside the heading must be refused."""
    import retitle_gated as RG
    real = RG._candidate

    def sneaky(text, line_no, old_title, new_title):
        cand = real(text, line_no, old_title, new_title)
        return cand.replace("- kind: claim", "- kind: tampered")
    monkeypatch.setattr(RG, "_candidate", sneaky)
    before = f.read_bytes()
    with pytest.raises(RG.RetitleRefused, match="more than the heading"):
        RG.retitle(f, "N-1", "Corrected title", apply=True)
    assert f.read_bytes() == before


def test_refuses_brackets_or_newlines_in_the_new_title(f):
    import retitle_gated as RG
    for bad in ("x [N-9]", "two\nlines", ""):
        with pytest.raises(RG.RetitleRefused):
            RG.retitle(f, "N-1", bad, apply=True)
    assert f.read_text() == DOC


def test_refuses_an_unknown_node(f):
    import retitle_gated as RG
    with pytest.raises(RG.RetitleRefused, match="no node"):
        RG.retitle(f, "N-404", "x", apply=True)


def test_refuses_when_the_title_text_is_not_unique_in_its_heading_line(f, tmp_path):
    import retitle_gated as RG
    g = f.parent / "e.lts"
    g.write_text("# Doc [DOC-B]\n\n## AB [AB]\n\n- kind: x\n", encoding="utf-8")   # title == id: "AB" occurs twice
    with pytest.raises(RG.RetitleRefused, match="not unique"):
        RG.retitle(g, "AB", "new", apply=True)


# ---- review 2026-10-04 (P1-1, P2-1, P3-2): lost update, CRLF, uncaught errors ---------------------------

def test_P1_REFUSES_a_concurrent_edit_between_read_and_write(f, monkeypatch):
    """Lost update: another writer lands after the tool's read; the tool must refuse, not overwrite."""
    import retitle_gated as RG
    real = RG._candidate
    interloper = DOC.replace("- kind: claim", "- kind: CONCURRENT").encode()

    def racing(text, line_no, old_title, new_title):
        cand = real(text, line_no, old_title, new_title)
        f.write_bytes(interloper)
        return cand
    monkeypatch.setattr(RG, "_candidate", racing)
    with pytest.raises(RG.RetitleRefused, match="changed since it was read"):
        RG.retitle(f, "N-1", "Corrected title", apply=True)
    assert f.read_bytes() == interloper


def test_P2_REFUSES_a_CRLF_file(f):
    import retitle_gated as RG
    f.write_bytes(DOC.replace("\n", "\r\n").encode())
    before = f.read_bytes()
    with pytest.raises(RG.RetitleRefused, match="CRLF"):
        RG.retitle(f, "N-1", "Corrected title", apply=True)
    assert f.read_bytes() == before


def test_P3_missing_file_and_bad_encoding_are_Refused_and_the_CLI_prints_JSON(f, tmp_path):
    import retitle_gated as RG
    with pytest.raises(RG.RetitleRefused):
        RG.retitle(tmp_path / "nope.lts", "N-1", "x")
    f.write_bytes(b"# D [DOC-A]\n\xff\xfe\n")
    with pytest.raises(RG.RetitleRefused):
        RG.retitle(f, "N-1", "x")
    r = _run(str(tmp_path / "nope.lts"), "N-1", "x")
    assert r.returncode == 1 and json.loads(r.stdout)["gate"] == "refused", r.stderr
