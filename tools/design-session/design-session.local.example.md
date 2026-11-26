# Design-session overlay: EXAMPLE

Copy to `private/design-session.local.md` (that path is gitignored and listed in PUBLISH-EXCLUDE, so it never
leaves this machine) and fill in YOUR deployment's details. This example names nobody, on purpose.

    mkdir -p private && cp tools/design-session/design-session.local.example.md private/design-session.local.md

The begin-design-session and end-design-session skills read the copy when it exists. Where it differs from
the skills or from `docs/SPEC-AUDIT-STEPS.md`, it governs. Delete any line you do not need.

## Auditor agent

- auditor-agent: <the name of the agent definition that runs your spec audits, or "none">
- auditor-procedure: <where its steps live, e.g. a node id in your own corpus; the agent fetches it at run time>
- if-procedure-unavailable: run docs/SPEC-AUDIT-STEPS.md by hand and say so in the report

## Roadmap search

- roadmap-search: <how this project's roadmap and backlog are searched: a tool and its scope, a corpus, or the files to read>

## Steps when a session opens

- <for example: enter your focus mode, writing the file your fleet's convention names>

## Steps when a session closes

- <for example: leave focus mode, then read your inbox before starting the audit>

## Where decision documents go

- decision-document-channel: <the tool or folder your stakeholder answers in, or "hand over as a file">

## Product checklists

- <a registry of per-product conformance checklists the auditor walks after the four directions, or "none">

## Harness prefixes

The extractor and the citation checker exclude a record whose text begins with a prefix that your harness types into
a session (it comes out marked as the stakeholder's). They ship a default list for the public harness; add the ones
your deployment's harness uses here, one per line. Each run names its source, and a file that cannot be read is a
loud warning. This is a lexical tripwire: it catches only the prefixes listed, and a line the operator types that
starts with one would be excluded too.

- harness-prefixes:
  - <a prefix your deployment's harness types at the start of a line, e.g. [MyHarness]>

The one layout that is read: the `- harness-prefixes:` line, then indented `- prefix` items directly under it, no blank line inside, ended by a top-level bullet, a heading or the end of the file. Only the one layout above is read. Some departures from it warn; any other may be read in part, wrongly, or not at all, without a warning. After any overlay edit, check the parsed list itself: run the extractor with --json and read harness_prefixes.prefixes (the counts line shows only how many were added).
