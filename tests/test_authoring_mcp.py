"""[AUTH-PATH-LAYER-1-MCP] — the authoring write surface, pinned.

⛔ THE CONTROLS EXERCISE THE SHAPES THE CORPUS ACTUALLY USES. A control that only
tries the shapes its author remembered certifies a narrower class than its name claims —
which is 03's own finding one level up (the seven-instance count was stale on arrival
because the eighth happened while writing it). So the defect corpus here is MEASURED,
not imagined: four shapes counted by the maintainer on 2026-08-12 in a single 561-line live
document (`acme/corpora/acme-specs/61-fleet-console.lts`) —

    - ⚠ premise:        - measured, 2026-08-12:      - worked example:      - forbidden — X:

— plus the five from 03 §1 `what-preview-catches-and-does-not`. All nine were verified
against the real parser before being written down here.
"""

from __future__ import annotations

import pytest

from factlattice.authoring.edit import EditRefused, add_node, set_property
from factlattice.parser import parse_text
from factlattice_engine.tools import LTS_ALL_TOOL_NAMES, LTS_AUTHORING_TOOL_NAMES, dispatch

# --------------------------------------------------------------------------------
# Fixtures. ID-LAST + DOCTYPE headings deliberately, because that is the live
# Acme shape and the shape the library's original lexical locator could not see:
# measured 2026-08-12, 260 of 1668 live nodes (15.6%) unreachable, INCLUDING every
# document root node in the corpora.
# --------------------------------------------------------------------------------
DOC = """---
domain: lib.ops
created: 2026-08-12
---
# A live-shaped document [SPEC:AUTH-FIX]

- kind: proposal-root
- created: 2026-08-12

## 1. First section [AUTH-FIX-ONE]

- kind: measurement
- instances: EIGHT
- see-also:
  - [AUTH-FIX-TWO]

Some prose that mentions a ratio: 3:1, and a uri doc://x/y.

## 2. Second section [AUTH-FIX-TWO]

- kind: finding
- verdict: holds

## Relationships

- [AUTH-FIX] DEPENDS_ON [AUTH-FIX-ONE]

**Version** 1.0
"""


@pytest.fixture()
def doc(tmp_path):
    p = tmp_path / "live.lts"
    p.write_text(DOC, encoding="utf-8")
    return p


def props(path) -> dict:
    text = path.read_text(encoding="utf-8")
    return {n.id: dict(n.properties) for n in parse_text(text, str(path)).nodes}


# --------------------------------------------------------------------------------
# 1. The regression that motivated rewriting the locator.
# --------------------------------------------------------------------------------

def test_id_last_doctyped_root_node_is_reachable(doc):
    """`# Title [SPEC:AUTH-FIX]` — an h1, ID-LAST, doctyped. The single most-edited
    node in every Acme file, and the one the lexical locator could not find."""
    r = set_property(doc, "AUTH-FIX", "status", "ratified")
    assert r.action == "added"
    assert props(doc)["AUTH-FIX"]["status"] == "ratified"


def test_id_last_section_heading_is_reachable(doc):
    """`## 1. First section [AUTH-FIX-ONE]` — numbered prefix, ID last."""
    set_property(doc, "AUTH-FIX-ONE", "detection-split", "guard 8, author 0")
    assert props(doc)["AUTH-FIX-ONE"]["detection-split"] == "guard 8, author 0"


# --------------------------------------------------------------------------------
# 2. Accommodate then refuse — on the MEASURED live shapes.
# --------------------------------------------------------------------------------

@pytest.mark.parametrize("authored,expected", [
    ("⚠ premise", "premise"),                       # live, 61-fleet-console
    ("worked example", "worked-example"),           # live, 61-fleet-console
    ("what-DOES-transfer", "what-does-transfer"),   # 03 §1, instance 1
    ("⛔ known-consequence", "known-consequence"),
    ("**bold-key**", "bold-key"),
    ("Title Case Key", "title-case-key"),
    ("  padded-key  ", "padded-key"),
])
def test_accommodates_the_unambiguous(doc, authored, expected):
    r = set_property(doc, "AUTH-FIX-ONE", authored, "a value")
    assert r.key == expected
    assert r.key_as_authored == authored
    assert r.coercions, "a coercion must be RECORDED, not applied silently"
    assert props(doc)["AUTH-FIX-ONE"][expected] == "a value"


@pytest.mark.parametrize("authored,must_mention", [
    ("2026-count", "DIGIT"),                        # 03: the one refusal that needs a choice
    ("measured, 2026-08-12", "illegal"),            # live, 61-fleet-console
    ("forbidden — X", "illegal"),                   # live, 61-fleet-console
    ("⚠⛔", "alphanumeric"),
])
def test_refuses_where_correcting_needs_a_choice(doc, authored, must_mention):
    before = doc.read_text(encoding="utf-8")
    with pytest.raises(EditRefused) as exc:
        set_property(doc, "AUTH-FIX-ONE", authored, "v")
    assert must_mention in str(exc.value)
    assert doc.read_text(encoding="utf-8") == before, "a refusal must write NOTHING"


def test_never_modifies_anything_past_the_first_colon(doc):
    """Values legitimately hold mixed-case identifiers, sha digests and pictographs."""
    value = "⛔ ExampleStatusNotMet FIXED_12 sha256:9f8Ab3 — 98.40%, not 91.0%"
    r = set_property(doc, "AUTH-FIX-TWO", "⚠ Finding", value)
    assert r.key == "finding"
    assert r.parsed_value == value, "the value must survive the round trip byte for byte"
    assert props(doc)["AUTH-FIX-TWO"]["finding"] == value


# --------------------------------------------------------------------------------
# 3. THE GATE. The displacement case is the whole reason the gate is whole-file.
# --------------------------------------------------------------------------------

def test_gate_catches_displacement_into_a_neighbouring_node(doc, monkeypatch):
    """⛔ THE TEST THE PROPOSAL ASKS FOR BY NAME.

    [AUTH-PATH-LAYER-1-MCP] `the-gate`: "A narrower 'did my key parse' check sails past
    an insertion that landed in a neighbouring node's block and displaced something."

    Simulated by corrupting the insertion-point chooser so the new line lands inside
    [AUTH-FIX-TWO]'s block, ON TOP of an existing property, while the caller still asks
    to amend [AUTH-FIX-ONE]. The narrow check ("did `zzz` parse?") would PASS — the key
    is present in the file and in the graph. The whole-file per-node check must refuse,
    and must name the collateral.
    """
    import factlattice.authoring.edit as edit_mod

    lines_seen = {}

    def _bad_anchor(lines, start0, end0):
        # point at the LAST property of the *other* node — an off-by-a-block bug
        for i, ln in enumerate(lines):
            if ln.startswith("- verdict:"):
                lines_seen["target"] = i
                return i          # insert AFTER `- verdict:` -> inside AUTH-FIX-TWO
        raise AssertionError("fixture drift: `- verdict:` not found")

    monkeypatch.setattr(edit_mod, "_property_block_end", _bad_anchor)
    before = doc.read_text(encoding="utf-8")

    with pytest.raises(EditRefused) as exc:
        set_property(doc, "AUTH-FIX-ONE", "zzz-new", "landed in the wrong block")

    msg = str(exc.value)
    assert "more than the intended change" in msg
    assert "AUTH-FIX-TWO" in msg, "the receipt must NAME the node that moved"
    assert "UNEXPLAINED" in msg
    assert doc.read_text(encoding="utf-8") == before, "a refusal must write NOTHING"
    # and the narrow check would indeed have passed — prove it, so the test is a
    # discrimination proof and not merely a red light.
    assert exc.value.evidence["actual_delta"]["AUTH-FIX-TWO"]["added"] == ["zzz-new"]
    assert exc.value.evidence["intended_delta"]["AUTH-FIX-ONE"]["added"] == ["zzz-new"]


def test_gate_catches_collateral_while_the_narrow_check_would_pass(doc, monkeypatch):
    """⛔ THE CASE THAT DISCRIMINATES, and the reason the gate is WHOLE-FILE.

    The previous test lands the key on the WRONG node — which the library's original
    narrower check (`fixed in parsed[node_id]`) would also have caught. Proving the
    whole-file gate is stronger requires the case where the narrow check PASSES:

        the key lands CORRECTLY on the target node, and something is destroyed
        SOMEWHERE ELSE in the file at the same time.

    That is what a bad splice actually looks like — a line-index arithmetic error that
    inserts in the right place and drops a line in the wrong one. Both assertions below
    matter: the narrow check is shown GREEN, and the real gate is shown RED.
    """
    import factlattice.authoring.edit as edit_mod

    def _correct_insert_but_clobbers_elsewhere(lines, start0, end0):
        for i, ln in enumerate(lines):
            if ln.startswith("- verdict:"):     # a property on the OTHER node
                del lines[i]                     # collateral, far from the insertion
                break
        else:
            raise AssertionError("fixture drift: `- verdict:` not found")
        return edit_mod._property_block_end.__wrapped__(lines, start0, end0)

    real = edit_mod._property_block_end
    _correct_insert_but_clobbers_elsewhere.__wrapped__ = real
    monkeypatch.setattr(edit_mod, "_property_block_end", _correct_insert_but_clobbers_elsewhere)
    before = doc.read_text(encoding="utf-8")

    with pytest.raises(EditRefused) as exc:
        set_property(doc, "AUTH-FIX-ONE", "zzz-new", "v")

    ev = exc.value.evidence
    # THE NARROW CHECK WOULD HAVE PASSED: the key landed on exactly the node asked for.
    assert ev["actual_delta"]["AUTH-FIX-ONE"]["added"] == ["zzz-new"]
    # THE WHOLE-FILE CHECK REFUSES: a property died on a node nobody asked about.
    assert ev["actual_delta"]["AUTH-FIX-TWO"]["removed"] == ["verdict"]
    assert "AUTH-FIX-TWO" in ev["unexplained"]
    assert doc.read_text(encoding="utf-8") == before, "a refusal must write NOTHING"


def test_gate_refuses_when_the_node_set_moves(doc, monkeypatch):
    import factlattice.authoring.edit as edit_mod

    def _eat_a_heading(lines, start0, end0):
        for i, ln in enumerate(lines):
            if ln.startswith("## 2."):
                lines[i] = "- zzz-new: ate a heading"
                return i - 1
        raise AssertionError("fixture drift")

    monkeypatch.setattr(edit_mod, "_property_block_end", _eat_a_heading)
    with pytest.raises(EditRefused) as exc:
        set_property(doc, "AUTH-FIX-ONE", "zzz-new", "v")
    assert "NODE SET" in str(exc.value)
    assert exc.value.evidence["nodes_lost"] == ["AUTH-FIX-TWO"]


# --------------------------------------------------------------------------------
# 4. Placement hazards that the gate would catch but that we would rather not cause.
# --------------------------------------------------------------------------------

def test_insertion_does_not_truncate_a_nested_block(doc):
    """[AUTH-FIX-ONE] ends with a nested `see-also:` block. A top-level line inserted
    into the middle of it silently truncates the list."""
    r = set_property(doc, "AUTH-FIX-ONE", "added-after-nested", "v")
    p = props(doc)["AUTH-FIX-ONE"]
    assert p["added-after-nested"] == "v"
    assert p["see-also"] == ["[AUTH-FIX-TWO]"], "the nested block must survive intact"
    assert r.delta == {"AUTH-FIX-ONE": {"added": ["added-after-nested"], "removed": []}}


def test_replace_is_in_place_adds_no_key_and_is_VISIBLE_in_the_delta(doc):
    # [TYPED-WRITER-APPEND] 2026-10-03: this test used to assert `delta == {}`, i.e. it PINNED
    # the defect (a value replace reporting as "no change"). No KEY moves — still true — but
    # the VALUE did, and the receipt now says so.
    r = set_property(doc, "AUTH-FIX-TWO", "verdict", "revised")
    assert r.action == "replaced"
    assert r.delta == {"AUTH-FIX-TWO": {"added": [], "removed": [], "changed": {
        "verdict": {"removed": ["holds"], "added": ["revised"]}}}}
    assert props(doc)["AUTH-FIX-TWO"]["verdict"] == "revised"


def test_allow_replace_false_refuses(doc):
    with pytest.raises(EditRefused, match="allow_replace"):
        set_property(doc, "AUTH-FIX-TWO", "verdict", "x", allow_replace=False)


# --------------------------------------------------------------------------------
# 3b. THE MULTI-VALUED / ACCUMULATED-KEY REPLACE HOLE.
#     A repeated key accumulates into a LIST (parser `_maybe_multivalue`); both
#     values are live. `set_property`'s replace path replaces only the FIRST physical
#     line matching the key, silently overwriting one element and keeping the rest —
#     and the key-set-only gate cannot see it, because the KEY COUNT is unchanged.
#     Measured 2026-09-26 on TCL-COMPARISON-API (`provenance` x2), the maintainer-directed.
# --------------------------------------------------------------------------------

MULTI = """# Root [TCL-MV]

- kind: api
- provenance: matched measured property (digest re-run 2026-09-01)
- provenance: re-tagged declared field after audit (2026-09-20)
- verdict: holds

## Neighbour [TCL-MV-N]

- kind: note
- provenance: neighbour A
- provenance: neighbour B
"""


@pytest.fixture()
def multidoc(tmp_path):
    p = tmp_path / "mv.lts"
    p.write_text(MULTI, encoding="utf-8")
    return p


def test_replace_on_accumulated_key_refuses_instead_of_silently_losing(multidoc):
    """⛔ THE DEFECT. Against the PRE-change engine this REPLACE 'succeeds' and destroys
    the first of two live `provenance` values, while the gate reports an empty delta and
    the receipt carries zero warnings — a silent data loss on the sanctioned write path.

    Fixed behaviour: a single-line replace of a multi-valued (accumulated) key is
    AMBIGUOUS and must be REFUSED, naming the count and the safe alternatives. Nothing
    is written; both original values survive on disk untouched."""
    before_text = multidoc.read_text(encoding="utf-8")
    before_val = props(multidoc)["TCL-MV"]["provenance"]
    assert isinstance(before_val, list) and len(before_val) == 2, "fixture must accumulate"

    with pytest.raises(EditRefused) as exc:
        set_property(multidoc, "TCL-MV", "provenance", "a new single value")

    msg = str(exc.value)
    assert "accumulated" in msg or "multi-valued" in msg, "the refusal must name the ambiguity"
    assert "provenance" in msg
    assert "2" in msg, "the refusal must state HOW MANY values are live"
    # Both alternatives the operator is told to use:
    assert "lts_repair_property" in msg
    # ⛔ NOTHING WRITTEN — both original values survive on disk.
    assert multidoc.read_text(encoding="utf-8") == before_text
    assert props(multidoc)["TCL-MV"]["provenance"] == before_val
    # the refusal carries the evidence, per the receipt discipline.
    assert exc.value.evidence.get("value_count") == 2


def test_replace_on_nested_block_list_refuses(doc):
    """A nested-block value (`see-also:` with indented items) ALSO parses to a list, and
    a single-line replace would orphan the indented items. Same ambiguity, same refusal."""
    before_text = doc.read_text(encoding="utf-8")
    with pytest.raises(EditRefused) as exc:
        set_property(doc, "AUTH-FIX-ONE", "see-also", "collapsed to one")
    assert "accumulated" in str(exc.value) or "multi-valued" in str(exc.value)
    assert doc.read_text(encoding="utf-8") == before_text


def test_gate_catches_a_lost_list_element_on_a_neighbouring_node(multidoc, monkeypatch):
    """⛔ THE GATE BACKSTOP — the more important half. Even if the behaviour refusal were
    removed, the value-aware whole-file gate must catch a lost LIST ELEMENT.

    Simulated as collateral: the caller amends one key, but a splice bug also drops one
    of the two `provenance` lines on the SAME node. The pre-change key-set-only gate
    would PASS this (the `provenance` KEY still exists — its count is unchanged), which
    is exactly the silent hole. The value-aware gate must REFUSE and name the change.
    """
    import factlattice.authoring.edit as edit_mod

    # The caller amends TCL-MV (the FIRST node); the splice bug drops one accumulated
    # `provenance` line on the NEIGHBOUR node (TCL-MV-N), which sits AFTER the target's
    # body — so the target's own property-block walk is unaffected (no index drift), and
    # the only anomaly is a lost list ELEMENT on a node nobody asked about.
    def _correct_insert_but_drops_a_neighbour_element(lines, start0, end0):
        for i, ln in enumerate(lines):
            if ln.startswith("- provenance: neighbour A"):
                del lines[i]                       # collateral: drop ONE element on TCL-MV-N
                break
        else:
            raise AssertionError("fixture drift: neighbour provenance line not found")
        return edit_mod._property_block_end.__wrapped__(lines, start0, end0)

    real = edit_mod._property_block_end
    _correct_insert_but_drops_a_neighbour_element.__wrapped__ = real
    monkeypatch.setattr(edit_mod, "_property_block_end", _correct_insert_but_drops_a_neighbour_element)
    before_text = multidoc.read_text(encoding="utf-8")

    with pytest.raises(EditRefused) as exc:
        set_property(multidoc, "TCL-MV", "zzz-new", "v")

    msg = str(exc.value)
    # THE KEY-SET CHECK WOULD HAVE PASSED: `provenance` still present on TCL-MV-N, count
    # unchanged (2 lines -> a 1-element list is still the SAME KEY). Only the VALUE moved.
    assert "provenance" in msg, "the gate must name the key whose VALUE moved"
    assert "value" in msg.lower(), "the refusal must be about a VALUE change, not a key count"
    assert "TCL-MV-N" in msg, "the gate must name the NEIGHBOUR node that moved"
    assert multidoc.read_text(encoding="utf-8") == before_text, "a refusal writes NOTHING"


def test_normal_scalar_replace_still_works(multidoc):
    """⛔ DO NOT WEAKEN THE SINGLE-VALUE REPLACE. A scalar key replaces cleanly, and the
    receipt shows the VALUE move (no KEY moved; [TYPED-WRITER-APPEND] made it visible)."""
    r = set_property(multidoc, "TCL-MV", "verdict", "revised")
    assert r.action == "replaced"
    assert set(r.delta["TCL-MV"]["changed"]) == {"verdict"}
    assert r.delta["TCL-MV"]["added"] == [] and r.delta["TCL-MV"]["removed"] == []
    assert props(multidoc)["TCL-MV"]["verdict"] == "revised"
    # and the untouched accumulated key is preserved intact.
    assert len(props(multidoc)["TCL-MV"]["provenance"]) == 2


def test_empty_value_is_refused_because_it_is_a_block_head(doc):
    with pytest.raises(EditRefused, match="NESTED-BLOCK"):
        set_property(doc, "AUTH-FIX-TWO", "empty", "   ")


def test_missing_node_names_the_nodes_that_exist(doc):
    with pytest.raises(EditRefused) as exc:
        set_property(doc, "NOPE", "k", "v")
    assert "AUTH-FIX-ONE" in str(exc.value), "a refusal must be actionable"


# --------------------------------------------------------------------------------
# 5. add_node
# --------------------------------------------------------------------------------

def test_add_node_lands_inside_the_section_subtree(doc):
    r = add_node(doc, "AUTH-FIX-ONE-A", "A sub-finding",
                 {"kind": "finding", "⚠ measured": "yes"}, section="AUTH-FIX-ONE")
    assert r.heading_level == 3
    assert r.properties == {"kind": "finding", "measured": "yes"}
    assert r.coercions == {"measured": ["stripped leading symbol/pictograph from the key region"]}
    text = doc.read_text(encoding="utf-8")
    assert text.index("[AUTH-FIX-ONE-A]") < text.index("## 2. Second section")


def test_add_node_never_lands_after_relationships(doc):
    """An h1-rooted section would otherwise swallow the whole document."""
    add_node(doc, "AUTH-FIX-TAIL", "Tail", {"kind": "note"}, section="AUTH-FIX")
    text = doc.read_text(encoding="utf-8")
    assert text.index("[AUTH-FIX-TAIL]") < text.index("## Relationships")


def test_add_node_refuses_a_non_id_shaped_id(doc):
    before = doc.read_text(encoding="utf-8")
    with pytest.raises(EditRefused, match="id-shaped"):
        add_node(doc, "lower-case", "T", {"kind": "x"})
    assert doc.read_text(encoding="utf-8") == before


def test_add_node_refuses_a_duplicate_id(doc):
    with pytest.raises(EditRefused, match="already exists"):
        add_node(doc, "AUTH-FIX-TWO", "T", {"kind": "x"})


def test_add_node_reports_every_bad_key_at_once(doc):
    with pytest.raises(EditRefused) as exc:
        add_node(doc, "AUTH-FIX-NEW", "T",
                 {"2026-count": "a", "forbidden — X": "b", "fine": "c"})
    msg = str(exc.value)
    assert "2026-count" in msg and "forbidden" in msg
    assert msg.count("\n  - ") == 2, "both bad keys named in ONE refusal, not one per retry"


def test_add_node_refuses_keys_that_collide_after_coercion(doc):
    with pytest.raises(EditRefused, match="SAME key"):
        add_node(doc, "AUTH-FIX-NEW", "T", {"My Key": "a", "my-key": "b"})


def test_add_node_gate_catches_splitting_an_existing_property_block(doc, monkeypatch):
    """A heading inserted into the MIDDLE of another node's property block ends that
    node early and steals the rest. The properties are still parsed — onto the wrong
    node — which is a SUBSTITUTION, and a substitution is worse than a loss because it
    looks well-formed. Named for what it actually catches, not for what would be
    impressive."""
    import factlattice.authoring.edit as edit_mod
    monkeypatch.setattr(edit_mod, "_subtree_end", lambda *a, **k: 6)   # inside [AUTH-FIX]
    before = doc.read_text(encoding="utf-8")

    with pytest.raises(EditRefused) as exc:
        add_node(doc, "AUTH-FIX-INNER", "Inner", {"kind": "x"}, section="AUTH-FIX-TWO")

    ev = exc.value.evidence
    assert ev["actual_delta"]["AUTH-FIX"]["removed"] == ["created", "kind"], \
        "the root node was truncated by the inserted heading"
    assert doc.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------------
# 6. The MCP surface — receipts, refusals-as-results, arbitrary paths.
# --------------------------------------------------------------------------------

def test_tools_are_registered():
    for n in ("lts_add_property", "lts_add_node", "lts_property_loss"):
        assert n in LTS_ALL_TOOL_NAMES
        assert n in LTS_AUTHORING_TOOL_NAMES
    from factlattice_engine.server import TOOLS
    names = {t["name"] for t in TOOLS}
    assert LTS_AUTHORING_TOOL_NAMES[0] in names and LTS_AUTHORING_TOOL_NAMES[1] in names


def test_dispatch_needs_no_loaded_store_and_accepts_an_arbitrary_path(doc):
    """`must-accept-arbitrary-paths` is normative: the files that most need this tool
    live OUTSIDE any corpus root, and `tools=None` here proves no store is consulted."""
    out = dispatch("lts_add_property", {
        "path": str(doc), "node_id": "AUTH-FIX", "key": "⛔ Disposition",
        "value": "ratified"}, tools=None)
    assert out["success"] is True and out["written"] is True


def test_receipt_contains_its_own_evidence(doc):
    """Principles III.7 — the report must CONTAIN the evidence, not assert it. If
    accepting the output requires running another command, the tool is under-built.

    [AUTH-RECEIPT-COMPACT] `receipt='full'` explicitly — this test pins the FULL shape
    (report prose, properties_before/after, context). The default `compact` shape's own
    proof-of-write invariants are pinned separately in test_authoring_receipt_compact.py."""
    out = dispatch("lts_add_property", {
        "path": str(doc), "node_id": "AUTH-FIX-ONE",
        "key": "⚠ premise", "value": "the guard caught 8 of 8 — sha256:9fAb",
        "receipt": "full"}, tools=None)

    assert out["path"].startswith("/"), "the path in a receipt must be ABSOLUTE"
    assert out["key"] == "premise" and out["key_as_authored"] == "⚠ premise"
    assert out["coercions"], "a silent coercion is the failure this tool exists to end"
    # the VALUE AS PARSED is the proof the field reached the graph, not just the file
    assert out["value_as_parsed"] == "the guard caught 8 of 8 — sha256:9fAb"
    assert out["delta"] == {"AUTH-FIX-ONE": {"added": ["premise"], "removed": []}}
    assert out["nodes_checked"] == 3
    assert "premise" in out["properties_after"] and "premise" not in out["properties_before"]
    assert out["line_number"] > 0 and out["line_text"] == "- premise: the guard caught 8 of 8 — sha256:9fAb"
    assert any(str(out["line_number"]) in c for c in out["context"])
    assert out["backup_path"].endswith(".bak")
    for token in ("WROTE", "GATE", "IN SITU", "nodes checked"):
        assert token in out["report"]


def test_refusal_is_a_result_not_an_exception(doc):
    before = doc.read_text(encoding="utf-8")
    out = dispatch("lts_add_property", {
        "path": str(doc), "node_id": "AUTH-FIX", "key": "2026-count", "value": "8"},
        tools=None)
    assert out["success"] is False and out["refused"] is True
    assert out["written"] is False and out["file_unchanged"] is True
    assert "DIGIT" in out["reason"]
    assert "REFUSED" in out["report"]
    assert doc.read_text(encoding="utf-8") == before


def test_dispatch_add_node_receipt(doc):
    out = dispatch("lts_add_node", {
        "path": str(doc), "node_id": "AUTH-FIX-THREE", "title": "Third",
        "section": "AUTH-FIX", "properties": {"kind": "finding", "Measured": "yes"}},
        tools=None)
    assert out["success"] is True
    assert out["properties_as_parsed"] == {"kind": "finding", "measured": "yes"}
    assert out["delta"] == {"AUTH-FIX-THREE": {"added": ["kind", "measured"], "removed": []}}
    assert out["coercions"] == {"measured": ["lowercased (emphasis is not machine-readable)"]}


def test_missing_argument_is_a_result(doc):
    out = dispatch("lts_add_property", {"path": str(doc)}, tools=None)
    assert out["success"] is False and "missing required argument" in out["error"]


# --------------------------------------------------------------------------------
# 7. [AUTH-PATH-FALSIFIER] — the metric, and its measured recall gap.
# --------------------------------------------------------------------------------

LOSSY = """---
domain: lib.ops
---
# T [SPEC:T]

- kind: root

## 1. S [T-A]

- ⚠ premise: emphasis before the key
- measured, 2026-08-12: a comma in the key region
- worked example: two words
- forbidden — X: an em dash
- Capitalised: case defect
- **bold-wrap: v**
- nospace:after colon
- 3-digit-initial: digit
- good-key: this one is fine
- see the docs at doc://x/y for the ratio 3:1

## Relationships

- [T] DEPENDS_ON [T-A]
"""


def test_falsifier_counts_the_shapes_the_ratified_lint_misses(tmp_path):
    from factlattice_engine.property_loss import scan_text
    findings = scan_text(LOSSY, "lossy.lts")
    keys = {f.key_region for f in findings}

    # the four the maintainer measured live, 2026-08-12, in 61-fleet-console.lts
    for live in ("⚠ premise", "measured, 2026-08-12", "worked example", "forbidden — X"):
        assert live in keys, f"the falsifier must count the LIVE shape {live!r}"
    # plus the classes 03 §1 measured as invisible to every instrument
    assert "nospace" in keys and "3-digit-initial" in keys

    # and it must NOT count what legitimately parses, nor prose, nor edge triples
    assert "good-key" not in keys
    assert not any("DEPENDS_ON" in f.text for f in findings), \
        "a property-shaped check in ## Relationships fires on every WELL-FORMED edge"
    assert not any("doc://" in f.text for f in findings), "the URI guard must hold"


def test_falsifier_reports_the_shipped_lint_s_recall_gap(tmp_path):
    """The second number to watch. A headline that falls while recall stays low means
    the habit moved sideways rather than stopped."""
    (tmp_path / "lossy.lts").write_text(LOSSY, encoding="utf-8")
    from factlattice_engine.property_loss import scan_corpus
    rep = scan_corpus([tmp_path])
    t = rep["totals"]
    assert t["headline_property_lines_lost"] >= 8
    assert t["missed_by_ratified_lint"] > 0, (
        "if this ever reaches 0 the ratified lint has caught up and the wider predicate "
        "can be retired — until then, reporting only the lint's number under-counts")
    assert 0.0 < t["ratified_lint_recall"] < 1.0
    assert "how_to_falsify" in rep


def test_falsifier_tiers_are_stable_and_documented(tmp_path):
    from factlattice_engine.property_loss import scan_text, TIER_A, TIER_B
    by_key = {f.key_region: f for f in scan_text(LOSSY, "l.lts")}
    assert by_key["⚠ premise"].tier == TIER_A            # coerces cleanly -> `premise`
    assert by_key["worked example"].tier == TIER_A       # spaces -> hyphens
    assert by_key["3-digit-initial"].tier == TIER_B      # needs an INVENTED prefix
    assert by_key["3-digit-initial"].reason == "digit_initial"
    assert by_key["measured, 2026-08-12"].tier == TIER_B
    assert by_key["⚠ premise"].normalised_key == "premise"
    # ⛔ tier B's `normalised_key` is a GUESS for corroboration only. It must NEVER be
    # what gets written — guessing a key is a SUBSTITUTION, and the standing ruling
    # (coverage.py SL-21/22) is that a substitution is worse than a loss.
    assert by_key["measured, 2026-08-12"].normalised_key == "measured"


OUTSIDE_BODY = """---
domain: lib.ops
---
# T [SPEC:T2]

- kind: root

## A section with NO id at all

- source-file: `ux_composer/src/composer/app.py`
- another-perfectly-legal-key: and a value
"""


def test_falsifier_excludes_outside_node_body_from_the_headline(tmp_path):
    """⛔ THE EXCLUSION THAT MAKES THE METRIC ABLE TO MOVE.

    `- source-file: …` under an ID-less heading is a PERFECTLY LEGAL property that
    produced nothing, because `_parse_properties` runs only over an [ID]-bearing
    heading's body. It is real loss and a DIFFERENT mechanism: `lts_add_property`
    cannot fix it (there is no node to address). Folding it into the headline would
    guarantee a flat trend and a false verdict of 'the diagnosis was wrong'.
    """
    from factlattice_engine.property_loss import scan_text, TIER_C, HEADLINE_TIERS
    findings = {f.key_region: f for f in scan_text(OUTSIDE_BODY, "o.lts")}
    assert findings["source-file"].tier == TIER_C
    assert findings["source-file"].tier not in HEADLINE_TIERS
    assert "never ran" in findings["source-file"].reason

    (tmp_path / "o.lts").write_text(OUTSIDE_BODY, encoding="utf-8")
    from factlattice_engine.property_loss import scan_corpus
    t = scan_corpus([tmp_path])["totals"]
    assert t["headline_property_lines_lost"] == 0
    assert t["excluded_outside_node_body"] == 2, "excluded, but COUNTED and reported"


def test_corroboration_needs_a_corpus_wide_vocabulary(tmp_path):
    """⛔ A PER-FILE VOCABULARY MEASURED ZERO — 0 corroborated out of 827 across 138
    live files, because a key lost on its only use in a file has no surviving sibling
    THERE. The signal exists only corpus-wide. Pinned so the scope cannot silently
    narrow again."""
    from factlattice_engine.property_loss import scan_corpus, scan_text, corroborate

    lossy = "---\ndomain: d\n---\n# A [SPEC:A]\n\n- kind: root\n- ⚠ premise: lost here\n"
    healthy = "---\ndomain: d\n---\n# B [SPEC:B]\n\n- premise: parsed fine over here\n"
    (tmp_path / "a.lts").write_text(lossy, encoding="utf-8")

    # alone: the key is nowhere corroborated
    assert scan_corpus([tmp_path])["totals"]["corroborated"] == 0
    # with a sibling file that proves `premise` is a real field: corroborated
    (tmp_path / "b.lts").write_text(healthy, encoding="utf-8")
    t = scan_corpus([tmp_path])["totals"]
    assert t["headline_property_lines_lost"] == 1 and t["corroborated"] == 1
    assert t["corroborated_share"] == 1.0

    # and the helper is scope-explicit rather than nesting-dependent
    assert corroborate(scan_text(lossy, "a.lts"), set())[0].key_parsed_elsewhere is False
    assert corroborate(scan_text(lossy, "a.lts"), {"premise"})[0].key_parsed_elsewhere is True


def test_falsifier_dispatches(tmp_path):
    (tmp_path / "lossy.lts").write_text(LOSSY, encoding="utf-8")
    out = dispatch("lts_property_loss", {"roots": [str(tmp_path)]}, tools=None)
    assert out["success"] is True
    assert out["totals"]["headline_property_lines_lost"] >= 8
    assert out["examples"], "the report must CONTAIN worked examples, not promise them"
