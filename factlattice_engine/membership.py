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

import os
from typing import Any, Dict, List, Optional, Tuple


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


def _contains(root: str, path: str) -> bool:
    """True when `path` is `root` or lies beneath it. `os.path.commonpath` rather than a
    `startswith`, so `/a/corpus-old` is not read as inside `/a/corpus`."""
    try:
        return os.path.commonpath([_norm(root), _norm(path)]) == _norm(root)
    except ValueError:      # different drives / one is relative in a way that cannot join
        return False


def registered_roots(store: object = None) -> Tuple[Dict[str, List[str]], str]:
    """`({corpus_id: [root, ...]}, source)`.

    `source` is `"store"` (the seated, serving index) or `"config"` (the declared
    registry) or `"none"`. Reported, never inferred by the caller.
    """
    if store is not None:
        try:
            roots = {p.corpus_id: [_norm(r) for r in (p.root_paths or [])]
                     for p in store.partitions()}
            if roots:
                return roots, "store"
        except Exception:  # noqa: BLE001 — a store that cannot answer is not fatal here
            pass
    # ⛔ NOT wrapped in `except Exception`. This used to swallow EVERY failure of the registry read and
    # return `({}, "none")`, so a broken registry made the root-membership warning go quiet -- the silent
    # fallback this lookup exists to remove. A `RegistryError` (`registry-not-found`) now PROPAGATES;
    # a write receipt that must still be delivered catches it explicitly (`authoring._annotate`).
    from . import config
    declared: Dict[str, List[str]] = {}
    for cid, spec in (config.default_corpora() or {}).items():
        paths: List[str] = []
        for root in (spec.get("roots") or []):
            p = root.get("path") if isinstance(root, dict) else root
            if p:
                paths.append(_norm(p))
        if spec.get("root"):
            paths.append(_norm(spec["root"]))
        declared[cid] = sorted(dict.fromkeys(paths))
    if declared:
        return declared, "config"
    return {}, "none"


def check(path: str, store: object = None) -> Dict[str, Any]:
    """Root-membership verdict for one authored path.

    Keys: `inside_corpus` (bool), `corpora` (ids containing it), `roots_source`,
    `roots_checked` ({corpus: [roots]}), and `warning` (None when it is inside one).
    """
    roots, source = registered_roots(store)
    hits = sorted(cid for cid, paths in roots.items()
                  if any(_contains(r, path) for r in paths))
    out: Dict[str, Any] = {
        "path": _norm(path),
        "inside_corpus": bool(hits),
        "corpora": hits,
        "roots_source": source,
        "roots_checked": {cid: list(paths) for cid, paths in sorted(roots.items())},
        "warning": None,
    }
    if hits:
        return out
    if source == "none":
        out["warning"] = (
            "⚠ ROOT MEMBERSHIP UNVERIFIABLE — no corpus roots are registered in this "
            "process (no store seated and no corpus registry resolved), so the engine "
            "cannot say whether this file will ever be indexed. The write landed.")
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
    return out


def annotate(receipt: Dict[str, Any], path: str, store: object = None) -> Dict[str, Any]:
    """Merge a membership verdict into a write receipt, in place, and append the warning
    to the receipt's own `warnings` list so it travels with the fields callers read."""
    verdict = check(path, store=store)
    receipt["root_membership"] = verdict
    if verdict["warning"]:
        receipt["warnings"] = list(receipt.get("warnings") or []) + [verdict["warning"]]
        if isinstance(receipt.get("report"), str):
            receipt["report"] += f"\n\nROOT MEMBERSHIP:\n  {verdict['warning']}"
    return receipt


__all__ = ["check", "annotate", "registered_roots"]
