"""[MECH-BR-WRITES] x-membership: ONE WRITE'S READING of the registered roots.

A write has to know where each registered root really is (a root's path may pass through a link) and whether it answers. Doing
that by resolving every root on the write path is what let one hung drive stop a write to a healthy file. So the engine takes
the reading ONCE, at the start of a call, from what the roots' own probes last answered, and every check in the write (membership,
the ledger's root check, `ledger.locate`) goes through that one object instead of resolving a root. A write resolves a root in one case only, to confirm an attach
(`factlattice_engine.membership._confirmed_hits`): one fresh realpath of an ok candidate root, on an auxiliary slot of its own, never queued
behind the root's probe, joined by every write that finds it running, and never of a marked root. A confirm never marks a root.

THIS MODULE HOLDS NO DISK CALL. It is the shape of the reading and the thread-local that carries it. Who fills it (the freshness
kernel's kept answers, or a bounded resolve in a process with no probe) is the engine's business (`factlattice_engine.membership`,
`factlattice_engine.boundedfs`). The core package cannot import the engine, so the engine installs a view and the ledger asks for
the current one; with none installed (a library caller) everything behaves exactly as it did before.

A view is a fact about the LAST ANSWER, not about now: a link swapped while a server runs reads the old place until the next
probe, one poll later. It is built fresh for every call and never kept across calls.
"""
from __future__ import annotations

import os
import threading
import unicodedata
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass

OK = "ok"                                   # the same words as factlattice_engine.rootstate (the one registry of them)
#: the refusal code of a write to a file under a root that is not answering. Defined HERE, once, because the core package (the ledger and the
#: writers) cannot import the engine; `factlattice_engine.rootstate` takes it from here, so the engine still has one registry of the words.
CODE_ROOT_UNRESPONSIVE = "root-unresponsive"
#: the refusal of a call naming a corpus that is held pending for the registry's verdict (its entry is one the release refuses; `VERDICT_PENDING_WHEN` below says when the server
#: enters registry-error mode). Defined here for the same reason: the ledger and the writers need it, and `rootstate` takes it from here.
CODE_CORPUS_VERDICT_PENDING = "corpus-verdict-pending"
#: when the registry-error mode a held corpus is waiting for begins, in the words every refusal and fix text of that code uses (one wording, in one place)
VERDICT_PENDING_WHEN = "the server enters registry-error mode (at the end of its first read pass, or at once when a later read meets the verdict)"


def lexical(path) -> str:
    """The path as written, made absolute and tidy, with NO symlink resolution: a string operation (a relative path asks the
    process for its working directory, which is not a registered root)."""
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))


def fold(path: str) -> str:
    """The comparison form for a REFUSAL: case-folded on every volume. A case-insensitive volume does not fold case in `realpath`, so a path spelled
    in another case names the same marked root; on a case-sensitive volume this over-refuses a distinct directory whose name differs only in case,
    which is the cheaper error (fail closed). Unicode is composed first (NFC): APFS treats a decomposed and a composed spelling as one file. Attach
    decisions never use it."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", os.path.normcase(path)).casefold())


def inside(root: str, path: str) -> bool:
    """`path` is `root` or lies beneath it (`commonpath`, never a string prefix)."""
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:
        return False


@dataclass(frozen=True)
class RootFact:
    """One registered root as last answered: where it was asked (`declared`, lexical), where it really is (`real`, from the last
    answer, None if it never answered) and its state (`ok`, `pending`, `unresponsive`)."""
    declared: str
    real: str | None
    state: str
    #: a string of a registry entry that FAILED on an unknown key (a typo'd `pth` holds the folder under another name): not a root anyone declared.
    #: It is probed like one, and a pending or unresponsive one is counted in `marked_count`, but only a write under it AS TYPED is refused
    #: (its kept real path makes a write a CANDIDATE: flagged and unattributed, never refused)
    untrusted: bool = False


class RootView:
    """What one call knows of the registered roots. Immutable after construction, so one write acts on one reading."""

    def __init__(self, facts: Iterable[RootFact] = (), *, resolver: Callable[[str], str] | None = None,
                 bounded: Callable[[str, Callable[[], object], object], object] | None = None) -> None:
        self.facts: tuple[RootFact, ...] = tuple(facts)
        self._resolver = resolver
        self._bounded = bounded
        self._by: dict[str, RootFact] = {}
        for f in self.facts:
            self._by.setdefault(f.declared, f)
            if f.real:
                self._by.setdefault(f.real, f)
        self._marked: tuple[RootFact, ...] = tuple(f for f in self.facts if f.state != OK)
        #: the path of the file this call writes, AS TYPED (not resolved): membership matches a root's declared spelling against it on a miss
        self.typed: str | None = None
        #: the RootRegistry that bounds this call's own reads (the kernel's, in a process that has one); opaque to the core
        self.fs_registry: object = None
        #: candidate roots (ok in the view) whose confirm did not answer in this call. NOT marked roots: the root stays ok for its probes to judge.
        self._unconfirmed: set = set()
        #: any unconfirmed candidate (a root that could hold the file by its kept real path or its declared spelling): the write cannot say where its file lives, so it is refused
        self.unconfirmed_holding: bool = False
        #: after the pending wait, a corpus holding this file's root is STILL held pending for the registry's verdict: the write is refused `corpus-verdict-pending`
        self.verdict_pending: bool = False
        #: the membership verdict of this call, by the file's real path: a write evaluates membership ONCE and the ledger record, the edge context and
        #: the receipt all read that one result
        self.verdicts: dict[str, dict] = {}
        #: the REGISTRY-STAGE failure of this call's membership evaluation, by the file's real path: the first evaluation's outcome is THE result for the
        #: write, failure included, and nothing evaluates membership again in the same call
        self.failures: dict[str, BaseException] = {}
        #: `time.monotonic()` when the write's own file resolve returned: a confirm's answer may be used only if that confirm job STARTED after it
        self.resolved_at: float | None = None

    # ---- what the view says ---------------------------------------------------------------------------
    @property
    def marked_count(self) -> int:
        """How many registered roots were pending or unresponsive when the view was taken (and no others: a confirm never marks a root)."""
        return len(self._marked)

    @property
    def unconfirmed_count(self) -> int:
        """How many candidate roots had a confirm that did not answer in this call."""
        return len(self._unconfirmed)

    @property
    def partial(self) -> bool:
        """The membership of this call is not whole: a root is marked, or a candidate went unconfirmed (which refuses the write, so on a write that lands it is the marked roots)."""
        return bool(self._marked or self._unconfirmed)

    def partial_fields(self) -> dict:
        return {"marked_count": self.marked_count, "unconfirmed_count": self.unconfirmed_count}

    def fact(self, declared) -> RootFact | None:
        """The fact for a root by its declared (lexical) spelling, or None."""
        f = self._by.get(lexical(declared))
        return f if f is not None and f.declared == lexical(declared) else None

    def note_unconfirmed(self, declared) -> None:
        """A candidate root (ok in the view) did not answer the confirm made for this call. A candidate is a root that COULD hold this file, by its kept
        real path or by its declared spelling as typed, so the write cannot say where its file lives and is refused (`membership-unconfirmed`). It is
        counted (unconfirmed_count); the root itself is not marked."""
        self._unconfirmed.add(lexical(declared))
        self.unconfirmed_holding = True

    def real(self, path) -> str:
        """The real path of a ROOT. A known root answers from its last result; a marked root that never answered answers with its
        lexical spelling (nothing is read from it); a path the view does not know goes to the resolver (bounded where the engine
        installed one), else is resolved as it always was."""
        key = lexical(path)
        f = self._by.get(key)
        if f is not None:
            if f.real:
                return f.real
            if f.state != OK:
                return key
        if self._resolver is not None:
            return self._resolver(key)
        return os.path.realpath(key)

    def marked_holding(self, path, only_state: str | None = None) -> bool:
        """Does `path` lie at or under a MARKED root, by that root's declared spelling or the real path it last answered? A string
        comparison only: this is how a write learns, before it reads anything, that the disk behind a file is not answering. A
        file that reaches a marked root only through a link nobody registered is not seen here."""
        if not self._marked:
            return False
        p = fold(lexical(path))
        for f in self._marked:
            if only_state is not None and f.state != only_state:
                continue                             # the caller asks about one state only (rootstate's word), e.g. a root that is NOT ANSWERING, not one merely pending
            if inside(fold(f.declared), p) or (not f.untrusted and f.real is not None and inside(fold(f.real), p)):
                return True
        return False

    def pending_holding(self, path, pending: str) -> tuple[RootFact, ...]:
        """The roots in state `pending` (the word is rootstate's; this module does not keep a copy of it) that hold `path`, by their declared
        spelling or a real path they last answered. A string comparison only. A write under one of them WAITS for its first read, as a named
        read does ([MECH-BR-BOOT] x-build-traps 5)."""
        if not self._marked:
            return ()
        p = fold(lexical(path))
        return tuple(f for f in self._marked
                     if f.state == pending and (inside(fold(f.declared), p) or (not f.untrusted and f.real is not None and inside(fold(f.real), p))))

    def bounded(self, key: str, fn: Callable[[], object], default: object) -> object:
        """Run `fn()` (a stat or a read under a state location or an old spelling) with the engine's bound, or `default` if it
        did not answer in time. With no bound installed, `fn()` as it always was."""
        if self._bounded is None:
            return fn()
        return self._bounded(key, fn, default)


EMPTY = RootView()

_tls = threading.local()


def current() -> RootView | None:
    """The view the current call was started with, or None (a library caller: nothing changes)."""
    return getattr(_tls, "view", None)


@contextmanager
def use(view: RootView | None):
    """Make `view` the current one on this thread for the duration. Nests; restores the previous one."""
    prev = getattr(_tls, "view", None)
    _tls.view = view
    try:
        yield view
    finally:
        _tls.view = prev


def install(view: RootView | None) -> RootView | None:
    """Set the current view without a `with` block, returning what it replaced (for `restore`). A caller that cannot indent its
    whole body (the dispatcher) pairs this with `restore` in a `finally`."""
    prev = getattr(_tls, "view", None)
    _tls.view = view
    return prev


def restore(prev: RootView | None) -> None:
    _tls.view = prev


__all__ = ["RootFact", "RootView", "EMPTY", "OK", "current", "use", "install", "restore", "lexical", "inside"]
