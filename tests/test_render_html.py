"""[LEC-RENDER] Tests for the generic static-HTML emitter (`render.html`).

Mirrors the acceptance bar `test_render.py` sets for Mermaid: not "string contains
the word", but structural — every page carries the caller's tagline verbatim, no
raw script/markup escapes the sanitizer, and an allow-list style page set never
emits a page for content the caller didn't hand it.
"""

from __future__ import annotations

import re

from factlattice_engine.render import (
    escape_html, render_index, render_markdown_lite, render_page, slugify, write_site,
)


# ---------------------------------------------------------------------------
# escape_html / slugify
# ---------------------------------------------------------------------------
def test_escape_html_neutralises_script_tags():
    hostile = '<script>alert(1)</script> & "quoted" \'single\''
    escaped = escape_html(hostile)
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped
    assert "&amp;" in escaped


def test_slugify_basic():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("") == "page"


def test_slugify_disambiguates_collisions():
    used = {}
    a = slugify("Report One", used=used)
    used[a] = "Report One"
    b = slugify("Report One!!", used=used)  # sanitizes to the same base
    used[b] = "Report One!!"
    assert a != b


# ---------------------------------------------------------------------------
# render_markdown_lite
# ---------------------------------------------------------------------------
def test_markdown_lite_headings_and_paragraph():
    html = render_markdown_lite("# Title\n\nSome *text* with **bold** and `code`.")
    assert "<h1>Title</h1>" in html
    assert "<em>text</em>" in html
    assert "<strong>bold</strong>" in html
    assert "<code>code</code>" in html


def test_markdown_lite_table():
    md = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    html = render_markdown_lite(md)
    assert "<table>" in html
    assert "<th>a</th>" in html
    assert "<td>1</td>" in html


def test_markdown_lite_list_and_code_fence():
    md = "- one\n- two\n\n```\nraw <code>\n```\n"
    html = render_markdown_lite(md)
    assert "<ul><li>one</li><li>two</li></ul>" in html
    assert "<pre><code>raw &lt;code&gt;</code></pre>" in html


def test_markdown_lite_never_passes_through_raw_html():
    md = "<img src=x onerror=alert(1)>"
    html = render_markdown_lite(md)
    assert "<img" not in html
    assert "&lt;img" in html


# ---------------------------------------------------------------------------
# render_page — the tagline contract
# ---------------------------------------------------------------------------
def test_render_page_carries_tagline_in_body_and_footer():
    page = render_page(title="Foo", tagline="FOR INTERNAL USE ONLY",
                       blocks=[{"type": "prose", "text": "hello"}])
    assert page.text.count("FOR INTERNAL USE ONLY") >= 2  # banner + footer
    assert "<html" in page.text and "</html>" in page.text


def test_render_page_nda_tagline_gets_nda_class():
    page = render_page(title="Foo", tagline="Acme Customer Distribution under NDA Only",
                       blocks=[])
    assert 'class="tagline nda"' in page.text


def test_render_page_default_tagline_is_not_nda_styled():
    page = render_page(title="Foo", tagline="FOR ACME INTERNAL USE ONLY", blocks=[])
    assert 'class="tagline nda"' not in page.text
    assert 'class="tagline"' in page.text


def test_render_page_nav_marks_active():
    nav = [{"label": "Papers", "href": "papers.html"}, {"label": "Backlog", "href": "backlog.html"}]
    page = render_page(title="Papers", tagline="x", blocks=[], nav=nav, active_href="papers.html")
    assert 'href="papers.html" class="active"' in page.text
    assert 'href="backlog.html">' in page.text


def test_render_page_unknown_block_is_visible_not_silent():
    page = render_page(title="X", tagline="x", blocks=[{"type": "not-a-real-type", "foo": "bar"}])
    assert "unrecognised block" in page.text


# ---------------------------------------------------------------------------
# render_index — sparse-section notice (never fake content for an empty section)
# ---------------------------------------------------------------------------
def test_render_index_empty_section_shows_notice_not_fake_cards():
    page = render_index(
        title="Research State", tagline="x",
        sections=[{"heading": "Open Questions", "items": [],
                   "empty_note": "populates as the operational-knowledge corpora land"}],
    )
    assert "notice-empty" in page.text
    assert "populates as the operational-knowledge corpora land" in page.text
    assert 'class="card-grid"' not in page.text  # no card grid ELEMENT (CSS rule always present)


def test_render_index_populated_section_renders_cards():
    page = render_index(
        title="Papers", tagline="x",
        sections=[{"heading": "Papers", "items": [
            {"title": "Report A", "href": "a.html", "subtitle": "sub", "tags": ["t1"]},
        ]}],
    )
    assert "card-grid" in page.text
    assert "Report A" in page.text
    assert 'href="a.html"' in page.text


# ---------------------------------------------------------------------------
# write_site — allow-list discipline: only pages HANDED to it are written
# ---------------------------------------------------------------------------
def test_write_site_writes_exactly_the_pages_given(tmp_path):
    pages = {
        "index.html": render_page(title="Index", tagline="x", blocks=[]),
        "papers/a.html": render_page(title="A", tagline="x", blocks=[]),
    }
    result = write_site(pages, tmp_path)
    written_rel = {str(p).replace(str(tmp_path) + "/", "") for p in result.written}
    assert written_rel == {"index.html", "papers/a.html"}
    assert (tmp_path / "index.html").exists()
    assert (tmp_path / "papers" / "a.html").exists()
    all_files = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()}
    assert all_files == {"index.html", "papers/a.html"}


def test_write_site_excluded_content_never_appears_on_disk(tmp_path):
    """The allow-list model, exercised directly: a page never PASSED to write_site
    (e.g. because a consumer's classifier excluded it) cannot appear in the output
    by any path other than being in the `pages` dict handed in."""
    sensitive_page = render_page(title="Sensitive Morph Paper", tagline="x", blocks=[])
    allowed_pages = {"index.html": render_page(title="Index", tagline="x", blocks=[])}
    # sensitive_page is deliberately never added to allowed_pages.
    write_site(allowed_pages, tmp_path)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert "Sensitive Morph Paper" not in path.read_text(encoding="utf-8")
