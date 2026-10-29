"""[LEC-RENDER] The SUBGRAPH-GENERIC Mermaid emitter.

This module knows NOTHING about flows. Its whole contract is:

    render_mermaid(nodes, edges) -> MermaidDiagram

where `nodes` is any list of node dicts (`id`, `title`, `properties`) and `edges`
is any list of edge dicts (`source`, `target`, optional `type` / `label`). A
process flow, a supersession chain, a dependency tree and a decision-lineage walk
are all just different node+edge sets pointed at the SAME function — which is the
[LEC-RENDER] requirement that a flow be "the first caller, not the only one".

Everything domain-shaped is a HOOK, never a hardcode:
  * `shape_fn(node)   -> shape name`   (default: diamond iff the node carries `condition`)
  * `label_fn(node)   -> label text`   (default: `condition` else `title` else `id`)
  * `edge_label_fn(e) -> label|None`   (default: edge `label`, else the edge TYPE
                                        unless it is a silent spine type)
  * `group_by=<property key>`          (default off; the flow layer passes 'actor')

DERIVED, NEVER STORED ([LEC-RENDER] why-derived-not-stored): nothing here writes a
diagram anywhere. The output is produced from the live graph at query time, so it
cannot drift from the corpus, and none of SMD's diagram-freshness policing apparatus
is needed.

------------------------------------------------------------------------------
ESCAPING — the single most likely source of silent breakage
------------------------------------------------------------------------------
Node ids in .lts are `[A-Z][A-Z0-9_-]*`; Mermaid ids must not contain `-` (a run of
two is the link operator), so every id is mapped to a deterministic safe id and the
mapping is RETURNED (`node_ids`) so a caller can correlate diagram back to corpus.

Labels are ALWAYS emitted double-quoted, which is what makes `(`, `)`, `[`, `]`,
`{`, `}`, `:`, `;` and unicode (`→`) safe inside label text (Mermaid's documented
"put the text in quotes" rule). The characters a quoted label still cannot carry
raw are escaped to Mermaid entity codes:

    #  -> #35;   (FIRST — we are about to introduce '#' ourselves)
    &  -> #amp;  "  -> #quot;   <  -> #lt;    >  -> #gt;

`&`/`<`/`>` matter because Mermaid renders labels as HTML by default; `"` would
terminate the quoted span. Newlines/tabs collapse to single spaces (a raw newline
inside a label is a syntax error).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from ..reason_codes import ENDPOINT_NOT_IN_NODE_SET_REASON

# Shape name -> (open delimiter, close delimiter). The label always sits between
# them double-quoted, e.g. rect -> `N_1["text"]`, decision -> `N_1{"text"}`.
SHAPES: Dict[str, tuple] = {
    "rect": ("[", "]"),
    "decision": ("{", "}"),
    "round": ("(", ")"),
    "stadium": ("([", "])"),
    "subroutine": ("[[", "]]"),
    "circle": ("((", "))"),
    "hexagon": ("{{", "}}"),
}
DEFAULT_SHAPE = "rect"

VALID_DIRECTIONS = ("TD", "TB", "BT", "LR", "RL")

# Edge types whose name adds nothing to a diagram — the plain sequence spine.
# Anything else (CAUSES, SUPERSEDES, DEPENDS_ON, ...) auto-labels the arrow, which
# is what makes a non-flow subgraph readable through the same emitter.
SILENT_EDGE_TYPES = frozenset({"BEFORE", "SEQUENCE"})

_UNSAFE_ID_RE = re.compile(r"[^0-9A-Za-z_]")
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------
def escape_label(text: object) -> str:
    """Make arbitrary node/edge text safe inside a DOUBLE-QUOTED Mermaid label.

    Order matters: `#` is escaped first, because every subsequent escape emits a
    `#...;` entity and a second pass would mangle them.
    """
    if text is None:
        return ""
    s = str(text)
    s = _WS_RE.sub(" ", s).strip()          # newlines/tabs/runs -> single spaces
    s = s.replace("#", "#35;")
    s = s.replace("&", "#amp;")
    s = s.replace('"', "#quot;")
    s = s.replace("<", "#lt;")
    s = s.replace(">", "#gt;")
    return s


def safe_node_id(node_id: object, used: Optional[Dict[str, str]] = None) -> str:
    """Deterministic .lts id -> Mermaid identifier.

    `DEC-1` -> `DEC_1`. `used` (safe_id -> original_id) is consulted so two distinct
    corpus ids that sanitize to the same token get disambiguated instead of silently
    fusing into one box.
    """
    base = _UNSAFE_ID_RE.sub("_", str(node_id))
    if not base or base[0].isdigit():
        base = "n_" + base
    if used is None:
        return base
    cand = base
    i = 2
    while cand in used and used[cand] != str(node_id):
        cand = f"{base}__{i}"
        i += 1
    return cand


# ---------------------------------------------------------------------------
# Default hooks
# ---------------------------------------------------------------------------
def default_shape_fn(node: dict) -> str:
    """A node carrying `condition` is a decision point -> diamond."""
    props = node.get("properties") or {}
    cond = props.get("condition")
    if isinstance(cond, str) and cond.strip():
        return "decision"
    if isinstance(cond, list) and cond:
        return "decision"
    return DEFAULT_SHAPE


def default_label_fn(node: dict) -> str:
    """A diamond asks a QUESTION, so a decision node labels with its `condition`;
    everything else labels with its title (falling back to the id)."""
    props = node.get("properties") or {}
    cond = props.get("condition")
    if isinstance(cond, list):
        cond = cond[0] if cond else None
    if isinstance(cond, str) and cond.strip():
        return cond.strip()
    title = node.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return str(node.get("id") or "?")


def default_edge_label_fn(edge: dict) -> Optional[str]:
    """The authored branch label ('yes'/'no'/'amber') wins; otherwise the edge TYPE
    is shown unless it is a silent spine type. This is what lets the SAME emitter
    render a legible SUPERSEDES chain with no flow-specific configuration."""
    label = edge.get("label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    etype = edge.get("type")
    if isinstance(etype, str) and etype and etype.split(".")[-1] not in SILENT_EDGE_TYPES:
        return etype
    return None


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class MermaidDiagram:
    text: str
    node_ids: Dict[str, str] = field(default_factory=dict)   # corpus id -> mermaid id
    node_count: int = 0
    edge_count: int = 0
    groups: List[str] = field(default_factory=list)
    skipped_edges: List[dict] = field(default_factory=list)  # endpoint not in node set

    def __str__(self) -> str:  # so a caller can just print(diagram)
        return self.text

    def to_dict(self) -> dict:
        return {"mermaid": self.text, "node_ids": self.node_ids,
                "node_count": self.node_count, "edge_count": self.edge_count,
                "groups": self.groups, "skipped_edges": self.skipped_edges}


# ---------------------------------------------------------------------------
# The emitter
# ---------------------------------------------------------------------------
def render_mermaid(nodes: Sequence[dict], edges: Sequence[dict], *,
                   direction: str = "TD",
                   group_by: Optional[str] = None,
                   shape_fn: Optional[Callable[[dict], str]] = None,
                   label_fn: Optional[Callable[[dict], str]] = None,
                   edge_label_fn: Optional[Callable[[dict], Optional[str]]] = None,
                   title: Optional[str] = None,
                   indent: str = "    ") -> MermaidDiagram:
    """Render ANY node+edge set as a Mermaid `flowchart`. Pure and deterministic.

    Node/edge ORDER is the caller's order (the walk's discovery order), so the same
    subgraph always produces byte-identical text — a prerequisite for diffing a
    derived diagram and for testing it.

    An edge whose source or target is not in `nodes` is DROPPED FROM THE DIAGRAM and
    reported in `skipped_edges` — never emitted, because a dangling reference would
    silently declare a phantom node in Mermaid (the exact class of silent breakage
    this layer exists to avoid).
    """
    shape_fn = shape_fn or default_shape_fn
    label_fn = label_fn or default_label_fn
    edge_label_fn = edge_label_fn or default_edge_label_fn
    direction = direction if direction in VALID_DIRECTIONS else "TD"

    # --- id mapping ---------------------------------------------------------
    used: Dict[str, str] = {}
    node_ids: Dict[str, str] = {}
    ordered: List[dict] = []
    for nd in nodes:
        nid = str(nd.get("id"))
        if nid in node_ids:
            continue                      # de-dup; first occurrence wins
        sid = safe_node_id(nid, used)
        used[sid] = nid
        node_ids[nid] = sid
        ordered.append(nd)

    # --- grouping (swimlanes) ----------------------------------------------
    groups: List[str] = []
    grouped: Dict[str, List[dict]] = {}
    ungrouped: List[dict] = []
    for nd in ordered:
        g = None
        if group_by:
            gv = (nd.get("properties") or {}).get(group_by)
            if isinstance(gv, list):
                gv = gv[0] if gv else None
            if isinstance(gv, str) and gv.strip():
                g = gv.strip()
        if g is None:
            ungrouped.append(nd)
        else:
            if g not in grouped:
                grouped[g] = []
                groups.append(g)
            grouped[g].append(nd)

    def _decl(nd: dict) -> str:
        sid = node_ids[str(nd.get("id"))]
        shape = shape_fn(nd)
        open_d, close_d = SHAPES.get(shape, SHAPES[DEFAULT_SHAPE])
        return f'{sid}{open_d}"{escape_label(label_fn(nd))}"{close_d}'

    lines: List[str] = []
    if title:
        # A YAML frontmatter title block is the only Mermaid-native way to title a
        # flowchart; `---` fences are part of the diagram source, not markdown.
        lines.append("---")
        lines.append(f"title: {escape_label(title)}")
        lines.append("---")
    lines.append(f"flowchart {direction}")

    for gi, g in enumerate(groups):
        lines.append(f'{indent}subgraph grp_{gi}["{escape_label(g)}"]')
        for nd in grouped[g]:
            lines.append(f"{indent}{indent}{_decl(nd)}")
        lines.append(f"{indent}end")
    for nd in ungrouped:
        lines.append(f"{indent}{_decl(nd)}")

    # --- edges --------------------------------------------------------------
    skipped: List[dict] = []
    emitted_edges = 0
    seen_edges: set = set()
    for e in edges:
        src, tgt = str(e.get("source")), str(e.get("target"))
        if src not in node_ids or tgt not in node_ids:
            skipped.append({"source": e.get("source"), "target": e.get("target"),
                            "type": e.get("type"), "reason": ENDPOINT_NOT_IN_NODE_SET_REASON})
            continue
        lbl = edge_label_fn(e)
        key = (src, tgt, lbl, e.get("type"))
        if key in seen_edges:
            continue
        seen_edges.add(key)
        a, b = node_ids[src], node_ids[tgt]
        if lbl:
            lines.append(f'{indent}{a} -->|"{escape_label(lbl)}"| {b}')
        else:
            lines.append(f"{indent}{a} --> {b}")
        emitted_edges += 1

    return MermaidDiagram(text="\n".join(lines), node_ids=node_ids,
                          node_count=len(ordered), edge_count=emitted_edges,
                          groups=groups, skipped_edges=skipped)
