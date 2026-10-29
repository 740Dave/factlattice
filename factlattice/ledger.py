"""[NODE-CHANGE-LEDGER] v1 -- the append-only, hash-chained record of every APPLIED typed write.

One JSON line per applied write, one ledger file PER CORPUS FILE:

    <state_dir>/ledger/<sha256(realpath)[:32]>.jsonl        dir 0700, file 0600

WHY A SEPARATE MODULE, OUTSIDE factlattice/authoring
    `tests/test_atomic_write_round2.py::test_no_authoring_module_writes_a_corpus_file_directly`
    fails the build on a direct file write inside factlattice/authoring. The ledger is NOT a
    corpus file, so its writer lives here and the guard stays exactly as strict as it was.
    `factlattice.authoring.edit` calls `append` from its ONE shared locked step, right after
    `os.replace`, still inside the per-file lock, so `ledger_seq` and `prev_sha256` are
    serialised by the lock that already serialises the file (no second lock).

STATE DIR
    Same resolution as `edit._lock_dir()` / `factlattice_engine.querylog.state_dir()`:
    $FACTLATTICE_STATE_DIR, else $XDG_STATE_HOME/factlattice, else ~/.local/state/factlattice.

[L8] A LEDGER PATH NEVER LANDS INSIDE A REGISTERED CORPUS ROOT
    `factlattice` cannot see the engine's corpus registry, so the check is INJECTED: the engine
    adapter passes the registered roots (`roots=`), and `append` refuses a ledger path under any
    of them (`check_outside_roots`). Independently of the registry, a ledger path inside the
    written file's OWN directory tree is always refused (a bare call has no registry to ask).
    Refusal is a `LedgerError`; the caller turns it into `ledger: {status: "failed"}` on the
    receipt -- the corpus write has already landed by then, and that is said loudly.

Failures are LOUD, never silent: `append` raises `LedgerError`; it never swallows.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

V = 1
# the record's field order (the table in the spec); `append` fills the writer-owned ones
FIELDS = ("v", "ts", "ledger_seq", "path_real", "verb", "node_id", "key", "delta", "discarded",
          "node_hash_before", "node_hash_after", "file_sha256_before", "file_sha256_after",
          "proposer", "approver", "reason", "via", "corpora", "corpora_error", "engine_head",
          "backup_path", "inserted", "follows_out_of_band_edit", "prior_blob", "undo_evidence_missing", "prev_sha256")
# [LEDGER-VERB-VOCAB] the verbs a record may carry. A NEW verb is added here WITH a test, and undo/verify
# (`check_undoable`) refuse any verb not listed, loudly, instead of treating it as an ordinary edit.
KNOWN_VERBS = ("set_property", "append", "repair_property", "add_node", "write_document",
               "rename_property", "remove_property")
CREATE_UNDO_PREFIX = "created file:"
MAX_REASON, MAX_PRINCIPAL, MAX_VIA = 2000, 200, 200
_TAIL_CHUNK = 64 * 1024


class LedgerError(Exception):
    """The ledger could not be appended. The corpus write (if any) has ALREADY landed."""


def check_undoable(rec: dict) -> None:
    """The ONE decision point for undo/verify over a ledger record: raise `LedgerError` for a record that
    must not be treated as an ordinary reversible edit.
      * an unknown verb (a verb added without teaching undo/verify about it);
      * a FILE CREATION (`add_node` whose `file_sha256_before` is null and whose `undo_evidence_missing`
        says a file was created): there is no prior content to restore, and restoring "an empty file"
        would be a different, wrong state. Undo of a create is not supported."""
    verb = rec.get("verb")
    if verb not in KNOWN_VERBS:
        raise LedgerError(f"unknown ledger verb {verb!r}: refusing to undo or verify a record this build "
                          f"does not understand (known: {', '.join(KNOWN_VERBS)}).")
    if (verb == "add_node" and rec.get("file_sha256_before") is None
            and str(rec.get("undo_evidence_missing") or "").startswith(CREATE_UNDO_PREFIX)):
        raise LedgerError("undo of a file creation is not supported: the record is a CREATE "
                          f"(seq {rec.get('ledger_seq')}, {rec.get('path_real')}); there is no prior content.")


def state_dir() -> Path:
    env = os.environ.get("FACTLATTICE_STATE_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state") / "factlattice"


EPHEMERAL_ENV = "FACTLATTICE_LEDGER_EPHEMERAL_ROOTS"
EPHEMERAL_SKIP = {"status": "skipped", "reason": "ledger-skipped-ephemeral"}   # a CODE: the target is under the OS temp dir
LIVE_DETAIL_NOTE = "live_detail is for this live reply only: NEVER persist it"


def describe(block: dict | None) -> str:
    """`(code): text` -- the one place the operator-facing form of a ledger outcome is built, so every route (the MCP adapter, the
    library writers) prints the WHY and not only the code."""
    b = block or {}
    detail = b.get("live_detail")
    return f"({b.get('reason')}): {detail}" if detail else f"({b.get('reason')})"


def live_block(status: str, code: str, exc_or_text: object) -> dict:
    """A ledger outcome whose `reason` is a fixed CODE. The exception class is `error_class` (when there is an exception), and the
    text (it can hold a path with any directory name) is `live_detail`, beside its NEVER-persist note."""
    out = {"status": status, "reason": code}
    if isinstance(exc_or_text, BaseException):
        out["error_class"] = type(exc_or_text).__name__
        try:
            text = str(exc_or_text)
        except Exception:                                    # noqa: BLE001
            text = "<unprintable>"
    else:
        text = str(exc_or_text)
    out["live_detail"], out["live_detail_note"] = text, LIVE_DETAIL_NOTE
    return out


def default_ephemeral_roots() -> list[str]:
    """The production roots, IGNORING the test override."""
    return [os.path.realpath(r) for r in (tempfile.gettempdir(), "/tmp", "/private/tmp")]


def ephemeral_roots() -> list[str]:
    """Realpaths of the roots under which a write is EPHEMERAL: the OS temp dir, /tmp and /private/tmp,
    or, when $FACTLATTICE_LEDGER_EPHEMERAL_ROOTS is SET (an os.pathsep list; set-but-empty = nothing is
    ephemeral), exactly those entries. The override exists for the test suite, whose tmp_path lives
    under the OS temp dir."""
    env = os.environ.get(EPHEMERAL_ENV)
    raw = [e for e in env.split(os.pathsep) if e] if env is not None else \
        [tempfile.gettempdir(), "/tmp", "/private/tmp"]
    return [os.path.realpath(os.path.expanduser(r)) for r in raw]


def is_ephemeral(real_path, roots: Optional[Iterable[str]] = None) -> bool:
    """True if the REALPATH of `real_path` lies under an ephemeral root (realpath on BOTH sides, so
    /var vs /private/var and /tmp vs /private/tmp spellings agree and a symlink is judged by where it
    points; commonpath, never a string prefix, so /tmpfoo is not under /tmp)."""
    p = os.path.realpath(os.path.expanduser(str(real_path)))
    return any(_inside(r, p) for r in (ephemeral_roots() if roots is None else roots))


def ledger_path(real_path, state_dir: Optional[Path] = None) -> Path:
    real = os.path.realpath(real_path)
    key = hashlib.sha256(real.encode("utf-8")).hexdigest()[:32]
    return Path(state_dir if state_dir is not None else globals()["state_dir"]()) / "ledger" / f"{key}.jsonl"


def validate_meta(*, proposer=None, approver=None, reason=None, via=None) -> Optional[str]:
    """The refusal reason (a string) for out-of-bounds metadata, else None. Checked BEFORE any write."""
    for name, val, cap in (("proposer", proposer, MAX_PRINCIPAL), ("approver", approver, MAX_PRINCIPAL),
                           ("reason", reason, MAX_REASON)):
        if val is not None and not isinstance(val, str):
            return f"`{name}` must be a string."
        if val is not None:
            try:
                val.encode("utf-8")
            except UnicodeEncodeError:
                return f"`{name}` does not encode to UTF-8 (a lone surrogate?)."
        if val is not None and len(val) > cap:
            return f"`{name}` is {len(val)} chars; the ledger bound is {cap}."
    if isinstance(via, str):
        try:
            via.encode("utf-8")
        except UnicodeEncodeError:
            return "`via` does not encode to UTF-8 (a lone surrogate?)."
    if via is not None and not (isinstance(via, str) and len(via) <= MAX_VIA
                                and (via == "direct" or (via.startswith("clerk:") and len(via) > 6))):
        return "`via` must be 'direct' or 'clerk:<queue_item_id>' (<= 200 chars)."
    return None


def _norm(p) -> str:
    return os.path.realpath(os.path.expanduser(str(p)))


def _inside(root: str, path: str) -> bool:
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:
        return False


def check_outside_roots(path: Path, roots: Iterable[str]) -> None:
    """[L8] Raise LedgerError if `path` (a would-be ledger path) lies inside any of `roots`."""
    p = _norm(path)
    for r in roots or ():
        if _inside(_norm(r), p):
            raise LedgerError(f"the ledger path {path} is INSIDE the registered corpus root {r}; a ledger "
                              f"must never live in a corpus (a file-sync service would sync it and the indexer could "
                              f"read it). Set FACTLATTICE_STATE_DIR outside every corpus root.")


def blob_dir(state_dir: Optional[Path] = None) -> Path:
    return Path(state_dir if state_dir is not None else globals()["state_dir"]()) / "ledger-blobs"


def store_blob(data: bytes, *, state_dir: Optional[Path] = None, roots: Optional[Iterable[str]] = None) -> str:
    """Store `data` content-addressed at <state_dir>/ledger-blobs/<sha256> (dir 0700, file 0600), written
    ONCE (an existing blob is kept: dedup). Refused inside a registered root ([L8]). Returns the sha256."""
    sha = hashlib.sha256(data).hexdigest()
    bd = blob_dir(state_dir)
    check_outside_roots(bd, roots or ())
    try:
        bd.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest = bd / sha
        if dest.exists():
            return sha
        tmp = bd / f".{sha}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, dest)                 # same bytes whoever wins the race
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        raise LedgerError(f"prior-content blob not stored: {type(e).__name__}: {e}") from None
    return sha


def _last_line(fd: int) -> Optional[bytes]:
    size = os.fstat(fd).st_size
    if size == 0:
        return None
    pos, buf = size, b""
    while pos > 0 and buf.count(b"\n") < 2:
        step = min(_TAIL_CHUNK, pos)
        pos -= step
        buf = os.pread(fd, step, pos) + buf
    if not buf.endswith(b"\n"):
        raise LedgerError("the ledger's last line is torn (no trailing newline); refusing to chain onto "
                          "it. A torn tail means a previous append was interrupted -- inspect the file.")
    body = buf[:-1]
    start = body.rfind(b"\n") + 1
    if start == 0 and pos > 0:
        raise LedgerError("the ledger's last line exceeds the tail window; cannot chain onto it.")
    return buf[start:]


def append(real_path, fields: dict, *, state_dir: Optional[Path] = None,
           roots: Optional[Iterable[str]] = None, prior_bytes: Optional[bytes] = None) -> dict:
    """Append ONE record. The CALLER MUST hold the corpus file's per-file lock (this does not lock):
    seq/prev_sha256 assume no concurrent appender. Without the lock, records can interleave and
    the chain breaks; the empty-file cleanup is size-checked so it never deletes another's record.

    `fields` supplies verb/node_id/key/delta/... (anything absent is null); `v`, `ts`,
    `ledger_seq`, `path_real` and `prev_sha256` are the writer's own. Returns
    `{"status": "ok", "seq": N, "file": <ledger path>}`; raises `LedgerError` on any failure.

    CHAIN BREAK: if the previous record's `file_sha256_after` != this record's `file_sha256_before`, the
    file changed outside the writers. The write is NOT blocked; the record carries
    `follows_out_of_band_edit: true` and the return gains `out_of_band: {since_seq, since_ts}`.
    `prior_bytes` (whole-file writes): stored as a durable blob, `prior_blob` = its sha256."""
    real = os.path.realpath(real_path)
    try:
        lp = ledger_path(real, state_dir)
        check_outside_roots(lp, roots or ())
        if _inside(_norm(Path(real).parent), _norm(lp)):
            raise LedgerError(f"the ledger path {lp} is inside the corpus file's own directory "
                              f"{Path(real).parent}; refusing.")
        lp.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        created = not lp.exists()
        fd = os.open(lp, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        wrote = False
        try:
            last = _last_line(fd)
            oob = None
            if last is None:
                seq, prev = 1, None
            else:
                try:
                    plast = json.loads(last)
                    seq = int(plast["ledger_seq"]) + 1
                except (ValueError, KeyError, TypeError):
                    raise LedgerError("the ledger's last line is not a valid record; refusing to chain "
                                      "onto it.") from None
                prev = hashlib.sha256(last).hexdigest()
                if plast.get("file_sha256_after") != fields.get("file_sha256_before"):
                    oob = {"since_seq": plast["ledger_seq"], "since_ts": plast.get("ts")}
            rec = {k: fields.get(k) for k in FIELDS}
            rec["follows_out_of_band_edit"] = oob is not None
            rec["prior_blob"] = None
            # a caller-DECLARED gap (a created file has no prior content) is kept as stated; a gap found
            # here (the blob could not be stored) is what turns the return into `ok_without_undo`
            rec["undo_evidence_missing"] = fields.get("undo_evidence_missing")
            blob_gap = None
            if prior_bytes is not None:
                try:
                    rec["prior_blob"] = store_blob(prior_bytes, state_dir=state_dir, roots=roots)
                except LedgerError as e:       # keep the record; the GAP lives in the chain forever
                    blob_gap = rec["undo_evidence_missing"] = str(e)
            rec.update(v=V, ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                       ledger_seq=seq, path_real=real, prev_sha256=prev)
            if rec["via"] is None:
                rec["via"] = "direct"
            line = (json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            n = os.write(fd, line)                           # ONE write() of the whole line (O_APPEND)
            if n != len(line):
                raise LedgerError(f"short ledger write ({n} of {len(line)} bytes); the tail is now torn.")
            wrote = True
            os.fsync(fd)
        finally:
            os.close(fd)
            if created and not wrote:           # never leave a ledger file we wrote no record into...
                try:                            # ...but only if it is STILL empty: a non-empty file holds
                    if os.path.getsize(lp) == 0:  # someone's record (a caller that broke the lock contract)
                        os.unlink(lp)
                except OSError:
                    pass
    except LedgerError:
        raise
    except OSError as e:
        raise LedgerError(f"{type(e).__name__}: {e}") from None
    out = {"status": "ok", "seq": seq, "file": str(lp)}
    if blob_gap:
        out.update(live_block("ok_without_undo", "undo-blob-not-stored", blob_gap))
    if oob is not None:
        out["out_of_band"] = oob
    return out


def read_lines(real_path, state_dir: Optional[Path] = None) -> Optional[list[bytes]]:
    """Every ledger line (bytes, newline kept) for a corpus file, or None if it has no ledger."""
    lp = ledger_path(real_path, state_dir)
    try:
        return lp.read_bytes().splitlines(keepends=True)
    except FileNotFoundError:
        return None


__all__ = ["V", "FIELDS", "KNOWN_VERBS", "check_undoable", "LedgerError", "state_dir", "ledger_path", "validate_meta",
           "check_outside_roots", "is_ephemeral", "ephemeral_roots", "EPHEMERAL_SKIP", "blob_dir", "store_blob", "append", "read_lines"]
