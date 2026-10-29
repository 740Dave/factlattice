"""[INV-DECLARED-EXTENT] — every result reports WHAT IT SEARCHED.

    A ZERO WITH AN EXTENT BLOCK IS A FACT. A BARE ZERO IS A RESEARCH TASK.

THE DEFECT THIS CLOSES. [FL-RECALL-ZERO-IS-AMBIGUOUS] measured a `lts_find` zero as
consistent with FIVE distinct states, none of which the response could discriminate:

    1. the corpus genuinely holds no such node  — the only informative case
    2. the corpus id is wrong or misspelled     — a wrong id and an empty corpus present
                                                  IDENTICALLY
    3. the concept is present under a different SPELLING   [FL-RECALL-PARTITION]
    4. the concept is present under a different KEY        [FL-RECALL-TWO-VOCABULARIES]
    5. the target was never indexed at all      [FL-RECALL-INVISIBLE-PRECONDITIONS]

The measured cost is not a wrong answer; it is that the correct response to any zero
becomes "re-query three more ways and then grep" — eleven shell workarounds in one
session where the engine was the declared instrument.

WHY IT IS A SIBLING OF `budget.py` AND NOT A FIELD ON EACH TOOL. `budget.declare()`
exists because "there is more" spelled two ways is a second thing that can be forgotten.
The same argument holds one level up: "here is where I looked" spelled per-tool is a
contract that forks the first time a tool is added. So the vocabulary is fixed HERE and
every layer merges `Extent.to_dict()` — including the layers that answer with NO STORE
SEATED (lint, preview), which is why this module imports nothing from the engine.

THE VOCABULARY IS FIXED (`Extent` below builds it; nobody hand-rolls it):

    corpora_consulted   corpus ids actually searched, sorted
    corpora_skipped     [{corpus, reason}] — searched-eligible but not searched (stale)
    corpora_available   every corpus the store knows, so an empty `consulted` is legible
    nodes_in_scope      the DENOMINATOR: nodes the predicate actually ran against
    keys_consulted      the property/field keys the predicate READ. A zero over
                        `keys_consulted: [status, lifecycle-position]` is a different
                        fact from a zero over `[status]` alone.
    index               {fresh, stale_corpora, built_at{corpus: stamp}} — a node added
                        since the last parse is invisible, and its absence currently
                        reads as "no such decision".
    scope               what the RUN was pointed at (a file, a directory, a corpus id)
    rules_inert         checks that did NOT run, by name. See `lint/core.py`: a headline
                        computed with profile-driven rules inert must not travel as a
                        bare number.
    body_scanned        whether BODY PROSE was read. `keys_consulted` names the structured
                        keys a predicate read; it had no way to say that the largest thing
                        in the corpus — the prose — was not read at all.
    scope_note          THE ROUTING SENTENCE. Where a scope is narrower than the caller's
                        question, this names the tool that covers the remainder.

[FL-PROSE-BLIND] — THE SIXTH AMBIGUOUS ZERO, measured 2026-08-13. `lts_find(text=...)`
reads id, title and STRING property values, and nothing else. The graph index carries no
body text whatsoever, so prose was never searchable, and no field in the response said so.
Measured: a widget corpus returned SIX confident, well-formed hits for its own subject
term, every one of them an `artifact:` property whose value was a markdown FILENAME, over a
corpus holding no subject-matter content at all — `complete: true`, `truncated: false`, and
nothing anywhere indicating the body had never been read.

    A ZERO THAT DOES NOT DECLARE ITS OWN SCOPE IS THE DEFECT, NOT THE RECALL.

⛔ THE CHEAP ANSWER MUST ROUTE, NEVER SATISFY — the rule the digest tier is built on.
`lts_find` keeps its exact structured semantics and gains a POINTER: `body_scanned: false`
plus a `scope_note` naming `lts_find_in_prose` and counting the nodes whose body it did not
read. Widening `lts_find` itself was considered and REJECTED: prose hits arriving free
alongside structured ones erodes the very pressure that makes authors move facts INTO
properties, and that pressure is productive. Reaching for prose stays a deliberate second
act, because that act is itself the signal that the structure failed the caller.

DENOMINATOR DISCIPLINE, APPLIED TO THE TOOL THAT HOSTS IT. The corpora themselves insist
that a claim of `clean` without a denominator is indistinguishable from a check that
inspected nothing. The query layer made exactly that claim on every zero.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from factlattice import registry

from .privacy import PRIVATE_NOTE

# How close a misspelling has to be before the engine will name it. Deliberately
# generous (0.6 is difflib's own default cut) because the cost of a wrong suggestion is
# one glance and the cost of NO suggestion is an indistinguishable zero.
SUGGESTION_CUTOFF = 0.6
MAX_SUGGESTIONS = 3


@dataclass
class Extent:
    """What a response SEARCHED — merged into the payload by `to_dict()`.

    Every field defaults to "nothing claimed", so a layer that can only report part of
    its extent reports that part rather than being unable to declare at all. What it may
    NOT do is omit the block: an absent extent and a searched-nothing extent must never
    look alike, which is the same rule `budget.declare()` enforces one level down.
    """

    corpora_consulted: List[str] = field(default_factory=list)
    corpora_skipped: List[Dict[str, Any]] = field(default_factory=list)
    corpora_available: Optional[List[str]] = None
    nodes_in_scope: int = 0
    keys_consulted: List[str] = field(default_factory=list)
    index_fresh: Optional[bool] = None
    built_at: Dict[str, Any] = field(default_factory=dict)
    root_paths: Dict[str, List[str]] = field(default_factory=dict)
    scope: Optional[Dict[str, Any]] = None
    rules_inert: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    # [FL-PROSE-BLIND]. Tri-state ON PURPOSE: None = this layer does not deal in bodies at
    # all (lint, preview, list_corpora) and claims nothing; False = it could have and did
    # NOT, which is a fact the caller needs; True = it did. A layer that omits the field is
    # silent, and a layer that says False has DECLARED — those must never look alike, which
    # is the same rule the block itself exists to enforce one level up.
    body_scanned: Optional[bool] = None
    scope_note: Optional[str] = None
    # [REGISTRY-SOURCE-EXTENT-KEY] ABSENT unless the read named no corpus AND the registry in use is the
    # built-in EXAMPLE (`"example"`): the one case where a reader could mistake the example corpora for
    # their own. A stable key, never a free-text note.
    registry_source: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "corpora_consulted": sorted(self.corpora_consulted),
            "corpora_skipped": list(self.corpora_skipped),
            "nodes_in_scope": int(self.nodes_in_scope),
            "keys_consulted": list(self.keys_consulted),
            "index": {
                "fresh": self.index_fresh,
                "stale_corpora": sorted(c["corpus"] for c in self.corpora_skipped
                                        if c.get("reason") == "stale" and c.get("corpus")),
                "built_at": dict(self.built_at),
            },
        }
        if self.corpora_available is not None:
            out["corpora_available"] = sorted(self.corpora_available)
        if self.root_paths:
            out["root_paths"] = {k: list(v) for k, v in self.root_paths.items()}
        if self.scope is not None:
            out["scope"] = self.scope
        if self.rules_inert:
            out["rules_inert"] = list(self.rules_inert)
        if self.notes:
            out["notes"] = list(self.notes)
        # `is not None`, NOT truthiness: `body_scanned: False` is the whole point of the
        # field and `if self.body_scanned:` would drop exactly the case that matters.
        if self.body_scanned is not None:
            out["body_scanned"] = bool(self.body_scanned)
        if self.scope_note is not None:
            out["scope_note"] = self.scope_note
        if self.registry_source is not None:
            out["registry_source"] = self.registry_source
        return out


def declare_extent(**kwargs: Any) -> Dict[str, Any]:
    """`{"extent": {...}}`, ready to merge into a response.

    Built in one place so no layer can spell the contract its own way or emit half of it
    and call it declared — the same reason `budget.declare()` exists.
    """
    return {"extent": Extent(**kwargs).to_dict()}


def did_you_mean(unknown: str, known: Iterable[str],
                 *, cutoff: float = SUGGESTION_CUTOFF,
                 limit: int = MAX_SUGGESTIONS) -> List[str]:
    """Near-matches for an unknown identifier, closest first.

    [FL-RECALL-WORKAROUNDS] friction-6: "a misspelled corpus returns nothing rather than
    a did-you-mean. Given that a wrong id and an empty result are indistinguishable, this
    is a cheap and high-value fix."

    Case-insensitive, and a case-only difference is ALWAYS offered regardless of cutoff —
    `Acme-Data-Science` for `acme-data-science` is the single most likely typo and a ratio
    cut has no business filtering it out.
    """
    pool = [str(k) for k in known]
    if not unknown or not pool:
        return []
    lowered = {k.lower(): k for k in pool}
    exact_case_fold = lowered.get(str(unknown).lower())
    ranked = difflib.get_close_matches(str(unknown), pool, n=limit, cutoff=cutoff)
    if exact_case_fold and exact_case_fold not in ranked:
        ranked.insert(0, exact_case_fold)
    return ranked[:limit]


def unknown_corpus(corpus: str, known: Iterable[str],
                   private: Iterable[str] = ()) -> Dict[str, Any]:
    """The not-found response for a corpus id, carrying the discrimination a zero cannot.

    ⛔ THE ERROR STRING NAMES THE SUGGESTION. Some callers read `error` and nothing else,
    so a `did_you_mean` field beside a bare "corpus not found" would be exactly the
    "12 errors sitting beside a prose caveat" failure [FL-RECALL-SCOPE-VERDICT] measures:
    the field callers quote must carry its own caveat.
    """
    # `known` MUST already be the PUBLIC ids only (`LtsEngine._public_corpus_ids`): a private
    # corpus name never appears in an error, a suggestion or a list
    # (ISSUE-PRIVATE-NAMES-IN-ERRORS). One line says why they are absent.
    available = sorted(str(k) for k in known)
    leaked = set(available) & {str(x) for x in private}
    assert not leaked, "unknown_corpus: `known` contains a private corpus id (leak guard)"
    suggestions = did_you_mean(corpus, available)
    error = f"corpus not found: {corpus!r}"
    if suggestions:
        error += f" — did you mean {', '.join(repr(s) for s in suggestions)}?"
    elif available:
        error += f" — loaded corpora: {', '.join(repr(s) for s in available)}"
    else:
        error += f" — NO public corpora are loaded (check the corpus registry: {registry.env_var_name()} or {registry.pointer_display()})"
    error += f". Note: {PRIVATE_NOTE}."
    return {
        "success": False,
        "error": error,
        "corpus": corpus,
        "did_you_mean": suggestions,
        "corpora_available": available,
        "note": PRIVATE_NOTE,
    }


__all__ = ["Extent", "declare_extent", "did_you_mean", "unknown_corpus",
           "SUGGESTION_CUTOFF", "MAX_SUGGESTIONS"]
