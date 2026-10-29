"""[LEC-SELF-HEAL] — stale is a TRANSIENT state, not a terminal one.

============================================================================
WHAT PHASE 2 BOUGHT, AND WHAT IT DID NOT
============================================================================
[LEC-FRESHNESS] bought the CORRECTNESS half: on a content change the maintained root
moves, `Partition.freshness()` reports stale in O(1), and every gated tool returns
`success=False, stale=True [INV-LTS-FRESH]` — a loud refusal, never a wrong answer.

It did NOT buy the AVAILABILITY half. Nothing ACTED on the stale signal, so the index was
never rebuilt and the refusal was permanent until the process restarted. Every
`decision_add` therefore blinded every subsequent query against that corpus: writing a
decision blinded the writer to the graph it had just written. Refuse-only is safe and
useless.

============================================================================
THE DESIGN — LAZY, SURGICAL, SYNCHRONOUS
============================================================================
On the QUERY path, when the freshness gate returns stale:

    1. re-index (see `incremental.py`: re-parse ONLY the files whose content hash moved),
    2. publish the new content onto the SAME Partition object, `files_hash` written LAST,
    3. re-verify, and only then serve.

Deliberately SYNCHRONOUS and on the correctness path. Watcher-triggered background
pre-warm is explicitly OUT of scope ([LEC-SELF-HEAL] boundary) — it is a latency
optimization, and this is the one component whose whole job is "never lie".

============================================================================
THE FOUR INVARIANTS, AND WHERE EACH IS ENFORCED
============================================================================
1. NEVER SERVE STALE.  The heal never returns "ok" on its own say-so. It publishes
   `part.files_hash` LAST (after nodes/layers), and the CALLER (`tools.LtsEngine._gate`)
   re-runs the ordinary freshness gate afterwards. During the rebuild window the verdict
   is still stale, so the refusal stands. A concurrent reader can at worst observe
   (new nodes, OLD files_hash) -> verdict STALE -> refusal. It can never observe
   (old nodes, NEW files_hash), which is the only ordering that could serve stale.

2. FAIL CLOSED.  Every failure mode — unreadable root, corrupt/unparseable corpus, a file
   rewritten mid-re-index (`CorpusMoved`), a post-rebuild verdict that is still stale, no
   rebuild recipe at all — leaves the partition UNTOUCHED (nothing is published) and
   returns a `HealOutcome` that the gate turns into the same loud stale marker, now
   carrying a `heal` diagnostic block. Nothing propagates out of `heal()`; the server
   cannot be killed by a bad corpus.

3. NO REBUILD STORMS.  Two mechanisms, both in this module:
     * COALESCING  — one `threading.RLock` per corpus_id. Concurrent callers queue on it;
       every one of them re-checks freshness *under the lock* before rebuilding, so the
       winner's rebuild satisfies all of them and the losers do zero work.
     * ATTEMPT KEYING — `_last_attempt[corpus_id]` records the LIVE ROOT HASH that a
       rebuild was attempted against. A heal request observing that same root again is
       SUPPRESSED without rebuilding. Since a successful rebuild makes the partition
       fresh (so the gate never calls heal at all), the net rule is exactly: AT MOST ONE
       REBUILD PER DETECTED ROOT CHANGE. A corpus being written continuously therefore
       costs at most one rebuild per distinct content state, never a stampede per call.
     * (optional) `min_interval` adds a wall-clock floor between attempts for the same
       corpus. Default 0.0 — the attempt key already bounds the work; this is a knob for
       a pathological writer, and refusing during the cooldown is fail-closed-safe.

4. DETERMINISM.  The rebuild runs through the SAME `CorpusIndex` the cold build used, and
   `incremental.RootIndex.assemble()` recomputes every cross-file property (node map, id
   collisions, alias index, dangling flags, layers, stats) in full from the per-file parse
   cache. Only the PARSE is incremental. See `incremental.py`'s determinism argument.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

from .incremental import CorpusIndex, CorpusMoved
from .kernel import combine_leaf_map, combine_roots

logger = logging.getLogger(__name__)

# Environment knobs (deployment-tunable; both default to "off / no extra restriction").
_ENV_DISABLE = "FACTLATTICE_SELF_HEAL"          # "0"/"false" disables healing entirely
_ENV_MIN_INTERVAL = "FACTLATTICE_HEAL_MIN_INTERVAL"
_ENV_FORCE_FULL = "FACTLATTICE_HEAL_FULL"       # "1"/"true" forces the full re-index
# "0"/"false" reverts the gate to phase-2's pure O(1) compare against the maintained root,
# accepting a stale answer for up to one watcher poll interval. See
# `verdict.PollVerifiedFreshness` for why the default is ON.
_ENV_VERIFY_ON_QUERY = "FACTLATTICE_VERIFY_ON_QUERY"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# ⛔ [TOOL-REASON-IS-A-FIXED-CODE] the CLOSED set of `HealOutcome.reason` sentences: constants, never built from a hash, a count,
# a path or an exception message (those are HealOutcome's own fields: `reparsed`, `mode`, `error`). tests/test_reason_sites.py
# scans this file and fails on any `reason=` that is not one of these literals.
HEAL_REASONS = (
    "self-heal raised; refusing (fail-closed)",
    "already fresh (coalesced onto a prior rebuild)",
    "no rebuild recipe on this partition (cannot self-heal; refusing) [INV-LTS-FRESH]",
    "partition has no readable roots to re-index",
    "authoritative re-verify says fresh; no rebuild needed",
    "rebuild cooldown active; refusing this call",
    "corpus changed during the re-index; refusing (fail-closed) — retry after it settles",
    "re-index failed; the previous index was NOT published and is NOT served (fail-closed)",
    "re-index produced no readable roots; refusing",
    "corpus moved during the re-index; refusing (fail-closed) — retry after it settles",
    "corpus changed while the re-index ran; refusing (fail-closed) — retry after it settles",
    "post-rebuild verdict is STILL stale; refusing",
    "rebuild already attempted for this content; suppressed to avoid a rebuild storm",
    "re-indexed; fresh",
)

HEAL_REASON_CODES = dict(zip(HEAL_REASONS, (
    "self-heal-raised",
    "already-fresh",
    "no-rebuild-recipe",
    "no-readable-roots",
    "reverify-fresh",
    "rebuild-cooldown",
    "corpus-changed-during-reindex",
    "reindex-failed",
    "reindex-no-readable-roots",
    "corpus-moved-during-reindex",
    "corpus-changed-while-reindexing",
    "still-stale-after-rebuild",
    "rebuild-already-attempted",
    "reindexed-fresh",
)))


@dataclass(frozen=True)
class HealOutcome:
    """The observable result of one heal request. Returned to the gate, which folds the
    failure cases into the stale marker so a refusal always explains itself."""
    corpus_id: str
    attempted: bool                 # did we actually run a rebuild?
    healed: bool                    # is the partition fresh as a result?
    mode: Optional[str] = None      # "incremental" | "full" | None
    reparsed: int = 0               # files re-parsed by this rebuild
    reason: Optional[str] = None    # why we did / did not act
    error: Optional[str] = None     # the fail-closed error, if any

    def to_dict(self) -> dict:
        # `reason` is a CODE (a failed heal is a FAULT block); the fixed sentence is `explanation`. Both are closed sets.
        code = HEAL_REASON_CODES.get(self.reason, "heal-unspecified") if self.reason is not None else None
        return {"attempted": self.attempted, "healed": self.healed, "mode": self.mode,
                "reparsed": self.reparsed, "reason": code, "explanation": self.reason, "error": self.error}


class SelfHealer:
    """Rebuilds a stale partition IN PLACE, surgically, and never lets a caller serve
    the result until the ordinary freshness gate agrees.

    Counters (all asserted by `tests/test_self_heal.py`):
        `rebuilds`   — rebuild executions (the storm-guard metric)
        `suppressed` — heal requests denied by the attempt key / cooldown
        `failures`   — rebuilds that ended fail-closed
        `healed`     — rebuilds that ended fresh
    """

    def __init__(self, store, *, kernel=None, freshness_provider=None,
                 min_interval: Optional[float] = None, clock=time.monotonic,
                 force_full: Optional[bool] = None) -> None:
        self.store = store
        self._explicit_kernel = kernel
        if freshness_provider is None:
            from .verdict import ContentHashFreshness
            freshness_provider = ContentHashFreshness()
        self.freshness_provider = freshness_provider
        if min_interval is None:
            try:
                min_interval = float(os.environ.get(_ENV_MIN_INTERVAL, "0") or 0.0)
            except ValueError:
                min_interval = 0.0
        self.min_interval = max(0.0, min_interval)
        self.force_full = _env_flag(_ENV_FORCE_FULL, False) if force_full is None \
            else bool(force_full)
        self._clock = clock
        self._locks: Dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._last_attempt: Dict[str, str] = {}     # corpus_id -> live root attempted
        self._last_attempt_at: Dict[str, float] = {}
        self.rebuilds = 0
        self.suppressed = 0
        self.failures = 0
        self.healed = 0

    # --- plumbing -----------------------------------------------------------
    @property
    def kernel(self):
        """The freshness kernel to consult: an explicitly injected one, else the store's
        (seated by `server.build_tools`). None is tolerated — the heal then hashes the
        roots itself, which is correct but not O(0)."""
        return self._explicit_kernel if self._explicit_kernel is not None \
            else getattr(self.store, "kernel", None)

    def _lock_for(self, corpus_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(corpus_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[corpus_id] = lock
            return lock

    def forget(self, corpus_id: str) -> None:
        """Drop all per-corpus state for a corpus being runtime-unregistered
        ([REG-RUNTIME-CORPUS]): the rebuild lock and the storm-guard attempt-key memory
        (`_last_attempt` / `_last_attempt_at`). Without this, re-registering the SAME
        corpus_id later would inherit a stale attempt-key that happens to match the new
        content's live root and suppress its first heal — an unlikely but real
        cross-registration leak this closes at the source. Safe on an id that was never
        healed; never raises."""
        with self._locks_guard:
            self._locks.pop(corpus_id, None)
        self._last_attempt.pop(corpus_id, None)
        self._last_attempt_at.pop(corpus_id, None)

    # --- the content oracle -------------------------------------------------
    def _authoritative_leaves(self, part):
        """({root_path: {rel: hash}}, live_root) over the corpus's CURRENT content.

        With a kernel: `full_reverify` re-hashes every leaf unconditionally (one hash-only
        pass, NO parsing) so the change set we diff against is authoritative even if the
        watcher's mtime hint missed something. Without a kernel: hash the roots directly.
        Either way the oracle is the file CONTENT hash, never mtime/size.
        """
        kernel = self.kernel
        if kernel is not None and kernel.leaves_snapshot(part) is not None:
            kernel.full_reverify(part)
            leaves = kernel.leaves_snapshot(part)
            return leaves, kernel.current_root(part)
        from factlattice.hashing import file_hash_map
        leaves = {rp: file_hash_map(rp) for rp in (part.root_paths or [])}
        if not leaves:
            return {}, None
        return leaves, combine_roots({rp: combine_leaf_map(m) for rp, m in leaves.items()})

    # --- the public entry point --------------------------------------------
    def heal(self, part) -> HealOutcome:
        """Make a stale partition fresh, or fail closed. NEVER raises."""
        cid = getattr(part, "corpus_id", None) or "<unknown>"
        try:
            with self._lock_for(cid):
                return self._heal_locked(part, cid)
        except Exception as exc:  # noqa: BLE001 — a heal fault must never kill the server
            logger.exception("[lts-engine] self-heal fault for corpus %s", cid)
            self.failures += 1
            return HealOutcome(cid, attempted=True, healed=False,
                               error=f"{type(exc).__name__}: {exc}",
                               reason="self-heal raised; refusing (fail-closed)")

    def _heal_locked(self, part, cid: str) -> HealOutcome:
        # (3) COALESCE: re-check under the lock. Callers that queued behind a winning
        # rebuild find the partition already fresh and do NO work at all.
        fr = self.freshness_provider.evaluate(part)
        if fr.fresh:
            return HealOutcome(cid, attempted=False, healed=True,
                               reason="already fresh (coalesced onto a prior rebuild)")

        ci: Optional[CorpusIndex] = getattr(part, "corpus_index", None)
        if ci is None:
            return HealOutcome(cid, attempted=False, healed=False,
                               reason="no rebuild recipe on this partition "
                                      "(cannot self-heal; refusing) [INV-LTS-FRESH]")

        leaves, live_root = self._authoritative_leaves(part)
        if live_root is None:
            return HealOutcome(cid, attempted=False, healed=False,
                               reason="partition has no readable roots to re-index")
        if live_root == part.files_hash:
            # The hint-based verdict was pessimistic; the authoritative pass says fresh.
            return HealOutcome(cid, attempted=False, healed=True,
                               reason="authoritative re-verify says fresh; no rebuild needed")

        # (3) STORM GUARD: at most ONE rebuild per detected root change.
        if self._last_attempt.get(cid) == live_root:
            self.suppressed += 1
            return HealOutcome(cid, attempted=False, healed=False,
                               reason="rebuild already attempted for this content; "
                                      "suppressed to avoid a rebuild storm")
        if self.min_interval:
            last = self._last_attempt_at.get(cid)
            if last is not None and (self._clock() - last) < self.min_interval:
                self.suppressed += 1
                return HealOutcome(cid, attempted=False, healed=False,
                                   reason="rebuild cooldown active; refusing this call")

        self._last_attempt[cid] = live_root
        self._last_attempt_at[cid] = self._clock()
        self.rebuilds += 1
        try:
            outcome = self._rebuild(part, ci, leaves, live_root, cid)
        except CorpusMoved as moved:
            self.failures += 1
            return HealOutcome(cid, attempted=True, healed=False,
                               error=str(moved),
                               reason="corpus changed during the re-index; refusing "
                                      "(fail-closed) — retry after it settles")
        except Exception as exc:  # noqa: BLE001 — (2) FAIL CLOSED
            logger.exception("[lts-engine] re-index failed for corpus %s", cid)
            self.failures += 1
            return HealOutcome(cid, attempted=True, healed=False,
                               error=f"{type(exc).__name__}: {exc}",
                               reason="re-index failed; the previous index was NOT "
                                      "published and is NOT served (fail-closed)")
        if outcome.healed:
            self.healed += 1
            self._last_attempt.pop(cid, None)   # a fresh corpus needs no suppression
        else:
            self.failures += 1
        return outcome

    # --- the rebuild --------------------------------------------------------
    def _rebuild(self, part, ci: CorpusIndex, leaves, live_root: str,
                 cid: str) -> HealOutcome:
        kernel = self.kernel
        seated_roots = set(part.root_paths or [])

        # --- re-index: surgical when we have a seeded cache, full otherwise ---
        if self.force_full or not ci.seeded:
            nodes, layers, root_paths, index_hash = ci.cold_build()
            mode = "full"
            reparsed = ci.cached_file_count
        else:
            nodes, layers, root_paths, index_hash, touched = ci.incremental_build(leaves)
            mode = "incremental"
            reparsed = len(touched)

        if not root_paths:
            return HealOutcome(cid, attempted=True, healed=False, mode=mode,
                               reparsed=reparsed,
                               reason="re-index produced no readable roots; refusing")

        # --- (1) NEVER SERVE STALE: the index must match live content ---------
        # `index_hash` is derived from the parse cache's OWN certified leaves — the hash of
        # the exact bytes each cached parse was taken from. If it disagrees with the
        # authoritative live root, some file moved between the content pass and the parse:
        # refuse rather than publish an index that would claim a hash it does not hold.
        structural = set(root_paths) != seated_roots
        if structural:
            # The ROOT SET changed (a registered-but-empty root appeared / a root vanished),
            # so the seated oracle covers the wrong file set. Re-seat from the new roots and
            # take that as the live truth.
            part.root_paths = list(root_paths)
            if isinstance(part.index.get("source_corpus"), dict):
                part.index["source_corpus"]["root_paths"] = list(root_paths)
            if kernel is not None:
                live_root = kernel.seat_partition(part) or live_root
            else:
                _lv, relive = self._authoritative_leaves(part)
                if relive is not None:
                    live_root = relive
        if index_hash != live_root:
            return HealOutcome(cid, attempted=True, healed=False, mode=mode,
                               reparsed=reparsed,
                               error=f"rebuilt index hash {index_hash} != live content "
                                     f"hash {live_root}",
                               reason="corpus moved during the re-index; refusing "
                                      "(fail-closed) — retry after it settles")
        # Second, cheap (stat-only) confirmation that nothing changed while we parsed.
        if kernel is not None and not structural and kernel.poll_once(part):
            return HealOutcome(cid, attempted=True, healed=False, mode=mode,
                               reparsed=reparsed,
                               reason="corpus changed while the re-index ran; refusing "
                                      "(fail-closed) — retry after it settles")

        self._publish(part, nodes, layers, root_paths, index_hash)
        if kernel is not None:
            # The kernel's leaves ARE authoritative for the content we just indexed, so
            # rebind rather than re-seat: no re-hash, and the very next query is O(0).
            kernel.rebind_partition(part)

        fr = self.freshness_provider.evaluate(part)
        if not fr.fresh:
            return HealOutcome(cid, attempted=True, healed=False, mode=mode,
                               reparsed=reparsed,
                               reason="post-rebuild verdict is STILL stale; refusing")
        return HealOutcome(cid, attempted=True, healed=True, mode=mode,
                           reparsed=reparsed,
                           reason="re-indexed; fresh")

    @staticmethod
    def _publish(part, nodes, layers, root_paths, index_hash: str) -> None:
        """Swap the rebuilt content onto the live Partition, `files_hash` LAST.

        ORDERING IS THE INVARIANT (see invariant 1 in the module docstring). A concurrent
        reader that interleaves here sees either the old index with the old hash (fresh
        verdict against the old content is impossible — the root already moved, so it
        refuses), or the new index with the old hash (refuses), or the new index with the
        new hash (serves, correctly). The one ordering that could serve stale — new hash
        over old content — cannot occur.
        """
        stats = dict(part.stats or {})
        stats["total_nodes"] = len(nodes)
        stats["total_edges"] = sum(len(ld.get("edges", ())) for ld in layers.values())
        for derived in ("dangling_edges", "unresolved_target_edges", "edges_by_layer",
                        "id_collisions", "parse_errors", "unregistered_ns_edges"):
            stats.pop(derived, None)

        part.nodes = nodes
        part.layers = layers
        part.stats = stats
        part.root_paths = list(root_paths)
        # Freshness-keyed caches ([LEC-FRESHNESS] #2) — drop them explicitly rather than
        # relying on the key compare, so no walk can ever see new nodes over old adjacency.
        part._adj_cache = None
        part._adj_key = None
        part._deg_cache = None
        part._deg_key = None
        idx = part.index if isinstance(part.index, dict) else None
        if idx is not None:
            idx["nodes"] = nodes
            idx["layers"] = layers
            idx["stats"] = stats
            sc = idx.get("source_corpus")
            if isinstance(sc, dict):
                sc["root_paths"] = list(root_paths)
                sc["files_hash"] = index_hash
        part.files_hash = index_hash            # <-- the publish. LAST. Always last.


__all__ = ["HealOutcome", "SelfHealer"]
