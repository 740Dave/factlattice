"""
LTS authoring schema — DRAFT for review, 2026-08-12, analyst.

WHAT THIS IS
    A pydantic model of an .lts document, and a writer built on it. The point is NOT
    normalisation. The point is that an illegal property key FAILS TO CONSTRUCT.
    Same principle as a test library's typed operands: make the wrong thing
    unrepresentable rather than detectable.

THE ARCHITECTURAL DECISION THIS ENCODES (the maintainer raised it; it decides everything else)
    A validator that an agent calls before writing is a SEAM. The agent can skip it,
    can route around a refusal, or can validate and then post-edit — in which case the
    check ran on different bytes than the ones that landed. That last shape is the
    defect we spent 2026-08-11/12 on.

    Therefore: THIS MODULE IS THE SOLE WRITER. An authoring agent emits a structured
    Document (JSON conforming to these models); it never holds the file bytes. It
    cannot skip the gate because there is no other path; cannot work around a refusal
    because a refusal means no file exists; cannot post-edit because its output is the
    writer's INPUT.

THE GATE THAT MATTERS IS NOT THE GRAMMAR
    Every guard written for this so far — including mine, five times — is an
    enumeration of REMEMBERED failure modes, so it inherits the shape of the memory
    rather than the shape of the failure. My grep was structurally blind to
    glyph-prefixed keys because I wrote it against the defect I was thinking about.

    The grammar-independent invariant is:

        EVERY KEY AUTHORED MUST APPEAR IN THE PARSED `properties`.

    Round-trip the emitted text through the REAL parser (lts_preview) and reconcile.
    That needs no knowledge of the grammar, and it catches SL-13, SL-14, digit-initial,
    and SL-23 — the class nobody has named yet. See `write_document`.

⛔ NON-GOAL: this module MUST NOT re-implement or hardcode the parser's tables.
    The set of edge-minting property keys lives in the parser (a closed table). Copying
    it here creates a second copy that drifts — the exact defect flagged in
    basis_fingerprint.json, where a superseded gate framing sat in an authoritative-
    looking file. Read it from the parser, or discover it by round-trip. See
    `EdgeKeyRegistry`.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

# ──────────────────────────────────────────────────────────────────────────────
# 1. THE PRIMITIVE THAT DOES THE WORK
# ──────────────────────────────────────────────────────────────────────────────

# SPEC: a property key must match ^[a-z][a-z0-9_-]*$ — begins with a lowercase
# letter, thereafter lowercase / digit / hyphen / underscore ONLY.
#
# This one regex subsumes all five normalisation rules, because it does not
# enumerate violations. Uppercase, pictographs, leading digits, leading spaces —
# each fails the same anchor. Violation forms are open-ended; the rule is not.
PropertyKey = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_-]*$", min_length=1, max_length=120),
]

NodeId = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_-]*$", min_length=2)]

# A DOCUMENT-ROOT id, which may carry a doctype: `ACME-DECISIONS` or `SPEC:ACME-DECISIONS`.
#
# ⛔ `NodeId` FORBIDS THE COLON, so the document-root id could not be spelled in the form
# every corpus in the ecosystem actually authors — measured 2026-08-13: `Frontmatter(id=
# "SPEC:X-ROOT")` raised ValidationError, so no document this writer could emit was able
# to carry a doctype at all. The doctype is part of the parser's heading grammar
# (`parser._heading_id`: `[DOCTYPE:ID]`, ONE bracket), not a decoration, so a writer that
# cannot express it cannot write a PROFILE, a DELTA or a REGISTRY.
DocumentRootId = Annotated[
    str, StringConstraints(pattern=r"^(?:[A-Z][A-Z0-9_]*:)?[A-Z][A-Z0-9_-]*$", min_length=2)
]

# A value may be a scalar, a list (P3/P4), or a mapping (P5, e.g. `confidence`).
# ⚠ OPEN QUESTION 1 — this union is the loosest thing in the model. A dict value is
#    only legal for keys the profile declares as mapping-shaped. Enforcing that needs
#    the profile; see OPEN QUESTIONS at the foot.
PropertyValue = str | list[str] | dict[str, Any]


# ── the coercion boundary ─────────────────────────────────────────────────────
#
# DESIGN PRINCIPLE (the maintainer, 2026-08-12): a type system should make CATEGORY ERRORS
# unconstructable and TYPOS COSTLESS. An earlier draft of this file rejected on key
# case, which conflated the two — it prevented a wrong keystroke, not a wrong claim.
#
# That is not merely unhelpful, it is DANGEROUS: an over-strict gate creates an
# incentive to route around it, and a guard nobody enters is the exact failure this
# whole effort exists to close. The author must be able to speak in a natural voice.
#
# THE LINE: normalise where the mapping is UNAMBIGUOUS; refuse only where correcting
# requires a CHOICE. Case in an identifier carries no information, so lowercasing
# invents nothing. Choosing a prefix for a digit-initial key invents a name.
#
# ⛔ AND EVERY COERCION IS RECORDED. Silent accommodation teaches the author nothing
#    and becomes a transformation that can be wrong without announcing it.

_LEADING_NON_ALNUM = re.compile(r"^[^A-Za-z0-9]+")
_MD_EMPHASIS = re.compile(r"^\*{1,3}|\*{1,3}$|^_{1,3}|_{1,3}$")
_INNER_WS = re.compile(r"\s+")


def _coerce_key(raw: str) -> tuple[str, list[str]]:
    """Return (normalised_key, coercions_applied). Pure; no rejection here."""
    applied: list[str] = []
    k = raw

    if k != k.strip():
        k = k.strip(); applied.append("stripped surrounding whitespace")
    if _MD_EMPHASIS.search(k):
        k = _MD_EMPHASIS.sub("", k); applied.append("removed markdown emphasis wrapper")
    if _LEADING_NON_ALNUM.match(k):
        # pictographs, bullets, stray symbols before the key
        k = _LEADING_NON_ALNUM.sub("", k)
        applied.append("stripped leading symbol/pictograph from the key region")
    if _INNER_WS.search(k):
        k = _INNER_WS.sub("-", k); applied.append("spaces -> hyphens")
    if k != k.lower():
        k = k.lower(); applied.append("lowercased (emphasis is not machine-readable)")

    return k, applied


class Property(BaseModel):
    """One `- key: value` line.

    Constructed PERMISSIVELY: an author may write `- ⛔ What DOES Transfer:` and get a
    legal `what-does-transfer`, with the coercion recorded. Construction fails only
    where the input is genuinely AMBIGUOUS.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: PropertyKey
    value: PropertyValue
    coercions: tuple[str, ...] = ()
    key_as_authored: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _accommodate_then_constrain(cls, data: Any) -> Any:
        if not isinstance(data, dict) or not isinstance(data.get("key"), str):
            return data
        raw = data["key"]
        fixed, applied = _coerce_key(raw)

        # ⛔ NOT gated on `applied`. An earlier draft only explained itself when a
        # coercion had run, so the DIGIT-INITIAL case — which needs no coercion and is
        # the one refusal that genuinely requires an authoring decision — fell through
        # to a bare `String should match pattern`. The most important refusal had the
        # least useful message. Caught 2026-08-12 by the acceptance test.
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", fixed):
            if fixed and fixed[0].isdigit():
                hint = (
                    " The key begins with a DIGIT. Legalising it means INVENTING a "
                    "prefix (`doc-003-…`, `corpus-003-…`) and choosing one is AUTHORING, "
                    "not normalisation. Name the field yourself — this is the one class "
                    "the boundary deliberately will not decide for you."
                )
            elif not fixed:
                hint = (
                    " Nothing survived coercion — the key region held no alphanumeric "
                    "character at all. There is no intent here to infer."
                )
            else:
                hint = (
                    " Remaining illegal characters are not in the unambiguous-coercion "
                    "set, so correcting would require guessing what you meant."
                )
            was = f"key {raw!r}" + (f" -> {fixed!r}" if applied else "")
            raise ValueError(f"{was} is not a legal key.{hint}")

        return {
            **data,
            "key": fixed,
            "coercions": tuple(applied),
            "key_as_authored": raw if applied else None,
        }

    @field_validator("value")
    @classmethod
    def _value_is_never_normalised(cls, v: PropertyValue) -> PropertyValue:
        # ⛔ Deliberately a no-op. Recorded so nobody adds normalisation here later.
        # Values legitimately hold ExampleStatusNotMet, FIXED_12, INT8_VECTOR,
        # sha256 digests and pictographs. A writer that mis-identifies the key boundary
        # corrupts content SILENTLY, which is strictly worse than the defect it fixes.
        return v


# ──────────────────────────────────────────────────────────────────────────────
# 2. EDGE-BEARING KEYS — read from the parser, never copied
# ──────────────────────────────────────────────────────────────────────────────


class EdgeKeyRegistry(BaseModel):
    """The closed set of property keys that MINT AN EDGE.

    ⛔ Populated by READING THE PARSER at startup, never by a literal here.

    Why this matters concretely: on 2026-08-12 I used `relates-to` in the very node
    documenting that relationship-shaped properties mint nothing. It is not in the
    table; it minted nothing; the node's edge count was 4 where I intended 12.
    Converting to `see-also` fixed it. A relationship-SHAPED key that is not an
    edge-MINTING key is the most expensive silent loss in the format, because the
    property still parses — you get a field and no edge, and nothing complains.
    """

    model_config = ConfigDict(frozen=True)

    minting_keys: frozenset[str]
    source: str = Field(description="how the table was obtained; must not be 'literal'")

    @field_validator("source")
    @classmethod
    def _never_a_literal(cls, v: str) -> str:
        if v == "literal":
            raise ValueError(
                "the edge-key table must be read from the parser, not copied. "
                "A second copy drifts from the first and nothing announces it."
            )
        return v


# ──────────────────────────────────────────────────────────────────────────────
# 3. NODE
# ──────────────────────────────────────────────────────────────────────────────


class Lifecycle(StrEnum):
    """⚠ OPEN QUESTION 2 — this enum belongs to the DOMAIN PROFILE, not to this file.
    Hardcoding it here is the same drift hazard as the edge table. Shown inline only
    to make the draft readable; the real model loads it from the profile."""

    proposed = "proposed"
    committed = "committed"
    validated = "validated"
    contingent = "contingent"
    superseded = "superseded"
    deferred = "deferred"
    withdrawn = "withdrawn"


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: NodeId
    title: str = Field(min_length=8)
    properties: list[Property]

    # ⛔ NO NODE-SIZE CAP, AND NO KEY-LENGTH ADVISORY. Both were in an earlier draft
    # and both are removed (the maintainer, 2026-08-12).
    #
    # A size cap does not merely risk silent failure — it creates an incentive to DROP
    # CONTENT TO FIT THE THRESHOLD, which is content loss wearing compliance's clothes.
    # And a key-length advisory contradicts a principle we already hold: a long
    # descriptive key is a FEATURE for a reader with no memory, because it states the
    # claim before the value is reached. Penalising it optimises against the cold
    # reader, who is the only reader that matters.
    #
    # If a node is really three nodes, that is a JUDGEMENT about subject matter — an
    # intelligence's job, surfaced in review. It is not a property count.

    @field_validator("properties")
    @classmethod
    def _keys_unique(cls, v: list[Property]) -> list[Property]:
        seen: dict[str, int] = {}
        for p in v:
            seen[p.key] = seen.get(p.key, 0) + 1
        dupes = {k: n for k, n in seen.items() if n > 1}
        if dupes:
            raise ValueError(
                f"duplicate property keys {dupes}. A later duplicate silently REPLACES "
                f"the earlier one in `properties`, so the first value is lost with no "
                f"diagnostic."
            )
        return v

    @model_validator(mode="after")
    def _evidence_required_for_validated(self) -> Node:
        """A `validated` claim without a verification handle reads as asserted-not-checked."""
        keys = {p.key for p in self.properties}
        lifecycle = next(
            (p.value for p in self.properties if p.key == "lifecycle-position"), None
        )
        if lifecycle == "validated" and "stage-evidence" not in keys:
            raise ValueError(
                f"[{self.node_id}] is lifecycle-position: validated but carries no "
                f"`stage-evidence`. Every validated claim carries a verification handle "
                f"or it reads as asserted-not-checked."
            )
        return self

    def coercion_report(self) -> list[str]:
        """What the boundary CHANGED. Never silent — the author must see their own
        input rewritten, or the accommodation becomes a transformation that can be
        wrong without announcing it."""
        return [
            f"[{self.node_id}] {p.key_as_authored!r} -> {p.key!r} :: {'; '.join(p.coercions)}"
            for p in self.properties
            if p.coercions
        ]


# ──────────────────────────────────────────────────────────────────────────────
# 4. DOCUMENT
# ──────────────────────────────────────────────────────────────────────────────


class Frontmatter(BaseModel):
    """SPEC §1.5 frontmatter.

    ⛔ THE INTERSECTION WAS EMPTY. This model was `extra="forbid"` over
    `{title, id, domain, author, date, status}` while the shipped linter's V9 required
    `{scope, audience, boundary}` — three keys this model REFUSED. So the sanctioned
    writer could not emit a single document that passed the sanctioned linter, and
    neither side could see it, because each was internally consistent and no test
    crossed them. Measured with probes on 2026-08-13.

    THE FIX IS BOTH HALVES, and it matters which way each goes:
      * the §1.5 keys are now REQUIRED here, imported from `factlattice.constants` so
        they cannot drift from the rule that checks them;
      * `extra` is now `allow`, because frontmatter is an OPEN vocabulary — live corpora
        author `not-this`, `confidentiality`, `artifact-readiness`, `created`. Forbidding
        them bought no safety (frontmatter keys mint no graph, so a stray key loses
        nothing) and cost the writer every document with a house convention. The strict
        gate belongs on PROPERTY KEYS, where a bad key silently loses a field; here it
        was strictness pointed at the one place it could only do harm.
    """

    model_config = ConfigDict(extra="allow")

    title: str
    id: DocumentRootId
    domain: str
    author: str
    date: str
    # SPEC §1.5, required. Named from the ONE constant the linter's V9 also reads.
    scope: str
    audience: str
    boundary: str
    status: str | None = None


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frontmatter: Frontmatter
    document_tag: Literal["SPEC", "PROFILE", "DELTA", "REGISTRY"] | None = None
    section: str
    nodes: list[Node]

    def authored_keys(self) -> dict[str, set[str]]:
        """The reconciliation baseline: what the AUTHOR believes they wrote."""
        return {n.node_id: {p.key for p in n.properties} for n in self.nodes}


# ──────────────────────────────────────────────────────────────────────────────
# 5. THE WRITER — the only path to disk, and the only place the gate lives
# ──────────────────────────────────────────────────────────────────────────────


class WriteRefused(Exception):
    """No file was created. The refusal IS the outcome, not an obstacle."""


def write_document(doc: Document, path: str, preview_fn, corpus: str | None = None, *,
                   proposer: str | None = None, approver: str | None = None,
                   reason: str | None = None, via: str | None = None,
                   corpora: list | None = None, engine_head: str | None = None,
                   ledger_roots: list | None = None, corpora_error: str | None = None) -> dict:
    """Render, ROUND-TRIP THROUGH THE REAL PARSER, reconcile, and only then write.

    `preview_fn` is the live `lts_preview`. The parser is the authority on what the
    document became; this module is the authority on what the author intended. The
    gate is the comparison, not either one alone.

    ⛔ THE LOAD-BEARING ASSERTION — grammar-independent by design:
           every key in `doc.authored_keys()` appears in the parsed `properties`.
       Not "no known violation found". Not "lint is clean". Reconciliation.
    """
    text = _render(doc)                      # pure function, no I/O
    result = preview_fn(content=text, corpus=corpus)

    parsed = {
        n["id"]: set(n.get("properties", {}))
        for n in result.get("became", {}).get("nodes", [])
    }
    authored = doc.authored_keys()

    dropped = {
        nid: sorted(keys - parsed.get(nid, set())) for nid, keys in authored.items()
    }
    dropped = {k: v for k, v in dropped.items() if v}

    missing_nodes = [nid for nid in authored if nid not in parsed]

    # ⛔ THE DOCTYPE IS RECONCILED TOO, for exactly the reason the keys are. `document_tag`
    # is a claim about what the file IS, and it was silently false for every PROFILE this
    # writer emitted (a second bracket the parser never read as a doctype). Reconciling
    # authored-vs-parsed is the only check that could have caught it — a renderer test
    # comparing bytes to bytes would have passed, because the bytes were exactly what the
    # renderer intended and the renderer was wrong.
    became_tag = (result.get("became", {}) or {}).get("document_tag")
    if doc.document_tag and became_tag != doc.document_tag:
        raise WriteRefused(
            f"NOT WRITTEN. authored `document_tag={doc.document_tag!r}` but the parser "
            f"read {became_tag!r}. A document registered as a {doc.document_tag} while "
            f"carrying no {doc.document_tag} tag is PARSED AS CONTENT: its declarations "
            f"answer content queries and appear as dangle orphans, and only "
            f"`lts_profile_audit` can see it — `lts_lint` and `lts_find` are both silent."
        )

    if dropped or missing_nodes:
        raise WriteRefused(
            f"NOT WRITTEN. authored-vs-parsed reconciliation failed.\n"
            f"  nodes that minted nothing : {missing_nodes}\n"
            f"  keys that did not parse   : {dropped}\n"
            f"  parser's own LOST channel : {result.get('lost')}\n"
            f"⚠ The LOST channel may be EMPTY while keys are missing — measured "
            f"2026-08-12: a dropped key landed in the `ignorable` bucket alongside "
            f"blank lines, and preview reported lost:0 at 100% coverage. THE "
            f"RECONCILIATION IS THE GATE; the parser's own loss report is not."
        )

    # Profile currency — presence is not currency. `available: true` on a stale
    # profile auto-discovered from a scratch directory certifies against the wrong
    # vocabulary. Measured 2026-08-12: lint resolved a 390-line stale copy over the
    # 453-line canonical one.
    prof = result.get("resolution", {}).get("profile", {})
    if not prof.get("available"):
        raise WriteRefused(
            "NOT WRITTEN. No profile in scope — every profile-driven rule was INERT, "
            "so a clean result means nothing was checked."
        )

    # LF, always. The SHARED locked + atomic write step ([ISSUE-WRITE-ATOMICITY]):
    # imported here because edit.py imports this module.
    from .edit import EditRefused, write_whole_file
    try:
        written = write_whole_file(path, text, proposer=proposer, approver=approver, reason=reason,
                                   via=via, corpora=corpora, engine_head=engine_head,
                                   ledger_roots=ledger_roots, corpora_error=corpora_error)
    except EditRefused as e:
        raise WriteRefused(f"NOT WRITTEN. {e}") from None
    ledger = getattr(written, "ledger", None)

    result = {
        "written": path,
        "nodes": len(doc.nodes),
        "document_tag": became_tag,
        "keys_authored": sum(len(k) for k in authored.values()),
        "keys_parsed": sum(len(v) for v in parsed.values()),
        # ⛔ WAS `n.advisories()`, A METHOD THAT DOES NOT EXIST ON `Node`. Every REFUSAL
        # path returned before reaching this line, so the writer's refusals were tested
        # and exercised while its SUCCESS path raised AttributeError and had, on the
        # evidence, never once run to completion. A gate whose accept branch is dead is a
        # gate that has only ever been observed rejecting — which is precisely how it kept
        # a good reputation while being unusable. The intended report is the coercion one.
        "coercions": [c for n in doc.nodes for c in n.coercion_report()],
        "ledger": ledger,                      # [NODE-CHANGE-LEDGER] always present
    }
    warns = []
    if ledger and ledger.get("status") == "failed":    # loud: the file landed, the audit record did not
        from .. import ledger as _ledger
        warns.append(f"WRITTEN, BUT NOT LEDGERED {_ledger.describe(ledger)}")
    from .edit import oob_warnings
    warns += oob_warnings(ledger, path)                # a chain break: an edit outside the writers
    if warns:
        result["warnings"] = warns
    return result


def _render_value(v: PropertyValue, indent: str = "  ") -> str:
    """P1 scalar / P3 multi-value / P5 mapping. Values pass through UNCHANGED."""
    if isinstance(v, str):
        return f" {v}"
    if isinstance(v, list):
        return "\n" + "\n".join(f"{indent}- {item}" for item in v)
    if isinstance(v, dict):
        # P5: key-only head, two-space indent (SPEC §7.1)
        return "\n" + "\n".join(f"{indent}{k}: {val}" for k, val in v.items())
    raise TypeError(f"unrenderable value type {type(v)!r}")


def _document_heading(title: str, root_id: str, document_tag: str | None) -> str:
    """The h1 the parser will actually read as a doctyped document root.

    ⛔ THE OLD RENDERER EMITTED A SECOND BRACKET — `# Title [ID] [PROFILE]` — and the
    parser's heading grammar (`parser._heading_id`) does not read that as a doctype at
    all: FIRST id-like bracket wins, so the id was `ID`, `document_tag` came back None,
    and `is_profile_document()` was False. Measured 2026-08-13.

    ⇒ So `write_document(document_tag="PROFILE")` MANUFACTURED the exact defect
      [FL-RECALL-INVISIBLE-PRECONDITIONS] precondition-2 records as live: a file
      registered as a domain profile, carrying no PROFILE tag, therefore PARSED AS
      CONTENT — 56 declaration nodes answering content queries and appearing as dangle
      orphans. The sanctioned surface produced the defect it exists to prevent, which
      makes it worse than no surface: it is a trap with a good reputation.

    The doctype is a SEGMENT INSIDE THE ONE BRACKET: `[PROFILE:X-PROFILE]`. A root id
    that already carries a doctype is respected as authored; a conflicting
    `document_tag` is a refusal, not a silent overwrite — the two are independent
    declarations of the same fact and disagreeing about it is exactly the class the
    engine's own `registered_profile_not_tagged_profile` check exists to catch.
    """
    authored_doctype, bare = (root_id.split(":", 1) if ":" in root_id
                              else (None, root_id))
    if document_tag and authored_doctype and authored_doctype != document_tag:
        raise WriteRefused(
            f"NOT WRITTEN. The frontmatter id declares doctype {authored_doctype!r} and "
            f"`document_tag` declares {document_tag!r}. These are two independent "
            f"declarations of one fact; a writer that silently picks one produces a "
            f"document whose registration and whose tag disagree — which is the exact "
            f"drift `lts_profile_audit` exists to find. Say it once.")
    doctype = document_tag or authored_doctype
    bracket = f"[{doctype}:{bare}]" if doctype else f"[{bare}]"
    return f"# {title} {bracket}"


def _render(doc: Document) -> str:
    fm = doc.frontmatter
    out: list[str] = ["---"]
    for k, val in fm.model_dump(exclude_none=True).items():
        out.append(f"{k}: {val}")
    out += ["---", ""]

    out += [_document_heading(fm.title, fm.id, doc.document_tag), "",
            f"## {doc.section}", ""]

    for n in doc.nodes:
        out.append(f"### [{n.node_id}] {n.title}")
        for p in n.properties:
            out.append(f"- {p.key}:{_render_value(p.value)}")
        out.append("")

    return "\n".join(out).rstrip("\n") + "\n"  # single trailing LF


# ⛔ REQUIRED. `from __future__ import annotations` defers every annotation to a string,
# so pydantic must resolve `PropertyKey`/`PropertyValue` from the module namespace at
# build time. Without an explicit rebuild the models load fine and then raise
# PydanticUserError at the FIRST construction — i.e. the failure is deferred past import
# and looks like a validation error when it is not one. Caught 2026-08-12 by a test whose
# refusal branch reported that error as a successful refusal.
for _m in (Property, Node, Frontmatter, Document, EdgeKeyRegistry):
    _m.model_rebuild()


# ──────────────────────────────────────────────────────────────────────────────
# OPEN QUESTIONS — the ones writing this surfaced. I should not decide these alone.
# ──────────────────────────────────────────────────────────────────────────────
#
# 1. VALUE UNION. `str | list | dict` is the loosest thing here. A dict is legal only
#    for profile-declared mapping keys (`confidence`). Should the model reject a dict
#    on an undeclared key? Leaning YES — an accidental mapping parses to something
#    unintended and nothing complains.
#
# 2. WHERE THE VOCABULARY LIVES. Lifecycle and importance enums belong to the domain
#    profile, and the edge-key table belongs to the parser. NEITHER should be literal
#    in this file. That means the model cannot be fully static — it is parameterised
#    by a profile at construction. Adds a dependency; removes a drift hazard. I think
#    that trade is obviously right, but it is a real architectural commitment.
#
# 3. NODE SIZE — advisory or hard? My own nodes run to 25 properties. Either they are
#    too big and the cap should bite, or 20 is wrong. I do not think I can answer this
#    without being the interested party; a cap I set to accommodate my own habit is
#    not a cap.
#
# 4. RELATIONSHIP-SHAPED KEYS THAT MINT NOTHING. Should `relates-to` be REJECTED
#    outright, given it looks like an edge and is not one? Rejecting is safer;
#    it also forbids a legitimate prose field that merely reads relational.
#
# 5. WHO OWNS THIS. It is a FactLattice tool, drafted from a deployment need, living
#    for now in a deployment-side proposals directory OUTSIDE every registered corpus
#    root. It belongs in factlattice-deployment. Not moved unilaterally.
#
# 6. RETIREMENT. If this lands, the KEY HYGIENE rules added to the spec-editing agent definition
#    on 2026-08-12 MUST be removed. Two copies of one enforcement drift, and the copy
#    written in prose for an intelligence is the one that will drift first.
