"""Which root resolver this interpreter selected (spec 38 MECH-BR-PROBE x-progress), for the tests that depend on it.

`factlattice_engine.rootstate.realpath_progress` is the engine's copy of CPython 3.13.2's `posixpath.realpath` (`_realpath_copy`, which reports progress
after every component) only when the running interpreter's own algorithm equals it; on any other interpreter it is `_realpath_fallback` (`os.path.realpath`
under an elapsed budget of the root deadline x the path's components + `FALLBACK_MARGIN_S`). A test whose window assumes a hung root is marked after ONE
root deadline needs the per-component resolver; on the fallback the mark comes after the allowance, which is what `test_fallback_*` checks instead.
The predicate is the identity of the selected function itself, not a version string.
"""
from __future__ import annotations

import pytest

NEEDS_PER_COMPONENT_REASON = ("needs the per-component resolver (CPython 3.13.2 copy); this interpreter uses the elapsed-budget fallback "
                              "(spec 38 x-progress)")
NEEDS_FALLBACK_REASON = "needs the elapsed-budget fallback; this interpreter selected the per-component resolver (CPython 3.13.2 copy)"


def copy_selected() -> bool:
    from factlattice_engine import rootstate
    return rootstate.realpath_progress is rootstate._realpath_copy


#: skip unless the per-component resolver is the one selected on this interpreter
needs_per_component_resolver = pytest.mark.skipif(not copy_selected(), reason=NEEDS_PER_COMPONENT_REASON)
#: skip unless the elapsed-budget fallback is the one selected on this interpreter
needs_fallback_resolver = pytest.mark.skipif(copy_selected(), reason=NEEDS_FALLBACK_REASON)
