"""The sanctioned surface, audited against itself.

Four closures, each of which is a case of THE TOOL HAVING THE DEFECT IT EXISTS TO PREVENT:

  A1  the sanctioned WRITER could not emit a document the sanctioned LINTER accepts (the
      required-frontmatter intersection was EMPTY); could not express a doctype at all;
      MANUFACTURED the content-parsed-profile defect when asked for a PROFILE; and raised
      AttributeError on its own success path, so its accept branch had never run.

  A3  the heading grammar lets a CITATION hijack the node id, silently, producing a
      SUBSTITUTION — and the document arguing the graph is less connected than it looks
      has a section that is not in the graph.

  8   the engine's own reflexive verbs were DEFERRED behind a tool search while `Read`,
      `Edit` and `Write` were one step away, and three agents independently abandoned the
      engine for `grep` citing exactly that gradient.

  3   a lint headline computed with profile-driven rules INERT travelled as a verdict.
"""
from __future__ import annotations

import pytest

from factlattice.constants import REQUIRED_FRONTMATTER
from factlattice.parser import detect_id_hijack, is_profile_document, parse_text
from factlattice_engine.tools import dispatch


# ---------------------------------------------------------------------------
# A1. THE WRITER CAN NOW WRITE SOMETHING THE LINTER ACCEPTS
# ---------------------------------------------------------------------------
def _fm(**over):
    from factlattice.authoring.schema import Frontmatter
    base = dict(title="A control document", id="D-ROOT", domain="plt.t",
                author="test", date="2026-08-13",
                scope="s", audience="a", boundary="b")
    base.update(over)
    return Frontmatter(**base)


def test_the_writer_and_the_linter_now_agree_about_required_frontmatter():
    """⛔ THE INTERSECTION WAS EMPTY. `Frontmatter` was `extra="forbid"` over
    `{title,id,domain,author,date,status}` while V9 REQUIRED `{scope,audience,boundary}` —
    three keys the model refused. No document the writer could emit passed the linter, and
    neither side could see it because each was internally consistent."""
    fm = _fm()
    assert all(getattr(fm, k) for k in REQUIRED_FRONTMATTER)


def test_the_required_frontmatter_set_is_shared_not_twinned():
    """One definition. The next divergence must be a test failure, not an impossibility
    nobody can name."""
    from factlattice_engine.lint.profile_rules import REQUIRED_FRONTMATTER as V9
    assert tuple(V9) == tuple(REQUIRED_FRONTMATTER)


def test_frontmatter_accepts_a_corpus_house_convention_key():
    """`extra` is `allow` now, deliberately. Frontmatter keys mint no graph, so forbidding
    `not-this`/`confidentiality`/`artifact-readiness` bought no safety and cost the writer
    every document with a house convention. Strictness belongs on PROPERTY keys, where a
    bad key silently loses a field."""
    fm = _fm(**{"confidentiality": "internal-only"})
    assert fm.model_dump()["confidentiality"] == "internal-only"


def test_a_document_root_id_can_carry_its_doctype():
    """`NodeId`'s pattern forbids the colon, so no document this writer emitted could
    carry a `DOCTYPE:ID` form — the spelling every corpus in the ecosystem authors."""
    assert _fm(id="SPEC:D-ROOT").id == "SPEC:D-ROOT"


@pytest.mark.parametrize("tag", ["SPEC", "PROFILE", "DELTA", "REGISTRY"])
def test_a_document_tag_renders_as_a_doctype_the_parser_actually_reads(tag):
    """⛔ THE OLD RENDERER EMITTED A SECOND BRACKET — `# Title [ID] [PROFILE]` — which the
    heading grammar does not read as a doctype at all (FIRST id-like bracket wins). So
    `write_document(document_tag="PROFILE")` MANUFACTURED precondition-2's defect: a file
    registered as a profile, carrying no PROFILE tag, therefore PARSED AS CONTENT, its
    declarations answering content queries and appearing as dangle orphans.

    The sanctioned surface produced the defect it exists to prevent, which is worse than
    no surface: it is a trap with a good reputation."""
    from factlattice.authoring.schema import Document, Node, Property, _render
    doc = Document(frontmatter=_fm(), document_tag=tag, section="Vocabulary",
                   nodes=[Node(node_id="D-ITEM", title="A registered item here",
                               properties=[Property(key="prefix", value="DEC")])])
    text = _render(doc)
    h1 = next(l for l in text.splitlines() if l.startswith("# "))
    assert h1 == f"# A control document [{tag}:D-ROOT]"
    pf = parse_text(text, "probe.lts")
    assert pf.document_tag == tag
    assert is_profile_document(pf) is (tag == "PROFILE")
    assert "D-ROOT" in {n.id for n in pf.nodes}


def test_two_disagreeing_doctype_declarations_are_refused_not_silently_merged():
    """The frontmatter id and `document_tag` are two INDEPENDENT declarations of one fact.
    Silently picking one produces a document whose registration and whose tag disagree —
    the exact drift `lts_profile_audit` exists to find."""
    from factlattice.authoring.schema import WriteRefused, _document_heading
    with pytest.raises(WriteRefused):
        _document_heading("T", "SPEC:D-ROOT", "PROFILE")
    # ...and agreeing is fine, said once or twice.
    assert _document_heading("T", "SPEC:D-ROOT", "SPEC") == "# T [SPEC:D-ROOT]"
    assert _document_heading("T", "SPEC:D-ROOT", None) == "# T [SPEC:D-ROOT]"


def test_the_writers_success_path_does_not_raise_on_a_method_that_does_not_exist():
    """⛔ `write_document` returned `n.advisories()` — a method `Node` does not have. Every
    REFUSAL returned before that line, so the writer's refusals were exercised while its
    ACCEPT branch raised AttributeError and had, on the evidence, never once run to
    completion. A gate whose accept branch is dead has only ever been observed rejecting,
    which is precisely how it kept a good reputation while being unusable."""
    from factlattice.authoring.schema import (
        Document, Node, Property, _render, write_document,
    )
    doc = Document(frontmatter=_fm(), document_tag="SPEC", section="Body",
                   nodes=[Node(node_id="D-ITEM", title="An item with a long title",
                               properties=[Property(key="kind", value="measurement")])])

    def fake_preview(content, corpus=None):
        pf = parse_text(content, "draft.lts")
        return {"became": {"nodes": [{"id": n.id, "properties": dict(n.properties)}
                                     for n in pf.nodes],
                           "document_tag": pf.document_tag},
                "resolution": {"profile": {"available": True}}, "lost": []}

    import tempfile, os
    out = os.path.join(tempfile.mkdtemp(), "written.lts")
    receipt = write_document(doc, out, fake_preview)
    assert receipt["written"] == out
    assert receipt["document_tag"] == "SPEC"
    assert receipt["coercions"] == []
    assert os.path.isfile(out)
    assert _render(doc) == open(out, encoding="utf-8").read()


def test_the_writer_refuses_when_the_parser_read_a_different_doctype():
    """Authored-vs-parsed reconciliation, applied to the doctype for the same reason it is
    applied to the keys. A renderer test comparing bytes to bytes would have PASSED on the
    old two-bracket bug, because the bytes were exactly what the renderer intended and the
    renderer was wrong."""
    from factlattice.authoring.schema import (
        Document, Node, Property, WriteRefused, write_document,
    )
    doc = Document(frontmatter=_fm(), document_tag="PROFILE", section="Body",
                   nodes=[Node(node_id="D-ITEM", title="An item with a long title",
                               properties=[Property(key="kind", value="measurement")])])

    def lying_preview(content, corpus=None):
        pf = parse_text(content, "draft.lts")
        return {"became": {"nodes": [{"id": n.id, "properties": dict(n.properties)}
                                     for n in pf.nodes],
                           "document_tag": None},          # the OLD behaviour
                "resolution": {"profile": {"available": True}}, "lost": []}

    with pytest.raises(WriteRefused) as exc:
        write_document(doc, "/dev/null/never", lying_preview)
    assert "PARSED AS CONTENT" in str(exc.value)


# ---------------------------------------------------------------------------
# A3. NODE-ID HIJACK BY IN-TITLE CITATION
# ---------------------------------------------------------------------------
HIJACK = ("8. Independent replication of [FLCD-CONNECTIVITY], on a second corpus, "
          "by an author who did not know it existed [FL-RECALL-REPLICATION]")


def test_the_hijack_reproduces_in_isolation():
    """The negative control for the whole rule: the defect is real and the grammar really
    does mint the citation."""
    src = (f"---\nscope: s\naudience: a\nboundary: b\n---\n# D [D-ROOT]\n\n"
           f"## {HIJACK}\n\n- kind: measurement\n")
    ids = {n.id for n in parse_text(src, "x.lts").nodes}
    assert ids == {"D-ROOT", "FLCD-CONNECTIVITY"}
    assert "FL-RECALL-REPLICATION" not in ids


def test_the_detector_names_both_the_minted_and_the_intended_id():
    hit = detect_id_hijack(HIJACK)
    assert hit["shape"] == "citation_hijack"
    assert hit["minted"] == "FLCD-CONNECTIVITY"
    assert hit["intended"] == "FL-RECALL-REPLICATION"


@pytest.mark.parametrize("heading", [
    # ID-FIRST is a first-class declaration form. Measured: a first cut of this rule
    # fired on THREE of these across 178 live files and every one was correct authoring.
    "[ACME-WIDGET-CONFIRM] See It Run (the confirmation half of [ACME-GADGET-REPORT])",
    "[ACME-SAMPLE-UNIQUE-ENTRY] The launcher's singleton — a re-ratification of [ACME-SAMPLE-UNIQUE]",
    # An ordinal is numbering, not content: this is still ID-FIRST.
    "8. [SOME-ID] Title citing [OTHER-ID]",
    # Two ids mid-sentence: neither reading is more likely, and a detector that guesses
    # is a detector that gets switched off.
    "Migrating [OLD-SPEC] to [NEW-SPEC] safely",
    # One id: nothing to confuse.
    "Ordinary heading [ONLY-ONE]",
    "[ONLY-ONE] Ordinary heading",
], ids=["id-first-paren", "id-first-dash", "ordinal-id-first", "mid-sentence",
        "id-last-single", "id-first-single"])
def test_the_detector_does_not_fire_on_legitimate_headings(heading):
    assert detect_id_hijack(heading) is None


def test_two_adjacent_ids_are_a_multi_id_declaration_not_a_hijack():
    """Calling this a hijack would name the WRONG CAUSE and send the reader to fix the
    wrong thing. The author declared two and the grammar mints one — a real loss, its own
    kind, and at `warning` because the remedy is an authoring decision."""
    hit = detect_id_hijack("10. Raptor doctrine — the picket line [ACME-DPM-RAPTOR] · [ACME-DPM-PICKET]")
    assert hit["shape"] == "multi_id"


def test_the_hijack_rule_reaches_lint_at_error_severity(tmp_path):
    src = (f"---\nscope: s\naudience: a\nboundary: b\n---\n# D [D-ROOT]\n\n"
           f"## {HIJACK}\n\n- kind: measurement\n")
    (tmp_path / "d.lts").write_text(src, encoding="utf-8")
    res = dispatch("lts_lint", {"source": str(tmp_path)})
    hits = [f for f in res["findings"] if f["origin"] == "id_hijack"]
    assert len(hits) == 1
    assert hits[0]["kind"] == "id_hijack" and hits[0]["severity"] == "error"
    assert hits[0]["detail"]["intended"] == "FL-RECALL-REPLICATION"


def test_the_rule_is_demotable_for_a_corpus_that_means_the_first_id(tmp_path):
    from factlattice_engine.lint import lint
    src = (f"---\nscope: s\naudience: a\nboundary: b\n---\n# D [D-ROOT]\n\n"
           f"## {HIJACK}\n\n- kind: measurement\n")
    (tmp_path / "d.lts").write_text(src, encoding="utf-8")
    r = lint(str(tmp_path), id_hijack_severity={"id_hijack": "warning"})
    assert not r.has_errors


def test_the_grammar_is_the_parsers_own_not_a_second_copy():
    """A linter carrying its own idea of "id-shaped" drifts from the grammar it lints."""
    import factlattice_engine.lint.id_hijack as mod
    import inspect
    src = inspect.getsource(mod)
    assert "detect_id_hijack" in src
    assert "re.compile" not in src, "the lint layer grew its own bracket grammar"


# ---------------------------------------------------------------------------
# 8. SERVER-SIDE TOOL PROMINENCE
# ---------------------------------------------------------------------------
def test_the_reflexive_verbs_declare_themselves_always_load():
    """⛔ THE SERVER DECLARES ITS OWN REFLEXIVE VERBS. A client-side `alwaysLoad` fixes one
    machine; the server declaring them fixes every caller everywhere — including the
    stranger who installs this from the open-source release and never reads a manifest."""
    from factlattice_engine.server import ALWAYS_LOAD_TOOLS, TOOLS
    by_name = {t["name"]: t for t in TOOLS}
    for name in ALWAYS_LOAD_TOOLS:
        assert name in by_name, f"{name} is marked always-load and is not a tool"
        assert by_name[name].get("_meta", {}).get("anthropic/alwaysLoad") is True


def test_the_field_survives_to_tools_list():
    """Asserted through the TRANSPORT, not on the table: a `_meta` the server strips on
    the way out is a manifest decision nobody receives."""
    from factlattice_engine.server import ALWAYS_LOAD_TOOLS, Server
    resp = Server(tools=None).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    served = {t["name"]: t for t in resp["result"]["tools"]}
    assert set(ALWAYS_LOAD_TOOLS) <= set(served)
    for name in ALWAYS_LOAD_TOOLS:
        assert served[name]["_meta"]["anthropic/alwaysLoad"] is True


def test_the_long_tail_stays_deferred():
    """Prominence is ZERO-SUM against context. A tool reached deliberately loses nothing
    by costing a search, and marking everything is the same as marking nothing."""
    from factlattice_engine.server import TOOLS
    deferred = {"lts_aggregate", "lts_flow", "lts_profile_audit", "lts_property_loss",
                "lts_preview", "lts_selfcheck", "lts_lint", "lts_traverse",
                "lts_capabilities"}
    by_name = {t["name"]: t for t in TOOLS}
    for name in deferred:
        assert "_meta" not in by_name[name], f"{name} claimed prominence it does not need"


def test_the_write_verbs_are_prominent_because_their_own_text_demands_it():
    """`lts_add_property`'s description says *"Use this instead of Edit/Write for ANY
    `- key: value`… There is no 'too small to bother'."* ⛔ `Edit` and `Write` are base
    tools. Exhorting against a gradient the manifest creates is a memory against a cost,
    and memory loses."""
    from factlattice_engine.server import ALWAYS_LOAD_TOOLS, TOOLS
    spec = next(t for t in TOOLS if t["name"] == "lts_add_property")
    assert "instead of Edit/Write" in spec["description"]
    assert "lts_add_property" in ALWAYS_LOAD_TOOLS
    assert "lts_repair_property" in ALWAYS_LOAD_TOOLS


def test_repair_is_advertised_with_a_schema_and_dispatches():
    from factlattice_engine.server import TOOLS
    from factlattice_engine.tools import LTS_ALL_TOOL_NAMES
    spec = next(t for t in TOOLS if t["name"] == "lts_repair_property")
    assert set(spec["inputSchema"]["required"]) == {"path", "node_id", "line", "key"}
    assert "value" in spec["inputSchema"]["properties"]      # optional, on purpose
    assert "lts_repair_property" in LTS_ALL_TOOL_NAMES


# ---------------------------------------------------------------------------
# 3. SCOPE IS A FIRST-CLASS LINT FIELD; INERT RULES SUPPRESS THE HEADLINE
# ---------------------------------------------------------------------------
def _clean_doc(tmp_path, name="d.lts"):
    (tmp_path / name).write_text(
        "---\nscope: s\naudience: a\nboundary: b\n---\n# D [D-ROOT]\n\n"
        "## A node [N-ONE]\n\n- kind: measurement\n- see-also: [D-ROOT]\n",
        encoding="utf-8")
    return tmp_path / name


def test_a_lint_result_declares_the_scope_it_ran_at(tmp_path):
    """"The same file, two opposite lint verdicts, depending on scope" — 12 errors alone,
    0 inside its corpus, all 12 phantom dangles."""
    f = _clean_doc(tmp_path)
    single = dispatch("lts_lint", {"source": str(f)})
    whole = dispatch("lts_lint", {"source": str(tmp_path)})
    assert single["scope"]["kind"] == "single_file"
    assert "SINGLE-FILE SCOPE" in single["scope"]["caveat"]
    assert whole["scope"]["kind"] == "directory"
    assert whole["scope"]["caveat"] is None


def test_an_inert_rule_set_emits_errors_null_not_a_bare_count(tmp_path):
    """⛔ THE ERROR COUNT IS THE FIELD CALLERS QUOTE, AND A NUMBER SITTING BESIDE A PROSE
    CAVEAT TRAVELS AS A NUMBER. The only way to stop it travelling without its caveat is
    to make it UNQUOTABLE. Same discipline as `lost: 0` not being a gate."""
    _clean_doc(tmp_path)
    res = dispatch("lts_lint", {"source": str(tmp_path)})
    assert res["rules_inert"], "this fixture has no profile; V1-V11 cannot have run"
    assert res["summary"]["errors"] is None and res["summary"]["warnings"] is None
    assert "did not run" in res["summary"]["suppressed_because"]
    # ...and the raw numbers survive, BEHIND the caveat rather than beside it.
    assert isinstance(res["summary"]["errors_observed"], int)
    assert res["summary"]["total"] == (res["summary"]["errors_observed"]
                                       + res["summary"]["warnings_observed"])


def test_rules_inert_names_what_did_not_run(tmp_path):
    _clean_doc(tmp_path)
    res = dispatch("lts_lint", {"source": str(tmp_path), "structure": False})
    joined = " ".join(res["rules_inert"])
    assert "V1-V11" in joined and "structural diagnostics" in joined


def test_the_default_dangle_narrowing_is_not_counted_as_an_inert_rule(tmp_path):
    """⛔ THE LOAD-BEARING RESTRAINT. `dangle=False` is the DEFAULT, so listing it would
    make `rules_inert` non-empty on every ordinary run, null the headline on every
    ordinary run, and turn a signal into a constant. A suppression that fires always is a
    suppression nobody reads. The narrowing is recorded in `scope` instead."""
    _clean_doc(tmp_path)
    res = dispatch("lts_lint", {"source": str(tmp_path)})
    assert not any("dangle" in r for r in res["rules_inert"])
    assert res["scope"]["orphan_check"] is False
    assert "do not read this result as 'no orphans'" in res["scope"]["narrowing"].lower()


def test_the_lint_extent_carries_its_denominator(tmp_path):
    _clean_doc(tmp_path)
    res = dispatch("lts_lint", {"source": str(tmp_path)})
    ext = res["extent"]
    assert ext["nodes_in_scope"] == 2 and ext["files_in_scope"] == 1
    assert ext["scope"] == res["scope"]
    assert ext["profile"]["available"] is False


def test_a_corpus_shaped_md_in_a_corpus_root_draws_a_diagnostic(tmp_path):
    """Two of the six documents proposing improvements to this engine are `.md`, and both
    were silently absent from the corpus registered to hold them — including the
    cross-cutting opinion on the whole series. `_SHADOW_SUFFIXES` covered `.bak` and not
    `.md`."""
    _clean_doc(tmp_path)
    (tmp_path / "06-cross-cutting-opinion.md").write_text("# prose\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# readme\n", encoding="utf-8")
    res = dispatch("lts_lint", {"source": str(tmp_path)})
    prose = [f for f in res["findings"] if f["kind"] == "unmigrated_prose"]
    assert [f["source_file"].endswith("06-cross-cutting-opinion.md") for f in prose] == [True]
    assert prose[0]["severity"] == "info"
