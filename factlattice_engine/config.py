"""Server config: corpus_id (= project) -> labeled roots.

The ENGINE is generic; this mapping is the ONLY deployment-specific surface. A corpus
is a project: `corpus_id = project id`. As of [ENH-2]/[ENH-3] a project's corpus can
carry up to THREE LABELED roots — `decisions` / `specs` / `artifacts` — merged into ONE
Partition so `lts_find(corpus=..., area="decisions", ...)` can scope a query to just
one of them (query surface: "query within project.specs, project.decisions,
project.artifacts").

The registry is chosen by `factlattice.registry.resolve_registry()` -- the ONLY code that reads the
registry environment variables or the per-user pointer file (order: the registry variable, which must
exist and parse and never falls back; then the per-user pointer file (see `factlattice.registry`); then, for
the first-run trial only, the built-in EXAMPLE registry below). Its file is a JSON object describing the
loadable corpus set, in either the single-root shape `{corpus_id: {root, domain_suffix}}` or the
multi-root shape `{corpus_id: {roots: [{path, area[, only_file]}, ...], domain_suffix}}`; the root
variable sets the base directory relative paths resolve under (default: the registry file's directory;
for the example, the working directory).

The `DECLARED_PROJECTS` list (it lives in `factlattice.registry`, re-exported here) is a GENERIC EXAMPLE
registry, NOT a real deployment. It shows the shape of a project registry. Replace it by configuring
your own registry and leave it untouched.

Notes on the multi-root model this example illustrates:
  * decision/artifact files are often CO-LOCATED as siblings in a shared directory
    (e.g. every project's decision graph under one `decisions/` dir). `only_file`
    pins a root to exactly one file's nodes/edges so siblings don't leak across corpora.
  * a root that doesn't exist on disk degrades gracefully to "not found" and is SKIPPED
    (never crashes) — see `incremental.CorpusIndex.live_roots` / `build_merged_partition`.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from factlattice import CorpusFileError, build_index, registry
from factlattice.domain_profiles import (
    ProfileEdgeConfig,
    default_profile_dir,
    load_profile_edge_config,
)
from .incremental import CorpusIndex
from .privacy import private_reason
from .store import LtsStore, Partition

# The example registry (a generic illustration, first-run trial only) lives in `factlattice.registry`
# beside the lookup that selects it; re-exported so `config.DECLARED_PROJECTS` keeps meaning what it did.
DECLARED_PROJECTS: List[dict] = registry.EXAMPLE_PROJECTS


def declared_projects() -> List[dict]:
    root = registry.example_root()
    return [{**pr, "decision_lts_exists": os.path.isfile(os.path.join(root, pr["decision_lts"]))}
            for pr in DECLARED_PROJECTS]


# --- The LOADABLE corpus set the MCP server actually builds -------------------
def default_corpora() -> Dict[str, dict]:
    """The registry's corpus entries, from THE lookup (`registry.resolve_registry`). Raises
    `registry.RegistryError` (`registry-not-found`) on a broken configured registry: there is no fallback."""
    return registry.resolve_registry().corpora


# --- domain-profile edge config (D5 layer table + D6 namespace registration) --------
# The engine's partition-build path loads the domain profiles ONCE and threads the
# derived `{namespaced_type -> layer}` map + `registered_ns` into every parse, so a
# corpus built THROUGH the engine resolves `dec.*`/`art.*` edges to their profile-
# declared layers/namespaces. A bare `parse_file`/`build_index` (no profile) is left on
# its pre-profile defaults for back-compat. The load is memoized per profile dir.
_PROFILE_EDGE_CACHE: Dict[str, ProfileEdgeConfig] = {}


_REGISTERED_PROFILE_CACHE: Dict[str, object] = {}


def registered_profile(spec: dict):
    """Load the profile a corpus's registry entry NAMES (`profile:`), memoized.

    ⛔ THIS CLOSES V5c, AND V5c IS THE ONE THAT MADE THE OTHERS UNFALSIFIABLE.
    MEASURED 2026-08-16 before the fix: `corpus-config.json` carries a `profile:` key
    per corpus, and it was read by `analytics.profile_audit` and
    `analytics.profile_registration` — the AUDIT surfaces — and by NO graph-building
    path at all. `build_store` / `build_partition` / `build_merged_partition` used only
    `engine_profile_config()`, i.e. the SHIPPED EXAMPLE `domain-profiles/` directory
    (namespaces art/cln/dec/ext/mkr/swe). The deployment's own registered profile,
    which is where all 17 of its domain verbs and its ONE `property_edges` key live,
    reached the served graph through no path whatsoever.

    ⭐ WHY THAT MATTERS MORE THAN IT SOUNDS. Threading `property_edges=` (C4) WITHOUT
    this would have produced a green wire with nothing flowing through it: the
    entry-point-equivalence control passes its profile explicitly, so it would have
    gone GREEN while the real deployment gained exactly ZERO edges — an instrument
    agreeing with the wrong answer, which is the failure class
    the epistemics rule names. The fix is not "thread the parameter"; it is
    "thread the parameter AND give it something to carry".

    A missing/unreadable/unregistered profile yields None and the engine behaves
    exactly as before — a corpus that registers nothing loses nothing.
    """
    path = spec.get("profile") if isinstance(spec, dict) else None
    if not path or not os.path.isfile(path):
        return None
    cached = _REGISTERED_PROFILE_CACHE.get(path)
    if cached is None:
        from factlattice.profile import load_profile
        try:
            cached = load_profile(path)
        except Exception:
            # A profile that fails to load must not take the corpus down with it: the
            # graph is still servable on core vocabulary. The failure is NOT silent —
            # `analytics.profile_audit` reports it against the same registry entry.
            return None
        _REGISTERED_PROFILE_CACHE[path] = cached
    return cached


def parser_config_for(spec: Optional[dict] = None,
                      profile: Optional[ProfileEdgeConfig] = None):
    """The ONE `ParserConfig` the live engine parses a corpus against. [C3/C4/V5c]

    Merges the engine-wide `ProfileEdgeConfig` (D5 layers + D6 namespaces) with the
    corpus's OWN registered profile (verbs + minting keys + declared keys) through
    `ParserConfig.from_profile`, the single sanctioned seam.
    """
    from factlattice.parser_config import ParserConfig
    if profile is None:
        profile = engine_profile_config()
    return ParserConfig.from_profile(profile=registered_profile(spec or {}),
                                     edge_config=profile)


def engine_profile_config(profile_dir: Optional[str] = None) -> ProfileEdgeConfig:
    """The merged domain-profile edge config for the engine (memoized per dir).

    `profile_dir` defaults to `FACTLATTICE_PROFILE_DIR` / the shipped `domain-profiles`
    dir (see `default_profile_dir`). A missing/empty dir yields an EMPTY config
    (`registered_ns == {'core'}`, no edge layers), leaving the engine's behaviour
    identical to the pre-profile engine on a deployment with no profiles.
    """
    key = profile_dir or default_profile_dir()
    cfg = _PROFILE_EDGE_CACHE.get(key)
    if cfg is None:
        cfg = load_profile_edge_config(key)
        _PROFILE_EDGE_CACHE[key] = cfg
    return cfg


# --- single-root partition build (the legacy registry shape) --
def build_partition(corpus_id: str, root: str, domain_suffix: str,
                    built_at: Optional[str] = None,
                    profile: Optional[ProfileEdgeConfig] = None,
                    spec: Optional[dict] = None) -> Partition:
    """Build an in-memory Partition from ONE corpus root directory (no disk index needed).

    Reuses the parser (`factlattice.build_index`); constructs the Partition directly so
    the MCP server can serve on-demand with a session-fresh index. `profile` supplies the
    D5/D6 edge config; when None it is loaded from the default profile dir.
    """
    if built_at is None:
        built_at = f"config:{corpus_id}"
    if profile is None:
        profile = engine_profile_config()
    cfg = parser_config_for(spec, profile)
    index = build_index(corpus_root=root, corpus_id=corpus_id,
                        domain_suffix=domain_suffix, built_at=built_at, config=cfg)
    part = Partition(index, source_path=root)
    # [LEC-SELF-HEAL] attach an UNSEEDED rebuild recipe. This path builds through the
    # parser's own `build_index` (it wants the full index schema: alias_index,
    # id_collisions, adjacency), so there is no parse cache to inherit — the FIRST heal
    # therefore falls back to a full re-index (observable as `mode="full"`), and every
    # heal after it is surgical.
    part.corpus_index = CorpusIndex(corpus_id, domain_suffix,
                                    [{"path": root, "area": None}], config=cfg)
    return part


# --- multi-root, area-labeled partition build ([ENH-2]/[ENH-3]) ---------------
# The per-root parse (`_build_root_contribution`) MOVED to `incremental.CorpusIndex`
# ([LEC-SELF-HEAL]): the cold build and the self-heal re-index must be the SAME code, or
# a healed index and a cold-start index are only equal by two implementations agreeing.
# `CorpusIndex.live_roots()` carries the graceful-skip rule (a root directory that does
# not exist, or an `only_file` pin whose file is absent, contributes nothing);
# `CorpusIndex._materialize()` carries the only_file filter + `area` stamping + merge.
def build_merged_partition(corpus_id: str, roots: List[dict], domain_suffix: str,
                           built_at: Optional[str] = None,
                           profile: Optional[ProfileEdgeConfig] = None,
                           spec: Optional[dict] = None,
                           ) -> Optional[Partition]:
    """Build ONE Partition merging multiple area-labeled roots into a single corpus
    ([ENH-2]: `lts_find(area=...)` scopes to one). Roots that don't exist yet are
    SKIPPED (most example projects are registered-but-empty). Returns None if NO root
    contributed anything (nothing to register). `profile` supplies the D5/D6 edge config
    (layer map + registered namespaces); when None it is loaded from the default dir."""
    if built_at is None:
        built_at = f"config:{corpus_id}"
    if profile is None:
        profile = engine_profile_config()
    # [LEC-SELF-HEAL] the cold build runs THROUGH the CorpusIndex — the very same object
    # a later self-heal re-indexes through. That is what makes a healed index identical to
    # a cold-start index BY CONSTRUCTION rather than by two implementations agreeing.
    # It also removes a whole redundant corpus read: the parse cache certifies the content
    # hash of every file it parsed, so `files_hash` no longer needs a second full pass
    # (`combined_files_hash`) over the same bytes.
    ci = CorpusIndex(corpus_id, domain_suffix, roots,
                     config=parser_config_for(spec, profile))
    merged_nodes, merged_layers, root_paths, files_hash = ci.cold_build(as_rebuild=False)
    if not root_paths:
        return None
    index = {
        "source_corpus": {"corpus_id": corpus_id, "domain_suffix": domain_suffix,
                          "files_hash": files_hash,
                          "root_paths": root_paths},
        "nodes": merged_nodes, "layers": merged_layers,
        # Files parsed but deliberately NOT folded into the content graph because they
        # declare themselves domain profiles (`document_tag: PROFILE`). Carried on the
        # index so the exclusion is visible to an operator and free to audit.
        "profile_documents": list(ci.profile_documents),
        "stats": {"total_nodes": len(merged_nodes),
                  "total_edges": sum(len(ld["edges"]) for ld in merged_layers.values()),
                  "profile_documents": len(ci.profile_documents)},
    }
    part = Partition(index, source_path=";".join(root_paths))
    part.corpus_index = ci
    return part


def build_store(corpora: Optional[Dict[str, dict]] = None) -> LtsStore:
    """Build a LtsStore in-memory from the corpus config, skipping empty/missing roots."""
    corpora = corpora if corpora is not None else default_corpora()
    profile = engine_profile_config()  # load the D5/D6 domain-profile config ONCE
    store = LtsStore()
    for cid, spec in corpora.items():
        # [ISSUE-NONREGULAR-MEMBER] ONE bad member fails ONLY its own corpus, loudly: the corpus
        # is not served (no silent partial view, [INV-LTS-FRESH] GAP-1), the failure is recorded with its code and
        # root-relative path, and every other corpus keeps serving. Nothing else is caught here: any other exception still
        # propagates to `build_tools`, which treats it as a registry problem as before.
        try:
            if "roots" in spec:
                part = build_merged_partition(cid, spec["roots"],
                                              spec.get("domain_suffix", f"proj.{cid}"),
                                              profile=profile, spec=spec)
            else:
                root = spec.get("root")
                part = None
                if root and os.path.isdir(root):
                    part = build_partition(cid, root, spec.get("domain_suffix", f"proj.{cid}"),
                                           profile=profile, spec=spec)
        except CorpusFileError as exc:
            store.failed_corpora[cid] = {"code": exc.code, "path": exc.rel_path,
                                         "private": private_reason(cid, spec) is not None}
            continue
        if part is not None and part.nodes:  # only register a corpus that parsed >=1 node
            # [INV-PRIVATE-FANOUT] stamped from the registry entry, so `private: false`
            # can opt a `-personality` id out and `private: true` can opt any id in.
            part.private_reason = private_reason(cid, spec)
            store.add_partition(part)
    return store
