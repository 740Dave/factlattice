#!/usr/bin/env python3
"""CI check `smoke`: the packages import on the Python that runs this, and `lts_selfcheck` passes through the engine's tool door
(`python -m factlattice_engine call lts_selfcheck --json`) against the BUILT-IN EXAMPLE REGISTRY.

    ci_smoke.py            exit 0 pass; 1 fail (the failing step is named); never reads or writes the real registry or state

The selfcheck runs with a fresh scratch HOME and state/backup dirs and an environment built from a short list of named variables (no
FACTLATTICE_* variable passes), and the result must say `source: example` (a smoke that quietly used a real registry would be a
different test). The registry variables are not named in this file: only factlattice/registry.py may name them (tests/_registry_guard.py).
"""
import importlib
import json
import os
import subprocess
import sys
import tempfile

MODULES = (
    "factlattice",
    "factlattice.parser",
    "factlattice.authoring",
    "factlattice.selfcheck",
    "factlattice_engine",
    "factlattice_engine.server",
    "factlattice_engine.analytics",
    "factlattice_engine.lint",
    "factlattice_engine.render",
    "factlattice_engine.reviewer",
    "factlattice_engine.selfcheck",
)


def fail(msg):
    print(f"[ci_smoke] FAIL: {msg}", flush=True)
    return 1


def main():
    print(f"[ci_smoke] python {sys.version.split()[0]} at {sys.executable}", flush=True)
    for m in MODULES:
        try:
            importlib.import_module(m)
        except Exception as e:  # any import-time failure is red, with the module named
            return fail(f"import {m}: {type(e).__name__}: {e}")
    print(f"[ci_smoke] imported {len(MODULES)} modules", flush=True)
    scratch = tempfile.mkdtemp(prefix="ci-smoke-")  # left in place
    env = {  # explicit, literal reads only (the registry guard rejects a whole-environment copy)
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "LANG": os.environ.get("LANG", ""),
        "LC_ALL": os.environ.get("LC_ALL", ""),
        "TMPDIR": os.environ.get("TMPDIR", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env = {k: v for k, v in env.items() if v}
    for var, sub in (("HOME", "home"), ("FACTLATTICE_STATE_DIR", "st"), ("FACTLATTICE_BACKUP_DIR", "bk"), ("LTS_ENGINE_STATE_DIR", "lt")):
        env[var] = os.path.join(scratch, sub)
        os.makedirs(env[var])
    cmd = [sys.executable, "-m", "factlattice_engine", "call", "lts_selfcheck", "--json"]
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        return fail(f"`{' '.join(cmd[1:])}` exit {p.returncode}: {(p.stdout + p.stderr)[-600:]}")
    try:
        d = json.loads(p.stdout)
    except ValueError as e:
        return fail(f"selfcheck output is not JSON ({e}): {p.stdout[:300]}")
    src = d.get("call_context", {}).get("corpus_config", {}).get("source")
    if d.get("tool") != "lts_selfcheck" or d.get("success") is not True or d.get("refused") is not False:
        return fail(f"selfcheck did not succeed: success={d.get('success')} refused={d.get('refused')} tool={d.get('tool')}")
    r = d.get("result") or {}
    if r.get("success") is not True or r.get("verdict") != "PASS" or r.get("failed") != 0 or not r.get("passed"):
        return fail(f"selfcheck result: success={r.get('success')} verdict={r.get('verdict')} passed={r.get('passed')} failed={r.get('failed')}")
    if src != "example":
        return fail(f"selfcheck ran against registry source {src!r}, not the built-in example registry")
    print(f"[ci_smoke] lts_selfcheck PASS ({r['passed']} cases) on the built-in example registry", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
