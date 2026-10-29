"""Invariants for agent definitions ported into agents/ from another family (SPEC-D-ports-v2 §5).

Part D of the factlattice-reviewer build: smd-first-bringup, smd-qa-tester and
smd-refactoring-analyst are ported to factlattice-*.  A later port joins by ONE LINE in
PORTED_DEFS (checklist rule c1).  The marker sentences pinned here are stated in the spec and
change only together with this test (rule c3).  The gap analyzer's Step 0 block is copied into
each port and pinned byte-identical (rule c2).

Assertions: (a) file + frontmatter, (b) r1 tool/prose agreement, (c) T1 read-path marker,
(d) no SMD read instruction / machine path, (e) domain-agnostic denylist, (f) T3 marker,
(g) analyst edge-data terms, (h) bringup+qa report terms, (i) Step 0 copy pinned.
"""
import re
from pathlib import Path

import pytest
import yaml

AGENTS = Path(__file__).resolve().parent.parent / "agents"

# kind selects which of (f)/(g)/(h) apply.  A later port joins by one line here.
PORTED_DEFS = (
    ("factlattice-first-bringup", "writer"),
    ("factlattice-qa-tester", "writer"),
    ("factlattice-refactoring-analyst", "analyst"),
)

# (e) Domain-agnostic denylist, matched case-insensitively.  Extend here.
# Deployment-specific names are policed by tools/disclosure_guard.py --tree (private vocabulary), not listed in this public file.
DENYLIST = (
    "smd-densifier", "smd-linter", "smd-", "Don",
)

# Terms matched as a whole word (a following apostrophe continues the word), so "don't" does not hit "Don".
WORD_TERMS = ("Don",)

# (d) Forbidden everywhere except on a line carrying the marker "(history)".
FORBIDDEN_PATHS = (r"/Users/", r"~/\.claude/acme-docs", r"semantic-markdown-specification", r"\.smd\b")

T1_MARKER = "Read path: MCP tools, then the call route, then stop"
T3_WRITER = ("no git command that changes the repository or working tree", "no sudo")
T3_ANALYST = ("read-only git only (log, show, diff, status)", "no sudo")

ENGINE_PREFIX = "mcp__lts-engine__"
_TOOL_RE = re.compile(r"mcp__lts-engine__[a-z_]+|(?<![A-Za-z0-9_])lts_[a-z_]+")


def denylist_hits(text):
    """Denylisted terms in text, case-insensitive. Substring match, except WORD_TERMS (word boundary)."""
    low = text.lower()
    return [t for t in DENYLIST
            if (re.search(r"(?<!\w)" + re.escape(t.lower()) + r"(?![\w'\u2019])", low) if t in WORD_TERMS else t.lower() in low)]


def _ids():
    return [n for n, _ in PORTED_DEFS]


def _load(name):
    path = AGENTS / f"{name}.md"
    assert path.is_file(), f"(a) {path} does not exist"
    text = path.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n(.*)\Z", text, re.S)
    assert m, f"(a) {name}: no YAML frontmatter block"
    fm = yaml.safe_load(m.group(1))
    assert isinstance(fm, dict), f"(a) {name}: frontmatter is not a mapping"
    return text, fm, m.group(2)


def _tools(fm):
    t = fm.get("tools")
    if isinstance(t, str):
        return [x.strip() for x in t.split(",") if x.strip()]
    return list(t or [])


def _step0():
    gap = (AGENTS / "factlattice-gap-analyzer.md").read_text(encoding="utf-8")
    a = gap.index("## Step 0: Resolve the Domain Profile")
    b = gap.index("## Step 0.5")
    assert a < b
    return gap[a:b]


def _norm(tok):
    tok = tok.rstrip("_")
    return tok if tok.startswith(ENGINE_PREFIX) else ENGINE_PREFIX + tok


@pytest.mark.parametrize("name", _ids())
def test_a_file_and_frontmatter(name):
    _, fm, _ = _load(name)
    assert fm.get("name") == name
    assert fm.get("model"), "model must be set"
    assert fm.get("description")
    assert _tools(fm)


@pytest.mark.parametrize("name", _ids())
def test_b_r1_tools_equal_body_mentions(name):
    _, fm, body = _load(name)
    in_tools = {t for t in _tools(fm) if t.startswith(ENGINE_PREFIX)}
    in_body = {_norm(t) for t in _TOOL_RE.findall(body)}
    # Non-vacuous: a port reads specs through the engine (T1), so an empty set on both sides is a defect.
    assert in_tools, "r1: no engine tools in tools: (a def with none passes set-equality vacuously)"
    assert in_tools == in_body, (
        f"r1: tools-only={sorted(in_tools - in_body)} body-only={sorted(in_body - in_tools)}")


@pytest.mark.parametrize("name", _ids())
def test_c_t1_read_path(name):
    _, _, body = _load(name)
    assert T1_MARKER in body
    assert "python -m factlattice_engine call" in body


@pytest.mark.parametrize("name", _ids())
def test_d_no_smd_read_or_machine_path(name):
    text, _, _ = _load(name)
    for i, line in enumerate(text.splitlines(), 1):
        if "(history)" in line:
            continue
        for pat in FORBIDDEN_PATHS:
            assert not re.search(pat, line), f"(d) {name}:{i} matches {pat!r}: {line.strip()[:100]}"


@pytest.mark.parametrize("name", _ids())
def test_e_domain_agnostic(name):
    text, _, _ = _load(name)
    # The pinned Step 0 copy is byte-identical to the gap analyzer's (assertion i) and its worked
    # example names the acme profile; it is exempt from the denylist, nothing else is.
    # Exempt only when the block is really present; a def lacking it is scanned in full, so (e)
    # fails for its own reason and not only because (i) does.
    hits = denylist_hits(text.replace(_step0(), ""))
    assert not hits, f"(e) {name} contains denylisted {hits}"


@pytest.mark.parametrize("name,kind", PORTED_DEFS)
def test_f_t3_git_and_sudo(name, kind):
    _, _, body = _load(name)
    low = body.lower()
    for marker in (T3_WRITER if kind == "writer" else T3_ANALYST):
        assert marker.lower() in low, f"(f) {name} lacks T3 marker {marker!r}"


@pytest.mark.parametrize("name", [n for n, k in PORTED_DEFS if k == "analyst"])
def test_g_analyst_edge_terms(name):
    _, _, body = _load(name)
    for term in ("lts_reverse", "lts_find_in_prose", "unminted references"):
        assert term in body, f"(g) {name} lacks {term!r}"


@pytest.mark.parametrize("name", [n for n, k in PORTED_DEFS if k == "writer"])
def test_h_writer_report_terms(name):
    _, _, body = _load(name)
    low = body.lower()
    assert "starting head" in low
    assert "paths created or modified" in low


@pytest.mark.parametrize("name", _ids())
def test_i_step0_copy_is_byte_identical(name):
    text, _, _ = _load(name)
    assert _step0() in text, f"(i) {name} lacks the gap analyzer's current Step 0 block"


def test_e_word_terms_match_as_a_word_only():
    assert denylist_hits("I don't see it") == [], "don't must not hit"
    assert "Don" in denylist_hits("the Don builder"), "the word Don must hit"
    assert "Don" in denylist_hits("DON")


@pytest.mark.parametrize("name", [n for n, k in PORTED_DEFS if k == "writer"])
def test_j_profile_less_run_states_it_in_the_verdict_line(name):
    """The architect ruling P2-2 (amends S3): bringup and qa mark the SPEC READ as profile-inert, keep
    their own verdict, and carry the profile-less state in the verdict line itself."""
    _, _, body = _load(name)
    assert "spec read without a profile" in body


@pytest.mark.parametrize("name", [n for n, k in PORTED_DEFS if k == "writer"])
def test_k_process_hygiene_boundary_and_report_line(name):
    """The architect install review: the two defs that LAUNCH things carry the process-hygiene sentence in
    Boundaries (outside the pinned Step 0 block) and a report line listing processes started and stopped.
    The refactoring analyst launches nothing and is deliberately not required to carry it."""
    text, _, body = _load(name)
    for marker in ("Stop every process you started before you report",
                   "never stop, signal or reconfigure a process you did not start",
                   "never launch against a port or state directory that a live service is using"):
        assert marker in body, f"(k) {name} lacks the process-hygiene marker {marker!r}"
    assert "list the processes started and stopped" in body.lower(), f"(k) {name} lacks the report line"
    boundaries = body.split("## Boundaries", 1)[1]
    assert "never stop, signal or reconfigure" in boundaries, f"(k) {name}: sentence is not under Boundaries"
    assert "never stop, signal or reconfigure" not in _step0(), "(k) the sentence leaked into the pinned Step 0 block"
