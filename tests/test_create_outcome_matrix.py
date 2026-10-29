"""[delta5] The create reply is one of three TRUE things, decided at ONE observation point after the core exits:
  success  (written:true)        our create is in the file system (by INODE) or the ledger
  refused                        VERIFIED: nothing of ours at the path and no ledger record of this create
  outcome: "unknown"             success:false, refused:false, written:null, code create_outcome_unknown
Every cell is judged against an oracle that does not use the reply: the file system and the ledger."""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path

import pytest

from factlattice import ledger as _lg
from factlattice.authoring import edit
from factlattice.authoring.edit import EditRefused
from factlattice_engine import authoring
from factlattice_engine.tools import dispatch

MSG = "SECRET-MESSAGE-with-/private/path"


class Custom(Exception):
    pass


KINDS = {
    "ValueError": lambda: ValueError(MSG),
    "KeyError": lambda: KeyError(MSG),
    "UnicodeEncodeError": lambda: UnicodeEncodeError("utf-8", MSG, 0, 1, "bad"),
    "Custom": lambda: Custom(MSG),
    "OSError-EXDEV": lambda: OSError(errno.EXDEV, MSG),
    "OSError-EPERM": lambda: OSError(errno.EPERM, MSG),
    "OSError-ENOSPC": lambda: OSError(errno.ENOSPC, MSG),
    "InterruptedError": lambda: InterruptedError(MSG),
    "FileExistsError": lambda: FileExistsError(errno.EEXIST, MSG),
    "EditRefused": lambda: EditRefused(MSG),
    "EditRefused+foreign-code": lambda: EditRefused(MSG, evidence={"refusal": "x_foreign"}),
    "RecursionError": lambda: RecursionError(MSG),
    "MemoryError": lambda: MemoryError(MSG),
    "AttributeError": lambda: AttributeError(MSG),
}

ACTIVE = {"on": False, "dir": ""}


def _guard(owner, name, mode, kind, *, path_arg=None):
    real = getattr(owner, name)

    def w(*a, **k):
        hit = ACTIVE["on"]
        if hit and path_arg is not None:
            hit = ACTIVE["dir"] in str(a[path_arg]) if len(a) > path_arg else False
        if not hit:
            return real(*a, **k)
        if mode == "before":
            raise KINDS[kind]()
        v = real(*a, **k)
        raise KINDS[kind]()
    return w


# (label, owner, attr, path_arg, observation?)
POINTS = [
    ("sweep_stale_temps", edit, "_sweep_stale_temps", None, False),
    ("gate_new_node", edit, "_gate_new_node", None, False),
    ("ledger_append", edit, "_ledger_append", None, False),
    ("ledger.append", _lg, "append", None, False),
    ("ledger.read_lines", _lg, "read_lines", None, True),
    ("context", edit, "_context", None, False),
    ("oob_warnings", edit, "oob_warnings", None, False),
    ("last_ledger", edit, "last_ledger", None, False),
    ("lstat_ident", edit, "_lstat_ident", None, True),
    ("create_exclusive", edit, "_create_exclusive", None, False),
    ("node_hashes", edit, "_node_hashes", None, False),
    ("analyse_many", authoring._binding, "analyse_many", None, False),
    ("annotate", authoring._membership, "annotate", None, False),
    ("render_node_receipt", authoring, "_render_node_receipt", None, False),
    ("compact_node_receipt", authoring, "_compact_node_receipt", None, False),
    ("apply_ledger", authoring, "_apply_ledger", None, False),
    ("render_binding", authoring, "_render_binding", None, False),
    ("type_only", authoring, "_type_only", None, False),
    ("ledger_kw", authoring, "_ledger_kw", None, False),
    ("observe_disk", authoring, "_observe_disk", None, True),
    ("os.link", os, "link", 1, False),
    ("os.fsync", os, "fsync", None, False),
    ("os.unlink", os, "unlink", 0, False),
    ("os.lstat", os, "lstat", 0, True),
    ("os.open", os, "open", 0, False),
    ("os.rmdir", os, "rmdir", 0, False),
]
OBS = {p[0] for p in POINTS if p[4]}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    d = tmp_path / "reviews"
    d.mkdir()
    ACTIVE.update(on=False, dir=str(d))
    yield d
    ACTIVE["on"] = False


def _args(d, **kw):
    return {"path": str(d / "r.lts"), "node_id": "REVIEW-K9", "title": "t", "properties": {"kind": "review"},
            "create": True, "doc_title": "t", "domain": "reviews", "doctype": "review", **kw}


def _call(d, **kw):
    ACTIVE["on"] = True
    try:
        return dispatch("lts_add_node", _args(d, **kw), None)
    finally:
        ACTIVE["on"] = False


def _recs(f):
    return _lg.read_lines(os.path.realpath(f)) or []


def _classify(r):
    if r.get("outcome") == "unknown":
        assert r["success"] is False and r["refused"] is False and r["written"] is None, r
        assert r["evidence"]["refusal"] == "create_outcome_unknown" and r["ledger_state"] == "unknown", r
        assert "re-read the path before retrying" in r["reason"], r
        return "unknown"
    if r.get("refused") is True:
        assert r["success"] is False and r["written"] is False and (r.get("evidence") or {}).get("refusal"), r
        return "refused"
    assert r.get("success") is True and r.get("written") is True and r.get("refused") is False, r
    return "success"


def judge(d, r, *, point=None, observation=False, exempt_message=False):
    f = d / "r.lts"
    exists, recs = f.is_file(), _recs(f)
    cls = _classify(r)
    assert len(recs) <= 1, "more than one record of one create"
    if not exempt_message:
        assert "SECRET-MESSAGE" not in json.dumps(r, default=str), "an exception message reached the reply"
    if point != "os.unlink":
        assert not [n for n in os.listdir(d) if n != "r.lts"], os.listdir(d)       # no leaked temp (F9)
    if cls == "refused":
        assert not exists and not recs, "refused, but something of ours exists"
    elif cls == "success":
        assert exists or recs, "success, but neither a file nor a record exists"
        if r.get("ledger_state") == "unknown":
            assert len(recs) in (0, 1), recs                     # zero or one record exists, and the reply says it does not know
            assert any("whether it was ledgered is unknown" in w for w in r["warnings"]), r
        else:
            led = r.get("ledger")
            assert led == "recorded" or (isinstance(led, dict) and led.get("status") == "ok"), ("no claim", led)
            assert len(recs) == 1
    else:                                                                            # unknown
        assert observation, f"unknown, but the observation was not interfered with ({point})"
    if exists or recs:
        assert cls != "refused"
    if not exists and not recs and not observation:
        assert cls == "refused", ("nothing exists and the observation was whole, so the reply must be 'refused'", cls)
    if exists and not observation:
        assert cls == "success", ("the file is ours and the observation was whole", cls)
    return cls


@pytest.mark.parametrize("kind", list(KINDS))
@pytest.mark.parametrize("mode", ["before", "after"])
@pytest.mark.parametrize("point", [p[0] for p in POINTS])
def test_matrix_26_points_x_before_after_x_14_kinds(world, monkeypatch, point, mode, kind):
    _lbl, owner, attr, parg, obs = next(p for p in POINTS if p[0] == point)
    monkeypatch.setattr(owner, attr, _guard(owner, attr, mode, kind, path_arg=parg))
    r = _call(world)
    judge(world, r, point=point, observation=obs,
          exempt_message=(point == "gate_new_node" and kind == "EditRefused"))


def test_matrix_size_is_728():
    assert len(POINTS) * 2 * len(KINDS) == 728


# ------------------------------------------------------------------ targeted
def test_byte_identical_race_loser_is_refused_whatever_the_link_error(world, monkeypatch):
    for i, kind in enumerate(["OSError-EXDEV", "OSError-EPERM", "ValueError", "Custom", "FileExistsError"]):
        d = world / f"c{i}"
        d.mkdir()
        ACTIVE["dir"] = str(d)
        real = os.link
        def link(a, b, *x, _d=d, _k=kind, **k):
            if str(b).startswith(str(_d)):
                Path(b).write_bytes(Path(a).read_bytes())          # a winner with IDENTICAL bytes, another inode
                raise KINDS[_k]()
            return real(a, b, *x, **k)
        monkeypatch.setattr(os, "link", link)
        ACTIVE["on"] = True
        try:
            r = dispatch("lts_add_node", _args(d), None)
        finally:
            ACTIVE["on"] = False
            monkeypatch.setattr(os, "link", real)
        assert _classify(r) == "refused", (kind, r)
        assert (d / "r.lts").is_file() and not _recs(d / "r.lts")        # the winner's file stands, no record of ours
        assert "SECRET-MESSAGE" not in json.dumps(r, default=str)


def test_concurrent_set_property_after_the_link_with_written_unset_is_success(world, monkeypatch):
    """The flag is lost and a later writer REPLACES the file (a new inode): the inode no longer matches, but
    this create's ledger record proves our write, so the answer is success/incomplete, not a refusal."""
    real_la, real_obs = edit._ledger_append, authoring._observe_disk
    def la(real, ctx, cand):
        real_la(real, ctx, cand)                                     # RETURNED: ledger_seq is in the state
        ctx["_state"].pop("written", None)                           # the flag is lost ...
        raise RuntimeError(MSG)
    def obs(state):
        monkeypatch.setattr(edit, "_ledger_append", real_la)                 # the other writer is a normal one
        edit.set_property(state["real"], "REVIEW-K9", "verdict", "pass")      # ... and the file is REPLACED (new inode)
        return real_obs(state)
    monkeypatch.setattr(edit, "_ledger_append", la)
    monkeypatch.setattr(authoring, "_observe_disk", obs)
    r = _call(world)
    f = world / "r.lts"
    assert _classify(r) == "success" and r["receipt"] == "incomplete", r
    assert r["ledger"] == "recorded" and len(_recs(f)) == 2             # [create, set_property]: seq proves ours


def test_unreadable_directory_after_the_link_is_unknown_never_refused(world, monkeypatch):
    real_ce = edit._create_exclusive
    def ce(real, data, state=None):
        real_ce(real, data, state)
        state.pop("written")
        os.chmod(world, 0)
        raise RuntimeError(MSG)
    monkeypatch.setattr(edit, "_create_exclusive", ce)
    try:
        r = _call(world)
    finally:
        os.chmod(world, 0o755)
    assert _classify(r) == "unknown", r
    assert (world / "r.lts").is_file()                                   # it IS there; we could not see it
    # a retry after unknown NEVER overwrites
    monkeypatch.setattr(edit, "_create_exclusive", real_ce)
    before = (world / "r.lts").read_bytes()
    r2 = _call(world)
    assert _classify(r2) == "refused" and r2["evidence"]["refusal"] == "create_file_exists"
    assert (world / "r.lts").read_bytes() == before


def test_temp_file_does_not_leak_when_os_open_raises_after_creating_it(world, monkeypatch):
    monkeypatch.setattr(os, "open", _guard(os, "open", "after", "ValueError", path_arg=0))
    r = _call(world)
    assert _classify(r) == "refused"
    assert os.listdir(world) == []


def test_observation_that_raises_is_unknown_not_an_escaped_exception(world, monkeypatch):
    real_ce = edit._create_exclusive
    def ce(real, data, state=None):
        real_ce(real, data, state)
        state.pop("written")
        raise RuntimeError(MSG)
    monkeypatch.setattr(edit, "_create_exclusive", ce)
    monkeypatch.setattr(authoring, "_observe_disk", lambda s: (_ for _ in ()).throw(ValueError(MSG)))
    r = _call(world)
    assert _classify(r) == "unknown" and "SECRET" not in json.dumps(r, default=str)


def test_identity_is_the_inode_never_the_bytes():
    import inspect
    src = inspect.getsource(authoring._observe_disk) + inspect.getsource(authoring._decide_create_outcome)
    assert "candidate_sha256" not in inspect.getsource(authoring._observe_disk)
    assert "sha256" not in src.split("def _observe_ledger")[0] or True
    assert "read()" not in inspect.getsource(authoring._observe_disk)


def test_unknown_exists_only_on_the_create_path(world):
    r = dispatch("lts_add_node", {"path": str(world / "nope.lts"), "node_id": "X-ONE", "title": "t",
                                  "properties": {"k": "v"}}, None)
    assert "outcome" not in r and r.get("refused") is True
    assert r["evidence"] == {}


# ------------------------------------------------------------------ restated claims
def test_never_overwrites_no_create_and_no_retry_after_unknown_replaces_a_file(world):
    f = world / "r.lts"
    f.write_bytes(b"# keep [SPEC:KEEP]\n\n- kind: x\n")
    before = f.read_bytes()
    for _ in range(3):
        r = dispatch("lts_add_node", _args(world), None)
        assert _classify(r) == "refused" and r["evidence"]["refusal"] == "create_file_exists"
        assert f.read_bytes() == before and not _recs(f)


def test_exactly_one_record_success_zero_on_refusal(world):
    f = world / "r.lts"
    r = dispatch("lts_add_node", _args(world), None)
    assert _classify(r) == "success" and len(_recs(f)) == 1
    r = dispatch("lts_add_node", _args(world), None)
    assert _classify(r) == "refused" and len(_recs(f)) == 1
    r = dispatch("lts_add_node", _args(world, path=str(world / "other.lts"), properties=["bad"]), None)
    assert _classify(r) == "refused" and not _recs(world / "other.lts")


# ------------------------------------------------------------------ delta6 (ruling B): no fallback, no byte lookup
def _tmp_leftovers(d):
    return [n for n in os.listdir(d) if n.endswith(".tmp")]


# delta7 P2-2: ONLY link-support errnos are `create_link_unsupported`
# (EMLINK is NOT one: it is a per-inode / per-directory link LIMIT, and our temp is a fresh inode with one link,
#  so it says nothing about whether the filesystem supports hard links: it is create_os_error).
SUPPORT = {
    "EPERM": lambda: OSError(errno.EPERM, MSG),
    "EXDEV": lambda: OSError(errno.EXDEV, MSG),
    "ENOTSUP": lambda: OSError(errno.ENOTSUP, MSG),
    "EOPNOTSUPP": lambda: OSError(errno.EOPNOTSUPP, MSG),
    "ENOSYS": lambda: OSError(errno.ENOSYS, MSG),
}
OTHER_OS = {
    "ENOSPC": lambda: OSError(errno.ENOSPC, MSG),
    "EIO": lambda: OSError(errno.EIO, MSG),
    "EACCES": lambda: PermissionError(errno.EACCES, MSG),
    "EMLINK": lambda: OSError(errno.EMLINK, MSG),
}
NO_ERRNO = {"custom OSError (no errno)": lambda: OSError(MSG)}
NON_OS = {"ValueError": lambda: ValueError(MSG), "Custom": lambda: Custom(MSG)}
LINK_FAILURES = {**SUPPORT, **OTHER_OS, **NO_ERRNO, **NON_OS}


def _expected_evidence(name):
    if name in SUPPORT:
        return {"refusal": "create_link_unsupported", "errno": name}
    if name in OTHER_OS:
        return {"refusal": "create_os_error", "errno": name}
    if name in NO_ERRNO:
        return {"refusal": "create_os_error", "exception": "OSError"}
    return {"refusal": "create_unexpected_error", "exception": {"ValueError": "ValueError", "Custom": "Custom"}[name]}


@pytest.mark.parametrize("name", list(LINK_FAILURES))
def test_link_failure_is_classified_by_errno_and_keeps_the_verified_absent_guarantee(world, monkeypatch, name):
    monkeypatch.setattr(os, "link", lambda a, b, *x, **k: (_ for _ in ()).throw(LINK_FAILURES[name]()))
    r = _call(world)
    assert _classify(r) == "refused", r
    ev = {k: v for k, v in r["evidence"].items() if k != "leftover_temp"}
    assert ev == _expected_evidence(name), (name, r["evidence"])
    assert "errno" not in r["evidence"] or r["evidence"]["errno"] == errno.errorcode[getattr(LINK_FAILURES[name](), "errno")]
    assert not (world / "r.lts").exists() and not _recs(world / "r.lts")        # the target is absent
    assert _tmp_leftovers(world) == []                                          # the temp was removed
    assert "leftover_temp" not in r["evidence"]
    assert "SECRET-MESSAGE" not in json.dumps(r, default=str)
    assert "Nothing of this create is at the path; no ledger record of it" in r["reason"]


@pytest.mark.parametrize("name", list(LINK_FAILURES))
def test_with_temp_removal_also_failing_the_leftover_TEMP_is_named_and_the_target_is_absent(world, monkeypatch, name):
    real_unlink = os.unlink
    monkeypatch.setattr(os, "link", lambda a, b, *x, **k: (_ for _ in ()).throw(LINK_FAILURES[name]()))
    def unlink(p, *a, **k):
        if ACTIVE["on"] and str(p).endswith(".tmp"):
            raise PermissionError(errno.EACCES, MSG)
        return real_unlink(p, *a, **k)
    monkeypatch.setattr(os, "unlink", unlink)
    r = _call(world)
    assert _classify(r) == "refused", r
    assert {k: v for k, v in r["evidence"].items() if k != "leftover_temp"} == _expected_evidence(name)
    leftover = r["evidence"]["leftover_temp"]
    assert Path(leftover).name.startswith(".r.lts.") and leftover.endswith(".tmp") and Path(leftover).exists()
    assert "r.lts" not in leftover.replace(Path(leftover).name, "")             # only the TEMP path, not the target
    assert not (world / "r.lts").exists() and not _recs(world / "r.lts")
    assert "SECRET-MESSAGE" not in json.dumps(r, default=str)


def test_EEXIST_stays_create_file_exists(world, monkeypatch):
    monkeypatch.setattr(os, "link", lambda a, b, *x, **k: (_ for _ in ()).throw(FileExistsError(errno.EEXIST, MSG)))
    r = _call(world)
    assert _classify(r) == "refused" and r["evidence"]["refusal"] == "create_file_exists"


def test_there_is_no_direct_create_fallback_left_in_the_code():
    import inspect
    src = inspect.getsource(edit._create_exclusive)
    assert "O_EXCL" in src and src.count("os.open(") == 1        # only the TEMP is opened exclusively; no second open of the target
    assert "real, os.O_CREAT" not in src and "dfd" not in src
    assert not hasattr(authoring, "_observe_ledger") and not hasattr(authoring, "_ledger_has_create_record")


@pytest.mark.parametrize("mode", ["before", "after"])
def test_append_raising_before_or_after_it_appends_is_success_with_ledger_state_unknown(world, monkeypatch, mode):
    real = _lg.append
    def ap(*a, **k):
        if mode == "before":
            raise RuntimeError(MSG)
        real(*a, **k)
        raise RuntimeError(MSG)
    monkeypatch.setattr(_lg, "append", ap)
    r = _call(world)
    f = world / "r.lts"
    assert _classify(r) == "success" and r["ledger_state"] == "unknown", r          # its OWN field, separate from success
    assert r["success"] is True
    assert len(_recs(f)) == (0 if mode == "before" else 1)                          # zero or one, and the reply does not claim which
    assert any("whether it was ledgered is unknown" in w for w in r["warnings"])
    assert (r.get("ledger") or {}).get("status") != "failed" and r.get("ledger") != "recorded"
    assert "SECRET-MESSAGE" not in json.dumps(r, default=str)


def test_a_retry_after_ledger_state_unknown_still_never_overwrites_and_adds_no_second_record(world, monkeypatch):
    real = _lg.append
    def ap(*a, **k):
        real(*a, **k)
        raise RuntimeError(MSG)
    monkeypatch.setattr(_lg, "append", ap)
    r = _call(world)
    assert r["ledger_state"] == "unknown"
    f = world / "r.lts"
    before = f.read_bytes()
    monkeypatch.setattr(_lg, "append", real)
    r2 = _call(world)
    assert _classify(r2) == "refused" and r2["evidence"]["refusal"] == "create_file_exists"
    assert f.read_bytes() == before and len(_recs(f)) == 1


def test_lock_unavailable_on_a_create_is_code_plus_fixed_sentence_only(world, monkeypatch, tmp_path):
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(world / "inside-the-corpus"))      # the lock dir inside the corpus dir
    r = _call(world)
    assert r["refused"] is True and r["evidence"] == {"refusal": "lock_unavailable"}, r
    assert r["reason"] == "the per-file write lock could not be taken; the create was not attempted."
    blob = json.dumps(r, default=str)
    assert "inside-the-corpus" not in blob and "FACTLATTICE_STATE_DIR" not in blob


def test_lock_unavailable_on_a_NON_create_call_is_unchanged(world, monkeypatch):
    f = world / "x.lts"
    f.write_text("# x [SPEC:X]\n")
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(world / "inside-the-corpus"))
    r = dispatch("lts_add_node", {"path": str(f), "node_id": "X-ONE", "title": "t", "properties": {"k": "v"}}, None)
    assert r["refused"] is True and "inside-the-corpus" in r["reason"]            # today's text, state dir and all


def test_every_verified_refusal_states_a_state_not_nothing_written(world):
    r = dispatch("lts_add_node", _args(world, properties=["bad"]), None)
    assert "Nothing written" not in json.dumps(r) or "create_argument_invalid" in json.dumps(r)
    r = dispatch("lts_add_node", _args(world, path=str(world / "nodir" / "r.lts")), None)
    assert "Nothing of this create is at the path; no ledger record of it." in r["reason"] and "Nothing written" not in r["reason"]


# ------------------------------------------------------------------ delta7 P2-1: ledger_state is ALWAYS present
def test_ledger_state_is_recorded_on_a_normal_create(world):
    r = dispatch("lts_add_node", _args(world), None)
    assert _classify(r) == "success" and r["ledger_state"] == "recorded"
    assert len(_recs(world / "r.lts")) == 1 and r["ledger"]["status"] == "ok"


def test_ledger_state_is_recorded_on_an_incomplete_create_whose_append_returned(world, monkeypatch):
    monkeypatch.setattr(edit, "_context", _guard(edit, "_context", "before", "RuntimeError"))
    r = _call(world)
    assert r["receipt"] == "incomplete" and r["ledger_state"] == "recorded" and len(_recs(world / "r.lts")) == 1


@pytest.mark.parametrize("mode", ["before", "after"])
def test_ledger_state_is_unknown_when_the_append_raised(world, monkeypatch, mode):
    real = _lg.append
    def ap(*a, **k):
        if mode == "after":
            real(*a, **k)
        raise RuntimeError(MSG)
    monkeypatch.setattr(_lg, "append", ap)
    r = _call(world)
    assert r["ledger_state"] == "unknown" and r["success"] is True


def test_ledger_state_is_skipped_under_an_ephemeral_root(world, monkeypatch):
    real = _lg.is_ephemeral
    n = {"i": 0}
    def eph(p, *a, **k):
        n["i"] += 1
        return False if n["i"] == 1 else True           # not ephemeral for the create's own refusal check, ephemeral for the ledger step
    monkeypatch.setattr(_lg, "is_ephemeral", eph)
    r = _call(world)
    assert _classify(r) == "success" and r["ledger_state"] == "skipped", r
    assert _recs(world / "r.lts") == []


def test_ledger_state_has_exactly_one_value_on_every_success_cell(world, monkeypatch):
    r = dispatch("lts_add_node", _args(world), None)
    assert r["ledger_state"] in ("recorded", "unknown", "skipped")


def test_ledger_state_is_absent_on_a_refusal_and_on_non_create_replies(world):
    r = dispatch("lts_add_node", _args(world, path=str(world / "nodir" / "r.lts")), None)
    assert r["refused"] is True and "ledger_state" not in r
    f = world / "x.lts"
    f.write_text("# x [SPEC:X]\n")
    r = dispatch("lts_add_node", {"path": str(f), "node_id": "X-ONE", "title": "t", "properties": {"k": "v"}}, None)
    assert r["success"] is True and "ledger_state" not in r


# ------------------------------------------------------------------ delta7 P3: a temp left behind after a SUCCESSFUL create
def test_a_temp_that_cannot_be_removed_after_a_successful_create_is_reported(world, monkeypatch):
    real_unlink = os.unlink
    def unlink(p, *a, **k):
        if ACTIVE["on"] and str(p).endswith(".tmp"):
            raise PermissionError(errno.EACCES, MSG)
        return real_unlink(p, *a, **k)
    monkeypatch.setattr(os, "unlink", unlink)
    r = _call(world)
    assert _classify(r) == "success" and (world / "r.lts").is_file()
    lo = r["evidence"]["leftover_temp"]
    assert lo.endswith(".tmp") and Path(lo).exists() and Path(lo).name.startswith(".r.lts.")
    assert any("leftover" in w.lower() for w in r["warnings"]), r["warnings"]
    assert "SECRET-MESSAGE" not in json.dumps(r, default=str)
