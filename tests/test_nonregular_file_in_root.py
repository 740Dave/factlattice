"""A non-regular, broken or unreadable `.lts` inside a corpus root: fail LOUD with a precise code, never hang.

Ticket ISSUE-NONREGULAR-MEMBER (measured 2026-10-08):
  (1) a FIFO named `*.lts` made the corpus build block FOREVER at `hashing.file_content_hash`;
  (2) a broken symlink / unreadable file failed the whole registry and callers said `registry-not-found`.

Binding direction (the ticket's `correction-fix-direction`, which keeps [INV-LTS-FRESH] GAP-1: never serve a silent
partial view): a member whose name ends `.lts` is classified by lstat/stat BEFORE any open --
  `corpus-file-not-regular`   FIFO, socket, device (also through a symlink)
  `corpus-file-broken-link`   a symlink that does not resolve (dangling or a loop)
  `corpus-file-unreadable`    a regular file that cannot be read
Each carries the ROOT-RELATIVE path. ONE bad file fails only its own corpus; the rest keep serving.

EVERY case that could hang on the pre-fix code runs in a SUBPROCESS with a hard timeout, so the RED itself cannot hang
the suite. Special files are planted ONLY under pytest's tmp_path (a scratch root), never in a registered real root.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TIMEOUT = 20
NOT_REGULAR, BROKEN, UNREADABLE = "corpus-file-not-regular", "corpus-file-broken-link", "corpus-file-unreadable"
GOOD = "# D [D-1]\n\n## N [N-1]\n\n- status: active\n"
CLEAN = "# C [C-1]\n\n## M [M-1]\n\n- status: active\n"

KINDS = {                      # kind -> expected code
    "fifo": NOT_REGULAR,
    "socket": NOT_REGULAR,
    "symlink-to-fifo": NOT_REGULAR,
    "broken-symlink": BROKEN,
    "symlink-loop": BROKEN,
    "unreadable": UNREADABLE,
}


def plant(kind: str, dirpath: Path, name: str = "bad.lts") -> str:
    """Plant ONE special member `dirpath/name` (scratch only). Returns the name."""
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / name
    if kind == "fifo":
        os.mkfifo(p)
    elif kind == "socket":
        cwd = os.getcwd()
        os.chdir(dirpath)                       # AF_UNIX paths are short on macOS: bind by a relative name
        try:
            s = socket.socket(socket.AF_UNIX)
            s.bind(name)
            plant.socks.append(s)               # keep it open for the test's life
        finally:
            os.chdir(cwd)
    elif kind == "symlink-to-fifo":
        os.mkfifo(dirpath / "target.fifo")
        os.symlink("target.fifo", p)
    elif kind == "broken-symlink":
        os.symlink("no-such-target.lts", p)
    elif kind == "symlink-loop":
        os.symlink(name, p)
    elif kind == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("running as root: chmod 000 does not make a file unreadable")
        p.write_text(GOOD)
        p.chmod(0)
    else:
        raise AssertionError(kind)
    return name


plant.socks = []


@pytest.fixture(autouse=True)
def _close_socks():
    yield
    for s in plant.socks:
        s.close()
    plant.socks.clear()


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """Scratch workspace: registry with a BAD corpus (a regular file + the planted member under sub/) and a CLEAN one."""
    base = tmp_path / "w"
    bad, clean = base / "badroot", base / "cleanroot"
    (bad / "sub").mkdir(parents=True)
    clean.mkdir(parents=True)
    (bad / "regular.lts").write_text(GOOD)            # "a regular file next to each": it must not rescue the corpus
    (clean / "c.lts").write_text(CLEAN)
    reg = base / "reg.json"
    reg.write_text(json.dumps({
        "badcorp": {"roots": [{"path": str(bad), "area": "specs"}], "domain_suffix": "p.bad"},
        "cleancorp": {"roots": [{"path": str(clean), "area": "specs"}], "domain_suffix": "p.clean"},
    }))
    state = tmp_path / "st"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTLATTICE_", "LTS_ENGINE_"))}
    env.update({"PYTHONPATH": str(REPO), "FACTLATTICE_STATE_DIR": str(state), "LTS_ENGINE_STATE_DIR": str(state),
                "FACTLATTICE_BACKUP_DIR": str(tmp_path / "bak"), "FACTLATTICE_CODE_WATCH": "0",
                "FACTLATTICE_QUERY_LOG": str(tmp_path / "ql.jsonl"), "FACTLATTICE_CORPUS_CONFIG": str(reg),
                "FACTLATTICE_PROFILE_DIR": str(tmp_path / "noprofiles")})
    monkeypatch.chdir(tmp_path)
    return {"base": base, "bad": bad, "clean": clean, "reg": reg, "env": env, "tmp": tmp_path, "ql": tmp_path / "ql.jsonl"}


def sub_run(argv, env, timeout=TIMEOUT):
    """Run python in a child with a hard wall-clock bound. A hang is reported as a TIMEOUT, never waited on."""
    t0 = time.monotonic()
    try:
        cp = subprocess.run([sys.executable, *argv], env=env, cwd=str(REPO), capture_output=True, text=True,
                            timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.fail(f"HANG: the child did not return within {timeout}s (fail-loud-not-hang breach)", pytrace=False)
    return cp, time.monotonic() - t0


def sub_json(code: str, env, timeout=TIMEOUT):
    cp, dt = sub_run(["-c", code], env, timeout)
    assert cp.returncode == 0, f"child failed rc={cp.returncode}\nSTDERR:\n{cp.stderr[-2000:]}"
    return json.loads(cp.stdout.strip().splitlines()[-1]), dt


BUILD = """
import json
from factlattice_engine import config
from factlattice import registry
choice = registry.resolve_registry()
store = config.build_store(choice.corpora)
print(json.dumps({"loaded": sorted(p.corpus_id for p in store.partitions()),
                  "failed": getattr(store, "failed_corpora", None)}))
"""


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_build_fails_only_the_bad_corpus_with_the_precise_code(ws, kind):
    plant(kind, ws["bad"] / "sub")
    out, dt = sub_json(BUILD, ws["env"])
    assert out["loaded"] == ["cleancorp"], out                # the other corpus keeps serving; the bad one is NOT served
    failed = out["failed"]
    assert failed is not None and list(failed) == ["badcorp"], failed
    assert failed["badcorp"]["code"] == KINDS[kind]
    assert failed["badcorp"]["path"] == "sub/bad.lts"          # ROOT-RELATIVE, never absolute
    assert dt < 10


E2E = """
import json, os
from factlattice_engine import tools
from factlattice_engine.server import build_tools
eng = build_tools(watch=False)
lc = tools.dispatch("lts_list_corpora", {}, tools=eng)
bad = tools.dispatch("lts_find", {"corpus": "badcorp", "text": "N"}, tools=eng)
good = tools.dispatch("lts_find", {"corpus": "cleancorp", "text": "M"}, tools=eng)
print(json.dumps({"lc": lc, "bad": bad, "good_count": good.get("count"), "good_ok": good.get("success")}))
"""


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_surfaces_report_the_code_not_registry_not_found(ws, kind):
    plant(kind, ws["bad"] / "sub")
    out, _ = sub_json(E2E, ws["env"])
    code = KINDS[kind]
    lc = out["lc"]
    assert lc["success"] is True
    assert [c["corpus_id"] for c in lc["corpora"]] == ["cleancorp"]
    ff = lc["failed_corpora"]
    assert ff == [{"corpus_id": "badcorp", "code": code, "path": "sub/bad.lts"}], ff
    assert {"corpus": "badcorp", "reason": code} in lc["extent"]["corpora_skipped"]
    assert "registry_error" not in json.dumps(lc) and "registry-not-found" not in json.dumps(lc)
    bad = out["bad"]
    assert bad["success"] is False and bad["reason"] == code, bad
    assert bad["error_class"] == "CorpusFileError"
    assert bad["path"] == "sub/bad.lts"
    assert out["good_ok"] is True and out["good_count"] >= 1


def test_every_corpus_bad_still_boots_and_names_the_code(ws):
    """Not a registry problem: with ALL corpora failed the server still boots (`require_corpora=True`) and says why."""
    plant("fifo", ws["bad"] / "sub")
    plant("broken-symlink", ws["clean"])
    out, _ = sub_json(E2E, ws["env"])
    ff = {f["corpus_id"]: f["code"] for f in out["lc"]["failed_corpora"]}
    assert ff == {"badcorp": NOT_REGULAR, "cleancorp": BROKEN}
    assert out["lc"]["count"] == 0
    assert out["bad"]["reason"] == NOT_REGULAR


def test_fifo_build_returns_promptly(ws):
    """The headline: it used to block forever; now < 2 s inside the child (plus interpreter start)."""
    plant("fifo", ws["bad"] / "sub")
    out, dt = sub_json(BUILD, ws["env"], timeout=TIMEOUT)
    assert out["failed"]["badcorp"]["code"] == NOT_REGULAR
    assert dt < 10


DIRECT = """
import json, sys, time
from factlattice import file_content_hash
t = time.monotonic()
try:
    file_content_hash(sys.argv[1])
    r = {"raised": None}
except Exception as e:
    r = {"raised": getattr(e, "code", None), "cls": type(e).__name__, "path": getattr(e, "rel_path", None),
         "is_oserror": isinstance(e, OSError), "text": str(e)}
r["secs"] = time.monotonic() - t
print(json.dumps(r))
"""


@pytest.mark.parametrize("kind", ["fifo", "socket", "symlink-to-fifo", "broken-symlink", "symlink-loop", "unreadable"])
def test_file_content_hash_refuses_directly(ws, kind):
    """Defence in depth: a caller that bypasses `list_lts_files` (incremental.py calls it directly) cannot hang either."""
    plant(kind, ws["bad"] / "sub")
    target = str(ws["bad"] / "sub" / "bad.lts")
    cp, _ = sub_run(["-c", DIRECT, target], ws["env"])
    assert cp.returncode == 0, cp.stderr[-1500:]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert r["raised"] == KINDS[kind], r
    assert r["secs"] < 2
    assert r["cls"] == "CorpusFileError" and r["is_oserror"] is True                         # stays an OSError so every "OSError propagates (loud)" site holds
    assert str(ws["tmp"]) not in r["text"]                 # the message is path-free by construction (rel path only)


def test_file_content_hash_on_a_regular_file_is_unchanged(ws):
    out, _ = sub_json("import json,hashlib,sys\nfrom factlattice import file_content_hash\n"
                      f"p={str(ws['bad'] / 'regular.lts')!r}\n"
                      "print(json.dumps({'ok': file_content_hash(p) == 'sha256:' + hashlib.sha256(open(p,'rb').read()).hexdigest()}))",
                      ws["env"])
    assert out["ok"] is True


FRESH = """
import json, sys
from factlattice import files_hash
root, idx = sys.argv[1], sys.argv[2]
h, _ = files_hash(root)
json.dump({"source_corpus": {"files_hash": h}}, open(idx, "w"))
"""


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_freshness_check_says_error_never_fresh(ws, kind):
    idx = ws["tmp"] / "idx.json"
    cp, _ = sub_run(["-c", FRESH, str(ws["bad"]), str(idx)], ws["env"])           # index built while the root is clean
    assert cp.returncode == 0, cp.stderr[-1500:]
    plant(kind, ws["bad"] / "sub")
    cp, _ = sub_run(["-m", "factlattice_engine.freshness", "--corpus", str(ws["bad"]), "--index", str(idx)], ws["env"])
    rec = json.loads(cp.stdout.strip().splitlines()[-1])
    assert rec["verdict"] == "error", rec
    assert cp.returncode == 2
    assert rec["reason"] == KINDS[kind] and rec["path"] == "sub/bad.lts"
    assert str(ws["tmp"]) not in cp.stdout                 # no absolute path in the verdict line


KERNEL = """
import json, os, sys
from factlattice_engine import tools
from factlattice_engine.server import build_tools
eng = build_tools(watch=False)
part = eng.store.get_partition("cleancorp")
assert part.freshness().fresh
os.mkfifo(os.path.join(sys.argv[1], "late.lts"))
eng.store.kernel.tick()                                  # one watcher cycle over every seated corpus
fr = part.freshness()
lc = tools.dispatch("lts_list_corpora", {}, tools=eng)
print(json.dumps({"fresh": fr.fresh, "listed_fresh": [c["fresh"] for c in lc["corpora"] if c["corpus_id"] == "cleancorp"]}))
"""


def test_a_fifo_planted_after_boot_is_stale_never_fresh_and_never_hangs(ws):
    cp, _ = sub_run(["-c", KERNEL, str(ws["clean"])], ws["env"])
    assert cp.returncode == 0, cp.stderr[-1500:]
    out = json.loads(cp.stdout.strip().splitlines()[-1])
    assert out["fresh"] is False and out["listed_fresh"] == [False]


@pytest.mark.parametrize("kind", ["fifo", "broken-symlink"])
def test_query_log_carries_the_code_and_no_absolute_path(ws, kind):
    plant(kind, ws["bad"] / "sub")
    sub_json(E2E, ws["env"])
    text = ws["ql"].read_text()
    assert text.strip(), "the query log is empty: the check below would be vacuous"
    assert KINDS[kind] in text                              # the code is persisted
    assert str(ws["tmp"]) not in text and "badroot" not in text   # no absolute (or root) path


@pytest.mark.parametrize("kind", ["fifo", "broken-symlink", "unreadable"])
def test_reviewer_helper_reports_the_precise_code(ws, kind):
    """`frame` is the reviewer verb in this base that reads a corpus. The reply is the helper failure contract:
    a fixed `reason` code, `error_class`, a fixed path-free `explanation`; the registry is fine, so not registry-not-found."""
    plant(kind, ws["bad"] / "sub")
    cp, _ = sub_run(["-m", "factlattice_engine.reviewer", "frame", "--corpus", "badcorp", "--source-file", "regular.lts"],
                    ws["env"])
    assert cp.returncode == 2, (cp.stdout, cp.stderr[-1000:])
    body = json.loads(cp.stdout)
    assert body["reason"] == KINDS[kind], body
    assert body["error_class"] == "CorpusFileError"
    assert body["explanation"] and str(ws["tmp"]) not in body["explanation"]
    assert "registry-not-found" not in cp.stdout
    assert str(ws["tmp"]) not in cp.stdout                  # not even in raw_error: the message is rel-path only


def test_list_lts_files_keeps_regular_files_and_symlinks_to_regular_files(ws):
    """The new classification must not reject members that were fine: a symlink to a regular file stays a member."""
    os.symlink(str(ws["clean"] / "c.lts"), str(ws["bad"] / "link.lts"))
    out, _ = sub_json("import json\nfrom factlattice import list_lts_files\n"
                      f"print(json.dumps(list_lts_files({str(ws['bad'])!r})))", ws["env"])
    assert out == ["link.lts", "regular.lts"]


def test_non_lts_special_files_are_not_members_and_are_left_alone(ws):
    """Only `*.lts` names are members: a FIFO named `x.fifo` is not shaped like a member, so it is not an error."""
    os.mkfifo(ws["bad"] / "x.fifo")
    out, _ = sub_json(BUILD, ws["env"])
    assert out["loaded"] == ["badcorp", "cleancorp"] and not out["failed"]


REGISTER = """
import json, sys
from factlattice_engine import tools
from factlattice_engine.server import build_tools
eng = build_tools(watch=False)
r = tools.dispatch("lts_register_corpus", {"corpus_id": "adhoc", "spec": {"root": sys.argv[1], "domain_suffix": "p.adhoc"}}, tools=eng)
print(json.dumps({"r": r, "loaded": sorted(eng.store.corpus_ids())}))
"""


@pytest.mark.parametrize("kind", ["fifo", "broken-symlink"])
def test_runtime_register_refuses_with_the_code_and_registers_nothing(ws, kind):
    adhoc = ws["tmp"] / "adhoc"
    adhoc.mkdir()
    (adhoc / "ok.lts").write_text(GOOD)
    plant(kind, adhoc / "d")
    cp, _ = sub_run(["-c", REGISTER, str(adhoc)], ws["env"])
    assert cp.returncode == 0, cp.stderr[-1500:]
    out = json.loads(cp.stdout.strip().splitlines()[-1])
    assert out["r"]["success"] is False and out["r"]["reason"] == KINDS[kind] and out["r"]["path"] == "d/bad.lts"
    assert "adhoc" not in out["loaded"]
