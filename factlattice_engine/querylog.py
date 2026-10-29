"""QUERY LOG — what was ASKED, so the corpus can be measured by what is READ.

    ⛔ THE ENGINE RECORDED NOTHING ABOUT WHAT WAS ASKED OF IT. No query log, no audit
    trail, no telemetry of any kind. Every instrument in this project measures the corpus
    as an ARTIFACT — does it parse, does it lint, does it keep what it authored. Nothing
    measured it as something people USE.

THE REASON TO BUILD THIS IS NOT LATENCY. It is the question no existing instrument can
answer:

    ⭐ WHICH PARTS OF THE CORPUS ARE NEVER ASKED FOR?

A node that is well-formed, correct, lint-clean, freshly indexed and ENTIRELY UNREAD is
invisible to every check this engine has. It passes all of them. `key_loss` cannot see it,
`lts_property_loss` cannot see it, `profile_audit` cannot see it, and `lts_lint` reports
the file clean — because nothing is wrong with it except that it answers no question
anyone asks. Over a window, a zero-hit node is one of three things: mis-named (nobody
guesses its id), mis-filed (it sits in a corpus nobody queries), or it should not have
been written. All three are actionable and none of them are visible today.

That is the same defect shape this codebase keeps re-finding one level up: a claim with no
DENOMINATOR. `lint clean` without a rule-set, `lost: 0` without coverage, a zero without
an extent — and now a corpus without a read profile. The remedy is the same: measure the
whole population and report the part that is silent.

AND ONE THING THAT IS FREE AT THIS SEAM: RE-QUERY STORMS.
    The same question re-asked in different phrasings inside a short window is a caller
    TELLING YOU THE FIRST ANSWER FAILED. [FL-RECALL-ZERO-IS-AMBIGUOUS] measured that
    behaviour by reading a transcript afterwards — "the correct response to any zero is
    currently to re-query three more ways and then grep". From here it is observable from
    the engine side, live, without anyone keeping a diary.

WHERE IT LIVES, AND WHY NOT IN A CORPUS
    ⛔ NEVER INSIDE A CORPUS ROOT. Telemetry in a parsed root is a live defect this
    project already carries (`corpora/acme-specs/events/quality.jsonl`, queued for
    relocation for exactly this reason): every appended line changes the corpus content
    hash, so the freshness kernel sees a write, marks the partition stale, and re-indexes
    — a log that makes the engine re-parse the corpus it is logging. It also lands the
    log inside the `*.lts` glob's directory, where the corpus-file lint has to reason
    about it. So: `$FACTLATTICE_STATE_DIR`, else `$XDG_STATE_HOME/factlattice`, else
    `~/.local/state/factlattice` — the deployment's own state, outside every root.

WHAT IS LOGGED, AND THE PRIVACY LINE
    STRUCTURE IS LOGGED LITERALLY — tool, corpus, prefix, status, area, tier, since/until.
    Those are VOCABULARY: they name the corpus's own schema, they are what a profile
    already publishes, and they are the fields the analysis needs.

    IDENTIFIERS ARE LOGGED LITERALLY — `node_id`, `start_node`, and the ids ACTUALLY
    RETURNED. This is the one deliberate choice, and the never-read analysis is entirely
    dependent on it: you cannot report which nodes went unread without recording which
    were read. A node id is a public name in the graph — it travels in edges, `see-also`
    lists and citations already — not content.

    ⛔ `text=` IS NOT LOGGED LITERALLY BY DEFAULT. A free-text query carries corpus
    CONTENT, and often the searcher's own working notes. It is recorded as a
    12-hex-character digest plus a length. Set `FACTLATTICE_QUERY_LOG_LITERALS=1` for a
    local dev context.

    ⭐ AND THE DIGEST COSTS THE ANALYSIS NOTHING. Storm detection asks "how many DISTINCT
    phrasings of a question in one window", and distinctness is exactly what a digest
    preserves. The one thing the hash loses — reading the queries — is the one thing that
    would make this a surveillance log rather than an instrument.

ERRORS ARE LOGGED AS STRUCTURE, NEVER AS TEXT (schema change, 2026-10-08, round 6). The record used to hold
    `error`, the first 400 characters of the reply's message; a message can carry a path with any directory name in
    it, and no pattern can make free text safe. A failed call now writes ONLY: `error_class` (the exception class
    name when the reply is `ClassName: ...`), `error_code` (the reply's short `reason` code, e.g. `invalid_limit` or
    `registry-not-found`), and, for a registry refusal ONLY, `error` = the RegistryError's `persist_text` (built from
    structured parts with basenames only). Nothing else from an error message is written, and nothing reads `error`
    back. Where the named path is a directory (the example registry's root, the cwd), the persisted basename is that
    directory's own name; that is accepted.

FAILURE POSTURE: a logging failure NEVER breaks a query. Every write is wrapped, the
failure is reported once per process to the standard logger, and the engine answers. A
telemetry layer that can take the service down is worse than no telemetry.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import re as _re

logger = logging.getLogger(__name__)

ENV_DIR = "FACTLATTICE_STATE_DIR"
ENV_LOG = "FACTLATTICE_QUERY_LOG"           # a path, or "0"/"off" to disable
ENV_LITERALS = "FACTLATTICE_QUERY_LOG_LITERALS"
LOG_BASENAME = "query-log.jsonl"
MISS_LOG_BASENAME = "miss-log.jsonl"          # [QUERY-REPORT-MISS] deliberate known-item miss reports

# The per-record cap on `result_ids`. Generous, because the never-read analysis degrades
# with every id it does not see — and ⛔ when it bites, the record SAYS SO
# (`result_ids_capped`), so a downstream "never read" claim can be qualified instead of
# quietly wrong. Same rule as everywhere else here: a bound is legitimate, a SILENT bound
# is not ([INV-DECLARED-REACH]).
MAX_RESULT_IDS = 500

# The tools whose calls are worth a record. Writes are included deliberately: "which
# nodes are never asked for" is a question about ATTENTION, and an edit is attention.
_LOGGED_TOOLS = frozenset({
    "lts_find", "lts_node", "lts_traverse", "lts_capabilities", "lts_flow",
    "lts_list_corpora", "lts_aggregate", "lts_profile_audit", "lts_lint",
    "lts_preview", "lts_selfcheck", "lts_property_loss",
    "lts_add_property", "lts_add_node", "lts_repair_property",
    "lts_rename_property", "lts_remove_property",
    # [QUERYLOG-SEMANTIC-COVERAGE] reads that were silently unlogged until 2026-10-03
    "lts_semantic_find", "lts_find_in_prose", "lts_reverse", "lts_history",
    # [QUERY-REPORT-MISS] the feedback tool (its free text is NOT logged here: query/why are not in _TEXT_ARGS)
    "lts_report_miss",
})

# Structural filter arguments — logged VERBATIM. Vocabulary, not content.
_STRUCTURAL_ARGS = ("corpus", "prefix", "status", "area", "domain", "since", "until",
                    "tier", "op", "attr", "attr2", "group_by", "function", "service",
                    "date_property", "direction", "max_depth", "edge_types", "layers")
# Identifier arguments — logged VERBATIM. See the module docstring.
_ID_ARGS = ("node_id", "start_node", "topic", "node")
# Free-text arguments — DIGESTED unless literals are enabled.
_TEXT_ARGS = ("text",)
# Path-bearing arguments. Logged as a basename: the analysis needs to know WHICH file was
# edited, and a full path leaks a directory layout for no analytical gain.
_PATH_ARGS = ("path", "source", "roots", "paths")

# One id per PROCESS, so a storm can be attributed to a single caller rather than
# smeared across every client that happened to share a minute.
SESSION_ID = uuid.uuid4().hex[:12]

_warned_once = False


# ---------------------------------------------------------------------------
# location
# ---------------------------------------------------------------------------
def state_dir() -> Path:
    """The deployment's own state directory — ⛔ never a corpus root."""
    env = os.environ.get(ENV_DIR)
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "factlattice"


def miss_log_path() -> Path:
    """[QUERY-REPORT-MISS] <state_dir>/miss-log.jsonl. Unlike the query log it has no off switch: a
    miss report is a deliberate act by the caller. ⛔ never inside a corpus root (the tool checks)."""
    return state_dir() / MISS_LOG_BASENAME


def log_path() -> Optional[Path]:
    """The resolved log path, or None when logging is switched off.

    `FACTLATTICE_QUERY_LOG` may be an explicit path, or `0`/`off`/`false`/`no` to
    disable. Absent, it defaults to ON under `state_dir()` — a telemetry layer that must
    be opted into is a telemetry layer nobody has.
    """
    raw = os.environ.get(ENV_LOG)
    if raw is not None:
        if raw.strip().lower() in ("0", "off", "false", "no", ""):
            return None
        return Path(raw).expanduser()
    return state_dir() / LOG_BASENAME


def literals_enabled() -> bool:
    return os.environ.get(ENV_LITERALS, "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# record construction
# ---------------------------------------------------------------------------
def digest_text(value: object) -> Dict[str, Any]:
    """A free-text query as `{sha, len}` — or verbatim when literals are enabled.

    12 hex characters: collision-free at any plausible log volume, and short enough that
    a human scanning the log can still match two records by eye.
    """
    text = "" if value is None else str(value)
    if literals_enabled():
        return {"text": text, "text_len": len(text)}
    return {"text_sha": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
            "text_len": len(text)}


def _digest_value(value: object) -> Dict[str, Any]:
    """A term / condition VALUE as `{sha, len}` (or `{value, len}` under literals): the same rule
    `text` follows. [FIND-QUERY-SURFACE §9a] Keys, operators and modes are structure and are
    logged literally by the callers; only the values go through here."""
    t = "" if value is None else str(value)
    if literals_enabled():
        return {"value": t, "len": len(t)}
    return {"sha": hashlib.sha256(t.encode("utf-8")).hexdigest()[:12], "len": len(t)}


_QS_MODES = ("phrase", "word", "prefix")
_KEY_RE = None


def _struct(value: object, ok) -> object:
    """Log `value` literally only when it is genuine STRUCTURE (a fixed-vocabulary mode, or a
    well-formed property key); anything else (a typo, a pasted value) is caller content and is
    digested like a term. Without this a refused call could leak a value through a 'structure' field."""
    if isinstance(value, str) and ok(value):
        return value
    return _digest_value(value)


def _qs_filters(a: dict) -> Dict[str, Any]:
    """[FIND-QUERY-SURFACE] the new query-surface arguments as log structure. Never raises on a
    malformed argument (a refused query is logged too)."""
    out: Dict[str, Any] = {}
    import re
    global _KEY_RE
    if _KEY_RE is None:
        _KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
    mode_ok = lambda m: m in _QS_MODES
    key_ok = lambda k: bool(_KEY_RE.match(k))
    default = a.get("match") if mode_ok(a.get("match")) else "phrase"
    for name in ("all_of", "any_of", "none_of"):
        raw = a.get(name)
        if raw is None:
            continue
        rows = []
        for el in (raw if isinstance(raw, list) else [raw]):
            if isinstance(el, dict):
                rows.append({"match": _struct(el.get("match", default), mode_ok),
                             **_digest_value(el.get("term"))})
            else:
                rows.append({"match": str(default), **_digest_value(el)})
        out[name] = rows
    if a.get("match") is not None:
        out["match"] = _struct(a["match"], mode_ok)
    if a.get("fields") is not None:
        fl = a["fields"]
        out["fields"] = ([_struct(f, lambda k: k in ("id", "title") or key_ok(k)) for f in fl]
                         if isinstance(fl, list) else _digest_value(fl))
    # caller VALUES: digest + length unless literals are on (a tag or file name can carry content)
    if a.get("document_tag") is not None:
        out["document_tag"] = _digest_value(a["document_tag"])
    if a.get("source_file") is not None:
        out["source_file"] = _digest_value(a["source_file"])
    if a.get("explain"):
        out["explain"] = True
    wh = a.get("where")
    if wh is not None:
        rows = []
        for c in (wh if isinstance(wh, list) else [wh]):
            if not isinstance(c, dict):
                rows.append({"malformed": True})
                continue
            row: Dict[str, Any] = {"key": _struct(c.get("key"), key_ok)}
            for op in ("has", "lacks", "equals", "one_of", "not_equals", "not_one_of"):
                if op in c:
                    row["op"] = op
                    if op in ("equals", "not_equals"):
                        row.update(_digest_value(c[op]))
                    elif op in ("one_of", "not_one_of") and isinstance(c[op], list):
                        row["values"] = [_digest_value(v) for v in c[op]]
                    break
            rows.append(row)
        out["where"] = rows
    return out


def _basename(value: object) -> object:
    if isinstance(value, (list, tuple)):
        return [os.path.basename(str(v).rstrip("/")) for v in value]
    return os.path.basename(str(value).rstrip("/"))


def result_ids(result: dict) -> List[str]:
    """The node ids this response ACTUALLY DELIVERED, across every response shape.

    ⛔ EMITTED, NOT MATCHED. A truncated result's tail was never seen by the caller, so it
    was not read, and counting it as read would make the never-read report understate
    exactly the population it exists to find. `total_matched` is recorded separately for
    the callers who want the other number.
    """
    out: List[str] = []
    if not isinstance(result, dict):
        return out
    # `hits` is `lts_find_in_prose`'s emitted collection — a prose hit IS attention on that
    # node, and omitting it would make the never-read report overstate exactly the
    # population the prose tool exists to surface.
    for key in ("nodes", "digest", "capabilities", "routed_nodes", "routed_digest",
                "hits"):
        for item in (result.get(key) or []):
            if isinstance(item, dict) and item.get("id"):
                out.append(str(item["id"]))
    node = result.get("node")
    if isinstance(node, dict) and node.get("id"):
        out.append(str(node["id"]))
    for anchor in (result.get("anchor_nodes") or []):
        out.append(str(anchor))
    # Write surfaces name their target explicitly; an edit is attention too.
    if result.get("node_id"):
        out.append(str(result["node_id"]))
    seen: Dict[str, None] = {}
    for nid in out:
        seen.setdefault(nid, None)
    return list(seen)


def suggestions(result: dict) -> Optional[Dict[str, Any]]:
    """[QUERYLOG-SEMANTIC-COVERAGE] the semantic suggestion block a response SHOWED, if any.

    ⛔ Kept apart from `result_ids`: a suggestion shown is not a node read, and counting it
    as attention would make the never-read report understate the silent population.
    """
    if not isinstance(result, dict):
        return None
    blk = result.get("did_you_mean_semantic")
    if not isinstance(blk, dict):
        blk = result if result.get("kind") == "semantic-suggestions-not-matches" else None
    if blk is None:
        return None
    raw = blk.get("candidates")
    raw = raw if isinstance(raw, (list, tuple)) else []   # a malformed block must not drop the record
    cands = [{"id": c.get("id"), "rank": c.get("rank"), "label": c.get("label")}
             for c in raw if isinstance(c, dict)]
    out: Dict[str, Any] = {"status": blk.get("status"), "preface": "preface" in blk,
                           "candidates": cands[:MAX_RESULT_IDS],
                           "capped": len(cands) > MAX_RESULT_IDS}
    if blk.get("label_policy"):
        out["label_policy"] = blk["label_policy"]
    # [SEM-TOPUP-LIMIT] the query-path embedding cost, so a slow on-miss find can be decomposed later.
    # Only for an `ok` block: a building/unavailable block carries placeholder zeros from _ensure's
    # initial info (review F1), and a key the block did not carry stays absent (never a fabricated 0).
    if blk.get("status") == "ok":
        for k in ("embedded_on_query", "nodes_unembedded_stale"):
            if k in blk:
                out[k] = blk[k]
    return out


def build_record(tool: str, arguments: dict, result: dict, elapsed_ms: float,
                 *, session: str = SESSION_ID) -> dict:
    """One JSONL record. Pure — no I/O — so it is testable without a filesystem."""
    a = arguments or {}
    r = result if isinstance(result, dict) else {}

    filters: Dict[str, Any] = {k: a[k] for k in _STRUCTURAL_ARGS if a.get(k) is not None}
    for k in _ID_ARGS:
        if a.get(k) is not None:
            filters[k] = a[k]
    for k in _TEXT_ARGS:
        if a.get(k) is not None:
            filters.update(digest_text(a[k]))
    for k in _PATH_ARGS:
        if a.get(k) is not None:
            if tool == "lts_find" and k == "path":
                # [A-E7] `lts_find(path=...)` names a file the CALLER chose (possibly in a private corpus): a
                # digest and length ALWAYS, even with FACTLATTICE_QUERY_LOG_LITERALS on (stricter than the
                # literal-or-digest rule: a log must not become a map of private file locations).
                t = str(a[k])
                filters[k] = {"sha": hashlib.sha256(t.encode("utf-8", "replace")).hexdigest()[:12], "len": len(t)}
            else:
                filters[k] = _basename(a[k])
    filters.update(_qs_filters(a))

    ids = result_ids(r)
    capped = len(ids) > MAX_RESULT_IDS

    rec: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "session": session,
        "tool": tool,
        "ok": bool(r.get("success", True)),
        "elapsed_ms": round(float(elapsed_ms), 2),
        "filters": filters,
        "count": r.get("count"),
        "total_matched": r.get("total_matched", r.get("total")),
        "truncated": r.get("truncated"),
        "result_ids": ids[:MAX_RESULT_IDS],
        "result_ids_capped": capped,
    }
    sg = suggestions(r)
    if sg is not None:
        rec["suggestions"] = sg
    # [FIND-QUERY-SURFACE §6] the phrase-zero fallback's matches, kept APART from result_ids: they are
    # matches by a looser rule, not the exact answer the caller was shown as such.
    wf = r.get("matched_all_words")
    if isinstance(wf, dict):
        wids = [str(n.get("id")) for n in (wf.get("nodes") or []) if isinstance(n, dict) and n.get("id")]
        rec["word_fallback"] = {"total": wf.get("total"), "ids": wids[:MAX_RESULT_IDS],
                                "capped": bool(wf.get("capped")) or len(wids) > MAX_RESULT_IDS}
    if r.get("tier") is not None:
        rec["tier"] = r["tier"]
    if r.get("error") or r.get("success") is False:
        # STRUCTURE ONLY (see the module docstring): class, code, and a registry refusal's persist_text.
        m = _re.match(r"^([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Warning|Exit|Interrupt)):", str(r.get("error") or ""))
        if m:
            rec["error_class"] = m.group(1)
        # the fixed code: `evidence.refusal` when the reply has one (the typed writers put a SENTENCE in `reason`),
        # else `reason`; either is persisted ONLY if it matches the code pattern (SDLC [TOOL-REASON-IS-A-FIXED-CODE])
        ev = r.get("evidence")
        code = ev.get("refusal") if isinstance(ev, dict) and isinstance(ev.get("refusal"), str) else r.get("reason")
        if isinstance(code, str) and _re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", code):
            rec["error_code"] = code
        if code == "registry-not-found" and r.get("error_persist"):
            rec["error"] = str(r["error_persist"])[:400]
    if r.get("stale") or r.get("refused"):
        rec["refused"] = True
    # ⭐ THE EXTENT RIDES ALONG, and it is what makes a logged ZERO legible months later.
    # Without it a `count: 0` in the log is the same research task the response used to
    # be: genuinely absent, wrong corpus, wrong key, or a stale index — indistinguishable.
    ext = r.get("extent")
    if isinstance(ext, dict):
        rec["extent"] = {
            "nodes_in_scope": ext.get("nodes_in_scope"),
            "corpora_consulted": ext.get("corpora_consulted"),
            "keys_consulted": ext.get("keys_consulted"),
            "index_fresh": (ext.get("index") or {}).get("fresh"),
        }
    return rec


# ---------------------------------------------------------------------------
# the writer
# ---------------------------------------------------------------------------
def write(record: dict) -> bool:
    """Append one record. ⛔ NEVER RAISES — a telemetry failure must not fail a query.

    Reported once per process rather than per call: a log directory that is unwritable
    produces one warning, not one per query, because a warning storm is how an operator
    learns to ignore the channel.
    """
    global _warned_once
    path = log_path()
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001
        if not _warned_once:
            _warned_once = True
            logger.warning("[lts-engine] query log disabled for this process "
                           "(%s: %s); queries are UNAFFECTED", type(exc).__name__, exc)
        return False


def record_call(tool: str, arguments: dict, result: dict, elapsed_ms: float) -> None:
    """Build and append, swallowing everything. The one function `dispatch` calls."""
    if tool not in _LOGGED_TOOLS:
        return
    try:
        write(build_record(tool, arguments, result, elapsed_ms))
    except Exception:  # noqa: BLE001
        pass


class timed:
    """`with timed() as t: ...` -> `t.ms`. A context manager rather than a decorator so
    the dispatch seam stays one readable block."""

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.ms = (time.perf_counter() - self._t0) * 1000.0
        return False


__all__ = ["state_dir", "log_path", "miss_log_path", "MISS_LOG_BASENAME", "literals_enabled", "digest_text", "result_ids",
           "build_record", "write", "record_call", "timed", "SESSION_ID",
           "MAX_RESULT_IDS", "ENV_DIR", "ENV_LOG", "ENV_LITERALS"]
