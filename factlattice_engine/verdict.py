"""[INV-LTS-FRESH] — the content-hash freshness kernel.

============================================================================
Only `ContentHashFreshness` (~15 LOC) and the `FreshnessProvider` Protocol live here.
There is no constant-cost verdict-read machinery or watchdog liveness gate: those
existed to make freshness evaluation constant-TIME so a denied caller could not use
latency as an existence oracle — a threat model with no analogue in the single-operator,
single local MCP-client deployment this engine targets.
============================================================================
"""

from __future__ import annotations

from typing import Protocol

from .store import FreshnessResult, Partition


class FreshnessProvider(Protocol):
    """What the query path consults for a partition's freshness."""

    def evaluate(self, part: Partition) -> FreshnessResult: ...


class ContentHashFreshness:
    """Inline content-hash freshness (delegates to `Partition.freshness()`).

    Compare the live corpus content hash to the hash the index was built from. The
    content hash is the SOLE correctness oracle; how it is OBTAINED is the partition's
    business:

      * with a seated `kernel.FreshnessKernel` ([LEC-FRESHNESS], the deployment shape)
        `Partition.freshness()` is an O(1) compare against an INCREMENTALLY-maintained
        root — no per-query corpus re-hash;
      * without one it falls back to the original full `combined_files_hash` re-hash.

    Either way the verdict is a content-hash equality, never an mtime/size judgement.
    A stale index is a cheap, safe-to-defer rebuild, not a correctness emergency
    ([LTS-DERIVED-PROJECTION]) — but it is reported LOUDLY, never served ([INV-LTS-FRESH]).
    """

    def evaluate(self, part: Partition) -> FreshnessResult:
        return part.freshness()


class PollVerifiedFreshness:
    """[LEC-SELF-HEAL] close the WATCH-LAG window before answering.

    THE HOLE THIS CLOSES. With a seated kernel, `Partition.freshness()` compares the index
    hash against the kernel's MAINTAINED root — which only advances when the watcher polls
    (default every 1.0s). A write that lands between polls is therefore invisible, and the
    gate reports FRESH over PRE-WRITE content: the engine SERVES STALE, silently, with
    `success=True`. That is bounded-latency eventual consistency, which is a reasonable
    contract for a monitor and a disqualifying one for the component whose whole job is
    "never lie" — and it is precisely the window [LEC-SELF-HEAL] lives in, because the
    canonical sequence is `decision_add` immediately followed by a query IN THE SAME
    SESSION. Healing a stale index is pointless if the query never learns it is stale.

    THE FIX, AND ITS COST. Drive the kernel's own `poll_once` before evaluating. That is
    the mtime/size HINT pass: one `os.stat` per corpus file, and a CONTENT re-hash of only
    the files whose hint moved. So on an unchanged corpus the query path still performs
    ZERO per-file content hashes and reads ZERO corpus bytes — phase-2's O(0) claim, which
    is stated in hashes and bytes, is untouched — while a write becomes visible to the
    VERY NEXT query instead of up to a poll-interval later.

    mtime remains a HINT, never a verdict: `poll_once` re-hashes the hinted files and only
    moves the root if their CONTENT actually changed, and the kernel's periodic
    `full_reverify` backstop still covers hint-invisible edits. Wired at the composition
    root (`server.build_tools`), so `ContentHashFreshness` keeps its pure-O(1) semantics
    for callers who want them (`FACTLATTICE_VERIFY_ON_QUERY=0`).
    """

    def evaluate(self, part: Partition) -> FreshnessResult:
        kernel = getattr(part, "_kernel", None)
        if kernel is not None:
            try:
                kernel.poll_once(part)
            except Exception:  # noqa: BLE001 — a hint pass must never break a query;
                pass           # the maintained root simply does not advance this call
        return part.freshness()
