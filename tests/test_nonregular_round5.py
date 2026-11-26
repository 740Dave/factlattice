"""Round 5 (Opus delta Diamond at da2f0e3: P2-A, P2-B, P2-C).

P2-A  the stat hint is taken BEFORE the hash and re-checked AFTER: a write that lands between the hash and the hint can never be
      recorded as 'unchanged' (it used to be served old-as-fresh, ~30 s with the watcher, forever without it).
P2-B  an EIO while LISTING a directory fails only its own corpus (coded corpus-dir-unreadable), not the whole registry.
P2-C  the SDLC row states the three round-4 extension points.
Each case runs in a child with a hard timeout.
"""
from __future__ import annotations

import json

import pytest

from tests.test_nonregular_file_in_root import REPO, sub_run
from tests.test_nonregular_round2 import w  # noqa: F401

RACE = """
import json, os, sys
from factlattice.hashing import CorpusFileError
from factlattice_engine import tools
from factlattice_engine.server import build_tools

a, scenario = sys.argv[1], sys.argv[2]
f = os.path.join(a, "a.lts")
V1 = open(f).read()
eng = build_tools(watch=False)
k = eng.store.kernel
part = eng.store.get_partition("acorp")
st = {"deny": False, "n": 0, "arm": None}
real = k._file_hasher
LATE = "\\n## Late [A-Y]\\n\\n- status: active\\n"
def hasher(p):
    if p.endswith("a.lts"):
        if st["deny"]:
            raise CorpusFileError("corpus-file-unreadable", "a.lts")
        h = real(p)
        if st["arm"] is not None:
            st["n"] += 1
            if st["n"] == st["arm"]:                 # a WRITE lands between the hash and whatever is recorded after it
                open(p, "a").write(LATE)
        return h
    return real(p)
k._file_hasher = hasher

def find(t):
    r = tools.dispatch("lts_find", {"corpus": "acorp", "text": t}, tools=eng)
    return {"success": r.get("success"), "ids": sorted(n["id"] for n in r.get("nodes", []))}

res = {}
if scenario == "poll":
    # fault, then the file is put back to its indexed content and the cause removed; the recovery re-hash races a write
    st["deny"] = True
    open(f, "a").write("\\n## Extra [A-X]\\n\\n- status: active\\n")
    k.tick()
    open(f, "w").write(V1)
    st["deny"] = False
    st["arm"], st["n"] = 2, 0                        # hash #1 = the probe of the bad member, #2 = the re-add of the leaf
    k.tick()
elif scenario == "reload":
    st["arm"], st["n"] = 1, 0
    k.full_reverify(part)
res["first"] = find("Late")
k.tick()
res["second"] = find("Late")
print(json.dumps(res))
"""


@pytest.mark.parametrize("scenario", ["poll", "reload"])
def test_a_write_between_the_hash_and_the_hint_is_never_served_old_as_fresh(w, scenario):
    cp, _ = sub_run(["-c", RACE.replace('"""', ""), str(w["roots"]["acorp"]), scenario], w["env"])
    assert cp.returncode == 0, cp.stderr[-2500:]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    for which in ("first", "second"):
        got = r[which]
        # the new content, or STALE (success false); NEVER old content served as fresh
        assert got["success"] is False or got["ids"] == ["A-Y"], (which, r)
    assert r["second"]["success"] is True and r["second"]["ids"] == ["A-Y"], r      # and it converges


DIR_EIO = """
import errno, json, os, sys
real = os.scandir
def bad(path="."):
    if str(path).endswith("acorp"):
        raise OSError(errno.EIO, "injected EIO")
    return real(path)
os.scandir = bad
from factlattice_engine import config
from factlattice import registry
store = config.build_store(registry.resolve_registry().corpora)
print(json.dumps({"loaded": sorted(p.corpus_id for p in store.partitions()), "failed": store._failed_corpora}))
"""


def test_an_eio_while_listing_a_directory_fails_only_its_own_corpus(w):
    cp, _ = sub_run(["-c", DIR_EIO], w["env"])
    assert cp.returncode == 0, cp.stderr[-2000:]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert "ccorp" in r["loaded"] and "acorp" not in r["loaded"], r
    assert r["failed"]["acorp"]["code"] == "corpus-dir-unreadable", r


def test_the_sdlc_row_states_the_round_4_extension_points():
    sdlc = (REPO / "SDLC.md").read_text(encoding="utf-8")
    row = next(l for l in sdlc.splitlines() if l.startswith("- [ ] [CORPUS-MEMBER-IS-A-READABLE-REGULAR-FILE]"))
    for needle in ("RootState.fault", "stale while", "cleared when", "_io_error", "name-gone", "_writer_target_error",
                   "CHECK-THEN-USE", "hint BEFORE the hash"):
        assert needle in row, needle


@pytest.mark.skipif(not (REPO / "docs" / "build-logs").is_dir(),
                    reason="docs/build-logs/ is not in a published snapshot (PUBLISH-EXCLUDE removes it)")
def test_the_report_carries_the_round_4_gate_line_and_the_round_5_section():
    rep = (REPO / "docs/build-logs/BUILD-REPORT-nonregular-file-2026-10-08.md").read_text(encoding="utf-8")
    assert "### Round 4 gate line" in rep and "4799 passed, 25 skipped in 1891.53s (0:31:31)" in rep
    assert "### Round 5 gate line" in rep
