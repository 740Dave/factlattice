"""[FL-CONFORMANCE] The construct × position cross-product, DERIVED not transcribed.

⭐ THE DISTINCTION THIS MODULE EXISTS TO DRAW.

`factlattice_engine.selfcheck` measures SILENT-LOSS RECALL: malformed input goes in, and
the question is whether the failure announces itself. That axis is open-ended by nature —
it grows only as live corpora teach a new class — and its `limit` field says so honestly:
"cannot prove there is no nineteenth."

This module measures the OTHER axis: CONFORMANCE. Well-formed input goes in, and the
question is whether the graph that comes out is the graph the specification says. ⛔ THAT
INPUT SPACE IS FINITE. It is not the wild; it is the construct list the format defines,
enumerable from three machine-readable sources:

  1. the parser's own tables            — CORE_EDGE_LAYER, REL_PROPERTY_EDGES,
                                          FLOW_PROPERTY_EDGES, REL_PROPERTY_ALIASES
  2. each loaded profile's vocabulary   — `relationship_verbs` (the validated extensions)
  3. the SPEC's construct list          — P1-P5, E1-E5, frontmatter, doctype tags,
                                          id-tables, wrapped values, continuations

"Cannot prove there is no nineteenth silent-loss class" is TRUE of recall and FALSE of
conformance — and while both lived in one suite, the open-ended disclaimer covered for the
completable half. That is precisely how two hand-picked relationship verbs came to stand
in for a table the engine itself enumerates: `control.expected.json`'s own case names say
"an edge-bearing key from REL_PROPERTY_EDGES (see-also)" and "a SECOND REL_PROPERTY_EDGES
key (depends-on)". Both worked. The suite was green forever while `relates-to`, `uses`,
`upholds` and `documents` minted nothing across 419 authored references.

⛔ NON-COMPLIANT INPUT IS A THIRD CATEGORY and is NOT measured here. A corpus full of
malformed keys is a hygiene problem; it says nothing about whether the parser implements
its own specification on input that IS well formed.

HOW TO ADD THE NEXT FAMILY. Write a generator that yields `Cell`s and register it in
`CONFORMANCE_FAMILIES`. The runner, the exemption discipline, the failure vocabulary and
the census are shared — a family is data plus an enumeration, never a bespoke test. The
verb family below is the worked example, not the special case.

⛔ AN UNEXPLAINED GAP IS A FAILURE, NEVER A SKIP. A construct that is deliberately valid
in only some of its positions must say so through a named exemption carrying its REASON
(`parser.TRIPLE_ONLY_VERBS`), the way a DELIBERATELY_UNMAPPED table works in a
pre-commit reachability gate. A cell with no edge and no reason breaks the build.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, Iterable, Iterator, List, Optional, Tuple

from .parser import (
    CORE_EDGE_LAYER,
    FLOW_PROPERTY_EDGES,
    REL_PROPERTY_EDGES,
    TRIPLE_ONLY_VERBS,
    _layer_for_type,
    parse_text,
    rel_property_edge_map,
    verb_property_key,
)

# The three syntactic positions a relationship verb has. Naming them here (rather than
# inline in the generator) is what makes "every construct × every position" a statement
# the code can make, instead of a claim a docstring makes.
POSITIONS: Tuple[str, ...] = ("property-key", "triple-bulleted", "triple-unbulleted")

SRC, TGT = "CF-A", "CF-B"


@dataclass(frozen=True)
class Expectation:
    """One edge a cell must produce."""
    type: str
    source: str
    target: str
    layer: str
    inference: str
    label: Optional[str] = None

    def as_tuple(self):
        return (self.type, self.source, self.target, self.layer, self.inference, self.label)


@dataclass(frozen=True)
class Cell:
    """One (construct, position) cell of a conformance cross-product.

    `exemption` is the ONLY sanctioned way for a cell to expect nothing. It carries the
    reason verbatim from the vocabulary that declared it, so the reason is reviewable in
    the same diff that creates the gap.
    """
    family: str
    construct: str
    position: str
    enumerated_from: str
    fixture: str
    domain_verbs: FrozenSet[str] = frozenset()
    profile_layers: Optional[Dict[str, str]] = None
    expect: Tuple[Expectation, ...] = ()
    exemption: Optional[str] = None
    spelling: Optional[str] = None

    @property
    def id(self) -> str:
        spelt = f"::{self.spelling}" if self.spelling and self.spelling != self.construct else ""
        return f"{self.family}/{self.construct}{spelt}/{self.position}"


# ---------------------------------------------------------------------------
# fixture rendering — one well-formed document per cell
# ---------------------------------------------------------------------------
_DOC_HEAD = (
    "---\n"
    'title: "conformance cell"\n'
    'id: "SPEC:CF"\n'
    "---\n"
    "# Conformance cell [SPEC:CF]\n"
)


def _property_fixture(key: str, value: str = f"[{TGT}]") -> str:
    return (f"{_DOC_HEAD}\n"
            f"## [{SRC}] Source\n\n"
            f"- {key}: {value}\n\n"
            f"## [{TGT}] Target\n\n"
            f"- status: ok\n")


def _triple_fixture(verb: str, bulleted: bool) -> str:
    bullet = "- " if bulleted else ""
    return (f"{_DOC_HEAD}\n"
            f"## [{SRC}] Source\n\n"
            f"- status: ok\n\n"
            f"## [{TGT}] Target\n\n"
            f"- status: ok\n\n"
            f"## Relationships\n\n"
            f"{bullet}[{SRC}] {verb} [{TGT}]\n")


# ---------------------------------------------------------------------------
# FAMILY: relationship-verb  (the worked example)
# ---------------------------------------------------------------------------
def relationship_verb_cells(
    domain_verbs: FrozenSet[str] = frozenset(),
    profile_layers: Optional[Dict[str, str]] = None,
    profile_label: str = "core-only",
) -> Iterator[Cell]:
    """Every verb the vocabulary registers, × every position a verb has.

    ⛔ NOTHING HERE IS HAND-LISTED. The verbs come from `CORE_EDGE_LAYER` and from the
    profile's own `domain_verbs`; the property spellings come from
    `rel_property_edge_map()`, which is the SAME derivation the parser runs at E4. A test
    that transcribed either would be the defect wearing a test's clothes.
    """
    fam = "relationship-verb"
    key_map = rel_property_edge_map(domain_verbs, profile_layers)
    flow_keys = set(FLOW_PROPERTY_EDGES)

    verbs: Dict[str, str] = {}
    for v in CORE_EDGE_LAYER:
        verbs[v] = "parser.CORE_EDGE_LAYER"
    for v in sorted(domain_verbs):
        verbs.setdefault(v, f"profile:{profile_label} relationship_verbs")

    # --- position: property-key -------------------------------------------
    # one cell per SPELLING, so an alias (`relates-to`) is independently falsifiable
    # from the mechanical form (`related-to`) it sits beside.
    spellings: Dict[str, List[str]] = {}
    for key, (etype, _layer, _rev) in key_map.items():
        spellings.setdefault(etype, []).append(key)

    for verb, origin in verbs.items():
        if verb in TRIPLE_ONLY_VERBS:
            yield Cell(family=fam, construct=verb, position="property-key",
                       enumerated_from=origin,
                       fixture=_property_fixture(verb_property_key(verb)),
                       domain_verbs=domain_verbs, profile_layers=profile_layers,
                       expect=(), exemption=TRIPLE_ONLY_VERBS[verb],
                       spelling=verb_property_key(verb))
            continue
        for key in sorted(spellings.get(verb, [])):
            etype, layer, reversed_dir = key_map[key]
            s, t = (TGT, SRC) if reversed_dir else (SRC, TGT)
            yield Cell(family=fam, construct=verb, position="property-key",
                       enumerated_from=origin, fixture=_property_fixture(key),
                       domain_verbs=domain_verbs, profile_layers=profile_layers,
                       expect=(Expectation(etype, s, t, layer, "property"),),
                       spelling=key)
        if not spellings.get(verb):
            # No spelling and no exemption -> the cell FAILS with an empty expectation
            # it cannot satisfy. This is the branch that went red on `uses`.
            yield Cell(family=fam, construct=verb, position="property-key",
                       enumerated_from=origin,
                       fixture=_property_fixture(verb_property_key(verb)),
                       domain_verbs=domain_verbs, profile_layers=profile_layers,
                       expect=(Expectation(verb, SRC, TGT,
                                           _layer_for_type(verb, profile_layers),
                                           "property"),),
                       spelling=verb_property_key(verb))

    # FLOW spellings are property-only forms of an existing core verb; enumerate them
    # so a table that grows a key grows a cell.
    for key in sorted(flow_keys):
        etype, layer, reversed_dir, label_spec = FLOW_PROPERTY_EDGES[key]
        value = "[%s]" % TGT if label_spec != "*" else "\n  - amber: [%s]" % TGT
        label = None if label_spec in (None, "*") else label_spec
        if label_spec == "*":
            label = "amber"
        s, t = (TGT, SRC) if reversed_dir else (SRC, TGT)
        yield Cell(family=fam, construct=etype, position="property-key",
                   enumerated_from="parser.FLOW_PROPERTY_EDGES",
                   fixture=_property_fixture(key, value),
                   domain_verbs=domain_verbs, profile_layers=profile_layers,
                   expect=(Expectation(etype, s, t, layer, "property", label),),
                   spelling=key)

    # --- positions: triples ------------------------------------------------
    for verb, origin in verbs.items():
        for position, bulleted in (("triple-bulleted", True), ("triple-unbulleted", False)):
            yield Cell(family=fam, construct=verb, position=position,
                       enumerated_from=origin, fixture=_triple_fixture(verb, bulleted),
                       domain_verbs=domain_verbs, profile_layers=profile_layers,
                       expect=(Expectation(verb, SRC, TGT,
                                           _layer_for_type(verb, profile_layers),
                                           "authored"),),
                       spelling=verb)


CONFORMANCE_FAMILIES: Dict[str, Callable[..., Iterator[Cell]]] = {
    "relationship-verb": relationship_verb_cells,
}


# ---------------------------------------------------------------------------
# the runner
# ---------------------------------------------------------------------------
@dataclass
class CellResult:
    cell: Cell
    ok: bool
    detail: str
    produced: Tuple[Tuple, ...] = ()


def check_cell(cell: Cell) -> CellResult:
    pf = parse_text(cell.fixture, "conformance.lts",
                    domain_verbs=cell.domain_verbs,
                    profile_layers=cell.profile_layers)
    produced = tuple(
        (e.type, e.source, e.target, e.layer, e.inference, getattr(e, "label", None))
        for e in pf.edges if e.inference != "structural"
    )
    if cell.exemption is not None:
        if produced:
            return CellResult(cell, False,
                              f"EXEMPT construct minted anyway: {produced!r}. An exemption "
                              f"claims the position is invalid; this one is live.", produced)
        if not cell.exemption.strip():
            return CellResult(cell, False, "exemption carries no reason", produced)
        return CellResult(cell, True, f"exempt: {cell.exemption.strip().splitlines()[0]}", produced)

    missing = [e.as_tuple() for e in cell.expect if e.as_tuple() not in produced]
    if missing:
        why = ("NOTHING minted — the construct parsed and the graph never saw it"
               if not produced else f"produced {produced!r}")
        return CellResult(cell, False,
                          f"expected {missing!r}; {why}. If this position is deliberately "
                          f"invalid for `{cell.construct}`, say so in "
                          f"parser.TRIPLE_ONLY_VERBS with a reason — a silent gap is a "
                          f"failure, not a skip.", produced)
    return CellResult(cell, True, "ok", produced)


def enumerate_cells(profiles: Optional[Iterable[Tuple[str, FrozenSet[str], Optional[Dict[str, str]]]]] = None,
                    families: Optional[Iterable[str]] = None) -> List[Cell]:
    """Every cell, for the core vocabulary and for each supplied profile vocabulary.

    `profiles` is an iterable of `(label, domain_verbs, profile_layers)`. The core-only
    run is always included, so a deployment with no profile is still fully checked.
    """
    names = list(families) if families else list(CONFORMANCE_FAMILIES)
    vocabularies = [("core-only", frozenset(), None)]
    vocabularies.extend(profiles or [])
    cells: List[Cell] = []
    for name in names:
        gen = CONFORMANCE_FAMILIES[name]
        for label, verbs, layers in vocabularies:
            cells.extend(gen(domain_verbs=verbs, profile_layers=layers, profile_label=label))
    return cells


def run_conformance(**kwargs) -> Dict[str, object]:
    results = [check_cell(c) for c in enumerate_cells(**kwargs)]
    failures = [r for r in results if not r.ok]
    return {
        "verdict": "PASS" if not failures else "FAIL",
        "cells": len(results),
        "passed": len(results) - len(failures),
        "failed": len(failures),
        "exempt": sum(1 for r in results if r.cell.exemption is not None),
        "families": sorted({r.cell.family for r in results}),
        "failures": [{"cell": r.cell.id, "enumerated_from": r.cell.enumerated_from,
                      "detail": r.detail} for r in failures],
    }


def warrant(profiles: Optional[Iterable[Tuple[str, FrozenSet[str], Optional[Dict[str, str]]]]] = None,
            families: Optional[Iterable[str]] = None) -> Dict[str, object]:
    """The CONFORMANCE WARRANT — a guarantee with an enumerated scope.

    ⭐ THE TWO WORDS THAT SHAPE THIS FUNCTION.

    WARRANT: the verdict is a guarantee, not a report. If a construct is in the known
    universe and does not produce its specified graph output, that is a FAILURE — there is
    no "measures recall against the known list only" hedge here, because the hedge is true
    of robustness and IRRELEVANT to conformance. Well-formed input over an enumerated
    construct set has no unknown remainder to disclaim.

    THE KNOWN UNIVERSE: the guarantee states its own extent, in numbers a reader can check
    and a successor can watch move. "We validate the core language" is the old caveat
    wearing a confident voice; `core constructs: N of N` is not.

    ⛔ THE INPUT IS THE DECLARED SET, ENTIRE. THE WARRANT IS USAGE-BLIND. Cells are
    generated from the profile and the parser's own tables, and from NOTHING ELSE. There is
    no occurrence count anywhere in this function's input path, and deliberately no
    `declared_but_unused` / `used_but_unregistered` list to consult — any such list is an
    intermediate snapshot that can go stale, drift, or arrive incomplete, which is the same
    failure shape as the two hand-copied verbs that caused the defect this module closes.
    THE PROFILE IS THE ENUMERATION.

    A verb used once in a million documents therefore carries EXACTLY the obligation of one
    used everywhere. If the warrant covered only what a corpus already used, a newly
    registered extension would stay unvalidated until the first author reached for it, and
    then fail SILENTLY — which is precisely how `uses:` and `upholds:` reached 2026-08-13
    with 31 authored references and zero edges.

    Frequency is a VOCABULARY GOVERNANCE signal, not a coverage one: a declared verb nobody
    writes is evidence the vocabulary is over-broad and should be NARROWED. ⭐ Frequency can
    shrink the VOCABULARY; it can never shrink the WARRANT. Once declared: tested — until
    undeclared. Undeclared vocabulary is outside the warrant BY DEFINITION (the profile
    answers by omission), and is a corpus-hygiene finding, never a parser verdict.

    ⛔ AN EXEMPTION WITHOUT A STATED REASON FAILS, or exemptions become the new sampling.
    """
    cells = enumerate_cells(profiles=profiles, families=families)
    results = [check_cell(c) for c in cells]
    failures = [r for r in results if not r.ok]
    core_cells = [r for r in results if r.cell.enumerated_from.startswith("parser.")]
    ext_cells = [r for r in results if r.cell.enumerated_from.startswith("profile:")]
    profile_labels = sorted({r.cell.enumerated_from for r in ext_cells})
    exemptions = {r.cell.construct: r.cell.exemption for r in results if r.cell.exemption}
    unreasoned = [v for v, reason in exemptions.items() if not (reason or "").strip()]
    verdict = "WARRANTED" if not failures and not unreasoned else "FAILED"
    return {
        "scope": "CONFORMANCE — well-formed input over the enumerated known universe",
        "verdict": verdict,
        "warrant": (
            "Every core language element and every REGISTERED corpus extension listed "
            "below produces its specified graph output over well-formed input. This is a "
            "guarantee over a stated extent, not a sample. It makes NO claim about "
            "malformed input — that is the separate RECALL axis, which keeps its own "
            "open-ended limit."),
        "core_constructs": f"{len(core_cells) - len([r for r in failures if r in core_cells])} of {len(core_cells)}",
        "registered_extensions": f"{len(ext_cells) - len([r for r in failures if r in ext_cells])} of {len(ext_cells)} across {len(profile_labels)} profile vocabularies",
        "profiles": profile_labels,
        "families": sorted({c.family for c in cells}),
        "exemptions": [{"construct": v, "reason": r} for v, r in sorted(exemptions.items())],
        "exemptions_without_a_reason": unreasoned,
        "cells": len(results),
        "failed": len(failures),
        "failures": [{"cell": r.cell.id, "enumerated_from": r.cell.enumerated_from,
                      "detail": r.detail} for r in failures],
        "usage_blind": (
            "Cells are generated from the declared set entire. No occurrence count appears "
            "anywhere in this verdict's input path; frequency is a vocabulary-narrowing "
            "signal reported under governance, never an input to the warrant."),
        "out_of_scope": (
            "Non-standards-compliant `.lts` in live corpora is CORPUS HYGIENE, not parser "
            "correctness, and is measured by neither this warrant nor the recall suite. "
            "Vocabulary that is not declared is outside the warrant by definition."),
    }
