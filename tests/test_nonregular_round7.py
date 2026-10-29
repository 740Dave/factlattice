"""Round 7 (Opus delta Diamond at 3790112: P2-A, P2-C).

P2-A  a REFUSED lts_register_corpus must not erase a boot-failed corpus's `failed_corpora` entry: after it the corpus is still listed
      as failed with its code and queries on it answer that code (the entry used to be popped BEFORE the seat).
P2-C  the register rollback test carries a real signal: every piece of kernel state the seat can leave behind is asserted absent after
      a refused seat, and the same probe reports it PRESENT after a successful one (a positive control).
"""
from __future__ import annotations

import json

import pytest

from tests.test_nonregular_file_in_root import UNREADABLE, sub_run
from tests.test_nonregular_round2 import w  # noqa: F401
from tests.test_nonregular_round6 import PRELUDE

REREG = PRELUDE + """
from factlattice_engine.server import build_tools
unsettled["on"] = True                       # the cause: acorp's member never settles while the kernel seats it
eng = build_tools(watch=False)
k = eng.store.kernel
def probe(cid):
    return {"in_store": cid in eng.store.corpus_ids(), "parts": cid in k._parts, "states": cid in k._states, "roots": cid in k._roots}
def view():
    lc = tools.dispatch("lts_list_corpora", {}, tools=eng)
    fd = tools.dispatch("lts_find", {"corpus": "acorp", "text": "An"}, tools=eng)
    return {"failed": {f["corpus_id"]: f["code"] for f in lc.get("failed_corpora", [])},
            "listed": [c["corpus_id"] for c in lc["corpora"]],
            "find": {"success": fd.get("success"), "reason": fd.get("reason"), "error_class": fd.get("error_class")}}
res = {"boot": view(), "boot_probe": probe("acorp")}
# a re-register while the cause STILL STANDS is refused ...
r = tools.dispatch("lts_register_corpus", {"corpus_id": "acorp"}, tools=eng)
res["refused"] = {k_: r.get(k_) for k_ in ("success", "reason", "error_class")}
res["after_refusal"] = view()                # ... and must leave the failed entry, the listing and the query answer untouched
res["after_probe"] = probe("acorp")
# the cause clears: the same call now succeeds, the entry goes, and the probe sees the state PRESENT (positive control)
unsettled["on"] = False
r2 = tools.dispatch("lts_register_corpus", {"corpus_id": "acorp"}, tools=eng)
res["ok"] = {"success": r2.get("success")}
res["ok_view"] = view()
res["ok_probe"] = probe("acorp")
print(json.dumps(res))
"""


def test_a_refused_reregister_keeps_the_failed_entry_and_the_query_answer(w):
    cp, _ = sub_run(["-c", REREG, str(w["roots"]["acorp"]), "unsettled"], w["env"])
    assert cp.returncode == 0, cp.stderr[-2500:]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert r["boot"]["failed"].get("acorp") == UNREADABLE and "acorp" not in r["boot"]["listed"], r
    assert r["boot_probe"] == {"in_store": False, "parts": False, "states": False, "roots": False}, r
    assert r["refused"]["success"] is False and r["refused"]["reason"] == UNREADABLE, r
    after = r["after_refusal"]
    assert after["failed"].get("acorp") == UNREADABLE, r                    # still listed as failed, with its code
    assert "acorp" not in after["listed"], r
    assert after["find"] == {"success": False, "reason": UNREADABLE, "error_class": "CorpusFileError"}, r   # NOT reason: null
    assert r["after_probe"] == {"in_store": False, "parts": False, "states": False, "roots": False}, r     # nothing left behind
    # positive control: the probe can see state, and a success removes the failed entry
    assert r["ok"]["success"] is True and r["ok_probe"] == {"in_store": True, "parts": True, "states": True, "roots": True}, r
    assert "acorp" not in r["ok_view"]["failed"] and "acorp" in r["ok_view"]["listed"], r
