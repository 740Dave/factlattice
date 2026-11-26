"""[FL-BOUNDED-ROOTS] the elapsed-budget fallback resolver, through the real server (spec 38 MECH-BR-PROBE x-progress).

On an interpreter whose `posixpath.realpath` is not the CPython 3.13.2 algorithm the engine copied, `rootstate.realpath_progress` is
`_realpath_fallback`: it cannot report progress per component, so the resolve is given an ELAPSED BUDGET of the root deadline times the number of
components of the root path (counted lexically) plus a fixed margin, and a hung root is still marked once that budget is spent. The tests that assume
a mark after ONE root deadline (c2, c4, c5, ...) need the per-component copy and are skipped on the fallback (`tests/resolver_mode.py`); this file is
what keeps a real check on the fallback, so that 3.11, 3.12 and 3.14 are not left with only a skip.

THE ALLOWANCE IS PINNED HERE, NOT READ FROM THE ENGINE. A bound computed from `rootstate.FALLBACK_MARGIN_S` or from the engine's own allowance would
move with a mutated engine and could never go red on it. The spec's numbers are written below: margin 1.0 s, root deadline 1 s (this file's environment),
components counted lexically in the root's path as the registry spells it.

WHAT THIS DOES NOT SHOW: a real hung drive (the stall harness is a model, see its docstring), the copy's behaviour (that is the other tests), or any
interpreter on which the copy is selected (this test is skipped there, with a stated reason).
"""
from __future__ import annotations

import time

from tests.resolver_mode import needs_fallback_resolver
from tests.test_bounded_roots import CALL_OK_S, CODE_UNRESPONSIVE, ROOT_DEADLINE_S, Asker, Checks, _boot_then_stall, entry, ws  # noqa: F401  (ws is a fixture)

#: spec 38 x-progress: seconds added to root deadline x components
SPEC_MARGIN_S = 1.0
#: slack beyond the computed allowance, stated: the watcher polls once a second, and a loaded host may be late by a second or two more
SLACK_S = 3.0
POLL_S = 0.2


def _components(path) -> int:
    return max(1, len([c for c in str(path).split("/") if c]))


@needs_fallback_resolver
def test_fallback_a_hung_root_is_marked_within_its_allowance_and_a_healthy_corpus_keeps_answering(ws, record_property):
    """On the fallback, b1 (hung after boot) must be marked `unresponsive` no later than ROOT_DEADLINE_S x (components of b1's root path) + SPEC_MARGIN_S
    + SLACK_S after the stall begins, while a1 keeps answering every call inside CALL_OK_S, and a named read of b1 then says `root-unresponsive`.
    Not asserted: that a1 answers DURING the unmarked window (the hang-onset window, a known limit with its own xfail records).
    RED when the fallback never marks (an allowance made huge, or a resolve whose silence is never measured): the mark does not arrive in time."""
    from factlattice_engine import rootstate
    assert rootstate.realpath_progress is rootstate._realpath_fallback            # the guard the decorator also states, here as the test's own premise
    n = _components(ws.root("b1"))
    allowance = ROOT_DEADLINE_S * n + SPEC_MARGIN_S
    limit = allowance + SLACK_S
    ck = Checks()
    srv = _boot_then_stall(ws)
    try:
        ask = Asker(srv, ck)
        srv.engage()
        t0 = time.monotonic()
        # Until the first poll marks b1, a call that has to probe it waits on the request thread (the documented hang-onset window, see the xfail
        # `test_onset_*` records), so the listing is asked with the WHOLE allowance as its limit: its reply, with b1 unresponsive, is the mark.
        listing, _ = ask("list, waiting for the mark", "lts_list_corpora", {"heal": False}, limit=limit)
        marked_after = time.monotonic() - t0
        b1 = entry(listing, "b1")
        marked = b1 is not None and (b1.get("root_health") or {}).get("status") == "unresponsive"
        record_property("fallback_components", n)
        record_property("fallback_allowance_s", allowance)
        record_property("fallback_marked_after_s", round(marked_after, 2) if marked else None)
        ck.ok(marked, f"b1 was not marked unresponsive within its allowance: allowance {ROOT_DEADLINE_S}s x {n} components + {SPEC_MARGIN_S}s = {allowance:.1f}s, "
                      f"plus slack {SLACK_S}s = {limit:.1f}s (the reply, if any, came after {marked_after:.2f}s: {b1})")
        # the healthy corpus keeps answering, repeatedly, each inside CALL_OK_S, while b1 stays hung
        for i in range(5):
            r, dt = ask(f"healthy a1 (round {i})", "lts_find", {"corpus": "a1", "text": "lanternfish", "tier": "digest"})
            ck.ok(r is None or (r.get("success") and r.get("count") == 1 and dt <= CALL_OK_S), f"a1 answers inside {CALL_OK_S}s (round {i}): {r and r.get('reason')} after {dt}s")
            time.sleep(0.2)
        r, _ = ask("named b1 after the mark", "lts_find", {"corpus": "b1", "text": "lanternfish"})
        ck.ok(r is not None and r.get("success") is False and r.get("reason") == CODE_UNRESPONSIVE, f"a named read of b1 refuses {CODE_UNRESPONSIVE}: {r}")
        srv.require_hit()
    finally:
        srv.close()
    ck.done(f"fallback (hung root, {n} components, allowance {allowance:.1f}s + slack {SLACK_S}s)")
