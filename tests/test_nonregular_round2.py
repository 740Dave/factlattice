"""Round 2 of ISSUE-NONREGULAR-MEMBER (Diamond d533ebc: F1-F4 and the P2s).

F1  a failed PRIVATE corpus is never NAMED on an unnamed call (counted anonymously); named only when the caller named it.
F2  a member that turns unreadable AFTER boot never serves FRESH (chmod, and chmod plus an edit); one bad corpus never stops
    the others from being polled.
F3  EVERY reader of corpus files is routed through the safe lister/reader: a FIFO anywhere cannot hang a tool (each reader is
    driven through a FIFO under a subprocess timeout).
F4  every corpus-OMITTED fan-out tool names the failed corpora (driven from the tool list, not a hand-picked list).
P2  coded replies for lint/preview/the lint CLI, `lts_preview corpus=<failed id>`, an unreadable DIRECTORY fails only its own
    corpus (`corpus-dir-unreadable`), the descriptions state the new surface, and an AST TRIPWIRE keeps new readers honest.

Special files are planted ONLY under pytest's tmp_path. Every hang-prone case runs in a child with a hard timeout.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_nonregular_file_in_root import (BROKEN, CLEAN, GOOD, NOT_REGULAR, REPO, TIMEOUT, UNREADABLE, plant, sub_json,
                                                sub_run)

DIR_UNREADABLE = "corpus-dir-unreadable"


@pytest.fixture
def w(tmp_path, monkeypatch):
    """acorp (FIRST in registry order, may be damaged), ccorp (clean), badcorp (FIFO), secret-personality (private, FIFO)."""
    base = tmp_path / "w"
    roots = {n: base / n for n in ("acorp", "ccorp", "badcorp", "secret")}
    for n, r in roots.items():
        r.mkdir(parents=True)
    (roots["acorp"] / "a.lts").write_text("# A [A-1]\n\n## An [A-N]\n\n- status: active\n")
    (roots["ccorp"] / "c.lts").write_text(CLEAN)
    (roots["badcorp"] / "b.lts").write_text(GOOD)
    (roots["secret"] / "s.lts").write_text("# S [S-1]\n\n## Sn [S-N]\n\n- status: active\n")
    os.mkfifo(roots["badcorp"] / "x.lts")
    os.mkfifo(roots["secret"] / "y.lts")
    reg = base / "reg.json"
    reg.write_text(json.dumps({
        "acorp": {"roots": [{"path": str(roots["acorp"]), "area": "specs"}], "domain_suffix": "p.a"},
        "badcorp": {"roots": [{"path": str(roots["badcorp"]), "area": "specs"}], "domain_suffix": "p.b"},
        "ccorp": {"roots": [{"path": str(roots["ccorp"]), "area": "specs"}], "domain_suffix": "p.c"},
        "zzsecret-personality": {"roots": [{"path": str(roots["secret"]), "area": "specs"}], "domain_suffix": "p.s"},
    }))
    state = tmp_path / "st"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTLATTICE_", "LTS_ENGINE_"))}
    env.update({"PYTHONPATH": str(REPO), "FACTLATTICE_STATE_DIR": str(state), "LTS_ENGINE_STATE_DIR": str(state),
                "FACTLATTICE_BACKUP_DIR": str(tmp_path / "bak"), "FACTLATTICE_CODE_WATCH": "0",
                "FACTLATTICE_QUERY_LOG": str(tmp_path / "ql.jsonl"), "FACTLATTICE_CORPUS_CONFIG": str(reg),
                "FACTLATTICE_PROFILE_DIR": str(tmp_path / "noprofiles")})
    monkeypatch.chdir(tmp_path)
    return {"base": base, "roots": roots, "env": env, "tmp": tmp_path, "ql": tmp_path / "ql.jsonl", "reg": reg}


# ------------------------------------------------------------------------------------------------ F1 privacy
UNNAMED = """
import json
from factlattice_engine import tools
from factlattice_engine.server import build_tools
eng = build_tools(watch=False)
calls = {
  "lts_list_corpora": {}, "lts_find": {"text": "N"}, "lts_find_in_prose": {"text": "status"},
  "lts_aggregate": {"op": "count"}, "lts_profile_audit": {},
}
out = {n: tools.dispatch(n, a, tools=eng) for n, a in calls.items()}
out["named"] = tools.dispatch("lts_find", {"corpus": "zzsecret-personality", "text": "S"}, tools=eng)
print(json.dumps(out))
"""


def test_a_failed_private_corpus_is_never_named_on_an_unnamed_call(w):
    out, _ = sub_json(UNNAMED, w["env"])
    for name, reply in out.items():
        if name == "named":
            continue
        assert "zzsecret" not in json.dumps(reply), f"{name} NAMES the failed private corpus"
    lc = out["lts_list_corpora"]
    assert [f["corpus_id"] for f in lc["failed_corpora"]] == ["badcorp"]       # the public failure is still named
    assert lc["private_failed"] == 1                                            # the private one is counted, anonymously
    skips = lc["extent"]["corpora_skipped"]
    assert {"reason": "private-corpus-failed", "count": 1} in skips
    assert {"corpus": "badcorp", "reason": NOT_REGULAR} in skips
    # named by the caller: the caller already knows the name, so the code is answered
    assert out["named"]["reason"] == NOT_REGULAR


def test_the_query_log_does_not_name_a_failed_private_corpus_on_an_unnamed_call(w):
    sub_json(UNNAMED.replace('out["named"] = tools.dispatch("lts_find", {"corpus": "zzsecret-personality", "text": "S"}, tools=eng)',
                             'pass'), w["env"])
    text = w["ql"].read_text()
    assert text.strip() and "zzsecret" not in text


# ------------------------------------------------------------------------------------------------ F2 fail closed after boot
RUNTIME = """
import json, os, sys, stat, time
from factlattice_engine import tools
from factlattice_engine.server import build_tools
a, c = sys.argv[1], sys.argv[2]
eng = build_tools(watch=False)
k = eng.store.kernel
part_a, part_c = eng.store.get_partition("acorp"), eng.store.get_partition("ccorp")
res = {"boot_fresh": part_a.freshness().fresh}
f = os.path.join(a, "a.lts")

def tick():
    try:
        k.tick(); return None
    except Exception as e:
        return type(e).__name__

# (1) chmod 000 only (content and mtime unchanged)
os.chmod(f, 0)
res["chmod_tick_raised"] = tick()
res["chmod_fresh"] = part_a.freshness().fresh
r = tools.dispatch("lts_find", {"corpus": "acorp", "text": "An"}, tools=eng)
res["chmod_find_success"] = r.get("success")
os.chmod(f, 0o644)
res["restored_tick_raised"] = tick()
res["restored_fresh"] = part_a.freshness().fresh
# (2) chmod 000 PLUS an edit: must never serve the OLD content as fresh
open(f, "a").write("\\n## Extra [A-X]\\n\\n- status: active\\n")
os.chmod(f, 0)
res["edit_tick_raised"] = tick()
res["edit_fresh"] = part_a.freshness().fresh
r = tools.dispatch("lts_find", {"corpus": "acorp", "text": "An"}, tools=eng)
res["edit_find_success"] = r.get("success")
res["edit_find_text"] = json.dumps(r)[:6000]
# (3) the OTHER corpus is still polled: a legitimate new file in ccorp becomes visible although acorp (ahead of it) is broken
before = k.current_root(part_c)
open(os.path.join(c, "new.lts"), "w").write("# N [N-9]\\n\\n## Nn [N-N]\\n\\n- status: active\\n")
res["other_tick_raised"] = tick()
res["other_root_moved"] = k.current_root(part_c) != before
# (4) recovery: readable again -> the edit is served, fresh
os.chmod(f, 0o644)
res["recover_tick_raised"] = tick()
r = tools.dispatch("lts_find", {"corpus": "acorp", "text": "Extra"}, tools=eng)
res["recovered_success"] = r.get("success")
res["recovered_count"] = r.get("count")
print(json.dumps(res))
"""


def test_a_member_that_turns_unreadable_after_boot_never_serves_fresh(w):
    if os.geteuid() == 0:
        pytest.skip("running as root: chmod 000 does not make a file unreadable")
    # acorp is the first corpus in registry order; badcorp/secret fail at boot and are not part of this
    cp, _ = sub_run(["-c", RUNTIME, str(w["roots"]["acorp"]), str(w["roots"]["ccorp"])], w["env"])
    assert cp.returncode == 0, cp.stderr[-2500:]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert r["boot_fresh"] is True
    assert r["chmod_tick_raised"] is None and r["edit_tick_raised"] is None and r["other_tick_raised"] is None, r
    assert r["chmod_fresh"] is False and r["chmod_find_success"] is False, r
    assert r["restored_fresh"] is True, r                                       # recovery works once readable
    assert r["edit_fresh"] is False and r["edit_find_success"] is False, r      # never the OLD content as fresh
    assert UNREADABLE in r["edit_find_text"]
    assert r["other_root_moved"] is True, r                                     # one bad corpus does not stop the others
    assert r["recovered_success"] is True and r["recovered_count"] >= 1, r


# ------------------------------------------------------------------------------------------------ F3 reader inventory
READERS = {
    "lts_property_loss": """
from factlattice_engine import tools
r = tools.dispatch("lts_property_loss", {"roots": [ROOT]}, tools=None)
OUT = {"success": r.get("success"), "reason": r.get("reason"), "text": json.dumps(r)[:6000]}
""",
    "key_loss.scan_roots": """
from factlattice_engine import key_loss
r = key_loss.scan_roots([ROOT])
OUT = {"success": True, "text": json.dumps(r, default=str)[:3000], "unreadable": [u[-12:] for u in r.get("unreadable", [])]}
""",
    "contradiction.index_corpus": """
from factlattice.authoring import contradiction
try:
    contradiction.index_corpus(ROOT); OUT = {"success": True}
except Exception as e:
    OUT = {"success": False, "cls": type(e).__name__, "code": getattr(e, "code", None)}
""",
    "parser.parse_file": """
from factlattice.parser import parse_file
try:
    parse_file(ROOT + "/x.lts"); OUT = {"success": True}
except Exception as e:
    OUT = {"success": False, "cls": type(e).__name__, "code": getattr(e, "code", None)}
""",
    "lts_lint": """
from factlattice_engine import tools
r = tools.dispatch("lts_lint", {"source": ROOT}, tools=None)
OUT = {"success": r.get("success"), "reason": r.get("reason"), "text": json.dumps(r)[:600]}
""",
    "lts_preview": """
from factlattice_engine import tools
r = tools.dispatch("lts_preview", {"content": "# P [P-1]\\n", "corpus": ROOT}, tools=None)
OUT = {"success": r.get("success"), "reason": r.get("reason"), "text": json.dumps(r)[:600]}
""",
    "lts_add_property": """
from factlattice_engine import tools
r = tools.dispatch("lts_add_property", {"path": ROOT + "/x.lts", "node_id": "A-1", "key": "k", "value": "v"}, tools=None)
OUT = {"success": r.get("success"), "text": json.dumps(r)[:600]}
""",
    "domain_profiles.read_profile_edges": """
from factlattice.domain_profiles import read_profile_edges
try:
    read_profile_edges(ROOT + "/x.lts"); OUT = {"success": True}
except Exception as e:
    OUT = {"success": False, "cls": type(e).__name__, "code": getattr(e, "code", None)}
""",
    "hashing.safe_read_text": """
from factlattice.hashing import safe_read_text
try:
    safe_read_text(ROOT + "/x.lts"); OUT = {"success": True}
except Exception as e:
    OUT = {"success": False, "cls": type(e).__name__, "code": getattr(e, "code", None)}
""",
}


@pytest.mark.parametrize("reader", sorted(READERS))
def test_every_reader_of_corpus_files_survives_a_fifo(w, tmp_path, reader):
    root = tmp_path / "fiforoot"
    root.mkdir()
    (root / "ok.lts").write_text(GOOD)
    os.mkfifo(root / "x.lts")
    code = ("import json, sys\nROOT = sys.argv[1]\n" + READERS[reader] + "print(json.dumps(OUT))\n")
    cp, _ = sub_run(["-c", code, str(root)], w["env"])               # a HANG fails the test (HANG: ...)
    assert cp.returncode == 0, cp.stderr[-2000:]
    out = json.loads(cp.stdout.strip().splitlines()[-1])
    blob = json.dumps(out)
    if reader == "lts_add_property":
        assert out["success"] is False and "nothing written" in blob, out                     # refused, promptly
    elif reader in ("lts_property_loss", "key_loss.scan_roots"):
        # retro-scans REPORT a bad member as a row (never a hang, never silently skipped)
        assert ("corpus-file-not-regular: x.lts" in blob) or ("x.lts" in blob and "unreadable" in blob), out
    else:
        assert out.get("success") is not True or reader == "lts_preview", (reader, out)
        assert NOT_REGULAR in blob, (reader, out)                                          # the precise code


def test_key_loss_reports_the_fifo_as_a_row_not_a_hang(w, tmp_path):
    # (the report lists the member under `unreadable`; it is neither skipped nor read)
    root = tmp_path / "klroot"
    root.mkdir()
    (root / "ok.lts").write_text(GOOD)
    os.mkfifo(root / "x.lts")
    code = ("import json, sys\nfrom factlattice_engine import key_loss\nr = key_loss.scan_roots([sys.argv[1]])\n"
            "print(json.dumps(r, default=str))\n")
    cp, _ = sub_run(["-c", code, str(root)], w["env"])
    assert cp.returncode == 0, cp.stderr[-2000:]
    assert json.loads(cp.stdout.strip().splitlines()[-1])["unreadable"][0].endswith("x.lts")


# ------------------------------------------------------------------------------------------------ F4 fan-outs from the tool list
FANOUT = """
import json
from factlattice_engine import tools, server
eng = server.build_tools(watch=False)
opt = []
for t in server.TOOLS:
    props = (t.get("inputSchema") or {}).get("properties") or {}
    req = (t.get("inputSchema") or {}).get("required") or []
    if "corpus" in props and "corpus" not in req:
        opt.append(t["name"])
ARGS = {"lts_find": {"text": "N"}, "lts_find_in_prose": {"text": "status"}, "lts_aggregate": {"op": "count"},
        "lts_profile_audit": {}, "lts_list_corpora": {}}
res = {}
for n in opt:
    if n in ARGS:
        res[n] = tools.dispatch(n, ARGS[n], tools=eng)
print(json.dumps({"optional": opt, "res": res}))
"""

# corpus-optional tools that are NOT a corpus fan-out (they take a corpus only as a TARGET): decided here, on purpose.
NOT_A_FANOUT = {"lts_preview": "corpus is the TARGET of a draft (an id or a directory), not a fan-out"}


def test_every_corpus_omitted_fanout_tool_names_the_failed_corpora(w):
    out, _ = sub_json(FANOUT, w["env"])
    unknown = [n for n in out["optional"] if n not in out["res"] and n not in NOT_A_FANOUT]
    assert not unknown, f"a new corpus-optional tool needs a fan-out decision (and a failed-corpus report): {unknown}"
    assert {"lts_find", "lts_find_in_prose", "lts_aggregate", "lts_profile_audit"} <= set(out["res"])
    for name, reply in out["res"].items():
        assert reply.get("success") is True, (name, reply)
        blob = json.dumps(reply)
        assert "zzsecret" not in blob, f"{name} names the failed private corpus"
        skipped = (reply.get("extent") or {}).get("corpora_skipped") or reply.get("corpora_skipped") or []
        assert {"corpus": "badcorp", "reason": NOT_REGULAR} in skipped, (name, skipped)
        assert {"reason": "private-corpus-failed", "count": 1} in skipped, (name, skipped)


# ------------------------------------------------------------------------------------------------ P2
CODED = """
import json
from factlattice_engine import tools
from factlattice_engine.server import build_tools
eng = build_tools(watch=False)
out = {
  "lint": tools.dispatch("lts_lint", {"source": ROOT}, tools=eng),
  "preview_dir": tools.dispatch("lts_preview", {"content": "# P [P-1]\\n", "corpus": ROOT}, tools=eng),
  "preview_id": tools.dispatch("lts_preview", {"content": "# P [P-1]\\n", "corpus": "badcorp"}, tools=eng),
}
print(json.dumps(out))
"""


def test_lint_and_preview_answer_with_the_code(w, tmp_path):
    root = tmp_path / "pr"
    root.mkdir()
    (root / "ok.lts").write_text(GOOD)
    os.mkfifo(root / "x.lts")
    code = "ROOT = " + repr(str(root)) + "\n" + CODED
    out, _ = sub_json(code, w["env"])
    for k in ("lint", "preview_dir", "preview_id"):
        r = out[k]
        assert r["success"] is False and r["reason"] == NOT_REGULAR, (k, r)
        assert r["error_class"] == "CorpusFileError", (k, r)
    assert out["lint"]["path"] == "x.lts"
    assert out["preview_id"]["path"] == "x.lts"                 # `corpus=<failed id>` is NOT "unknown_corpus"


def test_the_lint_cli_prints_the_code_not_a_traceback(w, tmp_path):
    root = tmp_path / "lc"
    root.mkdir()
    (root / "ok.lts").write_text(GOOD)
    os.mkfifo(root / "x.lts")
    cp, _ = sub_run(["-m", "factlattice_engine.lint", str(root)], w["env"])
    assert "Traceback" not in cp.stderr and "Traceback" not in cp.stdout, cp.stderr[-800:]
    assert NOT_REGULAR in (cp.stdout + cp.stderr)
    assert cp.returncode == 2


DIRCASE = """
import json
from factlattice_engine import config
from factlattice import registry
store = config.build_store(registry.resolve_registry().corpora)
print(json.dumps({"loaded": sorted(p.corpus_id for p in store.partitions()), "failed": store.failed_corpora}))
"""


def test_an_unreadable_directory_fails_only_its_own_corpus_with_a_precise_code(w):
    if os.geteuid() == 0:
        pytest.skip("running as root: chmod 000 does not make a directory unreadable")
    d = w["roots"]["acorp"] / "locked"
    d.mkdir()
    (d / "in.lts").write_text(GOOD)
    d.chmod(0)
    try:
        out, _ = sub_json(DIRCASE, w["env"])
        e2e = """
import json
from factlattice_engine import tools
from factlattice_engine.server import build_tools
eng = build_tools(watch=False)
print(json.dumps({"lc": tools.dispatch("lts_list_corpora", {}, tools=eng),
                  "find": tools.dispatch("lts_find", {"corpus": "acorp", "text": "A"}, tools=eng)}))
"""
        out2, _ = sub_json(e2e, w["env"])
        rv = sub_run(["-m", "factlattice_engine.reviewer", "frame", "--corpus", "acorp", "--source-file", "a.lts"], w["env"])[0]
    finally:
        d.chmod(0o755)
    assert "ccorp" in out["loaded"] and "acorp" not in out["loaded"], out          # NOT the whole registry
    assert out["failed"]["acorp"]["code"] == DIR_UNREADABLE and out["failed"]["acorp"]["path"] == "locked"
    assert out2["find"]["reason"] == DIR_UNREADABLE
    body = json.loads(rv.stdout)
    assert body["reason"] == DIR_UNREADABLE and "registry-not-found" not in rv.stdout, body


def test_the_descriptions_state_the_failed_corpora_surface():
    from factlattice_engine import server
    desc = {t["name"]: t["description"] for t in server.TOOLS}
    d = desc["lts_list_corpora"]
    for needle in ("failed_corpora", "private_failed", NOT_REGULAR, BROKEN, UNREADABLE, DIR_UNREADABLE, "CorpusFileError"):
        assert needle in d, needle
    for n in ("lts_find", "lts_aggregate", "lts_profile_audit", "lts_find_in_prose"):
        assert "corpora_skipped" in desc[n] and "corpus-file-" in desc[n], n


# ------------------------------------------------------------------------------------------------ the tripwire
SCAN_DIRS = ("factlattice", "factlattice_engine")
RAW = {"rglob", "glob", "iglob", "read_text", "read_bytes", "scandir", "iterdir", "listdir", "walk"}
SHUTIL_COPY = {"copyfile", "copy2", "copyfileobj", "copytree", "copy"}      # `copy` only on the name `shutil` (copy.copy is not I/O)
WRITE_FLAGS = ("O_WRONLY", "O_RDWR", "O_APPEND", "O_TRUNC")      # O_RDONLY|O_CREAT still blocks on an existing FIFO: it is a READ

# The ONLY file-wide exemption is the safe module itself. Every other exemption is PER FUNCTION and states why that site cannot be
# handed a non-regular path. Two honest classes of reason are accepted: (a) NAMES ONLY: the call lists or globs names and opens
# nothing; (b) ENGINE-OWNED FILE: the path is a file the engine (or its installer) created in a location only the engine's own
# user can write (state directory, model directory, the engine's code tree, a shipped control file, the registry the operator
# configured), so planting a FIFO there needs the same trust as editing the engine itself. "A caller named the path" is NOT a
# reason: that is exactly the hole the reviewer readers were.
FILE_EXEMPT = {
    "factlattice/hashing.py": "THE safe module",
}
_STATE = "engine-owned state file in the engine user's own state directory; planting a FIFO there needs the trust to edit the engine"
_MODEL = "engine-owned model/index file under the engine user's own model directory; same trust boundary as editing the engine"
_NAMES = "lists NAMES only and opens nothing; any member it finds is then read through list_lts_files / safe_read_text"
FUNC_EXEMPT = {
    ("factlattice/authoring/edit.py", "_sweep_stale_temps"): "lists the writer's own temp directory for stale tmp NAMES; opens nothing",
    ("factlattice/authoring/edit.py", "_write"): "shutil.copy2 backs up the file the writer is about to replace; that same locked operation has already read it through safe_read_bytes, so a FIFO was refused before this point",
    ("factlattice/authoring/edit.py", "_atomic_replace"): "os.open of the file's parent DIRECTORY (O_RDONLY) to fsync it; a directory, not a member",
    ("factlattice/authoring/edit.py", "_copy_xattrs_darwin"): "libc copyfile(3) of extended attributes between the replaced file (already read through safe_read_bytes) and its temp copy",
    ("factlattice/corpus.py", "_build_over_files"): "shutil.copyfile refuses a FIFO itself (SpecialFileError) and fails on a socket; NOT covered: a character device",
    ("factlattice_engine/preview.py", "_stage"): "copies members list_lts_files classified moments before; shutil.copyfile refuses a FIFO itself (SpecialFileError)",
    ("factlattice_engine/build_stamp.py", "_open_carry_dir"): "os.open of the engine's own carry DIRECTORY (O_DIRECTORY|O_NOFOLLOW)",
    ("factlattice_engine/build_stamp.py", "take_inherited_stdin_carry"): "reads the engine's own carry file in its own 0700 carry directory (O_NOFOLLOW)",
    ("factlattice_engine/semantic/embedd.py", "_read_starts"): "reads the embedder's own start-log file in its run directory (O_NOFOLLOW)",
    ("factlattice_engine/semantic/embedd.py", "_read_tripped"): "reads the embedder's own keep-tripped file in its run directory (O_NOFOLLOW)",
    ("factlattice_engine/semantic/index.py", "_load"): "np.load runs only after _sha256_file (file_content_hash) proved vectors.npy is a regular file with the manifest hash",
    ("factlattice_engine/semantic/index.py", "_adopt_legacy"): "shutil.copyfile of the legacy index files inside the engine's own model directory",
    ("factlattice/domain_profiles.py", "discover_profiles"): _NAMES,
    ("factlattice/profile.py", "discover_profile"): _NAMES,
    ("factlattice/profile.py", "_read_profile_file"): "opens a .yaml/.json profile (the .lts branch goes through parse_file); the operator-configured profile file, not corpus content",
    ("factlattice/enumeration_gap.py", "control_corpus_text"): "reads the SHIPPED selfcheck control file inside the engine's own package directory",
    ("factlattice/ledger.py", "read_lines"): _STATE,
    ("factlattice/registry.py", "read_registry_file"): "reads the registry JSON the operator configured (REGISTRY-SINGLE-READER); the operator's own file, read once at boot",
    ("factlattice/registry.py", "_read_pointer"): "reads the per-user pointer file in the user's own config directory",
    ("factlattice_engine/build_stamp.py", "build_stamp"): "walks and reads the engine's OWN code tree to stamp the build",
    ("factlattice_engine/build_stamp.py", "sweep_stale_carry"): "lists the engine's own carry directory NAMES",
    ("factlattice_engine/callcli.py", "_load_args"): "reads the --args-file JSON the invoking user gave on their own command line; not corpus content, and a block there stalls only that user's own process",
    ("factlattice_engine/freshness.py", "main"): "reads the graph_index.json argument of the freshness CLI (JSON, not a corpus member); the corpus itself is read through files_hash",
    ("factlattice_engine/lint/__main__.py", "resolve_targets"): "globs the user's pattern for NAMES; members then go through list_lts_files, and an explicit file must pass os.path.isfile",
    ("factlattice_engine/lint/corpus_files.py", "find_corpus_file_issues"): "rglob('*') for FILE NAMES only; never opens a member",
    ("factlattice_engine/readprofile.py", "read_records"): _STATE,
    ("factlattice_engine/selfcheck/__init__.py", "compute_control_digest"): "reads the SHIPPED control corpus inside the engine's own package directory",
    ("factlattice_engine/selfcheck/__init__.py", "run_selfcheck"): "reads the SHIPPED expected-results file inside the engine's own package directory",
    ("factlattice_engine/semantic/embedd.py", "_mcp_install_env"): "reads the operator's own MCP config JSON while installing the embedder service (an install-time CLI, not a query path)",
    ("factlattice_engine/semantic/embedd.py", "_legacy_agents"): "lists and reads earlier-release embedder plists in the user's own LaunchAgents directory (install-time CLI, no corpus file). It opens O_NOFOLLOW|O_NONBLOCK and fstat-checks S_ISREG before reading, so a FIFO or symlink is reported, never read (tests/test_semantic_embedd_always_on.py::test_mig10 and test_mig8)",
    ("factlattice_engine/semantic/embedd.py", "launchagent"): "reads the LaunchAgent plist this installer wrote in the user's own LaunchAgents directory",
    ("factlattice_engine/semantic/embedd.py", "pidfiles"): "lists pid-file NAMES in the embedder run directory; opens nothing",
    ("factlattice_engine/semantic/embedd.py", "source_fingerprint"): "hashes the engine's OWN source files to fingerprint the embedder",
    ("factlattice_engine/semantic/embedd.py", "watch_fingerprint"): "reads the engine's own watch fingerprint file in the embedder run directory",
    ("factlattice_engine/semantic/embedder.py", "_sha256"): _MODEL,
    ("factlattice_engine/semantic/index.py", "_other_version_dirs"): "lists index version directory NAMES; opens nothing",
    ("factlattice_engine/semantic/index.py", "newest"): "stats index-directory entries for the newest mtime; opens nothing",
    ("factlattice_engine/semantic/suggest.py", "_pin_from_env"): "reads the model-pin JSON from the operator's own environment at start-up",
    ("factlattice_engine/semantic/tokenizer.py", "__init__"): _MODEL,
}


def _raw_sites(path: Path):
    """(lineno, what, enclosing function) for each raw walk/glob/read call and each open() for reading."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = {}

    def walk(n, fn):
        for c in ast.iter_child_nodes(n):
            f = c.name if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            owner[id(c)] = f
            walk(c, f)
    walk(tree, "<module>")
    # `import shutil as sh` / `import numpy as npy`: resolve the alias so `sh.copyfile` / `npy.load` are seen as shutil / numpy
    modalias = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    modalias[a.asname] = a.name.split(".")[0]
    for n in ast.walk(tree):
        # an ALIASED or imported open/copy: `o = open`, `from io import open`, `from shutil import copyfile`
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Name) and n.value.id in ("open", "copyfile"):
            yield n.lineno, "alias:" + n.value.id, owner.get(id(n), "<module>")
        if isinstance(n, ast.ImportFrom) and n.module in ("io", "os", "builtins", "shutil", "pathlib", "glob") and \
                any(a.name in ("open", "copyfile", "copy2", "copy", "copyfileobj", "copytree") or a.name in RAW for a in n.names):
            yield n.lineno, "from-import:" + n.module, owner.get(id(n), "<module>")
        if not isinstance(n, ast.Call):
            continue
        fn = n.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        base = getattr(fn.value, "id", None) if isinstance(fn, ast.Attribute) else None
        base = modalias.get(base, base)
        where = owner.get(id(n), "<module>")
        if name in RAW:
            yield n.lineno, name, where
        elif name in SHUTIL_COPY and isinstance(fn, ast.Attribute) and (name != "copy" or base == "shutil"):
            yield n.lineno, "shutil." + name, where
        elif name == "open" and isinstance(fn, ast.Attribute) and base == "os":
            flags = ast.unparse(n.args[1]) if len(n.args) > 1 else ""
            if not any(f in flags for f in WRITE_FLAGS):                      # a READ-ONLY os.open (writes and locks are out of scope)
                yield n.lineno, "os.open(read)", where
        elif name == "open" and isinstance(fn, ast.Attribute) and base not in (None, "os"):
            yield n.lineno, f"{base}.open", where                              # io.open, Path.open, gzip.open ... (a `.open(` method)
        elif name == "open" and isinstance(fn, ast.Attribute):
            yield n.lineno, "x.open", where                                    # open on a call/attribute result, e.g. Path(p).open()
        elif name == "load" and isinstance(fn, ast.Attribute) and base in ("np", "numpy"):
            yield n.lineno, "np.load", where
        elif name == "open" and isinstance(fn, ast.Name):
            mode = None
            if len(n.args) > 1 and isinstance(n.args[1], ast.Constant):
                mode = n.args[1].value
            for kw in n.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            if mode is None or "r" in str(mode):
                yield n.lineno, "open(read)", where


def _unexempt(root: Path):
    out = []
    for d in SCAN_DIRS:
        for p in sorted((root / d).rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            if any(rel == k or rel.startswith(k + "/") for k in FILE_EXEMPT):
                continue
            for ln, what, fn in _raw_sites(p):
                if (rel, fn) in FUNC_EXEMPT:
                    continue
                out.append(f"{rel}:{ln} {what} in {fn}")
    return out


def test_tripwire_no_raw_walk_or_read_of_corpus_files_outside_the_safe_module():
    """A TRIPWIRE for the LITERAL call forms listed below, not a proof that no unsafe reader exists.

    SEEN: rglob / glob / iglob / scandir / iterdir / listdir / os.walk; read_text / read_bytes; read-mode builtin `open(...)`;
    a READ-ONLY `os.open(...)` (no O_WRONLY/O_RDWR/O_CREAT/O_APPEND/O_TRUNC in its flags); any `X.open(...)` method call
    (`io.open`, `Path(p).open()`, `gzip.open` ...); `shutil.copyfile/copy/copy2/copyfileobj/copytree`; `np.load`; an ALIAS
    assignment `o = open`; a `from io|os|builtins|shutil|pathlib import open/copy...`. Each form has a planted case in
    tests/test_nonregular_round3.py that must fire.

    EXEMPTED FUNCTION: an exemption is per FUNCTION NAME, so a NEW raw read added inside any exempted function (and inside a second
    def with an exempt name) stays green. That is the price of per-function granularity; `corpus._build_over_files` (which admits
    a character device) and every function in FUNC_EXEMPT are covered only by review, not by this scan.

    ALSO SEEN: `import shutil as sh` / `import numpy as npy` module aliases (resolved), `from os import listdir as ld`, and an
    `os.open` whose flags are unknown (read is assumed) or O_RDONLY|O_CREAT (that still blocks on a FIFO).

    NOT SEEN (uncovered, by design of an AST scan over literal calls): `eval('open')(p)` / `exec`, `importlib` and `__import__`
    forms other than a dotted call, a module alias bound any way but `import x as y` (for example `sh = shutil` or an alias made
    in another module and imported), an `os.open` that spells a read with write flags, a read through a variable-held or returned function
    (`f = getattr(io, 'open')`, `reader(path)`), `importlib`/`__import__` tricks, `ctypes`/C extensions (numpy's own readers beyond
    `np.load`, `mmap`, `plistlib.load(open(...))` is seen only through its `open`), subprocess (`cat file`), write-mode opens
    and lock/temp files (out of scope: this guards READS of corpus files), a read through a file object someone else opened
    (`json.load(fh)`), and any library that opens a path internally. A guard that claims more than this is the false invariant
    this fix removes. A new reader of corpus files must go through `factlattice.hashing.safe_read_text/safe_read_bytes/
    walk_lts_paths/list_lts_files`, or earn an exemption above with a reason."""
    assert _unexempt(REPO) == []


def test_tripwire_negative_control_it_fires_on_a_planted_raw_reader(tmp_path):
    (tmp_path / "factlattice").mkdir()
    (tmp_path / "factlattice_engine").mkdir()
    (tmp_path / "factlattice" / "planted.py").write_text(
        "from pathlib import Path\n\ndef scan(r):\n    for f in Path(r).rglob('*.lts'):\n        f.read_text()\n"
        "    return open('x.lts')\n")
    found = _unexempt(tmp_path)
    assert any("rglob" in s for s in found) and any("read_text" in s for s in found) and any("open(read)" in s for s in found), found


def test_the_sdlc_row_is_a_rule_for_future_readers_and_fanouts():
    sdlc = (REPO / "SDLC.md").read_text(encoding="utf-8")
    row = next(l for l in sdlc.splitlines() if l.startswith("- [ ] [CORPUS-MEMBER-IS-A-READABLE-REGULAR-FILE]"))
    for needle in ("safe_read_text", "walk_lts_paths", "failed_corpora", "private-corpus-failed", "TRIPWIRE", "every fan-out"):
        assert needle in row, needle
    assert "NOTHING skips a member-shaped file silently" not in row            # the overclaim the Diamond found
