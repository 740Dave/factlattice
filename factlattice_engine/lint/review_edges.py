"""REVIEW-EDGE lint -- `attaches-to-hook: [REVHOOK-...]` and `applies-rule: [REVRULE-...]`.

Three rules (0 prior uses of either key over every registered root). ALL THREE are WARNINGS, never errors (post-review P2-5 and delta P2-A: they run on every corpus, customer corpora may
use the generic keys, and a misshapen extension inside its host must add 0 errors to the host). Two run over the finalized graph's ATTACHES_TO_HOOK / APPLIES_RULE
edges (one parse, never a re-derivation of the minting rule); the first reads the property VALUE:

  review_edge_not_refs       the value must be REFS ONLY: well-formed `[ID]` tokens joined by separators
                             (whitespace , ;). Prose, an unbracketed id or a malformed bracket stores as
                             text and mints NOTHING while lint would otherwise stay clean.
  review_edge_target_missing the target is defined nowhere in the corpus (a `dangling` edge with no
                             `target_domain`). These edges never cross corpora: ids are partition-local,
                             so a qualified cross-corpus pointer is a PROPERTY (`stub-of`), not this key.
  review_edge_wrong_shape    `attaches-to-hook` runs REVPROC- -> REVHOOK-; `applies-rule` runs
                             REVPROC- -> REVRULE- or REVRULE- -> any rule node (an existing rule is
                             pointed to, never copied). Anything else is a mis-wired extension graph.
"""
from __future__ import annotations

from typing import Dict, List

import factlattice

from .canonical import _refs_only

ORIGIN = "review-edges"

K_NOT_REFS = "review_edge_not_refs"
K_MISSING = "review_edge_target_missing"
K_SHAPE = "review_edge_wrong_shape"

# [post-review P2-5, delta P2-A] NO review-edge lint is ever an error, in any corpus. The shape and target rules run on EVERY corpus,
# including customer corpora that may already use the generic keys `applies-rule` / `attaches-to-hook` for
# their own purposes, and a misshapen extension living inside its host must not turn the HOST's lint red
# (C2). The reviewer's own extension validation is what REFUSES a misshapen extension.
REVIEW_EDGE_SEVERITY: Dict[str, str] = {K_NOT_REFS: "warning", K_MISSING: "warning", K_SHAPE: "warning"}

# edge type -> the property key that mints it (the lint is keyed on the parser's own table)
_KEYS = {"ATTACHES_TO_HOOK": "attaches-to-hook", "APPLIES_RULE": "applies-rule"}


def _shape_ok(etype: str, source: str, target: str) -> bool:
    if etype == "ATTACHES_TO_HOOK":
        return source.startswith("REVPROC-") and target.startswith("REVHOOK-")
    if source.startswith("REVPROC-"):
        return target.startswith("REVRULE-")
    return source.startswith("REVRULE-")


def find_review_edge_findings(corpus: "factlattice.ParsedCorpus") -> List[dict]:
    out: List[dict] = []
    for n in corpus.nodes:
        for key in _KEYS.values():
            if key not in n.properties:
                continue
            val = n.properties[key]
            for item in (val if isinstance(val, list) else [val]):
                if isinstance(item, str) and not item.strip():
                    continue
                if not _refs_only(item):
                    out.append({"kind": K_NOT_REFS, "node_id": n.id,
                                "message": (f"`{key}` on {n.id!r} must be refs only (well-formed [ID] "
                                            f"tokens and separators): {str(item)[:80]!r}"),
                                "source_file": n.source_file, "source_line": n.line_range[0],
                                "detail": {"key": key, "value": str(item)[:200]}})
    index = corpus.to_graph_index()
    for layer in index.get("layers", {}).values():
        for e in layer.get("edges", []):
            et = e.get("type")
            if et not in _KEYS:
                continue
            src, tgt = e["source"], e["target"]
            base = {"node_id": src, "source_file": e.get("source_file"), "source_line": e.get("source_line")}
            if e.get("dangling") and not e.get("target_domain"):
                out.append({**base, "kind": K_MISSING,
                            "message": f"`{_KEYS[et]}` target {tgt!r} of {src!r} is defined nowhere in this corpus",
                            "detail": {"key": _KEYS[et], "target": tgt}})
            if not _shape_ok(et, src, tgt):
                want = ("REVPROC- -> REVHOOK-" if et == "ATTACHES_TO_HOOK"
                        else "REVPROC- -> REVRULE-, or REVRULE- -> a rule node")
                out.append({**base, "kind": K_SHAPE,
                            "message": f"`{_KEYS[et]}` {src!r} -> {tgt!r} is the wrong shape (expected {want})",
                            "detail": {"key": _KEYS[et], "source": src, "target": tgt}})
    return out
