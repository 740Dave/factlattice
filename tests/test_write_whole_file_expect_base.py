"""write_whole_file(expect_base=...): compare-and-swap UNDER the per-file lock (review P1-1, 2026-10-04).

An interim writer reads a file, gates on it, then writes. Without a base check a concurrent edit between
the read and the write is silently overwritten. `expect_base` makes the write refuse unless the bytes read
under the lock equal the bytes the caller gated. Default None leaves behaviour unchanged.
"""
from __future__ import annotations

import pytest

from factlattice.authoring.edit import EditRefused, write_whole_file

DOC = "# D [DOC-A]\n\n## N [N-1]\n\n- kind: y\n"


@pytest.fixture()
def f(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    p = tmp_path / "corpus" / "d.lts"
    p.parent.mkdir()
    p.write_text(DOC, encoding="utf-8")
    return p


def test_expect_base_refuses_when_the_file_changed_since_it_was_read(f):
    gated = f.read_bytes()
    interloper = DOC.replace("- kind: y", "- kind: CONCURRENT").encode()
    f.write_bytes(interloper)                      # the concurrent writer lands between read and write
    with pytest.raises(EditRefused, match="changed since it was read"):
        write_whole_file(f, DOC.replace("- kind: y", "- kind: mine"), expect_base=gated)
    assert f.read_bytes() == interloper


def test_expect_base_matching_bytes_write_normally(f):
    new = DOC.replace("- kind: y", "- kind: mine")
    write_whole_file(f, new, expect_base=f.read_bytes())
    assert f.read_text() == new


def test_default_none_is_unchanged(f):
    f.write_bytes(b"anything else\n")
    new = DOC.replace("- kind: y", "- kind: mine")
    write_whole_file(f, new)
    assert f.read_text() == new
