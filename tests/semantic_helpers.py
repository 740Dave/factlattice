"""Shared hermetic fixtures for the semantic tests: a tiny ONNX 'embedder' built in-test."""
from __future__ import annotations

import hashlib
import os

import numpy as np

from factlattice_engine.semantic import embedder as E

VOCAB = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "hello", "world", "zero", "##map", "map",
         "version", "field", ",", "!", "?", "-", "_", "sw"]
DIM = 8


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _tiny_model(root, repo="test/tiny", revision="r0"):
    """Gather(table, input_ids) -> last_hidden_state [B, L, DIM]; attention_mask and
    token_type_ids are declared inputs (consumed by an Identity) like a real BERT export."""
    import onnx
    from onnx import TensorProto, helper, numpy_helper
    d = os.path.join(root, repo.replace("/", "__"), revision)
    os.makedirs(os.path.join(d, "onnx"), exist_ok=True)
    rng = np.random.default_rng(7)
    table = rng.standard_normal((len(VOCAB), DIM)).astype(np.float32)
    g = helper.make_graph(
        [helper.make_node("Gather", ["table", "input_ids"], ["last_hidden_state"], axis=0),
         helper.make_node("Identity", ["attention_mask"], ["am_out"]),
         helper.make_node("Identity", ["token_type_ids"], ["tt_out"])],
        "tiny",
        [helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["b", "l"]),
         helper.make_tensor_value_info("attention_mask", TensorProto.INT64, ["b", "l"]),
         helper.make_tensor_value_info("token_type_ids", TensorProto.INT64, ["b", "l"])],
        [helper.make_tensor_value_info("last_hidden_state", TensorProto.FLOAT, ["b", "l", DIM]),
         helper.make_tensor_value_info("am_out", TensorProto.INT64, ["b", "l"]),
         helper.make_tensor_value_info("tt_out", TensorProto.INT64, ["b", "l"])],
        [numpy_helper.from_array(table, "table")])
    m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)])
    m.ir_version = 8
    onnx.save(m, os.path.join(d, "onnx", "model.onnx"))
    with open(os.path.join(d, "vocab.txt"), "w") as fh:
        fh.write("\n".join(VOCAB) + "\n")
    pin = E.ModelPin(name="tiny", repo=repo, revision=revision,
                     onnx_sha256=_sha(os.path.join(d, "onnx", "model.onnx")),
                     vocab_sha256=_sha(os.path.join(d, "vocab.txt")),
                     pooling="mean", query_prefix="search query: ", doc_prefix="", dim=DIM)
    return pin


