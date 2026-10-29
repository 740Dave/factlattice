"""factlattice_engine — lts-engine: the FactLattice LTS-tooling library.

ONE library over the LTS graph index, exposing FOUR composed API layers over the same
graph index + freshness gate. Per-project multi-tenant, read-only. Deliberately
security-free: it targets a single-operator, single local MCP-client deployment, so
there is no ACL / audit / identity layer — a missing or empty corpus is a plain
not-found, never a leak-guarded response.

Four API layers (each: a module, an MCP tool, a spec node in SPEC.md §4):
  1. QUERY     — tools.LtsEngine: find / find_in_prose / node / traverse / capabilities /
                 list_corpora (MCP: lts_find / lts_find_in_prose / lts_node /
                 lts_traverse / lts_capabilities / lts_list_corpora). The graph read
                 surface. `find`'s `text` reads STRUCTURE ONLY (id/title/string property
                 values); `find_in_prose` reads BODY TEXT and is a separate tool on
                 purpose ([FL-PROSE-BLIND], see `tools.find_in_prose`).
  2. ANALYTICS — analytics.LtsAggregator: count / group_by / numeric (sum/avg/min/max)
                 / distinct / cross_tab / top_k / degree / top_by_degree /
                 count_by_edge_type / orphans (MCP: lts_aggregate). A deterministic
                 fold over the QUERY layer's find — inherits its freshness gate.
  3. LINT      — lint.lint: structural diagnostics + bidirectional dangle-lint over the
                 SAME parser (MCP: lts_lint), so lints-clean == ingests-clean.
  4. RENDER    — render.render_mermaid + render.walk_flow ([LEC-RENDER], MCP: lts_flow):
                 a queried SUBGRAPH -> a Mermaid diagram, DERIVED at query time and
                 stored nowhere (so it cannot drift). The emitter is subgraph-GENERIC;
                 process flow is its first caller, not its only one. Routes through the
                 SAME `_gate()` choke point, so a stale corpus renders NO diagram.

Supporting modules:
  * store.LtsStore/Partition     — multi-tenant partition registry
  * router.Router                — cross-corpus routing
  * verdict.ContentHashFreshness — the content-hash freshness gate the query path consults
  * kernel.FreshnessKernel       — [LEC-FRESHNESS] the INCREMENTAL freshness kernel:
                                   content-hash leaves + an incrementally-maintained root,
                                   a stdlib mtime-hint polling watch, and a periodic full
                                   re-verify backstop. Makes on-query validation ~O(0).
  * incremental.CorpusIndex      — [LEC-SELF-HEAL] the SURGICAL re-index: a per-file parse
                                   cache that re-parses only the files whose content hash
                                   moved, then recomputes every cross-file property in
                                   full (so a healed index == a cold-start index).
  * selfheal.SelfHealer          — [LEC-SELF-HEAL] makes a STALE verdict TRANSIENT:
                                   rebuild synchronously on the query path, re-verify,
                                   then serve. Fail-closed, coalesced, storm-guarded.
  * lifecycle                    — the `status` LIFECYCLE TOKEN: `lts_find(status=…)`
                                   matches a bare token against the qualified forms the
                                   corpus authors (`proposed — awaiting ratify`), which
                                   an exact-string filter silently missed. Paired with
                                   lint rule V11, which flags the drift it tolerates.
  * config                       — corpus_id -> root mapping (a generic example seed)
  * server                       — thin stdio MCP wrapper over the transport-agnostic dispatch
"""

from __future__ import annotations

from .lifecycle import is_bare_lifecycle_token, lifecycle_token, status_matches
from .store import FreshnessResult, LtsStore, Partition, combined_files_hash
from .router import AclCheck, AllowAllAcl, RouteResult, Router, StalePartitionError
from .reverse_index import Referrer, ReverseIndex, build_domain_index, snapshot_key
from .verdict import ContentHashFreshness, FreshnessProvider, PollVerifiedFreshness
from .kernel import FreshnessKernel, RootState, combine_leaf_map, combine_roots
from .incremental import CorpusIndex, CorpusMoved, RootIndex
from .selfheal import HealOutcome, SelfHealer
from .tools import (
    DEFAULT_DATE_PROPERTY,
    LTS_AGGREGATE_OPS,
    LTS_ALL_TOOL_NAMES,
    LTS_ANALYTICS_TOOL_NAMES,
    LTS_AUTHORING_TOOL_NAMES,
    LTS_LINT_TOOL_NAMES,
    LTS_REGISTRY_TOOL_NAMES,
    LTS_RENDER_TOOL_NAMES,
    LTS_SELFCHECK_TOOL_NAMES,
    LTS_TOOL_NAMES,
    LtsEngine,
    dispatch,
    normalize_date,
    reset_server_tools,
    set_server_tools,
)

__version__ = "0.1.0"

__all__ = [
    "LtsStore", "Partition", "FreshnessResult", "combined_files_hash",
    "Router", "RouteResult", "AclCheck", "AllowAllAcl", "StalePartitionError",
    "Referrer", "ReverseIndex", "build_domain_index", "snapshot_key",
    "ContentHashFreshness", "FreshnessProvider", "PollVerifiedFreshness",
    "FreshnessKernel", "RootState", "combine_leaf_map", "combine_roots",
    "CorpusIndex", "CorpusMoved", "RootIndex", "SelfHealer", "HealOutcome",
    "LtsEngine", "dispatch", "set_server_tools", "reset_server_tools",
    "LTS_TOOL_NAMES", "LTS_ANALYTICS_TOOL_NAMES", "LTS_LINT_TOOL_NAMES",
    "LTS_RENDER_TOOL_NAMES", "LTS_SELFCHECK_TOOL_NAMES", "LTS_REGISTRY_TOOL_NAMES",
    "LTS_ALL_TOOL_NAMES", "LTS_AGGREGATE_OPS", "LTS_AUTHORING_TOOL_NAMES",
    "DEFAULT_DATE_PROPERTY", "normalize_date",
    "lifecycle_token", "is_bare_lifecycle_token", "status_matches",
]
