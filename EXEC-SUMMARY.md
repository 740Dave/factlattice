# FactLattice — Executive Summary

> The entry point. Read this to decide whether FactLattice solves a problem you have.
> Then read [README.md](README.md) for the mechanics, [USE-CASES.md](USE-CASES.md) for
> worked examples, and `specs/` for FactLattice specified in FactLattice.

---

## The one-paragraph version

FactLattice is a structured semantic knowledge graph in which the graph is the primary
artifact: specifications, diagrams, test plans, and other documents are derived from it rather
than being the source of truth. The model is domain-general — it holds a clinical history or an
estate plan as readily as an engineering corpus — but this summary follows the
software-engineering case, where the benefits are easiest to measure. Relationships are
**declared** when a document is authored and **traversed deterministically** at query time — no
embeddings, no model inference, and no ranking except an optional deterministic BM25 order for text queries, anywhere in the core path (an optional `semantic` extra adds
semantic *suggestions* beside it; it is off unless installed, and it never feeds the deterministic answer). The engine does not answer from an index it has detected as stale, so the usual failure
mode is a loud refusal rather than a plausible wrong answer. Diagrams are rendered from the
graph on request and stored nowhere, so they cannot drift.

You never learn the notation. FactLattice documents are written by an AI partner, not by hand:
you work in natural-language dialogue and the partner authors and maintains the `.lts`. What you
review is the result — a rendered view, a query answer, a diagram — not raw markup. The point of
the format is that a machine can hold your domain precisely; your part is the conversation.

---

## The problem

Every individual or organization that writes things down eventually has the same three problems, and they
are the same problem wearing three hats.

**The documents disagree with reality, and nobody can tell which ones.** A requirement is
dropped; the module that implemented it is deleted; the test plan still references both. The
information needed to notice is present — it is spread across four documents that no single
person reads together. There is no mechanical way to ask "what still references this?"
because the references are prose.

**Diagrams are wrong and everyone knows it.** A sequence diagram was accurate when it was
pasted in. The flow changed. The diagram did not. Every convention for fixing this — a
freshness date, a review checklist, a CI job that diffs the picture — is a process that fails
quietly, because the diagram is a second copy of a fact and copies drift.

**Decisions get re-visited or re-litigated with no record of why.** A choice was made for good reasons that were never written down. Two years later somebody — increasingly, somebody's agent — argues
the other way, and the argument is unanswerable because the reasoning is gone. Being right
the first time provides no protection.

A search index does not fix any of these. Search answers *what looks related to this*. These
are questions about *what is declared to be related to this*, which is a different question
with a different correct answer.

---

## What FactLattice does about it

Relationships are declared and typed at authoring time, in the document, next to the thing
they describe:

```
## [MOD-ORDER-API] The order API module
- implements: [REQ-IDEMPOTENT]
- depends-on: [MOD-ORDER-STORE]
- tested-by: [TEST-DUPLICATE-SUBMIT]
```

Once declared, the questions above become mechanical:

- *What still references this requirement?* — a reverse traversal.
- *What breaks if I change this module?* — a causal and semantic walk with a depth bound.
- *Which requirements have no test?* — an orphan query on the verification edge type.
- *Draw the deployment flow.* — a diagram rendered from the graph, right now, from the
  current content.
- *Why is it done this way?* — the decision node, with its rejected alternatives and the
  assumption whose change would reopen it.

Divergence between the graph and the implementation stops being decay and becomes a
**finding**: a dangling edge, an orphan node, a stale marker, a lint error with a file and a
line number.

---

## What you actually get

**A notation.** `.lts` documents encoding typed nodes, typed edges, properties, and four
graph layers (semantic, temporal, causal, entity). Plain UTF-8 text, in your repository, next
to the code.

**An engine.** factlattice-engine — four composed API layers over one graph index and one freshness
gate:

| Layer | Answers | Example |
|-------|---------|---------|
| QUERY | Which nodes? How connected? | `lts_find(prefix="REQ", status="proposed")` |
| ANALYTICS | How many? Grouped how? | `lts_aggregate(op="count", group_by="status")` |
| LINT | Is this corpus well-formed? | `lts_lint(source="specs/", dangle=True)` |
| RENDER | What does this subgraph look like? | `lts_flow(start_node="FLOW-DEPLOY")` |

The layers compose rather than coexist: ANALYTICS folds over QUERY's own selection function,
RENDER walks the same adjacency maps, LINT runs the same parser. Because LINT uses the
engine's parser, on the same files and profile it sees the same nodes and edges — there is no second parser to
drift. A file with 0 lint errors can still lose content, so read the warnings or run `lts_preview`.

**A methodology.** Machine authorship, pluggable domain vocabulary, and decision records that
carry the premise they depend on.

---

## The four guarantees

Stated as guarantees because they are implemented and testable, not as goals.

**Deterministic.** Zero model inference in parse, index, traverse, aggregate, lint, or render. The core needs no model; the optional `semantic` extra adds semantic suggestions as a separate, clearly-labelled tool.
The same corpus and query return the same nodes, order and content on any machine (the response's own corpus paths, timestamps and elapsed-time notes aside). The trade is
explicit: FactLattice gives up recall on *undeclared* relationships in exchange for exactness
and reproducibility. Right for specifications, requirements, decisions and flows; wrong for
open-ended discovery over an unstructured pile.

**Freshness is a correctness property.** A corpus detected as stale is rebuilt or refused with
a loud stale marker (corpus id, index hash, live hash, cause), never answered from the old index. The content hash is the oracle;
mtime is only a hint for *which* files to re-hash, because mtime is wrong in both directions
in ordinary situations (a preserving copy moves content without mtime; a `touch` moves mtime
without content). By default each query runs the hint pass (one `stat` per corpus file) and then
compares an incrementally-maintained Merkle-style root, and a periodic full re-verify backstops it. The backstop bounds the window
for a change the hint cannot see (an edit that keeps size and mtime); until it runs, such a change
can be answered from the old index.

**Stale is transient, not terminal.** On detecting staleness the engine re-indexes only the
changed files, re-runs the ordinary freshness check, and serves only if it now passes.
Within one engine process, concurrent callers wait for the rebuild and are served once it
succeeds; a rebuild error fails closed with a refusal, leaving the served index untouched. At most one rebuild per detected content change in each engine process, never one per query.

**Derived, never stored.** Diagrams are rendered from the graph at query time and written
nowhere, so they cannot drift. No second copy, no freshness policing.

---

## What it is not

Not a database — no storage engine, no query planner. Not a wiki. Not an embedding index (an optional extra can suggest candidates by embedding, but the graph's answers do not depend on it). Not
a documentation generator, though documentation is one of the things you can project from a
corpus. It reads a directory of text files, builds an in-memory index, and answers questions
about it deterministically.

It is also not a format you type. Humans do not hand-edit `.lts` documents: an AI agent
composes them, and humans read rendered output. The `.lts` extension is deliberately unmapped
on developer machines. This buys density (the source is never a human's first read) and, more
importantly, discipline (every relationship must be declared, which is the precondition for
deterministic traversal).

---

## Multi-domain by construction

Nothing in the format knows about any particular domain. What `REQ-` means, which edge types
are legal, which properties are required — all of it is registered by a **domain profile**
that tooling loads at runtime. No domain is the reference implementation; the first adopter
does not get to name the vocabulary for everyone after.

Namespaces are the single deconfliction mechanism, applied uniformly to edge types, id
prefixes, properties, document tags, cross-corpus references, and event stream types, in three
tiers: **core** (immutable, universal), **registered** (published in a profile, loaded on
demand), **private** (bare names, local, no collision guarantee). A parser meeting an
unregistered namespace preserves the edge and flags it, so a consumer who has not loaded your
profile still gets a complete graph.

The reference profiles shipped in `domain-profiles/` span software engineering, clinical
records, hardware/maker work, and agentic artifacts and decisions. If a format encodes both
interface contracts and time-indexed clinical assessments, the domain-agnostic claim is
substantiated rather than asserted.

**Stated plainly:** the engine core consumes exactly one hook from a profile today — the set
of registered namespaces. Prefix, required-property and enum rules are implemented in the linter
at warning severity, but they run only when a machine-readable profile is in scope, and the
reference profiles shipped in `domain-profiles/` do not yet provide one.

---

## Recorded decisions, and why the premise matters

FactLattice has one first-class primitive for how understanding changes:

```yaml
change: "What changed"
context_for_change:
  prior_state: "What was believed before"
  reasoning: "Why it changed"
  rejected_alternative: "What else was considered"
  counter_argument: "The strongest argument against"
  counter_response: "Why that argument does not hold"
  invalidating_assumption: "The premise whose change reopens this"
  implication: "Downstream effects"
```

`invalidating_assumption` is the load-bearing field. It converts a decision from a conclusion
into a conditional — *this holds while X holds* — so a future reader can tell "still valid,
defend it" from "the premise moved, reopen it" without reconstructing the original argument.

This is what closes the re-litigation failure mode. It matters more now than it did five
years ago: an AI agent asked to evaluate a decision it has no context for will produce a
competent argument against it, and "my agent disagrees with your agent" is not a governance
process. The reasoning has to be durable and queryable. Here it is a property of a node.

---

## Evaluating it

The fastest honest test is the dogfood corpus. `specs/` is FactLattice's own specification,
authored in FactLattice:

```bash
python3 -m factlattice_engine.lint lint specs/ --dangle
# exits 0; the summary lines include:
#   -- <N> finding(s): 0 error(s), <N> warning(s)
#   -- profile: NONE IN SCOPE ... profile-driven rules V1-V6 were INERT
```

That is "no errors", not "no findings": the specs carry style warnings (legacy-mode documents,
undeclared keys), and with no domain profile in scope the profile-driven rules do not run.

Then render a flow out of it — the diagram is a query result over `specs/`, not a stored
picture:

```
lts_flow(corpus="alpha", start_node="FLOW-QUERY", group_by="actor")
```

(Run from the repository root with no registry configured: the built-in example registry's
`alpha` corpus is `specs/`, and `FLOW-QUERY` is defined in `specs/30-freshness-and-self-heal.lts`.)

A format that could not carry its own specification would fail that test immediately and
loudly. That is the point of running it first.

Read [USE-CASES.md](USE-CASES.md) next: five worked use cases, each with the constructs and
queries involved and an explicit note on where support is currently partial.

---

## Status

**FactLattice 1.0 — DRAFT.** The notation and all four engine layers are implemented and
tested. The significant known gaps: profile-driven validation is inert with the shipped reference profiles,
and several use cases in [USE-CASES.md](USE-CASES.md) are partially supported in ways
documented there rather than glossed.
