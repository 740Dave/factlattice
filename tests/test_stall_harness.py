"""The stall harness is an INSTRUMENT, so it is tested as one: a positive control per blocked call name (a planted
site that must stall), a negative control (the same call outside the root must NOT stall), release lets every
blocked call finish with its true result, and the server hook refuses to be honoured without its marker.

What this does not show: that the model equals a hung drive (it does not), or that a call form outside the harness's
documented list is blocked (it is not). See the harness module docstring for both lists.
"""
from __future__ import annotations

import fcntl
import io
import json
import os
import shutil
import sys
import time
import types
from pathlib import Path

import pytest

from tests import stall_harness as H
from tests.stall_harness import HarnessError, RealServer, RootStall, Workspace, run_limited

STALLED_AFTER_S = 0.3      # a planted site that has not returned by now is stalled
FINISH_S = 5.0             # after release it must finish within this
ROOT_PY = Path(__file__).resolve().parent.parent


@pytest.fixture()
def root(tmp_path):
    r = tmp_path / "root"
    (r / "sub").mkdir(parents=True)
    (r / "f.txt").write_text("hello", encoding="utf-8")
    (r / "sub" / "g.txt").write_text("x", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    (other / "f.txt").write_text("hello", encoding="utf-8")
    (r / "emptydir").mkdir()
    return r


def _sites(r):
    f = str(r / "f.txt")
    return {
        "stat": lambda: os.stat(f),
        "lstat": lambda: os.lstat(f),
        "scandir": lambda: list(os.scandir(str(r))),
        "listdir": lambda: os.listdir(str(r)),
        "walk": lambda: list(os.walk(str(r))),
        "open": lambda: open(f).read(),
        "os.open": lambda: os.close(os.open(f, os.O_RDONLY)),
        "os.replace": lambda: os.replace(str(r / "sub" / "g.txt"), str(r / "sub" / "g2.txt")),
        "realpath": lambda: os.path.realpath(f),
        "isdir": lambda: os.path.isdir(str(r)),
        "isfile": lambda: os.path.isfile(f),
        "exists": lambda: os.path.exists(f),
        "link": lambda: os.link(f, str(r / "sub" / "hard.txt")),
        "rename": lambda: os.rename(str(r / "sub" / "g.txt"), str(r / "sub" / "g3.txt")),
        "mkdir": lambda: os.mkdir(str(r / "newdir")),
        "rmdir": lambda: os.rmdir(str(r / "emptydir")),
        "unlink": lambda: os.unlink(str(r / "sub" / "g.txt")),
        "remove": lambda: os.remove(str(r / "sub" / "g.txt")),
        "access": lambda: os.access(f, os.R_OK),
        "utime": lambda: os.utime(f, None),
        "FileIO": lambda: io.FileIO(f).close(),
        # by descriptor: the descriptor is opened under the root BEFORE the stall engages (RootStall(..., engage=False) in the test)
        "fstat": lambda: os.fstat(_FD[0]),
        "fsync": lambda: os.fsync(_FD[0]),
        "fchmod": lambda: os.fchmod(_FD[0], 0o644),
        "flock": lambda: fcntl.flock(_FD[0], fcntl.LOCK_SH),
    }


_FD: list = [None]


def _path_sites(r):
    f, d = r / "f.txt", r
    return {
        "Path.exists": lambda: f.exists(), "Path.is_file": lambda: f.is_file(), "Path.is_dir": lambda: d.is_dir(),
        "Path.stat": lambda: f.stat(), "Path.resolve": lambda: f.resolve(), "Path.open": lambda: f.open().read(),
        "Path.read_text": lambda: f.read_text(), "Path.read_bytes": lambda: f.read_bytes(),
        "Path.iterdir": lambda: list(d.iterdir()), "Path.glob": lambda: list(d.glob("*")),
        "Path.rglob": lambda: list(d.rglob("*")), "Path.mkdir": lambda: (d / "pm").mkdir(),
        "Path.unlink": lambda: (d / "sub" / "g.txt").unlink(),
    }


def test_the_site_table_covers_every_call_name(root):
    assert set(_sites(root)) == set(H.CALL_NAMES)


def _open_fd_under(stall, path):
    """Open a descriptor while the stall is installed but NOT engaged, so it is recorded and not gated; then engage."""
    stall.disengage()
    fd = os.open(str(path), os.O_RDONLY)
    stall.engage()
    return fd


@pytest.mark.parametrize("name", H.CALL_NAMES)
def test_positive_control_each_call_name_stalls_then_finishes(root, name):
    site = _sites(root)[name]
    with RootStall(root) as stall:
        fd = None
        if name in H.FD_NAMES:
            fd = _FD[0] = _open_fd_under(stall, root / "f.txt")
        out = run_limited(site, STALLED_AFTER_S, name=f"plant-{name}")
        try:
            assert not out.done, f"{name}: the planted site under the stalled root returned (it should be blocked)"
            assert name in stall.hit_names(), (name, stall.hit_names())
        finally:
            stall.release()
        out.thread.join(FINISH_S)
        if fd is not None:
            os.close(fd)
        assert out.done and out.error is None, (name, out.error)


@pytest.mark.parametrize("name", H.CALL_NAMES)
def test_negative_control_same_call_outside_the_root_is_not_stalled(root, tmp_path, name):
    other = tmp_path / "other"
    (other / "sub").mkdir(exist_ok=True)
    (other / "sub" / "g.txt").write_text("x")
    (other / "emptydir").mkdir(exist_ok=True)
    with RootStall(root) as stall:
        fd = None
        if name in H.FD_NAMES:
            fd = _FD[0] = os.open(str(other / "f.txt"), os.O_RDONLY)
        out = run_limited(_sites(other)[name], 2.0, name=f"outside-{name}")
        if fd is not None:
            os.close(fd)
        assert out.done and out.error is None, (name, out.error)
        assert stall.hit_count == 0


def test_a_reused_descriptor_number_does_not_stall_an_unrelated_file(root, tmp_path):
    with RootStall(root) as stall:
        stall.disengage()
        fd = os.open(str(root / "f.txt"), os.O_RDONLY)
        stall.engage()
        os.close(fd)
        fd2 = os.open(str(tmp_path / "other" / "f.txt"), os.O_RDONLY)   # likely the same number
        try:
            out = run_limited(lambda: os.fsync(fd2), 1.0)
            assert out.done, "a descriptor number reused for a file outside the root must not stall"
        finally:
            os.close(fd2)


def test_a_descriptor_opened_before_install_cannot_be_blocked_and_this_is_the_documented_limit(root):
    fd = os.open(str(root / "f.txt"), os.O_RDONLY)
    try:
        with RootStall(root):
            out = run_limited(lambda: os.fstat(fd), 1.0)
            assert out.done, "unmapped descriptor: documented as not blockable"
    finally:
        os.close(fd)


def test_direntry_methods_are_not_blockable_documented_limit(root):
    entries = list(os.scandir(str(root)))                 # listed BEFORE the stall
    with RootStall(root) as stall:
        out = run_limited(lambda: [e.stat() for e in entries] + [e.is_dir() for e in entries], 1.0)
        assert out.done and stall.hit_count == 0, "DirEntry methods bypass every Python name: documented as not blockable"


def test_a_symlink_spelled_outside_the_root_that_points_into_it_stalls(root, tmp_path):
    link = tmp_path / "elsewhere"
    link.symlink_to(root)
    with RootStall(root) as stall:
        out = run_limited(lambda: os.stat(str(link / "f.txt")), STALLED_AFTER_S)
        try:
            assert not out.done
        finally:
            stall.release()
        out.thread.join(FINISH_S)
        assert out.done


def test_hits_are_capped_but_counted(root):
    with RootStall(root, slow_s=0.0001) as stall:
        for _ in range(H.HITS_KEPT + 50):
            os.stat(str(root / "f.txt"))
        assert len(stall.hits) == H.HITS_KEPT and stall.hit_count == H.HITS_KEPT + 50


@pytest.mark.parametrize("name", sorted(_path_sites(Path("/nonexistent-never-used"))))
def test_positive_control_each_path_form_stalls_then_finishes(root, name):
    site = _path_sites(root)[name]
    with RootStall(root) as stall:
        out = run_limited(site, STALLED_AFTER_S, name=f"plant-{name}")
        try:
            assert not out.done, f"{name}: returned under the stalled root"
            assert stall.hits, name
        finally:
            stall.release()
        out.thread.join(FINISH_S)
        assert out.done and out.error is None, (name, out.error)


def test_os_replace_stalls_when_only_the_destination_is_under_the_root(root, tmp_path):
    src = tmp_path / "other" / "f.txt"
    with RootStall(root) as stall:
        out = run_limited(lambda: os.replace(str(src), str(root / "landed.txt")), STALLED_AFTER_S)
        try:
            assert not out.done
        finally:
            stall.release()
        out.thread.join(FINISH_S)
        assert out.done and (root / "landed.txt").is_file()


def test_a_slash_var_spelling_of_the_root_also_stalls(tmp_path):
    # the root given under its real path, the call made through a symlinked spelling of its parent
    real = tmp_path / "real"
    (real / "r").mkdir(parents=True)
    (real / "r" / "f").write_text("x")
    link = tmp_path / "link"
    link.symlink_to(real)
    with RootStall(link / "r") as stall:
        out = run_limited(lambda: os.stat(str(real / "r" / "f")), STALLED_AFTER_S)
        try:
            assert not out.done
        finally:
            stall.release()
        out.thread.join(FINISH_S)
        assert out.done


def test_the_rebinding_sweep_catches_a_from_os_import_binding(root):
    mod = types.ModuleType("factlattice.fake_swept")
    mod.stat = os.stat                                   # what `from os import stat` leaves behind
    sys.modules["factlattice.fake_swept"] = mod
    try:
        with RootStall(root) as stall:
            assert ("factlattice.fake_swept", "stat") in stall.rebound
            out = run_limited(lambda: mod.stat(str(root / "f.txt")), STALLED_AFTER_S)
            try:
                assert not out.done
            finally:
                stall.release()
            out.thread.join(FINISH_S)
            assert out.done
        assert mod.stat is H._REAL["os.stat"], "the sweep must put the original back"
    finally:
        sys.modules.pop("factlattice.fake_swept", None)


def test_uninstall_restores_every_original(root):
    before = (os.stat, os.lstat, os.scandir, os.listdir, os.walk, os.open, os.replace, os.path.realpath,
              os.path.isdir, os.path.isfile, os.path.exists, __import__("builtins").open, __import__("io").open)
    with RootStall(root):
        assert os.stat is not before[0]
    after = (os.stat, os.lstat, os.scandir, os.listdir, os.walk, os.open, os.replace, os.path.realpath,
             os.path.isdir, os.path.isfile, os.path.exists, __import__("builtins").open, __import__("io").open)
    assert all(a is b for a, b in zip(before, after))
    assert os.stat in os.supports_follow_symlinks


def test_a_cap_ends_a_block_nobody_released(root):
    with RootStall(root, cap_s=0.2):
        out = run_limited(lambda: os.stat(str(root / "f.txt")), 3.0)
        assert out.done, "the hard cap must end a block that was never released"


def test_slow_mode_delays_but_does_not_hang(root):
    with RootStall(root, slow_s=0.15):
        out = run_limited(lambda: os.stat(str(root / "f.txt")), 3.0)
        assert out.done and out.elapsed >= 0.14




# ---- the real-server form: launcher, confirmation, and the ways it could silently not apply ---------------
def test_real_server_with_the_launcher_stalls_a_named_read_until_released(tmp_path):
    """The engine is unmodified: no seam, no variable. A named read of the stalled corpus blocks, release lets it finish with
    its true answer (so the block was the harness, not a crash)."""
    ws = Workspace(tmp_path / "w")
    with RealServer(ws, stall_root=ws.root("b1"), engaged=False) as srv:
        assert srv.installed_file.exists(), "start() returned only after the child confirmed the block"
        assert srv.imported_package().startswith(str(ROOT_PY)), srv.imported_package()
        srv.initialize(timeout=30)
        res, _ = srv.call("lts_find", {"corpus": "b1", "text": "lanternfish", "tier": "digest"}, timeout=10)
        assert res.get("count") == 1, res                # not engaged yet: the model is idle
        srv.engage()
        rid = srv.send_call("lts_find", {"corpus": "b1", "text": "lanternfish", "tier": "digest"})
        with pytest.raises(H.NoReply):
            srv.wait_reply(rid, 1.5)
        srv.require_hit()
        srv.release()
        late = srv.unwrap(srv.wait_reply(rid, 10))
        assert late.get("count") == 1, late
    assert "factlattice_engine/server.py" not in str(ROOT_PY / "tests"), "sanity"


def test_the_engine_is_unmodified(tmp_path):
    """No engine seam: no engine file refers to the harness, the site hook, a marker or a hook variable."""
    for f in list((ROOT_PY / "factlattice_engine").rglob("*.py")) + list((ROOT_PY / "factlattice").rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        for needle in ("stall_harness", "stall_site", "FL_STALL_HARNESS", "FACTLATTICE_TEST_STALL"):
            assert needle not in text, (f, needle)
    text = (ROOT_PY / "factlattice_engine" / "server.py").read_text(encoding="utf-8")
    for needle in ("STALL", "HOOK", "FACTLATTICE_TEST"):
        assert needle not in text, needle
    assert not (ROOT_PY / "tests" / "STALL_HOOK_MARKER").exists()


def test_EXPERIMENT_1_a_block_that_did_not_apply_raises_harness_error(tmp_path):
    """The silent case: the plain server started although a stall was asked for (what the reader did by removing the marker).
    Before this fix five of six red records read XFAIL. Now start() refuses."""
    ws = Workspace(tmp_path / "w")
    with pytest.raises(HarnessError) as ei:
        RealServer(ws, stall_root=ws.root("b1"), install=False, confirm_s=1.5).start()
    print("EXPERIMENT 1 ->", type(ei.value).__name__, str(ei.value)[:160])
    assert "not confirmed installed" in str(ei.value)


def test_EXPERIMENT_2_a_child_that_dies_at_startup_raises_harness_error(tmp_path):
    """The reader's second case: a child that dies at startup left c1 and c11 XFAIL as 'boot blocked'."""
    ws = Workspace(tmp_path / "w")
    empty = tmp_path / "brokenpkg"                      # a tree whose engine package raises on import: the child dies after
    (empty / "factlattice_engine").mkdir(parents=True)  # the block is installed and before it can serve (an installed copy of
    (empty / "factlattice_engine" / "__init__.py").write_text("raise ImportError('planted startup failure')\n")  # the engine
    (empty / "factlattice").mkdir()                     # elsewhere on the path must not rescue it: PYTHONPATH wins
    (empty / "factlattice" / "__init__.py").write_text("")
    with pytest.raises(HarnessError) as ei:
        with RealServer(ws, stall_root=ws.root("b1"), engaged=True, code_root=empty) as srv:
            srv.initialize(timeout=5)
    print("EXPERIMENT 2 ->", type(ei.value).__name__, str(ei.value)[:200])
    assert "gone" in str(ei.value) or "exited" in str(ei.value)


def test_a_malformed_launcher_spec_is_a_harness_error_not_a_blocked_server(tmp_path):
    ws = Workspace(tmp_path / "w")
    srv = RealServer(ws, stall_root=ws.root("b1"), engaged=False, confirm_s=1.5)
    srv.child_env[H.SPEC_ENV] = "{not json"            # the start-up hook fails; the interpreter continues without a block
    with pytest.raises(HarnessError) as ei:
        srv.start()
    assert "not confirmed installed" in str(ei.value)


def test_require_hit_refuses_a_run_where_nothing_was_blocked(tmp_path):
    ws = Workspace(tmp_path / "w")
    with RealServer(ws, stall_root=ws.root("b1"), engaged=False) as srv:
        srv.initialize(timeout=30)
        srv.call("lts_find", {"corpus": "a1", "text": "lanternfish", "tier": "digest"}, timeout=10)
        with pytest.raises(HarnessError):
            srv.require_hit()
        srv.require_hit("nothing is expected: the model was never engaged")


# ---- round 3: stale confirmations, foreign pids, re-exec, counters --------------------------------------
def test_P1_a_second_server_on_the_same_workspace_cannot_inherit_the_first_ones_confirmation_or_hits(tmp_path):
    """The reviewer's stale-confirmation probe as a test. Run 1 is a genuine stalled server and is closed. Run 2 on the SAME
    workspace asks for a stall but starts the plain server (install=False): it must raise, not return on run 1's files."""
    ws = Workspace(tmp_path / "w")
    s1 = RealServer(ws, stall_root=ws.root("b1"), engaged=False).start()
    try:
        s1.initialize(timeout=30)
        s1.engage()
        rid = s1.send_call("lts_find", {"corpus": "b1", "text": "lanternfish", "tier": "digest"})
        with pytest.raises(H.NoReply):
            s1.wait_reply(rid, 1.0)
        assert s1.hit_total() > 0
    finally:
        s1.close()
    with pytest.raises(HarnessError) as ei:
        RealServer(ws, stall_root=ws.root("b1"), install=False, confirm_s=1.5).start()
    assert "not confirmed installed" in str(ei.value)
    # and a properly installed run 2 starts from ZERO hits: run 1's hits are not its hits
    with RealServer(ws, stall_root=ws.root("b1"), engaged=False) as s2:
        s2.initialize(timeout=30)
        assert s2.hit_total() == 0 and s2.hits() == []
        with pytest.raises(HarnessError):
            s2.require_hit()
        assert s2.ctl != s1.ctl


def test_P1_a_confirmation_for_another_pid_or_other_names_is_refused(tmp_path):
    ws = Workspace(tmp_path / "w")
    forged = RealServer(ws, stall_root=ws.root("b1"), install=False, confirm_s=2.0)
    forged.installed_file.write_text(json.dumps({"pid": 1, "wrapped": {"os.stat": True}, "names": list(H.CALL_NAMES)}))
    with pytest.raises(HarnessError) as ei:
        forged.start()
    assert "stale or foreign" in str(ei.value)
    partial = RealServer(ws, stall_root=ws.root("b1"), engaged=False, names=["stat", "open"])
    with partial as srv:                                    # a partial block asked for and installed is accepted ...
        assert sorted(json.loads(srv.installed_file.read_text())["names"]) == ["open", "stat"]
    other = RealServer(ws, stall_root=ws.root("b1"), engaged=False, names=["stat", "open"])
    other.spec["names"] = list(H.CALL_NAMES)                # ... one that covers other names than asked is refused
    other.child_env[H.SPEC_ENV] = json.dumps({**other.spec, "names": ["stat"]})
    with pytest.raises(HarnessError) as ei2:
        other.start()
    assert "not the names asked for" in str(ei2.value)


def test_P2_the_block_survives_a_re_exec_of_the_server(tmp_path):
    """build_stamp re-execs the server as `python -m factlattice_engine.server` when its code changes, which a launcher script
    would not survive. The start-up hook reinstalls: this test turns the code watch on, edits a COPY's build_stamp, and shows the
    same pid comes back with generation 2 and the stall still applied."""
    ws = Workspace(tmp_path / "w")
    copy = tmp_path / "copy"
    for pkg in ("factlattice", "factlattice_engine"):
        shutil.copytree(ROOT_PY / pkg, copy / pkg, ignore=shutil.ignore_patterns("__pycache__"))
    with RealServer(ws, stall_root=ws.root("b1"), engaged=False, code_root=copy,
                    env={"FACTLATTICE_CODE_WATCH": "1", "FACTLATTICE_CODE_WATCH_INTERVAL": "0.3"}) as srv:
        pid = srv.proc.pid
        srv.initialize(timeout=30)
        res, _ = srv.call("lts_find", {"corpus": "b1", "text": "lanternfish", "tier": "digest"}, timeout=10)
        assert res.get("count") == 1 and srv.generation() == 1
        with open(copy / "factlattice_engine" / "build_stamp.py", "a") as fh:
            fh.write("\n# edited by the re-exec test\n")
        end = time.monotonic() + 15
        while srv.generation() < 2 and time.monotonic() < end:
            time.sleep(0.1)
        assert srv.generation() == 2, "the server did not re-exec (or the block was not reinstalled after it)"
        assert srv.proc.pid == pid and srv.alive()
        srv.engage()
        rid = srv.send_call("lts_find", {"corpus": "b1", "text": "lanternfish", "tier": "digest"})
        with pytest.raises(H.NoReply):
            srv.wait_reply(rid, 1.5)                         # the stall still applies after the exec
        srv.require_hit()
        srv.release()
        assert srv.unwrap(srv.wait_reply(rid, 10)).get("count") == 1


def test_hit_total_counts_past_the_detail_cap(tmp_path):
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "f").write_text("x")
    spec = {"root": str(root), "engage_file": str(ctl / "engage"), "release_file": str(ctl / "release"),
            "log_file": str(ctl / "hits.jsonl"), "count_file": str(ctl / "hits.count"), "slow_s": 0.0001, "names": ["stat"]}
    (ctl / "engage").write_text("1")
    stall = H.install_server_hook(json.dumps(spec))
    try:
        for _ in range(450):
            os.stat(str(root / "f"))
    finally:
        stall.uninstall()
    assert len((ctl / "hits.jsonl").read_text().splitlines()) == 400
    assert (ctl / "hits.count").read_text() == "450"


def test_alias_cache_is_cleared_when_engage_or_release_state_changes(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "f").write_text("x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "link"
    link.symlink_to(elsewhere)
    flag = {"on": False}
    with RootStall(root, engaged_fn=lambda: flag["on"], cap_s=5) as stall:
        flag["on"] = True
        assert run_limited(lambda: os.stat(str(link)), 1.0).done          # cached: link -> elsewhere, not the root
        flag["on"] = False
        link.unlink()
        link.symlink_to(root)                                              # retargeted into the root
        flag["on"] = True                                                  # state change (as a file would): cache cleared
        out = run_limited(lambda: os.stat(str(link / "f")), 0.3)
        try:
            assert not out.done, "a retargeted symlink must stall after the engage state changed"
        finally:
            stall.release()
        out.thread.join(FINISH_S)
