"""Tests for the ANALYTICS `lts_profile_audit` op — corpus vocabulary vs. declared
profile. Mirrors the hermetic-partition pattern the analytics selftest uses: a
hand-built in-memory partition with a KNOWN vocabulary + a KNOWN declared profile, so
every section (used, declared, both diffs, the power-law tail split, and the
required-property conformance rate) is asserted against exact expected values.

Also proves the freshness gate: a STALE corpus returns the loud stale marker and NO
numbers (never a silently-wrong drift figure), mirroring the existing analytics stale
test.
"""

from __future__ import annotations

from factlattice_engine.analytics import (
    LtsAggregator, profile_audit, NullProfileAdapter, YamlProfileAdapter,
    resolve_profile_adapter,
)
from factlattice_engine.analytics.profile_audit import (
    _parse_scope,
    _property_edges_section,
    _property_edges_wiring,
)


# --- freshness providers (hermetic, rootless partitions need a forced verdict) ----
class _AlwaysFresh:
    def evaluate(self, part):
        from factlattice_engine.store import FreshnessResult
        return FreshnessResult(part.corpus_id, True, "hermetic", "hermetic")


class _AlwaysStale:
    def evaluate(self, part):
        from factlattice_engine.store import FreshnessResult
        return FreshnessResult(part.corpus_id, False, "old", "new", "hermetic-stale")


def _node(nid, prefix, props):
    return {"id": nid, "type_prefix": prefix, "title": nid, "domain": "D1",
            "status": None, "area": "decisions", "token_count": 5,
            "source_file": None, "line_range": [], "properties": dict(props)}


# KNOWN vocabulary. Property frequencies (post-injection-exclusion):
#   rationale 6, part-of 5, importance 3, invalidating_assumption 2, note 1
# `aliases` is a PARSER-INJECTED key and must NOT be counted.
_NODES = [
    _node("DEC-1", "DEC", {"rationale": "r", "part-of": "X", "importance": "high",
                           "invalidating_assumption": "a", "aliases": ["DEC-1b"]}),
    _node("DEC-2", "DEC", {"rationale": "r", "part-of": "X", "importance": "low"}),
    _node("DEC-3", "DEC", {"rationale": "r", "part-of": "X"}),
    _node("FIX-1", "FIX", {"rationale": "r", "part-of": "X", "note": "n"}),
    _node("LIB-1", "LIB", {"rationale": "r", "part-of": "X", "importance": "high",
                           "invalidating_assumption": "a"}),
    _node("LIB-2", "LIB", {"rationale": "r"}),
]

# KNOWN edges (resolved). Types: PART_OF, DEPENDS_ON, CONTAINS, RESOLVES (1 each).
_EDGES = [
    {"source": "DEC-1", "target": "DEC-2", "type": "PART_OF", "dangling": False, "flags": []},
    {"source": "DEC-2", "target": "DEC-3", "type": "DEPENDS_ON", "dangling": False, "flags": []},
    {"source": "LIB-1", "target": "DEC-1", "type": "CONTAINS", "dangling": False, "flags": []},
    {"source": "FIX-1", "target": "DEC-1", "type": "RESOLVES", "dangling": False, "flags": []},
]

# KNOWN declared profile (the swappable "declared" side), current YAML shape as a dict.
_PROFILE = {
    "domain": {"id": "test.audit", "namespaces": ["test.audit"]},
    "prefixes": [{"prefix": "LIB-"}, {"prefix": "APP-"}],   # LIB used, APP dead
    "properties": ["rationale", "part-of", "status"],       # status declared-but-unused
    "relationship_verbs": {"core": ["DEPENDS_ON", "RELATED_TO"], "domain": ["USES"]},
}


def _build_tools(freshness, token_budget=1):
    """Hermetic engine. token_budget=1 forces the cursor-drain so the audit is proven
    to see EVERY node under a truncating budget (completeness)."""
    from factlattice_engine.store import LtsStore, Partition
    from factlattice_engine.tools import LtsEngine
    index = {
        "source_corpus": {"corpus_id": "hermetic", "domain_suffix": "test.audit",
                          "files_hash": "hermetic", "root_paths": []},
        "nodes": {n["id"]: n for n in _NODES},
        "layers": {"semantic": {"edges": _EDGES}},
        "stats": {"total_nodes": len(_NODES), "total_edges": len(_EDGES)},
    }
    store = LtsStore()
    store.add_partition(Partition(index))
    return LtsEngine(store, default_token_budget=token_budget,
                     freshness_provider=freshness)


def _audit(**kw):
    agg = LtsAggregator(_build_tools(_AlwaysFresh()))
    return agg.profile_audit(corpus="hermetic", profile=_PROFILE, **kw)


# --------------------------------------------------------------------------- #
# prefixes
# --------------------------------------------------------------------------- #
def test_prefixes_used_counts_and_diffs():
    r = _audit()
    pfx = r["prefixes"]
    # used (normalized to the <segment>- form for diffing against declared strings)
    assert set(pfx["used"]) == {"LIB-", "DEC-", "FIX-"}
    # per-prefix count over the found set
    assert pfx["counts"] == {"LIB": 2, "DEC": 3, "FIX": 1}
    assert sorted(pfx["declared"]) == ["APP-", "LIB-"]
    assert pfx["used_but_unregistered"] == ["DEC-", "FIX-"]   # drift in
    assert pfx["declared_but_unused"] == ["APP-"]             # dead vocab


# --------------------------------------------------------------------------- #
# properties — used set, frequency, the power-law tail split, both diffs
# --------------------------------------------------------------------------- #
def test_properties_frequency_and_tail_split():
    r = _audit(tail_threshold=5)
    prop = r["properties"]
    # `aliases` (parser-injected) must be excluded from the used vocabulary
    assert "aliases" not in prop["used"]
    assert set(prop["used"]) == {"rationale", "part-of", "importance",
                                 "invalidating_assumption", "note"}
    assert prop["frequency"] == {"rationale": 6, "part-of": 5, "importance": 3,
                                 "invalidating_assumption": 2, "note": 1}
    dist = prop["distribution"]
    assert dist["tail_threshold"] == 5
    assert dist["core_count"] == 2 and dist["tail_count"] == 3
    assert dist["core"] == {"rationale": 6, "part-of": 5}
    assert dist["tail"] == ["importance", "invalidating_assumption", "note"]


def test_properties_diffs_against_declared():
    r = _audit()
    prop = r["properties"]
    assert sorted(prop["declared"]) == ["part-of", "rationale", "status"]
    assert prop["used_but_unregistered"] == ["importance", "invalidating_assumption", "note"]
    assert prop["declared_but_unused"] == ["status"]


def test_tail_threshold_is_configurable():
    r = _audit(tail_threshold=3)
    dist = r["properties"]["distribution"]
    # >=3: rationale(6), part-of(5), importance(3) => core 3; tail: inv_assump(2), note(1)
    assert dist["core_count"] == 3 and dist["tail_count"] == 2
    assert set(dist["core"]) == {"rationale", "part-of", "importance"}


# --------------------------------------------------------------------------- #
# edge types
# --------------------------------------------------------------------------- #
def test_edge_types_used_counts_and_diffs():
    r = _audit()
    et = r["edge_types"]
    assert et["counts"] == {"CONTAINS": 1, "DEPENDS_ON": 1, "PART_OF": 1, "RESOLVES": 1}
    assert set(et["used"]) == {"CONTAINS", "DEPENDS_ON", "PART_OF", "RESOLVES"}
    assert sorted(et["declared"]) == ["DEPENDS_ON", "RELATED_TO", "USES"]
    assert et["used_but_unregistered"] == ["CONTAINS", "PART_OF", "RESOLVES"]
    assert et["declared_but_unused"] == ["RELATED_TO", "USES"]
    assert et["resolved_edges"] == 4


# --------------------------------------------------------------------------- #
# required-property conformance
# --------------------------------------------------------------------------- #
def test_conformance_rate_scoped_by_prefix():
    r = _audit(required_property="invalidating_assumption", required_scope="prefix=DEC")
    conf = r["conformance"]
    assert conf["required_property"] == "invalidating_assumption"
    assert conf["scope"] == {"prefix": "DEC"}
    # DEC-1 has it; DEC-2, DEC-3 do not => 1 of 3
    assert conf["present"] == 1 and conf["total"] == 3
    assert abs(conf["rate"] - (1 / 3)) < 1e-9


def test_conformance_absent_when_no_required_property():
    assert "conformance" not in _audit()


# --------------------------------------------------------------------------- #
# no-profile mode — used side only, declared/diffs marked "no profile loaded"
# --------------------------------------------------------------------------- #
def test_no_profile_returns_used_side_only():
    agg = LtsAggregator(_build_tools(_AlwaysFresh()))
    r = agg.profile_audit(corpus="hermetic")   # no profile
    assert r["success"] and r["profile"]["available"] is False
    assert set(r["prefixes"]["used"]) == {"LIB-", "DEC-", "FIX-"}
    assert r["prefixes"]["declared"] == "no profile loaded"
    assert r["prefixes"]["used_but_unregistered"] == "no profile loaded"
    assert r["properties"]["declared_but_unused"] == "no profile loaded"
    # the USED side is still fully populated
    assert r["properties"]["frequency"]["rationale"] == 6


# --------------------------------------------------------------------------- #
# completeness under a truncating token budget (cursor-drain)
# --------------------------------------------------------------------------- #
def test_complete_under_truncating_budget():
    # token_budget=1 in _build_tools forces per-node truncation; the audit must still
    # see all 6 nodes / all 4 edges.
    r = _audit()
    assert r["node_total"] == 6
    assert r["properties"]["frequency"]["rationale"] == 6
    assert r["edge_types"]["resolved_edges"] == 4


# --------------------------------------------------------------------------- #
# freshness gate — a STALE corpus returns the loud marker, NEVER numbers
# --------------------------------------------------------------------------- #
def test_stale_corpus_returns_marker_no_numbers():
    agg = LtsAggregator(_build_tools(_AlwaysStale()))
    r = agg.profile_audit(corpus="hermetic", profile=_PROFILE,
                          required_property="invalidating_assumption",
                          required_scope="prefix=DEC")
    assert r.get("success") is False and r.get("stale") is True
    # NO analytics payload leaked past the gate
    for k in ("prefixes", "properties", "edge_types", "conformance", "node_total"):
        assert k not in r


def test_unknown_corpus_fails():
    agg = LtsAggregator(_build_tools(_AlwaysFresh()))
    r = agg.profile_audit(corpus="does-not-exist", profile=_PROFILE)
    assert r.get("success") is False


# --------------------------------------------------------------------------- #
# the swappable ProfileAdapter seam
# --------------------------------------------------------------------------- #
def test_resolve_profile_adapter_dispatch():
    assert isinstance(resolve_profile_adapter(None), NullProfileAdapter)
    assert isinstance(resolve_profile_adapter(_PROFILE), YamlProfileAdapter)
    # an already-built adapter is passed through unchanged (a future .lts adapter drops here)
    custom = NullProfileAdapter()
    assert resolve_profile_adapter(custom) is custom


def test_yaml_adapter_projects_declared_sets():
    a = YamlProfileAdapter(_PROFILE)
    assert a.available is True
    assert a.prefixes() == {"LIB-", "APP-"}
    assert a.properties() == {"rationale", "part-of", "status"}
    assert a.edge_types() == {"DEPENDS_ON", "RELATED_TO", "USES"}


def test_yaml_adapter_no_property_registry_is_empty_set():
    # an example profile that declares no general property vocabulary
    a = YamlProfileAdapter({"domain": {"id": "x"}, "prefixes": [{"prefix": "Z-"}]})
    assert a.properties() == set()      # -> every used property is used_but_unregistered
    assert a.edge_types() == set()


# --------------------------------------------------------------------------- #
# [C7 / Era 0] THE TRUTHFUL WIRING CHECK
# --------------------------------------------------------------------------- #
def test_property_edges_wiring_is_measured_true_on_every_path():
    """MEASURED 2026-08-16 (Era 0): `property_edges=` reached NO production parse entry
    point -- review V5, the clearest MUST-grade conformance violation found.
    MEASURED 2026-08-16 (Era 1, C3/C4): it reaches EVERY one.

    ⛔ `every`, not `any`. Reachable-from-one-path IS V5b, the state where the same
    (document, profile) pair produced a different graph depending on which entry point
    served it. A test asserting `any` would go green on the bug it is guarding."""
    w = _property_edges_wiring()
    assert len(w["checked"]) == 4
    assert w["unreachable_from"] == []
    assert w["reachable_from_every_production_path"] is True
    assert sorted(w["reachable_from"]) == sorted(w["checked"])


def test_declared_not_threaded_is_the_negative_control(monkeypatch):
    """THE NEGATIVE CONTROL -- an instrument that can only report SUCCESS is not an
    instrument the epistemics rule.

    Era 0 could prove this instrument fires simply by registering a key, because the
    wiring was broken and EVERY registration was declared-not-threaded. C4 fixed the
    wiring, so that proof evaporated: the instrument now reports a true zero, and a
    true zero from a never-seen-to-fail instrument is indistinguishable from a broken
    one. So the fault is INJECTED -- the wiring probe is forced to report a path
    unreachable, i.e. a simulated regression of C4 -- and the instrument must go red,
    naming exactly that key, FL-W106, error."""
    from factlattice_engine.analytics import profile_audit as pa

    prof = {"domain": {"id": "x"}, "prefixes": [{"prefix": "TST-"}]}
    prof_with_registration = {
        **prof,
        "edge_types": [{"property_key": "exemplifies", "type": "EXEMPLIFIES",
                        "layer": "semantic"}],
    }
    dirty = YamlProfileAdapter(prof_with_registration)

    # (1) TRUE ZERO -- the wiring is genuinely correct, so nothing is flagged and the
    #     registered key IS in the truthful threaded set.
    sec = _property_edges_section(dirty, [{"properties": {"exemplifies": "[TST-1]"}}])
    assert sec["declared_not_threaded"] == []
    assert "exemplifies" in sec["threaded_keys_in_effect"]

    # (2) INJECTED FAULT -- C4 regresses on ONE path. The instrument must fire.
    broken = {"checked": ["a", "b"], "reachable_from": ["a"],
              "unreachable_from": ["b"],
              "reachable_from_any_production_path": True,
              "reachable_from_every_production_path": False}
    monkeypatch.setattr(pa, "_property_edges_wiring", lambda: broken)
    sec = _property_edges_section(dirty, [{"properties": {"exemplifies": "[TST-1]"}}])
    assert [d["key"] for d in sec["declared_not_threaded"]] == ["exemplifies"]
    assert sec["declared_not_threaded"][0]["code"] == "FL-W106"
    assert sec["declared_not_threaded"][0]["severity"] == "error"
    assert "exemplifies" not in sec["threaded_keys_in_effect"]
    # ...while the back-compat field still lists it, which is exactly the V6 shape the
    # truthful field exists to correct.
    assert "exemplifies" in sec["minting_keys_in_effect"]


def test_parse_scope_forms():
    assert _parse_scope("prefix=DEC") == {"prefix": "DEC"}
    assert _parse_scope("prefix=DEC,area=decisions") == {"prefix": "DEC", "area": "decisions"}
    assert _parse_scope({"prefix": "DEC"}) == {"prefix": "DEC"}
    assert _parse_scope(None) == {}


# --------------------------------------------------------------------------- #
# MCP dispatch surface
# --------------------------------------------------------------------------- #
def test_dispatch_lts_profile_audit():
    from factlattice_engine.tools import dispatch
    tools = _build_tools(_AlwaysFresh())
    r = dispatch("lts_profile_audit",
                 {"corpus": "hermetic", "profile": _PROFILE,
                  "required_property": "invalidating_assumption",
                  "required_scope": "prefix=DEC", "tail_threshold": 5},
                 tools=tools)
    assert r["success"] is True
    assert r["prefixes"]["used_but_unregistered"] == ["DEC-", "FIX-"]
    assert r["conformance"]["present"] == 1 and r["conformance"]["total"] == 3
