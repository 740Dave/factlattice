"""Record the BASE responses of tests/qs_fixture.BASE_CALLS (run against the PRE-change tree).

    PYTHONPATH=<tree> FACTLATTICE_STATE_DIR=<tmp> python3 tests/fixtures/record_qs_base.py <out.json>

The fixture is recorded under the same registry isolation the test suite applies (tests/conftest.py
_isolate_registry_lookup): FACTLATTICE_CORPUS_CONFIG unset and HOME an empty directory, so no live
registry or user-config pointer can change a reply (lts_profile_audit's reply depends on it). This
script applies that isolation itself before importing the engine. Re-record only for a deliberate
reply-shape change, and check the diff against the old fixture before committing (spec 39
x-minted-keys was the last such change).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.pop("FACTLATTICE_CORPUS_CONFIG", None)
os.environ["HOME"] = tempfile.mkdtemp(prefix="qs-record-home-")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.qs_fixture import run_base_calls  # noqa: E402

if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp())
    (tmp / "w").mkdir()
    out = run_base_calls(tmp / "w", tmp / "state")
    Path(sys.argv[1]).write_text(json.dumps(out, indent=1, sort_keys=False, ensure_ascii=False), encoding="utf-8")
    print(f"recorded {len(out)} calls -> {sys.argv[1]}")
