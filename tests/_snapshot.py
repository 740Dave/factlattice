"""Tell a published snapshot from the development tree, positively.

A published snapshot carries `.github/workflows/published-snapshot.md`. The trunk keeps that file at `publish/github/workflows/published-snapshot.md`, and the
release's one folder map (`publish/github/workflows/` -> `.github/workflows/`) places it in the snapshot and nowhere else. A test that needs a file the snapshot
does not contain skips when `is_published_snapshot()` is true, and runs everywhere else, so a file deleted by accident from the trunk fails its test instead of
turning it into a skip. tests/test_snapshot_marker.py pins the three ends of this: the trunk has no mapped copy, the trunk's source exists, and a fake snapshot reads true.
"""

import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MARKER_SOURCE = os.path.join("publish", "github", "workflows", "published-snapshot.md")
MARKER_MAPPED = os.path.join(".github", "workflows", "published-snapshot.md")


def is_published_snapshot(root=ROOT):
    """True when `root` holds the mapped marker as a regular file (a directory or a dangling link of that name does not count)."""
    path = os.path.join(root, MARKER_MAPPED)
    return os.path.isfile(path) and not os.path.islink(path)


def skip_reason(path):
    """The one skip reason, in the form release_prep's report and the declared-skip pin read."""
    return f"{path} is not in a published snapshot (PUBLISH-EXCLUDE)"
