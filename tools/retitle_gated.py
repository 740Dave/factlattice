#!/usr/bin/env python3
"""Gated RETITLE of one node's heading text, through the ledgered write step.

    retitle_gated.py PATH NODE_ID "New title" [--apply] [--proposer P] [--reason R]

An interim tool until the engineer's `retitle_node` verb exists (design note v1.1 §1). The typed writers
reach only properties; a hand edit is unrecorded (it shows later only as a ledger chain break).
This helper changes exactly ONE heading's title text and writes through
`factlattice.authoring.edit.write_whole_file`: per-file lock, atomic replace, a ledger record
with a durable prior blob (so the change is attributable and undoable from the record).

CONCURRENCY (residual, inherent to an advisory lock: an UNLOCKED writer landing between the locked re-check and the replace is not caught): the file is read once as bytes, gated, and written with `write_whole_file(..., expect_base=<those
bytes>)`, so the bytes it gated are re-checked under the per-file lock (a concurrent edit => refusal, not a
lost update). A CRLF file is refused (the write step would normalise every line ending).

THE GATE (all must hold, or nothing is written):
  1. exactly one line of the file differs: the target node's heading line;
  2. the re-parsed file has the identical ordered node-id list, identical properties on EVERY
     node, identical titles on every OTHER node, and an identical edge list;
  3. the target's title parses to exactly the requested text.
The new title may not contain `[`, `]` or a newline (they would change the heading grammar), and
the old title text must occur exactly once in the heading line (so the replacement cannot touch
the id bracket).

Dry run is the DEFAULT: it prints one JSON object (before/after heading and the gate verdict).
`--apply` writes, then reports the ledger outcome. Exit 0 = pass (and written with --apply),
1 = refused (nothing written), 2 = usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from factlattice.parser import parse_text  # noqa: E402


class RetitleRefused(Exception):
    pass


def _parse(text: str, name: str):
    return parse_text(text, name)


def _node(parsed, node_id: str):
    for n in parsed.nodes:
        if n.id == node_id:
            return n
    return None


def _candidate(text: str, line_no: int, old_title: str, new_title: str) -> str:
    """Replace the title text inside the heading line (1-based `line_no`). Nothing else."""
    lines = text.split("\n")
    line = lines[line_no - 1]
    if line.count(old_title) != 1:
        raise RetitleRefused(f"the current title text is not unique in its heading line "
                             f"({line!r}); refusing, because the replacement could touch the id bracket.")
    lines[line_no - 1] = line.replace(old_title, new_title, 1)
    return "\n".join(lines)


def _edges(parsed):
    return sorted(json.dumps(e if isinstance(e, dict) else getattr(e, "__dict__", repr(e)),
                             sort_keys=True, default=str) for e in (parsed.edges or []))


def retitle(path, node_id: str, new_title: str, *, apply: bool = False,
            proposer: str | None = None, reason: str | None = None) -> dict:
    p = Path(path).expanduser()
    if not new_title or not new_title.strip():
        raise RetitleRefused("the new title is empty.")
    if any(c in new_title for c in "[]\n\r"):
        raise RetitleRefused("the new title may not contain '[', ']' or a newline (they change the heading grammar).")
    try:
        base = p.read_bytes()                      # read ONCE as bytes; the write re-checks these under the lock
        text = base.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise RetitleRefused(f"cannot read {p} as UTF-8 ({type(e).__name__}); nothing written.") from e
    if "\r" in text:
        raise RetitleRefused("CRLF file: the write step would normalise every line ending; nothing written.")
    before = _parse(text, p.name)
    node = _node(before, node_id)
    if node is None:
        raise RetitleRefused(f"no node {node_id!r} in {p}.")
    line_no = node.line_range[0]
    old_line = text.split("\n")[line_no - 1]
    cand = _candidate(text, line_no, node.title, new_title)

    # --- the gate --------------------------------------------------------------------------
    a, b = text.split("\n"), cand.split("\n")
    diff = [i for i in range(max(len(a), len(b))) if (a[i] if i < len(a) else None) != (b[i] if i < len(b) else None)]
    if diff != [line_no - 1]:
        raise RetitleRefused(f"the candidate changes more than the heading line (lines {[i + 1 for i in diff]}); nothing written.")
    after = _parse(cand, p.name)
    ids_b, ids_a = [n.id for n in before.nodes], [n.id for n in after.nodes]
    if ids_b != ids_a:
        raise RetitleRefused("the node-id list changed; nothing written.")
    for nb, na in zip(before.nodes, after.nodes):
        if nb.properties != na.properties:
            raise RetitleRefused(f"properties of {nb.id} changed; nothing written.")
        if nb.id != node_id and nb.title != na.title:
            raise RetitleRefused(f"the title of {nb.id} changed; nothing written.")
    if _edges(before) != _edges(after):
        raise RetitleRefused("the edge list changed; nothing written.")
    if _node(after, node_id).title != new_title:
        raise RetitleRefused(f"the new title parses as {_node(after, node_id).title!r}, not {new_title!r}; nothing written.")

    out = {"path": str(p), "node_id": node_id, "line": line_no, "before": old_line,
           "after": cand.split("\n")[line_no - 1], "gate": "pass", "applied": False}
    if apply:
        from factlattice.authoring.edit import EditRefused, write_whole_file
        try:
            written = write_whole_file(p, cand, expect_base=base, proposer=proposer,
                                       reason=reason or f"retitle {node_id}")
        except EditRefused as e:
            raise RetitleRefused(str(e)) from e
        out["applied"] = True
        out["ledger"] = getattr(written, "ledger", None)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("path")
    ap.add_argument("node_id")
    ap.add_argument("new_title")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--proposer")
    ap.add_argument("--reason")
    args = ap.parse_args(argv)
    try:
        out = retitle(args.path, args.node_id, args.new_title, apply=args.apply,
                      proposer=args.proposer, reason=args.reason)
    except RetitleRefused as e:
        print(json.dumps({"gate": "refused", "reason": str(e), "applied": False}))
        return 1
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
