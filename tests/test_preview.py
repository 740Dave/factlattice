"""`lts_preview` — the authoring feedback loop ([FL-PREVIEW]).

STANDING RULE: every count here is EXACT. "non-zero" would pass on a tool that had
started reporting garbage, and the defect class this whole layer exists to catch is
precisely one that a loose assertion cannot see.

The two controls are frozen, structure-preserving derivatives of real files (see `fixtures/preview/PROVENANCE.md`):

  * POSITIVE — `spec41-broken-control.lts`. `lts_lint` returns 0 errors / exit 0, and
    its 5 warnings all land on lines 4-10 — ZERO findings of any kind on the lines that
    were actually lost. Its own root `[SPEC:LIB-01]` (line 3) mints no
    node; `[LIB-20]`, whose stated purpose is "Decompositional edges this
    realizes", yields 0 properties and 0 edges. Preview must find all of it, exactly.
    `test_lint_is_silent_on_exactly_the_lines_preview_reports` PINS that gap, so the
    justification for this whole layer stops being an anecdote.
  * NEGATIVE — `spec29-clean-control.lts`. Must come back CLEAN. Without it, a tool
    that flags everything looks identical to one that works.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from factlattice_engine.preview import (
    INTENT_ADD,
    INTENT_REPLACE,
    MODE_ISOLATED,
    MODE_TARGET_CORPUS,
    PreviewRefusal,
    preview,
)
from factlattice_engine.tools import LTS_ALL_TOOL_NAMES, dispatch

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "preview")
SPEC41 = os.path.join(FIXTURES, "spec41-broken-control.lts")
SPEC29 = os.path.join(FIXTURES, "spec29-clean-control.lts")

SPEC41_SHA = "82f70e6819129bf015d6c263613d04df37655bff3f9f68ca2c5b843664d66fd9"
SPEC29_SHA = "0ef3005c263c9131c6741c698bedca0f0cb2cc9efd5f1383f0b4eed5ae9b212e"

# --- the exact facts of the positive control --------------------------------
SPEC41_NODES = ["LIB-04", "LIB-08", "LIB-09", "LIB-05",
                "LIB-07", "LIB-20"]
# The document's own root, plus the three `- [RULE-*]:` lines under [LIB-20].
SPEC41_UNMINTED = ["LIB-01", "RULE-21",
                   "RULE-10", "RULE-22"]
SPEC41_UNMINTED_LINES = [3, 232, 233, 235]
# The document-metadata block orphaned by the un-`#`-prefixed root. Note the count is
# FIVE, not the ~20 lines the block occupies: a wrapped continuation line is prose, and
# only the five `- key:` HEADS are property-shaped. Recorded exactly, not rounded.
#
# (QUAL) 2026-08-11 — line 218 APPENDED, and this is a MEASURED MOVEMENT, not a
# re-baseline. `- scope (deliberately minimal, reviewable, non-irreversible):` is a
# property-shaped bullet whose KEY REGION carries a parenthetical qualifier. It was
# reaching the graph as NOTHING and reading as PROSE, so it was invisible to line
# accounting, invisible to `lts_lint`, and invisible to the `lost` channel of the very
# instrument this file tests. SL-21. The pin grows by exactly one line because exactly
# one new real loss became visible.
#
# (C8) Era 0, 2026-08-16 — lines 232/233/235 APPENDED, and it is the SAME movement one
# rule over. `- [RULE-*]: …` (SPEC41_UNMINTED_LINES) puts a bracketed id in the KEY
# position; the maintainer's 2026-08-11 ruling says FLAG, never mint, and until this rule that
# flag did not exist — the lines were `list_prose`, invisible to `factlattice.coverage`,
# and so invisible to THIS channel, even though `declared_nothing_minted` (a completely
# separate detector) already reported them from the other direction. The two channels
# now agree on these three lines for the first time; see
# `test_lint_now_covers_the_three_DX_lines_and_still_not_line_3` for what that means for
# `lts_lint`'s two origins.
SPEC41_UNACCOUNTED_LINES = [4, 5, 6, 7, 10, 218, 232, 233, 235]


def _read(p: str) -> str:
    with open(p, "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def p41():
    return preview(path=SPEC41)


@pytest.fixture(scope="module")
def p29():
    return preview(path=SPEC29)


# ---------------------------------------------------------------------------
# The controls themselves
# ---------------------------------------------------------------------------
def test_control_fixtures_are_byte_frozen():
    """A control that drifts is not a control. Every number below depends on these bytes."""
    for path, want in ((SPEC41, SPEC41_SHA), (SPEC29, SPEC29_SHA)):
        got = hashlib.sha256(open(path, "rb").read()).hexdigest()
        assert got == want, f"{os.path.basename(path)} changed: {got}"


# ---------------------------------------------------------------------------
# 1. WHAT IT BECAME
# ---------------------------------------------------------------------------
def test_spec41_became_exactly_six_nodes_and_zero_edges(p41):
    b = p41["became"]
    assert b["node_count"] == 6
    assert [n["id"] for n in b["nodes"]] == SPEC41_NODES
    assert b["edge_count"] == 0
    assert b["edges"] == []
    # No frontmatter block at all -> no document tag, no domain. The `- domain: lib.loans`
    # on line 4 is a PROPERTY line the parser never reached, not frontmatter.
    assert b["frontmatter"] == {}
    assert b["document_tag"] is None
    assert b["domain"] is None


def test_spec41_edges_section_yields_zero_properties_and_zero_edges(p41):
    """[LIB-20] exists as a node and is EMPTY — the exact silent loss.

    Its stated purpose is "Decompositional edges this realizes". It mints a node
    (the heading is well formed), and that node carries nothing: no properties, no
    edges. Three authored `- [RULE-*]:` lines produced neither.
    """
    node = next(n for n in p41["became"]["nodes"] if n["id"] == "LIB-20")
    assert node["property_count"] == 0
    assert node["properties"] == {}
    assert [e for e in p41["became"]["edges"] if e["source"] == "LIB-20"] == []


# ---------------------------------------------------------------------------
# 2 + 4. WHAT IT LOST / DECLARED VS MINTED
# ---------------------------------------------------------------------------
def test_spec41_root_id_is_declared_but_not_minted(p41):
    """The headline finding: the document's own `[SPEC:LIB-01]` root."""
    dm = p41["declared_vs_minted"]
    assert dm["declared_count"] == 10
    assert dm["minted_count"] == 6
    assert dm["not_minted"] == SPEC41_UNMINTED
    root = next(d for d in dm["declared"] if d["id"] == "LIB-01")
    assert root["minted"] is False
    assert root["line"] == 3
    assert root["shape"] == "bare_leading_bracket"
    assert root["doctype"] == "SPEC"
    assert "HEADING that lost its `#`" in root["why"]


def test_spec41_three_dx_lines_are_reported_lost_with_their_reason(p41):
    lost = p41["lost"]["declared_nothing_minted"]
    dx = [d for d in lost if d["id"].startswith("RULE-")]
    assert [d["id"] for d in dx] == SPEC41_UNMINTED[1:]
    assert [d["line"] for d in dx] == [232, 233, 235]
    for d in dx:
        assert d["shape"] == "bullet_leading_bracket"
        assert d["minted"] is False
        # The WHY must name the parser branch AND both remedies.
        assert "_LI_NODEDEF_RE" in d["why"]
        assert "## Relationships" in d["why"]


def test_spec41_metadata_block_is_unaccounted_with_the_enclosing_heading_reason(p41):
    la = p41["lost"]["line_accounting"]
    assert [v["line"] for v in la] == SPEC41_UNACCOUNTED_LINES
    # (QUAL) The metadata block's five, line 218, and the three (C8) `- [RULE-*]:` lines
    # are THREE DIFFERENT mechanisms, and the split is asserted rather than the union:
    # an orphaned-but-well-formed key is a STRUCTURAL problem (give the heading an
    # `[ID]`), a qualified key is a GRAMMAR problem (move the qualifier into the
    # value), and a bracketed id in the key position is a NAME-CHOICE problem (give the
    # field its own lowercase key). Collapsing them into one expectation would let any
    # one of the three stop firing without the test noticing.
    orphaned = [v for v in la if v["line"] not in (218, 232, 233, 235)]
    qualified = [v for v in la if v["line"] == 218]
    id_colon = [v for v in la if v["line"] in (232, 233, 235)]
    assert {v["kind"] for v in orphaned} == {"near_miss_property"}
    assert {v["near_branch"] for v in orphaned} == {"_parse_properties"}
    for v in orphaned:
        assert "nearest enclosing heading carries no" in v["why"]
    assert [v["kind"] for v in qualified] == ["near_miss_qualified_key_paren"]
    assert "move the qualifier into the VALUE" in qualified[0]["why"]
    assert {v["kind"] for v in id_colon} == {"near_miss_id_colon"}
    for v in id_colon:
        assert "FLAGGED, never minted" in v["why"]
    assert la[0]["text"].strip() == "- domain: lib.loans"


def test_spec41_total_loss_is_fifteen_across_three_named_channels(p41):
    lost = p41["lost"]
    # (QUAL) 5 -> 6 / 9 -> 10: line 218's qualified key. See SPEC41_UNACCOUNTED_LINES.
    # (C8) 6 -> 9 / 10 -> 13: the three `- [RULE-*]:` lines now ALSO raise a
    # `line_accounting` finding (near_miss_id_colon), on top of the `declared_nothing_
    # minted` one they already raised — see SPEC41_UNACCOUNTED_LINES. `total` counts
    # both channels, so it grows by 3, not by 0 — the two channels overlapping on a
    # LINE is not the same as them overlapping on a COUNT.
    # (V12b) 13 -> 15: the THIRD channel. `LIB-09` carries `pinned-to:` and
    # `exemplifies:` — both pure reference lists under NON-minting keys — so both mint
    # no edge and are now reported HERE, in the authoring loop, where before this fix
    # `lts_preview` said LOST:0 for them while `lts_lint` (V12) already flagged them
    # corpus-wide. This is the control that fails if preview goes silent again.
    assert len(lost["line_accounting"]) == 9
    assert len(lost["declared_nothing_minted"]) == 4
    assert len(lost["edge_shaped_property_minted_nothing"]) == 2
    assert lost["total"] == 15
    # The three PER-OCCURRENCE loss channels count toward `total`; the two DE-DUPLICATED
    # verb lists (the maintainer's rulings 2a/2b) are aggregate views over them, documented in the
    # same `channels` map so a reader can see every sub-key of `lost`.
    assert set(lost["channels"]) == {
        "line_accounting", "declared_nothing_minted",
        "edge_shaped_property_minted_nothing",
        "undeclared_edge_verbs", "case_mismatch_edge_verbs"}
    # The new channel names the (node, key) and offers the nearest minting alternative,
    # so the report is a WORKLIST, not just an alarm.
    by_key = {u["key"]: u for u in lost["edge_shaped_property_minted_nothing"]}
    assert set(by_key) == {"pinned-to", "exemplifies"}
    assert by_key["exemplifies"]["node_id"] == "LIB-09"
    assert by_key["exemplifies"]["nearest_minting_alternatives"]  # non-empty
    # SPEC41 has only genuinely-undeclared verbs, no case variants — so the (2b) list is
    # populated and the (2a) WARN is empty. That asymmetry is itself the control.
    assert lost["undeclared_edge_verbs"] == ["exemplifies", "pinned-to"]  # deduped, sorted
    assert lost["case_mismatch_edge_verbs"] == []
    assert lost["case_mismatch_warn"] is None


# ---------------------------------------------------------------------------
# 3. LINE ACCOUNTING
# ---------------------------------------------------------------------------
def test_spec41_line_accounting_is_exact(p41):
    # consumed 35 -> 167 / ignorable 196 -> 64 when the parser learned the WRAPPED
    # property value and the ID TABLE. Both counts moved for the same reason: 132
    # lines this control authors as value text or as acceptance rows were being
    # classified `prose` / `table_row` and dropped. `unaccounted` and `coverage_pct`
    # are UNCHANGED — the five near-misses this control exists to pin are the same
    # five lines (4-7, 10), which is what makes the movement an improvement in
    # capture rather than a change in the finding set.
    #
    # (QUAL) 2026-08-11: ignorable 64 -> 63, unaccounted 5 -> 6, coverage 97.9 -> 97.5.
    # This is the OPPOSITE movement to the one above and it is also an improvement: a
    # line MOVED from `ignorable` to `unaccounted`. Coverage going DOWN is what a newly
    # named loss class is supposed to do to it — the line was always lost, and the drop
    # in the percentage is the instrument finally saying so.
    #
    # (C8) Era 0, 2026-08-16: ignorable 63 -> 60, unaccounted 6 -> 9, coverage 97.5 ->
    # 96.2. Same movement again, same reason: the three `- [RULE-*]:` lines were
    # `list_prose` (ignorable) and are now `near_miss_id_colon` (unaccounted). Coverage
    # dropping a THIRD time is this instrument doing its job a third time, not drift.
    la = p41["line_accounting"]
    assert la == {
        "lines": 236, "consumed": 167, "ignorable": 60, "unaccounted": 9,
        "coverage_pct": 96.2, "rule": la["rule"],
    }
    assert la["consumed"] + la["ignorable"] + la["unaccounted"] == la["lines"]
    assert "COVERAGE, not equality" in la["rule"]


# ---------------------------------------------------------------------------
# THE TRIPWIRE
# ---------------------------------------------------------------------------
def test_spec41_losses_cannot_silently_stop_being_reported(p41):
    """FAIL if preview ever stops finding spec 41's losses.

    A preview that silently stops reporting them is the SAME failure one layer up —
    a tool whose clean result means nothing was checked. This test exists to make that
    regression noisy, so it asserts identity of the loss set, not a threshold.
    """
    unminted = {d["id"]: d["line"]
                for d in p41["lost"]["declared_nothing_minted"]}
    assert unminted == dict(zip(SPEC41_UNMINTED, SPEC41_UNMINTED_LINES))
    assert [v["line"] for v in p41["lost"]["line_accounting"]] == SPEC41_UNACCOUNTED_LINES
    assert p41["line_accounting"]["unaccounted"] == 9
    # 13 -> 15 (V12b): the two edge-shaped non-minting properties on LIB-09 are
    # now part of the loss set this tripwire refuses to let go silent.
    assert p41["lost"]["total"] == 15


def test_lint_now_covers_the_three_DX_lines_and_still_not_line_3(p41):
    """THE GAP THIS PIN GUARDED HAS NOW CLOSED TWICE — deliberately, and this test says how.

    ⛔ THE PREVIOUS ASSERTION (2026-08, `test_lint_is_silent_on_exactly_the_lines_preview
    _reports`) was that lint touched NONE of `SPEC41_UNMINTED_LINES`, and it closed with
    "if a future lint change starts covering those lines, this test fails and the
    justification gets re-examined rather than quietly inherited." This is the SECOND
    re-examination, written down instead of inherited.

    ROUND 1 (2026-08-13): `factlattice_engine.lint.key_loss` — an authored top-level
    bullet in a node body that MINTED NO PROPERTY is now a warning. It fires on lines
    232 / 233 / 235 under origin `key_loss`, because a `[NODE-REF]` used as a key is an
    unambiguous statement of intent that produces nothing.

    ROUND 2 (Era 0, 2026-08-16, C8 / the maintainer's 2026-08-11 ruling): `factlattice.coverage`
    gained `near_miss_id_colon` — `- [ID]: description` is now STRUCTURE-SHAPED to the
    LINE-ACCOUNTING rule too, not only to key_loss's shape classifier. The SAME three
    lines now ALSO fire under origin `coverage`. ⇒ THE TWO ORIGINS THIS TEST ONCE PROVED
    DISJOINT ON THESE LINES ARE NO LONGER DISJOINT — that is the correct outcome of a
    second, independent instrument reaching the same conclusion by a different route
    (line-shape vs bullet-shape), not a regression in either rule's scope.

    WHAT DID NOT CHANGE, and is the reason preview still exists:
      · LINE 3 — a `[SPEC:…]` heading that declared a node and minted none. It is not a
        bullet, is not `- [ID]: ...`-shaped, and is covered by NEITHER lint origin.
        Preview is still the only tool that reports it.
      · SEVERITY — still 0 errors, still exit 0. Both rules are warnings by design; see
        `lint/key_loss.py` and `lint/coverage.py`.
    """
    lint = dispatch("lts_lint", {"source": SPEC41})
    assert lint["has_errors"] is False
    assert lint["exit_code"] == 0

    by_origin = {}
    for f in lint["findings"]:
        by_origin.setdefault(f["origin"], []).append(f["source_line"])

    # `coverage` now covers the metadata block AND the three `- [RULE-*]:` lines (C8) —
    # SPEC41_UNACCOUNTED_LINES already carries all nine.
    assert sorted(by_origin["coverage"]) == SPEC41_UNACCOUNTED_LINES
    # ...and it still does not reach line 3 (a heading, covered by neither rule).
    assert 3 not in by_origin["coverage"]

    # `key_loss` still covers the three `- [RULE-*]:` bullets — and ONLY those, of the
    # declared-nothing-minted set. Line 3 is a heading, not a bullet.
    key_loss_lines = sorted(by_origin["key_loss"])
    assert set(SPEC41_UNMINTED_LINES) & set(key_loss_lines) == {232, 233, 235}
    assert 3 not in key_loss_lines
    assert [f for f in lint["findings"]
            if f["origin"] == "key_loss" and f["source_line"] == 232
            ][0]["detail"]["shapes"] == ["bracketed_ref_key", "uppercase_key"]

    # THE NEW OVERLAP, NAMED: 232/233/235 now carry findings from BOTH origins.
    assert set(by_origin["coverage"]) & set(by_origin["key_loss"]) == {232, 233, 235}
    assert [f["kind"] for f in lint["findings"]
            if f["origin"] == "coverage" and f["source_line"] == 232] == \
        ["near_miss_id_colon"]

    # ...and preview still reports BOTH sets, which remains the delta between the tools
    # (preview also names line 3, which no lint origin reaches).
    assert sorted(v["line"] for v in p41["lost"]["line_accounting"]) == \
        sorted(by_origin["coverage"])
    assert sorted(d["line"] for d in p41["lost"]["declared_nothing_minted"]) == \
        sorted(SPEC41_UNMINTED_LINES)


# ---------------------------------------------------------------------------
# THE NEGATIVE CONTROL
# ---------------------------------------------------------------------------
def test_spec29_negative_control_is_clean_on_every_channel_but_the_new_one(p29):
    """⛔ THE NEGATIVE CONTROL WAS NEVER CLEAN. It only looked clean against v20.

    This test asserted `unaccounted == 0`, `coverage 100.0%`, `lost.total == 0` and
    `"LOST: nothing"` in the report, and every one of those was TRUE against the
    silent-loss class list of the day — and FALSE about the file. Spec 29 carries SEVEN
    property-shaped bullets whose key region holds a parenthetical qualifier
    (`- ack (RECOMMENDED — loop-closing, two-part): …`, `- precedence (per-device vs
    broadcast): …`), each of which reached the graph as NOTHING while reading as prose.

    That is the whole argument for versioning a cleanliness claim (`silent-loss-classes`
    standing rule 4). A negative control does not prove a file is clean; it proves the
    file is clean AGAINST A NAMED LIST ON A NAMED DATE. Adding SL-21 invalidated this
    control's claim within the hour, exactly as the class list says it should.

    The three channels that were genuinely clean STAY asserted at zero — this is not a
    blanket re-baseline, and the declared-vs-minted channel in particular must not be
    allowed to drift under cover of the change.
    """
    assert p29["became"]["node_count"] == 18
    assert p29["became"]["edge_count"] == 58
    assert p29["became"]["document_tag"] == "SPEC"
    assert p29["became"]["domain"] == "lib.loans"
    # STILL CLEAN, and pinned so it stays that way.
    assert p29["declared_vs_minted"]["declared_count"] == 18
    assert p29["declared_vs_minted"]["minted_count"] == 18
    assert p29["declared_vs_minted"]["not_minted"] == []
    assert p29["lost"]["declared_nothing_minted"] == []
    assert p29["errors"] == []
    # NOT CLEAN, and now visible. 0 -> 7, all SL-21, all the same mechanism.
    la = p29["lost"]["line_accounting"]
    assert [v["line"] for v in la] == [159, 193, 194, 197, 204, 206, 209]
    assert {v["kind"] for v in la} == {"near_miss_qualified_key_paren"}
    assert p29["lost"]["total"] == 7
    assert p29["line_accounting"]["unaccounted"] == 7
    assert p29["line_accounting"]["coverage_pct"] == 97.3
    assert "LOST (7)" in p29["report"]


# ---------------------------------------------------------------------------
# ⛔ RESOLUTION MODE — declared in every response
# ---------------------------------------------------------------------------
def test_isolated_mode_declares_itself_and_reports_no_dangle(p41, p29):
    for out in (p41, p29):
        r = out["resolution"]
        assert r["mode"] == MODE_ISOLATED
        assert r["corpus_root"] is None
        assert r["reference_integrity"] == "NOT EVALUATED"
        assert out["reference_integrity"]["evaluated"] is False
        # The declaration must SAY it, in words, in the payload an author reads.
        assert "REFERENCE INTEGRITY WAS NOT EVALUATED" in r["declaration"]
        assert "NOT EVALUATED" in out["report"]
        # ...and no dangle may appear as a defect anywhere in isolated mode.
        assert "newly_dangling" not in out["reference_integrity"]
        assert all(not e.get("dangling") for e in out["became"]["edges"])


# ---------------------------------------------------------------------------
# CONTENT MODE IS PRIMARY (the protocol's step (b) has no file)
# ---------------------------------------------------------------------------
def test_content_mode_needs_no_file_and_matches_path_mode(p41):
    out = preview(_read(SPEC41), virtual_path="draft-in-flight.lts")
    assert out["source"]["origin"] == "content"
    assert out["source"]["path"] is None
    assert out["source"]["virtual_path"] == "draft-in-flight.lts"
    for key in ("became", "lost", "line_accounting", "declared_vs_minted"):
        assert out[key] == p41[key], f"{key} differs between content and path mode"


def test_virtual_path_gets_an_lts_suffix():
    out = preview("# t\n\n## [AB-X] Title\n- k: v\n", virtual_path="draft")
    assert out["source"]["virtual_path"] == "draft.lts"


@pytest.mark.parametrize("kwargs,reason", [
    ({}, "no_input"),
    ({"content": "x", "path": SPEC41}, "ambiguous_input"),
    ({"path": "/nonexistent/nope.lts"}, "file_not_found"),
])
def test_input_refusals(kwargs, reason):
    with pytest.raises(PreviewRefusal) as exc:
        preview(**kwargs)
    assert exc.value.reason == reason


# ---------------------------------------------------------------------------
# TARGET-CORPUS MODE — "what would this become IF it landed in corpus X"
# ---------------------------------------------------------------------------
FILE_A = """# A

## [AA-ONE] One
- k: v

## [AA-TWO] Two
- k: v
"""

FILE_B = """# B

## [BB-ONE] B One
- depends-on: [AA-TWO]
"""


@pytest.fixture()
def mini_corpus(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.lts").write_text(FILE_A, encoding="utf-8")
    (root / "b.lts").write_text(FILE_B, encoding="utf-8")
    return str(root)


def test_target_corpus_mode_declares_root_file_count_and_intent(mini_corpus):
    out = preview("# C\n\n## [CC-ONE] C One\n- k: v\n", corpus=mini_corpus,
                  virtual_path="c.lts")
    r = out["resolution"]
    assert r["mode"] == MODE_TARGET_CORPUS
    assert r["corpus_root"] == mini_corpus
    assert r["corpus_files"] == 2
    assert r["intent"] == INTENT_ADD
    assert r["replaces"] is None
    assert r["reference_integrity"] == "EVALUATED"
    assert out["reference_integrity"]["evaluated"] is True
    assert out["delta"] is None


def test_isolation_invents_a_dangle_that_target_corpus_mode_resolves(mini_corpus):
    """The 114-vs-56 contract, in miniature.

    The SAME content: previewed alone, the reference to `[AA-TWO]` is unresolvable and
    MUST NOT be reported as a defect; previewed against the corpus that defines it, it
    resolves. A tool that reported the isolated case as a dangle would be reporting a
    phantom, and would be ignored within a week.
    """
    draft = "# C\n\n## [CC-ONE] C One\n- depends-on: [AA-TWO]\n"
    solo = preview(draft, virtual_path="c.lts")
    assert solo["reference_integrity"]["evaluated"] is False
    assert "outbound_unresolved" not in solo["reference_integrity"]

    landed = preview(draft, corpus=mini_corpus, virtual_path="c.lts")
    assert landed["reference_integrity"]["outbound_unresolved_count"] == 0


def test_target_corpus_mode_reports_a_real_outbound_dangle(mini_corpus):
    draft = "# C\n\n## [CC-ONE] C One\n- depends-on: [ZZ-NOPE]\n"
    out = preview(draft, corpus=mini_corpus, virtual_path="c.lts")
    ri = out["reference_integrity"]
    assert ri["outbound_unresolved_count"] == 1
    assert ri["outbound_unresolved"][0]["to"] == "ZZ-NOPE"
    assert ri["outbound_unresolved"][0]["from"] == "CC-ONE"


# ---------------------------------------------------------------------------
# EDITS PREVIEW AS REPLACEMENTS — refuse rather than guess
# ---------------------------------------------------------------------------
def test_unnamed_edit_is_refused_not_guessed(mini_corpus):
    """Resolving an edit as an ADD yields a duplicate-id collision per node — noise
    shaped exactly like a real defect. Refuse, and name the remedy."""
    with pytest.raises(PreviewRefusal) as exc:
        preview(FILE_A, corpus=mini_corpus, virtual_path="a.lts")
    ref = exc.value
    assert ref.reason == "ambiguous_intent"
    assert ref.detail["colliding_ids"] == ["AA-ONE", "AA-TWO"]
    assert ref.detail["defined_in"] == ["a.lts"]
    assert ref.detail["likely_replaces"] == "a.lts"


def test_replaces_naming_a_missing_file_is_refused(mini_corpus):
    with pytest.raises(PreviewRefusal) as exc:
        preview(FILE_A, corpus=mini_corpus, replaces="not-here.lts")
    assert exc.value.reason == "replaces_not_found"


def test_replaces_makes_the_edit_collision_free(mini_corpus):
    out = preview(FILE_A, corpus=mini_corpus, replaces="a.lts")
    assert out["resolution"]["intent"] == INTENT_REPLACE
    assert out["resolution"]["replaces"] == "a.lts"
    assert out["resolution"]["replaces_inferred"] is False
    assert out["collisions"] == []
    assert out["delta"]["verdict"].startswith("CLEAN")


def test_path_inside_the_target_corpus_infers_the_replacement(mini_corpus):
    """NOT a guess: the caller handed us this exact path and it is in the corpus."""
    out = preview(path=os.path.join(mini_corpus, "a.lts"), corpus=mini_corpus)
    assert out["resolution"]["intent"] == INTENT_REPLACE
    assert out["resolution"]["replaces"] == "a.lts"
    assert out["resolution"]["replaces_inferred"] is True


# ---------------------------------------------------------------------------
# THE DELTA — an edit that silently drops a node
# ---------------------------------------------------------------------------
def test_delta_names_the_node_an_edit_drops_and_the_reference_it_breaks(mini_corpus):
    dropped = "# A\n\n## [AA-ONE] One\n- k: v2\n- extra: e\n"
    out = preview(dropped, corpus=mini_corpus, replaces="a.lts")
    d = out["delta"]
    assert d["against"] == "a.lts"
    assert [n["id"] for n in d["nodes_lost"]] == ["AA-TWO"]
    assert d["nodes_gained"] == []
    assert d["nodes_retained"] == 1
    assert d["properties_gained"] == {"AA-ONE": ["extra"]}
    assert d["properties_lost"] == {}
    assert d["verdict"].startswith("REGRESSION")
    # ...and the corpus reference that this edit BREAKS, attributed to the edit.
    assert out["reference_integrity"]["newly_dangling"] == ["BB-ONE -> AA-TWO"]
    assert out["reference_integrity"]["newly_resolved"] == []
    assert "NODE REMOVED [AA-TWO]" in out["report"]


def test_delta_reports_newly_resolved_when_an_edit_fixes_a_dangle(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.lts").write_text("# A\n\n## [AA-ONE] One\n- k: v\n", encoding="utf-8")
    (root / "b.lts").write_text("# B\n\n## [BB-ONE] B One\n- depends-on: [AA-TWO]\n",
                                encoding="utf-8")
    fixed = "# A\n\n## [AA-ONE] One\n- k: v\n\n## [AA-TWO] Two\n- k: v\n"
    out = preview(fixed, corpus=str(root), replaces="a.lts")
    ri = out["reference_integrity"]
    assert ri["newly_resolved"] == ["BB-ONE -> AA-TWO"]
    assert ri["newly_dangling"] == []
    assert out["delta"]["nodes_gained"] == ["AA-TWO"]
    assert out["delta"]["nodes_lost"] == []
    assert out["delta"]["verdict"].startswith("CLEAN")


def test_unknown_corpus_is_refused(tmp_path):
    with pytest.raises(PreviewRefusal) as exc:
        preview("# x\n", corpus=str(tmp_path / "nope"))
    assert exc.value.reason == "unknown_corpus"


# ---------------------------------------------------------------------------
# The declaration channel is NARROW — a reference is not a declaration
# ---------------------------------------------------------------------------
def test_references_are_not_reported_as_unminted_declarations():
    """Measured precision matters more than recall here: a rule that fires on ordinary
    references is a rule that gets switched off. Property values, mid-prose citations,
    fenced code and `## Relationships` triples are all references, never declarations."""
    doc = (
        "# Doc\n\n"
        "## [AA-ONE] One\n"
        "- pinned-to: [DEC-ELSEWHERE], [DEC-OTHER]\n"
        "- note: see [AA-TWO] for the rest\n"
        "\n"
        "Prose mentioning ([ACME-SOMETHING]) inline.\n"
        "\n"
        "```\n"
        "[FENCED-ID] not a declaration\n"
        "```\n"
        "\n"
        "## Relationships\n"
        "- [AA-ONE] DEPENDS_ON [AA-TWO]\n"
        "\n"
        "## [AA-TWO] Two\n"
        "- k: v\n"
    )
    out = preview(doc, virtual_path="d.lts")
    assert out["declared_vs_minted"]["not_minted"] == []
    assert out["declared_vs_minted"]["declared_count"] == 2
    assert out["lost"]["declared_nothing_minted"] == []


def test_a_list_node_def_mints_and_is_not_a_loss():
    doc = ("# Doc\n\n## [AA-ONE] One\n"
           "- [AA-KID] **A Child Node** — defined in the node-def form\n")
    out = preview(doc, virtual_path="d.lts")
    kid = next(d for d in out["declared_vs_minted"]["declared"] if d["id"] == "AA-KID")
    assert kid["shape"] == "list_node_def"
    assert kid["minted"] is True
    assert out["lost"]["declared_nothing_minted"] == []


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------
def test_registered_on_the_mcp_surface():
    from factlattice_engine.server import TOOLS
    entry = next(t for t in TOOLS if t["name"] == "lts_preview")
    assert "lts_preview" in LTS_ALL_TOOL_NAMES
    props = entry["inputSchema"]["properties"]
    assert set(props) >= {"content", "path", "corpus", "replaces", "virtual_path"}
    # The schema must SAY that mode is declared and that edits replace.
    assert "RESOLUTION MODE" in entry["description"]
    assert "ambiguous_intent" in entry["description"]


def test_dispatch_answers_without_a_loaded_store():
    """A draft is not corpus state: preview must answer on a server with no corpora."""
    out = dispatch("lts_preview", {"content": _read(SPEC41), "virtual_path": "d.lts"})
    assert out["success"] is True
    # (QUAL) 9 -> 10; (C8) 10 -> 13; (V12b) 13 -> 15; see SPEC41_UNACCOUNTED_LINES
    assert out["lost"]["total"] == 15
    assert out["resolution"]["mode"] == MODE_ISOLATED


def test_dispatch_returns_a_refusal_as_a_result_not_a_crash(mini_corpus):
    out = dispatch("lts_preview", {"content": FILE_A, "corpus": mini_corpus,
                                   "virtual_path": "a.lts"})
    assert out["success"] is False
    assert out["refused"] is True
    assert out["reason"] == "ambiguous_intent"
    assert out["colliding_ids"] == ["AA-ONE", "AA-TWO"]


def test_the_whole_payload_is_json_serializable(p41):
    import json
    json.loads(json.dumps(p41))


def test_verbose_untruncates_property_values():
    long = "x" * 400
    doc = f"# D\n\n## [AA-ONE] One\n- k: {long}\n"
    terse = preview(doc, virtual_path="d.lts")["became"]["nodes"][0]["properties"]["k"]
    full = preview(doc, virtual_path="d.lts", verbose=True)["became"]["nodes"][0]["properties"]["k"]
    assert len(terse) == 160 and terse.endswith("...")
    assert full == long


# ---------------------------------------------------------------------------
# V12b — the AUTHORING-LOOP half of the unminted-reference-list detection.
# `lts_lint` (V12) flags a pure reference list under a non-minting key CORPUS-WIDE;
# `lts_preview` is the compose->preview->land gate, so the SAME silent loss must be
# LOUD here or an author sees green over a vanished edge. Both directions are pinned,
# because an instrument that can only report loss is as useless as one that can only
# report success.
# ---------------------------------------------------------------------------
def test_edge_shaped_property_under_a_non_minting_key_is_reported_lost():
    doc = ("# Doc\n\n## [AA-ONE] One\n"
           "- blocks: [AA-TWO]\n"
           "- exemplifies: [AA-TWO]\n\n"
           "## [AA-TWO] Two\n- k: v\n")
    out = preview(doc, virtual_path="d.lts")
    ch = out["lost"]["edge_shaped_property_minted_nothing"]
    keys = {u["key"] for u in ch}
    assert keys == {"blocks", "exemplifies"}
    # It is a WORKLIST: each carries where it is and the nearest key that WOULD mint.
    for u in ch:
        assert u["node_id"] == "AA-ONE"
        assert u["nearest_minting_alternatives"]  # non-empty suggestion
    # And it counts toward the LOST total the author reads.
    assert out["lost"]["total"] >= 2
    assert "EDGE-SHAPED-PROPERTY-MINTED-NOTHING" in out["report"]


def test_a_minting_key_reference_is_NOT_reported_lost():
    """The negative control: `see-also` MINTS, so it must never appear in the loss
    channel — a detector that fires on a well-formed edge is one that gets muted."""
    doc = ("# Doc\n\n## [AA-ONE] One\n"
           "- see-also: [AA-TWO]\n\n"
           "## [AA-TWO] Two\n- k: v\n")
    out = preview(doc, virtual_path="d.lts")
    assert out["lost"]["edge_shaped_property_minted_nothing"] == []
    assert out["became"]["edge_count"] == 1  # see-also minted the edge


def test_content_and_path_mode_agree_on_the_new_channel(tmp_path):
    """The new channel carries no `source_file`, so the two entry modes stay identical
    — the parity `test_content_mode_needs_no_file_and_matches_path_mode` requires."""
    doc = ("# Doc\n\n## [AA-ONE] One\n- blocks: [AA-TWO]\n\n"
           "## [AA-TWO] Two\n- k: v\n")
    f = tmp_path / "d.lts"
    f.write_text(doc, encoding="utf-8")
    from_content = preview(doc, virtual_path="d.lts")["lost"][
        "edge_shaped_property_minted_nothing"]
    from_path = preview(path=str(f))["lost"]["edge_shaped_property_minted_nothing"]
    assert from_content == from_path


# ---------------------------------------------------------------------------
# The maintainer's 2026-08-17 rulings — the THREE de-duplicated edge-verb channels, proven
# on the preview surface (the lint surface is proven in test_lint.py). One shared
# classifier, so a control that holds here holds there.
# ---------------------------------------------------------------------------
def test_case_mismatch_of_a_declared_verb_goes_to_the_case_channel_not_undeclared():
    """(Ruling 2a) `Depends-On` / `depends_on` are case/separator variants of the
    declared `depends-on`: they belong in the Case-mismatch WARN, NEVER in the
    unknown-verb alert, and per the maintainer they are WARNed, not silently minted."""
    doc = ("# Doc\n\n## [AA-ONE] One\n"
           "- Depends-On: [AA-TWO]\n"
           "- depends_on: [AA-TWO]\n\n"
           "## [AA-TWO] Two\n- k: v\n")
    out = preview(doc, virtual_path="d.lts")
    lost = out["lost"]
    assert set(lost["case_mismatch_edge_verbs"]) == {"Depends-On", "depends_on"}
    assert lost["case_mismatch_canonical"] == {
        "Depends-On": "depends-on", "depends_on": "depends-on"}
    assert lost["case_mismatch_warn"] == "Case-mismatch {Depends-On, depends_on}"
    # MUST NOT appear in the undeclared alert…
    assert "Depends-On" not in lost["undeclared_edge_verbs"]
    assert "depends_on" not in lost["undeclared_edge_verbs"]
    # …and (the maintainer: do NOT silently normalize-and-mint) no DEPENDS_ON edge exists yet.
    assert all(e["type"] != "DEPENDS_ON" for e in out["became"]["edges"])
    # The WARN is loud in the rendered report.
    assert "Case-mismatch {Depends-On, depends_on}" in out["report"]


def test_genuinely_undeclared_verb_lists_once_even_used_n_times():
    """(Ruling 2b) `frobnicate` matches no declared verb by case: it is the positive
    alert, de-duplicated to ONE entry no matter how many times it is authored."""
    doc = ("# Doc\n\n## [AA-ONE] One\n"
           "- frobnicate: [AA-TWO]\n"
           "- frobnicate: [AA-THREE]\n\n"
           "## [AA-TWO] Two\n- k: v\n\n## [AA-THREE] Three\n- k: v\n")
    out = preview(doc, virtual_path="d.lts")
    lost = out["lost"]
    assert lost["undeclared_edge_verbs"].count("frobnicate") == 1
    assert "frobnicate" not in lost["case_mismatch_edge_verbs"]
    assert lost["case_mismatch_warn"] is None


def test_declared_core_and_corpus_registered_verbs_are_in_neither_channel(mini_corpus):
    """(Ruling 2c) A declared verb — core (`see-also`) — is in NEITHER channel and
    mints. `documents` under the acme profile is the corpus-registered half; it is
    covered end-to-end in test_lint.py where that profile is loaded."""
    doc = ("# Doc\n\n## [AA-ONE] One\n- see-also: [AA-TWO]\n\n"
           "## [AA-TWO] Two\n- k: v\n")
    out = preview(doc, virtual_path="d.lts")
    lost = out["lost"]
    assert lost["undeclared_edge_verbs"] == []
    assert lost["case_mismatch_edge_verbs"] == []
    assert lost["edge_shaped_property_minted_nothing"] == []
    assert out["became"]["edge_count"] == 1  # see-also minted


def test_case_mismatch_warn_prints_even_when_total_loss_is_zero():
    """A lone separator-variant parses cleanly and adds nothing to `total`; the WARN must
    still be loud, or preview would print 'LOST: nothing' over a real, fixable defect —
    the silent-success antipattern this whole design fights."""
    doc = ("# Doc\n\n## [AA-ONE] One\n- depends_on: [AA-TWO]\n\n"
           "## [AA-TWO] Two\n- k: v\n")
    out = preview(doc, virtual_path="d.lts")
    assert out["lost"]["case_mismatch_edge_verbs"] == ["depends_on"]
    assert "Case-mismatch {depends_on}" in out["report"]
