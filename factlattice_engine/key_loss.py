"""[KEY-LOSS-RETROSCAN] — the instrument that would have caught `overview.lts`.

WHAT WENT WRONG, MEASURED
    `~/projects/pcb/overview.lts` (the maintainer, 2026-07-18) authored 46 top-level
    `- ` bullets. The parser minted 27 properties. Nineteen bullets — including ALL FIVE
    HAL stresses, the load-bearing content of the fleet's only PCB record — produced
    nothing, because each opens

        - **(S1) CAN transport — the tenth kind…** …

    Markdown emphasis in the key region. A property key must match `^[a-z][a-z0-9_-]*$`
    (`_PROP_SIMPLE_RE`); anything else makes the whole line parse as PROSE.

    ⛔ `lts_lint` REPORTED THE FILE CLEAN OF THIS THE WHOLE TIME. Measured 2026-08-13:
    the ratified [FL-LINE-ACCOUNTING] coverage lint emits ZERO findings on that file —
    twelve dangling-ref warnings and nothing else. Not because the rule is broken;
    because the rule is a PRECISION instrument (01 tuned it against a 96%-false-positive
    naive predicate) and the S1–S5 lines are, to a precision instrument, prose. Silence
    was the whole defect.

WHY THIS IS NOT `property_loss.py`
    `factlattice_engine.property_loss` is the FALSIFIER for proposal 03 — a per-month
    trend counter with a deliberately narrow headline (tiers A+B, in node bodies). It is
    built on a colon-split: `_SPLIT` requires a colon within the first 80 characters of
    the bullet. THE FIVE STRESS LINES HAVE NO SUCH COLON, so the falsifier does not see
    them either. Measured, not assumed — see `tests/test_key_loss.py::
    test_falsifier_misses_the_known_case`.

    So this module asks a DIFFERENT and cruder question, and crudeness is the point:

        AUTHORED-MINUS-MINTED. How many top-level bullets did the author write in a node
        body, how many properties came out, and WHICH LINES ARE THE DIFFERENCE?

    That number is arithmetic, not a heuristic — it cannot miss a failure shape nobody
    has named yet, which is exactly how every prior guard failed (see
    `authoring/schema.py`: "an enumeration of REMEMBERED failure modes, so it inherits
    the shape of the memory rather than the shape of the failure"). The named SHAPES
    below only EXPLAIN each dropped line; they never decide whether it was dropped.

THE TWO INSTRUMENTS THIS SPLITS INTO
    RETRO-SCAN (this module, `scan_file`/`scan_roots`) — reports the full delta. Every
        dropped bullet, with its line number, annotated with the strongest intent signal
        found. Recall over precision: a prose bullet in the list costs a reader a glance;
        a missed one costs a month.
    LINT (`factlattice_engine.lint.key_loss`) — fires only on the shapes carrying real
        evidence of property INTENT. Precision over recall, because a rule that cries
        wolf on prose gets switched off in a day.

⛔ NO SECOND COPY OF THE GRAMMAR. What parsed is read from the parser's own consumption
   record (`LineCoverage.consumed`); what WOULD have normalised is read from
   `factlattice.authoring.schema._coerce_key`. This module owns neither.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from factlattice.authoring.schema import _coerce_key
from factlattice.coverage import account_file
from factlattice.parser import (
    BRANCH_PROP_BOLD,
    BRANCH_PROP_KEY_ONLY,
    BRANCH_PROP_SIMPLE,
    SECTION_CAUSAL,
    SECTION_RELATIONSHIPS,
    parse_text,
)

# The branches that MINT A PROPERTY HEAD. A bullet consumed by anything else (an edge
# triple, a node-def list item, an id-table row) is not a lost property — it is a
# different kind of line doing its job.
PROPERTY_HEAD_BRANCHES = frozenset({
    BRANCH_PROP_SIMPLE, BRANCH_PROP_BOLD, BRANCH_PROP_KEY_ONLY,
})

# Bullet markers INCLUDING the ones the parser will never accept. `-` is the only marker
# `_PROP_SIMPLE_RE` matches; every other glyph here is a silent total loss for the line.
_BULLET = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*+–—•·●▪])[ \t]+(?P<body>.*)$")
_LEGAL_KEY = re.compile(r"^[a-z][a-z0-9_\-]*$")
# Colon not followed by `/` or `:` — the URI guard. `doc://x`, `https://h`, `1:1:` are
# not key separators, and without this guard they dominate the finding set.
_COLON = re.compile(r":(?![/:])")
# A leading markdown emphasis RUN: `**…**`, `*…*`, `__…__`, `_…_`.
_EMPH_LEAD = re.compile(r"^(?P<open>\*\*\*|\*\*|\*|___|__|_)(?P<run>.+?)(?P=open)(?P<rest>.*)$")
# Key regions are short and few-worded. Measured: real keys are hyphenated 1–4 tokens
# (`source-file`, `mcu / brains`, `Round-trip test`); five-token regions are prose with
# a mid-sentence colon.
_WORDISH = re.compile(r"^\S+(?:[ \t]\S+){0,4}$")
_MAX_KEY_REGION = 60
# A key region wrapped in a CODE SPAN — `- `select`: Click to select widgets`. Coerces
# to a legal key, but it is the ENUMERATION idiom (the values of a namespace listed
# under a heading), not a field label. Named separately from `pictograph_key` so the
# two are never conflated: a backtick is neither a pictograph nor an emoji, and folding
# it into that bucket is what made a first cut of this scan read 57 pictographs where
# 40 of them were code spans.
_CODE_SPAN_KEY = re.compile(r"^`[^`]+`$")
# A NODE REFERENCE used as the key — `- [ACME-LEAF-OWNS-BEHAVIOR]: the Host owns spawn/…`.
# Named separately because the author's intent here is unambiguous and specific: they are
# writing a per-target field, and what they get is prose. Independently corroborated —
# `preview.py`'s control fixture already lists lines 232/233/235 as UNMINTED for exactly
# this reason, while the linter said nothing about them.
_BRACKET_REF_KEY = re.compile(r"^\[[A-Z][A-Z0-9_:-]*\]")
# ⛔ THE PRECISION RULE. A key region containing a FINITE VERB is a sentence clause with
# a mid-sentence colon, not a field label:
#     - Internal bus is operational: publishing to `composer/tool/mode` changes state.
#     - This follows Acme convention: state engines always have a value.
# Both coerce to perfectly legal keys and both are prose. Measured 2026-08-13: this one
# predicate removes 100+ false positives from the flagged set while keeping every
# confirmed real loss (`mcu / brains`, `connectors / ports`, `headline for the
# architect`, `widget IDs`) — none of which contains a verb.
#
# ⛔ CASE-SENSITIVE, DELIBERATELY. An IGNORECASE first cut classified `**(S1) CAN
# transport — …**` as prose, because `CAN` — the bus — matched the modal verb `can`. It
# silently dropped the single most load-bearing line in the acceptance case, and the
# scan still LOOKED right (18 flagged instead of 17). English finite verbs are lowercase
# mid-clause; an uppercase token is a name, not a verb.
_CLAUSE_VERB = re.compile(
    r"(?:^|[ \t])(?:is|are|was|were|be|been|being|has|have|had|does|do|did|must|should|"
    r"shall|can|could|will|would|may|might|uses|use|used|follows|means|requires|"
    r"returns|becomes|makes|gets|goes|lives|reads|writes|holds|owns|needs|wants|"
    r"there|they|it|we|you"
    r"|This|These|Those|It|We|You|They|There|Every|Each|Any|If|When|Because)(?:[ \t]|$)")

# A bold LEAD-IN LABEL — the S1–S5 shape. Bounded because an entire bolded sentence
# followed by nothing is a callout, not a field label.
_MAX_LABEL_CHARS = 110
_MAX_LABEL_TOKENS = 18
_MAX_LABEL_HEAD_TOKENS = 7
# `**(S1) …**` — an enumeration tag in front of the label. Stripped before the head is
# taken, because otherwise the head is the empty string and the label reads as unlabelled.
_LABEL_TAG = re.compile(r"^\((?:[^)]{1,12})\)\s*")

_RESERVED_SECTIONS = frozenset({SECTION_RELATIONSHIPS, SECTION_CAUSAL})

# ── the named shapes ─────────────────────────────────────────────────────────────
# Each is a HYPOTHESIS ABOUT AUTHOR INTENT, and each is falsifiable by reading the line.
# `confidence` drives what the LINT fires on; the retro-scan reports every shape.
HIGH, MEDIUM, NONE = "high", "medium", "none"

SHAPES: dict[str, tuple[str, str]] = {
    # shape                        confidence  one-line explanation
    "md_emphasis_key":            (HIGH,   "markdown emphasis in the key region"),
    "pictograph_key":             (HIGH,   "leading pictograph/symbol before the key"),
    "uppercase_key":              (HIGH,   "uppercase in the key region"),
    "no_space_after_colon":       (HIGH,   "no space after the colon"),
    "nonascii_bullet_marker":     (HIGH,   "bullet marker is not '-'"),
    "code_span_key":              (MEDIUM, "key region is a code span — enumeration idiom"),
    "bracketed_ref_key":          (HIGH,   "a [NODE-REF] used as the key"),
    "spaced_key":                 (MEDIUM, "spaces in the key region"),
    "slashed_key":                (MEDIUM, "slash in the key region"),
    "digit_initial_key":          (MEDIUM, "key region begins with a digit"),
    "dashed_qualifier_key":       (MEDIUM, "en/em-dash qualifier in the key region"),
    "parenthetical_key":          (MEDIUM, "parenthetical in the key region"),
    "unnormalisable_key":         (MEDIUM, "word-shaped key region, not normalisable"),
    "emphasis_label_lead":        (MEDIUM, "bold lead-in label with no colon"),
    "outside_node_body":          (MEDIUM, "property-shaped, but no node to hang it on"),
    "prose_no_signal":            (NONE,   "reads as prose; no property intent detected"),
}
LINT_SHAPES = frozenset(s for s, (c, _) in SHAPES.items() if c in (HIGH, MEDIUM)) - {"prose_no_signal"}

# ── the HIGH band's own contract, enforced ───────────────────────────────────────
# ⛔ HIGH MEANS "MECHANICAL: COERCES TO A LEGAL KEY, NO AUTHORING DECISION REQUIRED", and
# it is the band the lint's docstring recommends ratcheting to `error` FIRST. Measured
# 2026-08-13 across 178 live `.lts` files: 75 of 866 HIGH findings (8.7%) carried
# `normalised_key: None` — i.e. they did NOT coerce to a legal key, so the band's own
# stated property was false for one in twelve of its members, and ratcheting HIGH to
# `error` would have red-gated on findings no mechanical fix can clear.
#
# The split is by WHERE THE EVIDENCE COMES FROM, not by severity taste:
#
#   INTRINSIC evidence — the author's intent is proven by PUNCTUATION they typed, and the
#   key region legitimately may not normalise. `- [ACME-LEAF-OWNS-BEHAVIOR]: the Host owns…`
#   is unambiguously a per-target field; an id is uppercase and never coerces. A missing
#   space after a colon, and a bullet marker that is not `-`, are the same: the shape is
#   the evidence. These stay HIGH with no candidate key.
#
#   LEXICAL evidence — the ONLY evidence is that the key region looks key-shaped after
#   coercion (`md_emphasis_key`, `uppercase_key`, `spaced_key`). When coercion does not
#   in fact produce a legal key, there is no evidence left: what remains is a bold lead-in
#   or a mid-sentence colon, which is prose or, at best, a field whose name requires a
#   CHOICE. Those demote to MEDIUM — `key_loss_choice`, which is precisely what they are.
#
# This narrows nothing that was measured real and un-claims a property the band did not
# have. A band that cannot keep its own promise cannot be ratcheted, and an un-ratchetable
# band is a warning stream.
_INTRINSIC_HIGH_SHAPES = frozenset({
    "bracketed_ref_key", "no_space_after_colon", "nonascii_bullet_marker",
    "pictograph_key",
})


@dataclass(frozen=True)
class Dropped:
    """One top-level bullet, in a node body, that minted no property."""

    source_file: str
    line_number: int          # 1-based
    text: str
    node_id: Optional[str]
    shapes: tuple[str, ...]   # every signal detected, strongest first
    confidence: str
    key_region: Optional[str]
    normalised_key: Optional[str]   # what it WOULD have been. Never written anywhere.
    key_parsed_elsewhere: bool = False
    ratified_kind: Optional[str] = None      # what [FL-LINE-ACCOUNTING] called it
    seen_by_ratified_lint: bool = False      # would today's lts_lint have shown this?

    @property
    def primary_shape(self) -> str:
        return self.shapes[0] if self.shapes else "prose_no_signal"

    def render(self) -> str:
        n = f" [{self.node_id}]" if self.node_id else ""
        return (f"{self.source_file}:{self.line_number}:{n} "
                f"{'+'.join(self.shapes)} ({self.confidence}) :: {self.text.strip()[:120]}")


@dataclass
class FileReport:
    source_file: str
    authored_bullets: int          # top-level `- ` bullets anywhere outside a fence
    authored_in_node_bodies: int   # …of those, the ones the property grammar was offered
    minted_properties: int
    dropped: list[Dropped] = field(default_factory=list)
    node_count: int = 0
    error: Optional[str] = None

    @property
    def loss(self) -> int:
        """authored-minus-minted, restricted to node bodies. THE headline number."""
        return len(self.dropped)

    @property
    def flagged(self) -> int:
        """…of which carry an intent signal (what the LINT would fire on)."""
        return sum(1 for d in self.dropped if d.confidence != NONE)

    def to_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "authored_bullets": self.authored_bullets,
            "authored_in_node_bodies": self.authored_in_node_bodies,
            "minted_properties": self.minted_properties,
            "nodes": self.node_count,
            "loss": self.loss,
            "flagged": self.flagged,
            "dropped": [asdict(d) for d in self.dropped],
            "error": self.error,
        }


# ──────────────────────────────────────────────────────────────────────────────────
# the classifier — EXPLAINS a drop, never decides one
# ──────────────────────────────────────────────────────────────────────────────────

def classify(line: str, *, in_node_body: bool) -> tuple[tuple[str, ...], str, Optional[str], Optional[str]]:
    """(shapes, confidence, key_region, normalised_key) for one dropped bullet line.

    ⛔ Called ONLY on lines the parser already declined. It answers "what did the author
    most likely mean", never "did this parse" — the parser owns that and this module
    does not second-guess it.
    """
    bm = _BULLET.match(line)
    if bm is None:                       # defensive; callers filter to bullets
        return ("prose_no_signal",), NONE, None, None

    shapes: list[str] = []
    body = bm.group("body").rstrip()
    if bm.group("marker") != "-":
        shapes.append("nonascii_bullet_marker")

    cm = _COLON.search(body)
    key_region: Optional[str] = None
    fixed: Optional[str] = None

    if cm is not None and cm.start() <= _MAX_KEY_REGION:
        key_region = body[: cm.start()].strip()
        value = body[cm.end():]
        if not key_region or not value.strip():
            # `- key:` alone is a NESTED-BLOCK head — a different mechanism with its own
            # ratified kind. Mixing it in here would blend two failure rates into one.
            key_region = None
        else:
            fixed, applied = _coerce_key(key_region)
            if _CLAUSE_VERB.search(key_region):
                # a sentence clause with a mid-sentence colon — see _CLAUSE_VERB
                return ("prose_no_signal",), NONE, key_region, None
            if value and not value[:1].isspace():
                shapes.append("no_space_after_colon")
            # ⛔ MATCH THE COERCION MESSAGES EXACTLY. A first cut tested `"emphasis" in a`
            # first, which also matches the LOWERCASING message ("emphasis is not
            # machine-readable") — so every uppercase key in the corpus was reported as a
            # markdown-emphasis loss. 386 findings carried the wrong cause. Substring
            # matching against prose is how a measurement lies without failing.
            for a in applied:
                if a.startswith("removed markdown emphasis"):
                    shapes.append("md_emphasis_key")
                elif a.startswith("stripped leading symbol"):
                    shapes.append("code_span_key" if _CODE_SPAN_KEY.match(key_region)
                                  else "bracketed_ref_key" if _BRACKET_REF_KEY.match(key_region)
                                  else "pictograph_key")
                elif a.startswith("spaces"):
                    shapes.append("spaced_key")
                elif a.startswith("lowercased"):
                    shapes.append("uppercase_key")
            wordish = bool(_WORDISH.match(key_region)) and len(key_region) <= _MAX_KEY_REGION
            if not _LEGAL_KEY.match(fixed):
                if not wordish:
                    # sentence-shaped key region: 01 measured this bucket at 96% prose
                    return ("prose_no_signal",), NONE, key_region, None
                if fixed[:1].isdigit():
                    shapes.append("digit_initial_key")
                if "/" in fixed:
                    shapes.append("slashed_key")
                if any(c in fixed for c in "–—"):
                    shapes.append("dashed_qualifier_key")
                if "(" in fixed or ")" in fixed:
                    shapes.append("parenthetical_key")
                if not shapes:
                    shapes.append("unnormalisable_key")
                # the CANDIDATE key, for corroboration only — never written. Guessing a
                # key is a SUBSTITUTION, and this project has ruled a substitution worse
                # than a loss (coverage.py SL-21/22).
                head = re.split(r"[(,—–/]", key_region, maxsplit=1)[0]
                cand, _ = _coerce_key(head.strip())
                fixed = cand if _LEGAL_KEY.match(cand) else None
            elif not wordish:
                return ("prose_no_signal",), NONE, key_region, None

    if not shapes:
        # NO COLON (or a colon that led nowhere). The remaining intent signal is the
        # BOLD LEAD-IN LABEL — `- **(S1) CAN transport — …** rest of the sentence`.
        # This is the shape that lost overview.lts, and the shape both the
        # ratified coverage lint and the proposal-03 falsifier are structurally blind to.
        em = _EMPH_LEAD.match(body)
        if em and em.group("rest").strip():
            run = em.group("run").strip()
            # The LABEL HEAD, not the whole bolded run: a field label may be followed by
            # an em-dashed gloss inside the same emphasis (`**(S1) CAN transport — the
            # tenth kind…**`). Judging the whole run would call every one of those a
            # sentence; judging the head alone is what separates a label from a callout.
            head = re.split(r"[:—–(,.;]", _LABEL_TAG.sub("", run), maxsplit=1)[0].strip()
            if (head and len(run) <= _MAX_LABEL_CHARS
                    and len(run.split()) <= _MAX_LABEL_TOKENS
                    and len(head.split()) <= _MAX_LABEL_HEAD_TOKENS
                    and not _CLAUSE_VERB.search(head)):
                cand, _ = _coerce_key(head)
                return (("emphasis_label_lead",), MEDIUM, head,
                        cand if _LEGAL_KEY.match(cand) else None)
        return ("prose_no_signal",), NONE, key_region, None

    if not in_node_body:
        # The property grammar NEVER RAN over this line — there is no node to hang the
        # field on. Real loss, different mechanism; named so it is never conflated with
        # a key-hygiene miss (which is all an authoring surface can fix).
        shapes.append("outside_node_body")

    order = {HIGH: 0, MEDIUM: 1, NONE: 2}
    shapes.sort(key=lambda s: order[SHAPES[s][0]])
    confidence = SHAPES[shapes[0]][0]
    # THE HIGH BAND KEEPS ITS OWN PROMISE — see `_INTRINSIC_HIGH_SHAPES`. A HIGH claim
    # resting on LEXICAL evidence alone, whose key region did NOT coerce to a legal key,
    # has no evidence left; it is a choice, not a mechanical fix.
    if (confidence == HIGH and fixed is None
            and not (set(shapes) & _INTRINSIC_HIGH_SHAPES)):
        confidence = MEDIUM
    return tuple(dict.fromkeys(shapes)), confidence, key_region, fixed


# ──────────────────────────────────────────────────────────────────────────────────
# the scan
# ──────────────────────────────────────────────────────────────────────────────────

def scan_text(text: str, source_file: str) -> FileReport:
    pf = parse_text(text, source_file)
    cov = pf.coverage
    lines = text.splitlines()

    verdicts = {v.line_index: v for v in account_file(cov)}
    node_at: dict[int, str] = dict(cov.node_body)

    minted = sum(len(n.properties) for n in pf.nodes)
    parsed_keys = {k for n in pf.nodes for k in n.properties}

    authored = 0
    authored_in_body = 0
    dropped: list[Dropped] = []

    for i, line in enumerate(lines):
        if i in cov.fenced:
            continue
        bm = _BULLET.match(line)
        if bm is None or bm.group("indent"):
            continue                       # top-level bullets only
        authored += 1
        if cov.section.get(i) in _RESERVED_SECTIONS:
            continue                       # edge triples: a different grammar, doing its job
        in_body = i in node_at
        if in_body:
            authored_in_body += 1
        if cov.consumed.get(i) in PROPERTY_HEAD_BRANCHES:
            continue                       # it minted a property. Nothing to report.
        if cov.consumed.get(i) is not None:
            continue                       # consumed by SOME other branch, doing its job
        shapes, conf, region, fixed = classify(line, in_node_body=in_body)
        if not in_body and conf == NONE:
            continue                       # prose outside a node body is just prose
        v = verdicts.get(i)
        dropped.append(Dropped(
            source_file=source_file, line_number=i + 1, text=line.rstrip(),
            node_id=node_at.get(i), shapes=shapes, confidence=conf,
            key_region=region, normalised_key=fixed,
            key_parsed_elsewhere=bool(fixed and fixed in parsed_keys),
            ratified_kind=(v.kind if v else None),
            seen_by_ratified_lint=bool(v and v.unaccounted),
        ))

    return FileReport(
        source_file=source_file, authored_bullets=authored,
        authored_in_node_bodies=authored_in_body, minted_properties=minted,
        dropped=dropped, node_count=len(pf.nodes),
    )


def scan_file(path: str | Path) -> FileReport:
    from factlattice.hashing import safe_read_text
    p = Path(path)
    try:
        return scan_text(safe_read_text(str(p), p.name), str(p))
    except Exception as exc:  # noqa: BLE001 — an unreadable file is a REPORTED row
        return FileReport(source_file=str(p), authored_bullets=0,
                          authored_in_node_bodies=0, minted_properties=0,
                          error=f"{type(exc).__name__}: {exc}")


def scan_roots(roots: Iterable[str | Path]) -> dict:
    """THE RETRO-SCAN. Every `.lts` under every root, ranked by ABSOLUTE LOSS.

    Ranking is by absolute count, not rate, on purpose: the goal is to find the next
    `overview.lts` before someone relies on it, and a small file that lost its
    only three fields is a smaller problem than a keystone document that lost nineteen.
    """
    files: list[Path] = []
    for root in roots:
        rp = Path(root).expanduser()
        if rp.is_dir():
            from factlattice.hashing import walk_lts_paths
            files += [Path(x) for x in walk_lts_paths(str(rp))]
        elif rp.exists():
            files.append(rp)
    files = sorted(dict.fromkeys(files))

    reports = [scan_file(f) for f in files]

    # Corroboration is only meaningful CORPUS-WIDE: a key lost on its only use in a file
    # has by construction no surviving sibling there. (property_loss.py measured the
    # per-file version at 0/827 — the signal exists only across the scanned set.)
    vocabulary: set[str] = set()
    for f, r in zip(files, reports):
        if r.error:
            continue
        try:
            from factlattice.hashing import safe_read_text
            pf = parse_text(safe_read_text(str(f), f.name), str(f))
            vocabulary |= {k for n in pf.nodes for k in n.properties}
        except Exception:  # noqa: BLE001
            pass
    for r in reports:
        r.dropped = [Dropped(**{**asdict(d),
                               "key_parsed_elsewhere": bool(d.normalised_key
                                                            and d.normalised_key in vocabulary)})
                     for d in r.dropped]

    by_shape: Counter = Counter()
    for r in reports:
        for d in r.dropped:
            for s in d.shapes:
                by_shape[s] += 1

    lossy = sorted((r for r in reports if r.loss), key=lambda r: (-r.loss, r.source_file))
    return {
        "roots": [str(Path(r).expanduser()) for r in roots],
        "files_scanned": len(files),
        "files_with_loss": len(lossy),
        "totals": {
            "authored_bullets": sum(r.authored_bullets for r in reports),
            "authored_in_node_bodies": sum(r.authored_in_node_bodies for r in reports),
            "minted_properties": sum(r.minted_properties for r in reports),
            "lost_bullets": sum(r.loss for r in reports),
            "flagged_with_intent": sum(r.flagged for r in reports),
            "seen_by_ratified_lint": sum(1 for r in reports for d in r.dropped
                                         if d.seen_by_ratified_lint),
            "corroborated": sum(1 for r in reports for d in r.dropped
                                if d.key_parsed_elsewhere),
        },
        "by_shape": dict(by_shape.most_common()),
        "vocabulary_size": len(vocabulary),
        "files": [r.to_dict() for r in lossy],
        "unreadable": [r.source_file for r in reports if r.error],
    }


def render_text(report: dict, *, show_prose: bool = False) -> str:
    t = report["totals"]
    out = [
        f"KEY-LOSS RETRO-SCAN — {report['files_scanned']} .lts files, "
        f"{report['files_with_loss']} with loss",
        f"  authored top-level bullets : {t['authored_bullets']}",
        f"  minted properties          : {t['minted_properties']}",
        f"  LOST (authored, minted nothing, not an edge/table line) : {t['lost_bullets']}",
        f"  …carrying an intent signal : {t['flagged_with_intent']}",
        f"  …today's lts_lint shows    : {t['seen_by_ratified_lint']}",
        "",
        "RANKED BY ABSOLUTE LOSS",
    ]
    for f in report["files"]:
        out.append(f"\n  {f['source_file']}")
        out.append(f"    {f['authored_bullets']} authored / {f['minted_properties']} minted "
                   f"→ {f['loss']} lost ({f['flagged']} flagged) over {f['nodes']} nodes")
        for d in f["dropped"]:
            if not show_prose and d["confidence"] == NONE:
                continue
            nid = d["node_id"] or "-"
            out.append(f"      L{d['line_number']:>4} [{nid}] {'+'.join(d['shapes'])} "
                       f"({d['confidence']}) :: {d['text'].strip()[:110]}")
    out.append("")
    out.append("BY SHAPE: " + ", ".join(f"{k}={v}" for k, v in report["by_shape"].items()))
    return "\n".join(out)


def _main(argv: list[str] | None = None) -> int:
    import argparse, json, sys
    ap = argparse.ArgumentParser(prog="python3 -m factlattice_engine.key_loss",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("roots", nargs="+")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="include prose bullets with no intent signal")
    a = ap.parse_args(argv)
    rep = scan_roots(a.roots)
    print(json.dumps(rep, indent=2) if a.json else render_text(rep, show_prose=a.all))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
