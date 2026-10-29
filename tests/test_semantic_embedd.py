"""[MECH-SEMANTIC-EMBEDD] the shared embedding service behind LTS_SEMANTIC_EMBEDDER=shared.

Hermetic: the tiny in-test ONNX model, a SHORT state root under /tmp (AF_UNIX paths are
limited to ~104 bytes), a real spawned service process where the property is cross-process,
and an in-process Server where a test must reach inside (peer-credential refusal, priority).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import tempfile
import threading
import time

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")

from factlattice_engine.semantic import embedder as E  # noqa: E402
from factlattice_engine.semantic import embedd as D  # noqa: E402
from factlattice_engine.semantic.suggest import SemanticService  # noqa: E402
from tests.semantic_helpers import _tiny_model  # noqa: E402

TEXTS = ["hello world", "tile map version field", "hello", "tile map", "version field !",
         "world world world hello tile map version", "sw", "field ?"] * 9      # 72 texts, 3 batches


@pytest.fixture()
def env():
    root = tempfile.mkdtemp(prefix="fled-", dir="/tmp")
    models = os.path.join(root, "m")
    pin = _tiny_model(models)
    yield root, models, pin
    for f in D.pidfiles(os.path.join(root, "st")):                 # never leave a service behind
        try:
            pid = int(open(f).read().strip() or 0)
            if pid and pid != os.getpid() and _is_embedd_service(pid):   # never trust a pidfile blindly
                os.kill(pid, signal.SIGKILL)
        except (OSError, ValueError):
            pass
    shutil.rmtree(root, ignore_errors=True)


def _is_embedd_service(pid):
    import subprocess
    cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    return "factlattice_engine.semantic.embedd" in cmd and " serve " in cmd + " "


def _client(env, **kw):
    root, models, pin = env
    return D.SharedEmbedder(pin, models, state_root=os.path.join(root, "st"), **kw)


def test_default_is_local_and_shared_is_opt_in(env, monkeypatch):
    root, models, pin = env
    monkeypatch.delenv("LTS_SEMANTIC_EMBEDDER", raising=False)
    svc = SemanticService(state_root=os.path.join(root, "st"), model_root=models, pin=pin)
    assert isinstance(svc._get_embedder(), E.Embedder)
    monkeypatch.setenv("LTS_SEMANTIC_EMBEDDER", "shared")
    svc = SemanticService(state_root=os.path.join(root, "st"), model_root=models, pin=pin)
    assert isinstance(svc._get_embedder(), D.SharedEmbedder)


def test_vectors_are_byte_identical_to_in_process(env):
    root, models, pin = env
    local = E.load_embedder(pin, models)
    shared = _client(env)
    for q in (True, False):
        assert shared.embed(TEXTS, query=q).tobytes() == local.embed(TEXTS, query=q).tobytes()
    assert shared.embed([], query=True).shape == (0, pin.dim)


def test_concurrent_first_use_starts_exactly_one_service(env):
    clients = [_client(env) for _ in range(4)]
    out, errs = [], []

    def go(c):
        try:
            out.append(c.embed(["hello"], query=True).tobytes())
        except Exception as ex:   # noqa: BLE001
            errs.append(repr(ex))
    ts = [threading.Thread(target=go, args=(c,)) for c in clients]
    [t.start() for t in ts]; [t.join(60) for t in ts]
    assert not errs and len(set(out)) == 1
    assert len({c.server_pid for c in clients}) == 1
    assert len(D.pidfiles(os.path.join(env[0], "st"))) == 1


def test_a_killed_service_is_restarted_by_the_next_call(env):
    c = _client(env)
    c.embed(["hello"], query=True)
    first = c.server_pid
    os.kill(first, signal.SIGKILL)
    time.sleep(0.2)
    assert c.embed(["tile map"], query=True).shape == (1, env[2].dim)
    assert c.server_pid != first


def test_a_stalled_service_is_unavailable_within_the_deadline_never_a_hang(env):
    root, models, pin = env
    c = _client(env, frame_deadline_s=1.0)
    run = D.run_dir(os.path.join(root, "st"))
    path = os.path.join(run, f"embedd-{c.material_hash}.sock")
    srv = socket.socket(socket.AF_UNIX); srv.bind(path); srv.listen(1)     # accepts, never answers
    t0 = time.monotonic()
    with pytest.raises(E.SemanticUnavailable):
        c.embed(["hello"], query=True)
    assert time.monotonic() - t0 < 5
    srv.close()


def test_a_version_mismatch_is_refused_never_mixed(env, monkeypatch):
    root, models, pin = env
    c = _client(env)
    real = D.material

    def other(pin_, max_len):
        return real(pin_, max_len) + "|tampered"
    srv = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=30)
    monkeypatch.setattr(srv, "material", other(pin, 512))        # same socket name, other material
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    D.wait_for_socket(srv.sock_path, 10)
    with pytest.raises(E.SemanticUnavailable) as ei:
        c.embed(["hello"], query=True)
    assert ei.value.status == "embedder_version_mismatch"
    srv.shutdown()


def test_two_versions_use_two_services(env):
    a, b = _client(env), _client(env, max_len=256)
    assert a.material_hash != b.material_hash
    a.embed(["hello"], query=True); b.embed(["hello"], query=True)
    assert a.server_pid != b.server_pid


def test_a_peer_with_another_euid_is_refused(env, monkeypatch):
    root, models, pin = env
    srv = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=30)
    monkeypatch.setattr(D, "_peer_uid", lambda conn: os.geteuid() + 1)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    D.wait_for_socket(srv.sock_path, 10)
    with pytest.raises(E.SemanticUnavailable):
        _client(env).embed(["hello"], query=True)
    srv.shutdown()


def test_a_symlinked_run_dir_or_socket_is_refused(env, tmp_path):
    root, models, pin = env
    st = os.path.join(root, "st"); os.makedirs(st)
    os.symlink(str(tmp_path), os.path.join(st, "run"))                # pre-planted run dir
    with pytest.raises(E.SemanticUnavailable):
        _client(env).embed(["hello"], query=True)
    os.remove(os.path.join(st, "run"))
    c = _client(env)
    run = D.run_dir(st)
    os.symlink(str(tmp_path / "evil.sock"), os.path.join(run, f"embedd-{c.material_hash}.sock"))
    with pytest.raises(E.SemanticUnavailable):
        c.embed(["hello"], query=True)


def test_the_run_dir_is_0700_whatever_the_umask(env):
    old = os.umask(0)
    try:
        run = D.run_dir(os.path.join(env[0], "st"))
    finally:
        os.umask(old)
    assert os.stat(run).st_mode & 0o777 == 0o700


def test_a_preplanted_pidfile_symlink_cannot_hijack(env, tmp_path):
    root, models, pin = env
    c = _client(env)
    run = D.run_dir(os.path.join(root, "st"))
    target = tmp_path / "victim"; target.write_text("keep")
    os.symlink(str(target), os.path.join(run, f"embedd-{c.material_hash}.pid"))
    srv = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=30)
    with pytest.raises(D.Unsafe):
        srv.acquire_pidfile()
    assert target.read_text() == "keep"


def test_queries_jump_ahead_of_a_build(env, monkeypatch):
    root, models, pin = env
    srv = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=30)
    srv.test_batch_delay_s = 0.2
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    D.wait_for_socket(srv.sock_path, 10)
    build, query = _client(env), _client(env)
    done = {}

    def run_build():
        build.embed(TEXTS * 4, query=False)           # 9 batches x 0.2 s
        done["build"] = time.monotonic()
    tb = threading.Thread(target=run_build); tb.start()
    time.sleep(0.3)
    t0 = time.monotonic(); query.embed(["hello"], query=True); done["query"] = time.monotonic()
    tb.join(30)
    assert done["query"] - t0 < 1.0 and done["query"] < done["build"]
    srv.shutdown()


def test_an_idle_service_exits_and_removes_its_socket(env):
    root, models, pin = env
    srv = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=0.5)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    D.wait_for_socket(srv.sock_path, 10)
    t.join(10)
    assert not t.is_alive() and not os.path.exists(srv.sock_path)


def test_suggest_reports_unavailable_never_raises_when_the_service_is_unsafe(env, monkeypatch, tmp_path):
    from tests.test_semantic_index import _part, _write_corpus
    root, models, pin = env
    st = os.path.join(root, "st"); os.makedirs(st)
    _write_corpus(tmp_path / "corpus")
    monkeypatch.setenv("LTS_SEMANTIC_EMBEDDER", "shared")
    svc = SemanticService(state_root=st, model_root=models, pin=pin, build_mode="sync")
    os.symlink(str(tmp_path), os.path.join(st, "run"))
    r = svc.suggest(_part(tmp_path), "hello world")
    assert r["status"] in ("unavailable", "index_failed") and r["candidates"] == []
    assert json.dumps(r)


def _reference_embed(emb, texts, query, batch=32):
    """Embedder.embed as it was at 2bc3ef6, frozen: the refactor into plan()/run_batch() must not
    move a single bit, or every live sidecar would silently mismatch new query vectors."""
    prefix = emb.pin.query_prefix if query else emb.pin.doc_prefix
    enc = [emb.tokenizer.encode(prefix + t, emb.max_len)[0] for t in texts]
    out = np.zeros((len(texts), emb.pin.dim), dtype=np.float32)
    order = sorted(range(len(enc)), key=lambda i: (len(enc[i]), i))
    for b in range(0, len(order), batch):
        idx = order[b:b + batch]
        width = max(len(enc[i]) for i in idx)
        ids = np.zeros((len(idx), width), dtype=np.int64)
        mask = np.zeros((len(idx), width), dtype=np.int64)
        for r, i in enumerate(idx):
            ids[r, :len(enc[i])] = enc[i]
            mask[r, :len(enc[i])] = 1
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in emb._inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = emb.session.run([emb._out], feed)[0]
        if emb.pin.pooling == "cls":
            vec = hidden[:, 0, :]
        else:
            m = mask[..., None].astype(np.float32)
            vec = (hidden * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1e-9)
        vec = vec / np.maximum(np.linalg.norm(vec, axis=1, keepdims=True), 1e-12)
        out[idx] = vec.astype(np.float32)
    return out


def test_the_plan_refactor_moves_no_bit(env):
    root, models, pin = env
    local = E.load_embedder(pin, models)
    for q in (True, False):
        assert local.embed(TEXTS, query=q).tobytes() == _reference_embed(local, TEXTS, q).tobytes()
    assert hasattr(local, "plan") and hasattr(local, "run_batch")


def test_a_query_is_not_blocked_by_a_build_on_the_same_engine(env, monkeypatch):
    """Acceptance-3 finding (2026-10-02): ONE engine = ONE SharedEmbedder; its background index
    worker's build and a query from another thread must not serialise on the client. Measured
    before the fix: a query waited 268.9 s behind a acme-docs rebuild in the same engine."""
    root, models, pin = env
    srv = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=30)
    srv.test_batch_delay_s = 0.2
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    D.wait_for_socket(srv.sock_path, 10)
    engine = _client(env)                                  # the one embedder an engine holds
    done = {}

    def run_build():
        engine.embed(TEXTS * 4, query=False)               # 9 batches x 0.2 s
        done["build"] = time.monotonic()
    tb = threading.Thread(target=run_build); tb.start()
    time.sleep(0.3)
    t0 = time.monotonic(); engine.embed(["hello"], query=True); done["query"] = time.monotonic()
    tb.join(30)
    assert done["query"] - t0 < 1.0 and done["query"] < done["build"]
    srv.shutdown()


def test_a_query_does_not_wait_for_the_running_build_batch(env):
    """Acceptance-3 finding #2 (2026-10-02): with ONE inference thread a query still waits for
    the build batch already running (p95 2.6 s, max 11 s during a acme-docs rebuild). Queries
    need their own lane, concurrent with the build, as in-process inference is."""
    root, models, pin = env
    srv = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=30)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    D.wait_for_socket(srv.sock_path, 10)
    while srv._embedder is None:
        time.sleep(0.01)
    real = srv._embedder.run_batch

    def slow_builds(ids, mask):
        if ids.shape[0] > 1:                     # a build batch: 1.5 s each
            time.sleep(1.5)
        return real(ids, mask)
    srv._embedder.run_batch = slow_builds
    engine = _client(env)
    tb = threading.Thread(target=lambda: engine.embed(TEXTS, query=False)); tb.start()
    time.sleep(0.5)                              # a build batch is now running
    t0 = time.monotonic(); engine.embed(["hello"], query=True)
    assert time.monotonic() - t0 < 0.5
    tb.join(30); srv.shutdown()


_ENGINE_A = """
import sys, time, json, os
sys.path.insert(0, {tree!r})
from factlattice_engine.semantic import embedd as D, embedder as E
pin = E.ModelPin(**json.loads({pin!r}))
c = D.SharedEmbedder(pin, {models!r}, state_root={st!r})
c.embed(["hello"], query=True)
print(c.server_pid, flush=True)
time.sleep(60)                                    # stays alive, never reaps its children
"""


def test_a_service_killed_under_another_engine_comes_back_within_the_deadline(env):
    """Acceptance-4 finding (2026-10-02): engine A spawned the service as ITS child; after a
    SIGKILL it was A's zombie, os.kill(pid, 0) said 'alive', the respawn engine B triggered
    exited at once, and every call waited the 60 s startup deadline."""
    import dataclasses
    import subprocess
    import sys
    root, models, pin = env
    st = os.path.join(root, "st")
    tree = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = _ENGINE_A.format(tree=tree, pin=json.dumps(dataclasses.asdict(pin)), models=models, st=st)
    a = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        victim = int(a.stdout.readline())
        os.kill(victim, signal.SIGKILL)
        time.sleep(0.2)
        b = _client(env, frame_deadline_s=5.0)
        t0 = time.monotonic()
        assert b.embed(["tile map"], query=True).shape == (1, pin.dim)
        assert time.monotonic() - t0 < 8 and b.server_pid != victim
    finally:
        a.kill(); a.wait()


def test_a_query_never_waits_past_its_deadline_for_a_spawn(env):
    import fcntl
    root, models, pin = env
    c = _client(env, frame_deadline_s=1.0)
    run = D.run_dir(os.path.join(root, "st"))
    fd = os.open(os.path.join(run, f"embedd-{c.material_hash}.spawn"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)                      # another engine is (slowly) spawning
    try:
        t0 = time.monotonic()
        with pytest.raises(E.SemanticUnavailable):
            c.embed(["hello"], query=True)
        assert time.monotonic() - t0 < 3
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


def test_an_interactive_doc_embed_rides_the_query_lane(env):
    """Acceptance-3 final run (2026-10-02): one query took 10.06 s. A query that finds a few stale
    nodes tops them up SYNCHRONOUSLY; those are document embeds (query=False), so they queued in
    the build lane behind a running rebuild. Interactive doc embeds must use the query lane."""
    root, models, pin = env
    srv = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=30)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    D.wait_for_socket(srv.sock_path, 10)
    while srv._embedder is None:
        time.sleep(0.01)
    real = srv._embedder.run_batch

    def slow_builds(ids, mask):
        if ids.shape[0] > 1:
            time.sleep(1.5)
        return real(ids, mask)
    srv._embedder.run_batch = slow_builds
    engine = _client(env)
    tb = threading.Thread(target=lambda: engine.embed(TEXTS, query=False)); tb.start()
    time.sleep(0.5)
    t0 = time.monotonic(); engine.embed(["tile map"], query=False, interactive=True)
    assert time.monotonic() - t0 < 0.5
    tb.join(30); srv.shutdown()


def test_suggest_tops_up_interactively(env, monkeypatch, tmp_path):
    from tests.test_semantic_index import _part, _write_corpus
    from factlattice_engine.semantic import index as I
    root, models, pin = env
    _write_corpus(tmp_path / "corpus")
    seen = []
    real = I.SemanticIndex.refresh

    def spy(self, part, interactive=False):
        seen.append(interactive)
        return real(self, part, interactive=interactive)
    monkeypatch.setattr(I.SemanticIndex, "refresh", spy)
    svc = SemanticService(state_root=os.path.join(root, "st"), model_root=models, pin=pin, build_mode="sync")
    svc.suggest(_part(tmp_path), "hello world")
    assert seen and all(seen)                         # every refresh from inside a query is interactive


# --- review audit-SEMANTIC-EMBEDD-2026-10-02: P1-1..P1-4 (negative claims N1-N3) + P2s --------
def _fake_service(env, script):
    """A hostile/buggy service on the real socket path: answers hello correctly, then runs
    `script(conn, request_header)` for every embed request. Returns (client, stop)."""
    import struct as _st
    c = _client(env, frame_deadline_s=1.0)
    path = os.path.join(D.run_dir(os.path.join(env[0], "st")), f"embedd-{c.material_hash}.sock")
    srv = socket.socket(socket.AF_UNIX); srv.bind(path); srv.listen(8); srv.settimeout(0.2)
    stop = threading.Event()

    def handle(conn):
        try:
            D._recv(conn)
            D._send(conn, {"ok": True, "material": c.material, "pid": 4242})
            while not stop.is_set():
                h, _ = D._recv(conn)
                script(conn, h)
        except Exception:   # noqa: BLE001 - a fake service just drops the connection
            pass
        finally:
            conn.close()

    def loop():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
        srv.close()
    threading.Thread(target=loop, daemon=True).start()
    return c, stop, _st


def _vec(dim, val, rows=1):
    return np.full((rows, dim), val, dtype=np.float32).tobytes()


@pytest.mark.parametrize("bad", ["notjson", "idx_str", "idx_oob", "idx_neg", "short_payload"])
def test_P1_1_a_malformed_frame_is_unavailable_never_a_raw_exception(env, bad):
    dim = env[2].dim

    def script(conn, h):
        if bad == "notjson":
            conn.sendall(_st.pack(">II", 5, 0) + b"{nope")
        elif bad == "idx_str":
            D._send(conn, {"rid": h.get("rid"), "idx": "abc"}, _vec(dim, 1.0))
        elif bad == "idx_oob":
            D._send(conn, {"rid": h.get("rid"), "idx": [5]}, _vec(dim, 1.0))
        elif bad == "idx_neg":
            D._send(conn, {"rid": h.get("rid"), "idx": [-1]}, _vec(dim, 1.0))
        else:
            D._send(conn, {"rid": h.get("rid"), "idx": [0]}, b"\x00" * 3)
        D._send(conn, {"rid": h.get("rid"), "done": True, "n": 1})
    c, stop, _st = _fake_service(env, script)
    globals()["_st"] = _st
    with pytest.raises(E.SemanticUnavailable):
        c.embed(["hello"], query=True)
    stop.set()


def test_P1_2_a_failed_call_never_leaks_frames_into_the_next(env):
    dim = env[2].dim
    calls = {"n": 0}

    def script(conn, h):
        calls["n"] += 1
        if calls["n"] == 1:          # garbage first, then a COMPLETE valid answer of 111s queued behind it
            conn.sendall(_st.pack(">II", 5, 0) + b"{nope")
            D._send(conn, {"rid": h.get("rid"), "idx": [0]}, _vec(dim, 111.0))
            D._send(conn, {"rid": h.get("rid"), "done": True, "n": 1})
        else:
            D._send(conn, {"rid": h.get("rid"), "idx": [0]}, _vec(dim, 222.0))
            D._send(conn, {"rid": h.get("rid"), "done": True, "n": 1})
    c, stop, _st = _fake_service(env, script)
    globals()["_st"] = _st
    with pytest.raises(E.SemanticUnavailable):
        c.embed(["hello"], query=True)
    out = c.embed(["hello"], query=True)
    assert float(out[0, 0]) == 222.0
    stop.set()


def test_P1_2b_a_frame_from_another_request_is_refused(env):
    dim = env[2].dim

    def script(conn, h):
        D._send(conn, {"rid": 12345, "idx": [0]}, _vec(dim, 111.0))       # not this request's id
        D._send(conn, {"rid": 12345, "done": True, "n": 1})
    c, stop, _st = _fake_service(env, script)
    with pytest.raises(E.SemanticUnavailable):
        c.embed(["hello"], query=True)
    stop.set()


def test_P1_3_duplicate_rows_never_pass_as_complete(env):
    dim = env[2].dim

    def script(conn, h):
        D._send(conn, {"rid": h.get("rid"), "idx": [0]}, _vec(dim, 1.0))
        D._send(conn, {"rid": h.get("rid"), "idx": [0]}, _vec(dim, 1.0))   # row 1 never sent
        D._send(conn, {"rid": h.get("rid"), "done": True, "n": 2})
    c, stop, _st = _fake_service(env, script)
    with pytest.raises(E.SemanticUnavailable):
        c.embed(["hello", "world"], query=True)
    stop.set()


def test_P1_4_a_byte_trickle_cannot_stretch_a_query_past_its_deadline(env):
    def script(conn, h):
        for _ in range(100):                  # one byte every 0.3 s, forever-ish
            conn.sendall(b"\x00")
            time.sleep(0.3)
    c, stop, _st = _fake_service(env, script)
    t0 = time.monotonic()
    with pytest.raises(E.SemanticUnavailable):
        c.embed(["hello"], query=True)        # frame_deadline_s = 1.0
    assert time.monotonic() - t0 < 2.0
    stop.set()


def test_P1_4b_a_stuck_launcher_cannot_stretch_a_query_past_its_deadline(env, monkeypatch):
    monkeypatch.setattr(D, "_LAUNCHER", "import time\ntime.sleep(20)\n")
    c = _client(env, frame_deadline_s=1.0)
    t0 = time.monotonic()
    with pytest.raises(E.SemanticUnavailable):
        c.embed(["hello"], query=True)
    assert time.monotonic() - t0 < 2.5


def test_P2_1_idle_exit_fires_with_a_connected_idle_client(env):
    root, models, pin = env
    srv = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=0.6)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    D.wait_for_socket(srv.sock_path, 10)
    c = _client(env)
    c.embed(["hello"], query=True)            # this thread now holds a connection
    t.join(10)
    assert not t.is_alive()


def test_P2_2_a_recycled_pid_in_a_stale_pidfile_does_not_block_start(env):
    root, models, pin = env
    srv = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=30)
    with open(srv.pid_path, "w") as fh:
        fh.write(str(os.getpid()))            # a LIVE, unrelated process (pytest itself): kill -0 succeeds
    assert srv.acquire_pidfile() is True


def test_P2_2_a_refused_start_never_deletes_the_live_service_files(env):
    root, models, pin = env
    live = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=30)
    t = threading.Thread(target=live.serve_forever, daemon=True); t.start()
    D.wait_for_socket(live.sock_path, 10)
    second = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=30)
    second.acquire_pidfile = lambda: True     # e.g. it misjudged the pidfile stale (P2-2a/b)
    try:
        second.serve_forever()                # must notice the live socket at bind and leave
    except D.Unsafe:
        pass
    assert os.path.exists(live.sock_path) and os.path.exists(live.pid_path)
    live.shutdown()


def test_P2_4_jobs_of_a_vanished_client_are_cancelled(env, monkeypatch):
    root, models, pin = env
    srv = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=30)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    D.wait_for_socket(srv.sock_path, 10)
    while srv._embedder is None:
        time.sleep(0.01)
    real, ran = srv._embedder.run_batch, {"n": 0}

    def slow(ids, mask):
        ran["n"] += 1
        time.sleep(0.8)                                        # each batch outlasts the frame deadline
        return real(ids, mask)
    srv._embedder.run_batch = slow
    c = _client(env, frame_deadline_s=0.5, startup_s=5)        # gives up waiting for the first frame
    with pytest.raises(E.SemanticUnavailable):
        c.embed(TEXTS * 6, query=False)                        # 14 batches if all ran (~11 s)
    time.sleep(6.0)                                            # uncancelled, ~8 would have run by now
    assert ran["n"] <= 3, ran
    srv.shutdown()


def test_P2_R1_a_queued_build_is_kept_alive_not_timed_out(env, monkeypatch):
    """Re-audit P2-R1: build requests share one build lane FIFO; engine B's first frame can wait
    behind engine A's whole build longer than the per-frame deadline although the service is
    making progress (the go-live re-prewarm shape). Heartbeats keep a queued build alive."""
    root, models, pin = env
    monkeypatch.setenv("LTS_EMBEDD_HEARTBEAT_S", "0.3")
    srv = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=30)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    D.wait_for_socket(srv.sock_path, 10)
    while srv._embedder is None:
        time.sleep(0.01)
    real = srv._embedder.run_batch

    def slow(ids, mask):
        time.sleep(0.6)
        return real(ids, mask)
    srv._embedder.run_batch = slow
    a, b = _client(env, frame_deadline_s=1.0), _client(env, frame_deadline_s=1.0)
    errs = []
    ta = threading.Thread(target=lambda: a.embed(TEXTS * 4, query=False)); ta.start()   # ~5.4 s of batches
    time.sleep(0.2)
    try:
        b.embed(TEXTS, query=False)                  # queued behind all of A's batches
    except E.SemanticUnavailable as ex:
        errs.append(repr(ex))
    ta.join(30); srv.shutdown()
    assert not errs, errs


def test_P2_R1b_heartbeats_alone_cannot_hold_a_build_forever(env):
    def script(conn, h):
        for _ in range(200):
            D._send(conn, {"rid": h.get("rid"), "wait": True})
            time.sleep(0.2)
    c, stop, _st = _fake_service(env, script)
    c.startup_s = 1.0                                 # build total cap = max(startup_s, per-text budget)
    t0 = time.monotonic()
    with pytest.raises(E.SemanticUnavailable):
        c.embed(["hello"], query=False)
    assert time.monotonic() - t0 < 3.0
    stop.set()


@pytest.mark.parametrize("n,lane", [(256, 0), (257, 1)])
def test_P2_R2_interactive_max_routes_the_lane(env, n, lane):
    seen = []

    def script(conn, h):
        seen.append(h.get("lane"))
        D._send(conn, {"rid": h.get("rid"), "error": "probe only"})
    c, stop, _st = _fake_service(env, script)
    with pytest.raises(E.SemanticUnavailable):
        c.embed(["hello"] * n, query=False, interactive=True)
    assert seen == [lane]
    stop.set()


# --- the architect P3 rulings (preconditions for enabling the flag) ---------------------------
def test_P3_2_an_unknown_flag_value_warns_loudly_and_uses_local(env, monkeypatch, capsys):
    root, models, pin = env
    monkeypatch.setenv("LTS_SEMANTIC_EMBEDDER", "Shared")         # a typo must not pass silently
    svc = SemanticService(state_root=os.path.join(root, "st"), model_root=models, pin=pin)
    assert isinstance(svc._get_embedder(), E.Embedder)
    err = capsys.readouterr().err
    assert "LTS_SEMANTIC_EMBEDDER" in err and "'Shared'" in err and "local" in err


def test_P3_3_the_batch_delay_test_hook_is_not_read_from_the_environment(env, monkeypatch):
    root, models, pin = env
    monkeypatch.setenv("LTS_EMBEDD_TEST_BATCH_DELAY_S", "5")     # production must ignore this
    srv = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=30)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    D.wait_for_socket(srv.sock_path, 10)
    t0 = time.monotonic(); _client(env).embed(["hello"], query=True)
    assert time.monotonic() - t0 < 2.0
    srv.shutdown()


def test_P3_4_a_symlinked_or_foreign_state_root_is_refused(env, tmp_path):
    root, models, pin = env
    real = tmp_path / "real-state"; real.mkdir()
    link = os.path.join(root, "st-link"); os.symlink(str(real), link)
    with pytest.raises(D.Unsafe):
        D.run_dir(link)
    with pytest.raises(E.SemanticUnavailable):
        D.SharedEmbedder(pin, models, state_root=link).embed(["hello"], query=True)


def test_R6_an_internal_error_in_a_connection_is_logged_with_a_traceback(env, capsys):
    root, models, pin = env
    srv = D.Server(os.path.join(root, "st"), pin, models, 512, idle_s=30)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    D.wait_for_socket(srv.sock_path, 10)
    while srv._embedder is None:
        time.sleep(0.01)

    def broken_plan(*a, **k):
        raise RuntimeError("plan exploded")
    srv._embedder.plan = broken_plan
    with pytest.raises(E.SemanticUnavailable):
        _client(env).embed(["hello"], query=True)
    srv.shutdown(); t.join(5)
    err = capsys.readouterr().err
    assert "Traceback" in err and "plan exploded" in err
