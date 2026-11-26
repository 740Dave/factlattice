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
import stat
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from factlattice import files_hash
from factlattice.hashing import safe_read_text

from .rootstate import SKIP_REASON


def combined_files_hash(root_paths: list[str]) -> str:
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
    index_hash: str | None
    live_hash: str | None
    reason: str | None = None       # a fixed CODE (`no-root-paths`, `live-rehash-failed`), never message text
    detail: str | None = None       # the message (it can hold a path): LIVE reply only, never persisted


def _root_fingerprint(root: str) -> tuple:
    """Names and stats only: lists `.lts` entry NAMES under a root and stats them, opening nothing (a FIFO is
    stat-ed, never opened), so it is safe on a root the engine has not yet classified. A symlinked member is stat-ed
    twice, the link (`lstat`) and its TARGET (`stat`, or the error doing so), because the engine follows the link when
    it reads the member: creating a broken link's target, or making an unreadable target readable, must move the
    fingerprint (round-3 Diamond P1-C1). Each stat row carries the change time (ctime) as well as mode, size and mtime:
    an ACL or owner change can make a member unreadable, or readable again, with no change to mode, size or
    mtime, but it moves ctime (round-3b Diamond P1-D1). Bounded by the root's own size; it runs only while a corpus's registered root
    set differs from its seated one."""
    out = []
    try:
        out.append(("", os.stat(root).st_mode))
    except OSError as e:
        return (("", "err", e.errno),)
    def onerror(e):
        out.append((os.path.relpath(getattr(e, "filename", root) or root, root), "err", e.errno))
    for dp, _dirs, files in os.walk(root, onerror=onerror):
        for f in files:
            if not f.endswith(".lts"):
                continue
            fp = os.path.join(dp, f)
            rel = os.path.relpath(fp, root)
            try:
                st = os.lstat(fp)
                out.append((rel, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
            except OSError as e:
                out.append((rel, "err", e.errno))
                continue
            if stat.S_ISLNK(st.st_mode):
                try:
                    tg = os.stat(fp)        # follows the link; stat never opens, so a FIFO target is safe
                    out.append(("->", rel, tg.st_mode, tg.st_size, tg.st_mtime_ns, tg.st_ctime_ns))
                except OSError as e:
                    out.append(("->", rel, "err", e.errno))
    return tuple(sorted(out, key=repr))


class Partition:
    """One loaded corpus index — an isolated graph. Never combined with another."""

    def __init__(self, index: dict, source_path: str | None = None):
        sc = index.get("source_corpus") or {}
        self.index = index
        self.source_path = source_path
        self.corpus_id: str = sc.get("corpus_id")
        self.domain_suffix: str = sc.get("domain_suffix")
        self.files_hash: str | None = sc.get("files_hash")
        self.root_paths: list[str] = list(sc.get("root_paths") or [])
        #: set by the boot loader just before `seat_partition`: {abs root path: (RootStates, commit)} that the root's fused job already read and hashed; `seat_partition` takes and clears it
        self._prepared_seat: dict | None = None
        self.nodes: dict[str, dict] = index.get("nodes", {})
        self.layers: dict[str, dict] = index.get("layers", {})
        self.stats: dict[str, object] = index.get("stats", {})
        # Files this partition PARSED but does not speak for: `.lts` domain profiles
        # (`document_tag: PROFILE`), which are vocabulary rather than content. Paths are
        # absolute from both builders and after a heal (SDLC 7.6, Domain profiles); a raw
        # disk index loaded as-is (`load_partition`) keeps `build_index`'s root-relative form.
        # Never empty-by-accident: an absent key means the
        # index predates the split, not that no profile was found.
        self.profile_documents: list[str] = list(index.get("profile_documents") or [])
        if not self.corpus_id:
            raise ValueError("index missing source_corpus.corpus_id")
        # --- [LEC-FRESHNESS] incremental freshness + adjacency cache ----------
        # The kernel (if attached) maintains this partition's root incrementally so
        # `freshness()` is an O(1) compare; the adjacency cache is keyed to that root
        # so a graph walk reuses it while fresh and rebuilds only on a real change.
        self._kernel: object | None = None
        # --- [LEC-SELF-HEAL] the rebuild RECIPE ------------------------------
        # An `incremental.CorpusIndex` attached by `config` at build time: the labeled
        # root specs (area / only_file pins, which `root_paths` alone has lost) plus the
        # per-file parse cache. Its presence is what makes a STALE verdict a TRANSIENT
        # state — `selfheal.SelfHealer` re-indexes THROUGH it, re-parsing only the files
        # whose content hash moved. A partition without one (a raw index dict loaded from
        # disk / a synthetic test fixture) simply cannot self-heal and keeps phase-2's
        # terminal refusal.
        self.corpus_index: object | None = None
        # Derived-cache slots. Each is ONE tuple `(freshness_key, source, value)` so a reader sees all three
        # or none: `source` is the very `layers` / `nodes` object the value was built from, and a slot is
        # served only while `source is` the partition's current one. See `_derived`.
        self._adj_slot: tuple | None = None
        self._adj_builds: int = 0  # instrument: rebuilds observed (invalidation test)
        self._deg_slot: tuple | None = None
        self._deg_builds: int = 0
        # --- [FL-PROSE-BLIND] the BODY index ---------------------------------
        # The graph index carries NO body text at all (see `factlattice.parser.Node.
        # to_dict`: id/title/properties/line_range/hashes and nothing else), so
        # `lts_find_in_prose` has to read the source. Cached exactly like adjacency —
        # keyed to the freshness root, so a real content change rebuilds it and a run of
        # prose queries reads the filesystem ONCE.
        self._body_slot: tuple | None = None
        self._body_builds: int = 0

    # --- [LEC-FRESHNESS] kernel attachment ------------------------------------
    def attach_kernel(self, kernel: object) -> None:
        """Bind an incremental FreshnessKernel; makes `freshness()` O(1) once seated."""
        self._kernel = kernel

    # --- node access (always corpus-scoped; there is no global lookup) --------
    def get_node(self, node_id: str) -> dict | None:
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

    def _drop_derived_caches(self) -> None:
        """Forget every cache derived from nodes/layers. Called by the heal's publish; correctness does
        not depend on it (a slot whose source is no longer current is never served), it only frees the
        old content at once."""
        self._adj_slot = None
        self._deg_slot = None
        self._body_slot = None

    def _derived(self, slot: str, source_attr: str, build):
        """The value of a cache derived from `self.<source_attr>` ('nodes' or 'layers'), built at most
        once per (freshness key, source object).

        ⛔ A CACHE IS KEYED ON WHAT IT WAS BUILT FROM. The freshness key is the kernel's maintained root,
        which moves when a file changes; `nodes` / `layers` are replaced only when the heal PUBLISHES. In
        the gap, "key == current" says nothing about the content a build read. So the object the build
        reads is captured ONCE, here, handed to `build(self, src)` (which must read nothing else off `self`), and
        stored beside the value; a later reader is served only if that object is still the partition's
        current one. A publish REPLACES the `nodes` and `layers` containers (it does not add to or remove from
        them in place; edge dicts shared with earlier layers can still be edited, see SDLC 7.6), so
        identity of the containers is exact, and holding the reference keeps the object alive, so identity cannot be
        reused. The key stays as a second condition: a root that moved with no publish (no healer) still
        rebuilds, as before. Whatever order the key and the source are read in, and whenever the publish
        lands, a stored slot is either consistent (value built from its own source) or unreachable."""
        key = self._freshness_key()
        src = getattr(self, source_attr)
        hit = getattr(self, slot)
        if hit is not None and hit[0] == key and hit[1] is src:
            return hit[2]
        value = build(self, src)
        setattr(self, slot, (key, src, value))
        return value

    def _build_adjacency(self, layers=None):
        """Forward/reverse incidence: {node_id: [(layer, edge), ...]}. Uniform across
        single-root and merged partitions (built from `layers[...].edges`, so it works
        even for merged partitions that never carried the parser's `adjacency` dict)."""
        layers = self.layers if layers is None else layers
        fwd: dict[str, list] = {}
        rev: dict[str, list] = {}
        for layer, ldata in layers.items():
            for edge in ldata.get("edges", []):
                fwd.setdefault(edge["source"], []).append((layer, edge))
                rev.setdefault(edge["target"], []).append((layer, edge))
        return fwd, rev

    def adjacency(self):
        """Cached (forward, reverse) incidence maps, rebuilt on a real content change (freshness
        root change) or when the heal has published new layers. Replaces the per-call O(E) edge
        rescan the graph walk used to do ([LEC-FRESHNESS] #2)."""
        return self._derived("_adj_slot", "layers", Partition._built_adjacency)

    def _built_adjacency(self, layers):
        self._adj_builds += 1
        return self._build_adjacency(layers)

    def _build_degree_index(self, layers=None) -> dict[str, object]:
        """Whole-partition degree + edge-type histogram. Edge resolution follows the
        engine's `resolved_edge_count` rule (skip `dangling` + `unresolved_target`)."""
        in_deg: dict[str, int] = {}
        out_deg: dict[str, int] = {}
        edge_types: dict[str, int] = {}
        total = 0
        resolved = 0
        layers = self.layers if layers is None else layers
        for e in (e for ld in layers.values() for e in ld.get("edges", [])):
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

    def degree_index(self) -> dict[str, object]:
        """Cached degree/edge-type histogram for the ANALYTICS layer's graph-native
        aggregates, keyed to the freshness root exactly like `adjacency()`. Closes the
        perf follow-on the analytics layer flagged for this phase (its Tier-2 ops used
        to re-scan every edge per call). Callers MUST NOT mutate the returned dicts."""
        return self._derived("_deg_slot", "layers", Partition._built_degree)

    def _built_degree(self, layers):
        self._deg_builds += 1
        return self._build_degree_index(layers)

    # --- body index ([FL-PROSE-BLIND]) ----------------------------------------
    def _resolve_source(self, source_file: str) -> str | None:
        """Absolute path for a node's `source_file`, across a merged multi-root
        partition (a node's source_file is relative to its OWN root, not root_paths[0])."""
        if not source_file or not self.root_paths:
            return None
        for root in self.root_paths:
            cand = os.path.join(root, source_file)
            if os.path.isfile(cand):
                return cand
        return None

    def _build_body_index(self, nodes=None):
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
        nodes = self.nodes if nodes is None else nodes     # read ONCE; everything below uses this object
        by_file: dict[str, list[str]] = {}
        unreadable: dict[str, str] = {}
        index: dict[str, list[tuple]] = {}
        chars = 0
        for nid, nd in nodes.items():
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
            "nodes_in_scope": len(nodes),
            "body_chars": chars,
        }
        return index, report

    def body_index(self):
        """Cached `(index, report)` for prose search, invalidated on a real content change.

        Returns the SAME pair `adjacency()` returns for edges: built once per freshness
        root, reused for every subsequent query against that root. Callers MUST NOT mutate
        the returned structures.
        """
        return self._derived("_body_slot", "nodes", Partition._built_body)

    def _built_body(self, nodes):
        self._body_builds += 1
        return self._build_body_index(nodes)

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
    def freshness(self, *, observed: bool = False) -> FreshnessResult:
        """The content-hash verdict, plus [ROOT-SET-CHANGE]: content hashes cover only the SEATED roots, so a
        registered root that appeared (or vanished) after the index was built left the verdict fresh while the corpus
        served something other than what its registry names. Every provider, the gate and the healer read freshness
        here, so one check covers them all; the healer already re-seats a structural root change. The root-set check runs
        FIRST, before the content pass; its cost is one `stat` per REGISTERED root (not per file)."""
        # Root set FIRST: a vanished seated root would otherwise make the content pass raise, every query.
        changed = self._root_set_change(observed=observed)
        # [INV-LTS-FRESH] [MECH-BR-CALLS]: the choke point EVERY provider, the gate, the router, the healer and the listing read
        # freshness through. A corpus with a pending or unresponsive root is never fresh, however equal its last-read hashes are:
        # its maintained root describes a moment before the root stopped answering. Checked AFTER the root-set pass, which is
        # what can mark a root that had not been marked when the call began.
        k = self._kernel
        mark = k.root_mark(self) if k is not None and hasattr(k, "root_mark") else None
        if mark is not None:
            return FreshnessResult(self.corpus_id, False, self.files_hash, None, SKIP_REASON[mark["status"]])
        if changed is not None:
            return FreshnessResult(self.corpus_id, False, self.files_hash, None, "root-set-changed", changed)
        return self._content_freshness()

    def _live_root_set(self, ci, observed: bool = False) -> set:
        """The registered roots that are present, as absolute paths. [MECH-BR-PROBE] With a kernel attached this is the answer
        its workers KEPT from their last probe of each root (the probe a named read waits for ran after the read began), so the
        query path makes no file-system call here, and a marked root's disk is never touched. A root the kernel has not
        observed yet counts as unchanged (the seated set): it cannot happen after a seat, which observes every declared root,
        and this path must not read a disk. With no kernel (a library caller, a test) it asks the disk, as it always did."""
        k = self._kernel
        if k is not None and hasattr(k, "live_root_paths"):
            if not observed:
                # a caller that did not just probe asks the roots' workers (bounded by the root deadline) rather than the disk;
                # `observed=True` is the provider that polled a moment ago, whose probe included this observation
                k.refresh_observations(self)
            live = k.live_root_paths(self)
            if live is not None:
                return live
            return {os.path.abspath(r) for r in (self.root_paths or [])}
        return {p for _spec, p in ci.live_roots()}

    def _root_set_signature(self):
        """None when the root set is unchanged; else (gone roots, fingerprints of every live root, seated and new).
        A root's fingerprint (`_root_fingerprint`) is its own mode, every `.lts` entry's (path, mode, size, mtime,
        ctime) or the error reading it, and for a symlinked entry also its target's (mode, size, mtime, ctime) or the
        error. The re-seat
        reads the seated roots as well as the new ones and follows member links, so these are the states a re-seat
        failure that the engine itself reports (a FIFO, an unreadable or missing member, an unreadable directory, a
        broken or unreadable link target, including unreadability set by an ACL or owner change) depends on; covering only the new roots (round-2 P1-A1), or only the link
        and not its target (round-3 P1-C1), left the key fixed after such a fault was repaired. The self-heal storm
        guard keys on it, so a failed re-seat is retried once that state changes. A change outside it (a non-`.lts`
        file) does not move the key, and neither does a fix made outside the file system (for example granting the
        engine's process a permission): such a cause is retried only after something in the fingerprint changes. When a seated root is GONE the corpus is refused fail-closed and the
        fingerprints are not used, so none are computed. Cost otherwise: one names-and-stat walk of the live roots
        per query, only while the root set differs from the seated one."""
        ci = getattr(self, "corpus_index", None)
        if ci is None or not hasattr(ci, "live_roots"):
            return None
        live = self._live_root_set(ci)
        seated = {os.path.abspath(r) for r in (self.root_paths or [])}
        if live == seated:
            return None
        gone = tuple(sorted(seated - live))
        if gone:
            return (gone, ())                # refused fail-closed by the healer; no walk (round-3 P3-C2)
        return (gone, tuple((r, _root_fingerprint(r)) for r in sorted(live)))

    def _root_set_change(self, observed: bool = False) -> str | None:
        """None when the roots that resolve on disk are the roots the index was built from; else a description (it can
        hold paths: it travels only as `detail`, which is never persisted)."""
        ci = getattr(self, "corpus_index", None)
        if ci is None or not hasattr(ci, "live_roots"):
            return None
        live = self._live_root_set(ci, observed=observed)
        seated = {os.path.abspath(r) for r in (self.root_paths or [])}
        if live == seated:
            return None
        return (f"registered roots changed since the index was built: appeared {sorted(live - seated)}, "
                f"gone {sorted(seated - live)}")

    def _content_freshness(self) -> FreshnessResult:
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


class StoreSealed(RuntimeError):
    """The store is in registry-error mode (`clear_all` ran): nothing is seated into it any more. Raised INSIDE the locked step that would seat a corpus, so a register
    that passed the registry gate just before the mode began cannot leave a corpus in the store of a server that serves none."""


class CorpusPresent(ValueError):
    """The corpus id is already served or pending at the moment of the seat (`state` says which). A ValueError, as `add_partition`'s clobber refusal
    always was."""

    def __init__(self, corpus_id: str, state: str):
        super().__init__(f"corpus_id already loaded (would clobber): {corpus_id!r}" if state == "served" else f"corpus_id is pending: {corpus_id!r}")
        self.corpus_id, self.state = corpus_id, state


def failed_record(code: str, path: str | None, private: bool, domain_suffix: str | None, **extra) -> dict:
    """THE record of a corpus that is registered but not served ([INV-BR-NOT-EMPTY], the contract of `store._failed_corpora`). Every writer builds it here:
    `code` (a stable code), `path` (root-relative member file, or None when there is none), `private` (decided when the failure was recorded: a failed
    corpus has no partition to carry it) and `domain_suffix` (so a hop into the corpus can be DECLARED, never dropped). `extra` carries what only some
    codes have (`problems` of a registry-key failure, `reason` of a corpus-root failure)."""
    out = {"code": code, "path": path, "private": bool(private), "domain_suffix": domain_suffix}
    out.update(extra)
    return out


class LtsStore:
    """A registry of independent Partitions. Selects/filters by corpus_id or domain.

    [INV-LTS-CORPUS-ISOLATION]: no method ever fuses two partitions' nodes/edges.
    `get_node` is ALWAYS scoped by corpus_id, so a colliding id cannot clobber.
    """

    def __init__(self):
        self._by_corpus_id: dict[str, Partition] = {}
        self._by_domain: dict[str, list[str]] = {}
        # The incremental freshness kernel (attached by server.build_tools); held for
        # its process lifetime so the background watcher stays alive.
        self.kernel: object | None = None
        # corpus_id -> {"code": <corpus-file-* code>, "path": <root-relative path>}: corpora whose build failed LOUD because
        # a member `.lts` is not a readable regular file ([DEC-LTS-NONREGULAR-FILE-IN-ROOT-HANGS-OR-MISREPORTS]). Such a
        # corpus is NOT served (never a partial view) and is REPORTED (`lts_list_corpora`, and the corpus-scoped tools).
        self._failed_corpora: dict[str, dict[str, str]] = {}
        # A registered corpus whose every root is missing: {corpus_id: {spec, private}}. Not served; declared by lts_list_corpora.
        self._no_roots_corpora: dict[str, dict] = {}
        # [MECH-BR-BOOT] x-server: a corpus registered but NOT READ YET: {corpus_id: {spec, private, paths, since, stuck}}. It has no partition, so it
        # is in none of `corpus_ids()`, `partitions()` and `get_partition`, and it is never an empty corpus: a call naming it waits for its read
        # (`wait_loaded`), a listing reports it pending with null totals, and a corpus-omitted read declares it. It leaves this table when its read
        # ends: into `_by_corpus_id` (served), `_failed_corpora` or `_no_roots_corpora`. Written by the boot loader only.
        self._pending_corpora: dict[str, dict] = {}
        self._pending_cv = threading.Condition()
        self._order: list[str] = []                 # the registry's corpus order: partitions are kept in it whatever order their reads end in
        # [MECH-BR-BOOT] x-load-error (1): the corpora the release does NOT serve whose entry's roots cannot be read as paths (`config.unstated_location`).
        # While one stands a write is never attributed to anything (it lands flagged, never refused, never "outside every root"). Written by the boot loader
        # (`add_unlocated`, when the corpus ends unserved or fails on a key) and cleared by `clear_unlocated` (register, unregister) or `clear_all`; read once
        # per write into the view, BEFORE the write's look. It decides NO boot verdict.
        self._unlocated: frozenset = frozenset()
        # [MECH-BR-BOOT] x-load-error: True once registry-error mode emptied this store (`clear_all`). The mode ends only by a restart, so it is never cleared. It is the one
        # attribute `clear_all` SETS instead of emptying (the coverage test names it exempt), and it makes `add_partition_clearing` refuse inside its locked step.
        self._sealed: bool = False

    # --- registration ---------------------------------------------------------
    def add_partition(self, partition: Partition) -> Partition:
        cid = partition.corpus_id
        with self._pending_cv:        # ONE lock covers add, remove and settle (it is re-entrant: settle calls this): no writer sees another half-done
            if self._sealed:
                raise StoreSealed("registry-error mode")
            if cid in self._by_corpus_id:
                raise ValueError(f"corpus_id already loaded (would clobber): {cid!r}")
            self._by_corpus_id[cid] = partition
            self._by_domain.setdefault(partition.domain_suffix, []).append(cid)
            return partition

    def declare_pending(self, corpus_id: str, rec: dict) -> None:
        with self._pending_cv:
            if self._sealed:
                return                                         # registry-error mode: nothing is declared into a sealed store
            self._pending_corpora[corpus_id] = rec
            self._pending_cv.notify_all()

    def settle(self, corpus_id: str, *, partition: Partition | None = None, failed: dict | None = None,
               no_roots: dict | None = None, only_if_pending: bool = True, unlocated: bool = False) -> bool:
        """The end of a pending corpus's read, in ONE step under the store's condition: the corpus leaves `_pending_corpora` and enters exactly one of
        the served partitions, `_failed_corpora` or `_no_roots_corpora`, so a reader never finds it in none (it would read as unknown) or in two. A
        corpus no longer pending (unregistered while it was read) is not published, and False says so; so is one that is already served (its stale pending record is dropped).
        `unlocated=True` records the corpus in `_unlocated` IN THE SAME STEP (its roots cannot be read as paths): a reader never finds it in the no-roots table
        and not yet flagged ([STORE-TABLES-ONE-SNAPSHOT-ONE-STEP] covers `_unlocated` too)."""
        with self._pending_cv:
            if self._sealed:
                return False                                   # registry-error mode: nothing is published into a sealed store
            if only_if_pending and corpus_id not in self._pending_corpora:
                return False
            if partition is None and corpus_id in self._by_corpus_id:
                # it is SERVED already (a stale pending record): the move to failed / no roots is refused INSIDE this step, the pending record is dropped
                # and nothing is recorded, so the corpus is never served and failed at once
                self._pending_corpora.pop(corpus_id, None)
                self._pending_cv.notify_all()
                return False
            if partition is not None:
                self.add_partition(partition)
                self._reorder()
            elif failed is not None:
                self._failed_corpora[corpus_id] = failed
            elif no_roots is not None:
                self._no_roots_corpora[corpus_id] = no_roots
            if unlocated and partition is None:
                self._unlocated = self._unlocated | {corpus_id}
            self._pending_corpora.pop(corpus_id, None)
            self._pending_cv.notify_all()
            return True

    def consistent(self):
        """The store's one lock, for a reader that must read the store AND another state that moves with it in a single step: the listing reads the tables and
        the engine's registry-error state together, and a write's view reads the tables and `unlocated` together, so a corpus that settles unserved and flagged
        is never seen in the no-roots table without its flag, nor flagged while still pending. It is re-entrant: the store's own methods may be called inside
        it. Do no disk work inside."""
        return self._pending_cv

    def lookup(self, corpus_id: str):
        """(partition, pending record, failed record) for one corpus, read together under the condition `settle` holds, so a corpus that ends its read
        between two looks is never found in none of them (it would read as an unknown corpus)."""
        with self._pending_cv:
            return (self._by_corpus_id.get(corpus_id), self._pending_corpora.get(corpus_id), self._failed_corpora.get(corpus_id))

    def snapshot(self):
        """(served partitions, a copy of the pending table) taken together under the condition `settle` also holds, so a corpus is in exactly one of them."""
        with self._pending_cv:
            return list(self._by_corpus_id.values()), dict(self._pending_corpora)

    def snapshot_all(self):
        """(served partitions, pending, failed, no_roots) copies taken together under the lock: a listing built from them never finds a corpus that
        moved between two of its reads in none of the four."""
        with self._pending_cv:
            return (list(self._by_corpus_id.values()), dict(self._pending_corpora), dict(self._failed_corpora), dict(self._no_roots_corpora))

    def add_partition_clearing(self, partition: Partition):
        """Seat a served partition and drop its failed / no-roots records in ONE step (a re-register of a boot-failed corpus): the corpus is never in
        two tables. It REFUSES (`CorpusPresent`, nothing changed) if the id is served or pending at this moment: check and seat are one locked step. Returns the dropped `(failed, no_roots)` records, so a roll-back (`remove_partition_restoring`) can put them back."""
        with self._pending_cv:
            if self._sealed:
                raise StoreSealed("registry-error mode")      # checked in the SAME step as the seat: a register that passed the gate before the mode began seats nothing
            cid = partition.corpus_id
            if cid in self._by_corpus_id:                    # the precondition is checked HERE, in the step that seats, never before the build
                raise CorpusPresent(cid, "served")
            if cid in self._pending_corpora:                  # the boot loader (or the watcher's retry) holds it: a second owner would end in two tables
                raise CorpusPresent(cid, "loading")
            self.add_partition(partition)
            dropped = (self._failed_corpora.pop(partition.corpus_id, None), self._no_roots_corpora.pop(partition.corpus_id, None))
            self._pending_cv.notify_all()
            return dropped

    def remove_partition_restoring(self, corpus_id: str, failed: dict | None, no_roots: dict | None) -> None:
        """The inverse of `add_partition_clearing`, in one step: the partition leaves and the records it dropped return, so a refused re-register
        leaves the corpus listed as failed, with its code, and is never in none of the tables."""
        with self._pending_cv:
            self.remove_partition(corpus_id)
            if self._sealed:
                return                                         # registry-error mode: the partition leaves, the records do NOT come back into a sealed store
            if failed is not None:
                self._failed_corpora[corpus_id] = failed
            if no_roots is not None:
                self._no_roots_corpora[corpus_id] = no_roots
            self._pending_cv.notify_all()

    def fail(self, corpus_id: str, code: str, path: str | None = None, private: bool = False, domain_suffix: str | None = None, **extra) -> dict:
        """Record `corpus_id` as failed, through `failed_record`, under the store's one lock. Returns the record."""
        rec = failed_record(code, path, private, domain_suffix, **extra)
        with self._pending_cv:
            if not self._sealed:                               # registry-error mode: nothing is recorded into a sealed store
                self._failed_corpora[corpus_id] = rec
                self._pending_cv.notify_all()
        return rec

    def move_failed_to_pending(self, corpus_id: str, rec, only_reason: str | None = None) -> bool:
        """Failed -> pending in ONE step under the lock (the watcher reads a profile-failed corpus again). A reader never finds the corpus in no table:
        it leaves `_failed_corpora` and enters `_pending_corpora` together. False (and nothing changes) if it is no longer failed, or, when
        `only_reason` is given, if its failed record no longer carries that `reason`: the precondition is read HERE, inside the step, from the record
        that is being moved. `rec` is the pending record, or a function of the failed record that builds it (so `private` and `domain_suffix` come from
        the record that is moved, not from one read before)."""
        with self._pending_cv:
            if self._sealed:
                return False
            old = self._failed_corpora.get(corpus_id)
            if old is None:
                return False
            if only_reason is not None and old.get("reason") != only_reason:
                return False
            del self._failed_corpora[corpus_id]
            self._no_roots_corpora.pop(corpus_id, None)
            self._pending_corpora[corpus_id] = rec(old) if callable(rec) else rec
            self._pending_cv.notify_all()
            return True

    def tables(self):
        """(pending, failed, no_roots) copies taken together under the lock: a decision that needs more than one of the unserved tables (the hop router's
        question 'which unserved corpus owns this domain') reads them from ONE snapshot, never from three separate reads that a move between them
        could straddle."""
        with self._pending_cv:
            return dict(self._pending_corpora), dict(self._failed_corpora), dict(self._no_roots_corpora)

    def lookup_all(self, corpus_id: str):
        """(partition, pending, failed, no_roots record) of one corpus, read together under the lock."""
        with self._pending_cv:
            return (self._by_corpus_id.get(corpus_id), self._pending_corpora.get(corpus_id), self._failed_corpora.get(corpus_id),
                    self._no_roots_corpora.get(corpus_id))

    def clear_no_roots(self) -> None:
        with self._pending_cv:
            self._no_roots_corpora.clear()

    def clear_all(self, also=None) -> list[str]:
        """Registry-error mode serves and lists NO corpus: empty all four tables in ONE locked step and return the ids that were served (the caller
        unseats them). The release's synchronous boot raised before any store existed, so a listing there shows nothing; a store the loader had already
        filled must say the same ([MECH-BR-BOOT] x-load-error: through the real server's boot). EVERY piece of store state is emptied (SDLC
        [STORE-TABLES-ONE-SNAPSHOT-ONE-STEP], and `tests/test_bounded_roots_boot.py::test_clear_all_empties_every_store_attribute`). `also`, if given, runs
        INSIDE the same locked step, BEFORE the state is emptied: the engine seats its registry error there, so a reader that takes the lock never finds the
        error beside a full store, or an empty store with no error, and a raising `also` leaves the store as it was."""
        with self._pending_cv:
            if also is not None:
                also()                      # FIRST: if it raises, nothing was emptied and the served ids are not lost (the store is untouched)
            gone = list(self._by_corpus_id)
            self._by_corpus_id = {}
            self._by_domain = {}
            self._pending_corpora.clear()
            self._failed_corpora.clear()
            self._no_roots_corpora.clear()
            self._unlocated = frozenset()
            self._order = []
            self._sealed = True
            self._pending_cv.notify_all()
            return gone

    def hold_for_verdict(self, corpus_id: str) -> bool:
        """The corpus's read yielded the registry-error verdict: it STAYS pending, now marked `verdict` so a write under its root waits for the mode's step itself, not a root
        deadline. The record is replaced whole (readers hold snapshots). False when it is no longer pending or the store is sealed (the check is inside the locked step)."""
        with self._pending_cv:
            rec = self._pending_corpora.get(corpus_id)
            if self._sealed or rec is None:
                return False
            self._pending_corpora[corpus_id] = {**rec, "verdict": True}
            self._pending_cv.notify_all()
            return True

    def forget_pending(self, corpus_id: str) -> bool:
        with self._pending_cv:
            gone = self._pending_corpora.pop(corpus_id, None) is not None
            self._pending_cv.notify_all()
            return gone

    def wait_loaded(self, corpus_id: str, timeout: float, stop=None) -> bool:
        """Wait until `corpus_id` is no longer pending, for at most `timeout` seconds; `stop()` (polled every 50 ms) ends the wait early. True iff it
        is no longer pending. Takes no kernel lock and touches no disk."""
        end = time.monotonic() + timeout
        with self._pending_cv:
            while corpus_id in self._pending_corpora:
                left = end - time.monotonic()
                if left <= 0 or (stop is not None and stop()):
                    return False
                self._pending_cv.wait(min(left, 0.05))
            return True

    def add_unlocated(self, corpus_id: str) -> None:
        with self._pending_cv:
            if not self._sealed:                               # registry-error mode: the store holds no state to flag
                self._unlocated = self._unlocated | {corpus_id}

    def clear_unlocated(self, corpus_id: str) -> None:
        with self._pending_cv:
            self._unlocated = self._unlocated - {corpus_id}

    def forget_unlocated(self, corpus_id: str) -> bool:
        """Unregister an entry the registry could not place, in one locked step: it leaves `unlocated` and its failed / no-roots record goes with it.
        True when it was recorded as unlocated and not served."""
        with self._pending_cv:
            if corpus_id not in self._unlocated or corpus_id in self._by_corpus_id:
                return False
            self._unlocated = self._unlocated - {corpus_id}
            self._failed_corpora.pop(corpus_id, None)
            self._no_roots_corpora.pop(corpus_id, None)
            self._pending_cv.notify_all()
            return True

    def unlocated(self) -> frozenset:
        """The corpora whose entry the registry cannot state a location for (immutable: one read is one consistent answer)."""
        return self._unlocated

    def set_order(self, corpus_ids) -> None:
        with self._pending_cv:
            self._order = list(corpus_ids)

    def _reorder(self) -> None:
        """Keep the served partitions in the registry's order (a read that ends first does not jump the queue); one atomic rebinding."""
        if not self._order:
            return
        with self._pending_cv:        # held by every writer, so the dict it reads is the dict it replaces
            rank = {c: i for i, c in enumerate(self._order)}
            ordered = sorted(self._by_corpus_id, key=lambda c: (rank.get(c, len(rank)), c))
            self._by_corpus_id = {c: self._by_corpus_id[c] for c in ordered}

    def remove_partition(self, corpus_id: str, failed: dict | None = None) -> Partition | None:
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

        `failed` (a record from `failed_record`): the corpus leaves the served table and enters `_failed_corpora` in the SAME locked step (a corpus
        that could not be seated), so a reader never finds it in both or in neither.
        """
        with self._pending_cv:        # the same lock as add_partition and settle
            part = self._by_corpus_id.pop(corpus_id, None)
            if part is None:
                return None
            if failed is not None:
                self._failed_corpora[corpus_id] = failed
                self._pending_cv.notify_all()
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
    def load(cls, index_paths: Iterable[str]) -> LtsStore:
        store = cls()
        for p in index_paths:
            store.load_partition(p)
        return store

    # --- selection / filtering ------------------------------------------------
    def corpus_ids(self) -> list[str]:
        return list(self._by_corpus_id.keys())

    def partitions(self) -> list[Partition]:
        return list(self._by_corpus_id.values())

    def get_partition(self, corpus_id: str) -> Partition | None:
        return self._by_corpus_id.get(corpus_id)

    def select(self, corpus_id: str) -> Partition | None:
        """Select exactly ONE corpus by id (a query is scoped to one corpus)."""
        return self._by_corpus_id.get(corpus_id)

    def filter_by_domain(self, domain_suffix: str) -> list[Partition]:
        """All partitions bearing a domain suffix (selection axis, [LTS-PARTITION])."""
        with self._pending_cv:
            return [self._by_corpus_id[c] for c in self._by_domain.get(domain_suffix, []) if c in self._by_corpus_id]

    # --- scoped node access (never global) ------------------------------------
    def get_node(self, corpus_id: str, node_id: str) -> dict | None:
        part = self._by_corpus_id.get(corpus_id)
        return part.get_node(node_id) if part else None

    # --- derived, per-partition stats -----------------------------------------
    def stats(self, corpus_id: str) -> dict | None:
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

    def freshness(self, corpus_id: str) -> FreshnessResult | None:
        part = self._by_corpus_id.get(corpus_id)
        return part.freshness() if part else None
