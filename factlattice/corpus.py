"""The §7 wrapper: `parse() -> ParsedCorpus` with first-class `diagnostics`.

This is the code the library contract asks for (contract §7). Everything under it —
`parser.py`, `index_builder.py`, `hashing.py`, `constants.py` — is the lts-engine engine
core; the whole point is ONE parser with NO divergence from what the query engine
ingests. This module does not re-implement any parsing or graph-building rule: it
*composes* the core and *unifies* the diagnostics the core scatters today into the
contract's first-class taxonomy.

The core scatters "preserve-and-flag" signals (contract §3) across three places:
  * `Edge.flags` carrying `unregistered_ns` / `unresolved_target`  (parse-time),
  * `Edge.dangling`                                                (index whole-corpus pass),
  * `build_index`'s `id_collisions` + `parse_errors` lists         (index level).
`ParsedCorpus.diagnostics` is the single first-class collection unifying them into the
four contract §3 classes — `dangling | unregistered_ns | unresolved_target | collisions`
(plus `parse_error`, surfaced so a build-time bad file is never silently dropped).

Design invariant — ONE source of truth: `parse()` runs the copied `build_index` exactly
once and caches its dict. `to_graph_index()` returns that dict (so it is IDENTICAL to
`build_index`'s output by construction), and `diagnostics` is DERIVED from the same dict
— a linter and lts-engine therefore cannot disagree on any §1–§3 rule.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .index_builder import build_index
from .parser import (
    CANONICAL_LAYERS,
    Edge,
    LineCoverage,
    Node,
    ParseFinding,
    is_profile_document,
    parse_file,
)
from .parser_config import ParserConfig
from .profile import (
    DEFAULT_PROFILE,
    RESOLVED_NONE,
    Profile,
    ProfileResolution,
    resolve_profile_ex,
)

# The contract §3 flag taxonomy, verbatim (alignment doc adopts these names too).
# `collisions` is the §3/alignment-doc spelling (§7 prose uses the singular
# "collision"; we standardize on the plural per §3's class heading + the alignment doc).
DIAG_DANGLING = "dangling"
DIAG_UNREGISTERED_NS = "unregistered_ns"
DIAG_UNRESOLVED_TARGET = "unresolved_target"
DIAG_COLLISIONS = "collisions"
DIAG_PARSE_ERROR = "parse_error"  # not one of the four §3 classes; surfaced so a
#                                   build-time unparseable file is never silently lost.

CONTRACT_DIAGNOSTIC_CLASSES = (
    DIAG_DANGLING, DIAG_UNREGISTERED_NS, DIAG_UNRESOLVED_TARGET, DIAG_COLLISIONS,
)


@dataclass
class Diagnostic:
    """One preserve-and-flag finding (contract §3), first-class.

    `kind` is one of the taxonomy constants above. `source_file`/`source_line` locate
    it. Edge-derived diagnostics carry the offending `edge` dict; a `collisions`
    diagnostic carries `node_id` + `detail` (kept/collision source+line). Nothing here
    is a *judgement* — severity/policy is the consumer's (linter's) call; this is the
    raw, deterministic signal both the linter and lts-engine see identically.
    """

    kind: str
    message: str
    source_file: Optional[str] = None
    source_line: Optional[int] = None
    node_id: Optional[str] = None
    edge: Optional[dict] = None
    detail: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "message": self.message,
            "source_file": self.source_file,
            "source_line": self.source_line,
            "node_id": self.node_id,
            "edge": self.edge,
            "detail": self.detail,
        }


@dataclass
class ParsedCorpus:
    """The §7 in-memory model — the PRIMARY interface for consumers.

    - `nodes`  : list[Node]  (parse-time dataclasses, all requested files)
    - `edges`  : list[Edge]  (parse-time dataclasses; per-parse `flags`)
    - `diagnostics` : list[Diagnostic]  (contract §3, first-class, whole-corpus)
    - `source_corpus` : the `source_corpus` block of the graph index (provenance)

    `edges_by_layer()` groups edges into the four canonical layers (the §7
    "edges[4 layers]" view). `to_graph_index()` returns the §4 `graph_index.json`
    dict — IDENTICAL to `build_index`'s output (it is the cached build).

    Whole-corpus signals (`dangling`, `collisions`) live in `diagnostics`, not on the
    parse-time `edges` — mirroring the core's own split (the parser marks a provisional
    per-file state; `build_index` finalizes over the whole corpus). Read `diagnostics`
    for the authoritative preserve-and-flag verdict.
    """

    nodes: List[Node]
    edges: List[Edge]
    diagnostics: List[Diagnostic]
    source_corpus: dict
    profile: Profile = field(default_factory=lambda: DEFAULT_PROFILE)
    # HOW `profile` came to be in scope (explicit / corpus-config / auto-discovery /
    # none) and against which root. Carried so every consumer can DECLARE the
    # vocabulary it judged against instead of leaving a caller to infer it — the
    # difference between "clean" and "the rules were switched off".
    profile_resolution: ProfileResolution = field(
        default_factory=lambda: ProfileResolution(DEFAULT_PROFILE, RESOLVED_NONE))
    _index: dict = field(default_factory=dict, repr=False)
    # [FL-LINE-ACCOUNTING] one consumption record per parsed file, in parse order.
    # A pure side-output of the same single parse — it cannot disagree with the
    # graph, because it IS the graph build's own record of what it read.
    coverage: List[LineCoverage] = field(default_factory=list, repr=False)
    # Structural ambiguities the parser REFUSED to resolve (today: only the P5
    # list-vs-mapping discrimination). Additive; empty unless a corpus authors an
    # ambiguous `- key:` block. Kept OFF the graph index deliberately — the index is
    # the graph, and a finding is a statement about the SOURCE, not about the graph.
    property_findings: List[ParseFinding] = field(default_factory=list, repr=False)
    # [A1/A2/A3, Era 1] the VOCABULARY channel — version marker, undeclared keys,
    # unregistered verbs. Separate from `property_findings` for the reason
    # `ParsedFile.vocabulary_findings` states: `legacy_document` fires on every
    # unstamped file, i.e. on the entire installed base, and merging it into the
    # structural channel would destroy that channel's only signal.
    vocabulary_findings: List[ParseFinding] = field(default_factory=list, repr=False)

    def to_graph_index(self) -> dict:
        """Return the §4 graph_index.json dict — identical to `build_index`'s output."""
        return self._index

    @property
    def profile_documents(self) -> List[str]:
        """Files under this corpus that were PARSED but excluded from the content
        graph because they declare themselves domain profiles (`document_tag: PROFILE`).

        Exposed so a consumer can always answer "which files does this corpus not speak
        for?" — an exclusion nobody can enumerate is indistinguishable from a bug.
        """
        return list(self._index.get("profile_documents") or [])

    def edges_by_layer(self) -> Dict[str, List[Edge]]:
        """Group parse-time edges into the four canonical layers (§7 edges[4 layers])."""
        out: Dict[str, List[Edge]] = {layer: [] for layer in CANONICAL_LAYERS}
        for e in self.edges:
            out.setdefault(e.layer, []).append(e)
        return out

    def diagnostics_of(self, kind: str) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.kind == kind]

    def diagnostic_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for d in self.diagnostics:
            counts[d.kind] = counts.get(d.kind, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# parse()
# ---------------------------------------------------------------------------
def parse(source=None, *, paths=None, profile=None,
          corpus_id: str = "corpus", domain_suffix: Optional[str] = None,
          built_at: Optional[str] = None,
          registered_ns=None) -> ParsedCorpus:
    """Parse a FactLattice corpus into the §7 in-memory model + diagnostics.

    `source` may be:
      * a directory  -> the whole .lts corpus under it (the primary linter/lts-engine path);
      * a single .lts file, or a list of .lts files -> that exact file-set;
    or pass `paths=[...]` explicitly. Exactly one of a dir / file(s) is expected.

    `profile` (contract §5): a path to a `<domain>-profile.lts` / `NN-<name>-profile.yaml`,
    a dict, or a `Profile`. When None it is RESOLVED, in this order, against the corpus
    root (the directory for `source=`, the derived `common_root` for `paths=`):
    explicit argument -> the corpus registry (`factlattice.registry`) -> directory
    auto-discovery -> none. The outcome — including `none` — is REPORTED on
    `ParsedCorpus.profile_resolution`; it is never a silent default. The profile
    supplies the core's `registered_ns` hook. `registered_ns=` may be passed to
    override the profile entirely (advanced).

    Determinism: `build_index` is run exactly once; `to_graph_index()` returns its dict
    and `diagnostics` is derived from it, so the JSON serialization and the diagnostics
    can never disagree.
    """
    file_list, corpus_root, is_dir = _normalize_inputs(source, paths)

    # ⛔ CALL-FORM PARITY. `source=<dir>` and `paths=[every file under that dir]` are
    # the SAME question and must get the SAME vocabulary. They did not: file-set mode
    # passed corpus_root=None, so profile resolution was skipped entirely and hundreds
    # of profile findings appeared or vanished purely on how the caller phrased the
    # call. The common root is derived with the SAME helper `_build_over_files` uses
    # for its staging layout (`common_root`) — one derivation, so the root a file-set
    # is PROFILED against is by construction the root it is LABELLED against.
    resolution_root = corpus_root if is_dir else common_root(file_list)
    resolution = resolve_profile_ex(profile, resolution_root)
    prof = resolution.profile
    # ⛔ [C3, Era 1] ONE constructed vocabulary, built at the ONE seam, threaded to
    # every parse this function performs. Before this, `ns` and `dv` were threaded by
    # hand and `property_edges` / `profile_layers` WERE NOT THREADED AT ALL on this
    # path — so a profile that registered a minting key or a per-type layer was, on the
    # `lts_lint` / `lts_preview` / `lts_profile_audit` path, inert while reading as
    # functional (review V5/V5b). `ParserConfig.from_profile` is where the two profile
    # systems merge and the ONLY place they may.
    cfg = ParserConfig.from_profile(profile=prof, registered_ns=registered_ns)
    dom_suffix = domain_suffix if domain_suffix is not None else (prof.domain_id or "unknown")

    if is_dir:
        index = build_index(corpus_root, corpus_id=corpus_id, domain_suffix=dom_suffix,
                            built_at=built_at, config=cfg)
        nodes, edges, coverage, prop_findings, vocab_findings = _reparse_dir(corpus_root, cfg)
    else:
        # File-set mode: stage the exact files into a temp corpus so the SAME copied
        # build_index runs over precisely them (guarantees to_graph_index() == build_index
        # output with zero divergence). Labels become basenames in this mode.
        nodes, edges, index, coverage, prop_findings, vocab_findings = _build_over_files(
            file_list, cfg, corpus_id, dom_suffix, built_at)

    diagnostics = _diagnostics_from_index(index)
    return ParsedCorpus(
        nodes=nodes, edges=edges, diagnostics=diagnostics,
        source_corpus=index["source_corpus"], profile=prof,
        profile_resolution=resolution, _index=index,
        coverage=coverage, property_findings=prop_findings,
        vocabulary_findings=vocab_findings,
    )


# ---------------------------------------------------------------------------
# input normalization
# ---------------------------------------------------------------------------
def _normalize_inputs(source, paths):
    """Return (file_list, corpus_root, is_dir).

    is_dir=True  -> corpus_root is a real directory; file_list is unused for building
                    (build_index walks the dir) but returned for reference.
    is_dir=False -> file_list is the exact set of .lts files to parse; corpus_root is None.
    """
    if paths is not None and source is not None:
        raise ValueError("pass either `source` or `paths=`, not both")
    if paths is not None:
        files = [os.fspath(p) for p in paths]
        return files, None, False
    if source is None:
        raise ValueError("parse() needs a corpus directory, a file, or paths=[...]")
    if isinstance(source, (list, tuple)):
        return [os.fspath(p) for p in source], None, False
    src = os.fspath(source)
    if os.path.isdir(src):
        return [], os.path.abspath(src), True
    if os.path.isfile(src):
        return [src], None, False
    raise FileNotFoundError(f"not a corpus dir or file: {src!r}")


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------
def _reparse_dir(corpus_root: str, cfg):
    """Collect Node/Edge dataclasses for a directory corpus, using the SAME
    corpus-relative source_file labels build_index uses (so `.nodes`/`.edges` labels
    line up with the index dict / diagnostics). Same deterministic parse_file call."""
    from .hashing import list_lts_files
    nodes: List[Node] = []
    edges: List[Edge] = []
    coverage: List[LineCoverage] = []
    prop_findings: List[ParseFinding] = []
    vocab_findings: List[ParseFinding] = []
    for rel in list_lts_files(corpus_root):
        abs_path = os.path.join(corpus_root, rel)
        try:
            pf = parse_file(abs_path, rel_source_file=rel, config=cfg)
        except Exception:
            continue  # build_index records this in parse_errors -> surfaced as a diagnostic
        # Same content/vocabulary split `build_index` applies (see
        # `parser.is_profile_document`). Keeping it here too is what stops the linter
        # and the index from disagreeing about what the corpus CONTAINS — the whole
        # point of the one-parser guarantee.
        #
        # ⛔ NODES AND EDGES ONLY. `coverage` and `findings` are kept, because they are
        # statements about the SOURCE, not about the graph — the same distinction this
        # module already draws for `property_findings`. A profile is still a parsed
        # file, so a structure-shaped line the parser silently dropped inside one is
        # still [FL-LINE-ACCOUNTING]'s business; suppressing that would create a file
        # class where silent loss is invisible, which is the one thing this engine
        # refuses to have.
        if not is_profile_document(pf):
            nodes.extend(pf.nodes)
            edges.extend(pf.edges)
        if pf.coverage is not None:
            coverage.append(pf.coverage)
        prop_findings.extend(pf.findings)
        vocab_findings.extend(pf.vocabulary_findings)
    return nodes, edges, coverage, prop_findings, vocab_findings


def common_root(file_list) -> Optional[str]:
    """The deepest directory common to `file_list` — the file-set's implied corpus root.

    ONE derivation, TWO readers: `_build_over_files` uses it to lay out its staging
    tree (the SL-16 fix — see that function's ⛔ note on why structure is preserved),
    and `parse()` uses it to resolve the PROFILE for a `paths=` call. A second
    root-derivation here would let a file-set be labelled against one root and judged
    against another, which is the same class of bug in a new coat.

    Returns None when the list is empty or the files share no root at all (different
    drives) — the honest answer, so the caller falls through to "no profile in scope"
    and says so, rather than silently profiling against an invented root.
    """
    abspaths = [os.path.abspath(os.fspath(p)) for p in (file_list or [])]
    if not abspaths:
        return None
    try:
        if len(abspaths) == 1:
            return os.path.dirname(abspaths[0]) or None
        return os.path.commonpath(abspaths) or None
    except ValueError:
        return None


def _build_over_files(file_list, cfg, corpus_id, dom_suffix, built_at):
    """Stage the exact files into a temp dir and run the copied build_index over them.

    Returns (nodes, edges, index, coverage, property_findings). The staged tree PRESERVES each file's path
    relative to the deepest directory common to the whole list, so the label a file gets
    here is the label it would get from a directory build of that same root.

    ⛔ WHY THE STRUCTURE IS PRESERVED (do not "simplify" this back to basenames).
    `build_index` resolves a duplicate node id by sorting the file list and keeping the
    FIRST definition. Flattening to `os.path.basename` throws away the directory component
    BEFORE that sort, which silently changes the sort key and therefore the winner:

        dir  mode : 'decision-graph-x.lts'  <  'proposals/decision-graph-x-STAGING.lts'   -> LIVE kept
        flat mode : 'decision-graph-x-STAGING.lts'  <  'decision-graph-x.lts'             -> STAGING kept
                                              ^ '-' is 0x2D, '.' is 0x2E

    One first-wins rule over two different strings gave two different winners for the same
    corpus — `lts_lint(paths=[...])` reported a kept file the engine did not serve, in BOTH
    argument orders. Flattening also degraded `source_file` to a bare basename, so the
    provenance was already gone in the very field the collision finding prints.

    The temp dir is removed once the (deterministic) index is cached — build_index is pure
    over content, so nothing is lost by discarding the staging. NOTE for
    [FL-LINE-ACCOUNTING]: because the staging is deleted here, the coverage record RETAINS
    the source lines — a consumer cannot re-read them from the recorded label.
    """
    staged: List[tuple] = []  # (rel_name, abs_original)
    seen: Dict[str, int] = {}
    abspaths: List[str] = []
    for p in file_list:
        if not os.path.isfile(p):
            raise FileNotFoundError(f"not a file: {p!r}")
        abspaths.append(os.path.abspath(p))

    # Deepest directory common to every input; falls back to per-file basenames only when
    # the inputs share no root at all (different drives), where structure cannot be kept.
    # SAME helper `parse()` profiles against — see `common_root`.
    common = common_root(abspaths)

    for ap in abspaths:
        rel = os.path.relpath(ap, common).replace(os.sep, "/") if common else os.path.basename(ap)
        if rel in seen:  # disambiguate identical relative paths deterministically
            seen[rel] += 1
            stem, ext = os.path.splitext(rel)
            rel = f"{stem}.{seen[rel]}{ext}"
        else:
            seen[rel] = 0
        staged.append((rel, ap))

    tmp = tempfile.mkdtemp(prefix="factlattice-corpus-")
    try:
        for rel, abs_orig in staged:
            dest = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(abs_orig, dest)
        index = build_index(tmp, corpus_id=corpus_id, domain_suffix=dom_suffix,
                            built_at=built_at, config=cfg)
        nodes: List[Node] = []
        edges: List[Edge] = []
        coverage: List[LineCoverage] = []
        prop_findings: List[ParseFinding] = []
        vocab_findings: List[ParseFinding] = []
        for rel, abs_orig in staged:
            pf = parse_file(os.path.join(tmp, rel), rel_source_file=rel, config=cfg)
            # Vocabulary, not content — mirrors build_index over the staging dir.
            # Coverage/findings are kept for the same reason `_reparse_dir` keeps them.
            if not is_profile_document(pf):
                nodes.extend(pf.nodes)
                edges.extend(pf.edges)
            if pf.coverage is not None:
                coverage.append(pf.coverage)
            prop_findings.extend(pf.findings)
            vocab_findings.extend(pf.vocabulary_findings)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return nodes, edges, index, coverage, prop_findings, vocab_findings


# ---------------------------------------------------------------------------
# diagnostics derivation (single source of truth = the index dict)
# ---------------------------------------------------------------------------
def _diagnostics_from_index(index: dict) -> List[Diagnostic]:
    """Unify the core's scattered preserve-and-flag signals into first-class
    Diagnostics, read straight off the (authoritative, whole-corpus) index dict."""
    diags: List[Diagnostic] = []

    # collisions (contract §3): duplicate node IDs across the corpus.
    for coll in index.get("id_collisions", []):
        diags.append(Diagnostic(
            kind=DIAG_COLLISIONS,
            message=f"duplicate node id {coll['id']!r} "
                    f"(kept {coll['kept_source_file']}, also defined in {coll['collision_source_file']})",
            source_file=coll.get("collision_source_file"),
            source_line=(coll.get("collision_line_range") or [None])[0],
            node_id=coll.get("id"),
            detail=coll,
        ))

    # parse_error: a file build_index could not parse (never silently dropped).
    for pe in index.get("parse_errors", []):
        diags.append(Diagnostic(
            kind=DIAG_PARSE_ERROR,
            message=f"parse error in {pe.get('source_file')}: {pe.get('error')}",
            source_file=pe.get("source_file"),
            detail=pe,
        ))

    # edge-level flags: unregistered_ns / unresolved_target / dangling.
    # An edge can yield MORE THAN ONE diagnostic (e.g. an unregistered-ns edge whose
    # target is also missing) — each condition is surfaced independently.
    for layer in CANONICAL_LAYERS:
        for e in index.get("layers", {}).get(layer, {}).get("edges", []):
            flags = e.get("flags", [])
            if DIAG_UNREGISTERED_NS in flags:
                diags.append(Diagnostic(
                    kind=DIAG_UNREGISTERED_NS,
                    message=f"edge type {e['type']!r} uses an unregistered namespace "
                            f"({e['source']} -> {e['target']})",
                    source_file=e.get("source_file"), source_line=e.get("source_line"),
                    edge=e,
                ))
            if DIAG_UNRESOLVED_TARGET in flags:
                diags.append(Diagnostic(
                    kind=DIAG_UNRESOLVED_TARGET,
                    message=f"authored edge {e['type']!r} from {e['source']} has a prose "
                            f"target (not a [node]): {e['target']!r}",
                    source_file=e.get("source_file"), source_line=e.get("source_line"),
                    edge=e,
                ))
            elif e.get("dangling"):
                # dangling ONLY when it is a real [node] ref that resolves nowhere —
                # an unresolved_target (prose) is its own class, not double-counted here.
                diags.append(Diagnostic(
                    kind=DIAG_DANGLING,
                    message=f"edge {e['type']!r} from {e['source']} targets {e['target']!r}, "
                            f"which is defined nowhere in the corpus",
                    source_file=e.get("source_file"), source_line=e.get("source_line"),
                    node_id=e.get("target"), edge=e,
                ))
    return diags
