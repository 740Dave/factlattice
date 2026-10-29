"""CLI: build the lts-engine graph index for a corpus.

    python -m factlattice.build --corpus <root> --out <index_path> \
        [--corpus-id X --domain-suffix Y]

Writes graph_index.json ATOMICALLY (temp + os.rename). Idempotent: re-running over
an unchanged corpus produces a byte-identical index (built_at is derived from the
corpus files_hash unless --built-at is supplied, keeping the build reproducible).
exit 0 on success, non-zero on failure. This is the REBUILD entrypoint
([LTS-WATCHDOG] seam).
"""

from __future__ import annotations

import argparse
import sys

from .hashing import files_hash
from .index_builder import build_index, compute_snapshot_hash, write_index_atomic


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lts-build", description=__doc__)
    ap.add_argument("--corpus", required=True, help="corpus root dir (tree of .lts)")
    ap.add_argument("--out", required=True, help="output graph_index.json path")
    ap.add_argument("--corpus-id", default="example")
    ap.add_argument("--domain-suffix", default="demo.example")
    ap.add_argument("--built-at", default=None,
                    help="ISO-8601 build timestamp (injected clock); default is a "
                         "deterministic tag derived from the corpus content hash")
    ap.add_argument("--commit-ref", default=None,
                    help="provenance placeholder (Phase-1); carried into schema")
    args = ap.parse_args(argv)

    try:
        built_at = args.built_at
        if built_at is None:
            # Deterministic, content-derived tag so builds are reproducible and the
            # CLI never bakes datetime.now() into the artifact. Real wall-clock
            # timestamps are supplied by the caller via --built-at when wanted.
            fhash, _ = files_hash(args.corpus)
            built_at = f"content:{fhash}"

        index = build_index(
            corpus_root=args.corpus,
            corpus_id=args.corpus_id,
            domain_suffix=args.domain_suffix,
            built_at=built_at,
            commit_ref=args.commit_ref,
        )
        # Anchor the snapshot_hash over the assembled content (SPEC §3.2).
        index["source_corpus"]["snapshot_hash"] = compute_snapshot_hash(index)
        write_index_atomic(index, args.out)
    except Exception as exc:  # noqa: BLE001
        print(f"lts-build: FAILED: {exc!r}", file=sys.stderr)
        return 1

    sc = index["source_corpus"]
    st = index["stats"]
    print(
        f"lts-build: OK corpus={args.corpus_id} out={args.out} "
        f"nodes={st['total_nodes']} edges={st['total_edges']} "
        f"dangling={st['dangling_edges']} unregistered_ns={st['unregistered_ns_edges']} "
        f"collisions={st['id_collisions']} files_hash={sc['files_hash']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
