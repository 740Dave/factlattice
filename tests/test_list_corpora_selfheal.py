"""[LIST-CORPORA-SELF-HEAL] — `lts_list_corpora` heals stale corpora before reporting.

Spec: (internal design note)

THE FOOT-GUN THIS CLOSES. `lts_list_corpora` reported a corpus `fresh:false` and could
not clear it on retry, because it was a pure status read that never reached the
[LEC-SELF-HEAL] choke point (`LtsEngine._gate`). A caller re-queried `list` twice with no
effect. The fix adds ONLY a trigger point: for each corpus the freshness gate calls stale,
`list_corpora` routes it through the SAME `_gate` the query tools use (same healer, same
locking, same incremental re-parse, same re-verify-before-serve), then reports post-heal.

The acceptance bar, and where each item is proven (numbering = the spec's):

  1. HEALS BY DEFAULT        -> test_default_list_heals_a_stale_corpus
                                test_default_list_via_dispatch_heals
  2. heal=false: NO HEAL, NO SIDE EFFECT
                             -> test_heal_false_reports_stale_and_does_not_mutate
                                test_heal_false_via_dispatch_is_side_effect_free
  3. FAIL-SOFT               -> test_fail_soft_deleted_root_real_on_disk
                                test_fail_soft_rebuild_raises
                                test_fail_soft_even_if_the_healer_breaks_its_contract
                                test_heal_requested_but_no_healer_wired
  4. NO NEEDLESS WORK        -> test_already_fresh_corpora_are_never_healed_or_reparsed
                                test_only_the_stale_corpus_is_reparsed_and_only_its_changed_file
  5. SHARED STATE            -> test_heal_persists_on_the_shared_engine

  MCP surface                -> test_schema_advertises_heal_param
                                test_mcp_handle_round_trip
                                test_real_stdio_server_heals_on_list (subprocess, the
                                real `python -m factlattice_engine.server`)

Every test builds REAL `.lts` files on disk and the REAL server wiring
(`FreshnessKernel` + `PollVerifiedFreshness` + `SelfHealer`, exactly as
`server.build_tools` composes them). No mock of the heal or of freshness is used except
in the two tests whose whole point is a heal that raises.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

import factlattice.hashing as _hashing
from factlattice_engine import (
    FreshnessKernel, LtsEngine, LtsStore, PollVerifiedFreshness, SelfHealer,
)
from factlattice_engine.config import build_merged_partition, build_partition
from factlattice_engine.incremental import CorpusIndex
from factlattice_engine.tools import dispatch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ===========================================================================
# fixtures — real files on disk, server-shaped wiring
# ===========================================================================
def _lts(node_id: str, definition: str) -> str:
    return ("---\ndomain: core.example\n---\n"
            f"# Test [SPEC:{node_id}-DOC]\n\n"
            f"### [{node_id}] Node {node_id}\n- definition: {definition}\n")


@pytest.fixture
def two_corpora(tmp_path):
    """`p` = the deployment shape (merged decisions+specs); `c` = a plain single root."""
    dec = tmp_path / "decisions"
    specs = tmp_path / "specs"
    single = tmp_path / "single"
    for d in (dec, specs, single):
        d.mkdir()
    (dec / "decision-graph-p.lts").write_text(
        "---\ndomain: core.p\n---\n# D [SYSTEM:D-DOC]\n\n"
        "### [DEC-1] One\n- definition: one\n", encoding="utf-8")
    for i in range(5):
        (specs / f"s{i}.lts").write_text(_lts(f"SPC{i}", f"spec {i}"), encoding="utf-8")
    for i in range(4):
        (single / f"n{i}.lts").write_text(_lts(f"N{i}", f"node {i}"), encoding="utf-8")
    p = build_merged_partition("p", [{"path": str(dec), "area": "decisions"},
                                     {"path": str(specs), "area": "specs"}], "proj.p")
    c = build_partition("c", str(single), "proj.c")
    return {"dec": dec, "specs": specs, "single": single, "p": p, "c": c}


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


def _append_dec2(dec):
    with open(dec / "decision-graph-p.lts", "a", encoding="utf-8") as fh:
        fh.write("\n### [DEC-2] Two\n- definition: two\n- depends-on: [DEC-1]\n")


def _entry(res, cid):
    return next(c for c in res["corpora"] if c["corpus_id"] == cid)


class _HealSpy:
    """Counts `SelfHealer.heal` invocations without changing what it does."""

    def __init__(self, healer):
        self.calls = []
        real = healer.heal

        def counting(part):
            self.calls.append(part.corpus_id)
            return real(part)

        healer.heal = counting


# ===========================================================================
# 1. HEALS BY DEFAULT
# ===========================================================================
def test_default_list_heals_a_stale_corpus(two_corpora):
    """Edit a corpus source -> the DEFAULT `list_corpora()` reports it fresh, with the
    post-heal counts. Pre-fix this returned `fresh:false` forever (the foot-gun)."""
    t = two_corpora
    eng, kernel, healer = _server_shaped(t["p"], t["c"])
    before = _entry(eng.list_corpora(), "p")
    assert before["fresh"] is True and before["total_nodes_live"] >= 2

    _append_dec2(t["dec"])          # NO watcher poll: verify-on-query must see it
    assert eng.freshness_provider.evaluate(t["p"]).fresh is False, \
        "precondition: the edit must make the corpus stale"

    res = eng.list_corpora()
    assert res["success"] is True
    p = _entry(res, "p")
    assert p["fresh"] is True, p
    assert res["stale_corpora"] == []
    assert "heal_error" not in p
    assert p["total_nodes_live"] == before["total_nodes_live"] + 1, \
        "the report must be POST-heal: the node just written is counted"
    assert p["counts_are_as_of"] == "the live corpus content"
    assert res["healed_corpora"] == ["p"], \
        "the staleness signal is preserved as `healed_corpora`, not erased"
    assert res["extent"]["index"]["fresh"] is True
    assert res["extent"]["index"]["stale_corpora"] == []
    assert healer.rebuilds == 1 and healer.healed == 1


def test_default_list_via_dispatch_heals(two_corpora):
    """The transport-agnostic door (`dispatch`) with NO arguments — what a bare MCP
    `lts_list_corpora()` call reaches — heals too."""
    t = two_corpora
    eng, kernel, healer = _server_shaped(t["p"], t["c"])
    (t["single"] / "n2.lts").write_text(_lts("N2", "node 2 EDITED"), encoding="utf-8")

    res = dispatch("lts_list_corpora", {}, tools=eng)
    assert res["success"] is True
    assert _entry(res, "c")["fresh"] is True
    assert res["stale_corpora"] == [] and res["healed_corpora"] == ["c"]


def test_explicit_null_heal_means_the_default_not_off(two_corpora):
    """`{"heal": null}` must not be coerced to False and silently disable the heal."""
    t = two_corpora
    eng, kernel, healer = _server_shaped(t["p"], t["c"])
    _append_dec2(t["dec"])
    res = dispatch("lts_list_corpora", {"heal": None}, tools=eng)
    assert res["stale_corpora"] == [] and res["healed_corpora"] == ["p"]


# ===========================================================================
# 2. heal=false — today's behaviour, and NO side effect
# ===========================================================================
def test_heal_false_reports_stale_and_does_not_mutate(two_corpora):
    t = two_corpora
    eng, kernel, healer = _server_shaped(t["p"], t["c"])
    spy = _HealSpy(healer)
    hash_before = t["p"].files_hash
    nodes_before = dict(t["p"].nodes)
    t["p"].corpus_index.reset_parse_ops()

    _append_dec2(t["dec"])
    for _ in range(2):              # the second call proves the first did not heal
        res = eng.list_corpora(heal=False)
        assert res["success"] is True
        p = _entry(res, "p")
        assert p["fresh"] is False
        assert res["stale_corpora"] == ["p"]
        # heal=false is TODAY'S response shape: none of the new fields appear.
        assert "heal_error" not in p
        assert "healed_corpora" not in res

    assert spy.calls == [], "heal=false must never reach the healer"
    assert healer.rebuilds == 0
    assert t["p"].files_hash == hash_before and t["p"].nodes == nodes_before, \
        "heal=false must not mutate the partition"
    assert t["p"].corpus_index.parse_ops == 0


def test_heal_false_via_dispatch_is_side_effect_free(two_corpora):
    t = two_corpora
    eng, kernel, healer = _server_shaped(t["p"], t["c"])
    _append_dec2(t["dec"])
    for _ in range(2):
        res = dispatch("lts_list_corpora", {"heal": False}, tools=eng)
        assert res["success"] is True and res["stale_corpora"] == ["p"]
    assert healer.rebuilds == 0


# ===========================================================================
# 3. FAIL-SOFT — a heal failure degrades ONE entry, never the call
# ===========================================================================
def test_fail_soft_deleted_root_real_on_disk(two_corpora):
    """UNMOCKED. A corpus whose root vanished cannot be re-indexed: the real heal fails
    (FileNotFoundError, fail-closed). `list_corpora` must not raise; that entry is
    `fresh:false` + `heal_error`; the OTHER corpus — itself stale — still heals and
    reports normally.

    (Why not a "malformed .lts"? Measured while building this: the parser is permissive —
    invalid UTF-8 and structurally garbage `.lts` both re-index CLEANLY, so a malformed
    file does not make a heal fail. A deleted root is the real on-disk failure.)"""
    t = two_corpora
    eng, kernel, healer = _server_shaped(t["p"], t["c"])
    _append_dec2(t["dec"])                 # p: stale, healable
    shutil.rmtree(t["single"])             # c: stale, NOT healable

    res = eng.list_corpora()               # must not raise
    assert res["success"] is True
    c = _entry(res, "c")
    assert c["fresh"] is False
    assert "FileNotFoundError" in c["heal_error"], c
    assert " " in c["heal_error"].split(":")[0], c["heal_error"]          # the fixed SENTENCE, not the bare heal code
    p = _entry(res, "p")
    assert p["fresh"] is True and "heal_error" not in p
    assert res["stale_corpora"] == ["c"]
    assert res["healed_corpora"] == ["p"]

    # And it stays soft on retry — no raise, same honest answer.
    again = eng.list_corpora()
    assert again["success"] is True and _entry(again, "c")["heal_error"]


def test_fail_soft_rebuild_raises(two_corpora, monkeypatch):
    """A re-index that RAISES (the "newly-malformed source" case the spec names)."""
    t = two_corpora
    eng, kernel, healer = _server_shaped(t["p"], t["c"])
    before_hash = t["p"].files_hash
    _append_dec2(t["dec"])

    def boom(self, *a, **kw):
        raise RuntimeError("simulated malformed corpus")

    monkeypatch.setattr(CorpusIndex, "incremental_build", boom)
    monkeypatch.setattr(CorpusIndex, "cold_build", boom)

    res = eng.list_corpora()
    assert res["success"] is True
    p = _entry(res, "p")
    assert p["fresh"] is False
    assert "simulated malformed corpus" in p["heal_error"]
    assert _entry(res, "c")["fresh"] is True and "heal_error" not in _entry(res, "c")
    assert res["stale_corpora"] == ["p"] and res["healed_corpora"] == []
    assert t["p"].files_hash == before_hash, "a failed heal publishes NOTHING"


def test_fail_soft_even_if_the_healer_breaks_its_contract(two_corpora):
    """`SelfHealer.heal` promises never to raise. `list_corpora` does not rely on that
    promise: a healer that raises anyway still cannot fail the call."""
    t = two_corpora
    eng, kernel, healer = _server_shaped(t["p"], t["c"])
    _append_dec2(t["dec"])

    def broken(part):
        raise ValueError("healer contract breach")

    healer.heal = broken
    res = eng.list_corpora()
    assert res["success"] is True
    p = _entry(res, "p")
    assert p["fresh"] is False and "healer contract breach" in p["heal_error"]
    assert _entry(res, "c")["fresh"] is True


def test_heal_requested_but_no_healer_wired(two_corpora):
    """FACTLATTICE_SELF_HEAL=0 (no healer): the default call cannot heal, and SAYS so
    rather than silently reporting stale as if it had tried."""
    t = two_corpora
    eng, kernel, _ = _server_shaped(t["p"], t["c"], healer=False)
    _append_dec2(t["dec"])
    res = eng.list_corpora()
    assert res["success"] is True
    p = _entry(res, "p")
    assert p["fresh"] is False
    assert "not wired" in p["heal_error"]


# ===========================================================================
# 4. NO NEEDLESS WORK — an already-fresh corpus is never healed or re-parsed
# ===========================================================================
def test_already_fresh_corpora_are_never_healed_or_reparsed(two_corpora, monkeypatch):
    t = two_corpora
    eng, kernel, healer = _server_shaped(t["p"], t["c"])
    eng.list_corpora()                      # settle
    spy = _HealSpy(healer)
    for part in (t["p"], t["c"]):
        part.corpus_index.reset_parse_ops()
    hashes = []
    real = _hashing.file_content_hash
    monkeypatch.setattr(_hashing, "file_content_hash",
                        lambda path: hashes.append(path) or real(path))
    kernel.hash_ops = 0

    for _ in range(10):
        res = eng.list_corpora()
        assert res["stale_corpora"] == [] and res["healed_corpora"] == []

    assert spy.calls == [], "a fresh corpus must never reach the healer"
    assert healer.rebuilds == 0
    assert t["p"].corpus_index.parse_ops == 0 and t["c"].corpus_index.parse_ops == 0
    assert kernel.hash_ops == 0 and hashes == [], \
        "a fresh list must not content-hash anything (O(0) preserved)"


def test_only_the_stale_corpus_is_reparsed_and_only_its_changed_file(two_corpora):
    t = two_corpora
    eng, kernel, healer = _server_shaped(t["p"], t["c"])
    spy = _HealSpy(healer)
    for part in (t["p"], t["c"]):
        part.corpus_index.reset_parse_ops()

    (t["specs"] / "s3.lts").write_text(_lts("SPC3", "spec 3 REVISED"), encoding="utf-8")
    res = eng.list_corpora()
    assert _entry(res, "p")["fresh"] is True

    assert spy.calls == ["p"], "only the stale corpus is handed to the healer"
    assert t["c"].corpus_index.parse_ops == 0, "the fresh corpus is not re-parsed"
    assert t["p"].corpus_index.parse_ops == 1, \
        "the SAME incremental heal: only the one changed file is re-parsed"
    assert t["p"].corpus_index.incremental_rebuilds == 1


# ===========================================================================
# 5. SHARED STATE — the heal persists on the engine every tool shares
# ===========================================================================
def test_heal_persists_on_the_shared_engine(two_corpora):
    t = two_corpora
    eng, kernel, healer = _server_shaped(t["p"], t["c"])
    _append_dec2(t["dec"])

    assert _entry(eng.list_corpora(), "p")["fresh"] is True          # heals
    after = eng.list_corpora(heal=False)                              # observes only
    assert _entry(after, "p")["fresh"] is True and after["stale_corpora"] == []

    # ...and the query tools see the healed index with NO second rebuild.
    found = eng.find(corpus="p", prefix="DEC-")
    assert found["success"] is True
    assert [n["id"] for n in found["nodes"]] == ["DEC-1", "DEC-2"]
    assert healer.rebuilds == 1


# ===========================================================================
# the MCP surface
# ===========================================================================
def test_schema_advertises_heal_param():
    from factlattice_engine.server import TOOLS
    spec = next(t for t in TOOLS if t["name"] == "lts_list_corpora")
    heal = spec["inputSchema"]["properties"]["heal"]
    assert heal["type"] == "boolean"
    assert "default true" in heal["description"].lower()
    assert "heal_error" in spec["description"]


def test_mcp_handle_round_trip(two_corpora):
    """Through `server.Server.handle` — the JSON-RPC layer the stdio loop drives."""
    from factlattice_engine.server import Server
    t = two_corpora
    eng, kernel, healer = _server_shaped(t["p"], t["c"])
    srv = Server(eng)
    _append_dec2(t["dec"])

    def call(args):
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "lts_list_corpora", "arguments": args}})
        assert r["result"]["isError"] is False
        return json.loads(r["result"]["content"][0]["text"])

    assert call({"heal": False})["stale_corpora"] == ["p"]
    assert call({})["stale_corpora"] == []
    assert call({"heal": False})["stale_corpora"] == []


def test_real_stdio_server_heals_on_list(tmp_path):
    """AT THE OBJECT: spawn the real `python -m factlattice_engine.server` over stdio
    (watcher on, as deployed; code-currency re-exec off so the test cannot bounce),
    edit a corpus file, and call `lts_list_corpora` exactly as an MCP client does."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "x.lts").write_text(_lts("X1", "x one"), encoding="utf-8")
    cfg = tmp_path / "corpora.json"
    cfg.write_text(json.dumps({"proj": {"roots": [{"path": str(root), "area": "specs"}],
                                        "domain_suffix": "proj.p"}}), encoding="utf-8")
    env = dict(os.environ, FACTLATTICE_CORPUS_CONFIG=str(cfg), PYTHONPATH=REPO_ROOT,
               FACTLATTICE_CODE_WATCH="0",
               # keep the subprocess out of the developer's real query log
               FACTLATTICE_QUERY_LOG=str(tmp_path / "query-log.jsonl"),
               FACTLATTICE_STATE_DIR=str(tmp_path / "state"))
    proc = subprocess.Popen([sys.executable, "-m", "factlattice_engine.server"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, env=env, cwd=REPO_ROOT)
    rid = [0]

    def rpc(method, params=None):
        rid[0] += 1
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid[0], "method": method,
                                     "params": params or {}}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    def list_corpora(args):
        r = rpc("tools/call", {"name": "lts_list_corpora", "arguments": args})
        return json.loads(r["result"]["content"][0]["text"])

    try:
        assert rpc("initialize")["result"]["serverInfo"]["name"] == "lts-engine"
        tools = rpc("tools/list")["result"]["tools"]
        lc = next(t for t in tools if t["name"] == "lts_list_corpora")
        assert lc["inputSchema"]["properties"]["heal"]["type"] == "boolean"

        assert list_corpora({})["stale_corpora"] == []
        (root / "y.lts").write_text(_lts("Y1", "y one"), encoding="utf-8")

        observed = list_corpora({"heal": False})
        assert observed["stale_corpora"] == ["proj"], observed
        healed = list_corpora({})
        assert healed["stale_corpora"] == [] and healed["healed_corpora"] == ["proj"]
        assert healed["corpora"][0]["fresh"] is True
        assert list_corpora({"heal": False})["stale_corpora"] == []
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=10)
