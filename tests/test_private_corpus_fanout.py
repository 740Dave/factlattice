"""[INV-PRIVATE-FANOUT] — a corpus-omitted query never reads a PRIVATE corpus.

THE DEFECT THIS PINS (acme-team ISSUE-FANOUT-LEAK, measured
2026-10-02 on main 14a02a8): `lts_find(text="memory")` with `corpus` omitted consulted all
37 loaded corpora, including the 9 `*-personality` corpora, and 20 of its 582 hits were
personality nodes from 8 different Intelligences' selves. The only guard was a convention
in each agent definition ("always pass corpus="). One forgotten argument fed one
Intelligence another's self.

THE RULE. A corpus is PRIVATE when its registry entry says `private: true`, or when its id
ends in `-personality` (unless the registry says `private: false` explicitly). Every
corpus-omitted fan-out skips private corpora and lists EVERY private corpus under
`extent.corpora_skipped` as ONE anonymous entry `{reason: "private: name it explicitly",
count: N}` counting the whole set, hit or no hit, so the skip never reveals which private
corpus held a match (and never NAMES one: ISSUE-PRIVATE-NAMES-IN-ERRORS). Naming a
private corpus exactly is unchanged: that is how an Intelligence reads its own self.

⛔ Each test asserts BOTH directions: the private node is absent from the fan-out AND
present when its corpus is named. A test of absence alone passes on an engine that has
simply lost the corpus.
"""
from __future__ import annotations

import pytest

from factlattice_engine import config
from factlattice_engine.privacy import PRIVATE_REASON, private_reason
from factlattice_engine.store import FreshnessResult, LtsStore, Partition
from factlattice_engine.tools import LtsEngine

SECRET = "zanzibarquokka"   # a token that exists in exactly one place: the private corpus


class _AlwaysFresh:
    def evaluate(self, part):
        return FreshnessResult(corpus_id=part.corpus_id, fresh=True,
                               index_hash="h", live_hash="h")


def _write(root, name, body):
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(body, encoding="utf-8")


def _corpus_files(tmp_path):
    _write(tmp_path / "pub", "00-pub.lts",
           "# Public corpus [PUB-DOC]\n\n"
           "## Public handover note [PUB-1]\n\n- status: active\n\n"
           "An ordinary public note about memory budgets.\n\n"
           "## Public pointer into other corpora [PUB-2]\n\n- status: active\n"
           "- depends-on: [proj.self:SPEC:SELF-MEM-1]\n"
           "- relates-to: [proj.optout:SPEC:OPT-1]\n")
    _write(tmp_path / "self", "00-self.lts",
           "# A self [SELF-DOC]\n\n"
           f"## The {SECRET} memory [SELF-MEM-1]\n\n- status: active\n"
           f"- what: I remember the {SECRET}.\n- tracks: [proj.pub:SPEC:PUB-1]\n"
           "- depends-on: [proj.pub:SPEC:PUB-1]\n\n"
           f"Body prose that names the {SECRET} again.\n")
    _write(tmp_path / "flagged", "00-flagged.lts",
           "# Flagged private [FLAG-DOC]\n\n"
           f"## A flagged {SECRET} record [FLAG-1]\n\n- status: active\n"
           "- depends-on: [proj.self:SPEC:SELF-MEM-1]\n\n"
           f"Prose with the {SECRET}.\n")
    _write(tmp_path / "optout", "00-optout.lts",
           "# Opted-out personality [OPT-DOC]\n\n"
           f"## Shared {SECRET} notes [OPT-1]\n\n- status: active\n")


def _registry(tmp_path):
    return {
        "pub": {"roots": [{"path": str(tmp_path / "pub"), "area": "specs"}],
                "domain_suffix": "proj.pub"},
        "someone-personality": {"roots": [{"path": str(tmp_path / "self"), "area": "specs"}],
                                "domain_suffix": "proj.self"},
        "flagged": {"roots": [{"path": str(tmp_path / "flagged"), "area": "specs"}],
                    "domain_suffix": "proj.flagged", "private": True},
        "shared-personality": {"roots": [{"path": str(tmp_path / "optout"), "area": "specs"}],
                               "domain_suffix": "proj.optout", "private": False},
    }


@pytest.fixture()
def eng(tmp_path):
    _corpus_files(tmp_path)
    store = config.build_store(_registry(tmp_path))
    assert sorted(store.corpus_ids()) == ["flagged", "pub", "shared-personality",
                                          "someone-personality"]
    return LtsEngine(store, freshness_provider=_AlwaysFresh())


PRIVATE = {"someone-personality", "flagged"}


def _ids(res, key="nodes"):
    return {(n.get("corpus"), n.get("id")) for n in res.get(key) or []}


def _assert_private_skipped(res):
    ext = res["extent"]
    consulted = set(ext["corpora_consulted"])
    assert not (consulted & PRIVATE), consulted
    # ISSUE-PRIVATE-NAMES-IN-ERRORS: the skip is declared as ONE anonymous count
    # of EVERY private corpus (hit or no hit), never as names.
    skipped = [s for s in ext["corpora_skipped"] if s.get("reason") == PRIVATE_REASON]
    assert skipped == [{"reason": PRIVATE_REASON, "count": len(PRIVATE)}], ext["corpora_skipped"]
    assert not any(c in str(ext["corpora_skipped"]) for c in PRIVATE)


# --- the classifier ---------------------------------------------------------
def test_private_reason_rules():
    assert private_reason("x-personality") is not None
    assert private_reason("x-personality", {"private": False}) is None
    assert private_reason("plain", {"private": True}) is not None
    assert private_reason("plain") is None
    assert private_reason("plain", {}) is None


# --- lts_find ---------------------------------------------------------------
def test_find_fanout_skips_private_and_says_so(eng):
    res = eng.find(text=SECRET)
    assert res["success"]
    got = _ids(res)
    assert ("someone-personality", "SELF-MEM-1") not in got
    assert ("flagged", "FLAG-1") not in got
    # the explicit opt-out is still fanned out over
    assert ("shared-personality", "OPT-1") in got
    _assert_private_skipped(res)


def test_find_digest_fanout_skips_private(eng):
    res = eng.find(text=SECRET, tier="digest")
    rows = {(r.get("corpus"), r.get("id")) for r in res["digest"]}
    assert not any(c in PRIVATE for c, _ in rows), rows
    assert res["total_matched"] == len(rows)
    _assert_private_skipped(res)


def test_find_naming_a_private_corpus_still_reads_it(eng):
    res = eng.find(corpus="someone-personality", text=SECRET)
    assert ("someone-personality", "SELF-MEM-1") in _ids(res)
    assert res["extent"]["corpora_skipped"] == []


# --- lts_find_in_prose ------------------------------------------------------
def test_find_in_prose_fanout_skips_private(eng):
    res = eng.find_in_prose(text=SECRET)
    assert res["success"], res
    blob = repr(res)
    assert "SELF-MEM-1" not in blob and "FLAG-1" not in blob
    _assert_private_skipped(res)


def test_find_in_prose_named_private_corpus_still_reads_it(eng):
    res = eng.find_in_prose(text=SECRET, corpus="flagged")
    assert "FLAG-1" in repr(res)


# --- lts_reverse: a private referrer of a PUBLIC node -------------------------
def test_reverse_on_public_node_hides_private_referrers(eng):
    res = eng.reverse(corpus="pub", node_id="PUB-1")
    assert res["success"], res
    assert not any(r["corpus"] in PRIVATE for r in res["referrers"]), res["referrers"]
    assert res["total_matched"] == len([r for r in res["referrers"]])


def test_reverse_digest_on_public_node_hides_private_referrers(eng):
    res = eng.find(corpus="pub", prefix="PUB-1", include_reverse_digest=True)
    for n in res["nodes"]:
        rd = n["reverse_digest"]
        # the one PUBLIC referrer (PUB-DOC CONTAINS PUB-1) stays; the private one goes
        assert rd["total_referrers"] == 1, rd
        assert not any(r["corpus"] in PRIVATE for r in rd["sample"])


def test_reverse_control_private_referrer_exists_in_the_index(eng):
    """Positive control for the two tests above: the private referrer IS really in the
    graph (it is the SELF-MEM-1 `tracks: [PUB-1]`), so its absence above is the rule
    working, not a fixture that never referenced PUB-1."""
    idx, _parts, _stale = eng._reverse_index_for()
    raw = {(r.corpus, r.node_id) for r in idx.referrers("pub", "PUB-1")}
    assert ("someone-personality", "SELF-MEM-1") in raw, raw


# --- lts_aggregate: edge counts over a corpus-omitted fan-out -------------------
def _agg(eng):
    from factlattice_engine.analytics import LtsAggregator
    return LtsAggregator(eng)


def test_aggregate_edge_types_fanout_skips_private(eng):
    """An unscoped edge-type histogram must count only PUBLIC corpora's edges. Control:
    each private corpus has edges of its own when named (its document CONTAINS)."""
    for c in PRIVATE:
        assert _agg(eng).count_by_edge_type(corpus=c)["total_edges"] >= 1, c
    public_total = sum(_agg(eng).count_by_edge_type(corpus=c)["total_edges"]
                       for c in ("pub", "shared-personality"))
    fan = _agg(eng).count_by_edge_type()
    assert fan["success"], fan
    assert fan["total_edges"] == public_total, (fan["total_edges"], public_total)


def test_aggregate_degree_guard_private_cross_corpus_edge_is_not_counted(eng):
    """REGRESSION GUARD, not a RED test: the private self's `depends-on` to PUB-1 is a
    cross-corpus edge, which mints DANGLING inside its own partition, and `degree_index`
    counts resolved edges only, so it never reached PUB-1's degree even before the fix.
    Pinned so a future cross-corpus edge resolver cannot silently start counting it."""
    fan = _agg(eng).degree("PUB-1")
    pub = _agg(eng).degree("PUB-1", corpus="pub")
    assert fan["in_degree"] == pub["in_degree"], (fan, pub)


# --- review F1: a cross-corpus HOP must not route into a private corpus ---------------
# Probe strings that exist ONLY in the private self's node (the opted-out PUBLIC corpus
# also carries SECRET in its title, so SECRET alone is the wrong probe here).
PRIVATE_TEXT_A = f"The {SECRET} memory"
PRIVATE_TEXT_B = f"I remember the {SECRET}"
def _assert_withheld_anonymous(res):
    """The hop is DECLARED, but the stub names no corpus id and no target node
    (ISSUE-PRIVATE-NAMES-IN-ERRORS, review F1)."""
    stubs = res.get("routed_withheld")
    assert stubs, list(res)
    expect = {"withheld": True, "reason": "edge into a private corpus; name it explicitly to follow"}
    assert all(w == expect for w in stubs), stubs


def test_node_cross_corpus_hop_into_private_is_withheld(eng):
    res = eng.node(corpus="pub", node_id="PUB-2")
    assert res["success"], res
    assert PRIVATE_TEXT_A not in repr(res) and PRIVATE_TEXT_B not in repr(res)
    assert not any(r.get("corpus") in PRIVATE for r in res.get("routed_nodes", []))
    _assert_withheld_anonymous(res)


def test_node_cross_corpus_hop_into_public_still_routes(eng):
    """Control: the same node's hop into an opted-out (public) corpus routes in full."""
    res = eng.node(corpus="pub", node_id="PUB-2")
    assert ("shared-personality", "OPT-1") in {(r["corpus"], r["id"]) for r in res["routed_nodes"]}


def test_traverse_cross_corpus_hop_into_private_is_withheld(eng):
    res = eng.traverse(corpus="pub", start_node="PUB-2", max_depth=2)
    assert res["success"], res
    assert PRIVATE_TEXT_A not in repr(res) and PRIVATE_TEXT_B not in repr(res)
    _assert_withheld_anonymous(res)


def test_hop_from_inside_the_private_corpus_is_unchanged(eng):
    """A self reading its own corpus routes OUT to public nodes exactly as before."""
    res = eng.node(corpus="someone-personality", node_id="SELF-MEM-1")
    assert res["success"] and "routed_withheld" not in res


def test_digest_tiers_also_declare_the_withheld_hop(eng):
    for res in (eng.node(corpus="pub", node_id="PUB-2", tier="digest"),
                eng.traverse(corpus="pub", start_node="PUB-2", max_depth=2, tier="digest")):
        assert res["success"], res
        assert PRIVATE_TEXT_A not in repr(res)
        _assert_withheld_anonymous(res)


# --- review F4 / delta F-D1: a TRIPWIRE for new corpus-iteration code ----------------
# Every place the engine touches the store's corpus collection is listed here with the
# exact number of touches and the reason its privacy is correct. A new touch anywhere
# (a new function, a new call inside an allowlisted function, module- or class-level code,
# a store-internal container access) changes the inventory and fails this test until
# someone decides how it treats private corpora.
# ⚠ WHAT THIS IS NOT: enforcement. Static scanning cannot see `getattr(store, "partitions")`,
# a method reference passed around, or iteration of a list a helper already returned. It
# catches the ordinary way new fan-out code gets written; review catches the rest.
_STORE_PRIMITIVE = "LtsStore primitive: defines the corpus collection itself"
FANOUT_ALLOWLIST = {
    ("analytics/core.py", "<class LtsAggregator>._adjacency", "partitions"): (1, "filters is_private_partition"),
    ("analytics/exemplifies.py", "_fresh_partitions", "partitions"): (1, "filters is_private_partition"),
    ("analytics/profile_registration.py", "audit_profile_registration", "corpus_ids"): (1, "registration METADATA per corpus (F2)"),
    ("callcli.py", "registry_statement", "partitions"): (1, "a COUNT of loaded corpora for the registry statement, never names (tests/test_cli_call.py asserts no private name in the envelope)"),
    ("kernel.py", "<class FreshnessKernel>.seat_store", "partitions"): (1, "freshness bookkeeping; serves nothing"),
    ("membership.py", "registered_roots", "partitions"): (1, "root-path membership for write receipts (F2)"),
    ("readprofile.py", "_live_corpus_nodes", "partitions"): (1, "operator CLI listing node ids (P3, accepted)"),
    ("router.py", "<class Router>._resolve_corpus", "filter_by_domain"): (1, "resolves ONE target corpus; privacy decided by the caller (_route)"),
    ("server.py", "build_tools", "partitions"): (1, "emptiness check at boot"),
    ("tools.py", "<class LtsEngine>._fanout_parts", "corpus_ids"): (1, "IS the privacy-aware fan-out"),
    ("tools.py", "<class LtsEngine>._public_corpus_ids", "corpus_ids"): (1, "the PUBLIC-only id list every error/extent uses"),
    ("tools.py", "<class LtsEngine>._private_corpus_ids", "partitions"): (1, "IS the privacy classifier"),
    ("tools.py", "<class LtsEngine>._reverse_index_snapshot", "corpus_ids"): (1, "index over all; output filtered by _public_referrers"),
    ("tools.py", "<class LtsEngine>.list_corpora", "partitions"): (2, "corpus METADATA listing (F2)"),
    ("tools.py", "<class LtsEngine>.register_corpus", "corpus_ids"): (1, "duplicate-id check"),
    ("tools.py", "<class LtsEngine>.unregister_corpus", "corpus_ids"): (1, "membership check; `available` is public-only"),
    ("store.py", "<class LtsStore>.__init__", "_by_corpus_id"): (1, _STORE_PRIMITIVE),
    ("store.py", "<class LtsStore>.__init__", "_by_domain"): (1, _STORE_PRIMITIVE),
    ("store.py", "<class LtsStore>.add_partition", "_by_corpus_id"): (2, _STORE_PRIMITIVE),
    ("store.py", "<class LtsStore>.add_partition", "_by_domain"): (1, _STORE_PRIMITIVE),
    ("store.py", "<class LtsStore>.corpus_ids", "_by_corpus_id"): (1, _STORE_PRIMITIVE),
    ("store.py", "<class LtsStore>.filter_by_domain", "_by_corpus_id"): (1, _STORE_PRIMITIVE),
    ("store.py", "<class LtsStore>.filter_by_domain", "_by_domain"): (1, _STORE_PRIMITIVE),
    ("store.py", "<class LtsStore>.freshness", "_by_corpus_id"): (1, _STORE_PRIMITIVE),
    ("store.py", "<class LtsStore>.get_node", "_by_corpus_id"): (1, _STORE_PRIMITIVE),
    ("store.py", "<class LtsStore>.get_partition", "_by_corpus_id"): (1, _STORE_PRIMITIVE),
    ("store.py", "<class LtsStore>.partitions", "_by_corpus_id"): (1, _STORE_PRIMITIVE),
    ("store.py", "<class LtsStore>.remove_partition", "_by_corpus_id"): (1, _STORE_PRIMITIVE),
    ("store.py", "<class LtsStore>.remove_partition", "_by_domain"): (2, _STORE_PRIMITIVE),
    ("store.py", "<class LtsStore>.select", "_by_corpus_id"): (1, _STORE_PRIMITIVE),
    ("store.py", "<class LtsStore>.stats", "_by_corpus_id"): (1, _STORE_PRIMITIVE),
}
_FANOUT_NAMES = {"partitions", "corpus_ids", "filter_by_domain", "_by_corpus_id", "_by_domain"}


def _fanout_inventory():
    import ast
    import collections
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "factlattice_engine"
    sites = collections.Counter()
    for f in sorted(root.rglob("*.py")):
        rel = f.relative_to(root).as_posix()
        stack = []

        class V(ast.NodeVisitor):
            def _scoped(self, node, name):
                stack.append(name); self.generic_visit(node); stack.pop()

            def visit_FunctionDef(self, n): self._scoped(n, n.name)
            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, n): self._scoped(n, f"<class {n.name}>")

            def visit_Attribute(self, n):
                if n.attr in _FANOUT_NAMES:
                    sites[(rel, ".".join(stack) or "<module>", n.attr)] += 1
                self.generic_visit(n)
        V().visit(ast.parse(f.read_text(encoding="utf-8")))
    return sites


def test_every_corpus_iteration_site_has_a_privacy_decision():
    found = _fanout_inventory()
    allowed = {k: v[0] for k, v in FANOUT_ALLOWLIST.items()}
    changed = sorted((k, found.get(k, 0), allowed.get(k, 0))
                     for k in set(found) | set(allowed) if found.get(k, 0) != allowed.get(k, 0))
    assert not changed, (
        "corpus-collection touch sites changed [(site, found, allowlisted)]: "
        f"{changed}. Decide each new touch's privacy ([INV-PRIVATE-FANOUT]): route it "
        "through _fanout_parts / is_private_partition, or allowlist it with the reason")


# --- delta review F-D2 / F-D4 -------------------------------------------------------
def test_capabilities_declares_a_withheld_private_hop(eng):
    res = eng.capabilities(corpus="pub", node_id="PUB-2")
    assert res["success"], res
    assert PRIVATE_TEXT_A not in repr(res) and PRIVATE_TEXT_B not in repr(res)
    _assert_withheld_anonymous(res)
    # control: the hop into the opted-out PUBLIC corpus still routes
    assert any(r.get("corpus") == "shared-personality" for r in res["routed_capabilities"])


def test_private_to_private_hop_is_withheld(eng):
    """flagged (private, named) -> someone-personality (a DIFFERENT private corpus)."""
    res = eng.node(corpus="flagged", node_id="FLAG-1")
    assert res["success"], res
    assert PRIVATE_TEXT_A not in repr(res) and PRIVATE_TEXT_B not in repr(res)
    _assert_withheld_anonymous(res)


def test_stale_private_target_is_withheld_not_reported_stale(tmp_path):
    """Privacy is decided BEFORE freshness: a hop into a stale private corpus is the same
    withheld stub as a fresh one (one rule, not two paths that leak differently)."""
    class _SelfStale:
        def evaluate(self, part):
            stale = part.corpus_id == "someone-personality"
            return FreshnessResult(corpus_id=part.corpus_id, fresh=not stale,
                                   index_hash="h", live_hash="CHANGED" if stale else "h")
    _corpus_files(tmp_path)
    e2 = LtsEngine(config.build_store(_registry(tmp_path)), freshness_provider=_SelfStale())
    res = e2.traverse(corpus="pub", start_node="PUB-2", max_depth=1)
    assert res["success"], res
    assert not any(r.get("corpus") == "someone-personality" for r in res.get("routed_stale", []))
    _assert_withheld_anonymous(res)
