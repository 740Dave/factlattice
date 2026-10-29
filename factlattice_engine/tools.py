"""The lts-engine query surface — 4 read-only tools over the engine, no security layer.

Four kept methods — BFS traversal, the taxonomy/prefix/status/text predicate,
source-excerpt reading, token-budget slicing (token budgeting is context-hygiene for
an LLM consumer, not a security feature):

    lts_find          find by taxonomy/prefix/status/text  (+ since/until time filter)
                      NOTE `status` is LIFECYCLE-aware, not raw-equal — see `lifecycle.py`
                      and `_node_matches` below.
                      NOTE `text` is STRUCTURE-ONLY (id/title/string property values); it
                      declares that scope in `extent.scope_note` and routes onward.
    lts_find_in_prose find a term in node BODY TEXT — the scope `lts_find` cannot see
                      ([FL-PROSE-BLIND]). Separate tool, not a widened matcher: see
                      `find_in_prose` for why the split is the design and not a compromise.
    lts_node          node + per-layer edges + source excerpt
    lts_traverse      bounded typed-edge walk               (+ since/until time filter)
    lts_capabilities  the capability surface around a topic
    lts_flow          [LEC-RENDER] walk a flow subgraph -> Mermaid + the structured walk
    lts_reverse       [LTS-REVERSE-LOOKUP] target -> referrers, the completeness path
                      `lts_find`/`lts_node`'s own reverse-edge views cannot answer (a
                      cross-corpus edge into this node, or a non-minting property-value
                      `[ID]` reference); see `reverse_index.py`. `include_reverse_digest`
                      on `find`/`node` embeds a CAPPED, non-complete preview of it.

There is NO identity / ACL / leak-free ceremony: a missing corpus is a plain
not-found, a routed edge into an absent partition returns a plain "unresolved", and
there is no provenance tool — the engine simply reads the same filesystem the
operator edits.

A `since`/`until` time filter runs over a configured date property (default
`decided-in-session`) on find/traverse, and `list_corpora` enumerates the registered
partitions.

`dispatch(name, arguments) -> dict` is transport-agnostic: it has ZERO knowledge of
MCP/stdio. `server.py` wraps it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import io
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from . import querylog
from . import query_surface as _qs
from factlattice import registry as _registry
from factlattice.registry import RegistryError
from factlattice.hashing import CORPUS_FILE_CODES, CorpusFileError, safe_read_bytes, safe_read_text

from .caution import caution_for
from .budget import budget_slice, declare, resolve_budget
from .extent import declare_extent, unknown_corpus
from .reverse_index import ReverseIndex, build_domain_index, snapshot_key
from .lifecycle import (
    LIFECYCLE_KEYS, is_bare_lifecycle_token, lifecycle_matches, lifecycle_token,
    status_matches,
)
from .reason_codes import NAMED_CORPUS_REASON, WITHHELD_HOP_REASON
from .privacy import PRIVATE_NOTE, PRIVATE_REASON, is_private_partition, private_reason
from .ordering import OrderError, order_and_window, resolve_order_by, result_metadata
from .store import FreshnessResult, LtsStore, Partition
from .router import AclCheck, AllowAllAcl, Router, StalePartitionError
from .tiers import (
    TIER_DIGEST, TIER_FULL, TIER_PAGE, TierError, assert_digestable,
    assert_full_fits, digest_row, digest_rows, incident_degree_fn, resolve_tier,
)

logger = logging.getLogger(__name__)

# THE single origin of the default response budget. Every other site — the engine
# constructor, the MCP tool schemas in `server.py` — REFERENCES this name; nothing
# restates the number. A value restated in N places is how a caller ends up reading an
# advertised default the engine does not actually apply.
#
# Sized from MEASUREMENT, not taste. The boot-read slices of the live
# `acme-console` / `acme-notes` corpora, measured with
# `lts_aggregate op=sum attr=token_count` (which is budget-independent):
#     inventory (prefix=PROJECT)          21 nodes    4,965 tokens
#     warts (corpus=acme-notes)   16 nodes    8,032 tokens
#     dispatch gate (status=proposed)     18 nodes    9,708 tokens
#     principles (prefix=PRINCIPLE)       31 nodes    7,199 tokens
#     the WHOLE acme-console    1,471 nodes  541,653 tokens
# The old default of 8000 sat BELOW two of the four routine slices — the warts slice
# overran it by 32 tokens (0.4%) — so `find`'s honest truncation declaration fired on
# every boot and any caller deriving a statistic from `nodes` got a wrong answer that
# looked right. 32768 clears every boot slice with real headroom.
#
# It is deliberately NOT unlimited: at 541,653 tokens for one corpus, an unbounded
# `find` is a worse failure than truncation — the query SUCCEEDS and destroys the
# caller's context. The bound stays; only its size was wrong.
DEFAULT_TOKEN_BUDGET = 32768
MAX_EXCERPT_CHARS = 4000
# The corpus date-property lts-engine reads for the since/until filter. A decision
# node might carry `- decided-in-session: 2026-07-20 (c14)`; the leading ISO date
# token is what the filter compares. Configurable per LtsEngine instance / per query.
DEFAULT_DATE_PROPERTY = "decided-in-session"
# [SEM-SESSION-PREFACE] shown before the FIRST semantic results of a session (one engine
# process = one MCP client session), never again in that process (the maintainer ruling).
SEMANTIC_PREFACE = ("Semantic suggestions are leads, not answers. Confirm one with a targeted exact "
                    "query (lts_node on its id, or lts_find) before relying on it.")

# --- [ENH-1] robust date-property normalization --------------------------------
# The corpus's date values are inconsistent in the wild: full `2026-07-20`, a trailing
# `(...)` session/commit marker (`2026-07-20 (c14)`), a bare month `2026-07 (prose)`,
# or a bare year. `normalize_date` extracts a COMPARABLE `YYYY-MM-DD` from any of
# these (lexicographic ISO compare is the ordering the rest of the engine relies on),
# or None when the value is unparseable — an excluded-from-the-filter outcome, never
# a crash. DOCUMENTED CONVENTION: a partial `YYYY-MM`/`YYYY` normalizes to the 1st of
# the month / Jan 1st of that year (the earliest instant the partial value could mean)
# — a `since` bound over a `YYYY-MM` node is therefore conservative (includes it as
# early as possible); flagged for the maintainer if a different convention (e.g. "last day of
# the month" for `until` bounds) is wanted instead.
_TRAILING_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")
_ISO_LEADING_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")


def normalize_date(value: object) -> Optional[str]:
    """Extract a comparable `YYYY-MM-DD` from a corpus date-property value.

    Handles (whitespace-tolerant): full `2026-07-20`; `2026-07-20 (c14)` (trailing
    `(...)` marker stripped); `2026-07` / `2026-07 (prose)` (partial month, ->
    1st-of-month); bare `2026`(-> Jan 1st). Returns None for anything else
    (unparseable garbage, non-strings) — the caller EXCLUDES such a node from a
    date-filtered query rather than crashing or guessing.
    """
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    v = _TRAILING_PAREN_RE.sub("", v).strip()
    m = _ISO_LEADING_RE.match(v)
    if not m:
        return None
    year, month, day = m.groups()
    if day:
        return f"{year}-{month}-{day}"
    if month:
        return f"{year}-{month}-01"
    return f"{year}-01-01"


def _resolve_limit(limit: Any) -> Tuple[Optional[int], Optional[dict]]:
    """`limit` -> `(resolved_int_or_None, error_payload_or_None)` ([PARAM-LIMIT]).

    `None`/absent -> `(None, None)`: unbounded, today's behavior, byte-identical
    ([INV-LIMIT-CALLER-SET] — the engine substitutes NOTHING for an absent limit).
    A non-negative integer -> `(int, None)`. `limit=0` is legal (an empty result with
    `total_matched` still reporting the true count). Anything else is a caller error,
    refused loudly rather than silently coerced or ignored — P3 (gap-analyzer):
    `bool` is a Python `int` subclass and a whole-number `float` would otherwise
    truncate SILENTLY (`3.7` -> `3` with no signal a fraction was dropped), and this
    feature's whole ethos is "never silently coerce" ([PARAM-ORDER-BY]'s identical
    stance on `direction`) — so both are refused rather than accepted by accident.
    """
    if limit is None:
        return None, None
    if isinstance(limit, bool):
        return None, {"success": False, "reason": "invalid_limit",
                      "error": f"limit={limit!r} must be a non-negative integer, "
                                "not a bool."}
    if isinstance(limit, float) and not limit.is_integer():
        return None, {"success": False, "reason": "invalid_limit",
                      "error": (f"limit={limit!r} must be a non-negative integer; "
                                "a non-whole float is never silently truncated.")}
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return None, {"success": False, "reason": "invalid_limit",
                      "error": f"limit={limit!r} must be a non-negative integer."}
    if n < 0:
        return None, {"success": False, "reason": "invalid_limit",
                      "error": f"limit={limit!r} must be >= 0."}
    return n, None


def _not_found(corpus: str, known=(), private=()) -> dict:
    """Not-found for a nonexistent corpus — WITH the loaded set and a did-you-mean.

    Still no leak-free ceremony (there is no hostile other-tenant to hide corpus
    existence from), and now no ambiguity either: [FL-RECALL-ZERO-IS-AMBIGUOUS] state-2 is
    "the corpus id is wrong or misspelled — a wrong corpus id and a missing corpus PRESENT
    IDENTICALLY", and a bare `corpus not found` was the whole of that identity. See
    `extent.unknown_corpus`.
    """
    return unknown_corpus(corpus, known, private=private)


PRIVATE_FAILED_REASON = "private-corpus-failed"      # the anonymous `corpora_skipped` entry for a failed PRIVATE corpus (count only)


def _corpus_file_refusal(corpus: Optional[str], failed: dict) -> dict:
    """The reply for a corpus whose build failed LOUD on a member `.lts` ([ISSUE-NONREGULAR-MEMBER]).

    `reason` is the FIXED CODE (`corpus-file-not-regular|broken-link|unreadable`); the root-relative path rides in `path`.
    The corpus is not served (never a partial view), the registry is fine (this is NOT `registry-not-found`), and the
    other corpora are unaffected. `error` begins with the exception class and carries no absolute path."""
    code, rel = failed["code"], failed["path"]
    if corpus is None:       # a path-mode tool (lint, preview of a directory, the typed writers, property_loss): no corpus id
        return {"success": False, "reason": code, "error_class": "CorpusFileError", "path": rel,
                "error": f"CorpusFileError: {code} at {rel!r} (relative to the directory read). Nothing was read past it "
                         "(no partial view). Fix or remove that file."}
    return {"success": False, "reason": code, "error_class": "CorpusFileError", "corpus": corpus, "path": rel,
            "error": f"CorpusFileError: corpus {corpus!r} could not be built: {code} at {rel!r} (root-relative). "
                     "The corpus is NOT served (no partial view); other corpora are unaffected. Fix or remove that file and "
                     "restart or re-register the corpus."}


def _writer_target_error(path) -> Optional["CorpusFileError"]:
    """The corpus-file-* condition (if any) of the file a typed writer was asked to edit. A path that does not exist, or a
    directory, is NOT one: the writer's own message stands for those. This pre-check sits OUTSIDE the writers' own coded
    exception net, so it must never raise: ANY other failure (a NUL in the path, a non-string, an injected fault) returns None
    and the writer's own handling, which codes every exception, takes over."""
    try:
        if not isinstance(path, str) or not path:
            return None
        p = os.path.expanduser(path)
        base = os.path.basename(p.rstrip("/")) or "."
        from factlattice.hashing import check_member
        try:
            check_member(p, base)
        except CorpusFileError as exc:
            if exc.code == "corpus-file-not-regular" and os.path.isdir(p):
                return None
            return exc
        if os.path.isfile(p) and not os.access(p, os.R_OK):
            return CorpusFileError("corpus-file-unreadable", base)
    except Exception:  # noqa: BLE001 — see the docstring
        return None
    return None


def _writer_corpus_file_refusal(_auth, exc: "CorpusFileError") -> dict:
    """A typed writer's `refused: true` reply for a corpus-file-* condition: the usual refusal shape, with `reason` = the code,
    `error_class`, `evidence.refusal` = the code (the query log reads it there) and a basename-only `path`."""
    r = _auth._refusal(_auth.EditRefused(
        f"CorpusFileError: {exc.code}: {exc.rel_path}. Nothing written.", evidence={"refusal": exc.code}), path=exc.rel_path)
    r["reason"], r["error_class"], r["path"] = exc.code, "CorpusFileError", exc.rel_path
    r.setdefault("evidence", {})["refusal"] = exc.code
    return r


def _coded(exc: "CorpusFileError") -> dict:
    return _corpus_file_refusal(None, {"code": exc.code, "path": exc.rel_path})


def _stale_marker(fr: FreshnessResult, heal=None) -> dict:
    """A LOUD query-time staleness fail ([INV-LTS-FRESH]) — never serve old data.

    [LEC-SELF-HEAL]: reaching this marker now means the engine TRIED to heal and could
    not (or deliberately declined to retry a rebuild that just failed). `heal` carries the
    `HealOutcome` so the refusal explains itself instead of just saying "go away".
    """
    marker = {
        "success": False, "stale": True, "corpus": fr.corpus_id,
        "index_hash": fr.index_hash, "live_hash": fr.live_hash, "reason": fr.reason,
        "error": (f"STALE: the lts-engine index diverges from the live corpus content "
                  f"(corpus={fr.corpus_id}); rebuild the partition "
                  f"(python -m factlattice_engine.build). [INV-LTS-FRESH]"),
    }
    if getattr(fr, "detail", None):
        marker["live_detail"] = fr.detail
        marker["live_detail_note"] = "live_detail is for this live reply only: NEVER persist it"
    if heal is not None:
        marker["heal"] = heal.to_dict()
        marker["error"] += f" Self-heal did not restore freshness: {heal.reason}"
    if marker.get("reason") is None:
        # [ISSUE-NONREGULAR-MEMBER] a corpus that is stale BECAUSE a member is not readable carries that code in
        # its top-level `reason` (a closed set of fixed codes), not only inside the heal text.
        blob = f"{getattr(fr, 'detail', '') or ''} {getattr(heal, 'error', '') or ''}"
        for code in CORPUS_FILE_CODES:
            if code in blob:
                marker["reason"], marker["error_class"] = code, "CorpusFileError"
                # [Opus Diamond P3-5] rebuilding does not help an unreadable member: say what does
                marker["error"] = marker["error"].replace(
                    "rebuild the partition (python -m factlattice_engine.build).",
                    f"a corpus member cannot be read ({code}); fix or remove that file, the corpus recovers on its own.")
                break
    return marker


def _date_in_range(node: dict, since: Optional[str], until: Optional[str],
                   date_property: str) -> bool:
    """since/until time filter over a configured date property (lexicographic ISO compare).

    A node whose date property is absent/unparseable is EXCLUDED when a bound is set
    (can't prove it falls in range) and INCLUDED when no bound is set. Values are run
    through `normalize_date` ([ENH-1]) so the corpus's inconsistent date forms compare
    correctly."""
    if not since and not until:
        return True
    props = node.get("properties") or {}
    d = normalize_date(props.get(date_property))
    if d is None:
        return False
    if since and d < since:
        return False
    if until and d > until:
        return False
    return True


def _did_you_mean(node_id: str, nodes: Dict[str, dict]) -> List[str]:
    """Near-matches for an unknown NODE id. Same argument as the corpus one: a wrong id
    and an absent node present identically, and the correct response to that ambiguity is
    currently to grep."""
    from .extent import did_you_mean
    return did_you_mean(node_id, nodes.keys())


# ISSUE-PRIVATE-NAMES-IN-ERRORS (review F1): a withheld hop is declared but
# ANONYMOUS: no corpus id (engine-resolved from the authored domain), no target node id.
_WITHHELD_STUB = {"withheld": True, "reason": WITHHELD_HOP_REASON}


def _private_skips(private_corpora) -> List[dict]:
    """[INV-PRIVATE-FANOUT] ONE anonymous entry counting EVERY private corpus skipped, hit
    or no hit: the count never reveals which private corpus held a match, and the entry
    carries no corpus NAME (ISSUE-PRIVATE-NAMES-IN-ERRORS: no error, suggestion
    or extent field may name a private corpus). The skip is still DECLARED."""
    n = len(list(private_corpora))
    return [{"reason": PRIVATE_REASON, "count": n}] if n else []


def _scalar_or_elements(v) -> List[str]:
    """`str(v)` for a scalar (today's comparison, unchanged); each element for a list."""
    if isinstance(v, list):
        return [str(e) for e in v]
    return [str(v)]


def _string_values(v) -> List[str]:
    """The STRING values a property carries: the string itself, or every string ELEMENT of a
    list. A repeated key (`- alias: a` / `- alias: b`) and a nested `- item` block both parse to
    a LIST (parser._maybe_multivalue), and every element is live. A mapping (P5) is still not
    read: its values are sub-keyed structure, not the property's own value.
    [FL-LIST-VALUED-PROPERTY-BLIND] measured 2026-10-03: the old `isinstance(v, str)` guard hid
    EVERY value of a two-line `alias`, including the first."""
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [e for e in v if isinstance(e, str)]
    return []


def _text_hit(node: dict, node_id: str, needle: str) -> bool:
    """`lts_find`'s text scope: id, title, STRING property values. NOTHING ELSE.

    ⛔ UNCHANGED BY [FL-PROSE-BLIND], on purpose. Prose search is a separate tool
    (`find_in_prose`), so this predicate — and therefore every `lts_find` result set that
    exists today — is exactly what it was. `_structure_hit_in` below is the same walk
    with the hit location recorded; the two MUST agree, and a test asserts they do.
    """
    return bool(_structure_hit_in(node, node_id, needle))


def _structure_hit_in(node: dict, node_id: str, needle: str) -> List[str]:
    """WHERE in the structured scope the needle appears — `['title', 'properties.artifact']`.

    Empty list == no structured hit == `_text_hit` is False. This exists so
    `lts_find_in_prose` can tell a caller not merely THAT a prose hit is also structured
    but where, which is what separates "already answerable" from "the redundancy is a
    filename in an `artifact:` property" — the originating defect's exact shape.

    The match is the WHOLE needle as one case-insensitive substring of a single field, never
    word by word ([FIND-PHRASE-SEMANTICS], 2026-10-05: "borrower card contents" hits where
    "borrower contents" does not). The fields are the id, the title, every string property
    value, and every string ELEMENT of a list-valued property (`_string_values`, since
    [FL-LIST-VALUED-PROPERTY-BLIND]), so repeated `alias` lines are read.

    Note what it does NOT read, because `find_in_prose` will find these and must report
    them honestly: a property whose value is a MAPPING (sub-keyed structure) is invisible
    here, as is body prose, so such a node is body-only by this predicate's lights even
    though the term may sit in something the author authored as structure.
    """
    needle = needle.lower()
    where: List[str] = []
    if needle in node_id.lower():
        where.append("id")
    if needle in (node.get("title") or "").lower():
        where.append("title")
    props = node.get("properties") or {}
    for k, v in props.items():
        if any(needle in sv.lower() for sv in _string_values(v)):
            where.append(f"properties.{k}")
    return where


# --- [FL-PROSE-BLIND] the routing sentence ---------------------------------------
# ⛔ IT ROUTES; IT DOES NOT APOLOGISE. The failure mode this replaces is a caveat in
# prose beside a number the caller quotes: the sentence has to name the tool, name the
# scope it did NOT cover, and carry the denominator, or it is decoration.
#
# EMITTED ONLY FOR A TEXT QUERY. A `prefix=`/`status=` query has no term to look for in
# prose, so a note there would fire on every call and be trained away within a session —
# which would cost exactly the queries that need it. Emitted for a NON-ZERO result too:
# the originating case returned SIX confident hits, not zero.
def _prose_scope_note(nodes_with_body: int, nodes_in_scope: int) -> str:
    return (
        "STRUCTURED SCOPE ONLY — id, title, and STRING property values were read; "
        f"BODY PROSE WAS NOT. {nodes_with_body} of {nodes_in_scope} nodes in scope carry "
        "source lines beyond their heading that this query did not read (index-derived, "
        "so it counts property lines too and is an UPPER BOUND on unread prose). "
        "A zero — or a thin non-zero — here is consistent with the term being present "
        "ONLY in prose. lts_find_in_prose(text=...) covers it and flags each hit "
        "body-only vs also-in-structure."
    )


# How much of one matching line travels, and how many lines one node may contribute.
# LOCALITY, NOT CONTENT: the caller gets enough to decide whether to open the node, and
# `lts_node` remains the way to actually read it. A generous line cap here would quietly
# turn the diagnostic into a document reader.
MAX_PROSE_LINE_CHARS = 240
MAX_PROSE_LINES_PER_NODE = 3


def _clip(line: str) -> str:
    s = line.strip()
    return s if len(s) <= MAX_PROSE_LINE_CHARS else s[:MAX_PROSE_LINE_CHARS] + " …"


def _prose_hit(node: dict, node_id: str, rows: List[tuple], matched: List[tuple],
               where: List[str], context: int) -> dict:
    """One prose hit: routing identity + locality + the structure classification.

    `rows` is the node's whole body (for the context window), `matched` the subset whose
    lowered text contains the needle. `_tokens` is an internal budget weight, stripped
    before the hit is emitted.
    """
    shown = matched[:MAX_PROSE_LINES_PER_NODE]
    by_no = {r[0]: r[1] for r in rows}
    lines = []
    for lineno, raw, _low in shown:
        entry = {"line": lineno, "text": _clip(raw)}
        if context:
            before = [_clip(by_no[n]) for n in range(lineno - context, lineno)
                      if n in by_no]
            after = [_clip(by_no[n]) for n in range(lineno + 1, lineno + 1 + context)
                     if n in by_no]
            if before:
                entry["before"] = before
            if after:
                entry["after"] = after
        lines.append(entry)
    hit = {
        "id": node_id,
        "title": node.get("title"),
        "source_file": node.get("source_file"),
        "line_range": node.get("line_range"),
        "lines": lines,
        "match_lines": len(matched),
        "lines_shown": len(shown),
        "in_structure": bool(where),
        "structure_hit_in": where,
        # ⛔ THE FIELD THIS TOOL EXISTS FOR. `body-only` == "lts_find(text=…) does NOT
        # return this node" == a densification finding: the graph cannot answer for it.
        "finding": "also-in-structure" if where else "body-only",
    }
    chars = sum(len(e["text"]) for e in lines) + len(str(node.get("title") or "")) + 80
    hit["_tokens"] = max(1, chars // 4)
    return hit


def _prose_digest_row(corpus: str, hit: dict) -> dict:
    """A digest row for a prose hit — IDENTITY AND COUNTS, and no body text whatsoever.

    Every field here is read from the graph INDEX (id/title/source_file/line_range) or is
    a COUNT derived from the scan. `lines` is not carried and cannot be reconstructed from
    what is. See [LTS-TIER]: digest answers "is there something here I need to read?" and
    is deliberately unable to answer "what does it say?" — a prose tool must honour that
    harder than anything else in the surface, not less.
    """
    return {"corpus": corpus, "id": hit["id"], "title": hit["title"],
            "source_file": hit["source_file"], "line_range": hit["line_range"],
            "match_lines": hit["match_lines"], "in_structure": hit["in_structure"],
            "structure_hit_in": hit["structure_hit_in"], "finding": hit["finding"]}


def _node_matches(node: dict, node_id: str, domain, function, service,
                  prefix, status, text, area=None, cq=None) -> Optional[dict]:
    """taxonomy/prefix/status/text predicate for find(),
    + [ENH-2] area-scope: a node's `area` is stamped at build time (config.py) from
    which labeled root (decisions/specs/artifacts/...) it was parsed under.

    Returns None on no-match, or a small dict of MATCH PROVENANCE on a match (today:
    `matched_via` for the lifecycle key that answered). Truthiness is preserved for
    callers that only ask yes/no — an empty dict is never returned.

    `status` is the ONE filter that is not raw equality, and it now sweeps the UNION of
    every lifecycle-bearing key ([FL-RECALL-TWO-VOCABULARIES]): `status`,
    `lifecycle-position`, `lifecycle_position`. Each is compared through
    `lifecycle.status_matches`, so the bare-token widening and the qualified-string
    precision are unchanged — this only ADDS the keys the corpus also authors on. The
    measured defect: `status="contingent"` returned ONE while `lifecycle-position:
    contingent` held THREE, in a different file, with overlap ZERO. See `lifecycle.py`.
    """
    if prefix and not node_id.startswith(prefix):
        return None
    if domain and node.get("domain") != domain:
        return None
    matched_via = None
    if status:
        matched_via = lifecycle_matches(node, status)
        if matched_via is None:
            return None
    if area and node.get("area") != area:
        return None
    props = node.get("properties") or {}
    if function and str(function) not in _scalar_or_elements(props.get("function")):
        return None
    if service and str(service) not in _scalar_or_elements(props.get("service")):
        return None
    if text and not _text_hit(node, node_id, text):
        return None
    # [FIND-QUERY-SURFACE C4] the new conditions (terms, where, source_file, document_tag) are
    # evaluated HERE, in the one shared predicate, from a query compiled once per call. cq=None
    # (every call that uses none of them) leaves this function exactly what it was.
    if cq is not None and not cq.matches(node, node_id):
        return None
    return {"matched_via": matched_via} if matched_via else {"matched": True}


def _keys_consulted(domain, function, service, prefix, status, text, area,
                    date_property, dated: bool) -> List[str]:
    """The field/property keys the predicate actually READ, in a stable order.

    ⛔ THE KEYS CONSULTED, NOT THE KEYS PRESENT. A zero over `[status,
    lifecycle-position, lifecycle_position]` is a different fact from a zero over
    `[status]` alone, and until the response said which it was, the caller could not tell
    them apart — [FL-RECALL-ZERO-IS-AMBIGUOUS] state-4.
    """
    keys: List[str] = []
    if prefix:
        keys.append("id")
    if domain:
        keys.append("domain")
    if status:
        keys.extend(LIFECYCLE_KEYS)
    if area:
        keys.append("area")
    if function:
        keys.append("function")
    if service:
        keys.append("service")
    if text:
        # `_text_hit` reads the id, the title and EVERY property value. Saying so is the
        # point: a caller who sees `properties.*` knows a text miss is not a key miss.
        keys.extend(["id", "title", "properties.*"])
    if dated:
        keys.append(date_property)
    return list(dict.fromkeys(keys))


_SEMANTIC_STATUSES = ("ok", "unavailable", "index_building")


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HEX_DIGEST_RE = re.compile(r"[0-9a-f]{8,64}")


def _exc_class(exc: BaseException) -> str:
    """The exception's class name ONLY if it is a plain identifier, else "Exception": a class can
    be created with any __name__ (a path, a newline, a private corpus name), so even the class name
    is data until validated (delta-3 review P1)."""
    name = type(exc).__name__
    return name if isinstance(name, str) and _IDENT_RE.fullmatch(name) else "Exception"


def _exc_status(exc: BaseException) -> str:
    """[QUERY-REPORT-MISS status-vocabulary] `error: <ExceptionClassName>` and NOTHING else: an
    exception's text can carry any path (relative, spaced, ~user, a private corpus name), so it
    goes to the server's stderr log only; the class name itself is validated (_exc_class)."""
    return f"error: {_exc_class(exc)}"


def _semantic_status(raw) -> str:
    """Only ok | unavailable | index_building ever reach the response or the record."""
    return raw if raw in _SEMANTIC_STATUSES else "unavailable"


def _path_in_any_root(path, roots) -> bool:
    """Is `path` (which may not exist yet) at or under any of `roots`? Compared three ways, any one
    is enough: the realpath prefix; the case-normalised realpath prefix (macOS volumes are usually
    case-insensitive, and realpath does not fold case); and filesystem identity (st_dev, st_ino) of
    every existing ancestor of `path` against each root, which is immune to case, symlinks and
    bind-style aliases."""
    def fold(x):
        return os.path.normcase(os.path.realpath(str(x))).casefold()
    real, f = os.path.realpath(str(path)), fold(path)
    ids = set()
    for r in roots:
        try:
            st = os.stat(str(r))
            ids.add((st.st_dev, st.st_ino))
        except OSError:
            pass
        rr, rf = os.path.realpath(str(r)), fold(r)
        if real == rr or real.startswith(rr + os.sep) or f == rf or f.startswith(rf + os.sep):
            return True
    cur = os.path.abspath(str(path))
    while True:
        try:
            st = os.stat(cur)
            if (st.st_dev, st.st_ino) in ids:
                return True
        except OSError:
            pass
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def _semantic_fault(ex: BaseException) -> dict:
    """[TOOL-REASON-IS-A-FIXED-CODE] a semantic-service fault: a fixed code in `reason`, the exception class, and the
    message (it can hold an absolute path) only in the LIVE-ONLY `live_detail`. Used by EVERY site that turns a service
    fault into a block, nested ones included."""
    return {"reason": "semantic-service-fault", "error_class": type(ex).__name__,
            "live_detail": f"{type(ex).__name__}: {ex}",
            "live_detail_note": "live_detail is for this live reply only: NEVER persist it"}


class LtsEngine:
    """The 4 tool handlers over a LtsStore + Router. Pure/testable — no server import,
    no caller identity, no ACL ceremony. Every tool is `tool(corpus, ...) -> dict`."""

    def __init__(self, store: LtsStore, acl: Optional[AclCheck] = None,
                 default_token_budget: int = DEFAULT_TOKEN_BUDGET,
                 freshness_provider: object = None,
                 date_property: str = DEFAULT_DATE_PROPERTY,
                 self_heal: bool = False, healer: object = None):
        self.store = store
        self.acl: AclCheck = acl if acl is not None else AllowAllAcl()
        if freshness_provider is None:
            from .verdict import ContentHashFreshness
            freshness_provider = ContentHashFreshness()
        self.freshness_provider = freshness_provider
        # --- [LEC-SELF-HEAL] the self-healer ---------------------------------
        # A COLLABORATOR, injected at the composition root (`server.build_tools`), not a
        # default: healing needs a rebuild recipe on the partition and (to stay O(0)) a
        # seated freshness kernel, neither of which an arbitrary `LtsEngine(store)` can
        # promise. With no healer the engine keeps phase-2's pure gate: a loud, terminal
        # refusal. With one, a stale verdict becomes TRANSIENT.
        if healer is None and self_heal:
            from .selfheal import SelfHealer
            healer = SelfHealer(store, freshness_provider=self.freshness_provider)
        self.healer = healer
        self.router = Router(store, acl=self.acl,
                             freshness_provider=self.freshness_provider,
                             healer=healer)
        self.default_token_budget = default_token_budget
        self.date_property = date_property
        # [SEM-FIND] the semantic suggestion service, created lazily on first use so the core
        # engine never imports the optional model stack ([DEC-SEMANTIC-ASSIST]).
        self.semantic = None
        # [SEM-SESSION-PREFACE] one flag for BOTH semantic surfaces; test-and-set under a lock.
        self._semantic_preface_shown = False
        self._semantic_preface_lock = threading.Lock()
        # [LTS-REVERSE-LOOKUP] the cached ReverseIndex + the partition snapshot it was
        # built from. `None` until the first `reverse()`/`_reverse_digest()` call —
        # building it eagerly here would cost every engine construction (tests included)
        # a full-store scan nothing asked for yet.
        self._reverse_index: Optional[ReverseIndex] = None
        self._reverse_index_key: Optional[tuple] = None
        # [CAPABILITY-REGISTRY-CHOICE] which registry this engine was built from, captured ONCE by the
        # composition root (`server.build_tools` / `server.boot_engine`) via `seat_registry` /
        # `seat_registry_error`. None on an engine seated by hand (a unit test): the field is then ABSENT
        # from `lts_list_corpora`, which a reader treats as "server too old", never as "nothing to report".
        self.registry_boot: Optional[dict] = None
        self.registry_ids: frozenset = frozenset()
        self.registry_error: Optional[RegistryError] = None

    # --- the registry this engine was built from -----------------------------
    def seat_registry(self, choice) -> None:
        """Record the registry the store was built from (what the server ACTUALLY loaded)."""
        import datetime as _dt
        self.registry_boot = {"source": choice.source, "path": choice.path, "sha256_at_boot": choice.sha256,
                              "resolved_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.registry_ids = frozenset(str(k) for k in choice.corpora)
        self.registry_error = None

    def seat_registry_error(self, err: RegistryError) -> None:
        """REGISTRY-ERROR MODE: the registry could not be resolved at boot. The engine serves NO corpus data;
        every corpus-scoped tool refuses with `registry-not-found`; the mode ends only by a RESTART."""
        self.registry_boot = {"source": err.source, "path": err.path, "error": str(err)}
        self.registry_ids = frozenset()
        self.registry_error = err

    def _registry_choice(self) -> Optional[dict]:
        """`lts_list_corpora.registry_choice`: {source, path, sha256_at_boot, sha256_now, resolved_at}, or
        {source, path, error} in error mode. Corpus-free by construction. None when never seated."""
        b = self.registry_boot
        if b is None:
            return None
        if self.registry_error is not None:
            return {"source": b["source"], "path": b["path"], "error": b["error"],
                    "error_persist": self.registry_error.persist_text}
        out = dict(b)
        try:
            out["sha256_now"] = _registry.resolve_registry().sha256
        except RegistryError as exc:
            out["sha256_now"] = None
            out["error_now"] = str(exc)
        res = {k: out[k] for k in ("source", "path", "sha256_at_boot", "sha256_now", "resolved_at")}
        if "error_now" in out:
            res["error_now"] = out["error_now"]
        return res

    def _registry_extent_kw(self, corpus_omitted: bool) -> dict:
        """[REGISTRY-SOURCE-EXTENT-KEY] a corpus-OMITTED read under the built-in example registry says so in
        its extent: an answer that came from the example corpora must never read as the user's own."""
        b = self.registry_boot
        if corpus_omitted and b is not None and b.get("source") == "example" and self.registry_error is None:
            return {"registry_source": "example"}
        return {}

    # --- shared helpers -------------------------------------------------------
    def _freshness_eval(self, part: Partition) -> FreshnessResult:
        return self.freshness_provider.evaluate(part)

    def _gate(self, corpus: str):
        """THE CHOKE POINT: existence + freshness + [LEC-SELF-HEAL] recovery.

        Returns (partition, error_dict). On any failure `partition` is None and
        `error_dict` is the response to return verbatim.

        EVERY gated surface funnels through here — `node`/`traverse`/`capabilities`, the
        single-corpus AND multi-corpus arms of `find`, and therefore the whole ANALYTICS
        layer (which folds over `find`). One place decides "serve / heal / refuse", so a
        new tool inherits the behaviour instead of having to remember to call a rebuild.

        The heal is NOT trusted: after it runs, the ORDINARY freshness gate is re-run and
        only a genuinely fresh verdict serves. During the rebuild window, and on any heal
        failure, the phase-2 refusal stands verbatim ([INV-LTS-FRESH]).
        """
        part = self.store.get_partition(corpus)
        if part is None:
            failed = self.store.failed_corpora.get(corpus)
            if failed is not None:
                return None, _corpus_file_refusal(corpus, failed)
            return None, _not_found(corpus, self._public_corpus_ids(),
                                    self._private_corpus_ids())
        fr = self._freshness_eval(part)
        if fr.fresh:
            return part, None
        if self.healer is None:
            return None, _stale_marker(fr)
        outcome = self.healer.heal(part)
        fr = self._freshness_eval(part)          # re-verify; never trust the heal's word
        if fr.fresh:
            return part, None
        return None, _stale_marker(fr, heal=outcome)

    # --- corpora that FAILED to build on a corpus-file-* code ([ISSUE-NONREGULAR-MEMBER]) ---
    def _failed_view(self):
        """(public failures [{corpus_id, code, path}], number of PRIVATE failures). [INV-PRIVATE-FANOUT]: a failed private
        corpus never reached a Partition, so the privacy decision is the one stamped when the failure was recorded; on an
        UNNAMED call it is COUNTED anonymously and never named (a caller that names it gets the code from the gate)."""
        fails = sorted(self.store.failed_corpora.items())
        pub = [{"corpus_id": cid, "code": f["code"], "path": f["path"]} for cid, f in fails if not f.get("private")]
        return pub, sum(1 for _cid, f in fails if f.get("private"))

    def _failed_skips(self) -> List[dict]:
        pub, n = self._failed_view()
        return ([{"corpus": f["corpus_id"], "reason": f["code"]} for f in pub]
                + ([{"reason": PRIVATE_FAILED_REASON, "count": n}] if n else []))

    def _attach_failed(self, out: dict, arguments: dict) -> dict:
        """A corpus-OMITTED fan-out answer (aggregate, profile_audit) says which corpora it could not read."""
        if isinstance(out, dict) and out.get("success") and not arguments.get("corpus"):
            skips = self._failed_skips()
            if skips:
                out["corpora_skipped"] = skips
        return out

    # --- [INV-DECLARED-EXTENT] --------------------------------------------------
    def _extent(self, parts: List[Partition], *, keys_consulted=(), skipped=(),
                scope=None, notes=(), body_scanned=None, scope_note=None) -> dict:
        """`{"extent": {...}}` for a set of partitions actually searched.

        ⛔ IT COUNTS THE NODES IN SCOPE, NOT THE NODES RETURNED. That number is the
        DENOMINATOR — "the corpora themselves insist that a claim of clean without a
        denominator is indistinguishable from a check that inspected nothing, and the
        query layer made exactly that claim on every zero."

        `built_at` and `fresh` ride along because a node added since the last parse is
        invisible and its absence currently reads as "no such decision"
        ([FL-RECALL-INVISIBLE-PRECONDITIONS] precondition-3): `lts_profile_audit` refuses
        over a stale index, while find/node/traverse answered from it without comment.
        They still answer — a self-healing gate makes that safe — but they now SAY what
        they answered from.
        """
        built_at = {}
        for p in parts:
            sc = (p.index.get("source_corpus") or {}) if isinstance(p.index, dict) else {}
            built_at[p.corpus_id] = sc.get("built_at") or p.index.get("generated_at")
        skipped = list(skipped)
        # A corpus-OMITTED (fan-out) read must not look like it read everything: a corpus that FAILED to build on a
        # corpus-file-* code is named, with the code, in `corpora_skipped` ([ISSUE-NONREGULAR-MEMBER]).
        if not (scope and scope.get("corpus") not in (None, "", "ALL LOADED")):
            skipped += self._failed_skips()
        return declare_extent(
            corpora_consulted=[p.corpus_id for p in parts],
            corpora_skipped=skipped,
            corpora_available=self._public_corpus_ids(),
            nodes_in_scope=sum(len(p.nodes) for p in parts),
            keys_consulted=list(keys_consulted),
            index_fresh=not [x for x in skipped if x.get("reason") == "stale" or x.get("reason") in CORPUS_FILE_CODES
                             or x.get("reason") == PRIVATE_FAILED_REASON],
            built_at=built_at,
            root_paths={p.corpus_id: list(p.root_paths) for p in parts},
            scope=scope,
            notes=list(notes),
            body_scanned=body_scanned,
            scope_note=scope_note,
            **self._registry_extent_kw(bool(scope) and scope.get("corpus") == "ALL LOADED"),
        )

    def _budget(self, token_budget: Optional[int]) -> int:
        """Resolve the effective budget — DELEGATES to `budget.resolve_budget`.

        A caller who names a budget has stated the size of the context it is willing to
        spend; the engine's job is to honour that number, and to apply the default only
        when it was not told one. That ruling is shared, not restated, so a layer added
        later (LINT was) inherits it rather than re-deciding it differently.
        """
        return resolve_budget(token_budget, self.default_token_budget)

    def _budget_slice(self, items: List[Any], offset: int, budget: int,
                      keyfn) -> Tuple[List[Any], bool, Optional[int], int]:
        """Token-budget slice ([ENGINE-BUDGET]) — DELEGATES to `budget.budget_slice`.

        The body used to live here, as a private method on the engine. That put the
        contract out of reach of the one layer that answers with no store seated
        ([LAYER-LINT], which never constructs an `LtsEngine`), and lint was
        consequently the only collection-returning surface with no bound and no
        declaration. Kept as a method purely so existing call sites read unchanged;
        there is now exactly ONE implementation, in `factlattice_engine.budget`.
        (The id this docstring used to cite, `[LTS-TOKEN-BUDGET]`, was defined in no
        spec document — the contract four tools implement had no spec node.)
        """
        return budget_slice(items, offset, budget, keyfn)

    def _source_excerpt(self, part: Partition, node: dict,
                        token_budget: Optional[int], *,
                        tier: Optional[str] = None) -> Tuple[Optional[str], bool]:
        """Read the node's own `line_range` from its source file.

        `tier` decides which cap governs — and the split is load-bearing, not
        cosmetic ([ISSUE-EXCERPT-BUDGET]):

        * `tier is None` (the LEGACY path): capped at `MAX_EXCERPT_CHARS`, exactly as
          before this fix — that constant predates the tier feature by one commit and
          exists to bound the UNTIERED response's default size. Byte-identical legacy
          behaviour is a tested guarantee ([LTS-TIER] additive-only); this branch must
          never change.
        * `tier` is `digest`/`page`/`full` (any TIERED caller): the caller's own
          `token_budget` (already resolved to a default when absent) IS the ceiling —
          the same contract `assert_full_fits` enforces for `find`/`traverse`. The
          legacy `MAX_EXCERPT_CHARS` floor MUST NOT additionally clamp a tiered read:
          doing so silently substitutes a ~1000-token constant for whatever budget the
          caller declared, so raising `token_budget` (the error's own remediation)
          could never help — measured on `Acme-RESEARCH-FRAME` (1,437-token body, 10
          edges) refusing at the DEFAULT 32,768-token budget, and on
          `DEC-ACME-LOOKUP-ORDER-IS-FIXED` (2,871-token body) still refusing at
          `token_budget=90000`. `token_estimate()` (`factlattice/parser.py`) is the
          engine's one canonical ~4-chars/token ratio — the same ratio used here keeps
          this cap and the node's own `token_count` commensurable.
        """
        sf = node.get("source_file")
        lr = node.get("line_range") or []
        if not sf or not part.root_paths:
            return None, False
        # A merged multi-root partition ([ENH-2]) has one node source_file per its OWN
        # root, not necessarily root_paths[0] — try each root, first hit wins.
        path = next((os.path.join(r, sf) for r in part.root_paths
                    if os.path.isfile(os.path.join(r, sf))), None)
        if path is None:
            path = os.path.join(part.root_paths[0], sf)  # best-effort fallback
        try:
            lines = io.StringIO(safe_read_text(path, sf), newline="\n").readlines()
        except OSError:
            return None, False
        start = max(1, lr[0]) if lr else 1
        end = min(len(lines), lr[1]) if len(lr) > 1 else start
        excerpt = "".join(lines[start - 1:end])
        if tier is not None:
            cap = self._budget(token_budget) * 4  # ~4 chars/token; budget IS the ceiling
        else:
            cap = MAX_EXCERPT_CHARS
            if token_budget:
                cap = min(cap, self._budget(token_budget) * 4)  # ~4 chars/token
        if len(excerpt) > cap:
            return excerpt[:cap], True
        return excerpt, False

    def _is_cross(self, part: Partition, edge: dict) -> bool:
        td = edge.get("target_domain")
        return bool(td and td != part.domain_suffix)

    def _route(self, edge: dict, origin_corpus: Optional[str] = None):
        """Route a cross-corpus edge. Returns (kind, payload):
          ('node', routed_node)      target readable + fresh;
          ('withheld', stub)         target lives in a PRIVATE corpus other than
                                     `origin_corpus` ([INV-PRIVATE-FANOUT]): the hop is
                                     declared, its content is not served;
          ('stale', marker)          target STALE;
          ('unresolved', marker)     target not found / no such corpus (plain — NOT elided)."""
        # [INV-PRIVATE-FANOUT] privacy is decided BEFORE freshness, so a stale private
        # target is the same withheld stub as a fresh one (one rule, not two paths).
        td = edge.get("target_domain")
        if td:
            _status, tpart = self.router._resolve_corpus(td)
            if (tpart is not None and tpart.corpus_id != origin_corpus
                    and is_private_partition(tpart)):
                return "withheld", dict(_WITHHELD_STUB)
        try:
            rr = self.router.route_edge(edge)
        except StalePartitionError as spe:
            return "stale", {"corpus": spe.freshness.corpus_id,
                             "target": edge.get("target"),
                             "error": "STALE routed partition; not served [INV-LTS-FRESH]"}
        if rr.ok and rr.node is not None:
            tpart = self.store.get_partition(rr.corpus_id)
            if (rr.corpus_id != origin_corpus and tpart is not None
                    and is_private_partition(tpart)):
                return "withheld", dict(_WITHHELD_STUB)
            return "node", {"corpus": rr.corpus_id, **rr.node}
        return "unresolved", {"target": edge.get("target"),
                              "target_domain": edge.get("target_domain"),
                              "status": rr.status}

    # --- [INV-PRIVATE-FANOUT] corpus-omitted reads skip private corpora --------
    def _private_corpus_ids(self) -> List[str]:
        return sorted(p.corpus_id for p in self.store.partitions()
                      if is_private_partition(p))

    def _public_corpus_ids(self) -> List[str]:
        """Every corpus id that may be NAMED in a response (error, suggestion, extent).
        A private corpus is reached only when the caller names it exactly, so its id
        never appears in anything the engine volunteers."""
        private = set(self._private_corpus_ids())
        return sorted(c for c in self.store.corpus_ids() if c not in private)

    def _name_safe_stale(self, stale) -> List[str]:
        """`stale` minus private corpora (a stale private corpus is counted in
        `complete`, never named)."""
        private = set(self._private_corpus_ids())
        return [c for c in stale if c not in private]

    def _private_stale_field(self, stale) -> dict:
        n = len(stale) - len(self._name_safe_stale(stale))
        return {"private_stale_count": n} if n else {}

    def _private_stale_skips(self, stale) -> List[dict]:
        """ONE anonymous stale entry for private corpora: keeps `extent.index.fresh` false
        (self-consistent with `complete: false`) without naming any of them."""
        n = len(stale) - len(self._name_safe_stale(stale))
        return [{"reason": "stale", "private_stale_count": n}] if n else []

    def _fanout_parts(self):
        """The corpus-omitted arm of every multi-corpus read: gate each PUBLIC corpus
        through the self-heal choke point and return `(parts, stale_corpora,
        private_corpora)`. A private corpus is never gated, read or healed here; it is
        returned by id only so the caller can declare the skip."""
        private = set(self._private_corpus_ids())
        parts: List[Partition] = []
        stale: List[str] = []
        for cid in self.store.corpus_ids():
            if cid in private:
                continue
            p, _gate_err = self._gate(cid)
            if p is not None:
                parts.append(p)
            else:
                stale.append(cid)
        return parts, stale, sorted(private)

    def _public_referrers(self, idx, corpus: str, node_id: str):
        """`idx.referrers` minus every referrer homed in a private corpus OTHER than the
        one the caller named. A personality node citing a public node is a fact about
        that self, and a reverse lookup on the public node is a fan-out over its
        referrers' homes; the named corpus's own referrers are kept."""
        private = set(self._private_corpus_ids()) - {corpus}
        refs = [r for r in idx.referrers(corpus, node_id) if r.corpus not in private]
        return refs, sorted(private)

    # --- [LTS-REVERSE-LOOKUP] the ReverseIndex choke point -------------------
    def _reverse_index_snapshot(self):
        """Gate EVERY loaded corpus ([LEC-SELF-HEAL], self-heal-then-re-verify — the
        SAME choke point `_gate` already is) and return `(fresh_parts, stale_corpora)`.
        A corpus that cannot be verified fresh is never handed to `ReverseIndex.build`
        — see `reverse_index.py`'s freshness contract."""
        fresh_parts: List[Partition] = []
        stale_corpora: List[str] = []
        for cid in self.store.corpus_ids():
            p, err = self._gate(cid)
            if p is not None:
                fresh_parts.append(p)
            else:
                stale_corpora.append(cid)
        return fresh_parts, stale_corpora

    def _reverse_index_for(self):
        """The cached `ReverseIndex`, rebuilt iff the fresh-partition snapshot moved.

        Returns `(index, fresh_parts, stale_corpora)`. `stale_corpora` is reported by
        every caller so an incomplete answer is never mistaken for a complete one
        ([INV-LTS-FRESH]) — the union this index answers over can only ever be the
        corpora that were verifiably fresh AT THIS CALL.
        """
        fresh_parts, stale_corpora = self._reverse_index_snapshot()
        key = snapshot_key(fresh_parts)
        if self._reverse_index is None or self._reverse_index_key != key:
            self._reverse_index = ReverseIndex.build(fresh_parts)
            self._reverse_index_key = key
        return self._reverse_index, fresh_parts, stale_corpora

    # --- lts_reverse ([LTS-REVERSE-LOOKUP]) ----------------------------------
    def reverse(self, node_id: str, corpus: str, limit: Optional[int] = None,
               offset: int = 0, edge_types: Optional[List[str]] = None,
               verbs: Optional[List[str]] = None, token_budget=None) -> dict:
        """The COMPLETE `target -> referrers` answer for one node — the completeness
        path `include_reverse_digest` on `lts_find`/`lts_node` deliberately is NOT
        ([R5]). Referrers come from BOTH channels `reverse_index.py` documents: typed
        graph edges (same-corpus AND cross-corpus) and non-minting property-VALUE `[ID]`
        references. Paged like every other bounded collection ([ENGINE-BUDGET] /
        [INV-DECLARED-REACH]): `count`/`total_matched`/`truncated`/`cursor`.

        `edge_types` filters `how="edge:<TYPE>"` entries to the named types (case-
        sensitive, matching the graph's own edge-type spelling, e.g. `DEPENDS_ON`).
        `verbs` filters `how="property:<key>"` entries to the named property keys.
        Both filters are additive/optional; omitting both returns every referrer of
        either channel.

        `complete` is `not stale_corpora` — the corpus this node lives in was
        successfully gated (or the call refuses below, exactly like `node`/`traverse`),
        but a referrer living in a corpus this process could not verify fresh is a hole
        in the union, and this field says so rather than silently under-counting.
        """
        part, err = self._gate(corpus)
        if err is not None:
            return err
        budget = self._budget(token_budget)
        idx, _fresh_parts, stale_corpora = self._reverse_index_for()
        all_refs, private_corpora = self._public_referrers(idx, corpus, node_id)
        if edge_types:
            wanted_edges = {f"edge:{t}" for t in edge_types}
            all_refs = [r for r in all_refs if r.how in wanted_edges]
        if verbs:
            wanted_props = {f"property:{v}" for v in verbs}
            all_refs = [r for r in all_refs if r.how in wanted_props]
        total = len(all_refs)
        rows = [r.to_dict() for r in all_refs]
        emitted, truncated, cursor, _total_again = self._budget_slice(
            rows, offset, budget, keyfn=lambda r: {"token_count": 1})
        if limit is not None:
            lim = max(0, int(limit))
            if len(emitted) > lim:
                emitted = emitted[:lim]
                cursor = offset + lim
                truncated = True
        out = {
            "success": True, "corpus": corpus, "node_id": node_id,
            "referrers": emitted,
            **declare(emitted, truncated, cursor, total),
            "complete": not stale_corpora,
            "stale_corpora": self._name_safe_stale(stale_corpora),
            **self._private_stale_field(stale_corpora),
            "unresolved_domain_refs": len(idx.unresolved),
            **self._extent(
                [part], keys_consulted=["edges.*", "properties.*"],
                skipped=([{"corpus": c, "reason": "stale"}
                          for c in self._name_safe_stale(stale_corpora)]
                         + self._private_stale_skips(stale_corpora)
                         + _private_skips(private_corpora)),
                scope={"tool": "lts_reverse", "corpus": corpus, "node_id": node_id,
                       "edge_types": edge_types, "verbs": verbs}),
        }
        return out

    # --- lts_history ([NODE-CHANGE-LEDGER] v1) ---------------------------------------
    HISTORY_DEFAULT_LIMIT = 20

    def history(self, corpus: str, node_id: str, limit: Optional[int] = None) -> dict:
        """The ledgered changes to ONE node, newest first. READ-ONLY.

        Reads the ledger of the node's SOURCE FILE (one ledger per corpus file; the node's
        `source_file` is resolved against the partition's roots, then realpath'd). ⛔ ABSENCE IS
        DECLARED: with no ledger, `ledger: "none"` and `history_complete: false` (nothing is known,
        which is not "nothing happened"); with a ledger, `ledger_since` is the first record's ts and
        any change before it is unknown, so `history_complete` stays false in v1 either way.
        `reconciled` says whether the last record's `file_sha256_after` equals the file on disk NOW;
        false means an edit outside the writers since the last ledgered write (reported, not blocked).
        """
        from factlattice import ledger as _ledger
        part, err = self._gate(corpus)
        if err is not None:
            return err
        nd = part.get_node(node_id)
        if nd is None:
            return {"success": False, "corpus": corpus, "node_id": node_id,
                    "error": f"node {node_id!r} not found in corpus {corpus!r}",
                    "did_you_mean": _did_you_mean(node_id, part.nodes)}
        sf = nd.get("source_file")
        path = next((os.path.join(r, sf) for r in part.root_paths
                     if sf and os.path.isfile(os.path.join(r, sf))), None)
        if path is None:
            return {"success": False, "corpus": corpus, "node_id": node_id,
                    "error": f"source file {sf!r} of node {node_id!r} is not under any root of {corpus!r}"}
        real = os.path.realpath(path)
        lim = self.HISTORY_DEFAULT_LIMIT if limit is None else max(0, int(limit))
        raw = _ledger.read_lines(real)
        base = {"success": True, "corpus": corpus, "node_id": node_id, "source_file": sf,
                "history_complete": False}
        scope = {"tool": "lts_history", "corpus": corpus, "node_id": node_id, "limit": lim}
        if raw is None:
            return {**base, "ledger": "none", "ledger_since": None, "reconciled": None,
                    "chain_file_breaks": [], "history_has_out_of_band_edits": None,
                    "records": [], "count": 0, "total_matched": 0, "truncated": False,
                    "note": "this file has NO ledger: changes made before ledgering began, or by any "
                            "writer that bypasses the typed writers, are UNKNOWN, not absent.",
                    **self._history_extent(part, scope, ledger_file=None, records_scanned=0)}
        recs, bad = [], 0
        for line in raw:
            try:
                recs.append(json.loads(line))
            except ValueError:
                bad += 1
        tail_ok = True
        try:
            json.loads(raw[-1]) if raw else None
        except ValueError:
            tail_ok = False
        # [F2] out-of-band edits BETWEEN ledgered writes: a record whose file_sha256_before is not the
        # previous record's file_sha256_after (the file changed with no writer involved).
        breaks = [{"seq": b.get("ledger_seq"), "ts": b.get("ts")}
                  for a_, b in zip(recs, recs[1:])
                  if b.get("file_sha256_before") != a_.get("file_sha256_after")]
        mine = [r for r in recs if r.get("node_id") == node_id]
        mine.reverse()
        shown = mine[:lim]
        try:
            disk = hashlib.sha256(safe_read_bytes(real)).hexdigest()
        except OSError:
            disk = None
        # [F10] never computed from an older record when the tail cannot be read
        reconciled = (bool(recs) and recs[-1].get("file_sha256_after") == disk) if tail_ok else None
        return {**base, "ledger": "present", "ledger_since": recs[0].get("ts") if recs else None,
                "reconciled": reconciled,
                "reconciled_reason": None if tail_ok else "tail unparseable",
                "chain_file_breaks": breaks,
                "history_has_out_of_band_edits": bool(breaks) or reconciled is False,
                "records": shown, "count": len(shown),
                "total_matched": len(mine), "truncated": len(mine) > len(shown),
                "unparseable_lines": bad,
                **self._history_extent(part, scope, ledger_file=str(_ledger.ledger_path(real)),
                                       records_scanned=len(recs))}

    def _history_extent(self, part, scope, **ledger_facts) -> dict:
        ext = self._extent([part], keys_consulted=["source_file"], scope=scope)
        ext["extent"].update(ledger_facts)
        return ext

    # --- [R5] the reverse-referrer DIGEST -- non-completeness by construction ---
    # A small, capped preview embedded on `lts_find`/`lts_node` via
    # `include_reverse_digest=True`. It NEVER claims to be the answer: every field name
    # here is chosen so a reader who only glances at the dict still sees the shape of
    # what was left out, and `see_also` names the ONE call that closes the gap.
    REVERSE_DIGEST_SAMPLE_CAP = 5

    def _reverse_digest(self, corpus: str, node_id: str) -> dict:
        idx, _fresh_parts, stale_corpora = self._reverse_index_for()
        all_refs, _private = self._public_referrers(idx, corpus, node_id)
        total = len(all_refs)
        sample = [r.to_dict() for r in all_refs[:self.REVERSE_DIGEST_SAMPLE_CAP]]
        returned = len(sample)
        return {
            "total_referrers": total,
            "returned": returned,
            "omitted": total - returned,
            "truncated": total > returned,
            "sample": sample,
            "complete": not stale_corpora,
            "stale_corpora": self._name_safe_stale(stale_corpora),
            **self._private_stale_field(stale_corpora),
            "see_also": (f"lts_reverse(corpus={corpus!r}, node_id={node_id!r}) "
                        f"for the complete, paged referrer list"),
        }

    @property
    def _qs_caches(self):
        """[FIND-QUERY-SURFACE §8] per-partition lowercased-field / word caches for word and
        phrase terms, keyed on the freshness key and LRU-bounded (CACHE_MAX corpora)."""
        c = self.__dict__.get("_qs_caches_obj")
        if c is None:
            c = self.__dict__["_qs_caches_obj"] = _qs.FieldCaches()
        return c

    def _lexrank_for(self, part):
        """Deterministic BM25 over the partition's semantic cards, cached on its freshness key
        (no model stack: semantic.lexrank / index.partition_cards import no numpy)."""
        from .semantic.index import partition_cards
        from .semantic.lexrank import LexRank
        from collections import OrderedDict
        cache = self.__dict__.setdefault("_lexrank_cache", OrderedDict())
        key = part._freshness_key()
        hit = cache.get(part.corpus_id)
        if hit is None or hit[0] != key:
            hit = (key, LexRank(partition_cards(part)))
        cache[part.corpus_id] = hit
        cache.move_to_end(part.corpus_id)
        while len(cache) > LEXRANK_CACHE_MAX:     # [S5 review F5] bounded (acme-docs ~70 MB)
            cache.popitem(last=False)
        return hit[1]

    # --- [QUERY-REPORT-MISS] lts_report_miss ----------------------------------------
    MISS_MAX_IDS, MISS_MAX_WHY, MISS_MAX_QUERY, MISS_MAX_SHORT = 10, 2000, 2000, 500

    def report_miss(self, corpus=None, query=None, expected_ids=None, why=None,
                    tool_used=None, found_via=None) -> dict:
        """File a known-item miss: the query a caller ran, the node(s) it knows exist, and why.

        Read-only on every corpus. The evidence is captured NOW (the corpus moves): the rank of
        each expected id under the exact `lts_find` (relevance order) and under `lts_semantic_find`
        (k=50), or None. One JSON line is appended to `<state_dir>/miss-log.jsonl`; a write
        failure is `success: false` with a reason, never an exception. `corpus` is required (no
        fan-out; a private corpus only when named). The query and `why` are stored LITERALLY: the
        caller chose to disclose them.
        """
        import uuid
        from datetime import datetime, timezone
        from . import querylog
        if not corpus or not isinstance(corpus, str):
            return {"success": False, "error": "lts_report_miss requires `corpus`: a corpus id string (it never fans out)"}
        if not isinstance(query, str) or not query.strip():
            return {"success": False, "error": "lts_report_miss requires a non-empty `query`"}
        if len(query) > self.MISS_MAX_QUERY:
            return {"success": False, "error": f"`query` is over {self.MISS_MAX_QUERY:,} characters"}
        if (not isinstance(expected_ids, list) or not expected_ids
                or not all(isinstance(i, str) and i.strip() for i in expected_ids)):
            return {"success": False, "error": "lts_report_miss requires `expected_ids`: a non-empty list of node id strings"}
        ids = list(dict.fromkeys(i.strip() for i in expected_ids))
        if len(ids) > self.MISS_MAX_IDS:
            return {"success": False, "error": f"at most {self.MISS_MAX_IDS} expected_ids per report (got {len(ids)})"}
        if not isinstance(why, str) or not why.strip():
            return {"success": False, "error": "lts_report_miss requires `why` (what you needed and why the result did not serve)"}
        if len(why) > self.MISS_MAX_WHY:
            return {"success": False, "error": f"`why` is over {self.MISS_MAX_WHY:,} characters"}
        tool_used = "lts_find" if tool_used is None else tool_used
        for label, val in (("tool_used", tool_used), ("found_via", found_via)):
            if val is not None and (not isinstance(val, str) or len(val) > self.MISS_MAX_SHORT):
                return {"success": False, "error": f"`{label}` must be a string of at most {self.MISS_MAX_SHORT} characters"}
        part, err = self._gate(corpus)
        if err is not None:
            return err
        unknown = [i for i in ids if i not in part.nodes]
        known = [i for i in ids if i in part.nodes]
        if not known:
            return {"success": False, "unknown_ids": unknown, "error": (
                "none of the expected_ids exist in corpus %r (%s); nothing was recorded. "
                "Check the ids with lts_find / lts_node." % (corpus, ", ".join(unknown)))}
        # the evidence, captured now ------------------------------------------------------
        exact_status, exact_pos = "ok", {}
        try:
            ex = self.find(corpus=corpus, text=query, rank="relevance", semantic="off", tier="digest",
                           word_fallback=False)
            if ex.get("success") is False:
                if ex.get("reason") == "digest_ceiling_exceeded" or "ceiling" in str(ex.get("error")).lower():
                    exact_status = "error: digest ceiling exceeded"
                else:
                    logger.warning("[lts-engine] report_miss: exact search failed: %s", str(ex.get("error"))[:300])
                    exact_status = "error: FindFailed"
            else:
                exact_pos = {d["id"]: n for n, d in enumerate(ex.get("digest") or [], 1)}
        except Exception as exc:  # noqa: BLE001 -- the report is evidence; a find fault must not lose it
            logger.warning("[lts-engine] report_miss: exact search raised: %s: %s", type(exc).__name__, exc)
            exact_status = _exc_status(exc)
        sem_status, sem_pos, index_version = "unavailable", {}, None
        try:
            sem = self.semantic_find(corpus=corpus, text=query, k=50)
            sem_status = _semantic_status(sem.get("status"))
            if sem_status != sem.get("status"):
                logger.warning("[lts-engine] report_miss: semantic status %r reported as %r", sem.get("status"), sem_status)
            iv = sem.get("index_version")
            index_version = iv if isinstance(iv, str) and _HEX_DIGEST_RE.fullmatch(iv) else None
            if sem_status == "ok":
                sem_pos = {c["id"]: n for n, c in enumerate(sem.get("candidates") or [], 1)}
        except Exception as exc:  # noqa: BLE001 -- a model fault is a STATUS, never a failed report
            logger.warning("[lts-engine] report_miss: semantic search raised: %s: %s", type(exc).__name__, exc)
            sem_status = _exc_status(exc)
        ranks = {i: {"exact": exact_pos.get(i), "semantic": sem_pos.get(i)} for i in known}
        try:
            from .build_stamp import build_stamp
            head = build_stamp()
        except Exception:  # noqa: BLE001
            head = None
        report_id = uuid.uuid4().hex[:16]
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "session": querylog.SESSION_ID, "report_id": report_id, "corpus": corpus,
               "query": query, "expected_ids": ids, "unknown_ids": unknown, "why": why,
               "tool_used": tool_used, "found_via": found_via, "engine_build_stamp": head,
               "index_version": index_version, "exact_status": exact_status,
               "semantic_status": sem_status, "ranks": ranks}
        out = {"success": True, "report_id": report_id, "corpus": corpus, "ranks": ranks,
               "unknown_ids": unknown, "exact_status": exact_status, "semantic_status": sem_status}
        try:
            path = querylog.miss_log_path()
            real = os.path.realpath(str(path))
            try:
                from .membership import registered_roots      # root-path membership, not corpus content
                roots, _src = registered_roots(self.store)
                all_roots = [r for rs in roots.values() for r in rs]
            except Exception:  # noqa: BLE001 -- unknown roots: FAIL CLOSED below
                all_roots = []
            if not all_roots or any(not isinstance(r, str) or not r.strip() for r in all_roots):
                # ⛔ fail closed: if the roots cannot be enumerated the guard cannot be verified, so
                # nothing is written. Names no path and no corpus.
                return {"success": False, "report_id": report_id, "exact_status": exact_status,
                        "semantic_status": sem_status,
                        "error": ("cannot verify the miss log is outside every corpus root: corpus roots "
                                  "unknown; nothing written"),
                        "reason": "corpus-roots-unknown"}
            if _path_in_any_root(path, all_roots):
                # ⛔ names no path, no corpus, no root: it may be a private corpus
                return {"success": False, "report_id": report_id, "ranks": ranks,
                        "unknown_ids": unknown, "exact_status": exact_status, "semantic_status": sem_status,
                        "error": ("the state dir resolves inside a registered corpus root; refused, nothing "
                                  "written. Set FACTLATTICE_STATE_DIR to a directory outside every corpus."),
                        "reason": "miss-log-inside-corpus-root"}
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[lts-engine] report_miss: could not write the miss log: %s: %s", type(exc).__name__, exc)
            return {"success": False, "report_id": report_id, "ranks": ranks, "unknown_ids": unknown,
                    "exact_status": exact_status, "semantic_status": sem_status,
                    "error": f"could not write the miss log: {_exc_class(exc)}",
                    "reason": "miss-log-write-failed", "error_class": _exc_class(exc)}
        return out

    # --- [SEM-FIND] lts_semantic_find -----------------------------------------------
    def semantic_find(self, corpus=None, text=None, k: int = 10, prefix=None, status=None,
                      area=None) -> dict:
        """Explicit, opt-in semantic SUGGESTIONS for one named corpus ([DEC-SEMANTIC-ASSIST]).

        Never matches: the answer is a labelled suggestion block (`kind:
        semantic-suggestions-not-matches`, `is_assertion: false`), with no `nodes` and no match
        count. `corpus` is REQUIRED: there is no fan-out, so a private corpus is searched only
        when a caller names it ([INV-PRIVATE-FANOUT]). A missing model stack is a reported
        `status: unavailable`, never an error.
        """
        if not corpus:
            return {"success": False, "error": (
                "lts_semantic_find requires `corpus`: it never fans out (a private corpus is "
                "searched only when named). Pick one with lts_list_corpora.")}
        if not text or not str(text).strip():
            return {"success": False, "error": "lts_semantic_find requires non-empty `text`"}
        part, err = self._gate(corpus)
        if err is not None:
            return err
        def keep(nid, nd):
            return _node_matches(nd, nid, None, None, None, prefix, status, None, area=area) is not None
        try:
            k = max(1, min(int(k), 50))
        except (TypeError, ValueError):
            k = 10
        try:
            if self.semantic is None:      # [delta F-D2] construction is inside the fault guard too
                from .semantic.suggest import SemanticService
                self.semantic = SemanticService()
            block = self.semantic.suggest(part, str(text), k=k, keep=keep)
        except Exception as ex:  # noqa: BLE001 -- [review F4] a service fault is a STATUS, never an error
            block = {"mode": "semantic", "kind": "semantic-suggestions-not-matches", "is_assertion": False,
                     "status": "unavailable", "candidates": [], **_semantic_fault(ex)}
        return {**self._preface(block), "success": True, "corpus": corpus, "query": text, **block,
                **self._extent([part], keys_consulted=["semantic:head-card", "semantic:body-chunks"],
                               scope={"tool": "lts_semantic_find", "corpus": corpus,
                                      "filters": {k2: v for k2, v in dict(prefix=prefix, status=status,
                                                                          area=area).items() if v}})}

    # --- lts_find -----------------------------------------------------------
    def find(self, corpus=None, domain=None, function=None, service=None,
             prefix=None, status=None, text=None, since=None, until=None,
             date_property=None, area=None, token_budget=None, offset: int = 0,
             tier=None, limit=None, order_by=None,
             include_reverse_digest: bool = False, semantic=None, rank=None,
             all_of=None, any_of=None, none_of=None, match=None, where=None, fields=None,
             source_file=None, document_tag=None, explain=False,
             word_fallback: bool = True, path=None) -> dict:
        """`_find_exact` + the optional [SEM-ON-MISS] suggestion block.

        `path` ([A-E7 / FIND-PATH-SOURCE]): ONE `.lts` file parsed in isolation instead of a registered
        corpus, exclusive with `corpus`; see `pathsource`. Read-only, no index, no shared cache; the exact
        answer only (`semantic` other than off, `word_fallback`, `rank='relevance'`, `source_file` and
        `include_reverse_digest` are refused, and the env defaults for suggestions and the word fallback
        never reach it).

        [FIND-QUERY-SURFACE] `all_of`/`any_of`/`none_of`/`match`/`where`/`fields`/`source_file`/
        `document_tag`/`explain` are ADDITIVE: a call using none of them returns exactly the
        response it always did. Their conditions are evaluated in `_node_matches` from a query
        compiled once (`query_surface.compile_query`); a refusal is `success: false` naming the
        parameter. `word_fallback=False` is an INTERNAL keyword (not in the schema) that keeps
        internal callers' exact semantics independent of LTS_FIND_WORD_FALLBACK.

        `rank` ([S5-RANK-RELEVANCE]): None/"default" = today's (corpus, id) order; "relevance" =
        a deterministic BM25 order of the SAME exact match set (membership and counts unchanged),
        applied BEFORE paging. Requires `text` and a named `corpus`; exclusive with `order_by`.

        `semantic`: "off" | "on_miss"; None = env LTS_SEMANTIC_ON_MISS (off when unset; the
        The maintainer ruled "Turn on now" on 2026-10-03, so the deployment sets it). The exact answer is computed by
        `_find_exact` exactly as before and is NEVER modified: on a lexical ZERO for a NAMED
        corpus, a separate `did_you_mean_semantic` block is added beside it, and one sentence
        is appended to `extent.scope_note`. Off, the response is byte-identical to before.
        """
        path_part = None
        if path is not None:
            from . import pathsource as _ps
            refusal = self._path_refusal(path, corpus, semantic, rank, source_file, include_reverse_digest)
            if refusal:
                return {"success": False, "error": refusal}
            try:
                path_part, file_source = _ps.load_partition(path)
            except _ps.PathRefusal as pe:
                return {"success": False, "error": str(pe)}
            semantic, word_fallback = "off", False
        try:
            cq = _qs.compile_query(
                all_of=all_of, any_of=any_of, none_of=none_of, match=match, where=where,
                fields=fields, source_file=source_file, document_tag=document_tag,
                explain=explain, corpus=corpus,
                other_positive_filter=bool(domain or function or service or prefix or status
                                           or text or since or until or area))
        except _qs.QueryError as qe:
            return {"success": False, "error": str(qe)}
        mode = semantic
        if mode is None:
            mode = "on_miss" if os.environ.get("LTS_SEMANTIC_ON_MISS", "").strip().lower() in (
                "1", "on", "on_miss", "true", "yes") else "off"
        if mode not in ("off", "on_miss"):
            return {"success": False, "error": (
                f"semantic={semantic!r} is not one of 'off' | 'on_miss'")}
        if rank not in (None, "default", "relevance"):
            return {"success": False, "error": f"rank={rank!r} is not one of 'default' | 'relevance'"}
        if rank == "relevance":
            if not text or not str(text).strip():
                return {"success": False, "error": "rank='relevance' needs non-empty `text` (it ranks against the query)"}
            if not corpus:
                return {"success": False, "error": (
                    "rank='relevance' needs a named `corpus` (BM25 scores are not comparable across corpora)")}
            if order_by is not None:
                return {"success": False, "error": "rank='relevance' and order_by are exclusive: pick one ordering"}
        res = self._find_exact(corpus=corpus, domain=domain, function=function, service=service,
                               prefix=prefix, status=status, text=text, since=since, until=until,
                               date_property=date_property, area=area, token_budget=token_budget,
                               offset=offset, tier=tier, limit=limit, order_by=order_by,
                               include_reverse_digest=include_reverse_digest,
                               rank=("relevance" if rank == "relevance" else None), cq=cq,
                               _path_part=path_part)
        if path_part is not None and res.get("success", True) and "error" not in res:
            res["file_source"] = file_source
        if rank == "relevance" and res.get("success", True) and "error" not in res:
            res["rank_applied"] = {"by": "bm25", "scope": "the exact match set only",
                                   "membership_changed": False}
        ok = res.get("success", True) and "error" not in res
        if cq is not None and ok:
            # [FIND-QUERY-SURFACE §5] the echo + warnings exist iff a new parameter was used
            res["query_interpreted"] = cq.echo(text=text, filters=dict(
                prefix=prefix, status=status, area=area, domain=domain, function=function,
                service=service, since=since, until=until,
                date_property=((date_property or self.date_property) if (since or until) else None)))
            warns = cq.warnings()
            if warns:
                res["warnings"] = warns
        if (word_fallback and ok and text and corpus and res.get("total_matched") == 0
                and _qs.fallback_enabled()):
            blk_wf = self._word_fallback(
                corpus, text, domain=domain, function=function, service=service, prefix=prefix,
                status=status, area=area, since=since, until=until, date_property=date_property,
                cq=cq)
            if blk_wf is not None:
                res["matched_all_words"] = blk_wf
        if mode != "on_miss" or not text or not res.get("success", True) or res.get("total_matched") != 0:
            return res
        blk = self._did_you_mean(
            corpus, text, domain=domain, function=function, service=service, prefix=prefix,
            status=status, area=area, since=since, until=until, date_property=date_property, cq=cq)
        res["did_you_mean_semantic"] = {**self._preface(blk), **blk}
        ext = res.get("extent")
        if isinstance(ext, dict):
            ext["scope_note"] = ((ext.get("scope_note") or "") + " This zero covers exact matching "
                                 "only; `did_you_mean_semantic` holds suggestions, which are not "
                                 "matches and not evidence either way.").strip()
        return res

    @staticmethod
    def _path_refusal(path, corpus, semantic, rank, source_file, include_reverse_digest):
        """[A-E7] what has no meaning for a lone file. Messages name the PARAMETER, never the path."""
        if corpus:
            return "`path` and `corpus` are exclusive: name one source"
        if semantic not in (None, "off"):
            return "`semantic` must be 'off' (or omitted) with `path`: there is no index to suggest from"
        if rank not in (None, "default"):
            return "rank='relevance' is not available with `path`: there are no corpus statistics for one file"
        if source_file is not None:
            return "`source_file` is meaningless with `path` (one file): drop it"
        if include_reverse_digest:
            return "`include_reverse_digest` needs a registered corpus: not available with `path`"
        return None

    def _word_fallback(self, corpus, text, *, domain, function, service, prefix, status, area,
                       since, until, date_property, cq) -> Optional[dict]:
        """[FIND-QUERY-SURFACE §6] the phrase-zero fallback block, or None when it does not
        apply. The exact fields are never touched; this only builds the additive
        `matched_all_words`. Words = word-mode words of `text` minus lexrank's STOP list, each
        applied as its own all_of word term ANYWHERE in the node's structure; every other filter
        of the call (including the caller's own terms and where) applies."""
        if _qs.is_id_shaped(text):
            return None
        words = _qs.fallback_words(text)
        if len(words) < 2:
            return None
        try:
            part, err = self._gate(corpus)
            if err is not None:
                return None
            dprop = date_property or self.date_property
            cq2 = (cq if cq is not None else _qs.empty_query()).derive_with_all_of(
                _qs.fallback_terms(words))
            if since or until:
                cq2.date = (since, until, dprop)
            cq2.bind(part, self._qs_caches)
            ids = []
            for nid, nd in part.nodes.items():
                if _node_matches(nd, nid, domain, function, service, prefix, status, None,
                                 area=area, cq=cq2) is None:
                    continue
                if not _date_in_range(nd, since, until, dprop):
                    continue
                ids.append(nid)
            ranking = None
            try:
                score = dict(self._lexrank_for(part).search(text, k=len(part.nodes)))
                ids.sort(key=lambda i: (-score.get(i, 0.0), i))
            except Exception as ex:  # noqa: BLE001 -- unavailable relevance is a label, never an error
                ids.sort()
                ranking = f"id-order (relevance unavailable: {type(ex).__name__})"
            shown = ids[:_qs.FALLBACK_CAP]
            blk = {"kind": "word-and-fallback", "is_exact_match": False, "is_assertion": False,
                   "note": ("No field contains your text as one phrase; these nodes contain every "
                            "query word (whole words, no stemming) in their id/title/properties. "
                            "Ranked by keyword relevance."),
                   "query_words": words, "total": len(ids),
                   "nodes": [{"rank": r, "id": i, "title": part.nodes[i].get("title")}
                             for r, i in enumerate(shown, 1)],
                   "capped": len(ids) > len(shown)}
            if ranking:
                blk["ranking"] = ranking
            return blk
        except Exception as ex:  # noqa: BLE001 -- the fallback never touches the exact answer
            logger.warning("[lts-engine] word fallback failed: %s: %s", type(ex).__name__, ex)
            return None

    def _preface(self, block) -> dict:
        """[SEM-SESSION-PREFACE] `{"preface": ...}` for the first ok block WITH candidates in this
        process, else `{}`. Unavailable / skipped / empty blocks never consume it."""
        if block.get("status") != "ok" or not block.get("candidates"):
            return {}
        with self._semantic_preface_lock:      # one test-and-set, always under the lock
            if self._semantic_preface_shown:
                return {}
            self._semantic_preface_shown = True
        return {"preface": SEMANTIC_PREFACE}

    def _did_you_mean(self, corpus, text, domain=None, function=None, service=None, prefix=None,
                      status=None, area=None, since=None, until=None, date_property=None,
                      cq=None) -> dict:
        base = {"mode": "semantic", "kind": "semantic-suggestions-not-matches", "is_assertion": False}
        if not corpus:
            return {**base, "status": "skipped", "candidates": [], "reason": NAMED_CORPUS_REASON}
        try:
            part, err = self._gate(corpus)
            if err is not None:
                return {**base, "status": "unavailable", "candidates": [], "reason": "corpus-not-servable"}
            if self.semantic is None:
                from .semantic.suggest import SemanticService
                self.semantic = SemanticService()

            dprop = date_property or self.date_property
            if cq is not None:
                cq.bind(part, self._qs_caches)

            def keep(nid, nd):   # [review F10] EVERY filter the exact query applied, except text
                return (_node_matches(nd, nid, domain, function, service, prefix, status, None,
                                      area=area, cq=cq) is not None
                        and _date_in_range(nd, since, until, dprop))
            return self.semantic.suggest(part, str(text), k=5, keep=keep)
        except Exception as ex:  # noqa: BLE001 -- R5: a suggestion fault never touches the exact answer
            return {**base, "status": "unavailable", "candidates": [], **_semantic_fault(ex)}

    def _find_exact(self, corpus=None, domain=None, function=None, service=None,
                    prefix=None, status=None, text=None, since=None, until=None,
                    date_property=None, area=None, token_budget=None, offset: int = 0,
                    tier=None, limit=None, order_by=None,
                    include_reverse_digest: bool = False, rank=None, cq=None,
                    _path_part=None) -> dict:
        """Find by taxonomy/prefix/status/text.

        `tier` ([LTS-TIER]) is ADDITIVE; None = the LEGACY path, unchanged. See
        `traverse` and `tiers.py` for the tier contracts.

        `limit`/`order_by` ([QUERY-LIMIT-ORDER]) are ALSO additive: absent/None for
        both leaves the response byte-identical to today ([SPEC-BACKWARD-COMPAT]).
        See `ordering.py` for the pipeline stage (filter -> order -> limit) they add.

        `include_reverse_digest` ([LTS-REVERSE-LOOKUP], default False) is ALSO
        additive: omitted, the response is BYTE-IDENTICAL to today. Set True and every
        emitted node/digest-row carries a `reverse_digest` — `total_referrers`/
        `returned`/`omitted`/`truncated` + `see_also` — a CAPPED preview, never the
        complete answer ([R5]); call `lts_reverse` for that.
        """
        try:
            tier = resolve_tier(tier)
        except TierError as te:
            return te.payload
        budget = self._budget(token_budget)
        dprop = date_property or self.date_property
        try:
            # `"recency"` is sugar for `{field: dprop, direction: "desc"}`
            # ([PARAM-ORDER-BY]) — `dprop` must be resolved first.
            resolved_order = resolve_order_by(order_by, dprop)
        except OrderError as oe:
            return oe.payload
        limit, limit_err = _resolve_limit(limit)
        if limit_err is not None:
            return limit_err
        fanout = not corpus and _path_part is None
        # [A-E7] a path find's field caches are PER CALL and never shared with a corpus partition
        caches = _qs.FieldCaches() if _path_part is not None else self._qs_caches
        if _path_part is not None:
            parts = [_path_part]
        elif corpus:
            part, err = self._gate(corpus)
            if err is not None:
                return err
            parts = [part]
        else:
            # [LEC-SELF-HEAL] the multi-corpus arm goes through the SAME choke point, so
            # an un-scoped `find` heals every stale corpus it touches instead of silently
            # dropping it into `stale_corpora` forever. [INV-PRIVATE-FANOUT] it never
            # reads a private corpus: those are read only when named.
            parts, stale_corpora, private_corpora = self._fanout_parts()
        if cq is not None:
            sf_err = cq.validate_source_file(parts, area)
            if sf_err:
                return {"success": False, "error": sf_err}
            if since or until:
                cq.date = (since, until, dprop)
        matches: List[Tuple[str, dict]] = []
        # [FL-RECALL-TWO-VOCABULARIES]: which lifecycle key actually answered, per node,
        # so a corpus keyed on `lifecycle-position` stops looking like a corpus with one
        # `status` hit. Reported as `matched_via` on the node and rolled up in `extent`.
        via_counts: Dict[str, int] = {}
        for part in parts:
            if cq is not None:
                cq.bind(part, caches)
            for nid, nd in part.nodes.items():
                hit = _node_matches(nd, nid, domain, function, service,
                                    prefix, status, text, area=area, cq=cq)
                if hit is None:
                    continue
                if not _date_in_range(nd, since, until, dprop):
                    continue
                if hit.get("matched_via"):
                    via_counts[hit["matched_via"]] = via_counts.get(hit["matched_via"], 0) + 1
                matches.append((part.corpus_id, nd if not hit.get("matched_via")
                                else {**nd, "matched_via": hit["matched_via"]}))
        matches.sort(key=lambda cm: (cm[0], cm[1]["id"]))
        if rank == "relevance" and text and corpus:
            # [S5-RANK-RELEVANCE] re-order the SAME set by BM25 before order/limit/paging.
            score = dict(self._lexrank_for(parts[0]).search(text, k=len(parts[0].nodes)))
            matches.sort(key=lambda cm: (-score.get(cm[1]["id"], 0.0), cm[0], cm[1]["id"]))
        # [QUERY-LIMIT-ORDER] filter -> order -> limit ([SPEC-ORDER-OF-OPERATIONS]).
        # `total_matched_raw` is the count BEFORE order_by/limit and is what every
        # `total_matched`/`truncated` field below is measured against, unchanged from
        # today ([INV-DECLARED-REACH]: "a count describes the corpus, not the
        # response") — `windowed` is what the TIER ceilings below are re-based on.
        total_matched_raw = len(matches)
        windowed, order_applied = order_and_window(
            matches, order_by=resolved_order, limit=limit,
            get_id=lambda cm: (cm[0], cm[1]["id"]),
            get_properties=lambda cm: cm[1].get("properties") or {})
        filters = {"since": since, "until": until, "date_property": dprop, "area": area}
        skipped = ([] if not fanout else
                   [{"corpus": c, "reason": "stale"} for c in stale_corpora]
                   + _private_skips(private_corpora))
        # [FL-PROSE-BLIND] the routing pointer. NOTHING above this line changed: the match
        # set is byte-for-byte what it was, and the whole of the fix is that the response
        # now says what it did not read. The count is index-derived (`line_range` compare,
        # no file I/O) so declaring the scope costs a `find` nothing.
        scope_note = None
        if text:
            scope_note = _prose_scope_note(
                sum(p.nodes_with_unread_body() for p in parts),
                sum(len(p.nodes) for p in parts))
        kc = _keys_consulted(domain, function, service, prefix, status,
                             text, area, dprop, bool(since or until))
        sfilters = {k: v for k, v in
                    dict(domain=domain, function=function, service=service,
                         prefix=prefix, status=status, text=text, area=area,
                         since=since, until=until).items() if v is not None}
        if cq is not None:
            cq.finalize(parts)
            kc = kc + [k for k in cq.keys_consulted() if k not in kc]
            sfilters.update(cq.scope_filters())
        extent = self._extent(
            parts,
            keys_consulted=kc,
            skipped=skipped,
            scope={"tool": "lts_find",
                   "corpus": ("(one file, by path)" if _path_part is not None else corpus or "ALL LOADED"),
                   "filters": sfilters},
            notes=([f"lifecycle sweep matched via {via_counts}"] if via_counts else []),
            body_scanned=False,
            scope_note=scope_note,
        )

        # --- [LTS-TIER] digest: EVERY node of `windowed`, no bodies, no offset ---
        # [TIER-DIGEST-LIMIT]: the ceiling is evaluated against `len(windowed)`
        # (post-`limit`), never `total_matched_raw` — that is the entire point of
        # `limit` rescuing an oversized digest.
        if tier == TIER_DIGEST:
            try:
                assert_digestable(len(windowed), offset=offset, what="match set")
            except TierError as te:
                return te.payload
            # One degree function per partition touched — `find` spans corpora, and a
            # node's degree is only meaningful inside its own partition
            # ([INV-LTS-CORPUS-ISOLATION]: there is no global graph to count against).
            degree_fns = {}
            for p in parts:
                fwd_adj, rev_adj = p.adjacency()
                degree_fns[p.corpus_id] = incident_degree_fn(fwd_adj, rev_adj)
            rows = [digest_row(nd, edge_count=degree_fns[cid](nd),
                               date_property=dprop, corpus=cid)
                    for cid, nd in windowed]
            if include_reverse_digest:
                for row in rows:
                    row["reverse_digest"] = self._reverse_digest(row["corpus"], row["id"])
            count = len(rows)
            out = {"success": True, "tier": TIER_DIGEST, "digest": rows,
                   "count": count, "total_matched": total_matched_raw,
                   "complete": True, "truncated": count < total_matched_raw,
                   "cursor": None, "filters": filters, **extent}
            out.update(result_metadata(limit=limit, order_applied=order_applied,
                                       total_matched=total_matched_raw,
                                       windowed_len=len(windowed), count=count,
                                       cursor=None))
            if fanout:
                out["stale_corpora"] = stale_corpora
            return out

        # --- [LTS-TIER] full: the complete WINDOWED set, or an ERROR -------------
        # [TIER-FULL-LIMIT]: `limit` narrows the candidate set the ceiling is
        # evaluated against; it never relaxes the ceiling itself.
        if tier == TIER_FULL:
            try:
                assert_full_fits(windowed, budget,
                                 token_of=lambda cm: cm[1].get("token_count") or 1,
                                 what="match set")
            except TierError as te:
                return te.payload
            emitted, cursor = windowed, None
            # `full` never pages, so its only source of a fraction is `limit` itself
            # — see the digest branch's identical reasoning.
            truncated = len(emitted) < total_matched_raw
        else:
            # `page` and the LEGACY path (tier is None) page WITHIN `windowed`
            # ([SPEC-PAGINATION-COMPOSITION]). `cursor` is ALWAYS window-relative —
            # null once the window (== the whole match set when `limit`/`order_by`
            # are absent) is exhausted, which is what [SCEN-PAGE-CURSOR-NEVER-
            # ESCAPES-LIMIT] requires and what makes an UNMODIFIED caller's cursor
            # progression byte-identical to today at every offset.
            #
            # `truncated` is the one field that must mean two different things
            # depending on whether the caller opted in:
            #   * UNMODIFIED (no `limit`, no `order_by`): `windowed is matches`
            #     byte-for-byte, so `truncated` MUST stay exactly today's
            #     window-relative "is there more to page to from here" — the
            #     [SPEC-BACKWARD-COMPAT] guarantee holds at every `offset`, not
            #     only `offset=0`.
            #   * MODIFIED: [SCEN-RESULT-METADATA-DISTINGUISHES-LIMIT-FROM-COMPLETE]
            #     requires the GLOBAL `count < total_matched_raw` — "newest 30 of
            #     108" must read `truncated: true` even when the whole 30-node
            #     window fits in one page (`cursor: null`). The two fields
            #     legitimately diverge once `limit`/`order_by` is in play; that is
            #     not a bug, it is `bound_reason` earning its place in the response.
            emitted, window_truncated, cursor, _win_total = self._budget_slice(
                windowed, offset, budget, keyfn=lambda cm: cm[1])
            if limit is None and resolved_order is None:
                truncated = window_truncated
            else:
                truncated = len(emitted) < total_matched_raw

        nodes = [{"corpus": cid, **nd} for cid, nd in emitted]
        if include_reverse_digest:
            for nd_out in nodes:
                nd_out["reverse_digest"] = self._reverse_digest(nd_out["corpus"], nd_out["id"])
        count = len(nodes)
        out = {"success": True, "nodes": nodes, "count": count,
               "total_matched": total_matched_raw, "truncated": truncated,
               "cursor": cursor, "filters": filters, **extent}
        if tier is not None:
            # `complete` is "is the WINDOW exhausted" — `cursor is None` — NOT
            # `not truncated`. Once `limit`/`order_by` is set, `truncated` answers a
            # GLOBAL question ("is this less than the whole corpus") while `complete`
            # must keep answering a WINDOW question ("did I get everything this tier
            # promised"); `full`'s successful case and a paged call's exhausted last
            # page both have `cursor is None` and must read `complete: true` even
            # though `truncated` can legitimately be `true` at the same time (found
            # by the independent gap-analyzer — P0, both cases now regression-tested).
            out.update({"tier": tier, "offset": max(0, offset),
                        "next_offset": cursor, "complete": cursor is None})
        out.update(result_metadata(limit=limit, order_applied=order_applied,
                                   total_matched=total_matched_raw,
                                   windowed_len=len(windowed), count=count,
                                   cursor=cursor))
        if fanout:
            out["stale_corpora"] = stale_corpora
        return out

    # --- lts_find_in_prose ---------------------------------------------------
    def find_in_prose(self, text: str, corpus=None, domain=None, function=None,
                      service=None, prefix=None, status=None, area=None,
                      since=None, until=None, date_property=None,
                      body_only: bool = False, context: int = 0,
                      token_budget=None, offset: int = 0, tier=None,
                      all_of=None, any_of=None, none_of=None, match=None, where=None,
                      fields=None, source_file=None, document_tag=None, explain=False) -> dict:
        """[FL-PROSE-BLIND] Find a term in the BODY TEXT of nodes — the scope `lts_find`
        does not read.

        ⛔ A DIAGNOSTIC, NOT A BETTER `lts_find`. Reaching for this tool is a deliberate
        second act, and THAT ACT IS THE SIGNAL: it means the structure did not answer the
        caller's question. The response is shaped to keep it that way — it returns
        LOCALITY (id, title, file, line, the matching line) rather than content, so acting
        on a hit still costs an `lts_node`, and the pressure that makes authors move facts
        into properties is not relieved by making prose feel equally good.

        THE FLAG IS MOST OF THE VALUE. Every hit is classified:

            finding: "body-only"           the term is in the body and NOWHERE in id,
                                           title or any string property. `lts_find` with
                                           the same term DOES NOT RETURN THIS NODE. This is
                                           a DENSIFICATION FINDING — content the graph
                                           cannot answer for.
            finding: "also-in-structure"   `lts_find` already returns it; `structure_hit_in`
                                           names where. Redundant coverage, not a gap.

        The classifier is `_structure_hit_in`, the same walk `lts_find`'s own `_text_hit`
        performs, so `body-only` means EXACTLY "absent from the other tool's result set"
        and cannot drift from it.

        Matching is substring, case-insensitive — deliberately identical to `lts_find`'s,
        because the whole point is that the two answers are comparable. Every non-text
        filter is the SAME predicate `lts_find` applies, so a scoped structured query and
        its prose counterpart cover the same population.
        """
        import time
        started = time.monotonic()
        try:
            tier = resolve_tier(tier)
        except TierError as te:
            return te.payload
        needle = (text or "").strip()
        if not needle:
            # A prose sweep with no term is not a cheap query, it is the whole corpus.
            # Refuse rather than answer with everything.
            return {"success": False,
                    "error": ("lts_find_in_prose requires a non-empty `text` term — it "
                              "searches body prose, and an empty term matches every line "
                              "of every node. To enumerate a corpus use lts_find with "
                              "tier='digest'.")}
        # [FIND-QUERY-SURFACE §7] the new conditions are POPULATION filters here (the structural
        # gate shared with lts_find); `text` stays a body search.
        try:
            cq = _qs.compile_query(
                all_of=all_of, any_of=any_of, none_of=none_of, match=match, where=where,
                fields=fields, source_file=source_file, document_tag=document_tag,
                explain=explain, corpus=corpus,
                other_positive_filter=bool(domain or function or service or prefix or status
                                           or since or until or area or needle))
        except _qs.QueryError as qe:
            return {"success": False, "error": str(qe)}
        budget = self._budget(token_budget)
        dprop = date_property or self.date_property
        if corpus:
            part, err = self._gate(corpus)
            if err is not None:
                return err
            parts, stale_corpora = [part], []
        else:
            # Same choke point as `find`'s multi-corpus arm ([LEC-SELF-HEAL]): an
            # unscoped prose sweep heals every stale corpus it touches rather than
            # silently dropping it. [INV-PRIVATE-FANOUT] private corpora: named only.
            parts, stale_corpora, private_corpora = self._fanout_parts()

        if cq is not None:
            sf_err = cq.validate_source_file(parts, area)
            if sf_err:
                return {"success": False, "error": sf_err}
            if since or until:
                cq.date = (since, until, dprop)
        low = needle.lower()
        hits: List[Tuple[str, dict]] = []
        scanned_nodes = 0
        files_read = 0
        files_unreadable: List[dict] = []
        body_chars = 0
        for part in parts:
            if cq is not None:
                cq.bind(part, self._qs_caches)
            body, report = part.body_index()
            files_read += report["files_read"]
            files_unreadable.extend({**u, "corpus": part.corpus_id}
                                    for u in report["files_unreadable"])
            body_chars += report["body_chars"]
            for nid, nd in part.nodes.items():
                # THE SAME structural gate `find` applies, with text deliberately None:
                # the text term is answered against the BODY below, everything else is
                # answered identically, so the two tools scope over the same population.
                if _node_matches(nd, nid, domain, function, service,
                                 prefix, status, None, area=area, cq=cq) is None:
                    continue
                if not _date_in_range(nd, since, until, dprop):
                    continue
                rows = body.get(nid)
                if not rows:
                    continue
                scanned_nodes += 1
                matched_rows = [r for r in rows if low in r[2]]
                if not matched_rows:
                    continue
                where = _structure_hit_in(nd, nid, needle)
                if body_only and where:
                    continue
                hits.append((part.corpus_id,
                             _prose_hit(nd, nid, rows, matched_rows, where, context)))
        hits.sort(key=lambda ch: (ch[0], ch[1]["id"]))
        body_only_count = sum(1 for _c, h in hits if h["finding"] == "body-only")
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)

        filters = {"text": needle, "body_only": bool(body_only), "since": since,
                   "until": until, "date_property": dprop, "area": area}
        skipped = ([] if corpus else
                   [{"corpus": c, "reason": "stale"} for c in stale_corpora]
                   + _private_skips(private_corpora))
        scanned = bool(files_read) or not parts
        kc = ["body:prose", "body:property-lines"]
        sfilters = {k: v for k, v in
                    dict(domain=domain, function=function, service=service,
                         prefix=prefix, status=status, text=needle, area=area,
                         since=since, until=until,
                         body_only=body_only or None).items() if v is not None}
        if cq is not None:
            cq.finalize(parts)
            kc = kc + [k for k in cq.keys_consulted() if k not in kc]
            sfilters.update(cq.scope_filters())
        extent = self._extent(
            parts,
            keys_consulted=kc,
            skipped=skipped,
            scope={"tool": "lts_find_in_prose", "corpus": corpus or "ALL LOADED",
                   "filters": sfilters},
            notes=[f"body scan: {scanned_nodes} nodes with body, {files_read} files read, "
                   f"{body_chars} body chars, {elapsed_ms} ms"],
            body_scanned=scanned,
            scope_note=(
                "BODY SCOPE ONLY — this read source lines beneath each node's heading. It "
                "does NOT apply lts_find's structured predicate, so a node asserting the "
                "term in a property and never mentioning it in prose is ABSENT here. "
                "`finding: body-only` marks the hits lts_find(text=...) does not return; "
                "`also-in-structure` marks the ones it already does."),
        )
        out_common = {
            "success": True, "filters": filters,
            "body_only_count": body_only_count,
            "also_in_structure_count": len(hits) - body_only_count,
            "nodes_scanned": scanned_nodes, "elapsed_ms": elapsed_ms,
            **({"files_unreadable": files_unreadable} if files_unreadable else {}),
            **extent,
        }
        if not corpus:
            out_common["stale_corpora"] = stale_corpora
        if cq is not None:
            out_common["query_interpreted"] = cq.echo(text=needle, filters=dict(
                prefix=prefix, status=status, area=area, domain=domain, function=function,
                service=service, since=since, until=until,
                date_property=((date_property or self.date_property) if (since or until) else None)))
            out_common["query_interpreted"]["text"]["mode"] = (
                "BODY search: the text as one case-insensitive substring of one body line; "
                "the other conditions below are POPULATION filters")
            _w = cq.warnings()
            if _w:
                out_common["warnings"] = _w

        # --- [LTS-TIER] digest: EVERY hit, IDENTITY AND COUNTS, NO BODY TEXT -----
        # ⛔ The one rule a prose tool could most easily break. A digest row here carries
        # the node's routing identity and HOW MANY lines matched — never a matching line,
        # never an excerpt. `_prose_digest_row` is the only builder, so there is one place
        # for that promise to be kept and one place to test it.
        if tier == TIER_DIGEST:
            try:
                assert_digestable(len(hits), offset=offset, what="prose hit set")
            except TierError as te:
                return te.payload
            rows = [_prose_digest_row(cid, h) for cid, h in hits]
            return {**out_common, "tier": TIER_DIGEST, "digest": rows,
                    "count": len(rows), "total_matched": len(hits),
                    "complete": True, "truncated": False, "cursor": None}

        if tier == TIER_FULL:
            try:
                assert_full_fits(hits, budget,
                                 token_of=lambda ch: ch[1]["_tokens"], what="prose hit set")
            except TierError as te:
                return te.payload
            emitted, truncated, cursor, total = hits, False, None, len(hits)
        else:
            emitted, truncated, cursor, total = self._budget_slice(
                hits, offset, budget, keyfn=lambda ch: {"token_count": ch[1]["_tokens"]})
        out = {**out_common,
               "hits": [{"corpus": cid, **{k: v for k, v in h.items() if k != "_tokens"}}
                        for cid, h in emitted],
               "count": len(emitted), "total_matched": total,
               "truncated": truncated, "cursor": cursor}
        if tier is not None:
            out.update({"tier": tier, "offset": max(0, offset),
                        "next_offset": cursor, "complete": not truncated})
        return out

    # --- lts_node -----------------------------------------------------------
    @staticmethod
    def _caution_field(part, node_id, fwd_adj, rev_adj) -> dict:
        """[CANONICAL-EDGE-CAUTION] `{"caution": {...}}` for a legacy/superseded node, else {} (so a
        non-triggering node's response is byte-identical to before)."""
        c = caution_for(part, node_id, fwd_adj, rev_adj)
        return {"caution": c} if c else {}

    def node(self, corpus: str, node_id: str, token_budget=None, tier=None,
            include_reverse_digest: bool = False) -> dict:
        """Fetch one node.

        `tier` ([LTS-TIER]) is ADDITIVE; None = the LEGACY path, unchanged.
        `digest` = the routing row + a COMPLETE edge map and no body of any kind (no
        properties, no source excerpt). `full` = today's payload but it ERRORS if the
        edge map or the excerpt would have been truncated. `page` is accepted and
        behaves as the legacy slice, labelled — a single node has nothing to page.

        `include_reverse_digest` ([LTS-REVERSE-LOOKUP], default False) is ADDITIVE:
        omitted, the response is BYTE-IDENTICAL to today. Set True and the response
        carries a `reverse_digest` — a CAPPED preview of `lts_reverse`'s answer, never
        the complete one ([R5]).
        """
        try:
            tier = resolve_tier(tier)
        except TierError as te:
            return te.payload
        part, err = self._gate(corpus)
        if err is not None:
            return err
        extent = self._extent(
            [part], keys_consulted=["id"],
            scope={"tool": "lts_node", "corpus": corpus, "node_id": node_id})
        nd = part.get_node(node_id)
        if nd is None:
            # ⛔ A NOT-FOUND NODE IS THE SINGLE-NODE FORM OF THE AMBIGUOUS ZERO, so it
            # gets the same treatment: the extent says how many nodes were in scope and
            # when the index was built, and `did_you_mean` discriminates a typo from a
            # genuine absence without a second call.
            out = {"success": True, "corpus": corpus, "node": None,
                   "note": f"node {node_id!r} not found in corpus {corpus!r}",
                   "did_you_mean": _did_you_mean(node_id, part.nodes), **extent}
            if tier == TIER_DIGEST:
                out.pop("node")
                out.update({"tier": tier, "digest": None, "complete": True})
            elif tier is not None:
                out.update({"tier": tier, "complete": True})
            return out
        budget = self._budget(token_budget)
        edges: Dict[str, dict] = {}
        routed_nodes: List[dict] = []
        raw_fwd: List[Tuple[str, str]] = []
        raw_rev: List[Tuple[str, str]] = []
        # [LEC-FRESHNESS] #2: only the edges incident to this node, from the cache.
        fwd_adj, rev_adj = part.adjacency()
        routed_withheld: List[dict] = []
        for layer, edge in fwd_adj.get(node_id, ()):
            if self._is_cross(part, edge):
                kind, payload = self._route(edge, origin_corpus=corpus)
                if kind == "node":
                    routed_nodes.append(payload)
                elif kind == "withheld":
                    routed_withheld.append(payload)
            else:
                raw_fwd.append((layer, edge["target"]))
        for layer, edge in rev_adj.get(node_id, ()):
            if not self._is_cross(part, edge):
                raw_rev.append((layer, edge["source"]))
        combined = ([("f", l, t) for (l, t) in raw_fwd]
                    + [("r", l, s) for (l, s) in raw_rev])

        # --- [LTS-TIER] digest: the routing row, a COMPLETE edge map, NO body -----
        # The edge map is built from `combined` in full (no `_budget_slice`): endpoint
        # ids are structure, not content, and completeness is the tier's contract.
        if tier == TIER_DIGEST:
            degree_of = incident_degree_fn(fwd_adj, rev_adj)
            for direction, layer, endpoint_id in combined:
                slot = edges.setdefault(layer, {"forward": [], "reverse": []})
                slot["forward" if direction == "f" else "reverse"].append(endpoint_id)
            digest_out = {"success": True, "corpus": corpus, "tier": TIER_DIGEST,
                    "digest": digest_row(nd, edge_count=degree_of(nd),
                                         date_property=self.date_property),
                    "edges": edges,
                    **self._caution_field(part, node_id, fwd_adj, rev_adj),
                    "routed_digest": digest_rows(
                        routed_nodes, date_property=self.date_property,
                        corpus_fn=lambda n: n.get("corpus")),
                    "complete": True, "truncated": False,
                    "edge_cursor": None, "edge_total": len(combined)}
            if include_reverse_digest:
                digest_out["reverse_digest"] = self._reverse_digest(corpus, node_id)
            if routed_withheld:   # [INV-PRIVATE-FANOUT] additive: absent when empty
                digest_out["routed_withheld"] = routed_withheld
            return digest_out

        emitted_ep, edges_truncated, edge_cursor, edge_total = self._budget_slice(
            combined, 0, budget, keyfn=lambda ep: {"token_count": 1})
        for direction, layer, endpoint_id in emitted_ep:
            slot = edges.setdefault(layer, {"forward": [], "reverse": []})
            slot["forward" if direction == "f" else "reverse"].append(endpoint_id)
        excerpt, excerpt_truncated = self._source_excerpt(part, nd, token_budget, tier=tier)
        truncated = excerpt_truncated or edges_truncated

        # --- [LTS-TIER] full: complete or ERROR. A clipped excerpt is a fraction ---
        if tier == TIER_FULL and truncated:
            return {"success": False, "reason": "full_ceiling_exceeded",
                    "tier": TIER_FULL, "corpus": corpus, "node_id": node_id,
                    "edge_total": edge_total, "token_budget": budget,
                    "excerpt_truncated": excerpt_truncated,
                    "edges_truncated": edges_truncated,
                    "error": ("tier='full' cannot return a COMPLETE node "
                              f"{node_id!r} within token_budget={budget} "
                              f"(edges_truncated={edges_truncated}, "
                              f"excerpt_truncated={excerpt_truncated}). `full` never "
                              "truncates. Raise `token_budget`, or use tier='digest' "
                              "to see the node's shape without its body.")}

        out = {"success": True, "corpus": corpus, "node": nd, "edges": edges,
               **self._caution_field(part, node_id, fwd_adj, rev_adj),
               "routed_nodes": routed_nodes,
               "source_file": nd.get("source_file"), "line_range": nd.get("line_range"),
               "source_excerpt": excerpt,
               "truncated": truncated,
               "edge_cursor": edge_cursor, "edge_total": edge_total, **extent}
        if tier is not None:
            out.update({"tier": tier, "complete": not truncated})
        if include_reverse_digest:
            out["reverse_digest"] = self._reverse_digest(corpus, node_id)
        if routed_withheld:   # [INV-PRIVATE-FANOUT] additive: absent when empty
            out["routed_withheld"] = routed_withheld
        return out

    # --- lts_traverse -------------------------------------------------------
    def traverse(self, corpus: str, start_node: str, edge_types=None, layers=None,
                 direction: str = "both", max_depth: int = 1, since=None, until=None,
                 date_property=None, area=None, token_budget=None, offset: int = 0,
                 tier=None, limit=None, order_by=None) -> dict:
        """Bounded typed-edge walk.

        `tier` ([LTS-TIER], see `tiers.py`) is ADDITIVE and defaults to None = the
        LEGACY path, byte-identical to the pre-tier engine. `digest` returns EVERY node
        with no bodies; `page` returns one explicit page of full content; `full` returns
        the complete result or ERRORS rather than truncating.

        `limit`/`order_by` ([QUERY-LIMIT-ORDER]) are ALSO additive: `limit` truncates
        the DISCOVERED set (after `max_depth`/traversal policy already bounded
        exploration); `order_by` overrides the default BFS discovery order when
        supplied, and both are no-ops when omitted ([SPEC-BACKWARD-COMPAT]).
        """
        try:
            tier = resolve_tier(tier)
        except TierError as te:
            return te.payload
        part, err = self._gate(corpus)
        if err is not None:
            return err
        traverse_extent = self._extent(
            [part], keys_consulted=["id", "edges.type", "edges.layer"],
            scope={"tool": "lts_traverse", "corpus": corpus, "start_node": start_node,
                   "max_depth": max_depth, "direction": direction,
                   "edge_types": list(edge_types) if edge_types else None,
                   "layers": list(layers) if layers else None})
        if not part.has_node(start_node):
            out = {"success": True, "corpus": corpus, "start_node": start_node,
                   "nodes": [], "edges": [], "truncated": False, "cursor": None,
                   "note": "start_node not found in corpus",
                   "did_you_mean": _did_you_mean(start_node, part.nodes),
                   **traverse_extent}
            if tier == TIER_DIGEST:
                out["digest"] = out.pop("nodes")
                out.update({"tier": tier, "count": 0, "total": 0, "complete": True})
            elif tier is not None:
                out.update({"tier": tier, "count": 0, "total": 0, "complete": True})
            return out
        budget = self._budget(token_budget)
        dprop = date_property or self.date_property
        try:
            resolved_order = resolve_order_by(order_by, dprop)
        except OrderError as oe:
            return {**oe.payload, "corpus": corpus, "start_node": start_node}
        limit, limit_err = _resolve_limit(limit)
        if limit_err is not None:
            return {**limit_err, "corpus": corpus, "start_node": start_node}
        direction = direction or "both"
        et = set(edge_types) if edge_types else None

        visited = {start_node}
        order: List[dict] = [part.get_node(start_node)]
        used_edges: List[dict] = []
        routed_nodes: List[dict] = []
        routed_stale: List[dict] = []
        routed_withheld: List[dict] = []   # [INV-PRIVATE-FANOUT]
        frontier: List[Tuple[str, int]] = [(start_node, 0)]

        # [LEC-FRESHNESS] #2: walk the CACHED incidence maps (rebuilt only on a real
        # content change) instead of rescanning every edge for every frontier node.
        fwd_adj, rev_adj = part.adjacency()
        layer_sel = set(layers) if layers else None

        while frontier:
            nid, depth = frontier.pop(0)
            if depth >= max_depth:
                continue
            # Edges incident to `nid` only — deduped so a self-loop is seen once.
            incident: List[Tuple[str, dict]] = []
            seen_edge_ids: set = set()
            for layer, edge in fwd_adj.get(nid, ()):
                if id(edge) not in seen_edge_ids:
                    seen_edge_ids.add(id(edge))
                    incident.append((layer, edge))
            for layer, edge in rev_adj.get(nid, ()):
                if id(edge) not in seen_edge_ids:
                    seen_edge_ids.add(id(edge))
                    incident.append((layer, edge))
            for layer, edge in incident:
                if layer_sel is not None and layer not in layer_sel:
                    continue
                fwd = edge["source"] == nid and direction in ("forward", "both")
                rev = edge["target"] == nid and direction in ("reverse", "both")
                if not (fwd or rev):
                    continue
                if et and edge["type"] not in et:
                    continue
                td = edge.get("target_domain")
                if fwd and td and td != part.domain_suffix:
                    used_edges.append(edge)
                    kind, payload = self._route(edge, origin_corpus=corpus)
                    if kind == "node":
                        routed_nodes.append(payload)
                    elif kind == "stale":
                        routed_stale.append(payload)
                    elif kind == "withheld":
                        routed_withheld.append(payload)
                    continue
                neighbor = edge["target"] if fwd else edge["source"]
                if neighbor in visited or not part.has_node(neighbor):
                    if part.has_node(neighbor) and neighbor in visited:
                        used_edges.append(edge)
                    continue
                visited.add(neighbor)
                order.append(part.get_node(neighbor))
                used_edges.append(edge)
                frontier.append((neighbor, depth + 1))

        # Optional time/area filter: keep the start node always; filter DISCOVERED nodes.
        if since or until or area:
            order = [n for n in order
                     if n["id"] == start_node
                     or (_date_in_range(n, since, until, dprop)
                         and (not area or n.get("area") == area))]
        filters = {"since": since, "until": until, "date_property": dprop, "area": area}

        # [QUERY-LIMIT-ORDER] filter -> order -> limit ([SPEC-ORDER-OF-OPERATIONS]).
        # `total_matched_raw` (the discovered-and-filtered count, BEFORE order_by/
        # limit) is what every `total`/`truncated` field below is measured against
        # unless a tier's own contract says otherwise ([TIER-PAGE-LIMIT]) — `windowed`
        # is what the TIER ceilings are re-based on ([SPEC-TIER-INTERACTION]).
        total_matched_raw = len(order)
        windowed, order_applied = order_and_window(
            order, order_by=resolved_order, limit=limit,
            get_id=lambda n: n["id"], get_properties=lambda n: n.get("properties") or {})

        # --- [LTS-TIER] the digest tier: ALWAYS COMPLETE, never a body -----------
        # It does NOT touch `_budget_slice`. There is no code path here by which a
        # digest could emit a subset — the guard raises, it does not trim — which is
        # the property that stops this tier from rebuilding the truncation defect one
        # level up with nicer packaging. [TIER-DIGEST-LIMIT]: the ceiling is evaluated
        # against `len(windowed)` (post-`limit`), never `total_matched_raw`.
        if tier == TIER_DIGEST:
            try:
                assert_digestable(len(windowed), offset=offset, what="traversal")
            except TierError as te:
                return {**te.payload, "corpus": corpus, "start_node": start_node}
            degree_of = incident_degree_fn(fwd_adj, rev_adj)
            rows = digest_rows(windowed, edge_count_fn=degree_of, date_property=dprop)
            all_ids = {n["id"] for n in windowed}
            edges = [e for e in used_edges
                     if e["source"] in all_ids and e["target"] in all_ids]
            count = len(rows)
            out = {"success": True, "corpus": corpus, "start_node": start_node,
                   "tier": TIER_DIGEST, "digest": rows, "edges": edges,
                   "routed_digest": digest_rows(
                       routed_nodes, date_property=dprop,
                       corpus_fn=lambda n: n.get("corpus")),
                   "routed_stale": routed_stale,
                   **({"routed_withheld": routed_withheld} if routed_withheld else {}),
                   "count": count, "total": total_matched_raw,
                   "complete": True, "truncated": count < total_matched_raw,
                   "cursor": None, "filters": filters, **traverse_extent}
            out.update(result_metadata(limit=limit, order_applied=order_applied,
                                       total_matched=total_matched_raw,
                                       windowed_len=len(windowed), count=count,
                                       cursor=None))
            return out

        # --- [LTS-TIER] the full tier: complete, or an ERROR. Never a fraction ---
        # [TIER-FULL-LIMIT]: `limit` narrows the candidate set the ceiling is
        # evaluated against; it never relaxes the ceiling itself.
        if tier == TIER_FULL:
            try:
                assert_full_fits(windowed, budget,
                                 token_of=lambda n: n.get("token_count") or 1,
                                 what="traversal")
            except TierError as te:
                return {**te.payload, "corpus": corpus, "start_node": start_node}
            emitted, cursor = windowed, None
            # `full` never pages, so its only source of a fraction is `limit` itself.
            truncated = len(emitted) < total_matched_raw
        else:
            # `page` and the LEGACY path (tier is None) page WITHIN `windowed`
            # ([SPEC-PAGINATION-COMPOSITION]). `cursor` is ALWAYS window-relative
            # (null once the window is exhausted — [SCEN-PAGE-CURSOR-NEVER-ESCAPES-
            # LIMIT], and BYTE-IDENTICAL to today's cursor progression at every
            # offset when `windowed is order`). `truncated` means two different
            # things depending on whether the caller opted in — see `find`'s
            # identical branch for the full reasoning: UNMODIFIED keeps today's
            # exact window-relative value ([SPEC-BACKWARD-COMPAT] at every offset);
            # MODIFIED uses the GLOBAL `count < total_matched_raw`
            # ([SCEN-RESULT-METADATA-DISTINGUISHES-LIMIT-FROM-COMPLETE]).
            emitted, window_truncated, cursor, _win_total = self._budget_slice(
                windowed, offset, budget, keyfn=lambda n: n)
            if limit is None and resolved_order is None:
                truncated = window_truncated
            else:
                truncated = len(emitted) < total_matched_raw

        emitted_ids = {n["id"] for n in emitted}
        edges = [e for e in used_edges
                 if e["source"] in emitted_ids and e["target"] in emitted_ids]
        out = {"success": True, "corpus": corpus, "start_node": start_node,
               "nodes": emitted, "edges": edges,
               "routed_nodes": routed_nodes, "routed_stale": routed_stale,
               **({"routed_withheld": routed_withheld} if routed_withheld else {}),
               **declare(emitted, truncated, cursor, total_matched_raw),
               # `total` is the pre-[INV-DECLARED-REACH] spelling of `total_matched`,
               # kept as an ALIAS so no existing caller breaks. New readers should use
               # the declared vocabulary; this key is the compatibility tail, not a
               # second contract.
               "total": total_matched_raw,
               "filters": filters, **traverse_extent}
        if tier is not None:
            # `complete` is "is the WINDOW exhausted" (`cursor is None`), NOT
            # `not truncated` — see `find`'s identical branch. `truncated` can stay
            # `true` (global, "less than the whole corpus") on the exhausted last
            # page of a limited traversal while `complete` correctly reads `true`
            # (found by the independent gap-analyzer — P0).
            out.update({"tier": tier, "count": len(emitted),
                        "offset": max(0, offset), "next_offset": cursor,
                        "complete": cursor is None})
        out.update(result_metadata(limit=limit, order_applied=order_applied,
                                   total_matched=total_matched_raw,
                                   windowed_len=len(windowed), count=len(emitted),
                                   cursor=cursor))
        return out

    # --- lts_capabilities ---------------------------------------------------
    def capabilities(self, corpus: str, topic=None, node_id=None, token_budget=None,
                     offset: int = 0) -> dict:
        """[INV-DECLARED-REACH]. `offset` was MISSING here while the response already
        returned a `cursor` and the shared schema text already promised "a cursor to
        resume from via `offset`" — a truncation declaration whose resume path did not
        exist, so the remainder of a bounded capability surface was unreachable. Found
        by the cross-surface sweep in `tests/test_declared_reach.py`, which is the whole
        reason that test is parametrized over every tool rather than written about lint.
        """
        part, err = self._gate(corpus)
        if err is not None:
            return err
        budget = self._budget(token_budget)
        anchors: List[str] = []
        # [ISSUE-ANCHOR-TRUNCATION] the topic->anchor match set was
        # capped at `[:5]` with NO signal that a 6th+ match existed — a silent bound the
        # rest of this response's own `truncated`/`cursor` machinery does not cover,
        # because it only ever sees the 5 that survived the slice. `anchor_matches_total`
        # is the true match count BEFORE the cap; `anchors_truncated` says whether the
        # cap actually bit. Both are 0/False-safe when `node_id`/an exact `topic` hit
        # already pins a single anchor (no ambiguity to lose there).
        anchor_matches_total = 0
        if node_id and part.has_node(node_id):
            anchors = [node_id]
            anchor_matches_total = 1
        elif topic and part.has_node(topic):
            anchors = [topic]
            anchor_matches_total = 1
        elif topic:
            all_topic_matches = [nid for nid, nd in part.nodes.items()
                                 if _text_hit(nd, nid, topic)]
            anchor_matches_total = len(all_topic_matches)
            anchors = all_topic_matches[:5]
        anchors_truncated = len(anchors) < anchor_matches_total

        anchor_set = set(anchors)
        edges: List[dict] = []
        caps: List[dict] = []
        routed_capabilities: List[dict] = []
        routed_withheld: List[dict] = []   # [INV-PRIVATE-FANOUT]
        seen: set = set()
        # [LEC-FRESHNESS] #2: gather only ANCHOR-incident edges from the cached maps
        # (deduped: an edge joining two anchors must be considered exactly once).
        fwd_adj, rev_adj = part.adjacency()
        incident: List[dict] = []
        seen_edge_ids: set = set()
        for anchor in anchors:
            for _layer, edge in list(fwd_adj.get(anchor, ())) + list(rev_adj.get(anchor, ())):
                if id(edge) not in seen_edge_ids:
                    seen_edge_ids.add(id(edge))
                    incident.append(edge)
        for edge in incident:
            if not (edge["source"] in anchor_set or edge["target"] in anchor_set):
                continue
            if self._is_cross(part, edge) and edge["source"] in anchor_set:
                kind, payload = self._route(edge, origin_corpus=corpus)
                if kind == "node":
                    edges.append(edge)
                    routed_capabilities.append(payload)
                elif kind == "withheld":
                    routed_withheld.append(payload)
                continue
            edges.append(edge)
            nb = edge["target"] if edge["source"] in anchor_set else edge["source"]
            if nb not in anchor_set and nb not in seen and part.has_node(nb):
                seen.add(nb)
                caps.append(part.get_node(nb))
        emitted, caps_truncated, cursor, total = self._budget_slice(
            caps, offset, budget, keyfn=lambda n: n)
        surface_ids = anchor_set | {n.get("id") for n in emitted}
        surface_edges = [e for e in edges
                         if e["source"] in surface_ids and e["target"] in surface_ids]
        edges_emitted, edges_truncated, edge_cursor, edge_total = self._budget_slice(
            surface_edges, 0, budget, keyfn=lambda e: e)
        truncated = caps_truncated or edges_truncated
        summary = (f"{len(emitted)} capability node(s) connect to "
                   f"{(node_id or topic)!r} in {corpus} across {len(edges_emitted)} edge(s)"
                   + (" (truncated)" if truncated else ""))
        return {"success": True, "corpus": corpus, "topic": topic, "node_id": node_id,
                "anchor_nodes": anchors,
                "anchor_matches_total": anchor_matches_total,
                "anchors_truncated": anchors_truncated,
                "capabilities": emitted,
                "routed_capabilities": routed_capabilities, "edges": edges_emitted,
                **({"routed_withheld": routed_withheld} if routed_withheld else {}),
                "summary": summary,
                **declare(emitted, truncated, cursor, total),
                "total": total,   # pre-[INV-DECLARED-REACH] alias; see `traverse`
                "edge_cursor": edge_cursor, "edge_total": edge_total,
                **self._extent(
                    [part],
                    keys_consulted=(["id", "title", "properties.*"] if topic and not anchors
                                    else ["id"]),
                    scope={"tool": "lts_capabilities", "corpus": corpus,
                           "topic": topic, "node_id": node_id,
                           "anchor_nodes": anchors})}

    # --- lts_flow ([LEC-RENDER] the RENDER layer's corpus-facing entry) ------
    def flow(self, corpus: str, start_node: str, edge_types=None, max_depth: int = 8,
             direction: str = "forward", membership: bool = True,
             group_by: Optional[str] = "actor", layout: str = "TD",
             include_container: bool = False, title=None,
             token_budget=None, offset: int = 0) -> dict:
        """Walk a flow subgraph and return BOTH the Mermaid text AND the structured walk.

        THE FRESHNESS CONTRACT: this goes through `_gate()` — the same choke point as
        find/node/traverse/capabilities — so a STALE corpus returns the loud
        [INV-LTS-FRESH] marker and NO `mermaid` key at all. A diagram is never rendered
        over content the engine cannot prove current; self-heal ([LEC-SELF-HEAL]) applies
        identically, because it lives inside that gate.

        `edge_types` overrides the flow spine, which is what makes this tool generic:
        `edge_types=['SUPERSEDES']` renders a supersession chain, `['DEPENDS_ON']` a
        dependency tree, through the SAME walk + the SAME emitter.
        """
        from .render import render_mermaid, walk_flow

        part, err = self._gate(corpus)
        if err is not None:
            return err
        if not part.has_node(start_node):
            return {"success": True, "corpus": corpus, "start_node": start_node,
                    "mermaid": None, "nodes": [], "edges": [], "membership_edges": [],
                    **declare([], False, None, 0), "total": 0,
                    "note": "start_node not found in corpus"}

        budget = self._budget(token_budget)
        try:
            depth = max(1, int(max_depth))
        except (TypeError, ValueError):
            depth = 8

        fwd_adj, rev_adj = part.adjacency()
        walk = walk_flow(start_node=start_node, get_node=part.get_node,
                         has_node=part.has_node, fwd_adj=fwd_adj, rev_adj=rev_adj,
                         edge_types=edge_types, membership=membership,
                         direction=direction or "forward", max_depth=depth)

        # Token budget over the walk's nodes, then induce the edge set on what survived
        # — identical discipline to `traverse`, so a huge flow degrades by truncation
        # rather than by blowing the consumer's context.
        emitted, truncated, cursor, total = self._budget_slice(
            walk.nodes, offset, budget, keyfn=lambda n: n)
        emitted_ids = {str(n["id"]) for n in emitted}
        edges = [e for e in walk.edges
                 if str(e["source"]) in emitted_ids and str(e["target"]) in emitted_ids]

        render_nodes = emitted
        if walk.container is not None and not include_container:
            render_nodes = [n for n in emitted if str(n["id"]) != walk.container]

        # Group only when the property is actually present — an empty `subgraph` block
        # for a corpus that does not use swimlanes would be noise.
        gb = group_by or None
        if gb and not any((n.get("properties") or {}).get(gb) for n in render_nodes):
            gb = None

        diagram = render_mermaid(render_nodes, edges, direction=layout,
                                 group_by=gb, title=title)
        return {"success": True, "corpus": corpus, "start_node": start_node,
                "mermaid": diagram.text,
                "nodes": emitted, "edges": edges,
                "membership_edges": walk.membership_edges,
                "container": walk.container,
                "node_ids": diagram.node_ids, "groups": diagram.groups,
                "skipped_edges": diagram.skipped_edges,
                **declare(emitted, truncated, cursor, total),
                "total": total,   # pre-[INV-DECLARED-REACH] alias; see `traverse`
                "max_depth_reached": walk.max_depth_reached,
                "diagram": {"format": "mermaid", "direction": layout,
                            "group_by": gb, "node_count": diagram.node_count,
                            "edge_count": diagram.edge_count}}

    # --- lts_preview ([FL-PREVIEW]) the AUTHORING feedback loop --------------
    # An engine METHOD as well as an MCP tool, because the loop it serves
    # (compose -> preview -> revise -> write) is a first-class engine capability, not
    # a transport affordance. It reads no partition and needs no freshness gate — a
    # draft is not corpus state — but it accepts the store so `corpus="<loaded id>"`
    # can resolve to that corpus's root the same way a query would.
    def preview(self, content: Optional[str] = None, *, path: Optional[str] = None,
                corpus: Optional[str] = None, replaces: Optional[str] = None,
                virtual_path: Optional[str] = None, profile=None,
                verbose: bool = False) -> dict:
        from .preview import preview as _preview
        return _preview(content, path=path, corpus=corpus, replaces=replaces,
                        virtual_path=virtual_path, profile=profile,
                        verbose=verbose, store=self.store)

    # --- lts_register_corpus / lts_unregister_corpus ([REG-RUNTIME-CORPUS]) -------
    # Spec: (internal design note)
    #
    # THE PROBLEM THIS CLOSES: the corpus set was built ONCE at `server.build_tools`
    # (`config.build_store()`), with no runtime path to add or drop a corpus — a new
    # corpus, a config-file edit, or a temp corpus that needs cleaning up all required a
    # full process RESTART, and a client `/mcp reconnect` re-attaches transport only; it
    # does not reload config or code. `register_corpus` builds ONE corpus (read its
    # config entry or an explicit ad hoc spec, parse it, seat it in the freshness
    # kernel) and calls `store.add_partition` — the SAME builder functions
    # (`config.build_partition` / `build_merged_partition`) the cold boot path uses, so a
    # runtime-registered corpus is byte-for-byte what a restart would have produced, not
    # a second, divergent code path. `unregister_corpus` is the exact inverse: it
    # unseats the kernel state and forgets the healer's per-corpus memory BEFORE
    # dropping the partition, so nothing is left half-tracked.
    def register_corpus(self, corpus_id: str, spec: Optional[dict] = None) -> dict:
        """Register + build + seat ONE corpus at runtime. No restart, no reconnect.

        `spec` is optional: when omitted, the corpus registry is RE-READ from disk
        (`config.default_corpora()` — the same registry (`factlattice.registry`) the
        server booted from, read fresh so a config-file edit made after boot is picked
        up) and `corpus_id`'s entry is looked up there. Pass `spec` explicitly to
        register an AD HOC corpus that has no config-file entry at all (e.g. a temp
        corpus like a one-off sample directory) — same shape as one `default_corpora()`
        value: either `{"roots": [{"path", "area"[, "only_file"]}, ...], "domain_suffix"}`
        (multi-root/area-labeled) or `{"root": "...", "domain_suffix": "..."}`
        (single-root).

        Refuses (returns `success: False`, changes NOTHING) when: `corpus_id` is already
        loaded (would clobber — use `unregister_corpus` first if you mean to replace it);
        no `spec` was given and none is found in the config registry; or the resolved
        root(s) parsed to ZERO nodes (missing directory / no `.lts` files — the same
        graceful-skip the cold boot path applies, surfaced here as a refusal rather than
        a silent no-op, because a runtime caller asked for a SPECIFIC corpus and is owed
        a reason it didn't appear).

        A root whose realpath is under the OS temp dir is accepted (a one-off sample corpus is a
        legitimate use) but is EPHEMERAL: the node-change ledger skips every write there, so the
        response carries `warnings: ["EPHEMERAL ROOT: ..."]` (key absent when no root is ephemeral).

        On success the new partition is immediately queryable by every tool
        (`lts_find`/`lts_node`/... `corpus=corpus_id`) and participates in the ordinary
        content-hash self-heal exactly like a corpus loaded at boot — `kernel_seated` and
        `self_heal_ready` say so explicitly rather than leaving it to be assumed.
        """
        if corpus_id in self.store.corpus_ids():
            return {"success": False, "corpus_id": corpus_id,
                    "error": f"corpus_id already loaded (would clobber): {corpus_id!r}; "
                             "call unregister_corpus first if you mean to replace it"}
        from . import config as _config
        if spec is None:
            try:
                choice = _registry.resolve_registry()   # THE lookup, re-read from disk, not the boot-time copy
            except RegistryError as exc:
                return {**_registry.refusal(exc), "corpus_id": corpus_id}
            spec = choice.corpora.get(corpus_id)
            if spec is None:
                if choice.source == "example":
                    # no registry is configured: that, not "no config entry", is the answer (P1)
                    return {**_registry.refusal(_registry.example_missing(corpus_id)), "corpus_id": corpus_id}
                return {"success": False, "corpus_id": corpus_id, "error": (
                    f"no config entry for corpus_id {corpus_id!r} in the corpus registry "
                    f"(the {choice.source} registry); pass "
                    "`spec` explicitly to register an ad hoc corpus")}
        profile = _config.engine_profile_config()
        part = None
        try:
            if "roots" in spec:
                part = _config.build_merged_partition(
                    corpus_id, spec["roots"], spec.get("domain_suffix", f"proj.{corpus_id}"),
                    profile=profile, spec=spec)
            else:
                root = spec.get("root")
                if root and os.path.isdir(root):
                    part = _config.build_partition(
                        corpus_id, root, spec.get("domain_suffix", f"proj.{corpus_id}"),
                        profile=profile, spec=spec)
        except CorpusFileError as exc:
            # [ISSUE-NONREGULAR-MEMBER] a member .lts that is not a readable regular file:
            # a refusal with the precise code (nothing registered), exactly as the boot path reports it.
            return _corpus_file_refusal(corpus_id, {"code": exc.code, "path": exc.rel_path})
        if part is None or not part.nodes:
            return {"success": False, "corpus_id": corpus_id, "spec": spec, "error": (
                "corpus root(s) resolved to zero nodes (missing directory, or no "
                "parseable .lts files under it) -- nothing to register")}
        part.private_reason = private_reason(corpus_id, spec)   # [INV-PRIVATE-FANOUT]
        # NOTE: a boot-failed corpus's `failed_corpora` entry is NOT popped here. It is dropped only after the SEAT succeeds
        # (below): a refused re-register must leave the corpus listed as failed, with its code ([Opus Diamond P2-A]).
        self.store.add_partition(part)
        kernel = getattr(self.store, "kernel", None)
        try:
            seeded_root = kernel.seat_partition(part) if kernel is not None else None
        except CorpusFileError as exc:
            # [Opus Diamond P1-A] the seat failed: ROLL BACK completely (not loaded, not seated, not half-registered), with a
            # coded refusal; a retry after the cause clears starts from nothing and succeeds.
            self.store.remove_partition(corpus_id)
            if kernel is not None:
                kernel.unseat_partition(corpus_id)
            self._reverse_index = None
            self._reverse_index_key = None
            return _corpus_file_refusal(corpus_id, {"code": exc.code, "path": exc.rel_path})
        self.store.failed_corpora.pop(corpus_id, None)          # seated: no longer a failed corpus
        # A newly-registered corpus is a SURFACE CHANGE — invalidate any cached
        # reverse-lookup snapshot so `lts_reverse` sees the new partition's edges on its
        # very next call rather than answering from a stale pre-registration snapshot.
        self._reverse_index = None
        self._reverse_index_key = None
        from factlattice import ledger as _ledger
        eph = [r for r in (part.root_paths or ())
               if _ledger.is_ephemeral(r, _ledger.default_ephemeral_roots())]
        out = {"success": True, "corpus_id": corpus_id, "domain_suffix": part.domain_suffix,
               "total_nodes": len(part.nodes), "total_edges": part.total_edges,
               "root_paths": part.root_paths,
               "kernel_seated": kernel is not None and seeded_root is not None,
               "self_heal_ready": getattr(part, "corpus_index", None) is not None}
        if eph:       # ABSENT when no root is ephemeral
            out["warnings"] = [f"EPHEMERAL ROOT: {r} resolves under the OS temp dir; writes there are NOT "
                               "ledgered (no history, no undo record)" for r in eph]
        return out

    def unregister_corpus(self, corpus_id: str) -> dict:
        """The runtime INVERSE of `register_corpus`. No restart, no reconnect.

        Cleans up a temp/ad hoc corpus (e.g. a one-off sample loaded for a mid-run
        check) without disturbing any other loaded corpus. Order matters and is
        deliberate: the kernel is unseated and the healer's per-corpus memory is
        forgotten BEFORE the partition is dropped from the store, so a concurrent
        watcher tick or query racing this call can observe "not yet unregistered" (old
        state, fully consistent) or "fully unregistered" (not-found, fully consistent) —
        never a partition that is gone from the store but still watched.

        Refuses (`success: False`, changes nothing) only when `corpus_id` was never
        loaded; there is no other failure mode — unregistering never touches disk.
        """
        if corpus_id not in self.store.corpus_ids():
            return {"success": False, "corpus_id": corpus_id,
                    "error": f"no such loaded corpus: {corpus_id!r}",
                    "available": self._public_corpus_ids(),
                    "note": PRIVATE_NOTE}
        kernel = getattr(self.store, "kernel", None)
        was_seated = kernel.unseat_partition(corpus_id) if kernel is not None else False
        if self.healer is not None:
            self.healer.forget(corpus_id)
        removed = self.store.remove_partition(corpus_id)
        self._reverse_index = None
        self._reverse_index_key = None
        return {"success": True, "corpus_id": corpus_id,
                "domain_suffix": getattr(removed, "domain_suffix", None),
                "was_kernel_seated": was_seated}

    # --- lts_list_corpora (net-new; enumerates registered partitions) -------
    def _heal_for_listing(self, part: Partition) -> Optional[str]:
        """[LIST-CORPORA-SELF-HEAL] route ONE stale partition through the query path's
        own choke point (`_gate`: heal, then re-verify with the ordinary freshness gate)
        and return `None` if it is now fresh, else the `heal_error` string.

        ⛔ NO SECOND HEAL MECHANISM. This calls `_gate`, the SAME function every query
        tool funnels through — same `SelfHealer`, same per-corpus lock, same storm guard,
        same incremental re-parse, same fresh-before-serve verification. Only the trigger
        point is new.

        FAIL-SOFT, and deliberately stronger than the healer's own contract:
        `SelfHealer.heal` promises never to raise, but a status call must not depend on a
        promise made by a component it merely calls — anything that escapes is caught
        here and reported on this one corpus's entry, never propagated to the call.
        """
        try:
            healed, marker = self._gate(part.corpus_id)
        except Exception as exc:  # noqa: BLE001 — one corpus must never fail the listing
            logger.exception("[lts-engine] list_corpora: heal raised for corpus %s",
                             part.corpus_id)
            return f"self-heal raised; not healed: {type(exc).__name__}: {exc}"
        if healed is not None:
            return None
        if self.healer is None:
            return ("self-heal is not wired on this engine (no healer; e.g. "
                    "FACTLATTICE_SELF_HEAL=0), so this stale corpus cannot be healed here")
        heal = (marker or {}).get("heal") or {}
        reason = heal.get("explanation") or heal.get("reason") or "self-heal did not restore freshness"
        error = heal.get("error")
        return f"{reason}: {error}" if error else reason

    def list_corpora(self, heal: bool = True) -> dict:
        """The registry, with EVERY count carrying its own freshness inline.

        [FL-RECALL-INVISIBLE-PRECONDITIONS]: "`lts_list_corpora` reported 171 for a
        corpus whose actual node count was 184… Counts quoted from `list_corpora` should
        carry the freshness flag INLINE, not in a sibling field a caller may not read."
        `total_nodes` is read off the index's `stats`, so on a stale index it is the
        count as of the last build — which is why `total_nodes_live` (the loaded map) and
        `counts_agree` are beside it: two independent readings of one fact, disagreeing
        visibly instead of one of them travelling alone.

        [LIST-CORPORA-SELF-HEAL] `heal` (default True): every corpus the freshness gate
        calls STALE is first routed through the query path's own self-heal
        (`_heal_for_listing` -> `_gate`), and the entry reports the POST-heal state. A
        corpus already fresh is never handed to the healer. Additive response fields,
        present only when `heal` is True:
          * `healed_corpora` — corpora that were stale at this call and are fresh after
            it. Keeps the staleness SIGNAL observable even though the state was cleared.
          * per-entry `heal_error` — only on an entry whose heal did not restore
            freshness; that entry stays `fresh: false`. A heal failure NEVER fails the
            call: the worst case is today's answer for that one corpus, with the reason
            surfaced instead of hidden.
        `heal=False` is today's behaviour exactly: side-effect free (nothing is healed,
        nothing mutated) and the response carries neither new field — use it to OBSERVE
        staleness, e.g. in a corpus-liveness sweep.
        """
        corpora = []
        healed_corpora: List[str] = []
        for p in self.store.partitions():
            fr = self._freshness_eval(p)
            heal_error: Optional[str] = None
            if heal and not fr.fresh:
                heal_error = self._heal_for_listing(p)
                try:
                    fr = self._freshness_eval(p)     # report the POST-heal verdict
                except Exception as exc:  # noqa: BLE001 — fail-soft; keep the stale fr
                    heal_error = heal_error or (
                        f"post-heal freshness re-check raised: {type(exc).__name__}: {exc}")
                if fr.fresh:
                    heal_error = None
                    healed_corpora.append(p.corpus_id)
                elif heal_error is None:
                    heal_error = "healed, but the post-heal freshness re-check is still stale"
            sc = (p.index.get("source_corpus") or {}) if isinstance(p.index, dict) else {}
            stated = p.stats.get("total_nodes", len(p.nodes))
            entry = {
                "corpus_id": p.corpus_id, "domain_suffix": p.domain_suffix,
                "total_nodes": stated,
                "total_nodes_live": len(p.nodes),
                "counts_agree": stated == len(p.nodes),
                "total_edges": p.total_edges,
                "resolved_edge_count": p.resolved_edge_count,
                "root_paths": p.root_paths, "fresh": fr.fresh,
                "built_at": sc.get("built_at") or (p.index.get("generated_at")
                                                   if isinstance(p.index, dict) else None),
                "counts_are_as_of": ("the last index build — this corpus is STALE, so a "
                                     "node added since then is not in these numbers and "
                                     "is not findable"
                                     if not fr.fresh else "the live corpus content"),
            }
            if heal_error is not None:
                entry["heal_error"] = heal_error
            corpora.append(entry)
        corpora.sort(key=lambda c: c["corpus_id"])
        stale = sorted(c["corpus_id"] for c in corpora if not c["fresh"])
        # corpora that FAILED to build on a non-regular/broken/unreadable member: listed with the code, never served
        failed, private_failed = self._failed_view()
        failed_fields = ({"failed_corpora": failed} if failed else {})
        if private_failed:
            failed_fields["private_failed"] = private_failed      # a COUNT: a failed private corpus is never named
        heal_fields = {"healed_corpora": sorted(healed_corpora)} if heal else {}
        choice = self._registry_choice()
        choice_field = {"registry_choice": choice} if choice is not None else {}
        return {"success": True, "count": len(corpora), "corpora": corpora,
                "stale_corpora": stale, **failed_fields, **heal_fields, **choice_field,
                **declare_extent(
                    corpora_consulted=[c["corpus_id"] for c in corpora],
                    corpora_available=[c["corpus_id"] for c in corpora],
                    corpora_skipped=([{"corpus": c, "reason": "stale"} for c in stale] + self._failed_skips()),
                    nodes_in_scope=sum(len(p.nodes) for p in self.store.partitions()),
                    keys_consulted=["corpus_id"],
                    index_fresh=not stale and not failed and not private_failed,
                    built_at={c["corpus_id"]: c["built_at"] for c in corpora},
                    root_paths={c["corpus_id"]: c["root_paths"] for c in corpora},
                    scope={"tool": "lts_list_corpora", "corpus": "ALL LOADED"},
                    **self._registry_extent_kw(True))}


# ---------------------------------------------------------------------------
# Transport-agnostic dispatch — ZERO MCP/stdio knowledge (plan §5).
# ---------------------------------------------------------------------------
# QUERY-layer tools (this module). ANALYTICS (`lts_aggregate`) and LINT (`lts_lint`)
# are routed by the same `dispatch()` but implemented in the sibling `analytics` /
# `lint` layers, imported lazily so importing the QUERY layer never drags them in.
LTS_TOOL_NAMES = (
    "lts_find", "lts_node", "lts_traverse", "lts_capabilities",
    "lts_list_corpora", "lts_reverse", "lts_history",
)
LTS_ANALYTICS_TOOL_NAMES = ("lts_aggregate", "lts_profile_audit")
LTS_LINT_TOOL_NAMES = ("lts_lint",)
# SELFCHECK layer. Like `lts_lint` it needs only the parser and no loaded store, so it is
# answerable by a server whose corpora failed to load — which is exactly the server you
# most want to interrogate. Kept a separate name tuple rather than folded into the LINT
# tuple because it answers a different question: lint asks "is this CORPUS clean?",
# selfcheck asks "is this PROCESS's parser correct?".
LTS_SELFCHECK_TOOL_NAMES = ("lts_selfcheck",)
# PREVIEW layer ([FL-PREVIEW]). Like lint/selfcheck it needs no loaded store — a draft
# is not corpus state — so it is answerable during authoring, before anything lands.
# NOT folded into the LINT tuple: lint asks "does this violate a rule?", preview asks
# "what did my document BECOME, and what did it lose?" — and the measured gap between
# those two questions (0 lint findings on a file that drops its own root node) is the
# entire reason this layer exists.
LTS_PREVIEW_TOOL_NAMES = ("lts_preview",)
# RENDER layer ([LEC-RENDER]). A DISTINCT tool, not an option on `lts_traverse`: flow
# semantics (membership vs sequence, decision shapes, labelled branches, swimlanes) are
# richer than a generic walk, and overloading traverse would make its contract mushy.
LTS_RENDER_TOOL_NAMES = ("lts_flow",)
# AUTHORING layer ([AUTH-PATH-LAYER-1-MCP]). The first WRITE surface on the engine, and
# the first layer whose justification is an EFFORT GRADIENT rather than a capability:
# every other tool here answers a question, these two perform the edit the author was
# going to perform anyway — via a raw text edit, which is where all eight measured
# silent property losses happened. It lives beside `lts_find`/`lts_node` precisely so
# the honest path costs fewer actions than the bypass. Needs no loaded store (a file
# being edited need not be in a registered corpus, and `must-accept-arbitrary-paths` is
# normative: corpus-scoped decision files now live with their datasets, outside corpus
# roots). `lts_property_loss` is the layer's own FALSIFIER, shipped with it rather than
# after it.
# `lts_repair_property` is the loop-closer: `lts_property_loss` FINDS and, until it
# landed, nothing in the sanctioned surface FIXED. See its schema in `server.py`.
# `lts_rename_property` / `lts_remove_property` ([RENAME-REMOVE-PROPERTY]) are the other two edits an
# author performs on a line that ALREADY PARSES, which `lts_repair_property` refuses and `lts_add_property`
# cannot do (it adds or replaces a value; it can neither rename a key nor remove a line).
LTS_AUTHORING_TOOL_NAMES = ("lts_add_property", "lts_add_node", "lts_property_loss",
                            "lts_repair_property", "lts_rename_property", "lts_remove_property")
# REGISTRY layer ([REG-RUNTIME-CORPUS]). The corpus set built ONCE at `build_tools` gets
# its runtime add/remove: `lts_register_corpus` builds+seats+serves a corpus with no
# process restart; `lts_unregister_corpus` is its exact inverse (cleans up a temp/ad hoc
# corpus). A SURFACE CHANGE, so `server.py` emits `notifications/tools/list_changed`
# after either one succeeds — see `Server.SURFACE_CHANGING_TOOLS`.
LTS_REGISTRY_TOOL_NAMES = ("lts_register_corpus", "lts_unregister_corpus")
# SEMANTIC layer ([SEM-FIND], [DEC-SEMANTIC-ASSIST]). Explicit, opt-in suggestions that are
# NEVER matches. Its model stack is an optional extra, imported only when the tool is called.
LTS_SEMANTIC_TOOL_NAMES = ("lts_semantic_find",)
# FEEDBACK layer ([QUERY-REPORT-MISS]). An agent files a known-item miss; the evidence goes to
# <state_dir>/miss-log.jsonl, never into a corpus. Read-only on every corpus.
LTS_FEEDBACK_TOOL_NAMES = ("lts_report_miss",)
# WRITE tools: every tool that changes something outside the answer to the question asked.
# Derived from the layer tuples above (never a hand-kept second list): the three typed WRITERS
# (they edit a `.lts` file and append to the node-change ledger), the REGISTRY pair (they change
# which corpora a process serves), and `lts_report_miss` (it appends to the state-dir miss log).
# Everything else the server serves is a READ. The command-line door (`python -m
# factlattice_engine call`, `callcli.py`) refuses these unless `--allow-write` is given, and a
# test asserts every tool in `server.TOOLS` is exactly one of read / write there.
LTS_WRITE_TOOL_NAMES = (tuple(n for n in LTS_AUTHORING_TOOL_NAMES if n != "lts_property_loss")
                        + LTS_REGISTRY_TOOL_NAMES + LTS_FEEDBACK_TOOL_NAMES)
# READ-ONLY ALLOW-LIST: the tools explicitly known to write NOTHING IN ANY CORPUS. A read tool may
# still write exactly two things, both outside every corpus and by design: (1) one record to the
# query log (`FACTLATTICE_QUERY_LOG` / the state dir), and (2) for `lts_semantic_find` and for
# `lts_find` when its `semantic="on_miss"` option runs, a DERIVED vector index under
# `LTS_ENGINE_STATE_DIR`. The command-line door classifies by THIS list, fail closed: a served tool
# that is not on it is treated as a writer and refused without `--allow-write`.
# ⛔ A LITERAL tuple on purpose, NOT derived from the layer tuples above: a tool added to an existing
# layer tuple (say LTS_TOOL_NAMES) must NOT become a read as a side effect. Adding a read tool is a
# DELIBERATE TWO-PLACE EDIT: this literal and `PINNED_READ_ONLY` in tests/test_cli_call.py, in the
# same change, with the proof it writes nothing in any corpus (SDLC 7.6 [CLI-CALL-READ-ALLOW-LIST]).
# `lts_find_in_prose` is listed though it is absent from `LTS_ALL_TOOL_NAMES` (pre-existing gap).
LTS_READ_ONLY_TOOL_NAMES = (
    "lts_find", "lts_node", "lts_traverse", "lts_capabilities", "lts_list_corpora", "lts_reverse",
    "lts_history", "lts_aggregate", "lts_profile_audit", "lts_lint", "lts_flow", "lts_selfcheck",
    "lts_preview", "lts_semantic_find", "lts_property_loss", "lts_find_in_prose",
)
# The full lts-engine tool surface across all SIX API layers.
LTS_ALL_TOOL_NAMES = (LTS_TOOL_NAMES + LTS_ANALYTICS_TOOL_NAMES
                      + LTS_LINT_TOOL_NAMES + LTS_RENDER_TOOL_NAMES
                      + LTS_SELFCHECK_TOOL_NAMES + LTS_PREVIEW_TOOL_NAMES
                      + LTS_AUTHORING_TOOL_NAMES + LTS_REGISTRY_TOOL_NAMES
                      + LTS_SEMANTIC_TOOL_NAMES + LTS_FEEDBACK_TOOL_NAMES)

# The aggregate ops `lts_aggregate` exposes (the ANALYTICS layer surface).
LTS_AGGREGATE_OPS = (
    "count", "group_by", "numeric", "sum", "avg", "min", "max", "distinct",
    "cross_tab", "top_k", "degree", "top_by_degree", "count_by_edge_type", "orphans",
    "exemplifies_coverage",
)
_AGG_FILTER_KEYS = ("prefix", "domain", "status", "area", "text",
                    "since", "until", "function", "service") + _qs.NEW_PARAMS

# [S5 review F5] at most this many corpora keep a BM25 index in memory (LRU).
LEXRANK_CACHE_MAX = 8

_SERVER_TOOLS: Optional[LtsEngine] = None


def reset_server_tools() -> None:
    global _SERVER_TOOLS
    _SERVER_TOOLS = None


def set_server_tools(tools: LtsEngine) -> None:
    """Inject the process-wide LtsEngine (server.py builds it from config)."""
    global _SERVER_TOOLS
    _SERVER_TOOLS = tools


def _run_aggregate(agg, t, a):
    """The lts_aggregate body (moved verbatim from `_dispatch` so the caller can attach the
    `query_interpreted`/`warnings` of the find it drove, [FIND-QUERY-SURFACE])."""
    op = a.get("op")
    corpus = a.get("corpus")
    raw = bool(a.get("raw", False))
    dg = a.get("date_granularity", "month")
    filters = {k: a[k] for k in _AGG_FILTER_KEYS if a.get(k) is not None}
    if op == "count":
        return agg.count(corpus=corpus, group_by=a.get("group_by"),
                         raw=raw, date_granularity=dg, **filters)
    if op == "group_by":
        return agg.group_by(a["attr"], corpus=corpus, raw=raw,
                            date_granularity=dg, **filters)
    if op in ("numeric", "sum", "avg", "min", "max"):
        nop = a.get("numeric_op") or (op if op != "numeric" else "sum")
        return agg.numeric(a["attr"], op=nop, corpus=corpus,
                           group_by=a.get("group_by"), raw=raw,
                           date_granularity=dg, **filters)
    if op == "distinct":
        return agg.distinct(a["attr"], corpus=corpus, raw=raw,
                            date_granularity=dg, **filters)
    if op == "cross_tab":
        return agg.cross_tab(a["attr"], a["attr2"], corpus=corpus, raw=raw,
                             date_granularity=dg, **filters)
    if op == "top_k":
        return agg.top_k(a["attr"], int(a.get("k", 10)),
                         by=a.get("by", "count"), agg=a.get("agg", "sum"),
                         corpus=corpus, raw=raw, date_granularity=dg, **filters)
    if op == "degree":
        return agg.degree(a["node"], corpus=corpus, **filters)
    if op == "top_by_degree":
        return agg.top_by_degree(int(a.get("k", 10)), corpus=corpus,
                                 direction=a.get("direction", "both"), **filters)
    if op == "count_by_edge_type":
        return agg.count_by_edge_type(corpus=corpus, **filters)
    if op == "orphans":
        return agg.orphans(corpus=corpus, limit=a.get("limit"), **filters)
    if op == "exemplifies_coverage":
        cov_kw = {}
        if a.get("decision_prefixes") is not None:
            cov_kw["decision_prefixes"] = a["decision_prefixes"]
        if a.get("principle_prefix") is not None:
            cov_kw["principle_prefix"] = a["principle_prefix"]
        if a.get("edge_verb") is not None:
            cov_kw["edge_verb"] = a["edge_verb"]
        return agg.exemplifies_coverage(corpus=corpus, limit=a.get("limit"),
                                        **cov_kw, **filters)
    return {"success": False,
            "error": f"unknown aggregate op {op!r}; expected one of {LTS_AGGREGATE_OPS}"}


def _attach_query_extras(out, agg):
    """[FIND-QUERY-SURFACE] an aggregate over the new filters carries the echo + warnings of the
    find it drove (absent when no new parameter was used, so old calls are byte-identical)."""
    if isinstance(out, dict) and out.get("success", True) and agg.query_extras:
        out = {**out, **agg.query_extras}
    return out


# [ERROR-CODE-REGISTRY-NOT-FOUND] Tools EXEMPT from the blanket corpus-scoped refusal in REGISTRY-ERROR MODE because
# they read no corpus: the parser-only layers (lint, preview, selfcheck), the typed writers and the property-loss scan
# (they take a PATH), and `lts_list_corpora` (which reports the mode itself). Exempt is not the same as answering:
# only `lts_selfcheck` and `lts_list_corpora` answer in error mode (lint and preview resolve their profile through the
# registry and so refuse); the writers still write. Every OTHER served tool is corpus-scoped and
# is refused with zero data. A literal tuple, pinned by `tests/test_registry_lookup.py`: a new tool must be
# classified deliberately, not inherit an answer.
REGISTRY_ERROR_EXEMPT_TOOLS = (
    "lts_lint", "lts_preview", "lts_selfcheck", "lts_list_corpora",
    "lts_add_property", "lts_add_node", "lts_repair_property", "lts_property_loss",
    "lts_rename_property", "lts_remove_property",
)


def _registry_gate(t: object, name: str, a: dict) -> Optional[dict]:
    """The registry's two refusals, applied to every tool that reads the corpus set ([ERROR-CODE-REGISTRY-NOT-FOUND]).

    1. REGISTRY-ERROR MODE (the server booted with a broken registry): every tool except `lts_list_corpora`
       (which reports the mode) answers `registry-not-found` and returns ZERO data. Nothing stale or partial
       is ever served; the mode ends only by a restart, and the reason text says so.
    2. The built-in EXAMPLE registry is in use and the request NAMES a corpus it does not have: that is
       `registry-not-found` ("no corpus registry is configured ..."), never the generic unknown-corpus error --
       an example answer for a corpus the caller meant to find in THEIR registry must be a stop.
    An engine seated by hand (no registry seated) is exempt: it never resolved a registry.
    """
    err = getattr(t, "registry_error", None)
    if err is not None:
        return None if name in REGISTRY_ERROR_EXEMPT_TOOLS else _registry.refusal(err, restart_note=True)
    boot = getattr(t, "registry_boot", None)
    if boot and boot.get("source") == "example":
        cid = a.get("corpus")
        if isinstance(cid, str) and cid and cid not in getattr(t, "registry_ids", ()) \
                and t.store.get_partition(cid) is None:
            return _registry.refusal(_registry.example_missing(cid))
    return None


def dispatch(name: str, arguments: dict, tools: Optional[LtsEngine] = None) -> dict:
    """Single entry point for the lts_* tools, WITH the query log around it.

    ⛔ THE LOG SEAM IS HERE, NOT IN `server.py`. `Server.handle` is the MCP transport's
    door; `dispatch` is the ENGINE's door, and its own docstring has always said so:
    "transport-agnostic… ZERO MCP/stdio knowledge". Logging at the transport would record
    the MCP caller and miss the library caller, the CLI, and the a message bus handler this
    module's header names as the migration path — so the read profile would understate by
    exactly the callers nobody thought about, which is the failure shape this whole
    engine keeps closing.

    The body is `_dispatch`; this wrapper exists so that EVERY return path is recorded.
    There are twenty-odd `return`s in there and a decorator-free "log before each one"
    would be a list of places to forget.

    ⛔ AND THE LOG NEVER CHANGES THE ANSWER. `record_call` swallows everything; a broken
    log directory produces one warning per process and the query is served regardless.
    """
    with querylog.timed() as t:
        result = _dispatch(name, arguments, tools)
    querylog.record_call(name, arguments or {}, result, t.ms)
    return result


def _ledger_args(a: dict) -> dict:
    """[NODE-CHANGE-LEDGER] the optional audit metadata a write call may carry (echoed verbatim)."""
    return {k: a.get(k) for k in ("proposer", "approver", "reason", "via")}


def _dispatch(name: str, arguments: dict, tools: Optional[LtsEngine] = None) -> dict:
    """The dispatch body. Pure dict-in/dict-out; no transport, no logging.

    `tools` may be injected (tests); otherwise the process-wide singleton set via
    `set_server_tools` is used. No `_caller_identity` is read — there is no ACL.
    """
    a = arguments or {}
    # --- REGISTRY-ERROR MODE, for the tools that are exempt because they take a PATH: an exempt tool that is given a
    # `corpus` is asking for registry data (`lts_lint(corpus=...)` resolves a corpus root through the registry), so it is
    # refused like every corpus-scoped tool. MEASURED (round 4): in error mode only `lts_selfcheck` and `lts_list_corpora`
    # answer; `lts_lint` and `lts_preview` refuse even without a corpus, because they resolve their profile THROUGH the
    # registry. (Round 3, P2-3.)
    _t0 = tools if tools is not None else _SERVER_TOOLS
    if name in REGISTRY_ERROR_EXEMPT_TOOLS and a.get("corpus") and getattr(_t0, "registry_error", None) is not None \
            and name != "lts_list_corpora":
        return _registry.refusal(_t0.registry_error, restart_note=True)
    # --- LINT layer: needs only the parser + a path, no loaded store ---
    if name == "lts_lint":
        try:
            from .lint import lint as _lint
            res = _lint(a.get("source") or a.get("path"), paths=a.get("paths"),
                        profile=a.get("profile"),
                        structure=a.get("structure", True),
                        dangle=a.get("dangle", False))
            # [ENGINE-BUDGET] / [INV-DECLARED-REACH]. The MCP surface is BOUNDED where
            # the library `lint()` and its CLI are not: this consumer is an Intelligence
            # with a finite context, and an unbounded directory lint is the largest
            # response this engine can produce (measured 667 findings / ~203k tokens on
            # one project's acme-specs — a whole context window in one call). The
            # slice is declared, the cursor resumes it, and `summary` still counts the
            # CORPUS, so nothing here can under-report in silence.
            out = res.to_dict(
                token_budget=resolve_budget(a.get("token_budget"), DEFAULT_TOKEN_BUDGET),
                offset=int(a.get("offset") or 0))
            out["success"] = True
            out["exit_code"] = res.exit_code()
            out["has_errors"] = res.has_errors
            return out
        except RegistryError as exc:    # a broken registry is a NAMED failure, never an empty answer
            return _registry.refusal(exc)
        except CorpusFileError as exc:  # a FIFO/broken/unreadable member: the precise code, not a bare exception text
            return _coded(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[lts-engine] error dispatching lts_lint")
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    # --- PREVIEW layer: parser only; a DRAFT is not corpus state ---
    # Deliberately BEFORE the store gate. The authoring loop's whole point is that the
    # content being previewed is not on disk anywhere the engine watches, so requiring
    # a loaded store to answer "what would this become?" would make the tool unusable
    # in exactly the phase it exists for. The store is passed through ONLY so a
    # `corpus="<loaded id>"` argument can resolve to that corpus's root; a `corpus`
    # given as a directory path needs nothing loaded at all.
    if name == "lts_preview":
        try:
            from .preview import PreviewRefusal, preview as _preview
            t0 = tools if tools is not None else _SERVER_TOOLS
            _failed = getattr(getattr(t0, "store", None), "failed_corpora", None) or {}
            if isinstance(a.get("corpus"), str) and a["corpus"] in _failed:     # registered, but it failed to build
                return _corpus_file_refusal(a["corpus"], _failed[a["corpus"]])
            try:
                return _preview(
                    a.get("content"), path=a.get("path"), corpus=a.get("corpus"),
                    replaces=a.get("replaces"), virtual_path=a.get("virtual_path"),
                    profile=a.get("profile"), verbose=bool(a.get("verbose", False)),
                    store=getattr(t0, "store", None))
            except PreviewRefusal as ref:
                # A refusal is a RESULT, not a crash: it names the reason and the
                # remedy, which is the whole point of refusing instead of guessing.
                return ref.to_dict()
        except RegistryError as exc:
            return _registry.refusal(exc)
        except CorpusFileError as exc:
            return _coded(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[lts-engine] error dispatching lts_preview")
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    # --- AUTHORING layer ([AUTH-PATH-LAYER-1-MCP]): parser + a path, no loaded store ---
    # Deliberately BEFORE the store gate, for a reason the proposal makes normative:
    # `must-accept-arbitrary-paths` — "corpus-scoped decision files now live with their
    # datasets, outside corpus roots. A tool that writes only inside the corpus root
    # would miss the files that most need it." Requiring a registered corpus would
    # reintroduce exactly the coverage hole layer 1 exists to close, and the author
    # would go back to the raw text edit.
    if name in ("lts_add_property", "lts_add_node", "lts_property_loss",
                "lts_repair_property", "lts_rename_property", "lts_remove_property"):
        try:
            from . import authoring as _auth
            # The store, WHEN one is seated, purely so the receipt can answer "is this
            # path inside a registered corpus root?" ([FL-RECALL-INVISIBLE-PRECONDITIONS]
            # precondition-1). It is never required: `must-accept-arbitrary-paths` is
            # normative, and with no store the membership check falls back to the declared
            # registry and SAYS which source it used.
            _t = tools if tools is not None else _SERVER_TOOLS
            _store = getattr(_t, "store", None)
            # ⛔ NO oracle is injected and `lts_preview` is NOT consulted. The gate is a
            # direct `factlattice.parser.parse_text` round-trip inside the writer.
            # Preview runs that same core, so it is not an independent second opinion,
            # and its one distinct view — the `lost` channel — is measured-insufficient:
            # 2026-08-12, a dropped key landed in `ignorable` beside blank lines while
            # preview reported `lost: 0` at 100.0% coverage.
            if name != "lts_property_loss":
                # [Opus Diamond F2] a corpus-file-* condition on the file a typed writer is asked to edit is answered with the CODE
                # (`reason` and `evidence.refusal`) and `error_class`, not free text and not "does not exist". Nothing is written.
                _bad = _writer_target_error(a.get("path"))
                if _bad is not None:
                    return _writer_corpus_file_refusal(_auth, _bad)
            if name == "lts_add_property":
                return _auth.add_property(
                    a["path"], a["node_id"], a["key"], a["value"],
                    allow_replace=bool(a.get("allow_replace", True)),
                    mode=a.get("mode") or "set", store=_store,
                    receipt=a.get("receipt"), **_ledger_args(a))
            if name == "lts_add_node":
                if (a.get("create") or a.get("create_parent")) and not (
                        "path" in a and "node_id" in a):
                    # [delta2 F2] a create with a required argument absent is a CODED refusal too
                    return _auth._refusal(_auth.EditRefused(
                        "`path` and `node_id` are required. Nothing written.",
                        evidence={"refusal": "create_argument_invalid"}), path=None)
                return _auth.add_node(
                    a["path"], a["node_id"], a.get("title") or a["node_id"],
                    a.get("properties") or {}, section=a.get("section"),
                    level=a.get("level"), store=_store, receipt=a.get("receipt"),
                    create=bool(a.get("create", False)),
                    create_parent=bool(a.get("create_parent", False)),
                    doc_title=a.get("doc_title"), domain=a.get("domain"),
                    doctype=a.get("doctype"), **_ledger_args(a))
            if name == "lts_repair_property":
                return _auth.repair_property(
                    a["path"], a["node_id"], int(a["line"]), a["key"],
                    a.get("value"), store=_store, receipt=a.get("receipt"), **_ledger_args(a))
            if name in ("lts_rename_property", "lts_remove_property"):
                need = ("path", "node_id", "key") + (("new_key",) if name == "lts_rename_property" else ())
                absent = [k for k in need if k not in a]
                if absent:      # a CODED refusal: the query log persists the code, not the argument text
                    return _auth._fixed_code_refusal(_auth.EditRefused(
                        f"required argument(s) missing: {', '.join(absent)}. Nothing written.",
                        evidence={"refusal": "bad_argument"}), None)
            if name == "lts_rename_property":
                return _auth.rename_property(
                    a["path"], a["node_id"], a["key"], a["new_key"], line=a.get("line"),
                    allow_edge_change=a.get("allow_edge_change", False), store=_store,
                    receipt=a.get("receipt"), **_ledger_args(a))
            if name == "lts_remove_property":
                return _auth.remove_property(
                    a["path"], a["node_id"], a["key"], line=a.get("line"),
                    expect_value=a.get("expect_value"),
                    allow_edge_change=a.get("allow_edge_change", False), store=_store,
                    receipt=a.get("receipt"), **_ledger_args(a))
            roots = a.get("roots") or a.get("root") or a.get("path")
            if not roots:
                return {"success": False, "error": "lts_property_loss needs `roots`"}
            return _auth.property_loss(roots, by_month=bool(a.get("by_month", False)),
                                       limit_examples=int(a.get("limit_examples", 12)))
        except KeyError as exc:
            return {"success": False, "error": f"missing required argument: {exc}"}
        except RegistryError as exc:
            return _registry.refusal(exc)
        except CorpusFileError as exc:
            return _coded(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[lts-engine] error dispatching %s", name)
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    # --- SELFCHECK layer: parser only, no loaded store (same shape as lts_lint) ---
    if name == "lts_selfcheck":
        try:
            from .selfcheck import run_selfcheck as _selfcheck
            return _selfcheck()
        except Exception as exc:  # noqa: BLE001
            logger.exception("[lts-engine] error dispatching lts_selfcheck")
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    t = tools if tools is not None else _SERVER_TOOLS
    if t is None:
        return {"success": False, "error": "lts-engine store not loaded (call set_server_tools)"}
    refused = _registry_gate(t, name, a)
    if refused is not None:
        return refused
    try:
        # --- ANALYTICS layer: a deterministic fold over the QUERY layer's find ---
        if name == "lts_aggregate":
            from .analytics import LtsAggregator
            agg = LtsAggregator(t)
            return t._attach_failed(_attach_query_extras(_run_aggregate(agg, t, a), agg), a)
        # --- ANALYTICS layer: profile audit (corpus vocabulary vs. declared profile) ---
        if name == "lts_profile_audit":
            from .analytics import LtsAggregator
            agg = LtsAggregator(t)
            filters = {k: a[k] for k in _AGG_FILTER_KEYS if a.get(k) is not None}
            return t._attach_failed(_attach_query_extras(agg.profile_audit(
                corpus=a.get("corpus"), profile=a.get("profile"),
                required_property=a.get("required_property"),
                required_scope=a.get("required_scope"),
                tail_threshold=int(a.get("tail_threshold", 5)), **filters), agg), a)
        if name == "lts_find":
            if a.get("path") is not None and "word_fallback" in a:
                return {"success": False, "error": "`word_fallback` is not available with `path`: "
                        "the word fallback needs a registered corpus"}
            return t.find(corpus=a.get("corpus"), domain=a.get("domain"),
                          function=a.get("function"), service=a.get("service"),
                          prefix=a.get("prefix"), status=a.get("status"),
                          text=a.get("text"), since=a.get("since"), until=a.get("until"),
                          date_property=a.get("date_property"), area=a.get("area"),
                          token_budget=a.get("token_budget"), offset=a.get("offset", 0),
                          tier=a.get("tier"), limit=a.get("limit"),
                          order_by=a.get("order_by"),
                          include_reverse_digest=bool(a.get("include_reverse_digest", False)),
                          semantic=a.get("semantic"), rank=a.get("rank"),
                          **({"path": a["path"]} if a.get("path") is not None else {}),
                          **{k: a[k] for k in _qs.NEW_PARAMS if k in a})
        if name == "lts_find_in_prose":
            return t.find_in_prose(
                text=a.get("text"), corpus=a.get("corpus"), domain=a.get("domain"),
                function=a.get("function"), service=a.get("service"),
                prefix=a.get("prefix"), status=a.get("status"), area=a.get("area"),
                since=a.get("since"), until=a.get("until"),
                date_property=a.get("date_property"),
                body_only=bool(a.get("body_only", False)),
                context=int(a.get("context") or 0),
                token_budget=a.get("token_budget"), offset=a.get("offset", 0),
                tier=a.get("tier"),
                **{k: a[k] for k in _qs.NEW_PARAMS if k in a})
        if name == "lts_node":
            return t.node(corpus=a["corpus"], node_id=a["node_id"],
                          token_budget=a.get("token_budget"), tier=a.get("tier"),
                          include_reverse_digest=bool(a.get("include_reverse_digest", False)))
        if name == "lts_report_miss":
            return t.report_miss(corpus=a.get("corpus"), query=a.get("query"),
                                 expected_ids=a.get("expected_ids"), why=a.get("why"),
                                 tool_used=a.get("tool_used"), found_via=a.get("found_via"))
        if name == "lts_semantic_find":
            return t.semantic_find(corpus=a.get("corpus"), text=a.get("text"),
                                   k=a.get("k", 10), prefix=a.get("prefix"),
                                   status=a.get("status"), area=a.get("area"))
        if name == "lts_reverse":
            return t.reverse(node_id=a["node_id"], corpus=a["corpus"],
                             limit=a.get("limit"), offset=int(a.get("offset") or 0),
                             edge_types=a.get("edge_types"), verbs=a.get("verbs"),
                             token_budget=a.get("token_budget"))
        if name == "lts_history":
            return t.history(corpus=a["corpus"], node_id=a["node_id"], limit=a.get("limit"))
        if name == "lts_traverse":
            return t.traverse(corpus=a["corpus"], start_node=a["start_node"],
                              edge_types=a.get("edge_types"), layers=a.get("layers"),
                              direction=a.get("direction", "both"),
                              max_depth=a.get("max_depth", 1),
                              since=a.get("since"), until=a.get("until"),
                              date_property=a.get("date_property"), area=a.get("area"),
                              token_budget=a.get("token_budget"), offset=a.get("offset", 0),
                              tier=a.get("tier"), limit=a.get("limit"),
                              order_by=a.get("order_by"))
        if name == "lts_flow":
            return t.flow(corpus=a["corpus"], start_node=a["start_node"],
                          edge_types=a.get("edge_types"),
                          max_depth=a.get("max_depth", 8),
                          direction=a.get("direction", "forward"),
                          membership=a.get("membership", True),
                          group_by=a.get("group_by", "actor"),
                          layout=a.get("layout", "TD"),
                          include_container=a.get("include_container", False),
                          title=a.get("title"),
                          token_budget=a.get("token_budget"), offset=a.get("offset", 0))
        if name == "lts_capabilities":
            return t.capabilities(corpus=a["corpus"], topic=a.get("topic"),
                                  node_id=a.get("node_id"),
                                  token_budget=a.get("token_budget"),
                                  offset=int(a.get("offset") or 0))
        if name == "lts_list_corpora":
            # [LIST-CORPORA-SELF-HEAL] default True. Absent OR null means the default;
            # only an explicit false opts out (so `"heal": null` cannot silently disable it).
            heal_arg = a.get("heal")
            return t.list_corpora(heal=True if heal_arg is None else bool(heal_arg))
        if name == "lts_register_corpus":
            return t.register_corpus(a["corpus_id"], spec=a.get("spec"))
        if name == "lts_unregister_corpus":
            return t.unregister_corpus(a["corpus_id"])
    except KeyError as exc:
        return {"success": False, "error": f"missing required argument: {exc}"}
    except RegistryError as exc:    # e.g. lts_profile_audit over a registry that no longer resolves
        return _registry.refusal(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[lts-engine] error dispatching %s", name)
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"success": False, "error": f"unknown lts tool: {name}"}
