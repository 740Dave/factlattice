"""[SIDECAR-PER-VERSION] (followups item 13, found planning go-live-runbook-2, 2026-10-02).

The sidecar dir was semantic/<model>/<corpus>/ for EVERY index_version. On a version bump,
old-code and new-code engines that coexist (until every session reconnects) each found the
other's index "incompatible" and rebuilt it in place: explicit semantic calls ping-ponged full
rebuilds (~10 CPU-min each). Contract now:
  * each index_version has its own dir: semantic/<model>/v-<index_version>/<corpus>/, so two
    versions never touch each other's files;
  * a legacy semantic/<model>/<corpus>/ sidecar of the SAME version is ADOPTED by copy (no
    re-embed, legacy left intact for still-running old engines); a different version is not;
  * other versions' dirs untouched for GC_AGE_S are garbage-collected after a successful write,
    only when their lock is free, and never the running version.
"""
from __future__ import annotations

import os
import shutil
import stat
import time

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")

from factlattice_engine.semantic import embedder as E  # noqa: E402
from factlattice_engine.semantic import index as I  # noqa: E402
from tests.test_semantic_index import CountingEmbedder, _part, _write_corpus  # noqa: E402
from tests.semantic_helpers import _tiny_model  # noqa: E402


class Wrap(CountingEmbedder):
    """CountingEmbedder that also forwards max_len etc., so index_version sees the real
    embedder's parameters (the bare wrapper hides max_len, collapsing two versions into one)."""

    def __getattr__(self, name):
        return getattr(self.inner, name)


@pytest.fixture()
def env(tmp_path):
    pin = _tiny_model(str(tmp_path / "models"))
    inner = E.load_embedder(pin, str(tmp_path / "models"))
    other = E.Embedder(inner.pin, inner.session, inner.tokenizer, 256)    # a different index_version
    _write_corpus(tmp_path / "corpus")
    return tmp_path, Wrap(inner), Wrap(other)


def test_two_versions_sharing_a_state_root_never_rebuild_each_other(env):
    tmp, new, old = env
    st = str(tmp / "state")
    a, b = I.SemanticIndex(st, "c", new), I.SemanticIndex(st, "c", old)
    assert I.index_version(new.model_id, new) != I.index_version(old.model_id, old)
    assert a.dir != b.dir
    a.refresh(_part(tmp)); b.refresh(_part(tmp))
    n_new, n_old = new.texts, old.texts
    for _ in range(3):                                    # the ping-pong: alternate the two engines
        assert I.SemanticIndex(st, "c", new).refresh(_part(tmp))["embedded_nodes"] == 0
        assert I.SemanticIndex(st, "c", old).refresh(_part(tmp))["embedded_nodes"] == 0
    assert (new.texts, old.texts) == (n_new, n_old)


def test_dir_is_keyed_by_version_and_private_at_every_level(env):
    tmp, new, _ = env
    idx = I.SemanticIndex(str(tmp / "state"), "c", new)
    idx.refresh(_part(tmp))
    ver = I.index_version(new.model_id, new)
    assert idx.dir == os.path.join(str(tmp / "state"), "semantic", new.model_id, f"v-{ver}", "c")
    d = idx.dir
    for _ in range(4):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700, d
        d = os.path.dirname(d)


def _make_legacy(tmp, emb):
    """Build a sidecar, then move it to the pre-change legacy path semantic/<model>/<corpus>."""
    idx = I.SemanticIndex(str(tmp / "state"), "c", emb)
    idx.refresh(_part(tmp))
    legacy = os.path.join(str(tmp / "state"), "semantic", emb.model_id, "c")
    shutil.move(idx.dir, legacy)
    shutil.rmtree(os.path.dirname(idx.dir))
    return legacy


def test_a_same_version_legacy_sidecar_is_adopted_without_reembedding(env):
    tmp, new, _ = env
    legacy = _make_legacy(tmp, new)
    before = new.texts
    idx = I.SemanticIndex(str(tmp / "state"), "c", new)
    s = idx.refresh(_part(tmp))
    assert s["embedded_nodes"] == 0 and new.texts == before
    assert os.path.isfile(os.path.join(idx.dir, "manifest.json"))
    assert os.path.isfile(os.path.join(legacy, "manifest.json"))      # left for old engines


def test_a_different_version_legacy_sidecar_is_not_adopted(env):
    tmp, new, old = env
    _make_legacy(tmp, old)
    idx = I.SemanticIndex(str(tmp / "state"), "c", new)
    with pytest.raises(I.IndexIncompatible) as ei:
        idx.search(new.embed(["hello"], query=True)[0], 3)
    assert ei.value.status == "index_incompatible"          # another version exists: say so
    before = new.texts
    assert idx.refresh(_part(tmp))["rebuilt_reason"] == "index_incompatible"
    assert new.texts > before                               # rebuilt, not adopted


def test_stale_other_versions_are_collected_fresh_and_running_ones_kept(env, monkeypatch):
    tmp, new, old = env
    st = str(tmp / "state")
    b = I.SemanticIndex(st, "c", old); b.refresh(_part(tmp))
    legacy = _make_legacy(tmp, old)                      # an old-layout dir too (old version)
    b2 = I.SemanticIndex(st, "c", old); b2.refresh(_part(tmp))
    ancient = time.time() - I.GC_AGE_S - 3600
    for root in (b2.dir, legacy):
        for dp, _dn, fs in os.walk(root):
            for f in fs:
                os.utime(os.path.join(dp, f), (ancient, ancient))
    a = I.SemanticIndex(st, "c", new); a.refresh(_part(tmp))
    assert not os.path.exists(b2.dir) and not os.path.exists(legacy)
    assert os.path.isfile(os.path.join(a.dir, "manifest.json"))
    # a recently used other version is kept
    c = I.SemanticIndex(st, "c", old); c.refresh(_part(tmp))
    I.SemanticIndex(st, "c", new).refresh(_part(tmp))
    assert os.path.isfile(os.path.join(c.dir, "manifest.json"))


# --- review audit-SEMANTIC-SIDECAR-PER-VERSION F1, F2 and P3s ----------------------------
def _age(root, secs):
    t = time.time() - secs
    for dp, _dn, fs in os.walk(root):
        for f in fs:
            os.utime(os.path.join(dp, f), (t, t))


def test_an_adoption_failure_falls_through_to_a_rebuild(env, monkeypatch):
    tmp, new, _ = env
    _make_legacy(tmp, new)

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(I.shutil, "copyfile", boom)
    s = I.SemanticIndex(str(tmp / "state"), "c", new).refresh(_part(tmp))
    assert s["embedded_nodes"] > 0                          # rebuilt, not raised


def test_a_torn_adoption_is_never_served(env, monkeypatch):
    tmp, new, _ = env
    _make_legacy(tmp, new)
    real, calls = I.shutil.copyfile, {"n": 0}

    def torn(src, dst):
        calls["n"] += 1
        if src.endswith("fps.json"):
            open(dst, "w").write("{partial")                # a real mid-copy failure leaves a temp
            raise OSError(5, "I/O error")                   # dies before the manifest is copied
        return real(src, dst)
    monkeypatch.setattr(I.shutil, "copyfile", torn)
    idx = I.SemanticIndex(str(tmp / "state"), "c", new)
    idx.refresh(_part(tmp))
    assert os.path.isfile(os.path.join(idx.dir, "manifest.json"))   # a full rebuild wrote it
    assert not [f for f in os.listdir(idx.dir) if f.endswith(".tmp")]
    monkeypatch.setattr(I.shutil, "copyfile", real)
    assert I.SemanticIndex(str(tmp / "state"), "c", new).refresh(_part(tmp))["embedded_nodes"] == 0


def test_gc_skips_a_locked_dir_and_rechecks_age_under_the_lock(env):
    import fcntl
    tmp, new, old = env
    st = str(tmp / "state")
    b = I.SemanticIndex(st, "c", old); b.refresh(_part(tmp)); _age(b.dir, I.GC_AGE_S + 3600)
    fd = os.open(os.path.join(b.dir, ".lock"), os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)                          # an old engine is writing it
    try:
        I.SemanticIndex(st, "c", new).refresh(_part(tmp))
        assert os.path.isfile(os.path.join(b.dir, "manifest.json"))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
    a = I.SemanticIndex(st, "c", new)
    real_flock = I.fcntl.flock

    def write_then_lock(f, op):                             # a fresh write lands between the
        if op & fcntl.LOCK_NB:                              # age check and the lock
            os.utime(os.path.join(b.dir, "manifest.json"), None)
        return real_flock(f, op)
    I.fcntl.flock = write_then_lock
    try:
        assert a._gc_other_versions() == []
    finally:
        I.fcntl.flock = real_flock
    assert os.path.isfile(os.path.join(b.dir, "manifest.json"))


def test_gc_never_follows_a_symlink_out_of_the_model_dir(env, tmp_path):
    tmp, new, _ = env
    st = str(tmp / "state")
    a = I.SemanticIndex(st, "c", new); a.refresh(_part(tmp))
    outside = tmp_path / "precious" / "c"
    outside.mkdir(parents=True); (outside / "keep.txt").write_text("x")
    _age(str(outside), I.GC_AGE_S + 3600)
    os.symlink(str(tmp_path / "precious"), os.path.join(a.model_dir, "v-" + "0" * 16))
    a._gc_other_versions()
    assert (outside / "keep.txt").exists()


def test_manifest_is_stamped_with_the_instance_version(env, monkeypatch):
    import json
    tmp, new, _ = env
    idx = I.SemanticIndex(str(tmp / "state"), "c", new)
    monkeypatch.setattr(I, "index_version", lambda *a, **k: "f" * 16)   # must not be re-derived
    idx.refresh(_part(tmp))
    assert json.load(open(os.path.join(idx.dir, "manifest.json")))["index_version"] == idx.version
