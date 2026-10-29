"""CLAIM-SHAPED IDENTIFIERS — a verdict in the id travels without its evidence.

    An IDENTIFIER should name the SUBJECT. A TITLE should be a LABEL.
    When either carries the VERDICT, the conclusion travels without its evidence — and
    identifiers travel furthest, into edges, `see-also` lists, citations and agent
    bootstraps.

        ACME-TRUNCATION-RISK            POINTER  — a reader must fetch to learn anything
        ACME-TIER-TRUNCATION-IS-A-LIE   ARGUMENT — a reader can cite it UNREAD

MEASURED, and this is why it is not a style objection: an architect cited
`[ACME-TIER-TRUNCATION-IS-A-LIE]` three times in dispatch briefs in one night WITHOUT EVER
OPENING THE NODE, and downstream agents took it as settled. The id did the arguing. That
is the same failure as a headline number travelling without its scope — the id is a claim
with no denominator attached, and it is MORE dangerous than a bare count because an id is
designed to be copied.

⛔ THIS IS NOT A RENAME WORKLIST, AND MUST NEVER BECOME ONE. Citations span four corpora
and the cost of a rename lands hardest on the MOST-CITED nodes — precisely the ones whose
naming matters most, and precisely the ones a rename breaks worst. The remedy is
forward-only and additive:

    SUBJECT IN THE IDENTIFIER, VERDICT IN A `claim:` PROPERTY.

a convention already half-practised in this ecosystem (the `ACME-VISION-*` pillars do it;
the `ACME-TIER-*` nodes do not). So this rule flags at `warning`, and the existing
population is reported as a CENSUS WITH A DENOMINATOR (`claim_id_census`, emitted on
every run) rather than as a list of things to go and fix.

CALIBRATION — every clause below was bought with a measurement over 3,034 nodes in 179
live `.lts` files across three repositories. The naive predicate fired 96 times (3.2%).
Three exclusions, in the order they were forced:

 1. MACHINE-MINTED IDS — 19 hits were auto-generated `DEFECT-…-3A9B1D` / `DELIV-…-7C21A9F4`
    ids, truncated mid-phrase by a slug generator. Nobody authored them as a claim and
    there is no naming decision available. Excluded by the trailing-hex signature.

 2. CLAIM-GENRE FAMILIES — 46 hits were `DEC-`, `PRINCIPLE-`, `INSIGHT-`, `INV-`, `DEFECT-`.
    ⛔ A DECISION THAT DOES NOT STATE ITS DECISION IN ITS NAME IS HARDER TO CITE, NOT
    EASIER. `DEC-1-4-LIMIT-IS-0-61` is LEGITIMATELY claim-shaped and its whole value is
    answering in one call; flagging a principle for asserting something is flagging it for
    existing. The genre is the licence, and it is overridable per corpus.

 3. ⭐ BARE NEGATION, MEASURED AND REJECTED — and this is a deliberate deviation from the
    brief, which named "a copula, a NEGATION, or a verdict noun" as signals. Split and
    measured separately over the same population:

        copula only  ->  9 findings (0.30%)   8 of 9 hand-classified TRUE
        negation only-> 11 findings (0.36%)   almost all FALSE

    `NO-` and `-NOT-` overwhelmingly name a BOUNDARY or a DISTINCTION, which is subject-
    naming and is exactly what the rule wants: `ACME-SOB-NO-POLICY-LAYER`, `KGM-NOT-IMPL`,
    `DRI-NO-MIGRATION`, `ACME-DS-REMOVED-NOT-DELETED`, `ACME-SUB-NOT-NEW`. Including
    negation would have MORE THAN DOUBLED the finding count while at least halving
    precision — and the brief's own warning is that "a noisy rule gets disabled and then
    protects nothing". So negation is not a signal here.

    Verdict NOUNS are dropped for the same reason and one more: the brief itself rules
    `RISK` acceptable and `IS-A-LIE` not, so the discriminator was never the noun — it was
    the COPULA that turns a noun phrase into a sentence. A bare noun blacklist would be a
    matter of taste with no measurement behind it.

RESIDUAL FALSE-POSITIVE RATE, STATED: 1 of 9 (`LEC-CORPUS-PATH-MUST-EXIST`, where `MUST`
is a REQUIREMENT's own modality rather than a verdict about a subject). ~11% on a
warning-severity rule that fires 9 times in 3,034 nodes. Named here so the number travels
with the rule instead of being rediscovered.

⛔ THE HARM MODEL HAS TWO HALVES — AMENDED 2026-08-16 (the maintainer), Era 1
--------------------------------------------------------------------
This module used to assert, in this docstring and in its own census `note`, that
citation-travel is *"the entire harm model"* — and on that basis it counted 454
sentence-shaped titles and explicitly declined to flag any of them.

**That was incomplete, and the missing half is independent.** the maintainer's amendment: a key or
id whose name reads as SELF-EVIDENT *suppresses reading the body that carries the real
guidance*. His example — a name of the shape `ALWAYS-QUERY-BROADLY` — is easy to skip
when busy, because reading the name FEELS like having read the instruction. Nothing has
to travel anywhere for that harm to land; it happens in the reader, at the moment of
skimming a find-result or a digest row.

So there are two harms, and they are detected by DIFFERENT signals:

  1. **CITATION LEAKAGE** — the name ARGUES. Signal: a COPULA segment (`X-IS-Y`). The id
     gets cited unread and the verdict travels as if it were the subject. Existing rule
     (`claim_shaped_id`).
  2. **READ SUPPRESSION** — the name INSTRUCTS. Signal: an IMPERATIVE in INITIAL position
     (`NEVER-TRUST-…`, `DONT-LEAP-…`, "Always cold with the bootstrap"). The name is a
     complete, actionable directive, so the body is redundant to a busy reader. New rule
     (`read_suppressing_name`).

⭐ THE TWO RULES HAVE OPPOSITE GENRE LICENCES, AND THAT IS THE PROOF THEY ARE
INDEPENDENT RATHER THAN ONE RULE WITH TWO SPELLINGS. `PRINCIPLE` / `STRATEGY` / `DEC`
are LICENSED for rule 1 — their genre IS a claim, and flagging a principle for asserting
something is flagging it for existing. For rule 2 those same families are the HIGHEST
RISK, because a principle whose name is a finished instruction is exactly the artifact a
reader stops at. MEASURED on the Acme corpora: the copula rule flags **0 of the 3**
directive-initial ids, and two of those three (`ACME-DONT-LEAP-BEFORE-LOOKING`,
`ACME-BOOT-CORE-30-NEVER-TRUST-AN-UNGUARDED-ZERO`) are precisely the maintainer's class. A genre
licence carried over from rule 1 would have silenced rule 2 on its best findings.

⛔ AND THIS IS WHY TITLES ARE NOW IN SCOPE. The old exemption reasoned "a title does not
travel into edges or citations". True — and irrelevant to harm 2, because a title travels
into every `lts_find` result row, every `tier='digest'` listing and every worklist, which
is exactly where skimming happens. Titles are still NOT flagged for being sentence-shaped
(454 of them are, and most are legitimate description); they are flagged only for being
IMPERATIVE-INITIAL, which is a far narrower and differently-motivated predicate.

MEASURED, Acme corpora, 1,900 nodes, 2026-08-16 — the FINAL predicate:
**24 findings — 3 ids (0.16%) and 22 titles (1.16%), one node carrying both.** Every one
hand-classified TRUE; the two false-positive classes found on the way (`Do …?` as an
INTERROGATIVE, and tier-2 verbs used as nouns) are excluded structurally, not by
blacklist. **Overlap with `claim_shaped_id`: ZERO of 24.** That number is the strongest
single piece of evidence in this module — the two rules, run over the same 1,900 nodes,
select entirely disjoint populations, so read-suppression is not a re-spelling of
citation-leakage and could not have been caught by widening rule 1.

By family: ACME 9, DEC 6, STRATEGY 4, PRINCIPLE 3, DX 1, DD 1 — i.e. **7 of 24 are in
families rule 1 LICENSES**, which is the licence inversion made concrete.

⚠ WHAT THIS RULE DOES NOT MEASURE, stated so it is not over-sold: whether a body actually
WENT UNREAD. Read-suppression is a claim about a reader, and this is a lexical proxy for
it. The proxy is defensible (a name that is a complete instruction is a name that CAN be
acted on unread) but it is a proxy, and the honest instrument for the real thing is the
query log — `read_suppressing_name` findings whose nodes are cited but never fetched
would be the direct measurement. That is not built, and is named here as the gap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

import factlattice

ORIGIN = "claim_id"
KIND_CLAIM_ID = "claim_shaped_id"
KIND_READ_SUPPRESSING = "read_suppressing_name"

# `warning`, never `error`. The remedy is an authoring decision on FUTURE ids, the
# existing population is explicitly not a worklist, and an error-severity naming rule on a
# corpus with 3,000 nodes is a rule someone turns off in an afternoon.
CLAIM_ID_SEVERITY: Dict[str, str] = {KIND_CLAIM_ID: "warning",
                                     KIND_READ_SUPPRESSING: "warning"}

# ---------------------------------------------------------------------------
# THE READ-SUPPRESSION SIGNAL — an IMPERATIVE in INITIAL position
# ---------------------------------------------------------------------------
# ⛔ TWO TIERS, BECAUSE ONE WORD LIST MEASURED BADLY AND THE MEASUREMENT IS RECORDED
# RATHER THAN THE TUNED RESULT ALONE.
#
# TIER 1 — MODALS AND NEGATORS. None of these is a noun or an adjective in English, so
# initial position is sufficient evidence on its own. This is the shape of the maintainer's own
# example (`ALWAYS-QUERY-BROADLY`).
DIRECTIVE_MODALS = frozenset({
    "ALWAYS", "NEVER", "DONT", "DO", "AVOID", "PREFER", "STOP",
})

# TIER 2 — BARE IMPERATIVE VERBS. Every one is ALSO a common noun in this domain, and
# that ambiguity is not theoretical:
#     MEASURED, first attempt: admitting these on initial position alone produced 25 id
#     findings of which TWELVE were `ACME-BUILD-PHASE-0`, `ACME-BUILD-PLAN`,
#     `ACME-BUILD-AGENTS` … — "build" as a NOUN, naming a subject, which is exactly what
#     the convention ASKS FOR. Add `SBR-READ-VS-REASON` and `LEC-READ-PATH-VANISHED-ROOT`
#     ("the read path" is a compound noun) and precision was near 50%.
# So tier 2 is admitted ONLY in a TITLE and ONLY when followed by a DETERMINER — i.e.
# when the verb takes an object phrase and the title is therefore a SENTENCE rather than
# a noun phrase. "Build a DETERMINISTIC linter…" qualifies; "Build pipeline order" does
# not. That single extra token moved the measurement from ~50% to 100% precision on the
# surveyed population, and it is a grammatical fact rather than a blacklist that has to
# grow every time a new noun appears.
DIRECTIVE_VERBS = frozenset({
    "USE", "READ", "KEEP", "MAKE", "TREAT", "ASSUME", "ASK", "COLLAPSE",
    "DECOMPOSE", "SHIP", "BUILD", "WRITE", "CHECK", "ROUTE", "DEFAULT",
    "STATE", "OPEN", "START", "PIN", "NAME", "GIVE", "PUT",
})

# Determiners/quantifiers that mark the start of an OBJECT PHRASE.
_DETERMINERS = frozenset({
    "THE", "A", "AN", "EACH", "EVERY", "ALL", "ANY", "BOTH", "YOUR", "ITS",
    "THIS", "THAT", "THESE", "THOSE", "BIG", "WORK", "MY", "OUR",
})

# Segments skipped when locating the START of an id's meaningful name: the family prefix
# is stripped by the caller, and what remains often opens with a short sub-family code
# (`BOOT`, `ICL`, `HKR`) or a numeric ordinal (`30`, `70`). `ALWAYS-…` after `ACME-BOOT-
# CORE-30-` is still an id that opens with a directive; requiring position 0 exactly
# would miss every namespaced family, which is most of the corpus.
_MAX_SUBFAMILY_SEGMENT = 4

# The COPULA set — a segment that turns a noun phrase into a sentence. Measured; see the
# module docstring for what was tried and rejected.
CLAIM_COPULA = frozenset({
    "IS", "ISNT", "ARE", "ARENT", "WAS", "WERE",
    "MUST", "CANNOT", "SHOULD", "WILL", "DOES",
})

# A slug-generator signature: a trailing 6-8 hex group, optionally plus an attempt suffix
# (`-A1`). Uppercase-only, so a legitimate word segment cannot match.
MACHINE_MINTED_RE = re.compile(r"-[0-9A-F]{6,8}(?:-A\d+)?$")

# Node families whose GENRE IS A CLAIM. Overridable per corpus (see `find_claim_ids`).
CLAIM_GENRE_PREFIXES = frozenset({
    "DEC",        # a decision records what was decided
    "PRINCIPLE",  # a principle IS an assertion
    "INSIGHT",    # an insight IS a finding
    "INV",        # an invariant IS a claim, and a normative one
    "DEFECT",     # a defect names a failure
    "FIX", "SCEN", "STRATEGY", "GUIDANCE", "DD",
})


@dataclass
class ClaimFinding:
    kind: str
    message: str
    source_file: Optional[str]
    source_line: Optional[int]
    node_id: Optional[str] = None
    detail: dict = field(default_factory=dict)


def is_machine_minted(node_id: str) -> bool:
    return bool(MACHINE_MINTED_RE.search(node_id or ""))


def claim_segments(node_id: str) -> List[str]:
    """The copula segments in an id, in order. Empty when the id names a subject."""
    return [s for s in (node_id or "").split("-") if s in CLAIM_COPULA]


def directive_opener(node_id: str, type_prefix: Optional[str] = None) -> Optional[str]:
    """The IMPERATIVE this id opens with, past its family prefix — else None.

    ⛔ NO GENRE LICENCE IS APPLIED HERE, DELIBERATELY, and it is the whole design point.
    `classify()` exempts DEC/PRINCIPLE/STRATEGY because their genre IS a claim. For
    read-suppression those families are the HIGHEST risk, not the lowest: a principle
    whose id is a finished instruction is precisely the name a busy reader acts on
    without opening the body. Measured — the copula rule flags 0 of the 3 ids this
    function finds.

    ⛔ TIER 1 ONLY. An id has no grammar around it — there is no determiner after
    `ACME-BUILD-` to tell you whether `BUILD` is a verb or a noun, and measurement says
    it is usually the noun. Tier-2 verbs are therefore admitted in TITLES only.
    """
    segs = [s for s in (node_id or "").upper().split("-") if s]
    if segs and type_prefix and segs[0] == type_prefix.upper():
        segs = segs[1:]
    i = 0
    while i < len(segs) and (segs[i].isdigit()
                             or (len(segs[i]) <= _MAX_SUBFAMILY_SEGMENT
                                 and segs[i] not in DIRECTIVE_MODALS)):
        i += 1
    if i < len(segs) and segs[i] in DIRECTIVE_MODALS:
        return segs[i]
    return None


def directive_title(title: str) -> Optional[str]:
    """The IMPERATIVE a title opens with — else None.

    ⛔ THE `?` GUARD IS A MEASURED EXCLUSION, not a defensive flourish. "Do the asset GET
    and PUT routes constrain the path…?" opens with `Do` and is an INTERROGATIVE: it
    asks rather than instructs, so it suppresses no reading — it invites it. It was the
    single false positive in the 28-title measurement, and naming the class is cheaper
    than carrying the noise.
    """
    text = (title or "").strip()
    if not text or "?" in text:
        return None
    words = [w.strip(",.:;\"'`") for w in text.upper().replace("`", "").split()]
    if not words:
        return None
    if words[0] in DIRECTIVE_MODALS:
        return words[0]
    # TIER 2: an ambiguous verb counts only when it takes an OBJECT PHRASE, which is
    # what makes the title a sentence rather than a noun phrase. See DIRECTIVE_VERBS.
    if (words[0] in DIRECTIVE_VERBS and len(words) > 1
            and words[1] in _DETERMINERS):
        return words[0]
    return None


def classify(node_id: str, type_prefix: str,
             claim_genre: Iterable[str] = CLAIM_GENRE_PREFIXES) -> Optional[dict]:
    """`{copulas, subject}` when the id ARGUES, else None. Pure and side-effect free."""
    if is_machine_minted(node_id):
        return None
    if type_prefix in set(claim_genre):
        return None
    copulas = claim_segments(node_id)
    if not copulas:
        return None
    segs = node_id.split("-")
    cut = segs.index(copulas[0])
    return {"copulas": copulas,
            "subject": "-".join(segs[:cut]),
            "predicate": "-".join(segs[cut:])}


def find_claim_ids(corpus: "factlattice.ParsedCorpus",
                   claim_genre: Iterable[str] = CLAIM_GENRE_PREFIXES) -> List[ClaimFinding]:
    """Every claim-shaped identifier in `corpus`, deterministic in id order."""
    out: List[ClaimFinding] = []
    for node in sorted(corpus.nodes, key=lambda n: (n.source_file or "", n.id)):
        hit = classify(node.id, node.type_prefix, claim_genre)
        if hit is None:
            continue
        subject = hit["subject"] or node.type_prefix
        out.append(ClaimFinding(
            kind=KIND_CLAIM_ID,
            message=(
                f"[{node.id}] states a VERDICT in its identifier "
                f"(`{hit['predicate']}`), not a subject. An id travels further than any "
                f"other field — into edges, `see-also` lists, citations and agent "
                f"bootstraps — so the conclusion arrives everywhere WITHOUT its evidence, "
                f"and a reader can cite it having never opened the node. "
                f"⛔ DO NOT RENAME THIS ONE: citations span corpora and the cost lands "
                f"hardest on the most-cited nodes. The convention is forward-only — name "
                f"the SUBJECT (`{subject}-…`) and carry the verdict in a `claim:` "
                f"property, where it can be qualified, dated and superseded."),
            source_file=node.source_file,
            source_line=(node.line_range or [None])[0],
            node_id=node.id,
            detail={"copulas": hit["copulas"], "subject": hit["subject"],
                    "predicate": hit["predicate"], "type_prefix": node.type_prefix,
                    "has_claim_property": "claim" in node.properties},
        ))
    return out


def find_read_suppressing_names(
        corpus: "factlattice.ParsedCorpus") -> List[ClaimFinding]:
    """Every name that INSTRUCTS instead of naming — the SECOND harm (the maintainer, 2026-08-16).

    ⛔ NO `claim_genre` PARAMETER, and its absence is the design. See the module
    docstring: the genre licence that correctly exempts a PRINCIPLE from arguing would,
    applied here, silence this rule on exactly the nodes it exists to find.
    """
    out: List[ClaimFinding] = []
    for node in sorted(corpus.nodes, key=lambda n: (n.source_file or "", n.id)):
        if is_machine_minted(node.id):
            continue
        in_id = directive_opener(node.id, node.type_prefix)
        in_title = directive_title(node.title)
        if not in_id and not in_title:
            continue
        where = "identifier and title" if (in_id and in_title) else (
            "identifier" if in_id else "title")
        out.append(ClaimFinding(
            kind=KIND_READ_SUPPRESSING,
            message=(
                f"[{node.id}]'s {where} opens with the imperative "
                f"`{(in_id or in_title).title()}`, so the NAME reads as a complete "
                f"instruction. That is the SECOND harm model (ratified 2026-08-16, "
                f"the maintainer): a name that reads as self-evident SUPPRESSES READING THE BODY "
                f"that carries the real guidance — a reader who is busy acts on the name "
                f"and never learns the exception, the scope or the evidence. This is "
                f"INDEPENDENT of citation-travel: nothing has to leave this file for the "
                f"harm to land, and the genre licence that exempts a decision or a "
                f"principle from ARGUING does not apply to INSTRUCTING. "
                f"⛔ DO NOT RENAME AN EXISTING ID — citations span corpora. Forward-only: "
                f"name the SUBJECT the guidance is about and let the body deliver the "
                f"instruction. A TITLE, unlike an id, travels nowhere and may be "
                f"rewritten freely."),
            source_file=node.source_file,
            source_line=(node.line_range or [None])[0],
            node_id=node.id,
            detail={"directive_in_id": in_id, "directive_in_title": in_title,
                    "title": node.title, "type_prefix": node.type_prefix,
                    "harm": "read_suppression",
                    "renameable": bool(in_title and not in_id)},
        ))
    return out


def census(corpus: "factlattice.ParsedCorpus",
           claim_genre: Iterable[str] = CLAIM_GENRE_PREFIXES) -> dict:
    """The TREND WITH A DENOMINATOR — emitted on every run, including a clean one.

    ⛔ THE DENOMINATOR IS THE POINT. "9 claim-shaped ids" is a number nobody can act on;
    "9 of 3,034, and 7 of them in two prefixes" is a direction. And a per-prefix split is
    what makes the convention visible as a convention: a family at 0% is practising it, a
    family at 40% is not, and the second is a conversation rather than a lint.

    Reported even when zero, for the same reason `key_loss` reports its ratio: a rule that
    emits only findings cannot distinguish "this corpus names subjects" from "the rule was
    switched off".
    """
    genre = set(claim_genre)
    total = flagged = licensed = machine = 0
    by_prefix: Dict[str, Dict[str, int]] = {}
    for node in corpus.nodes:
        total += 1
        slot = by_prefix.setdefault(node.type_prefix, {"nodes": 0, "claim_shaped": 0})
        slot["nodes"] += 1
        if is_machine_minted(node.id):
            machine += 1
            continue
        if not claim_segments(node.id):
            continue
        if node.type_prefix in genre:
            licensed += 1
            continue
        flagged += 1
        slot["claim_shaped"] += 1
    # Titles that merely ASSERT: still counted, still not flagged (most are legitimate
    # description). Titles that INSTRUCT are a different predicate and ARE flagged.
    sentence_titles = sum(1 for n in corpus.nodes
                          if set((n.title or "").upper().split()) & CLAIM_COPULA)
    directive_ids = sum(1 for n in corpus.nodes
                        if not is_machine_minted(n.id)
                        and directive_opener(n.id, n.type_prefix))
    directive_titles = sum(1 for n in corpus.nodes
                           if not is_machine_minted(n.id) and directive_title(n.title))
    return {
        "nodes_total": total,
        "claim_shaped_ids": flagged,
        "claim_shaped_rate": round(flagged / total, 4) if total else None,
        "licensed_by_genre": licensed,
        "machine_minted_skipped": machine,
        "sentence_shaped_titles": sentence_titles,
        # [AMENDED 2026-08-16, the maintainer] the second harm, counted separately because it is
        # detected by a different signal and licensed by a different rule.
        "read_suppressing_ids": directive_ids,
        "read_suppressing_titles": directive_titles,
        "by_prefix": {k: v for k, v in sorted(by_prefix.items()) if v["claim_shaped"]},
        "genre_prefixes": sorted(genre),
        "note": (
            "`licensed_by_genre` are claim-shaped ids in families whose genre IS a claim "
            "(a decision, a principle, an invariant) — expected, not a finding. "
            "⛔ THERE ARE TWO HARM MODELS, ratified 2026-08-16 (the maintainer). (1) CITATION "
            "LEAKAGE — an id ARGUES, travels into edges and citations, and the verdict "
            "arrives without its evidence; signal = a copula; that is "
            "`claim_shaped_ids`. (2) READ SUPPRESSION — a name INSTRUCTS, so it reads as "
            "self-evident and the reader never opens the body that carries the real "
            "guidance; signal = an IMPERATIVE in initial position; that is "
            "`read_suppressing_ids` / `read_suppressing_titles`, and it IS flagged. This "
            "field previously read 'a title does not travel into edges or citations, "
            "which is the entire harm model' — that claim was INCOMPLETE and is "
            "superseded: travel is one harm, not the whole of it, and 454 "
            "sentence-shaped titles were being waved through on the incomplete half. "
            "`sentence_shaped_titles` remains COUNTED AND NOT FLAGGED (a title that "
            "asserts is usually just description); a title that INSTRUCTS is flagged, "
            "because a title travels into every find-result row and digest listing, "
            "which is exactly where skimming happens."),
    }


def census_line(stats: Dict[str, object]) -> str:
    return (f"-- claim-shaped ids: {stats['claim_shaped_ids']} of {stats['nodes_total']} "
            f"node(s) argue in their identifier "
            f"({stats['licensed_by_genre']} more licensed by genre) "
            f"— subject in the id, verdict in a `claim:` property")
