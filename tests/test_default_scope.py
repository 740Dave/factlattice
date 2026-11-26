"""specs/37-default-scope.lts: an unnamed search reads a named list, says so, and everything stays reachable by name.

Runs a real server process over MCP stdio on a scratch tree, with only the registry and this variable set, because a
default set in a launcher's environment is a claim about that entry point and not about a function chosen here.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ("a1", "a2", "b1")


def _doc(c, ref=""):
    see = f"- see-also: {ref}\n" if ref else ""
    return (f'---\nlts: "1.1"\ntitle: {c}\ndomain: x.{c}\ndoctype: SPEC\n---\n\n# {c} root [{c.upper()}-ROOT]\n\n'
            f"- summary: root\n\n## Node in {c} [{c.upper()}-NODE]\n\n- part-of: [{c.upper()}-ROOT]\n- topic: lanternfish\n"
            f"{see}\nBody prose mentions zebrafin here.\n")


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    t = tmp_path_factory.mktemp("scope")
    for d in ("home", "state", "corpora"):
        (t / d).mkdir()
    reg = {}
    for c in PUBLIC + ("secret",):
        (t / "corpora" / c).mkdir()
        (t / "corpora" / c / f"{c}.lts").write_text(_doc(c, "[x.b1:B1-NODE]" if c == "a1" else ""), encoding="utf-8")
        reg[c] = {"roots": [{"path": str(t / "corpora" / c)}], "domain_suffix": f"x.{c}"}
    reg["secret"]["private"] = True
    (t / "reg.json").write_text(json.dumps(reg), encoding="utf-8")
    return t


class Srv:
    def __init__(self, tree, listing):
        env = {"PATH": os.environ["PATH"], "HOME": str(tree / "home"), "FACTLATTICE_CORPUS_CONFIG": str(tree / "reg.json"),
               "FACTLATTICE_STATE_DIR": str(tree / "state"), "PYTHONPATH": str(ROOT)}
        if listing is not None:
            env["LTS_DEFAULT_CORPORA"] = listing
        self.p = subprocess.Popen([sys.executable, "-m", "factlattice_engine.server"], cwd=str(tree), env=env, text=True,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.n = 1
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.p.stdin.flush()

    def _rpc(self, method, params):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            assert line, "server closed"
            r = json.loads(line)
            if r.get("id") == self.n:
                return r

    def call(self, tool, **args):
        r = self._rpc("tools/call", {"name": tool, "arguments": args})
        return json.loads(r["result"]["content"][0]["text"])

    def settle(self, ids):
        """Wait until every named corpus is served. The server answers while corpora are still PENDING (spec 38 x-server): a call naming a pending corpus waits
        for it, but a search with a default list waits only for the corpora ON the list, so a corpus off the list can still be pending when the search runs, and
        the search then counts it differently (a pending public corpus is not yet in left_out_count; a pending private one is counted in `private-corpus-failed`,
        not `private: name it explicitly`, spec 38 x-list). These tests assert the settled state, so they take it explicitly; tests/test_default_scope_boot_window.py
        pins the pending state on purpose."""
        for c in ids:
            r = self.call("lts_find", corpus=c, prefix="NOSUCH-")
            assert r.get("success") is not False, (c, r)

    def close(self):
        self.p.terminate()
        self.p.wait()


@pytest.fixture()
def srv(tree):
    made = []

    def make(listing=None, settle=True):
        s = Srv(tree, listing)
        made.append(s)
        if settle:
            s.settle(PUBLIC + ("secret",))
        return s
    yield make
    for s in made:
        s.close()


def corpora_of(r):
    return sorted({n["corpus"] for n in (r.get("nodes") or r.get("hits") or [])})


FIND = dict(text="lanternfish")
PROSE = dict(text="zebrafin")


@pytest.mark.parametrize("listing", [None, "", " , "], ids=["unset", "empty", "only-commas"])
def test_c1_no_list_means_no_change(srv, listing):
    s = srv(listing)
    for tool, a in (("lts_find", FIND), ("lts_find_in_prose", PROSE)):
        r = s.call(tool, **a)
        assert corpora_of(r) == list(PUBLIC)
        assert "search_scope" not in r["extent"]


@pytest.mark.parametrize("tool,a", [("lts_find", FIND), ("lts_find_in_prose", PROSE)], ids=["find", "prose"])
def test_c2_a_list_narrows_and_says_so(srv, tool, a):
    r = srv("a1, a2").call(tool, **a)
    assert corpora_of(r) == ["a1", "a2"]
    sc = r["extent"]["search_scope"]
    assert sc["source"] == "server-list" and sc["left_out_count"] == 1 and "all" in sc["widen"]
    assert "stale_count" not in sc
    assert "zero_in_scope" not in sc and "unknown_count" not in sc
    assert r["extent"]["corpora_consulted"] == ["a1", "a2"]


def test_c2_a_zero_says_it_is_a_zero_in_scope(srv):
    r = srv("a1").call("lts_find", prefix="B1-")
    assert r.get("count", r.get("total_matched")) == 0 and r["extent"]["search_scope"]["zero_in_scope"] is True


def test_c2_the_call_names_a_scope_not_all_loaded(srv):
    assert srv("a1").call("lts_find", **FIND)["extent"]["scope"]["corpus"] == "SEARCH SCOPE"


def test_c2_duplicates_read_once(srv):
    r = srv("a1,a1, a1").call("lts_find", **FIND)
    assert [n["corpus"] for n in r["nodes"]] == ["a1"] and r["extent"]["corpora_consulted"] == ["a1"]
    r = srv(None).call("lts_find", corpora=["a1", "a1"], **FIND)
    assert [n["corpus"] for n in r["nodes"]] == ["a1"]


def test_c4_a_name_listed_twice_counts_once(srv):
    assert srv("a1,nosuch,nosuch").call("lts_find", **FIND)["extent"]["search_scope"]["unknown_count"] == 1


def test_c3_a_call_all_still_says_all_loaded(srv):
    assert srv("a1").call("lts_find", corpora="all", **FIND)["extent"]["scope"]["corpus"] == "ALL LOADED"


def test_c2_two_stale_corpora_count_two(srv, tree):
    fs = [tree / "corpora" / c / f"{c}.lts" for c in ("a1", "a2")]
    s = srv("a1,a2")
    assert s.call("lts_find", **FIND)["count"] == 2
    for f in fs:
        os.chmod(f, 0)
    try:
        assert s.call("lts_find", **FIND)["extent"]["search_scope"]["stale_count"] == 2
    finally:
        for f in fs:
            os.chmod(f, 0o644)


def test_c2_a_stale_corpus_in_scope_is_not_a_clean_zero(srv, tree):
    f = tree / "corpora" / "a2" / "a2.lts"
    s = srv("a2")
    assert s.call("lts_find", **FIND)["count"] == 1, "control: readable first"
    os.chmod(f, 0)
    try:
        r = s.call("lts_find", **FIND)
    finally:
        os.chmod(f, 0o644)
    sc = r["extent"]["search_scope"]
    assert sc["stale_count"] == 1 and "zero_in_scope" not in sc, r["extent"]


def test_log_records_source_and_count_never_names(srv, tree):
    srv("a1").call("lts_find", **FIND)
    rec = json.loads((tree / "state" / "query-log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert rec["extent"]["search_scope"] == {"source": "server-list", "left_out_count": 2}


def test_cli_states_the_lists_size_not_its_names(tree):
    env = {"PATH": os.environ["PATH"], "HOME": str(tree / "home"), "FACTLATTICE_CORPUS_CONFIG": str(tree / "reg.json"),
           "FACTLATTICE_STATE_DIR": str(tree / "state"), "PYTHONPATH": str(ROOT), "LTS_DEFAULT_CORPORA": "a1, a2,a1"}
    o = subprocess.run([sys.executable, "-m", "factlattice_engine", "call", "lts_find", "--args-json", json.dumps(FIND)],
                       cwd=str(tree), env=env, capture_output=True, text=True)
    assert o.returncode == 0, o.stderr
    assert "default list of 2 corpora" in o.stderr, o.stderr
    j = json.loads(o.stdout[o.stdout.index("{"):])
    assert j["extent"]["search_scope"]["source"] == "server-list" and j["extent"]["corpora_consulted"] == ["a1", "a2"]


@pytest.mark.parametrize("falsy", [0, False, [], ""], ids=["zero", "false", "empty-list", "empty-str"])
def test_c1_a_falsy_corpus_still_reads_as_omitted(srv, falsy):
    r = srv(None).call("lts_find", corpus=falsy, **FIND)
    assert corpora_of(r) == list(PUBLIC) and "search_scope" not in r["extent"]


def test_c3_not_a_wall(srv):
    s = srv("a1,a2")
    assert corpora_of(s.call("lts_find", corpus="b1", **FIND)) == ["b1"]
    r = s.call("lts_find", corpus="b1", **FIND)
    assert "search_scope" not in r["extent"]
    assert s.call("lts_node", corpus="b1", node_id="B1-NODE")["success"] is True
    assert "b1" in s.call("lts_find", **FIND)["extent"]["corpora_available"]
    assert "b1" in [c["corpus_id"] for c in s.call("lts_list_corpora", heal=False)["corpora"]]
    r = s.call("lts_find", corpora="all", **FIND)
    assert corpora_of(r) == list(PUBLIC) and r["extent"]["search_scope"]["source"] == "call-all"
    assert r["extent"]["search_scope"]["left_out_count"] == 0 and "widen" not in r["extent"]["search_scope"]
    r = s.call("lts_find", corpora=["b1"], **FIND)
    assert corpora_of(r) == ["b1"] and r["extent"]["search_scope"]["source"] == "call-list"
    assert r["extent"]["search_scope"]["left_out_count"] == 2


def test_c3_a_call_list_works_on_a_server_with_none(srv):
    r = srv(None).call("lts_find_in_prose", corpora=["a1", "b1"], **PROSE)
    assert corpora_of(r) == ["a1", "b1"] and r["extent"]["search_scope"]["source"] == "call-list"


def test_c3_edges_and_counts_stay_whole(srv):
    s = srv("b1")
    refs = s.call("lts_reverse", corpus="b1", node_id="B1-NODE")["referrers"]
    assert "a1" in {x["corpus"] for x in refs}
    whole = srv(None).call("lts_aggregate", op="count")
    scoped = s.call("lts_aggregate", op="count")
    assert whole["success"] is True and whole["total"] == scoped["total"] and whole["total"] >= 6, whole
    assert "search_scope" not in json.dumps(scoped)


def test_c3_the_reviewers_site_config_lookup_reads_everything(tree, monkeypatch):
    """The real reviewer path, with the site config in a corpus off the list. The control shows the list would hide it."""
    from factlattice_engine import tools
    from factlattice_engine.reviewer import __main__ as M
    from factlattice_engine.reviewer import lookup_site_config
    f = tree / "corpora" / "a2" / "site.lts"
    f.write_text('---\nlts: "1.1"\ntitle: s\ndomain: x.a2\ndoctype: SPEC\n---\n\n# Site [REVIEWER-SITE-CONFIG]\n\n- summary: s\n',
                 encoding="utf-8")
    try:
        monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(tree / "reg.json"))
        monkeypatch.setenv("LTS_DEFAULT_CORPORA", "a1")
        from factlattice_engine.server import build_tools
        engine = build_tools(watch=False, require_corpora=False)
        hidden = lookup_site_config(lambda **kw: tools.dispatch("lts_find", kw, tools=engine))
        assert hidden.status == "none", "control: the list must hide it from a plain dispatch"
        assert M._site_config(engine).status != "none"
    finally:
        f.unlink()


def test_c4_private_on_the_list_is_not_read_or_named(srv):
    r = srv("a1,secret").call("lts_find", **FIND)
    assert corpora_of(r) == ["a1"]
    assert "secret" not in json.dumps(r)
    assert {"reason": "private: name it explicitly", "count": 1} in r["extent"]["corpora_skipped"]


def test_c4_a_call_naming_private_reads_it(srv):
    r = srv(None).call("lts_find", corpora=["secret"], **FIND)
    assert corpora_of(r) == ["secret"]
    assert not [x for x in r["extent"]["corpora_skipped"] if str(x.get("reason", "")).startswith("private")]


def test_c4_unknown_names_are_counted_never_named(srv):
    r = srv("a1,nosuch-corpus").call("lts_find", **FIND)
    assert corpora_of(r) == ["a1"] and r["extent"]["search_scope"]["unknown_count"] == 1
    assert "nosuch-corpus" not in json.dumps(r)


def test_c4_all_unknown_reads_nothing_and_never_falls_back(srv):
    r = srv("nosuch").call("lts_find", **FIND)
    assert corpora_of(r) == [] and r["extent"]["corpora_consulted"] == []
    assert r["extent"]["search_scope"]["unknown_count"] == 1 and r["extent"]["search_scope"]["zero_in_scope"] is True


@pytest.mark.parametrize("args,says", [
    (dict(corpora=["a1"], corpus="a1"), "exclusive"), (dict(corpora=[]), "non-empty list"), (dict(corpora=[1]), "non-empty list"),
    (dict(corpora="a1"), "non-empty list"), (dict(corpora=3), "non-empty list"), (dict(corpora={"a": 1}), "non-empty list"),
    (dict(corpora=["nosuch"]), "corpus not found"), (dict(corpus=["a1"]), "names one corpus"), (dict(corpus={"a": 1}), "names one corpus"),
    (dict(corpus=7), "corpus not found"),
], ids=["with-corpus", "empty", "non-string", "other-string", "number", "mapping", "unknown", "corpus-list", "corpus-mapping", "corpus-scalar"])
@pytest.mark.parametrize("tool,a", [("lts_find", FIND), ("lts_find_in_prose", PROSE)], ids=["find", "prose"])
def test_c5_refusals(srv, tool, a, args, says):
    r = srv(None).call(tool, **a, **args)
    assert r.get("success") is False and "TypeError" not in str(r.get("error")), r
    assert not (r.get("nodes") or r.get("hits"))
    assert says in str(r.get("error")), r


def test_c5_corpora_with_path_is_refused(srv, tree):
    r = srv(None).call("lts_find", path=str(tree / "corpora" / "a1" / "a1.lts"), corpora=["a1"], **FIND)
    assert r.get("success") is False


def test_a_cold_tool_description_says_it_exists(srv):
    tools = srv(None)._rpc("tools/list", {})["result"]["tools"]
    for name in ("lts_find", "lts_find_in_prose"):
        t = next(x for x in tools if x["name"] == name)
        assert "corpora" in t["inputSchema"]["properties"]
