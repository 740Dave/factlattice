"""[delta3] The reply, the filesystem and the ledger must AGREE about a create, wherever an exception lands.

An exception raised BEFORE the file is linked into place is a coded refusal and nothing exists. An exception
raised AFTER the link leaves the file ON DISK, so the reply must never say refused / nothing written: it is
`success: true, written: true, receipt: "incomplete"`, with `ledger: "recorded"` iff `_ledger_append` RETURNED and
otherwise its OWN field `ledger_state: "unknown"` (zero or one record exists; there is no lookup). Injection points are the delta-3 review's seven, plus a before-the-link control."""
from __future__ import annotations

import json
import logging
import os

import pytest

from factlattice import ledger as _ledger
from factlattice.authoring import edit
from factlattice_engine.tools import dispatch


@pytest.fixture()
def out(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    d = tmp_path / "reviews"
    d.mkdir()
    return d


def _go(out):
    f = out / "r.lts"
    r = dispatch("lts_add_node", {"path": str(f), "node_id": "REVIEW-K9", "title": "t",
                                  "properties": {"kind": "review"}, "create": True, "doc_title": "t",
                                  "domain": "reviews", "doctype": "review"}, None)
    return f, r


def _records(f):
    return _ledger.read_lines(os.path.realpath(f)) or []


def _boom(msg="SECRET-MESSAGE-with-/private/path"):
    raise RuntimeError(msg)


def _after(real_fn, raise_when="after"):
    """Wrap `real_fn` so it raises RuntimeError before or after doing its work."""
    def w(*a, **k):
        if raise_when == "before":
            _boom()
        v = real_fn(*a, **k)
        _boom()
        return v
    return w


def _agree(out, f, r, *, expect_written):
    recs = _records(f)
    leftovers = [n for n in os.listdir(out) if n != "r.lts"]
    assert not leftovers, leftovers
    assert "SECRET-MESSAGE" not in json.dumps(r, default=str)                 # no exception text, ever
    if not expect_written:
        assert r["refused"] is True and r["success"] is False and r["written"] is False, r
        assert r["evidence"] == {"refusal": "create_unexpected_error", "exception": "RuntimeError"}, r
        assert not f.exists() and recs == []                                  # reply, disk, ledger agree
        return
    assert f.is_file(), "the file is on disk"
    assert r["success"] is True and r["written"] is True and r["refused"] is False, r
    assert r.get("receipt") in ("incomplete", "compact", "full")
    _ledger_claim(r, recs)
    if r.get("receipt") == "incomplete":
        assert r["receipt_error"] == "RuntimeError"
        assert any("re-read" in w for w in r["warnings"]), r


def _ledger_claim(r, recs):
    """`ledger: recorded` (or an ok dict) iff _ledger_append returned: exactly one record. Otherwise the OWN
    field `ledger_state: unknown`, with the fixed warning: zero or one record."""
    if r.get("ledger_state") == "unknown":
        assert len(recs) in (0, 1), recs
        assert any("whether it was ledgered is unknown" in w for w in r["warnings"]), r
    else:
        assert r.get("ledger") == "recorded" or (isinstance(r.get("ledger"), dict) and r["ledger"].get("status") == "ok"), r
        assert len(recs) == 1, recs


def test_control_before_the_link_is_a_refusal_and_nothing_exists(out, monkeypatch):
    monkeypatch.setattr(edit, "_gate_new_node", lambda *a, **k: _boom())
    f, r = _go(out)
    _agree(out, f, r, expect_written=False)


def test_control_no_injection_is_a_normal_success(out):
    f, r = _go(out)
    assert r["success"] is True and r["created"] is True and len(_records(f)) == 1 and "receipt" in r
    assert r["receipt"] != "incomplete"


def test_1_after_the_link_inside_the_core(out, monkeypatch):
    real = os.link
    def link_then_boom(a, b, *x, **k):
        real(a, b, *x, **k)
        _boom()
    monkeypatch.setattr(os, "link", link_then_boom)
    f, r = _go(out)
    _agree(out, f, r, expect_written=True)


def test_2_after__create_exclusive_before_the_ledger_append(out, monkeypatch):
    monkeypatch.setattr(edit, "_create_exclusive", _after(edit._create_exclusive))
    f, r = _go(out)
    _agree(out, f, r, expect_written=True)
    assert r["ledger_state"] == "unknown" and _records(f) == []


def test_3_inside__ledger_append_before_it_appends(out, monkeypatch):
    real = _ledger.is_ephemeral
    def late(path, *a, **k):
        if os.path.lexists(str(path)):          # only the call made AFTER the file exists (inside _ledger_append)
            _boom()
        return real(path, *a, **k)
    monkeypatch.setattr(_ledger, "is_ephemeral", late)
    f, r = _go(out)
    _agree(out, f, r, expect_written=True)
    assert r["ledger_state"] == "unknown" and _records(f) == []


def test_4_inside__ledger_append_after_it_appends(out, monkeypatch):
    monkeypatch.setattr(_ledger, "append", _after(_ledger.append))     # the record IS written, then it raises
    f, r = _go(out)
    assert f.is_file() and len(_records(f)) == 1                       # disk and ledger agree: written, recorded
    assert r["refused"] is False and r["success"] is True and r["written"] is True
    assert r["success"] is True and r["ledger_state"] == "unknown"   # append raised: zero or one record, here one
    assert "ledger" not in r or r["ledger"] != "recorded"
    assert "SECRET-MESSAGE" not in json.dumps(r, default=str)         # F4: no exception message on ANY create reply


def test_5_in_oob_warnings(out, monkeypatch):
    monkeypatch.setattr(edit, "oob_warnings", lambda *a, **k: _boom())
    f, r = _go(out)
    _agree(out, f, r, expect_written=True)
    assert len(_records(f)) == 1 and r["ledger"] == "recorded"


def test_6_in__context(out, monkeypatch):
    real = edit._context
    monkeypatch.setattr(edit, "_context", lambda *a, **k: _boom())
    f, r = _go(out)
    _agree(out, f, r, expect_written=True)
    assert len(_records(f)) == 1


def test_7_in__serialised_finally__hash_cache(out, monkeypatch):
    real = edit._hash_cache
    def late():
        if (out / "r.lts").exists():
            _boom()
        return real()
    monkeypatch.setattr(edit, "_hash_cache", late)
    f, r = _go(out)
    _agree(out, f, r, expect_written=True)        # (the hash cache is also read inside _ledger_append, whose
    #                                               own guard turns that into a failed ledger: no record, so "missing")


@pytest.mark.parametrize("kind", ["before", "after"])
def test_one_server_side_log_line_names_type_and_stage_but_no_message(out, monkeypatch, caplog, kind):
    if kind == "before":
        monkeypatch.setattr(edit, "_gate_new_node", lambda *a, **k: _boom())
    else:
        monkeypatch.setattr(edit, "_context", lambda *a, **k: _boom())
    with caplog.at_level(logging.WARNING):
        f, r = _go(out)
    lines = [rec for rec in caplog.records if "create" in rec.getMessage() and "RuntimeError" in rec.getMessage()]
    assert len(lines) == 1, [x.getMessage() for x in caplog.records]
    msg = lines[0].getMessage()
    assert lines[0].levelno == logging.WARNING
    assert ("before_write" if kind == "before" else "after_write") in msg
    assert "SECRET-MESSAGE" not in msg and str(out) not in msg
    assert lines[0].exc_info is None                                    # no traceback (a message could ride in it)


def test_a_concurrent_identical_creator_is_still_the_coded_exists_refusal(out):
    f, r1 = _go(out)
    assert r1["success"] is True
    f, r2 = _go(out)
    assert r2["refused"] is True and r2["evidence"]["refusal"] == "create_file_exists"
    assert len(_records(f)) == 1



# ===================================================== delta4: ONE observation point, the whole matrix
from factlattice.authoring.edit import EditRefused  # noqa: E402

MSG = "SECRET-MESSAGE-with-/private/path"
EXC = {
    "RuntimeError": lambda: RuntimeError(MSG),
    "OSError": lambda: OSError(5, MSG),
    "PermissionError": lambda: PermissionError(13, MSG),
    "FileExistsError": lambda: FileExistsError(17, MSG),
    "EditRefused": lambda: EditRefused(MSG),
}


def _raiser(kind):
    def f(*a, **k):
        raise EXC[kind]()
    return f


def _then(real, kind):
    def f(*a, **k):
        v = real(*a, **k)
        raise EXC[kind]()
    return f


def _p_gate(mp, out, kind):                    # control: before the link
    mp.setattr(edit, "_gate_new_node", _raiser(kind))


def _p_link_before(mp, out, kind):             # the link call itself fails, nothing linked
    mp.setattr(os, "link", _raiser(kind))


def _p_link_after(mp, out, kind):              # real link, THEN the call raises (NFS-style: link done, reply lost)
    mp.setattr(os, "link", _then(os.link, kind))


def _p_exclusive_after(mp, out, kind):
    mp.setattr(edit, "_create_exclusive", _then(edit._create_exclusive, kind))


def _p_ledger_append_before(mp, out, kind):
    mp.setattr(edit, "_ledger_append", _raiser(kind))


def _p_ledger_append_after(mp, out, kind):
    mp.setattr(edit, "_ledger_append", _then(edit._ledger_append, kind))


def _p_oob(mp, out, kind):
    mp.setattr(edit, "oob_warnings", _raiser(kind))


def _p_context(mp, out, kind):
    mp.setattr(edit, "_context", _raiser(kind))


def _p_last_ledger(mp, out, kind):
    mp.setattr(edit, "last_ledger", _raiser(kind))


def _p_finally(mp, out, kind):                 # _serialised's finally (_hash_cache), only once the file exists
    real = edit._hash_cache
    def late():
        if (out / "r.lts").exists():
            raise EXC[kind]()
        return real()
    mp.setattr(edit, "_hash_cache", late)


POINTS = {"gate(before link)": _p_gate, "link raises before": _p_link_before, "link raises after": _p_link_after,
          "after _create_exclusive": _p_exclusive_after, "_ledger_append before": _p_ledger_append_before,
          "_ledger_append after": _p_ledger_append_after, "oob_warnings": _p_oob, "_context": _p_context,
          "last_ledger": _p_last_ledger, "_serialised finally": _p_finally}


def _check(out, f, r, *, authored_message=False):
    """reply vs filesystem vs ledger. Returns whether the file is on disk.

    `authored_message`: the ONE documented exception to "no message in a create reply": an EditRefused raised
    inside the two validators that the create path wraps with a code (`_coerce_properties`, `_gate_new_node`)
    is indistinguishable from the validators' own AUTHORED refusals, whose text is deterministic and is the
    caller's feedback (it names the offending key). Every other EditRefused is uncoded and gets a fixed sentence."""
    recs = _records(f)
    on_disk = f.is_file()
    blob = json.dumps(r, default=str)
    if not authored_message:
        assert "SECRET-MESSAGE" not in blob and "/private/path" not in blob, "an exception message reached the reply"
    assert not [n for n in os.listdir(out) if n != "r.lts"], os.listdir(out)
    if not on_disk:
        assert r.get("refused") is True and r.get("success") is False and r.get("written") is False, r
        assert (r.get("evidence") or {}).get("refusal"), r
        assert recs == [], "refused but a ledger record exists"
        return False
    assert r.get("success") is True and r.get("written") is True and r.get("refused") is False, r
    _ledger_claim(r, recs)
    return True


@pytest.mark.parametrize("kind", list(EXC))
@pytest.mark.parametrize("point", list(POINTS))
def test_matrix_reply_filesystem_and_ledger_agree(out, monkeypatch, point, kind):
    POINTS[point](monkeypatch, out, kind)
    f, r = _go(out)
    on_disk = _check(out, f, r, authored_message=(point == "gate(before link)" and kind == "EditRefused"))
    if point == "gate(before link)":
        assert on_disk is False                     # the control really is before the link
    if point in ("after _create_exclusive", "_ledger_append before", "_ledger_append after", "oob_warnings",
                 "_context", "last_ledger", "link raises after"):
        assert on_disk is True                      # these really are after the link


def test_matrix_size():
    assert len(POINTS) * len(EXC) >= 50


@pytest.mark.parametrize("kind", ["before", "after"])
def test_inside_ledger_append_guard_swallows_but_no_message_leaks(out, monkeypatch, kind):
    real = _ledger.append
    monkeypatch.setattr(_ledger, "append", _raiser("OSError") if kind == "before" else _then(real, "OSError"))
    f, r = _go(out)
    assert _check(out, f, r) is True


def test_a_concurrent_set_property_between_the_link_and_the_reply_cannot_flip_the_answer(out, monkeypatch):
    from factlattice_engine import authoring
    real = authoring._membership.annotate
    def concurrent_then_boom(o, p, **k):
        edit.set_property(p, "REVIEW-K9", "verdict", "pass")           # the lock is free again: another writer
        raise RuntimeError(MSG)
    monkeypatch.setattr(authoring._membership, "annotate", concurrent_then_boom)
    f, r = _go(out)
    assert f.is_file() and r["success"] is True and r["written"] is True and r["receipt"] == "incomplete"
    assert r["ledger"] == "recorded" and len(_records(f)) == 2          # [create, set_property]
    assert "SECRET-MESSAGE" not in json.dumps(r, default=str)


def test_F2_a_link_that_raises_after_linking_is_OUR_write_not_create_file_exists(out, monkeypatch):
    monkeypatch.setattr(os, "link", _then(os.link, "OSError"))
    f, r = _go(out)
    assert f.is_file() and r["success"] is True and r["written"] is True
    assert (r.get("evidence") or {}).get("refusal") != "create_file_exists"
    assert len(_records(f)) == 1


def test_F2_two_identical_concurrent_creators_still_leave_the_loser_a_refusal(out):
    f, r1 = _go(out)
    f, r2 = _go(out)                                                   # byte-identical request, file exists
    assert r1["success"] is True and r2["refused"] is True and r2["evidence"]["refusal"] == "create_file_exists"


def test_F3_ledger_presence_is_not_decided_by_the_last_line(out, monkeypatch):
    real = edit._ledger_append
    def append_then_other_write_then_boom(*a, **k):
        real(*a, **k)
        edit.set_property(a[0], "REVIEW-K9", "verdict", "pass")       # becomes the ledger's LAST line
        raise RuntimeError(MSG)
    monkeypatch.setattr(edit, "_ledger_append", append_then_other_write_then_boom)
    f, r = _go(out)
    assert r["success"] is True and r["ledger"] == "recorded", r


def test_F4_no_message_in_a_normal_ledger_failure_reply_either(out, monkeypatch):
    monkeypatch.setattr(_ledger, "append", _raiser("RuntimeError"))     # nothing appended
    f, r = _go(out)
    assert r["success"] is True and "SECRET-MESSAGE" not in json.dumps(r, default=str)
    assert r["ledger_state"] == "unknown" and _records(f) == []
