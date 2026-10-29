"""[REG-RUNTIME-CORPUS] — runtime `register_corpus` / `unregister_corpus` + the
`notifications/tools/list_changed` push.

Spec: (internal design note)

THE FOOT-GUN THIS CLOSES. The corpus set was built ONCE at `server.build_tools`
(`config.build_store()`); there was no runtime path to add or drop a corpus, so a new
corpus / a config-file edit / a temp corpus needing cleanup all required a full process
RESTART — and a client `/mcp reconnect` re-attaches transport ONLY; it does not reload
config or code. `lts_register_corpus` builds ONE corpus through the SAME builder
functions the cold boot path uses (`config.build_partition` / `build_merged_partition`)
and seats it in the SAME freshness kernel, so a runtime-registered corpus is
byte-for-byte what a restart would have produced. `lts_unregister_corpus` is the exact
inverse. Both are declared a SURFACE CHANGE: a successful call pushes
`notifications/tools/list_changed`, and `initialize` advertises `listChanged: true` so a
connected client picks up the push at all (the one-line trap: `listChanged: false`/
omitted makes a compliant client ignore it — measured as an MCP relay's bug).

Every test builds REAL `.lts` files on disk and the REAL server wiring (`FreshnessKernel`
+ `PollVerifiedFreshness` + `SelfHealer`, exactly as `server.build_tools` composes them),
mirroring `tests/test_list_corpora_selfheal.py`'s fixture shape. No mocks except where a
test's whole point is a fail-soft path.

The acceptance bar:
  (a) REGISTER MAKES IT QUERYABLE, NO RESTART -> test_register_corpus_explicit_spec_is_immediately_queryable
                                                  test_register_corpus_multi_root_spec
                                                  test_register_corpus_reads_config_from_disk_when_spec_omitted
  (b) SELF-HEAL PARTICIPATION               -> test_registered_corpus_is_kernel_seated
                                                test_registered_corpus_self_heals_like_a_boot_time_corpus
  (c) REFUSALS CHANGE NOTHING                -> test_register_refuses_if_already_loaded
                                                test_register_refuses_on_empty_root
                                                test_unregister_refuses_on_unknown_id
  (d) UNREGISTER IS THE CLEAN INVERSE        -> test_unregister_corpus_makes_it_unfindable
                                                test_unregister_does_not_disturb_other_corpora
                                                test_unregister_unseats_kernel_and_forgets_healer
                                                test_reregister_same_id_after_unregister_is_clean
  (e) MCP surface + list_changed PUSH        -> test_schema_lists_registry_tools
                                                test_init_advertises_list_changed
                                                test_successful_register_queues_list_changed
                                                test_successful_unregister_queues_list_changed
                                                test_failed_register_queues_nothing
                                                test_read_only_tools_queue_nothing
                                                test_real_stdio_server_register_no_restart_and_pushes_notification
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from factlattice_engine import (
    FreshnessKernel, LtsEngine, LtsStore, PollVerifiedFreshness, SelfHealer,
)
from factlattice_engine.config import build_merged_partition, build_partition
from factlattice_engine.tools import LTS_ALL_TOOL_NAMES, LTS_REGISTRY_TOOL_NAMES, dispatch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ===========================================================================
# fixtures — real files on disk, server-shaped wiring (mirrors
# test_list_corpora_selfheal.py's `two_corpora` / `_server_shaped`)
# ===========================================================================
def _lts(node_id: str, definition: str) -> str:
    return ("---\ndomain: core.example\n---\n"
            f"# Test [SPEC:{node_id}-DOC]\n\n"
            f"### [{node_id}] Node {node_id}\n- definition: {definition}\n")


@pytest.fixture
def boot_corpus(tmp_path):
    """One corpus loaded at 'boot' — what `register_corpus` must NOT disturb."""
    root = tmp_path / "boot"
    root.mkdir()
    (root / "b0.lts").write_text(_lts("B0", "boot zero"), encoding="utf-8")
    return build_partition("boot", str(root), "proj.boot")


def _server_shaped(*parts, healer=True):
    """Exactly `server.build_tools`'s wiring: seated kernel + verify-on-query + healer."""
    store = LtsStore()
    for part in parts:
        store.add_partition(part)
    kernel = FreshnessKernel()
    kernel.seat_store(store)
    store.kernel = kernel
    provider = PollVerifiedFreshness()
    h = SelfHealer(store, kernel=kernel, freshness_provider=provider) if healer else None
    return LtsEngine(store, healer=h, freshness_provider=provider), kernel, h


# ===========================================================================
# (a) register makes a corpus queryable, with no restart
# ===========================================================================
def test_register_corpus_explicit_spec_is_immediately_queryable(tmp_path, boot_corpus):
    eng, kernel, healer = _server_shaped(boot_corpus)
    new_root = tmp_path / "fresh"
    new_root.mkdir()
    (new_root / "n0.lts").write_text(_lts("N0", "fresh zero"), encoding="utf-8")

    # Pre-condition: the pre-change engine has no such method at all — this call would
    # raise AttributeError against the code as it stood before this branch.
    res = eng.register_corpus("fresh", spec={"root": str(new_root),
                                              "domain_suffix": "proj.fresh"})
    assert res["success"] is True, res
    assert res["corpus_id"] == "fresh"
    assert res["total_nodes"] == 2   # the `_lts` helper mints an -DOC node + the content node
    assert res["kernel_seated"] is True
    assert res["self_heal_ready"] is True
    assert "fresh" in eng.store.corpus_ids()

    # Queryable with NO restart, THROUGH THE SAME ENGINE OBJECT that was already
    # serving `boot` — no re-instantiation, no re-import.
    found = eng.find(corpus="fresh", prefix="N0")
    assert found["success"] is True
    assert {n["id"] for n in found["nodes"]} == {"N0", "N0-DOC"}
    # And `boot` is untouched.
    assert eng.find(corpus="boot", prefix="B0")["nodes"][0]["id"] in ("B0", "B0-DOC")


def test_register_corpus_multi_root_spec(tmp_path, boot_corpus):
    eng, kernel, healer = _server_shaped(boot_corpus)
    dec = tmp_path / "dec"
    specs = tmp_path / "specs"
    dec.mkdir()
    specs.mkdir()
    (dec / "decision-graph.lts").write_text(
        "---\ndomain: core.p\n---\n# D [SYSTEM:D-DOC]\n\n"
        "### [DEC-1] One\n- definition: one\n", encoding="utf-8")
    (specs / "s0.lts").write_text(_lts("SPC0", "spec zero"), encoding="utf-8")

    res = eng.register_corpus("p", spec={
        "roots": [{"path": str(dec), "area": "decisions"},
                  {"path": str(specs), "area": "specs"}],
        "domain_suffix": "proj.p"})
    assert res["success"] is True, res
    assert res["total_nodes"] == 4   # dec: D-DOC + DEC-1; specs: SPC0-DOC + SPC0
    found = eng.find(corpus="p", area="decisions", prefix="DEC")
    assert [n["id"] for n in found["nodes"]] == ["DEC-1"]


def test_register_corpus_reads_config_from_disk_when_spec_omitted(tmp_path, boot_corpus,
                                                                   monkeypatch):
    """Omitting `spec` re-reads `FACTLATTICE_CORPUS_CONFIG` FRESH from disk — a
    config-file entry added AFTER the engine/process started is picked up."""
    eng, kernel, healer = _server_shaped(boot_corpus)
    root = tmp_path / "cfgproj"
    root.mkdir()
    (root / "c0.lts").write_text(_lts("C0", "cfg zero"), encoding="utf-8")
    cfg_path = tmp_path / "corpora.json"
    cfg_path.write_text(json.dumps({
        "cfgproj": {"root": str(root), "domain_suffix": "proj.cfgproj"}}),
        encoding="utf-8")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(cfg_path))

    res = eng.register_corpus("cfgproj")
    assert res["success"] is True, res
    assert res["total_nodes"] == 2
    assert {n["id"] for n in eng.find(corpus="cfgproj", prefix="C0")["nodes"]} \
        == {"C0", "C0-DOC"}


# ===========================================================================
# (b) self-heal participation — a runtime-registered corpus is NOT a second,
#     divergent mechanism from a boot-time one
# ===========================================================================
def test_registered_corpus_is_kernel_seated(tmp_path, boot_corpus):
    eng, kernel, healer = _server_shaped(boot_corpus)
    root = tmp_path / "seeded"
    root.mkdir()
    (root / "s0.lts").write_text(_lts("S0", "seeded zero"), encoding="utf-8")
    eng.register_corpus("seeded", spec={"root": str(root), "domain_suffix": "proj.seeded"})

    part = eng.store.get_partition("seeded")
    assert kernel.current_root(part) is not None, \
        "the kernel must track a runtime-registered corpus exactly like a boot-time one"
    assert kernel.tracked_roots(part) == [str(root)]


def test_registered_corpus_self_heals_like_a_boot_time_corpus(tmp_path, boot_corpus):
    eng, kernel, healer = _server_shaped(boot_corpus)
    root = tmp_path / "healable"
    root.mkdir()
    (root / "h0.lts").write_text(_lts("H0", "healable zero"), encoding="utf-8")
    reg = eng.register_corpus("healable", spec={"root": str(root),
                                                 "domain_suffix": "proj.healable"})
    assert reg["success"] is True

    part = eng.store.get_partition("healable")
    (root / "h1.lts").write_text(_lts("H1", "healable one"), encoding="utf-8")
    assert eng.freshness_provider.evaluate(part).fresh is False, \
        "precondition: the edit must make the corpus stale"

    # The ordinary query path heals it — no special-cased "runtime corpus" branch.
    found = eng.find(corpus="healable", prefix="H")
    assert found["success"] is True
    assert {n["id"] for n in found["nodes"]} == {"H0", "H0-DOC", "H1", "H1-DOC"}
    assert healer.rebuilds == 1
    assert eng.freshness_provider.evaluate(part).fresh is True


# ===========================================================================
# (c) refusals change nothing
# ===========================================================================
def test_register_refuses_if_already_loaded(tmp_path, boot_corpus):
    eng, kernel, healer = _server_shaped(boot_corpus)
    before_ids = set(eng.store.corpus_ids())
    res = eng.register_corpus("boot", spec={"root": str(tmp_path), "domain_suffix": "x"})
    assert res["success"] is False
    assert "already loaded" in res["error"]
    assert set(eng.store.corpus_ids()) == before_ids


def test_register_refuses_on_empty_root(tmp_path, boot_corpus):
    eng, kernel, healer = _server_shaped(boot_corpus)
    empty = tmp_path / "empty"
    empty.mkdir()
    res = eng.register_corpus("empty", spec={"root": str(empty), "domain_suffix": "proj.e"})
    assert res["success"] is False
    assert "zero nodes" in res["error"]
    assert "empty" not in eng.store.corpus_ids()


def test_register_refuses_when_no_config_entry_and_no_spec(boot_corpus, monkeypatch):
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG", raising=False)
    eng, kernel, healer = _server_shaped(boot_corpus)
    res = eng.register_corpus("nonexistent-id")
    assert res["success"] is False
    # SPEC-U1a: with NO registry configured the answer is not "no config entry in the example registry" but
    # `registry-not-found`; an explicitly configured registry that lacks the id keeps the "no config entry" text
    assert res["reason"] == "registry-not-found" and "has no corpus 'nonexistent-id'" in res["error"]


def test_unregister_refuses_on_unknown_id(boot_corpus):
    eng, kernel, healer = _server_shaped(boot_corpus)
    res = eng.unregister_corpus("never-loaded")
    assert res["success"] is False
    assert "boot" in res["available"]
    assert "boot" in eng.store.corpus_ids()


# ===========================================================================
# (d) unregister is the clean inverse
# ===========================================================================
def test_unregister_corpus_makes_it_unfindable(tmp_path, boot_corpus):
    eng, kernel, healer = _server_shaped(boot_corpus)
    root = tmp_path / "temp"
    root.mkdir()
    (root / "t0.lts").write_text(_lts("T0", "temp zero"), encoding="utf-8")
    eng.register_corpus("temp", spec={"root": str(root), "domain_suffix": "proj.temp"})
    assert "temp" in eng.store.corpus_ids()

    res = eng.unregister_corpus("temp")
    assert res["success"] is True, res
    assert "temp" not in eng.store.corpus_ids()
    gone = eng.find(corpus="temp", prefix="T")
    assert gone["success"] is False
    assert "temp" in json.dumps(gone).lower() or "not found" in json.dumps(gone).lower()


def test_unregister_does_not_disturb_other_corpora(tmp_path, boot_corpus):
    eng, kernel, healer = _server_shaped(boot_corpus)
    root = tmp_path / "temp2"
    root.mkdir()
    (root / "u0.lts").write_text(_lts("U0", "u zero"), encoding="utf-8")
    eng.register_corpus("temp2", spec={"root": str(root), "domain_suffix": "proj.temp2"})

    res = eng.unregister_corpus("temp2")
    assert res["success"] is True
    assert eng.find(corpus="boot", prefix="B")["nodes"][0]["id"] == "B0", \
        "unregistering one corpus must not disturb a sibling"


def test_unregister_unseats_kernel_and_forgets_healer(tmp_path, boot_corpus):
    eng, kernel, healer = _server_shaped(boot_corpus)
    root = tmp_path / "temp3"
    root.mkdir()
    (root / "v0.lts").write_text(_lts("V0", "v zero"), encoding="utf-8")
    eng.register_corpus("temp3", spec={"root": str(root), "domain_suffix": "proj.temp3"})
    part = eng.store.get_partition("temp3")

    res = eng.unregister_corpus("temp3")
    assert res["success"] is True
    assert res["was_kernel_seated"] is True
    assert kernel.current_root(part) is None, "kernel must forget the unseated corpus"
    assert kernel.tracked_roots(part) is None
    assert "temp3" not in healer._locks
    assert "temp3" not in healer._last_attempt


def test_reregister_same_id_after_unregister_is_clean(tmp_path, boot_corpus):
    """Re-registering the SAME corpus_id with DIFFERENT content must not inherit stale
    storm-guard memory from the first registration (the reason `SelfHealer.forget`
    exists) and must reflect the new content, not the old."""
    eng, kernel, healer = _server_shaped(boot_corpus)
    root = tmp_path / "cycled"
    root.mkdir()
    (root / "w0.lts").write_text(_lts("W0", "w zero"), encoding="utf-8")
    eng.register_corpus("cycled", spec={"root": str(root), "domain_suffix": "proj.cycled"})
    assert eng.unregister_corpus("cycled")["success"] is True

    root2 = tmp_path / "cycled2"
    root2.mkdir()
    (root2 / "x0.lts").write_text(_lts("X0", "x zero"), encoding="utf-8")
    (root2 / "x1.lts").write_text(_lts("X1", "x one"), encoding="utf-8")
    res = eng.register_corpus("cycled", spec={"root": str(root2),
                                              "domain_suffix": "proj.cycled"})
    assert res["success"] is True
    assert res["total_nodes"] == 4
    found = eng.find(corpus="cycled", prefix="X")
    assert {n["id"] for n in found["nodes"]} == {"X0", "X0-DOC", "X1", "X1-DOC"}


# ===========================================================================
# (e) MCP surface + the list_changed push
# ===========================================================================
def test_schema_lists_registry_tools():
    from factlattice_engine.server import TOOLS
    names = {t["name"] for t in TOOLS}
    assert {"lts_register_corpus", "lts_unregister_corpus"} <= names
    assert set(LTS_REGISTRY_TOOL_NAMES) <= set(LTS_ALL_TOOL_NAMES)
    reg = next(t for t in TOOLS if t["name"] == "lts_register_corpus")
    assert reg["inputSchema"]["required"] == ["corpus_id"]
    unreg = next(t for t in TOOLS if t["name"] == "lts_unregister_corpus")
    assert unreg["inputSchema"]["required"] == ["corpus_id"]


def test_init_advertises_list_changed():
    """The one-line trap this whole capability is worthless without: a client silently
    ignores the push unless `capabilities.tools.listChanged` is True at init."""
    from factlattice_engine.server import Server
    srv = Server(tools=None)
    resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp["result"]["capabilities"]["tools"]["listChanged"] is True


def test_successful_register_queues_list_changed(tmp_path, boot_corpus):
    from factlattice_engine.server import Server
    eng, kernel, healer = _server_shaped(boot_corpus)
    srv = Server(eng)
    root = tmp_path / "pushed"
    root.mkdir()
    (root / "p0.lts").write_text(_lts("P0", "p zero"), encoding="utf-8")

    resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "lts_register_corpus",
        "arguments": {"corpus_id": "pushed", "spec": {"root": str(root),
                                                       "domain_suffix": "proj.pushed"}}}})
    assert resp["result"]["isError"] is False
    queued = srv.drain_notifications()
    assert queued == [{"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}]


def test_successful_unregister_queues_list_changed(tmp_path, boot_corpus):
    from factlattice_engine.server import Server
    eng, kernel, healer = _server_shaped(boot_corpus)
    root = tmp_path / "pushed2"
    root.mkdir()
    (root / "q0.lts").write_text(_lts("Q0", "q zero"), encoding="utf-8")
    eng.register_corpus("pushed2", spec={"root": str(root), "domain_suffix": "proj.pushed2"})

    srv = Server(eng)
    resp = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "lts_unregister_corpus", "arguments": {"corpus_id": "pushed2"}}})
    assert resp["result"]["isError"] is False
    assert srv.drain_notifications() == [
        {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}]


def test_failed_register_queues_nothing(boot_corpus):
    """A REFUSAL is not a surface change: no push for a call that changed nothing. The
    negative control — a notifier that fires on every call regardless of outcome would
    pass every positive test above and still be wrong."""
    from factlattice_engine.server import Server
    eng, kernel, healer = _server_shaped(boot_corpus)
    srv = Server(eng)
    resp = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "lts_register_corpus",
        "arguments": {"corpus_id": "boot", "spec": {"root": "/nonexistent", "domain_suffix": "x"}}}})
    assert resp["result"]["isError"] is True
    assert srv.drain_notifications() == []


def test_read_only_tools_queue_nothing(boot_corpus):
    """The other half of the negative control: an ordinary read (`lts_list_corpora`)
    never queues a push — the mechanism is scoped to `SURFACE_CHANGING_TOOLS`, not
    wired to every successful call."""
    from factlattice_engine.server import Server
    eng, kernel, healer = _server_shaped(boot_corpus)
    srv = Server(eng)
    resp = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "lts_list_corpora", "arguments": {}}})
    assert resp["result"]["isError"] is False
    assert srv.drain_notifications() == []


def test_drain_notifications_is_fifo_and_empties():
    from factlattice_engine.server import Server
    srv = Server(tools=None)
    srv.pending_notifications.append({"a": 1})
    srv.pending_notifications.append({"b": 2})
    assert srv.drain_notifications() == [{"a": 1}, {"b": 2}]
    assert srv.drain_notifications() == []


# ===========================================================================
# the real transport — subprocess, exactly like an MCP client sees it
# ===========================================================================
def test_real_stdio_server_register_no_restart_and_pushes_notification(tmp_path):
    """AT THE OBJECT: spawn the real `python -m factlattice_engine.server` (as
    deployed), register a brand-new corpus over stdio with NO restart, read the
    `notifications/tools/list_changed` push off the SAME stream, query the new corpus,
    then unregister it and observe the same push + the corpus becoming unfindable again."""
    boot_root = tmp_path / "boot"
    boot_root.mkdir()
    (boot_root / "b0.lts").write_text(_lts("B0", "boot zero"), encoding="utf-8")
    cfg = tmp_path / "corpora.json"
    cfg.write_text(json.dumps({
        "boot": {"root": str(boot_root), "domain_suffix": "proj.boot"}}), encoding="utf-8")
    env = dict(os.environ, FACTLATTICE_CORPUS_CONFIG=str(cfg), PYTHONPATH=REPO_ROOT,
               FACTLATTICE_CODE_WATCH="0",
               FACTLATTICE_QUERY_LOG=str(tmp_path / "query-log.jsonl"),
               FACTLATTICE_STATE_DIR=str(tmp_path / "state"))
    proc = subprocess.Popen([sys.executable, "-m", "factlattice_engine.server"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, env=env, cwd=REPO_ROOT)
    rid = [0]

    def rpc_raw(method, params=None):
        rid[0] += 1
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid[0], "method": method,
                                     "params": params or {}}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    def call(name, arguments):
        r = rpc_raw("tools/call", {"name": name, "arguments": arguments})
        return r, json.loads(r["result"]["content"][0]["text"])

    try:
        init = rpc_raw("initialize")
        assert init["result"]["serverInfo"]["name"] == "lts-engine"
        assert init["result"]["capabilities"]["tools"]["listChanged"] is True

        tools = rpc_raw("tools/list")["result"]["tools"]
        names = {t["name"] for t in tools}
        assert {"lts_register_corpus", "lts_unregister_corpus"} <= names

        # A plain read produces exactly ONE line back — no stray push behind it. Prove
        # this by immediately sending a `ping` and getting ITS response next, not a
        # notification.
        _, listed = call("lts_list_corpora", {})
        assert listed["success"] is True
        ping = rpc_raw("ping")
        assert ping["result"] == {}

        new_root = tmp_path / "runtime-added"
        new_root.mkdir()
        (new_root / "r0.lts").write_text(_lts("R0", "runtime zero"), encoding="utf-8")

        # Not yet registered: unfindable.
        _, pre = call("lts_find", {"corpus": "runtime-added", "prefix": "R"})
        assert pre["success"] is False

        resp, reg = call("lts_register_corpus", {
            "corpus_id": "runtime-added",
            "spec": {"root": str(new_root), "domain_suffix": "proj.runtime"}})
        assert resp["result"]["isError"] is False
        assert reg["success"] is True and reg["total_nodes"] == 2

        # THE PUSH: the very next line on the stream is the notification, over the REAL
        # transport, with no request behind it (no `id`).
        push = json.loads(proc.stdout.readline())
        assert push == {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}

        # Queryable with NO restart of this same process.
        _, found = call("lts_find", {"corpus": "runtime-added", "prefix": "R0"})
        assert found["success"] is True
        assert {n["id"] for n in found["nodes"]} == {"R0", "R0-DOC"}

        resp2, unreg = call("lts_unregister_corpus", {"corpus_id": "runtime-added"})
        assert resp2["result"]["isError"] is False
        assert unreg["success"] is True
        push2 = json.loads(proc.stdout.readline())
        assert push2 == {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}

        _, post = call("lts_find", {"corpus": "runtime-added", "prefix": "R"})
        assert post["success"] is False

        # `boot` was never disturbed by any of the above.
        _, boot_found = call("lts_find", {"corpus": "boot", "prefix": "B0"})
        assert boot_found["success"] is True
        assert {n["id"] for n in boot_found["nodes"]} == {"B0", "B0-DOC"}
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=10)
