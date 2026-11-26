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
multi-root shape `{corpus_id: {roots: [{path, area[, only_file]}, ...], domain_suffix}}`, optionally with a
`named_roots` table beside them (expanded and stripped in `factlattice.registry`); the root
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

from factlattice import build_index, registry
from factlattice.domain_profiles import (
    ProfileEdgeConfig,
    default_profile_dir,
    default_profile_dirs,
    load_profile_edge_config,
)

from .incremental import CorpusIndex
from .privacy import private_reason
from .store import LtsStore, Partition

# The example registry (a generic illustration, first-run trial only) lives in `factlattice.registry`
# beside the lookup that selects it; re-exported so `config.DECLARED_PROJECTS` keeps meaning what it did.
DECLARED_PROJECTS: list[dict] = registry.EXAMPLE_PROJECTS


def declared_projects() -> list[dict]:
    root = registry.example_root()
    return [{**pr, "decision_lts_exists": os.path.isfile(os.path.join(root, pr["decision_lts"]))}
            for pr in DECLARED_PROJECTS]


# --- The LOADABLE corpus set the MCP server actually builds -------------------
def default_corpora() -> dict[str, dict]:
    """The registry's corpus entries, from THE lookup (`registry.resolve_registry`). Raises
    `registry.RegistryError` (`registry-not-found`) on a broken configured registry: there is no fallback."""
    return registry.resolve_registry().corpora


# --- domain-profile edge config (D5 layer table + D6 namespace registration) --------
# The engine's partition-build path loads the domain profiles ONCE and threads the
# derived `{namespaced_type -> layer}` map + `registered_ns` into every parse, so a
# corpus built THROUGH the engine resolves `dec.*`/`art.*` edges to their profile-
# declared layers/namespaces. A bare `parse_file`/`build_index` (no profile) is left on
# its pre-profile defaults for back-compat. The load is memoized per profile dir.
_PROFILE_EDGE_CACHE: dict[str, ProfileEdgeConfig] = {}


_REGISTERED_PROFILE_CACHE: dict[str, object] = {}


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
        from factlattice.profile import ProfileCollisionError, load_profile
        try:
            cached = load_profile(path)
        except ProfileCollisionError:
            # a profile that declares a name the engine reserves (or collides with a core one) is NOT served on core vocabulary
            # as if nothing were wrong: the loader fails the corpora that name it, loudly, and no other corpus (spec 39 REQ-NS-TRACE)
            raise
        except Exception:
            # A profile that fails to load must not take the corpus down with it: the
            # graph is still servable on core vocabulary. The failure is NOT silent —
            # `analytics.profile_audit` reports it against the same registry entry.
            return None
        _REGISTERED_PROFILE_CACHE[path] = cached
    return cached


_UNSET = object()


def profile_failure_fields(exc) -> dict:
    """The fields a failed record (and a refusal) carries for a profile the loader refused, from a `ProfileCollisionError`: TWO cases, and the record says
    which. A NAME the engine reserves or owns: `reason: profile-collision`, `names` (sorted). A MALFORMED profile where no name collided: `reason:
    profile-invalid`, `detail` (a short fixed phrase). Never the exception's message and never a path. ONE function, read by the boot loader and by the
    runtime `lts_register_corpus`, so the two doors cannot give different reasons."""
    names = sorted(getattr(exc, "names", ()) or ())
    if names:
        return {"reason": "profile-collision", "names": names}
    return {"reason": "profile-invalid", "detail": getattr(exc, "detail", None) or "profile-rejected"}


def parser_config_for(spec: dict | None = None,
                      profile: ProfileEdgeConfig | None = None,
                      registered: object = _UNSET):
    """The ONE `ParserConfig` the live engine parses a corpus against. [C3/C4/V5c]

    Merges the engine-wide `ProfileEdgeConfig` (D5 layers + D6 namespaces) with the
    corpus's OWN registered profile (verbs + minting keys + declared keys) through
    `ParserConfig.from_profile`, the single sanctioned seam.
    """
    from factlattice.parser_config import ParserConfig
    if profile is None:
        profile = engine_profile_config()
    # `registered`: the corpus's profile when the caller already read it (the bounded loader reads it on a worker); unset: read it here, as always
    return ParserConfig.from_profile(profile=registered_profile(spec or {}) if registered is _UNSET else registered,
                                     edge_config=profile)


def engine_profile_config(profile_dir: str | None = None) -> ProfileEdgeConfig:
    """The merged domain-profile edge config for the engine (memoized per dir).

    `profile_dir` defaults to `FACTLATTICE_PROFILE_DIR` / the shipped `domain-profiles`
    dir (see `default_profile_dir`). A missing/empty dir yields an EMPTY config
    (`registered_ns == {'core'}`, no edge layers), leaving the engine's behaviour
    identical to the pre-profile engine on a deployment with no profiles.
    """
    key = profile_dir or default_profile_dirs()
    cfg = _PROFILE_EDGE_CACHE.get(key)
    if cfg is None:
        cfg = load_profile_edge_config(key)
        _PROFILE_EDGE_CACHE[key] = cfg
    return cfg


# --- single-root partition build (the legacy registry shape) --
def build_partition(corpus_id: str, root: str, domain_suffix: str,
                    built_at: str | None = None,
                    profile: ProfileEdgeConfig | None = None,
                    spec: dict | None = None) -> Partition:
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
    # `build_index` records excluded profiles root-relative (lint shows them that way). A Partition carries them
    # absolute, as `build_merged_partition` and every heal do, so the registration audit can compare them with
    # the registry's paths whatever the process's working directory.
    abs_root = os.path.abspath(root)
    index["profile_documents"] = [os.path.join(abs_root, rel) for rel in index.get("profile_documents") or []]
    part = Partition(index, source_path=root)
    # [LEC-SELF-HEAL] attach an UNSEEDED rebuild recipe. This path builds through the
    # parser's own `build_index` (it wants the full index schema: alias_index,
    # id_collisions, adjacency), so there is no parse cache to inherit — the FIRST heal
    # therefore falls back to a full re-index (observable as `mode="full"`), and every
    # heal after it is surgical.
    part.corpus_index = CorpusIndex(corpus_id, domain_suffix,
                                    [{"path": root, "area": None}], config=cfg)
    part.corpus_index.registered_profile = (spec or {}).get("profile")
    return part


# --- multi-root, area-labeled partition build ([ENH-2]/[ENH-3]) ---------------
# The per-root parse (`_build_root_contribution`) MOVED to `incremental.CorpusIndex`
# ([LEC-SELF-HEAL]): the cold build and the self-heal re-index must be the SAME code, or
# a healed index and a cold-start index are only equal by two implementations agreeing.
# `CorpusIndex.live_roots()` carries the graceful-skip rule (a root directory that does
# not exist, or an `only_file` pin whose file is absent, contributes nothing);
# `CorpusIndex._materialize()` carries the only_file filter + `area` stamping + merge.
def build_merged_partition(corpus_id: str, roots: list[dict], domain_suffix: str,
                           built_at: str | None = None,
                           profile: ProfileEdgeConfig | None = None,
                           spec: dict | None = None,
                           ) -> Partition | None:
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
    ci.registered_profile = (spec or {}).get("profile")
    merged_nodes, merged_layers, root_paths, files_hash = ci.cold_build(as_rebuild=False)
    if not root_paths:
        return None
    return partition_from_build(corpus_id, domain_suffix, ci, merged_nodes, merged_layers, root_paths, files_hash)


def partition_from_build(corpus_id: str, domain_suffix: str, ci: CorpusIndex, merged_nodes: dict, merged_layers: dict,
                         root_paths: list[str], files_hash: str) -> Partition:
    """The Partition `build_merged_partition` makes from a finished cold build. Memory only: the bounded loader reads the roots itself (each on its
    own worker) and finishes with this, so the two builds cannot differ in what they make."""
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


def served_root_entries(spec: dict | None):
    """THE selection `build_store` makes, shared so nothing else re-derives it: `("roots", [entry, ...])` when the entry
    has a `roots` key at all (even empty), otherwise `("root", <the single root or None>)`. When `roots` is present
    `root` is ignored."""
    if "roots" in spec:                       # `roots: null` means NO roots (SPEC and tests/test_reviewer_nested_private pin it); a malformed shape was judged by `check_entry_shape` first
        return ("roots", spec["roots"])
    return ("root", spec.get("root"))


def is_path_like(text) -> bool:
    """THE path-like rule ([MECH-BR-BOOT] x-load-error, UNTRUSTED ROOTS; stated in SDLC [ENTRY-SHAPE-ONE-JUDGE] and SPEC.md): after stripping, a string is
    path-like when it starts with `/` or `~`, or holds a `/`. A bare word (`bad`, `.`, `..`) is not: it names no place without a working directory, which
    is not a registered root. Pure string work."""
    if not isinstance(text, str):
        return False
    t = text.strip()
    return bool(t) and (t.startswith(("/", "~")) or "/" in t)


def unstated_location(spec, strings: bool = True) -> bool:
    """[MECH-BR-BOOT] x-load-error (1): True when an entry has NO STATED LOCATION, i.e. its roots cannot be read as paths. The caller applies it only to an
    entry the release does NOT serve (an entry that is served has its declared roots as its location, whatever else the shape judge says of it: a non-boolean
    `private` or an int `domain_suffix` leave the roots readable). Two readings, both pure string work:
      (a) SHAPE of the location fields only: an entry that is not an object; `roots` neither null nor a list; a root that is neither a string nor an object;
          a blank root string; a root object whose `path` is not a non-blank string, or that has no `path` and no unknown key; a `root` that is not a
          non-blank string. (`private`, `domain_suffix`, `profile`, `external_id_prefixes`, `area` and `only_file` are not location fields.)
      (b) a KEY-FAILED entry (an unknown key, entry or root level) whose unknown key holds a root-ish string that is not path-like (`is_path_like`): `pth: "bad"`.
    A named-root failure, a `${...}` string and a NUL are not here: they are flagged for every write by the guard. `strings=False` is reading (a) alone: the
    containment readers (privacy, the reviewer) want the declared superset of roots and must not stop on a mistyped key's value."""
    from factlattice import registry as _reg
    if not isinstance(spec, dict):
        return True
    roots = spec.get("roots")
    if roots is not None and not isinstance(roots, (list, tuple)):
        return True
    readable = 0                      # roots that are non-blank strings
    soft = False                      # a root object whose `path` is not a non-blank string: the release read it as a missing root, others may still be there
    for r in (roots or ()):
        if isinstance(r, str):
            if not r.strip():
                return True
            readable += 1
        elif isinstance(r, dict):
            if "path" in r:
                if isinstance(r["path"], str) and r["path"].strip():
                    readable += 1
                else:
                    soft = True
            elif not any(k not in _reg.REGISTRY_ROOT_KEYS and not _reg.is_note_key(k) for k in r):
                return True
        else:
            return True
    root = spec.get("root")
    if root is not None:
        if not isinstance(root, str) or not root.strip():
            return True
        readable += 1
    if soft and not readable:
        return True                   # every root it names is unreadable; with another root readable the entry still has a stated location
    if strings and _reg.entry_key_problems(spec):
        from .reviewer import _untrusted_strings
        unknown = [v for k, v in spec.items() if k not in _reg.REGISTRY_ENTRY_KEYS and not _reg.is_note_key(k) and k != _reg.NAMED_FAILURE_KEY]
        for r in (roots or ()):
            if isinstance(r, dict):
                unknown += [v for k, v in r.items() if k not in _reg.REGISTRY_ROOT_KEYS and not _reg.is_note_key(k)]
        for v in unknown:
            for x in _untrusted_strings(v):
                t = x.strip()
                if t and not t.startswith("${") and "\x00" not in t and not is_path_like(t):
                    return True
    return False


def untrusted_path_strings(spec) -> list:
    """[MECH-BR-BOOT] x-load-error (UNTRUSTED ROOTS): the PATH-LIKE strings of a registry entry that failed on an unknown key, as typed (and stripped of a stray space), for the
    kernel to probe and for a write to compare against. Every string anywhere in the entry counts as a candidate spelling (a misspelt entry may write its path
    as a key, or split it into components); a string is path-like when it starts with `/` or `~` or holds a `/` (a bare word such as an area name names no
    place). A string with a NUL or a leading `${` is NOT here: it names no place at all, and an entry holding one is flagged for every write (`membership`).
    Pure string work: no disk."""
    from .reviewer import _untrusted_strings
    out = []
    for x in _untrusted_strings(spec):
        for form in dict.fromkeys((x, x.strip())):
            if not form or "\x00" in form or form.startswith("${"):
                continue
            if is_path_like(form):
                out.append(form)
    return list(dict.fromkeys(out))


def check_entry_shape(spec) -> None:
    """Raise `TypeError` (class names only, never a value) for any malformed registry-entry SHAPE, before any root is read: an entry that is not an
    object; `roots` that is not a list; a root that is neither a string nor a root object; a root object whose `path` is not a string, or whose
    `area` / `only_file` is neither a string nor absent; a `root` that is not a string; a `domain_suffix` that is not a string.

    THE JUDGE FOR THE WRITE SIDE AND FOR `lts_register_corpus`'s REPLY. It decides NO BOOT VERDICT ([MECH-BR-BOOT] x-load-error, third amendment, the
    architects' design in specs/38): no entry shape's boot verdict changes from release af4cfc2, so the loader reproduces what that release's build did
    with each shape (`bootload.EntryShapeError`) and never asks this function. One function, so the membership readers, the ledger, the loader's record of
    entries a write cannot place (`store.unlocated`) and the register reply judge a shape the same way. `null` for an optional key means the key is absent
    (`roots: null` = no roots)."""
    if not isinstance(spec, dict):
        raise TypeError("a corpus entry must be an object, not " + type(spec).__name__)
    if "roots" in spec:
        roots = spec["roots"]
        if roots is not None and not isinstance(roots, (list, tuple)):          # `null` has always meant "no roots" (a corpus that serves nothing)
            raise TypeError("`roots` must be a list, not " + type(roots).__name__)
        for r in (roots or ()):
            if isinstance(r, str):
                if not r.strip():
                    raise TypeError("a root's path must not be blank")
                continue
            if not isinstance(r, dict):
                raise TypeError("a root must be a string or a root object, not " + type(r).__name__)
            if "path" in r:
                if not isinstance(r["path"], str):
                    raise TypeError("a root's `path` must be a string, not " + type(r["path"]).__name__)
                if not r["path"].strip():
                    raise TypeError("a root's `path` must not be blank")
            elif not any(k not in registry.REGISTRY_ROOT_KEYS and not registry.is_note_key(k) for k in r):
                # no `path` and no unknown key either (an empty object, or `area` alone): nothing could have been meant, so it is a shape problem. A path missing
                # BECAUSE its key is misspelt (`pth`) keeps the unknown-key rule: that corpus fails alone and `membership._untrusted_entry_guard` covers its writes.
                raise TypeError("a root object must have a `path`")
            for k in ("area", "only_file"):
                if r.get(k) is not None and not isinstance(r[k], str):
                    raise TypeError(f"a root's `{k}` must be a string, not " + type(r[k]).__name__)
    if spec.get("root") is not None and not isinstance(spec["root"], str):
        raise TypeError("`root` must be a string, not " + type(spec["root"]).__name__)
    if isinstance(spec.get("root"), str) and not spec["root"].strip():
        raise TypeError("`root` must not be blank")
    if spec.get("domain_suffix") is not None and not isinstance(spec["domain_suffix"], str):
        raise TypeError("`domain_suffix` must be a string, not " + type(spec["domain_suffix"]).__name__)
    if spec.get("private") is not None and not isinstance(spec["private"], bool):
        raise TypeError("`private` must be a boolean, not " + type(spec["private"]).__name__)
    if spec.get("profile") is not None and not isinstance(spec["profile"], str):
        raise TypeError("`profile` must be a string, not " + type(spec["profile"]).__name__)
    pfx = spec.get("external_id_prefixes")
    if pfx is not None and (not isinstance(pfx, (list, tuple)) or not all(isinstance(x, str) for x in pfx)):
        raise TypeError("`external_id_prefixes` must be a list of strings, not " + type(pfx).__name__)


def served_root_paths(spec: dict | None) -> list[str]:
    """The roots the engine SERVES for one registry entry, as realpaths: exactly `served_root_entries` (roots if present,
    otherwise root). `~` and a relative path were already resolved when the registry was read (`registry.read_registry_file`);
    a value still relative here came from an ad hoc entry and resolves against the process cwd. This is the reader for 'is the document under THIS corpus'. The declared-registry superset (roots AND
    root) is `membership.declared_root_paths`, used only to FIND containers."""
    if not isinstance(spec, dict):
        return []
    kind, val = served_root_entries(spec)
    items = (val or []) if kind == "roots" else ([val] if val else [])
    out: list[str] = []
    for r in items:
        path = r.get("path") if isinstance(r, dict) else r
        if path and isinstance(path, (str, os.PathLike)):
            out.append(os.path.realpath(str(path)))
    return list(dict.fromkeys(out))


def registry_key_failure(cid: str, spec) -> dict | None:
    """The `store.failed_corpora` record for a registry entry that carries an unknown key (maintainers' ruling of 2026-10-29), or
    None for a clean entry. `code` is the first problem's code; `problems` lists them all (`registry.entry_key_problems`). It is
    PRIVATE when the engine's own privacy rule says so OR when an unknown key, at entry OR root level, is `private` or a near-miss of it: a corpus whose
    `privat: true` was meant to hide it must not be NAMED on an unnamed call because of the typo. A path has none (`path: None`)."""
    problems = registry.entry_key_problems(spec)
    if not problems:
        return None
    private = private_reason(cid, spec) is not None or any(
        registry.is_private_shaped(p["key"]) for p in problems if p["code"] not in registry.NAMED_ROOT_CODES)
    return {"code": problems[0]["code"], "path": None, "private": private, "problems": problems}


def build_store(corpora: dict[str, dict] | None = None) -> LtsStore:
    """Build a LtsStore in-memory from the corpus config, skipping empty/missing roots. A corpus whose every root is missing is
    kept in `store.no_roots_corpora`, so a half-finished move is declared and does not vanish ([MECH-NR-REGISTRY] x-health).

    [MECH-BR-BOOT] x-library: BOUNDED. Every root is read on its own worker in the process's one registry (`bootload.StoreLoader`), all at once, so a
    root that does not answer costs its corpus and no more: the corpus goes into `store.failed_corpora` with code `corpus-root-unresponsive` (no path),
    is not served, and the call returns inside about one root deadline (LTS_ROOT_DEADLINE_S, read once per process) while three or fewer reads are stuck, ceil(K/4) root deadlines from four on. A root that already has a
    stuck worker, or a process already holding 16, fails its corpus at once with no new thread, so a caller that builds per request cannot pile
    threads up. Stuck workers are daemon threads: a short-lived process exits at once.
    """
    if corpora is None:
        corpora = default_corpora()
    else:
        corpora = {cid: registry.expand_adhoc_entry(spec) for cid, spec in corpora.items()}
    from .bootload import build_blocking
    return build_blocking(corpora, engine_profile_config())     # the D5/D6 domain-profile config is loaded ONCE


def every_root_missing(spec: dict) -> bool:
    """True when an entry declares at least one root and none can be read: not a directory, or an absent `only_file` pin. An
    existing empty root is not missing: a registered-but-empty project is not an error."""
    return bool(declared_root_entries(spec)) and all(root_missing_reason(r) for r in declared_root_entries(spec))


def declared_root_entries(spec: dict) -> list[dict]:
    """The roots `build_store` would serve for one entry, as `{path, area[, only_file]}`."""
    kind, val = served_root_entries(spec)
    items = (val or []) if kind == "roots" else ([val] if val else [])
    out: list[dict] = []
    for r in items:
        if isinstance(r, dict):
            out.append(r)
        elif isinstance(r, str):
            out.append({"path": r, "area": None})
    return out


def root_missing_reason(root: dict) -> str | None:
    """`root_health`'s reason for a root that cannot be read, or None. The same test as `CorpusIndex.root_health`."""
    path = root.get("path")
    if not isinstance(path, str) or not os.path.isdir(path):
        return "not-a-directory"
    only_file = root.get("only_file")
    if only_file and not os.path.isfile(os.path.join(path, only_file)):
        return "only-file-absent"
    return None
