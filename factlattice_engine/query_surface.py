"""[FIND-QUERY-SURFACE] the lts_find query surface: terms, match modes, property conditions,
document filters, the echo and the phrase-zero fallback's word split.

ONE module holds compile / validate / match / echo, so the three tools that share
`tools._node_matches` (lts_find, lts_find_in_prose, lts_aggregate-through-find) cannot drift
([SPEC §0 C4]). Nothing here parses a string form: every parameter is structured JSON
(The maintainer ruled any inline syntax out, 2026-10-05).

THE CONTRACT WITH THE OLD SURFACE (C2). `compile_query` returns None when a call uses none
of the new parameters, and `_node_matches` given `cq=None` is byte-for-byte today's
predicate. The compiled query is built ONCE per call and handed to the predicate.

Field reading for terms is exactly `_structure_hit_in`'s field set: the id, the title, every
string property value and every string ELEMENT of a list-valued property (`_string_values`);
mappings and body prose are excluded. `fields` restricts it.
"""
from __future__ import annotations

import bisect
import copy
import os
import re
import sys
import threading
import weakref
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

MATCH_MODES = ("phrase", "word", "prefix")
WHERE_OPS = ("has", "lacks", "equals", "one_of", "not_equals", "not_one_of")
# every parameter whose use turns the echo on ([SPEC §5])
NEW_PARAMS = ("all_of", "any_of", "none_of", "match", "where", "fields",
              "source_file", "document_tag", "explain")

WORD_RE = re.compile(r"[a-z0-9]+")
KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
ID_SHAPE_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:[-_:][A-Z0-9]+)+$")
MIN_PREFIX_LEN = 2
CACHE_MAX = 8                     # per-corpus word/field caches, LRU (same bound as LEXRANK_CACHE_MAX)
CACHE_MB_ENV = "LTS_FIND_CACHE_MAX_MB"   # [FIND-QUERY-SURFACE-6] total byte ceiling across the caches
CACHE_MB_MAX = 4096               # upper sanity bound: a typo (an extra zero or three) must not mean unbounded
CACHE_MB_DEFAULT = 150            # ~150 MB per process; six agent sessions together stay under ~1 GB
FALLBACK_ENV = "LTS_FIND_WORD_FALLBACK"
FALLBACK_CAP = 20

COMBINE = ("every listed condition must hold (AND); any_of needs at least one hit; "
           "none_of excludes")
TEXT_MODE = "phrase (whole text as one substring of one field)"


class QueryError(ValueError):
    """A refused query. The message names the parameter; it is returned as `error`."""


def words_of(s: str) -> List[str]:
    """Word mode's words: `[a-z0-9]+` runs of the lowercased text. No stemming, no stop-words."""
    return WORD_RE.findall(s.lower())


_SV = None


def _string_values(v):
    global _SV
    if _SV is None:                       # lazy: tools imports this module
        from .tools import _string_values as sv
        _SV = sv
    return _SV(v)


# --------------------------------------------------------------------------- fallback env
_WARNED_FALLBACK: set = set()


def fallback_enabled() -> bool:
    """LTS_FIND_WORD_FALLBACK=on|off, default off, read per call. An unknown value warns
    once per value and means off (the LTS_SEMANTIC_HIGH_LABEL precedent)."""
    raw = os.environ.get(FALLBACK_ENV)
    if raw is None:
        return False
    v = raw.strip().lower()
    if v == "on":
        return True
    if v in ("off", ""):
        return False
    if raw not in _WARNED_FALLBACK:
        _WARNED_FALLBACK.add(raw)
        print(f"[lts-engine] WARNING: {FALLBACK_ENV}={raw!r} is not 'on' or 'off'; using off",
              file=sys.stderr, flush=True)
    return False


def fallback_words(text: str) -> List[str]:
    """The words of a `text` for the fallback: word-mode words minus lexrank's STOP list,
    de-duplicated in order of first appearance."""
    from .semantic.lexrank import STOP
    out: List[str] = []
    for w in words_of(text):
        if w not in STOP and w not in out:
            out.append(w)
    return out


def is_id_shaped(text: str) -> bool:
    return bool(ID_SHAPE_RE.match((text or "").strip()))


# --------------------------------------------------------------------------- field cache
_PUBLISH_LOCK = threading.Lock()   # held only to publish one entry's words (cold builds only)


class _Entry:
    """One node's structured fields, lowercased once; words derived lazily."""
    __slots__ = ("lows", "_words", "_union", "_fc")

    def __init__(self, lows, fc=None):
        self.lows = lows              # tuple of (field_name, lowered_string)
        self._words = None
        self._union = None
        # owning FieldCache (WEAK: a strong ref makes entry<->cache a cycle that outlives eviction until
        # the cyclic GC runs); told how many bytes the lazy words add
        self._fc = weakref.ref(fc) if fc is not None else None

    def words(self):
        if self._words is None:
            # [words/union race, internal ticket] build into locals, then publish under a lock held
            # only for the check-and-two-assignments: `_union` BEFORE `_words`, so a reader that sees
            # `_words` set always sees `_union` set (it used to get None, and every caller raised
            # TypeError). A builder that loses the race publishes nothing and counts no bytes, so racing
            # builders grow the estimate exactly once (review F1 at 54c8f9c).
            words = tuple((f, tuple(WORD_RE.findall(low))) for f, low in self.lows)
            u = set()
            n = 0
            for _f, ws in words:
                u.update(ws)
                n += _WORDS_FIELD_B + 8 * len(ws)
                for w in ws:
                    n += _STR_B + len(w)
            n += _SET_B + _SET_SLOT_B * len(u)
            union = frozenset(u)
            with _PUBLISH_LOCK:
                publisher = self._words is None
                if publisher:
                    self._union = union
                    self._words = words
            fc = self._fc() if self._fc is not None else None
            if publisher and fc is not None:
                fc._grow(n)
        return self._words

    def union(self):
        self.words()
        return self._union


# Retained-size ESTIMATE constants (CPython 3.13, 64-bit), calibrated against tracemalloc on a real
# 9,661-node corpus by tests/test_find_cache_byte_ceiling.py (within +/-35%). Incremental and O(1) per
# entry: no sizeof walks on the hot path.
_STR_B = 49                # a str object's fixed part; + len for ASCII text
_LOW_B = 56 + 8            # a (field, lowered) pair tuple + its slot in the lows tuple
_ENTRY_B = 200             # _Entry object + its dict slot + the lows tuple header
_WORDS_FIELD_B = 56 + 40   # per field: the (field, words) pair + the words tuple header
_SET_B = 216               # the per-entry word-union frozenset header
_SET_SLOT_B = 40           # per unique word in that frozenset's table
_VOCAB_B = 8               # per vocabulary word in the sorted list


def _build_entry(node: dict, node_id: str, fc=None) -> _Entry:
    lows = [("id", node_id.lower()), ("title", (node.get("title") or "").lower())]
    for k, v in (node.get("properties") or {}).items():
        for sv in _string_values(v):
            lows.append((k, sv.lower()))
    e = _Entry(tuple(lows), fc)
    if fc is not None:
        n = _ENTRY_B
        for _f, low in lows:
            n += _LOW_B + _STR_B + len(low)
        fc._grow(n)
    return e


class FieldCache:
    """Per-partition cache of node entries + a lazily built sorted vocabulary (prefix mode).
    Keyed on the partition freshness key by the owner (`FieldCaches`). Keeps a running ESTIMATE of its
    retained bytes (`est_bytes`), reported to the owner as it grows so the owner can hold a byte ceiling."""

    def __init__(self, key, owner=None):
        self.key = key
        self.entries: Dict[str, _Entry] = {}
        self._vocab: Optional[List[str]] = None
        self.est_bytes = 0
        self._owner = owner           # None once detached (evicted / oversize / ceiling 0)

    def _grow(self, n: int):
        self.est_bytes += n
        if self._owner is not None:
            self._owner._grew(self, n)

    def entry(self, node: dict, node_id: str) -> _Entry:
        e = self.entries.get(node_id)
        if e is None:
            e = self.entries[node_id] = _build_entry(node, node_id, self)
        return e

    def vocab(self, nodes: Dict[str, dict]) -> List[str]:
        if self._vocab is None:
            v = set()
            for nid, nd in nodes.items():
                v.update(self.entry(nd, nid).union())
            self._vocab = sorted(v)
            self._grow(56 + _VOCAB_B * len(self._vocab))
        return self._vocab


_WARNED_CEILINGS: set = set()


def ceiling_bytes() -> int:
    """The total byte ceiling, read from the environment on every call. ASCII digits only; a bad value
    warns once per distinct value on stderr and the default applies. 0 = cache nothing."""
    raw = os.environ.get(CACHE_MB_ENV)
    if raw is None:
        return CACHE_MB_DEFAULT * 1024 * 1024
    if raw.isascii() and raw.isdigit():
        mb = int(raw)
        if mb > CACHE_MB_MAX:
            if raw not in _WARNED_CEILINGS:
                _WARNED_CEILINGS.add(raw)
                print(f"lts-engine: {CACHE_MB_ENV}={raw[:24]!r} is above {CACHE_MB_MAX}; clamped to "
                      f"{CACHE_MB_MAX} MB", file=sys.stderr)
            mb = CACHE_MB_MAX
        return mb * 1024 * 1024
    if raw not in _WARNED_CEILINGS:
        _WARNED_CEILINGS.add(raw)
        print(f"lts-engine: ignoring {CACHE_MB_ENV}={raw!r} (ASCII digits only); using the default "
              f"{CACHE_MB_DEFAULT} MB", file=sys.stderr)
    return CACHE_MB_DEFAULT * 1024 * 1024


class FieldCaches:
    """LRU of FieldCache by corpus id, validated on the partition freshness key. Bounded by count
    (CACHE_MAX) AND by a total byte ESTIMATE ceiling (`ceiling_bytes()`); eviction is LRU by recency.
    A corpus that alone exceeds the ceiling serves its own call and is then not retained."""

    def __init__(self):
        self._c: "OrderedDict[str, FieldCache]" = OrderedDict()
        self._total = 0
        self.evictions = 0
        self.oversize_skips = 0

    def _detach(self, cid: str):
        fc = self._c.pop(cid)
        fc._owner = None
        self._total -= fc.est_bytes

    def for_part(self, part) -> FieldCache:
        key = part._freshness_key()
        cid = part.corpus_id
        cap = ceiling_bytes()                    # read per call; enforced here as well as on growth
        fc = self._c.get(cid)
        if fc is not None and fc.key != key:
            self._detach(cid)
            fc = None
        if cap <= 0:
            for c in list(self._c):              # 0 = cache nothing, including what is already warm
                self._detach(c)
                self.evictions += 1
            self.oversize_skips += 1
            return FieldCache(key)               # detached: serves this call only
        if fc is None:
            fc = FieldCache(key, self)
            self._c[cid] = fc
        self._c.move_to_end(cid)
        while len(self._c) > CACHE_MAX:
            self._detach(next(iter(self._c)))
            self.evictions += 1
        for c in [c for c in self._c if c != cid]:    # LRU first, the requested corpus is spared
            if self._total <= cap:
                break
            self._detach(c)
            self.evictions += 1
        if self._total > cap and fc.est_bytes > cap:   # it alone no longer fits
            self._detach(cid)
            self.oversize_skips += 1
        return fc

    def _grew(self, fc: FieldCache, n: int):
        self._total += n
        cap = ceiling_bytes()
        if self._total <= cap:
            return
        mine = next((cid for cid, c in self._c.items() if c is fc), None)
        if mine is not None and fc.est_bytes > cap:         # alone over the ceiling: spare the neighbours
            self._detach(mine)
            self.oversize_skips += 1
            return
        for cid in [c for c in self._c if c != mine]:      # LRU first; the growing cache is spared
            if self._total <= cap:
                return
            self._detach(cid)
            self.evictions += 1

    def stats(self) -> dict:
        """Tests/benchmarks only; never part of a tool response."""
        return {"entries": len(self._c), "estimated_bytes": self._total,
                "ceiling_bytes": ceiling_bytes(), "evictions": self.evictions,
                "oversize_skips": self.oversize_skips}


# --------------------------------------------------------------------------- compiled parts
class Term:
    __slots__ = ("raw", "match", "phrase", "words", "fields", "expansion")

    def __init__(self, raw: str, match: str, fields: Optional[frozenset]):
        self.raw = raw
        self.match = match
        self.fields = fields
        self.phrase = raw.lower() if match == "phrase" else None
        self.words = tuple(words_of(raw)) if match != "phrase" else ()
        self.expansion: Optional[frozenset] = None     # prefix mode: vocabulary words it covers

    def echo(self) -> dict:
        d = {"term": self.raw, "match": self.match}
        if self.match != "phrase":
            d["words"] = list(self.words)
        d["fields"] = sorted(self.fields) if self.fields is not None else "all structured fields"
        return d


class Cond:
    __slots__ = ("key", "op", "value", "values")

    def __init__(self, key, op, value=None, values=None):
        self.key, self.op, self.value, self.values = key, op, value, values

    def test(self, props: dict) -> bool:
        op = self.op
        if op == "has":
            return self.key in props
        if op == "lacks":
            return self.key not in props
        if self.key not in props:
            return False              # equals/one_of/not_*: a node LACKING the key never matches
        vals = [s.strip() for s in _string_values(props[self.key])]
        if op == "equals":
            return self.value in vals
        if op == "one_of":
            return any(s in self.values for s in vals)
        if op == "not_equals":
            return any(s != self.value for s in vals)
        return any(s not in self.values for s in vals)          # not_one_of

    def echo(self) -> dict:
        d: Dict[str, Any] = {"key": self.key, "op": self.op}
        if self.op in ("equals", "not_equals"):
            d["value"] = self.value
        elif self.op in ("one_of", "not_one_of"):
            d["values"] = sorted(self.values)
        if self.key == "status":
            d["note"] = "plain property equality: no lifecycle widening (unlike the `status` filter)"
        return d


# --------------------------------------------------------------------------- the query
class CompiledQuery:
    def __init__(self):
        self.all_of: Tuple[Term, ...] = ()
        self.any_of: Tuple[Term, ...] = ()
        self.none_of: Tuple[Term, ...] = ()
        self.where: Tuple[Cond, ...] = ()
        self._wkeys: Tuple[str, ...] = ()
        self.fields: Optional[frozenset] = None
        self.default_match = "phrase"
        self.match_given = False
        self.source_file: Optional[str] = None
        self.document_tag: Optional[str] = None
        self.explain = False
        self.date: Optional[tuple] = None            # (since, until, date_property), set by the tool
        self.fallback = False                         # True on the fallback's derived query
        self._cache: Optional[FieldCache] = None
        self._nodes: Optional[dict] = None
        self.reset_stats()

    # -- per-call mutable state (scope counters for the misspelled-key guard) ----------
    def reset_stats(self):
        self.scope_total = 0
        self.key_present: Dict[str, int] = {}
        self.untagged_in_scope: Optional[int] = None

    @property
    def has_terms(self) -> bool:
        return bool(self.all_of or self.any_of or self.none_of)

    def derive_with_all_of(self, extra: List[Term]) -> "CompiledQuery":
        c = copy.copy(self)
        c.all_of = tuple(self.all_of) + tuple(extra)
        c.fallback = True
        c._cache = None
        c._nodes = None
        c.reset_stats()
        return c

    # -- binding to a partition (cache + prefix expansion) ------------------------------
    def bind(self, part, caches: Optional[FieldCaches]):
        self._nodes = part.nodes
        self._cache = caches.for_part(part) if caches is not None else None
        for t in self.all_of + self.any_of + self.none_of:
            if t.match == "prefix":
                if self._cache is not None:
                    voc = self._cache.vocab(part.nodes)
                    p = t.words[0]
                    lo = bisect.bisect_left(voc, p)
                    hi = lo
                    while hi < len(voc) and voc[hi].startswith(p):
                        hi += 1
                    t.expansion = frozenset(voc[lo:hi])
                else:
                    t.expansion = None

    # -- the term test -----------------------------------------------------------------
    def _entry(self, node, node_id) -> _Entry:
        if self._cache is not None:
            return self._cache.entry(node, node_id)
        return _build_entry(node, node_id)

    @staticmethod
    def _hit(t: Term, ent: _Entry) -> bool:
        fs = t.fields
        if t.match == "phrase":
            p = t.phrase
            for f, low in ent.lows:
                if (fs is None or f in fs) and p in low:
                    return True
            return False
        if t.match == "prefix":
            exp = t.expansion
            if fs is None:
                if exp is not None:
                    return not exp.isdisjoint(ent.union())
                p = t.words[0]
                return any(w.startswith(p) for w in ent.union())
            p = t.words[0]
            for f, ws in ent.words():
                if f in fs and any((w in exp) if exp is not None else w.startswith(p) for w in ws):
                    return True
            return False
        ws_ = t.words
        if len(ws_) == 1:
            w0 = ws_[0]
            if fs is None:
                return w0 in ent.union()
            return any(f in fs and w0 in wt for f, wt in ent.words())
        u = ent.union()
        for w in ws_:
            if w not in u:
                return False
        n = len(ws_)
        for f, wt in ent.words():
            if fs is not None and f not in fs:
                continue
            if len(wt) < n:
                continue
            for i in range(len(wt) - n + 1):
                if wt[i:i + n] == ws_:
                    return True
        return False

    def matches(self, node: dict, node_id: str) -> bool:
        """Every new condition, ANDed. Called from `tools._node_matches` (C4)."""
        if self.document_tag is not None and node.get("document_tag") != self.document_tag:
            return False
        if self.source_file is not None and node.get("source_file") != self.source_file:
            return False
        if self.has_terms:
            ent = self._entry(node, node_id)
            for t in self.all_of:
                if not self._hit(t, ent):
                    return False
            if self.any_of and not any(self._hit(t, ent) for t in self.any_of):
                return False
            for t in self.none_of:
                if self._hit(t, ent):
                    return False
        if self.where:
            if self.date is not None:          # the scope counters must see ALL other filters
                from .tools import _date_in_range
                if not _date_in_range(node, *self.date):
                    return False
            props = node.get("properties") or {}
            self.scope_total += 1
            for k in self._wkeys:              # a key counted once per node, however many conditions name it
                if k in props:
                    self.key_present[k] = self.key_present.get(k, 0) + 1
            for c in self.where:
                if not c.test(props):
                    return False
        return True

    # -- extent ------------------------------------------------------------------------
    def keys_consulted(self) -> List[str]:
        keys: List[str] = []
        if self.has_terms:
            seen = set()
            for t in self.all_of + self.any_of + self.none_of:
                if t.fields is None:
                    names = ["id", "title", "properties.*"]
                else:
                    names = [f if f in ("id", "title") else f"properties.{f}" for f in sorted(t.fields)]
                for n in names:
                    if n not in seen:
                        seen.add(n)
                        keys.append(n)
        for c in self.where:
            n = f"properties.{c.key}"
            if n not in keys:
                keys.append(n)
        if self.source_file is not None:
            keys.append("source_file")
        if self.document_tag is not None:
            keys.append("document_tag")
        return keys

    def scope_filters(self) -> dict:
        d: Dict[str, Any] = {}
        for name in ("all_of", "any_of", "none_of"):
            ts = getattr(self, name)
            if ts and not self.fallback:
                d[name] = [t.echo() for t in ts]
        if self.where:
            d["where"] = [c.echo() for c in self.where]
        if self.source_file is not None:
            d["source_file"] = self.source_file
        if self.document_tag is not None:
            d["document_tag"] = self.document_tag
        return d

    # -- validation against the loaded partition(s) --------------------------------------
    def validate_source_file(self, parts, area) -> Optional[str]:
        """An error string, or None. Unknown file / ambiguous multi-root path are refused."""
        sf = self.source_file
        if sf is None:
            return None
        if len(parts) != 1:
            return "source_file needs a named `corpus`"
        part = parts[0]
        nodes = [nd for nd in part.nodes.values() if nd.get("source_file") == sf]
        if not nodes:
            return (f"source_file ({_d(sf)}) matches no node in corpus {part.corpus_id!r}: it must equal "
                    "a node's `source_file` exactly (relative to the corpus root, as lts_node reports it)")
        # decided by ROOT identity, not by area label: two roots can share a label
        roots = [r for r in part.root_paths if os.path.isfile(os.path.join(r, sf))]
        if len(roots) > 1 and area:
            specs = getattr(getattr(part, "corpus_index", None), "root_specs", None)
            if specs:
                area_of = {os.path.abspath(sp["path"]): sp.get("area") for sp in specs}
                roots = [r for r in roots if area_of.get(os.path.abspath(r)) == area]
            elif not any(nd.get("area") == area for nd in nodes):
                roots = []       # no root-spec info: fall back to the node labels (cannot tell roots apart)
        if len(roots) > 1:
            return (f"source_file ({_d(sf)}) exists under {len(roots)} roots of corpus "
                    f"{part.corpus_id!r}" + (" even after `area`" if area else "")
                    + ": it is ambiguous; narrow it with `area` so exactly one root remains")
        return None

    # -- response fields -----------------------------------------------------------------
    def finalize(self, parts) -> None:
        if self.document_tag is not None:
            self.untagged_in_scope = sum(1 for p in parts for nd in p.nodes.values()
                                         if not nd.get("document_tag"))

    def warnings(self) -> List[str]:
        out: List[str] = []
        if self.match_given and not self.has_terms:
            out.append("`match` has no effect: no all_of / any_of / none_of terms were given")
        if self.fields is not None and not self.has_terms:
            out.append("`fields` has no effect: no all_of / any_of / none_of terms were given "
                       "(it does not apply to `text` or `where`)")
        if self.where:
            seen = set()
            for c in self.where:
                if c.key in seen:
                    continue
                seen.add(c.key)
                if self.key_present.get(c.key, 0) == 0:
                    if self.scope_total == 0:
                        out.append(f"no node passes the other filters, so nothing in scope carries "
                                   f"'{c.key}'; check the filters and the spelling")
                    elif c.op == "lacks":
                        out.append(f"no node in scope carries '{c.key}'; `lacks` therefore matches every "
                                   "node in scope, so check the spelling")
                    else:
                        out.append(f"no node in scope carries '{c.key}'; `{c.op}` therefore matches no "
                                   "node in scope, so check the spelling")
        return out

    def echo(self, *, text, filters: dict) -> dict:
        q: Dict[str, Any] = {
            "text": ({"value": text, "mode": TEXT_MODE} if text else None),
        }
        for name in ("all_of", "any_of", "none_of"):
            ts = getattr(self, name)
            if ts:
                q[name] = [t.echo() for t in ts]
        if self.where:
            rows = []
            for c in self.where:
                r = c.echo()
                r["key_present_in_scope"] = self.key_present.get(c.key, 0)
                rows.append(r)
            q["where"] = rows
        f = {k: v for k, v in filters.items() if v is not None}
        if self.source_file is not None:
            f["source_file"] = self.source_file
        if self.document_tag is not None:
            f["document_tag"] = self.document_tag
            if self.untagged_in_scope is not None:
                f["document_tag_untagged_in_scope"] = self.untagged_in_scope
        q["filters"] = f
        if self.has_terms:
            q["match_default"] = self.default_match
        q["combine"] = COMBINE
        return q


# --------------------------------------------------------------------------- compile
def _err(msg: str):
    raise QueryError(msg)


def _d(v) -> str:
    """Describe a caller value WITHOUT quoting it: its TYPE and LENGTH only. Refusal text is
    returned to the caller AND logged verbatim by querylog, so it must never carry a caller VALUE
    (term, condition value, file name, mode, operator or key text). Only parameter names, indices
    and the fixed operator/mode vocabulary may appear."""
    if isinstance(v, str):
        return f"a str, {len(v)} chars"
    if isinstance(v, (list, tuple, dict, set, frozenset)):
        return f"a {type(v).__name__}, {len(v)} items"
    return f"a {type(v).__name__}"


def _compile_terms(name: str, raw, default: str, fields) -> Tuple[Term, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        _err(f"{name} must be a list of terms (each a string or {{\"term\": str, \"match\": ...}})")
    if not raw:
        _err(f"{name} is an empty list: give at least one term, or omit {name}")
    out = []
    for i, el in enumerate(raw):
        where = f"{name}[{i}]"
        if isinstance(el, str):
            term, mode = el, default
        elif isinstance(el, dict):
            extra = set(el) - {"term", "match"}
            if extra:
                _err(f"{where}: unknown key(s) in a term object ({len(extra)}); an object term has only `term` and `match`")
            term = el.get("term")
            mode = el.get("match", default)
            if not isinstance(term, str):
                _err(f"{where}: `term` must be a string")
        else:
            _err(f"{where}: a term is a string or an object {{\"term\", \"match\"}}, got {_d(el)}")
        if mode not in MATCH_MODES:
            _err(f"{where}: match ({_d(mode)}) is not one of {' | '.join(MATCH_MODES)}")
        if not term.strip():
            _err(f"{where}: an empty term is refused (it would match every node)")
        t = Term(term, mode, fields)
        if mode != "phrase" and not t.words:
            _err(f"{where}: the term ({_d(term)}) has no word characters ([a-z0-9]) for match={mode}; "
                 "use match='phrase' for punctuation")
        if mode == "prefix":
            if len(t.words) != 1:
                _err(f"{where}: prefix terms must be one word (got a {len(t.words)}-word str, {len(term)} chars)")
            if len(t.words[0]) < MIN_PREFIX_LEN:
                _err(f"{where}: a prefix shorter than {MIN_PREFIX_LEN} characters is refused "
                     "(it would match nearly everything)")
        out.append(t)
    return tuple(out)


def _str_value(where: str, v) -> str:
    if not isinstance(v, str):
        _err(f"{where}: values must be strings, got {_d(v)}; property values "
             "are strings, so a number or boolean is refused rather than coerced")
    s = v.strip()
    if not s:
        _err(f"{where}: an empty value is refused")
    return s


def _compile_where(raw) -> Tuple[Cond, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        _err("where must be a list of conditions")
    if not raw:
        _err("where is an empty list: give at least one condition, or omit where")
    out = []
    for i, el in enumerate(raw):
        w = f"where[{i}]"
        if not isinstance(el, dict):
            _err(f"{w}: a condition is an object {{\"key\": K, <one operator>: ...}}")
        key = el.get("key")
        if not isinstance(key, str) or not KEY_RE.match(key):
            _err(f"{w}: `key` ({_d(key)}) is not a valid property key (^[a-z][a-z0-9_-]*$)")
        ops = [k for k in el if k != "key"]
        bad = [k for k in ops if k not in WHERE_OPS]
        if bad:
            _err(f"{w}: unknown operator(s) ({len(bad)}); the operators are {', '.join(WHERE_OPS)}")
        if len(ops) != 1:
            _err(f"{w}: a condition needs exactly ONE operator ({', '.join(WHERE_OPS)}), got {len(ops)}")
        op = ops[0]
        v = el[op]
        if op in ("has", "lacks"):
            if v is not True:
                _err(f"{w}: `{op}` takes true (use the other of has/lacks to negate), got {_d(v)}")
            out.append(Cond(key, op))
        elif op in ("equals", "not_equals"):
            out.append(Cond(key, op, value=_str_value(f"{w}.{op}", v)))
        else:
            if not isinstance(v, list):
                _err(f"{w}: `{op}` takes a list of strings")
            if not v:
                _err(f"{w}: `{op}` is an empty list")
            out.append(Cond(key, op, values=frozenset(_str_value(f"{w}.{op}[{j}]", x)
                                                      for j, x in enumerate(v))))
    return tuple(out)


def compile_query(*, all_of=None, any_of=None, none_of=None, match=None, where=None,
                  fields=None, source_file=None, document_tag=None, explain=None,
                  corpus=None, other_positive_filter: bool = False) -> Optional[CompiledQuery]:
    """Validate and compile. None = no new parameter in use (the old path, untouched).
    Raises QueryError with a message naming the parameter."""
    used = (all_of is not None or any_of is not None or none_of is not None or match is not None
            or where is not None or fields is not None or source_file is not None
            or document_tag is not None or bool(explain))
    if not used:
        return None
    cq = CompiledQuery()
    if match is not None:
        if match not in MATCH_MODES:
            _err(f"match ({_d(match)}) is not one of {' | '.join(MATCH_MODES)}")
        cq.default_match = match
        cq.match_given = True
    fset = None
    if fields is not None:
        if not isinstance(fields, list) or not fields:
            _err("fields must be a non-empty list of 'id' | 'title' | property keys")
        for f in fields:
            if not isinstance(f, str) or not f.strip():
                _err(f"fields: an entry ({_d(f)}) is not a non-empty string")
        fset = frozenset(fields)
        cq.fields = fset
    cq.all_of = _compile_terms("all_of", all_of, cq.default_match, fset)
    cq.any_of = _compile_terms("any_of", any_of, cq.default_match, fset)
    cq.none_of = _compile_terms("none_of", none_of, cq.default_match, fset)
    cq.where = _compile_where(where)
    cq._wkeys = tuple(dict.fromkeys(c.key for c in cq.where))
    if source_file is not None:
        if not isinstance(source_file, str) or not source_file:
            _err("source_file must be a non-empty string")
        if not corpus:
            _err("source_file needs a named `corpus` (a file path is only meaningful inside one corpus)")
        cq.source_file = source_file
    if document_tag is not None:
        if not isinstance(document_tag, str) or not document_tag:
            _err("document_tag must be a non-empty string")
        cq.document_tag = document_tag
    cq.explain = bool(explain)
    if cq.none_of and not (cq.all_of or cq.any_of or cq.where or other_positive_filter
                           or cq.source_file is not None or cq.document_tag is not None):
        _err("none_of alone would mean 'every node except...': add a positive filter "
             "(all_of, any_of, where, prefix, status, area, text, ...)")
    return cq


def empty_query() -> CompiledQuery:
    return CompiledQuery()


def fallback_terms(words: List[str]) -> List[Term]:
    return [Term(w, "word", None) for w in words]
