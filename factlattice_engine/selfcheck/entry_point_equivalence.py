"""ENTRY-POINT EQUIVALENCE — Era 1, C3/C4 (review V5 / V5b / V5c).

Parses ONE (document, profile) control pair through every production parse path this
engine has, and diffs the resulting edge sets against a REFERENCE parse that threads
every vocabulary hook the parser accepts.

THE INVARIANT UNDER TEST, stated so it is falsifiable:

    **A parse result is a function of `(bytes, ParserConfig)` and NOTHING ELSE.
    The entry point is not an input.**

⛔ EACH PATH IS CONFIGURED THROUGH ITS OWN REAL SURFACE, AND THAT IS THE POINT.
Era 0's version of this module handed each path whatever object it happened to accept
and recorded the divergence. That measured the defect but could not measure the FIX:
a path given a vocabulary it structurally cannot carry will diverge forever, and
"diverges forever" and "is broken" are different facts. So each path here is set up the
way a DEPLOYMENT sets it up —

  * `factlattice.corpus.parse()`            <- a profile (dict / path), as `lts_lint` does
  * `factlattice_engine.config.build_partition` <- a corpus-registry `spec` carrying
    `profile:`, exactly the shape `private/corpus-config.json` uses
  * `factlattice.index_builder.build_index` <- a `ParserConfig`

— and equivalence then means what a reader assumes it means: configure the SAME
vocabulary by each surface's own idiom, get the SAME graph.

⛔ WHY A NEGATIVE CONTROL SHIPS INSIDE THE INSTRUMENT. Before C3/C4 this module was
red, so its ability to detect divergence was proven by the defect itself. Once the
defect is fixed that proof is gone, and a green result from an instrument that has
never been seen to fail is not evidence (the epistemics rule: an instrument that
can only report SUCCESS is not an instrument). `run_negative_control()` re-runs the
identical comparison with ONE path deliberately starved of the profile, and asserts it
comes back DIVERGENT. If the negative control ever goes green, the comparison itself
has stopped working and `run()`'s green means nothing.

WHAT THE CONTROL PAIR PROBES — three INDEPENDENT vocabulary hooks, deliberately
isolated onto three different edges so a reader can attribute each divergence to its
cause without cross-talk:

  * `exemplifies` property        -> `property_edges` (V5)
  * a bare `UPHOLDS` verb         -> `domain_verbs`   (V5b)
  * `dom.CUSTOM_TYPE`'s LAYER     -> `profile_layers` (V5b)

and the registry `spec` carrying `profile:` is what probes V5c — the corpus's own
registered profile reaching the SERVED graph, not merely the audit surfaces.
"""
from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

# --- the control document -----------------------------------------------------
#   `dom.CUSTOM_TYPE` sits under a NON-canonical subhead ("Cross-Domain") on purpose:
#   a canonical `### Semantic` subhead would set `rel_layer` and the profile's D5 table
#   would never be CONSULTED, which would make that probe measure nothing.
CONTROL_DOC = """# Control [SPEC:EPC-ROOT]

## [EPC-A] A
- exemplifies: [EPC-B]

## [EPC-B] B

## Relationships
### Semantic
- [EPC-A] UPHOLDS [EPC-B]

### Cross-Domain
- [EPC-A] dom.CUSTOM_TYPE [EPC-B]
"""

# The SAME vocabulary, expressed once, then handed to each surface in its own idiom.
REGISTERED_NS = frozenset({"core", "dom"})
DOMAIN_VERBS = frozenset({"UPHOLDS"})
PROPERTY_EDGES: Dict[str, Tuple[str, str, bool]] = {
    "exemplifies": ("EXEMPLIFIES", "semantic", False),
}
PROFILE_LAYERS: Dict[str, str] = {"dom.CUSTOM_TYPE": "causal"}

# The dict-shaped profile `factlattice.corpus.parse()` / `load_profile` reads. It
# declares ALL THREE hooks — the verb, the minting key, and the namespaced type's
# layer — because a profile that declared only some of them would make this test
# measure the profile's incompleteness rather than the engine's threading.
_DICT_PROFILE: Dict[str, Any] = {
    "domain": {"id": "epc", "namespaces": ["dom"]},
    "prefixes": [{"prefix": "EPC-"}],
    "relationship_verbs": {"domain": ["UPHOLDS"]},
    "edge_types": [
        {"property_key": "exemplifies", "type": "EXEMPLIFIES", "layer": "semantic"},
        {"type": "dom.CUSTOM_TYPE", "layer": "causal"},
    ],
}

# The `.lts` serialization of the SAME profile — this is what a real corpus registers
# through `corpus-config.json`'s `profile:` key, and it is the V5c probe.
_LTS_PROFILE = """# EPC profile [PROFILE:EPC-PROFILE]

## [PROFILE-EPC] Domain identity
- domain-id: epc
- namespaces: dom

## [PREFIX-EPC] Node prefixes
- registers-prefix: EPC-

## [EDGETYPE-UPHOLDS] A preserves invariant B
- edge-type: UPHOLDS
- tier: domain

## [EDGETYPE-EXEMPLIFIES] A is an instance of B
- edge-type: EXEMPLIFIES
- tier: domain
- property-key: exemplifies
- layer: semantic

## [EDGETYPE-CUSTOM] a namespaced type whose layer the profile declares
- edge-type: dom.CUSTOM_TYPE
- layer: causal
"""

_PROBE_EDGES = {("EPC-A", "EXEMPLIFIES", "EPC-B"),
                ("EPC-A", "UPHOLDS", "EPC-B"),
                ("EPC-A", "dom.CUSTOM_TYPE", "EPC-B")}


def _edge_sig(e: dict) -> Tuple[str, str, str, str, Tuple[str, ...]]:
    return (e["source"], e["type"], e["target"], e["layer"],
            tuple(sorted(e.get("flags") or [])))


def _probe_edges(sigs: List[tuple]) -> Dict[Tuple[str, str, str], dict]:
    out = {}
    for s in sigs:
        key = (s[0], s[1], s[2])
        if key in _PROBE_EDGES:
            out[key] = {"layer": s[3], "flags": list(s[4])}
    return out


def _reference_config():
    """Every hook threaded by hand — the definition of "fully wired"."""
    from factlattice.parser_config import ParserConfig
    return ParserConfig.from_kwargs(
        registered_ns=REGISTERED_NS, profile_layers=PROFILE_LAYERS,
        domain_verbs=DOMAIN_VERBS, property_edges=PROPERTY_EDGES)


def _run_path_a(root: str, profile: Any) -> List[tuple]:
    """`factlattice.corpus.parse()` — `lts_lint` / `lts_preview` / `lts_profile_audit`."""
    from factlattice.corpus import parse as corpus_parse
    pc = corpus_parse(source=root, profile=profile)
    return sorted(_edge_sig(e.to_dict()) for e in pc.edges)


def _run_path_b(root: str, spec: Optional[dict]) -> List[tuple]:
    """`config.build_partition` + `incremental` — the LIVE MCP query engine behind
    `lts_find` / `lts_traverse` / a registered corpus's `lts_lint`. Configured by a
    corpus-registry `spec`, which is the V5c surface."""
    from factlattice.domain_profiles import ProfileEdgeConfig
    from factlattice_engine.config import build_partition
    pec = ProfileEdgeConfig(edge_layers={}, namespaces=frozenset())
    part = build_partition("epc", root, "epc", profile=pec, spec=spec)
    return sorted(_edge_sig(e) for layer in part.index["layers"].values()
                  for e in layer["edges"])


def _run_path_c(root: str, config) -> List[tuple]:
    """`factlattice.index_builder.build_index` with a `ParserConfig`. Before C3/C4 this
    function had NO `property_edges` parameter at all, so `exemplifies` could not mint
    here even in principle — the omission was invisible because a parameter that does
    not exist is not one anyone forgot to pass."""
    from factlattice.index_builder import build_index
    idx = build_index(root, corpus_id="epc", domain_suffix="epc", config=config)
    return sorted(_edge_sig(e) for layer in idx["layers"].values()
                  for e in layer["edges"])


def _run_path_reference(doc_path: str) -> List[tuple]:
    from factlattice.parser import parse_file
    pf = parse_file(doc_path, config=_reference_config())
    return sorted(_edge_sig(e.to_dict()) for e in pf.edges)


def _compare(reference: dict, paths: Dict[str, dict]) -> List[Dict[str, Any]]:
    divergences: List[Dict[str, Any]] = []
    for path_name, produced in paths.items():
        for probe in sorted(_PROBE_EDGES):
            want = reference.get(probe)
            got = produced.get(probe)
            if want == got:
                continue
            src, etype, tgt = probe
            if got is None:
                why = (f"`{etype}` MINTED NOTHING on this path (present on the "
                       f"reference parse) -- the hook this probe exercises is not "
                       f"threaded here at all")
            elif want is not None and got.get("layer") != want.get("layer"):
                why = (f"layer mismatch: reference says {want['layer']!r} (profile D5 "
                       f"table honoured), this path produced {got['layer']!r} "
                       f"(CORE_EDGE_LAYER default -- the profile's per-domain layer "
                       f"table was never consulted)")
            elif want is not None and got.get("flags") != want.get("flags"):
                why = (f"flag mismatch: reference flags={want['flags']!r}, this path "
                       f"produced flags={got['flags']!r} -- the verb registration this "
                       f"probe exercises is not threaded here")
            else:
                why = "differs from the reference; see `expected`/`produced`"
            divergences.append({"path": path_name, "probe": f"{src} {etype} {tgt}",
                                "expected": want, "produced": got, "why": why})
    return divergences


def _run(starve: Optional[str] = None) -> Dict[str, Any]:
    """Run the comparison. `starve` names a path to deliberately deny the profile —
    that is the negative control, and it MUST come back divergent."""
    tmp = tempfile.mkdtemp(prefix="factlattice-epc-")
    try:
        doc_path = os.path.join(tmp, "control.lts")
        with open(doc_path, "w", encoding="utf-8") as fh:
            fh.write(CONTROL_DOC)
        # The profile lives OUTSIDE the corpus root: a `.lts` profile inside the root
        # would also be parsed as a corpus file, and its own EDGETYPE nodes would join
        # the graph under test. (It carries `document_tag: PROFILE` so it would in fact
        # be excluded — but relying on that here would make this test depend on the
        # exclusion mechanism as well as on threading, which is one variable too many.)
        prof_dir = tempfile.mkdtemp(prefix="factlattice-epc-prof-")
        prof_path = os.path.join(prof_dir, "epc-profile.lts")
        with open(prof_path, "w", encoding="utf-8") as fh:
            fh.write(_LTS_PROFILE)

        reference = _probe_edges(_run_path_reference(doc_path))
        A = "A_corpus_parse (lts_lint / lts_preview / lts_profile_audit)"
        B = ("B_live_engine (config.build_partition + incremental -- lts_find / "
             "lts_traverse / a registered corpus's lts_lint)")
        C = "C_build_index (factlattice.index_builder.build_index)"
        paths = {
            A: _probe_edges(_run_path_a(tmp, None if starve == A else _DICT_PROFILE)),
            B: _probe_edges(_run_path_b(
                tmp, None if starve == B else {"profile": prof_path})),
            C: _probe_edges(_run_path_c(
                tmp, None if starve == C else _reference_config())),
        }
        divergences = _compare(reference, paths)
        return {
            "success": True,
            "starved_path": starve,
            "reference": {f"{k[0]} {k[1]} {k[2]}": v for k, v in reference.items()},
            "probes": {name: {f"{k[0]} {k[1]} {k[2]}": v for k, v in produced.items()}
                       for name, produced in paths.items()},
            "divergences": divergences,
            "equivalent": not divergences,
        }
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            shutil.rmtree(prof_dir, ignore_errors=True)
        except NameError:
            pass


def run() -> Dict[str, Any]:
    """Parse the control pair through every production path and report agreement.

    `equivalent` is the headline. It is True as of Era 1 (C3/C4/V5c); it was False on
    every production path before, by design of the defect rather than of the test.
    """
    result = _run()
    result["detection_status"] = (
        "MUST PASS as of Era 1 (C3/C4). Was KNOWN RED through Era 0: `property_edges` "
        "reached zero production paths, `domain_verbs` and `profile_layers` reached "
        "one different path each, and the corpus's own REGISTERED profile reached the "
        "served graph through none (V5c). Prove this instrument still works with "
        "`run_negative_control()`.")
    return result


def run_negative_control() -> Dict[str, Any]:
    """⛔ THE PROOF THAT `run()`'s GREEN MEANS ANYTHING.

    Re-runs the identical comparison with the LIVE ENGINE path deliberately denied its
    registered profile — the exact V5c condition — and asserts the comparison notices.
    A green result HERE is a failure of the instrument, not of the engine, and it
    invalidates `run()`'s verdict rather than merely sitting beside it.
    """
    B = ("B_live_engine (config.build_partition + incremental -- lts_find / "
         "lts_traverse / a registered corpus's lts_lint)")
    result = _run(starve=B)
    result["negative_control"] = {
        "starved": B,
        "expectation": "DIVERGENT — the starved path must lose EXEMPLIFIES and gain "
                       "an `unregistered_verb` flag on UPHOLDS",
        "fired": not result["equivalent"],
        "verdict": "PASS (the instrument can still detect divergence)"
                   if not result["equivalent"] else
                   "FAIL — THE INSTRUMENT IS BROKEN. It reported equivalence for a "
                   "path it was explicitly denied the vocabulary. Every green from "
                   "`run()` is now worthless until this is fixed.",
    }
    return result


def render_text(result: Dict[str, Any]) -> str:
    lines = [f"entry-point equivalence: "
             f"{'EQUIVALENT' if result['equivalent'] else 'DIVERGENT'}"]
    if result.get("detection_status"):
        lines.append(f"  {result['detection_status']}")
    if result.get("negative_control"):
        nc = result["negative_control"]
        lines.append(f"  negative control [{nc['starved']}]: {nc['verdict']}")
    if result["divergences"]:
        lines.append(f"  {len(result['divergences'])} divergence(s):")
        for d in result["divergences"]:
            lines.append(f"    [{d['path']}] {d['probe']}")
            lines.append(f"      expected: {d['expected']!r}")
            lines.append(f"      produced: {d['produced']!r}")
            lines.append(f"      why: {d['why']}")
    return "\n".join(lines)


__all__ = ["run", "run_negative_control", "render_text", "CONTROL_DOC",
           "PROPERTY_EDGES", "DOMAIN_VERBS", "PROFILE_LAYERS"]
