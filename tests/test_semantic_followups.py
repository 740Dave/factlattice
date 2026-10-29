"""Post-land follow-ups (composition-delta review F-1/F-2, followups items 7 and 9)."""
from __future__ import annotations

import os
import stat

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")

from factlattice_engine.semantic import embedder as E  # noqa: E402
from factlattice_engine.semantic import index as I  # noqa: E402
from factlattice_engine.semantic import tokenizer as T  # noqa: E402
from tests.test_semantic_index import CountingEmbedder, _part, _write_corpus  # noqa: E402
from tests.semantic_helpers import _tiny_model  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    pin = _tiny_model(str(tmp_path / "models"))
    emb = CountingEmbedder(E.load_embedder(pin, str(tmp_path / "models")))
    _write_corpus(tmp_path / "corpus")
    return tmp_path, emb


def test_F1_the_embed_implementation_is_versioned(env):
    """composition-delta F-1: pooling maths / normalisation / padding live in embed(); a change
    there must invalidate sidecars, via EMBEDDER_VERSION in the version material."""
    _tmp, emb = env
    assert f"emb:{E.EMBEDDER_VERSION}" in I._version_material(emb.model_id, emb.inner)


def test_F2_a_read_only_load_tightens_old_0755_dirs(env):
    tmp, emb = env
    idx = I.SemanticIndex(str(tmp / "state"), "c", emb)
    idx.refresh(_part(tmp))
    d, dirs = idx.dir, []          # every level, whatever the layout ([SIDECAR-PER-VERSION]: 4)
    while d != str(tmp / "state"):
        dirs.append(d)
        d = os.path.dirname(d)
    assert len(dirs) == 4
    for d in dirs:
        os.chmod(d, 0o755)                                   # an index from an older build
    I.SemanticIndex(str(tmp / "state"), "c", emb)._load()    # read-only path
    for d in dirs:
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700, d


def test_item7_unicode_line_and_paragraph_separators_are_whitespace():
    assert T.pre_tokenize(T.normalize("zero map version")) == ["zero", "map", "version"]
    assert T.TOKENIZER_VERSION >= 2       # a tokenizer change bumps the version (checklist rule)


def test_item9_ratified_proposal_header_is_not_stale():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo, "proposals", "dec-semantic-assist-amends-no-llm.md")
    if not os.path.isfile(path):
        pytest.skip("internal proposal document is not part of a published tree")
    txt = open(path).read()
    assert "RATIFIED" in txt.split("\n\n")[1] or "RATIFIED" in txt[:600]
