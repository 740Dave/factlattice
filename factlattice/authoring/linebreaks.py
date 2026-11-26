"""Which characters a one-line `- key: value` cannot carry: the ones the PARSER ends a line at.

The typed writers emit every text input as part of one physical line (a property line, a heading, a
frontmatter field). The parser reads its lines through `factlattice.parser.split_source_lines`; any character
that function splits at ends the line early, so the stored value is cut there and the tail becomes a stray
prose line, while the whole-file key gate sees a self-consistent result. The writers therefore REFUSE such a
character in every text input (maintainers' ruling of 2026-10-28).

THE SET IS DERIVED, NEVER LISTED. `is_line_break(ch)` asks the parser's own splitting function whether a line
holding `ch` is one line or two, looking the function up at CALL TIME. Change the parser's splitting and the
refusal changes with it; `tests/test_writer_line_break_refusal.py` measures the parser independently over every
code point and fails if the two ever differ.

CRLF: an input holding CR LF holds two line-break characters. It is refused, and the first one (CR) is named.
CRLF line endings of an existing FILE are not input and are not touched by any writer.
"""
from __future__ import annotations

import functools
from collections.abc import Iterator

from .. import parser as _parser

#: labels for the message only. Membership is NEVER decided from this table.
_LABELS = {
    0x0A: "LINE FEED", 0x0B: "LINE TABULATION", 0x0C: "FORM FEED", 0x0D: "CARRIAGE RETURN",
    0x1C: "FILE SEPARATOR", 0x1D: "GROUP SEPARATOR", 0x1E: "RECORD SEPARATOR", 0x85: "NEXT LINE",
    0x2028: "LINE SEPARATOR", 0x2029: "PARAGRAPH SEPARATOR",
}


@functools.cache
def _is_line_break_cached(ch: str, splitter) -> bool:
    return len(splitter("a" + ch + "b")) != 1


def is_line_break(ch: str) -> bool:
    """True when the parser's own splitting ends a line at the single character `ch`.
    The splitter is passed into the cache key, so replacing `parser.split_source_lines` changes the answer."""
    return _is_line_break_cached(ch, _parser.split_source_lines)


# the cache is keyed on the splitter, so a test that swaps it needs no explicit reset; this is for symmetry.
is_line_break.cache_clear = _is_line_break_cached.cache_clear      # type: ignore[attr-defined]


def label(ch: str) -> str:
    return _LABELS.get(ord(ch), "line-break character")


def first_line_break(text: str) -> tuple[int, str] | None:
    """(0-based index in code points, the character) of the first character of `text` at which the parser would
    end a line, or None. One whole-string call to the parser's splitter settles the common (clean) case."""
    if not isinstance(text, str):
        return None
    if len(_parser.split_source_lines("a" + text + "b")) == 1:
        return None
    for i, ch in enumerate(text):
        if is_line_break(ch):
            return i, ch
    # No single character explains the split (the splitter breaks on a SEQUENCE, or `is_line_break` and the
    # splitter disagree): locate it anyway. The number of parts of "a" + prefix + "b" never decreases as the prefix
    # grows, so the first prefix that splits is found by bisection, and its last character is the one named.
    lo, hi = 0, len(text) - 1                       # the whole text splits, so some prefix ending at <= hi does
    while lo < hi:
        mid = (lo + hi) // 2
        if len(_parser.split_source_lines("a" + text[:mid + 1] + "b")) != 1:
            hi = mid
        else:
            lo = mid + 1
    return lo, text[lo]


def iter_texts(obj, name: str) -> Iterator[tuple[str, str]]:
    """Every string reachable in `obj` (a str, list, dict or pydantic model), with a label for the message."""
    if isinstance(obj, str):
        yield name, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from iter_texts(k, f"{name} key")
            yield from iter_texts(v, f"{name}[{k if isinstance(k, str) and not first_line_break(k) else '...'}]")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from iter_texts(v, f"{name}[{i}]")
    elif hasattr(obj, "model_dump"):
        yield from iter_texts(obj.model_dump(), name)


__all__ = ["first_line_break", "is_line_break", "iter_texts", "label"]
