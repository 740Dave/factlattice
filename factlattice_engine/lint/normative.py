"""NORMATIVE-CONTENT lint (spec 39): the findings that need the whole corpus.

The findings about the keys of one node (f1 to f5, f6 the quote tripwire, f8 to f11, f14) are raised by the parser and surface
through `vocabulary.py`, with the severity the document's mode decides. This module holds the rest, all
WARNINGS, which need the node set or the edge set:

  restates_target_not_statement   f7   `restates` names an existing node that carries no `source-text`
  restates_from_non_normative     f7   `restates` on a node that is itself non-normative
  confirmation_of_target_not_statement   f12  `confirmation-of` names an existing node that carries no `source-text`
  checked_against_non_normative   f12  `checked-against` names an existing non-normative node
  quote_corrected_unrevised       f13  a node the DECIDING confirmation of a quote is checked against, when
                                        that confirmation is `corrected`, and the node carries no
                                        `revised-per` naming it

The deciding confirmation of a quote is the one with the latest `confirmation-date`, compared by the full
value (a date-only value is the start of that day, UTC). `corrected` wins only on exactly equal values, so
the quote is checked again; when several corrected confirmations tie, `revised-per` naming any of them
clears the warning. A superseded correction raises nothing. Reads the PARSER's edges and properties; no
second grammar.
"""
from __future__ import annotations

import factlattice
from factlattice.core_keys import EFFECTIVE_KEY
from factlattice.parser import _property_refs, confirmation_date_key

ORIGIN = "normative"

K_RESTATES_TARGET = "restates_target_not_statement"
K_RESTATES_FROM = "restates_from_non_normative"
K_CONFIRMS_TARGET = "confirmation_of_target_not_statement"
K_CHECKED_NONNORM = "checked_against_non_normative"
K_CORRECTED = "quote_corrected_unrevised"

NORMATIVE_SEVERITY: dict[str, str] = {k: "warning" for k in (
    K_RESTATES_TARGET, K_RESTATES_FROM, K_CONFIRMS_TARGET, K_CHECKED_NONNORM, K_CORRECTED)}



def _items(v):
    return v if isinstance(v, list) else [v]


def _refs(node, key):
    return [tid for (dom, tid) in _property_refs((node.properties or {}).get(key, "")) if not dom]


def _is_nonnormative(n) -> bool:
    return (n.properties or {}).get(EFFECTIVE_KEY) == "false"


def find_normative_findings(corpus: factlattice.ParsedCorpus) -> list[dict]:
    out: list[dict] = []
    by_id = {}
    for n in corpus.nodes:
        by_id.setdefault(n.id, n)

    def add(kind, node, message, **detail):
        out.append({"kind": kind, "node_id": node.id, "message": message,
                    "source_file": node.source_file, "source_line": node.line_range[0],
                    "detail": detail})

    for n in corpus.nodes:
        props = n.properties or {}
        # f7: restates
        if "restates" in props:
            if _is_nonnormative(n):
                add(K_RESTATES_FROM, n, f"`restates` on [{n.id}], which is non-normative; only a normative "
                    f"node restates a source statement.")
            for tid in _refs(n, "restates"):
                t = by_id.get(tid)
                if t is not None and "source-text" not in (t.properties or {}):
                    add(K_RESTATES_TARGET, n, f"`restates` on [{n.id}] names [{tid}], which is not a source "
                        f"statement (no `source-text`).", target=tid)
        # f12
        if "confirmation-of" in props:
            for tid in _refs(n, "confirmation-of"):
                t = by_id.get(tid)
                if t is not None and "source-text" not in (t.properties or {}):
                    add(K_CONFIRMS_TARGET, n, f"`confirmation-of` on [{n.id}] names [{tid}], which is not a source "
                        f"statement (no `source-text`).", target=tid)
        if "checked-against" in props:
            for tid in _refs(n, "checked-against"):
                t = by_id.get(tid)
                if t is not None and _is_nonnormative(t):
                    add(K_CHECKED_NONNORM, n, f"`checked-against` on [{n.id}] names [{tid}], which is "
                        f"non-normative.", target=tid)

    # f13: group confirmations by the statement they confirm
    by_quote: dict[str, list] = {}
    for n in corpus.nodes:
        if "confirmation-of" not in (n.properties or {}):
            continue
        refs = _refs(n, "confirmation-of")
        key = confirmation_date_key((n.properties or {}).get("confirmation-date"))
        if len(refs) == 1 and key is not None:
            by_quote.setdefault(refs[0], []).append((key, n))
    for quote, rows in sorted(by_quote.items()):
        top = max(k for k, _ in rows)
        tied = [c for k, c in rows if k == top]
        corrected = [c for c in tied if (c.properties.get("confirmation-verdict") or "").strip() == "corrected"]
        if not corrected:
            continue
        ids = sorted(c.id for c in corrected)
        targets: dict[str, None] = {}
        for c in corrected:
            for tid in _refs(c, "checked-against"):
                targets.setdefault(tid)
        for tid in targets:
            t = by_id.get(tid)
            if t is None:
                continue
            named = {x for (_d, x) in _property_refs((t.properties or {}).get("revised-per", ""))}
            if not named & set(ids):
                add(K_CORRECTED, t,
                    f"the stakeholder corrected the quote this node restates ([{quote}], by confirmation "
                    f"{', '.join('[' + i + ']' for i in ids)}); revise the node and name the correction "
                    f"with `revised-per: [{ids[0]}]`.", quote=quote, confirmations=ids)
    return out
