# Appendix: Decision-Lifecycle & Continuity Tracking

**Parent document**: [SPEC.md](SPEC.md) (FactLattice 1.0 Specification)
**Companion**: [APPENDIX-VERSIONING.md](APPENDIX-VERSIONING.md) (Versioning, Change Tracking, and Impact Assessment)
**Status**: Draft — extension appendix for architect review

---

### Conformance Language

The key words MUST, MUST NOT, SHALL, SHALL NOT, SHOULD, SHOULD NOT, MAY, and RECOMMENDED in this document are to be interpreted as described in RFC 2119, consistent with SPEC.md.

---

## 0. Purpose and Scope

FactLattice already tracks how a corpus's *content* changes over time. SPEC.md §8 defines the `{change, context_for_change}` primitive and DELTA change records; [APPENDIX-VERSIONING.md](APPENDIX-VERSIONING.md) defines the graph-index snapshot chain, per-commit DELTA generation, session-ID linkage, and the use of change records as **handoff artifacts the next session reads** (APPENDIX-VERSIONING §5.2). Those mechanisms answer: *"which nodes changed, how, and who is affected?"*

This appendix defines a **decision-level layer that composes over that node-change layer**. It answers a different question: *"where does a decision or work item sit in its own pipeline, does its labeled position match ground truth, and how does a fresh session orient itself in the decisions of the sessions that preceded it?"*

The distinction is load-bearing:

| Layer | Question | Mechanism | Defined in |
|-------|----------|-----------|-----------|
| Node-change tracking | How did this node's *content* evolve across commits? | DELTA / CR records, graph-index snapshot chain | SPEC.md §8, APPENDIX-VERSIONING |
| **Decision-lifecycle tracking** | Where is this *decision* in its pipeline, and is the label true? | This appendix | This appendix |

This appendix **MUST NOT** be read as replacing or duplicating APPENDIX-VERSIONING. It does not redefine change capture, snapshot versioning, impact classification, audience routing, or session-handoff — it reuses all of them (§8 below states the reconciliation explicitly). A change to a decision node's `lifecycle-position` is an ordinary node property change and is captured as a property-diff by the existing DELTA machinery; this appendix only adds the *interpretation* of that property, the edges that make progression queryable, and the lint/traversal patterns that expose mismatches.

### 0.1 The Motivating Failure

Curated, prose-based continuity **leaks**. A single decision commonly advances one stage per working session and is handed off with a label like "decided" or "approved." But a labeled stage is a *claim*, not evidence. A body of work can be marked "approved" while remaining uncommitted; a work item can be marked "in progress" while a finished deliverable already exists on a side branch. What a session drops at its boundary is, almost by definition, whatever did not clear the bar for the curated summary — so the gap hides in exactly what curation omitted.

The countermeasure this appendix specifies is to make lifecycle **position** a first-class, machine-checkable attribute, and to detect — in both directions — where the curated position disagrees with the graph's ground truth.

---

## 1. The `lifecycle-position` Attribute

### 1.1 Definition

A **decision node** or **work node** MAY carry a descriptive property `lifecycle-position` whose value names the stage the node currently occupies in an ordered pipeline.

```
### [DEC-ADOPT-GRPC] Adopt gRPC for inter-service transport
- lifecycle-position: committed
- decided-in-session: S31
- rationale: JSON-over-HTTP framing cost dominated the hot path; gRPC gives us streaming and codegen.
```

`lifecycle-position` is a **descriptive property** in the sense of SPEC.md §1.3 — it describes the node and does not by itself generate an edge. It is distinct from the existing `status` property (SPEC.md §1.3), which describes the *node content's* editorial state (`operational`, `planned`, `draft`, `deprecated`). A node MAY carry both: `status` describes the document node; `lifecycle-position` describes the progress of the decision the node records.

### 1.2 The Value Space Is Domain-Configured, Not Hardcoded

The ordered set of stage names is **domain vocabulary**, not core vocabulary, per SPEC.md P4 (*Mechanisms Are Core, Implementations Are Domain*). The `lifecycle-position` *attribute* — its presence, its role as an ordered position, and the dangle semantics built on it — is the core mechanism. The specific stage names are declared in a domain profile (§3).

- The parser MUST treat the value as an opaque string and MUST NOT assume any particular pipeline.
- The linter, when a domain profile declaring a pipeline is loaded, MUST validate that the value is one of the profile's declared stages, and SHOULD warn on an unrecognized value.
- Without a profile, `lifecycle-position` is preserved and passed through unvalidated (SPEC.md P3 graceful degradation).

**Illustrative pipelines (neutral domains; non-normative):**

| Domain | Declared ordered pipeline |
|--------|---------------------------|
| A software platform corpus | `raised` → `decided` → `spec-authored` → `built` → `committed` → `deployed` |
| A research corpus | `proposed` → `designed` → `data-collected` → `analyzed` → `peer-reviewed` → `published` |

A pipeline is a total order over its stages. A profile MAY declare terminal off-pipeline states (e.g., `superseded`, `deferred`, `withdrawn`) that are not positions on the ladder; §5.4 covers how the dangle detector treats them.

---

## 2. Stage-Transition Edges

### 2.1 Position Is a Claim; Edges Are Evidence

`lifecycle-position` records what a node *claims*. To make progression queryable and to detect mismatch, a node SHOULD additionally record **stage-transition edges** that bind the node to the concrete artifact evidencing arrival at each stage reached.

A stage-transition edge is generated from the relationship-bearing property `stage-evidence` (per the auto-generation model of SPEC.md §1.3). Each entry maps a reached stage to the artifact node or reference that evidences it:

```
### [DEC-ADOPT-GRPC] Adopt gRPC for inter-service transport
- lifecycle-position: committed
- decided-in-session: S31
- stage-evidence: decided -> [DEC-RECORD-4412]
- stage-evidence: spec-authored -> [SPEC-TRANSPORT-GRPC]
- stage-evidence: built -> [PR-887]
- stage-evidence: committed -> [COMMIT-a1b2c3d]
```

Each `stage-evidence` entry generates one edge:

| Property | Generated edge type | Layer | Direction | Carries |
|----------|--------------------|-------|-----------|---------|
| `stage-evidence: <stage> -> [REF]` | `lifecycle.ADVANCED_TO` | temporal | decision node → evidence artifact | `to-stage: <stage>` |

The edge is temporal-layer because it records progression in time (SPEC.md §1.4). The set of `to-stage` values across a node's `lifecycle.ADVANCED_TO` edges is the node's **evidenced frontier** — the highest stage for which real evidence exists.

### 2.2 Namespace and Promotion

`lifecycle.ADVANCED_TO` (and the other edge/property names introduced here) live in the registered `lifecycle.*` namespace per SPEC.md §9 and P3. They are **not** core edge types — SPEC.md §1.4 holds core edge types immutable. If 3+ domains converge on decision-lifecycle tracking, `lifecycle.*` becomes a candidate for core promotion or an intermediate namespace via the frequency-driven governance process (SPEC.md §16). A parser encountering `lifecycle.*` without the profile loaded MUST preserve the edges and flag them for the linter, never drop them.

### 2.3 Optional: Direct Authoring

An author MAY instead declare `lifecycle.ADVANCED_TO` edges directly in a `## Relationships` section (SPEC.md Rule E2) when the evidence artifact is external to the corpus and has no bracketed node ID; in that case the `to-stage` is carried as an edge property. The `stage-evidence` property form is RECOMMENDED because it keeps position and evidence adjacent in the node body.

---

## 3. Domain Profile: The Lifecycle Pipeline Declaration

A domain that uses decision-lifecycle tracking declares its pipeline in its profile YAML (SPEC.md §10), in a new `decision_lifecycle` section:

```yaml
# In 99-{domain}-profile.yaml
decision_lifecycle:
  pipeline:                     # ordered; index defines rank
    - raised
    - decided
    - spec-authored
    - built
    - committed
    - deployed
  off_pipeline:                 # terminal states not on the ladder
    - superseded
    - deferred
    - withdrawn
  decision_prefixes: [DEC]      # node prefixes treated as decision/work nodes
  evidence_edge: lifecycle.ADVANCED_TO
  session_key_property: decided-in-session
```

The linter loads this section to validate `lifecycle-position` values, to rank stages for dangle detection (§5), and to recognize decision nodes by prefix. This mirrors the domain-configured `change_tracking` section defined in APPENDIX-VERSIONING §7.6; the two sections are independent and MAY coexist in one profile.

The pipeline declaration is domain data, per SPEC.md P4. The linter and the traversal engine contain no hardcoded stage names.

---

## 4. Session-Keying of Decision Nodes

### 4.1 The `decided-in-session` Property

A decision node SHOULD carry `decided-in-session`, a descriptive property naming the session in which the decision was **made** — not the session in which the node was authored, archived, or last committed.

```
- decided-in-session: S31
```

The value is a domain-defined session identifier (an ordinal like `S31`, a session UUID, or a dated key). The domain profile names the property via `session_key_property` (§3) so tooling need not hardcode it.

### 4.2 Relationship to APPENDIX-VERSIONING Session Linkage

APPENDIX-VERSIONING already carries a `session` field on change records and JSONL `corpus_change` events (APPENDIX-VERSIONING §1.3, §2.1, §5.3), keyed to the session that **committed the change**. These two session keys answer different questions and MUST NOT be conflated:

| Key | On | Names | Answers |
|-----|-----|-------|---------|
| `session` (versioning) | change records / `corpus_change` events | the session that committed the bytes | "when did this node's content change?" |
| `decided-in-session` (this appendix) | decision nodes | the session the ruling was made | "when was this decision decided?" |

A decision decided in session S31 but not committed until S33 carries `decided-in-session: S31` on the node, while the DELTA that records its commit carries `session: S33`. Preserving the original decision session is what makes the "prior N sessions" orientation query (§6) meaningful — it keys each decision to *when it was decided*, so a fresh session can retrieve "the load-bearing decisions of the previous two sessions" without the answer being distorted by later bookkeeping commits.

### 4.3 No Second Master

Because the decision graph carries `decided-in-session` on each node, the session-handoff view is a **projection** of the graph, not a separately maintained handoff file. This avoids two-master drift: there is no hand-updated "what happened last session" document to fall out of sync with the graph. The handoff *is* a query (§6).

---

## 5. The Bidirectional-Dangle Pattern

### 5.1 Definition

A **dangle** is a node whose curated `lifecycle-position` disagrees with its evidenced frontier (§2.1). The pattern is **bidirectional** — the disagreement can run in either direction, and both directions are defects:

- **FORWARD_DANGLE** (*less done than labeled*): the label claims a stage higher than the evidenced frontier. A node with `lifecycle-position: committed` but whose highest `to-stage` evidence is `built` (no `lifecycle.ADVANCED_TO` edge with `to-stage: committed`) claims to be committed with nothing evidencing the commit.
- **INVERSE_DANGLE** (*more done than labeled*): evidence exists for a stage higher than the label claims. A node with `lifecycle-position: in-progress` that carries a `lifecycle.ADVANCED_TO` edge with `to-stage: deployed` is further along than its label admits.

The inverse case is easy to overlook and is often the more expensive: an under-labeled item invites duplicated or redundant work and hides a finished-but-untracked deliverable.

### 5.2 Why an Ordered Position Is Required

Dangle detection is only possible because `lifecycle-position` is a *ranked* position, not a binary flag. A plain `status: unresolved`/`resolved` flag finds nothing here: nobody labels an "approved" or "committed" thing as unresolved, so a binary flag cannot surface "approved-but-uncommitted." The dangle lives precisely in the space between two *advanced* stages, which only an ordered pipeline can express.

### 5.3 As a Linter Rule

A conforming linter, with a `decision_lifecycle` profile loaded, MUST implement dangle detection as follows. Let `rank(s)` be the index of stage `s` in the profile's `pipeline` (§3), and let `frontier(n)` be the maximum `rank(to-stage)` over node `n`'s `lifecycle.ADVANCED_TO` edges (or "none" if the node has no such edges).

```
for each decision node n with a lifecycle-position value p on the pipeline:
    if frontier(n) == none:
        emit UNEVIDENCED_POSITION            # position claimed, zero evidence edges
    else if rank(p) > frontier(n):
        emit FORWARD_DANGLE  (claimed: p, evidenced: frontier)
    else if rank(p) < frontier(n):
        emit INVERSE_DANGLE  (claimed: p, evidenced: frontier)
    # rank(p) == frontier(n): consistent, no finding
```

Each finding MUST be emitted to the quality event stream as a `factlattice.finding` (SPEC.md §11.2; §9 below), so dangles are aggregatable and trend-trackable like any other quality signal. Severity SHOULD reflect the gap width and the criticality of the missing stage (e.g., a claimed-but-unevidenced `deployed` or `committed` stage on a high-importance decision is more severe than a one-stage gap on a low-importance one).

### 5.4 Off-Pipeline States

A node whose `lifecycle-position` is a declared `off_pipeline` state (§3) — `superseded`, `deferred`, `withdrawn` — is exempt from FORWARD/INVERSE dangle checks (it is not on the ladder). The linter SHOULD instead verify that a `superseded` node carries a `supersedes`/`SUPERSEDES` relationship (SPEC.md §1.3) identifying its replacement, so a retired decision cannot silently re-propagate.

### 5.5 As a Graph Traversal

The same pattern is available at query time, not only at lint time. A consumer computes dangles by traversing (SPEC.md §4): enter at decision nodes (by `decision_prefixes`), follow `lifecycle.ADVANCED_TO` on the temporal layer, and compare each node's `lifecycle-position` property against its evidenced frontier. The traversal is O(decision nodes × their evidence edges). This lets a fresh session run a live dangle sweep as an orientation step (§6) rather than trusting a prior linter run.

---

## 6. The `re-derive-via` Edge

### 6.1 Concept

Some decisions encode a *judgment* — a calibration a future reader must not merely recall but must be able to **re-derive**, because a recalled-but-unre-earned judgment degrades into a rule followed by rote rather than applied with understanding. This is the same risk SPEC.md §8.1 names as the "debate amplifier" anti-pattern: context severed from a decision leaves it defenseless.

A **cold fixture** is a reusable artifact — a runnable check, a worked counter-example, a calibration exercise — that reconstructs the judgment behind a decision by making a fresh reader work through the case that produced it, rather than reading a conclusion. The `re-derive-via` edge binds a decision to the cold fixture that re-derives its judgment.

`re-derive-via` is the decision-level companion to SPEC.md §8.1's `invalidating_assumption`: the assumption field tells a future reader *when to reopen* a decision; the `re-derive-via` fixture lets them *re-earn the judgment* the decision embodies.

### 6.2 Mechanism

`re-derive-via` is a relationship-bearing property (SPEC.md §1.3) generating an edge:

| Property | Generated edge type | Layer | Direction |
|----------|--------------------|-------|-----------|
| `re-derive-via: [FIXTURE-REF], ...` | `lifecycle.RE_DERIVED_VIA` | entity | decision node → each cold-fixture artifact |

```
### [DEC-FAIL-CLOSED-ON-STALE-CACHE] A derived index must fail loud on staleness, never serve stale silently
- lifecycle-position: deployed
- decided-in-session: S28
- rationale: A silent-stale answer has no error to catch; the check must key on freshness, not on error-presence.
- re-derive-via: [FIXTURE-STALE-CACHE-DRILL]
```

The edge is entity-layer (SPEC.md §1.4): it relates an actor/artifact (the decision) to another artifact (the fixture). A decision MAY carry zero, one, or (rarely) more `re-derive-via` edges. Bindings MUST be accurate — a fixture is bound to a decision only when it genuinely re-derives that decision's judgment.

### 6.3 Binding Frequency Is an Importance Signal

The value of `re-derive-via` is not per-node; it is **emergent across sessions**. The number of distinct decisions (especially recent ones) that bind a given fixture is a signal of how load-bearing that fixture's lesson is:

- A fixture bound by many decisions across the recent arc is a judgment the work **keeps reaching for** — a hot-path signal.
- Counterintuitively, high binding frequency means *load-bearing*, not *redundant*. A frequently-cited fixture is the opposite of a duplicate — it is the calibration most worth re-deriving first.

This yields an orientation query (§6.4): *the most-frequently `re-derive-via`-bound fixtures over the last N sessions.* Because frequency is the signal, an author MUST NOT suppress a real binding merely because the fixture is already bound often (that would corrupt the signal), and MUST NOT inflate the signal with speculative bindings.

### 6.4 Bootstrap Orientation Queries

A fresh session orients itself by querying the decision graph rather than re-reading curated prose. Two queries (graph traversal, SPEC.md §4), plus a live dangle sweep, do most of the work:

| Query | Traversal | Returns |
|-------|-----------|---------|
| **Recent load-bearing decisions** | decision nodes where `decided-in-session` ≥ (current − N) AND (`importance: high` OR is a dangle per §5.5) | "the last N sessions' consequential decisions plus everything whose label is untrue" |
| **Hottest fixtures** | `lifecycle.RE_DERIVED_VIA` edges from decisions in the last N sessions, grouped by target, ordered by count desc | "the judgments the work keeps reaching for — re-derive these first" |
| **Live dangle sweep** | §5.5 | "anything that leaked since the last lint" |

These queries make session handoff a **projection of the graph** (§4.3), complementing — not replacing — the change-record handoff artifacts of APPENDIX-VERSIONING §5.2.

---

## 7. Decision-Node Reference Shape

A decision node is a FactLattice node (SPEC.md §1.1) using the domain's decision prefix (RECOMMENDED convention: `DEC-`). The following shape composes the attributes defined here with existing core properties and relationship-bearing edges. All fields except the header are OPTIONAL unless a domain profile requires them.

```
### [DEC-<slug>] <short decision title>
- lifecycle-position: <stage>            # §1; domain-configured value
- decided-in-session: <session-key>      # §4; the session the ruling was MADE
- importance: high | medium | low        # self-stated AND derived (edge-density, supersedes, gates-a-program, binding frequency)
- rationale: <the why a bare log would lose>          # core descriptive property (SPEC §1.3)
- stage-evidence: <stage> -> [ARTIFACT]  # §2; one per reached stage → lifecycle.ADVANCED_TO
- supersedes: [DEC-<id>]                  # core relationship-bearing (SPEC §1.3) → SUPERSEDES; stops corrected beliefs re-propagating
- depends-on: [<ref>]                     # core relationship-bearing → DEPENDS_ON
- re-derive-via: [FIXTURE-<id>]           # §6; → lifecycle.RE_DERIVED_VIA
- born-from: <the concrete case/event that produced it>   # descriptive; a judgment severed from its case is hollow
```

`supersedes`, `depends-on` reuse core edges (SPEC.md §1.3) — this appendix introduces no new core vocabulary, only the `lifecycle.*` registered namespace and three properties (`lifecycle-position`, `decided-in-session`, plus the relationship-bearing `stage-evidence` and `re-derive-via`).

---

## 8. Composition with APPENDIX-VERSIONING — Explicit Reconciliation

This appendix is strictly additive over APPENDIX-VERSIONING. The reconciliation:

| Concern | Owned by APPENDIX-VERSIONING | Added here | Interaction |
|---------|------------------------------|-----------|-------------|
| Recording that a node changed | DELTA / CR records, graph-index snapshot chain, `corpus_change` events | — | A `lifecycle-position` change is an ordinary property-diff captured by the existing DELTA generator; **no new change-capture mechanism is introduced.** |
| Session identity | `session` on change records = the committing session | `decided-in-session` on decision nodes = the deciding session | Two distinct keys, never merged (§4.2). |
| Session handoff | Change records are handoff artifacts the next session reads (§5.2) | Graph orientation queries: recent decisions, hottest fixtures, live dangle sweep (§6.4) | Complementary. The change-record handoff says *what content changed*; the decision-graph handoff says *what was decided, where it sits, and what to re-derive*. |
| Impact / blast radius | graph impact traversal, audience routing (§3) | — | Reused unchanged; a decision node participates in blast radius like any node. |
| Snapshot versioning | `snapshot_version` chain (§4.2) | — | Reused. Dangle findings and binding-frequency are computed against a snapshot like any other query. |
| What a change *means* | `{change, context_for_change}`, `invalidating_assumption` (SPEC §8.1) | `re-derive-via` cold-fixture binding (§6) | `invalidating_assumption` says *when to reopen*; `re-derive-via` says *how to re-earn the judgment*. Companions, not substitutes. |

**Rule of composition:** where APPENDIX-VERSIONING already defines a mechanism (change capture, snapshots, session field, impact, audience), this appendix uses it and MUST NOT define a parallel one. This appendix adds only the decision-lifecycle attribute, its evidence edges, the bidirectional-dangle lint/traversal, session-keying of the decision event, and the cold-fixture re-derivation edge.

---

## 9. Quality Events

Consistent with SPEC.md §11.2 and §17.4, dangle detection and archival emit structured events to `{corpus}/events/quality.jsonl`:

- Each dangle finding (FORWARD_DANGLE, INVERSE_DANGLE, UNEVIDENCED_POSITION) is a `factlattice.finding` event, carrying the decision node ID, the claimed position, the evidenced frontier, and severity.
- Binding-frequency and decision-graph health MAY be summarized in a `factlattice.corpus_health` event (e.g., decision-node count, dangle count by direction, top-N bound fixtures).

This makes lifecycle drift a measurable, publishable SDLC signal rather than a narrative observation.

---

## 10. Summary — What Exists vs. What's New

| Capability | Mechanism | Status |
|------------|-----------|--------|
| Node-level content change tracking | DELTA / CR, graph-index snapshots | Exists (APPENDIX-VERSIONING) |
| Session-ID on change records | `session` field | Exists (APPENDIX-VERSIONING) |
| `{change, context_for_change}`, `invalidating_assumption` | SPEC §8.1 | Exists |
| Ordered lifecycle position on a decision/work node | `lifecycle-position` (domain-configured vocabulary) | **New** |
| Stage-transition (evidence) edges | `stage-evidence` → `lifecycle.ADVANCED_TO` (temporal) | **New** |
| Bidirectional-dangle detection | Linter rule + graph traversal (forward + inverse + unevidenced) | **New** |
| Cold-fixture re-derivation binding | `re-derive-via` → `lifecycle.RE_DERIVED_VIA` (entity) | **New** |
| Binding-frequency-as-importance | Grouped count over `lifecycle.RE_DERIVED_VIA` in last N sessions | **New** |
| Decision-event session-keying | `decided-in-session` (distinct from versioning `session`) | **New** |
| Session handoff as a graph projection | Orientation queries (§6.4) | **New** |
| Lifecycle pipeline declaration | Profile `decision_lifecycle` section | **New** |

Everything under "New" lives in the registered `lifecycle.*` namespace and a small set of properties. No core edge type is added (SPEC.md §1.4 immutability preserved). If 3+ domains converge on decision-lifecycle tracking, the `lifecycle.*` vocabulary is a candidate for promotion via the frequency-driven process (SPEC.md §16).

---

**Version**: 1.0
**Status**: Draft — extension appendix for architect review
**Intelligence**: Claude Opus 4.8
