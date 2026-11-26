"""The published-snapshot marker (tests/_snapshot.py): the trunk never carries the mapped copy, its source must exist, and the helper reads a snapshot as one."""

import os
import subprocess

from tests._snapshot import MARKER_MAPPED, MARKER_SOURCE, ROOT, is_published_snapshot, skip_reason

SENTENCE = "This file exists only in a published snapshot of this repository; tests use it to tell a snapshot from the development tree."


def in_trunk():
    """The trunk layout: the source of the folder map is here. (A snapshot has moved it away.)"""
    return os.path.isdir(os.path.join(ROOT, "publish", "github", "workflows"))


def test_the_marker_source_exists_on_trunk_so_a_deleted_marker_fails_loudly():
    # Runs in the snapshot too: there the source has been moved to the mapped path, so one of the two must hold. Neither = every snapshot would read as trunk.
    assert os.path.isfile(os.path.join(ROOT, MARKER_SOURCE)) or os.path.isfile(os.path.join(ROOT, MARKER_MAPPED))
    path = os.path.join(ROOT, MARKER_SOURCE if in_trunk() else MARKER_MAPPED)
    assert open(path, encoding="utf-8").read().strip() == SENTENCE


def test_a_trunk_tree_never_carries_the_mapped_marker():
    if not in_trunk():
        import pytest

        pytest.skip(skip_reason("publish/github/workflows/"))
    assert not os.path.lexists(os.path.join(ROOT, MARKER_MAPPED)), "the mapped marker exists on disk in the trunk layout"
    assert not is_published_snapshot()
    if os.path.isdir(os.path.join(ROOT, ".git")) or os.path.isfile(os.path.join(ROOT, ".git")):
        r = subprocess.run(["git", "-C", ROOT, "ls-files", "--", MARKER_MAPPED], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and r.stdout == "", f"the trunk tracks the mapped marker: {r.stdout!r} {r.stderr!r}"


def test_the_helper_reads_a_snapshot_layout_as_one_and_refuses_lookalikes(tmp_path):
    # positive control: the helper must be able to say True, and must not say True for a directory or a link of that name
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    assert not is_published_snapshot(str(tmp_path))
    (wf / "published-snapshot.md").mkdir()
    assert not is_published_snapshot(str(tmp_path))
    (wf / "published-snapshot.md").rmdir()
    os.symlink(str(tmp_path / "nowhere"), str(wf / "published-snapshot.md"))
    assert not is_published_snapshot(str(tmp_path))
    os.unlink(str(wf / "published-snapshot.md"))
    (wf / "published-snapshot.md").write_text(SENTENCE + "\n")
    assert is_published_snapshot(str(tmp_path))
