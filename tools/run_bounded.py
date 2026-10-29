#!/usr/bin/env python3
"""Run a command with a wall-clock bound: `run_bounded.py SECONDS -- cmd args...`.

The command runs in its OWN process group (new session). On timeout the whole group, including
grandchildren such as a test's spawned server, gets SIGTERM, a short grace, then ALWAYS SIGKILL
(a SIGTERM-ignoring grandchild must not survive just because the leader exited), and we exit 124
with a message on stderr. If this wrapper is itself sent SIGTERM/SIGINT/SIGHUP, it forwards the
same SIGTERM-then-SIGKILL to the group before exiting, so it never leaves orphans. Nothing outside
that group is ever signalled. A process that calls setsid() to leave the group is out of reach by
construction. SECONDS must be a finite positive number; anything else exits 125 (never unbounded).
Exit status passes through otherwise. Used by commit-if-green so a hung suite (2026-10-03: 13 min
blocked on an unbounded stdout read, at 0% CPU) is a loud refusal, not a silent wait.
"""
import math
import os
import signal
import subprocess
import sys

GRACE_S = 3.0


def _kill_group(pgid: int, leader: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)               # the group WE created (pgid == leader pid)
        except ProcessLookupError:
            return
        if sig == signal.SIGTERM:
            try:
                leader.wait(timeout=GRACE_S)
            except subprocess.TimeoutExpired:
                pass
    try:
        leader.wait(timeout=GRACE_S)
    except subprocess.TimeoutExpired:
        pass


def main(argv):
    if len(argv) < 3 or argv[1] != "--":
        print("usage: run_bounded.py SECONDS -- cmd [args...]", file=sys.stderr)
        return 2
    try:
        limit = float(argv[0])
    except ValueError:
        limit = float("nan")
    if not math.isfinite(limit) or limit <= 0:
        print(f"run_bounded: bound {argv[0]!r} is not a finite positive number of seconds; refusing "
              "(an unbounded run is never allowed)", file=sys.stderr)
        return 125
    p = subprocess.Popen(argv[2:], start_new_session=True)

    def _forward(signum, _frame):
        _kill_group(p.pid, p)
        sys.exit(128 + signum)
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, _forward)
    try:
        return p.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        print(f"run_bounded: command exceeded {limit:g}s; killing its process group {p.pid}",
              file=sys.stderr, flush=True)
        _kill_group(p.pid, p)
        return 124


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
