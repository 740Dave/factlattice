"""Record the BASE responses of tests/qs_fixture.BASE_CALLS (run against the PRE-change tree).

    PYTHONPATH=<tree> FACTLATTICE_STATE_DIR=<tmp> python3 tests/fixtures/record_qs_base.py <out.json>

The committed tests/fixtures/qs_base_responses.json was recorded this way against main 64ee63f
(a detached worktree), so test_query_surface_parity.py compares the branch to the REAL base.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.qs_fixture import run_base_calls  # noqa: E402

if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp())
    (tmp / "w").mkdir()
    out = run_base_calls(tmp / "w", tmp / "state")
    Path(sys.argv[1]).write_text(json.dumps(out, indent=1, sort_keys=False, ensure_ascii=False), encoding="utf-8")
    print(f"recorded {len(out)} calls -> {sys.argv[1]}")
