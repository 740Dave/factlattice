"""Round 4 of ISSUE-NONREGULAR-MEMBER (Opus Diamond at ef4c67c: F1, F2, P2-1, P2-2, P3s).

F1  a kernel fault that is cleared WITHOUT any stat-hint change (an ACL deny-read added then removed) must not stay sticky: a later
    edit of ANOTHER file in the root is served new or STALE, never old-and-fresh; the root stays STALE while the fault stands.
F2  each typed writer (add_property, add_node, repair_property) answers a corpus-file-* condition with `reason` = the code and
    `error_class: CorpusFileError` (it used to say free text, or "does not exist").
P2-2 a raw non-permission OSError (EIO) at hash time fails CLOSED (coded corpus-file-unreadable), never serves old content as fresh.
Every case runs in a child with a hard timeout. Special files only under tmp_path.
"""
from __future__ import annotations

import errno
import json
import os

import pytest

from tests.test_nonregular_file_in_root import BROKEN, GOOD, KINDS, NOT_REGULAR, UNREADABLE, plant, sub_run
from tests.test_nonregular_round2 import w  # noqa: F401  (the registry fixture)

# ------------------------------------------------------------------------------------------------ F1 / P2-2: the kernel
KERNEL = """
import errno, json, os, sys
from factlattice.hashing import CorpusFileError
from factlattice_engine import tools
from factlattice_engine.server import build_tools

a = sys.argv[1]
mode = sys.argv[2]                       # "acl" (CorpusFileError, no hint change) | "eio" (a raw OSError)
eng = build_tools(watch=False)
k = eng.store.kernel
part = eng.store.get_partition("acorp")
os.makedirs(os.path.join(a, "sub"), exist_ok=True)
open(os.path.join(a, "sub", "b.lts"), "w").write("# B [B-1]\\n\\n## Bn [B-N]\\n\\n- status: active\\n")
# let the kernel see sub/b.lts, then settle
k.tick(); tools.dispatch("lts_find", {"corpus": "acorp", "text": "Bn"}, tools=eng)

deny = {"on": False}
real = k._file_hasher
def hasher(p):
    if deny["on"] and p.endswith("a.lts"):
        if mode == "eio":
            raise OSError(errno.EIO, "injected EIO")
        raise CorpusFileError("corpus-file-unreadable", "a.lts")
    return real(p)
k._file_hasher = hasher

def find(text):
    r = tools.dispatch("lts_find", {"corpus": "acorp", "text": text}, tools=eng)
    return {"success": r.get("success"), "stale": r.get("stale"), "reason": r.get("reason"), "dbg": json.dumps(r.get("heal"))[:600],
            "ids": sorted(n["id"] for n in r.get("nodes", []))}

res = {}
f = os.path.join(a, "a.lts")
# 1. the cause appears (injected, so mtime/size/mode do NOT move) together with an edit of a.lts
deny["on"] = True
open(f, "a").write("\\n## Extra [A-X]\\n\\n- status: active\\n")
k.tick()
res["fault_find"] = find("Extra")
res["fault_fresh"] = part.freshness().fresh
# 2. while the fault stands, ANOTHER file is edited: still stale (never old-and-fresh)
open(os.path.join(a, "sub", "b.lts"), "w").write("# B [B-1]\\n\\n## Bn [B-N]\\n\\n- status: active\\n\\n## Bnew [B-W]\\n\\n- status: active\\n")
k.tick()
res["fault_other_find"] = find("Bnew")
res["fault_other_fresh"] = part.freshness().fresh
# 3. the cause is removed; NOTHING about a.lts changes on disk (no hint change)
deny["on"] = False
res["recover_find"] = find("Extra")
# 4. a SECOND file is edited after recovery: the reply must carry the new content or be STALE, never old-and-fresh
open(os.path.join(a, "sub", "b.lts"), "w").write("# B [B-1]\\n\\n## Bn [B-N]\\n\\n- status: active\\n\\n## Bnew [B-W]\\n\\n- status: active\\n\\n## Bnewer [B-Z]\\n\\n- status: active\\n")
res["second_edit_find"] = find("Bnewer")
print(json.dumps(res))
"""


@pytest.mark.parametrize("mode", ["acl", "eio"])
def test_a_cleared_fault_is_not_sticky_and_the_root_stays_stale_while_it_stands(w, mode):
    cp, _ = sub_run(["-c", KERNEL, str(w["roots"]["acorp"]), mode], w["env"])
    assert cp.returncode == 0, cp.stderr[-2500:]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    # while the fault stands: STALE, coded, never the old content
    assert r["fault_fresh"] is False and r["fault_find"]["success"] is False and r["fault_find"]["stale"] is True, r
    assert r["fault_find"]["reason"] == UNREADABLE, r
    assert r["fault_other_fresh"] is False and r["fault_other_find"]["success"] is False, r
    # after the cause is removed (no hint change): the edit of a.lts is served
    assert r["recover_find"]["success"] is True and r["recover_find"]["ids"] == ["A-X"], r
    # a later edit of ANOTHER file: new content, or STALE; NEVER old-and-fresh
    sf = r["second_edit_find"]
    assert (sf["success"] is True and sf["ids"] == ["B-Z"]) or sf["success"] is False, sf


# ------------------------------------------------------------------------------------------------ P2-2 at the hashing level
HASH_EIO = """
import errno, json, os, sys
import factlattice.hashing as H
real_fstat = os.fstat
def bad_fstat(fd):
    import sys as _s
    if _s._getframe(1).f_code.co_filename.endswith("hashing.py"):
        raise OSError(errno.EIO, "injected EIO")
    return real_fstat(fd)
os.fstat = bad_fstat
out = {}
for name, fn in (("hash", H.file_content_hash), ("read", H.safe_read_bytes)):
    try:
        fn(sys.argv[1]); out[name] = None
    except Exception as e:
        out[name] = [type(e).__name__, getattr(e, "code", None)]
print(json.dumps(out))
"""


def test_a_raw_eio_at_hash_or_read_time_is_a_coded_unreadable(w, tmp_path):
    f = tmp_path / "ok.lts"
    f.write_text(GOOD)
    cp, _ = sub_run(["-c", HASH_EIO, str(f)], w["env"])
    assert cp.returncode == 0, cp.stderr[-1500:]
    assert json.loads(cp.stdout.strip().splitlines()[-1]) == {"hash": ["CorpusFileError", UNREADABLE],
                                                                "read": ["CorpusFileError", UNREADABLE]}


# ------------------------------------------------------------------------------------------------ F2: the typed writers
WRITER = """
import json, sys
from factlattice_engine import tools
which, path = sys.argv[1], sys.argv[2]
args = {"lts_add_property": {"path": path, "node_id": "A-1", "key": "k", "value": "v"},
        "lts_add_node": {"path": path, "node_id": "NEW-1", "title": "New"},
        "lts_repair_property": {"path": path, "node_id": "A-1", "line": 3, "key": "k"}}[which]
r = tools.dispatch(which, args, tools=None)
print(json.dumps({"success": r.get("success"), "refused": r.get("refused"), "written": r.get("written"),
                  "reason": r.get("reason"), "error_class": r.get("error_class"), "path": r.get("path"),
                  "evidence": r.get("evidence"), "text": json.dumps(r)[:1500]}))
"""
WRITERS = ["lts_add_property", "lts_add_node", "lts_repair_property"]
CONDITIONS = ["fifo", "socket", "symlink-to-fifo", "broken-symlink", "symlink-loop", "unreadable"]


@pytest.mark.parametrize("kind", CONDITIONS)
@pytest.mark.parametrize("which", WRITERS)
def test_each_typed_writer_answers_a_corpus_file_condition_with_the_code(w, tmp_path, which, kind):
    root = tmp_path / "wr"
    plant(kind, root)
    cp, _ = sub_run(["-c", WRITER, which, str(root / "bad.lts")], w["env"])
    assert cp.returncode == 0, cp.stderr[-1500:]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert r["success"] is False and r["refused"] is True and r["written"] is False, r
    assert r["reason"] == KINDS[kind], r
    assert r["error_class"] == "CorpusFileError", r
    assert (r["evidence"] or {}).get("refusal") == KINDS[kind], r             # the query log reads the code from here
    assert "does not exist" not in r["text"] and str(tmp_path) not in r["reason"], r
    assert r["path"] == "bad.lts"                                             # root-relative / basename, never absolute


def test_a_missing_file_still_gets_the_writers_own_message(w, tmp_path):
    cp, _ = sub_run(["-c", WRITER, "lts_add_property", str(tmp_path / "nope.lts")], w["env"])
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert r["refused"] is True and r["reason"] not in (NOT_REGULAR, BROKEN, UNREADABLE)


# ------------------------------------------------------------------------------------------------ P2-1 / P3: honest docs
def test_the_tripwire_docstring_states_the_exemption_and_alias_blind_spots():
    from tests import test_nonregular_round2 as r2
    doc = r2.test_tripwire_no_raw_walk_or_read_of_corpus_files_outside_the_safe_module.__doc__
    for needle in ("EXEMPTED FUNCTION", "stays green", "import x as y", "eval", "O_RDONLY|O_CREAT"):
        assert needle in doc, needle


@pytest.mark.parametrize("form,src", [
    ("module alias", "import shutil as sh\n\ndef f(p, q):\n    sh.copyfile(p, q)\n"),
    ("numpy alias", "import numpy as npy\n\ndef f(p):\n    return npy.load(p)\n"),
    ("aliased from-import of listdir", "from os import listdir as ld\n\ndef f(p):\n    return ld(p)\n"),
])
def test_the_tripwire_now_sees_module_aliases(tmp_path, form, src):
    from tests.test_nonregular_round3 import _scan_text
    assert _scan_text(tmp_path, src), form


def test_the_sdlc_row_and_report_state_the_writer_shape_truthfully():
    from tests.test_nonregular_file_in_root import REPO
    sdlc = (REPO / "SDLC.md").read_text(encoding="utf-8")
    row = next(l for l in sdlc.splitlines() if l.startswith("- [ ] [CORPUS-MEMBER-IS-A-READABLE-REGULAR-FILE]"))
    assert "typed writers" in row and "evidence.refusal" in row
    assert "NOT covered by a per-function check" in row                      # the exempted-function blind spot is stated
