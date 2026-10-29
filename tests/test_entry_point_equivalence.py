"""C3/C4 (Era 1) — entry-point equivalence, now GREEN and guarded.

⛔ THE HISTORY IS THE POINT AND IS KEPT DELIBERATELY. Through Era 0 these assertions
read `assert r["equivalent"] is False` and asserted THREE production paths diverging in
THREE different shapes — because they did (review V5/V5b/V5c). Era 1's C3/C4 threaded
`ParserConfig` through every path and taught `config.build_store` to honour a corpus's
own registered `profile:`, and the same instrument now reports EQUIVALENT.

The assertions were inverted deliberately, in the same commit as the fix, with the old
expectation recorded here rather than deleted — a test whose expectation changes
silently is indistinguishable from a test that was quietly weakened.

⛔ AND A GREEN INSTRUMENT NEEDS A NEGATIVE CONTROL MORE THAN A RED ONE DID. While the
defect existed, the instrument's ability to detect divergence was proven by the defect.
That proof is now gone. `test_negative_control_*` below injects the exact V5c fault and
requires the instrument to fire; if it stops firing, every green here is worthless.
"""
from __future__ import annotations

from factlattice_engine.selfcheck.entry_point_equivalence import (
    render_text,
    run,
    run_negative_control,
)


def test_every_production_path_agrees_with_the_reference():
    """THE HEADLINE. Configure the SAME vocabulary through each surface's own idiom —
    a profile dict, a corpus-registry `spec`, a `ParserConfig` — and get the SAME
    graph. MEASURED False on all three paths through Era 0; True as of C3/C4."""
    r = run()
    assert r["success"] is True
    assert r["divergences"] == [], render_text(r)
    assert r["equivalent"] is True


def test_all_three_probes_are_actually_present_on_the_reference():
    """Guards the degenerate green: if the reference parse minted NOTHING, every path
    would trivially 'agree' with it about nothing. All three probe edges must exist."""
    r = run()
    assert len(r["reference"]) == 3
    assert set(r["reference"]) == {
        "EPC-A EXEMPLIFIES EPC-B",      # property_edges  (V5)
        "EPC-A UPHOLDS EPC-B",          # domain_verbs    (V5b)
        "EPC-A dom.CUSTOM_TYPE EPC-B",  # profile_layers  (V5b)
    }


def test_every_path_actually_minted_all_three_probes():
    """Equivalence is asserted on CONTENT, not on emptiness. Each production path must
    carry all three probe edges — not merely fail to disagree about them."""
    r = run()
    assert len(r["probes"]) == 3
    for path_name, produced in r["probes"].items():
        assert len(produced) == 3, f"{path_name} minted {sorted(produced)}"
    # the layer the profile DECLARED, not the CORE_EDGE_LAYER default
    for produced in r["probes"].values():
        assert produced["EPC-A dom.CUSTOM_TYPE EPC-B"]["layer"] == "causal"
        assert produced["EPC-A UPHOLDS EPC-B"]["flags"] == []


def test_negative_control_the_instrument_still_detects_divergence():
    """⛔ THE PROOF THAT THE GREEN ABOVE MEANS ANYTHING the epistemics rule.

    Deny the live-engine path its registered profile — the exact V5c condition, where
    `corpus-config.json`'s `profile:` key was read by the audit surfaces and by no
    graph-building path — and the instrument must go red on that path and only that
    path."""
    n = run_negative_control()
    assert n["equivalent"] is False, (
        "THE INSTRUMENT IS BROKEN: it reported equivalence for a path explicitly "
        "denied its vocabulary. Every green from run() is worthless until this passes.")
    assert n["negative_control"]["fired"] is True
    assert n["negative_control"]["verdict"].startswith("PASS")
    # exactly one path starved -> exactly one path divergent
    assert {d["path"] for d in n["divergences"]} == {n["negative_control"]["starved"]}
    # and it loses precisely the three things the registered profile was carrying
    probes = {d["probe"] for d in n["divergences"]}
    assert probes == {"EPC-A EXEMPLIFIES EPC-B", "EPC-A UPHOLDS EPC-B",
                      "EPC-A dom.CUSTOM_TYPE EPC-B"}


def test_reference_path_needs_its_arguments_to_be_green():
    """The reference is not green BY CONSTRUCTION — remove the hooks and it reports the
    same absence a starved production path does. The falsifier for "the reference
    always agrees with itself because it is a tautology": it does not."""
    import os
    import shutil
    import tempfile

    from factlattice.parser import parse_file

    from factlattice_engine.selfcheck.entry_point_equivalence import CONTROL_DOC

    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "control.lts")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(CONTROL_DOC)
        pf = parse_file(path)          # NO vocabulary threaded at all
        assert "EXEMPLIFIES" not in {e.type for e in pf.edges}
        flagged = [e for e in pf.edges if e.type == "UPHOLDS"]
        assert flagged and "unregistered_verb" in flagged[0].flags
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_render_text_reports_the_verdict_both_ways():
    assert "EQUIVALENT" in render_text(run())
    assert "DIVERGENT" in render_text(run_negative_control())
