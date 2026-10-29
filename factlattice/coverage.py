"""LINE ACCOUNTING [FL-LINE-ACCOUNTING] — the invariant that authored content
cannot be silently dropped from the structured view.

    Every non-blank line of a `.lts` file is either CONSUMED by a parser branch,
    or CLASSIFIED as ignorable. Anything else is a lint warning.

WHY ONE INVARIANT AND NOT THREE RULES
-------------------------------------
Three distinct silent-drop mechanisms were found in the same corpus, all with the
same SHAPE — the author wrote structure, the parser read nothing, the linter said
`0 errors`:

  1. a `## Relationships` triple whose VERB was registered by the domain profile but
     not by the parser's hardcoded core table (the profile's `relationship_verbs`
     block was read only by a reporting tool) — the whole line discarded;
  2. a `## Relationships` triple with NO leading `- ` bullet — `_REL_SRC_RE`
     required one, so the line was never even looked at (this half included 194
     triples using CORE verbs the parser knows perfectly, so it was never a verb
     problem at all);
  3. a `- key: value` line matching none of SPEC §2.2's four formats — consumed by
     neither branch of `_parse_properties`, absent from `properties`, still present
     in `source_excerpt` so the file reads correct.

A FOURTH and a FIFTH were found in 2026-08 by measuring CAPTURE RATIO rather than line
coverage, and both are the reason this docstring can no longer end at three:

  4. a WRAPPED property value. The head parsed; the continuation lines classified
     `prose` and were therefore ACCOUNTED-AS-IGNORABLE while carrying two thirds of
     what the author wrote. On `41-example-wrapped-values.lts`, 61
     of 61 wrapped properties truncated to their first physical line and this module
     reported `coverage 100.0%, unaccounted 0`.
  5. an ID TABLE. A table whose first column is an identifier column is the acceptance
     contract of a spec, and `table_row` is in IGNORABLE_KINDS, so 14 rows of
     machine-and-bench verification handles scored as legitimately-not-data.

Both are now CONSUMED by named parser branches (`property_continuation`,
`id_table_row`), which is the only durable fix: an ignorable classification that is
wrong makes the instrument agree with the defect it exists to find. The lesson is
recorded here rather than in a commit message because it bounds what this invariant
can promise — line coverage answers "was every line READ?", never "was what the line
SAID kept?", and a capture-ratio measurement is what distinguishes the two.

A targeted rule per mechanism requires DISCOVERING the mechanism first, which is
what took eighteen days and a hand survey each time. Line accounting is the one
predicate that subsumes all three and, more to the point, the fourth nobody has
found yet — it does not ask "did the grammar I know about match?", it asks "did
ANY branch read this line, and if not, does it look like structure?".

WHY COVERAGE AND NOT EQUALITY
-----------------------------
An earlier framing was "warn when the parsed value differs from the written text".
That misfires: the parser has FIVE legitimate transformations where parsed != written
by design (bold-key normalization, multi-value bracket extraction, nested block ->
list, repeated key accumulating into a list, `.strip()`). Every one of them still
CONSUMES its source line, so a coverage check is immune to all five where an
equality check drowns in them. Coverage is the right predicate; equality is not.
(Credit: `[SPEC:FL-PROP-NEARMISS]`, which drew this conclusion one step short of
generalising it.)

WHY THE PARSER OWNS THE CONSUMPTION RECORD
------------------------------------------
This module NEVER re-derives "would the parser have matched this?". It reads
`LineCoverage.consumed`, which the parser writes at the exact points its branches
commit. A second copy of the grammar living in the linter is the same drift hazard
one level up — and a second copy of the verb table living in a reporting tool is
literally how mechanism 1 came about.

WHAT THIS MODULE DOES OWN
-------------------------
The IGNORABLE classifier: given a line the parser did not consume, is it one of the
things SPEC §2.3 legitimately permits in a body (prose, headings, fenced code,
tables, hand-drawn diagrams), or is it STRUCTURE-SHAPED — a line whose form says the
author expected it to be read? Structure-shape is deliberately narrow, two forms:

  * KEY-COLON  `<ident>:<space>` at the start of a column-0 list item, modulo the
    three enumerated cosmetic decorations (bold wrapper, leading pictograph, key
    case) — the neighbourhood of SPEC §2.2;
  * TRIPLE     `[ref] VERB [` — a bracketed source, an UPPER_SNAKE verb, and a
    BRACKETED target. Requiring the closing bracket is what stops prose minting
    findings: `[X] MUST be reused` has no `[` after `MUST`.

Everything else is prose, which on the surveyed corpora is 96% of candidate lines
and MUST NOT warn — a rule that cries wolf on prose is a rule that gets switched
off in a day.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .parser import (
    CORE_EDGE_LAYER,
    SECTION_CAUSAL,
    SECTION_RELATIONSHIPS,
    LineCoverage,
    _resolve_ref,
    classify_bracket_token,
)

# ---------------------------------------------------------------------------
# Verdict kinds
# ---------------------------------------------------------------------------
# CONSUMED — a parser branch read it. Never a finding.
KIND_CONSUMED = "consumed"

# IGNORABLE — explicitly permitted, non-structural content (SPEC §2.3 and the
# ordinary furniture of a Markdown-shaped document). Never a finding. The set is
# CLOSED and enumerated here so that "ignorable" can never quietly become the
# escape hatch that swallows the very thing this invariant exists to catch.
KIND_BLANK = "blank"                      # empty or whitespace-only
KIND_FENCED = "fenced_code"               # inside ``` / ~~~ (incl. the delimiters)
KIND_THEMATIC_BREAK = "thematic_break"    # --- / *** / ___ horizontal rule
# A DECORATIVE table only. A table whose first column is an identifier column is DATA
# and is CONSUMED by `id_table_row` before it ever reaches this classifier — see the
# discriminator in `parser._is_id_table`.
KIND_TABLE_ROW = "table_row"              # | a | b |  and its |---|---| separator
KIND_DIAGRAM = "diagram"                  # hand-drawn box/ASCII art outside a fence
KIND_BLOCKQUOTE = "blockquote"            # > callout
KIND_HTML_COMMENT = "html_comment"        # <!-- ... --> / raw HTML
KIND_PROSE = "prose"                      # a narrative paragraph line
KIND_LIST_PROSE = "list_prose"            # a column-0 bullet carrying narrative
KIND_NESTED_LIST_PROSE = "nested_list_prose"   # an INDENTED bullet (see NOTE below)
KIND_ORDERED_LIST = "ordered_list_item"   # `1. ...`

IGNORABLE_KINDS = frozenset({
    KIND_BLANK, KIND_FENCED, KIND_THEMATIC_BREAK, KIND_TABLE_ROW, KIND_DIAGRAM,
    KIND_BLOCKQUOTE, KIND_HTML_COMMENT, KIND_PROSE, KIND_LIST_PROSE,
    KIND_NESTED_LIST_PROSE, KIND_ORDERED_LIST,
})

# UNACCOUNTED — structure-shaped and read by nobody. These are the findings.
KIND_NEAR_MISS_PROPERTY = "near_miss_property"     # SPEC §2.2 neighbourhood
KIND_NEAR_MISS_TRIPLE = "near_miss_triple"         # `[SRC] VERB [TGT]`, no edge
KIND_NEAR_MISS_CAUSAL = "near_miss_causal"         # causal-graph line, no edge
KIND_UNACCOUNTED_FRONTMATTER = "unaccounted_frontmatter"  # F1 block, not a field
# An ID TABLE (first column is an identifier column) under a heading with no `[ID]`.
# The rows are data by the same rule that promotes every other ID table; there is
# simply no node to hang them on. Exactly the `outside_node_body` story one grammar
# over, and reported with the same loudness so the two cannot diverge in silence.
KIND_NEAR_MISS_ID_TABLE = "near_miss_id_table"
# (QUAL) SL-21 / SL-22 — a property-shaped bullet whose KEY REGION carries a QUALIFIER
# before the colon. Two kinds, not one variant of `near_miss_property`, for two reasons:
# the incidences are measured separately (the parenthetical form and the dash form are
# different authoring habits with different rates), and folding them into
# `near_miss_property` would move an existing per-corpus count that other work is
# tracking. See `_qualified_key_near_miss` for the discriminator and the RULING.
KIND_NEAR_MISS_QUALIFIED_PAREN = "near_miss_qualified_key_paren"   # `- key (qual): v`
KIND_NEAR_MISS_QUALIFIED_DASH = "near_miss_qualified_key_dash"     # `- key — qual: v`
# [C8] Era 0 / the maintainer's 2026-08-11 ruling (review V4 / class d -- ratified in neither
# SPEC nor code until now): `- [ID]: description` at column 0. The bracket occupies
# the KEY position; `_PROP_SIMPLE_RE`'s key grammar is `[a-z][a-z0-9_-]*` and never
# matches a bracket, so this line reached NOTHING before this rule -- neither a
# property (correct: the ruling says never mint) nor a diagnostic (the gap the
# ruling closes): it fell all the way through to `KIND_LIST_PROSE`, ordinary and
# silent. See `_id_colon_near_miss` for the discriminator.
KIND_NEAR_MISS_ID_COLON = "near_miss_id_colon"

UNACCOUNTED_KINDS = frozenset({
    KIND_NEAR_MISS_PROPERTY, KIND_NEAR_MISS_TRIPLE, KIND_NEAR_MISS_CAUSAL,
    KIND_UNACCOUNTED_FRONTMATTER, KIND_NEAR_MISS_ID_TABLE,
    KIND_NEAR_MISS_QUALIFIED_PAREN, KIND_NEAR_MISS_QUALIFIED_DASH,
    KIND_NEAR_MISS_ID_COLON,
})

# ---------------------------------------------------------------------------
# Shape regexes. These mirror the PARSER's own anchors; they never SUBSTITUTE for
# them. The parser decides what parsed; these only decide what the author's line
# LOOKED like once it demonstrably did not parse.
# ---------------------------------------------------------------------------
_BULLET_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*+])[ \t]+(?P<body>.*)$")
_ORDERED_RE = re.compile(r"^[ \t]*\d+[.)][ \t]+")
_THEMATIC_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$")
_TABLE_RE = re.compile(r"^[ \t]*\|")
_TABLE_SEP_RE = re.compile(r"^[ \t]*[|:\- ]{4,}$")
_HTML_RE = re.compile(r"^[ \t]*<")
_BLOCKQUOTE_RE = re.compile(r"^[ \t]*>")

# KEY-COLON shape. Mirrors `_PROP_SIMPLE_RE`'s key grammar (`[a-z][a-z0-9_-]*`) but
# case-insensitively, and REQUIRES the colon to be followed by whitespace or EOL.
# That last requirement is load-bearing: it is what keeps `https://…`, `doc://…`,
# `bus://assets/x` and `namespace:id` out of the finding set entirely.
_KEY_COLON_RE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_\-]*)[ \t]*:(?:[ \t]+(?P<value>.*\S.*))?[ \t]*$")
_LOWER_KEY_RE = re.compile(r"^[a-z][a-z0-9_\-]*$")

# (QUAL) SL-21 / SL-22 — the KEY REGION carries a QUALIFIER.
#
# THE DEFECT. `- requirement (qualified): text` and `- requirement — dashed: text` are
# BOTH authored in the live corpora and BOTH reach the graph as nothing. Neither matches
# `_PROP_SIMPLE_RE` (whose key grammar is `[a-z][a-z0-9_-]*`, no space, no paren, no
# dash), and neither matches `_KEY_COLON_RE` above — so the near-miss classifier returns
# None and `classify_line` calls the line LIST PROSE. Prose is IGNORABLE, so line
# accounting is satisfied at 100% and `lts_preview`'s `lost` channel is empty. The line
# is accounted for; the property is gone. Measured cost: rewriting `[ACME-EXAMPLE-COLD]`'s one
# bare `- requirement:` into the parenthetical form destroyed that node's only parsed
# requirement, and both instruments stayed green.
#
# THE RULING — FLAG, DO NOT PARSE (the maintainer, 2026-08-11; not re-decidable here).
# Teaching the parser to accept these forms means GUESSING the key: in
# `- requirement (qualified): text`, is the key `requirement` or `requirement (qualified)`?
# A wrong key is a SUBSTITUTION, and this project has already established that a
# substitution is worse than a loss — SL-18's `acme.ds.GOVERNS` silently became `GOVERNS`
# and looked perfectly well-formed. A missing property is detectable; a plausible wrong
# one is not. This is also the standing profile doctrine in `[GUIDANCE-LIFECYCLE-COMMENTS]`
# ("move the comment to its own property, not widen the enum") one grammar over: move the
# qualifier into the VALUE, do not widen the KEY.
#
# THE DISCRIMINATOR, and what it deliberately EXCLUDES.
#   * The key region is everything before the FIRST colon. A paren in the VALUE —
#     `- delta: (leading paren in value) form` — is untouched, parses today and must keep
#     parsing. That exclusion is the whole discriminator; an over-eager matcher that
#     scans the line rather than the key region looks identical on the positive cases.
#   * The leading token must be a well-formed LOWERCASE key (`[a-z][a-z0-9_-]*`). This is
#     the precision anchor: a false positive then requires prose that opens with a
#     lowercase word, carries a short qualifier, and reaches a colon.
#   * The qualifier is capped at 60 characters and may not contain a colon or a nested
#     paren, so a long prose clause is not mistaken for a qualifier.
#   * DASH means EN-DASH or EM-DASH only (U+2013 / U+2014). Hyphen-minus is deliberately
#     NOT in the class: `- key - qual: v` is ambiguous with both ordinary prose and with
#     the legal hyphenated key grammar (`blocked-on`), and widening to it would be a
#     precision loss with a plausible-sounding justification — the same trade the (WRAP)
#     comment in the parser refused for the same characters.
_QUAL_PAREN_KEY_RE = re.compile(
    r"^(?P<key>[a-z][a-z0-9_\-]*)"
    r"[ \t]*(?P<qualifier>\([^()]{1,60}\))"
    r"[ \t]*:(?:[ \t]+(?P<value>.*\S.*))?[ \t]*$"
)
_QUAL_DASH_KEY_RE = re.compile(
    r"^(?P<key>[a-z][a-z0-9_\-]*)"
    r"[ \t]*(?P<qualifier>[–—][ \t]*[^:()–—]{1,60}?)"
    r"[ \t]*:(?:[ \t]+(?P<value>.*\S.*))?[ \t]*$"
)
# Class A: the bold wrapper opens BEFORE the key and closes AFTER the value.
_BOLD_WRAP_RE = re.compile(r"^\*\*(?P<inner>.+)\*\*[ \t]*$")
# The well-formed bold key (`**key**: value`) — used only to peel the decoration.
_BOLD_KEY_RE = re.compile(r"^\*\*(?P<key>[^*]+?)\*\*[ \t]*(?P<rest>:.*)$")

# [C8] `- [ID]: description` -- a bracketed id sitting in the KEY position. The maintainer's
# 2026-08-11 ruling: FLAGGED, never minted (a guessed key would be a SUBSTITUTION,
# SL-18's standing lesson -- is the key `ID`? the doctype? the description? nobody
# guesses). Anchored at column 0 like `_KEY_COLON_RE`; the VALUE half is optional,
# same shape discipline as every other near-miss regex here.
_ID_COLON_RE = re.compile(
    r"^\[(?P<ref>[A-Za-z0-9_.:\- ]+?)\][ \t]*:(?:[ \t]+(?P<value>.*\S.*))?[ \t]*$"
)

# TRIPLE shape: a bracketed source ref, a verb token, and a BRACKETED target.
_TRIPLE_RE = re.compile(
    r"^\[(?P<src>[A-Za-z0-9_.:\- ]+?)\][ \t]+"
    r"(?P<verb>[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)?)[ \t]+"
    r"(?=\[)"
)
_UPPER_VERB_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# A causal-graph body line that clearly means an arrow between two refs.
_CAUSAL_SHAPE_RE = re.compile(r"\[[A-Za-z0-9_.:\- ]+\][ \t]*(?:->|→|=>)")

# Box-drawing / ASCII-art alphabet for hand-drawn diagrams (SPEC §2.3 lists them as
# ignored). A line is a diagram when its non-space characters are drawn almost
# entirely from this set.
_ART_CHARS = set("│─└├┌┐┘┬┴┼╔╗╚╝║═╭╮╰╯▼▲◄►◀▶→←↑↓⇒⇐↔•·+-_=*/\\<>^v|~.:'\"()[]{}#")
_ART_MIN_LEN = 4
_ART_THRESHOLD = 0.85


def _strip_pictographs(s: str) -> Tuple[str, bool]:
    """Strip a LEADING run of pictographic symbols (Unicode category `So`) plus the
    emoji joiners that trail them.

    Deliberately NARROW. The surveyed corpora showed that a looser "strip leading
    punctuation" rule turns `- (deferred: …)` and a backticked `` `uri: "doc://…"` ``
    into false positives — 6 out of 6 on that corpus. `(` is `Ps`, backtick is `Sk`;
    only `So` (⛔ ✅ ⚠ 🔴 …) is stripped, so both stay classified as prose.
    """
    i = 0
    stripped = False
    while i < len(s):
        ch = s[i]
        if unicodedata.category(ch) == "So" or ch in "️‍":
            i += 1
            stripped = True
        elif stripped and ch in " \t":
            i += 1
        else:
            break
    return s[i:], stripped


def _triple_why(verb: str, bare: str, bulleted: bool, section: Optional[str]) -> str:
    """Name the ACTUAL cause, not a disjunction of possible ones. The three are
    mechanically distinguishable from what we already know about the line."""
    if section != SECTION_RELATIONSHIPS:
        return (f"reads as `[SRC] {verb} [TGT]` but sits outside `## Relationships`, "
                f"and E2 edge extraction only runs inside that section")
    if not bulleted:
        return (f"reads as `[SRC] {verb} [TGT]` inside `## Relationships` but has no "
                f"leading `- ` bullet, which `_REL_SRC_RE` requires -- so the line was "
                f"never examined for an edge at all")
    if bare not in CORE_EDGE_LAYER:
        return (f"the verb {bare!r} is neither a core edge type nor registered with "
                f"the parser, so `_find_edge_type` returned None and the whole triple "
                f"was discarded (a profile's `relationship_verbs` block does not reach "
                f"the parser)")
    return (f"a bulleted core-verb triple that still produced no edge -- this should "
            f"be unreachable; report it")


def _is_diagram(text: str) -> bool:
    core = text.strip()
    if len(core) < _ART_MIN_LEN:
        return False
    dense = [c for c in core if not c.isspace()]
    if not dense:
        return False
    hits = sum(1 for c in dense if c in _ART_CHARS)
    return hits / len(dense) >= _ART_THRESHOLD


@dataclass
class LineVerdict:
    """One line's accounting verdict. `unaccounted` is the only thing a lint acts on."""

    source_file: str
    line_index: int              # 0-based, indexes LineCoverage.lines
    line_number: int             # 1-based, what an editor shows
    text: str
    kind: str
    consumed_by: Optional[str] = None
    near_branch: Optional[str] = None   # the parser branch that ALMOST took it
    detail: Dict[str, object] = field(default_factory=dict)

    @property
    def unaccounted(self) -> bool:
        return self.kind in UNACCOUNTED_KINDS

    def message(self) -> str:
        """Actionable: what was written, what almost read it, and what to do."""
        snippet = self.text.strip()
        if len(snippet) > 120:
            snippet = snippet[:117] + "..."
        why = self.detail.get("why", "")
        near = f" (nearly consumed by {self.near_branch})" if self.near_branch else ""
        tail = f" -- {why}" if why else ""
        return (f"line reads as structure but no parser branch consumed it{near}: "
                f"{snippet!r}{tail}")

    def to_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "line_number": self.line_number,
            "text": self.text,
            "kind": self.kind,
            "consumed_by": self.consumed_by,
            "near_branch": self.near_branch,
            "detail": self.detail,
        }


def classify_line(cov: LineCoverage, idx: int) -> LineVerdict:
    """Account for ONE line: consumed, ignorable, or unaccounted.

    Order of decision is fixed and total:
      1. consumed by a parser branch                       -> KIND_CONSUMED
      2. blank / fenced / furniture (rule, table, quote, HTML, ordered list, art)
      3. inside the frontmatter block and not a field      -> UNACCOUNTED
      4. STRUCTURE-SHAPED (key-colon or triple)            -> UNACCOUNTED
      5. everything else                                   -> prose
    """
    text = cov.lines[idx]
    line_number = idx + 1
    mk = lambda kind, **kw: LineVerdict(   # noqa: E731 - local, keeps the table readable
        source_file=cov.source_file, line_index=idx, line_number=line_number,
        text=text, kind=kind, **kw)

    branch = cov.consumed.get(idx)
    if branch is not None:
        return mk(KIND_CONSUMED, consumed_by=branch)

    if not text.strip():
        return mk(KIND_BLANK)
    if idx in cov.fenced:
        return mk(KIND_FENCED)

    section = cov.section.get(idx)

    # --- frontmatter block: a non-field line inside F1 -----------------------
    if idx < cov.body_start:
        stripped = text.strip()
        if stripped.startswith("#"):
            return mk(KIND_PROSE, detail={"where": "frontmatter comment"})
        return mk(KIND_UNACCOUNTED_FRONTMATTER, near_branch="frontmatter_field",
                  detail={"why": "inside the `---` frontmatter block but not a "
                                 "`key: value` field, so it is in no frontmatter dict "
                                 "(a wrapped/continued value is the usual cause)"})

    # --- furniture -----------------------------------------------------------
    if _THEMATIC_RE.match(text):
        return mk(KIND_THEMATIC_BREAK)
    if _TABLE_RE.match(text) or _TABLE_SEP_RE.match(text):
        # ASK the parser (never re-derive): did its ID-TABLE discriminator accept this
        # table and then find no `[ID]` heading to attach it to?
        if idx in getattr(cov, "id_table_unattached", ()):
            return mk(KIND_NEAR_MISS_ID_TABLE, near_branch="id_table_row",
                      detail={"why": "the table's first column is an identifier column, "
                                     "so its rows are DATA -- but the nearest enclosing "
                                     "heading carries no `[ID]`, and ID tables are read "
                                     "only inside a node body. Give the enclosing "
                                     "heading an `[ID]` and every row lands as an "
                                     "`id_table` property"})
        return mk(KIND_TABLE_ROW)
    if _BLOCKQUOTE_RE.match(text):
        return mk(KIND_BLOCKQUOTE)
    if _HTML_RE.match(text):
        return mk(KIND_HTML_COMMENT)
    if _ORDERED_RE.match(text):
        return mk(KIND_ORDERED_LIST)
    if _is_diagram(text):
        return mk(KIND_DIAGRAM)

    # --- the causal-graph section has its own grammar ------------------------
    if section == SECTION_CAUSAL:
        if _CAUSAL_SHAPE_RE.search(text):
            return mk(KIND_NEAR_MISS_CAUSAL, near_branch="_CAUSAL_EDGE_RE",
                      detail={"why": "an arrow between bracketed refs inside "
                                     "`## Causal Graph`, but not in the "
                                     "`EDGE_TYPE: [SRC] -> [TGT]` form E1/E3 reads"})
        return mk(KIND_PROSE, detail={"where": "causal graph section"})

    bullet = _BULLET_RE.match(text)
    body = bullet.group("body") if bullet else text
    indented = bool(bullet and bullet.group("indent"))

    # --- TRIPLE shape (mechanisms 1 and 2) -----------------------------------
    # Checked in EVERY section, bulleted or not: an authored triple is a triple. The
    # bracketed-target requirement inside `_TRIPLE_RE` is what keeps prose out.
    triple = _TRIPLE_RE.match(body.strip())
    if triple is not None:
        src = _resolve_ref(triple.group("src"))
        verb = triple.group("verb")
        bare = verb.split(".")[-1]
        if src is not None and _UPPER_VERB_RE.match(bare):
            return mk(KIND_NEAR_MISS_TRIPLE, near_branch="_parse_section_edges",
                      detail={
                          "why": _triple_why(verb, bare, bool(bullet), section),
                          "verb": verb,
                          "source_ref": src[1],
                          "bulleted": bool(bullet),
                          "section": section,
                      })

    # --- KEY-COLON shape (mechanism 3 and its neighbourhood) -----------------
    # Section-aware: SKIPPED inside `## Relationships`, whose lines legitimately
    # read `- [SRC] VERB [TGT]` and are `_parse_section_edges`' business. Found by
    # dogfooding the rule against the proposal that motivated it, whose sole hit was
    # its own edge triple. A rule that warns on the corpus's own edge idiom would be
    # abandoned in a day.
    if section == SECTION_RELATIONSHIPS:
        return mk(KIND_PROSE, detail={"where": "relationships section"})

    if bullet is not None and not indented and bullet.group("marker") == "-":
        # [C8] Checked FIRST: `_KEY_COLON_RE` below anchors on `[A-Za-z][A-Za-z0-9_-]*`
        # and a line opening `[` never matches it, so without this branch
        # `- [ID]: description` falls all the way to `KIND_LIST_PROSE` -- exactly the
        # silent gap the 2026-08-11 ruling closes (review V4 / class d).
        idm = _ID_COLON_RE.match(body.rstrip())
        if idm is not None:
            ref = _resolve_ref(idm.group("ref"))
            cls = classify_bracket_token(idm.group("ref"))
            if ref is not None or cls == "MALFORMED-REF":
                return mk(KIND_NEAR_MISS_ID_COLON, near_branch="_PROP_SIMPLE_RE",
                          detail={"why": _WHY["id_colon"],
                                  "ref": ref[1] if ref is not None else idm.group("ref"),
                                  "ref_class": "REF" if ref is not None else cls,
                                  "has_value": idm.group("value") is not None})
        in_body = idx in cov.node_body
        near = _key_colon_near_miss(body, in_node_body=in_body)
        if near is not None:
            variant, key, has_value, qualifier = near
            detail: Dict[str, object] = {
                "why": _WHY[variant], "variant": variant,
                "key": key, "has_value": has_value,
            }
            if qualifier is not None:
                detail["qualifier"] = qualifier
            if not in_body:
                detail["orphaned_from"] = "no enclosing [ID]-bearing heading"
            else:
                detail["node_id"] = cov.node_body[idx]
            return mk(_VARIANT_KIND.get(variant, KIND_NEAR_MISS_PROPERTY),
                      near_branch=_NEAR_BRANCH[variant], detail=detail)
        return mk(KIND_LIST_PROSE)

    if bullet is not None:
        # NOTE (deliberate recall/precision trade): an INDENTED bullet is never
        # property-shaped to the parser -- P1/P2/P3/P4 all anchor at column 0, and a
        # nested item is only read directly under a `- key:` head. Checking indented
        # bullets for key-colon shape would fire on ordinary sub-bullets of prose,
        # which are common. We take the precision. Measured cost is reported in the
        # commit that introduced this rule.
        return mk(KIND_NESTED_LIST_PROSE)

    return mk(KIND_PROSE)


# variant -> the parser branch that ALMOST consumed the line, and why it did not.
_NEAR_BRANCH = {
    "case": "_PROP_SIMPLE_RE",
    "bold_wrap": "_PROP_BOLD_RE",
    "symbol_prefix": "_PROP_SIMPLE_RE",
    "empty_block": "_PROP_KEY_ONLY_RE",
    "outside_node_body": "_parse_properties",
    "unexplained": "_parse_properties",
    "qualified_paren": "_PROP_SIMPLE_RE",
    "qualified_dash": "_PROP_SIMPLE_RE",
    # NOTE: "id_colon" is NOT looked up through this table -- its `near_branch` is
    # passed directly at its (separate, earlier) call site in `classify_line`, since
    # it does not go through `_key_colon_near_miss`'s variant dispatch at all.
}
# (QUAL) variant -> the finding KIND. Anything not listed is `near_miss_property`, so the
# existing five variants keep their kind and their per-corpus count exactly.
_VARIANT_KIND = {
    "qualified_paren": KIND_NEAR_MISS_QUALIFIED_PAREN,
    "qualified_dash": KIND_NEAR_MISS_QUALIFIED_DASH,
}
_WHY = {
    "case": "the key is not all-lowercase, and `_PROP_SIMPLE_RE` anchors on "
            "`[a-z][a-z0-9_-]*` -- lowercase the key and it parses",
    "bold_wrap": "the `**` opens before the KEY and closes after the VALUE; "
                 "`_PROP_BOLD_RE` needs `- **key**: value`",
    "symbol_prefix": "a leading pictograph breaks the `^-[ \\t]+[a-z]` anchor -- "
                     "move the marker into the value (`- key: (marker) value`)",
    "empty_block": "a `- key:` head whose indented `- item` block did not follow "
                   "(mis-indented, a `*` bullet, or a blank line between), so P4 "
                   "collected nothing and the key is absent from `properties`",
    "outside_node_body": "property-shaped, but the nearest enclosing heading carries "
                         "no `[ID]` -- node bodies end at the NEXT heading of ANY "
                         "level, so an un-tagged `### Contract` subhead orphans every "
                         "property under it and `_parse_properties` never sees them",
    "unexplained": "well-formed `- key: value` at column 0 inside a node body that no "
                   "branch consumed -- this should be unreachable; report it",
    # (QUAL) The WHY names the REPAIR, because the repair is the whole point of flagging
    # rather than parsing: the parser will not guess which half of the key region is the
    # key, and the author is the only one who knows.
    "qualified_paren": "the key region carries a PARENTHETICAL qualifier before the "
                       "colon (`- key (qualifier): value`), and `_PROP_SIMPLE_RE`'s key "
                       "grammar is `[a-z][a-z0-9_-]*` -- no space, no parenthesis. The "
                       "property is NOT set and the line reads as prose, so line "
                       "accounting stays green. NOT parsed on purpose: `key (qualifier)` "
                       "is ambiguous between the key `key` and the key "
                       "`key (qualifier)`, and a guessed key is a SUBSTITUTION (SL-18), "
                       "which is worse than a loss. REPAIR: move the qualifier into the "
                       "VALUE (`- key: (qualifier) value`), or give it its own property",
    "id_colon": "a bracketed id occupies the KEY position (`- [ID]: description`). "
               "Ruled FLAGGED, never minted (the maintainer, 2026-08-11): guessing whether the "
               "key should be the id, the id's doctype, or `description` itself is a "
               "SUBSTITUTION (SL-18's standing lesson), so the line stays prose and "
               "the property is NOT set. REPAIR: give the field its own lowercase key "
               "(`- verified-by: [ID] -- description`), or move the id into a "
               "`## Relationships` triple if a graph edge was intended",
    "qualified_dash": "the key region carries an EN/EM-DASH qualifier before the colon "
                      "(`- key -- qualifier: value`), and `_PROP_SIMPLE_RE`'s key "
                      "grammar is `[a-z][a-z0-9_-]*` -- no space, no dash. The property "
                      "is NOT set and the line reads as prose, so line accounting stays "
                      "green. NOT parsed on purpose: the dash makes the key boundary "
                      "ambiguous, and a guessed key is a SUBSTITUTION (SL-18), which is "
                      "worse than a loss. REPAIR: move the qualifier into the VALUE "
                      "(`- key: qualifier -- value`), or give it its own property",
}


def _key_colon_near_miss(body: str, *,
                         in_node_body: bool
                         ) -> Optional[Tuple[str, str, bool, Optional[str]]]:
    """Is this list-item body one enumerated decoration away from SPEC §2.2?

    Returns `(variant, key, has_value, qualifier)` or None. The decorations are exactly
    the three the measured near-miss survey enumerated -- bold wrapper, leading
    pictograph, key case -- plus the P4 head that collected nothing, plus the
    ORPHANED case (`in_node_body=False`: the line is perfectly well-formed and the
    property grammar was simply never run over it), plus the two (QUAL) classes where
    the KEY REGION itself carries a qualifier. `qualifier` is None for every variant
    but those two.
    Nothing else is added speculatively; each extra normalization costs precision,
    and the naive "a `- ` line with a colon" predicate measured 96% FALSE POSITIVE.
    """
    text = body.rstrip()
    if not text:
        return None

    variant: Optional[str] = None

    # Class A: `**key: value**` -- the wrapper spans the whole item.
    wrapped = _BOLD_WRAP_RE.match(text)
    if wrapped is not None:
        inner = wrapped.group("inner").strip()
        if _KEY_COLON_RE.match(inner):
            text, variant = inner, "bold_wrap"
    if variant is None:
        # Well-formed bold key, peeled so a CASE problem underneath is still seen.
        boldkey = _BOLD_KEY_RE.match(text)
        if boldkey is not None:
            text = boldkey.group("key").strip() + boldkey.group("rest")

    # Class B: leading pictograph.
    if variant is None:
        stripped, had = _strip_pictographs(text)
        if had:
            text, variant = stripped, "symbol_prefix"

    m = _KEY_COLON_RE.match(text)
    if m is None:
        # (QUAL) SL-21 / SL-22. Reached ONLY after the well-formed key-colon shape has
        # already been ruled out, so nothing that was classified before is reclassified
        # now: this branch can only convert a line that was about to be called PROSE.
        # Checked on the PEELED text, so `- **key (qual)**: v` is seen too.
        qual = _qualified_key_near_miss(text)
        if qual is not None:
            return qual
        return None
    key = m.group("key")
    value = m.group("value")

    if not in_node_body:
        # DOMINANT explanation, whatever the decoration: `_parse_properties` runs ONLY
        # over an [ID]-bearing heading's body, so this line was never offered to the
        # grammar at all. Reporting a decoration here would send the author chasing a
        # cosmetic fix for a structural problem.
        variant = "outside_node_body"
    elif variant is None:
        # Offered and rejected. The remaining explanations are the key's CASE or a P4
        # head whose block did not follow.
        if not _LOWER_KEY_RE.match(key):
            variant = "case"
        elif value is None:
            variant = "empty_block"
        else:
            # A well-formed `- key: value` at column 0, inside a node body, that NO
            # branch took. Under today's grammar this should be unreachable. It is
            # kept, named, and reported rather than silently folded into a
            # neighbouring variant -- an unexplained accounting failure is the single
            # most interesting thing this invariant can produce, and rounding it off
            # to a known cause is how the last three mechanisms stayed hidden.
            variant = "unexplained"
    return variant, key, value is not None, None


def _qualified_key_near_miss(text: str) -> Optional[Tuple[str, str, bool, str]]:
    """(QUAL) SL-21 / SL-22 — does the KEY REGION carry a qualifier before the colon?

    Returns `(variant, key, has_value, qualifier)` or None. `variant` is
    `qualified_paren` or `qualified_dash`; the two are kept apart because their
    incidences are measured apart.

    ⛔ THE EXCLUSION IS THE POINT. `- delta: (leading paren in value) form` parses today
    and must keep parsing: the paren is in the VALUE, and both regexes anchor the
    qualifier strictly BEFORE the first colon. An over-eager matcher that searches the
    whole line satisfies every positive case here and destroys that one, which is why
    the self-check control carries the exclusion beside each positive.

    `in_node_body` is deliberately NOT a parameter. Unlike a well-formed key-colon line,
    a qualified key would fail the property grammar even inside a perfectly tagged node
    body — the orphan explanation is not the dominant one here, and the repair is the
    same in both places. The caller still records the orphan status in `detail`.
    """
    for variant, rx in (("qualified_paren", _QUAL_PAREN_KEY_RE),
                        ("qualified_dash", _QUAL_DASH_KEY_RE)):
        m = rx.match(text)
        if m is not None:
            return (variant, m.group("key"), m.group("value") is not None,
                    m.group("qualifier").strip())
    return None


def account_file(cov: LineCoverage) -> List[LineVerdict]:
    """Every line of one file, accounted for. Total by construction."""
    return [classify_line(cov, i) for i in range(len(cov.lines))]


def unaccounted_lines(cov: LineCoverage) -> List[LineVerdict]:
    """Only the findings: structure-shaped lines no parser branch consumed."""
    return [v for v in account_file(cov) if v.unaccounted]


def account_summary(verdicts: List[LineVerdict]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in verdicts:
        out[v.kind] = out.get(v.kind, 0) + 1
    out["_total"] = len(verdicts)
    out["_unaccounted"] = sum(1 for v in verdicts if v.unaccounted)
    return out
