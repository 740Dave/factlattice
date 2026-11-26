"""The deployment overlay: ONE optional directory, named by `FACTLATTICE_OVERLAY`, never a hard-coded path.

WHAT IT IS FOR. The public tree ships the engine and generic examples. Material that belongs to ONE deployment (a
domain profile for its own workspace, a test that reads its real documents, its private data in time) lives in a
separate repository, checked out anywhere, and is pointed at by this variable. With no overlay the engine and the
whole suite run, and the deployment-only tests are SKIPPED, each skip naming its reason. The layout is described in
PUBLISHING.md ("The deployment overlay").

THE SINGLE-READER RULE. This module is the ONLY code that reads the variable (`tests/_registry_guard.py` polices it,
the same way it polices the registry variables). Everything else asks here: `overlay_root()`, `overlay_path(...)`,
`env_var_name()` (for naming the variable in a child process's environment, never spelling it).

WHAT "USABLE" MEANS. An absolute path to an existing REAL directory: `lstat` of the normalised path says directory, so a link (to a
directory or to anything else, spelled with or without a trailing `/` or `/.`) is not usable. Only the overlay directory ITSELF is checked:
links in the parent components of the path, and links inside the overlay, are followed (a known limit, PUBLISHING.md), and neither is a file, a missing path or a relative path (it would mean something
different from every working directory). An unset or empty variable means "no overlay" and is silent (`FACTLATTICE_OVERLAY=`
is the shell's own way of unsetting for one command).

SET BUT UNUSABLE is never silent and never a guess:
  * a dev or CI run (`overlay_root()`): returns None and WARNS ONCE per process and value, naming the reason;
  * a public release run (`overlay_root(strict=True)`, which tools/release_prep.py uses in required mode): RAISES
    `OverlayUnusable`, because a release made while the operator believes an overlay is in force, and it is not,
    would be a release made on a belief.
"""

from __future__ import annotations

import os
import stat
import warnings
from pathlib import Path

ENV_OVERLAY = "FACTLATTICE_OVERLAY"


class OverlayUnusable(RuntimeError):
    """The variable is set, and what it names cannot be used as the overlay (strict mode only; otherwise a warning)."""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


class OverlayWarning(UserWarning):
    """The variable is set but names no usable overlay; the run goes on as if no overlay were present."""


# The values already warned about in this process: "warns once" is per distinct (value, reason), not per call.
_WARNED: set[tuple[str, str]] = set()


def env_var_name() -> str:
    """The variable's NAME, for messages and for a child process's environment. Never read the environment through this."""
    return ENV_OVERLAY


def _state() -> tuple[Path | None, str | None, str | None]:
    """-> (root, raw value, problem). `root` is set only when usable; `problem` only when the variable is set and unusable."""
    raw = os.environ.get(ENV_OVERLAY)
    if raw is None or not raw.strip():
        return None, None, None
    if "\x00" in raw:
        return None, raw, "the value holds a NUL character"
    if not os.path.isabs(raw):
        return None, raw, "the value is not an absolute path"
    # lstat of the NORMALISED path, and that same path is what is returned: `lstat("link/")` and `lstat("link/.")` FOLLOW the link (POSIX
    # resolves a trailing slash or dot), so lstat of the raw spelling accepted a symlinked overlay. normpath drops the trailing `/`, `/.`
    # and doubled slashes, leaving the last component to be examined as itself.
    norm = os.path.normpath(raw)
    try:
        mode = os.lstat(norm).st_mode
    except FileNotFoundError:
        return None, raw, "the path does not exist"
    except OSError as exc:
        return None, raw, f"the path cannot be examined ({type(exc).__name__})"
    if stat.S_ISLNK(mode):
        return None, raw, "the path is a link (the overlay must be a real directory, not a link)"
    if not stat.S_ISDIR(mode):
        return None, raw, "the path is not a directory"
    if not os.access(norm, os.R_OK | os.X_OK):
        return None, raw, "the directory cannot be read"
    return Path(norm), raw, None


def raw_value() -> str | None:
    """The variable's value exactly as set (None when unset). For a test harness that must put the ambient state back for one test;
    nothing in the engine decides anything from it."""
    return os.environ.get(ENV_OVERLAY)


def overlay_problem() -> str | None:
    """Why a SET variable is unusable, or None (unset, or usable). Pure: no warning, no raise."""
    return _state()[2]


def overlay_root(*, strict: bool = False) -> Path | None:
    """The overlay directory, or None when there is none. See the module docstring for the three outcomes."""
    root, raw, problem = _state()
    if problem is None:
        return root
    message = f"{ENV_OVERLAY} is set to {raw!r} but is not usable: {problem}"
    if strict:
        raise OverlayUnusable(message, problem)
    key = (raw or "", problem)
    if key not in _WARNED:
        _WARNED.add(key)
        warnings.warn(message + "; running with NO overlay", OverlayWarning, stacklevel=2)
    return None


def overlay_path(*parts: str) -> Path | None:
    """`<overlay>/<parts...>` when an overlay is in force and that path exists, else None.

    A part that would leave the overlay (`..`, an absolute part) gives None: the layout is fixed by the
    document, and a caller cannot reach outside it through this."""
    root = overlay_root()
    if root is None:
        return None
    for part in parts:
        if not part or os.path.isabs(part) or ".." in Path(part).parts:
            return None
    path = root.joinpath(*parts)
    return path if path.exists() else None


def overlay_profile_dir() -> str | None:
    """`<overlay>/domain-profiles` when the overlay has one, else None."""
    p = overlay_path("domain-profiles")
    return str(p) if p is not None and p.is_dir() else None


def skip_reason(what: str) -> str:
    """The sentence a deployment-only test skips with: names what it needs and why it is absent here."""
    root, raw, problem = _state()
    if root is not None:
        return f"{what}: the overlay at {root} does not provide it"
    if problem is not None:
        return f"{what}: {ENV_OVERLAY} is set but not usable ({problem})"
    return f"{what}: {ENV_OVERLAY} is not set (no deployment overlay)"
