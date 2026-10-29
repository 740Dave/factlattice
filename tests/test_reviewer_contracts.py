"""X1/X4 VERSIONED-CONTRACTS, REVIEWER-CONFIG-KEYS, REVIEW-KEY-GRAMMAR (SDLC 7.6 rows, factlattice-reviewer Spec A).

The reviewer's contracts are written out LITERALLY in `tests/reviewer_contract.py` and checked here against every
place the shipped system states them: the Python constants, the shipped hook nodes, the profile's required lists
and registered prefixes, the REVIEWER-CONFIG node, and the core procedure's own text. Renaming or removing a hook
or a review field in ANY of those makes a test here fail until the literals move with it, and moving the literals
is the contract change that bumps `hook-contract-version` / `review-format-version`.
"""
from __future__ import annotations

import datetime
import os
import re

import pytest

from factlattice.parser import parse_file
from factlattice.profile import load_profile
from factlattice_engine import reviewer
from tests import reviewer_contract as C

CORPUS = reviewer.CORPUS_DIR


def _nodes(fname):
    return {n.id: n for n in parse_file(os.path.join(CORPUS, fname)).nodes}


# ----------------------------------------------------------------------------------------- hooks (X1)
def test_the_hook_names_and_contract_version_are_pinned_together():
    assert reviewer.CORE_HOOKS == C.CORE_HOOKS
    assert reviewer.HOOK_CONTRACT_VERSION == C.HOOK_CONTRACT_VERSION == 1
    hooks = {i: n for i, n in _nodes("reviewer-hooks.lts").items() if i.startswith("REVHOOK-")}
    assert tuple(sorted(hooks)) == tuple(sorted(C.CORE_HOOKS))
    for i, n in hooks.items():
        assert n.properties["hook-contract-version"] == str(C.HOOK_CONTRACT_VERSION), i
        assert "stub-of" not in n.properties, f"{i}: a core hook is not a stub"


def test_every_core_step_attaches_to_a_pinned_hook_and_every_hook_has_a_step():
    core = parse_file(os.path.join(CORPUS, "reviewer-core.lts"))
    targets = [e.target for e in core.edges if e.type == "ATTACHES_TO_HOOK"]
    assert targets and set(targets) <= set(C.CORE_HOOKS)
    assert set(targets) == set(C.CORE_HOOKS), "a hook with no core step attached"


def test_the_core_procedure_is_proposed_until_approved():
    """AGENT-DEF-CHECKLIST r9."""
    nodes = {i: n for i, n in _nodes("reviewer-core.lts").items() if i.startswith("REVPROC-")}
    assert "REVPROC-CORE" in nodes and len(nodes) == 11
    for i, n in nodes.items():
        assert n.properties.get("status") == "proposed", i
        assert re.fullmatch(r"\d+", n.properties["order"]), i
    assert sorted(int(n.properties["order"]) for n in nodes.values()) == list(range(0, 11))


# ----------------------------------------------------------------------------------------- review fields (X4)
def test_the_review_format_version_is_pinned():
    assert reviewer.REVIEW_FORMAT_VERSION == C.REVIEW_FORMAT_VERSION == 2


def test_the_profile_required_lists_are_the_pinned_field_lists():
    prof = load_profile(reviewer.PROFILE_PATH)
    for fam, want in C.REQUIRED.items():
        assert sorted(prof.required_properties(fam)) == sorted(want), fam
    for fam, want in C.RECOMMENDED.items():
        assert sorted(prof.recommended_properties(fam)) == sorted(want), fam
    for fam in set(C.REQUIRED) - set(C.RECOMMENDED):
        assert prof.recommended_properties(fam) == frozenset(), fam


def test_the_profile_registers_exactly_the_reserved_prefixes():
    prof = load_profile(reviewer.PROFILE_PATH)
    assert sorted(prof.prefix_set) == sorted(C.PREFIXES)


def test_every_required_key_is_declared_so_a_strict_review_does_not_reject_it():
    prof = load_profile(reviewer.PROFILE_PATH)
    from factlattice.parser import FLOW_PROPERTY_EDGES, REL_PROPERTY_EDGES
    minting = set(REL_PROPERTY_EDGES) | set(FLOW_PROPERTY_EDGES)
    need = {k for keys in C.REQUIRED.values() for k in keys} | set(C.RECOMMENDED["REVPROV"]) | set(C.OPTIONAL_KEYS)
    need -= minting                                    # built-in minting keys are never declared (and are refused if)
    assert need <= set(prof.declared_property_keys), sorted(need - set(prof.declared_property_keys))


def test_the_pinned_lists_are_not_vacuous():
    assert sum(len(v) for v in C.REQUIRED.values()) == 22 + 13 + 11 + 5 + 5 + 4 + 1 + 1 + 7
    assert len(C.REQUIRED["REVIEW"]) == 22 and len(C.REQUIRED["UNREVIEWED"]) == 13
    assert "creation-ledger-state" in C.REQUIRED["REVIEW"] and "creation-ledger-state" in C.REQUIRED["UNREVIEWED"]


# ----------------------------------------------------------------------------------------- REVIEWER-CONFIG-KEYS
def test_reviewer_config_keys_are_pinned_declared_and_hold_no_site_values():
    assert reviewer.CONFIG_KEYS == C.CONFIG_KEYS
    node = _nodes("reviewer-config.lts")["REVIEWER-CONFIG"]
    assert tuple(sorted(node.properties)) == tuple(sorted(C.CONFIG_KEYS))
    assert set(C.CONFIG_KEYS) <= set(load_profile(reviewer.PROFILE_PATH).declared_property_keys)
    for k, v in node.properties.items():
        assert str(v).strip().lower() == "none", f"{k}: the shipped config holds a site value ({v!r})"
    parsed = reviewer.parse_site_config(node.properties, source="x")
    assert parsed.default_output_root is None and parsed.overrides == () and parsed.default_root_corpora == ()


def test_the_shipped_site_config_node_is_not_the_site_lookup_target():
    """The site lookup is `prefix=REVIEWER-SITE-CONFIG`; the shipped REVIEWER-CONFIG must not match it."""
    assert not "REVIEWER-CONFIG".startswith(reviewer.SITE_CONFIG_NODE)
    assert not any(i.startswith(reviewer.SITE_CONFIG_NODE) for i in _nodes("reviewer-config.lts"))


# ----------------------------------------------------------------------------------------- REVIEW-KEY-GRAMMAR
NOW = datetime.datetime(2026, 10, 7, 12, 30, 5, tzinfo=datetime.timezone.utc)


def test_review_key_grammar():
    k = reviewer.review_key("acme-docs-lts", now=NOW, rand=0xAB12)
    assert k == "ACME-DOCS-LTS-20261007T123005Z-AB12"
    assert reviewer.REVIEW_KEY_RE.match(k)
    # id-grammar safe in every node id it appears in
    for nid in (f"REVIEW-{k}", f"UNREVIEWED-{k}", f"REVFINDING-{k}-12", f"REVCHECK-{k}-1", f"REVPROV-{k}-3"):
        assert re.fullmatch(r"[A-Z][A-Z0-9_-]*", nid), nid


@pytest.mark.parametrize("given,slug", [("Sample Corpus_LTS", "SAMPLE-CORPUS-LTS"), (None, "NO-CORPUS"), ("..", "NO-CORPUS"),
                                        ("../../etc", "ETC"), ("a/b", "A-B")])
def test_review_key_slug_is_sanitised(given, slug):
    k = reviewer.review_key(given, now=NOW, rand=1)
    assert k == f"{slug}-20261007T123005Z-0001" and reviewer.REVIEW_KEY_RE.match(k)


def test_review_key_changes_with_time_corpus_and_the_random_part():
    base = reviewer.review_key("c", now=NOW, rand=1)
    assert base != reviewer.review_key("c", now=NOW + datetime.timedelta(seconds=1), rand=1)
    assert base != reviewer.review_key("d", now=NOW, rand=1)
    assert base != reviewer.review_key("c", now=NOW, rand=2)
    assert len({reviewer.review_key("c") for _ in range(50)}) > 40          # the live path is random


@pytest.mark.parametrize("bad", ["", "lower-20261007T123005Z-AB12", "X-20261007T123005Z-ab12", "X-20261007T123005-AB12",
                                 "X-20261007T123005Z-AB1", "X_Y-20261007T123005Z-AB12", "X-2026-10-07-AB12"])
def test_review_key_grammar_rejects(bad):
    assert not reviewer.REVIEW_KEY_RE.match(bad)


def test_two_reviews_with_the_same_key_cannot_share_a_file(tmp_path):
    """Uniqueness is enforced by the exclusive create (A-E1), not by trusting the random part."""
    from factlattice_engine.tools import dispatch
    out = tmp_path / "reviews" / "docs"
    out.parent.mkdir()
    k = reviewer.review_key("docs", now=NOW, rand=7)
    args = {"path": str(out / f"REVIEW-{k}.lts"), "node_id": f"REVIEW-{k}", "title": "r", "create": True,
            "create_parent": True, "doctype": "review", "properties": {"verdict": "in-progress"}}
    first = dispatch("lts_add_node", dict(args), tools=None)
    second = dispatch("lts_add_node", dict(args), tools=None)
    assert first["success"] is True and first["created"] is True
    assert second["refused"] is True and second["evidence"]["refusal"] == "create_file_exists"
    assert open(args["path"]).read().count(f"[REVIEW-{k}]") == 1


# ----------------------------------------------------------------------------------------- the PROC's words match the tools
def test_the_core_text_names_fields_that_the_tools_really_return(tmp_path):
    """REVPROC-CORE tells the reviewer to read `totals.headline_property_lines_lost` (F6) and to require
    `file_source` and `extent.scope.corpus == (one file, by path)` (stale-server guard). Pin that those names are
    what the tools return TODAY, so the procedure cannot drift from the engine."""
    from factlattice_engine.tools import dispatch
    text = open(os.path.join(CORPUS, "reviewer-core.lts"), encoding="utf-8").read()
    for needle in ("totals.headline_property_lines_lost", "totals.files_scanned", "file_source",
                   "extent.scope.corpus", "(one file, by path)", "outcome", "ledger_state", "created"):
        assert needle in text, needle
    lost = tmp_path / "lost.lts"
    lost.write_text("# D [SPEC:PL]\n\n## [PL-A] a\n\n- ok: fine\n- **Bold Key**: lost\n")
    r = dispatch("lts_property_loss", {"roots": [str(lost)]}, tools=None)
    assert r["totals"]["headline_property_lines_lost"] == 1 and r["totals"]["files_scanned"] == 1
    clean = tmp_path / "clean.lts"
    clean.write_text("# D [SPEC:PL]\n\n## [PL-A] a\n\n- ok: fine\n")
    z = dispatch("lts_property_loss", {"roots": [str(clean)]}, tools=None)
    assert z["totals"]["headline_property_lines_lost"] == 0 and z["totals"]["files_scanned"] == 1
    # an empty DIRECTORY scans nothing, which is what the files_scanned guard catches. (A path that does not exist
    # reports files_scanned 1 and 0 lost: an engine gap the run criteria cover by confirming the document exists.)
    empty = tmp_path / "emptydir"
    empty.mkdir()
    e = dispatch("lts_property_loss", {"roots": [str(empty)]}, tools=None)
    assert e["totals"]["files_scanned"] == 0 and e["totals"]["headline_property_lines_lost"] == 0
