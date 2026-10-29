"""Deterministic, pure, NO-LLM FactLattice (.lts) parser.

Contract: FactLattice SPEC.md §1 (node/edge/property model), §2 (parser rules
F1/N1/N2/P1-P4/E1-E6), §1.4 (four graph layers), §9.2 (cross-domain refs); with
[LTS-PARSER]: unregistered-namespace and dangling-target edges are PRESERVED AND
FLAGGED, never dropped (FactLattice P3 / §2.4). Parsing is regular because .lts is
machine-authored (FactLattice P1).

------------------------------------------------------------------------------
GRAMMAR MAPPING (syntax -> graph), as read from SPEC.md + the real staged corpus
------------------------------------------------------------------------------
NODES
  * `# Title [DOCTYPE:ID]`  (h1)  -> N2: document_tag=DOCTYPE for the file, AND a
      document-level node id=ID (last segment). We emit the h1 as a node so that
      cross-refs of the form `[SYSTEM:SYSTEM-IDENTITY]` / `[gov.example:SYSTEM:X]`
      that address the DOCUMENT resolve instead of dangling. (Resolved ambiguity
      A1 in the report — SPEC §2.1 N1 says nodes are h2-h4; the corpus references
      the h1 doc id as an edge target, so we make it addressable.)
  * `## [ID] Title` / `### [ID] Title` / `#### [ID] Title` (h2-h4)  -> N1 node.
      The bracketed ID may appear at the START (`### [GW-PROBLEM] Title`) or END
      (`## 1. Purpose [LTS-PURPOSE]`) of the heading; both are handled.
  * A heading with NO bracketed id (`## Relationships`, `## 1. Identity Classes`)
      is a section divider, not a node. It still bounds node content + nesting.
  * type_prefix = ID text before the first hyphen (SPEC §1.1: TERM-TOKEN -> TERM).

PROPERTIES (within a node body, SPEC §2.2)
  * P1 simple    `- key: value`
  * P2 bold-key  `- **key**: value`  (bold stripped)
  * P3 multivalue`- key: [A], [B]`   (bracket ids -> array)
  * P4 nested    `- key:` + indented `  - item` lines -> array
  * P5 mapping   `- key:` + indented `  subkey: value` lines (NO bullet) -> DICT.
      SPEC §8.1 ({change, context_for_change}), §8.2 ({task, context_for_task}) and
      §7.1 (confidence) all specify their primitive AS A YAML MAPPING, and foundational
      principle P2 ("Notation Freedom") states that a YAML block is a notation and "the
      parser extracts the same abstract graph regardless of which notation was used" —
      a statement P2 says "cannot be overridden by any section below". §2.2 enumerated
      only four forms, so the mapping form matched nothing and `props[key] = items if
      items` dropped the property ENTIRELY: 14 `context_for_change` blocks authored in
      §8.1's own documented shape (5 of them in this repo's own `specs/50-decisions.lts`)
      were absent from every query while reading correct on the page. P5 closes that.
      This is a CONFORMANCE fix to §2.2, not a new notation.
      Discrimination under one `- key:` head is LEXICAL and TOTAL:
        indented `- item`              -> P4, list value      (unchanged)
        indented `subkey: value`       -> P5, mapping value   (new)
        a line indented DEEPER than the mapping's own entries, and not itself
          entry-shaped                 -> a folded CONTINUATION of the entry above it
                                          (the corpora wrap long quoted values; without
                                          this the tail of a block would be dropped,
                                          which is the very defect being closed)
        BOTH forms under one head, or a DEEPER entry-shaped line (a nested mapping,
          which is not in the model) -> a FINDING on `ParsedFile.findings`; the property
                                          is NOT set and NO line is consumed, so the
                                          coverage lint also still reports it. Never a
                                          guess: an ambiguous block yields nothing.
      P1-P4 behaviour is untouched. A P4 head whose block collects nothing is still
      recorded as nothing (see the NOTE in `_parse_properties`).
  Relationship-bearing properties (SPEC §1.3) auto-generate typed edges (E4,
  inference=property). Direction/type/layer per the SPEC §1.3 table:
    see-also->RELATED_TO(sem)  depends-on->DEPENDS_ON(sem)  implements->IMPLEMENTS(sem)
    contrast-with->CONTRAST_WITH(sem)  supersedes->SUPERSEDES(sem)  resolves->RESOLVES(sem)
    part-of->PART_OF(sem)  used-by->USED_BY(entity, REVERSED)  issuer->ISSUES(entity, REVERSED)
    tested-by->TESTED_BY(entity, REVERSED)
    canonical-node->CANONICAL(sem; legacy -> canonical node, NOT supersession; bare
    `canonical` does NOT mint)
  [LEC-RENDER] FLOW/BRANCH vocabulary (FLOW_PROPERTY_EDGES) — additive, all mapping to
  the EXISTING core temporal `BEFORE`, carrying an optional edge LABEL:
    next-step->BEFORE  converges-to->BEFORE  if-true->BEFORE "yes"
    if-false->BEFORE "no"  branches->BEFORE <per-item label>
  `condition` (the decision question) and `actor` (the swimlane) are DESCRIPTIVE —
  they are read by the render layer for shape/grouping and generate NO edges.
  All other properties are descriptive/structural — stored, no edge.

EDGES (SPEC §2.1)
  * E1/E3 Causal Graph section: lines `EDGE_TYPE: [SRC] -> [TGT]` under a
      `## Causal Graph` heading (often inside a ``` fence). Layer=causal,
      inference=authored.
  * E2 `## Relationships` section: `### <LayerName>` subheads (Semantic / Temporal
      / Causal / Entity, plus non-layer subheads like "Cross-Spec"); each edge line
      `[SRC] EDGE_TYPE [TGT] <trailing prose ignored>`, with an OPTIONAL leading
      `- ` bullet (corpora author both forms). Layer = the subhead if it names a
      canonical layer, else derived from the edge type. inference=authored.
      The VERB is core, profile-registered (`domain_verbs=`, projected from a
      profile's `relationship_verbs`), or — when it sits directly before a
      BRACKETED target — preserved and flagged `unregistered_verb` (SPEC §2.4
      preserve-and-flag). It is never dropped in silence.
  * E4 relationship-bearing properties (above). inference=property.
  * E5 structural CONTAINS from heading nesting (parent node -> child node), entity
      layer, inference=structural; also sets each node's parent_section.
  * E6 mention edges from bracketed ids in prose are DELIBERATELY NOT emitted in
      Phase 1 (SPEC §1.2 lowest priority; high-noise). Documented scope choice.
  * E7 computed edges (dynamic-link expansion) are out of Phase-1 scope.

LINE ACCOUNTING (SPEC §2.5; [FL-LINE-ACCOUNTING])
  * Every branch above RECORDS the line indices it consumed into `ParsedFile.coverage`
      (a `LineCoverage`). Pure side-output: no node, edge or property is affected.
      `factlattice.coverage` classifies what was NOT consumed as ignorable (SPEC §2.3)
      or as a silent drop, and `factlattice_engine.lint.coverage` reports the latter.
      The record lives HERE, at the branch commit points, so that it cannot drift from
      the grammar it describes.

FLAGS (never drop; [LTS-PARSER])
  * flags=['unregistered_ns'] : edge TYPE is namespaced `domain.TYPE` where `domain`
      is not core and not a registered profile (default registered set = {'core'}).
      Bare core types (IMPLEMENTS, DEPENDS_ON, ...) are valid; bare non-core types
      are treated as the FactLattice 'private' tier (preserved, NOT flagged).
  * dangling=True : the target node id is absent from the built node_map. Applied
      by the index builder in a post-parse pass (needs the whole-corpus node set);
      the parser marks a provisional per-file dangling and records cross_corpus.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .hashing import content_hash

# ---------------------------------------------------------------------------
# Core edge-type -> layer table (FactLattice SPEC §1.4 + §1.3 property table)
# ---------------------------------------------------------------------------
# NOTE: CANONICAL (minted by the `canonical-node` property key, REL_PROPERTY_EDGES) is
# DELIBERATELY not listed here, so it is not a CORE verb. It is nevertheless
# reachable by a second route: an unknown uppercase verb in a `## Relationships` triple mints as-is, so
# `[A] CANONICAL [B]` mints a CANONICAL edge (semantic layer) on base and on head alike. Only
# the KEY path dedups an identical repeated target; duplicated triples are NOT deduped, like
# every other verb. The lint (lint/canonical.py) works over edges, so it covers both routes.
CORE_EDGE_LAYER: Dict[str, str] = {
    # semantic
    "IS_A": "semantic", "PART_OF": "semantic", "HAS_PROPERTY": "semantic",
    "SPECIALIZES": "semantic", "IMPLEMENTS": "semantic", "RELATED_TO": "semantic",
    "SIMILAR_TO": "semantic", "CONTRAST_WITH": "semantic", "DEPENDS_ON": "semantic",
    "SUPERSEDES": "semantic", "RESOLVES": "semantic",
    # temporal
    "BEFORE": "temporal", "AFTER": "temporal", "SEQUENCE": "temporal",
    "CONCURRENT": "temporal", "DURATION": "temporal", "TIME_INDEXED_STATE": "temporal",
    "VALIDITY_WINDOW": "temporal", "DEADLINE": "temporal",
    # causal
    "CAUSES": "causal", "CASCADES_TO": "causal", "PRECONDITION_FOR": "causal",
    "ENABLES": "causal", "PREVENTS": "causal",
    # entity
    "ISSUES": "entity", "HOLDS": "entity", "MANAGES": "entity", "CONTAINS": "entity",
    "OWNED_BY": "entity", "REFERS_TO": "entity", "MENTIONED_IN": "entity",
    "USED_BY": "entity", "TESTED_BY": "entity",
}

CANONICAL_LAYERS = ("semantic", "temporal", "causal", "entity")

# Relationship-bearing property -> (edge_type, layer, reversed_direction)
# reversed=True means the edge runs referenced_id -> node (SPEC §1.3 Direction col).
REL_PROPERTY_EDGES: Dict[str, Tuple[str, str, bool]] = {
    "see-also": ("RELATED_TO", "semantic", False),
    "relates-to": ("RELATED_TO", "semantic", False),
    "related-to": ("RELATED_TO", "semantic", False),
    "depends-on": ("DEPENDS_ON", "semantic", False),
    "implements": ("IMPLEMENTS", "semantic", False),
    "contrast-with": ("CONTRAST_WITH", "semantic", False),
    "supersedes": ("SUPERSEDES", "semantic", False),
    "superseded-by": ("SUPERSEDES", "semantic", True),
    # 2026-10-03: a legacy node -> its CANONICAL node (same thing, non-canonical name or
    # number). NOT supersession: the legacy node's content stays valid. The KEY is
    # `canonical-node`, NOT `canonical`: bare `canonical` is already free-text status prose
    # (`canonical: true -- cutover accepted...`) on 10 acme-docs nodes and must not mint
    # (review F1). The edge TYPE name "CANONICAL" is final;
    # factlattice_engine/caution.py CANONICAL_EDGE_TYPE must equal it (SDLC 7.6).
    "canonical-node": ("CANONICAL", "semantic", False),
    "resolves": ("RESOLVES", "semantic", False),
    "part-of": ("PART_OF", "semantic", False),
    "spawns": ("CAUSES", "causal", False),
    "follows": ("BEFORE", "temporal", True),
    "used-by": ("USED_BY", "entity", True),
    "issuer": ("ISSUES", "entity", True),
    "tested-by": ("TESTED_BY", "entity", True),
    "verify": ("TESTED_BY", "entity", True),
    "verified-by": ("TESTED_BY", "entity", True),
    # 2026-10-07 (factlattice-reviewer, Spec A A-E3): the review-extension PROC graph. Prior use of BOTH
    # keys over every registered corpus root (tools/key_prior_use.py, 58 roots, 774 files): 0 uses, so
    # nothing existing starts minting. Forward edges, same corpus only (ids are partition-local):
    #   attaches-to-hook: REVPROC -> a REVHOOK node (the extension's stub of a core hook)
    #   applies-rule:     REVPROC -> REVRULE, and REVRULE -> an existing rule node (pointing, never copying)
    # Ordering within a hook is the integer `order` PROPERTY, never an edge. `applies-to` (the REVRULE
    # selector, free text) and every other spelling stays prose. Lint: lint/review_edges.py.
    "attaches-to-hook": ("ATTACHES_TO_HOOK", "semantic", False),
    "applies-rule": ("APPLIES_RULE", "semantic", False),
}

# ---------------------------------------------------------------------------
# WHY THESE SEVEN WERE ADDED (2026-08-15) — and why the table is not the whole fix
# ---------------------------------------------------------------------------
# Measured over 154 `.lts` files in the acme repo, classifying every occurrence of
# every ref-bearing property key as a PURE REFERENCE LIST (bracketed ids + separators,
# optional trailing gloss) or as PROSE THAT CITES:
#
#   relates-to     most common      the natural spelling of `see-also`. 11 files. The
#                                   defect that opened this work: every one invisible to
#                                   `lts_traverse` while `lts_lint` reported CLEAN.
#   related-to       1 pure         third spelling of the same relation; registered so the
#                                   synonym set is closed rather than two-thirds closed.
#   verify          35 pure /100%   authors' spelling of `tested-by`, which sat at ZERO
#   verified-by      8 pure /100%   uses under its official name. The expressive name is
#                                   the one an author reaches for ([FL-RECALL-PROPOSALS]).
#   spawns           5 pure /100%   an insight SPAWNS a decision — core causal CAUSES.
#   superseded-by    3 pure /100%   ⛔ REVERSED. The passive voice of `supersedes`, named
#                                   in `binding.INVERSE_VOICE` as a measured trap.
#   follows          2 pure /100%   ⛔ REVERSED. "A follows B" == B BEFORE A.
#
# ⛔ THE DISCRIMINATOR, because it is what keeps this table honest. "The value contains a
# bracketed id" is NOT the test for membership here. `rationale` (96 occurrences),
# `definition` (94) and `requirement` (58) all carry bracketed ids and are 0% pure —
# they cite node ids INSIDE SENTENCES. Registering them would turn every inline citation
# into a graph edge and destroy what a traversal means. The test is whether the VALUE IS
# A REFERENCE LIST. `tests/test_property_edge_minting.py` pins both directions.
#
# ⛔ DELIBERATELY NOT ADDED — `re-derive-via`, despite 106 occurrences. It is a THREE-layer
# failure and this table is only the first layer: its targets (`[FIXTURE-acme-source-boundary]`)
# are not id-shaped, so `_resolve_ref` rejects them, and NO `[FIXTURE-*]` node exists in any
# corpus. Registering the key would mint 1 edge of 106 and that one would dangle — a
# confident, well-formed near-zero that reads as a fix. See SPEC.md §1.3.
#
# ⛔ DELIBERATELY NOT ADDED — `exemplifies`, `upholds`, `uses`, `documents`, `realizes`.
# These are Acme DOMAIN vocabulary, not core relations. Hardcoding them here would
# reverse 5d546a9 ("the engine stops naming a deployment"). They are now registrable by
# the domain profile instead — see `Profile.property_edges` (the maintainer's ruling 2026-08-15).

# ---------------------------------------------------------------------------
# [LEC-RENDER] FLOW / BRANCH vocabulary — the sequence spine of a process flow.
# ---------------------------------------------------------------------------
# Additive companion to REL_PROPERTY_EDGES (kept a separate table so the SPEC §1.3
# table's shape/contents are untouched; both are merged at the ONE E4 call site).
# Entry = (edge_type, layer, reversed, edge_label). `edge_label`:
#   None  -> unlabelled edge
#   "*"   -> per-item label, read from the property item (`- amber: [STEP-HOLD]`)
#   str   -> fixed label for every ref in that property
#
# SEQUENCE-PROPERTY CHOICE (A-ruled, recorded here because it is load-bearing):
#   * The edge TYPE is the EXISTING core temporal `BEFORE`, NOT a new `NEXT`. `A BEFORE B`
#     already means exactly "A precedes B", is already in CORE_EDGE_LAYER + SPEC §1.4, and
#     already traversable via `lts_traverse(layers=['temporal'])`. Minting `NEXT` would be a
#     pure synonym, would need a SPEC §16 core amendment, and would SPLIT the flow spine so
#     that a corpus authored with `BEFORE` no longer renders. Reuse beats invention.
#   * The PROPERTY KEY is `next-step`, NOT `next`. `next` is ALREADY IN LIVE USE in surveyed
#     corpora as a free-text "what happens next on this project" narrative property whose
#     prose CONTAINS bracketed ids (`- next: ... arrives with the web tier ([DEC-42])`).
#     Registering `next` as edge-generating would retroactively mint temporal edges into
#     those files — i.e. existing corpora would stop parsing identically. `next-step` has
#     zero occurrences across every surveyed corpus (2026-07-23), so this table is strictly
#     additive.
FLOW_PROPERTY_EDGES: Dict[str, Tuple[str, str, bool, Optional[str]]] = {
    "next-step":    ("BEFORE", "temporal", False, None),
    "if-true":      ("BEFORE", "temporal", False, "yes"),
    "if-false":     ("BEFORE", "temporal", False, "no"),
    "branches":     ("BEFORE", "temporal", False, "*"),
    "converges-to": ("BEFORE", "temporal", False, None),
}

# ---------------------------------------------------------------------------
# [FL-VERB-PARITY] VERB-SURFACE PARITY — one vocabulary, two syntactic surfaces
# ---------------------------------------------------------------------------
# A relationship verb has TWO valid positions in `.lts`: a `## Relationships` triple
# (`[A] USES [B]`) and a property key (`- uses: [B]`). Before this table existed the two
# were served by INDEPENDENT vocabularies — the triple path consulted CORE_EDGE_LAYER +
# the profile's `domain_verbs`, while the property path consulted a HAND-WRITTEN table of
# ten keys. Measured 2026-08-13 across 80 files of a live deployment: `relates-to` (342
# refs), `documents` (44), `upholds` (17), `uses` (14) and `exemplifies` (6) were authored
# as properties, PARSED CLEANLY, and minted ZERO edges — 429 authored relationships as
# zero edges, `lts_lint`/`lts_preview` both clean, because a property that parses is not
# an error.
#
# ⭐ WHY THIS IS A TABLE-DERIVATION, NOT FIVE MORE ROWS IN REL_PROPERTY_EDGES. Adding the
# five measured verbs by hand fixes the instance and RE-ARMS THE CLASS: the sixth verb a
# profile registers is dropped again, in silence, exactly as these five were. The property
# surface is therefore DERIVED from the verb vocabulary at parse time
# (`rel_property_edge_map`), so a verb cannot be registered into one surface only — the maintainer
# ruling, 2026-09-23: "a relationship is a relationship regardless of authoring surface;
# double-registration IS the silent-loss trap."
#
# ⛔ 2026-09-23 UNIFY: this closes BOTH directions, not one. Before this pass, a verb
# registered under a profile's `relationship_verbs` (triple surface) got a property key
# for free (this table); a verb registered ONLY via a profile's `edge_types[].property_key`
# (`Profile.property_edges`, the maintainer's 2026-08-15 ruling) did NOT get a bare-triple surface —
# a profile that wanted BOTH still had to declare the SAME verb TWICE, in two separate
# blocks, and a profile author who declared only one silently lost the other. `parse_text`
# now folds a `property_edges` registration's bare (non-namespaced) edge type into the
# `domain_verbs` used for BOTH the property-key derivation below AND the triple recogniser
# (`_find_edge_type`) — see `effective_domain_verbs`. Declaring a verb on either surface now
# registers it on both.
#
# ⛔ 2026-09-23 CORRECTION (cold audit, an internal audit finding): the unify's FIRST
# landing over-corrected by ALSO deriving a property-key surface for every bare
# `CORE_EDGE_LAYER` verb unconditionally, with no purity vetting. That derivation is
# REMOVED — see the comment above `TRIPLE_ONLY_VERBS` and `rel_property_edge_map`'s own
# docstring. The unify described above (a verb registered on either PROFILE surface gets
# both) is UNCHANGED and is the real, measured recovery this pass keeps.
REL_PROPERTY_ALIASES: Dict[str, Tuple[str, bool]] = {
    # Authored property spellings that are NOT the mechanical kebab of their verb. These
    # are LEXICAL facts about how corpora write, so they are enumerated rather than
    # derived; the mechanical spelling (`related-to`) keeps working alongside them.
    #   value = (canonical verb, reversed_direction)
    "see-also":   ("RELATED_TO", False),   # SPEC §1.3's own spelling; predates this table
    "relates-to": ("RELATED_TO", False),   # the house idiom of the surveyed corpora
}

# Verbs that are DELIBERATELY triple-only: they have no property surface, and the reason
# is carried here rather than left as an absence. An unexplained gap is a conformance
# FAILURE, not a skip (`factlattice.conformance`); the only way to have a gap is to write
# down why. Removing an entry here is how you grant a verb a property surface; there is
# no third state.
#
# ---------------------------------------------------------------------------
# [2026-09-23 cold-audit correction] — HIGH-severity
# over-recognition, reproduced 3/3 before this fix landed.
# ---------------------------------------------------------------------------
# The FL-VERB-PARITY unify (6aa1a94) briefly derived a property-key surface for EVERY
# `CORE_EDGE_LAYER` verb UNCONDITIONALLY (bar the two structural exemptions above), with
# no purity vetting — unlike the manually-added `REL_PROPERTY_EDGES` entries (see-also,
# relates-to, verify, verified-by, spawns, superseded-by, follows), each measured
# 96-100% pure-reference across real corpora BEFORE being granted a surface. That
# unconditional grant minted SPURIOUS edges from common-English-word verbs used as
# ordinary prose-citation properties. Reproduced:
#   `acme-specs/73-storage.lts:310`
#       - sequence: [ACME-R-BOOT-SEQUENCE] describes a generic display device...
#   `acme-specs/84-bus-kernel.lts:44`
#   `acme-console/decision-graph-ui-architect.lts:608`
# Each is a citation INSIDE A SENTENCE, not a reference list — exactly the shape
# `REL_PROPERTY_EDGES`'s own discriminator excludes (`rationale`/`definition`/
# `requirement` are 0% pure for the identical reason). 0 edges on `main`, nonzero on
# the unify build.
#
# RULING: granting a verb a property surface is a CLAIM that its property-usage is a
# pure reference list; that claim must be VETTED, never granted unconditionally. The
# default for an unvetted core verb is TRIPLE_ONLY. `rel_property_edge_map` below no
# longer derives a property key from `CORE_EDGE_LAYER` at all — a core verb's property
# surface now comes ONLY from an explicit `REL_PROPERTY_EDGES` / `REL_PROPERTY_ALIASES`
# entry, i.e. from having ALREADY been vetted the way USES/UPHOLDS/DOCUMENTS were. The
# entries below are common-English-word core verbs that were swept up in the
# unconditional grant and had NEVER been vetted; they are named explicitly (rather than
# left as a silent absence) so the gap is reviewable and re-openable per-verb.
#
# Re-openable, per verb: MEASURE its real-corpus property-key usage purity the way
# USES/UPHOLDS/DOCUMENTS were (bracketed-ids-as-reference-list vs. prose-that-cites),
# and add it to `REL_PROPERTY_EDGES` explicitly if it clears the bar — that addition IS
# the vetting act, and it is what removes the verb from this table.
_UNVETTED_COMMON_WORD_VERB_REASON = (
    "Common-English-word core verb, swept into a property-key surface by the "
    "FL-VERB-PARITY unify's unconditional CORE_EDGE_LAYER grant with NO purity vetting "
    "(unlike USES/UPHOLDS/DOCUMENTS, each measured 96-100% pure-reference before being "
    "granted a surface). Cold audit reproduced spurious edges from exactly this class "
    "3/3 in real corpora (`- sequence: [X] describes a generic display device...` "
    "minted a false SEQUENCE edge in acme-specs/73-storage.lts:310) — a citation "
    "inside a sentence, not a reference list. the audit finding: granting a "
    "property surface is a claim that must be vetted; default is TRIPLE_ONLY until it "
    "is. Re-openable: measure this verb's real-corpus property-key purity and add it to "
    "REL_PROPERTY_EDGES explicitly — that addition IS the vetting act."
)
TRIPLE_ONLY_VERBS: Dict[str, str] = {
    "CONTAINS": (
        "E5 already mints CONTAINS from heading nesting, so the document tree IS the "
        "CONTAINS relation. A property-authored `contains:` would add a second, "
        "author-asserted containment; the two are distinguishable only by `inference` "
        "(structural vs property), and every consumer that filters on type alone would "
        "silently read a forged tree. Authors wanting 'A holds B' have core HOLDS. "
        "Re-openable: give the property form a distinct type before granting it a key."
    ),
    "HAS_PROPERTY": (
        "The property surface of HAS_PROPERTY is EVERY property line in the file. A key "
        "`has-property: [X]` would assert an edge whose target is a property NAME, which "
        "is a category error — properties are not nodes."
    ),
    # --- unvetted common-English-word core verbs (the audit finding) ---------
    "IS_A": _UNVETTED_COMMON_WORD_VERB_REASON,
    "SPECIALIZES": _UNVETTED_COMMON_WORD_VERB_REASON,
    "SIMILAR_TO": _UNVETTED_COMMON_WORD_VERB_REASON,
    "BEFORE": _UNVETTED_COMMON_WORD_VERB_REASON,
    "AFTER": _UNVETTED_COMMON_WORD_VERB_REASON,
    "SEQUENCE": _UNVETTED_COMMON_WORD_VERB_REASON,
    "CONCURRENT": _UNVETTED_COMMON_WORD_VERB_REASON,
    "DURATION": _UNVETTED_COMMON_WORD_VERB_REASON,
    "TIME_INDEXED_STATE": _UNVETTED_COMMON_WORD_VERB_REASON,
    "VALIDITY_WINDOW": _UNVETTED_COMMON_WORD_VERB_REASON,
    "DEADLINE": _UNVETTED_COMMON_WORD_VERB_REASON,
    "CAUSES": _UNVETTED_COMMON_WORD_VERB_REASON,
    "CASCADES_TO": _UNVETTED_COMMON_WORD_VERB_REASON,
    "PRECONDITION_FOR": _UNVETTED_COMMON_WORD_VERB_REASON,
    "ENABLES": _UNVETTED_COMMON_WORD_VERB_REASON,
    "PREVENTS": _UNVETTED_COMMON_WORD_VERB_REASON,
    "ISSUES": _UNVETTED_COMMON_WORD_VERB_REASON,
    "HOLDS": _UNVETTED_COMMON_WORD_VERB_REASON,
    "MANAGES": _UNVETTED_COMMON_WORD_VERB_REASON,
    "OWNED_BY": _UNVETTED_COMMON_WORD_VERB_REASON,
    "REFERS_TO": _UNVETTED_COMMON_WORD_VERB_REASON,
    "MENTIONED_IN": _UNVETTED_COMMON_WORD_VERB_REASON,
}


def verb_property_key(verb: str) -> str:
    """The property-key spelling of a relationship verb. `USES` -> `uses`.

    The ONE place the kebab convention lives, so the parser and the parity test cannot
    disagree about what a verb's property key is.
    """
    return verb.strip().lower().replace("_", "-")


def effective_domain_verbs(domain_verbs: Optional[frozenset],
                           property_edges: Optional[Dict[str, Tuple[str, str, bool]]]
                           ) -> frozenset:
    """[FL-VERB-PARITY] UNIFY: fold a profile's property-surface registrations
    (`property_edges`, from `edge_types[].property_key`) into the verb set used for
    BARE-TRIPLE recognition, so a verb registered on EITHER surface is recognised on
    BOTH.

    Only BARE (non-namespaced) edge types are folded in: `_find_edge_type`'s namespaced
    branch (`domain.TYPE`) never consults `domain_verbs` at all — a namespaced type is
    already recognised by its own dotted-segment rule — so folding a namespaced etype in
    here would be inert, not merely redundant.

    `load_profile` refuses a `property_edges` registration whose bare TYPE collides with
    a core edge type (`_edge_types_from_raw` R1), so this can never re-admit a core verb
    through the back door; it only ever adds verbs a profile itself declared.
    """
    out = set(domain_verbs or DEFAULT_DOMAIN_VERBS)
    if property_edges:
        out.update(etype for (etype, _layer, _rev) in property_edges.values()
                   if "." not in etype)
    return frozenset(out)


def rel_property_edge_map(
    domain_verbs: frozenset = None,
    profile_layers: Optional[Dict[str, str]] = None,
) -> Dict[str, Tuple[str, str, bool]]:
    """The COMPLETE property-key -> (edge_type, layer, reversed) map for one parse,
    DERIVED from the verb vocabulary in force rather than transcribed alongside it.

    ⛔ [2026-09-23 cold-audit correction] There is NO step
    that derives a property-key surface from `CORE_EDGE_LAYER` wholesale any more. An
    earlier version of this function granted every core verb a property surface
    unconditionally (bar `TRIPLE_ONLY_VERBS`'s two structural exemptions), which is a
    CLAIM — "this verb's property-usage is a pure reference list" — made with no
    vetting, and it minted spurious edges from common-English-word verbs used as
    ordinary prose-citation properties (`- sequence: [X] describes a generic display
    device...` minted a false SEQUENCE edge; see the comment block above
    `TRIPLE_ONLY_VERBS`). A core verb now gets a property surface ONLY by being
    EXPLICITLY vetted into `REL_PROPERTY_EDGES` / `REL_PROPERTY_ALIASES` — the same bar
    USES/UPHOLDS/DOCUMENTS cleared (96-100% pure-reference, measured) before this file
    granted them one. The default for an unvetted core verb is `TRIPLE_ONLY`.

    Assembled lowest-precedence first, so a curated/explicit entry always wins over a
    derived one:

      1. DERIVED from `domain_verbs`   — every verb the loaded profile registered, on
                                          EITHER surface (see `effective_domain_verbs`).
                                          This is the ONE unconditional grant, and it is
                                          deliberate: a profile author REGISTERING a verb
                                          for this corpus IS the vetting act — unlike a
                                          bare core verb, which nobody chose here.
      2. REL_PROPERTY_ALIASES          — authored spellings that are not the kebab form
      3. REL_PROPERTY_EDGES            — the curated SPEC §1.3 table (reversed
                                          directions). This is ALSO where every vetted
                                          CORE verb's own mechanical kebab spelling
                                          lives (`depends-on` -> DEPENDS_ON, `part-of`
                                          -> PART_OF, ...) — a core verb earns a property
                                          surface by getting an entry HERE, and that
                                          entry is the whole mechanism; there is no
                                          separate CORE_EDGE_LAYER-wide derivation step.

    Layer resolution goes through `_layer_for_type`, the SAME function the triple path
    uses (`_parse_section_edges`), so the two surfaces cannot assign different layers to
    the same verb.

    A caller that wants the property_edges registration itself included in the returned
    map (rather than only folded into `domain_verbs`) still applies it on top — see the
    E4 call site in `parse_text`, which is the one place both are merged.

    With no profile, `domain_verbs` is empty and step 1 contributes nothing — the
    no-profile parse gains only the curated/vetted keys (steps 2+3), which is now
    IDENTICAL in shape to the pre-FL-VERB-PARITY baseline for bare core verbs (the
    2026-08-13 change's own deliberate half, commit 8d83459) — the parity fix this
    function still delivers is confined to PROFILE-registered verbs (step 1), which is
    the actual, measured recovery (+212 edges) this unify was for.
    """
    if domain_verbs is None:
        domain_verbs = DEFAULT_DOMAIN_VERBS
    out: Dict[str, Tuple[str, str, bool]] = {}
    for verb in sorted(domain_verbs):
        if verb in TRIPLE_ONLY_VERBS:
            continue
        out[verb_property_key(verb)] = (verb, _layer_for_type(verb, profile_layers), False)
    for key, (verb, reversed_dir) in REL_PROPERTY_ALIASES.items():
        out[key] = (verb, _layer_for_type(verb, profile_layers), reversed_dir)
    out.update(REL_PROPERTY_EDGES)
    return out


# A node carrying this property is a DECISION point. It is deliberately NOT
# edge-generating — its value is the question being asked (prose), not a reference.
# The render layer turns it into a diamond; the graph is unchanged by it.
DECISION_PROPERTY = "condition"

# The optional swimlane property. Descriptive; grouping only, never an edge.
ACTOR_PROPERTY = "actor"

# Edge-type namespaces treated as registered by default. FactLattice §9: 'core.*'
# is always valid; bare core types are core. A domain profile would extend this.
DEFAULT_REGISTERED_NS = frozenset({"core"})

# Relationship verbs a profile has registered BARE (`relationship_verbs.core` +
# `relationship_verbs.domain`, e.g. Acme's USES/DOCUMENTS/UPHOLDS/REALIZES/
# EXEMPLIFIES). EMPTY by default, so a parse with no profile is byte-identical to
# the pre-fix behaviour. A bare verb is accepted iff it is core OR in this set —
# deliberately NOT "any UPPERCASE token", which would mint edges out of prose.
DEFAULT_DOMAIN_VERBS: frozenset = frozenset()

# ---------------------------------------------------------------------------
# LINE ACCOUNTING ([FL-LINE-ACCOUNTING]) — the parser's consumption record
# ---------------------------------------------------------------------------
# The parser records, per source line, WHICH branch consumed it. Nothing about the
# graph changes; this is a pure side-output. Its consumer is the coverage lint
# (`factlattice.coverage` + `factlattice_engine.lint.coverage`), which asserts the
# invariant: EVERY non-blank body line is either CONSUMED by a parser branch or
# CLASSIFIED as ignorable — anything else is authored content the structured view
# silently dropped.
#
# WHY THE PARSER OWNS THIS: the alternative is for the linter to re-derive "would
# this line have parsed?" from a second copy of the grammar. That second copy is
# exactly the drift that produced the domain-verb silent drop (the profile's
# `relationship_verbs` list was read by a reporting tool while the parser used its
# own hardcoded table, and the two disagreed for 18 days in silence). The linter
# must ASK, never re-derive.
BRANCH_FRONTMATTER_DELIM = "frontmatter_delimiter"
BRANCH_FRONTMATTER_FIELD = "frontmatter_field"
BRANCH_HEADING_NODE = "heading_node"
BRANCH_HEADING_DIVIDER = "heading_divider"
BRANCH_PROP_SIMPLE = "property_simple"        # _PROP_SIMPLE_RE  (SPEC §2.2 P1)
BRANCH_PROP_BOLD = "property_bold"            # _PROP_BOLD_RE    (SPEC §2.2 P2)
BRANCH_PROP_KEY_ONLY = "property_key_only"    # _PROP_KEY_ONLY_RE(SPEC §2.2 P4/P5 head)
BRANCH_PROP_NESTED_ITEM = "property_nested_item"  # _NESTED_ITEM_RE (P4 body)
# P5 (SPEC §8.1/§8.2/§7.1 YAML-mapping form). The entry and its wrapped continuation
# lines get DISTINCT branch names so the coverage record says which grammar read each
# line — a continuation counted as an entry would hide a real drop.
BRANCH_PROP_MAPPING_ENTRY = "property_mapping_entry"        # _NESTED_MAP_RE  (P5 body)
BRANCH_PROP_MAPPING_CONT = "property_mapping_continuation"  # folded value tail (P5)
# A WRAPPED value's tail. Same discipline as the P5 pair above and for the same
# reason: the head and the tail get DISTINCT branch names, so the coverage record
# says which grammar read each line. (WRAP) below.
BRANCH_PROP_CONT = "property_continuation"          # folded tail of a P1/P2 value
BRANCH_PROP_ITEM_CONT = "property_item_continuation"  # folded tail of a P4 list item
# ID-TABLE rows (TBL below). The three line roles are named separately so that a
# reader of the coverage record can tell a promoted table apart from a decorative
# one, and can see which line carried which row.
BRANCH_ID_TABLE_HEADER = "id_table_header"
BRANCH_ID_TABLE_RULE = "id_table_rule"        # the |---|---| separator
BRANCH_ID_TABLE_ROW = "id_table_row"
BRANCH_NODE_DEF_LIST_ITEM = "node_def_list_item"  # _LI_NODEDEF_RE
BRANCH_EDGE_RELATIONSHIPS = "edge_relationships"  # _REL_SRC_RE + _find_edge_type (E2)
BRANCH_EDGE_CAUSAL = "edge_causal"                # _CAUSAL_EDGE_RE (E1/E3)

# Named sections whose body lines have their OWN grammar (`- [SRC] VERB [TGT]` /
# `TYPE: [A] -> [B]`). The coverage classifier is section-aware because of them:
# a property-shaped check applied inside `## Relationships` fires on every
# well-formed edge triple, and a rule that warns on the corpus's own edge idiom
# gets switched off within a day.
SECTION_RELATIONSHIPS = "relationships"
SECTION_CAUSAL = "causal"

# ---------------------------------------------------------------------------
# [A1 / C10] THE FORMAT-VERSION MARKER — the mechanism the whole roadmap hangs on
# ---------------------------------------------------------------------------
# ⛔ ABSENCE IS NOT AN ERROR AND MUST NEVER BECOME ONE. Verified before this landed:
# ZERO of the live corpus files carry any version-ish frontmatter key, so an absent
# marker is the state of the ENTIRE INSTALLED BASE. Absence therefore means LEGACY —
# parse exactly as before, plus warnings — and presence is the author's explicit,
# per-document opt-in to strict vocabulary enforcement.
#
# ⛔ WHY PER-DOCUMENT AND NOT PER-CORPUS OR PER-ENGINE. A flag day is the one migration
# shape that cannot be staged: every document must be correct before any document can
# be checked. A marker in the document makes strictness a property of the DOCUMENT, so
# a corpus migrates one file at a time and a corpus that never migrates keeps working
# forever. This is CommonMark's tolerated-divergence period with a marker instead of a
# guess.
#
# ⛔ AN UNKNOWN VERSION DOES NOT SILENTLY DEGRADE TO LEGACY. `lts: "9.9"` is an author
# asserting a contract this parser does not implement; treating it as legacy would
# answer confidently under the wrong rules. It is a finding, and it is parsed strictly
# (the most conservative reading of "the author asked for more rigour than I have").
MODE_LEGACY = "legacy"
MODE_STRICT = "strict"

#: Format versions this parser implements. `None` (absent) is always accepted.
SUPPORTED_FORMAT_VERSIONS = frozenset({"1.1"})

#: The frontmatter field. Chosen over `factlattice:` because it matches the extension
#: (SPEC amendment A1; pre-integration question 6, answered — the field is uncontended
#: either way, so the tie broke on the name an author already has in their fingers).
FORMAT_VERSION_FIELD = "lts"

_FORMAT_VERSION_RE = re.compile(r"^\d+\.\d+$")

_FORMAT_VERSION_MESSAGES = {
    "format_version_empty": (
        "`lts:` frontmatter field is present but EMPTY. Parsed in LEGACY mode. This is "
        "reported rather than silently defaulted because an empty marker is an author "
        "reaching for the field and losing the value."),
    "format_version_malformed": (
        "`lts: {value}` is not a `<major>.<minor>` version. Parsed in LEGACY mode; "
        "supported: {supported}."),
    "format_version_unsupported": (
        "`lts: {value}` declares a format version this parser does not implement "
        "(supported: {supported}). Parsed in STRICT mode — the most conservative "
        "reading of an author asking for more rigour than is available. It is NOT "
        "degraded to legacy: that would answer confidently under the wrong rules."),
}


def parse_format_version(frontmatter: Dict[str, str]):
    """Read the `lts:` marker -> `(version_or_None, mode, finding_kind_or_None)`.

    Quoting is stripped: `lts: "1.1"`, `lts: '1.1'` and `lts: 1.1` are the same
    assertion, and a format whose meaning depended on YAML quoting style would be a
    silent-loss mechanism of exactly the kind this spec exists to close.
    """
    raw = frontmatter.get(FORMAT_VERSION_FIELD)
    if raw is None:
        return None, MODE_LEGACY, None
    version = str(raw).strip().strip('"').strip("'").strip()
    if not version:
        # `lts:` with nothing after it — an author reached for the marker and the
        # value was lost. NOT legacy-by-default: that would swallow the attempt.
        return None, MODE_LEGACY, "format_version_empty"
    if not _FORMAT_VERSION_RE.match(version):
        return version, MODE_LEGACY, "format_version_malformed"
    if version not in SUPPORTED_FORMAT_VERSIONS:
        return version, MODE_STRICT, "format_version_unsupported"
    return version, MODE_STRICT, None


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
# A bracketed id token: last colon-segment must look like an UPPERCASE id, so we
# do not mistake prose brackets for node ids.
_BRACKET_RE = re.compile(r"\[([A-Za-z0-9_.][A-Za-z0-9_.:\- ]*?)\]")
_UPPER_ID_RE = re.compile(r"^[A-Z][A-Z0-9_\-]*$")
# [C1 / SPEC A4] identifier-SHAPED but not a well-formed id: letters/digits/_/- only.
# Used only by `classify_bracket_token` to tell an ATTEMPTED ref (`[Mixed-case-x]`,
# `[COMMIT-09b80e9]`) apart from ordinary prose brackets (`[lowercase words]`, which
# fail this because of the space).
_MALFORMED_REF_SHAPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*$")
# A leading section ORDINAL — `8. `, `10.1 `, `2)` . Numbering, not content: a heading
# that opens `8. [SOME-ID] Title` is still the ID-FIRST form. Used only by
# `detect_id_hijack`; the heading grammar itself is indifferent to it.
_LEADING_ORDINAL_RE = re.compile(r"^\s*\d+(?:\.\d+)*[.)]?\s*")

# List-item node DEFINITION ([LTS-PARSER] NODE-EXTRACTION-COVERAGE;
# gov.example profile [CONV-INV-REGISTRY] / [CONV-LI-NODE]): a top-level list item of
# the form `- [ID]( [ID])* **bold descriptive text** ...` DEFINES the node(s). The
# distinguishing signal vs the three list-item shapes we already handle:
#   * `- [SRC] TYPE [TGT]` / `- [SRC] example.VERB <prose>` -> EDGE (a typed verb
#     token follows the id, NOT a bold span);
#   * `- key: value` -> PROPERTY (starts lowercase `key:`, never `[ID]`);
#   * `- [ID] **bold** ...` -> NODE DEF (a bold span IMMEDIATELY follows the leading
#     id-run, with no typed verb between). The `**` right after the id-run is what
#     mechanically excludes the edge case.
_LI_NODEDEF_RE = re.compile(r"^-[ \t]+((?:\[[A-Z][A-Z0-9_\-]*\][ \t]*)+)\*\*(.+?)\*\*")
_LI_LEADING_ID_RE = re.compile(r"\[([A-Z][A-Z0-9_\-]*)\]")
# [LTS-PARSER] MULTI-ID CO-DEF alias-form branch (A-ruled): an EXPLICIT alias marker in
# the def's PROSE collapses a multi-leading-id def to ONE node + aliases (vs. the default
# two-node co_defined_with). Matched only in the prose region AFTER the id list, so an id
# that itself contains "AKA" cannot false-trigger the collapse.
_ALIAS_MARKER_RE = re.compile(r"\b(?:aka|formerly|alias\s+for)\b", re.IGNORECASE)

_PROP_SIMPLE_RE = re.compile(r"^-[ \t]+([a-z][a-z0-9_\-]*)[ \t]*:[ \t]+(.+)$")
_PROP_BOLD_RE = re.compile(r"^-[ \t]+\*\*([a-z][a-z0-9_\-]*)\*\*[ \t]*:[ \t]+(.+)$")
_PROP_KEY_ONLY_RE = re.compile(r"^-[ \t]+(?:\*\*)?([a-z][a-z0-9_\-]*)(?:\*\*)?[ \t]*:[ \t]*$")
_NESTED_ITEM_RE = re.compile(r"^[ \t]+-[ \t]+(.+)$")
# P5 mapping entry: an INDENTED, UNBULLETED `subkey: value`. The key grammar is
# `_PROP_SIMPLE_RE`'s, deliberately — a mapping sub-field is a property key, and a
# second key grammar is exactly the kind of duplicate that drifts. Requiring a
# NON-EMPTY value (like P1) is what keeps `subkey:` alone out of the mapping: an
# empty sub-value is ambiguous (null? a nested block?) and this branch never guesses.
_NESTED_MAP_RE = re.compile(r"^([ \t]+)([a-z][a-z0-9_\-]*)[ \t]*:[ \t]+(.+)$")
_LEADING_WS_RE = re.compile(r"^([ \t]*)")

# (WRAP) A property value that WRAPS across physical lines. The corpora author long
# `- key: …` values as a head plus indented continuation lines; before this branch
# existed the value was truncated to its first physical line and the tail classified
# as ignorable prose, so the loss was invisible to line accounting as well
# (`corpora/acme-specs/41-…`: 61 of 61 wrapped properties truncated, coverage
# reported 100.0% / 0 unaccounted while dropping most of their text).
#
# A continuation is defined by EXCLUSION, and the exclusions are the FURNITURE shapes
# that start their own structure. Anything indented deeper than its head that is none
# of these is value text. The list is deliberately short and each entry is a shape the
# author unambiguously meant as new structure, not as a wrapped sentence:
# MEASURED: the marker class is `[-*+]` — the SAME three `_NESTED_ITEM_RE` and
# coverage's `_BULLET_RE` use, and no more. An earlier draft added the en/em dash
# (`–—`) on the theory that SL-13's mis-bullet is a bullet wherever it appears; that
# cost 19 lines on `58-document-root-and-element-inflation.lts` alone, where a
# `context_for_change` clause WRAPS onto a line beginning `— every clause was …`. A
# dash opening a wrapped sentence is punctuation, and closing the value there also
# killed the four mapping entries after it. A fourth marker class here is a data loss
# with a plausible-sounding justification.
_CONT_BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+")     # a (sub-)list item
_CONT_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]")              # a heading
_CONT_TABLE_RE = re.compile(r"^[ \t]*\|")                         # a table row
_CONT_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)")                # a code fence
_CONT_QUOTE_RE = re.compile(r"^[ \t]*>")                          # a blockquote
_CONT_ORDERED_RE = re.compile(r"^[ \t]*\d+[.)][ \t]")             # an ordered item
_CONT_RULE_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$")  # a thematic break

# Relationships-section edge line begins `- [SRC] ...`; the edge type + target are
# extracted with _find_edge_type below (real corpus lines carry prose targets and
# trailing commentary, not always a clean `[SRC] TYPE [TGT]`).
# The leading `- ` bullet is OPTIONAL: the surveyed corpora author BOTH
# `- [SRC] VERB [TGT]` and bare `[SRC] VERB [TGT]` inside `## Relationships`.
# A line only becomes an edge if a RECOGNISED verb is then found, so unbulleted
# prose beginning with a bracketed id still yields nothing.
_REL_SRC_RE = re.compile(r"^(?:-[ \t]+)?\[([A-Za-z0-9_.:\- ]+?)\][ \t]+(.*)$")
# [SL-18] The dotted part repeats. A domain namespace is not limited to ONE segment --
# TWO-SEGMENT namespaces of the shape `acme.ds` / `lib.eng` are routinely declared
# (found in a live deployed corpus, 2026-08) -- so a single optional `.seg`
# tokenized `acme.ds.GOVERNS` as TWO tokens (`acme.ds`, `GOVERNS`). The first was skipped
# (lowercase suffix), the second matched by bare membership, and the edge was emitted
# as `GOVERNS` with the NAMESPACE SILENTLY STRIPPED -- making it indistinguishable from
# any other domain's bare `GOVERNS` and voiding the P3 deconfliction guarantee, with no
# flag. Not a dropped line (nothing was lost); a SUBSTITUTED edge type, which line
# accounting is structurally blind to because the line WAS consumed.
# The branch below already handles multi-segment correctly -- `tok.split(".")[-1]` takes
# the trailing segment -- it simply never received the whole token.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*")
# An UNREGISTERED but unambiguously-authored verb: an UPPER_SNAKE token sitting
# directly between the source id and a BRACKETED target. Requiring the bracket is
# what makes this safe against prose ("[X] MUST be reused" has no `[` after MUST).
_UNREG_VERB_RE = re.compile(r"^([A-Z][A-Z0-9_]*)[ \t]+(?=\[)")
# Causal-graph line: `EDGE_TYPE: [SRC] -> [TGT]` (arrow -> or unicode →)
_CAUSAL_EDGE_RE = re.compile(
    r"^[ \t]*([A-Za-z0-9_.]+)[ \t]*:[ \t]*\[([A-Za-z0-9_.:\- ]+?)\][ \t]*(?:->|→)[ \t]*\[([A-Za-z0-9_.:\- ]+?)\]"
)


@dataclass
class Node:
    id: str
    type_prefix: str
    title: str
    domain: Optional[str]
    source_file: str
    line_range: Tuple[int, int]
    content_hash: str
    token_count: int
    status: Optional[str]
    properties: Dict[str, object]
    parent_section: Optional[str] = None
    document_tag: Optional[str] = None
    # Provenance of the node's extraction: 'header' (h1-h4) or 'list_item'
    # (`- [ID] **bold**` node-def, [LTS-PARSER] NODE-EXTRACTION-COVERAGE).
    definition_form: str = "header"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type_prefix": self.type_prefix,
            "title": self.title,
            "domain": self.domain,
            "source_file": self.source_file,
            "line_range": [self.line_range[0], self.line_range[1]],
            "content_hash": self.content_hash,
            "token_count": self.token_count,
            "status": self.status,
            "properties": self.properties,
            "parent_section": self.parent_section,
            "document_tag": self.document_tag,
            "definition_form": self.definition_form,
        }


@dataclass
class Edge:
    source: str
    target: str
    type: str
    layer: str
    inference: str
    source_file: str
    source_line: int
    # Preserve+flag machinery ([LTS-PARSER]); never dropped.
    flags: List[str] = field(default_factory=list)
    dangling: bool = False
    target_domain: Optional[str] = None  # cross-corpus qualifier if the ref had one
    # [LEC-RENDER] optional edge label ('yes'/'no'/a branch name). Set only by the
    # FLOW_PROPERTY_EDGES vocabulary; OMITTED from to_dict when None so every
    # pre-existing corpus serializes to a BYTE-IDENTICAL graph_index.json.
    label: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "source": self.source,
            "target": self.target,
            "type": self.type,
            "layer": self.layer,
            "inference": self.inference,
            "source_file": self.source_file,
            "source_line": self.source_line,
            "flags": self.flags,
            "dangling": self.dangling,
            "target_domain": self.target_domain,
        }
        if self.label is not None:
            d["label"] = self.label
        return d


# ---------------------------------------------------------------------------
# PARSE FINDINGS — an AMBIGUOUS block is reported, never resolved by guessing
# ---------------------------------------------------------------------------
# The P5 (§8.1 YAML-mapping) branch must discriminate list-vs-mapping LEXICALLY and
# TOTALLY. Two shapes are genuinely ambiguous and both are reported here rather than
# resolved:
#   * MIXED       — indented `- item` bullets AND unbulleted `subkey: value` entries
#                   under ONE `- key:` head. In YAML that is a sequence and a mapping
#                   at the same level, which is invalid; picking either reading would
#                   silently discard the other half.
#   * NESTED MAP  — an entry-shaped line indented DEEPER than the mapping's own entry
#                   column. A property value in this model is scalar | list | mapping,
#                   with no recursion, so this is either a mapping-of-mappings (not in
#                   the model) or a mis-indented sibling. Both need the author, not a
#                   parser heuristic.
# On either finding the property is NOT set and NO line is consumed, so the block also
# stays visible to the [FL-LINE-ACCOUNTING] coverage lint. Two independent alarms for
# one shape is deliberate: the whole class of bug this closes is one that was audible
# in neither channel.
FINDING_MIXED_PROPERTY_BLOCK = "mixed_property_block"
FINDING_NESTED_MAPPING_BLOCK = "nested_mapping_block"


@dataclass
class ParseFinding:
    """One structural ambiguity the parser refused to resolve. Additive side-output:
    no node, edge or property is affected by its presence."""

    kind: str
    message: str
    source_file: Optional[str] = None
    source_line: Optional[int] = None   # 1-based, the `- key:` head
    node_id: Optional[str] = None
    key: Optional[str] = None
    detail: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "message": self.message,
            "source_file": self.source_file,
            "source_line": self.source_line,
            "node_id": self.node_id,
            "key": self.key,
            "detail": self.detail,
        }


@dataclass
class LineCoverage:
    """Per-file LINE ACCOUNTING record ([FL-LINE-ACCOUNTING]) — a pure side-output.

    `consumed` maps a 0-based line index to the NAME of the parser branch that
    consumed it (`BRANCH_*` above). A line index absent from `consumed` was read by
    no branch: it is either legitimately ignorable (blank / heading-free prose /
    fenced code / a table / a hand-drawn diagram — SPEC §2.3) or it is authored
    content the structured view dropped in silence. Deciding which is
    `factlattice.coverage.classify_line`'s job, not the parser's — the parser states
    only the fact of consumption, never the judgement.

    `lines` is retained so a consumer can classify without re-reading the file. That
    matters: in `corpus.parse()`'s file-set mode the recorded `source_file` is a
    basename inside a temp dir that is deleted before the caller sees the result, so
    re-reading from the label is not merely wasteful, it is wrong.
    """

    source_file: str
    lines: List[str]
    body_start: int                 # first line index after frontmatter
    consumed: Dict[int, str] = field(default_factory=dict)
    fenced: Set[int] = field(default_factory=set)      # inside ``` / ~~~ (incl. delimiters)
    section: Dict[int, str] = field(default_factory=dict)  # idx -> SECTION_* (only if in one)
    # idx -> the node id whose body `_parse_properties` was actually run over. A line
    # ABSENT here was never offered to the property grammar at all — which is a
    # materially different (and more alarming) failure than "offered and rejected",
    # and the classifier needs to be able to say which.
    node_body: Dict[int, str] = field(default_factory=dict)
    # (TBL) Lines of a table the ID-TABLE discriminator ACCEPTS but which sits under a
    # heading carrying no `[ID]`, so there is no node to attach the rows to. The parser
    # states the fact; `coverage.classify_line` turns it into the finding. Recorded
    # here rather than re-derived in the linter for the same reason `consumed` is —
    # a second copy of the discriminator would drift from this one.
    id_table_unattached: Set[int] = field(default_factory=set)

    def record(self, idx: int, branch: str) -> None:
        """First writer wins, so the record is deterministic under the parser's
        fixed branch order (properties -> list-item node defs -> section edges)."""
        if 0 <= idx < len(self.lines) and idx not in self.consumed:
            self.consumed[idx] = branch

    def unconsumed_indices(self) -> List[int]:
        return [i for i in range(len(self.lines)) if i not in self.consumed]

    def to_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "line_count": len(self.lines),
            "body_start": self.body_start,
            "consumed": {str(k): v for k, v in sorted(self.consumed.items())},
            "fenced": sorted(self.fenced),
            "section": {str(k): v for k, v in sorted(self.section.items())},
        }


@dataclass
class ParsedFile:
    source_file: str
    domain: Optional[str]
    document_tag: Optional[str]
    nodes: List[Node]
    edges: List[Edge]
    frontmatter: Dict[str, str]
    # Line-accounting side-output ([FL-LINE-ACCOUNTING]). Additive and optional so
    # every existing consumer of ParsedFile is untouched.
    coverage: Optional[LineCoverage] = None
    # Structural ambiguities the parser refused to guess at (P5 discrimination).
    # Additive, empty for every corpus that has none — which today is all of them.
    findings: List[ParseFinding] = field(default_factory=list)
    # [A1 / C10] The document's declared format version, verbatim, and the MODE the
    # parse ran in. `format_version is None` == the unstamped installed base == legacy.
    # Additive: every pre-C10 consumer of ParsedFile is untouched.
    format_version: Optional[str] = None
    mode: str = MODE_LEGACY
    # [A1/A2/A3] VOCABULARY diagnostics — version marker, undeclared keys, unregistered
    # verbs.
    #
    # ⛔ A SEPARATE CHANNEL FROM `findings`, DELIBERATELY. `findings` means "the parser
    # met a structure it refused to GUESS at" and its established contract is that a
    # structurally clean document yields `[]`. Vocabulary judgement is a different
    # question about a document that parsed perfectly well, and `legacy_document` fires
    # on EVERY unstamped file — i.e. on the entire installed base. Merging the two would
    # have made `findings` non-empty for every document in existence, destroying the one
    # signal that channel carries, and the tests that assert `findings == []` are what
    # caught the attempt. A1's own words are "MUST warn `legacy_document` once per file
    # AT LINT": the parser supplies the fact, the linter decides the severity.
    vocabulary_findings: List[ParseFinding] = field(default_factory=list)


# ---------------------------------------------------------------------------
# THE PROFILE-DOCUMENT SIGNAL — intrinsic, and it travels with the file
# ---------------------------------------------------------------------------
# A `.lts` domain profile is a document parsed by the parser it configures. It is
# VOCABULARY, not CONTENT: its `PREFIX-*` / `EDGETYPE-*` / `DOCTAG-*` nodes are how the
# profile represents itself under the ratified `{domain}-profile.lts` one-node-per-
# registered-item encoding — they are the dictionary, not an entry in the book.
#
# ⛔ WHY THE FILE'S OWN DOCTAG, NOT `corpus-config.json`.
# Registration answers a DIFFERENT question ("which profile governs which corpus") and
# it can be WRONG — `private/corpus-config.json` carried `profile: None` for all eleven
# corpora for weeks. Had content-exclusion depended on registration, a mis-registered
# profile would have silently become content, which is exactly what happened: 88
# declaration nodes answered content queries in `acme-console` and generated 88
# permanent `dangle.orphan` warnings (a dictionary is cited by nothing, because content
# does not cite its dictionary).
#
# The doctag is intrinsic: it survives relocation, copying, and a wrong config. The two
# signals are still cross-checked — see
# `factlattice_engine.analytics.profile_registration.audit_profile_registration`, which
# reports their DISAGREEMENT as a finding rather than silently preferring one.
#
# ⛔ EXCLUDED FROM CONTENT, NOT UNPARSED. The file is still parsed in full, and it is
# still loaded AS a profile through `factlattice.profile.load_profile` (an entirely
# separate reader). Only its contribution to the CONTENT partition is dropped, and it is
# dropped LOUDLY: `build_index` records every skipped file under the index's
# `profile_documents` key and `stats["profile_documents"]`.
PROFILE_DOCUMENT_TAG = "PROFILE"


def is_profile_document(parsed: object) -> bool:
    """True iff this parsed file declares itself a domain profile (`document_tag`
    == `PROFILE`). Accepts a `ParsedFile`, a `Node`, or any node dict — the parser
    replicates `document_tag` onto every node it emits, so the same predicate answers
    at file level and node level."""
    if isinstance(parsed, dict):
        tag = parsed.get("document_tag")
    else:
        tag = getattr(parsed, "document_tag", None)
    return bool(tag) and str(tag).strip().upper() == PROFILE_DOCUMENT_TAG


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def token_estimate(text: str) -> int:
    """Deterministic token estimate (~4 chars/token). No external tokenizer so the
    parser stays pure and dependency-free; used only for budget management.

    PUBLIC on purpose. `Node.token_count` is produced here, and the engine's
    [ENGINE-BUDGET] slicer spends against that number — so anything that has to cost an
    item the budget will later measure (lint findings, for one) must use THIS function
    and not a same-looking twin. Two estimators that agree today are two estimators.
    """
    return max(1, round(len(text) / 4))


# Historical private name, kept as an alias so no in-tree caller forks off it.
_token_estimate = token_estimate


def classify_bracket_token(content: str) -> str:
    """[C1] SPEC A4's partition on ONE bracket token's LAST colon-segment, trimmed:
    `REF | MALFORMED-REF | PROSE-BRACKET`. PUBLIC and used by `_resolve_ref` (via the
    `REF` case), the malformed-ref finding emitters below, `binding.is_pure_reference_list`
    and the lint layer -- ONE classifier, so the parser and every consumer of it can
    never disagree about what a ref-shaped bracket is (the same discipline
    `heading_id_candidates` documents for the id grammar).

        REF            the segment matches the well-formed id grammar (`_UPPER_ID_RE`)
        MALFORMED-REF  identifier-shaped (letters/digits/_/- only, per
                       `_MALFORMED_REF_SHAPE_RE`), NOT a well-formed id, and carries a
                       "-"/"_" or an UPPER-initial character -- an ATTEMPTED ref that
                       is not simply a lowercase word (`[relates-to]` would NOT fire
                       this test alone if it had no upper/dash signal at all; in
                       practice every measured instance carries the dash of a
                       multi-segment id or a leading capital, e.g. `[Mixed-Case-P1]`,
                       `[COMMIT-09b80e9]`).
        PROSE-BRACKET  everything else `_BRACKET_RE` accepts (a bracket containing a
                       space, e.g. `[lowercase words]`, is prose by construction).
    """
    seg = content.strip().split(":")[-1].strip()
    if _UPPER_ID_RE.match(seg):
        return "REF"
    if _MALFORMED_REF_SHAPE_RE.match(seg) and (
            "-" in seg or "_" in seg or (seg[:1].isalpha() and seg[:1].isupper())):
        return "MALFORMED-REF"
    return "PROSE-BRACKET"


# [C1] the ONE new ParseFinding kind Era 0 adds. Additive side-output exactly like
# FINDING_MIXED_PROPERTY_BLOCK / FINDING_NESTED_MAPPING_BLOCK above: no node, edge or
# property is affected by its presence, so emitting it changes NO existing parse
# result -- the Era 0 compatibility contract (PARSER-ENGINEERING-SPEC.md §7).
FINDING_MALFORMED_REF = "malformed_ref"


def _malformed_ref_finding(*, raw: str, position: str, source_file: Optional[str],
                           source_line: int, node_id: Optional[str] = None,
                           key: Optional[str] = None) -> "ParseFinding":
    return ParseFinding(
        kind=FINDING_MALFORMED_REF,
        message=(f"`[{raw}]` at {position} is bracket-SHAPED like an id but is not a "
                 f"well-formed one (`{_UPPER_ID_RE.pattern}`) -- SPEC A4 MALFORMED-REF. "
                 f"It neither became a graph reference nor raised a diagnostic before "
                 f"this rule; it is stored/skipped exactly as if it were prose."),
        source_file=source_file, source_line=source_line, node_id=node_id, key=key,
        detail={"position": position, "raw": raw},
    )


def _scan_value_for_malformed_refs(value: object, *, position: str,
                                   source_file: Optional[str], source_line: int,
                                   node_id: Optional[str], key: Optional[str],
                                   findings: List["ParseFinding"]) -> None:
    """[C1] Scan a property VALUE (str or list[str]) for MALFORMED-REF bracket tokens
    and append one finding per hit. Additive: does not touch `value` or the node."""
    items = value if isinstance(value, list) else [value]
    for item in items:
        if not isinstance(item, str):
            continue
        for m in _BRACKET_RE.finditer(item):
            if classify_bracket_token(m.group(1)) == "MALFORMED-REF":
                findings.append(_malformed_ref_finding(
                    raw=m.group(1), position=position, source_file=source_file,
                    source_line=source_line, node_id=node_id, key=key))


def _resolve_ref(bracket_content: str) -> Optional[Tuple[Optional[str], str]]:
    """Resolve a bracketed reference to (domain_or_None, node_id) per SPEC §9.2.

    Forms: `[NODE-ID]`, `[DOCTYPE:NODE-ID]`, `[domain:DOCTYPE:NODE-ID]`.
    The domain qualifier is the leading segment containing a dot (gov.example).
    Returns None if the last segment is not an uppercase id (i.e. prose bracket).
    """
    raw = bracket_content.strip()
    segments = [s.strip() for s in raw.split(":")]
    node_id = segments[-1]
    if not _UPPER_ID_RE.match(node_id):
        return None
    domain = None
    if len(segments) >= 2 and "." in segments[0]:
        domain = segments[0]
    return domain, node_id


def _extract_refs(value: str) -> List[Tuple[Optional[str], str]]:
    """All well-formed [ ... ] references in a property value, in order."""
    refs: List[Tuple[Optional[str], str]] = []
    for m in _BRACKET_RE.finditer(value):
        r = _resolve_ref(m.group(1))
        if r is not None:
            refs.append(r)
    return refs


def _heading_id(text: str) -> Optional[Tuple[str, Optional[str], str]]:
    """From a heading's text, extract (node_id, doctype_or_None, title).

    Handles ID-first `[GW-PROBLEM] Title`, ID-last `1. Purpose [LTS-PURPOSE]`,
    and doctype `[SYSTEM:SYSTEM-IDENTITY]` forms. Returns None if no id-like bracket.
    """
    best: Optional[Tuple[str, Optional[str], str, Tuple[int, int]]] = None
    for m in _BRACKET_RE.finditer(text):
        content = m.group(1).strip()
        segments = [s.strip() for s in content.split(":")]
        node_id = segments[-1]
        if not _UPPER_ID_RE.match(node_id):
            continue
        doctype = None
        if len(segments) == 2 and "." not in segments[0]:
            doctype = segments[0]
        elif len(segments) >= 3:
            doctype = segments[-2]
        title = (text[: m.start()] + text[m.end():]).strip()
        best = (node_id, doctype, title, (m.start(), m.end()))
        break  # first id-like bracket wins
    if best is None:
        return None
    return best[0], best[1], best[2]


def heading_id_candidates(text: str) -> List[Tuple[str, Optional[str], int, int]]:
    """EVERY id-shaped bracket in a heading, in order: `(node_id, doctype, start, end)`.

    `_heading_id` breaks at the FIRST id-like bracket, which is the grammar and is not
    changing. This exposes the ones it never looked at, so a CONSUMER can ask whether the
    author's intended id was one of them. PUBLIC and side-effect-free: the parser owns
    the grammar, the linter owns the policy, and a second bracket grammar in the linter
    is how the two start disagreeing about what an id is.
    """
    out: List[Tuple[str, Optional[str], int, int]] = []
    for m in _BRACKET_RE.finditer(text):
        content = m.group(1).strip()
        segments = [s.strip() for s in content.split(":")]
        node_id = segments[-1]
        if not _UPPER_ID_RE.match(node_id):
            continue
        doctype = None
        if len(segments) == 2 and "." not in segments[0]:
            doctype = segments[0]
        elif len(segments) >= 3:
            doctype = segments[-2]
        out.append((node_id, doctype, m.start(), m.end()))
    return out


def detect_id_hijack(text: str) -> Optional[Dict[str, object]]:
    """Is this heading's node id a CITATION that hijacked the author's intended id?

    ⛔ A SILENT-LOSS CLASS NONE OF THE PROPOSAL DOCUMENTS NAMES, reproduced in isolation
    2026-08-13. The heading grammar takes the FIRST bracketed token as the id, so

        ## 8. Independent replication of [FLCD-CONNECTIVITY], on a second corpus,
             by an author who did not know it existed [FL-RECALL-REPLICATION]

    mints `FLCD-CONNECTIVITY`, leaves the INTENDED `[FL-RECALL-REPLICATION]` inside the
    title STRING where nothing can reference it, and then COLLIDES with the real
    `FLCD-CONNECTIVITY` defined in another file — where first-root-wins drops the whole
    section out of the graph.

    ⇒ THE DOCUMENT ARGUING THAT THE GRAPH IS LESS CONNECTED THAN IT LOOKS HAS A SECTION
      THAT IS NOT IN THE GRAPH. This is a SUBSTITUTION, not a loss: the node exists, it
      is queryable, and it is the wrong node — which is worse, because it looks
      well-formed.

    THE PREDICATE, AND EVERY CLAUSE IN IT WAS BOUGHT WITH A MEASURED FALSE POSITIVE.
    A first cut — "more than one id-shaped bracket and the last one is terminal" — fired
    several times across a set of live `.lts` files, and most were the legitimate ID-FIRST form:

        [ACME-WIDGET-CONFIRM] See It Run (the confirmation half of [ACME-GADGET-REPORT])

    which declares its id at the START and cites at the end, exactly as intended. So:

      (a) MORE THAN ONE id-shaped bracket — otherwise there is nothing to confuse;
      (b) the LAST one is TERMINAL (only whitespace/trailing punctuation after it) —
          ID-LAST is a first-class declaration form, so a terminal id-bracket is a
          declaration;
      (c) the FIRST one is NOT at the heading START (a leading `8. ` / `10.1 ` ordinal
          does not count as content) — because ID-FIRST is ALSO a first-class form, and
          a heading that opens with its id has already declared it;
      (d) there is real PROSE between the first bracket and the last — which is what
          makes the first one a CITATION rather than a co-declaration. Two adjacent ids
          at the end (`… the picket line [A] · [B]`) is a different shape: the author
          declared two and the grammar mints one. It is a real loss and it is reported
          as `multi_id`, not as a hijack, because calling it a hijack would name the
          wrong cause and send the reader to fix the wrong thing.

    A heading that merely mentions two ids mid-sentence (`## Migrating [OLD] to [NEW]
    safely`) does NOT fire at all: neither reading is more likely than the other, and a
    detector that guesses is a detector that gets switched off.

    Returns None, or `{shape, minted, intended, candidates}` with `shape` in
    `{"citation_hijack", "multi_id"}`.
    """
    cands = heading_id_candidates(text)
    if len(cands) < 2:
        return None
    minted_id, _mdoc, first_start, first_end = cands[0]
    last_id, last_doc, last_start, last_end = cands[-1]
    if text[last_end:].strip(" \t.,;:)*_`"):
        return None                       # the last id is mid-sentence: not a declaration
    # (c) an ID-FIRST heading has already declared its id. A leading ordinal (`8. `,
    # `10.1 `) is numbering, not content, so it does not push the id out of first place.
    if not _LEADING_ORDINAL_RE.sub("", text[:first_start]).strip(" \t#*_`"):
        return None
    between = text[first_end:last_start]
    shape = "citation_hijack" if between.strip(" \t·,;—–-|/") else "multi_id"
    return {
        "shape": shape,
        "minted": minted_id,
        "intended": last_id,
        "intended_doctype": last_doc,
        "candidates": [c[0] for c in cands],
    }


def _layer_for_type(edge_type: str, profile_layers: Optional[Dict[str, str]] = None) -> str:
    """Layer for an edge type, PROFILE-FIRST (D5 per-domain layer table).

    Resolution order:
      1. the profile's `{namespaced_type -> layer}` map, if the fully-namespaced type
         is declared there (e.g. `dec.DEPENDS_ON -> causal`);
      2. else the CORE table on the bare suffix (bare core types unchanged);
      3. else `semantic` (default).
    With `profile_layers` None/empty this is byte-identical to the pre-D5 behaviour."""
    if profile_layers:
        declared = profile_layers.get(edge_type)
        if declared is not None:
            return declared
    bare = edge_type.split(".")[-1]
    return CORE_EDGE_LAYER.get(bare, "semantic")


def _find_edge_type(text: str, domain_verbs: frozenset = DEFAULT_DOMAIN_VERBS):
    """Find the first edge-type token in `text`: a namespaced `domain.TYPE`, a bare
    core edge type, or a bare PROFILE-REGISTERED verb.
    Returns (edge_type, end_offset, recognised) or None.

    Real Relationships lines are not always `[SRC] TYPE [TGT]` — a profile edge
    like `example.GATES constitutional acts` carries a PROSE target, and some lines
    put words between source and the typed verb. We locate the typed verb
    positionally rather than assuming it is the first token."""
    # ANCHORED FORM FIRST. An UPPER_SNAKE token sitting directly between the source
    # id and a BRACKETED target is unambiguously the authored verb, so it wins over
    # the positional scan below. This ordering is what kills a FABRICATION class:
    # on `[DD-X] DOCUMENTS [ACME-TERM-SUPERSEDES-55]` the scan-first order skipped the
    # unrecognised DOCUMENTS and matched `SUPERSEDES` as a SUBSTRING inside the
    # TARGET's node id, emitting an edge to the literal string "-55]" -- a wrong edge
    # is worse than a missing one, and line accounting cannot see it (the line WAS
    # consumed). Recognition is still decided by membership, so a core or
    # profile-registered verb here is unflagged exactly as before.
    #
    # (Deviation from the original proposal diff, which put this fallback AFTER the
    # scan and therefore left the fabrication in place for any corpus parsing without
    # a profile. Behaviour is unchanged for every line whose verb is at position 0,
    # which is the overwhelming majority; a line with words between the source and
    # the verb still falls through to the scan below.)
    mu = _UNREG_VERB_RE.match(text)
    if mu is not None:
        tok = mu.group(1)
        return tok, mu.end(1), (tok in CORE_EDGE_LAYER or tok in domain_verbs)

    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if "." in tok:
            # namespaced edge type: the segment after the dot is UPPERCASE
            # (example.GATES). A DOMAIN qualifier (gov.example, ops.example) is lowercase
            # after the dot and must NOT be mistaken for an edge type.
            suffix = tok.split(".")[-1]
            if _UPPER_ID_RE.match(suffix):
                return tok, m.end(), True
        elif tok in CORE_EDGE_LAYER or tok in domain_verbs:
            # ROOT CAUSE #1: a BARE verb was previously accepted ONLY if core, so
            # every verb a profile registered under `relationship_verbs.domain`
            # (USES / DOCUMENTS / UPHOLDS / REALIZES / EXEMPLIFIES) fell through
            # to `return None` and its whole line was discarded.
            return tok, m.end(), True
    return None


# A verb-SHAPED token: UPPER_SNAKE, at least two characters so a stray `A` or an
# initial does not read as an attempted verb. This is deliberately NOT the same regex
# as `_UNREG_VERB_RE` — that one requires a bracket ahead, and requiring a bracket is
# precisely what made the prose-target case invisible (C13).
_VERB_SHAPE_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")

# Words that are UPPER_SNAKE-shaped but are emphasis or acronyms, not attempted verbs.
# Kept SMALL and enumerated: an open-ended exclusion list would quietly re-silence the
# class this diagnostic exists to expose, one entry at a time.
_NOT_A_VERB = frozenset({"NOT", "AND", "THE", "BUT", "ONLY", "NEVER", "ALWAYS",
                         "MUST", "SHOULD", "MAY", "TODO", "FIXME", "NOTE", "WARNING"})


def _flag_unrecognized_verb_line(remainder: str, source_file: str, source_line: int,
                                 node_id: Optional[str], strict: bool,
                                 findings: List["ParseFinding"]) -> None:
    """[C13 / A3] Name a `## Relationships` line whose verb resolved to nothing.

    ⛔ IT REPORTS ONLY WHEN THERE IS A VERB-SHAPED TOKEN TO REPORT. A Relationships
    line that is genuinely prose (`- [DEC-X] is the reason we stopped`) carries no
    UPPER_SNAKE token and produces NO finding — otherwise this would fire on every
    explanatory line in the section and be switched off within a day, which is the
    documented fate of every shape-heuristic in this parser's history.
    """
    for m in _VERB_SHAPE_RE.finditer(remainder):
        tok = m.group(1)
        if tok in _NOT_A_VERB:
            continue
        findings.append(ParseFinding(
            kind="unrecognized_verb_line",
            message=(f"`{tok}` is verb-shaped and sits in a `## Relationships` line, "
                     f"but it is neither a core edge type nor profile-registered, and "
                     f"its target is not bracketed — so the WHOLE LINE mints nothing "
                     f"and, before this diagnostic, did so silently. Register the verb "
                     f"or bracket the target."),
            source_file=source_file, source_line=source_line, node_id=node_id,
            detail={"verb": tok, "severity": "error" if strict else "warning",
                    "mode": MODE_STRICT if strict else MODE_LEGACY,
                    "line": remainder.strip()[:120]},
        ))
        return   # one finding per line: the first verb-shaped token IS the attempt


def _type_flags(edge_type: str, registered_ns: frozenset) -> List[str]:
    """FactLattice §2.4 namespace handling -> flags. Only namespaced types whose
    namespace is unregistered are flagged; bare types are core/private (unflagged)."""
    if "." in edge_type:
        ns = edge_type.split(".")[0]
        if ns not in registered_ns:
            return ["unregistered_ns"]
    return []


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def _coerce_config(config, registered_ns, profile_layers, domain_verbs,
                   property_edges):
    """Resolve the (config | loose kwargs) pair into ONE `ParserConfig`. [C3]

    ⛔ SUPPLYING BOTH IS AN ERROR, NOT A MERGE. A silently-merged pair is how V5 stayed
    invisible: the call site LOOKED like it had passed the vocabulary while the value
    that actually reached the parse came from somewhere else. Refusing names the
    ambiguity at the one place a human is reading.
    """
    from .parser_config import ParserConfig  # local: parser_config imports this module

    loose = (registered_ns is not None or profile_layers is not None
             or domain_verbs is not None or property_edges is not None)
    if config is not None:
        if loose:
            raise TypeError(
                "pass EITHER config=ParserConfig(...) OR the loose vocabulary kwargs "
                "(registered_ns/profile_layers/domain_verbs/property_edges), never "
                "both — a merged pair hides which value reached the parse (review V5)")
        return config
    return ParserConfig.from_kwargs(
        registered_ns=registered_ns, profile_layers=profile_layers,
        domain_verbs=domain_verbs, property_edges=property_edges)


def parse_file(path: str, registered_ns: Optional[frozenset] = None,
               rel_source_file: Optional[str] = None,
               profile_layers: Optional[Dict[str, str]] = None,
               domain_verbs: Optional[frozenset] = None,
               property_edges: Optional[Dict[str, Tuple[str, str, bool]]] = None,
               config: Optional["object"] = None,
               ) -> ParsedFile:
    """Parse one .lts file into its nodes + typed edges. Pure and deterministic.

    `rel_source_file` overrides the source_file label recorded on nodes/edges
    (the index builder passes the corpus-relative path); defaults to `path`.

    ⛔ `config=` (a `factlattice.parser_config.ParserConfig`) is THE supported way to
    supply vocabulary as of Era 1/C3. The loose `registered_ns=` / `profile_layers=` /
    `domain_verbs=` / `property_edges=` keywords are RETAINED for one deprecation
    release (this is a public repo; removal is semver-major) and are equivalent to
    `config=ParserConfig.from_kwargs(...)`. They are what produced review V5: a hook
    added to this signature and to no call site fails nothing, because a missing
    keyword argument is indistinguishable from a default.
    """
    from .hashing import safe_read_text      # never blocks on a FIFO/socket/device ([ISSUE-NONREGULAR-MEMBER])
    text = safe_read_text(path, rel_source_file)
    return parse_text(text, rel_source_file or path,
                      config=_coerce_config(config, registered_ns, profile_layers,
                                            domain_verbs, property_edges))


def parse_text(text: str, source_file: str,
               registered_ns: Optional[frozenset] = None,
               profile_layers: Optional[Dict[str, str]] = None,
               domain_verbs: Optional[frozenset] = None,
               property_edges: Optional[Dict[str, Tuple[str, str, bool]]] = None,
               config: Optional["object"] = None,
               ) -> ParsedFile:
    """Parse raw .lts text (testable without a file). Deterministic.

    ⛔ THE INVARIANT (C3): the result is a function of `(text, config)` and nothing
    else. The ENTRY POINT IS NOT AN INPUT — see `factlattice.parser_config` for why
    that sentence is the whole point, and
    `factlattice_engine.selfcheck.entry_point_equivalence` for the falsifier.

    `config.property_edges` is the DOMAIN PROFILE's registered edge-bearing property
    keys (`{key: (edge_type, layer, reversed)}`), merged OVER the built-in table at the
    one E4 call site. Empty == byte-identical to the pre-ruling behaviour. A profile
    CANNOT reach a built-in key through here — `load_profile` refuses that collision at
    load time, so by the time a map arrives it is disjoint from `REL_PROPERTY_EDGES` /
    `FLOW_PROPERTY_EDGES`.
    """
    cfg = _coerce_config(config, registered_ns, profile_layers, domain_verbs,
                         property_edges)
    registered_ns = cfg.registered_ns
    profile_layers = cfg.profile_layers or None
    domain_verbs = cfg.domain_verbs
    property_edges = cfg.property_edges or None
    # [FL-VERB-PARITY] UNIFY: a verb registered on EITHER surface is recognised on BOTH.
    # Fold `property_edges`' bare edge types (a profile's `edge_types[].property_key`
    # registration) into the verb set used for triple recognition, so a verb a profile
    # registered ONLY for the property surface still parses as a bare `[A] VERB [B]`
    # triple — the double-registration gap this pass closes. See
    # `effective_domain_verbs`.
    domain_verbs = effective_domain_verbs(domain_verbs, property_edges)
    lines = text.splitlines()
    n = len(lines)

    # --- F1: frontmatter (leading --- ... --- block) -----------------------
    frontmatter: Dict[str, str] = {}
    body_start = 0
    fm_delims: List[int] = []
    fm_fields: List[int] = []
    if lines and lines[0].strip() == "---":
        fm_delims.append(0)
        for i in range(1, n):
            if lines[i].strip() == "---":
                body_start = i + 1
                fm_delims.append(i)
                break
            m = re.match(r"^([a-z][a-z0-9_\-]*)[ \t]*:[ \t]*(.*)$", lines[i])
            if m:
                frontmatter[m.group(1)] = m.group(2).strip()
                fm_fields.append(i)
    domain = frontmatter.get("domain") or None

    # --- [A1 / C10] format version -> parse MODE ---------------------------
    # Derived BEFORE any body branch runs, because every strict-mode severity keys off
    # it. `strict_default` lets a conformance case drive the strict path over an
    # unstamped fixture; it is False everywhere in production and an unstamped document
    # is therefore LEGACY, which is the state of the entire installed base.
    format_version, mode, _fv_finding = parse_format_version(frontmatter)
    if mode == MODE_LEGACY and format_version is None and cfg.strict_default:
        mode = MODE_STRICT
    strict = (mode == MODE_STRICT)

    # --- [FL-LINE-ACCOUNTING] consumption record (pure side-output) --------
    coverage = LineCoverage(source_file=source_file, lines=lines, body_start=body_start)
    for i in fm_delims:
        coverage.record(i, BRANCH_FRONTMATTER_DELIM)
    for i in fm_fields:
        coverage.record(i, BRANCH_FRONTMATTER_FIELD)

    # [C1] declared here (rather than beside `nodes` below) so the heading loop can
    # append to it too -- see the `heading id-candidates` scan just below.
    findings: List[ParseFinding] = []
    # [A1/A2/A3] the vocabulary channel — see `ParsedFile.vocabulary_findings` for why
    # this is NOT `findings`.
    vocab: List[ParseFinding] = []

    # [A1 / C10] the marker's own diagnostics. Emitted for EVERY document, including
    # legacy ones — `legacy_document` is how an operator counts the un-migrated tail,
    # and per A1 it is a WARNING that a parser MUST NOT refuse.
    if _fv_finding is not None:
        vocab.append(ParseFinding(
            kind=_fv_finding,
            message=_FORMAT_VERSION_MESSAGES[_fv_finding].format(
                value=frontmatter.get(FORMAT_VERSION_FIELD),
                supported=", ".join(sorted(SUPPORTED_FORMAT_VERSIONS))),
            source_file=source_file, source_line=1,
            detail={"declared": frontmatter.get(FORMAT_VERSION_FIELD),
                    "supported": sorted(SUPPORTED_FORMAT_VERSIONS),
                    "mode": mode, "severity": "warning"},
        ))
    elif format_version is None:
        vocab.append(ParseFinding(
            kind="legacy_document",
            message=(f"no `{FORMAT_VERSION_FIELD}:` frontmatter marker — parsed in "
                     f"LEGACY mode (today's descriptive behaviour plus warnings). "
                     f"Add `{FORMAT_VERSION_FIELD}: \"1.1\"` to opt this document into "
                     f"strict vocabulary enforcement (SPEC §1.5 / amendment A1)."),
            source_file=source_file, source_line=1,
            detail={"mode": MODE_LEGACY, "severity": "warning"},
        ))

    # --- collect headings --------------------------------------------------
    # Each heading: (line_idx, level, node_id_or_None, doctype_or_None, title)
    headings: List[Tuple[int, int, Optional[str], Optional[str], str]] = []
    in_fence = False
    for i in range(body_start, n):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            coverage.fenced.add(i)
            continue
        if in_fence:
            coverage.fenced.add(i)
            continue
        hm = _HEADING_RE.match(line)
        if not hm:
            continue
        level = len(hm.group(1))
        htext = hm.group(2)
        parsed = _heading_id(htext)
        if parsed is None:
            headings.append((i, level, None, None, htext.strip()))
            coverage.record(i, BRANCH_HEADING_DIVIDER)
            # [C1] SPEC A4 "heading id-candidates" position, V7's class: a heading
            # that mints NO node because its only bracket(s) are ill-formed silently
            # demotes to a divider. Scan for the ATTEMPT so it is a finding, not
            # silence. (A heading that DID mint an id is not re-scanned here: its
            # other brackets are prose-citation territory, out of Era 0 scope --
            # SPEC.md §1.2 E6.)
            _scan_value_for_malformed_refs(
                htext, position="heading", source_file=source_file,
                source_line=i + 1, node_id=None, key=None, findings=findings)
        else:
            node_id, doctype, title = parsed
            headings.append((i, level, node_id, doctype, title))
            coverage.record(i, BRANCH_HEADING_NODE)

    # Document tag from the first h1 that carries a doctype.
    document_tag: Optional[str] = None
    for (_i, level, _nid, doctype, _title) in headings:
        if level == 1 and doctype:
            document_tag = doctype
            break

    # --- build nodes (headings that carry an id) ---------------------------
    nodes: List[Node] = []
    node_index_by_heading: Dict[int, int] = {}  # heading position -> nodes[] index
    # Content of a node spans from its heading line to the line before the next
    # heading of ANY level (node or divider).
    heading_line_idxs = [h[0] for h in headings]

    for hpos, (line_idx, level, node_id, doctype, title) in enumerate(headings):
        if node_id is None:
            continue
        # content end = next heading line (exclusive), else EOF
        end_idx = n
        for later in heading_line_idxs:
            if later > line_idx:
                end_idx = later
                break
        content_lines = lines[line_idx:end_idx]
        content_text = "\n".join(content_lines)
        # parent_section: nearest ancestor heading with an id and lower level
        parent_section = None
        for prev in range(hpos - 1, -1, -1):
            p_level = headings[prev][1]
            p_id = headings[prev][2]
            if p_level < level and p_id is not None:
                parent_section = p_id
                break
        for _b in range(line_idx, end_idx):
            coverage.node_body.setdefault(_b, node_id)
        props = _parse_properties(content_lines, coverage=coverage, offset=line_idx,
                                  findings=findings, source_file=source_file,
                                  node_id=node_id)
        # (TBL) ID tables in this node's body become ONE mapping property. Run AFTER
        # the property grammar so an authored `- id_table:` property (none exists in
        # any surveyed corpus, but the model permits it) always wins over the derived
        # one — a derived value must never overwrite something the author wrote.
        id_rows = _parse_id_tables(content_lines, coverage=coverage, offset=line_idx)
        if id_rows and ID_TABLE_PROPERTY not in props:
            props[ID_TABLE_PROPERTY] = id_rows
        status = None
        if isinstance(props.get("status"), str):
            status = props["status"]
        node = Node(
            id=node_id,
            type_prefix=node_id.split("-")[0],
            title=title,
            domain=domain,
            source_file=source_file,
            line_range=(line_idx + 1, end_idx),  # 1-indexed inclusive
            content_hash=content_hash(content_text),
            token_count=_token_estimate(content_text),
            status=status,
            properties=props,
            parent_section=parent_section,
            document_tag=(doctype if level == 1 else document_tag),
        )
        node_index_by_heading[hpos] = len(nodes)
        nodes.append(node)

    # (TBL) Whole-file pass, AFTER every node body is known: an ID table under an
    # untagged heading lands on its nearest `[ID]`-bearing ANCESTOR; one with no such
    # ancestor is recorded so the classifier can report it rather than let it pass as
    # ignorable furniture.
    _attach_orphan_id_tables(lines, coverage, headings,
                             {nd.id: nd for nd in nodes})

    # --- N-LI: list-item node DEFINITIONS (`- [ID] **bold**`) --------------
    # Extracted in addition to header nodes so that refs to list-defined ids
    # (INV-/PULSE-/WATCH-/…) RESOLVE instead of dangling. definition_form marks
    # them auditably distinct from header nodes.
    li_contains: List[Tuple[str, str, int]] = []  # (parent_id, child_id, line_1idx)
    in_fence = False
    for i in range(body_start, n):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _LI_NODEDEF_RE.match(line)
        if not m:
            continue
        lead_ids = _LI_LEADING_ID_RE.findall(m.group(1))
        if not lead_ids:
            continue
        coverage.record(i, BRANCH_NODE_DEF_LIST_ITEM)
        title = m.group(2).strip()
        # parent_section = nearest preceding heading that carries an id
        parent_section = None
        for (h_line, _h_level, h_id, _dt, _t) in headings:
            if h_line >= i:
                break
            if h_id is not None:
                parent_section = h_id
        ch = content_hash(line)
        tok = _token_estimate(line)
        # ALIAS-FORM COLLAPSE ([LTS-PARSER] MULTI-ID CO-DEF, second branch, A-ruled):
        # a multi-leading-id def whose PROSE (region after the id list, so an "AKA"-bearing id
        # can't false-trigger) carries an EXPLICIT alias marker (aka / formerly / alias for)
        # collapses to ONE node (the FIRST id) with the other id(s) recorded as ALIASES — so a
        # ref to either resolves to the one node (alias resolution wired in index_builder).
        # Absent the marker, the default two-node co_defined_with path (below) is UNCHANGED.
        if len(lead_ids) > 1 and _ALIAS_MARKER_RE.search(line[m.end(1):]):
            canonical = lead_ids[0]
            nodes.append(Node(
                id=canonical,
                type_prefix=canonical.split("-")[0],
                title=title,
                domain=domain,
                source_file=source_file,
                line_range=(i + 1, i + 1),
                content_hash=ch,
                token_count=tok,
                status=None,
                properties={"aliases": [x for x in lead_ids if x != canonical]},
                parent_section=parent_section,
                document_tag=document_tag,
                definition_form="list_item",
            ))
            if parent_section:
                li_contains.append((parent_section, canonical, i + 1))
            continue
        for lead_id in lead_ids:
            props: Dict[str, object] = {}
            if len(lead_ids) > 1:
                # Ambiguous multi-id node-def (e.g. `- [INV-W-4] [INV-W-8] **…**`):
                # co-defined siblings sharing one statement. FLAGGED, not guessed —
                # each id becomes a resolvable node; report notes the shape.
                props["co_defined_with"] = [x for x in lead_ids if x != lead_id]
            nodes.append(Node(
                id=lead_id,
                type_prefix=lead_id.split("-")[0],
                title=title,
                domain=domain,
                source_file=source_file,
                line_range=(i + 1, i + 1),
                content_hash=ch,
                token_count=tok,
                status=None,
                properties=props,
                parent_section=parent_section,
                document_tag=document_tag,
                definition_form="list_item",
            ))
            if parent_section:
                li_contains.append((parent_section, lead_id, i + 1))

    edges: List[Edge] = []

    # --- E4: relationship-bearing property edges ---------------------------
    # [FL-VERB-PARITY] the key map is DERIVED per parse from the verb vocabulary in
    # force (core verbs + this parse's `domain_verbs`, ALREADY UNIFIED above with any
    # `property_edges` registration — see `effective_domain_verbs`), not read from a
    # hand-written table alone — so a verb registered for triples has a property
    # surface by construction, and vice versa. `property_edges` is applied LAST so an
    # EXPLICIT profile registration (which may use a non-kebab spelling) always wins;
    # the two are guaranteed disjoint from the built-in table (`load_profile` refuses
    # a built-in collision), so this can never SUBSTITUTE a built-in's meaning.
    _rel_edges = rel_property_edge_map(domain_verbs, profile_layers)
    if property_edges:
        _rel_edges.update(property_edges)
    for hpos, ni in node_index_by_heading.items():
        node = nodes[ni]
        line_idx = headings[hpos][0]
        for key, (etype, layer, reversed_dir) in _rel_edges.items():
            if key not in node.properties:
                continue
            _seen_canon = set()
            for (tdomain, tid) in _property_refs(node.properties[key]):
                if etype == "CANONICAL":      # an identical repeated target is ONE edge
                    if (tdomain, tid) in _seen_canon:
                        continue
                    _seen_canon.add((tdomain, tid))
                src, tgt = (tid, node.id) if reversed_dir else (node.id, tid)
                edges.append(Edge(
                    source=src, target=tgt, type=etype, layer=layer,
                    inference="property", source_file=source_file,
                    source_line=line_idx + 1, flags=[], dangling=False,
                    target_domain=tdomain,
                ))
        # --- E4b: [LEC-RENDER] flow/branch vocabulary (labelled sequence edges) ---
        # Same E4 mechanism, same inference='property'; the only addition is the
        # optional edge label that makes a branch renderable as a labelled arrow.
        for key, (etype, layer, reversed_dir, label_spec) in FLOW_PROPERTY_EDGES.items():
            if key not in node.properties:
                continue
            for (tdomain, tid, label) in _labelled_property_refs(
                    node.properties[key], label_spec):
                src, tgt = (tid, node.id) if reversed_dir else (node.id, tid)
                edges.append(Edge(
                    source=src, target=tgt, type=etype, layer=layer,
                    inference="property", source_file=source_file,
                    source_line=line_idx + 1, flags=[], dangling=False,
                    target_domain=tdomain, label=label,
                ))
        # [C1] SPEC A4 "minting-key values" ref-expecting position: scan every
        # MINTING key's value (built-in + FLOW + this parse's profile-registered
        # `property_edges`) for a MALFORMED-REF bracket the two loops above silently
        # passed over (`_property_refs`/`_extract_refs` only harvest well-formed
        # REFs). Red test: `- relates-to: [Mixed-case-x]` -> one finding, zero
        # edges. Additive: `edges` is untouched by this block.
        _minting_keys_here = dict(_rel_edges)
        _minting_keys_here.update(FLOW_PROPERTY_EDGES)
        for key in _minting_keys_here:
            if key not in node.properties:
                continue
            _scan_value_for_malformed_refs(
                node.properties[key], position="property value",
                source_file=source_file, source_line=line_idx + 1,
                node_id=node.id, key=key, findings=findings)

    # --- E5: structural CONTAINS edges (entity, structural) ----------------
    for hpos, ni in node_index_by_heading.items():
        node = nodes[ni]
        if node.parent_section:
            edges.append(Edge(
                source=node.parent_section, target=node.id, type="CONTAINS",
                layer="entity", inference="structural", source_file=source_file,
                source_line=headings[hpos][0] + 1, flags=[], dangling=False,
            ))

    # E5 for list-item nodes: parent section CONTAINS the list-defined node.
    for (parent_id, child_id, ln) in li_contains:
        edges.append(Edge(
            source=parent_id, target=child_id, type="CONTAINS",
            layer="entity", inference="structural", source_file=source_file,
            source_line=ln, flags=[], dangling=False,
        ))

    # --- E1/E3 (Causal Graph) + E2 (Relationships) section edges -----------
    edges.extend(_parse_section_edges(lines, body_start, headings, source_file,
                                      registered_ns, profile_layers,
                                      domain_verbs=domain_verbs, coverage=coverage,
                                      findings=findings, strict=strict, vocab=vocab))

    # [A2 / C11] key-vocabulary diagnostics, and [A3 / C13] the verb regime, are
    # emitted AFTER the whole file is parsed: both need the node set, and neither
    # changes a single node, edge or property in either mode. Severity is what the
    # mode decides — SPEC §1.5's entire contract.
    _check_key_vocabulary(nodes, cfg, strict, source_file, vocab)

    return ParsedFile(
        source_file=source_file, domain=domain, document_tag=document_tag,
        nodes=nodes, edges=edges, frontmatter=frontmatter, coverage=coverage,
        findings=findings, format_version=format_version, mode=mode,
        vocabulary_findings=vocab,
    )


# ---------------------------------------------------------------------------
# [A2 / C11] KEY VOCABULARY — DECLARED-OR-MARKED
# ---------------------------------------------------------------------------
# THE REGIME, in one line: a property key in a strict document must be CORE,
# PROFILE-DECLARED, or EXPLICITLY PRIVATE (`x-`). Anything else is an attempt that
# failed, and the maintainer's words for it are "recognized as an ATTEMPT ... and be REJECTED".
#
# ⛔ WHAT THIS AMENDS, stated plainly because it is an identity change and not a bug
# fix. P3 grants a PRIVATE TIER ("bare names — local use, no registration needed") and
# P5 says "don't architect vocabulary upfront". The measured 3,177-distinct / 2,655-
# hapax key population is those principles WORKING AS WRITTEN — machine authors doing
# what P5 told them to, minus the registry sweep that was supposed to close the loop.
# So the open key space is not indiscipline the parser failed to catch, and A2 is a
# deliberate amendment to P3/P5-as-applied-to-keys, dated and attributed. It could not
# honestly ship as a bug fix.
#
# ⛔ THE `x-` VALVE IS WHAT MAKES THE REGIME AFFORDABLE. Hard-rejecting the tail would
# damage 738 of 2,691 nodes and destroy 2,655 authored assertions. Marking them
# preserves every value and every node, costs no author time (the rewrite is
# mechanical), and — the actual point — makes a deliberate one-off DISTINGUISHABLE
# FROM A TYPO, which is the one thing today's bare private tier cannot do.
#
# ⛔ NOTHING HERE CHANGES A PARSE. No property is dropped, renamed, or refused in
# EITHER mode; an undeclared key still parses and still holds its value. What the mode
# decides is the SEVERITY of the finding. That is the entire A1 contract, and it is
# what lets a strict document be adopted one file at a time.

_KEY_VOCAB_KIND = "undeclared_key"


def _check_key_vocabulary(nodes, cfg, strict: bool, source_file: str,
                          findings: List[ParseFinding]) -> None:
    """Emit `undeclared_key` findings per A2. Never mutates a node or a property."""
    from .core_keys import core_keys, is_private_key, suggest_keys, suggestion_excluded_keys

    declared = frozenset(core_keys()) | frozenset(cfg.declared_property_keys or ())
    declared |= frozenset(cfg.property_edges or {})
    # membership uses `declared`; fuzzy suggestions use `pool` (CANONICAL keys excluded, F1)
    pool = declared - suggestion_excluded_keys()
    severity = "error" if strict else "warning"

    for node in nodes:
        for key in (node.properties or {}):
            if key in declared or is_private_key(key):
                continue
            if key == ID_TABLE_PROPERTY or key == "co_defined_with":
                continue     # parser-synthesised, never authored
            findings.append(_UndeclaredKeyFinding._lazy(
                key=key, declared=pool, strict=strict, source_file=source_file,
                source_line=getattr(node, "line_range", (None, None))[0],
                node_id=getattr(node, "id", None)))


class _UndeclaredKeyFinding(ParseFinding):
    """[PERF-LAZY-KEY-SUGGEST] An `undeclared_key` ParseFinding whose `message` and `detail`
    (the did-you-mean suggestions) are computed on FIRST ACCESS, then stored, and are the same
    values the eager code produced. Plain parsing (build_store, engine start) never reads them;
    lint, selfcheck and writer receipts do. Measured 2026-10-02 (the architect): 5.2 of 8.2 s of a
    37-corpus build_store() went to suggestions nobody read. It compares and prints as a plain
    ParseFinding."""

    # The dataclass __init__ is inherited, so `dataclasses.replace` (and any caller building one
    # from fields) gets an ordinary, fully materialised finding. Only `_lazy` defers.
    @classmethod
    def _lazy(cls, *, key, declared, strict, source_file, source_line, node_id):
        self = cls.__new__(cls)
        self.kind = _KEY_VOCAB_KIND
        self.source_file, self.source_line, self.node_id, self.key = (
            source_file, source_line, node_id, key)
        self._pending = (declared, strict)
        return self

    def __getattr__(self, name):              # only reached while message/detail are unset
        if name not in ("message", "detail"):
            raise AttributeError(name)
        d = self.__dict__
        pending = d.get("_pending")           # read BEFORE re-checking: the writer pops it only
        if name in d:                         # after storing both values, so pending is None
            return d[name]                    # implies they are already here
        if pending is None:
            raise AttributeError(name)
        from .core_keys import suggest_keys
        declared, strict = pending
        key = self.key
        near = suggest_keys(key, declared)
        if near:
            advice = ("did you mean " +
                      " / ".join(f"`{c}`" for c in near[:3]) + "?")
        else:
            advice = (f"declare it in the domain profile, mark it private as "
                      f"`x-{key}`, or rewrite it")
        # Racing first accesses compute the SAME values. setdefault is a single dict operation:
        # the first writer wins, so every reader gets the same `detail` object and a value a
        # caller set before the first read is never overwritten. Both are stored before the
        # idempotent pop, so no reader can see a half-built finding or raise.
        d.setdefault("detail", {"suggestions": near, "severity": "error" if strict else "warning",
                                "mode": MODE_STRICT if strict else MODE_LEGACY})
        d.setdefault("message", f"property key `{key}` is neither a core key, nor declared by "
                                f"the domain profile, nor marked private (`x-`) — {advice}")
        d.pop("_pending", None)
        return d[name]

    def _fields(self):
        return (self.kind, self.message, self.source_file, self.source_line,
                self.node_id, self.key, self.detail)

    def __eq__(self, other):
        if isinstance(other, ParseFinding):
            return self._fields() == (other.kind, other.message, other.source_file,
                                      other.source_line, other.node_id, other.key, other.detail)
        return NotImplemented

    __hash__ = None

    def __repr__(self):
        return ParseFinding.__repr__(self).replace(type(self).__qualname__, "ParseFinding", 1)


def _property_refs(value: object) -> List[Tuple[Optional[str], str]]:
    """Extract [domain?, id] refs from a property value (str or list).

    A P5 MAPPING value yields NOTHING, deliberately. §8.2's `context_for_task` carries
    a `depends_on:` SUB-FIELD, and §8.1's fields quote node ids inside prose; harvesting
    refs out of a mapping would mint edges that no corpus authored, from a sub-key that
    merely SHARES a name with a relationship-bearing property. The five-corpus before/
    after diff (identical edge counts and edge-type sets) is the receipt for this."""
    out: List[Tuple[Optional[str], str]] = []
    if isinstance(value, str):
        out.extend(_extract_refs(value))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                out.extend(_extract_refs(item))
    return out


# A per-item branch label: `- amber: [STEP-HOLD]` inside a P4-nested `branches:` list.
# The label is the text BEFORE the colon; it must not itself contain a bracket (so a
# prose item that merely happens to contain a colon after a ref cannot be misread).
_BRANCH_LABEL_RE = re.compile(r"^([^\[\]:]{1,60}?)[ \t]*:[ \t]*(?=.*\[)")


def _labelled_property_refs(value: object,
                            label_spec: Optional[str]) -> List[Tuple[Optional[str], str, Optional[str]]]:
    """[LEC-RENDER] refs + their edge LABEL from a flow/branch property value.

    `label_spec` is FLOW_PROPERTY_EDGES' 4th element:
      None -> every ref unlabelled;
      "*"  -> per-item label harvested from the item's own `label: [REF]` prefix
              (the P4-nested multi-way `branches:` form); an item with no such
              prefix yields an UNLABELLED edge rather than a guessed one;
      str  -> that fixed label on every ref (`if-true` -> "yes").
    """
    items: List[str] = []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = [i for i in value if isinstance(i, str)]
    out: List[Tuple[Optional[str], str, Optional[str]]] = []
    for item in items:
        label: Optional[str] = label_spec
        if label_spec == "*":
            m = _BRANCH_LABEL_RE.match(item.strip())
            label = m.group(1).strip() if m else None
        for (tdomain, tid) in _extract_refs(item):
            out.append((tdomain, tid, label))
    return out


def _indent_width(line: str) -> int:
    """Leading-whitespace width, tabs expanded to 4. Used ONLY to compare a line's
    indent against the mapping's own entry column, never to decide what a line is."""
    return len(_LEADING_WS_RE.match(line).group(1).expandtabs(4))


def _starts_new_structure(line: str) -> bool:
    """Does this line OPEN its own structure rather than continue a value? (WRAP)

    The ONE predicate behind all three continuation branches — scalar (P1/P2), list
    item (P4) and mapping entry (P5). Keeping it in one function is the point: three
    copies of "what ends a wrapped value" is three chances to disagree, and a wrapped
    value that ends in one grammar but not another is a silent drop with a plausible
    alibi.
    """
    return bool(
        _CONT_BULLET_RE.match(line)
        or _CONT_HEADING_RE.match(line)
        or _CONT_TABLE_RE.match(line)
        or _CONT_FENCE_RE.match(line)
        or _CONT_QUOTE_RE.match(line)
        or _CONT_ORDERED_RE.match(line)
        or _CONT_RULE_RE.match(line)
    )


def _fold_continuation(content_lines: List[str], head: int, head_indent: int,
                       fenced: Optional[Set[int]] = None, offset: int = 0):
    """Read the wrapped tail of the value whose head is `content_lines[head]`. (WRAP)

    Returns `(tail_lines, end)` where `tail_lines` are the stripped continuation
    texts in order and `end` is the index of the first line that is NOT part of the
    value. A line continues the value when it is:

      * non-blank                       (a blank line closes a value, as it closes a
                                         P4/P5 block — one termination rule, not two);
      * indented STRICTLY deeper than its head;
      * not `_starts_new_structure` — not a bullet, heading, table row, fence,
        blockquote, ordered item or thematic break;
      * not inside a ``` fence (the parser's own fence record decides, never a guess).

    Whitespace: the newline collapses to ONE space and the line is stripped of its
    structural indent — nothing else. Internal spacing is the author's and is kept
    verbatim, which is why this joins `line.strip()` rather than re-flowing the text.
    """
    tail: List[str] = []
    j = head + 1
    m = len(content_lines)
    while j < m:
        line = content_lines[j]
        if not line.strip():
            break
        if _indent_width(line) <= head_indent:
            break
        if fenced is not None and (offset + j) in fenced:
            break
        if _starts_new_structure(line):
            break
        tail.append(line.strip())
        j += 1
    return tail, j


def _read_indented_block(content_lines: List[str], head: int):
    """Read the indented block under the `- key:` head at `content_lines[head]`.

    Returns `(form, value, end, consumed, ambiguity)`:
      form       'list' | 'mapping' | 'empty' | 'ambiguous'
      value      List[str] for 'list', Dict[str, object] for 'mapping', else None
      end        index of the first line AFTER the block (the P4 `j`)
      consumed   [(index, BRANCH_*)] — recorded by the caller only on success
      ambiguity  (finding_kind, detail_dict) when form == 'ambiguous', else None

    P4 EQUIVALENCE (load-bearing): for every block whose lines are all `- item`
    bullets, this returns exactly the list, `end` and consumption the previous
    hand-written loop returned. The loop terminates on the SAME conditions — a blank
    line, or any line the block grammar does not recognise — so nothing that parsed
    before parses differently now. Verified by re-parsing five corpora and diffing.
    """
    m = len(content_lines)
    items: List[str] = []
    item_indent: Optional[int] = None
    entries: List[List[str]] = []      # [key, value] pairs, in authored order
    entry_indent: Optional[int] = None
    consumed: List[Tuple[int, str]] = []
    j = head + 1
    while j < m:
        line = content_lines[j]
        if not line.strip():
            break                       # a blank line closes the block (P4, unchanged)

        nested = _NESTED_ITEM_RE.match(line)
        if nested is not None:
            if entries:
                return ("ambiguous", None, j, consumed,
                        (FINDING_MIXED_PROPERTY_BLOCK,
                         {"at_offset": j, "form_seen_first": "mapping",
                          "form_seen_second": "list", "line": line}))
            items.append(nested.group(1).strip())
            if item_indent is None:
                item_indent = _indent_width(line)
            consumed.append((j, BRANCH_PROP_NESTED_ITEM))
            j += 1
            continue

        mm = _NESTED_MAP_RE.match(line)
        if mm is not None:
            if items:
                return ("ambiguous", None, j, consumed,
                        (FINDING_MIXED_PROPERTY_BLOCK,
                         {"at_offset": j, "form_seen_first": "list",
                          "form_seen_second": "mapping", "line": line}))
            indent = _indent_width(line)
            if entry_indent is None:
                entry_indent = indent
            if indent == entry_indent:
                entries.append([mm.group(2), mm.group(3).strip()])
                consumed.append((j, BRANCH_PROP_MAPPING_ENTRY))
                j += 1
                continue
            if indent > entry_indent:
                # entry-shaped, but a column deeper than its siblings: a mapping of
                # mappings (not in the model) or a mis-indent. Reported, not guessed.
                return ("ambiguous", None, j, consumed,
                        (FINDING_NESTED_MAPPING_BLOCK,
                         {"at_offset": j, "entry_indent": entry_indent,
                          "line_indent": indent, "line": line}))
            break                       # dedented out of the block

        # Neither shape. Inside a MAPPING only, a line indented deeper than the entry
        # column continues the entry above it — the corpora wrap long quoted values
        # across lines, and a break here would drop the tail of a §8.1 block while
        # marking the head consumed, i.e. convert a LOUD drop into a silent one.
        if (entries and entry_indent is not None
                and _indent_width(line) > entry_indent
                and not _starts_new_structure(line)):
            entries[-1][1] = (entries[-1][1] + " " + line.strip()).strip()
            consumed.append((j, BRANCH_PROP_MAPPING_CONT))
            j += 1
            continue

        # (WRAP) The same fold for a LIST item. Before it existed, a wrapped `- when:`
        # item did not merely lose its tail — the unrecognised line CLOSED THE BLOCK,
        # so every SIBLING item after it was dropped too (`41-…`'s `scenario:` kept
        # `when` and lost `then` entirely). A list item's tail is exactly a scalar's
        # tail: deeper than the item's own bullet column, and not the start of new
        # structure. The bullet case cannot reach here — `_NESTED_ITEM_RE` above takes
        # every indented bullet first, so a sub-bullet is still a sibling item, not a
        # continuation. P4 EQUIVALENCE is preserved for every block that parsed before:
        # this branch only fires where the old loop would have BROKEN.
        if (items and item_indent is not None
                and _indent_width(line) > item_indent
                and not _starts_new_structure(line)):
            items[-1] = (items[-1] + " " + line.strip()).strip()
            consumed.append((j, BRANCH_PROP_ITEM_CONT))
            j += 1
            continue
        break                           # anything else closes the block (P4, unchanged)

    if items:
        return "list", items, j, consumed, None
    if entries:
        mapping: Dict[str, object] = {}
        for k, v in entries:
            # Same repeated-key rule the rest of the grammar uses (SPEC §2.2 / the
            # `_maybe_multivalue` accumulation) rather than a second, silent one.
            mapping[k] = _maybe_multivalue(k, v, mapping)
        return "mapping", mapping, j, consumed, None
    return "empty", None, j, consumed, None


def _parse_properties(content_lines: List[str],
                      coverage: Optional[LineCoverage] = None,
                      offset: int = 0,
                      findings: Optional[List[ParseFinding]] = None,
                      source_file: Optional[str] = None,
                      node_id: Optional[str] = None) -> Dict[str, object]:
    """Parse P1/P2/P3/P4/P5 properties from a node body.

    Returns key -> str | list | dict (P5 mapping; SPEC §8.1/§8.2/§7.1).

    `coverage`/`offset` are the [FL-LINE-ACCOUNTING] side-channel: each branch
    records the ABSOLUTE line index it consumed (`offset` is the node's heading line
    index, `content_lines` starting there). Recording happens at exactly the points
    the branch commits, so the record cannot drift from the grammar — that is the
    entire reason the record lives here rather than in the linter.

    `findings` collects the blocks the P5 discrimination refused to resolve.
    """
    props: Dict[str, object] = {}
    i = 0
    m = len(content_lines)
    while i < m:
        line = content_lines[i]
        bold = _PROP_BOLD_RE.match(line)
        simple = _PROP_SIMPLE_RE.match(line)
        key_only = _PROP_KEY_ONLY_RE.match(line)
        if bold or simple:
            # (WRAP) A P1/P2 head plus its wrapped tail is ONE value. The head anchors
            # at column 0 (`^-`), so the head indent is 0 and any deeper non-furniture
            # line below it belongs to this value.
            match = bold or simple
            head_branch = BRANCH_PROP_BOLD if bold else BRANCH_PROP_SIMPLE
            key, val = match.group(1), match.group(2).strip()
            tail, j = _fold_continuation(
                content_lines, i, 0,
                fenced=(coverage.fenced if coverage is not None else None),
                offset=offset)
            if tail:
                val = " ".join([val] + tail)
            props[key] = _maybe_multivalue(key, val, props)
            if coverage is not None:
                coverage.record(offset + i, head_branch)
                for k in range(i + 1, j):
                    coverage.record(offset + k, BRANCH_PROP_CONT)
            i = j
            continue
        if key_only:
            # P4 (indented `- item` -> list) or P5 (indented `subkey: value` -> dict).
            key = key_only.group(1)
            form, value, j, consumed, ambiguity = _read_indented_block(content_lines, i)
            if form in ("list", "mapping"):
                props[key] = value
                if coverage is not None:
                    coverage.record(offset + i, BRANCH_PROP_KEY_ONLY)
                    for (k, branch) in consumed:
                        coverage.record(offset + k, branch)
                i = j
                continue
            if form == "ambiguous" and findings is not None:
                kind, detail = ambiguity
                detail = dict(detail)
                at = detail.pop("at_offset")
                findings.append(ParseFinding(
                    kind=kind,
                    message=_AMBIGUITY_MESSAGE[kind].format(
                        key=key, line=detail.get("line", "").strip()),
                    source_file=source_file,
                    source_line=offset + i + 1,
                    node_id=node_id,
                    key=key,
                    detail={**detail, "conflict_line": offset + at + 1},
                ))
            # NOTE (deliberate): a `- key:` head that collected NOTHING — and an
            # AMBIGUOUS block, which stores nothing either — is NOT recorded as
            # consumed. It parsed as nothing and stored nothing (mis-indented items,
            # `*` bullets, an intervening blank line, or a block the grammar refuses
            # to guess at), and the coverage lint is exactly the control that surfaces
            # it. Recording it here would hide the drop.
        i += 1
    return props


_AMBIGUITY_MESSAGE = {
    FINDING_MIXED_PROPERTY_BLOCK:
        "property {key!r}: the indented block mixes `- item` bullets (P4 list) with "
        "unbulleted `subkey: value` entries (P5 mapping) under ONE `- key:` head. A "
        "value is a list OR a mapping, never both, so the property is NOT set — split "
        "the block, or bullet every line. First conflicting line: {line!r}",
    FINDING_NESTED_MAPPING_BLOCK:
        "property {key!r}: a mapping entry is indented DEEPER than its siblings. A "
        "property value is scalar, list or mapping with no nesting, so the property is "
        "NOT set — align the entry with the others, or fold it into the value above. "
        "Offending line: {line!r}",
}


# ---------------------------------------------------------------------------
# (TBL) THE ID TABLE — a table whose first column is an IDENTIFIER column is DATA
# ---------------------------------------------------------------------------
# SPEC §2.3 lists tables among the ignorable furniture, and for a DECORATIVE table
# that is right — a two-column "Layer | What it does" comparison is prose in a grid.
# But the corpora also author their ACCEPTANCE CONTRACTS as tables: `| ID | Requirement
# | verify.machine | verify.human |` with rows `SUB-C1 … SUB-C16` is the whole testable
# surface of a spec, and it was reaching the graph as exactly nothing
# (`corpora/acme-specs/41-…`'s `[ACME-SUB-ACCEPT]` captured 7% of its authored
# bytes, all of it from the three `- key:` lines around a 14-row table).
#
# THE DISCRIMINATOR IS STRUCTURAL AND CONSERVATIVE, and it is stated in one place:
#
#   A table is an ID TABLE iff it has a header row, a `|---|` rule, at least one body
#   row, at least TWO columns, its first-column body cells are UNIQUE, and EITHER
#     (H) its first HEADER cell is an id-ish name — `id`, `ids`, `#`, or `… id`; OR
#     (S) at least 80% of its first-column body cells are id-SHAPED — an
#         UPPER-SNAKE/hyphen id (`SUB-C1`, `ACME-R-C20`, `M1`).
#
# WHY UNDER-CLAIM. A decorative table wrongly promoted pollutes every node it lands on
# and is then queryable, cited and trusted; a data table left out stays exactly as
# invisible as it is today. The asymmetry is not close, so the rule takes the
# false-negative every time. Deliberately NOT promoted, though each is arguably data:
# `| Namespace | State Engine | … |`, `| Key | Required | Type |`, `| Param | Type |
# Default |`, and any table whose first column repeats.
#
# WHY PROPERTIES ON THE ENCLOSING NODE, NOT CHILD NODES. The rows are ATTRIBUTES of
# the contract the node states — `SUB-C1` is not an entity with its own lifecycle,
# references or relationships; it is a row of `[ACME-SUB-ACCEPT]`. Minting 479 nodes
# corpus-wide would inflate the entity count, and every `CONTAINS` edge to them would
# be a structural fact nobody authored. The rows land as ONE mapping property keyed by
# the id column, which is the P5 mapping shape the model already carries.
#
# WHY THE ROW TEXT IS `header: cell` PAIRS. A bare `|`-join would strip the column
# semantics the header carries, and a reader (human or Intelligence) would have to go
# back to the source to know which cell was the machine test and which the bench gate.
#
# WHY NO EDGES ARE MINTED FROM ROW CELLS. Identical to the P5 mapping rule in
# `_property_refs`: cells quote node ids inside prose, and harvesting them would mint
# hundreds of edges no corpus authored. The text is captured; the graph is not guessed.
_TBL_ROW_RE = re.compile(r"^[ \t]*\|")
_TBL_RULE_RE = re.compile(r"^[ \t]*\|[ \t:\-|]+\|?[ \t]*$")
_TBL_ID_HEADERS = frozenset({"id", "ids", "#"})
_TBL_ID_CELL_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:[-.][A-Z0-9]+)+$|^[A-Z]{1,6}[0-9]+$")
_TBL_ID_SHARE = 0.8
# The property key the promoted rows land under. One key per node, whatever the number
# of ID tables in its body, so a consumer has ONE place to look.
ID_TABLE_PROPERTY = "id_table"


def _tbl_cells(line: str) -> List[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _tbl_plain(cell: str) -> str:
    """Strip the cosmetic wrappers a cell may carry (`**bold**`, `` `code` ``,
    `[brackets]`) so the SHAPE test sees the token the author meant. Used only for
    the discriminator — the value stored is always the cell as authored."""
    return cell.strip().strip("*`[]() ").strip()


def _is_id_table(header: List[str], body: List[List[str]]) -> bool:
    if len(header) < 2 or not body:
        return False
    firsts = [_tbl_plain(r[0]) for r in body if r and _tbl_plain(r[0])]
    if len(firsts) != len(body) or len(set(firsts)) != len(firsts):
        return False        # a blank or repeated first cell is not an identifier column
    h0 = _tbl_plain(header[0]).lower()
    if h0 in _TBL_ID_HEADERS or h0.endswith(" id"):
        return True
    hits = sum(1 for f in firsts if _TBL_ID_CELL_RE.match(f))
    return hits / len(firsts) >= _TBL_ID_SHARE


def _iter_id_tables(content_lines: List[str], fenced: Optional[Set[int]] = None,
                    offset: int = 0):
    """Yield `(block, header, body_idx, body)` for every ID TABLE in `content_lines`.

    ONE scanner, used by both the promoting path and the unattached-table detector, so
    the two can never disagree about what an ID table is.
    """
    i = 0
    m = len(content_lines)
    while i < m:
        if not _TBL_ROW_RE.match(content_lines[i]) or (
                fenced is not None and (offset + i) in fenced):
            i += 1
            continue
        start = i
        while i < m and _TBL_ROW_RE.match(content_lines[i]) and not (
                fenced is not None and (offset + i) in fenced):
            i += 1
        block = list(range(start, i))
        # header / rule / body, in the fixed Markdown order. A run of `|` lines with no
        # `|---|` rule is not a table (it is ASCII art or a wrapped line) and is skipped.
        if len(block) < 3 or not _TBL_RULE_RE.match(content_lines[block[1]]):
            continue
        header = _tbl_cells(content_lines[block[0]])
        body_idx = [k for k in block[2:] if not _TBL_RULE_RE.match(content_lines[k])]
        body = [_tbl_cells(content_lines[k]) for k in body_idx]
        if _is_id_table(header, body):
            yield block, header, body_idx, body


def _parse_id_tables(content_lines: List[str],
                     coverage: Optional[LineCoverage] = None,
                     offset: int = 0) -> Dict[str, object]:
    """(TBL) Promote every ID TABLE in this node body to `{row_id: "col: cell; …"}`.

    Returns the merged mapping (empty when the body holds no ID table). Consumption is
    recorded per line, exactly as the property branches do, so a promoted row stops
    counting as ignorable `table_row` furniture the moment it becomes data.
    """
    rows: Dict[str, object] = {}
    fenced = coverage.fenced if coverage is not None else None
    for block, header, body_idx, body in _iter_id_tables(content_lines, fenced, offset):
        for k, cells in zip(body_idx, body):
            row_id = _tbl_plain(cells[0])
            pairs = [f"{header[c].strip()}: {cells[c].strip()}"
                     for c in range(1, min(len(header), len(cells)))]
            rows[row_id] = _maybe_multivalue(row_id, "; ".join(pairs), rows)
            if coverage is not None:
                coverage.record(offset + k, BRANCH_ID_TABLE_ROW)
        if coverage is not None:
            coverage.record(offset + block[0], BRANCH_ID_TABLE_HEADER)
            coverage.record(offset + block[1], BRANCH_ID_TABLE_RULE)
    return rows


def _enclosing_node_for_line(idx: int, headings) -> Optional[str]:
    """(TBL) The node whose scope CONTAINS line `idx`, ascending untagged headings.

    A node body ends at the next heading of ANY level, so a table under a plain
    `## 4. Compliance` sits in no node body even though the document plainly puts it
    inside the enclosing `# … [SPEC:X]`. For a PROPERTY the parser refuses to guess
    (`outside_node_body` is a flagged near-miss) and that refusal is right — re-homing
    a stray `status:` onto a document root would silently restate a fact about the
    wrong entity. An ID table is different in the one way that matters: the rows land
    under their own derived `id_table` key, they set no node field, and they mint no
    edge, so the worst case of attaching to an ancestor is a COARSER owner, never a
    wrong assertion. Against that, dropping a spec's entire acceptance contract is
    certain loss. So tables ascend; properties do not.

    The walk is the ordinary document-tree ancestor walk, the same shape
    `parent_section` already uses: from the innermost heading above the line, keep the
    first heading that is BOTH strictly more significant (a lower level) and
    `[ID]`-bearing. Returns None only when no ancestor carries an id at all.
    """
    cur_level: Optional[int] = None
    for (h_line, h_level, h_id, _dt, _t) in reversed(headings):
        if h_line >= idx:
            continue
        if cur_level is None:
            if h_id is not None:
                return h_id            # innermost enclosing heading IS a node
            cur_level = h_level
            continue
        if h_level < cur_level:
            if h_id is not None:
                return h_id
            cur_level = h_level
    return None


def _attach_orphan_id_tables(lines: List[str], coverage: LineCoverage,
                             headings, nodes_by_id: Dict[str, "Node"]) -> None:
    """(TBL) Land every ID table that no node body covered onto its ancestor node.

    Runs after node construction, over the WHOLE file, so `coverage.node_body` is
    complete and "already attached" is a fact rather than a prediction. A table with
    no `[ID]`-bearing ancestor anywhere above it has nothing to attach to and is
    recorded for the classifier to report — the residual case, and the only one.
    """
    for block, header, body_idx, body in _iter_id_tables(lines, coverage.fenced, 0):
        if all(k in coverage.node_body for k in block):
            continue                    # already promoted by the owning node's pass
        owner = _enclosing_node_for_line(block[0], headings)
        node = nodes_by_id.get(owner) if owner else None
        if node is None:
            for k in block:
                if k not in coverage.consumed:
                    coverage.id_table_unattached.add(k)
            continue
        rows = node.properties.get(ID_TABLE_PROPERTY)
        if not isinstance(rows, dict):
            if rows is not None:
                continue                # an AUTHORED `id_table` property wins, always
            rows = {}
            node.properties[ID_TABLE_PROPERTY] = rows
        for k, cells in zip(body_idx, body):
            row_id = _tbl_plain(cells[0])
            pairs = [f"{header[c].strip()}: {cells[c].strip()}"
                     for c in range(1, min(len(header), len(cells)))]
            rows[row_id] = _maybe_multivalue(row_id, "; ".join(pairs), rows)
            coverage.record(k, BRANCH_ID_TABLE_ROW)
        coverage.record(block[0], BRANCH_ID_TABLE_HEADER)
        coverage.record(block[1], BRANCH_ID_TABLE_RULE)


def _maybe_multivalue(key: str, val: str, props: Dict[str, object]) -> object:
    """Store a property value. The raw string is kept verbatim (P3 multi-value refs
    are re-scanned by edge extraction, so no information is lost by not splitting).
    A repeated key accumulates into a list (e.g. and-when/and-then conjuncts)."""
    existing = props.get(key)
    if existing is None:
        return val
    if isinstance(existing, list):
        existing.append(val)
        return existing
    return [existing, val]


def _parse_section_edges(lines: List[str], body_start: int,
                         headings: List[Tuple[int, int, Optional[str], Optional[str], str]],
                         source_file: str, registered_ns: frozenset,
                         profile_layers: Optional[Dict[str, str]] = None,
                         domain_verbs: frozenset = DEFAULT_DOMAIN_VERBS,
                         coverage: Optional[LineCoverage] = None,
                         findings: Optional[List["ParseFinding"]] = None,
                         strict: bool = False,
                         vocab: Optional[List["ParseFinding"]] = None) -> List[Edge]:
    """Parse `## Relationships` (E2) and `## Causal Graph` (E1/E3) sections.

    Section membership is by heading title (case-insensitive contains), tracked
    across the file. Within Relationships, the current `### <Layer>` subhead sets
    the layer for canonical-layer names; else the layer is derived from edge type.
    Causal-graph edges are read even inside ``` fences (they live there in the
    corpus), which is why we scan raw lines with an explicit section state.
    """
    edges: List[Edge] = []
    n = len(lines)

    # Map heading line-idx -> (level, title) for section detection.
    head_by_line = {h[0]: (h[1], h[4]) for h in headings}

    section = None        # 'relationships' | 'causal' | None
    section_level = 0
    rel_layer = None      # current canonical layer within relationships

    for i in range(body_start, n):
        line = lines[i]
        if i in head_by_line:
            level, title = head_by_line[i]
            tl = title.lower()
            # A heading at or above the section level closes the section.
            if section is not None and level <= section_level:
                section = None
                rel_layer = None
            if "relationships" in tl:
                section = "relationships"
                section_level = level
                rel_layer = None
                continue
            if "causal graph" in tl:
                section = "causal"
                section_level = level
                continue
            if section == "relationships" and level > section_level:
                # subhead names the layer (Semantic / Temporal / Causal / Entity)
                first_word = re.split(r"[ \t(]", tl.strip(), maxsplit=1)[0]
                rel_layer = first_word if first_word in CANONICAL_LAYERS else None
                continue
            continue

        if coverage is not None and section is not None:
            coverage.section[i] = section

        if section == "relationships":
            m = _REL_SRC_RE.match(line)
            if not m:
                continue
            src = _resolve_ref(m.group(1))
            if src is None:
                # [C1] SPEC A4 "Relationships operands" (source) position -- V2's
                # class: a mixed-case/ill-formed source is silently skipped today
                # (the whole line vanishes). Flag the ATTEMPT before moving on.
                if findings is not None:
                    _scan_value_for_malformed_refs(
                        m.group(1), position="relationships_source",
                        source_file=source_file, source_line=i + 1,
                        node_id=None, key=None, findings=findings)
                continue
            remainder = m.group(2)
            found = _find_edge_type(remainder, domain_verbs)
            if found is None:
                # [C13 / A3] THE PROSE-TARGET SILENT PATH. Today this `continue` is
                # where a line like `- [A] BUILDS_ON the earlier design` vanishes: the
                # verb is UPPER_SNAKE and unregistered, and there is no bracket after
                # it, so the anchored form does not match and the positional scan finds
                # no known token. The line is inside `## Relationships`, so its author
                # unambiguously meant an edge. SPEC §2.4 says unregistered types are
                # "preserved but flagged" — that clause covers the bracket-anchored
                # case only, and this one was never specified at all (review V11, class
                # b). It STILL does not mint (minting an unknown verb would fabricate a
                # type), but it is no longer silent.
                if vocab is not None:
                    _flag_unrecognized_verb_line(
                        remainder, source_file=source_file, source_line=i + 1,
                        node_id=src[1], strict=strict, findings=vocab)
                continue  # a `[ID] ...` line with no verb-shaped token is prose
            etype, type_end, recognised = found
            if coverage is not None:
                coverage.record(i, BRANCH_EDGE_RELATIONSHIPS)
            after_type = remainder[type_end:]
            layer = rel_layer or _layer_for_type(etype, profile_layers)
            flags = _type_flags(etype, registered_ns)
            if not recognised:
                flags = flags + ["unregistered_verb"]   # RV01: preserve AND flag
                # [C13 / A3] In a STRICT document an unregistered verb is an ERROR;
                # in a legacy document preserve+flag stands, unchanged. The EDGE is
                # identical either way — only the finding's severity moves, which is
                # what makes the strict document adoptable one file at a time.
                if vocab is not None:
                    vocab.append(ParseFinding(
                        kind="unregistered_verb",
                        message=(f"edge verb `{etype}` is not a core type and is not "
                                 f"registered by the domain profile. Register it under "
                                 f"the profile's `relationship_verbs.domain` / an "
                                 f"`EDGETYPE-*` node, or rewrite it to a registered "
                                 f"verb."),
                        source_file=source_file, source_line=i + 1,
                        node_id=src[1],
                        detail={"verb": etype, "severity": "error" if strict
                                else "warning",
                                "mode": MODE_STRICT if strict else MODE_LEGACY},
                    ))
            # Prefer a bracketed node target after the type; else PRESERVE the edge
            # with a prose-target snippet flagged 'unresolved_target' (never drop —
            # [LTS-PARSER]). Format ambiguity reported to the Architect.
            tgt_refs = _extract_refs(after_type)
            if not tgt_refs and findings is not None:
                # [C1] "Relationships operands" (target) position: the target slot
                # carried a bracket-shaped attempt that did not resolve to a REF.
                # Complementary to the existing `unresolved_target` edge flag below
                # (which still fires either way) -- this names WHY, when the reason
                # is a malformed id rather than a genuine prose target.
                _scan_value_for_malformed_refs(
                    after_type, position="relationships_target",
                    source_file=source_file, source_line=i + 1,
                    node_id=src[1], key=None, findings=findings)
            if tgt_refs:
                tdomain, tid = tgt_refs[0]
                edges.append(Edge(
                    source=src[1], target=tid, type=etype, layer=layer,
                    inference="authored", source_file=source_file, source_line=i + 1,
                    flags=flags, dangling=False, target_domain=tdomain,
                ))
            else:
                snippet = after_type.strip()[:80]
                edges.append(Edge(
                    source=src[1], target=snippet, type=etype, layer=layer,
                    inference="authored", source_file=source_file, source_line=i + 1,
                    flags=flags + ["unresolved_target"], dangling=True,
                    target_domain=None,
                ))
        elif section == "causal":
            m = _CAUSAL_EDGE_RE.match(line)
            if not m:
                continue
            etype, src_ref, tgt_ref = m.group(1), m.group(2), m.group(3)
            src = _resolve_ref(src_ref)
            tgt = _resolve_ref(tgt_ref)
            if src is None or tgt is None:
                # [C1] SPEC A4 "Causal operands" position -- V3's class: an
                # ill-formed causal operand is silently skipped today.
                if findings is not None:
                    if src is None:
                        _scan_value_for_malformed_refs(
                            src_ref, position="causal_source",
                            source_file=source_file, source_line=i + 1,
                            node_id=None, key=None, findings=findings)
                    if tgt is None:
                        _scan_value_for_malformed_refs(
                            tgt_ref, position="causal_target",
                            source_file=source_file, source_line=i + 1,
                            node_id=None, key=None, findings=findings)
                continue
            if coverage is not None:
                coverage.record(i, BRANCH_EDGE_CAUSAL)
            edges.append(Edge(
                source=src[1], target=tgt[1], type=etype, layer="causal",
                inference="authored", source_file=source_file, source_line=i + 1,
                flags=_type_flags(etype, registered_ns), dangling=False,
                target_domain=tgt[0],
            ))
    return edges
