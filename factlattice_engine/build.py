"""CLI: build the lts-engine graph index for a corpus.

    python -m factlattice_engine.build --corpus <root_dir> --out <index_path> \
        [--corpus-id X --domain-suffix Y]

Writes graph_index.json atomically; idempotent (built_at derived from the corpus
content hash unless --built-at). Delegates all real work to the parser package
(`factlattice.build_index`).
"""

from __future__ import annotations

import argparse
import sys

from factlattice import build_index, compute_snapshot_hash, files_hash, write_index_atomic


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lts-build", description=__doc__)
    ap.add_argument("--corpus", required=True, help="corpus root dir (tree of .lts)")
    ap.add_argument("--out", required=True, help="output graph_index.json path")
    ap.add_argument("--corpus-id", default="corpus")
    ap.add_argument("--domain-suffix", default="proj.corpus")
    ap.add_argument("--built-at", default=None,
                    help="build tag; default = content-derived (reproducible)")
    args = ap.parse_args(argv)

    try:
        built_at = args.built_at
        if built_at is None:
            fhash, _ = files_hash(args.corpus)
            built_at = f"content:{fhash}"
        index = build_index(corpus_root=args.corpus, corpus_id=args.corpus_id,
                            domain_suffix=args.domain_suffix, built_at=built_at)
        index["source_corpus"]["snapshot_hash"] = compute_snapshot_hash(index)
        write_index_atomic(index, args.out)
    except Exception as exc:  # noqa: BLE001
        print(f"lts-build: FAILED: {exc!r}", file=sys.stderr)
        return 1

    sc, st = index["source_corpus"], index["stats"]
    print(f"lts-build: OK corpus={args.corpus_id} out={args.out} "
          f"nodes={st['total_nodes']} edges={st['total_edges']} "
          f"dangling={st['dangling_edges']} files_hash={sc['files_hash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
