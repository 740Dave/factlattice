"""Post-land follow-up P2s (audit-SEMANTIC-FOLLOWUPS-2026-10-02) + P3s.

Reference read 2026-10-02: huggingface/tokenizers `tokenizers/tk-encode/src/normalizers/bert.rs`:
  is_control(c) = c.is_other() except \\t \\n \\r
  ⚠ bert.rs's COMMENT says "Cc, Cf, Cn or Co"; the unicode_categories 0.1.1 CODE (lib.rs is_other)
  is Cc|Cf|Co, with no Cn table (review audit-SEMANTIC-TOKENIZER-PARITY F1). The code is what runs.
  is_chinese_char ranges include 0x2B920..=0x2CEAF  (NOT 0x2B820, the original Python BERT value)
"""
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
from tests.test_semantic_index import CountingEmbedder  # noqa: E402
from tests.semantic_helpers import _tiny_model  # noqa: E402


def _emb(tmp_path):
    pin = _tiny_model(str(tmp_path / "models"))
    return CountingEmbedder(E.load_embedder(pin, str(tmp_path / "models")))


@pytest.mark.parametrize("ch,cat", [("\ue000", "Co"),        # private use
                                    ("\U000f0000", "Co"),    # supplementary private use
                                    ("\ud800", "Cs")])       # lone surrogate (surrogateescape input)
def test_rust_is_other_characters_are_removed_like_controls(ch, cat):
    import unicodedata
    assert unicodedata.category(ch) == cat
    assert T.normalize(f"led{ch}ger") == "ledger"


def test_unassigned_cn_is_KEPT_as_in_rust():
    """Rust keeps Cn (WordPiece then maps it to [UNK]); deleting it diverges on ~825k points."""
    import unicodedata
    assert unicodedata.category("\u0378") == "Cn"
    assert T.normalize("zero\u0378map") == "zero\u0378map"


def test_lowercase_is_per_character_like_rust_no_final_sigma_context():
    assert T.normalize("\u039f\u0394\u039f\u03a3") == "\u03bf\u03b4\u03bf\u03c3"   # ΟΔΟΣ -> οδοσ


def test_index_version_material_names_unicode_tables_and_numpy(tmp_path):
    import unicodedata
    import numpy
    emb = _emb(tmp_path)
    raw = I._version_material(emb.model_id, emb.inner)
    assert f"|ucd:{unicodedata.unidata_version}" in raw
    assert f"|np:{numpy.__version__}" in raw


def test_tab_newline_cr_still_become_spaces_not_removed():
    assert T.normalize("tile\tmap\nversion\rx") == "tile map version x"


def test_cjk_extension_range_matches_rust_not_original_bert():
    ext_e_gap = "\U0002b820"          # inside 0x2B820..0x2B91F: Rust does NOT treat it as CJK
    in_range = "\U0002b920"            # Rust's range starts here
    assert T.normalize(f"a{ext_e_gap}b") == f"a{ext_e_gap}b"
    assert T.normalize(f"a{in_range}b") == f"a {in_range} b"


def test_tokenizer_version_bumped_for_this_change():
    assert T.TOKENIZER_VERSION >= 4


def test_index_version_material_names_runtime_and_storage_dtype(tmp_path):
    import onnxruntime
    emb = _emb(tmp_path)
    raw = I._version_material(emb.model_id, emb.inner)
    assert f"|ort:{onnxruntime.__version__}" in raw
    assert f"|store:{I.STORAGE_DTYPE}" in raw


def test_read_path_tightens_each_dir_independently(tmp_path, monkeypatch):
    """One un-chmod-able dir must not stop the others being tightened (P3: one try wrapped all)."""
    emb = _emb(tmp_path)
    idx = I.SemanticIndex(str(tmp_path / "state"), "c", emb)
    os.makedirs(idx.dir)
    top = os.path.dirname(os.path.dirname(idx.dir))
    for d in (top, os.path.dirname(idx.dir), idx.dir):
        os.chmod(d, 0o755)
    real = os.chmod

    def chmod(p, m):
        if os.path.abspath(p) == os.path.abspath(top):
            raise PermissionError("not ours")
        return real(p, m)
    monkeypatch.setattr(I.os, "chmod", chmod)
    idx._tighten_dirs_best_effort()
    assert stat.S_IMODE(os.stat(os.path.dirname(idx.dir)).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(idx.dir).st_mode) == 0o700


def test_item9_ratified_header_is_the_first_line():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo, "proposals", "dec-semantic-assist-amends-no-llm.md")
    if not os.path.isfile(path):
        pytest.skip("internal proposal document is not part of a published tree")
    first = open(path).readline()
    assert first.startswith("# RATIFIED") and "specs/50-decisions.lts" in first
