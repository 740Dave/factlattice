"""ID HIJACK — a heading whose node id is a CITATION, not a declaration.

    ## 8. Independent replication of [FLCD-CONNECTIVITY], on a second corpus,
         by an author who did not know it existed [FL-RECALL-REPLICATION]

mints `FLCD-CONNECTIVITY`. The intended `[FL-RECALL-REPLICATION]` stays inside the title
STRING, where nothing can reference it and no query returns it, and the minted id then
COLLIDES with the real `FLCD-CONNECTIVITY` in another file — where first-root-wins drops
one of the two sections out of the graph entirely.

⛔ IT IS A SUBSTITUTION, NOT A LOSS, AND THAT IS WHY IT DESERVES ITS OWN RULE. Every other
class in the 01-06 series makes something DISAPPEAR; this one makes something LIE. The
node exists, it parses, it is queryable, and it is the wrong node. Nothing in the file
looks wrong to a reader, and the collision diagnostic — when there is one — names a
DUPLICATE ID, which sends the reader to rename a node rather than to notice that a
citation ate a declaration.

SEVERITY: `error`, and this is not the usual judgement call.
--------------------------------------------------------------
`key_loss` ships at `warning` because legitimate prose bullets are common and an
error-severity introduction red-gates every corpus on day one. This is the opposite
situation: the shape has NO legitimate reading. A heading that ends with an id-shaped
bracket has declared its id there, and an earlier id-shaped bracket in the same heading
is a citation. Measured over every `.lts` in this repository and the Acme corpora at
introduction (see the test): the finding count is small enough to fix in one pass, which
is the condition under which an error is honest.

Demote with `structure_severity={"id_hijack": "warning"}` if a corpus legitimately writes
two ids in a heading and means the first.

⛔ NO SECOND BRACKET GRAMMAR. The predicate is `factlattice.parser.detect_id_hijack`, next
to `_heading_id` which it is about, using the parser's own `_BRACKET_RE`/`_UPPER_ID_RE`.
A linter that carried its own idea of "id-shaped" would drift from the grammar it lints.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import factlattice
from factlattice.parser import _HEADING_RE, detect_id_hijack

ORIGIN = "id_hijack"
KIND_ID_HIJACK = "id_hijack"         # a citation ate the declaration — a SUBSTITUTION
KIND_MULTI_ID = "multi_id_heading"   # two declarations, one heading, one minted node

ID_HIJACK_SEVERITY: Dict[str, str] = {
    KIND_ID_HIJACK: "error",
    # `warning`, not error: the author declared BOTH ids on purpose and the grammar can
    # only mint one. The remedy is an authoring decision (split the heading, or move the
    # second id into a `- see-also:`), and a rule whose fix requires a choice does not
    # belong at error severity — same reasoning `key_loss` gives for its MEDIUM band.
    KIND_MULTI_ID: "warning",
}

_KIND_FOR_SHAPE = {"citation_hijack": KIND_ID_HIJACK, "multi_id": KIND_MULTI_ID}


@dataclass
class HijackFinding:
    kind: str
    message: str
    source_file: Optional[str]
    source_line: Optional[int]
    node_id: Optional[str] = None
    detail: dict = field(default_factory=dict)


def scan_lines(lines: List[str], source_file: str) -> List[HijackFinding]:
    """Fence-aware heading sweep over one file's lines."""
    out: List[HijackFinding] = []
    in_fence = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        hm = _HEADING_RE.match(line)
        if not hm:
            continue
        hit = detect_id_hijack(hm.group(2))
        if hit is None:
            continue
        kind = _KIND_FOR_SHAPE[hit["shape"]]
        if kind == KIND_ID_HIJACK:
            message = (
                f"this heading minted [{hit['minted']}] — a CITATION — while the id it "
                f"declares, [{hit['intended']}], stayed inside the title text where "
                f"nothing can reference it and no query returns it. "
                f"If [{hit['minted']}] is also defined elsewhere the two COLLIDE and one "
                f"section leaves the graph. This is a SUBSTITUTION, not a loss: the node "
                f"parses and is queryable and is the wrong node. "
                f"Fix: move the citation into the node body (`- see-also: "
                f"[{hit['minted']}]`) so the heading carries exactly one bracket.")
        else:
            others = ", ".join(f"[{c}]" for c in hit["candidates"][1:])
            message = (
                f"this heading declares {len(hit['candidates'])} ids and the grammar "
                f"mints exactly ONE: [{hit['minted']}]. {others} exist only inside the "
                f"title string — no node, no edge, no `lts_find` hit — and any reference "
                f"to them dangles. Fix: give each id its own heading, or keep one and "
                f"relate the rest from the body.")
        out.append(HijackFinding(
            kind=kind,
            message=message,
            source_file=source_file, source_line=i + 1,
            node_id=hit["minted"],
            detail={"shape": hit["shape"], "minted": hit["minted"],
                    "intended": hit["intended"],
                    "intended_doctype": hit["intended_doctype"],
                    "candidates": hit["candidates"], "heading": hm.group(2)},
        ))
    return out


def find_id_hijacks(corpus: "factlattice.ParsedCorpus") -> List[HijackFinding]:
    """Every hijacked heading in `corpus`, deterministic in file order.

    ⛔ Re-scans from `LineCoverage.lines`, not from the file. In `parse()`'s file-set mode
    the recorded `source_file` is a basename inside a temp dir that is deleted before the
    caller sees the result — the same reason `key_loss.find_key_losses` does.
    """
    out: List[HijackFinding] = []
    for cov in corpus.coverage:
        out += scan_lines(list(cov.lines), cov.source_file)
    return out
