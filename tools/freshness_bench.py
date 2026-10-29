"""[LEC-FRESHNESS] + [LEC-SELF-HEAL] proof harness — measured, not timed.

Generates a synthetic corpus of N `.lts` files, then runs the SAME query workload twice
over it:

    BASELINE  — no freshness kernel. Every query calls `Partition.freshness()`, which
                re-hashes the whole corpus (`combined_files_hash` -> `files_hash`).
    KERNEL    — an incremental `FreshnessKernel` seated once at startup. Every query is
                an O(1) compare against the maintained root.

The headline metric is a COUNT, not a clock: how many per-file content hashes (and how
many corpus bytes read) the QUERY PATH performs on an UNCHANGED corpus. Wall time is
reported alongside, but the count is the claim.

[LEC-SELF-HEAL] adds three more sections, each with its own count-based claim:

    LAZY REBUILD    — the same 1-file edit, with the healer wired: the refusal becomes
                      TRANSIENT and the re-index costs ONE parse, not N (the parse-side
                      analogue of "1 leaf re-hashed, not 200").
    O(0) AFTER HEAL — phase-2's zero-hash query path survives the heal.
    VERIFY-ON-QUERY — the price of never serving a write that landed between watcher
                      polls: stats, never content hashes (so the O(0) claim is intact).

    python -m tools.freshness_bench [--files N] [--queries Q] [--kb K]
"""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import time

import factlattice.hashing as _hashing
from factlattice_engine import (
    FreshnessKernel, LtsEngine, LtsStore, PollVerifiedFreshness, SelfHealer,
)
from factlattice_engine.config import build_merged_partition, build_partition

_REAL_FILE_HASH = _hashing.file_content_hash


class Meter:
    """Counts per-file content hashes + bytes read on whatever path invokes them."""

    def __init__(self) -> None:
        self.hashes = 0
        self.bytes = 0

    def install(self) -> None:
        def counting(path: str) -> str:
            self.hashes += 1
            try:
                self.bytes += os.path.getsize(path)
            except OSError:
                pass
            return _REAL_FILE_HASH(path)

        _hashing.file_content_hash = counting

    @staticmethod
    def uninstall() -> None:
        _hashing.file_content_hash = _REAL_FILE_HASH

    def reset(self) -> None:
        self.hashes = 0
        self.bytes = 0


def make_corpus(root: str, n_files: int, kb_per_file: int) -> None:
    os.makedirs(root, exist_ok=True)
    # padding so each file is ~kb_per_file KB — this is the corpus-bytes tax being removed
    pad_line = "- note: " + ("x" * 100) + "\n"
    pad = pad_line * max(1, (kb_per_file * 1024) // len(pad_line))
    for i in range(n_files):
        body = (
            "---\ndomain: core.bench\n---\n"
            f"# Bench {i} [SPEC:BEN-{i}-DOC]\n\n"
            f"### [BEN-{i}] Node {i}\n"
            f"- definition: benchmark node {i}\n"
            "- status: operational\n"
            f"{pad}"
        )
        if i:
            body += ("\n## Relationships\n\n### Semantic\n"
                     f"- [BEN-{i}] DEPENDS_ON [BEN-{i - 1}]\n")
        with open(os.path.join(root, f"n{i}.lts"), "w", encoding="utf-8") as fh:
            fh.write(body)


def run_workload_named(engine: LtsEngine, corpus: str, queries: int) -> None:
    for i in range(queries):
        engine.find(corpus=corpus, prefix="BEN-")
        engine.node(corpus=corpus, node_id=f"BEN-{i % 5}")
        engine.traverse(corpus=corpus, start_node=f"BEN-{i % 5}", max_depth=3)


def run_workload(engine: LtsEngine, queries: int) -> None:
    run_workload_named(engine, "bench", queries)


def _engine(part) -> LtsEngine:
    store = LtsStore()
    store.add_partition(part)
    return LtsEngine(store)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="freshness-bench", description=__doc__)
    ap.add_argument("--files", type=int, default=200)
    ap.add_argument("--queries", type=int, default=50)
    ap.add_argument("--kb", type=int, default=8, help="approx KB per .lts file")
    args = ap.parse_args(argv)

    tmp = tempfile.mkdtemp(prefix="lts-bench-")
    root = os.path.join(tmp, "corpus")
    try:
        make_corpus(root, args.files, args.kb)
        total_bytes = sum(os.path.getsize(os.path.join(root, f))
                          for f in os.listdir(root))
        print(f"corpus: {args.files} .lts files, {total_bytes / 1024:.0f} KB total; "
              f"{args.queries} query rounds (3 tool calls each)\n")

        meter = Meter()
        meter.install()
        try:
            # --- BASELINE: no kernel, full re-hash per query -------------------
            part = build_partition("bench", root, "proj.bench")
            eng = _engine(part)
            meter.reset()
            t0 = time.perf_counter()
            run_workload(eng, args.queries)
            base_t = time.perf_counter() - t0
            base_hashes, base_bytes = meter.hashes, meter.bytes

            # --- KERNEL: seat once, then O(1) compares -------------------------
            part2 = build_partition("bench", root, "proj.bench")
            kernel = FreshnessKernel()
            meter.reset()
            t_seat0 = time.perf_counter()
            kernel.seat_partition(part2)
            seat_t = time.perf_counter() - t_seat0
            seat_hashes = kernel.hash_ops
            eng2 = _engine(part2)

            meter.reset()
            kernel.hash_ops = 0
            t1 = time.perf_counter()
            run_workload(eng2, args.queries)
            kern_t = time.perf_counter() - t1
            kern_hashes = meter.hashes + kernel.hash_ops
            kern_bytes = meter.bytes
        finally:
            meter.uninstall()

        print("QUERY-PATH VALIDATION COST (unchanged corpus)")
        print(f"  baseline (full re-hash / query) : {base_hashes:>8} file-hashes  "
              f"{base_bytes / 1024 / 1024:>8.1f} MB read  {base_t * 1000:>8.1f} ms")
        print(f"  kernel   (maintained root)      : {kern_hashes:>8} file-hashes  "
              f"{kern_bytes / 1024 / 1024:>8.1f} MB read  {kern_t * 1000:>8.1f} ms")
        print(f"\n  one-time SEAT cost              : {seat_hashes:>8} file-hashes  "
              f"{seat_t * 1000:>8.1f} ms  (startup/reconnect only)")

        print("\nRESULT")
        print(f"  per-query file-hashes: {base_hashes / args.queries:.1f} -> "
              f"{kern_hashes / args.queries:.1f}")
        print(f"  O(0) claim holds: {kern_hashes == 0}")
        if base_t > 0 and kern_t > 0:
            print(f"  wall-clock speedup on the query path: {base_t / kern_t:.1f}x")

        # --- and the change is still caught, incrementally --------------------
        with open(os.path.join(root, "n7.lts"), "a", encoding="utf-8") as fh:
            fh.write("\n- amended: yes\n")
        kernel.hash_ops = 0
        changed = kernel.poll_once(part2)
        print(f"\nINCREMENTAL CHANGE DETECTION")
        print(f"  edited 1 of {args.files} files -> root moved: {changed}; "
              f"leaves re-hashed: {kernel.hash_ops} (not {args.files})")
        res = eng2.find(corpus="bench", prefix="BEN-")
        print(f"  next query verdict: success={res['success']} "
              f"stale={res.get('stale')} [INV-LTS-FRESH]")

        # --- [LEC-SELF-HEAL]: and the refusal is TRANSIENT --------------------
        # Same corpus, same edit, but with the healer wired (the `build_tools` shape).
        # The headline here is the PARSE count: a 1-file edit must cost 1 parse, not N.
        part3 = build_merged_partition("bench2", [{"path": root, "area": "specs"}],
                                       "proj.bench2")
        store3 = LtsStore()
        store3.add_partition(part3)
        kernel3 = FreshnessKernel()
        kernel3.seat_store(store3)
        store3.kernel = kernel3
        healer = SelfHealer(store3, kernel=kernel3)
        eng3 = LtsEngine(store3, healer=healer)
        eng3.find(corpus="bench2", prefix="BEN-")

        with open(os.path.join(root, "n11.lts"), "a", encoding="utf-8") as fh:
            fh.write("\n- amended: yes\n")
        kernel3.poll_once(part3)
        part3.corpus_index.reset_parse_ops()
        meter.install()
        meter.reset()
        kernel3.hash_ops = 0
        t2 = time.perf_counter()
        healed = eng3.find(corpus="bench2", prefix="BEN-")
        heal_t = time.perf_counter() - t2
        meter.uninstall()
        print("\n[LEC-SELF-HEAL] LAZY REBUILD ON DEMAND (same 1-file edit)")
        print(f"  next query verdict: success={healed['success']} "
              f"stale={healed.get('stale')}  (the refusal is TRANSIENT)")
        print(f"  files RE-PARSED    : {part3.corpus_index.parse_ops:>8}  "
              f"(corpus has {args.files})")
        print(f"  heal wall time     : {heal_t * 1000:>8.1f} ms  "
              f"(one authoritative content pass + 1 parse)")

        meter.install()
        meter.reset()
        kernel3.hash_ops = 0
        t3 = time.perf_counter()
        run_workload_named(eng3, "bench2", args.queries)
        post_t = time.perf_counter() - t3
        meter.uninstall()
        print(f"  O(0) AFTER the heal: {meter.hashes + kernel3.hash_ops:>8} file-hashes "
              f"over {args.queries} query rounds  "
              f"(claim holds: {meter.hashes + kernel3.hash_ops == 0})")

        # --- the price of never serving a write that landed between polls ------
        # `PollVerifiedFreshness` runs the kernel's stat-only HINT pass before answering,
        # so a write is visible to the VERY NEXT query rather than up to a poll interval
        # later. It costs stats, never content hashes — the O(0) metric above is measured
        # in hashes and bytes, and stays zero.
        eng4 = LtsEngine(store3, healer=healer,
                         freshness_provider=PollVerifiedFreshness())
        meter.install()
        meter.reset()
        kernel3.hash_ops = 0
        t4 = time.perf_counter()
        run_workload_named(eng4, "bench2", args.queries)
        verify_t = time.perf_counter() - t4
        meter.uninstall()
        print(f"\nVERIFY-ON-QUERY (the server default; closes the watch-lag window)")
        print(f"  maintained-root gate  : {post_t * 1000:>8.1f} ms  "
              f"{0:>5} content hashes")
        print(f"  + stat-only hint pass : {verify_t * 1000:>8.1f} ms  "
              f"{meter.hashes + kernel3.hash_ops:>5} content hashes  "
              f"({(verify_t - post_t) * 1000 / max(1, args.queries * 3):.3f} ms/call "
              f"over {args.files} files)")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
