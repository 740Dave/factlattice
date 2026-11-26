"""Spec 39 fast-follow x-minted-keys, SCEN-NS-MINTED-KEYS-NAMED: every node object in a reply that a served tool
returns (an object with a string `id` and a `properties` mapping) carries `minted_keys` beside `properties`, and
the engine's one definition of the minted set is the set SPEC.md section 1.3 states.

The reply paths are not listed by hand and trusted. The test CALLS every tool in `server.TOOLS` through `dispatch`
(the door every transport uses), each with arguments that make it answer in the fixture, walks every reply for any
object that has a `properties` key, and fails on a node object without the right `minted_keys` and on a
`properties` carrier that is not a node object and not named in `NOT_NODE_CARRIERS`. The only tools it does not
call are in `NOT_CALLED`, each with its reason, and a tool in `server.TOOLS` that is in neither group fails
the completeness test. A plant that makes any one tool return a node object that is not named fails the test
for that tool (`test_a_plant_in_any_single_tool_is_caught`).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from factlattice import core_keys
from factlattice_engine import FreshnessResult, config, server, tools
from factlattice_engine.tools import LtsEngine, dispatch

ROOT = Path(__file__).resolve().parent.parent

DOC_C = """---
lts: "1.1"
title: T
domain: d
---
# T [SPEC:T-ROOT]

## Plain [N-A]

- status: active
- depends-on: [N-B]
- depends-on: [proj.b:SPEC:X-1]

## Bare [N-B]

- **Bold Key**: lost on purpose, a line for the repair tool
"""

DOC_B = """---
lts: "1.1"
title: B
domain: d
---
# B [SPEC:B-ROOT]

## Far node [X-1]

- status: active
"""

# `properties` carriers that are not node objects (no string id): a vocabulary report. Named so that a new one fails.
NOT_NODE_CARRIERS = {("lts_profile_audit", "")}

# Tools the test does NOT call, each with the reason it cannot answer in the fixture.
NOT_CALLED = {
    "lts_register_corpus": "writes the registry file it was started from; its reply carries no node object",
    "lts_unregister_corpus": "writes the registry file it was started from; its reply carries no node object",
}


class _AlwaysFresh:
    """The typed writers change the file after the index was built; the readers after them must still answer."""
    def evaluate(self, part):
        return FreshnessResult(part.corpus_id, True, part.files_hash, part.files_hash)


@pytest.fixture()
def eng(tmp_path):
    rc, rb = tmp_path / "corp_c", tmp_path / "corp_b"
    rc.mkdir()
    rb.mkdir()
    (rc / "00-d.lts").write_text(DOC_C, encoding="utf-8")
    (rb / "00-b.lts").write_text(DOC_B, encoding="utf-8")
    store = config.build_store({"c": {"roots": [{"path": str(rc)}], "domain_suffix": "proj.c"},
                                "b": {"roots": [{"path": str(rb)}], "domain_suffix": "proj.b"}})
    return LtsEngine(store, freshness_provider=_AlwaysFresh()), rc


def _calls(root):
    """(tool, arguments) for every tool the test calls. Readers first, then the typed writers (they change the
    file), then the tools that read what the writers left (history)."""
    f = str(root / "00-d.lts")
    bold_line = DOC_C.splitlines().index("- **Bold Key**: lost on purpose, a line for the repair tool") + 1
    return [
        ("lts_node", {"corpus": "c", "node_id": "N-A"}),
        ("lts_node", {"corpus": "c", "node_id": "N-B", "tier": "page"}),
        ("lts_node", {"corpus": "c", "node_id": "N-A", "tier": "digest"}),
        ("lts_find", {"corpus": "c", "prefix": "N-"}),
        ("lts_find", {"corpus": "c", "prefix": "N-", "tier": "page"}),
        ("lts_find", {"corpus": "c", "prefix": "N-", "tier": "full"}),
        ("lts_find", {"corpus": "c", "prefix": "N-", "tier": "digest"}),
        ("lts_find", {"corpus": "c", "text": "active", "rank": "relevance"}),
        ("lts_find", {"corpus": "c", "prefix": "N-", "include_reverse_digest": True}),
        ("lts_find_in_prose", {"corpus": "c", "text": "active"}),
        ("lts_traverse", {"corpus": "c", "start_node": "N-A", "direction": "both"}),
        ("lts_traverse", {"corpus": "c", "start_node": "N-A", "direction": "both", "tier": "page"}),
        ("lts_traverse", {"corpus": "c", "start_node": "N-A", "direction": "both", "tier": "digest"}),
        ("lts_flow", {"corpus": "c", "start_node": "N-A"}),
        ("lts_reverse", {"corpus": "c", "node_id": "N-B"}),
        ("lts_capabilities", {"corpus": "c", "node_id": "N-A"}),
        ("lts_aggregate", {"corpus": "c", "op": "count"}),
        ("lts_aggregate", {"corpus": "c", "op": "group_by", "attr": "status"}),
        ("lts_profile_audit", {"corpus": "c"}),
        ("lts_list_corpora", {}),
        ("lts_semantic_find", {"corpus": "c", "text": "active"}),
        ("lts_preview", {"content": DOC_C}),
        ("lts_preview", {"content": DOC_C, "corpus": "c", "replaces": "00-d.lts"}),
        ("lts_lint", {"source": str(root)}),
        ("lts_selfcheck", {}),
        ("lts_report_miss", {"corpus": "c", "query": "a question", "expected_ids": ["N-A"], "why": "test"}),
        # the typed writers: each lands on the file in the fixture, in this order
        ("lts_add_property", {"path": f, "node_id": "N-A", "key": "note", "value": "one"}),
        ("lts_rename_property", {"path": f, "node_id": "N-A", "key": "note", "new_key": "remark"}),
        ("lts_remove_property", {"path": f, "node_id": "N-A", "key": "remark"}),
        ("lts_add_node", {"path": f, "node_id": "N-C", "title": "Added", "properties": {"status": "new"}}),
        ("lts_repair_property", {"path": f, "node_id": "N-B", "line": bold_line, "key": "bold-key"}),
        ("lts_property_loss", {"roots": [str(root)]}),
        ("lts_history", {"corpus": "c", "node_id": "N-A"}),
    ]


def _walk(obj, path, out):
    """Every dict in `obj` that has a `properties` key: (path, the dict)."""
    if isinstance(obj, dict):
        if "properties" in obj:
            out.append((path, obj))
        for k, v in obj.items():
            if k != "properties":
                _walk(v, f"{path}.{k}", out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk(v, f"{path}[]", out)


def _property_carriers(eng_root):
    e, root = eng_root
    found, replies = [], {}
    for tool, args in _calls(root):
        reply = dispatch(tool, args, tools=e)
        assert isinstance(reply, dict), tool
        assert reply.get("success") is not False, (tool, args, reply)   # a call that is refused answers nothing
        replies.setdefault(tool, []).append(reply)
        out = []
        _walk(reply, "", out)
        found.extend((tool, p, d) for p, d in out)
    return found, replies


def _is_node_object(d):
    return isinstance(d.get("id"), str) and isinstance(d.get("properties"), dict)


def _failures(carriers):
    bad = []
    for tool, path, d in carriers:
        if not _is_node_object(d):
            if (tool, path) not in NOT_NODE_CARRIERS:
                bad.append(f"{tool}{path}: a properties carrier that is not a node object and is not named")
            continue
        want = sorted(k for k in d["properties"] if k in core_keys.ENGINE_MINTED_KEYS)
        if "minted_keys" not in d:
            bad.append(f"{tool}{path}: carries properties without minted_keys")
        elif d["minted_keys"] != want:
            bad.append(f"{tool}{path}: minted_keys {d['minted_keys']!r} != {want!r}")
    return bad


# the node-object paths the walk must find; a walk that finds fewer has stopped seeing the replies (positive control)
EXPECTED_PATHS = {("lts_node", ".node"), ("lts_node", ".routed_nodes[]"),
                  ("lts_find", ".nodes[]"), ("lts_traverse", ".nodes[]"), ("lts_traverse", ".routed_nodes[]"),
                  ("lts_flow", ".nodes[]"), ("lts_capabilities", ".capabilities[]"),
                  ("lts_capabilities", ".routed_capabilities[]"), ("lts_preview", ".became.nodes[]")}


def test_every_node_object_in_a_reply_names_the_minted_keys(eng):  # SCEN-NS-MINTED-KEYS-NAMED
    carriers, replies = _property_carriers(eng)
    seen = {(t, p) for t, p, _ in carriers}
    assert EXPECTED_PATHS <= seen, EXPECTED_PATHS - seen
    assert not _failures(carriers), _failures(carriers)
    assert not (NOT_NODE_CARRIERS - seen)                       # a named exemption that no longer occurs is stale


def test_every_served_tool_is_called_or_named_with_a_reason(eng):
    _, root = eng
    called = {t for t, _ in _calls(root)}
    served = {s["name"] for s in server.TOOLS}
    assert not (served - called - set(NOT_CALLED)), served - called - set(NOT_CALLED)
    assert not (set(NOT_CALLED) - served) and not (set(NOT_CALLED) & called)
    assert all(NOT_CALLED.values())                             # every exemption gives its reason


def test_the_called_tools_answered_non_empty_where_they_can(eng):
    """A reply with no node in it proves nothing about its path: the tools that return nodes returned some."""
    _, replies = _property_carriers(eng)
    assert replies["lts_history"][0].get("success") is True
    for tool in ("lts_node", "lts_find", "lts_traverse", "lts_flow", "lts_capabilities", "lts_preview"):
        assert any(_property_carriers_of(r) for r in replies[tool]), tool


def _property_carriers_of(reply):
    out = []
    _walk(reply, "", out)
    return out


def test_a_plant_in_any_single_tool_is_caught(eng, monkeypatch):  # negative control, one tool at a time
    """For each served tool the test calls: make that tool alone return a node object and skip the naming for
    it. The walk must fail for that tool and for no other."""
    e, root = eng
    real_name, real_dispatch = tools.name_minted_keys, tools._dispatch
    current = {"tool": None, "target": None}
    plant = {"success": True, "node": {"id": "PLANT-1", "properties": {"status": "x", "normative_effective": "true"}}}

    def fake_dispatch(name, arguments, t=None):
        current["tool"] = name
        return plant if name == current["target"] else real_dispatch(name, arguments, t)

    monkeypatch.setattr(tools, "_dispatch", fake_dispatch)
    monkeypatch.setattr(tools, "name_minted_keys",
                        lambda o: o if current["tool"] == current["target"] else real_name(o))
    for target in sorted({t for t, _ in _calls(root)}):
        current["target"] = target
        # a fresh fixture per tool: the writers change the file
        carriers = []
        for tool, args in _calls(root):
            if tool != target:
                continue
            reply = dispatch(tool, args, tools=e)
            out = []
            _walk(reply, "", out)
            carriers.extend((tool, p, d) for p, d in out)
            break
        bad = _failures(carriers)
        assert bad == [f"{target}.node: carries properties without minted_keys"], (target, bad)


def test_the_walk_fails_on_planted_replies():  # negative control on the checker itself
    planted = [("lts_planted", ".node", {"id": "X", "properties": {"status": "a", "normative_effective": "true"}})]
    assert _failures(planted) == ["lts_planted.node: carries properties without minted_keys"]
    assert _failures([("lts_planted", ".node", {**planted[0][2], "minted_keys": []})])
    assert _failures([("lts_planted", ".x", {"properties": {"a": "b"}})])        # an unnamed non-node carrier


def test_the_walk_fails_when_the_tool_layer_stops_naming_the_keys(eng, monkeypatch):  # negative control, whole layer
    monkeypatch.setattr(tools, "name_minted_keys", lambda obj: obj)
    carriers, _ = _property_carriers(eng)
    bad = _failures(carriers)
    for path in EXPECTED_PATHS:
        assert any(b.startswith(f"{path[0]}{path[1]}:") for b in bad), path


def test_a_plant_that_stops_naming_the_routed_and_capability_paths_goes_red(eng, monkeypatch):
    """The routed nodes and the capability lists are reached through a corpus hop and a capability anchor: the
    fixture has both. A layer that names every other node object but strips these three paths is caught."""
    real = tools.name_minted_keys
    skip = ("routed_nodes", "capabilities", "routed_capabilities")

    def partial(obj):
        out = real(obj)
        if isinstance(out, dict):
            out = {k: ([{a: b for a, b in n.items() if a != "minted_keys"} for n in v] if k in skip and isinstance(v, list) else v)
                   for k, v in out.items()}
        return out

    monkeypatch.setattr(tools, "name_minted_keys", partial)
    carriers, _ = _property_carriers(eng)
    bad = _failures(carriers)
    for path in (("lts_node", ".routed_nodes[]"), ("lts_traverse", ".routed_nodes[]"),
                 ("lts_capabilities", ".capabilities[]"), ("lts_capabilities", ".routed_capabilities[]")):
        assert any(b.startswith(f"{path[0]}{path[1]}:") for b in bad), (path, bad)
    assert not any(b.startswith(("lts_find.", "lts_flow.", "lts_preview.")) for b in bad)


def test_a_minted_keys_already_on_the_object_is_replaced_by_the_computed_value():
    got = tools.name_minted_keys({"nodes": [{"id": "X", "properties": {"normative_effective": "true", "a": "b"},
                                             "minted_keys": ["a", "forged"]}]})
    assert got["nodes"][0]["minted_keys"] == ["normative_effective"]
    assert list(got["nodes"][0]).index("minted_keys") == list(got["nodes"][0]).index("properties") + 1


def test_removing_the_named_keys_leaves_exactly_the_authored_keys(eng):
    e, _ = eng
    plain = dispatch("lts_node", {"corpus": "c", "node_id": "N-A"}, tools=e)["node"]
    bare = dispatch("lts_node", {"corpus": "c", "node_id": "N-B"}, tools=e)["node"]
    for node, authored in ((plain, {"status", "depends-on"}), (bare, set())):
        assert node["minted_keys"] == ["normative_effective"]
        assert set(node["minted_keys"]) <= set(node["properties"])         # the key stays inside properties
        left = {k for k in node["properties"] if k not in node["minted_keys"]}
        assert left == authored
    # the store's own node was not mutated by naming the keys
    assert "minted_keys" not in e.store.get_partition("c").nodes["N-A"]


def test_property_count_counts_every_key_including_minted(eng):
    e, _ = eng
    rows = dispatch("lts_find", {"corpus": "c", "prefix": "N-", "tier": "digest"}, tools=e)["digest"]
    full = {n["id"]: n for n in dispatch("lts_find", {"corpus": "c", "prefix": "N-"}, tools=e)["nodes"]}
    assert rows
    for r in rows:
        assert "minted_keys" not in r                                  # a digest row carries no properties
        assert r["property_count"] == len(full[r["id"]]["properties"])
    pv = dispatch("lts_preview", {"content": DOC_C}, tools=e)["became"]["nodes"]
    for n in pv:
        assert n["property_count"] == len(n["properties"])


def test_the_one_definition_is_the_set_spec_md_states():
    text = (ROOT / "SPEC.md").read_text("utf-8")
    m = re.search(r"The engine-minted keys are exactly: ((?:`[a-z_]+`(?:, )?)+)\.", text)
    assert m, "SPEC.md does not state the minted set in the form 'The engine-minted keys are exactly: `k`.'"
    stated = set(re.findall(r"`([a-z_]+)`", m.group(1)))
    assert stated == set(core_keys.ENGINE_MINTED_KEYS) == {"normative_effective"}
    assert "minted_keys" in text
    from factlattice import parser
    assert parser.EFFECTIVE_KEY in core_keys.ENGINE_MINTED_KEYS
    assert core_keys.ENGINE_MINTED_KEYS == {core_keys.EFFECTIVE_KEY}


def _literal_spellings(root):
    """(file, line) of every quoted spelling of a minted key in the engine packages, `core_keys.py` and tests excluded."""
    hits = []
    for pkg in ("factlattice", "factlattice_engine"):
        for f in sorted((root / pkg).rglob("*.py")):
            if f.name == "core_keys.py" or "selfcheck" in f.parts and f.suffix != ".py":
                continue
            for i, line in enumerate(f.read_text("utf-8").splitlines(), 1):
                if re.search(r"""["'](?:%s)["']""" % "|".join(map(re.escape, core_keys.ENGINE_MINTED_KEYS)), line):
                    hits.append((str(f.relative_to(root)), i))
    return hits


def test_no_module_spells_a_minted_key_but_core_keys():
    assert _literal_spellings(ROOT) == []


def test_the_literal_scan_fires_on_a_planted_spelling(tmp_path):
    (tmp_path / "factlattice").mkdir()
    (tmp_path / "factlattice_engine").mkdir()
    (tmp_path / "factlattice_engine" / "x.py").write_text('K = "normative_effective"\n', encoding="utf-8")
    (tmp_path / "factlattice" / "core_keys.py").write_text('K = "normative_effective"\n', encoding="utf-8")
    assert _literal_spellings(tmp_path) == [("factlattice_engine/x.py", 1)]
