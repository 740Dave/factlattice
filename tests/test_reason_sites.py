"""[TOOL-REASON-IS-A-FIXED-CODE]: a TRIPWIRE, not a proof.

THIS SCAN IS A TRIPWIRE for the LITERAL, F-STRING, FORMAT-CALL and CONCATENATION forms of a `reason` (a dict entry `"reason": X`, a
keyword `reason=X`, and the first positional argument of `PreviewRefusal(...)`). It CANNOT SEE these five shapes, and nothing here
claims otherwise:
  1. a variable built elsewhere (`r = f"..."` ... `{"reason": r}`);
  2. a subscript assignment (`d["reason"] = f"..."`);
  3. a conditional expression value (`"reason": a if c else f"..."`);
  4. a subscript value (`"reason": other["reason"]`, `"reason": parts[0]`);
  5. `d.setdefault("reason", f"...")`.
The EVIDENCE is the runtime battery (tests/test_tool_reason_codes.py), which walks real replies; this scan only catches the cheap, common
regressions early. Exemptions are PER SITE (file + exact source text), each with the reason it is allowed; there is no file-wide exemption.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "factlattice_engine"


def _functions(tree):
    """node id -> name of the enclosing function (`<module>` outside any)."""
    owner = {}

    def walk(n, fn):
        for c in ast.iter_child_nodes(n):
            f = c.name if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            owner[id(c)] = f
            walk(c, f)
    walk(tree, "<module>")
    return owner


def _reason_sites_in(path):
    """(lineno, node, enclosing function) for every `"reason": <value>` dict entry, every `reason=<value>` keyword and the first
    positional argument of a `PreviewRefusal(...)` call in a file."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    owner = _functions(tree)
    for n in ast.walk(tree):
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value == "reason":
                    yield v.lineno, v, owner.get(id(v), "<module>")
        elif isinstance(n, ast.keyword) and n.arg == "reason":
            yield n.value.lineno, n.value, owner.get(id(n.value), "<module>")
        elif isinstance(n, ast.Call) and getattr(n.func, "id", None) == "PreviewRefusal" and n.args:
            yield n.args[0].lineno, n.args[0], owner.get(id(n.args[0]), "<module>")


def _reason_sites(path):
    """(lineno, node) pairs, as before."""
    for ln, v, _fn in _reason_sites_in(path):
        yield ln, v


# PER-SITE exemptions, keyed by (file relative to factlattice_engine/, ENCLOSING FUNCTION, exact source text of the value) -> why.
# A second identical text in ANOTHER function of the same file is not exempt (test_a_second_str_exc_in_another_function_is_not_exempt).
EXEMPT_SITES = {
    ("authoring.py", "_refusal", "str(exc)"): "typed writers: the top-level refusal sentence is pinned (code = evidence.refusal)",
    ("authoring.py", "_outcome_unknown", "UNKNOWN_REASON"): "typed writers: a module constant",
    ("authoring.py", "add_node", "reason"): "typed writers: the pass-through of the writers' own refusal",
    ("authoring.py", "_ledger_kw", "reason"): "typed writers: the caller's audit `reason` metadata, not a reply field",
    ("freshness.py", "main", "repr(exc)"): "the freshness CLI's stdout record, not a tool reply",
    ("freshness.py", "main", "'authoritative source is empty (no .lts under --corpus)'"): "the freshness CLI's stdout record, not a tool reply",
    ("freshness.py", "main", "'index missing source_corpus.files_hash'"): "the freshness CLI's stdout record, not a tool reply",
    ("semantic/embedd.py", "_count_start", "f'crash-loop limit: {n} starts within {window_s:.0f} s'"): "the embedder daemon's marker FILE, not a reply (named gap)",
    ("semantic/embedd.py", "_count_start", "f'uncountable: {type(ex).__name__}: {ex}'"): "the embedder daemon's marker FILE, not a reply (named gap)",
    ("server.py", "<module>", "{'type': 'string', 'description': 'OPTIONAL audit metadata, <= 2000 chars: why this change was made. Stored literally in the ledger'}"):
        "a JSON-schema property named `reason`, not a reply",
    ("preview.py", "to_dict", "self.reason"): "PreviewRefusal.to_dict: the constructor argument is scanned at every call site",
    ("reverse_index.py", "_file_reference", "'unresolved_domain' if not candidates else 'ambiguous_domain'"):
        "a conditional between two literal codes (shape 3 above; read by hand)",
    ("reviewer/__init__.py", "to_dict", "self.reason"): "Resolution.to_dict: built from RESOLVE_REFUSALS codes",
    ("reviewer/__init__.py", "evidence", "None"): "no reason",
    ("reviewer/__init__.py", "compare_registries", "None"): "no reason",
    ("reviewer/__init__.py", "evidence", "why"): "reviewer-internal pass-through of a code",
    ("reviewer/__init__.py", "scan_secrets", "why"): "reviewer-internal pass-through of a code",
    ("reviewer/__init__.py", "refuse", "code"): "refuse(code): a key of RESOLVE_REFUSALS",
    ("reviewer/__init__.py", "check_registrations", "''"): "an empty reason on a success",
    ("reviewer/__main__.py", "_main", "''"): "an empty reason on a success",
    ("reviewer/__main__.py", "_fail", "code"): "_fail(code): HELPER_REASON_CODES",
    ("reviewer/__main__.py", "_registry_stop", "registry.CODE"): "a constant code",
    ("semantic/suggest.py", "_live_reason", "code"): "_live_reason(code): a fixed code from the callers",
}


def _exempt(rel, v, fn):
    return (rel, fn, ast.unparse(v)) in EXEMPT_SITES


def _bad_form(v):
    return (isinstance(v, (ast.JoinedStr, ast.BinOp))
            or (isinstance(v, ast.Call) and not isinstance(v.func, ast.Name))
            or (isinstance(v, ast.Call) and v.func.id in {"str", "repr", "format"}))


def test_the_scan_names_what_it_cannot_see():
    doc = __doc__
    assert "TRIPWIRE" in doc and "CANNOT SEE" in doc
    for shape in ("variable built elsewhere", "subscript assignment", "conditional expression", "subscript value", "setdefault"):
        assert shape in doc, shape
    sdlc = (ROOT / "SDLC.md").read_text(encoding="utf-8")
    row = next(l for l in sdlc.splitlines() if l.startswith("- [ ] [TOOL-REASON-IS-A-FIXED-CODE]"))
    assert "TRIPWIRE" in row and "setdefault" in row and "RUNTIME battery" in row
    assert "tests/test_reason_sites.py` scans the sources for any `reason` that is an f-string or a call" not in row


def test_no_reason_in_the_tool_reply_code_is_an_fstring_a_call_or_a_format():
    offenders = []
    for f in sorted(PKG.rglob("*.py")):
        rel = f.relative_to(PKG).as_posix()
        for ln, v, fn in _reason_sites_in(f):
            if _bad_form(v) and not _exempt(rel, v, fn):
                offenders.append(f"{rel}:{ln}:{fn}: {ast.unparse(v)[:90]}")
    assert offenders == [], offenders


def test_every_literal_reason_is_a_code_or_registered():
    from factlattice_engine.reason_codes import info_reasons, is_code
    info = info_reasons()
    bad = []
    for f in sorted(PKG.rglob("*.py")):
        rel = f.relative_to(PKG).as_posix()
        for ln, v, fn in _reason_sites_in(f):
            if isinstance(v, ast.Constant) and isinstance(v.value, str) and not _exempt(rel, v, fn):
                if not (is_code(v.value) or v.value in info):
                    bad.append(f"{rel}:{ln}: {v.value[:70]!r}")
    assert bad == [], bad


def test_the_reviewer_package_is_scanned_and_has_no_file_wide_exemption(tmp_path):
    # round 4: the old file-wide exemption of reviewer/__init__.py hid an f-string reason (the site-overrides-missing check)
    bad = [(ln, ast.unparse(v)) for ln, v, fn in _reason_sites_in(PKG / "reviewer/__init__.py") if _bad_form(v) and not _exempt("reviewer/__init__.py", v, fn)]
    assert bad == [], bad


def test_a_planted_path_in_a_preview_refusals_positional_reason_is_found(tmp_path):
    p = tmp_path / "x.py"
    p.write_text('def f(path):\n    raise PreviewRefusal(f"no such file: {path}", "m")\n', encoding="utf-8")
    (site,) = list(_reason_sites(p))
    assert _bad_form(site[1])
    q = tmp_path / "y.py"
    q.write_text('def f(path):\n    raise PreviewRefusal("file_not_found", "m")\n', encoding="utf-8")
    (site,) = list(_reason_sites(q))
    assert not _bad_form(site[1])


def test_every_preview_refusal_in_the_engine_is_a_literal_code():
    from factlattice_engine.reason_codes import is_code
    sites = [v for ln, v in _reason_sites(PKG / "preview.py") if isinstance(v, ast.Constant) or _bad_form(v)]
    assert len(sites) >= 7
    for v in sites:
        assert isinstance(v, ast.Constant) and is_code(v.value), ast.unparse(v)


def test_the_negative_control_the_scan_can_find_an_interpolated_reason(tmp_path):
    p = tmp_path / "x.py"
    p.write_text('def f(exc, key):\n    return {"reason": f"failed: {exc}", "k": 1}\n', encoding="utf-8")
    sites = list(_reason_sites(p))
    assert len(sites) == 1 and isinstance(sites[0][1], ast.JoinedStr) and _bad_form(sites[0][1])


def test_heal_reasons_are_closed_literals():
    from factlattice_engine.selfheal import HEAL_REASONS
    tree = ast.parse((PKG / "selfheal.py").read_text(encoding="utf-8"))
    found = [kw.value for n in ast.walk(tree) if isinstance(n, ast.Call) for kw in n.keywords if kw.arg == "reason"]
    assert len(found) >= 12
    for v in found:
        assert isinstance(v, ast.Constant) and v.value in HEAL_REASONS, ast.unparse(v)
    assert len(set(HEAL_REASONS)) == len(HEAL_REASONS)


def test_registered_sentences_are_path_free_by_construction():
    from factlattice_engine.reason_codes import info_reasons
    for s in info_reasons():
        assert "{" not in s and "}" not in s, s
        assert not re.search(r"(^|\s)(/[A-Za-z0-9_.-]+/|~/|[A-Za-z]:\\\\)", s), s       # no absolute path inside a sentence
        assert not re.search(r"\d", s), s                                              # no count, no hash


def test_property_loss_classifier_only_returns_registered_reasons():
    from factlattice_engine import property_loss as pl
    from factlattice_engine.reason_codes import info_reasons
    samples = ["- ⛔ FLAGGED 2026-08-10: v", "- **Bold Key**: v", "- Some long sentence like key region here, with words: v",
               "- 9digit: v", "- born-from (refinement): v", "- key,with,comma: v", "- UPPER Key: v"]
    seen = set()
    for body in samples:
        for in_body in (True, False):
            r = pl._classify(body[2:], in_node_body=in_body)
            if r:
                seen.add(r[3])
    assert seen and seen <= info_reasons(), seen - info_reasons()


def test_stale_marker_reason_is_a_code_and_the_message_is_live_only(tmp_path):
    from factlattice_engine.store import FreshnessResult, Partition
    from factlattice_engine import tools
    fr = FreshnessResult("c", False, "a", None, "live-rehash-failed", f"live re-hash failed: OSError('{tmp_path}/zq it\\'s')")
    m = tools._stale_marker(fr)
    assert m["reason"] == "live-rehash-failed" and str(tmp_path) in m["live_detail"] and "NEVER persist" in m["live_detail_note"]
    assert str(tmp_path) not in m["reason"] and str(tmp_path) not in m["error"]


def test_files_unreadable_reason_is_a_code_with_a_live_only_detail(tmp_path):
    src = (PKG / "store.py").read_text(encoding="utf-8")
    assert '"source-file-not-found"' in src and '"source-file-unreadable"' in src
    assert 'unreadable[sf] = f"' not in src


def test_binding_reason_is_a_code_not_the_callers_key():
    from factlattice_engine import binding
    r = binding.analyse("zq it's \"weird\" key", ["[X-1]"], node_id="N-1") if hasattr(binding, "analyse") else {}
    if r.get("reason"):
        assert r["reason"] == "key-not-in-minting-table" and "weird" not in r["reason"]
        assert "weird" in r["binding_summary"]


def test_report_miss_reasons_are_codes():
    src = (PKG / "tools.py").read_text(encoding="utf-8")
    for code in ("corpus-roots-unknown", "miss-log-inside-corpus-root", "miss-log-write-failed"):
        assert f'"reason": "{code}"' in src, code
    assert '"reason": _exc_class(exc)' not in src


def test_the_stale_marker_codes_are_pinned_where_they_are_produced(tmp_path):
    """Kills the mutant that puts a sentence into FreshnessResult's positional `reason` (55 tests survived it)."""
    from factlattice_engine.store import Partition
    from factlattice_engine import store as store_mod
    from factlattice_engine.reason_codes import is_code
    p = Partition.__new__(Partition)
    p.corpus_id, p.files_hash, p.root_paths, p._kernel = "c", "h", [], None
    fr = p.freshness()
    assert fr.reason == "no-root-paths" and is_code(fr.reason) and fr.detail is None
    p.root_paths = [str(tmp_path / "zq it's gone")]
    real = store_mod.combined_files_hash
    store_mod.combined_files_hash = lambda roots: (_ for _ in ()).throw(OSError(f"unreadable {roots}"))
    try:
        fr = p.freshness()
    finally:
        store_mod.combined_files_hash = real
    assert fr.reason == "live-rehash-failed" and is_code(fr.reason)
    assert str(tmp_path) in fr.detail and str(tmp_path) not in fr.reason


def test_every_exemption_matches_a_real_site_and_none_is_dead():
    live = set()
    for f in sorted(PKG.rglob("*.py")):
        rel = f.relative_to(PKG).as_posix()
        for _ln, v, fn in _reason_sites_in(f):
            live.add((rel, fn, ast.unparse(v)))
    assert set(EXEMPT_SITES) <= live, sorted(set(EXEMPT_SITES) - live)


def test_a_second_str_exc_in_another_function_is_not_exempt(tmp_path):
    """The exemption is per (file, function, text): the same `str(exc)` added in a NEW function of authoring.py is an offender."""
    src = (PKG / "authoring.py").read_text(encoding="utf-8") + '\n\ndef a_new_writer_path(exc):\n    return {"reason": str(exc)}\n'
    p = tmp_path / "authoring.py"
    p.write_text(src, encoding="utf-8")
    offenders = [(fn, ast.unparse(v)) for _ln, v, fn in _reason_sites_in(p) if _bad_form(v) and not _exempt("authoring.py", v, fn)]
    assert offenders == [("a_new_writer_path", "str(exc)")], offenders
    # and the real file has none
    assert [1 for _ln, v, fn in _reason_sites_in(PKG / "authoring.py") if _bad_form(v) and not _exempt("authoring.py", v, fn)] == []


def test_the_reason_codes_docstring_says_tripwire_like_the_rest():
    from factlattice_engine import reason_codes
    d = reason_codes.__doc__
    assert "TRIPWIRE" in d and "RUNTIME battery" in d
    assert "both fail on it" not in d and "static scan (tests/test_reason_sites.py) both" not in d


def test_heal_codes_are_pinned_as_a_set():
    from factlattice_engine.selfheal import HEAL_REASONS, HEAL_REASON_CODES
    assert set(HEAL_REASON_CODES) == set(HEAL_REASONS) and len(HEAL_REASONS) == 14
    assert len(set(HEAL_REASON_CODES.values())) == 14, "two heal reasons share one code (a merge)"
    assert set(HEAL_REASON_CODES.values()) == {
        "self-heal-raised", "already-fresh", "no-rebuild-recipe", "no-readable-roots", "reverify-fresh", "rebuild-cooldown",
        "corpus-changed-during-reindex", "reindex-failed", "reindex-no-readable-roots", "corpus-moved-during-reindex",
        "corpus-changed-while-reindexing", "still-stale-after-rebuild", "rebuild-already-attempted", "reindexed-fresh"}
    # every code is a code, and every outcome the healer can build round-trips through to_dict without falling to the default
    from factlattice_engine.reason_codes import is_code
    from factlattice_engine.selfheal import HealOutcome
    for sentence, code in HEAL_REASON_CODES.items():
        assert is_code(code)
        d = HealOutcome("c", True, False, reason=sentence).to_dict()
        assert d["reason"] == code and d["explanation"] == sentence
