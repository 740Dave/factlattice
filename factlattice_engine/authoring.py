"""[AUTH-PATH-LAYER-1-MCP] — the WRITE surface, beside `lts_find` and `lts_node`.

WHY A TOOL AND NOT A DOCUMENT
    Proposal 03 [AUTH-PATH-DIAGNOSIS]: the typed writer that already existed gated
    WHOLE-DOCUMENT writes, but the operation people actually perform is "add one
    property to one node in an 800-line file". Constructing a Document to change one
    line is absurd, so the author reached for a raw text edit — and EVERY ONE of the
    eight measured silent losses happened there.

    ⛔ THE BYPASS WAS CHEAPER THAN THE SANCTIONED PATH. That is the whole finding. This
    module makes the sanctioned path cheaper: two MCP calls, no import, no shell, in the
    same tool namespace the author is already in. [AUTH-PATH-LAYER-1-MCP]
    `why-it-inverts-the-gradient` — "that is the only enforcement that does not require
    policing."

THE RECEIPT IS THE POINT
    Principles III.7: the report must CONTAIN the evidence, not assert it. A receipt
    that says "written successfully" and requires the caller to go run `lts_node` to
    find out whether the field exists has moved the verification burden back onto the
    author — which is the failure mode of a diagnostic (03's opening argument: "the
    author who authored the losing line is the author who reads the clean report").

    So every receipt carries, inline:
      - the ABSOLUTE path written and the 1-based line number of the line written
      - the key AS AUTHORED, the key AS WRITTEN, and every coercion between them
      - the value AS THE PARSER PRODUCED IT (not as submitted) — this is the proof the
        field exists in the graph, not merely in the file
      - the WHOLE-FILE parsed-property delta, per node, with the count of nodes checked
      - the written line in situ with two lines of context, so placement is eyeballable
      - the backup path, so reversal needs no reconstruction from memory

    ⛔ COMPACT BY DEFAULT [AUTH-RECEIPT-COMPACT]. The dominant cost of a full receipt is
    `root_membership.roots_checked` (every REGISTERED corpus's roots, echoed on every
    single write) plus the `report` field's prose restatement of fields the receipt
    already carries structurally. Measured: an Intelligence writing ten properties in one
    session burned ~50K tokens on receipts alone. `receipt="compact"` (the DEFAULT) keeps
    every field that PROVES the write landed — path, node_id, key/coercions,
    value_as_parsed (truncated past ~300 chars with the full length stated), line number
    + the line itself, the per-node delta, nodes_checked/nodes_changed, edges_minted,
    warnings, backup_path, and root_membership reduced to {inside_corpus, corpora,
    warning} — and drops `roots_checked`, the prose `report`, and the full
    properties_before/properties_after lists (the delta already proves the change).
    `receipt="full"` is the old shape, byte-for-byte, for the rare debugging case.
    ⛔ A REFUSAL IS NEVER COMPACTED, regardless of `receipt` — nothing was written, so
    there is no bulk to trim, and a refusal that starts losing fields is indistinguishable
    from a refusal dressed as success.

    ⛔ AND NO `preview_fn`. `lts_preview` is not consulted, by design: it runs the same
    parse core the gate runs, so it is not an independent oracle, and its one distinct
    view — the `lost` channel — is measured-insufficient (a dropped key landed in
    `ignorable` beside blank lines while preview reported `lost: 0` at 100.0% coverage).
    An oracle a caller can supply is an oracle a caller can weaken.

    A REFUSAL CARRIES THE SAME EVIDENCE. It is a result, not an exception: nothing was
    written, and the receipt says what would have moved and why that was not the
    intended change.

WHAT IS SHARED WITH THE `/lts-author` SKILL
    Nothing is forked. This module is a THIN adapter over
    `factlattice.authoring.edit`, which is the module the skill's writer path already
    calls. One implementation, two front doors — the skill for a whole document plus a
    contradiction check, these tools for the single-property and single-node edits that
    are ~all of the traffic. Any fix to the gate lands in both at once, by construction.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from factlattice.authoring.edit import (
    EditRefused,
    add_node as _add_node,
    remove_property as _remove_property,
    rename_property as _rename_property,
    repair_property as _repair_property,
    set_property as _set_property,
)

from factlattice import ledger as _ledger
from . import binding as _binding
from . import membership as _membership
from factlattice.registry import RegistryError as _RegistryError

logger = logging.getLogger(__name__)

_TOOL_NAMES = ("lts_add_property", "lts_add_node", "lts_repair_property",
               "lts_rename_property", "lts_remove_property")

# --------------------------------------------------------------------------------
# [NODE-CHANGE-LEDGER] v1 — what the adapter adds to every write: the membership at WRITE TIME
# (`corpora`), the engine build stamp (`engine_head`), and the registered roots the ledger must
# stay out of (L8). `factlattice` itself cannot see the registry, so it is INJECTED from here.
# --------------------------------------------------------------------------------
_ENGINE_HEAD: list = []


def _engine_head() -> Optional[str]:
    """The engine build stamp, computed ONCE per process on the first write (it hashes every engine
    .py file, ~10s of ms, so never per write). It is the build as read from disk at first write, not
    at import; a code change after that is the sentinel's business ([MECH-BUILD-STAMP])."""
    if not _ENGINE_HEAD:
        try:
            from .build_stamp import build_stamp
            _ENGINE_HEAD.append(build_stamp())
        except Exception:  # noqa: BLE001
            _ENGINE_HEAD.append(None)
    return _ENGINE_HEAD[0]


def _annotate(out: dict, path, store) -> dict:
    """`membership.annotate`, except that a registry failure cannot turn a LANDED write into an error.

    `membership` propagates `RegistryError` (it used to swallow it and answer 'no registry'). But this runs
    AFTER the write: the file is changed, the ledger recorded, and a raised exception here would report a
    failure for a write that happened. So the receipt is delivered, with the failure NAMED on it and as a
    warning -- the root-membership verdict is "unverifiable: registry-not-found", never a silent 'none'.
    """
    try:
        return _membership.annotate(out, path, store=store)
    except _RegistryError as exc:
        msg = (f"⚠ ROOT MEMBERSHIP UNVERIFIABLE -- {exc}. The write LANDED; the engine cannot say whether "
               "this file is inside a registered corpus root because the corpus registry does not resolve.")
        out["root_membership"] = {"path": str(path), "inside_corpus": None, "corpora": [], "roots_source": "none",
                                  "registry_error": str(exc), "warning": msg}
        out["warnings"] = list(out.get("warnings") or []) + [msg]
        if isinstance(out.get("report"), str):
            out["report"] += f"\n\nROOT MEMBERSHIP:\n  {msg}"
        return out


def _ledger_kw(path, store, proposer, approver, reason, via) -> dict:
    """The adapter-only ledger kwargs. A registry failure is NEVER swallowed silently: the roots check
    (L8) and `corpora` are skipped, the record carries `corpora_error`, and the caller pops
    `_skipped` to put "ledger roots check SKIPPED: <error>" on the receipt ([F6])."""
    kw = dict(proposer=proposer, approver=approver, reason=reason, via=via, engine_head=_engine_head())
    try:
        roots, _src = _membership.registered_roots(store)
        kw["ledger_roots"] = [r for rs in roots.values() for r in rs]
        kw["corpora"] = _membership.check(str(Path(path).expanduser().resolve()), store=store)["corpora"]
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        kw.update(ledger_roots=None, corpora=None, corpora_error=err, _skipped=err)
    return kw


def _split_skipped(kw: dict):
    return {k: v for k, v in kw.items() if k != "_skipped"}, kw.get("_skipped")


def _apply_ledger(out: dict, ledger: Optional[dict], skipped: Optional[str] = None) -> None:
    """Put the ledger outcome on the FULL receipt (compact copies it). A failure is loud: the write
    landed, the audit record did not."""
    out["ledger"] = ledger
    if skipped:
        out["warnings"] = list(out.get("warnings") or []) + [f"ledger roots check SKIPPED: {skipped}"]
    if ledger and ledger.get("status") == "failed":
        msg = f"WRITTEN, BUT NOT LEDGERED {_ledger.describe(ledger)}"
        out["warnings"] = [msg] + list(out.get("warnings") or [])
        out["report"] = msg + "\n\n" + out["report"]

# --------------------------------------------------------------------------------
# [AUTH-RECEIPT-COMPACT] — the compact receipt transform. A pure RESPONSE-SHAPE
# reduction over an already-built full receipt: nothing here touches the gate, the
# write, or what gets validated. Applied AFTER `_membership.annotate`, so both modes
# see the same root-membership verdict — compact just reads less of it.
# --------------------------------------------------------------------------------

_TRUNC_LEN = 300
_CTX_MARK_RE = re.compile(r"^\s*(\d+)>\s(.*)$")


def _normalize_receipt_mode(receipt: Optional[str]) -> str:
    """Accommodate-then-default, never refuse: an unrecognised value is treated as
    `compact` (the safe, cheap default) rather than erroring a write that already
    landed. Only the literal `"full"` (case-insensitive) opts out."""
    if isinstance(receipt, str) and receipt.strip().lower() == "full":
        return "full"
    return "compact"


def _truncate(value):
    if isinstance(value, str) and len(value) > _TRUNC_LEN:
        return (f"{value[:_TRUNC_LEN]}… [truncated for compact receipt; "
                f"full value is {len(value)} chars — re-run with receipt='full', "
                f"or read it back with lts_node]")
    return value


def _compact_root_membership(rm: Optional[dict]) -> dict:
    """`{inside_corpus, corpora, warning}` — `warning` ALWAYS present (None when clean),
    matching the full-mode contract, so a caller never has to branch on whether the key
    exists before reading it. `roots_checked`/`roots_source` are what this drops."""
    rm = rm or {}
    out = {
        "inside_corpus": rm.get("inside_corpus"),
        "corpora": list(rm.get("corpora") or []),
        "warning": rm.get("warning"),
    }
    if rm.get("registry_error"):                 # ABSENT unless the registry did not resolve
        out["registry_error"] = rm["registry_error"]
    return out


def _marked_line(context) -> tuple[Optional[int], Optional[str]]:
    """Pull the ONE marked line (and its 1-based number) out of an `_context` tuple —
    used where the underlying `EditResult`/`RepairResult` has no separate `line_text`
    field of its own (`add_node`)."""
    for line in context or ():
        m = _CTX_MARK_RE.match(line)
        if m:
            return int(m.group(1)), m.group(2)
    return None, None


def _compact_property_receipt(out: dict, *, is_repair: bool) -> dict:
    compact = {
        "success": out["success"], "written": out["written"], "refused": out["refused"],
        "path": out["path"], "node_id": out["node_id"],
        "key": out["key"], "key_as_authored": out["key_as_authored"],
        "coercions": list(out["coercions"]),
        "value_as_parsed": _truncate(out["value_as_parsed"]),
        "line_number": out["line_number"],
        "delta": out["delta"], "nodes_checked": out["nodes_checked"],
        "nodes_changed": len(out["delta"]),
        "edges_minted": list(out.get("edges_minted") or []),
        "warnings": list(out.get("warnings") or []),
        "backup_path": out.get("backup_path"),
        "root_membership": _compact_root_membership(out.get("root_membership")),
        "ledger": out.get("ledger"),
        "receipt": "compact",
    }
    if is_repair:
        compact["line_before"] = out["line_before"]
        compact["line_after"] = out["line_after"]
    else:
        compact["action"] = out["action"]
        compact["line_text"] = out["line_text"]
    return compact


def _compact_node_receipt(out: dict) -> dict:
    line_number, line_text = _marked_line(out.get("context"))
    props_parsed = out.get("properties_as_parsed") or {}
    compact = {
        "success": out["success"], "written": out["written"], "refused": out["refused"],
        "path": out["path"], "node_id": out["node_id"],
        "title": out.get("title"), "section": out.get("section"),
        "heading_level": out.get("heading_level"),
        "line_number": line_number if line_number is not None else out.get("heading_line"),
        "line_text": line_text,
        "properties_as_parsed": {k: _truncate(v) for k, v in props_parsed.items()},
        "coercions": dict(out.get("coercions") or {}),
        "delta": out["delta"], "nodes_checked": out["nodes_checked"],
        "nodes_changed": len(out["delta"]),
        "edges_minted": list(out.get("edges_minted") or []),
        "warnings": list(out.get("warnings") or []),
        "backup_path": out.get("backup_path"),
        "root_membership": _compact_root_membership(out.get("root_membership")),
        "ledger": out.get("ledger"),
        "receipt": "compact",
    }
    if out.get("created"):
        compact["created"] = True
        compact["created_parent"] = out.get("created_parent")
    return compact


def _render_binding(b: dict) -> list[str]:
    """The BINDING half of a receipt. [FL-RECALL-PROPOSALS] 5b: the write surface
    guaranteed a property LANDS and never said whether it BINDS."""
    lines = ["", "BINDING — does the graph see the relationship?", f"       {b['binding_summary']}"]
    for e in b.get("edges_minted", []):
        tgt = f"{e['target_domain']}:{e['target']}" if e.get("target_domain") else e["target"]
        lines.append(f"       + {e['source']} --{e['type']}({e['layer']})--> {tgt}")
    for ref in b.get("references_seen_but_not_minted", []):
        lines.append(f"       ⚠ [{ref}] seen in the value, NO edge minted")
    return lines


def _render_property_receipt(r) -> str:
    lines = [
        f"WROTE  {r.path}:{r.line_number}",
        f"       {r.line_text}",
        "",
        f"node   [{r.node_id}]   action: {r.action}",
    ]
    if r.coercions:
        lines.append(f"key    {r.key_as_authored!r} -> {r.key!r}")
        lines += [f"         · {c}" for c in r.coercions]
    else:
        lines.append(f"key    {r.key!r}  (no coercion needed)")
    lines += [
        f"value  as parsed: {r.parsed_value!r}",
        "",
        "GATE — the whole-file parsed delta, per node:",
        f"       nodes checked: {r.nodes_checked}   nodes changed: {len(r.delta)}",
    ]
    for nid, d in r.delta.items():
        lines.append(f"       [{nid}] added {d['added']}  removed {d['removed']}")
        for ck, cv in d.get("changed", {}).items():
            lines.append(f"       [{nid}] {ck}: value removed {cv['removed']}  added {cv['added']}"
                         + ("  (reordered)" if cv.get("reordered") else ""))
    lines += [
        f"       [{r.node_id}] properties: {len(r.properties_before)} -> {len(r.properties_after)}",
        "",
        "IN SITU:",
    ] + [f"  {c}" for c in r.context]
    if r.warnings:
        lines += ["", "WARNINGS:"] + [f"  ⚠ {w}" for w in r.warnings]
    lines += ["", f"backup {r.backup_path}"]
    return "\n".join(lines)


def _render_node_receipt(r) -> str:
    lines = [
        f"WROTE  {r.path}:{r.heading_line}   h{r.heading_level}",
        f"node   [{r.node_id}]  {r.title!r}"
        + (f"   under section [{r.section}]" if r.section else "   (document tail)"),
        "",
        "PROPERTIES as parsed:",
    ] + [f"       {k}: {v!r}" for k, v in r.properties.items()]
    if r.coercions:
        lines += ["", "COERCIONS:"] + [f"       {k!r}: {'; '.join(v)}" for k, v in r.coercions.items()]
    lines += [
        "",
        "GATE — the whole-file parsed delta, per node:",
        f"       nodes checked: {r.nodes_checked}   nodes changed: {len(r.delta)}",
    ]
    for nid, d in r.delta.items():
        lines.append(f"       [{nid}] added {d['added']}  removed {d['removed']}")
        for ck, cv in d.get("changed", {}).items():
            lines.append(f"       [{nid}] {ck}: value removed {cv['removed']}  added {cv['added']}"
                         + ("  (reordered)" if cv.get("reordered") else ""))
    lines += ["", "IN SITU:"] + [f"  {c}" for c in r.context]
    if r.warnings:
        lines += ["", "WARNINGS:"] + [f"  ⚠ {w}" for w in r.warnings]
    lines += ["", (f"CREATED the file" + (f" and its parent directory {r.created_parent}" if r.created_parent else "")
                   + " (no backup: there was no prior file; undo of a create is not supported)")
              if r.created else f"backup {r.backup_path}"]
    return "\n".join(lines)


def _render_repair_receipt(r) -> str:
    """⛔ BOTH LINES, ALWAYS. A repair receipt that showed only the result would be
    unreviewable: the whole claim is that ONE line changed and nothing else did."""
    lines = [
        f"REPAIRED  {r.path}:{r.line_number}",
        f"       -  {r.line_before}",
        f"       +  {r.line_after}",
        "",
        f"node   [{r.node_id}]",
        f"key    was {r.key_region!r}  ->  {r.key!r}",
    ]
    lines += [f"         · {c}" for c in r.coercions]
    lines += [
        f"value  from {r.value_source}; as parsed: {r.parsed_value!r}",
        "",
        "GATE — the whole-file parsed delta, per node:",
        f"       nodes checked: {r.nodes_checked}   nodes changed: {len(r.delta)}",
    ]
    for nid, d in r.delta.items():
        lines.append(f"       [{nid}] added {d['added']}  removed {d['removed']}")
        for ck, cv in d.get("changed", {}).items():
            lines.append(f"       [{nid}] {ck}: value removed {cv['removed']}  added {cv['added']}"
                         + ("  (reordered)" if cv.get("reordered") else ""))
    lines += [
        f"       [{r.node_id}] properties: {len(r.properties_before)} -> {len(r.properties_after)}",
        "", "IN SITU:",
    ] + [f"  {c}" for c in r.context]
    if r.warnings:
        lines += ["", "WARNINGS:"] + [f"  ⚠ {w}" for w in r.warnings]
    lines += ["", f"backup {r.backup_path}"]
    return "\n".join(lines)


def _refusal(exc: EditRefused, *, path, **ctx) -> dict:
    """A refusal is a RESULT. Nothing was written; the file on disk is unchanged."""
    out = {
        "success": False,
        "refused": True,
        "written": False,
        "path": str(Path(path).expanduser().resolve()) if path else None,
        "reason": str(exc),
        "evidence": getattr(exc, "evidence", {}) or {},
        "file_unchanged": True,
        **ctx,
    }
    out["report"] = (
        f"REFUSED — nothing written.\n"
        f"  path   {out['path']}\n"
        f"  reason {out['reason']}\n"
        + ("".join(f"\n  evidence.{k} = {v!r}" for k, v in out["evidence"].items())
           if out["evidence"] else "")
    )
    return out


def add_property(path: str, node_id: str, key: str, value: str, *,
                 allow_replace: bool = True, mode: str = "set", store: object = None,
                 receipt: Optional[str] = "compact", proposer: Optional[str] = None,
                 approver: Optional[str] = None, reason: Optional[str] = None,
                 via: Optional[str] = None) -> dict:
    """Add or replace ONE property on ONE node of ONE file. Arbitrary paths accepted.

    `mode="append"` adds one more line for a multi-valued key ([TYPED-WRITER-APPEND]).
    `receipt`: `"compact"` (default) or `"full"` — see [AUTH-RECEIPT-COMPACT] above. A
    refusal ignores this entirely and always carries full reason + evidence."""
    rmode = _normalize_receipt_mode(receipt)
    try:
        _lk, skipped = _split_skipped(_ledger_kw(path, store, proposer, approver, reason, via))
        r = _set_property(path, node_id, key, value, allow_replace=allow_replace, mode=mode, **_lk)
    except EditRefused as exc:
        return _refusal(exc, path=path, node_id=node_id, key_as_authored=key)
    except (OSError, UnicodeDecodeError) as exc:
        return _refusal(EditRefused(f"{type(exc).__name__}: {exc}"), path=path,
                        node_id=node_id, key_as_authored=key)
    bind = _binding.analyse(r.key, r.parsed_value, node_id=r.node_id)
    out = {
        "success": True, "written": True, "refused": False,
        "path": r.path, "node_id": r.node_id, "action": r.action,
        "key": r.key, "key_as_authored": r.key_as_authored,
        "coercions": list(r.coercions),
        "value_as_submitted": r.value, "value_as_parsed": r.parsed_value,
        "line_number": r.line_number, "line_text": r.line_text,
        "properties_before": list(r.properties_before),
        "properties_after": list(r.properties_after),
        "delta": r.delta, "nodes_checked": r.nodes_checked,
        "nodes_changed": len(r.delta),
        "context": list(r.context), "backup_path": r.backup_path,
        "warnings": list(r.warnings),
        # [FL-RECALL-PROPOSALS] 5b — the receipt now says whether the property BINDS.
        **{k: bind[k] for k in ("edges_minted", "references_seen_but_not_minted",
                                "nearest_minting_alternatives", "key_mints")},
        "binding": bind,
        "gate": ("the parsed property set differs from the pre-edit set by EXACTLY this "
                 "change, per node, across the whole file — both sides read by "
                 "factlattice.parser.parse_text"),
        "report": _render_property_receipt(r) + "\n" + "\n".join(_render_binding(bind)),
        "receipt": "full",
    }
    if bind["references_seen_but_not_minted"]:
        out["warnings"] = list(out["warnings"]) + [bind["binding_summary"]]
    _apply_ledger(out, r.ledger, skipped)
    out = _annotate(out, r.path, store)
    if rmode == "compact":
        return _compact_property_receipt(out, is_repair=False)
    return out


def _utf8_ok(v) -> bool:
    try:
        v.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _create_arg_problem(path, node_id, title, properties, section, level, doc_title, domain, doctype,
                        proposer=None, approver=None, reason=None, via=None):
    """[P2-B, delta2 F1] up-front TYPE validation for a create: the name of the first wrong-typed argument
    and what it was, or None. Only names and types are reported, never a value (a path may be private).
    `path` may be a str or an os.PathLike (it worked before the create path existed)."""
    def bad(name, want, v):
        return f"`{name}` must be {want} (got {type(v).__name__})"
    try:
        p = os.fspath(path)
    except TypeError:
        p = None
    if not isinstance(p, str) or not p.strip() or "\x00" in p or not _utf8_ok(p):
        return ("`path` must be a non-empty string or path object without NUL characters and encodable as UTF-8"
                + ("" if isinstance(p, str) else f" (got {type(path).__name__})"))
    if not isinstance(node_id, str):
        return bad("node_id", "a string", node_id)
    if not isinstance(title, str):
        return bad("title", "a string", title)
    if properties is not None and not isinstance(properties, dict):
        return bad("properties", "an object (key -> value)", properties)
    for k in (properties or {}):
        if not isinstance(k, str):
            return f"every `properties` key must be a string (got {type(k).__name__})"
    if section is not None and not isinstance(section, str):
        return bad("section", "a string", section)
    if level is not None and (isinstance(level, bool) or not isinstance(level, int)):
        return bad("level", "an integer", level)
    for n, v in (("doc_title", doc_title), ("domain", domain), ("doctype", doctype),
                 ("proposer", proposer), ("approver", approver), ("reason", reason), ("via", via)):
        if v is not None and not isinstance(v, str):
            return bad(n, "a string", v)
    for n, v in (("node_id", node_id), ("title", title), ("section", section), ("doc_title", doc_title),
                 ("domain", domain), ("doctype", doctype), ("proposer", proposer), ("approver", approver),
                 ("reason", reason), ("via", via)):
        if isinstance(v, str) and not _utf8_ok(v):
            return f"`{n}` is not encodable as UTF-8 (a lone surrogate?)"
    for k, v in (properties or {}).items():
        if not _utf8_ok(k) or (isinstance(v, str) and not _utf8_ok(v)):
            return "a `properties` key or value is not encodable as UTF-8 (a lone surrogate?)"
    return None


def _safe_exc_name(exc: BaseException) -> str:
    n = type(exc).__name__
    return n if n.isidentifier() and len(n) <= 80 else "Exception"


def add_node(path, node_id, title,
             properties: Optional[dict] = None, *, section: Optional[str] = None,
             level: Optional[int] = None, store: object = None,
             receipt: Optional[str] = "compact", proposer: Optional[str] = None,
             approver: Optional[str] = None, reason: Optional[str] = None,
             via: Optional[str] = None, create: bool = False, create_parent: bool = False,
             doc_title: Optional[str] = None, domain: Optional[str] = None,
             doctype: Optional[str] = None) -> dict:
    """The tool-layer `lts_add_node`. WITHOUT `create`/`create_parent` this is the unchanged writer.

    WITH either flag ([delta5]) the reply is exactly one of three TRUE things, decided at ONE observation
    point AFTER the core exits (however it exits), from the inode we made, the file system and the ledger:
      * success  (`written: true`): our create is in the file system or the ledger. A normal success is
        returned untouched; otherwise `receipt: "incomplete"` with the exception TYPE NAME or `inner_refusal`
        CODE, and `ledger` "recorded" / "missing" / "unknown";
      * refused: VERIFIED, nothing of ours is at the path and no ledger record of this create exists;
      * `outcome: "unknown"` (`success: false, refused: false, written: null`, code
        `create_outcome_unknown`): the observation could not be completed or cannot rule our write in or
        out. Re-read the path before retrying; a retry never overwrites.
    `outcome: "unknown"` exists ONLY here: a call without `create`/`create_parent` is the unchanged writer.
    No exception message reaches a create reply; one server-side warning line names type, stage and
    observation."""
    if not (create or create_parent):
        return _add_node_impl(path, node_id, title, properties, section=section, level=level, store=store,
                              receipt=receipt, proposer=proposer, approver=approver, reason=reason, via=via,
                              create=create, create_parent=create_parent, doc_title=doc_title,
                              domain=domain, doctype=doctype)
    # THE CHOKE POINT ([delta5]). Whatever the core does (returns success, returns ANY refusal, raises ANY
    # Exception) the outcome is computed AFTER it exits, from OBSERVATION, never from an inner handler. The
    # reply is exactly one of three TRUE things: success (our create is in the file system or the ledger),
    # refused (VERIFIED: nothing of ours at the path and no ledger record of this create), or
    # `outcome: "unknown"` (the observation could not be completed, or cannot rule our write in or out).
    state: dict = {"stage": "before_write"}
    result, raised = None, None
    try:
        result = _add_node_impl(path, node_id, title, properties, section=section, level=level, store=store,
                                receipt=receipt, proposer=proposer, approver=approver, reason=reason, via=via,
                                create=create, create_parent=create_parent, doc_title=doc_title,
                                domain=domain, doctype=doctype, _state=state)
    except Exception as exc:  # noqa: BLE001 -- the choke point IS the point
        raised = exc
    try:
        return _decide_create_outcome(state, result, raised, node_id)
    except Exception:  # noqa: BLE001 -- even deciding must not escape: say what we cannot say
        return _outcome_unknown(state)


def _observe_disk(state: dict):
    """True: OUR file (same (st_dev, st_ino) as the inode we made) is at the path, or `written` was set.
    False: VERIFIED not ours: the link was never attempted, nothing is at the path, or the link call itself
    failed and what is there is another inode (`not_ours`). None: could not tell (a stat failed, or another
    inode is there with no evidence of how it got there). Identity is the INODE, never the bytes."""
    try:
        if state.get("written"):
            return True
        if not state.get("link_attempted") or state.get("ident") is None:
            return False
        from factlattice.authoring.edit import _lstat_ident
        there = _lstat_ident(state["real"])
        if there is None:
            return False
        if there == tuple(state["ident"]):
            return True
        return False if state.get("not_ours") else None
    except Exception:  # noqa: BLE001
        return None


CREATE_STATE_CLAIM = "Nothing of this create is at the path; no ledger record of it."
LEDGER_UNKNOWN_WARNING = "the create succeeded but whether it was ledgered is unknown"
UNKNOWN_REASON = "could not determine whether the file was created; re-read the path before retrying"


def _note_leftover_temp(res: dict, state) -> None:
    """A temp file that could not be removed after a SUCCESSFUL create is reported (the TEMP path only)."""
    lo = (state or {}).get("leftover_temp")
    if lo:
        res["evidence"] = {**(res.get("evidence") or {}), "leftover_temp": lo}
        res["warnings"] = list(res.get("warnings") or []) + [
            "a temporary file could not be removed and was left beside the new file; remove it by hand (evidence.leftover_temp)"]


def _state_claim(result: dict) -> dict:
    """A verified refusal says what is TRUE (a state claim), not the old "Nothing written." """
    for k in ("reason", "report"):
        if isinstance(result.get(k), str):
            result[k] = result[k].replace("Nothing written.", CREATE_STATE_CLAIM)
    return result


def _decide_create_outcome(state, result, raised, node_id):
    disk = _observe_disk(state)
    seq = state.get("ledger_seq")                      # set only when `_ledger_append` RETURNED with a record
    inner = None
    if raised is None and isinstance(result, dict) and result.get("refused"):
        inner = str((result.get("evidence") or {}).get("refusal") or "none")
    if raised is None and not inner:
        return result              # the core succeeded and said so (a ledger fault rides in `ledger_state`)
    name = (_safe_exc_name(raised) if raised is not None
            else state.pop("caught", None) or f"refusal:{inner}")
    written = disk is True or seq is not None
    verified_absent = disk is False and seq is None
    # one server-side line: the exception TYPE (or inner refusal code), the stage and the observation; never a message
    logger.warning("lts_add_node create: unexpected %s at stage=%s disk=%s ledger_seq=%s", name,
                   "after_write" if written else "before_write" if verified_absent else "unknown", disk, seq)
    if written:
        return _written_but_incomplete(state, name if raised is not None else None,
                                       node_id=node_id if isinstance(node_id, str) else None,
                                       inner_refusal=inner, disk=disk, seq=seq)
    if verified_absent:
        if raised is None:
            return _state_claim(result)                   # an inner refusal, and nothing of ours exists
        return _state_claim(_refusal(EditRefused(
            "the create failed unexpectedly before anything was written. Nothing written.",
            evidence={"refusal": "create_unexpected_error", "exception": name,
                      **({"leftover_temp": state["leftover_temp"]} if state.get("leftover_temp") else {})}),
            path=None))
    return _outcome_unknown(state)


def _outcome_unknown(state: dict) -> dict:
    """The observation could not be completed (or could not rule our write in or out). Built from constants
    so that it cannot itself fail; never a refusal and never a success."""
    real = state.get("real") if isinstance(state, dict) else None
    return {"success": False, "refused": False, "written": None, "outcome": "unknown",
            "reason": UNKNOWN_REASON, "evidence": {"refusal": "create_outcome_unknown"},
            "ledger_state": "unknown", "path": real if isinstance(real, str) else None}


def _written_but_incomplete(state: dict, exc_name, *, node_id=None, inner_refusal=None, disk=True, seq=None) -> dict:
    """Our create is in the file system (by inode) or the ledger, so this is NEVER a refusal. `ledger` is
    "recorded" iff `_ledger_append` RETURNED (a seq); otherwise the ledger state is its OWN top-level field,
    `ledger_state: "unknown"` (zero or one record exists), with a fixed warning. Exception TYPE NAME or inner
    refusal CODE only."""
    warnings = ["WRITTEN, but the rest of the create failed after the file was made; re-read the file to verify."
                if disk is not False else
                "A ledger record of this create exists but the file could not be confirmed at the path; re-read it."]
    out = {"success": True, "written": True, "refused": False, "path": state.get("real"), "node_id": node_id,
           "created": True, "created_parent": state.get("created_parent"),
           "receipt": "incomplete", "warnings": warnings}
    if exc_name:
        out["receipt_error"] = exc_name
    if inner_refusal:
        out["inner_refusal"] = inner_refusal
    if seq is not None:
        out["ledger"] = "recorded"
        out["ledger_state"] = "recorded"
    else:
        out["ledger_state"] = "unknown"
        warnings.append(LEDGER_UNKNOWN_WARNING)
    _note_leftover_temp(out, state)
    return out


def _type_only(text) -> str:
    """For a create reply: the exception TYPE from a "Type: message" string, never the message."""
    head = str(text).split(":", 1)[0].strip()
    return head if head.isidentifier() and len(head) <= 80 else "ledger error"


def _add_node_impl(path, node_id, title,
             properties: Optional[dict] = None, *, section: Optional[str] = None,
             level: Optional[int] = None, store: object = None,
             receipt: Optional[str] = "compact", proposer: Optional[str] = None,
             approver: Optional[str] = None, reason: Optional[str] = None,
             via: Optional[str] = None, create: bool = False, create_parent: bool = False,
             doc_title: Optional[str] = None, domain: Optional[str] = None,
             doctype: Optional[str] = None, _state: Optional[dict] = None) -> dict:
    """Append a NEW node at the end of `section`'s subtree (or the document tail).

    `create=True` ([A-E1]) makes the FILE too (exclusive create; frontmatter, then this node);
    `create_parent=True` may make exactly one missing parent directory.

    `receipt`: `"compact"` (default) or `"full"` — see [AUTH-RECEIPT-COMPACT] above."""
    mode = _normalize_receipt_mode(receipt)
    if create or create_parent:
        problem = _create_arg_problem(path, node_id, title, properties, section, level,
                                      doc_title, domain, doctype, proposer, approver, reason, via)
        if problem:
            return _refusal(EditRefused(f"{problem}. Nothing written.",
                                        evidence={"refusal": "create_argument_invalid"}),
                            path=None, node_id=None)
        path = os.fspath(path)
    try:
        _lk, skipped = _split_skipped(_ledger_kw(path, store, proposer, approver, reason, via))
        r = _add_node(path, node_id, title, dict(properties or {}), section=section,
                      level=level, **_lk, create=bool(create), create_parent=bool(create_parent),
                      doc_title=doc_title, domain=domain, doctype=doctype,
                      _create_state=_state if (create or create_parent) else None)
    except EditRefused as exc:
        from factlattice.authoring.edit import KNOWN_CREATE_CODES
        if (create or create_parent) and (exc.evidence or {}).get("refusal") == "lock_unavailable":
            # [delta6 F4] the lock text carries the state-dir path and the OS message: code + fixed sentence only
            exc = EditRefused("the per-file write lock could not be taken; the create was not attempted.",
                              evidence={"refusal": "lock_unavailable"})
        elif (create or create_parent) and (exc.evidence or {}).get("refusal") not in KNOWN_CREATE_CODES:
            # [delta2 F3] by construction: a create refusal no core helper coded (e.g. a ledger-metadata
            # bound such as an over-long `reason`) still carries a stable code. [delta4 F4] and its MESSAGE
            # is not ours to relay (it may carry a path): a fixed sentence replaces it.
            if _state is not None:
                _state["caught"] = "EditRefused"
            exc = EditRefused("the create was refused. Nothing of the request was relayed.",
                              evidence={**(exc.evidence or {}), "refusal": "create_refused"})
        return _refusal(exc, path=path, node_id=node_id, section=section)
    except (OSError, UnicodeDecodeError) as exc:
        if create or create_parent:
            if _state is not None:
                _state["caught"] = _safe_exc_name(exc)
            # [P2-B] a create's OS failure: a stable code and the errno NAME only (the exception text
            # carries the path, which may be a private location)
            en = errno.errorcode.get(getattr(exc, "errno", None) or 0)
            ev = {"refusal": "create_os_error"}
            ev.update({"errno": en} if en else {"exception": _safe_exc_name(exc)})   # errno ONLY an errno name
            if _state and _state.get("leftover_temp"):
                ev["leftover_temp"] = _state["leftover_temp"]
            return _refusal(EditRefused(f"the create failed with {en or _safe_exc_name(exc)}. Nothing written.",
                                        evidence=ev),
                            path=path, node_id=node_id, section=section)
        return _refusal(EditRefused(f"{type(exc).__name__}: {exc}", evidence={"refusal": "os_error"}),
                        path=path, node_id=node_id, section=section)
    try:
        bind = _binding.analyse_many(r.properties, node_id=r.node_id)
        out = {
            "success": True, "written": True, "refused": False,
            "path": r.path, "node_id": r.node_id, "section": r.section, "title": r.title,
            "heading_level": r.heading_level, "heading_line": r.heading_line,
            "properties_as_parsed": r.properties, "coercions": r.coercions,
            "delta": r.delta, "nodes_checked": r.nodes_checked,
            "nodes_changed": len(r.delta),
            "context": list(r.context), "backup_path": r.backup_path,
            "warnings": list(r.warnings),
            **{k: bind[k] for k in ("edges_minted", "references_seen_but_not_minted")},
            "binding": bind,
            "gate": ("the file gained exactly this node and its properties, and NOTHING "
                     "moved on any existing node — both sides read by "
                     "factlattice.parser.parse_text"),
            "report": _render_node_receipt(r) + "\n" + "\n".join(_render_binding(bind)),
            "receipt": "full",
        }
        if bind["references_seen_but_not_minted"]:
            out["warnings"] = list(out["warnings"]) + [bind["binding_summary"]]
        if r.created:      # [A-E1] present ONLY on a create (absent otherwise: SDLC 7.6 response-key rule)
            out["created"] = True
            out["created_parent"] = r.created_parent
        led, sk = r.ledger, skipped
        if create or create_parent:
            # [delta4 F4] no exception MESSAGE in a create reply: the ledger-failure text and the roots-check
            # text carry "Type: message"; keep the type only
            if isinstance(led, dict) and led.get("status") == "failed":
                # `_ledger_append` did not return a record: whether one exists is UNKNOWN (zero or one).
                # It must never read as "failed" (a record may exist), nor as "missing".
                led = {"status": "unknown"}
            sk = _type_only(skipped) if skipped else skipped
        _apply_ledger(out, led, sk)
        out = _annotate(out, r.path, store)
        res = _compact_node_receipt(out) if mode == "compact" else out
        if create or create_parent:
            # ALWAYS present on a create reply, exactly one value, its OWN top-level field (never folded into
            # `success`): recorded (the append returned a record) | unknown (it raised) | skipped (an ephemeral
            # root: by design no record is written)
            st = led.get("status") if isinstance(led, dict) else None
            res["ledger_state"] = ("recorded" if st in ("ok", "ok_without_undo")
                                   else "skipped" if st == "skipped" else "unknown")
            if res["ledger_state"] == "unknown":
                res["warnings"] = [LEDGER_UNKNOWN_WARNING] + list(res.get("warnings") or [])
            _note_leftover_temp(res, _state)
        return res
    except Exception:
        # the file IS written when this is a create: the adapter's choke point (`add_node`) decides the
        # outcome from the disk, so this handler must not (and never reports a refusal)
        raise


def repair_property(path: str, node_id: str, line: int, key: str,
                    value: Optional[str] = None, *, store: object = None,
                    receipt: Optional[str] = "compact", proposer: Optional[str] = None,
                    approver: Optional[str] = None, reason: Optional[str] = None,
                    via: Optional[str] = None) -> dict:
    """Rewrite ONE malformed line in place so it parses — 06 §7, the found→fixed loop.

    `receipt`: `"compact"` (default) or `"full"` — see [AUTH-RECEIPT-COMPACT] above."""
    mode = _normalize_receipt_mode(receipt)
    try:
        _lk, skipped = _split_skipped(_ledger_kw(path, store, proposer, approver, reason, via))
        r = _repair_property(path, node_id, line, key, value, **_lk)
    except EditRefused as exc:
        return _refusal(exc, path=path, node_id=node_id, line=line,
                        key_as_authored=key)
    except (OSError, UnicodeDecodeError) as exc:
        return _refusal(EditRefused(f"{type(exc).__name__}: {exc}"), path=path,
                        node_id=node_id, line=line, key_as_authored=key)
    bind = _binding.analyse(r.key, r.parsed_value, node_id=r.node_id)
    out = {
        "success": True, "written": True, "refused": False,
        "path": r.path, "node_id": r.node_id, "key": r.key,
        "key_as_authored": r.key_as_authored, "coercions": list(r.coercions),
        "line_number": r.line_number,
        "line_before": r.line_before, "line_after": r.line_after,
        "key_region_before": r.key_region, "value_source": r.value_source,
        "value": r.value, "value_as_parsed": r.parsed_value,
        "properties_before": list(r.properties_before),
        "properties_after": list(r.properties_after),
        "delta": r.delta, "nodes_checked": r.nodes_checked,
        "nodes_changed": len(r.delta),
        "context": list(r.context), "backup_path": r.backup_path,
        "warnings": list(r.warnings),
        **{k: bind[k] for k in ("edges_minted", "references_seen_but_not_minted",
                                "nearest_minting_alternatives", "key_mints")},
        "binding": bind,
        "gate": ("the parsed property set differs from the pre-edit set by EXACTLY this "
                 "change, per node, across the whole file — the SAME gate as "
                 "lts_add_property, both sides read by factlattice.parser.parse_text. "
                 "⛔ The document was NOT re-rendered: exactly one line changed."),
        "report": _render_repair_receipt(r) + "\n" + "\n".join(_render_binding(bind)),
        "receipt": "full",
    }
    if bind["references_seen_but_not_minted"]:
        out["warnings"] = list(out["warnings"]) + [bind["binding_summary"]]
    _apply_ledger(out, r.ledger, skipped)
    out = _annotate(out, r.path, store)
    if mode == "compact":
        return _compact_property_receipt(out, is_repair=True)
    return out


# --------------------------------------------------------------------------------
# [RENAME-REMOVE-PROPERTY] `lts_rename_property` / `lts_remove_property`. The core writers
# (`edit.rename_property` / `edit.remove_property`) own the gate, the lock, the backup and the ledger;
# this is the thin adapter, same shape as `repair_property` above: a refusal is a RESULT with a fixed
# code in `evidence.refusal` and a sentence that never carries the file path, the receipt shows the
# line(s) before and after, and `compact` (the default) keeps everything that PROVES the write.
# --------------------------------------------------------------------------------

_EDGE_BUILTIN = "built-in tables only (no profile applied: {})"


def _edge_context(path, store) -> tuple[list, object, str, bool, list]:
    """(parser_configs, profile, edge_check, degraded, incomplete): the vocabulary the EDGE RULE parses with.

    The file's corpora are resolved through the SAME membership path the receipt's `root_membership` uses, and each
    corpus's registered profile is loaded through `config.registered_profile` / `config.parser_config_for`, the
    seam the serving engine builds its graph with, so a profile-registered `property_edges` key (`documents` ->
    DOCUMENTS) mints in BOTH parses. NEVER RETURNS EARLY (Diamond F-A): every corpus's config that CAN be loaded is
    collected and every one that cannot is recorded in `incomplete` as {corpus, code}; the codes are
    `profile_file_missing`, `profile_load_failed` and `corpus_not_in_registry` (seated, absent from the registry
    file: F-C). A non-empty `incomplete` makes the core refuse a change that could be an edge key
    (`edge-context-incomplete`). A corpus whose registry entry has NO `profile` is the documented
    `corpus_registers_no_profile`: built-in tables, a note, no refusal. `degraded` is True whenever `edge_check`
    reports less than the full vocabulary because something could not be determined."""
    from . import config as _cfg

    def builtin(code):
        return [None], None, _EDGE_BUILTIN.format(code), True, []
    try:
        corpora = _membership.check(str(Path(path).expanduser().resolve()), store=store)["corpora"]
        specs = _cfg.default_corpora()
    except Exception:  # noqa: BLE001 -- includes RegistryError; named, never swallowed silently
        # no registry: we cannot know whether the file belongs to a corpus WITH a profile, so this is INCOMPLETE
        # (round 3b): a key that could be an edge key is refused. (`outside_every_corpus` is different: a registry that
        # resolves and lists no corpus for this file means no profile can apply, so the built-in check is complete.)
        return ([None], None, _EDGE_BUILTIN.format("registry_unavailable"), True,
                [{"corpus": None, "code": "registry_unavailable"}])
    if not corpora:
        return builtin("outside_every_corpus")
    configs: list = []
    names: list = []
    incomplete: list = []
    profile = None
    for cid in corpora:
        spec = specs.get(cid)
        if spec is None:
            incomplete.append({"corpus": cid, "code": "corpus_not_in_registry"})
            continue
        ppath = spec.get("profile")
        if not ppath:
            continue
        if not os.path.isfile(ppath):
            incomplete.append({"corpus": cid, "code": "profile_file_missing"})
            continue
        try:
            prof = _cfg.registered_profile(spec)
            cfg = _cfg.parser_config_for(spec) if prof is not None else None
        except Exception:  # noqa: BLE001
            prof = cfg = None
        if prof is None or cfg is None:
            incomplete.append({"corpus": cid, "code": "profile_load_failed"})
            continue
        configs.append(cfg)
        names.append(f"{os.path.basename(ppath)} (corpus {cid})")
        profile = profile or prof
    codes = ",".join(sorted({i["code"] for i in incomplete}))
    if not configs:
        if incomplete:
            return [None], None, _EDGE_BUILTIN.format(codes), True, incomplete
        return [None], None, _EDGE_BUILTIN.format("corpus_registers_no_profile"), False, []
    check = "profile applied: " + ", ".join(names) + " (its property_edges plus the built-in tables)"
    if incomplete:
        check += "; NOT applied: " + ", ".join(f"{i['corpus']} ({i['code']})" for i in incomplete)
    return configs, profile, check, bool(incomplete), incomplete


#: The codes whose core text can name a directory on this machine (the engine STATE dir, or the corpus file in a
#: preflight/CAS message). For these the reply's `explanation` is a FIXED sentence and the core text moves to `live_detail`
#: beside its NEVER-persist note (Diamond F5/F7; round 4).
_PATH_BEARING_EXPLANATIONS = {
    "lock_unavailable": "the per-file write lock could not be taken; nothing was written",
    "lock_busy": "another writer holds this file's lock; retry. Nothing written",
    "write_refused": "the write was refused before it landed (see live_detail); nothing was written",
}
_PATH_BEARING_CODES = tuple(_PATH_BEARING_EXPLANATIONS)

#: Every `edge_check` outcome code, and the subset that makes the edge context INCOMPLETE (a corpus containing the file whose
#: profile cannot be determined, or no registry at all). Pinned against this module's literals, SPEC 4.6.6 and both tool
#: descriptions by the rename/remove tests, the same way `edit.WRITER_REFUSAL_CODES` is.
EDGE_INCOMPLETE_CODES = ("profile_file_missing", "profile_load_failed", "corpus_not_in_registry", "registry_unavailable")
EDGE_CHECK_CODES = EDGE_INCOMPLETE_CODES + ("outside_every_corpus", "corpus_registers_no_profile")


def _fixed_code_refusal(exc: EditRefused, path, **ctx) -> dict:
    """A refusal of the two NEW writers, which have no legacy pin and so follow the RULE ([TOOL-REASON-IS-A-FIXED-CODE]):
    top-level `reason` is the fixed code (== `evidence.refusal`), the path-free human sentence is `explanation`, and any
    text that may carry a path is `live_detail` with its NEVER-persist note."""
    code = (exc.evidence or {}).get("refusal") or "write_refused"
    out = _refusal(exc, path=path, **ctx)
    sentence = out["reason"]
    if code in _PATH_BEARING_EXPLANATIONS:
        out["live_detail"] = sentence
        out["live_detail_note"] = ("for the caller only, because it may name a directory on this machine; "
                                   "never persisted (the query log keeps only the code)")
        sentence = _PATH_BEARING_EXPLANATIONS[code]
    out["reason"] = code
    out["explanation"] = sentence
    out["report"] = (f"REFUSED — nothing written.\n  path   {out['path']}\n  reason {code}\n  {sentence}"
                     + "".join(f"\n  evidence.{k} = {v!r}" for k, v in out["evidence"].items()))
    return out


def _writer_refusal(exc: BaseException, path, **ctx) -> dict:
    """A refusal for the two new writers. An OS or decode failure becomes a FIXED sentence and code: the
    exception text of an OSError names the path, and a path must not reach a reply that is also logged."""
    if isinstance(exc, EditRefused):
        return _fixed_code_refusal(exc, path, **ctx)
    if isinstance(exc, UnicodeDecodeError):
        return _fixed_code_refusal(EditRefused("the file is not valid UTF-8. Nothing written.",
                                               evidence={"refusal": "not_utf8"}), path, **ctx)
    en = errno.errorcode.get(getattr(exc, "errno", None) or 0)
    return _fixed_code_refusal(EditRefused(f"the file could not be read or written ({en or _safe_exc_name(exc)}). "
                                           f"Nothing written.", evidence={"refusal": "os_error"}), path, **ctx)


def _bad_path(path) -> Optional[dict]:
    if not isinstance(path, str) or not path.strip() or "\x00" in path or not _utf8_ok(path):
        return _fixed_code_refusal(EditRefused("`path` must be a non-empty string without NUL characters. Nothing written.",
                                               evidence={"refusal": "bad_argument"}), None)
    return None


def _render_gate_block(r) -> list[str]:
    lines = ["GATE — the whole-file parsed delta, per node:",
             f"       nodes checked: {r.nodes_checked}   nodes changed: {len(r.delta)}"]
    for nid, d in r.delta.items():
        lines.append(f"       [{nid}] added {d['added']}  removed {d['removed']}")
        for ck, cv in d.get("changed", {}).items():
            lines.append(f"       [{nid}] {ck}: value removed {cv['removed']}  added {cv['added']}"
                         + ("  (reordered)" if cv.get("reordered") else ""))
    lines.append(f"       [{r.node_id}] properties: {len(r.properties_before)} -> {len(r.properties_after)}")
    lines += ["", f"EDGES — minted edges that moved (edge_check: {r.edge_check}):"]
    if not (r.edges_removed or r.edges_added):
        lines.append("       none")
    for e in r.edges_removed:
        lines.append(f"       - {e['source']} --{e['type']}--> {e['target']}")
    for e in r.edges_added:
        lines.append(f"       + {e['source']} --{e['type']}--> {e['target']}")
    return lines


def _render_rename_receipt(r) -> str:
    lines = [f"RENAMED  {r.path}:{r.line_number}", f"       -  {r.line_before}", f"       +  {r.line_after}", "",
             f"node   [{r.node_id}]", f"key    {r.key!r} -> {r.new_key!r}"
             + (f"   (authored {r.new_key_as_authored!r})" if r.new_key_as_authored else "")]
    lines += [f"         · {c}" for c in r.coercions]
    lines += [f"value  untouched; as parsed under the new key: {r.parsed_value!r}", ""]
    lines += _render_gate_block(r) + ["", "IN SITU:"] + [f"  {c}" for c in r.context]
    if r.warnings:
        lines += ["", "WARNINGS:"] + [f"  ⚠ {w}" for w in r.warnings]
    return "\n".join(lines + ["", f"backup {r.backup_path}"])


def _render_remove_receipt(r) -> str:
    lines = [f"REMOVED  {r.path}:{r.line_number}"] + [f"       -  {ln}" for ln in r.removed_lines] + [
        "", f"node   [{r.node_id}]", f"key    {r.key!r}   value removed (as parsed): {r.value_removed!r}",
        f"       `{r.key}` now holds: {r.remaining_value!r}", ""]
    lines += _render_gate_block(r) + ["", "IN SITU (the junction where the line(s) were):"] + [f"  {c}" for c in r.context]
    if r.warnings:
        lines += ["", "WARNINGS:"] + [f"  ⚠ {w}" for w in r.warnings]
    return "\n".join(lines + ["", f"backup {r.backup_path}"])


def _compact_writer_receipt(out: dict, *, rename: bool) -> dict:
    compact = {
        "success": out["success"], "written": out["written"], "refused": out["refused"],
        "path": out["path"], "node_id": out["node_id"], "key": out["key"],
        "line_number": out["line_number"],
        "delta": out["delta"], "nodes_checked": out["nodes_checked"], "nodes_changed": len(out["delta"]),
        "edges_removed": list(out["edges_removed"]), "edges_added": list(out["edges_added"]),
        "edge_check": out["edge_check"],
        "warnings": list(out.get("warnings") or []), "backup_path": out.get("backup_path"),
        "root_membership": _compact_root_membership(out.get("root_membership")),
        "ledger": out.get("ledger"), "receipt": "compact",
    }
    if rename:
        compact.update(new_key=out["new_key"], new_key_as_authored=out["new_key_as_authored"],
                       coercions=list(out["coercions"]), line_before=out["line_before"],
                       line_after=out["line_after"], value_as_parsed=_truncate(out["value_as_parsed"]))
    else:
        compact.update(removed_lines=list(out["removed_lines"]), value_removed=_truncate(out["value_removed"]),
                       remaining_value=_truncate(out["remaining_value"]))
    return compact


def rename_property(path: str, node_id: str, key: str, new_key: str, *, line: Optional[int] = None,
                    allow_edge_change: bool = False, store: object = None,
                    receipt: Optional[str] = "compact", proposer: Optional[str] = None,
                    approver: Optional[str] = None, reason: Optional[str] = None,
                    via: Optional[str] = None) -> dict:
    """Rename the KEY of ONE property line on ONE node; the value is byte-for-byte untouched.

    `receipt`: `"compact"` (default) or `"full"` -- see [AUTH-RECEIPT-COMPACT] above. A refusal ignores it."""
    bad = _bad_path(path)
    if bad:
        return bad
    mode = _normalize_receipt_mode(receipt)
    try:
        _lk, skipped = _split_skipped(_ledger_kw(path, store, proposer, approver, reason, via))
        cfgs, profile, edge_check, degraded, incomplete = _edge_context(path, store)
        r = _rename_property(path, node_id, key, new_key, line=line, allow_edge_change=allow_edge_change,
                             edge_configs=cfgs, edge_check=edge_check, edge_incomplete=incomplete, **_lk)
    except (EditRefused, OSError, UnicodeDecodeError) as exc:
        return _writer_refusal(exc, path, node_id=node_id, line=line, key_as_authored=key)
    bind = _binding.analyse(r.new_key, r.parsed_value, node_id=r.node_id, profile=profile)
    out = {
        "success": True, "written": True, "refused": False,
        "path": r.path, "node_id": r.node_id, "key": r.key, "new_key": r.new_key,
        "new_key_as_authored": r.new_key_as_authored, "coercions": list(r.coercions),
        "line_number": r.line_number, "line_before": r.line_before, "line_after": r.line_after,
        "value_as_parsed": r.parsed_value,
        "properties_before": list(r.properties_before), "properties_after": list(r.properties_after),
        "delta": r.delta, "nodes_checked": r.nodes_checked, "nodes_changed": len(r.delta),
        "edges_removed": list(r.edges_removed), "edges_added": list(r.edges_added),
        "allow_edge_change": bool(allow_edge_change), "edge_check": r.edge_check,
        "context": list(r.context), "backup_path": r.backup_path,
        "warnings": list(r.warnings) + ([f"edge_check: {r.edge_check}"] if degraded else []),
        "key_mints": bind["key_mints"], "edges_minted": bind["edges_minted"], "binding": bind,
        "gate": ("the parsed property set differs from the pre-edit set by EXACTLY {that node: `key` loses "
                 "that one value, `new_key` gains the identical value}, per node, across the whole file -- "
                 "the SAME gate as lts_add_property, both sides read by factlattice.parser.parse_text. "
                 "⛔ The document was NOT re-rendered: exactly one line changed, and only its key region."),
        "report": _render_rename_receipt(r) + "\n" + "\n".join(_render_binding(bind)),
        "receipt": "full",
    }
    _apply_ledger(out, r.ledger, skipped)
    out = _annotate(out, r.path, store)
    return _compact_writer_receipt(out, rename=True) if mode == "compact" else out


def remove_property(path: str, node_id: str, key: str, *, line: Optional[int] = None,
                    expect_value: Optional[str] = None, allow_edge_change: bool = False,
                    store: object = None, receipt: Optional[str] = "compact",
                    proposer: Optional[str] = None, approver: Optional[str] = None,
                    reason: Optional[str] = None, via: Optional[str] = None) -> dict:
    """Remove ONE property line (with exactly the wrapped tail the parser folds into its value) from ONE node.

    `receipt`: `"compact"` (default) or `"full"` -- see [AUTH-RECEIPT-COMPACT] above. A refusal ignores it."""
    bad = _bad_path(path)
    if bad:
        return bad
    mode = _normalize_receipt_mode(receipt)
    try:
        _lk, skipped = _split_skipped(_ledger_kw(path, store, proposer, approver, reason, via))
        cfgs, _profile, edge_check, degraded, incomplete = _edge_context(path, store)
        r = _remove_property(path, node_id, key, line=line, expect_value=expect_value,
                             allow_edge_change=allow_edge_change, edge_configs=cfgs, edge_check=edge_check, edge_incomplete=incomplete, **_lk)
    except (EditRefused, OSError, UnicodeDecodeError) as exc:
        return _writer_refusal(exc, path, node_id=node_id, line=line, key_as_authored=key)
    out = {
        "success": True, "written": True, "refused": False,
        "path": r.path, "node_id": r.node_id, "key": r.key, "line_number": r.line_number,
        "removed_lines": list(r.removed_lines), "value_removed": r.value_removed,
        "remaining_value": r.remaining_value,
        "properties_before": list(r.properties_before), "properties_after": list(r.properties_after),
        "delta": r.delta, "nodes_checked": r.nodes_checked, "nodes_changed": len(r.delta),
        "edges_removed": list(r.edges_removed), "edges_added": list(r.edges_added),
        "allow_edge_change": bool(allow_edge_change), "edge_check": r.edge_check,
        "context": list(r.context), "backup_path": r.backup_path,
        "warnings": list(r.warnings) + ([f"edge_check: {r.edge_check}"] if degraded else []),
        "gate": ("the parsed property set differs from the pre-edit set by EXACTLY that one value gone "
                 "from that node, per node, across the whole file -- the SAME gate as lts_add_property, "
                 "both sides read by factlattice.parser.parse_text; AND a byte-level gate required the new file to equal "
                 "the old file minus EXACTLY the lines the parser attributes to that value (the head and its folded "
                 "wrapped tail; a sub-bullet, table row or fence under it stays). ⛔ The document was NOT re-rendered."),
        "report": _render_remove_receipt(r),
        "receipt": "full",
    }
    _apply_ledger(out, r.ledger, skipped)
    out = _annotate(out, r.path, store)
    return _compact_writer_receipt(out, rename=False) if mode == "compact" else out


def property_loss(roots, *, by_month: bool = False, limit_examples: int = 12) -> dict:
    """[AUTH-PATH-FALSIFIER] — the number that can kill proposal 03. See
    `factlattice_engine.property_loss` for why it is not just `unaccounted_lines()`."""
    from .property_loss import scan_corpus
    if isinstance(roots, (str, Path)):
        roots = [roots]
    return scan_corpus(roots, by_month=by_month, limit_examples=limit_examples)
