"""CLI: freshness-check a lts-engine graph index (content hashing only, never mtime/size).

    python -m factlattice_engine.freshness --corpus <root_dir> --index <index_path>

The [INV-LTS-FRESH] hash-equality kernel + a machine-readable verdict line. No
watchdog / verdict-store coupling.

    exit 0 = FRESH   exit 3 = STALE   exit 2 = ERROR (unreadable / empty source)

SCOPE NOTE ([LEC-FRESHNESS]): this CLI stays a deliberate ONE-SHOT full re-hash. A
single invocation has no prior state to update incrementally — the full hash IS its
seat. The incremental kernel (`factlattice_engine.kernel.FreshnessKernel`) is for the
LONG-RUNNING MCP server, where the root is seated once and then maintained, so a query
costs an O(1) compare instead of an O(corpus-bytes) re-read. Both compute the SAME
content-hash oracle, so their verdicts always agree.
"""

from __future__ import annotations

import argparse
import json
import sys

from factlattice import CorpusFileError, files_hash

EXIT_FRESH, EXIT_STALE, EXIT_ERROR = 0, 3, 2


def _emit(verdict, *, mirror_hash=None, authoritative_hash=None, reason=None, path=None):
    rec = {"verdict": verdict, "mirror_hash": mirror_hash,
           "authoritative_hash": authoritative_hash}
    if reason is not None:
        rec["reason"] = reason
    if path is not None:
        rec["path"] = path                    # ROOT-RELATIVE (a corpus-file-* refusal)
    print(json.dumps(rec))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lts-freshness", description=__doc__)
    ap.add_argument("--corpus", required=True, help="corpus root dir (tree of .lts)")
    ap.add_argument("--index", required=True, help="graph_index.json path to check")
    args = ap.parse_args(argv)

    index_hash = None
    try:
        with open(args.index, "r", encoding="utf-8") as fh:
            index = json.load(fh)
        index_hash = index.get("source_corpus", {}).get("files_hash")
        if not index_hash:
            _emit("error", reason="index missing source_corpus.files_hash")
            return EXIT_ERROR
        live_hash, fmap = files_hash(args.corpus)
    except CorpusFileError as exc:
        # a FIFO/socket/device, broken link or unreadable member: the precise code and the ROOT-RELATIVE path, no absolute path
        _emit("error", mirror_hash=index_hash, reason=exc.code, path=exc.rel_path)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001
        _emit("error", mirror_hash=index_hash, reason=repr(exc))
        return EXIT_ERROR

    if not fmap:
        _emit("error", mirror_hash=index_hash, authoritative_hash=live_hash,
              reason="authoritative source is empty (no .lts under --corpus)")
        return EXIT_ERROR

    fresh = (index_hash == live_hash)
    _emit("fresh" if fresh else "stale", mirror_hash=index_hash, authoritative_hash=live_hash)
    return EXIT_FRESH if fresh else EXIT_STALE


if __name__ == "__main__":
    raise SystemExit(main())
