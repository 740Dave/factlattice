"""Hand-crafted .lts snippets exercising each parser rule + each edge layer."""

# Exercises: h1 doc tag + doc node, h2/h3 nodes, P1/P2/P3/P4 properties,
# all 4 edge layers (semantic/temporal/causal/entity) via Relationships +
# Causal Graph + property edges, an unregistered-namespace edge, and a
# dangling-target edge.
FULL = """\
---
scope: "A synthetic test corpus file."
domain: core.example
confidentiality: internal-only
---
# Synthetic Spec [SYSTEM:SYN-DOC]

> A blockquote summary that must not be parsed for edges.

## 1. Systems

### [SYN-A] Alpha node
- definition: the first system
- status: operational
- depends-on: ["[SYN-B]"]
- see-also: [SYN-C], [SYN-MISSING]
- **role**: bold-key role value

### [SYN-B] Beta node
- status: draft
- used-by: ["[SYN-A]"]
- tags:
  - one
  - two

## 2. Timeline

### [SYN-C] Gamma node
- definition: third system

## Relationships

### Semantic
- [SYN-A] IMPLEMENTS [SYN-C]
- [SYN-A] DEPENDS_ON [gov.example:SYSTEM:EXAMPLE-KEYSTORE]

### Temporal
- [SYN-B] BEFORE [SYN-C]

### Entity
- [SYN-C] USED_BY [SYN-A]

### Cross-Spec
- [SYN-A] example.GATES [SYN-B]

## Causal Graph

```
ENABLES: [SYN-A] -> [SYN-C]
PRECONDITION_FOR: [SYN-B] -> [SYN-A]
```

## Non-Goals
"""

# Minimal file with only a dangling relationship edge.
DANGLING = """\
---
domain: core.example
---
# Dangler [SPEC:DANGLE-DOC]

### [DNG-A] Anchor
- definition: anchor

## Relationships

### Semantic
- [DNG-A] DEPENDS_ON [DNG-DOES-NOT-EXIST]
"""

# Two files that define the SAME id (collision), plus a bare-private-type edge.
COLLIDE_1 = """\
---
domain: core.example
---
# One [SPEC:C1]

### [DUP-ID] First definition
- definition: first
"""

COLLIDE_2 = """\
---
domain: core.example
---
# Two [SPEC:C2]

### [DUP-ID] Second definition
- definition: second
"""

# Exercises the 3-way list-item discrimination: node-def vs edge vs property, plus
# a multi-leading-id node-def and a ref that should resolve to a list-defined node.
LIST_ITEM = """\
---
domain: core.example
---
# Watchlike [SYSTEM:LI-DOC]

## 1. Invariants

- [INV-W-1] **Single-holder conn.** Exactly one instance holds the conn; a design choice governs the rest ([INV-W-14]).
- [PULSE-GAUGE] **The pulse carries the health gauge.** Each tick reads the feed.
- [INV-W-4] [INV-W-8] **WO authority is procedural.** Two invariants merged into one statement.
- see-also: [PULSE-GAUGE]
- depends-on: ["[INV-W-1]"]

## 2. Edges that must NOT become nodes

### [LI-ANCHOR] Anchor node
- role: anchor

## Relationships

### Semantic
- [LI-ANCHOR] DEPENDS_ON [INV-W-1]

### Causal (typed, profile edges)
- [LI-ANCHOR] example.GATES some capability — a prose target, not a node
"""
