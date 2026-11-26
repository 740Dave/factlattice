#!/usr/bin/env python3
"""CI check (part of `checks`): every package source in uv.lock is the default PyPI index.

    ci_lock_sources.py [--lock uv.lock]     exit 0 clean; 1 a package from another source (each is printed); 2 the lock could not be read

Reads the lock's own `source` field of every [[package]]. Green only for `{ registry = "https://pypi.org/simple" }`, plus ONE named exception:
the project itself, `factlattice`, whose source is exactly `{ editable = "." }`. Anything else is red: another registry URL, `git`, `path`, `url`,
`directory`, `virtual`, a second editable, or the root project's name with any other source. No pattern is matched, only the two literal forms.
There is deliberately no "package not named in pyproject" check (every transitive dependency would trip it).
This file is on the full path (tools/ci_*.py): a PR that edits it decides its own result.
"""
import argparse
import sys
import tomllib

PYPI = "https://pypi.org/simple"
ROOT_PROJECT = ("factlattice", {"editable": "."})  # the one expected exception, named in full


def offenders(lock):
    """-> [(package name, source)] for every package whose source is neither the PyPI registry nor the named root project."""
    out = []
    for pkg in lock.get("package", []):
        name, src = pkg.get("name"), pkg.get("source")
        if src == {"registry": PYPI}:
            continue
        if (name, src) == ROOT_PROJECT:
            continue
        out.append((name, src))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", default="uv.lock")
    a = ap.parse_args(argv)
    try:
        with open(a.lock, "rb") as fh:
            lock = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"[ci_lock_sources] FAIL: cannot read {a.lock}: {type(e).__name__}: {e}")
        return 2
    n = len(lock.get("package", []))
    if not n:
        print(f"[ci_lock_sources] FAIL: {a.lock} names no packages (a lock of nothing is not a pass)")
        return 2
    bad = offenders(lock)
    for name, src in bad:
        print(f"[ci_lock_sources] FAIL: package {name!r} has source {src!r}; only {{registry = {PYPI!r}}} and the root project {ROOT_PROJECT[0]!r} are allowed")
    if bad:
        return 1
    print(f"[ci_lock_sources] clean: {n} packages, all from {PYPI} (root project {ROOT_PROJECT[0]!r} excepted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
