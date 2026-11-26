#!/usr/bin/env python3
"""McCabe complexity ratchet. Adapted from a sibling repository's complexity ratchet; changed: names, the ruff
configuration is the GATE's pyproject.toml (--gate), and the baseline is tools/ci_complexity_baseline.json of the GATE.

    python tools/ci_complexity_ratchet.py check     # default
    python tools/ci_complexity_ratchet.py update    # lower-only: drops fixed entries, lowers reduced ones, REFUSES to add or raise
    python tools/ci_complexity_ratchet.py update --force   # an explicit, reviewed act: re-record everything as it is now
    python tools/ci_complexity_ratchet.py report    # print counts

FAILS if (a) a function over the threshold (15) is not in tools/ci_complexity_baseline.json (a new complex function), or
(b) a function that is in the baseline has a HIGHER complexity than recorded. Functions at or under 15 are not tracked, so a
function may grow while it stays <= 15. Entries are keyed by root-relative file + qualified function name (Class.method,
outer.inner). MOVING OR RENAMING A FUNCTION (or its class, or its file) MAKES IT A NEW FUNCTION: if it is over 15 it fails until
you either simplify it or deliberately `update --force`. That is intended: a rename is the cheap way to dodge a ratchet.

Why ruff's C901 and not the mccabe module: ruff is already the linter in the shared toolchain, so the ratchet and `ruff check`
agree on every number (same McCabe implementation, same parser, same exclusions); it is fast (0.3 s over the whole tree);
and the pinned version comes from uv.lock. We run it with max-complexity 0 so that every function is reported, then apply the
15 threshold here. `--ignore-noqa` is passed so a `# noqa: C901` comment cannot hide a function from the ratchet.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import ci_ruff_support as rs  # tools/ is sys.path[0] when this file is run as a script

REPO = Path(__file__).resolve().parent.parent
THRESHOLD = 15
MSG = re.compile(r"`(?P<name>.+?)` is too complex \((?P<n>\d+) > 0\)")


def effective_baseline(root: Path, gate: Path) -> dict[str, int]:
    """The baseline the check judges by. When the gate is the tree itself (a local run, ci.yml): that file. In the merge queue
    (the gate is the default branch's checkout, the tree the merged result) a function is budgeted only if BOTH baselines list it,
    at the LOWER of the two scores: a change can tighten the ratchet (drop an entry, lower a score) and never loosen it (add an
    entry, raise a score). The tree's file is read as data. A missing or unreadable tree baseline budgets nothing: every function
    over 15 is then new, so the run is red.
    """
    gate_file = gate / "tools" / "ci_complexity_baseline.json"
    base = rs.load_json(gate_file, {}).get("functions", {})
    if root.resolve() == gate.resolve():
        return base
    try:
        mine = rs.load_json(root / "tools" / "ci_complexity_baseline.json", {}).get("functions", {})
        mine = {k: v for k, v in mine.items() if isinstance(v, int)}
    except (ValueError, AttributeError):
        print("complexity_ratchet: the tree's baseline is unreadable: it budgets nothing", file=sys.stderr)
        mine = {}
    return {k: min(v, mine[k]) for k, v in base.items() if k in mine}


def measure(root: Path, gate: Path) -> dict[str, int]:
    """{ "file::qualname": complexity } for EVERY function (duplicates of one qualname get #2, #3 in source order).
    ruff's configuration (line length, excludes) is the GATE's pyproject.toml, never the tree's own."""
    findings = rs.run_ruff_json(
        root,
        gate,
        [
            "--config",
            str(gate / "pyproject.toml"),
            "--select",
            "C901",
            "--ignore-noqa",
            "--config",
            "lint.mccabe.max-complexity = 0",
            "--config",
            "lint.per-file-ignores = {}",
        ],
    )
    findings.sort(key=lambda f: (f["filename"], f["location"]["row"]))
    infos: dict[str, rs.FileInfo] = {}
    out: dict[str, int] = {}
    for f in findings:
        m = MSG.search(f["message"])
        if not m:
            sys.exit(f"complexity_ratchet: cannot parse ruff message {f['message']!r}")
        name, row = f["filename"], f["location"]["row"]
        if name not in infos:
            infos[name] = rs.FileInfo(root / name)
        info = infos[name]
        qn = info.def_at(row) or m["name"]
        key = f"{name}::{qn}"
        i = 2
        while key in out:
            key, i = f"{name}::{qn}#{i}", i + 1
        out[key] = int(m["n"])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="check", choices=["check", "update", "report"])
    ap.add_argument("--root", type=Path, default=Path.cwd(), help="the tree under test (default: the current directory)")
    ap.add_argument(
        "--gate",
        type=Path,
        default=REPO,
        help="the checkout holding the gate: its pyproject.toml and baseline (default: this script's repo)",
    )
    ap.add_argument("--min-files", type=int, default=rs.MIN_FILES_DEFAULT, help="fail if ruff would scan fewer files than this")
    ap.add_argument("--baseline", type=Path, default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    gate = args.gate.resolve()
    rs.require_scanned_files(root, gate, args.min_files)
    baseline_path = args.baseline or (gate / "tools" / "ci_complexity_baseline.json")
    now = measure(root, gate)
    over = {k: v for k, v in now.items() if v > THRESHOLD}

    if args.mode == "report":
        print(f"functions measured: {len(now)}; >{THRESHOLD}: {len(over)}; >50: {sum(1 for v in now.values() if v > 50)}")
        return 0

    if args.mode == "update":
        old = rs.load_json(baseline_path, {}).get("functions", {})
        if args.force or not old:
            new = over
        else:
            added = [k for k in over if k not in old]
            raised = [k for k in over if k in old and over[k] > old[k]]
            if added or raised:
                print(
                    "complexity_ratchet: refusing to update: would add/raise " + ", ".join(sorted(added + raised)[:10]) + " (use --force)",
                    file=sys.stderr,
                )
                return 1
            new = over
        rs.dump_json(baseline_path, {"threshold": THRESHOLD, "functions": new})
        print(f"complexity_ratchet: recorded {len(new)} function(s) over {THRESHOLD} -> {baseline_path}")
        return 0

    if not baseline_path.exists():
        print(f"complexity_ratchet: FAIL: baseline {baseline_path} is missing", file=sys.stderr)
        return 2
    base = rs.load_json(baseline_path, {}).get("functions", {}) if args.baseline else effective_baseline(root, gate)
    problems = []
    for key, n in sorted(over.items()):
        if key not in base:
            problems.append(f"NEW function over {THRESHOLD}: {key} = {n} (a moved/renamed function counts as new)")
        elif n > base[key]:
            problems.append(f"INCREASED: {key} {base[key]} -> {n}")
    for key in sorted(base):
        if key not in now:
            problems.append(f"STALE: {key} is in the baseline but no such function exists: remove it (the baseline only shrinks)")
    if problems:
        print("complexity_ratchet: FAIL:", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        return 1
    improved = sum(1 for k, v in base.items() if now.get(k, 0) < v)
    print(
        f"complexity_ratchet: OK ({len(over)} over {THRESHOLD}; {improved} baselined function(s) improved or gone; run `update` to lock in)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
