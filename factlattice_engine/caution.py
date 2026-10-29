"""[CANONICAL-EDGE-CAUTION] Part B: the `caution` annotation (semantic suggestions + lts_node).

A node that is a LEGACY LABEL for another node (outgoing CANONICAL edge) or that is SUPERSEDED
carries `caution: {kind, canonical_id, canonical_title, text}`. It ANNOTATES: it never changes
rank, membership or scores, and exact `lts_find` results do not carry it (v1).

Edge direction, measured against the parser: `A supersedes: [B]` mints A->B SUPERSEDES, and
`B superseded-by: [A]` (reversed=True) mints the SAME A->B edge. So a node is superseded iff it
has an INBOUND SUPERSEDES edge, for both key spellings. A CANONICAL edge runs legacy -> canonical,
so the legacy node has it OUTBOUND. The canonical edge wins when a node has both.

The edges come from `Partition.adjacency()` (the cached incidence map `lts_node` itself uses);
nothing is re-derived from properties.
"""
from __future__ import annotations

from typing import Optional

CANONICAL_EDGE_TYPE = "CANONICAL"      # one place to change if Part A names the edge differently
SUPERSEDES_EDGE_TYPE = "SUPERSEDES"
CAUTION_ENABLED = True                 # the B3 test switches it off to compare rank/scores


def _live(edge) -> bool:
    return not edge.get("dangling") and "unresolved_target" not in (edge.get("flags") or ())


def _label(node: dict) -> Optional[str]:
    v = (node.get("properties") or {}).get("canonical-label")
    if isinstance(v, (list, tuple)):
        v = "; ".join(str(x) for x in v if str(x).strip())
    v = str(v).strip() if v is not None else ""
    return v or None


def caution_for(part, node_id: str, fwd=None, rev=None) -> Optional[dict]:
    """The caution for `node_id` in `part`, or None. Pass `fwd`/`rev` to reuse one adjacency fetch."""
    if not CAUTION_ENABLED:
        return None
    if fwd is None or rev is None:
        fwd, rev = part.adjacency()
    nd = part.nodes.get(node_id)
    if nd is None:
        return None
    target = kind = None
    for _layer, e in fwd.get(node_id, ()):
        if (e.get("type") == CANONICAL_EDGE_TYPE and _live(e) and e.get("target") in part.nodes
                and e.get("target") != node_id):          # a self-edge is no pointer (review F1)
            if target is None or e["target"] < target:
                target, kind = e["target"], "legacy-label"
    if target is None:
        for _layer, e in rev.get(node_id, ()):
            if (e.get("type") == SUPERSEDES_EDGE_TYPE and _live(e) and e.get("source") in part.nodes
                    and e.get("source") != node_id):
                if target is None or e["source"] < target:
                    target, kind = e["source"], "superseded"
    if target is None:
        return None
    title = part.nodes[target].get("title")
    text = _label(nd) or (f"canonical definition: {title}" if kind == "legacy-label"
                          else f"superseded by: {title}")
    return {"kind": kind, "canonical_id": target, "canonical_title": title, "text": text}
