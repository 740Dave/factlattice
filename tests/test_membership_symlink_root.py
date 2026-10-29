"""[FL-MEMBERSHIP-SYMLINK] — a symlinked corpus root false-positives "outside every root".

Measured this session: `~/projectlink` and `~/workspace` are symlinked roots into
a file-sync service. `factlattice.authoring.edit` resolves the authored path with
`Path(path).expanduser().resolve()` (line 318/479/604) — which resolves symlinks, so a
write through a symlinked root arrives at `membership.check` as the REALPATH. But
`membership._norm` normalized the REGISTERED root with `abspath`, which does NOT resolve
symlinks — so the registered root stayed in symlink form. Two representations of the same
directory, compared for containment, never matched: the write landed, but the receipt
carried a false "OUTSIDE EVERY REGISTERED CORPUS ROOT" warning every time.

The fix: `_norm` canonicalizes with `os.path.realpath` (which resolves symlinks) instead
of `abspath` (which does not), so both sides of the comparison collapse to the same
representation regardless of which one was authored through a symlink.

Two cases:
  1. THE REPRO — a write through a symlinked root must NOT warn (must FAIL pre-fix).
  2. THE NEGATIVE CONTROL — a write to a path genuinely outside every root (even after
     symlink resolution) must STILL warn. The fix must not blanket-suppress the warning.
"""
from __future__ import annotations

import os

import pytest

from factlattice_engine import membership
from factlattice_engine.tools import dispatch

BROKEN = """---
scope: s
audience: a
boundary: b
---
# Control [D-ROOT]

## A node [N-ONE]

- kind: measurement
"""


@pytest.fixture()
def symlinked_root(tmp_path):
    """A real directory plus a symlink that points at it — the `~/projectlink` shape."""
    real_dir = tmp_path / "real_corpus"
    real_dir.mkdir()
    link_root = tmp_path / "corpus_link"
    os.symlink(real_dir, link_root)
    return real_dir, link_root


def test_a_write_through_a_symlinked_registered_root_does_not_false_positive(
    symlinked_root, monkeypatch
):
    """THE REPRO. Register the corpus root by its SYMLINK path (as a corpus config
    naturally would — `~/projectlink`, not its a file-sync service realpath), then write through
    that same symlink path. The write must land AND must be recognized as inside the
    registered root — no "OUTSIDE EVERY REGISTERED CORPUS ROOT" warning."""
    real_dir, link_root = symlinked_root
    doc = link_root / "control.lts"          # authored path goes THROUGH the symlink
    doc.write_text(BROKEN, encoding="utf-8")

    monkeypatch.setattr(
        membership, "registered_roots",
        lambda store=None: ({"alpha": [str(link_root)]}, "config"),
    )

    res = dispatch(
        "lts_add_property",
        {"path": str(doc), "node_id": "N-ONE", "key": "note", "value": "x"},
    )

    assert res["success"] is True and res["written"] is True
    assert res["root_membership"]["inside_corpus"] is True, (
        f"false-positive: {res['root_membership']}"
    )
    assert res["root_membership"]["corpora"] == ["alpha"]
    assert res["root_membership"]["warning"] is None
    assert not any("OUTSIDE EVERY REGISTERED CORPUS ROOT" in w
                   for w in res.get("warnings") or [])


def test_membership_check_directly_on_a_symlinked_root(symlinked_root):
    """The unit-level shape, independent of the dispatch/receipt plumbing: `check()`
    called with the REALPATH of a file under a symlinked root, against a registered root
    given in SYMLINK form, must report containment."""
    real_dir, link_root = symlinked_root
    real_file = real_dir / "control.lts"
    real_file.write_text(BROKEN, encoding="utf-8")

    class _FakeStore:
        def partitions(self):
            return []

    # Roots registered in symlink form (as a human-authored config naturally would be);
    # the path handed to check() is the fully-resolved realpath, mirroring what
    # `Path(...).resolve()` produces upstream in `factlattice.authoring.edit`.
    roots = {"alpha": [str(link_root)]}
    monkeypatch_roots = lambda store=None: (roots, "config")  # noqa: E731
    orig = membership.registered_roots
    membership.registered_roots = monkeypatch_roots
    try:
        verdict = membership.check(str(real_file.resolve()))
    finally:
        membership.registered_roots = orig

    assert verdict["inside_corpus"] is True
    assert verdict["corpora"] == ["alpha"]
    assert verdict["warning"] is None


def test_a_path_genuinely_outside_every_root_still_warns(symlinked_root, monkeypatch):
    """THE NEGATIVE CONTROL. The fix must not over-correct into never warning: a path
    that is outside every registered root — symlink-resolved or not — must still produce
    the "outside every root" warning."""
    real_dir, link_root = symlinked_root
    # A sibling directory, never registered and not reachable via the symlink at all.
    outside_dir = link_root.parent / "elsewhere"
    outside_dir.mkdir()
    doc = outside_dir / "control.lts"
    doc.write_text(BROKEN, encoding="utf-8")

    monkeypatch.setattr(
        membership, "registered_roots",
        lambda store=None: ({"alpha": [str(link_root)]}, "config"),
    )

    res = dispatch(
        "lts_add_property",
        {"path": str(doc), "node_id": "N-ONE", "key": "note", "value": "x"},
    )

    assert res["success"] is True and res["written"] is True   # still lands
    assert res["root_membership"]["inside_corpus"] is False
    assert any("OUTSIDE EVERY REGISTERED CORPUS ROOT" in w
               for w in res.get("warnings") or [])


def test_a_sibling_symlink_named_similarly_does_not_count_as_containment(
    symlinked_root, monkeypatch
):
    """Guards against an over-broad fix (e.g. prefix-matching realpaths) reintroducing
    the sibling-directory false positive `_contains` already guards against
    (`/a/corpus-old` inside `/a/corpus`), now checked through symlink resolution too."""
    real_dir, link_root = symlinked_root
    sibling_real = real_dir.parent / (real_dir.name + "-old")
    sibling_real.mkdir()
    sibling_link = link_root.parent / "corpus_link-old"
    os.symlink(sibling_real, sibling_link)

    doc = sibling_link / "control.lts"
    doc.write_text(BROKEN, encoding="utf-8")

    monkeypatch.setattr(
        membership, "registered_roots",
        lambda store=None: ({"alpha": [str(link_root)]}, "config"),
    )

    res = dispatch(
        "lts_add_property",
        {"path": str(doc), "node_id": "N-ONE", "key": "note", "value": "x"},
    )
    assert res["root_membership"]["inside_corpus"] is False
    assert any("OUTSIDE EVERY REGISTERED CORPUS ROOT" in w
               for w in res.get("warnings") or [])
