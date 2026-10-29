"""[ISSUE-WRITE-ATOMICITY] typed writes are atomic and serialised per file.

RED-first, measured on 25541f6 (see the commit message for the rates):
  (a) two PROCESSES add DIFFERENT properties to the SAME node from the same base, released at the
      same wall-clock instant. The current writer reads, gates and truncate-writes with no lock, so
      one update is silently lost. Asserted over ROUNDS fresh temp dirs, never one run.
  (b) a writer SIGKILLed mid-write. The fault is injected by patching the write primitives the
      writer uses (Path.write_text for the old in-place writer; os.write for the temp-file
      writer), so the SAME test runs against both code shapes; it is not vacuous on the fix.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from factlattice.authoring import edit
from factlattice.authoring.edit import EditRefused, set_property, add_node, repair_property
from factlattice.parser import parse_text

ROUNDS = int(os.environ.get("FL_ATOMIC_ROUNDS", "30"))
REPO = str(Path(__file__).resolve().parents[1])


def _doc(n_nodes: int = 40) -> str:
    out = ["# Doc [SPEC:AW-ROOT]\n", "- kind: root\n"]
    for i in range(n_nodes):
        out.append(f"\n## Node {i} [AW-N{i}]\n\n- kind: x\n- note: filler line {i} " + "x" * 60 + "\n")
    return "".join(out)


def _props(path: Path) -> dict:
    return {n.id: dict(n.properties) for n in parse_text(path.read_text(encoding="utf-8"), str(path)).nodes}


_CHILD = textwrap.dedent("""
    import json, sys, time
    sys.path.insert(0, {repo!r})
    from factlattice.authoring.edit import set_property, EditRefused
    path, key, value, start = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
    while time.time() < start:
        pass
    try:
        set_property(path, "AW-N5", key, value)
        print(json.dumps({{"ok": True}}))
    except EditRefused as e:
        print(json.dumps({{"ok": False, "refused": str(e)[:200]}}))
""")


def _race_round(tmp_path: Path, i: int) -> dict:
    d = tmp_path / f"round{i}"
    d.mkdir()
    (d / "corpus").mkdir()
    f = d / "corpus" / "c.lts"
    f.write_text(_doc(), encoding="utf-8")
    env = dict(os.environ, FACTLATTICE_BACKUP_DIR=str(d / "bak"), FACTLATTICE_STATE_DIR=str(d / "state"))
    start = time.time() + 1.2
    procs = [subprocess.Popen([sys.executable, "-c", _CHILD.format(repo=REPO), str(f), k, v, str(start)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
             for k, v in (("alpha", "A"), ("beta", "B"))]
    outs = [json.loads(p.communicate(timeout=60)[0].strip().splitlines()[-1]) for p in procs]
    got = _props(f)["AW-N5"]
    return {"alpha": got.get("alpha") == "A", "beta": got.get("beta") == "B", "outs": outs}


def test_two_processes_adding_different_properties_lose_no_update(tmp_path):
    lost, silent, detail = 0, 0, []
    for i in range(ROUNDS):
        r = _race_round(tmp_path, i)
        if not (r["alpha"] and r["beta"]):
            lost += 1
            detail.append(r)
            # SILENT = the writer that lost its change was TOLD it succeeded
            silent += sum(1 for o, k in zip(r["outs"], ("alpha", "beta")) if o["ok"] and not r[k])
    print(f"\nLOST-UPDATE RATE: {lost}/{ROUNDS} rounds ({silent} writers told 'ok' whose change is gone)")
    assert lost == 0, f"lost updates in {lost}/{ROUNDS} rounds; e.g. {detail[:2]}"


_KILL_CHILD = textwrap.dedent("""
    import os, signal, sys, pathlib
    sys.path.insert(0, {repo!r})
    from factlattice.authoring import edit
    path = sys.argv[1]
    def die(): os.kill(os.getpid(), signal.SIGKILL)
    # old in-place writer: truncate+write half, then die
    _wt = pathlib.Path.write_text
    def wt(self, data, *a, **k):
        if str(self).endswith("c.lts"):
            with open(self, "w") as fh:
                fh.write(data[: len(data) // 2]); fh.flush()
            die()
        return _wt(self, data, *a, **k)
    pathlib.Path.write_text = wt
    # temp-file writer: write half of the first chunk to the temp fd, then die
    _ow = os.write
    def ow(fd, data):
        _ow(fd, bytes(data)[: len(data) // 2]); die()
    os.write = ow
    # also die right before the rename (temp complete, target untouched)
    mode = sys.argv[2]
    if mode == "before-replace":
        os.write = _ow
        def rp(*a, **k): die()
        os.replace = rp
    edit.set_property(path, "AW-N5", "gamma", "G")
""")


@pytest.mark.parametrize("mode", ["mid-write", "before-replace"])
def test_a_writer_killed_mid_write_leaves_the_old_or_the_new_file_never_a_partial(tmp_path, mode):
    (tmp_path / "corpus").mkdir()
    f = tmp_path / "corpus" / "c.lts"
    old = _doc()
    f.write_text(old, encoding="utf-8")
    env = dict(os.environ, FACTLATTICE_BACKUP_DIR=str(tmp_path / "bak"), FACTLATTICE_STATE_DIR=str(tmp_path / "state"))
    p = subprocess.run([sys.executable, "-c", _KILL_CHILD.format(repo=REPO), str(f), mode], env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == -9, (p.returncode, p.stderr[-400:])      # the fault really fired
    now = f.read_text(encoding="utf-8")
    assert now == old or "gamma: G" in now, "partial file left behind"
    if now != old:                                                    # if new, it must be COMPLETE
        assert len(parse_text(now, "x").nodes) == len(parse_text(old, "x").nodes)


# ---- new behaviour (cannot be RED-by-rate; they fail on the old code for want of the feature) ----
def test_lock_timeout_refuses_busy_and_writes_nothing(tmp_path, monkeypatch):
    (tmp_path / "corpus").mkdir()
    f = tmp_path / "corpus" / "c.lts"
    f.write_text(_doc(), encoding="utf-8")
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_WRITE_LOCK_TIMEOUT", "0.3")
    before = f.read_bytes()
    with edit._file_lock(f.resolve()):
        t0 = time.time()
        with pytest.raises(EditRefused, match="busy"):
            set_property(f, "AW-N5", "delta", "D")
        assert 0.25 < time.time() - t0 < 5
    assert f.read_bytes() == before
    assert not (tmp_path / "bak").exists() or not list((tmp_path / "bak").iterdir())
    set_property(f, "AW-N5", "delta", "D")                            # lock released: works
    assert _props(f)["AW-N5"]["delta"] == "D"


def test_lock_file_lives_in_the_state_dir_never_the_corpus_dir(tmp_path, monkeypatch):
    f = tmp_path / "corpus" / "c.lts"
    f.parent.mkdir()
    f.write_text(_doc(), encoding="utf-8")
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    set_property(f, "AW-N5", "delta", "D")
    assert sorted(p.name for p in f.parent.iterdir()) == ["c.lts"]    # no sidecar, no temp left
    assert list((tmp_path / "state").rglob("*.lock"))


def test_mode_is_preserved_and_every_writer_takes_the_lock(tmp_path, monkeypatch):
    (tmp_path / "corpus").mkdir()
    f = tmp_path / "corpus" / "c.lts"
    f.write_text(_doc(), encoding="utf-8")
    os.chmod(f, 0o640)
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_WRITE_LOCK_TIMEOUT", "0.2")
    calls = [lambda: set_property(f, "AW-N5", "a1", "v"),
             lambda: set_property(f, "AW-N5", "a1", "v2", mode="append"),
             lambda: add_node(f, "AW-NEW", "New node", properties={"kind": "z"}),
             lambda: repair_property(f, "AW-N5", 1, "x")]
    with edit._file_lock(f.resolve()):
        for c in calls:
            with pytest.raises(EditRefused, match="busy"):
                c()
    set_property(f, "AW-N5", "a1", "v")
    assert (os.stat(f).st_mode & 0o777) == 0o640


def test_a_lock_dir_inside_the_corpus_dir_is_refused(tmp_path, monkeypatch):
    f = tmp_path / "c.lts"
    f.write_text(_doc(), encoding="utf-8")
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path.parent / "bak-x"))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    before = f.read_bytes()
    with pytest.raises(EditRefused, match="INSIDE the corpus"):
        set_property(f, "AW-N5", "delta", "D")
    assert f.read_bytes() == before


def test_stale_temp_from_a_killed_writer_is_swept_but_a_fresh_one_is_not(tmp_path, monkeypatch):
    (tmp_path / "corpus").mkdir()
    f = tmp_path / "corpus" / "c.lts"
    f.write_text(_doc(), encoding="utf-8")
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    stale, fresh = f.parent / ".c.lts.1.aaaaaaaa.tmp", f.parent / ".c.lts.2.bbbbbbbb.tmp"
    stale.write_text("x"); fresh.write_text("y")
    os.utime(stale, (time.time() - 7200,) * 2)
    set_property(f, "AW-N5", "delta", "D")
    assert not stale.exists() and fresh.exists()
