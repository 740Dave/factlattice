"""factlattice_engine.analytics.profile_audit — corpus vocabulary vs. declared profile.

A profile *declares* a vocabulary (ID prefixes, property names, relationship/edge
verbs); a corpus *uses* one. They drift. This op turns the one-shot, ad-hoc
`PROFILE-RECONCILIATION-DRAFT.md` audit into a re-runnable ANALYTICS query, so drift is a
number an author watches — used-but-unregistered (vocab creeping in) and
declared-but-unused (dead vocab) — not a subagent pass.

It is a pure COMPOSITION over the existing ANALYTICS machinery: the USED side is derived
from the SAME complete, freshness-gated node set `group_by`/`count`/`distinct` use
(`LtsAggregator._find_all`) plus `count_by_edge_type`. It therefore inherits
[INV-LTS-FRESH] for free: a STALE corpus never returns numbers — the first gated fetch
fails and its loud stale marker is propagated VERBATIM (never a silently-wrong drift
figure). See `profile_audit()`.

--------------------------------------------------------------------------------
THE DECLARED SIDE IS SWAPPABLE (a ProfileAdapter)
--------------------------------------------------------------------------------
The "declared" set comes from the profile. TODAY the linter loads a `99-*-profile.yaml`
(see `factlattice.profile.Profile.raw`); a just-made decision (NOT yet built) will make
the `.lts` domain-profile extensions authoritative instead. So the audit reads the
declared vocabulary ONLY through a small `ProfileAdapter` that returns
`{prefixes, properties, edge_types}` — never off a concrete profile format directly. The
value of the audit (the USED side + the diffs) is format-independent; repointing at
`.lts` domain profiles later is a NEW adapter, no change to `profile_audit`.

If no declared set is available, the audit returns the USED-side analysis with the
declared set + both diffs marked `"no profile loaded"` rather than failing.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Set

from ..reason_codes import UNREACHABLE_KEY_REASON

# --------------------------------------------------------------------------- #
# property keys the PARSER injects (not author-declared vocabulary). Excluded from
# the "used properties" fold so the audit measures the AUTHORED vocabulary, matching
# how the reconciliation draft counts. `aliases` is emitted for multi-id headers
# (parser `_parse_node`), never typed by an author.
# --------------------------------------------------------------------------- #
_INJECTED_PROPERTY_KEYS: Set[str] = {"aliases"}


# =========================================================================== #
# The ProfileAdapter seam — the ONLY place a profile FORMAT is read.
# =========================================================================== #
class ProfileAdapter:
    """Return the declared vocabulary as `{prefixes, properties, edge_types}` sets.

    A concrete adapter knows ONE profile format. `available` is False when there is no
    declared vocabulary to compare against (the audit then reports the used side with
    the diffs marked "no profile loaded"). `source` is a human label for provenance.
    """

    available: bool = False
    source: str = "no profile loaded"

    def prefixes(self) -> Optional[Set[str]]:
        return None

    def properties(self) -> Optional[Set[str]]:
        return None

    def edge_types(self) -> Optional[Set[str]]:
        return None


class NullProfileAdapter(ProfileAdapter):
    """No declared vocabulary — used when no profile is supplied/resolvable."""

    available = False
    source = "no profile loaded"


class YamlProfileAdapter(ProfileAdapter):
    """Adapter over a loaded `factlattice.profile.Profile.raw`, EITHER serialization.

    ⚠ THE CLASS NAME IS NOW A MISNOMER, kept only so existing imports do not break.
    `factlattice.profile.load_profile` reads `.lts`, `.yaml` and `.json` into the SAME
    dict ([ACME-NARROW-WAIST] applied to the profile), so this adapter has always
    handled a `.lts` profile correctly — it just LABELLED it `yaml:...`, which is the
    kind of small lie that makes an audit unreadable. `source` is now derived from the
    file extension, so the label names the serialization actually on disk.

    Declared-set extraction from `raw`:
      * prefixes   — `raw["prefixes"]`, each `{prefix, ...}` -> the `prefix` string
                     (kept verbatim, incl. its trailing '-', e.g. `ACME-`, `DP-CIO-`).
      * properties — `raw["properties"]` if present (list of names or `{name|property}`
                     dicts). An example profile that declares no general property
                     vocabulary yields the empty set here — every used property then
                     shows as used_but_unregistered, which is the true state of drift.
      * edge_types — `raw["relationship_verbs"]` core + domain verb lists, UPPERCASED.

    Accepts a path/dict/`Profile` (anything `factlattice.profile.load_profile` takes).
    """

    def __init__(self, profile: Any, resolved_from: str = "explicit-argument",
                 detail: Optional[str] = None):
        from factlattice.profile import load_profile
        self._profile = load_profile(profile)
        # ⛔ PUBLIC, and it is load-bearing. `_property_edges_section` reads
        # `getattr(adapter, "profile", None)`; with the loaded profile stored ONLY as
        # `_profile`, that getattr returned None on every call and the block reported
        # `profile_registered: {}` / `profile_registered_count: 0` for a profile that
        # demonstrably registered keys. Measured 2026-08-15 against 3f9a2c1. The block
        # added to instrument the new capability was itself inert, and it failed in the
        # SAFE-LOOKING direction: a well-formed zero, indistinguishable from a profile
        # that genuinely registers nothing (the epistemics rule rule-negative-control
        # — an instrument that can only report one answer is not an instrument).
        self.profile = self._profile
        self.raw: Dict[str, Any] = dict(self._profile.raw or {})
        self.available = True
        self.domain_id = self._profile.domain_id
        self.path = self._profile.path
        self.resolved_from = resolved_from
        self.detail = detail
        fmt = os.path.splitext(self.path)[1].lstrip(".").lower() if self.path else "dict"
        name = os.path.basename(self.path) if self.path else "<dict>"
        self.source = f"{fmt or 'dict'}:{name} (resolved_from={resolved_from}" + (
            f", {detail})" if detail else ")")

    def prefixes(self) -> Optional[Set[str]]:
        out: Set[str] = set()
        for item in self.raw.get("prefixes") or []:
            if isinstance(item, dict):
                p = item.get("prefix")
            else:
                p = item
            if p:
                out.add(str(p))
        return out

    def properties(self) -> Optional[Set[str]]:
        out: Set[str] = set()
        for item in self.raw.get("properties") or []:
            if isinstance(item, dict):
                name = item.get("name") or item.get("property")
            else:
                name = item
            if name:
                out.add(str(name))
        return out

    def edge_types(self) -> Optional[Set[str]]:
        """The declared edge-type vocabulary: `relationship_verbs` UNION `edge_types[]`.

        ⛔ `edge_types[]` WAS MISSING, and its absence was invisible in exactly the way
        this tool exists to prevent. A profile declaring `{type: clinical.CONTRAINDICATES,
        layer: causal}` had that type in NO declared set, so:
          * when the corpus used it, it showed as `used_but_unregistered` — the audit
            accused the corpus of drift against a vocabulary the profile HAD declared;
          * when the corpus did not use it, it showed as nothing at all — a
            registered-but-unused edge type invisible to the one tool built to find that.
        Both diffs were wrong, in opposite directions, from one missing union.

        The two declaration sites are not redundant: `relationship_verbs` registers a
        BARE verb so `[A] GOVERNS [B]` is a recognised triple, while `edge_types[]`
        registers a type's LAYER (and, since 2026-08-15, the property key that mints it).
        A namespaced type only ever appears in the second.
        """
        out: Set[str] = set()
        rv = self.raw.get("relationship_verbs")
        if isinstance(rv, dict):
            for tier in ("core", "domain"):
                for v in rv.get(tier) or []:
                    if v:
                        out.add(str(v).upper())
        # `Profile.edge_types` is the SAME projection the loader validated, never a
        # second reader — a second one could disagree with what actually loaded.
        try:
            out.update(self._profile.edge_types or {})
        except Exception:  # noqa: BLE001 — a malformed profile must not kill the audit
            pass
        return out


def resolve_profile_adapter(profile: Any = None,
                            corpus: Optional[str] = None) -> ProfileAdapter:
    """Pick the adapter for `profile`, falling back to the corpus's REGISTERED profile.

    * a `ProfileAdapter` instance  -> used as-is.
    * a path/dict/`factlattice.Profile` -> loaded (`.lts` / `.yaml` / `.json`, one
      loader, one dict — see `YamlProfileAdapter`'s misnomer note).
    * `None` + a `corpus` id       -> the `profile` key on that corpus's entry in
      the corpus registry (`factlattice.registry`, via `load_corpus_config`; a broken registry RAISES
      `RegistryError`, never an empty answer). THIS is the wire that was missing: the audit could
      only ever see a profile a caller hand-typed, so `lts_profile_audit(corpus=...)`
      reported `no profile loaded` for corpora that had one sitting right there.
    * nothing resolvable          -> NullProfileAdapter (used-side only, and it SAYS so).
    """
    if isinstance(profile, ProfileAdapter):
        return profile
    if profile is not None:
        return YamlProfileAdapter(profile, resolved_from="explicit-argument")
    if corpus:
        from factlattice.profile import load_corpus_config
        spec = (load_corpus_config() or {}).get(corpus)
        path = spec.get("profile") if isinstance(spec, dict) else None
        if path and os.path.isfile(path):
            return YamlProfileAdapter(path, resolved_from="corpus-config",
                                      detail=f"corpus={corpus}")
        if path:
            adapter = NullProfileAdapter()
            adapter.source = (f"corpus {corpus!r} registers profile {path!r}, "
                              "WHICH DOES NOT EXIST — declared side unavailable")
            return adapter
    return NullProfileAdapter()


# =========================================================================== #
# [C7 / Era 0] THE TRUTHFUL WIRING CHECK — is `property_edges=` REACHABLE at all?
# =========================================================================== #
# PARSER-ENGINEERING-SPEC.md rev 2 C7: "`binding.minting_keys_for` +
# `profile_audit._property_edges_section` split `{declared, threaded, in_effect}`; a
# declared-not-threaded key is `FL-W106` at ERROR severity (an engine defect, not an
# authoring one)."
#
# MEASURED 2026-08-16, by grep, not by inference: `property_edges=` is a KEYWORD
# PARAMETER OF `factlattice.parser.parse_file` / `parse_text` AND NOTHING ELSE —
#     grep -rl "property_edges=" factlattice factlattice_engine
# returns exactly ONE file, `factlattice/parser.py` (the two `def`s themselves). It is
# never PASSED as an argument by `factlattice.index_builder.build_index`,
# `factlattice.corpus.parse()` (`_reparse_dir` / `_build_over_files`, both call
# `parse_file` without it), `factlattice_engine.incremental.RootIndex._parse`, or
# `factlattice_engine.config.build_partition` / `build_merged_partition` (which do not
# even carry it: `domain_profiles.ProfileEdgeConfig`, the object those two functions
# thread, has no `property_edges` FIELD to carry — a disjoint profile representation
# from `factlattice.profile.Profile.property_edges`, review V5's "two disjoint profile
# systems" one layer deeper than V5 already measured it).
#
# ⇒ A profile's `property_edges` registration is reachable from EXACTLY ONE call
# shape in this codebase: a hand-written `parse_text(..., property_edges=prof.
# property_edges)` — which is what `tests/test_property_edge_minting.py` does, and
# ONLY there. No production entry point (the linter, the live MCP query engine, the
# incremental self-heal path) ever constructs that call. `binding.minting_keys_for`
# correctly computes what WOULD mint under the registration; this function checks
# whether anything downstream of a real parse ever ASKS it to.
#
# THIS IS A STATIC / STRUCTURAL CHECK, DELIBERATELY, NOT AN EDGE-COUNT HEURISTIC.
# An edge-count check ("does the registered TYPE appear among the corpus's live
# edges?") would read a FALSE POSITIVE the moment the same edge TYPE is also produced
# by the built-in table or by an authored `## Relationships` triple — exactly the kind
# of coincidental agreement the epistemics rule's rule-self-verification warns
# against. Reachability is a fact about the CALL GRAPH, not about the graph.
def _property_edges_wiring() -> Dict[str, Any]:
    """Does a profile-registered minting key ACTUALLY MINT through each production
    path? Returns `{checked, reachable_from, reachable_from_any_production_path}`.

    ⛔ THIS IS A ROUND-TRIP, NOT A SIGNATURE INSPECTION, AND THE CHANGE WAS FORCED BY
    THE FIX ITSELF. Era 0's version asked `"property_edges" in signature(fn).parameters`.
    After C3 that predicate is WRONG IN BOTH DIRECTIONS at once:
      * `build_index` still ACCEPTS a deprecated `property_edges=` kwarg, so the old
        check would answer "reachable" for a parameter production no longer uses;
      * `RootIndex` / `_reparse_dir` now thread the vocabulary through `config=` and
        no longer name `property_edges` at all, so the old check would answer
        "unreachable" for the paths that were just FIXED.
    An instrument that reports success because a parameter EXISTS, rather than because
    an edge ARRIVED, is the failure class the epistemics rule names. So this parses
    a probe document carrying a registered key through each real entry point and looks
    for the edge on the other side. It cannot be fooled by a signature.
    """
    import os
    import tempfile

    PROBE_DOC = ("# Wiring probe [SPEC:PEW-ROOT]\n\n"
                 "## [PEW-A] A\n- exemplifies: [PEW-B]\n\n## [PEW-B] B\n")
    PROBE_KEY = {"exemplifies": ("EXEMPLIFIES", "semantic", False)}
    PROBE_PROFILE = {
        "domain": {"id": "pew"},
        "edge_types": [{"property_key": "exemplifies", "type": "EXEMPLIFIES",
                        "layer": "semantic"}],
    }

    def _minted(edges) -> bool:
        return any(getattr(e, "type", None) == "EXEMPLIFIES"
                   or (isinstance(e, dict) and e.get("type") == "EXEMPLIFIES")
                   for e in edges)

    checks: Dict[str, bool] = {}
    tmp = tempfile.mkdtemp(prefix="factlattice-pew-")
    try:
        with open(os.path.join(tmp, "probe.lts"), "w", encoding="utf-8") as fh:
            fh.write(PROBE_DOC)

        from factlattice.index_builder import build_index
        from factlattice.parser_config import ParserConfig
        cfg = ParserConfig.from_kwargs(property_edges=PROBE_KEY)

        try:
            idx = build_index(tmp, corpus_id="pew", domain_suffix="pew", config=cfg)
            checks["factlattice.index_builder.build_index"] = _minted(
                [e for lay in idx["layers"].values() for e in lay["edges"]])
        except Exception:                                    # pragma: no cover
            checks["factlattice.index_builder.build_index"] = False

        try:
            from factlattice.corpus import parse as corpus_parse
            pc = corpus_parse(source=tmp, profile=PROBE_PROFILE)
            checks["factlattice.corpus.parse (dir mode -- lts_lint / lts_preview)"] = \
                _minted(pc.edges)
        except Exception:                                    # pragma: no cover
            checks["factlattice.corpus.parse (dir mode -- lts_lint / lts_preview)"] = False

        try:
            from factlattice.corpus import parse as corpus_parse
            pc = corpus_parse(paths=[os.path.join(tmp, "probe.lts")],
                              profile=PROBE_PROFILE)
            checks["factlattice.corpus.parse (file-set mode)"] = _minted(pc.edges)
        except Exception:                                    # pragma: no cover
            checks["factlattice.corpus.parse (file-set mode)"] = False

        try:
            from factlattice_engine.incremental import RootIndex
            ri = RootIndex(tmp, config=cfg)
            ri.full_build()
            checks["factlattice_engine.incremental.RootIndex (live MCP query engine)"] = \
                _minted([e for rel_edges in ri._edges.values() for e in rel_edges])
        except Exception:                                    # pragma: no cover
            checks["factlattice_engine.incremental.RootIndex (live MCP query engine)"] = False
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    return {
        "checked": sorted(checks),
        "reachable_from": sorted(k for k, v in checks.items() if v),
        "unreachable_from": sorted(k for k, v in checks.items() if not v),
        "reachable_from_any_production_path": any(checks.values()),
        "reachable_from_every_production_path": bool(checks) and all(checks.values()),
        "method": "round-trip: a probe document carrying a registered minting key is "
                  "parsed through each entry point and the resulting edge set is "
                  "searched for the minted edge. Deliberately NOT a signature check "
                  "— see this function's docstring.",
    }


# =========================================================================== #
# EDGE-BEARING PROPERTY KEYS — built-in vs. profile-registered vs. actually used
# =========================================================================== #
def _property_edges_section(adapter, nodes) -> Dict[str, Any]:
    """Which property keys MINT here, and which registered ones nobody uses.

    Added 2026-08-15 with the profile-registrable edge keys (the maintainer's ruling). Without
    this block a profile could register a key and get NO signal either way: an unused
    registration is invisible, and a key that mints is indistinguishable from one that
    merely parses. `lts_profile_audit` is the one tool built to find exactly that drift,
    so the new vocabulary has to appear in it or the capability ships half-instrumented.

    ⛔ The BUILT-IN side is worth reading even with no profile loaded. Measured over the
    acme corpora on the day the table was widened: 9 of 15 built-in keys had ZERO
    corpus use (`implements`, `issuer`, `used-by`, `tested-by`, `next-step`, `if-true`,
    `if-false`, `branches`, `converges-to`) while the corpus leaned hard on spellings the
    table did not carry. The table and the corpus had drifted in BOTH directions at once.
    """
    from factlattice.parser import FLOW_PROPERTY_EDGES, REL_PROPERTY_EDGES

    builtin = set(REL_PROPERTY_EDGES) | set(FLOW_PROPERTY_EDGES)
    registered: Dict[str, Any] = {}
    prof = getattr(adapter, "profile", None)
    if prof is not None:
        registered = dict(getattr(prof, "property_edges", None) or {})

    used_keys: Set[str] = set()
    for nd in nodes:
        used_keys.update((nd.get("properties") or {}).keys())

    minting = builtin | set(registered)

    # [C7 / Era 0] THE TRUTHFUL SPLIT. `minting_keys_in_effect` (below, UNCHANGED —
    # nothing that reads it today breaks) answers "what WOULD mint if `property_edges`
    # reached the parser". `wiring` answers whether it does. `threaded` is the
    # INTERSECTION the two together make honest: built-ins are ALWAYS threaded (they
    # need no parameter — `REL_PROPERTY_EDGES`/`FLOW_PROPERTY_EDGES` are compiled into
    # every call site); a PROFILE-REGISTERED key is threaded only if
    # `wiring.reachable_from_any_production_path` is true. Measured today: it is not,
    # for every profile, on every production path — so `declared_not_threaded` equals
    # the full registered set until C4 (Era 1) lands the wiring.
    wiring = _property_edges_wiring()
    # ⛔ `EVERY` PATH, NOT `ANY`. "Reachable from at least one entry point" IS the V5b
    # condition — the state in which the same (document, profile) pair produced a
    # different graph depending on who asked. Calling that "threaded" would repeat V6
    # ("audit reports declared as in-effect") one level up, which is precisely the
    # mistake this field was created to stop making.
    threaded_registered = (set(registered)
                           if wiring["reachable_from_every_production_path"]
                           else set())
    declared_not_threaded = sorted(set(registered) - threaded_registered)
    threaded = builtin | threaded_registered

    return {
        "builtin": sorted(builtin),
        "profile_registered": {k: list(v) for k, v in sorted(registered.items())},
        "profile_registered_count": len(registered),
        # ⛔ KEPT FOR BACK-COMPAT, BUT READ IT KNOWING WHAT IT ANSWERS: "declared or
        # built-in", NOT "actually minting on a served graph". This is the exact V6
        # shape ("audit surfaces report declared keys as in-effect") that a truthful
        # reader must not repeat — use `threaded_keys_in_effect` for the real question.
        "minting_keys_in_effect": sorted(minting),
        "registered_but_unused": sorted(set(registered) - used_keys),
        "builtin_but_unused": sorted(builtin - used_keys),
        "minting_keys_used": sorted(minting & used_keys),
        # THE TRUTHFUL FIELDS (C7).
        "wiring": wiring,
        "threaded_keys_in_effect": sorted(threaded),
        "declared_not_threaded": [
            {"key": k, "code": "FL-W106", "severity": "error",
             "reason": UNREACHABLE_KEY_REASON}
            for k in declared_not_threaded
        ],
    }


# =========================================================================== #
# EDGE TYPES — core vs. profile-registered, and WHICH DECLARATION SITE said so
# =========================================================================== #
def _edge_type_registration(adapter, used_edges: Set[str]) -> Dict[str, Any]:
    """Attribute every declared edge type to its source, and diff each source SEPARATELY.

    Added 2026-08-15 closing the edge-TYPE half of the registrability ruling. The plain
    `declared_but_unused` diff answers "is anything declared and unused" but not "WHO
    declared it", and those are different questions the moment a profile can register an
    edge type of its own: a core type nobody uses is unremarkable, while a
    PROFILE-REGISTERED type nobody uses is the exact drift this tool exists to surface —
    somebody wrote a declaration and no author ever reached for it.

    `core_shadowed` is the negative control on the loader, read from the OTHER side.
    `profile.load_profile` refuses a bare core redefinition, so this list must ALWAYS be
    empty; if it is ever non-empty, a profile reached the audit through a path that did
    not run the guard, and the number says so instead of the guard's absence being
    invisible.
    """
    from factlattice.parser import CORE_EDGE_LAYER

    registered: Dict[str, str] = {}
    prof = getattr(adapter, "profile", None)
    if prof is not None:
        try:
            registered = dict(getattr(prof, "edge_types", None) or {})
        except Exception:  # noqa: BLE001
            registered = {}

    verbs: Set[str] = set()
    rv = getattr(adapter, "raw", {}).get("relationship_verbs")
    if isinstance(rv, dict):
        for tier in ("core", "domain"):
            for v in rv.get(tier) or []:
                if v:
                    verbs.add(str(v).upper())

    core = set(CORE_EDGE_LAYER)
    return {
        "core": sorted(core),
        "profile_registered": dict(sorted(registered.items())),
        "profile_registered_count": len(registered),
        "relationship_verbs": sorted(verbs),
        "registered_but_unused": sorted(set(registered) - used_edges),
        "verbs_declared_but_unused": sorted(verbs - used_edges),
        "used_but_neither_core_nor_declared": sorted(
            used_edges - core - set(registered) - verbs),
        # MUST be empty — the loader refuses a bare core redefinition (profile.py R1).
        "core_shadowed": sorted(t for t in registered if "." not in t and t in core),
    }


# =========================================================================== #
# REVIEW EXTENSIONS (SPEC §10.4, A-E5) — what the profile registers, whether it holds, and the look-alikes
# =========================================================================== #
def _review_extensions_section(adapter, corpus_roots) -> Optional[Dict[str, Any]]:
    """The profile's review-extension registrations, their validation, and `unprojected_review_extension`.

    ⛔ THE LOUD HALF OF AUDIT D4. The `.lts` profile loader projects only the node kinds it knows and skips
    the rest silently, so a registration under a misspelled kind (`REVEXT-X`, the extension DOCUMENT's own
    kind) loads clean and registers nothing. `unprojected_review_extension` lists every profile node that
    carries `path` and `ext-id` under a kind the loader does not know. A clean empty list is therefore a
    statement about the profile FILE, not about the projection (the projection is what it checks).

    ABSENT WHEN NOT TRIGGERED (SDLC 7.6 response-key rule): returns None, and the key is then not in the answer,
    for a corpus with no profile and for a profile that registers no extension, names no `review-output` and has
    no look-alike. A call on such a corpus is byte-identical to what it was before this section existed.
    """
    prof = getattr(adapter, "profile", None)
    if prof is None:
        return None
    from factlattice.profile import unprojected_review_extensions, validate_review_extensions
    regs = prof.review_extensions
    out_raw = prof.review_output
    unprojected = unprojected_review_extensions(getattr(prof, "path", None))
    findings = validate_review_extensions(prof, corpus_roots=corpus_roots or None)
    if not (regs or out_raw or unprojected or findings):
        return None
    return {
        "available": True,
        "registered_count": len(regs),
        "registered": [{"ext_id": r.ext_id, "path": r.path, "realpath": prof.resolve_path(r.path)}
                       for r in regs],
        "review_output": {"value": out_raw,
                          "realpath": prof.resolve_path(out_raw) if out_raw else None},
        "findings": findings,
        "unprojected_review_extension": unprojected,
    }


# =========================================================================== #
# diff helper — a used set vs. a declared set (declared may be absent)
# =========================================================================== #
def _diff_section(used: Set[str], declared: Optional[Set[str]]) -> Dict[str, Any]:
    """Build the {used, declared, used_but_unregistered, declared_but_unused} block.
    `declared is None` -> declared side + both diffs marked "no profile loaded"."""
    used_sorted = sorted(used)
    if declared is None:
        return {
            "used": used_sorted,
            "used_count": len(used),
            "declared": "no profile loaded",
            "used_but_unregistered": "no profile loaded",
            "declared_but_unused": "no profile loaded",
        }
    return {
        "used": used_sorted,
        "used_count": len(used),
        "declared": sorted(declared),
        "declared_count": len(declared),
        "used_but_unregistered": sorted(used - declared),
        "declared_but_unused": sorted(declared - used),
    }


def _corpus_roots(agg, corpus: Optional[str]) -> List[str]:
    """The roots of `corpus` as the serving store knows them (best effort; [] when unknown)."""
    try:
        store = getattr(getattr(agg, "_get_tools", lambda: None)(), "store", None)
        part = store.get_partition(corpus) if store is not None and corpus else None
        return [str(r) for r in (getattr(part, "root_paths", None) or [])]
    except Exception:  # noqa: BLE001 — the roots only sharpen a check; their absence must not kill the audit
        return []


def _parse_scope(required_scope: Any) -> Dict[str, str]:
    """Parse a node-filter scope into `find` filter kwargs.

    Accepts a dict (returned as-is) or a compact string `"prefix=DEC"` /
    `"prefix=DEC,area=decisions"` (comma-separated `key=value`). `None`/"" -> {}.
    """
    if not required_scope:
        return {}
    if isinstance(required_scope, dict):
        return {k: v for k, v in required_scope.items() if v is not None}
    out: Dict[str, str] = {}
    for pair in str(required_scope).split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            out[k] = v
    return out


# =========================================================================== #
# the op
# =========================================================================== #
def profile_audit(agg, *, corpus: Optional[str] = None, profile: Any = None,
                  required_property: Optional[str] = None,
                  required_scope: Any = None, tail_threshold: int = 5,
                  **filters) -> dict:
    """Measure `corpus`'s actual vocabulary against its declared `profile`.

    Composes `LtsAggregator._find_all` (the SAME complete, freshness-gated fetch
    `group_by` uses) + `count_by_edge_type`. FRESHNESS-GATED exactly like the other
    analytics ops: a STALE corpus propagates the loud stale marker VERBATIM, no numbers.

    Returns `{success, corpus, profile: {available, source, ...}, prefixes, properties,
    edge_types, conformance, filters}`:
      * prefixes    — used set + per-prefix count; declared; both diffs (used prefixes
                      are diffed in their `<segment>-` normalized form against the
                      declared prefix strings).
      * properties  — used set + per-property frequency; the frequency distribution
                      split at `tail_threshold` (core `>=` vs tail `<`); declared;
                      both diffs.
      * edge_types  — used + counts; declared; both diffs.
      * conformance — for `required_property` over `required_scope`
                      (e.g. `"prefix=DEC"`): present/total + rate. Omitted if no
                      `required_property` given.
    """
    adapter = resolve_profile_adapter(profile, corpus=corpus)

    # -- USED side: one complete, freshness-gated fetch (STALE -> verbatim marker) --
    nodes, res = agg._find_all(corpus, filters)
    if nodes is None:
        return res  # not-found / STALE, propagated verbatim (no numbers)

    # ---- prefixes: type_prefix histogram over the found set --------------------
    prefix_counts: Dict[str, int] = {}
    for nd in nodes:
        p = nd.get("type_prefix")
        if p:
            prefix_counts[str(p)] = prefix_counts.get(str(p), 0) + 1
    used_prefixes = set(prefix_counts)
    # A declared prefix carries its trailing '-' (`ACME-`, `DP-CIO-`); a used type_prefix
    # is the pre-first-hyphen segment (`ACME`, `DP`). Diff in the `<segment>-` form so
    # `ACME` matches declared `ACME-`, while `DP` (declared only as `DP-CIO-`) is drift.
    declared_prefixes = adapter.prefixes()
    used_prefixes_norm = {p + "-" for p in used_prefixes}
    prefixes_section = _diff_section(used_prefixes_norm, declared_prefixes)
    prefixes_section["counts"] = dict(sorted(
        prefix_counts.items(), key=lambda kv: (-kv[1], kv[0])))

    # ---- properties: per-key frequency + the power-law tail split --------------
    prop_freq: Dict[str, int] = {}
    for nd in nodes:
        for key in (nd.get("properties") or {}):
            if key in _INJECTED_PROPERTY_KEYS:
                continue
            prop_freq[key] = prop_freq.get(key, 0) + 1
    used_properties = set(prop_freq)
    declared_properties = adapter.properties()
    properties_section = _diff_section(used_properties, declared_properties)
    thr = int(tail_threshold)
    core = {k: v for k, v in prop_freq.items() if v >= thr}
    tail = {k: v for k, v in prop_freq.items() if v < thr}
    properties_section["frequency"] = dict(sorted(
        prop_freq.items(), key=lambda kv: (-kv[1], kv[0])))
    properties_section["distribution"] = {
        "tail_threshold": thr,
        "core_count": len(core),          # properties used >= threshold
        "tail_count": len(tail),          # the long tail, used < threshold
        "core": dict(sorted(core.items(), key=lambda kv: (-kv[1], kv[0]))),
        "tail": sorted(tail.keys()),
    }

    # ---- edge types: reuse the gated edge-type histogram -----------------------
    et = agg.count_by_edge_type(corpus=corpus, **filters)
    if not et.get("success"):
        return et  # STALE/not-found on the edge path -> propagate verbatim
    edge_counts: Dict[str, int] = dict(et.get("groups") or {})
    used_edges = set(edge_counts)
    declared_edges = adapter.edge_types()
    edges_section = _diff_section(used_edges, declared_edges)
    edges_section["counts"] = dict(sorted(
        edge_counts.items(), key=lambda kv: (-kv[1], kv[0])))
    edges_section["total_edges"] = et.get("total_edges")
    edges_section["resolved_edges"] = et.get("resolved_edges")
    edges_section["registration"] = _edge_type_registration(adapter, used_edges)

    # ---- the AGREEMENT CHECK: intrinsic doctag vs. corpus-config registration ----
    # Two independent declarations of the same fact; their disagreement is a finding.
    # Free here — both sides already exist (see `profile_registration`). Best-effort:
    # a failure to run the cross-check must never take down the audit that DID run.
    registration: Dict[str, Any]
    try:
        from .profile_registration import audit_profile_registration
        registration = audit_profile_registration(
            getattr(getattr(agg, "_get_tools", lambda: None)(), "store", None),
            corpus=corpus)
    except Exception as exc:  # noqa: BLE001
        registration = {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    out: Dict[str, Any] = {
        "success": True,
        "corpus": corpus,
        "profile": {
            "available": adapter.available,
            "source": adapter.source,
            "resolved_from": getattr(adapter, "resolved_from", "none"),
            "domain_id": getattr(adapter, "domain_id", None),
            "path": getattr(adapter, "path", None),
        },
        "registration": registration,
        "node_total": len(nodes),
        "prefixes": prefixes_section,
        "properties": properties_section,
        "edge_types": edges_section,
        "property_edges": _property_edges_section(adapter, nodes),
        "filters": {k: v for k, v in filters.items() if v is not None},
    }
    review_ext = _review_extensions_section(adapter, _corpus_roots(agg, corpus))
    if review_ext is not None:                 # ABSENT when not triggered (SDLC 7.6, response-key rule)
        out["review_extensions"] = review_ext

    # ---- required-property conformance (optional) ------------------------------
    if required_property:
        scope = _parse_scope(required_scope)
        scope_nodes, sres = agg._find_all(corpus, {**filters, **scope})
        if scope_nodes is None:
            return sres  # STALE/not-found on the scoped fetch -> verbatim
        present = 0
        for nd in scope_nodes:
            props = nd.get("properties") or {}
            if required_property in props:
                present += 1
        total = len(scope_nodes)
        out["conformance"] = {
            "required_property": required_property,
            "scope": scope or "(all found nodes)",
            "present": present,
            "total": total,
            "rate": (present / total) if total else 0.0,
        }

    if not corpus and "stale_corpora" in res:
        out["stale_corpora"] = res["stale_corpora"]
    return out
