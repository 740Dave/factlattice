"""[FL-VERB-PARITY] THE ENUMERATION GAP — a GOVERNANCE report, never an input.

⛔ THIS MODULE IS DELIBERATELY NOT IMPORTED BY `factlattice.conformance`. The direction of
that dependency is the whole point: the warrant may never consult a usage measurement, and
the cheapest way to guarantee that is for the usage measurement to live somewhere the
warrant does not import. `tests/test_verb_surface_parity.py` asserts the import graph.

WHAT IT MEASURES. The DECLARED set (from the parser's tables and a profile) against what
the RECALL control corpus happens to exercise. Both sides derived, neither sampled.

ITS TWO LEGITIMATE USES.
  1. watching the control corpus's conformance shortfall shrink toward zero;
  2. VOCABULARY GOVERNANCE — a declared verb nobody writes is evidence the vocabulary is
     OVER-BROAD and should be NARROWED.

⭐ Frequency can shrink the VOCABULARY. It can never shrink the WARRANT. Once declared:
tested — until undeclared. A number from this module that were ever allowed to decide what
gets tested would re-create the exact defect [FL-VERB-PARITY] exists to close: two verbs
picked out of a table the engine enumerates, both working, and the suite green forever.

⛔ AND THE COMPLEMENT IS NOT A LIST. Vocabulary that is not declared is outside the warrant
BY DEFINITION — the profile answers by omission. There is no `used_but_unregistered` set
here for the same reason there is no `declared_but_unused` set: an intermediate enumeration
is a snapshot, and a snapshot goes stale. THE PROFILE IS THE ENUMERATION.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import FrozenSet, List, Optional

from .conformance import POSITIONS, relationship_verb_cells
from .parser import CORE_EDGE_LAYER, rel_property_edge_map


@dataclass
class FamilyCensus:
    family: str
    enumerated_from: str
    defined: int
    exercised_by_control: int
    cells_defined: int
    cells_generated: int
    note: str = ""
    missing: List[str] = field(default_factory=list)

    @property
    def gap(self) -> int:
        return self.defined - self.exercised_by_control


def control_corpus_text() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "selfcheck", "control.lts"), encoding="utf-8") as fh:
        return fh.read()


def verb_census(domain_verbs: FrozenSet[str] = frozenset(),
                control_text: Optional[str] = None) -> FamilyCensus:
    """How many relationship verbs exist, how many the control corpus touches, the gap."""
    text = control_corpus_text() if control_text is None else control_text
    key_map = rel_property_edge_map(domain_verbs)
    verbs = sorted(set(CORE_EDGE_LAYER) | set(domain_verbs))
    spellings = {v: sorted(k for k, m in key_map.items() if m[0] == v) for v in verbs}
    exercised, missing = [], []
    for v in verbs:
        hit = (f" {v} " in text) or any(f"- {k}:" in text for k in spellings.get(v, ()))
        (exercised if hit else missing).append(v)
    cells = len(list(relationship_verb_cells(domain_verbs)))
    return FamilyCensus(
        family="relationship-verb",
        enumerated_from="parser.CORE_EDGE_LAYER + profile relationship_verbs",
        defined=len(verbs),
        exercised_by_control=len(exercised),
        cells_defined=len(verbs) * len(POSITIONS),
        cells_generated=cells,
        note="a verb is 'exercised' if the control corpus contains it in ANY position",
        missing=missing,
    )
