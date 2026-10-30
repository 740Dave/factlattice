# FOR-LLMS.md: a summary of this repository for a language model

This repository holds FactLattice, a plain-text notation for typed knowledge graphs, and factlattice-engine, which queries them. The remainder of this document uses the FactLattice format.

<!-- gnomon-platform-lts:start -->
## [GP-PLATFORM] The Gnomon Platform

- kind: platform
- goal: The Gnomon Platform makes it easy to lead an outstanding AI team that remembers, collaborates with you and produces extraordinary results.
- scope: four fundamental problems that come up when one person leads a team of AI agents
- provides-wrappers: value-added wrappers around native AI services
- provides-mcp: a range of MCP services
- provides-web-interface: a web interface for managing agent interactions and multiple projects (workspaces), each with their own agent profiles and capabilities
- problem-memory: an agent starts each session without what the team learned before, and even when a memory store exists it may answer without consulting it
- solution-memory-storage: specs, decisions and each agent's memory live as plain-text FactLattice files that factlattice-engine can query
- model-independence: the memory layer is independent of any model or vendor: FactLattice files are plain text, factlattice-engine runs no model inference, and it serves the graph over MCP, an open protocol, to any MCP client that can launch a local stdio server (measured: Claude Code and OpenAI Codex CLI); an agent built on another model can read and write the same knowledge graph
- harness-scope: only the Gnomon engine harness is tied to one agent today (Claude Code, on macOS); Codex support and Windows and Linux support are planned work in the harness, not limits of the memory layer
- solution-memory-rendering: each agent's definition is rendered at launch with its memory inlined, and a failed expansion is shown loudly
- problem-ambiguity: given an unclear spec, or too little context, an agent may guess silently
- solution-ambiguity-document: the agent puts the choice to the pilot as a decision document, with options, tradeoffs and a recommendation
- solution-ambiguity-answer: the pilot's answer is recorded and delivered back to the agent
- problem-agent-behaviour: a confident report can rest on a check that never ran
- solution-agent-behaviour-record: the harness records every action it takes with its source
- solution-agent-behaviour-status: status is reported from evidence about the live object
- solution-agent-behaviour-review: every change is reviewed by someone who did not write it
- problem-synthesis: what a decision needs is scattered across many documents
- solution-synthesis: decisions, specs and the links between them form one graph that factlattice-engine can follow, so an agent can gather what bears on a choice
- design-principles: https://github.com/740Dave/gnomon/blob/main/docs/design-principles.md
- design-principles-covers: pillars of outstanding teams (among them compelling purpose, psychological safety, complementary strengths between the human and the agents) and the principles of the pilot's experience
- source: README.md between the gnomon-platform-opening markers, "Why Gnomon Exists"

### [GP-GNOMON-ENGINE] Gnomon engine

- part-of: [GP-PLATFORM]
- repository: https://github.com/740Dave/gnomon
- role: the heart of the platform
- required: for the full experience of the platform
- provides: the harness that runs the team, and the web dashboard the pilot uses to lead it
- status: draft release
- includes-decisions: the Gnomon engine ships its own decision-support documents (gnomon/dsd/); the Decision engine is the same capability as a standalone project
- source: README.md between the gnomon-platform-opening markers, "The projects that make up the Gnomon Platform", Gnomon engine row
- source-status: README.md of https://github.com/740Dave/gnomon ("Draft. This README is being written for the first public release.") and the header of its docs/design-principles.md (status: draft)

### [GP-FACTLATTICE-ENGINE] factlattice-engine

- part-of: [GP-PLATFORM]
- repository: https://github.com/740Dave/factlattice
- service-name: `lts-engine`
- role: the team's memory and shared knowledge
- provides: a memory graph agents can query, traverse and update over MCP
- storage: plain-text FactLattice (`.lts`) files that people can read, diff and version
- use-in-definitions: agent definitions embed factlattice-engine queries for essential knowledge
- use-by-agents: agents query anything else when they need it, and can update their own or the team's shared memory graph
- write-visibility: the next query sees the change
- model-independence: no model inference in the engine; any agent, of any model or vendor, whose MCP client can launch a local stdio server can use the same graph (measured: Claude Code and OpenAI Codex CLI); a client limited to remote MCP servers over HTTP needs a bridge; the files themselves need no engine at all to read
- measured-other-client: on 2026-10-28 the maintainers ran OpenAI Codex CLI 0.164.0 (a GPT-6 model) against its own factlattice-engine MCP server process, with the same code and registry, and so the same graph, as a Claude Code session; Codex fetched a node with all its properties, traversed its edges, and wrote a new node through the typed writer, and the Claude Code session then read back exactly what Codex wrote (Codex needs the server's tools approved in its configuration)
- os: POSIX; the MCP server, `call` command line and typed writers import `fcntl`, so not Windows; the optional always-on embedder uses launchd (macOS only); the repository records testing on macOS only
- status: draft release
- readme-status: FactLattice 1.0 — DRAFT; notation and four engine layers implemented and tested
- source: README.md between the gnomon-platform-opening markers, "The projects that make up the Gnomon Platform", factlattice-engine row
- source-status: README.md of https://github.com/740Dave/factlattice, "Status"
- source-service-name: README.md of https://github.com/740Dave/factlattice, engine description

### [GP-DECISION-ENGINE] Decision engine

- part-of: [GP-PLATFORM]
- repository: https://github.com/740Dave/decision-engine
- provides: decision-support documents (DSDs) and the decisions-made graph
- role: a single, consistent way for multiple agents to ask the pilot for decisions
- storage: every decision stored persistently in FactLattice files that factlattice-engine can query
- status: placeholder
- source: README.md between the gnomon-platform-opening markers, "The projects that make up the Gnomon Platform", Decision engine row
- source-status: README.md of https://github.com/740Dave/decision-engine, "This repository" ("Coming soon.")

### [GP-GNOMON-PLUGINS] Gnomon plugins

- part-of: [GP-PLATFORM]
- repository: https://github.com/740Dave/gnomon-plugins
- kinds: platform-wide plugins and workspace-specific plugins
- role: a library of optional, platform-wide capability enhancements
- workspace-plugins: typically developed by users to meet a particular project's needs
- status: placeholder
- source: README.md between the gnomon-platform-opening markers, "The projects that make up the Gnomon Platform", Gnomon plugins row
- source-status: README.md of https://github.com/740Dave/gnomon-plugins, "This repository" ("Coming soon.")

### Which parts of the Gnomon Platform to use: a decision matrix [GP-ADOPTION-MATRIX]

- part-of: [GP-PLATFORM]
- kind: decision-matrix
- question: which projects of the Gnomon Platform does a given need call for
- rule: read the rows in order; use the first row whose need matches; the status column says whether the project can be used today
- row-1-need: only a structured way for agents to ask a human for decisions, and a record of the answers, without the rest of the platform
- row-1-use: [GP-DECISION-ENGINE]; it writes plain FactLattice files, and [GP-FACTLATTICE-ENGINE] is optional, for querying them; [GP-GNOMON-ENGINE] also includes this capability, if the whole engine is wanted
- row-1-status: not available yet: the repository is a placeholder
- row-2-need: only a memory graph agents can query, traverse and write, kept as plain-text files
- row-2-use: [GP-FACTLATTICE-ENGINE] alone, as a Python library, a command line, or an MCP server registered in an MCP client; no other platform project is needed
- row-2-status: draft release
- row-3-need: to lead a team of Claude Code agents on one Mac, with shared memory, decision documents and a web dashboard
- row-3-use: [GP-GNOMON-ENGINE] with [GP-FACTLATTICE-ENGINE]; decision documents are built into [GP-GNOMON-ENGINE]
- row-3-status: Gnomon engine draft release; factlattice-engine draft release
- row-4-need: optional extras such as printing, PDF handling or attention lights
- row-4-use: [GP-GNOMON-PLUGINS], plugins for [GP-GNOMON-ENGINE]
- row-4-status: not available yet: the repository is a placeholder
- row-5-need: an agent or model other than Claude Code, for example OpenAI Codex
- row-5-use: for memory, [GP-FACTLATTICE-ENGINE] today: any MCP client that can launch a local stdio server can query, traverse and write the same graph, with no dependency on Claude Code (see its measured-other-client line); to run the team in a harness, [GP-GNOMON-ENGINE] supports Claude Code only today
- row-5-status: factlattice-engine: usable from MCP clients that launch local stdio servers, two measured (draft release); Gnomon engine: Claude Code only, Codex support stated as in progress in its README
- row-6-need: an operating system other than macOS
- row-6-use: [GP-FACTLATTICE-ENGINE] is POSIX: its MCP server, `call` command line and typed writers import the POSIX-only `fcntl` module, so they cannot run on Windows; its optional always-on embedder uses launchd, which is macOS only; the repository records no testing outside macOS; [GP-GNOMON-ENGINE] requires macOS
- row-6-status: Gnomon engine: macOS only, Windows and Linux support stated as planned in its README; factlattice-engine: not Windows, other POSIX systems untested
- source: the status and role lines of the project nodes above; README.md of https://github.com/740Dave/gnomon, Requirements; README.md of https://github.com/740Dave/decision-engine, This repository; factlattice/authoring/edit.py of https://github.com/740Dave/factlattice (the fcntl import)
<!-- gnomon-platform-lts:end -->

## [REPO] Repository
- python: 3.9 or later
- required-dependency: `pydantic>=2`, the only required third-party dependency
- optional-extra: `semantic`, an optional model stack
- licence: MIT
- spec-status: "FactLattice 1.0 — DRAFT"
- source: `pyproject.toml`
- source: `LICENSE`
- source: `README.md`
- part-of: [GP-PLATFORM]

## [ENGINE] factlattice-engine
- part-of: [REPO]
- depends-on: [NOTATION]
- see-also: [IDEA-DETERMINISTIC]
- see-also: [IDEA-FRESHNESS]
- role: the repository's main deliverable
- forms: Python library; command line; MCP server
- python-package: `factlattice`
- python-module: `factlattice_engine`
- service-name: `lts-engine`, as reported by the MCP server and used by the specifications
- tool-names: every tool is named `lts_*`
- operations: find nodes; walk typed edges; count and group; draw diagrams; write facts through typed writers that refuse a malformed edit
- source: `README.md`
- source: `factlattice_engine/server.py`

## [NOTATION] FactLattice notation
- part-of: [REPO]
- file-shape: YAML frontmatter, one `h1`, and nodes created by headings carrying a bracketed uppercase id (`## [REQ-IDEMPOTENT] ...`)
- properties: bullet lines `- key: value` on a node
- edges: a fixed set of relationship keys (`implements`, `depends-on`, `tested-by`, ...) create typed edges
- primary-artifact: the graph; documents, diagrams and plans are derived from it
- source: `SPEC.md`
- source: `specs/10-abstract-model.lts`
- source: `README.md`

## [METHODOLOGY] Methodology
- part-of: [REPO]
- authorship: an AI writes the `.lts` files during conversation; humans read the rendered output
- relationships: declared, not inferred
- decisions: stored with the assumption that would reopen them
- vocabulary: supplied by domain profiles
- source: `README.md`
- source: `EXEC-SUMMARY.md`

## [PROC-QUICK-START] Quick start from a clean clone
- part-of: [ENGINE]
- kind: procedure
- prerequisites: Python 3.9 or later; a clean clone of this repository, with steps 1 to 3 run from its root; no registry configured (`FACTLATTICE_CORPUS_CONFIG` unset, no `~/.config/factlattice/config.json`)
- example-registry: built in; its only loadable corpus, `alpha`, is this repository's `specs/`; two other example entries point at directories that do not exist and are skipped
- step-1: `pip install .`
- step-1-verify: exit code 0; pip prints `Successfully installed`, listing `factlattice-0.1.0` and `pydantic`
- step-2: `python3 -m factlattice_engine call lts_find --args-json '{"corpus": "alpha", "text": "stale", "rank": "relevance"}'`
- step-2-verify: exit code 0; stdout is JSON with `"success": true` and `"count": 7`; stderr names the registry as `built-in example registry`
- step-3: `python3 -m factlattice_engine call lts_traverse --args-json '{"corpus": "alpha", "start_node": "INV-FRESH", "direction": "reverse", "max_depth": 1}'`
- step-3-verify: exit code 0; stdout is JSON with `"success": true`, 11 nodes and 10 edges, the first node `INV-FRESH`
- step-4: `echo '{"notes": {"root": "/absolute/path/to/your/lts/files", "domain_suffix": "example.notes"}}' > registry.json`, with the placeholder replaced by the absolute path of a directory of `.lts` files
- step-4-verify: exit code 0; `registry.json` holds the one entry `notes`
- step-5: `export FACTLATTICE_CORPUS_CONFIG="$PWD/registry.json"`
- step-5-verify: exit code 0; no output
- step-6: `python3 -m factlattice_engine call lts_list_corpora`
- step-6-verify: exit code 0; stderr prints `corpora  : 1 loaded (1 registry entries)`; stdout lists `"corpus_id": "notes"` with `"fresh": true`
- step-6-failure: with the placeholder path left in step 4, exit code 1 and stderr `ERROR: registry loaded 0 corpora`
- step-7: register the server in an MCP client: `{"mcpServers": {"factlattice": {"command": "python3", "args": ["-m", "factlattice_engine.server"]}}}`
- step-7-verify: the server answers an MCP `initialize` with `serverInfo` name `lts-engine`, and `tools/list` returns 24 tools, all named `lts_*`
- write-mode: `call` is read-only unless `--allow-write` is added
- verified-with: a fresh virtual environment, Python 3.13, on this tree; step 6 against a directory holding one `.lts` file
- source: `README.md#quick-start`

## [TOOLS] Tool surface
- part-of: [ENGINE]
- source: `factlattice_engine/server.py`

### [TOOLS-FIND] Find and read
- part-of: [TOOLS]
- tool: `lts_find`: structured fields only (id, title, string properties)
- tool: `lts_find_in_prose`: body text; labels each hit body-only or also-in-structure
- tool: `lts_node`
- tool: `lts_traverse`: bounded typed-edge walk; optional since/until window
- tool: `lts_reverse`: who points here, across corpora
- tool: `lts_capabilities`

### [TOOLS-COUNT] Count
- part-of: [TOOLS]
- tool: `lts_aggregate`: count, group_by, numeric folds, distinct, cross_tab, top_k, degree, orphans, ...

### [TOOLS-CHECK] Check
- part-of: [TOOLS]
- tool: `lts_lint`: structure, plus forward and orphan dangles
- tool: `lts_profile_audit`: vocabulary in use against the declared profile
- tool: `lts_property_loss`: property-shaped lines that failed to parse
- tool: `lts_selfcheck`: the running parser re-parses a shipped control corpus

### [TOOLS-DRAW] Draw
- part-of: [TOOLS]
- tool: `lts_flow`: Mermaid diagram from the live graph; nothing stored

### [TOOLS-AUTHOR] Author
- part-of: [TOOLS]
- tool: `lts_preview`
- tool: `lts_add_property`
- tool: `lts_add_node`
- tool: `lts_repair_property`
- tool: `lts_rename_property`
- tool: `lts_remove_property`
- tool: `lts_history`

### [TOOLS-CORPORA] Corpora
- part-of: [TOOLS]
- tool: `lts_list_corpora`: self-heals stale corpora by default
- tool: `lts_register_corpus`
- tool: `lts_unregister_corpus`

### [TOOLS-RETRIEVAL] Retrieval quality
- part-of: [TOOLS]
- tool: `lts_semantic_find`: suggestions, never matches
- tool: `lts_report_miss`: records a known-item search miss

## [RESULT-SHAPE] Result shape
- part-of: [ENGINE]
- unit: whole nodes; a node that does not fit the token budget is left out and a cursor is returned, never cut mid-content (one exception, below)
- exception: `lts_node`'s untiered `source_excerpt` is capped at 4,000 characters (less with a small `token_budget`) and flagged `truncated: true`
- first-node: always returned whole, even if it alone exceeds the budget; with `tier="full"` the call refuses instead
- status-filter: lifecycle-aware; `status="proposed"` also matches `proposed — awaiting ratify`
- source: `README.md`
- source: `factlattice_engine/budget.py`
- source: `factlattice_engine/tiers.py`

## [AGENT-MEMORY] Use as long-term memory for a team of LLM agents
- depends-on: [ENGINE]
- evaluation: the repository describes the parts this use is built from; it does not claim the use has been evaluated
- source: `README.md`

### [MEM-SEMANTIC] Semantic knowledge
- part-of: [AGENT-MEMORY]
- covers: what things are and how they relate
- held-as: the typed graph: nodes, properties, semantic- and entity-layer edges
- source: `README.md`

### [MEM-PROCEDURAL] Procedural knowledge
- part-of: [AGENT-MEMORY]
- covers: how to do something
- held-as: flows and steps; `lts_flow` renders them from the current graph
- source: `SPEC.md` §5

### [MEM-EPISODIC] Episodic knowledge
- part-of: [AGENT-MEMORY]
- see-also: [IDEA-TYPED-WRITERS]
- covers: what happened, and when
- decisions: decision nodes carry `decided-in-session`, the session in which the decision was made
- ledger: the node-change ledger of typed writes (scope and limits under [IDEA-TYPED-WRITERS])
- delta-records: the versioning appendix specifies DELTA change records of what changed between commits
- not-shipped: the `factlattice-delta-gen` command-line tool the appendix describes (a design)
- shipped: agent definitions that produce DELTA documents (`factlattice-delta-gen`, `factlattice-diff-agent`)
- source: `README.md`
- source: `APPENDIX-DECISION-LIFECYCLE.md`
- source: `APPENDIX-VERSIONING.md`
- source: `agents/factlattice-delta-gen.md`
- source: `agents/factlattice-diff-agent.md`

### [MEM-TEMPORAL] Temporal knowledge
- part-of: [AGENT-MEMORY]
- held-as: a layer of temporal edges (`BEFORE`, `SEQUENCE`, `VALIDITY_WINDOW`, ...)
- filters: since/until on a date property (default `decided-in-session`)
- source: `README.md`
- source: `SPEC.md`
- source: `factlattice_engine/server.py`

### [MEM-SHARED] Shared between agents
- part-of: [AGENT-MEMORY]
- serving: one engine serves every corpus in one registry to all of its callers
- profile: the Agentic Decisions profile is written for "a team of agents and humans"
- private-rule: a corpus marked `private: true`, or with an id ending in `-personality` (the maintainer's convention: one private corpus per agent), is by default never returned by a search that does not name it
- private-reverse: `lts_reverse` indexes such a corpus but filters its referrers out
- private-opt-out: a registry entry can opt out with `private: false`
- source: `README.md`
- source: `domain-profiles/agentic_decisions.lts`
- source: `factlattice_engine/privacy.py`

### [MEM-SESSIONS] Across session boundaries
- part-of: [AGENT-MEMORY]
- mechanism: a new session reads the graph instead of re-reading transcripts
- orientation-queries: recent load-bearing decisions; the most-cited re-derivation fixtures; a sweep for dangling edges
- handoff: "a projection of the graph", not a hand-kept file
- hooks: a design example you build yourself: session-start hooks that load each namespace's files in a manifest-defined order
- not-shipped: the hook scripts
- source: `APPENDIX-DECISION-LIFECYCLE.md`
- source: `SESSION-LIFECYCLE.md`

### [MEM-AGENTS] Agent definitions
- part-of: [AGENT-MEMORY]
- location: `agents/`
- format: Claude Code subagent files
- covers: linting, QA, gap analysis, archiving decisions from transcripts, densifying, diffing
- limit: some, such as `factlattice-linter`, check by reading rather than by running the engine; the authoritative check is `lts_lint`
- source: `agents/`

## [REPO-USED-BY-GNOMON] How the Gnomon engine uses factlattice-engine
- part-of: [REPO]
- see-also: [GP-GNOMON-ENGINE]
- definition-rendering: Gnomon renders each agent definition at launch by expanding `lts:` macros into factlattice-engine query results, in full or digest form
- dsd-answers: Gnomon stores each answered decision-support document as typed nodes in a `.lts` file, written through factlattice-engine's typed writer (`add_node`) when `factlattice` is importable, otherwise by a plain file append
- memory-writes: Gnomon's memory verb writes an agent's memory as a node through the same typed writer
- source: `gnomon/lts_expand.py` of https://github.com/740Dave/gnomon
- source: `gnomon/agent_render.py` of https://github.com/740Dave/gnomon
- source: `gnomon/dsd/writer.py` of https://github.com/740Dave/gnomon
- source: `gnomon/memory.py` of https://github.com/740Dave/gnomon
- source: `gnomon/harness/self_mcp.py` of https://github.com/740Dave/gnomon

## [CORE-IDEAS] Core ideas
- part-of: [REPO]

### [IDEA-TYPED-EDGES] Typed nodes and edges in plain text
- part-of: [CORE-IDEAS]
- edge-type: each edge has a type, one of four fixed layers (semantic, temporal, causal, entity), and an inference class recording where it came from
- implemented-classes: `property` (a relationship key); `authored` (an explicit relationships or causal-graph line); `structural` (heading nesting)
- specified-not-produced: `mention`, `computed`
- heuristic-edges: none; there is no heuristic edge source
- dangling: an edge to an undefined id stays in the index flagged dangling (reported by `lts_lint --dangle`, listed by `lts_node`); `lts_traverse` does not return it
- source: `SPEC.md` §1.2
- source: `specs/10-abstract-model.lts`
- source: `README.md`

### [IDEA-DETERMINISTIC] Deterministic by design
- part-of: [CORE-IDEAS]
- no-inference: parse, index, traverse, aggregate, lint and render do no model inference
- trade-off: exact, reproducible answers over *declared* relationships; no recall on undeclared ones
- semantic-tool: optional and separately labelled; its output sits in its own field and never changes the deterministic answer; its model dependencies are an optional extra
- source: `README.md`
- source: `specs/30-freshness-and-self-heal.lts`

### [IDEA-FRESHNESS] A detected change is never answered from the old index
- part-of: [CORE-IDEAS]
- oracle: content hash; file size and mtime are only a hint about which files to re-hash
- on-change: re-indexes only the changed files inside the call, re-checks freshness, serves only if that check passes
- concurrency: within one engine process, concurrent callers wait for the rebuild and are served once it succeeds
- refusal: when the rebuild fails or cannot run (for example the re-index errors, the files change again during it, there is no rebuild recipe, or a rebuild of this same content already failed and is not retried until the files change again), a structured refusal (corpus id, index hash, live hash, cause in its `heal` block) instead of an answer
- fail-closed: on a rebuild error the served index is left untouched
- hint-blind-spot: an edit that leaves a file's size and mtime unchanged is invisible to the hint; the periodic full re-verify catches it, not necessarily the next query
- source: `specs/30-freshness-and-self-heal.lts`
- source: `factlattice_engine/selfheal.py`
- source: `README.md`

### [IDEA-TYPED-WRITERS] Typed writers refuse malformed keys
- part-of: [CORE-IDEAS]
- key-grammar: `^[a-z][a-z0-9_-]*$`
- malformed-key: a line with any other key parses as prose; the field disappears from queries; lint reports only a warning (0 errors, exit 0)
- writers: `lts_add_property`, `lts_add_node`, `lts_repair_property`, `lts_rename_property`, `lts_remove_property`
- correction: unambiguous key spellings are fixed, each correction reported in the receipt
- refusal: any key that would need an invented fix
- whole-file-check: after every write the whole file is re-parsed; the parsed properties, values included, must change only by the intended edit, or nothing is written
- value-check: for an add or append, the stored value is compared with the parse of the submitted line on its own; a replaced or repaired value is not compared
- continuation-lines: replacing a wrapped value keeps the old continuation lines
- line-break-gap: neither check catches a character `str.splitlines` treats as a line break (for example U+2028, U+2029, U+0085, a vertical tab, a form feed or a carriage return); the value is written and parses cut off there, unless the rest parses as a property or heading, which the gate refuses; the receipt's `value_as_parsed` shows it
- ledger: every applied typed write to a file outside a temporary directory (the OS temp directory, `/tmp`, `/private/tmp`) adds a hash-chained record, with node and file hashes, to a per-file ledger in local engine state, readable with `lts_history`
- not-ledgered: edits made outside the writers; writes under a temporary directory
- ledger-failure: if the append fails, the write stands and the receipt says WRITTEN, BUT NOT LEDGERED
- source: `factlattice/authoring/edit.py`
- source: `factlattice/authoring/schema.py`
- source: `factlattice/ledger.py`
- source: `README.md`

### [IDEA-LINE-ACCOUNTING] Silent loss is reported
- part-of: [CORE-IDEAS]
- requirement: every non-blank line is either consumed by the parser or classified as deliberately ignorable ("line accounting")
- preview: `lts_preview` shows what a draft would become before it is written, including the lines and ids that would produce nothing
- source: `SPEC.md` §2.5
- source: `factlattice_engine/preview.py`

### [IDEA-CONFIDENCE] Confidence annotations
- part-of: [CORE-IDEAS]
- schema: any node or edge may carry `confidence: {value: 0.0–1.0, blast_radius, rationale, assessed_by, assessed_at}`; the schema is part of the core
- assessment: left to each domain (principle P7)
- source: `SPEC.md` §7

### [IDEA-DECISION-PREMISE] Decisions with their premise
- part-of: [CORE-IDEAS]
- record: `{change, context_for_change}`, including `rejected_alternative`, `counter_argument` and `invalidating_assumption`
- reading: "this holds while X holds"
- source: `APPENDIX-DECISION-LIFECYCLE.md`
- source: `SPEC.md`
- source: `EXEC-SUMMARY.md`

### [IDEA-PROFILES] Domain profiles
- part-of: [CORE-IDEAS]
- registration: prefixes, edge types and properties, per domain, under namespaces: core, registered or private
- validation: profile-driven (prefixes, required properties, enum values and more), implemented in the linter at warning severity
- condition: runs only when a machine-readable profile is in scope
- reference-profiles: those in `domain-profiles/` do not yet provide one, so with them the rules are inert, and lint says so in its output
- source: `SPEC.md`
- source: `README.md`

## [MEASURED] Measured and written down in the repository
- part-of: [REPO]

### [M-SPEC-AMBIGUITY] Specification-ambiguity experiments
- part-of: [MEASURED]
- design: four small experiments; each round gives the same task to implementers working from prose alone or from a spec or graph
- round-2: the prose-only implementation computed p95 = 95.5 where 100.0 was intended
- round-3: working from shorthand plus the graph, the implementer got 100.0
- round-4: the no-graph and graph branches reached opposite SLA verdicts on the same data
- reproducible: rounds 1 and 2, from the repository
- reported-only: rounds 3 and 4; their corpus is not published
- source: `case-studies/specification-ambiguity/`

### [M-PREVIEW-CONTROL] Preview against the shipped control fixture
- part-of: [MEASURED]
- result: lint reports 0 errors (the losses appear only among its warnings); `lts_preview` names the document's root node and three authored relationships as lost
- source: `README.md`
- source: `factlattice_engine/preview.py`

### [M-STRUCTURE-ONLY-FIND] Structure-only find on a corpus without subject matter
- part-of: [MEASURED]
- observed-on: a private corpus (not published)
- result: a structure-only `find` returned six filename hits over a corpus with no subject-matter content
- consequence: the `extent.body_scanned` and `scope_note` fields were added
- source: `README.md`
- source: `specs/20-engine-layers.lts`

### [M-PRIVATE-FANOUT] Unscoped search across personal corpora
- part-of: [MEASURED]
- result: 20 nodes from 8 agents' personal corpora among 582 hits
- consequence: the private-corpus rule
- source: `specs/20-engine-layers.lts`

### [M-RETRIEVAL-MISSES] Retrieval check
- part-of: [MEASURED]
- result: 128 misses in 1,443 questions
- consequence: the semantic-suggestion tool
- source: `specs/50-decisions.lts`

## [NOT-MEASURED] Not measured, or stated without data in the repository
- part-of: [REPO]
- item: whether agents using FactLattice as memory work better than agents using any other memory approach (`README.md`)
- item: the density figure in SPEC P1, 15–30 addressable concepts per 1000 tokens against 3–8 for prose, given without a source (`SPEC.md`)
- item: the analysis of 171 documents that SPEC §0 names as the principles' origin, not included (`SPEC.md`)
- item: recall on undeclared relationships, given up by design (`README.md`)
- item: profile-driven validation against the shipped reference profiles, which do not supply the machine-readable vocabulary the implemented rules need (`README.md`)
- item: prose-mention edges, rule E6, reserved and not implemented at conformance level 1 (`SPEC.md`)
- item: use outside the maintainer's own deployment (no file; an absence)

## [KEY-FILES] Key files
- part-of: [REPO]
- file: `README.md`: what factlattice-engine does, quick start, agent memory, guarantees and layers, notation, profiles, methodology
- file: `EXEC-SUMMARY.md`: the short account of the approach
- file: `USE-CASES.md`: five worked cases, each with what is only partly supported
- file: `SPEC.md`: the specification, marked DRAFT (P0–P7, parser contract, engine contract, flows, confidence, knowledge evolution, profiles)
- file: `APPENDIX-DECISION-LIFECYCLE.md`: session-keyed decisions, orientation queries, `re-derive-via`
- file: `APPENDIX-VERSIONING.md`: DELTA change records, snapshots, session handoff
- file: `SESSION-LIFECYCLE.md`: a design example for session-start context loading, hooks and engineering logs
- file: `specs/`: FactLattice specified in FactLattice; `specs/30-freshness-and-self-heal.lts` is the freshness contract
- file: `domain-profiles/agentic_decisions.lts`: the decision-graph profile for agent systems
- file: `factlattice/parser.py`, `factlattice_engine/server.py`, `factlattice_engine/tools.py`: parser, MCP tool definitions, query implementation
- file: `case-studies/specification-ambiguity/`: the experiments
- file: `docs/PROCEDURES-AS-GRAPH-NODES.md` and `docs/procedures-example/`: procedures as nodes (shape, steps as a flow, finding them by prefix, a decision matrix, a domain profile whose required-property rules fire, with a control in `tests/test_procedures_example.py`)
