"""The RESPONSE-TIER layer — `digest` / `page` / `full` ([LTS-TIER]).

WHY THIS LAYER EXISTS
=====================
`lts_traverse` slices against `token_budget` and declares the slice honestly
(`truncated`, `cursor`, `total`). That is honest at the PROTOCOL level and dishonest
ERGONOMICALLY: the easy path returns a fraction that is well-formed and reads like a
complete answer. Measured, 2026-08-13, against the live corpus:

    traverse(corpus='acme-console', start_node='ACME-VISION',
             token_budget=2500)
      -> 4 of 10 nodes, truncated=True, cursor=4
         (ACME-VISION + 3; NONE of the five substantive children)

A root plus a footnote, shaped exactly like "what this vision is".

THE PRINCIPLE: **truncation is a lie; an error is a fact.** A response must never be a
partial result wearing a whole result's clothes. Three tiers, each with a contract it
cannot break:

  digest — ALWAYS COMPLETE for what it claims. Every node in the result set, carrying
           NOTHING derived from the body. Cheap because it has no bodies; if it is ever
           big enough that truncation would be tempting, it ERRORS instead — that size
           is a signal, not something to quietly trim.
  page   — full node content, ONE explicit page: `offset` in, `next_offset` +
           `total` + `truncated` out. Truncation here is not a lie because the response
           is NAMED a page.
  full   — full node content, COMPLETE. Exceeding the ceiling ERRORS rather than
           truncating. The caller who asks for `full` has accepted the cost; what they
           have not accepted is a silent fraction.

  (tier omitted) — the LEGACY path, byte-identical to the pre-tier engine. Nothing
           about an existing caller changes unless it opts in. See `resolve_tier`.

WHAT A DIGEST DELIBERATELY DOES NOT CARRY
=========================================
No `properties`. No summary. No excerpt. No first-N-characters. No "key properties".
No highlights. Nothing derived from the body — see `BODY_DERIVED_KEYS`, which the test
suite asserts against every digest row this module can produce.

This is the DESIGN CONSTRAINT, not an omission. A digest must answer

    "is there something here I need to read?"

and must NOT answer

    "what does it say?"

A summary is a partial answer wearing a complete answer's clothes — the same failure as
truncation, packaged more attractively, and it trades an agent that KNOWS it is missing
something for one that is CONFIDENT it is not. A digest that tempts you to stop is worse
than a cap, because a cap at least leaves you suspicious. **The insufficiency is the
feature.**

The acceptance test for any field someone is tempted to add here: *could an agent
plausibly write a paragraph on the subject from the digest alone?* If yes, the field is
too rich. Titles are the boundary case and are IN: a title is a LABEL and is necessary
for routing; a summary is a CLAIM and is not.

`timestamp` is the one field read out of the property bag, and it is laundered through
`normalize_date` so the value can only ever be a `YYYY-MM-DD` — a prose date property
(`2026-07 (prose)`) yields `2026-07-01`, never the prose. The field is structurally
incapable of carrying body text; that is why it is safe.

EVERY FIELD IS ALREADY COMPUTED. This is a PRESENTATION tier, not new computation:
`token_count`, `content_hash`, `line_range`, `source_file` are stamped at index build,
`property_count` is a `len()`, `edge_count` is a lookup in the adjacency cache the
walk already holds.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

# --- the tier vocabulary -------------------------------------------------------
TIER_DIGEST = "digest"
TIER_PAGE = "page"
TIER_FULL = "full"
TIERS = (TIER_DIGEST, TIER_PAGE, TIER_FULL)

# The digest row's ENTIRE field set. Exhaustive and closed: `digest_row` builds exactly
# these keys and the regression test asserts the produced keys equal this tuple, so a
# future "just one more field for usability" cannot land unnoticed.
DIGEST_FIELDS = (
    "id", "title", "token_count", "property_count", "edge_count",
    "source_file", "line_range", "content_hash", "timestamp",
)
# `find` spans corpora, so its rows carry the corpus that ADDRESSES the node. Routing,
# not content — a row you cannot fetch from is not a routing aid.
DIGEST_ADDRESS_FIELDS = ("corpus",)

# Keys that would make a digest answer "what does it say?". Named here so the guard is
# one list a reviewer can read, and so the regression test is a data-driven assertion
# rather than a hand-maintained list of `assert "x" not in row`.
BODY_DERIVED_KEYS = frozenset({
    "properties", "property", "props",
    "summary", "abstract", "description", "gist", "tldr",
    "excerpt", "source_excerpt", "snippet", "preview", "sample",
    "body", "content", "text", "raw", "lines", "source_lines",
    "first_lines", "head", "highlights", "key_properties", "key_points",
    "note", "notes", "rationale", "context", "detail", "details",
})

# --- ceilings ------------------------------------------------------------------
# The digest ceiling is a BLAST-RADIUS guard, not a budget. It exists so the one thing a
# digest promises — COMPLETENESS — is never quietly broken; above it the engine refuses
# and says so, naming the count, because at that size the count IS the finding.
#
# Sized from MEASUREMENT, not taste (2026-08-13, live `acme-console`): a digest
# row serializes to ~365 chars ≈ 91 tokens (`content_hash` alone is ~18 of them). Against
# the routine slices the engine actually serves — the 16-to-31-node boot slices listed on
# `DEFAULT_TOKEN_BUDGET` — 1000 rows is ~30x headroom, so nothing routine can hit it.
# A WHOLE-CORPUS digest of the 1,565-node `acme-console` deliberately does NOT
# fit: ~140k tokens of digest is not a routing aid, it is a context bomb, and the honest
# answer to "digest the entire corpus" is "that is too much, narrow it" — not a fraction.
DIGEST_MAX_NODES = 1000

# `full` has no separate number: its ceiling IS the caller's effective `token_budget`.
# A caller who names a budget has stated the context they are willing to spend; `full`
# either delivers the complete result inside it or refuses. It never silently spends a
# fraction of that budget and calls the result an answer.


class TierError(Exception):
    """A tier contract could not be honoured. Carries the response dict to return.

    Raised — not returned — so a tier violation cannot be dropped on the floor by a
    caller that forgets to check a return value. `payload` is the `success: False`
    response the tool hands back verbatim.
    """

    def __init__(self, payload: dict):
        super().__init__(payload.get("error", "tier error"))
        self.payload = payload


def resolve_tier(value: Any) -> Optional[str]:
    """Normalize the `tier` argument. `None` means LEGACY — do not change behaviour.

    Absent/empty -> None, and every legacy caller lands here: the tools branch on
    `tier is None` and run the pre-tier code path untouched. An UNKNOWN tier is an
    ERROR, never a silent fallback to legacy: a typo'd `tier="digset"` that quietly
    served the truncating path would rebuild the exact defect this layer removes.
    """
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip().lower()
        if not v:
            return None
        if v in TIERS:
            return v
    raise TierError({
        "success": False, "error": f"unknown tier {value!r}; expected one of {TIERS}",
        "reason": "unknown_tier", "tiers": list(TIERS),
    })


# --- the digest row ------------------------------------------------------------
def digest_row(node: dict, *, edge_count: int = 0,
               date_property: Optional[str] = None,
               corpus: Optional[str] = None) -> dict:
    """ONE digest row: exactly `DIGEST_FIELDS` (+ `corpus` when addressing needs it).

    Reads only index-stamped metadata. The single property-bag read is the timestamp,
    and it is normalized to `YYYY-MM-DD` or dropped to None — see the module docstring
    for why that keeps the row structurally incapable of carrying body text.
    """
    from .tools import normalize_date  # local: avoid an import cycle at module load

    props = node.get("properties") or {}
    ts = None
    if date_property:
        ts = normalize_date(props.get(date_property))
    row = {
        "id": node.get("id"),
        "title": node.get("title"),
        "token_count": node.get("token_count"),
        "property_count": len(props),
        "edge_count": int(edge_count),
        "source_file": node.get("source_file"),
        "line_range": node.get("line_range"),
        "content_hash": node.get("content_hash"),
        "timestamp": ts,
    }
    if corpus is not None:
        row["corpus"] = corpus
    return row


def digest_rows(nodes: Sequence[dict], *,
                edge_count_fn: Optional[Callable[[dict], int]] = None,
                date_property: Optional[str] = None,
                corpus_fn: Optional[Callable[[dict], Optional[str]]] = None,
                ) -> List[dict]:
    """Digest a whole result set — ALL of it. There is no offset and no budget here.

    Completeness is the tier's whole claim, so this function has no mechanism by which
    it could return a subset: the ceiling check that guards it (`assert_digestable`)
    RAISES, it does not trim.
    """
    out: List[dict] = []
    for n in nodes:
        ec = edge_count_fn(n) if edge_count_fn is not None else 0
        cid = corpus_fn(n) if corpus_fn is not None else None
        out.append(digest_row(n, edge_count=ec, date_property=date_property,
                              corpus=cid))
    return out


def assert_digestable(total: int, *, offset: int = 0, what: str = "result set") -> None:
    """Guard the digest's two invariants BEFORE any row is built.

    1. `offset` must be 0. A digest with an offset is a truncated subset with a polite
       name — the defect rebuilt one level up. Refused, loudly.
    2. `total` must be <= `DIGEST_MAX_NODES`. Above it the digest is refused rather
       than trimmed: the size is the finding.
    """
    if offset:
        raise TierError({
            "success": False, "reason": "digest_offset_rejected",
            "tier": TIER_DIGEST, "offset": offset, "total": total,
            "error": ("a digest is ALWAYS COMPLETE and therefore takes no `offset` "
                      f"(got offset={offset}). Drop `offset` for the whole {what}, or "
                      "use tier='page' when you actually want a paged slice."),
        })
    if total > DIGEST_MAX_NODES:
        raise TierError({
            "success": False, "reason": "digest_ceiling_exceeded",
            "tier": TIER_DIGEST, "total": total, "ceiling": DIGEST_MAX_NODES,
            "error": (f"digest would cover {total} nodes, over the {DIGEST_MAX_NODES}-node "
                      "ceiling. A digest is never truncated — a result this broad is the "
                      "finding. Narrow the query (prefix/status/area/max_depth/edge_types), "
                      "or page it with tier='page'."),
        })


def assert_full_fits(items: Sequence[Any], budget: int, *,
                     token_of: Callable[[Any], int],
                     what: str = "result set") -> int:
    """`full` tier ceiling: the COMPLETE result must fit the caller's budget, or ERROR.

    Returns the total token cost when it fits. Raises `TierError` when it does not —
    the tier's entire point: exceeding the ceiling errors rather than truncating, so a
    `full` response is either the whole thing or nothing at all, never a fraction that
    reads like the whole thing.
    """
    cost = sum(max(0, int(token_of(it) or 0)) for it in items)
    if cost > budget:
        raise TierError({
            "success": False, "reason": "full_ceiling_exceeded",
            "tier": TIER_FULL, "total": len(items),
            "total_token_count": cost, "token_budget": budget,
            "error": (f"tier='full' needs {cost} tokens for the complete {what} "
                      f"({len(items)} nodes) but the budget is {budget}. `full` NEVER "
                      "truncates — a partial result that looks whole is the failure this "
                      "tier exists to prevent. Raise `token_budget` to at least "
                      f"{cost}, use tier='digest' to see what is there, or tier='page' "
                      "to walk it a page at a time."),
        })
    return cost


def incident_degree_fn(fwd_adj: Dict[str, Iterable], rev_adj: Dict[str, Iterable]
                       ) -> Callable[[dict], int]:
    """`edge_count` = a node's DEDUPED incident degree in its corpus (fwd + rev).

    Corpus degree, deliberately, not degree-within-this-result: the number answers
    "how connected is this node?" — a routing signal that does not change meaning with
    the shape of the query that surfaced it. Reads the adjacency cache the walk is
    already holding, so it costs a dict lookup.
    """
    def _degree(node: dict) -> int:
        nid = node.get("id")
        seen = set()
        for _layer, edge in fwd_adj.get(nid, ()):
            seen.add(id(edge))
        for _layer, edge in rev_adj.get(nid, ()):
            seen.add(id(edge))
        return len(seen)
    return _degree
