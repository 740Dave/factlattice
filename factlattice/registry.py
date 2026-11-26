"""ONE registry lookup for every route, with NO silent fallback ([REGISTRY-SINGLE-READER]).

A corpus REGISTRY says which corpora exist and where their roots are. Every route to the engine (the MCP
server, `python -m factlattice_engine call`, the reviewer helpers, the library's profile resolution) must
read the SAME registry, or stop with a named reason. This module is the ONLY code that reads the registry
environment variables or the per-user pointer file; `tests/test_registry_lookup.py` fails the build if any
other non-test `.py` so much as spells their names (the check resolves constants, not just calls).

THE DEFECT IT CLOSES (measured at main 6099483). `config.default_corpora()` returned the built-in EXAMPLE
registry when the variable was unset AND when it named a file that did not exist; four other readers
(`profile.load_corpus_config`, `profile.registered_profile_for_root`, `callcli.corpus_config_info` and an
import-time read in `config.py`) read the environment on their own. In an ordinary agent shell the variable
is set only in the MCP server's environment, so every CLI route read a different registry from the server.

THE ORDER (a closed set, `SOURCES`):

  1. `env`          the registry variable is set (non-empty). The file must exist and parse. If it does not,
                    `RegistryError` -- it NEVER falls back to anything. A set variable is a declaration.
  2. `user-config`  a per-user POINTER file, `$XDG_CONFIG_HOME/factlattice/config.json` (default
                    `~/.config/factlattice/config.json`), `{"corpus_config": "<absolute path>"}`. Present
                    means it must be valid and name an existing, parseable registry; else `RegistryError`
                    naming the pointer. A site whose registry lives elsewhere points at it without copying.
  3. `example`      the built-in example registry. ONLY for the documented first-run trial; every route says
                    so, and a request that NAMES a corpus the example registry lacks is `registry-not-found`
                    (`example_missing`), never the generic unknown-corpus error.

POINTER HARDENING. It must be a regular file owned by the current user; the only key is `corpus_config`
(an unknown key is refused: a typo must not be silently ignored); the value must be ABSOLUTE after `~`
expansion, is resolved with realpath and must be a regular file.

THE ROOT (R-2). Relative paths in a registry resolve under a root. For `env` and `user-config` the default
root is the registry FILE's directory; for `example` it is the working directory; the root variable beats
both. WHAT THE HASH COVERS (re-ruled, round 2): the registry bytes ALWAYS; PLUS $FACTLATTICE_PROFILE_DIR when it is set
(it changes how a corpus is read); ELSE the names and contents of the profiles the deployment overlay loads, when it loads any (an overlay
with no profile adds nothing: the hash is byte-identical to no overlay); PLUS the working directory AND this
resolved root, only when some corpus-root path (`roots[].path` / legacy `root`) or `profile` path is non-absolute
(a leading `~` counts as non-absolute); PLUS $HOME, only when such a path starts with `~`. A registry that names no
relative path hashes the same from every working directory. A `${name}` value is not a relative path: its location is
in the table, so the table's own spellings are what the hash takes account of (see `_digest`). The working directory
stays in the hash of a relative registry although, since named roots, a relative ROOT no longer resolves against it
(`read_registry_file`): a false mismatch there is the safe direction, and existing tests pin it.

NAMED ROOTS (specs/36-named-roots.lts). A top-level key `named_roots` names each location. A corpus entry's root path
and profile may be `${name}` or `${name}/relative`. `read_registry_file` is the ONE place that expands them, so every
consumer of an entry sees absolute paths and none expands anything itself ([NAMED-ROOTS-EXPAND-ONCE]). It never raises
for a bad table or an unknown name: that entry carries `named_root_failure` and fails on its own, like an unknown key.

LAYERING (R-1). This is the CORE package: it imports nothing from the engine. `factlattice_engine.config`,
`factlattice.profile` and `callcli` import it.

NO REGISTRY OR POINTER WRITER EXISTS IN U1a. This module only reads. `factlattice init` (U1b) will be the only
writer of the pointer; atomic-write (temp file, then rename, so a reader never sees a torn pointer) is a U1b
(init) requirement: no registry or pointer writer exists in U1a; atomic-write is a U1b (init) requirement.

ERROR TEXT NEVER ECHOES FILE CONTENT. A registry or pointer file may hold corpus ids (private ones included), so
no message built here contains a key, a value or a JSON fragment from a file. The only things allowed in it are the
variable name, the pointer path, the named registry path, and the error CLASS (never the parser's message, which
carries positions and, for a bad encoding, bytes).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import stat
from collections.abc import Sequence
from typing import Any, NamedTuple

ENV_CONFIG = "FACTLATTICE_CORPUS_CONFIG"
ENV_ROOT = "FACTLATTICE_CORPUS_ROOT"

CODE = "registry-not-found"
SOURCES = ("env", "user-config", "example")
POINTER_KEYS = ("corpus_config",)
POINTER_DISPLAY = "~/.config/factlattice/config.json"
_HASH_SEP = b"\n--root--\n"

# ------------------------------------------------------------------------------ the registry ENTRY schema ([REGISTRY-NOTE-KEYS])
# ONE definition of which keys a registry entry and a registry root may carry. It is DERIVED FROM THE CODE THAT READS THEM (the
# readers are listed in the commit that last changed this set) and it is what the engine enforces where it builds a corpus
# (`entry_key_problems`, called by `factlattice_engine.config.build_store` and `LtsEngine.register_corpus`); the contradiction
# checker imports it and defines no list of its own. A key not in the set is not "ignored": it is a setting that would be accepted
# and quietly absent (`privat: true` leaves a corpus public), so the corpus that carries it FAILS, loudly, and the others serve.
EXTERNAL_PREFIXES_KEY = "external_id_prefixes"        # read by `factlattice.authoring.contradiction` only
REGISTRY_ENTRY_KEYS = frozenset({
    "roots",                  # the multi-root shape: [{path, area, only_file}, ...]
    "root",                   # the legacy single-root shape
    "domain_suffix",          # config.build_store / register_corpus
    "profile",                # config.registered_profile, profile.registered_profile_for_root, profile audit, reviewer, authoring
    "private",                # privacy.private_reason
    EXTERNAL_PREFIXES_KEY,
})
REGISTRY_ROOT_KEYS = frozenset({
    "path",                   # incremental.CorpusIndex.live_roots, registry._entry_paths, membership, profile, reviewer
    "area",                   # incremental.CorpusIndex._materialize (stamps node["area"])
    "only_file",              # incremental.CorpusIndex.live_roots / _materialize
})
NOTE_KEY_SUFFIX = "_note"                  # any key ending this is free text: never read, never refused
ENTRY_KEY_CODE = "registry-entry-unknown-key"
ROOT_KEY_CODE = "registry-root-unknown-key"
# ------------------------------------------------------------------------------ the named_roots table ([GRAM-NR-TABLE])
NAMED_ROOTS_KEY = "named_roots"            # a top-level key beside the corpus entries; never a corpus
NAMED_ROOT_KEYS = frozenset({
    "path",                   # factlattice.registry (expansion); the rest of the engine sees only expanded entries
    "was",                    # the spellings this root was known by
    "local",                  # was spellings that on this machine name something unrelated
    "state",                  # where this root's write history lives
    "private",                # never listed by a no-argument path lookup
})
NAMED_ROOT_UNKNOWN = "named-root-unknown"        # an entry refers to a name the table does not hold
NAMED_ROOTS_MALFORMED = "named-roots-malformed"  # the table is not of the grammar's shape
NAMED_ROOT_CODES = (NAMED_ROOT_UNKNOWN, NAMED_ROOTS_MALFORMED)
NAMED_FAILURE_KEY = "named_root_failure"         # set by the expansion on an entry it could not expand: {code, name | detail[, root]}
KEY_CODES = (ENTRY_KEY_CODE, ROOT_KEY_CODE) + NAMED_ROOT_CODES


def is_note_key(key: object) -> bool:
    return isinstance(key, str) and key.endswith(NOTE_KEY_SUFFIX)


def nearest_key(key: object, valid) -> str | None:
    """The valid key closest to `key` (case-insensitive), or None when nothing is close."""
    near = difflib.get_close_matches(str(key).strip().lower(), sorted(valid), n=1)
    return near[0] if near else None


def is_private_shaped(key: object) -> bool:
    """True for `private` in any case and for a near-miss of it (`privat`, `Private`, `privacy`): the strongest signal that an author
    meant to hide a corpus, wherever in the entry it was written (a root object included)."""
    return isinstance(key, str) and (key.strip().lower() == "private" or nearest_key(key, {"private"}) is not None)


def _note_case(key: object) -> bool:
    """A key that ends `_note` only in another case (`_NOTE`, `_Note`): a comment the author meant, not a data key."""
    return isinstance(key, str) and key.lower().endswith(NOTE_KEY_SUFFIX) and not key.endswith(NOTE_KEY_SUFFIX)


def _problem(scope: str, key: object, valid, code: str) -> dict:
    p = {"scope": scope, "key": str(key), "nearest": None if _note_case(key) else nearest_key(key, valid), "code": code}
    if _note_case(key):
        p["note_case"] = True            # the hint is "comment keys end in lowercase `_note`", never a data key
    return p


def problem_hint(problem: dict, kind: str) -> str:
    """The sentence tail that says what to do about one problem (`kind` is "entry" or "root")."""
    if problem.get("note_case"):
        return "; comment keys end in lowercase `_note`"
    if kind == "root" and is_private_shaped(problem.get("key")):
        # Checked before `nearest`: a root `privat` is nearest `path`, which points away from the fix.
        return "; `private` is an entry key, not a root key: move it to the corpus entry"
    if problem.get("nearest"):
        return f"; the nearest valid key is {problem['nearest']!r}"
    return "; no valid key is close"


def named_failure(entry: object) -> dict | None:
    """The record the expansion left on an entry it could not expand, or None. Anything else under that key is an ordinary
    unknown key. A file can write this record itself; that fails its own corpus, loudly, which is the safe direction."""
    failure = entry.get(NAMED_FAILURE_KEY) if isinstance(entry, dict) else None
    return failure if isinstance(failure, dict) and failure.get("code") in NAMED_ROOT_CODES else None


def entry_key_problems(entry: object) -> list:
    """Every unknown key in ONE registry entry and in its root dicts, in a stable order (entry keys sorted, then root keys in
    root order). Each problem is `{scope: "entry"|"root", key, nearest, code}`. `[]` for a clean entry, and for anything that is
    not an object (a wrong-shaped entry is a different, existing failure). A key ending `_note` is never a problem. A root given
    as a bare string has no keys. This is the SINGLE definition of what counts as an unknown key."""
    out: list = []
    if not isinstance(entry, dict):
        return out
    failure = named_failure(entry)
    if failure is not None:              # first, so `problems[0]` is why the corpus fails
        out.append({"scope": "entry", "key": failure.get("name") or failure.get("detail"), "nearest": None,
                    "code": failure["code"], **{k: failure[k] for k in ("name", "detail") if k in failure}})
    for k in sorted(entry, key=str):
        if k in REGISTRY_ENTRY_KEYS or is_note_key(k) or (k == NAMED_FAILURE_KEY and failure is not None):
            continue
        out.append(_problem("entry", k, REGISTRY_ENTRY_KEYS, ENTRY_KEY_CODE))
    roots = entry.get("roots")
    if isinstance(roots, (list, tuple)):
        for r in roots:
            if not isinstance(r, dict):
                continue
            for k in r:
                if k in REGISTRY_ROOT_KEYS or is_note_key(k):
                    continue
                out.append(_problem("root", k, REGISTRY_ROOT_KEYS, ROOT_KEY_CODE))
    return out

# The corpus-free statement of a registry choice, as `lts_list_corpora` reports it ([CAPABILITY-REGISTRY-CHOICE]).
REGISTRY_CHOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": ("Which registry this server loaded. Corpus-free: it names no corpus. `sha256_at_boot` is "
                    "what the server actually loaded; `sha256_now` is the file as it is on disk at this call "
                    "(a difference means the server serves an OLDER registry than the file and needs a restart). "
                    "In registry-error mode the object is {source, path, error} and no corpus is served."),
    "properties": {
        "source": {"type": ["string", "null"], "enum": list(SOURCES) + [None]},
        "path": {"type": ["string", "null"]},
        "sha256_at_boot": {"type": "string"},
        "sha256_now": {"type": ["string", "null"]},
        "resolved_at": {"type": "string"},
        "error": {"type": "string"},
        "error_persist": {"type": "string", "description": "the error with every path reduced to its basename (for persisted records)"},
    },
    "required": ["source", "path", "sha256_at_boot", "sha256_now", "resolved_at"],
}


class RegistryError(Exception):
    """The registry could not be resolved. `code` is always `registry-not-found` (a stable, documented code:
    MCP, `call` and the reviewer helpers show it identically). `reason` is the human sentence; it may name
    the variable and the pointer path (the user's own) but NEVER a corpus id from a registry."""

    code = CODE

    def __init__(self, reason: str, *, source: str | None = None, path: str | None = None,
                 persist: str | None = None, known: Sequence[str] = ()):
        self.reason = reason
        self.source = source
        self.path = path
        self.known = tuple(k for k in ([path] + list(known)) if k)      # the paths this error names (its own parts)
        # `persist_text` is built from the structured parts with BASENAMES ONLY. An error constructed WITHOUT parts
        # persists a fixed generic sentence, never its (possibly path-bearing) reason.
        self._persist = persist
        super().__init__(f"{CODE}: {reason}")

    @property
    def persist_text(self) -> str:
        if self._persist is not None:
            return f"{CODE}: {self._persist}"
        return f"{CODE}: the registry could not be resolved (details are on the live reply only)"


def _both(template: str, **paths: str):
    """`(full, persist, known)`: `template` rendered with the real paths and with their basenames. Placeholders may carry
    `!r`. Literal braces in a template are doubled."""
    full = template.format(**paths)
    reduced = template.format(**{k: _base(v) for k, v in paths.items()})
    return full, reduced, tuple(str(v) for v in paths.values())


def _base(p: object) -> str:
    t = str(p)
    return os.path.basename(t.rstrip("/")) or t


def _error(template: str, source: str | None = None, **paths: str) -> RegistryError:
    """A RegistryError from a template and its named paths; `path` (if given) is the registry the error is about."""
    full, reduced, known = _both(template, **paths)
    return RegistryError(full, source=source, path=paths.get("path"), persist=reduced, known=known)


class RegistryChoice(NamedTuple):
    source: str                       # one of SOURCES
    path: str | None               # the registry file, or None for the built-in example
    sha256: str                       # bytes; + cwd/root/$HOME only when a path needs them
    root: str                         # where relative paths in the registry resolve from
    corpora: dict[str, dict]          # the registry itself (NEVER put this in a report: it names corpora)
    cwd: str = "<cwd unavailable>"    # the working directory CAPTURED at resolve time (a deleted cwd must not break logging)
    named_roots: dict[str, dict] | None = None    # the parsed table; None when absent or malformed (`read_table`)
    table_problem: dict[str, Any] | None = None   # `read_table`'s `problem` for the same bytes (None when the table is absent or sound)

    def statement(self) -> dict[str, Any]:
        """`{source, path, sha256}` -- corpus-free; safe to print, record and compare."""
        return {"source": self.source, "path": self.path, "sha256": self.sha256}


# ------------------------------------------------------------------------------ the example registry
# A GENERIC example (NOT a deployment): the shape of a project registry, for the first-run trial only.
EXAMPLE_PROJECTS = [
    {"id": "alpha", "decision_lts": "decisions/decision-graph-alpha.lts", "specs_dir": "specs", "artifact_lts": "deliverables/deliverable-ledger.lts", "root": "."},
    {"id": "beta",  "decision_lts": "decisions/decision-graph-beta.lts",  "specs_dir": "specs", "artifact_lts": "deliverables/deliverable-ledger.lts", "root": "projects/beta"},
    {"id": "gamma", "decision_lts": "decisions/decision-graph-gamma.lts", "specs_dir": "spec",  "artifact_lts": "deliverables/deliverable-ledger.lts", "root": "projects/gamma"},
]


CWD_UNAVAILABLE = "<cwd unavailable>"


def _cwd() -> str:
    """The working directory, or a fixed marker if it has been deleted: a persisted path or a hash input must never be
    the reason a record or a boot is lost."""
    try:
        return os.getcwd()
    except OSError:
        return CWD_UNAVAILABLE


def example_root() -> str:
    """The example registry's root: the root variable, else the working directory."""
    return os.environ.get(ENV_ROOT) or _cwd()


def project_root_dir(pr: dict, root: str) -> str:
    """A project's own root dir per its `root:` (absolute path / '.' / relative to `root`)."""
    r = pr.get("root") or "."
    if os.path.isabs(r):
        return r
    if r == ".":
        return root
    return os.path.join(root, r)


def project_roots(pr: dict, root: str) -> list:
    """The up-to-three labeled roots for one example project (decisions / specs / artifacts)."""
    dec_path = os.path.join(root, pr["decision_lts"])
    art_path = os.path.join(root, pr["artifact_lts"])
    return [
        {"path": os.path.dirname(dec_path), "area": "decisions", "only_file": os.path.basename(dec_path)},
        {"path": os.path.join(project_root_dir(pr, root), pr.get("specs_dir") or ""), "area": "specs"},
        {"path": os.path.dirname(art_path), "area": "artifacts", "only_file": os.path.basename(art_path)},
    ]


def example_corpora(root: str) -> dict[str, dict]:
    return {pr["id"]: {"roots": project_roots(pr, root), "domain_suffix": f"proj.{pr['id']}"}
            for pr in EXAMPLE_PROJECTS}


# --------------------------------------------------------------------------------------- the lookup
def pointer_path() -> str:
    """`$XDG_CONFIG_HOME/factlattice/config.json`; a relative or empty XDG value is ignored (the XDG rule),
    falling back to `~/.config`."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = xdg if xdg and os.path.isabs(xdg) else os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "factlattice", "config.json")


def resolve_root(config_file: str | None = None) -> str:
    """Where relative paths in a registry resolve from: the root variable, else the registry file's
    directory, else (no file: the example) the working directory."""
    base = os.environ.get(ENV_ROOT)
    if base:
        return base
    return os.path.dirname(os.path.abspath(config_file)) if config_file else _cwd()


def _entry_paths(corpora: dict[str, dict]):
    """Every corpus-root path and `profile` path a registry names (strings only)."""
    for spec in corpora.values():
        if not isinstance(spec, dict):
            continue
        for r in spec.get("roots") or []:
            p = r.get("path") if isinstance(r, dict) else r
            if isinstance(p, str) and p and not p.startswith(_REF_OPEN):
                yield p
        for key in ("root", "profile"):
            if isinstance(spec.get(key), str) and spec[key] and not spec[key].startswith(_REF_OPEN):
                yield spec[key]


def _table_strings(table: object):
    """`(locations, spellings)` of a table as written, tolerating any shape: `path` and `state` values can be relative;
    `was` and `local` spellings never are. A `${name}` state is not counted: its location is that root's `path`."""
    locations, spellings = [], []
    for spec in (table.values() if isinstance(table, dict) else ()):
        if not isinstance(spec, dict):
            continue
        for key in ("path", "state"):
            v = spec.get(key)
            if isinstance(v, str) and v and not v.startswith(_REF_OPEN):
                locations.append(v)
        for key in ("was", "local"):
            vs = spec.get(key)
            spellings += [v for v in (vs if isinstance(vs, list) else ()) if isinstance(v, str)]
    return locations, spellings


def _digest(content: bytes, root: str, corpora: dict[str, dict] | None = None, *, always_root: bool = False,
            table: object = None) -> str:
    """sha256 over the bytes; plus cwd + root if any path is non-absolute (or `always_root`, the example);
    plus $HOME if any path starts with `~`; plus $FACTLATTICE_PROFILE_DIR when set (else the names and contents of the profiles the deployment overlay loads, when it loads any). `corpora` and `table` are AS WRITTEN,
    never expanded. A table location or spelling that names the home adds $HOME and nothing else: it does not depend on the
    working directory, and counting it would make two routes disagree over one registry."""
    paths = list(_entry_paths(corpora or {}))
    locations, spellings = _table_strings(table)
    relative_location = any(not os.path.isabs(p) and not _is_home_spelled(p) for p in locations)
    extra = b""
    if always_root or relative_location or any(not os.path.isabs(p) for p in paths):
        extra += _HASH_SEP + root.encode("utf-8") + b"\n--cwd--\n" + _cwd().encode("utf-8")
    if any(p.startswith("~") for p in paths) or any(_is_home_spelled(p) for p in locations + spellings):
        extra += b"\n--home--\n" + os.path.expanduser("~").encode("utf-8")
    pdir = os.environ.get("FACTLATTICE_PROFILE_DIR")
    if pdir:
        extra += b"\n--profile-dir--\n" + pdir.encode("utf-8")
    else:
        extra += _overlay_profiles_input()
    return hashlib.sha256(content + extra).hexdigest()


def _overlay_profiles_input() -> bytes:
    """The overlay's PROFILES as a hash input, or nothing. The ruling: the identity hash changes only when the LOADED profile content
    changes, so an overlay with no `domain-profiles/`, an empty one, or one holding no `.lts` adds NOTHING (byte-identical to no overlay),
    and the same loaded content from another path gives the same hash. The directory is read ONCE here (one listing, each file once): a
    directory or file that vanishes mid-call yields what was read, never an exception (`resolve_registry` calls this). The overlay is
    ignored under an explicit FACTLATTICE_PROFILE_DIR, exactly as `domain_profiles.default_profile_dirs` ignores it."""
    from . import overlay
    from .domain_profiles import discover_profiles
    from .hashing import CorpusFileError, safe_read_bytes
    pd = overlay.overlay_profile_dir()
    if pd is None:
        return b""
    parts = []
    for path in discover_profiles(pd):
        try:
            raw = safe_read_bytes(path)
            parts.append(os.path.basename(path).encode("utf-8") + b"\0" + hashlib.sha256(raw).digest())
        except (OSError, CorpusFileError):
            continue                                  # vanished between the listing and the read: it is not loaded either
    return (b"\n--overlay-profiles--\n" + b"\n".join(parts)) if parts else b""


# ---- PERSISTED surfaces carry STRUCTURE, never free error text --------------------------------------------------
# FULL paths: terminal output (`call` stdout/stderr) and live MCP replies. Persisted surfaces (the query log, REVIEW and
# UNREVIEWED fields) carry only: an error class, an error code, the reviewer's own fixed reasons, and a RegistryError's
# `persist_text`, which is built from structured parts with BASENAMES only. There is deliberately NO function that
# "cleans" free text: a pattern can never be complete (folder names hold spaces, both quote kinds and unicode), and
# rounds 4-5 proved it. CONTENT never, on any surface.


def env_var_name() -> str:
    """The registry variable's NAME: for messages, and for naming the variable in a CHILD process's environment (a tool that hands the
    registry to a scrubbed child asks here and never spells the name; SDLC [GUARD-ALLOW-LIST-AND-DISPLAY-HELPERS]). Reading the environment
    through this is caught by the guard's 'environment read with a key that is not a plain string' rule."""
    return ENV_CONFIG


def pointer_display() -> str:
    """The pointer's conventional location, for display."""
    return POINTER_DISPLAY


def entries_from(path: str) -> dict[str, dict]:
    """The corpus entries of an EXPLICIT registry file (strict: `RegistryError` if missing, unparseable or not an
    object). For callers handed a path (`profile.load_corpus_config(path)`); it does not consult the lookup."""
    return read_registry_file(path)[0]


def resolved_entries(path: str) -> dict[str, dict]:
    """The public way for anything outside the engine to read a registry file: corpus entries only, expanded, with the table
    stripped ([MECH-NR-REGISTRY]). An entry that could not be expanded keeps the value as written and carries `named_root_failure`
    ({code, name | detail}). `RegistryError` if the file is missing, unparseable or not an object. CLI: `registry resolved`."""
    return read_registry_file(path)[0]


def raw_entry(path: str, corpus_id: str) -> object:
    """The entry for `corpus_id` EXACTLY AS WRITTEN in a registry file (before any expansion), or None if the file holds none. `RegistryError` if the file
    cannot be read. For the one reader that must judge the entry's shape before `expand_entry` turns a blank root into an absolute path."""
    data, _content = _read_raw(path)
    return data.get(corpus_id)


def read_table(path: str) -> dict[str, Any]:
    """`{table, problem}` for a registry file's named_roots: `table` is the parsed table (None when absent or malformed) and
    `problem` is None or `{code, detail[, root]}`. A malformed table no corpus uses reaches no corpus, so this is how it is seen."""
    data, _content = _read_raw(path)
    table, problem = parse_named_roots(data.get(NAMED_ROOTS_KEY), resolve_root(path)) if NAMED_ROOTS_KEY in data else (None, None)
    return {"table": table, "problem": problem}


def _read_raw(path: str, *, context: tuple | None = None, source: str | None = None):
    """`(data, content_bytes)`: the file's JSON object as written, or `RegistryError`."""
    who, wp = context or ("the registry {path!r}", {"path": path})        # a TEMPLATE and its paths, rendered both ways
    if not os.path.isfile(path):
        raise _error(who + " does not exist (or is not a file)", source, **wp)
    try:
        with open(path, "rb") as fh:
            content = fh.read()
        data = json.loads(content.decode("utf-8"))
    except (OSError, ValueError, RecursionError) as exc:      # RecursionError: a pathologically deep array
        raise _error(who + " cannot be read as JSON (" + type(exc).__name__ + ")", source, **wp) from None
    if not isinstance(data, dict):
        raise _error(who + " must hold a JSON object of corpus entries, not " + type(data).__name__, source, **wp)
    return data, content


def read_registry_file(path: str, *, context: tuple | None = None, source: str | None = None):
    """`(corpora, content_bytes)` for a registry file, or `RegistryError`. The file must exist, parse as
    JSON and be an OBJECT. Missing and unparseable are both `registry-not-found` (v2 A-1: today a missing
    file fell back silently while a non-JSON one raised a bare JSONDecodeError). `corpora` is the corpus entries,
    expanded, with the table stripped: the ONE expansion ([NAMED-ROOTS-EXPAND-ONCE])."""
    data, content = _read_raw(path, context=context, source=source)
    return _expanded(data, resolve_root(path))[0], content


# ---- the table and `${name}` ([GRAM-NR-TABLE], [MECH-NR-REGISTRY]) -----------------------------------------------
_REF_OPEN = "${"
_REF = re.compile(r"\$\{([a-z][a-z0-9_-]*)\}(?:/(.*))?")     # always fullmatch: `$` would also match before a trailing newline
_NAME = re.compile(r"[a-z][a-z0-9_-]*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _parse_reference(value: str):
    """`(name, relative)` for a value that is the form `${name}` or `${name}/relative`; None for one that starts `${` and is not.
    The relative part cannot leave the root: an absolute part (`${a}//x`) would discard the base in `os.path.join`, and a `..`
    component would name a location the table does not."""
    m = _REF.fullmatch(value)
    if not m:
        return None
    rel = m.group(2) or ""
    if rel.startswith("/") or ".." in rel.split("/") or _CONTROL.search(rel):
        return None
    return m.group(1), rel


def _is_home_spelled(value: str) -> bool:
    return value == "~" or value.startswith(("~/", "$HOME/"))


def _location(value: str, base: str | None) -> str:
    """A location as a root path or table path may be written, made absolute: `~` from the home, relative from `base` (left
    as written when there is no base: an ad hoc entry has no registry file). An absolute value is returned as written."""
    if value.startswith("~"):
        return os.path.expanduser(value)      # `~user` that cannot be resolved stays as written: a missing root, declared
    if os.path.isabs(value) or base is None:
        return value
    return os.path.normpath(os.path.join(base, value))


def _spelling_key(value: str, base: str | None) -> str:
    """A spelling compared with another as WRITTEN, not as it resolves on this machine: `~/x` and `$HOME/x` are one spelling and
    `/Users/me/x` is another, so a table is well-formed or not whatever machine reads it. A spelling that happens to resolve to
    a current path here is not an error: the later slices ignore it ([GRAM-NR-TABLE] x-local)."""
    if value.startswith("$HOME/"):
        value = "~/" + value[len("$HOME/"):]
    if not value.startswith("~") and not os.path.isabs(value) and base is not None:
        value = os.path.join(base, value)
    if value.startswith("~/"):
        return "~/" + os.path.normpath(value[2:])
    return ("/" + os.path.normpath(value).lstrip("/")) if os.path.isabs(value) else (os.path.normpath(value) if value != "~" else value)


def _unusable_text(value: str) -> bool:
    """Text every path call would raise on: a NUL, or a lone surrogate with no encoding. One test for every table field, so the fault is `named-roots-malformed`,
    with `registry resolved` naming it. A tab or a newline is a legal name in a file system, so it is taken."""
    if "\x00" in value:
        return True
    try:
        value.encode("utf-8")
        return False
    except UnicodeEncodeError:
        return True


def _good_location(value: object) -> bool:
    """`path`: absolute, `~`, `~/...` or relative; not `${name}` (v1) and not `~user`; no unusable text."""
    return (isinstance(value, str) and bool(value) and not value.startswith(_REF_OPEN) and not _unusable_text(value)
            and (not value.startswith("~") or _is_home_spelled(value)) and not value.startswith("$HOME"))


def _good_spelling(value: object) -> bool:
    return isinstance(value, str) and not _unusable_text(value) and (os.path.isabs(value) or value.startswith(("~/", "$HOME/")))


def parse_named_roots(raw: object, base: str | None):
    """`(table, problem)`. `table` is `{name: {path (absolute), was, local, state, private}}` and `problem` None; or `table` is
    None and `problem` is `{code, detail[, root]}` with a fixed `detail`, for a table that is not of the grammar's shape.
    A key ending `_note` is free text at either level."""
    def bad(detail: str, root: str | None = None):
        return None, {"code": NAMED_ROOTS_MALFORMED, "detail": detail, **({"root": root} if root else {})}

    if not isinstance(raw, dict):
        return bad("table-not-object")
    names = [n for n in raw if not is_note_key(n)]
    table: dict[str, dict] = {}
    for name in names:
        spec = raw[name]
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            return bad("bad-name")
        if not isinstance(spec, dict):
            return bad("root-not-object", name)
        if any(k not in NAMED_ROOT_KEYS and not is_note_key(k) for k in spec):
            return bad("unknown-key", name)
        if not _good_location(spec.get("path")):
            return bad("path-bad", name)
        was, local = spec.get("was", []), spec.get("local", [])
        if not isinstance(was, list) or not all(_good_spelling(v) for v in was):
            return bad("was-bad", name)
        if not isinstance(local, list) or not all(isinstance(v, str) and v in was for v in local):
            return bad("local-not-in-was", name)
        if not isinstance(spec.get("private", False), bool):
            return bad("private-not-bool", name)
        state = spec.get("state")
        if state is not None and not (isinstance(state, str) and state and not _unusable_text(state)):
            return bad("state-bad", name)
        table[name] = {"path": _location(spec["path"], base).rstrip("/") or "/", "was": list(was), "local": list(local),
                       "private": spec.get("private", False), **({"state": state} if state is not None else {})}
    seen_paths: dict[str, str] = {}
    for name in table:
        key = _spelling_key(raw[name]["path"], base)
        if key in seen_paths:
            return bad("duplicate-spelling", name)
        seen_paths[key] = name
    seen_was: dict[str, str] = {}
    for name, spec in table.items():
        for spelling in spec["was"]:
            key = _spelling_key(spelling, base)
            if key in seen_paths:
                return bad("was-equals-current-path", name)
            if seen_was.setdefault(key, name) != name:
                return bad("duplicate-spelling", name)
    for name, spec in table.items():
        state = spec.get("state")
        if state is None:
            continue
        if state.startswith(_REF_OPEN):
            ref = _parse_reference(state)
            if ref is None or ref[0] not in table:
                return bad("state-bad", name)
        elif not _good_location(state):
            return bad("state-bad", name)
    for name, spec in table.items():         # worked out once: where this root's history lives, absolute ([MECH-NR-LEDGER])
        state = spec.get("state")
        if state is None:
            continue
        if state.startswith(_REF_OPEN):
            ref_name, rel = _parse_reference(state)
            spec["state_path"] = os.path.join(table[ref_name]["path"], rel) if rel else table[ref_name]["path"]
        else:
            spec["state_path"] = _location(state, base)
    return table, None


def _expand_value(value: str, table: dict[str, dict] | None, problem: dict | None, base: str | None, *,
                  fixes_location: bool, failures: list) -> str:
    """One root path or profile value. Records a failure and returns the value as written if it cannot be expanded."""
    if value.startswith(_REF_OPEN):
        if problem is not None:
            failures.append(dict(problem))
            return value
        ref = _parse_reference(value)
        if ref is None:
            failures.append({"code": NAMED_ROOT_UNKNOWN, "detail": "bad-reference"})     # no part of the value: it may hold a path
            return value
        name, rel = ref
        if not table or name not in table:
            failures.append({"code": NAMED_ROOT_UNKNOWN, "name": name})
            return value
        return os.path.join(table[name]["path"], rel) if rel else table[name]["path"]
    if value.startswith("~"):
        return os.path.expanduser(value)
    return _location(value, base) if fixes_location else value


def expand_entry(spec: object, table: dict[str, dict] | None, problem: dict | None, base: str | None) -> object:
    """One corpus entry with its root paths and profile expanded; a new object, the input untouched. Anything of a shape this
    does not know is passed through for the existing failure to meet. `base` None (an ad hoc entry, no registry file) leaves
    a relative root as written."""
    if not isinstance(spec, dict):
        return spec
    failures: list = []
    out = dict(spec)

    def root_value(v):
        return _expand_value(v, table, problem, base, fixes_location=True, failures=failures) if isinstance(v, str) and v else v

    roots = spec.get("roots")
    if isinstance(roots, list):
        out["roots"] = [({**r, "path": root_value(r.get("path"))} if isinstance(r, dict) and "path" in r
                         else (root_value(r) if isinstance(r, str) else r)) for r in roots]
    if "root" in spec:
        out["root"] = root_value(spec["root"])
    prof = spec.get("profile")
    if isinstance(prof, str) and prof:
        # x-fix-relative covers roots: a relative profile keeps the profile lookup's own rule.
        out["profile"] = _expand_value(prof, table, problem, base, fixes_location=False, failures=failures)
    if failures:
        out[NAMED_FAILURE_KEY] = failures[0]
    return out


def expand_adhoc_entry(spec: object) -> object:
    """An entry handed straight to `build_store` or `lts_register_corpus`: the same forms through the same function, with the
    table of THE lookup, read only when the entry names one. With no registry file a relative root stays as written. An entry that
    already failed (the boot registry's, handed to `build_store`) is final: it is not expanded again from a second read."""
    if named_failure(spec) is not None:
        return spec
    table, problem = None, None
    if isinstance(spec, dict) and _names_a_root(spec):
        try:
            choice = resolve_registry()
            table = choice.named_roots
            if table is None and choice.path:
                problem = read_table(choice.path)["problem"]
        except RegistryError:
            table = None
    return expand_entry(spec, table, problem, None)


def _names_a_root(spec: dict) -> bool:
    values = [spec.get("root"), spec.get("profile")]
    roots = spec.get("roots")
    for r in (roots if isinstance(roots, list) else ()):
        values.append(r.get("path") if isinstance(r, dict) else r)
    return any(isinstance(v, str) and v.startswith(_REF_OPEN) for v in values)


def _expanded(data: dict, base: str):
    """`(corpora, table)` from a registry file's JSON object."""
    table, problem = None, None
    if NAMED_ROOTS_KEY in data:
        table, problem = parse_named_roots(data[NAMED_ROOTS_KEY], base)
    corpora = {cid: expand_entry(spec, table, problem, base) for cid, spec in data.items() if cid != NAMED_ROOTS_KEY}
    return corpora, table


def _from_file(source: str, path: str, context: tuple) -> RegistryChoice:
    data, content = _read_raw(path, context=context, source=source)
    root = resolve_root(path)
    written = {cid: spec for cid, spec in data.items() if cid != NAMED_ROOTS_KEY}
    try:
        digest = _digest(content, root, written, table=data.get(NAMED_ROOTS_KEY))
    except Exception as exc:  # noqa: BLE001 -- a wrong-shaped entry fails here first; class only, never content
        raise _error("the registry {path!r} could not be loaded (" + type(exc).__name__ + ": its corpus entries "
                     "could not be interpreted)", source, path=path) from None
    corpora, table = _expanded(data, root)
    problem = parse_named_roots(data[NAMED_ROOTS_KEY], root)[1] if NAMED_ROOTS_KEY in data else None
    return RegistryChoice(source, path, digest, root, corpora, _cwd(), table, problem)


def _read_pointer(pointer: str) -> str:
    """The registry path a pointer file names, validated; else `RegistryError` naming the pointer."""
    def bad(why: str, **more: str) -> RegistryError:
        # `why` is a template over `more` (named paths) and may use {{ }} for literal braces
        return _error("the per-user registry pointer {pointer} " + why, "user-config", pointer=pointer, **more)

    try:
        st = os.stat(pointer)
    except OSError as exc:
        raise bad(f"cannot be read ({type(exc).__name__}); it must be a regular file") from None
    if not stat.S_ISREG(st.st_mode):
        raise bad("is not a regular file")
    if st.st_uid != os.geteuid():
        raise bad("is not owned by the current user")
    try:
        with open(pointer, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise bad(f"does not parse as JSON ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise bad("must hold a JSON object {{\"corpus_config\": \"<absolute path>\"}}")
    unknown = [k for k in data if k not in POINTER_KEYS]
    if unknown:
        # ⛔ NEVER echo the keys: a registry copied to the pointer path would list its corpus ids.
        looks = " (this looks like a corpus registry, not a pointer to one)" \
            if any(isinstance(data[k], dict) for k in unknown) else ""
        raise bad(f"has {len(unknown)} unknown key(s); the only key is {POINTER_KEYS[0]!r}{looks}")
    val = data.get(POINTER_KEYS[0])
    if not isinstance(val, str) or not val.strip():
        raise bad(f"must carry {POINTER_KEYS[0]!r} as a non-empty string")
    target = os.path.expanduser(val)
    if not os.path.isabs(target):
        raise bad("names a path that is not absolute (after ~ expansion)")
    real = os.path.realpath(target)
    if not os.path.isfile(real):
        raise bad("names {val!r}, which does not exist (or is not a regular file)", val=val)
    return real


def resolve_registry() -> RegistryChoice:
    """THE lookup. Order: env (must exist and parse; no fallback) -> the per-user pointer -> the example.
    Raises `RegistryError` (`registry-not-found`) on any broken configured source."""
    env_path = os.environ.get(ENV_CONFIG)
    if env_path:
        env_path = os.path.abspath(env_path)     # stored as an absolute path, whatever the cwd
        return _from_file("env", env_path, (ENV_CONFIG + " names {path!r}, which", {"path": env_path}))
    pointer = pointer_path()
    if os.path.lexists(pointer):
        real = _read_pointer(pointer)
        return _from_file("user-config", real, ("the registry {path!r} named by the pointer {pointer},",
                                                {"path": real, "pointer": pointer}))
    root = example_root()
    corpora = example_corpora(root)
    canon = json.dumps(corpora, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return RegistryChoice("example", None, _digest(canon, root, always_root=True), root, corpora, _cwd())


def example_missing(corpus_id: str) -> RegistryError:
    """A request NAMED a corpus the built-in example registry lacks: not 'unknown corpus' but 'no registry'."""
    return RegistryError(
        f"no corpus registry is configured (set {ENV_CONFIG} or create {POINTER_DISPLAY}); "
        f"the built-in example registry is in use and has no corpus '{corpus_id}'",
        source="example",
        persist=f"no corpus registry is configured; the built-in example registry is in use and has no corpus '{corpus_id}'")


def refusal(err: RegistryError, *, restart_note: bool = False) -> dict[str, Any]:
    """A tool-result dict for a registry failure: `success: False`, `reason: registry-not-found`, ZERO data.
    The engine's tools, MCP and `call` all return exactly this shape, so they show it identically."""
    text = str(err)
    if restart_note:
        text += (". This server is in REGISTRY-ERROR MODE: no corpus-scoped tool serves data until the "
                 "registry is fixed and the server is restarted (fix the registry, then restart the server)")
    out: dict[str, Any] = {"success": False, "reason": CODE, "error": text,
                           # the PERSISTED form (basenames only), for the query log; the live reply keeps full paths
                           "error_persist": err.persist_text}
    if err.source is not None:
        out["registry_source"] = err.source
    return out
