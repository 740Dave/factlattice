"""Round 3 of ISSUE-NONREGULAR-MEMBER (delta Diamond at 88b9ef3: F-1, F-2, P3 stale reply).

F-1  no reader may hang on a FIFO: the reviewer's `scan_secrets`, `gen6_scan`, `count_registrations` (and the CLI verbs that call
     them) answer the existing coded refusal. Each case runs in a child with a hard timeout, so a hang FAILS the test.
F-2  the tripwire has NO file-wide and NO `<any>` exemption for a file that reads corpus-adjacent paths, and it FIRES for a raw
     open() planted in a scratch copy of each of tools.py, store.py, semantic/index.py and reviewer/__init__.py.
P3   a STALE reply for a faulted corpus carries the code in its top-level `reason` (not null).
Special files are planted only under pytest's tmp_path.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from tests.test_nonregular_file_in_root import GOOD, KINDS, NOT_REGULAR, REPO, UNREADABLE, plant, sub_run
from tests.test_nonregular_round2 import _unexempt, w  # noqa: F401  (w: the registry fixture)

FUNCS = {
    "scan_secrets": "from factlattice_engine.reviewer import scan_secrets as f",
    "gen6_scan": "from factlattice_engine.reviewer import gen6_scan as f",
    "count_registrations": "from factlattice_engine.reviewer import count_registrations as f",
}


@pytest.mark.parametrize("fn", sorted(FUNCS))
def test_reviewer_readers_refuse_a_fifo_with_the_code_and_never_block(w, tmp_path, fn):
    root = tmp_path / "rv"
    root.mkdir()
    os.mkfifo(root / "doc.lts")
    code = (FUNCS[fn] + "\nimport json, sys\ntry:\n    f(sys.argv[1]); out = {'ok': True}\n"
            "except Exception as e:\n    out = {'ok': False, 'cls': type(e).__name__, 'code': getattr(e, 'code', None)}\n"
            "print(json.dumps(out))\n")
    cp, _ = sub_run(["-c", code, str(root / "doc.lts")], w["env"])          # a hang fails here
    assert cp.returncode == 0, cp.stderr[-1500:]
    out = json.loads(cp.stdout.strip().splitlines()[-1])
    assert out == {"ok": False, "cls": "CorpusFileError", "code": NOT_REGULAR}, out


@pytest.mark.parametrize("verb,flag", [("scan-secrets", "--document"), ("gen6-scan", "--path")])
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_reviewer_cli_verbs_answer_the_coded_refusal(w, tmp_path, verb, flag, kind):
    root = tmp_path / "rc"
    plant(kind, root)
    cp, _ = sub_run(["-m", "factlattice_engine.reviewer", verb, flag, str(root / "bad.lts")], w["env"])
    assert cp.returncode == 2, (cp.stdout, cp.stderr[-800:])
    body = json.loads(cp.stdout)
    assert body["reason"] == KINDS[kind] and body["error_class"] == "CorpusFileError", body
    assert body["explanation"] and str(tmp_path) not in cp.stdout, cp.stdout      # fixed sentence, no absolute path


def test_reviewer_check_registrations_cli_maps_a_fifo_profile(w, tmp_path):
    """count_registrations is reached through check_registrations(profile_path, audit): drive that call path."""
    root = tmp_path / "cr"
    root.mkdir()
    os.mkfifo(root / "prof.lts")
    code = ("import json, sys\nfrom factlattice_engine.reviewer import check_registrations\n"
            "try:\n    check_registrations(sys.argv[1], {'success': True}); out = {'ok': True}\n"
            "except Exception as e:\n    out = {'ok': False, 'code': getattr(e, 'code', None)}\nprint(json.dumps(out))\n")
    cp, _ = sub_run(["-c", code, str(root / "prof.lts")], w["env"])
    assert json.loads(cp.stdout.strip().splitlines()[-1]) == {"ok": False, "code": NOT_REGULAR}, cp.stdout


# ------------------------------------------------------------------------------------------------ P3 stale reply
STALE = """
import json, os, sys
from factlattice_engine import tools
from factlattice_engine.server import build_tools
eng = build_tools(watch=False)
f = os.path.join(sys.argv[1], "a.lts")
open(f, "a").write("\\n## Extra [A-X]\\n\\n- status: active\\n")
os.chmod(f, 0)
try:
    k = eng.store.kernel
    k.tick()
    r = tools.dispatch("lts_find", {"corpus": "acorp", "text": "An"}, tools=eng)
finally:
    os.chmod(f, 0o644)
print(json.dumps({"success": r.get("success"), "stale": r.get("stale"), "reason": r.get("reason")}))
"""


def test_a_stale_reply_for_a_faulted_corpus_carries_the_code_as_its_reason(w):
    if os.geteuid() == 0:
        pytest.skip("running as root")
    cp, _ = sub_run(["-c", STALE, str(w["roots"]["acorp"])], w["env"])
    assert cp.returncode == 0, cp.stderr[-1500:]
    out = json.loads(cp.stdout.strip().splitlines()[-1])
    assert out == {"success": False, "stale": True, "reason": UNREADABLE}, out


# ------------------------------------------------------------------------------------------------ F-2 tripwire
PLANTED = "\n\ndef _planted_raw_reader(p):\n    with open(p) as fh:\n        return fh.read()\n"
SCRATCH_TARGETS = ["factlattice_engine/tools.py", "factlattice_engine/store.py", "factlattice_engine/semantic/index.py",
                   "factlattice_engine/reviewer/__init__.py"]


@pytest.mark.parametrize("rel", SCRATCH_TARGETS)
def test_the_tripwire_fires_for_a_raw_open_planted_in_a_scratch_copy(tmp_path, rel):
    for d in ("factlattice", "factlattice_engine"):
        (tmp_path / d).mkdir(exist_ok=True)
    dst = tmp_path / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO / rel, dst)
    baseline = [s for s in _unexempt(tmp_path) if s.startswith(rel)]
    dst.write_text(dst.read_text(encoding="utf-8") + PLANTED, encoding="utf-8")
    after = [s for s in _unexempt(tmp_path) if s.startswith(rel)]
    planted = [s for s in after if "_planted_raw_reader" in s]
    assert planted, f"the tripwire is BLIND to a raw open() planted in {rel}: {after}"
    assert len(after) == len(baseline) + 1


def test_the_exemption_tables_have_no_file_wide_or_any_entries():
    from tests import test_nonregular_round2 as r2
    assert set(r2.FILE_EXEMPT) == {"factlattice/hashing.py"}, sorted(r2.FILE_EXEMPT)       # only the safe module itself
    assert not [k for k in r2.FUNC_EXEMPT if k[1] == "<any>"], [k for k in r2.FUNC_EXEMPT if k[1] == "<any>"]
    for k, why in r2.FUNC_EXEMPT.items():
        low = why.lower()
        assert len(why) > 25 and "explicit path" not in low and "named by" not in low, (k, why)


def test_every_function_exemption_still_names_a_raw_site():
    """An exemption for a site that no longer exists is a stale excuse (and a hole for the next reader in that function)."""
    from tests import test_nonregular_round2 as r2
    live = set()
    for d in r2.SCAN_DIRS:
        for p in (REPO / d).rglob("*.py"):
            rel = p.relative_to(REPO).as_posix()
            for _ln, _what, fn in r2._raw_sites(p):
                live.add((rel, fn))
    stale = [k for k in r2.FUNC_EXEMPT if k not in live]
    assert not stale, stale


# ------------------------------------------------------------------------------------------------ honest scope: every form it claims to see fires
PLANTED_FORMS = {
    "os.open(read)": "import os\n\ndef f(p):\n    return os.open(p, os.O_RDONLY | os.O_NONBLOCK)\n",
    "io.open": "import io\n\ndef f(p):\n    return io.open(p)\n",
    "Path.open": "from pathlib import Path\n\ndef f(p):\n    return Path(p).open()\n",
    "shutil.copyfile": "import shutil\n\ndef f(p, q):\n    shutil.copyfile(p, q)\n",
    "shutil.copy2": "import shutil\n\ndef f(p, q):\n    shutil.copy2(p, q)\n",
    "np.load": "import numpy as np\n\ndef f(p):\n    return np.load(p)\n",
    "alias:open": "def f(p):\n    o = open\n    return o(p)\n",
    "from-import": "from io import open as o\n\ndef f(p):\n    return o(p)\n",
    "builtin open": "def f(p):\n    return open(p)\n",
    "read_bytes": "from pathlib import Path\n\ndef f(p):\n    return Path(p).read_bytes()\n",
}
NOT_FLAGGED = {
    "os.open write/lock": "import os\n\ndef f(p):\n    return os.open(p, os.O_WRONLY | os.O_CREAT, 0o600)\n",
    "copy.copy": "import copy\n\ndef f(x):\n    return copy.copy(x)\n",
    "open for write": "def f(p):\n    return open(p, 'w')\n",
}


def _scan_text(tmp_path, text):
    (tmp_path / "factlattice").mkdir(exist_ok=True)
    (tmp_path / "factlattice_engine").mkdir(exist_ok=True)
    (tmp_path / "factlattice" / "planted.py").write_text(text, encoding="utf-8")
    return [s for s in _unexempt(tmp_path) if s.startswith("factlattice/planted.py")]


@pytest.mark.parametrize("form", sorted(PLANTED_FORMS))
def test_every_form_the_tripwire_claims_to_see_fires_on_a_planted_case(tmp_path, form):
    assert _scan_text(tmp_path, PLANTED_FORMS[form]), f"the tripwire is blind to: {form}"


@pytest.mark.parametrize("form", sorted(NOT_FLAGGED))
def test_the_tripwire_does_not_flag_what_it_says_it_ignores(tmp_path, form):
    assert _scan_text(tmp_path, NOT_FLAGGED[form]) == []


def test_the_tripwire_docstring_states_what_it_does_not_see():
    from tests import test_nonregular_round2 as r2
    doc = r2.test_tripwire_no_raw_walk_or_read_of_corpus_files_outside_the_safe_module.__doc__
    for needle in ("NOT SEEN", "variable-held", "ctypes", "C extensions", "mmap", "subprocess", "json.load(fh)", "write-mode"):
        assert needle in doc, needle
