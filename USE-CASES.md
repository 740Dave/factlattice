# FactLattice — Use Cases

Five use cases, written as the acceptance tests for the design. Each states the problem, how
FactLattice addresses it, which constructs and queries are involved, and — plainly — where
support is currently partial.

All examples use a fictional company, **Meridian Instrument Works (MIW)**, which builds
industrial flow-measurement equipment: the **FS-1** handheld field sensor, the **GC-4**
fixed-install gateway controller, and **Atlas**, a hosted service that ingests readings from
both. Every id, path, and quantity below is invented.

> These cases describe what FactLattice is *for*. For evidence that the central claim holds —
> four reproducible experiments measuring how ambiguity turns into wrong code, and how a graph
> prevents it — see the companion study
> [`case-studies/specification-ambiguity/`](case-studies/specification-ambiguity/README.md).
> It is runnable: each round ships the actual implementations and a `compare.py` you can execute.

---

## 1. Strategy that reaches the design guideline [UC-STRATEGY]

### The problem

MIW's leadership writes a strategy: *field-serviceable hardware, ten-year support horizon,
no proprietary connectors*. Two years later an engineer selects a proprietary connector for
the GC-4 because it saves eleven dollars per unit and nobody in that review had read the
strategy document. The strategy is not wrong and the engineer is not careless. The strategy
simply has no mechanical connection to the decision surface where it should have applied.

The usual mitigations — a design-review checklist, a principles page on the intranet — are
copies of the strategy in a second place. They drift, and nobody can answer "which product
decisions does this principle actually constrain?" without reading everything.

### How FactLattice addresses it

Strategy, principles, design guidelines, core technology, and product portfolios become nodes
in one graph, connected by declared edges. The path from a strategy statement to the specific
guideline that implements it is a traversal, and so is the reverse.

```
## [PRIN-SERVICEABLE] Field-serviceable hardware
- part-of: [STRAT-MIW-2027]
- status: ratified
- owner: engineering-leadership

## [GUIDE-CONNECTORS] Connectors must be commodity-sourced
- implements: [PRIN-SERVICEABLE]
- rationale-ref: [DEC-NO-PROPRIETARY-CONNECTOR]
- applies-to: [PORT-FIELD-HARDWARE]

## [PROD-GC4] GC-4 gateway controller
- part-of: [PORT-FIELD-HARDWARE]
- depends-on: [TECH-SENSOR-BUS]
```

Two queries close the loop in both directions.

*Downward — what does this principle constrain?*

```
lts_traverse(corpus="miw", start_node="PRIN-SERVICEABLE",
             edge_types=["IMPLEMENTS", "PART_OF"],
             direction="reverse", max_depth=4)
```

*Upward — which principles govern this product?*

```
lts_traverse(corpus="miw", start_node="PROD-GC4",
             edge_types=["PART_OF", "IMPLEMENTS"],
             direction="forward", max_depth=4)
```

*Coverage — which guidelines implement nothing?*

```
lts_aggregate(op="orphans", corpus="miw", prefix="GUIDE")
```

An unattached guideline is a guideline nobody derived from a principle. An unimplemented
principle is a strategy statement with no engineering consequence. Both are now findings
rather than intuitions.

### Constructs involved

Node prefixes `STRAT-`, `PRIN-`, `GUIDE-`, `TECH-`, `PORT-`, `PROD-`; `part-of` and
`implements` edges; semantic-layer traversal; the `orphans` aggregate; a domain profile
registering the prefix taxonomy.

### Where support is partial

- **The profile does not enforce the shape.** Nothing currently stops a `GUIDE-` node from
  being authored with no `implements` edge, or with an `implements` edge pointing at another
  guideline instead of a principle. Prefix-aware validation exists in the linter (warning
  severity) but is inert with the shipped reference profiles. The `orphans` query finds the gap after
  the fact; a linter rule would prevent it.
- **No policy engine.** FactLattice tells you the guideline exists and what it governs. It
  does not evaluate a design against it. Compliance remains a review activity; the graph makes
  the review's inputs complete.

---

## 2. Specifications that drive agent-authored software [UC-SPEC-TO-CODE]

### The problem

MIW wants the Atlas ingest service written largely by an agent, to commercial quality. The
blocker is not code generation. It is that the specification is a set of documents in which
ambiguity is invisible: three requirements silently overlap, one contradicts a data-model
constraint, two say "must be fast", and nothing states which of them are testable. An agent
handed that will produce plausible code and a test suite that tests the wrong invariants.

### How FactLattice addresses it

The specification is authored as a graph with the properties that make it executable: every
requirement is `testable` or explicitly not, carries a priority, names the interface contract
it constrains, and either has a verification node or is visibly missing one.

```
## [REQ-INGEST-IDEMPOTENT] Duplicate readings are idempotent
- status: accepted
- priority: must
- testable: true
- constrains: [API-INGEST-V2]
- tested-by: [TEST-INGEST-DUPLICATE]

## [REQ-INGEST-LATENCY] Ingest acknowledges within 200 ms at p99
- status: accepted
- priority: must
- testable: true
- measurement: p99 over a 24-hour window, 500 readings/s offered load
- tested-by: [TEST-INGEST-LATENCY]

## [INV-NO-SILENT-DROP] A reading is never dropped without an error record
- status: accepted
- applies-to: [API-INGEST-V2]
- tested-by: [TEST-DROP-AUDIT]
```

The agent's build loop becomes a set of queries rather than a reading task:

```
# the complete, exact set of must-requirements for this interface
lts_traverse(corpus="miw-atlas", start_node="API-INGEST-V2",
             edge_types=["CONSTRAINS", "IMPLEMENTS"],
             direction="reverse", max_depth=2)

# what is specified but unverified — the test strategy's work list
lts_find(corpus="miw-atlas", prefix="REQ", status="accepted")
lts_aggregate(op="count", corpus="miw-atlas", prefix="REQ", group_by="status")
```

A test strategy is then derivable rather than invented: every `testable: true` requirement
with no inbound `TESTED_BY` edge is a required test; every invariant is a property test; the
priority field orders the work. Because the query is deterministic and complete, "did we test
everything we specified" has an exact answer instead of an estimate.

Divergence is detectable in the other direction too. When the implementation is recorded as
its own node population, an implementation node that realizes nothing, or a requirement with
no implementation, is a query result:

```
lts_aggregate(op="orphans", corpus="miw-atlas", prefix="MOD")
```

### Constructs involved

`REQ-`, `INV-`, `API-`, `TEST-`, `MOD-` prefixes; `implements`, `tested-by`, `depends-on`
edges; status and priority properties; the `count`/`group_by`/`orphans` aggregates; the
forward-dangle lint as a gate on half-written specs.

### Where support is partial

- **"Commercial grade" is not a property the graph can assert.** FactLattice makes the
  specification complete, consistent-by-reference, and mechanically queryable. It does not
  make the requirements *good*. A precise specification of the wrong behaviour produces
  precisely wrong software.
- **No test execution or coverage import.** `last-run` and `last-result` are specified
  properties on verification nodes, but nothing ships that writes them back from a CI run.
  Today they are hand-recorded by the agent that ran the tests, which means their freshness is
  a convention rather than a guarantee. This is the single largest gap in this use case.
- **Requirement contradiction is not detected.** The linter finds references to nothing and
  nodes nothing references. It does not find two requirements that cannot both hold —
  detecting that requires semantics the format deliberately does not have.

---

## 3. Documenting a process flow [UC-FLOW]

### The problem

MIW's field-calibration procedure involves a technician, the FS-1, and the Atlas service, with
two decision points and a failure path. It is documented in a Word file with a diagram that
was accurate in the first revision. The procedure has changed four times. The diagram has
changed once. Nobody trusts it, so the technicians follow tribal knowledge and the document
exists to satisfy an auditor.

This is the archetypal diagram-drift failure, and it is not a discipline problem. The diagram
is a second copy of a fact. Any process for keeping a second copy in sync is a process that
fails quietly.

### How FactLattice addresses it

**A flow is a graph, so the diagram is a query result.** The procedure is authored as steps
with declared transitions; the picture is rendered on request from the current content and
stored nowhere.

```
## [FLOW-FIELD-CAL] Field calibration procedure
- part-of: [PROC-FIELD-SERVICE]

### [STEP-CONNECT] Technician connects the FS-1 to the reference cell
- part-of: [FLOW-FIELD-CAL]
- actor: technician
- next-step: [STEP-SELFTEST]

### [STEP-SELFTEST] Does the self-test pass?
- part-of: [FLOW-FIELD-CAL]
- actor: FS-1
- condition: self-test pass?
- if-true: [STEP-MEASURE]
- if-false: [STEP-QUARANTINE]

### [STEP-MEASURE] Record three reference measurements
- part-of: [FLOW-FIELD-CAL]
- actor: technician
- next-step: [STEP-DRIFT]

### [STEP-DRIFT] Is drift within tolerance?
- part-of: [FLOW-FIELD-CAL]
- actor: Atlas
- condition: drift within tolerance?
- if-true: [STEP-CERTIFY]
- if-false: [STEP-ADJUST]
```

Then:

```
lts_flow(corpus="miw-service", start_node="FLOW-FIELD-CAL",
         group_by="actor", layout="TD")
```

`actor` becomes a swimlane. `condition` makes the node a diamond labelled with the question.
`if-true`/`if-false` become labelled branches. Membership edges (`part-of`) define which nodes
belong to the flow but are not drawn by default (to draw them, pass `edge_types` with `PART_OF` together with the sequence types you still want, since `edge_types` replaces the default set), because a container-to-step arrow is not a step
transition. Convergence needs no special construct: two transitions into one step simply
produce two arrows into one box.

The same tool renders any subgraph. `edge_types=["SUPERSEDES"]` over the same corpus renders
a revision chain; `edge_types=["DEPENDS_ON"]` renders a dependency tree — same walk, same
emitter, no flow-specific configuration.

Because RENDER passes the same freshness gate as every other layer, a corpus detected as stale
renders no diagram at all rather than an old one (an edit that keeps a file's size and mtime can
render from the old index until the periodic full re-verify).

### Constructs involved

`FLOW-`/`STEP-` prefixes; `next-step`, `if-true`, `if-false`, `branches`, `converges-to`
flow properties; the descriptive `condition` and `actor` properties; membership via `part-of`
and `contains`; the RENDER layer's `lts_flow`.

### Where support is partial

- **One output format.** RENDER emits Mermaid flowcharts. Sequence diagrams, state charts and
  BPMN are not emitted; the emitter is generic over node/edge sets, so additional emitters are
  a bounded piece of work, but they do not exist today.
- **Loops render as edges, not as loop constructs.** A back-edge produces an arrow back to the
  earlier step, which is correct but reads as a cycle rather than as "repeat until".
- **Swimlanes are single-valued.** A step with two actors takes the first for grouping
  purposes. A genuinely two-actor step should be authored as two steps.
- **No execution semantics.** This documents and draws a process. It does not run one,
  validate that a real execution followed it, or check the flow for unreachable steps.

---

## 4. Decisions linked to their insight and their blast radius [UC-DECISION]

### The problem

MIW decided that the FS-1 would not store readings locally beyond 48 hours. The decision came
from a specific field insight: a customer audit found technicians using the handheld as a
system of record, which broke a data-retention obligation. Two years on, a product manager
proposes seven-day local storage as an offline-mode feature. It is a reasonable proposal. The
original insight is in a trip report nobody can find, and the decision looks arbitrary, so it
gets reversed — and the retention obligation is breached again.

Separately: nobody can list which specification sections that decision actually shaped, so
reversing it has an unknown blast radius.

### How FactLattice addresses it

A decision is a node linked in three directions: back to the insight that produced it, forward
to the specification sections it shaped, and inward to the premise it depends on.

```
## [DEC-NO-LOCAL-RETENTION] FS-1 retains readings for at most 48 hours
- status: accepted
- decision-date: 2026-03-11
- see-also: [INSIGHT-AUDIT-2026]
- affects: [REQ-FS1-STORAGE], [REQ-FS1-SYNC], [INV-NO-SYSTEM-OF-RECORD]
- change: "Local retention capped at 48 hours."
- context_for_change:
    prior_state: "Retention was unbounded, limited only by flash capacity."
    reasoning: "A customer audit found technicians treating the handheld as a system of
      record, which places the customer in breach of their own retention policy."
    rejected_alternative: "Warn in the UI and leave retention unbounded. Rejected: the
      warning does not change the behaviour that creates the breach."
    counter_argument: "Field crews work offline for up to five days."
    counter_response: "Offline capture is a separate requirement served by queued sync,
      not by unbounded local retention."
    invalidating_assumption: "That customers' retention policies treat the handheld as
      in-scope. If a customer's policy explicitly excludes field instruments, the
      constraint does not apply to them."
    implication: "Offline operation must be served by sync queueing."

## [INSIGHT-AUDIT-2026] Field audit: handhelds used as systems of record
- source: customer site audit, three sites
- confidence: 0.9
```

The product manager's question is now two queries:

```
# what produced this decision, and what did it shape?
lts_node(corpus="miw", node_id="DEC-NO-LOCAL-RETENTION")

# blast radius: what depends on the sections this decision affects?
lts_traverse(corpus="miw", start_node="REQ-FS1-STORAGE",
             edge_types=["DEPENDS_ON", "IMPLEMENTS", "CONSTRAINS"],
             direction="reverse", max_depth=3)
```

The proposal is not blocked. It is *informed*: here is the insight, here is the premise the
decision rests on, and here is everything that moves if you reverse it. If the customer's
policy genuinely excludes field instruments, the `invalidating_assumption` has moved and
reopening is correct. If it has not, the counter-response already answers the proposal.

Decisions are append-only. A reversal mints a new node with a `supersedes` edge; the original
is retained with `status: superseded`, so the audit trail records what was believed and when.
`lts_flow(edge_types=["SUPERSEDES"])` renders the lineage.

### Constructs involved

`DEC-`, `INSIGHT-`, `REQ-`, `INV-` prefixes; `see-also`, `supersedes`, `depends-on` edges; the
`change`/`context_for_change` primitive with `invalidating_assumption`; the `confidence`
property on the insight; reverse traversal for blast radius; RENDER over `SUPERSEDES` for
lineage.

### Where support is partial

- **`context_for_change` is prose, not structured fields.** The block is authored as indented
  YAML-style text inside the node and is stored as document content, not as individually
  queryable sub-properties. You cannot today run "find every decision whose
  `invalidating_assumption` mentions retention policy" as a structured query — free-text
  `lts_find(text=...)` is the available approximation. Promoting these to first-class
  queryable fields is a known, straightforward extension.
- **Blast radius is a traversal, not a ranked impact analysis.** The result is a bounded set
  of reachable nodes. It does not weigh them, and depth 3 versus depth 4 is a judgement the
  caller makes.
- **Nothing detects a decision that has been silently ignored.** If the specification drifted
  away from the decision without a superseding record, that shows as an inconsistency only if
  somebody encoded the divergence. The graph makes the check possible, not automatic.

---

## 5. Finding the overlap before you build it again [UC-OVERLAP]

### The problem

An MIW product manager proposes "Atlas Alerts": threshold monitoring on incoming readings with
notification routing. It is a good idea. It is also, in part, already built — the GC-4 has
local threshold alarms, Atlas already has a notification-routing component built for service
tickets, and there is an eighteen-month-old specification for exactly this feature that was
shelved. Nobody involved knows all three of those facts. The company is about to pay for work
it has already done, and to create a second notification path that will diverge from the
first.

This is the most expensive failure on this page and the hardest to notice, because the cost is
invisible: nobody ever sees the invoice for the thing they rebuilt.

### How FactLattice addresses it

The new concept is decomposed into its constituent capabilities, and each is used as a probe
against the existing corpus. Three query shapes do the work.

*Capability surface — what already exists around this topic?*

```
lts_capabilities(corpus="miw", topic="threshold")
lts_capabilities(corpus="miw", topic="notification")
```

`capabilities` returns the connected surface around a topic: the nodes that match and
everything joined to them. It answers "what is already here, and what is it wired to" in one
call.

*Cross-corpus scan — is this specified anywhere, in any product line?*

```
lts_find(text="threshold", status="accepted")      # no corpus= : every registered corpus
lts_find(text="notification routing")
```

An unscoped `find` runs across every registered non-private corpus and reports which corpora it
could not serve (private ones are counted, not named), so "we looked everywhere" is a claim with evidence rather than a feeling.

*Build state — how much of it is already implemented?*

```
lts_aggregate(op="count", corpus="miw", text="threshold", group_by="status")
lts_traverse(corpus="miw", start_node="CMP-NOTIFY-ROUTER",
             edge_types=["IMPLEMENTS", "DEPENDS_ON"], direction="reverse", max_depth=2)
```

The `status` histogram distinguishes *specified* from *accepted* from *implemented* from
*deprecated*, which is the difference between "we designed this" and "we shipped this". The
reverse traversal from the existing component shows what already depends on it, which is what
makes reuse-versus-rebuild a decision with numbers behind it.

The document-level frontmatter is doing quiet work here. `scope`, `boundary` and `not-this`
let a reader — or an agent — decide whether a question belongs in a document without reading
it. On a large corpus that is the difference between scanning every file and reading a handful
of frontmatter blocks.

### Constructs involved

`lts_capabilities`; unscoped multi-corpus `lts_find` with text predicates; the
`count`/`group_by` aggregate over `status`; reverse traversal on `IMPLEMENTS`/`DEPENDS_ON`;
document `scope`/`boundary`/`not-this` frontmatter; cross-corpus namespaced references for
products that live in separate corpora.

### Where support is partial

- **Overlap detection is lexical, not semantic — and this is the sharp edge of the
  determinism trade.** `find(text="threshold")` finds "threshold". It does not find "limit
  alarm", "trip point", or "excursion detection". If the existing specification used different
  words, this query misses it. The mitigations are real but they are mitigations: register
  domain vocabulary in a profile so terms are normalized at authoring time; use `aliases` on
  nodes; probe with several terms. Nothing in FactLattice does synonym expansion, and adding
  it would mean adding inference to the query path, which is exactly what the deterministic
  guarantee excludes. **A semantic search tool alongside the engine is the right answer for
  this specific question; it is not something the engine should absorb.**
- **Coverage estimation is manual.** The queries surface what exists. Converting "seven
  related nodes, four implemented" into "roughly 60% already built" is a judgement the reader
  makes; nothing computes it.
- **Cross-corpus reach depends on registration.** An unscoped `find` covers the corpora the
  engine has loaded. A product line whose corpus was never registered is invisible, and the
  result set will not tell you it exists. This is a deployment discipline problem, not a
  format problem, but it is the most likely way this use case fails in practice.

---

## Summary

| Use case | Primary constructs | Support |
|----------|--------------------|---------|
| [UC-STRATEGY] Strategy to design guideline | `part-of`, `implements`, `orphans` | Good; profile-driven shape validation missing |
| [UC-SPEC-TO-CODE] Specs driving agent-built software | `tested-by`, status/priority, orphan queries | Good; no CI result write-back |
| [UC-FLOW] Process flow documentation | flow properties, `lts_flow` | Strong; Mermaid flowcharts only |
| [UC-DECISION] Decisions linked to insight and impact | `context_for_change`, reverse traversal | Good; `context_for_change` not yet field-queryable |
| [UC-OVERLAP] Finding existing overlap | `capabilities`, multi-corpus `find` | Partial; lexical matching only |

The pattern across all five: FactLattice makes the *inputs* to a judgement complete, exact and
cheap to obtain. It does not make the judgement. Where a use case needs inference — synonym
expansion, requirement contradiction, coverage estimation — the answer is a separate tool
alongside the engine, not inside it. That boundary is deliberate and is what the determinism
guarantee costs.
