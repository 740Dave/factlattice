"""Phase 2 — the cross-corpus edge ROUTING layer ([LTS-XCORPUS-EDGE]).

============================================================================
The routing logic (`_resolve_corpus`, `route`, `route_edge`) is constructed with
`AllowAllAcl()` + `ContentHashFreshness()` as the PERMANENT defaults. The engine
carries no security/governance layer: there is no audit dispatcher, no
leak-free timing ceremony, and no confidentiality gating. The ACL seam stays a
Protocol with a permissive stub — the target deployment has one operator and one
local MCP client, so there is no other tenant to gate against.
============================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple

from .store import FreshnessResult, LtsStore, Partition


# ---------------------------------------------------------------------------
# ACL seam — HOOK ONLY, permanently permissive (the security layer is CUT).
# ---------------------------------------------------------------------------
class AclCheck(Protocol):
    """The pluggable ACL boundary. lts-engine ships only the permissive stub."""

    def can_read(self, caller_identity: object, corpus_id: str) -> bool: ...


class AllowAllAcl:
    """Every corpus reads as ALLOW. The permanent lts-engine policy: no ACL layer."""

    def can_read(self, caller_identity: object, corpus_id: str) -> bool:
        return True


class StalePartitionError(RuntimeError):
    """Raised when a routed hop targets a STALE partition — the loud fail that
    forbids silently serving the entry corpus's fresh view as complete."""

    def __init__(self, freshness: FreshnessResult):
        self.freshness = freshness
        super().__init__(
            f"routed partition {freshness.corpus_id!r} is STALE "
            f"(index={freshness.index_hash} live={freshness.live_hash} "
            f"reason={freshness.reason} detail={freshness.detail}); refusing to serve stale [INV-LTS-FRESH]"
        )


@dataclass
class RouteResult:
    ok: bool
    status: str  # resolved | not_found | unresolved_corpus | ambiguous_domain
    corpus_id: Optional[str]
    node_id: Optional[str]
    node: Optional[dict]
    freshness: Optional[FreshnessResult]


class Router:
    """Thin routing layer over a LtsStore. Holds no graph of its own."""

    def __init__(self, store: LtsStore, acl: Optional[AclCheck] = None,
                 freshness_provider: object = None, healer: object = None):
        self.store = store
        self.acl: AclCheck = acl if acl is not None else AllowAllAcl()
        if freshness_provider is None:
            from .verdict import ContentHashFreshness
            freshness_provider = ContentHashFreshness()
        self.freshness_provider = freshness_provider
        # [LEC-SELF-HEAL] a routed hop is a gated surface too: the TARGET partition gets
        # the same heal-then-re-verify treatment as the entry corpus, so one stale
        # neighbour no longer permanently amputates cross-corpus routing.
        self.healer = healer
        # Boundary-call ledger so tests can assert the ACL hook is CALLED at the seam.
        self.acl_calls: List[Tuple[object, str]] = []

    def _evaluate_freshness(self, part: Partition) -> FreshnessResult:
        fr = self.freshness_provider.evaluate(part)
        if fr.fresh or self.healer is None:
            return fr
        self.healer.heal(part)
        return self.freshness_provider.evaluate(part)  # re-verify; never trust the heal

    def _resolve_corpus(self, target_domain: str) -> Tuple[str, Optional[Partition]]:
        """Resolve a domain suffix to exactly one partition (or a status)."""
        candidates = self.store.filter_by_domain(target_domain)
        if not candidates:
            return "unresolved_corpus", None
        if len(candidates) > 1:
            # Suffix is a selection key, not an identity — surface ambiguity.
            return "ambiguous_domain", None
        return "resolved", candidates[0]

    def route(self, target_domain: str, node_id: str,
              caller_identity: object = None) -> RouteResult:
        """Resolve `[target_domain:…:node_id]` to a node in the TARGET partition,
        applying the (permissive) ACL hook then the target's freshness check."""
        status, part = self._resolve_corpus(target_domain)
        if part is None:
            return RouteResult(False, status, None, node_id, None, None)

        fresh = self._evaluate_freshness(part)

        self.acl_calls.append((caller_identity, part.corpus_id))
        if not self.acl.can_read(caller_identity, part.corpus_id):
            return RouteResult(False, "not_found", None, None, None, None)

        # A routed hop into a STALE partition FAILS LOUD for THAT partition.
        if not fresh.fresh:
            raise StalePartitionError(fresh)

        node = part.get_node(node_id)
        if node is None:
            return RouteResult(False, "not_found", part.corpus_id, node_id, None, fresh)
        return RouteResult(True, "resolved", part.corpus_id, node_id, node, fresh)

    def route_edge(self, edge: dict, caller_identity: object = None) -> RouteResult:
        """Route a cross-corpus edge dict (uses its authored `target_domain`)."""
        target_domain = edge.get("target_domain")
        if not target_domain:
            return RouteResult(False, "unresolved_corpus", None, edge.get("target"),
                               None, None)
        return self.route(target_domain, edge["target"], caller_identity)
