"""[SEM-EMBEDDER] S1: the offline, hash-pinned embedder and its failure contract.

The contract under test (ISSUE-SEMANTIC-DESIGN honesty rules 4, 7, 8):
  * weights load ONLY from a pinned local path, and only if BOTH the ONNX file and the vocab
    match their sha256 pins; a mismatch is REFUSED (rule 7: a drifted model is refused,
    never mixed);
  * a missing runtime, a missing file or a pin mismatch makes semantic UNAVAILABLE, a
    reportable status, never an exception that could take down an exact query (rule 4);
  * loading and embedding open no network connection (rule 8);
  * embeddings are deterministic (same text -> bit-identical vector) and unit-norm.

The model here is a tiny HERMETIC ONNX graph built in the test (an embedding-table Gather),
so the suite needs no downloaded weights. An integration test against the real pinned
weights runs only where they are staged, and SKIPS loudly elsewhere.
"""
from __future__ import annotations

import hashlib
import os
import socket

import pytest

np = pytest.importorskip("numpy")
ort = pytest.importorskip("onnxruntime")
onnx = pytest.importorskip("onnx")

from factlattice_engine.semantic import embedder as E  # noqa: E402

from tests.semantic_helpers import VOCAB, DIM, _sha, _tiny_model  # noqa: E402


@pytest.fixture()
def tiny(tmp_path):
    return str(tmp_path), _tiny_model(str(tmp_path))


def test_loads_and_embeds_unit_norm(tiny):
    root, pin = tiny
    emb = E.load_embedder(pin, root)
    v = emb.embed(["hello world", "tile map version field"], query=False)
    assert v.shape == (2, DIM) and v.dtype == np.float32
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-5)
    assert emb.model_id == f"tiny@{pin.onnx_sha256[:12]}"


def test_deterministic_bit_identical(tiny):
    root, pin = tiny
    a = E.load_embedder(pin, root).embed(["hello, world!"], query=True)
    b = E.load_embedder(pin, root).embed(["hello, world!"], query=True)
    assert a.tobytes() == b.tobytes()


def test_query_prefix_is_applied_only_to_queries(tiny):
    root, pin = tiny
    emb = E.load_embedder(pin, root)
    assert emb.embed(["hello"], query=True).tobytes() != emb.embed(["hello"], query=False).tobytes()


def test_onnx_pin_mismatch_is_refused(tiny):
    root, pin = tiny
    bad = E.ModelPin(**{**pin.__dict__, "onnx_sha256": "0" * 64})
    with pytest.raises(E.SemanticUnavailable) as ei:
        E.load_embedder(bad, root)
    assert ei.value.status == "pin_mismatch" and "model.onnx" in str(ei.value)


def test_vocab_pin_mismatch_is_refused(tiny):
    root, pin = tiny
    bad = E.ModelPin(**{**pin.__dict__, "vocab_sha256": "f" * 64})
    with pytest.raises(E.SemanticUnavailable) as ei:
        E.load_embedder(bad, root)
    assert ei.value.status == "pin_mismatch" and "vocab.txt" in str(ei.value)


def test_swapped_weight_file_is_refused(tiny):
    """A same-named file with different bytes (the 'model drifted under us' case)."""
    root, pin = tiny
    p = os.path.join(root, "test__tiny", "r0", "onnx", "model.onnx")
    with open(p, "ab") as fh:
        fh.write(b"\0")
    with pytest.raises(E.SemanticUnavailable) as ei:
        E.load_embedder(pin, root)
    assert ei.value.status == "pin_mismatch"


def test_missing_weights_are_unavailable(tmp_path):
    pin = E.ModelPin(name="ghost", repo="no/such", revision="r", onnx_sha256="0" * 64,
                     vocab_sha256="0" * 64, pooling="cls", query_prefix="", doc_prefix="", dim=4)
    with pytest.raises(E.SemanticUnavailable) as ei:
        E.load_embedder(pin, str(tmp_path))
    assert ei.value.status == "weights_missing"


def test_missing_runtime_is_unavailable(tiny, monkeypatch):
    root, pin = tiny
    monkeypatch.setattr(E, "_import_runtime", lambda: (_ for _ in ()).throw(ImportError("no onnxruntime")))
    with pytest.raises(E.SemanticUnavailable) as ei:
        E.load_embedder(pin, root)
    assert ei.value.status == "runtime_missing"


def test_no_network_during_load_and_embed(tiny, monkeypatch):
    root, pin = tiny

    def _deny(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket, "socket", _deny)
    monkeypatch.setattr(socket, "create_connection", _deny)
    monkeypatch.setattr(socket, "getaddrinfo", _deny)
    E.load_embedder(pin, root).embed(["hello world"], query=True)


def test_core_engine_imports_without_the_semantic_extra():
    """[DEC-NO-LLM] as amended: the core engine has zero model dependencies. Importing the
    engine must not import numpy or onnxruntime."""
    import subprocess, sys
    code = ("import sys; import factlattice_engine.tools, factlattice_engine.server; "
            "bad=[m for m in ('onnxruntime','numpy') if m in sys.modules]; "
            "print(','.join(bad)); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert r.returncode == 0, f"core engine imported: {r.stdout.strip()} {r.stderr[-300:]}"


# --- integration: the real pinned weights, where staged ------------------------------
REAL = os.environ.get("LTS_SEMANTIC_MODEL_DIR", os.path.expanduser("~/.lts-engine/models"))


def test_real_bge_small_canonical_tokens_and_load():
    pin = E.PINS.get("bge-small")
    if pin is None or not os.path.isdir(os.path.join(REAL, pin.repo.replace("/", "__"), pin.revision)):
        pytest.skip("bge-small weights not staged at LTS_SEMANTIC_MODEL_DIR (integration only)")
    emb = E.load_embedder(pin, REAL)
    ids, unk = emb.tokenizer.encode("Hello, world!")
    assert ids == [101, 7592, 1010, 2088, 999, 102] and unk == 0   # canonical bert-base-uncased
    v = emb.embed(["Ledger version header"], query=True)
    assert v.shape == (1, 384)
