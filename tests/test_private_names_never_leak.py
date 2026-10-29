"""[INV-PRIVATE-FANOUT] NAMES — no error, suggestion or extent field may NAME a private corpus.

THE DEFECT (acme-team ISSUE-PRIVATE-NAMES-IN-ERRORS, 2026-10-03): the
fan-out rule kept private corpus CONTENT out of corpus-omitted reads, but `_not_found`
answered a mistyped corpus id with `corpora available: [...]`, `did_you_mean` suggestions
and a loaded-corpora list that included `*-personality` ids; `extent.corpora_available`
named every corpus on EVERY successful call; `extent.corpora_skipped` named each skipped
private corpus on every fan-out; `unregister_corpus` echoed `available`. An agent learned
other agents' private corpus names from a typo.

⛔ THE TEST ENUMERATES THE TOOL REGISTRY (`server.TOOLS`), so a tool added tomorrow is
covered with no edit here: every tool whose schema takes a `corpus`/`corpus_id` is called
with an UNKNOWN id deliberately spelled one character from a private corpus name (the worst
case for did-you-mean), and the whole response is deep-scanned, every string AND every key,
for any private corpus id / domain suffix / root directory name.

⛔ Negative control: `test_scan_fires_on_a_planted_name` proves the scanner is not a
check that can only report success.
"""
from __future__ import annotations

import json

import pytest

from factlattice_engine import config
from factlattice_engine.server import TOOLS
from factlattice_engine.store import FreshnessResult
from factlattice_engine.tools import LtsEngine, dispatch

PRIV_A = "quokka-personality"        # private by id suffix
PRIV_B = "vaultzed"                  # private by registry flag
PUBLIC = "pubcorp"
PRIVATE_TOKENS = [PRIV_A, PRIV_B, "proj.quokkaself", "proj.hushbox", "dir-quokka", "dir-vaultzed"]


class _AlwaysFresh:
    def evaluate(self, part):
        return FreshnessResult(corpus_id=part.corpus_id, fresh=True, index_hash="h",
                               live_hash="h")


def _w(root, name, body):
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(body, encoding="utf-8")


@pytest.fixture()
def eng(tmp_path):
    _w(tmp_path / "dir-pub", "00.lts", "# P [P-DOC]\n\n## Pub note [PUB-1]\n\n- status: active\n\nhello\n")
    _w(tmp_path / "dir-quokka", "00.lts", "# Q [Q-DOC]\n\n## Self [Q-1]\n\n- status: active\n\nhello\n")
    _w(tmp_path / "dir-vaultzed", "00.lts", "# V [V-DOC]\n\n## Vault [V-1]\n\n- status: active\n\nhello\n")
    reg = {
        PUBLIC: {"roots": [{"path": str(tmp_path / "dir-pub"), "area": "specs"}],
                 "domain_suffix": "proj.pub"},
        PRIV_A: {"roots": [{"path": str(tmp_path / "dir-quokka"), "area": "specs"}],
                 "domain_suffix": "proj.quokkaself"},
        PRIV_B: {"roots": [{"path": str(tmp_path / "dir-vaultzed"), "area": "specs"}],
                 "domain_suffix": "proj.hushbox", "private": True},
    }
    return LtsEngine(config.build_store(reg), freshness_provider=_AlwaysFresh())


def _strings(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(o, (list, tuple, set)):
        for v in o:
            yield from _strings(v)
    elif o is not None:
        yield str(o)


def _leaks(result, tokens=PRIVATE_TOKENS):
    return sorted({t for s in _strings(result) for t in tokens if t in s})


def _fill(spec):
    t = spec.get("type")
    if "enum" in spec:
        return spec["enum"][0]
    return {"string": "x", "integer": 1, "boolean": False, "array": [], "object": {}}.get(t, "x")


def _args(tool, corpus_key, bad):
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    args = {k: _fill(props[k]) for k in schema.get("required", []) if k in props}
    for k in _corpus_keys(tool):
        args[k] = bad
    return args


def _corpus_keys(tool):
    """EVERY top-level param whose name contains "corpus" (corpus, corpus_id, corpora, ...)."""
    props = (tool.get("inputSchema") or {}).get("properties") or {}
    return [k for k in props if "corpus" in k.lower() or "corpora" in k.lower()]


def _corpus_key(tool):
    ks = _corpus_keys(tool)
    return ks[0] if ks else None


def _schema_corpus_names(node, path=""):
    """Independent walk of the WHOLE schema (nested too) for any property named like a corpus."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "properties" and isinstance(v, dict):
                for pk, pv in v.items():
                    if "corpus" in pk.lower() or "corpora" in pk.lower():
                        yield f"{path}/{pk}"
                    yield from _schema_corpus_names(pv, f"{path}/{pk}")
            else:
                yield from _schema_corpus_names(v, path)
    elif isinstance(node, list):
        for v in node:
            yield from _schema_corpus_names(v, path)


GATED = [t for t in TOOLS if _corpus_key(t)]
# one character off each private id: the worst case for a did-you-mean
TYPOS = [PRIV_A[:-1], PRIV_B[:-1], "zzz-nothing"]


def test_scan_fires_on_a_planted_name():
    assert _leaks({"a": ["fine", {"b": f"loaded: {PRIV_A}"}]}) == [PRIV_A]
    assert _leaks({PRIV_B: 1}) == [PRIV_B]          # keys are scanned too
    assert _leaks({"a": "clean"}) == []


def test_no_gated_tool_has_a_corpus_like_param_the_scanner_skipped():
    """A tool whose schema has a corpus-like param ANYWHERE (nested too) that the harness
    does not set would be invisible to the leak test. That must fail, not pass."""
    for t in TOOLS:
        found = set(_schema_corpus_names(t.get("inputSchema") or {}))
        covered = {f"/{k}" for k in _corpus_keys(t)}
        assert found == covered, (t["name"], sorted(found - covered))


def test_registry_enumeration_is_not_vacuous():
    names = {t["name"] for t in GATED}
    assert {"lts_find", "lts_node", "lts_traverse", "lts_reverse"} <= names, names
    assert len(GATED) >= 8


@pytest.mark.parametrize("bad", TYPOS)
@pytest.mark.parametrize("tool", GATED, ids=lambda t: t["name"])
def test_unknown_corpus_never_names_a_private_corpus(eng, tool, bad):
    if tool["name"] == "lts_register_corpus":
        pytest.skip("register takes a NEW id and echoes only what the caller sent")
    res = dispatch(tool["name"], _args(tool, _corpus_key(tool), bad), tools=eng)
    assert isinstance(res, dict)
    assert _leaks(res) == [] , (tool["name"], bad, json.dumps(res)[:600])


@pytest.mark.parametrize("tool", GATED, ids=lambda t: t["name"])
def test_corpus_omitted_calls_never_name_a_private_corpus(eng, tool):
    props = (tool.get("inputSchema") or {}).get("properties") or {}
    if _corpus_key(tool) not in (tool.get("inputSchema") or {}).get("required", []):
        args = _args(tool, _corpus_key(tool), "x")
        args.pop(_corpus_key(tool))
        if tool["name"] in ("lts_list_corpora", "lts_register_corpus", "lts_unregister_corpus"):
            pytest.skip("explicit enumeration / registry surface")
        res = dispatch(tool["name"], args, tools=eng)
        assert _leaks(res) == [], (tool["name"], json.dumps(res)[:600])


def test_successful_public_call_extent_names_no_private_corpus(eng):
    res = eng.find(corpus=PUBLIC, text="Pub note")
    assert res["success"] and res["total_matched"] >= 1
    assert _leaks(res) == [], res["extent"]
    res = eng.find(text="Pub note")                   # fan-out
    assert res["success"]
    assert _leaks(res) == [], res["extent"]
    # the skip is still DECLARED, as an anonymous count, so a zero is not mistaken for "searched all"
    skipped = [s for s in res["extent"]["corpora_skipped"] if s.get("reason", "").startswith("private")]
    assert skipped and skipped[0]["count"] == 2


def test_unknown_corpus_still_lists_public_and_says_private_need_exact_name(eng):
    res = eng.find(corpus="pubcor", text="Pub note")
    assert not res["success"]
    assert PUBLIC in res["corpora_available"] and PUBLIC in res["error"]
    assert "private corpora are searched only when named exactly" in res["error"]
    assert _leaks(res) == []


@pytest.mark.parametrize("name", [PRIV_A, PRIV_B])
def test_an_exactly_named_private_corpus_still_works(eng, name):
    res = eng.find(corpus=name, text={PRIV_A: "Self", PRIV_B: "Vault"}[name])
    assert res["success"] and res["total_matched"] >= 1
    assert res["extent"]["corpora_consulted"] == [name]
    # naming it is the caller's own act; the node tool works too
    nid = {PRIV_A: "Q-1", PRIV_B: "V-1"}[name]
    assert eng.node(corpus=name, node_id=nid)["success"]
    # and the exactly-named call still does not enumerate the OTHER private corpus
    other = PRIV_B if name == PRIV_A else PRIV_A
    assert other not in json.dumps(res)


def test_list_corpora_is_the_explicit_enumeration_surface(eng):
    res = eng.list_corpora(heal=False)
    assert {c["corpus_id"] for c in res["corpora"]} == {PUBLIC, PRIV_A, PRIV_B}


# --- F1: routed_withheld stubs are anonymous ---------------------------------------------
EDGE_TOKENS = [PRIV_A, PRIV_B, "dir-quokka", "dir-vaultzed"]   # ids + dirs (the DOMAIN is authored in the edge itself)


def _edge_registry(tmp_path):
    _w(tmp_path / "dir-pub", "00.lts",
       "# P [P-DOC]\n\n## Pub note [PUB-1]\n\n- status: active\n"
       "- depends-on: [proj.quokkaself:SPEC:Q-1]\n- relates-to: [proj.hushbox:SPEC:V-1]\n\nhello\n")
    _w(tmp_path / "dir-quokka", "00.lts",
       "# Q [Q-DOC]\n\n## Self [Q-1]\n\n- status: active\n- depends-on: [proj.pub:SPEC:PUB-1]\n\nhello\n")
    _w(tmp_path / "dir-vaultzed", "00.lts",
       "# V [V-DOC]\n\n## Vault [V-1]\n\n- status: active\n- depends-on: [proj.pub:SPEC:PUB-1]\n\nhello\n")
    return {
        PUBLIC: {"roots": [{"path": str(tmp_path / "dir-pub"), "area": "specs"}], "domain_suffix": "proj.pub"},
        PRIV_A: {"roots": [{"path": str(tmp_path / "dir-quokka"), "area": "specs"}], "domain_suffix": "proj.quokkaself"},
        PRIV_B: {"roots": [{"path": str(tmp_path / "dir-vaultzed"), "area": "specs"}],
                 "domain_suffix": "proj.hushbox", "private": True},
    }


@pytest.fixture()
def eng_edges(tmp_path):
    return LtsEngine(config.build_store(_edge_registry(tmp_path)), freshness_provider=_AlwaysFresh())


STUB = {"withheld": True, "reason": "edge into a private corpus; name it explicitly to follow"}


def _all_stubs(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "routed_withheld":
                yield from v
            else:
                yield from _all_stubs(v)
    elif isinstance(o, list):
        for v in o:
            yield from _all_stubs(v)


def _nodeish_args(tool):
    a = _args(tool, "corpus", PUBLIC)
    for k in ("node_id", "start_node"):
        if k in a:
            a[k] = "PUB-1"
    if tool["name"] == "lts_aggregate":
        a["op"] = "count"
    return a


@pytest.mark.parametrize("tool", GATED, ids=lambda t: t["name"])
def test_public_to_private_edge_names_no_private_corpus_in_any_tool(eng_edges, tool):
    if tool["name"] in ("lts_register_corpus", "lts_unregister_corpus"):
        pytest.skip("registry mutation surface")
    res = dispatch(tool["name"], _nodeish_args(tool), tools=eng_edges)
    assert _leaks(res, EDGE_TOKENS) == [], (tool["name"], json.dumps(res)[:800])


@pytest.mark.parametrize("call", ["node", "traverse", "node_digest", "traverse_digest", "capabilities"])
def test_withheld_stubs_are_anonymous_and_still_declared(eng_edges, call):
    e = eng_edges
    res = {
        "node": lambda: e.node(corpus=PUBLIC, node_id="PUB-1"),
        "traverse": lambda: e.traverse(corpus=PUBLIC, start_node="PUB-1"),
        "node_digest": lambda: e.node(corpus=PUBLIC, node_id="PUB-1", tier="digest"),
        "traverse_digest": lambda: e.traverse(corpus=PUBLIC, start_node="PUB-1", tier="digest"),
        "capabilities": lambda: e.capabilities(corpus=PUBLIC, node_id="PUB-1"),
    }[call]()
    assert res["success"]
    stubs = list(_all_stubs(res))
    assert stubs, ("the hop must still be DECLARED", list(res))
    assert all(st == STUB for st in stubs), stubs
    assert _leaks(res, EDGE_TOKENS) == []


# --- F2 / F3: a STALE private corpus is counted, never named --------------------------------
class _StalePrivate:
    def evaluate(self, part):
        fresh = part.corpus_id != PRIV_A
        return FreshnessResult(corpus_id=part.corpus_id, fresh=fresh, index_hash="h",
                               live_hash="h" if fresh else "DIFFERENT",
                               reason=None if fresh else "stale-for-test")


@pytest.fixture()
def eng_stale(tmp_path):
    return LtsEngine(config.build_store(_edge_registry(tmp_path)), freshness_provider=_StalePrivate())


def test_reverse_with_a_stale_private_corpus_is_self_consistent_and_anonymous(eng_stale):
    res = eng_stale.reverse(corpus=PUBLIC, node_id="PUB-1")
    assert res["success"]
    assert _leaks(res, EDGE_TOKENS) == [], json.dumps(res)[:800]          # kills `_name_safe_stale`
    assert res["complete"] is False
    assert res["stale_corpora"] == []
    assert res["extent"]["index"]["fresh"] is False
    assert {"reason": "stale", "private_stale_count": 1} in res["extent"]["corpora_skipped"]


def test_reverse_digest_with_a_stale_private_corpus_is_anonymous(eng_stale):
    d = eng_stale._reverse_digest(PUBLIC, "PUB-1")
    assert _leaks(d, EDGE_TOKENS) == []
    assert d["complete"] is False and d["private_stale_count"] == 1


def test_stale_name_safe_mutation_would_be_caught(eng_stale):
    """Negative control: if the filter is bypassed the scan DOES see the name."""
    eng_stale._name_safe_stale = lambda stale: list(stale)
    res = eng_stale.reverse(corpus=PUBLIC, node_id="PUB-1")
    assert _leaks(res, EDGE_TOKENS) == [PRIV_A]


# --- F7 ---------------------------------------------------------------------------------
def test_unknown_corpus_refuses_a_known_list_containing_a_private_id():
    from factlattice_engine.extent import unknown_corpus
    with pytest.raises(AssertionError):
        unknown_corpus("x", [PUBLIC, PRIV_A], private=[PRIV_A])
    assert unknown_corpus("x", [PUBLIC], private=[PRIV_A])["success"] is False
