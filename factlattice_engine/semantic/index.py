"""[SEM-INDEX] The per-corpus embedding sidecar: incremental, drift-refusing, crash-safe.

Layout: `<state_root>/semantic/<model_id>/v-<index_version>/<corpus_id>/` (every level 0700, never inside a corpus root;
an embedding index of confidential text must not be synced or committed with it):
  manifest.json  index_version, model_id, card_schema, corpus, counts, vectors_sha256, built_at
  vectors.npy    float16 [n_vectors, dim], L2-normalised rows
  rows.json      {"ids": [...], "kinds": ["head"|"chunk", ...]}  (row-aligned with vectors)
  fps.json       {node_id: card_fingerprint}  (the incremental-refresh key)
Write order: every data file via temp+rename, the manifest LAST, pinning the vectors' sha256.
A torn write therefore reads as `index_corrupt`, never as a smaller index.

`index_version` = sha256 of `_version_material` (model, cards, chunking, tokenizer, embedder,
runtime, storage dtype: the full list is in SDLC.md §7.6). A sidecar whose
version differs from the running config is REFUSED for search (`index_incompatible`) and
fully rebuilt by `refresh`: vectors from two spaces are never mixed (honesty rule 7).
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import shutil
import threading
import time
import unicodedata
from typing import Dict, List, Optional, Tuple

from factlattice.hashing import file_content_hash, safe_read_text

from . import cards as C

CHUNK_PENALTY = 0.03
STORAGE_DTYPE = "float16"
# [SIDECAR-PER-VERSION] another version's sidecar untouched this long is garbage-collected after a
# successful write (lock permitting). A version is ~21 MB for today's six live corpora.
GC_AGE_S = 14 * 86400   # vectors.npy dtype; part of index_version


class IndexIncompatible(Exception):
    """`status`: index_missing | index_incompatible | index_corrupt."""

    def __init__(self, status: str, detail: str = ""):
        super().__init__(f"{status}: {detail}" if detail else status)
        self.status, self.detail = status, detail


def _version_material(model_id: str, embedder=None) -> str:
    """Everything that shapes the stored vectors: the model, the card schema and chunking, the
    chunk penalty, the tokenizer implementation version ([S1-S4 P3-6], [composition F-G2]) and,
    from the embedder, its vocab sha, pooling, query/doc prefix, max_len and dimension."""
    from .embedder import EMBEDDER_VERSION
    from .tokenizer import TOKENIZER_VERSION
    raw = (f"{model_id}|cards-v{C.CARD_SCHEMA_VERSION}|{C.CHUNK_WORDS}/{C.CHUNK_OVERLAP}/{C.MAX_CHUNKS}"
           f"|pen{CHUNK_PENALTY}|tok:{TOKENIZER_VERSION}|ucd:{unicodedata.unidata_version}"
           f"|emb:{EMBEDDER_VERSION}")
    if embedder is not None:
        pin = embedder.pin
        raw += (f"|vocab:{pin.vocab_sha256}|pool:{pin.pooling}|qp:{pin.query_prefix!r}"
                f"|dp:{pin.doc_prefix!r}|len:{getattr(embedder, 'max_len', None)}|dim:{pin.dim}"
                f"|ort:{_ort_version()}|np:{_np_version()}|store:{STORAGE_DTYPE}")
    return raw


def _ort_version() -> str:
    """The inference runtime shapes the floats too: a runtime upgrade re-embeds, never mixes."""
    from .embedder import _import_runtime
    return _import_runtime().__version__


def _np_version() -> str:
    import numpy
    return numpy.__version__


def index_version(model_id: str, embedder=None) -> str:
    """A change to anything in `_version_material` makes an existing sidecar incompatible."""
    raw = _version_material(model_id, embedder)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def partition_cards(part) -> Dict[str, List[Tuple[str, str]]]:
    """{node_id: [(kind, text), ...]} for every node, reading each source file once."""
    files: Dict[str, List[str]] = {}
    out: Dict[str, List[Tuple[str, str]]] = {}
    for nid in sorted(part.nodes):
        nd = part.nodes[nid]
        sf, lr = nd.get("source_file"), nd.get("line_range") or []
        lines: List[str] = []
        if sf and len(lr) == 2:
            if sf not in files:
                src = part._resolve_source(sf)
                try:
                    files[sf] = safe_read_text(src, sf).splitlines()
                except (OSError, TypeError):
                    files[sf] = []
            lines = files[sf][lr[0]:lr[1]]          # the heading line itself is skipped
        out[nid] = C.node_cards(nid, nd, lines)
    return out


def _sha256_file(path: str) -> str:
    return file_content_hash(path)[len("sha256:"):]      # never blocks on a FIFO swapped into the index directory


class SemanticIndex:
    def __init__(self, state_root: str, corpus_id: str, embedder):
        self.corpus_id, self.embedder = corpus_id, embedder
        self.model_id = embedder.model_id
        # [SIDECAR-PER-VERSION] one dir per index_version: engines running two versions side by
        # side (until every session reconnects after a version bump) each read and write only
        # their own sidecar, so neither rebuilds the other's in place (the go-live ping-pong).
        self.version = index_version(self.model_id, embedder)
        self.model_dir = os.path.join(state_root, "semantic", self.model_id)
        self.dir = os.path.join(self.model_dir, f"v-{self.version}", corpus_id)
        self.legacy_dir = os.path.join(self.model_dir, corpus_id)     # the pre-version layout
        self._loaded = None    # (manifest, V float32, ids, kinds, fps)
        self._loaded_stamp = None   # manifest (mtime_ns, size) the cache was loaded from

    @contextlib.contextmanager
    def _locked(self):
        """[review F5] One writer per sidecar, across threads AND processes: an exclusive
        flock on <dir>/.lock held for the whole load-diff-write of a refresh."""
        self._make_private_dirs()
        fd = os.open(os.path.join(self.dir, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _make_private_dirs(self):
        """semantic/, semantic/<model>/ and the corpus leaf are ALL 0700: corpus names, including
        private ones, are not listable by other users (S1-S4 P3-2)."""
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        for d in self._private_levels():
            os.chmod(d, 0o700)

    def _private_levels(self):
        """semantic/, semantic/<model>/, semantic/<model>/v-<version>/ and the corpus leaf."""
        return (os.path.dirname(self.model_dir), self.model_dir, os.path.dirname(self.dir), self.dir)

    def _tighten_dirs_best_effort(self):
        """Read path: tighten each dir on its own, so one we can't chmod doesn't skip the rest."""
        for d in self._private_levels():
            try:
                os.chmod(d, 0o700)
            except OSError:                  # not ours to chmod: leave it, the read still proceeds
                pass

    def _manifest_stamp(self):
        try:
            st = os.stat(os.path.join(self.dir, "manifest.json"))
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    # --- persistence -------------------------------------------------------------------
    def _load(self, reuse: bool = True):
        """Load (and fully verify) the sidecar; reuse the in-memory copy while the manifest on
        disk is unchanged, so a query does not re-read and re-hash the vectors every time."""
        stamp = self._manifest_stamp()
        if reuse and self._loaded is not None and stamp is not None and stamp == self._loaded_stamp:
            return self._loaded
        if os.path.isdir(self.dir):          # [composition-delta F-2] tighten an older 0755 path on read
            self._tighten_dirs_best_effort()
        import numpy as np
        man_p = os.path.join(self.dir, "manifest.json")
        if not os.path.isfile(man_p):
            others = self._other_version_dirs()
            if others:   # a sidecar exists, just not for this version: say so, as before
                raise IndexIncompatible("index_incompatible",
                                        f"no sidecar for running {self.version}; found {len(others)} "
                                        f"other version(s), e.g. {others[0]}")
            raise IndexIncompatible("index_missing", self.dir)
        try:
            man = json.loads(safe_read_text(man_p))
        except (OSError, ValueError) as ex:
            raise IndexIncompatible("index_corrupt", f"manifest: {ex}") from None
        running = self.version
        if man.get("index_version") != running:
            raise IndexIncompatible("index_incompatible",
                                    f"sidecar {man.get('index_version')} != running {running}")
        vec_p = os.path.join(self.dir, "vectors.npy")
        if not os.path.isfile(vec_p) or _sha256_file(vec_p) != man.get("vectors_sha256"):
            raise IndexIncompatible("index_corrupt", "vectors sha256 does not match the manifest")
        try:
            V = np.load(vec_p).astype(np.float32)
            rows = json.loads(safe_read_text(os.path.join(self.dir, "rows.json")))
            fps = json.loads(safe_read_text(os.path.join(self.dir, "fps.json")))
        except (OSError, ValueError) as ex:
            raise IndexIncompatible("index_corrupt", str(ex)) from None
        if V.shape[0] != len(rows["ids"]) or V.shape[0] != man.get("n_vectors"):
            raise IndexIncompatible("index_corrupt", "row count mismatch")
        self._loaded = (man, V, rows["ids"], rows["kinds"], fps)
        self._loaded_stamp = stamp
        return self._loaded

    def _write(self, V, ids, kinds, fps, n_nodes):
        import numpy as np
        self._make_private_dirs()

        def put(name, writer):
            tmp = os.path.join(self.dir, f"{name}.{os.getpid()}.{threading.get_ident()}.tmp")
            writer(tmp)
            os.replace(tmp, os.path.join(self.dir, name))
        put("vectors.npy", lambda p: np.save(open(p, "wb"), V.astype(STORAGE_DTYPE)))
        put("rows.json", lambda p: json.dump({"ids": ids, "kinds": kinds}, open(p, "w")))
        put("fps.json", lambda p: json.dump(fps, open(p, "w")))
        man = {"index_version": self.version, "model_id": self.model_id,
               "card_schema": C.CARD_SCHEMA_VERSION, "corpus": self.corpus_id,
               "n_nodes": n_nodes, "n_vectors": int(V.shape[0]),
               "vectors_sha256": _sha256_file(os.path.join(self.dir, "vectors.npy")),
               "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
        put("manifest.json", lambda p: json.dump(man, open(p, "w"), indent=1))   # LAST
        self._loaded = (man, V.astype(STORAGE_DTYPE).astype(np.float32), ids, kinds, fps)
        self._loaded_stamp = self._manifest_stamp()

    # --- refresh -----------------------------------------------------------------------
    def refresh(self, part, interactive: bool = False) -> dict:
        """Bring the sidecar in line with `part`. Embeds only nodes whose card fingerprint
        moved; drops deleted nodes; rebuilds fully (and says why) when the sidecar is missing,
        incompatible or corrupt. Serialised per sidecar ([review F5])."""
        with self._locked():
            if not os.path.isfile(os.path.join(self.dir, "manifest.json")):
                try:
                    self._adopt_legacy()
                except OSError:   # adoption is an optimisation: a failed copy means a normal rebuild
                    pass          # (the manifest goes last, so nothing partial is ever served)
            out = self._refresh_locked(part, interactive)
        self._gc_other_versions()
        return out

    # --- [SIDECAR-PER-VERSION] legacy adoption + garbage collection ----------------------
    def _other_version_dirs(self) -> List[str]:
        """This corpus's sidecars for OTHER versions: v-<16 hex>/<corpus> siblings and the legacy dir."""
        out = []
        try:
            names = sorted(os.listdir(self.model_dir))
        except OSError:
            return out
        mine = os.path.basename(os.path.dirname(self.dir))
        for n in names:
            if n != mine and len(n) == 18 and n.startswith("v-") and \
                    all(c in "0123456789abcdef" for c in n[2:]):
                d = os.path.join(self.model_dir, n, self.corpus_id)
                if os.path.isdir(d):
                    out.append(d)
        if os.path.isfile(os.path.join(self.legacy_dir, "manifest.json")):
            out.append(self.legacy_dir)
        return out

    def _adopt_legacy(self) -> bool:
        """Copy a legacy-layout sidecar of the SAME index_version into this version's dir (no
        re-embed). Copy, not move: an old-code engine may still be using the legacy dir. Held
        under the legacy dir's own lock (old engines write it under that lock); our own lock is
        already held by refresh(). The manifest goes LAST, so a torn copy reads as missing."""
        man_p = os.path.join(self.legacy_dir, "manifest.json")
        if not os.path.isfile(man_p):
            return False
        fd = os.open(os.path.join(self.legacy_dir, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                if json.loads(safe_read_text(man_p)).get("index_version") != self.version:
                    return False
            except (OSError, ValueError):
                return False
            for name in ("vectors.npy", "rows.json", "fps.json", "manifest.json"):
                src = os.path.join(self.legacy_dir, name)
                if not os.path.isfile(src):
                    return False
                tmp = os.path.join(self.dir, f"{name}.{os.getpid()}.{threading.get_ident()}.tmp")
                try:
                    shutil.copyfile(src, tmp)
                    os.replace(tmp, os.path.join(self.dir, name))
                except OSError:
                    try:
                        os.remove(tmp)            # no partial temp file left behind
                    except OSError:
                        pass
                    raise
            return True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _gc_other_versions(self, now: Optional[float] = None) -> List[str]:
        """Remove other versions' sidecars for this corpus that nothing has written for GC_AGE_S,
        only when their lock is free (non-blocking). Never the running version. Best effort."""
        now = time.time() if now is None else now
        removed = []
        root = os.path.realpath(self.model_dir)

        def newest(d):
            return max((os.stat(os.path.join(d, f)).st_mtime for f in os.listdir(d)),
                       default=os.stat(d).st_mtime)
        for d in self._other_version_dirs():
            try:
                # never follow a link out of semantic/<model>/: every level must be a real dir
                # inside it ([review P3: a symlinked v-<hex> made rmtree delete outside the tree])
                if os.path.islink(d) or os.path.islink(os.path.dirname(d)) or \
                        os.path.commonpath([root, os.path.realpath(d)]) != root:
                    continue
                if now - newest(d) < GC_AGE_S:
                    continue
                fd = os.open(os.path.join(d, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(fd)
                    continue                              # in use: leave it
                try:
                    if time.time() - newest(d) < GC_AGE_S:
                        continue                          # written between the check and the lock
                    shutil.rmtree(d)
                    removed.append(d)
                finally:
                    os.close(fd)
                parent = os.path.dirname(d)
                if parent != self.model_dir:
                    try:
                        os.rmdir(parent)                  # an emptied v-<version>/ dir
                    except OSError:
                        pass
            except OSError:
                continue
        return removed

    def _refresh_locked(self, part, interactive: bool = False) -> dict:
        import numpy as np
        cards = partition_cards(part)
        fps_now = {nid: C.card_fingerprint(c) for nid, c in cards.items()}
        rebuilt_reason: Optional[str] = None
        try:
            _man, V_old, ids_old, kinds_old, fps_old = self._load(reuse=False)   # re-read under the lock
        except IndexIncompatible as ex:
            rebuilt_reason, V_old, ids_old, kinds_old, fps_old = ex.status, None, [], [], {}
        changed = sorted(n for n, fp in fps_now.items() if fps_old.get(n) != fp)
        dropped = sorted(n for n in fps_old if n not in fps_now)
        keep = [i for i, n in enumerate(ids_old) if n in fps_now and n not in changed]
        new_ids: List[str] = []
        new_kinds: List[str] = []
        texts: List[str] = []
        for nid in changed:
            for kind, text in cards[nid]:
                new_ids.append(nid); new_kinds.append(kind); texts.append(text)
        if not changed and not dropped and V_old is not None:
            return {"embedded_nodes": 0, "dropped_nodes": 0, "unchanged_nodes": len(fps_now),
                    "vectors": len(ids_old), "rebuilt_reason": None}
        dim = getattr(self.embedder.pin, "dim", None)
        # [MECH-SEMANTIC-EMBEDD] a refresh someone is waiting on asks a shared service for its
        # query lane; local embedders have no lanes and are called exactly as before
        kw = {"interactive": True} if interactive and getattr(self.embedder, "supports_interactive", False) else {}
        V_new = self.embedder.embed(texts, query=False, **kw) if texts else np.zeros((0, dim or 1), np.float32)
        parts = [V_old[keep]] if V_old is not None and keep else []
        V = np.concatenate(parts + [V_new], axis=0) if parts else V_new
        ids = [ids_old[i] for i in keep] + new_ids
        kinds = [kinds_old[i] for i in keep] + new_kinds
        self._write(V, ids, kinds, fps_now, len(fps_now))
        return {"embedded_nodes": len(changed), "dropped_nodes": len(dropped),
                "unchanged_nodes": len(fps_now) - len(changed), "vectors": len(ids),
                "rebuilt_reason": rebuilt_reason}

    # --- search ------------------------------------------------------------------------
    def search(self, qv, k: int = 10) -> List[Tuple[str, float, str]]:
        """[(node_id, score, "head"|"chunk")], one row per node, best first. Raises
        IndexIncompatible rather than answering from a missing/incompatible/corrupt sidecar."""
        import numpy as np
        _man, V, ids, kinds, _fps = self._loaded or self._load()
        if V.shape[0] == 0:
            return []
        s = V @ np.asarray(qv, dtype=np.float32)
        is_chunk = np.fromiter((x == "chunk" for x in kinds), dtype=bool, count=len(kinds))
        s = np.where(is_chunk, s - CHUNK_PENALTY, s)
        best: Dict[str, Tuple[float, str]] = {}
        for i in np.argsort(-s, kind="stable"):
            nid = ids[i]
            if nid not in best:
                best[nid] = (float(s[i]), kinds[i])
                if len(best) >= k:
                    break
        return [(n, sc, via) for n, (sc, via) in best.items()]
