"""Reader for `.lts` domain-profile edge-type + namespace declarations (D5/D6).

WHY THIS EXISTS
---------------
`parser._layer_for_type` used to resolve EVERY edge type via `CORE_EDGE_LAYER` on
its bare suffix, so a namespaced `dec.DEPENDS_ON` came out `semantic` regardless of
what the domain profile declared — and `parser._type_flags` treated every non-`core`
namespace as unregistered. Meanwhile the agentic profiles ALREADY declare, per edge
type, its fully-namespaced type, its namespace, and its layer. This module reads those
declarations so the engine's partition-build path can:

  * resolve `dec.DEPENDS_ON -> causal`, `art.IN_SESSION -> temporal`, ... from the
    profile (D5 — per-domain layer table), and
  * register the profile's namespaces (`dec`, `art`, ...) so those edges are no longer
    flagged `unregistered_ns` (D6 — namespace registration).

The parser core stays generic: it consumes exactly two hooks — `registered_ns`
(already threaded) and a new `{namespaced_type -> layer}` map — both projected here.

FORMAT PARSED (as the profiles already author it, unchanged)
------------------------------------------------------------
Each profile's §6 "Domain-Specific Edge Types" section carries markdown tables of the
shape `| Type | Layer | Description |`, e.g.::

    | dec.SUPERSEDES | temporal | This decision replaces an earlier one ... |
    | art.IN_SESSION | temporal | This artifact was produced in a specific session |

A declaration row is recognised by its SHAPE, not by section-heading numbering:

  * first cell is a fully-namespaced edge type `<ns>.<TYPE>` where `<ns>` is a
    lowercase namespace token and `<TYPE>` is an UPPERCASE_SNAKE edge type;
  * second cell is one of the four canonical layers (semantic/temporal/causal/entity).

Header rows (`| Type | Layer | ... |`) and separator rows (`|------|`) never match
(their first cell is not `<ns>.<TYPE>`), and neither do the §5 property tables
(first cell like `lifecycle-position`, no `.<UPPER>`) or the §7 doc-type tables
(first cell like `DECISION-GRAPH`, no lowercase `<ns>.` prefix). Shape-matching is
therefore section-independent AND self-validating.

The DECLARED NAMESPACE for registration is taken from the lowercase prefix of these
edge types (`dec`, `art`, ...) — this is deliberately the SAME token the parser's
`_type_flags` derives from an edge type at parse time, so registration and flagging
can never disagree. (See `KNOWN_FORMAT_NOTES` for why §1's "Namespace | DEC" display
value and §4's node-ID prefix registry are NOT used for this.)

Any row that LOOKS like an edge declaration (first cell `<ns>.<TYPE>`) but whose layer
cell is not canonical is recorded in `.ambiguities` (best-effort structured parse; never
fails the load over one messy row) — a follow-up for the D1 profile-format firm-up.

⛔ CHANGED 2026-08-15 — A CROSS-PROFILE LAYER DISAGREEMENT IS NOW A REFUSAL, NOT AN
AMBIGUITY. It used to keep the FIRST declaration (deterministic by sorted path) and
record a `conflicting_layer` note. That is first-wins with a warning attached, and this
is the LIVE engine build path: `config.build_partition` / `build_merged_partition` feed
`edge_layers` straight into `parser._layer_for_type` for every corpus in the deployment.
The consequence of the old behaviour was that one type meant two things, the winner was
decided by filename sort order, and every `lts_traverse(layers=[...])` over the losing
profile's corpus silently returned a subset — with `lts_lint` reporting CLEAN, because a
note in `.ambiguities` is not a finding and nothing reads it.

This is the same rule `profile.merge_edge_types` applies to `edge_types[]` declarations,
and the same rule `profile.merge_property_edges` already applied to property keys.
Agreement across profiles merges; disagreement refuses and names both profiles.

MEASURED BEFORE THE CHANGE, so this is an addition and not an outage: the six shipped
`domain-profiles/*.lts` declare 72 edge types across six namespaces with ZERO conflicts
and ZERO ambiguities. The guard is being installed while the corpus is clean, which is
the only time a guard can be installed without also being a migration.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Tuple

from .parser import CANONICAL_LAYERS

# A markdown table row whose FIRST cell is a fully-namespaced edge type. The trailing
# `.*` after the second cell tolerates the description column (and a missing final `|`).
_EDGE_ROW_RE = re.compile(
    r"^\|\s*([a-z][a-z0-9_]*\.[A-Z][A-Z0-9_]*)\s*\|\s*([^|]*?)\s*\|"
)

# Notes on aspects of the profile format that are deliberately NOT used as the
# authoritative source for edge-namespace registration (surfaced for the D1 firm-up).
KNOWN_FORMAT_NOTES: Tuple[str, ...] = (
    "§1 'Domain Identity' declares `| Namespace | DEC |` in UPPERCASE, but edge types "
    "are authored lowercase (`dec.SUPERSEDES`); the lowercase edge-type prefix is used "
    "for registration so it matches the parser's own `_type_flags` derivation exactly.",
    "§4 'ID Prefix Registry' lists NODE-ID prefixes (DEC-, DD-, FIX-), which are a "
    "different concept from edge-type namespaces and are intentionally not read here.",
)


@dataclass
class ProfileEdgeConfig:
    """Merged, engine-ready projection of every domain profile's edge declarations.

    - `edge_layers`: global `{namespaced_type -> layer}` map consumed by
      `parser._layer_for_type`. Namespaced types are globally unique, so no
      per-corpus profile resolution is needed (this sidesteps the not-yet-built
      `conforms-to` mechanism, by design).
    - `namespaces`: the set of declared edge namespaces (`dec`, `art`, ...).
    - `registered_ns`: `{'core'} | namespaces` — the `registered_ns=` the parser wants.
    - `ambiguities`: structured records of rows/types that could not be parsed
      cleanly (non-canonical layer, or a type declared with conflicting layers).
    - `sources`: profile file paths that contributed at least one declaration.
    """

    edge_layers: Dict[str, str] = field(default_factory=dict)
    namespaces: FrozenSet[str] = frozenset()
    ambiguities: List[dict] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)

    @property
    def registered_ns(self) -> FrozenSet[str]:
        return frozenset({"core"}) | set(self.namespaces)


def read_profile_edges(path: str) -> Tuple[Dict[str, str], List[dict]]:
    """Read ONE `.lts` profile's edge-type declarations.

    Returns `({namespaced_type -> layer}, ambiguities)`. `ambiguities` holds rows that
    matched the edge-row SHAPE but carried a non-canonical layer (best-effort; the row
    is skipped, never guessed, and never crashes the load).
    """
    edges: Dict[str, str] = {}
    ambiguities: List[dict] = []
    from .hashing import safe_read_text
    for lineno, line in enumerate(safe_read_text(path).split("\n"), start=1):
        m = _EDGE_ROW_RE.match(line.rstrip("\n"))
        if not m:
            continue
        etype, layer = m.group(1), m.group(2).strip()
        if layer not in CANONICAL_LAYERS:
            ambiguities.append({
                "kind": "non_canonical_layer",
                "profile": path,
                "line": lineno,
                "type": etype,
                "layer_cell": layer,
            })
            continue
        edges[etype] = layer
    return edges, ambiguities


def discover_profiles(profile_dir: str) -> List[str]:
    """All `.lts` domain-profile files in `profile_dir`, sorted for determinism."""
    if not profile_dir or not os.path.isdir(profile_dir):
        return []
    return sorted(glob.glob(os.path.join(profile_dir, "*.lts")))


def load_profile_edge_config(profile_dir: str) -> ProfileEdgeConfig:
    """Build ONE merged `ProfileEdgeConfig` from every `.lts` profile in `profile_dir`.

    Missing/empty dir -> an EMPTY config (`registered_ns == {'core'}`, no edge layers),
    so the engine behaves exactly as before the profile change on a deployment with no
    profiles.

    ⛔ RAISES `ProfileCollisionError` when two profiles declare the SAME namespaced edge
    type at DIFFERENT layers (see the module docstring for why this replaced first-wins).
    Identical re-declaration across profiles is fine and merges silently — that is the
    ordinary case for a shared corpus root, and it is the same call
    `profile.merge_edge_types` makes.
    """
    from .profile import ProfileCollisionError  # local: keeps the import graph acyclic

    edge_layers: Dict[str, str] = {}
    ambiguities: List[dict] = []
    sources: List[str] = []
    namespaces: set = set()
    owner: Dict[str, str] = {}

    for path in discover_profiles(profile_dir):
        edges, amb = read_profile_edges(path)
        ambiguities.extend(amb)
        if edges:
            sources.append(path)
        for etype, layer in edges.items():
            ns = etype.split(".")[0]
            namespaces.add(ns)
            prior = edge_layers.get(etype)
            if prior is not None and prior != layer:
                raise ProfileCollisionError(
                    f"profiles '{owner.get(etype)}' and '{path}' both declare edge type "
                    f"'{etype}' but disagree about its layer: '{prior}' vs '{layer}'. "
                    f"Refusing to build an edge-layer table whose meaning depends on "
                    f"profile filename sort order — every layer-scoped traversal over "
                    f"the losing profile's corpus would silently return a subset.")
            edge_layers[etype] = layer
            owner.setdefault(etype, path)
    return ProfileEdgeConfig(
        edge_layers=edge_layers,
        namespaces=frozenset(namespaces),
        ambiguities=ambiguities,
        sources=sources,
    )


def default_profile_dir() -> str:
    """The domain-profiles directory shipped alongside the packages.

    Env `FACTLATTICE_PROFILE_DIR` overrides. Default is `<repo_root>/domain-profiles`
    (the `factlattice` package's parent), which holds on both the dev checkout and the
    deployment. A path that does not exist degrades to an empty config (inert).
    """
    env = os.environ.get("FACTLATTICE_PROFILE_DIR")
    if env:
        return env
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(pkg_parent, "domain-profiles")


__all__ = [
    "ProfileEdgeConfig",
    "read_profile_edges",
    "discover_profiles",
    "load_profile_edge_config",
    "default_profile_dir",
    "KNOWN_FORMAT_NOTES",
]
