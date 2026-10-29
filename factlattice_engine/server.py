"""Thin stdio MCP server over the transport-agnostic engine (plan §5).

    python -m factlattice_engine.server

Speaks MCP over stdio: newline-delimited JSON-RPC 2.0 (the MCP stdio framing), handling
`initialize`, `tools/list`, `tools/call`, and `notifications/*`. Implemented in STDLIB
ONLY (no `mcp` package) so the whole deliverable stays zero-third-party — the dependency
footprint is exactly `factlattice` (the parser) + Python stdlib. The server is a delivery
shell: every tool call collapses to `tools.dispatch(name, arguments) -> dict`, which has
zero knowledge of MCP. Swapping this for a a message bus request/response handler later
touches no engine code (the migration path in plan §5).

Register it in an MCP client config like any other local stdio server, e.g.:
    { "mcpServers": { "factlattice": {
        "command": "python3", "args": ["-m", "factlattice_engine.server"] } } }
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Dict, List, Optional

from factlattice import registry
from factlattice.authoring.edit import WRITER_REFUSAL_CODES
from factlattice.registry import RegistryError

from . import config
from .kernel import FreshnessKernel
from .selfheal import (
    SelfHealer, _ENV_DISABLE as _ENV_SELF_HEAL, _ENV_VERIFY_ON_QUERY, _env_flag,
)
from . import query_surface as _qs
from .tiers import DIGEST_FIELDS, DIGEST_MAX_NODES, TIERS
from .tools import DEFAULT_TOKEN_BUDGET, LtsEngine, dispatch
from .build_stamp import SERVING_LOCK
from .store import LtsStore
from .verdict import PollVerifiedFreshness

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "lts-engine", "version": "0.1.0"}

# --- MCP tool schemas (the 4 query tools + list_corpora) ----------------------
_STR = {"type": "string"}
_INT = {"type": "integer"}
# The advertised default is DERIVED from `tools.DEFAULT_TOKEN_BUDGET`, never restated:
# a schema that says one number while the engine applies another is the
# artifact-disagrees-with-the-system failure, and it is unfalsifiable from the outside.
# ONE `_BUDGET` object is shared by every tool that takes a budget, so the five tool
# schemas cannot drift from each other either.
_BUDGET = {**_INT, "description": (
    f"max tokens of node content in the response (default {DEFAULT_TOKEN_BUDGET}). "
    "An explicit value OVERRIDES the default in BOTH directions — smaller to spend "
    "less context, LARGER to pull a slice the default would truncate. When the result "
    "does not fit, the response says so: `count` < `total_matched`, `truncated: true`, "
    "and a `cursor` to resume from via `offset`.")}
# The `status` filter is LIFECYCLE-AWARE (see `factlattice_engine/lifecycle.py`); the
# semantics ride on the tool schema so a caller never has to read the engine source to
# know that a bare token widens and a qualified string does not.
# --- [LTS-TIER] the response-tier selector (see `factlattice_engine/tiers.py`) ------
# ADDITIVE and OPTIONAL. Omitting `tier` is the legacy path and is byte-identical to the
# pre-tier engine — no existing caller changes behaviour. Like `_BUDGET`, ONE object is
# shared by every tool that takes a tier, and its text is DERIVED from the tier module's
# own constants so the advertised field list cannot drift from the field list the engine
# actually builds.
_TIER = {**_STR, "enum": list(TIERS), "description": (
    "response tier. OMIT for the legacy response (unchanged). "
    "'digest' = ALWAYS COMPLETE — every node in the result set, carrying only "
    + "/".join(DIGEST_FIELDS) +
    " and NOTHING derived from the body (no properties, no summary, no excerpt). It "
    "answers 'is there something here I need to read?' and deliberately cannot answer "
    "'what does it say?'. It is never truncated: it takes no `offset`, and past "
    f"{DIGEST_MAX_NODES} nodes it ERRORS rather than trimming. "
    "'page' = full content, one explicit page (`offset` in; `next_offset`, `total`, "
    "`truncated` out). "
    "'full' = full content, COMPLETE — if it does not fit `token_budget` it ERRORS "
    "rather than returning a fraction that looks whole.")}
_STATUS = {**_STR, "description": (
    "lifecycle filter. A BARE token ('proposed', 'in-progress', 'operational') matches "
    "the whole lifecycle INCLUDING qualified values like "
    "'proposed \u2014 awaiting ratify (BLOCKS slice 2)'. A QUALIFIED string matches "
    "exactly, so precision is available when you want it.")}
# --- [QUERY-LIMIT-ORDER] `limit` + `order_by` \u2014 bounding by count, not by time ------
# ADDITIVE and OPTIONAL, on `lts_find` and `lts_traverse` only ([SPEC-COVERAGE-SCOPE]).
# Omitting both leaves the response byte-identical to today ([SPEC-BACKWARD-COMPAT]) \u2014
# the engine NEVER substitutes a fixed N for an absent `limit` ([INV-LIMIT-CALLER-SET]).
_LIMIT = {**_INT, "description": (
    "ceiling on the number of nodes in the ordered result, applied LAST (after "
    "filtering and ordering). Absent = unbounded, today's behavior. `limit=0` is "
    "legal: an empty result with `total_matched` still reporting the true pre-limit "
    "count. The engine never substitutes a default N for an absent `limit` \u2014 state "
    "the number you want, e.g. `limit=30, order_by=\"recency\"` for 'the newest 30' "
    "regardless of how large the match set is.")}
_ORDER_BY = {"description": (
    "the ordering `limit` is taken FROM. `\"recency\"` is sugar for "
    "`{field: <this query's resolved date_property>, direction: \"desc\"}` \u2014 the "
    "headline case. Or `{field, direction}` where `field` may name ANY property key "
    "(not only a date) and `direction` is `\"asc\"`/`\"desc\"` (defaults to `\"desc\"` "
    "when omitted; an unrecognized value is a refused `invalid_order_direction`, never "
    "silently coerced). Omitted/null = today's default order, "
    "(corpus, id) ascending for `find`, BFS discovery order for `traverse` \u2014 UNCHANGED, "
    "never recency by default. A node lacking the field, or holding a value that does "
    "not match the field's dominant type across the match set, sorts LAST regardless of "
    "direction (tiebroken by id ascending) rather than erroring \u2014 see `unorderable_count` "
    "in the response's `order_applied`."),
    "oneOf": [
        {"type": "string", "enum": ["recency"]},
        {"type": "object", "properties": {
            "field": _STR, "direction": {**_STR, "enum": ["asc", "desc"]}},
            "required": ["field"]},
    ]}
# --- [LTS-REVERSE-LOOKUP] the reverse-referrer DIGEST flag -----------------------
# ADDITIVE and OPTIONAL on `lts_find`/`lts_node` only. Omitted (default False), the
# response is BYTE-IDENTICAL to today. Shared text so the two tools' fields cannot
# drift from each other or from what `lts_reverse` actually promises to complete.
_INCLUDE_REVERSE_DIGEST = {"type": "boolean", "description": (
    "attach a CAPPED, non-complete preview of `lts_reverse`'s answer to each returned "
    "node/digest-row: `reverse_digest` = {total_referrers, returned, omitted, "
    "truncated, sample, see_also}. Default false (byte-identical response). This is a "
    "DIGEST, never the completeness path — `total_referrers` can exceed `returned` and "
    "`see_also` always names the `lts_reverse` call that has the rest.")}
# --- [FIND-QUERY-SURFACE] terms, match modes, property conditions, document filters -------
# ADDITIVE and OPTIONAL on `lts_find`, `lts_aggregate` and `lts_find_in_prose` (where they are
# POPULATION filters). A call using none of them returns exactly the response it always did.
# No string syntax exists anywhere: these are structured parameters a form can compile to.
_MATCH = {**_STR, "enum": list(_qs.MATCH_MODES), "description": (
    "how terms are matched, default 'phrase'. phrase = today's rule (a case-insensitive substring "
    "of ONE field, i.e. '*x*'); word = the term's words appear as WHOLE words, contiguous and in "
    "order, in ONE field (words are [a-z0-9] runs, no stemming, no stop-words; 'go-live' hits "
    "'go live' but not 'go to live'); prefix = some whole word starts with the term ('lantern' "
    "hits 'lantern-architect', not 'alantern'; one word, at least 2 characters). No glob, no regex.")}
_TERM_LIST = lambda what: {"type": "array", "items": {"oneOf": [
    _STR, {"type": "object", "properties": {
        "term": _STR, "match": {**_STR, "enum": list(_qs.MATCH_MODES)}}, "required": ["term"]}]},
    "description": what}
_ALL_OF = _TERM_LIST(
    "terms that must ALL hit (AND). Each is a string (using `match`) or {term, match}. Searched in the "
    "same fields as `text` (id, title, string property values and list elements; not body). "
    "Case-insensitive. Empty strings/lists are refused.")
_ANY_OF = _TERM_LIST("terms of which AT LEAST ONE must hit (OR). Same element forms and fields as all_of.")
_NONE_OF = _TERM_LIST(
    "terms none of which may hit. A call with only none_of is refused: add a positive filter "
    "(it would mean 'every node except...').")
_WHERE = {"type": "array", "description": (
    "property conditions returning ids, ANDed: each is {key, <ONE operator>}: has:true | lacks:true | "
    "equals:V | one_of:[V..] | not_equals:V | not_one_of:[V..]. For a list-valued key, equals/one_of "
    "ask 'ANY element', and not_equals/not_one_of ask 'ANY element outside': not_one_of is exactly "
    "the violation set of 'every value of K must be one of {..}'. Equality is exact (trimmed) and "
    "CASE-SENSITIVE, unlike terms; values must be strings. not_one_of/not_equals never match a node "
    "lacking the key: ask `lacks` separately. where is AND-only: for OR, make two calls. Inside "
    "`where`, `status` is plain property equality (no lifecycle widening). The response echoes "
    "`key_present_in_scope` per key and warns when no node in scope carries it (a likely misspelling)."),
    "items": {"type": "object", "properties": {
        "key": _STR, "has": {"type": "boolean"}, "lacks": {"type": "boolean"},
        "equals": _STR, "not_equals": _STR,
        "one_of": {"type": "array", "items": _STR}, "not_one_of": {"type": "array", "items": _STR}},
        "required": ["key"]}}
_FIELDS = {"type": "array", "items": _STR, "description": (
    "restrict where TERMS are matched to these fields: 'id', 'title' or property keys (default: all "
    "structured fields). Does not apply to `text` or `where`; body is never offered here.")}
_SOURCE_FILE = {**_STR, "description": (
    "population filter: the node's `source_file` exactly as the engine reports it (relative to the "
    "corpus root). Needs a named `corpus`; an unknown file, or a path under several roots without "
    "`area`, is refused.")}
_DOCUMENT_TAG = {**_STR, "description": (
    "population filter: the node's `document_tag` (e.g. 'SPEC'), exact and CASE-SENSITIVE; nodes "
    "with no tag never match (the echo counts them).")}
_EXPLAIN = {"type": "boolean", "description": (
    "add `query_interpreted` (what the engine actually evaluated) to the response of ANY call, "
    "including a plain `text` call. Always present when a terms/where/fields/source_file/"
    "document_tag parameter is used.")}
_QS_PROPS = {"all_of": _ALL_OF, "any_of": _ANY_OF, "none_of": _NONE_OF, "match": _MATCH,
             "where": _WHERE, "fields": _FIELDS, "source_file": _SOURCE_FILE,
             "document_tag": _DOCUMENT_TAG, "explain": _EXPLAIN}
_PREFIX_NOTE = {**_STR, "description": (
    "id prefix filter. A TYPE filter needs the trailing hyphen ('PROC-'): 'PROC' also matches ids "
    "such as 'PROCESS-...'.")}
_QS_DESC = (
    " TERMS AND CONDITIONS (all optional, all structured; nothing is parsed out of `text`): `all_of`/"
    "`any_of`/`none_of` take terms, each matched per `match` (phrase | word | prefix); `where` takes "
    "property conditions that return ids; `fields`, `source_file` and `document_tag` narrow the search. "
    "Case rules: TERMS (text, all_of, any_of, none_of) are case-insensitive; `where` equality and "
    "`document_tag` are case-sensitive. where is AND-only: for OR, make two calls. not_one_of/"
    "not_equals never match a node lacking the key: ask `lacks` separately. In `prefix`, a TYPE filter "
    "needs the trailing hyphen ('PROC-'): 'PROC' also matches ids such as 'PROCESS-...'. A call that "
    "uses any of these returns `query_interpreted` (what was evaluated) and, when something looks "
    "wrong (e.g. a misspelled where key), `warnings`; `explain: true` adds the echo to any call.")
_FAILED_CORPORA_NOTE = (
    " A corpus-OMITTED call names every corpus it could not read in `corpora_skipped` "
    "(`{corpus, reason: corpus-file-not-regular|corpus-file-broken-link|corpus-file-unreadable|corpus-dir-unreadable}`; "
    "a failed PRIVATE corpus is counted as `{reason: private-corpus-failed, count}`, never named).")
_FIND_DESC_BASE = (
(
            "FOR A TEXT QUESTION, CALL: lts_find(corpus=..., text=..., rank='relevance'); `text` is ONE "
            "phrase. FOR 'ALL OF THESE WORDS': lts_find(corpus=..., all_of=['w1', 'w2'], match='word'). "
            "Stranger-phrased questions: lts_semantic_find(corpus, text) gives SUGGESTIONS (never matches). "
            "The `semantic` parameter's on_miss mode adds them beside an exact zero (default: server env "
            "LTS_SEMANTIC_ON_MISS).\n""Find decision/spec nodes by taxonomy/prefix/status/free-text, "
                        "optionally scoped to one corpus (project) and a since/until time "
                        "window over a date property (default 'decided-in-session'). "
                        "Omit `corpus` to search every loaded project. "
                        "⛔ `text` MATCHES STRUCTURE ONLY — id, title and STRING property "
                        "values. The WHOLE `text` is matched as ONE case-insensitive substring "
                        "within a single field (the id, the title, or one property value; list "
                        "elements such as repeated `alias` lines count), NOT word by word: "
                        "'borrower card contents' can match where 'borrower contents' does not. "
                        "The text is used as given (not trimmed or whitespace-normalised). "
                        "BODY PROSE IS NOT SEARCHED, so a term present only in "
                        "prose returns a confident ZERO here; every text query's "
                        "`extent.scope_note` says so and routes to `lts_find_in_prose`, "
                        "which covers the body and flags each hit body-only vs "
                        "also-in-structure. `limit`+`order_by` bound the response by NODE "
                        "COUNT rather than by hunting for a `since` window that happens to "
                        "fit — e.g. `limit=30, order_by=\"recency\"` for 'the newest 30' "
                        "regardless of how large the match set is; see their own "
                        "descriptions.") + _QS_DESC)
_FALLBACK_SENTENCE = (
    " On an exact zero of a `text` query in a named corpus, `matched_all_words` lists nodes whose "
    "structure contains every query word (whole words, no stemming; a looser rule than the phrase, "
    "not an exact match).")


def _find_description() -> str:
    """lts_find's description, built at startup: the fallback sentence is advertised ONLY when the
    server process starts with LTS_FIND_WORD_FALLBACK=on (never advertised where it is not live)."""
    return _FIND_DESC_BASE + _FAILED_CORPORA_NOTE + (_FALLBACK_SENTENCE if _qs.fallback_enabled() else "")


# --- SERVER-SIDE TOOL PROMINENCE -------------------------------------------------
# ⛔ THE SERVER DECLARES ITS OWN REFLEXIVE VERBS. `_meta["anthropic/alwaysLoad"] = True`
# asks the client to keep a tool's SCHEMA resident rather than deferring it behind a
# search step.
#
# WHY IT IS ON THE SERVER AND NOT IN ONE CLIENT'S CONFIG. Three agents, independently,
# abandoned this engine for `grep` — and every one of them gave the SAME reason, which is
# not a preference and not a knowledge gap:
#
#     "In my harness the lts-engine tools arrive DEFERRED — names only, no schemas. To
#      call one I must notice I want it → ToolSearch → receive the schema → then call.
#      THREE STEPS. `Read` and `Bash` are in my base tool set with schemas already
#      loaded: ONE step. That asymmetry is small in the abstract and decisive in
#      practice, because IT IS PAID AT THE EXACT MOMENT OF LOWEST MOTIVATION. The
#      instruction to query is a memory; the friction is immediate. Memory loses."
#
# And the contradiction is in the product's own words: `lts_add_property`'s description
# says *"Use this instead of Edit/Write for ANY `- key: value`… There is no 'too small to
# bother'."* ⛔ `Edit` and `Write` are base tools; `lts_add_property` required a
# ToolSearch first. The exhortation is a memory and the gradient is a cost.
#
# ⇒ A CLIENT-SIDE `alwaysLoad` FIXES ONE MACHINE. THE SERVER DECLARING ITS OWN REFLEXIVE
#   VERBS FIXES EVERY CALLER EVERYWHERE — including the stranger who installs this from
#   the open-source release and never reads a manifest.
#
# THE SET IS DELIBERATELY SMALL, because prominence is zero-sum against context: the three
# core READ verbs the bootstrap tells authors to run first, and the three WRITE verbs that
# exist specifically to be cheaper than the bypass. Deferral remains right for the long
# tail (`aggregate`, `flow`, `profile_audit`, `property_loss`, `preview`, `traverse`,
# `capabilities`, `selfcheck`, `lint`) — a tool reached deliberately loses nothing by
# costing a search.
#
# FALSIFIER, and it is already specified: un-defer these and re-run `lts_property_loss`
# with `by_month`. If the loss count does not fall, the friction diagnosis is wrong and
# the curation-versus-system split is the better model.
ALWAYS_LOAD_TOOLS = frozenset({
    # READ — the three the decision-graph sweep says to run before reading any prose.
    "lts_list_corpora", "lts_find", "lts_node",
    # WRITE — the operations an author performs. `lts_repair_property` is here for the
    # reason the whole series exists: on the ONE occasion an author had the sanctioned
    # surface loaded, a live finding in hand and every intention of using it, THE
    # OPERATION THEY NEEDED WAS NOT IN IT, and they bypassed it again with Python. A
    # repair verb that is three steps away closes the found→fixed loop on paper only.
    "lts_add_property", "lts_add_node", "lts_repair_property",
})
_ALWAYS_LOAD_META = {"anthropic/alwaysLoad": True}


def _apply_tool_prominence(tools: list) -> list:
    """Stamp `_meta` onto the reflexive verbs. Applied ONCE, over the whole table, so a
    tool cannot be added to `ALWAYS_LOAD_TOOLS` and then quietly not carry the field —
    the set is the single source and the schemas are derived from it."""
    for spec in tools:
        if spec["name"] in ALWAYS_LOAD_TOOLS:
            spec["_meta"] = {**spec.get("_meta", {}), **_ALWAYS_LOAD_META}
    return tools


TOOLS = [
    {
        "name": "lts_find",
        "description": _find_description(),
        "inputSchema": {"type": "object", "properties": {
            "rank": {"type": "string", "enum": ["default", "relevance"], "description": (
                "relevance: order the SAME exact matches by deterministic BM25 against `text` "
                "(most relevant first; membership and counts unchanged). Needs `corpus` and "
                "`text`; exclusive with order_by. Recommended for text queries: "
                "lts_find(corpus, text, rank='relevance').")},
            "semantic": {"type": "string", "enum": ["off", "on_miss"], "description": (
                "on_miss: on an exact ZERO in a NAMED corpus, add a separate "
                "`did_you_mean_semantic` block of SUGGESTIONS (never matches; count and nodes are "
                "untouched). Default: the server env LTS_SEMANTIC_ON_MISS (off when unset). The first "
                "suggestion block of a session starts with a short `preface`; later ones omit it.")},
            "corpus": _STR, "text": _STR, "prefix": _PREFIX_NOTE, "domain": _STR,
            "path": {**_STR, "description": (
                "ONE `.lts` file to search INSTEAD of a registered corpus (exclusive with `corpus`): it is "
                "parsed in isolation, read-only, with no index and no cache shared with any corpus, and "
                "answers the same exact query as a one-file corpus would. Refused with it: `semantic` other "
                "than off, `rank='relevance'`, `source_file`, `include_reverse_digest`. No domain profile "
                "applies. A refusal never echoes the path; the response carries `file_source` "
                "(sha256, bytes) of the bytes parsed, and the query log keeps only a digest of the path.")},
            "status": _STATUS, "function": _STR, "service": _STR,
            "since": {**_STR, "description": "ISO date lower bound, e.g. 2026-07-20"},
            "until": {**_STR, "description": "ISO date upper bound"},
            "date_property": {**_STR, "description": "property key to filter on (default decided-in-session)"},
            "area": {**_STR, "description": "restrict to one labeled root, e.g. 'decisions'/'specs'/'artifacts'"},
            "token_budget": _BUDGET, "offset": _INT, "tier": _TIER,
            "limit": _LIMIT, "order_by": _ORDER_BY,
            "include_reverse_digest": _INCLUDE_REVERSE_DIGEST, **_QS_PROPS}},
    },
    {
        "name": "lts_semantic_find",
        "description": (
            "SEMANTIC SUGGESTIONS for a question in ONE named corpus: \"what in this corpus is "
            "probably about X?\", for stranger-phrased questions that `lts_find` (exact substring) "
            "returns zero for. Ranked by relevance: keyword statistics fused with embedding "
            "similarity from a local, hash-pinned model (no network).\n"
            "⛔ SUGGESTIONS, NEVER MATCHES. The answer is a labelled block (`kind: "
            "semantic-suggestions-not-matches`, `is_assertion: false`) with no `nodes` and no "
            "match count, and an EMPTY list is NOT evidence of absence. Each candidate carries "
            "`confidence` (`strong match` only when both rankers agree with margin and every "
            "query word is known to the corpus, and never when the block carries `label_policy`; "
            "otherwise `related, not asserted`), "
            "`matched_terms`, `unmatched_terms` (query words NOT in that node: read them, they "
            "flag a plausible neighbour that does not answer), and an `evidence` line. Read the "
            "node with `lts_node` before relying on it. The first suggestion block of a session "
            "starts with a short `preface` saying so; later ones omit it.\n"
            "A candidate whose node is a legacy label (outgoing CANONICAL edge) or is superseded "
            "carries a `caution` naming the canonical node: it annotates and never changes rank, "
            "membership or scores, and exact `lts_find` results do not carry it (v1).\n"
            "`corpus` is REQUIRED (it never fans out; a private corpus is searched only when "
            "named). If the optional model stack is absent or not yet indexed, `status` says "
            "`unavailable` / `index_building` with a reason: that is not an error."),
        "inputSchema": {"type": "object", "properties": {
            "corpus": {**_STR, "description": "the corpus to search (required)"},
            "text": {**_STR, "description": "the question, in your own words (required)"},
            "k": {"type": "integer", "description": "max suggestions (default 10, max 50)"},
            "prefix": _STR, "status": _STATUS,
            "area": {**_STR, "description": "restrict to one labeled root"},
        }, "required": ["corpus", "text"]},
    },
    {
        "name": "lts_report_miss",
        "description": (
            "FILE A KNOWN-ITEM MISS. Use it when you looked for something by a real query, you "
            "KNOW the node exists in the corpus (you have its id, from memory or because you found "
            "it another way), and the search did not return it. It records your query, the node id(s) "
            "you expected and why, plus the evidence captured at report time: each expected id's rank "
            "in the exact `lts_find` result (relevance order) and in `lts_semantic_find` (k=50), or "
            "null if absent. The record goes to the engine's miss log (state dir, never a corpus); "
            "humans and agents use it to fix aliases, titles and recall fixtures. It is READ-ONLY on "
            "every corpus: it edits nothing and fixes nothing.\n"
            "`corpus`, `query`, `expected_ids` (1-10 node ids) and `why` (at most 2,000 characters) are "
            "required. Unknown ids are listed in `unknown_ids`; a report whose ids are ALL unknown is "
            "refused. `query` and `why` are stored literally: file only what you are willing to have "
            "recorded. `corpus` is required (no fan-out; a private corpus only when you name it).\n"
            "Limits: `query` at most 2,000 characters; `tool_used` and `found_via` at most 500 each. The "
            "miss log is a deliberate report, so it is written even when the query log is switched off "
            "(FACTLATTICE_QUERY_LOG=0); it lives in the state dir and is refused if that resolves inside "
            "a corpus root. The response carries `exact_status` and `semantic_status` beside `ranks`: a "
            "null rank with `exact_status: ok` means the exact search ran and did not return the id; "
            "an `error` status means that search itself failed, so the null is NOT evidence of a miss."),
        "inputSchema": {"type": "object", "properties": {
            "corpus": {**_STR, "description": "the corpus you searched (required)"},
            "query": {**_STR, "description": "the exact query you ran (required)"},
            "expected_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10,
                             "description": "node id(s) you know exist and expected back (1-10)"},
            "why": {**_STR, "description": "what you needed and why the result did not serve (max 2,000 characters)"},
            "tool_used": {**_STR, "description": "what you ran: lts_find (default), lts_find_in_prose or lts_semantic_find"},
            "found_via": {**_STR, "description": "how you eventually found the node, e.g. 'lts_node by id from memory'"},
        }, "required": ["corpus", "query", "expected_ids", "why"]},
    },
    {
        "name": "lts_find_in_prose",
        "description": (
            "SEARCH THE BODY TEXT OF NODES — the scope `lts_find` DOES NOT READ. "
            "`lts_find(text=...)` matches only id, title and STRING property values; the "
            "graph index carries no body text at all, so a term that appears only in prose "
            "returns a confident, well-formed ZERO there. This tool covers that gap.\n"
            "⛔ EVERY HIT IS CLASSIFIED, and the flag is most of the value. "
            "`finding: 'body-only'` = the term is in the body and NOWHERE in id, title or "
            "any string property — `lts_find` DOES NOT RETURN THIS NODE, so it is a "
            "DENSIFICATION FINDING: content the graph cannot answer for. "
            "`finding: 'also-in-structure'` = `lts_find` already returns it, and "
            "`structure_hit_in` names where (e.g. `properties.artifact`) — redundant "
            "coverage, not a gap. `body_only=true` filters to the findings alone.\n"
            "IT RETURNS LOCALITY, NOT CONTENT: id, title, source_file, and the matching "
            "line(s) with line numbers (`context=N` for a tight window). To READ a hit, "
            "call `lts_node`. That is deliberate — this is a DIAGNOSTIC for when the "
            "structure failed to answer, not a cheaper `lts_find`; a high body-only count "
            "on a term is a worklist saying the corpus discusses something it does not "
            "assert. Matching is substring and case-insensitive, identical to `lts_find`'s, "
            "and every non-text filter is the SAME predicate, so the two answers are "
            "directly comparable. `tier='digest'` returns EVERY hit as identity + counts "
            "and NEVER a line of body text. A corpus-OMITTED call names every corpus it could not read in `corpora_skipped` (`{corpus, reason: corpus-file-not-regular|corpus-file-broken-link|corpus-file-unreadable|corpus-dir-unreadable}`; a failed PRIVATE corpus is counted as `{reason: private-corpus-failed, count}`, never named)."),
        "inputSchema": {"type": "object", "properties": {
            "text": {**_STR, "description": "the term to find in body text (required, non-empty)"},
            "corpus": _STR, "prefix": _PREFIX_NOTE, "domain": _STR, "status": _STATUS,
            "function": _STR, "service": _STR,
            "area": {**_STR, "description": "restrict to one labeled root, e.g. 'decisions'/'specs'"},
            "since": {**_STR, "description": "ISO date lower bound"},
            "until": {**_STR, "description": "ISO date upper bound"},
            "date_property": _STR,
            "body_only": {"type": "boolean",
                          "description": ("keep ONLY hits absent from id/title/string "
                                          "properties — the densification worklist "
                                          "(default false: everything, flagged)")},
            "context": {**_INT, "description": "lines of context either side of a match (default 0)"},
            "token_budget": _BUDGET, "offset": _INT, "tier": _TIER, **_QS_PROPS},
            "required": ["text"]},
    },
    {
        "name": "lts_node",
        "description": ("Fetch one node: its properties, per-layer forward/reverse edges, "
                        "and a source excerpt. `tier='digest'` returns the node's routing "
                        "row + a COMPLETE edge map and no body at all. A legacy-label or superseded node "
                        "also carries a top-level `caution` naming the canonical node; it annotates, "
                        "never changes rank, and exact `lts_find` results do not carry it (v1)."),
        "inputSchema": {"type": "object", "properties": {
            "corpus": _STR, "node_id": _STR, "token_budget": _BUDGET, "tier": _TIER,
            "include_reverse_digest": _INCLUDE_REVERSE_DIGEST},
            "required": ["corpus", "node_id"]},
    },
    {
        "name": "lts_reverse",
        "description": (
            "WHO POINTS AT THIS NODE? The completeness path for the reverse direction "
            "`lts_node`'s own reverse-edge map cannot answer, because that map is built "
            "ONLY from edges authored INSIDE the target's own corpus — a cross-corpus "
            "referrer, or a same-corpus reference sitting in a property key that never "
            "mints an edge, is invisible there. This tool unions BOTH: typed graph "
            "edges (same-corpus and cross-corpus, `how=\"edge:<TYPE>\"`) and bracketed "
            "`[ID]` references living in a NON-MINTING property key's value "
            "(`how=\"property:<key>\"` — the informal citations `lts_traverse` cannot "
            "see today). [INV-LTS-CORPUS-ISOLATION] holds throughout: a referrer never "
            "attaches to a same-named node in the wrong corpus, and a cross-corpus "
            "reference whose domain qualifier resolves to zero or more than one corpus "
            "is filed as `unresolved_domain_refs`, never guessed. `edge_types`/`verbs` "
            "narrow each channel independently. Paged and bounded like every other "
            "collection here (`count`/`total_matched`/`truncated`/`cursor`); `complete` "
            "is false and `stale_corpora` is non-empty when a source corpus could not "
            "be verified fresh — an incomplete answer is DECLARED, never silently "
            "passed off as the whole graph. `lts_find`/`lts_node`'s `include_reverse_"
            "digest` flag gives a cheap capped PREVIEW of this same answer inline; this "
            "tool is where the complete list lives."),
        "inputSchema": {"type": "object", "properties": {
            "corpus": {**_STR, "description": "the TARGET node's own corpus"},
            "node_id": {**_STR, "description": "the TARGET node id, bare (no doctype prefix)"},
            "edge_types": {"type": "array", "items": _STR,
                           "description": "keep only edge-channel referrers of these types "
                                          "(e.g. ['DEPENDS_ON', 'SUPERSEDES'])"},
            "verbs": {"type": "array", "items": _STR,
                      "description": "keep only property-channel referrers via these "
                                     "NON-MINTING property keys (e.g. ['tracks', 'rationale'])"},
            "token_budget": _BUDGET, "offset": _INT, "limit": _LIMIT},
            "required": ["corpus", "node_id"]},
    },
    {
        "name": "lts_traverse",
        "description": ("Bounded typed-edge walk from a start node (BFS). Filter by "
                        "edge_types (DEPENDS_ON/SUPERSEDES/RESOLVES/CONTAINS/...), layers, "
                        "direction, max_depth, and an optional since/until time window. "
                        "For a BROAD walk prefer `tier='digest'`: it returns EVERY node in "
                        "the walk (never a truncated subset) with no bodies, so you can see "
                        "what is there and then fetch the few nodes you actually need."),
        "inputSchema": {"type": "object", "properties": {
            "corpus": _STR, "start_node": _STR,
            "edge_types": {"type": "array", "items": _STR},
            "layers": {"type": "array", "items": _STR},
            "direction": {**_STR, "enum": ["forward", "reverse", "both"]},
            "max_depth": _INT, "since": _STR, "until": _STR, "date_property": _STR,
            "area": {**_STR, "description": "restrict discovered nodes to one labeled root"},
            "token_budget": _BUDGET, "offset": _INT, "tier": _TIER,
            "limit": _LIMIT, "order_by": _ORDER_BY},
            "required": ["corpus", "start_node"]},
    },
    {
        "name": "lts_capabilities",
        "description": "The capability surface around a topic or node: connected nodes + edges.",
        "inputSchema": {"type": "object", "properties": {
            # `offset` was ABSENT while the response already returned a `cursor` and
            # `_BUDGET`'s own text promised resuming "via `offset`" — the truncation was
            # declared and the resume path did not exist, so the tail of a bounded
            # capability surface was unreachable. See [INV-DECLARED-REACH].
            "corpus": _STR, "topic": _STR, "node_id": _STR,
            "token_budget": _BUDGET, "offset": _INT},
            "required": ["corpus"]},
    },
    # --- RENDER layer ([LEC-RENDER]) -----------------------------------------
    {
        "name": "lts_flow",
        "description": ("Render a process flow (or any subgraph) as a MERMAID diagram, "
                        "DERIVED from the live graph — never a stored snapshot. Walks "
                        "part-of/CONTAINS for step membership and BEFORE/SEQUENCE/CAUSES/"
                        "PRECONDITION_FOR/ENABLES/CASCADES_TO for sequence. Returns BOTH "
                        "`mermaid` text and the structured walk (nodes/edges). A node with "
                        "a `condition` property renders as a decision diamond; `if-true`/"
                        "`if-false`/`branches` become labelled arrows; two arrows into one "
                        "node is convergence; an `actor` property groups nodes into swimlane "
                        "subgraphs. Set `edge_types` to point the SAME renderer at a "
                        "supersession chain (['SUPERSEDES']) or a dependency tree "
                        "(['DEPENDS_ON']). Freshness-gated: a stale corpus returns the "
                        "[INV-LTS-FRESH] marker and NO diagram."),
        "inputSchema": {"type": "object", "properties": {
            "corpus": _STR, "start_node": _STR,
            "edge_types": {"type": "array", "items": _STR,
                           "description": "override the sequence edge set"},
            "max_depth": _INT,
            "direction": {**_STR, "enum": ["forward", "both"]},
            "membership": {"type": "boolean",
                           "description": "descend CONTAINS/PART_OF to collect steps (default true)"},
            "group_by": {**_STR, "description": "node property to group into swimlanes (default 'actor'); null to disable"},
            "layout": {**_STR, "enum": ["TD", "TB", "BT", "LR", "RL"]},
            "include_container": {"type": "boolean",
                                  "description": "draw the start node even when it is a pure container"},
            "title": _STR, "token_budget": _BUDGET, "offset": _INT},
            "required": ["corpus", "start_node"]},
    },
    {
        "name": "lts_list_corpora",
        "description": (
            "Enumerate the loaded project corpora (partitions) with node/edge counts and "
            "freshness. By DEFAULT it SELF-HEALS first: every corpus found stale is "
            "re-indexed through the same self-heal the query tools use (only changed "
            "files re-parsed), and the entry reports the POST-heal state — so a stale "
            "flag here is one that could NOT be cleared, not one a retry would clear. "
            "`healed_corpora` names the corpora this call healed. A corpus whose heal "
            "fails stays `fresh: false` and carries `heal_error` (the reason); a heal "
            "failure never fails the call. `heal=false` = observe only: no heal, no side "
            "effect, today's response shape (for staleness/liveness sweeps). "
            "`registry_choice` (CORPUS-FREE; readable under every registry source) says "
            "WHICH registry this server loaded: `{source: env|user-config|example, path, "
            "sha256_at_boot, sha256_now, resolved_at}`. `sha256_at_boot` is the registry the "
            "server actually loaded; `sha256_now` is the file as it is on disk at this call "
            "(different = the server serves an OLDER registry than the file: restart it). "
            "If the registry could not be resolved at boot the server is in REGISTRY-ERROR "
            "MODE: `registry_choice` is `{source, path, error}`, no corpus is served, every "
            "corpus-scoped tool answers `registry-not-found` with zero data, and only a "
            "server restart ends the mode. The field is ABSENT on a server that predates it "
            "(read that as 'server too old', never as 'nothing to report'). "
            "A corpus whose build FAILED LOUD because one of its files cannot be read safely is NOT served "
            "(never a partial view) and is reported here, not as a registry problem: `failed_corpora` = "
            "[{corpus_id, code, path}] with `code` one of `corpus-file-not-regular` (a FIFO, socket or device, or a "
            "link to one, named *.lts: never opened), `corpus-file-broken-link` (a link that does not resolve), "
            "`corpus-file-unreadable`, `corpus-dir-unreadable` (a directory in the root cannot be listed), and "
            "`path` ROOT-RELATIVE (never absolute). The same entries ride in `extent.corpora_skipped` "
            "(`{corpus, reason: <code>}`). A failed PRIVATE corpus is never named on an unnamed call: it is "
            "counted in `private_failed` and as `{reason: private-corpus-failed, count}`. Every other corpus keeps "
            "serving. A corpus-scoped tool called on a failed corpus answers `{success: false, reason: <code>, "
            "error_class: CorpusFileError, corpus, path}`."),
        "inputSchema": {"type": "object", "properties": {
            "heal": {"type": "boolean", "description": (
                "self-heal stale corpora before reporting (default true). false = "
                "side-effect-free status read that reports staleness as found")}}},
    },
    # --- REGISTRY layer ([REG-RUNTIME-CORPUS]) ---------------------------------
    {
        "name": "lts_register_corpus",
        "description": (
            "Register + build + seat ONE corpus at RUNTIME — no server restart, no "
            "client `/mcp reconnect`. The corpus set is normally built ONCE at server "
            "start; this is the runtime add. Omit `spec` to re-read the corpus registry "
            "from disk (the same registry the server booted from, read FRESH so a "
            "post-boot config edit is picked up; a registry that no longer resolves "
            "refuses with `reason: registry-not-found`) and look up "
            "`corpus_id` there. Pass `spec` to register an AD HOC corpus with no "
            "registry entry at all (e.g. a one-off temp/sample corpus): either "
            "`{roots: [{path, area[, only_file]}, ...], domain_suffix}` (multi-root, "
            "area-labeled — decisions/specs/artifacts) or `{root, domain_suffix}` "
            "(single-root). Refuses, changing nothing, when `corpus_id` is already "
            "loaded (unregister first to replace it), no config entry is found and no "
            "`spec` was given, or the resolved root(s) parsed to zero nodes. On success "
            "the corpus is IMMEDIATELY queryable by every tool and participates in the "
            "ordinary content-hash self-heal exactly like a corpus loaded at boot — "
            "`kernel_seated`/`self_heal_ready` say so explicitly. A successful call is a "
            "SURFACE CHANGE: the server pushes `notifications/tools/list_changed` right "
            "after the response, so a connected client re-lists without being asked. "
            "A root under the OS temp dir is allowed (a temp sample corpus) but the "
            "response carries `warnings` (EPHEMERAL ROOT): writes there are NOT ledgered."),
        "inputSchema": {"type": "object", "properties": {
            "corpus_id": {**_STR, "description": "the corpus id to register (must not already be loaded)"},
            "spec": {"type": "object", "description": (
                "optional ad hoc registry entry (see description); omit to look "
                "`corpus_id` up in the on-disk corpus-config registry instead")}},
            "required": ["corpus_id"]},
    },
    {
        "name": "lts_unregister_corpus",
        "description": (
            "The runtime INVERSE of `lts_register_corpus` — unload one corpus with no "
            "server restart. Unseats it from the freshness kernel and forgets the "
            "self-healer's per-corpus memory BEFORE dropping the partition, so nothing "
            "is left half-tracked; every OTHER loaded corpus is untouched. Cleans up a "
            "temp/ad hoc corpus a runtime `lts_register_corpus` loaded (or any corpus "
            "loaded at boot). Refuses only when `corpus_id` is not currently loaded — "
            "there is no other failure mode; it never touches disk. A successful call "
            "is a SURFACE CHANGE: the server pushes `notifications/tools/list_changed` "
            "right after the response."),
        "inputSchema": {"type": "object", "properties": {
            "corpus_id": {**_STR, "description": "the loaded corpus id to unregister"}},
            "required": ["corpus_id"]},
    },
    # --- ANALYTICS layer ------------------------------------------------------
    {
        "name": "lts_aggregate",
        "description": ("Aggregate over the graph (deterministic fold over `find`, "
                        "freshness-gated + complete). `op`: count | group_by | numeric "
                        "| sum | avg | min | max | distinct | cross_tab | top_k | degree "
                        "| top_by_degree | count_by_edge_type | orphans | exemplifies_coverage. Same filters as "
                        "lts_find. Use `attr`/`attr2` for group/pivot keys, `group_by` for "
                        "a grouped numeric, `k`/`by`/`agg` for top_k, `node` for degree. A corpus-OMITTED call names every corpus it could not read in `corpora_skipped` (`{corpus, reason: corpus-file-not-regular|corpus-file-broken-link|corpus-file-unreadable|corpus-dir-unreadable}`; a failed PRIVATE corpus is counted as `{reason: private-corpus-failed, count}`, never named)."),
        "inputSchema": {"type": "object", "properties": {
            "op": {**_STR, "enum": ["count", "group_by", "numeric", "sum", "avg", "min",
                                    "max", "distinct", "cross_tab", "top_k", "degree",
                                    "top_by_degree", "count_by_edge_type", "orphans",
                                    "exemplifies_coverage"]},
            "corpus": _STR, "attr": _STR, "attr2": _STR, "group_by": _STR,
            "numeric_op": {**_STR, "enum": ["sum", "avg", "min", "max"]},
            "by": _STR, "agg": {**_STR, "enum": ["sum", "avg", "min", "max"]},
            "k": _INT, "node": _STR,
            "direction": {**_STR, "enum": ["both", "in", "out"]},
            "raw": {"type": "boolean"},
            "date_granularity": {**_STR, "enum": ["day", "month", "year"]},
            "limit": _INT,
            "prefix": _STR, "domain": _STR, "status": _STATUS, "area": _STR, "text": _STR,
            "function": _STR, "service": _STR, "since": _STR, "until": _STR,
            "decision_prefixes": {"type": "array", "items": _STR},
            "principle_prefix": _STR, "edge_verb": _STR, **_QS_PROPS},
            "required": ["op"]},
    },
    {
        "name": "lts_profile_audit",
        "description": ("Audit a corpus's ACTUAL vocabulary (ID prefixes, property "
                        "names, edge/relationship types) against its DECLARED profile — "
                        "the re-runnable form of the profile-reconciliation pass. For "
                        "each of prefixes / properties / edge_types: the used set + "
                        "per-item counts, the declared set, and both drift diffs "
                        "(`used_but_unregistered`, `declared_but_unused`). Properties "
                        "also carry the frequency distribution split at `tail_threshold` "
                        "(the power-law core vs. tail). `required_property` + "
                        "`required_scope` (e.g. 'prefix=DEC') give present/total "
                        "conformance for a mandatory field. The declared side is read "
                        "through a swappable ProfileAdapter over the corpus's REGISTERED "
                        "profile (the `profile` key on its corpus-registry "
                        "entry, `.lts` or `.yaml`); `profile.resolved_from` reports "
                        "whether that registration, an explicit argument, or nothing at "
                        "all supplied it, and `available:false` means used-side only. "
                        "ALSO carries `registration` — THE AGREEMENT CHECK between the "
                        "two independent ways a profile is declared: the file's own "
                        "intrinsic `document_tag: PROFILE` (which is what excludes it "
                        "from every content graph) and the config's `profile:` key "
                        "(which says WHICH profile governs WHICH corpus). Their "
                        "disagreement is a finding: `registered_profile_not_tagged_"
                        "profile` (config calls it a profile, the file does not — so it "
                        "is STILL CONTENT), `unregistered_profile_document` (a "
                        "PROFILE-tagged file in a corpus root that nobody registers — "
                        "correctly out of content, but its vocabulary is inert), "
                        "`registered_profile_missing`. "
                        "Freshness-gated: a stale corpus "
                        "returns the [INV-LTS-FRESH] marker and NO numbers. A corpus-OMITTED call names every corpus it could not read in `corpora_skipped` (`{corpus, reason: corpus-file-not-regular|corpus-file-broken-link|corpus-file-unreadable|corpus-dir-unreadable}`; a failed PRIVATE corpus is counted as `{reason: private-corpus-failed, count}`, never named)."),
        "inputSchema": {"type": "object", "properties": {
            "corpus": _STR,
            "profile": {**_STR, "description": "override the declared vocabulary with an explicit profile path (.lts/.yaml/.json); OMIT to use the corpus's registered profile"},
            "required_property": {**_STR, "description": "a property to check for conformance, e.g. invalidating_assumption"},
            "required_scope": {"oneOf": [_STR, {"type": "object"}], "description": "node filter for the conformance check: 'prefix=DEC' or an object of find filters, e.g. {\"prefix\": \"DEC-\", \"where\": [{\"key\": \"status\", \"has\": true}]}"},
            "tail_threshold": {**_INT, "description": "property-frequency core/tail split (default 5)"},
            "prefix": _STR, "domain": _STR, "status": _STATUS, "area": _STR, "text": _STR}},
    },
    # --- LINT layer -----------------------------------------------------------
    {
        "name": "lts_lint",
        "description": ("Lint a FactLattice corpus (a `.lts` file or a directory linted "
                        "recursively as ONE corpus). Structural diagnostics (parse_error, "
                        "collisions, dangling, unregistered_ns, unresolved_target) plus, "
                        "with `dangle`, the bidirectional dangle-lint (forward + orphan). "
                        "Lints-clean == ingests-clean (one parser, no drift). "
                        "THE RESULT DECLARES ITS PROFILE: `profile` = {resolved_from, "
                        "path, domain_id, available, corpus_root} naming the vocabulary "
                        "the verdict was judged against and HOW it was chosen — "
                        "explicit-argument | corpus-config | directory-auto-discovery | "
                        "none. `available:false` means the profile-driven rules (V1-V6: "
                        "prefixes, doc tags, required/recommended properties, enums, "
                        "lifecycle) were INERT, so a clean result there means nothing was "
                        "checked — read that field before trusting a count. `source` and "
                        "`paths` resolve the SAME profile for the same corpus (a file "
                        "list is resolved against its derived common root). "
                        "`profile_documents` names any `.lts` DOMAIN PROFILE in the "
                        "linted set (`document_tag: PROFILE`): parsed in full, but "
                        "excluded from the content graph and therefore from every count "
                        "and finding here — a profile is vocabulary, not content. "
                        "BOUNDED AND SELF-DECLARING: `findings` is budgeted like every "
                        "other collection, and the response always states `count`, "
                        "`total_matched`, `truncated` and a `cursor` to resume from via "
                        "`offset`. `summary` counts the WHOLE CORPUS, never the carried "
                        "page — so `summary.total` == `total_matched`, and a page is "
                        "never mistakable for the verdict."),
        "inputSchema": {"type": "object", "properties": {
            "source": {**_STR, "description": "a .lts file or a corpus directory"},
            "paths": {"type": "array", "items": _STR,
                      "description": "explicit .lts file list instead of `source`; "
                                     "resolves the SAME profile as source=<their common root>"},
            "profile": {**_STR, "description": "domain profile path (.lts/.yaml/.json). "
                                               "OMIT to resolve from the corpus registry "
                                               "(the corpus registry's `profile` key), "
                                               "then directory auto-discovery; the result's "
                                               "`profile.resolved_from` reports which applied"},
            "structure": {"type": "boolean", "description": "run structural diagnostics (default true)"},
            "dangle": {"type": "boolean", "description": "run the bidirectional dangle-lint"},
            # The SAME `_BUDGET` object every other budgeted tool shares — lint is not a
            # special case, and giving it its own schema text is how the contract forks.
            "token_budget": _BUDGET, "offset": _INT}},
    },
    # --- SELFCHECK layer -------------------------------------------------------
    {
        "name": "lts_selfcheck",
        "description": ("Ask THIS RUNNING PROCESS to re-parse the shipped control corpus "
                        "and check its own output against hand-authored ground truth. "
                        "`lts_lint` asks whether a CORPUS is clean; this asks whether the "
                        "PARSER IN THIS PROCESS is correct — a build-stamp check compares "
                        "hashes, this compares BEHAVIOUR, so a stale process fails it "
                        "regardless of what it believes its build to be. Returns an overall "
                        "verdict (PASS | FAIL | CONTROL_MODIFIED), the control's sha256 "
                        "digest, the silent-loss class list it was measured against, and a "
                        "per-case result naming, for each failure, the case, the source "
                        "line, what was expected and what was produced. Takes no arguments "
                        "and needs no loaded corpus. NOTE: measures recall against the "
                        "KNOWN class list only — a green run is not a correctness proof."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    # --- PREVIEW layer ([FL-PREVIEW]) -----------------------------------------
    {
        "name": "lts_preview",
        "description": (
            "WHAT DID MY DOCUMENT BECOME? Submit `.lts` CONTENT (not yet on disk) and "
            "get the graph it would produce, plus everything it would silently LOSE. "
            "This is the authoring loop: compose -> lts_preview -> revise -> repeat -> "
            "only then WRITE into a corpora dir. Landing in a corpus becomes the COMMIT "
            "action, so the corpus stops being where you discover the defect.\n"
            "NOT a linter. `lts_lint` asks 'does this violate a rule?' and answers over "
            "content the parser ALREADY READ; preview asks 'what did this become?' and "
            "reports what the parser read NOTHING from. Measured gap: on a real spec "
            "whose own root `[SPEC:…]` minted no node and whose edge section yielded "
            "0 edges and 0 properties, lint returned 0 errors, exit 0, and ZERO "
            "findings of any kind on the lines that were lost.\n"
            "Returns: (1) BECAME — nodes minted (id/title/type_prefix/document_tag/"
            "parent_section/properties), edges minted (source/type/target/inference), "
            "frontmatter; (2) LOST — two separately named channels: the ratified "
            "line-accounting rule's unaccounted lines (number, text, near-miss kind, and "
            "the linter's own WHY), and every bracketed id AUTHORED IN A DEFINING "
            "POSITION that inflated into nothing; (3) LINE ACCOUNTING — total/consumed/"
            "ignorable/unaccounted + coverage%, per SPEC §2.5's coverage-not-equality "
            "rule; (4) DECLARED vs MINTED; (5) ERRORS, separately; (6) `report`, the "
            "whole thing rendered for reading.\n"
            "⛔ THE RESPONSE DECLARES ITS RESOLUTION MODE. With no `corpus`, mode is "
            "`isolated`: minting and line accounting are exact but REFERENCE INTEGRITY "
            "IS NOT EVALUATED and no dangle is reported (a narrow scope invents phantom "
            "dangles — measured 114 at subset scope vs 56 at full scope). With `corpus`, "
            "mode is `target_corpus`: the content is resolved AS IF IT HAD LANDED there, "
            "so the dangles reported are the dangles you will actually have.\n"
            "EDITS PREVIEW AS REPLACEMENTS. Pass `replaces` for a draft that supersedes "
            "an existing corpus file; without it, a draft that redefines existing ids is "
            "REFUSED (`reason: ambiguous_intent`, with the colliding ids and the likely "
            "file) rather than reported as a wall of duplicate-id collisions. Replace "
            "mode adds `delta` — nodes/edges/properties GAINED and LOST against the "
            "version in the corpus, so an edit that silently drops a node is visible."),
        "inputSchema": {"type": "object", "properties": {
            "content": {**_STR, "description": (
                "the .lts source to preview. PRIMARY input — the authoring loop's draft "
                "is not on disk yet, which is the whole point")},
            "path": {**_STR, "description": (
                "read the content from this file instead (convenience wrapper over "
                "`content`); mutually exclusive with it")},
            "corpus": {**_STR, "description": (
                "the TARGET corpus — a directory path or a loaded corpus id. Switches "
                "reference resolution from `isolated` to `target_corpus`")},
            "replaces": {**_STR, "description": (
                "corpus-relative path of the file this draft SUPERSEDES (an edit). "
                "Omit only for a genuinely new file")},
            "virtual_path": {**_STR, "description": (
                "the filename this content will be written as; labels the result "
                "(default draft.lts)")},
            "profile": {**_STR, "description": "explicit domain profile path (.lts/.yaml/.json)"},
            "verbose": {"type": "boolean", "description": (
                "untruncated property values and line text (default false: values are "
                "clipped so a preview stays cheap in context)")}}},
    },
    # --- AUTHORING layer ([AUTH-PATH-LAYER-1-MCP]) -----------------------------
    {
        "name": "lts_add_property",
        "description": (
            "ADD, REPLACE OR (mode=append) APPEND ONE PROPERTY ON ONE NODE. Use this instead of Edit/Write for "
            "ANY `- key: value` you are adding to an existing `.lts` file — including, "
            "and especially, a one-line change. There is no 'too small to bother'.\n"
            "WHY: a property key must match `^[a-z][a-z0-9_-]*$`. Anything else — a "
            "capital, a pictograph, a `**bold**` wrapper, a space, a comma — makes the "
            "WHOLE LINE parse as PROSE. The field vanishes from `properties`, `lts_find` "
            "cannot see it, and `lts_lint` reports the corpus CLEAN, because prose is not "
            "an error. Measured 2026-08-12: EIGHT such losses by one author in one "
            "session, two of them AFTER building a tool to prevent it and one inside the "
            "document reporting the count. Every instance was a line the author had "
            "emphasised — emphasis was INVERSELY CORRELATED WITH SURVIVAL.\n"
            "ACCOMMODATE THEN REFUSE. Write the key in your natural voice. The tool "
            "normalises what is UNAMBIGUOUS and RECORDS every change in the receipt "
            "(`⛔ What DOES Transfer` -> `what-does-transfer`; `**bold-key**` -> "
            "`bold-key`; `Title Case` -> `title-case`). It refuses ONLY where correcting "
            "would require a CHOICE — a digit-initial key needs an INVENTED prefix, and "
            "choosing one is authoring, not normalisation. ⛔ NOTHING PAST THE FIRST "
            "COLON IS EVER TOUCHED: values legitimately hold mixed-case identifiers, sha "
            "digests and pictographs.\n"
            "THE GATE: after the surgical insert, the file is re-parsed and the parsed "
            "property set must differ from the pre-edit set by EXACTLY this change and "
            "NOTHING ELSE, per node, ACROSS THE WHOLE FILE. That catches an insertion "
            "that landed in a neighbouring node's block and displaced what was there — a "
            "narrower 'did my key parse' check sails straight past it. A refusal writes "
            "NOTHING; the file on disk is either the original or the intended result.\n"
            "THE RECEIPT PROVES ITSELF: absolute path and line number, the key as "
            "authored and as written with every coercion, the value AS THE PARSER "
            "PRODUCED IT, the whole-file per-node delta, the line in situ, and the backup "
            "path. You should not need to run a second tool to believe it.\n"
            "TO RENAME A KEY OR REMOVE A LINE that already parses, this is the wrong tool (it "
            "adds or replaces a value; it can do neither): use `lts_rename_property` / "
            "`lts_remove_property`.\n"
            "ARBITRARY PATHS ARE ACCEPTED — no loaded corpus is required. Corpus-scoped "
            "decision files live with their datasets, outside corpus roots, and those are "
            "the files that most need this."),
        "inputSchema": {"type": "object", "properties": {
            "path": {**_STR, "description": (
                "the .lts file to edit. Absolute or relative; it need NOT be inside a "
                "registered corpus")},
            "node_id": {**_STR, "description": (
                "the bracketed id of the node to amend, WITHOUT the doctype prefix — "
                "`ACME-DECISIONS`, not `SPEC:ACME-DECISIONS`. ID-first, ID-last and "
                "doctyped headings at ANY level h1-h6 are all found, because location "
                "runs through the real parser's heading grammar")},
            "key": {**_STR, "description": (
                "the property key, in your natural voice. Coerced if unambiguous, "
                "refused with a reason if a choice would be required")},
            "value": {**_STR, "description": (
                "everything after the colon. NEVER modified. Must be non-empty — "
                "`- key:` with nothing after it is a NESTED-BLOCK head, not a property")},
            "allow_replace": {"type": "boolean", "description": (
                "replace the key if the node already carries it (default true). Set "
                "false to make an accidental overwrite a refusal")},
            "receipt": {**_STR, "description": (
                "`\"compact\"` (DEFAULT) or `\"full\"`. Compact keeps everything that "
                "PROVES the write — path, node_id, key/coercions, value_as_parsed "
                "(truncated past ~300 chars with the full length stated), line number + "
                "the line, the per-node delta, nodes_checked/nodes_changed, "
                "edges_minted, warnings, backup_path, and root_membership reduced to "
                "{inside_corpus, corpora, warning} — and drops `roots_checked` (every "
                "corpus's roots, echoed on every write) plus the prose `report` and the "
                "full properties_before/properties_after lists. `full` is the old shape, "
                "byte-for-byte, for the rare debugging case. A REFUSAL is NEVER "
                "compacted regardless of this setting — nothing was written, so there is "
                "no bulk to trim")},
            "mode": {**_STR, "enum": ["set", "append"], "description": (
                "`set` (default): add the key, or REPLACE its single value (the receipt now "
                "shows the replace as removed/added under `delta[node].changed`; a key that "
                "already holds SEVERAL values is refused, not overwritten). `append`: add ONE "
                "more `- key: value` line for a MULTI-VALUED key such as `alias`, right after "
                "the last line carrying that key; the gate requires that node's multiset of "
                "values for the key to grow by EXACTLY one and nothing else to move in the "
                "file. An identical key+value is refused; a nested-block key is refused. "
                "`allow_replace` is irrelevant in this mode. Nested-list append supports `-` "
                "bullets only; a `*`-bullet list is refused. Replacing a key that holds a "
                "mapping or nested block is refused in `set` mode. ⚠ Appending to a "
                "lifecycle key such as `status` makes the node invisible to the status "
                "filter (a list value is not a lifecycle value); append is for multi-valued "
                "keys like `alias`")}},
            "required": ["path", "node_id", "key", "value"]},
    },
    {
        "name": "lts_add_node",
        "description": (
            "APPEND A NEW NODE to an existing `.lts` file, at the end of `section`'s "
            "subtree (or the document tail when `section` is omitted). The second of the "
            "two operations authors actually perform; use it instead of Edit/Write.\n"
            "Every key goes through the same accommodate-then-refuse normalisation as "
            "`lts_add_property`, and the WHOLE SET is validated before a single byte is "
            "written — a refusal names every bad key at once rather than one per attempt. "
            "The node id itself is checked against `^[A-Z][A-Z0-9_-]*$`: a heading whose "
            "bracket is not id-shaped mints NO NODE, silently, and the properties under "
            "it are then attributed to the PRECEDING node — a substitution, which is "
            "worse than a loss because it looks well-formed.\n"
            "THE GATE: the file must gain EXACTLY this node with EXACTLY these "
            "properties, and NOTHING may move on any existing node. Refusal writes "
            "nothing. The receipt carries the heading line number, the properties AS "
            "PARSED, the whole-file per-node delta, and the block in situ.\n"
            "Insertion stops before `## Relationships` / `## Causal Graph` regardless of "
            "level: those sections' bullets are edge triples read by a different grammar, "
            "and a node appended after them parses but is misfiled."),
        "inputSchema": {"type": "object", "properties": {
            "path": {**_STR, "description": "the .lts file to append to (arbitrary path)"},
            "node_id": {**_STR, "description": (
                "the new node's id, bare and id-shaped (`^[A-Z][A-Z0-9_-]*$`). Refused if "
                "it already exists in the file")},
            "title": {**_STR, "description": "heading text; the id is appended in brackets"},
            "properties": {"type": "object", "description": (
                "key -> value map. Keys coerced as in lts_add_property; values untouched")},
            "section": {**_STR, "description": (
                "id of the section node to append INSIDE. Omit for the document tail")},
            "level": {**_INT, "description": (
                "heading level override (default: one deeper than the section, capped "
                "at 6; with `create`, 1)")},
            "create": {"type": "boolean", "description": (
                "make the FILE: the path must NOT exist (an existing file, empty or not, or a "
                "symlink there is refused, never overwritten, also under a race of two "
                "creators). The file is created by exclusive create under the per-file lock and "
                "holds frontmatter (`lts: \"1.1\"`, `title`, `domain`/`doctype` when given, "
                "`generated-by`) then this node. Also refused: a missing parent directory "
                "(unless `create_parent`), a path under an ephemeral root (the OS temp dir), "
                "and `section`. One ledger record, with no undo: undo of a create is not "
                "supported. Default false: a missing path is refused as before. A create has exactly "
                "three outcomes, each true: SUCCESS (`written: true`; our file is in the file system or "
                "the ledger; `receipt: incomplete` means the receipt could not be completed; "
                "`ledger_state` is always present on a create reply with exactly one value: "
                "`recorded` (the ledger append returned a record), `unknown` (the append raised: the "
                "create succeeded but whether it was ledgered is unknown, zero or one record exists) or "
                "`skipped` (an ephemeral root, where no record is written)), REFUSED (verified: nothing of this create is "
                "at the path and no ledger record of it exists), or OUTCOME `unknown` "
                "(`success: false, refused: false, written: null`, code `create_outcome_unknown`): the "
                "engine could not determine whether the file was created. On `unknown`, re-read the path "
                "before retrying; a retry never overwrites (it is refused with `create_file_exists` if "
                "anything is there). The file is placed with an exclusive temp file and a no-overwrite hard "
                "link and there is no other way: a filesystem that cannot hard-link (link errno EPERM, "
                "ENOTSUP, EOPNOTSUPP, EXDEV or ENOSYS) is refused `create_link_unsupported`; any other OS "
                "error is `create_os_error` and anything else `create_unexpected_error`. In each case "
                "nothing is left at the path; `evidence.leftover_temp` names a temp file that could not be "
                "removed, if any (also on a success). `unknown`, `ledger_state` and "
                "`create_link_unsupported` appear only for a create; an ordinary call is unchanged")},
            "create_parent": {"type": "boolean", "description": (
                "with `create`: make the ONE missing parent directory (only when its own parent "
                "exists; deeper creation is refused). Recorded in the same single ledger record "
                "(`created_parent` in its delta). Refused without `create`")},
            "doc_title": {**_STR, "description": (
                "with `create`: the frontmatter `title` (default: this node's title)")},
            "domain": {**_STR, "description": "with `create`: the frontmatter `domain` (omitted if absent)"},
            "doctype": {**_STR, "description": "with `create`: the frontmatter `doctype` (omitted if absent)"},
            "receipt": {**_STR, "description": (
                "`\"compact\"` (DEFAULT) or `\"full\"` — see `lts_add_property`'s "
                "`receipt` for the full accounting of what each mode keeps and drops. A "
                "REFUSAL is never compacted")}},
            "required": ["path", "node_id", "title"]},
    },
    {
        "name": "lts_property_loss",
        "description": (
            "[AUTH-PATH-FALSIFIER] COUNT PROPERTY-SHAPED LINES THAT FAILED TO PARSE, per "
            "corpus root, optionally per month. This is the number that can KILL the "
            "authoring-path proposal: if it does not fall once `lts_add_property` is in "
            "use, the diagnosis (a missing feature, not indiscipline) was WRONG.\n"
            "It does NOT simply count the ratified [FL-LINE-ACCOUNTING] near-miss kinds. "
            "That classifier is tuned for PRECISION — the right target for a lint that "
            "nags, the wrong one for a falsifier. Measured on the nine live defect "
            "shapes it flags FOUR; en-dash bullets, a missing space after the colon, a "
            "digit-initial key and a comma in the key region are all called PROSE and "
            "counted as ignorable. A falsifier built on that can FALL while the failure "
            "holds, because the author's habit moved one class sideways.\n"
            "So this uses a wider predicate in CONFIDENCE TIERS — A normalisable (the key "
            "region coerces to legal under the writer's OWN normalisation), B "
            "choice-required (word-shaped but needs an invented prefix/decision), C wide "
            "(sentence-shaped, excluded from the headline because 01 measured that bucket "
            "at 96% prose). HEADLINE = A + B. `## Relationships` / `## Causal Graph` are "
            "skipped: their bullets are edge triples, and a property-shaped check there "
            "fires on every WELL-FORMED edge.\n"
            "It also reports `ratified_lint_recall` — the fraction of real losses the "
            "SHIPPED lint would have shown an author — so the gap between what is "
            "measured and what is warned about is visible instead of being the silent "
            "difference between two numbers nobody compares."),
        "inputSchema": {"type": "object", "properties": {
            "roots": {"type": "array", "items": _STR, "description": (
                "corpus roots or individual .lts files. Directories are scanned "
                "recursively")},
            "by_month": {"type": "boolean", "description": (
                "bucket findings by the month of the commit that last touched each line "
                "(`git blame`), giving a TREND immediately rather than after a month of "
                "waiting. Files outside a work tree are bucketed `unversioned` and "
                "reported, never dropped — those are exactly the volume-resident files "
                "the pre-commit layer cannot see")},
            "limit_examples": {**_INT, "description": "worked examples to include (default 12)"}},
            "required": ["roots"]},
    },
    {
        "name": "lts_repair_property",
        "description": (
            "REPAIR ONE MALFORMED LINE SO IT PARSES. ⛔ `lts_add_property` ADDS; IT "
            "CANNOT REPAIR — and repair is the operation the whole silent-loss series is "
            "about. The losses in every scanned corpus are malformed EXISTING lines:\n"
            "    - ⛔ FLAGGED 2026-08-10: <value>\n"
            "The value was never wrong; the glyph needs to move past the colon so the key "
            "region parses. `lts_add_property` would ADD a second, correct property "
            "BESIDE the broken prose line — the file then carries the fact twice, once "
            "queryable and once not, and the next reader cannot tell which is "
            "authoritative. That is not a fix.\n"
            "Measured: on the ONE occasion in a nine-hour session where an author had "
            "the sanctioned surface loaded, a live `lts_property_loss` finding in hand "
            "and every intention of using it, the operation they needed was not in the "
            "surface — so they fixed it with Python and bypassed the gate again.\n"
            "`line` is 1-based and REQUIRED. That is not a convenience: a repair names "
            "the exact broken line, because the alternative is the tool CHOOSING which "
            "prose line you meant, and choosing is authoring.\n"
            "`value` DEFAULTS to the value already on the line — everything after the "
            "key region, located by the line's own colon or its leading emphasis run. "
            "Where neither exists the tool REFUSES and asks for `value`, rather than "
            "guessing where the key ends: a guessed value is a SUBSTITUTION, and a "
            "substitution is worse than a loss because the result looks well-formed.\n"
            "THE GATE IS `lts_add_property`'s, EXACTLY: after the surgical replacement "
            "the file is re-parsed and the parsed property set must differ from the "
            "pre-edit set by EXACTLY this change and NOTHING ELSE, per node, across the "
            "whole file. A refusal writes nothing.\n"
            "⛔ IT DOES NOT RE-RENDER THE DOCUMENT. Exactly one line changes; every "
            "other byte is untouched. Canonical output over hand-authored prose makes a "
            "diff large enough to hide a real loss inside it, which is the opposite of "
            "what a repair is for. The receipt shows BOTH lines, `-` and `+`.\n"
            "REFUSES when: the line already parses (use lts_add_property), the line is "
            "not inside the named node's body (repairing it there would move the field "
            "onto the WRONG NODE), the line is indented (it belongs to a nested block), "
            "the node already carries the target key, or the key needs an invented "
            "prefix."),
        "inputSchema": {"type": "object", "properties": {
            "path": {**_STR, "description": "the .lts file to repair (arbitrary path)"},
            "node_id": {**_STR, "description": (
                "bracketed id of the node the line belongs to, WITHOUT the doctype "
                "prefix. The line must lie inside this node's body or the repair is "
                "refused")},
            "line": {**_INT, "description": (
                "1-based line number of the malformed line. Take it straight from an "
                "`lts_property_loss` / `lts_lint` key_loss finding")},
            "key": {**_STR, "description": (
                "the key this line was always meant to carry, in your natural voice. "
                "Coerced if unambiguous, refused with a reason if a choice is required")},
            "value": {**_STR, "description": (
                "OPTIONAL. Everything that should follow the colon. Omit to reuse the "
                "value already on the line — the usual case, since the value was not the "
                "thing that broke")},
            "receipt": {**_STR, "description": (
                "`\"compact\"` (DEFAULT) or `\"full\"` — see `lts_add_property`'s "
                "`receipt`. Compact still keeps BOTH lines (`line_before`/`line_after`) — "
                "that is the entire load-bearing proof of a repair. A REFUSAL is never "
                "compacted")}},
            "required": ["path", "node_id", "line", "key"]},
    },
    {
        "name": "lts_rename_property",
        "description": (
            "RENAME THE KEY OF ONE PROPERTY LINE ON ONE NODE; THE VALUE IS BYTE-FOR-BYTE UNTOUCHED. "
            "⛔ `lts_add_property` ADDS OR REPLACES A VALUE and cannot rename a key; "
            "`lts_repair_property` REFUSES a line that already parses. Until this tool the only path "
            "from `- confidence: high` to `- confidence-qualitative: high` was a hand edit — exactly "
            "the act the typed writers exist to prevent.\n"
            "Only the KEY REGION of the one line changes. Mixed-case identifiers, sha digests, "
            "pictographs, colons and trailing spaces in the value, the line's own ending, and EVERY "
            "OTHER BYTE OF THE FILE are the original's (CRLF files stay CRLF; a prose line holding "
            "U+2028 is not rewritten).\n"
            "ACCOMMODATE THEN REFUSE, on `new_key` exactly as `lts_add_property` does it: write the "
            "key in your natural voice; what is UNAMBIGUOUS is normalised and every coercion is "
            "RECORDED in the receipt (`coercions`, `new_key_as_authored`); a digit-initial key needs "
            "an INVENTED prefix, which is authoring, so it is refused. `key` is NOT coerced: it names "
            "an existing line and must match it.\n"
            "THE GATE IS `lts_add_property`'s, EXACTLY: after the surgical edit the whole file is "
            "re-parsed and the parsed delta must be EXACTLY {that node: `key` loses that one value, "
            "`new_key` gains the IDENTICAL value} and NOTHING ELSE on any node. A refusal writes "
            "nothing. Same per-file lock, compare-and-swap atomic write, backup and ONE "
            "node-change ledger record (verb `rename_property`, the line before and after).\n"
            "THE EDGE RULE: a rename can change the GRAPH, not just the file, when `key` or `new_key` "
            "is an edge-minting key (`see-also`, `depends-on`, `relates-to`, ...). The decision is made "
            "on the PARSED edges before vs after, not on key names: `see-also` -> `relates-to` mints the "
            "same edge and needs nothing. When an edge would be removed or added the tool REFUSES and "
            "lists them; `allow_edge_change=true` accepts it and the receipt lists `edges_removed` / "
            "`edges_added`.\n"
            "REFUSES when (the fixed code is `evidence.refusal`): the node or key is absent "
            "(`node_not_found`, `key_absent`); the node carries `key` more than once and no `line` was "
            "given (`key_ambiguous`, naming the lines); `line` is not that key's line on that node "
            "(`line_not_key_line`); `new_key` already exists on the node (`new_key_exists` -- there is "
            "no silent merge); the key heads a nested block or mapping (`key_nested_block` -- v1 does "
            "not rename a block); `new_key` equals `key` after normalisation (`new_key_same`); `new_key` "
            "is illegal (`new_key_illegal`); the rename would move an edge without "
            "`allow_edge_change` (`edge_change_refused`); or the gate fails (`gate_failed`).\n"
            "THE EDGE CHECK (`edge_check`, in every receipt and every edge refusal): the file's corpus is "
            "resolved the way `root_membership` is and that corpus's REGISTERED PROFILE is loaded; the "
            "edge rule parses BEFORE and AFTER with the profile's `property_edges` plus the built-in tables, "
            "so a profile-registered edge key (`documents` -> DOCUMENTS) counts like `see-also` does. When "
            "no profile can be determined the receipt says `edge_check: built-in tables only "
            "(no profile applied: <code>)` (and, except for `corpus_registers_no_profile`, a warning), <code> one "
            "of `outside_every_corpus`, `registry_unavailable`, `profile_file_missing`, `profile_load_failed`, "
            "`corpus_not_in_registry`, `corpus_registers_no_profile`: then ONLY the built-in edge keys are "
            "protected, and the receipt says so instead of staying silent. FAIL CLOSED: when a corpus that "
            "contains the file has an undeterminable profile (missing, unloadable, or seated but absent from "
            "the registry, or there is no registry at all: `registry_unavailable`), a rename or remove of a key that COULD be an edge key is REFUSED with "
            "`edge-context-incomplete`, naming the corpora (`corpus: null` for `registry_unavailable`: with no "
            "registry we cannot know whether the file belongs to a corpus with a profile), unless "
            "`allow_edge_change=true`. The rule is exactly: a built-in minting key in `key` or `new_key`, or an "
            "`[ID]` reference in the value (a profile key mints only from `[ID]` references). A change that really "
            "moves an edge under the vocabulary that WAS determined is refused first as `edge_change_refused`, "
            "which takes precedence over `edge-context-incomplete`. A plain key still proceeds with the receipt "
            "note and a warning; `outside_every_corpus` is complete (no corpus, no profile) and does not refuse.\n"
            "A REFUSAL'S top-level `reason` IS the fixed code (the same value as `evidence.refusal`); the path-free sentence is `explanation`; text that may name a path is `live_detail`. EVERY REFUSAL CODE: " + ", ".join(f"`{c}`" for c in WRITER_REFUSAL_CODES) + ".\n"
            "THE RECEIPT PROVES ITSELF: the absolute path, the line number, BOTH lines (`-` and `+`), "
            "the key as authored and as written, the value AS THE PARSER PRODUCED IT under the new key, "
            "the whole-file per-node delta, the edges that moved, the ledger outcome and the backup path."),
        "inputSchema": {"type": "object", "properties": {
            "path": {**_STR, "description": "the .lts file to edit (arbitrary path; it need NOT be in a registered corpus)"},
            "node_id": {**_STR, "description": (
                "the bracketed id of the node whose line is renamed, WITHOUT the doctype prefix")},
            "key": {**_STR, "description": (
                "the EXISTING key, exactly as it is on the line (legal keys are lowercase). Not coerced")},
            "new_key": {**_STR, "description": (
                "the key to rename it to, in your natural voice. Coerced if unambiguous (every change "
                "recorded in the receipt), refused if a choice is required or if it already exists on "
                "the node")},
            "line": {**_INT, "description": (
                "OPTIONAL, 1-based. REQUIRED when the node carries `key` more than once (a repeated key "
                "accumulates into a list and every element is live): names which line to rename. "
                "Refused unless it is a `- key:` line on this node")},
            "allow_edge_change": {"type": "boolean", "description": (
                "default false. Accept a rename that REMOVES or ADDS a minted edge. With false such a "
                "rename is refused and the edges are listed in `evidence`; with true the receipt lists "
                "`edges_removed` / `edges_added`")},
            "receipt": {**_STR, "description": (
                "`\"compact\"` (DEFAULT) or `\"full\"` -- see `lts_add_property`'s `receipt`. Compact "
                "still keeps BOTH lines (`line_before`/`line_after`), the delta and the edges that moved; "
                "it drops the prose `report`, `roots_checked` and the full properties lists. A REFUSAL is "
                "never compacted")}},
            "required": ["path", "node_id", "key", "new_key"]},
    },
    {
        "name": "lts_remove_property",
        "description": (
            "REMOVE ONE PROPERTY LINE FROM ONE NODE. ⛔ `lts_add_property` cannot remove anything and "
            "`lts_repair_property` only rewrites a line that does NOT parse; a line that parses and "
            "should go (a retired field, a duplicate, the stale half of a rename) could only be deleted "
            "by a hand edit.\n"
            "Exactly the named line leaves the file, together with the wrapped tail the parser folds into its "
            "value and NOTHING else (a sub-bullet, table row or fence under it stays, however indented); a byte-level "
            "gate refuses the write unless the new file is the old file minus exactly those lines. CRLF files stay CRLF.\n"
            "`expect_value` IS THE GUARD AGAINST REMOVING THE WRONG LINE: when given, the tool refuses "
            "unless the line's PARSED value equals it exactly (`expect_value_mismatch`, and the actual "
            "value is in `evidence`). Use it whenever the value is known; it is the difference between "
            "'remove the confidence line' and 'remove the confidence line that says low'.\n"
            "A key that appears more than once on the node (a list) needs `line`, exactly as in "
            "`lts_rename_property`.\n"
            "THE GATE IS `lts_add_property`'s, EXACTLY: the whole file is re-parsed and the parsed delta "
            "must be EXACTLY that one value gone from that node and NOTHING ELSE on any node. A refusal "
            "writes nothing. Same per-file lock, compare-and-swap atomic write, backup and ONE ledger "
            "record (verb `remove_property`, the removed line(s) verbatim).\n"
            "THE EDGE RULE: removing an edge-minting property (`see-also: [X]`) removes a graph edge. "
            "Decided on the PARSED edges before vs after: refused with the edges listed unless "
            "`allow_edge_change=true`; then the receipt lists `edges_removed`.\n"
            "REFUSES when (the fixed code is `evidence.refusal`): node or key absent (`node_not_found`, "
            "`key_absent`); more than one line and no `line` (`key_ambiguous`); `line` is not that key's "
            "line (`line_not_key_line`); the key heads a nested block or mapping (`key_nested_block`); "
            "`expect_value` differs (`expect_value_mismatch`); an edge would move without "
            "`allow_edge_change` (`edge_change_refused`); or the gate fails (`gate_failed`).\n"
            "THE EDGE CHECK (`edge_check`, in every receipt and every edge refusal): the file's corpus is "
            "resolved the way `root_membership` is and that corpus's REGISTERED PROFILE is loaded; the "
            "edge rule parses BEFORE and AFTER with the profile's `property_edges` plus the built-in tables, "
            "so a profile-registered edge key (`documents` -> DOCUMENTS) counts like `see-also` does. When "
            "no profile can be determined the receipt says `edge_check: built-in tables only "
            "(no profile applied: <code>)` (and, except for `corpus_registers_no_profile`, a warning), <code> one "
            "of `outside_every_corpus`, `registry_unavailable`, `profile_file_missing`, `profile_load_failed`, "
            "`corpus_not_in_registry`, `corpus_registers_no_profile`: then ONLY the built-in edge keys are "
            "protected, and the receipt says so instead of staying silent. FAIL CLOSED: when a corpus that "
            "contains the file has an undeterminable profile (missing, unloadable, or seated but absent from "
            "the registry, or there is no registry at all: `registry_unavailable`), a rename or remove of a key that COULD be an edge key is REFUSED with "
            "`edge-context-incomplete`, naming the corpora (`corpus: null` for `registry_unavailable`: with no "
            "registry we cannot know whether the file belongs to a corpus with a profile), unless "
            "`allow_edge_change=true`. The rule is exactly: a built-in minting key in `key` or `new_key`, or an "
            "`[ID]` reference in the value (a profile key mints only from `[ID]` references). A change that really "
            "moves an edge under the vocabulary that WAS determined is refused first as `edge_change_refused`, "
            "which takes precedence over `edge-context-incomplete`. A plain key still proceeds with the receipt "
            "note and a warning; `outside_every_corpus` is complete (no corpus, no profile) and does not refuse.\n"
            "A REFUSAL'S top-level `reason` IS the fixed code (the same value as `evidence.refusal`); the path-free sentence is `explanation`; text that may name a path is `live_detail`. EVERY REFUSAL CODE: " + ", ".join(f"`{c}`" for c in WRITER_REFUSAL_CODES) + ".\n"
            "THE RECEIPT PROVES ITSELF: the absolute path, the line number, every removed line verbatim, "
            "the value removed AS THE PARSER PRODUCED IT, what the key holds now, the whole-file "
            "per-node delta, the edges that moved, the ledger outcome and the backup path."),
        "inputSchema": {"type": "object", "properties": {
            "path": {**_STR, "description": "the .lts file to edit (arbitrary path; it need NOT be in a registered corpus)"},
            "node_id": {**_STR, "description": (
                "the bracketed id of the node the line belongs to, WITHOUT the doctype prefix")},
            "key": {**_STR, "description": "the EXISTING key, exactly as it is on the line. Not coerced"},
            "line": {**_INT, "description": (
                "OPTIONAL, 1-based. REQUIRED when the node carries `key` more than once: names which "
                "line to remove. Refused unless it is a `- key:` line on this node")},
            "expect_value": {**_STR, "description": (
                "OPTIONAL. Refuse unless the PARSED value of the line equals this string exactly (case, "
                "spacing and all). The guard against removing the wrong line")},
            "allow_edge_change": {"type": "boolean", "description": (
                "default false. Accept a removal that REMOVES a minted edge. With false it is refused "
                "and the edges are listed in `evidence`; with true the receipt lists `edges_removed`")},
            "receipt": {**_STR, "description": (
                "`\"compact\"` (DEFAULT) or `\"full\"` -- see `lts_add_property`'s `receipt`. Compact "
                "still keeps the removed line(s) verbatim, the delta and the edges that moved. A "
                "REFUSAL is never compacted")}},
            "required": ["path", "node_id", "key"]},
    },
]

_LEDGER_PROPS = {
    "proposer": {"type": "string", "description": (
        "OPTIONAL audit metadata, <= 200 chars: who proposed this change. Echoed verbatim into the "
        "node-change ledger; never inferred")},
    "approver": {"type": "string", "description": (
        "OPTIONAL audit metadata, <= 200 chars: who approved this change. Echoed verbatim into the ledger")},
    "reason": {"type": "string", "description": (
        "OPTIONAL audit metadata, <= 2000 chars: why this change was made. Stored literally in the ledger")},
    "via": {"type": "string", "description": (
        "OPTIONAL: `direct` (default) or `clerk:<queue_item_id>` -- the route this change took")},
}
for _spec in TOOLS:
    if _spec["name"] in ("lts_add_property", "lts_add_node", "lts_repair_property",
                         "lts_rename_property", "lts_remove_property"):
        _spec["inputSchema"]["properties"].update(_LEDGER_PROPS)
TOOLS.append({
    "name": "lts_history",
    "description": (
        "THE LEDGERED CHANGES TO ONE NODE, newest first (read-only). Every applied typed write "
        "(lts_add_property / lts_add_node / lts_repair_property / lts_rename_property / lts_remove_property / "
        "the document writer) appends one "
        "hash-chained record to a per-file ledger; this returns those for `node_id`: verb, key, the "
        "per-node delta, every value DISCARDED (verbatim, so a change can be undone from the record), "
        "node/file hashes before and after, proposer/approver/reason/via, and the corpora the file "
        "belonged to at write time. ⛔ ABSENCE IS DECLARED: a file with no ledger returns "
        "`ledger: \"none\"` and `history_complete: false` (unknown, not 'nothing happened'); "
        "`ledger_since` is the first record's ts, and changes before it are unknown. `reconciled` is "
        "true when the last record's file hash equals the file on disk now; false means someone edited "
        "the file outside the writers since (reported, not blocked)."),
    "inputSchema": {"type": "object", "properties": {
        "corpus": _STR,
        "node_id": {**_STR, "description": "the node id, bare (no doctype prefix)"},
        "limit": {**_INT, "description": "max records returned, newest first (default 20)"}},
        "required": ["corpus", "node_id"]},
})

_apply_tool_prominence(TOOLS)


# [REG-RUNTIME-CORPUS] tool calls that change the SERVED SURFACE (today: which corpora
# are loaded and queryable). A successful call to any of these gets a
# `notifications/tools/list_changed` push queued right behind its response — see
# `Server.handle`/`serve()` below. Named as a set here (not inlined in `handle`) so a
# future surface-changing tool is one line to add, not a re-derivation of the rule.
SURFACE_CHANGING_TOOLS = frozenset({"lts_register_corpus", "lts_unregister_corpus"})


class Server:
    """The stdio JSON-RPC handler.

    [MCP-LIST-CHANGED] advertises `capabilities.tools.listChanged: true` at
    `initialize` and PUSHES `notifications/tools/list_changed` after any tool call in
    `SURFACE_CHANGING_TOOLS` succeeds — the review-verified pattern (a real Claude Code
    client silently re-lists on this notification, no `/mcp reconnect`) applied to
    lts-engine's own corpus-registry surface. The known trap this closes: advertising
    `listChanged: false` (or omitting the block) at init makes a compliant client ignore
    the push outright — that was measured as an MCP relay's bug, and is exactly why
    `test_init_advertises_list_changed` asserts the capability, not just the push.

    `handle()` returns only the JSON-RPC response for the request just handled (its
    signature is unchanged, so every existing caller — including
    `test_list_corpora_selfheal.py`'s `test_mcp_handle_round_trip` — keeps working
    unmodified). A queued notification is a SEPARATE, sibling `dict` on
    `pending_notifications`; `serve()` drains and writes it immediately after the
    response it followed from. This keeps `handle()` pure return-one-thing while still
    letting one request produce two stdout lines (the MCP stdio framing is
    newline-delimited JSON-RPC objects, and a notification is just one with no `id`).
    """

    def __init__(self, tools: LtsEngine):
        self.tools = tools
        self.pending_notifications: List[dict] = []

    def handle(self, req: dict) -> Optional[dict]:
        method = req.get("method")
        rid = req.get("id")
        # Notifications (no id) get no response.
        if method == "notifications/initialized" or (method and method.startswith("notifications/")):
            return None
        if method == "initialize":
            return self._ok(rid, {"protocolVersion": PROTOCOL_VERSION,
                                  "capabilities": {"tools": {"listChanged": True}},
                                  "serverInfo": SERVER_INFO})
        if method == "tools/list":
            return self._ok(rid, {"tools": TOOLS})
        if method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            result = dispatch(name, arguments, tools=self.tools)
            if name in SURFACE_CHANGING_TOOLS and result.get("success"):
                # Queued, not written here: `handle()` stays transport-decision-free
                # about ORDERING (the response for THIS request must go out first;
                # `serve()` owns stdout and enforces that order).
                self.pending_notifications.append(
                    {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
            # MCP content envelope: JSON payload as text content.
            return self._ok(rid, {"content": [
                {"type": "text", "text": json.dumps(result, indent=2)}],
                "isError": not result.get("success", False)})
        if method == "ping":
            return self._ok(rid, {})
        return self._err(rid, -32601, f"method not found: {method}")

    def drain_notifications(self) -> List[dict]:
        """Pop every notification queued by the request just handled (FIFO, and empty
        after — a `serve()` that forgets to drain would otherwise re-send old
        notifications on the next request it happens to handle)."""
        pending, self.pending_notifications = self.pending_notifications, []
        return pending

    @staticmethod
    def _ok(rid: Any, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    @staticmethod
    def _err(rid: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def build_tools(watch: bool = True, require_corpora: bool = True) -> LtsEngine:
    """The composition root. `require_corpora=False` is for the command-line door
    (`callcli`): a newcomer's first run has no corpora, and the store-free tools (selfcheck,
    lint, preview, the typed writers) must still answer. The server keeps the default.

    ⛔ THE REGISTRY IS RESOLVED ONCE, HERE, BY THE ONE LOOKUP (`registry.resolve_registry`), and the SAME
    resolution both builds the store and is recorded on the engine (`seat_registry`): the hash reported as
    `sha256_at_boot` is the hash of the bytes the store was actually built from, not a second read that
    could have raced an edit. A broken configured registry RAISES `RegistryError` (no example fallback);
    `boot_engine` below turns that into REGISTRY-ERROR MODE for the long-running server, while `call` and
    the reviewer helpers let it stop the run (exit 2)."""
    choice = registry.resolve_registry()
    # ⛔ EVERY exception raised while INTERPRETING the registry (a wrong-shaped entry, an int where a path belongs, ...)
    # is a registry problem, not a crash: v3.1(a) says the server never refuses to boot over a registry. The reason
    # carries the exception CLASS and the registry path only, never a key, a value or a message from the file.
    try:
        store = config.build_store(choice.corpora)
    except Exception as exc:  # noqa: BLE001
        cls = type(exc).__name__
        if choice.path:
            raise registry._error("the registry {path!r} could not be loaded (" + cls + ": it, or one of its corpora, "
                                  "could not be loaded)", choice.source, path=choice.path) from None
        msg = f"the built-in example registry could not be loaded ({cls}: its corpora could not be loaded)"
        raise RegistryError(msg, source=choice.source, persist=msg) from None
    none_loaded = not store.partitions()
    if require_corpora and none_loaded and choice.source == "example" and not store.failed_corpora:
        # the newcomer's commonest case: nothing configured and nothing under the cwd. That is a REGISTRY problem, so
        # it takes the registry-error path (the server boots, lists its tools, and says why), never a bare crash.
        raise registry._error(
            "no corpus registry is configured (set " + registry.env_var_name() + " or create " + registry.pointer_display()
            + ") and the built-in example found no files under {root}", "example", root=choice.root)
    if require_corpora and none_loaded and not store.failed_corpora:
        # (a corpus that FAILED to load with a corpus-file code is not 'no corpus': it is reported by name, see list_corpora)
        # a registry that parses but yields ZERO loadable corpora (every root missing or empty) is error mode too
        raise registry._error("the registry {path!r} parses but none of its corpora could be loaded (every "
                              "root is missing or holds no .lts files)", choice.source, path=choice.path)
    # --- the profile/content split, announced at BOOT -------------------------
    # Domain profiles are excluded from every content graph by their own
    # `document_tag: PROFILE`. Say so once at startup, and say it again louder if the
    # intrinsic signal and `corpus-config.json` DISAGREE — a mis-registered profile is
    # silently-wrong data, and it stayed invisible for weeks precisely because nothing
    # ever printed it. Best-effort: an audit failure must never block the server.
    try:
        from .analytics.profile_registration import audit_profile_registration
        reg = audit_profile_registration(store)
        for path, cids in (reg.get("excluded_profile_documents") or {}).items():
            logger.info("[lts-engine] domain profile excluded from content: %s (in %s)",
                        path, ", ".join(cids))
        for finding in reg.get("findings") or []:
            logger.warning("[lts-engine] PROFILE REGISTRATION DISAGREEMENT (%s): %s",
                           finding["kind"], finding["message"])
    except Exception:  # noqa: BLE001
        logger.exception("[lts-engine] profile-registration audit failed (non-fatal)")
    # --- [LEC-FRESHNESS] seat the incremental freshness kernel ----------------
    # ONE full re-hash here establishes each partition's content-hash leaves + root.
    # After this, every query's freshness check is an O(1) compare against the
    # maintained root (no per-query corpus re-hash). The daemon watcher keeps the root
    # current: a fast mtime-hinted poll each tick, plus a periodic FULL re-verify as
    # the missed-event backstop so a hint-invisible change can never serve stale.
    kernel = FreshnessKernel()
    kernel.seat_store(store)
    store.kernel = kernel
    if watch:
        kernel.start()
    # --- [LEC-SELF-HEAL] make a stale verdict TRANSIENT -----------------------
    # THE composition root. The kernel says WHETHER to rebuild (O(1)); the healer acts on
    # it — on the query path, synchronously, re-parsing ONLY the files whose content hash
    # moved, then re-verifying before anything is served. Without this the server goes
    # dark on the first write to a corpus it has loaded. Set FACTLATTICE_SELF_HEAL=0 to
    # fall back to phase-2's terminal refusal.
    # The freshness gate VERIFIES before it answers (stat-only hint pass; zero content
    # hashes on an unchanged corpus). Without it the maintained root can lag a write by up
    # to a poll interval and the gate answers FRESH over pre-write content — i.e. serves
    # stale — which is exactly the write-then-read window self-heal exists for.
    provider = None
    if _env_flag(_ENV_VERIFY_ON_QUERY, True):
        provider = PollVerifiedFreshness()
    healer = None
    if _env_flag(_ENV_SELF_HEAL, True):
        healer = SelfHealer(store, kernel=kernel, freshness_provider=provider)
    # --- [MECH-BUILD-STAMP] make a stale CODE snapshot self-reconcile ---------
    # The self-heal above makes a stale DATA verdict transient — but ONLY if this process
    # is running the current engine code. A long-running server never reloads: if the engine
    # library is updated on disk under it (as [MECH-SELFHEAL] itself was), the running
    # process keeps executing its start-time snapshot and the on-disk fix is inert. This
    # sentinel stamps the loaded build, watches the on-disk build, and on divergence
    # reconciles by an AUTOMATIC, invisible relaunch (never a manual bounce) — the code
    # analogue of the kernel's [MECH-BACKSTOP]. Off-switch: FACTLATTICE_CODE_WATCH=0.
    if watch:
        from .build_stamp import CodeCurrencySentinel
        CodeCurrencySentinel().start()
    engine = LtsEngine(store, healer=healer, freshness_provider=provider)
    engine.seat_registry(choice)
    return engine


BOOT_RETRY_DELAY_S = 0.5


def boot_engine(*, retry_delay_s: float = BOOT_RETRY_DELAY_S, sleep=time.sleep,
                watch: bool = True, require_corpora: bool = True) -> LtsEngine:
    """The MCP server's boot: `build_tools`, except that a registry that cannot be resolved does NOT stop the
    server. It boots in REGISTRY-ERROR MODE ([v3.1]):

      * every tool is still LISTED (a client that cannot list tools cannot even see the failure);
      * `lts_list_corpora` reports `registry_choice = {source, path, error}` and zero corpora;
      * every corpus-scoped tool answers `registry-not-found` with the reason and ZERO data -- no stale or
        partial registry is ever served;
      * the mode ends ONLY by a server RESTART (deterministic: no mid-session reload state), and the reason
        text says "fix the registry, then restart the server".

    A failure at boot is retried ONCE after a short delay first: an editor that saves by truncate-then-write
    leaves a half-written file for a moment, and a server that boots in that window would sit in error mode
    for a file that is fine a quarter-second later. Only `RegistryError` is caught; any other exception (a
    bug, not a configuration problem) still stops the server loudly."""
    kw = {}
    if not watch:
        kw["watch"] = False
    if not require_corpora:
        kw["require_corpora"] = False
    for attempt in (1, 2):
        try:
            return build_tools(**kw)
        except RegistryError as exc:
            if attempt == 1:
                logger.warning("[lts-engine] registry did not resolve at boot (%s); retrying once in %.2fs",
                               exc, retry_delay_s)
                sleep(retry_delay_s)
                continue
            logger.error("[lts-engine] REGISTRY-ERROR MODE: %s. No corpus data will be served until the "
                         "registry is fixed and the server is restarted.", exc)
            engine = LtsEngine(LtsStore())
            engine.seat_registry_error(exc)
            if watch:
                from .build_stamp import CodeCurrencySentinel
                CodeCurrencySentinel().start()
            return engine
    raise AssertionError("unreachable")        # pragma: no cover


def _iter_requests(stdin, seed: bytes = b""):
    """Yield request lines from a REAL fd without ever exposing a request to a re-exec.

    Defect (ISSUE-UNBOUNDED-READ, part 2): `for line in stdin` consumed
    a request from the pipe BEFORE `serve()` took SERVING_LOCK, so a code-reconcile re-exec
    landing in that window destroyed a request the kernel had already handed over (measured
    ~6% of reconciles under CPU load).

    Protocol: wait for READABILITY with no lock (consumes nothing, so a client that is silent
    or has sent half a line can never hold the lock or block a reconcile); take SERVING_LOCK;
    do ONE read of what is available; process every COMPLETE line under the lock; before
    releasing, publish any leftover INCOMPLETE line via `set_stdin_carry` so a re-exec can
    hand it to its successor. At every release point therefore: all complete requests are
    answered, the only bytes held in user space are the carried partial, and everything else
    is still in the kernel pipe, which survives exec.
    """
    import os
    import select
    from .build_stamp import set_stdin_carry
    fd = stdin.fileno()
    buf = seed
    while True:
        select.select([fd], [], [])              # consumes nothing; no lock held
        SERVING_LOCK.acquire()
        try:
            chunk = os.read(fd, 65536)           # select said readable: does not block
            if not chunk:
                if buf:                          # EOF: an unterminated final line is still
                    line, buf = buf, b""         # answered (as `for line in stdin` did)
                    yield line.decode("utf-8", "replace")
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.decode("utf-8", "replace")
        finally:
            set_stdin_carry(buf)                 # still under the lock
            SERVING_LOCK.release()


def serve(stdin=None, stdout=None) -> None:
    real_fd = stdin is None
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    # Take the inherited carry BEFORE build_tools() starts the code-currency sentinel thread:
    # a reconcile firing in between would otherwise see an empty carry and discard it.
    seed = b""
    if real_fd:
        from .build_stamp import sweep_stale_carry, take_inherited_stdin_carry
        seed = take_inherited_stdin_carry()
        sweep_stale_carry()                      # after take: never sweeps what we inherited
    server = Server(boot_engine())
    lines = _iter_requests(stdin, seed) if real_fd else stdin
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        # [INV-RECONCILE-SERVES] hold the serving lock across dispatch+write so the
        # code-currency sentinel's re-exec lands BETWEEN requests, never mid-response.
        with SERVING_LOCK:
            resp = server.handle(req)
            if resp is not None:
                stdout.write(json.dumps(resp) + "\n")
                stdout.flush()
            # [MCP-LIST-CHANGED] the response for THIS request is already on the wire
            # (line above) before any notification it triggered goes out — a client
            # must never see the push before the call that caused it completes.
            for note in server.drain_notifications():
                stdout.write(json.dumps(note) + "\n")
                stdout.flush()


if __name__ == "__main__":
    serve()
