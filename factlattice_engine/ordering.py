"""[QUERY-LIMIT-ORDER] — `limit` and `order_by`, one pipeline stage each, shared by
`find` and `traverse` ([SPEC-COVERAGE-SCOPE]).

ONE implementation, reused by reference, for the same reason `budget.py` is its own
leaf module: the ordering/limit contract must not be re-derived per tool, or the two
copies drift the day a `field`'s comparability rule needs to change.

THE PIPELINE, per [SPEC-ORDER-OF-OPERATIONS] — filter, then order, then limit:

    match set  = every node passing the tool's own filters             (unchanged)
    ordered    = match set sorted by order_by  (or its existing default order)
    windowed   = ordered[:limit]               (or all of `ordered`)

`order_and_window` performs the last two stages. It never touches the FIRST — the
caller passes in an already-filtered `items` sequence and this module does not know
or care how it was produced.

NULL/INCOMPARABLE HANDLING ([SPEC-NULL-ORDERING]): a node whose `order_by.field` is
missing, or whose value's TYPE does not match the field's DOMINANT type across the
match set (dates compare to dates, numbers to numbers; a stray string in a numeric
field is not coerced), falls into one UNORDERABLE bucket. That bucket sorts AFTER
every comparable node regardless of `direction`, and is itself ordered by id
ascending — the same deterministic tiebreak applied to genuine ties among comparable
values, using a two-pass STABLE sort (sort by id first, then by value; the stable
sort preserves the id-ascending order for ties in value without a Python comparator
that would have to hand-roll the same tiebreak).
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# The two recognized `direction` values. Anything else is a caller error, never
# silently coerced ([PARAM-ORDER-BY]).
DIRECTIONS = ("asc", "desc")


class OrderError(Exception):
    """`order_by` could not be resolved. Carries the response dict to return verbatim
    — raised, not returned, for the same reason `tiers.TierError` is: a caller that
    forgets to check a return value must not be able to drop the refusal on the floor.
    """

    def __init__(self, payload: dict):
        super().__init__(payload.get("error", "order_by error"))
        self.payload = payload


def resolve_order_by(order_by: Any, default_field: Optional[str]) -> Optional[Dict[str, str]]:
    """Normalize the `order_by` argument to `{field, direction}`, or `None`.

    `None`/absent -> `None`: the caller did not ask for a re-order; the existing
    default order stands ([SPEC-BACKWARD-COMPAT]).
    `"recency"` -> sugar for `{field: default_field, direction: "desc"}`
    ([PARAM-ORDER-BY] — MUST be supported as the literal string).
    `{field, direction}` -> validated; `direction` defaults to `"desc"` when omitted.
    An unrecognized `direction` is `invalid_order_direction`, refused loudly, never
    silently coerced. Anything else (wrong type, empty field) is `invalid_order_by`.
    """
    if order_by is None:
        return None
    if isinstance(order_by, str):
        v = order_by.strip().lower()
        if v == "recency":
            return {"field": default_field, "direction": "desc"}
        raise OrderError({
            "success": False, "reason": "invalid_order_by",
            "error": (f"order_by={order_by!r} is not recognized; the only accepted "
                      "string form is \"recency\". Use {field, direction} for any "
                      "other property."),
        })
    if isinstance(order_by, dict):
        field = order_by.get("field")
        if not field or not isinstance(field, str):
            raise OrderError({
                "success": False, "reason": "invalid_order_by",
                "error": f"order_by={order_by!r} needs a non-empty string `field`.",
            })
        direction = order_by.get("direction", "desc")
        if not isinstance(direction, str) or direction.strip().lower() not in DIRECTIONS:
            raise OrderError({
                "success": False, "reason": "invalid_order_direction",
                "error": (f"order_by.direction={direction!r} is not recognized; "
                          f"expected one of {DIRECTIONS}. Never silently coerced to "
                          "\"desc\" — state the direction you mean."),
            })
        return {"field": field, "direction": direction.strip().lower()}
    raise OrderError({
        "success": False, "reason": "invalid_order_by",
        "error": (f"order_by={order_by!r} must be \"recency\" or a "
                  "{field, direction} mapping."),
    })


def _classify(value: Any) -> Tuple[Optional[str], Any]:
    """`(type, comparable_value)` for one raw property value, or `(None, None)` when
    the value cannot participate in any ordering (missing, list/mapping, bool, or a
    string that is not a recognizable date).

    Booleans are deliberately excluded from the numeric type (`isinstance(True, int)`
    is `True` in Python; a `- flag: true` property must never silently sort as `1`).
    """
    from .tools import normalize_date  # local: avoid an import cycle at module load

    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        return "number", float(value)
    if isinstance(value, str):
        d = normalize_date(value)
        if d is not None:
            return "date", d
        return "string", value
    return None, None


def order_and_window(items: Sequence[Any], *, order_by: Optional[Dict[str, str]],
                     limit: Optional[int],
                     get_id: Callable[[Any], Any],
                     get_properties: Callable[[Any], dict],
                     ) -> Tuple[List[Any], Optional[Dict[str, Any]]]:
    """Apply `order_by` (if any) then `limit` (if any) to an already-filtered `items`.

    Returns `(windowed, order_applied)`. `order_applied` is `None` when `order_by` was
    not supplied — the caller uses that to decide whether to add the metadata field at
    all ([SPEC-RESULT-METADATA]: omitted entirely, not null, when unused).

    `order_applied["unorderable_count"]` is scoped to the RETURNED (post-`limit`)
    window, not the whole match set — "these are the 30 newest" vs. "12 of THESE had
    no date" ([SPEC-NULL-ORDERING]) describes the response the caller is holding, not
    the corpus. Comparable nodes always sort before unorderable ones, so this is
    `max(0, len(windowed) - comparable_count)`.
    """
    if order_by is None:
        ordered = list(items)
        order_applied = None
    else:
        field = order_by["field"]
        direction = order_by["direction"]
        reverse = direction == "desc"

        typed: List[Tuple[Optional[str], Any, Any]] = []
        for it in items:
            props = get_properties(it) or {}
            vtype, val = _classify(props.get(field))
            typed.append((vtype, val, it))

        non_none_types = [t for t, _, _ in typed if t is not None]
        dominant = Counter(non_none_types).most_common(1)[0][0] if non_none_types else None

        comparable = [(val, it) for (t, val, it) in typed
                     if dominant is not None and t == dominant]
        unorderable = [it for (t, _val, it) in typed
                       if not (dominant is not None and t == dominant)]

        # Two-pass STABLE sort: id ascending first, then value — the second sort's
        # ties resolve in the first sort's order, which IS the id-ascending tiebreak
        # [SPEC-NULL-ORDERING] requires for genuine ties among comparable values.
        comparable.sort(key=lambda pair: get_id(pair[1]))
        comparable.sort(key=lambda pair: pair[0], reverse=reverse)
        unorderable.sort(key=get_id)

        ordered = [it for _val, it in comparable] + unorderable
        order_applied = {"field": field, "direction": direction,
                         "_comparable_count": len(comparable)}  # trimmed below

    windowed = ordered[: limit] if limit is not None else ordered

    if order_applied is not None:
        comparable_count = order_applied.pop("_comparable_count")
        comparable_in_window = min(len(windowed), comparable_count)
        order_applied["unorderable_count"] = len(windowed) - comparable_in_window

    return windowed, order_applied


def bound_reason(cursor: Optional[int]) -> str:
    """Why more of the WINDOW might still be fetchable ([SPEC-RESULT-METADATA]):
    `"token_budget"` when `cursor` (the tier's own OFFSET-AWARE "is there more of
    THIS WINDOW to page to" signal — the same one `next_offset` already carries) is
    not `None`; `"none"` once the window is exhausted (digest and full pass `cursor=
    None` unconditionally, so they are always `"none"` — either complete over the
    window or an error, never a fraction of it).

    ⛔ Deliberately NOT `count < windowed_total`: that comparison is not
    offset-aware — it is only correct at `offset=0`. On the LAST page of a
    limited window (`offset=20, count=10, windowed_total=30`) it would read
    `10 < 30` and falsely claim "more to fetch" even though `cursor is None`
    already (correctly) says the window is exhausted. `cursor` is the one signal
    `_budget_slice` already computes correctly at every offset — reuse it, don't
    re-derive a second, offset-blind one.
    """
    return "token_budget" if cursor is not None else "none"


def result_metadata(*, limit: Optional[int], order_applied: Optional[Dict[str, Any]],
                    total_matched: int, windowed_len: int, count: int,
                    cursor: Optional[int]) -> Dict[str, Any]:
    """The four additive [SPEC-RESULT-METADATA] fields, built in ONE place so no tool
    spells the contract differently or omits a field inconsistently.

    Returns `{}` — nothing to merge — when neither `limit` nor `order_applied` was
    supplied, which is what keeps an unmodified caller's response byte-identical
    ([SPEC-BACKWARD-COMPAT]: the four fields MUST be omitted entirely, not present
    with a null value).

    `cursor` is the tier's own window-relative continuation cursor (`None` once the
    window is exhausted) — see `bound_reason` for why `bound_reason` is derived from
    it rather than from `count`/`windowed_len` directly.
    """
    out: Dict[str, Any] = {}
    if limit is None and order_applied is None:
        return out
    if limit is not None:
        out["limit_applied"] = limit
        out["windowed_total"] = windowed_len
    if order_applied is not None:
        out["order_applied"] = order_applied
    out["bound_reason"] = bound_reason(cursor)
    return out
