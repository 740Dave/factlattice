"""[LEC-RENDER] The SUBGRAPH/DOCUMENT-GENERIC static-HTML emitter.

The fourth render target beside Mermaid/flow: turns generic CONTENT BLOCKS (never
lts-engine internals, never a domain vocabulary) into small, dependency-free, static
HTML documents. Nothing here knows about Acme, "papers", "research state",
a confidentiality tagline's TEXT, or any corpus id — those are consumer concerns. A
consumer builds a list of `Block` dicts (see `BLOCK KINDS` below) plus a page title
and an arbitrary tagline STRING, and this module turns that into one self-contained
HTML file with no external requests (inline `<style>`, no CDN, no JS dependency).

This mirrors [LEC-RENDER]'s existing split (`mermaid.py` = generic emitter,
`flow.py` = the first caller): `html.py` is the generic emitter, a caller supplies
the blocks. `render_markdown_lite` is the one CONTENT-SHAPED convenience included
here because turning already-written Markdown prose (e.g. a report's SUMMARY.md)
into HTML is itself generic — it has no Acme vocabulary in it.

DERIVED, NEVER STORED, same as the Mermaid emitter: nothing here writes to a corpus.
`write_site` writes plain files to an arbitrary OUTPUT directory a caller names —
that output is the generated site, not a graph mutation.

------------------------------------------------------------------------------
BLOCK KINDS (the whole vocabulary a caller can use)
------------------------------------------------------------------------------
    {"type": "heading",   "text": str, "level": int = 2}
    {"type": "prose",     "text": str, "markdown": bool = False}
    {"type": "notice",    "text": str, "kind": "info"|"warning"|"empty" = "info"}
    {"type": "table",     "headers": [str, ...], "rows": [[cell, ...], ...]}
    {"type": "list",      "items": [str, ...], "ordered": bool = False}
    {"type": "properties","data": {key: value, ...}}
    {"type": "cards",     "items": [{"title","href","subtitle"?,"tags"?,"meta"?}, ...]}
    {"type": "raw_html",  "html": str}   # pre-escaped/pre-rendered fragment

An unrecognised block `type` is rendered as an escaped `<pre>` dump rather than
raised or silently dropped — a caller typo becomes visible in the page, not a
silent gap in the generated site.
"""

from __future__ import annotations

import html as _html_stdlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

Block = Dict[str, object]

_SLUG_UNSAFE_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Escaping / slugging
# ---------------------------------------------------------------------------
def escape_html(text: object) -> str:
    """Escape arbitrary text for safe placement in HTML body/attribute context."""
    if text is None:
        return ""
    return _html_stdlib.escape(str(text), quote=True)


def slugify(value: object, *, used: Optional[Dict[str, str]] = None) -> str:
    """Arbitrary id/title -> a filesystem/URL-safe slug (`lower-case-hyphenated`).

    `used` (slug -> original) disambiguates two distinct inputs that would
    otherwise collide, the same discipline `safe_node_id` applies to Mermaid ids.
    """
    base = _SLUG_UNSAFE_RE.sub("-", str(value).strip().lower()).strip("-")
    if not base:
        base = "page"
    if used is None:
        return base
    cand = base
    i = 2
    while cand in used and used[cand] != str(value):
        cand = f"{base}-{i}"
        i += 1
    return cand


# ---------------------------------------------------------------------------
# Minimal, dependency-free Markdown -> HTML (block-level; no raw HTML passthrough)
# ---------------------------------------------------------------------------
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_UL_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _inline_md(text: str) -> str:
    """Inline spans only: escape first, then re-introduce safe tags for spans."""
    escaped = escape_html(text)
    escaped = _INLINE_CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", escaped)
    escaped = _LINK_RE.sub(
        lambda m: f'<a href="{escape_html(m.group(2))}">{m.group(1)}</a>', escaped)
    escaped = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", escaped)
    escaped = _ITALIC_RE.sub(lambda m: f"<em>{m.group(1)}</em>", escaped)
    return escaped


def _split_table_row(line: str) -> List[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in row.split("|")]


def render_markdown_lite(text: str) -> str:
    """A small, deterministic Markdown subset -> HTML. No raw HTML passthrough
    (source HTML is escaped, never executed) — safe for machine-generated prose
    of unknown provenance (report SUMMARY.md files) with no sanitizer dependency.

    Supports: headings, paragraphs, fenced code blocks, unordered/ordered lists,
    blockquotes, horizontal rules, pipe tables, and inline bold/italic/code/links.
    Anything else falls through as an escaped paragraph — degrades safely, never
    raises on unfamiliar Markdown.
    """
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out: List[str] = []
    i, n = 0, len(lines)
    para: List[str] = []

    def flush_para() -> None:
        if para:
            out.append(f"<p>{_inline_md(' '.join(para).strip())}</p>")
            para.clear()

    while i < n:
        line = lines[i]

        if line.strip().startswith("```"):
            flush_para()
            i += 1
            code_lines: List[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # consume closing fence (or EOF)
            out.append(f"<pre><code>{escape_html(chr(10).join(code_lines))}</code></pre>")
            continue

        m = _HEADER_RE.match(line)
        if m:
            flush_para()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline_md(m.group(2).strip())}</h{level}>")
            i += 1
            continue

        if _HR_RE.match(line) and not line.strip().startswith("|"):
            flush_para()
            out.append("<hr>")
            i += 1
            continue

        if line.strip().startswith("|") or (
            "|" in line and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1] or "")
        ):
            if i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1] or ""):
                flush_para()
                headers = _split_table_row(line)
                i += 2
                rows: List[List[str]] = []
                while i < n and lines[i].strip().startswith("|"):
                    rows.append(_split_table_row(lines[i]))
                    i += 1
                out.append(_table_html(headers, rows))
                continue

        m = _QUOTE_RE.match(line)
        if m and line.strip():
            flush_para()
            quote_lines = []
            while i < n and _QUOTE_RE.match(lines[i]) and lines[i].strip():
                quote_lines.append(_QUOTE_RE.match(lines[i]).group(1))
                i += 1
            out.append(f"<blockquote><p>{_inline_md(' '.join(quote_lines))}</p></blockquote>")
            continue

        if _UL_RE.match(line):
            flush_para()
            items = []
            while i < n and _UL_RE.match(lines[i]):
                items.append(_UL_RE.match(lines[i]).group(1))
                i += 1
            out.append(_list_html(items, ordered=False))
            continue

        if _OL_RE.match(line):
            flush_para()
            items = []
            while i < n and _OL_RE.match(lines[i]):
                items.append(_OL_RE.match(lines[i]).group(1))
                i += 1
            out.append(_list_html(items, ordered=True))
            continue

        if not line.strip():
            flush_para()
            i += 1
            continue

        para.append(line.strip())
        i += 1

    flush_para()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Block renderers
# ---------------------------------------------------------------------------
def _table_html(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    thead = "".join(f"<th>{_inline_md(str(h))}</th>" for h in headers)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{_inline_md(str(c))}</td>" for c in row)
        body_rows.append(f"<tr>{cells}</tr>")
    return (f"<table><thead><tr>{thead}</tr></thead>"
            f"<tbody>{''.join(body_rows)}</tbody></table>")


def _list_html(items: Sequence[object], *, ordered: bool) -> str:
    tag = "ol" if ordered else "ul"
    lis = "".join(f"<li>{_inline_md(str(it))}</li>" for it in items)
    return f"<{tag}>{lis}</{tag}>"


def _properties_html(data: Dict[str, object]) -> str:
    rows = []
    for key, value in data.items():
        if isinstance(value, (list, tuple)):
            value_html = ", ".join(escape_html(v) for v in value)
        else:
            value_html = escape_html(value)
        rows.append(f"<dt>{escape_html(key)}</dt><dd>{value_html}</dd>")
    return f"<dl class=\"props\">{''.join(rows)}</dl>"


def _cards_html(items: Sequence[dict]) -> str:
    cards = []
    for item in items:
        title = escape_html(item.get("title", ""))
        href = escape_html(item.get("href", "#"))
        subtitle = item.get("subtitle")
        tags = item.get("tags") or []
        meta = item.get("meta")
        parts = [f'<a class="card" href="{href}">', f'<div class="card-title">{title}</div>']
        if subtitle:
            parts.append(f'<div class="card-subtitle">{escape_html(subtitle)}</div>')
        if tags:
            tag_html = "".join(f'<span class="tag">{escape_html(t)}</span>' for t in tags)
            parts.append(f'<div class="card-tags">{tag_html}</div>')
        if meta:
            parts.append(f'<div class="card-meta">{escape_html(meta)}</div>')
        parts.append("</a>")
        cards.append("".join(parts))
    return f'<div class="card-grid">{"".join(cards)}</div>'


def _render_block(block: Block) -> str:
    btype = block.get("type")
    if btype == "heading":
        level = int(block.get("level", 2) or 2)
        level = min(max(level, 1), 6)
        return f"<h{level}>{_inline_md(str(block.get('text', '')))}</h{level}>"
    if btype == "prose":
        text = str(block.get("text", ""))
        if block.get("markdown"):
            return render_markdown_lite(text)
        paras = [p for p in text.split("\n\n") if p.strip()]
        return "\n".join(f"<p>{_inline_md(p.strip())}</p>" for p in paras) or ""
    if btype == "notice":
        kind = str(block.get("kind", "info"))
        return (f'<div class="notice notice-{escape_html(kind)}">'
                f"{_inline_md(str(block.get('text', '')))}</div>")
    if btype == "table":
        return _table_html(block.get("headers", []), block.get("rows", []))
    if btype == "list":
        return _list_html(block.get("items", []), ordered=bool(block.get("ordered")))
    if btype == "properties":
        return _properties_html(dict(block.get("data") or {}))
    if btype == "cards":
        return _cards_html(list(block.get("items") or []))
    if btype == "raw_html":
        return str(block.get("html", ""))
    # Unrecognised block: visible, not silent (a caller typo shows up, doesn't vanish).
    return f'<pre class="unknown-block">unrecognised block: {escape_html(block)}</pre>'


# ---------------------------------------------------------------------------
# Page shell + result
# ---------------------------------------------------------------------------
DEFAULT_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
       margin: 0; line-height: 1.55; color: #1a1a1a; background: #fafafa; }
.tagline { background: #2b2b2b; color: #fff; text-align: center; padding: 6px 12px;
           font-size: 0.78rem; letter-spacing: 0.04em; font-weight: 600; }
.tagline.nda { background: #7a3b00; }
nav.site-nav { display: flex; gap: 1.25rem; padding: 0.9rem 1.5rem; background: #fff;
               border-bottom: 1px solid #e2e2e2; flex-wrap: wrap; }
nav.site-nav a { color: #1a1a1a; text-decoration: none; font-weight: 600; font-size: 0.95rem; }
nav.site-nav a.active { color: #0b5fff; }
nav.site-nav a:hover { text-decoration: underline; }
main { max-width: 920px; margin: 0 auto; padding: 1.5rem 1.5rem 4rem; }
h1, h2, h3, h4 { line-height: 1.25; }
h1 { font-size: 1.7rem; margin-top: 0; }
h2 { font-size: 1.3rem; margin-top: 2rem; border-bottom: 1px solid #eee; padding-bottom: 0.3rem; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: 0.92rem; }
th, td { border: 1px solid #ddd; padding: 0.4rem 0.6rem; text-align: left; }
th { background: #f0f0f0; }
code { background: #eee; padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.9em; }
pre { background: #1e1e1e; color: #eee; padding: 1rem; overflow-x: auto; border-radius: 6px; }
pre code { background: none; padding: 0; color: inherit; }
blockquote { border-left: 4px solid #ccc; margin: 1rem 0; padding: 0.2rem 1rem; color: #444; }
.notice { border-radius: 6px; padding: 0.9rem 1.1rem; margin: 1rem 0; }
.notice-info { background: #eaf2ff; border: 1px solid #b6d1ff; }
.notice-warning { background: #fff4e5; border: 1px solid #ffcf8a; }
.notice-empty { background: #f2f2f2; border: 1px dashed #bbb; color: #555; font-style: italic; }
dl.props { display: grid; grid-template-columns: max-content 1fr; gap: 0.25rem 1rem; }
dl.props dt { font-weight: 600; color: #555; }
dl.props dd { margin: 0; }
.card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
             gap: 1rem; margin: 1rem 0; }
a.card { display: block; border: 1px solid #ddd; border-radius: 8px; padding: 1rem;
         background: #fff; text-decoration: none; color: inherit; }
a.card:hover { border-color: #0b5fff; box-shadow: 0 1px 6px rgba(0,0,0,0.08); }
.card-title { font-weight: 700; margin-bottom: 0.3rem; }
.card-subtitle { color: #555; font-size: 0.88rem; margin-bottom: 0.4rem; }
.card-tags { display: flex; flex-wrap: wrap; gap: 0.3rem; margin-bottom: 0.3rem; }
.card-tags .tag { background: #eee; border-radius: 10px; padding: 0.05rem 0.55rem; font-size: 0.76rem; }
.card-meta { color: #888; font-size: 0.78rem; }
footer.site-footer { text-align: center; color: #888; font-size: 0.78rem; padding: 1.5rem; }
""".strip()


@dataclass
class HtmlPage:
    text: str
    title: str
    tagline: str
    block_count: int = 0

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.text


def render_page(*, title: str, tagline: str, blocks: Sequence[Block],
                nav: Optional[Sequence[dict]] = None,
                active_href: Optional[str] = None,
                inline_css: str = DEFAULT_CSS) -> HtmlPage:
    """Render ONE self-contained static HTML page from generic content blocks.

    `nav` is a list of `{"label", "href"}`; `active_href` (if given and matching a
    nav entry's `href`) marks it `.active` so the current section is visually
    obvious. Nothing here decides WHAT the tagline says — the caller does — this
    only guarantees every page renders it, in a fixed, unmissable banner position.
    """
    nav = nav or []
    nda = "nda" in tagline.lower() or "nda-only" in tagline.lower()
    tagline_class = "tagline nda" if nda else "tagline"
    nav_html = "".join(
        f'<a href="{escape_html(item.get("href"))}"'
        f'{" class=\"active\"" if active_href and item.get("href") == active_href else ""}>'
        f'{escape_html(item.get("label"))}</a>'
        for item in nav
    )
    body_html = "\n".join(_render_block(b) for b in blocks)
    text = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape_html(title)}</title>
<style>{inline_css}</style>
</head>
<body>
<div class="{tagline_class}">{escape_html(tagline)}</div>
<nav class="site-nav">{nav_html}</nav>
<main>
<h1>{escape_html(title)}</h1>
{body_html}
</main>
<footer class="site-footer">{escape_html(tagline)}</footer>
</body>
</html>
"""
    return HtmlPage(text=text, title=title, tagline=tagline, block_count=len(blocks))


def render_index(*, title: str, tagline: str, sections: Sequence[dict],
                 nav: Optional[Sequence[dict]] = None,
                 active_href: Optional[str] = None,
                 intro: Optional[str] = None) -> HtmlPage:
    """A listing page: `sections` = `[{"heading", "items": [card-dict, ...], "empty_note"?}, ...]`.

    An empty `items` list renders `empty_note` (or a generic placeholder) via the
    `notice-empty` block instead of an empty `<div class="card-grid">` — a sparse
    section says so, rather than looking like a rendering failure.
    """
    blocks: List[Block] = []
    if intro:
        blocks.append({"type": "prose", "text": intro})
    for section in sections:
        blocks.append({"type": "heading", "level": 2, "text": section.get("heading", "")})
        items = section.get("items") or []
        if items:
            blocks.append({"type": "cards", "items": items})
        else:
            note = section.get("empty_note") or "Nothing to show here yet."
            blocks.append({"type": "notice", "kind": "empty", "text": note})
    return render_page(title=title, tagline=tagline, blocks=blocks,
                       nav=nav, active_href=active_href)


@dataclass
class SiteResult:
    output_dir: str
    written: List[str] = field(default_factory=list)


def write_site(pages: Dict[str, HtmlPage], output_dir: Union[str, Path]) -> SiteResult:
    """Write every `{relative_path: HtmlPage}` under `output_dir`. Pure I/O, no
    templating decisions live here. Creates parent directories as needed."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: List[str] = []
    for rel_path, page in pages.items():
        dest = out / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(page.text, encoding="utf-8")
        written.append(str(dest))
    return SiteResult(output_dir=str(out), written=written)
