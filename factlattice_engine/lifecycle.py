"""The LIFECYCLE TOKEN of a `status` value — one tokenizer, two consumers, no drift.

WHY THIS MODULE EXISTS
----------------------
`lts_find(status=X)` matched the `status` property by EXACT STRING. Corpus status
values are not exact strings: they are a lifecycle token followed, very often, by
free-text qualifier prose —

    proposed
    proposed — awaiting ratify
    proposed — awaiting ratify (BLOCKS slice 2's scene wiring; does NOT block slices 0 or 1)
    operational (2026-07-07)
    resolved; amended 2026-07-26 (freshest-source de-fork machine-green …)
    open — blocked on a live-vehicle candump (the maintainer + the Fiat 500e)

so `status=proposed` returned only the bare-`proposed` nodes and silently omitted every
qualified one. That is not cosmetic: the session-bootstrap read implements its DISPATCH
GATE (no spec ships to a builder while an unratified `[DD-*]` remains in it) as exactly
this query, and the omitted nodes included one whose own status string reads
"BLOCKS slice 2's scene wiring". A filter that returns 8 looks exactly like a filter
that works.

THE RULE
--------
`lifecycle_token(value)` derives the leading LOWERCASE-HYPHEN word, and only when it is
followed by a recognised BOUNDARY. `status_matches(raw, query)` then matches when

  (a) the raw string equals the query          — today's behaviour, preserved exactly; or
  (b) the query is ITSELF a bare lifecycle token AND the raw value's token equals it.

Condition (b)'s "query is itself a bare token" clause is deliberate and load-bearing: a
BARE query (`status=proposed`) asks a LIFECYCLE question and gets the whole lifecycle; a
QUALIFIED query (`status="proposed — awaiting ratify"`) asks an EXACT question and keeps
its precision instead of silently widening to 18 nodes.

⛔ THE RAW STRING IS NEVER TOUCHED. This module changes a FILTER, not data. The
qualifier prose is real content that readers depend on ("BLOCKS slice 2's scene wiring"
is the whole message), and `properties`/`status` come back off the index verbatim.

BOUNDARIES — derived from the real corpus values above, not invented
-------------------------------------------------------------------
A token is accepted only when the character right after it is end-of-string or one of
`_BOUNDARY_CHARS`: whitespace (`proposed — …`, `operational (…)`, `open — …`), `;`
(`resolved; amended …`), `.` , `,` , `:` , `(`/`)`/`[`/`]` , and the em/en dashes
(`—`/`–`) for the tight `proposed—awaiting` spelling.

DELIBERATELY *NOT* NORMALIZED — "no token" beats a WRONG token
--------------------------------------------------------------
`-` is part of a token, never a boundary. So `in-progress` and
`resolved-pending-live-confirmation` normalize to THEMSELVES; the latter is a distinct
lifecycle and must NOT collapse into `resolved`.

These shapes yield NO token at all, on purpose — they were never queryable, and the
brief for this fix is explicit that they need not become so:

  * anything not starting with a lowercase ASCII letter:
      `**operational (2026-07-07)**`   (leading bold marker)
      `OPEN — flagged for the architect's ratify; …`   (upper-case lifecycle)
      `UNCONFIRMED. Its author has explicitly downgraded …`
      `Skia tier = **OPERATIONAL 2026-07-07** — …`     (leading prose, capitalised)
  * a lowercase word followed by a NON-boundary character:
      `spec'd; build queued`      -> `spec` + `'`  -> no token
      `classic-CYD pass = …`      -> `classic` + `-` (the `-CYD` group fails on the
                                     upper case, and `-` is not a boundary) -> no token

  The alternative — reporting `resolved` for `resolved-pending-live-confirmation`, or
  `spec` for `spec'd` — would answer a lifecycle question WRONGLY, which is worse than
  not answering it.

KNOWN RESIDUAL (visible, not hidden): a status authored as lowercase PROSE tokens to its
first word — `query + heartbeat GLASS-GREEN live 2026-07-19; …` -> `query`;
`hook landed (2026-07-07); …` -> `hook`. Syntax alone cannot separate that from a domain
whose lifecycle really is named `query`. This is precisely why part (2) of the fix
exists: lint rule **V11** (`lint/profile_rules.py`) flags every status that is not a
BARE lifecycle token, so the corpus drift this filter tolerates stays VISIBLE instead of
being silently absorbed one layer down. Widening the filter WITHOUT the lint rule would
just move the invisibility, not remove it.
"""

from __future__ import annotations

import re
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

# --- THE LIFECYCLE KEY SET ([FL-RECALL-TWO-VOCABULARIES]) --------------------------
# ⛔ A LIFECYCLE QUERY SWEEPS THE UNION AND SAYS WHICH KEYS IT CONSULTED.
#
# Measured 2026-08-13 on the Acme corpus: `lts_find(status="contingent")` returned ONE
# node, keyed on `status:`, while `grep 'lifecycle-position:[[:space:]]*contingent'` over
# the SAME corpus returned THREE, in a different file. THE OVERLAP WAS ZERO — they are
# disjoint sets, and the caller reads "1 contingent decision" as an answer.
#
# A contingent sweep exists to find UN-DISCHARGED OBLIGATIONS, so missing an OLD one is
# worse than missing a new one: age makes an open obligation more urgent, not less. That
# makes the lifecycle filter the single worst surface in the engine to silently halve.
#
# `status` is the node's TOP-LEVEL field (the parser promotes `properties["status"]`);
# the other two are ordinary properties. `lifecycle-position` is the authored form across
# every corpus in the ecosystem, `lifecycle_position` the underscore spelling a domain
# might author instead — both are checked by V6 (`lint/profile_rules.py`), which imports
# these names from HERE rather than restating them, so the rule that flags drift and the
# filter that spans it can never disagree about what a lifecycle key is.
LIFECYCLE_STATUS_KEY = "status"
LIFECYCLE_PROPERTY = "lifecycle-position"
LIFECYCLE_PROPERTY_ALT = "lifecycle_position"
# Order is the REPORTED order in `keys_consulted`, and it is search order too: `status`
# first because it is the field the engine has always matched on, so a caller comparing
# an old answer to a new one sees the old answer's key named first.
LIFECYCLE_KEYS: Tuple[str, ...] = (
    LIFECYCLE_STATUS_KEY, LIFECYCLE_PROPERTY, LIFECYCLE_PROPERTY_ALT,
)

# A lifecycle token: lowercase ASCII words joined by single hyphens. `proposed`,
# `in-progress`, `resolved-pending-live-confirmation`.
_TOKEN_RE = re.compile(r"[a-z]+(?:-[a-z]+)*")

# The character classes that may legally TERMINATE a lifecycle token. Every one of these
# is present in the measured corpus values; see the module docstring.
_BOUNDARY_CHARS: FrozenSet[str] = frozenset(
    " \t\n\r\f\v"      # `proposed — awaiting ratify`, `operational (2026-07-07)`
    ";"                # `resolved; amended 2026-07-26 (…)`
    ".,:"              # `resolved.`, `open, pending`, `open: blocked on …`
    "()[]"             # a tight `open(target)` / `settled([DEC-…])`
    "—–"     # em dash / en dash, tight: `proposed—awaiting ratify`
)


def lifecycle_token(value: object) -> Optional[str]:
    """The normalized lifecycle token of a status value, or None when there isn't one.

    Returns None (never guesses) for: a non-string; an empty/whitespace value; a value
    that does not BEGIN with a lowercase ASCII letter; and a leading lowercase word that
    is not followed by a boundary character. See the module docstring for the exact
    boundary set and the shapes deliberately left untokenized.

    >>> lifecycle_token("proposed")
    'proposed'
    >>> lifecycle_token("proposed — awaiting ratify")
    'proposed'
    >>> lifecycle_token("operational (2026-07-07)")
    'operational'
    >>> lifecycle_token("resolved-pending-live-confirmation")   # NOT 'resolved'
    'resolved-pending-live-confirmation'
    >>> lifecycle_token("**operational (2026-07-07)**") is None
    True
    >>> lifecycle_token("spec'd; build queued") is None
    True
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    m = _TOKEN_RE.match(text)
    if m is None:                      # did not start with a lowercase ASCII letter
        return None
    token = m.group(0)
    rest = text[m.end():]
    if rest and rest[0] not in _BOUNDARY_CHARS:
        return None                    # `spec'd`, `classic-CYD` — no token beats a wrong one
    return token


def is_bare_lifecycle_token(value: object) -> bool:
    """True when a status value is EXACTLY a lifecycle token and nothing else.

    The predicate V11 lints on, and the same predicate `status_matches` uses to decide
    whether a QUERY is asking a lifecycle question or an exact-string question — one
    definition, so the rule that flags drift and the filter that tolerates it can never
    disagree about what "a bare token" means.
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(text) and lifecycle_token(text) == text


def status_matches(raw_status: object, query: object) -> bool:
    """The `lts_find(status=…)` predicate: raw-string equality OR lifecycle-token equality.

    `raw_status` is the node's status verbatim off the index; `query` is the caller's
    filter. Exact-string matching is preserved unchanged — this only ADDS matches, and
    only when the caller asked a bare-token (lifecycle) question. Returns False for a
    node with no status, exactly as the old `node.get("status") != status` did.
    """
    if raw_status == query:
        return True                     # today's behaviour, first and unconditional
    if not is_bare_lifecycle_token(query):
        return False                    # a QUALIFIED query stays exact-match, on purpose
    return lifecycle_token(raw_status) == str(query).strip()


# ---------------------------------------------------------------------------------
# THE UNION SWEEP ([FL-RECALL-TWO-VOCABULARIES] / [FL-RECALL-PROPOSALS] item 2)
# ---------------------------------------------------------------------------------

def _values(raw: object) -> List[object]:
    """A property value as a list of scalars. A repeated key accumulates into a list
    (parser `_maybe_multivalue`), and a lifecycle sweep that read only the first entry
    would reproduce the halving one level down."""
    if isinstance(raw, (list, tuple)):
        return [v for v in raw]
    return [] if raw is None else [raw]


def lifecycle_values(node: Dict[str, Any]) -> Dict[str, List[object]]:
    """`{key: [values]}` for every lifecycle-bearing key this node actually carries.

    Reads the node's TOP-LEVEL `status` and its `lifecycle-position` /
    `lifecycle_position` properties. A key the node does not carry is absent from the
    result — the caller reports the keys it CONSULTED (all of them), not the keys that
    happened to be populated, and conflating those two is how a sweep claims a reach it
    did not have.
    """
    props = node.get("properties") or {}
    out: Dict[str, List[object]] = {}
    for key in LIFECYCLE_KEYS:
        raw = node.get("status") if key == LIFECYCLE_STATUS_KEY else props.get(key)
        vals = [v for v in _values(raw) if v is not None]
        if vals:
            out[key] = vals
    return out


def lifecycle_matches(node: Dict[str, Any], query: object) -> Optional[str]:
    """The FIRST lifecycle key on `node` whose value matches `query`, or None.

    ⛔ THE UNION, NOT `status` ALONE. Each key is matched through `status_matches`, so
    the bare-token widening and the qualified-string precision that `status=` already had
    apply identically to `lifecycle-position` — one predicate, three keys, no second
    spelling of "does this value mean `contingent`".

    Returns the matching KEY (not a bool) so the response can say WHICH vocabulary
    answered. A caller that gets `matched_via: lifecycle-position` on a corpus it thought
    was keyed on `status` has just been handed the curation finding for free.
    """
    for key, values in lifecycle_values(node).items():
        for value in values:
            if status_matches(value, query):
                return key
    return None
