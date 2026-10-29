"""Round 6 (Opus delta Diamond at edb399f: P1-A, P2-1).

P1-A  a CorpusFileError raised while the kernel SEATS one corpus (a member that never settles, or turns unreadable, just after
      build_store) is CONTAINED: at boot that corpus is a failed corpus with its code and the server comes up with the others;
      at lts_register_corpus the registration rolls back completely with a coded refusal, and a retry after the cause clears succeeds.
P2-1  the tests round 5 left out: the modified-file race, the three-moves outcome at runtime, a directory EIO at depth and at runtime.
Every case runs in a child with a hard timeout.
"""
from __future__ import annotations

import json

import pytest

from tests.test_nonregular_file_in_root import UNREADABLE, sub_run
from tests.test_nonregular_round2 import w  # noqa: F401

PRELUDE = """
import errno, itertools, json, os, sys, threading, time
from factlattice.hashing import CorpusFileError
from factlattice_engine import tools, kernel as K
a, mode = sys.argv[1], sys.argv[2]
counter = itertools.count()
real_hint = K.FreshnessKernel._safe_hint
unsettled = {"on": False}
def hint(self, p):
    if unsettled["on"] and p.endswith("a.lts"):
        return (float(next(counter)), 1, 0o644)       # moves on EVERY look: the member never settles
    return real_hint(self, p)
K.FreshnessKernel._safe_hint = hint
real_hash_file = K.FreshnessKernel._hash_file
def hash_file(self, p, rel=None):
    if mode == "unreadable_at_seat" and unsettled["on"] and p.endswith("a.lts"):
        raise CorpusFileError(UNREADABLE_CODE, "a.lts")
    return real_hash_file(self, p, rel)
UNREADABLE_CODE = "corpus-file-unreadable"
K.FreshnessKernel._hash_file = hash_file
"""

BOOT = PRELUDE + """
from factlattice_engine.server import build_tools
stop = threading.Event()
def writer():                                        # a REAL in-place writer, rewriting the member as fast as it can
    f = os.path.join(a, "a.lts")
    while not stop.is_set():
        with open(f, "r+") as fh:
            body = fh.read(); fh.seek(0); fh.write(body); fh.flush()
if mode == "thread":
    t = threading.Thread(target=writer, daemon=True); t.start()
else:
    unsettled["on"] = True
try:
    eng = build_tools(watch=False)
    lc = tools.dispatch("lts_list_corpora", {}, tools=eng)
    out = {"booted": True, "loaded": [c["corpus_id"] for c in lc["corpora"]],
           "failed": {f["corpus_id"]: f["code"] for f in lc.get("failed_corpora", [])}}
except BaseException as e:
    out = {"booted": False, "exc": type(e).__name__, "code": getattr(e, "code", None)}
stop.set()
print(json.dumps(out))
"""


@pytest.mark.parametrize("mode", ["unsettled", "unreadable_at_seat"])
def test_a_corpus_that_fails_while_the_kernel_seats_it_is_a_failed_corpus_and_the_server_boots(w, mode):
    cp, _ = sub_run(["-c", BOOT, str(w["roots"]["acorp"]), mode], w["env"])
    assert cp.returncode == 0, cp.stderr[-2500:]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert r["booted"] is True, r                                         # the server comes up
    assert "ccorp" in r["loaded"] and "acorp" not in r["loaded"], r       # the others serve; THAT corpus does not
    assert r["failed"].get("acorp") == UNREADABLE, r                      # and says why


def test_a_real_writer_rewriting_a_member_in_place_cannot_stop_the_server_booting(w):
    cp, _ = sub_run(["-c", BOOT, str(w["roots"]["acorp"]), "thread"], w["env"], timeout=60)
    assert cp.returncode == 0, cp.stderr[-2500:]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert r["booted"] is True, r
    assert "ccorp" in r["loaded"], r
    if "acorp" not in r["loaded"]:                                         # lucky timing may let it settle; otherwise it is coded
        assert r["failed"].get("acorp") == UNREADABLE, r


REGISTER = PRELUDE + """
from factlattice_engine.server import build_tools
eng = build_tools(watch=False)
spec = {"root": a, "domain_suffix": "p.adhoc"}
unsettled["on"] = True
r1 = tools.dispatch("lts_register_corpus", {"corpus_id": "adhoc", "spec": spec}, tools=eng)
k = eng.store.kernel
state = {"loaded": "adhoc" in eng.store.corpus_ids(), "seated": k.current_root(eng.store.get_partition("adhoc")) if "adhoc" in eng.store.corpus_ids() else None,
         "tracked": "adhoc" in k._parts}
unsettled["on"] = False
r2 = tools.dispatch("lts_register_corpus", {"corpus_id": "adhoc", "spec": spec}, tools=eng)
print(json.dumps({"first": {k_: r1.get(k_) for k_ in ("success", "reason", "error_class", "path")}, "state": state,
                  "retry": {k_: r2.get(k_) for k_ in ("success", "kernel_seated", "error")}}))
"""


@pytest.mark.parametrize("mode", ["unsettled", "unreadable_at_seat"])
def test_register_rolls_back_fully_on_a_seat_failure_and_a_retry_succeeds(w, tmp_path, mode):
    adhoc = tmp_path / "adhoc"
    adhoc.mkdir()
    (adhoc / "a.lts").write_text("# A [A-1]\n\n## An [A-N]\n\n- status: active\n")
    cp, _ = sub_run(["-c", REGISTER, str(adhoc), mode], w["env"])
    assert cp.returncode == 0, cp.stderr[-2500:]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert r["first"]["success"] is False and r["first"]["reason"] == UNREADABLE and r["first"]["error_class"] == "CorpusFileError", r
    assert r["state"] == {"loaded": False, "seated": None, "tracked": False}, r        # nothing half-registered
    assert r["retry"]["success"] is True and r["retry"]["kernel_seated"] is True, r    # the retry works once the cause clears


# ------------------------------------------------------------------------------------------------ P2-1
RUNTIME = """
import errno, json, os, sys
from factlattice.hashing import CorpusFileError
from factlattice_engine import tools
from factlattice_engine.server import build_tools
a, scenario = sys.argv[1], sys.argv[2]
f = os.path.join(a, "a.lts")
eng = build_tools(watch=False)
k = eng.store.kernel
part = eng.store.get_partition("acorp")
LATE = "\\n## Late [A-Y]\\n\\n- status: active\\n"
state = {"n": 0}
real = k._file_hasher
def find(t):
    r = tools.dispatch("lts_find", {"corpus": "acorp", "text": t}, tools=eng)
    return {"success": r.get("success"), "reason": r.get("reason"), "ids": sorted(n["id"] for n in r.get("nodes", []))}
res = {}
if scenario == "modified_race":
    def h(p):
        x = real(p)
        if p.endswith("a.lts"):
            state["n"] += 1
            if state["n"] == 1:
                open(p, "a").write(LATE)            # a write lands after the hash of a MODIFIED member
        return x
    k._file_hasher = h
    open(f, "a").write("\\n## Extra [A-X]\\n\\n- status: active\\n")
    k.tick()
    res["first"] = find("Late"); k.tick(); res["second"] = find("Late")
elif scenario == "never_settles":
    import itertools
    c = itertools.count()
    import factlattice_engine.kernel as K
    real_hint = K.FreshnessKernel._safe_hint
    K.FreshnessKernel._safe_hint = lambda self, p: (float(next(c)), 1, 0o644) if p.endswith("a.lts") else real_hint(self, p)
    open(f, "a").write("\\n## Extra [A-X]\\n\\n- status: active\\n")
    k.tick()
    res["find"] = find("Extra"); res["fresh"] = part.freshness().fresh
elif scenario == "dir_eio_runtime":
    import factlattice.hashing as H
    real_walk = os.walk
    def bad_walk(top, *a_, **k_):
        if str(top).endswith("acorp"):
            raise CorpusFileError("corpus-dir-unreadable", "sub")
        return real_walk(top, *a_, **k_)
    os.walk = bad_walk
    k.tick()
    res["find"] = find("An"); res["fresh"] = part.freshness().fresh
print(json.dumps(res))
"""


def test_the_modified_file_race_is_never_served_old_as_fresh(w):
    cp, _ = sub_run(["-c", RUNTIME, str(w["roots"]["acorp"]), "modified_race"], w["env"])
    assert cp.returncode == 0, cp.stderr[-2000:]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    for which in ("first", "second"):
        assert r[which]["success"] is False or r[which]["ids"] == ["A-Y"], r
    assert r["second"]["success"] is True and r["second"]["ids"] == ["A-Y"], r


def test_a_member_that_never_settles_fails_the_root_closed_at_runtime(w):
    cp, _ = sub_run(["-c", RUNTIME, str(w["roots"]["acorp"]), "never_settles"], w["env"])
    assert cp.returncode == 0, cp.stderr[-2000:]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert r["fresh"] is False and r["find"]["success"] is False and r["find"]["reason"] == UNREADABLE, r


def test_a_directory_error_at_runtime_makes_the_corpus_stale_not_fresh(w):
    cp, _ = sub_run(["-c", RUNTIME, str(w["roots"]["acorp"]), "dir_eio_runtime"], w["env"])
    assert cp.returncode == 0, cp.stderr[-2000:]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert r["fresh"] is False and r["find"]["success"] is False, r


DEPTH = """
import errno, json, os
real = os.scandir
def bad(path="."):
    if str(path).endswith(os.path.join("acorp", "deep", "er")):
        raise OSError(errno.EIO, "injected EIO", str(path))
    return real(path)
os.scandir = bad
from factlattice_engine import config
from factlattice import registry
import sys
root = sys.argv[1]
os.makedirs(os.path.join(root, "deep", "er"), exist_ok=True)
store = config.build_store(registry.resolve_registry().corpora)
print(json.dumps({"loaded": sorted(p.corpus_id for p in store.partitions()), "failed": store.failed_corpora}))
"""


def test_a_directory_eio_at_depth_fails_only_its_corpus_with_the_relative_directory(w):
    cp, _ = sub_run(["-c", DEPTH, str(w["roots"]["acorp"])], w["env"])
    assert cp.returncode == 0, cp.stderr[-2000:]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert "ccorp" in r["loaded"] and "acorp" not in r["loaded"], r
    assert r["failed"]["acorp"] == {"code": "corpus-dir-unreadable", "path": "deep/er", "private": False}, r
