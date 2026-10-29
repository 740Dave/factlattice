"""[FIND-QUERY-SURFACE] shared fixture corpora + the C2 base-parity call list.

Imported by tests/test_query_surface*.py AND by tests/fixtures/record_qs_base.py, which records the
base (pre-change) responses of `BASE_CALLS` so the surface-parity test can prove a call using none of
the new parameters is byte-identical before and after.
"""
from __future__ import annotations

import json
import os
import re

from factlattice import build_index
from factlattice_engine import LtsEngine, LtsStore, Partition, config

# ---- corpus A: terms / modes / where / document_tag fixture ---------------------------------
A = """---
domain: PLT.QS
---

# Alpha doc [SPEC:QS-ROOT]

- status: operational

## Lantern architect note [PROC-LANTERN-ARCH]

- status: operational
- kind: note
- tier: gold
- decided-in-session: 2026-09-01
- alias: lantern qa checklist
- alias: second alias

Body prose mentions zorblax once.

## Alantern quality Aqaba [PROC-ALANTERN]

- status: draft
- kind: Note
- tier: silver
- decided-in-session: 2026-10-01

Body prose only.

## go-live checklist [PROC-LIVE]

- status: operational
- function: alpha
- service: svc1
- decided-in-session: 2026-09-15

## go live now [PROC-LIVE2]

- status: operational
- tier: gold

## go to live [PROC-LIVE3]

- status: operational

## Init [XL-M1]

- status: operational
- input: Borrower card CONTENTS (required)

## Borrower notes [XL-M2]

- status: operational
- description: card contents differ

## Qatar plan [PLAN-QATAR]

- status: operational
- kind: plan

## Y list a x [Y-LIST-AX]

- status: operational
- ykey: a
- ykey: x

## Y list a b [Y-LIST-AB]

- status: operational
- ykey: a
- ykey: b

## Y absent [Y-ABSENT]

- status: operational

## Y scalar x [Y-SCALAR-X]

- status: operational
- ykey: x

## Y scalar a [Y-SCALAR-A]

- status: operational
- ykey: a

## Y mapping [Y-MAP]

- status: operational
- ymap:
  sub: a

## Qualified status [Q-QUAL]

- status: proposed — awaiting review
"""

B = """---
domain: PLT.QS
---

# Untagged doc [UNT-ROOT]

- status: draft

## Untagged node [UNT-NODE]

- status: draft
- kind: note
"""


def engine_a(tmp_path, name="qs"):
    root = tmp_path / name
    root.mkdir()
    (root / "a.lts").write_text(A, encoding="utf-8")
    (root / "b.lts").write_text(B, encoding="utf-8")
    idx = build_index(str(root), corpus_id=name, domain_suffix="plt.qs", built_at="t")
    store = LtsStore()
    store.add_partition(Partition(idx, source_path=str(root)))
    return LtsEngine(store), str(root)


# ---- corpus M: two roots, same relative path under both (multi-root ambiguity) ---------------
def engine_multiroot(tmp_path):
    r0, r1 = tmp_path / "r0", tmp_path / "r1"
    r0.mkdir()
    r1.mkdir()
    (r0 / "same.lts").write_text("# D0 [SPEC:M-D0]\n\n- status: operational\n\n## One [M-ONE]\n\n- status: operational\n",
                                 encoding="utf-8")
    (r1 / "same.lts").write_text("# D1 [SPEC:M-D1]\n\n- status: operational\n\n## Two [M-TWO]\n\n- status: operational\n",
                                 encoding="utf-8")
    (r1 / "only1.lts").write_text("# D2 [SPEC:M-D2]\n\n- status: operational\n\n## Three [M-THREE]\n\n- status: operational\n",
                                  encoding="utf-8")
    cfg = {"m": {"roots": [{"path": str(r0), "area": "decisions"}, {"path": str(r1), "area": "specs"}]}}
    return LtsEngine(config.build_store(cfg))


# ---- the C2 base-parity call list (>= 20 representative EXISTING calls) ----------------------
# (name, tool, arguments). Dispatched through tools.dispatch so the schema-level path is covered.
BASE_CALLS = [
    ("find_text_hit", "lts_find", {"corpus": "qs", "text": "borrower card contents", "semantic": "off"}),
    ("find_text_zero", "lts_find", {"corpus": "qs", "text": "borrower contents", "semantic": "off"}),
    ("find_text_single_char", "lts_find", {"corpus": "qs", "text": "qa", "semantic": "off"}),
    ("find_text_case", "lts_find", {"corpus": "qs", "text": "LANTERN", "semantic": "off"}),
    ("find_text_alias_list", "lts_find", {"corpus": "qs", "text": "second alias", "semantic": "off"}),
    ("find_prefix", "lts_find", {"corpus": "qs", "prefix": "PROC-", "semantic": "off"}),
    ("find_prefix_nohyphen", "lts_find", {"corpus": "qs", "prefix": "PROC", "semantic": "off"}),
    ("find_status_bare", "lts_find", {"corpus": "qs", "status": "operational", "semantic": "off"}),
    ("find_status_draft", "lts_find", {"corpus": "qs", "status": "draft", "semantic": "off"}),
    ("find_area", "lts_find", {"corpus": "qs", "area": "decisions", "semantic": "off"}),
    ("find_since", "lts_find", {"corpus": "qs", "since": "2026-09-10", "semantic": "off"}),
    ("find_since_until", "lts_find", {"corpus": "qs", "since": "2026-09-01", "until": "2026-09-30", "semantic": "off"}),
    ("find_function", "lts_find", {"corpus": "qs", "function": "alpha", "semantic": "off"}),
    ("find_service_text", "lts_find", {"corpus": "qs", "service": "svc1", "text": "go", "semantic": "off"}),
    ("find_domain", "lts_find", {"corpus": "qs", "domain": "plt.qs", "prefix": "XL-", "semantic": "off"}),
    ("find_rank_relevance", "lts_find", {"corpus": "qs", "text": "go", "rank": "relevance", "semantic": "off"}),
    ("find_tier_digest", "lts_find", {"corpus": "qs", "prefix": "PROC-", "tier": "digest", "semantic": "off"}),
    ("find_tier_page", "lts_find", {"corpus": "qs", "prefix": "Y-", "tier": "page", "semantic": "off"}),
    ("find_tier_full", "lts_find", {"corpus": "qs", "prefix": "XL-", "tier": "full", "semantic": "off"}),
    ("find_limit_order", "lts_find", {"corpus": "qs", "prefix": "PROC-", "limit": 2, "order_by": "recency", "semantic": "off"}),
    ("find_offset_budget", "lts_find", {"corpus": "qs", "prefix": "Y-", "token_budget": 60, "offset": 0, "semantic": "off"}),
    ("find_reverse_digest", "lts_find", {"corpus": "qs", "prefix": "XL-", "include_reverse_digest": True, "semantic": "off"}),
    ("find_no_corpus", "lts_find", {"text": "lantern", "semantic": "off"}),
    ("find_unknown_corpus", "lts_find", {"corpus": "nope", "text": "x", "semantic": "off"}),
    ("find_rank_needs_text", "lts_find", {"corpus": "qs", "rank": "relevance", "semantic": "off"}),
    ("find_bad_semantic", "lts_find", {"corpus": "qs", "semantic": "maybe"}),
    ("prose_text", "lts_find_in_prose", {"corpus": "qs", "text": "zorblax"}),
    ("prose_body_only", "lts_find_in_prose", {"corpus": "qs", "text": "body prose", "body_only": True}),
    ("prose_digest", "lts_find_in_prose", {"corpus": "qs", "text": "prose", "tier": "digest", "prefix": "PROC-"}),
    ("prose_empty", "lts_find_in_prose", {"corpus": "qs", "text": "  "}),
    ("agg_count", "lts_aggregate", {"op": "count", "corpus": "qs"}),
    ("agg_count_prefix", "lts_aggregate", {"op": "count", "corpus": "qs", "prefix": "PROC-", "group_by": "status"}),
    ("agg_group_by", "lts_aggregate", {"op": "group_by", "attr": "tier", "corpus": "qs"}),
    ("agg_distinct", "lts_aggregate", {"op": "distinct", "attr": "kind", "corpus": "qs", "text": "n"}),
    ("audit_basic", "lts_profile_audit", {"corpus": "qs", "required_property": "status", "required_scope": "prefix=PROC-"}),
]


def _scrub_timing(o):
    """Wall-clock numbers (lts_find_in_prose's elapsed_ms and its 'N ms' note) are not behaviour."""
    if isinstance(o, dict):
        return {k: (0 if k == "elapsed_ms" else _scrub_timing(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_scrub_timing(v) for v in o]
    if isinstance(o, str):
        return re.sub(r"[0-9.]+ ms", "<ms>", o)
    return o


def norm(obj, root: str):
    """JSON-normalise a response: tmp roots replaced, timings scrubbed, so base and branch compare
    byte for byte."""
    s = json.dumps(obj, sort_keys=False, default=str, ensure_ascii=False)
    return _scrub_timing(json.loads(s.replace(root, "<ROOT>")))


def run_base_calls(tmp_path, state_dir):
    """Run BASE_CALLS and return {name: normalised response (+ the query-log record's filters)}."""
    from factlattice_engine import querylog, tools
    os.environ["FACTLATTICE_STATE_DIR"] = str(state_dir)
    eng, root = engine_a(tmp_path)
    out = {}
    for name, tool, args in BASE_CALLS:
        res = tools.dispatch(tool, dict(args), eng)
        rec = querylog.build_record(tool, dict(args), res, 0.0, session="S")
        rec.pop("ts", None)
        out[name] = norm({"response": res, "log": rec}, root)
    return out
