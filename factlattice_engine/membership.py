"""Is this path INSIDE a corpus? — the cheapest fix in the series, and it closes a class.

[FL-RECALL-INVISIBLE-PRECONDITIONS] precondition-1:

    "A document authored outside every registered root is simply invisible. There is no
     warning at author time, no warning at lint time, and no way to discover it except
     noticing that queries do not return it. An 802-line specification sat in a
     deployment-side proposals directory OUTSIDE every registered root for a day, fully
     valid, perfectly linted, and unreachable by every query."

    ⛔ AND THE REFLEXIVE PROOF: `docs/improvement_suggestions/` — the directory holding the
    four documents proposing improvements to this engine — appears in NO corpus
    `root_paths`. The documents critiquing the query engine were unqueryable BY that
    engine, and nobody noticed for four documents.

WHAT THIS IS AND IS NOT
    It is a WARNING ON A SANCTIONED WRITE, naming the registered roots. It is NOT a
    refusal, and making it one would be wrong: `must-accept-arbitrary-paths` is normative
    for the authoring layer — "corpus-scoped decision files now live with their datasets,
    outside corpus roots. A tool that writes only inside the corpus root would miss the
    files that most need it." Refusing would push the author straight back to the raw text
    edit, which is the whole failure this layer exists to invert.

    So: the write LANDS, and the receipt says the graph will not see it.

WHERE THE ROOTS COME FROM, AND WHY IT SAYS WHICH
    The live store when one is seated (the roots the SERVING process actually indexed);
    the declared corpus registry otherwise, because the authoring layer answers with no
    store loaded by design. The two can legitimately differ — a corpus registered but not
    yet built is in the config and not in the store — so the answer names its own source
    rather than presenting the union as one fact. A precondition check that cannot say
    which precondition it checked is the shape of the defect it is closing.
"""
from __future__ import annotations

import copy
import os
import time
from typing import Any

from factlattice import rootview as _rv


def _norm(path: str) -> str:
    """Canonicalize for comparison. `os.path.realpath` — not `abspath` — because it also
    resolves symlinks: a corpus root registered by its symlink path (`~/projectlink`,
    itself a symlink into a file-sync service) and an authored path resolved upstream via
    `Path(...).resolve()` (`factlattice.authoring.edit`, which ALSO resolves symlinks)
    must land on the SAME string, or a write through a symlinked root false-positives as
    "outside every registered corpus root" even though it lands inside one.
    [FL-MEMBERSHIP-SYMLINK] Both sides of every containment check pass through this
    function, so canonicalizing here canonicalizes both sides at once."""
    return os.path.realpath(os.path.expanduser(str(path)))


def _root_real(path: str) -> str:
    """The real path of a REGISTERED ROOT. [MECH-BR-WRITES] x-membership: from the current call's reading of the roots (each root's
    last answered probe), never from the disk, so a hung root cannot stop a write to another. With no reading installed (a library
    caller) it is `_norm`, as it always was. The FILE being written is never passed here: it is resolved by `_norm`, as today."""
    view = _rv.current()
    return view.real(path) if view is not None else _norm(path)


def _contains(root: str, path: str) -> bool:
    """True when `path` is `root` or lies beneath it. `os.path.commonpath` rather than a
    `startswith`, so `/a/corpus-old` is not read as inside `/a/corpus`."""
    r = _root_real(root)
    try:
        return os.path.commonpath([r, _norm(path)]) == r
    except ValueError:      # different drives / one is relative in a way that cannot join
        return False


def _contains_real(root_real: str, file_real: str) -> bool:
    """`_contains` for a root and a file already resolved, so the check resolves the file once, not once per root."""
    return _rv.inside(root_real, file_real)


def _look(store: object):
    """The store's ONE look, `(served, pending, failed, no_roots)`: `store.snapshot_all()`, or four empty tables for no store. A store that cannot
    give it RAISES, and the caller treats that as membership UNCONFIRMED (the write is refused): nothing here turns a failed look into 'nothing
    there'."""
    if store is None:
        return [], {}, {}, {}
    return store.snapshot_all()


def _view_specs(view) -> dict:
    """The registry's entries as this write's view holds them: read ONCE per write (the first reader keeps them on the view), not once per reader. A
    registry that cannot be read raises, as it always did, and keeps nothing."""
    specs = getattr(view, "specs", None)
    if specs is None:
        err = getattr(view, "specs_error", None)
        if err is not None:
            raise err                                # the read failed once in this write: THAT is the registry's answer for the whole write, never a second read
        from . import config
        try:
            specs = config.default_corpora() or {}
        except Exception as exc:  # noqa: BLE001
            exc._registry_read = True
            view.specs_error = exc
            raise
        view.specs = specs
    return specs


def _look_failed(view) -> bool:
    """True when the store's look failed when this view was taken (noted on it as unconfirmed)."""
    return hasattr(view, "look") and view.look is None


def _view_look(view, store: object):
    """The call's ONE look, as the view carries it. A view whose look failed re-raises that failure (the write is refused); a view built by hand
    (a test double, with no `look` at all) takes the store's look, once."""
    if hasattr(view, "look"):
        if view.look is None:
            raise view.look_error
        return view.look
    return _look(store)


def snapshot(store: object = None, look=None) -> _rv.RootView:
    """The reading of the registered roots a call takes ONCE, at its start ([MECH-BR-WRITES] x-membership), together with the call's ONE look at the
    store (`view.look`, which `check` and everything under it read; a write takes no second look). With a server's kernel it is the kernel's kept
    answers: no disk. A root the kernel holds no answer for (a process with no probe, or a corpus that failed to build and so is in no slot) is
    resolved on the bounded machinery of `boundedfs`, all of them at once, so K hung roots cost one deadline and a pending or failed corpus's root
    still counts as a root. It does not raise, and it does not hide a failure: a look that raised, or a declared root that cannot be evaluated, is
    NOTED on the view (`note_unconfirmed`), so the write that reads this view is refused `membership-unconfirmed` ([MECH-BR-WRITES] D). A registry
    that does not read is the registry stage's RegistryError, raised by `check`, as before."""
    from . import boundedfs
    facts = []
    kernel = getattr(store, "kernel", None) if store is not None else None
    # ONE registry per process: a process with a kernel bounds its own reads on the kernel's registry, so a root has one slot and one running job
    reg = getattr(kernel, "roots", None) if kernel is not None else None
    have = set()
    if kernel is not None and hasattr(kernel, "root_facts"):
        facts = list(kernel.root_facts())
        have = {f.declared for f in facts}
    need: list[str] = []
    problem = None
    look_error = None
    try:        # BEFORE the look: a register between the two reads then leaves a stale flag (more flagged), never a cleared flag beside an old look
        unlocated = store.unlocated() if store is not None and hasattr(store, "unlocated") else frozenset()
    except Exception:  # noqa: BLE001 -- a store that cannot say is a store that cannot say where an entry lives
        unlocated = frozenset({"<unknown>"})
    specs_read = None
    specs_error = None
    try:
        if look is None:
            if store is not None and hasattr(store, "consistent") and hasattr(store, "unlocated"):
                with store.consistent():                  # ONE locked step: the tables and the unlocated set move together (settle writes both in one)
                    look = _look(store)
                    unlocated = store.unlocated()
            else:
                look = _look(store)
    except Exception as exc:  # noqa: BLE001 -- NOT swallowed: noted below, and the write is refused
        look, look_error, problem = None, exc, "<the look at the store failed>"
    untrusted: set = set()                         # [MECH-BR-BOOT] x-load-error (UNTRUSTED ROOTS): the strings of the entries that failed on a key, as this write's registry reading has them
    try:
        failed = look[2] if look is not None else {}
        if kernel is None or failed:
            from factlattice import registry as _registry

            from . import config
            try:
                specs = specs_read = config.default_corpora() or {}
            except Exception as exc:  # noqa: BLE001 -- NOT swallowed: kept on the view, and every reader of the registry in this write meets THIS failure (P2-6)
                exc._registry_read = True
                specs_error = exc
                raise
            for cid, spec in specs.items():
                if kernel is None or cid in failed:
                    need += [p for p in _declared_lexical(spec)]
                    if isinstance(spec, dict) and _registry.entry_key_problems(spec):
                        untrusted |= {_rv.lexical(x) for x in config.untrusted_path_strings(spec)}
    except Exception:  # noqa: BLE001 -- the REGISTRY stage (a registry that cannot be read, or an entry that cannot be evaluated) is `registered_roots`' to raise,
        need = []                                  # and `check`'s to keep for the whole write (the pinned rule, MECH-BR-WRITES D): it is met again there, not hidden here
        untrusted = set()
    # a string of a failed entry is a root-like fact, and the ONLY untrusted facts a view carries are the ones this write's registry reading still names
    # (a probe left over from an entry since fixed marks nothing)
    facts = [f for f in facts if not f.untrusted or f.declared in untrusted]
    have = {f.declared for f in facts}
    need = [p for p in dict.fromkeys(need + sorted(untrusted)) if p not in have]
    if need:
        facts += boundedfs.resolve_roots(need, reg, untrusted=untrusted)
    view = _rv.RootView(facts, resolver=lambda p: boundedfs.resolve_one(p, reg),
                        bounded=lambda key, fn, default: boundedfs.bounded_call(key, fn, default, reg))
    view.fs_registry = reg
    view.look, view.look_error = look, look_error
    view.unlocated = unlocated
    if specs_read is not None:
        view.specs = specs_read                  # the write's one read of the registry's entries
    elif specs_error is not None:
        view.specs_error = specs_error           # ... or its one failed read, which stands for the whole write: the pinned registry rule (the write lands flagged)
    if problem is not None:
        view.note_unconfirmed(problem)
    return view


def await_pending(view: _rv.RootView, path, store: object = None) -> _rv.RootView:
    """[MECH-BR-BOOT] x-build-traps 5: a write to a file under a root that is still PENDING (never yet read: the boot window), or under a corpus that is
    still being read, WAITS for that read, up to the root deadline, as a named read does, and then takes a NEW reading of the roots (the root is ok and
    the corpus served, or unresponsive and the write is refused root-unresponsive by the ordinary rule). Slice 3 refused it at once. Two waits, in
    order: the pending ROOT's first read (its slot leaves pending when the seat ends), then the pending CORPUS (it is published a moment after its
    roots turn ok; a write in that gap would otherwise be recorded as outside every corpus). A wait stops early when a root of the corpus is
    unresponsive (it will not finish). A root and corpus that are not pending, a process with no kernel and a path under neither return `view`
    unchanged. String comparison decides; no disk is touched on this thread."""
    if _look_failed(view):
        return view                      # the store's look failed (noted on the view): nothing pending is known and nothing is waited for; `check` refuses membership-unconfirmed
    try:
        typed = os.fspath(path)
    except TypeError:
        return view
    if not isinstance(typed, str) or not typed.strip() or "\x00" in typed:
        return view
    from .rootstate import PENDING, UNRESPONSIVE
    holding = view.pending_holding(typed, PENDING)
    kernel = getattr(store, "kernel", None) if store is not None else None
    reg = getattr(kernel, "roots", None) if kernel is not None else None
    if reg is None:
        return view
    p = _rv.fold(_rv.lexical(typed))
    pend = {cid: rec for cid, rec in _view_look(view, store)[1].items()
            if any(_rv.inside(_rv.fold(_rv.lexical(r)), p) for r in rec.get("paths") or ())}
    if not holding and not pend:
        return view
    def unresponsive(rec):
        m = reg.mark_for(rec.get("paths") or [])
        return m is not None and m["status"] == UNRESPONSIVE
    if any(unresponsive(rec) for rec in pend.values()):
        # a root of the corpus is already marked: it will not finish, and the file's own root is pending BECAUSE of it (its seat waits for the sibling),
        # so waiting would only spend a deadline on every attempt. The write is refused at once by the ordinary rule (a pending root is marked).
        return view
    end = time.monotonic() + reg.deadline_s
    for f in holding:
        slot = reg.slot(f.declared, create=False)
        if slot is not None:
            slot.wait_not_pending(max(0.0, end - time.monotonic()))
    for cid, rec in pend.items():
        store.wait_loaded(cid, max(0.0, end - time.monotonic()), stop=lambda rec=rec: unresponsive(rec))
    fresh = snapshot(store)                       # a NEW look after the wait replaces the old one whole
    fresh.typed = view.typed
    # A corpus held pending for the registry's verdict is answered EXACTLY as any pending corpus: the wait above is the same one root deadline. If the mode's step came within it, the
    # fresh look is the mode's and the write meets its rules. If the corpus is still held (judged on this FRESH look, so a write whose first look predates the verdict is covered), the
    # write is refused `corpus-verdict-pending` (`authoring.verdict_pending_refusal`): the true cause, never a root that did not answer.
    for rec in _view_look(fresh, store)[1].values():
        if rec.get("verdict") and any(_rv.inside(_rv.fold(_rv.lexical(r)), p) for r in rec.get("paths") or ()):
            fresh.verdict_pending = True
            break
    return fresh


def _declared_map(look, view=None) -> dict[str, list[str]]:
    """{corpus: [declared root spelling, lexical]}: the served corpora's roots as the kernel was given them, and, for a corpus that failed to build
    or is pending, the registry's. No disk. It takes the call's ONE look; a failure of THAT is the caller's to turn into membership UNCONFIRMED (`check`
    does). The registry is read ONCE per write (`_view_specs`); a read that fails raises, and `check` makes that the registry stage's result (the write lands
    flagged), never a stale answer."""
    declared: dict[str, list[str]] = {}
    served, pending, failed, _no_roots = look
    for p in served:
        declared[p.corpus_id] = [_rv.lexical(r) for r in (p.root_paths or [])]
    served_known = bool(declared)
    if not served_known or failed or pending:
        if view is not None:
            specs = _view_specs(view)
        else:
            from . import config
            specs = config.default_corpora() or {}
        for cid, spec in specs.items():
            if cid not in declared and (not served_known or cid in failed or cid in pending):
                declared[cid] = _declared_lexical(spec)
    return declared


def _confirmed_hits(view: _rv.RootView, store: object, real: str, kept_hits: list[str]) -> list[str]:
    """[MECH-BR-WRITES] x-membership. Kept real paths decide the REFUSAL and which roots are CANDIDATES for this file: a root whose kept real path
    holds the file's real path, or whose declared spelling holds the path as typed (a link swapped since the last probe: the kept path is one poll
    old). The ATTACH decision confirms each candidate that is OK with one bounded fresh resolve of that root, run on the root's own auxiliary
    "confirm" slot and never queued behind its probe (`boundedfs.confirm_real`; a confirm that began after this write's file resolve is JOINED): the
    write attaches to a corpus only if the file's real path lies under that fresh real path, so neither a stale kept path nor a link inside a root
    can attach it to the wrong corpus. A marked or pending root is never resolved (a write under one was refused earlier); it attaches by its kept
    path as before. A candidate root whose confirm does not answer is noted on the view (`unconfirmed_count`) and the write is REFUSED
    `membership-unconfirmed` (so a write that lands has unconfirmed_count 0). A kept answer that no declared root of its corpus explains keeps
    standing."""
    from . import boundedfs
    typed = None
    if getattr(view, "typed", None):
        try:
            if _norm(view.typed) == real:                # the typed path must be the very file being checked
                typed = _rv.lexical(view.typed)
        except (OSError, ValueError):
            typed = None
    declared = _declared_map(_view_look(view, store), view)      # a failed look raises here: the confirm stage fails closed with it
    out = set()
    explained = set()                                # corpora with a root that was a candidate: confirmed or not, by the fresh path alone
    fresh: dict[str, str | None] = {}
    for cid, spellings in declared.items():
        for d in spellings:
            f = view.fact(d)
            if f is None:
                if _rv.inside(view.real(d), real):       # a root the view holds no answer for is resolved by the bounded resolver: fresh already
                    out.add(cid)
                    explained.add(cid)
                continue
            kept_in = bool(f.real) and _rv.inside(f.real, real)
            if not (kept_in or (typed is not None and _rv.inside(d, typed))):
                continue
            explained.add(cid)
            if f.state != _rv.OK:
                if kept_in:
                    out.add(cid)
                continue
            if d not in fresh:
                got = boundedfs.confirm_real(d, view.fs_registry, not_before=view.resolved_at)
                if not got:
                    # stuck, or no answer within the root deadline (a confirm already running that began after this write's resolve is JOINED, so
                    # this is its result too): counted, never marked. Every candidate could hold this file (by its kept path or its declared
                    # spelling), so the write cannot say where its file lives and is refused
                    view.note_unconfirmed(d)
                    fresh[d] = None
                else:
                    fresh[d] = got
            if fresh[d] and _rv.inside(fresh[d], real):
                out.add(cid)
    out |= {c for c in kept_hits if c not in explained}   # a kept answer no declared root explains (the map does not know it) stands
    return sorted(out)


def _declared_lexical(spec: Any) -> list[str]:
    """`declared_root_paths` without resolving anything: the same paths, as written."""
    _shape_guard(spec)
    out: list[str] = []
    for root in (spec.get("roots") or []):
        p = root.get("path") if isinstance(root, dict) else root
        if p and isinstance(p, (str, os.PathLike)):
            out.append(_rv.lexical(p))
    r = spec.get("root")
    if r and isinstance(r, (str, os.PathLike)):
        out.append(_rv.lexical(r))
    return out


def current_view(store: object = None, look=None) -> _rv.RootView:
    """The call's reading if one is installed, else a fresh one (built from `look`, the caller's own look, if given)."""
    return _rv.current() or snapshot(store, look)


def _untrusted_entry_guard(real: str, view, store: object = None) -> None:
    """A corpus that FAILED on an unknown registry key has a location nobody can trust (a typo'd `pth` holds the folder under another name), so a write
    whose file lies under any path-like string of that entry is not attributed and not called 'outside every root': it raises, which is the registry
    stage's result (the write lands flagged, `corpora` null). It is never REFUSED for being a candidate. Other writes are untouched.

    [MECH-BR-BOOT] x-load-error (UNTRUSTED ROOTS) (the architects' design, specs/38): NO DISK. Each string was probed on the process's own registry, off every writer's
    thread (the kernel at boot, or `snapshot`'s bounded resolve in a process with none), and the view carries the answer: a write is a candidate when
    its file lies under the string AS TYPED or under the real path the probe kept. A string that has not answered (pending, unresponsive) has no kept
    path; it counts in `marked_count` and a write under it AS TYPED is refused `root-unresponsive` by `authoring.marked_refusal`, before this runs."""
    failed = _view_look(view, None)[2] if hasattr(view, "look") else {}
    if not failed and store is not None:
        return                                     # a store knows which corpora failed; with NO store (a library writer) the registry's own entries decide
    from factlattice import registry

    from . import config
    from .reviewer import _untrusted_strings
    specs = _view_specs(view)                      # an unreadable registry raises: the pinned registry rule (the write lands flagged)
    real_f = _rv.fold(real)
    typed = getattr(view, "typed", None)
    typed_f = _rv.fold(_rv.lexical(typed)) if isinstance(typed, str) and typed.strip() and "\x00" not in typed else None
    for cid in (failed if store is not None else list(specs)):
        spec = specs.get(cid)
        if not (isinstance(spec, dict) and registry.entry_key_problems(spec)):
            continue
        strings = _untrusted_strings(spec)
        if registry.named_failure(spec) is not None or any(x.strip().startswith("${") or "\x00" in x for x in strings):
            raise RuntimeError("the file may lie under an entry whose location cannot be read")     # no location at all: it may hold any file
        if config.unstated_location(spec):
            raise RuntimeError("the file may lie under an entry whose location cannot be stated")   # a root-ish bare word in a mistyped key: where is not known
        for x in config.untrusted_path_strings(spec):
            cand = _rv.lexical(x)
            cand_f = _rv.fold(cand)
            f = view.fact(cand)
            if _rv.inside(cand_f, real_f) or (typed_f is not None and _rv.inside(cand_f, typed_f)) \
                    or (f is not None and f.real and _rv.inside(_rv.fold(f.real), real_f)):
                raise RuntimeError("the file lies under an entry whose location cannot be trusted")


def _shape_guard(spec: dict) -> None:
    """Raise `TypeError` for an entry with NO STATED LOCATION (`config.unstated_location`): its roots cannot be read as paths, so a write lands flagged and
    attributed to no corpus. An entry whose other fields are malformed (a non-boolean `private`, an int `domain_suffix`) still has readable roots and is NOT
    refused here: the release serves it, and its location is its declared roots."""
    from . import config
    if config.unstated_location(spec, strings=False):
        raise TypeError("the entry's roots cannot be read as paths")


def declared_root_paths(spec: Any) -> list[str]:
    """Every root path ONE registry entry DECLARES, normalised (`~` expanded, realpath), in order, without duplicates:
    `roots` (dicts with a `path`, or bare strings) AND the single `root`. A deliberate fail-safe SUPERSET of what the
    engine serves (`config.served_root_paths`: roots if present, else root). Use it to FIND which corpora might contain a
    file (privacy, membership); never to decide that a file is under a corpus the engine serves."""
    _shape_guard(spec)                           # a malformed shape (a non-object entry too) raises (membership then refuses): a string `roots` is never read as one root per character
    paths: list[str] = []
    for root in (spec.get("roots") or []):
        p = root.get("path") if isinstance(root, dict) else root
        if p and isinstance(p, (str, os.PathLike)):
            paths.append(_root_real(p))
    r = spec.get("root")
    if r and isinstance(r, (str, os.PathLike)):
        paths.append(_root_real(r))
    return list(dict.fromkeys(paths))


def registered_roots(store: object = None) -> tuple[dict[str, list[str]], str]:
    """`({corpus_id: [root, ...]}, source)`.

    `source` is `"store"` (the seated, serving index) or `"config"` (the declared
    registry) or `"none"`. Reported, never inferred by the caller.

    [MECH-BR-WRITES] x-membership: every root's real path comes from the call's reading of the roots (`snapshot`), not from the disk, and the corpora
    from the call's ONE look (`view.look`). Nothing here swallows a failure: a look that raised, or a root that cannot be evaluated, raises out to
    `check`, which makes membership UNCONFIRMED (the write is refused). The one exception is a registry that cannot be read while a corpus has
    failed to build: its roots are unknown, so the view is marked unconfirmed and the write is refused for that reason.
    """
    if store is not None:
        view = current_view(store)
        served, _pending, failed, _no_roots = _view_look(view, store)
        with _rv.use(view):
            roots = {p.corpus_id: [_root_real(r) for r in (p.root_paths or [])]
                     for p in served}
            # a corpus that failed to build is in no partition, but its root is still a root: a write under it must not read as
            # outside every root ([PLAN-BR] x-build-traps 2). Its declared roots come from the registry, resolved from the reading.
            if failed and roots:
                specs = _view_specs(view)              # a registry that will not read raises: the pinned registry rule, the write lands flagged (never a stale answer)
                for cid in failed:
                    if cid not in roots and cid in specs:
                        roots[cid] = sorted(dict.fromkeys(declared_root_paths(specs[cid])))
        if roots:
            return roots, "store"
    # ⛔ NOT wrapped in `except Exception`. This used to swallow EVERY failure of the registry read and
    # return `({}, "none")`, so a broken registry made the root-membership warning go quiet -- the silent
    # fallback this lookup exists to remove. A `RegistryError` (`registry-not-found`) now PROPAGATES;
    # a write receipt that must still be delivered catches it explicitly (`authoring._annotate`).
    from . import config
    declared: dict[str, list[str]] = {}
    specs = config.default_corpora() or {}
    with _rv.use(current_view(store)):
        for cid, spec in specs.items():
            paths = declared_root_paths(spec)
            declared[cid] = sorted(dict.fromkeys(paths))
    if declared:
        return declared, "config"
    return {}, "none"


def check(path: str, store: object = None) -> dict[str, Any]:
    """Root-membership verdict for one authored path.

    Keys: `inside_corpus` (bool), `corpora` (ids containing it), `roots_source`,
    `roots_checked` ({corpus: [roots]}), and `warning` (None when it is inside one).
    """
    view = current_view(store)
    real = _norm(path)                               # the FILE is resolved as it always was; the roots are not
    cached = view.verdicts.get(real)
    if cached is not None:                           # a write evaluates membership ONCE: record, edge context and receipt read this result
        return copy.deepcopy(cached)
    failed = view.failures.get(real)
    if failed is not None:                           # ... and a registry-stage failure is THAT result too: no second evaluation in the same write
        raise failed
    view.resolved_at = time.monotonic()              # a confirm's answer is usable only if its job STARTED after this
    with _rv.use(view):
        if _look_failed(view):
            # the STORE'S look failed (noted on the view by `snapshot`): which corpora are seated is not known, so membership is UNCONFIRMED and the write is
            # refused ([MECH-BR-WRITES] D) -- never "no corpus here", and never the registry stage's rule
            roots, source = {}, "none"
        else:
            try:
                if getattr(view, "unlocated", None):
                    # [MECH-BR-BOOT] x-load-error (UNTRUSTED ROOTS): the registry cannot state where one of its entries lives (the shape judge refuses it, though boot went on), so
                    # where this file lives cannot be stated either: it lands flagged and is attributed to nothing, never "outside every root"
                    raise RuntimeError("the registry cannot state the location of one of its entries")
                roots, source = registered_roots(store)
                _untrusted_entry_guard(real, view, store)
            except Exception as exc:
                # the REGISTRY stage, the pinned rule (MECH-BR-WRITES D, ruled 2026-11-03): a RegistryError, or a failure inside the roots lookup, is the ONE
                # result of this write's membership evaluation. `authoring.unconfirmed_refusal` leaves it to its loud rule: the write lands under today's
                # key, flagged (`corpora_error`), with no membership_partial; every later read in this write meets the same failure.
                exc._registry_stage = True
                view.failures[real] = exc
                raise
    with _rv.use(view):                              # a root is normalised from the reading, not from the disk
        hits = sorted(cid for cid, paths in roots.items()
                      if any(_contains_real(_root_real(r), real) for r in paths))
        if hits or getattr(view, "typed", None):
            try:
                hits = _confirmed_hits(view, store, real, hits)
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "_registry_read", False):
                    exc._registry_stage = True       # the registry's own read failed: the same registry-stage result as the first stage (the write lands flagged)
                    view.failures[real] = exc
                    raise
                view.note_unconfirmed("<the confirm stage failed>")      # FAIL CLOSED: the confirm stage failed, so which corpora hold this file is not known and the write is refused
    out: dict[str, Any] = {
        "path": real,
        "inside_corpus": bool(hits),
        "corpora": hits,
        "roots_source": source,
        "roots_checked": {cid: list(paths) for cid, paths in sorted(roots.items())},
        "warning": None,
    }
    if view.partial:
        # a root is marked (or a candidate went unconfirmed, which refuses the write): the corpora named here may be incomplete (spec x-healthy-file), and "outside every root"
        # is NOT said, since it cannot be known
        out["membership_partial"] = view.partial_fields()
        if view.unconfirmed_holding:
            out["unconfirmed_holding"] = True
        if not hits:
            out["inside_corpus"] = None
        view.verdicts[real] = copy.deepcopy(out)
        return out
    if hits:
        view.verdicts[real] = copy.deepcopy(out)
        return out
    if source == "none":
        out["warning"] = (
            "⚠ ROOT MEMBERSHIP UNVERIFIABLE — no corpus roots are registered in this "
            "process (no store seated and no corpus registry resolved), so the engine "
            "cannot say whether this file will ever be indexed. The write landed.")
        view.verdicts[real] = copy.deepcopy(out)
        return out
    listed = ", ".join(f"{cid} -> {', '.join(paths) or '(no roots)'}"
                       for cid, paths in sorted(roots.items()))
    out["warning"] = (
        f"⚠ THIS FILE IS OUTSIDE EVERY REGISTERED CORPUS ROOT. The write LANDED and the "
        f"file is valid, but no query will ever return it: `lts_find` searches indexed "
        f"corpora, and nothing indexes this path. Registered roots ({source}): {listed}. "
        f"Either move the file under a root, or register its directory as a corpus root. "
        f"An 802-line specification sat outside every root for a day, perfectly linted "
        f"and unreachable by every query — this warning is that class.")
    view.verdicts[real] = copy.deepcopy(out)
    return out


def annotate(receipt: dict[str, Any], path: str, store: object = None) -> dict[str, Any]:
    """Merge a membership verdict into a write receipt, in place, and append the warning
    to the receipt's own `warnings` list so it travels with the fields callers read."""
    verdict = check(path, store=store)
    receipt["root_membership"] = verdict
    if verdict.get("membership_partial"):
        receipt["membership_partial"] = dict(verdict["membership_partial"])
    if verdict["warning"]:
        receipt["warnings"] = list(receipt.get("warnings") or []) + [verdict["warning"]]
        if isinstance(receipt.get("report"), str):
            receipt["report"] += f"\n\nROOT MEMBERSHIP:\n  {verdict['warning']}"
    return receipt


__all__ = ["check", "annotate", "registered_roots", "snapshot", "current_view"]
