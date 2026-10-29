"""[LEC-FRESHNESS] — the INCREMENTAL, event-driven freshness kernel.

============================================================================
The problem this solves ([SPEC:LTS-ENGINE-CONSOLIDATION] [LEC-FRESHNESS], the #1
perf finding): the base engine re-hashes the WHOLE corpus on EVERY query
(`Partition.freshness()` -> `combined_files_hash(root_paths)` -> `files_hash()` over
every `.lts`). On a long-running MCP stdio server that is an O(corpus-bytes) I/O tax
on every single call.

This kernel maintains freshness INCREMENTALLY so a query does ~O(0) validation while
KEEPING the content hash as the sole correctness oracle:

  * content-hash LEAVES         — the mtime-immune per-file content hash (`file_content_hash`)
                                  stays the oracle; a leaf hash NEVER derives from mtime/size.
  * an incrementally-maintained ROOT — on a change event only the affected leaf/leaves are
                                  re-hashed and the root recombined (Merkle-style). Unchanged
                                  files are NOT re-read. `Partition.freshness()` becomes an
                                  O(1) compare of the maintained root against the index hash.
  * SEAT on startup/reconnect   — one full re-hash establishes the leaf maps + root.
  * a filesystem WATCH          — a dependency-light STDLIB polling watcher (mtime+size are a
                                  HINT for WHICH leaves to re-check; the content hash is the
                                  verdict). No third-party dep (the library is zero-third-party
                                  by design; `watchdog` is NOT a declared dependency).
  * a periodic full RE-VERIFY   — the bounded backstop: `full_reverify()` re-hashes every leaf
                                  unconditionally so a MISSED or mtime-invisible change (e.g. a
                                  content-preserving-mtime edit) can NEVER leave the server
                                  silently serving stale.

MTIME IS A HINT, CONTENT IS THE ORACLE. `poll_once()` uses (mtime,size) only to pick which
leaves to re-hash; it then re-hashes their CONTENT and only flips the root if the content
hash actually changed (so a `touch` with identical bytes yields NO false-stale). The only
change class the hint can miss — identical (mtime,size) but different bytes — is closed by
`full_reverify()`, the periodic backstop.
============================================================================
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from factlattice import CorpusFileError, file_content_hash, list_lts_files
from factlattice.hashing import CORPUS_FILE_UNREADABLE


# ---------------------------------------------------------------------------
# Leaf recombine — reproduce `factlattice.hashing.files_hash` +
# `store.combined_files_hash` EXACTLY, but from an in-memory leaf map WITHOUT
# re-reading unchanged files. Parity with those two functions is load-bearing:
# the maintained root is compared against the index's stored `files_hash`, so it
# must be byte-identical to what the builder stamped. (tests assert this parity.)
# ---------------------------------------------------------------------------
def combine_leaf_map(fmap: Dict[str, str]) -> str:
    """SHA-256 over the sorted, newline-joined 'relpath\\x00filehash' records.

    Byte-identical to `factlattice.hashing.files_hash(root)[0]` for the same
    {relpath: file_content_hash} map — but computed from the cached map, so
    unchanged files are never re-read.
    """
    joined = "\n".join(f"{rel}\x00{fmap[rel]}" for rel in sorted(fmap))
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


def combine_roots(per_root: Dict[str, str]) -> str:
    """Combine per-root hashes into the partition root.

    Byte-identical to `store.combined_files_hash(root_paths)`: a single root is
    returned as-is (degenerate case); multiple roots are SHA-256 over the sorted,
    newline-joined 'root\\x00roothash' records.
    """
    if len(per_root) == 1:
        return next(iter(per_root.values()))
    parts = [f"{root}\x00{per_root[root]}" for root in sorted(per_root)]
    return "sha256:" + hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
@dataclass
class RootState:
    """Maintained state for ONE corpus root: the content-hash leaves + the mtime/size
    HINT snapshot used to decide which leaves to re-check. `hints` is NEVER consulted
    as a freshness verdict — only to narrow the set of files re-hashed by the oracle."""
    root_path: str
    leaves: Dict[str, str] = field(default_factory=dict)          # rel -> content hash
    hints: Dict[str, Tuple[float, int, int]] = field(default_factory=dict)  # rel -> (mtime, size, mode)
    # (code, rel, hint-of-the-bad-member) while a member is a corpus-file-* failure; leaves are EMPTY then, so the root diverges (STALE)
    fault: Optional[Tuple[str, str, Tuple[float, int, int]]] = None

    def root_hash(self) -> str:
        return combine_leaf_map(self.leaves)


class FreshnessKernel:
    """Per-store incremental freshness. Seat once, then maintain via poll/backstop.

    Instrumentation: `hash_ops` counts every per-file CONTENT hash performed. The O(0)
    proof asserts this counter does NOT advance on the query path when the corpus is
    fresh (a query reads the cached root, never a file).
    """

    def __init__(self, *,
                 file_hasher: Callable[[str], str] = file_content_hash,
                 lister: Callable[[str], List[str]] = list_lts_files,
                 poll_interval: float = 1.0,
                 backstop_every: int = 30) -> None:
        self._file_hasher = file_hasher
        self._lister = lister
        self.poll_interval = poll_interval
        # After `backstop_every` fast polls, one full re-verify runs (the missed-event
        # backstop). Bounds the worst-case stale window for a hint-invisible change.
        self.backstop_every = max(1, backstop_every)
        self._states: Dict[str, Dict[str, RootState]] = {}   # corpus_id -> {root_path: RootState}
        self._roots: Dict[str, str] = {}                     # corpus_id -> maintained root
        self._lock = threading.RLock()
        self._ticks = 0
        self.hash_ops = 0                                     # proof instrument
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._parts: Dict[str, "object"] = {}                # corpus_id -> Partition (weak use)

    # --- hashing (the ONLY place a file's content is read) --------------------
    def _hash_file(self, abs_path: str, rel: Optional[str] = None) -> str:
        self.hash_ops += 1
        try:
            return self._file_hasher(abs_path)
        except CorpusFileError as exc:                    # a refusal names the ROOT-RELATIVE path, never the absolute one
            if rel is not None:
                exc.set_rel(rel)
            raise
        except OSError:
            # [Opus Diamond P2-2] any other I/O error at hash time (EIO, a vanished file) is coded, so every caller of the kernel
            # (poll, full re-verify, the self-heal) fails CLOSED with a precise code instead of leaking a raw error
            raise CorpusFileError(CORPUS_FILE_UNREADABLE, rel if rel is not None else os.path.basename(abs_path)) from None

    def _hash_stable(self, abs_path: str, rel: str):
        """(content hash, stat hint) of a member, the hint taken BEFORE the hash and RE-CHECKED after it. [Opus Diamond P2-A] A
        write landing between the hash and a hint taken afterwards would be recorded as 'unchanged' and served old-as-fresh until
        the backstop; with the hint taken first it is detected by the next poll, and here the member is simply re-hashed. A member
        that never settles (three moves in a row) is UNVERIFIABLE: it raises a coded corpus-file-unreadable, so the root fails
        closed (STALE) instead of recording a hash that may not match the bytes."""
        for _ in range(3):
            before = self._safe_hint(abs_path)
            h = self._hash_file(abs_path, rel)
            if self._safe_hint(abs_path) == before:
                return h, before
        raise CorpusFileError(CORPUS_FILE_UNREADABLE, rel)

    @staticmethod
    def _stat_hint(abs_path: str) -> Tuple[float, int, int]:
        st = os.stat(abs_path)
        # the permission bits ride in the hint: a `chmod` changes neither mtime nor size, and an unreadable member must be
        # noticed by the fast poll (not only by the periodic backstop) -- [ISSUE-NONREGULAR-MEMBER] F2
        return (st.st_mtime, st.st_size, st.st_mode)

    # --- seating (startup / reconnect) ----------------------------------------
    def seat_partition(self, part) -> Optional[str]:
        """One full re-hash to establish the leaf maps + root for a partition.

        A partition with no `root_paths` (e.g. a synthetic in-memory index) is NOT
        seated (returns None); its `freshness()` falls back to the fail-closed path.
        """
        if not getattr(part, "root_paths", None):
            return None
        with self._lock:
            was_seated = part.corpus_id in self._states          # a RE-seat (the heal's structural change) keeps its old state on failure
            part.attach_kernel(self)
            self._parts[part.corpus_id] = part
            states: Dict[str, RootState] = {}
            try:
                for root_path in part.root_paths:
                    st = RootState(root_path=root_path)
                    self._reload_root_full(st)
                    states[root_path] = st
            except OSError as exc:
                # [Opus Diamond P1-A] a member that never settles, or turns unreadable, WHILE the corpus is being seated: leave
                # NOTHING half-seated (no kernel binding, no state) and raise ONE coded error. The caller contains it per
                # corpus (`seat_store` marks that corpus failed; `register_corpus` rolls the registration back).
                if not was_seated:
                    self._parts.pop(part.corpus_id, None)
                    part.attach_kernel(None)
                if isinstance(exc, CorpusFileError):
                    raise
                raise CorpusFileError(CORPUS_FILE_UNREADABLE, ".") from None
            self._states[part.corpus_id] = states
            self._roots[part.corpus_id] = combine_roots(
                {rp: st.root_hash() for rp, st in states.items()})
            return self._roots[part.corpus_id]

    def seat_store(self, store) -> None:
        """Seat every partition in a store and attach the kernel to each. [Opus Diamond P1-A] A corpus that cannot be seated
        (`CorpusFileError`) is REMOVED from the store and recorded in `store.failed_corpora` with its code: one corpus fails, the
        server and the other corpora come up."""
        from .privacy import is_private_partition
        for part in list(store.partitions()):
            try:
                self.seat_partition(part)
            except CorpusFileError as exc:
                store.remove_partition(part.corpus_id)
                store.failed_corpora[part.corpus_id] = {"code": exc.code, "path": exc.rel_path,
                                                        "private": is_private_partition(part)}

    # --- unseating (runtime unregister, [REG-RUNTIME-CORPUS]) -----------------
    def unseat_partition(self, corpus_id: str) -> bool:
        """Drop a corpus's maintained leaves/root/watch-membership — the INVERSE of
        `seat_partition`. Returns True iff the corpus was tracked (a no-op unseat of an
        unseated/unknown id returns False and never raises).

        After this the kernel no longer polls or backstops this corpus_id (`tick()`
        iterates `_parts`, which no longer carries it), and `current_root()` reverts to
        returning None for it — exactly the "never seated" state, so a caller cannot
        tell an unseat apart from a corpus that was never registered, which is the point:
        there must be no residual half-tracked state for a runtime-unregistered corpus.
        """
        with self._lock:
            tracked = corpus_id in self._states or corpus_id in self._roots \
                or corpus_id in self._parts
            self._states.pop(corpus_id, None)
            self._roots.pop(corpus_id, None)
            self._parts.pop(corpus_id, None)
            return tracked

    def _reload_root_full(self, st: RootState) -> None:
        """Re-hash EVERY leaf under a root unconditionally (content oracle)."""
        leaves: Dict[str, str] = {}
        hints: Dict[str, Tuple[float, int, int]] = {}
        for rel in self._lister(st.root_path):
            ap = os.path.join(st.root_path, rel)
            leaves[rel], hints[rel] = self._hash_stable(ap, rel)       # hint BEFORE the hash, re-checked after (P2-A)
        st.leaves = leaves
        st.hints = hints
        st.fault = None                                  # a full re-hash SUCCEEDED: any earlier fault is obsolete

    # --- the O(1) query-path read (NO hashing) --------------------------------
    def current_root(self, part) -> Optional[str]:
        """The maintained root for a partition, or None if unseated. O(1), no I/O.

        DELIBERATELY LOCK-FREE: this is THE query-path read, so it must not contend with
        the watcher thread (which holds `_lock` while re-hashing). A single dict `.get`
        is atomic under the GIL and `_recompute_root` REBINDS the entry in one statement,
        so a concurrent reader sees either the previous root or the new one — never a
        torn value. Both are self-consistent content hashes, so the freshness verdict is
        correct either way; a change observed a few microseconds later is exactly the
        bounded-latency behaviour the watcher already implies.
        """
        return self._roots.get(getattr(part, "corpus_id", None))

    # --- [LEC-SELF-HEAL] the leaf map, as the re-index's change oracle ---------
    def leaves_snapshot(self, part) -> Optional[Dict[str, Dict[str, str]]]:
        """{root_path: {rel: content_hash}} for a seated partition, or None.

        This is the SAME content-hash leaf map the freshness verdict is derived from, so
        the self-heal re-index and the freshness gate can never disagree about WHICH
        files changed. Returned as a copy taken under the lock (the watcher mutates the
        live maps); the caller diffs it against the parse cache's own certified leaves.
        """
        with self._lock:
            states = self._states.get(getattr(part, "corpus_id", None))
            if states is None:
                return None
            return {rp: dict(st.leaves) for rp, st in states.items()}

    def tracked_roots(self, part) -> Optional[List[str]]:
        """The root paths this kernel is maintaining for a partition (or None)."""
        with self._lock:
            states = self._states.get(getattr(part, "corpus_id", None))
            return list(states) if states is not None else None

    def rebind_partition(self, part) -> None:
        """Point the kernel's per-corpus state at `part` without re-hashing.

        Used after a self-heal that rebuilt a partition's CONTENT in place: the leaves and
        root are already authoritative for that content, so re-seating (a full re-hash)
        would be pure waste. Keeps the O(0) query path intact after a heal.
        """
        with self._lock:
            if part.corpus_id in self._states:
                self._parts[part.corpus_id] = part
                part.attach_kernel(self)

    # --- incremental update (fast path — mtime/size HINT) ---------------------
    def poll_once(self, part) -> bool:
        """Re-hash ONLY leaves whose (mtime,size) hint changed / were added / removed.

        Returns True iff the maintained root actually changed. Unchanged files are
        stat()'d (cheap) but never re-read. A content-preserving `touch` re-hashes the
        one leaf, finds the SAME content hash, and does NOT flip the root (no false-stale).
        """
        with self._lock:
            states = self._states.get(part.corpus_id)
            if states is None:
                return False
            changed = False
            for st in states.values():
                changed |= self._poll_root(st)
            if changed:
                self._recompute_root(part.corpus_id, states)
            return changed

    def _poll_root(self, st: RootState) -> bool:
        try:
            current = set(self._lister(st.root_path))
        except OSError:
            # An inaccessible root is UNVERIFIABLE. Force the leaves empty so the root
            # diverges from the index hash -> the freshness gate fails closed (STALE),
            # never silently fresh.
            if st.leaves:
                st.leaves.clear()
                st.hints.clear()
                return True
            return False
        if st.fault is not None:
            # A member is a corpus-file-* failure and the leaves were EMPTIED (the root diverges: STALE, never fresh).
            # [Opus Diamond F1] The fault must NEVER outlive its cause, or later edits of OTHER members would be hidden: it is
            # cleared (a) when the leaves were repopulated by a successful full re-hash (self-heal / backstop), (b) when the
            # bad member left the listing, (c) when a hash of the bad member now SUCCEEDS (the cause can leave mtime, size
            # and mode untouched: an ACL, a policy layer). While it stands the leaves stay empty, so the root stays STALE;
            # this probe hashes ONE file per tick, not the whole corpus.
            _code, bad_rel, _bad_hint = st.fault
            if st.leaves or bad_rel not in current:
                st.fault = None
            else:
                try:
                    self._hash_file(os.path.join(st.root_path, bad_rel), bad_rel)
                except OSError:
                    return False                             # still unreadable: leaves stay EMPTY, the root stays STALE
                st.fault = None
        prev = set(st.leaves)
        changed = False
        cur = ""
        try:
            for rel in sorted(current - prev):               # added file
                cur = rel
                ap = os.path.join(st.root_path, rel)
                st.leaves[rel], st.hints[rel] = self._hash_stable(ap, rel)      # hint BEFORE the hash (P2-A)
                changed = True
            for rel in prev - current:                       # removed file
                st.leaves.pop(rel, None)
                st.hints.pop(rel, None)
                changed = True
            for rel in sorted(current & prev):               # possibly modified
                cur = rel
                ap = os.path.join(st.root_path, rel)
                hint = self._safe_hint(ap)
                if st.hints.get(rel) == hint:
                    continue                                 # hint says unchanged -> skip re-read
                new_hash, hint = self._hash_stable(ap, rel)  # CONTENT is the verdict; hint taken before, re-checked after
                st.hints[rel] = hint                         # (recorded only once the hash succeeded)
                if new_hash != st.leaves[rel]:
                    st.leaves[rel] = new_hash
                    changed = True
        except OSError as exc:
            # [ISSUE-NONREGULAR-MEMBER] a member that turned unreadable (or a new one that is) is
            # an UNVERIFIABLE root, exactly like an unlistable one: empty the leaves so the root diverges from the index hash
            # and the gate fails closed (STALE, never fresh over old content). A raw OSError that is not already coded (EIO,
            # a vanished file) is the same: [Opus Diamond P2-2] fail CLOSED as `corpus-file-unreadable`.
            code = exc.code if isinstance(exc, CorpusFileError) else CORPUS_FILE_UNREADABLE
            rel_p = exc.rel_path if isinstance(exc, CorpusFileError) and exc.rel_path in current else cur
            st.fault = (code, rel_p, self._safe_hint(os.path.join(st.root_path, rel_p)) if rel_p else (0.0, -1, 0))
            st.leaves.clear()
            st.hints.clear()
            return True
        return changed

    def _safe_hint(self, abs_path: str) -> Tuple[float, int, int]:
        try:
            return self._stat_hint(abs_path)
        except OSError:
            return (0.0, -1, 0)

    # --- the backstop (missed / hint-invisible change) ------------------------
    def full_reverify(self, part) -> bool:
        """Re-hash EVERY leaf unconditionally (content oracle) and recompute the root.

        The bounded periodic backstop: catches any change the hint-based `poll_once`
        could miss (identical mtime+size but different bytes: `touch -r`, `cp -p`, clock
        skew). Returns True iff the maintained root changed as a result.
        """
        with self._lock:
            states = self._states.get(part.corpus_id)
            if states is None:
                return False
            old = self._roots.get(part.corpus_id)
            for st in states.values():
                self._reload_root_full(st)
            self._recompute_root(part.corpus_id, states)
            return self._roots.get(part.corpus_id) != old

    def _recompute_root(self, corpus_id: str, states: Dict[str, RootState]) -> None:
        self._roots[corpus_id] = combine_roots(
            {rp: st.root_hash() for rp, st in states.items()})

    def _mark_unverifiable(self, part) -> None:
        """A full re-verify could not read the corpus: empty its leaves so the maintained root diverges (STALE, fail closed)."""
        with self._lock:
            states = self._states.get(part.corpus_id)
            if not states:
                return
            for st in states.values():
                st.leaves.clear()
                st.hints.clear()
            self._recompute_root(part.corpus_id, states)

    # --- the background watcher loop ------------------------------------------
    def tick(self) -> None:
        """One watcher cycle over all seated partitions: a fast poll of each, plus a
        full re-verify of each every `backstop_every` ticks (the missed-event backstop)."""
        with self._lock:
            self._ticks += 1
            do_backstop = (self._ticks % self.backstop_every == 0)
            parts = list(self._parts.values())
        for part in parts:
            # ONE bad corpus must never stop the others from being polled: a fault is contained to its corpus (and the corpus
            # itself fails closed, see `_poll_root`; an exception here is a backstop fault, which the self-heal reports).
            try:
                self.poll_once(part)
            except Exception:  # noqa: BLE001
                pass
            if do_backstop:
                try:
                    self.full_reverify(part)
                except Exception:  # noqa: BLE001 — the leaves are left as they were: the NEXT heal re-reads and reports it
                    self._mark_unverifiable(part)

    def start(self) -> None:
        """Start the daemon polling watcher (deployment path). Idempotent."""
        if self._thread is not None:
            return
        self._stop.clear()

        def _run() -> None:
            while not self._stop.wait(self.poll_interval):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 — a watcher fault must not kill the server
                    pass

        self._thread = threading.Thread(target=_run, name="lts-freshness-watch",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=self.poll_interval * 2)
        self._thread = None
