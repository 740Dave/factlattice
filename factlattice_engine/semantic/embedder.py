"""[SEM-EMBEDDER] The offline, hash-pinned sentence embedder behind lts-engine's semantic assist.

Contract (ISSUE-SEMANTIC-DESIGN honesty rules 4, 7, 8):
  * Weights load ONLY from `<model_root>/<repo with "/" -> "__">/<revision>/`, and only if
    `onnx/model.onnx` AND `vocab.txt` match their sha256 pins. A mismatch is REFUSED
    (`pin_mismatch`): a drifted model is never mixed with an index built by another.
  * Every way this can fail raises `SemanticUnavailable(status, detail)`. The CALLER turns that
    into a reported `status: unavailable` on the semantic field; it never reaches the exact
    path (rule 4).
  * No network. Nothing here imports an HTTP client or a model hub; onnxruntime runs a local
    file on CPU. A test denies sockets during load + embed.
  * Deterministic: CPU execution, fixed batch padding, L2-normalised float32 output.

numpy and onnxruntime are the OPTIONAL `semantic` extra. This module imports them lazily, so
importing the engine never pulls them in ([DEC-NO-LLM], as amended: zero model dependencies
for the core).
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from .tokenizer import WordPieceTokenizer

DEFAULT_MODEL_ROOT = os.path.expanduser("~/.lts-engine/models")
# Bump on ANY change to Embedder.embed (pooling maths, normalisation, padding/batching that can
# change a vector): it is part of every index_version (composition-delta review F-1).
EMBEDDER_VERSION = 1
BGE_QUERY = "Represent this sentence for searching relevant passages: "


class SemanticUnavailable(Exception):
    """`status` is one of: runtime_missing | weights_missing | pin_mismatch | load_failed."""

    def __init__(self, status: str, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


@dataclass(frozen=True)
class ModelPin:
    name: str
    repo: str
    revision: str
    onnx_sha256: str
    vocab_sha256: str
    pooling: str            # "cls" | "mean"
    query_prefix: str
    doc_prefix: str
    dim: int


# The S0 bake-off candidates, pinned at staging (2026-10-02, ~/.lts-engine/models/manifest.tsv).
_VOCAB = "07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3"
PINS: Dict[str, ModelPin] = {p.name: p for p in (
    ModelPin("bge-small", "BAAI/bge-small-en-v1.5", "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a",
             "828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35", _VOCAB,
             "cls", BGE_QUERY, "", 384),
    ModelPin("minilm-l6", "sentence-transformers/all-MiniLM-L6-v2", "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
             "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452", _VOCAB,
             "mean", "", "", 384),
    ModelPin("arctic-s", "Snowflake/snowflake-arctic-embed-s", "e596f507467533e48a2e17c007f0e1dacc837b33",
             "579c1f1778a0993eb0d2a1403340ffb491c769247fb46acc4f5cf8ac5b89c1e1", _VOCAB,
             "cls", BGE_QUERY, "", 384),
    ModelPin("bge-base", "BAAI/bge-base-en-v1.5", "a5beb1e3e68b9ab74eb54cfd186867f64f240e1a",
             "9bc579acdba21c253c62a9bf866891355a63ffa3442b52c8a37d75b2ccb91848", _VOCAB,
             "cls", BGE_QUERY, "", 768),
    ModelPin("arctic-m15", "Snowflake/snowflake-arctic-embed-m-v1.5", "e58a8f756156a1293d763f17e3aae643474e9b8a",
             "bc6f7662487a0796e6f86229c8e5c3ab2f7059e08e0bb2f524125273f72171c4", _VOCAB,
             "cls", BGE_QUERY, "", 768),
    ModelPin("nomic-v15", "nomic-ai/nomic-embed-text-v1.5", "e9b6763023c676ca8431644204f50c2b100d9aab",
             "147d5aa88c2101237358e17796cf3a227cead1ec304ec34b465bb08e9d952965", _VOCAB,
             "mean", "search_query: ", "search_document: ", 768),
)}


def _import_runtime():
    import numpy  # noqa: F401
    import onnxruntime
    return onnxruntime


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Embedder:
    def __init__(self, pin: ModelPin, session, tokenizer: WordPieceTokenizer, max_len: int):
        import numpy as np
        self._np = np
        self.pin, self.session, self.tokenizer, self.max_len = pin, session, tokenizer, max_len
        self.model_id = f"{pin.name}@{pin.onnx_sha256[:12]}"
        self._inputs = {i.name for i in session.get_inputs()}
        outs = [o.name for o in session.get_outputs()]
        self._out = "token_embeddings" if "token_embeddings" in outs else outs[0]

    def plan(self, texts: List[str], query: bool, batch: int = 32):
        """The exact batches embed() runs, as [(row indices, input_ids, attention_mask)]:
        texts sorted by token length (ties by position), cut into `batch`-sized groups, each
        padded to its own longest member. [MECH-SEMANTIC-EMBEDD] The shared service runs this
        same plan, so its vectors are byte-identical to in-process ones."""
        np = self._np
        prefix = self.pin.query_prefix if query else self.pin.doc_prefix
        enc = [self.tokenizer.encode(prefix + t, self.max_len)[0] for t in texts]
        order = sorted(range(len(enc)), key=lambda i: (len(enc[i]), i))   # deterministic
        out = []
        for b in range(0, len(order), batch):
            idx = order[b:b + batch]
            width = max(len(enc[i]) for i in idx)
            ids = np.zeros((len(idx), width), dtype=np.int64)
            mask = np.zeros((len(idx), width), dtype=np.int64)
            for r, i in enumerate(idx):
                ids[r, :len(enc[i])] = enc[i]
                mask[r, :len(enc[i])] = 1
            out.append((idx, ids, mask))
        return out

    def run_batch(self, ids, mask):
        """One planned batch -> L2-normalised float32 vectors, one row per text."""
        np = self._np
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self.session.run([self._out], feed)[0]
        if self.pin.pooling == "cls":
            vec = hidden[:, 0, :]
        else:
            m = mask[..., None].astype(np.float32)
            vec = (hidden * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1e-9)
        vec = vec / np.maximum(np.linalg.norm(vec, axis=1, keepdims=True), 1e-12)
        return vec.astype(np.float32)

    def embed(self, texts: List[str], query: bool, batch: int = 32):
        out = self._np.zeros((len(texts), self.pin.dim), dtype=self._np.float32)
        for idx, ids, mask in self.plan(texts, query, batch):
            out[idx] = self.run_batch(ids, mask)
        return out


def load_embedder(pin: ModelPin, model_root: Optional[str] = None, max_len: int = 512) -> Embedder:
    """Verify both pins, then load on CPU. Raises SemanticUnavailable, never anything else."""
    try:
        ort = _import_runtime()
    except ImportError as ex:
        raise SemanticUnavailable("runtime_missing",
                                  f"{ex}; install the optional `semantic` extra") from None
    root = model_root or os.environ.get("LTS_SEMANTIC_MODEL_DIR") or DEFAULT_MODEL_ROOT
    base = os.path.join(root, pin.repo.replace("/", "__"), pin.revision)
    onnx_path, vocab_path = os.path.join(base, "onnx", "model.onnx"), os.path.join(base, "vocab.txt")
    for p in (onnx_path, vocab_path):
        if not os.path.isfile(p):
            raise SemanticUnavailable("weights_missing", p)
    for p, want in ((onnx_path, pin.onnx_sha256), (vocab_path, pin.vocab_sha256)):
        got = _sha256(p)
        if got != want:
            raise SemanticUnavailable(
                "pin_mismatch", f"{os.path.relpath(p, base)} sha256 {got[:16]}... != pinned {want[:16]}...")
    try:
        so = ort.SessionOptions()
        so.intra_op_num_threads = int(os.environ.get("LTS_SEMANTIC_THREADS", "0") or 0)
        session = ort.InferenceSession(onnx_path, so, providers=["CPUExecutionProvider"])
        return Embedder(pin, session, WordPieceTokenizer(vocab_path), max_len)
    except Exception as ex:  # noqa: BLE001 -- any loader fault is "unavailable", reported
        raise SemanticUnavailable("load_failed", f"{type(ex).__name__}: {ex}") from None
