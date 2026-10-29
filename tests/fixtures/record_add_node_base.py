"""Records the replies of ORDINARY (non-create) lts_add_node calls through `tools.dispatch`, normalised, as a JSON
fixture. Run against the BASE tree: `python3 tests/fixtures/record_add_node_base.py <out.json>` with that tree
first on sys.path. The pin test (tests/test_add_node_noncreate_pin.py) replays the same calls on the tree under
test and compares field by field. `outcome: "unknown"` exists ONLY on the create path; these must not move."""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

DOC = """# Doc [SPEC:PIN-ROOT]

- kind: root

## Node one [PIN-ONE]

- kind: x
- status: draft

## Node two [PIN-TWO]

- kind: y
"""


def cases(root: Path):
    f = root / "c" / "p.lts"
    def a(**kw):
        return {"path": str(f), **kw}
    return [
        ("ok_compact", a(node_id="PIN-NEW", title="New", properties={"kind": "z", "note": "n"})),
        ("ok_full", a(node_id="PIN-NEW2", title="New two", properties={"kind": "z"}, receipt="full")),
        ("ok_in_section", a(node_id="PIN-NEW3", title="Three", properties={"kind": "z"}, section="PIN-ONE")),
        ("ok_level", a(node_id="PIN-NEW4", title="Four", properties={"kind": "z"}, level=3)),
        ("ok_ledger_meta", a(node_id="PIN-NEW5", title="Five", properties={"kind": "z"}, proposer="p", reason="r")),
        ("ok_edge_key", a(node_id="PIN-NEW6", title="Six", properties={"see-also": "[PIN-ONE]"})),
        ("dup_id", a(node_id="PIN-ONE", title="Dup", properties={"kind": "z"})),
        ("bad_node_id", a(node_id="lower", title="t", properties={"kind": "z"})),
        ("bad_key", a(node_id="PIN-X1", title="t", properties={"1bad key": "v"})),
        ("empty_value", a(node_id="PIN-X2", title="t", properties={"kind": " "})),
        ("multiline_value", a(node_id="PIN-X3", title="t", properties={"kind": "a\nb"})),
        ("bad_section", a(node_id="PIN-X4", title="t", properties={"kind": "z"}, section="NOPE")),
        ("missing_file", {"path": str(root / "c" / "absent.lts"), "node_id": "PIN-X5", "title": "t",
                          "properties": {"kind": "z"}}),
        ("create_false_explicit", a(node_id="PIN-X6", title="t", properties={"kind": "z"}, create=False)),
        ("properties_wrong_type", a(node_id="PIN-X7", title="t", properties=["kind", "z"])),
        ("node_id_wrong_type", a(node_id=7, title="t", properties={"kind": "z"})),
        ("level_wrong_type", a(node_id="PIN-X8", title="t", properties={"kind": "z"}, level="x")),
        ("path_wrong_type", {"path": 12345, "node_id": "PIN-X9", "title": "t", "properties": {"kind": "z"}}),
        ("missing_required", {"title": "t"}),
        ("bad_via", a(node_id="PIN-X10", title="t", properties={"kind": "z"}, via="nope")),
        ("long_reason", a(node_id="PIN-X11", title="t", properties={"kind": "z"}, reason="r" * 3000)),
    ]


def norm(o, root):
    s = json.dumps(o, sort_keys=True, default=str)
    s = s.replace(os.path.realpath(root), "<ROOT>").replace(str(root), "<ROOT>")
    s = re.sub(r"\d{8}T\d{6}\.\d+\.[0-9a-f]{8}(\.\d+)?", "<STAMP>", s)
    s = re.sub(r"ledger/[0-9a-f]{32}\.jsonl", "ledger/<KEY>.jsonl", s)
    s = re.sub(r'"ts": "[^"]+"', '"ts": "<TS>"', s)
    s = re.sub(r'"elapsed_ms": [0-9.]+', '"elapsed_ms": 0', s)
    return _drop_env(json.loads(s))


def _drop_env(o):
    """Registry-dependent text (which corpora exist on THIS machine) is not the writer's behaviour."""
    if isinstance(o, dict):
        return {k: _drop_env(v) for k, v in o.items() if k not in ("root_membership", "roots_checked")}
    if isinstance(o, list):
        return [_drop_env(v) for v in o if not (isinstance(v, str) and "OUTSIDE EVERY REGISTERED CORPUS" in v)]
    if isinstance(o, str) and "OUTSIDE EVERY REGISTERED CORPUS" in o:
        return o.split("OUTSIDE EVERY REGISTERED CORPUS")[0]
    return o


def run(root: Path):
    from factlattice_engine.tools import dispatch
    (root / "c").mkdir()
    (root / "c" / "p.lts").write_text(DOC, encoding="utf-8")
    out = {}
    for name, args in cases(root):
        try:
            r = dispatch("lts_add_node", args, None)
        except Exception as e:  # noqa: BLE001
            r = {"ESCAPED": type(e).__name__}
        out[name] = norm(r, root)
    # the same file after all of that, so a changed write is caught too
    out["_final_file"] = (root / "c" / "p.lts").read_text(encoding="utf-8")
    # an OS error and a raw exception from the core, non-create: today's replies
    import factlattice_engine.authoring as au
    real = au._add_node
    for kind, exc in (("os_error", OSError(28, "No space left")), ("runtime", RuntimeError("boom"))):
        au._add_node = lambda *a, _e=exc, **k: (_ for _ in ()).throw(_e)
        try:
            out["core_" + kind] = norm(dispatch("lts_add_node", {"path": str(root / "c" / "p.lts"), "node_id": "PIN-Y1",
                                                                  "title": "t", "properties": {"kind": "z"}}, None), root)
        finally:
            au._add_node = real
    return out


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp())
    os.environ["FACTLATTICE_STATE_DIR"] = str(tmp / "state")
    os.environ["FACTLATTICE_BACKUP_DIR"] = str(tmp / "bak")
    os.environ["FACTLATTICE_LEDGER_EPHEMERAL_ROOTS"] = ""
    res = run(tmp)
    Path(sys.argv[1]).write_text(json.dumps(res, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    print(len(res), "recorded")
