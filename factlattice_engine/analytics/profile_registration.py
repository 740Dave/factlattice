"""THE AGREEMENT CHECK — a profile is declared TWICE, and the two must agree.

============================================================================
TWO INDEPENDENT SIGNALS
============================================================================
A `.lts` domain profile announces itself two different ways, answering two different
questions:

  INTRINSIC (the file)   `document_tag: PROFILE` — "I am a domain profile."
                         This is the signal `factlattice.parser.is_profile_document`
                         reads, and the ONLY one that governs CONTENT exclusion. It
                         travels with the file: it survives relocation, copying, and a
                         wrong config.

  CONFIG (the registry)  `corpus-config.json`'s `profile:` key — "profile P governs
                         corpus C." Registration cannot be derived from the file (a
                         profile does not know which corpora it serves), so this key
                         stays required. It answers WHICH, never WHETHER.

Neither subsumes the other, and NEITHER IS TRUSTED ALONE. `private/corpus-config.json`
carried `profile: None` for all eleven corpora for weeks; had exclusion depended on
registration, a mis-registered profile would have silently become content — which is
precisely what happened to `platform-acme-profile.lts` (88 declaration nodes
answering content queries, 88 permanent `dangle.orphan` warnings).

So their DISAGREEMENT is reported as a finding rather than silently resolved:

  `registered_profile_not_tagged_profile`
      Config names P as a corpus's profile, but P's own `document_tag` is not
      `PROFILE`. P is therefore STILL CONTENT — its declaration nodes are in the graph
      answering content queries. Either P is not really a profile, or it is missing its
      doctag. Both are authoring bugs and both are invisible without this check.

  `unregistered_profile_document`
      A `PROFILE`-tagged file sits inside a corpus root but is registered as NOBODY's
      profile. It is correctly excluded from content (the intrinsic signal did its job),
      but no corpus is being judged against it — the vocabulary it declares is inert.

============================================================================
IT COSTS NOTHING
============================================================================
Both facts already exist by the time this runs. The excluded-as-profile set is recorded
on the partition (`Partition.profile_documents`, stamped by the content fold that did
the excluding), and the registered set is one read of the corpus config. The only I/O
this adds is reading the doctag of each REGISTERED profile path — a handful of files,
and only the ones a human explicitly named.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

KIND_REGISTERED_NOT_TAGGED = "registered_profile_not_tagged_profile"
KIND_UNREGISTERED_PROFILE_DOC = "unregistered_profile_document"
KIND_REGISTERED_PROFILE_MISSING = "registered_profile_missing"


def _real(path: Optional[str]) -> Optional[str]:
    return os.path.realpath(path) if path else None


def document_tag_of(path: str) -> Optional[str]:
    """The file's own `document_tag`, read THROUGH THE PARSER (never a regex).

    The doctag is the load-bearing signal for content exclusion; deriving it here with
    a second, cheaper reader would be exactly the kind of parallel implementation that
    drifts. Returns None for an unparseable/absent file — the caller distinguishes
    "not a profile" from "could not tell".
    """
    from factlattice.parser import parse_file
    try:
        return parse_file(path, rel_source_file=os.path.basename(path)).document_tag
    except Exception:  # noqa: BLE001 — an unreadable file is a finding, not a crash
        return None


def audit_profile_registration(store: Any = None, *, corpus: Optional[str] = None,
                               config: Optional[Dict[str, Any]] = None) -> dict:
    """Cross-check the intrinsic doctag against `corpus-config.json`'s registrations.

    `store` — a loaded `LtsStore` (optional). When given, the PROFILE-tagged files
    actually excluded from each partition are read off `Partition.profile_documents`
    for free. Without it, only the registered side can be checked and
    `scanned_partitions` is False, which the result SAYS rather than implying a clean
    scan of something never scanned.

    `corpus` — scope the report to one corpus id (the registered-side check still
    considers every corpus's registration, because a profile excluded in corpus C may
    legitimately be registered by corpus D).

    Returns `{success, findings, registered_profiles, excluded_profile_documents,
    scanned_partitions, corpus}`.
    """
    if config is None:
        from factlattice.profile import load_corpus_config
        config = load_corpus_config() or {}

    findings: List[dict] = []

    # --- registered side: every path a corpus names as its profile -----------------
    registered: Dict[str, List[str]] = {}      # realpath -> [corpus_id, ...]
    registered_report: List[dict] = []
    for cid, spec in sorted((config or {}).items()):
        path = spec.get("profile") if isinstance(spec, dict) else None
        if not path:
            continue
        registered.setdefault(_real(path), []).append(cid)

    for rpath, cids in sorted(registered.items(), key=lambda kv: str(kv[0])):
        exists = bool(rpath) and os.path.isfile(rpath)
        tag = document_tag_of(rpath) if exists else None
        registered_report.append({"path": rpath, "corpora": sorted(cids),
                                  "exists": exists, "document_tag": tag})
        if not exists:
            findings.append({
                "kind": KIND_REGISTERED_PROFILE_MISSING,
                "path": rpath, "corpora": sorted(cids),
                "message": (f"corpora {sorted(cids)} register profile {rpath!r}, which "
                            "does not exist — those corpora are judged against nothing"),
            })
            continue
        if tag != "PROFILE":
            findings.append({
                "kind": KIND_REGISTERED_NOT_TAGGED,
                "path": rpath, "corpora": sorted(cids), "document_tag": tag,
                "message": (f"corpora {sorted(cids)} register {os.path.basename(rpath)!r} "
                            f"as their domain profile, but its document_tag is {tag!r}, "
                            "not 'PROFILE' — so it is STILL PARSED AS CONTENT and its "
                            "declaration nodes answer content queries. Tag the h1 "
                            "`[PROFILE:<ID>]` (and the frontmatter id) or stop "
                            "registering it as a profile."),
            })

    # --- intrinsic side: what the content fold actually excluded -------------------
    excluded: Dict[str, List[str]] = {}        # realpath -> [corpus_id, ...]
    scanned = False
    if store is not None:
        scanned = True
        for cid in store.corpus_ids():
            if corpus and cid != corpus:
                continue
            part = store.get_partition(cid)
            for p in getattr(part, "profile_documents", None) or []:
                excluded.setdefault(_real(p), []).append(cid)

    for epath, cids in sorted(excluded.items(), key=lambda kv: str(kv[0])):
        if epath in registered:
            continue
        findings.append({
            "kind": KIND_UNREGISTERED_PROFILE_DOC,
            "path": epath, "corpora": sorted(cids),
            "message": (f"{os.path.basename(str(epath))!r} declares document_tag "
                        f"'PROFILE' and sits inside the root(s) of {sorted(cids)}, so it "
                        "is correctly excluded from content — but NO corpus registers it "
                        "as its profile, so the vocabulary it declares is inert. Add it "
                        "as a `profile:` in the corpus config, or move it out of the "
                        "corpus root."),
        })

    return {
        "success": True,
        "corpus": corpus,
        "scanned_partitions": scanned,
        "registered_profiles": registered_report,
        "excluded_profile_documents": {str(k): sorted(v) for k, v in sorted(
            excluded.items(), key=lambda kv: str(kv[0]))},
        "findings": findings,
        "finding_counts": _counts(findings),
    }


def _counts(findings: List[dict]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for f in findings:
        out[f["kind"]] = out.get(f["kind"], 0) + 1
    return out


__all__ = [
    "audit_profile_registration",
    "document_tag_of",
    "KIND_REGISTERED_NOT_TAGGED",
    "KIND_UNREGISTERED_PROFILE_DOC",
    "KIND_REGISTERED_PROFILE_MISSING",
]
