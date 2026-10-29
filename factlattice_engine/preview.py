"""PREVIEW — the authoring feedback loop [FL-PREVIEW].

    An Intelligence composes an `.lts`, and nothing surfaces the parse result back
    into its context. It cannot see what its document BECAME.

That is the root cause this module closes, and it is NOT the lint's job. Lint answers
*"does this violate a rule?"*. Preview answers *"what did my document actually become,
and what did it lose?"*. The distinction is measurable, not rhetorical. Measured on the
committed `41-example-process-launcher.lts` (frozen at `tests/fixtures/preview/`):

    lts_lint        ->  0 errors, exit 0; 5 warnings, ALL of them on lines 4-10
    the document    ->  its own root `[SPEC:ACME-INTELLIGENCE-HOST]` (line 3) mints NO
                        node, and `[ACME-IH-EDGES]` — a section whose stated purpose is
                        "Decompositional edges this realizes" — yields 0 properties and
                        0 edges, its three authored `- [DX-*]:` lines producing neither
    lts_lint on those lines  ->  ZERO findings of any kind

Errors catch NOISY failures. Only the GRAPH RESULT plus LINE ACCOUNTING catch SILENT
ones, and the silent ones are the whole problem.

THE PROTOCOL THIS SERVES
------------------------
    a) compose an .lts draft (a new file, or edits to an existing one)
    b) submit to lts-engine  ->  (graph result, losses, errors)
    c) REVIEW the result, edit the draft, repeat (b)
    d) when correct, WRITE into a registered corpora dir, which lts-engine polls

Three consequences are designed in, not bolted on:

  * CONTENT MODE IS PRIMARY. During (a)-(c) the draft exists only in the authoring
    Intelligence's context — it is on no disk the engine watches. A path-only API
    cannot serve this loop at all, so `content=` is the first-class entry point and
    `path=` is the convenience wrapper that reads bytes and calls it.

  * THE PREVIEW RESOLVES AGAINST THE TARGET CORPUS. Step (b) happens outside any
    corpus; step (d) lands inside one. Reference integrity is a WHOLE-CORPUS property
    — measured on this corpus, a subset scope reported 114 dangling where the full set
    has 56, i.e. 58 phantom dangles. If (b) previews in isolation while (d) lands in a
    corpus, the two disagree and the author learns to ignore the tool. So `corpus=`
    asks the real question: *what would this content become IF it landed in corpus X*.

  * AN EDIT PREVIEWS AS A REPLACEMENT. If a draft that revises an existing file were
    resolved as an ADDITION, every node the file already defines would report a
    duplicate-id collision — noise shaped exactly like a real defect. `replaces=`
    names the file being superseded and the corpus is evaluated AS IF swapped. When
    the caller does not say and the draft collides with the corpus, this REFUSES
    rather than guesses (`ambiguous_intent`): a plausible wrong answer is the precise
    failure class this tool exists to end.

    Replace mode also earns the output an edit most needs — the DELTA. Nodes, edges
    and properties GAINED and LOST against the version already in the corpus, plus the
    inbound references the edit newly breaks or newly resolves. An edit that silently
    drops an existing node is invisible if you only report the new state.

⛔ EVERY RESPONSE DECLARES ITS RESOLUTION MODE.
Reference integrity is meaningless without saying what "the corpus" was. `resolution`
names the mode (`isolated` | `target_corpus`), the root, the file count, the intent
(`add` | `replace`), and the profile — and in `isolated` mode reference integrity is
reported as NOT EVALUATED and no dangle is presented as a defect. A preview that
reports 58 false dangles is worse than nothing, because it is ignored within a week.

NO SECOND IMPLEMENTATION OF LINE ACCOUNTING
-------------------------------------------
Everything structural here is delegated:
  * `factlattice.corpus.parse`               — the one parse, for both modes
  * `factlattice_engine.lint.find_unaccounted` / `coverage_stats` — the ratified
    line-accounting rule, called verbatim on the same `ParsedCorpus`
  * `factlattice.coverage.account_file` / `UNACCOUNTED_KINDS` — via the above
A second copy of the grammar living in a preview tool is exactly the fork-divergence
this project keeps paying for (see `factlattice/coverage.py`'s three mechanisms).

WHAT THIS MODULE DOES OWN: THE DECLARATION CHANNEL
--------------------------------------------------
Line accounting is a rule about LINES. It cannot, by construction, see the loss on
`41`'s line 3 or its three `- [DX-*]:` lines: those classify `prose` / `list_prose`,
which are legitimately ignorable shapes, and PROMOTING them inside the shared
classifier would re-judge every corpus at once. So preview adds a SECOND, separately
named loss channel over the same parse: every bracketed id the document AUTHORS in a
DEFINING position, and whether it inflated into a node. An authored bracket that minted
nothing is the headline finding precisely because it is invisible today. The channel is
labelled in the payload (`lost.declared_nothing_minted`) so a reader always knows which
rule spoke — this is additive to line accounting, never a substitute for it, and it
changes no lint verdict anywhere.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from types import SimpleNamespace
from typing import Dict, List, Optional, Sequence, Tuple

import factlattice
from factlattice import corpus as fl_corpus
from factlattice.hashing import list_lts_files, safe_read_text
from factlattice.parser import (
    _BRACKET_RE,
    _HEADING_RE,
    _LI_NODEDEF_RE,
    _UPPER_ID_RE,
    SECTION_CAUSAL,
    SECTION_RELATIONSHIPS,
)

from .lint.coverage import coverage_stats, find_unaccounted
from .lint.dangle import (
    ambiguous_reference_warn_line,
    classify_unqualified_references,
    unqualified_warn_line,
)
from .lint.unminted import (
    edge_verb_channels,
    edge_verb_warn_line,
    find_unminted_reference_lists,
)

MODE_ISOLATED = "isolated"
MODE_TARGET_CORPUS = "target_corpus"
INTENT_ADD = "add"
INTENT_REPLACE = "replace"

DEFAULT_VIRTUAL_PATH = "draft.lts"
# Property values are truncated in the default payload: an author reads a preview to
# see SHAPE, and a spec node's `context_for_change` block alone can run to 2 kB. Pass
# `verbose=True` for untruncated values.
VALUE_CHARS = 160


class PreviewRefusal(Exception):
    """A refusal, not a failure. Carries the machine-readable reason + remedy.

    Raised only where GUESSING would produce a plausible wrong answer (an unnamed
    edit read as an addition; an unresolvable corpus root). Every message names what
    to pass to proceed.
    """

    def __init__(self, reason: str, message: str, **detail):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        return {"success": False, "refused": True, "reason": self.reason,
                "error": self.message, **self.detail}


# ---------------------------------------------------------------------------
# THE DECLARATION CHANNEL — bracketed ids in a DEFINING position
# ---------------------------------------------------------------------------
# Narrow on purpose. A `.lts` is dense with brackets and only a few of them are
# DECLARATIONS; reporting a reference as an unminted declaration would be the
# cry-wolf failure that gets a tool switched off. The defining positions are exactly
# the two the parser itself recognises, plus the two near-misses of those:
#
#   HEADING        `### [ACME-ID-5A] Title`      -> `_heading_id`, mints a node
#   LIST NODE-DEF  `- [ID] **Title** ...`       -> `_LI_NODEDEF_RE`, mints a node
#   BARE LEADING   `[SPEC:X] Title` at col 0    -> mints NOTHING (a heading that
#                                                  lost its `#`)
#   BULLET LEADING `- [ID]: text`               -> mints NOTHING (neither node-def,
#                                                  nor property, nor edge)
#
# A bracket anywhere else on the line — in a property VALUE (`- pinned-to: [DEC-X]`),
# mid-prose (`... ([ACME-IH-CONSOLE]) becomes ...`) — is a REFERENCE and is never
# reported here. Lines inside `## Relationships` / `## Causal Graph` are excluded
# too: their leading bracket is an EDGE SOURCE, and a triple that produced no edge is
# already line accounting's `near_miss_triple`.
SHAPE_HEADING = "heading"
SHAPE_LIST_NODEDEF = "list_node_def"
SHAPE_BARE_LEADING = "bare_leading_bracket"
SHAPE_BULLET_LEADING = "bullet_leading_bracket"

_BULLET_HEAD_RE = re.compile(r"^-[ \t]+(?P<body>.*)$")

_WHY_BARE_LEADING = (
    "a bracketed id at column 0 with title text after it reads as a HEADING that lost "
    "its `#`. `_heading_id` runs ONLY on `#`-prefixed lines, so no node was minted and "
    "the whole line was accounted as prose — including every `- key: value` beneath it, "
    "which then has no enclosing [ID]-bearing heading to attach to. Prefix the line with "
    "`# ` (or `## `) and the node, its title and its properties all appear."
)
_WHY_BULLET_LEADING = (
    "a `- [ID] …` list item mints a node ONLY in the `- [ID] **Bold Title**` node-def "
    "form (`_LI_NODEDEF_RE` requires the bold title). It is not a property either "
    "(`_PROP_SIMPLE_RE` anchors on a bare lowercase key, never a bracket), and E2 edge "
    "extraction runs only inside `## Relationships` — so the id and its text reached no "
    "parser branch at all. Write it as `- [ID] **Title**` to mint a node, or move it "
    "under `## Relationships` as `- [SRC] VERB [ID]` to mint an edge."
)
_WHY_HEADING_REJECTED = (
    "a heading carrying a bracket that `_heading_id` did not accept as an id — the last "
    "`:`-separated segment must match `^[A-Z][A-Z0-9_-]*$`. No node was minted for this "
    "heading, so its whole body is outside any node."
)


class Declaration:
    """One bracketed id authored in a defining position, and what became of it."""

    __slots__ = ("id", "doctype", "line", "text", "shape", "minted", "minted_at",
                 "why")

    def __init__(self, id: str, doctype: Optional[str], line: int, text: str,
                 shape: str):
        self.id = id
        self.doctype = doctype
        self.line = line
        self.text = text
        self.shape = shape
        self.minted = False
        self.minted_at: Optional[int] = None
        self.why: Optional[str] = None

    def to_dict(self, *, verbose: bool = False) -> dict:
        d = {"id": self.id, "doctype": self.doctype, "line": self.line,
             "shape": self.shape, "minted": self.minted,
             "text": self.text if verbose else _clip(self.text, VALUE_CHARS)}
        if self.minted:
            d["minted_at_line"] = self.minted_at
        if self.why:
            d["why"] = self.why
        return d


def _leading_bracket_id(body: str) -> Optional[Tuple[str, Optional[str]]]:
    """`(node_id, doctype)` when `body` STARTS with an id-like bracket, else None.

    Mirrors `_heading_id`'s id/doctype segmentation so a bracket is judged by exactly
    the rule that would have minted it in a heading — the point is to report what the
    author ALMOST got, and a second segmentation rule here would report a different
    almost."""
    if not body.startswith("["):
        return None
    m = _BRACKET_RE.match(body)
    if m is None:
        return None
    segments = [s.strip() for s in m.group(1).strip().split(":")]
    node_id = segments[-1]
    if not _UPPER_ID_RE.match(node_id):
        return None
    doctype = None
    if len(segments) == 2 and "." not in segments[0]:
        doctype = segments[0]
    elif len(segments) >= 3:
        doctype = segments[-2]
    return node_id, doctype


def find_declarations(cov: "factlattice.LineCoverage") -> List[Declaration]:
    """Every bracketed id authored in a DEFINING position, in file order.

    Reads the parser's own `LineCoverage` (its `lines`, `fenced`, `section` and
    `body_start` records) rather than re-deriving document structure — same anti-fork
    rule the coverage classifier follows.
    """
    out: List[Declaration] = []
    for idx in range(cov.body_start, len(cov.lines)):
        if idx in cov.fenced:
            continue
        raw = cov.lines[idx]
        line_no = idx + 1
        stripped = raw.strip()
        if not stripped:
            continue

        heading = _HEADING_RE.match(raw)
        if heading is not None:
            text = heading.group(2)
            from factlattice.parser import _heading_id
            hid = _heading_id(text)
            if hid is not None:
                node_id, doctype, _title = hid
                out.append(Declaration(node_id, doctype, line_no, stripped,
                                       SHAPE_HEADING))
            elif _BRACKET_RE.search(text):
                d = Declaration(_BRACKET_RE.search(text).group(1).strip(), None,
                                line_no, stripped, SHAPE_HEADING)
                d.why = _WHY_HEADING_REJECTED
                out.append(d)
            continue

        # An edge source is not a declaration; a triple that minted nothing is line
        # accounting's `near_miss_triple`, and double-reporting it would double-count
        # the same defect under two rules.
        section = cov.section.get(idx)
        if section in (SECTION_RELATIONSHIPS, SECTION_CAUSAL):
            continue

        bullet = _BULLET_HEAD_RE.match(raw)   # column 0 only: `_LI_NODEDEF_RE` anchors at ^-
        if bullet is not None:
            body = bullet.group("body")
            hit = _leading_bracket_id(body)
            if hit is None:
                continue
            node_id, doctype = hit
            shape = (SHAPE_LIST_NODEDEF if _LI_NODEDEF_RE.match(raw)
                     else SHAPE_BULLET_LEADING)
            out.append(Declaration(node_id, doctype, line_no, stripped, shape))
            continue

        if raw[:1] in (" ", "\t"):
            continue    # indented: not a column-0 defining position
        hit = _leading_bracket_id(stripped)
        if hit is not None:
            node_id, doctype = hit
            out.append(Declaration(node_id, doctype, line_no, stripped,
                                   SHAPE_BARE_LEADING))
    return out


def _settle_declarations(decls: Sequence[Declaration],
                         nodes: Sequence["factlattice.Node"]) -> None:
    """Mark each declaration minted/not, and attach the WHY for the losses."""
    minted_at: Dict[str, int] = {}
    for n in nodes:
        minted_at.setdefault(n.id, n.line_range[0])
    for d in decls:
        if d.id in minted_at:
            d.minted = True
            d.minted_at = minted_at[d.id]
            if d.shape in (SHAPE_HEADING, SHAPE_LIST_NODEDEF):
                d.why = None
            continue
        d.minted = False
        if d.why is None:
            d.why = (_WHY_BARE_LEADING if d.shape == SHAPE_BARE_LEADING
                     else _WHY_BULLET_LEADING if d.shape == SHAPE_BULLET_LEADING
                     else _WHY_HEADING_REJECTED)


# ---------------------------------------------------------------------------
# Corpus resolution — the mode DECLARATION's factual basis
# ---------------------------------------------------------------------------
def _resolve_corpus_root(corpus: str, store=None) -> Tuple[str, dict]:
    """`(root_dir, how)` for a `corpus` given as a directory PATH or a loaded NAME.

    Refuses on a multi-root corpus it cannot disambiguate rather than silently
    previewing against one of several roots — the file count in the declaration would
    then be a true statement about the wrong corpus.
    """
    if os.path.isdir(corpus):
        return os.path.abspath(corpus), {"resolved_from": "directory_path"}
    if store is not None:
        part = store.get_partition(corpus)
        if part is not None:
            roots = [r for r in (part.root_paths or []) if os.path.isdir(r)]
            if len(roots) == 1:
                return os.path.abspath(roots[0]), {
                    "resolved_from": "loaded_corpus", "corpus_id": part.corpus_id}
            raise PreviewRefusal(
                "ambiguous_corpus_root",
                f"corpus {corpus!r} has {len(roots)} live roots; preview stages ONE "
                f"corpus root, so pass `corpus` as the directory path you mean",
                candidate_roots=roots)
    raise PreviewRefusal(
        "unknown_corpus",
        f"corpus {corpus!r} is neither a directory on disk nor a loaded corpus id",
        hint="pass `corpus` as an absolute path to the corpus directory")


def _rel_within(root: str, path: str) -> Optional[str]:
    """`path` as a root-relative posix path, or None if it is outside `root`.

    [GUARDS-CANONICALISE-FIRST] Both sides are canonicalised (realpath) before the containment
    test, and the test is on path COMPONENTS: a root reached through a symlink (~/workspace ->
    a file-sync service) matches a path given in resolved form, a file named `..x` is inside, and a symlink
    inside the root that points outside is outside."""
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    except ValueError:
        return None
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return None
    return rel.replace(os.sep, "/")


def _stage(root: str, draft_rel: str, content: str, replaces_rel: Optional[str]) -> str:
    """Copy `root`'s .lts set into a temp dir with the draft SWAPPED IN, and return it.

    A directory copy (not a file-set parse) so the staged parse runs the SAME
    `build_index` over a real directory that step (d) will run after the write — the
    preview's answer and the post-write answer are produced by one code path.
    """
    stage = tempfile.mkdtemp(prefix="lts-preview-")
    for rel in list_lts_files(root):
        if replaces_rel is not None and rel == replaces_rel:
            continue
        dst = os.path.join(stage, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(os.path.join(root, rel), dst)
    # Non-.lts files (a `99-*-profile.yaml`, a README) are NOT copied: the staged parse
    # is handed the baseline's already-resolved Profile object, so vocabulary parity is
    # guaranteed by passing the profile, not by re-discovering it at a new root.
    dst = os.path.join(stage, draft_rel)
    os.makedirs(os.path.dirname(dst) or stage, exist_ok=True)
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(content)
    return stage


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------
def _clip(s: str, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 3] + "..."


def _prop_view(props: Dict[str, object], verbose: bool) -> Dict[str, object]:
    if verbose:
        return props
    out: Dict[str, object] = {}
    for k, v in props.items():
        if isinstance(v, list):
            out[k] = [_clip(x, VALUE_CHARS) for x in v]
        elif isinstance(v, dict):
            out[k] = {kk: _clip(vv, VALUE_CHARS) for kk, vv in v.items()}
        else:
            out[k] = _clip(v, VALUE_CHARS)
    return out


def _node_view(n: "factlattice.Node", verbose: bool) -> dict:
    return {
        "id": n.id,
        "title": n.title,
        "type_prefix": n.type_prefix,
        "document_tag": n.document_tag,
        "parent_section": n.parent_section,
        "definition_form": n.definition_form,
        "status": n.status,
        "line_range": [n.line_range[0], n.line_range[1]],
        "property_count": len(n.properties),
        "properties": _prop_view(n.properties, verbose),
    }


def _edge_view(e: "factlattice.Edge") -> dict:
    d = {"source": e.source, "type": e.type, "target": e.target, "layer": e.layer,
         "inference": e.inference, "source_line": e.source_line}
    if e.flags:
        d["flags"] = list(e.flags)
    if e.dangling:
        d["dangling"] = True
    if e.label is not None:
        d["label"] = e.label
    return d


def _edge_key(e) -> Tuple[str, str, str]:
    return (e.source, e.type, e.target)


def _loss_view(v, verbose: bool) -> dict:
    return {"line": v.line_number,
            "text": v.text if verbose else _clip(v.text, VALUE_CHARS),
            "kind": v.kind,
            "near_branch": v.near_branch,
            "why": v.detail.get("why")}


# ---------------------------------------------------------------------------
# THE TOOL
# ---------------------------------------------------------------------------
def preview(content: Optional[str] = None, *, path: Optional[str] = None,
            corpus: Optional[str] = None, replaces: Optional[str] = None,
            virtual_path: Optional[str] = None, profile=None,
            verbose: bool = False, store=None) -> dict:
    """What would this `.lts` content BECOME, and what would it LOSE?

    `content` is primary (the authoring loop's step (b) has no file yet); `path` is
    the convenience wrapper that reads it. `corpus` — a directory path or a loaded
    corpus id — switches reference resolution from `isolated` to `target_corpus`.
    `replaces` names the corpus-relative file this draft supersedes (an EDIT);
    without it a draft that collides with the corpus is REFUSED, not guessed.
    """
    if content is None and path is None:
        raise PreviewRefusal("no_input", "pass `content` (primary) or `path`")
    if content is not None and path is not None:
        raise PreviewRefusal(
            "ambiguous_input",
            "pass `content` OR `path`, not both — with both, the preview cannot say "
            "which bytes it judged")

    origin = "content"
    if content is None:
        origin = "path"
        if not os.path.isfile(path):
            raise PreviewRefusal("file_not_found", f"no such file: {path!r}")
        content = safe_read_text(path, os.path.basename(path))

    label = virtual_path or (os.path.basename(path) if path else DEFAULT_VIRTUAL_PATH)
    if not label.endswith(".lts"):
        label += ".lts"

    src = {"origin": origin, "path": os.path.abspath(path) if path else None,
           "virtual_path": label, "bytes": len(content.encode("utf-8")),
           "lines": len(content.splitlines()),
           "sha256": "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()}

    if corpus is None:
        return _preview_isolated(content, label, src, profile, verbose)
    return _preview_in_corpus(content, label, src, corpus, replaces, profile,
                              verbose, store)


# --- isolated ---------------------------------------------------------------
def _preview_isolated(content: str, label: str, src: dict, profile, verbose: bool) -> dict:
    tmp = tempfile.mkdtemp(prefix="lts-preview-solo-")
    try:
        p = os.path.join(tmp, label)
        os.makedirs(os.path.dirname(p) or tmp, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
        parsed = fl_corpus.parse(paths=[p], profile=profile, corpus_id="preview")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    out = _core_payload(parsed, label, verbose)
    out["source"] = src
    out["resolution"] = {
        "mode": MODE_ISOLATED,
        "corpus_root": None,
        "corpus_files": 1,
        "intent": None,
        "replaces": None,
        "reference_integrity": "NOT EVALUATED",
        "profile": _profile_view(parsed),
        "declaration": (
            "ISOLATED PREVIEW — this file was parsed ALONE. Node and edge minting, "
            "line accounting and the declaration channel are exact. REFERENCE "
            "INTEGRITY WAS NOT EVALUATED: every `[REF]` to a node defined in another "
            "file is unresolvable here by construction, so no dangle is reported. "
            "(Measured: a subset scope on this corpus reports 114 dangling where the "
            "full set has 56.) Pass `corpus=` to resolve references for real."),
    }
    out["reference_integrity"] = {
        "evaluated": False,
        "why": "isolated preview — pass `corpus=` to resolve references",
    }
    out["delta"] = None
    out["report"] = render_text(out)
    return out


# --- target corpus ----------------------------------------------------------
def _preview_in_corpus(content: str, label: str, src: dict, corpus: str,
                       replaces: Optional[str], profile, verbose: bool,
                       store) -> dict:
    root, how = _resolve_corpus_root(corpus, store)
    baseline = fl_corpus.parse(source=root, profile=profile, corpus_id="preview-base")
    corpus_files = len(list_lts_files(root))

    # --- intent: ADD or REPLACE, never inferred from a guess -----------------
    replaces_rel: Optional[str] = None
    replaces_inferred = False
    if replaces:
        replaces_rel = _rel_within(root, replaces) if os.path.isabs(replaces) else \
            replaces.replace(os.sep, "/")
        if replaces_rel is None:
            raise PreviewRefusal(
                "replaces_outside_corpus",
                f"`replaces` {replaces!r} is not inside the corpus root {root!r}")
        if not os.path.isfile(os.path.join(root, replaces_rel)):
            raise PreviewRefusal(
                "replaces_not_found",
                f"`replaces` names {replaces_rel!r}, which does not exist in "
                f"{root!r} — a wrong path would silently downgrade this EDIT to an "
                f"ADD, and the collision noise that follows looks exactly like a real "
                f"defect. Pass the corpus-relative path of the file being superseded, "
                f"or omit `replaces` if this is genuinely a new file.")
    elif src["path"]:
        # NOT a guess: the caller handed us this exact path and it lies in the corpus.
        cand = _rel_within(root, src["path"])
        if cand and os.path.isfile(os.path.join(root, cand)):
            replaces_rel, replaces_inferred = cand, True

    draft_rel = replaces_rel or label
    baseline_ids = {n.id for n in baseline.nodes}
    draft_solo = factlattice.parse_text(content, draft_rel)
    draft_ids = {n.id for n in draft_solo.nodes}

    if replaces_rel is None:
        clash = sorted(draft_ids & baseline_ids)
        if clash:
            owners = sorted({n.source_file for n in baseline.nodes if n.id in set(clash)})
            raise PreviewRefusal(
                "ambiguous_intent",
                f"this draft defines {len(clash)} node id(s) that corpus {root!r} "
                f"already defines, and no `replaces` was given. Previewing an EDIT as "
                f"an ADD reports a duplicate-id collision for every one of them — "
                f"noise shaped exactly like a real defect. Say which file this draft "
                f"supersedes via `replaces`, or rename the ids if it is genuinely new.",
                colliding_ids=clash, defined_in=owners,
                likely_replaces=owners[0] if len(owners) == 1 else None)

    intent = INTENT_REPLACE if replaces_rel else INTENT_ADD

    stage = _stage(root, draft_rel, content, replaces_rel)
    try:
        candidate = fl_corpus.parse(source=stage, profile=baseline.profile,
                                    corpus_id="preview-candidate")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    out = _core_payload(candidate, draft_rel, verbose)
    out["source"] = src
    out["resolution"] = {
        "mode": MODE_TARGET_CORPUS,
        "corpus_root": root,
        "corpus_resolved_from": how.get("resolved_from"),
        "corpus_files": corpus_files,
        "intent": intent,
        "replaces": replaces_rel,
        "replaces_inferred": replaces_inferred,
        "reference_integrity": "EVALUATED",
        # The BASELINE's resolution, not the candidate's: the candidate is parsed at a
        # staging root with the baseline's Profile object handed in explicitly, so its
        # own `resolved_from`/`corpus_root` would truthfully describe a temp directory
        # and tell the author nothing about the vocabulary in play. The baseline
        # resolved against the REAL corpus root, which is the answer to the question.
        "profile": _profile_view(baseline),
        "declaration": (
            f"TARGET-CORPUS PREVIEW — this content was resolved AS IF it had landed in "
            f"{root!r} ({corpus_files} .lts files"
            + (f", REPLACING {replaces_rel!r}" if replaces_rel else ", as a NEW file")
            + "). Reference integrity is evaluated at full corpus scope, so the "
              "dangles reported here are the dangles you will have after the write — "
              "no isolation phantoms."),
    }
    out["reference_integrity"] = _reference_integrity(baseline, candidate, draft_rel)
    out["delta"] = (_delta(baseline, candidate, replaces_rel, draft_rel, verbose)
                    if intent == INTENT_REPLACE else None)
    out["report"] = render_text(out)
    return out


def _profile_view(parsed) -> dict:
    """Name the vocabulary this preview was judged against (never a silent default)."""
    res = getattr(parsed, "profile_resolution", None)
    prof = getattr(parsed, "profile", None)
    view = {"domain_id": getattr(prof, "domain_id", None)}
    for attr in ("resolved_from", "path"):
        if res is not None and hasattr(res, attr):
            view[attr] = getattr(res, attr)
    if res is not None and hasattr(res, "to_dict"):
        try:
            view = {**res.to_dict(), **{k: v for k, v in view.items() if v is not None}}
        except Exception:  # noqa: BLE001 — a profile view must never break a preview
            pass
    return view


# --- the payload every mode shares ------------------------------------------
def _core_payload(parsed, draft_rel: str, verbose: bool) -> dict:
    """WHAT IT BECAME + WHAT IT LOST + LINE ACCOUNTING + DECLARED-VS-MINTED + ERRORS,
    all restricted to the draft file's own contribution to `parsed`."""
    nodes = [n for n in parsed.nodes if n.source_file == draft_rel]
    edges = [e for e in parsed.edges if e.source_file == draft_rel]
    cov = next((c for c in parsed.coverage if c.source_file == draft_rel), None)
    if cov is None and len(parsed.coverage) == 1:
        cov = parsed.coverage[0]

    frontmatter: Dict[str, str] = {}
    doc_tag = domain = None
    if cov is not None:
        pf = factlattice.parse_text("\n".join(cov.lines), draft_rel)
        frontmatter, doc_tag, domain = pf.frontmatter, pf.document_tag, pf.domain

    # ONE shim so the ratified line-accounting functions run verbatim on the draft's
    # own coverage record. They are duck-typed on `.coverage`; re-deriving their two
    # loops here would be the fork this module exists not to be.
    scope = _CoverageScope([cov] if cov is not None else [])
    stats = coverage_stats(scope)
    losses = find_unaccounted(scope)

    decls = find_declarations(cov) if cov is not None else []
    _settle_declarations(decls, nodes)
    unminted = [d for d in decls if not d.minted]

    # THIRD LOST CHANNEL — an edge the author WROTE that minted no edge.
    # V12 (§19 errata) is detected corpus-wide by `lts_lint`; preview is the AUTHORING
    # LOOP, so the SAME silent loss must surface HERE or the compose->preview->land gate
    # reports green over a vanished edge (measured: `blocks: [X]` previewed LOST:0 while
    # minting nothing). ONE predicate, never re-derived: the exact
    # `find_unminted_reference_lists` the lint uses, scoped to THIS draft's nodes and
    # resolved against THIS parse's profile — so a profile-registered key that now MINTS
    # (e.g. `documents:` under the acme profile) is correctly NOT reported.
    _prof = getattr(parsed, "profile", None)
    _draft_scope = SimpleNamespace(
        nodes=nodes, coverage=[cov] if cov is not None else [], profile=_prof)
    unminted_edges = find_unminted_reference_lists(_draft_scope, profile=_prof)
    # (the maintainer 2026-08-17) The two DE-DUPLICATED edge-verb channels off the SAME shared
    # classifier the lint uses, so preview (V12b) and lint (V12) cannot disagree:
    #   undeclared_edge_verbs   — genuinely-unknown verbs (the register-or-typo worklist)
    #   case_mismatch_edge_verbs — miscased variants of a declared verb (WARN, fix casing)
    # The draft's coverage is passed so the uppercase variant (`- Depends-On: [X]`), which
    # never became a property, is still reached.
    _verb_channels = edge_verb_channels(_draft_scope, profile=_prof)
    _case_warn = edge_verb_warn_line(_verb_channels)
    # RULING C (2026-08-17): a BARE dangling reference with a NAMESPACE-QUALIFIED twin
    # is the namespace analog of Case-mismatch — surfaced, never auto-resolved. Scoped to
    # THIS draft's own dangling refs over whatever scope preview parsed (isolated = this
    # file only; target_corpus = the whole corpus). Inert on the current id model.
    _unqualified = classify_unqualified_references(parsed, source_file=draft_rel)
    _unq_warn = unqualified_warn_line(_unqualified)
    _amb_warn = ambiguous_reference_warn_line(_unqualified)

    errors = [d.to_dict() for d in parsed.diagnostics_of("parse_error")
              if d.source_file in (draft_rel, None)]
    errors += [f.to_dict() for f in parsed.property_findings
               if f.source_file in (draft_rel, None)]
    collisions = [d.to_dict() for d in parsed.diagnostics_of("collisions")
                  if d.source_file == draft_rel]

    total = max(stats["lines"], 1)
    accounted = stats["consumed"] + stats["ignorable"]

    return {
        "success": True,
        "tool": "lts_preview",
        "source": None,   # filled by caller
        "became": {
            "frontmatter": frontmatter,
            "document_tag": doc_tag,
            "domain": domain,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "nodes": [_node_view(n, verbose) for n in nodes],
            "edges": [_edge_view(e) for e in edges],
        },
        "lost": {
            "total": len(losses) + len(unminted) + len(unminted_edges),
            "line_accounting": [_loss_view(v, verbose) for v in losses],
            "declared_nothing_minted": [d.to_dict(verbose=verbose) for d in unminted],
            # NB no `source_file`: like the two channels above (which carry `line`/`id`
            # only), this is a SINGLE-DRAFT view where the file is always the draft, and
            # the field is the ONE thing that differs between content-mode (virtual label)
            # and path-mode (basename) — carrying it would break their required parity.
            "edge_shaped_property_minted_nothing": [
                {"node_id": u.node_id, "key": u.key,
                 "value": u.value if verbose else _clip(u.value, 80),
                 "line_number": u.line_number,
                 "nearest_minting_alternatives": list(u.alternatives)}
                for u in unminted_edges],
            # (the maintainer 2026-08-17) De-duplicated verb lists — the actionable output an
            # author/architect reads: which verbs to REGISTER (undeclared) vs FIX the
            # casing of (case_mismatch). Aggregated across the draft, one entry per verb.
            "undeclared_edge_verbs": _verb_channels["undeclared_edge_verbs"],
            "case_mismatch_edge_verbs": _verb_channels["case_mismatch_edge_verbs"],
            "case_mismatch_canonical": _verb_channels["case_mismatch_canonical"],
            "case_mismatch_warn": _case_warn,
            "channels": {
                "line_accounting": "the ratified [FL-LINE-ACCOUNTING] rule — a "
                                   "structure-shaped line no parser branch consumed",
                "declared_nothing_minted": "a bracketed id authored in a DEFINING "
                                           "position that inflated into no node",
                "edge_shaped_property_minted_nothing": "a property whose value IS a "
                                           "pure reference list under a NON-minting key "
                                           "(V12) — the reference is real and authored "
                                           "but no edge exists for it; declare it in "
                                           "`## Relationships`, rename onto a minting "
                                           "key, or register the key in the profile",
                "undeclared_edge_verbs": "(ruling 2b) de-duplicated GENUINELY-unknown "
                                           "edge verbs — no declared verb matches even "
                                           "by case; the register-vs-typo worklist",
                "case_mismatch_edge_verbs": "(ruling 2a) de-duplicated keys that are a "
                                           "CASE/separator variant of a declared verb "
                                           "(`Depends-On`->`depends-on`): WARNed, never "
                                           "silently minted — fix the casing and it mints",
            },
        },
        "unqualified_references": {
            "unqualified_references": _unqualified["unqualified_references"],
            "ambiguous_references": _unqualified["ambiguous_references"],
            "warn": _unq_warn,
            "ambiguous_warn": _amb_warn,
            "channel": "(RULING C) a bare reference that does not resolve but whose bare "
                       "id matches exactly one (unqualified) or several (ambiguous) "
                       "NAMESPACE-QUALIFIED definitions - strict identity is kept; the "
                       "engine names the likely target so the author qualifies it",
        },
        "line_accounting": {
            "lines": stats["lines"],
            "consumed": stats["consumed"],
            "ignorable": stats["ignorable"],
            "unaccounted": stats["unaccounted"],
            "coverage_pct": round(100.0 * accounted / total, 1),
            "rule": "COVERAGE, not equality (SPEC §2.5): every non-blank line is "
                    "either CONSUMED by a parser branch or CLASSIFIED as ignorable; "
                    "anything else is unaccounted.",
        },
        "declared_vs_minted": {
            "declared_count": len(decls),
            "minted_count": len(decls) - len(unminted),
            "not_minted": [d.id for d in unminted],
            "declared": [d.to_dict(verbose=verbose) for d in decls],
        },
        "errors": errors,
        "collisions": collisions,
    }


class _CoverageScope:
    """A `ParsedCorpus`-shaped view carrying exactly the draft's coverage record.

    `coverage_stats` and `find_unaccounted` read `corpus.coverage` and nothing else.
    Handing them this scope calls the RATIFIED rule verbatim over one file — the
    alternative (copying their two loops) is the fork.
    """

    __slots__ = ("coverage",)

    def __init__(self, coverage):
        self.coverage = coverage


# --- reference integrity (target-corpus mode only) --------------------------
def _reference_integrity(baseline, candidate, draft_rel: str) -> dict:
    """The dangles this draft CAUSES or RESOLVES — a delta, never a raw count.

    A raw dangling count would carry the corpus's pre-existing backlog into the
    author's face on every preview. What an author can act on is the CHANGE: what my
    draft breaks, and what it fixes.
    """
    def dangle_set(parsed):
        return {(d.source_file, d.source_line, (d.edge or {}).get("source"), d.node_id)
                for d in parsed.diagnostics_of("dangling")}

    base_d, cand_d = dangle_set(baseline), dangle_set(candidate)
    # A replaced file's lines move; compare by (referencing node, missing target) so a
    # re-numbered but unchanged reference is not reported as new.
    def pair_set(s):
        return {(a, b) for (_f, _l, a, b) in s}

    new_pairs = pair_set(cand_d) - pair_set(base_d)
    fixed_pairs = pair_set(base_d) - pair_set(cand_d)

    outbound = [{"from": (d.edge or {}).get("source"), "to": d.node_id,
                 "line": d.source_line, "type": (d.edge or {}).get("type")}
                for d in candidate.diagnostics_of("dangling")
                if d.source_file == draft_rel]
    draft_ids = {n.id for n in candidate.nodes if n.source_file == draft_rel}
    inbound = [{"from": e.source, "to": e.target, "file": e.source_file,
                "line": e.source_line, "type": e.type}
               for e in candidate.edges
               if e.source_file != draft_rel and e.target in draft_ids]

    return {
        "evaluated": True,
        "scope": "whole target corpus (draft swapped in)",
        "outbound_unresolved": outbound,
        "outbound_unresolved_count": len(outbound),
        "inbound_references": inbound,
        "inbound_reference_count": len(inbound),
        "newly_dangling": sorted(f"{a} -> {b}" for a, b in new_pairs),
        "newly_resolved": sorted(f"{a} -> {b}" for a, b in fixed_pairs),
        "corpus_dangling_before": len(base_d),
        "corpus_dangling_after": len(cand_d),
        "backlog_note": (
            "`corpus_dangling_before/after` is the TARGET CORPUS's own pre-existing "
            "backlog AT THIS ROOT — context, never a finding against this draft, and "
            "scope-dependent (a narrower root reports more, because cross-corpus "
            "targets fall outside it). Only `newly_dangling` / `newly_resolved` are "
            "attributable to the content previewed."),
    }


# --- delta (replace mode only) ----------------------------------------------
def _delta(baseline, candidate, replaces_rel: str, draft_rel: str,
           verbose: bool) -> dict:
    """What this edit GAINS and LOSES against the version already in the corpus.

    The silent drop of an existing node is exactly the accident the protocol exists to
    prevent, and it is invisible in a report of the new state alone.
    """
    old_nodes = {n.id: n for n in baseline.nodes if n.source_file == replaces_rel}
    new_nodes = {n.id: n for n in candidate.nodes if n.source_file == draft_rel}
    old_edges = {_edge_key(e) for e in baseline.edges if e.source_file == replaces_rel}
    new_edges = {_edge_key(e) for e in candidate.edges if e.source_file == draft_rel}

    props_lost: Dict[str, List[str]] = {}
    props_gained: Dict[str, List[str]] = {}
    for nid in sorted(set(old_nodes) & set(new_nodes)):
        ok, nk = set(old_nodes[nid].properties), set(new_nodes[nid].properties)
        if ok - nk:
            props_lost[nid] = sorted(ok - nk)
        if nk - ok:
            props_gained[nid] = sorted(nk - ok)

    lost_ids = sorted(set(old_nodes) - set(new_nodes))
    return {
        "against": replaces_rel,
        "nodes_lost": [{"id": i, "title": old_nodes[i].title} for i in lost_ids],
        "nodes_gained": sorted(set(new_nodes) - set(old_nodes)),
        "nodes_retained": len(set(old_nodes) & set(new_nodes)),
        "edges_lost": sorted(f"{s} {t} {d}" for (s, t, d) in old_edges - new_edges),
        "edges_gained": sorted(f"{s} {t} {d}" for (s, t, d) in new_edges - old_edges),
        "properties_lost": props_lost,
        "properties_gained": props_gained,
        "verdict": ("CLEAN — nothing the corpus already had was dropped"
                    if not lost_ids and not (old_edges - new_edges) and not props_lost
                    else "REGRESSION — this edit REMOVES content the corpus has today"),
    }


# ---------------------------------------------------------------------------
# Rendered report — what an author actually reads
# ---------------------------------------------------------------------------
def render_text(out: dict) -> str:
    r = out["resolution"]
    la = out["line_accounting"]
    b = out["became"]
    lines: List[str] = []
    lines.append(f"lts_preview — {out['source']['virtual_path'] if out.get('source') else '(draft)'}")
    lines.append(f"-- mode: {r['mode']} | {r['declaration']}")
    prof = r.get("profile") or {}
    lines.append(f"-- profile: {prof.get('domain_id') or 'NONE IN SCOPE'} "
                 f"(resolved_from={prof.get('resolved_from', 'unknown')})")
    lines.append("")
    lines.append(f"BECAME: {b['node_count']} node(s), {b['edge_count']} edge(s), "
                 f"document_tag={b['document_tag']!r}, domain={b['domain']!r}")
    for n in b["nodes"]:
        lines.append(f"  + [{n['id']}] {_clip(n['title'], 60)!r} "
                     f"(L{n['line_range'][0]}-{n['line_range'][1]}, "
                     f"{n['property_count']} props, {n['definition_form']})")
    for e in b["edges"]:
        lines.append(f"  -> {e['source']} {e['type']} {e['target']} (L{e['source_line']})")
    if not b["edges"]:
        lines.append("  -> (no edges)")
    lines.append("")
    lines.append(f"LINE ACCOUNTING: {la['lines']} lines = {la['consumed']} consumed + "
                 f"{la['ignorable']} ignorable + {la['unaccounted']} UNACCOUNTED "
                 f"({la['coverage_pct']}% coverage)")
    dm = out["declared_vs_minted"]
    lines.append(f"DECLARED vs MINTED: {dm['declared_count']} bracketed id(s) authored "
                 f"in a defining position, {dm['minted_count']} minted a node, "
                 f"{len(dm['not_minted'])} MINTED NOTHING")
    lines.append("")
    lost = out["lost"]
    if lost["total"] == 0:
        lines.append("LOST: nothing — every authored line was read or is ignorable, "
                     "and every declared id minted.")
    else:
        lines.append(f"LOST ({lost['total']}):")
        for d in lost["declared_nothing_minted"]:
            lines.append(f"  ! L{d['line']} DECLARED-NOTHING-MINTED [{d['id']}] "
                         f"({d['shape']})")
            lines.append(f"      {d['text']}")
            lines.append(f"      why: {d.get('why')}")
        for v in lost["line_accounting"]:
            lines.append(f"  ! L{v['line']} {v['kind']} (near {v['near_branch']})")
            lines.append(f"      {v['text']}")
            lines.append(f"      why: {v['why']}")
        for u in lost.get("edge_shaped_property_minted_nothing", []):
            alt = (", nearest minting key(s): "
                   + ", ".join(u["nearest_minting_alternatives"])
                   if u["nearest_minting_alternatives"] else "")
            lines.append(f"  ! L{u['line_number']} EDGE-SHAPED-PROPERTY-MINTED-NOTHING "
                         f"`{u['key']}` on [{u['node_id']}]{alt}")
            lines.append(f"      {u['value']}")
            lines.append("      why: the value is a pure reference list but the key is "
                         "not a minting key, so no edge exists for it")
    # ALWAYS printed (outside the total==0 gate): a lone separator-variant parses
    # cleanly and adds nothing to `total`, so gating this on total would let a
    # case-mismatch print "LOST: nothing" — the silent-success bug this design fights.
    if lost.get("case_mismatch_warn"):
        lines.append("")
        lines.append("⚠ " + lost["case_mismatch_warn"])
        for miscased, canon in (lost.get("case_mismatch_canonical") or {}).items():
            lines.append(f"      fix casing: {miscased} -> {canon} (then it mints)")
    if lost.get("undeclared_edge_verbs"):
        lines.append("")
        lines.append("UNDECLARED EDGE VERBS (register as a profile edge key, or fix as "
                     "a typo — the maintainer/architect decision): "
                     + ", ".join(lost["undeclared_edge_verbs"]))
    uq = out.get("unqualified_references") or {}
    if uq.get("warn"):
        lines.append("")
        lines.append("⚠ " + uq["warn"] + "  (did you mean the qualified id?)")
    if uq.get("ambiguous_warn"):
        lines.append("")
        lines.append("⚠ " + uq["ambiguous_warn"] + "  (qualify it — do not guess)")
    if out.get("errors"):
        lines.append("")
        lines.append(f"ERRORS ({len(out['errors'])}):")
        for e in out["errors"]:
            lines.append(f"  x {e.get('kind')}: {e.get('message')}")
    if out.get("collisions"):
        lines.append("")
        lines.append(f"COLLISIONS ({len(out['collisions'])}):")
        for c in out["collisions"]:
            lines.append(f"  x {c.get('message')}")
    ri = out.get("reference_integrity") or {}
    lines.append("")
    if not ri.get("evaluated"):
        lines.append("REFERENCE INTEGRITY: NOT EVALUATED (isolated preview) — no "
                     "dangle is reported here, and none should be inferred.")
    else:
        lines.append(f"REFERENCE INTEGRITY (whole target corpus): "
                     f"{ri['outbound_unresolved_count']} unresolved outbound, "
                     f"{ri['inbound_reference_count']} inbound reference(s) to this file")
        for x in ri["newly_dangling"]:
            lines.append(f"  ! NEWLY DANGLING: {x}")
        for x in ri["newly_resolved"]:
            lines.append(f"  + newly resolved: {x}")
    d = out.get("delta")
    if d:
        lines.append("")
        lines.append(f"DELTA vs {d['against']}: {d['verdict']}")
        for n in d["nodes_lost"]:
            lines.append(f"  - NODE REMOVED [{n['id']}] {n['title']!r}")
        for i in d["nodes_gained"]:
            lines.append(f"  + node added [{i}]")
        for k, v in d["properties_lost"].items():
            lines.append(f"  - properties removed from [{k}]: {', '.join(v)}")
        for x in d["edges_lost"]:
            lines.append(f"  - EDGE REMOVED: {x}")
        for x in d["edges_gained"]:
            lines.append(f"  + edge added: {x}")
    return "\n".join(lines)


__all__ = ["preview", "render_text", "find_declarations", "Declaration",
           "PreviewRefusal", "MODE_ISOLATED", "MODE_TARGET_CORPUS",
           "INTENT_ADD", "INTENT_REPLACE"]
