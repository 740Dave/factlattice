"""Contradiction check for FactLattice authoring — does this draft disagree with the graph?

WHY THIS EXISTS
    The schema/writer gate (`authoring/schema.py`) closes SILENT LOSS: a key that does
    not parse. Measured over one intensive authoring session, that class cost three
    fields, all recovered.

    The larger class it does NOT touch is WRONG CONTENT — a beautifully-formed node
    carrying a claim the graph already contradicts. Same session, same author: a stale
    figure left inside a node after the finding was revised (91% and 98.40% coexisting
    in ONE node, describing the same quantity), an over-claim retracted the same day,
    and a corroborating anchor nearly presented as independent.

    None of those is a grammar defect. All of them were caught late, by a human or by
    the author re-reading. This module is the attempt to catch them at write time.

WHAT IT IS AND IS NOT
    It is a CANDIDATE GENERATOR with a denominator, not an oracle. Every finding names
    what it compared and where. A contradiction in prose is not mechanically decidable;
    what IS decidable is a narrow set of structural disagreements that empirically
    account for most of the damage. Checks A-F below.

    ⛔ It must never auto-resolve. A checker that silently "fixes" a disagreement picks
    a winner between two claims, which is authoring. It reports; the author decides.

BOUND, STATED AS AN ACTION NOT A CAVEAT
    VERIFY: these checks are lexical over `.lts` source. They do not understand
    negation, scope, or hedging. A draft can pass every check and still be wrong. Use
    the result to direct attention, never to certify.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────────────

_NODE_HEAD = re.compile(r"^#{2,6}\s*\[([A-Z][A-Z0-9_-]*)\]\s*(.*)$")
_PROP = re.compile(r"^-\s+([a-z][a-z0-9_-]*):\s*(.*)$")
_REF = re.compile(r"\[([A-Z][A-Z0-9_-]{2,})\]")
_CODE_SPAN = re.compile(r"`[^`]*`")

# ⛔ CALIBRATION, 2026-08-12. First run on the real 47-node graph produced 42 findings with a
# false-positive rate near 100%. A check that fires on everything is noise, and noise gets
# ignored precisely when it matters. Three defects, each fixed at the point it occurred:
#
#   C  — 85 dangling of which 37 were PURE SCOPE ARTIFACT (deliverables/ not indexed), and the
#         remainder were mostly legitimate refs to specs OUTSIDE this corpus, plus `A-Z` scraped
#         from a REGEX CHARACTER CLASS and `DEC-X` from a worked example. Matching a token
#         without asking what role it plays — inside the tool built to catch that.
#   A  — fired because the author DOCUMENTED A CORRECTION INLINE ("98.40%, not 91.0%"), which is
#         the correct authoring behaviour. The check penalised honesty.
#   D  — fired on a prose key that repeats a ref already minted by a proper `supersedes:` on the
#         same node. True, but not consequential.

# Ids that legitimately live outside this corpus: role-definition invariants, CRP problems,
# spec amendments, silent-loss classes, bulk-import invariants, design principles.
_EXTERNAL_PREFIXES = (
    "INV-", "IH-", "DP", "CRP-", "SL-", "A-", "OP-", "LCB-", "MUT-",
    "SPEC:", "FLFP-", "REGISTRY:", "DS:", "PROC-", "FL-00",
)


def _strip_code(text: str) -> str:
    """Refs inside backticks are ILLUSTRATIVE, not citations — a regex character class, an
    example id, a shell snippet. Blanking them is the difference between a usable check and
    one that reports a regex as a dangling node."""
    return _CODE_SPAN.sub(" ", text)


def _is_external(ref: str) -> bool:
    return ref.startswith(_EXTERNAL_PREFIXES) or len(ref) <= 4

# A figure: percentage, decimal, or grouped integer. Bare years/dates excluded — they
# are provenance, not claims, and they generated pure noise in the first cut.
_FIGURE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+\.\d+%?|\d+%)(?![\w])")
_DATEISH = re.compile(r"^(19|20)\d{2}$")

# Relationship-SHAPED keys. Which of these actually MINT is the parser's business, not
# ours — the set is passed in. Copying the parser's table here would be a second copy
# that drifts, which is the defect this whole effort exists to close.
_RELATIONAL_SHAPE = re.compile(
    r"^(see|refs?|ref-to|relates?|related|links?|linked|cites?|supersed\w*|depends?|"
    r"blocks?|blocked|derives?|derived|implements?|replaces?)([-_]|$)"
)

_OFF_PIPELINE = {"superseded", "withdrawn", "deferred"}
_UNSETTLED = {"proposed", "contingent"}


@dataclass
class Finding:
    check: str
    severity: str  # "block" | "review"
    node_id: str
    message: str
    evidence: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        head = f"[{self.severity.upper()}] {self.check} @ {self.node_id}: {self.message}"
        return "\n".join([head, *(f"      {e}" for e in self.evidence)])


@dataclass
class ParsedNode:
    node_id: str
    title: str
    props: dict[str, str]
    source: str


# ──────────────────────────────────────────────────────────────────────────────


def parse_lts(text: str, source: str = "<draft>") -> list[ParsedNode]:
    """Lexical parse, sufficient for these checks. NOT a substitute for the real parser —
    the writer's authored-vs-parsed reconciliation is what proves the document survives."""
    nodes: list[ParsedNode] = []
    cur: ParsedNode | None = None
    for line in text.splitlines():
        if (h := _NODE_HEAD.match(line)) :
            cur = ParsedNode(h.group(1), h.group(2).strip(), {}, source)
            nodes.append(cur)
        elif cur and (p := _PROP.match(line)):
            cur.props[p.group(1)] = p.group(2).strip()
    return nodes


def index_corpus(root: str | Path) -> dict[str, ParsedNode]:
    idx: dict[str, ParsedNode] = {}
    from factlattice.hashing import safe_read_text, walk_lts_paths
    for f in map(Path, walk_lts_paths(str(root))):
        for n in parse_lts(safe_read_text(str(f), f.name), source=str(f)):
            idx[n.node_id] = n
    return idx


def _figures(text: str) -> set[str]:
    return {
        f for f in _FIGURE.findall(text)
        if not _DATEISH.match(f.replace(",", "").split(".")[0]) or "%" in f or "." in f
    }


def _subject_key(prop_key: str) -> str:
    """A coarse signature for 'what quantity is this about'. Deliberately coarse: it
    over-groups, which produces review noise rather than missed contradictions. Erring
    the other way would make silence meaningless."""
    stop = {
        "the", "a", "an", "is", "are", "was", "were", "and", "or", "not", "of", "to",
        "in", "on", "by", "for", "it", "this", "that", "with", "as", "at", "be",
    }
    toks = [t for t in re.split(r"[-_]", prop_key) if t and t not in stop and not t.isdigit()]
    return " ".join(sorted(toks[:4]))


# ── the checks ────────────────────────────────────────────────────────────────


_PCT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?%)")


def check_a_stale_summary(nodes: list[ParsedNode]) -> list[Finding]:
    """A — THE REVISION SIGNATURE: a summary field left carrying a superseded figure.

    ⛔ The first implementation of this check was WRONG and its own acceptance test
    exposed it. It grouped figures PER PROPERTY, so it never compared the summary field
    against the detail field — which is where the contradiction lived — and it flagged
    `1,481`, `1,497` and `98.40%` co-occurring in one sentence as divergent when they
    are one statement. It fired on the positive control for the wrong reason and fired
    on the negative control too. A check with that false-positive rate is noise, and
    noise gets ignored precisely when it matters.

    The precise signal is a SUBSET relation between properties, not divergence within
    one. When a finding is revised, the detail field is rewritten to carry BOTH values
    ("98.40%, not 91.0%") while the summary field is left carrying ONLY THE OLD ONE.
    So: property P is suspect when its percentages are a strict subset of another
    property's, and that other property carries at least one value P does not.

    Restricted to percentages deliberately. Counts and versions co-occur legitimately
    (`1,481 of 1,497`, `v1.4`); a percentage stated two ways about one node's subject
    is the shape that actually went wrong.
    """
    out: list[Finding] = []
    for n in nodes:
        # ⛔ A property that SUPERSEDES its own earlier figure ("98.40%, not 91.0%",
        # "revised from", "do not quote") is the author doing the right thing. Excluding it
        # is not leniency — including it PENALISED HONESTY and produced a false positive on
        # the one node whose correction had already been made.
        _SUPERSEDING = re.compile(
            r"\b(not|revised|superseded|corrected|was|formerly|do not quote|no longer)\b", re.I)
        pcts = {k: set(_PCT.findall(v)) for k, v in n.props.items()
                if not _SUPERSEDING.search(v)}
        pcts = {k: v for k, v in pcts.items() if v}
        for k1, s1 in pcts.items():
            for k2, s2 in pcts.items():
                if k1 == k2 or not s1 < s2:  # strict subset
                    continue
                out.append(
                    Finding(
                        "A/stale-summary-figure", "review", n.node_id,
                        f"`{k1}` states only {sorted(s1)}, while `{k2}` states "
                        f"{sorted(s2)} — a superset including {sorted(s2 - s1)}. "
                        f"If this finding was revised, `{k1}` may be carrying the "
                        f"superseded number while `{k2}` carries the correction.",
                        [f"{k1}: {n.props[k1][:110]}", f"{k2}: {n.props[k2][:110]}"],
                    )
                )
    return out


def check_b_offpipeline_citations(
    nodes: list[ParsedNode], corpus: dict[str, ParsedNode]
) -> list[Finding]:
    """B — the draft cites a node that is superseded/withdrawn, or one whose confidence
    was assessed to 0.0 (a falsified claim retained as a record of the error)."""
    out: list[Finding] = []
    for n in nodes:
        body = " ".join(n.props.values())
        for ref in set(_REF.findall(body)):
            tgt = corpus.get(ref)
            if not tgt or ref == n.node_id:
                continue
            lc = tgt.props.get("lifecycle-position", "").strip().lower()
            falsified = any(
                k.startswith("confidence") and re.search(r"\b0\.0\b", v)
                for k, v in tgt.props.items()
            )
            ack = re.search(rf"{re.escape(ref)}.{{0,160}}(supersed|falsifi|retract|no longer|corrected)",
                            body, re.I | re.S)
            if (lc in _OFF_PIPELINE or falsified) and not ack:
                out.append(
                    Finding(
                        "B/off-pipeline-citation", "review", n.node_id,
                        f"cites [{ref}] which is "
                        f"{'lifecycle=' + lc if lc in _OFF_PIPELINE else 'confidence 0.0 (falsified)'}"
                        f", with no acknowledgement nearby. A superseded node reads "
                        f"perfectly plausible standing alone.",
                        [f"target: {tgt.source}"],
                    )
                )
    return out


def check_c_dangling(nodes: list[ParsedNode], corpus: dict[str, ParsedNode]) -> list[Finding]:
    """C — reference to a node id that does not exist. Only run with the FULL corpus in
    scope; a narrow scope invents phantom dangles."""
    known = set(corpus) | {n.node_id for n in nodes}
    out: list[Finding] = []
    for n in nodes:
        body = _strip_code(" ".join(n.props.values()))
        missing = sorted({r for r in _REF.findall(body)
                          if r not in known and not _is_external(r)})
        if missing:
            out.append(Finding("C/dangling-reference", "review", n.node_id,
                               f"{len(missing)} reference(s) resolve to nothing", missing))
    return out


def check_d_nonminting_relational_keys(
    nodes: list[ParsedNode], minting_keys: frozenset[str]
) -> list[Finding]:
    """D — a key that LOOKS like an edge and is not one. The property still parses, so
    you get a field and no edge, and nothing complains. Live instance: `relates-to` used
    in the very node documenting that relationship-shaped properties mint nothing."""
    out: list[Finding] = []
    for n in nodes:
        already = {r for k in n.props if k in minting_keys
                   for r in _REF.findall(n.props[k])}
        offenders = []
        for k, v in n.props.items():
            if not (_RELATIONAL_SHAPE.match(k) and k not in minting_keys):
                continue
            refs = set(_REF.findall(_strip_code(v))) - already
            if refs:
                offenders.append(k)
        if offenders:
            out.append(
                Finding("D/relational-key-mints-nothing", "block", n.node_id,
                        "key reads as a relationship and contains node refs, but is not "
                        "in the parser's minting set — the refs will NOT become edges",
                        [f"{k}  (minting keys: {', '.join(sorted(minting_keys)) or 'NONE SUPPLIED'})"
                         for k in offenders])
            )
    return out


def check_e_lifecycle_regression(
    nodes: list[ParsedNode], corpus: dict[str, ParsedNode]
) -> list[Finding]:
    """E — a `validated` node whose declared dependency is still proposed/contingent.
    Validation cannot be stronger than what it rests on."""
    out: list[Finding] = []
    for n in nodes:
        if n.props.get("lifecycle-position", "").strip().lower() != "validated":
            continue
        for k in ("depends-on", "stage-evidence"):
            for ref in _REF.findall(n.props.get(k, "")):
                tgt = corpus.get(ref)
                if tgt and tgt.props.get("lifecycle-position", "").strip().lower() in _UNSETTLED:
                    out.append(
                        Finding("E/lifecycle-regression", "review", n.node_id,
                                f"declared validated, but depends on [{ref}] which is "
                                f"{tgt.props['lifecycle-position']}",
                                [f"via `{k}`  ({tgt.source})"])
                    )
    return out


def check_f_corpus_figure_divergence(
    nodes: list[ParsedNode], corpus: dict[str, ParsedNode]
) -> list[Finding]:
    """F — the draft restates a figure the graph already carries for the same subject,
    with a different value. Fires only where the draft explicitly cites the node it
    disagrees with; unrestricted cross-corpus matching produced pure noise."""
    out: list[Finding] = []
    for n in nodes:
        for k, v in n.props.items():
            subj = _subject_key(k)
            mine = _figures(v)
            if not (mine and subj):
                continue
            for ref in set(_REF.findall(v)):
                tgt = corpus.get(ref)
                if not tgt:
                    continue
                for tk, tv in tgt.props.items():
                    if _subject_key(tk) != subj:
                        continue
                    theirs = _figures(tv)
                    if theirs and mine and not (mine & theirs):
                        out.append(
                            Finding("F/corpus-figure-divergence", "review", n.node_id,
                                    f"`{k}` cites [{ref}] and states {sorted(mine)}, "
                                    f"where that node's `{tk}` states {sorted(theirs)} "
                                    f"about the same subject ({subj!r})",
                                    [f"target: {tgt.source}"])
                        )
    return out


# ──────────────────────────────────────────────────────────────────────────────


def check(
    draft_text: str,
    corpus_root: str | Path | list[str | Path],
    minting_keys: frozenset[str] = frozenset(),
) -> dict:
    """Run A-F. Returns findings plus the DENOMINATORS each check examined — a result
    without them is indistinguishable from a check that inspected nothing."""
    nodes = parse_lts(draft_text)
    # ⛔ ACCEPT MULTIPLE ROOTS. A narrow scope INVENTS phantom dangles — measured
    # 2026-08-12: indexing only `corpora/` reported 85 dangling where the full set has
    # 48, and every one of the 25 that survived calibration was a `[DELIV-*]` id living
    # in `deliverables/`. The engine itself warns about this (114 dangling at subset
    # scope vs 56 at full). Index everything the corpus is actually made of.
    roots = corpus_root if isinstance(corpus_root, list) else [corpus_root]
    corpus: dict[str, ParsedNode] = {}
    for r in roots:
        corpus.update(index_corpus(r))

    findings = [
        *check_a_stale_summary(nodes),
        *check_b_offpipeline_citations(nodes, corpus),
        *check_c_dangling(nodes, corpus),
        *check_d_nonminting_relational_keys(nodes, minting_keys),
        *check_e_lifecycle_regression(nodes, corpus),
        *check_f_corpus_figure_divergence(nodes, corpus),
    ]
    return {
        "findings": findings,
        "blocking": [f for f in findings if f.severity == "block"],
        "denominators": {
            "draft_nodes": len(nodes),
            "draft_properties": sum(len(n.props) for n in nodes),
            "corpus_nodes_indexed": len(corpus),
            "corpus_files": len({n.source for n in corpus.values()}),
            "corpus_roots": [str(r) for r in roots],
            "minting_keys_supplied": len(minting_keys),
        },
        "bound": (
            "VERIFY: lexical checks only — no understanding of negation, scope or "
            "hedging. Passing all six does NOT mean the draft is correct. "
            + ("⛔ minting_keys was EMPTY, so check D was INERT."
               if not minting_keys else "")
        ),
    }
