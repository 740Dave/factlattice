"""[S5-RANK-RELEVANCE] `lts_find(rank="relevance")`: a deterministic BM25 ORDER of the SAME exact
match set. Membership, counts and every exact field except order are unchanged.

Maintainer ruling 2026-10-02 (a composite ruling recorded internally):
"opt-in, deterministic BM25 over the SAME exact match set, membership and counts unchanged".
The measured reason: probe Q2 "version field" puts the right node 5th of 6 in today's (corpus, id)
order, purely because of id spelling.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from factlattice_engine import config
from factlattice_engine.tools import LtsEngine


def _corpus(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "00-c.lts").write_text(
        "# Corpus [DOC-C]\n\n"
        "## Alpha incidental [A-1]\n\n- what: mentions a version field once, in passing, among many other unrelated words here\n\n"
        "## Beta incidental [B-1]\n\n- note: a version field is not the topic of this node at all\n\n"
        "## The version field itself [Z-9]\n\n- what: the version field of the record\n- alias: version field\n\n"
        "## Unrelated [C-1]\n\n- what: nothing to see\n", encoding="utf-8")


@pytest.fixture()
def eng(tmp_path):
    _corpus(tmp_path / "c")
    return LtsEngine(config.build_store({"c": {"roots": [{"path": str(tmp_path / "c"), "area": "specs"}],
                                               "domain_suffix": "proj.c"}}))


def _ids(r):
    return [n["id"] for n in (r.get("nodes") or r.get("digest") or [])]


def test_default_order_is_unchanged(eng):
    a = eng.find(corpus="c", text="version field")
    b = eng.find(corpus="c", text="version field", rank=None)
    assert a == b and _ids(a) == ["A-1", "B-1", "Z-9"]        # today's (corpus, id) order
    assert "rank_applied" not in a


def test_relevance_reorders_the_same_set_and_nothing_else(eng):
    d = eng.find(corpus="c", text="version field")
    r = eng.find(corpus="c", text="version field", rank="relevance")
    assert sorted(_ids(r)) == sorted(_ids(d))
    assert _ids(r)[0] == "Z-9"                                # the node ABOUT it first
    for k in ("count", "total_matched", "truncated", "complete"):
        assert r.get(k) == d.get(k), k
    assert r["rank_applied"]["by"] == "bm25"


def test_relevance_is_applied_before_paging(eng):
    full = _ids(eng.find(corpus="c", text="version field", rank="relevance"))
    page1 = _ids(eng.find(corpus="c", text="version field", rank="relevance", limit=1))
    assert page1 == full[:1]
    dig = _ids(eng.find(corpus="c", text="version field", rank="relevance", tier="digest"))
    assert dig == full


def test_relevance_is_deterministic(eng):
    runs = {json.dumps(_ids(eng.find(corpus="c", text="version field", rank="relevance"))) for _ in range(5)}
    assert len(runs) == 1


def test_relevance_refusals_are_loud(eng):
    for kwargs, word in ((dict(corpus="c", rank="relevance"), "text"),
                         (dict(text="version", rank="relevance"), "corpus"),
                         (dict(corpus="c", text="version", rank="relevance", order_by="recency"), "order_by"),
                         (dict(corpus="c", text="version", rank="score"), "rank")):
        r = eng.find(**kwargs)
        assert r["success"] is False and word in r["error"], (kwargs, r)


def test_relevance_composes_with_on_miss_zero(eng):
    r = eng.find(corpus="c", text="no such phrase anywhere", rank="relevance")
    assert r["count"] == 0 and r["total_matched"] == 0


def test_relevance_needs_no_model_stack(tmp_path):
    _corpus(tmp_path / "c")
    code = (
        "import sys, json; from factlattice_engine import config; from factlattice_engine.tools import LtsEngine; "
        f"e=LtsEngine(config.build_store({{'c': {{'roots': [{{'path': {str(tmp_path / 'c')!r}, 'area': 'specs'}}], 'domain_suffix': 'proj.c'}}}})); "
        "e.find(corpus='c', text='version field', rank='relevance'); "
        "bad=[m for m in ('numpy','onnxruntime') if m in sys.modules]; sys.exit(1 if bad else 0)")
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
