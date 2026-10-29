"""[SEM-INDEX] S2: the per-corpus embedding sidecar and its freshness contract.

Contract (design §2 Freshness/Storage; honesty rule 7):
  * one directory per (model, corpus) under the engine's state root, mode 0700, OUTSIDE corpus
    roots; per-corpus only, so a filter bug cannot leak across corpora (nothing co-resident);
  * refresh is INCREMENTAL by per-node card fingerprint: an edited node re-embeds exactly its
    own cards, a no-op refresh embeds nothing, a deleted node is dropped;
  * an index built under another card schema / model is never mixed: refresh rebuilds it and
    says why; search on it refuses (IndexIncompatible);
  * writes are crash-safe: vectors land via temp+rename, the manifest is written LAST and pins
    the vectors' sha256, so a torn write reads as corrupt, never as a smaller index.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")

from factlattice_engine import config  # noqa: E402
from factlattice_engine.semantic import cards as C  # noqa: E402
from factlattice_engine.semantic import embedder as E  # noqa: E402
from factlattice_engine.semantic import index as I  # noqa: E402
from tests.semantic_helpers import _tiny_model  # noqa: E402


class CountingEmbedder:
    """Wraps a real embedder and counts the texts it is asked to embed."""

    def __init__(self, inner):
        self.inner, self.texts = inner, 0
        self.model_id, self.pin = inner.model_id, inner.pin

    def embed(self, texts, query):
        self.texts += len(texts)
        return self.inner.embed(texts, query)


def _write_corpus(root, extra=""):
    root.mkdir(parents=True, exist_ok=True)
    (root / "00-c.lts").write_text(
        "# Corpus [DOC-C]\n\n"
        "## Hello world node [N-1]\n\n- status: active\n- what: hello world\n\nhello world prose\n\n"
        "## Tile map node [N-2]\n\n- what: tile map version field\n\ntile map prose\n\n"
        "## Version field node [N-3]\n\n- what: version field\n" + extra, encoding="utf-8")


def _part(tmp_path):
    store = config.build_store({"c": {"roots": [{"path": str(tmp_path / "corpus"), "area": "specs"}],
                                      "domain_suffix": "proj.c"}})
    return store.get_partition("c")


@pytest.fixture()
def env(tmp_path):
    model_root = tmp_path / "models"
    pin = _tiny_model(str(model_root))
    emb = CountingEmbedder(E.load_embedder(pin, str(model_root)))
    _write_corpus(tmp_path / "corpus")
    return tmp_path, emb


def test_first_refresh_embeds_everything_then_a_noop_refresh_embeds_nothing(env):
    tmp, emb = env
    idx = I.SemanticIndex(str(tmp / "state"), "c", emb)
    s1 = idx.refresh(_part(tmp))
    assert s1["embedded_nodes"] == 4 and s1["dropped_nodes"] == 0     # DOC-C + 3 nodes
    before = emb.texts
    s2 = idx.refresh(_part(tmp))
    assert s2["embedded_nodes"] == 0 and s2["unchanged_nodes"] == 4
    assert emb.texts == before


def test_an_edited_node_reembeds_exactly_its_own_cards(env):
    tmp, emb = env
    idx = I.SemanticIndex(str(tmp / "state"), "c", emb)
    idx.refresh(_part(tmp))
    p = tmp / "corpus" / "00-c.lts"
    p.write_text(p.read_text().replace("tile map prose", "tile map prose, edited"), encoding="utf-8")
    before = emb.texts
    s = idx.refresh(_part(tmp))
    assert s["embedded_nodes"] == 1 and s["unchanged_nodes"] == 3, s
    n2_cards = len(C.node_cards("N-2", _part(tmp).nodes["N-2"], ["tile map prose, edited"]))
    assert emb.texts - before == n2_cards


def test_a_deleted_node_is_dropped_and_never_returned(env):
    tmp, emb = env
    idx = I.SemanticIndex(str(tmp / "state"), "c", emb)
    idx.refresh(_part(tmp))
    p = tmp / "corpus" / "00-c.lts"
    txt = p.read_text()
    p.write_text(txt[:txt.index("## Version field node")], encoding="utf-8")
    s = idx.refresh(_part(tmp))
    assert s["dropped_nodes"] == 1
    qv = emb.embed(["version field"], query=True)[0]
    assert "N-3" not in [n for n, _s, _v in idx.search(qv, 10)]


def test_search_returns_one_row_per_node_best_first(env):
    tmp, emb = env
    idx = I.SemanticIndex(str(tmp / "state"), "c", emb)
    idx.refresh(_part(tmp))
    hits = idx.search(emb.embed(["tile map version"], query=True)[0], 10)
    ids = [n for n, _s, _v in hits]
    assert len(ids) == len(set(ids)) and set(ids) <= {"DOC-C", "N-1", "N-2", "N-3"}
    assert [s for _n, s, _v in hits] == sorted([s for _n, s, _v in hits], reverse=True)
    assert all(v in ("head", "chunk") for _n, _s, v in hits)


def test_storage_is_private_per_model_per_corpus_and_crash_safe(env):
    tmp, emb = env
    idx = I.SemanticIndex(str(tmp / "state"), "c", emb)
    idx.refresh(_part(tmp))
    d = idx.dir
    assert d.startswith(str(tmp / "state")) and emb.model_id.split("@")[0] in d and d.endswith("/c")
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert not [f for f in os.listdir(d) if f.endswith(".tmp")]
    man = json.load(open(os.path.join(d, "manifest.json")))
    assert man["corpus"] == "c" and man["model_id"] == emb.model_id and "vectors_sha256" in man
    # a torn/corrupt vectors file reads as corrupt, never as a smaller index
    with open(os.path.join(d, "vectors.npy"), "r+b") as fh:
        fh.seek(200); fh.write(b"\xff\xff\xff\xff")
    fresh = I.SemanticIndex(str(tmp / "state"), "c", emb)
    with pytest.raises(I.IndexIncompatible) as ei:
        fresh.search(emb.embed(["hello"], query=True)[0], 3)
    assert ei.value.status == "index_corrupt"


def test_a_card_schema_change_is_refused_for_search_and_rebuilt_by_refresh(env, monkeypatch):
    tmp, emb = env
    I.SemanticIndex(str(tmp / "state"), "c", emb).refresh(_part(tmp))
    monkeypatch.setattr(C, "CARD_SCHEMA_VERSION", C.CARD_SCHEMA_VERSION + 1)
    idx = I.SemanticIndex(str(tmp / "state"), "c", emb)
    with pytest.raises(I.IndexIncompatible) as ei:
        idx.search(emb.embed(["hello"], query=True)[0], 3)
    assert ei.value.status == "index_incompatible"
    s = idx.refresh(_part(tmp))
    assert s["rebuilt_reason"] == "index_incompatible" and s["embedded_nodes"] == 4


def test_missing_index_is_reported_not_searched_as_empty(env):
    tmp, emb = env
    idx = I.SemanticIndex(str(tmp / "state"), "c", emb)
    with pytest.raises(I.IndexIncompatible) as ei:
        idx.search(emb.embed(["hello"], query=True)[0], 3)
    assert ei.value.status == "index_missing"


def test_index_version_covers_every_vector_shaping_parameter(env):
    """S1-S4 review P3-6: pooling, query/doc prefix and max_len shape the vectors, so they must
    be in index_version; otherwise a pin change with the same ONNX file mixes two vector spaces."""
    tmp, emb = env
    base = I.index_version(emb.model_id, emb.inner)
    for field, value in (("pooling", "cls"), ("query_prefix", "q: "), ("doc_prefix", "d: ")):
        other = E.ModelPin(**{**emb.pin.__dict__, field: value})
        if getattr(emb.pin, field) == value:
            continue
        assert I.index_version(emb.model_id, E.Embedder(other, emb.inner.session, emb.inner.tokenizer,
                                                        emb.inner.max_len)) != base, field
    assert I.index_version(emb.model_id, E.Embedder(emb.pin, emb.inner.session, emb.inner.tokenizer,
                                                    256)) != base, "max_len"


def test_index_version_covers_the_tokenizer(env):
    """Composition review F-G2: a same-ONNX pin with a different VOCAB loaded an old sidecar as
    compatible. The vocab sha and the tokenizer implementation version must be in index_version."""
    tmp, emb = env
    base = I.index_version(emb.model_id, emb.inner)
    swapped = E.ModelPin(**{**emb.pin.__dict__, "vocab_sha256": "e" * 64})
    assert I.index_version(emb.model_id, E.Embedder(swapped, emb.inner.session, emb.inner.tokenizer,
                                                    emb.inner.max_len)) != base
    from factlattice_engine.semantic import tokenizer as T
    assert f"tok:{T.TOKENIZER_VERSION}" in I._version_material(emb.model_id, emb.inner)


def test_sidecar_parent_dirs_are_private_too(env):
    """S1-S4 P3-2 / composition gap: semantic/ and semantic/<model>/ were 0755, so corpus names
    (including private ones) were listable."""
    import stat
    tmp, emb = env
    I.SemanticIndex(str(tmp / "state"), "c", emb).refresh(_part(tmp))
    for d in (tmp / "state" / "semantic", tmp / "state" / "semantic" / emb.model_id):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700, d
