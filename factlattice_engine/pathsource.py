"""[A-E7 / X7 FIND-PATH-SOURCE] a ONE-FILE partition for `lts_find(path=...)`.

The file is parsed by the engine's own per-file parser (`incremental.RootIndex._parse`, the only place a
corpus file is parsed, so newline/encoding handling, profile-document exclusion and the content-hash
certificate are exactly a registered corpus's), assembled by the same `assemble()`, and seated in a
`Partition` that is NEVER added to a store, never indexed, never cached and never shared. Only the named file
is read: its siblings are not listed or parsed (`RootIndex._parse(rel)`, not `full_build`).

PRIVACY (SDLC 7.6 privacy + FIND-QUERY-SURFACE-4): a refusal never echoes the path (nor any part of it, which
could be a private corpus's directory name); it states the TYPE and LENGTH of what it was given, as
`query_surface._d` does. The partition's `root_paths` is EMPTY, so no extent field can name the directory, and
its corpus label is a content digest (`file:<12 hex>`), never a path. The caller named the file, so a file
under a private corpus root IS answered; nothing in the answer names that corpus.
"""
from __future__ import annotations

import hashlib
import os
from typing import Optional, Tuple

from factlattice.hashing import safe_read_bytes
from factlattice.parser_config import ParserConfig

from .config import parser_config_for
from .incremental import CorpusMoved, RootIndex
from .store import Partition


_AFTER_READ_HOOK = None      # test seam only


class PathRefusal(Exception):
    """A named refusal; `str(e)` never contains the path."""


def describe(path: object) -> str:
    """What was passed, without the value: type and length (+ a short digest of a string)."""
    if isinstance(path, str):
        return (f"path (a string, length {len(path)}, sha256 "
                f"{hashlib.sha256(path.encode('utf-8', 'replace')).hexdigest()[:12]})")
    return f"path (type {type(path).__name__})"


def load_partition(path: object) -> Tuple[Partition, dict]:
    """Parse ONE `.lts` file into a throwaway `Partition`. Returns `(partition, file_source)` where
    `file_source` is `{"sha256", "bytes"}` of the exact bytes parsed. Raises `PathRefusal`."""
    if not isinstance(path, str) or not path.strip():
        raise PathRefusal(f"{describe(path)} must be a non-empty string naming one .lts file")
    try:
        real = os.path.realpath(os.path.expanduser(path))
        ok = os.path.isfile(real) and real.lower().endswith(".lts")
    except (OSError, ValueError):
        ok = False
    if not ok:
        raise PathRefusal(f"{describe(path)} is not a readable regular file ending in .lts "
                          "(a directory, a missing file and any other extension are refused)")
    root, rel = os.path.dirname(real), os.path.basename(real)
    ri = RootIndex(root, config=parser_config_for(None, None))
    try:
        data = _read(real)
        if _AFTER_READ_HOOK is not None:          # test seam: a writer landing between the two reads
            _AFTER_READ_HOOK(real)
        ri._parse(rel)
    except CorpusMoved:
        raise PathRefusal(f"{describe(path)} changed while it was being read; retry") from None
    except OSError as e:
        raise PathRefusal(f"{describe(path)} could not be read ({type(e).__name__})") from None
    if rel in ri._errors:
        raise PathRefusal(f"{describe(path)} could not be parsed ({_cls(ri._errors[rel])})")
    sha = hashlib.sha256(data).hexdigest()
    if ri.leaves.get(rel) != f"sha256:{sha}":
        # [P2-1] the parse read the file again; if its certified hash is not the hash of the bytes we
        # read first, `file_source` would certify bytes that were not the ones answered from.
        raise PathRefusal(f"{describe(path)} changed while it was being read; retry")
    asm = ri.assemble()
    label = f"file:{sha[:12]}"
    nodes, layers = asm["nodes"], asm["layers"]
    index = {
        "source_corpus": {"corpus_id": label, "domain_suffix": None, "files_hash": ri.root_hash(),
                          "root_paths": []},
        "nodes": nodes, "layers": layers,
        "profile_documents": [],
        "stats": {"total_nodes": len(nodes),
                  "total_edges": sum(len(ld["edges"]) for ld in layers.values()), "profile_documents": 0},
    }
    part = Partition(index, source_path=None)
    part.private_reason = None
    return part, {"sha256": sha, "bytes": len(data)}


def _read(real: str) -> bytes:
    return safe_read_bytes(real, os.path.basename(real))


def _cls(err: dict) -> str:
    """The exception CLASS NAME only (repr can carry text from the file or its path)."""
    e = str(err.get("error", ""))
    name = e.split("(", 1)[0].strip()
    return name if name.isidentifier() else "Exception"
