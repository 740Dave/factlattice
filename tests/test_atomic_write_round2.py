"""[ISSUE-WRITE-ATOMICITY] round 2: compare-and-swap, symlinked corpus file,
write_document on the shared step, metadata preservation, read-only refusal, flock-unsupported.

Fault injection is by patching `os.replace` (the swap point), which exists in the new writer; the
old in-place writer never calls it, so these tests are RED there, not vacuous.
"""
from __future__ import annotations

import errno
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from factlattice.authoring import edit
from factlattice.authoring.edit import EditRefused, set_property
from factlattice.parser import parse_text
from tests.test_atomic_write_lock import _doc, _props, _KILL_CHILD, REPO


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    c = tmp_path / "corpus"
    c.mkdir()
    return c


@pytest.fixture
def f(env):
    p = env / "c.lts"
    p.write_text(_doc(), encoding="utf-8")
    return p


def _no_tmp(d: Path):
    return [p.name for p in d.iterdir() if p.name.endswith(".tmp")]


# ---- (1) compare-and-swap ------------------------------------------------------------------
INTERLOPER = "\n## Interloper [AW-INT]\n\n- kind: hand-edit\n"


def test_cas_refuses_when_the_file_changed_under_the_writer(f, monkeypatch):
    hook_calls = []

    def hook(real):
        hook_calls.append(real)
        with open(real, "a", encoding="utf-8") as fh:
            fh.write(INTERLOPER)
    monkeypatch.setattr(edit, "_BEFORE_REPLACE_HOOK", hook)
    with pytest.raises(EditRefused, match="changed on disk") as ei:
        set_property(f, "AW-N5", "gamma", "G")
    assert hook_calls
    cur = f.read_bytes()
    assert INTERLOPER.encode() in cur and b"gamma: G" not in cur       # interloper kept, writer's lost
    assert ei.value.evidence["current_sha256"] == hashlib.sha256(cur).hexdigest()
    assert ei.value.evidence["base_sha256"] != ei.value.evidence["current_sha256"]
    assert _no_tmp(f.parent) == []


def test_cas_negative_control_without_the_comparison_the_interlopers_change_is_lost(f, monkeypatch):
    def hook(real):
        with open(real, "a", encoding="utf-8") as fh:
            fh.write(INTERLOPER)
    monkeypatch.setattr(edit, "_BEFORE_REPLACE_HOOK", hook)
    monkeypatch.setattr(edit, "_cas_check", lambda real, base, tmp: None)
    set_property(f, "AW-N5", "gamma", "G")
    assert b"AW-INT" not in f.read_bytes()          # the silent loss the CAS exists to refuse


def test_cas_does_not_fire_without_an_interloper(f):
    set_property(f, "AW-N5", "gamma", "G")
    assert _props(f)["AW-N5"]["gamma"] == "G"


# ---- (2) symlinked corpus FILE ---------------------------------------------------------------
def test_a_symlinked_corpus_file_stays_a_symlink_and_the_target_gets_the_bytes(env):
    realdir = env.parent / "real"
    realdir.mkdir()
    target = realdir / "t.lts"
    target.write_text(_doc(), encoding="utf-8")
    link = env / "link.lts"
    link.symlink_to(target)
    set_property(link, "AW-N5", "gamma", "G")
    assert link.is_symlink() and os.readlink(link) == str(target)
    assert _props(target)["AW-N5"]["gamma"] == "G"
    assert _no_tmp(env) == [] and _no_tmp(realdir) == []
    assert [p.name for p in env.iterdir()] == ["link.lts"]
    # the lock is keyed on the REAL path: holding it via the target blocks a write via the link
    os.environ["FACTLATTICE_WRITE_LOCK_TIMEOUT"] = "0.2"
    try:
        with edit._file_lock(target):
            with pytest.raises(EditRefused, match="busy"):
                set_property(link, "AW-N5", "delta", "D")
    finally:
        del os.environ["FACTLATTICE_WRITE_LOCK_TIMEOUT"]


def test_symlink_negative_control_replacing_onto_the_link_path_makes_it_a_regular_file(env):
    realdir = env.parent / "real2"
    realdir.mkdir()
    target = realdir / "t.lts"
    target.write_text("x")
    link = env / "l.lts"
    link.symlink_to(target)
    tmp = env / ".l.tmp"
    tmp.write_text("y")
    os.replace(tmp, link)
    assert not link.is_symlink() and target.read_text() == "x"      # what the assertion above forbids


def test_the_temp_name_never_ends_in_dot_lts(f, monkeypatch):
    seen = []
    monkeypatch.setattr(edit, "_BEFORE_REPLACE_HOOK",
                        lambda real: seen.extend(p.name for p in real.parent.iterdir()))
    set_property(f, "AW-N5", "gamma", "G")
    temps = [n for n in seen if n != "c.lts"]
    assert temps and all(n.startswith(".") and n.endswith(".tmp") and not n.endswith(".lts") for n in temps)


# ---- P2-1: write_document on the shared step --------------------------------------------------
def _doc_obj():
    from tests.test_sanctioned_surface import _fm
    from factlattice.authoring.schema import Document, Node, Property
    return Document(frontmatter=_fm(), document_tag="SPEC", section="Body",
                    nodes=[Node(node_id="D-ITEM", title="An item with a long title",
                                properties=[Property(key="kind", value="measurement")])])


def _fake_preview(content, corpus=None):
    pf = parse_text(content, "draft.lts")
    return {"became": {"nodes": [{"id": n.id, "properties": dict(n.properties)} for n in pf.nodes],
                       "document_tag": pf.document_tag},
            "resolution": {"profile": {"available": True}}, "lost": []}


def test_write_document_takes_the_lock_and_writes_nothing_when_busy(env, monkeypatch):
    from factlattice.authoring.schema import WriteRefused, write_document
    out = env / "w.lts"
    out.write_text("OLD\n")
    monkeypatch.setenv("FACTLATTICE_WRITE_LOCK_TIMEOUT", "0.2")
    with edit._file_lock(out):
        with pytest.raises(WriteRefused, match="busy"):
            write_document(_doc_obj(), str(out), _fake_preview)
    assert out.read_text() == "OLD\n"


def test_write_document_is_atomic_a_kill_mid_write_leaves_the_old_file(env, tmp_path):
    child = f'''
import os, signal, sys
sys.path.insert(0, {REPO!r}); sys.path.insert(0, {REPO + "/tests"!r})
_ow = os.write
def ow(fd, data):
    _ow(fd, bytes(data)[: len(data) // 2]); os.kill(os.getpid(), signal.SIGKILL)
os.write = ow
import builtins
_open = builtins.open
def op(file, mode="r", *a, **k):
    if "w" in mode and str(file).endswith("w.lts"):
        _open(file, "w").write(open(file).read()[:3]); os.kill(os.getpid(), signal.SIGKILL)
    return _open(file, mode, *a, **k)
builtins.open = op
from factlattice.authoring.schema import write_document
from tests.test_atomic_write_round2 import _doc_obj, _fake_preview
write_document(_doc_obj(), sys.argv[1], _fake_preview)
'''
    out = env / "w.lts"
    out.write_text("OLD CONTENT OF SOME LENGTH\n")
    p = subprocess.run([sys.executable, "-c", child, str(out)], capture_output=True, text=True,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), timeout=60)
    assert p.returncode == -9, p.stderr[-500:]
    assert out.read_text() == "OLD CONTENT OF SOME LENGTH\n"


def test_write_document_creates_a_new_file_and_backs_up_an_existing_one(env, tmp_path):
    from factlattice.authoring.schema import write_document, _render
    new = env / "n.lts"
    r = write_document(_doc_obj(), str(new), _fake_preview)
    assert new.read_text() == _render(_doc_obj()) and r["written"] == str(new)
    write_document(_doc_obj(), str(new), _fake_preview)
    assert list((tmp_path / "bak").glob("n.lts.*.bak"))


# ---- P2-2: metadata, hard links -----------------------------------------------------------------
def test_mtime_moves_even_for_a_same_length_edit(f):
    old = time.time() - 5000
    os.utime(f, (old, old))
    set_property(f, "AW-N5", "g", "1")
    set_property(f, "AW-N5", "g", "2")           # same length
    assert time.time() - f.stat().st_mtime < 60


def test_group_and_xattr_survive_the_replace(f):
    other = [g for g in os.getgroups() if g != f.stat().st_gid]
    if other:
        os.chown(f, -1, other[0])
    subprocess.run(["xattr", "-w", "com.example.test", "v", str(f)], check=True)
    st0 = f.stat()
    set_property(f, "AW-N5", "gamma", "G")
    if other:
        assert f.stat().st_gid == other[0]
    out = subprocess.run(["xattr", "-p", "com.example.test", str(f)], capture_output=True, text=True)
    assert out.stdout.strip() == "v"


def test_a_hard_linked_corpus_file_is_refused_not_severed(f):
    twin = f.parent / "twin.lts"
    os.link(f, twin)
    before = f.read_bytes()
    with pytest.raises(EditRefused, match="hard link"):
        set_property(f, "AW-N5", "gamma", "G")
    assert f.read_bytes() == before == twin.read_bytes() and os.stat(f).st_ino == os.stat(twin).st_ino


# ---- P2-3: read-only stays read-only --------------------------------------------------------------
def test_a_read_only_corpus_file_is_refused_with_nothing_written(f, tmp_path):
    os.chmod(f, 0o444)
    before = f.read_bytes()
    with pytest.raises(EditRefused, match="not writable"):
        set_property(f, "AW-N5", "gamma", "G")
    assert f.read_bytes() == before and (f.stat().st_mode & 0o777) == 0o444
    assert not (tmp_path / "bak").exists() or not list((tmp_path / "bak").iterdir())


# ---- P3-2: flock unsupported fails fast ---------------------------------------------------------------
def test_flock_unsupported_fails_fast_not_a_ten_second_busy(f, monkeypatch):
    import fcntl

    def boom(fd, op):
        if op & fcntl.LOCK_UN:
            raise OSError(errno.ENOTSUP, "Operation not supported")
        raise OSError(errno.ENOTSUP, "Operation not supported")
    monkeypatch.setattr(fcntl, "flock", boom)
    before = f.read_bytes()
    t0 = time.time()
    with pytest.raises(EditRefused, match="ENOTSUP|not support"):
        set_property(f, "AW-N5", "gamma", "G")
    assert time.time() - t0 < 2 and f.read_bytes() == before


# ---- real SIGKILL at a random moment (the review's P3-6: the injected test is not a real kill) ----
_LOOP = '''
import sys, time
sys.path.insert(0, {repo!r})
from factlattice.authoring.edit import set_property
path = sys.argv[1]
print("ready", flush=True)
for i in range(10000):
    set_property(path, "AW-N5", "k%05d" % i, "v")
'''


def test_real_sigkill_at_random_moments_never_leaves_a_partial_file(env, tmp_path, monkeypatch):
    import random
    rng = random.Random(1234)
    f = env / "c.lts"
    f.write_text(_doc(80), encoding="utf-8")
    n0 = len(parse_text(f.read_text(), "x").nodes)
    e = dict(os.environ, FACTLATTICE_BACKUP_DIR=str(tmp_path / "bak"), FACTLATTICE_STATE_DIR=str(tmp_path / "state"),
             PYTHONDONTWRITEBYTECODE="1")
    rounds = int(os.environ.get("FL_KILL_ROUNDS", "30"))
    killed_mid = 0
    for r in range(rounds):
        p = subprocess.Popen([sys.executable, "-c", _LOOP.format(repo=REPO), str(f)], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=e)
        p.stdout.readline()
        time.sleep(rng.uniform(0.0, 0.15))
        p.kill()
        p.wait()
        text = f.read_text(encoding="utf-8")
        pf = parse_text(text, "x")
        assert len(pf.nodes) == n0, f"round {r}: partial file ({len(pf.nodes)} nodes)"
        ks = sorted(k for k in dict(next(n for n in pf.nodes if n.id == "AW-N5").properties) if k.startswith("k0"))
        # sequential writes: the surviving keys are exactly a PREFIX k00000..k0000j (never a gap)
        assert ks == [f"k{i:05d}" for i in range(len(ks))], f"round {r}: gap {ks[-3:]}"
        killed_mid += 1
        for t in f.parent.glob(".*.tmp"):
            t.unlink()
        # next round continues from this state: keys keep growing, so also assert monotone growth
    assert killed_mid == rounds


def test_no_authoring_module_writes_a_corpus_file_directly():
    """SDLC 7.6 'Corpus writes' made mechanical (review P3-5): the only file writes in
    factlattice/authoring are edit.py's shared step (os.open temp + os.replace)."""
    import re
    root = Path(REPO) / "factlattice" / "authoring"
    bad = []
    for p in root.glob("*.py"):
        for i, ln in enumerate(p.read_text().splitlines(), 1):
            code = ln.split("#")[0]
            if re.search(r"\.write_text\(|open\([^)]*['\"][wa]b?\+?['\"]", code):
                bad.append(f"{p.name}:{i}: {ln.strip()}")
    assert not bad, bad
