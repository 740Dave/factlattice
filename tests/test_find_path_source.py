"""[A-E7 / X7 FIND-PATH-SOURCE] `lts_find(path=<one .lts file>)`: the file is parsed in isolation (the one-file
partition is built by the engine's own per-file parser, `incremental.RootIndex`), exclusive with `corpus`.
READ-ONLY, no index. Refuses what has no meaning for a lone file (`semantic` other than off, `word_fallback`,
`rank='relevance'`, `include_reverse_digest`, `source_file`). The path is logged as a digest, never literally.
The field caches are never shared with a corpus partition. No refusal or extent text names a private corpus."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from factlattice_engine import config, querylog, server, tools
from factlattice_engine.tools import LtsEngine, dispatch
from tests import qs_fixture as F

CANARY = "secretcorp-canary-7f3a"


@pytest.fixture()
def world(tmp_path):
    """A one-file corpus `one` and the SAME file, byte for byte, elsewhere (the path under test)."""
    d1 = tmp_path / "one"
    d1.mkdir()
    (d1 / "a.lts").write_text(F.A, encoding="utf-8")
    d2 = tmp_path / "loose"
    d2.mkdir()
    f = d2 / "a.lts"
    f.write_text(F.A, encoding="utf-8")
    eng = LtsEngine(config.build_store({"one": {"roots": [{"path": str(d1), "area": None}],
                                                 "domain_suffix": "plt.one"}}))
    return eng, f


SHAPES = [
    dict(text="borrower card contents"),
    dict(text="borrower contents"),                                   # a zero
    dict(prefix="PROC-"),
    dict(status="operational"),
    dict(all_of=["live"]),
    dict(all_of=["qa"], match="word"),
    dict(all_of=["lantern"], match="prefix"),
    dict(any_of=["qatar", "zorblax"]),
    dict(all_of=["live"], none_of=["now"]),
    dict(where=[{"key": "tier", "equals": "gold"}]),
    dict(where=[{"key": "tier", "one_of": ["gold", "silver"]}]),
    dict(where=[{"key": "alias", "has": True}]),
    dict(where=[{"key": "kind", "not_equals": "note"}]),
    dict(fields=["title"], all_of=["notes"]),
    dict(document_tag="SPEC"),
    dict(prefix="PROC-", limit=2, order_by={"field": "tier", "direction": "asc"}),
    dict(prefix="PROC-", tier="digest"),
    dict(prefix="PROC-", tier="page", token_budget=400),
    dict(since="2026-09-10", until="2026-09-30"),
    dict(prefix="PROC-", explain=True, all_of=["go"]),
    dict(text="zzz-no-such-thing"),
]


def _norm(r):
    """Everything that legitimately differs between a registered corpus and a loose file: the corpus
    LABEL, where the bytes live (root_paths), the index provenance, and `file_source`. Everything else must
    be equal, key for key."""
    r = copy.deepcopy(r)
    r.pop("file_source", None)
    ext = r.get("extent") or {}
    for k in ("corpora_consulted", "corpora_available", "root_paths", "built_at", "index"):
        ext.pop(k, None)
    (ext.get("scope") or {}).pop("corpus", None)
    for n in (r.get("nodes") or []) + (r.get("digest") or []):
        n.pop("corpus", None)
    return r


def test_there_are_at_least_twenty_shapes():
    assert len(SHAPES) >= 20


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: ",".join(s))
def test_path_find_equals_find_on_a_one_file_corpus(world, shape):
    eng, f = world
    via_corpus = dispatch("lts_find", {"corpus": "one", "semantic": "off", **shape}, eng)
    via_path = dispatch("lts_find", {"path": str(f), "semantic": "off", **shape}, eng)
    assert via_corpus.get("success") is True, via_corpus
    assert via_path.get("success") is True, via_path
    assert _norm(via_corpus) == _norm(via_path)
    # the answer itself, whatever its envelope: same ids, same counts, same order
    ids = lambda r: [n["id"] for n in (r.get("nodes") or r.get("digest") or [])]
    assert ids(via_corpus) == ids(via_path)
    for k in ("count", "total_matched", "truncated", "cursor"):
        assert via_corpus.get(k) == via_path.get(k), k


def test_a_page_of_nodes_is_identical_apart_from_the_corpus_label(world):
    eng, f = world
    c = dispatch("lts_find", {"corpus": "one", "prefix": "PROC-", "semantic": "off"}, eng)
    p = dispatch("lts_find", {"path": str(f), "prefix": "PROC-", "semantic": "off"}, eng)
    strip = lambda ns: [{k: v for k, v in n.items() if k != "corpus"} for n in ns]
    assert strip(c["nodes"]) == strip(p["nodes"]) and strip(c["nodes"])


def test_twenty_repeated_calls_are_identical(world):
    eng, f = world
    first = json.dumps(dispatch("lts_find", {"path": str(f), "prefix": "PROC-", "semantic": "off"}, eng),
                       sort_keys=True)
    for _ in range(20):
        assert json.dumps(dispatch("lts_find", {"path": str(f), "prefix": "PROC-", "semantic": "off"}, eng),
                          sort_keys=True) == first


# ------------------------------------------------------------------------------- refusals
@pytest.mark.parametrize("extra,needle", [
    ({"semantic": "on_miss"}, "semantic"),
    ({"rank": "relevance", "text": "x"}, "relevance"),
    ({"corpus": "one"}, "exclusive"),
    ({"include_reverse_digest": True}, "include_reverse_digest"),
    ({"source_file": "a.lts"}, "source_file"),
    ({"word_fallback": True}, "word_fallback"),
])
def test_refusals(world, extra, needle):
    eng, f = world
    r = dispatch("lts_find", {"path": str(f), **extra}, eng)
    assert r["success"] is False and needle in r["error"], r
    assert str(f) not in json.dumps(r)


def test_the_semantic_env_default_never_reaches_a_path_find(world, monkeypatch):
    eng, f = world
    monkeypatch.setenv("LTS_SEMANTIC_ON_MISS", "1")
    r = dispatch("lts_find", {"path": str(f), "text": "zzz-no-such-thing"}, eng)
    assert r["success"] is True and r["total_matched"] == 0
    assert "did_you_mean_semantic" not in r


def test_the_word_fallback_env_never_reaches_a_path_find(world, monkeypatch):
    eng, f = world
    monkeypatch.setenv("LTS_FIND_WORD_FALLBACK", "1")
    r = dispatch("lts_find", {"path": str(f), "text": "borrower contents"}, eng)
    assert r["success"] is True and r["total_matched"] == 0 and "matched_all_words" not in r


@pytest.mark.parametrize("make", ["missing", "directory", "not_lts", "empty_string", "number", "unparseable"])
def test_a_bad_path_is_a_named_refusal_that_never_echoes_the_path(world, tmp_path, make):
    eng, f = world
    d = tmp_path / CANARY
    d.mkdir()
    if make == "missing":
        p = str(d / "nope.lts")
    elif make == "directory":
        p = str(d)
    elif make == "not_lts":
        (d / "x.txt").write_text("# x [SPEC:X]\n")
        p = str(d / "x.txt")
    elif make == "empty_string":
        p = ""
    elif make == "number":
        p = 12345
    else:
        (d / "bad.lts").write_bytes(b"\xff\xfe\x00 not utf8 \xc3\x28")
        p = str(d / "bad.lts")
    r = dispatch("lts_find", {"path": p, "semantic": "off"}, eng)
    assert r["success"] is False and r.get("error"), r
    assert CANARY not in json.dumps(r) and str(tmp_path) not in json.dumps(r)


# ------------------------------------------------------------------------------- privacy
def test_a_file_under_a_private_corpus_root_is_answered_and_names_no_private_corpus(tmp_path):
    root = tmp_path / CANARY
    root.mkdir()
    f = root / "p.lts"
    f.write_text("# Doc [SPEC:PV-ROOT]\n\n## One [PV-ONE]\n\n- status: operational\n- kind: self\n", encoding="utf-8")
    pub = tmp_path / "pub"
    pub.mkdir()
    (pub / "a.lts").write_text(F.A, encoding="utf-8")
    eng = LtsEngine(config.build_store({
        "pub": {"roots": [{"path": str(pub), "area": None}], "domain_suffix": "p.pub"},
        f"{CANARY}-personality": {"roots": [{"path": str(root), "area": None}], "domain_suffix": "p.priv"}}))
    r = dispatch("lts_find", {"path": str(f), "prefix": "PV-", "semantic": "off"}, eng)
    assert r["success"] is True and [n["id"] for n in r["nodes"]] == ["PV-ONE", "PV-ROOT"]
    blob = json.dumps(r)
    assert CANARY not in blob and str(tmp_path) not in blob
    bad = dispatch("lts_find", {"path": str(root / "nope.lts"), "semantic": "off"}, eng)
    assert CANARY not in json.dumps(bad)


# ------------------------------------------------------------------------------- read-only, no sharing
def test_the_file_and_its_directory_are_untouched(world):
    eng, f = world
    before = (f.read_bytes(), os.stat(f).st_mtime_ns, os.stat(f).st_ino, sorted(os.listdir(f.parent)))
    for shape in SHAPES[:6]:
        dispatch("lts_find", {"path": str(f), "semantic": "off", **shape}, eng)
    after = (f.read_bytes(), os.stat(f).st_mtime_ns, os.stat(f).st_ino, sorted(os.listdir(f.parent)))
    assert before == after


def test_a_path_find_writes_no_file_anywhere_but_the_query_log(world, tmp_path, monkeypatch):
    eng, f = world
    opened = []
    real_open = os.open

    def spy(p, flags, *a, **k):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            opened.append(str(p))
        return real_open(p, flags, *a, **k)
    monkeypatch.setattr(os, "open", spy)
    import builtins
    real_b = builtins.open

    def bspy(p, mode="r", *a, **k):
        if any(c in mode for c in "wax+"):
            opened.append(str(p))
        return real_b(p, mode, *a, **k)
    monkeypatch.setattr(builtins, "open", bspy)
    dispatch("lts_find", {"path": str(f), "prefix": "PROC-", "semantic": "off"}, eng)
    assert [p for p in opened if "querylog" not in p and "query-log" not in p and ".jsonl" not in p] == []


def test_the_field_caches_are_never_shared_with_a_corpus_partition(world):
    eng, f = world
    warm = dispatch("lts_find", {"corpus": "one", "all_of": ["live"], "match": "word", "semantic": "off"}, eng)
    stats0 = dict(eng._qs_caches.stats())
    for _ in range(3):
        dispatch("lts_find", {"path": str(f), "all_of": ["live"], "match": "word", "semantic": "off"}, eng)
    assert dict(eng._qs_caches.stats()) == stats0                     # nothing added, nothing evicted
    again = dispatch("lts_find", {"corpus": "one", "all_of": ["live"], "match": "word", "semantic": "off"}, eng)
    assert json.dumps(again, sort_keys=True) == json.dumps(warm, sort_keys=True)


def test_a_file_changed_between_calls_is_re_read_not_served_from_a_cache(world):
    eng, f = world
    r1 = dispatch("lts_find", {"path": str(f), "all_of": ["zebra-new"], "semantic": "off"}, eng)
    f.write_text(F.A + "\n## Zebra-new thing [ZZ-NEW]\n\n- status: operational\n", encoding="utf-8")
    r2 = dispatch("lts_find", {"path": str(f), "all_of": ["zebra-new"], "semantic": "off"}, eng)
    assert r1["total_matched"] == 0 and [n["id"] for n in r2["nodes"]] == ["ZZ-NEW"]


def test_concurrent_path_finds_each_see_their_own_file(tmp_path):
    eng = LtsEngine(config.build_store({}))
    files = []
    for i in range(6):
        d = tmp_path / f"d{i}"
        d.mkdir()
        p = d / "x.lts"
        p.write_text(f"# Doc [SPEC:T{i}-ROOT]\n\n## Own [T{i}-ONE]\n\n- status: operational\n- marker: unique{i}\n",
                     encoding="utf-8")
        files.append(p)
    out, errs = {}, []

    def run(i):
        try:
            for _ in range(15):
                r = dispatch("lts_find", {"path": str(files[i]), "all_of": [f"unique{i}"], "match": "word",
                                          "semantic": "off"}, eng)
                got = [n["id"] for n in r["nodes"]]
                if got != [f"T{i}-ONE"]:
                    errs.append((i, got))
        except Exception as e:  # noqa: BLE001
            errs.append((i, repr(e)))
    ts = [threading.Thread(target=run, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs


# ------------------------------------------------------------------------------- the log
def test_the_path_is_logged_as_a_digest_never_literally_even_with_literals_on(world, monkeypatch):
    eng, f = world
    monkeypatch.setenv(querylog.ENV_LITERALS, "1")
    res = dispatch("lts_find", {"path": str(f), "prefix": "PROC-", "semantic": "off"}, eng)
    rec = querylog.build_record("lts_find", {"path": str(f), "prefix": "PROC-"}, res, 1.0)
    blob = json.dumps(rec)
    assert str(f) not in blob and f.name not in blob and str(f.parent) not in blob
    want = hashlib.sha256(str(os.path.realpath(f)).encode()).hexdigest()[:12]
    assert rec["filters"]["path"] == {"sha": want, "len": len(str(os.path.realpath(f)))} or \
        rec["filters"]["path"]["sha"] == hashlib.sha256(str(f).encode()).hexdigest()[:12]


def test_a_writer_still_logs_its_path_as_a_basename():
    rec = querylog.build_record("lts_add_node", {"path": "/a/b/c.lts", "node_id": "X"}, {"success": True}, 1.0)
    assert rec["filters"]["path"] == "c.lts"


def test_the_query_log_records_a_path_find(world, tmp_path, monkeypatch):
    eng, f = world
    logdir = tmp_path / "qlog"
    monkeypatch.setenv(querylog.ENV_DIR, str(logdir))
    monkeypatch.delenv(querylog.ENV_LOG, raising=False)
    dispatch("lts_find", {"path": str(f), "prefix": "PROC-", "semantic": "off"}, eng)
    from factlattice_engine.server import Server
    srv = Server(eng)
    srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "lts_find", "arguments": {"path": str(f), "prefix": "PROC-", "semantic": "off"}}})
    recs = [json.loads(l) for p in logdir.rglob("*.jsonl") for l in p.read_text().splitlines()]
    mine = [r for r in recs if r["tool"] == "lts_find" and "path" in r["filters"]]
    assert mine and str(f) not in json.dumps(mine)


# ------------------------------------------------------------------------------- surface
def test_schema_advertises_path_on_lts_find_only():
    spec = next(t for t in server.TOOLS if t["name"] == "lts_find")
    assert "path" in spec["inputSchema"]["properties"]
    assert "path" not in next(t for t in server.TOOLS if t["name"] == "lts_aggregate")["inputSchema"]["properties"]


def test_lts_find_stays_on_the_read_allow_list():
    assert "lts_find" in tools.LTS_READ_ONLY_TOOL_NAMES


def test_the_call_door_serves_it_without_allow_write(world, capsys):
    from tests.test_cli_call import call_json
    eng, f = world
    rc, env, _ = call_json("lts_find", {"path": str(f), "prefix": "PROC-", "semantic": "off"}, capsys)
    assert rc == 0, env
    res = env["result"] if "result" in env else env
    assert res["success"] is True and res["total_matched"] > 0


def test_the_response_declares_its_source_by_hash_and_names_no_path(world):
    eng, f = world
    r = dispatch("lts_find", {"path": str(f), "prefix": "PROC-", "semantic": "off"}, eng)
    assert r["file_source"]["sha256"] == hashlib.sha256(f.read_bytes()).hexdigest()
    assert r["file_source"]["bytes"] == len(f.read_bytes())
    assert str(f) not in json.dumps(r)


def test_file_source_is_absent_on_a_corpus_find(world):
    eng, _ = world
    assert "file_source" not in dispatch("lts_find", {"corpus": "one", "prefix": "PROC-", "semantic": "off"}, eng)


# ===================================================== post-review P2-1: hash and parse the SAME bytes
def test_a_file_appended_between_the_read_and_the_parse_is_refused_not_mis_certified(world, monkeypatch):
    from factlattice_engine import pathsource
    eng, f = world

    def append_between(real):
        with open(real, "a", encoding="utf-8") as fh:
            fh.write("\n## Swapped in [ST-SWAP]\n\n- status: operational\n")
    monkeypatch.setattr(pathsource, "_AFTER_READ_HOOK", append_between, raising=False)
    r = dispatch("lts_find", {"path": str(f), "semantic": "off"}, eng)
    assert r["success"] is False and "changed while" in r["error"], r
    assert str(f) not in json.dumps(r)


def test_the_certified_bytes_are_the_parsed_bytes_when_nothing_races(world):
    eng, f = world
    r = dispatch("lts_find", {"path": str(f), "semantic": "off"}, eng)
    assert r["file_source"] == {"sha256": hashlib.sha256(f.read_bytes()).hexdigest(), "bytes": len(f.read_bytes())}
