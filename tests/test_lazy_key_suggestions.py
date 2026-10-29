"""[PERF-LAZY-KEY-SUGGEST] Parsing must not pay for did-you-mean suggestions nobody reads.

The architect measured 2026-10-02 (live 181bbf4, web-job config): build_store() 8.2 s, of which
5.2 s was parser._check_key_vocabulary -> suggest_keys -> _levenshtein, for findings that only
lint/receipts read. Contract: an `undeclared_key` finding's `message` and `detail` are computed
on FIRST ACCESS, and are then byte-identical to the eager values; the finding still IS a
ParseFinding and compares/prints as one.
"""
from __future__ import annotations

import factlattice
from factlattice import core_keys
from factlattice.parser import ParseFinding


def _corpus(tmp_path, keys=("see-alos", "questoin", "zz-custom")):
    lines = ["# Doc [DOC-1]", ""]
    for i in range(5):
        lines += [f"## Node {i} [N-{i}]", ""] + [f"- {k}: v{i}" for k in keys] + [""]
    (tmp_path / "c.lts").write_text("\n".join(lines), encoding="utf-8")
    return tmp_path


def _count(monkeypatch):
    n = {"calls": 0}
    real = core_keys._suggest_keys_uncached

    def counting(*a, **k):
        n["calls"] += 1
        return real(*a, **k)
    monkeypatch.setattr(core_keys, "_suggest_keys_uncached", counting)
    core_keys.suggest_keys.cache_clear()
    return n


def _vocab(corpus):
    return [f for f in corpus.vocabulary_findings if f.kind == "undeclared_key"]


def test_parse_computes_no_suggestions(tmp_path, monkeypatch):
    n = _count(monkeypatch)
    corpus = factlattice.parse(str(_corpus(tmp_path)))
    assert len(_vocab(corpus)) == 15
    assert n["calls"] == 0, n


def test_first_access_computes_and_matches_the_eager_text(tmp_path, monkeypatch):
    n = _count(monkeypatch)
    f = [x for x in _vocab(factlattice.parse(str(_corpus(tmp_path)))) if x.key == "see-alos"][0]
    assert isinstance(f, ParseFinding)
    assert f.message == ("property key `see-alos` is neither a core key, nor declared by the "
                         "domain profile, nor marked private (`x-`) — did you mean `see-also`?")
    assert f.detail == {"suggestions": ["see-also"], "severity": "warning", "mode": "legacy"}
    assert n["calls"] == 1
    z = [x for x in _vocab(factlattice.parse(str(tmp_path))) if x.key == "zz-custom"][0]
    assert z.message.endswith("declare it in the domain profile, mark it private as `x-zz-custom`, "
                              "or rewrite it") and z.detail["suggestions"] == []


def test_equality_repr_and_to_dict_behave_as_a_plain_parse_finding(tmp_path):
    f = _vocab(factlattice.parse(str(_corpus(tmp_path))))[0]
    plain = ParseFinding(**f.to_dict())
    assert f == plain and plain == f
    assert repr(f) == repr(plain) and repr(f).startswith("ParseFinding(")
    assert f.to_dict() == plain.to_dict()


def test_concurrent_first_access_is_safe_and_consistent(tmp_path):
    """The engine serves from threads: two first accesses racing must both get the value,
    never AttributeError/KeyError, and the finding must end up fully materialised."""
    import sys
    import threading
    corpus = factlattice.parse(str(_corpus(tmp_path)))
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)                      # force interleaving inside __getattr__
    try:
        for _ in range(30):
            fs = _vocab(factlattice.parse(str(tmp_path)))
            errors, barrier = [], threading.Barrier(8)

            def read(f=fs[0]):
                try:
                    barrier.wait()
                    assert f.message and f.detail["severity"]
                except Exception as ex:          # noqa: BLE001 - the test reports any failure
                    errors.append(repr(ex))
            ts = [threading.Thread(target=read) for _ in range(8)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            assert not errors, errors
            assert "_pending" not in vars(fs[0]) and fs[0] == ParseFinding(**fs[0].to_dict())
    finally:
        sys.setswitchinterval(old)
    assert corpus


def test_dataclasses_replace_and_presets_behave_as_on_a_plain_finding(tmp_path):
    """review audit-PERF-LAZY-KEY-SUGGEST P2-1 / P3-1."""
    import dataclasses
    f = _vocab(factlattice.parse(str(_corpus(tmp_path))))[0]
    g = dataclasses.replace(f, node_id="N-X")
    assert g.node_id == "N-X" and g.message == f.message and g.detail == f.detail
    h = _vocab(factlattice.parse(str(tmp_path)))[0]
    h.message = "preset"
    assert h.detail["suggestions"] is not None and h.message == "preset"


def test_detail_identity_is_stable_under_concurrent_first_access(tmp_path):
    """review P3-2: every racing reader must get the SAME detail dict."""
    import sys
    import threading
    _corpus(tmp_path)
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(30):
            f = _vocab(factlattice.parse(str(tmp_path)))[0]
            seen, barrier = [], threading.Barrier(8)

            def read():
                barrier.wait()
                seen.append(id(f.detail))
            ts = [threading.Thread(target=read) for _ in range(8)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            assert len(set(seen)) == 1 and seen[0] == id(f.detail)
    finally:
        sys.setswitchinterval(old)
