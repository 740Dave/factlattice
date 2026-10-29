"""[SEM-CARDS] What text represents a node to the semantic assist.

One HEAD card per node: the id with hyphens spaced, the title, the node's descriptive STRING
property values (each string element of a LIST-valued property too: a repeated key parses as a
list [SEM-CARDS-LIST-VALUES]; nested containers are dropped), and its property KEYS as bare words (so "which field holds the version"
can meet a `field-1` key). Lifecycle, date, hash and pure reference-list values are left out:
they add noise and pull every node toward its neighbours. Up to MAX_CHUNKS BODY chunks of prose
follow, with the heading and property lines removed (they are already in the head).

Pure functions of (node dict, the node's own source lines). No model, no I/O. Bump
CARD_SCHEMA_VERSION on ANY change here: it is part of every index fingerprint, so an index
built from older cards is refused rather than mixed (honesty rule 7).
"""
from __future__ import annotations

import hashlib
import re
from typing import List, Tuple

# The PARSER's own property-line grammar, imported, never re-derived: a line is a property
# (and so already in the head card, or deliberately excluded) only if the parser mints it.
from factlattice.parser import _PROP_BOLD_RE, _PROP_KEY_ONLY_RE, _PROP_SIMPLE_RE

CARD_SCHEMA_VERSION = 2   # 2: list-valued properties reach the head card [SEM-CARDS-LIST-VALUES]
CHUNK_WORDS, CHUNK_OVERLAP, MAX_CHUNKS = 160, 25, 6

_EXCLUDE_KEY = re.compile(r"(status|lifecycle|date|session|when|hash|sha|^id$|-at$|^at$)")
_REF_ONLY = re.compile(r"^\s*(\[[^\]]+\][\s,;]*)+$")
_HEAD_LINE = re.compile(r"^\s*#{1,6}\s")


def head_card(node_id: str, node: dict) -> str:
    parts = [node_id.replace("-", " ").replace("_", " "), node.get("title") or ""]
    keys: List[str] = []
    for k, v in (node.get("properties") or {}).items():
        keys.append(k.replace("-", " ").replace("_", " "))
        if _EXCLUDE_KEY.search(k):
            continue
        # [SEM-CARDS-LIST-VALUES] a repeated key parses as a LIST: each string element is a value
        for x in (v if isinstance(v, list) else [v]):
            if isinstance(x, str) and not _REF_ONLY.match(x):
                parts.append(x)
    parts.append(" ".join(dict.fromkeys(keys)))
    return "\n".join(p for p in parts if p)


def _is_property_line(line: str) -> bool:
    return bool(_PROP_SIMPLE_RE.match(line) or _PROP_BOLD_RE.match(line)
                or _PROP_KEY_ONLY_RE.match(line))


def body_chunks(lines: List[str]) -> List[str]:
    prose = [ln for ln in lines if ln.strip() and not _is_property_line(ln)
             and not _HEAD_LINE.match(ln) and not ln.strip().startswith("---")]
    words = " ".join(prose).split()
    out: List[str] = []
    step = CHUNK_WORDS - CHUNK_OVERLAP
    for i in range(0, len(words), step):
        out.append(" ".join(words[i:i + CHUNK_WORDS]))
        if len(out) == MAX_CHUNKS or i + CHUNK_WORDS >= len(words):
            break
    return out


def node_cards(node_id: str, node: dict, body_lines: List[str]) -> List[Tuple[str, str]]:
    """[("head", text), ("chunk", text), ...] for one node."""
    return [("head", head_card(node_id, node))] + [("chunk", c) for c in body_chunks(body_lines)]


def card_fingerprint(cards: List[Tuple[str, str]]) -> str:
    h = hashlib.sha256()
    for kind, text in cards:
        h.update(kind.encode()); h.update(b"\0"); h.update(text.encode("utf-8")); h.update(b"\1")
    return f"v{CARD_SCHEMA_VERSION}:{h.hexdigest()}"
