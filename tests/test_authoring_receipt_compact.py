"""[AUTH-RECEIPT-COMPACT] — compact receipts for the write surface.

The maintainer ruling: `lts_add_property` / `lts_add_node` /
`lts_repair_property` receipts cost ~5K tokens each, dominated by
`root_membership.roots_checked` (every corpus's roots, echoed on every write) and the
`report` field's restatement of what `context`/structured fields already carry. An
Intelligence writing 10 properties burns ~50K tokens on receipts alone.

`receipt="compact"` (the new DEFAULT) keeps everything that PROVES the write landed and
drops the rest. `receipt="full"` is the old shape, unchanged byte-for-byte, for the rare
debugging case. A REFUSAL is NEVER compacted — it always carries full reason + evidence,
regardless of the `receipt` argument, because a refusal that looks compact-and-clean is
indistinguishable from a refusal that looks like success once fields start disappearing.

⛔ This module tests the MCP ADAPTER layer (`factlattice_engine/authoring.py` +
`.tools.dispatch`) only. `factlattice/authoring/edit.py` — the gate itself — is
UNTOUCHED: compaction is a RESPONSE-SHAPE change, never a change to what is validated or
written. `tests/test_authoring_mcp.py` pins that the gate logic is unchanged; this file
pins that the receipt shape changed correctly on top of it.
"""

from __future__ import annotations

import pytest

from factlattice.parser import parse_text
from factlattice_engine.tools import dispatch

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
# 1. DEFAULT IS COMPACT, and compact still proves the write.
# --------------------------------------------------------------------------------

def test_add_property_defaults_to_compact(doc):
    out = dispatch("lts_add_property", {
        "path": str(doc), "node_id": "AUTH-FIX-ONE",
        "key": "premise", "value": "the guard caught 8 of 8"}, tools=None)
    assert out["success"] is True
    assert out["receipt"] == "compact"
    # bulky fields DROPPED
    for k in ("report", "properties_before", "properties_after", "context"):
        assert k not in out, f"{k!r} must be dropped in compact mode"
    assert "roots_checked" not in out["root_membership"]
    assert "roots_source" not in out["root_membership"]
    # proof of the write SURVIVES
    assert out["path"].startswith("/")
    assert out["node_id"] == "AUTH-FIX-ONE"
    assert out["key"] == "premise"
    assert out["value_as_parsed"] == "the guard caught 8 of 8"
    assert out["line_number"] > 0
    assert out["line_text"] == "- premise: the guard caught 8 of 8"
    assert out["delta"] == {"AUTH-FIX-ONE": {"added": ["premise"], "removed": []}}
    assert out["nodes_checked"] == 3
    assert out["nodes_changed"] == 1
    assert out["backup_path"].endswith(".bak")
    assert out["edges_minted"] == []
    assert out["root_membership"]["inside_corpus"] is False
    assert isinstance(out["root_membership"]["corpora"], list)
    # and the write REALLY landed, independent of what the receipt says
    assert props(doc)["AUTH-FIX-ONE"]["premise"] == "the guard caught 8 of 8"


def test_add_property_receipt_full_is_the_old_shape(doc):
    out = dispatch("lts_add_property", {
        "path": str(doc), "node_id": "AUTH-FIX-ONE",
        "key": "premise", "value": "the guard caught 8 of 8", "receipt": "full"},
        tools=None)
    assert out["receipt"] == "full"
    for token in ("WROTE", "GATE", "IN SITU", "nodes checked"):
        assert token in out["report"]
    assert "premise" in out["properties_after"]
    assert "premise" not in out["properties_before"]
    assert "roots_checked" in out["root_membership"]
    assert any(str(out["line_number"]) in c for c in out["context"])


def test_compact_is_smaller_than_full(doc):
    import json
    compact = dispatch("lts_add_property", {
        "path": str(doc), "node_id": "AUTH-FIX-ONE", "key": "k1", "value": "v1"},
        tools=None)
    full = dispatch("lts_add_property", {
        "path": str(doc), "node_id": "AUTH-FIX-ONE", "key": "k2", "value": "v2",
        "receipt": "full"}, tools=None)
    compact_len = len(json.dumps(compact))
    full_len = len(json.dumps(full))
    assert compact_len < full_len * 0.5, (compact_len, full_len)


# --------------------------------------------------------------------------------
# 2. VALUE TRUNCATION — the value proves the write but must not itself re-inflate
#    the receipt back to full size.
# --------------------------------------------------------------------------------

def test_compact_truncates_a_long_value_and_states_the_full_length(doc):
    long_value = "x" * 500
    out = dispatch("lts_add_property", {
        "path": str(doc), "node_id": "AUTH-FIX-TWO", "key": "bignote",
        "value": long_value}, tools=None)
    assert len(out["value_as_parsed"]) < 500
    assert "truncated" in out["value_as_parsed"]
    assert "500" in out["value_as_parsed"], "the full length must be stated"
    # the FILE and the graph still hold the value untruncated
    assert props(doc)["AUTH-FIX-TWO"]["bignote"] == long_value


def test_full_receipt_never_truncates(doc):
    long_value = "x" * 500
    out = dispatch("lts_add_property", {
        "path": str(doc), "node_id": "AUTH-FIX-TWO", "key": "bignote2",
        "value": long_value, "receipt": "full"}, tools=None)
    assert out["value_as_parsed"] == long_value


# --------------------------------------------------------------------------------
# 3. A REFUSAL IS NEVER COMPACTED.
# --------------------------------------------------------------------------------

def test_refusal_is_never_compacted_even_when_compact_is_requested(doc):
    before = doc.read_text(encoding="utf-8")
    out = dispatch("lts_add_property", {
        "path": str(doc), "node_id": "AUTH-FIX", "key": "2026-count", "value": "8",
        "receipt": "compact"}, tools=None)
    assert out["success"] is False and out["refused"] is True
    assert "DIGIT" in out["reason"]
    assert "REFUSED" in out["report"], "a refusal keeps its FULL report regardless of `receipt`"
    assert "evidence" in out, "a refusal keeps its full evidence CHANNEL regardless of `receipt`"
    assert doc.read_text(encoding="utf-8") == before


MULTI = """# Root [TCL-MV]

- kind: api
- provenance: matched measured property (digest re-run 2026-09-01)
- provenance: re-tagged declared field after audit (2026-09-20)
"""


def test_refusal_evidence_survives_compact_on_an_evidence_bearing_refusal(tmp_path):
    """The accumulated-key refusal carries non-empty `evidence` (value_count etc.) — the
    stronger proof that `receipt='compact'` never trims a refusal's evidence channel."""
    p = tmp_path / "mv.lts"
    p.write_text(MULTI, encoding="utf-8")
    out = dispatch("lts_add_property", {
        "path": str(p), "node_id": "TCL-MV", "key": "provenance",
        "value": "a new single value", "receipt": "compact"}, tools=None)
    assert out["success"] is False and out["refused"] is True
    assert out["evidence"].get("value_count") == 2
    assert "REFUSED" in out["report"]


# --------------------------------------------------------------------------------
# 4. add_node — same discipline.
# --------------------------------------------------------------------------------

def test_add_node_defaults_to_compact(doc):
    out = dispatch("lts_add_node", {
        "path": str(doc), "node_id": "AUTH-FIX-THREE", "title": "Third",
        "section": "AUTH-FIX", "properties": {"kind": "finding", "Measured": "yes"}},
        tools=None)
    assert out["success"] is True
    assert out["receipt"] == "compact"
    for k in ("report", "context"):
        assert k not in out
    assert "roots_checked" not in out["root_membership"]
    assert out["properties_as_parsed"] == {"kind": "finding", "measured": "yes"}
    assert out["delta"] == {"AUTH-FIX-THREE": {"added": ["kind", "measured"], "removed": []}}
    assert out["nodes_changed"] == 1
    assert out["coercions"] == {"measured": ["lowercased (emphasis is not machine-readable)"]}
    assert out["line_number"] > 0
    assert "AUTH-FIX-THREE" in out["line_text"]


def test_add_node_full_still_has_context_and_report(doc):
    out = dispatch("lts_add_node", {
        "path": str(doc), "node_id": "AUTH-FIX-THREE", "title": "Third",
        "section": "AUTH-FIX", "properties": {"kind": "finding"}, "receipt": "full"},
        tools=None)
    assert out["receipt"] == "full"
    assert out["context"]
    assert "WROTE" in out["report"]


# --------------------------------------------------------------------------------
# 5. repair_property — BOTH lines (before/after) must survive compaction; that is
#    the entire load-bearing proof of a repair.
# --------------------------------------------------------------------------------

REPAIR_DOC = """# Root [REP-A]

- kind: root
- ⛔ FLAGGED 2026-08-10: something broke
"""


@pytest.fixture()
def repairdoc(tmp_path):
    p = tmp_path / "repair.lts"
    p.write_text(REPAIR_DOC, encoding="utf-8")
    return p


def test_repair_property_compact_keeps_both_lines(repairdoc):
    out = dispatch("lts_repair_property", {
        "path": str(repairdoc), "node_id": "REP-A", "line": 4, "key": "flagged"},
        tools=None)
    assert out["success"] is True
    assert out["receipt"] == "compact"
    assert "report" not in out
    assert "properties_before" not in out
    assert out["line_before"] == "- ⛔ FLAGGED 2026-08-10: something broke"
    assert out["line_after"].startswith("- flagged:")
    assert out["value_as_parsed"] == "something broke"
    # this repair MINTS a new key (the malformed line parsed as nothing before), so the
    # delta is non-empty here — unlike a repair that only fixes an existing key's value.
    assert out["delta"] == {"REP-A": {"added": ["flagged"], "removed": []}}
    assert out["nodes_changed"] == 1
    assert props(repairdoc)["REP-A"]["flagged"] == "something broke"


def test_repair_property_full_unchanged(repairdoc):
    out = dispatch("lts_repair_property", {
        "path": str(repairdoc), "node_id": "REP-A", "line": 4, "key": "flagged",
        "receipt": "full"}, tools=None)
    assert out["receipt"] == "full"
    assert "REPAIRED" in out["report"]
    assert "properties_before" in out


# --------------------------------------------------------------------------------
# 6. `receipt` is exposed on the MCP tool schemas.
# --------------------------------------------------------------------------------

def test_receipt_param_is_in_the_tool_schemas():
    from factlattice_engine.server import TOOLS
    for name in ("lts_add_property", "lts_add_node", "lts_repair_property"):
        schema = next(t for t in TOOLS if t["name"] == name)["inputSchema"]
        assert "receipt" in schema["properties"], f"{name} must expose `receipt`"
