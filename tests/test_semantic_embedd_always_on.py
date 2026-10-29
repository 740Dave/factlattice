"""[MECH-EMBEDD-ALWAYS-ON] `embedd keep` and `embedd launchagent`, acceptance-1..6.
(acceptance-7 is a measurement after the maintainer-gated install; deliberately not here.)

Hermetic: tiny in-test ONNX model; short /tmp run dirs (AF_UNIX path limit); an INJECTED launchctl
runner and plist path (the real launchctl is guarded below: any call to it fails the test); the
real ~/.lts-engine and ~/Library/LaunchAgents are never touched. Processes are killed only by
handles this file started itself.
"""
from __future__ import annotations

import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")

from factlattice_engine.semantic import embedd as D  # noqa: E402
from factlattice_engine.semantic.suggest import SemanticService, _pin_from_env  # noqa: E402
from tests.test_semantic_embedd import env, _client  # noqa: E402,F401  (reused fixtures)

SEM_DIR = os.path.dirname(os.path.abspath(D.__file__))            # .../factlattice_engine/semantic
ENGINE_PKG = os.path.dirname(SEM_DIR)                             # .../factlattice_engine


@pytest.fixture(autouse=True)
def _real_launchctl_is_forbidden(monkeypatch):
    real = subprocess.run

    def guarded(argv, *a, **k):
        if isinstance(argv, (list, tuple)) and argv and os.path.basename(str(argv[0])) == "launchctl":
            raise AssertionError(f"a test reached the REAL launchctl: {argv}")
        return real(argv, *a, **k)
    monkeypatch.setattr(subprocess, "run", guarded)


def _cfg(env):
    root, models, pin = env
    return D.KeepConfig(pin=pin, state_root=os.path.join(root, "st"), model_root=models, max_len=512)


def _start_keep(env, **kw):
    """keep in a thread; returns (thread, result dict, stop event, log list)."""
    stop, log, res = threading.Event(), [], {}
    cfg = _cfg(env)

    def run():
        res["rc"] = D.keep(cfg, stop=stop, log=log.append, **kw)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, res, stop, log


def _sock(env):
    cfg = _cfg(env)
    return os.path.join(cfg.state_root, "run", f"embedd-{D.material_hash(D.material(cfg.pin, 512))}.sock")


def _wait(pred, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# --- acceptance-1 -----------------------------------------------------------------------------
def test_acc1_keep_resolves_the_same_material_as_shared_embedder(env, monkeypatch):
    root, models, pin = env
    pf = os.path.join(root, "pin.json")
    json.dump(asdict(pin), open(pf, "w"))
    monkeypatch.setenv("LTS_SEMANTIC_PIN_FILE", pf)
    monkeypatch.setenv("LTS_ENGINE_STATE_DIR", os.path.join(root, "st"))
    monkeypatch.setenv("LTS_SEMANTIC_EMBEDDER", "shared")
    emb = SemanticService()._get_embedder()                    # exactly what an engine builds
    cfg = D.keep_config_from_env()
    assert cfg.max_len == 512 and cfg.state_root == os.path.join(root, "st")
    srv = D.Server(cfg.state_root, cfg.pin, cfg.model_root, cfg.max_len)
    assert srv.hash == emb.material_hash
    assert srv.sock_path == emb._paths()[1]                    # the socket engines connect to


def test_acc1_default_state_root_and_pin_follow_the_engine_defaults(monkeypatch):
    for k in ("LTS_ENGINE_STATE_DIR", "LTS_SEMANTIC_PIN_FILE", "LTS_SEMANTIC_MODEL"):
        monkeypatch.delenv(k, raising=False)
    cfg = D.keep_config_from_env()
    assert cfg.state_root == os.path.expanduser("~/.lts-engine")
    assert cfg.pin == _pin_from_env()                          # the engine's own default pin
    monkeypatch.setenv("LTS_SEMANTIC_MODEL", "arctic-m15")
    assert D.keep_config_from_env().pin == _pin_from_env()


def test_acc1_a_different_model_env_gives_a_different_socket(env, monkeypatch):
    root, models, pin = env
    pf = os.path.join(root, "pin.json")
    json.dump(dict(asdict(pin), onnx_sha256="0" * 64), open(pf, "w"))
    monkeypatch.setenv("LTS_SEMANTIC_PIN_FILE", pf)
    assert D.material_hash(D.material(D.keep_config_from_env().pin, 512)) != \
        D.material_hash(D.material(pin, 512))


# --- acceptance-2 -----------------------------------------------------------------------------
def test_acc2_keep_never_idle_exits(env, monkeypatch):
    monkeypatch.setenv("LTS_EMBEDD_IDLE_S", "0.3")
    t, res, stop, log = _start_keep(env, watch_s=0.1, takeover_s=0.1)
    try:
        assert D.wait_for_socket(_sock(env), 15)
        time.sleep(1.5)                                        # five times LTS_EMBEDD_IDLE_S
        assert t.is_alive() and os.path.exists(_sock(env))
        c = _client(env)
        assert c.embed(["hello world"], query=True).shape == (1, env[2].dim)   # still serving
    finally:
        stop.set(); t.join(10)
    assert res["rc"] == 0


# --- acceptance-3 -----------------------------------------------------------------------------
def test_acc3_fingerprint_change_exits_75_after_a_clean_shutdown(env):
    fp = {"v": "a"}
    t, res, stop, log = _start_keep(env, fingerprint_fn=lambda: fp["v"], watch_s=0.1, takeover_s=0.1)
    try:
        assert D.wait_for_socket(_sock(env), 15)
        time.sleep(0.4)
        assert t.is_alive()                                    # unchanged fingerprint: keeps serving
        fp["v"] = "b"                                          # a go-live
        t.join(10)
        assert not t.is_alive() and res["rc"] == 75
    finally:
        stop.set(); t.join(10)
    assert not os.path.exists(_sock(env))                      # socket removed
    probe = D.Server(os.path.join(env[0], "st"), env[2], env[1], 512)
    assert probe.acquire_pidfile()                             # pidfile lock released
    probe._cleanup()


def test_acc3_the_fingerprint_covers_exactly_the_named_files(tmp_path):
    pkg = tmp_path / "semantic"
    shutil.copytree(SEM_DIR, pkg, ignore=shutil.ignore_patterns("__pycache__"))
    assert D.source_fingerprint(str(pkg)) == D.source_fingerprint(str(pkg))     # stable
    for name in ("embedd.py", "embedder.py", "index.py", "tokenizer.py", "cards.py", "suggest.py", "__init__.py"):
        before = D.source_fingerprint(str(pkg))
        with open(pkg / name, "a") as fh:
            fh.write("\n# edit\n")
        assert D.source_fingerprint(str(pkg)) != before, name
    before = D.source_fingerprint(str(pkg))
    with open(pkg / "lexrank.py", "a") as fh:
        fh.write("\n# edit\n")
    assert D.source_fingerprint(str(pkg)) == before            # not a vector-shaping file


def _cli_env(env):
    root, models, pin = env
    pf = os.path.join(root, "pin.json")
    json.dump(asdict(pin), open(pf, "w"))
    code = os.path.join(root, "code")
    shutil.copytree(ENGINE_PKG, os.path.join(code, "factlattice_engine"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    # the copy must be a COHERENT pair: the engine now imports `factlattice.registry`, and an editable
    # install of the core may point at ANOTHER checkout that lacks it (the subprocess would then mix trees)
    shutil.copytree(os.path.join(os.path.dirname(ENGINE_PKG), "factlattice"), os.path.join(code, "factlattice"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    return code, dict(os.environ, PYTHONPATH=code, LTS_SEMANTIC_PIN_FILE=pf, LTS_SEMANTIC_MODEL_DIR=models,
                      LTS_ENGINE_STATE_DIR=os.path.join(root, "st"), LTS_EMBEDD_KEEP_WATCH_S="0.2",
                      LTS_EMBEDD_KEEP_TAKEOVER_S="0.2")


def test_acc3_the_real_cli_exits_75_when_its_source_changes_and_0_on_sigterm(env):
    code, e = _cli_env(env)
    argv = [sys.executable, "-m", "factlattice_engine.semantic.embedd", "keep"]
    p = subprocess.Popen(argv, env=e, cwd=env[0], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert D.wait_for_socket(_sock(env), 30), "keep never bound its socket"
        with open(os.path.join(code, "factlattice_engine", "semantic", "tokenizer.py"), "a") as fh:
            fh.write("\n# go-live\n")
        assert p.wait(timeout=20) == 75
        assert not os.path.exists(_sock(env))
    finally:
        if p.poll() is None:
            p.kill(); p.wait()                                 # our own child only
    p = subprocess.Popen(argv, env=e, cwd=env[0], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert D.wait_for_socket(_sock(env), 30)
        p.terminate()
        assert p.wait(timeout=20) == 0
        assert not os.path.exists(_sock(env))
    finally:
        if p.poll() is None:
            p.kill(); p.wait()


# --- acceptance-4 -----------------------------------------------------------------------------
def test_acc4_keep_waits_while_the_pidfile_is_held_then_takes_over(env):
    root, models, pin = env
    holder = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=600)
    ht = threading.Thread(target=holder.serve_forever, daemon=True); ht.start()
    assert D.wait_for_socket(holder.sock_path, 15)
    t, res, stop, log = _start_keep(env, watch_s=0.1, takeover_s=0.1)
    try:
        time.sleep(1.0)
        assert t.is_alive() and "rc" not in res                # did not exit, did not fight
        assert any("waiting" in l for l in log) and not any("serving" in l for l in log)
        socks = [n for n in os.listdir(os.path.dirname(holder.sock_path)) if n.endswith(".sock")]
        assert socks == [os.path.basename(holder.sock_path)]   # no second service
        assert _client(env).embed(["hello"], query=True).shape[0] == 1   # holder still answers
        holder.shutdown(); ht.join(10)                         # the on-demand service idle-exits
        assert _wait(lambda: any("serving" in l for l in log), 15)
        assert D.wait_for_socket(_sock(env), 10)
        assert _client(env).embed(["hello"], query=True).shape[0] == 1   # keep now serves
    finally:
        stop.set(); t.join(10)
    assert res["rc"] == 0


# --- acceptance-5 -----------------------------------------------------------------------------
def _starts_path(env):
    return os.path.join(env[0], "st", "run", "embedd-keep-starts.json")


def _seed_starts(env, stamps):
    os.makedirs(os.path.dirname(_starts_path(env)), exist_ok=True)
    json.dump(stamps, open(_starts_path(env), "w"))


def test_acc5_the_fifth_start_in_600s_exits_0_with_a_logged_reason(env):
    now = 1_000_000.0
    _seed_starts(env, [now - 500, now - 400, now - 300, now - 10])    # four recent starts
    log = []
    rc = D.keep(_cfg(env), stop=threading.Event(), log=log.append, clock=lambda: now,
                watch_s=0.1, takeover_s=0.1)
    assert rc == 0
    assert any("crash-loop" in l for l in log)
    assert not os.path.exists(_sock(env))                      # it did not serve
    assert len(json.load(open(_starts_path(env)))) == 5        # the start was recorded


def test_acc5_fewer_than_five_proceeds(env):
    now = 1_000_000.0
    _seed_starts(env, [now - 500, now - 300, now - 10])        # this is the 4th
    t, res, stop, log = _start_keep(env, clock=lambda: now, watch_s=0.1, takeover_s=0.1)
    try:
        assert D.wait_for_socket(_sock(env), 15)
    finally:
        stop.set(); t.join(10)
    assert res["rc"] == 0 and not any("crash-loop" in l for l in log)


def test_acc5_a_start_after_600s_quiet_proceeds_and_the_record_resets(env):
    old = 1_000_000.0
    _seed_starts(env, [old - 4, old - 3, old - 2, old - 1, old])     # a full crash loop...
    now = old + 601                                                  # ...then 600 s of quiet
    t, res, stop, log = _start_keep(env, clock=lambda: now, watch_s=0.1, takeover_s=0.1)
    try:
        assert D.wait_for_socket(_sock(env), 15)
    finally:
        stop.set(); t.join(10)
    assert res["rc"] == 0 and not any("crash-loop" in l for l in log)
    assert json.load(open(_starts_path(env))) == [now]


def test_acc5_a_corrupt_starts_file_is_treated_as_empty_and_rewritten_0600(env):
    os.makedirs(os.path.dirname(_starts_path(env)), exist_ok=True)
    open(_starts_path(env), "w").write("{not json")
    t, res, stop, log = _start_keep(env, watch_s=0.1, takeover_s=0.1)
    try:
        assert D.wait_for_socket(_sock(env), 15)
    finally:
        stop.set(); t.join(10)
    assert (os.stat(_starts_path(env)).st_mode & 0o777) == 0o600


# --- acceptance-6 -----------------------------------------------------------------------------
def OK_PROBE(python, env):
    return True, ""


class FakeRunner:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.calls, self.rc, self.stdout, self.stderr = [], rc, stdout, stderr

    def __call__(self, argv):
        self.calls.append(list(argv))
        return SimpleNamespace(returncode=self.rc, stdout=self.stdout, stderr=self.stderr)


def _mcp(root, env_extra=None, command="fakepy", entry=True, raw=None):
    """A temp MCP config + a fake interpreter on a temp PATH dir. Never the real ~/.mcp.json."""
    binp = os.path.join(root, "bin")
    os.makedirs(binp, exist_ok=True)
    exe = os.path.join(binp, "fakepy")
    open(exe, "w").write("#!/bin/sh\n"); os.chmod(exe, 0o755)
    path = os.path.join(root, "mcp.json")
    e = {"PYTHONPATH": "/live/checkout", "ACME_ROOT": "/not/copied", "LTS_SEMANTIC_EMBEDDER": "shared"}
    e.update(env_extra or {})
    cfg = {"mcpServers": {"lts-engine": {"command": command, "args": ["-m", "x"], "env": e}} if entry
           else {"other": {"command": "x"}}}
    open(path, "w").write(raw if raw is not None else json.dumps(cfg))
    return path, binp, exe


@pytest.fixture()
def la(env):
    root, models, pin = env
    mcp, binp, exe = _mcp(root)
    plist = os.path.join(root, "LaunchAgents", "io.factlattice.lts-embedd.plist")
    return dict(plist_path=plist, state_root=os.path.join(root, "st"), uid=501, pin=pin,
                mcp_config=mcp, path=binp, probe=OK_PROBE), exe


def test_acc6_the_plist_matches_the_launchd_spec(la):
    la, exe = la
    assert D.launchagent("install", runner=FakeRunner(), out=io.StringIO(), **la) == 0
    p = plistlib.loads(open(la["plist_path"], "rb").read())
    log = os.path.join(la["state_root"], "run", "embedd-keep.log")
    assert p == {
        "Label": "io.factlattice.lts-embedd",
        "ProgramArguments": [exe, "-m", "factlattice_engine.semantic.embedd", "keep"],
        "EnvironmentVariables": {"PYTHONPATH": "/live/checkout"},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    assert (os.stat(la["plist_path"]).st_mode & 0o777) == 0o600
    assert D.LAUNCHD_LABEL == "io.factlattice.lts-embedd"


def test_acc6_install_bootstraps_through_the_injected_runner(la):
    la, _exe = la
    r = FakeRunner()
    assert D.launchagent("install", runner=r, out=io.StringIO(), **la) == 0
    assert r.calls == [["launchctl", "bootstrap", "gui/501", la["plist_path"]]]


def test_acc6_a_failed_bootstrap_is_loud_and_leaves_no_new_plist(la):
    la, _exe = la
    r = FakeRunner(rc=5, stderr="Bootstrap failed: 5: Input/output error")
    out = io.StringIO()
    assert D.launchagent("install", runner=r, out=out, **la) == 1
    assert "Bootstrap failed" in out.getvalue()
    assert not os.path.exists(la["plist_path"])


def test_acc6_uninstall_boots_out_and_removes_the_plist(la):
    la, _exe = la
    D.launchagent("install", runner=FakeRunner(), out=io.StringIO(), **la)
    r = FakeRunner()
    assert D.launchagent("uninstall", runner=r, out=io.StringIO(), **la) == 0
    assert r.calls == [["launchctl", "bootout", "gui/501/io.factlattice.lts-embedd"]]
    assert not os.path.exists(la["plist_path"])


def test_acc6_uninstall_when_not_loaded_still_removes_the_plist_and_says_so(la):
    la, _exe = la
    D.launchagent("install", runner=FakeRunner(), out=io.StringIO(), **la)
    out = io.StringIO()
    assert D.launchagent("uninstall", runner=FakeRunner(rc=3, stderr="No such process"), out=out, **la) == 0
    assert not os.path.exists(la["plist_path"]) and "not loaded" in out.getvalue()


def test_acc6_status_prints_loaded_pid_and_material(la):
    la, _exe = la
    r = FakeRunner(stdout="io.factlattice.lts-embedd = {\n\tstate = running\n\tpid = 4242\n}\n")
    out = io.StringIO()
    assert D.launchagent("status", runner=r, out=out, **la) == 0
    assert r.calls == [["launchctl", "print", "gui/501/io.factlattice.lts-embedd"]]
    text = out.getvalue()
    assert "loaded: yes" in text and "pid: 4242" in text
    assert f"material: {D.material_hash(D.material(la['pin'], 512))}" in text
    out = io.StringIO()
    D.launchagent("status", runner=FakeRunner(rc=113, stderr="Could not find service"), out=out, **la)
    assert "loaded: no" in out.getvalue()


def test_acc6_the_cli_routes_through_the_injected_runner(la):
    la, _exe = la
    os.environ  # path comes from the injected PATH below
    r = FakeRunner()
    rc = D._main(["launchagent", "install", "--plist-path", la["plist_path"], "--state-root",
                  la["state_root"], "--mcp-config", la["mcp_config"], "--path", la["path"]], runner=r, probe=OK_PROBE)
    assert rc == 0 and r.calls[0][:3] == ["launchctl", "bootstrap", f"gui/{os.getuid()}"]
    assert os.path.exists(la["plist_path"])


# --- label migration: an earlier release installed the agent under another reverse-DNS prefix --------
OLD_LABEL = "com.example.lts-embedd"      # shape only: recognised by shape + exact content, never by name


def _old_agent(la, exe, label=OLD_LABEL, env=None, fname=None, **override):
    """The plist an earlier release would have written: identical to this install's apart from Label."""
    d = os.path.dirname(la["plist_path"])
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, fname or (label + ".plist"))
    pl = D.build_plist(python=exe, env=env or {"PYTHONPATH": "/live/checkout"},
                       log_path=os.path.join(la["state_root"], "run", "embedd-keep.log"))
    pl["Label"] = label
    pl.update(override)
    with open(f, "wb") as fh:
        plistlib.dump(pl, fh)
    return f


def _alarm(seconds=10):
    import signal

    def boom(*_a):
        raise AssertionError(f"hung for {seconds} s (a blocking read)")
    signal.signal(signal.SIGALRM, boom)
    signal.alarm(seconds)


class ScriptRunner(FakeRunner):
    """rc per (verb, label-or-path suffix): {('bootout', OLD_LABEL): 5}."""
    def __init__(self, rcs=None):
        super().__init__()
        self.rcs = rcs or {}

    def __call__(self, argv):
        self.calls.append(list(argv))
        rc = 0
        for (verb, tail), v in self.rcs.items():
            if argv[1] == verb and argv[-1].endswith(tail):
                rc = v
        return SimpleNamespace(returncode=rc, stdout="", stderr="boom" if rc else "")


def test_mig1_keep_migrates_an_old_label_plist(la):
    la, exe = la
    old = _old_agent(la, exe)
    r, out = FakeRunner(), io.StringIO()
    assert D.launchagent("install", runner=r, out=out, **la) == 0
    stop = ["launchctl", "bootout", f"gui/501/{OLD_LABEL}"]
    assert stop in r.calls
    assert r.calls.index(stop) < r.calls.index(["launchctl", "bootstrap", "gui/501", la["plist_path"]])
    assert not os.path.exists(old) and os.path.exists(la["plist_path"])
    assert os.listdir(os.path.dirname(la["plist_path"])) == ["io.factlattice.lts-embedd.plist"]
    assert "migrated" in out.getvalue()


def test_mig1b_a_failed_bootstrap_puts_the_old_agent_back_and_says_so(la):
    la, exe = la
    old = _old_agent(la, exe)
    r, out = ScriptRunner({("bootstrap", "io.factlattice.lts-embedd.plist"): 5}), io.StringIO()
    assert D.launchagent("install", runner=r, out=out, **la) == 1
    assert os.path.exists(old) and not os.path.exists(la["plist_path"])
    assert ["launchctl", "bootstrap", "gui/501", old] in r.calls
    assert f"restored the earlier agent {OLD_LABEL}" in out.getvalue()


def test_mig1c_a_failed_restore_of_the_old_agent_is_reported(la):
    la, exe = la
    old = _old_agent(la, exe)
    r, out = ScriptRunner({("bootstrap", ".plist"): 5}), io.StringIO()      # both bootstraps fail
    assert D.launchagent("install", runner=r, out=out, **la) == 1
    assert f"COULD NOT restore the earlier agent {OLD_LABEL}" in out.getvalue()
    assert os.path.exists(old)


def test_mig1d_any_exception_in_the_window_restores_the_old_agent(la, monkeypatch):
    la, exe = la
    old = _old_agent(la, exe)
    real = os.replace

    def bad_replace(src, dst, *a, **k):
        if dst == la["plist_path"]:
            raise OSError("disk full")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "replace", bad_replace)
    r = FakeRunner()
    with pytest.raises(OSError):
        D.launchagent("install", runner=r, out=io.StringIO(), **la)
    assert ["launchctl", "bootstrap", "gui/501", old] in r.calls          # old agent back in service
    assert os.path.exists(old)


def test_mig2_nothing_present_installs_only_the_new_label(la):
    la, _exe = la
    r = FakeRunner()
    assert D.launchagent("install", runner=r, out=io.StringIO(), **la) == 0
    assert r.calls == [["launchctl", "bootstrap", "gui/501", la["plist_path"]]]
    assert os.listdir(os.path.dirname(la["plist_path"])) == ["io.factlattice.lts-embedd.plist"]


def test_mig3_reinstall_is_idempotent_and_leaves_unrelated_plists_alone(la):
    la, _exe = la
    D.launchagent("install", runner=FakeRunner(), out=io.StringIO(), **la)
    d = os.path.dirname(la["plist_path"])
    with open(os.path.join(d, "com.other.lts-embedd.plist"), "wb") as fh:   # same suffix, a different program
        plistlib.dump({"Label": "com.other.lts-embedd", "ProgramArguments": ["/bin/other"]}, fh)
    with open(os.path.join(d, "com.other.thing.plist"), "wb") as fh:
        plistlib.dump({"Label": "com.other.thing"}, fh)
    names = ("com.other.lts-embedd.plist", "com.other.thing.plist")
    before = {f: open(os.path.join(d, f), "rb").read() for f in names}
    r = FakeRunner()
    assert D.launchagent("install", runner=r, out=io.StringIO(), **la) == 0
    assert r.calls == [["launchctl", "bootstrap", "gui/501", la["plist_path"]]]
    assert sorted(os.listdir(d)) == sorted(("io.factlattice.lts-embedd.plist",) + names)
    assert {f: open(os.path.join(d, f), "rb").read() for f in names} == before


def test_mig4_status_reports_an_old_label_leftover(la):
    la, exe = la
    _old_agent(la, exe)
    r, out = FakeRunner(), io.StringIO()
    assert D.launchagent("status", runner=r, out=out, **la) == 0
    assert ["launchctl", "print", f"gui/501/{OLD_LABEL}"] in r.calls
    assert f"legacy label {OLD_LABEL}: installed" in out.getvalue()
    assert "loaded: yes" in out.getvalue().split("legacy label")[1]


def test_mig5_a_separate_dev_agent_of_the_same_module_is_not_an_earlier_release(la):
    """F1: same module, another label, its own state dir: a different install, never removed."""
    la, exe = la
    dev = _old_agent(la, exe, label="com.dev.lts-embedd",
                     env={"PYTHONPATH": "/dev/checkout", "LTS_ENGINE_STATE_DIR": "/dev/state"})
    before = open(dev, "rb").read()
    r, out = FakeRunner(), io.StringIO()
    assert D.launchagent("install", runner=r, out=out, **la) == 0
    assert r.calls == [["launchctl", "bootstrap", "gui/501", la["plist_path"]]]      # no bootout of the dev agent
    assert open(dev, "rb").read() == before
    assert "com.dev.lts-embedd" in out.getvalue() and "left alone" in out.getvalue()
    out = io.StringIO()
    D.launchagent("status", runner=FakeRunner(), out=out, **la)
    assert "com.dev.lts-embedd" in out.getvalue() and "differs" in out.getvalue()


def test_mig5b_an_explicit_adopt_flag_migrates_a_differing_agent(la):
    la, exe = la
    dev = _old_agent(la, exe, label="com.dev.lts-embedd", env={"PYTHONPATH": "/dev/checkout"})
    r = FakeRunner()
    assert D.launchagent("install", runner=r, out=io.StringIO(), adopt_legacy=["com.dev.lts-embedd"], **la) == 0
    assert ["launchctl", "bootout", "gui/501/com.dev.lts-embedd"] in r.calls and not os.path.exists(dev)


def test_mig5c_the_cli_has_the_adopt_flag(la):
    la, exe = la
    dev = _old_agent(la, exe, label="com.dev.lts-embedd", env={"PYTHONPATH": "/dev/checkout"})
    rc = D._main(["launchagent", "install", "--plist-path", la["plist_path"], "--state-root", la["state_root"],
                  "--mcp-config", la["mcp_config"], "--path", la["path"], "--adopt-legacy", "com.dev.lts-embedd"],
                 runner=FakeRunner(), probe=OK_PROBE)
    assert rc == 0 and not os.path.exists(dev)


def test_mig6_a_plist_not_named_after_its_label_is_not_migrated(la):
    """F6: the `<label>.plist` file-name clause."""
    la, exe = la
    f = _old_agent(la, exe, fname="stray.lts-embedd.plist")        # identical content, wrong file name
    r = FakeRunner()
    assert D.launchagent("install", runner=r, out=io.StringIO(), **la) == 0
    assert r.calls == [["launchctl", "bootstrap", "gui/501", la["plist_path"]]] and os.path.exists(f)


def test_mig7_a_surviving_loaded_old_agent_stops_the_install(la):
    """F2a: bootout failed and the agent is still loaded: do not bootstrap a second one."""
    la, exe = la
    old = _old_agent(la, exe)
    r, out = ScriptRunner({("bootout", OLD_LABEL): 5}), io.StringIO()     # print rc 0 = still loaded
    assert D.launchagent("install", runner=r, out=out, **la) == 1
    assert not os.path.exists(la["plist_path"]) and os.path.exists(old)
    assert not any(c[1] == "bootstrap" for c in r.calls)
    assert "still loaded" in out.getvalue()


def test_mig7b_a_bootout_of_a_not_loaded_old_agent_proceeds(la):
    la, exe = la
    old = _old_agent(la, exe)
    r = ScriptRunner({("bootout", OLD_LABEL): 3, ("print", OLD_LABEL): 113})   # not loaded
    assert D.launchagent("install", runner=r, out=io.StringIO(), **la) == 0
    assert not os.path.exists(old) and os.path.exists(la["plist_path"])
    assert ["launchctl", "bootstrap", "gui/501", old] not in r.calls


def test_mig8_a_symlinked_legacy_plist_is_reported_and_refused(la):
    """F2b: cannot be classified, so install refuses (no agent or plist written) and status says so."""
    la, exe = la
    real = _old_agent(la, exe, fname="real.txt")
    link = os.path.join(os.path.dirname(real), OLD_LABEL + ".plist")
    os.symlink(real, link)
    r, out = FakeRunner(), io.StringIO()
    assert D.launchagent("install", runner=r, out=out, **la) == 1
    assert r.calls == [] and not os.path.exists(la["plist_path"]) and "symlink" in out.getvalue()
    out = io.StringIO()
    D.launchagent("status", runner=FakeRunner(), out=out, **la)
    assert "cannot classify" in out.getvalue() and "symlink" in out.getvalue()


def test_mig9_a_failed_unlink_of_the_old_plist_is_loud(la, monkeypatch):
    """F2d: the new agent is up but the old plist stayed: both would load at login."""
    la, exe = la
    old = _old_agent(la, exe)
    real = os.unlink

    def bad_unlink(p, *a, **k):
        if p == old:
            raise PermissionError("denied")
        return real(p, *a, **k)
    monkeypatch.setattr(os, "unlink", bad_unlink)
    out = io.StringIO()
    assert D.launchagent("install", runner=FakeRunner(), out=out, **la) == 1
    assert "COULD NOT remove" in out.getvalue() and old in out.getvalue()


def test_mig10_a_fifo_named_like_a_legacy_plist_does_not_hang(la):
    """F4: a blocking read of a FIFO. SIGALRM turns a hang into a failure."""
    la, exe = la
    d = os.path.dirname(la["plist_path"])
    os.makedirs(d, exist_ok=True)
    os.mkfifo(os.path.join(d, OLD_LABEL + ".plist"))
    _alarm(10)
    try:
        r, out = FakeRunner(), io.StringIO()
        assert D.launchagent("install", runner=r, out=out, **la) == 1
        D.launchagent("status", runner=FakeRunner(), out=io.StringIO(), **la)
    finally:
        import signal
        signal.alarm(0)
    assert r.calls == [] and "not a regular file" in out.getvalue()
    assert not os.path.exists(os.path.join(la["state_root"], "run", "embedd-keep.log"))   # no log created either


def _install_ok(la):
    assert D.launchagent("install", runner=FakeRunner(), out=io.StringIO(), **la) == 0


# --- install-environment (MCP config) ----------------------------------------------------------
def test_ie_install_copies_the_material_env_from_the_mcp_entry(env):
    root, models, pin = env
    mcp, binp, exe = _mcp(root, {"LTS_SEMANTIC_MODEL": "m", "LTS_SEMANTIC_PIN_FILE": "/p.json",
                                 "LTS_ENGINE_STATE_DIR": os.path.join(root, "mst"), "FACTLATTICE_CORPUS_ROOT": "/no"})
    plist = os.path.join(root, "la", "x.plist")
    r = FakeRunner()
    assert D.launchagent("install", runner=r, out=io.StringIO(), plist_path=plist, state_root=os.path.join(root, "st"),
                         uid=1, mcp_config=mcp, path=binp, probe=OK_PROBE) == 0
    p = plistlib.loads(open(plist, "rb").read())
    assert p["EnvironmentVariables"] == {"PYTHONPATH": "/live/checkout", "LTS_SEMANTIC_MODEL": "m",
                                         "LTS_SEMANTIC_PIN_FILE": "/p.json", "LTS_ENGINE_STATE_DIR": os.path.join(root, "mst")}
    assert p["ProgramArguments"][0] == exe                      # the command, resolved via the injected PATH


@pytest.mark.parametrize("why,kw,needle", [
    ("missing config", dict(missing=True), "MCP config"),
    ("no lts-engine entry", dict(entry=False), "lts-engine"),
    ("unresolvable command", dict(command="no-such-interpreter-xyz"), "no-such-interpreter-xyz"),
    ("unparseable config", dict(raw="{nope"), "MCP config"),
    ("no PYTHONPATH", dict(nopp=True), "PYTHONPATH"),
])
def test_ie_install_refuses_loudly_and_writes_nothing(env, why, kw, needle):
    root, models, pin = env
    kw = dict(kw)
    missing, nopp = kw.pop("missing", False), kw.pop("nopp", False)
    mcp, binp, exe = _mcp(root, **kw)
    if missing:
        os.unlink(mcp)
    if nopp:
        cfg = json.load(open(mcp)); del cfg["mcpServers"]["lts-engine"]["env"]["PYTHONPATH"]
        json.dump(cfg, open(mcp, "w"))
    plist = os.path.join(root, "la", "x.plist")
    r, out = FakeRunner(), io.StringIO()
    rc = D.launchagent("install", runner=r, out=out, plist_path=plist, state_root=os.path.join(root, "st"),
                       uid=1, mcp_config=mcp, path=binp, probe=OK_PROBE)
    assert rc == 1 and needle in out.getvalue() and "refus" in out.getvalue().lower(), why
    assert r.calls == [] and not os.path.exists(plist) and not os.path.exists(os.path.dirname(plist))
    assert not os.path.exists(os.path.join(root, "st"))


def test_ie_the_default_mcp_config_is_home_mcp_json(monkeypatch):
    # the module constant is resolved at IMPORT; the per-test HOME isolation (TEST-CONFIG-ISOLATION) moves HOME
    # afterwards, so the expectation is taken from the HOME the session started with
    from tests import _state_guard as G
    assert D.DEFAULT_MCP_CONFIG == os.path.join(G.REAL_HOME, ".mcp.json")


def test_ie_status_reports_in_sync_then_drift_on_env_and_interpreter(la):
    la, exe = la
    _install_ok(la)
    out = io.StringIO()
    D.launchagent("status", runner=FakeRunner(), out=out, **la)
    assert "in sync" in out.getvalue() and "drift" not in out.getvalue()
    cfg = json.load(open(la["mcp_config"]))
    cfg["mcpServers"]["lts-engine"]["env"]["LTS_SEMANTIC_MODEL"] = "other"     # engines moved on
    json.dump(cfg, open(la["mcp_config"], "w"))
    out = io.StringIO()
    D.launchagent("status", runner=FakeRunner(), out=out, **la)
    assert "drift:" in out.getvalue() and "LTS_SEMANTIC_MODEL" in out.getvalue()
    cfg["mcpServers"]["lts-engine"]["env"].pop("LTS_SEMANTIC_MODEL")
    json.dump(cfg, open(la["mcp_config"], "w"))
    other = os.path.join(os.path.dirname(exe), "otherpy")
    shutil.copy(exe, other)
    cfg["mcpServers"]["lts-engine"]["command"] = "otherpy"
    json.dump(cfg, open(la["mcp_config"], "w"))
    out = io.StringIO()
    D.launchagent("status", runner=FakeRunner(), out=out, **la)
    assert "drift:" in out.getvalue() and "interpreter" in out.getvalue()


def test_ie_status_with_an_unreadable_config_says_drift_with_the_reason(la):
    la, _ = la
    _install_ok(la)
    os.unlink(la["mcp_config"])
    out = io.StringIO()
    D.launchagent("status", runner=FakeRunner(), out=out, **la)
    assert "drift:" in out.getvalue() and "MCP config" in out.getvalue()


def test_ie_the_keep_source_files_comment_points_at_the_spec_obligation():
    src = open(D.__file__).read()
    i = src.index("KEEP_SOURCE_FILES = (")
    assert "obligation" in src[max(0, i - 400):i].lower() and "install-environment" in src[max(0, i - 400):i]


def test_acc6_the_guard_can_fail_the_real_launchctl_is_refused():
    with pytest.raises(AssertionError, match="REAL launchctl"):
        subprocess.run(["launchctl", "list"])


# --- review fixes (audit-MECH-EMBEDD-ALWAYS-ON-2026-10-03) ------------------------------------
def _watch_case(env, monkeypatch, mutate):
    """keep with the DEFAULT (broadened) fingerprint; `mutate()` is called once it is serving."""
    t, res, stop, log = _start_keep(env, watch_s=0.1, takeover_s=0.1)
    try:
        assert D.wait_for_socket(_sock(env), 15)
        time.sleep(0.5)
        assert t.is_alive(), "the fingerprint moved with no change made"
        mutate()
        t.join(10)
        assert not t.is_alive() and res["rc"] == 75
    finally:
        stop.set(); t.join(10)
    assert not os.path.exists(_sock(env))


def test_p1_a_stable_environment_keeps_serving_under_the_default_fingerprint(env):
    t, res, stop, log = _start_keep(env, watch_s=0.1, takeover_s=0.1)
    try:
        assert D.wait_for_socket(_sock(env), 15)
        time.sleep(1.0)
        assert t.is_alive()
    finally:
        stop.set(); t.join(10)
    assert res["rc"] == 0


@pytest.mark.parametrize("dist", ["onnxruntime", "numpy"])
def test_p1_a_dist_version_change_alone_exits_75(env, monkeypatch, dist):
    ver = {"onnxruntime": "1.0", "numpy": "2.0"}
    monkeypatch.setattr(D, "_dist_version", lambda name: ver[name])
    _watch_case(env, monkeypatch, lambda: ver.__setitem__(dist, "9.9"))


def test_p1_a_pin_file_content_change_alone_exits_75(env, monkeypatch):
    pf = os.path.join(env[0], "pin.json")
    json.dump(asdict(env[2]), open(pf, "w"))
    monkeypatch.setenv("LTS_SEMANTIC_PIN_FILE", pf)
    _watch_case(env, monkeypatch, lambda: json.dump(dict(asdict(env[2]), query_prefix="q: "), open(pf, "w")))


def test_p1_an_interpreter_change_alone_exits_75(env, monkeypatch):
    st = SimpleNamespace(st_mtime_ns=1, st_size=100)
    monkeypatch.setattr(D, "_interpreter_stat", lambda path: st)
    _watch_case(env, monkeypatch, lambda: setattr(st, "st_size", 101))


def test_p1_the_fingerprint_parts_are_each_independent(monkeypatch):
    base = D.watch_fingerprint()
    monkeypatch.setattr(D, "_dist_version", lambda name: "x")
    assert D.watch_fingerprint() != base
    monkeypatch.undo()
    monkeypatch.setattr(D, "_interpreter_stat", lambda path: SimpleNamespace(st_mtime_ns=5, st_size=5))
    assert D.watch_fingerprint() != base


def test_p21_an_unwritable_starts_record_stops_with_exit_0_never_a_restart_loop(env):
    run = os.path.join(env[0], "st", "run")
    os.makedirs(run, mode=0o700, exist_ok=True)
    os.mkdir(os.path.join(run, "embedd-keep-starts.json"))       # the record path is a directory
    log = []
    rc = D.keep(_cfg(env), stop=threading.Event(), log=log.append, watch_s=0.1, takeover_s=0.1)
    assert rc == 0 and any("cannot record" in l for l in log)
    assert not os.path.exists(_sock(env))
    assert [n for n in os.listdir(run) if n.endswith(".tmp")] == []   # no stale temp left


def test_p21_a_permission_error_on_the_record_also_exits_0(env, monkeypatch):
    def boom(*a, **k):
        raise PermissionError("read-only")
    monkeypatch.setattr(D, "_record_start", boom)
    log = []
    assert D.keep(_cfg(env), stop=threading.Event(), log=log.append) == 0
    assert any("cannot record" in l for l in log)


def test_p22_a_failed_reinstall_restores_the_previous_plist_bytes(la):
    la, _ = la
    _install_ok(la)
    before = open(la["plist_path"], "rb").read()
    out = io.StringIO()
    assert D.launchagent("install", runner=FakeRunner(rc=5, stderr="already loaded"), out=out,
                         pythonpath="/somewhere/else", **la) == 1
    assert open(la["plist_path"], "rb").read() == before
    assert (os.stat(la["plist_path"]).st_mode & 0o777) == 0o600
    assert not [n for n in os.listdir(os.path.dirname(la["plist_path"])) if n.endswith(".tmp")]


def test_p23_install_smoke_checks_the_interpreter_with_the_plist_env(la):
    la, exe = la
    calls = []
    la = dict(la, probe=lambda py, env: (calls.append((py, dict(env))) or (True, "")))
    _install_ok(la)
    assert calls == [(exe, {"PYTHONPATH": "/live/checkout"})]


def test_p23_a_failing_smoke_check_refuses_and_writes_nothing(la):
    la, exe = la
    r = FakeRunner()
    out = io.StringIO()
    rc = D.launchagent("install", runner=r, out=out, **dict(la, probe=lambda py, env: (False, "No module named onnxruntime")))
    assert rc == 1 and "REFUSED" in out.getvalue() and "onnxruntime" in out.getvalue()
    assert r.calls == [] and not os.path.exists(la["plist_path"])


def test_p23_the_default_probe_really_runs_the_interpreter(env):
    ok, why = D._default_probe(sys.executable, {"PYTHONPATH": ENGINE_PKG and os.path.dirname(ENGINE_PKG)})
    assert ok, why
    bad = os.path.join(env[0], "bad.sh")
    open(bad, "w").write("#!/bin/sh\necho nope >&2\nexit 3\n"); os.chmod(bad, 0o755)
    ok, why = D._default_probe(bad, {"PYTHONPATH": "/x"})
    assert not ok and "nope" in why


def test_p23_status_reports_an_interpreter_that_cannot_import_the_model_stack(la):
    la, _ = la
    _install_ok(la)
    out = io.StringIO()
    D.launchagent("status", runner=FakeRunner(), out=out, **dict(la, probe=lambda py, env: (False, "boom")))
    assert "drift:" in out.getvalue() and "interpreter cannot import the model stack" in out.getvalue()


def test_p25_status_shows_a_tripped_crash_loop(la):
    la, _ = la
    _install_ok(la)
    now = 2_000_000.0
    run = os.path.join(la["state_root"], "run")
    json.dump([now - 50, now - 40, now - 30, now - 20, now - 10], open(os.path.join(run, "embedd-keep-starts.json"), "w"))
    out = io.StringIO()
    D.launchagent("status", runner=FakeRunner(), out=out, now=now, **la)
    assert "crash-loop: tripped at" in out.getvalue() and "5 starts in 600 s" in out.getvalue() \
        and "on-demand" in out.getvalue()
    json.dump([now - 50, now - 40], open(os.path.join(run, "embedd-keep-starts.json"), "w"))
    out = io.StringIO()
    D.launchagent("status", runner=FakeRunner(), out=out, now=now, **la)
    assert "tripped" not in out.getvalue()


def test_p32_the_log_goes_to_the_mcp_state_dir_and_is_0600(env):
    root, models, pin = env
    mcp, binp, exe = _mcp(root, {"LTS_ENGINE_STATE_DIR": os.path.join(root, "mst")})
    plist = os.path.join(root, "la", "x.plist")
    assert D.launchagent("install", runner=FakeRunner(), out=io.StringIO(), plist_path=plist,
                         state_root=os.path.join(root, "other"), uid=1, mcp_config=mcp, path=binp, probe=OK_PROBE) == 0
    log = os.path.join(root, "mst", "run", "embedd-keep.log")
    assert plistlib.loads(open(plist, "rb").read())["StandardOutPath"] == log
    assert (os.stat(log).st_mode & 0o777) == 0o600
    assert not os.path.exists(os.path.join(root, "other"))


def test_p33_the_model_dir_is_copied_from_the_mcp_entry(env):
    root, models, pin = env
    mcp, binp, exe = _mcp(root, {"LTS_SEMANTIC_MODEL_DIR": "/models"})
    python, e = D._mcp_install_env(mcp, binp, None)
    assert e["LTS_SEMANTIC_MODEL_DIR"] == "/models"


def test_p24_the_checklist_carries_the_always_on_rule():
    txt = open(os.path.join(os.path.dirname(ENGINE_PKG), "SDLC.md")).read()
    sec = txt[txt.index("### 7.6"):txt.index("## 8. Stage 5")]
    for needle in ("KEEP_SOURCE_FILES", "watch_fingerprint", "io.factlattice.lts-embedd", "embedd-keep-starts.json",
                   "embedd-keep-tripped.json", "on_ready"):
        assert needle in sec, needle


# --- Delta review N2 / N3 ---------------------------------------------------------------------
def _marker(env):
    return os.path.join(env[0], "st", "run", "embedd-keep-tripped.json")


def test_n2_tripping_writes_a_durable_0600_marker(env):
    now = 1_000_000.0
    _seed_starts(env, [now - 500, now - 400, now - 300, now - 10])
    assert D.keep(_cfg(env), stop=threading.Event(), log=[].append, clock=lambda: now) == 0
    m = json.load(open(_marker(env)))
    assert m["time"] == now and m["starts"] == 5 and "crash-loop" in m["reason"]
    assert (os.stat(_marker(env)).st_mode & 0o777) == 0o600


def test_n2_a_symlinked_marker_is_replaced_never_written_through(env, tmp_path):
    now = 1_000_000.0
    _seed_starts(env, [now - 500, now - 400, now - 300, now - 10])
    victim = tmp_path / "victim"; victim.write_text("keep me")
    os.symlink(victim, _marker(env))
    D.keep(_cfg(env), stop=threading.Event(), log=[].append, clock=lambda: now)
    assert victim.read_text() == "keep me"
    assert not os.path.islink(_marker(env)) and json.load(open(_marker(env)))["starts"] == 5


def test_n2_status_shows_the_marker_after_the_window_has_decayed(la):
    la, _ = la
    _install_ok(la)
    run = os.path.join(la["state_root"], "run")
    json.dump({"time": 1000.0, "starts": 5, "window_s": 600, "reason": "crash-loop"},
              open(os.path.join(run, "embedd-keep-tripped.json"), "w"))
    out = io.StringIO()
    D.launchagent("status", runner=FakeRunner(), out=out, now=1_000_000.0, **la)    # starts file: empty
    assert "crash-loop: tripped at" in out.getvalue() and "5 starts in 600 s" in out.getvalue() \
        and "on-demand" in out.getvalue()


def test_n2_the_marker_is_cleared_only_when_serving_not_while_waiting(env):
    root, models, pin = env
    run = os.path.join(root, "st", "run")
    os.makedirs(run, mode=0o700, exist_ok=True)
    json.dump({"time": 1.0, "starts": 5, "window_s": 600, "reason": "x"}, open(_marker(env), "w"))
    holder = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=600)
    ht = threading.Thread(target=holder.serve_forever, daemon=True); ht.start()
    assert D.wait_for_socket(holder.sock_path, 15)
    t, res, stop, log = _start_keep(env, watch_s=0.1, takeover_s=0.1)
    try:
        time.sleep(0.8)
        assert any("waiting" in l for l in log) and os.path.exists(_marker(env))   # not serving yet
        holder.shutdown(); ht.join(10)
        assert _wait(lambda: not os.path.exists(_marker(env)), 15)                  # bound and serving
        assert os.path.exists(_sock(env))
    finally:
        stop.set(); t.join(10)


def test_n2_status_says_loaded_but_not_running_when_launchd_reports_no_pid(la):
    la, _ = la
    out = io.StringIO()
    D.launchagent("status", runner=FakeRunner(stdout="x = {\n\tstate = not running\n}\n"), out=out, **la)
    assert "loaded but not running" in out.getvalue()
    out = io.StringIO()
    D.launchagent("status", runner=FakeRunner(stdout="x = {\n\tpid = 7\n}\n"), out=out, **la)
    assert "loaded but not running" not in out.getvalue()


def test_n3_a_refusal_at_server_construction_is_counted_and_trips_the_limit(env):
    root, models, pin = env
    cfg = D.KeepConfig(pin=pin, state_root=os.path.join(root, "s" * 90), model_root=models, max_len=512)
    now, log = 1_000_000.0, []
    rcs = [D.keep(cfg, stop=threading.Event(), log=log.append, clock=lambda: now) for _ in range(5)]
    assert rcs == [2, 2, 2, 2, 0]                     # the 5th start trips: stop restarting
    assert any("crash-loop" in l for l in log)
    assert os.path.exists(os.path.join(cfg.state_root, "run", "embedd-keep-tripped.json"))


def test_n3_a_refusal_before_keep_is_reached_is_counted_too(env, monkeypatch):
    root = env[0]
    monkeypatch.setenv("LTS_ENGINE_STATE_DIR", os.path.join(root, "st"))
    monkeypatch.setenv("LTS_SEMANTIC_MODEL", "no-such-model")          # pin resolution refuses
    assert [D._main(["keep"]) for _ in range(5)] == [2, 2, 2, 2, 0]


def test_n3_an_unusable_run_dir_cannot_count_so_it_stops_with_exit_0(env, tmp_path):
    root = env[0]
    os.makedirs(os.path.join(root, "st"))
    os.symlink(tmp_path, os.path.join(root, "st", "run"))              # a symlinked run dir is refused
    log = []
    assert D.keep(_cfg(env), stop=threading.Event(), log=log.append) == 0
    assert any("cannot" in l for l in log)


# --- Delta-2 review ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["LTS_EMBEDD_KEEP_WATCH_S", "LTS_EMBEDD_KEEP_TAKEOVER_S"])
@pytest.mark.parametrize("bad", ["fast", "", "nan", "0", "-1"])
def test_d2p21_a_bad_interval_is_refused_loudly_and_counted(env, monkeypatch, capsys, name, bad):
    monkeypatch.setenv("LTS_ENGINE_STATE_DIR", os.path.join(env[0], "st"))
    monkeypatch.setenv(name, bad)
    monkeypatch.setattr(D, "keep", lambda *a, **k: 99)     # reaching keep with a bad interval is the bug
    rcs = [D._main(["keep"]) for _ in range(5)]
    assert rcs == [2, 2, 2, 2, 0]                 # never an uncounted traceback / exit 1
    assert name in capsys.readouterr().out


def test_d2p22_status_with_an_unusable_run_dir_says_so_instead_of_a_traceback(la, tmp_path):
    la, _ = la
    _install_ok(la)
    shutil.rmtree(os.path.join(la["state_root"], "run"))
    os.symlink(tmp_path, os.path.join(la["state_root"], "run"))
    out = io.StringIO()
    assert D.launchagent("status", runner=FakeRunner(), out=out, **la) == 0
    assert "run dir unusable:" in out.getvalue()


def test_d2p23_an_uncountable_start_still_tries_to_leave_a_marker_saying_why(env):
    run = os.path.join(env[0], "st", "run")
    os.makedirs(run, mode=0o700, exist_ok=True)
    os.mkdir(os.path.join(run, "embedd-keep-starts.json"))       # the record cannot be written
    log = []
    assert D.keep(_cfg(env), stop=threading.Event(), log=log.append) == 0
    m = json.load(open(_marker(env)))
    assert m["reason"].startswith("uncountable:") and (os.stat(_marker(env)).st_mode & 0o777) == 0o600


def test_d2p23_status_shows_the_uncountable_reason(la):
    la, _ = la
    _install_ok(la)
    run = os.path.join(la["state_root"], "run")
    json.dump({"time": 1000.0, "starts": 0, "window_s": 600, "reason": "uncountable: OSError: x"},
              open(os.path.join(run, "embedd-keep-tripped.json"), "w"))
    out = io.StringIO()
    D.launchagent("status", runner=FakeRunner(), out=out, now=1_000_000.0, **la)
    assert "uncountable: OSError: x" in out.getvalue()


@pytest.mark.parametrize("body", ['{"time": "yesterday"}', '{"time": 1e999999, "starts": "x"}', "{bad", '{"time": 5, "window_s": "w"}'])
def test_d2p32_a_corrupt_marker_does_not_crash_status(la, body):
    la, _ = la
    _install_ok(la)
    open(os.path.join(la["state_root"], "run", "embedd-keep-tripped.json"), "w").write(body)
    out = io.StringIO()
    assert D.launchagent("status", runner=FakeRunner(), out=out, **la) == 0
    assert "tripped (marker unreadable)" in out.getvalue()


def test_d2p33_a_stale_tmp_from_pid_reuse_does_not_make_every_start_uncountable(env):
    run = os.path.join(env[0], "st", "run")
    os.makedirs(run, mode=0o700, exist_ok=True)
    for stale in ("embedd-keep-starts.json", "embedd-keep-tripped.json"):
        open(os.path.join(run, f"{stale}.{os.getpid()}.tmp"), "w").write("stale")
    now = 1_000_000.0
    _seed_starts(env, [now - 4, now - 3, now - 2, now - 1])
    log = []
    assert D.keep(_cfg(env), stop=threading.Event(), log=log.append, clock=lambda: now) == 0
    assert any("crash-loop" in l for l in log) and not any("cannot record" in l for l in log)
    assert json.load(open(_marker(env)))["starts"] == 5


def test_mig11_a_runner_failure_on_the_second_bootout_restores_the_first(la):
    """The guarded region starts at the FIRST bootout, so it covers the rest of the loop."""
    la, exe = la
    old1 = _old_agent(la, exe, label="com.example.lts-embedd")
    old2 = _old_agent(la, exe, label="com.example2.lts-embedd")

    class R(FakeRunner):
        def __call__(self, argv):
            super().__call__(argv)
            if argv[1] == "bootout" and argv[-1].endswith("com.example2.lts-embedd"):
                raise TimeoutError("launchctl hung")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
    r, out = R(), io.StringIO()
    with pytest.raises(TimeoutError):
        D.launchagent("install", runner=r, out=out, **la)
    assert ["launchctl", "bootstrap", "gui/501", old1] in r.calls          # the first agent is back
    assert os.path.exists(old1) and os.path.exists(old2) and not os.path.exists(la["plist_path"])
    assert "restored the earlier agent com.example.lts-embedd" in out.getvalue()


def test_mig12_a_stopped_agent_is_restored_at_most_once(la):
    la, exe = la
    old1 = _old_agent(la, exe, label="com.example.lts-embedd")
    _old_agent(la, exe, label="com.example2.lts-embedd")

    class R(FakeRunner):
        def __call__(self, argv):
            super().__call__(argv)
            if argv[1] == "bootout" and argv[-1].endswith("com.example2.lts-embedd"):
                return SimpleNamespace(returncode=5, stdout="", stderr="busy")      # and print says loaded
            if argv[1] == "bootstrap" and argv[-1] == old1:
                raise TimeoutError("launchctl hung")                                # the restore itself fails
            return SimpleNamespace(returncode=0, stdout="", stderr="")
    r = R()
    with pytest.raises(TimeoutError):
        D.launchagent("install", runner=r, out=io.StringIO(), **la)
    assert r.calls.count(["launchctl", "bootstrap", "gui/501", old1]) == 1
