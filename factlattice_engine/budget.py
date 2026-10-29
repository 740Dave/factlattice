"""THE token-budget slice and the truncation declaration — [ENGINE-BUDGET],
[INV-DECLARED-REACH].

ONE implementation, reused by reference by every layer that returns a COLLECTION.
It lives here, in its own leaf module with no engine imports, precisely so that the
LINT layer — which must answer with no store seated and therefore cannot import
`tools.LtsEngine` — reaches the same code QUERY reaches instead of growing a second
copy of it.

That is not tidiness. The defect this module was extracted for was a SCOPE CLAUSE:
[ENGINE-BUDGET] said "every layer that returns NODES applies a token budget", the
slicer lived as a private method on `LtsEngine`, and LINT — whose repeated payload is
called `findings`, and which never constructs an `LtsEngine` — fell out of the contract
by wording and by reach at the same time. A second spelling of "there is more" is a
second thing that can be forgotten.

THE VOCABULARY IS FIXED (`declare()` below builds it, nobody hand-rolls it):

    count          items actually carried in this response
    total_matched  items the engine found, BEFORE any bound applied
    truncated      true when count < total_matched
    cursor         the `offset` that resumes at the first omitted item; None when whole

And the rule that makes the numbers mean something: **a count describes the CORPUS, not
the RESPONSE.** Any summary/total/histogram travelling beside a bounded collection is
folded over everything found, before the slice. See [INV-DECLARED-REACH].
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple


def budget_slice(items: List[Any], offset: int, budget: int,
                 keyfn: Callable[[Any], dict]) -> Tuple[List[Any], bool, Optional[int], int]:
    """Token-budget slice ([ENGINE-BUDGET]).

    Skips `offset`, emits in order until the next item would exceed `budget` (always
    >=1 emitted so a single over-budget item still makes progress). Items are never
    split: an over-budget item is skipped WHOLE, so no consumer reasons from half of
    one. Returns `(emitted, truncated, cursor, total)` where `total` is the count
    BEFORE `offset` and the budget were applied.

    `keyfn(item)` returns a dict carrying a `token_count`; absent/zero costs 1.
    """
    total = len(items)
    emitted: List[Any] = []
    used = 0
    i = max(0, offset)
    while i < total:
        cost = int(keyfn(items[i]).get("token_count") or 1)
        if emitted and used + cost > budget:
            break
        emitted.append(items[i])
        used += cost
        i += 1
    truncated = i < total
    cursor = i if truncated else None
    return emitted, truncated, cursor, total


def resolve_budget(token_budget: Optional[Any], default: int) -> int:
    """Resolve the effective budget: an explicit per-call `token_budget` WINS, in BOTH
    directions; absent/garbage falls back to `default`.

    `default` is a DEFAULT, not a ceiling. It once clamped with `min(default, tb)`,
    which silently swallowed every request for MORE: a caller that explicitly asked for
    600000 over a 541,653-token corpus got the 8000-token default's 36 nodes back,
    truncated, with no signal its argument had been ignored ([DEF-BUDGET-TOO-SMALL]).
    Shared so that a layer added later inherits that ruling instead of re-deciding it.
    """
    if token_budget is None:
        return default
    try:
        tb = int(token_budget)
    except (TypeError, ValueError):
        return default
    return max(1, tb)


def declare(emitted: List[Any], truncated: bool, cursor: Optional[int],
            total: int) -> Dict[str, Any]:
    """The [INV-DECLARED-REACH] fields for a bounded collection, as a dict to merge
    into a response. Built in one place so no layer can spell the contract its own way
    or emit three of the four fields and call it declared."""
    return {"count": len(emitted), "total_matched": total,
            "truncated": truncated, "cursor": cursor}


# THE estimator, imported not re-derived: `Node.token_count` — the number this module's
# slicer spends against — is produced by exactly this function in the parser. A twin here
# would agree until the day it didn't, and nothing would notice.
from factlattice import token_estimate  # noqa: E402  (re-exported for budget callers)

__all__ = ["budget_slice", "declare", "resolve_budget", "token_estimate"]
