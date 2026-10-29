"""Phase 2 — the PARTITIONED multi-corpus store ([LTS-PARTITION]).

============================================================================
The MULTI-TENANCY model. The freshness oracle is the parser package's content hash:
    from factlattice import files_hash
There is no security/ACL code in this layer — it is tenant-first, security-blind
by design.
============================================================================

Loads MULTIPLE graph_index.json files, ONE per corpus, keyed by corpus_id and
selectable/filterable by domain_suffix. It NEVER merges them into a single global
graph ([INV-LTS-CORPUS-ISOLATION]): each corpus stays an INDEPENDENT partition
that is loaded and freshness-checked on its own. A node id that collides across two
corpora resolves to two DIFFERENT nodes because every lookup is scoped by corpus_id
— there is no shared node map to clobber.

Freshness ([INV-LTS-FRESH]) is recomputed per partition over the live corpus
CONTENT hash (never mtime/size), reusing the parser's `files_hash` oracle.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Optional

from factlattice import files_hash
from factlattice.hashing import safe_read_text


def combined_files_hash(root_paths: List[str]) -> str:
    """Deterministic content hash over one-or-more corpus roots ([ENH-3] freshness fix).

    A merged multi-root partition (config.py's decisions/specs/artifacts areas) is
    fresh iff EVERY one of its roots is unchanged — so the live re-hash must cover
    ALL `root_paths`, not just the first. SHA-256 over the sorted, newline-joined
    'root_path\\x00files_hash(root_path)' records; changes iff any root's content
    changes (same content-only guarantee as the single-root `files_hash` oracle).

    DEGENERATES to the plain single-root `files_hash` when exactly one root is given
    (returned as-is, not re-wrapped) — this is what lets `Partition.freshness()` call
    this ONE function unconditionally: a legacy single-root partition (built by
    `factlattice.build_index`, which stamps `files_hash(root)` directly) and a merged
    partition that only ended up with one surviving root (config.py's graceful-skip
    path) both compare correctly against the SAME stored hash.
    """
    if len(root_paths) == 1:
        h, _ = files_hash(root_paths[0])
        return h
    parts = []
    for root in sorted(root_paths):
        h, _ = files_hash(root)
        parts.append(f"{root}\x00{h}")
    joined = "\n".join(parts)
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FreshnessResult:
    """Per-partition freshness verdict — the [INV-LTS-FRESH] leg-2 kernel result.

    `fresh` is True iff the live corpus CONTENT hash equals the hash the index was
    built from. `reason` is populated on error (root missing/unreadable) — an
    unverifiable partition is reported NOT-fresh (fail-closed), never silently fresh.
    """
    corpus_id: str
    fresh: bool
    index_hash: Optional[str]
    live_hash: Optional[str]
    reason: Optional[str] = None       # a fixed CODE (`no-root-paths`, `live-rehash-failed`), never message text
    detail: Optional[str] = None       # the message (it can hold a path): LIVE reply only, never persisted


class Partition:
    """One loaded corpus index — an isolated graph. Never combined with another."""

    def __init__(self, index: dict, source_path: Optional[str] = None):
        sc = index.get("source_corpus") or {}
        self.index = index
        self.source_path = source_path
        self.corpus_id: str = sc.get("corpus_id")
        self.domain_suffix: str = sc.get("domain_suffix")
        self.files_hash: Optional[str] = sc.get("files_hash")
        self.root_paths: List[str] = list(sc.get("root_paths") or [])
        self.nodes: Dict[str, dict] = index.get("nodes", {})
        self.layers: Dict[str, dict] = index.get("layers", {})
        self.stats: Dict[str, object] = index.get("stats", {})
        # Files this partition PARSED but does not speak for: `.lts` domain profiles
        # (`document_tag: PROFILE`), which are vocabulary rather than content. Paths are
        # whatever the builder recorded (abs for the merged path, corpus-relative for a
        # single-root `build_index`). Never empty-by-accident: an absent key means the
        # index predates the split, not that no profile was found.
        self.profile_documents: List[str] = list(index.get("profile_documents") or [])
        if not self.corpus_id:
            raise ValueError("index missing source_corpus.corpus_id")
        # --- [LEC-FRESHNESS] incremental freshness + adjacency cache ----------
        # The kernel (if attached) maintains this partition's root incrementally so
        # `freshness()` is an O(1) compare; the adjacency cache is keyed to that root
        # so a graph walk reuses it while fresh and rebuilds only on a real change.
        self._kernel: Optional[object] = None
        # --- [LEC-SELF-HEAL] the rebuild RECIPE ------------------------------
        # An `incremental.CorpusIndex` attached by `config` at build time: the labeled
        # root specs (area / only_file pins, which `root_paths` alone has lost) plus the
        # per-file parse cache. Its presence is what makes a STALE verdict a TRANSIENT
        # state — `selfheal.SelfHealer` re-indexes THROUGH it, re-parsing only the files
        # whose content hash moved. A partition without one (a raw index dict loaded from
        # disk / a synthetic test fixture) simply cannot self-heal and keeps phase-2's
        # terminal refusal.
        self.corpus_index: Optional[object] = None
        self._adj_cache: Optional[tuple] = None
        self._adj_key: Optional[str] = None
        self._adj_builds: int = 0  # instrument: rebuilds observed (invalidation test)
        self._deg_cache: Optional[Dict[str, object]] = None
        self._deg_key: Optional[str] = None
        self._deg_builds: int = 0
        # --- [FL-PROSE-BLIND] the BODY index ---------------------------------
        # The graph index carries NO body text at all (see `factlattice.parser.Node.
        # to_dict`: id/title/properties/line_range/hashes and nothing else), so
        # `lts_find_in_prose` has to read the source. Cached exactly like adjacency —
        # keyed to the freshness root, so a real content change rebuilds it and a run of
        # prose queries reads the filesystem ONCE.
        self._body_cache: Optional[tuple] = None
        self._body_key: Optional[str] = None
        self._body_builds: int = 0

    # --- [LEC-FRESHNESS] kernel attachment ------------------------------------
    def attach_kernel(self, kernel: object) -> None:
        """Bind an incremental FreshnessKernel; makes `freshness()` O(1) once seated."""
        self._kernel = kernel

    # --- node access (always corpus-scoped; there is no global lookup) --------
    def get_node(self, node_id: str) -> Optional[dict]:
        return self.nodes.get(node_id)

    def has_node(self, node_id: str) -> bool:
        return node_id in self.nodes

    # --- edges ----------------------------------------------------------------
    def iter_edges(self) -> Iterator[dict]:
        for layer in self.layers.values():
            for edge in layer.get("edges", []):
                yield edge

    @property
    def total_edges(self) -> int:
        stated = self.stats.get("total_edges")
        if isinstance(stated, int):
            return stated
        return sum(1 for _ in self.iter_edges())

    @property
    def resolved_edge_count(self) -> int:
        """Node->node edges only (A2 ruling): excludes dangling AND unresolved_target."""
        count = 0
        for edge in self.iter_edges():
            if edge.get("dangling"):
                continue
            if "unresolved_target" in edge.get("flags", ()):
                continue
            count += 1
        return count

    def cross_corpus_edges(self) -> Iterator[dict]:
        """Edges authored to point OUT of this corpus: target_domain set and != own."""
        for edge in self.iter_edges():
            td = edge.get("target_domain")
            if td and td != self.domain_suffix:
                yield edge

    # --- adjacency cache ([LEC-FRESHNESS] #2 perf finding) --------------------
    def _freshness_key(self) -> str:
        """The key the adjacency cache is validated against. When a kernel is attached
        the MAINTAINED root is the key, so a real content change (which moves the root)
        invalidates adjacency; without a kernel the static index hash keys it (built
        once, reused for the process)."""
        if self._kernel is not None:
            root = self._kernel.current_root(self)
            if root is not None:
                return root
        return self.files_hash or "<static>"

    def _build_adjacency(self):
        """Forward/reverse incidence: {node_id: [(layer, edge), ...]}. Uniform across
        single-root and merged partitions (built from `layers[...].edges`, so it works
        even for merged partitions that never carried the parser's `adjacency` dict)."""
        fwd: Dict[str, List] = {}
        rev: Dict[str, List] = {}
        for layer, ldata in self.layers.items():
            for edge in ldata.get("edges", []):
                fwd.setdefault(edge["source"], []).append((layer, edge))
                rev.setdefault(edge["target"], []).append((layer, edge))
        return fwd, rev

    def adjacency(self):
        """Cached (forward, reverse) incidence maps, invalidated only on a real content
        change (freshness-root change). Replaces the per-call O(E) edge rescan the graph
        walk used to do ([LEC-FRESHNESS] #2)."""
        key = self._freshness_key()
        if self._adj_cache is None or self._adj_key != key:
            self._adj_cache = self._build_adjacency()
            self._adj_key = key
            self._adj_builds += 1
        return self._adj_cache

    def _build_degree_index(self) -> Dict[str, object]:
        """Whole-partition degree + edge-type histogram. Edge resolution follows the
        engine's `resolved_edge_count` rule (skip `dangling` + `unresolved_target`)."""
        in_deg: Dict[str, int] = {}
        out_deg: Dict[str, int] = {}
        edge_types: Dict[str, int] = {}
        total = 0
        resolved = 0
        for e in self.iter_edges():
            total += 1
            if e.get("dangling"):
                continue
            if "unresolved_target" in (e.get("flags") or ()):
                continue
            resolved += 1
            et = e.get("type") or "(untyped)"
            edge_types[et] = edge_types.get(et, 0) + 1
            src, tgt = e.get("source"), e.get("target")
            if src is not None:
                out_deg[src] = out_deg.get(src, 0) + 1
            if tgt is not None:
                in_deg[tgt] = in_deg.get(tgt, 0) + 1
        return {"in_deg": in_deg, "out_deg": out_deg, "edge_types": edge_types,
                "total_edges": total, "resolved_edges": resolved}

    def degree_index(self) -> Dict[str, object]:
        """Cached degree/edge-type histogram for the ANALYTICS layer's graph-native
        aggregates, keyed to the freshness root exactly like `adjacency()`. Closes the
        perf follow-on the analytics layer flagged for this phase (its Tier-2 ops used
        to re-scan every edge per call). Callers MUST NOT mutate the returned dicts."""
        key = self._freshness_key()
        if self._deg_cache is None or self._deg_key != key:
            self._deg_cache = self._build_degree_index()
            self._deg_key = key
            self._deg_builds += 1
        return self._deg_cache

    # --- body index ([FL-PROSE-BLIND]) ----------------------------------------
    def _resolve_source(self, source_file: str) -> Optional[str]:
        """Absolute path for a node's `source_file`, across a merged multi-root
        partition (a node's source_file is relative to its OWN root, not root_paths[0])."""
        if not source_file or not self.root_paths:
            return None
        for root in self.root_paths:
            cand = os.path.join(root, source_file)
            if os.path.isfile(cand):
                return cand
        return None

    def _build_body_index(self):
        """{node_id: [(lineno, raw, lowered), ...]} + a scan report.

        ⛔ WHAT COUNTS AS BODY, and why the ruling is not obvious:

        * THE HEADING LINE IS EXCLUDED. `line_range[0]` is `## Title [ID]` — that is the
          node's id and title, which is precisely what `lts_find` already reads. Leaving it
          in would make the doctype tag (`[SPEC:...]`) a prose hit on every node in a
          document and drown the signal.
        * PROPERTY LINES ARE INCLUDED, deliberately. `tools._text_hit` reads string property
          values and the string ELEMENTS of list-valued properties (`_string_values`), but a
          MAPPING-valued property is invisible to `lts_find`. Excluding property lines here
          would reproduce that blindness for mappings, which this tool exists to close. The per-hit `in_structure` flag is what keeps the two
          cases distinguishable, so nothing is lost by scanning wide.
        * NODE RANGES DO NOT NEST. The parser ends a node's range at the next heading of
          ANY level, so a section does not swallow its children and one hit cannot inflate
          into a hit on every ancestor. The one exception is a list-item node definition
          (`definition_form: list-item`), whose single line also lies inside its parent's
          range — a term on that line legitimately hits both.

        Lines are stored WITH their 1-indexed number so a hit can name a line, and with a
        pre-lowered twin so a run of queries does not re-lower the corpus every call.
        """
        by_file: Dict[str, List[str]] = {}
        unreadable: Dict[str, str] = {}
        index: Dict[str, List[tuple]] = {}
        chars = 0
        for nid, nd in self.nodes.items():
            sf = nd.get("source_file")
            lr = nd.get("line_range") or []
            if not sf or len(lr) < 2:
                continue
            if sf not in by_file and sf not in unreadable:
                path = self._resolve_source(sf)
                if path is None:
                    unreadable[sf] = ("source-file-not-found", None)
                else:
                    try:
                        by_file[sf] = safe_read_text(path, sf).splitlines()
                    except OSError as exc:
                        unreadable[sf] = ("source-file-unreadable", f"{type(exc).__name__}: {exc}")
            lines = by_file.get(sf)
            if lines is None:
                continue
            # +1 on the start: skip the heading line (see the docstring's first ruling).
            start = max(1, int(lr[0])) + 1
            end = min(len(lines), int(lr[1]))
            rows = []
            for n in range(start, end + 1):
                raw = lines[n - 1]
                if not raw.strip():
                    continue
                rows.append((n, raw, raw.lower()))
                chars += len(raw)
            if rows:
                index[nid] = rows
        report = {
            "files_read": len(by_file),
            "files_unreadable": [{"source_file": k, "reason": code,
                                  **({"error_class": detail.partition(":")[0],
                                      "live_detail": detail,
                                      "live_detail_note": "live_detail is for this live reply only: NEVER persist it"}
                                     if detail else {})}
                                 for k, (code, detail) in sorted(unreadable.items())],
            "nodes_with_body": len(index),
            "nodes_in_scope": len(self.nodes),
            "body_chars": chars,
        }
        return index, report

    def body_index(self):
        """Cached `(index, report)` for prose search, invalidated on a real content change.

        Returns the SAME pair `adjacency()` returns for edges: built once per freshness
        root, reused for every subsequent query against that root. Callers MUST NOT mutate
        the returned structures.
        """
        key = self._freshness_key()
        if self._body_cache is None or self._body_key != key:
            self._body_cache = self._build_body_index()
            self._body_key = key
            self._body_builds += 1
        return self._body_cache

    def nodes_with_unread_body(self) -> int:
        """How many in-scope nodes carry source lines BEYOND their heading — the count
        `lts_find` puts in its `scope_note` to route a caller to the prose tool.

        ⛔ INDEX-DERIVED, NO FILE I/O. `lts_find` must not pay a filesystem scan to
        declare its own scope, so this counts line ranges, not content: a node whose range
        is heading-plus-properties is counted even though a human would not call those
        lines prose. It is therefore an UPPER BOUND on unread prose, and the note says so.
        """
        count = 0
        for nd in self.nodes.values():
            lr = nd.get("line_range") or []
            if len(lr) >= 2 and int(lr[1]) > int(lr[0]):
                count += 1
        return count

    # --- freshness ([INV-LTS-FRESH] leg-2, content hashing only) ------------
    def freshness(self) -> FreshnessResult:
        # [LEC-FRESHNESS] fast path: a seated kernel maintains the live content-hash
        # root incrementally, so this is an O(1) compare with NO file I/O. The content
        # hash is still the oracle — the kernel only maintains it incrementally.
        if self._kernel is not None:
            root = self._kernel.current_root(self)
            if root is not None:
                return FreshnessResult(
                    corpus_id=self.corpus_id,
                    fresh=(self.files_hash == root),
                    index_hash=self.files_hash,
                    live_hash=root,
                )
        # Fallback (no kernel / unseated): the original full re-hash. Preserved so
        # synthetic indexes, the CLI, and direct `Partition.freshness()` callers behave
        # exactly as before.
        if not self.root_paths:
            return FreshnessResult(self.corpus_id, False, self.files_hash, None,
                                   "no-root-paths")
        try:
            # [ENH-3 fix] hash ALL root_paths, not just root_paths[0] — a multi-root
            # partition (config.py's decisions/specs/artifacts areas) is stale if ANY
            # of its roots changed. `combined_files_hash` degenerates correctly to the
            # plain single-root hash when there's only one, so this is unconditional.
            live_hash = combined_files_hash(self.root_paths)
        except Exception as exc:  # noqa: BLE001 — unverifiable => fail-closed STALE
            return FreshnessResult(self.corpus_id, False, self.files_hash, None,
                                   "live-rehash-failed", f"live re-hash failed: {exc!r}")
        return FreshnessResult(
            corpus_id=self.corpus_id,
            fresh=(self.files_hash == live_hash),
            index_hash=self.files_hash,
            live_hash=live_hash,
        )


class LtsStore:
    """A registry of independent Partitions. Selects/filters by corpus_id or domain.

    [INV-LTS-CORPUS-ISOLATION]: no method ever fuses two partitions' nodes/edges.
    `get_node` is ALWAYS scoped by corpus_id, so a colliding id cannot clobber.
    """

    def __init__(self):
        self._by_corpus_id: Dict[str, Partition] = {}
        self._by_domain: Dict[str, List[str]] = {}
        # The incremental freshness kernel (attached by server.build_tools); held for
        # its process lifetime so the background watcher stays alive.
        self.kernel: Optional[object] = None
        # corpus_id -> {"code": <corpus-file-* code>, "path": <root-relative path>}: corpora whose build failed LOUD because
        # a member `.lts` is not a readable regular file ([ISSUE-NONREGULAR-MEMBER]). Such a
        # corpus is NOT served (never a partial view) and is REPORTED (`lts_list_corpora`, and the corpus-scoped tools).
        self.failed_corpora: Dict[str, Dict[str, str]] = {}

    # --- registration ---------------------------------------------------------
    def add_partition(self, partition: Partition) -> Partition:
        cid = partition.corpus_id
        if cid in self._by_corpus_id:
            raise ValueError(f"corpus_id already loaded (would clobber): {cid!r}")
        self._by_corpus_id[cid] = partition
        self._by_domain.setdefault(partition.domain_suffix, []).append(cid)
        return partition

    def remove_partition(self, corpus_id: str) -> Optional[Partition]:
        """The runtime INVERSE of `add_partition` ([REG-RUNTIME-CORPUS]).

        Unregisters a loaded corpus without disturbing any other partition: pops it out
        of `_by_corpus_id` and out of its `domain_suffix` bucket (dropping the bucket
        entirely once empty, so a stale empty list can never masquerade as "still
        registered" for `filter_by_domain`). Returns the removed `Partition` (so a caller
        can still report what was unloaded), or `None` if `corpus_id` was not loaded —
        never raises on a no-op unregister.

        Deliberately does NOT touch the kernel or the self-healer: they hold their own
        per-corpus-id state (the maintained root, the rebuild lock/attempt-key) and must
        be told separately (`FreshnessKernel.unseat_partition` / `SelfHealer.forget`) so
        this method stays usable on a bare store in tests with neither attached.
        """
        part = self._by_corpus_id.pop(corpus_id, None)
        if part is None:
            return None
        bucket = self._by_domain.get(part.domain_suffix)
        if bucket is not None:
            try:
                bucket.remove(corpus_id)
            except ValueError:
                pass
            if not bucket:
                self._by_domain.pop(part.domain_suffix, None)
        return part

    def load_partition(self, index_path: str) -> Partition:
        index = json.loads(safe_read_text(index_path, os.path.basename(index_path)))
        return self.add_partition(Partition(index, source_path=index_path))

    @classmethod
    def load(cls, index_paths: Iterable[str]) -> "LtsStore":
        store = cls()
        for p in index_paths:
            store.load_partition(p)
        return store

    # --- selection / filtering ------------------------------------------------
    def corpus_ids(self) -> List[str]:
        return list(self._by_corpus_id.keys())

    def partitions(self) -> List[Partition]:
        return list(self._by_corpus_id.values())

    def get_partition(self, corpus_id: str) -> Optional[Partition]:
        return self._by_corpus_id.get(corpus_id)

    def select(self, corpus_id: str) -> Optional[Partition]:
        """Select exactly ONE corpus by id (a query is scoped to one corpus)."""
        return self._by_corpus_id.get(corpus_id)

    def filter_by_domain(self, domain_suffix: str) -> List[Partition]:
        """All partitions bearing a domain suffix (selection axis, [LTS-PARTITION])."""
        return [self._by_corpus_id[c] for c in self._by_domain.get(domain_suffix, [])]

    # --- scoped node access (never global) ------------------------------------
    def get_node(self, corpus_id: str, node_id: str) -> Optional[dict]:
        part = self._by_corpus_id.get(corpus_id)
        return part.get_node(node_id) if part else None

    # --- derived, per-partition stats -----------------------------------------
    def stats(self, corpus_id: str) -> Optional[dict]:
        part = self._by_corpus_id.get(corpus_id)
        if part is None:
            return None
        return {
            "corpus_id": part.corpus_id,
            "domain_suffix": part.domain_suffix,
            "total_nodes": part.stats.get("total_nodes", len(part.nodes)),
            "total_edges": part.total_edges,
            "resolved_edge_count": part.resolved_edge_count,
            "dangling_edges": part.stats.get("dangling_edges"),
            "unresolved_target_edges": part.stats.get("unresolved_target_edges"),
        }

    def freshness(self, corpus_id: str) -> Optional[FreshnessResult]:
        part = self._by_corpus_id.get(corpus_id)
        return part.freshness() if part else None
