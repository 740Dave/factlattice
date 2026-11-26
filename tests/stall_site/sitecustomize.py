"""Test-only interpreter start-up hook for the stall harness. NOT part of the engine and not in the package list.

Python imports `sitecustomize` at the start of EVERY interpreter, and the environment (PYTHONPATH and the spec variable) is
inherited across os.execv, so a server that re-execs itself on a code change (build_stamp.reexec_argv re-enters as
`python -m factlattice_engine.server`) comes back up with the block reinstalled. A launcher script would be gone after the exec.

It acts only when the harness's spec variable is set, and only in the first process that claims the owner file (the server
itself, and the same pid after an exec); a subprocess the server spawns finds another owner pid and does nothing. It shadows any
other `sitecustomize` later on the path for that child, which a test environment can accept.
"""
import importlib.util
import os
import sys

_SPEC_ENV = "FL_STALL_HARNESS_SPEC"

if os.environ.get(_SPEC_ENV):
    _h = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stall_harness.py")
    _s = importlib.util.spec_from_file_location("_fl_stall_harness", _h)
    _m = importlib.util.module_from_spec(_s)
    sys.modules["_fl_stall_harness"] = _m
    _s.loader.exec_module(_m)
    _m.site_install(os.environ[_SPEC_ENV])
