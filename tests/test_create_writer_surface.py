"""[A-E1] the create path through the three doors: the library receipt, the MCP door (`Server.handle`)
and the command-line door (`call ... --allow-write`). `call` still requires --allow-write for it."""
from __future__ import annotations

import errno
import json
import os

import pytest

from factlattice import ledger
from factlattice_engine import server
from tests.test_cli_call import call_json, server_door, world  # noqa: F401


def _args(path, **kw):
    return {"path": str(path), "node_id": "REVIEW-K9", "title": "Review K9",
            "properties": {"verdict": "in-progress"}, "create": True, "doc_title": "t",
            "domain": "reviews", "doctype": "review", **kw}


@pytest.fixture()
def out(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    (tmp_path / "reviews").mkdir()
    return tmp_path / "reviews"


def test_the_schema_advertises_the_new_arguments():
    spec = next(t for t in server.TOOLS if t["name"] == "lts_add_node")
    props = spec["inputSchema"]["properties"]
    for k in ("create", "create_parent", "doc_title", "domain", "doctype"):
        assert k in props, k
    assert props["create"]["type"] == "boolean" and "create" not in spec["inputSchema"]["required"]


def test_mcp_door_creates_and_receipts_it(world, out):
    payload, is_err = server_door("lts_add_node", _args(out / "sub" / "r.lts", create_parent=True))
    assert not is_err and payload["success"] is True and payload["written"] is True
    assert payload["created"] is True and payload["created_parent"] == os.path.realpath(out / "sub")
    assert payload["ledger"]["status"] == "ok" and payload["backup_path"] is None
    assert len(ledger.read_lines(os.path.realpath(out / "sub" / "r.lts"))) == 1


def test_full_receipt_report_says_created_and_no_backup(world, out):
    payload, _ = server_door("lts_add_node", _args(out / "r.lts", receipt="full"))
    assert "CREATED the file" in payload["report"] and "backup None" not in payload["report"]


def test_refusal_is_a_named_json_refusal_with_a_code_and_writes_nothing(world, out):
    (out / "r.lts").write_text("# x [SPEC:X-ONE]\n")
    payload, _ = server_door("lts_add_node", _args(out / "r.lts"))
    assert payload["success"] is False and payload["refused"] is True and payload["written"] is False
    assert payload["evidence"]["refusal"] == "create_file_exists"
    assert (out / "r.lts").read_text() == "# x [SPEC:X-ONE]\n"


def test_create_keys_are_ABSENT_when_create_was_not_used(world, out):
    f = out / "r.lts"
    server_door("lts_add_node", _args(f))
    payload, _ = server_door("lts_add_node", {"path": str(f), "node_id": "REVFINDING-K9-1",
                                              "title": "F", "properties": {"kind": "finding"}})
    assert payload["success"] is True and "created" not in payload and "created_parent" not in payload


def test_call_door_requires_allow_write_for_a_create(world, out, capsys):
    f = out / "r.lts"
    rc, env, _ = call_json("lts_add_node", _args(f), capsys)
    assert rc != 0 and env["refused"] is True and "--allow-write" in env["error"] and not f.exists()
    rc, env, _ = call_json("lts_add_node", _args(f), capsys, "--allow-write")
    assert rc == 0 and f.is_file()
    assert env["result"]["created"] is True


def test_call_door_names_the_refusal_not_an_AttributeError_on_a_fresh_process_first_call(world, out, capsys):
    """F-A1 through the door: a fresh `call` is always a thread's first writer call."""
    rc, env, _ = call_json("lts_add_node", {"path": str(out / "nope.lts"), "node_id": "X-ONE",
                                            "title": "t", "properties": {"kind": "a"}},
                           capsys, "--allow-write")
    r = env["result"] if "result" in env else env
    assert r["refused"] is True and "is not a file" in r["reason"]
    assert "AttributeError" not in json.dumps(env)


# ===================================================== post-review P1-1: the flags go through exactly as given
def test_create_parent_WITHOUT_create_is_refused_at_the_mcp_door_and_writes_nothing(world, out):
    f = out / "sub" / "r.lts"
    payload, _ = server_door("lts_add_node", {"path": str(f), "node_id": "REVIEW-K9", "title": "t",
                                              "properties": {"kind": "review"}, "create_parent": True})
    assert payload["success"] is False and payload["refused"] is True and payload["written"] is False
    assert payload["evidence"]["refusal"] == "create_parent_needs_create"
    assert not (out / "sub").exists() and not f.exists()
    assert "created" not in payload


def test_create_parent_WITHOUT_create_is_refused_through_dispatch_and_call(world, out, capsys):
    from factlattice_engine.tools import dispatch
    f = out / "sub2" / "r.lts"
    args = {"path": str(f), "node_id": "REVIEW-K9", "title": "t", "properties": {"kind": "review"},
            "create_parent": True}
    r = dispatch("lts_add_node", args, None)
    assert r["refused"] is True and r["evidence"]["refusal"] == "create_parent_needs_create"
    rc, env, _ = call_json("lts_add_node", args, capsys, "--allow-write")
    res = env["result"] if "result" in env else env
    assert res["refused"] is True and res["evidence"]["refusal"] == "create_parent_needs_create"
    assert not (out / "sub2").exists()


def test_create_alone_on_a_missing_parent_is_refused_at_every_door(world, out, capsys):
    from factlattice_engine.tools import dispatch
    f = out / "sub3" / "r.lts"
    args = {"path": str(f), "node_id": "REVIEW-K9", "title": "t", "properties": {"kind": "review"},
            "create": True}
    payload, _ = server_door("lts_add_node", args)
    assert payload["refused"] is True and payload["evidence"]["refusal"] == "create_parent_missing"
    assert dispatch("lts_add_node", args, None)["evidence"]["refusal"] == "create_parent_missing"
    rc, env, _ = call_json("lts_add_node", args, capsys, "--allow-write")
    res = env["result"] if "result" in env else env
    assert res["evidence"]["refusal"] == "create_parent_missing" and not (out / "sub3").exists()


# ===================================================== post-delta-review P2-B: the TOOL layer refuses with a code too
def _tool(args):
    from factlattice_engine.tools import dispatch
    return dispatch("lts_add_node", args, None)


def _base(out, **kw):
    return {"path": str(out / "r.lts"), "node_id": "REVIEW-K9", "title": "t",
            "properties": {"kind": "review"}, "create": True, **kw}


BAD_ARGS = {
    "properties not a dict": dict(properties=["kind", "review"]),
    "property key not a string": dict(properties={1: "x"}),
    "path not a string": dict(path=12345),
    "NUL in path": dict(path="/tmp/a\x00b.lts"),
    "level not an int": dict(level="x"),
    "node_id not a string": dict(node_id=7),
    "section not a string": dict(section=5, create=True),
    "doc_title not a string": dict(doc_title=3),
}


@pytest.mark.parametrize("name", list(BAD_ARGS))
def test_wrong_typed_create_arguments_are_a_coded_refusal_and_write_nothing(world, out, name):
    r = _tool(_base(out, **BAD_ARGS[name]))
    assert r.get("refused") is True and r.get("success") is False, (name, r)
    assert r["evidence"]["refusal"] == "create_argument_invalid", (name, r)
    assert not (out / "r.lts").exists()


def test_wrong_typed_create_arguments_through_the_mcp_and_call_doors(world, out, capsys):
    a = _base(out, properties=["x"])
    payload, _ = server_door("lts_add_node", a)
    assert payload["refused"] is True and payload["evidence"]["refusal"] == "create_argument_invalid"
    rc, env, _ = call_json("lts_add_node", a, capsys, "--allow-write")
    res = env["result"] if "result" in env else env
    assert res["refused"] is True and res["evidence"]["refusal"] == "create_argument_invalid"


@pytest.mark.parametrize("err,name", [(errno.ENAMETOOLONG, "ENAMETOOLONG"), (errno.EACCES, "EACCES"), (errno.ENOSPC, "ENOSPC")])
def test_an_OSError_on_create_is_a_coded_refusal_with_the_errno_name_and_no_path(world, out, monkeypatch, err, name):
    from factlattice_engine import authoring
    def boom(*a, **k):
        raise OSError(err, os.strerror(err), str(out / "PRIVATE-SECRET-DIR" / "r.lts"))
    monkeypatch.setattr(authoring, "_add_node", boom)
    r = _tool(_base(out))
    assert r["refused"] is True and r["evidence"]["refusal"] == "create_os_error"
    assert r["evidence"]["errno"] == name
    assert "PRIVATE-SECRET-DIR" not in json.dumps(r)        # the exception's own path never reaches the reply
    assert r["reason"] == f"the create failed with {name}. Nothing of this create is at the path; no ledger record of it."


def test_a_really_too_long_path_is_refused_with_a_code(world, out):
    r = _tool(_base(out, path=str(out / ("x" * 400) / "r.lts"), create_parent=True))
    assert r["refused"] is True and r["evidence"].get("refusal"), r


def test_no_uncoded_refusal_in_the_tool_layer_add_node():
    """Static: every EditRefused built in authoring.add_node, and in its validator, names a code."""
    import ast, inspect, textwrap
    from factlattice_engine import authoring
    total = 0
    for fn in (authoring.add_node, authoring._add_node_impl, authoring._decide_create_outcome,
               authoring._create_arg_problem):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "EditRefused":
                total += 1
                assert any(k.arg == "evidence" for k in node.keywords), f"{fn.__name__}: uncoded EditRefused line {node.lineno}"
    assert total >= 3


# ===================================================== delta2: "every create refusal is coded" BY CONSTRUCTION
def _fresh(tmp_path_factory_dir):
    d = tmp_path_factory_dir
    d.mkdir(exist_ok=True)
    return d


LEDGER_ARGS = ["proposer", "approver", "reason", "via"]


@pytest.mark.parametrize("name", LEDGER_ARGS)
@pytest.mark.parametrize("bad", [5, ["x"], {"a": 1}, True])
def test_F1_wrong_typed_ledger_arguments_are_a_coded_refusal(world, out, name, bad):
    r = _tool(_base(out, **{name: bad}))
    assert r.get("refused") is True and r["evidence"]["refusal"] == "create_argument_invalid", (name, bad, r)
    assert not (out / "r.lts").exists()


SURROGATE = "ab\ud800cd"


@pytest.mark.parametrize("kw", [
    dict(path=SURROGATE), dict(title=SURROGATE), dict(node_id=SURROGATE), dict(section=SURROGATE),
    dict(doc_title=SURROGATE), dict(domain=SURROGATE), dict(properties={"kind": SURROGATE}),
    dict(properties={SURROGATE: "v"}), dict(path="~nosuchuser-xyz/x.lts"), dict(level=10 ** 30),
    dict(path=b"bytes.lts"), dict(receipt=object()), dict(create_parent=object()),
])
def test_F2_odd_values_are_refused_with_a_code_and_write_nothing(world, out, kw):
    before = sorted(os.listdir(out))
    r = _tool(_base(out, **kw))
    assert r.get("success") is not True or r.get("created") is True, r            # a success is a real create
    if r.get("success") is not True:
        assert r.get("refused") is True and r["evidence"].get("refusal"), (kw, r)
        assert sorted(os.listdir(out)) == before
        assert "Traceback" not in json.dumps(r, default=str)


def test_F2_the_catch_all_names_the_type_only_and_no_message(world, out, monkeypatch):
    from factlattice_engine import authoring
    def boom(*a, **k):
        raise RuntimeError("secret-path /Users/you/PRIVATE")
    monkeypatch.setattr(authoring, "_add_node", boom)
    r = _tool(_base(out))
    assert r["refused"] is True and r["evidence"] == {"refusal": "create_unexpected_error", "exception": "RuntimeError"}
    assert "PRIVATE" not in json.dumps(r) and not (out / "r.lts").exists()


def test_F2_a_receipt_failure_AFTER_the_write_never_claims_a_refusal(world, out, monkeypatch):
    from factlattice_engine import authoring
    def boom(*a, **k):
        raise RuntimeError("late secret")
    monkeypatch.setattr(authoring._binding, "analyse_many", boom)
    r = _tool(_base(out))
    assert (out / "r.lts").is_file()                                      # it WAS written
    assert r["success"] is True and r["written"] is True and r["refused"] is False
    assert r["receipt"] == "incomplete" and r["receipt_error"] == "RuntimeError"
    assert "late secret" not in json.dumps(r)


def test_F2_calls_without_create_are_not_wrapped(world, out, monkeypatch):
    from factlattice_engine import authoring
    def boom(*a, **k):
        raise RuntimeError("raw")
    monkeypatch.setattr(authoring, "_add_node", boom)
    r = _tool({"path": str(out / "x.lts"), "node_id": "X-ONE", "title": "t", "properties": {"k": "v"}})
    assert r.get("refused") is not True and r["success"] is False and "RuntimeError" in r["error"]


def test_F2_a_create_with_a_required_argument_missing_is_a_coded_refusal(world, out):
    r = _tool({"create": True, "title": "t"})
    assert r["refused"] is True and r["evidence"]["refusal"] == "create_argument_invalid"


def test_F5_a_pathlib_path_is_accepted_for_create(world, out):
    from factlattice_engine import authoring
    r = authoring.add_node(out / "pl.lts", "REVIEW-K9", "t", {"kind": "review"}, create=True,
                           doc_title="t", domain="reviews", doctype="review")
    assert r["success"] is True and r["created"] is True and (out / "pl.lts").is_file()


# ------------------------------------------------------------------ F3: the fuzz
FUZZ_VALUES = [None, "", " ", "x" * 3000, 0, 1, -1, 10 ** 30, 1.5, True, False, [], [1], {}, {"a": 1},
               {1: "x"}, "\ud800", "a\x00b", "~nosuchuser-xyz/x.lts", "\n", "..", "a/../../b", b"b",
               object(), ("t",)]
PARAMS = ["path", "node_id", "title", "properties", "section", "level", "receipt", "proposer", "approver",
          "reason", "via", "create_parent", "doc_title", "domain", "doctype"]


def _listing(root):
    return sorted(os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs + [""])


def test_F3_fuzz_every_parameter_every_odd_value_never_escapes_uncoded_and_never_writes_on_refusal(world, tmp_path):
    """Drive create through dispatch for EVERY parameter x a generated set of wrong types and odd values.
    Each result is a real success, or `refused: true` WITH an `evidence.refusal` code and a tree that is
    byte-for-byte what it was. (`create`/`create_parent` only coerce to bool, so they are fuzzed too.)"""
    n = bad = 0
    problems = []
    for i, param in enumerate(PARAMS + ["create"]):
        for j, v in enumerate(FUZZ_VALUES):
            root = tmp_path / f"fz{i}_{j}"
            root.mkdir()
            args = {"path": str(root / "r.lts"), "node_id": "REVIEW-K9", "title": "t",
                    "properties": {"kind": "review"}, "create": True}
            args[param] = v
            if param == "create" and not v:
                continue            # a falsy `create` IS a non-create call: not a create refusal
            before = _listing(root)
            try:
                r = _tool(args)
            except Exception as e:                                   # nothing may escape dispatch
                problems.append((param, repr(v)[:30], f"ESCAPED {type(e).__name__}"))
                continue
            n += 1
            if r.get("success") is True:
                continue
            bad += 1
            if r.get("refused") is not True or not (r.get("evidence") or {}).get("refusal"):
                problems.append((param, repr(v)[:30], "uncoded", {k: str(r.get(k))[:60] for k in ("refused", "error", "evidence")}))
            elif _listing(root) != before:
                problems.append((param, repr(v)[:30], "WROTE on refusal"))
    assert n > 300 and bad > 50, (n, bad)                              # the fuzz really ran and really refused
    assert not problems, problems[:10]


def test_the_tool_text_states_the_three_outcomes_ledger_state_and_link_unsupported():
    spec = next(t for t in server.TOOLS if t["name"] == "lts_add_node")
    d = spec["inputSchema"]["properties"]["create"]["description"]
    for needle in ("SUCCESS", "REFUSED", "unknown", "ledger_state", "re-read the path before retrying",
                   "a retry never overwrites", "create_link_unsupported", "leftover_temp"):
        assert needle in d, needle
