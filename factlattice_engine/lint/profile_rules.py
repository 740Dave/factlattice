"""PROFILE-DRIVEN VALIDATION (V1-V11) — the consumer the profile never had.

`[PROFILE-CONTRACT]` states the gap plainly: *"profile-driven validation of prefixes,
required properties, and enum values is specified here but not yet enforced by the
shipped linter. A profile is currently documentation plus a namespace registration."*
This module is that enforcement. The live Acme profile declares ten top-level keys; two
were consumed (`domain.namespaces` -> `registered_ns`, `relationship_verbs` ->
`domain_verbs`). The other eight were inert — including `linter_guidance.notes`, a
hand-authored warning about silent property drop written INTO A FIELD NOTHING READ.

------------------------------------------------------------------------------
⛔ TWO RULES GOVERN EVERY LINE BELOW
------------------------------------------------------------------------------
1. NEVER read `profile.raw[...]`. Only the typed accessors — `prefixes` /
   `prefix_set` / `document_tags` / `enum_values(key)` / `required_properties(x)` /
   `recommended_properties(x)` / `lifecycle_stages` / `guidance_notes`. Profiles are
   moving from YAML to `.lts`; a validator coupled to the dict shape needs rewriting
   at migration. One interface, two serializations ([ACME-NARROW-WAIST]).

2. NO SECOND GRAMMAR. These validators read the PARSED graph (`ParsedCorpus.nodes`,
   `.edges`, `.coverage`) and the `Profile` interface. They do NOT re-implement
   "would this have parsed?" — SPEC §2.5 names a duplicated grammar as a silent-drop
   mechanism, and `agents/factlattice-linter.md` is the 440-line proof (its property
   table lists ten edge-bearing keys; the parser has fifteen across two tables). The
   ONE place this module needs a fact the graph does not carry — a file's frontmatter
   — it gets by CALLING THE PARSER (`factlattice.parse_text`) over the line record the
   parse already retained, never by re-deriving the frontmatter regex.

------------------------------------------------------------------------------
THE GATE: no profile -> no findings, and that is deliberate
------------------------------------------------------------------------------
Every validator here is SILENT for a corpus that loads no profile. A corpus with no
profile has declared no vocabulary, so there is nothing it has failed to honour; the
pass is opt-in by authoring a profile. Acme's corpora and this repository's own
`specs/` load none today, and stay clean here until one is authored — expected, not a
bug. Note for a later ratification: V7 (§7.1), V8 (§8.1), V9 (§1.5), V10 (§12.1) and
V11 (the `status` lifecycle-token agreement check) are CORE rules that could in
principle run profileless; they are behind the same gate on purpose, so "turning on
profile validation" is one decision a corpus makes once, not eleven rules that arrive
unannounced. V11 is the one most likely to be promoted out of the gate first — it
describes an ENGINE contract (`lts_find(status=…)`), not a domain vocabulary — and it
is behind the gate today only for consistency; flagged for the maintainer.

------------------------------------------------------------------------------
SEVERITY: warning, always, at introduction. NON-NEGOTIABLE.
------------------------------------------------------------------------------
`[LAYER-LINT]`: *"Severity is linter policy, not parser judgement… an unreferenced
node is routinely legitimate, so making it fail a gate by default would train authors
to disable the gate."* Ten validators against corpora that have never been checked
produce a large day-one count. Land at `warning`. Measure. Triage. Promote per
validator, with a recorded reason, via `lint(profile_severity={...})`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

import factlattice
from factlattice.constants import REQUIRED_FRONTMATTER as _REQUIRED_FRONTMATTER
from factlattice.profile import Profile

from ..lifecycle import (
    LIFECYCLE_PROPERTY, LIFECYCLE_PROPERTY_ALT,
    is_bare_lifecycle_token, lifecycle_token,
)

ORIGIN = "profile"

# --- finding kinds, one per validator (stable, grep-able) --------------------
KIND_UNREGISTERED_PREFIX = "unregistered_prefix"            # V1
KIND_UNREGISTERED_DOCUMENT_TAG = "unregistered_document_tag"  # V2
KIND_MISSING_REQUIRED_PROPERTY = "missing_required_property"  # V3
KIND_MISSING_RECOMMENDED_PROPERTY = "missing_recommended_property"  # V4
KIND_ENUM_VALUE_UNKNOWN = "enum_value_unknown"              # V5
KIND_LIFECYCLE_POSITION_UNKNOWN = "lifecycle_position_unknown"  # V6
KIND_CONFIDENCE_MALFORMED = "confidence_malformed"          # V7
KIND_MISSING_INVALIDATING_ASSUMPTION = "missing_invalidating_assumption"  # V8
KIND_MISSING_FRONTMATTER = "missing_frontmatter"            # V9
KIND_STATUS_NOT_LIFECYCLE_TOKEN = "status_not_lifecycle_token"  # V11

# Every kind lands at `warning`. See the module docstring — this is policy, and the
# ratchet is `lint(profile_severity={...})`, per validator, with a recorded reason.
PROFILE_SEVERITY: Dict[str, str] = {
    KIND_UNREGISTERED_PREFIX: "warning",
    KIND_UNREGISTERED_DOCUMENT_TAG: "warning",
    KIND_MISSING_REQUIRED_PROPERTY: "warning",
    KIND_MISSING_RECOMMENDED_PROPERTY: "warning",
    KIND_ENUM_VALUE_UNKNOWN: "warning",
    KIND_LIFECYCLE_POSITION_UNKNOWN: "warning",
    KIND_CONFIDENCE_MALFORMED: "warning",
    KIND_MISSING_INVALIDATING_ASSUMPTION: "warning",
    KIND_MISSING_FRONTMATTER: "warning",
    KIND_STATUS_NOT_LIFECYCLE_TOKEN: "warning",
}

# V-number -> (kind, spec clause). The report ordering, and the triage worklist's
# column headings. Kept as data so a caller can enumerate the surface without
# knowing the function names.
VALIDATORS: Tuple[Tuple[str, str, str], ...] = (
    ("V1", KIND_UNREGISTERED_PREFIX, "SPEC §10.1 prefixes / [PROFILE-CONTRACT]"),
    ("V2", KIND_UNREGISTERED_DOCUMENT_TAG, "SPEC §1.7 + profile document_tags"),
    ("V3", KIND_MISSING_REQUIRED_PROPERTY, "profile <family>.required"),
    ("V4", KIND_MISSING_RECOMMENDED_PROPERTY, "profile <family>.recommended"),
    ("V5", KIND_ENUM_VALUE_UNKNOWN, "profile enums (confidentiality, artifact_readiness, …)"),
    ("V6", KIND_LIFECYCLE_POSITION_UNKNOWN, "profile decision_lifecycle"),
    ("V7", KIND_CONFIDENCE_MALFORMED, "SPEC §7.1"),
    ("V8", KIND_MISSING_INVALIDATING_ASSUMPTION, "SPEC §8.1"),
    ("V9", KIND_MISSING_FRONTMATTER, "SPEC §1.5"),
    # V10 (low_density / SPEC §12.1) REMOVED 2026-08-17 — the density warning trained
    # authors to optimise for a token-ratio number, skewing spec output toward
    # compression at the expense of the right content (the maintainer). The V-number is left
    # RETIRED, never reused, so every prior report's numbering still reads true.
    ("V11", KIND_STATUS_NOT_LIFECYCLE_TOKEN,
     "engine agreement check — factlattice_engine/lifecycle.py"),
)

VALIDATOR_OF_KIND: Dict[str, str] = {kind: v for (v, kind, _src) in VALIDATORS}

# --- vocabulary the SPEC owns (not a profile's to declare) -------------------
# SPEC §1.7 core document type tags, verbatim. A profile registers the DOMAIN half
# (`Profile.document_tags`); this is the cross-domain half V2 unions it with.
CORE_DOCUMENT_TAGS: FrozenSet[str] = frozenset({
    "META", "SYSTEM", "SPEC", "GUIDE", "FLOW", "IMPL", "GLOSSARY", "CHARTER",
    "PROJECT", "TEST", "DELTA",
})

# SPEC §1.5 — the three RATIFIED-required frontmatter keys. `audience` is required by
# The maintainer's ratification as well as the §1.5 table.
# ⛔ IMPORTED, NOT RESTATED — see `factlattice.constants.REQUIRED_FRONTMATTER`. This rule
# and `authoring.schema.Frontmatter` held disjoint ideas of "required frontmatter", which
# made the sanctioned writer structurally incapable of satisfying the sanctioned linter.
REQUIRED_FRONTMATTER: Tuple[str, ...] = tuple(_REQUIRED_FRONTMATTER)

# SPEC §7.1 — `blast_radius` is an enum; `value` is a float 0.0-1.0 ("not an enum",
# §7.1 is explicit about that, because quality tools need to sort with granularity).
CONFIDENCE_BLAST_RADIUS: FrozenSet[str] = frozenset({"low", "medium", "high"})
CONFIDENCE_VALUE_MIN = 0.0
CONFIDENCE_VALUE_MAX = 1.0

# SPEC §8.1 — the load-bearing sub-field of `context_for_change`.
INVALIDATING_ASSUMPTION = "invalidating_assumption"
CHANGE_PROPERTY = "change"
CONTEXT_FOR_CHANGE_PROPERTY = "context_for_change"

# The property whose value V6 validates. Hyphenated is the authored form across every
# corpus in the ecosystem; the underscore spelling is accepted so a domain that
# authors it the other way is checked rather than silently skipped.
#
# ⛔ IMPORTED, NOT RESTATED. These names used to be defined here AND implied by
# `lts_find(status=…)` reading only `status` — two independent notions of "the lifecycle
# key", which is precisely how [FL-RECALL-TWO-VOCABULARIES] measured V6 validating three
# `lifecycle-position: contingent` nodes that the query layer could not see. One
# definition, in `factlattice_engine.lifecycle`, so the rule that flags the drift and the
# filter that spans it cannot disagree about what a lifecycle key is.
# (re-exported above from ..lifecycle — LIFECYCLE_PROPERTY, LIFECYCLE_PROPERTY_ALT)


@dataclass
class ProfileFinding:
    """One profile-validation finding. `validator` carries the V-number so a triage
    list can be grouped by rule without string-matching the message."""

    kind: str
    validator: str
    message: str
    source_file: Optional[str] = None
    source_line: Optional[int] = None
    node_id: Optional[str] = None
    detail: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "validator": self.validator, "message": self.message,
            "source_file": self.source_file, "source_line": self.source_line,
            "node_id": self.node_id, "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# helpers over what the PARSER produced (never over what it might have produced)
# ---------------------------------------------------------------------------
def profile_is_loaded(corpus: "factlattice.ParsedCorpus") -> bool:
    """True when the corpus actually loaded a profile.

    `DEFAULT_PROFILE` carries an empty `raw`, so emptiness is the signal. This is the
    ONE place `raw` is touched, and it is a truthiness test on the container — not a
    read of any key — so it survives the `.lts` migration unchanged (both backends
    produce the same dict, and a loaded profile is non-empty under both).
    """
    return bool(getattr(corpus, "profile", None) and corpus.profile.raw)


def _values(value: object) -> List[str]:
    """A parsed property value -> the list of scalar strings it holds.

    The parser stores a property as `str` (P1/P2/P3 inline; a repeated key becomes a
    `list[str]`) or `list[str]` (P4 nested items, taken verbatim). This flattens both
    WITHOUT interpreting the text — no splitting, no unquoting, no guessing.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    out.append(text)
        return out
    if isinstance(value, dict):
        # A P5 MAPPING holds no scalar list. Returning `str(dict)` here made V7 lint the
        # dict's REPR and report a perfectly good `value: 0.35` as "not a float".
        # Sub-fields of a mapping are read by `_subfield`, keyed.
        return []
    text = str(value).strip()
    return [text] if text else []


def _subfield(value: object, name: str) -> Optional[str]:
    """Read a `name: …` SUB-FIELD out of a property value, or None.

    ⛔ READ THIS BEFORE REUSING IT. This is not a grammar. The parser has no notion of
    a sub-field: SPEC §8.1's and §7.1's nested mappings are authored as either

        - context_for_change:              (a) P4 nested bullets -> list[str], each
          - invalidating_assumption: X          item a STRING with the sub-field name
                                                INSIDE it — not a queryable field;

        - context_for_change:              (b) the §8.1/§7.1 YAML-mapping shape, now
            invalidating_assumption: X          parsed as P5 into a real `dict` — the
                                                sub-field is a KEYED LOOKUP, no text
                                                scanning at all.

    Shape (b) WAS dark: before P5 it collected no `- item` lines, so the whole property
    was dropped and never reached this function. P5 closed that in the PARSER, which is
    where it belonged — this function does not re-derive the grammar (SPEC §2.5), it
    reads the mapping the parser now hands it.

    Shape (a) is still read the old way, by looking for the sub-field name at the head
    of an item — and note it remains strictly worse: the sub-field name lives INSIDE the
    string, so `_subfield` has to find it by prefix. Only shape (b) makes it a field.
    """
    if isinstance(value, dict):
        # KEYED, not scanned. Case-insensitive and hyphen/underscore tolerant, because
        # §7.1 writes `blast_radius` while some corpora author `blast-radius`.
        want = name.lower().replace("-", "_")
        for key, item in value.items():
            if str(key).lower().replace("-", "_") == want:
                text = str(item).strip()
                return text or None
        return None
    prefix = name.lower()
    for item in _values(value):
        text = item.strip().lstrip("*_- \t")
        if not text.lower().startswith(prefix):
            continue
        rest = text[len(prefix):].lstrip("*_ \t")
        if rest.startswith(":"):
            return rest[1:].strip()
    return None


def _has_subfield(value: object, name: str) -> bool:
    return _subfield(value, name) is not None


def _frontmatter_by_file(corpus: "factlattice.ParsedCorpus") -> Dict[str, Dict[str, str]]:
    """`source_file -> frontmatter dict`, obtained BY CALLING THE PARSER.

    Frontmatter is a per-FILE fact and `ParsedCorpus` carries only the node/edge graph
    plus the [FL-LINE-ACCOUNTING] line record — the graph index has no frontmatter
    block at all. Rather than re-derive the frontmatter regex here (a second grammar,
    SPEC §2.5), we re-run `factlattice.parse_text` over the lines the parse ALREADY
    RETAINED on `LineCoverage.lines` and read `ParsedFile.frontmatter` off it.

    Retained lines, not a re-read from disk: in `parse()`'s file-set mode the recorded
    `source_file` labels a temp directory that is deleted before the caller sees the
    result, so re-reading by label would be wrong, not merely wasteful.
    """
    out: Dict[str, Dict[str, str]] = {}
    prof = corpus.profile
    for cov in getattr(corpus, "coverage", ()) or ():
        try:
            pf = factlattice.parse_text(
                "\n".join(cov.lines), cov.source_file,
                registered_ns=prof.registered_ns, domain_verbs=prof.domain_verbs)
        except Exception:  # noqa: BLE001 - a file that cannot re-parse is already a
            continue        # parse_error diagnostic; never a second, noisier report.
        out[cov.source_file] = dict(pf.frontmatter or {})
    return out


def _bare(prefix: str) -> str:
    """`DEC-` and `DEC` are the same family. The profile authors the punctuation, the
    parser's `type_prefix` does not carry it; normalize both sides rather than make a
    caller know which side it is holding (the same rule `Profile._family_block` uses)."""
    return (prefix or "").strip().rstrip("-").upper()


def _enum_for_property(profile: Profile, key: str) -> Optional[FrozenSet[str]]:
    """The declared enum for a property key, or None if the domain declares none.

    A profile authors an enum key in `snake_case` (`artifact_readiness`) while the
    property/frontmatter key is `kebab-case` (`artifact-readiness`) — SPEC §1.5's own
    table spells it with a hyphen and Acme's profile with an underscore. Both spellings
    are asked of the accessor; `enum_values` refuses structural keys itself, so a
    property called `prefixes` cannot be mistaken for an enum here.
    """
    for candidate in (key, key.replace("-", "_"), key.replace("_", "-")):
        got = profile.enum_values(candidate)
        if got is not None:
            return got
    return None


# ---------------------------------------------------------------------------
# V6's DELIVERABLE — the query that was declared and never implemented
# ---------------------------------------------------------------------------
def lifecycle_report(corpus: "factlattice.ParsedCorpus") -> Dict[str, object]:
    """The decision-lifecycle view `decision_lifecycle` declared and nothing served.

    `[PROFILE-CONTRACT]`'s inventory called this out: the Acme profile declares
    `pipeline: [proposed, committed, validated]` plus `off_pipeline`, which is exactly
    the capability asked for and never built — *"show me decisions made but never
    implemented."* V6 CHECKS the value; this function makes the stage QUERYABLE, and
    it rides out on `LintResult.to_dict()` so the `lts_lint` MCP tool serves it with
    no change to the tool.

    Keys:
      `stages`                   the declared pipeline / off_pipeline, in order;
      `by_stage`                 declared stage -> sorted node ids at it;
      `unknown`                  undeclared value -> sorted node ids (V6's findings);
      `unstaged`                 nodes whose FAMILY REQUIRES `lifecycle-position` and
                                 that do not carry it (derived from
                                 `required_properties`, so it is the profile's own
                                 definition of "a node that must have a stage", not a
                                 guess about which prefixes are decisions);
      `made_but_not_implemented` nodes at a NON-TERMINAL pipeline stage — proposed or
                                 committed, never validated. THE query.
      `terminal_stage`           the last declared pipeline stage.
    """
    profile = corpus.profile
    pipeline, off_pipeline = profile.lifecycle_stages
    declared = list(pipeline) + list(off_pipeline)
    by_stage: Dict[str, List[str]] = {s: [] for s in declared}
    unknown: Dict[str, List[str]] = {}
    unstaged: List[str] = []

    for node in corpus.nodes:
        raw_value = None
        for key in (LIFECYCLE_PROPERTY, LIFECYCLE_PROPERTY_ALT):
            if key in node.properties:
                raw_value = node.properties[key]
                break
        if raw_value is None:
            required = profile.required_properties(node.type_prefix)
            if LIFECYCLE_PROPERTY in required or LIFECYCLE_PROPERTY_ALT in required:
                unstaged.append(node.id)
            continue
        for value in _values(raw_value):
            if value in by_stage:
                by_stage[value].append(node.id)
            else:
                unknown.setdefault(value, []).append(node.id)

    terminal = pipeline[-1] if pipeline else None
    made_not_implemented: List[str] = []
    for stage in pipeline[:-1]:
        made_not_implemented.extend(by_stage.get(stage, []))

    return {
        "stages": {"pipeline": list(pipeline), "off_pipeline": list(off_pipeline)},
        "terminal_stage": terminal,
        "by_stage": {k: sorted(set(v)) for k, v in by_stage.items()},
        "unknown": {k: sorted(set(v)) for k, v in sorted(unknown.items())},
        "unstaged": sorted(set(unstaged)),
        "made_but_not_implemented": sorted(set(made_not_implemented)),
        "counts": {
            "staged": sum(len(set(v)) for v in by_stage.values()),
            "unknown": sum(len(set(v)) for v in unknown.values()),
            "unstaged": len(set(unstaged)),
            "made_but_not_implemented": len(set(made_not_implemented)),
        },
    }


# ---------------------------------------------------------------------------
# V1 - V11
# ---------------------------------------------------------------------------
def _v1_prefixes(corpus, profile: Profile) -> List[ProfileFinding]:
    """V1 — a node's `type_prefix` is a prefix the profile registers (SPEC §10.1)."""
    registered = {_bare(p) for p in profile.prefix_set}
    if not registered:
        return []          # the domain registers no prefixes: nothing to be wrong about
    out: List[ProfileFinding] = []
    for node in corpus.nodes:
        if _bare(node.type_prefix) in registered:
            continue
        out.append(ProfileFinding(
            kind=KIND_UNREGISTERED_PREFIX, validator="V1",
            message=(f"node id prefix {node.type_prefix!r} ({node.id}) is not registered "
                     f"by profile {profile.domain_id or '<unnamed>'}; registered: "
                     f"{', '.join(sorted(registered))}"),
            source_file=node.source_file, source_line=node.line_range[0],
            node_id=node.id,
            detail={"type_prefix": node.type_prefix,
                    "registered_prefixes": sorted(registered)},
        ))
    return out


def _v2_document_tags(corpus, profile: Profile) -> List[ProfileFinding]:
    """V2 — a document's tag is a CORE tag (§1.7) or one the profile registers.

    Per FILE, not per node: `document_tag` is a file-level fact the parser replicates
    onto every node in the file, so a per-node report would multiply one authoring
    mistake by the file's node count.
    """
    allowed = CORE_DOCUMENT_TAGS | profile.document_tags
    seen: Dict[str, Tuple[str, int]] = {}
    for node in corpus.nodes:
        if not node.document_tag:
            continue
        key = f"{node.source_file}\x00{node.document_tag}"
        if key not in seen or node.line_range[0] < seen[key][1]:
            seen[key] = (node.id, node.line_range[0])
    out: List[ProfileFinding] = []
    for key in sorted(seen):
        source_file, tag = key.split("\x00", 1)
        if tag in allowed:
            continue
        node_id, line = seen[key]
        out.append(ProfileFinding(
            kind=KIND_UNREGISTERED_DOCUMENT_TAG, validator="V2",
            message=(f"document tag {tag!r} is neither a SPEC §1.7 core tag nor "
                     f"registered by profile {profile.domain_id or '<unnamed>'} "
                     f"(registered: {', '.join(sorted(profile.document_tags)) or 'none'})"),
            source_file=source_file, source_line=line, node_id=node_id,
            detail={"document_tag": tag,
                    "core_tags": sorted(CORE_DOCUMENT_TAGS),
                    "profile_tags": sorted(profile.document_tags)},
        ))
    return out


def _missing_properties(corpus, profile: Profile, kind: str, validator: str,
                        accessor) -> List[ProfileFinding]:
    """Shared body of V3/V4 — they differ only in which accessor they consult and, by
    policy, in nothing else at introduction (both `warning`; V4 is the one expected to
    be demoted or narrowed first when the day-one count is triaged)."""
    out: List[ProfileFinding] = []
    for node in corpus.nodes:
        wanted = accessor(node.type_prefix)
        if not wanted:
            continue
        for prop in sorted(wanted):
            if prop in node.properties:
                continue
            out.append(ProfileFinding(
                kind=kind, validator=validator,
                message=(f"{node.id} ({node.type_prefix}-family) is missing "
                         f"{'required' if validator == 'V3' else 'recommended'} "
                         f"property {prop!r}"),
                source_file=node.source_file, source_line=node.line_range[0],
                node_id=node.id,
                detail={"property": prop, "type_prefix": node.type_prefix,
                        "family_expects": sorted(wanted)},
            ))
    return out


def _v3_required(corpus, profile: Profile) -> List[ProfileFinding]:
    """V3 — every property the node family declares `required` is present."""
    return _missing_properties(corpus, profile, KIND_MISSING_REQUIRED_PROPERTY, "V3",
                               profile.required_properties)


def _v4_recommended(corpus, profile: Profile) -> List[ProfileFinding]:
    """V4 — every property the node family declares `recommended` is present."""
    return _missing_properties(corpus, profile, KIND_MISSING_RECOMMENDED_PROPERTY, "V4",
                               profile.recommended_properties)


def _v5_enums(corpus, profile: Profile,
              frontmatter: Dict[str, Dict[str, str]]) -> List[ProfileFinding]:
    """V5 — a property whose key names a profile-declared enum holds a declared value.

    Checked on node properties AND on frontmatter, because the two enums the ecosystem
    actually declares (`confidentiality`, `artifact_readiness`) are authored in
    frontmatter (SPEC §1.5) far more often than on a node.
    """
    out: List[ProfileFinding] = []
    for node in corpus.nodes:
        for key in sorted(node.properties):
            allowed = _enum_for_property(profile, key)
            if allowed is None:
                continue
            for value in _values(node.properties[key]):
                if value in allowed:
                    continue
                out.append(ProfileFinding(
                    kind=KIND_ENUM_VALUE_UNKNOWN, validator="V5",
                    message=(f"{node.id}: property {key!r} = {value!r} is not a declared "
                             f"value ({', '.join(sorted(allowed)) or 'none declared'})"),
                    source_file=node.source_file, source_line=node.line_range[0],
                    node_id=node.id,
                    detail={"property": key, "value": value,
                            "declared": sorted(allowed), "where": "property"},
                ))
    for source_file in sorted(frontmatter):
        fm = frontmatter[source_file]
        for key in sorted(fm):
            allowed = _enum_for_property(profile, key)
            if allowed is None:
                continue
            for value in _values(fm[key]):
                if value in allowed:
                    continue
                out.append(ProfileFinding(
                    kind=KIND_ENUM_VALUE_UNKNOWN, validator="V5",
                    message=(f"frontmatter {key!r} = {value!r} is not a declared value "
                             f"({', '.join(sorted(allowed)) or 'none declared'})"),
                    source_file=source_file, source_line=None,
                    detail={"property": key, "value": value,
                            "declared": sorted(allowed), "where": "frontmatter"},
                ))
    return out


def _v6_lifecycle(corpus, profile: Profile) -> List[ProfileFinding]:
    """V6 — `lifecycle-position` holds a stage the profile declares.

    The CHECK is here; the QUERY it unlocks is `lifecycle_report()`, which is the
    actual deliverable (see that function's docstring).
    """
    stages = profile.lifecycle_stages.all_stages
    if not stages:
        return []          # the domain declares no lifecycle: nothing to validate
    report = lifecycle_report(corpus)
    unknown: Dict[str, List[str]] = report["unknown"]  # type: ignore[assignment]
    if not unknown:
        return []
    node_by_id = {n.id: n for n in corpus.nodes}
    out: List[ProfileFinding] = []
    for value in sorted(unknown):
        for node_id in unknown[value]:
            node = node_by_id.get(node_id)
            out.append(ProfileFinding(
                kind=KIND_LIFECYCLE_POSITION_UNKNOWN, validator="V6",
                message=(f"{node_id}: lifecycle-position {value!r} is not a declared "
                         f"stage (pipeline: {', '.join(profile.lifecycle_stages.pipeline)}; "
                         f"off_pipeline: {', '.join(profile.lifecycle_stages.off_pipeline)})"),
                source_file=node.source_file if node else None,
                source_line=node.line_range[0] if node else None,
                node_id=node_id,
                detail={"value": value, "declared": sorted(stages)},
            ))
    return out


def _v7_confidence(corpus, profile: Profile) -> List[ProfileFinding]:
    """V7 — a `confidence` block conforms to SPEC §7.1.

    §7.1: `value` is a float 0.0-1.0 ("not an enum… this enables quality tools to sort
    and prioritize with granularity that an enum cannot"); `blast_radius` is
    low|medium|high. What the PARSER hands us is one of:

      * `str`       — the inline form `- confidence: 0.85`; the whole value is `value`;
      * `list[str]` — the P4 nested form; sub-fields are read per `_subfield`'s rules;
      * ABSENT      — including the §7.1 YAML-mapping shape, which collects no `- item`
                      lines and is therefore DROPPED by the parser. V7 cannot see it,
                      and does not pretend to: an absent block yields NO finding.

    `blast_radius` is validated only when present. §7.1 states a type for `value` and
    an enum for `blast_radius`; it does not mark the latter mandatory, and inventing a
    requirement the spec does not state is how a linter earns its own disablement.
    """
    out: List[ProfileFinding] = []
    for node in corpus.nodes:
        if "confidence" not in node.properties:
            continue
        raw = node.properties["confidence"]
        is_block = isinstance(raw, (list, tuple, dict))
        if is_block:
            value_text = _subfield(raw, "value")
            blast = _subfield(raw, "blast_radius") or _subfield(raw, "blast-radius")
        else:
            value_text = _values(raw)[0] if _values(raw) else None
            blast = None
        form = "block" if is_block else "inline"

        if value_text is None:
            out.append(ProfileFinding(
                kind=KIND_CONFIDENCE_MALFORMED, validator="V7",
                message=f"{node.id}: confidence block carries no `value` (SPEC §7.1)",
                source_file=node.source_file, source_line=node.line_range[0],
                node_id=node.id, detail={"problem": "value_missing", "form": form},
            ))
        else:
            try:
                numeric = float(value_text.strip().strip('"').strip("'"))
            except (TypeError, ValueError):
                numeric = None
            if numeric is None:
                out.append(ProfileFinding(
                    kind=KIND_CONFIDENCE_MALFORMED, validator="V7",
                    message=(f"{node.id}: confidence value {value_text!r} is not a float "
                             f"(SPEC §7.1: a float 0.0-1.0, deliberately not an enum)"),
                    source_file=node.source_file, source_line=node.line_range[0],
                    node_id=node.id,
                    detail={"problem": "value_not_float", "value": value_text, "form": form},
                ))
            elif not (CONFIDENCE_VALUE_MIN <= numeric <= CONFIDENCE_VALUE_MAX):
                out.append(ProfileFinding(
                    kind=KIND_CONFIDENCE_MALFORMED, validator="V7",
                    message=(f"{node.id}: confidence value {numeric} is outside "
                             f"{CONFIDENCE_VALUE_MIN}-{CONFIDENCE_VALUE_MAX} (SPEC §7.1)"),
                    source_file=node.source_file, source_line=node.line_range[0],
                    node_id=node.id,
                    detail={"problem": "value_out_of_range", "value": numeric, "form": form},
                ))
        if blast is not None and blast.strip().strip('"').strip("'") not in CONFIDENCE_BLAST_RADIUS:
            out.append(ProfileFinding(
                kind=KIND_CONFIDENCE_MALFORMED, validator="V7",
                message=(f"{node.id}: confidence blast_radius {blast!r} is not one of "
                         f"{', '.join(sorted(CONFIDENCE_BLAST_RADIUS))} (SPEC §7.1)"),
                source_file=node.source_file, source_line=node.line_range[0],
                node_id=node.id,
                detail={"problem": "blast_radius_unknown", "value": blast, "form": form},
            ))
    return out


def _v8_invalidating_assumption(corpus, profile: Profile) -> List[ProfileFinding]:
    """V8 — `change` present => `context_for_change` carries `invalidating_assumption`.

    ⛔ THIS VALIDATOR IS PARTIALLY DARK TODAY, AND THAT IS THE POINT OF SAYING SO.
    §8.1 documents `context_for_change` as a YAML MAPPING. The parser's P4 branch
    collects `  - item` lines only, so an authored mapping collects nothing and the
    property is DROPPED WHOLE — V8 cannot distinguish "the author wrote a full seven
    field context block in the documented shape" from "the author wrote nothing". The
    finding therefore names the FORM it saw:

      form=absent  the property did not reach the graph. May be a genuinely missing
                   block, or an §8.1-shaped mapping the parser dropped. INDISTINGUISHABLE
                   here — closing that is parser conformance work, not linter work.
      form=block   P4 nested bullets. Parsed to an ARRAY OF STRINGS with the sub-field
                   name inside the string, so `invalidating_assumption` is NOT a
                   queryable field; V8 reads it out of the item text (`_subfield`) and
                   marks `queryable: false` on the finding.
      form=inline  a single string; searched the same way.

    `test_v8_yaml_mapping_form_is_dark` asserts the limitation rather than papering
    over it: it authors the §8.1 shape and asserts V8 reports form=absent.
    """
    out: List[ProfileFinding] = []
    for node in corpus.nodes:
        if CHANGE_PROPERTY not in node.properties:
            continue
        if CONTEXT_FOR_CHANGE_PROPERTY not in node.properties:
            out.append(ProfileFinding(
                kind=KIND_MISSING_INVALIDATING_ASSUMPTION, validator="V8",
                message=(f"{node.id}: has `change` but no `context_for_change` in the "
                         f"parsed graph; SPEC §8.1 requires the `invalidating_assumption` "
                         f"field (NOTE: §8.1's YAML-mapping shape is dropped by the "
                         f"parser, so an authored block in that form looks like this)"),
                source_file=node.source_file, source_line=node.line_range[0],
                node_id=node.id,
                detail={"form": "absent", "queryable": False, "dark": True},
            ))
            continue
        raw = node.properties[CONTEXT_FOR_CHANGE_PROPERTY]
        form = "block" if isinstance(raw, (list, tuple)) else "inline"
        if _has_subfield(raw, INVALIDATING_ASSUMPTION):
            continue
        out.append(ProfileFinding(
            kind=KIND_MISSING_INVALIDATING_ASSUMPTION, validator="V8",
            message=(f"{node.id}: `context_for_change` carries no "
                     f"`{INVALIDATING_ASSUMPTION}` (SPEC §8.1 — the field that tells a "
                     f"future reader when to reopen the decision)"),
            source_file=node.source_file, source_line=node.line_range[0],
            node_id=node.id,
            detail={"form": form, "queryable": False, "dark": False},
        ))
    return out


def _v9_frontmatter(corpus, profile: Profile,
                    frontmatter: Dict[str, Dict[str, str]]) -> List[ProfileFinding]:
    """V9 — SPEC §1.5's required frontmatter is present.

    One finding PER MISSING KEY, not one per file: the triage unit is "this document
    needs an `audience`", and a single collapsed finding would hide two of three.
    """
    out: List[ProfileFinding] = []
    for source_file in sorted(frontmatter):
        fm = frontmatter[source_file]
        for key in REQUIRED_FRONTMATTER:
            if fm.get(key):
                continue
            out.append(ProfileFinding(
                kind=KIND_MISSING_FRONTMATTER, validator="V9",
                message=(f"frontmatter is missing required key {key!r} "
                         f"(SPEC §1.5: scope, audience, boundary)"),
                source_file=source_file, source_line=None,
                detail={"key": key, "required": list(REQUIRED_FRONTMATTER),
                        "present": sorted(fm)},
            ))
    return out


def _v11_status_token(corpus, profile: Profile) -> List[ProfileFinding]:
    """V11 — a node's `status` is a BARE lifecycle token, queryable by `lts_find`.

    ⛔ THIS IS AN AGREEMENT CHECK, and it is the load-bearing half of the status fix.

    `lts_find(status=…)` used to compare the status property by EXACT STRING while the
    corpus authored `<lifecycle-token> — <free-text qualifier>`, so the DISPATCH GATE
    ("no spec ships while an unratified `[DD-*]` remains in it"), which is implemented
    as `status=proposed`, could not see its own blockers — including a node whose status
    string literally reads "BLOCKS slice 2's scene wiring". `lifecycle.status_matches`
    now tolerates the qualified forms.

    Widening the filter ALONE would have hidden the drift one layer down: the gate would
    start working and nobody would ever learn that the corpus's status field had stopped
    being a queryable enum. So this rule makes the drift VISIBLE. It flags, per node:

      `qualified`   a real lifecycle token plus qualifier prose — `proposed — awaiting
                    ratify`, `operational (2026-07-07)`. The filter DOES match these
                    now; the finding says the value is prose, not data, and names the
                    token it will be found by.
      `untokenized` no lifecycle token could be derived AT ALL, so the value is
                    reachable ONLY by an exact-string query: `**operational (…)**`
                    (leading bold), `UNCONFIRMED. Its author …` (leading capital),
                    `spec'd; build queued` (non-boundary character after the word).
                    `lifecycle.py` documents each shape and why it refuses to guess —
                    "no token" is deliberate, but it is not free, and this names the cost.

    It reads `Node.status` — the exact field the index serves and `_node_matches`
    filters on — so the rule and the filter cannot disagree about what they are looking
    at, and it shares ONE predicate (`is_bare_lifecycle_token`) with the filter so they
    cannot disagree about what "bare" means.

    Silent for a node with no `status`: absence is V3/V4's question (is it required?),
    not this rule's (is what is there queryable?).
    """
    out: List[ProfileFinding] = []
    for node in corpus.nodes:
        raw = getattr(node, "status", None)
        if not raw or is_bare_lifecycle_token(raw):
            continue
        token = lifecycle_token(raw)
        shape = "qualified" if token else "untokenized"
        if token:
            why = (f"it carries qualifier prose after the lifecycle token {token!r}; "
                   f"`lts_find(status={token!r})` finds it, but the field is no longer "
                   f"a queryable enum")
        else:
            why = ("no lifecycle token can be derived from it (it does not begin with a "
                   "lowercase token followed by a boundary), so it is reachable ONLY by "
                   "an exact-string query")
        excerpt = raw if len(raw) <= 120 else raw[:117] + "…"
        out.append(ProfileFinding(
            kind=KIND_STATUS_NOT_LIFECYCLE_TOKEN, validator="V11",
            message=(f"{node.id}: status {excerpt!r} is not a bare lifecycle token — "
                     f"{why}"),
            source_file=node.source_file, source_line=node.line_range[0],
            node_id=node.id,
            detail={"status": raw, "lifecycle_token": token, "shape": shape,
                    "queryable_as": token},
        ))
    return out


# ---------------------------------------------------------------------------
# the pass
# ---------------------------------------------------------------------------
def find_profile_violations(corpus: "factlattice.ParsedCorpus",
                            kinds: Optional[Sequence[str]] = None) -> List[ProfileFinding]:
    """Run V1-V9 and V11 over an already-parsed corpus. Deterministic; ordered by validator.

    Returns `[]` — never raises — when the corpus loaded no profile. See the module
    docstring for why the gate exists and what it deliberately keeps switched off.
    """
    if not profile_is_loaded(corpus):
        return []
    profile = corpus.profile
    frontmatter = _frontmatter_by_file(corpus)

    findings: List[ProfileFinding] = []
    findings += _v1_prefixes(corpus, profile)
    findings += _v2_document_tags(corpus, profile)
    findings += _v3_required(corpus, profile)
    findings += _v4_recommended(corpus, profile)
    findings += _v5_enums(corpus, profile, frontmatter)
    findings += _v6_lifecycle(corpus, profile)
    findings += _v7_confidence(corpus, profile)
    findings += _v8_invalidating_assumption(corpus, profile)
    findings += _v9_frontmatter(corpus, profile, frontmatter)
    findings += _v11_status_token(corpus, profile)

    if kinds:
        keep = set(kinds)
        findings = [f for f in findings if f.kind in keep]
    return findings


def profile_findings_by_validator(findings: Sequence[ProfileFinding]) -> Dict[str, int]:
    """`{'V1': 1, 'V3': 12, …}` — the triage worklist, in validator order."""
    counts = {v: 0 for (v, _k, _s) in VALIDATORS}
    for f in findings:
        counts[f.validator] = counts.get(f.validator, 0) + 1
    return counts
