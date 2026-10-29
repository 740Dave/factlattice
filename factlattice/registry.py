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
(it changes how a corpus is read); PLUS the working directory AND this
resolved root, only when some corpus-root path (`roots[].path` / legacy `root`) or `profile` path is non-absolute
(a leading `~` counts as non-absolute); PLUS $HOME, only when such a path starts with `~`. The STORE resolves
relative roots against the process cwd, the profile lookup resolves them under the resolved root, so both bases
matter for a relative registry and neither for an absolute-only one: two routes report the same hash exactly when
every reader would resolve every corpus to the same absolute paths. (Resolution itself is unchanged: U1a-2.)

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

import hashlib
import json
import os
import stat
from typing import Any, Dict, NamedTuple, Optional, Sequence

ENV_CONFIG = "FACTLATTICE_CORPUS_CONFIG"
ENV_ROOT = "FACTLATTICE_CORPUS_ROOT"

CODE = "registry-not-found"
SOURCES = ("env", "user-config", "example")
POINTER_KEYS = ("corpus_config",)
POINTER_DISPLAY = "~/.config/factlattice/config.json"
_HASH_SEP = b"\n--root--\n"

# The corpus-free statement of a registry choice, as `lts_list_corpora` reports it ([CAPABILITY-REGISTRY-CHOICE]).
REGISTRY_CHOICE_SCHEMA: Dict[str, Any] = {
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

    def __init__(self, reason: str, *, source: Optional[str] = None, path: Optional[str] = None,
                 persist: Optional[str] = None, known: Sequence[str] = ()):
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


def _error(template: str, source: Optional[str] = None, **paths: str) -> "RegistryError":
    """A RegistryError from a template and its named paths; `path` (if given) is the registry the error is about."""
    full, reduced, known = _both(template, **paths)
    return RegistryError(full, source=source, path=paths.get("path"), persist=reduced, known=known)


class RegistryChoice(NamedTuple):
    source: str                       # one of SOURCES
    path: Optional[str]               # the registry file, or None for the built-in example
    sha256: str                       # bytes; + cwd/root/$HOME only when a path needs them
    root: str                         # where relative paths in the registry resolve from
    corpora: Dict[str, dict]          # the registry itself (NEVER put this in a report: it names corpora)
    cwd: str = "<cwd unavailable>"    # the working directory CAPTURED at resolve time (a deleted cwd must not break logging)

    def statement(self) -> Dict[str, Any]:
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


def example_corpora(root: str) -> Dict[str, dict]:
    return {pr["id"]: {"roots": project_roots(pr, root), "domain_suffix": f"proj.{pr['id']}"}
            for pr in EXAMPLE_PROJECTS}


# --------------------------------------------------------------------------------------- the lookup
def pointer_path() -> str:
    """`$XDG_CONFIG_HOME/factlattice/config.json`; a relative or empty XDG value is ignored (the XDG rule),
    falling back to `~/.config`."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = xdg if xdg and os.path.isabs(xdg) else os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "factlattice", "config.json")


def resolve_root(config_file: Optional[str] = None) -> str:
    """Where relative paths in a registry resolve from: the root variable, else the registry file's
    directory, else (no file: the example) the working directory."""
    base = os.environ.get(ENV_ROOT)
    if base:
        return base
    return os.path.dirname(os.path.abspath(config_file)) if config_file else _cwd()


def _entry_paths(corpora: Dict[str, dict]):
    """Every corpus-root path and `profile` path a registry names (strings only)."""
    for spec in corpora.values():
        if not isinstance(spec, dict):
            continue
        for r in spec.get("roots") or []:
            p = r.get("path") if isinstance(r, dict) else r
            if isinstance(p, str) and p:
                yield p
        for key in ("root", "profile"):
            if isinstance(spec.get(key), str) and spec[key]:
                yield spec[key]


def _digest(content: bytes, root: str, corpora: Optional[Dict[str, dict]] = None, *, always_root: bool = False) -> str:
    """sha256 over the bytes; plus cwd + root if any path is non-absolute (or `always_root`, the example);
    plus $HOME if any path starts with `~`; plus $FACTLATTICE_PROFILE_DIR when set. See the module docstring."""
    paths = list(_entry_paths(corpora or {}))
    extra = b""
    if always_root or any(not os.path.isabs(p) for p in paths):
        extra += _HASH_SEP + root.encode("utf-8") + b"\n--cwd--\n" + _cwd().encode("utf-8")
    if any(p.startswith("~") for p in paths):
        extra += b"\n--home--\n" + os.path.expanduser("~").encode("utf-8")
    pdir = os.environ.get("FACTLATTICE_PROFILE_DIR")
    if pdir:
        extra += b"\n--profile-dir--\n" + pdir.encode("utf-8")
    return hashlib.sha256(content + extra).hexdigest()


# ---- PERSISTED surfaces carry STRUCTURE, never free error text --------------------------------------------------
# FULL paths: terminal output (`call` stdout/stderr) and live MCP replies. Persisted surfaces (the query log, REVIEW and
# UNREVIEWED fields) carry only: an error class, an error code, the reviewer's own fixed reasons, and a RegistryError's
# `persist_text`, which is built from structured parts with BASENAMES only. There is deliberately NO function that
# "cleans" free text: a pattern can never be complete (folder names hold spaces, both quote kinds and unicode), and
# rounds 4-5 proved it. CONTENT never, on any surface.


def env_var_name() -> str:
    """The registry variable's NAME, for DISPLAY in messages only. Reading through this is caught by the guard's
    'environment read with a key that is not a plain string' rule."""
    return ENV_CONFIG


def pointer_display() -> str:
    """The pointer's conventional location, for display."""
    return POINTER_DISPLAY


def entries_from(path: str) -> Dict[str, dict]:
    """The corpus entries of an EXPLICIT registry file (strict: `RegistryError` if missing, unparseable or not an
    object). For callers handed a path (`profile.load_corpus_config(path)`); it does not consult the lookup."""
    return read_registry_file(path)[0]


def read_registry_file(path: str, *, context: Optional[tuple] = None, source: Optional[str] = None):
    """`(corpora, content_bytes)` for a registry file, or `RegistryError`. The file must exist, parse as
    JSON and be an OBJECT. Missing and unparseable are both `registry-not-found` (v2 A-1: today a missing
    file fell back silently while a non-JSON one raised a bare JSONDecodeError)."""
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


def _from_file(source: str, path: str, context: tuple) -> RegistryChoice:
    corpora, content = read_registry_file(path, context=context, source=source)
    root = resolve_root(path)
    try:
        digest = _digest(content, root, corpora)
    except Exception as exc:  # noqa: BLE001 -- a wrong-shaped entry fails here first; class only, never content
        raise _error("the registry {path!r} could not be loaded (" + type(exc).__name__ + ": its corpus entries "
                     "could not be interpreted)", source, path=path) from None
    return RegistryChoice(source, path, digest, root, corpora, _cwd())


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


def refusal(err: RegistryError, *, restart_note: bool = False) -> Dict[str, Any]:
    """A tool-result dict for a registry failure: `success: False`, `reason: registry-not-found`, ZERO data.
    The engine's tools, MCP and `call` all return exactly this shape, so they show it identically."""
    text = str(err)
    if restart_note:
        text += (". This server is in REGISTRY-ERROR MODE: no corpus-scoped tool serves data until the "
                 "registry is fixed and the server is restarted (fix the registry, then restart the server)")
    out: Dict[str, Any] = {"success": False, "reason": CODE, "error": text,
                           # the PERSISTED form (basenames only), for the query log; the live reply keeps full paths
                           "error_persist": err.persist_text}
    if err.source is not None:
        out["registry_source"] = err.source
    return out
