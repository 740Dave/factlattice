"""CANONICAL-EDGE lint -- `canonical-node: [ID]` (legacy node -> canonical node).

(The key is `canonical-node`; bare `canonical` is free-text status prose in existing
corpora and does not mint.) Five error-grade rules. Four run over the finalized graph's
CANONICAL edges (one parse, never a re-derivation of the minting rule); the fifth reads
the `canonical-node` property VALUE itself:

  canonical_node_not_refs    the value must be REFS ONLY: well-formed `[ID]` tokens joined
                            by separators (whitespace , ;). Prose, an unbracketed id, a
                            lowercase/padded/malformed bracket or a trailing gloss is an
                            error, because such a value stores as text and mints nothing
                            while lint would otherwise stay clean (review F2).

  canonical_node_empty      `- canonical-node:` with NO value (and no nested list): the parser
                            drops it, so it would be silent; it is a finding (delta review F3)
  canonical_target_missing  the target is defined nowhere (a `dangling` edge with no
                            `target_domain`). Only a DOTTED-DOMAIN qualified ref
                            (`[a.b:ID]`) carries a `target_domain` and is left to the
                            existing cross-corpus conventions; a non-dotted qualifier is
                            read as a doctype, dropped, and its bare id is linted here
  canonical_self            a node names itself as its own canonical
  canonical_multiple        more than one DISTINCT canonical target -- ambiguous, so
                            refusal-grade (the same target written twice is not multiple)
  canonical_cycle           A -> B -> ... -> A (length >= 2); reported once per cycle

Error severity from day one: the key is new, so no existing corpus is gated by it.
"""
from __future__ import annotations

from typing import Dict, List, Set

import re

import factlattice
from factlattice.parser import _BRACKET_RE, classify_bracket_token

ORIGIN = "canonical"
EDGE_TYPE = "CANONICAL"

K_MISSING = "canonical_target_missing"
K_SELF = "canonical_self"
K_MULTIPLE = "canonical_multiple"
K_CYCLE = "canonical_cycle"
K_NOT_REFS = "canonical_node_not_refs"
K_EMPTY = "canonical_node_empty"
KEY = "canonical-node"

CANONICAL_SEVERITY: Dict[str, str] = {
    K_NOT_REFS: "error", K_EMPTY: "error", K_MISSING: "error", K_SELF: "error", K_MULTIPLE: "error", K_CYCLE: "error",
}


def find_canonical_findings(corpus: "factlattice.ParsedCorpus") -> List[dict]:
    index = corpus.to_graph_index()
    alias = index.get("alias_index", {}) or {}
    edges = [e for layer in index.get("layers", {}).values()
             for e in layer.get("edges", []) if e.get("type") == EDGE_TYPE]
    out: List[dict] = []

    for n in corpus.nodes:
        if KEY not in n.properties:
            continue
        val = n.properties[KEY]
        items = val if isinstance(val, list) else [val]
        for item in items:
            if isinstance(item, str) and not item.strip():
                continue                      # reported once, as canonical_node_empty
            if not _refs_only(item):
                out.append({"kind": K_NOT_REFS, "node_id": n.id,
                            "message": (f"`{KEY}` on {n.id!r} must be refs only (well-formed "
                                        f"[ID] tokens and separators): {str(item)[:80]!r}"),
                            "source_file": n.source_file, "source_line": n.line_range[0],
                            "detail": {"value": str(item)[:200]}})

    out.extend(_empty_values(corpus))

    def add(kind, e, msg, **detail):
        out.append({"kind": kind, "node_id": e["source"], "message": msg,
                    "source_file": e.get("source_file"), "source_line": e.get("source_line"),
                    "detail": detail})

    targets: Dict[str, List[dict]] = {}
    for e in edges:
        src, tgt = e["source"], alias.get(e["target"], e["target"])
        if src == tgt:
            add(K_SELF, e, f"node {src!r} names itself as its own canonical")
            continue
        if e.get("dangling") and not e.get("target_domain"):
            add(K_MISSING, e, f"canonical target {e['target']!r} of {src!r} is defined nowhere",
                target=e["target"])
        targets.setdefault(src, []).append(e)

    nxt: Dict[str, str] = {}
    for src, es in targets.items():
        distinct = sorted({alias.get(e["target"], e["target"]) for e in es})
        if len(distinct) > 1:
            add(K_MULTIPLE, es[0],
                f"node {src!r} has {len(distinct)} canonical targets {distinct}: "
                f"ambiguous, a node has at most one", targets=distinct)
        nxt[src] = distinct[0]

    seen: Set[str] = set()
    for start in sorted(nxt):
        path: List[str] = []
        cur = start
        while cur in nxt and cur not in seen and cur not in path:
            path.append(cur)
            cur = nxt[cur]
        if cur in path:
            cyc = path[path.index(cur):]
            e = next(x for x in targets[min(cyc)])
            add(K_CYCLE, e, "canonical cycle: " + " -> ".join(cyc + [cyc[0]]), cycle=cyc)
        seen.update(path)
    return out


def _refs_only(value) -> bool:
    """True iff `value` is one or more well-formed `[ID]` refs joined only by separators."""
    if not isinstance(value, str):
        return False
    rest, last, refs = [], 0, 0
    for m in _BRACKET_RE.finditer(value):
        if classify_bracket_token(m.group(1)) != "REF" or m.group(1) != m.group(1).strip():
            return False
        rest.append(value[last:m.start()])
        last = m.end()
        refs += 1
    rest.append(value[last:])
    return refs > 0 and all(not seg.replace(",", " ").replace(";", " ").strip() for seg in rest)


_EMPTY_RE = re.compile(r"^\s*[-*]\s+" + re.escape(KEY) + r"\s*:\s*$")


def _empty_values(corpus) -> List[dict]:
    """`- canonical-node:` lines with no value. The parser is inconsistent about them (no
    trailing space: the key is DROPPED; trailing space: stored as ''), so this reads the
    parser's own LineCoverage record (`lines` + `node_body`, which names the node whose body
    each line belonged to), never the file path (a file-set parse's labels are basenames).
    A line whose node carries a non-blank value for the key, or a nested list, is not empty."""
    nodes = {(n.source_file, n.id): n for n in corpus.nodes}
    found: List[dict] = []
    for cov in getattr(corpus, "coverage", ()):
        for idx, line in enumerate(cov.lines):
            if not _EMPTY_RE.match(line):
                continue
            nid = cov.node_body.get(idx)
            n = nodes.get((cov.source_file, nid)) if nid else None
            if n is None:
                continue
            v = n.properties.get(KEY)
            if v not in (None, "", [], {}) and not (isinstance(v, str) and not v.strip()):
                continue
            found.append({"kind": K_EMPTY, "node_id": n.id,
                          "message": f"`{KEY}` on {n.id!r} has no value (line {idx + 1}); an empty "
                                     f"value mints no edge",
                          "source_file": cov.source_file, "source_line": idx + 1, "detail": {}})
    return found
