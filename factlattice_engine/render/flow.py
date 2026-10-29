"""[LEC-RENDER] The FLOW WALK — a thin, pure subgraph selector.

A FLOW IS A GRAPH. There is no flow data model here and none is needed: this module
selects a subgraph out of the graph the parser already built, and hands it to the
generic emitter in `mermaid.py`. It is deliberately small — all the diagram knowledge
lives in the emitter, all the corpus/freshness knowledge lives in `tools.LtsEngine`.

Two edge families do two different jobs:

  MEMBERSHIP (`CONTAINS` / `PART_OF`)  answers "which nodes ARE this flow?" — it
      defines the subgraph but is NOT drawn, because a container-to-step arrow is
      not a step transition. Returned separately as `membership_edges`.

  SEQUENCE (`BEFORE` / `SEQUENCE` / `CAUSES` / `PRECONDITION_FOR` / `ENABLES` /
      `CASCADES_TO`) answers "what follows what?" — these ARE the arrows.

`AFTER` is deliberately NOT in the default sequence set: `A AFTER B` runs B -> A in
flow order, so mixing it in would silently invert arrows. A caller who wants it can
pass it via `edge_types` and accept the direction as authored.

Convergence needs no code — two sequence edges into one node simply produce two
arrows into one box.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# Forward-directed sequence types only (see `AFTER` note above).
FLOW_EDGE_TYPES: Tuple[str, ...] = (
    "BEFORE", "SEQUENCE", "CAUSES", "PRECONDITION_FOR", "ENABLES", "CASCADES_TO",
)
MEMBERSHIP_EDGE_TYPES: Tuple[str, ...] = ("CONTAINS", "PART_OF")

DEFAULT_MAX_DEPTH = 8


@dataclass
class FlowWalk:
    """The STRUCTURED half of an `lts_flow` answer (the Mermaid text is the other).

    `nodes` is in deterministic discovery order: the start node, then its members in
    membership-discovery order, then sequence-BFS discoveries.
    """
    start_node: str
    nodes: List[dict] = field(default_factory=list)
    edges: List[dict] = field(default_factory=list)              # sequence (drawn)
    membership_edges: List[dict] = field(default_factory=list)   # structure (not drawn)
    container: Optional[str] = None    # start node acting purely as a container
    max_depth_reached: bool = False

    def node_ids(self) -> List[str]:
        return [str(n.get("id")) for n in self.nodes]


def _bare(etype: object) -> str:
    """Namespaced `x.TYPE` compares on its suffix, exactly like `_layer_for_type`."""
    return str(etype or "").split(".")[-1]


def walk_flow(*, start_node: str,
              get_node: Callable[[str], Optional[dict]],
              has_node: Callable[[str], bool],
              fwd_adj: Dict[str, Sequence[Tuple[str, dict]]],
              rev_adj: Dict[str, Sequence[Tuple[str, dict]]],
              edge_types: Optional[Sequence[str]] = None,
              membership: bool = True,
              direction: str = "forward",
              max_depth: int = DEFAULT_MAX_DEPTH) -> FlowWalk:
    """Select the flow subgraph rooted at `start_node`. Pure — no I/O, no corpus.

    Step 1 (membership): descend `CONTAINS` forward and `PART_OF` reverse from the
      start to collect the flow's steps. A flow authored as
      `### [FLOW-X]` + steps carrying `- part-of: [FLOW-X]` is therefore addressable
      by its container id, which is the ergonomic entry point.
    Step 2 (sequence): BFS along the sequence edge types from EVERY seed (start +
      members), bounded by `max_depth`.
    Step 3: keep every sequence edge whose BOTH endpoints landed in the node set —
      that is what makes convergence, and back-edges/loops, appear automatically.
    """
    et = {t.upper() for t in (edge_types or FLOW_EDGE_TYPES)}
    mem_types = set(MEMBERSHIP_EDGE_TYPES)
    depth_hit = False

    if not has_node(start_node):
        return FlowWalk(start_node=start_node)

    order: List[str] = [start_node]
    seen = {start_node}
    membership_edges: List[dict] = []

    # --- 1. membership ------------------------------------------------------
    members: List[str] = []
    if membership:
        frontier: List[Tuple[str, int]] = [(start_node, 0)]
        while frontier:
            nid, depth = frontier.pop(0)
            if depth >= max_depth:
                depth_hit = True
                continue
            children: List[Tuple[str, dict]] = []
            for _layer, edge in fwd_adj.get(nid, ()):
                if _bare(edge.get("type")) == "CONTAINS" and edge.get("source") == nid:
                    children.append((str(edge.get("target")), edge))
            for _layer, edge in rev_adj.get(nid, ()):
                if _bare(edge.get("type")) == "PART_OF" and edge.get("target") == nid:
                    children.append((str(edge.get("source")), edge))
            for child, edge in children:
                if not has_node(child):
                    continue
                if edge not in membership_edges:
                    membership_edges.append(edge)
                if child in seen:
                    continue
                seen.add(child)
                members.append(child)
                order.append(child)
                frontier.append((child, depth + 1))

    # --- 2. sequence BFS ----------------------------------------------------
    seeds = [start_node] + members
    frontier = [(s, 0) for s in seeds]
    while frontier:
        nid, depth = frontier.pop(0)
        if depth >= max_depth:
            depth_hit = True
            continue
        neighbours: List[str] = []
        for _layer, edge in fwd_adj.get(nid, ()):
            if edge.get("source") != nid:
                continue
            if _bare(edge.get("type")) in et:
                neighbours.append(str(edge.get("target")))
        if direction == "both":
            for _layer, edge in rev_adj.get(nid, ()):
                if edge.get("target") != nid:
                    continue
                if _bare(edge.get("type")) in et:
                    neighbours.append(str(edge.get("source")))
        for nb in neighbours:
            if nb in seen or not has_node(nb):
                continue
            seen.add(nb)
            order.append(nb)
            frontier.append((nb, depth + 1))

    # --- 3. induced sequence edges -----------------------------------------
    edges: List[dict] = []
    seen_edge_ids: set = set()
    for nid in order:
        for _layer, edge in fwd_adj.get(nid, ()):
            if id(edge) in seen_edge_ids:
                continue
            if _bare(edge.get("type")) not in et:
                continue
            if str(edge.get("source")) in seen and str(edge.get("target")) in seen:
                seen_edge_ids.add(id(edge))
                edges.append(edge)

    # --- container detection ------------------------------------------------
    # A start node with members but NO incident sequence edge is a pure container
    # (`[FLOW-DEPLOY]` holding steps). It is reported so the render layer can leave
    # it out of the diagram instead of drawing a disconnected box beside the flow.
    container = None
    if members and not any(str(e.get("source")) == start_node
                           or str(e.get("target")) == start_node for e in edges):
        container = start_node

    nodes = [get_node(nid) for nid in order]
    nodes = [n for n in nodes if n is not None]
    return FlowWalk(start_node=start_node, nodes=nodes, edges=edges,
                    membership_edges=membership_edges, container=container,
                    max_depth_reached=depth_hit)
