"""[UNKNOWN-ARGUMENT-REFUSED] the other half of the rule: every argument name a tool's HANDLER reads is declared in
that tool's advertised inputSchema.

`dispatch` refuses names the schema does not declare. If a handler also reads a name the schema omits, that feature
becomes unreachable through every door (and was undiscoverable before). This test walks the handler code statically
(`tools._dispatch` and the helpers that receive its argument dict), attributes each read of the argument dict
(`a.get("k")`, `a["k"]`, `"k" in a`, `a.pop("k")`, and comprehensions over a literal tuple or `_AGG_FILTER_KEYS`) to the
tool(s) of the enclosing `if name == ...` / `if name in ...` branch, and diffs it with `server.TOOLS`.

An access it cannot read (a variable key) FAILS rather than being skipped, and so does a new helper that takes the
argument dict and is not mapped here: a check that can silently see nothing is not a check.

WHAT IT DOES NOT SEE (measured by planting reads): an alias of the argument dict (`b = a`), a function nested in
`_dispatch`, `for k in a`, `dict(a)`, `**a`, `a.setdefault`, `a.items()`, and a helper whose parameter is not named
`a` or `arguments`. None exists today; SDLC 7.6 [UNKNOWN-ARGUMENT-REFUSED] forbids writing handler code in those forms.
Waivers are keyed by (tool, key), so a later real read of a waived key is waived too.
"""
from __future__ import annotations

import ast
from pathlib import Path

from factlattice_engine import tools
from factlattice_engine.server import TOOLS

SRC = Path(tools.__file__).read_text(encoding="utf-8")
TREE = ast.parse(SRC)
SCHEMA = {t["name"]: set(t["inputSchema"].get("properties", {})) for t in TOOLS}
ALL = set(SCHEMA)
ARG_NAMES = ("a", "arguments")

# helpers that receive the argument dict, with the tools whose arguments they read. Anything else that takes a
# parameter called `a` / `arguments` must be listed in IGNORED with a reason, or the test fails.
WRITERS = {n for n, p in SCHEMA.items() if "via" in p}
HELPERS = {
    "_run_aggregate": {"lts_aggregate"},
    "_ledger_args": WRITERS,
    "_attach_failed": {"lts_aggregate", "lts_profile_audit"},
    "_search_scope": {"lts_find", "lts_find_in_prose"},
    "_mode_began_meanwhile": {"lts_find"},
}
IGNORED = {"dispatch": "the wrapper: passes the dict on", "_refuse_unknown_arguments": "the check itself",
           "_registry_gate": "reads no argument name (a comment names one)"}


def _resolve_names(node):
    """The tool names a branch test restricts to, or None when the test says nothing about `name`."""
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        out = None
        for v in node.values:
            r = _resolve_names(v)
            if r is not None:
                out = r if out is None else out & r
        return out
    if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "name" \
            and len(node.ops) == 1:
        op, comp = node.ops[0], node.comparators[0]
        if isinstance(op, ast.Eq) and isinstance(comp, ast.Constant):
            return {comp.value}
        if isinstance(op, ast.NotEq) and isinstance(comp, ast.Constant):
            return ALL - {comp.value}
        if isinstance(op, ast.In):
            if isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                return {e.value for e in comp.elts}
            if isinstance(comp, ast.Name):
                return set(getattr(tools, comp.id))
    return None


def _consts_outside_tests(node):
    """String constants in `node`, not those inside an `if` TEST of a conditional expression."""
    if isinstance(node, ast.IfExp):
        yield from _consts_outside_tests(node.body)
        yield from _consts_outside_tests(node.orelse)
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node
    for ch in ast.iter_child_nodes(node):
        yield from _consts_outside_tests(ch)


class _Reads:
    def __init__(self):
        self.reads = []          # (tool-set, key, lineno)
        self.unreadable = []     # (lineno, source)

    def key(self, scope, node, key_node):
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            self.reads.append((frozenset(scope), key_node.value, node.lineno))
        else:
            self.unreadable.append((node.lineno, ast.unparse(node)))

    def expr(self, scope, node):
        for n in ast.walk(node):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("get", "pop") \
                    and isinstance(n.func.value, ast.Name) and n.func.value.id in ARG_NAMES and n.args:
                if isinstance(n.args[0], ast.Name) and self._loop_keys(node, n.args[0].id) is not None:
                    for k in self._loop_keys(node, n.args[0].id):
                        self.reads.append((frozenset(scope), k, n.lineno))
                else:
                    self.key(scope, n, n.args[0])
            elif isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id in ARG_NAMES:
                sl = n.slice
                if isinstance(sl, ast.Name) and self._loop_keys(node, sl.id) is not None:
                    for k in self._loop_keys(node, sl.id):
                        self.reads.append((frozenset(scope), k, n.lineno))
                else:
                    self.key(scope, n, sl)
            elif isinstance(n, ast.Compare) and len(n.ops) == 1 and isinstance(n.ops[0], (ast.In, ast.NotIn)) \
                    and isinstance(n.comparators[0], ast.Name) and n.comparators[0].id in ARG_NAMES:
                if isinstance(n.left, ast.Name) and self._loop_keys(node, n.left.id) is not None:
                    for k in self._loop_keys(node, n.left.id):
                        self.reads.append((frozenset(scope), k, n.lineno))
                else:
                    self.key(scope, n, n.left)

    @staticmethod
    def _loop_keys(root, var):
        """The literal keys a comprehension variable `var` ranges over (a tuple of constants, or `_AGG_FILTER_KEYS`)."""
        for n in ast.walk(root):
            if isinstance(n, (ast.DictComp, ast.ListComp, ast.SetComp, ast.GeneratorExp)):
                for g in n.generators:
                    if isinstance(g.target, ast.Name) and g.target.id == var:
                        it = g.iter
                        if isinstance(it, ast.Name) and it.id == "_AGG_FILTER_KEYS":
                            return list(tools._AGG_FILTER_KEYS)
                        if isinstance(it, ast.Attribute) and it.attr == "NEW_PARAMS":
                            return list(tools._qs.NEW_PARAMS)
                        if isinstance(it, (ast.Tuple, ast.List)) and all(isinstance(e, ast.Constant) for e in it.elts):
                            return [e.value for e in it.elts]
                        if isinstance(it, ast.Name):            # a local tuple: every string constant in its assignment
                            for a_ in ast.walk(TREE):
                                if isinstance(a_, ast.Assign) and any(isinstance(t_, ast.Name) and t_.id == it.id
                                                                      for t_ in a_.targets):
                                    ks = [c.value for c in _consts_outside_tests(a_.value)]
                                    if ks:
                                        return ks
        return None

    def stmts(self, scope, body):
        scope = set(scope)
        for st in body:
            if isinstance(st, ast.If):
                r = _resolve_names(st.test)
                inner = (scope & r) if r is not None else scope
                self.expr(inner, st.test)
                self.stmts(inner, st.body)
                self.stmts(scope, st.orelse)
                if r is not None and isinstance(st.test, ast.Compare) and st.body and isinstance(st.body[-1], (ast.Return, ast.Raise)):
                    scope = scope - r        # that branch always leaves: what FOLLOWS in this block is for the other tools
            elif isinstance(st, (ast.Try,)):
                self.stmts(scope, st.body)
                for h in st.handlers:
                    self.stmts(scope, h.body)
                self.stmts(scope, st.orelse)
                self.stmts(scope, st.finalbody)
            elif isinstance(st, (ast.With, ast.For, ast.While)):
                self.stmts(scope, st.body)
            elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            else:
                self.expr(scope, st)


def _functions():
    return {n.name: n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)}


# (tool, name) pairs read by a helper SHARED with a tool that does not declare the name. Each needs a reason.
WAIVED = {("lts_remove_property", "new_key"): "`need = (...) + ((\"new_key\",) if name == \"lts_rename_property\" else ())`: the walk reads the conditional tuple as one",
          ("lts_find_in_prose", "path"): "`_search_scope` is shared with lts_find, which declares `path`; find_in_prose has none, so it is always absent there"}


def _collect():
    fns = _functions()
    rd = _Reads()
    rd.stmts(set(ALL), fns["_dispatch"].body)
    for name, scope in HELPERS.items():
        rd.stmts(set(scope), fns[name].body)
    return rd


def test_every_function_taking_the_argument_dict_is_accounted_for():
    for name, fn in _functions().items():
        params = [p.arg for p in fn.args.args + fn.args.kwonlyargs]
        if any(p in ARG_NAMES for p in params):
            assert name in HELPERS or name in IGNORED or name == "_dispatch", \
                f"{name} takes the argument dict: map its tools in HELPERS (or IGNORED with a reason)"


def test_no_handler_read_is_unreadable():
    rd = _collect()
    assert not rd.unreadable, f"variable-key reads the walk cannot attribute: {rd.unreadable}"


def test_the_walk_sees_reads_at_all():
    """Negative control: the instrument can report something. A walk that found nothing would pass the real test."""
    rd = _collect()
    by_tool = {t: {k for s, k, _ in rd.reads if t in s} for t in ALL}
    assert {"corpus", "prefix", "text", "tier"} <= by_tool["lts_find"], by_tool["lts_find"]
    assert {"source", "paths", "dangle"} <= by_tool["lts_lint"]
    assert {"roots", "by_month"} <= by_tool["lts_property_loss"]
    assert {"since", "until", "where"} <= by_tool["lts_profile_audit"]       # through the `_AGG_FILTER_KEYS` comprehension
    assert {"proposer", "via"} <= by_tool["lts_add_node"]                    # through `_ledger_args`
    assert sum(len(v) for v in by_tool.values()) > 150


def test_every_name_a_handler_reads_is_declared_in_its_schema():
    rd = _collect()
    missing = {}
    for scope, key, line in rd.reads:
        for tool in scope:
            if key not in SCHEMA[tool] and (tool, key) not in WAIVED:
                missing.setdefault(tool, {}).setdefault(key, line)
    assert not missing, f"handlers read names their schema does not declare (tool: {{name: line}}): {missing}"
