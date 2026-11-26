"""[NODE-CHANGE-LEDGER] v1 -- the append-only, hash-chained record of every APPLIED typed write.

One JSON line per applied write, one ledger file PER CORPUS FILE:

    <state_dir>/ledger/<sha256(realpath)[:32]>.jsonl        dir 0700, file 0600
    <state>/ledger/<sha256(name:rel)[:32]>.jsonl            under a named root that declares a state

`locate` is the ONE way to a ledger path ([LEDGER-LOCATE-ONE-FUNCTION], [MECH-NR-LEDGER]): where a write goes, which files hold
this file's history, and whether a write may happen at all are one answer, worked out from one table.

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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from . import rootview as _rv

V = 1
# the record's field order (the table in the spec); `append` fills the writer-owned ones
FIELDS = ("v", "ts", "ledger_seq", "path_real", "verb", "node_id", "key", "delta", "discarded",
          "node_hash_before", "node_hash_after", "file_sha256_before", "file_sha256_after",
          "proposer", "approver", "reason", "via", "corpora", "corpora_error", "engine_head",
          "backup_path", "inserted", "follows_out_of_band_edit", "prior_blob", "undo_evidence_missing", "named_root", "membership_partial", "prev_sha256")
# [LEDGER-VERB-VOCAB] the verbs a record may carry. A NEW verb is added here WITH a test, and undo/verify
# (`check_undoable`) refuse any verb not listed, loudly, instead of treating it as an ordinary edit.
KNOWN_VERBS = ("set_property", "append", "repair_property", "add_node", "write_document",
               "rename_property", "remove_property", "unread_time_guard_fault")
#: a verb that is NOT an edit: a note in the chain that a guard faulted while this file was written. Nothing to undo.
NOT_AN_EDIT_VERBS = ("unread_time_guard_fault",)
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
    if verb in NOT_AN_EDIT_VERBS:
        raise LedgerError(f"ledger record {rec.get('ledger_seq')} ({verb}) is a note that a guard faulted, not an edit: "
                          "there is nothing to undo or verify.")
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


def is_ephemeral(real_path, roots: Iterable[str] | None = None) -> bool:
    """True if the REALPATH of `real_path` lies under an ephemeral root (realpath on BOTH sides, so
    /var vs /private/var and /tmp vs /private/tmp spellings agree and a symlink is judged by where it
    points; commonpath, never a string prefix, so /tmpfoo is not under /tmp)."""
    p = os.path.realpath(os.path.expanduser(str(real_path)))
    return any(_inside(r, p) for r in (ephemeral_roots() if roots is None else roots))


# ---- [MECH-NR-LEDGER]: where history lives -------------------------------------------------------------------------------------
STATE_UNAVAILABLE = "state-unavailable"
STATE_INSIDE_CORPUS = "named-root-state-inside-corpus"
STATE_UNPROVABLE = "named-root-state-unprovable"     # a registry entry failed, so its root cannot be shown to lie outside the state
TABLE_MALFORMED = "named-roots-malformed"
LOCATE_CODES = (STATE_UNAVAILABLE, STATE_INSIDE_CORPUS, STATE_UNPROVABLE, TABLE_MALFORMED)
#: [MECH-BR-WRITES] x-inside-marked: the refusal of a write to a file under a root that is not answering. It is decided BEFORE the writer
#: starts (no lock, nothing read under the root), so it is not one of `locate`'s codes; it joins the writers' stable codes and has its fix text here.
ROOT_UNRESPONSIVE = _rv.CODE_ROOT_UNRESPONSIVE
#: a write whose file lives under a root that did not CONFIRM, within the root deadline, where that file really is. Retryable; nothing is written; the
#: root is not marked.
MEMBERSHIP_UNCONFIRMED = "membership-unconfirmed"
#: a write under the root of a corpus held pending for the registry's verdict, still held when the one-deadline wait ended. Retryable; nothing is written; no root is marked.
CORPUS_VERDICT_PENDING = _rv.CODE_CORPUS_VERDICT_PENDING
WRITE_REFUSAL_CODES = (ROOT_UNRESPONSIVE, MEMBERSHIP_UNCONFIRMED, CORPUS_VERDICT_PENDING)
#: `state-unavailable` can clear by itself when a drive returns; each of these is a fault in the configuration and stays until it is fixed.
CONFIGURATION_CODES = (STATE_INSIDE_CORPUS, STATE_UNPROVABLE, TABLE_MALFORMED)
#: what clears each refusal, in the words a refusal and `lts_history` use. No path and no name, so anything may repeat them. Pinned by a test.
_RESTART = "then restart every engine server, which keeps the registry it booted from"
_CHECK = "`python -m factlattice_engine registry resolved --file <registry>` prints the fault"
REFUSAL_FIX = {
    STATE_UNAVAILABLE: "To clear it: make this root's state location reachable (reconnect or wake the drive that holds it), then retry; no restart is needed. If it was never "
                       "begun: this engine cannot begin one, so remove `state` from the table and restart every engine server; if its marker names another root, correct the table. Only files under this root are affected, and this corpus keeps serving reads.",
    STATE_INSIDE_CORPUS: f"To clear it: move this root's `state` in the registry's named_roots table outside every corpus root and away from the file's own folder, {_RESTART}. "
                         "Only files under this root are affected, and this corpus keeps serving reads.",
    STATE_UNPROVABLE: f"To clear it: correct the failed entry in the registry ({_CHECK}), {_RESTART}. "
                      "Only roots that declare a state are affected; the failed entry's own corpus is not served, and others keep serving reads.",
    ROOT_UNRESPONSIVE: "To clear it: wait for the root to answer, or reconnect or wake the drive that holds it; no restart is needed, and the next write to this file finds it served. "
                       "Nothing was read from the root and nothing was written. Only files under a root that is not answering are affected; writes to every other file go on.",
    MEMBERSHIP_UNCONFIRMED: "To clear it: retry the write. A root that could hold this file did not confirm where the file lives within the root deadline (a busy or slow drive), "
                            "so the write cannot say which corpus it belongs to; no restart is needed. Nothing was written, the root is not marked, and writes to files under other roots go on.",
    CORPUS_VERDICT_PENDING: "To clear it: retry the write once " + _rv.VERDICT_PENDING_WHEN + "; no restart is needed for the retry. This corpus's registry entry is one the release refuses, so the retry then meets that mode's rules "
                            "(to serve again, fix the registry entry and restart). No root failed to answer. Nothing was written, and writes to files under other roots go on.",
    TABLE_MALFORMED: f"To clear it: correct the named_roots table in the registry ({_CHECK}), {_RESTART}. "
                     "Every write outside the temp dir is affected, on every corpus, until then; a corpus that uses a name is not served, and others keep serving reads.",
}


#: where one code has causes that clear differently, the words follow the cause. Each is tested against a run that really has that cause.
DETAIL_FIX = {
    "old-location-unknown": "To clear it: make the old location in this root's `was` readable (a drive mid-mount, or a permission on the path), then retry; if that spelling names something unrelated "
                            "on this machine, mark it `local` in the table and restart every engine server. No state location is involved. Only this file is affected; its history serves what could be read.",
    "ledger-unreadable": "To clear it: make this file's ledger under the machine state folder readable, then retry. Only this file is affected; its history serves what could be read.",
    "state-ledger-unreadable": "To clear it: make the root's state location readable (a drive mid-mount, or a permission on the path), then retry; no restart is needed. Only this file is affected.",
    "case-variant-of-a-state-root": "To clear it: write by the path as the table spells the root, or correct the table's spelling and restart every engine server. A case variant would take a different write lock "
                                    "from the same ledger. Only paths spelled this way are affected.",
}


def fix_for(code: str, detail: str | None = None) -> str:
    return DETAIL_FIX.get(detail or "") or REFUSAL_FIX[code]


#: one sentence per reason in `ledger_left_out`. No path, no name.
LEFT_OUT_NOTES = {
    "different-file-at-old-location": "A different file exists at an old spelling, so its ledger is that file's for now; it joins this history when that file is gone.",
    "old-location-unknown": "Whether a file exists at an old spelling could not be read, so that ledger is left out until it can.",
    "ledger-unreadable": "A ledger of this file could not be read, so it is left out until it can.",
    "old-spelling-belongs-to-another-file": "A ledger reached through a shorter old spelling belongs, by the table's longest match, to another file. Where spellings nest that rule decides, and it may be wrong about a folder that really was a subfolder.",
}
MARKER_NAME = ".factlattice-state"
NEW_CHAIN_REASON = ("this write begins a new chain: no ledger was found in this state location or through the table; history may "
                    "exist in another state folder or under a spelling the table does not hold; unknown, not absent.")


@dataclass(frozen=True)
class TableContext:
    """What one registry says, read once: the table, why it cannot be used, and every root the file declares, served or not."""
    table: dict[str, dict] | None = None
    problem: tuple[str, str] | None = None       # (code, detail): the table or the registry cannot be read, so no location can be proved
    declared: tuple[str, ...] = ()                  # realpaths of every root every entry declares
    unprovable: bool = False                        # an entry failed on a key or a name: where its root is cannot be known


@dataclass(frozen=True)
class Location:
    """`locate`'s answer. `write_file` is where the next record goes; `read_files` every file that counts as this file's history."""
    real: str
    base: Path                                      # the folder holding ledger/ and ledger-blobs/ for a write
    write_file: Path
    read_files: tuple[Path, ...]
    root: str | None = None
    rel: str | None = None
    state: str | None = None                     # the declared state location, absolute
    refusal: tuple[str, str] | None = None       # (code, detail): a write is refused; reads say so
    continues: Path | None = None                # the legacy ledger a first record under a state chains onto
    several: int = 0                                # how many counted ledgers a newly started one sits beside
    other_ledgers: int = 0                          # counted ledgers beside the one this write continues: history is split, and a receipt says so
    left_out: tuple[str, ...] = ()                  # fixed reasons, one per ledger left out; never a path
    ephemeral_state: bool = False                   # a state under the OS temp dir: no record, and said ([LEDGER-NO-EPHEMERAL-ROOTS])
    private_root: bool = False
    machine_base: Path | None = None             # the machine state folder, where the read side also looks for a blob
    guard_roots: tuple[str, ...] = ()               # [L8] every root the registry declares, served or not
    reads_serve: bool = False                       # a write is refused, but what could be read still counts: x-legacy, "left out with a note"

    @property
    def named_root(self) -> dict | None:
        return {"name": self.root, "rel": self.rel} if self.root else None


_PINNED: TableContext | None = None


_PIN_FINAL = True


def pin_table(context: TableContext, final: bool = True) -> None:
    """A long-running server reads the registry once, at boot, and keeps it ([INV-NR-ONE-TABLE]). A process that never pins reads
    the file each time it asks (measured for every short-lived engine process), so it is on a new registry when the file changes.
    `final=False` is the PROVISIONAL pin of a server whose declared roots are not all resolved yet (a root may hang): the table is the booted one,
    the declared real paths are the roots' lexical spellings until the final pin replaces it ([MECH-BR-BOOT])."""
    global _PINNED, _PIN_FINAL
    _PINNED = context
    _PIN_FINAL = True if context is None else bool(final)


def pin_is_final() -> bool:
    return _PINNED is not None and _PIN_FINAL


def unpin_table() -> None:
    pin_table(None)  # type: ignore[arg-type]


def _add_root(paths: list[str], value: str, resolve=None) -> bool:
    """A root that cannot be resolved (a NUL, a lone surrogate) is unknown, like a failed entry. It must not stop a boot or a write that today's engine serves."""
    try:
        paths.append((resolve or _root_real)(value))
        return False
    except (ValueError, OSError):
        return True


def _declared_roots(entries: dict, resolve=None) -> tuple[tuple[str, ...], bool]:
    from . import registry
    paths: list[str] = []
    unprovable = False
    for spec in (entries or {}).values():
        if not isinstance(spec, dict) or registry.named_failure(spec) is not None or registry.entry_key_problems(spec):
            unprovable = True             # never consulted: its root is not known, whatever it appears to say
            continue
        roots = spec.get("roots")
        if roots is not None and not isinstance(roots, list):
            unprovable = True             # a `roots` that is not a list: its location cannot be trusted, so it is never read as containing nothing
        for r in (roots if isinstance(roots, list) else []):
            v = r.get("path") if isinstance(r, dict) else r
            if not isinstance(r, (str, dict)) or (isinstance(r, dict) and "path" in r and not isinstance(r["path"], str)):
                unprovable = True         # neither a string nor a root object, or a non-string path: the same
            elif (isinstance(r, str) and not r.strip()) or (isinstance(r, dict) and isinstance(r.get("path"), str) and not r["path"].strip()):
                unprovable = True         # a blank path
            elif isinstance(r, dict) and "path" not in r and not any(k not in registry.REGISTRY_ROOT_KEYS and not registry.is_note_key(k) for k in r):
                unprovable = True         # no path and no unknown key (an empty root object): nothing could have been meant
            if isinstance(v, str) and v:
                unprovable = _add_root(paths, v, resolve) or unprovable
        r = spec.get("root")
        if r is not None and (not isinstance(r, str) or not r.strip()):
            unprovable = True
        if isinstance(r, str) and r:
            unprovable = _add_root(paths, r, resolve) or unprovable
    return tuple(dict.fromkeys(paths)), unprovable


def context_from_file(path: str) -> TableContext:
    """The table of one registry file, or `RegistryError`."""
    from . import registry
    read = registry.read_table(path)
    declared, unprovable = _declared_roots(registry.resolved_entries(path))
    problem = read["problem"]
    return TableContext(table=read["table"], problem=(TABLE_MALFORMED, problem.get("detail") or "malformed") if problem else None,
                        declared=declared, unprovable=unprovable)


def context_deferred(choice) -> Callable[[], TableContext]:
    """The ledger table of the registry a server BOOTED from, as a function that finishes it later. [INV-NR-ONE-TABLE] `choice` is the one lookup the
    server built its store from (its bytes are what `sha256_at_boot` names): the table, its problem and the corpus entries are taken from it, NOT from a
    second read of the file, so an edit made while a root is stalled is never the table that gets pinned. What is left to do, and is deferred, is the
    only disk work: resolving each declared root (`_declared_roots`), which a hung root can stop; the caller runs the returned function bounded."""
    entries, table, problem = choice.corpora, choice.named_roots, choice.table_problem

    def finish() -> TableContext:
        declared, unprovable = _declared_roots(entries)
        return TableContext(table=table, problem=(TABLE_MALFORMED, problem.get("detail") or "malformed") if problem else None,
                            declared=declared, unprovable=unprovable)
    return finish


def context_provisional(choice) -> TableContext:
    """The same table as `context_deferred(choice)()`, with the declared roots' LEXICAL spellings (a string operation: no disk) instead of their real
    paths. A server pins it at boot, so every table read before the final pin goes by the booted bytes too, never by the file as it is now."""
    def lexical_root(value):
        if "\x00" in value:                   # the final pin's realpath refuses a NUL, and so does this: the root is unknown in both pins
            raise ValueError("embedded null byte")
        return _rv.lexical(value)
    declared, unprovable = _declared_roots(choice.corpora, resolve=lexical_root)
    problem = choice.table_problem
    return TableContext(table=choice.named_roots, problem=(TABLE_MALFORMED, problem.get("detail") or "malformed") if problem else None,
                        declared=declared, unprovable=unprovable)


def table_context() -> TableContext:
    """The table this process goes by: the pinned one, or the registry the one lookup chooses now. A configured registry that cannot be read
    holds no table we can see, so a write goes under today's key, as a write with no registry always has: the older rule ([registry_unavailable],
    a receipt that survives a broken registry and says so), kept by the maintainers' ruling of 2026-11-03. Its worst case is a new ledger beside the history,
    said by `new_chain` and joined with one reported break when the table returns: visible and recoverable while no state root exists."""
    if _PINNED is not None:
        return _PINNED
    from . import registry
    try:
        choice = registry.resolve_registry()
        return context_from_file(choice.path) if choice.path and choice.source != "example" else TableContext()
    except registry.RegistryError:
        return TableContext()


def _machine_key(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:32]


def _machine_base(state_dir: Path | None) -> Path:
    return Path(state_dir if state_dir is not None else globals()["state_dir"]())


def _present(path: Path) -> bool | None:
    """True, False, or None for a stat that failed with anything but not-found: `Path.exists` would answer False, and a drive going away would read as no ledger."""
    try:
        os.stat(path)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None


def _root_real(path) -> str:
    """The real path of a REGISTERED ROOT, or of a root's spelling in the table. [MECH-BR-WRITES] x-membership: from the current call's
    reading of the roots (each root's last answered probe), never resolved here, so a root that does not answer cannot stop a write to
    another. With no reading installed (a library caller) it is `_norm`, as it always was. The written file never goes through this."""
    v = _rv.current()
    return v.real(path) if v is not None else _norm(path)


def _disk(key: str, fn: Callable[[], object], default: object) -> object:
    """One read under a STATE LOCATION or an OLD SPELLING (a stat, a marker): with the engine's bound when the call has installed one,
    and `default` if it did not answer in time. With none installed, `fn()` as it always was."""
    v = _rv.current()
    return v.bounded(key, fn, default) if v is not None else fn()


def _under_marked(path) -> bool:
    """Is `path` under a root the call's reading found not answering? A string comparison; nothing is read."""
    v = _rv.current()
    return v is not None and v.marked_holding(path)


def _named_key(name: str, rel: str) -> str:
    return hashlib.sha256(f"{name}:{rel}".encode()).hexdigest()[:32]


def _expand_spelling(spelling: str) -> str:
    home = os.path.expanduser("~")
    for form in ("~/", "$HOME/"):
        if spelling.startswith(form):
            spelling = os.path.join(home, spelling[len(form):])
            break
    return "/" + os.path.normpath(spelling).lstrip("/")      # POSIX normpath keeps two leading slashes


def _variant_rest(root: str, real: str) -> str | None:
    """On a case-insensitive volume a path may spell a root in another case, and realpath does not canonicalise case. `real` is under
    `root` when its leading components fold equal AND name the same folder by identity, so a case-sensitive volume is never fooled."""
    parts, root_parts = real.split(os.sep), root.split(os.sep)
    if len(parts) <= len(root_parts) or [x.casefold() for x in parts[:len(root_parts)]] != [x.casefold() for x in root_parts]:
        return None
    if _under_marked(root):
        return os.sep.join(parts[len(root_parts):])    # a root that does not answer: nothing is asked of it, and a spelling that folds equal is
                                                       # taken as a variant (fail closed: the write is refused, never quietly left outside the state)
    try:
        same = _disk(root, lambda: os.path.samefile(root, os.sep.join(parts[:len(root_parts)])), _NO_ANSWER)
        if same is _NO_ANSWER or same:               # a stat that does not answer is a variant too: fail closed
            return os.sep.join(parts[len(root_parts):])
    except (OSError, ValueError):
        pass
    return None


def _root_of(real: str, table: dict[str, dict]) -> tuple[str, str] | None:
    """`(name, rel)` of the root whose CURRENT path holds `real`, the longest where roots nest, and the first name in sorted order where
    two names reach one real path, so the file's order cannot pick. A root's path may itself pass through a link, so it is compared by its
    real path. A file at a `was` spelling that still exists is under no root. Case is not folded: see `_variant_of_state_root`."""
    best: tuple[int, str, str] | None = None
    for name in sorted(table):
        root = _root_real(table[name]["path"])
        if real != root and _inside(root, real) and (best is None or len(root) > best[0]):
            best = (len(root), name, os.path.relpath(real, root))
    return (best[1], best[2]) if best else None


def _variant_of_state_root(real: str, table: dict[str, dict]) -> bool:
    """True when a path under no root spells a state root in another case and names the same folder by identity. Treating it as under that
    root would share the ledger and not the per-file lock, which is keyed on the real path as spelled, and two writers would fork the chain;
    treating it as under no root would quietly leave the state. So it is refused."""
    for spec in table.values():
        if spec.get("state_path") is None:
            continue
        root = _root_real(spec["path"])
        if _variant_rest(root, real) is not None:
            return True
    return False


def _spellings(table: dict[str, dict]) -> list[tuple[str, str]]:
    """`(place, name)` for every root's current path and every `was` not marked local, normal in form: what x-match searches."""
    out: list[tuple[str, str]] = []
    for name in sorted(table):
        spec = table[name]
        local = {_expand_spelling(x) for x in spec.get("local", ())}
        out.append((os.path.normpath(spec["path"]), name))
        out += [(_expand_spelling(w), name) for w in spec.get("was", ()) if _expand_spelling(w) not in local]
    return out


def owner_of_old_path(old: str, table: dict[str, dict]) -> str | None:
    """Where the table says a path written at an old spelling is now, by the resolver's own rule ([MECH-NR-RESOLVE] x-match): the longest
    spelling among every root's path and `was`. One old path has ONE answer, so one ledger can never be claimed by two current files,
    across spellings that nest or across roots. None when no spelling holds it."""
    best: tuple[int, str, str] | None = None
    for place, name in _spellings(table):
        if (old == place or _inside(place, old)) and (best is None or len(place) > best[0]):
            best = (len(place), place, name)
    if best is None:
        return None
    rest = os.path.relpath(old, best[1])
    root = _root_real(table[best[2]]["path"])
    return root if rest == "." else os.path.normpath(os.path.join(root, rest))


def _old_paths(real: str, table: dict[str, dict]) -> list[str]:
    """Where this file may have been, literally: through each `was` not marked local of EVERY root whose current path holds it, the outer
    roots of a nest too, since the owner of an old path may be a file under an inner root with no `was` of its own. Not a spelling that names the
    same place as a local one on this machine ([GRAM-NR-TABLE] x-local). A ledger key is the literal real path at write time, so nothing here is
    realpath-ed: a link left at the old location must still find the ledger written there. `owner_of_old_path` then decides whose each one is."""
    out: list[str] = []
    for name in sorted(table):
        spec = table[name]
        root = _root_real(spec["path"])
        if real == root or not _inside(root, real):
            continue
        rel = os.path.relpath(real, root)
        local = {_expand_spelling(x) for x in spec.get("local", ())}
        for was in spec.get("was", ()):
            if was in spec.get("local", ()):
                continue
            old = _expand_spelling(was)
            if old not in local:
                out.append(os.path.normpath(os.path.join(old, rel)))
    return list(dict.fromkeys(out))


_NO_ANSWER = object()


def _same_place(a: str, b: str) -> bool | None:
    """True or False when the two paths answered; None when the stat did not answer in time. A stat that did not answer is unknown, never "another
    file" ([INV-BR-FAIL-CLOSED-KEPT]); the caller refuses on it."""
    try:
        got = _disk(a, lambda: os.path.samefile(a, b), _NO_ANSWER)
    except (FileNotFoundError, NotADirectoryError):
        return False                              # an answered "no such path": not the same file
    except (OSError, ValueError):
        return None                               # EIO, ETIMEDOUT, ESTALE, EACCES, a bad path: the disk did not tell us (as `_present`)
    return None if got is _NO_ANSWER else bool(got)


def _identity(old: str, real: str) -> str:
    """'same' | 'absent' | 'different' | 'unknown', for a file at an old spelling against the current one: equal real paths first,
    then st_dev and st_ino. A stat that fails with anything but not-found means unknown."""
    try:
        if os.path.realpath(old) == real:
            return "same"
        st = os.stat(old)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return "absent"
    except OSError:
        return "unknown"
    try:
        cur = os.stat(real)
    except (FileNotFoundError, NotADirectoryError):
        return "different"
    except OSError:
        return "unknown"
    return "same" if (st.st_dev, st.st_ino) == (cur.st_dev, cur.st_ino) else "different"


def _legacy(real: str, machine: Path, table: dict[str, dict]) -> tuple[list[Path], list[str], bool]:
    """`(counted, left_out, unknown)`: the ledgers in the machine state folder that count as this file's history through the table,
    beside the one under today's key. A `was` may name the real path of the current location (x-was asks for the real path of an earlier
    one, and a root's path can pass through a link), and then its key IS today's key: one ledger is counted once."""
    counted: list[Path] = []
    left_out: list[str] = []
    unknown = False
    here = _machine_key(real)
    for old in _old_paths(real, table):
        if _machine_key(old) == here:
            continue
        lp = machine / "ledger" / f"{_machine_key(old)}.jsonl"
        try:
            os.stat(lp)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            unknown = True
            left_out.append("ledger-unreadable")
            continue
        owner = owner_of_old_path(old, table)
        if _under_marked(old) or (owner is not None and owner != real and _under_marked(owner)):
            # [MECH-BR-WRITES] x-healthy-file: deciding whose this ledger is needs a root that is not answering, so it cannot be decided,
            # and history must be provable: unknown, with the same code as a stat that fails with anything but not-found. Nothing is read.
            unknown = True
            left_out.append("old-location-unknown")
            continue
        if owner is not None and owner != real:
            same = _same_place(owner, real)
            if same is None:                      # the stat did not answer: history cannot be proved, the same refusal as an unreadable old location
                unknown = True
                left_out.append("old-location-unknown")
                continue
            if not same:
                left_out.append("old-spelling-belongs-to-another-file")
                continue
        verdict = _disk(old, lambda: _identity(old, real), "unknown")
        if verdict == "unknown":
            unknown = True
            left_out.append("old-location-unknown")
        elif verdict == "different":
            left_out.append("different-file-at-old-location")
        else:
            counted.append(lp)
    return counted, left_out, unknown


def _folded(path: str) -> str:
    return os.path.normcase(_norm(path)).casefold()


def _folded_root(path: str) -> str:
    return os.path.normcase(_root_real(path)).casefold()


def _state_inside(state: str, real: str, ctx: TableContext) -> bool:
    """[L8] for a state location: folded on purpose, so it errs towards refusal on a case-sensitive volume ([MECH-NR-RESOLVE] x-canonical)."""
    s = os.path.normcase(_disk(state, lambda: _norm(state), os.path.normpath(state))).casefold()
    return _inside(_folded(str(Path(real).parent)), s) or any(_inside(_folded_root(r), s) for r in ctx.declared)


def read_marker(state: str) -> str | None:
    """The root name in a state location's marker, or None for anything else: absent, unreadable or not one of ours."""
    try:
        with open(os.path.join(state, MARKER_NAME), "rb") as fh:
            data = json.loads(fh.read(4096).decode("utf-8"))
        name = data.get("root") if isinstance(data, dict) else None
        return name if isinstance(name, str) else None
    except (OSError, ValueError):
        return None


def locate(real_path, *, state_dir: Path | None = None, context: TableContext | None = None) -> Location:
    """The ONE way to a ledger path. Stats and reads the marker; writes nothing. `refusal` set means no write may start from this
    answer, and a reader says so: nothing else in this module or outside it decides that."""
    real = os.path.realpath(real_path)
    machine = _machine_base(state_dir)
    here = machine / "ledger" / f"{_machine_key(real)}.jsonl"
    ctx = context if context is not None else table_context()
    plain = Location(real=real, base=machine, write_file=here, read_files=(here,) if here.exists() else (), machine_base=machine,
                     guard_roots=ctx.declared)
    if ctx.problem is not None:
        return Location(real=real, base=machine, write_file=here, read_files=(here,) if here.exists() else (), machine_base=machine,
                        refusal=ctx.problem, reads_serve=True)        # today's key still answers a read; only a write needs the table
    if not ctx.table:
        return plain
    hit = _root_of(real, ctx.table)
    if hit is None:
        if _variant_of_state_root(real, ctx.table):
            return Location(real=real, base=machine, write_file=here, read_files=plain.read_files, machine_base=machine, guard_roots=ctx.declared,
                            refusal=(STATE_UNAVAILABLE, "case-variant-of-a-state-root"), reads_serve=True)
        return plain
    name, rel = hit
    spec = ctx.table[name]
    common = dict(real=real, root=name, rel=rel, private_root=bool(spec.get("private")), machine_base=machine, guard_roots=ctx.declared)
    counted, left_out, unknown = _legacy(real, machine, ctx.table)
    state = spec.get("state_path")
    if state is None:
        have_current = here.exists()
        reads = ((here,) if have_current else ()) + tuple(counted)
        refusal = (STATE_UNAVAILABLE, "old-location-unknown") if unknown else None
        if have_current or len(counted) != 1:
            return Location(base=machine, write_file=here, read_files=reads, refusal=refusal, left_out=tuple(left_out), reads_serve=unknown,
                            several=len(counted) if not have_current and len(counted) > 1 else 0,
                            other_ledgers=len(counted) if have_current else 0, **common)
        return Location(base=machine, write_file=counted[0], read_files=reads, refusal=refusal, left_out=tuple(left_out), reads_serve=unknown, **common)
    target = Path(state) / "ledger" / f"{_named_key(name, rel)}.jsonl"
    # today's key for the current real path is one more legacy ledger
    if here.exists():
        counted = [here] + counted
    if _disk(state, lambda: is_ephemeral(state), False):
        return Location(base=Path(state), write_file=target, read_files=(), state=state, ephemeral_state=True, **common)
    refusal = None
    if _state_inside(state, real, ctx):             # first: [GRAM-NR-TABLE] x-refusals, a location failing both is reported as this
        refusal = (STATE_INSIDE_CORPUS, "state-inside-corpus-root")
    elif ctx.unprovable:
        refusal = (STATE_UNPROVABLE, "registered-roots-unknown")
    elif _disk(state, lambda: read_marker(state), None) != name:
        refusal = (STATE_UNAVAILABLE, "no-marker-for-this-root")
    elif unknown:
        refusal = (STATE_UNAVAILABLE, "old-location-unknown")
    has_target = _disk(state, lambda: _present(target), None) if refusal is None else None
    if refusal is None and has_target is None:
        refusal = (STATE_UNAVAILABLE, "state-ledger-unreadable")
    if refusal is not None and refusal[1] == "old-location-unknown":   # the state is sound: what could be read still counts, and only the write waits
        has_target = _disk(state, lambda: _present(target), None)
        reads = ((target,) if has_target else ()) + tuple(counted)
        return Location(base=Path(state), write_file=target, read_files=reads, state=state, refusal=refusal, left_out=tuple(left_out), reads_serve=True, **common)
    if refusal is not None:
        return Location(base=Path(state), write_file=target, read_files=(), state=state, refusal=refusal, left_out=tuple(left_out), **common)
    reads = ((target,) if has_target else ()) + tuple(counted)
    return Location(base=Path(state), write_file=target, read_files=reads, state=state, left_out=tuple(left_out),
                    continues=counted[0] if not has_target and len(counted) == 1 else None,
                    several=len(counted) if not has_target and len(counted) > 1 else 0, **common)


def ledger_path(real_path, state_dir: Path | None = None) -> Path:
    """Where the next record for this file goes, by `locate`."""
    return locate(real_path, state_dir=state_dir).write_file


def validate_meta(*, proposer=None, approver=None, reason=None, via=None) -> str | None:
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
        try:
            real = _root_real(r)
        except (ValueError, OSError):
            # a registered root that cannot be resolved (a NUL, a lone surrogate) is UNKNOWN: the ledger path cannot be proved outside it, so this
            # refuses cleanly (a LedgerError the writers already handle) instead of letting a bare ValueError out
            raise LedgerError("a registered corpus root cannot be resolved, so the ledger path cannot be proved to lie outside it; "
                              "the ledger refuses (fix the registry entry).") from None
        if _inside(real, p):
            raise LedgerError(f"the ledger path {path} is INSIDE the registered corpus root {r}; a ledger "
                              f"must never live in a corpus (a file-sync service would sync it and the indexer could "
                              f"read it). Set FACTLATTICE_STATE_DIR outside every corpus root.")


def blob_dir(state_dir: Path | None = None) -> Path:
    return Path(state_dir if state_dir is not None else globals()["state_dir"]()) / "ledger-blobs"


def store_blob(data: bytes, *, state_dir: Path | None = None, roots: Iterable[str] | None = None) -> str:
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


def read_blob(sha: str, location: Location) -> bytes | None:
    """Prior content by digest: the state location first, then the machine state folder ([MECH-NR-LEDGER] x-blobs). None when neither has it."""
    if location.machine_base is None:
        bases = [location.base]
    else:
        bases = list(dict.fromkeys([location.base, location.machine_base]))
    for base in bases:
        try:
            return (blob_dir(base) / sha).read_bytes()
        except OSError:
            continue
    return None


def _last_line(fd: int) -> bytes | None:
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


def append(real_path, fields: dict, *, state_dir: Path | None = None,
           roots: Iterable[str] | None = None, prior_bytes: bytes | None = None,
           location: Location | None = None) -> dict:
    """Append ONE record. The CALLER MUST hold the corpus file's per-file lock (this does not lock):
    seq/prev_sha256 assume no concurrent appender. Without the lock, records can interleave and
    the chain breaks; the empty-file cleanup is size-checked so it never deletes another's record.

    `fields` supplies verb/node_id/key/delta/... (anything absent is null); `v`, `ts`,
    `ledger_seq`, `path_real` and `prev_sha256` are the writer's own. Returns
    `{"status": "ok", "seq": N, "file": <ledger path>}`; raises `LedgerError` on any failure.

    CHAIN BREAK: if the previous record's `file_sha256_after` != this record's `file_sha256_before`, the
    file changed outside the writers. The write is NOT blocked; the record carries
    `follows_out_of_band_edit: true` and the return gains `out_of_band: {since_seq, since_ts}`.
    `prior_bytes` (whole-file writes): stored as a durable blob, `prior_blob` = its sha256.

    `location`: `locate`'s answer from inside the same lock, so one write acts on one reading of the table. Without it, `append`
    asks for itself. A `refusal` raises `LedgerError`: the writers refuse earlier, before anything is written."""
    real = os.path.realpath(real_path)
    try:
        loc = location if location is not None else locate(real, state_dir=state_dir)
        if loc.refusal is not None:
            raise LedgerError(f"{loc.refusal[0]} ({loc.refusal[1]})")
        lp = loc.write_file
        guard = list(roots or ()) + list(loc.guard_roots)
        check_outside_roots(lp, guard)
        if _inside(_norm(Path(real).parent), _norm(lp)):
            raise LedgerError(f"the ledger path {lp} is inside the corpus file's own directory "
                              f"{Path(real).parent}; refusing.")
        if loc.state is not None:
            # the corpus write has landed by now, and the check was before it: a drive gone in that window must not start history at a bare mount point
            if _disk(loc.state, lambda: read_marker(loc.state), None) != loc.root:
                raise LedgerError(f"{STATE_UNAVAILABLE} (state-went-away-during-the-write)")
            lp.parent.mkdir(exist_ok=True, mode=0o700)
        else:
            lp.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        created = not lp.exists()
        fd = os.open(lp, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        wrote = False
        try:
            last = _last_line(fd)
            new_chain = False
            if last is None and loc.continues is not None:
                cfd = os.open(loc.continues, os.O_RDONLY)
                try:
                    last = _last_line(cfd)
                finally:
                    os.close(cfd)
            oob = None
            if last is None:
                seq, prev = 1, None
                new_chain = True
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
                    rec["prior_blob"] = store_blob(prior_bytes, state_dir=loc.base, roots=guard)
                except LedgerError as e:       # keep the record; the GAP lives in the chain forever
                    blob_gap = rec["undo_evidence_missing"] = str(e)
            rec.update(v=V, ts=datetime.now(UTC).isoformat(timespec="milliseconds"),
                       ledger_seq=seq, path_real=real, prev_sha256=prev)
            if rec["via"] is None:
                rec["via"] = "direct"
            if loc.named_root:           # only where a root applies: a record under no root is what it always was
                rec["named_root"] = loc.named_root
            else:
                del rec["named_root"]
            v = _rv.current()
            if v is not None and v.partial:    # [MECH-BR-WRITES] x-healthy-file: a root is marked, so `corpora` may be incomplete (a candidate that went unconfirmed refuses the write, so on a record it is 0)
                rec["membership_partial"] = v.partial_fields()
            else:
                del rec["membership_partial"]
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
    if new_chain:
        out.update(new_chain=True, new_chain_reason=NEW_CHAIN_REASON)
    if loc.several:
        out["several_ledgers"] = loc.several
    if loc.other_ledgers:
        out["other_ledgers"] = loc.other_ledgers
    if blob_gap:
        out.update(live_block("ok_without_undo", "undo-blob-not-stored", blob_gap))
    if oob is not None:
        out["out_of_band"] = oob
    return out


def read_sources(real_path, *, state_dir: Path | None = None, location: Location | None = None) -> tuple[Location, list[tuple[Path, list[bytes]]]]:
    """`(location, [(file, lines)])`: every ledger that counts as this file's history, read whole. A refused location reads nothing."""
    loc = location if location is not None else locate(real_path, state_dir=state_dir)
    out: list[tuple[Path, list[bytes]]] = []
    if loc.refusal is None or loc.reads_serve:
        unreadable = 0
        for f in loc.read_files:
            try:
                out.append((f, f.read_bytes().splitlines(keepends=True)))
            except FileNotFoundError:
                continue
            except OSError:                       # a directory there, or a permission: left out, and said, never raised at a reader
                unreadable += 1
        if unreadable:
            loc = replace(loc, left_out=loc.left_out + ("ledger-unreadable",) * unreadable)
    return loc, out


def parse_record(line: bytes) -> dict | None:
    """One ledger line as a record, or None for anything else: bad JSON, JSON that is not an object, or nesting too deep to parse."""
    try:
        rec = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return rec if isinstance(rec, dict) else None


def _ts_of(line: bytes) -> str:
    ts = (parse_record(line) or {}).get("ts")
    return ts if isinstance(ts, str) else ""


def merge_lines(sources: list[tuple[Path, list[bytes]]]) -> list[bytes]:
    """The union in ts order; one source is returned as it stands, unsorted. A line with no readable ts stays with the line before it.
    A record is hash-chained, so an identical line is one record."""
    if len(sources) == 1:
        return list(sources[0][1])
    keyed, seen = [], set()
    for i, (_f, lines) in enumerate(sources):
        last = ""
        for n, line in enumerate(lines):
            last = _ts_of(line) or last
            if line not in seen:
                seen.add(line)
                keyed.append((last, i, n, line))
    return [k[3] for k in sorted(keyed, key=lambda k: k[:3])]


def read_lines(real_path, state_dir: Path | None = None) -> list[bytes] | None:
    """Every ledger line (bytes, newline kept) for a corpus file through `locate`, in ts order, or None if nothing counts."""
    _loc, sources = read_sources(real_path, state_dir=state_dir)
    return merge_lines(sources) if sources else None


__all__ = ["STATE_UNPROVABLE", "TABLE_MALFORMED", "CONFIGURATION_CODES", "locate", "owner_of_old_path", "fix_for", "DETAIL_FIX", "LEFT_OUT_NOTES", "parse_record", "Location", "TableContext", "pin_table", "unpin_table", "table_context", "context_from_file", "read_marker",
           "read_sources", "merge_lines", "read_blob", "MARKER_NAME", "STATE_UNAVAILABLE", "STATE_INSIDE_CORPUS", "LOCATE_CODES", "NEW_CHAIN_REASON",
           "ROOT_UNRESPONSIVE", "CORPUS_VERDICT_PENDING", "WRITE_REFUSAL_CODES", "REFUSAL_FIX",
           "V", "FIELDS", "KNOWN_VERBS", "check_undoable", "LedgerError", "state_dir", "ledger_path", "validate_meta",
           "check_outside_roots", "is_ephemeral", "ephemeral_roots", "EPHEMERAL_SKIP", "blob_dir", "store_blob", "append", "read_lines"]
