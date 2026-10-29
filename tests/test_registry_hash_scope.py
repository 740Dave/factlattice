"""U1a round 2 (Q-3 re-ruled): WHAT THE REGISTRY HASH COVERS.

The hash is over the registry bytes ALWAYS, plus cwd + resolved root only when some corpus-root or `profile` path is
non-absolute (a leading `~` counts), plus $HOME only when some such path starts with `~`. The store resolves relative
roots against the process cwd; the profile lookup resolves them under the resolved root; so both bases matter, and
neither matters for an absolute-only registry. Resolution itself is NOT changed here (U1a-2).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture()
def w(tmp_path, monkeypatch):
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "st"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    for d in ("c1", "c2", "r1", "r2", "h1", "h2"):
        (tmp_path / d).mkdir()
    return tmp_path


def digest(w, monkeypatch, registry, *, root=None, cwd=None, home=None):
    from factlattice import registry as R
    f = w / "reg.json"
    f.write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(f))
    if root is None:
        monkeypatch.delenv("FACTLATTICE_CORPUS_ROOT", raising=False)
    else:
        monkeypatch.setenv("FACTLATTICE_CORPUS_ROOT", str(w / root))
    monkeypatch.chdir(w / (cwd or "c1"))
    if home:
        monkeypatch.setenv("HOME", str(w / home))
    return R.resolve_registry().sha256


ABS = lambda w: {"a": {"roots": [{"path": str(w / "x"), "area": "specs"}], "profile": str(w / "p.lts")}}
REL_ROOT = {"a": {"roots": [{"path": "rel/dir", "area": "specs"}]}}
REL_LEGACY = {"a": {"root": "rel/dir"}}
REL_PROFILE = lambda w: {"a": {"roots": [{"path": str(w / "x")}], "profile": "prof.lts"}}
TILDE = {"a": {"roots": [{"path": "~/corp"}]}}


def test_1_absolute_only_registry_is_equal_across_root_values(w, monkeypatch):
    a = digest(w, monkeypatch, ABS(w), root="r1")
    b = digest(w, monkeypatch, ABS(w), root="r2")
    c = digest(w, monkeypatch, ABS(w), root=None, cwd="c2")
    assert a == b == c


def test_2_a_relative_corpus_root_differs_with_the_root_value(w, monkeypatch):
    assert digest(w, monkeypatch, REL_ROOT, root="r1") != digest(w, monkeypatch, REL_ROOT, root="r2")
    assert digest(w, monkeypatch, REL_LEGACY, root="r1") != digest(w, monkeypatch, REL_LEGACY, root="r2")


def test_3_a_relative_profile_path_only_differs_with_the_root_value(w, monkeypatch):
    assert digest(w, monkeypatch, REL_PROFILE(w), root="r1") != digest(w, monkeypatch, REL_PROFILE(w), root="r2")


def test_4_a_relative_root_differs_with_the_cwd_at_the_same_root_setting(w, monkeypatch):
    assert digest(w, monkeypatch, REL_ROOT, root="r1", cwd="c1") != digest(w, monkeypatch, REL_ROOT, root="r1", cwd="c2")


def test_5_a_tilde_root_differs_with_HOME_and_a_relative_one_does_not(w, monkeypatch):
    assert digest(w, monkeypatch, TILDE, home="h1") != digest(w, monkeypatch, TILDE, home="h2")
    assert digest(w, monkeypatch, REL_ROOT, home="h1") == digest(w, monkeypatch, REL_ROOT, home="h2")


def test_6_the_example_registry_differs_across_cwds(w, monkeypatch):
    from factlattice import registry as R
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG", raising=False)
    monkeypatch.delenv("FACTLATTICE_CORPUS_ROOT", raising=False)
    monkeypatch.chdir(w / "c1")
    a = R.resolve_registry().sha256
    monkeypatch.chdir(w / "c2")
    assert R.resolve_registry().source == "example" and R.resolve_registry().sha256 != a


def test_7_live_shape_registry_mcp_style_vs_plain_shell_is_equal_and_the_reviewer_does_not_stop(w, monkeypatch):
    from factlattice import registry as R
    from factlattice_engine.reviewer import compare_registries
    reg = ABS(w)
    mcp_hash = digest(w, monkeypatch, reg, root="r1")                 # MCP-style: root set
    boot = R.resolve_registry()
    mcp = {"source": boot.source, "path": boot.path, "sha256_at_boot": mcp_hash, "sha256_now": mcp_hash,
           "resolved_at": "t"}
    cli = digest(w, monkeypatch, reg, root=None, cwd="c2")            # plain shell: root unset, other cwd
    cmp = compare_registries(mcp, R.resolve_registry().statement())
    assert mcp_hash == cli
    assert cmp["mcp_used"] and not cmp["mismatch"] and cmp["registry_mcp"] == cmp["registry_cli"]


def test_the_bytes_always_count(w, monkeypatch):
    a = digest(w, monkeypatch, ABS(w))
    b = digest(w, monkeypatch, {**ABS(w), "z": {"root": str(w / "y")}})
    assert a != b
