"""A cache keyed on a freshness signal must be keyed on what it was BUILT FROM.

The defect (live, measured): the partition's freshness key is the kernel's MAINTAINED root, which
moves the instant a file changes, while `Partition.nodes` / `.layers` are replaced only when the
self-heal publishes. A reader that had already passed its gate and then reached a cache in the gap
between the two built the cache from the OLD nodes and filed it under the NEW root. The heal then
published the new nodes under that same root, the key still matched, and the cache kept serving the
old content until the next change: `success: true`, `fresh: true`, count 0, with the token sitting
in `part.nodes`.

Every test here forces that interleaving DETERMINISTICALLY through the tool a caller meets, by
wrapping the engine's `_gate` so that, once, AFTER the gate has passed fresh and BEFORE the tool
reaches its cache, a file is written and the kernel is polled (the root moves; nodes are still the
old ones). The next call is a read that BEGAN after the write completed: it must see the write.
Each test has a CONTROL (the same steps without the interleaving) that must pass on any code, so a
red cannot be a setup fault.

Sibling temp dirs only (corpus/ under tmp_path); nothing under the real state dir.
"""
from __future__ import annotations

import os

import pytest

import factlattice_engine
from factlattice_engine import FreshnessKernel, LtsEngine
from factlattice_engine.config import build_store
from factlattice_engine.selfheal import SelfHealer
from factlattice_engine.verdict import PollVerifiedFreshness


def _doc(extra_a1: str = "", extra_a2: str = "") -> str:
    return ("---\ndomain: core.example\n---\n"
            "# Test [SPEC:A-DOC]\n\n"
            "### [A1] Widget one\n- definition: base alpha\n" + extra_a1 + "\n"
            "### [A2] Widget two\n- definition: base beta gamma\n" + extra_a2 + "\n")


class Rig:
    """One corpus `a`, a seated kernel with NO watcher thread (every root move is an explicit
    `poll_once`), a real healer, and the real freshness provider."""

    def __init__(self, base, text=None):
        self.root = os.path.join(str(base), "corpus")
        os.mkdir(self.root)
        self.path = os.path.join(self.root, "f.lts")
        with open(self.path, "w") as fh:
            fh.write(text if text is not None else _doc())
        self.store = build_store({"a": {"root": self.root, "domain_suffix": "proj.a"}})
        self.kernel = FreshnessKernel(poll_interval=100.0)
        self.kernel.seat_store(self.store)
        self.store.kernel = self.kernel
        provider = PollVerifiedFreshness()
        healer = SelfHealer(self.store, kernel=self.kernel, freshness_provider=provider)
        self.eng = LtsEngine(self.store, healer=healer, freshness_provider=provider)
        self.part = self.store.get_partition("a")
        self.real_gate = self.eng._gate

    def write(self, line: str, node_marker: str = "### [A1]") -> None:
        """Append `line` to the A1 node's property block (the line right after A1's definition)."""
        with open(self.path) as fh:
            src = fh.read()
        head, _, tail = src.partition(node_marker)
        first, sep, rest = tail.partition("\n- definition:")
        # insert after the definition line of that node
        defline, nl, after = rest.partition("\n")
        new = head + node_marker + first + sep + defline + nl + line + "\n" + after
        with open(self.path, "w") as fh:
            fh.write(new)

    def append_eof(self, line: str) -> None:
        """Append `line` as the last line of the file: OUTSIDE every line range the old nodes carry."""
        with open(self.path, "a") as fh:
            fh.write(line + "\n")

    def move_root(self) -> None:
        """The write has completed and the kernel has seen it: the maintained root moved; the
        partition's nodes/layers are still the old ones until a heal publishes."""
        self.kernel.poll_once(self.part)
        assert self.kernel.current_root(self.part) != self.part.files_hash   # positive control: it moved

    def after_next_gate(self, fn) -> None:
        """One-shot: the next `_gate` passes fresh as usual, then `fn()` runs before the tool goes on."""
        state = {"armed": True}
        real = self.real_gate

        def gate(corpus):
            res = real(corpus)
            if state["armed"] and res[1] is None:
                state["armed"] = False
                fn()
            return res
        self.eng._gate = gate

    def heal_now(self):
        """The heal, via the real gate (publishes the new nodes/layers)."""
        part, err = self.real_gate("a")
        assert err is None, err
        assert self.part.files_hash == self.kernel.current_root(self.part)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ---------------------------------------------------------------- body index (lts_find_in_prose)
TOK = "tokZZZ"


def _prose_count(rig, **kw):
    r = rig.eng.find_in_prose(text=TOK, **kw)
    assert r["success"] is True, r
    return r["count"]


def test_prose_control_no_interleaving_sees_the_write(rig):
    rig.eng.find_in_prose(text="base", corpus="a")                     # warm the body cache
    rig.append_eof(f"- k: {TOK}")
    rig.move_root()
    assert _prose_count(rig, corpus="a") == 1


def test_prose_reader_in_the_gap_must_not_poison_the_read_that_follows(rig):
    rig.eng.find_in_prose(text="base", corpus="a")                     # warm
    def write_and_move():
        rig.append_eof(f"- k: {TOK}")
        rig.move_root()
    rig.after_next_gate(write_and_move)
    rig.eng.find_in_prose(text=TOK, corpus="a")                        # the reader that gated before the write
    rig.eng._gate = rig.real_gate
    assert _prose_count(rig, corpus="a") == 1, "named read that began after the write missed it"
    assert _prose_count(rig) == 1, "corpus-omitted read that began after the write missed it"
    assert TOK in repr(rig.part.nodes)                                  # the heal did publish it


# ---------------------------------------------------------------- adjacency (node / traverse)
def _fwd_targets(rig, start="A1"):
    r = rig.eng.traverse(corpus="a", start_node=start, direction="forward", max_depth=1)
    assert r["success"] is True, r
    return sorted(n["id"] for n in r.get("nodes", []))


def _edge_rig(rig):
    # warm adjacency at the OLD root, with a first traversal
    assert _fwd_targets(rig) == ["A1"] or _fwd_targets(rig) == []


def test_adjacency_control_no_interleaving_sees_the_new_edge(rig):
    _edge_rig(rig)
    rig.write("- depends-on: [A2]")
    rig.move_root()
    assert "A2" in _fwd_targets(rig)


def test_adjacency_build_straddling_the_publish_must_not_poison_the_next_read(rig):
    """The window alone is closed for adjacency by `_publish`'s explicit clear. The hole is a build
    that STRADDLES the publish: it reads the old layers, the heal publishes and clears, then the build
    stores old edges under the new root."""
    _edge_rig(rig)
    real_build = rig.part._build_adjacency

    def straddling_build(*a, **kw):
        out = real_build(*a, **kw)          # built from the OLD layers
        rig.part._build_adjacency = real_build
        rig.heal_now()                      # ...the heal publishes + clears while that build is in flight
        return out
    def write_and_move():
        rig.write("- depends-on: [A2]")
        rig.move_root()
        rig.part._build_adjacency = straddling_build
    rig.after_next_gate(write_and_move)
    _fwd_targets(rig)                       # the in-flight reader (its own answer is a pre-write answer)
    rig.eng._gate = rig.real_gate
    assert "A2" in _fwd_targets(rig), "traverse that began after the write missed the edge"
    node = rig.eng.node(corpus="a", node_id="A1")
    edges = repr(node)
    assert "A2" in edges, "lts_node that began after the write missed the edge"


# ---------------------------------------------------------------- degree index (lts_aggregate)
def _edge_count(rig):
    from factlattice_engine.analytics import LtsAggregator
    r = LtsAggregator(rig.eng).count_by_edge_type(corpus="a")
    assert r["success"] is True, r
    return r["resolved_edges"]


def test_degree_index_build_straddling_the_publish_must_not_poison_the_next_read(rig):
    base = _edge_count(rig)
    real_build = rig.part._build_degree_index

    def straddling_build(*a, **kw):
        out = real_build(*a, **kw)
        rig.part._build_degree_index = real_build
        rig.heal_now()
        return out
    def write_and_move():
        rig.write("- depends-on: [A2]")
        rig.move_root()
        rig.part._build_degree_index = straddling_build
    rig.after_next_gate(write_and_move)
    _edge_count(rig)
    rig.eng._gate = rig.real_gate
    assert _edge_count(rig) == base + 1, "lts_aggregate that began after the write missed the edge"


# ---------------------------------------------------------------- structural find (field caches)
def _find_ids(rig, term, match="phrase"):
    """A term in `all_of` is what runs through the per-partition field caches (bare `text` does not)."""
    r = rig.eng.find(corpus="a", all_of=[{"term": term, "match": match}])
    assert r["success"] is True, r
    return sorted(n["id"] for n in r["nodes"])


def test_find_control_no_interleaving_sees_the_new_value(rig):
    _find_ids(rig, "base")
    rig.write("- note: tokYYY")
    rig.move_root()
    assert _find_ids(rig, "tokYYY") == ["A1"]


def test_find_reader_in_the_gap_must_not_poison_the_field_cache(rig):
    assert _find_ids(rig, "base") == ["A1", "A2"]
    assert _find_ids(rig, "tok", "prefix") == []        # vocabulary built too
    def write_and_move():
        rig.write("- note: tokYYY")
        rig.move_root()
    rig.after_next_gate(write_and_move)
    # the in-flight reader: its FIRST call builds the vocabulary AND the entries from the old nodes
    r = rig.eng.find(corpus="a", all_of=[{"term": "tok", "match": "prefix"}, {"term": "base"}])
    assert r["success"] is True
    rig.eng._gate = rig.real_gate
    assert _find_ids(rig, "tokYYY") == ["A1"], "phrase term that began after the write missed it"
    assert _find_ids(rig, "tokyy", "prefix") == ["A1"], "prefix term (vocabulary cache) missed it"


# ---------------------------------------------------------------- relevance rank (lexrank cache)
def _rank(rig):
    r = rig.eng.find(corpus="a", text="Widget", rank="relevance")
    assert r["success"] is True, r
    return [n["id"] for n in r["nodes"]]


LONG = " ".join(f"filler{i}" for i in range(60))


def _cold_rank_after_edit(tmp_path):
    """The relevance order of a COLD engine over the corpus as it stands after the edit."""
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    edited = Rig(tmp_path / "x")
    edited.write(f"- more: {LONG}")
    return _rank(Rig(tmp_path / "y", text=open(edited.path).read()))


def test_rank_sensitivity_control_the_edit_really_changes_the_order(tmp_path):
    """So a stale order is distinguishable from a fresh one: measured on cold engines."""
    (tmp_path / "z").mkdir()
    assert _rank(Rig(tmp_path / "z")) != _cold_rank_after_edit(tmp_path)


def test_rank_reader_in_the_gap_must_not_poison_the_lexrank_cache(tmp_path):
    expected = _cold_rank_after_edit(tmp_path)
    (tmp_path / "live").mkdir()
    rig = Rig(tmp_path / "live")
    _rank(rig)                                # warm
    def write_and_move():
        rig.write(f"- more: {LONG}")
        rig.move_root()
    rig.after_next_gate(write_and_move)
    _rank(rig)
    rig.eng._gate = rig.real_gate
    assert _rank(rig) == expected, "relevance order after the write is the pre-write order"


# ---------------------------------------------------------------- reverse index (lts_reverse)
def _referrers(rig, node="A2"):
    r = rig.eng.reverse(corpus="a", node_id=node)
    assert r["success"] is True, r
    return sorted(x["node_id"] for x in r["referrers"] if x["node_id"] != "A-DOC")


def test_reverse_control_no_interleaving_sees_the_new_referrer(rig):
    assert _referrers(rig) == []
    rig.write("- depends-on: [A2]")
    rig.move_root()
    assert _referrers(rig) == ["A1"]


def test_reverse_snapshot_in_the_gap_must_not_poison_the_reverse_index(rig):
    assert _referrers(rig) == []             # warm
    real_snap = rig.eng._reverse_index_snapshot
    state = {"armed": True}

    def snap():
        out = real_snap()                    # every corpus gated fresh...
        if state["armed"]:
            state["armed"] = False
            rig.write("- depends-on: [A2]")  # ...then the write lands and the root moves,
            rig.move_root()                  # before snapshot_key() and build() read the partitions
        return out
    rig.eng._reverse_index_snapshot = snap
    _referrers(rig)                          # the in-flight reader
    rig.eng._reverse_index_snapshot = real_snap
    assert _referrers(rig) == ["A1"], "lts_reverse that began after the write missed the referrer"


# ---------------------------------------------------------------- semantic per-corpus state
def test_semantic_state_built_in_the_gap_must_not_survive_the_publish(rig):
    """`SemanticService._state` caches cards + BM25 under the freshness key. NOT driven through
    `lts_semantic_find`: that tool needs the embedding model before it reaches `_state`, which this
    environment does not have. The unit is called with the same real partition in the same gap."""
    from factlattice_engine.semantic.suggest import SemanticService
    svc = SemanticService()
    svc._state(rig.part)                                   # warm at the old root
    rig.write("- note: tokQQQ")
    rig.move_root()
    svc._state(rig.part)                                   # the reader in the gap: new root, old nodes
    rig.heal_now()
    text = repr(svc._state(rig.part)["cards"])
    assert "tokQQQ" in text, "semantic state after the publish is the pre-write state"


# ---------------------------------------------------------------- the second interleaving: a build that straddles the publish
def test_body_build_straddling_the_publish_must_not_poison_the_next_read(rig):
    """The window test above has the key move before the build; here the build READS the old nodes and the
    publish lands before it stores. A source captured after the build (or a key alone) is fooled by this."""
    rig.eng.find_in_prose(text="base", corpus="a")
    real_build = rig.part._build_body_index
    def straddling(*a, **kw):
        out = real_build(*a, **kw)          # read from the OLD nodes
        rig.part._build_body_index = real_build
        rig.heal_now()                      # publish lands before the result is stored
        return out
    def write_and_move():
        rig.append_eof(f"- k: {TOK}")
        rig.move_root()
        rig.part._build_body_index = straddling
    rig.after_next_gate(write_and_move)
    rig.eng.find_in_prose(text=TOK, corpus="a")
    rig.eng._gate = rig.real_gate
    assert _prose_count(rig, corpus="a") == 1


def test_reverse_build_straddling_the_publish_must_not_poison_the_next_read(rig):
    from factlattice_engine.reverse_index import ReverseIndex
    assert _referrers(rig) == []
    real_snap = rig.eng._reverse_index_snapshot
    real_build = ReverseIndex.build.__func__
    state = {"armed": True}
    def snap():
        out = real_snap()
        if state["armed"]:
            rig.write("- depends-on: [A2]")
            rig.move_root()
        return out
    def build(cls, parts, *a, **kw):
        idx = real_build(cls, parts, *a, **kw)     # from the OLD nodes/layers
        if state["armed"]:
            state["armed"] = False
            rig.heal_now()                          # the publish lands before the index is stored
        return idx
    rig.eng._reverse_index_snapshot = snap
    ReverseIndex.build = classmethod(build)
    try:
        _referrers(rig)
    finally:
        ReverseIndex.build = classmethod(real_build)
        rig.eng._reverse_index_snapshot = real_snap
    assert _referrers(rig) == ["A1"]


def test_field_entry_built_from_a_superseded_node_dict_is_not_served(rig):
    """A caller binds on one read of `part.nodes` and iterates another: an entry is served only for the
    dict it was lowered from, whatever its cache is keyed on."""
    from factlattice_engine import query_surface as qs
    caches = qs.FieldCaches()
    old_nodes = rig.part.nodes
    fc = caches.for_part(rig.part)
    assert "base" in repr(fc.entry(old_nodes["A1"], "A1").lows)
    rig.write("- note: tokYYY")
    rig.move_root()
    rig.heal_now()
    new_nodes = rig.part.nodes
    assert new_nodes is not old_nodes and new_nodes["A1"] is not old_nodes["A1"]
    assert "tokyyy" in repr(fc.entry(new_nodes["A1"], "A1").lows)       # same cache object, new dict
    assert caches.for_part(rig.part) is not fc               # and a new bind gets a new cache


# ---------------------------------------------------------------- pin: a new freshness-keyed cache must be accounted for
import pathlib
import re

_KEY_CALL = re.compile(r"\._freshness_key\(\)")
# Every production reader of `_freshness_key()` and what stops it serving content it was not built from.
_ACCOUNTED = {
    "store.py": 1,                 # Partition._derived: adjacency, degree index, body index (source object stored beside the value)
    "query_surface.py": 1,         # FieldCaches.for_part: FieldCache.src + _Entry.src
    "tools.py": 1,                 # LtsEngine._lexrank_for: the nodes object is the third element of the cached tuple
    "reverse_index.py": 2,         # snapshot_key and one mention in the module docstring; the engine pairs it with snapshot_sources / sources_current
    "semantic/suggest.py": 1,      # SemanticService._state: st["nodes"]
}


def _scan(root: pathlib.Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*.py")):
        n = len(_KEY_CALL.findall(p.read_text(encoding="utf-8")))
        if n:
            out[str(p.relative_to(root))] = n
    return out


def test_scanner_finds_a_planted_call(tmp_path):
    (tmp_path / "x.py").write_text("k = part._freshness_key()\n")
    (tmp_path / "y.py").write_text("# nothing here\n")
    assert _scan(tmp_path) == {"x.py": 1}


def test_every_freshness_key_reader_is_accounted_for():
    """A new cache filed under `_freshness_key()` fails here until its author has read SDLC 7.6
    [CACHE-KEYED-ON-WHAT-IT-WAS-BUILT-FROM], keyed it on the object it is built from, added a forced-
    interleaving test above, and listed it. A tripwire for the literal call form only: a cache keyed on
    `files_hash` or `kernel.current_root` directly is not seen."""
    root = pathlib.Path(factlattice_engine.__file__).parent
    assert _scan(root) == _ACCOUNTED


# ================================================================ round 2: pins for the rule itself
def _with_new_edge_published(rig):
    """(old_nodes, old_layers) of a partition whose heal has since published a new edge A1 -> A2 and a token."""
    old_nodes, old_layers = rig.part.nodes, rig.part.layers
    rig.write("- depends-on: [A2]")
    rig.append_eof(f"- k: {TOK}")
    rig.move_root()
    rig.heal_now()
    assert rig.part.nodes is not old_nodes and rig.part.layers is not old_layers
    return old_nodes, old_layers


# ---- the freshness key stays a SECOND condition: a root that moved with no publish still rebuilds
def test_key_is_still_a_condition_when_nothing_is_published(rig):
    part = rig.part
    part.adjacency(); part.degree_index(); part.body_index()
    before = (part._adj_builds, part._deg_builds, part._body_builds)
    nodes, layers = part.nodes, part.layers
    rig.write("- depends-on: [A2]")
    rig.move_root()                                   # root moved; NO heal: nodes/layers are the same objects
    assert part.nodes is nodes and part.layers is layers
    part.adjacency(); part.degree_index(); part.body_index()
    assert (part._adj_builds, part._deg_builds, part._body_builds) == tuple(b + 1 for b in before)


def test_key_is_still_a_condition_for_field_lexrank_semantic_and_reverse(rig):
    from factlattice_engine import query_surface as qs
    from factlattice_engine.semantic.suggest import SemanticService
    part = rig.part
    caches, svc = qs.FieldCaches(), SemanticService()
    fc, lex, st = caches.for_part(part), rig.eng._lexrank_for(part), svc._state(part)
    rig.eng._reverse_index_snapshot = lambda: ([part], [])
    ri_slot = rig.eng._reverse_index_for() and rig.eng._reverse_slot
    rig.write("- depends-on: [A2]")
    rig.move_root()                                   # same nodes/layers objects, new key
    assert caches.for_part(part) is not fc
    assert rig.eng._lexrank_for(part) is not lex
    assert svc._state(part) is not st
    rig.eng._reverse_index_for()
    assert rig.eng._reverse_slot is not ri_slot and rig.eng._reverse_slot[0] is not ri_slot[0]


# ---- a build reads the object it was GIVEN, never `self.<attr>` again
def test_builders_read_the_object_they_are_given_not_the_partition(rig):
    from factlattice_engine.reverse_index import ReverseIndex, snapshot_sources
    from factlattice_engine.semantic.index import partition_cards
    part = rig.part
    old_nodes, old_layers = _with_new_edge_published(rig)
    fwd, _rev = part._build_adjacency(old_layers)
    assert not any(e["target"] == "A2" for _l, e in fwd.get("A1", [])), "adjacency built from the live layers"
    assert part._build_degree_index(old_layers)["resolved_edges"] < part._build_degree_index(part.layers)["resolved_edges"]
    body, report = part._build_body_index(old_nodes)
    assert report["nodes_in_scope"] == len(old_nodes)
    assert TOK not in repr(body), "body index built from the live nodes"
    assert TOK not in repr(partition_cards(part, old_nodes)), "cards built from the live nodes"
    idx = ReverseIndex.build([part], sources={"a": (old_nodes, old_layers)})
    assert [r.node_id for r in idx.referrers("a", "A2") if r.node_id != "A-DOC"] == [], "reverse index built from live layers"
    assert [r.node_id for r in ReverseIndex.build([part], sources=snapshot_sources([part])).referrers("a", "A2")
            if r.node_id != "A-DOC"] == ["A1"]                                           # control: the live objects see it


def _heal_before_build(rig, attr):
    """Wrap `rig.part.<attr>` so the heal publishes just BEFORE the real builder runs (and is called with whatever
    object `_derived` captured). A builder fed by a re-read of `self` would now see the new content."""
    real = getattr(rig.part, attr)
    def wrapped(*a, **kw):
        setattr(rig.part, attr, real)
        rig.heal_now()
        return real(*a, **kw)
    setattr(rig.part, attr, wrapped)


@pytest.mark.parametrize("attr,slot,src_attr", [("_build_adjacency", "_adj_slot", "layers"),
                                                ("_build_degree_index", "_deg_slot", "layers"),
                                                ("_build_body_index", "_body_slot", "nodes")])
def test_derived_hands_the_builder_the_object_it_captured(tmp_path, attr, slot, src_attr):
    rig = Rig(tmp_path)
    part = rig.part
    call = {"_build_adjacency": part.adjacency, "_build_degree_index": part.degree_index,
            "_build_body_index": part.body_index}[attr]
    old = getattr(part, src_attr)
    edges_before = part.resolved_edge_count
    rig.write("- depends-on: [A2]")
    rig.append_eof(f"- k: {TOK}")
    rig.move_root()
    _heal_before_build(rig, attr)               # the publish lands as the build starts
    assert getattr(part, src_attr) is old
    value = call()
    assert getattr(part, src_attr) is not old   # (the wrapper healed)
    stored = getattr(part, slot)
    assert stored[1] is old, "the slot does not record the object the build was handed"
    if attr == "_build_adjacency":
        assert not any(e["type"] == "DEPENDS_ON" for l in value[0].values() for _x, e in l), "built from the live layers"
    elif attr == "_build_degree_index":
        assert value["resolved_edges"] == edges_before, "built from the live layers"
    else:
        assert TOK not in repr(value), "built from the live nodes"
    assert call() is not value, "a slot built from a superseded object was served"


# ---- a publish that lands DURING a build: field caches, relevance cache, semantic state
def test_relevance_cache_built_across_a_publish_is_not_served(tmp_path):
    from factlattice_engine.semantic import index as sidx
    expected = _cold_rank_after_edit(tmp_path)
    (tmp_path / "live").mkdir()
    rig = Rig(tmp_path / "live")
    real = sidx.partition_cards
    def straddle(part, nodes=None):
        sidx.partition_cards = real
        rig.write(f"- more: {LONG}")
        rig.move_root()
        rig.heal_now()                              # the publish lands as the cards are about to be read
        return real(part, nodes)
    sidx.partition_cards = straddle
    try:
        first = rig.eng._lexrank_for(rig.part)
    finally:
        sidx.partition_cards = real
    assert first.search("filler7", 5) == [], "built from the live nodes, not the object it captured"
    assert rig.eng._lexrank_for(rig.part).search("filler7", 5), "the superseded build was served"
    assert _rank(rig) == expected


def test_semantic_state_built_across_a_publish_is_not_served(rig):
    from factlattice_engine.semantic import suggest
    from factlattice_engine.semantic.suggest import SemanticService
    svc = SemanticService()
    real = suggest.partition_cards
    def straddle(part, nodes=None):
        suggest.partition_cards = real
        rig.write("- note: tokQQQ")
        rig.move_root()
        rig.heal_now()                              # the publish lands as the cards are about to be read
        return real(part, nodes)
    suggest.partition_cards = straddle
    try:
        first = svc._state(rig.part)
    finally:
        suggest.partition_cards = real
    assert "tokQQQ" not in repr(first["cards"]), "built from the live nodes, not the object it captured"
    assert "tokQQQ" in repr(svc._state(rig.part)["cards"]), "the superseded build was served"


# ---- the field-cache BIND reads `part.nodes` once: cache and vocabulary come from the same object
def test_bind_builds_the_prefix_vocabulary_from_the_nodes_it_captured(rig):
    """A publish lands between `for_part` and the vocabulary build inside `CompiledQuery.bind`. The term's
    expansion must come from the object bind captured (the old nodes), not from a second read of `part.nodes`."""
    from factlattice_engine import query_surface as qs
    real = qs.FieldCaches()
    class HealsAfterForPart:
        def for_part(self, part, nodes=None):
            fc = real.for_part(part, nodes)
            rig.write("- note: tokYYY")
            rig.move_root()
            rig.heal_now()                      # part.nodes is now a different object
            return fc
    old_nodes = rig.part.nodes
    cq = qs.compile_query(all_of=[{"term": "tokyy", "match": "prefix"}], corpus="a")
    cq.bind(rig.part, HealsAfterForPart())
    assert rig.part.nodes is not old_nodes
    assert cq.all_of[0].expansion == frozenset(), "vocabulary came from a second read of part.nodes"
    # control: bound after the publish, the same query expands to the new word
    cq2 = qs.compile_query(all_of=[{"term": "tokyy", "match": "prefix"}], corpus="a")
    cq2.bind(rig.part, qs.FieldCaches())
    assert cq2.all_of[0].expansion == frozenset({"tokyyy"})


def test_for_part_serves_the_nodes_it_was_given_not_the_partitions_current_ones(rig):
    from factlattice_engine import query_surface as qs
    caches = qs.FieldCaches()
    old_nodes = rig.part.nodes
    rig.write("- note: tokYYY")
    rig.move_root()
    rig.heal_now()
    new_nodes = rig.part.nodes
    fc_old = caches.for_part(rig.part, old_nodes)
    assert fc_old.src is old_nodes, "for_part ignored the nodes it was handed"
    assert "tokyyy" not in fc_old.vocab(old_nodes)
    fc_new = caches.for_part(rig.part, new_nodes)
    assert fc_new is not fc_old and fc_new.src is new_nodes


# ---- sources_current: BOTH objects count (a mutant checking only nodes, or only layers, must die)
def test_sources_current_requires_both_nodes_and_layers_to_be_the_same_objects():
    from factlattice_engine.reverse_index import sources_current
    n1, n2, l1, l2 = {"x": 1}, {"x": 1}, {"y": 1}, {"y": 1}      # equal but not identical: identity is the rule
    assert sources_current({"a": (n1, l1)}, {"a": (n1, l1)}) is True
    assert sources_current({"a": (n1, l1)}, {"a": (n1, l2)}) is False      # same nodes, new layers
    assert sources_current({"a": (n1, l1)}, {"a": (n2, l1)}) is False      # new nodes, same layers
    assert sources_current({"a": (n1, l1)}, {"a": (n2, l2)}) is False
    assert sources_current({"a": (n1, l1)}, {"a": (n1, l1), "b": (n1, l1)}) is False   # a corpus appeared
    assert sources_current({"a": (n1, l1), "b": (n1, l1)}, {"a": (n1, l1)}) is False   # a corpus went
    assert sources_current(None, {"a": (n1, l1)}) is False


# ---- the reverse slot
def test_reverse_call_returns_the_index_of_its_own_snapshot_whatever_the_slot_holds(rig):
    """After this call has stored its slot and before it returns, another thread's result replaces the slot
    (an older or newer build landing). The call must return the index it built from ITS snapshot, not whatever
    the slot holds by then (kills: `return self._reverse_slot[0]`). Forced single-threaded by replacing the
    slot at the instant after this call's own assignment."""
    from factlattice_engine.reverse_index import ReverseIndex
    from factlattice_engine.tools import LtsEngine
    eng = rig.eng
    eng._reverse_index_snapshot = lambda: ([rig.part], [])
    sentinel = (object(), "other-key", {"other": ({}, {})})
    built = {}
    real_build = ReverseIndex.build.__func__
    real_setattr = LtsEngine.__setattr__
    def build(cls, parts, *a, **kw):
        built["idx"] = real_build(cls, parts, *a, **kw)
        return built["idx"]
    def setattr_then_replace(self, name, value):
        real_setattr(self, name, value)
        if self is eng and name == "_reverse_slot" and value is not None:
            real_setattr(self, name, sentinel)       # "another thread" lands right after this call's assignment
    ReverseIndex.build = classmethod(build)
    LtsEngine.__setattr__ = setattr_then_replace
    try:
        eng._reverse_slot = None
        got, _parts, _stale = eng._reverse_index_for()
    finally:
        LtsEngine.__setattr__ = real_setattr
        ReverseIndex.build = classmethod(real_build)
    assert eng._reverse_slot is sentinel            # control: the replacement really happened before the return
    assert got is built["idx"], "the call returned the slot's index, not the one built from its own snapshot"
    assert got is not sentinel[0]


def test_reverse_slot_is_written_as_one_assignment_TRIPWIRE(rig):
    """STRUCTURAL TRIPWIRE, not a race test. Two threads cannot be forced to interleave INSIDE a sequence of
    attribute assignments, so this pins the shape that closes the window by construction: one rebuild writes
    exactly one attribute, `_reverse_slot`, once, as a 3-tuple. On the 22884d9 shape (three assignments:
    `_reverse_index`, `_reverse_index_key`, `_reverse_index_sources`) it fails. It cannot prove there is no
    other torn state; the race itself is `probes-cache/revrace.py`, a manual probe."""
    from factlattice_engine.tools import LtsEngine
    eng = rig.eng
    eng._reverse_index_snapshot = lambda: ([rig.part], [])
    writes = []
    real_setattr = LtsEngine.__setattr__
    def recording(self, name, value):
        if self is eng and "reverse" in name:
            writes.append((name, type(value).__name__, len(value) if isinstance(value, tuple) else None))
        real_setattr(self, name, value)
    LtsEngine.__setattr__ = recording
    try:
        eng._reverse_slot = None
        writes.clear()
        eng._reverse_index_for()
    finally:
        LtsEngine.__setattr__ = real_setattr
    assert writes == [("_reverse_slot", "tuple", 3)], writes
    assert not any(n in eng.__dict__ for n in ("_reverse_index", "_reverse_index_key", "_reverse_index_sources"))
