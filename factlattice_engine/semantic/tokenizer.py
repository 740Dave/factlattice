"""Pure-Python BERT-uncased WordPiece tokenizer over a pinned `vocab.txt`.

Why not HF `tokenizers`: it is not installed, and installing it is a second network act
beyond the maintainer-approved one-time weight download (ISSUE-SEMANTIC-DESIGN
MODEL, gate a). Every candidate model declares the SAME pipeline in its `tokenizer.json`
(BertNormalizer{clean_text, handle_chinese_chars, lowercase, strip_accents=None -> follows
lowercase}, BertPreTokenizer, WordPiece{"##", max_input_chars_per_word=100, unk="[UNK]"},
TemplateProcessing `[CLS] A [SEP]`), and all six pinned `vocab.txt` files are byte-identical
(sha256 07eced37...). This module implements exactly that declared pipeline and no more.

⚠ RESIDUAL (named, not hidden): parity is checked against the declared pipeline, the vocab's
own splits and a READING of the Rust source (huggingface/tokenizers tk-encode normalizers/bert.rs,
2026-10-02, and the unicode_categories 0.1.1 crate it calls: is_control, CJK ranges). It is NOT checked against a running Rust tokenizer,
which is not installed.
`unk_rate` lets a caller see when input falls outside what this path covers.
"""
from __future__ import annotations

import unicodedata
from typing import Dict, List, Tuple

CLS, SEP, UNK, PAD = "[CLS]", "[SEP]", "[UNK]", "[PAD]"
# Bump on ANY change to normalisation, pre-tokenisation or WordPiece here: it is part of every
# index_version, so an index tokenised differently is refused, never mixed (F-G2).
TOKENIZER_VERSION = 4   # 2: Zl/Zp (U+2028/U+2029) are whitespace, as in the Rust BertNormalizer
                        # 3: (unlanded) controls + Co/Cn/Cs, CJK 0x2B920
                        # 4: controls = the crate's is_other CODE (Cc Cf Co; Cn KEPT), Cs dropped
                        #    (Rust str cannot hold it); CJK 0x2B920; per-character lowercase
MAX_CHARS_PER_WORD = 100


def _is_whitespace(ch: str) -> bool:
    """The reference (Rust `tokenizers`) BertNormalizer/BertPreTokenizer whitespace: tab, LF,
    CR, plus Rust's char::is_whitespace. Control characters are removed BEFORE this test, so the
    remainder is exactly the Unicode space, line and paragraph separators (Zs, Zl, Zp)."""
    if ch in (" ", "\t", "\n", "\r"):
        return True
    return unicodedata.category(ch) in ("Zs", "Zl", "Zp")


def _is_control(ch: str) -> bool:
    """Rust BertNormalizer `is_control` = `unicode_categories` `is_other()` except tab/LF/CR.
    The crate's CODE is Cc|Cf|Co; its doc comment and bert.rs's both say "Cn" too, but there is no
    Cn table, so an unassigned code point is KEPT (and WordPiece maps it to [UNK]). Cs is dropped
    here only because a Rust `str` cannot hold a lone surrogate at all.
    ⚠ RESIDUAL: categories come from Python's Unicode tables (`unidata_version`, in index_version),
    Rust's from the crate's older tables: ~20 newer Cf code points still diverge."""
    if ch in ("\t", "\n", "\r"):
        return False
    return unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs")


def _is_punctuation(ch: str) -> bool:
    cp = ord(ch)
    if 33 <= cp <= 47 or 58 <= cp <= 64 or 91 <= cp <= 96 or 123 <= cp <= 126:
        return True
    return unicodedata.category(ch).startswith("P")


def _is_cjk(cp: int) -> bool:
    # Rust tokenizers' ranges, which start the last block at 0x2B920 (original BERT: 0x2B820).
    return (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0x20000 <= cp <= 0x2A6DF
            or 0x2A700 <= cp <= 0x2B73F or 0x2B740 <= cp <= 0x2B81F or 0x2B920 <= cp <= 0x2CEAF
            or 0xF900 <= cp <= 0xFAFF or 0x2F800 <= cp <= 0x2FA1F)


def normalize(text: str) -> str:
    """BertNormalizer: clean_text -> handle_chinese_chars -> strip_accents -> lowercase."""
    out = []
    for ch in text:
        cp = ord(ch)
        if cp == 0 or cp == 0xFFFD or _is_control(ch):
            continue
        if _is_whitespace(ch):
            out.append(" ")
        elif _is_cjk(cp):
            out.append(f" {ch} ")
        else:
            out.append(ch)
    s = unicodedata.normalize("NFD", "".join(out))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return "".join(c.lower() for c in s)   # per character, as Rust: no final-sigma context


def pre_tokenize(text: str) -> List[str]:
    """BertPreTokenizer: split on whitespace (dropped) and punctuation (isolated)."""
    words: List[str] = []
    cur: List[str] = []
    for ch in text:
        if _is_whitespace(ch):
            if cur:
                words.append("".join(cur)); cur = []
        elif _is_punctuation(ch):
            if cur:
                words.append("".join(cur)); cur = []
            words.append(ch)
        else:
            cur.append(ch)
    if cur:
        words.append("".join(cur))
    return words


class WordPieceTokenizer:
    def __init__(self, vocab_path: str):
        with open(vocab_path, "r", encoding="utf-8") as fh:
            tokens = [line.rstrip("\n") for line in fh]
        self.vocab: Dict[str, int] = {t: i for i, t in enumerate(tokens)}
        for special in (CLS, SEP, UNK, PAD):
            if special not in self.vocab:
                raise ValueError(f"vocab missing {special}")
        self.cls_id, self.sep_id = self.vocab[CLS], self.vocab[SEP]
        self.unk_id, self.pad_id = self.vocab[UNK], self.vocab[PAD]
        self._cache: Dict[str, List[str]] = {}

    def _wordpiece(self, word: str) -> List[str]:
        hit = self._cache.get(word)
        if hit is not None:
            return hit
        if len(word) > MAX_CHARS_PER_WORD:
            pieces = [UNK]
        else:
            pieces, start, bad = [], 0, False
            while start < len(word):
                end, cur = len(word), None
                while start < end:
                    sub = word[start:end]
                    if start > 0:
                        sub = "##" + sub
                    if sub in self.vocab:
                        cur = sub
                        break
                    end -= 1
                if cur is None:
                    bad = True
                    break
                pieces.append(cur)
                start = end
            if bad:
                pieces = [UNK]
        if len(self._cache) < 200_000:
            self._cache[word] = pieces
        return pieces

    def tokenize(self, text: str) -> List[str]:
        out: List[str] = []
        for w in pre_tokenize(normalize(text)):
            out.extend(self._wordpiece(w))
        return out

    def encode(self, text: str, max_len: int = 512) -> Tuple[List[int], int]:
        """`([CLS] ids [SEP])` truncated to `max_len` INCLUDING the specials, plus the
        number of [UNK] pieces (so a caller can report an unk rate)."""
        pieces = self.tokenize(text)[: max_len - 2]
        ids = [self.cls_id] + [self.vocab[p] for p in pieces] + [self.sep_id]
        return ids, sum(1 for p in pieces if p == UNK)
