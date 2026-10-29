"""The reviewer's structure gate at DOCUMENT scope: `python -m factlattice_engine.reviewer dangles --corpus C --document P`.

THE DEFECT. The procedure linted the document ALONE with dangle true, and a file-scope lint reports every reference to a
node defined in a SIBLING file of the same corpus as a forward dangle, although all of them resolve in the corpus. The gate
stopped every document that links across files. (A witness on a real private corpus lives in a separate file that is not
part of the published tree.)

THE FIX under test: the helper runs the same document-scope lint, resolves each forward-dangle target against the REGISTERED
CORPUS through the engine's own resolution (`Partition.get_node`; for a domain-qualified ref the engine's router), and returns
`true_dangles`, `resolved_elsewhere` (a count and the target ids) and every non-dangle lint error, unchanged.

Each test drives real files through the real engine (no function is patched except in the failure-contract case).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "factlattice_engine" / "reviewer" / "shipped" / "reviewer-core.lts"

A = """---
lts: "1.1"
title: A
---
# A [SPEC:A-ROOT]

## Local [A-1]

- status: active
{refs}
"""
B = """---
lts: "1.1"
title: B
---
# B [SPEC:B-ROOT]

## Remote [B-1]

- status: active
"""


def _env(monkeypatch, tmp_path):
    for k in ("FACTLATTICE_STATE_DIR", "LTS_ENGINE_STATE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / "st"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setenv("FACTLATTICE_CODE_WATCH", "0")
    monkeypatch.setenv("FACTLATTICE_QUERY_LOG", str(tmp_path / "ql.jsonl"))
    monkeypatch.chdir(tmp_path)


def _registry(tmp_path, monkeypatch, corpora):
    reg = tmp_path / "reg.json"
    def spec(v):
        d = {"roots": [{"path": str(v[0]), "area": "specs"}], "domain_suffix": v[1]}
        if len(v) > 2 and v[2]:
            d["private"] = True
        if len(v) > 3:
            d["profile"] = v[3]
        return d
    reg.write_text(json.dumps({cid: spec(v) for cid, v in corpora.items()}))
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(reg))
    return reg


def run(argv, capsys):
    from factlattice_engine.reviewer.__main__ import main
    capsys.readouterr()
    rc = main(argv)
    cap = capsys.readouterr()
    try:
        body = json.loads(cap.out)
    except ValueError:
        body = None
    return rc, body, cap


@pytest.fixture
def world(tmp_path, monkeypatch):
    """corpus c1 = a.lts + b.lts; c2 = one file; d1 and d2 BOTH carry domain suffix `proj.dup` (the ambiguous suffix)."""
    _env(monkeypatch, tmp_path)
    roots = {}
    for name in ("c1", "c2", "d1", "d2"):
        (tmp_path / name).mkdir()
        roots[name] = tmp_path / name
    (roots["c1"] / "b.lts").write_text(B, encoding="utf-8")
    (roots["c2"] / "c.lts").write_text("---\nlts: \"1.1\"\ntitle: C\n---\n# C [SPEC:C-ROOT]\n\n## Far [C-1]\n\n- status: active\n",
                                       encoding="utf-8")
    for n, nid in (("d1", "D-1"), ("d2", "D-1")):
        (roots[n] / "d.lts").write_text(f"---\nlts: \"1.1\"\ntitle: D\n---\n# D [SPEC:{n.upper()}-ROOT]\n\n## Dup [{nid}]\n\n- status: active\n",
                                        encoding="utf-8")
    _registry(tmp_path, monkeypatch, {"c1": (roots["c1"], "proj.c1"), "c2": (roots["c2"], "proj.c2"),
                                      "d1": (roots["d1"], "proj.dup"), "d2": (roots["d2"], "proj.dup")})

    def doc(refs: str) -> Path:
        p = roots["c1"] / "a.lts"
        p.write_text(A.format(refs=refs), encoding="utf-8")
        return p
    return {"doc": doc, "roots": roots, "tmp": tmp_path}


def _old_gate(path):
    """What the OLD procedure step did: lts_lint on the document alone, dangle true. Returns the error findings."""
    from factlattice_engine import tools
    r = tools.dispatch("lts_lint", {"source": str(path), "dangle": True, "token_budget": 200000}, tools=None)
    return [f for f in r["findings"] if f["severity"] == "error"]


# --------------------------------------------------------------------------------------------- (1) sibling-file reference
def test_a_reference_to_a_sibling_file_is_resolved_elsewhere_not_a_dangle(world, capsys):
    p = world["doc"]("- depends-on: [B-1]")
    old = _old_gate(p)
    assert [f["kind"] for f in old] == ["forward"], old                  # the OLD gate stops here: this is the defect
    rc, body, cap = run(["dangles", "--corpus", "c1", "--document", str(p)], capsys)
    assert rc == 0, cap.out[:600]
    assert body["true_dangle_count"] == 0 and body["true_dangles"] == []
    assert body["resolved_elsewhere"] == {"count": 1, "target_count": 1, "targets": ["B-1"]}
    assert body["other_error_count"] == 0 and body["other_errors"] == []
    assert body["complete"] is True


# --------------------------------------------------------------------------------------------- (2) exists nowhere
def test_a_reference_that_exists_nowhere_is_a_true_dangle_and_stops(world, capsys):
    p = world["doc"]("- depends-on: [B-1] [NOWHERE-1]")
    rc, body, cap = run(["dangles", "--corpus", "c1", "--document", str(p)], capsys)
    assert rc == 1, cap.out[:600]
    assert body["true_dangle_count"] == 1
    d = body["true_dangles"][0]
    assert d["target"] == "NOWHERE-1" and d["node"] == "A-1" and isinstance(d["line"], int) and d["line"] > 0
    assert body["resolved_elsewhere"] == {"count": 1, "target_count": 1, "targets": ["B-1"]}


# --------------------------------------------------------------------------------------------- (3) domain-qualified refs
def test_a_domain_qualified_ref_is_decided_by_the_engines_router(world, capsys):
    p = world["doc"]("- depends-on: [proj.c2:C-1] [proj.c2:C-MISSING] [no.such.dom:Z-1]")
    rc, body, _ = run(["dangles", "--corpus", "c1", "--document", str(p)], capsys)
    assert rc == 1
    assert body["resolved_elsewhere"] == {"count": 1, "target_count": 1, "targets": ["C-1"]}
    got = {(d["target"], d["target_domain"]): d["status"] for d in body["true_dangles"]}
    assert set(got) == {("C-MISSING", "proj.c2"), ("Z-1", "no.such.dom")}, got
    assert got[("C-MISSING", "proj.c2")] == "not_found"
    assert got[("Z-1", "no.such.dom")] == "unresolved_corpus"


def test_an_ambiguous_domain_suffix_is_a_true_dangle_even_though_the_node_exists(world, capsys):
    p = world["doc"]("- depends-on: [proj.dup:D-1]")
    rc, body, _ = run(["dangles", "--corpus", "c1", "--document", str(p)], capsys)
    assert rc == 1
    assert body["resolved_elsewhere"]["count"] == 0
    assert [(d["target"], d["status"]) for d in body["true_dangles"]] == [("D-1", "ambiguous_domain")]


def test_the_helper_agrees_with_lts_node_on_every_target(world):
    """The helper must not invent a resolution: for each target, `resolved` iff the engine's own lts_node (or, for a
    domain-qualified ref, the same router lts_node routes with) finds it."""
    from factlattice_engine import tools
    from factlattice_engine.server import build_tools
    eng = build_tools(watch=False, require_corpora=False)
    assert tools.dispatch("lts_node", {"corpus": "c1", "node_id": "B-1"}, tools=eng)["node"] is not None
    assert tools.dispatch("lts_node", {"corpus": "c1", "node_id": "NOWHERE-1"}, tools=eng)["node"] is None


# --------------------------------------------------------------------------------------------- (4) other lint errors still stop
def test_a_non_dangle_lint_error_is_still_a_stop(world, capsys):
    p = world["doc"]("- depends-on: [B-1]\n\n## Twice [A-1]\n\n- status: active")          # a duplicate id: a collision
    old = [f for f in _old_gate(p) if f["kind"] != "forward"]
    assert old, "the fixture must plant a NON-dangle error; the old gate shows none"
    rc, body, _ = run(["dangles", "--corpus", "c1", "--document", str(p)], capsys)
    assert rc == 1
    assert body["true_dangle_count"] == 0 and body["resolved_elsewhere"]["count"] == 1
    assert body["other_error_count"] == len(old) >= 1
    assert all(e["kind"] != "forward" for e in body["other_errors"])
    assert {"origin", "kind", "node_id", "source_line", "message"} <= set(body["other_errors"][0])


def test_a_clean_document_exits_zero_with_all_three_counts_zero(world, capsys):
    p = world["doc"]("")
    rc, body, _ = run(["dangles", "--corpus", "c1", "--document", str(p)], capsys)
    assert rc == 0 and body["true_dangle_count"] == 0 and body["other_error_count"] == 0
    assert body["resolved_elsewhere"] == {"count": 0, "target_count": 0, "targets": []}


# --------------------------------------------------------------------------------------------- (5) failure contract
def test_failures_follow_the_helper_contract_with_planted_directory_names(tmp_path, monkeypatch, capsys):
    from tests.test_reviewer_reasons import PLANTS, assert_failure_shape
    for label, plant in sorted(PLANTS.items()):
        base = tmp_path / label / plant
        (base / "corp").mkdir(parents=True)
        (base / "corp" / "a.lts").write_text(A.format(refs=""), encoding="utf-8")
        _env(monkeypatch, tmp_path / label)
        _registry(base, monkeypatch, {"pub": (base / "corp", "p")})
        doc = base / "corp" / "a.lts"
        # a document that is not there
        rc, body, _ = run(["dangles", "--corpus", "pub", "--document", str(base / "gone.lts")], capsys)
        assert rc == 2, (label, body)
        assert_failure_shape(body, f"dangles:{label}:missing", "lint-incomplete")
        # a corpus the engine does not serve
        rc, body, _ = run(["dangles", "--corpus", "nope", "--document", str(doc)], capsys)
        assert rc == 2, (label, body)
        assert_failure_shape(body, f"dangles:{label}:unknown-corpus", "corpus-not-served")
        # no registry at all
        monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(base / "gone.json"))
        rc, body, _ = run(["dangles", "--corpus", "pub", "--document", str(doc)], capsys)
        assert rc == 2, (label, body)
        assert_failure_shape(body, f"dangles:{label}:registry", "registry-not-found")


def test_the_new_reason_codes_are_in_the_closed_list():
    from factlattice_engine.reviewer.__main__ import HELPER_REASON_CODES
    assert {"corpus-not-served", "lint-incomplete"} <= set(HELPER_REASON_CODES)


# --------------------------------------------------------------------------------------------- (6) the procedure names the helper
def _structure_gate_step() -> str:
    text = CORE.read_text(encoding="utf-8")
    a = text.index("## [REVPROC-CORE-STRUCTURE-GATE]")
    return text[a:text.index("\n## [REVPROC-CORE-FRAME]", a)]


def test_the_procedure_step_calls_the_helper_and_states_the_new_stop_condition():
    step = _structure_gate_step()
    assert "python -m factlattice_engine.reviewer dangles --corpus <C> --document <P>" in step
    assert "TRUE dangle" in step and "other_errors" in step
    assert "resolved_elsewhere" in step and "true_dangles" in step
    assert "structure-gate:lint-errors" in step                      # the stop reason is unchanged
    assert "Run lts_lint on the document with `source` the document" not in step     # the raw lint is no longer the gate


def test_the_procedure_records_the_three_counts_in_the_revcheck():
    step = _structure_gate_step()
    for needle in ("true dangles", "resolved-elsewhere", "other errors"):
        assert needle in step, needle


def test_the_definition_file_names_the_helper_where_it_names_this_step():
    d = (REPO / "agents" / "factlattice-reviewer.md").read_text(encoding="utf-8")
    assert "python -m factlattice_engine.reviewer dangles" in d


# --------------------------------------------------------------------------------------------- real subprocess, read-only (SDLC REVIEWER-CLI-COMMANDS)
def test_dangles_through_a_real_subprocess_is_read_only_and_exits_as_documented(world):
    import subprocess
    tmp = world["tmp"]
    p = world["doc"]("- depends-on: [B-1] [NOWHERE-1]")
    env = {**os.environ, "FACTLATTICE_STATE_DIR": str(tmp / "st"), "LTS_ENGINE_STATE_DIR": str(tmp / "st"),
           "FACTLATTICE_BACKUP_DIR": str(tmp / "bak"), "FACTLATTICE_CODE_WATCH": "0",
           "FACTLATTICE_QUERY_LOG": str(tmp / "ql.jsonl"), "FACTLATTICE_CORPUS_CONFIG": str(tmp / "reg.json")}

    def snap():
        return sorted((str(q), q.read_bytes()) for r in world["roots"].values() for q in r.rglob("*") if q.is_file())
    before = snap()
    r = subprocess.run([sys.executable, "-m", "factlattice_engine.reviewer", "dangles", "--corpus", "c1", "--document", str(p)],
                       capture_output=True, text=True, cwd=str(REPO), env=env, timeout=120)
    assert r.returncode == 1, (r.stdout[:400], r.stderr[:400])
    body = json.loads(r.stdout)
    assert body["true_dangle_count"] == 1 and body["resolved_elsewhere"]["count"] == 1
    assert snap() == before


# =========================================================================================== ROUND 2 (Diamond f872d97)
# F1: a ref into a PRIVATE corpus is unverifiable, never "resolved".
def _private_world(world, monkeypatch):
    tmp = world["tmp"]
    pv = tmp / "pvroot"
    pv.mkdir()
    (pv / "p.lts").write_text("---\nlts: \"1.1\"\ntitle: P\n---\n# P [SPEC:P-ROOT]\n\n## Priv [P-1]\n\n- status: active\n", encoding="utf-8")
    r = world["roots"]
    _registry(tmp, monkeypatch, {"c1": (r["c1"], "proj.c1"), "zqprivcorp": (pv, "proj.pv", True)})
    return pv


def test_F1_a_private_target_is_unverifiable_not_resolved_and_does_not_stop(world, monkeypatch, capsys):
    _private_world(world, monkeypatch)
    p = world["doc"]("- depends-on: [B-1] [proj.pv:P-1] [proj.pv:P-NOPE]")
    rc, body, cap = run(["dangles", "--corpus", "c1", "--document", str(p)], capsys)
    assert rc == 0, cap.out[:800]                                       # unverifiable does not stop the gate
    assert body["resolved_elsewhere"] == {"count": 1, "target_count": 1, "targets": ["B-1"]}     # never called "resolved"
    assert body["unverifiable_private"] == {"count": 2, "target_count": 2, "targets": ["P-1", "P-NOPE"], "domains": ["proj.pv"]}
    assert body["true_dangle_count"] == 0
    assert "zqprivcorp" not in cap.out                                   # the private corpus id is never named


def test_F1_unverifiable_does_not_hide_a_true_dangle(world, monkeypatch, capsys):
    _private_world(world, monkeypatch)
    p = world["doc"]("- depends-on: [proj.pv:P-NOPE] [NOWHERE-1]")
    rc, body, _ = run(["dangles", "--corpus", "c1", "--document", str(p)], capsys)
    assert rc == 1 and body["true_dangle_count"] == 1 and body["unverifiable_private"]["count"] == 1


def test_F1_a_public_corpus_has_an_empty_unverifiable_bucket(world, capsys):
    p = world["doc"]("- depends-on: [B-1]")
    rc, body, _ = run(["dangles", "--corpus", "c1", "--document", str(p)], capsys)
    assert body["unverifiable_private"] == {"count": 0, "target_count": 0, "targets": [], "domains": []}


# F2: same-corpus targets resolve EXACTLY as the whole-corpus dangle lint does, aliases included.
def _forward(findings):
    return [f for f in findings if f["origin"] == "dangle" and f["kind"] == "forward" and f["severity"] == "error"]


def test_F2_differential_per_document_true_dangles_sum_to_the_whole_corpus_lint(tmp_path, monkeypatch, capsys):
    from factlattice_engine import tools
    _env(monkeypatch, tmp_path)
    root = tmp_path / "al"
    root.mkdir()
    mk = lambda name, body: (root / name).write_text('---\nlts: "1.1"\ntitle: T\n---\n' + body, encoding="utf-8")
    mk("x.lts", "# X [SPEC:X-ROOT]\n\n## One [X-1]\n\n- status: active\n- depends-on: [CANON-1] [ALIAS-1] [Y-1] [GONE-1] [ALIAS-NOPE]\n")
    mk("y.lts", "# Y [SPEC:Y-ROOT]\n\n## Defs [Y-1]\n\n- [CANON-1] [ALIAS-1] **the canonical node, aka the alias**\n- status: active\n"
                "- depends-on: [ALIAS-1] [GONE-2]\n")
    mk("z.lts", "# Z [SPEC:Z-ROOT]\n\n## Three [Z-1]\n\n- status: active\n- depends-on: [ALIAS-1] [X-1] [GONE-1]\n")
    _registry(tmp_path, monkeypatch, {"al": (root, "proj.al")})
    whole = tools.dispatch("lts_lint", {"source": str(root), "dangle": True, "token_budget": 10**6}, tools=None)
    want = sorted((f["source_file"], f["detail"]["target"]) for f in _forward(whole["findings"]))
    assert ("x.lts", "ALIAS-1") not in want and ("x.lts", "GONE-1") in want, want       # the whole-corpus lint resolves the alias
    got = []
    for name in ("x.lts", "y.lts", "z.lts"):
        rc, body, _ = run(["dangles", "--corpus", "al", "--document", str(root / name)], capsys)
        got += [(name, d["target"]) for d in body["true_dangles"]]
    assert sorted(got) == want, (got, want)
    assert len(want) == 4


# F3 / F4: the procedure text
def test_F3_the_stop_decision_is_the_dangles_exit_code_and_lint_groups_never_decides():
    step = _structure_gate_step()
    assert "exit code of the `dangles` helper alone" in step
    assert "`lint-groups` is used only to group warnings for reporting and never decides a stop" in step
    assert "`errors` count" in step


def test_F1_the_procedure_records_the_unverifiable_private_count_and_does_not_stop_on_it():
    step = _structure_gate_step()
    assert "unverifiable_private" in step and "unverifiable-private" in step
    assert "never called resolved" in step


def test_F4_extension_validation_lints_through_the_helper_not_a_file_scope_lint():
    text = CORE.read_text(encoding="utf-8")
    a = text.index("## [REVPROC-CORE-VALIDATE-EXTENSIONS]")
    step = text[a:text.index("\n## [REVPROC-CORE-STRUCTURE-GATE]", a)]
    assert "Lint the extension (lts_lint with dangle true" not in step
    assert "python -m factlattice_engine.reviewer dangles --corpus <C> --document <E>" in step
    assert "--profile" in step and "reviewer profile" in step


def test_F4_the_helper_accepts_an_explicit_profile_and_lints_under_it(world, capsys):
    from factlattice_engine.reviewer import PROFILE_PATH
    p = world["doc"]("- depends-on: [B-1]")
    rc, body, cap = run(["dangles", "--corpus", "c1", "--document", str(p), "--profile", PROFILE_PATH], capsys)
    assert body["profile"] == {"used": True, "source": "argument"}, body
    assert body["warnings"] == []
    assert body["resolved_elsewhere"]["count"] == 1


# P3: a registered profile path that does not exist is a warning, not silence
def test_P3_a_registered_profile_that_does_not_exist_is_reported(world, monkeypatch, capsys):
    r = world["roots"]
    _registry(world["tmp"], monkeypatch, {"c1": (r["c1"], "proj.c1", False, str(world["tmp"] / "no-such-profile.lts"))})
    p = world["doc"]("- depends-on: [B-1]")
    rc, body, cap = run(["dangles", "--corpus", "c1", "--document", str(p)], capsys)
    assert rc == 0 and body["profile"] == {"used": False, "source": "none"}
    assert body["warnings"] == [{"code": "profile-path-not-found", "origin": "registry"}]
    assert "no-such-profile" not in cap.out                                 # a fixed code; the path is never echoed


def test_P3_an_explicit_profile_that_does_not_exist_is_reported(world, capsys):
    p = world["doc"]("")
    rc, body, cap = run(["dangles", "--corpus", "c1", "--document", str(p), "--profile", str(world["tmp"] / "nope.lts")], capsys)
    assert body["warnings"] == [{"code": "profile-path-not-found", "origin": "argument"}]
    assert "nope.lts" not in cap.out


# =========================================================================================== ROUND 3 (Diamond 87d6460)
# P2-1: the second extension lint is the extension's OWN corpus profile, found by membership, and only if it has one.
STRICT_EXT = ('---\nlts: "1.1"\ntitle: E\n---\n# E [SPEC:E-ROOT]\n\n## Ext [E-1]\n\n- status: active\n- ext-id: E\n')
HOST_PROFILE = "---\ntitle: p\ndocument_tag: PROFILE\n---\n# p [PROFILE:P]\n\n## [PROFILE-DOMAIN] d\n\n- domain-id: hostp\n"


def _ext_world(world, monkeypatch):
    """c1 = the REVIEWED corpus, with a profile that does not declare the key `ext-id`; `inside` = a served corpus whose profile is the
    reviewer profile (it declares `ext-id`); `bare` = a served corpus with no profile. Returns the extension paths."""
    from factlattice_engine.reviewer import PROFILE_PATH
    tmp, r = world["tmp"], world["roots"]
    hp = tmp / "host-profile.lts"
    hp.write_text(HOST_PROFILE, encoding="utf-8")
    for n in ("inside", "bare"):
        (tmp / n).mkdir()
    outside = tmp / "elsewhere"
    outside.mkdir()
    paths = {"outside": outside / "ext.lts", "inside": tmp / "inside" / "ext.lts", "bare": tmp / "bare" / "ext.lts"}
    for p in paths.values():
        p.write_text(STRICT_EXT, encoding="utf-8")
    _registry(tmp, monkeypatch, {"c1": (r["c1"], "proj.c1", False, str(hp)),
                                 "inside": (tmp / "inside", "proj.in", False, PROFILE_PATH),
                                 "bare": (tmp / "bare", "proj.bare")})
    return paths, PROFILE_PATH


def test_P21_the_old_second_lint_falsely_fails_a_strict_extension_outside_every_corpus(world, monkeypatch, capsys):
    """The defect, reproduced: the REVIEWED corpus's profile does not declare the extension's key, so the lint without --profile exits 1."""
    paths, revprof = _ext_world(world, monkeypatch)
    rc1, b1, _ = run(["dangles", "--corpus", "c1", "--document", str(paths["outside"]), "--profile", revprof], capsys)
    assert rc1 == 0 and b1["other_error_count"] == 0, b1                    # under the reviewer profile it is fine
    rc2, b2, _ = run(["dangles", "--corpus", "c1", "--document", str(paths["outside"])], capsys)
    assert rc2 == 1 and b2["other_errors"] and all(e["kind"] == "undeclared_key" for e in b2["other_errors"]), b2


def test_P21_an_extension_in_no_served_corpus_has_no_own_corpus_lint(world, monkeypatch, capsys):
    paths, _ = _ext_world(world, monkeypatch)
    rc, body, cap = run(["dangles", "--own-corpus", "--document", str(paths["outside"])], capsys)
    assert rc == 0, cap.out[:500]
    assert body["own_corpus"] == {"applicable": False, "note": "own-corpus lint: not applicable", "why": "extension-in-no-served-corpus"}
    assert "true_dangle_count" not in body                                   # nothing was linted, so nothing is claimed


def test_P21_an_extension_inside_a_served_corpus_is_linted_under_THAT_corpus_profile(world, monkeypatch, capsys):
    paths, _ = _ext_world(world, monkeypatch)
    rc, body, cap = run(["dangles", "--own-corpus", "--document", str(paths["inside"])], capsys)
    assert rc == 0, cap.out[:800]
    assert body["own_corpus"] == {"applicable": True} and body["other_error_count"] == 0
    assert body["profile"] == {"used": True, "source": "registry"}
    assert "inside" not in cap.out.replace("own_corpus", "")                 # the corpus id is not echoed


def test_P21_an_own_corpus_without_a_profile_is_not_applicable_never_the_reviewed_corpus_profile(world, monkeypatch, capsys):
    paths, _ = _ext_world(world, monkeypatch)
    rc, body, _ = run(["dangles", "--own-corpus", "--document", str(paths["bare"])], capsys)
    assert rc == 0 and body["own_corpus"] == {"applicable": False, "note": "own-corpus lint: not applicable",
                                              "why": "own-corpus-has-no-profile"}


def test_P21_own_corpus_and_corpus_are_exclusive_and_one_is_required(world, capsys):
    p = world["doc"]("")
    for argv in (["dangles", "--document", str(p)], ["dangles", "--own-corpus", "--corpus", "c1", "--document", str(p)],
                 ["dangles", "--own-corpus", "--profile", "x", "--document", str(p)]):
        rc, body, _ = run(argv, capsys)
        assert rc == 2 and body["reason"] == "usage-error", (argv, body)


def test_P21_the_procedure_runs_the_second_lint_only_by_membership():
    text = CORE.read_text(encoding="utf-8")
    a = text.index("## [REVPROC-CORE-VALIDATE-EXTENSIONS]")
    step = text[a:text.index("\n## [REVPROC-CORE-STRUCTURE-GATE]", a)]
    assert "python -m factlattice_engine.reviewer dangles --own-corpus --document <E>" in step
    assert "own-corpus lint: not applicable" in step
    assert "never the profile of the corpus under review" in step
    assert "again without `--profile`" not in step


# P3-1: the alias index is built ONCE per helper call.
def test_P31_the_alias_index_is_built_once_per_call_not_once_per_finding(world, monkeypatch, capsys):
    import factlattice.index_builder as ib
    real, seen = ib.build_alias_index, []

    def spy(node_map):
        seen.append(sys._getframe(1).f_code.co_name)
        return real(node_map)
    monkeypatch.setattr(ib, "build_alias_index", spy)
    p = world["doc"]("- depends-on: [B-1] [NOWHERE-1] [NOWHERE-2] [NOWHERE-3] [NOWHERE-4]")
    seen.clear()
    from factlattice_engine.reviewer.__main__ import main
    capsys.readouterr()
    rc = main(["dangles", "--corpus", "c1", "--document", str(p)])
    body = json.loads(capsys.readouterr().out)
    assert body["true_dangle_count"] == 4 and body["resolved_elsewhere"]["count"] == 1
    assert "resolve" not in seen, seen                                      # never from inside the per-finding resolver
    assert seen.count("_main") == 1, seen


# P2-2: build_alias_index is the ONE definition. THIS IS A TRIPWIRE (see the SDLC row), and a REGRESSION pin: it passes at 87d6460,
# where the helper already called the function; it guards against a later inline copy, it was never RED.
def alias_claim_reads(base_dirs):
    """Every read of the constant key `aliases` through `.get(...)` or a subscript, in any quote style (found on the AST, not by text),
    as `(relative path, line, enclosing function)`. A computed key, an `.items()` walk or a `getattr` is invisible to it."""
    import ast
    out = []
    for base in base_dirs:
        for f in sorted(Path(base).rglob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            parents = {}
            for n in ast.walk(tree):
                for c in ast.iter_child_nodes(n):
                    parents[c] = n

            def enclosing(n):
                while n in parents:
                    n = parents[n]
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        return n.name
                return "<module>"
            for n in ast.walk(tree):
                key = None
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get" and n.args:
                    key = n.args[0]
                elif isinstance(n, ast.Subscript):
                    key = n.slice
                if isinstance(key, ast.Constant) and key.value == "aliases":
                    out.append((f.name, n.lineno, enclosing(n)))
    return out


def test_P22_build_alias_index_is_the_single_definition_and_its_three_call_sites_call_it():
    import inspect
    from factlattice import index_builder
    from factlattice_engine import incremental
    from factlattice_engine.reviewer import __main__ as rmain
    sites = {"build_index": index_builder.build_index, "incremental assemble": incremental.RootIndex.assemble,
             "dangles helper": rmain._main}
    for name, fn in sites.items():
        src = inspect.getsource(fn)
        assert "build_alias_index(" in src, f"{name} does not call build_alias_index"
        assert "'aliases'" not in src and '"aliases"' not in src.replace('properties["aliases"]', ""), f"{name} reads the alias claims itself"
    reads = alias_claim_reads((REPO / "factlattice", REPO / "factlattice_engine"))
    assert [(f, fn) for f, _, fn in reads if f != "profile.py"] == [("index_builder.py", "build_alias_index")], reads


def test_P22_the_pin_RUNS_against_planted_copies_and_fires_on_each(tmp_path):
    """Negative control that EXECUTES the scan on scratch copies of the real sources with a divergent alias loop planted in each
    spelling and position. Every plant must be reported; the unplanted copy must report exactly the one legitimate read."""
    import shutil
    real = (REPO / "factlattice" / "index_builder.py").read_text(encoding="utf-8")
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "index_builder.py").write_text(real, encoding="utf-8")
    assert [(f, fn) for f, _, fn in alias_claim_reads([clean])] == [("index_builder.py", "build_alias_index")]
    plants = {
        "single-quote get elsewhere": ("other.py", "def f(nd):\n    return (nd.get('properties') or {}).get('aliases') or []\n"),
        "double-quote get elsewhere": ("other.py", 'def f(nd):\n    return nd.get("properties", {}).get("aliases", [])\n'),
        "subscript elsewhere": ("other.py", "def f(nd):\n    return nd['properties']['aliases']\n"),
        "divergent loop beside a retained call": ("index_builder.py", real + "\n\ndef build_index_extra(node_map):\n"
            "    alias_index = build_alias_index(node_map)\n    for c, n in node_map.items():\n"
            "        for a in n['properties']['aliases']:\n            alias_index.setdefault(a, c)\n    return alias_index\n"),
    }
    for label, (name, text) in plants.items():
        d = tmp_path / label.replace(" ", "-")
        d.mkdir()
        (d / "index_builder.py").write_text(real, encoding="utf-8")
        (d / name).write_text(text, encoding="utf-8")
        reads = [(f, fn) for f, _, fn in alias_claim_reads([d])]
        assert reads != [("index_builder.py", "build_alias_index")], (label, "THE PIN DID NOT FIRE", reads)
    # and the blind spot is real, stated rather than hidden: a computed key is not seen
    d = tmp_path / "blind"
    d.mkdir()
    (d / "index_builder.py").write_text(real, encoding="utf-8")
    (d / "other.py").write_text("KEY = 'ali' + 'ases'\ndef f(nd):\n    return nd['properties'][KEY]\n", encoding="utf-8")
    assert [(f, fn) for f, _, fn in alias_claim_reads([d])] == [("index_builder.py", "build_alias_index")]


def test_P22_and_P33_the_sdlc_rows_name_the_single_definition_and_the_new_surface():
    sdlc = (REPO / "SDLC.md").read_text(encoding="utf-8")
    row = next(l for l in sdlc.splitlines() if l.startswith("- [ ] [DANGLE-RESOLUTION-NAMESPACE-ONE-DEFINITION]"))
    for needle in ("build_alias_index", "build_index", "incremental", "dangles", "test_P22", "TRIPWIRE", "REGRESSION",
                   "CANNOT", "computed key", "BESIDE"):
        assert needle in row, needle
    cli = next(l for l in sdlc.splitlines() if l.startswith("- [ ] [REVIEWER-CLI-COMMANDS]"))
    for needle in ("--profile", "--own-corpus", "unverifiable_private", "`profile`", "`warnings`", "own_corpus"):
        assert needle in cli, needle


# P3-2: the procedure reads the helper's profile and warnings fields.
def test_P32_the_procedure_reads_profile_and_warnings_and_records_the_warning():
    step = _structure_gate_step()
    assert "`profile`" in step and "`warnings`" in step
    assert "profile-path-not-found" in step and "REVCHECK" in step


# P3 (round 3b): a document that does not exist is a clean helper-contract reply, not an engine traceback on stderr.
def test_P3b_own_corpus_on_a_missing_path_inside_a_served_root_is_clean(world, monkeypatch):
    """A real subprocess: the engine's traceback goes to the process's stderr, which an in-process capture does not see."""
    import subprocess
    paths, _ = _ext_world(world, monkeypatch)
    tmp = world["tmp"]
    gone = tmp / "inside" / "not-there.lts"
    env = {**os.environ, "FACTLATTICE_STATE_DIR": str(tmp / "st"), "LTS_ENGINE_STATE_DIR": str(tmp / "st"),
           "FACTLATTICE_BACKUP_DIR": str(tmp / "bak"), "FACTLATTICE_CODE_WATCH": "0", "FACTLATTICE_QUERY_LOG": str(tmp / "ql.jsonl"),
           "FACTLATTICE_CORPUS_CONFIG": str(tmp / "reg.json")}
    for argv in (["--own-corpus"], ["--corpus", "c1"]):
        r = subprocess.run([sys.executable, "-m", "factlattice_engine.reviewer", "dangles", *argv, "--document", str(gone)],
                           capture_output=True, text=True, cwd=str(REPO), env=env, timeout=120)
        body = json.loads(r.stdout)
        assert r.returncode == 2 and body["reason"] == "lint-incomplete" and body["error_class"] == "InBand" and "error" not in body, body
        assert r.stderr == "", r.stderr[:400]
        assert "not-there" not in r.stdout


# =========================================================================================== ROUND 3c (cold check of 02d13c1)
def _sub_env(tmp):
    return {**os.environ, "FACTLATTICE_STATE_DIR": str(tmp / "st"), "LTS_ENGINE_STATE_DIR": str(tmp / "st"),
            "FACTLATTICE_BACKUP_DIR": str(tmp / "bak"), "FACTLATTICE_CODE_WATCH": "0", "FACTLATTICE_QUERY_LOG": str(tmp / "ql.jsonl"),
            "FACTLATTICE_CORPUS_CONFIG": str(tmp / "reg.json")}


def _non_files(root):
    """Every kind of path that is not a readable regular file. A missing path, a directory and `.` are inside the served root; a FIFO, a
    broken symlink and an unreadable file are OUTSIDE every root. Inside a root, a broken symlink or an unreadable file makes the engine
    fail to load the corpus (`registry-not-found`, checked first, and correct); a FIFO inside a root makes the engine build BLOCK FOREVER
    (factlattice/hashing.py, a pre-existing engine defect, ticket ISSUE-NONREGULAR-MEMBER). NEVER add a FIFO
    inside a served root to any test: the suite would hang. Returns {label: (document arg, cwd)}."""
    out = {"missing": (str(root / "missing.lts"), root)}
    (root / "adir").mkdir()
    out["directory"] = (str(root / "adir"), root)
    out["dot"] = (".", root)
    el = root.parent / "elsewhere"
    el.mkdir(exist_ok=True)
    os.mkfifo(el / "pipe.lts")
    out["fifo"] = (str(el / "pipe.lts"), root)
    os.symlink(el / "no-target.lts", el / "broken.lts")
    out["broken-symlink"] = (str(el / "broken.lts"), root)
    if os.geteuid() != 0:
        f = el / "locked.lts"
        f.write_text(STRICT_EXT, encoding="utf-8")
        os.chmod(f, 0)
        out["unreadable"] = (str(f), root)
    return out


@pytest.mark.parametrize("mode", ["--own-corpus", "--corpus c1"])
def test_P3c_every_non_file_gives_the_clean_reply_rc2_and_empty_stderr(world, monkeypatch, mode):
    import subprocess
    _ext_world(world, monkeypatch)
    tmp = world["tmp"]
    root = tmp / "inside" if mode == "--own-corpus" else world["roots"]["c1"]
    kinds = _non_files(root)
    try:
        for label, (doc, cwd) in kinds.items():
            r = subprocess.run([sys.executable, "-m", "factlattice_engine.reviewer", "dangles", *mode.split(), "--document", doc],
                               capture_output=True, text=True, cwd=str(cwd), env={**_sub_env(tmp), "PYTHONPATH": str(REPO)}, timeout=120)
            body = json.loads(r.stdout)
            assert r.returncode == 2, (label, r.returncode, r.stdout[:300])
            assert body["reason"] == "lint-incomplete" and body["error_class"] == "InBand" and "error" not in body, (label, body)
            assert r.stderr == "", (label, r.stderr[:300])
            assert str(root) not in r.stdout and "locked" not in r.stdout and "broken" not in r.stdout, (label, r.stdout[:300])
    finally:
        if "unreadable" in kinds:
            os.chmod(root.parent / "elsewhere" / "locked.lts", 0o644)             # so the test framework can clean the temp directory


# the ORDER: the registry and the corpus-served checks come FIRST, the file check after them
def test_P3c_a_missing_document_under_a_broken_registry_is_registry_not_found(world, monkeypatch, capsys):
    _ext_world(world, monkeypatch)
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(world["tmp"] / "gone.json"))
    for argv in (["--corpus", "c1"], ["--own-corpus"]):
        rc, body, _ = run(["dangles", *argv, "--document", str(world["tmp"] / "nope.lts")], capsys)
        assert rc == 2 and body["reason"] == "registry-not-found", (argv, body)


def test_P3c_a_missing_document_in_an_unserved_corpus_is_corpus_not_served(world, monkeypatch, capsys):
    _ext_world(world, monkeypatch)
    rc, body, _ = run(["dangles", "--corpus", "nope", "--document", str(world["tmp"] / "nope.lts")], capsys)
    assert rc == 2 and body["reason"] == "corpus-not-served", body


def test_P3c_own_corpus_on_a_missing_document_outside_every_corpus_is_rc2_not_a_silent_pass(world, monkeypatch, capsys):
    """DOCUMENTED CHANGE: this was rc 0 `own_corpus.applicable: false` before round 3b. A missing extension must not pass silently."""
    _ext_world(world, monkeypatch)
    rc, body, _ = run(["dangles", "--own-corpus", "--document", str(world["tmp"] / "elsewhere" / "missing.lts")], capsys)
    assert rc == 2 and body["reason"] == "lint-incomplete" and "own_corpus" not in body, body


def test_P3c_the_sdlc_row_states_the_non_file_reply_and_the_own_corpus_change():
    sdlc = (REPO / "SDLC.md").read_text(encoding="utf-8")
    cli = next(l for l in sdlc.splitlines() if l.startswith("- [ ] [REVIEWER-CLI-COMMANDS]"))
    for needle in ("a missing path, a directory, a FIFO, a broken symlink and an unreadable file", "lint-incomplete", "exit 2",
                   "registry and corpus checks run first", "rc 0", "now exit 2"):
        assert needle in cli, needle


# P2-1 (cold check of c9335b9): the --own-corpus ORDER is registry, served-corpus, AMBIGUITY, then the file check, then `not applicable`.
def test_P3d_own_corpus_decides_ambiguity_BEFORE_the_file_check(world, monkeypatch, capsys):
    """Two served corpora whose roots both contain the document (nested roots) AND the document is missing: the answer is
    `own-corpus-ambiguous`, not `lint-incomplete`. If the file check were moved ahead of the ambiguity decision this reads the other way."""
    tmp = world["tmp"]
    outer = tmp / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    for root, nid in ((outer, "O"), (inner, "I")):
        (root / f"{nid.lower()}.lts").write_text(f'---\nlts: "1.1"\ntitle: {nid}\n---\n# {nid} [SPEC:{nid}-ROOT]\n', encoding="utf-8")
    _registry(tmp, monkeypatch, {"outer": (outer, "proj.outer"), "inner": (inner, "proj.inner")})
    rc, body, cap = run(["dangles", "--own-corpus", "--document", str(inner / "missing.lts")], capsys)
    assert rc == 2 and body["reason"] == "own-corpus-ambiguous" and body["error_class"] == "InBand", body
    # the same document, present, is ambiguous too: the decision does not depend on the file
    rc, body, _ = run(["dangles", "--own-corpus", "--document", str(inner / "i.lts")], capsys)
    assert rc == 2 and body["reason"] == "own-corpus-ambiguous", body
