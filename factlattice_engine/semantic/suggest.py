"""[SEM-FIND] The hybrid semantic SUGGESTION service behind `lts_semantic_find` (and, later, the
`on_miss` block).

Ranking, as decided at S0 (lts-semantic-recall-v1, arctic-embed-m-v1.5): reciprocal-rank fusion
(k=60) of the deterministic BM25 (`lexrank`) and the embedding ranking (`index`), each to depth
50. There is NO score floor (measured useless). Confidence is AGREEMENT, not a score: the top
candidate is `high` only when both rankers and the fusion agree on it, the embedding margin is at
least HIGH_MARGIN, and no query term is unknown to the corpus. Everything else is `medium`.
Every candidate carries its matched and unmatched query terms and an evidence line.

Honesty (ratified [DEC-SEMANTIC-ASSIST]): this block is never `nodes`, never a match count,
`is_assertion: false`, and an empty list is not evidence of absence. Any failure of the model
stack is a reported STATUS, never an exception into the caller.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

from ..caution import caution_for
from . import cards as C
from .embedder import PINS, ModelPin, SemanticUnavailable, load_embedder
from .index import IndexIncompatible, SemanticIndex, index_version, partition_cards
from .lexrank import LexRank

DEFAULT_MODEL = "arctic-m15"
RRF_K, DEPTH, HIGH_MARGIN, SYNC_TOPUP_LIMIT = 60, 50, 0.02, 25
CONFIDENCE_WORD = {"high": "strong match", "medium": "related, not asserted"}
NOTE = ("Suggestions are ranked by relevance (keyword statistics fused with embedding "
        "similarity). They are NOT matches and NOT corpus assertions; an empty list is not "
        "evidence of absence. Read the node before relying on it.")


LABEL_POLICY_OFF = "strong-match disabled by deployment"
_WARNED_TOPUP: set = set()


def sync_topup_limit_from_env() -> int:
    """[SEM-TOPUP-LIMIT] LTS_SEMANTIC_SYNC_TOPUP_LIMIT=<int >= 0>, default SYNC_TOPUP_LIMIT; read per call.

    The most nodes a query re-embeds ON the query path (~90 ms each, measured 2026-10-05). Past it the
    stale index is served (`nodes_unembedded_stale` says so) and a background refresh starts."""
    raw = os.environ.get("LTS_SEMANTIC_SYNC_TOPUP_LIMIT")
    if raw is None or not raw.strip():
        return SYNC_TOPUP_LIMIT
    v = raw.strip()
    if v.isascii() and v.isdigit():          # ASCII digits only: no "1_0", "+3", or non-ASCII digits (review F3)
        return int(v)
    if raw not in _WARNED_TOPUP:             # a typo must never pass silently (P3-2 precedent) -- once per value
        _WARNED_TOPUP.add(raw)
        print(f"[lts-engine] WARNING: LTS_SEMANTIC_SYNC_TOPUP_LIMIT={raw!r} is not an integer >= 0; "
              f"using {SYNC_TOPUP_LIMIT}", file=sys.stderr, flush=True)
    return SYNC_TOPUP_LIMIT
_WARNED_HIGH_LABEL: set = set()


def high_label_enabled() -> bool:
    """[SEM-LABEL-CAP] LTS_SEMANTIC_HIGH_LABEL=on|off, default on; read per call."""
    raw = os.environ.get("LTS_SEMANTIC_HIGH_LABEL", "on")
    v = (raw or "on").strip().lower()
    if v not in ("on", "off"):   # a typo must never pass silently (P3-2 precedent) -- once per value
        if raw not in _WARNED_HIGH_LABEL:
            _WARNED_HIGH_LABEL.add(raw)
            print(f"[lts-engine] WARNING: LTS_SEMANTIC_HIGH_LABEL={raw!r} is not 'on' or 'off'; "
                  "using on (strong-match labels allowed)", file=sys.stderr, flush=True)
        return True
    return v == "on"


def top_label(bm25_top: Optional[str], emb: List[Tuple[str, float]], fused_top: Optional[str],
              unknown: List[str], margin: float = HIGH_MARGIN) -> str:
    if not emb or fused_top is None or bm25_top is None or unknown:
        return "medium"
    if not (bm25_top == emb[0][0] == fused_top):
        return "medium"
    m = emb[0][1] - emb[1][1] if len(emb) > 1 else 1.0
    return "high" if m >= margin else "medium"


def _evidence(cards: List[Tuple[str, str]], matched: List[str], width: int = 220) -> str:
    """The card text with the most matched terms, clipped around the first match."""
    from .lexrank import tokens
    def body(kind, text):
        # the head card opens with the spaced id and the title, which the caller already shows
        if kind != "head":
            return text
        lines = text.split("\n")
        # head = [spaced id, title, *property values, property-key line]; keep the values only
        return "\n".join(lines[2:-1]) if len(lines) >= 3 else ""
    texts = [(k, body(k, t)) for k, t in cards]
    texts = [(k, t) for k, t in texts if t.strip()]
    if not texts:
        return "(no text beyond the title)"
    best, best_n = (texts[0][1] if texts else ""), -1
    for _kind, text in texts:
        n = len(set(tokens(text)) & set(matched))
        if n > best_n:
            best, best_n = text, n
    flat = " ".join(best.split())
    low = flat.lower()
    pos = min([low.find(t) for t in matched if low.find(t) >= 0] or [0])
    start = max(0, pos - width // 3)
    snippet = flat[start:start + width]
    return ("…" if start else "") + snippet + ("…" if start + width < len(flat) else "")


def _pin_from_env() -> ModelPin:
    pin_file = os.environ.get("LTS_SEMANTIC_PIN_FILE")
    if pin_file:
        return ModelPin(**json.load(open(pin_file)))
    return PINS[os.environ.get("LTS_SEMANTIC_MODEL", DEFAULT_MODEL)]


# ⛔ [TOOL-REASON-IS-A-FIXED-CODE] a reply's top-level `reason` is a fixed code, `success: true` or not. The sentence that
# used to ride in it (it could hold an absolute path with any directory name in it) is `live_detail`: a LIVE-reply field
# the query log never reads and a caller must never persist.
LIVE_DETAIL_NOTE = "live_detail is for this live reply only: NEVER persist it"


def _code(status: str) -> str:
    return str(status).replace("_", "-")


def _live_reason(code: str, detail: str) -> dict:
    return {"reason": code, "live_detail": detail, "live_detail_note": LIVE_DETAIL_NOTE}


def _unavailable_reason(ex: "SemanticUnavailable") -> dict:
    return _live_reason(_code(ex.status), str(ex.detail))


class SemanticService:
    """One per engine process. Holds the (lazily loaded) embedder, a per-corpus card/BM25 cache
    keyed on the partition's freshness key, and a single bounded background index worker."""

    def __init__(self, state_root: Optional[str] = None, model_root: Optional[str] = None,
                 pin: Optional[ModelPin] = None, build_mode: str = "background",
                 sync_topup_limit: Optional[int] = None, retry_after_s: float = 300.0,
                 cache_max: int = 8):
        self.state_root = state_root or os.environ.get("LTS_ENGINE_STATE_DIR") or os.path.expanduser("~/.lts-engine")
        self.model_root = model_root
        self.pin = pin or _pin_from_env()
        self.model_id = f"{self.pin.name}@{self.pin.onnx_sha256[:12]}"
        # None = follow LTS_SEMANTIC_SYNC_TOPUP_LIMIT per call; an explicit value wins (tests, CLI)
        self.build_mode, self.sync_topup_limit = build_mode, sync_topup_limit
        self._embedder = None
        self._unavailable: Optional[SemanticUnavailable] = None
        from collections import OrderedDict
        self.cache_max = cache_max           # [composition F-G1] per-corpus caches are LRU-bounded
        self._corpus: "OrderedDict[str, dict]" = OrderedDict()
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._queue: Dict[str, object] = {}
        self._current: Optional[str] = None
        self._failed: Dict[str, Tuple[float, str]] = {}   # corpus -> (when, reason)  [review F6]
        self._indexes: "OrderedDict[str, SemanticIndex]" = OrderedDict()
        self.retry_after_s = retry_after_s

    # --- model -------------------------------------------------------------------------
    def _get_embedder(self):
        if self._embedder is None and self._unavailable is None:
            mode = os.environ.get("LTS_SEMANTIC_EMBEDDER", "local") or "local"
            if mode not in ("local", "shared"):   # P3-2: a typo must never pass silently
                print(f"[lts-engine] WARNING: LTS_SEMANTIC_EMBEDDER={mode!r} is not 'local' or 'shared'; "
                      "using local (one model in this process)", file=sys.stderr, flush=True)
                mode = "local"
            if mode == "shared":
                # [MECH-SEMANTIC-EMBEDD] one model per user; connects lazily, fails per call
                from .embedd import SharedEmbedder
                self._embedder = SharedEmbedder(self.pin, self.model_root, state_root=self.state_root)
                return self._embedder
            try:
                self._embedder = load_embedder(self.pin, self.model_root)
            except SemanticUnavailable as ex:
                self._unavailable = ex
        if self._unavailable is not None:
            raise self._unavailable
        return self._embedder

    # --- per-corpus deterministic state --------------------------------------------------
    def effective_sync_topup_limit(self) -> int:
        """[SEM-TOPUP-LIMIT] the constructor's value if given, else the env (default SYNC_TOPUP_LIMIT)."""
        return self.sync_topup_limit if self.sync_topup_limit is not None else sync_topup_limit_from_env()

    def _state(self, part) -> dict:
        key = part._freshness_key()
        st = self._corpus.get(part.corpus_id)
        if st is None or st["key"] != key:
            cards = partition_cards(part)
            st = {"key": key, "cards": cards, "lex": LexRank(cards),
                  "fps": {n: C.card_fingerprint(c) for n, c in cards.items()}}
        self._corpus[part.corpus_id] = st
        self._corpus.move_to_end(part.corpus_id)
        while len(self._corpus) > self.cache_max:
            self._corpus.popitem(last=False)
        return st

    # --- index lifecycle -----------------------------------------------------------------
    def _index(self, corpus_id: str) -> SemanticIndex:
        idx = self._indexes.get(corpus_id)
        if idx is None:
            idx = SemanticIndex(self.state_root, corpus_id, self._get_embedder())
        self._indexes[corpus_id] = idx
        self._indexes.move_to_end(corpus_id)
        while len(self._indexes) > self.cache_max:
            self._indexes.popitem(last=False)
        return idx

    def _start_background(self, part) -> None:
        """[review F6] The worker clears itself under the SAME lock this checks, so a corpus
        queued while a build is finishing can never be stranded."""
        with self._lock:
            self._queue[part.corpus_id] = part
            if self._worker is None:
                worker = threading.Thread(target=self._drain, name="lts-semantic-index", daemon=True)
                try:
                    worker.start()
                except Exception:      # [delta F-D7] never leave the slot wedged
                    self._queue.pop(part.corpus_id, None)
                    raise
                self._worker = worker

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._worker, self._current = None, None
                    return
                cid, part = self._queue.popitem()
                self._current = cid
            try:
                self._index(cid).refresh(part)
                self._failed.pop(cid, None)
            except Exception as ex:   # noqa: BLE001 -- recorded, reported as index_failed [F6]
                self._failed[cid] = (time.monotonic(), f"{type(ex).__name__}: {ex}")

    def wait_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                w = self._worker
            if w is None:
                return True
            w.join(min(0.5, max(0.0, deadline - time.monotonic())))
        with self._lock:
            return self._worker is None

    def _building(self, corpus_id: str) -> bool:
        with self._lock:
            return corpus_id in self._queue or self._current == corpus_id

    def _ensure(self, part, st) -> Tuple[Optional[SemanticIndex], dict]:
        """Returns (index or None, info). info: status, index_fresh, nodes_unembedded_stale,
        embedded_on_query."""
        idx = self._index(part.corpus_id)
        info = {"status": "ok", "index_fresh": True, "nodes_unembedded_stale": 0, "embedded_on_query": 0}
        try:
            _man, _V, _ids, _kinds, fps_old = idx._load()
        except IndexIncompatible as ex:
            failed = self._failed.get(part.corpus_id)
            if failed and not self._building(part.corpus_id):
                when, why = failed
                if time.monotonic() - when < self.retry_after_s:
                    return None, {**info, "status": "index_failed", "index_fresh": False,
                                  **_live_reason("index-build-failed",
                                                 f"the last index build failed ({why}); retry in "
                                                 f"{max(1, int(self.retry_after_s - (time.monotonic() - when)))} s")}
                self._failed.pop(part.corpus_id, None)
            if self.build_mode == "sync":
                try:
                    idx.refresh(part, interactive=True)
                except SemanticUnavailable as sx:     # e.g. the shared service is down: labelled
                    return None, {**info, "status": "unavailable", "index_fresh": False,
                                  **_unavailable_reason(sx)}
                info["embedded_on_query"] = len(st["fps"])
                return idx, info
            if not self._building(part.corpus_id):
                try:
                    self._start_background(part)
                except Exception as sx:   # noqa: BLE001 [delta F-D7]
                    return None, {**info, "status": "unavailable", "index_fresh": False,
                                  **_live_reason("index-worker-start-failed",
                                                 f"could not start the index worker: {type(sx).__name__}: {sx}")}
            return None, {**info, "status": "index_building", "index_fresh": False,
                          **_live_reason(_code(ex.status), "building the semantic index for this corpus "
                                                           "in the background; ask again shortly")}
        changed = sum(1 for n, fp in st["fps"].items() if fps_old.get(n) != fp) + \
            sum(1 for n in fps_old if n not in st["fps"])
        if changed == 0:
            return idx, info
        stale = {**info, "index_fresh": False, "nodes_unembedded_stale": changed}
        failed = self._failed.get(part.corpus_id)
        if failed and time.monotonic() - failed[0] < self.retry_after_s:    # [delta F-D3] cooled down
            return idx, {**stale, "refresh_failed": failed[1]}
        if changed <= self.effective_sync_topup_limit() or self.build_mode == "sync":
            try:
                stats = idx.refresh(part, interactive=True)
            except Exception as ex:   # noqa: BLE001 [delta F-D3] serve the stale index, say why
                self._failed[part.corpus_id] = (time.monotonic(), f"{type(ex).__name__}: {ex}")
                return idx, {**stale, "refresh_failed": self._failed[part.corpus_id][1]}
            self._failed.pop(part.corpus_id, None)
            info["embedded_on_query"] = stats["embedded_nodes"] + stats["dropped_nodes"]
            return idx, info
        if not self._building(part.corpus_id):
            try:
                self._start_background(part)
            except Exception as sx:   # noqa: BLE001 [delta F-D7]
                return idx, {**stale, "refresh_failed": f"could not start the index worker: {sx}"}
        return idx, stale

    # --- the suggestion block --------------------------------------------------------------
    def suggest(self, part, query: str, k: int = 10, keep=None) -> dict:
        """`keep(node_id, node) -> bool` is the caller's filter (prefix/status/area)."""
        base = {"mode": "semantic", "kind": "semantic-suggestions-not-matches", "is_assertion": False,
                "model": self.model_id, "index_version": None, "note": NOTE}
        try:
            base["index_version"] = index_version(self.model_id, self._get_embedder())
        except SemanticUnavailable as ex:
            return {**base, "status": "unavailable", **_unavailable_reason(ex),
                    "candidates": [], "index_fresh": False}
        st = self._state(part)
        idx, info = self._ensure(part, st)
        if idx is None:
            return {**base, **info, "candidates": []}
        keep = keep or (lambda nid, nd: True)
        ok = lambda nid: nid in part.nodes and keep(nid, part.nodes[nid])  # noqa: E731
        lex: LexRank = st["lex"]
        bm = [(n, s) for n, s in lex.search(query, DEPTH * 4) if ok(n)][:DEPTH]
        try:
            qv = self._get_embedder().embed([query], query=True)[0]
        except SemanticUnavailable as ex:              # [MECH-SEMANTIC-EMBEDD] never into the caller
            return {**base, "status": "unavailable", **_unavailable_reason(ex),
                    "candidates": [], "index_fresh": False}
        hits = idx.search(qv, DEPTH * 4)
        emb = [(n, s) for n, s, _v in hits if ok(n)][:DEPTH]
        via = {n: v for n, _s, v in hits}
        fused: Dict[str, float] = {}
        for ranked in (bm, emb):
            for r, (n, _s) in enumerate(ranked):
                fused[n] = fused.get(n, 0.0) + 1.0 / (RRF_K + r + 1)
        order = sorted(fused, key=lambda n: (-fused[n], n))[:k]
        unknown = lex.unknown_terms(query)
        bm_score, emb_score = dict(bm), dict(emb)
        first = top_label(bm[0][0] if bm else None, emb, order[0] if order else None, unknown)
        capped = not high_label_enabled()
        if capped:                                  # [SEM-LABEL-CAP] policy, not a judgement
            first = "medium"
        cands = []
        fwd_adj, rev_adj = part.adjacency()      # [CANONICAL-EDGE-CAUTION] the cached incidence map
        for i, nid in enumerate(order):
            label = first if i == 0 else "medium"
            matched, unmatched = lex.term_coverage(query, nid)
            cands.append({"rank": i + 1, "id": nid, "title": part.nodes[nid].get("title"),
                          "label": label, "confidence": CONFIDENCE_WORD[label],
                          "matched_terms": matched, "unmatched_terms": unmatched,
                          "evidence": _evidence(st["cards"].get(nid, []), matched),
                          "via": via.get(nid, "lexical"),
                          # [review F7 / ratified rule 2] the scores behind the rank
                          "scores": {"fused": round(fused[nid], 6),
                                     "embedding": (round(emb_score[nid], 4) if nid in emb_score else None),
                                     "bm25": (round(bm_score[nid], 4) if nid in bm_score else None)}})
            caution = caution_for(part, nid, fwd_adj, rev_adj)     # annotates; never reorders
            if caution:
                cands[-1]["caution"] = caution
        return {**base, **info, "candidates": cands, "unknown_terms": unknown,
                **({"label_policy": LABEL_POLICY_OFF} if capped else {}),
                # scores.embedding is the chunk-penalised cosine the ranking used [delta F-D9]
                "index": {"embedded_on_query": info["embedded_on_query"],
                          "nodes_unembedded_stale": info["nodes_unembedded_stale"]}}
