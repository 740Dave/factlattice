"""In-place property edits on an existing .lts file — the operation that actually happens.

WHY THIS EXISTS, STATED HONESTLY
    `schema.py` gates WHOLE-DOCUMENT writes. But nearly every real authoring action is
    "add one property to one node in an 800-line file". Constructing a full `Document`
    to change one line is absurd, so the author reaches for a raw text edit — and every
    one of the EIGHT emphasis-in-key failures measured on 2026-08-12 happened there.
    (The original text of this docstring said SEVEN. Corrected per the author's own note
    of the same day: the eighth occurred INSIDE the enforcement spec, in the section
    counting the other seven, so the count was stale on arrival. Guard 8/8, author 0/8.)

    ⛔ THE BYPASS WAS NOT LAZINESS. IT WAS A MISSING FEATURE. A gate that does not cover
    the operation people perform is a gate they walk around, and then the walking-around
    is misdiagnosed as indiscipline. This module closes that gap so the honest path is
    also the EASY path — which is the only version that holds.

TWO DESIGN CHOICES, BOTH DELIBERATE

    1. ⛔ IT DOES NOT RE-RENDER THE DOCUMENT. Loading a hand-authored file and emitting
       canonical form would reformat prose blocks, blockquotes and section structure,
       producing an enormous diff in which a real loss would be invisible. Instead the
       edit is SURGICAL — one line inserted or replaced, every other byte untouched —
       and the reconciliation runs over the WHOLE FILE afterwards.

    2. ⛔ THE INVARIANT IS STRONGER THAN "MY KEY PARSED". It is: the parsed property set
       must differ from the pre-edit set by EXACTLY the intended change and nothing else.
       That catches collateral damage — a neighbouring property silently dropped because
       an insertion landed inside another node's block — which a "did my key land?" check
       would sail straight past. Same reason a positive control is not enough without a
       denominator.

    A refusal writes NOTHING. The file on disk is either the original or the intended
    result; there is no third state.

--------------------------------------------------------------------------------------
REVISION 2026-08-12 (ui-architect), building [AUTH-PATH-LAYER-1-MCP]. The two
design choices above are UNCHANGED and are the reason this module is right. What changed
is the ORACLE, and it changed because the lexical one was measured wrong:

    ⛔ THE LEXICAL NODE LOCATOR MISSED 260 OF 1668 LIVE NODES (15.6%).
       Measured 2026-08-12 over `acme/corpora/**/*.lts`. The old `_NODE_HEAD`
       required `## [BARE-ID]` — ID-FIRST, h2-h6, no doctype. The real parser's
       `_heading_id` also accepts ID-LAST (`## 1. Purpose [LTS-PURPOSE]`), h1, and
       doctyped brackets (`[SPEC:ACME-DECISIONS]`). EVERY document root node in the
       Acme corpora is an h1 ID-last doctyped heading, so the single most-edited
       node in every file was unreachable — the tool would have refused `node not found`
       on the node people edit most.

    ⛔ AND THE LEXICAL PROPERTY READER IS NOT THE AUTHORITY EITHER. It recognised four
       of the parser's formats imperfectly (no bold keys, no nested-block awareness, no
       continuation folding, no id-tables), so the "intended delta" it computed could
       disagree with the parser in BOTH directions — refusing a good edit, or approving
       one whose collateral it could not see.

    THE FIX IS NOT A BETTER REGEX. It is to stop having a second grammar at all: both
    sides of the gate now run `factlattice.parser.parse_text`, the SAME core the engine
    and the linter run (`factlattice/__init__.py`: "ONE parser, N consumers"). The gate
    is now literally the §3 sentence — *the parsed property set must differ from the
    pre-edit set by exactly the intended change and nothing else, per node, across the
    whole file* — with `parsed` meaning parsed, on both sides.

    AND THE PROPOSAL'S STATED REASON FOR PUTTING THIS ON THE ENGINE IS WRONG (see
    [AUTH-PATH-LAYER-1-MCP] `why-it-must-live-on-the-engine`): "a library that accepts a
    parser callable cannot be handed the authoritative one". It can — this module lives
    INSIDE the `factlattice` package, one dot from `..parser`. The acceptance test used a
    stand-in oracle because it was written outside the package, not because the package
    could not reach the parser. The real argument for the engine is the EFFORT GRADIENT
    (`why-it-inverts-the-gradient`), and that argument stands on its own.

    ⛔ AND THE `preview_fn` INJECTION POINT IS GONE, NOT DEMOTED. The author's own note
    (2026-08-12, item 3) names it "the one defect you must fix in the port": a caller
    that supplies the oracle can supply a stand-in, and their acceptance test had to.
    An earlier draft of this port kept it as an opt-in SECOND opinion; that was wrong on
    two counts. It leaves a seam a future caller can mistake for the gate, and
    `lts_preview` runs THIS SAME parse core — so it is not a second oracle at all, it is
    the same oracle wearing a hat. Its `lost` channel, the one genuinely different view
    it offers, is measured-insufficient (a dropped key landed in `ignorable` beside blank
    lines at 100.0% coverage) and is therefore the one thing that must never be gated on.
    The parser is called directly, here, with no way to substitute it.
"""

from __future__ import annotations

import hashlib
import json
import errno
import fcntl
import functools
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from collections import Counter
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import ledger as _ledger
from ..parser import (_HEADING_RE, _PROP_BOLD_RE, _PROP_KEY_ONLY_RE, _PROP_SIMPLE_RE, _fold_continuation,
                      _heading_id, parse_text, BRANCH_PROP_BOLD, BRANCH_PROP_CONT, BRANCH_PROP_SIMPLE)
from .schema import _coerce_key

# Property-line shapes, mirroring SPEC §2.2. Used ONLY to choose an insertion point —
# never to decide whether something parsed. That decision belongs to the parser.
_PROP_ANY = re.compile(r"^-[ \t]+(?:\*\*)?([a-z][a-z0-9_\-]*)(?:\*\*)?[ \t]*:")
_INDENTED = re.compile(r"^[ \t]+\S")
_KEY_LEGAL = re.compile(r"[a-z][a-z0-9_\-]*")

# Sections whose bodies are NOT node bodies: their lines are consumed by
# `_parse_section_edges`, not `_parse_properties`. A new node must never be appended
# into one, and 01's hard-won lesson ([FL-PROP-NEARMISS-RULE] `must-be-section-aware`)
# is that a property-shaped check applied there fires on every well-formed edge triple.
RESERVED_SECTION_TITLES = frozenset({
    "relationships", "causal graph", "edges", "graph", "references",
})


class EditRefused(Exception):
    """Nothing was written. The refusal IS the outcome."""

    def __init__(self, message: str, *, evidence: Optional[dict] = None):
        super().__init__(message)
        self.evidence = evidence or {}


@dataclass
class EditResult:
    path: str                       # ABSOLUTE — a receipt naming a relative path proves nothing
    node_id: str
    key: str
    value: str
    action: str                     # "added" | "replaced"
    coercions: tuple[str, ...]
    key_as_authored: str | None
    line_number: int                # 1-based, of the line written
    line_text: str
    parsed_value: object            # the value AS THE PARSER PRODUCED IT
    properties_before: tuple[str, ...]
    properties_after: tuple[str, ...]
    delta: dict                     # {node_id: {"added": [...], "removed": [...]}} — WHOLE FILE
    nodes_checked: int
    backup_path: str
    context: tuple[str, ...] = ()   # the written line in situ, for eyeballing placement
    warnings: tuple[str, ...] = field(default_factory=tuple)
    ledger: dict | None = None      # [NODE-CHANGE-LEDGER] {"status": "ok", seq, file} | {"status": "failed", reason}


@dataclass
class RepairResult:
    """A malformed EXISTING line, rewritten in place so it parses. See `repair_property`."""

    path: str
    node_id: str
    key: str
    value: str
    line_number: int                # 1-based, of the line repaired
    line_before: str                # the malformed line, verbatim
    line_after: str                 # the line that replaced it
    key_region: str | None          # what the key region WAS, before repair
    value_source: str               # "caller" | "derived_from_line"
    coercions: tuple[str, ...]
    key_as_authored: str | None
    parsed_value: object            # the value AS THE PARSER PRODUCED IT
    properties_before: tuple[str, ...]
    properties_after: tuple[str, ...]
    delta: dict                     # WHOLE FILE, per node
    nodes_checked: int
    backup_path: str
    context: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    ledger: dict | None = None


@dataclass
class AddNodeResult:
    path: str
    node_id: str
    section: str | None
    title: str
    heading_level: int
    heading_line: int               # 1-based
    properties: dict                # key -> value AS PARSED
    coercions: dict                 # authored key -> list of coercions
    delta: dict
    nodes_checked: int
    backup_path: str
    context: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    ledger: dict | None = None
    created: bool = False           # [A-E1] True only for a `create=True` write that made the file
    created_parent: str | None = None   # [A-E1] the ONE directory level `create_parent` made, if any


# ---------------------------------------------------------------------------------
# The oracle. ONE parser, both sides of the gate.
# ---------------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# [ISSUE-WRITE-ATOMICITY] the shared write step: one lock, one atomic replace
# ---------------------------------------------------------------------------
# EVERY corpus write goes through `_serialised` (the lock, held across read -> gate -> write)
# and `_write` (backup, then atomic replace). A new writer MUST use both (SDLC.md 7.6).
_LOCK_TIMEOUT_ENV = "FACTLATTICE_WRITE_LOCK_TIMEOUT"
_LOCK_TIMEOUT_DEFAULT_S = 10.0
_STALE_TMP_S = 3600.0


def _lock_dir() -> Path:
    """The engine STATE dir (same resolution as `factlattice_engine.querylog.state_dir`, which
    core cannot import) + `write-locks/`. NEVER the corpus dir: a file-sync service would sync lock files."""
    env = os.environ.get("FACTLATTICE_STATE_DIR")
    if env:
        base = Path(env).expanduser()
    else:
        xdg = os.environ.get("XDG_STATE_HOME")
        base = (Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state") / "factlattice"
    return base / "write-locks"


def _lock_timeout() -> float:
    try:
        t = float(os.environ.get(_LOCK_TIMEOUT_ENV, _LOCK_TIMEOUT_DEFAULT_S))
        return t if t >= 0 else _LOCK_TIMEOUT_DEFAULT_S
    except ValueError:
        return _LOCK_TIMEOUT_DEFAULT_S


@contextmanager
def _file_lock(path: Path):
    """Exclusive per-file lock: fcntl.flock on a sidecar in the state dir, keyed by the file's
    realpath hash. Bounded wait; on timeout REFUSE ("busy, retry"), writing nothing."""
    real = Path(os.path.realpath(path))
    ld = _lock_dir()
    try:
        ld.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise EditRefused(f"cannot create the write-lock directory {ld} ({e}); refusing rather than "
                          f"writing unlocked. Nothing written.", evidence={"refusal": "lock_unavailable"}) from None
    if Path(os.path.realpath(ld)) == real.parent or real.parent in Path(os.path.realpath(ld)).parents:
        raise EditRefused(f"the write-lock directory {ld} is INSIDE the corpus directory {real.parent}; "
                          f"lock files must never live in a corpus. Set FACTLATTICE_STATE_DIR elsewhere. "
                          f"Nothing written.", evidence={"refusal": "lock_unavailable"})
    key = hashlib.sha256(str(real).encode("utf-8")).hexdigest()
    fd = os.open(ld / f"{key}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    timeout = _lock_timeout()
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    # NOT "busy": the filesystem cannot lock (ENOTSUP/ENOLCK/...). Fail fast, loudly.
                    raise EditRefused(f"cannot take the write lock for {real.name} in {ld}: "
                                      f"{errno.errorcode.get(e.errno, e.errno)} ({e.strerror}). The state "
                                      f"dir's filesystem may not support flock; set FACTLATTICE_STATE_DIR "
                                      f"to a local directory. Nothing written.",
                                      evidence={"refusal": "lock_unavailable"}) from None
                if time.monotonic() >= deadline:
                    raise EditRefused(f"{real.name} is busy: another writer held its lock for "
                                      f"{timeout:g}s. Retry. Nothing written.",
                                      evidence={"refusal": "lock_busy"}) from None
                time.sleep(0.02)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass     # round 5 (F3): by now the write (and its ledger record) may have happened; a failed release
                     # must not surface as a refusal that says "Nothing written". The fd is gone either way.


def _serialised(fn):
    """Hold the per-file lock across the WHOLE read -> gate -> write of a writer. The writers read
    the file themselves, inside the call, so under the lock they always edit and gate the FRESH
    text and a later writer includes the earlier one's change."""
    @functools.wraps(fn)
    def wrapper(path, *args, **kwargs):
        real = Path(os.path.realpath(Path(path).expanduser()))
        with _file_lock(real):
            try:
                return fn(path, *args, **kwargs)
            finally:
                _bases().pop(str(real), None)
                _hash_cache().clear()
    return wrapper


def _sweep_stale_temps(path: Path) -> None:
    """Best effort, under the lock: a writer SIGKILLed mid-write leaves its temp (never the target)."""
    prefix = f".{path.name}."
    try:
        for p in path.parent.iterdir():
            if p.name.startswith(prefix) and p.name.endswith(".tmp") and \
                    time.time() - p.stat().st_mtime > _STALE_TMP_S:
                p.unlink()
    except OSError:
        pass


_BASES = threading.local()          # real path -> the exact BYTES the candidate was computed from
_BEFORE_REPLACE_HOOK = None         # test seam: called after the temp is fsynced, before the CAS


def _bases() -> dict:
    if not hasattr(_BASES, "d"):
        _BASES.d = {}
    return _BASES.d


def _read_base(path) -> str:
    """The ONE way a writer reads the file it is about to edit (under the lock). Records the raw
    bytes as the compare-and-swap BASE and returns the text exactly as `Path.read_text` would
    (utf-8, universal newlines)."""
    real = os.path.realpath(path)
    from factlattice.hashing import safe_read_bytes
    raw = safe_read_bytes(str(real))
    _bases()[real] = raw
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _preflight(real: Path) -> None:
    """Refusals that the old in-place writer gave for free and an inode swap would not."""
    if not real.exists():
        return
    if not os.access(real, os.W_OK):
        raise EditRefused(f"{real} is not writable by this process; refusing rather than replacing a "
                          f"read-only corpus file. Nothing written.")
    if os.stat(real).st_nlink > 1:
        raise EditRefused(f"{real} has {os.stat(real).st_nlink} hard links; an atomic replace would "
                          f"silently sever them. Resolve the links first. Nothing written.")


def _cas_check(real: Path, base: Optional[bytes], tmp: Path) -> None:
    """Compare-and-swap: the bytes on disk must still be the BASE the candidate was computed
    from. Catches a writer OUTSIDE the lock (hand edit, sync client, pre-fix engine). No retry.
    RESIDUAL WINDOW: this check and `os.replace` are two syscalls; a write landing between them
    is still lost. The lock closes the window for every writer that takes it."""
    try:
        from factlattice.hashing import safe_read_bytes
        current: Optional[bytes] = safe_read_bytes(str(real))
    except FileNotFoundError:
        current = None
    if current != base:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        sha = lambda b: None if b is None else hashlib.sha256(b).hexdigest()
        raise EditRefused(
            "file changed on disk since it was read, by a writer outside the lock (hand edit, sync "
            "client, or pre-fix engine). Nothing written; re-run. "
            f"base sha256={sha(base)} current sha256={sha(current)}",
            evidence={"base_sha256": sha(base), "current_sha256": sha(current)})


def _atomic_replace(path: Path, text: str) -> None:
    """Temp file in the directory of the REAL path (a symlinked corpus file stays a symlink;
    the temp is never named *.lts), O_EXCL, write, fsync, metadata copied from the original,
    compare-and-swap against the base bytes, os.replace onto the REAL path, best-effort dir fsync.
    The target is only ever the old or the new file."""
    real = Path(os.path.realpath(path))
    base = _bases().get(str(real), _UNSET)
    if base is _UNSET:
        raise RuntimeError("corpus write without a recorded base: a writer must read its file with "
                           "`_read_base` (or go through `write_whole_file`)")
    data = text.encode("utf-8")
    existing = real.exists()
    st = os.stat(real) if existing else None
    mode = (st.st_mode & 0o7777) if st else 0o644
    tmp = real.parent / f".{real.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        if st is not None:
            try:                                   # best effort: owner first, then flags/xattrs/mode
                os.chown(tmp, st.st_uid, st.st_gid)
            except OSError:
                pass
            _copy_xattrs_darwin(real, tmp)         # shutil.copystat has no xattr support on macOS
            try:
                shutil.copystat(real, tmp)
            except OSError:
                pass
            # copystat copied the ORIGINAL's mtime; the engine keys staleness on (mtime, size), so
            # a same-length edit must not look unchanged.
            os.utime(tmp, None)
        if _BEFORE_REPLACE_HOOK is not None:
            _BEFORE_REPLACE_HOOK(real)
        _cas_check(real, base, tmp)
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(real.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


_UNSET = object()


def _copy_xattrs_darwin(src: Path, dst: Path) -> None:
    """Best effort: macOS copyfile(3) with COPYFILE_XATTR (+ACL), so a file-sync service attrs etc. survive."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        libc.copyfile(os.fsencode(src), os.fsencode(dst), None, 1 | 4)   # ACL | XATTR
    except Exception:
        pass


# ---------------------------------------------------------------------------------
# [NODE-CHANGE-LEDGER] v1: the ledger append, in the ONE shared locked step
# ---------------------------------------------------------------------------------
_LAST = threading.local()


def oob_warnings(ledger: dict | None, path) -> tuple[str, ...]:
    """The loud receipt warnings from the ledger append: a chain break, and a whole-file write whose
    prior content could not be preserved (empty when neither)."""
    out: tuple[str, ...] = ()
    if (ledger or {}).get("status") == "ok_without_undo":
        out += ("WRITTEN AND LEDGERED, BUT THE PRIOR CONTENT WAS NOT PRESERVED: "
                f"{_ledger.describe(ledger)}",)
    oob = (ledger or {}).get("out_of_band")
    if not oob:
        return out
    return out + (f"OUT-OF-BAND EDIT: {os.path.realpath(path)} changed outside the writers since ledger seq "
            f"{oob['since_seq']} ({oob['since_ts']}); that change is not recorded",)


def last_ledger() -> dict | None:
    """The ledger outcome of the most recent write on THIS thread ({"status": "ok"|"failed", ...})."""
    return getattr(_LAST, "ledger", None)


def _meta(*, proposer=None, approver=None, reason=None, via=None, corpora=None, engine_head=None,
          ledger_roots=None, corpora_error=None) -> dict:
    """Validate the ledger metadata BEFORE any write (a refusal writes nothing), return it as a dict."""
    bad = _ledger.validate_meta(proposer=proposer, approver=approver, reason=reason, via=via)
    if bad:
        raise EditRefused(f"{bad} Nothing written.")
    return {"proposer": proposer, "approver": approver, "reason": reason, "via": via,
            "corpora": list(corpora) if corpora is not None else None, "engine_head": engine_head,
            "corpora_error": corpora_error,
            "roots": list(ledger_roots) if ledger_roots is not None else None}


def _sha_bytes(b: Optional[bytes]) -> Optional[str]:
    return None if b is None else hashlib.sha256(b).hexdigest()


def _ledger_append(real: Path, ctx: Optional[dict], candidate: str) -> None:
    """Right after `os.replace`, INSIDE the per-file lock. Never raises: a failure is recorded in
    `last_ledger()` as {"status": "failed", "reason": ...} (the corpus write has already landed)."""
    if ctx is None:
        _LAST.ledger = None
        return
    if _ledger.is_ephemeral(real):          # (e) scratch/preflight copy: no record, no blob, and SAID
        _LAST.ledger = dict(_ledger.EPHEMERAL_SKIP)
        return
    try:
        base = _bases().get(str(real))
        fields = {k: ctx.get(k) for k in ("verb", "node_id", "key", "delta", "discarded",
                                          "proposer", "approver", "reason", "via", "corpora",
                                          "corpora_error", "engine_head", "backup_path", "inserted",
                                          "undo_evidence_missing")}
        fields["discarded"] = fields["discarded"] or []
        fields["file_sha256_before"] = _sha_bytes(base)
        fields["file_sha256_after"] = _sha_bytes(candidate.encode("utf-8"))
        nid = ctx.get("node_id")
        if nid is not None:
            fields["node_hash_before"] = _node_hashes(ctx["original"]).get(nid) if ctx.get("original") else None
            fields["node_hash_after"] = _node_hashes(candidate).get(nid)
        appended = _ledger.append(real, fields, roots=ctx.get("roots"),
                                  prior_bytes=base if ctx.get("store_prior_blob") else None)
        if isinstance(ctx.get("_state"), dict):      # [delta4] this write's record exists: say which, at once
            ctx["_state"]["ledger_seq"] = appended.get("seq")
        _LAST.ledger = appended
    except _ledger.LedgerError as e:
        _LAST.ledger = _ledger.live_block("failed", "ledger-write-failed", e)
    except Exception as e:  # noqa: BLE001 -- the audit trail must never turn a landed write into a crash
        _LAST.ledger = _ledger.live_block("failed", "ledger-write-failed", e)


class WrittenPath(type(Path())):
    """What `write_whole_file` returns: still a `Path` (every existing caller keeps working), plus the
    ledger outcome as `.ledger` ({"status": "ok", seq, file} | {"status": "failed", reason})."""
    ledger: dict | None = None


def write_whole_file(path, text: str, *, expect_base: "bytes | None" = None, **ledger_kw) -> Path:
    """The shared write step for a WHOLE-DOCUMENT writer (schema.write_document): lock, record the
    existing bytes as the base (None if the file is new), backup an existing file, atomic replace,
    ledger. Returns the real path written as a `WrittenPath` (a Path with `.ledger`); the outcome is
    also `last_ledger()`. `ledger_kw`: proposer/approver/reason/via/corpora/engine_head/ledger_roots/
    corpora_error (all optional).

    `expect_base` (default None = unchanged): the exact bytes the caller read and gated. They are compared
    with the bytes read UNDER THE LOCK; a mismatch (or a vanished file) raises EditRefused BEFORE any backup
    or replace, so a concurrent edit between the caller's read and this call is never overwritten."""
    meta = _meta(**ledger_kw)
    real = Path(os.path.realpath(Path(path).expanduser()))
    with _file_lock(real):
        try:
            from factlattice.hashing import safe_read_bytes
            old_bytes = safe_read_bytes(str(real)) if real.exists() else None
            if expect_base is not None and old_bytes != expect_base:
                raise EditRefused("file changed since it was read and gated (compare-and-swap under the "
                                  "per-file lock failed); nothing written.")
            _bases()[str(real)] = old_bytes
            ctx = {"verb": "write_document", "node_id": None, "key": None, "discarded": [],
                   "store_prior_blob": old_bytes is not None, **meta}
            old_text = None if old_bytes is None else old_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            try:
                oh = {} if old_text is None else dict(_node_hashes(old_text))
                nh = dict(_node_hashes(text))
                ctx["delta"] = {n: ("added" if n not in oh else "removed" if n not in nh else "changed")
                                for n in sorted(set(oh) | set(nh)) if oh.get(n) != nh.get(n)}
            except Exception:  # noqa: BLE001
                ctx["delta"] = None
            if real.exists():
                _write(real, old_text or "", text, ctx=ctx)       # backup, replace, ledger (with backup_path)
            else:
                _sweep_stale_temps(real)
                _atomic_replace(real, text)
                _ledger_append(real, {**ctx, "backup_path": None}, text)
        finally:
            _bases().pop(str(real), None)
            _hash_cache().clear()
    out = WrittenPath(real)
    out.ledger = last_ledger()
    return out



_HASHES = threading.local()          # text -> {node_id: parser content_hash}, filled by the gate's parses


def _hash_cache() -> dict:
    if not hasattr(_HASHES, "d"):
        _HASHES.d = {}
    return _HASHES.d


def _parsed_props(text: str, source_file: str = "<edit>") -> dict[str, dict]:
    """node_id -> {key: value}, straight from the authoritative parser.

    [NODE-CHANGE-LEDGER] also remembers each node's parser `content_hash` for this text, so the
    ledger can record node_hash_before/after from the gate's OWN before/after parses (no re-parse)."""
    pf = parse_text(text, source_file)
    cache = _hash_cache()
    if len(cache) >= 4:
        cache.pop(next(iter(cache)))
    cache[text] = {n.id: n.content_hash for n in pf.nodes}
    return {n.id: dict(n.properties) for n in pf.nodes}


def _node_hashes(text: str) -> dict:
    hit = _hash_cache().get(text)
    if hit is None:                                  # a writer whose gate did not parse this text
        _parsed_props(text)
        hit = _hash_cache()[text]
    return hit


def _parsed_spans(text: str, source_file: str = "<edit>") -> dict[str, tuple[int, int]]:
    """node_id -> 1-based inclusive `line_range` from the authoritative parser."""
    pf = parse_text(text, source_file)
    return {n.id: tuple(n.line_range) for n in pf.nodes}


def _headings(lines: list[str]) -> list[tuple[int, int, Optional[str], str]]:
    """(idx0, level, node_id_or_None, title) for every heading, fence-aware.

    Uses the parser's OWN `_HEADING_RE` / `_heading_id`, so ID-first, ID-last and
    doctyped forms are all recognised identically to the parser. No second grammar.
    """
    out: list[tuple[int, int, Optional[str], str]] = []
    in_fence = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        hm = _HEADING_RE.match(line)
        if not hm:
            continue
        parsed = _heading_id(hm.group(2))
        if parsed is None:
            out.append((i, len(hm.group(1)), None, hm.group(2).strip()))
        else:
            out.append((i, len(hm.group(1)), parsed[0], parsed[2]))
    return out


def _property_block_end(lines: list[str], start0: int, end0: int) -> int:
    """Index of the last line belonging to the node's leading property block.

    Returns `start0` (the heading) when the node has no properties. Walks nested
    blocks and folded continuations so an insertion never lands INSIDE one —
    inserting a top-level `- key: value` in the middle of a nested block silently
    truncates that block, which is precisely the collateral the gate exists to catch.
    We would rather not cause it than merely detect it.
    """
    last = start0
    in_prop = False
    for i in range(start0 + 1, end0):
        line = lines[i]
        if _PROP_ANY.match(line):
            last, in_prop = i, True
        elif in_prop and _INDENTED.match(line):
            last = i
        elif not line.strip():
            continue                       # a blank does not end the block, nor extend it
        else:
            in_prop = False                # prose, a table, a fence: the block is over
    return last


def _items(v: object) -> list[str]:
    """A parsed property value as its (key-less) VALUE ITEMS: a scalar is one item, a list is
    one item per element (every element is live — `parser._maybe_multivalue`), a mapping is
    one canonical-JSON item. The unit of the multiset the gate and the receipt compare."""
    if isinstance(v, list):
        return [e if isinstance(e, str) else json.dumps(e, sort_keys=True, default=str) for e in v]
    if isinstance(v, str):
        return [v]
    return [json.dumps(v, sort_keys=True, default=str)]


def _multiset_move(bv: object, av: object) -> tuple[list[str], list[str]]:
    """(removed_items, added_items) between two parsed values, as MULTISETS."""
    b, a = Counter(_items(bv)), Counter(_items(av))
    return sorted((b - a).elements()), sorted((a - b).elements())


def _last_key_line(lines: list[str], start0: int, end0: int, key: str) -> tuple[int, bool]:
    """(index of the LAST top-level `- key:` line in the node body, any such line is a
    nested-block head). Fence-aware; uses `_PROP_ANY`, never to decide what PARSED."""
    last, nested, in_fence = -1, False, False
    for i in range(start0 + 1, end0):
        t = lines[i].strip()
        if t.startswith("```") or t.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _PROP_ANY.match(lines[i])
        if m and m.group(1) == key:
            last = i
            if not lines[i].split(":", 1)[1].strip():
                nested = True
    return last, nested


def _delta(before: dict[str, dict], after: dict[str, dict]) -> dict:
    """Per-node, VALUE-aware delta over the WHOLE file. Empty dict == nothing moved.

    [TYPED-WRITER-APPEND] 2026-10-03. This used to compare KEY SETS only, so a value REPLACE
    (and a lost or added list element) reported `delta {}` / nodes changed 0 — a change that
    passed as "no change" in the receipt, even after aaa3d7e made the GATE value-aware. Each
    node entry now carries, beyond the key-set `added`/`removed` (shape unchanged, so a pure
    key add still reads `{"added": [k], "removed": []}`), a `changed` map for every key present
    on BOTH sides whose (key, value) MULTISET moved:

        "changed": {key: {"removed": [old items], "added": [new items]}}

    so a replace is `removed: [old]` + `added: [new]` and an append is `added: [value]`. A key
    whose items are identical but whose ORDER moved reports `reordered: true` instead.
    """
    out: dict[str, dict] = {}
    for nid in sorted(set(before) | set(after)):
        bn, an = before.get(nid, {}), after.get(nid, {})
        added, removed = sorted(set(an) - set(bn)), sorted(set(bn) - set(an))
        changed: dict[str, dict] = {}
        for k in sorted(set(bn) & set(an)):
            if bn[k] != an[k]:
                rem, add = _multiset_move(bn[k], an[k])
                changed[k] = ({"removed": rem, "added": add} if (rem or add)
                              else {"removed": [], "added": [], "reordered": True})
        if added or removed or changed:
            entry = {"added": added, "removed": removed}
            if changed:
                entry["changed"] = changed
            out[nid] = entry
    return out


def _value_pairs(before: dict[str, dict], after: dict[str, dict]) -> tuple[set, set, dict]:
    """Whole-file, VALUE-aware delta as (node_id, key) pairs.

    Returns (added, removed, changed) where `added`/`removed` are sets of (nid, key) and
    `changed` is {(nid, key): (before_value, after_value)} — a key present on BOTH sides
    whose parsed VALUE differs. This is the layer the key-set `_delta` cannot see: a lost
    LIST ELEMENT on an accumulated key changes the value while leaving the key in place,
    so it shows up ONLY here. Measured 2026-09-26 (TCL-COMPARISON-API, provenance x2).
    """
    added: set = set()
    removed: set = set()
    changed: dict = {}
    for nid in set(before) | set(after):
        b, a = before.get(nid, {}), after.get(nid, {})
        for k in set(a) - set(b):
            added.add((nid, k))
        for k in set(b) - set(a):
            removed.add((nid, k))
        for k in set(b) & set(a):
            if b[k] != a[k]:
                changed[(nid, k)] = (b[k], a[k])
    return added, removed, changed


def _gate(original: str, candidate: str, source_file: str, *,
          added: frozenset = frozenset(), removed: frozenset = frozenset(),
          changed_allowed: frozenset = frozenset(),
          value_exact: dict | None = None,
          before: dict | None = None) -> tuple[dict, dict, dict]:
    """THE gate — VALUE-aware. Refuses unless the whole-file parsed change is EXACTLY:

      - the (node, key) pairs in `added` appeared (and NO others),
      - the (node, key) pairs in `removed` disappeared (and NO others),
      - every (node, key) whose VALUE changed is within `changed_allowed`.

    `changed_allowed` is a SUPERSET bound, not an exact match, because an idempotent
    replace (new value == old value) legitimately changes nothing — but a replace may
    ALSO change a value, and only the intended target may. Everything else must be
    byte-identical, per node, across the whole file — VALUE included. That last clause is
    the fix: the previous gate compared KEY SETS only, so a lost list element on any node
    passed silently ([AUTH-PATH-LAYER-1-MCP] §3, extended 2026-09-26).

    `value_exact` ([TYPED-WRITER-APPEND]) tightens a changed key from "may change" to
    "its (key, value) MULTISET moved by EXACTLY this": {(node, key): (removed_items,
    added_items | None)}. `added_items=None` means "exactly one new item, any value" (a
    replace, whose parsed text the caller cannot predict) and an unchanged key is then
    accepted (idempotent); a list means the added multiset must equal it and NO change is
    refused (an append that did not grow the multiset did not happen). Keys named here are
    implicitly in `changed_allowed`.

    `before` may be passed precomputed to avoid re-parsing the original.
    """
    value_exact = dict(value_exact or {})
    changed_allowed = frozenset(changed_allowed) | frozenset(value_exact)
    before = _parsed_props(original, source_file) if before is None else before
    after = _parsed_props(candidate, source_file)

    if set(before) != set(after):
        gained, lost = sorted(set(after) - set(before)), sorted(set(before) - set(after))
        raise EditRefused(
            f"the edit changed the NODE SET — nodes gained {gained}, nodes lost {lost}. "
            f"Nothing written. (Symptom of an insertion landing outside the target block, "
            f"or of a heading being displaced.)",
            evidence={"nodes_before": len(before), "nodes_after": len(after),
                      "nodes_gained": gained, "nodes_lost": lost},
        )

    a_added, a_removed, a_changed = _value_pairs(before, after)
    added, removed, changed_allowed = set(added), set(removed), set(changed_allowed)
    changed_keys = set(a_changed)

    unexpected_added = a_added - added
    missing_added = added - a_added
    unexpected_removed = a_removed - removed
    missing_removed = removed - a_removed
    unexpected_changed = changed_keys - changed_allowed

    # the exact-multiset check for value_exact keys
    exact_violations: dict = {}
    for (nid, k), (exp_rem, exp_add) in value_exact.items():
        if (nid, k) in a_changed:
            rem, add = _multiset_move(*a_changed[(nid, k)])
            bi, ai = _items(a_changed[(nid, k)][0]), _items(a_changed[(nid, k)][1])
            # an append (nothing removed, a given item added) must also KEEP THE ORDER of the
            # existing values: the before-sequence is a prefix of the after-sequence
            ok_order = not (exp_add is not None and not exp_rem) or ai[:len(bi)] == bi
            ok_rem = Counter(rem) == Counter(exp_rem) and ok_order
            ok_add = (len(add) == 1) if exp_add is None else (Counter(add) == Counter(exp_add))
            if not (ok_rem and ok_add):
                exact_violations[f"{nid}.{k}"] = {
                    "expected_removed": sorted(exp_rem),
                    "expected_added": "exactly one item" if exp_add is None else sorted(exp_add),
                    "actual_removed": rem, "actual_added": add}
        elif exp_add is not None and (nid, k) not in a_added:
            exact_violations[f"{nid}.{k}"] = {
                "expected_added": sorted(exp_add), "actual": "no change — the multiset did not grow"}

    if (unexpected_added or missing_added or unexpected_removed or missing_removed
            or unexpected_changed or exact_violations):
        actual = _delta(before, after)
        # A readable, node-named picture of the VALUE anomalies (the class the key-set
        # `actual` above cannot show), so the refusal explains itself.
        value_moves = {f"{nid}.{k}": {"before": bv, "after": av}
                       for (nid, k), (bv, av) in a_changed.items()
                       if (nid, k) in unexpected_changed}
        # Backward-compatible key-set-shaped evidence, so callers/tests that read the
        # original `intended_delta` / `unexplained` contract keep working.
        intended_delta: dict = {}
        for (nid, k) in added:
            intended_delta.setdefault(nid, {"added": [], "removed": []})["added"].append(k)
        for (nid, k) in removed:
            intended_delta.setdefault(nid, {"added": [], "removed": []})["removed"].append(k)
        for v in intended_delta.values():
            v["added"].sort(); v["removed"].sort()
        unexplained = {k: v for k, v in actual.items() if intended_delta.get(k) != v}
        raise EditRefused(
            f"the parsed property set moved by more than the intended change. "
            f"intended added {sorted(added)}, removed {sorted(removed)}, "
            f"value-change allowed on {sorted(changed_allowed)}; "
            f"UNEXPLAINED added {sorted(unexpected_added)}, removed {sorted(unexpected_removed)}, "
            f"VALUE-CHANGED {sorted(unexpected_changed)}, "
            f"MULTISET-MISMATCH {exact_violations or '{}'}. Nothing written.\n"
            f"⚠ this whole-file, VALUE-aware check catches an insertion landing in a "
            f"NEIGHBOURING node's block AND a lost LIST ELEMENT on an accumulated key — "
            f"both of which a 'did my key parse' or key-count check sails straight past.",
            evidence={"intended_added": sorted(added), "intended_removed": sorted(removed),
                      "intended_value_change": sorted(changed_allowed),
                      "unexpected_added": sorted(unexpected_added),
                      "unexpected_removed": sorted(unexpected_removed),
                      "unexpected_value_changed": sorted(unexpected_changed),
                      "multiset_mismatch": exact_violations,
                      "value_moves": value_moves,
                      "intended_delta": intended_delta, "unexplained": unexplained,
                      "actual_delta": actual, "nodes_checked": len(before)},
        )
    return before, after, _delta(before, after)


def _context(lines: list[str], idx0: int, radius: int = 2) -> tuple[str, ...]:
    lo, hi = max(0, idx0 - radius), min(len(lines), idx0 + radius + 1)
    return tuple(f"{i + 1:>5}{'>' if i == idx0 else ' '} {lines[i]}" for i in range(lo, hi))


# ---------------------------------------------------------------------------------
# lts_add_property
# ---------------------------------------------------------------------------------

def _requested_as_parsed(line_text: str, key: str):
    """What the PARSER makes of the single line `line_text` standing alone in a node: the requested value under the
    parser's OWN normalisation (strip, bold/simple forms, and whatever else it does), obtained by parsing it, never by
    re-deriving it."""
    pf = parse_text("# Probe [PROBE-ONE]\n\n" + line_text + "\n", "<value-check>")
    return next(n for n in pf.nodes if n.id == "PROBE-ONE").properties.get(key)


def _value_check(line_text: str, key: str, node_id: str, before: dict, after: dict, action: str) -> None:
    """THE ADD/APPEND VALUE CHECK (round 6, Opus Diamond N1). `_gate` accepts ANY value on an added key, and a byte gate
    cannot see an insertion that RE-ATTRIBUTES neighbouring lines (they are byte-identical, but the parser now folds
    them into the new value). So: the stored value AS PARSED must equal the requested value under the parser's own
    normalisation, and for an append the earlier items must be untouched. Otherwise refuse; nothing is written."""
    if action == "replaced":
        # NOT checked, on purpose (round 6): replacing a WRAPPED value leaves its old continuation lines attached to
        # the new line, so 88aa5d6 stores `new value + old tail` (measured: 7 of 235 real replaces). Refusing that
        # would change a live writer's outcome; it is ticketed as its own defect in the build report.
        return
    want = _requested_as_parsed(line_text, key)
    got = after[node_id].get(key)
    if action == "appended":
        items, prior = _items(got), _items(before[node_id].get(key))
        ok = items[:len(prior)] == prior and items[len(prior):] == _items(want)
    else:
        ok = got == want
    if not ok:
        raise EditRefused(
            f"the stored value of `{key}` on [{node_id}] is not the requested one: the parser reads {got!r} where the "
            f"line alone reads {want!r} (the new line has absorbed, or been absorbed by, neighbouring lines). "
            f"Nothing written.",
            evidence={"node_id": node_id, "key": key, "parsed_value": got, "requested_as_parsed": want,
                      "refusal": "gate_failed"})


@_serialised
def set_property(
    path: str | Path,
    node_id: str,
    key: str,
    value: str,
    *,
    allow_replace: bool = True,
    mode: str = "set",
    proposer: Optional[str] = None,
    approver: Optional[str] = None,
    reason: Optional[str] = None,
    via: Optional[str] = None,
    corpora: Optional[list] = None,        # adapter-only: membership at write time
    engine_head: Optional[str] = None,     # adapter-only: the engine build stamp
    ledger_roots: Optional[list] = None,   # adapter-only: registered roots a ledger must stay out of (L8)
    corpora_error: Optional[str] = None,   # adapter-only: why `corpora` could not be resolved
) -> EditResult:
    """Add or replace ONE property on ONE node. Coerces the key region, edits
    surgically, reconciles the WHOLE FILE through the real parser, then writes — or
    refuses and writes nothing.

    `mode="append"` ([TYPED-WRITER-APPEND]) adds ONE more `- key: value` line for a
    MULTI-VALUED key (e.g. `alias`) even when the key already exists, right after the last
    line carrying that key (and its folded continuation). Its gate requires the node's
    multiset of values for that key to grow by EXACTLY one and nothing else to move anywhere
    in the file. A value already present is refused; a nested-block key is refused;
    `allow_replace` is irrelevant (nothing is replaced). On an absent key it is a plain add."""
    if mode not in ("set", "append"):
        raise EditRefused(f"mode {mode!r} is not one of 'set' | 'append'. Nothing written.")
    meta = _meta(proposer=proposer, approver=approver, reason=reason, via=via, corpora=corpora,
                 engine_head=engine_head, ledger_roots=ledger_roots, corpora_error=corpora_error)
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise EditRefused(f"{path} is not a file — nothing written. "
                          f"(Arbitrary paths ARE accepted; this one does not exist.)")
    original = _read_base(path)

    fixed, coercions = _coerce_key(key)
    if not re.fullmatch(_KEY_LEGAL, fixed):
        raise EditRefused(_refusal_message(key, fixed, coercions))
    if "\n" in value:
        raise EditRefused("multi-line values are not supported by the surgical editor; "
                          "use the document writer. Nothing written.")
    if not value.strip():
        raise EditRefused("an empty value makes `- key:` a NESTED-BLOCK head, not a "
                          "property — the parser would look for indented items and find "
                          "prose. Give the field a value. Nothing written.")

    spans = _parsed_spans(original, str(path))
    if node_id not in spans:
        raise EditRefused(
            f"node [{node_id}] does not exist in {path} — nothing written. "
            f"Nodes present: {sorted(spans)[:20]}"
            f"{' …' if len(spans) > 20 else ''}"
        )
    before_props = _parsed_props(original, str(path))     # authoritative, reused by the gate

    lines = original.splitlines()
    start0 = spans[node_id][0] - 1              # line_range is 1-based inclusive
    end0 = _body_end(lines, start0)
    new_line = f"- {fixed}: {value}"

    existing = next((i for i in range(start0 + 1, end0)
                     if (m := _PROP_ANY.match(lines[i])) and m.group(1) == fixed), None)
    last, nested = _last_key_line(lines, start0, end0, fixed) if mode == "append" else (-1, False)
    if mode == "append" and last >= 0:
        existing_val = before_props.get(node_id, {}).get(fixed)
        dup = value.strip() in _items(existing_val)
        dup_msg = (f"[{node_id}] already carries `{fixed}: {value.strip()}` — appending an "
                   f"identical (key, value) would duplicate it. Nothing written.")
        dup_ev = {"node_id": node_id, "key": fixed, "existing_values": _items(existing_val)}
        if nested:
            # A nested LIST block (`- key:` then indented `- item` bullets) is the shape the
            # real corpora use for a multi-valued key (acme-docs TERM-PROBE's `alias`). The
            # new value joins THAT block as one more bullet at the block's own indent.
            # Anything else nested (a P5 mapping, a block mixed with flat `- key:` lines) is
            # refused: appending would mix shapes under one key and the parser keeps one.
            head = last                       # the nested head is the LAST line with the key
            end_blk = head + 1
            while end_blk < end0 and _INDENTED.match(lines[end_blk]):
                end_blk += 1
            bullets = [ln for ln in lines[head + 1:end_blk]
                       if re.match(r"^[ \t]+-[ \t]+\S", ln)]
            flat = [i for i in range(start0 + 1, end0)
                    if (m3 := _PROP_ANY.match(lines[i])) and m3.group(1) == fixed
                    and lines[i].split(":", 1)[1].strip()]
            if not isinstance(existing_val, list) or not bullets or flat \
                    or lines[head].split(":", 1)[1].strip():
                raise EditRefused(
                    f"[{node_id}] carries `{fixed}` as a nested block that is not a plain "
                    f"`- item` LIST (a mapping, a mixed block, or a block alongside flat "
                    f"`- {fixed}:` lines) — appending would mix shapes under one key and the "
                    f"parser keeps one. Edit it with the document writer. Nothing written.",
                    evidence={"node_id": node_id, "key": fixed, "existing_value": existing_val})
            if dup:
                raise EditRefused(dup_msg, evidence=dup_ev)
            indent = re.match(r"^([ \t]+)", bullets[0]).group(1)
            at = end_blk
            new_line = f"{indent}- {value.strip()}"
        elif isinstance(existing_val, dict):
            raise EditRefused(f"[{node_id}] carries `{fixed}` as a mapping — nothing "
                              f"appendable. Nothing written.",
                              evidence={"node_id": node_id, "key": fixed})
        else:
            if dup:
                raise EditRefused(dup_msg, evidence=dup_ev)
            at = last + 1
            while at < end0 and _INDENTED.match(lines[at]):    # skip the last line's folded tail
                at += 1
        lines = lines[:at] + [new_line] + lines[at:]
        written0, action = at, "appended"
    elif existing is not None and mode != "append":
        if not allow_replace:
            raise EditRefused(f"[{node_id}] already has `{fixed}` and allow_replace=False. "
                              f"Nothing written.")
        # ⛔ FAIL-LOUD ON AN ACCUMULATED / MULTI-VALUED KEY. A repeated key (or a nested
        # block) parses to a LIST — every element is live (`parser._maybe_multivalue`). A
        # single-line surgical replace would rewrite exactly ONE line and keep the rest,
        # silently destroying one element, and because the KEY still exists the key-set
        # gate cannot see the loss. Replace here is AMBIGUOUS: which element did you mean?
        # We refuse rather than guess. Measured 2026-09-26 (TCL-COMPARISON-API,
        # provenance x2), the maintainer-directed.
        existing_val = before_props.get(node_id, {}).get(fixed)
        if isinstance(existing_val, dict) or (
                not isinstance(existing_val, list) and not lines[existing].split(":", 1)[1].strip()):
            raise EditRefused(
                f"[{node_id}] holds `{fixed}` as a NESTED BLOCK (a mapping or an indented "
                f"item block) — a single-line REPLACE would fold the children into a scalar "
                f"and destroy the structure. Edit the block with the document writer. "
                f"Nothing written.",
                evidence={"node_id": node_id, "key": fixed, "existing_value": existing_val})
        if isinstance(existing_val, list):
            raise EditRefused(
                f"[{node_id}] holds {len(existing_val)} accumulated (multi-valued) values "
                f"for `{fixed}` — a repeated key or a nested block accumulates into a list, "
                f"and every element is live. A single-line REPLACE would silently overwrite "
                f"ONE element and keep the rest; the key set does not change, so the gate "
                f"could not catch the loss. This is AMBIGUOUS and refused — nothing written.\n"
                f"  To change ONE value: use `lts_repair_property` on its specific line.\n"
                f"  To collapse them to a single value: delete the extra lines first (the "
                f"document writer) then add.",
                evidence={"node_id": node_id, "key": fixed,
                          "accumulated_values": existing_val,
                          "value_count": len(existing_val)},
            )
        old_line = lines[existing]
        lines = lines[:existing] + [new_line] + lines[existing + 1:]
        written0, action = existing, "replaced"
    else:
        anchor = _property_block_end(lines, start0, end0)
        lines = lines[:anchor + 1] + [new_line] + lines[anchor + 1:]
        written0, action = anchor + 1, "added"

    candidate = "\n".join(lines) + ("\n" if original.endswith("\n") else "")

    if action == "added":
        before, after, actual = _gate(original, candidate, str(path),
                                      added=frozenset({(node_id, fixed)}),
                                      before=before_props)
    elif action == "appended":   # the (key, value) multiset grows by EXACTLY this one item
        before, after, actual = _gate(original, candidate, str(path),
                                      value_exact={(node_id, fixed): ([], [value.strip()])},
                                      before=before_props)
    else:  # replaced — the old scalar goes, exactly ONE new item arrives (or nothing, if idempotent)
        before, after, actual = _gate(
            original, candidate, str(path),
            value_exact={(node_id, fixed): (
                _items(before_props[node_id][fixed]) if fixed in before_props[node_id] else [], None)},
            before=before_props)

    _value_check(f"- {fixed}: {value}", fixed, node_id, before, after, action)   # round 6 (N1): the stored value is the requested one

    warnings: tuple[str, ...] = ()
    parsed_value = after[node_id][fixed]
    if action == "replaced" and before[node_id].get(fixed) == parsed_value:
        warnings += (f"`{fixed}` already parsed to this exact value; the file changed "
                     f"but the graph did not.",)

    discarded = ([{"node_id": node_id, "key": fixed, "line_number": existing + 1, "line": old_line,
                   "values": _items(before[node_id].get(fixed))}] if action == "replaced" else [])
    backup = _write(path, original, candidate, ctx={
        "verb": "append" if action == "appended" else "set_property", "node_id": node_id, "key": fixed,
        "delta": actual, "discarded": discarded, **meta,
        "inserted": ({"line_number": written0 + 1, "lines": [new_line]} if action != "replaced" else None)})
    return EditResult(
        path=str(path), node_id=node_id, key=fixed, value=value, action=action,
        coercions=tuple(coercions), key_as_authored=key if coercions else None,
        line_number=written0 + 1, line_text=new_line, parsed_value=parsed_value,
        properties_before=tuple(before[node_id]), properties_after=tuple(after[node_id]),
        delta=actual, nodes_checked=len(before), backup_path=str(backup),
        context=_context(lines, written0), warnings=warnings + oob_warnings(last_ledger(), path), ledger=last_ledger(),
    )


# ---------------------------------------------------------------------------------
# lts_repair_property — THE operation this whole series is about
# ---------------------------------------------------------------------------------
# ⛔ `set_property` ADDS. IT CANNOT REPAIR. (06 §7, found by USING the tools rather than
# reading them.) The losses in every scanned corpus are malformed EXISTING lines —
#
#     - ⛔ FLAGGED 2026-08-10: <value>
#
# — needing the glyph moved past the colon so the key region parses. `set_property` would
# have ADDED a second, correct property BESIDE the broken prose line, leaving the original
# as prose: the file then carries the fact twice, once queryable and once not, and the
# next reader cannot tell which one is authoritative. That is not a fix.
#
#     "The repair operation is the one this entire series is about, and it is the one
#      operation the authoring surface does not cover. Every document here is a report of
#      a loss; none of the tooling closes the loop from FOUND to FIXED through the
#      sanctioned path."
#
# The author who found that fixed their two losses WITH PYTHON — so on the one occasion in
# a nine-hour session where the sanctioned surface was loaded, a live finding was in hand
# and there was every intention of using it, the operation needed was not in it.
#
# ⛔ AND IT DOES NOT RE-RENDER. Same ruling as `set_property`, and it matters MORE here:
# canonical output over hand-authored prose makes a diff large enough to hide a real loss
# inside it, and the whole point of a repair is that the reviewer can see, in two lines of
# diff, exactly what moved.

# A leading markdown emphasis RUN — `**…**`, `*…*`, `__…__`, `_…_`. Used ONLY to find the
# end of a key region when the caller did not give a value; never to decide anything.
_EMPH_LEAD = re.compile(r"^(?P<open>\*\*\*|\*\*|\*|___|__|_)(?P<run>.+?)(?P=open)(?P<rest>.*)$")
# Colon not followed by `/` or `:` — the URI guard, so `doc://x` and `1:1:` are not read
# as key separators. Same predicate `factlattice_engine.key_loss` uses.
_KEY_COLON = re.compile(r":(?![/:])")
# Any bullet marker, INCLUDING the ones the parser will never accept — an en-dash bullet
# is itself one of the measured loss shapes, and a repair tool that cannot even see the
# line would be useless on it.
_ANY_BULLET = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*+–—•·●▪])[ \t]+(?P<body>.*)$")
_MAX_KEY_REGION = 80


def _split_key_region(body: str) -> tuple[str | None, str | None, str]:
    """(key_region, value, how) for a malformed bullet body.

    Two shapes, in order, and NOTHING else:
      1. a colon within the first `_MAX_KEY_REGION` chars — the `⛔ FLAGGED 2026-08-10:`
         shape, which is where the measured losses live;
      2. a leading emphasis RUN with text after it — the `**(S1) label** rest` shape that
         cost `overview.lts` five of its nineteen fields.

    Anything else returns `(None, None, reason)`. ⛔ It does NOT fall back to "take the
    first N words": guessing where a key ends is INVENTING a value, and a wrong value is a
    SUBSTITUTION — which this project has ruled worse than a loss, because the result
    looks well-formed. The caller is told to pass `value` explicitly instead.
    """
    cm = _KEY_COLON.search(body)
    if cm is not None and cm.start() <= _MAX_KEY_REGION:
        region, value = body[: cm.start()].strip(), body[cm.end():].strip()
        if region and value:
            return region, value, "colon"
        if region and not value:
            return None, None, (
                "the line's key region is followed by an EMPTY value — `- key:` with "
                "nothing after it is a NESTED-BLOCK head, not a property. Pass `value` "
                "explicitly if this line was meant to be a property")
    em = _EMPH_LEAD.match(body)
    if em is not None and em.group("rest").strip():
        return em.group("run").strip(), em.group("rest").strip(), "emphasis"
    return None, None, (
        "could not determine where the key region ends — the line has no key-shaped "
        f"colon in its first {_MAX_KEY_REGION} characters and no leading emphasis run. "
        "Pass `value` explicitly (everything that should follow the colon); guessing it "
        "would be INVENTING content, and a substitution is worse than a loss")


@_serialised
def repair_property(
    path: str | Path,
    node_id: str,
    line: int,
    key: str,
    value: str | None = None,
    *,
    proposer: Optional[str] = None,
    approver: Optional[str] = None,
    reason: Optional[str] = None,
    via: Optional[str] = None,
    corpora: Optional[list] = None,        # adapter-only: membership at write time
    engine_head: Optional[str] = None,     # adapter-only: the engine build stamp
    ledger_roots: Optional[list] = None,   # adapter-only: registered roots a ledger must stay out of (L8)
    corpora_error: Optional[str] = None,   # adapter-only: why `corpora` could not be resolved
) -> RepairResult:
    """Rewrite ONE malformed line, in place, so the field it was always meant to be parses.

    `line` is 1-based and REQUIRED. It is not a convenience: a repair names the exact line
    that is broken, because the alternative is the tool choosing which prose line the
    author meant — and choosing is authoring.

    `value` defaults to the value ALREADY ON THE LINE (everything after the key region),
    which is the shape of every measured loss: the value was never wrong, only the key.
    Pass it explicitly to override, or when the line's key region cannot be located.

    Gated EXACTLY as `set_property` is: after the surgical replacement the file is
    re-parsed and the parsed property set must differ from the pre-edit set by EXACTLY
    `{node_id: {"added": [key]}}` — per node, across the whole file. A refusal writes
    nothing.
    """
    meta = _meta(proposer=proposer, approver=approver, reason=reason, via=via, corpora=corpora,
                 engine_head=engine_head, ledger_roots=ledger_roots, corpora_error=corpora_error)
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise EditRefused(f"{path} is not a file — nothing written.")
    original = _read_base(path)
    lines = original.splitlines()

    if not isinstance(line, int) or line < 1 or line > len(lines):
        raise EditRefused(
            f"line {line} is outside {path} (1..{len(lines)}) — nothing written.",
            evidence={"lines_in_file": len(lines)})
    idx0 = line - 1
    line_before = lines[idx0]

    spans = _parsed_spans(original, str(path))
    if node_id not in spans:
        raise EditRefused(
            f"node [{node_id}] does not exist in {path} — nothing written. "
            f"Nodes present: {sorted(spans)[:20]}{' …' if len(spans) > 20 else ''}")
    start0 = spans[node_id][0] - 1
    end0 = _body_end(lines, start0)
    if not (start0 < idx0 < end0):
        raise EditRefused(
            f"line {line} is not inside [{node_id}]'s body (lines "
            f"{start0 + 2}..{end0}) — repairing it there would move the field onto the "
            f"WRONG NODE, which is a substitution and looks well-formed. Nothing written.",
            evidence={"node_body_lines": [start0 + 2, end0], "line": line,
                      "line_text": line_before})

    before_props = _parsed_props(original, str(path))
    bm = _ANY_BULLET.match(line_before)
    if bm is None:
        raise EditRefused(
            f"line {line} is not a bullet — repair rewrites a malformed `- key: value` "
            f"line, it does not turn prose into one. Nothing written.\n"
            f"  {line_before!r}",
            evidence={"line_text": line_before})
    if bm.group("indent"):
        raise EditRefused(
            f"line {line} is INDENTED — it belongs to a nested block or a folded "
            f"continuation, not to the top-level property grammar. Rewriting it would "
            f"silently truncate the block it is inside. Nothing written.",
            evidence={"line_text": line_before})
    if _PROP_ANY.match(line_before):
        existing_key = _PROP_ANY.match(line_before).group(1)
        raise EditRefused(
            f"line {line} ALREADY parses as a property (`{existing_key}`) — there is "
            f"nothing to repair. Use `lts_add_property` to change its value or key. "
            f"Nothing written.",
            evidence={"line_text": line_before, "parsed_key": existing_key})

    fixed, coercions = _coerce_key(key)
    if not re.fullmatch(_KEY_LEGAL, fixed):
        raise EditRefused(_refusal_message(key, fixed, coercions))
    if fixed in before_props.get(node_id, {}):
        raise EditRefused(
            f"[{node_id}] ALREADY carries a parsed property `{fixed}` — repairing this "
            f"line onto the same key would leave the node with two authored sources for "
            f"one field, and the parser keeps only one. Choose a different key, or amend "
            f"the existing property with `lts_add_property`. Nothing written.",
            evidence={"existing_value": before_props[node_id][fixed],
                      "line_text": line_before})

    key_region, derived_value, how = _split_key_region(bm.group("body"))
    if value is None:
        if derived_value is None:
            raise EditRefused(f"no `value` given and {how}. Nothing written.",
                              evidence={"line_text": line_before})
        value, value_source = derived_value, "derived_from_line"
    else:
        value_source = "caller"
    if "\n" in value:
        raise EditRefused("multi-line values are not supported by the surgical editor. "
                          "Nothing written.")
    if not value.strip():
        raise EditRefused(
            "an empty value makes `- key:` a NESTED-BLOCK head, not a property. "
            "Nothing written.")

    new_line = f"- {fixed}: {value}"
    lines2 = lines[:idx0] + [new_line] + lines[idx0 + 1:]
    candidate = "\n".join(lines2) + ("\n" if original.endswith("\n") else "")

    before, after, actual = _gate(original, candidate, str(path),
                                  added=frozenset({(node_id, fixed)}),
                                  before=before_props)

    warnings: tuple[str, ...] = ()
    if value_source == "derived_from_line" and how == "emphasis":
        warnings += (
            f"the value was derived by stripping a leading emphasis run "
            f"({key_region!r}); the emphasis is GONE from the file. If it carried "
            f"meaning, re-author it inside the value.",)
    parsed_value = after[node_id][fixed]
    if isinstance(parsed_value, str) and parsed_value.strip() != value.strip():
        warnings += (f"the parser folded the value: authored {value!r}, parsed "
                     f"{parsed_value!r}. The receipt reports what the GRAPH holds.",)

    backup = _write(path, original, candidate, ctx={
        "verb": "repair_property", "node_id": node_id, "key": fixed, "delta": actual,
        "discarded": [{"node_id": node_id, "key": None, "line_number": line, "line": line_before,
                       "values": []}], **meta})
    return RepairResult(
        path=str(path), node_id=node_id, key=fixed, value=value,
        line_number=line, line_before=line_before, line_after=new_line,
        key_region=key_region, value_source=value_source,
        coercions=tuple(coercions), key_as_authored=key if coercions else None,
        parsed_value=parsed_value,
        properties_before=tuple(before[node_id]), properties_after=tuple(after[node_id]),
        delta=actual, nodes_checked=len(before), backup_path=str(backup),
        context=_context(lines2, idx0), warnings=warnings + oob_warnings(last_ledger(), path), ledger=last_ledger(),
    )


# ---------------------------------------------------------------------------------
# lts_rename_property / lts_remove_property  [RENAME-REMOVE-PROPERTY]
# ---------------------------------------------------------------------------------
# WHY. `set_property` ADDS or REPLACES a value; `repair_property` rewrites a line that does NOT parse
# and REFUSES one that does. Nothing renamed or removed a line that parses, so the honest path ended
# where the hand edit began (35 `- confidence: high` lines had to become
# `- confidence-qualitative: high`). These two close that gap on the SAME machinery: the per-file
# lock (`_serialised`), the compare-and-swap atomic replace and backup (`_write`), the ledger, and
# the whole-file value-aware `_gate`. There is no second gate here.
#
# THREE DECISIONS THAT ARE NOT OBVIOUS
#   1. ⛔ BYTES, NOT LINES. The older writers read with universal newlines and re-join with "\n", so a
#      CRLF file is rewritten whole, and `str.splitlines` also splits on U+2028, U+000C, U+0085 and
#      the C0 separators, so a prose line containing one is silently changed. These writers split with
#      `keepends`, edit ONE element (rename) or remove exactly the lines the PARSER attributes to the target
#      (remove), and join: every other byte of the file is the original's. `_byte_gate` checks this at byte level
#      against the parser's own line-accounting record (round 5: the first version removed by a writer-side
#      indentation rule and the property-only gate could not see the lines vanish).
#   2. THE KEY LOCATOR IS NOT FENCE-AWARE, ON PURPOSE. The parser reads `- key: v` lines inside a code
#      fence as properties (measured), so a fence-aware locator would count fewer lines than the
#      parser has values and the two could not be reconciled. The locator mirrors the parser; the GATE
#      is what proves the edit moved the right thing.
#   3. THE EDGE RULE READS EDGES, NOT KEY NAMES. "key or new_key is in the minting tables" is the reason
#      a rename can change the graph, but the decision is made on the PARSED edge multiset before vs
#      after (`see-also` -> `relates-to` mints the same edge and needs no flag; a minting key whose
#      value holds no `[ID]` mints nothing and needs none).
#
# Every refusal carries a FIXED CODE in `evidence.refusal` (SDLC [TOOL-REASON-IS-A-FIXED-CODE]); the
# sentence in the message never carries the file path.

#: EVERY fixed code `evidence.refusal` can carry on `lts_rename_property` / `lts_remove_property`. One list: both tool
#: descriptions are built from it and SPEC 4.6.6 is checked against it (`tests/test_rename_remove_round2.py`).
WRITER_REFUSAL_CODES = (
    "node_not_found", "duplicate_node_id", "key_absent", "key_ambiguous", "line_not_key_line", "key_nested_block",
    "key_lines_unreconciled", "key_line_unlocatable", "expect_value_mismatch", "edge-context-incomplete", "new_key_exists", "new_key_same",
    "new_key_illegal", "edge_change_refused", "edge_change_outside_node", "gate_failed", "bad_argument",
    "not_a_file", "metadata_invalid", "os_error", "not_utf8", "lock_busy", "lock_unavailable", "write_refused")

#: what a result says when the caller (the MCP adapter) did not resolve the file's corpus profile.
EDGE_CHECK_UNRESOLVED = "built-in tables only (no profile applied: not_resolved)"


def _refuse(code: str, message: str, **evidence) -> EditRefused:
    return EditRefused(message, evidence={"refusal": code, **evidence})


def _coded_refusals(fn):
    """Under `_serialised`: any EditRefused that did not name its own code (a ledger-metadata bound, a
    read-only file, the CAS) still leaves with one, so the query log can persist it."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except EditRefused as exc:
            if not (exc.evidence or {}).get("refusal"):
                exc.evidence = {**(exc.evidence or {}), "refusal": "write_refused"}
            raise
    return wrapper


@dataclass
class RenameResult:
    path: str
    node_id: str
    key: str
    new_key: str
    new_key_as_authored: str | None
    coercions: tuple[str, ...]
    line_number: int                # 1-based, of the renamed line (the same line before and after)
    line_before: str
    line_after: str
    value: str                      # the value item, as the parser produced it BEFORE the rename
    parsed_value: object            # the new key's value as the parser produced it AFTER (== value)
    properties_before: tuple[str, ...]
    properties_after: tuple[str, ...]
    delta: dict
    nodes_checked: int
    backup_path: str
    edges_removed: list = field(default_factory=list)
    edges_added: list = field(default_factory=list)
    context: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    ledger: dict | None = None
    edge_check: str = EDGE_CHECK_UNRESOLVED     # which vocabulary the edge rule parsed with


@dataclass
class RemoveResult:
    path: str
    node_id: str
    key: str
    line_number: int                # 1-based, of the FIRST removed line
    removed_lines: list             # every physical line removed, verbatim (the line + its folded tail)
    value_removed: str              # the value item, as the parser produced it
    remaining_value: object         # what the key holds now (None when it is gone)
    properties_before: tuple[str, ...]
    properties_after: tuple[str, ...]
    delta: dict
    nodes_checked: int
    backup_path: str
    edges_removed: list = field(default_factory=list)
    edges_added: list = field(default_factory=list)
    context: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    ledger: dict | None = None
    edge_check: str = EDGE_CHECK_UNRESOLVED


def _read_raw(path) -> str:
    """The file's text with its line endings INTACT. Records the CAS base exactly like `_read_base`
    (which normalises newlines, and is therefore not what a byte-faithful writer wants)."""
    _read_base(path)
    return _bases()[os.path.realpath(path)].decode("utf-8")


def _split_keepends(text: str) -> tuple[list[str], list[str]]:
    """(contents, eols) by `str.splitlines` semantics, so index i is the parser's line i+1."""
    contents, eols = [], []
    for seg in text.splitlines(keepends=True):
        body = (seg.splitlines() or [""])[0]
        contents.append(body)
        eols.append(seg[len(body):])
    return contents, eols


def _key_match(line: str):
    """(match, nested_head) for a line the PARSER reads as a top-level property, else (None, False). The three
    regexes are the parser's own (`_parse_properties`), imported, never re-derived: a second line grammar is what
    let `- **k:** v` and `- k:v` look like properties here and not to the parser (Diamond F3)."""
    m = _PROP_BOLD_RE.match(line) or _PROP_SIMPLE_RE.match(line)
    if m:
        return m, False
    m = _PROP_KEY_ONLY_RE.match(line)
    return (m, True) if m else (None, False)


def _key_lines(lines: list[str], start0: int, end0: int, key: str) -> list[tuple[int, bool]]:
    """[(index, heads_a_nested_block)] for every top-level `- key:` line in the node body, in order, by the
    parser's own line grammar. NOT fence-aware, because the parser is not (measured: a `- k: v` line inside a
    ``` or ~~~ fence is a property), so this agrees with the parser on fenced lines by construction."""
    out = []
    for i in range(start0 + 1, end0):
        m, nested = _key_match(lines[i])
        if m and m.group(1) == key:
            out.append((i, nested))
    return out


def _splice_key(content: str, new_key: str) -> str:
    """The line with ONLY its key region replaced; the value and everything else is the original's bytes."""
    m, _nested = _key_match(content)
    return content[:m.start(1)] + new_key + content[m.end(1):]


def _edge_id(e) -> tuple:
    return (e.source, e.type, e.target, e.layer, e.inference, e.target_domain, e.label, bool(e.dangling))


def _edge_row(t: tuple) -> dict:
    return {"source": t[0], "type": t[1], "target": t[2], "layer": t[3], "inference": t[4],
            "target_domain": t[5], "dangling": t[7]}


def _edge_move(original: str, candidate: str, source_file: str, configs=None) -> tuple[list, list]:
    """(removed, added) edges between the two texts, as MULTISETS of edge identities (the line number an
    edge came from is deliberately NOT part of its identity: removing a line shifts every later one).

    `configs` is a list of `ParserConfig`s (one per corpus the file belongs to; `None` is the built-in tables only).
    The adapter resolves them from the file's corpus profile, so a profile-registered `property_edges` key
    (`documents` -> DOCUMENTS) mints in BOTH parses. A move seen under any config counts (Diamond F1)."""
    removed: Counter = Counter()
    added: Counter = Counter()
    for cfg in (configs or [None]):
        b = Counter(_edge_id(e) for e in parse_text(original, source_file, config=cfg).edges)
        a = Counter(_edge_id(e) for e in parse_text(candidate, source_file, config=cfg).edges)
        removed |= (b - a)
        added |= (a - b)
    return ([_edge_row(t) for t in sorted(removed.elements(), key=repr)],
            [_edge_row(t) for t in sorted(added.elements(), key=repr)])


def _gate_coded(*a, **kw):
    """`_gate`, whose refusal is given the fixed code `gate_failed`."""
    try:
        return _gate(*a, **kw)
    except EditRefused as exc:
        exc.evidence = {**(exc.evidence or {}), "refusal": "gate_failed"}
        raise


def _check_common_args(node_id, key, line) -> None:
    for name, v in (("node_id", node_id), ("key", key)):
        if not isinstance(v, str) or not v:
            raise _refuse("bad_argument", f"`{name}` must be a non-empty string. Nothing written.")
    if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
        raise _refuse("bad_argument", "`line` must be a positive integer (1-based) or omitted. Nothing written.")


def _open_node(path, node_id):
    """(path, original_text, lines, eols, start0, end0, before_props) -- or a coded refusal."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise _refuse("not_a_file", "the given path does not exist or is not a regular file. Nothing written.")
    original = _read_raw(path)
    lines, eols = _split_keepends(original)
    pf = parse_text(original, str(path))
    spans = {n.id: tuple(n.line_range) for n in pf.nodes}
    heads = [n.line_range[0] for n in pf.nodes if n.id == node_id]
    if len(heads) > 1:
        raise _refuse("duplicate_node_id",
                      f"node id [{node_id}] is defined {len(heads)} times, with headings on lines {heads}; the "
                      f"parser keeps the LAST and the first is invisible to the gate, so which did you mean? "
                      f"Give the nodes distinct ids first. Nothing written.", node_id=node_id, lines=heads)
    if node_id not in spans:
        raise _refuse("node_not_found",
                      f"node [{node_id}] does not exist in the file — nothing written. Nodes present: "
                      f"{sorted(spans)[:20]}{' …' if len(spans) > 20 else ''}", node_id=node_id)
    before_props = _parsed_props(original, str(path))
    start0 = spans[node_id][0] - 1
    return path, original, lines, eols, start0, _body_end(lines, start0), before_props


def _resolve_key_line(lines, start0, end0, before_props, node_id, key, line, verb):
    """The ONE line this call acts on: (idx0, ordinal, n_lines, item, value). Refuses, naming a fixed code, unless
    the answer is unambiguous AND reconciles with what the parser holds for the key."""
    value = before_props[node_id].get(key)
    found = _key_lines(lines, start0, end0, key)
    if not found:
        if key in before_props[node_id]:
            raise _refuse("key_line_unlocatable",
                          f"[{node_id}] holds `{key}` in the graph, but no top-level `- {key}:` line in its body "
                          f"carries it (a table row, a different bullet marker, or a bold form this writer does "
                          f"not rewrite). Edit it with the document writer. Nothing written.", node_id=node_id, key=key)
        raise _refuse("key_absent", f"[{node_id}] does not carry `{key}` — nothing to {verb}. Nothing written.",
                      node_id=node_id, key=key)
    numbers = [i + 1 for i, _n in found]
    if key not in before_props[node_id]:      # the locator saw a line the parser holds no key for
        raise _refuse("key_line_unlocatable",
                      f"a `- {key}:` line on [{node_id}] (line {numbers[0]}) is not a property to the parser, so "
                      f"there is nothing to {verb}. Nothing written.", node_id=node_id, key=key, lines=numbers)
    if line is not None:
        hit = [f for f in found if f[0] + 1 == line]
        if not hit:
            raise _refuse("line_not_key_line",
                          f"line {line} is not a `- {key}:` line on [{node_id}] (its `{key}` lines are "
                          f"{numbers}). Nothing written.", node_id=node_id, key=key, line=line, lines=numbers)
        chosen = hit[0]
    elif len(found) > 1:
        raise _refuse("key_ambiguous",
                      f"[{node_id}] carries `{key}` {len(found)} times, on lines {numbers}; every one is live, so "
                      f"which did you mean? Pass `line`. Nothing written.", node_id=node_id, key=key, lines=numbers)
    else:
        chosen = found[0]
    if any(n for _i, n in found) or isinstance(value, dict):
        raise _refuse("key_nested_block",
                      f"`{key}` on [{node_id}] heads a NESTED BLOCK (an indented list or a mapping); v1 does "
                      f"not {verb} a block, because the children would be orphaned or folded. Edit it with the "
                      f"document writer. Nothing written.", node_id=node_id, key=key, lines=numbers)
    items = _items(value)
    if len(items) != len(found):
        raise _refuse("key_lines_unreconciled",
                      f"[{node_id}] has {len(found)} `- {key}:` line(s) but the parser holds {len(items)} value(s) "
                      f"for it, so the line cannot be tied to its value. Nothing written.",
                      node_id=node_id, key=key, lines=numbers)
    ordinal = found.index(chosen)
    return chosen[0], ordinal, len(found), items[ordinal], value


def _removal_end(lines: list[str], idx0: int, *, fenced=None) -> int:
    """End (exclusive) of the lines a removal of the property headed at `lines[idx0]` takes: the head plus the
    wrapped tail THE PARSER folds into its value, by the parser's OWN rule (`parser._fold_continuation`, imported,
    never re-derived; a head is at column 0, so head indent 0, and `fenced` is the parser's own fence record).
    A bullet, ordered item, quote, table row, rule, heading or fence opener under the value is NOT part of it,
    however deeply indented (round 5, F1)."""
    return _fold_continuation(lines, idx0, 0, fenced=fenced, offset=0)[1]


def _line_starts(lines: list[str], eols: list[str]) -> list[int]:
    """Offset of each line's first character in the original text (an independent walk, not `_join`)."""
    out, at = [], 0
    for c, e in zip(lines, eols):
        out.append(at)
        at += len(c) + len(e)
    return out


def _byte_gate(original: str, candidate: str, source_file: str, lines: list[str], eols: list[str], idx0: int,
               *, rename_to: Optional[str] = None) -> None:
    """THE BYTE-LEVEL GATE (round 5). `_gate` compares PARSED PROPERTIES, so it cannot see a body line vanish:
    that is how F1 passed it. This one compares BYTES, against an expectation built from the PARSER's own
    line-accounting record of `original` (not from the writer's edit):

      remove: `candidate` == `original` minus EXACTLY the head line `idx0` and the lines the parser recorded
              as that value's folded tail (`BRANCH_PROP_CONT`, directly below it);
      rename: `candidate` == `original` with ONLY the key span of line `idx0` replaced by `rename_to`.

    Anything else is a refusal (`gate_failed`) and nothing is written."""
    cov = parse_text(original, source_file).coverage
    head = cov.consumed.get(idx0)
    if head not in (BRANCH_PROP_BOLD, BRANCH_PROP_SIMPLE):
        raise _refuse("gate_failed", f"byte gate: the parser did not read line {idx0 + 1} as a flat property head "
                      f"(it recorded {head!r}), so the edit has no attributed extent. Nothing written.", line=idx0 + 1)
    starts = _line_starts(lines, eols)
    if rename_to is not None:
        m, _nested = _key_match(lines[idx0])
        expected = original[:starts[idx0] + m.start(1)] + rename_to + original[starts[idx0] + m.end(1):]
        what = f"only the key span of line {idx0 + 1}"
    else:
        end = idx0 + 1
        while cov.consumed.get(end) == BRANCH_PROP_CONT:
            end += 1
        stop = starts[end] if end < len(starts) else len(original)
        expected = original[:starts[idx0]] + original[stop:]
        what = f"exactly lines {idx0 + 1}-{end} (the head and the {end - idx0 - 1} line(s) the parser folded into it)"
    if candidate != expected:
        n_exp, n_got = len(expected), len(candidate)
        at = next((i for i, (a, b) in enumerate(zip(expected, candidate)) if a != b), min(n_exp, n_got))
        raise _refuse("gate_failed",
                      f"byte gate: the new file differs from the original by more than {what}: it is {n_got} "
                      f"characters where the parser's attribution expects {n_exp}, first different at offset {at}. "
                      f"A line the parser did not attach to the property would have been lost or altered. "
                      f"Nothing written.", expected_chars=n_exp, got_chars=n_got, first_difference=at)


def _join(contents: list[str], eols: list[str]) -> str:
    return "".join(c + e for c, e in zip(contents, eols))


def _edge_rule(original, candidate, path, node_id, allow_edge_change, verb, configs, edge_check):
    """(edges_removed, edges_added); refuses when edges move and the caller did not allow it."""
    removed, added = _edge_move(original, candidate, str(path), configs)
    if not (removed or added):
        return [], []
    stray = [e for e in removed + added if node_id not in (e["source"], e["target"])]
    if stray:
        raise _refuse("edge_change_outside_node",
                      f"the {verb} would change edges that do not touch [{node_id}]: {stray}. That is not the "
                      f"intended change. Nothing written.", edges_removed=removed, edges_added=added,
                      edge_check=edge_check)
    if not allow_edge_change:
        raise _refuse("edge_change_refused",
                      f"the {verb} would REMOVE {len(removed)} and ADD {len(added)} minted edge(s) "
                      f"(see evidence.edges_removed / evidence.edges_added; edge_check: {edge_check}). The graph, "
                      f"not just the file, would change. Pass allow_edge_change=true to accept it. "
                      f"Nothing written.", edges_removed=removed, edges_added=added, edge_check=edge_check)
    return removed, added


def _could_be_edge(keys, item) -> bool:
    """True when this change COULD move an edge under SOME vocabulary: a key in the built-in minting tables, or a
    value holding an id-shaped `[ID]` reference (the only thing any profile's property_edges key can mint from)."""
    from ..parser import FLOW_PROPERTY_EDGES, REL_PROPERTY_EDGES, _extract_refs
    return any(k in REL_PROPERTY_EDGES or k in FLOW_PROPERTY_EDGES for k in keys if k) or bool(_extract_refs(item))


def _incomplete_rule(incomplete, keys, item, allow_edge_change, verb, edge_check):
    """FAIL CLOSED (Diamond F-A/F-C): the edge context is incomplete when a corpus containing the file has a profile
    that cannot be determined. A change that could be an edge key is then refused without `allow_edge_change`."""
    if not incomplete or allow_edge_change or not _could_be_edge(keys, item):
        return
    names = [f"{i['corpus']} ({i['code']})" if i["corpus"] is not None else f"(no corpus registry: {i['code']})"
             for i in incomplete]
    raise _refuse("edge-context-incomplete",
                  f"the {verb} touches a key that could be an edge key, but the profile of {', '.join(names)} cannot "
                  f"be determined, so the edge check would be narrower than the graph (edge_check: {edge_check}). "
                  f"Fix the registration, or pass allow_edge_change=true to accept the risk. Nothing written.",
                  corpora=list(incomplete), edge_check=edge_check)


def _mint_warnings(key, new_key, removed, added) -> tuple[str, ...]:
    from ..parser import FLOW_PROPERTY_EDGES, REL_PROPERTY_EDGES
    out: tuple[str, ...] = ()
    for k in (key, new_key):
        if k and (k in REL_PROPERTY_EDGES or k in FLOW_PROPERTY_EDGES) and not (removed or added):
            out += (f"`{k}` is a minting key, but this change moved no edge (its value holds no `[ID]` "
                    f"reference, or the other key mints the same edge).",)
    if "status" in (key, new_key):
        out += ("`status` feeds the node's lifecycle status, which the property gate does not compare.",)
    return out


def _quiet_context(lines: list[str], at: int, radius: int = 2) -> tuple[str, ...]:
    lo, hi = max(0, at - radius), min(len(lines), at + radius)
    return tuple(f"{i + 1:>5}  {lines[i]}" for i in range(lo, hi))


@_serialised
@_coded_refusals
def rename_property(
    path: str | Path,
    node_id: str,
    key: str,
    new_key: str,
    *,
    line: Optional[int] = None,
    allow_edge_change: bool = False,
    proposer: Optional[str] = None,
    approver: Optional[str] = None,
    reason: Optional[str] = None,
    via: Optional[str] = None,
    corpora: Optional[list] = None,        # adapter-only: membership at write time
    engine_head: Optional[str] = None,     # adapter-only: the engine build stamp
    ledger_roots: Optional[list] = None,   # adapter-only: registered roots a ledger must stay out of (L8)
    corpora_error: Optional[str] = None,   # adapter-only: why `corpora` could not be resolved
    edge_configs: Optional[list] = None,   # adapter-only: ParserConfigs of the file's corpora (profile property_edges)
    edge_check: Optional[str] = None,      # adapter-only: the sentence saying which vocabulary that was
    edge_incomplete: Optional[list] = None,   # adapter-only: [{corpus, code}] whose profile could not be determined
) -> RenameResult:
    """Rename the KEY of ONE property line on ONE node; the value is untouched.

    `new_key` goes through the same accommodate-then-refuse normalisation as `set_property`. The gate is
    the whole-file parsed delta: EXACTLY {node: `key` loses that one value, `new_key` gains the identical
    value}. A refusal writes nothing."""
    try:
        meta = _meta(proposer=proposer, approver=approver, reason=reason, via=via, corpora=corpora,
                     engine_head=engine_head, ledger_roots=ledger_roots, corpora_error=corpora_error)
    except EditRefused as exc:
        raise _refuse("metadata_invalid", str(exc)) from None
    _check_common_args(node_id, key, line)
    if not isinstance(new_key, str) or not new_key:
        raise _refuse("bad_argument", "`new_key` must be a non-empty string. Nothing written.")
    if not isinstance(allow_edge_change, bool):
        raise _refuse("bad_argument", "`allow_edge_change` must be true or false. Nothing written.")
    fixed, coercions = _coerce_key(new_key)
    if not re.fullmatch(_KEY_LEGAL, fixed):
        raise _refuse("new_key_illegal", _refusal_message(new_key, fixed, coercions))
    if fixed == key:
        raise _refuse("new_key_same", f"`new_key` {new_key!r} is the key it already has"
                      f"{' after normalisation' if coercions else ''} — there is nothing to rename. Nothing written.")

    path, original, lines, eols, start0, end0, before_props = _open_node(path, node_id)
    idx0, ordinal, n_lines, item, value = _resolve_key_line(lines, start0, end0, before_props, node_id,
                                                            key, line, "rename")
    if fixed in before_props[node_id]:
        raise _refuse("new_key_exists",
                      f"[{node_id}] already carries `{fixed}`; renaming `{key}` onto it would silently MERGE two "
                      f"properties, and the parser keeps both as a list. Remove or rename the other first. "
                      f"Nothing written.", node_id=node_id, key=key, new_key=fixed)

    line_before = lines[idx0]
    line_after = _splice_key(line_before, fixed)
    new_contents = lines[:idx0] + [line_after] + lines[idx0 + 1:]
    candidate = _join(new_contents, eols)
    _byte_gate(original, candidate, str(path), lines, eols, idx0, rename_to=fixed)

    if n_lines == 1:      # the key goes, the new key arrives
        before, after, actual = _gate_coded(original, candidate, str(path), removed=frozenset({(node_id, key)}),
                                            added=frozenset({(node_id, fixed)}), before=before_props)
    else:                 # one element of a list leaves the key; the key stays
        before, after, actual = _gate_coded(original, candidate, str(path), added=frozenset({(node_id, fixed)}),
                                            value_exact={(node_id, key): ([item], [])}, before=before_props)
    items = _items(value)
    rest = items[:ordinal] + items[ordinal + 1:]
    got_rest = _items(after[node_id][key]) if key in after[node_id] else []
    if after[node_id].get(fixed) != item or got_rest != rest:
        raise _refuse("gate_failed",
                      f"the parsed value did not travel intact: `{fixed}` holds {after[node_id].get(fixed)!r}, "
                      f"expected exactly {item!r}; `{key}` holds {got_rest!r}, expected {rest!r}. "
                      f"Nothing written.", node_id=node_id)
    edge_check = edge_check or EDGE_CHECK_UNRESOLVED
    removed, added = _edge_rule(original, candidate, path, node_id, allow_edge_change, "rename", edge_configs,
                                edge_check)
    _incomplete_rule(edge_incomplete, (key, fixed), item, allow_edge_change, "rename", edge_check)

    backup = _write(path, original, candidate, ctx={
        "verb": "rename_property", "node_id": node_id, "key": key, "delta": actual,
        "discarded": [{"node_id": node_id, "key": key, "line_number": idx0 + 1, "line": line_before,
                       "values": [item]}], **meta,
        "inserted": {"line_number": idx0 + 1, "lines": [line_after]}})
    return RenameResult(
        path=str(path), node_id=node_id, key=key, new_key=fixed,
        new_key_as_authored=new_key if coercions else None, coercions=tuple(coercions),
        line_number=idx0 + 1, line_before=line_before, line_after=line_after, value=item,
        parsed_value=after[node_id][fixed], properties_before=tuple(before[node_id]),
        properties_after=tuple(after[node_id]), delta=actual, nodes_checked=len(before),
        backup_path=str(backup), edges_removed=removed, edges_added=added,
        context=_context(new_contents, idx0),
        warnings=_mint_warnings(key, fixed, removed, added) + oob_warnings(last_ledger(), path),
        ledger=last_ledger(), edge_check=edge_check)


@_serialised
@_coded_refusals
def remove_property(
    path: str | Path,
    node_id: str,
    key: str,
    *,
    line: Optional[int] = None,
    expect_value: Optional[str] = None,
    allow_edge_change: bool = False,
    proposer: Optional[str] = None,
    approver: Optional[str] = None,
    reason: Optional[str] = None,
    via: Optional[str] = None,
    corpora: Optional[list] = None,        # adapter-only: membership at write time
    engine_head: Optional[str] = None,     # adapter-only: the engine build stamp
    ledger_roots: Optional[list] = None,   # adapter-only: registered roots a ledger must stay out of (L8)
    corpora_error: Optional[str] = None,   # adapter-only: why `corpora` could not be resolved
    edge_configs: Optional[list] = None,   # adapter-only: ParserConfigs of the file's corpora (profile property_edges)
    edge_check: Optional[str] = None,      # adapter-only: the sentence saying which vocabulary that was
    edge_incomplete: Optional[list] = None,   # adapter-only: [{corpus, code}] whose profile could not be determined
) -> RemoveResult:
    """Remove ONE property line and EXACTLY the wrapped tail the parser attributes to its value
    (`parser._fold_continuation`): never a sub-bullet, ordered item, quote, table row, rule, heading or fence
    under it, however indented. A byte-level gate (`_byte_gate`) refuses the write if the new file is not the
    old file minus exactly those lines.

    `expect_value`, when given, must equal the line's PARSED value exactly or nothing is written: the guard
    against removing the wrong line. The gate is the whole-file parsed delta: EXACTLY that one value gone
    from that node."""
    try:
        meta = _meta(proposer=proposer, approver=approver, reason=reason, via=via, corpora=corpora,
                     engine_head=engine_head, ledger_roots=ledger_roots, corpora_error=corpora_error)
    except EditRefused as exc:
        raise _refuse("metadata_invalid", str(exc)) from None
    _check_common_args(node_id, key, line)
    if expect_value is not None and not isinstance(expect_value, str):
        raise _refuse("bad_argument", "`expect_value` must be a string or omitted. Nothing written.")
    if not isinstance(allow_edge_change, bool):
        raise _refuse("bad_argument", "`allow_edge_change` must be true or false. Nothing written.")

    path, original, lines, eols, start0, end0, before_props = _open_node(path, node_id)
    idx0, ordinal, n_lines, item, value = _resolve_key_line(lines, start0, end0, before_props, node_id,
                                                            key, line, "remove")
    if expect_value is not None and item != expect_value:
        raise _refuse("expect_value_mismatch",
                      f"the `{key}` line on [{node_id}] (line {idx0 + 1}) holds {item!r}, not the expected "
                      f"{expect_value!r}. Nothing written.", node_id=node_id, key=key, line=idx0 + 1,
                      actual_value=item)
    end_rm = _removal_end(lines, idx0, fenced=parse_text(original, str(path)).coverage.fenced)
    removed_lines = lines[idx0:end_rm]
    new_contents = lines[:idx0] + lines[end_rm:]
    candidate = _join(new_contents, eols[:idx0] + eols[end_rm:])
    _byte_gate(original, candidate, str(path), lines, eols, idx0)

    if n_lines == 1:
        before, after, actual = _gate_coded(original, candidate, str(path), removed=frozenset({(node_id, key)}),
                                            before=before_props)
    else:
        before, after, actual = _gate_coded(original, candidate, str(path),
                                            value_exact={(node_id, key): ([item], [])}, before=before_props)
    items = _items(value)
    rest = items[:ordinal] + items[ordinal + 1:]
    got_rest = _items(after[node_id][key]) if key in after[node_id] else []
    if got_rest != rest:
        raise _refuse("gate_failed",
                      f"`{key}` holds {got_rest!r} after the removal, expected exactly {rest!r}. "
                      f"Nothing written.", node_id=node_id)
    edge_check = edge_check or EDGE_CHECK_UNRESOLVED
    removed, added = _edge_rule(original, candidate, path, node_id, allow_edge_change, "removal", edge_configs,
                                edge_check)
    _incomplete_rule(edge_incomplete, (key,), item, allow_edge_change, "removal", edge_check)

    backup = _write(path, original, candidate, ctx={
        "verb": "remove_property", "node_id": node_id, "key": key, "delta": actual,
        "discarded": [{"node_id": node_id, "key": key, "line_number": idx0 + 1,
                       "line": "\n".join(removed_lines), "values": [item]}], **meta, "inserted": None})
    return RemoveResult(
        path=str(path), node_id=node_id, key=key, line_number=idx0 + 1, removed_lines=list(removed_lines),
        value_removed=item, remaining_value=after[node_id].get(key),
        properties_before=tuple(before[node_id]), properties_after=tuple(after[node_id]),
        delta=actual, nodes_checked=len(before), backup_path=str(backup), edges_removed=removed,
        edges_added=added, context=_quiet_context(new_contents, idx0),
        warnings=_mint_warnings(key, None, removed, added) + oob_warnings(last_ledger(), path),
        ledger=last_ledger(), edge_check=edge_check)


# ---------------------------------------------------------------------------------
# lts_add_node
# ---------------------------------------------------------------------------------

@_serialised
def add_node(
    path: str | Path,
    node_id: str,
    title: str,
    properties: dict[str, str],
    *,
    section: str | None = None,
    level: int | None = None,
    proposer: Optional[str] = None,
    approver: Optional[str] = None,
    reason: Optional[str] = None,
    via: Optional[str] = None,
    corpora: Optional[list] = None,        # adapter-only: membership at write time
    engine_head: Optional[str] = None,     # adapter-only: the engine build stamp
    ledger_roots: Optional[list] = None,   # adapter-only: registered roots a ledger must stay out of (L8)
    corpora_error: Optional[str] = None,   # adapter-only: why `corpora` could not be resolved
    create: bool = False,                  # [A-E1] make the FILE (exclusive create); default unchanged
    create_parent: bool = False,           # [A-E1] with `create`: make exactly ONE missing parent dir
    doc_title: Optional[str] = None,       # [A-E1] frontmatter `title` (default: the node's title)
    domain: Optional[str] = None,          # [A-E1] frontmatter `domain` (omitted when None)
    doctype: Optional[str] = None,         # [A-E1] frontmatter `doctype` (omitted when None)
    _create_state: Optional[dict] = None,  # adapter-only: a per-CALL holder the create path reports into
) -> AddNodeResult:
    """Append a NEW node at the end of `section`'s subtree (or at end of document when
    `section` is None). Same gate: the whole-file parsed delta must be exactly the new
    node's properties on the new node, and NOTHING on any existing node.

    `create=True` ([A-E1]): the file must NOT exist; it is created by exclusive create under the
    per-file lock, holding frontmatter then this node. See `_create_node_file`."""
    meta = _meta(proposer=proposer, approver=approver, reason=reason, via=via, corpora=corpora,
                 engine_head=engine_head, ledger_roots=ledger_roots, corpora_error=corpora_error)
    if create_parent and not create:
        raise EditRefused("`create_parent` only means something with `create=True`. Nothing written.",
                          evidence={"refusal": "create_parent_needs_create"})
    if create:
        return _create_node_file(path, node_id, title, properties, meta=meta, section=section,
                                 level=level, create_parent=create_parent, doc_title=doc_title,
                                 domain=domain, doctype=doctype, state=_create_state)
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise EditRefused(f"{path} is not a file — nothing written.")
    original = _read_base(path)
    lines = original.splitlines()

    existing_spans = _parsed_spans(original, str(path))
    if node_id in existing_spans:
        raise EditRefused(
            f"[{node_id}] already exists in {path} (line {existing_spans[node_id][0]}). "
            f"A duplicate id collides in the graph. Use `set_property` to amend it, or "
            f"choose another id. Nothing written."
        )
    if not re.fullmatch(r"[A-Z][A-Z0-9_\-]*", node_id):
        raise EditRefused(
            f"node id {node_id!r} is not id-shaped (`^[A-Z][A-Z0-9_-]*$`) — the parser "
            f"would read the heading as a plain divider and mint NO NODE, silently. "
            f"Nothing written."
        )

    coerced = _coerce_properties(properties)

    heads = _headings(lines)
    if section is None:
        insert0, lvl = len(lines), (level or 2)
    else:
        sec = next((h for h in heads if h[2] == section), None)
        if sec is None:
            raise EditRefused(
                f"section [{section}] not found in {path} — nothing written. "
                f"Sections present: {[h[2] for h in heads if h[2]][:20]}"
            )
        sec_idx, sec_level = sec[0], sec[1]
        lvl = level or min(sec_level + 1, 6)
        insert0 = _subtree_end(lines, heads, sec_idx, sec_level)

    # back off over trailing blanks / thematic breaks so the new node does not land
    # after the document's tail furniture
    while insert0 - 1 > 0 and (not lines[insert0 - 1].strip()
                               or re.fullmatch(r"[-*_]{3,}", lines[insert0 - 1].strip())):
        insert0 -= 1

    block = ["", f"{'#' * lvl} {title.strip()} [{node_id}]", ""]
    block += [f"- {k}: {v}" for k, v, _ in coerced]
    lines2 = lines[:insert0] + block + lines[insert0:]
    candidate = "\n".join(lines2) + ("\n" if original.endswith("\n") else "")

    before, after, actual = _gate_new_node(
        original, candidate, str(path), node_id,
        added=frozenset((node_id, k) for k, _, _ in coerced))
    warnings: tuple[str, ...] = ()

    heading0 = insert0 + 1
    backup = _write(path, original, candidate, ctx={
        "verb": "add_node", "node_id": node_id, "key": None, "delta": actual, "discarded": [], **meta,
        "inserted": {"line_number": insert0 + 1, "lines": list(block)}})
    return AddNodeResult(
        path=str(path), node_id=node_id, section=section, title=title.strip(),
        heading_level=lvl, heading_line=heading0 + 1,
        properties=dict(after[node_id]),
        coercions={k: a for k, _, a in coerced if a},
        delta=actual, nodes_checked=len(after), backup_path=str(backup),
        context=_context(lines2, heading0, radius=len(block) + 1),
        warnings=warnings + oob_warnings(last_ledger(), path), ledger=last_ledger(),
    )


#: the refusal codes the create path itself emits (the SDLC X6 list). A create refusal whose code is NOT here is
#: not one of ours (an injected or foreign EditRefused) and its message is never relayed.
KNOWN_CREATE_CODES = frozenset({
    "create_file_exists", "create_parent_missing", "create_grandparent_missing", "create_parent_not_directory",
    "create_parent_needs_create", "create_section_unsupported", "create_ephemeral_root",
    "create_frontmatter_value", "create_frontmatter_lost", "create_argument_invalid", "create_node_id_invalid",
    "create_property_invalid", "create_gate_failed", "create_refused", "create_os_error",
    "create_unexpected_error", "create_outcome_unknown", "create_link_unsupported", "lock_busy",
    "lock_unavailable"})

CREATE_LINK_UNSUPPORTED_TEXT = (
    "this filesystem did not allow the no-overwrite hard link that `create` places the file with, and "
    "`create` has no other way to create a file. Nothing of this create is at the path; no ledger record of it.")

CREATE_UNDO_NOTE = "created file: undo of a create is not supported"
CREATE_GENERATED_BY = "factlattice lts_add_node (create=true)"


@contextmanager
def _coded(code: str):
    """[P2-2] a shared validator's refusal (key coercion, the parse gate) raised on the CREATE path
    gets a stable `evidence.refusal` code; a refusal that already carries one keeps it."""
    try:
        yield
    except EditRefused as e:
        if "refusal" not in (e.evidence or {}):
            e.evidence = {**(e.evidence or {}), "refusal": code}
        raise


def _fm_value(name: str, v: Optional[str], *, default: Optional[str] = None) -> Optional[str]:
    """A frontmatter value must round-trip through the parser's one-line `key: value` field: no line
    breaks, not empty. None means 'omit the key' (or the default)."""
    if v is None:
        v = default
    if v is None:
        return None
    v = str(v)
    if "\n" in v or "\r" in v or not v.strip():
        raise EditRefused(f"frontmatter `{name}` must be a single non-empty line (a multi-line or empty "
                          f"value cannot round-trip the parser's frontmatter field). Nothing written.",
                          evidence={"refusal": "create_frontmatter_value"})
    return v.strip()


#: ONLY these errnos from `os.link` mean "this filesystem cannot make the no-overwrite hard link". EMLINK is
#: deliberately NOT here: it is a per-inode / per-directory link LIMIT, and our temp is a fresh inode with one
#: link, so it says nothing about hard-link support (it is an ordinary os error).
LINK_SUPPORT_ERRNOS = frozenset(getattr(errno, n) for n in ("EPERM", "ENOTSUP", "EOPNOTSUPP", "EXDEV", "ENOSYS")
                                if hasattr(errno, n))


class _LinkUnsupported(Exception):
    """The no-overwrite hard link could not be made (anything but EEXIST). Carries only an errno NAME (or the
    exception type) and, if the temp could not be removed, the TEMP path. Never the target."""

    def __init__(self, what: str, leftover_temp: Optional[str] = None):
        super().__init__(what)
        self.what, self.leftover_temp = what, leftover_temp


def _unlink_quiet(p) -> bool:
    try:
        os.unlink(p)
        return True
    except FileNotFoundError:
        return True
    except Exception:  # noqa: BLE001 -- includes anything a wrapped unlink raises: the answer is just "not removed"
        return False


def _create_exclusive(real: Path, data: bytes, state: Optional[dict] = None) -> None:
    """Make `real` exist with exactly `data` through ONE primitive, or raise and leave nothing at `real`.

    The temp is O_EXCL in the target's directory; `os.link` onto the target is the atomic no-overwrite
    step (EEXIST if anything, a symlink included, is there), so a reader never sees a partial file and a
    creator that loses a race, even with no lock, cannot replace the winner.

    THERE IS NO FALLBACK (delta6, the architect's ruling B). A filesystem that cannot hard-link refuses:
    `os.link` failing with anything but EEXIST raises `_LinkUnsupported` BEFORE anything is observable at the
    target, and the temp is removed (if even that fails, the TEMP path, only that, rides on the exception).
    EEXIST stays `FileExistsError`. The one recovery kept is the inode check: if `os.link` raised but the
    target IS our inode, the link happened and something raised after it.

    IDENTITY (delta5): OUR file is identified by (st_dev, st_ino), recorded in `state["ident"]` the moment we
    have it (the temp's inode; the link target IS that inode). Bytes are never identity."""
    tmp = real.parent / f".{real.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except BaseException:
        _unlink_quiet(tmp)                  # the call may have created the file before it raised
        raise
    leftover: Optional[str] = None
    try:
        try:
            st = os.fstat(fd)
            if state is not None:
                state["ident"] = (st.st_dev, st.st_ino)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fchmod(fd, 0o644)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            if state is not None:
                state["link_attempted"] = True
            os.link(tmp, real)
        except Exception as e:
            if state is not None and _lstat_ident(real) == state.get("ident"):
                pass                                  # the link HAPPENED, then something raised: it is ours
            else:
                if state is not None:
                    state["not_ours"] = True          # whatever is at the path is not our inode
                if isinstance(e, FileExistsError):
                    raise
                if isinstance(e, OSError) and e.errno in LINK_SUPPORT_ERRNOS:
                    raise _LinkUnsupported(errno.errorcode[e.errno]) from None
                raise                                 # any other failure keeps its own class (os error / unexpected)
        if state is not None:
            state["written"] = True                  # the file IS on disk
    finally:
        if not _unlink_quiet(tmp):
            leftover = str(tmp)
            if state is not None:
                state["leftover_temp"] = leftover
        # a refusal that left its temp behind must say where (the TEMP path only)
        exc = sys.exc_info()[1]
        if isinstance(exc, _LinkUnsupported) and leftover:
            exc.leftover_temp = leftover


def _lstat_ident(p):
    """(st_dev, st_ino) of whatever is at `p` (symlinks not followed), None if nothing, and it RAISES for any
    other OSError: the caller must not mistake "could not look" for "not there"."""
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino)


def _create_node_file(path, node_id, title, properties, *, meta, section, level, create_parent,
                      doc_title, domain, doctype, state=None) -> AddNodeResult:
    """[A-E1 / X6 CREATE-WRITER] The `create=True` half of `add_node`. Under the per-file lock:

      validate everything (no directory or file is touched until every refusal has had its say) ->
      refuse an ephemeral root, an existing target (symlink included), a missing parent unless
      `create_parent` (then EXACTLY one level, and only if the grandparent exists) ->
      build frontmatter + the node and run the parse GATE on it (the file gains exactly this node and
      its properties; every frontmatter key authored comes back from the parser) ->
      make the parent if asked, then exclusive-create the file -> ONE ledger record.

    Called ONLY from `add_node`, which is `_serialised` already: the per-file lock is held (flock is
    per open file description, so taking it again here would deadlock against ourselves).

    `state` (adapter-only, a per-CALL dict, never a thread-local) is the CONTRACT by which the adapter's
    single observation point learns how far this got. The core sets, in this order: `real` BEFORE the link;
    `ident` = (st_dev, st_ino) of OUR file the moment we have it (the temp's inode); `link_attempted` just
    before the link; `written` when the file is on disk (the link returned, OR the link call raised but the
    path has our inode); `not_ours` when the link call failed and what is at the path is not our inode;
    `leftover_temp` when the temp could not be removed; `ledger_seq` the instant `_ledger_append` has
    RETURNED with this write's record (it is the ONLY proof of a record; there is no lookup); `created_parent`.
    IDENTITY IS THE INODE, never the bytes. The adapter never trusts an inner handler: it decides the outcome
    (success | verified refusal | unknown) from this state and the disk AFTER the core exits, however it
    exits. A new step after the link must keep to that contract.

    Undo position: NONE. The record says so (`undo_evidence_missing`), and `ledger.check_undoable`
    refuses it loudly."""
    if section is not None:
        raise EditRefused("`create=True` makes a new file with no sections; `section` has nothing to "
                          "name. Nothing written.", evidence={"refusal": "create_section_unsupported"})
    if not re.fullmatch(r"[A-Z][A-Z0-9_\-]*", node_id):
        raise EditRefused(
            f"node id {node_id!r} is not id-shaped (`^[A-Z][A-Z0-9_-]*$`) — the parser would read the "
            f"heading as a plain divider and mint NO NODE, silently. Nothing written.",
            evidence={"refusal": "create_node_id_invalid"})
    with _coded("create_property_invalid"):
        coerced = _coerce_properties(properties)
    fm_title = _fm_value("title", doc_title, default=title)
    fm = [("lts", '"1.1"'), ("title", fm_title)]
    for k, v in (("domain", _fm_value("domain", domain)), ("doctype", _fm_value("doctype", doctype))):
        if v is not None:
            fm.append((k, v))
    fm.append(("generated-by", CREATE_GENERATED_BY))

    raw = Path(path).expanduser()
    real = Path(os.path.realpath(raw))
    if _ledger.is_ephemeral(real):
        raise EditRefused(
            f"{real} is under an ephemeral root (the OS temp dir): a write there is not ledgered, and a "
            f"created file must be. Choose a durable location. Nothing written.",
            evidence={"refusal": "create_ephemeral_root"})
    if os.path.lexists(raw) or os.path.lexists(real):
        raise EditRefused(f"{real} already exists (a file, an empty file or a symlink): `create` never "
                          f"overwrites. Use `lts_add_node` without `create` to add to it. Nothing written.",
                          evidence={"refusal": "create_file_exists"})
    parent = real.parent
    if os.path.lexists(parent) and not parent.is_dir():
        raise EditRefused(f"{parent} exists and is not a directory. Nothing written.",
                          evidence={"refusal": "create_parent_not_directory"})
    need_parent = not parent.is_dir()
    if need_parent:
        if not create_parent:
            raise EditRefused(f"the parent directory {parent} does not exist, and `create` makes no "
                              f"directories unless `create_parent=true`. Nothing written.",
                              evidence={"refusal": "create_parent_missing"})
        if not parent.parent.is_dir():
            raise EditRefused(f"`create_parent` makes exactly ONE directory level, and {parent.parent} "
                              f"(the grandparent) does not exist either. Nothing written.",
                              evidence={"refusal": "create_grandparent_missing"})

    lvl = level or 1
    block = ["", f"{'#' * lvl} {title.strip()} [{node_id}]", ""]
    block += [f"- {k}: {v}" for k, v, _ in coerced]
    lines2 = ["---"] + [f"{k}: {v}" for k, v in fm] + ["---"] + block
    candidate = "\n".join(lines2) + "\n"

    with _coded("create_gate_failed"):
        before, after, actual = _gate_new_node(
            "", candidate, str(real), node_id, added=frozenset((node_id, k) for k, _, _ in coerced))
    got_fm = parse_text(candidate, str(real)).frontmatter
    lost_fm = [k for k, v in fm if got_fm.get(k) != v]
    if lost_fm:
        raise EditRefused(f"frontmatter keys did not round-trip through the parser: {lost_fm}. "
                          f"Nothing written.", evidence={"refusal": "create_frontmatter_lost",
                                                         "lost": lost_fm})

    if state is not None:
        state.update(real=str(real))
    _bases()[str(real)] = None                  # the file did not exist: file_sha256_before = null
    made_parent: Optional[str] = None
    if need_parent:
        try:
            os.mkdir(parent, 0o755)
        except FileExistsError:
            if not parent.is_dir():
                raise EditRefused(f"{parent} appeared and is not a directory. Nothing written.",
                                  evidence={"refusal": "create_parent_not_directory"})
        else:
            made_parent = str(parent)
    try:
        _sweep_stale_temps(real)
        try:
            _create_exclusive(real, candidate.encode("utf-8"), state)
        except FileExistsError:
            raise EditRefused(f"{real} was created by another writer a moment ago: `create` never "
                              f"overwrites. Nothing written.",
                              evidence={"refusal": "create_file_exists"}) from None
        except _LinkUnsupported as lu:
            ev = {"refusal": "create_link_unsupported", "errno": lu.what}
            if lu.leftover_temp:
                ev["leftover_temp"] = lu.leftover_temp       # the TEMP path only: remove it by hand
            raise EditRefused(CREATE_LINK_UNSUPPORTED_TEXT, evidence=ev) from None
    except BaseException:
        if made_parent:
            try:
                os.rmdir(parent)                # best effort: we made it, it is empty
            except OSError:
                pass
        raise
    if state is not None:
        state.update(written=True, stage="after_write", created_parent=made_parent)
    delta = dict(actual)
    if made_parent:
        delta["created_parent"] = made_parent
    heading_idx = len(["---"] + fm + ["---"]) + 1           # 0-based index of the node heading
    _ledger_append(real, {"verb": "add_node", "node_id": node_id, "key": None, "delta": delta,
                          "discarded": [], **meta, "backup_path": None, "inserted": None,
                          "undo_evidence_missing": CREATE_UNDO_NOTE, "_state": state}, candidate)
    return AddNodeResult(
        path=str(real), node_id=node_id, section=None, title=title.strip(), heading_level=lvl,
        heading_line=heading_idx + 1, properties=dict(after[node_id]),
        coercions={k: a for k, _, a in coerced if a}, delta=actual, nodes_checked=len(after),
        backup_path=None, context=_context(lines2, heading_idx, radius=len(block) + 1),
        warnings=oob_warnings(last_ledger(), real), ledger=last_ledger(),
        created=True, created_parent=made_parent)


def _coerce_properties(properties: dict) -> list[tuple[str, str, list[str]]]:
    """Coerce every key BEFORE touching the file; refuse as a set, not one at a time.
    Returns [(key, value, coercions)]."""
    coerced: list[tuple[str, str, list[str]]] = []
    bad: list[str] = []
    for k, v in properties.items():
        fixed, applied = _coerce_key(k)
        if not re.fullmatch(_KEY_LEGAL, fixed):
            bad.append(_refusal_message(k, fixed, applied))
        elif not str(v).strip():
            bad.append(f"key {k!r} has an empty value; `- key:` is a nested-block head, "
                       f"not a property.")
        elif "\n" in str(v):
            bad.append(f"key {k!r} has a multi-line value; use the document writer.")
        else:
            coerced.append((fixed, str(v), applied))
    if bad:
        raise EditRefused("refused before writing anything:\n  - " + "\n  - ".join(bad))
    if len({k for k, _, _ in coerced}) != len(coerced):
        raise EditRefused("two authored keys coerce to the SAME key; one would silently "
                          "overwrite the other. Nothing written.")
    return coerced


def _gate_new_node(original: str, candidate: str, source_file: str,
                   node_id: str, *, added: frozenset) -> tuple[dict, dict, dict]:
    """Same VALUE-aware gate, one relaxation: the node SET is expected to gain exactly
    `node_id`. `added` is the (node_id, key) pairs the new node introduces; NOTHING on any
    existing node may be added, removed, or have its VALUE changed."""
    before = _parsed_props(original, source_file)
    after = _parsed_props(candidate, source_file)
    gained, lost = set(after) - set(before), set(before) - set(after)
    if gained != {node_id} or lost:
        raise EditRefused(
            f"the edit was meant to mint exactly [{node_id}]; it minted {sorted(gained)} "
            f"and lost {sorted(lost)}. Nothing written. "
            f"({'the heading minted NO node — check the id is bracketed and id-shaped' if not gained else 'a heading was displaced'})",
            evidence={"nodes_gained": sorted(gained), "nodes_lost": sorted(lost)},
        )
    a_added, a_removed, a_changed = _value_pairs(before, after)
    added = set(added)
    unexpected_added = a_added - added
    missing_added = added - a_added
    if unexpected_added or missing_added or a_removed or a_changed:
        actual = _delta(before, after)
        value_moves = {f"{nid}.{k}": {"before": bv, "after": av}
                       for (nid, k), (bv, av) in a_changed.items()}
        raise EditRefused(
            f"the parsed property set moved by more than the new node. "
            f"intended added {sorted(added)}; UNEXPLAINED added {sorted(unexpected_added)}, "
            f"removed {sorted(a_removed)}, VALUE-CHANGED {sorted(a_changed)}. Nothing written.",
            evidence={"intended_added": sorted(added),
                      "unexpected_added": sorted(unexpected_added),
                      "unexpected_removed": sorted(a_removed),
                      "unexpected_value_changed": sorted(a_changed),
                      "value_moves": value_moves,
                      "actual_delta": actual, "nodes_checked": len(after)},
        )
    return before, after, _delta(before, after)


def _subtree_end(lines: list[str], heads, sec_idx: int, sec_level: int) -> int:
    """End (exclusive, 0-based) of a section's subtree.

    Stops at the next heading of level <= the section's, AND at any reserved
    edge-section heading (`## Relationships`, `## Causal Graph`) regardless of level.
    Without the second rule an h1-rooted section swallows the whole document and a new
    node lands AFTER the relationships block, where its properties are still parsed but
    a human reading the file would call it misfiled.
    """
    for idx, level, _nid, title in heads:
        if idx <= sec_idx:
            continue
        if level <= sec_level or title.strip().lower() in RESERVED_SECTION_TITLES:
            return idx
    return len(lines)


def _body_end(lines: list[str], start0: int) -> int:
    """End (exclusive) of a node's own body: the next heading line of ANY level, which
    is exactly the parser's rule ("to the line before the next heading of ANY level")."""
    in_fence = False
    for i in range(start0 + 1, len(lines)):
        s = lines[i].strip()
        if s.startswith("```") or s.startswith("~~~"):
            in_fence = not in_fence
            continue
        if not in_fence and _HEADING_RE.match(lines[i]):
            return i
    return len(lines)


def _refusal_message(raw: str, fixed: str, applied: list[str]) -> str:
    was = f"key {raw!r}" + (f" -> {fixed!r}" if applied else "")
    if fixed and fixed[0].isdigit():
        hint = (" The key begins with a DIGIT. Legalising it means INVENTING a prefix "
                "(`doc-003-…`, `corpus-003-…`) and choosing one is AUTHORING, not "
                "normalisation. Name the field yourself — this is the one class the "
                "boundary deliberately will not decide for you.")
    elif not fixed:
        hint = (" Nothing survived coercion — the key region held no alphanumeric "
                "character at all. There is no intent here to infer.")
    else:
        illegal = sorted({c for c in fixed if not re.match(r"[a-z0-9_\-]", c)})
        hint = (f" Remaining illegal characters {illegal} are not in the "
                f"unambiguous-coercion set, so correcting would require guessing what "
                f"you meant.")
    return f"{was} is not a legal key.{hint} Nothing written."


def backup_dir() -> Path:
    """Where backups live. NEVER beside the file being edited.

    ⛔ THE SANCTIONED WRITER'S BACKUP WAS THE ARTIFACT THE SANCTIONED LINTER EXISTS TO
    CATCH. `path.with_suffix(path.suffix + ".bak")` dropped `60-board-hardware.lts.bak`
    INSIDE the corpus root, and `lint/corpus_files.py::KIND_SHADOW_COPY` immediately —
    and correctly — flags it: "outside the `*.lts` glob, so the linter, the index builder
    and lts_find all ignore it, while a human or an agent grepping this directory reads it
    as content." Every `lts_add_property` call manufactured one.

    Worse than untidy: a shadow copy of a LIVE document with nothing marking which is
    authoritative is a fork, and the reader who opens the wrong one has no signal at all.

    So backups go to a scratch tree outside every corpus, `FACTLATTICE_BACKUP_DIR` if set,
    and they are TIMESTAMPED — the old scheme also let a second edit silently overwrite
    the only copy of the pre-first-edit file, so the receipt's promise that "a reversal
    must not require reconstructing the original from memory" held for exactly one edit.
    """
    env = os.environ.get("FACTLATTICE_BACKUP_DIR")
    base = Path(env).expanduser() if env else Path(tempfile.gettempdir()) / "factlattice-backups"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _backup_path(path: Path) -> Path:
    """A collision-proof scratch path that still names the file it came from.

    ⛔ MICROSECONDS, AND THEN A UNIQUIFYING LOOP. A first cut stamped to the SECOND and
    reproduced the exact defect it was written to fix: two edits inside one second landed
    on the same name, and the second silently overwrote the only pre-edit copy. Caught by
    `test_a_second_edit_does_not_overwrite_the_only_pre_edit_copy` — which is why that
    test asserts on TWO edits rather than one. A guard whose control exercises a single
    call certifies a narrower property than its name claims.
    """
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:8]
    base = backup_dir()
    for n in range(1000):
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S.%f")
        candidate = base / f"{path.name}.{stamp}.{digest}{'' if not n else f'.{n}'}.bak"
        if not candidate.exists():
            return candidate
    raise EditRefused(f"could not allocate a unique backup name under {base}; refusing "
                      f"to overwrite an existing backup. Nothing written.")


def _write(path: Path, original: str, candidate: str, *, ctx: Optional[dict]) -> Path:
    """Backup, then ATOMICALLY replace. The backup is part of the receipt: a reversal must not
    require reconstructing the original from memory — see `backup_dir` for why it does NOT
    live beside the original. The caller holds `_file_lock` (every writer is `_serialised`)."""
    _preflight(Path(os.path.realpath(path)))
    backup = _backup_path(path)
    shutil.copy2(path, backup)
    _sweep_stale_temps(path)
    _atomic_replace(path, candidate)      # no in-place truncate: a kill leaves the old or the new file
    if ctx is not None:                   # [NODE-CHANGE-LEDGER] inside the lock, right after os.replace
        _ledger_append(Path(os.path.realpath(path)),
                       {**ctx, "original": original, "backup_path": str(backup)}, candidate)
    return backup
