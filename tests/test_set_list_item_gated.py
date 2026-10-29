"""tools/set_list_item_gated.py: replace ONE value of a multi-valued key through the ledgered
write_whole_file, until the engineer's `set_list_item` verb (design note v1.1 §1, line 170) exists.

Gate: exactly one line differs; the (node, key) list differs at exactly one position (expect_old -> value),
same length; every other key on the node and every other node identical; the edge multiset is identical,
or (relationship key) differs by exactly one edge removed and one added, both from the node.
Addressing by expect_old; `index` only breaks a tie between equal elements. Dry run is the default.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "set_list_item_gated.py"
sys.path.insert(0, str(REPO / "tools"))

DOC = (
    "# D [DOC-A]\n\n"
    "## Flow [N-1]\n\n- step: capture\n- step: wrong value\n- step: verify\n"
    "- see-also: [N-2]\n- see-also: [N-3]\n- kind: flow\n- part-of: [DOC-A]\n\n"
    "## Dup [N-4]\n\n- stage: same\n- stage: same\n- stage: other\n\n"
    "## T [N-2]\n\n- kind: y\n\n## U [N-3]\n\n- kind: z\n\n## V [N-5]\n\n- kind: w\n"
)


@pytest.fixture()
def f(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    p = tmp_path / "corpus" / "d.lts"
    p.parent.mkdir()
    p.write_text(DOC, encoding="utf-8")
    return p


def _G():
    import set_list_item_gated as G
    return G


def test_dry_run_default_writes_nothing_and_reports(f):
    before = f.read_bytes()
    r = subprocess.run([sys.executable, str(TOOL), str(f), "N-1", "step", "wrong value", "corrected value"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["gate"] == "pass" and out["applied"] is False and out["index"] == 1
    assert out["before"] == "- step: wrong value" and out["after"] == "- step: corrected value"
    assert f.read_bytes() == before


def test_apply_swaps_exactly_one_element_and_is_ledgered(f):
    G = _G()
    res = G.set_list_item(f, "N-1", "step", "wrong value", "corrected value", apply=True, reason="false claim")
    assert res["gate"] == "pass" and res["ledger"]["status"] == "ok"
    from factlattice.parser import parse_text
    n = [x for x in parse_text(f.read_text(), "d.lts").nodes if x.id == "N-1"][0]
    assert n.properties["step"] == ["capture", "corrected value", "verify"]


def test_relationship_key_swaps_exactly_one_edge(f):
    G = _G()
    res = G.set_list_item(f, "N-1", "see-also", "[N-3]", "[N-5]", apply=True)
    assert res["gate"] == "pass" and res["edge_delta"] == {"removed": [["N-1", "N-3"]], "added": [["N-1", "N-5"]]}


def test_REFUSES_a_riding_along_change(f, monkeypatch):
    """The architect's control: anything beyond the one element is refused, nothing written."""
    G = _G()
    real = G._candidate

    def sneaky(text, line_no, key, new_value):
        return real(text, line_no, key, new_value).replace("- kind: flow", "- kind: tampered")
    monkeypatch.setattr(G, "_candidate", sneaky)
    before = f.read_bytes()
    with pytest.raises(G.SetListItemRefused, match="more than the one line"):
        G.set_list_item(f, "N-1", "step", "wrong value", "corrected value", apply=True)
    assert f.read_bytes() == before


def test_REFUSES_a_duplicate_element_without_index_and_takes_it_with_index(f):
    """The architect's control: equal elements are ambiguous without an index tiebreak."""
    G = _G()
    before = f.read_bytes()
    with pytest.raises(G.SetListItemRefused, match="ambiguous"):
        G.set_list_item(f, "N-4", "stage", "same", "fixed", apply=True)
    assert f.read_bytes() == before
    res = G.set_list_item(f, "N-4", "stage", "same", "fixed", index=1, apply=True)
    assert res["gate"] == "pass"
    from factlattice.parser import parse_text
    n = [x for x in parse_text(f.read_text(), "d.lts").nodes if x.id == "N-4"][0]
    assert n.properties["stage"] == ["same", "fixed", "other"]


def test_index_must_point_at_an_element_equal_to_expect_old(f):
    G = _G()
    with pytest.raises(G.SetListItemRefused, match="index"):
        G.set_list_item(f, "N-4", "stage", "same", "fixed", index=2, apply=True)


def test_refusals_scalar_part_of_missing_value_unknown_node_bad_value(f):
    G = _G()
    cases = [("N-1", "kind", "flow", "x", "scalar"),            # single-valued: use add_property
             ("N-1", "part-of", "[DOC-A]", "[N-2]", "move"),    # placement: use move
             ("N-1", "step", "absent", "x", "not an element"),
             ("N-404", "step", "a", "b", "no node"),
             ("N-1", "step", "wrong value", "two\nlines", "newline")]
    before = f.read_bytes()
    for node, key, old, new, msg in cases:
        with pytest.raises(G.SetListItemRefused, match=msg):
            G.set_list_item(f, node, key, old, new, apply=True)
    assert f.read_bytes() == before


# ---- review 2026-10-04 -----------------------------------------------------------------------------

def _N(text, node_id):
    from factlattice.parser import parse_text
    return [x for x in parse_text(text, "d.lts").nodes if x.id == node_id][0]


def test_P1_REFUSES_a_concurrent_edit_between_read_and_write(f, monkeypatch):
    """Lost update: another writer lands after the tool's read; the tool must refuse, not overwrite."""
    G = _G()
    real = G._candidate
    interloper = DOC.replace("- kind: flow", "- kind: CONCURRENT").encode()

    def racing(text, line_no, key, new_value):
        cand = real(text, line_no, key, new_value)
        f.write_bytes(interloper)
        return cand
    monkeypatch.setattr(G, "_candidate", racing)
    with pytest.raises(G.SetListItemRefused, match="changed since it was read"):
        G.set_list_item(f, "N-1", "step", "wrong value", "corrected value", apply=True)
    assert f.read_bytes() == interloper


def test_P2_1_REFUSES_a_CRLF_file(f):
    G = _G()
    f.write_bytes(DOC.replace("\n", "\r\n").encode())
    before = f.read_bytes()
    with pytest.raises(G.SetListItemRefused, match="CRLF"):
        G.set_list_item(f, "N-1", "step", "wrong value", "corrected value", apply=True)
    assert f.read_bytes() == before


def test_P3_missing_file_and_bad_encoding_are_Refused_and_the_CLI_prints_JSON(f, tmp_path):
    G = _G()
    with pytest.raises(G.SetListItemRefused):
        G.set_list_item(tmp_path / "nope.lts", "N-1", "step", "a", "b")
    f.write_bytes(b"# D [DOC-A]\n\xff\xfe\n")
    with pytest.raises(G.SetListItemRefused):
        G.set_list_item(f, "N-1", "step", "a", "b")
    r = subprocess.run([sys.executable, str(TOOL), str(tmp_path / "nope.lts"), "N-1", "step", "a", "b"],
                       capture_output=True, text=True)
    assert r.returncode == 1 and json.loads(r.stdout)["gate"] == "refused", r.stderr


# P2-2: each gate layer bites on its own (removing a layer must turn its test red)

def test_P2_2a_property_gate_refuses_a_one_line_rewrite_as_a_different_key(f, monkeypatch):
    G = _G()
    monkeypatch.setattr(G, "_candidate", lambda text, line_no, key, new_value: "\n".join(
        "- kind: x" if i == line_no - 1 else ln for i, ln in enumerate(text.split("\n"))))
    before = f.read_bytes()
    with pytest.raises(G.SetListItemRefused, match="another key|parses as"):
        G.set_list_item(f, "N-1", "step", "wrong value", "corrected value", apply=True)
    assert f.read_bytes() == before


def test_P2_2a2_property_gate_refuses_when_the_list_does_not_parse_as_the_wanted_replacement(f, monkeypatch):
    """One line changed, same key, but the list does not parse as the wanted single replacement."""
    G = _G()
    monkeypatch.setattr(G, "_candidate", lambda text, line_no, key, new_value: "\n".join(
        "- step: a" if i == line_no - 1 else ln for i, ln in enumerate(text.split("\n"))))
    before = f.read_bytes()
    with pytest.raises(G.SetListItemRefused, match="parses as"):
        G.set_list_item(f, "N-1", "step", "wrong value", "corrected value", apply=True)
    assert f.read_bytes() == before


def _after_parse_patch(monkeypatch, G, mutate):
    """Corrupt the 'after' parse (2nd parse_text call) so ONE gate layer is the only thing that can notice."""
    real = G.parse_text
    calls = {"n": 0}

    def wrapped(text, name):
        calls["n"] += 1
        parsed = real(text, name)
        if calls["n"] == 2:
            mutate(parsed)
        return parsed
    monkeypatch.setattr(G, "parse_text", wrapped)


def _by_id(parsed, node_id):
    return next(n for n in parsed.nodes if n.id == node_id)


@pytest.mark.parametrize("layer,mutate,match", [
    ("another key on the node", lambda pf: _by_id(pf, "N-1").properties.__setitem__("kind", "tampered"),
     "another key"),
    ("another node's properties", lambda pf: _by_id(pf, "N-2").properties.__setitem__("kind", "tampered"),
     "properties of N-2 changed"),
    ("node-id list", lambda pf: pf.nodes.pop(), "node-id list changed"),
])
def test_P2_2a3_each_property_and_id_layer_bites_on_its_own(f, monkeypatch, layer, mutate, match):
    G = _G()
    _after_parse_patch(monkeypatch, G, mutate)
    before = f.read_bytes()
    with pytest.raises(G.SetListItemRefused, match=match):
        G.set_list_item(f, "N-1", "step", "wrong value", "corrected value", apply=True)
    assert f.read_bytes() == before


def _edge_patch(monkeypatch, G, mutate):
    real = G.parse_text
    calls = {"n": 0}

    def wrapped(text, name):
        calls["n"] += 1
        parsed = real(text, name)
        if calls["n"] == 2:                      # the 'after' parse
            parsed.edges = mutate(list(parsed.edges))
        return parsed
    monkeypatch.setattr(G, "parse_text", wrapped)


def test_P2_2b_edge_gate_refuses_an_extra_edge(f, monkeypatch):
    import dataclasses
    G = _G()
    _edge_patch(monkeypatch, G, lambda es: es + [dataclasses.replace(es[0], source="N-1", target="N-5")])
    before = f.read_bytes()
    with pytest.raises(G.SetListItemRefused, match="not exactly one swap"):
        G.set_list_item(f, "N-1", "see-also", "[N-3]", "[N-5]", apply=True)
    assert f.read_bytes() == before


def test_P2_4_edge_swap_must_have_the_same_type(f, monkeypatch):
    import dataclasses
    G = _G()

    def retype(es):
        return [dataclasses.replace(e, type="DEPENDS_ON") if (e.source, e.target) == ("N-1", "N-5") else e
                for e in es]
    _edge_patch(monkeypatch, G, retype)
    before = f.read_bytes()
    with pytest.raises(G.SetListItemRefused, match="not exactly one swap|type"):
        G.set_list_item(f, "N-1", "see-also", "[N-3]", "[N-5]", apply=True)
    assert f.read_bytes() == before


def test_P2_2c_unlocatable_line_is_refused_and_names_repair_property(f):
    """Two equal parsed elements, one written as a bold key the line regex does not match."""
    G = _G()
    f.write_text(DOC.replace("- stage: same\n- stage: same\n", "- **stage**: same\n- stage: same\n"), encoding="utf-8")
    before = f.read_bytes()
    with pytest.raises(G.SetListItemRefused, match="cannot locate") as ei:
        G.set_list_item(f, "N-4", "stage", "same", "fixed", index=0, apply=True)
    assert "repair_property" in str(ei.value)
    assert f.read_bytes() == before


# P2-3: self edge refused; unverified targets reported

def test_P2_3_REFUSES_a_self_edge(f):
    G = _G()
    before = f.read_bytes()
    with pytest.raises(G.SetListItemRefused, match="itself|self"):
        G.set_list_item(f, "N-1", "see-also", "[N-3]", "[N-1]", apply=True)
    assert f.read_bytes() == before


def test_P2_3_reports_new_targets_that_are_not_nodes_in_this_file(f):
    G = _G()
    out = G.set_list_item(f, "N-1", "see-also", "[N-3]", "[OTHER-FILE-9]")      # dry run
    assert out["gate"] == "pass" and out["new_ref_targets_unverified"] == ["OTHER-FILE-9"]
    out = G.set_list_item(f, "N-1", "see-also", "[N-3]", "[N-5]")
    assert out["new_ref_targets_unverified"] == []
    out = G.set_list_item(f, "N-1", "step", "wrong value", "corrected value")
    assert "new_ref_targets_unverified" not in out


def test_every_tools_writer_passes_expect_base():
    """SDLC 7.6 [INTERIM-WRITER-EXPECT-BASE] made mechanical (delta review P2-1): every write_whole_file call
    in tools/*.py passes expect_base, so a new interim writer cannot reintroduce the lost update."""
    import re
    bad = []
    for p in (REPO / "tools").glob("*.py"):
        src = p.read_text(encoding="utf-8")
        for m in re.finditer(r"write_whole_file\(", src):
            call = src[m.start(): src.find(")", m.start()) + 1]
            if "expect_base" not in call and "import" not in src[max(0, m.start() - 40): m.start()]:
                bad.append(f"{p.name}: {call[:80]}")
    assert not bad, bad
