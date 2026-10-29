"""ONE constructed configuration value for the parser — Era 1, C3 (review V5/V5b/V5c).

WHY THIS MODULE EXISTS
----------------------
The parser accepts five vocabulary hooks. Before this module, each production entry
point threaded a DIFFERENT SUBSET of them, by hand, at the call site — and the subsets
were not merely incomplete, they were **disjoint**:

  * `property_edges=`  reachable from ZERO production call sites (V5)
  * `domain_verbs=`    reachable from `factlattice.corpus.parse()` only
  * `profile_layers=`  reachable from `factlattice_engine.config.build_*` only  (V5b)
  * the corpus's own REGISTERED profile (`corpus-config.json`'s `profile:` key)
    read by the AUDIT surfaces and by NO graph-building path at all              (V5c)

⇒ the SAME `(document, profile)` pair produced a DIFFERENT graph depending on which
entry point served it. `lts_lint`'s docstring claim — "lints clean == ingests clean,
one parser, no drift" — was measurably false for exactly this reason.

THE INVARIANT THIS ESTABLISHES, and it is the whole point:

    **A parse result is a function of `(bytes, ParserConfig)` and NOTHING ELSE.**
    The entry point is not an input.

That is a falsifiable statement, and
`factlattice_engine.selfcheck.entry_point_equivalence` is the falsifier: it parses one
control pair through every production path and diffs the graphs. Any future call site
that hand-rolls a subset again re-breaks that case.

⛔ WHY A FROZEN DATACLASS AND NOT A DICT OR kwargs. Loose kwargs are what produced V5:
adding a hook to `parse_text` did not add it to `build_index`, and NOTHING FAILED —
the omission is invisible at every call site because a missing keyword argument is
just a default. A single constructed value makes the omission impossible to express:
there is one object, it either carries the vocabulary or it does not, and `from_profile`
is the ONE place it is built from profile data.

⛔ THE TWO PROFILE SYSTEMS MERGE HERE AND ONLY HERE. `factlattice.profile.Profile`
(registered_ns / domain_verbs / property_edges / declared_property_keys) and
`factlattice.domain_profiles.ProfileEdgeConfig` (edge_layers / namespaces) are separate
readers of separate serializations that each project a PARTIAL view of the same
vocabulary. `ParserConfig.from_profile(profile, edge_config)` is the single seam. Merging
them at any other site would reintroduce exactly the divergence this module closes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple

from .parser import DEFAULT_DOMAIN_VERBS, DEFAULT_REGISTERED_NS

# An immutable empty mapping — a frozen dataclass whose default is a plain `{}` would
# be a shared mutable, and a config that can be mutated after construction is not a
# config, it is a global.
_EMPTY_MAP: Mapping[str, Any] = MappingProxyType({})

# --- format-version modes (A1 / C10) ---------------------------------------
MODE_LEGACY = "legacy"
MODE_STRICT = "strict"


@dataclass(frozen=True)
class ParserConfig:
    """The complete vocabulary a parse is performed against.

    Fields map 1:1 onto the parser's hooks:

    - `registered_ns` — edge-type namespaces that are NOT flagged `unregistered_ns` (D6).
    - `profile_layers` — `{namespaced_type -> layer}`, the domain profile's D5 table.
    - `domain_verbs` — BARE relationship verbs a profile registered; without these a
      `[A] UPHOLDS [B]` triple is flagged `unregistered_verb`.
    - `property_edges` — `{key -> (edge_type, layer, reversed)}`, the profile's
      registered edge-bearing property keys (SPEC §1.3.1, the maintainer's ruling 2026-08-15).
    - `declared_property_keys` — the profile's NON-minting declared descriptive keys
      (SPEC §1.3.3 / amendment A2). Parse-time input because the `undeclared_key`
      diagnostic is emitted by the parser, at the line, where the author can see it.

    `strict_default` is the mode applied to a document that carries NO `lts:`
    frontmatter field. It is `legacy` and a caller should essentially never change it;
    it exists so a conformance test can drive the strict path over an unstamped fixture
    without editing the fixture (SPEC §1.5, amendment A1).
    """

    registered_ns: FrozenSet[str] = DEFAULT_REGISTERED_NS
    profile_layers: Mapping[str, str] = _EMPTY_MAP
    domain_verbs: FrozenSet[str] = DEFAULT_DOMAIN_VERBS
    property_edges: Mapping[str, Tuple[str, str, bool]] = _EMPTY_MAP
    declared_property_keys: FrozenSet[str] = frozenset()
    strict_default: bool = False

    # --- construction ------------------------------------------------------
    @classmethod
    def from_profile(cls, profile: Optional[object] = None,
                     edge_config: Optional[object] = None,
                     registered_ns: Optional[object] = None,
                     strict_default: bool = False) -> "ParserConfig":
        """THE ONE SEAM where the two profile systems become one parser vocabulary.

        `profile` is a `factlattice.profile.Profile` (or None).
        `edge_config` is a `factlattice.domain_profiles.ProfileEdgeConfig` (or None).
        `registered_ns` overrides the union (advanced; `corpus.parse(registered_ns=)`).

        Either argument may be None — a deployment with no profiles yields a config
        byte-identical in effect to the pre-profile parser, which is what keeps the
        no-profile regression bar at zero.

        ⛔ LAYER DISAGREEMENT BETWEEN THE TWO SYSTEMS IS A REFUSAL, not a precedence
        rule, and it is the SAME refusal SPEC §1.3.2 constraint 2 already states for
        two profiles governing one root. Silently preferring one source would make a
        type's layer depend on which reader saw it first — the precise failure
        `domain_profiles.load_profile_edge_config` already refuses one level down.
        """
        from .profile import ProfileCollisionError

        ns: set = set(DEFAULT_REGISTERED_NS)
        layers: Dict[str, str] = {}
        verbs: set = set()
        prop_edges: Dict[str, Tuple[str, str, bool]] = {}
        declared_keys: set = set()

        # -- the ProfileEdgeConfig side (D5 layer table + D6 namespaces) -----
        if edge_config is not None:
            ns |= set(getattr(edge_config, "registered_ns", ()) or ())
            for etype, layer in (getattr(edge_config, "edge_layers", None) or {}).items():
                layers[etype] = layer

        # -- the Profile side (verbs, minting keys, declared keys, namespaces)
        if profile is not None:
            ns |= set(getattr(profile, "registered_ns", ()) or ())
            verbs |= set(getattr(profile, "domain_verbs", ()) or ())
            prop_edges.update(getattr(profile, "property_edges", None) or {})
            declared_keys |= set(getattr(profile, "declared_property_keys", ()) or ())
            # A `Profile` carries no `edge_layers` attribute of its own — THAT is the
            # V5b gap. Its `.lts`/YAML source DOES declare per-type layers, so they are
            # projected here rather than being unreachable from this side forever.
            for etype, layer in (_profile_edge_layers(profile) or {}).items():
                prior = layers.get(etype)
                if prior is not None and prior != layer:
                    raise ProfileCollisionError(
                        f"edge type '{etype}' is declared at layer '{prior}' by the "
                        f"engine's merged domain-profile config and at layer "
                        f"'{layer}' by the corpus's registered profile "
                        f"({getattr(profile, 'path', None)!r}). Refusing to build a "
                        f"parser config whose meaning depends on which profile reader "
                        f"ran first — every layer-scoped traversal would silently "
                        f"return a subset. (SPEC §1.3.2 constraint 2.)")
                layers[etype] = layer

        if registered_ns is not None:
            ns = set(registered_ns)

        return cls(
            registered_ns=frozenset(ns),
            profile_layers=MappingProxyType(dict(layers)),
            domain_verbs=frozenset(verbs),
            property_edges=MappingProxyType(dict(prop_edges)),
            declared_property_keys=frozenset(declared_keys),
            strict_default=strict_default,
        )

    # --- kwargs bridge (the deprecation path, engineering-spec §7 "API") ----
    @classmethod
    def from_kwargs(cls, registered_ns=None, profile_layers=None, domain_verbs=None,
                    property_edges=None, declared_property_keys=None,
                    strict_default: bool = False) -> "ParserConfig":
        """Build a config from the legacy loose keyword arguments.

        Kept so every pre-C3 caller (including third-party ones — this is a PUBLIC
        repo) keeps working through one deprecation release. `None` means "not
        supplied" and takes the field default; this is what makes the bridge lossless
        rather than a silent reset to core-only.
        """
        return cls(
            registered_ns=(DEFAULT_REGISTERED_NS if registered_ns is None
                           else frozenset(registered_ns)),
            profile_layers=MappingProxyType(dict(profile_layers or {})),
            domain_verbs=(DEFAULT_DOMAIN_VERBS if domain_verbs is None
                          else frozenset(domain_verbs)),
            property_edges=MappingProxyType(dict(property_edges or {})),
            declared_property_keys=frozenset(declared_property_keys or ()),
            strict_default=strict_default,
        )

    def merge(self, other: "ParserConfig") -> "ParserConfig":
        """Union two configs. Layer disagreement refuses, per §1.3.2 constraint 2."""
        from .profile import ProfileCollisionError

        layers = dict(self.profile_layers)
        for etype, layer in other.profile_layers.items():
            prior = layers.get(etype)
            if prior is not None and prior != layer:
                raise ProfileCollisionError(
                    f"edge type '{etype}' declared at both layer '{prior}' and "
                    f"'{layer}' by two merged parser configs")
            layers[etype] = layer
        return ParserConfig(
            registered_ns=self.registered_ns | other.registered_ns,
            profile_layers=MappingProxyType(layers),
            domain_verbs=self.domain_verbs | other.domain_verbs,
            property_edges=MappingProxyType({**self.property_edges,
                                             **other.property_edges}),
            declared_property_keys=(self.declared_property_keys
                                    | other.declared_property_keys),
            strict_default=self.strict_default or other.strict_default,
        )

    # --- reporting ---------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        """A JSON-safe summary — what an audit surface should report as THREADED.

        Deliberately named `describe`, not `to_dict`: this is the value an instrument
        shows an operator, and per V6 the honest question is not "what was declared"
        but "what is in effect on the parse that actually ran".
        """
        return {
            "registered_ns": sorted(self.registered_ns),
            "profile_layers": dict(sorted(self.profile_layers.items())),
            "domain_verbs": sorted(self.domain_verbs),
            "property_edges": {k: list(v) for k, v in
                               sorted(self.property_edges.items())},
            "declared_property_keys_count": len(self.declared_property_keys),
            "strict_default": self.strict_default,
        }


#: The vocabulary a parse gets when no profile is supplied at all. Byte-identical in
#: effect to the pre-C3 defaults of `parse_file`/`parse_text`.
CORE_ONLY = ParserConfig()


def _profile_edge_layers(profile: object) -> Dict[str, str]:
    """Project a `Profile`'s own edge-type declarations onto the D5 layer table.

    Reads the profile's ALREADY-PARSED `raw` dict (`edge_types[].layer`) rather than
    re-reading the file, so the layer a config threads is by construction the layer the
    profile loader validated. A namespaced type with a declared canonical layer
    contributes; a bare type does not (bare types resolve through `CORE_EDGE_LAYER` and
    are governed by §1.3.2 constraint 1, which refuses a bare core redefinition at
    profile load — so there is nothing here to override).
    """
    from .parser import CANONICAL_LAYERS

    raw = getattr(profile, "raw", None)
    if not isinstance(raw, dict):
        return {}
    recs = raw.get("edge_types")
    if not isinstance(recs, list):
        return {}
    out: Dict[str, str] = {}
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        etype = rec.get("type") or rec.get("edge-type")
        layer = rec.get("layer")
        if not etype or not layer:
            continue
        etype, layer = str(etype).strip(), str(layer).strip()
        if "." not in etype:
            continue          # bare: CORE_EDGE_LAYER governs, see the docstring
        if layer not in CANONICAL_LAYERS:
            continue          # non-canonical rows are the profile loader's ambiguity
        out[etype] = layer
    return out


__all__ = ["ParserConfig", "CORE_ONLY", "MODE_LEGACY", "MODE_STRICT"]
