"""[AUTH-PATH-FALSIFIER] — the number that can kill proposal 03.

WHAT THIS IS FOR, STATED AS THE PROPOSAL STATES IT
    [AUTH-PATH-NOT-A-CAGE] `what-would-falsify-the-whole-proposal`:

        "if the recurrence rate does not fall after layer 1 is available and used.
         That is measurable — count property-shaped lines that fail to parse, per
         corpus, per month. If it does not move, the diagnosis in section 2 was
         wrong and the cause is something else."

    Three enforcement layers installed with no way to know whether the diagnosis held
    is exactly the shape proposal 03 argues against. So the counter ships WITH layer 1,
    not after it, and a BASELINE is recorded before adoption.

WHY IT IS NOT JUST `unaccounted_lines()`
    The obvious implementation is to reuse the ratified [FL-LINE-ACCOUNTING] classifier
    in `factlattice.coverage` and count the `near_miss_property*` kinds. That classifier
    is deliberately PRECISE — 01 measured the naive "a `- ` line with a colon" predicate
    at 96% FALSE POSITIVE and tuned against it — and precision is the right target for a
    LINT that nags an author.

    ⛔ IT IS THE WRONG TARGET FOR A FALSIFIER. Measured 2026-08-12 on the nine live
    defect shapes (the four from `acme/corpora/acme-specs/61-fleet-console.lts`
    plus 03 §1's five), the ratified classifier flags FOUR:

        - ⚠ premise: …                 near_miss_property          DETECTED
        - forbidden — X: …             near_miss_qualified_key_dash DETECTED
        - Capitalised: …               near_miss_property          DETECTED
        - **bold-wrap: v**             near_miss_property          DETECTED
        - measured, 2026-08-12: …      list_prose                  MISSED
        - worked example: …            list_prose                  MISSED
        – endash-bullet: …             prose                       MISSED
        - nospace:after colon          list_prose                  MISSED
        - 3-digit-initial: …           list_prose                  MISSED

    A falsifier built on a detector that misses five of nine classes can FALL while the
    failure holds — the author's habit shifts one class sideways and the graph reports
    success. That is the precise failure mode 03 names for layer 2 ("an enforcement layer
    whose firing is uncertain is WORSE THAN NONE, because it will be cited as
    protection"), one instrument over.

    So this counter uses its OWN, WIDER predicate, reports it in CONFIDENCE TIERS, and
    reports the ratified classifier's number ALONGSIDE — so the gap between what is
    measured and what is linted is visible, quantified, and shrinkable, instead of being
    the silent difference between two numbers nobody compares.

THE TIERS — AND WHY THE HEADLINE IS NARROWER THAN "PROPERTY-SHAPED"
    ⛔ A FIRST CUT OF THIS COUNTER READ 1,194 AND WAS USELESS. Measured 2026-08-12 across
    138 live files, a literal reading of "property-shaped lines that fail to parse"
    returned 1,194 — and inspecting the population showed most of it CANNOT MOVE when
    `lts_add_property` is adopted, for two distinct reasons:

      · lines OUTSIDE any node body. "- source-file: .../app.py" is a perfectly legal
        property that produced nothing because `_parse_properties` runs ONLY over an
        [ID]-bearing heading's body. That is a STRUCTURAL defect (there is no node to
        hang the field on), not a key-hygiene one, and no amount of authoring-surface
        adoption fixes it. Counting it in the headline guarantees a flat trend line and
        a false verdict of "the diagnosis was wrong".
      · documented PROSE IDIOMS. `- pan/zoom: …`, `- tap vs drag (on release): …`,
        `- ELEVATION / shadow: …` are knowing prose, and the standing engine doctrine for
        the qualified-key forms is explicitly FLAG, DO NOT PARSE (`coverage.py`, SL-21 /
        SL-22 RULING). The corpus tolerates them on purpose.

    A FALSIFIER MUST COUNT ONLY WHAT THE INTERVENTION COULD CHANGE. Otherwise the
    denominator drowns the signal and the proposal is refuted by arithmetic rather than
    by evidence — which is the same error, one instrument over, as citing an unverified
    hook as protection.

    A  normalisable    IN A NODE BODY, key region coerces to a legal key under the SAME
                       unambiguous normalisation `authoring.schema._coerce_key` applies.
                       The author would have got the field. Highest confidence.
    B  choice-required IN A NODE BODY, word-shaped key region that cannot be normalised
                       without INVENTING something. `lts_add_property` REFUSES these, by
                       name, with a reason — so adoption converts them into a rename, and
                       the count is expected to fall.
    C  outside-body    the grammar never ran over the line. Reported SEPARATELY and
                       EXCLUDED from the headline: real loss, different mechanism, not
                       layer 1's to fix.
    D  wide            sentence-shaped key region. Excluded — 01 measured this bucket at
                       96% prose.

    HEADLINE = A + B, in node bodies only.

    CORROBORATION. Each finding also carries `key_parsed_elsewhere`: does the normalised
    key appear as a SUCCESSFULLY PARSED property somewhere else in the scanned set? That
    is the strongest available evidence the author meant a field rather than a sentence
    — `premise` parses on 40 other nodes, `pan/zoom` parses nowhere. The corroborated
    subtotal is the number to trust most, and it is reported beside the headline rather
    than instead of it, because a brand-new field legitimately has no precedent.

PER MONTH
    `by_month=True` buckets each finding by the month of the commit that last touched
    its line (`git blame --line-porcelain`), so a TREND is available immediately rather
    than after a month of waiting. Ungated files (untracked, or outside a work tree) are
    counted under `"unversioned"` and reported separately rather than dropped — the
    volume-resident decision files 03 §5 flags as layer 3's blind spot land here, and
    silently omitting them would flatter exactly the population layer 1 exists to cover.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional

from factlattice.coverage import account_file
from factlattice.parser import _HEADING_RE, _heading_id, parse_text

TIER_A = "A_normalisable"
TIER_B = "B_choice_required"
TIER_C = "C_outside_node_body"
TIER_D = "D_wide"
HEADLINE_TIERS = (TIER_A, TIER_B)

# Bullet markers, INCLUDING the en/em dash forms the ratified classifier calls prose.
_BULLET = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*+–—])[ \t]+(?P<body>.*)$")
# Colon-not-followed-by-slash: the URI guard. `doc://x`, `bus://a`, `https://h` are NOT
# property-shaped, and without this guard they dominate the finding set.
_SPLIT = re.compile(r"^(?P<key>[^:]{1,80}?)[ \t]*:(?![/:])[ \t]*(?P<value>.*)$")
_LEGAL_KEY = re.compile(r"^[a-z][a-z0-9_\-]*$")
# A key region is at most FOUR whitespace-separated tokens. Measured: real keys are
# hyphenated and short (`source-file`, `bus-native symmetry`, `Round-trip test`), while
# five-token regions are prose with a mid-sentence colon (`drag tracks the finger 1:1:`).
_WORDISH = re.compile(r"^\S+(?:[ \t]\S+){0,3}$")

# Reserved sections: their bullets are edge triples parsed by `_parse_section_edges`,
# not properties. 01 [FL-PROP-NEARMISS-RULE] `must-be-section-aware` — a property-shaped
# check applied here fires on every WELL-FORMED edge triple, and "a rule that warns on
# the corpus's own edge idiom would be abandoned in a day".
_RESERVED = frozenset({"relationships", "causal graph", "edges", "graph", "references"})

# Class-B signatures: what makes a key region need an authoring CHOICE rather than a
# normalisation. Kept explicit so the tier boundary is auditable, not vibes.
_B_SIGNATURES = (
    ("digit_initial", lambda k: bool(k) and k[0].isdigit()),
    ("embedded_comma", lambda k: "," in k),
    ("parenthetical", lambda k: "(" in k or ")" in k),
    ("dashed_qualifier", lambda k: "–" in k or "—" in k or " - " in k),
    ("slashed", lambda k: "/" in k),
)


@dataclass(frozen=True)
class Finding:
    source_file: str
    line_number: int
    text: str
    tier: str
    key_region: str
    normalised_key: Optional[str]    # tier A: what WOULD be written. tier B: a GUESS,
                                     # used only for corroboration, never written.
    reason: str
    node_id: Optional[str]
    ratified_kind: Optional[str]      # what [FL-LINE-ACCOUNTING] called it, or None
    detected_by_ratified: bool
    key_parsed_elsewhere: bool = False  # the corroboration signal — see THE TIERS
    month: Optional[str] = None       # "YYYY-MM" of the commit that last touched the line


def _coerce(raw: str) -> tuple[str, list[str]]:
    """The SAME normalisation the writer applies. Imported rather than reimplemented so
    tier A can never drift from what `lts_add_property` would silently accept."""
    from factlattice.authoring.schema import _coerce_key
    return _coerce_key(raw)


def _reserved_spans(lines: list[str]) -> list[tuple[int, int]]:
    """0-based [start, end) spans of reserved edge sections."""
    heads, in_fence = [], False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        hm = _HEADING_RE.match(line)
        if hm:
            parsed = _heading_id(hm.group(2))
            title = (parsed[2] if parsed else hm.group(2)).strip().lower()
            heads.append((i, len(hm.group(1)), title))
    spans = []
    for n, (idx, level, title) in enumerate(heads):
        if title not in _RESERVED:
            continue
        end = len(lines)
        for idx2, level2, _t in heads[n + 1:]:
            if level2 <= level:
                end = idx2
                break
        spans.append((idx, end))
    return spans


# ⛔ [TOOL-REASON-IS-A-FIXED-CODE] the CLOSED set of per-finding `reason` sentences `_classify` returns (registered in
# reason_codes.info_reasons()). tests/test_reason_sites.py checks that the classifier returns nothing else.
PROPERTY_LOSS_REASONS = tuple(name for name, _pred in _B_SIGNATURES) + (
    "sentence-shaped key region — probably prose",
    "key region normalises to a legal key",
    "word-shaped key region, not normalisable",
    "outside any node body — the property grammar never ran",
)


def _classify(body: str, *, in_node_body: bool
              ) -> Optional[tuple[str, str, Optional[str], str]]:
    """(tier, key_region, normalised_key_or_None, reason) or None if not property-shaped."""
    m = _SPLIT.match(body.rstrip())
    if m is None:
        return None
    key_region = m.group("key").strip()
    value = m.group("value").strip()
    if not key_region or not value:
        # `- key:` with nothing after is a NESTED-BLOCK head, a different mechanism with
        # its own ratified kind (`empty_block`). Not counted here; counting it would mix
        # two failure rates into one trend line.
        return None
    fixed, _applied = _coerce(key_region)
    wordish = bool(_WORDISH.match(key_region)) and len(key_region) <= 40

    if not in_node_body:
        # DOMINANT explanation whatever the decoration, exactly as the ratified
        # classifier rules (`coverage._key_colon_near_miss`, `in_node_body=False`):
        # the property grammar was never offered this line. Layer 1 cannot fix it.
        if _LEGAL_KEY.match(fixed) or wordish:
            return TIER_C, key_region, (fixed if _LEGAL_KEY.match(fixed) else None), \
                "outside any node body — the property grammar never ran"
        return None

    if _LEGAL_KEY.match(fixed):
        if not wordish:
            return TIER_D, key_region, fixed, "sentence-shaped key region — probably prose"
        return TIER_A, key_region, fixed, "key region normalises to a legal key"
    # The CANDIDATE key: what the author most plausibly meant, taken as the key region
    # up to the first qualifier opener. `born-from (refinement)` -> `born-from`. Used
    # ONLY for corroboration — it is never written anywhere, and `lts_add_property`
    # still refuses the line, because guessing the key is a SUBSTITUTION and this
    # project has ruled that a substitution is worse than a loss (coverage.py SL-21/22).
    head, _sep, _rest = re.split(r"[(,—–/]", key_region, maxsplit=1)[0], None, None
    cand, _ = _coerce(head.strip())
    cand = cand if _LEGAL_KEY.match(cand) else None
    for name, pred in _B_SIGNATURES:
        if pred(fixed) and wordish:
            return TIER_B, key_region, cand, name
    if wordish:
        return TIER_B, key_region, cand, "word-shaped key region, not normalisable"
    return TIER_D, key_region, None, "sentence-shaped key region — probably prose"


def scan_text(text: str, source_file: str) -> list[Finding]:
    """Every property-shaped line in one file that produced no property."""
    pf = parse_text(text, source_file)
    lines = text.splitlines()
    reserved = _reserved_spans(lines)

    # What DID parse, by line: the parser is the authority on that, always.
    parsed_lines: set[int] = set()
    verdicts = {v.line_index: v for v in account_file(pf.coverage)}
    for idx, v in verdicts.items():
        if v.kind == "consumed":
            parsed_lines.add(idx)

    node_at: dict[int, str] = {}
    for n in pf.nodes:
        for i in range(n.line_range[0] - 1, min(n.line_range[1], len(lines))):
            node_at[i] = n.id

    # every key that DID parse somewhere in this file — the corroboration source. The
    # caller folds these across files so a key proven elsewhere corroborates here.
    parsed_keys: set[str] = set()
    for n in pf.nodes:
        parsed_keys.update(n.properties)

    out: list[Finding] = []
    in_fence = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or i in parsed_lines:
            continue
        if any(lo <= i < hi for lo, hi in reserved):
            continue
        bm = _BULLET.match(line)
        if bm is None or bm.group("indent"):
            continue                     # top-level bullets only; indented items are
                                         # nested-block members with their own grammar
        cls = _classify(bm.group("body"), in_node_body=i in node_at)
        if cls is None:
            continue
        tier, key_region, fixed, reason = cls
        v = verdicts.get(i)
        out.append(Finding(
            source_file=source_file, line_number=i + 1, text=line.rstrip(),
            tier=tier, key_region=key_region, normalised_key=fixed, reason=reason,
            node_id=node_at.get(i),
            ratified_kind=(v.kind if v else None),
            detected_by_ratified=bool(v and v.unaccounted),
            key_parsed_elsewhere=bool(fixed and fixed in parsed_keys),   # per-file;
            # re-stamped corpus-wide by `corroborate()` — see its docstring
        ))
    return out


def parsed_keys_of(text: str, source_file: str = "<x>") -> set[str]:
    """Every property key this file successfully produced. The corroboration vocabulary."""
    return {k for n in parse_text(text, source_file).nodes for k in n.properties}


def scan_file(path: str | Path) -> list[Finding]:
    from factlattice.hashing import safe_read_text
    p = Path(path)
    return scan_text(safe_read_text(str(p), p.name), str(p))


def corroborate(findings: list[Finding], vocabulary: set[str]) -> list[Finding]:
    """Re-stamp `key_parsed_elsewhere` against a WHOLE-SCAN vocabulary.

    ⛔ A PER-FILE VOCABULARY MEASURED ZERO. First cut evaluated corroboration against the
    keys parsed in the SAME file, and returned 0 corroborated out of 827 across 138 live
    files — because a key lost on its only use in a file has, by construction, no
    surviving sibling there. The signal only exists corpus-wide: `premise` parses on
    dozens of other nodes, `pan/zoom` parses nowhere. Kept as a named function so the
    scope of the vocabulary is a visible argument rather than an accident of nesting.
    """
    return [Finding(**{**asdict(f),
                      "key_parsed_elsewhere": bool(f.normalised_key
                                                   and f.normalised_key in vocabulary)})
            for f in findings]


def _blame_months(path: Path, line_numbers: Iterable[int]) -> dict[int, str]:
    """1-based line -> 'YYYY-MM' of the commit that last touched it. {} if unversioned."""
    wanted = sorted(set(line_numbers))
    if not wanted:
        return {}
    try:
        args = ["git", "-C", str(path.parent), "blame", "--line-porcelain"]
        for ln in wanted:
            args += ["-L", f"{ln},{ln}"]
        args += ["--", str(path)]
        raw = subprocess.run(args, capture_output=True, text=True, timeout=60)
        if raw.returncode != 0:
            return {}
    except Exception:  # noqa: BLE001 — an unavailable blame is a missing bucket, not a crash
        return {}
    out: dict[int, str] = {}
    cur_line: Optional[int] = None
    import datetime
    for ln in raw.stdout.splitlines():
        parts = ln.split()
        if len(parts) >= 4 and len(parts[0]) == 40 and all(c in "0123456789abcdef" for c in parts[0]):
            cur_line = int(parts[2])
        elif ln.startswith("author-time ") and cur_line is not None:
            ts = int(ln.split()[1])
            out[cur_line] = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m")
    return out


def scan_corpus(roots: Iterable[str | Path], *, by_month: bool = False,
                limit_examples: int = 12) -> dict:
    """THE FALSIFIER. Point-in-time count of property-shaped lines that failed to parse,
    per corpus root, optionally bucketed by the month the line was last written."""
    report: dict = {"success": True, "roots": [], "totals": {}, "generated_for": "AUTH-PATH-FALSIFIER"}
    grand = Counter()
    grand_tier = Counter()
    grand_month: Counter = Counter()
    grand_reason = Counter()
    ratified_detected = 0
    all_examples: list[dict] = []

    for root in roots:
        rp = Path(root).expanduser().resolve()
        from factlattice.hashing import safe_read_text, walk_lts_paths
        files = [Path(x) for x in walk_lts_paths(str(rp))] if rp.is_dir() else [rp]
        r_tier, r_reason, r_month = Counter(), Counter(), Counter()
        r_detected = r_corrob = 0
        per_file: dict[str, int] = {}
        # PASS 1 — the vocabulary of keys that DO parse anywhere under this root.
        vocabulary: set[str] = set()
        for f in files:
            try:
                vocabulary |= parsed_keys_of(safe_read_text(str(f), f.name), str(f))
            except Exception:  # noqa: BLE001 — pass 2 reports the unreadable file
                pass
        # PASS 2 — the findings, corroborated against it.
        for f in files:
            try:
                findings = corroborate(scan_file(f), vocabulary)
            except Exception as exc:  # noqa: BLE001
                report.setdefault("unreadable", []).append({"file": str(f), "error": str(exc)})
                continue
            if by_month and findings:
                months = _blame_months(f, [x.line_number for x in findings])
                findings = [Finding(**{**asdict(x), "month": months.get(x.line_number, "unversioned")})
                            for x in findings]
            head = [x for x in findings if x.tier in HEADLINE_TIERS]
            if head:
                per_file[str(f)] = len(head)
            for x in findings:
                r_tier[x.tier] += 1
                if x.tier in HEADLINE_TIERS:
                    r_reason[x.reason] += 1
                    if x.month:
                        r_month[x.month] += 1
                    if x.detected_by_ratified:
                        r_detected += 1
                    if x.key_parsed_elsewhere:
                        r_corrob += 1
                    if len(all_examples) < limit_examples:
                        all_examples.append({
                            "file": str(f), "line": x.line_number, "tier": x.tier,
                            "key_region": x.key_region, "reason": x.reason,
                            "node": x.node_id, "ratified_kind": x.ratified_kind,
                            "detected_by_ratified_lint": x.detected_by_ratified,
                            "key_parsed_elsewhere": x.key_parsed_elsewhere,
                            "text": x.text.strip()[:110],
                        })
        headline = sum(r_tier[t] for t in HEADLINE_TIERS)
        report["roots"].append({
            "root": str(rp), "files_scanned": len(files),
            "vocabulary_size": len(vocabulary),
            "headline_property_lines_lost": headline,
            "corroborated": r_corrob,
            "excluded_outside_node_body": r_tier[TIER_C],
            "excluded_sentence_shaped": r_tier[TIER_D],
            "by_tier": dict(r_tier),
            "by_reason": dict(r_reason.most_common()),
            "by_month": dict(sorted(r_month.items())) if by_month else None,
            "detected_by_ratified_lint": r_detected,
            "missed_by_ratified_lint": headline - r_detected,
            "ratified_lint_recall": (round(r_detected / headline, 3) if headline else None),
            "worst_files": dict(sorted(per_file.items(), key=lambda kv: -kv[1])[:10]),
        })
        grand_tier.update(r_tier)
        grand_reason.update(r_reason)
        grand_month.update(r_month)
        ratified_detected += r_detected
        grand["corroborated"] += r_corrob
        grand["files"] += len(files)

    headline = sum(grand_tier[t] for t in HEADLINE_TIERS)
    report["totals"] = {
        "files_scanned": grand["files"],
        "headline_property_lines_lost": headline,
        "corroborated": grand["corroborated"],
        "corroborated_share": (round(grand["corroborated"] / headline, 3) if headline else None),
        "excluded_outside_node_body": grand_tier[TIER_C],
        "excluded_sentence_shaped": grand_tier[TIER_D],
        "by_tier": dict(grand_tier),
        "by_reason": dict(grand_reason.most_common()),
        "by_month": dict(sorted(grand_month.items())) if by_month else None,
        "detected_by_ratified_lint": ratified_detected,
        "missed_by_ratified_lint": headline - ratified_detected,
        "ratified_lint_recall": (round(ratified_detected / headline, 3) if headline else None),
    }
    report["examples"] = all_examples
    report["how_to_falsify"] = (
        "Re-run this after layer 1 has been in use for a month and compare "
        "`by_month`. If the per-month headline count does NOT fall, the diagnosis in "
        "[AUTH-PATH-DIAGNOSIS] (a missing feature, not indiscipline) was WRONG and the "
        "cause is elsewhere. `ratified_lint_recall` is the second number to watch: it is "
        "the fraction of real losses the shipped [FL-LINE-ACCOUNTING] lint would have "
        "shown an author, and a headline that falls while recall stays low means the "
        "habit moved rather than stopped."
    )
    return report
