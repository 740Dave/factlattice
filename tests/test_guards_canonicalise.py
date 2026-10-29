"""SDLC 7.6 [GUARDS-CANONICALISE-FIRST]: a check that can short-circuit runs on the CANONICAL input
(realpath on both sides, path COMPONENTS not string prefixes). preview._rel_within was the engine's
one non-compliant containment check: abspath (no symlink resolution) plus a string `..` prefix test.
"""
from __future__ import annotations

import os

from factlattice_engine.preview import _rel_within


def test_a_file_whose_name_starts_with_two_dots_is_inside(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    (root / "..x.lts").write_text("x")
    assert _rel_within(str(root), str(root / "..x.lts")) == "..x.lts"


def test_root_via_a_symlink_and_path_in_resolved_form_is_inside(tmp_path):
    real = tmp_path / "real-root"; real.mkdir()
    (real / "a.lts").write_text("x")
    link = tmp_path / "link-root"; link.symlink_to(real)
    # the root as registered (through a symlink, like ~/workspace -> a file-sync service) and the file as resolved
    assert _rel_within(str(link), str(real / "a.lts")) == "a.lts"
    assert _rel_within(str(real), str(link / "a.lts")) == "a.lts"


def test_a_path_outside_is_outside(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    other = tmp_path / "other.lts"; other.write_text("x")
    assert _rel_within(str(root), str(other)) is None
    assert _rel_within(str(root), str(root / ".." / "other.lts")) is None


def test_a_symlink_inside_root_pointing_outside_is_outside(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    outside = tmp_path / "secret.lts"; outside.write_text("x")
    (root / "innocent.lts").symlink_to(outside)
    assert _rel_within(str(root), str(root / "innocent.lts")) is None


def test_sdlc_carries_the_rule():
    sdlc = (os.path.join(os.path.dirname(__file__), "..", "SDLC.md"))
    text = open(sdlc, encoding="utf-8").read()
    assert "[GUARDS-CANONICALISE-FIRST]" in text and "pinning test" in text.lower()
