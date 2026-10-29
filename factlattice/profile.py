"""Domain-profile loading (contract §5 profile hook).

The copied parser core (`parser.py` / `index_builder.py`) is generic: the ONLY
domain-specific knob it accepts is `registered_ns: frozenset` — the set of edge-type
namespaces treated as registered (an unregistered namespace on an edge is preserved
and FLAGGED, never dropped; see parser `_type_flags`). Everything else the core does
is format-level.

Per FactLattice SPEC.md P4/§10 and the library contract §5, the parser must LOAD a
domain profile rather than hardcode any domain. This module is that loader.

------------------------------------------------------------------------------
TWO SERIALIZATIONS, ONE INTERFACE  ([ACME-NARROW-WAIST] applied to the profile)
------------------------------------------------------------------------------
A profile may be authored as `NN-<name>-profile.yaml` (the legacy shape, and the
shape deployed corpora are live in today) or as `<domain>-profile.lts` (the
self-hosting shape `[PROFILE-SELFHOST]` calls normative: "a domain profile is a
`.lts` document parsed by the parser it configures"). BOTH backends produce the
SAME dict, and every consumer reads that dict through the TYPED ACCESSORS on
`Profile` — `prefixes`, `document_tags`, `enum_values`, `required_properties`,
`recommended_properties`, `lifecycle_stages`, `guidance_notes`.

⛔ Consumers MUST NOT reach into `Profile.raw[...]`. `raw` is retained for backward
compatibility only. A validator coupled to the YAML dict shape
(`raw["prefixes"][i]["prefix"]`) is coupled to a serialization we have already
decided to replace, and needs rewriting the day a corpus ships a `.lts` profile.
The accessors are the contract; the dict is an implementation detail of the two
backends, and their equivalence is asserted by fixture, not by assurance.

------------------------------------------------------------------------------
THE `.lts` PROFILE ENCODING  — architect's design call, RATIFIABLE, NOT SETTLED
------------------------------------------------------------------------------
This encoding is a design call made by the UI Architect to unblock
`[PROFILE-SELFHOST]`. It is offered for ratification; it is NOT a settled part of
SPEC §10 and may be amended before it is. Nothing outside this module depends on
its details — a ratified change lands here and nowhere else.

ONE NODE PER REGISTERED ITEM. The node-id PREFIX carries the KIND; the properties
carry the payload. No new syntax is introduced: every construct below is a plain
h2 node with plain `- key: value` (P1) / `- key:` + indented items (P4) properties.

    ## [PROFILE-DOMAIN] Acme Analytics — Data Science
    - domain-id: acme.ds
    - namespaces: acme.ds, acme.eng      # P1 inline -> comma-split
    - domain-name: ...  - domain-version: ...  - conventions-source: ...

    ## [PREFIX-DEC] Decision nodes
    - registers-prefix: DEC-
    - document: decision-graph-analyst
    - definition: Load-bearing DS decisions

    ## [DOCTAG-AMEND] Governance amendment
    - document-tag: AMEND
    - definition: ...

    ## [EDGETYPE-GOVERNS] A constrains the frame for B
    - edge-type: GOVERNS                 # BARE  -> a relationship VERB
    - tier: domain                       #          (relationship_verbs.<tier>)
    - definition: ...
    ## [EDGETYPE-CONTRAINDICATES] ...
    - edge-type: clinical.CONTRAINDICATES  # NAMESPACED -> an edge_types[] entry
    - layer: semantic                      #   (namespaced types need no verb
    - definition: ...                      #    registration; the parser accepts them)

    ## [ENUM-CONFIDENTIALITY] ...
    - enum-key: confidentiality
    - values:                            # P4 nested -> items taken VERBATIM
      - public
      - nda-shareable
    - extended-values:                   # optional; yields the {core, extended} shape
      - ...

    ## [NODEFAMILY-DECISION] ...
    - section: decision_nodes
    - families: DEC-, MUT-
    - required:            (P4 list)
    - recommended:         (P4 list)

    ## [LIFECYCLE-DECISION] ...
    - section: decision_lifecycle        # optional, defaults to decision_lifecycle
    - pipeline:            (P4 list)
    - off-pipeline:        (P4 list)
    - decision-prefixes: DEC, MUT
    - session-key-property: decided-in-session

    ## [GUIDANCE-CONTINUATION] ...
    - note: <one physical line>

LIST VALUES — the one rule worth knowing: a P1 inline value (`- families: DEC-, MUT-`)
is COMMA-SPLIT; a P4 nested block's items are taken VERBATIM (so a note or a stage
name may contain commas). The distinction is mechanical, not heuristic.

⛔ BOOTSTRAPPING INVARIANT (enforced by `_assert_core_only_parse`)
A `.lts` profile MUST be parseable using CORE notation only. It cannot use the
domain notations it registers — the profile would then be needed in order to parse
the profile. `_read_lts_profile` therefore parses with `DEFAULT_REGISTERED_NS` and
`DEFAULT_DOMAIN_VERBS` (never the profile's own), and REJECTS a profile whose parse
produced any `unregistered_ns` / `unregistered_verb` edge — that flag is the exact
signature of a profile reaching for its own vocabulary. Rejecting loudly beats
loading a profile whose own edges were silently mis-tiered.

------------------------------------------------------------------------------
Auto-discovery: `discover_profile(corpus_root)` matches `*-profile.lts` FIRST (the
normative form) then the legacy `[0-9][0-9]-*-profile.yaml`.

YAML: PyYAML is an OPTIONAL dependency. The core four files stay stdlib-only; only this
net-new loader needs YAML, and only when an actual `.yaml` profile is supplied. If a
`.yaml` profile is requested and PyYAML is absent, we raise a clear error rather than
silently ignore the profile (never silently drop — contract Prime Directive). A `.lts`
profile needs no optional dependency at all: it is read by the parser this repo ships.
"""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, NamedTuple, Optional, Sequence, Tuple

from . import registry
from .parser import DEFAULT_DOMAIN_VERBS, DEFAULT_REGISTERED_NS

# Glob for profile auto-discovery in a corpus root (contract §5 / alignment doc).
PROFILE_GLOB = "[0-9][0-9]-*-profile.yaml"
# `[PROFILE-SELFHOST]`: `<domain>-profile.lts` in the corpus root. Deliberately NOT
# `*.lts` — that would match every document in the corpus.
PROFILE_GLOB_LTS = "*-profile.lts"

# Top-level profile keys with a KNOWN structural meaning. `enum_values` refuses to
# reinterpret one of these as an enum, so `enum_values('prefixes')` is None rather
# than a set of garbage harvested out of a record list.
_STRUCTURAL_KEYS: FrozenSet[str] = frozenset({
    "domain", "prefixes", "document_tags", "relationship_verbs", "edge_types",
    "properties", "decision_nodes", "mutation_nodes", "decision_lifecycle",
    "linter_guidance", "traversal", "roles", "frontmatter_extensions",
    "relationship_notation", "change_tracking",
    "property_keys",          # SPEC §1.3.3 / amendment A2 — the declared-key tier
    "review_extensions",      # SPEC §10.4 (A-E5) — REVEXTREG registrations
})


class PrefixSpec(NamedTuple):
    """One registered node-id prefix (SPEC §10.1 `prefixes[]`).

    `note` absorbs whichever descriptive key the serialization used — Acme authors
    `note`, SPEC §10.1's examples author `node_type`, and `description` appears in
    the wild. One field, so a validator never branches on serialization.
    """

    prefix: str
    document: Optional[str] = None
    note: Optional[str] = None


class ReviewExtensionReg(NamedTuple):
    """One registered review extension (SPEC §10.4; `.lts` node kind `REVEXTREG-<id>`, or a
    `review_extensions[]` record in YAML/JSON): `path` to the extension document and the `ext_id` of the
    extension defined in it. Both exactly as authored; `Profile.resolve_path` turns a path into a realpath."""

    path: str
    ext_id: str


class LifecycleStages(NamedTuple):
    """`decision_lifecycle` — the pipeline and the legitimate ways off it.

    Tuple-unpackable (`pipeline, off_pipeline = profile.lifecycle_stages`). The
    union is what V6 validates `lifecycle-position` against; the SPLIT is what
    answers "decisions made but never implemented".
    """

    pipeline: List[str]
    off_pipeline: List[str]

    @property
    def all_stages(self) -> FrozenSet[str]:
        return frozenset(self.pipeline) | frozenset(self.off_pipeline)


def _str_or_none(value: object) -> Optional[str]:
    """First scalar of a property value, as a stripped str; None if there is none.

    A repeated property key accumulates into a list (parser `_maybe_multivalue`),
    so a scalar read must tolerate a list without guessing which entry was meant —
    it takes the first, deterministically.
    """
    if isinstance(value, (list, tuple)):
        for item in value:
            got = _str_or_none(item)
            if got is not None:
                return got
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _record_field(rec: object, *keys: str) -> Optional[str]:
    """First present key of a mapping-shaped profile record, as a str."""
    if not isinstance(rec, dict):
        return None
    for key in keys:
        if key in rec and rec[key] is not None:
            return str(rec[key]).strip() or None
    return None


def _scalar_set(value: object) -> Optional[FrozenSet[str]]:
    """A list of scalars -> a set of strings. Anything else -> None.

    Returning None (not an empty set) for a non-list is what lets `enum_values`
    distinguish "not an enum" from "an enum with no members".
    """
    if not isinstance(value, (list, tuple)):
        return None
    out = set()
    for item in value:
        if isinstance(item, (dict, list, tuple)):
            return None
        text = _str_or_none(item)
        if text:
            out.add(text)
    return frozenset(out)


def _str_list(value: object) -> List[str]:
    """A profile list-valued field -> `list[str]`; anything unusable -> `[]`."""
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            text = _str_or_none(item)
            if text:
                out.append(text)
        return out
    text = _str_or_none(value)
    return [text] if text else []


@dataclass
class Profile:
    """A loaded domain profile.

    - `registered_ns` is the ONLY field the copied core consumes: it becomes the
      `registered_ns=` argument to `parse_file` / `build_index`. It is
      `{'core'} | <domain namespaces>` so bare core edge types stay valid and the
      domain's namespaces (e.g. `lib.eng`, `lib.ops`) are recognized.
    - `raw` is the full parsed profile dict. ⛔ RETAINED FOR BACKWARD COMPATIBILITY
      ONLY — new consumers read the TYPED ACCESSORS below, which are identical
      across the YAML and `.lts` backends. See the module docstring.
    - `path` is where it was loaded from (None for a synthetic/dict profile).

    Every accessor is TOTAL over absent sections: a missing section yields an empty
    collection, never a KeyError and never an exception. The one deliberate
    exception is `enum_values`, which returns None for an UNDECLARED key so a
    validator can tell "this domain declares no such enum" (skip the check) from
    "this domain declares it empty" (nothing is valid) — those are different rules.
    """

    registered_ns: FrozenSet[str]
    domain_verbs: FrozenSet[str] = DEFAULT_DOMAIN_VERBS
    domain_id: Optional[str] = None
    raw: Dict[str, object] = field(default_factory=dict)
    path: Optional[str] = None

    @property
    def property_edges(self) -> Dict[str, Tuple[str, str, bool]]:
        """The domain's registered edge-bearing property keys.

        `{property_key: (edge_type, layer, reversed)}`, projected from the profile's
        `edge_types[].property_key` declarations. Empty for a profile that registers
        none, so a corpus with no such declaration parses byte-identically to before
        The maintainer's 2026-08-15 ruling.

        ⛔ GUARANTEED DISJOINT from the built-in minting table: `load_profile` raises
        `ProfileCollisionError` rather than returning a map that overlaps it.
        """
        return _property_edges_from_raw(self.raw, self.path)

    @property
    def declared_property_keys(self) -> FrozenSet[str]:
        """The domain's declared NON-minting descriptive keys (SPEC §1.3.3 / A2).

        ⛔ THIS PROPERTY EXISTS BECAUSE ITS ABSENCE WOULD HAVE BEEN V5 AGAIN, INSIDE THE
        AMENDMENT THAT RATIFIES AGAINST V5. A2 defines three tiers — core,
        profile-declared, `x-`-marked. Without this projection the middle tier would be
        declarable in a profile and read by nothing: `ParserConfig.from_profile` would
        `getattr` it, get nothing, and every profile-declared key would still be
        reported `undeclared_key`. That is precisely §1.3.1 constraint 3's own words —
        *"inert while reading as functional"* — and shipping the regime without this
        would have meant ratifying a rule onto wiring that does not exist, which is the
        failure the whole Era-1 brief is organised around.

        Read from `property_keys: [...]` (YAML/JSON) or `PROPKEY-*` nodes (`.lts`).
        Empty for a profile that declares none, so nothing changes for a corpus that
        does not use the feature.
        """
        return _declared_property_keys_from_raw(self.raw, self.path)

    @property
    def edge_types(self) -> Dict[str, str]:
        """The domain's declared edge TYPES as `{edge_type: layer}`.

        Every `edge_types[]` record, whether or not it also registers a `property_key`
        — a namespaced type declared purely for its layer is still a registration, and
        `lts_profile_audit` cannot report a registered-but-unused edge type it was never
        shown.

        ⛔ GUARANTEED FREE OF BARE CORE NAMES and of names the parser cannot recognise:
        `load_profile` raises `ProfileCollisionError` rather than returning a map that
        shadows core or that could never mint. Cross-profile agreement is the caller's
        job — `merge_edge_types`.
        """
        return _edge_types_from_raw(self.raw, self.path)

    @property
    def review_extensions(self) -> List[ReviewExtensionReg]:
        """The review extensions this profile REGISTERS, in declaration order (SPEC §10.4, A-E5).

        Total: `[]` for a profile that registers none (his 62133 ruling: with nothing registered the generic
        review runs). A record with no `path` or no `ext_id` is SKIPPED here and surfaces in
        `validate_review_extensions` / `unprojected_review_extensions`, never as a half-registration.
        """
        out: List[ReviewExtensionReg] = []
        for item in self.raw.get("review_extensions") or []:
            path = _record_field(item, "path")
            ext = _record_field(item, "ext_id", "ext-id")
            if path and ext:
                out.append(ReviewExtensionReg(path=path, ext_id=ext))
        return out

    @property
    def review_output(self) -> Optional[str]:
        """The per-corpus review OUTPUT root, as authored (`review-output` on the `.lts` PROFILE node,
        `domain.review_output` in YAML/JSON), or None. Precedence against the site config lives in
        `factlattice_engine.reviewer.resolve_output`, not here."""
        dom = self.raw.get("domain")
        if isinstance(dom, dict):
            return _str_or_none(dom.get("review_output") or dom.get("review-output"))
        return None

    def resolve_path(self, value: str) -> Optional[str]:
        """`value` (a path authored in this profile) as a realpath: `~` expanded; a relative path is taken
        relative to the directory of the profile FILE, and is unresolvable (None) for a dict profile, which
        has no directory. Never touches the filesystem beyond `realpath`."""
        if not value:
            return None
        v = os.path.expanduser(str(value))
        if not os.path.isabs(v):
            if not self.path:
                return None
            v = os.path.join(os.path.dirname(self.path), v)
        return os.path.realpath(v)

    @property
    def namespaces(self) -> List[str]:
        dom = self.raw.get("domain") or {}
        if isinstance(dom, dict):
            ns = dom.get("namespaces") or []
            if isinstance(ns, list):
                return [str(x) for x in ns]
        return []

    # --- typed accessors (BUILD-profile-driven-validation §3) ---------------

    @property
    def prefixes(self) -> List[PrefixSpec]:
        """Registered node-id prefixes, in declaration order (V1's input).

        Accepts both the record form (`- {prefix: "DEC-", document: …, note: …}`)
        and a bare list of strings; a record with no `prefix` key is skipped rather
        than materialised as a `PrefixSpec(None)` that would silently register the
        empty prefix — i.e. match every id.
        """
        out: List[PrefixSpec] = []
        for item in self.raw.get("prefixes") or []:
            if isinstance(item, dict):
                pref = _record_field(item, "prefix")
                if not pref:
                    continue
                out.append(PrefixSpec(
                    prefix=pref,
                    document=_record_field(item, "document"),
                    note=_record_field(item, "note", "description", "node_type"),
                ))
            else:
                pref = _str_or_none(item)
                if pref:
                    out.append(PrefixSpec(prefix=pref))
        return out

    @property
    def prefix_set(self) -> FrozenSet[str]:
        """Just the prefix strings — the membership test V1 actually runs."""
        return frozenset(p.prefix for p in self.prefixes)

    @property
    def document_tags(self) -> FrozenSet[str]:
        """Doctypes this domain registers (V2's input; CORE tags are §1.7's, not a
        profile's, so this set is the DOMAIN half of that check and no more)."""
        out = set()
        for item in self.raw.get("document_tags") or []:
            tag = _record_field(item, "tag") if isinstance(item, dict) else _str_or_none(item)
            if tag:
                out.add(tag)
        return frozenset(out)

    def enum_values(self, key: str) -> Optional[FrozenSet[str]]:
        """Declared values of the enum `key`, or None if the domain declares no such
        enum. Handles both authored shapes: a flat list (`confidentiality`) and a
        tiered mapping of lists (`artifact_readiness: {core: […], extended: […]}`),
        which are unioned — a tier is an authoring convenience, not a sub-vocabulary.

        A STRUCTURAL key (`prefixes`, `domain`, …) is never reinterpreted as an enum.
        """
        if not key or key in _STRUCTURAL_KEYS:
            return None
        block = self.raw.get(key)
        if block is None:
            return None
        flat = _scalar_set(block)
        if flat is not None:
            return flat
        if isinstance(block, dict):
            out = set()
            for tier_values in block.values():
                tier = _scalar_set(tier_values)
                if tier is None:
                    return None  # not enum-shaped; do not guess
                out |= tier
            return frozenset(out)
        return None

    def _family_blocks(self, family_or_section: str) -> List[Dict[str, object]]:
        """Resolve a node-family SECTION name (`decision_nodes`) or a FAMILY prefix
        (`DEC-`, or `DEC`) to EVERY schema block that claims it. A family matches a
        section whose `families` list contains it; the trailing hyphen is optional on
        both sides so a caller holding a parsed node's `type_prefix` (`DEC`) resolves
        without re-deriving the profile's punctuation.

        ⛔ RETURNS ALL MATCHES, NOT THE FIRST. A family may legitimately appear in more
        than one block, and the requirements then UNION — a node must satisfy every
        schema that claims it.

        This previously returned the first match in dict order, which silently dropped
        requirements: Acme registers `MUT-` in BOTH `decision_nodes.families` and
        `mutation_nodes.families`, so `required_properties("MUT")` yielded only
        decision_nodes' {rationale, lifecycle-position} while mutation_nodes'
        {depends-on, written-before-execution, invalidates, executed} were NEVER
        CHECKED — including the two fields that ARE the write-before-mutate gate. V3
        under-reported and said nothing, which is the failure class this whole
        validation pass exists to close. Union can only ever check MORE; first-match
        loses requirements silently and depends on dict ordering, which nothing
        declares.
        """
        want = (family_or_section or "").strip()
        if not want:
            return []
        out: List[Dict[str, object]] = []
        block = self.raw.get(want)
        if isinstance(block, dict):
            out.append(block)
        want_bare = want.rstrip("-").upper()
        for value in self.raw.values():
            if not isinstance(value, dict) or value in out:
                continue
            for fam in _str_list(value.get("families")):
                if fam.rstrip("-").upper() == want_bare:
                    out.append(value)
                    break
        return out

    def _family_union(self, family_or_section: str, key: str) -> FrozenSet[str]:
        out: set = set()
        for block in self._family_blocks(family_or_section):
            out |= set(_str_list(block.get(key)))
        return frozenset(out)

    def required_properties(self, family_or_section: str) -> FrozenSet[str]:
        """Properties a node of this family/section MUST carry (V3). Unions every
        block claiming the family — see `_family_blocks`."""
        return self._family_union(family_or_section, "required")

    def recommended_properties(self, family_or_section: str) -> FrozenSet[str]:
        """Properties a node of this family/section SHOULD carry (V4 — lower
        severity than V3 by policy, see the brief §5). Unions every block claiming
        the family — see `_family_blocks`."""
        return self._family_union(family_or_section, "recommended")

    @property
    def lifecycle_stages(self) -> LifecycleStages:
        """`(pipeline, off_pipeline)` from `decision_lifecycle`; `([], [])` if the
        domain declares none."""
        block = self.raw.get("decision_lifecycle")
        if not isinstance(block, dict):
            return LifecycleStages([], [])
        return LifecycleStages(
            pipeline=_str_list(block.get("pipeline")),
            off_pipeline=_str_list(block.get("off_pipeline") or block.get("off-pipeline")),
        )

    @property
    def guidance_notes(self) -> List[str]:
        """`linter_guidance.notes` — hand-authored warnings the domain wrote FOR the
        linter. In Acme's profile these describe silent-drop mechanisms, authored into
        a field nothing read; this accessor is the field finally acquiring a reader."""
        block = self.raw.get("linter_guidance")
        if isinstance(block, dict):
            return _str_list(block.get("notes"))
        return _str_list(block)


# The default (no-profile) profile: only `core` is a registered namespace, matching
# the core's DEFAULT_REGISTERED_NS. Bare non-core edge types are still preserved as the
# FactLattice 'private' tier (unflagged) by the core — see parser `_type_flags`.
DEFAULT_PROFILE = Profile(registered_ns=DEFAULT_REGISTERED_NS, domain_id=None, raw={}, path=None)


def _registered_ns_from_raw(raw: Dict[str, object]) -> FrozenSet[str]:
    """Project a profile dict onto the core's `registered_ns` hook.

    Registered namespaces = {'core'} plus every namespace the profile's `domain`
    block declares. Bare relationship verbs are a SEPARATE hook — see
    `_domain_verbs_from_raw` and the parser's `domain_verbs=` argument. (The old
    docstring here claimed bare verbs "land in the core's unflagged private tier
    regardless" — they did NOT; that false claim is the defect this fix closes.)
    """
    ns = set(DEFAULT_REGISTERED_NS)  # always includes 'core'
    dom = raw.get("domain")
    if isinstance(dom, dict):
        for n in dom.get("namespaces") or []:
            ns.add(str(n))
    return frozenset(ns)


def _domain_verbs_from_raw(raw: Dict[str, object]) -> FrozenSet[str]:
    """Project a profile's `relationship_verbs` onto the parser's `domain_verbs` hook.

    Both `core:` and `domain:` lists are read (a profile may restate core verbs);
    verbs are upper-cased to match the parser's UPPER_SNAKE verb tokens. A profile
    with no `relationship_verbs` block yields the empty set == pre-fix behaviour.

    This function is the whole point of the fix: before it, `relationship_verbs` was
    read by EXACTLY ONE file in the deployment — the reporting-only
    `factlattice_engine/analytics/profile_audit.py` — and never by the parser. Verb
    registration was decorative from the parser's first commit.
    """
    rv = raw.get("relationship_verbs")
    if not isinstance(rv, dict):
        return DEFAULT_DOMAIN_VERBS
    out = set()
    for key in ("core", "domain"):
        vals = rv.get(key) or []
        if isinstance(vals, list):
            for v in vals:
                tok = str(v).strip().upper()
                if tok:
                    out.add(tok)
    return frozenset(out)


class ProfileCollisionError(ValueError):
    """A profile registration that would SUBSTITUTE rather than ADD.

    Raised at profile LOAD (never at parse, never as a warning) when a profile's
    `property_key` registration collides with a built-in minting key, or when two
    profiles registering the same key disagree about what it means.

    ⛔ WHY THIS IS A REFUSAL AND NOT A PRECEDENCE RULE. A lost edge is a silence, and a
    silence eventually gets noticed. A REDEFINED edge is a LIE: `part-of` has 1351 uses
    across 104 files, and a profile quietly remapping it would leave every one of those
    traversals answering confidently and wrongly, with `lts_lint` still reporting the
    corpus CLEAN. Last-wins and first-wins both pick a winner silently; a warning is a
    string nobody reads. The only safe behaviour is to refuse to load at all and name
    both definitions (the epistemics rule: signals that AGREE WITH THE WRONG ANSWER
    are the failure class this codebase is organised against).
    """


# A registrable property key must be able to PARSE as a property key, or the
# registration is inert while reading as functional (SPEC §2.2 P1).
_PROPERTY_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def _builtin_minting_keys() -> Dict[str, Tuple[str, str, bool]]:
    """The built-in minting table, read from the parser — never a second copy."""
    from .parser import FLOW_PROPERTY_EDGES, REL_PROPERTY_EDGES
    out: Dict[str, Tuple[str, str, bool]] = dict(REL_PROPERTY_EDGES)
    for k, v in FLOW_PROPERTY_EDGES.items():
        out[k] = (v[0], v[1], v[2])
    return out


def _declared_property_keys_from_raw(raw: Dict[str, object],
                                     path: Optional[str] = None) -> FrozenSet[str]:
    """Project a profile's `property_keys[]` onto A2's declared-key tier.

    ⛔ SAME REFUSAL CONSTRAINTS AS §1.3.1, DELIBERATELY WORDED THE SAME WAY. A key that
    cannot PARSE as a property key could never match anything, so the declaration would
    be inert while reading as functional — the identical failure §1.3.1 constraint 3
    guards for minting keys, one tier over. Two guards that disagreed about the same
    grammar would themselves be a fork, and every future author would have to re-derive
    which one governed their case (§1.3.2's own reasoning).

    ⛔ A MINTING KEY IS NOT RE-DECLARED HERE. Declaring an edge-bearing key as a plain
    descriptive key would be a quiet contradiction — one profile saying a key both does
    and does not mint — so it is refused rather than merged.
    """
    keys = raw.get("property_keys")
    if keys is None:
        return frozenset()
    if not isinstance(keys, list):
        raise ProfileCollisionError(
            f"profile `property_keys` must be a list of key names"
            f"{f' (profile: {path})' if path else ''}, got {type(keys).__name__}")
    where = f" (profile: {path})" if path else ""
    builtins = _builtin_minting_keys()
    out = set()
    for item in keys:
        key = _str_or_none(item)
        if not key:
            continue
        if not _PROPERTY_KEY_RE.match(key):
            raise ProfileCollisionError(
                f"profile declares property key '{key}'{where}, which cannot parse as "
                f"a property key (must match ^[a-z][a-z0-9_-]*$). The declaration "
                f"would be INERT while reading as functional.")
        if key in builtins:
            raise ProfileCollisionError(
                f"profile declares '{key}' under `property_keys`{where}, but it is a "
                f"BUILT-IN EDGE-MINTING key. Declaring it as a plain descriptive key "
                f"would have the profile assert that it both does and does not mint. "
                f"Remove it — built-in keys need no declaration.")
        out.add(key)
    return frozenset(out)


def _property_edges_from_raw(raw: Dict[str, object],
                             path: Optional[str] = None
                             ) -> Dict[str, Tuple[str, str, bool]]:
    """Project a profile's `edge_types[].property_key` onto the parser's hook.

    The maintainer's ruling 2026-08-15: "Domain profiles absolutely should be able to register
    their own edge keys." The declaration rides on the EXISTING `edge_types` record
    rather than a new parallel block, so an edge type is declared ONCE and simply says
    which property key mints it — reuse beats invention (the same call already recorded
    for `BEFORE` vs a new `NEXT`).

    Refuses, loudly, on a built-in collision or a non-key-shaped key.
    """
    recs = raw.get("edge_types")
    if not isinstance(recs, list):
        return {}
    builtins = _builtin_minting_keys()
    where = f" (profile: {path})" if path else ""
    out: Dict[str, Tuple[str, str, bool]] = {}
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        key = _str_or_none(rec.get("property_key") or rec.get("property-key"))
        if not key:
            continue
        etype = _str_or_none(rec.get("type") or rec.get("edge-type"))
        if not etype:
            raise ProfileCollisionError(
                f"profile registers property_key '{key}' with no edge type{where}")
        if not _PROPERTY_KEY_RE.match(key):
            raise ProfileCollisionError(
                f"profile registers property_key '{key}'{where}, which cannot parse as "
                f"a property key (must match ^[a-z][a-z0-9_-]*$). The registration "
                f"would be INERT while reading as functional.")
        if key in builtins:
            b_type, b_layer, b_rev = builtins[key]
            raise ProfileCollisionError(
                f"profile{where} registers property key '{key}' -> {etype}, but '{key}' "
                f"is a BUILT-IN minting key meaning {b_type} "
                f"(layer={b_layer}, reversed={b_rev}). Refusing to load: redefining a "
                f"built-in is a SUBSTITUTION, not an addition — every existing use would "
                f"silently change meaning. Choose a different property key.")
        layer = _str_or_none(rec.get("layer")) or "semantic"
        rev = bool(rec.get("reversed") or rec.get("reversed_dir") or False)
        prior = out.get(key)
        if prior is not None and prior != (etype, layer, rev):
            raise ProfileCollisionError(
                f"profile{where} registers property key '{key}' twice, disagreeing: "
                f"{prior} vs {(etype, layer, rev)}")
        out[key] = (etype, layer, rev)
    return out


# ===========================================================================
# THE EDGE-TYPE NAMESPACE — the same discipline, applied one level up
# ===========================================================================
# ⛔ WHY THIS BLOCK EXISTS. The 2026-08-15 ruling made property KEYS registrable and
# guarded them with load-time refusals. The engineer who built it flagged its own hole
# in the handover: "I applied collision discipline to property KEYS only, not to the
# edge-TYPE namespace." Measured against the code as it stood at 3f9a2c1:
#
#   * a profile could declare `{type: PART_OF, property_key: contained-by,
#     layer: causal}` and LOAD CLEAN. The resulting graph then carries PART_OF edges in
#     TWO layers at once — `lts_traverse(layers=['semantic'])` silently returns half of
#     them, and `lts_lint` calls the corpus clean. Reproduced, printed, before the fix.
#   * two profiles could declare `GOVERNS` at different layers through DIFFERENT
#     property keys, so the key-level guard stayed silent and nothing refused.
#   * `domain_profiles.load_profile_edge_config` resolved exactly that conflict
#     FIRST-WINS with an `.ambiguities` record — a warning nobody reads, on the live
#     engine build path.
#
# THE RULES, chosen to AGREE WITH THE PROPERTY-KEY PATH RATHER THAN MERELY BEING
# DEFENSIBLE. Two collision rules that disagree with each other would themselves be a
# fork, and every future author would have to re-derive which one governs their case:
#
#   R1 CORE COLLISION -> refuse ALWAYS, even if identical.       (== the built-in rule)
#   R2 CROSS-PROFILE  -> agreement merges, disagreement refuses. (== the merge rule)
#   R3 NAME SHAPE     -> refuse what the parser cannot recognise. (== the key-shape rule)
#
# ⛔ R1 APPLIES TO BARE NAMES ONLY, AND THAT IS NOT A LOOPHOLE — it is the entire point
# of a namespace. Measured over the six shipped `domain-profiles/*.lts`: 72 declared edge
# types, ALL namespaced, and NINE of them deliberately disagree with core on the layer
# (`art.SUPERSEDES`/`dec.SUPERSEDES`/`swe.SUPERSEDES` are temporal where core SUPERSEDES
# is semantic; `dec.DEPENDS_ON`/`swe.DEPENDS_ON` are causal where core is semantic;
# `dec.RESOLVES` is causal). A rule keyed on the BARE SUFFIX would refuse all nine and
# break every shipped profile, while protecting nothing: `dec.SUPERSEDES` and
# `SUPERSEDES` are distinct types in the graph and no traversal can confuse them. It is
# the BARE name that is indistinguishable from core, so it is the bare name that is
# refused.
#
# ⛔ WHY `reversed` IS NOT PART OF AN EDGE TYPE'S IDENTITY, and why R2 compares LAYER.
# `reversed` is a property of the KEY -> TYPE mapping (which voice the key is written
# in), not of the type. Core itself proves it: `supersedes` and `superseded-by` both mint
# SUPERSEDES with opposite `reversed`. Refusing two profiles for declaring the active and
# passive voice of the same relation would refuse what core does on line 150 of the
# parser. The LAYER is the type's identity because the layer is what
# `lts_traverse(layers=[...])` filters on — declare one type at two layers and half the
# graph goes missing from every layer-scoped query, which is the failure R2 exists for.

# The conforming edge-type NAME shape, DERIVED FROM THE PARSER'S OWN RECOGNISERS rather
# than composed from memory, and pinned by a round-trip test that feeds each candidate
# through the real `_find_edge_type` (`tests/test_edge_type_namespace.py`):
#
#   BARE       `_UNREG_VERB_RE` = `^([A-Z][A-Z0-9_]*)[ \t]+(?=\[)` — the ANCHORED form
#              the parser tries FIRST, and the one that exists to kill a fabrication
#              class. Uppercase-initial, uppercase/digit/underscore after.
#   NAMESPACED `_TOKEN_RE` segments `[A-Za-z][A-Za-z0-9_]*` joined by dots, with the
#              FINAL segment additionally required to match `_UPPER_ID_RE`.
#
# Measured non-conformers and what the parser ACTUALLY does with each — note that the
# last four are not merely inert, they SUBSTITUTE a different edge type, which is the
# [SL-18] class:
#     'CONTRA-INDICATES'         -> no verb found        (inert)
#     'HAS PROPERTY'             -> no verb found        (inert)
#     '_LEADING' / '1STEP'       -> no verb found        (inert)
#     'clinical.contraindicates' -> no verb found        (inert)
#     '.GOVERNS'                 -> `GOVERNS`            ⛔ SUBSTITUTED
#     'clinical..GOVERNS'        -> `GOVERNS`            ⛔ SUBSTITUTED
#     'clin-ical.GOVERNS'        -> `ical.GOVERNS`       ⛔ SUBSTITUTED
#     'clinical.GOV-ERNS'        -> `clinical.GOV`       ⛔ SUBSTITUTED
_EDGE_TYPE_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9_]*\.)*[A-Z][A-Z0-9_]*$")


def _core_edge_layers() -> Dict[str, str]:
    """The core edge-type table, read from the parser — never a second copy.

    A second list here would drift from `CORE_EDGE_LAYER` and the guard would go
    partially inert for exactly the types added after it was written: the same failure
    shape as the defect this whole branch is about.
    """
    from .parser import CANONICAL_LAYERS, CORE_EDGE_LAYER  # noqa: F401
    return dict(CORE_EDGE_LAYER)


def _canonical_layers() -> Tuple[str, ...]:
    from .parser import CANONICAL_LAYERS
    return tuple(CANONICAL_LAYERS)


def _edge_types_from_raw(raw: Dict[str, object],
                         path: Optional[str] = None) -> Dict[str, str]:
    """Project a profile's `edge_types[]` declarations onto `{edge_type -> layer}`.

    This is the EDGE-TYPE half of the 2026-08-15 registrability ruling, and it enforces
    R1/R3 above. R2 lives in `merge_edge_types` (and, for the live engine build path, in
    `domain_profiles.load_profile_edge_config`).

    Returns the declared types even when they carry no `property_key` — a namespaced
    type declared purely for its layer is still a registration, and
    `lts_profile_audit` has to be able to see it or a registered-but-unused edge type
    is invisible to the one tool built to find that.
    """
    recs = raw.get("edge_types")
    if not isinstance(recs, list):
        return {}
    core = _core_edge_layers()
    canonical = _canonical_layers()
    where = f" (profile: {path})" if path else ""
    out: Dict[str, str] = {}
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        etype = _str_or_none(rec.get("type") or rec.get("edge-type"))
        if not etype:
            continue
        # --- R3: a name the parser cannot recognise, or would recognise as SOMETHING
        # ELSE. Both are refused, and the second is the worse of the two.
        if not _EDGE_TYPE_RE.match(etype):
            raise ProfileCollisionError(
                f"profile{where} declares edge type '{etype}', which is not an edge-type "
                f"name the parser can recognise (must match "
                f"^(?:[A-Za-z][A-Za-z0-9_]*\\.)*[A-Z][A-Z0-9_]*$ — an UPPER_SNAKE bare "
                f"verb, optionally dot-namespaced). Refusing to load: such a declaration "
                f"is at best INERT while reading as functional, and at worst SUBSTITUTED "
                f"— the parser reads 'clin-ical.GOVERNS' as the edge type 'ical.GOVERNS' "
                f"and emits it with no flag.")
        # --- R1: a BARE core name. Refused even if the declaration is identical.
        if "." not in etype and etype in core:
            dom = raw.get("domain")
            ns = (_str_or_none(dom.get("id")) if isinstance(dom, dict) else None) or "mydomain"
            ns = ns.split(".")[-1]
            raise ProfileCollisionError(
                f"profile{where} declares edge type '{etype}', which is a CORE edge type "
                f"(layer={core[etype]}). Refusing to load: a profile may not redefine a "
                f"core type, EVEN IDENTICALLY — the graph would carry one type with two "
                f"meanings and every layer-scoped traversal would return part of it, "
                f"confidently. Namespace it ('{ns}.{etype}') to declare your own, or "
                f"declare it under `relationship_verbs` if you only mean to RESTATE that "
                f"the core verb is used here.")
        layer = _str_or_none(rec.get("layer"))
        if layer is not None and layer not in canonical:
            # Not one of the four canonical layers -> no `lts_traverse(layers=[...])`
            # filter can ever name it, so the declaration is inert while reading as
            # functional. Same class as R3, one field over.
            raise ProfileCollisionError(
                f"profile{where} declares edge type '{etype}' at layer '{layer}', which "
                f"is not one of the canonical layers {list(canonical)}. Refusing to load: "
                f"no layer-scoped query could ever name it, so the declaration would be "
                f"INERT while reading as functional.")
        layer = layer or "semantic"
        prior = out.get(etype)
        if prior is not None and prior != layer:
            raise ProfileCollisionError(
                f"profile{where} declares edge type '{etype}' twice, disagreeing about "
                f"its layer: '{prior}' vs '{layer}'. Refusing to load a profile that "
                f"contradicts itself.")
        out[etype] = layer
    return out


def merge_edge_types(profiles: Sequence["Profile"]) -> Dict[str, str]:
    """Union the declared edge TYPES of several profiles, refusing disagreement (R2).

    The exact shape of `merge_property_edges`, deliberately: SAME-NAME-SAME-DEFINITION
    MERGES, disagreement refuses and names both profiles and both definitions. Never
    last-wins, never first-wins, never a warning.

    ⛔ WHY AGREEMENT IS ALLOWED HERE WHILE A CORE COLLISION IS REFUSED EVEN IDENTICALLY.
    They are not the same situation and the asymmetry is deliberate. A core type has ONE
    owner — the platform — so a profile restating it adds nothing and can only ever start
    to drift; refusing it keeps a single source of truth. Two DOMAIN profiles, by
    contrast, have no shared owner: `corpora/acme-specs/` is a root of THREE
    registered corpora, so two profiles declaring `EXEMPLIFIES` at `semantic` is the
    ORDINARY case, and refusing it would make a shared root unusable. What must never
    happen is the two disagreeing, because then the type's meaning depends on load order.
    """
    out: Dict[str, str] = {}
    owner: Dict[str, Optional[str]] = {}
    for prof in profiles:
        pid = prof.domain_id or prof.path or "<anonymous>"
        for etype, layer in (prof.edge_types or {}).items():
            prior = out.get(etype)
            if prior is not None and prior != layer:
                raise ProfileCollisionError(
                    f"profiles '{owner.get(etype)}' and '{pid}' both declare edge type "
                    f"'{etype}' but disagree about its layer: '{prior}' vs '{layer}'. "
                    f"Refusing to build a graph whose edge meaning depends on profile "
                    f"load order.")
            out[etype] = layer
            owner.setdefault(etype, pid)
    return out


def merge_property_edges(profiles: Sequence["Profile"]
                         ) -> Dict[str, Tuple[str, str, bool]]:
    """Union the registered property-edge keys of several profiles, refusing conflicts.

    ⛔ THIS IS A LIVE SCENARIO, NOT A HYPOTHETICAL. `corpora/acme-specs/` is a root
    of THREE registered corpora (`acme-console`, `acme-viewer`,
    `acme-monitor`). A file dropped there joins all three, so two profiles
    disagreeing about one key is an ordinary Tuesday. Agreement is fine and expected;
    disagreement refuses and names both profiles.
    """
    out: Dict[str, Tuple[str, str, bool]] = {}
    owner: Dict[str, Optional[str]] = {}
    for prof in profiles:
        pid = prof.domain_id or prof.path or "<anonymous>"
        for key, spec in (prof.property_edges or {}).items():
            prior = out.get(key)
            if prior is not None and prior != spec:
                raise ProfileCollisionError(
                    f"profiles '{owner.get(key)}' and '{pid}' both register property "
                    f"key '{key}' but disagree: {prior} vs {spec}. Refusing to build a "
                    f"graph whose edge meaning depends on profile load order.")
            out[key] = spec
            owner.setdefault(key, pid)
    return out


def load_profile(source) -> Profile:
    """Load a domain profile from a path (str/os.PathLike) or a dict.

    - str/PathLike ending in `.lts` / a YAML / JSON suffix -> parse the file.
    - dict -> treat as an already-parsed profile body.
    - Profile -> returned as-is.
    Raises FileNotFoundError for a missing path, and a clear RuntimeError if a
    `.yaml` is given without PyYAML installed (never silently ignore a profile).
    All three backends produce the SAME dict, so the typed accessors on the returned
    `Profile` are identical regardless of which serialization was on disk.
    """
    if isinstance(source, Profile):
        return source
    if isinstance(source, dict):
        raw = source
        dom = raw.get("domain") or {}
        domain_id = dom.get("id") if isinstance(dom, dict) else None
        # ⛔ VALIDATE AT LOAD, not on first access. A collision discovered lazily is a
        # collision discovered by whoever happens to read the accessor — which on this
        # path is nobody, so the bad registration would simply be ignored.
        # BOTH namespaces are validated here: the property KEYS and the edge TYPES. The
        # 2026-08-15 ruling shipped with only the first, so a profile shadowing core
        # `PART_OF` loaded clean and produced a two-layer PART_OF graph.
        _property_edges_from_raw(raw, None)
        _edge_types_from_raw(raw, None)
        return Profile(registered_ns=_registered_ns_from_raw(raw),
                       domain_verbs=_domain_verbs_from_raw(raw),
                       domain_id=domain_id, raw=raw, path=None)

    path = os.fspath(source)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"profile not found: {path}")
    raw = _read_profile_file(path)
    dom = raw.get("domain") or {}
    domain_id = dom.get("id") if isinstance(dom, dict) else None
    abspath = os.path.abspath(path)
    _property_edges_from_raw(raw, abspath)
    _edge_types_from_raw(raw, abspath)
    return Profile(registered_ns=_registered_ns_from_raw(raw),
                   domain_verbs=_domain_verbs_from_raw(raw),
                   domain_id=domain_id, raw=raw, path=abspath)


def _read_profile_file(path: str) -> Dict[str, object]:
    lower = path.lower()
    if lower.endswith((".yaml", ".yml")):
        try:
            import yaml  # PyYAML: optional dep, needed only for YAML profiles
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                f"profile {path!r} is YAML but PyYAML is not installed; "
                "`pip install factlattice[profile]` (or PyYAML) to load YAML profiles"
            ) from exc
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    elif lower.endswith(".json"):
        import json
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    elif lower.endswith(".lts"):
        data = _read_lts_profile(path)
    else:
        raise ValueError(
            f"unsupported profile extension: {path!r} (want .lts/.yaml/.yml/.json)")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"profile {path!r} did not parse to a mapping")
    return data


# ---------------------------------------------------------------------------
# `.lts` backend — [PROFILE-SELFHOST]
# ---------------------------------------------------------------------------
# The whole backend is ONE projection function feeding the SAME dict the YAML
# loader produces, so equivalence of the two serializations is BY CONSTRUCTION
# and nothing downstream (accessors, validators, `raw`) can tell them apart.
# Encoding + the bootstrapping invariant: see the module docstring.

# Flags whose presence proves the profile reached for vocabulary it has not yet
# registered — i.e. that it tried to parse itself with itself.
_BOOTSTRAP_VIOLATION_FLAGS: FrozenSet[str] = frozenset({
    "unregistered_ns", "unregistered_verb",
})


def _assert_core_only_parse(parsed, path: str) -> None:
    """⛔ Enforce the bootstrapping invariant.

    `_read_lts_profile` parses with CORE settings only (`DEFAULT_REGISTERED_NS`,
    `DEFAULT_DOMAIN_VERBS`) — it cannot do otherwise, because the profile is what
    would supply the domain settings and it is not loaded yet. That makes any
    `unregistered_ns` / `unregistered_verb` flag on the resulting edges a proof that
    the profile used a notation it is itself registering. Such a document parses
    DIFFERENTLY before and after it is loaded, so its projection is not trustworthy.
    We raise instead of loading it half-understood (never silently drop).
    """
    offenders = []
    for edge in getattr(parsed, "edges", ()):  # tolerate any ParsedFile shape
        hit = _BOOTSTRAP_VIOLATION_FLAGS.intersection(edge.flags or ())
        if hit:
            offenders.append(f"line {edge.source_line}: {edge.type} ({', '.join(sorted(hit))})")
    if offenders:
        raise ValueError(
            f"profile {path!r} violates the .lts profile BOOTSTRAPPING INVARIANT: a "
            "profile must be parseable with CORE notation only — it cannot use the "
            "domain notations it registers, or the profile would be needed to parse "
            "the profile. Offending edges: " + "; ".join(offenders[:5])
            + (f" (+{len(offenders) - 5} more)" if len(offenders) > 5 else "")
        )


def _lts_list(value: object) -> List[str]:
    """A `.lts` property value -> `list[str]`, by the ONE mechanical rule.

    * P4 nested (`- key:` + indented items) parses to a `list` -> items VERBATIM,
      so a stage name or a note may contain commas.
    * P1/P2 inline (`- key: A, B`) parses to a `str` -> COMMA-SPLIT, which is how a
      short registry line (`- families: DEC-, MUT-`) is authored.

    A repeated inline key accumulates to a list of strs (parser `_maybe_multivalue`);
    each element is then comma-split too, which is the only reading under which
    `- ns: a` twice and `- ns: a, b` once cannot disagree.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for item in value:
            text = _str_or_none(item)
            if text:
                out.append(text)
        return out
    text = _str_or_none(value)
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _lts_inline_list(value: object) -> List[str]:
    """As `_lts_list` but comma-splits list elements too — used for the inline-only
    registry fields (`namespaces`, `families`, `decision-prefixes`) where a comma
    never belongs to the value."""
    out: List[str] = []
    for item in _lts_list(value):
        out.extend(part.strip() for part in item.split(",") if part.strip())
    return out


def _project_lts_nodes(nodes: Sequence) -> Dict[str, object]:
    """Project parsed `.lts` profile nodes onto the YAML loader's dict shape.

    Dispatch is on the node's `type_prefix` (the id text before the first hyphen),
    which is exactly the "node id prefix carries the kind" rule of the encoding. A
    node whose kind is unrecognised, or whose payload property is missing, is
    SKIPPED — a `.lts` profile is a normal document and may carry prose sections,
    an h1 document node, and section dividers alongside its declarations.
    """
    domain: Dict[str, object] = {}
    prefixes: List[Dict[str, object]] = []
    document_tags: List[Dict[str, object]] = []
    property_keys: List[str] = []
    review_extensions: List[Dict[str, object]] = []
    edge_types: List[Dict[str, object]] = []
    rel_verbs: Dict[str, List[str]] = {}
    notes: List[str] = []
    out: Dict[str, object] = {}

    for node in nodes:
        kind = (node.type_prefix or "").upper()
        props = node.properties or {}

        if kind == "PROFILE":
            domain_id = _str_or_none(props.get("domain-id"))
            if not domain_id:
                continue  # not the domain declaration (e.g. a prose node named PROFILE-*)
            domain["id"] = domain_id
            for prop_key, out_key in (("domain-name", "name"),
                                      ("domain-version", "version"),
                                      ("conventions-source", "conventions_source")):
                got = _str_or_none(props.get(prop_key))
                if got:
                    domain[out_key] = got
            namespaces = _lts_inline_list(props.get("namespaces"))
            if namespaces:
                domain["namespaces"] = namespaces
            review_output = _str_or_none(props.get("review-output"))
            if review_output:
                domain["review_output"] = review_output     # SPEC §10.4 (A-E5)

        elif kind == "PREFIX":
            prefix = _str_or_none(props.get("registers-prefix"))
            if not prefix:
                continue
            rec: Dict[str, object] = {"prefix": prefix}
            document = _str_or_none(props.get("document"))
            if document:
                rec["document"] = document
            note = _str_or_none(props.get("definition") or props.get("note"))
            if note:
                rec["note"] = note
            prefixes.append(rec)

        elif kind == "DOCTAG":
            tag = _str_or_none(props.get("document-tag"))
            if not tag:
                continue
            rec = {"tag": tag}
            note = _str_or_none(props.get("definition") or props.get("note"))
            if note:
                rec["note"] = note
            document_tags.append(rec)

        elif kind == "EDGETYPE":
            edge_type = _str_or_none(props.get("edge-type"))
            if not edge_type:
                continue
            # `- property-key: <key>` registers this edge type as MINTABLE FROM A
            # PROPERTY (the maintainer's ruling 2026-08-15). It rides the existing EDGETYPE
            # declaration so the type is declared once. Independent of bare/namespaced:
            # a BARE domain verb (EXEMPLIFIES) is exactly the common case.
            prop_key = _str_or_none(props.get("property-key"))
            if prop_key:
                rec = {"type": edge_type, "property_key": prop_key}
                layer = _str_or_none(props.get("layer"))
                if layer:
                    rec["layer"] = layer
                reversed_raw = (_str_or_none(props.get("reversed")) or "").lower()
                if reversed_raw in ("true", "yes", "1"):
                    rec["reversed"] = True
                definition = _str_or_none(props.get("definition"))
                if definition:
                    rec["description"] = definition
                edge_types.append(rec)
                if "." not in edge_type:
                    # still a bare verb for `## Relationships` triples
                    tier = (_str_or_none(props.get("tier")) or "domain").lower()
                    rel_verbs.setdefault(tier, []).append(edge_type.upper())
                continue
            if "." in edge_type:
                # NAMESPACED: the parser already accepts it once its namespace is
                # registered, so it needs an edge_types[] declaration (layer table),
                # NOT a bare-verb registration.
                rec = {"type": edge_type}
                layer = _str_or_none(props.get("layer"))
                if layer:
                    rec["layer"] = layer
                definition = _str_or_none(props.get("definition"))
                if definition:
                    rec["description"] = definition
                edge_types.append(rec)
            else:
                # BARE: this is what `domain_verbs` is, and it is the ONLY thing that
                # makes `[A] GOVERNS [B]` a recognised triple (parser `_find_edge_type`).
                tier = (_str_or_none(props.get("tier")) or "domain").lower()
                rel_verbs.setdefault(tier, []).append(edge_type.upper())

        elif kind == "PROPKEY":
            # SPEC §1.3.3 (A2): a profile declares the DESCRIPTIVE keys it owns.
            # ⛔ REUSES `- property-key:`, the same field an EDGETYPE node uses to
            # register a MINTING key. Dispatch is on the node's type_prefix, so the two
            # can never be confused, and reuse beats invention — the same call already
            # recorded for `BEFORE` vs a new `NEXT`. A `- property-keys:` nested list is
            # accepted too, because A2's seed sets run to hundreds of keys per corpus
            # and one node per key would be unreadable.
            one = _str_or_none(props.get("property-key"))
            if one:
                property_keys.append(one)
            property_keys.extend(_lts_list(props.get("property-keys")))
            property_keys.extend(_lts_inline_list(props.get("property-keys")))

        elif kind == "REVEXTREG":
            # SPEC §10.4 (A-E5): a REGISTRATION of a review extension. NOT `REVEXT-`, which is the extension
            # DOCUMENT's own root node (audit delta N1). Both properties are required; a half-registration is
            # skipped here and reported by `unprojected_review_extensions` / `validate_review_extensions`.
            ext_path = _str_or_none(props.get("path"))
            ext_id = _str_or_none(props.get("ext-id"))
            if ext_path and ext_id:
                review_extensions.append({"path": ext_path, "ext_id": ext_id})

        elif kind == "ENUM":
            enum_key = _str_or_none(props.get("enum-key"))
            if not enum_key or enum_key in _STRUCTURAL_KEYS:
                continue
            values = _lts_list(props.get("values"))
            extended = _lts_list(props.get("extended-values"))
            out[enum_key] = {"core": values, "extended": extended} if extended else values

        elif kind == "NODEFAMILY":
            section = _str_or_none(props.get("section"))
            if not section:
                continue
            block: Dict[str, object] = {}
            families = _lts_inline_list(props.get("families"))
            if families:
                block["families"] = families
            for prop_key in ("required", "recommended"):
                items = _lts_list(props.get(prop_key))
                if items:
                    block[prop_key] = items
            out[section] = block

        elif kind == "LIFECYCLE":
            section = _str_or_none(props.get("section")) or "decision_lifecycle"
            block = {}
            pipeline = _lts_list(props.get("pipeline"))
            if pipeline:
                block["pipeline"] = pipeline
            off_pipeline = _lts_list(props.get("off-pipeline"))
            if off_pipeline:
                block["off_pipeline"] = off_pipeline
            decision_prefixes = _lts_inline_list(props.get("decision-prefixes"))
            if decision_prefixes:
                block["decision_prefixes"] = decision_prefixes
            session_key = _str_or_none(props.get("session-key-property"))
            if session_key:
                block["session_key_property"] = session_key
            out[section] = block

        elif kind == "GUIDANCE":
            note = _str_or_none(props.get("note"))
            if note:
                notes.append(note)

    if domain:
        out["domain"] = domain
    if prefixes:
        out["prefixes"] = prefixes
    if document_tags:
        out["document_tags"] = document_tags
    if edge_types:
        out["edge_types"] = edge_types
    if review_extensions:
        out["review_extensions"] = review_extensions
    if property_keys:
        # de-duplicated, order-stable: a key declared twice is agreement, not conflict
        out["property_keys"] = sorted(set(property_keys))
    if rel_verbs:
        out["relationship_verbs"] = rel_verbs
    if notes:
        out["linter_guidance"] = {"notes": notes}
    return out


def _read_lts_profile(path: str) -> Dict[str, object]:
    """Parse a `.lts` profile with CORE settings only, then project it.

    Core-only is not a default that could drift — it is the bootstrapping invariant
    made structural: this function has no profile to consult, because it IS the
    thing that produces one.
    """
    from .parser import parse_file  # local: keeps the import graph acyclic and cheap

    parsed = parse_file(
        path,
        registered_ns=DEFAULT_REGISTERED_NS,   # ⛔ core only — see module docstring
        domain_verbs=DEFAULT_DOMAIN_VERBS,     # ⛔ core only
        profile_layers=None,                   # ⛔ core only
    )
    _assert_core_only_parse(parsed, path)
    return _project_lts_nodes(parsed.nodes)


# ---------------------------------------------------------------------------
# SPEC §10.4 (A-E5) — review-extension registrations: the validator and the unknown-kind check
# ---------------------------------------------------------------------------
# Node kinds `_project_lts_nodes` DISPATCHES on. A profile node whose kind is none of these is SKIPPED by the
# loader without a word; `unprojected_review_extensions` names the ones that look like a registration.
PROFILE_NODE_KINDS: FrozenSet[str] = frozenset({
    "PROFILE", "PREFIX", "DOCTAG", "EDGETYPE", "PROPKEY", "ENUM", "NODEFAMILY", "LIFECYCLE", "GUIDANCE",
    "REVEXTREG",
})


def unprojected_review_extensions(profile_path: str) -> List[Dict[str, object]]:
    """Profile nodes that CARRY `path` AND `ext-id` but whose kind the loader does not know.

    This is the loud half of audit D4: the `.lts` loader skips an unrecognised kind silently, so a registration
    spelled `REVEXT-X` (the extension document's own kind) or `REVEXTREG_X` or `REVEXTRG-X` parses, lints
    clean and registers nothing. Only a `.lts` profile has node kinds; any other serialisation returns `[]`.
    A node of a KNOWN kind that merely carries two more properties is not reported. Never raises.
    """
    if not profile_path or not str(profile_path).lower().endswith(".lts") or not os.path.isfile(profile_path):
        return []
    from .parser import parse_file
    try:
        parsed = parse_file(profile_path, registered_ns=DEFAULT_REGISTERED_NS,
                            domain_verbs=DEFAULT_DOMAIN_VERBS, profile_layers=None)
    except Exception:                                    # noqa: BLE001 — an unreadable profile is the loader's report
        return []
    out: List[Dict[str, object]] = []
    for node in parsed.nodes:
        kind = (node.type_prefix or "").upper()
        if kind in PROFILE_NODE_KINDS:
            continue
        props = node.properties or {}
        if _str_or_none(props.get("path")) and _str_or_none(props.get("ext-id")):
            out.append({"node_id": node.id, "kind": kind, "line": node.line_range[0] if node.line_range else None,
                        "message": (f"{node.id} carries `path` and `ext-id` but its kind {kind!r} is not a "
                                    "profile node kind the loader projects, so it registers NOTHING; the "
                                    "registration kind is REVEXTREG")})
    return out


def _ext_node_id(ext_id: str) -> str:
    e = str(ext_id).strip()
    return e if e.upper().startswith("REVEXT-") else "REVEXT-" + e


def _ext_bare(ext_id: str) -> str:
    e = str(ext_id).strip()
    return e[len("REVEXT-"):] if e.upper().startswith("REVEXT-") else e


def validate_review_extensions(profile: "Profile",
                               corpus_roots: Optional[Sequence[str]] = None) -> List[Dict[str, object]]:
    """Check every registered review extension; `[]` means all fine. Findings are dicts
    `{kind, ext_id, path, message[, keys]}`:

      ext_path_unresolved            the path does not resolve (by realpath) to a readable `.lts` file;
      ext_id_not_in_file             the file defines no node `REVEXT-<ext-id>` (the full node id is accepted too);
      ext_id_duplicate               two registrations share an ext-id (addendum C1 step 0), naming BOTH realpaths;
      ext_strict_in_host_missing_keys (C2) with `corpus_roots` given: the extension lies inside a root of the
                                     corpus it serves, is stamped `lts: "1.1"`, and uses REV* property keys the
                                     host profile does not declare, so registering it would make the host fail
                                     its OWN structure gate. An extension outside every root may be strict.

    Relative paths resolve against the profile file's directory (`Profile.resolve_path`). Nothing here raises
    for a bad registration; a bad registration is the finding.
    """
    findings: List[Dict[str, object]] = []
    seen: Dict[str, str] = {}
    declared = profile.declared_property_keys
    for reg in profile.review_extensions:
        real = profile.resolve_path(reg.path)
        bare = _ext_bare(reg.ext_id)

        def add(kind, message, **extra):
            findings.append({"kind": kind, "ext_id": reg.ext_id, "path": reg.path, "message": message, **extra})

        if not real or not os.path.isfile(real) or not real.lower().endswith(".lts"):
            add("ext_path_unresolved",
                f"extension {reg.ext_id!r}: path {reg.path!r} does not resolve to a readable .lts file"
                + (f" (realpath {real})" if real else " (relative path with no profile directory to resolve against)"))
            continue
        if bare in seen:
            add("ext_id_duplicate",
                f"ext-id {bare!r} is registered twice: {seen[bare]} and {real}")
        else:
            seen[bare] = real
        from .parser import parse_file
        try:
            parsed = parse_file(real, registered_ns=DEFAULT_REGISTERED_NS, domain_verbs=DEFAULT_DOMAIN_VERBS,
                                profile_layers=None)
        except Exception as exc:                         # noqa: BLE001
            add("ext_path_unresolved", f"extension {reg.ext_id!r}: {real} could not be parsed ({type(exc).__name__})")
            continue
        want = _ext_node_id(reg.ext_id)
        if not any(n.id == want for n in parsed.nodes):
            add("ext_id_not_in_file", f"extension {reg.ext_id!r}: {real} defines no node {want}")
        if corpus_roots:
            inside = any(_is_within(real, os.path.realpath(r)) for r in corpus_roots)
            if inside and getattr(parsed, "mode", None) == "strict":
                used = sorted({k for n in parsed.nodes if (n.type_prefix or "").upper().startswith("REV")
                               for k in (n.properties or {}) if k != "aliases"})
                missing = [k for k in used if k not in declared]
                if missing:
                    add("ext_strict_in_host_missing_keys",
                        f"extension {reg.ext_id!r} ({real}) would make the corpus it serves fail its own "
                        "structure gate (a strict-mode document using undeclared keys); drop the lts marker "
                        "or declare the keys", keys=missing)
    return findings


def discover_profile(corpus_root: str) -> Optional[str]:
    """Return the path of the auto-discovered profile in `corpus_root`, or None.

    Two conventions, checked in this order:
      1. `*-profile.lts` — `[PROFILE-SELFHOST]`, the normative form a domain moves TO;
      2. `[0-9][0-9]-*-profile.yaml` — contract §5 / the `99-*-profile.yaml` legacy
         form, which stays fully supported (the live Acme profile is one).

    `.lts` is preferred so a corpus mid-migration that carries BOTH gets the new one
    deterministically. Within a convention the lexicographically-first match wins (a
    corpus normally carries exactly one profile). Note the `.lts` glob requires the
    `-profile` suffix: a bare `*.lts` would match every document in the corpus.
    """
    if not corpus_root or not os.path.isdir(corpus_root):
        return None
    for pattern in (PROFILE_GLOB_LTS, PROFILE_GLOB):
        matches = sorted(glob.glob(os.path.join(corpus_root, pattern)))
        if matches:
            return matches[0]
    return None


# ---------------------------------------------------------------------------
# THE CORPUS REGISTRY — a profile is REGISTERED, not merely co-located
# ---------------------------------------------------------------------------
# A profile that is only ever auto-discovered is a profile that only applies to the
# ONE directory it happens to sit in. That is how a landed, parsing, 32-prefix
# profile can be wired to NOTHING: `corpora/acme-console/` discovers it,
# `corpora/acme-specs/` discovers a different (legacy) file, and a lint over a
# FILE LIST discovers neither — three answers to one question, none of them declared.
#
# The corpus registry already declares which roots belong to which corpus.
# A `"profile"` key on a corpus entry is therefore the natural place to DECLARE the
# vocabulary those roots are judged against, and it is read here — in the library,
# stdlib-only — so `factlattice.parse()`, `lint()` and the `lts_lint` MCP tool all
# resolve THE SAME profile for the same bytes on disk. The registry is chosen by
# `factlattice.registry.resolve_registry()` -- the one lookup the engine's `config.py`
# also uses -- so this library and the engine can never read two different registries
# (and the library still never imports the engine: the resolver is in the CORE package).

# How a profile came to be in scope. A lint verdict that does not name one of these
# is not a verdict — the caller cannot tell "clean" from "the rules were off".
RESOLVED_EXPLICIT = "explicit-argument"
RESOLVED_CORPUS_CONFIG = "corpus-config"
RESOLVED_AUTO_DISCOVERY = "directory-auto-discovery"
RESOLVED_NONE = "none"


class ProfileResolution(NamedTuple):
    """The effective Profile PLUS the provenance of how it got chosen.

    `source` is one of the four RESOLVED_* constants. `none` is a first-class,
    REPORTED outcome — never a silent default: it means every profile-driven rule
    (V1-V6) was inert for this run, and a caller reading a clean result needs to
    know that.

    `detail` names the corpus id (corpus-config), the discovered filename
    (auto-discovery), or None. `conflict` is non-empty only when two corpora claim
    the same root with DIFFERENT profiles — surfaced rather than resolved silently.
    """

    profile: Profile
    source: str
    detail: Optional[str] = None
    root: Optional[str] = None
    conflict: Tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return bool(self.profile.raw)

    def to_dict(self) -> dict:
        out = {
            "resolved_from": self.source,
            "path": self.profile.path,
            "domain_id": self.profile.domain_id,
            "available": self.available,
            "corpus_root": self.root,
        }
        if self.detail:
            out["detail"] = self.detail
        if self.conflict:
            out["conflict"] = list(self.conflict)
        if not self.available:
            out["warning"] = (
                "NO PROFILE IN SCOPE — every profile-driven rule (registered prefixes, "
                "document tags, required/recommended properties, enums, lifecycle) was "
                "INERT for this run. A clean result here does not mean the corpus "
                "conforms; it means nothing was checked. Register a profile for this "
                "root in the corpus registry, or pass profile=."
            )
        return out


def load_corpus_config(path: Optional[str] = None) -> Dict[str, dict]:
    """The corpus registry's entries, from THE lookup (`registry.resolve_registry`), or from `path`.

    ⛔ THIS NO LONGER DEGRADES TO `{}`. It used to be "total": an unset variable, a missing file or a
    broken one all returned an empty registry, because a registry that cannot be read must not throw
    out of a parse. That contract is what let `check-registrations` report `ok` with zero registrations
    over the WRONG registry (the audit's M-1): an empty answer from a broken source is a silent
    fallback by another name. It now raises `registry.RegistryError` (`registry-not-found`) on a broken
    configured registry, and every caller either surfaces it as a named failure or lets it propagate.
    With nothing configured it returns the built-in example registry's entries (none of which carries a
    `profile`), exactly as the engine's other routes see it.

    An explicit `path` is read strictly too: a path that does not exist or parse is a `RegistryError`.
    """
    if path:
        return registry.entries_from(path)
    return registry.resolve_registry().corpora


def _is_within(child: str, parent: str) -> bool:
    """True when `child` IS `parent` or lies under it. Path-segment aware, so
    `.../acme-specs-old` does not match a root of `.../acme-specs`."""
    child = os.path.abspath(child)
    parent = os.path.abspath(parent)
    if child == parent:
        return True
    return child.startswith(parent.rstrip(os.sep) + os.sep)


def registered_profile_for_root(corpus_root: Optional[str],
                                config_path: Optional[str] = None,
                                ) -> Optional[Tuple[str, str, Tuple[str, ...]]]:
    """`(corpus_id, abs_profile_path, conflicts)` for the corpus owning `corpus_root`.

    A root may be claimed by SEVERAL corpora — in the live deployment
    `corpora/acme-specs` is a `specs` root of every project corpus. That is fine
    and expected as long as they agree on the vocabulary; the returned
    `corpus_id` is then the sorted-first claimant (deterministic) and `conflicts` is
    empty. If two claimants declare DIFFERENT profile files, `conflicts` names them
    all and the caller REPORTS it — a shared root judged by two vocabularies is a
    registry bug, not something to pick a winner for behind the caller's back.

    Matching is by containment: the registry entry with the LONGEST path that
    contains `corpus_root` wins, so a nested root is judged by its own corpus.
    """
    if not corpus_root:
        return None
    if config_path:
        corpora = load_corpus_config(config_path)
        base = registry.resolve_root(config_path)
    else:
        choice = registry.resolve_registry()           # raises RegistryError on a broken registry
        corpora, base = choice.corpora, choice.root
    if not corpora:
        return None

    best_len = -1
    claimants: List[Tuple[str, str]] = []   # (corpus_id, abs_profile_path)
    for corpus_id, spec in corpora.items():
        if not isinstance(spec, dict):
            continue
        prof = spec.get("profile")
        roots = spec.get("roots") or ([{"path": spec["root"]}] if spec.get("root") else [])
        for entry in roots:
            path = entry.get("path") if isinstance(entry, dict) else entry
            if not path:
                continue
            abs_root = path if os.path.isabs(path) else os.path.join(base, path)
            if not _is_within(corpus_root, abs_root):
                continue
            length = len(os.path.abspath(abs_root))
            if length < best_len:
                continue
            if length > best_len:
                best_len, claimants = length, []
            if prof:
                abs_prof = prof if os.path.isabs(prof) else os.path.join(base, prof)
                claimants.append((str(corpus_id), os.path.abspath(abs_prof)))

    if not claimants:
        return None
    claimants.sort()
    distinct = sorted({p for _cid, p in claimants})
    conflicts: Tuple[str, ...] = tuple(distinct) if len(distinct) > 1 else ()
    corpus_id, abs_prof = claimants[0]
    if len(claimants) > 1:
        corpus_id = ", ".join(sorted({cid for cid, _p in claimants}))
    return corpus_id, abs_prof, conflicts


def resolve_profile_ex(profile, corpus_root: Optional[str],
                       config_path: Optional[str] = None) -> ProfileResolution:
    """Resolve the effective Profile AND report how — the declared-verdict form.

    Order, most-specific first:
      1. `profile` given (path/dict/Profile)       -> `explicit-argument`
      2. the corpus registry claims `corpus_root`  -> `corpus-config`
      3. a profile file sits in `corpus_root`      -> `directory-auto-discovery`
      4. nothing                                   -> `none` (REPORTED, not silent)

    ⛔ The registry outranks auto-discovery ON PURPOSE. Auto-discovery answers "what
    file happens to be in this directory"; the registry answers "what vocabulary is
    this corpus judged against". When those disagree the registry is the declaration
    and the co-located file is an accident of layout.
    """
    if profile is not None:
        return ProfileResolution(load_profile(profile), RESOLVED_EXPLICIT,
                                 detail=None, root=corpus_root)
    if corpus_root:
        registered = registered_profile_for_root(corpus_root, config_path)
        if registered:
            corpus_id, abs_prof, conflicts = registered
            if os.path.isfile(abs_prof):
                return ProfileResolution(load_profile(abs_prof), RESOLVED_CORPUS_CONFIG,
                                         detail=corpus_id, root=corpus_root,
                                         conflict=conflicts)
            # A registered-but-missing profile is a LOUD nothing, never a quiet
            # fallthrough to whatever file the directory happens to contain.
            return ProfileResolution(
                DEFAULT_PROFILE, RESOLVED_NONE,
                detail=f"corpus {corpus_id!r} registers {abs_prof!r}, which does not exist",
                root=corpus_root, conflict=conflicts)
        found = discover_profile(corpus_root)
        if found:
            return ProfileResolution(load_profile(found), RESOLVED_AUTO_DISCOVERY,
                                     detail=os.path.basename(found), root=corpus_root)
    return ProfileResolution(DEFAULT_PROFILE, RESOLVED_NONE, detail=None, root=corpus_root)


def resolve_profile(profile, corpus_root: Optional[str]) -> Profile:
    """Resolve the effective Profile for a parse() call.

    Back-compat shim over `resolve_profile_ex` — same answer, provenance discarded.
    New callers should take the `ProfileResolution` so they can REPORT what judged
    the corpus.
    """
    return resolve_profile_ex(profile, corpus_root).profile
