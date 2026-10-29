"""[LEC-RENDER] The RENDER layer — the FOURTH co-equal lts-engine API surface.

Beside QUERY (`tools.py`), ANALYTICS (`analytics/`) and LINT (`lint/`), RENDER turns a
queried SUBGRAPH into a diagram or a document. Three pieces, deliberately separated:

    render.mermaid : the SUBGRAPH-GENERIC emitter — nodes+edges in, Mermaid out.
                     Knows nothing about flows.
    render.flow    : a thin flow WALK that selects a subgraph and feeds the emitter.
                     The first caller, not the only one.
    render.html    : the CONTENT-BLOCK-GENERIC static-site emitter — content blocks
                     in, self-contained HTML out. Knows nothing about any consumer's
                     domain vocabulary (no Acme, no "papers", no tagline text).

Everything is DERIVED at query time and stored nowhere ([LEC-RENDER]
why-derived-not-stored) — a derived diagram/page cannot go stale, so none of SMD's
diagram-drift policing (fingerprint checker / pre-push gate / generator-reviewer
agent pair) has any reason to exist.

The corpus-facing entry point is `LtsEngine.flow()` / the `lts_flow` MCP tool, which
routes through the SAME `_gate()` choke point as every other surface — so a stale
corpus yields the loud [INV-LTS-FRESH] marker and NO diagram. `render.html` has no
MCP entry point yet; it is consumed directly by static-site generators (e.g. the Acme
research-portal consumer) that build their own node/property gathering.
"""

from .flow import (
    DEFAULT_MAX_DEPTH, FLOW_EDGE_TYPES, MEMBERSHIP_EDGE_TYPES, FlowWalk, walk_flow,
)
from .html import (
    DEFAULT_CSS, Block, HtmlPage, SiteResult,
    escape_html, render_index, render_markdown_lite, render_page, slugify, write_site,
)
from .mermaid import (
    SHAPES, SILENT_EDGE_TYPES, VALID_DIRECTIONS, MermaidDiagram,
    default_edge_label_fn, default_label_fn, default_shape_fn,
    escape_label, render_mermaid, safe_node_id,
)

__all__ = [
    "DEFAULT_MAX_DEPTH", "FLOW_EDGE_TYPES", "MEMBERSHIP_EDGE_TYPES",
    "FlowWalk", "walk_flow",
    "SHAPES", "SILENT_EDGE_TYPES", "VALID_DIRECTIONS", "MermaidDiagram",
    "default_edge_label_fn", "default_label_fn", "default_shape_fn",
    "escape_label", "render_mermaid", "safe_node_id",
    "DEFAULT_CSS", "Block", "HtmlPage", "SiteResult",
    "escape_html", "render_index", "render_markdown_lite", "render_page",
    "slugify", "write_site",
]
