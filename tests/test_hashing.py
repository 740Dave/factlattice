"""Hashing tests: content-sensitivity + mtime-invariance (the load-bearing props)."""

import os
import time

from factlattice.hashing import (
    content_hash, file_content_hash, files_hash,
)


def test_content_hash_stable_and_prefixed():
    h1 = content_hash("hello world")
    h2 = content_hash("hello world")
    assert h1 == h2
    assert h1.startswith("sha256:")
    assert content_hash("hello world") != content_hash("hello worlds")


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def test_files_hash_content_sensitive(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    _write(root / "a.lts", "# A [SPEC:A]\n### [A-1] x\n")
    h_before, _ = files_hash(str(root))
    # change content -> hash MUST change
    _write(root / "a.lts", "# A [SPEC:A]\n### [A-1] y\n")
    h_after, _ = files_hash(str(root))
    assert h_before != h_after


def test_files_hash_add_and_remove_change_hash(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    _write(root / "a.lts", "# A [SPEC:A]\n")
    h1, _ = files_hash(str(root))
    _write(root / "b.lts", "# B [SPEC:B]\n")
    h2, _ = files_hash(str(root))
    assert h1 != h2                       # file added
    os.unlink(root / "b.lts")
    h3, _ = files_hash(str(root))
    assert h3 == h1                       # removal restores prior content set


def test_mtime_change_does_not_change_hash(tmp_path):
    """The load-bearing invariant ([INV-LTS-FRESH]): a content-preserving
    mtime bump (git checkout / touch -r) MUST NOT change the hash."""
    root = tmp_path / "corpus"
    root.mkdir()
    f = root / "a.lts"
    _write(f, "# A [SPEC:A]\ncontent stays identical\n")
    h_before = file_content_hash(str(f))
    corpus_before, _ = files_hash(str(root))
    # bump mtime a year forward WITHOUT touching content
    future = time.time() + 365 * 24 * 3600
    os.utime(f, (future, future))
    h_after = file_content_hash(str(f))
    corpus_after, _ = files_hash(str(root))
    assert h_before == h_after
    assert corpus_before == corpus_after


def test_non_lts_files_excluded(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    _write(root / "a.lts", "# A [SPEC:A]\n")
    h1, m1 = files_hash(str(root))
    _write(root / "notes.md", "not part of the corpus")
    _write(root / "events.jsonl", "{}")
    h2, m2 = files_hash(str(root))
    assert h1 == h2                       # .md/.jsonl do not affect corpus hash
    assert set(m1) == set(m2) == {"a.lts"}
