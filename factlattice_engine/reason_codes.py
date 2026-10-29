"""[TOOL-REASON-IS-A-FIXED-CODE] the closed registry of reply `reason` values.

THE RULE (re-ruled 2026-10-08, ruling (c)); the property that matters is SAFETY, not form:
  * the TOP-LEVEL `reason` of a tool reply, and the `reason` of any refusal or fault block (a dict that says
    `success: false`, or carries `error_class` / `live_detail`), is a FIXED CODE matching CODE_RE. The one exception
    is the typed writers' `refused: true` reply, whose `reason` is a pinned sentence and whose code is `evidence.refusal`;
  * a NESTED INFORMATIONAL `reason` is either such a code OR one of the module-level CONSTANT sentences registered in
    `info_reasons()`: each is path-free by construction and is never built from caller input, a path, a count, a hash or
    an exception message. The registry is closed: a sentence not in it is a defect. The EVIDENCE is the RUNTIME battery over real replies
    (tests/test_tool_reason_codes.py); the static scan (tests/test_reason_sites.py) is a TRIPWIRE only (literal, f-string, format-call and
    concatenation forms; it cannot see a variable built elsewhere, a subscript assignment, a conditional expression, a subscript value or
    `setdefault`).
Anything that varies (a path, an exception message, a count) goes in a separately named LIVE-ONLY field
(`live_detail`, with its NEVER-persist note) or its own field, never in `reason`.
"""
from __future__ import annotations

import re

CODE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")

# the typed writers: the only tools whose `refused: true` reply may carry a sentence in `reason`
TYPED_WRITERS = frozenset({"lts_add_node", "lts_add_property", "lts_repair_property"})

NAMED_CORPUS_REASON = ("suggestions need a named corpus (they never fan out); repeat with corpus=... "
                       "or call lts_semantic_find")
WITHHELD_HOP_REASON = "edge into a private corpus; name it explicitly to follow"
ENDPOINT_NOT_IN_NODE_SET_REASON = "endpoint not in node set"
UNREACHABLE_KEY_REASON = ("registered via profile.property_edges but UNREACHABLE from "
                          "every production parse entry point checked (see `wiring`) — "
                          "an engine defect, not an authoring one: this key mints ZERO "
                          "edges on any served graph regardless of how the corpus is "
                          "authored")


def is_code(value: object) -> bool:
    return isinstance(value, str) and CODE_RE.fullmatch(value) is not None


def info_reasons() -> frozenset:
    """Every registered constant informational sentence. Imports lazily (the owners import this module)."""
    from .privacy import PRIVATE_REASON
    from .property_loss import PROPERTY_LOSS_REASONS
    from .selfheal import HEAL_REASONS
    return frozenset({NAMED_CORPUS_REASON, WITHHELD_HOP_REASON, ENDPOINT_NOT_IN_NODE_SET_REASON,
                      UNREACHABLE_KEY_REASON, PRIVATE_REASON, *PROPERTY_LOSS_REASONS, *HEAL_REASONS})
