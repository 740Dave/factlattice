# The Gnomon Platform: FactLattice Repo

<!-- gnomon-platform-opening:start -->
**The Gnomon Platform makes it easy to lead an outstanding AI team that remembers, collaborates with you and produces extraordinary results.**

## Why Gnomon Exists

The Gnomon Platform is built to address four fundamental problems that come up when one person leads a team of AI agents. By so doing, the Gnomon Platform enables more effective collaboration between humans and AI team members. This extensible platform provides value-added wrappers around native AI services, enables a range of MCP services, and provides a web interface for managing agent interactions and multiple projects (workspaces), each with their own agent profiles and capabilities.

| Problem | What the Gnomon Platform does about it |
|---|---|
| **Memory.** An agent starts each session without what the team learned before, and even when a memory store exists it may answer without consulting it. | Specs, decisions and each agent's memory live as plain-text FactLattice files that factlattice-engine can query. Each agent's definition is rendered at launch with its memory inlined, and a failed expansion is shown loudly. |
| **Ambiguity.** Given an unclear spec, or too little context, an agent may guess silently. | The agent puts the choice to you as a decision document, with options, tradeoffs and a recommendation. Your answer is recorded and delivered back to the agent. |
| **Agent behaviour.** A confident report can rest on a check that never ran. | The harness records every action it takes with its source, status is reported from evidence about the live object, and every change is reviewed by someone who did not write it. |
| **Synthesis.** What a decision needs is scattered across many documents. | Decisions, specs and the links between them form one graph that factlattice-engine can follow, so an agent can gather what bears on a choice. |

The Gnomon design principles ([design-principles](https://github.com/740Dave/gnomon/blob/main/docs/design-principles.md)) also describe the pillars of outstanding teams that the platform builds on (among them compelling purpose, psychological safety, and complementary strengths between the human and the agents) and the principles of the pilot's experience. If you find Gnomon useful and are curious about its approach to human-AI collaboration, that document is worth a read.

## The projects that make up the Gnomon Platform

| Project | What it does for the team | Repository |
|---|---|---|
| **Gnomon engine:** The heart of the platform, and required for the full experience | The harness that runs the team, and the web dashboard you, the pilot, use to lead it. | https://github.com/740Dave/gnomon |
| **factlattice-engine:** Gives agents a memory graph they can query, traverse and update over MCP, kept as plain-text FactLattice (`*.lts`) files that people can read, diff and version too. | The team's memory and shared knowledge. Agent definitions embed factlattice-engine queries for essential knowledge; agents query anything else when they need it, and can update their own or the team's shared memory graph; the next query sees the change. | https://github.com/740Dave/factlattice |
| **Decision engine:** decision-support documents (DSDs) and the decisions-made graph | A single, consistent way for multiple agents to ask you for decisions, with every decision you make stored persistently in FactLattice files that factlattice-engine can query. | https://github.com/740Dave/decision-engine |
| **Gnomon plugins:** Gnomon supports platform-wide plugins and workspace-specific plugins. | A library of optional, platform-wide capability enhancements. Workspace-specific plugins are typically developed by users to meet a particular project's needs. | https://github.com/740Dave/gnomon-plugins |

<!-- gnomon-platform-opening:end -->

This repository is FactLattice and factlattice-engine: the Python package `factlattice`, whose engine is the module `factlattice_engine`. The engine's MCP server reports the name `lts-engine`, which is also what the specifications (`SPEC.md`, `specs/`) call it. An MCP client lists the server under the key you register it with (`factlattice` in the example below), and every tool it serves is named `lts_*`.

---

## factlattice-engine

**factlattice-engine** answers questions about a typed knowledge graph that lives in plain-text `.lts`
files in a repository. It is a Python library, a command line (`python3 -m factlattice_engine
call <tool>`) and an MCP server (`python3 -m factlattice_engine.server`, stdio), all through the
same dispatch. Agents use it to find nodes, follow typed relationships, count and group, draw
diagrams, and write new facts into the graph without silently losing them. Exact answers involve
no model inference: the same corpus and the same query give the same result. To try it, jump to
[Quick start](#quick-start).

What it does:

- **Query and traversal.** `lts_find` selects nodes by id prefix, lifecycle status, property
  conditions and text terms (phrase, whole-word or prefix matching, with optional deterministic
  BM25 relevance ordering). `lts_traverse` walks typed edges, filtered by edge type, layer,
  direction, depth and a since/until window. `lts_reverse` answers "what points at this node?",
  across corpora. `lts_node` returns one node with its edges.
- **Counting.** `lts_aggregate` gives counts, group-bys, numeric folds, cross-tabs, top-k,
  degree and orphan reports over the same selection, draining every page so a count is never
  partial.
- **A detected change is never answered from the old index.** Freshness is checked by content
  hash, with file size and mtime as a hint for which files to re-hash. When files have changed,
  the engine re-indexes them inside the call and serves only once the freshness check passes.
  If the rebuild fails or cannot run (for example the re-index errors, the files change again
  during it, there is no rebuild recipe, or a rebuild of this same content already failed and is
  not retried until the files change again), it returns a structured refusal (corpus id, index
  and live hashes, and the cause in its `heal` block) instead of an answer.
  An edit that leaves a file's size and mtime unchanged is invisible to that hint and is caught
  by the periodic full re-verify, not necessarily by the next query.
- **Typed writers.** `lts_add_node`, `lts_add_property`, `lts_repair_property`,
  `lts_rename_property` and `lts_remove_property` refuse a property key that would silently turn
  into prose, re-parse the whole file after the edit, and write nothing unless the parsed properties,
  values included, changed only by the intended edit. For an add or append, the stored value is also compared with what the parser makes of the submitted line on its own; a replaced or repaired value is not compared, and replacing a wrapped value keeps the old continuation lines. Neither check catches a character `str.splitlines` treats as a line break (for example U+2028, U+2029, U+0085, a vertical tab, a form feed or a carriage return): the value is written and parses cut off there, unless the rest parses as a property or heading, which the gate refuses. The receipt's `value_as_parsed` shows it. Each applied write is recorded in a hash-chained ledger
  (`lts_history`); writes to files under a temporary directory (the OS temp directory, `/tmp`,
  `/private/tmp`) are not ledgered, and if a ledger append fails the write stands and the receipt says
  so. `lts_preview` shows what a draft would become before it is written.
- **Checks.** `lts_lint` (structure, dangling references in both directions), `lts_property_loss`
  (property-shaped lines that did not parse), `lts_profile_audit` (vocabulary in use against the
  declared profile) and `lts_selfcheck` (the running parser against a shipped control corpus).
  Profile-driven lint rules are implemented at warning severity but are inert with the reference
  profiles shipped here (see Domain profiles below).
- **Diagrams.** `lts_flow` draws a Mermaid diagram from the live graph and stores nothing.
- **When structure is not enough.** `lts_find` searches structured fields only and says so;
  `lts_find_in_prose` searches body text and labels each hit. An optional `semantic` extra adds
  `lts_semantic_find`, which returns suggestions in a separate field and never changes an exact
  answer.

### Quick start

From a clean clone, with no configuration, the engine runs against a built-in example registry
whose only loadable corpus, `alpha`, is this repository's own specification in `specs/` (two other
example entries point at directories that do not exist and are skipped):

```bash
pip install .     # Python 3.9+; installs the one required dependency, pydantic
python3 -m factlattice_engine call lts_find --args-json '{"corpus": "alpha", "text": "stale", "rank": "relevance"}'
python3 -m factlattice_engine call lts_traverse --args-json '{"corpus": "alpha", "start_node": "INV-FRESH", "direction": "reverse", "max_depth": 1}'
```

The first finds the specification's nodes that mention "stale", most relevant first; the second
lists what points at the freshness invariant. Each command prints, on stderr, which registry and
build it ran against.

To query your own files, point the engine at a registry that names a directory of `.lts` files:

```bash
echo '{"notes": {"root": "/absolute/path/to/your/lts/files", "domain_suffix": "example.notes"}}' > registry.json
export FACTLATTICE_CORPUS_CONFIG="$PWD/registry.json"
python3 -m factlattice_engine call lts_list_corpora
```

To give an agent the same tools, register the server in an MCP client:
`{"mcpServers": {"factlattice": {"command": "python3", "args": ["-m", "factlattice_engine.server"]}}}`.
`call` is read-only unless you add `--allow-write`. [docs/TRY-BEFORE-YOU-DEPLOY.md](docs/TRY-BEFORE-YOU-DEPLOY.md)
walks through a ten-minute trial; `factlattice_engine/config.py` documents the registry shape,
and `factlattice/registry.py` the order in which the registry is found.

### factlattice-engine as agent memory

An LLM agent loses its working context when a session ends. A team of agents adds a second problem: what one agent learned has to reach the others. FactLattice approaches both by recording what matters as typed nodes and edges in plain-text `.lts` files in a repository. Any session can then query that graph, rather than relying on transcripts or summaries.

**What gets recorded, and how.**

- *Facts and relationships*: nodes with properties, and typed edges (`depends-on`, `implements`, `part-of`, ...) in four fixed layers: semantic, temporal, causal and entity.
- *Procedures*: flows made of steps and decision points. `lts_flow` draws them as diagrams from the current graph and stores nothing.
- *Decisions*: one node per decision, holding the reasoning, the alternative that was rejected, and the `invalidating_assumption` whose change would reopen it. `decided-in-session` records the session in which the decision was made.
- *Time*: temporal edges (`BEFORE`, `SEQUENCE`, `VALIDITY_WINDOW`), since/until filters on a date property, and the node-change ledger of each applied typed write (edits made outside the writers are not recorded). [APPENDIX-VERSIONING.md](APPENDIX-VERSIONING.md) specifies DELTA change records of what changed between commits; the command-line generator it describes is a design, not shipped, and agent definitions that produce DELTA documents are in `agents/`.

**Starting a new session.** The decision-lifecycle appendix defines orientation queries for a fresh session: the recent decisions that matter, the re-derivation fixtures the work cites most often, and a sweep for dangling edges. Handoff becomes a query over the graph, not a separate file someone has to keep up to date. [SESSION-LIFECYCLE.md](SESSION-LIFECYCLE.md) describes, as a design example you build yourself, a session-start hook that loads a namespace's core files in a manifest-defined order. The `factlattice-decision-archivist` agent definition moves decisions out of session transcripts and into the graph.

**Sharing between agents.** One engine serves every corpus in its registry to every caller, so agents read the same graph. A corpus marked `private: true`, or with an id ending in `-personality` (the convention for one private corpus per agent), is by default never returned by a search that does not name it explicitly (`lts_reverse` indexes it but filters its referrers out).

**Why the write and read paths matter for memory.** A property key must match `^[a-z][a-z0-9_-]*$`; any other spelling turns the line into prose, so the fact disappears from queries while the file still looks right. That is why agents write through the typed writers, and why the read path does not answer a detected change from the old index (both are described above and under Guarantees).

**Limits, stated plainly.** The graph answers only for relationships someone declared. This repository does not measure whether agents using FactLattice as memory do better than agents using other memory approaches. The case studies in `case-studies/specification-ambiguity/` are small, single-task experiments.


### Guarantees

These are properties the implementation holds, not aspirations. They are the claims to test
first.

**Deterministic.** Parse, index, traverse, aggregate, lint, render — zero model inference
anywhere in the core path. The core needs no model; an optional `semantic` extra adds semantic
*suggestions* as a separate tool, and its output never feeds the deterministic answer. The same corpus and the same query return the same nodes, order and content on
any machine (the response's own corpus paths, timestamps and elapsed-time notes aside).

This is the substantive contrast with retrieval-augmented and embedding-similarity
approaches. An embedding index computes similarity at query time and answers *what looks
related to this* — ranked, probabilistic, dependent on the model version and on how the text
happened to be phrased. FactLattice moves the work to authoring time: the author **declares**
that A depends on B, and the engine follows that declaration at query time. The answer to
"what depends on B" is then exact and complete with respect to what was declared.

The honest framing of the trade: FactLattice gives up recall on *undeclared* relationships
in exchange for exactness and reproducibility. That is the right trade for specifications,
requirements, decisions, and process flows — where being confidently approximate is the
failure mode — and the wrong trade for open-ended discovery over an unstructured document
pile.

**Freshness is a correctness property.** A corpus the engine has detected as stale is
rebuilt or refused, never answered from the old index. The refusal is structured — corpus id, index hash, live hash, reason — so a
consumer can distinguish "this corpus moved under me" from "this corpus does not exist".

The content hash is the oracle; a changed mtime is never by itself a verdict. mtime is wrong in both directions
in ordinary situations: a checkout or a preserving copy moves content without moving mtime,
and a `touch` moves mtime without moving content. mtime and size are used only as a *hint*
for which files to re-hash. By default each query first runs that hint pass (one `stat` per
corpus file, and a content re-hash only of the files whose size, mtime or permission bits moved), then compares
an incrementally-maintained Merkle-style root against the hash the index was stamped with; an
unchanged size, mtime and mode is trusted as unchanged until a periodic full re-verify (in the long-lived server; the one-shot `call` command has no watcher), which runs as a
backstop. That backstop bounds,
rather than closes, the window for a change the hint cannot see (an edit that keeps a file's size
and mtime): until it runs, such a change can be answered from the old index.

**Stale is transient, not terminal.** On detecting staleness the engine re-indexes only the
files whose content hash moved, re-runs the ordinary freshness check, and serves only if that
check now passes. The repair is never trusted on its own report. Within one engine process,
concurrent callers wait for the rebuild and, once it succeeds, are served from the new index
(separate engine processes each rebuild their own). On a rebuild error it fails closed, refuses,
and leaves the served index untouched.

**Derived, never stored.** Diagrams are rendered from the graph at query time and written
nowhere, so they cannot drift. There is no second copy to keep in sync and no freshness
policing to maintain.


### The engine in detail: four API layers, plus PREVIEW

One library, one graph index, one freshness gate. The layers compose rather than coexist —
ANALYTICS folds over QUERY's own selection function, RENDER walks the same adjacency maps,
LINT runs the same parser. Nothing is re-implemented per layer, which is a correctness
property: because LINT consumes the engine's parser, on the same files and profile it sees the same
nodes and edges, with no second parser to drift. A file with 0 lint errors can still lose content (see
PREVIEW below), so read the warnings or run `lts_preview`.

#### QUERY — which nodes, and how are they connected

`find`, `find_in_prose`, `node`, `traverse`, `capabilities`, `list_corpora`.

```
lts_find(corpus="payments", prefix="REQ", status="proposed")

lts_traverse(corpus="payments", start_node="REQ-REFUND-WINDOW",
             edge_types=["IMPLEMENTS", "TESTED_BY"],
             direction="reverse", max_depth=2)

lts_find_in_prose(corpus="payments", text="chargeback", body_only=True)
```

*Which requirements are still proposed, and what implements them?* Results are whole-node: a
node that does not fit the remaining token budget is omitted entirely and a cursor returned,
never truncated mid-content. (The exception: `lts_node`'s untiered `source_excerpt` is capped at
4,000 characters (less with a small `token_budget`) and flagged `truncated: true`.)

`status` is **lifecycle-aware**. Corpora author `status: proposed — awaiting ratify`, not a
bare enum, so a bare-token query (`status="proposed"`) matches the whole lifecycle including
every qualified value; a qualified query (`status="proposed — awaiting ratify"`) still matches
exactly. The raw string comes back untouched — the qualifier text is content. Lint rule **V11**
is the paired agreement check: when a machine-readable profile is in scope it flags every status
that is *not* a bare lifecycle token, so the drift the filter tolerates stays visible (with the
reference profiles shipped here it is inert, and lint lists it under `rules_inert`). Tokenizer
rules, boundary set, and the shapes it deliberately refuses to normalize:
`factlattice_engine/lifecycle.py`.

**`find`'s `text` matches STRUCTURE ONLY** — id, title, and string property values. The graph
index carries no body text at all, so a term that appears only in prose returns a confident,
well-formed **zero**. That was observed on a private corpus: a widget corpus answered a query for its
own subject term with six hits, *every one of them a markdown filename sitting in an
`artifact:` property*, over a corpus holding no subject-matter content — `complete: true`,
`truncated: false`, and nothing anywhere saying the body had never been read.

The fix is not a wider matcher. Every `find` query declares its own scope
(`extent.body_scanned: false`), and every `text` query also carries a `scope_note` that names what it did not read,
counts the nodes it did not read it in, and routes to **`lts_find_in_prose`** — a separate
tool that searches body text and classifies every hit:

| `finding` | meaning |
|---|---|
| `body-only` | the term is in the body and nowhere in id/title/string properties — **`lts_find` does not return this node**. A densification finding: content the graph cannot answer for. |
| `also-in-structure` | `lts_find` already returns it; `structure_hit_in` names where. Redundant coverage, not a gap. |

Two tools rather than one channel, deliberately. A property hit means *this node asserts
this*; a prose hit means *this node discusses this*. Merging them buys recall and destroys
that distinction — and prose hits arriving free alongside structured ones would erode the
pressure that makes authors move facts **into** properties, which is the pressure that makes
a corpus queryable in the first place. Reaching for prose stays a deliberate second act,
because that act is itself the signal that the structure failed the caller. `body_only=true`
turns the tool into a densification worklist generated by **real queries** rather than by
audit.

The cheap answer routes; it never satisfies — the same rule the `digest` tier is built on.

#### ANALYTICS — how many, grouped how

`count`, `group_by`, `numeric` (`sum`/`avg`/`min`/`max`), `distinct`, `cross_tab`, `top_k`,
`degree`, `top_by_degree`, `count_by_edge_type`, `orphans`.

```
lts_aggregate(op="count", corpus="payments", prefix="REQ", group_by="status")

lts_aggregate(op="top_by_degree", corpus="payments", k=5, direction="in")
```

*How many requirements in each lifecycle state; which five nodes are most depended-upon?*
Three guarantees make the number trustworthy: **completeness** (the fold drains QUERY's
token-budget cursor, so a count never silently depends on the consumer's context window),
**freshness** (a stale verdict propagates verbatim, so a stale corpus yields a refusal rather
than a wrong count), and **honesty** (a numeric fold skips non-numeric and absent values and
reports the skip count, never coercing a missing metric to zero).

#### LINT — is this corpus well-formed

```
lts_lint(source="specs/", dangle=True)

python3 -m factlattice_engine.lint lint specs/ --dangle
```

Structural diagnostics (parse errors, id collisions, dangling edges, unregistered namespaces,
unresolved targets) plus a bidirectional dangle-lint: **forward** findings for a reference to
an id defined nowhere, **orphan** findings for a defined node nothing points at. Parse errors,
collisions and forward dangles are errors; orphans and namespace flags are warnings, because
an unreferenced node is routinely legitimate and a gate that fires on it gets disabled.

#### RENDER — draw this subgraph

```
lts_flow(corpus="ops", start_node="FLOW-DEPLOY-APPROVAL",
         group_by="actor", layout="TD")
```

*Draw the deployment approval flow.* A flow is a graph, so the diagram is a query result.
Nothing is written anywhere.

The emitter knows nothing about flows — its contract is `render(nodes, edges) -> diagram`, so
the same code renders a process flow, a supersession chain, a dependency tree, or a decision
lineage depending only on which edge types the walk followed. Passing
`edge_types=["SUPERSEDES"]` renders a supersession chain through the identical path.

RENDER passes the same freshness gate as everything else: a stale corpus renders no diagram
at all.

#### PREVIEW — what did my document become

```
lts_preview(content="---\ndomain: ops\n---\n\n# [SPEC:OPS-ROLLBACK] ...",
            corpus="specs/", replaces="12-rollback.lts")
```

*I have not written this file yet. Show me the graph it would produce, and everything it
would silently lose.* This closes the authoring loop — compose, preview, revise, and only
then write — so landing a file in a corpus becomes the **commit** action rather than the
moment you discover the defect.

It is not a linter and does not replace one. LINT asks *does this violate a rule?* and can
only answer over content the parser already read. PREVIEW asks *what did this become?* and
reports what the parser read **nothing** from: nodes and edges minted, every unaccounted line
with the parser branch that nearly took it, and every bracketed id authored in a defining
position that inflated into no node. The gap is real and measured — on the shipped fixture
`tests/fixtures/preview/spec41-broken-control.lts`, lint returns 0 errors and exit 0 (the losses appear only among its warnings) while the
document's own root node and three authored relationships are gone.

Two properties make it usable rather than merely correct:

* **Content is the primary input.** A draft mid-authoring is on no disk the engine watches,
  so a path-only API could not serve the loop at all. `path=` is the convenience wrapper.
* **Every response declares its resolution mode.** Reference integrity is a whole-corpus
  property; a file previewed alone would invent dangles that do not exist once it lands.
  Without `corpus`, the result says `isolated` and reports **no dangle at all**. With
  `corpus`, the draft is resolved *as if it had already landed there* — and for an edit,
  `replaces` swaps it for the file it supersedes and reports the **delta**, so an edit that
  silently drops an existing node is visible before it happens. A draft that redefines
  existing ids with no `replaces` is refused, not guessed at.

---

## The format: FactLattice notation

factlattice-engine reads FactLattice notation: plain-text `.lts` documents that record a domain's
knowledge as typed nodes, typed edges and properties. The organizing idea is that **the graph is
the primary artifact**: documents, diagrams and plans are derived from it, so a divergence
between what is recorded and what is the case becomes something a query or a linter can report,
rather than silent decay. The idea is domain-general; this repository shows it most fully on
software engineering.

**You are not expected to write `.lts` files by hand.** They are authored by an AI partner during
ordinary dialogue: you say what you know or want, and the partner turns it into typed nodes and
edges, reconciles it with the existing graph, and keeps it current. The format is optimized for
the machine that writes it and the engine that reads it.

### A `.lts` document

A document is a container for a fragment of a typed graph. It carries frontmatter, one `h1`
that names the document, and nodes minted by headings that carry a bracketed uppercase id.
Properties on a node are descriptive by default; a specific set of keys is
relationship-bearing and mints typed edges.

```
---
title: "Order Service"
id: "SPEC:ORDER-SERVICE"
domain: ACME.SERVICES
---
# Order Service [SPEC:ORDER-SERVICE]

## [REQ-IDEMPOTENT] Order submission is idempotent
- status: accepted
- priority: must
- tested-by: [TEST-DUPLICATE-SUBMIT]

## [MOD-ORDER-API] The order API module
- implements: [REQ-IDEMPOTENT]
- depends-on: [MOD-ORDER-STORE]
- path: services/order/api.py
```

That yields exactly three nodes (`ORDER-SERVICE`, `REQ-IDEMPOTENT`, `MOD-ORDER-API`) and five
edges:

```
TEST-DUPLICATE-SUBMIT --TESTED_BY(entity, property)--> REQ-IDEMPOTENT
MOD-ORDER-API         --DEPENDS_ON(semantic, property)--> MOD-ORDER-STORE
MOD-ORDER-API         --IMPLEMENTS(semantic, property)--> REQ-IDEMPOTENT
ORDER-SERVICE         --CONTAINS(entity, structural)--> REQ-IDEMPOTENT
ORDER-SERVICE         --CONTAINS(entity, structural)--> MOD-ORDER-API
```

Note the third field on every edge: the **inference class** records how the edge came to
exist — `property` from a relationship-bearing property, `authored` from an explicit
relationships or causal-graph line, `structural` from heading nesting. Every edge in a
FactLattice corpus came from exactly one of those, and there is no heuristic source. That is
what makes the graph auditable, and it is why the orphan check can count only `authored` and
`property` edges: if `structural` counted, every nested node would be trivially "referenced"
by its own parent heading.

Edges are **preserved and flagged, never dropped**. `TEST-DUPLICATE-SUBMIT` and
`MOD-ORDER-STORE` are defined nowhere in that fragment, so their edges are kept and marked
dangling — a lint finding, not a silent loss (`lts_traverse` does not return an edge whose endpoint
is undefined; `lts_lint --dangle` and `lts_node` report it).

Every edge lands in exactly one of four layers: **semantic** (what is this, what is it part
of), **temporal** (what comes before what), **causal** (what makes what happen), **entity**
(who holds, owns, contains). Four layers is fixed core — a domain adds edge *types* freely
and assigns each to one of the four. Layers are the primary filter that turns a traversal
from a hairball into an answer.

Within a document, knowledge may be encoded as prose, YAML blocks, tables, property lists,
an explicit relationships section, or a causal-graph section. The parser extracts the same
abstract graph regardless. Diagrams are *not* one of these notations: a diagram is a query
result (see the RENDER layer), never something you write into a document.


### Domain profiles

The format knows that domains exist. It knows nothing about any particular domain. Everything
a knowledge format is normally tempted to hardcode — what `REQ-` means, which edge types are
legal, which properties are required — is registered by a profile that tooling loads at
runtime. No domain has special status; the first domain to adopt the format does not get to
name the vocabulary for the rest.

Namespaces are the single deconfliction mechanism, applied to every extensibility surface —
edge types, id prefixes, properties, document tags, cross-corpus references, event stream
types. Three tiers:

| Tier | Form | Guarantee |
|------|------|-----------|
| Core | `DEPENDS_ON`, `core.DEPENDS_ON` | Immutable, ships with the format, every parser understands it |
| Registered | `clinical.CONTRAINDICATES` | Published in a profile, loaded on demand |
| Private | a bare non-core type | Local use, no registration, no collision guarantee |

A parser meeting an unregistered namespace preserves the edge and flags it. Zero friction to
read a corpus whose profile you have not loaded; full validation when you author one.

Core vocabulary is promoted from observed convergence rather than designed up front: one
domain is domain-specific, two are noted, three or more with compatible semantics are a
candidate for core or for an intermediate namespace shared by that cluster.

**Current state, stated plainly:** the engine core consumes exactly one hook from a profile —
the set of registered namespaces. Profile-driven validation (prefixes, required properties, enum
values and more) is implemented in the linter at warning severity, but it runs only when a
machine-readable profile is in scope, and the reference profiles in `domain-profiles/` do not yet
provide one: with them those rules are inert, and lint says so in its output.


### Machine-authored, human-reviewed

Humans do not hand-edit `.lts` documents. An AI agent composes them during an interactive
session; humans read rendered output. The `.lts` extension is deliberately unmapped on
developer machines.

This is a design constraint, not a preference, and it buys two things. Density: a document
optimized for machine extraction can be far denser than one optimized for a human's first
read, because the human never reads the source. And discipline: every relationship must be
*declared*, which is precisely what makes query-time traversal deterministic.

---

## Methodology and case studies

FactLattice has three parts that ship together: the **notation** above; the **engine**,
factlattice-engine; and a **methodology**: machine authorship, declared (never inferred)
relationships, decisions recorded with the assumption that would reopen them, and domain
vocabulary supplied by profiles. [EXEC-SUMMARY.md](EXEC-SUMMARY.md) is the short account,
[USE-CASES.md](USE-CASES.md) has five worked cases with what each supports only partly, and
[case-studies/specification-ambiguity/](case-studies/specification-ambiguity/) holds four small
experiments (rounds 1-2 reproducible from this repository; rounds 3-4 reported).

To adopt FactLattice for a domain of your own:

1. Write a domain profile. `domain-profiles/` has reference examples (software engineering,
   clinical records, hardware, agentic artifacts and decisions); they are written for people. For
   the linter's profile rules to run, a profile must use the `.lts` profile encoding described in
   `factlattice/profile.py`; `factlattice_engine/reviewer/shipped/reviewer-profile.lts` is a
   working example.
2. Have an AI agent author your corpus against it; agent definitions are in `agents/`.
3. Gate on `python3 -m factlattice_engine.lint lint <corpus> --dangle` in CI. A run with 0
   errors is the cheapest available proof that the graph the engine will ingest is the graph you
   meant to write.

---

## Repository structure

```
README.md                This file: start here
EXEC-SUMMARY.md          The short account of the approach
FOR-LLMS.md              A summary of this repository for a language model
USE-CASES.md             Five worked use cases with their current support level
SPEC.md                  Normative FactLattice 1.0 specification (22 sections, §0 to §21)
SDLC.md                  Development lifecycle with dispatch orchestration
SESSION-LIFECYCLE.md     Manifest-driven bootstrap, hooks, engineering logs
APPENDIX-VERSIONING.md   DELTAs, snapshots, semantic diff system
APPENDIX-DECISION-LIFECYCLE.md  Decision node lifecycle

specs/                   FactLattice specified IN FactLattice (the dogfood corpus)
  00-factlattice-overview.lts     [SYSTEM:FL-OVERVIEW] — thesis and corpus map
  10-abstract-model.lts           [SPEC:FL-MODEL] — nodes, edges, properties, layers
  20-engine-layers.lts            [SPEC:FL-ENGINE] — QUERY / ANALYTICS / LINT / RENDER
  30-freshness-and-self-heal.lts  [SPEC:FL-FRESHNESS] — the correctness contract
  40-domain-profiles.lts          [SPEC:FL-PROFILES] — the extensibility mechanism
  50-decisions.lts                [DECISION:FL-DECISIONS] — why it is shaped this way

factlattice/             The parser, index builder and typed-writer core (pydantic in the authoring layer only)
factlattice_engine/      factlattice-engine: query, analytics, lint, render, freshness, self-heal
domain-profiles/         Reference domain profiles
docs/                    Additional documentation (try-before-you-deploy guide)
case-studies/            Reproducible and reported experiments (specification ambiguity)
agents/                  Agent definitions + the Quality Event Protocol
tests/                   Test suite
tools/                   Small maintenance tools: freshness_bench.py (freshness benchmark),
                         disclosure_guard.py + disclosure-denylist.example.tsv (a commit-time check
                         for text you must not publish; copy the TSV and fill in your own terms),
                         and land-time helpers (key_prior_use.py, run_bounded.py, commit-if-green,
                         pre-commit, mermaid_validate.mjs, retitle_gated.py, set_list_item_gated.py)
```

`specs/` is the dogfood proof and the fastest way to evaluate the claims on this page. It is
FactLattice's own specification, authored in FactLattice, and it lints with no errors:

```bash
python3 -m factlattice_engine.lint lint specs/ --dangle
```

The command exits 0 and prints a summary line of the form `-- <N> finding(s): 0 error(s),
<N> warning(s)`, followed by per-rule summary lines. The warnings are style findings (documents still in legacy mode, undeclared
keys), and the output says `profile: NONE IN SCOPE`: with no domain profile in scope the
profile-driven rules do not run, so read the result as "parses and resolves", not "fully
checked".

A format that could not carry its own specification would fail that test loudly.

---

## Status

**FactLattice 1.0 — DRAFT.** The notation and the four engine layers are implemented and
tested. Known gaps are stated where they occur rather than collected here; the significant
ones are profile-driven validation (implemented, but inert with the shipped reference profiles) and the partial support noted
per use case in [USE-CASES.md](USE-CASES.md).

## License

[MIT](LICENSE)
