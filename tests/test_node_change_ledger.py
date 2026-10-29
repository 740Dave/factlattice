"""[NODE-CHANGE-LEDGER] v1 -- every applied typed write appends ONE hash-chained record, inside the
shared per-file lock; `lts_history` reads it back and declares absence.

Spec: SPEC-node-change-ledger-v1-2026-10-03 (the architect).  Tests L1-L9 + the negative control.

The ledger lives OUTSIDE factlattice/authoring (factlattice/ledger.py) so that
tests/test_atomic_write_round2.py::test_no_authoring_module_writes_a_corpus_file_directly stays
unmodified and as strict as it was.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from factlattice import ledger
from factlattice.authoring import edit
from factlattice.authoring.edit import (EditRefused, add_node, repair_property, set_property,
                                        write_whole_file)
from factlattice.parser import parse_text

REPO = str(Path(__file__).resolve().parents[1])
ROUNDS = int(os.environ.get("FL_LEDGER_ROUNDS", "30"))

DOC = """# Doc [SPEC:LG-ROOT]

- kind: root

## Node one [LG-ONE]

- kind: x
- status: draft
- ⛔ FLAGGED 2026-08-10: a broken line to repair
- alias: first

## Node two [LG-TWO]

- kind: y
"""

RECORD_FIELDS = {"v", "ts", "ledger_seq", "path_real", "verb", "node_id", "key", "delta", "discarded",
                 "node_hash_before", "node_hash_after", "file_sha256_before", "file_sha256_after",
                 "proposer", "approver", "reason", "via", "corpora", "engine_head", "prev_sha256",
                 "backup_path", "inserted", "corpora_error",
                 "follows_out_of_band_edit", "prior_blob", "undo_evidence_missing"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    (tmp_path / "corpus").mkdir()
    f = tmp_path / "corpus" / "c.lts"
    f.write_text(DOC, encoding="utf-8")
    return f


def _lines(f: Path) -> list[bytes]:
    p = ledger.ledger_path(os.path.realpath(f))
    return p.read_bytes().splitlines(keepends=True) if p.exists() else []


def _recs(f: Path) -> list[dict]:
    return [json.loads(x) for x in _lines(f)]


def _verify_chain(raw: list[bytes]) -> list[str]:
    """The test-side verifier (selfcheck's sweep is step 2): returns problems, [] == intact."""
    bad, prev = [], None
    for i, line in enumerate(raw, 1):
        r = json.loads(line)
        if r["ledger_seq"] != i:
            bad.append(f"seq gap at line {i}: {r['ledger_seq']}")
        if r["prev_sha256"] != prev:
            bad.append(f"chain break at seq {r['ledger_seq']}")
        prev = hashlib.sha256(line).hexdigest()
    return bad


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _node_hash(path: Path, nid: str):
    for n in parse_text(path.read_text(encoding="utf-8"), str(path)).nodes:
        if n.id == nid:
            return n.content_hash
    return None


# ------------------------------------------------------------------------------------- L1
def test_L1_set_property_replace_records_every_field(env):
    f = env
    sha0, nh0 = _sha(f), _node_hash(f, "LG-ONE")
    r = set_property(f, "LG-ONE", "status", "active", proposer="agent-a", approver="lead",
                     reason="because", via="direct", corpora=["c"], engine_head="sha256:abc")
    (rec,) = _recs(f)
    assert set(rec) == RECORD_FIELDS
    assert rec["v"] == 1 and rec["ledger_seq"] == 1 and rec["prev_sha256"] is None
    assert rec["path_real"] == os.path.realpath(f) and rec["verb"] == "set_property"
    assert (rec["node_id"], rec["key"]) == ("LG-ONE", "status")
    assert rec["delta"] == r.delta
    assert rec["discarded"] and "draft" in json.dumps(rec["discarded"])
    assert rec["file_sha256_before"] == sha0 and rec["file_sha256_after"] == _sha(f)
    assert rec["node_hash_before"] == nh0 and rec["node_hash_after"] == _node_hash(f, "LG-ONE")
    assert rec["node_hash_before"] != rec["node_hash_after"]
    assert (rec["proposer"], rec["approver"], rec["reason"], rec["via"]) == ("agent-a", "lead", "because", "direct")
    assert rec["corpora"] == ["c"] and rec["engine_head"] == "sha256:abc"
    assert r.ledger["status"] == "ok" and r.ledger["seq"] == 1
    assert r.ledger["file"] == str(ledger.ledger_path(os.path.realpath(f)))


def test_L1_defaults_are_null_and_via_is_direct(env):
    set_property(env, "LG-ONE", "extra", "v")
    (rec,) = _recs(env)
    assert rec["proposer"] is rec["approver"] is rec["reason"] is None
    assert rec["corpora"] is None and rec["engine_head"] is None and rec["via"] == "direct"
    assert rec["discarded"] == [] and rec["node_hash_before"] is not None


def test_L1_append_verb(env):
    r = set_property(env, "LG-ONE", "alias", "second", mode="append")
    (rec,) = _recs(env)
    assert rec["verb"] == "append" and rec["delta"] == r.delta and rec["discarded"] == []


def test_L1_repair_verb(env):
    r = repair_property(env, "LG-ONE", 9, "flagged")
    (rec,) = _recs(env)
    assert rec["verb"] == "repair_property" and rec["key"] == "flagged" and rec["delta"] == r.delta
    assert rec["discarded"] and "a broken line to repair" in json.dumps(rec["discarded"])


def test_L1_add_node_verb(env):
    r = add_node(env, "LG-NEW", "New node", {"kind": "z"})
    (rec,) = _recs(env)
    assert rec["verb"] == "add_node" and rec["node_id"] == "LG-NEW" and rec["key"] is None
    assert rec["node_hash_before"] is None and rec["node_hash_after"] == _node_hash(env, "LG-NEW")
    assert rec["delta"] == r.delta and rec["discarded"] == []


def test_L1_write_whole_file_records_write_document(env):
    f = env
    sha0 = _sha(f)
    new = DOC.replace("- kind: y", "- kind: y\n- extra: e").replace("- alias: first\n", "")
    write_whole_file(f, new)
    (rec,) = _recs(f)
    assert set(rec) == RECORD_FIELDS
    assert rec["verb"] == "write_document" and rec["node_id"] is None and rec["key"] is None
    assert rec["file_sha256_before"] == sha0 and rec["file_sha256_after"] == _sha(f)
    assert set(rec["delta"]) == {"LG-ONE", "LG-TWO"}            # the nodes that moved
    assert edit.last_ledger()["status"] == "ok"


def test_L1_write_whole_file_new_file(env, tmp_path):
    p = tmp_path / "corpus" / "fresh.lts"
    write_whole_file(p, "# Fresh [SPEC:FR-ROOT]\n\n- kind: root\n")
    (rec,) = _recs(p)
    assert rec["file_sha256_before"] is None and rec["verb"] == "write_document"


def test_L1_unchanged_receipt_shape_still_holds(env):
    """Receipts are otherwise untouched: the existing fields keep their values."""
    r = set_property(env, "LG-ONE", "status", "active")
    assert r.action == "replaced" and r.key == "status" and r.nodes_checked == 3
    assert r.delta["LG-ONE"]["changed"]["status"] == {"removed": ["draft"], "added": ["active"]}


# ------------------------------------------------------------------------------------- L2
def test_L2_gate_refusal_appends_nothing(env):
    with pytest.raises(EditRefused):
        set_property(env, "NO-SUCH-NODE", "k", "v")
    with pytest.raises(EditRefused):
        set_property(env, "LG-ONE", "k", "multi\nline")
    assert _recs(env) == []
    set_property(env, "LG-ONE", "k", "v")                      # and a good write after is seq 1
    assert [r["ledger_seq"] for r in _recs(env)] == [1]


def test_L2_cas_refusal_appends_nothing(env, monkeypatch):
    def hand_edit(real):
        Path(real).write_text(Path(real).read_text() + "\nhand edit\n")
    monkeypatch.setattr(edit, "_BEFORE_REPLACE_HOOK", hand_edit)
    with pytest.raises(EditRefused, match="changed on disk"):
        set_property(env, "LG-ONE", "k", "v")
    assert _recs(env) == []


def test_L2_lock_timeout_appends_nothing(env, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_WRITE_LOCK_TIMEOUT", "0.2")
    with edit._file_lock(env):
        with pytest.raises(EditRefused, match="busy"):
            set_property(env, "LG-ONE", "k", "v")
    assert _recs(env) == []


def test_L2_bounds_refuse_before_any_write(env):
    before = env.read_bytes()
    for kw in ({"reason": "x" * 2001}, {"proposer": "x" * 201}, {"approver": "x" * 201}):
        with pytest.raises(EditRefused, match="Nothing written"):
            set_property(env, "LG-ONE", "k", "v", **kw)
    assert env.read_bytes() == before and _recs(env) == []
    set_property(env, "LG-ONE", "k", "v", reason="x" * 2000, proposer="p" * 200, approver="a" * 200)
    assert len(_recs(env)) == 1                                  # the bounds are inclusive


# ------------------------------------------------------------------------------------- L3
def test_L3_chain_of_five_and_tamper_detection(env):
    for i in range(5):
        set_property(env, "LG-ONE", f"k{i}", f"v{i}")
    raw = _lines(env)
    assert [json.loads(x)["ledger_seq"] for x in raw] == [1, 2, 3, 4, 5]
    assert json.loads(raw[0])["prev_sha256"] is None
    for i in range(1, 5):
        assert json.loads(raw[i])["prev_sha256"] == hashlib.sha256(raw[i - 1]).hexdigest()
    assert _verify_chain(raw) == []
    tampered = list(raw)
    rec = json.loads(tampered[2])
    rec["reason"] = "forged"
    tampered[2] = (json.dumps(rec) + "\n").encode()
    problems = _verify_chain(tampered)
    assert problems and "seq 4" in problems[0]                   # the NEXT record's prev no longer matches


# ------------------------------------------------------------------------------------- L4
def _undo(f: Path, rec: dict) -> None:
    lines = f.read_text(encoding="utf-8").split("\n")
    for d in rec["discarded"]:
        lines[d["line_number"] - 1] = d["line"]
    f.write_text("\n".join(lines), encoding="utf-8")


@pytest.mark.parametrize("op", ["replace", "repair"])
def test_L4_undo_from_the_record_alone(env, op):
    if op == "replace":
        set_property(env, "LG-ONE", "status", "active")
    else:
        repair_property(env, "LG-ONE", 9, "flagged")
    (rec,) = _recs(env)
    assert _sha(env) == rec["file_sha256_after"] != rec["file_sha256_before"]
    _undo(env, rec)
    assert _sha(env) == rec["file_sha256_before"]


# ------------------------------------------------------------------------------------- L5
_CHILD = textwrap.dedent("""
    import json, sys, time
    sys.path.insert(0, {repo!r})
    from factlattice.authoring.edit import set_property, EditRefused
    path, i, start = sys.argv[1], sys.argv[2], float(sys.argv[3])
    while time.time() < start:
        pass
    try:
        set_property(path, "LG-ONE", "k" + i, "v" + i)
        print("ok")
    except EditRefused:
        print("refused")
""")


def test_L5_concurrent_writers_leave_a_contiguous_intact_chain(tmp_path):
    total_ok = 0
    for rnd in range(ROUNDS):
        d = tmp_path / f"r{rnd}"
        (d / "corpus").mkdir(parents=True)
        f = d / "corpus" / "c.lts"
        f.write_text(DOC, encoding="utf-8")
        e = dict(os.environ, FACTLATTICE_BACKUP_DIR=str(d / "bak"), FACTLATTICE_STATE_DIR=str(d / "state"))
        start = time.time() + 1.0
        procs = [subprocess.Popen([sys.executable, "-c", _CHILD.format(repo=REPO), str(f), str(i), str(start)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
                 for i in range(8)]
        ok = sum(p.communicate(timeout=90)[0].strip().splitlines()[-1] == "ok" for p in procs)
        total_ok += ok
        lp = Path(e["FACTLATTICE_STATE_DIR"]) / "ledger"
        files = list(lp.glob("*.jsonl"))
        assert len(files) == 1, (rnd, files)
        raw = files[0].read_bytes().splitlines(keepends=True)
        assert len(raw) == ok, f"round {rnd}: {ok} writes landed, {len(raw)} ledger records"
        assert _verify_chain(raw) == [], f"round {rnd}"
        assert sum(f"- k{i}: v{i}" in f.read_text() for i in range(8)) == ok
    assert total_ok >= ROUNDS                                    # the test really wrote


# ------------------------------------------------------------------------------------- L6
def test_L6_unwritable_ledger_dir_is_loud_not_silent(env, tmp_path):
    state = tmp_path / "state" / "ledger"
    state.mkdir(parents=True)
    os.chmod(state, 0o500)
    try:
        r = set_property(env, "LG-ONE", "k", "v")
    finally:
        os.chmod(state, 0o700)
    assert "- k: v" in env.read_text()                          # the write LANDED
    assert r.ledger["status"] == "failed" and r.ledger["reason"]


def test_L6_adapter_report_leads_with_written_but_not_ledgered(env, tmp_path):
    from factlattice_engine import authoring
    state = tmp_path / "state" / "ledger"
    state.mkdir(parents=True)
    os.chmod(state, 0o500)
    try:
        full = authoring.add_property(str(env), "LG-ONE", "k", "v", receipt="full")
        compact = authoring.add_property(str(env), "LG-ONE", "k2", "v2")
    finally:
        os.chmod(state, 0o700)
    assert full["written"] is True and full["ledger"]["status"] == "failed"
    assert full["report"].startswith("WRITTEN, BUT NOT LEDGERED")
    assert compact["ledger"]["status"] == "failed"
    assert any("WRITTEN, BUT NOT LEDGERED" in w for w in compact["warnings"])


def test_ledger_dir_and_file_permissions(env):
    set_property(env, "LG-ONE", "k", "v")
    p = ledger.ledger_path(os.path.realpath(env))
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700
    assert p.name == hashlib.sha256(os.path.realpath(env).encode()).hexdigest()[:32] + ".jsonl"


# ------------------------------------------------------------------------------------- L7
def _engine(tmp_path):
    from factlattice_engine import config
    from factlattice_engine.tools import LtsEngine
    reg = {"c": {"roots": [{"path": str(tmp_path / "corpus"), "area": "specs"}], "domain_suffix": "proj.c"}}
    return LtsEngine(config.build_store(reg))


def test_L7_history_newest_first_reconciled_then_not(env, tmp_path):
    from factlattice_engine.tools import dispatch
    for i in range(3):
        set_property(env, "LG-ONE", f"k{i}", f"v{i}", reason=f"r{i}")
    set_property(env, "LG-TWO", "other", "o")
    e = _engine(tmp_path)                                       # seated AFTER the writes (no healer in tests)
    h = dispatch("lts_history", {"corpus": "c", "node_id": "LG-ONE"}, tools=e)
    assert h["success"] is True and h["ledger"] == "present"
    assert [x["reason"] for x in h["records"]] == ["r2", "r1", "r0"]       # newest first, this node only
    assert h["reconciled"] is True and h["history_complete"] is False       # before ledger_since is unknown
    assert h["ledger_since"] == _recs(env)[0]["ts"]
    assert h["extent"]["records_scanned"] == 4 and h["extent"]["ledger_file"]
    env.write_text(env.read_text() + "\nhand edit\n")
    e = _engine(tmp_path)
    h2 = dispatch("lts_history", {"corpus": "c", "node_id": "LG-ONE"}, tools=e)
    assert h2["reconciled"] is False
    assert dispatch("lts_history", {"corpus": "c", "node_id": "LG-ONE", "limit": 1}, tools=e)["count"] == 1


def test_L7_no_ledger_is_declared_not_implied(env, tmp_path):
    from factlattice_engine.tools import dispatch
    e = _engine(tmp_path)
    h = dispatch("lts_history", {"corpus": "c", "node_id": "LG-ONE"}, tools=e)
    assert h["success"] is True and h["ledger"] == "none" and h["history_complete"] is False
    assert h["records"] == [] and h["ledger_since"] is None


def test_L7_unknown_node_and_corpus_fail_loud(env, tmp_path):
    from factlattice_engine.tools import dispatch
    e = _engine(tmp_path)
    assert dispatch("lts_history", {"corpus": "c", "node_id": "NOPE"}, tools=e).get("success") is False
    assert dispatch("lts_history", {"corpus": "zzz", "node_id": "LG-ONE"}, tools=e).get("success") is False


def test_L7_history_is_logged_like_a_read_tool(env, tmp_path, monkeypatch):
    from factlattice_engine import querylog
    from factlattice_engine.tools import dispatch
    log = tmp_path / "q.jsonl"
    monkeypatch.setenv(querylog.ENV_LOG, str(log))
    dispatch("lts_history", {"corpus": "c", "node_id": "LG-ONE"}, tools=_engine(tmp_path))
    recs = [json.loads(x) for x in log.read_text().splitlines()]
    assert recs[-1]["tool"] == "lts_history" and "LG-ONE" in recs[-1]["result_ids"]


def test_L7_tool_is_in_the_surface_and_schemas_thread_the_new_kwargs():
    from factlattice_engine.server import TOOLS
    from factlattice_engine.tools import LTS_ALL_TOOL_NAMES
    by = {t["name"]: t for t in TOOLS}
    assert "lts_history" in LTS_ALL_TOOL_NAMES and "lts_history" in by
    for n in ("lts_add_property", "lts_add_node", "lts_repair_property"):
        props = by[n]["inputSchema"]["properties"]
        for k in ("proposer", "approver", "reason", "via"):
            assert k in props and props[k].get("description"), (n, k)


# ------------------------------------------------------------------------------------- L9
def test_L9_adapter_records_membership_at_write_time_and_bare_call_records_null(env, tmp_path):
    from factlattice_engine import authoring
    e = _engine(tmp_path)
    out = authoring.add_property(str(env), "LG-ONE", "k", "v", store=e.store, proposer="p",
                                 approver="a", reason="why", via="clerk:Q-1")
    assert out["written"] is True and out["ledger"]["status"] == "ok"
    (rec,) = _recs(env)
    assert rec["corpora"] == ["c"] and rec["corpora"] == out["root_membership"]["corpora"]
    assert rec["engine_head"] and rec["engine_head"].startswith("sha256:")
    assert (rec["proposer"], rec["approver"], rec["reason"], rec["via"]) == ("p", "a", "why", "clerk:Q-1")
    (tmp_path / "elsewhere").mkdir()
    other = tmp_path / "elsewhere" / "outside.lts"
    other.write_text(DOC, encoding="utf-8")
    out2 = authoring.add_property(str(other), "LG-ONE", "k", "v", store=e.store)
    assert _recs(other)[0]["corpora"] == []                     # adapter resolved: inside no corpus
    set_property(env, "LG-TWO", "k", "v")                       # bare factlattice call
    assert _recs(env)[-1]["corpora"] is None and _recs(env)[-1]["engine_head"] is None


def test_L9_dispatch_threads_proposer_approver_reason_via(env, tmp_path):
    from factlattice_engine.tools import dispatch
    e = _engine(tmp_path)
    dispatch("lts_add_property", {"path": str(env), "node_id": "LG-ONE", "key": "k", "value": "v",
                                  "proposer": "P", "approver": "A", "reason": "R", "via": "direct"}, tools=e)
    dispatch("lts_add_node", {"path": str(env), "node_id": "LG-N2", "title": "t", "properties": {"kind": "z"},
                              "proposer": "P2", "reason": "R2"}, tools=e)
    dispatch("lts_repair_property", {"path": str(env), "node_id": "LG-ONE", "line": 9, "key": "flagged",
                                     "approver": "A3"}, tools=e)
    r = _recs(env)
    assert [(x["verb"], x["proposer"], x["approver"], x["reason"]) for x in r] == [
        ("set_property", "P", "A", "R"), ("add_node", "P2", None, "R2"), ("repair_property", None, "A3", None)]


def test_L9_bounds_enforced_through_the_adapter(env):
    from factlattice_engine import authoring
    out = authoring.add_property(str(env), "LG-ONE", "k", "v", reason="x" * 2001)
    assert out["refused"] is True and _recs(env) == []


# ------------------------------------------------------------------------------------- L8
def test_L8_ledger_never_lands_inside_a_registered_corpus_root(env, tmp_path, monkeypatch):
    from factlattice_engine import authoring
    sub = tmp_path / "corpus" / "sub"                           # a subdir, so the lock-dir guard (which only
    sub.mkdir()                                                 # knows the file's own dir) does not pre-empt
    f = sub / "c.lts"
    f.write_text(DOC, encoding="utf-8")
    e = _engine(tmp_path)
    inside = tmp_path / "corpus" / "state"                      # state dir INSIDE the registered root
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(inside))
    out = authoring.add_property(str(f), "LG-ONE", "k", "v", store=e.store)
    assert out["written"] is True and out["ledger"]["status"] == "failed"
    assert "corpus root" in out["ledger"]["live_detail"]
    assert not (inside / "ledger").exists()                     # nothing created in the corpus


def test_L8_module_refuses_a_path_inside_given_roots(tmp_path):
    p = ledger.ledger_path("/some/file.lts", state_dir=tmp_path / "root" / "state")
    with pytest.raises(ledger.LedgerError, match="corpus root"):
        ledger.check_outside_roots(p, [str(tmp_path / "root")])
    ledger.check_outside_roots(p, [str(tmp_path / "elsewhere")])           # fine


def test_L8_bare_call_refuses_ledger_inside_the_files_own_directory(env, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(env.parent / "st"))
    # the write-lock dir guard would refuse first, so exercise the ledger guard directly
    with pytest.raises(ledger.LedgerError):
        ledger.append(os.path.realpath(env), {"verb": "x"}, state_dir=env.parent / "st")


# ----------------------------------------------------------------------- negative control
def test_negative_control_skipping_the_append_turns_L1_and_L5_red(env, monkeypatch):
    """If the append is skipped, the very assertions L1/L5 make must FAIL -- proving they can."""
    monkeypatch.setattr(ledger, "append", lambda *a, **k: {"status": "ok", "seq": 0, "file": "-"})
    set_property(env, "LG-ONE", "k", "v")
    assert _recs(env) == []                                      # L1's `(rec,) = _recs(f)` would raise
    ok = 1
    assert len(_lines(env)) != ok                                # L5's `len(raw) == ok` would fail


# =============================================================================== ROUND 2 (review)
def _wd_env():
    from tests.test_atomic_write_round2 import _doc_obj, _fake_preview
    from factlattice.authoring.schema import write_document
    return write_document, _doc_obj, _fake_preview


# ---------------------------------------------------------------------------------- F1 (P1)
def test_F1_write_whole_file_surfaces_the_outcome_and_stays_a_path(env, tmp_path):
    out = write_whole_file(tmp_path / "corpus" / "n.lts", "# N [SPEC:N-ROOT]\n\n- kind: root\n")
    assert isinstance(out, Path) and out.name == "n.lts"            # backward compatible
    assert out.ledger["status"] == "ok" and out.ledger["seq"] == 1


def test_F1_write_whole_file_failure_is_visible_on_the_return(env, tmp_path):
    state = tmp_path / "state" / "ledger"
    state.mkdir(parents=True)
    os.chmod(state, 0o500)
    try:
        out = write_whole_file(tmp_path / "corpus" / "n.lts", "# N [SPEC:N-ROOT]\n\n- kind: root\n")
    finally:
        os.chmod(state, 0o700)
    assert (tmp_path / "corpus" / "n.lts").exists() and out.ledger["status"] == "failed"


def test_F1_write_document_returns_the_ledger_and_warns_on_failure(env, tmp_path):
    write_document, _doc_obj, _fake_preview = _wd_env()
    ok = write_document(_doc_obj(), str(tmp_path / "corpus" / "w.lts"), _fake_preview)
    assert ok["ledger"]["status"] == "ok" and not ok.get("warnings")
    state = tmp_path / "state" / "ledger"
    os.chmod(state, 0o500)
    try:
        bad = write_document(_doc_obj(), str(tmp_path / "corpus" / "w2.lts"), _fake_preview)
    finally:
        os.chmod(state, 0o700)
    assert bad["written"] and bad["ledger"]["status"] == "failed"
    assert any("WRITTEN, BUT NOT LEDGERED" in w for w in bad["warnings"])


def test_F1_write_document_threads_the_audit_metadata(env, tmp_path):
    write_document, _doc_obj, _fake_preview = _wd_env()
    f = tmp_path / "corpus" / "w.lts"
    write_document(_doc_obj(), str(f), _fake_preview, proposer="P", approver="A", reason="R",
                   via="direct", corpora=["c"], engine_head="sha256:x", ledger_roots=[str(tmp_path / "zzz")])
    (rec,) = _recs(f)
    assert (rec["proposer"], rec["approver"], rec["reason"], rec["corpora"], rec["engine_head"]) == \
        ("P", "A", "R", ["c"], "sha256:x")


def test_F1_write_document_ledger_roots_are_enforced(env, tmp_path):
    write_document, _doc_obj, _fake_preview = _wd_env()
    sub = tmp_path / "corpus" / "sub"
    sub.mkdir()
    inside = tmp_path / "corpus" / "state2"
    os.environ["FACTLATTICE_STATE_DIR"] = str(inside)
    try:
        r = write_document(_doc_obj(), str(sub / "w.lts"), _fake_preview, ledger_roots=[str(tmp_path / "corpus")])
    finally:
        os.environ["FACTLATTICE_STATE_DIR"] = str(tmp_path / "state")
    assert r["ledger"]["status"] == "failed" and "corpus root" in r["ledger"]["live_detail"]
    assert not (inside / "ledger").exists()


# ----------------------------------------------------------------------------------- F2
def test_F2_history_reports_out_of_band_edits_between_ledgered_writes(env, tmp_path):
    from factlattice_engine.tools import dispatch
    set_property(env, "LG-ONE", "k0", "v0")
    set_property(env, "LG-ONE", "k1", "v1")
    env.write_text(env.read_text() + "\nhand edit\n")             # out of band
    set_property(env, "LG-ONE", "k2", "v2")                         # reconciled is true again
    h = dispatch("lts_history", {"corpus": "c", "node_id": "LG-ONE"}, tools=_engine(tmp_path))
    assert h["reconciled"] is True                                  # unchanged semantics
    assert [b["seq"] for b in h["chain_file_breaks"]] == [3] and h["chain_file_breaks"][0]["ts"]
    assert h["history_has_out_of_band_edits"] is True


def test_F2_clean_history_has_no_breaks(env, tmp_path):
    from factlattice_engine.tools import dispatch
    set_property(env, "LG-ONE", "k0", "v0")
    set_property(env, "LG-ONE", "k1", "v1")
    h = dispatch("lts_history", {"corpus": "c", "node_id": "LG-ONE"}, tools=_engine(tmp_path))
    assert h["chain_file_breaks"] == [] and h["history_has_out_of_band_edits"] is False


# ----------------------------------------------------------------------------------- F3
def _undo_all(f: Path, rec: dict) -> None:
    lines = f.read_text(encoding="utf-8").split("\n")
    ins = rec.get("inserted")
    if ins:
        del lines[ins["line_number"] - 1: ins["line_number"] - 1 + len(ins["lines"])]
    for d in rec["discarded"]:
        lines[d["line_number"] - 1] = d["line"]
    f.write_text("\n".join(lines), encoding="utf-8")


@pytest.mark.parametrize("verb", ["set_property", "append", "repair_property", "add_node"])
def test_F3_undo_restores_file_sha256_before_for_all_four_verbs(env, verb):
    if verb == "set_property":
        set_property(env, "LG-ONE", "status", "active")
    elif verb == "append":
        set_property(env, "LG-ONE", "alias", "second", mode="append")
    elif verb == "repair_property":
        repair_property(env, "LG-ONE", 9, "flagged")
    else:
        add_node(env, "LG-NEW", "New node", {"kind": "z", "note": "n"}, section=None)
    (rec,) = _recs(env)
    assert _sha(env) == rec["file_sha256_after"] != rec["file_sha256_before"]
    _undo_all(env, rec)
    assert _sha(env) == rec["file_sha256_before"]


def test_F3_set_add_of_a_new_key_and_add_node_in_a_section_also_undo(env):
    set_property(env, "LG-ONE", "brand-new", "v")
    add_node(env, "LG-N3", "Third", {"kind": "q"}, section="LG-ONE")
    for rec in reversed(_recs(env)):
        _undo_all(env, rec)
    assert _sha(env) == _recs(env)[0]["file_sha256_before"]


# ----------------------------------------------------------------------------------- F6
def test_F6_registry_error_is_not_swallowed(env, tmp_path, monkeypatch):
    from factlattice_engine import authoring, membership
    real_roots, calls = membership.registered_roots, []
    def boom(store=None):                      # fail only the adapter's ledger lookup (the first call)
        calls.append(1)
        if len(calls) % 2:
            raise RuntimeError("registry exploded")
        return real_roots(store)
    monkeypatch.setattr(membership, "registered_roots", boom)
    out = authoring.add_property(str(env), "LG-ONE", "k", "v")
    assert out["written"] is True
    assert any("ledger roots check SKIPPED: " in w and "registry exploded" in w for w in out["warnings"])
    (rec,) = _recs(env)
    assert rec["corpora"] is None and "registry exploded" in rec["corpora_error"]
    calls.clear()
    full = authoring.add_node(str(env), "LG-F6", "t", {"kind": "z"}, receipt="full")
    assert any("ledger roots check SKIPPED" in w for w in full["warnings"])


def test_F6_normal_writes_have_no_corpora_error(env):
    set_property(env, "LG-ONE", "k", "v")
    assert _recs(env)[0]["corpora_error"] is None


# ----------------------------------------------------------------------------------- F7
def test_F7_unencodable_metadata_is_refused_before_any_write(env):
    before = env.read_bytes()
    for kw in ({"reason": "bad \ud800 surrogate"}, {"proposer": "\udfff"}, {"approver": "x\ud83d"}):
        with pytest.raises(EditRefused, match="Nothing written"):
            set_property(env, "LG-ONE", "k", "v", **kw)
    assert env.read_bytes() == before and _recs(env) == []
    set_property(env, "LG-ONE", "k", "v", reason="fine \u2713 pictograph \U0001F600")
    assert len(_recs(env)) == 1


# ----------------------------------------------------------------------------------- F8
def _writer_recipes(f: Path, tmp_path: Path):
    write_document, _doc_obj, _fake_preview = _wd_env()
    return {
        "set_property": lambda: set_property(f, "LG-ONE", "r8", "v"),
        "repair_property": lambda: repair_property(f, "LG-ONE", 9, "flagged8"),
        "add_node": lambda: add_node(f, "LG-R8", "t", {"kind": "z"}),
        "rename_property": lambda: edit.rename_property(f, "LG-ONE", "kind", "kind-r8"),
        "remove_property": lambda: edit.remove_property(f, "LG-ONE", "alias", expect_value="first"),
        "write_whole_file": lambda: write_whole_file(f, f.read_text() + "\n"),
        "write_document": lambda: write_document(_doc_obj(), str(f), _fake_preview),
    }


def _discovered_writers() -> set:
    import inspect
    from factlattice.authoring import schema
    found = {n for n, fn in inspect.getmembers(edit, inspect.isfunction)
             if fn.__module__ == edit.__name__ and not n.startswith("_")
             and next(iter(inspect.signature(fn).parameters), None) == "path"}
    for n, fn in inspect.getmembers(schema, inspect.isfunction):
        if fn.__module__ == schema.__name__ and n.startswith("write"):
            found.add(n)
    return found


def test_F8_every_public_writer_appends_exactly_one_record(env, tmp_path):
    """Every public writer appends exactly ONE record, EXCEPT a create whose reply carries
    `ledger_state: "unknown"` (its `_ledger_append` did not return), for which ZERO OR ONE record exists
    (tests/test_create_outcome_matrix.py pins that case). Restated in SDLC 7.6 [CREATE-WRITER]."""
    recipes = _writer_recipes(env, tmp_path)
    found = _discovered_writers()
    assert found == set(recipes), f"a writer has no ledger recipe (or a recipe is stale): {found ^ set(recipes)}"
    for name, run in recipes.items():
        n0 = len(_recs(env))
        run()
        assert len(_recs(env)) == n0 + 1, f"{name} did not append exactly one ledger record"


# ----------------------------------------------------------------------------------- F10
def test_F10_unparseable_tail_makes_reconciled_null_not_stale(env, tmp_path):
    from factlattice_engine.tools import dispatch
    set_property(env, "LG-ONE", "k0", "v0")
    with open(ledger.ledger_path(os.path.realpath(env)), "ab") as fh:
        fh.write(b"this is not json\n")
    h = dispatch("lts_history", {"corpus": "c", "node_id": "LG-ONE"}, tools=_engine(tmp_path))
    assert h["reconciled"] is None and h["reconciled_reason"] == "tail unparseable"
    assert h["unparseable_lines"] == 1


def test_F10_parseable_tail_has_no_reason(env, tmp_path):
    from factlattice_engine.tools import dispatch
    set_property(env, "LG-ONE", "k0", "v0")
    h = dispatch("lts_history", {"corpus": "c", "node_id": "LG-ONE"}, tools=_engine(tmp_path))
    assert h["reconciled"] is True and h.get("reconciled_reason") is None


# ----------------------------------------------------------------------------------- F11
def test_F11_every_record_carries_its_backup_path(env, tmp_path):
    write_document, _doc_obj, _fake_preview = _wd_env()
    set_property(env, "LG-ONE", "k", "v")
    add_node(env, "LG-B", "t", {"kind": "z"})
    repair_property(env, "LG-ONE", 9, "flagged")
    write_document(_doc_obj(), str(env), _fake_preview)
    recs = _recs(env)
    assert len(recs) == 4
    for r in recs:
        assert r["backup_path"] and Path(r["backup_path"]).is_file(), r["verb"]
    assert "k: v" not in Path(recs[0]["backup_path"]).read_text()    # the pre-write copy
    assert _sha(Path(recs[3]["backup_path"])) == recs[3]["file_sha256_before"]


def test_F11_a_brand_new_file_has_no_backup(env, tmp_path):
    p = tmp_path / "corpus" / "fresh.lts"
    write_whole_file(p, "# F [SPEC:F-ROOT]\n\n- kind: root\n")
    assert _recs(p)[0]["backup_path"] is None


# ------------------------------------------------------------------- delta-review N1 / N2
def test_N1_unencodable_via_is_refused_before_any_write(env):
    """Delta review N1: F7's UTF-8 check covered proposer/approver/reason but not `via`."""
    before = env.read_bytes()
    with pytest.raises(EditRefused, match="Nothing written"):
        set_property(env, "LG-ONE", "k", "v", via="clerk:\ud800")
    assert env.read_bytes() == before and _recs(env) == []


def test_N2_response_keys_absent_when_not_triggered(env, tmp_path):
    """SDLC 7.6 Tool-surface rule, applied to this branch's own keys: `ledger` is ABSENT from a
    refusal receipt (nothing was written), and `warnings` is ABSENT/empty on a clean write_document."""
    from factlattice_engine import authoring
    refused = authoring.add_property(str(env), "LG-ONE", "k", "v", reason="x" * 2001)
    assert refused["refused"] is True and "ledger" not in refused
    refused2 = authoring.add_property(str(env), "NO-SUCH-NODE", "k", "v")
    assert refused2.get("refused") is True and "ledger" not in refused2
    write_document, _doc_obj, _fake_preview = _wd_env()
    ok = write_document(_doc_obj(), str(tmp_path / "corpus" / "n2.lts"), _fake_preview)
    assert ok["ledger"]["status"] == "ok" and not ok.get("warnings")
