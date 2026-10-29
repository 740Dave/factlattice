"""The shipped reviewer corpus (Spec A, slice 2 phase 2): it lints clean under its OWN profile, its edges are real,
and each generic DETERMINISTIC rule fires on its planted violation and stays silent on its clean twin, through
`lts_find(path=)` (the engine door the reviewer itself uses, validation step 4).

THE HARNESS HAS A NEGATIVE CONTROL. `_validate_rule` is the check the reviewer performs on every deterministic rule
(plant must report a violation per `expect`, twin must not). A harness that can only say "valid" would pass every
rule; `test_the_harness_rejects_*` feed it a rule that matches everything and one that matches nothing.
"""
from __future__ import annotations

import json
import os

import pytest

from factlattice.parser import parse_file
from factlattice_engine import reviewer
from factlattice_engine.tools import dispatch

CORPUS = reviewer.CORPUS_DIR
RULES_FILE = os.path.join(CORPUS, "reviewer-rules.lts")


def _nodes(fname):
    return {n.id: n for n in parse_file(os.path.join(CORPUS, fname)).nodes}


def _rules():
    return {i: n for i, n in _nodes("reviewer-rules.lts").items() if i.startswith("REVRULE-")}


# ----------------------------------------------------------------------------------------- lints clean
def test_the_corpus_lints_clean_under_its_own_profile():
    r = dispatch("lts_lint", {"source": CORPUS, "dangle": True}, tools=None)
    assert r["profile"]["available"] is True and r["profile"]["domain_id"] == "factlattice-reviewer"
    assert r["profile"]["path"] == reviewer.PROFILE_PATH, "the profile in force is the shipped one"
    s = r["summary"]
    assert s.get("errors", 0) == 0, [f for f in r["findings"] if f["severity"] == "error"]
    # the ONLY warnings are orphans (a leaf-heavy procedure document is mostly leaves); every profile rule is silent
    other = [f for f in r["findings"] if f["kind"] != "orphan"]
    assert other == [], other
    assert not any((r.get("profile_validators") or {}).values())
    assert r["key_loss"]["lost_bullets"] == 0 and r["key_loss"]["flagged"] == 0
    assert r["unminted"]["unminted_reference_lists"] == 0
    assert r["profile_documents"] == ["reviewer-profile.lts"], "the profile is excluded from content, not unparsed"


def test_lint_is_not_vacuous_a_broken_copy_is_caught(tmp_path):
    """Negative control: the same lint on a copy with a required property removed must NOT be clean."""
    import shutil
    d = tmp_path / "corpus"
    shutil.copytree(CORPUS, d)
    f = d / "reviewer-rules.lts"
    t = f.read_text()
    f.write_text(t.replace("- severity: major\n", "", 1))           # REVRULE-GEN-1 loses a required property
    r = dispatch("lts_lint", {"source": str(d), "dangle": True}, tools=None)
    v3 = [x for x in r["findings"] if x["kind"] == "missing_required_property"]
    assert len(v3) == 1 and v3[0]["node_id"] == "REVRULE-GEN-1"
    assert r["summary"].get("errors", 0) == 0, "V3 is a WARNING: this is why the self-gate counts it itself"


def test_no_property_is_lost_in_any_shipped_file():
    r = dispatch("lts_property_loss", {"roots": [CORPUS, reviewer.FIXTURES_DIR]}, tools=None)
    assert r["totals"]["files_scanned"] >= 14
    assert r["totals"]["headline_property_lines_lost"] == 0


def test_every_shipped_lts_parses_with_no_findings():
    import glob
    files = glob.glob(os.path.join(CORPUS, "*.lts")) + glob.glob(os.path.join(reviewer.FIXTURES_DIR, "*.lts"))
    assert len(files) >= 14
    for f in files:
        p = parse_file(f)
        assert p.nodes and not p.findings, f


# ----------------------------------------------------------------------------------------- edges are real
def test_the_edges_resolve_inside_the_corpus():
    ids = set()
    edges = []
    for f in sorted(os.listdir(CORPUS)):
        if f.endswith(".lts") and f != "reviewer-profile.lts":
            p = parse_file(os.path.join(CORPUS, f))
            ids |= {n.id for n in p.nodes}
            edges += [e for e in p.edges if e.type in ("ATTACHES_TO_HOOK", "APPLIES_RULE")]
    assert len(edges) == 13
    assert {e.target for e in edges} <= ids
    r = dispatch("lts_lint", {"source": CORPUS, "dangle": True}, tools=None)
    assert not [f for f in r["findings"] if f["origin"] == "review_edges"]
    applied = {e.target for e in edges if e.type == "APPLIES_RULE"}
    assert applied == set(_rules()), "every generic rule is applied by exactly the step that runs it"


# ----------------------------------------------------------------------------------------- the rules
def test_five_generic_rules_each_with_fixtures_that_exist():
    rules = _rules()
    assert sorted(rules) == [f"REVRULE-GEN-{i}" for i in range(1, 8)]
    for rid, n in rules.items():
        for key in ("planted-violation", "clean-twin"):
            path = os.path.normpath(os.path.join(os.path.dirname(RULES_FILE), n.properties[key]))
            assert os.path.isfile(path), (rid, key, path)
            assert path.startswith(reviewer.FIXTURES_DIR)
        assert n.properties["kind"] in ("deterministic", "judgement", "detector")
        assert n.properties["severity"] in ("info", "minor", "major", "blocker")
        assert n.properties["authority"] == "factlattice-reviewer:REVPROC-CORE"


def test_judgement_rules_carry_question_criteria_and_examples_and_deterministic_ones_a_query():
    kinds = {}
    for rid, n in _rules().items():
        kinds[rid] = n.properties["kind"]
        if n.properties["kind"] == "judgement":
            for k in ("question", "pass-criteria", "examples"):
                assert n.properties.get(k), (rid, k)
            assert "query" not in n.properties
        elif n.properties["kind"] == "detector":
            assert n.properties["command"] in ("scan-secrets", "gen6-scan") and n.properties["expect"] == "nonzero"
            assert "query" not in n.properties
        else:
            assert n.properties["expect"] in ("zero", "nonzero") or n.properties["expect"].startswith("count:")
            json.loads(n.properties["query"])
            assert "question" not in n.properties
    assert sorted(k for k, v in kinds.items() if v == "deterministic") == ["REVRULE-GEN-3", "REVRULE-GEN-4"]
    assert sorted(k for k, v in kinds.items() if v == "detector") == ["REVRULE-GEN-6", "REVRULE-GEN-7"]


_ENGINE = []


def _engine():
    """An engine serving the shipped reviewer corpus: the realistic host for a path find."""
    if not _ENGINE:
        from factlattice_engine import config
        from factlattice_engine.tools import LtsEngine
        _ENGINE.append(LtsEngine(config.build_store({reviewer.CORPUS_ID: {
            "roots": [{"path": CORPUS, "area": None}], "domain_suffix": "factlattice-reviewer"}})))
    return _ENGINE[0]


def _run_query(query, fixture_path):
    """The reviewer's door: lts_find with `path`, and the stale-server guards the procedure requires."""
    r = dispatch("lts_find", {**query, "path": fixture_path}, tools=_engine())
    assert r["success"] is True, r
    assert "file_source" in r, "stale-server guard 2a: file_source present"
    assert r["extent"]["scope"]["corpus"] == "(one file, by path)", "stale-server guard 2b"
    return r


def _violation(expect, n_hits):
    if expect == "nonzero":
        return n_hits > 0
    if expect == "zero":
        return n_hits == 0
    return n_hits == int(expect.split(":", 1)[1])


def _validate_rule(props, base):
    """Validation step 4 for a deterministic rule: (plant reports a violation, twin does not). Returns the plant's
    hit ids too."""
    q = json.loads(props["query"])
    plant = _run_query(q, os.path.normpath(os.path.join(base, props["planted-violation"])))
    twin = _run_query(q, os.path.normpath(os.path.join(base, props["clean-twin"])))
    return (_violation(props["expect"], plant["total_matched"]), _violation(props["expect"], twin["total_matched"]),
            sorted(n["id"] for n in plant["nodes"]))


@pytest.mark.parametrize("rid,hit", [("REVRULE-GEN-3", ["FX3P-A"]), ("REVRULE-GEN-4", ["FX4P-A"])])
def test_each_deterministic_generic_rule_fires_on_its_plant_and_is_silent_on_its_twin(rid, hit):
    props = _rules()[rid].properties
    on_plant, on_twin, hits = _validate_rule(props, os.path.dirname(RULES_FILE))
    assert on_plant is True and on_twin is False
    assert hits == hit, "exactly the planted node, not its sourced/dated neighbour"


def test_the_harness_rejects_a_rule_that_matches_everything_and_one_that_matches_nothing():
    base = os.path.dirname(RULES_FILE)
    good = dict(_rules()["REVRULE-GEN-3"].properties)
    everything = {**good, "query": json.dumps({"where": [{"key": "claim", "has": True}]})}
    nothing = {**good, "query": json.dumps({"where": [{"key": "claim", "has": True}, {"key": "claim", "lacks": True}]})}
    plant, twin, _ = _validate_rule(everything, base)
    assert (plant, twin) == (True, True), "matches the twin too: the harness must flag this rule as invalid"
    plant, twin, _ = _validate_rule(nothing, base)
    assert plant is False, "matches the plant not at all: the harness must flag this rule as invalid"
    assert not (plant is True and twin is False)


def test_expect_semantics_zero_and_count():
    assert _violation("zero", 0) and not _violation("zero", 2)
    assert _violation("count:2", 2) and not _violation("count:2", 1)
    assert _violation("nonzero", 1) and not _violation("nonzero", 0)


def test_path_find_is_read_only_on_the_shipped_fixtures():
    import hashlib
    def h():
        return {f: hashlib.sha256(open(os.path.join(reviewer.FIXTURES_DIR, f), "rb").read()).hexdigest()
                for f in sorted(os.listdir(reviewer.FIXTURES_DIR))}
    before = h()
    for rid in ("REVRULE-GEN-3", "REVRULE-GEN-4"):
        _validate_rule(_rules()[rid].properties, os.path.dirname(RULES_FILE))
    assert h() == before


# ----------------------------------------------------------------------------------------- C2 / N2 / C1
def _ext_doc(ext, strict=False):
    head = '---\nlts: "1.1"\ntitle: ext\n---\n' if strict else ""
    return head + f"""# Extension {ext} [SPEC:REVEXT-DOC-{ext}]

## [REVEXT-{ext}] Extension {ext}

- applies-to: host
- version: 1
- authority: host
- hook-contract-version: 1

## [REVHOOK-{ext}-STRUCTURE] Stub

- stub-of: factlattice-reviewer:REVHOOK-STRUCTURE
- hook-contract-version: 1

## [REVHOOK-{ext}-DETERMINISTIC] Stub

- stub-of: factlattice-reviewer:REVHOOK-DETERMINISTIC
- hook-contract-version: 1

## [REVPROC-{ext}-1] Check structure

- attaches-to-hook: [REVHOOK-{ext}-STRUCTURE]
- order: 1

## [REVPROC-{ext}-2] Apply the rule

- attaches-to-hook: [REVHOOK-{ext}-DETERMINISTIC]
- applies-rule: [REVRULE-{ext}-1]
- order: 1

## [REVRULE-{ext}-1] Rule {ext}

- applies-to: nodes with a {ext.lower()} property
- kind: deterministic
- severity: minor
- authority: host
- on-fail: finding
- query: {{"where": [{{"key": "{ext.lower()}", "has": true}}, {{"key": "ok", "lacks": true}}]}}
- expect: nonzero
- planted-violation: fixtures/{ext.lower()}-planted.lts
- clean-twin: fixtures/{ext.lower()}-clean.lts
"""


def _host(tmp_path):
    host = tmp_path / "host"
    (host / "fixtures").mkdir(parents=True)
    (host / "content.lts").write_text("# Content [SPEC:HOSTDOC]\n\n## [HOST-A] a\n\n- note: x\n")
    for ext in ("ONE", "TWO"):
        (host / f"ext-{ext.lower()}.lts").write_text(_ext_doc(ext))
        (host / "fixtures" / f"{ext.lower()}-planted.lts").write_text(
            f"# F [SPEC:FX{ext}P]\n\n## [FX{ext}P-A] a\n\n- {ext.lower()}: yes\n")
        (host / "fixtures" / f"{ext.lower()}-clean.lts").write_text(
            f"# F [SPEC:FX{ext}C]\n\n## [FX{ext}C-A] a\n\n- {ext.lower()}: yes\n- ok: yes\n")
    prof = tmp_path / "host-profile.lts"
    prof.write_text("---\ntitle: p\ndocument_tag: PROFILE\n---\n# p [PROFILE:HP]\n\n## [PROFILE-DOMAIN] d\n\n"
                    f"- domain-id: host\n\n## [REVEXTREG-ONE] one\n\n- path: {host / 'ext-one.lts'}\n- ext-id: ONE\n\n"
                    f"## [REVEXTREG-TWO] two\n\n- path: {host / 'ext-two.lts'}\n- ext-id: TWO\n")
    return host, prof


def test_c1_two_extensions_in_one_corpus_never_collide(tmp_path):
    """Both use STRUCTURE and DETERMINISTIC, both live INSIDE the corpus they serve. Lint (dangle) over the corpus
    gives 0 errors; both load; both rules validate; a third reusing an ext-id is refused naming both paths."""
    from factlattice.profile import load_profile, validate_review_extensions
    host, prof = _host(tmp_path)
    r = dispatch("lts_lint", {"source": str(host), "dangle": True, "profile": str(prof)}, tools=None)
    assert r["summary"].get("errors", 0) == 0, [f for f in r["findings"] if f["severity"] == "error"]
    p = load_profile(str(prof))
    assert [x.ext_id for x in p.review_extensions] == ["ONE", "TWO"]
    assert validate_review_extensions(p, corpus_roots=[str(host)]) == []
    for ext in ("one", "two"):
        props = {k: v for k, v in _node_props(host / f"ext-{ext}.lts", f"REVRULE-{ext.upper()}-1").items()}
        props = {k: (v.replace("fixtures/", str(host / "fixtures") + "/") if "fixtures/" in str(v) else v)
                 for k, v in props.items()}
        on_plant, on_twin, hits = _validate_rule(props, "/")
        assert (on_plant, on_twin) == (True, False), ext
    # ids never collide: the two extensions define disjoint ids although they use the same hook names
    ids = [n.id for ext in ("one", "two") for n in parse_file(str(host / f"ext-{ext}.lts")).nodes]
    assert len(ids) == len(set(ids))
    # a third extension reusing an ext-id is refused, naming both paths
    third = host / "ext-three.lts"
    third.write_text(_ext_doc("THREE").replace("REVEXT-THREE", "REVEXT-ONE"))
    prof.write_text(prof.read_text() + f"\n## [REVEXTREG-THREE] three\n\n- path: {third}\n- ext-id: ONE\n")
    f = validate_review_extensions(load_profile(str(prof)), corpus_roots=[str(host)])
    dup = [x for x in f if x["kind"] == "ext_id_duplicate"]
    assert len(dup) == 1 and str(host / "ext-one.lts") in dup[0]["message"] and str(third) in dup[0]["message"]


def _node_props(path, node_id):
    return {n.id: n for n in parse_file(str(path)).nodes}[node_id].properties


def test_n2_a_stray_extension_prefixed_node_in_an_ordinary_document_stays_in_the_frame(tmp_path):
    """The frame is selected by SOURCE FILE, never by prefix: a REVRULE-X node in an ordinary document of the
    reviewed corpus is returned by the frame query; the nodes of a registered extension document are not."""
    from factlattice_engine import config
    from factlattice_engine.tools import LtsEngine
    host, prof = _host(tmp_path)
    (host / "ordinary.lts").write_text("# Ordinary [SPEC:ORDDOC]\n\n## [REVRULE-X] A stray node in ordinary content\n\n- note: content\n")
    eng = LtsEngine(config.build_store({"host": {"roots": [{"path": str(host), "area": None}], "domain_suffix": "h"}}))
    frame = dispatch("lts_find", {"corpus": "host", "source_file": "ordinary.lts"}, tools=eng)
    assert "REVRULE-X" in [n["id"] for n in frame["nodes"]]
    ext = dispatch("lts_find", {"corpus": "host", "source_file": "ext-one.lts"}, tools=eng)
    assert {n["id"] for n in ext["nodes"]} >= {"REVEXT-ONE", "REVRULE-ONE-1"}
    assert "REVRULE-X" not in [n["id"] for n in ext["nodes"]]


def test_c2_an_extension_inside_the_host_adds_no_error_to_the_hosts_own_lint(tmp_path):
    host, prof = _host(tmp_path)
    with_ext = dispatch("lts_lint", {"source": str(host), "dangle": True, "profile": str(prof)}, tools=None)
    import shutil
    bare = tmp_path / "bare"
    bare.mkdir()
    shutil.copy(host / "content.lts", bare / "content.lts")
    without = dispatch("lts_lint", {"source": str(bare), "dangle": True, "profile": str(prof)}, tools=None)
    assert with_ext["summary"].get("errors", 0) == without["summary"].get("errors", 0) == 0


def test_the_core_procedure_guards_against_a_server_too_old_to_report_extensions(tmp_path, monkeypatch):
    """`lts_profile_audit` omits `review_extensions` when nothing is registered, so a server that predates it looks
    exactly like 'no extensions'. The procedure's tripwire is a count of `- ext-id:` lines in the profile file against
    the answer's registered list. Pin that the procedure says so, that the profile path it needs IS in the audit
    answer (resolved from the registry, as in a deployment), and that the count it relies on is what a real
    registration produces."""
    text = open(os.path.join(CORPUS, "reviewer-core.lts"), encoding="utf-8").read()
    for needle in ("Stale-server guard for extensions", "- ext-id:", "registrations-not-projected", "check-registrations --corpus"):
        assert needle in text, needle
    host, prof = _host(tmp_path)
    reg = {"host": {"roots": [{"path": str(host), "area": None}], "domain_suffix": "h", "profile": str(prof)}}
    cfg = tmp_path / "registry.json"
    cfg.write_text(json.dumps(reg))
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(cfg))
    from factlattice_engine import config
    from factlattice_engine.tools import LtsEngine
    eng = LtsEngine(config.build_store(reg))
    r = dispatch("lts_profile_audit", {"corpus": "host"}, tools=eng)
    assert r["profile"]["resolved_from"] == "corpus-config" and r["profile"]["path"] == str(prof)
    assert r["review_extensions"]["registered_count"] == open(prof).read().count("- ext-id:") == 2
