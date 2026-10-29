"""Content hashing for lts-engine.

Per FactLattice SPEC.md §1.8 (three-level integrity) and [LTS-INDEX]:

    content_hash  = SHA-256 over a node's / file's CONTENT text.
    files_hash    = SHA-256 over the ORDERED set of (relative_path, file_content_hash)
                    for every .lts file in the corpus root.

INVARIANT ([LTS-INDEX], [INV-LTS-FRESH]): hashing is over CONTENT only.
mtime and size are NEVER inputs. A content change with a preserved mtime
(git checkout, `touch -r`, clock skew) MUST change the hash; an mtime-only change
MUST NOT. This module reads file BYTES/text, never stat().
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat as _stat
from typing import Dict, List, Optional, Tuple

LTS_SUFFIX = ".lts"

# [ISSUE-NONREGULAR-MEMBER] the closed set of codes for a corpus member that cannot be hashed.
CORPUS_FILE_NOT_REGULAR = "corpus-file-not-regular"      # FIFO, socket or device (also through a symlink): NEVER opened
CORPUS_FILE_BROKEN_LINK = "corpus-file-broken-link"      # a symlink that does not resolve (dangling, or a loop)
CORPUS_FILE_UNREADABLE = "corpus-file-unreadable"        # a regular file the process may not read
CORPUS_DIR_UNREADABLE = "corpus-dir-unreadable"          # a directory inside the root the process may not list (rel path = the directory)
CORPUS_FILE_CODES = (CORPUS_FILE_NOT_REGULAR, CORPUS_FILE_BROKEN_LINK, CORPUS_FILE_UNREADABLE, CORPUS_DIR_UNREADABLE)


class CorpusFileError(OSError):
    """A `.lts` corpus member that is not a readable regular file. FAIL LOUD ([INV-LTS-FRESH] GAP-1): a corpus is never
    served as a silent partial view, and a FIFO/socket/device is never opened (an open of a FIFO blocks forever).

    Still an `OSError`, so every existing "OSError propagates (loud)" site keeps holding. `code` is one of
    `CORPUS_FILE_CODES`; `rel_path` is the path RELATIVE TO THE CORPUS ROOT, so `str(exc)` is path-free by construction
    (it can be persisted: SDLC [REGISTRY-PATH-SURFACES]). A caller that knows the root-relative path sets it with
    `set_rel` (the hash function itself only knows the path it was handed, and starts from its basename)."""

    def __init__(self, code: str, rel_path: str) -> None:
        self.code = code
        self.rel_path = rel_path
        super().__init__(f"{code}: {rel_path}")

    def set_rel(self, rel_path: str) -> "CorpusFileError":
        self.rel_path = rel_path
        self.args = (f"{self.code}: {rel_path}",)
        return self

    def __reduce__(self):                                   # picklable across a process boundary
        return (CorpusFileError, (self.code, self.rel_path))


def _io_error(exc: OSError, rel: str) -> OSError:
    """[P2-2] An OSError at classify/open/read time that is neither 'the name is gone' nor already coded (EIO, ETIMEDOUT, a cloud
    placeholder that cannot be materialised, EPERM from a policy layer ...) means the member cannot be read NOW: it is a coded
    `corpus-file-unreadable`, so a caller fails CLOSED instead of letting a raw error escape into a swallowing handler."""
    if isinstance(exc, (CorpusFileError, FileNotFoundError, NotADirectoryError, IsADirectoryError)):
        return exc
    return CorpusFileError(CORPUS_FILE_UNREADABLE, rel)


def check_member(abs_path: str, rel: Optional[str] = None) -> None:
    """Classify a `.lts` member WITHOUT opening it: lstat, and for a symlink the resolved target, must be a regular file.

    Raises `CorpusFileError` (not-regular / broken-link / unreadable-directory-component); returns None for a regular file
    (or a symlink that resolves to one). A path that does not exist at all raises the original `FileNotFoundError`
    (a vanished member is not a member-shaped file)."""
    rel = rel if rel is not None else os.path.basename(abs_path)
    try:
        st = os.lstat(abs_path)
        if _stat.S_ISLNK(st.st_mode):
            try:
                st = os.stat(abs_path)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EPERM):
                    raise CorpusFileError(CORPUS_FILE_UNREADABLE, rel) from None
                raise CorpusFileError(CORPUS_FILE_BROKEN_LINK, rel) from None   # dangling, a loop, a non-directory component
    except CorpusFileError:
        raise
    except OSError as exc:
        err = _io_error(exc, rel)
        if err is exc:
            raise
        raise err from None
    if not _stat.S_ISREG(st.st_mode):
        raise CorpusFileError(CORPUS_FILE_NOT_REGULAR, rel)


def content_hash(text: str) -> str:
    """Stable SHA-256 digest of a content string, prefixed 'sha256:'.

    Text is encoded UTF-8. Deterministic and pure — the same text always yields
    the same digest regardless of when/where it is computed.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _open_regular(path: str, rel: str) -> int:
    """Classify, then open `path` for reading WITHOUT ever blocking; return the descriptor (the caller closes it).

    lstat/stat BEFORE the open (a FIFO, socket or device is refused, never opened), `O_NONBLOCK` (a FIFO swapped in between
    the check and the open returns at once), and an `fstat` of the OPENED descriptor. Raises `CorpusFileError(rel)`."""
    check_member(path, rel)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except PermissionError:
        raise CorpusFileError(CORPUS_FILE_UNREADABLE, rel) from None
    except OSError as exc:
        if exc.errno in (errno.ENXIO, errno.EOPNOTSUPP, errno.ENOTSUP):      # a socket swapped in after the check
            raise CorpusFileError(CORPUS_FILE_NOT_REGULAR, rel) from None
        err = _io_error(exc, rel)
        if err is exc:
            raise
        raise err from None
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):          # swapped after the check: refuse, do not read
            raise CorpusFileError(CORPUS_FILE_NOT_REGULAR, rel)
    except BaseException as exc:
        os.close(fd)
        if isinstance(exc, OSError):
            err = _io_error(exc, rel)
            if err is not exc:
                raise err from None
        raise
    return fd


def file_content_hash(path: str) -> str:
    """SHA-256 over a file's raw bytes (content), 'sha256:'-prefixed.

    Reads bytes so line-ending or encoding quirks are hashed faithfully. Never
    consults mtime/size.

    NEVER BLOCKS ON A NON-REGULAR FILE ([ISSUE-NONREGULAR-MEMBER]): see `_open_regular`. A
    refusal is a `CorpusFileError` carrying the file's BASENAME (this function does not know the corpus root); a caller
    that does calls `set_rel` on it. Never the absolute path.
    """
    rel = os.path.basename(path)
    fd = _open_regular(path, rel)
    try:
        h = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except OSError as exc:
        err = _io_error(exc, rel)
        if err is exc:
            raise
        raise err from None
    finally:
        os.close(fd)
    return f"sha256:{h.hexdigest()}"


def safe_read_bytes(path: str, rel: Optional[str] = None) -> bytes:
    """THE way to read a corpus file's bytes: never blocks on a FIFO/socket/device, refuses with a `CorpusFileError` that
    carries `rel` (default: the basename), never an absolute path. A missing file raises the original `FileNotFoundError`."""
    rel = rel if rel is not None else os.path.basename(path)
    fd = _open_regular(path, rel)
    try:
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return fh.read()
    except OSError as exc:
        err = _io_error(exc, rel)
        if err is exc:
            raise
        raise err from None
    finally:
        os.close(fd)


def safe_read_text(path: str, rel: Optional[str] = None, encoding: str = "utf-8", errors: str = "strict") -> str:
    """`open(path, 'r', encoding=...).read()` for a corpus file, through `safe_read_bytes`: same decoding and the same
    universal-newline translation (`\r\n` and `\r` become `\n`), but it can never block on a non-regular file."""
    raw = safe_read_bytes(path, rel)
    return raw.decode(encoding, errors).replace("\r\n", "\n").replace("\r", "\n")


def hash_member(corpus_root: str, rel: str, hasher=None) -> str:
    """`file_content_hash` of `corpus_root/rel`, with any `CorpusFileError` re-labelled with the ROOT-RELATIVE path.
    `hasher` defaults to the module's CURRENT `file_content_hash` (looked up at call time, so a test spy still counts)."""
    fn = hasher if hasher is not None else file_content_hash
    try:
        return fn(os.path.join(corpus_root, rel))
    except CorpusFileError as exc:
        raise exc.set_rel(rel) from None


def _walk_reraise(err: OSError) -> None:
    """os.walk onerror callback that RE-RAISES ([INV-LTS-FRESH] GAP-1).

    os.walk's default (onerror=None) SILENTLY swallows an OSError from an inaccessible
    subdir and continues — yielding a PARTIAL file set with NO error. For the freshness
    oracle that is a FALSE-FRESH vector: a permission-denied SUBTREE that stays inaccessible
    across build+check reports FRESH over changed-inside content. Re-raise so the caller
    surfaces it — freshness-check → verdict:error (exit 2), build → loud fail — consistent
    with the file-level permission-denied path that already errors correctly. (A whole-root
    inaccessible corpus is caught separately via the empty file set → MED-6 verdict:error.)
    """
    raise err


def _walk_onerror(corpus_root: str):
    """`_walk_reraise`, except that a PERMISSION error on a directory becomes `CorpusFileError(corpus-dir-unreadable)` carrying
    the directory's ROOT-RELATIVE path, so one unreadable directory fails only its own corpus, loudly and precisely (never a
    partial view, never `registry-not-found`). So does any other OSError while listing (EIO ...); only a directory that vanished
    mid-walk (FileNotFoundError, NotADirectoryError) re-raises as before."""
    def _cb(err: OSError) -> None:
        if isinstance(err, PermissionError):
            fn = getattr(err, "filename", None)
            rel = os.path.relpath(fn, corpus_root).replace(os.sep, "/") if fn else "."
            raise CorpusFileError(CORPUS_DIR_UNREADABLE, rel) from None
        if isinstance(err, (FileNotFoundError, NotADirectoryError)):
            _walk_reraise(err)                    # the directory vanished mid-walk: not an I/O fault, behave as before
        # [Opus Diamond P2-B] any other OSError while LISTING a directory (EIO, ETIMEDOUT ...) is the same class: ONE corpus fails,
        # coded, never the whole registry as `registry-not-found`
        fn = getattr(err, "filename", None)
        rel = os.path.relpath(fn, corpus_root).replace(os.sep, "/") if fn else "."
        raise CorpusFileError(CORPUS_DIR_UNREADABLE, rel) from None
    return _cb


def walk_lts_paths(corpus_root: str) -> List[str]:
    """Sorted ABSOLUTE paths of every `*.lts` NAME under `corpus_root`, WITHOUT classifying or opening any of them. For the
    scanners that report a bad member as a ROW (key_loss, property_loss) instead of failing: the read that follows MUST use
    `safe_read_text`, which refuses a non-regular member with a `CorpusFileError`. An unreadable directory raises
    (`corpus-dir-unreadable`), never a silent partial list."""
    out: List[str] = []
    for dirpath, _dirs, files in os.walk(corpus_root, onerror=_walk_onerror(corpus_root)):
        out.extend(os.path.join(dirpath, n) for n in files if n.endswith(LTS_SUFFIX))
    out.sort()
    return out


def list_lts_files(corpus_root: str) -> List[str]:
    """Return sorted relative paths of every .lts file under corpus_root.

    Recursive. Sorting makes the ordered set deterministic across filesystems.
    Only .lts files are corpus members; .md notes, events/, etc. are excluded.
    An inaccessible subtree RAISES (never silently partial) — see _walk_reraise.
    A member-shaped name (`*.lts`) that is not a regular file (FIFO, socket, device, a broken or looping symlink, or a
    symlink to one of those) RAISES `CorpusFileError` with the root-relative path: it is never opened here and never
    silently skipped ([ISSUE-NONREGULAR-MEMBER]).
    """
    matches: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(corpus_root, onerror=_walk_onerror(corpus_root)):
        for name in filenames:
            if name.endswith(LTS_SUFFIX):
                abs_path = os.path.join(dirpath, name)
                rel = os.path.relpath(abs_path, corpus_root)
                # Normalize separators so the hash is stable across OSes.
                rel = rel.replace(os.sep, "/")
                check_member(abs_path, rel)
                matches.append(rel)
    matches.sort()
    return matches


def file_hash_map(corpus_root: str) -> Dict[str, str]:
    """Map of {relative_path: file_content_hash} for every .lts in the corpus."""
    out: Dict[str, str] = {}
    for rel in list_lts_files(corpus_root):
        out[rel] = hash_member(corpus_root, rel)
    return out


def files_hash(corpus_root: str) -> Tuple[str, Dict[str, str]]:
    """Deterministic corpus-level hash + the per-file map it is derived from.

    Returns (files_hash, file_hashes). The corpus hash is SHA-256 over the
    ordered, newline-joined 'relpath\\x00filehash' records. Because it is derived
    from CONTENT digests over a sorted path set, it changes iff any file's content
    changes, a file is added, or a file is removed — and NOT on an mtime-only touch.
    This is the freshness oracle ([INV-LTS-FRESH]).
    """
    fmap = file_hash_map(corpus_root)
    joined = "\n".join(f"{rel}\x00{fmap[rel]}" for rel in sorted(fmap))
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return f"sha256:{digest}", fmap
