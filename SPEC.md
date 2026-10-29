# FactLattice 1.0 Specification — DRAFT

> Normative specification for the FactLattice knowledge representation ecosystem. FactLattice defines a serialization-independent abstract model for encoding structured knowledge as typed nodes, edges, properties, and graph layers, with pluggable domain profiles for cross-domain extensibility.

**Status**: DRAFT — comprehensive rewrite addressing 31 audit findings. Second audit found 21 additional items; critical ones addressed in this revision.

### Conformance Language

The key words MUST, MUST NOT, SHALL, SHALL NOT, SHOULD, SHOULD NOT, MAY, and RECOMMENDED in this document are to be interpreted as described in RFC 2119. Requirements marked with MUST are non-negotiable for conforming implementations. Requirements marked with SHOULD are strongly recommended but may be deviated from with documented justification.

---

## 0. Foundational Principles

These govern every design decision in the specification. They emerged from analyzing 171 documents across 8 domains and cannot be overridden by any section below.

### P0: Knowledge Ecosystem, Not Document Format

FactLattice is an information ecosystem with four layers:

1. **Knowledge graph** — FactLattice documents (`.lts` files) containing typed nodes, edges, properties
2. **Event streams** — JSONL companion data for temporal assertions, audit trails, change records
3. **Traversal engine** — lts-engine, a constraint-driven graph walker with token budgets
4. **Rendering layer** — MkDocs, Knowledge Navigator, dashboards — the surfaces humans use

Design decisions are made at the graph level, not the document level. MkDocs and CLI are rendering surfaces for the same underlying graph. The graph index (`graph_index.json`) is the convergence artifact that all consumers share.

### P1: Machine-Authored, Human-Reviewed

Humans MUST NOT edit FactLattice documents directly. Only an Intelligence, during an interactive session, composes FactLattice content. The `.lts` file extension is deliberately unmapped on developer machines — double-clicking opens nothing. Humans interact exclusively through the rendering layer: MkDocs sites, dashboards, Knowledge Navigator.

This is a format-level invariant, not a preference. The density and structural precision that make FactLattice valuable (15–30 addressable concepts per 1000 tokens) are viable because the author is a machine. Human-authored prose achieves 3–8.

### P2: Notation Freedom Within the Document

A FactLattice document is not a Markdown file. It is a container that permits **any dense, ambiguity-eliminating notation** appropriate to the knowledge being encoded:

- Markdown prose for context and definitions
- YAML blocks for structured data and complex relationships
- Tables for matrices, taxonomies, comparison data
- Causal graph notation for cause-effect chains
- Extended flow notation for processes with branching and decision points
- Domain-specific notations registered in domain profiles

The core spec defines which notations the parser handles by default. Domain profiles may register additional notation styles. The parser extracts the same abstract graph regardless of which notation was used. A reader encountering a FactLattice document in a text editor sees a mix of notations — this is by design. The rendered MkDocs output presents a unified view.

### P3: Namespace Everything

One deconfliction mechanism for all extensibility surfaces: edge types, ID prefixes, properties, document tags, cross-domain references, event stream entry types. Borrowed from Example's bus architecture. No special cases.

Three tiers for open-source distribution:
- **Core** (`core.*`) — immutable, ships with the spec, every parser understands them
- **Registered** (`{domain}.*`) — published in domain profiles, parsers load on demand
- **Private** (bare names) — local use, no registration needed, no collision guarantees

A parser encountering an unregistered namespace degrades gracefully — preserves the edge, flags for the linter. Zero friction for consumers; full validation for authors.

### P4: Mechanisms Are Core, Implementations Are Domain

Test: does it define a structural mechanism (interface, contract, syntax rule) or a specific implementation (tool, workflow, vocabulary)?

- Governance mechanism = core. A specific domain's change-management workflow (e.g., Example's corpus-proposals, Example's amendment pattern) = domain.
- Confidence annotation attribute = core. Medical confidence methodology = domain.
- Relationship notation mechanism = core. Domain-specific notation styles = domain.
- Parser syntax for `[PREFIX-NAME]` = core. What prefixes mean = domain.

### P5: Frequency-Driven Taxonomy Evolution

Don't architect vocabulary upfront. Observe what domains actually use. When 3+ domains converge on the same concept, it's a candidate for promotion to a shared namespace or core. Registry sweeps detect convergence algorithmically.

### P6: Architectural Self-Hosting

FactLattice governance is described in FactLattice. Domain profiles are parseable by the parser they configure. The spec references itself via the mechanisms it defines. DELTAs (change records) are themselves FactLattice documents.

### P7: Confidence as Epistemology

Every claim carries implicit certainty. Making it explicit enables quality tools to prioritize and downstream Intelligences to evaluate rather than parrot. The confidence attribute (presence, schema, semantics) is core; assessment methodology is domain.

---

## 1. Abstract Model

### 1.1 Nodes

A node is a named unit of knowledge with a unique identifier, properties, and relationships.

```
Node:
  id: string              # Bracketed identifier, e.g., "GOV-AMEND-FLOW"
  type_prefix: string     # Prefix before first hyphen, e.g., "GOV"
  title: string           # Human-readable name
  source_file: string     # Origin file
  line_range: [int, int]  # Start and end lines
  content_hash: string    # SHA-256 of node content (for diff detection)
  token_count: int        # Token estimate for budget management
  properties: map         # Key-value pairs (core + domain)
  status: string          # Lifecycle state
  parent_section: string  # Containing section ID (structural inference)
  document_tag: string    # Inherited from document header
```

**Identifier formation rules:**
- Prefix-first: type prefix appears before the first hyphen (`TERM-TOKEN` → prefix `TERM`)
- Uppercase with hyphens: `[A-Z][A-Z0-9_\-]+`
- Unique within corpus: no two nodes share the same ID across all documents
- Stable: once assigned, an ID is permanent. Deprecation over deletion. Rename requires updating all references.

### 1.2 Edges

An edge is a typed, directed relationship between two nodes.

```
Edge:
  source: node_id
  target: node_id
  type: namespaced_string   # e.g., "core.DEPENDS_ON", "medical.CONTRADICTS"
  layer: enum               # semantic | temporal | causal | entity
  source_file: string       # Where the edge was declared
  source_line: int          # Line number of declaration
  inference: enum           # authored | property | structural | mention | computed
```

The `inference` field records how the edge was derived — crucial for priority resolution when multiple mechanisms declare edges between the same nodes.

**Edge priority** (highest to lowest):
1. Causal graph section declarations (E1, E3)
2. Relationship section declarations (E2)
3. Relationship-bearing properties (E4)
4. Structural inference from heading nesting (E5)
5. Mention detection from bracketed IDs in prose (E6)
6. Computed edges from dynamic link expansion (E7)

### 1.3 Properties

Properties are key-value pairs on nodes. They come in two fundamental categories:

**Relationship-bearing properties** — auto-generate typed edges:

| Property Key | Edge Type | Layer | Direction |
|-------------|-----------|-------|-----------|
| `see-also` | RELATED_TO | semantic | node → each referenced ID |
| `relates-to` | RELATED_TO | semantic | node → each referenced ID |
| `related-to` | RELATED_TO | semantic | node → each referenced ID |
| `depends-on` | DEPENDS_ON | semantic | node → each referenced ID |
| `implements` | IMPLEMENTS | semantic | node → each referenced ID |
| `contrast-with` | CONTRAST_WITH | semantic | node → each referenced ID |
| `supersedes` | SUPERSEDES | semantic | node → each referenced ID |
| `superseded-by` | SUPERSEDES | semantic | referenced ID → node |
| `canonical-node` | CANONICAL | semantic | node → each referenced ID |
| `used-by` | USED_BY | entity | referenced ID → node |
| `issuer` | ISSUES | entity | referenced ID → node |
| `tested-by` | TESTED_BY | entity | referenced ID → node |
| `verify` | TESTED_BY | entity | referenced ID → node |
| `verified-by` | TESTED_BY | entity | referenced ID → node |
| `spawns` | CAUSES | causal | node → each referenced ID |
| `follows` | BEFORE | temporal | referenced ID → node |
| `resolves` | RESOLVES | semantic | node → each referenced ID |
| `part-of` | PART_OF | semantic | node → referenced ID |
| `attaches-to-hook` | ATTACHES_TO_HOOK | semantic | node → each referenced ID |
| `applies-rule` | APPLIES_RULE | semantic | node → each referenced ID |

`attaches-to-hook` / `applies-rule` (added 2026-10-07 for the review-extension PROC graph; 0 prior uses
over every registered corpus root) mean: a `REVPROC-` node attaches to a `REVHOOK-` node, and a
`REVPROC-` node applies a `REVRULE-` node, which may itself apply an existing rule node ("pointing,
never copying"). Both edges are FORWARD and stay inside one corpus (ids are partition-local). They are
NOT ordering (`order` is an integer property) and NOT inclusion (`part-of`). The selector property
`applies-to` and every other spelling are prose and mint nothing. Lint origin `review-edges` checks the
value shape, that the target exists and the `REVPROC-` -> `REVHOOK-` / `REVRULE-` shape — ALL AS WARNINGS: no review-edge lint is ever an error in any corpus (a corpus that uses the generic keys for its own purposes, free text included, or a misshapen extension inside its host, is told but not failed; the reviewer's extension validation is what refuses a misshapen extension).

`see-also` / `relates-to` / `related-to` are SYNONYMS producing an identical edge; a
traversal cannot tell which spelling was authored. Synonyms are intended — the alternative
is that the natural spelling of a relation silently mints nothing.

**Membership is decided by VALUE SHAPE, not by whether the key sounds relational.** A
relationship-bearing property's value is a REFERENCE LIST (bracketed ids, separators, an
optional trailing gloss). `rationale`, `definition` and `requirement` all quote node ids —
inside SENTENCES — and are descriptive; harvesting those citations would turn every
inline reference into a graph edge. §8.1 quotes ids in prose on purpose.

#### 1.3.1 Profile-registered relationship properties

⛔ **AMENDED 2026-08-15 (the maintainer).** Edge-bearing property names were previously fixed in
core and NOT profile-configurable. **That is superseded.** A domain profile MAY register
its own edge-bearing property keys:

```yaml
edge_types:
  - { type: EXEMPLIFIES, property_key: exemplifies, layer: semantic }
  - { type: GOVERNS,     property_key: governed-by, layer: semantic, reversed: true }
```

and in the `.lts` serialization, as a field on the existing `EDGETYPE-*` declaration:

```
### [EDGETYPE-EXEMPLIFIES] A is a concrete instance of principle B
- edge-type: EXEMPLIFIES
- property-key: exemplifies
- layer: semantic
```

The edge type is declared ONCE and simply names the property that mints it.

**Constraints, all enforced as REFUSALS at profile load — never warnings, never
precedence rules:**

1. A profile **MUST NOT** redefine a built-in key from the table above, *even
   identically*. Collision raises `ProfileCollisionError` naming the key and both
   definitions. Rationale: a lost edge is a silence, but a REDEFINED edge is a lie —
   `part-of` alone has >1000 uses, and remapping it would leave every existing traversal
   answering confidently and wrongly while the corpus still lints clean.
2. Two profiles governing an overlapping corpus root **MUST NOT** disagree about a key.
   Identical registrations merge; disagreement refuses and names both profiles. (A
   single directory is routinely a root of several registered corpora, so this is an
   ordinary case, not an edge case.)
3. A registered key must match `^[a-z][a-z0-9_-]*$`, or it could never parse as a
   property and the registration would be inert while reading as functional.

#### 1.3.2 Profile-registered edge TYPES

⛔ **AMENDED 2026-08-15.** §1.3.1 gave profiles a registrable *property key* namespace
and guarded it. The **edge-type namespace** it mints into was left unguarded, and the
same three failures were reachable one level up. They are now governed by the SAME three
rules, deliberately worded to match §1.3.1's — **two collision rules that disagreed with
each other would themselves be a fork**, and every future author would have to re-derive
which one governed their case.

A profile declares an edge type in `edge_types[]` (YAML/JSON) or on an `EDGETYPE-*` node
(`.lts`), optionally with a `layer` and the `property_key` that mints it.

**Constraints, all enforced as REFUSALS at profile load:**

1. **A profile MUST NOT declare a BARE core edge type** (§1.4's table, as carried by
   `parser.CORE_EDGE_LAYER` — the operative table), *even identically*. One type would
   then carry two meanings, and because the LAYER is what `lts_traverse(layers=[…])`
   filters on, every layer-scoped query would return **part** of it, confidently, with
   the corpus still linting clean. Measured before the guard: a profile declaring
   `{type: PART_OF, layer: causal, property_key: contained-by}` loaded clean and produced
   PART_OF edges in `semantic` **and** `causal` at once.
   ⛔ **The rule keys on the BARE name, not on the bare suffix, and that is the point of
   a namespace.** `dec.SUPERSEDES` (temporal), `swe.DEPENDS_ON` (causal) and
   `dec.RESOLVES` (causal) all deliberately disagree with core and all SHIP; they are
   distinct types that no traversal can confuse with core. A rule keyed on the suffix
   would refuse nine shipped declarations while protecting nothing.
   ⛔ Restating a core verb under `relationship_verbs` remains **allowed** — that block
   attaches no semantics (the parser takes the union of core and domain verbs), so it is
   a genuine no-op.
2. **Two profiles governing an overlapping corpus root MUST NOT disagree about a type's
   layer.** Identical declarations merge; disagreement refuses and names both profiles
   and both layers — never last-wins, never first-wins, never a warning. This replaces a
   measured first-wins-by-filename-sort-order resolution on the live engine build path.
   ⛔ `reversed` is **not** part of a type's identity: it is a property of the *key → type*
   mapping, and core itself registers both voices of one relation (`supersedes` /
   `superseded-by` → SUPERSEDES). Two profiles declaring the active and passive voice of
   the same type is agreement, not conflict.
3. **A declared type must match `^(?:[A-Za-z][A-Za-z0-9_]*\.)*[A-Z][A-Z0-9_]*$`** — an
   UPPER_SNAKE bare verb, optionally dot-namespaced — and its `layer`, if given, must be
   one of §1.4's four. Anything else is at best inert while reading as functional, and at
   worst **substituted**: the parser reads `clin-ical.GOVERNS` as the edge type
   `ical.GOVERNS` and `.GOVERNS` as `GOVERNS`, emitting each with no flag and no finding.
   The shape is derived from the parser's own recognisers, not composed independently, and
   a round-trip test pins the two together.

#### 1.3.3 Property-key vocabulary: DECLARED-OR-MARKED — amendment A2

⛔ **NEW 2026-08-16 (Era 1, RATIFY), amending P3 and P5 AS APPLIED TO PROPERTY KEYS.**
Implements amendment A2 of the parser's engineering notes (rev 2, not published).

⛔ **THIS IS AN AMENDMENT TO THE FOUNDATIONAL PRINCIPLES, NOT A BUG FIX, AND IT IS
RECORDED AS ONE BECAUSE THE DIFFERENCE IS THE WHOLE ARGUMENT.** P3 grants a **Private
tier** — *"bare names — local use, no registration needed, no collision guarantees"* —
and P5 says *"Don't architect vocabulary upfront."* The measured population (**3,177
distinct keys in use, 0 declared, 2,655 used exactly once**, across 113 files) is
therefore **not indiscipline that a parser failed to catch**. It is those two principles
working exactly as written, produced by machine authors doing what P5 told them to do,
minus the registry-sweep governance P5 itself specifies and which was never running as a
gate. Anyone shipping this as a defect fix would be misdescribing a change of the
format's identity, and the next reader would be unable to find out why the rules moved.

**The regime.** A property key in a **strict-mode** document (§1.5.1) MUST be one of:

1. **A CORE key** — §1.3's four tables above (relationship-bearing, descriptive,
   classification, structural) plus §1.5's frontmatter table. ⛔ **Those tables become
   NORMATIVE-CLOSED rather than illustrative.** That sentence is the amendment's real
   content.
2. **A PROFILE-DECLARED key** — extending §1.3.1's mechanism to non-minting keys. A
   domain profile declares descriptive keys it owns, under the same refusal constraints
   §1.3.1 states (must match `^[a-z][a-z0-9_-]*$`; two profiles over one root must not
   disagree).
3. **AN EXPLICITLY-PRIVATE key** — prefix **`x-`** (`- x-observed-tonight: …`). Private
   keys parse as ordinary properties, mint nothing, and are exempt from declaration.
   This is P3's private tier retained in full, now **marked instead of ambient**.

An unmarked, undeclared key-shaped line is an **ERROR** (`undeclared_key`) in a strict
document and a **warning** in a legacy one. It carries **edit-distance suggestions**
against the declared set (`see-alos` → *"did you mean `see-also`"*); where nothing is
near, it says *declare it, mark it `x-`, or rewrite it* and **does not guess**.

⛔ **NOTHING IS DROPPED IN EITHER MODE.** An undeclared key still parses and still holds
its value. The mode decides the SEVERITY of a finding, per §1.5.1's invariant.

**Why `x-` and not `_`, and the reason is measurable rather than aesthetic.** The key
grammar is `^[a-z][a-z0-9_-]*$`, which REQUIRES a leading letter — so `_private` is
*itself an illegal key*, and choosing `_` would have forced a second amendment to the
key grammar. `x-` stays inside the existing grammar. (The OpenAPI/HTTP precedent is
real but secondary; the grammar argument is the deciding one.)

⛔ **WHY THE MARKED TIER IS LOAD-BEARING AND NOT A CONCESSION.** Hard-rejecting the tail
without it would destroy 2,655 authored assertions and damage 738 of 2,691 nodes. With
it, migration is mechanical, loses no value, and costs no author time. But the valve's
real purpose is narrower and more important: **it makes a deliberate one-off
DISTINGUISHABLE FROM A TYPO**, which is the one thing today's bare private tier
structurally cannot do — a mis-typed `see-alos` and an intentional local key are
byte-identical under P3 as originally written. That indistinguishability, not the
population size, is the defect A2 closes.

**Precision, and why closure is what buys it.** A shape-heuristic answering *"does this
prose line look like a property?"* measured **96% false-positive** on real corpora,
because prose and properties are genuinely shape-identical. Membership in a CLOSED
vocabulary is a different predicate: `see-alos` is not *near a shape*, it is one
character from a DECLARED NAME. The diagnostic is precise **because** the vocabulary is
closed — which is the engineering consequence of A2 rather than a preference about it.

**P5's promotion pipeline becomes the governed path** from `x-`/domain-declared keys
into shared vocabulary. P5 is not weakened by A2; it finally has the gate it always
implied — promotion is *from the marked tier*.

**Descriptive properties** — describe the node but do NOT generate edges:

| Property Key | Description |
|-------------|-------------|
| `definition` | Canonical definition |
| `role` | Function or responsibility |
| `scope` | What this node covers |
| `status` | Lifecycle: `operational`, `planned`, `draft`, `deprecated` |
| `rationale` | Why this decision was made |
| `tags` | Categorization labels |
| `alias` | Alternative name |
| `full-name` | Unabbreviated name |
| `problem-statement` | The problem this solves |
| `key-insight` | The critical realization |
| `implication` | What follows from this |
| `exception` | When the rule does not apply |
| `constraint` | Limitation or requirement |
| `given` | Optional precondition narrowing the scenario's applicability (Scenario nodes; Section 5.5) |
| `when` | Triggering condition for a behavioral expectation (Scenario nodes; Section 5.5) |
| `then` | Expected outcome that follows from `when` (Scenario nodes; Section 5.5) |
| `and-when` | Additional condition conjoined with `when` (Scenario nodes; Section 5.5) |
| `and-then` | Additional expectation conjoined with `then` (Scenario nodes; Section 5.5) |

**Classification properties** — document-level metadata:

| Property | Values | Default |
|----------|--------|---------|
| `artifact-readiness` | `educational`, `system-design`, `code-generation` | `educational` |
| `announcement-status` | `announced`, `unannounced` | — |
| `product-type` | `internal-tooling`, `poc`, `mvp`, `commercial-sdk`, `commercial-server`, `commercial-browser` | — |
| `confidentiality` | `public`, `nda-shareable`, `internal-only` | `internal-only` |

**Structural properties:**

| Property Key | Description |
|-------------|-------------|
| `child-document` | Path to child specification |
| `source-file` | Source code file |
| `line-range` | Line range in source |
| `actor` | Swimlane / participant label read by the RENDER layer (§4.9); descriptive, mints no edge |
| `condition` | Decision-point question read by the RENDER layer (§4.9); descriptive, mints no edge |

### 1.4 Graph Layers

The knowledge graph has four layers. This is non-negotiable core — without shared layers, cross-domain traversal is impossible.

| Layer | Purpose | Core Edge Types |
|-------|---------|----------------|
| **Semantic** | Structural relationships between concepts | IS_A, PART_OF, HAS_PROPERTY, SPECIALIZES, IMPLEMENTS, RELATED_TO, SIMILAR_TO, CONTRAST_WITH, DEPENDS_ON, SUPERSEDES |
| **Temporal** | Time-ordered relationships | BEFORE, AFTER, SEQUENCE, CONCURRENT, DURATION, TIME_INDEXED_STATE, VALIDITY_WINDOW, DEADLINE |
| **Causal** | Cause-effect chains | CAUSES, CASCADES_TO, PRECONDITION_FOR, ENABLES, PREVENTS |
| **Entity** | Actor-artifact relationships | ISSUES, HOLDS, MANAGES, CONTAINS, OWNED_BY, REFERS_TO, MENTIONED_IN, USED_BY |

**Core count: 28 edge types** (immutable per Decision 3) + 4 temporal primitives added by Decision 8 = **32 total core types.** RESOLVES, SATISFIES, VALIDATES, MITIGATES, CREATES, PRODUCES, CONSUMES, AUTHORIZED_BY were removed — they lack source material provenance. If 3+ domains converge on any of these, they are candidates for promotion through the frequency-driven governance process (Section 16).

Domain-specific edge types are registered in domain profiles using `{domain}.{TYPE}` (e.g., `medical.CONTRADICTS`, `example.ENDORSES`, `platform.ROUTES_TO`). A namespaced type MAY assign a different layer than a core type of the same bare name — `dec.SUPERSEDES` is temporal where core SUPERSEDES is semantic — because the two are distinct types. Redefining a **bare** core type is refused at profile load; see §1.3.2 for that rule and the two that accompany it.

### 1.5 Frontmatter

Every FactLattice document has metadata:

| Key | Required | Type | Description |
|-----|----------|------|-------------|
| `scope` | Yes | string | One-sentence coverage description |
| `audience` | Yes | string | Target readers |
| `boundary` | Yes | string | Authority boundaries |
| `not-this` | Recommended | string | Common misconceptions, out-of-scope |
| `parent` | If child | reference | Parent document |
| `depends-on` | If applicable | list | Prerequisites |
| `confidentiality` | No | enum | `public`, `nda-shareable`, `internal-only` (default: `internal-only`) |
| `announcement-status` | No | enum | `announced`, `unannounced` |
| `product-type` | No | enum | See classification properties |
| `artifact-readiness` | No | enum | `educational`, `system-design`, `code-generation` |
| `version-scope` | No | string | Major version line (e.g., `"2.x"`) |
| `supersedes-version` | No | string | Prior version line |
| `breaking-changes` | No | list | Structural discontinuities |
| `domain` | No | string | Domain profile this document belongs to |
| `lts` | No | string | **Format version** — see §1.5.1 |

Classification properties in frontmatter are inherited by all nodes in the document unless overridden inline.

#### 1.5.1 The format-version marker — amendment A1

⛔ **NEW 2026-08-16 (Era 1, RATIFY).** Implements amendment A1 of
the parser's engineering notes (rev 2, not published). Nothing above is superseded; this is a new
OPTIONAL field.

```
---
lts: "1.1"
---
```

**Semantics, and they are the mechanism the entire migration roadmap hangs on:**

- **ABSENCE = LEGACY MODE.** The document is parsed under the descriptive behaviour
  this specification described before Era 1, plus diagnostics at `warning` severity.
  A conforming parser **MUST NOT** refuse a document for lacking the field, and
  **SHOULD** report `legacy_document` once per file at lint so the un-migrated tail is
  countable.
- **PRESENCE (`lts: "1.1"` or later) = STRICT MODE.** The vocabulary regimes of §1.3.3
  (keys), §2.4.1 (verbs) and §1.1's reference totality apply at `error` severity.
- **An UNRECOGNISED version is a finding and is parsed STRICTLY, not degraded to
  legacy.** `lts: "9.9"` is an author asserting a contract this parser does not
  implement; reading it under the older, looser rules would answer confidently under
  rules the author did not ask for.
- **Quoting is not semantic.** `lts: "1.1"`, `lts: '1.1'` and `lts: 1.1` are the same
  assertion.

⛔ **THE INVARIANT THAT MAKES THE MARKER SAFE — and a conforming parser MUST hold it:**

> **The marker changes DIAGNOSTIC SEVERITY ONLY. The same bytes parsed with and without
> it MUST produce the same nodes, the same properties and the same edges.**

If the marker ever changed the graph, strictness would stop being stageable and a corpus
could no longer migrate one document at a time — which is the entire reason the field
exists. (Pinned by the control corpus, case `ERA1-VERSION-PRESENT`.)

**Verified precondition, 2026-08-16:** across 94 live corpus files there were **zero**
version-ish frontmatter keys. The field was uncontended, and absence is therefore the
state of the entire installed base — which is why absence had to mean *legacy* and not
*invalid*.

**Why per-document rather than per-corpus or per-engine.** A flag day is the one
migration shape that cannot be staged: every document must be correct before any
document can be checked. Making strictness a property of the DOCUMENT is CommonMark's
tolerated-divergence period with a marker instead of a guess — a corpus that never
migrates keeps working, in legacy mode, indefinitely.

### 1.6 Core Section Types

| Type | Required Properties | Purpose |
|------|-------------------|---------|
| Glossary | `definition` | Term definitions with canonical meanings |
| System | `role` or `definition` | System component architecture |
| Flow | Actors, Preconditions, Steps | Process flows (see Section 5.1–5.4 for extended format) |
| Scenario | `when`, `then` | Behavioral acceptance criterion; verifiable expectation attached to a parent node (see Section 5.5) |
| Invariant | constraint statement | Non-negotiable constraints |
| Not-This | misconception → correction | Explicit scope exclusions |
| Design Decision | `decision`, `rationale`, `status` | Architectural choices with reasoning |
| Resolution | `symptom`, `resolved-by` | Troubleshooting knowledge |
| Test Substrate | Acceptance Thresholds, Negative Test Cases | Test planning with criteria tables; aggregates Scenarios where applicable |
| Causal Graph | edge declarations | Cause-effect relationships |
| Relationships | typed edges by layer | Cross-cutting relationship declarations |

### 1.7 Document Type Tags

The document header carries a type tag: `# Title [DOCTYPE:ID]`

Core tags (cross-domain, used by 3+ domains):

| Tag | Purpose |
|-----|---------|
| META | Format specifications, reference guides |
| SYSTEM | System architecture, design principles |
| SPEC | Technical specifications |
| GUIDE | Operational guidance |
| FLOW | Protocol or process flows |
| IMPL | Implementation specifications |
| GLOSSARY | Term definitions |
| CHARTER | Project/product charters |
| PROJECT | Project specifications |
| TEST | Test planning substrates |
| DELTA | Change records (see Section 8) |

Domain-specific tags (e.g., `PRODUCT`, `AMEND`, `CPAT`) are registered in domain profiles.

### 1.8 Content Hashing

Three levels of integrity verification:

| Level | Hash | Purpose |
|-------|------|---------|
| Per-node | SHA-256 of node content | Diff detection between snapshots |
| Per-file | SHA-256 of file content | File-level change detection |
| Corpus | SHA-256 of all file hashes | Corpus-level integrity |

Content hashing is the foundation of the versioning/diff system (Section 8) and graph index snapshots (Section 3).

---

## 2. Parser Contract (Layer 0)

The parser is the bridge between FactLattice documents and the knowledge graph. It is domain-agnostic — it operates on syntax, not vocabulary. One parser produces one artifact: `graph_index.json`.

### 2.1 Extraction Rules

| Rule | Input | Output |
|------|-------|--------|
| **F1** | YAML frontmatter `---` block | Document-level metadata; inherited by contained nodes |
| **N1** | Node header `### [ID] Title` (h2-h4) | Graph node with type_prefix, title, source_file, line_range |
| **N2** | Document type tag `# Title [TYPE:ID]` | document_tag applied to all nodes in file |
| **N3** | Extended flow `impl` field | Implementation anchor with parse-time symbol resolution; `impl_stale` flag if unresolvable — ⚠ **RESERVED, unimplemented at conformance level 1 (see §2.1.1)** |
| **N4** | Scenario detection (any of: registered Scenario prefix; presence of both `when` and `then` properties; `section_type: scenario` in containing frontmatter) | Node tagged `node_kind: scenario` in properties; `when`/`then` validated (linter warns if missing); `and-when`/`and-then` collected as arrays — ⚠ **RESERVED, unimplemented at conformance level 1** |
| **P1** | Simple property `- key: value` | Entry in node's properties |
| **P2** | Bold-key property `- **key**: value` | Entry in properties (bold stripped) |
| **P3** | Multi-value property `- key: [A], [B]` | Array entry in properties |
| **P4** | Nested property `- key:` + indented list | Array entry in properties |
| **P5** | Mapping property `- key:` + indented `subkey: value` lines | Map entry in properties (§8.1/§8.2's `{change, context_for_change}` primitives) |
| **P6** | `version-scope` / `supersedes-version` | Version metadata; supersedes generates SUPERSEDES edges — ⛔ **RENAMED from "P5" 2026-08-16, see §2.1.1** |
| **E1** | Causal graph declaration format | Causal layer edges |
| **E2** | `## Relationships` section | Edges in declared layers |
| **E3** | Causal graph arrow format | Causal layer edges |
| **E4** | Relationship-bearing properties | Semantic/entity layer edges |
| **E5** | Section nesting (h2 > h3 > h4) | CONTAINS edges (entity layer) |
| **E6** | Bracketed IDs in prose | MENTIONED_IN edges (entity layer, lowest priority) — ⚠ **RESERVED, unimplemented at conformance level 1; a DELIBERATE scope choice, see §2.1.1** |
| **E7** | Dynamic link expansion manifest | Computed edges with provenance metadata — ⚠ **RESERVED, unimplemented at conformance level 1** |
| **E8** | Extended flow `condition`/`branches` | Decision edges with labeled outcomes — ⚠ **RESERVED, unimplemented at conformance level 1** |
| **E9** | Extended flow `next`/`converges_to` | Non-sequential flow edges — ⚠ **RESERVED, unimplemented at conformance level 1** |

#### 2.1.1 Conformance levels, and two corrections — amendment A4 (V8/V9)

⛔ **NEW 2026-08-16 (Era 1, RATIFY).** Two defects in THIS DOCUMENT, recorded as
§19's V8 and V9. Per the roadmap's own sequencing, *"the spec must be clean before it
can win arguments"* — a specification that contradicts itself cannot adjudicate between
a parser and a corpus, because either party can cite a different sentence of it.

**V8 — "P5" named two different rules, and §2.2 undercounted.** The table above used
`P5` for *version metadata* while the parser used `P5` for the *YAML-mapping property
form*, which it had added as "a CONFORMANCE fix to §2.2" — **a spec amendment executed
in a code comment**. Resolved by RENAMING this document's row to **P6**, and giving the
mapping form its own **P5** row above. The parser's name wins because it is the one in
every diagnostic, every test name and every finding already emitted; renaming the
document is a one-line change and renaming the code is a breaking one.

§2.2 additionally said *"four formats"* while the implementation carries **five, plus
continuation folding, plus id-table promotion**. That count is corrected below. The
parser sided with §8.1's mapping-shaped primitives and P2's notation-freedom principle
over §2.2's closed list, and it was RIGHT to — but that was a spec decision taken in a
comment, which is the governance failure §19 exists to end.

**V9 — specified-and-unimplemented rules were indistinguishable from implemented ones.**
N3, N4 and E6–E9 are described above in the same voice as rules the parser executes, so
*a conformance claim was unfalsifiable in either direction*: an implementation could
omit them and still claim conformance, and a reviewer could not tell which rules a
"conforming" parser had to have. They are now marked **RESERVED — unimplemented at
conformance level 1**.

> **Conformance level 1** = F1, N1, N2, P1–P6, E1–E5. A parser claiming level 1 MUST
> implement all of them and MUST NOT be required to implement any rule marked RESERVED.
> A RESERVED rule's text is normative for a FUTURE level and MUST NOT be read as
> optional-forever; it is a commitment with no date, stated as one.

E6 (prose-mention edges) is specifically a **deliberate scope choice, not an oversight**
— harvesting every bracketed id in prose would turn every inline citation into a graph
edge, which is the same failure §1.3's value-shape rule exists to prevent one level
down.

### 2.2 Property Notation Formats

⛔ **CORRECTED 2026-08-16 (V8).** This section previously said "four formats" and listed
four. The operative grammar carries **five formats plus two transformations**, and the
undercount is what let the mapping form be added in a code comment. Superseded text is
retained above in §2.1.1 rather than deleted.

**Five formats, all producing the same abstract properties:**

**Simple (P1):** `- key: value` → regex: `^-\s+([a-z][a-z0-9_\-]*)\s*:\s+(.+)$`

**Bold-key (P2):** `- **key**: value` → regex: `^-\s+\*\*([a-z][a-z0-9_\-]*)\*\*\s*:\s+(.+)$`
Bold normalized to plain by parser.

**Multi-value (P3):** `- key: [REF-A], [REF-B]` → each bracketed ID extracted as array element.

**Nested list (P4):** `- key:` followed by indented `  - item` lines → yields array.

**Mapping (P5):** `- key:` followed by indented `  subkey: value` lines → yields a MAP.
Required by §8.1/§8.2's `{change, context_for_change}` and `{task, context_for_task}`
primitives, which this document specifies and which no list-only grammar can represent.
⛔ A block mixing bulleted items and unbulleted mapping entries under one key is
**AMBIGUOUS and MUST be reported, never guessed** — guessing would silently pick one
reading of an author's intent.

**And two transformations, which are not formats but are load-bearing:**

- **Continuation folding (WRAP):** a P1/P2 head plus its wrapped tail lines is ONE
  value.
- **ID-table promotion:** a markdown table whose first cell is id-shaped contributes an
  `id_table` property to the enclosing node.

⛔ **Membership in the KEY grammar is `^[a-z][a-z0-9_\-]*$` and the anchors are
normative.** A qualifier in the key region (`- requirement (qualified): …`,
`- requirement — dashed: …`) does NOT parse as a property and MUST be FLAGGED rather
than accommodated by widening the key pattern: widening it moves the key boundary to
the LAST colon and destroys the value of every legitimate line that contains one.

### 2.3 What the Parser Ignores

| Content | Treatment |
|---------|-----------|
| Prose paragraphs | Included in node's line_range content, not semantically parsed |
| Hand-embedded diagrams (deprecated, §15.1) | Included in content, not parsed for edges — a relationship that exists only inside one is not declared |
| Code blocks (non-causal) | Included in content, not parsed |
| Tables (non-relationship) | Included in content, not parsed for structure |
| Blockquote summaries | Included in content |
| Horizontal rules | Visual separators, not parsed |
| **A line that LOOKS like structure but matches no rule above** | **NOT ignorable. Preserved in content like everything else, but it is a silent loss from the structured view and MUST be reported — see §2.5.** |

### 2.4 Parser Behavior with Namespaces

The parser is generous:
- Core edge types (`core.*`) are always valid
- Registered domain types (loaded from profile) are validated
- Unregistered types are preserved but flagged for the linter
- **Validation is the linter's job, not the parser's.** New domains can author immediately and formalize conventions later.

#### 2.4.1 Edge-verb regime — amendment A3

⛔ **NEW 2026-08-16 (Era 1, RATIFY).** Implements amendment A3 of
the parser's engineering notes (rev 2, not published). The generosity above is retained for LEGACY
documents and is not superseded there.

1. **Legacy documents (§1.5.1): unchanged.** An unregistered verb is preserved and
   flagged, exactly as the four bullets above describe.
2. **Strict documents: an unregistered verb is an ERROR** (`unregistered_verb`). The
   EDGE IS IDENTICAL in both modes — only the finding's severity moves, per §1.5.1's
   invariant. Register the verb in the profile, or rewrite it.
3. ⛔ **THE PROSE-TARGET PATH GETS A DIAGNOSTIC IN BOTH MODES** (`unrecognized_verb_line`
   — warning in legacy, error in strict). **This closes a gap that this specification
   never described at all**, and it is the more important half of A3:

   > `- [DEC-A] BUILDS_ON the earlier design` — inside `## Relationships`, so its author
   > unambiguously meant an edge. The verb is UPPER_SNAKE and unregistered, and the
   > target is prose rather than a bracketed id. Before this amendment the **entire
   > line was discarded with no edge, no flag, no finding and no line-accounting
   > record.**

   §2.4's "preserved but flagged" covers the **bracket-anchored** case only. The
   prose-target case was neither specified here nor decided anywhere — the parser
   simply returned nothing, and §2.5's anti-silent-loss invariant did not catch it
   because the line WAS consumed. It still does **not** mint (minting an unrecognised
   verb would fabricate a type, and a wrong edge is worse than a missing one), but it
   is no longer silent.

### 2.5 Line Accounting (the anti-silent-loss invariant)

Everything in §2.3 is ignored *deliberately*. The hazard is the line that is ignored
*accidentally*: it reads to its author as a property or an edge, it survives in
`line_range` / `source_excerpt` so the file looks right on the page, and it is simply
absent from every query. Nothing in §2.1–§2.4 requires a parser to say so, and in
practice no implementation did — three distinct mechanisms of this class went
undetected for weeks each while the linter reported zero findings.

The invariant that closes the class:

> **Every non-blank line of a `.lts` file is either CONSUMED by a parser branch, or
> CLASSIFIED as one of §2.3's ignorable forms. Anything else is a lint warning.**

Two properties make this the right predicate rather than a family of targeted rules:

* **Coverage, not equality.** A parser has several legitimate transformations where
  the parsed value differs from the written text by design (bold-key normalization,
  multi-value bracket extraction, nested block → array, repeated key accumulating
  into an array, whitespace stripping). Every one of them still *consumes* its source
  line, so line accounting is immune to all of them where a written-vs-parsed
  comparison drowns in false positives.
* **It does not need to know the mechanism.** A targeted rule per failure mode must
  be written after that mode is discovered. Line accounting reports the *next* one
  too.

Conformance requirements:

* A conforming parser SHOULD expose, per file, which line indices each branch
  consumed. The classifier MUST read that record rather than re-deriving "would this
  have parsed?" from a second copy of the grammar — a duplicated grammar is itself a
  silent-drop mechanism (this is precisely how a domain profile's registered
  relationship verbs came to be honoured by a reporting tool and ignored by the
  parser).
* A conforming linter SHOULD report unaccounted lines at `warning`, naming the file,
  the line, the text, and the branch that nearly consumed it.
* The ignorable set MUST be closed and enumerated. "Prose" is roughly 96% of
  candidate lines in surveyed corpora and must never warn; an ignorable set that is
  defined as "whatever was not consumed" is not an implementation of this invariant.

---

## 3. Graph Index (Layer 2)

The graph index is the convergence artifact — the single data structure that MkDocs, lts-engine, Knowledge Navigator, diff tools, and spec-gap analyzers all consume.

### 3.1 Schema

```json
{
  "version": "1.0",
  "generated_at": "ISO-8601",
  "generator": "factlattice-parser v1.0",
  "source_corpus": {
    "root_path": "/path/to/corpus",
    "files_hash": "sha256:...",
    "file_hashes": { "file.lts": "sha256:..." }
  },
  "snapshot_version": 47,
  "snapshot_hash": "sha256:...",
  "parent_snapshot_version": 46,
  "parent_snapshot_hash": "sha256:...",
  "commit_ref": "abc1234",
  "stats": {
    "total_nodes": 1995,
    "total_edges": 2153,
    "edges_by_layer": { "semantic": 800, "temporal": 150, "causal": 400, "entity": 803 }
  },
  "nodes": {
    "NODE-ID": {
      "id": "NODE-ID",
      "type_prefix": "NODE",
      "title": "Node Title",
      "source_file": "20-spec.lts",
      "line_range": [45, 78],
      "content_hash": "sha256:...",
      "token_count": 340,
      "properties": { "definition": "...", "status": "operational" },
      "status": "operational",
      "parent_section": "PARENT-ID",
      "document_tag": "SYSTEM"
    }
  },
  "layers": {
    "semantic": {
      "edges": [{ "source": "A", "target": "B", "type": "DEPENDS_ON", "layer": "semantic", "source_file": "...", "source_line": 42, "inference": "authored" }],
      "adjacency": { "A": ["B", "C"] },
      "reverse_adjacency": { "B": ["A"], "C": ["A"] }
    },
    "temporal": { "edges": [], "adjacency": {}, "reverse_adjacency": {} },
    "causal": { "edges": [], "adjacency": {}, "reverse_adjacency": {} },
    "entity": { "edges": [], "adjacency": {}, "reverse_adjacency": {} }
  }
}
```

### 3.2 Snapshot Versioning

Snapshots form an append-only chain:

| Field | Purpose |
|-------|---------|
| `snapshot_version` | Monotonically increasing integer. Enables ordering. |
| `snapshot_hash` | SHA-256 of graph_index.json content (excluding this field). Integrity verification. |
| `parent_snapshot_version` | Chain-of-custody. |
| `parent_snapshot_hash` | Tamper detection. |
| `commit_ref` | Git commit correlation. |

Snapshots are never modified after creation.

### 3.3 Multiple Consumers, One Index

| Consumer | Usage |
|----------|-------|
| lts-engine traversal engine | BFS from entry points with token budget |
| MkDocs plugin | Navigation structure, cross-references, search index |
| Knowledge Navigator | D3.js graph rendering, node content by line_range |
| Spec-gap analyzer | Walks IMPLEMENTS edges to find coverage gaps |
| Impact analyzer | Reverse DEPENDS_ON + forward CASCADES_TO traversal |
| Diff tool | Compares snapshot hashes to detect changes |

---

## 4. Engine Contract (Layer 3 — lts-engine)

**lts-engine** is ONE library over the Layer-2 graph index exposing FOUR composed API
surfaces — QUERY, ANALYTICS, LINT, RENDER — each a documented contract with its own module,
its own MCP tool(s), and the spec node below. All four compose over the SAME graph index and
the SAME freshness gate: a consumer (an Intelligence over MCP, or a human) can query,
aggregate, lint, and draw the same corpus through one coherent engine. §4.1–§4.5 specify the
QUERY layer's traversal contract; §4.6–§4.9 define the four-layer API surface.

### 4.1 Traversal Policy Schema

```yaml
policy:
  entry_points: [NODE-ID, ...]      # Starting nodes
  layers: [semantic, causal]          # Which graph layers to traverse
  edge_types: [DEPENDS_ON, ENABLES]   # Which edge types to follow
  max_depth: 3                        # Maximum traversal depth
  max_nodes: 50                       # Maximum nodes to return
  max_tokens: 32768                   # Token budget (lts-engine DEFAULT_TOKEN_BUDGET)
  direction: both                     # forward | reverse | both
  node_type_filter: [TERM, SYS]      # Filter by prefix
  priority_node_types: [INV, DD]      # Prioritized when budget is tight
  always_include: [NODE-X]            # Included regardless of budget
```

### 4.2 Whole-Node Integrity

Nodes MUST NOT be truncated. If a node cannot fit within the remaining token budget, it MUST be skipped entirely and a **stub** emitted:

```json
{ "id": "NODE-ID", "stub": true, "reason": "token_budget_exceeded", "token_count": 340 }
```

Stubs indicate the node exists but was excluded for budget reasons. The consumer can request the full node in a follow-up traversal.

### 4.3 Impact Analysis Algorithm

```
blast_radius(changed_node) =
  incoming: reverse traversal of DEPENDS_ON + IMPLEMENTS
    → "who depends on this node?"
  outgoing: forward traversal of CASCADES_TO + ENABLES + PRECONDITION_FOR
    → "what does this node cascade to?"
  depth: configurable, default 3
  budget: configurable, default 50 nodes
```

Output: flat list of reachable nodes with edge paths.

### 4.4 Intelligence Registry

The traversal engine maintains a registry mapping intelligence identifiers to context window sizes. Token budgets scale dynamically based on which Intelligence is requesting the traversal.

### 4.5 Domain Traversal Policies

Domain profiles may define:
- Default entry points per domain
- Named focus areas keyed to common tasks
- Keyword triggers that activate focus areas
- Primary layers (Enterprise = causal-heavy, Clinical = temporal-heavy, Platform = semantic-heavy)

### 4.6 QUERY Layer [LTS-QUERY]

- module: `factlattice_engine.tools` (`LtsEngine`)
- mcp-tools: `lts_find`, `lts_node`, `lts_traverse`, `lts_capabilities`, `lts_list_corpora`
- contract: the graph READ surface. `find` selects nodes by taxonomy / prefix / status /
  free-text / area with an optional since/until date-property window; `node` returns one
  node with its per-layer forward/reverse edges and a source excerpt; `traverse` is a
  bounded typed-edge BFS; `capabilities` is the connected surface around a topic/node;
  `list_corpora` enumerates loaded partitions with node/edge counts and freshness.
- guarantees: every read passes the freshness gate ([LTS-QUERY] MUST refuse a STALE
  partition, returning a loud stale marker, never old data); results are whole-node
  (§4.2) and token-budgeted with a cursor for continuation. Returns nodes/edges/subgraphs
  — never a scalar or an aggregate row (that is the ANALYTICS layer).
- registry: every route (the MCP server, `python -m factlattice_engine call`, the reviewer helpers, the
  library's profile resolution) takes its corpus registry from ONE lookup, `factlattice.registry.resolve_registry()`.
  Order, a closed set of sources: `env` (the registry variable; the file must exist and parse; it NEVER falls back),
  `user-config` (the per-user pointer file `$XDG_CONFIG_HOME/factlattice/config.json`, default
  `~/.config/factlattice/config.json`, `{"corpus_config": "<absolute path>"}`), then `example` (the built-in example
  registry, for the first-run trial only). A broken configured registry is `registry-not-found` (`call` and the helpers
  exit 2), and so is a request that NAMES a corpus the example registry lacks while it is in use ("no corpus registry
  is configured ..."); it is never the generic unknown-corpus error.
  `lts_list_corpora` carries a corpus-free `registry_choice`: `{source, path, sha256_at_boot, sha256_now,
  resolved_at}` (`sha256` is over the registry bytes, plus the working directory and resolved root only when a corpus-root or profile path is non-absolute, plus $HOME only when one starts with `~`, plus $FACTLATTICE_PROFILE_DIR when it is set; `sha256_at_boot` is the registry the
  server actually loaded and `sha256_now` the file as it is at the call, so a difference means the server serves an
  older registry and needs a restart). If the registry cannot be resolved at boot (after one retry), the server boots
  in REGISTRY-ERROR MODE: all tools are listed, `registry_choice` is `{source, path, error, error_persist}` (`error_persist` is the error with every path reduced to its basename, for persisted records), every corpus-scoped
  tool answers `registry-not-found` with zero data, and only a restart ends the mode. The field is absent on a server
  that predates it. A corpus-omitted read under the `example` source carries `extent.registry_source: "example"`.
  Every exception raised while interpreting a registry (a parse failure, a wrong-shaped entry, a registry whose
  corpora all fail to load) enters the same error mode, with a reason that carries the exception class and the registry
  path only. A registry file with a UTF-8 BOM is refused ("cannot be read as JSON"); save it without one.
  PRIVACY OF REGISTRY ERRORS, by surface (checkable):
  - Error text may name the env var, the pointer path and the registry path the user configured, never file CONTENT (no key, value or JSON fragment, and not the parser's own message, only its class).
  - FULL paths: terminal output (`call` stdout and stderr) and live MCP replies.
  - STRUCTURE only: the query log writes `error_class`, `error_code` and, for a registry refusal only, the error's `persist_text` (basenames built from parts); it writes no other error text (schema change: the old `error` free-text field is gone). REVIEW/UNREVIEWED fields carry `<source> <file name> sha256:<hex>`, the reviewer's fixed reasons and the same `persist_text`, never a raw tool error.
  - Where the named path is a directory (the example registry's root, the cwd, or an environment variable or the pointer names a directory), the persisted basename is that directory's own name; a directory's own name is accepted.
  - CONTENT never, on any surface.

#### 4.6.1 Response tiers [LTS-TIER]

⛔ **Retroactively documented 2026-09-11.** The `tier` argument (`digest` / `page` /
`full`, additive-only over `find`/`traverse`/`node`) shipped in `factlattice_engine/tiers.py`
(2026-08-13) but was never promoted into this SPEC — the only normative text lived in
that module's docstring. A defect this section exists to close
([ISSUE-EXCERPT-BUDGET]) went undetected for four weeks
precisely because there was no SPEC contract for the fix to be checked against.

- `tier` omitted: the LEGACY path, byte-identical to the pre-tier engine.
- `tier="digest"`: ALWAYS COMPLETE for the result set it claims — every matching node,
  carrying only routing metadata (id/title/token_count/property_count/edge_count/
  source_file/line_range/content_hash/timestamp) and NOTHING derived from the body.
  Takes no `offset`. Above `DIGEST_MAX_NODES` (1000) it ERRORS rather than trimming —
  the size itself is the finding.
- `tier="page"`: full node content, one explicit page (`offset` in; `next_offset` /
  `total` / `truncated` out). A page may legitimately be a fraction — it is NAMED one.
- `tier="full"`: full node content, COMPLETE, or an ERROR (`full_ceiling_exceeded`).
  **MUST NEVER return a silent fraction.** The ceiling for this contract is, in every
  case, **the caller's own `token_budget`** (resolved to `DEFAULT_TOKEN_BUDGET` when
  absent) — never a separate, lower, hardcoded constant. Concretely, for `lts_node`:
  the node's own body cost (its indexed `token_count`) plus its edge cost MUST be
  compared against the resolved budget; a response MUST NOT be refused on the grounds
  that an internal excerpt-rendering limit (e.g. a legacy default-response character
  cap) was exceeded while the true cost is within the caller's budget. A `full_ceiling_
  exceeded` error's stated remedy ("raise `token_budget`") MUST be true: raising the
  budget past the reported `total_token_count` MUST make the same request succeed.
  `find`/`traverse` enforce this via `assert_full_fits(items, budget, token_of=...)`;
  `lts_node` MUST hold the same invariant for its own body + edges.

#### 4.6.2 `limit` + `order_by` [LTS-LIMIT-ORDER]

⛔ **Implemented 2026-09-11** (`specs/20-engine-layers.lts`'s
`[QUERY-LIMIT-ORDER]` section — `rollout-status` there moves from `proposed` to
`shipped` alongside this entry). Two optional, per-query parameters on `find` and
`traverse` ONLY (§4.6-scope; NOT `capabilities` — no `tier` support to re-base against
yet — NOT `node` edge listings — a declared-complete contract §4.6.1 already covers —
NOT `lint` findings or `aggregate`, out of scope for the reasons given in the `.lts`
source's `[SPEC-COVERAGE-SCOPE]` table).

- **`limit`** (int >= 0, or absent): a ceiling on the number of nodes in the ORDERED
  result, applied LAST — after every existing filter and after `order_by`. Absent
  means unbounded, today's behavior, byte-identical. **The engine MUST NEVER
  substitute a fixed default (30, 50, 100…) for an absent `limit`** — the number
  belongs to the caller, never to the engine. `limit=0` is legal: an empty node list
  with `total_matched` still reporting the true pre-limit count.
- **`order_by`** (`"recency"`, `{field, direction}`, or absent): the ordering `limit`
  is taken FROM. `"recency"` is sugar for `{field: <the query's resolved
  date_property>, direction: "desc"}`. The general form's `field` may name ANY
  property key, not only a date; `direction` defaults to `"desc"` when omitted, and an
  unrecognized `direction` is a refused `invalid_order_direction` — never silently
  coerced. Absent leaves the existing default order exactly as it is today —
  `(corpus_id, id)` ascending for `find`, BFS discovery order for `traverse`.

**Pipeline (fixed order, no exceptions):**

```
match set  = every node passing the tool's existing filters      (unchanged)
ordered    = match set sorted by order_by  (or today's default order)
windowed   = ordered[:limit]               (or all of `ordered`)
response   = windowed, sliced again by tier / offset / token_budget (§4.6.1)
```

`total_matched` is always measured against `match set`, BEFORE `order_by` and BEFORE
`limit` — unchanged from §15's declared-reach vocabulary, and what lets a caller tell
"the newest 30 of 108" from "all 30 that exist."

**Null/incomparable ordering.** A node lacking `order_by.field`, or holding a value
whose TYPE does not match the field's DOMINANT type across the match set (dates
compare to dates, numbers to numbers; a stray string in a numeric field is never
coerced), falls into one UNORDERABLE bucket. That bucket sorts AFTER every comparable
node regardless of `direction`, and is itself ordered by id ascending — the same
tiebreak applied to genuine ties among comparable values, so two runs against an
unchanged corpus are byte-identical (§2's determinism expectation, [DEC-NO-LLM]). A
`field` matching no property anywhere is NOT an error: every node falls into the
unorderable bucket and `order_applied.unorderable_count` reports the size of the
bucket WITHIN the returned window.

**Tier interaction (re-bases §4.6.1's ceilings onto the WINDOWED set, post-`limit`):**

- `tier="digest"`: `assert_digestable`'s ceiling (`DIGEST_MAX_NODES`) is evaluated
  against `len(windowed)`, never `total_matched`. A match set over the ceiling that
  would refuse unbounded SUCCEEDS once `limit` narrows `windowed` under it. Still
  ALWAYS COMPLETE for what it claims — the top-N — and says so via `truncated: true`
  when `limit` narrowed the response below `total_matched`.
- `tier="full"`: `assert_full_fits`'s ceiling is evaluated against `windowed`; `limit`
  narrows the candidate set the ceiling must fit, it never relaxes the ceiling itself.
- `tier="page"` / legacy (tier omitted): `offset`/`token_budget` page WITHIN
  `windowed`, exactly as they page over the full match set today when no `limit`/
  `order_by` is supplied. `cursor`/`next_offset` MUST NOT exceed the size of
  `windowed`; once the caller has consumed the entire limited window, `cursor` is
  `null` even though `total_matched` may be far larger — `limit` and `offset` compose
  as window-then-page, not SQL's offset-then-limit, because `offset` here is a
  CONTINUATION cursor for one bounded answer the caller already committed to.

⛔ **`truncated` vs. `complete`/`bound_reason` — two questions, not one, once `limit`/
`order_by` is in play** (found by independent review after first landing; both fields
now correctly diverge from `truncated` rather than deriving from it):

- `truncated` (§15, [INV-DECLARED-REACH], UNCHANGED meaning) answers "is `count` less
  than `total_matched` — is there more in the CORPUS than THIS response carries." It
  is measured against the RAW pre-`limit` count and MAY legitimately read `true` on a
  response that is, in every other sense, complete — "the newest 30 of 108" is
  `truncated: true` even though all 30 are in hand.
- `complete` (§4.6.1) and `bound_reason` both answer a DIFFERENT question — "is the
  WINDOW exhausted" — and MUST be derived from the tier's own `cursor` (`None` once
  nothing more of `windowed` remains to fetch), NEVER from `truncated` or from a
  fresh `count < windowed_total` comparison. Both of those alternatives are correct
  only at `offset=0`: on the last page of a `limit`-bounded, budget-paged response
  (`cursor: null`, `next_offset: null`), and on a SUCCESSFUL `full`+`limit` call
  (`cursor` is always `None`), `truncated` can be `true` while the window is fully
  and successfully delivered — `complete` MUST still read `true` and `bound_reason`
  MUST still read `"none"` in both cases. A `cursor`-derived `complete`/`bound_reason`
  is byte-identical to the pre-`limit` formula at every `offset` when neither
  parameter is supplied (`cursor is None` and `not truncated` coincide there), so
  this is not a special case bolted onto the unmodified path — it is the ONE formula
  that is correct in both regimes.

**Result metadata — additive, omitted (not null) when unused ([INV-DECLARED-REACH],
§15):**

| field | present when | meaning |
|---|---|---|
| `limit_applied` | `limit` supplied | echoes the caller's `limit` |
| `order_applied` | `order_by` supplied | `{field, direction, unorderable_count}` |
| `windowed_total` | `limit` supplied | `min(total_matched, limit_applied)` |
| `bound_reason` | `limit` or `order_by` supplied | `"none"` \| `"token_budget"` — derived from `cursor` (see above), NOT from `count`/`windowed_total` directly |

**Backward compatibility (enumerated, MUST hold when neither parameter is supplied):**
the default order is unchanged for both tools; `digest`'s ceiling check runs against
`total_matched` exactly as today; `full`'s ceiling check runs against the full match
set; `page`/legacy slice the full match set by `offset`/`token_budget`; the response
carries NONE of the four fields above; every existing filter behaves exactly as it
does today. An unmodified caller's response is byte-identical, field for field,
ordering included, AT EVERY `offset` — not only `offset=0`.

Implementation: `factlattice_engine/ordering.py` (`resolve_order_by`,
`order_and_window`, `result_metadata`) — ONE implementation, reused by reference from
both `find` and `traverse` in `factlattice_engine/tools.py`, for the same reason
`budget.py` is its own leaf module (§4.6.1's shared-slice precedent).

#### 4.6.3 `caution` on suggestions and nodes [CANONICAL-EDGE-CAUTION]

A semantic suggestion candidate (`lts_semantic_find`, and the `did_you_mean_semantic`
block) and an `lts_node` response carry `caution` when the node is a LEGACY LABEL (an
outgoing `CANONICAL` edge, minted from `canonical-node: [ID]`) or is SUPERSEDED (an
INBOUND `SUPERSEDES` edge, from either `supersedes` or `superseded-by`). Shape:
`{kind: "legacy-label" | "superseded", canonical_id, canonical_title, text}`, where
`text` = the node's `canonical-label` if present, else "canonical definition: <title>"
or "superseded by: <title>". A legacy-label edge wins over supersession; several
targets pick the lexicographically smallest id; self-edges, dangling targets and
targets outside the node's own corpus produce NO caution.
The key is ABSENT (not null) when nothing triggers it. It ANNOTATES: rank, membership
and scores are byte-identical with and without it. Exact `lts_find` results do not
carry it (v1). Implementation: `factlattice_engine/caution.py`; `CANONICAL_EDGE_TYPE`
is pinned to the parser's `canonical-node` entry by a cross-import test.

#### 4.6.4 The node-change ledger and `lts_history` [NODE-CHANGE-LEDGER]

Every APPLIED corpus write (typed writers and `write_whole_file`) appends one
hash-chained JSON record to a per-file ledger,
`<state_dir>/ledger/<sha256(realpath)[:32]>.jsonl`, inside the per-file write lock,
right after `os.replace` (`factlattice/ledger.py`). A refusal appends nothing. Records
carry the verb (`set_property`, `append`, `repair_property`, `rename_property`,
`remove_property`, `add_node`, `write_document`), node and key, the value-aware `delta`, `discarded` (verbatim lines),
`inserted` (verbatim lines, for appends and added nodes), node and file hashes before
and after, `backup_path`, the caller's `proposer`/`approver`/`reason`/`via`, the
write-time `corpora`, `engine_head`, `ledger_seq` and `prev_sha256`. The record alone
can undo a write (CRLF files are restored in their LF form; see the ledger spec).
Every successful write receipt carries `ledger: {status: "ok", seq, file}`, or
`{status: "failed", reason: "ledger-write-failed", error_class, live_detail, live_detail_note}` (the `reason` is a fixed CODE; the
exception class is `error_class` and its text, which can hold a path, is `live_detail`, live reply only) with the warning and the report led by
"WRITTEN, BUT NOT LEDGERED (ledger-write-failed): <live_detail>". The
corpus write is never rolled back for a ledger failure, and the key is PRESENT on
every successful write.
`lts_history(corpus, node_id, limit)` is read-only and query-logged. It returns the
node's records, newest first, and DECLARES absence: `ledger: "none"` with
`history_complete: false` when the file has no ledger; otherwise `ledger_since`,
`reconciled` (null with `reconciled_reason` if the tail is unparseable),
`chain_file_breaks` (records whose `file_sha256_before` does not match the previous
record's `file_sha256_after`, i.e. an edit made outside the writers) and
`history_has_out_of_band_edits`.

Two further record fields. `follows_out_of_band_edit` (bool, always present): true when the
file's bytes at write time (`file_sha256_before`) differ from the previous record's
`file_sha256_after`, i.e. the file was changed outside the writers since the previous record;
the write is never blocked. A file's first record is false. `prior_blob` (sha256 or null):
for whole-file writes (`write_document`) over an existing file, the prior content is stored
once, content-addressed, at `<state_dir>/ledger-blobs/<sha256>` (dir 0700, files 0600, never
inside a registered corpus root, deduplicated), so the record plus the blob undoes the write;
null for a brand-new file and for typed writers (their record carries the byte-exact undo).
When a chain break is found the receipt (compact and full, `EditResult.warnings`, and
`write_document`'s `warnings`) carries "OUT-OF-BAND EDIT: <path> changed outside the writers
since ledger seq N (<ts>); that change is not recorded", and the `ledger` entry gains
`out_of_band: {since_seq, since_ts}`; both are ABSENT on a clean write.

A write whose target REALPATH (resolved on both sides; commonpath, never a string prefix) is under
the OS temp dir, `/tmp` or `/private/tmp` is EPHEMERAL: it appends no record and stores no blob,
and the receipt/result carries `ledger: {status: "skipped", reason: "ledger-skipped-ephemeral"}` (the target is under
the OS temp dir)`, never silently (`ledger.is_ephemeral`; `FACTLATTICE_LEDGER_EPHEMERAL_ROOTS`
replaces the roots, set-but-empty means nothing is ephemeral). That variable is a TEST hook (the
suite sets it empty): never set it in production, because pointing it at real corpus roots
disables their ledger. `lts_register_corpus` accepts a root that resolves under the OS temp dir (a temp sample corpus)
but its response carries `warnings: ["EPHEMERAL ROOT: <root> resolves under the OS temp dir; writes
there are NOT ledgered (no history, no undo record)"]` (default roots; key absent otherwise). A
PERMANENT corpus root must not be ephemeral (SDLC 7.6).

If the prior content of a whole-file write cannot be stored, the record is STILL written, with
`prior_blob: null` and `undo_evidence_missing: "<reason>"` (null otherwise; always present), the
`ledger` entry is `{status: "ok_without_undo", reason: "undo-blob-not-stored", live_detail, live_detail_note}` and `write_document` warns "WRITTEN AND
LEDGERED, BUT THE PRIOR CONTENT WAS NOT PRESERVED": the gap lives in the chain. A ledger file is
never created without a record in it.

#### 4.6.5 The `lts_find` query surface [FIND-QUERY-SURFACE]

Additive, optional parameters on `lts_find`, `lts_find_in_prose` (as POPULATION filters; its `text`
stays a body search) and `lts_aggregate` (so counts equal `lts_find`'s `total_matched`). A call that
uses none of them returns exactly the response it always did. Nothing is parsed out of `text`, and no
string query syntax exists: a human form compiles to these parameters.

- **Terms:** `all_of` (every term), `any_of` (at least one), `none_of` (no term). An element is a string
  (using `match`) or `{term, match}`. Fields searched are `text`'s: the id, the title, every string
  property value and every string element of a list (never body, never a mapping); `fields` restricts them
  (terms only). `match`: `phrase` (default, a case-insensitive substring of ONE field), `word` (the term's
  `[a-z0-9]+` words are whole, contiguous and in order in ONE field; no stemming, no stop-words; `go-live`
  hits `go live`), `prefix` (some whole word starts with the term; one word, at least 2 characters).
  A `none_of`-only call, empty strings or lists, and malformed terms are refused with an error naming the
  parameter.
- **`where`:** ANDed conditions `{key, <one operator>}`: `has`, `lacks`, `equals`, `one_of`, `not_equals`,
  `not_one_of`. A list-valued key matches on ANY element (`not_one_of` = any element outside the set). `not_*`
  never match a node lacking the key. Equality is exact (trimmed) and CASE-SENSITIVE; values must be strings.
  Keys follow `^[a-z][a-z0-9_-]*$`. Inside `where`, `status` is plain equality (no lifecycle widening).
- **Document filters:** `source_file` (exact, needs `corpus`; unknown or multi-root-ambiguous paths are
  refused) and `document_tag` (exact, case-sensitive; untagged nodes never match).
- **`query_interpreted`:** present iff a new parameter (or `explain: true`) is used: the text, each term with
  its mode, words and fields, each `where` condition with `key_present_in_scope`, the filters as applied and
  the combine rule. **`warnings`:** a list of strings, present only when one fires (e.g. no node in scope
  carries a `where` key: a likely misspelling). `extent.keys_consulted` gains the where keys and term fields.
- **Phrase-zero fallback `matched_all_words`:** with `LTS_FIND_WORD_FALLBACK=on` (default off; enabling it
  live is a maintainer ruling), a `text` query with exact `total_matched == 0` in a named corpus, at least two words
  after stop-word removal and a non-id-shaped text, adds `matched_all_words: {kind: "word-and-fallback",
  is_exact_match: false, is_assertion: false, note, query_words, total, nodes: [{rank, id, title}], capped}`
  (BM25 order, cap 20): nodes holding every query word as a whole word anywhere in their structure, under every
  other filter of the call. The exact fields are unchanged; the key is absent unless triggered; the query log
  records it as `word_fallback: {total, ids, capped}`, never in `result_ids`. Internal callers pass
  `word_fallback=False`.
- **Query log:** new term and condition VALUES are logged as digest + length unless
  `FACTLATTICE_QUERY_LOG_LITERALS` is set; keys, operators and modes are always logged.
- **Cache ceiling `LTS_FIND_CACHE_MAX_MB`:** the per-process word/prefix caches are bounded by an estimated
  byte total, in MB, ASCII digits only, read on every call. Default 150 (about 150 MB of CACHE per process; one process
  runs per agent session). `0` caches nothing (also trims what is already warm). Above 4096 it warns once on
  stderr and clamps to 4096; any other bad value warns once and uses 150. Least-recently-used corpora are
  evicted to fit (also on the next call after the value is lowered); a corpus whose estimate alone exceeds the
  ceiling serves its call and is not retained, so each later word/prefix/phrase call on it rebuilds the cache
  (about 17x slower per call on a 9,661-node corpus: ~230 ms against ~14 ms).
  **Ruled 2026-10-05 by the architect** (acme-team ISSUE-SEMANTIC-DESIGN,
  `cache-ceiling-ruling-2026-10-05`): keep the default at 150. A cache ceiling bounds the CACHE, not whole
  processes. Measured at the plateau (word+prefix on all 14 v3-snapshot corpora, `footprint`, load average
  ~4-5), one session's lts-engine is ~265 MB at the default, so six sessions are ~1.6 GB together, plus the
  shared embedder: an accepted known cost. Do not lower the default without a new measurement: at 100,
  acme-docs (~94 MiB) and a second corpus evict each other on alternating word queries (median
  12.6 -> 135.6 ms). Results are identical at any
  ceiling. Rule: `SDLC.md` [FIND-QUERY-SURFACE-6].

Implementation: `factlattice_engine/query_surface.py` (compile, validate, match, echo), evaluated in
`tools._node_matches`. Tests: `tests/test_query_surface.py`, `tests/test_query_surface_parity.py`.

#### 4.6.6 Renaming and removing a property line [RENAME-REMOVE-PROPERTY]

The typed writers `lts_add_property` (add or replace a value), `lts_add_node` and
`lts_repair_property` (rewrite a line that does not parse) cannot change the KEY of, or delete, a line
that already parses. Two further typed writers do, on the same machinery (per-file lock,
compare-and-swap atomic write, backup, the node-change ledger of §4.6.4, the whole-file value-aware
gate):

- `lts_rename_property(path, node_id, key, new_key, line?, allow_edge_change?, receipt?, proposer?,
  approver?, reason?, via?)` renames the key of ONE line on ONE node. Only the key region of that line
  changes; the value, the rest of the file and every line ending are byte-identical. `new_key` is
  normalised exactly as `lts_add_property` normalises a key, and every coercion is in the receipt;
  `key` is not coerced. The gate: the whole-file parsed delta is EXACTLY {that node: `key` loses that
  one value, `new_key` gains the identical value}.
- `lts_remove_property(path, node_id, key, line?, expect_value?, allow_edge_change?, receipt?, ...)`
  removes ONE line and EXACTLY the wrapped tail the parser attributes to its value (`parser._fold_continuation`, the
  parser's own rule): a sub-bullet, ordered item, quote, table row, rule, heading or fence opener under the value is NOT part
  of it, however deeply indented, and stays in the file. `expect_value`, when given, must equal the line's PARSED value
  exactly. Two gates: the whole-file parsed delta is EXACTLY that one value gone from that node, AND a BYTE-LEVEL gate
  (`gate_failed`) requires the new file to equal the old file minus exactly the lines the parser attributes to the target
  (rename: with only the key span of the line changed). The parsed-property gate alone cannot see a body line vanish.

Both refuse, writing nothing. In the reply the top-level `reason` IS the fixed code (the same value as `evidence.refusal`;
these two writers have no legacy pinned sentence, so they follow [TOOL-REASON-IS-A-FIXED-CODE] and are not on its exemption
list), the human sentence is `explanation` (path-free), and any text that may name a path is `live_detail` with its NEVER-persist
note. The codes: `node_not_found`, `key_absent`,
`key_ambiguous` (the key occurs more than once and no `line` was given; the lines are in
`evidence.lines`), `line_not_key_line`, `key_nested_block` (v1 does not touch a key that heads a nested
list or mapping), `key_lines_unreconciled`, `key_line_unlocatable`, `edge-context-incomplete`, `expect_value_mismatch`
(remove), `new_key_exists` (rename; there is no silent merge), `new_key_same`, `new_key_illegal`,
`edge_change_refused`, `edge_change_outside_node`, `gate_failed`, `bad_argument`, `not_a_file`,
`metadata_invalid`, `os_error`, `not_utf8`, `duplicate_node_id` (the parser keeps the LAST of two nodes with one
id, so the first would be invisible to the gate; both heading lines are in `evidence.lines`), plus the lock codes
`lock_busy` / `lock_unavailable` and `write_refused` for any other refusal. For the two lock codes `reason` is the
code and the text, which may name the state directory, is in `live_detail` with a `live_detail_note` (never
persisted). The EDGE RULE is decided on the PARSED edge multiset before and after (not on key names): an edge
removed or added is refused unless `allow_edge_change` is true, and the receipt then lists `edges_removed` /
`edges_added`. A rename between two keys that mint the same edge moves none.

What the edge rule covers is stated in every receipt as `edge_check`. The adapter resolves the file's corpus
through the same membership path as `root_membership`, loads that corpus's registered profile, and parses BEFORE
and AFTER with the profile's `property_edges` plus the built-in tables, so a profile-registered edge key (the
widgetkit profile's `documents` -> DOCUMENTS) is protected like `see-also`: `edge_check: profile applied:
<profile file> (corpus <id>) (its property_edges plus the built-in tables)`. When no profile can be determined
the receipt and a warning carry `edge_check: built-in tables only (no profile applied: <code>)`, <code> being
`outside_every_corpus`, `registry_unavailable`, `profile_file_missing`, `profile_load_failed`, `corpus_not_in_registry`
(a corpus seated in the process but absent from the registry file). Each of the first five degrades THAT corpus (or, for
`registry_unavailable` and `outside_every_corpus`, the whole check) to the built-in keys, and says so; in a file that belongs to
several corpora the other corpora's profiles are still applied (`edge_check` then reads `...; NOT applied: <corpus> (<code>)`).
`corpus_registers_no_profile` (the registry entry has no `profile`) means nothing to apply: no warning, no refusal. A file in several
corpora collects every profile it CAN load and records each one it cannot; it never stops at the first failure.

FAIL CLOSED: when a corpus that contains the file has an undeterminable profile (`profile_file_missing`,
`profile_load_failed`, `corpus_not_in_registry`), or when there is no resolvable registry at all (`registry_unavailable`: we
cannot know whether the file belongs to a corpus with a profile; `evidence.corpora` is then `[{corpus: null, code:
registry_unavailable}]`), a rename or remove whose key could be an edge key under any
vocabulary is REFUSED with `edge-context-incomplete`. The rule is exactly this: a built-in minting key in `key` or
`new_key`, or an `[ID]` reference in the value (a profile key mints only from `[ID]` references). PRECEDENCE: a change
that really moves an edge under the vocabulary that WAS determined is refused first as `edge_change_refused`, which takes
precedence over `edge-context-incomplete`; the latter covers the moves that cannot be seen. `evidence.corpora` names each corpus and code (`corpus: null` for `registry_unavailable`); `allow_edge_change` lifts both. A plain key still proceeds, with the
receipt note and a warning. `outside_every_corpus` is NOT incomplete: the registry resolved and no corpus contains the file, so
no profile can apply and the built-in check is complete. A caller of the core
functions (`edit.rename_property`, `edit.remove_property`) that passes no `edge_configs` gets
`built-in tables only (no profile applied: not_resolved)`.

`lts_add_property` VALUE CHECK. After the whole-file gate, an add or an append (not a replace) is refused, writing nothing
(`gate_failed`, "the stored value ... is not the requested one") unless the value stored on the node, AS THE PARSER READS IT,
equals the requested value as the parser reads the single line `- key: value` (for an append, the earlier items must also be
unchanged). It catches an insertion that makes the new value absorb neighbouring lines; it does not catch one that only
separates an indented block from its head. Where the line lands is unchanged from earlier versions and has known defects
(ISSUE-INSERT-IN-FENCE, ISSUE-LINE-SEPARATOR-TRUNCATION).

LINE ENDINGS DIFFER between the writers. These two edit one line (rename: only its key span) or remove one line with the wrapped tail the parser folds into it (remove), and keep every other byte (CRLF, a lone CR, U+2028,
U+000C, U+0085, a missing final newline). `lts_add_property`, `lts_add_node` and `lts_repair_property` read with
universal newlines and re-join with LF, so they normalise the whole file (and turn a U+2028 or U+000C inside a prose
line into a line break). Mixing the two kinds of writer on one file therefore normalises it at the first old-writer
call; the old writers are unchanged here. The ledger records verb `rename_property` (`discarded`: the line before, `inserted`: the
line after) or `remove_property` (`discarded`: the removed lines verbatim). A refusal is never
compacted. Both are reachable through `python -m factlattice_engine call <tool> --allow-write`.

### 4.7 ANALYTICS Layer [LTS-ANALYTICS]

- module: `factlattice_engine.analytics` (`LtsAggregator`)
- mcp-tools: `lts_aggregate` (op = count | group_by | numeric | sum | avg | min | max |
  distinct | cross_tab | top_k | degree | top_by_degree | count_by_edge_type | orphans)
- contract: a scalar / histogram / matrix answer over the graph. A DETERMINISTIC, no-LLM
  fold over the QUERY layer's OWN `find` (Tier 1) and over the same fresh partitions'
  `iter_edges` (Tier 2, graph-native degree / edge-type / orphans). It accepts the full
  QUERY filter surface verbatim.
- guarantees (MUST hold): **completeness** — the fold drains `find`'s token-budget cursor
  so an aggregate sees EVERY matching node, never a truncated slice; **freshness** — a
  not-found / STALE result from the QUERY layer is propagated VERBATIM, so a stale corpus
  never yields a silently-wrong count ([INV-LTS-FRESH]); **honesty** — a numeric fold
  SKIPS non-numeric/absent values and reports `skipped`, never coercing a missing metric
  to 0. Because it composes over QUERY, it inherits the freshness gate by construction.

### 4.8 LINT Layer [LTS-LINT]

- module: `factlattice_engine.lint` (`lint`)
- mcp-tools: `lts_lint`
- contract: structural + semantic/style validation of a `.lts` corpus. Structural
  diagnostics (parse_error, collisions, dangling, unregistered_ns, unresolved_target) come
  from the parser's own `Diagnostic`s, severity-tagged; the bidirectional dangle-lint adds
  forward (a `[PREFIX-NAME]` ref defined nowhere) and orphan (a defined node nothing
  references) findings. A single bare directory is linted recursively as ONE corpus (full
  cross-file dangle resolution).
- guarantee (MUST hold): the linter consumes the engine's OWN parser and re-implements no
  extraction or dangling rule, so **lints-clean ⟺ ingests-clean** by construction — one
  code path, no second parser to drift from the graph the QUERY/ANALYTICS layers read.
  This realizes the parser-linter alignment goal.

### 4.9 RENDER Layer [LTS-RENDER]

- module: `factlattice_engine.render` (`walk_flow` + `render_mermaid`)
- mcp-tools: `lts_flow`
- contract: a queried SUBGRAPH projected to a diagram. `walk_flow` selects the subgraph
  (membership via `CONTAINS`/`PART_OF` to establish which nodes belong; sequence via
  `BEFORE`/`SEQUENCE`/`CAUSES`/`PRECONDITION_FOR`/`ENABLES`/`CASCADES_TO` to establish
  order), and `render_mermaid` emits it. The emitter is subgraph-GENERIC: its contract is
  `render(nodes, edges) -> diagram`, so a process flow, a supersession chain, a dependency
  tree and a decision lineage all render through the same code, selected only by the
  `edge_types` filter passed to the walk.
- guarantees (MUST hold): **derived, never stored** — nothing is written; the diagram is
  produced from the live graph at request time and therefore cannot drift (§15);
  **freshness** — `lts_flow` routes through the same gate as QUERY, so a STALE corpus
  returns the stale marker and NO diagram at all; **no phantom nodes** — an edge with an
  endpoint outside the rendered node set is dropped and reported, never emitted;
  **determinism** — node and edge order follow the walk's discovery order, so the same
  subgraph produces byte-identical diagram text.
- membership is not drawn: `CONTAINS`/`PART_OF` define the flow's node set but are returned
  separately as `membership_edges`, because a container-to-step arrow is not a step
  transition. `AFTER` is deliberately excluded from the default sequence set (`A AFTER B`
  runs B→A in flow order, so including it would silently invert arrows); a caller who wants
  it passes it explicitly and accepts the authored direction.

---

## 5. Behavioral Notation: Flows and Scenarios

Two complementary behavioral notations live in this section:

- **Flows** (§5.1–§5.4) describe ordered actor-action sequences. They answer "how does this process happen?"
- **Scenarios** (§5.5) describe atomic behavioral acceptance criteria in WHEN/THEN form. They answer "for what triggering condition does what outcome hold?"

A Flow describes a path. A Scenario describes a contract. Both attach behaviorally to system specifications; both can be authored by Intelligences and consumed by acceptance-test generators.

### 5.1 Basic Flow Format

```
### [FLOW-ID] Flow Title

**Actors**: Actor A, Actor B
**Preconditions**: conditions that must hold
**Steps**:
1. Actor A → Actor B: action description
2. Actor B: validate something
**Security Checks**:
- Step 2: specific invariant enforced
```

### 5.2 Extended Step Fields

| Field | Required | Purpose |
|-------|----------|---------|
| `actor` | Yes | Who performs this step |
| `action` | Yes | What happens |
| `inputs` | No | Data consumed |
| `outputs` | No | Data produced |
| `security_checks` | No | Security invariants enforced |
| `note` | No | Human-readable context |
| `next` | No | Explicit next step (overrides sequential default) |
| `condition` | No | Decision predicate (marks step as decision point) |
| `if_true` | No | Step ID when condition is true |
| `if_false` | No | Step ID when condition is false |
| `branches` | No | Multi-way branch (>2 outcomes) |
| `converges_to` | No | Step ID where branches rejoin |
| `impl` | No | Implementation anchor: `file.py:function_name` |

### 5.3 Decision Points

**Binary:**
```
### Step 3: Check Dimensions
- **actor**: Pipeline
- **condition**: image dimensions >= minimum threshold
- **if_true**: Step 4
- **if_false**: Step SKIP
- **impl**: pipeline.py:_validate_dimensions
```

**Multi-way:**
```
### Step 4: Select Mode
- **condition**: manifest state
- **branches**:
  - fresh (manifest current): Step 10
  - incremental (config unchanged): Step 5
  - full (all other cases): Step 6
```

### 5.4 Implementation Anchors

Format: `filename:symbol` — references code by stable identifiers (function names, class methods), not line numbers.

**Parse-time resolution:** Parser searches configured source roots, locates symbol definition, resolves current line number. The authored anchor (`file:symbol`) and resolved location (`file:line`) are both carried. Source files are never modified.

**Stale anchor detection:** If symbol cannot be resolved (renamed, moved), step is flagged `impl_stale: true`.

### 5.5 Scenario Notation

A Scenario is a structured behavioral acceptance criterion in WHEN/THEN form. Scenarios make verifiability explicit at the spec layer: each Scenario states one triggering condition and one resulting expectation, both phrased so an acceptance-test generator (BDD, integration test, contract test) can wire to them mechanically.

Scenarios are first-class FactLattice nodes. They live nested under the requirement, capability, flow, endpoint, or interface they describe.

#### 5.5.1 Basic Scenario Format

```
### [SCEN-ID] Scenario Title (one-line case description)
- when: condition under which the expectation should hold
- then: outcome that the consumer can verify
```

Example:

```
### [SCEN-PATCH-EXTID-PRESERVES-UUID] PATCH externalId in place preserves userUuid
- when: a record exists with userUuid U and externalId E1, and a caller PATCHes that record with externalId E2
- then: the stored externalId is updated to E2 and the record's userUuid remains U
```

Required properties: `when`, `then`. Both MUST be present. A Scenario node missing either is flagged by the linter.

#### 5.5.2 Multi-Condition AND

For Scenarios with multiple conjoined conditions or expectations, use `and-when` and `and-then` (each instance adds one conjunct):

```
### [SCEN-EXAMPLE] Title
- when: triggering condition A
- and-when: triggering condition B
- and-when: triggering condition C
- then: expected outcome 1
- and-then: expected outcome 2
```

Alternatively, the nested-property form (P4) collapses multiple conjuncts into one list:

```
### [SCEN-EXAMPLE] Title
- when:
  - triggering condition A
  - triggering condition B
- then:
  - expected outcome 1
  - expected outcome 2
```

Both forms are valid. The compact `and-when`/`and-then` form is preferred for two- or three-conjunct Scenarios; the nested-property form is preferred for longer conjuncts where readability matters.

#### 5.5.3 Optional Preconditions

A Scenario MAY include a `given` precondition that narrows applicability without forming part of the trigger. `given` describes invariant context; `when` describes the triggering event:

```
### [SCEN-EXAMPLE] Title
- given: tenant T has an active subscription and at least one prior record
- when: a caller adds a record supplying externalId E
- then: the record is created and the response echoes E
```

`given` is optional. A Scenario without `given` applies to all states consistent with its parent's preconditions.

#### 5.5.4 Attachment to Parent Nodes via Containment

A Scenario attaches to its parent (the requirement, capability, flow, or endpoint it verifies) via **structural section nesting** (Section 2.1, Rule E5). The parent is an h2 or h3 node; Scenarios sit as h3 or h4 children:

```
## [REQ-PATCH-OPERATIONAL] In-place PATCH for operational attributes
...requirement body with normative SHALL language...

### [SCEN-PATCH-EXTID] PATCH externalId in place preserves userUuid
- when: ...
- then: ...

### [SCEN-PATCH-CRYPTO-REJECTED] PATCH of cryptographic-state field rejected
- when: ...
- then: ...
```

The parser emits a CONTAINS edge (entity layer, Rule E5) from each Scenario's parent to the Scenario. The graph index records `parent_section` on each Scenario node. Reverse traversal from the parent finds its Scenarios; forward traversal from a Scenario finds its parent. No new edge type is required.

A Scenario MAY also explicitly reference its parent via the `part-of` relationship-bearing property when the structural nesting is ambiguous (e.g., a Scenario file kept separately from the requirement file for organizational reasons):

```
### [SCEN-X] Title
- part-of: [REQ-PATCH-OPERATIONAL]
- when: ...
- then: ...
```

The PART_OF edge is semantic-layer. When both structural CONTAINS and explicit `part-of` are present, the parser produces both edges; they reinforce each other.

#### 5.5.5 Parser Behavior for Scenarios

The parser recognizes Scenario nodes through any of these signals (any one is sufficient):

1. **Type prefix** registered in the domain profile as a Scenario prefix (e.g., `SCEN-`). Domain profiles MAY declare `scenario_prefixes: [SCEN, SC]` in their `change_tracking` or `node_types` section.
2. **Property pattern**: the node has both `when` and `then` properties.
3. **Section type tag**: the parent document or section declares `section_type: scenario` in frontmatter.

Once detected, the parser:
- Emits a `node_kind: scenario` marker in the graph index `properties` map
- Validates that `when` and `then` are both present (warns via linter if missing)
- Resolves `and-when` and `and-then` as array entries (multiple occurrences collected)
- Treats `given` as optional context (no validation if absent)

Scenarios participate in `content_hash` computation like any other node; their hash covers the `when`/`then`/`given`/`and-*` properties plus the title.

#### 5.5.6 Why No VALIDATES Edge

Earlier drafts considered introducing a `VALIDATES` core edge type for the Scenario-to-parent relationship. VALIDATES was rejected per Section 1.4: it lacks source-material provenance (no domain had converged on it organically; it would have been retrofit-only) and core edge types are immutable per Decision 3.

The existing CONTAINS (structural) and PART_OF (explicit) relationships carry the load adequately. A consumer asking "what Scenarios verify [REQ-X]?" runs a reverse CONTAINS traversal from REQ-X, filters to nodes with `node_kind: scenario` in their properties, and returns the set. The traversal is O(parent's children).

If, over time, three or more domains converge on a need for a dedicated VERIFIES or VALIDATES edge, the frequency-driven promotion process (§16) can elevate it. Until then, CONTAINS + PART_OF + node_kind = scenario is sufficient.

#### 5.5.7 Scenarios in Versioning (DELTAs)

Scenario changes are first-class participants in DELTA documents (Appendix: Versioning):
- A Scenario added on an existing requirement node is an **additive** change for that requirement.
- A Scenario removed from an existing requirement is a **breaking** change (an acceptance criterion was retired without replacement).
- A Scenario's `when` or `then` content modified is **breaking** if it changes the acceptance contract, or **correction** if it fixes an earlier mis-stated expectation.
- A Scenario's title reworded with confirmed semantic equivalence is **cosmetic**.

Audience derivation routes Scenario changes preferentially to QA-engineer, test-author, and integration-tester roles. Domain profiles MAY register additional audience routing rules for Scenarios in their `change_tracking.role_vocabulary` (Appendix §7.6).

#### 5.5.8 What Scenarios Are Not

- Scenarios are **not** flows. A Flow describes an ordered actor sequence; a Scenario describes one trigger-outcome pair. A complex multi-step interaction is a Flow; the acceptance criteria for that Flow are a set of Scenarios attached to it.
- Scenarios are **not** test code. A Scenario is the normative acceptance criterion; the implementation test is downstream of it (and may live as an `impl` anchor on the Scenario node, analogous to Flow steps' `impl` field — Section 5.4).
- Scenarios are **not** examples in prose. Prose examples illustrate intent; Scenarios are parser-extracted, hash-tracked, and audience-routable structured nodes.

---

## 6. Dynamic Link Expansion (Computed Edges)

### 6.1 Concept

Computed edges are generated at graph build time by scanning artifacts outside the FactLattice corpus — test files, source code, CI manifests — and linking them back to FactLattice nodes.

### 6.2 Expansion Manifest

```json
{
  "expansion_version": "1.0",
  "generated_at": "ISO-8601",
  "rules_applied": ["test_file_scan", "source_docstring_tags"],
  "edges": [
    {
      "edge_type": "TESTED_BY",
      "source_node": "[ALG-LATENCY]",
      "target": "tests/test_latency.py::TestLatency",
      "confidence": "high",
      "rule": "test_file_scan",
      "evidence": "File name matches node ID pattern"
    }
  ]
}
```

### 6.3 Authoring Rules

1. **FactLattice stays authoritative.** Computed edges MUST NOT contradict authored edges. Authored edges take precedence.
2. **Not authored in `.lts` files.** Computed edges MUST live in the expansion manifest, injected at build time.
3. **Provenance is mandatory.** Every computed edge MUST carry rule, confidence, and evidence metadata.
4. **Reviewable before ingestion.** The manifest SHOULD be a CI-checkable artifact.

---

## 7. Confidence Annotations

### 7.1 Core Attribute

Any node or edge may carry a confidence annotation:

```yaml
confidence:
  value: 0.85              # 0.0–1.0 float
  blast_radius: high        # low | medium | high
  rationale: "Based on 3 verified implementations"
  assessed_by: "domain-architect"
  assessed_at: "2026-03-28T20:00:00Z"
```

The `value` field is a float from 0.0 to 1.0 — not an enum. This enables quality tools to sort and prioritize with granularity that an enum cannot.

The attribute schema is core. The methodology for assessing confidence is domain-specific, registered in domain profiles.

---

## 8. Knowledge Evolution

### 8.1 The {change, context_for_change} Primitive

A first-class FactLattice concept for encoding how understanding changes:

```yaml
change: "Description of what changed"
context_for_change:
  prior_state: "What was believed/decided before"
  reasoning: "Why it changed"
  rejected_alternative: "What other option was considered"
  counter_argument: "The strongest argument against this change"
  counter_response: "Why the counter-argument fails"
  invalidating_assumption: "What premise, if changed, warrants reopening"
  implication: "What downstream effects this has"
```

**Anti-pattern prevention:** Without reasoning chains, an Intelligence can be weaponized to argue against a settled decision because it lacks the context that produced it (the "debate amplifier" anti-pattern). The `invalidating_assumption` field is the defense — it tells a future Intelligence: "this decision hinged on X. If X still holds, defend it. If X changed, reopen it."

### 8.2 The {task, context_for_task} Primitive

A companion primitive for encoding task context that survives handoff:

```yaml
task: "Description of what needs to be done"
context_for_task:
  origin: "Where this task came from"
  design_constraints: "What must be preserved"
  traps: "What went wrong before or could go wrong"
  depends_on: "What must complete first"
  success_criteria: "How to know it's done"
  confidence: 0.85
  confidence_rationale: "Why this confidence level"
```

### 8.3 DELTA Documents

A DELTA is a **FactLattice document** (`.lts`) that records the semantic meaning of a change. It is not JSONL. It is not metadata. It IS knowledge — the change history of a system is part of understanding the system.

**Two systems, complementary:**
- **Version control** (git) records the mechanical change — what bytes changed, when, by whom. Auditable record.
- **DELTA** records the semantic change — which nodes affected, why, blast radius, invalidating assumptions.

Git tells you *what* changed. The DELTA tells you *what it means.*

**Document structure:**

```
# Change Record 2026-03-29-001 [DELTA:CR-2026-03-29-001]

## Summary
- commit: abc1234
- timestamp: ISO-8601
- files-touched: 3
- nodes-modified: 2
- estimated_highest_impact: breaking

## Node Changes
### [CRED-TOKEN] — modified
- change-type: modified
- suggested_impact: breaking
- possible_audience: [mobile-dev, security-reviewer]
- content-hash-before: sha256:...
- content-hash-after: sha256:...
- context_for_change:
    prior_state: "Token issuing used the legacy signing transport"
    reasoning: "New transport protocol required for multi-device support"
    rejected_alternative: "Wrapping legacy transport — adds latency, doesn't solve root cause"
    counter_argument: "Breaking change disrupts all mobile SDK consumers"
    counter_response: "Mobile SDK 3.x already abstracts the transport layer — update is internal"
    invalidating_assumption: "That mobile SDK 3.x abstracts the transport. If SDK 2.x consumers exist, this is more disruptive than estimated."
    implication: "All flows that depend on CRED-TOKEN need transport-aware test coverage"
- property-diffs:
  - REWORDED: definition (old → new)
  - ADDED: transport-protocol

## Edge Changes
- ADDED: [FLOW-TOKEN-REQ] → DEPENDS_ON → [CRED-TOKEN] (semantic)

## Impact by Audience
| Role | Changes Visible | Estimated Impact | Suggested Action |
|------|----------------|-----------------|-----------------|
| mobile-dev | CRED-TOKEN properties | breaking | Update SDK signing flow |

## Estimated Blast Radius
- [FLOW-TOKEN-REQ] ← DEPENDS_ON ← [CRED-TOKEN] (depth 1)
- [SYS-IDX] ← DEPENDS_ON ← [FLOW-TOKEN-REQ] (depth 2)
```

**Impact classification vocabulary:**

| Impact | Meaning | Consumer Action |
|--------|---------|-----------------|
| `breaking` | Existing consumers will malfunction | Must update |
| `additive` | New content, existing behavior unchanged | Should update tests |
| `deprecation` | Marked for future removal, still functional | Plan migration |
| `cosmetic` | Rewording/reformatting, semantic equivalence confirmed | No action |
| `correction` | Fix to prior error | Verify not depending on the wrong behavior |

**File naming:** `deltas/DELTA-{date}-{short-sha}.lts`

**Authoring model (Decision 9):** On-demand analysis is the primary mode. The Intelligence making the change also authors the semantic analysis — maximum context at minimum marginal cost. Analysis SHOULD be proportional to the change: one sentence for a cosmetic fix, a paragraph for an architectural addition, a full `{change, context_for_change}` block for a breaking change.

The DELTA is part of the check-in, not an afterthought. The Intelligence producing the change produces the analysis in the same session.

Terminology is always suggestive: `suggested_impact`, `estimated_blast_radius`, `possible_audience`. The consuming Intelligence applies critical thinking, not blind trust.

**Push pipeline (optional):** A post-commit hook MAY invoke `factlattice-delta-gen` automatically for batch production. This is a convenience, not the primary mode. The hook MUST skip if only `deltas/` files changed (circularity prevention).

**On-demand queries:** The diff_agent is a subprocess that can be invoked at any time — not just at commit time. Common modes:
- "Diff this feature branch against main"
- "What changed since snapshot N?"
- "What is the blast radius of modifying [NODE-ID]?"

DELTAs are one output format the diff_agent produces. The diff_agent MAY also produce inline analysis, summary reports, or interactive traversal results depending on the query.

**JSONL summary event:** Each DELTA also emits a summary to the event stream for temporal querying:

```json
{
  "type": "corpus_change",
  "id": "CR-2026-03-29-001",
  "date": "2026-03-29T14:30:00Z",
  "commit": "abc1234",
  "highest_impact": "breaking",
  "nodes_modified": ["CRED-TOKEN"],
  "change_record_path": "deltas/DELTA-2026-03-29-abc1234.lts"
}
```

The JSONL event is a summary pointer. The DELTA document is the canonical record.

---

## 9. Namespace Model

### 9.1 Universal Deconfliction

| Surface | Core Example | Domain Example |
|---------|-------------|----------------|
| Edge types | `core.DEPENDS_ON` | `medical.CONTRADICTS` |
| ID prefixes | `[TERM-*]` | `[PT-*]` (medical), `[GOV-*]` (Example), `[EX-*]` (ExampleCorp) |
| Properties | `scope` | `diagnosis` (medical), `policy-ref` (Example) |
| Document tags | `SYSTEM` | `PRODUCT` (ExampleCorp), `AMEND` (Example) |
| Cross-domain refs | — | `[gov.example:SYSTEM:EXAMPLE-GOVERNANCE]`, `[ops.example:SPEC:EX-ROUTER]` |
| Event stream types | `core.confidence_annotation` | `method.session` |

### 9.2 Cross-Domain References

Syntax: `[{domain}:{DOCTYPE}:{NODE-ID}]`

- Intra-domain: `[SYSTEM:EX-ROUTER]` (domain qualifier optional within the owning domain)
- Cross-domain: `[gov.example:SYSTEM:EXAMPLE-GOVERNANCE]`, `[ops.example:SPEC:EX-ROUTER]` (domain qualifier required)

---

## 10. Domain Profile System

### 10.1 Profile Format (YAML)

**Example: Example governance domain**

```yaml
domain:
  id: "gov.example"
  name: "Example Governance"
  version: "1.0"

prefixes:
  - prefix: "GOV-"
    document: "20-example-governance"
    node_type: "Governance nodes"

edge_types:
  - type: "example.ENDORSES"
    layer: semantic
    description: "Agent endorses a shared entry"

properties:
  - name: "policy-ref"
    type: string
    description: "Reference to governance policy document"

document_tags:
  - tag: "AMEND"
    description: "Governance amendment"

traversal:
  default_entry_points: ["DP-LIB-1"]
  primary_layers: [semantic, causal]

roles:
  architect: { aliases: ["System architects", "design authority"] }
  builder: { aliases: ["Implementers", "developers"] }
```

**Example: Medical domain**

```yaml
domain:
  id: "clinical.medical"
  name: "Clinical Medical Records"
  version: "1.0"

prefixes:
  - prefix: "PT-"
    document: "10-patient-records"
    node_type: "Patient record nodes"
  - prefix: "CLIN-"
    document: "20-clinical-observations"
    node_type: "Clinical observation nodes"

edge_types:
  - type: "medical.CONTRADICTS"
    layer: semantic
    description: "Medication or diagnosis contradiction"

properties:
  - name: "diagnosis"
    type: string
    description: "Clinical diagnosis associated with this node"

document_tags:
  - tag: "ASSESSMENT"
    description: "Clinical assessment document"

traversal:
  default_entry_points: ["PT-INDEX"]
  primary_layers: [semantic, temporal]

roles:
  clinician: { aliases: ["Doctors", "physicians", "attending"] }
  nurse: { aliases: ["Nursing staff", "RN"] }
```

Profiles may also include `frontmatter_extensions`, `relationship_notation`, and `change_tracking` sections. See domain-profiles/ for complete examples.

### 10.2 Profile Registration

Profiles are published as `99-{domain}-profile.yaml` in the domain's corpus root. The namespace registry aggregates all profiles for cross-domain resolution.

### 10.3 Profile Loading

Tooling loads domain profiles at runtime. A linter validating a document loads the corresponding profile (e.g., `99-example-profile.yaml`, `99-medical-profile.yaml`) to learn valid prefixes, edge types, and properties. The tool contains no domain-specific knowledge.

### 10.4 Review keys: extension registration and the review output root

A profile may REGISTER review extensions (a `.lts` document adding review processes and rules for the domain, see `factlattice-reviewer`) and may name where reviews of its corpus are written. Both keys exist in every serialisation and read through the same typed accessors (`Profile.review_extensions`, `Profile.review_output`), so the two loaders cannot disagree:

| | `.lts` profile | YAML / JSON profile |
|---|---|---|
| a registered extension | a node `REVEXTREG-<id>` with `- path:` and `- ext-id:` | `review_extensions: [{path: ..., ext_id: ...}]` |
| the review output root | `- review-output: <dir>` on the `PROFILE-DOMAIN` node | `domain: {review_output: <dir>}` |

- **`REVEXTREG-` is the REGISTRATION kind.** `REVEXT-<id>` is the extension document's own root node; the two are different shapes and must not share a prefix (they collided when the files sat together).
- `path` is an absolute path, `~...`, or relative to the directory of the profile file. `ext-id` is the extension's id (`ONE` names the node `REVEXT-ONE`; the full node id is accepted too). Both are required: a node with only one registers nothing.
- A profile that registers nothing is valid: the generic review runs.
- **The look-alike check.** The `.lts` loader projects only the node kinds it knows and skips any other without a word. A registration spelled under another kind (`REVEXT-X`, `REVEXTREG_X`) therefore parses, lints clean and registers nothing. `lts_profile_audit` returns a `review_extensions` section (registered extensions, the validator's findings, the `review-output`, and `unprojected_review_extension`: every profile node that carries `path` and `ext-id` under an unknown kind). The section is ABSENT when the profile registers no extension, names no `review-output` and has no look-alike.
- **Validation** (`factlattice.profile.validate_review_extensions`, reported in `review_extensions.findings`): each `path` must resolve, by realpath, to a readable `.lts` file; each `ext-id` must exist in that file; ext-ids must be unique across the profile (a duplicate names both realpaths); and an extension inside a root of the corpus it serves must not be stamped `lts: "1.1"` unless the host profile declares every `REV*` key it uses, because registering it would make the host fail its own structure gate.
- `review-output` is the per-corpus output root. Its precedence against a site configuration is fixed in code by `factlattice_engine.reviewer.resolve_output`, not here.

---

## 11. Event Streams (JSONL)

### 11.1 Base Class

```json
{"type": "core.entry", "id": "unique-id", "date": "ISO-8601"}
```

Domains extend with their own entry types:
- `example.session_time` — session duration records (Example)
- `method.engineering_log` — development session logs (Methodology)
- `core.confidence_annotation` — epistemological metadata
- `core.corpus_change` — DELTA summary events

### 11.2 Quality Metrics Event Stream

Every FactLattice agent that discovers, reports, or resolves an issue MUST emit structured JSONL events to the quality event stream. This is the data that makes the SDLC credible — not narrative reports, but aggregatable records.

**Finding event** (emitted when an agent discovers an issue):

```json
{
  "type": "factlattice.finding",
  "id": "GAP-DC-001",
  "date": "2026-03-28T17:30:00Z",
  "agent": "factlattice-gap-analyzer",
  "cycle_id": "GAP-2026-03-28-widgets",
  "spec": "34-router-pipeline.lts",
  "implementation": "~/project/src/router/pipeline.py",
  "severity": "P1",
  "category": "SPEC_IMPL_MISMATCH",
  "summary": "VALID_TYPES missing 4 new message types",
  "resolution_status": "open",
  "domain": "ops.example"
}
```

**Resolution event** (emitted when a finding is resolved):

```json
{
  "type": "factlattice.finding_resolved",
  "id": "GAP-DC-001-resolved",
  "date": "2026-03-29T02:23:00Z",
  "finding_ref": "GAP-DC-001",
  "resolved_by": "domain-builder",
  "resolution": "Added 4 types to VALID_TYPES set",
  "cycles_to_resolve": 1,
  "first_pass": true
}
```

**Densification event** (emitted by densifier + reviewer):

```json
{
  "type": "factlattice.densification",
  "id": "DENS-2026-03-28-001",
  "date": "2026-03-28T20:00:00Z",
  "agent": "factlattice-densifier",
  "document": "30-session-lifecycle.lts",
  "tokens_before": 12400,
  "tokens_after": 8200,
  "compression_ratio": 0.66,
  "reviewer_verdict": "PASS",
  "signal_loss_findings": 0
}
```

**Lint event** (emitted by linter):

```json
{
  "type": "factlattice.lint",
  "id": "LINT-2026-03-28-001",
  "date": "2026-03-28T19:00:00Z",
  "agent": "factlattice-linter",
  "document": "20-system-architecture.lts",
  "errors": 0,
  "warnings": 4,
  "info": 3,
  "density_score": 0.91,
  "domain": "ops.example"
}
```

**Aggregatable metrics** derived from the event stream:

| Metric | Query | Value for open-source credibility |
|--------|-------|----------------------------------|
| Total findings by severity | `count where type=factlattice.finding group by severity` | "247 issues caught, 31 at P0" |
| First-pass resolution rate | `count where first_pass=true / total resolved` | "94% first-pass resolution" |
| Mean cycles to resolve | `avg(cycles_to_resolve)` | "1.2 cycles average" |
| Agent effectiveness | `count where type=factlattice.finding group by agent` | "gap-analyzer: 180, linter: 67" |
| Densification savings | `sum(tokens_before - tokens_after)` | "Saved 2.4M tokens across corpus" |
| Quality trend | `count by severity by month` | P0 findings decreasing over time |

**Event stream location:** `{corpus}/events/quality.jsonl` — append-only, one line per event.

**Design rationale:** The review model catches real issues (GAP-001 keystore boundary, get_registry scoping bug, 31 XVII compliance gaps). But the evidence is scattered across discussion posts and gap reports. Structured events make the SDLC's value **measurable and publishable** — essential for open-source credibility and for submissions to standards bodies or academic venues.

### 11.3 Relationship to Graph

The traversal engine can compute edges from JSONL entries back to graph nodes. Event stream data is temporal by nature — it carries `date` for time-indexed querying.

---

## 12. Density Principles

### 12.1 Measurement

Density proxy: count of unique node IDs, property keys, and cross-references per 1000 tokens.
- Well-structured FactLattice: **15–30 addressable concepts per 1000 tokens**
- Narrative prose: 3–8

Documents under 70% of theoretical maximum are candidates for densification.

### 12.2 Compression Transforms

| Transform | Before | After |
|-----------|--------|-------|
| Prose rationale → property | "The reason we chose X is because Y" | `- rationale: Y` |
| Repeated emphasis → property | "This is critical. Really critical." | `- criticality: high` |
| Paragraph → table row | Paragraph explaining one item | Row in comparison table |
| Inline narration → structured notation | "The algorithm first does X..." | Extended flow or YAML block |

### 12.3 Safety Protocol

1. Preserve original as backup
2. Densify using `factlattice-densifier` agent
3. Independent review by `factlattice-signal-loss-reviewer`
4. Verdict: PASS / PASS_WITH_NOTES / FAIL
5. Reviewer biases toward flagging loss — false positive costs a review; false negative corrupts the source of truth

---

## 13. Document Layering

### 13.1 Prefix Numbering System (00–99)

| Range | Tier | Content | Stability |
|-------|------|---------|-----------|
| 00–09 | Foundation | Charter, glossary, methodology | Core (rarely changes) |
| 10–19 | Core Systems | Architecture, system definitions | Likely stable |
| 20–29 | Derived Systems | Data models, context, navigation | Likely stable |
| 30–39 | Process Systems | Protocol flows, orchestration | Evolving |
| 40–49 | Collaboration | Implementation specs | Evolving |
| 50–59 | Quality Assurance | Corpus QA, spec-gap analysis | Evolving |
| 60–69 | Deployment | Test substrates, CI/CD | Evolving |
| 90–99 | Guides & Conventions | Context loading, domain profiles | Stable |

### 13.2 Context Loading Triggers

Documents may include loading trigger tables:

```
| Question Type | Example | Load |
|---------------|---------|------|
| Flow details | "How does token issuing work?" | 32-flow-token-issuing.lts |
```

The `tier:` frontmatter key enables tiered loading strategies for context window management.

---

## 14. Provenance and Footer

### 14.1 Document Footer

```
---
**Version**: 1.0
**Created**: 2026-03-31
**Updated**: 2026-03-31
**Status**: Draft | Operational | Deprecated
**Intelligence**: Claude Opus 4.6
```

### 14.2 Intelligence Provenance

Documents composed by an Intelligence MUST identify the authoring model in the footer:

```
**Intelligence**: Claude Opus 4.6
```

For converted documents:
```
**Original Intelligence**: ChatGPT 5.2
**Converted By**: Claude Opus 4.6
```

### 14.3 Version Coexistence

Version-scoped documents append `-v{N}` to filenames. The parser emits SUPERSEDES edges from v2 nodes to v1 counterparts.

---

## 15. Diagrams

**Diagrams are derived from the graph, never stored in a document.** This is normative. A
diagram is a projection of a subgraph, produced at query time by the RENDER layer (§4.9)
from the current corpus content. Because there is no second copy, there is nothing that can
drift and no diagram-freshness machinery to maintain.

Authoring a diagram means authoring the graph it depicts. A process flow is authored as
step nodes carrying `next-step` / `if-true` / `if-false` / `branches` / `converges-to`
transitions, with the descriptive `condition` and `actor` properties controlling shape and
swimlane grouping; the picture is then obtained with `lts_flow`. The same emitter renders
any node/edge set, so a supersession chain, a dependency tree, or a decision lineage is the
same tool with a different `edge_types` filter.

### 15.1 Hand-embedded diagrams (DEPRECATED)

Earlier revisions of this specification described Mermaid blocks embedded in a document as
"static visualization snapshots", supported the diagram types `graph`, `flowchart`,
`sequenceDiagram`, `classDiagram` and `stateDiagram-v2`, and recommended embedding bracketed
ids in node labels (`KG[Knowledge Graph<br/>SYS-KG]`). That convention is **deprecated**: it
institutionalizes exactly the drift the RENDER layer eliminates, because an embedded diagram
is a copy of relationships the graph already holds.

Parser treatment is unchanged and remains normative for corpora that still contain such
blocks: an embedded diagram is included in a node's content and is **not** parsed for edges.
It contributes no knowledge to the graph. A relationship that exists only inside an embedded
diagram is, as far as every layer of the engine is concerned, not declared at all.

Migration: re-author the relationships the diagram depicted as typed edges, then delete the
block and render on demand.

### 15.2 Rendered output

The RENDER layer emits Mermaid `flowchart` syntax. Node ids are mapped deterministically to
legal Mermaid identifiers and the mapping is returned, so a diagram can be correlated back to
corpus ids. An edge with an endpoint outside the rendered node set is dropped and reported
rather than emitted, because an unknown endpoint in Mermaid silently declares a phantom node.

Other output formats (sequence diagrams, state charts) are not emitted today; the emitter is
generic over node/edge sets, so additional emitters are additive.

---

## 16. Governance

### 16.1 Core Governance

#### Frequency-Driven Promotion Process

The core taxonomy evolves through observed convergence, not upfront design:

**Registry sweep algorithm:**
1. Enumerate all registered domain edge types, prefixes, and properties across all published profiles
2. For each item, count the number of distinct domains using it (or a semantically equivalent variant)
3. Apply the threshold ladder:

| Domain Count | Action |
|-------------|--------|
| 1 domain | Domain-specific. No action. |
| 2 domains | Potential convergence. Noted in the registry. No promotion. |
| 3+ domains, compatible semantics | **Candidate for promotion.** Two paths: (a) promote to core if genuinely universal, or (b) create an intermediate namespace (e.g., `physical.WIRED_TO`) if useful to a cluster but not all domains. |
| 3+ domains, incompatible semantics | Namespaces already deconflict. No action needed — `medical.STATUS` and `engineering.STATUS` coexist without collision. |

**Intermediate namespaces:** When a concept is shared by a cluster of domains but not universally applicable, it gets its own namespace rather than core promotion. Example: `physical.WIRED_TO` is useful to Maker and Example but irrelevant to Medical and Legal. It lives at `physical.*`, not `core.*`.

**Core promotion criteria:** An item is promoted to core when:
- 3+ domains use it with compatible semantics
- Removing it from any one domain would break cross-domain traversal
- It defines a structural mechanism, not domain vocabulary (P4 test)

**Namespace conflict resolution:**
- Same name, different semantics: namespaces already handle this — no action needed
- Same name, same semantics, different domains: candidate for intermediate namespace or core
- Conflicting core proposals: resolved by the core maintainer with published rationale and `{change, context_for_change}` block

**Core spec amendments:** Changes to core edge types, parser contract, or graph index format require:
- Published `{change, context_for_change}` block with `invalidating_assumption`
- Companion DELTA document
- Review period before adoption

#### Non-Normative Common Prefixes Appendix

A companion document (non-normative) SHALL document prefixes observed across 3+ domains, serving as convergence-by-convention guidance for new domain authors. This appendix is auto-generated from registry sweeps.

### 16.2 Domain Governance

Domain-specific, registered in profiles:
- Example: corpus-proposals workflow, governance amendments
- Example: amendment pattern (`amends:` frontmatter)
- Methodology: artifact generation, quality gates

### 16.3 Amendment Pattern

Domains may use separate amendment files with `amends:` frontmatter referencing the base document. This is a cross-domain pattern that may be promoted to core if 3+ domains adopt it.

---

## 17. Tooling

### 17.1 File Extension

`.lts` — FactLattice file. Deliberately unmapped on developer machines so the content is reached through the rendering layer, not a text editor.

### 17.2 MkDocs Plugin

Primary human rendering surface. Provides:
- `.lts` extension support
- Namespaced cross-domain reference rendering
- Domain profile-aware validation
- Confidence annotation display
- DELTA document rendering

### 17.3 FactLattice Agents

| Agent | Purpose |
|-------|---------|
| `factlattice-linter` | Validates against domain profile, namespace resolution, confidence validation |
| `factlattice-densifier` | Domain-aware density optimization |
| spec-editing agent | Cross-domain reference support, namespace-qualified IDs |
| `factlattice-corpus-analyzer` | Frequency-driven convergence detection |
| `factlattice-gap-analyzer` | Domain profile-aware spec-vs-implementation audit |
| `factlattice-diff-agent` | Semantic versioning with {change, context_for_change} |
| `factlattice-profile-validator` | Validates domain profile YAML against schema |
| `factlattice-profile-extractor` | Extracts domain profile from conventions files |
| `factlattice-registry` | Namespace registry generation, frequency analysis |
| `factlattice-signal-loss-reviewer` | Post-densification semantic loss detection |
| `factlattice-delta-gen` | DELTA document generation from graph index diffs |

### 17.4 Agent Event Emission Requirement

Every FactLattice agent MUST emit structured JSONL events to `{corpus}/events/quality.jsonl` for every actionable finding, resolution, or quality measurement. This is not optional — the event stream is how the SDLC demonstrates its value. An agent that finds an issue but doesn't log it structurally has done half the work.

Event types per agent:

| Agent | Emits |
|-------|-------|
| `factlattice-linter` | `factlattice.lint` (errors, warnings, density score per document) |
| `factlattice-gap-analyzer` | `factlattice.finding` (per gap), `factlattice.finding_resolved` (when evaluated) |
| `factlattice-densifier` | `factlattice.densification` (before/after tokens, compression ratio) |
| `factlattice-signal-loss-reviewer` | `factlattice.densification` update (reviewer verdict, signal loss count) |
| `factlattice-corpus-analyzer` | `factlattice.corpus_health` (missing links, drift, orphaned definitions) |
| `factlattice-diff-agent` | `factlattice.finding` (for each change with impact >= additive) |
| `factlattice-delta-gen` | `core.corpus_change` (DELTA summary event) |
| `factlattice-profile-validator` | `factlattice.lint` (profile validation results) |

### 17.5 Traversal Engine (lts-engine)

See Section 4.

---

## 18. Adoption

### 18.1 Greenfield

For a new corpus, adoption is: write a domain profile registering the corpus's prefixes, edge types, and properties; author documents with the `.lts` extension; run the linter and profile validator against them. No prior format is assumed.

### 18.2 Convention Extraction

For an existing corpus whose conventions live as ad-hoc tables or embedded rules, `factlattice-profile-extractor` analyzes the corpus and produces a formal `99-*-profile.yaml`. This turns implicit conventions into a profile that tooling can load and enforce. One-time operation per domain; the profile is maintained directly thereafter.

### 18.3 Cross-Domain Reference Upgrade

Corpora that referenced other corpora by filesystem path migrate to namespaced references (`[domain.ns:TYPE:ID]`). The linter flags filesystem-path references as warnings; fix-on-touch.

### 18.4 Tooling Cutover

Adopting FactLattice means adopting its tooling as a set — linter, profile validator, MkDocs plugin, and the agent definitions — since domain-profile awareness runs through all of them. Partial adoption (profiles without the validating tooling) leaves the profile unenforced.

---

---

## 19. Errata Register — Divergences Between SPEC, Parser, and Corpus

⛔ **NEW 2026-08-16 (Era 0, the engine maintainers), implementing amendment A5 of
the parser's engineering notes (rev 2, not published).** Per the parser's architectural review (not published): this project has three artifacts that each behave as an authority — the
corpus, this SPEC, and the parser — and **no mechanism has bound any pair of them**.
Divergences were managed in *parser comments* ("DELIBERATELY NOT ADDED", "documented
scope choice") — the parser's comment block was the de-facto errata of this SPEC,
unreviewed as such. This section is that errata, made first-class: every found
divergence between what this SPEC says, what `factlattice/parser.py` does, and what
the corpus authors, WITH its classification and the deciding text. It is a REGISTER,
not a resolution — landing a row here does not rule on it; §21 ("Where corpus and
spec disagree, the SPEC wins and the corpus migrates" — the review's canonise/bug
procedure) is Era 1's to apply, row by row, as each is ratified.

**Classes** (review §2): **(a)** parser violates SPEC — a conformance bug. **(b)**
SPEC is silent, the parser decided — underspecification whose de-facto answer was
never reviewed. **(c)** SPEC is the stale/self-inconsistent party. **(d)** a ratified
ruling absent from BOTH SPEC and code — governance record that landed nowhere.

**Maintenance.** §4 of the companion engineering spec defines a clause-derived
conformance suite intended to keep this register live mechanically; that suite is
Era 1 scope and does not exist yet. Until it does, this table is maintained BY HAND,
same as every other section of this DRAFT — landing a row is a reviewed code diff,
exactly as amending any other section here is.

| # | Divergence | Class | Evidence and the deciding text |
|---|---|---|---|
| V1 | Mixed-case/ill-formed refs in property values were silently dropped, with no diagnostic | **(b), now (a)-CLOSING** | §1.1 defines the well-formed id (`[A-Z][A-Z0-9_-]+`); nothing said what happens to an ill-formed one. §2.5's principle (silent loss must be reported) pointed the way but its letter covered lines, not bracket tokens. **Closed 2026-08-16** by `factlattice.parser.classify_bracket_token` + `ParseFinding(kind="malformed_ref")` at every ref-expecting position (§20 below); still **detection only** — a malformed ref still does not bind, per Era 0's no-breakage contract. |
| V2 | Mixed-case Relationships SOURCE silently skipped the whole line | **(b), now (b)-DETECTED** | Same reasoning as V1, source position. Measured population was 0 across the surveyed corpora at review time; still 0 on this repo's own corpus. `classify_bracket_token` now fires a `malformed_ref` finding here too (§20) the moment the population is non-zero; the SILENT half of the behavior (still no edge) is unchanged pending Era 1. |
| V3 | Ill-formed Causal Graph operand silently skipped | **(b), now (b)-DETECTED** | Same reasoning, causal source/target position. Detection landed with V1/V2 (§20). |
| V4 | `- [ID]: description` neither minted (correct) nor flagged (ruled 2026-08-11) | **(d), CLOSED 2026-08-16** | The ruling was in neither this SPEC nor the code. **Now in both**: `KIND_NEAR_MISS_ID_COLON` (`factlattice/coverage.py`) reports the shape as a `near_miss_id_colon` warning (§20); it still does not mint — a guessed key would be a substitution, per the same SL-18 lesson §1.3.1 already cites. The ruling text itself now has a normative home: this row. |
| V5 | `property_edges` unreachable from every production entry point; `domain_verbs`/`profile_layers` partially threaded | **(a), CLOSED 2026-08-16 (Era 1, C3/C4)** | §1.3.1 (⛔AMENDED 2026-08-15): a profile MAY register edge-bearing keys; "the edge type is declared ONCE and simply names the property that mints it." **MEASURED 2026-08-16, more precisely than the review had it**: `property_edges=` is a keyword parameter of exactly ONE function pair (`factlattice.parser.parse_file`/`parse_text`) and is passed by **zero** production call sites — not `factlattice.index_builder.build_index`, not `factlattice.corpus.parse()` (either mode), not `factlattice_engine.incremental.RootIndex`/`CorpusIndex`, not `factlattice_engine.config.build_partition`/`build_merged_partition`. A profile-registered `property_edges` key mints on **no served graph, anywhere**, today. `factlattice_engine.analytics.profile_audit._property_edges_wiring()` is the reusable, re-runnable check (structural, not an edge-count heuristic — see its own docstring for why an edge-count check would false-positive). §1.3.1's own constraint-3 rationale — a registration must not be "inert while reading as functional" — names precisely this failure one level above the constraint it guards. |
| V5b | `domain_verbs` and `profile_layers` (D5) are each threaded on **exactly one** production path, and it is a **different** path for each | **(a), NEW 2026-08-16** | Not previously separated from V5. **MEASURED**: `factlattice.corpus.parse()` (the `lts_lint`/`lts_preview`/`lts_profile_audit` path) threads `Profile.domain_verbs` (a profile's `relationship_verbs.domain`) but **never** passes `profile_layers` to `build_index`/`parse_file` — `factlattice.profile.Profile` carries no `edge_layers` attribute for it to read even if it wanted to. `factlattice_engine.config.build_partition`/`build_merged_partition` (the LIVE MCP query engine behind `lts_find`/`lts_traverse`/a registered corpus's `lts_lint`) thread `profile_layers` (via `domain_profiles.ProfileEdgeConfig.edge_layers`) but that config object carries **no `domain_verbs` field at all**. ⇒ the SAME `(document, profile)` pair produces a **different graph** depending on which production entry point served it: a bare domain verb is unflagged on one path and `unregistered_verb`-flagged on the other; a namespaced type's layer is the profile's declared layer on one path and the `CORE_EDGE_LAYER` default on the other. `lts_lint`'s own docstring claim — "a corpus that lints clean is a corpus that ingests clean — one code path, no drift" — is measurably false for exactly this reason. Reproduced end-to-end (not just by signature inspection) by `factlattice_engine.selfcheck.entry_point_equivalence.run()`, which parses one control `(document, profile)` pair through all three checked production paths plus a fully-threaded reference and diffs the results. **MEASURED at the start of Era 1: 5 divergences across 3 production paths** (Era 0's build report said 3; the higher number is what the instrument actually returns and is recorded in preference to it). **CLOSED 2026-08-16 by C3**: `factlattice.parser_config.ParserConfig` is now ONE constructed value carrying all five hooks, `ParserConfig.from_profile(profile, edge_config)` is the ONE seam where the two profile systems merge, and every production path threads it. The instrument now reports EQUIVALENT and carries `run_negative_control()`, which starves one path and REQUIRES divergence — because once the defect is fixed, an instrument that has never been seen to fail proves nothing. |
| V6 | Audit surfaces report declared keys as in-effect | **(a)-adjacent, PARTIALLY CLOSED** | Follows from V5. `profile_audit._property_edges_section` now carries the truthful split (§20): `minting_keys_in_effect` is KEPT for back-compat but documents what it actually answers ("declared or built-in", not "threaded"); `threaded_keys_in_effect` and `declared_not_threaded` (each entry carrying `FL-W106`, `severity: error`) answer the real question. The instrument's own negative control (`tests/test_profile_audit.py::test_declared_not_threaded_is_the_negative_control`) proves it can report BOTH a true zero (nothing registered) and a true positive (something registered and unreachable). |
| V7 | Heading with only ill-formed brackets silently demotes to a divider | **(b), now (b)-DETECTED** | §2.1 N1 covers the valid form only. Population 0 at review time and still 0 on this repo's corpus; `classify_bracket_token` now scans heading id-candidates too (§20), so a non-zero population would surface as a `malformed_ref` finding instead of staying invisible. |
| V8 | "P5" names two different rules; §2.2 says "four formats" while the parser implements five + continuation folding + id-table promotion | **(c), CLOSED 2026-08-16 (Era 1)** | Resolved in §2.1.1. The document's `P5` row is RENAMED to **P6-version-metadata** and the parser's mapping form takes **P5**, because the parser's name is the one already in every diagnostic, test name and emitted finding — renaming the document costs one line, renaming the code is breaking. §2.2's "four formats" is corrected to five plus two transformations. ⛔ The substance of the finding was never the count: the mapping form was added to the parser as "a CONFORMANCE fix to §2.2" — **a spec amendment executed in a code comment** — and §19 exists so that the next one is a reviewed diff instead. |
| V9 | SPEC §2.1 rules N3, N4, E6-E9 are specified and unimplemented | **(c)/(a), CLOSED 2026-08-16 (Era 1)** | Resolved in §2.1.1 by defining **conformance level 1** (F1, N1, N2, P1-P6, E1-E5) and marking N3/N4/E6-E9 **RESERVED — unimplemented at level 1**. The defect was never the absence of the features: it was that a conformance claim was **unfalsifiable in both directions**, because nothing distinguished a rule a parser MUST have from one it need not. A RESERVED rule stays normative for a future level and is explicitly NOT optional-forever. |
| V10 | Key vocabulary: thousands of distinct keys used, most declared nowhere | **NOT a violation — SPEC-SANCTIONED** (P3's Private tier); **AMENDED 2026-08-16 (Era 1)** | The open key space was P3 and P5 working as written. §1.3.3 (amendment A2) now supersedes them AS APPLIED TO PROPERTY KEYS: declared-or-`x-`-marked, error in strict mode, warning in legacy. ⛔ Recorded as an AMENDMENT and never as a bug fix — the population was licensed behaviour, and misdescribing a change of the format's identity as a defect repair would leave the next reader unable to discover why the rules moved. The CAPABILITY ships in Era 1; no corpus is migrated and no document is stamped, which is Era 2. |
| V11 | Edge verbs: profile-declared "big six" (USES/UPHOLDS/DOCUMENTS/REALIZES/EXEMPLIFIES/GROUNDS) sit dead-threaded on the live engine path; the unregistered tail is preserve+flag (bracket-anchored) or silently dropped (prose-target) | **mixed (a)+(b)** | The big six's dead-threading is V5b's `domain_verbs` half, concretely: these verbs are typically registered via a profile's `relationship_verbs.domain`, which reaches `factlattice.corpus.parse()` but not the live MCP query engine. The prose-target silent-drop half (b) is unresolved by Era 0 (C13, Era 1). |

### 19.1 New Era-0 findings not in the review's original V-table

| # | Finding | Class | Evidence |
|---|---|---|---|
| V12 | A "pure reference list" under a non-minting key is common and was entirely uninstrumented | **(b), now DETECTED** | `factlattice_engine.binding.is_pure_reference_list` + `factlattice_engine.lint.unminted` (`origin=unminted`, `kind=unminted_reference_list`). Measured against the Acme deployment's `corpora/` tree, 2026-08-16: 130 findings across 14 distinct keys (`re-derive-via` 47, `documents` 28, `upholds` 14, `uses` 13, `grounds-claim` 9, ...). Three of those counts (`documents`=28, `upholds`=14, `uses`=13) match the parser engineering notes' (rev 2, not published) §6 predicted per-key breakdown exactly; `grounds-claim` (9 measured vs. 54 predicted) and the total (130 vs. ≈186 predicted) do not, and the gap is UNEXPLAINED by this build — reported, not reconciled. See the Era 0 build report. |
| V12b | V12's detection landed in `lts_lint` (corpus-wide) but NOT in `lts_preview` — the AUTHORING LOOP, the compose→preview→land gate an author actually runs before the corpus exists | **(b), NEW 2026-08-17, CLOSED the same day** | ⭐ **THE SILENT HALF OF V12.** MEASURED before the fix: `lts_preview` reported `LOST total: 0` on a draft carrying `blocks: [X]` and `exemplifies: [X]` — pure reference lists under non-minting keys that mint no edge — because preview's two loss channels (`line_accounting` = a structure-shaped line no branch consumed; `declared_nothing_minted` = a DEFINING-position id that inflated to no node) BOTH miss it: an edge-shaped property value is CONSUMED as a plain string (so line accounting is green) and its bracket is a REFERENCE, not a definition (so the declaration channel is silent). So an author composing in the loop lands the draft on a green preview while the edge they wrote does not exist; the corpus is exactly where the defect was then discovered — the failure `lts_preview` exists to prevent. This is the same shape as V6/V5c one surface over: the CORPUS-WIDE linter saw the loss and the AUTHORING instrument did not, so the loop reports success over a vanished edge (`the epistemics rule`: an instrument that can only report success is not an instrument). **Closed 2026-08-17** by a THIRD preview loss channel, `edge_shaped_property_minted_nothing`, computed from the ONE shared predicate `find_unminted_reference_lists` (never a second copy — the drift V5b/V12 keep re-finding), scoped to the draft's own nodes and resolved against THIS parse's profile, so a profile-registered key that now MINTS (e.g. `documents:` under the acme profile) is correctly NOT reported. Control: `tests/test_preview.py::test_spec41_total_loss_is_fifteen_across_three_named_channels` (the SPEC41 positive control's `ACME-IH-HOMES` carries `pinned-to:` + `exemplifies:`, 13→15) plus a bidirectional negative control (`test_a_minting_key_reference_is_NOT_reported_lost` — `see-also` mints and must never appear). ⛔ **NOT in scope, the maintainer's call (`[DEC-PROFILES-MAY-REGISTER-THEIR-OWN-EDGE-KEYS]`):** WHICH domain verbs (`blocks`, `exemplifies`, `requires`, `re-derive-via` 47, `documents` 28, `upholds` 14, `uses` 13, …) should be profile-registered to MINT rather than merely reported. The engine's obligation — make the silent loss LOUD in the loop — is what closed here; the vocabulary decision is surfaced, not taken. |
| V12c | An edge-shaped property under an UNDECLARED key was reported as one undifferentiated loss — a CASE TYPO of a declared verb (`Depends-On` for `depends-on`) was indistinguishable from a GENUINELY-unknown verb (`frobnicate`), and neither was returned as an actionable de-duplicated list | **(b), NEW 2026-08-17 (the maintainer's two rulings), CLOSED the same day** | **RULING 1 (which verbs mint):** "core verbs as defined in the spec, UNION any corpus-defined extensions." VERIFIED against the authority: `factlattice.parser.REL_PROPERTY_EDGES` equals the SPEC §1.3 relationship-bearing table verb-for-verb (17 keys, same edge type / layer / direction) — no engine change owed for minting. Pinned by `tests/test_repair_and_binding.py::test_engine_core_minting_allowlist_matches_the_spec_1_3_table`, which re-parses the SPEC table and diffs it, so a future drift in EITHER direction fails. **RULING 2 (undeclared handling), with the 2026-08-17 amendment:** an edge-shaped non-minting key is now split by the ONE shared classifier `factlattice_engine.binding.classify_edge_key` (casefold + separator-unify, compared to the EFFECTIVE minting vocabulary = core UNION profile `property_edges`) into **(a) case_variant** — a case/separator variant of a declared verb: a WARN of the exact shape `Case-mismatch {verb1, verb2, …}` (de-duplicated), NEVER silently normalised-and-minted, so the author corrects the source; **(b) undeclared** — matches no declared verb even by case: the positive alert, returned as the de-duplicated `undeclared_edge_verbs` list (the register-vs-typo worklist); **(c) declared** — mints, in neither channel. The distinguishing logic lives in that ONE predicate so `lts_lint` (V12) and `lts_preview` (V12b) cannot disagree; two non-overlapping input sources feed it (PARSED properties for lowercase keys + the UNCONSUMED raw node-body line for the uppercase variant the `^[a-z][a-z0-9_-]*$` key grammar rejects). Output fields, identical on both surfaces: `undeclared_edge_verbs`, `case_mismatch_edge_verbs`, `case_mismatch_canonical` (each miscased key → the verb to fix it TO); lint adds finding kind `edge_verb_case_mismatch` (warning) and preview adds `case_mismatch_warn`. Controls: `test_repair_and_binding.py::test_classify_edge_key_splits_declared_case_variant_and_undeclared` / `::test_edge_verb_channels_dedup_and_route` / `::test_lint_surface_carries_both_channels_and_the_case_warn` and `test_preview.py::test_case_mismatch_of_a_declared_verb_goes_to_the_case_channel_not_undeclared` / `::test_genuinely_undeclared_verb_lists_once_even_used_n_times`. **Still the maintainer/architect's call:** which `undeclared_edge_verbs` become registered profile extensions vs. are fixed as typos — the list is the input to that decision, not the decision. |
| V12d | A BARE reference `[X]` that fails to resolve gave the author no hint that a NAMESPACE-QUALIFIED definition `[NS:X]` exists — it read as an ordinary dangle | **(b), NEW 2026-08-17 (the maintainer RULING C: "C is the right approach … if this *ever* happens (and it has) C is the right solution"), CHANNEL LANDED + ENGINE GAP SURFACED** | The namespace analog of V12c's Case-mismatch: `factlattice_engine.lint.dangle.classify_unqualified_references` splits a bare, unresolved reference by how many namespace-qualified definitions share its bare id — **exactly one → `unqualified_references` {bare → NS:bare}** ("did you mean [NS:bare]?"), **two or more → `ambiguous_references` {bare → [candidates]}** (listed, never guessed), **zero → ordinary forward-dangle** (unchanged). STRICT identity is kept — bare and `NS:bare` are NEVER auto-equated (that "breaks the day two namespaces share a bare id, e.g. `[SPEC:X]` and `[DEC:X]`") — the engine only WARNs so the AUTHOR qualifies the source. Wired identically on `lts_lint` (report `unqualified_references`; findings `unqualified_reference` / `ambiguous_unqualified_reference` at warning; the matched bare targets are routed OUT of the plain `dangle.forward` / `structure.dangling` stream) and `lts_preview` (`unqualified_references` block + `warn`/`ambiguous_warn`), off the ONE resolution the dangle stream already uses — no second copy. ⛔ **MEASURED INERT ON THE CURRENT ID MODEL, and this is the load-bearing finding, surfaced not silently shipped:** `factlattice.parser._heading_id` / `_resolve_ref` keep only the LAST `:`-segment of an id, so `[SPEC:ACME-VISION]` is STORED as bare `ACME-VISION`, the per-node doctype is discarded, and a bare `[ACME-VISION]` **already resolves** to it — there are ZERO namespace-qualified stored ids, so the channel is empty on every current corpus (**measured 2026-08-17: 0 across `~/.config/factlattice/acme/corpora`, against 80 forward-dangles / 12 unresolved-target / 0 id-collisions**), and the ACME-FOUNDATION→ACME-VISION dangle that motivated this was a PARTITION-scope artifact of the live merged query engine, not a namespace one. The classifier's logic is proven by `tests/test_unqualified_reference.py` over constructed namespaced-id inputs, so it is correct the day the **id model preserves the namespace as identity (SPEC §1.1/§9.2) — a strict-identity change that is the maintainer's/architect's call and BREAKING to every existing bare↔qualified resolution.** Until then the wired channel is present, honest, and empty, and this row is where the decision is recorded. |
| V5c | A corpus's OWN REGISTERED PROFILE (`corpus-config.json`'s `profile:` key) reached the SERVED graph through **no path at all** — it was read only by the AUDIT surfaces | **(a), NEW 2026-08-16 (Era 1), CLOSED the same day** | ⭐ **NOT PREVIOUSLY SEPARATED FROM V5, AND IT IS THE ONE THAT MADE THE OTHER TWO UNFALSIFIABLE.** MEASURED before the fix: `spec.get("profile")` was read by `analytics.profile_audit` (:198) and `analytics.profile_registration` (:105) and by **no graph-building path** — `config.build_store` / `build_partition` / `build_merged_partition` used ONLY `engine_profile_config()`, i.e. the SHIPPED EXAMPLE `domain-profiles/` directory (namespaces `art/cln/dec/ext/mkr/swe`). The Acme deployment's own profile, which carries **all 17 of its domain verbs and its ONE `property_edges` key**, was loaded by nothing that builds a graph. ⛔ **WHY THIS IS THE HIGH-STAKES ROW:** fixing V5's threading WITHOUT this would have produced a green wire with nothing flowing through it — the entry-point-equivalence control passes its profile explicitly, so it would have gone GREEN while the real deployment gained **zero** edges. That is precisely a signal AGREEING WITH THE WRONG ANSWER (`the epistemics rule`), and it is the same shape as V6 ("the audit reports declared as in-effect") one level up: here the AUDIT read the registered profile and the GRAPH BUILDER did not, so an operator could be told which profile governed a corpus whose served graph had never seen it. Closed by `factlattice_engine.config.registered_profile` / `parser_config_for`. MEASURED AFTER: +29 source lines minting 44 property-inferred `DOCUMENTS` edges, 1,663 edges losing a now-incorrect `unregistered_verb` flag, **0 edges lost and 0 nodes changed**. |
| V14 | `lts_lint` counted sentence-shaped titles and DECLINED to flag them, asserting that citation-travel *"is the entire harm model"* | **(c) — a normative claim in an instrument, and it was INCOMPLETE**; **AMENDED 2026-08-16 (the maintainer)** | ⛔ **THE CLAIM WAS FALSE AS STATED, AND 454 TITLES WERE BEING WAVED THROUGH ON THE INCOMPLETE HALF.** the maintainer's amendment: a key or id whose name reads as SELF-EVIDENT **suppresses reading the body that carries the real guidance** — his example, a name shaped like `ALWAYS-QUERY-BROADLY`, is easy to skip when busy, because reading the name FEELS like having read the instruction. Nothing has to travel anywhere for that harm to land. So there are **TWO** harms: **(1) CITATION LEAKAGE** — the name ARGUES (signal: a copula), the id gets cited unread; **(2) READ SUPPRESSION** — the name INSTRUCTS (signal: an IMPERATIVE in initial position), and the body goes unopened. ⭐ **THE TWO RULES CARRY OPPOSITE GENRE LICENCES, WHICH IS THE PROOF THEY ARE INDEPENDENT RATHER THAN ONE RULE SPELLED TWICE**: `PRINCIPLE`/`STRATEGY`/`DEC` are LICENSED for (1) — a principle that asserts nothing is not a principle — and are the HIGHEST RISK for (2). MEASURED over 1,900 live nodes: 24 read-suppression findings (3 ids, 22 titles), 7 claim-shaped ids, and **ZERO overlap**; 7 of the 24 sit in families rule (1) licenses. Implemented as `read_suppressing_name` (`lint/claim_id.py`), warning severity, titles now IN SCOPE — a title travels into no edge but into every find-result row and digest listing, which is exactly where skimming happens. ⚠ Stated limit: this is a LEXICAL PROXY for a claim about a reader. The direct instrument would be the query log (nodes cited but never fetched); it is not built, and the gap is named rather than glossed. |
| V13 | A "labeled citation" shape (`- stage-evidence: committed -> [COMMIT-09b80e9]`) is invisible to BOTH the malformed-ref detector (V1, scoped to minting-key values by A4's own closed list) and the unminted-reference-list detector (V12, scoped to PURE reference lists) | **(b), NEWLY NAMED, UNDETECTED** | Measured: 176 malformed-shaped bracket tokens exist across ALL property values in the Acme `corpora/` tree (close to the review's original 164/V1 population); 173 of those 176 sit under NON-minting keys (`re-derive-via` 102, `stage-evidence` 29, `frame`/`chosen`/`recommendation`/`requirement`/... the remainder), and most of THOSE (e.g. every `stage-evidence` line) carry a leading prose label before the bracket, so `is_pure_reference_list` correctly refuses them too (a leading gloss is not a trailing one). Neither Era 0 instrument names this shape; it is real, authored, and currently silent. Left for Era 1: either A4's ref-expecting-position list widens, or this becomes a third, explicitly-scoped detector. |

---

## 20. Amendment A4 (partial) — Malformed-Ref Detection Landed, Era 0

⛔ **NEW 2026-08-16.** Implements the detection half of amendment A4 (§2) of the parser's
engineering notes (rev 2, not published), at Era 0's no-ratification, no-breakage scope. A bracket
token at a REF-EXPECTING POSITION — a minting-key property value (built-in or
profile-registered), a `## Relationships` source/target operand, a `## Causal Graph`
operand, or a heading id-candidate — is now partitioned into **REF | MALFORMED-REF |
PROSE-BRACKET** by `factlattice.parser.classify_bracket_token`:

```ebnf
REF            the last colon-segment matches the well-formed id grammar (§1.1)
MALFORMED-REF  identifier-shaped (letters/digits/_/- only), NOT a well-formed id,
               and carries a "-"/"_" or an upper-initial character
PROSE-BRACKET  everything else the scanner accepts (a bracket containing a space
               is prose by construction)
```

A MALFORMED-REF at a ref-expecting position raises `ParseFinding(kind="malformed_ref")`
— surfaced by `lts_lint` under `origin=malformed_ref`, severity `warning` at this
stage. **What this does NOT do**: it does not reject, does not normalize, and does
not change what binds — every existing document parses to the byte-identical graph
it parsed to before this landed (verified: the parser's own control corpus,
`factlattice/selfcheck/control.lts`, gained exactly one new case — SL-33 — and no
existing case's minted graph changed). Rejection-with-diagnostic (strict mode) and
normalization-with-warning (legacy mode) are C5/A4's Era-1 halves.

Separately, `- [ID]: description` (a bracketed id occupying the KEY position) is
flagged `near_miss_id_colon` by `factlattice.coverage` (the maintainer's 2026-08-11 ruling,
V4 above) — likewise detection only; the property is still not set.

---

## 21. Amendment A6 — Corpus Organization (SPECIFIED, NOT IMPLEMENTED)

⛔ **NEW 2026-08-16 (Era 1). STATUS: SPECIFIED ONLY.** No parser or engine change
accompanies this section, deliberately and by instruction. It is written now because the
requirements were ratified now, and a requirement held only in a conversation is a
requirement that arrives as a surprise. **Nothing in §21 is implemented; a conforming
level-1 parser ignores all of it.** Implementation is Era 2 or later.

These four requirements are the maintainer's (2026-08-16) and appear in none of amendments A1–A5.

### 21.1 Where the scheme is DECLARED — the placement decision, argued

The architect PROPOSED (offered, not ratified) that all four live in the corpus's
**domain profile**. **This section adopts that proposal, and the argument is one
specific mechanism rather than a preference:**

> A domain profile already carries `document_tag: PROFILE`, and §10's profile mechanism
> **already excludes a PROFILE-tagged file from every content graph** — it is parsed in
> full, loaded as vocabulary, and its nodes contribute nothing to any query. That
> exclusion exists, is tested, and is load-bearing today.

The requirement below that *"the prefix NEVER becomes a graph node"* therefore **falls
out of an existing mechanism rather than requiring a new exception.** That is the whole
case. Any other home — a new sidecar file, a frontmatter block, a `corpus-config.json`
key — would need its own bespoke non-nodeness rule, and this specification already
records what happens when content-exclusion depends on a mechanism other than the file's
own intrinsic doctag: `private/corpus-config.json` carried `profile: None` for eleven
corpora for weeks, and 88 declaration nodes answered content queries as a result
(see §1.3.1's neighbouring commentary and `parser.is_profile_document`).

⚠ **THE ONE ARGUMENT AGAINST, STATED RATHER THAN OMITTED.** A domain profile is
VOCABULARY — what symbols mean. Directory layout and load policy are DEPLOYMENT
concerns — where bytes live and when they are read. Putting them in the profile widens
what a profile is, and §10 will need to say so explicitly rather than letting it drift.
**It is adopted anyway**, because the alternative — a second declaration file that must
agree with the first — creates exactly the two-artifacts-no-binding condition §19's root
finding is about. One artifact that is slightly too broad beats two that can disagree.

### 21.2 Per-corpus directory organization

A profile MAY declare the directory structure its corpus expects. **The declaration is
DESCRIPTIVE of an intended layout and MUST NOT be a load-time refusal** — a corpus whose
files have moved must still parse, or the organization scheme becomes an outage.

### 21.3 `_category` subsection frontmatter

A subsection MAY carry a `_category` field grouping it within its corpus.

⚠ **OPEN, and it must be closed before implementation:** the leading underscore is
**outside** §1.3.3's key grammar (`^[a-z][a-z0-9_-]*$` requires a leading LETTER) — the
identical objection that decided `x-` over `_` in A2. Either `_category` is a
FRONTMATTER-only field governed by a different grammar than property keys, or it should
be spelled `x-category` / `category` for consistency. **This section does not decide it**
and must not be implemented until it is; implementing it as written would put a
second, contradictory rule into the key grammar A2 has just closed.

### 21.4 Per-corpus prefix numbering

A profile MAY declare a numbering scheme for its own ID prefixes.

⛔ **EACH CORPUS DEFINES ITS OWN. The SMD tier numbering MUST NOT be copied**, and this
is explicit because copying it is the obvious move and it is wrong: a numbering scheme
inherited from another corpus asserts a hierarchy that corpus's authors never chose,
and P4 ("mechanisms are core, implementations are domain") makes the *mechanism* core
and the *scheme* domain.

⛔ **THE PREFIX NEVER BECOMES A GRAPH NODE.** Per §21.1 this needs no new rule: the
declaration lives in a PROFILE-tagged document, which is already excluded from every
content graph.

### 21.5 Load categories — autoload / on-demand

A profile MAY classify its corpus's documents as **autoload** (read at engine start) or
**on-demand** (read when queried).

⚠ **This is a PERFORMANCE policy and MUST NOT become a correctness one.** An on-demand
document that has not been loaded is still part of the corpus: it MUST be counted, its
ids MUST resolve, and a reference into it MUST NOT report as dangling merely because it
is unread. Otherwise the freshness invariant (§19/`INV-FRESH`) and the dangle-lint would
both answer confidently and wrongly depending on load order — which is the same
entry-point-dependence review V5b measured, one layer up.

---

**This is a DRAFT. Sections are complete for audit but not yet normative. The open-source intent, domain equality, and notation freedom principles govern the entire document.**


> **Disclosure (reviewer-reasons, 2026-10-08).** `HealOutcome.to_dict()` (the `heal` block of a stale refusal) now carries `reason` as one of 14 fixed CODES
> (`self-heal-raised`, `no-rebuild-recipe`, ...) and the original fixed sentence as `explanation`. `lts_list_corpora`'s per-entry `heal_error` display string is
> built from the `explanation`, so its text is unchanged (`self-heal raised; refusing (fail-closed): <err>`); in the interim builds it had read
> `self-heal-raised: <err>` and was restored.
