"""[INV-PRIVATE-FANOUT] which corpora a corpus-omitted query may read.

A corpus is PRIVATE when its registry entry says `private: true`, or when its id ends in
`-personality` and the entry does not say `private: false`. A private corpus is read only
when a caller NAMES it; every corpus-omitted fan-out skips it and lists it under
`extent.corpora_skipped` with `PRIVATE_REASON`.

Why the id suffix is a default and not merely a convention: the personality corpora
(`<agent>-personality`, one per Intelligence) are registered by many hands, and the defect
this closes (acme-team ISSUE-FANOUT-LEAK) existed because
privacy depended on every caller remembering an argument. A registry entry that forgets
`private: true` must still be private.
"""
from __future__ import annotations

from typing import Optional

PRIVATE_SUFFIX = "-personality"
PRIVATE_REASON = "private: name it explicitly"
# The one line every corpus-listing error carries INSTEAD of private names.
PRIVATE_NOTE = "private corpora are searched only when named exactly"


def private_reason(corpus_id: str, spec: Optional[dict] = None) -> Optional[str]:
    """Why `corpus_id` is private ("registry" | "id-suffix"), or None if it is not."""
    flag = spec.get("private") if isinstance(spec, dict) else None
    if flag is True:
        return "registry"
    if flag is False:
        return None
    if corpus_id.endswith(PRIVATE_SUFFIX):
        return "id-suffix"
    return None


def is_private_partition(part) -> bool:
    """A partition's privacy, as stamped at build time (`config.build_store` /
    `register_corpus`), falling back to the id rule for a partition built by hand."""
    stamped = getattr(part, "private_reason", "unset")
    if stamped != "unset":
        return stamped is not None
    return private_reason(part.corpus_id) is not None
