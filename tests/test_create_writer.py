"""[A-E1 / X6 CREATE-WRITER] `lts_add_node(..., create=True[, create_parent=True])`: a file-creating writer
uses EXCLUSIVE create under the per-file lock, appends exactly ONE ledger record in the EXISTING field
vocabulary, states its undo position (none: undo/verify refuse a create loudly) and has a race test.

Spec: SPEC-A-reviewer-v3 A-E1 as governed by the v4 addendum C4(a)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from factlattice import ledger
from factlattice.authoring import edit
from factlattice.authoring.edit import EditRefused, add_node, set_property
from factlattice.parser import parse_text

REPO = str(Path(__file__).resolve().parents[1])
ROUNDS = int(os.environ.get("FL_CREATE_ROUNDS", "30"))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    (tmp_path / "out").mkdir()
    return tmp_path


def _recs(f) -> list[dict]:
    p = ledger.ledger_path(os.path.realpath(f))
    return [json.loads(x) for x in p.read_bytes().splitlines()] if p.exists() else []


def _create(path, **kw):
    kw.setdefault("doc_title", "A review")
    kw.setdefault("domain", "reviews")
    kw.setdefault("doctype", "review")
    return add_node(path, "REVIEW-K1", "Review K1", {"verdict": "in-progress", "kind": "review"},
                    create=True, **kw)


# --------------------------------------------------------------------------- what it writes
def test_create_writes_frontmatter_then_exactly_the_node(env):
    f = env / "out" / "r.lts"
    r = _create(f)
    text = f.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    pf = parse_text(text, str(f))
    assert [n.id for n in pf.nodes] == ["REVIEW-K1"]
    assert dict(pf.nodes[0].properties) == {"verdict": "in-progress", "kind": "review"}
    fm = pf.frontmatter
    assert fm["lts"].strip('"') == "1.1" and fm["title"] == "A review"
    assert fm["domain"] == "reviews" and fm["doctype"] == "review" and fm["generated-by"]
    assert r.delta == {"REVIEW-K1": {"added": ["kind", "verdict"], "removed": []}}
    assert r.created is True and r.created_parent is None


def test_without_create_a_missing_file_is_still_refused_exactly_as_before(env):
    with pytest.raises(EditRefused, match="is not a file — nothing written."):
        add_node(env / "out" / "nope.lts", "X-ONE", "t", {"kind": "a"})
    assert not (env / "out" / "nope.lts").exists()


def test_a_created_file_takes_ordinary_writers_afterwards(env):
    f = env / "out" / "r.lts"
    _create(f)
    add_node(f, "REVFINDING-K1-1", "F1", {"kind": "finding", "part-of": "[REVIEW-K1]"})
    set_property(f, "REVIEW-K1", "verdict", "pass")
    pf = parse_text(f.read_text(encoding="utf-8"), str(f))
    assert {n.id for n in pf.nodes} == {"REVIEW-K1", "REVFINDING-K1-1"}
    assert len(_recs(f)) == 3


# --------------------------------------------------------------------------- refusals
def test_refuses_an_existing_file_and_never_touches_it(env):
    f = env / "out" / "r.lts"
    f.write_bytes(b"# keep me [SPEC:KEEP]\n\n- kind: x\n")
    before = f.read_bytes()
    with pytest.raises(EditRefused) as ei:
        _create(f)
    assert "exists" in str(ei.value) and ei.value.evidence["refusal"] == "create_file_exists"
    assert f.read_bytes() == before and _recs(f) == []


def test_refuses_an_existing_EMPTY_file_too(env):
    f = env / "out" / "r.lts"
    f.write_bytes(b"")
    with pytest.raises(EditRefused, match="exists"):
        _create(f)
    assert f.read_bytes() == b""


def test_refuses_a_dangling_symlink_at_the_target(env):
    f = env / "out" / "r.lts"
    os.symlink(env / "out" / "elsewhere.lts", f)
    with pytest.raises(EditRefused, match="exists"):
        _create(f)
    assert not (env / "out" / "elsewhere.lts").exists()


def test_refuses_a_missing_parent_without_create_parent_and_makes_no_directory(env):
    f = env / "out" / "sub" / "r.lts"
    with pytest.raises(EditRefused) as ei:
        _create(f)
    assert ei.value.evidence["refusal"] == "create_parent_missing"
    assert not (env / "out" / "sub").exists()


def test_create_parent_creates_exactly_one_level(env):
    f = env / "out" / "sub" / "r.lts"
    r = _create(f, create_parent=True)
    assert f.is_file() and r.created_parent == str(os.path.realpath(f.parent))


def test_create_parent_refuses_when_the_grandparent_is_missing_and_makes_nothing(env):
    f = env / "out" / "a" / "b" / "r.lts"
    with pytest.raises(EditRefused) as ei:
        _create(f, create_parent=True)
    assert ei.value.evidence["refusal"] == "create_grandparent_missing"
    assert not (env / "out" / "a").exists()


def test_create_parent_refuses_when_the_parent_exists_as_a_file(env):
    (env / "out" / "sub").write_text("i am a file")
    with pytest.raises(EditRefused):
        _create(env / "out" / "sub" / "r.lts", create_parent=True)


def test_create_parent_without_create_is_refused(env):
    f = env / "out" / "x.lts"
    f.write_text("# x [SPEC:X]\n")
    with pytest.raises(EditRefused) as ei:
        add_node(f, "X-TWO", "t", {"kind": "a"}, create_parent=True)
    assert ei.value.evidence["refusal"] == "create_parent_needs_create"


def test_create_with_a_section_is_refused(env):
    with pytest.raises(EditRefused) as ei:
        _create(env / "out" / "r.lts", section="WHATEVER")
    assert ei.value.evidence["refusal"] == "create_section_unsupported"


def test_refuses_under_an_ephemeral_root_unless_the_documented_override_is_set(env, monkeypatch):
    f = env / "out" / "r.lts"
    monkeypatch.setenv(ledger.EPHEMERAL_ENV, str(env))        # declare this tree ephemeral
    assert ledger.is_ephemeral(f.parent)                      # the premise of the test
    with pytest.raises(EditRefused) as ei:
        _create(f)
    assert ei.value.evidence["refusal"] == "create_ephemeral_root" and not f.exists()
    assert not ledger.ledger_path(os.path.realpath(f)).exists()
    monkeypatch.setenv(ledger.EPHEMERAL_ENV, "")             # the documented test override
    _create(f)
    assert f.is_file()


def test_refuses_under_the_DEFAULT_ephemeral_roots_the_os_temp_dir(env, monkeypatch):
    import shutil, tempfile
    monkeypatch.delenv(ledger.EPHEMERAL_ENV)
    monkeypatch.setattr(tempfile, "tempdir", None)           # the production default, not the suite's
    d = Path(tempfile.mkdtemp(prefix="fltest-create-"))
    try:
        with pytest.raises(EditRefused) as ei:
            _create(d / "r.lts")
        assert ei.value.evidence["refusal"] == "create_ephemeral_root" and not (d / "r.lts").exists()
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.parametrize("bad", [{"doc_title": "a\nb"}, {"domain": "x\n---\ny"}, {"doctype": ""},
                                 {"doc_title": "  "}])
def test_frontmatter_values_that_would_not_round_trip_are_refused_before_any_write(env, bad):
    f = env / "out" / "r.lts"
    with pytest.raises(EditRefused):
        _create(f, **bad)
    assert not f.exists()


def test_bad_properties_refuse_before_any_directory_is_made(env):
    f = env / "out" / "sub" / "r.lts"
    with pytest.raises(EditRefused):
        add_node(f, "REVIEW-K1", "t", {"1bad key": "v"}, create=True, create_parent=True)
    assert not (env / "out" / "sub").exists()


# --------------------------------------------------------------------------- the ledger
def test_exactly_one_record_in_the_existing_vocabulary(env):
    f = env / "out" / "r.lts"
    _create(f)
    (rec,) = _recs(f)
    assert set(rec) == set(ledger.FIELDS)                     # no new field
    assert rec["verb"] == "add_node" and rec["node_id"] == "REVIEW-K1"
    assert rec["file_sha256_before"] is None and rec["prior_blob"] is None
    assert "created file" in rec["undo_evidence_missing"]
    assert rec["file_sha256_after"] == hashlib.sha256(f.read_bytes()).hexdigest()
    assert rec["ledger_seq"] == 1 and rec["prev_sha256"] is None and rec["backup_path"] is None
    assert rec["follows_out_of_band_edit"] is False
    assert "created_parent" not in (rec["delta"] or {})


def test_creating_the_parent_is_still_ONE_record_and_is_noted_in_delta(env):
    f = env / "out" / "sub" / "r.lts"
    _create(f, create_parent=True)
    (rec,) = _recs(f)
    assert rec["delta"]["created_parent"] == os.path.realpath(f.parent)
    assert "REVIEW-K1" in rec["delta"]


def test_a_create_is_a_clean_ledgered_status_not_a_lost_undo_warning(env):
    r = _create(env / "out" / "r.lts")
    assert r.ledger["status"] == "ok" and not edit.oob_warnings(r.ledger, env / "out" / "r.lts")


def test_undo_and_verify_refuse_a_create_loudly(env):
    f = env / "out" / "r.lts"
    _create(f)
    set_property(f, "REVIEW-K1", "verdict", "pass")
    create_rec, set_rec = _recs(f)
    with pytest.raises(ledger.LedgerError, match="undo of a file creation is not supported"):
        ledger.check_undoable(create_rec)
    ledger.check_undoable(set_rec)                            # an ordinary record still passes


def test_undo_and_verify_refuse_an_unknown_verb_loudly(env):
    f = env / "out" / "r.lts"
    _create(f)
    rec = dict(_recs(f)[0], verb="frobnicate")
    with pytest.raises(ledger.LedgerError, match="unknown ledger verb"):
        ledger.check_undoable(rec)


def test_every_verb_the_writers_use_is_known_to_the_ledger():
    assert {"set_property", "append", "repair_property", "add_node", "write_document"} <= set(ledger.KNOWN_VERBS)


# --------------------------------------------------------------------------- the race
_CHILD = textwrap.dedent("""
    import sys, time, contextlib
    sys.path.insert(0, {repo!r})
    from factlattice.authoring import edit
    from factlattice.authoring.edit import add_node, EditRefused
    path, i, start, nolock = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4] == "1"
    if nolock:      # NEGATIVE CONTROL: take the per-file lock away; exclusivity must still hold
        edit._file_lock = lambda p: contextlib.nullcontext()
    while time.time() < start:
        pass
    try:
        add_node(path, "REVIEW-K" + i, "Review " + i, {{"kind": "review", "who": i}}, create=True,
                 doc_title="t", domain="reviews", doctype="review")
        print("ok")
    except EditRefused as e:
        print("refused:" + str(e.evidence.get("refusal")))
""")


def _race(tmp_path, rounds, nolock):
    wins = 0
    for rnd in range(rounds):
        d = tmp_path / f"r{rnd}"
        (d / "out").mkdir(parents=True)
        f = d / "out" / "r.lts"
        e = dict(os.environ, FACTLATTICE_BACKUP_DIR=str(d / "bak"), FACTLATTICE_STATE_DIR=str(d / "state"),
                 FACTLATTICE_LEDGER_EPHEMERAL_ROOTS="")
        start = time.time() + 1.0
        procs = [subprocess.Popen([sys.executable, "-c", _CHILD.format(repo=REPO), str(f), str(i),
                                   str(start), "1" if nolock else "0"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
                 for i in (1, 2)]
        outs = []
        for p in procs:
            o, err = p.communicate(timeout=90)
            outs.append((o.strip().splitlines() or [err[-300:]])[-1])
        assert sorted(o.split(":")[0] for o in outs) == ["ok", "refused"], (rnd, outs)
        assert [o for o in outs if o != "ok"] == ["refused:create_file_exists"], (rnd, outs)
        pf = parse_text(f.read_text(encoding="utf-8"), str(f))
        assert len(pf.nodes) == 1, (rnd, [n.id for n in pf.nodes])      # exactly one node, never a blend
        winner = pf.nodes[0].id[-1]
        assert dict(pf.nodes[0].properties)["who"] == winner
        wins += 1
        if not nolock:
            assert len(_recs_in(d / "state", f)) == 1
    return wins


def _recs_in(state, f):
    p = Path(state) / "ledger" / (hashlib.sha256(os.path.realpath(f).encode()).hexdigest()[:32] + ".jsonl")
    return [json.loads(x) for x in p.read_bytes().splitlines()] if p.exists() else []


def test_30_rounds_two_creators_exactly_one_wins(tmp_path):
    assert _race(tmp_path, ROUNDS, nolock=False) == ROUNDS


def test_exclusive_create_holds_even_with_the_lock_removed(tmp_path):
    """Negative control: the lock is not what makes create exclusive."""
    assert _race(tmp_path, max(10, ROUNDS // 3), nolock=True) >= 10


# ===================================================== post-review P2-2: EVERY create refusal carries a code
def _refusal_of(fn):
    with pytest.raises(EditRefused) as ei:
        fn()
    return ei.value


def test_every_create_refusal_path_carries_an_evidence_refusal_code(env, monkeypatch):
    out = env / "out"
    ok = {"kind": "review"}
    cases = {
        "bad node id": lambda: add_node(out / "a.lts", "lower-case", "t", ok, create=True),
        "illegal property key": lambda: add_node(out / "b.lts", "REVIEW-K1", "t", {"1bad key": "v"}, create=True),
        "empty property value": lambda: add_node(out / "c.lts", "REVIEW-K1", "t", {"kind": "  "}, create=True),
        "multi-line value": lambda: add_node(out / "d.lts", "REVIEW-K1", "t", {"kind": "a\nb"}, create=True),
        "two keys coerce to one": lambda: add_node(out / "e.lts", "REVIEW-K1", "t",
                                                   {"My Key": "1", "my-key": "2"}, create=True),
        "section": lambda: add_node(out / "f.lts", "REVIEW-K1", "t", ok, create=True, section="X"),
        "frontmatter value": lambda: add_node(out / "g.lts", "REVIEW-K1", "t", ok, create=True, doc_title="a\nb"),
        "create_parent without create": lambda: add_node(out / "h.lts", "REVIEW-K1", "t", ok, create_parent=True),
        "parent missing": lambda: add_node(out / "nodir" / "i.lts", "REVIEW-K1", "t", ok, create=True),
        "grandparent missing": lambda: add_node(out / "x" / "y" / "j.lts", "REVIEW-K1", "t", ok,
                                                create=True, create_parent=True),
    }
    (out / "k.lts").write_text("# x [SPEC:X]\n")
    cases["file exists"] = lambda: add_node(out / "k.lts", "REVIEW-K1", "t", ok, create=True)
    (out / "afile").write_text("x")
    cases["parent is a file"] = lambda: add_node(out / "afile" / "l.lts", "REVIEW-K1", "t", ok, create=True)
    seen = {}
    for name, fn in cases.items():
        e = _refusal_of(fn)
        seen[name] = (e.evidence or {}).get("refusal")
    missing = [n for n, c in seen.items() if not c]
    assert not missing, f"create refusals with NO evidence.refusal code: {missing}  (all: {seen})"
    # the gate (a node the parser would not mint as authored) and the lock are refusals too
    from factlattice.authoring import edit
    monkeypatch.setattr(edit, "_gate_new_node", lambda *a, **k: (_ for _ in ()).throw(EditRefused("gate")))
    e = _refusal_of(lambda: add_node(out / "m.lts", "REVIEW-K1", "t", ok, create=True))
    assert (e.evidence or {}).get("refusal"), "a gate refusal on a create has no code"


def test_a_busy_lock_refusal_carries_a_code(env, monkeypatch):
    from factlattice.authoring import edit
    f = env / "out" / "n.lts"
    monkeypatch.setenv("FACTLATTICE_LOCK_TIMEOUT_S", "0.05") if hasattr(edit, "_lock_timeout") else None
    monkeypatch.setattr(edit, "_lock_timeout", lambda: 0.05)
    with edit._file_lock(f):
        import fcntl, os as _os
        # a second holder in the SAME process on a new fd conflicts (flock is per open file description)
        e = _refusal_of(lambda: add_node(f, "REVIEW-K1", "t", {"kind": "r"}, create=True))
    assert (e.evidence or {}).get("refusal") == "lock_busy"


def test_no_raise_in_the_create_helpers_is_uncoded():
    """Static: every `raise EditRefused(...)` in the create-only helpers names an evidence code."""
    import ast, inspect
    from factlattice.authoring import edit
    for fn in (edit._create_node_file, edit._fm_value, edit._create_exclusive):
        tree = ast.parse(__import__("textwrap").dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) \
                    and getattr(node.exc.func, "id", "") == "EditRefused":
                assert any(k.arg == "evidence" for k in node.exc.keywords), \
                    f"{fn.__name__}: an uncoded EditRefused at line {node.lineno}"
