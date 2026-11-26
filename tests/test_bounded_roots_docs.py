"""SDLC.md 7.5 / 7.6 carry the rules spec 38 x-extension-points names for the slices built so far, and each rule's check exists.

The rule ids are the ones the spec lists; CALL-WORKER-NO-STDIO belongs to slice 5 and is not required yet. A rule that names a check
(`tests/<file>::<test>`) must name a test that is defined, so a renamed test cannot leave a rule pointing at nothing (that happened to
[ROOT-STATE-ONE-REGISTRY] until this test was written)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
REQUIRED_76 = ("ROOT-READS-BOUNDED", "ROOT-STATE-ONE-REGISTRY", "INPROCESS-BUILD-STORE-CONTRACT", "ONE-BOUND-REGISTRY-PER-PROCESS", "ROOT-IO-INVENTORY")


def _sdlc() -> str:
    return (REPO / "SDLC.md").read_text(encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i + 1)]


def test_sdlc_7_6_carries_the_root_read_rules_and_7_5_the_hop_rule():
    t = _sdlc()
    s76 = _section(t, "### 7.6 FACTLATTICE-SPEC-CHECKLIST", "## 8. Stage 5")
    for rule in REQUIRED_76:
        assert f"- [ ] [{rule}]" in s76, f"SDLC 7.6 lacks the rule [{rule}]"
    s75 = _section(t, "### 7.5 Standing Check", "### 7.6 ")
    assert "routed_stale" in s75 and "root-pending" in s75, "SDLC 7.5 lacks the rule for a hop into a corpus that is not served"


# Test files about the trunk's own CI: PUBLISH-EXCLUDE removes them, so a published snapshot has none of them (spec 42 R10).
_PRIVATE_CI_TEST_FILES = {"tests/test_merge_queue.py", "tests/test_queue_flake_retry.py", "tests/test_prci_yields_to_queue.py",
                          "tests/test_nightly_issue.py"}


def _dangling_references(private_ci):
    """Every `tests/<file>.py::<name>` the SDLC checklist cites whose file or `def` is missing, for the cited files that are (private_ci=True)
    or are not (False) the trunk's own CI test files."""
    missing = []
    for m in re.finditer(r"`(tests/[\w./]+\.py)::(\w+)`", _sdlc()):
        f, name = m.group(1), m.group(2)
        if (f in _PRIVATE_CI_TEST_FILES) != private_ci:
            continue
        path = REPO / f
        if not path.is_file() or f"def {name}(" not in path.read_text(encoding="utf-8"):
            missing.append((f, name))
    return missing


def test_every_check_a_root_read_rule_names_is_a_test_that_exists():
    assert not _dangling_references(private_ci=False), _dangling_references(private_ci=False)


@pytest.mark.skipif(not all((REPO / f).is_file() for f in sorted(_PRIVATE_CI_TEST_FILES)),
                    reason="tests/test_merge_queue.py, tests/test_queue_flake_retry.py, tests/test_prci_yields_to_queue.py and "
                           "tests/test_nightly_issue.py are not in a published snapshot (PUBLISH-EXCLUDE removes them)")
def test_every_check_a_root_read_rule_names_a_trunk_ci_test_that_exists():
    assert not _dangling_references(private_ci=True), _dangling_references(private_ci=True)


def test_the_check_scan_fires_on_a_planted_dangling_reference():
    """Negative control: the same pattern, run over a planted rule, finds the missing test."""
    planted = "- [ ] [X] a rule. The check: `tests/test_bounded_roots_docs.py::test_that_is_not_there`."
    hits = [(m.group(1), m.group(2)) for m in re.finditer(r"`(tests/[\w./]+\.py)::(\w+)`", planted)]
    assert hits and f"def {hits[0][1]}(" not in (REPO / hits[0][0]).read_text(encoding="utf-8")


# ---- the store's one lock, and the failed-record contract (rounds 3 and 4 of slice 4) ----------------------------------------------
_TABLES = {"_by_corpus_id", "_by_domain", "_order", "_pending_corpora", "_failed_corpora", "_no_roots_corpora"}
_UNSERVED = {"_pending_corpora", "_failed_corpora", "_no_roots_corpora"}
_MUTATORS = {"pop", "update", "clear", "setdefault", "append", "remove", "popitem", "__setitem__", "__delitem__", "extend", "insert", "sort", "reverse"}


def _attr_of(node, names):
    while isinstance(node, ast.Subscript):
        node = node.value
    return node.attr if isinstance(node, ast.Attribute) and node.attr in names else None


class _Scan(ast.NodeVisitor):
    """Every write to one of the store's tables, with the scope it sits in and whether it sits LEXICALLY inside a `with <x>._pending_cv:` block.
    Entering a def, a lambda or a class resets 'locked': code defined in a block runs later, outside it."""

    def __init__(self):
        self.writes = []                  # (scope, attribute, locked, line)
        self.stack = []
        self.locked = False

    def _scope(self):
        return ".".join(self.stack) or "<module>"

    def _enter(self, node, name):
        saved, self.locked = self.locked, False
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()
        self.locked = saved

    def visit_FunctionDef(self, n): self._enter(n, n.name)
    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, n): self._enter(n, n.name)

    def visit_Lambda(self, n): self._enter(n, "<lambda>")

    def visit_With(self, n):
        takes = any(isinstance(i.context_expr, ast.Attribute) and i.context_expr.attr == "_pending_cv" for i in n.items)
        saved = self.locked
        self.locked = self.locked or takes
        for item in n.items:
            self.visit(item)
        for b in n.body:
            self.visit(b)
        self.locked = saved

    def _note(self, target_or_call_base, node):
        a = _attr_of(target_or_call_base, _TABLES)
        if a:
            self.writes.append((self._scope(), a, self.locked, node.lineno))

    def visit_Assign(self, n):
        for t in n.targets:
            self._note(t, n)
        self.generic_visit(n)

    def visit_AugAssign(self, n):
        self._note(n.target, n)
        self.generic_visit(n)

    def visit_AnnAssign(self, n):
        self._note(n.target, n)
        self.generic_visit(n)

    def visit_Delete(self, n):
        for t in n.targets:
            self._note(t, n)
        self.generic_visit(n)

    def visit_Call(self, n):
        if isinstance(n.func, ast.Attribute) and n.func.attr in _MUTATORS:
            self._note(n.func.value, n)
        self.generic_visit(n)


def _table_writes(tree):
    sc = _Scan()
    sc.visit(tree)
    return sc.writes


def _rule_violations(sources):
    """`sources` = {relative path: source}. Violations: a write in store.py outside a `with ..._pending_cv` block (the constructor is exempt), and a
    write to a table in ANY other module, module level included."""
    bad = []
    for rel, src in sources.items():
        for scope, attr, locked, line in _table_writes(ast.parse(src)):
            if rel.endswith("factlattice_engine/store.py"):
                if not locked and "__init__" not in scope.split("."):
                    bad.append((rel, scope, attr, line, "outside the lock"))
            else:
                bad.append((rel, scope, attr, line, "outside the store"))
    return bad


def _repo_sources():
    out = {}
    for base in ("factlattice", "factlattice_engine"):
        for f in (REPO / base).rglob("*.py"):
            out[f.relative_to(REPO).as_posix()] = f.read_text(encoding="utf-8")
    return out


def test_every_writer_of_the_store_tables_is_inside_the_stores_one_lock_and_nothing_else_writes_them():
    bad = _rule_violations(_repo_sources())
    assert not bad, f"table writes outside the store's lock, or outside the store: {bad}"
    # NEGATIVE CONTROLS: each planted shape must be seen, or the scan has a hole
    store_src = (REPO / "factlattice_engine" / "store.py").read_text(encoding="utf-8")
    base = {"factlattice_engine/store.py": store_src}

    def planted(rel, src):
        return _rule_violations({**base, rel: src}) if rel != "factlattice_engine/store.py" else _rule_violations({rel: src})
    s1 = store_src.replace("                self._failed_corpora[corpus_id] = rec\n                self._pending_cv.notify_all()\n        return rec",
                           "                self._failed_corpora[corpus_id] = rec\n                self._pending_cv.notify_all()\n        self._failed_corpora[corpus_id] = dict(rec)\n        return rec", 1)
    assert s1 != store_src and any(b[4] == "outside the lock" for b in planted("factlattice_engine/store.py", s1)), "S1: a write AFTER the with block of a locked method"
    s2 = store_src.replace("            gone = self._pending_corpora.pop(corpus_id, None) is not None\n",
                           "            gone = self._pending_corpora.pop(corpus_id, None) is not None\n            threading.Timer(0.0, lambda: self._pending_corpora.clear()).start()\n", 1)
    assert s2 != store_src and any("<lambda>" in b[1] for b in planted("factlattice_engine/store.py", s2)), "S2: a lambda defined in a locked block"
    assert planted("factlattice/ledger.py", "def f(store):\n    store._failed_corpora['x'] = {}\n"), "S4: a write in the core package"
    assert planted("factlattice_engine/rootstate.py", "STORE = None\nif STORE is not None:\n    STORE._failed_corpora['x'] = {}\n"), "S5: a write at module level"
    assert planted("factlattice_engine/tools.py", "def f(self):\n    self.store._order = []\n"), "S8: the registry-order table"
    s7 = store_src.replace("        with self._pending_cv:\n            self._no_roots_corpora.clear()", "        if True:\n            self._no_roots_corpora.clear()", 1)
    assert s7 != store_src and planted("factlattice_engine/store.py", s7), "S7: an unlocked method"
    # STATED LIMITS, not covered: an alias (`fc = store._failed_corpora; fc[k] = v`) and `setattr`/`getattr`-by-string writes. A reviewer's eyes, not this scan.


_SERVED_READERS = {"corpus_ids", "partitions", "get_partition", "select", "filter_by_domain", "lookup", "get_node", "stats", "freshness"}
_SNAPSHOT_CALLS = {"snapshot", "snapshot_all", "tables", "lookup_all"}
_TABLE_OF_ATTR = {"_pending_corpora": "pending", "_failed_corpora": "failed", "_no_roots_corpora": "no_roots"}


def _table_reads(sources):
    """{(file, function): {"direct": Counter(table), "snap": {snapshot call names}}} for every function OUTSIDE the store.

    WHAT IT SEES (and nothing wider is claimed in SDLC [STORE-TABLES-ONE-SNAPSHOT-ONE-STEP]):
      * a direct read of `pending_corpora`, `failed_corpora` or `no_roots_corpora` (an attribute, or `getattr` / `hasattr` with the name as a string
        constant), and the engine's `self._pending()` helper (the pending table);
      * a call of a served-table reader (`corpus_ids`, `partitions`, `get_partition`, `select`, `filter_by_domain`, `lookup`, `get_node`, `stats`,
        `freshness`) on a receiver whose source text contains `store`;
      * a call of a snapshot method (`snapshot`, `snapshot_all`, `tables`, `lookup_all`) on a receiver whose text contains `store`.
    WHAT IT CANNOT SEE: an alias (`t = store._failed_corpora`); a table passed in as an argument; a receiver whose text does not contain `store`;
    a served reader not in the list above; a nested def or lambda is counted under its own name, not its parent's; two looks of the SAME snapshot
    method at different times (after a wait) are allowed, because a function may legitimately look again; a read inside `store.py` (the store's
    own methods are checked by the lock scan instead)."""
    import collections
    out = collections.defaultdict(lambda: {"direct": collections.Counter(), "snap": set()})
    for rel, src in sources.items():
        if rel == "factlattice_engine/store.py":
            continue

        def visit(n, stack):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                stack = stack + [n.name]
            if isinstance(n, ast.Lambda):
                stack = stack + ["<lambda>"]
            key = (rel, ".".join(stack) or "<module>")
            if isinstance(n, ast.Attribute) and n.attr in _TABLE_OF_ATTR:
                out[key]["direct"][_TABLE_OF_ATTR[n.attr]] += 1
            if isinstance(n, ast.Call):
                f = n.func
                if (isinstance(f, ast.Name) and f.id in ("getattr", "hasattr") and len(n.args) >= 2
                        and isinstance(n.args[1], ast.Constant) and n.args[1].value in _TABLE_OF_ATTR):
                    out[key]["direct"][_TABLE_OF_ATTR[n.args[1].value]] += 1
                if isinstance(f, ast.Attribute):
                    recv = ast.unparse(f.value)
                    if f.attr == "_pending":
                        out[key]["direct"]["pending"] += 1
                    elif "store" in recv and f.attr in _SERVED_READERS:
                        out[key]["direct"]["served"] += 1
                    elif "store" in recv and f.attr in _SNAPSHOT_CALLS:
                        out[key]["snap"].add(f.attr)
            for c in ast.iter_child_nodes(n):
                visit(c, stack)
        visit(ast.parse(src), [])
    return out


def _snapshot_rule_violations(sources):
    """Functions outside the store that break rule (1): they read two or more of the four tables (served, pending, failed, no_roots) themselves; or
    take a snapshot AND read a table directly (the snapshot is then not the one look); or take two DIFFERENT snapshot calls."""
    bad = []
    for key, v in sorted(_table_reads(sources).items()):
        if len(v["direct"]) >= 2 or (v["snap"] and v["direct"]) or len(v["snap"]) >= 2:
            bad.append((key, dict(v["direct"]), sorted(v["snap"])))
    return bad


_PAIR_PLANTS = {                                           # one per pair of the four tables that a function can read
    ("served", "pending"): "return store.partitions(), store._pending()",
    ("served", "failed"): "return store.corpus_ids(), store._failed_corpora",
    ("served", "no_roots"): "return store.get_partition('x'), store._no_roots_corpora",
    ("pending", "failed"): "return store._pending_corpora, store._failed_corpora",
    ("pending", "no_roots"): "return store._pending_corpora, getattr(store, '_no_roots_corpora', {})",
    ("failed", "no_roots"): "return store._failed_corpora, store._no_roots_corpora",
}


def test_no_function_outside_the_store_reads_two_of_the_four_tables_itself():
    assert _snapshot_rule_violations(_repo_sources()) == []
    # NEGATIVE CONTROLS: every pair of tables, planted under an arbitrary function name, is seen
    for pair, body in _PAIR_PLANTS.items():
        got = _snapshot_rule_violations({"factlattice_engine/x.py": f"def anything(store):\n    {body}\n"})
        assert [g[0] for g in got] == [("factlattice_engine/x.py", "anything")], (pair, got)
    # a snapshot followed by a direct read of the same table is the shape the listing had (`nodes_in_scope`)
    assert _snapshot_rule_violations({"factlattice_engine/x.py": "def f(store):\n    a = store.snapshot_all()\n    return a, store.partitions()\n"})
    assert _snapshot_rule_violations({"factlattice_engine/x.py": "def f(store):\n    return store.tables(), store.snapshot()\n"})
    # CONTROLS THAT MUST STAY CLEAN: the same table twice; one snapshot; the same snapshot call looked at again after a wait
    assert not _snapshot_rule_violations({"factlattice_engine/x.py": "def f(store):\n    return store._failed_corpora, store._failed_corpora\n"})
    assert not _snapshot_rule_violations({"factlattice_engine/x.py": "def f(store):\n    a = store.snapshot_all()\n    b = store.snapshot_all()\n    return a, b\n"})
    # STATED LIMIT, shown so it is not a surprise: an alias is not seen
    assert not _snapshot_rule_violations({"factlattice_engine/x.py": "def f(store):\n    a = store._failed_corpora\n    b = store\n    return a, b.partitions()\n"})


def test_sdlc_7_6_carries_the_store_lock_and_failed_record_rules():
    s76 = _section(_sdlc(), "### 7.6 FACTLATTICE-SPEC-CHECKLIST", "## 8. Stage 5")
    for rule in ("STORE-TABLES-ONE-LOCK", "FAILED-RECORD-CONTRACT", "LEDGER-TABLE-TWO-PHASE-PIN", "STORE-TABLES-ONE-SNAPSHOT-ONE-STEP", "ROUTED-STALE-FIELDS"):
        assert f"- [ ] [{rule}]" in s76, rule


# ======================================================================================================================
# round 7: the barrier is STRUCTURAL ([STORE-TABLES-ONE-SNAPSHOT-ONE-STEP]): the tables are private to the store, and a decision takes one look
# ======================================================================================================================
_SNAPSHOT_METHOD_NAMES = {"snapshot", "snapshot_all", "tables", "lookup_all"}
_PRIVATE_TABLE_NAMES = {"_by_corpus_id", "_by_domain", "_order", "_pending_corpora", "_failed_corpora", "_no_roots_corpora",
                        "pending_corpora", "failed_corpora", "no_roots_corpora"}      # the public spellings stay forbidden too: no alias, no property


def _private_table_references(sources):
    """[(file, line, name)] for every reference outside `store.py` to a store table. WHAT IT CHECKS: an attribute access (`x.NAME`, any receiver, read or
    write) and a `getattr` / `hasattr` / `setattr` / `delattr` whose second argument is the name as a string CONSTANT, in `factlattice/` and
    `factlattice_engine/`. Complete for attribute access. WHAT IT CANNOT SEE: a name built at run time (`getattr(o, '_' + 'failed_corpora')`), `vars()` /
    `__dict__` access, reflection through another library, and a dict KEY that merely spells the name (the reply field `failed_corpora` is such a key)."""
    out = []
    for rel, src in sources.items():
        if rel == "factlattice_engine/store.py":
            continue
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Attribute) and n.attr in _PRIVATE_TABLE_NAMES:
                out.append((rel, n.lineno, n.attr))
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("getattr", "hasattr", "setattr", "delattr")
                    and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant)
                    and n.args[1].value in (_PRIVATE_TABLE_NAMES | _SNAPSHOT_METHOD_NAMES)):
                # a table, or a snapshot method reached by NAME (`getattr(store, "snapshot_all")()`): it would hide a look from the look-taker scan
                out.append((rel, n.lineno, n.args[1].value))
    return out


def test_no_file_outside_the_store_references_a_store_table():
    assert _private_table_references(_repo_sources()) == []
    # NEGATIVE CONTROLS: each shape must be seen
    for body in ("return store._failed_corpora", "store._pending_corpora['x'] = 1", "return store.failed_corpora", "return store._by_corpus_id",
                 "return getattr(store, '_no_roots_corpora', {})", "return hasattr(store, 'pending_corpora')", "setattr(store, '_order', [])",
                 "return self.store._by_domain", "return getattr(store, 'snapshot_all')()", "return hasattr(store, 'tables')",
                 "return getattr(self.store, 'lookup_all', None)"):
        assert _private_table_references({"factlattice_engine/x.py": f"def f(store):\n    {body}\n"}), body
    # CONTROL THAT MUST STAY CLEAN: the reply field is a dict key, not an attribute (stated limit)
    assert not _private_table_references({"factlattice_engine/x.py": "def f(out):\n    return out['failed_corpora']\n"})


_SNAPSHOT_METHODS = {"snapshot", "snapshot_all", "tables", "lookup_all"}
_LOOK_PRIMITIVES = {"_look", "_view_look"}         # membership's two accessors of the call's look: `_look` wraps `snapshot_all`, `_view_look` reads the view's look (and takes one only for a hand-made view); callers of them are not counted as taking a second look
_PER_CALL_DECISIONS = {"_gate", "_registry_gate", "_await_loaded"}       # each is its own decision point (one corpus; a wait): a caller may call them after its look
_LOOK_ARGS = ("look", "snap", "unserved", "tables")                      # an argument (or keyword) whose name starts so hands the caller's look down


def _calls_of(fn_node):
    """The same-file names a function calls (`self.NAME(...)` or `NAME(...)`)."""
    names = set()
    for n in ast.walk(fn_node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "self":
                names.add(f.attr)
            elif isinstance(f, ast.Name):
                names.add(f.id)
    return names


def _hands_look(call):
    """True when a call hands the caller's look down (an argument or keyword named `look*`, `snap*`, `unserved*` or `tables*`, or a subscript of one)."""
    return (any(isinstance(a, ast.Name) and a.id.startswith(_LOOK_ARGS) for a in call.args)
            or any((k.arg or "").startswith(_LOOK_ARGS) for k in call.keywords)
            or any(isinstance(a, ast.Subscript) and isinstance(a.value, ast.Name) and a.value.id.startswith(_LOOK_ARGS) for a in call.args))


def _calls_without_look(fn_node):
    """The same-file helpers a function calls WITHOUT handing its look down, minus the per-call decision points and the look primitives."""
    names = set()
    for n in ast.walk(fn_node):
        if isinstance(n, ast.Call) and not _hands_look(n):
            f = n.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "self":
                names.add(f.attr)
            elif isinstance(f, ast.Name):
                names.add(f.id)
    return names - _PER_CALL_DECISIONS - _LOOK_PRIMITIVES


def _reaching(funcs, nohands, rel, out):
    """The keys of the functions that REACH a look: they take one (they are in `out`), or call, without handing a look down, a same-file helper that reaches one
    (to a fixed point). A function reaches through a helper that takes no look itself only by this: that is the chain through a middle helper."""
    reach = {k for k in funcs if (rel, f"{k[0]}.{k[1]}" if k[0] else k[1]) in out}
    grew = True
    while grew:
        grew = False
        names = {k[1] for k in reach}
        for k in funcs:
            if k not in reach and nohands[k] & names:
                reach.add(k)
                grew = True
    return reach


def _add_chains(rel, tree, out):
    """CHAINS (to a fixed point): a function that calls TWO OR MORE different look-taking helpers of its file, or helpers that REACH a look (`_reaching`),
    takes two looks in one decision without calling the store itself, so it is a look-taker too. The per-call decision points are not counted."""
    funcs = {}
    for cls in [x for x in ast.walk(tree) if isinstance(x, (ast.ClassDef, ast.Module))]:
        for f in cls.body:
            if isinstance(f, ast.FunctionDef):
                funcs[(getattr(cls, "name", None), f.name)] = f
    nohands = {k: _calls_without_look(f) for k, f in funcs.items()}
    changed = True
    while changed:
        changed = False
        taker_names = {q.split(".")[-1] for (r, q) in out if r == rel} - _PER_CALL_DECISIONS - _LOOK_PRIMITIVES
        reach_names = {k[1] for k in _reaching(funcs, nohands, rel, out)}
        for (cn, fn), f in funcs.items():
            qual = f"{cn}.{fn}" if cn else fn
            if (rel, qual) in out:
                continue
            reaching = (_calls_of(f) & taker_names) | {nm for nm in nohands[(cn, fn)] if nm in reach_names and nm != fn}
            if len(reaching) >= 2:
                out[(rel, qual)] = ["<chain>"]
                changed = True


def _look_takers(sources):
    """{(file, qualified function): [snapshot methods it calls]} for functions OUTSIDE the store that call a snapshot method on a receiver whose source
    text contains `store`, or `_look(...)` (membership's single wrapper of `snapshot_all`)."""
    out = {}
    for rel, src in sources.items():
        if rel == "factlattice_engine/store.py":
            continue

        def visit(n, stack):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                stack = stack + [n.name]
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Attribute) and f.attr in _SNAPSHOT_METHODS and "store" in ast.unparse(f.value):
                    out.setdefault((rel, ".".join(stack) or "<module>"), []).append(f.attr)
                if isinstance(f, ast.Name) and f.id == "_look":
                    out.setdefault((rel, ".".join(stack) or "<module>"), []).append("_look")
            for c in ast.iter_child_nodes(n):
                visit(c, stack)
        tree = ast.parse(src)
        visit(tree, [])
        _add_chains(rel, tree, out)
    return out


def _helper_mediated_looks(sources):
    """[(file, function, helper, reaches)] for a function that takes a look AND calls a same-file helper (`self.NAME(...)` or `NAME(...)`) that,
    directly or through further such helpers, takes a look of its own or reads a served table, WITHOUT handing its look down (no argument or keyword
    named `look*`, `snap*`, `unserved*` or `tables*`). The per-call decision points `_gate`, `_registry_gate` and `_await_loaded` are not followed.
    WHAT IT CANNOT SEE: a helper in another file (`self.router.route_edge`: the router takes its served list as an argument now, and
    `test_the_router_takes_its_partitions_from_the_caller` pins that), a helper reached through an attribute that is not `self`, a callback."""
    import collections
    res = []
    takers = _look_takers(sources)
    for rel, src in sources.items():
        if rel == "factlattice_engine/store.py":
            continue
        tree = ast.parse(src)
        funcs = {}
        for cls in [n for n in ast.walk(tree) if isinstance(n, (ast.ClassDef, ast.Module))]:
            for f in cls.body:
                if isinstance(f, ast.FunctionDef):
                    funcs[(getattr(cls, "name", "<mod>"), f.name)] = f
        info = {}
        for (cn, fn), f in funcs.items():
            reads, calls = 0, []
            for n in ast.walk(f):
                if isinstance(n, ast.Call):
                    fu = n.func
                    if isinstance(fu, ast.Attribute):
                        recv = ast.unparse(fu.value)
                        if "store" in recv and fu.attr in _SERVED_READERS:
                            reads += 1
                        elif recv == "self":
                            hands = (any(isinstance(a, ast.Name) and a.id.startswith(_LOOK_ARGS) for a in n.args)
                                     or any((k.arg or "").startswith(_LOOK_ARGS) for k in n.keywords)
                                     or any(isinstance(a, ast.Subscript) and isinstance(a.value, ast.Name) and a.value.id.startswith(_LOOK_ARGS) for a in n.args))
                            calls.append((fu.attr, hands))
                    elif isinstance(fu, ast.Name):
                        calls.append((fu.id, any(isinstance(a, ast.Name) and a.id.startswith(_LOOK_ARGS) for a in n.args)))
            info[(cn, fn)] = (reads, calls)
        byname = collections.defaultdict(list)
        for k in info:
            byname[k[1]].append(k)

        def qual(k):
            return ".".join(x for x in k if x != "<mod>")

        def reaches(k, seen=()):
            reads, calls = info[k]
            out = set()
            if (rel, qual(k)) in takers or reads:
                out.add(k)
            for name, hands in calls:
                if hands or name in _PER_CALL_DECISIONS or name in _LOOK_PRIMITIVES:
                    continue
                for k2 in byname.get(name, []):
                    if k2 not in seen and k2 != k:
                        out |= reaches(k2, seen + (k,))
            return out
        for k, (reads, calls) in info.items():
            if (rel, qual(k)) in takers or reads:
                for name, hands in calls:
                    if hands or name in _PER_CALL_DECISIONS or name in _LOOK_PRIMITIVES:
                        continue
                    for k2 in byname.get(name, []):
                        r = reaches(k2)
                        if r:
                            res.append((rel, qual(k), name, sorted(qual(x) for x in r)))
    return res


_SMALLER_FORM = {          # the sites where a function that took a look calls a helper that takes its own: the SMALLER FORM, each with its reason (not the rule's full form)
    ('factlattice_engine/bootload.py', 'StoreLoader._join_bounded', '_failed'): 'loader-thread step: the move (`settle`) is one store step; the release of its root slots is bookkeeping after it, not a reply',
    ('factlattice_engine/bootload.py', 'StoreLoader._load_one', '_release'): 'loader-thread step (the entry-shape branch): the corpus STAYS PENDING for the registry\'s verdict (marked held in one store step); giving back its root slots is bookkeeping after it, not a reply',
    ('factlattice_engine/bootload.py', 'StoreLoader._load_one', '_failed'): 'loader-thread step: the move (`settle`) is one store step; the release of its root slots is bookkeeping after it, not a reply',
    ('factlattice_engine/bootload.py', 'StoreLoader._load_one', '_load_locked'): 'loader-thread step: the move is one store step; the release of root slots is bookkeeping after it, not a reply',
    ('factlattice_engine/bootload.py', 'StoreLoader._load_locked', '_failed'): 'CHAIN (the function takes no look itself; its look is in a helper). Loader-thread step: the move (`settle`) is one store step; the release of its root slots (the look inside `_release`) is bookkeeping after it, not a reply',
    ('factlattice_engine/bootload.py', 'StoreLoader._load_locked', '_release'): 'CHAIN. Loader-thread step (the corpus-with-no-nodes branch): the move (`settle`) is one store step; the release of its root slots, whose look is `_release`\'s own, is bookkeeping after it, not a reply',
    ('factlattice_engine/bootload.py', 'StoreLoader._load_locked', '_stuck_now'): 'CHAIN. Loader-thread step: a root that does not answer either keeps the corpus pending (no look) or fails it through `_failed` (one store step, then the root-slot bookkeeping); the function returns at once after it, so no reply holds two looks',
    ('factlattice_engine/server.py', 'build_tools', '_build_tools_async'): "CHAIN. The async branch RETURNS before any root is read: its looks (the registry-error verdict and the profile announcement) are taken later, on the loader's thread at the end of the first pass, which is a different decision point from this call; the two branches of `build_tools` never both run",
    ('factlattice_engine/server.py', 'build_tools', '_none_loaded_error'): "CHAIN. The blocking branch (the async branch has returned): the ONE look of this boot, taken after the store's roots were read, to decide the registry-error verdict once. After it, `_announce_profile_split(store)` and `kernel.seat_store(store)` read the store through helpers in other files (outside this scan); neither is part of the verdict this look decides, and no reply is built from both",
    ('factlattice_engine/reviewer/__main__.py', '_main', '_failed_container_stop'): 'the reviewer CLI: single thread, no moves',
    ('factlattice_engine/tools.py', 'LtsEngine._find_exact', '_extent'): "the reply's DECLARATION look (what was consulted, skipped and available), taken once inside the extent for all its fields; the data the reply holds was gathered under its own looks",
    ('factlattice_engine/tools.py', 'LtsEngine._find_exact', '_fanout_parts'): 'data-gathering step that waits for pending corpora and looks again by design (a new look after a wait)',
    ('factlattice_engine/tools.py', 'LtsEngine._find_exact', '_reverse_digest'): 'data-gathering step: its referrers are named from its own one look',
    ('factlattice_engine/tools.py', 'LtsEngine._reverse_digest', '_reverse_index_for'): 'data-gathering step that waits for pending corpora and looks again by design (a new look after a wait)',
    ('factlattice_engine/tools.py', 'LtsEngine.capabilities', '_extent'): "the reply's DECLARATION look (what was consulted, skipped and available), taken once inside the extent for all its fields; the data the reply holds was gathered under its own looks",
    ('factlattice_engine/tools.py', 'LtsEngine.find_in_prose', '_extent'): "the reply's DECLARATION look (what was consulted, skipped and available), taken once inside the extent for all its fields; the data the reply holds was gathered under its own looks",
    ('factlattice_engine/tools.py', 'LtsEngine.find_in_prose', '_fanout_parts'): 'data-gathering step that waits for pending corpora and looks again by design (a new look after a wait)',
    ('factlattice_engine/tools.py', 'LtsEngine.node', '_extent'): "the reply's DECLARATION look (what was consulted, skipped and available), taken once inside the extent for all its fields; the data the reply holds was gathered under its own looks",
    ('factlattice_engine/tools.py', 'LtsEngine.node', '_reverse_digest'): 'data-gathering step: its referrers are named from its own one look',
    ('factlattice_engine/tools.py', 'LtsEngine.report_miss', '_miss_append'): "CHAIN. The miss log's guard look (are the registered corpus roots known, so the log can be checked to sit outside every one), taken once at the append after the evidence was gathered; it is a guard on a write, not data the reply holds (the report's own ids come from the `_gate` call)",
    ('factlattice_engine/tools.py', 'LtsEngine.report_miss', '_miss_exact_evidence'): 'CHAIN. A nested tool call (`find`) made by a helper of report_miss, which is its own call with its own looks',
    ('factlattice_engine/tools.py', 'LtsEngine.report_miss', '_miss_semantic_evidence'): 'CHAIN. A nested tool call (`semantic_find`) made by a helper of report_miss, which is its own call with its own looks',
    ('factlattice_engine/tools.py', 'LtsEngine.reverse', '_extent'): "the reply's DECLARATION look (what was consulted, skipped and available), taken once inside the extent for all its fields; the data the reply holds was gathered under its own looks",
    ('factlattice_engine/tools.py', 'LtsEngine.reverse', '_reverse_index_for'): 'data-gathering step that waits for pending corpora and looks again by design (a new look after a wait)',
    ('factlattice_engine/tools.py', 'LtsEngine.traverse', '_extent'): "the reply's DECLARATION look (what was consulted, skipped and available), taken once inside the extent for all its fields; the data the reply holds was gathered under its own looks",
}
_LOOK_TAKERS = {                       # every function outside the store that takes a look: each is a decision point ("entry point"); a new one is a decision
    ('factlattice_engine/bootload.py', 'StoreLoader._join_bounded'),
    ('factlattice_engine/bootload.py', 'StoreLoader._load_locked'),      # CHAIN: no look of its own; reaches one through `_failed`, `_release`, `_stuck_now`
    ('factlattice_engine/bootload.py', 'StoreLoader._load_one'),
    ('factlattice_engine/bootload.py', 'StoreLoader._release'),
    ('factlattice_engine/bootload.py', 'StoreLoader._retry_stuck_roots'),
    ('factlattice_engine/membership.py', '_look'),
    ('factlattice_engine/membership.py', '_view_look'),
    ('factlattice_engine/membership.py', 'snapshot'),
    ('factlattice_engine/readprofile.py', '_live_corpus_nodes'),
    ('factlattice_engine/reviewer/__main__.py', '_failed_container_stop'),
    ('factlattice_engine/reviewer/__main__.py', '_main.resolve'),
    ('factlattice_engine/server.py', '_none_loaded_error'),
    ('factlattice_engine/server.py', 'build_tools'),      # CHAIN: no look of its own; reaches one through `_build_tools_async` and `_none_loaded_error` (two branches)
    ('factlattice_engine/tools.py', 'LtsEngine._attach_failed'),
    ('factlattice_engine/tools.py', 'LtsEngine._await_loaded'),
    ('factlattice_engine/tools.py', 'LtsEngine._await_loaded.stuck'),
    ('factlattice_engine/tools.py', 'LtsEngine._declare_search_scope'),
    ('factlattice_engine/tools.py', 'LtsEngine._extent'),
    ('factlattice_engine/tools.py', 'LtsEngine._fanout_parts'),
    ('factlattice_engine/tools.py', 'LtsEngine._find_exact'),
    ('factlattice_engine/tools.py', 'LtsEngine._gate'),
    ('factlattice_engine/tools.py', 'LtsEngine._reverse_digest'),
    ('factlattice_engine/tools.py', 'LtsEngine._reverse_index_snapshot'),
    ('factlattice_engine/tools.py', 'LtsEngine._search_scope'),
    ('factlattice_engine/tools.py', 'LtsEngine.capabilities'),
    ('factlattice_engine/tools.py', 'LtsEngine.find_in_prose'),
    ('factlattice_engine/tools.py', 'LtsEngine.list_corpora'),
    ('factlattice_engine/tools.py', 'LtsEngine.node'),
    ('factlattice_engine/tools.py', 'LtsEngine.register_corpus'),
    ('factlattice_engine/tools.py', 'LtsEngine._miss_append'),
    ('factlattice_engine/tools.py', 'LtsEngine.report_miss'),      # CHAIN: no look of its own; reaches one through `_miss_append`, `_miss_exact_evidence`, `_miss_semantic_evidence`
    ('factlattice_engine/tools.py', 'LtsEngine.reverse'),
    ('factlattice_engine/tools.py', 'LtsEngine.traverse'),
    ('factlattice_engine/tools.py', 'LtsEngine.unregister_corpus'),
    ('factlattice_engine/tools.py', '_dispatch'),
}


def test_only_listed_entry_points_take_a_look_and_none_calls_a_helper_that_takes_another():
    found = _look_takers(_repo_sources())
    assert set(found) == set(_LOOK_TAKERS), ("a function outside the store started or stopped taking a look: list it as an entry point, or pass the look "
                                             "it was given", sorted(set(found) ^ set(_LOOK_TAKERS)))
    mediated = {(r[0], r[1], r[2]) for r in _helper_mediated_looks(_repo_sources())}
    assert mediated == set(_SMALLER_FORM), ("a helper-mediated second look appeared or went away: fix it (hand the look down) or list it with its reason", sorted(mediated ^ set(_SMALLER_FORM)))
    # NEGATIVE CONTROLS: the shapes of G1 (helper that takes its own look), G2/G3 (helper that reads the served table), and the handed-down look that is fine
    g1 = "class E:\n    def gate(self):\n        look = self.store.snapshot_all()\n        return self.public()\n    def public(self):\n        return self.store.snapshot_all()\n"
    assert _helper_mediated_looks({"factlattice_engine/x.py": g1})
    g3 = "class E:\n    def a(self):\n        look = self.store.snapshot_all()\n        return self.mark()\n    def mark(self):\n        return self.store.get_partition('x')\n"
    assert _helper_mediated_looks({"factlattice_engine/x.py": g3})
    deep = "class E:\n    def a(self):\n        look = self.store.snapshot_all()\n        return self.b()\n    def b(self):\n        return self.c()\n    def c(self):\n        return self.store.partitions()\n"
    assert _helper_mediated_looks({"factlattice_engine/x.py": deep}), "reached through a second helper"
    chain = ("class E:\n    def a(self):\n        return self.b(), self.c()\n    def b(self):\n        return self.store.snapshot_all()\n"
             "    def c(self):\n        return self.store.tables()\n")
    got = _look_takers({"factlattice_engine/x.py": chain})
    assert ("factlattice_engine/x.py", "E.a") in got and got[("factlattice_engine/x.py", "E.a")] == ["<chain>"], got      # F5: two look-taking helpers = a look-taker
    one = "class E:\n    def a(self):\n        return self.b(), self.b()\n    def b(self):\n        return self.store.snapshot_all()\n"
    assert ("factlattice_engine/x.py", "E.a") not in _look_takers({"factlattice_engine/x.py": one}), "the same helper twice is one"
    deep2 = ("class E:\n    def a(self):\n        return self.m(), self.n()\n    def m(self):\n        return self.b(), self.c()\n    def n(self):\n        return self.d(), self.c()\n"
             "    def b(self):\n        return self.store.snapshot_all()\n    def c(self):\n        return self.store.tables()\n    def d(self):\n        return self.store.lookup_all('x')\n")
    assert ("factlattice_engine/x.py", "E.a") in _look_takers({"factlattice_engine/x.py": deep2}), "chains of chains reach a fixed point"
    fine = "class E:\n    def a(self):\n        look = self.store.snapshot_all()\n        return self.b(look)\n    def b(self, look):\n        return look[0]\n"
    assert not _helper_mediated_looks({"factlattice_engine/x.py": fine})
    assert _look_takers({"factlattice_engine/x.py": "def f(store):\n    return store.tables()\n"}) == {("factlattice_engine/x.py", "f"): ["tables"]}


def test_a_chain_through_a_helper_that_takes_no_look_is_a_look_taker():
    """The look-taker scan follows a function to a look through helpers that take none themselves (review F1 of the report_miss refactor)."""
    f = "factlattice_engine/x.py"
    # the planted violation: `entry` takes no look and calls no look-taker directly; `mid` takes none either and reaches `_fanout_parts` (a look-taker)
    planted = ("class E:\n    def entry(self):\n        return self.mid(), self.other()\n"
               "    def mid(self):\n        return self._fanout_parts()\n"
               "    def other(self):\n        return self.store.snapshot_all()\n"
               "    def _fanout_parts(self):\n        return self.store.snapshot_all()\n")
    got = _look_takers({f: planted})
    assert got.get((f, "E.entry")) == ["<chain>"], got
    assert (f, "E.mid") not in got, "the middle helper takes no look itself and is not listed"
    # deeper: the middle helper reaches the look through a second helper
    deeper = ("class E:\n    def entry(self):\n        return self.mid(), self.other()\n"
              "    def mid(self):\n        return self.mid2()\n    def mid2(self):\n        return self._fanout_parts()\n"
              "    def other(self):\n        return self.store.tables()\n"
              "    def _fanout_parts(self):\n        return self.store.snapshot_all()\n")
    assert _look_takers({f: deeper}).get((f, "E.entry")) == ["<chain>"]
    # a chain of chains reaches a fixed point: the caller of a chain-taker and another helper that reaches a look
    nested = planted + "    def top(self):\n        return self.entry(), self.mid()\n"
    assert _look_takers({f: nested}).get((f, "E.top")) == ["<chain>"]
    # CONTROLS THAT MUST STAY CLEAN: one helper reached twice (one look); a helper that reaches no look; a look handed down; the per-call decision points
    one = ("class E:\n    def entry(self):\n        return self.mid(), self.mid()\n    def mid(self):\n        return self._fanout_parts()\n"
           "    def _fanout_parts(self):\n        return self.store.snapshot_all()\n")
    assert (f, "E.entry") not in _look_takers({f: one})
    none = "class E:\n    def entry(self):\n        return self.mid(), self.pure()\n    def mid(self):\n        return self.pure()\n    def pure(self):\n        return 1\n"
    assert not _look_takers({f: none})
    # a look handed down: BOTH helpers reach a look, but `mid` is given the caller's look, so only `other` counts and the
    # caller is not listed. Without the handed-down exemption the caller would be listed (two helpers): this control can fail.
    handed = ("class E:\n    def entry(self, look):\n        return self.mid(look), self.other()\n    def mid(self, look):\n        return self._fanout_parts()\n"
              "    def other(self):\n        return self.store.tables()\n    def _fanout_parts(self):\n        return self.store.snapshot_all()\n")
    assert (f, "E.entry") not in _look_takers({f: handed})
    unhanded = handed.replace("self.mid(look)", "self.mid()")
    assert _look_takers({f: unhanded}).get((f, "E.entry")) == ["<chain>"], "the control's twin without the handed look must be listed"
    gate = ("class E:\n    def entry(self):\n        return self.mid(), self._gate()\n    def mid(self):\n        return self._gate()\n"
            "    def _gate(self):\n        return self.store.snapshot_all()\n")
    assert (f, "E.entry") not in _look_takers({f: gate}), "the per-call decision points are not followed"
    # and a chain-taker that is itself a caller of a look-taker is seen as helper-mediated (the entries of _SMALLER_FORM)
    med = {(r[1], r[2]) for r in _helper_mediated_looks({f: planted})}
    assert ("E.entry", "mid") in med and ("E.entry", "other") in med, med
