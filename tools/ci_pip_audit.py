#!/usr/bin/env python3
"""CI check (part of `checks`): the installed locked environment has no known vulnerability (pip-audit).

    ci_pip_audit.py [--path SITE_PACKAGES]     exit 0 no known vulnerability; non-zero otherwise (pip-audit's own code; 2 = could not run, including a path with no installed distribution)

Audits the site-packages that `uv sync --locked` installed (default: the purelib of the interpreter running this script, i.e. the locked
venv), with pip-audit PINNED to PIP_AUDIT_VERSION and run through `uv tool run --from pip-audit==<version>`. pip-audit is NOT in uv.lock: a
dependency group or extra would mean editing pyproject.toml's dependencies, which this change does not do; the pin lives here instead (a test
checks it equals ci_step.PIP_AUDIT_VERSION). NEEDS NETWORK ACCESS to the advisory database (PyPI JSON API); with none, pip-audit fails and so
does this check: red, never a silent pass. The project itself is not on PyPI, so pip-audit skips it by name.
"""
import argparse
import glob
import os
import subprocess
import sys
import sysconfig

PIP_AUDIT_VERSION = "2.10.1"


def command(path):
    return ["uv", "tool", "run", "--from", f"pip-audit=={PIP_AUDIT_VERSION}", "pip-audit", "--path", path, "--progress-spinner", "off"]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=sysconfig.get_paths()["purelib"])
    a = ap.parse_args(argv)
    n = len(glob.glob(os.path.join(a.path, "*.dist-info"))) if os.path.isdir(a.path) else 0
    if n == 0:  # F-E: an audit of nothing is not a pass (like ci_lock_sources and ci_gitleaks)
        print(f"[ci_pip_audit] FAIL: {a.path!r} is missing or holds no *.dist-info: nothing to audit (the audit did NOT run)")
        return 2
    print(f"[ci_pip_audit] {n} installed distributions in {a.path}", flush=True)
    cmd = command(a.path)
    print("[ci_pip_audit] " + " ".join(cmd), flush=True)
    try:
        rc = subprocess.run(cmd).returncode
    except OSError as e:
        print(f"[ci_pip_audit] FAIL: could not run uv: {e}")
        return 2
    print(f"[ci_pip_audit] {'no known vulnerabilities' if rc == 0 else 'FAIL: pip-audit exit ' + str(rc)}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
