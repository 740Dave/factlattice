"""disclosure_guard: the generic reader for a generated neutrality-terms file.

The guard reads ONE optional JSON file of agent and corpus ids (named by FACTLATTICE_NEUTRALITY_TERMS or --neutrality-terms),
turns each id into a rule, and matches them with a copy of the writer's matcher. Every term here is SYNTHETIC (`zorpbot`, `glim`,
`QZX`, `frumpkin-lib`); the false-positive context table used here is a synthetic one with the same two shapes as the real one.
The real table is private and is loaded from a private file, so it is never in this repository.

RED FIRST: `DG_GUARD_UNDER_TEST` points the tests at another copy of the guard (default: tools/disclosure_guard.py of this checkout),
so a run against an older copy shows each requirement red.
"""

import datetime
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

from tests._snapshot import is_published_snapshot, skip_reason

ROOT = pathlib.Path(__file__).resolve().parents[1]
GUARD = pathlib.Path(os.environ.get("DG_GUARD_UNDER_TEST") or ROOT / "tools" / "disclosure_guard.py")
T = "\t"
AGENTS = ["QZX", "glim", "zorpbot"]
CORPORA = ["frumpkin-lib", "vendel"]
VOCAB = T.join([r"quillworks", "built by quillworks inc", "a vendor name", "vocab-vendor"]) + "\n"
DENY = T.join([r"marblefoundry", "a marblefoundry order", "a synthetic customer", "deny-customer"]) + "\n"
CTX = {"zorp": [r"(?!(?<=\.)(?i:zorp_store)(?![A-Za-z0-9]))"], "glim": [r"(?<!(?i:need to ))"]}
# the real clock at import: the CLI runs read the real clock too, so a stamp made from NOW is 60 s old to them as well
NOW = datetime.datetime.now(datetime.UTC).replace(microsecond=0)


def load_guard():
    spec = importlib.util.spec_from_file_location("dg_under_test_neutrality", str(GUARD))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def stamp(offset_s=0):
    return (NOW + datetime.timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def digest(agents, corpora, words=None):
    doc = {"agent_ids": agents, "corpus_ids": corpora}
    if words is not None:
        doc["word_ids"] = words
    body = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def terms_doc(agents=None, corpora=None, generated_at=None, hand=None, sha=None, words=None):
    agents = sorted(AGENTS) if agents is None else agents
    corpora = sorted(CORPORA) if corpora is None else corpora
    return {
        "_header": {
            "generated_at": stamp(-60) if generated_at is None else generated_at,
            "generator": "x",
            "sources": {"hand_additions": {"agent_ids": 1, "corpus_ids": 1} if hand is None else hand},
            "sha256": digest(agents, corpora, words) if sha is None else sha,
        },
        "agent_ids": agents,
        "corpus_ids": corpora,
        **({} if words is None else {"word_ids": words}),
    }


def write(path, doc):
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return str(path)


# --- running the guard as a subprocess, with every private input pointed at scratch -----------------------------------
def cli(tmp_path, *args, tree=None, env_extra=None, mode="--tree", flag_terms=None, guard=None, vocab=None, deny=None):
    """Run the guard in `mode` on `tree`. The vocabulary and the denylist are scratch files; the neutrality variables are
    removed from the environment first, so only what a test passes is in force. -> CompletedProcess."""
    tree = tree or make_tree(tmp_path)
    priv = tmp_path / "priv"
    priv.mkdir(exist_ok=True)
    (priv / "vocab.tsv").write_text(VOCAB if vocab is None else vocab, encoding="utf-8")
    (priv / "deny.tsv").write_text(DENY if deny is None else deny, encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("FACTLATTICE_NEUTRALITY") and k not in ("FACTLATTICE_CORPUS_CONFIG", "FACTLATTICE_CORPUS_ROOT", "XDG_CONFIG_HOME")}
    env["HOME"] = str(tmp_path / "no-home")       # no per-user registry pointer: only a registry a test names is in force
    env["FACTLATTICE_PUBLISH_VOCAB"] = str(priv / "vocab.tsv")
    env["FACTLATTICE_DISCLOSURE_RULES"] = str(priv / "deny.tsv")
    env.update(env_extra or {})
    takes_dir = mode in ("--tree", "--publication")
    argv = [sys.executable, str(guard or GUARD), mode] + ([str(tree)] if takes_dir else [])
    if flag_terms:
        argv += ["--neutrality-terms", flag_terms]
    return subprocess.run(argv + list(args), cwd=str(tmp_path if takes_dir else tree), env=env, capture_output=True, text=True, timeout=120)


def make_tree(tmp_path, files=None):
    t = tmp_path / "tree"
    (t / "src").mkdir(parents=True, exist_ok=True)
    (t / "src" / "ok.py").write_text("print('library loans')\n", encoding="utf-8")
    for rel, text in (files or {}).items():
        (t / rel).parent.mkdir(parents=True, exist_ok=True)
        (t / rel).write_text(text, encoding="utf-8")
    return t


def out(r):
    return r.stdout + r.stderr


def good_file(tmp_path, **kw):
    return write(tmp_path / "terms.json", terms_doc(**kw))


# =========================================================================================================
# 1. a generic reader in the shipped guard
# =========================================================================================================
def test_r1_the_flag_turns_each_id_into_a_rule_named_by_its_family(tmp_path):
    tree = make_tree(tmp_path, {"src/a.py": "# the zorpbot did this\n", "src/b.py": "# see frumpkin-lib\n"})
    r = cli(tmp_path, tree=tree, flag_terms=good_file(tmp_path))
    assert r.returncode == 1, out(r)
    assert "src/a.py:1  [neutrality-agent-id]" in r.stdout and "src/b.py:1  [neutrality-corpus-id]" in r.stdout, out(r)


def test_r1_the_environment_variable_names_the_file_too(tmp_path):
    tree = make_tree(tmp_path, {"src/a.py": "# the zorpbot did this\n"})
    r = cli(tmp_path, tree=tree, env_extra={"FACTLATTICE_NEUTRALITY_TERMS": good_file(tmp_path)})
    assert r.returncode == 1 and "[neutrality-agent-id]" in r.stdout, out(r)


def test_r1_the_flag_wins_over_the_environment_variable(tmp_path):
    other = write(tmp_path / "other.json", terms_doc(agents=["otherbot"], corpora=["othercorp"]))
    tree = make_tree(tmp_path, {"src/a.py": "# zorpbot\n"})
    r = cli(tmp_path, tree=tree, flag_terms=other, env_extra={"FACTLATTICE_NEUTRALITY_TERMS": good_file(tmp_path)})
    assert r.returncode == 0, "the flag's file has no such term, so a hit means the environment variable won\n" + out(r)


def test_r1_the_selftest_runs_every_generated_rule_against_its_own_control(tmp_path):
    r = cli(tmp_path, mode="--selftest", flag_terms=good_file(tmp_path))
    caught_agent = re.findall(r"caught \[neutrality-agent-id\] on its own control", r.stdout)
    caught_corpus = re.findall(r"caught \[neutrality-corpus-id\] on its own control", r.stdout)
    assert (len(caught_agent), len(caught_corpus)) == (len(AGENTS), len(CORPORA)), out(r)
    assert "FAILED" not in r.stdout and r.returncode == 0, out(r)


def test_r1_negative_control_a_mute_generated_rule_is_a_selftest_failure():
    g = load_guard()
    terms = g.load_neutrality_terms  # the reader exists
    assert callable(terms)
    rules, problem = g.neutrality_rules(g.NeutralityTerms(True, agent_ids=["zorpbot"], corpus_ids=["vendel"]))
    assert problem is None and len(rules) == 2
    rules[0].control = "nothing of the sort"  # a rule that cannot fire on its control
    failures = g._control_failures(rules, echo=None, reveal=False)
    assert any(f.startswith("MUTE") for f in failures), failures


def test_r1_a_generated_term_that_appears_in_a_legitimate_control_line_does_not_fail_the_selftest(tmp_path):
    """The legitimate-line controls (acme.ds ...) guard the hand-written structural rules; a generated vocabulary cannot be known
    in advance, so a term such as `acme` must not turn the guard's own selftest red."""
    f = write(tmp_path / "t.json", terms_doc(agents=["acme"], corpora=["vendel"]))
    r = cli(tmp_path, mode="--selftest", flag_terms=f)
    assert "FALSE POSITIVE" not in r.stdout and "FAILED" not in r.stdout and r.returncode == 0, out(r)


def test_r1_no_deployment_name_path_or_product_appears_in_the_shipped_reader_or_its_tests():
    # fragments are joined here so this file does not name them either
    forbidden = ["gno" + "mon", "gen_neutrality" + "_terms", "neutrality" + "_patterns", "false_positive" + "_contexts"]
    for rel in ("tools/disclosure_guard.py", "tests/test_neutrality_terms.py"):
        text = (ROOT / rel).read_text(encoding="utf-8").lower()
        for word in forbidden:
            assert word not in text, (rel, word)


def test_r1_the_shipped_guard_carries_no_context_table_row():
    """The writer's table (short words that are also names here) is private. The shipped matcher has an empty one: a term
    with no table row is matched everywhere, so a missing private table can only ADD hits."""
    g = load_guard()
    ((pattern, _flags),) = g._term_regex(["zorp"])
    assert pattern == r"(?<![A-Za-z0-9])(?:zorp)(?![A-Za-z0-9])", pattern  # only the two edges; nothing before the term
    assert not [n for n in dir(g) if "FALSE_POSITIVE" in n.upper() and isinstance(getattr(g, n), dict)]


# =========================================================================================================
# 2. matching parity with the writer's matcher
# =========================================================================================================
# LEGACY-MODE TABLE: a fixed table taken from the writer's own cases, for terms of four or more characters (the matching of
# today's files, which the short-id rule does not touch). The writer's shared fixture (tests/data/term_regex_cases.json, pinned by sha256 in section
# "word_ids and the shared case fixture" below) is the drift alarm for the word-id and short-id rules and for the rows both tables state; this table is kept because
# it also covers what the fixture does not: all-capital terms of four and five characters (case-sensitive) against six (not), a
# term escaped rather than read as a pattern, a digit before a term, an underscore before a term, and the false-positive context rows
# (synthetic terms standing for the two real ones). Its own sha256 is pinned below so an edit is deliberate.
# (term, line, hit)
LEGACY_CASES = [
    ("zorpbot", "zorpbot", True),
    ("zorpbot", "a zorpbot b", True),
    ("zorpbot", "zorpbot.", True),
    ("zorpbot", "zorpbots", False),
    ("zorpbot", "xzorpbot", False),
    ("zorpbot", "zorpbot2", False),
    ("zorpbot", "2zorpbot", False),
    ("zorpbot", "x-zorpbot", True),
    ("zorpbot", "zorpbot-x", True),
    ("zorpbot", "zorpbot_x", True),
    ("zorpbot", "x_zorpbot", True),
    ("zorpbot", "ZORPBOT", True),
    ("zorpbot", "ZorpBot", True),
    ("QZX", "QZX", True),
    ("QZX", "see QZX here", True),
    ("QZX", "qzx", False),
    ("QZX", "Qzx", False),
    ("QZX", "QZXE", False),
    ("QZXWVU", "qzxwvu", True),  # six capitals: no longer a short acronym, so case-insensitive
    ("QZXWV", "qzxwv", False),  # five capitals: still a short acronym, so case-sensitive
    ("a.bc", "a.bc", True),
    ("a.bc", "axbc", False),  # a term is escaped, never a pattern
    # false-positive contexts: a hit is skipped only INSIDE a listed context
    ("zorp", "rm -f ~/Documents/.ZORP_STORE", False),
    ("zorp", ".ZORP_STORE", False),
    ("zorp", "ignore: .zorp_store and nothing else", False),
    ("zorp", "ZORP_STORE without the leading dot", True),
    ("zorp", "x.zorp_other", True),
    ("zorp", "a path segment /zorp/", True),
    ("zorp", "zorp", True),
    ("glim", "you need to glim the path", False),
    ("glim", "Need to GLIM basis only", False),
    ("glim", "she needs to glim", True),
    ("glim", "ask glim about it", True),
    ("glim", "the glim module", True),
    ("glim", "glim", True),
]
CONTEXTS = {k: tuple(v) for k, v in CTX.items()}


def _table_sha():
    return hashlib.sha256(json.dumps(LEGACY_CASES, separators=(",", ":")).encode("utf-8")).hexdigest()


def test_r2_the_legacy_mode_case_table_is_pinned():
    assert _table_sha() == "a37dc8d34291c81c04fdd42e00627b822e4a1368a669cd910d811ec4c0e976e7", (
        "the legacy-mode table changed; an edit to it must be deliberate (the shared fixture is pinned separately)"
    )


@pytest.mark.parametrize("term,line,hit", LEGACY_CASES)
def test_r2_the_copied_matcher_agrees_with_every_case(term, line, hit):
    g = load_guard()
    got = any(re.search(p, line, f) for p, f in g._term_regex([term], CONTEXTS))
    assert got is hit, (term, line, hit)


@pytest.mark.parametrize("term,line,hit", LEGACY_CASES)
def test_r2_a_generated_rule_agrees_with_every_case_too(term, line, hit):
    """The rule the guard really scans with (one per id, built by neutrality_rules) must give the matcher's answer."""
    g = load_guard()
    key = "corpus_ids" if term == "a.bc" else "agent_ids"
    terms = g.NeutralityTerms(True, agent_ids=[term] if key == "agent_ids" else [], corpus_ids=[term] if key == "corpus_ids" else [])
    rules, problem = g.neutrality_rules(terms, CONTEXTS)
    assert problem is None
    assert bool(g._matches(rules, line)) is hit, (term, line, hit)


def test_r2_one_matcher_over_many_terms_equals_the_rules_one_by_one():
    g = load_guard()
    terms = ["zorpbot", "QZX", "zorp", "glim", "a.bc"]
    combined = g._term_regex(terms, CONTEXTS)
    for _t, line, _h in LEGACY_CASES:
        together = any(re.search(p, line, f) for p, f in combined)
        apart = any(re.search(p, line, f) for t in terms for p, f in g._term_regex([t], CONTEXTS))
        assert together is apart, line


def test_r2_the_copy_cites_its_source_commit():
    assert "b83114100844959cea35b358332c5796ffa0b9d9" in GUARD.read_text(encoding="utf-8")


def test_r2_the_context_table_is_read_from_a_private_file(tmp_path):
    """The table lives in a file the deployment keeps; the shipped code has none. With the file named, a listed context is
    skipped; without it, the same line is a hit."""
    tree = make_tree(tmp_path, {"src/a.py": "x = '.ZORP_STORE'\n"})
    terms = write(tmp_path / "t.json", terms_doc(agents=["zorp"], corpora=["vendel"]))
    without = cli(tmp_path, tree=tree, flag_terms=terms)
    assert without.returncode == 1, out(without)
    ctx = write(tmp_path / "ctx.json", CTX)
    with_ctx = cli(tmp_path, "--neutrality-contexts", ctx, tree=tree, flag_terms=terms)
    assert with_ctx.returncode == 0, out(with_ctx)
    via_env = cli(tmp_path, tree=tree, flag_terms=terms, env_extra={"FACTLATTICE_NEUTRALITY_CONTEXTS": ctx})
    assert via_env.returncode == 0, out(via_env)


def test_r2_a_context_table_under_the_default_private_location_is_found(tmp_path):
    tree = make_tree(tmp_path, {"src/a.py": "x = '.ZORP_STORE'\n"})
    (tree / "PUBLISH-EXCLUDE").write_text("private/   # the deployment overlay never crosses\n", encoding="utf-8")
    (tree / "private").mkdir()
    (tree / "private" / "neutrality-term-contexts.json").write_text(json.dumps(CTX), encoding="utf-8")
    terms = write(tmp_path / "t.json", terms_doc(agents=["zorp"], corpora=["vendel"]))
    assert cli(tmp_path, tree=tree, flag_terms=terms).returncode == 0


@pytest.mark.parametrize("bad", ['["not", "an", "object"]', '{"zorp": []}', '{"Zorp": ["x"]}', '{"zorp": ["("]}', "{not json"])
def test_r2_an_unusable_context_table_loads_nothing_and_says_so(tmp_path, bad):
    tree = make_tree(tmp_path, {"src/a.py": "# zorp\n"})
    ctx = write(tmp_path / "ctx.json", bad)
    terms = write(tmp_path / "t.json", terms_doc(agents=["zorp"], corpora=["vendel"]))
    r = cli(tmp_path, "--neutrality-contexts", ctx, tree=tree, flag_terms=terms)
    assert r.returncode == 0 and "NOT LOADED" in r.stdout and "context table" in r.stdout, out(r)


def test_r2_a_context_that_swallows_its_own_term_is_refused_not_loaded():
    g = load_guard()
    rules, problem = g.neutrality_rules(g.NeutralityTerms(True, agent_ids=["zorp"], corpus_ids=["vendel"]), {"zorp": (r"(?!)",)})
    assert rules == [] and problem and "own control" in problem


# =========================================================================================================
# 3. validation: shape, sha256 and freshness, with five causes in order
# =========================================================================================================
def check(tmp_path, doc, now=NOW):
    return load_guard().load_neutrality_terms(write(tmp_path / "f.json", doc), now=now)


def test_r3_a_good_file_is_usable_and_carries_the_ids_sha_and_time(tmp_path):
    res = check(tmp_path, terms_doc())
    assert res.ok and res.cause is None
    assert list(res.agent_ids) == sorted(AGENTS) and list(res.corpus_ids) == sorted(CORPORA)
    assert res.sha256 == digest(sorted(AGENTS), sorted(CORPORA)) and res.generated_at == stamp(-60)


def test_r3_missing_is_the_first_cause(tmp_path):
    res = load_guard().load_neutrality_terms(str(tmp_path / "nope.json"), now=NOW)
    assert (res.ok, res.cause) == (False, "missing")


@pytest.mark.parametrize("raw", ["", "{not json", "[]x", '{"a": 1}{"b": 2}'])
def test_r3_unparsable_text_is_unparsable(tmp_path, raw):
    res = check(tmp_path, raw)
    assert (res.ok, res.cause) == (False, "unparsable")


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "2026-11-23 11:59:00",
        "2026-11-23T11:59:00+00:00",
        "2026-11-23T11:59:00",
        "2026-11-23T11:59:00Z\n",
        "2026-11-23T11:59:00.5Z",
        "2026-13-45T11:59:00Z",
        "2026-11-23T25:00:00Z",
        1795000000,
        "٢026-11-23T11:59:00Z",
        " 2026-11-23T11:59:00Z",
    ],
)
def test_r3_a_missing_or_not_strictly_formed_generated_at_is_unparsable_never_stale(tmp_path, value):
    doc = terms_doc()
    if value is None:
        del doc["_header"]["generated_at"]
    else:
        doc["_header"]["generated_at"] = value
    res = check(tmp_path, doc)
    assert (res.ok, res.cause) == (False, "unparsable"), (value, res.cause)


def test_r3_future_dated_beyond_the_clock_skew_and_the_boundary(tmp_path):
    assert check(tmp_path, terms_doc(generated_at=stamp(301))).cause == "future-dated"
    assert check(tmp_path, terms_doc(generated_at=stamp(300))).ok, "300 s of skew is allowed"
    assert check(tmp_path, terms_doc(generated_at=stamp(1))).ok


def test_r3_stale_at_21600_seconds_and_the_boundary(tmp_path):
    assert check(tmp_path, terms_doc(generated_at=stamp(-21600))).cause == "stale"
    assert check(tmp_path, terms_doc(generated_at=stamp(-21599))).ok, "under 21,600 s old is fresh"
    assert check(tmp_path, terms_doc(generated_at=stamp(-90000))).cause == "stale"


def test_r3_partial_when_hand_additions_were_absent(tmp_path):
    res = check(tmp_path, terms_doc(hand="NONE (file absent)"))
    assert (res.ok, res.cause) == (False, "partial")


def test_r3_the_causes_are_reported_in_order(tmp_path):
    """A file with several faults names the FIRST in the order missing, unparsable, (shape, sha256), future-dated, stale, partial."""
    partial = "NONE (file absent)"
    assert check(tmp_path, terms_doc(generated_at=stamp(-90000), hand=partial)).cause == "stale"  # stale before partial
    assert check(tmp_path, terms_doc(generated_at=stamp(900), hand=partial)).cause == "future-dated"  # future before partial
    assert check(tmp_path, terms_doc(generated_at="soon", hand=partial)).cause == "unparsable"  # unparsable before partial
    assert check(tmp_path, terms_doc(generated_at=stamp(-90000), sha="0" * 64)).cause == "sha256-mismatch"  # sha before freshness
    bad = terms_doc(generated_at=stamp(-90000), hand=partial)
    bad["extra"] = 1
    assert check(tmp_path, bad).cause == "shape"  # shape before freshness


def test_r3_every_failure_names_its_cause_and_never_a_term(tmp_path):
    g = load_guard()
    docs = [
        terms_doc(generated_at=stamp(-90000)),
        terms_doc(generated_at=stamp(900)),
        terms_doc(hand="NONE (file absent)"),
        terms_doc(sha="0" * 64),
        "{nope",
        terms_doc(agents=sorted(AGENTS, reverse=True)),
    ]
    docs.append(dict(terms_doc(), extra=["zorpbot"]))
    for doc in docs:
        res = g.load_neutrality_terms(write(tmp_path / "f.json", doc), now=NOW)
        assert not res.ok and res.cause in g.NEUTRALITY_CAUSES
        shown = res.why()
        assert not any(t.lower() in shown.lower() for t in AGENTS + CORPORA), shown


def test_r3_the_sha256_is_over_the_canonical_json_of_the_two_lists(tmp_path):
    """Pinned to a hand-computed value, so the test cannot agree with a wrong canonical form by sharing the code that makes it."""
    agents, corpora = ["aa"], ["bb"]
    expected = hashlib.sha256(b'{"agent_ids":["aa"],"corpus_ids":["bb"]}').hexdigest()
    assert check(tmp_path, terms_doc(agents=agents, corpora=corpora, sha=expected)).ok
    spaced = hashlib.sha256(b'{"agent_ids": ["aa"], "corpus_ids": ["bb"]}').hexdigest()
    assert check(tmp_path, terms_doc(agents=agents, corpora=corpora, sha=spaced)).cause == "sha256-mismatch"


# =========================================================================================================
# 6. drift: each case fails loudly, never a quiet partial load. One test per case.
# =========================================================================================================
def _assert_loud(tmp_path, doc):
    """The reader refuses, the dev scan warns and loads nothing from the file, and the publication gate refuses."""
    res = check(tmp_path, doc)
    assert not res.ok and res.cause in ("shape", "sha256-mismatch", "unparsable"), res.cause
    tree = make_tree(tmp_path, {"src/a.py": "# zorpbot frumpkin-lib vendel\n"})
    f = write(tmp_path / "drift.json", doc)
    dev = cli(tmp_path, tree=tree, flag_terms=f)
    assert dev.returncode == 0 and "NOT LOADED" in dev.stdout and res.cause in dev.stdout, "a drifted file must warn\n" + out(dev)
    assert "[neutrality-" not in dev.stdout, "nothing may be loaded from a drifted file\n" + out(dev)
    pub = cli(tmp_path, tree=tree, mode="--publication", flag_terms=f)
    assert pub.returncode == 1 and "REFUSED" in pub.stdout and res.cause in pub.stdout, out(pub)
    assert not any(t in out(dev) + out(pub) for t in ("zorpbot", "frumpkin-lib", "vendel"))


def test_r6_an_unknown_top_level_key(tmp_path):
    _assert_loud(tmp_path, dict(terms_doc(), surprise=["x"]))


@pytest.mark.parametrize("field", ["_header", "agent_ids", "corpus_ids"])
def test_r6_a_missing_required_field(tmp_path, field):
    doc = terms_doc()
    del doc[field]
    _assert_loud(tmp_path, doc)


@pytest.mark.parametrize("field", ["agent_ids", "corpus_ids"])
def test_r6_an_empty_list(tmp_path, field):
    doc = terms_doc()
    doc[field] = []
    doc["_header"]["sha256"] = digest(doc["agent_ids"], doc["corpus_ids"])  # a consistent sha: only the emptiness is wrong
    _assert_loud(tmp_path, doc)


def test_r6_a_sha_mismatch(tmp_path):
    _assert_loud(tmp_path, terms_doc(sha="f" * 64))


def test_r6_a_list_changed_after_the_file_was_written(tmp_path):
    doc = terms_doc()
    doc["agent_ids"] = sorted(doc["agent_ids"] + ["zzextra"])  # sorted, non-empty, but the sha no longer matches
    _assert_loud(tmp_path, doc)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.__setitem__("agent_ids", sorted(AGENTS, reverse=True)),  # not sorted
        lambda d: d.__setitem__("agent_ids", ["zorpbot", 7]),  # not strings
        lambda d: d.__setitem__("agent_ids", ["", "zorpbot"]),  # an empty term would match everything
        lambda d: d.__setitem__("agent_ids", "zorpbot"),  # not a list
        lambda d: d["_header"].pop("sha256"),
        lambda d: d["_header"]["sources"].__setitem__("hand_additions", 3),
        lambda d: d["_header"].pop("sources"),
        lambda d: d.__setitem__("_header", []),
    ],
    ids=["unsorted", "non-string", "empty-term", "not-a-list", "no-sha", "bad-hand", "no-sources", "header-not-object"],
)
def test_r6_the_other_shape_breaks(tmp_path, mutate):
    doc = terms_doc()
    mutate(doc)
    res = check(tmp_path, doc)
    assert not res.ok and res.cause in ("shape", "sha256-mismatch")


# =========================================================================================================
# 4. two modes (the lenient modes, and the publication gate)
# =========================================================================================================
def test_r4_a_dev_scan_with_no_input_warns_once_and_names_the_missing_input(tmp_path):
    r = cli(tmp_path)
    assert r.returncode == 0, out(r)
    lines = [ln for ln in r.stdout.splitlines() if "neutrality terms: NOT LOADED" in ln]
    assert len(lines) == 1 and "FACTLATTICE_NEUTRALITY_TERMS" in lines[0] and "NOT checked" in lines[0], out(r)


def test_r4_a_named_but_absent_file_warns_with_the_cause(tmp_path):
    r = cli(tmp_path, flag_terms=str(tmp_path / "nope.json"))
    assert r.returncode == 0 and "NOT LOADED" in r.stdout and "missing" in r.stdout, out(r)


def test_r4_a_stale_file_warns_names_stale_and_loads_nothing(tmp_path):
    tree = make_tree(tmp_path, {"src/a.py": "# zorpbot\n"})
    r = cli(tmp_path, tree=tree, flag_terms=good_file(tmp_path, generated_at="2020-01-01T00:00:00Z"))
    assert r.returncode == 0 and "NOT LOADED" in r.stdout and "stale" in r.stdout, out(r)
    assert "[neutrality-" not in r.stdout


def test_r4_a_valid_file_is_loaded_and_the_run_says_how_many_and_which_digest(tmp_path):
    r = cli(tmp_path, flag_terms=good_file(tmp_path))
    assert "loaded 3 agent id(s) and 2 corpus id(s)" in r.stdout and digest(sorted(AGENTS), sorted(CORPORA)) in r.stdout, out(r)


def test_r4_the_selftest_warns_too(tmp_path):
    r = cli(tmp_path, mode="--selftest")
    assert "neutrality terms: NOT LOADED" in r.stdout, out(r)


def snapshot_tree(tmp_path, files=None):
    t = make_tree(tmp_path, files)
    (t / "PUBLISH-EXCLUDE").write_text("docs/internal/   # build logs never cross\n", encoding="utf-8")
    return t


def test_r4_publication_with_no_input_and_no_requirement_is_as_before_plus_a_visible_warning(tmp_path):
    r = cli(tmp_path, tree=snapshot_tree(tmp_path), mode="--publication")
    assert r.returncode == 0 and "RESULT: green" in r.stdout, out(r)
    assert "neutrality terms: NOT LOADED" in r.stdout
    scope = next(ln for ln in r.stdout.splitlines() if ln.startswith("publication scope:"))
    assert "neutrality-terms" not in scope, "the scope line is what it always was when no input is named"


def test_r4_publication_required_with_no_input_refuses(tmp_path):
    r = cli(tmp_path, "--require-neutrality-terms", tree=snapshot_tree(tmp_path), mode="--publication")
    assert r.returncode == 1 and "RESULT: REFUSED" in r.stdout and "neutrality terms: required but no input named" in r.stdout, out(r)


@pytest.mark.parametrize(
    "kw,cause",
    [
        ({"generated_at": "2020-01-01T00:00:00Z"}, "stale"),
        ({"hand": "NONE (file absent)"}, "partial"),
        ({"sha": "0" * 64}, "sha256-mismatch"),
    ],
)
def test_r4_publication_required_with_an_invalid_file_refuses_and_names_the_cause(tmp_path, kw, cause):
    r = cli(
        tmp_path, "--require-neutrality-terms", tree=snapshot_tree(tmp_path), mode="--publication", flag_terms=good_file(tmp_path, **kw)
    )
    assert r.returncode == 1 and "RESULT: REFUSED" in r.stdout and cause in r.stdout, out(r)
    assert not any(t in out(r) for t in AGENTS + CORPORA)


def test_r4_publication_with_a_named_invalid_file_refuses_even_when_not_required(tmp_path):
    r = cli(tmp_path, tree=snapshot_tree(tmp_path), mode="--publication", flag_terms=good_file(tmp_path, sha="0" * 64))
    assert r.returncode == 1 and "RESULT: REFUSED" in r.stdout, out(r)


def test_r4_publication_with_a_valid_file_is_green_and_the_scope_line_has_the_digest_and_time_never_a_term(tmp_path):
    rep = tmp_path / "rep.json"
    r = cli(
        tmp_path,
        "--require-neutrality-terms",
        "--report",
        str(rep),
        tree=snapshot_tree(tmp_path),
        mode="--publication",
        flag_terms=good_file(tmp_path),
    )
    assert r.returncode == 0 and "RESULT: green" in r.stdout, out(r)
    scope = next(ln for ln in r.stdout.splitlines() if ln.startswith("publication scope:"))
    assert f"sha256={digest(sorted(AGENTS), sorted(CORPORA))}" in scope and f"generated_at={stamp(-60)}" in scope, scope
    doc = json.loads(rep.read_text(encoding="utf-8"))
    n = doc["scope"]["neutrality_terms"]
    assert (n["state"], n["agent_ids"], n["corpus_ids"]) == ("loaded", 3, 2)
    assert not any(t in rep.read_text(encoding="utf-8") + out(r) for t in AGENTS + CORPORA)


def test_r4_publication_with_a_valid_file_finds_a_planted_id_and_prints_neither_it_nor_its_line(tmp_path):
    tree = snapshot_tree(tmp_path, {"src/leak.py": "# the zorpbot wrote this\n"})
    r = cli(tmp_path, "--require-neutrality-terms", tree=tree, mode="--publication", flag_terms=good_file(tmp_path))
    assert r.returncode == 1 and "src/leak.py:1  [neutrality-agent-id]" in r.stdout, out(r)
    assert "zorpbot" not in out(r)


def test_r4_the_requirement_flag_belongs_to_the_publication_mode(tmp_path):
    r = cli(tmp_path, "--require-neutrality-terms")
    assert r.returncode == 2 and "belong to --publication" in r.stderr, out(r)


# =========================================================================================================
# shipped product names are exempt (BY NAME), an expected sha256, the selftest's last line
# =========================================================================================================
SHIPPED = {"agents/alpha-agent.md": "# an agent definition\n"}


def exempt_terms(tmp_path, agents, corpora=None):
    return write(tmp_path / "t.json", terms_doc(agents=sorted(agents), corpora=sorted(corpora or ["vendel"])))


def test_f2_an_id_equal_to_a_shipped_definition_stem_is_exempt_and_listed_by_name(tmp_path):
    tree = make_tree(tmp_path, {**SHIPPED, "src/a.py": "# the alpha-agent reviews this\n"})
    r = cli(tmp_path, tree=tree, flag_terms=exempt_terms(tmp_path, ["alpha-agent", "zorpbot"]))
    assert r.returncode == 0, out(r)
    assert "neutrality terms: 1 generated agent id(s) exempt as shipped product names: alpha-agent" in r.stdout, out(r)
    assert "zorpbot" not in out(r), "an id that is not exempt is never printed"


def test_f2_an_id_with_no_shipped_definition_is_still_refused(tmp_path):
    tree = make_tree(tmp_path, {**SHIPPED, "src/a.py": "# the zorpbot did this\n"})
    r = cli(tmp_path, tree=tree, flag_terms=exempt_terms(tmp_path, ["alpha-agent", "zorpbot"]))
    assert r.returncode == 1 and "src/a.py:1  [neutrality-agent-id]" in r.stdout, out(r)


def test_f2_an_excluded_definition_exempts_nothing(tmp_path):
    tree = make_tree(tmp_path, {**SHIPPED, "src/a.py": "# the alpha-agent reviews this\n"})
    (tree / "PUBLISH-EXCLUDE").write_text("agents/alpha-agent.md   # an internal definition\n", encoding="utf-8")
    r = cli(tmp_path, tree=tree, flag_terms=exempt_terms(tmp_path, ["alpha-agent"]))
    assert r.returncode == 1 and "src/a.py:1  [neutrality-agent-id]" in r.stdout, out(r)
    assert "exempt as shipped product names: alpha-agent" not in out(r)


@pytest.mark.parametrize(
    "term,text", [("foo-bar", "foo-bar"), ("barfoo", "barfoo"), ("Food", "Food")], ids=["longer", "prefixed", "case-variant"]
)
def test_f2_only_an_exact_stem_match_exempts(tmp_path, term, text):
    """agents/food.md exempts `food` and never `foo-bar`, `barfoo` or `Food` (the three-character `foo` would be a short id, upper case only)."""
    tree = make_tree(tmp_path, {"agents/food.md": "# an agent definition\n", "src/a.py": f"# {text}\n"})
    r = cli(tmp_path, tree=tree, flag_terms=exempt_terms(tmp_path, [term]))
    assert r.returncode == 1 and "src/a.py:1  [neutrality-agent-id]" in r.stdout, out(r)


def test_f2_the_exact_stem_itself_is_exempt(tmp_path):
    tree = make_tree(tmp_path, {"agents/foo.md": "# an agent definition\n", "src/a.py": "# foo\n"})
    r = cli(tmp_path, tree=tree, flag_terms=exempt_terms(tmp_path, ["foo"]))
    assert r.returncode == 0 and "exempt as shipped product names: foo" in r.stdout, out(r)


# The shipped-name exemption covers ONE thing: a rule the guard generated from a plain agent id, applied with each occurrence of an exempt
# shipped agent name blanked. It never covers a row of a rules file, whatever its name, nor a corpus id nor an id with another character
# (the SDLC row [SHIPPED-NAME-EXEMPTION-IS-NEUTRALITY-AGENT-ID-ONLY]).
SHIPPED_LONG = {"agents/zorp-quillbot.md": "# an agent definition\n"}


def inside_terms(tmp_path, agents=("zorp-quillbot", "quillbot")):
    """The shipped name `zorp-quillbot` is an exempt id; the shorter id `quillbot` is NOT exempt and is matched inside the name."""
    return exempt_terms(tmp_path, list(agents))


def inside_run(tmp_path, line, mode, vocab=None, deny=None, files=None, agents=("zorp-quillbot", "quillbot")):
    tree = make_tree(tmp_path, {**(SHIPPED_LONG if files is None else files), "src/a.py": line + "\n"})
    extra = ("--require-neutrality-terms",) if mode == "--publication" else ()
    return cli(tmp_path, *extra, tree=tree, flag_terms=inside_terms(tmp_path, agents), mode=mode, vocab=vocab, deny=deny)


@pytest.mark.parametrize("mode", ["--tree", "--publication"])
def test_f2_a_short_id_inside_an_exempt_shipped_name_is_not_a_finding(tmp_path, mode):
    r = inside_run(tmp_path, "# the zorp-quillbot reviews this", mode)
    assert r.returncode == 0, out(r)
    assert "src/a.py:1" not in r.stdout, out(r)


@pytest.mark.parametrize("mode", ["--tree", "--publication"])
@pytest.mark.parametrize("line", [
    "# the quillbot reviews this",                    # the short id alone
    "# the other-quillbot reviews this",              # inside a different, longer name
    "# the x-zorp-quillbot reviews this",             # the exempt name is the tail of a longer name
    "# the zorp-quillbot-2 reviews this",             # the exempt name is the head of a longer name
    "# the zorp-quillbot and the quillbot",           # inside the name AND, on the same line, alone
    "# the \u00e9zorp-quillbot reviews this",          # a non-ASCII letter glued before the name: a longer word, not the name
    "# the zorp-quillbot\u00e9 reviews this",          # the same after it
    "# the ZORP-QUILLBOT reviews this",               # the name in another case is not the name (M7: occurrence found case-insensitively)
    "# the Zorp-Quillbot reviews this",
    "# the zorp-quillbot\u0301 reviews this",         # a combining mark after the name is part of a longer word: not the name
    "# the e\u0301zorp-quillbot reviews this",        # and before it
], ids=["alone", "different-longer", "name-is-tail", "name-is-head", "both-on-one-line", "letter-before", "letter-after",
        "upper-case-name", "title-case-name", "mark-after", "mark-before"])
def test_f2_a_real_use_of_the_short_id_outside_the_exempt_name_is_still_a_finding(tmp_path, mode, line):
    r = inside_run(tmp_path, line, mode)
    assert r.returncode == 1 and "src/a.py:1  [neutrality-agent-id]" in r.stdout, out(r)


@pytest.mark.parametrize("mode", ["--tree", "--publication"])
def test_f2_a_denylist_term_inside_an_agent_file_named_after_a_shipped_agent_is_a_finding(tmp_path, mode):
    """The shipped name is exempt for its own neutrality rule only; a customer term in it is still red (path and line)."""
    deny = T.join([r"marblefoundry", "a marblefoundry order", "a synthetic customer", "deny-customer"]) + "\n"
    files = {"agents/marblefoundry-helper.md": "# an agent definition\n"}
    r = inside_run(tmp_path, "# the marblefoundry-helper agent", mode, deny=deny, files=files, agents=("marblefoundry-helper",))
    assert r.returncode == 1, out(r)
    assert "src/a.py:1  [deny-customer]" in r.stdout, out(r)
    assert re.search(r"agents/\S*-helper\.md:0  \[deny-customer\]", r.stdout), out(r)   # publication masks the matched span of a path


@pytest.mark.parametrize("mode", ["--tree", "--publication"])
def test_f2_a_vocabulary_row_matching_inside_the_shipped_name_is_a_finding(tmp_path, mode):
    vocab = T.join([r"quillbot", "a quillbot thing", "a synthetic vendor id", "vocab-quillbot"]) + "\n"
    r = inside_run(tmp_path, "# the zorp-quillbot reviews this", mode, vocab=vocab)
    assert r.returncode == 1 and "src/a.py:1  [vocab-quillbot]" in r.stdout, out(r)


@pytest.mark.parametrize("mode", ["--tree", "--publication"])
@pytest.mark.parametrize("pattern", [r"quillbot|quillbot cafe", r"quillbot(?: cafe)??", r"quillbot(?: cafe)?", r"quillbot cafe"],
                         ids=["alternation", "lazy", "greedy", "plain"])
def test_f2_a_vocabulary_hit_that_reaches_beyond_the_exempt_name_is_a_finding(tmp_path, mode, pattern):
    vocab = T.join([pattern, "a quillbot cafe", "a synthetic vendor phrase", "vocab-cafe"]) + "\n"
    r = inside_run(tmp_path, "# the zorp-quillbot cafe reviews this", mode, vocab=vocab)
    assert r.returncode == 1 and "src/a.py:1  [vocab-cafe]" in r.stdout, out(r)


@pytest.mark.parametrize("mode", ["--tree", "--publication"])
@pytest.mark.parametrize("kind", ["denylist", "vocabulary"])
def test_f2_a_rules_file_row_named_neutrality_agent_id_is_never_exempt(tmp_path, mode, kind):
    """P1-1: the exemption is keyed on what a rule IS (generated from a terms id), never on its name, which is free text in a rules file."""
    row = T.join([r"marblefoundry", "a marblefoundry order", "a synthetic customer", "neutrality-agent-id"]) + "\n"
    files = {"agents/marblefoundry-helper.md": "# an agent definition\n"}
    r = inside_run(tmp_path, "# the marblefoundry-helper agent", mode, files=files, agents=("marblefoundry-helper",),
                   **{"deny" if kind == "denylist" else "vocab": row})
    assert r.returncode == 1, out(r)
    assert "src/a.py:1  [neutrality-agent-id]" in r.stdout, out(r)
    assert re.search(r"agents/\S*-helper\.md:0  \[neutrality-agent-id\]", r.stdout), out(r)


@pytest.mark.parametrize("mode", ["--tree", "--publication"])
@pytest.mark.parametrize("longer,line", [
    ("zorp-quillbot.v2", "# the zorp-quillbot.v2 agent"),
    ("zorp-quillbot/x", "# see zorp-quillbot/x"),
    ("zorp-quillbot:staging", "# the zorp-quillbot:staging agent"),
    ("zorp-quillbot pro", "# the zorp-quillbot pro agent"),
    ("my.zorp-quillbot", "# the my.zorp-quillbot agent"),
], ids=["dot", "slash", "colon", "space", "dot-before"])
def test_f2_a_longer_generated_id_with_a_character_outside_the_plain_set_is_checked_unmasked(tmp_path, mode, longer, line):
    """P2-1: only a generated id of letters, digits and single hyphens is masked; any other generated id sees the text as it is."""
    r = inside_run(tmp_path, line, mode, agents=("zorp-quillbot", longer))
    assert r.returncode == 1 and "src/a.py:1  [neutrality-agent-id]" in r.stdout, out(r)


def test_f2_which_rules_may_be_masked_is_decided_by_what_the_rule_is():
    g = load_guard()
    gen = lambda ident: g.neutrality_rules(g.NeutralityTerms(True, agent_ids=[ident]))[0][0]
    for ident in ("quillbot", "zorp-quillbot", "QX", "a1-b2"):
        assert g._is_exemptible(gen(ident)), ident
    for ident in ("a.b", "a/b", "a:b", "a b", "my.", "a--b", "-a", "a_b", "\u00e9a", "\u212a"):      # U+212A (Kelvin) must not pass for `k`
        assert not g._is_exemptible(gen(ident)), ident
    assert not g._is_exemptible(g.Rule("neutrality-agent-id", "x", "x", "w", "rules-file"))
    assert not g._is_exemptible(g.Rule("neutrality-agent-id", "x", "x", "w", "t", generated=True))          # generated, but no id behind it
    assert not g._is_exemptible(g.Rule("neutrality-agent-id", "x", "x", "w", "t", ident="quillbot"))          # a plain id, but not generated
    assert not g._is_exemptible(g.Rule("deny-x", "x", "x", "w", "t", generated=True, ident="x"))
    corpus = g.neutrality_rules(g.NeutralityTerms(True, corpus_ids=["zorp"]))[0][0]
    assert not g._is_exemptible(corpus)


def test_f2_the_exemption_is_applied_to_neutrality_agent_id_rules_only():
    """In-process: on one line holding an exempt name, an agent-id rule is blanked inside the name; a denylist-shaped rule and a corpus-id rule are not."""
    g = load_guard()
    rules, _ = g.neutrality_rules(g.NeutralityTerms(True, agent_ids=["quillbot"], corpus_ids=["zorp"]))
    plain = g.Rule("deny-x", r"quillbot", "quillbot", "w", "test")
    named = g.Rule("neutrality-agent-id", r"quillbot", "quillbot", "w", "test")          # a rules-file row that borrows the name
    got = [r.name for r in g._matches_outside(rules + [plain, named], "the zorp-quillbot", ("zorp-quillbot",))]
    assert sorted(got) == ["deny-x", "neutrality-agent-id", "neutrality-corpus-id"] and named in g._matches_outside(rules + [named], "zorp-quillbot", ("zorp-quillbot",)), got
    assert sorted(r.name for r in g._matches_outside(rules + [plain], "the zorp-quillbot", ())) == ["deny-x", "neutrality-agent-id", "neutrality-corpus-id"]


def test_f2_the_exemption_control_is_run_by_the_tree_control_and_goes_red_when_the_exemption_is_inert_or_mute(monkeypatch):
    """P2-1: the selftest's tree-mode control reaches `_matches_outside` through `scan_tree`, so it fails if that path stops exempting or stops matching."""
    g = load_guard()
    rules, _ = g.neutrality_rules(g.NeutralityTerms(True, agent_ids=["zorpbot", "QX"], corpus_ids=["vendel"]))
    assert g._tree_control(rules, echo=None) == []
    lines = []
    g._tree_control(rules, echo=lines.append)
    assert any("exemption control" in x for x in lines), lines
    monkeypatch.setattr(g, "_matches_outside", lambda rs, text, names: g._matches(rs, text))
    inert = g._tree_control(rules, echo=None)
    assert any(x.startswith("EXEMPTION INERT") for x in inert), inert
    monkeypatch.setattr(g, "_matches_outside", lambda rs, text, names: [])
    mute = g._tree_control(rules, echo=None)
    assert any(x.startswith("EXEMPTION MUTE") for x in mute), mute


@pytest.mark.parametrize("name,mutant", [
    ("every rule (M2)", lambda g: (lambda r: True)),
    ("the generated check dropped", lambda g: (lambda r: bool(r.name == g._AGENT_ID_RULE and isinstance(r.ident, str) and g._PLAIN_ID.fullmatch(r.ident)))),
    ("the plain-id check dropped", lambda g: (lambda r: bool(r.generated and r.name == g._AGENT_ID_RULE))),
    ("the name check dropped (M9: a corpus id becomes exemptible)", lambda g: (lambda r: bool(r.generated and isinstance(r.ident, str) and g._PLAIN_ID.fullmatch(r.ident)))),
])
def test_f2_the_selftest_control_goes_red_when_the_exemption_is_too_wide(monkeypatch, name, mutant):
    g = load_guard()
    rules, _ = g.neutrality_rules(g.NeutralityTerms(True, agent_ids=["zorpbot"], corpus_ids=["vendel"]))
    assert g._tree_control(rules, echo=None) == []
    monkeypatch.setattr(g, "_is_exemptible", mutant(g))
    red = g._tree_control(rules, echo=None)
    assert any(x.startswith("EXEMPTION TOO WIDE") for x in red), (name, red)


def test_f2_a_corpus_id_is_never_exempt(tmp_path):
    tree = make_tree(tmp_path, {**SHIPPED, "src/a.py": "# see alpha-agent\n"})
    r = cli(tmp_path, tree=tree, flag_terms=exempt_terms(tmp_path, ["zorpbot"], corpora=["alpha-agent"]))
    assert r.returncode == 1 and "src/a.py:1  [neutrality-corpus-id]" in r.stdout, out(r)


def test_f2_the_exemption_reads_the_scanned_tree_not_a_configuration(tmp_path):
    """The same terms file and the same text: exempt in a tree that ships the definition, refused in one that does not."""
    terms = exempt_terms(tmp_path, ["alpha-agent"])
    shipping = make_tree(tmp_path / "a", {**SHIPPED, "src/a.py": "# alpha-agent\n"})
    bare = make_tree(tmp_path / "b", {"src/a.py": "# alpha-agent\n"})
    assert cli(tmp_path / "a", tree=shipping, flag_terms=terms).returncode == 0
    assert cli(tmp_path / "b", tree=bare, flag_terms=terms).returncode == 1


def test_f2_publication_records_the_exempt_names_in_the_report_and_the_scope_and_still_runs_every_control(tmp_path):
    rep = tmp_path / "rep.json"
    tree = snapshot_tree(tmp_path, {**SHIPPED, "src/a.py": "# alpha-agent\n"})
    r = cli(
        tmp_path,
        "--require-neutrality-terms",
        "--report",
        str(rep),
        tree=tree,
        mode="--publication",
        flag_terms=exempt_terms(tmp_path, ["alpha-agent", "zorpbot"]),
    )
    assert r.returncode == 0 and "RESULT: green" in r.stdout, out(r)
    assert "neutrality terms: 1 generated agent id(s) exempt as shipped product names: alpha-agent" in r.stdout
    scope = next(ln for ln in r.stdout.splitlines() if ln.startswith("publication scope:"))
    assert "exempt-agent-ids=1" in scope and "zorpbot" not in scope, scope
    assert json.loads(rep.read_text(encoding="utf-8"))["scope"]["neutrality_terms"]["exempt_agent_ids"] == ["alpha-agent"]
    assert "zorpbot" not in out(r) + rep.read_text(encoding="utf-8")


def test_f2_publication_still_refuses_an_id_whose_definition_is_excluded(tmp_path):
    tree = snapshot_tree(tmp_path, {**SHIPPED, "src/a.py": "# alpha-agent\n"})
    (tree / "PUBLISH-EXCLUDE").write_text("agents/alpha-agent.md   # an internal definition\n", encoding="utf-8")
    r = cli(tmp_path, "--require-neutrality-terms", tree=tree, mode="--publication", flag_terms=exempt_terms(tmp_path, ["alpha-agent"]))
    assert r.returncode == 1 and "[neutrality-agent-id]" in r.stdout, out(r)


def file_sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def test_f4_a_file_that_hashes_differently_from_the_validated_one_is_refused(tmp_path):
    """The swap: validate file A, replace it by a valid fresh file B with fewer ids, then run the guard told A's sha256."""
    tree = snapshot_tree(tmp_path, {"src/leak.py": "# the zorpbot wrote this\n"})
    a = good_file(tmp_path)
    expected = file_sha(a)
    write(tmp_path / "terms.json", terms_doc(agents=["otherbot"], corpora=["othercorp"]))
    r = cli(
        tmp_path,
        "--require-neutrality-terms",
        "--expect-neutrality-terms-sha256",
        expected,
        tree=tree,
        mode="--publication",
        flag_terms=str(tmp_path / "terms.json"),
    )
    assert r.returncode == 1 and "file-changed" in r.stdout and "RESULT: REFUSED" in r.stdout, out(r)
    assert "RESULT: green" not in r.stdout


def test_f4_the_matching_sha256_passes_and_the_planted_id_is_found(tmp_path):
    tree = snapshot_tree(tmp_path, {"src/leak.py": "# the zorpbot wrote this\n"})
    a = good_file(tmp_path)
    r = cli(
        tmp_path,
        "--require-neutrality-terms",
        "--expect-neutrality-terms-sha256",
        file_sha(a),
        tree=tree,
        mode="--publication",
        flag_terms=a,
    )
    assert r.returncode == 1 and "[neutrality-agent-id]" in r.stdout and "file-changed" not in r.stdout, out(r)


def test_f4_the_reader_names_the_cause_and_a_changed_contexts_table_is_refused_too(tmp_path):
    g = load_guard()
    f = good_file(tmp_path)
    res = g.load_neutrality_terms(f, now=NOW, expect_sha256="0" * 64)
    assert (res.ok, res.cause) == (False, "file-changed") and "file-changed" in g.NEUTRALITY_CAUSES
    assert g.load_neutrality_terms(f, now=NOW, expect_sha256=file_sha(f)).ok
    ctx = write(tmp_path / "ctx.json", CTX)
    contexts, problem, sha = g.load_neutrality_contexts(ctx, expect_sha256="0" * 64)
    assert contexts is None and problem and "validated" in problem
    assert g.load_neutrality_contexts(ctx, expect_sha256=file_sha(ctx))[0] is not None


def test_f8_a_selftest_with_a_named_but_unusable_file_is_partial_never_a_configured_green(tmp_path):
    r = cli(tmp_path, mode="--selftest", flag_terms=good_file(tmp_path, generated_at="2020-01-01T00:00:00Z"))
    assert r.returncode == 2, out(r)
    assert "selftest (PARTIAL)" in r.stdout and "UNCONFIGURED" in r.stdout, out(r)
    assert "The matchers demonstrably RAN" not in r.stdout and "✓ selftest" not in r.stdout, out(r)


def test_f8_a_selftest_with_a_good_file_is_still_a_configured_green(tmp_path):
    r = cli(tmp_path, mode="--selftest", flag_terms=good_file(tmp_path))
    assert r.returncode == 0 and "✓ selftest" in r.stdout, out(r)


def test_f9_the_shared_fixture_is_the_drift_alarm_and_the_guard_comment_says_so():
    text = " ".join(GUARD.read_text(encoding="utf-8").split())
    assert "which is the drift alarm" in text and FIXTURE_SOURCE_COMMIT in text
    assert "provisional" not in text.lower()


# =========================================================================================================
# only a REGULAR file directly under agents/ may exempt its stem (no link of any kind, no nesting, no stripping)
# =========================================================================================================
def _link_to_outside_file(tree, tmp_path):
    (tmp_path / "outside").mkdir(exist_ok=True)
    (tmp_path / "outside" / "x.md").write_text("# a definition elsewhere\n", encoding="utf-8")
    (tree / "agents").mkdir(exist_ok=True)
    (tree / "agents" / "alpha-agent.md").symlink_to(tmp_path / "outside" / "x.md")


def _link_dangling(tree, tmp_path):
    (tree / "agents").mkdir(exist_ok=True)
    (tree / "agents" / "alpha-agent.md").symlink_to(tmp_path / "outside" / "gone.md")


def _link_to_directory(tree, tmp_path):
    (tmp_path / "outside" / "somedir").mkdir(parents=True, exist_ok=True)
    (tree / "agents").mkdir(exist_ok=True)
    (tree / "agents" / "alpha-agent.md").symlink_to(tmp_path / "outside" / "somedir")


def _link_to_file_inside_the_tree(tree, tmp_path):
    (tree / "docs").mkdir(exist_ok=True)
    (tree / "docs" / "def.md").write_text("# a definition\n", encoding="utf-8")
    (tree / "agents").mkdir(exist_ok=True)
    (tree / "agents" / "alpha-agent.md").symlink_to(tree / "docs" / "def.md")


def _agents_directory_is_a_link(tree, tmp_path):
    (tmp_path / "outside" / "real-agents").mkdir(parents=True, exist_ok=True)
    (tmp_path / "outside" / "real-agents" / "alpha-agent.md").write_text("# a definition\n", encoding="utf-8")
    (tree / "agents").symlink_to(tmp_path / "outside" / "real-agents")


LINK_KINDS = [_link_to_outside_file, _link_dangling, _link_to_directory, _link_to_file_inside_the_tree, _agents_directory_is_a_link]


@pytest.mark.parametrize("build", LINK_KINDS, ids=lambda f: f.__name__.lstrip("_"))
def test_fa_a_link_of_any_kind_exempts_nothing_in_tree_mode(tmp_path, build):
    tree = make_tree(tmp_path, {"src/a.py": "# the alpha-agent reviews this\n"})
    build(tree, tmp_path)
    r = cli(tmp_path, tree=tree, flag_terms=exempt_terms(tmp_path, ["alpha-agent"]))
    assert r.returncode == 1 and "src/a.py:1  [neutrality-agent-id]" in r.stdout, out(r)
    assert "exempt as shipped product names: alpha-agent" not in out(r)


@pytest.mark.parametrize("build", LINK_KINDS, ids=lambda f: f.__name__.lstrip("_"))
def test_fa_a_link_of_any_kind_exempts_nothing_in_publication_mode(tmp_path, build):
    rep = tmp_path / "rep.json"
    tree = snapshot_tree(tmp_path, {"src/a.py": "# the alpha-agent reviews this\n"})
    build(tree, tmp_path)
    r = cli(
        tmp_path,
        "--require-neutrality-terms",
        "--report",
        str(rep),
        tree=tree,
        mode="--publication",
        flag_terms=exempt_terms(tmp_path, ["alpha-agent"]),
    )
    assert r.returncode == 1 and "[neutrality-agent-id]" in r.stdout, out(r)
    assert json.loads(rep.read_text(encoding="utf-8"))["scope"]["neutrality_terms"]["exempt_agent_ids"] == []


def test_fa_control_a_regular_file_directly_under_agents_exempts_in_both_modes(tmp_path):
    tree = snapshot_tree(tmp_path, {**SHIPPED, "src/a.py": "# the alpha-agent reviews this\n"})
    terms = exempt_terms(tmp_path, ["alpha-agent"])
    assert cli(tmp_path, tree=tree, flag_terms=terms).returncode == 0
    r = cli(tmp_path, "--require-neutrality-terms", tree=tree, mode="--publication", flag_terms=terms)
    assert r.returncode == 0 and "exempt as shipped product names: alpha-agent" in r.stdout, out(r)


def test_fb_a_definition_nested_below_agents_exempts_nothing(tmp_path):
    tree = snapshot_tree(tmp_path, {"agents/sub/xyzw.md": "# a definition\n", "src/a.py": "# the xyzw reviews this\n"})
    terms = exempt_terms(tmp_path, ["xyzw"])
    assert cli(tmp_path, tree=tree, flag_terms=terms).returncode == 1
    assert cli(tmp_path, "--require-neutrality-terms", tree=tree, mode="--publication", flag_terms=terms).returncode == 1


def test_fb_a_stem_with_a_trailing_space_is_not_the_stem_without_it(tmp_path):
    """agents/spacey .md has the stem `spacey ` (with the space): the id `spacey` is not exempt."""
    tree = snapshot_tree(tmp_path, {"agents/spacey .md": "# a definition\n", "src/a.py": "# the spacey reviews this\n"})
    terms = exempt_terms(tmp_path, ["spacey"])
    assert cli(tmp_path, tree=tree, flag_terms=terms).returncode == 1
    assert cli(tmp_path, "--require-neutrality-terms", tree=tree, mode="--publication", flag_terms=terms).returncode == 1


def _staged(tmp_path, link):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "agents").mkdir()
    (repo / "src" / "a.py").write_text("# the alpha-agent reviews this\n", encoding="utf-8")
    if link:
        (tmp_path / "def.md").write_text("# a definition elsewhere\n", encoding="utf-8")
        (repo / "agents" / "alpha-agent.md").symlink_to(tmp_path / "def.md")
    else:
        (repo / "agents" / "alpha-agent.md").write_text("# a definition\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "FACTLATTICE_NEUTRALITY"))}
    env.update({"HOME": str(tmp_path / "home"), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
    (tmp_path / "home").mkdir(exist_ok=True)
    priv = tmp_path / "priv"
    priv.mkdir(exist_ok=True)
    (priv / "vocab.tsv").write_text(VOCAB, encoding="utf-8")
    (priv / "deny.tsv").write_text(DENY, encoding="utf-8")
    env["FACTLATTICE_PUBLISH_VOCAB"], env["FACTLATTICE_DISCLOSURE_RULES"] = str(priv / "vocab.tsv"), str(priv / "deny.tsv")
    for argv in (["init", "-q"], ["add", "-A"]):
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *argv], cwd=str(repo), env=env, check=True, timeout=60, capture_output=True
        )
    return subprocess.run(
        [sys.executable, str(GUARD), "--neutrality-terms", exempt_terms(tmp_path, ["alpha-agent"])],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_fa_the_staged_check_exempts_a_regular_definition_and_not_a_link(tmp_path):
    regular = _staged(tmp_path / "r", link=False)
    assert regular.returncode == 0 and "exempt as shipped product names: alpha-agent" in regular.stdout, out(regular)
    linked = _staged(tmp_path / "l", link=True)
    assert linked.returncode == 1 and "[neutrality-agent-id]" in linked.stdout, out(linked)


def _git_repo_whose_agents_folder_became_a_link(tmp_path):
    """The index holds agents/alpha-agent.md as a regular file; the working copy's agents/ is then replaced by a LINK to a real folder
    that also holds a regular alpha-agent.md. Only the 'agents/ must be a real directory' check tells this from a shipped definition."""
    repo = tmp_path / "repo"
    (repo / "agents").mkdir(parents=True)
    (repo / "agents" / "alpha-agent.md").write_text("# a definition\n", encoding="utf-8")
    (repo / "notes.txt").write_text("# the alpha-agent reviews this\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "FACTLATTICE_NEUTRALITY"))}
    (tmp_path / "home").mkdir(exist_ok=True)
    env.update({"HOME": str(tmp_path / "home"), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
    for argv in (["init", "-q"], ["add", "-A"]):
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *argv], cwd=str(repo), env=env, check=True, timeout=60, capture_output=True
        )
    (repo / "agents").rename(tmp_path / "agents-moved-aside")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "alpha-agent.md").write_text("# a definition elsewhere\n", encoding="utf-8")
    (repo / "agents").symlink_to(tmp_path / "elsewhere")
    return repo, env


def test_fi_an_agents_folder_that_is_a_link_exempts_nothing_in_the_git_index_modes(tmp_path):
    repo, env = _git_repo_whose_agents_folder_became_a_link(tmp_path)
    terms = exempt_terms(tmp_path, ["alpha-agent"])
    priv = tmp_path / "priv"
    priv.mkdir(exist_ok=True)
    (priv / "vocab.tsv").write_text(VOCAB, encoding="utf-8")
    (priv / "deny.tsv").write_text(DENY, encoding="utf-8")
    env["FACTLATTICE_PUBLISH_VOCAB"], env["FACTLATTICE_DISCLOSURE_RULES"] = str(priv / "vocab.tsv"), str(priv / "deny.tsv")
    staged = subprocess.run(
        [sys.executable, str(GUARD), "--neutrality-terms", terms], cwd=str(repo), env=env, capture_output=True, text=True, timeout=120
    )
    assert staged.returncode == 1 and "[neutrality-agent-id]" in staged.stdout, out(staged)
    assert "exempt as shipped product names: alpha-agent" not in out(staged)
    tree = subprocess.run(
        [sys.executable, str(GUARD), "--tree", str(repo), "--neutrality-terms", terms],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert tree.returncode == 1 and "notes.txt:1  [neutrality-agent-id]" in tree.stdout, out(tree)
    assert "exempt as shipped product names: alpha-agent" not in out(tree)


# =========================================================================================================
# a generated corpus id is exempt if and only if the corpus SHIPS in the scanned tree (derived, never listed) and is not private
# =========================================================================================================
CID = "shipcorp"
NOTE_NONE = "corpus exemption: no registry; 0 corpus ids exempt"


def corpus_world(tmp_path, spec=None, cid=CID, reg_rel="trunk/private/corpus-config.json", root_rel="trunk/corpora/shipcorp", extra=None):
    """A synthetic registry under tmp_path/trunk/private and the env that makes the guard read it. `root_rel` is the registry's root,
    relative to tmp_path. -> env_extra."""
    reg = tmp_path / reg_rel
    reg.parent.mkdir(parents=True, exist_ok=True)
    entry = {"roots": [{"path": str(tmp_path / root_rel), "area": "specs"}], "domain_suffix": "x"}
    entry.update(spec or {})
    doc = {cid: entry}
    doc.update(extra or {})
    reg.write_text(json.dumps(doc), encoding="utf-8")
    return {"FACTLATTICE_CORPUS_CONFIG": str(reg)}


def corpus_terms(tmp_path, corpora=(CID,)):
    return write(tmp_path / "t.json", terms_doc(agents=["zorpbot"], corpora=sorted(corpora)))


HIT = {"src/a.py": f"# see {CID} here\n"}


def corpus_run(tmp_path, files, env, guard=None, mode="--tree", extra_args=(), corpora=(CID,)):
    tree = make_tree(tmp_path, {**HIT, **files})
    return cli(tmp_path, *extra_args, tree=tree, flag_terms=corpus_terms(tmp_path, corpora), env_extra=env, mode=mode, guard=guard)


def exempted(r):
    return r.returncode == 0 and f"1 generated corpus id(s) exempt as shipped corpora: {CID}" in r.stdout


def stays_red(r):
    return r.returncode == 1 and "src/a.py:1  [neutrality-corpus-id]" in r.stdout and "0 generated corpus id(s) exempt" in r.stdout


def test_c1_a_shipped_regular_directory_with_an_lts_file_exempts_the_corpus_and_names_it(tmp_path):
    r = corpus_run(tmp_path, {"corpora/shipcorp/a.lts": "# a corpus file\n"}, corpus_world(tmp_path))
    assert exempted(r), out(r)


def test_c1_the_exemption_holds_in_publication_mode_and_the_report_and_scope_name_it(tmp_path):
    rep = tmp_path / "rep.json"
    r = corpus_run(tmp_path, {"corpora/shipcorp/a.lts": "# a corpus file\n"}, corpus_world(tmp_path), mode="--publication", extra_args=("--report", str(rep)))
    assert r.returncode == 0 and f"1 generated corpus id(s) exempt as shipped corpora: {CID}" in r.stdout, out(r)
    scope = json.loads(rep.read_text(encoding="utf-8"))["scope"]["neutrality_terms"]
    assert scope["exempt_corpus_ids"] == [CID] and scope["corpus_exemption_note"] is None, scope
    assert "exempt-corpus-ids=1" in json.loads(rep.read_text(encoding="utf-8"))["scope_line"]


def test_c1_a_root_pinned_by_only_file_ships_only_when_that_file_ships(tmp_path):
    env = corpus_world(tmp_path, spec={"roots": [{"path": str(tmp_path / "trunk/corpora/shipcorp"), "area": "specs", "only_file": "one.lts"}]})
    assert exempted(corpus_run(tmp_path, {"corpora/shipcorp/one.lts": "# one\n"}, env))


def test_c1_an_only_file_root_whose_file_is_absent_exempts_nothing(tmp_path):
    env = corpus_world(tmp_path, spec={"roots": [{"path": str(tmp_path / "trunk/corpora/shipcorp"), "area": "specs", "only_file": "one.lts"}]})
    assert stays_red(corpus_run(tmp_path, {"corpora/shipcorp/other.lts": "# not the pinned file\n"}, env))


def _link_dir(tmp_path):
    (tmp_path / "real").mkdir(exist_ok=True)
    (tmp_path / "real" / "a.lts").write_text("# a corpus file\n", encoding="utf-8")
    (tmp_path / "tree" / "corpora").mkdir(parents=True, exist_ok=True)
    os.symlink(tmp_path / "real", tmp_path / "tree" / "corpora" / "shipcorp")


def test_c2_a_link_where_the_corpus_directory_should_be_exempts_nothing(tmp_path):
    make_tree(tmp_path)
    _link_dir(tmp_path)
    r = cli(tmp_path, tree=make_tree(tmp_path, HIT), flag_terms=corpus_terms(tmp_path), env_extra=corpus_world(tmp_path))
    assert stays_red(r), out(r)


def _link_file(tmp_path):
    (tmp_path / "elsewhere.lts").write_text("# not in the tree\n", encoding="utf-8")
    d = tmp_path / "tree" / "corpora" / "shipcorp"
    d.mkdir(parents=True, exist_ok=True)
    os.symlink(tmp_path / "elsewhere.lts", d / "a.lts")


def test_c2_a_directory_whose_only_lts_is_a_link_exempts_nothing(tmp_path):
    make_tree(tmp_path)
    _link_file(tmp_path)
    r = cli(tmp_path, tree=make_tree(tmp_path, HIT), flag_terms=corpus_terms(tmp_path), env_extra=corpus_world(tmp_path))
    assert stays_red(r), out(r)


def test_c2_a_publish_excluded_corpus_file_exempts_nothing(tmp_path):
    r = corpus_run(tmp_path, {"corpora/shipcorp/a.lts": "# a\n", "PUBLISH-EXCLUDE": "corpora/shipcorp/a.lts   # kept home\n"}, corpus_world(tmp_path))
    assert stays_red(r), out(r)


def test_c2_a_publish_excluded_directory_exempts_nothing(tmp_path):
    r = corpus_run(tmp_path, {"corpora/shipcorp/a.lts": "# a\n", "PUBLISH-EXCLUDE": "corpora/shipcorp/   # kept home\n"}, corpus_world(tmp_path))
    assert stays_red(r), out(r)


def test_c2_an_exclude_entry_naming_the_directory_itself_exempts_nothing(tmp_path):
    """An entry without a trailing slash names exactly one path; the directory's own name is conservative: fail closed."""
    r = corpus_run(tmp_path, {"corpora/shipcorp/a.lts": "# a\n", "PUBLISH-EXCLUDE": "corpora/shipcorp   # kept home\n"}, corpus_world(tmp_path))
    assert stays_red(r), out(r)


def test_c2_a_root_outside_the_trunk_exempts_nothing_even_when_a_same_named_path_ships(tmp_path):
    env = corpus_world(tmp_path, root_rel="other/corpora/shipcorp")
    r = corpus_run(tmp_path, {"corpora/shipcorp/a.lts": "# a\n", "other/corpora/shipcorp/a.lts": "# a\n"}, env)
    assert stays_red(r), out(r)


def test_c2_an_empty_corpus_directory_exempts_nothing(tmp_path):
    """No `.lts` under the path (a directory with only another kind of file, and a missing one): no exemption."""
    r = corpus_run(tmp_path, {"corpora/shipcorp/readme.txt": "text\n"}, corpus_world(tmp_path))
    assert stays_red(r), out(r)
    r = cli(tmp_path, tree=make_tree(tmp_path, HIT), flag_terms=corpus_terms(tmp_path), env_extra=corpus_world(tmp_path))
    assert stays_red(r), out(r)


@pytest.mark.parametrize("spec,cid", [({"private": True}, CID), ({}, CID + "-personality")], ids=["flag", "id-suffix"])
def test_c2_a_private_corpus_exempts_nothing(tmp_path, spec, cid):
    env = corpus_world(tmp_path, spec=spec, cid=cid)
    tree = make_tree(tmp_path, {"src/a.py": f"# see {cid} here\n", "corpora/shipcorp/a.lts": "# a\n"})
    r = cli(tmp_path, tree=tree, flag_terms=corpus_terms(tmp_path, (cid,)), env_extra=env)
    assert r.returncode == 1 and "[neutrality-corpus-id]" in r.stdout and "0 generated corpus id(s) exempt" in r.stdout, out(r)


def test_c3_no_registry_exempts_nothing_and_one_line_says_why(tmp_path):
    r = corpus_run(tmp_path, {"corpora/shipcorp/a.lts": "# a\n"}, {})
    assert r.returncode == 1 and "src/a.py:1  [neutrality-corpus-id]" in r.stdout and NOTE_NONE in r.stdout, out(r)
    assert r.stdout.count("corpus exemption:") == 1


def test_c3_an_unreadable_registry_exempts_nothing_and_says_so(tmp_path):
    bad = tmp_path / "trunk" / "private" / "corpus-config.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{not json", encoding="utf-8")
    r = corpus_run(tmp_path, {"corpora/shipcorp/a.lts": "# a\n"}, {"FACTLATTICE_CORPUS_CONFIG": str(bad)})
    assert r.returncode == 1 and "corpus exemption: registry unreadable (RegistryError); 0 corpus ids exempt" in r.stdout, out(r)


def test_c3_an_id_the_registry_does_not_hold_exempts_nothing(tmp_path):
    env = corpus_world(tmp_path, cid="othercorp")
    r = corpus_run(tmp_path, {"corpora/shipcorp/a.lts": "# a\n"}, env)
    assert stays_red(r), out(r)


def test_c3_a_registry_outside_a_trunks_private_folder_names_no_trunk_and_exempts_nothing(tmp_path):
    env = corpus_world(tmp_path, reg_rel="trunk/cfg/corpus-config.json", root_rel="trunk/cfg/corpora/shipcorp")
    r = corpus_run(tmp_path, {"cfg/corpora/shipcorp/a.lts": "# a\n"}, env)
    assert r.returncode == 1 and "[neutrality-corpus-id]" in r.stdout and "corpus exemption:" in r.stdout and "0 corpus ids exempt" in r.stdout, out(r)


def test_c4_an_agent_only_exemption_is_unchanged_beside_an_unshipped_corpus(tmp_path):
    env = corpus_world(tmp_path)
    tree = make_tree(tmp_path, {**SHIPPED, "src/a.py": "# the alpha-agent reviews this\n# see shipcorp\n"})
    r = cli(tmp_path, tree=tree, flag_terms=write(tmp_path / "t.json", terms_doc(agents=["alpha-agent"], corpora=[CID])), env_extra=env)
    assert "1 generated agent id(s) exempt as shipped product names: alpha-agent" in r.stdout, out(r)
    assert r.returncode == 1 and "src/a.py:2  [neutrality-corpus-id]" in r.stdout and "src/a.py:1" not in r.stdout, out(r)


def test_c4_only_the_shipped_one_of_two_corpus_ids_is_exempt_and_the_other_is_not_printed(tmp_path):
    env = corpus_world(tmp_path, extra={"hiddencorp": {"roots": [{"path": str(tmp_path / "trunk/corpora/hiddencorp"), "area": "specs"}], "domain_suffix": "h"}})
    tree = make_tree(tmp_path, {"src/a.py": "# shipcorp and hiddencorp\n", "corpora/shipcorp/a.lts": "# a\n"})
    r = cli(tmp_path, tree=tree, flag_terms=corpus_terms(tmp_path, (CID, "hiddencorp")), env_extra=env)
    assert r.returncode == 1 and f"1 generated corpus id(s) exempt as shipped corpora: {CID}" in r.stdout, out(r)
    assert "hiddencorp" not in r.stdout.split("RESULT")[0].split("src/a.py")[0], "an id that is not exempt is never printed"


# --- one mutant per condition: the same scenarios, against a copy of the guard with that one condition broken, must go the OTHER way ----
def _mutant(tmp_path, old, new):
    src = GUARD.read_text(encoding="utf-8")
    assert src.count(old) == 1, f"mutation anchor not found exactly once: {old!r}"
    home = tmp_path / "mut"
    (home / "tools").mkdir(parents=True)
    os.symlink(ROOT / "factlattice", home / "factlattice")
    os.symlink(ROOT / "factlattice_engine", home / "factlattice_engine")
    path = home / "tools" / "disclosure_guard.py"
    path.write_text(src.replace(old, new), encoding="utf-8")
    return path


def _pin_spec(tmp_path, pin):
    return corpus_world(tmp_path, spec={"roots": [{"path": str(tmp_path / "trunk/corpora/shipcorp"), "area": "specs", "only_file": pin}]})


def _sc_file_link(tmp_path):
    make_tree(tmp_path)
    _link_file(tmp_path)
    return {}, corpus_world(tmp_path), True


def _sc_pin_link(tmp_path):
    make_tree(tmp_path)
    (tmp_path / "elsewhere.lts").write_text("# elsewhere\n", encoding="utf-8")
    (tmp_path / "tree" / "corpora" / "shipcorp").mkdir(parents=True, exist_ok=True)
    os.symlink(tmp_path / "elsewhere.lts", tmp_path / "tree" / "corpora" / "shipcorp" / "one.lts")
    return {}, _pin_spec(tmp_path, "one.lts"), True


def _sc_private_folder_link(tmp_path):
    real = tmp_path / "trunk" / "overlay"
    real.mkdir(parents=True)
    os.symlink(real, tmp_path / "trunk" / "private")
    return {"corpora/shipcorp/a.lts": "# a\n"}, corpus_world(tmp_path), True


def _sc_unreadable(tmp_path):
    bad = tmp_path / "trunk" / "private" / "corpus-config.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{x", encoding="utf-8")
    return {"corpora/shipcorp/a.lts": "# a\n"}, {"FACTLATTICE_CORPUS_CONFIG": str(bad)}, True


def _sc_two_roots(tmp_path):
    roots = [{"path": str(tmp_path / "trunk/corpora/shipcorp"), "area": "specs"}, {"path": str(tmp_path / "trunk/corpora/second"), "area": "specs"}]
    return {"corpora/shipcorp/a.lts": "# a\n"}, corpus_world(tmp_path, spec={"roots": roots}), True


A_LTS = {"corpora/shipcorp/a.lts": "# a\n"}
# name -> (files, env builder). A builder takes tmp_path; `files` may be a function of tmp_path for the scenarios that make their own tree.
SCENARIOS = {
    "file-link": _sc_file_link,
    "pin-link": _sc_pin_link,
    "private-folder-link": _sc_private_folder_link,
    "unreadable": _sc_unreadable,
    "two-roots": _sc_two_roots,
    "excluded-file": lambda t: ({**A_LTS, "PUBLISH-EXCLUDE": "corpora/shipcorp/a.lts   # kept home\n"}, corpus_world(t), True),
    "excluded-dir-name": lambda t: ({**A_LTS, "PUBLISH-EXCLUDE": "corpora/shipcorp   # kept home\n"}, corpus_world(t), True),
    "outside-trunk": lambda t: ({**A_LTS, "other/corpora/shipcorp/a.lts": "# a\n"}, corpus_world(t, root_rel="other/corpora/shipcorp"), True),
    "no-lts": lambda t: ({"corpora/shipcorp/readme.txt": "text\n"}, corpus_world(t), True),
    "private": lambda t: (A_LTS, corpus_world(t, spec={"private": True}), True),
    "no-registry": lambda t: (A_LTS, {}, True),
    "empty-roots": lambda t: (A_LTS, corpus_world(t, spec={"roots": []}), True),
    "typo-private": lambda t: (A_LTS, corpus_world(t, spec={"privat": True}), True),
    "pin-not-lts": lambda t: ({"corpora/shipcorp/one.txt": "# one\n"}, _pin_spec(t, "one.txt"), True),
    "pin-excluded": lambda t: ({"corpora/shipcorp/one.lts": "# one\n", "PUBLISH-EXCLUDE": "corpora/shipcorp/one.lts   # kept home\n"}, _pin_spec(t, "one.lts"), True),
    # a malformed entry SHAPE the engine refuses (check_entry_shape): the only thing between each of these and an exemption (the files ship, the
    # entry is not private, carries no unknown key)
    "shape-only-file": lambda t: ({**A_LTS}, corpus_world(t, spec={"roots": [{"path": str(t / "trunk/corpora/shipcorp"), "area": "specs", "only_file": 5}]}), True),
    "shape-domain-suffix": lambda t: ({**A_LTS}, corpus_world(t, spec={"domain_suffix": 5}), True),
    "shape-area": lambda t: ({**A_LTS}, corpus_world(t, spec={"roots": [{"path": str(t / "trunk/corpora/shipcorp"), "area": ["x"]}]}), True),
    "pin-slash": lambda t: ({"corpora/shipcorp/sub/one.lts": "# one\n"}, _pin_spec(t, "sub/one.lts"), True),
    "no-trunk": lambda t: ({"cfg/corpora/shipcorp/a.lts": "# a\n"}, corpus_world(t, reg_rel="trunk/cfg/corpus-config.json", root_rel="trunk/cfg/corpora/shipcorp"), True),
}


def _scenario(tmp_path, name, guard):
    """The same scenario as the test of that name above, run with `guard`. -> CompletedProcess."""
    files, env, _ = SCENARIOS[name](tmp_path)
    return corpus_run(tmp_path, files, env, guard=guard)


MUTANTS = [
    ("shape-only-file", "config.check_entry_shape(spec)        # raises on a malformed entry", "pass"),
    ("two-roots", "ships = bool(roots) and all(_root_ships(", "ships = bool(roots) and any(_root_ships("),
    ("empty-roots", "ships = bool(roots) and all(", "ships = all("),
    ("pin-not-lts", 'target in set(files) and target.endswith(".lts") and not is_excluded', "target in set(files) and not is_excluded"),
    ("pin-excluded", "and not is_excluded(target, excl)", "and True"),
    ("pin-link", "and _is_regular(scanned, target))", "and True)"),
    ("pin-slash", 'if "/" in only or only in (".", ".."):', 'if only in (".", ".."):'),
    ("typo-private", "if config.registry_key_failure(cid, spec) is not None or registry.named_failure(spec) is not None:", "if False:"),
    ("private-folder-link", "if _private_folder_is_link(choice.path):", "if False:"),
    ("file-link", "return stat.S_ISREG(os.lstat(os.path.join(scanned, rel)).st_mode)", "return os.path.isfile(os.path.join(scanned, rel))"),
    ("excluded-file", "and f.endswith(\".lts\") and not is_excluded(f, excl) and _is_regular(scanned, f)", "and f.endswith(\".lts\") and _is_regular(scanned, f)"),
    ("excluded-dir-name", "if is_excluded(rel, excl) or is_excluded(rel + \"/\", excl) or not _real_path_inside(scanned, rel):", "if not _real_path_inside(scanned, rel):"),
    ("outside-trunk", "rel = os.path.relpath(os.path.normpath(os.path.abspath(path)), trunk)", "rel = os.path.relpath(os.path.normpath(os.path.abspath(path)), trunk).replace(\"../\", \"\")"),
    ("no-lts", "return any(f.startswith(rel + \"/\") and f.endswith(\".lts\")", "return any(f.startswith(rel + \"/\")"),
    ("private", "or private_reason(cid, spec) is not None:", "or False:"),
    ("no-registry", "return [], \"no registry\"", "return sorted(set(corpus_ids)), None"),
    ("unreadable", "return [], \"registry unreadable (\" + type(exc).__name__ + \")\"", "return sorted(set(corpus_ids)), None"),
    ("no-trunk", "if os.path.basename(d) == \"private\" else None", "if True else None"),
]


@pytest.mark.parametrize("name,old,new", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_c5_each_condition_has_a_mutant_that_goes_the_other_way(tmp_path, name, old, new):
    """The real guard keeps the id red in this scenario (the tests above); the guard with ONLY this condition broken exempts it."""
    if os.environ.get("DG_GUARD_UNDER_TEST"):
        pytest.skip("the mutants are made from this checkout's guard")
    (tmp_path / "real").mkdir()
    real = _scenario(tmp_path / "real", name, GUARD)
    assert real.returncode == 1 and "src/a.py:1  [neutrality-corpus-id]" in real.stdout, "the scenario is not red on the real guard\n" + out(real)
    (tmp_path / "m").mkdir()
    mut = _scenario(tmp_path / "m", name, _mutant(tmp_path / "m", old, new))
    assert mut.returncode == 0, f"the mutant of condition {name!r} did not change the outcome: nothing watches this condition\n" + out(mut)


def _dir_link_verdict(tmp_path, guard):
    """The path check in isolation (a file list that names a `.lts` beneath a LINKED directory, which a real listing never does, so the
    check is a second line of defence): -> whether the root ships."""
    spec = importlib.util.spec_from_file_location("dg_dirlink_" + guard.parent.parent.name, str(guard))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    make_tree(tmp_path)
    _link_dir(tmp_path)
    return mod._root_ships({"path": str(tmp_path / "trunk/corpora/shipcorp")}, str(tmp_path / "trunk"),
                           ["corpora/shipcorp/a.lts"], [], str(tmp_path / "tree"))


def test_c2_the_path_check_refuses_a_linked_directory_even_when_the_listing_names_a_file_beneath_it(tmp_path):
    assert _dir_link_verdict(tmp_path, GUARD) is False


def test_c5_the_linked_directory_check_has_a_mutant_that_goes_the_other_way(tmp_path):
    if os.environ.get("DG_GUARD_UNDER_TEST"):
        pytest.skip("the mutants are made from this checkout's guard")
    (tmp_path / "m").mkdir()
    mut = _mutant(tmp_path / "m", "if not stat.S_ISDIR(os.lstat(cur).st_mode):", "if not os.path.isdir(cur):")
    assert _dir_link_verdict(tmp_path / "m", mut) is True


# =========================================================================================================
# The optional `word_ids` key, the short-id rule, and the shared case fixture that pins the matcher
# =========================================================================================================
# The fixture is copied BYTE FOR BYTE from the writer's repository (merge commit below, path tests/data/term_regex_cases.json).
# Its owner is the writer's repository; it holds fake ids only. A change to it is a change to the contract, so the pin is its sha256.
FIXTURE = ROOT / "tests" / "data" / "term_regex_cases.json"
FIXTURE_SHA256 = "246edf1643f47d9beb3d543e4f17a9e7e7571b1781b9954b440c35e6409dc05f"
FIXTURE_SOURCE_COMMIT = "933ba98f72c9dda1ce53fc3853f3714314d6c9b4"
FIXTURE_DOC = json.loads(FIXTURE.read_text(encoding="utf-8"))
FIXTURE_TERMS = FIXTURE_DOC["terms"]
FIXTURE_ALL = FIXTURE_TERMS["agent_ids"] + FIXTURE_TERMS["corpus_ids"]
FIXTURE_ROWS = [pytest.param(c, id=c["id"]) for c in FIXTURE_DOC["cases"]]


def test_w89_the_shared_fixture_is_the_pinned_bytes():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256, (
        "tests/data/term_regex_cases.json is a byte-for-byte copy of the writer's file; do not edit it here")


def test_w89_the_fixture_has_thirty_one_rows_in_four_groups_and_one_word_id():
    groups = {c["rule"][:2] for c in FIXTURE_DOC["cases"]}
    assert len(FIXTURE_DOC["cases"]) == 31 and groups == {"a-", "b-", "c-", "d-"}
    assert FIXTURE_TERMS["word_ids"] == ["Notary", "notice"]


@pytest.mark.parametrize("case", FIXTURE_ROWS)
def test_w89_the_copied_matcher_agrees_with_every_fixture_row(case):
    """Per term: the terms a row expects are exactly the terms whose own pattern matches the text."""
    g = load_guard()
    got = sorted(t for t in FIXTURE_ALL
                 if any(re.search(p, case["text"], f) for p, f in g._term_regex([t], None, FIXTURE_TERMS["word_ids"])))
    assert got == case["expect"], case["rule"]


@pytest.mark.parametrize("case", FIXTURE_ROWS)
def test_w89_one_matcher_over_all_terms_gives_the_rows_answer_too(case):
    g = load_guard()
    pats = [re.compile(p, f) for p, f in g._term_regex(FIXTURE_ALL, None, FIXTURE_TERMS["word_ids"])]
    stored = {t.lower(): t for t in FIXTURE_ALL}       # a match is reported as the id as stored
    got = sorted({stored[m.group(0).lower()] for p in pats for m in p.finditer(case["text"])})
    assert got == case["expect"], case["rule"]


@pytest.mark.parametrize("case", FIXTURE_ROWS)
def test_w89_the_rule_the_guard_really_scans_with_gives_the_rows_answer_too(case):
    g = load_guard()
    got = []
    for key, ids in (("agent_ids", FIXTURE_TERMS["agent_ids"]), ("corpus_ids", FIXTURE_TERMS["corpus_ids"])):
        for t in ids:
            terms = g.NeutralityTerms(True, agent_ids=[t] if key == "agent_ids" else [], corpus_ids=[t] if key == "corpus_ids" else [],
                                      word_ids=[w for w in FIXTURE_TERMS["word_ids"] if w == t])
            rules, problem = g.neutrality_rules(terms)
            assert problem is None and len(rules) == 1, (t, problem)
            if g._matches(rules, case["text"]):
                got.append(t)
    assert sorted(got) == case["expect"], case["rule"]


def test_w89_with_no_word_ids_key_a_short_id_is_upper_case_only_and_a_word_is_matched_as_before():
    """The short-id rule is a constant of the matcher, never keyed on the list; a word id is special only when it is listed."""
    g = load_guard()
    for key in (None, []):
        def hit(t, text):
            return any(re.search(p, text, f) for p, f in g._term_regex([t], None, key))
        assert not hit("qx", "qx said") and hit("qx", "QX said"), key
        assert not hit("qx", "id X-QX-7") and not hit("qx", "id QX-2") and hit("qx", "see QX, then"), key
    assert any(re.search(p, "you should notice that", f) for p, f in g._term_regex(["notice"], None, None))
    assert any(re.search(p, "you should notice that", f) for p, f in g._term_regex(["notice"], None, ["other"]))
    assert not any(re.search(p, "you should notice that", f) for p, f in g._term_regex(["notice"], None, ["notice"]))


def test_w89_a_file_without_word_ids_gives_a_short_id_upper_case_only_matching_in_the_real_scan(tmp_path):
    f = write(tmp_path / "terms.json", terms_doc(agents=WA, corpora=WC))
    tree = make_tree(tmp_path, {"src/lo.py": "# zq alone and X-ZQ-9 and ZQ-2\n", "src/up.py": "# see ZQ, then\n"})
    r = cli(tmp_path, tree=tree, flag_terms=f)
    assert "src/lo.py" not in r.stdout and "src/up.py:1  [neutrality-agent-id]" in r.stdout, out(r)


def test_w89_an_empty_word_ids_list_still_turns_on_the_short_id_rule():
    g = load_guard()
    assert not any(re.search(p, "qx said", f) for p, f in g._term_regex(["qx"], None, []))
    assert any(re.search(p, "QX said", f) for p, f in g._term_regex(["qx"], None, []))


def test_w89_every_generated_rule_still_fires_on_its_own_control_with_word_ids():
    g = load_guard()
    terms = g.NeutralityTerms(True, agent_ids=["notice", "qx", "lantern"], corpus_ids=["team-handbook"], word_ids=["notice"])
    rules, problem = g.neutrality_rules(terms)
    assert problem is None and len(rules) == 4
    assert not [r.name for r in rules if not r.rx.search(r.control)]


# --- the reader: `word_ids` is accepted, validated, and covered by the header sha256 -------------------------------------
WA, WC = ["glim", "quibble", "zq"], ["vendel"]


def test_w89_a_file_with_word_ids_is_usable_and_carries_them(tmp_path):
    res = check(tmp_path, terms_doc(agents=WA, corpora=WC, words=["glim"]))
    assert res.ok and res.cause is None, res.why()
    assert list(res.word_ids) == ["glim"]
    assert res.sha256 == digest(WA, WC, ["glim"])


def test_w89_a_word_id_may_be_a_corpus_id(tmp_path):
    assert check(tmp_path, terms_doc(agents=WA, corpora=WC, words=["vendel"])).ok


def test_w89_an_empty_word_ids_list_is_usable_and_is_not_absent(tmp_path):
    res = check(tmp_path, terms_doc(agents=WA, corpora=WC, words=[]))
    assert res.ok and res.word_ids == ()


def test_w89_a_file_without_word_ids_is_still_usable_and_says_so(tmp_path):
    res = check(tmp_path, terms_doc(agents=WA, corpora=WC))
    assert res.ok and res.word_ids is None


@pytest.mark.parametrize(
    "words",
    [["quibble", "glim"], ["glim", "glim"], ["Glim"], "glim", ["glim", 7], [""], ["nothere"], [" glim"], None],
    ids=["unsorted", "duplicate", "upper-case", "not-a-list", "non-string", "empty-string", "in-neither-list", "untrimmed", "null"],
)
def test_w89_a_malformed_word_ids_is_refused_as_shape_and_names_no_term(tmp_path, words):
    doc = terms_doc(agents=WA, corpora=WC, words=["glim"])
    doc["word_ids"] = words
    doc["_header"]["sha256"] = "0" * 64     # whatever the digest, the shape check comes first
    res = check(tmp_path, doc)
    assert not res.ok and res.cause == "shape", res.why()
    assert "word_ids" in res.detail and not any(t in res.detail for t in WA + WC + ["nothere"]), res.detail


def test_w89_the_sha256_covers_word_ids_and_is_pinned_to_a_hand_computed_value(tmp_path):
    expected = hashlib.sha256(b'{"agent_ids":["aa"],"corpus_ids":["bb"],"word_ids":["aa"]}').hexdigest()
    assert check(tmp_path, terms_doc(agents=["aa"], corpora=["bb"], words=["aa"], sha=expected)).ok
    two_lists = hashlib.sha256(b'{"agent_ids":["aa"],"corpus_ids":["bb"]}').hexdigest()
    assert check(tmp_path, terms_doc(agents=["aa"], corpora=["bb"], words=["aa"], sha=two_lists)).cause == "sha256-mismatch"
    empty = hashlib.sha256(b'{"agent_ids":["aa"],"corpus_ids":["bb"],"word_ids":[]}').hexdigest()
    assert check(tmp_path, terms_doc(agents=["aa"], corpora=["bb"], words=[], sha=empty)).ok
    assert check(tmp_path, terms_doc(agents=["aa"], corpora=["bb"], words=[], sha=two_lists)).cause == "sha256-mismatch"


def test_w89_a_word_ids_list_changed_after_the_file_was_written_is_a_sha_mismatch(tmp_path):
    doc = terms_doc(agents=WA, corpora=WC, words=["glim"])
    doc["word_ids"] = ["glim", "quibble"]
    assert check(tmp_path, doc).cause == "sha256-mismatch"


def test_w89_any_other_unknown_top_level_key_is_still_refused_beside_word_ids(tmp_path):
    res = check(tmp_path, dict(terms_doc(agents=WA, corpora=WC, words=["glim"]), surprise=["x"]))
    assert not res.ok and res.cause == "shape"


def test_w89_the_word_ids_key_is_not_required(tmp_path):
    assert check(tmp_path, terms_doc(agents=WA, corpora=WC)).ok


# --- end to end: the real scan, tree mode ------------------------------------------------------------------------------
PROSE_TREE = {
    "src/prose.py": "# you quibble about it\n# a quibbler\n",
    "src/short.py": "# id X-ZQ-9 and ZQ-2 and zq alone\n",
    "src/real.py": "# ask QUIBBLE about it\n# see /quibble/inbox\n# also ZQ, then\n",
}


def test_w89_the_scan_goes_silent_on_prose_and_hyphenated_ids_and_still_catches_the_real_forms(tmp_path):
    f = write(tmp_path / "terms.json", terms_doc(agents=WA, corpora=WC, words=["quibble"]))
    r = cli(tmp_path, tree=make_tree(tmp_path, PROSE_TREE), flag_terms=f)
    assert r.returncode == 1, out(r)
    assert "src/prose.py" not in r.stdout and "src/short.py" not in r.stdout, out(r)
    assert "src/real.py:1  [neutrality-agent-id]" in r.stdout and "src/real.py:2  [neutrality-agent-id]" in r.stdout \
        and "src/real.py:3  [neutrality-agent-id]" in r.stdout, out(r)


def test_w89_the_same_tree_with_a_file_that_has_no_word_ids_still_matches_a_word_in_prose_but_not_a_short_id_in_a_hyphen_run(tmp_path):
    f = write(tmp_path / "terms.json", terms_doc(agents=WA, corpora=WC))
    r = cli(tmp_path, tree=make_tree(tmp_path, PROSE_TREE), flag_terms=f)
    assert "src/prose.py:1  [neutrality-agent-id]" in r.stdout, out(r)
    assert "src/short.py" not in r.stdout and "src/real.py:3  [neutrality-agent-id]" in r.stdout, out(r)


# Rows the shared fixture does not carry (its rows isolate each hyphen edge to an upper-case run, a digit, and a lower-case join on the
# left): the right edge joined to an upper-case LETTER run or a lower-case run, an underscore join, a parenthesis, and a bare id.
@pytest.mark.parametrize("text,hit", [
    ("id QX-X", False), ("id QX-x", True), ("id QX_x", True), ("id (QX)", True), ("QX", True),
])
def test_w89_the_short_id_rows_the_shared_fixture_does_not_carry(text, hit):
    g = load_guard()
    assert any(re.search(p, text, f) for p, f in g._term_regex(["qx"], None, [])) is hit, text


# --- the engine's own judgement of the entry, the why-lines, the per-user pointer route, a linked private/, and a check that cannot raise ----
def test_c6_a_typo_of_the_private_key_exempts_nothing_because_the_engine_would_not_serve_the_entry(tmp_path):
    r = corpus_run(tmp_path, {"corpora/shipcorp/a.lts": "# a\n"}, corpus_world(tmp_path, spec={"privat": True}))
    assert stays_red(r), out(r)
    assert "1 private, or an entry the engine would not serve" in r.stdout, out(r)


def test_c6_an_id_the_registry_does_not_hold_gets_a_count_only_why_line(tmp_path):
    r = corpus_run(tmp_path, {"corpora/shipcorp/a.lts": "# a\n"}, corpus_world(tmp_path, cid="othercorp"))
    assert stays_red(r) and "corpus exemption: not exempt: 1 not in the registry" in r.stdout, out(r)
    assert "othercorp" not in r.stdout, "the why-line counts, it never names"


def test_c6_an_id_that_does_not_ship_in_this_tree_gets_a_count_only_why_line(tmp_path):
    r = corpus_run(tmp_path, {"corpora/shipcorp/readme.txt": "text\n"}, corpus_world(tmp_path))
    assert stays_red(r) and "corpus exemption: not exempt: 1 not shipped in this tree" in r.stdout, out(r)


def test_c6_the_publication_report_carries_the_counts_of_the_ids_not_exempt(tmp_path):
    rep = tmp_path / "rep.json"
    corpus_run(tmp_path, {"corpora/shipcorp/readme.txt": "text\n"}, corpus_world(tmp_path), mode="--publication", extra_args=("--report", str(rep)))
    scope = json.loads(rep.read_text(encoding="utf-8"))["scope"]["neutrality_terms"]
    assert scope["corpus_exemption_not_exempt"] == {"absent": 0, "unserved": 0, "unshipped": 1}, scope


def pointer_world(tmp_path, reg):
    """A per-user pointer to `reg` under a scratch HOME (the deployment's own route). -> env_extra naming that HOME and no registry variable."""
    home = tmp_path / "ptr-home"
    (home / ".config" / "factlattice").mkdir(parents=True)
    (home / ".config" / "factlattice" / "config.json").write_text(json.dumps({"corpus_config": str(reg)}), encoding="utf-8")
    return {"HOME": str(home)}


def test_c7_the_per_user_pointer_route_exempts_a_shipped_corpus(tmp_path):
    env = corpus_world(tmp_path)
    reg = pathlib.Path(env["FACTLATTICE_CORPUS_CONFIG"])
    r = corpus_run(tmp_path, {"corpora/shipcorp/a.lts": "# a\n"}, pointer_world(tmp_path, reg))
    assert exempted(r), out(r)


def test_c7_a_linked_private_folder_exempts_nothing_and_says_why_on_the_environment_route(tmp_path):
    real = tmp_path / "trunk" / "overlay"
    real.mkdir(parents=True)
    os.symlink(real, tmp_path / "trunk" / "private")
    env = corpus_world(tmp_path)
    r = corpus_run(tmp_path, {"corpora/shipcorp/a.lts": "# a\n"}, env)
    assert r.returncode == 1 and "corpus exemption: the registry's private/ folder is a link; 0 corpus ids exempt" in r.stdout, out(r)


def test_c7_a_linked_private_folder_exempts_nothing_and_says_why_on_the_pointer_route(tmp_path):
    """The overlay layout: `private/` is a link to a folder that is itself named `private`. The pointer resolves the link, so the trunk is the
    overlay's parent and the corpus's roots (under the real trunk) are outside it: nothing is exempt, and the count line says the id did not ship."""
    real = tmp_path / "overlay" / "private"
    real.mkdir(parents=True)
    (tmp_path / "trunk").mkdir()
    os.symlink(real, tmp_path / "trunk" / "private")
    env = corpus_world(tmp_path)
    r = corpus_run(tmp_path, {"corpora/shipcorp/a.lts": "# a\n"}, pointer_world(tmp_path, pathlib.Path(env["FACTLATTICE_CORPUS_CONFIG"])))
    assert stays_red(r) and "corpus exemption: not exempt: 1 not shipped in this tree" in r.stdout, out(r)


def test_c8_a_nul_in_a_root_path_exempts_nothing_and_is_not_a_traceback(tmp_path):
    r = corpus_run(tmp_path, {"corpora/shipcorp/a.lts": "# a\n"}, corpus_world(tmp_path, spec={"roots": [{"path": str(tmp_path / "trunk/corpora/shipcorp") + "\x00x", "area": "specs"}]}))
    assert "Traceback" not in r.stderr, out(r)
    assert stays_red(r), out(r)


def test_c8_a_private_reason_that_raises_exempts_nothing_and_is_not_a_traceback(tmp_path, monkeypatch):
    env = corpus_world(tmp_path)
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", env["FACTLATTICE_CORPUS_CONFIG"])
    mod = load_guard()
    registry, config, _real = mod._engine_for_corpus_exemption()

    def boom(cid, spec):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "_engine_for_corpus_exemption", lambda: (registry, config, boom))
    tree = make_tree(tmp_path, {"corpora/shipcorp/a.lts": "# a\n"})
    exempt, note, why = mod.shipped_corpus_ids([CID], ["corpora/shipcorp/a.lts"], [], str(tree))
    assert exempt == [] and note is None and why == {"absent": 0, "unserved": 1, "unshipped": 0}, (exempt, note, why)


def test_c8_a_failure_of_the_whole_check_exempts_nothing_and_says_so(tmp_path, monkeypatch):
    env = corpus_world(tmp_path)
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", env["FACTLATTICE_CORPUS_CONFIG"])
    mod = load_guard()

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "_trunk_of_registry", boom)
    exempt, note, why = mod.shipped_corpus_ids([CID], [], [], str(tmp_path))
    assert exempt == [] and note == "exemption check failed (RuntimeError)", (exempt, note)


@pytest.mark.skipif(is_published_snapshot(), reason=skip_reason("PUBLISHING.md"))
def test_c9_the_docs_no_longer_say_corpus_ids_are_never_exempt():
    for rel in ("PUBLISHING.md", "CHANGELOG.md"):
        flat = " ".join((ROOT / rel).read_text(encoding="utf-8").split())
        assert "corpus ids are never exempt" not in flat, rel
    assert "shipped corpora" in (ROOT / "PUBLISHING.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["two-roots", "empty-roots", "pin-not-lts", "pin-excluded", "pin-link", "pin-slash",
                                  "shape-only-file", "shape-domain-suffix", "shape-area"])
def test_c2b_each_of_these_conditions_keeps_the_id_red_on_the_guard_under_test(tmp_path, name):
    """Direct tests of the conditions the mutant harness below also covers: a guard with ONE of them broken (DG_GUARD_UNDER_TEST) fails here."""
    r = _scenario(tmp_path, name, GUARD)
    assert r.returncode == 1 and "src/a.py:1  [neutrality-corpus-id]" in r.stdout, out(r)


# --- an exemption selects a rule by the ID it was made from, never by its control (a short or listed word id has an upper-case control) ---
def _exempt_case(tmp_path, agents, corpora, words, files, env=None):
    f = write(tmp_path / "t.json", terms_doc(agents=sorted(agents), corpora=sorted(corpora), words=words))
    return cli(tmp_path, tree=make_tree(tmp_path, files), flag_terms=f, env_extra=env)


@pytest.mark.parametrize("agent,words,text", [("qx", None, "QX"), ("qx", [], "QX"), ("glimmer", ["glimmer"], "GLIMMER")],
                         ids=["short", "short-with-empty-word-ids", "word"])
def test_w89_a_shipped_short_or_word_agent_id_is_exempt_and_not_flagged(tmp_path, agent, words, text):
    r = _exempt_case(tmp_path, [agent], ["vendel"], words, {f"agents/{agent}.md": "# a definition\n", "src/a.py": f"# {text} here\n"})
    assert r.returncode == 0 and f"1 generated agent id(s) exempt as shipped product names: {agent}" in r.stdout, out(r)
    assert "[neutrality-agent-id]" not in r.stdout, out(r)


@pytest.mark.parametrize("agent,words,text", [("qx", None, "QX"), ("glimmer", ["glimmer"], "GLIMMER")], ids=["short", "word"])
def test_w89_the_same_id_with_no_shipped_definition_is_still_flagged(tmp_path, agent, words, text):
    """Negative control for the test above: without the definition the same text is a finding."""
    r = _exempt_case(tmp_path, [agent], ["vendel"], words, {"src/a.py": f"# {text} here\n"})
    assert r.returncode == 1 and "src/a.py:1  [neutrality-agent-id]" in r.stdout, out(r)


@pytest.mark.parametrize("cid,words,text", [("shp", None, "SHP"), ("shipword", ["shipword"], "SHIPWORD")], ids=["short", "word"])
def test_w89_a_shipped_short_or_word_corpus_id_is_exempt_and_not_flagged(tmp_path, cid, words, text):
    env = corpus_world(tmp_path, cid=cid, root_rel=f"trunk/corpora/{cid}")
    r = _exempt_case(tmp_path, ["zorpbot"], [cid], words, {f"corpora/{cid}/a.lts": "# a corpus file\n", "src/a.py": f"# {text} here\n"}, env)
    assert r.returncode == 0 and f"1 generated corpus id(s) exempt as shipped corpora: {cid}" in r.stdout, out(r)
    assert "[neutrality-corpus-id]" not in r.stdout, out(r)


@pytest.mark.parametrize("cid,words,text", [("shp", None, "SHP"), ("shipword", ["shipword"], "SHIPWORD")], ids=["short", "word"])
def test_w89_a_short_or_word_corpus_id_that_does_not_ship_is_still_flagged(tmp_path, cid, words, text):
    env = corpus_world(tmp_path, cid=cid, root_rel=f"trunk/corpora/{cid}")
    r = _exempt_case(tmp_path, ["zorpbot"], [cid], words, {"src/a.py": f"# {text} here\n"}, env)
    assert r.returncode == 1 and "src/a.py:1  [neutrality-corpus-id]" in r.stdout, out(r)


# --- word_ids is stored exactly as the id is in agent_ids or corpus_ids, case included; no case folding anywhere ----------------------------
MA, MC = ["Notary", "apple", "zorpbot"], ["vendel"]       # plain codepoint order: an upper-case letter sorts before a lower-case one


def test_w89_a_mixed_case_word_id_is_accepted_when_it_is_stored_as_the_id_is(tmp_path):
    res = check(tmp_path, terms_doc(agents=MA, corpora=MC, words=["Notary", "apple"]))
    assert res.ok, res.why()
    assert list(res.word_ids) == ["Notary", "apple"] and res.sha256 == digest(MA, MC, ["Notary", "apple"])


@pytest.mark.parametrize("words", [["notary"], ["apple", "Notary"], ["Notary", "Notary"]], ids=["wrong-case", "not-codepoint-sorted", "duplicate"])
def test_w89_a_word_id_that_is_not_an_exact_member_or_not_in_codepoint_order_is_refused_as_shape(tmp_path, words):
    doc = terms_doc(agents=MA, corpora=MC, words=["Notary"])
    doc["word_ids"] = words
    doc["_header"]["sha256"] = "0" * 64
    res = check(tmp_path, doc)
    assert not res.ok and res.cause == "shape" and not any(t in res.detail for t in MA + MC), res.why()


def test_w89_a_word_id_whose_only_listed_form_has_other_case_is_refused_as_shape(tmp_path):
    res = check(tmp_path, terms_doc(agents=["Notary", "zorpbot"], corpora=MC, words=["notary"]))
    assert not res.ok and res.cause == "shape", res.why()


@pytest.mark.parametrize("text,hit", [
    ("see /Notary.md", True), ("see /NOTARY/inbox", True),   # the last is the upper-case form, which matches anywhere
    ("a notaryx", False), ("see /Notaryx/", False),
])
def test_w89_a_mixed_case_word_id_rows_the_shared_fixture_does_not_carry(text, hit):
    g = load_guard()
    assert any(re.search(p, text, f) for p, f in g._term_regex(["Notary"], None, ["Notary"])) is hit, text


def test_w89_the_rule_the_guard_scans_with_fires_on_its_own_control_for_a_mixed_case_word_id():
    g = load_guard()
    rules, problem = g.neutrality_rules(g.NeutralityTerms(True, agent_ids=["Notary"], corpus_ids=["vendel"], word_ids=["Notary"]))
    assert problem is None and not [r.name for r in rules if not r.rx.search(r.control)]
    assert [r.control for r in rules if r.ident == "Notary"] == ["NOTARY"]


# --- the run line and the report say whether word_ids was in force and how many ids it narrowed (counts only) -------------------------------
def test_w89_the_run_line_says_word_ids_in_force_and_how_many_ids_it_narrowed(tmp_path):
    f = write(tmp_path / "t.json", terms_doc(agents=["glimmer", "qx", "zorpbot"], corpora=["vendel"], words=["glimmer"]))
    r = cli(tmp_path, tree=make_tree(tmp_path), flag_terms=f)
    assert "word_ids in force, narrowing 1 id(s)" in r.stdout and "1 short id(s) matched in upper case only" in r.stdout, out(r)
    assert not any(t in out(r) for t in ("glimmer", "zorpbot", "vendel")), out(r)


def test_w89_the_run_line_says_word_ids_absent_when_the_file_has_no_key(tmp_path):
    f = write(tmp_path / "t.json", terms_doc(agents=["qx", "zorpbot"], corpora=["vendel"]))
    r = cli(tmp_path, tree=make_tree(tmp_path), flag_terms=f)
    assert "word_ids absent, no id narrowed as a word id" in r.stdout and "1 short id(s) matched in upper case only" in r.stdout, out(r)


def test_w89_the_publication_scope_line_and_report_carry_the_counts(tmp_path):
    f = write(tmp_path / "t.json", terms_doc(agents=["glimmer", "qx", "zorpbot"], corpora=["vendel"], words=["glimmer"]))
    rep = tmp_path / "rep.json"
    r = cli(tmp_path, "--report", str(rep), tree=snapshot_tree(tmp_path), mode="--publication", flag_terms=f)
    scope = next(ln for ln in r.stdout.splitlines() if ln.startswith("publication scope:"))
    assert "word-ids=1 short-ids=1" in scope, scope
    nt = json.loads(rep.read_text(encoding="utf-8"))["scope"]["neutrality_terms"]
    assert nt["word_ids"] == 1 and nt["short_ids"] == 1, nt
    f2 = write(tmp_path / "t2.json", terms_doc(agents=["zorpbot"], corpora=["vendel"]))
    r2 = cli(tmp_path, tree=snapshot_tree(tmp_path), mode="--publication", flag_terms=f2)
    assert "word-ids=absent short-ids=0" in next(ln for ln in r2.stdout.splitlines() if ln.startswith("publication scope:")), out(r2)


def test_w89_two_ids_that_differ_only_in_case_are_exempted_by_exact_case_and_the_other_stays_flagged(tmp_path):
    """agents/Notary.md ships: the id `Notary` is exempt; the id `notary` is a different id and its rule still fires."""
    f = write(tmp_path / "t.json", terms_doc(agents=["Notary", "notary", "zorpbot"], corpora=["vendel"]))
    tree = make_tree(tmp_path, {"agents/Notary.md": "# a definition\n", "src/a.py": "# the notary said\n"})
    r = cli(tmp_path, tree=tree, flag_terms=f)
    assert "1 generated agent id(s) exempt as shipped product names: Notary" in r.stdout, out(r)
    assert r.returncode == 1 and "src/a.py:1  [neutrality-agent-id]" in r.stdout, out(r)


def test_f2_the_too_wide_control_goes_red_when_the_row_loader_marks_a_named_row_generated(monkeypatch):
    """M17: the control builds its rows through `_parse_rows`, so a loader that makes a row named `neutrality-agent-id` generated is seen."""
    g = load_guard()
    rules, _ = g.neutrality_rules(g.NeutralityTerms(True, agent_ids=["zorpbot"], corpus_ids=["vendel"]))
    real = g._parse_rows

    def loader(path, text):
        rs, problems, n = real(path, text)
        return [g.Rule(r.name, r.rx.pattern, r.control, r.why, r.source, generated=True, ident=r.rx.pattern)
                if r.name == g._AGENT_ID_RULE else r for r in rs], problems, n

    monkeypatch.setattr(g, "_parse_rows", loader)
    red = g._tree_control(rules, echo=None)
    assert any(x.startswith("EXEMPTION TOO WIDE") and "rules-file row named" in x for x in red), red
