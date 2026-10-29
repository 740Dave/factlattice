#!/usr/bin/env python3
"""Gated replace of ONE value of a multi-valued key, through the ledgered write step.

    set_list_item_gated.py PATH NODE_ID KEY EXPECT_OLD NEW_VALUE [--index N] [--apply] [--proposer P] [--reason R]

An interim tool until the engineer's `set_list_item` verb exists (design note v1.1 §1, line 170, ratified). The typed
writers cannot do this: `set` refuses a key holding several values, `append` only adds, and `repair` needs a
malformed line. This helper rewrites exactly ONE `- KEY: value` line and writes through
`factlattice.authoring.edit.write_whole_file`: per-file lock, atomic replace, a ledger record with a durable
prior blob.

ADDRESSING: by EXPECT_OLD, the element's current parsed value. If several elements equal it, the call is
REFUSED as ambiguous unless `--index` (0-based position in the parsed list) names one of them. Index alone is
never accepted: list order shifts between proposal and apply.

THE GATE (all must hold, or nothing is written):
  1. exactly one line of the file differs: the chosen element's line;
  2. the (node, KEY) list after equals the list before with exactly that element replaced (same length);
  3. every other key on the node, and every other node's properties, are identical; the node-id list too;
  4. the edge multiset is identical, or (a relationship key) differs by exactly one edge removed and one
     added, both with the node as source and of the SAME type; a self-edge is refused. A new target that is
     not a node in this file is REPORTED (`new_ref_targets_unverified`), not refused (cross-file refs are legal).
CONCURRENCY (residual, inherent to an advisory lock: an UNLOCKED writer landing between the locked re-check and the replace is not caught): the file is read once as bytes, gated, and written with `write_whole_file(..., expect_base=<those
bytes>)`: the bytes it gated are re-checked under the per-file lock (a concurrent edit => refusal). A CRLF file
is refused.
REFUSED outright: a single-valued key (use lts_add_property), `part-of` (placement: use move), a nested-block
value (out of scope here), a NEW_VALUE containing a newline, an EXPECT_OLD that is not an element, a line
that cannot be located unambiguously.

Dry run is the DEFAULT (prints one JSON object). `--apply` writes. Exit 0 = pass, 1 = refused (nothing
written), 2 = usage error.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from factlattice.parser import parse_text  # noqa: E402


class SetListItemRefused(Exception):
    pass


def _node(parsed, node_id):
    return next((n for n in parsed.nodes if n.id == node_id), None)


def _edge_key(e):
    g = (lambda k: getattr(e, k, None) if not isinstance(e, dict) else e.get(k))
    return (g("source"), g("target"), g("type"))


def _candidate(text: str, line_no: int, key: str, new_value: str) -> str:
    """Rewrite the value of the `- KEY: value` line at 1-based `line_no`. Nothing else."""
    lines = text.split("\n")
    m = re.match(r"^(\s*-\s*" + re.escape(key) + r"\s*:\s*)(.*?)(\s*)$", lines[line_no - 1])
    if not m:
        raise SetListItemRefused(f"line {line_no} is not a plain `- {key}: value` line.")
    lines[line_no - 1] = m.group(1) + new_value + m.group(3)
    return "\n".join(lines)


def set_list_item(path, node_id: str, key: str, expect_old: str, new_value: str, *, index: int | None = None,
                  apply: bool = False, proposer: str | None = None, reason: str | None = None) -> dict:
    p = Path(path).expanduser()
    if "\n" in new_value or "\r" in new_value or not new_value.strip():
        raise SetListItemRefused("the new value is empty or contains a newline.")
    if key == "part-of":
        raise SetListItemRefused("`part-of` is placement: use move (design note §1), not a list-item swap.")
    try:
        base = p.read_bytes()                      # read ONCE as bytes; the write re-checks these under the lock
        text = base.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise SetListItemRefused(f"cannot read {p} as UTF-8 ({type(e).__name__}); nothing written.") from e
    if "\r" in text:
        raise SetListItemRefused("CRLF file: the write step would normalise every line ending; nothing written.")
    before = parse_text(text, p.name)
    node = _node(before, node_id)
    if node is None:
        raise SetListItemRefused(f"no node {node_id!r} in {p}.")
    vals = node.properties.get(key)
    if not isinstance(vals, list):
        raise SetListItemRefused(f"`{key}` on {node_id} is a scalar (or absent), not a multi-valued key: "
                                 f"use lts_add_property for a single value.")
    if any(not isinstance(v, str) for v in vals):
        raise SetListItemRefused(f"`{key}` holds nested values; out of scope for this helper.")
    positions = [i for i, v in enumerate(vals) if v == expect_old]
    if not positions:
        raise SetListItemRefused(f"{expect_old!r} is not an element of {node_id}.{key}.")
    if index is None:
        if len(positions) > 1:
            raise SetListItemRefused(f"{expect_old!r} occurs {len(positions)} times in {node_id}.{key}: ambiguous; "
                                     f"pass --index (one of {positions}).")
        index = positions[0]
    elif index not in positions:
        raise SetListItemRefused(f"index {index} does not hold {expect_old!r} (it is at {positions}).")

    # locate the element's line: the (k)-th `- KEY: <expect_old>` line inside the node's own range, where k is
    # the element's rank among equal elements (lines and parsed elements follow file order).
    lo, hi = node.line_range
    lines = text.split("\n")
    pat = re.compile(r"^\s*-\s*" + re.escape(key) + r"\s*:\s*(.*?)\s*$")
    hits = [i + 1 for i in range(lo - 1, min(hi, len(lines))) if (m := pat.match(lines[i])) and m.group(1) == expect_old]
    rank = positions.index(index)
    if len(hits) != len(positions):
        raise SetListItemRefused(f"cannot locate the element's line unambiguously ({len(hits)} matching lines for "
                                 f"{len(positions)} equal elements); use repair_property on the line, or wait for the engineer's set_list_item verb.")
    line_no = hits[rank]
    old_line = lines[line_no - 1]
    cand = _candidate(text, line_no, key, new_value)

    # --- the gate --------------------------------------------------------------------------
    a, b = text.split("\n"), cand.split("\n")
    diff = [i for i in range(max(len(a), len(b))) if (a[i] if i < len(a) else None) != (b[i] if i < len(b) else None)]
    if diff != [line_no - 1]:
        raise SetListItemRefused(f"the candidate changes more than the one line (lines {[i + 1 for i in diff]}); "
                                 f"nothing written.")
    after = parse_text(cand, p.name)
    if [n.id for n in before.nodes] != [n.id for n in after.nodes]:
        raise SetListItemRefused("the node-id list changed; nothing written.")
    for nb, na in zip(before.nodes, after.nodes):
        if nb.id != node_id:
            if nb.properties != na.properties:
                raise SetListItemRefused(f"properties of {nb.id} changed; nothing written.")
            continue
        if {k: v for k, v in nb.properties.items() if k != key} != {k: v for k, v in na.properties.items() if k != key}:
            raise SetListItemRefused(f"another key on {node_id} changed; nothing written.")
        want = list(vals); want[index] = new_value
        if na.properties.get(key) != want:
            raise SetListItemRefused(f"{node_id}.{key} parses as {na.properties.get(key)!r}, not {want!r}; nothing written.")
    eb = collections.Counter(_edge_key(e) for e in before.edges or [])
    ea = collections.Counter(_edge_key(e) for e in after.edges or [])
    removed, added = list((eb - ea).elements()), list((ea - eb).elements())
    if removed or added:
        if not (len(removed) == 1 and len(added) == 1 and removed[0][0] == node_id and added[0][0] == node_id
                and removed[0][2] == added[0][2]):
            raise SetListItemRefused(f"the edge change is not exactly one swap from {node_id} of the SAME type "
                                     f"(removed {removed}, added {added}); nothing written.")
        if added[0][1] == node_id:
            raise SetListItemRefused(f"the new reference targets {node_id} itself (a self-edge); nothing written.")

    out = {"path": str(p), "node_id": node_id, "key": key, "index": index, "line": line_no,
           "before": old_line, "after": cand.split("\n")[line_no - 1], "gate": "pass", "applied": False,
           "edge_delta": {"removed": [[r[0], r[1]] for r in removed], "added": [[x[0], x[1]] for x in added]}}
    if added:     # a relationship key: a target absent from THIS file may be a legitimate cross-file ref; report it
        ids = {n.id for n in before.nodes}
        out["new_ref_targets_unverified"] = sorted({x[1] for x in added if x[1] not in ids})
    if apply:
        from factlattice.authoring.edit import EditRefused, write_whole_file
        try:
            written = write_whole_file(p, cand, expect_base=base, proposer=proposer,
                                       reason=reason or f"set_list_item {node_id}.{key}")
        except EditRefused as e:
            raise SetListItemRefused(str(e)) from e
        out["applied"] = True
        out["ledger"] = getattr(written, "ledger", None)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    for a in ("path", "node_id", "key", "expect_old", "new_value"):
        ap.add_argument(a)
    ap.add_argument("--index", type=int)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--proposer")
    ap.add_argument("--reason")
    args = ap.parse_args(argv)
    try:
        out = set_list_item(args.path, args.node_id, args.key, args.expect_old, args.new_value, index=args.index,
                            apply=args.apply, proposer=args.proposer, reason=args.reason)
    except SetListItemRefused as e:
        print(json.dumps({"gate": "refused", "reason": str(e), "applied": False}))
        return 1
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
