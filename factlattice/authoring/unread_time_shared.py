"""Placeholder clock times: the detector, the quoted-span rule and the refusal text, in one file.

A placeholder time is an HH:MM[:SS][Z or z] shaped token, standing alone or after an ISO date's `T` or `t`, that holds at least one
real digit AND at least one x/X/? in a digit position (`04:2xZ`, `21:3?Z`). It is what a writer types when it wrote a
time without reading a clock. A question mark right after a complete HH:MM ("approved at 03:17?") is punctuation, not a
placeholder.

This file carries the shared LOGIC and nothing that belongs to one program: every program that refuses such a token holds
a byte-identical copy and calls these functions, so no two of them can disagree about what a token is, where it is, or
what the refusal says. It imports only the standard library and does no work at import beyond compiling its regexes.

  placeholder_tokens(text)    the detector: [(start, end, token)] over one string.
  quoted_spans(line)          the word-aware quote spans of ONE line: one half of the exemption (see QUOTING).
  find_unread_times(text)     hits in order, each with token, 1-based line and 0-based column.
  refusal_text(hits)          the refusal sentence; `shell=True` is the wording for a shell command.

QUOTING. A token is quoted, and exempt, when it lies wholly inside a span on its own line. A span is the text between a
pair of backticks, a pair of double quotes or a pair of single quotes. Quotes pair per kind (each pairs only with its
own kind), left to right on one line; an unpaired quote character opens no span; a span never crosses a line break (a line ends at any character of LINE_BREAKS).
An apostrophe (') with a Unicode word character (`\\w`: letters, digits, underscore and some numeric characters such as a vulgar fraction) on BOTH
sides ("Ann's", "don't") is part of a word: in the word-aware pairing it neither opens nor closes a single-quote span.
Backtick and double-quote spans have no such exception. `quoted_spans(line)` returns the word-aware spans. A token is exempt
only when it lies inside a span of both pairings: the word-aware one and the plain one, in which every single quote counts.
So the word rule can only add refusals: a token the plain pairing refuses is never exempted by it.
Whether the exemption applies is the CALLER'S choice: `find_unread_times(text, exempt_quoted=True)` applies it (the
default), `exempt_quoted=False` applies none, for a shell command, where quote characters are syntax and not citation.
KNOWN LIMITS: single quotes are a quote form, so an apostrophe that does not sit between two word characters (a possessive
plural such as "the cats' ", or one at the edge of a word) can still pair with a later single quote on the same line and
exempt a token falsely. A combining mark is not a word character, so in decomposed text ("e" plus U+0301, then "'s") the
apostrophe counts as a quote. The plain pairing counts every single quote on the line, word apostrophes included, so a token whose
only quotes are single quotes is always refused when an odd number of word apostrophes lies before it on its line, inside the phrase or before
it ("it's '04:2xZ'", "'it's 04:2xZ'"). Otherwise whether single quotes exempt a token depends on every quote on its line;
backticks are the reliable form. The check is for the plausible mistake, not for an author who evades it on purpose.
"""
import re
from typing import NamedTuple

CANDIDATE = re.compile(
    r"(?:(?<![\w:.])|(?<=\d{4}-\d\d-\d\d[Tt]))"
    r"[0-2][0-9xX?]:[0-5xX?][0-9xX?](?::[0-5xX?][0-9xX?])?[Zz]?(?![\w:])"
)
_DIGIT = re.compile(r"\d")
_PLACE = re.compile(r"[xX?]")
_QUESTION = re.compile(r"\d\d:\d\d\?")
_QUOTES = ("`", '"', "'")
_WORD = re.compile(r"\w")
#: every character at which a line ends: LF, CR, VT, FF, U+001C-U+001E, NEL, U+2028, U+2029 (CR LF counts as one break).
#: A quoted span never crosses one, and line numbers count them.
LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
_LINE_SPLIT = re.compile("\r\n|[" + LINE_BREAKS + "]")


def placeholder_tokens(text):
    """-> [(start, end, token)] for every placeholder clock time in text."""
    out = []
    for m in CANDIDATE.finditer(text):
        tok = m.group(0)
        if _QUESTION.fullmatch(tok):
            continue
        if _DIGIT.search(tok) and _PLACE.search(tok.rstrip("Zz")):
            out.append((m.start(), m.end(), tok))
    return out


class Hit(NamedTuple):
    token: str
    line: int          # 1-based, within the checked text
    column: int        # 0-based, within that line
    field: str = ""    # which input held it, when the caller checks several (empty otherwise)


def _in_word(line, i):
    """True when the character at i has a word character (`\\w`) on both sides: an apostrophe inside a word."""
    return 0 < i < len(line) - 1 and _WORD.match(line[i - 1]) is not None and _WORD.match(line[i + 1]) is not None


def _spans(line, word_aware):
    out = []
    for q in _QUOTES:
        at = [i for i, c in enumerate(line) if c == q and not (word_aware and q == "'" and _in_word(line, i))]
        out.extend(zip(at[0::2], at[1::2]))
    return out


def quoted_spans(line):
    """-> [(open, close)] index pairs of the word-aware quote spans on ONE line: per quote kind, left to right; an odd one out
    opens none; an apostrophe with a word character on both sides is skipped. This is not the exemption on its own:
    find_unread_times also requires the token to lie inside a span of the plain pairing (every single quote counts)."""
    return _spans(line, True)


def find_unread_times(text, field="", *, exempt_quoted=True):
    """-> [Hit] for every placeholder time in text, in order of appearance; [] means nothing to refuse.
    With exempt_quoted a token wholly inside a span of its own line, under both pairings, is left out."""
    hits = []
    for n, line in enumerate(_LINE_SPLIT.split(text), start=1):
        spans = quoted_spans(line) if exempt_quoted else []
        plain = _spans(line, False) if exempt_quoted else []
        for start, end, token in placeholder_tokens(line):
            if any(a < start and end <= b for a, b in spans) and any(a < start and end <= b for a, b in plain):
                continue
            hits.append(Hit(token, n, start, field))
    return hits


def refusal_text(hits, *, shell=False):
    """The refusal sentence: every token with its place (once per occurrence), then the remedies. `shell=True` is the
    wording for a shell command, where quoting is no exemption: it gives no quote advice and names the Edit tool."""
    found = "; ".join(
        f"`{h.token}` ({'field `' + h.field + '`, ' if h.field else ''}line {h.line}, column {h.column})" for h in hits)
    text = (f"Unread clock time: {found}. A time with an x, X or ? in a digit place means the clock was not read. "
            f"Read the clock with `date -u` and type the real time, or write \"time unread\". ")
    if shell:
        return text + ("A command that carries such a token is refused however it is quoted. To repair an old slip, "
                       "use the Edit tool: it checks only the lines it adds.")
    return text + ("If the token is there on purpose (for example citing someone else's slip), wrap it in backticks; "
                   "double quotes also work; single quotes can fail, for example when an apostrophe inside a word (as in it's) comes before the token on its line. A quote pairs only with its own kind, on one line, so a pair "
                   "split over two lines does not count.")
