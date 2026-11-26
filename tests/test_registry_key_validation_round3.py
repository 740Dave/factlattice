"""Round 3 of registry-key validation (Diamond on e4726f5: P3-A, P2-B).

P3-A  a `private`-shaped key written on a ROOT object gets a hint that points at the fix (`private` is an entry key, not a root
      key), at every caller of the shared hint: the registry helper, the contradiction checker and the corpus-failure reply.
      Other hints are unchanged.
P2-B  the `lts_list_corpora` description states the same private-counting rule as SPEC section 4.6: any case or a near-miss,
      at entry or root level, whatever the value.
"""
from __future__ import annotations

import pytest

ROOT_HINT = "`private` is an entry key, not a root key"


def _entry(root_key, value=True):
    return {"roots": [{"path": "/nowhere", root_key: value}]}


@pytest.mark.parametrize("key", ["private", "Private", "PRIVATE", "privat"])
def test_root_level_private_hint_points_at_the_entry(key):
    from factlattice.registry import entry_key_problems, problem_hint
    probs = entry_key_problems(_entry(key))
    assert [p["scope"] for p in probs] == ["root"]
    assert ROOT_HINT in problem_hint(probs[0], "root")


def test_other_hints_are_unchanged():
    from factlattice.registry import entry_key_problems, problem_hint
    (root_typo,) = entry_key_problems(_entry("pth", "/x"))
    assert problem_hint(root_typo, "root") == "; the nearest valid key is 'path'"
    (entry_typo,) = entry_key_problems({"privat": True, "roots": []})
    assert problem_hint(entry_typo, "entry") == "; the nearest valid key is 'private'"
    (note_case,) = entry_key_problems(_entry("why_NOTE", "x"))
    assert problem_hint(note_case, "root") == "; comment keys end in lowercase `_note`"
    (far,) = entry_key_problems(_entry("zzzzzz", "x"))
    assert problem_hint(far, "root") == "; no valid key is close"


def test_contradiction_checker_carries_the_root_private_hint():
    from factlattice.authoring.contradiction import external_prefixes_from_registry
    with pytest.raises(ValueError) as e:
        external_prefixes_from_registry({"c": _entry("private")}, ["c"])
    assert ROOT_HINT in str(e.value)


def test_corpus_failure_reply_carries_the_root_private_hint():
    from factlattice.registry import entry_key_problems
    from factlattice_engine import tools
    problems = entry_key_problems(_entry("private"))
    reply = tools._registry_key_refusal("c", {"code": problems[0]["code"], "problems": problems})
    assert ROOT_HINT in reply["error"]


def test_list_corpora_description_states_the_spec_rule():
    from factlattice_engine.server import TOOLS
    desc = next(t for t in TOOLS if t["name"] == "lts_list_corpora")["description"]
    assert "near-miss of `private`" in desc
    assert "in any case" in desc and "root" in desc and "whatever its value" in desc
