"""End-to-end self-test for the DETERMINISTIC `.lts` linter.

Plants a tiny fixture corpus with EXACTLY:
  (i)  a known FORWARD dangle  -- [FIX-A] references [FOO-BAR], never defined.
  (ii) a known ORPHAN          -- [FIX-B] is defined and referenced by nothing
                                   authored/property (only structural CONTAINS
                                   would ever point at it, which orphan detection
                                   deliberately excludes -- see dangle.py).
[FIX-A] itself is referenced BY [FIX-B] (a RELATED_TO edge) specifically so it does
NOT also show up as an orphan -- keeping the fixture's expected finding SET exactly
{forward: FOO-BAR, orphan: FIX-B}, no incidental extras to explain away.

Also proves a clean control corpus (no dangle, no orphan -- a two-node mutual
reference) produces ZERO findings, so the checks have no false positives.

Run::  python3 -m factlattice_engine.lint --selftest
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .core import lint
from .dangle import DIR_FORWARD, DIR_ORPHAN

FIXTURE_DANGLE = """\
## [FIX-A] Node A

- status: active

## [FIX-B] Node B, never referenced by anything

- status: active

## Relationships

### Semantic

- [FIX-A] DEPENDS_ON [FOO-BAR]
- [FIX-B] RELATED_TO [FIX-A]
"""

FIXTURE_CLEAN = """\
## [CLEAN-A] Node A

- status: active

## [CLEAN-B] Node B

- status: active

## Relationships

### Semantic

- [CLEAN-A] RELATED_TO [CLEAN-B]
- [CLEAN-B] RELATED_TO [CLEAN-A]
"""

FIXTURE_PARSE_ERROR_SIBLING = """\
## [OK-NODE] A fine node

- status: active
"""


class _Checker:
    """Tiny PASS/FAIL harness (same shape as decision_extractor/session_sweep)."""

    def __init__(self) -> None:
        self.failures = 0

    def check(self, claim: str, ok: bool, detail: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            self.failures += 1
        line = f"  [{mark}] {claim}"
        if detail:
            line += f"  ({detail})"
        print(line)


def run() -> int:
    c = _Checker()
    print("factlattice_engine.lint selftest")

    with tempfile.TemporaryDirectory(prefix="lts-lint-selftest-") as tmp:
        tmp_path = Path(tmp)

        # -- (1) the planted dangle fixture --------------------------------
        dangle_file = tmp_path / "fixture-dangle.lts"
        dangle_file.write_text(FIXTURE_DANGLE, encoding="utf-8")

        result = lint(str(dangle_file), dangle=True)
        findings = result.by_origin("dangle")

        forward = [f for f in findings if f.kind == DIR_FORWARD]
        orphan = [f for f in findings if f.kind == DIR_ORPHAN]

        c.check("exactly one forward dangle found", len(forward) == 1,
                f"got {len(forward)}: {[f.node_id for f in forward]}")
        if forward:
            c.check("forward dangle targets FOO-BAR", forward[0].node_id == "FOO-BAR",
                    forward[0].node_id)
            c.check("forward dangle is located (file:line)",
                    forward[0].source_file is not None and forward[0].source_line is not None,
                    forward[0].location())
            c.check("forward dangle severity is error", forward[0].severity == "error")

        c.check("exactly one orphan found", len(orphan) == 1,
                f"got {len(orphan)}: {[f.node_id for f in orphan]}")
        if orphan:
            c.check("orphan is FIX-B (not FIX-A, which IS referenced)",
                    orphan[0].node_id == "FIX-B", orphan[0].node_id)
            c.check("orphan is located (file:line)",
                    orphan[0].source_file is not None and orphan[0].source_line is not None,
                    orphan[0].location())
            c.check("orphan severity is warning", orphan[0].severity == "warning")

        c.check("FIX-A does NOT appear as an orphan (it IS referenced by FIX-B)",
                all(f.node_id != "FIX-A" for f in orphan))

        c.check("dangle-lint result reports errors present (exit-worthy)",
                result.has_errors and result.exit_code() == 1)

        # -- (2) the clean control: zero findings, no false positives -------
        clean_file = tmp_path / "fixture-clean.lts"
        clean_file.write_text(FIXTURE_CLEAN, encoding="utf-8")

        clean_result = lint(str(clean_file), dangle=True)
        c.check("clean control: zero findings", len(clean_result.findings) == 0,
                f"got {len(clean_result.findings)}: "
                f"{[f.render() for f in clean_result.findings]}")
        c.check("clean control: exit_code 0", clean_result.exit_code() == 0)

        # -- (3) structure-only mode still surfaces the dangling diagnostic
        #        (capability 1, independent of the dedicated dangle-lint) -----
        structure_only = lint(str(dangle_file), dangle=False, structure=True)
        struct_dangling = [f for f in structure_only.findings if f.kind == "dangling"]
        c.check("structure-only mode (no --dangle) still flags the FOO-BAR dangle "
                "as a generic structure diagnostic",
                len(struct_dangling) == 1 and struct_dangling[0].node_id == "FOO-BAR",
                f"got {len(struct_dangling)}")

        # -- (4) --dangle + structure together: no duplicate reporting of the
        #        same forward-dangle fact under two names -----------------------
        both = lint(str(dangle_file), dangle=True, structure=True)
        dup_dangling = [f for f in both.findings if f.origin == "structure" and f.kind == "dangling"]
        c.check("structure+dangle together: the FOO-BAR fact is NOT duplicated "
                "(suppressed from structure.dangling, present once as dangle.forward)",
                len(dup_dangling) == 0 and
                len([f for f in both.findings if f.origin == "dangle" and f.kind == DIR_FORWARD]) == 1)

    print(f"{'PASS' if c.failures == 0 else 'FAIL'}: {c.failures} failure(s)")
    return 1 if c.failures else 0


if __name__ == "__main__":
    sys.exit(run())
