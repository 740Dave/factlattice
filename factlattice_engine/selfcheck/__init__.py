"""PARSER SELF-VERIFICATION CONTROL — a RUNNING engine re-parses a known corpus and
checks its own output against hand-authored ground truth.

WHAT THIS IS FOR, AND WHY IT IS NOT A TEST
------------------------------------------
A fixture in `tests/` proves the code IN THE REPO is correct. This proves the code in
the RUNNING PROCESS is correct. On 2026-08-11 a `factlattice_engine.server` was found
serving 19 days stale — started Jul 23, predating line accounting, the Relationships-
triple fix, SL-16, SL-18 and the P5 mapping form. `[MECH-BUILD-STAMP]` could not catch
it, because that process predates the sentinel itself: a build-stamp check compares
HASHES, and a process with no sentinel has no hash to compare. This checks BEHAVIOUR.
A stale process fails it regardless of what it thinks its build is, because the stale
grammar is right there in the answer.

That is why `lts_selfcheck` is exposed as an MCP tool alongside `lts_lint`: a running
server can be ASKED, over the same transport a client already has open, "re-parse the
control and tell me what you get". No filesystem access, no restart, no inference from
a version string.

⛔ WHAT A GREEN SELF-CHECK DOES NOT MEAN
---------------------------------------
It proves the engine handles the silent-loss classes we have NAMED. It cannot prove
there is no nineteenth.

Per standing rule 10, a fixture with labelled ground truth is the only artifact where a
false NEGATIVE is knowable — but only against the KNOWN class list. Every class in
`control.lts` exists because a real corpus taught it to us; SL-5, SL-6, SL-7, SL-8,
SL-11, SL-16, SL-17 and SL-18 were all found by surveying live corpora, not by
imagining failure modes. The corpora remain how the list GROWS; this control only
measures whether the list is still HELD.

Read that sentence twice before quoting a green run. Over-trusting a clean instrument
is exactly what happened with line accounting: coverage proves nothing was silently
DISCARDED and says nothing about whether what was KEPT is correct (SL-11's fabricated
edge was CONSUMED, and therefore invisible to a 100%-accounted file). A control that
passes is evidence about a named list on a named date — the verdict carries both, which
is the point of `measured_against` and `control_digest` in the returned payload.

THE CONTROL PAIR
----------------
`factlattice/selfcheck/control.lts`           — the corpus (the system under test reads this)
`factlattice/selfcheck/control.expected.json` — the oracle (only `json` reads this)

The oracle is NEVER parsed by the system under test. If ground truth lived inside the
corpus, a parser broken enough to misread the corpus would misread its own oracle and
the check would agree with itself. See the `_readme` block in the sidecar for the second
reason (an expectation change must be a reviewable data diff, not an edit to the test
INPUT).

There is deliberately no way to regenerate the sidecar from parser output. See the
sidecar header for why: a control that can be re-blessed on failure can only ever pass.

THE RECORD IS TWO-LEVEL
-----------------------
LEVEL 1 is per SOURCE LINE — the disposition SPEC §2.5 already models (CONSUMED by a
named branch, or CLASSIFIED ignorable), read straight out of `LineCoverage` rather than
re-derived. LEVEL 2 is the graph artifacts. The bridge is each line's `contributes`.
The mapping is not 1:1 in either direction and is deliberately not flattened: a P5
mapping is several lines producing one property; a P3 line is one line producing several
edges; a heading is one line producing a node, its CONTAINS edge and every edge its
properties infer.

The payoff is localisation. "expected 12 properties, got 11" starts an investigation;
"line 109 should have been FLAGGED as a capitalised key, the parser CONSUMED it as a
property" names the defect.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from factlattice.coverage import IGNORABLE_KINDS, UNACCOUNTED_KINDS, account_file
from factlattice.parser import parse_file

# ---------------------------------------------------------------------------
# THE CONTROL DIGEST — a TRIPWIRE, not a lock
# ---------------------------------------------------------------------------
# SHA-256 over the control PAIR, in this declared order and with no separator:
#
#     sha256( bytes(control.lts) || bytes(control.expected.json) )
#
# Reproducible by hand:
#     cat factlattice/selfcheck/control.lts factlattice/selfcheck/control.expected.json \
#         | shasum -a 256
#
# WHY THE PAIR AND NOT JUST THE SIDECAR. The control IS both files. Hashing only the
# oracle lets them drift: someone edits the corpus, leaves the expectations untouched,
# and has changed what is MEASURED without touching what is CHECKED. Both bytes, one
# digest, one thing to cite.
#
# WHY THE CONSTANT LIVES HERE AND NOT IN THE SIDECAR. A digest stored inside
# `control.expected.json` is rewritten by whatever rewrote the file — one pass, both
# fields, no friction, no trace. A constant in Python means replacing the control
# requires editing CODE: a different review path, and it routes through the pre-commit
# gate's python arm. The added cost is the entire mechanism.
#
# ⚠ WHAT IT IS NOT. This is a TRIPWIRE, NOT TAMPER-PROOFING. The control can be
# replaced and this constant regenerated by anyone who can edit this file; nothing here
# prevents that, and claiming otherwise would be the same class of defect this artifact
# exists to catch. What it delivers is narrower and real:
#   * it converts a SILENT control swap into a visible code diff;
#   * it catches ACCIDENTAL mutation — a bad merge, an editor's trailing-whitespace fix,
#     a tool that rewrote the JSON;
#   * it makes a receipt citable. `PASS against control sha256:ab12…` from two machines
#     weeks apart can be compared for whether they tested the SAME control, and a green
#     result carrying an unrecognised digest is immediately suspicious.
# That last point is standing rule 4 — "a cleanliness claim carries the class list and
# the date it was measured against" — made MECHANICAL instead of prose.
#
# (SPEC §3.2's `snapshot_hash` is the same shape but has to exclude itself from its own
# input. Ours does not: the digest is not inside either hashed file. Do not import that
# complication.)
# CHANGED 2026-08-11, deliberately and with a reason — this is an ORACLE CHANGE, not a
# re-baseline of a red run. The control was GREEN (24/24) at the previous digest while
# two whole loss classes bled through the corpora, because it contained neither shape:
# no WRAPPED property value and no ID TABLE. Five cases and 33 lines were APPENDED
# (SL-19 wrapped value + its bullet exclusion; SL-20 ID table + its decorative-table
# exclusion + the ancestor-attachment case). Lines 1-155 and every one of their declared
# dispositions are byte-identical; the only pre-existing expectations edited are
# CTRL-DOC.properties and the GRAPH-TOTALS counts, both mechanical consequences of the
# appended lines. The point of quoting BOTH digests is that a reader can see the oracle
# moved and go read why:
#   was: sha256:12c46333d006be1cbe0aeaa42b26ef4f24d072417806fbaaa6117b5141590e51
# CHANGED AGAIN 2026-08-11 — the SAME kind of change, one grammar over, and it is an
# ORACLE CHANGE, not a re-baseline of a red run. The control was GREEN (27/27) at the
# previous digest while TWO more loss classes bled through the corpora, because it
# contained neither shape: no property-shaped bullet whose KEY REGION carried a
# PARENTHETICAL qualifier (SL-21) and none carrying an EN/EM-DASH qualifier (SL-22).
# Measured on the day they were named: 163 across the Acme corpora (151 paren, 12
# dash) in 21 files — including several inside the preview layer's own "clean" negative
# control, which `lts_preview` was reporting at 100.0% coverage / 0 lost.
# Four cases and 12 lines were APPENDED (SL-21 + its paren-in-the-VALUE exclusion,
# SL-22 + its dash-and-later-colon-in-the-VALUE exclusion). Lines 1-188 and every one of
# their declared dispositions are byte-identical; the only pre-existing expectations
# edited are the GRAPH-TOTALS counts and the two `flagged_line_count` assertions in
# WRAPPED-VALUE-EXCLUSION and REL-CONTINUATION-EXCLUSION (3 -> 5), all mechanical
# consequences of the appended lines. Quoting BOTH digests so a reader can see the
# oracle moved and go read why:
#   was: sha256:3578ce7813e2067e384687f1c0a11e22ab9abdfb032e8d670d78dbe5e65300a2
# CHANGED AGAIN 2026-08-13 — THE SAME KIND OF CHANGE, AND AGAIN AN ORACLE CHANGE RATHER
# THAN A RE-BASELINE OF A RED RUN. The control was GREEN (33/33) at the previous digest
# while a whole loss class bled through the corpora, because it contained neither shape:
# no heading in which a CITATION hijacks the node id (SL-23). Reproduced in isolation
# 2026-08-13 and found live in `04-recall-side-failures-and-reflexive-reach.lts:124`,
# where `## 8. Independent replication of [FLCD-CONNECTIVITY], … [FL-RECALL-REPLICATION]`
# mints the CITATION, leaves the declared id inside the title string, and collides with
# the real `FLCD-CONNECTIVITY` in another file — so the document arguing that the graph is
# less connected than it looks has a section that is not in the graph.
#
# ⛔ SL-23 IS THE FIRST SUBSTITUTION CLASS IN THIS CONTROL. Every other class here makes
# something DISAPPEAR; this one makes something LIE — the node parses, it is queryable,
# and it is the wrong node. Line accounting is GREEN on the offending line and the
# key-loss rule cannot see it (there is no bullet), which is why nothing caught it.
#
# One case and four lines were APPENDED (`CASE-CITED-NOT-DECLARED`, deliberately in the
# LIVE corpus shape: an ordinal, a mid-sentence citation, and the real id declared
# ID-LAST — a control that exercises a simpler shape than the corpus uses certifies a
# narrower class than its name claims). Lines 1-200 and every one of their declared
# dispositions are byte-identical; the only pre-existing expectations edited are the
# GRAPH-TOTALS counts (node 29->30, edge 42->43, line 200->204), all mechanical
# consequences of the appended lines. `finding_count` and `flagged_line_count` are
# UNCHANGED, and that is the finding: the new shape raises neither.
#   was: sha256:6bbde6fabf13f3b448fd87340fe6cc05b208fd58a11569946e8bd550ffa62576
# ⛔ CHANGED 2026-08-15 (was a578e6b1…). The control pair GAINED five cases — SL-24..SL-28,
# the property-edge minting classes. A control_digest change is the EXPECTED and
# REQUIRED consequence of adding a class to the list; it is a reviewed code diff, not
# tampering. The prior digest certified an engine on which `relates-to` minted nothing
# across 140 occurrences: PASS 34/34, digest matching, graph wrong.
#   was: sha256:ce355e65d275612e85e841e240562bf87da1e035e26498a0aac937e71e44e7bb
# ⛔ CHANGED AGAIN 2026-08-15 — FOUR cases (SL-29..SL-32) and NOT ONE SOURCE LINE.
# `control.lts` is BYTE-IDENTICAL across this change; the entire delta is 301 inserted
# lines in `control.expected.json`, 0 deleted. Again an ORACLE CHANGE, not a re-baseline
# of a red run: the previous digest certified an engine whose profile loader would accept
# a domain profile that redefines core `PART_OF`, producing a graph with PART_OF edges in
# TWO layers at once — `lts_traverse(layers=['semantic'])` returning half of them, and
# `lts_lint` reporting the corpus CLEAN. 39/39 green, digest matching, vocabulary poisoned.
#
# ⛔ SL-29..SL-32 ARE THE FIRST CASES IN THIS CONTROL WHOSE SUBJECT IS NOT THE PARSER.
# They exercise the PROFILE LOADER, because that is where this class of damage happens —
# before any document is read. A stale process parses `control.lts` perfectly and still
# accepts the poisoned profile, so no assertion over the parsed document could ever catch
# it. The trade this makes (a fixture living in the oracle file) is argued in full in the
# `_check_profile_assertion` block below and in the sidecar's ORACLE v5 note; the short
# version is that `load_profile(dict)` shares no reader with `json`, so the
# correlated-corruption rule the sidecar exists to enforce is not being bent.
#
# The same commit repaired `measured_against`, which 3f9a2c1 left claiming `SL-1 .. SL-23`
# after adding SL-24..SL-28. That string is returned VERBATIM in every verdict, so a green
# run was naming the wrong class list — standing rule 4 made mechanically false by the
# very commit that widened the list.
#
# ⛔ CHANGED 2026-08-16 (Era 0, PARSER-ENGINEERING-SPEC.md rev 2 C1 / SPEC A4). ONE case
# added — SL-33 — plus ONE `finding` assertion on the pre-existing CASE-LOWER-BRACKET
# fixture that this change ALSO affects (its `not-an-id` bracket now ALSO raises the
# new finding: an all-lowercase-hyphenated single token is identifier-SHAPED by the
# literal A4 grammar even though it carries no upper-initial). `control.lts` gained one
# heading + two property lines (226-230); GRAPH-TOTALS moved node 35->36, edge 52->53,
# finding_count 1->3, line_count 225->230 — flagged_line_count is UNCHANGED at 5,
# because a malformed_ref finding is an ADDITIVE ParseFinding, never a coverage-lint
# "unaccounted line": the property still parses, still stores its literal value, and
# still mints no edge (Era 0's own compatibility contract — "findings only; every
# graph byte-identical; nothing breaks" — PARSER-ENGINEERING-SPEC.md §7).
# The previous digest certified an engine on which `[Mixed-Case-Attempt]` (or
# `[COMMIT-09b80e9]`, or any of the review's measured 164 mixed-case bracket refs)
# vanished with NO diagnostic anywhere the parser could raise one — an id-shaped
# ATTEMPT and ordinary multi-word prose were indistinguishable to every instrument in
# this engine. 42/43 (now 43/44), digest matching, the attempt unheard.
#   was: sha256:e00a845a19ff2650791e8e7af1d6fcfab24b2ea46a79d7401a22b86be87e1144
# CHANGED 2026-10-03 -- an ORACLE CHANGE, not a re-baseline of a red run. The control was
# GREEN at the previous digest while `canonical-node: [ID]` (a legacy node -> its canonical
# node) was a plain stored property: the bracketed id sat as TEXT, no edge, lint clean,
# invisible to every traversal. SL-38 APPENDED 8 lines to control.lts (231-238: heading
# CASE-CANONICAL + `- canonical-node: [CASE-P1-SIMPLE]`, and heading CASE-CANONICAL-BARE +
# bare `- canonical: false -- ...; see [CASE-P1-SIMPLE]`, which must mint NOTHING because
# bare `canonical` is free-text status prose on 10 live acme-docs nodes, review F1) and
# the matching oracle entries (2 graph nodes, lines 231-238, cases MINT-CANONICAL and
# NOMINT-CANONICAL-BARE, GRAPH-TOTALS node 36->38, edge 53->56, line 230->238;
# oracle_version 6->7; measured_against SL-1 .. SL-38). Lines 1-230 and their
# dispositions are untouched. The key is `canonical-node`: bare `canonical` (10 prose uses) and
# `canonical-ref` (11 provenance pointers) both collided with existing free text.
#   was: sha256:7e6c31a9edfada0abf866d63bb1f0c52e90f7af7710e2ab3966255232e790860
# CHANGED 2026-10-07 -- an ORACLE CHANGE, not a re-baseline of a red run (factlattice-reviewer Spec A,
# A-E3). The control was GREEN at the previous digest while `attaches-to-hook` and `applies-rule`
# (the review-extension PROC graph edges) were plain stored properties: the bracketed ids sat as TEXT,
# no edge, lint clean, invisible to every traversal. SL-39 APPENDED 19 lines to control.lts (239-257:
# CASE-REV-HOOK, CASE-REV-PROC with both keys and an integer `order`, CASE-REV-RULE pointing at the
# existing CASE-P1-SIMPLE, and CASE-REV-BARE with the look-alikes `applies-to` / `attaches-to`, which
# must mint NOTHING) and the matching oracle entries (4 graph nodes, lines 239-257, cases
# MINT-REV-ATTACHES, MINT-REV-APPLIES, NOMINT-REV-BARE, GRAPH-TOTALS node 38->42, edge 56->63, line
# 238->257; oracle_version 7->8; measured_against SL-1 .. SL-39). Lines 1-238 and their dispositions
# are untouched. case_count 53 -> 56. Negative control: removing either table entry turns the
# verdict FAIL with the digest still matching (tests/test_review_edge_verbs.py).
#   was: sha256:b1a0d00a3b71401e05b7e08c57a9779a8681e72d934923b8f25f74e8025bf918
# CHANGED 2026-10-07 -- an ORACLE CHANGE, not a re-baseline of a red run (factlattice-reviewer Spec A, A-E4,
# slice 2). The control was GREEN at the previous digest (56/56) while the reviewer's own vocabulary was
# unmeasured: nothing checked that a review or extension node must carry its required properties, or that a
# stamped `lts: "1.1"` review declares every key it uses. SL-40 APPENDED two cases to control.expected.json and
# NOT ONE SOURCE LINE (`control.lts` is byte-identical): REVIEWER-SHAPES-VALID (one valid node of each of nine
# shapes under the SHIPPED reviewer profile: zero V3 findings, zero undeclared keys, strict mode) and
# REVIEWER-SHAPES-STRIPPED (the same nine nodes with every required property removed: EXACTLY the 65 V3 findings
# the oracle lists). A new assertion kind, `reviewer_shapes`, carries the documents inline (`given_*`), as the
# Era-1 and profile-load cases do; the expectations are read by `json` and are NOT derived from the profile.
# oracle_version 8->9; measured_against v10 (2026-10-07), SL-1 .. SL-40; case_count 56 -> 58. Negative controls:
# tests/test_reviewer_selfcheck_control.py edits a copy of the profile (drops a required key, adds one, undeclares
# a key, removes the profile) and requires the verdict to go FAIL with the digest still matching.
#   was: sha256:903121e781495992bad6ccd1c1e920a08fc16d68ffb3ca4166ef21f660d73969
# CHANGED 2026-10-07 -- VOCABULARY-ONLY ORACLE CHANGE (a publication scrub, not a re-baseline of a red run): nine
# deployment names in `control.lts` (1) and `control.expected.json` (12 lines) were replaced by neutral names. Every case,
# every assertion and every expected value is unchanged: the same 58 cases give the same verdict, and applying the
# reverse name mapping to the new files reproduces the previous bytes exactly. The digest moves only because the bytes did.
#   was: sha256:ebb694d8fe882d1fbe4aca6b383a0a4601f982928a8c5d183ad17495a3a4bc4b
# CHANGED 2026-10-07 -- AN ORACLE CHANGE FOLLOWING A DELIBERATE CONTRACT CHANGE (SPEC-U1a, registry lookup), not a
# re-baseline of a red run. The reviewer's REVIEW and UNREVIEWED shapes gained two REQUIRED properties,
# `registry-mcp` and `registry-cli`, and `review-format-version` went 1 -> 2 (the profile's required lists, the
# pinned test contract and the shipped procedure moved in the same change). `control.lts` is byte-identical; only
# `control.expected.json` changed: the two inline REVIEWER-SHAPES documents carry the new properties and format
# version 2, and the stripped twin's expected V3 list grew from 65 to 69 findings (the two new keys on each of
# the two nodes). Hand-edited in the ORACLE, read by `json`, NOT derived from the profile it checks. The verdict
# is unchanged (58/58); oracle_version 9->10. Negative control (run, not assumed): the control was run against the NEW profile
# with the OLD oracle and went FAIL 56/58 -- the valid twin reported the four new required keys as missing and the
# stripped twin's finding list differed from the old 65 -- then PASS 58/58 with the new oracle.
#   was: sha256:1e252cdfcb98a28481e767ea27420b957095fe66e6df91bf5cfc976f64cf10b8
# CHANGED 2026-10-27 -- VOCABULARY-ONLY ORACLE CHANGE (a second publication scrub, not a re-baseline of a red run): in
# `control.expected.json` (17 prose lines: commentary, notes, a case title and reasons) the names of two private spec files, a sibling
# project's test path and a commit abbreviation, and the measured counts quoted beside them, were replaced by invented
# names and figures of the same shape; the three corpus DATA dates (`created`, `authored_at`, `assessed_at`) moved
# identically in `control.lts` (3 lines) and in the oracle (3 lines), so the oracle has 20 changed lines and the corpus 3. No expectation, case, assertion or line count changed (both
# files keep their line counts): the same 58 cases give the same verdict, 58 passed, 0 failed. The oracle prose was
# edited by hand, as it always is (there is no regeneration path, by design), and no value the parser measures was touched.
#   was: sha256:d33ac77dd96ee2d22c99534cd7bc142b3d2dec6c6a5ccd42d1ac8c546c223076
CONTROL_DIGEST = "sha256:bb5a2f2fe033eb410e08129d63f4edef64eacd184e060c8159ddc05b0546a9ed"

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_CONTROL_MODIFIED = "CONTROL_MODIFIED"

_CONTROL_LTS = "control.lts"
_CONTROL_EXPECTED = "control.expected.json"

_LIMIT = (
    "Measures RECALL against the KNOWN silent-loss class list only (standing rule 10). "
    "A green result proves the engine still handles the classes we have NAMED; it "
    "cannot prove there is no nineteenth. Live corpora remain how the list grows."
)


def control_dir() -> str:
    """Directory holding the control PAIR — it ships inside the `factlattice` package."""
    import factlattice.selfcheck as _pkg

    return os.path.dirname(os.path.abspath(_pkg.__file__))


def compute_control_digest(directory: Optional[str] = None) -> str:
    """`sha256:` over control.lts THEN control.expected.json. Order is part of the contract."""
    d = directory or control_dir()
    h = hashlib.sha256()
    for name in (_CONTROL_LTS, _CONTROL_EXPECTED):
        with open(os.path.join(d, name), "rb") as fh:
            h.update(fh.read())
    return "sha256:" + h.hexdigest()


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------
def _edge_tuple(e: Any) -> Tuple:
    """Edge identity for comparison: the six declared fields plus the optional label."""
    if isinstance(e, dict):
        return (e.get("source"), e.get("type"), e.get("target"), e.get("layer"),
                e.get("inference"), tuple(e.get("flags") or []), e.get("label"))
    return (e.source, e.type, e.target, e.layer, e.inference, tuple(e.flags or []), e.label)


class _CaseRun:
    """Accumulates one case's failures. A failure ALWAYS carries what was expected and
    what was produced — a bare 'assertion failed' would defeat the whole point."""

    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.failures: List[dict] = []
        self.checks = 0

    def check(self, what: str, expected: Any, produced: Any, *, where: str = "") -> bool:
        self.checks += 1
        if expected == produced:
            return True
        f = {"case_id": self.spec.get("case_id"), "check": what,
             "expected": expected, "produced": produced}
        if where:
            f["where"] = where
        self.failures.append(f)
        return False

    def fail(self, what: str, expected: Any, produced: Any, *, where: str = "") -> None:
        self.checks += 1
        f = {"case_id": self.spec.get("case_id"), "check": what,
             "expected": expected, "produced": produced}
        if where:
            f["where"] = where
        self.failures.append(f)


# ---------------------------------------------------------------------------
# LEVEL 1 — per-line disposition
# ---------------------------------------------------------------------------
def _check_line(run: _CaseRun, lineno: int, decl: dict, verdicts, edges_by_line,
                nodes_by_id, prop_claims) -> None:
    where = f"line {lineno}"
    if not 1 <= lineno <= len(verdicts):
        run.fail("line exists", f"line {lineno} within 1..{len(verdicts)}",
                 f"file has {len(verdicts)} lines", where=where)
        return
    v = verdicts[lineno - 1]
    disp = decl.get("disposition")

    if disp == "CONSUMED":
        run.check("line disposition", "CONSUMED",
                  "CONSUMED" if v.kind == "consumed" else f"{disp_of(v)} ({v.kind})",
                  where=where)
        run.check("consuming branch", decl.get("branch"), v.consumed_by, where=where)
    elif disp == "IGNORABLE":
        run.check("line disposition", "IGNORABLE", disp_of(v), where=where)
        run.check("ignorable kind", decl.get("kind"), v.kind, where=where)
    elif disp == "FLAGGED":
        run.check("line disposition", "FLAGGED", disp_of(v), where=where)
        run.check("flag kind", decl.get("kind"), v.kind, where=where)
        run.check("near-miss variant", decl.get("variant"), v.detail.get("variant"), where=where)
        run.check("near-miss key", decl.get("key"), v.detail.get("key"), where=where)
        run.check("near branch", decl.get("near_branch"), v.near_branch, where=where)
    else:
        run.fail("declared disposition is one of CONSUMED/IGNORABLE/FLAGGED",
                 "CONSUMED|IGNORABLE|FLAGGED", disp, where=where)
        return

    # --- LEVEL 2 bridge: the graph artifacts this line contributed ----------
    contributes = decl.get("contributes") or []
    expected_edges = [_edge_tuple(c) for c in contributes if c.get("artifact") == "edge"]
    produced_edges = [_edge_tuple(e) for e in edges_by_line.get(lineno, [])]
    run.check("edges minted by this line", expected_edges, produced_edges, where=where)

    for c in contributes:
        art = c.get("artifact")
        if art == "edge":
            continue                    # already compared, as a set, above
        if art == "node":
            if c["id"] not in nodes_by_id:
                run.fail("node minted by this line", c["id"], "no such node", where=where)
            else:
                run.checks += 1
        elif art == "property":
            prop_claims.setdefault(c["node"], set()).add(c["key"])
            node = nodes_by_id.get(c["node"])
            if node is None or c["key"] not in node.properties:
                run.fail("property set by this line",
                         f"{c['node']}.{c['key']} present",
                         "node missing" if node is None else
                         f"keys={sorted(node.properties)}", where=where)
            else:
                run.checks += 1
        elif art == "property_entry":
            node = nodes_by_id.get(c["node"])
            val = node.properties.get(c["key"]) if node else None
            if not isinstance(val, dict) or c["subkey"] not in val:
                run.fail("mapping entry set by this line",
                         f"{c['node']}.{c['key']}[{c['subkey']!r}] present",
                         repr(val), where=where)
            else:
                run.checks += 1
        elif art == "property_item":
            node = nodes_by_id.get(c["node"])
            val = node.properties.get(c["key"]) if node else None
            if not isinstance(val, list) or len(val) <= c["index"]:
                run.fail("list item set by this line",
                         f"{c['node']}.{c['key']}[{c['index']}] present",
                         repr(val), where=where)
            else:
                run.checks += 1
        else:
            run.fail("known contributes artifact",
                     "node|property|property_entry|property_item|edge", art, where=where)


def disp_of(v) -> str:
    if v.kind == "consumed":
        return "CONSUMED"
    if v.kind in UNACCOUNTED_KINDS:
        return "FLAGGED"
    if v.kind in IGNORABLE_KINDS:
        return "IGNORABLE"
    return f"UNKNOWN({v.kind})"


# ---------------------------------------------------------------------------
# LEVEL 2 — nodes
# ---------------------------------------------------------------------------
_NODE_FIELDS = ("title", "type_prefix", "parent_section", "status",
                "document_tag", "definition_form")


def _check_node(run: _CaseRun, node_id: str, expected: dict, nodes_by_id) -> None:
    where = f"node {node_id}"
    node = nodes_by_id.get(node_id)
    if node is None:
        run.fail("node exists", node_id, "absent from the parse", where=where)
        return
    for field in _NODE_FIELDS:
        run.check(f"node.{field}", expected.get(field), getattr(node, field), where=where)
    run.check("node.properties", expected.get("properties"), node.properties, where=where)


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------
def _edge_matches(e, where: dict) -> bool:
    if "source" in where and e.source != where["source"]:
        return False
    if "type" in where and e.type != where["type"]:
        return False
    if "target" in where and e.target != where["target"]:
        return False
    if "layer" in where and e.layer != where["layer"]:
        return False
    if "target_contains" in where and where["target_contains"] not in e.target:
        return False
    if "flag" in where and where["flag"] not in (e.flags or []):
        return False
    return True


# ---------------------------------------------------------------------------
# PROFILE-LOAD assertions — the VOCABULARY the running process will accept
# ---------------------------------------------------------------------------
# ⛔ WHY THE CONTROL GREW A SECOND SUBJECT (2026-08-15). Every other case here asks
# "did the parser READ this document correctly". SL-29..SL-32 ask "will this engine
# ACCEPT a poisoned vocabulary" — and that question cannot be asked through
# `control.lts`, because the damage happens at profile LOAD, before any document is
# read. A stale process carrying the pre-guard loader parses `control.lts` perfectly
# and will still load a profile that shadows core `PART_OF`, after which every
# layer-scoped traversal in the deployment silently returns a subset.
#
# ⛔ THE COMPROMISE, STATED RATHER THAN GLOSSED. The profile FIXTURE (`given_profile`)
# is an INPUT and it lives in the oracle file, which the sidecar's own `_readme` says
# holds ground truth only. The rule it protects is CORRELATED CORRUPTION: an oracle
# read by the system under test would be misread by the same defect and the check would
# agree with itself. That risk is structurally absent here — `load_profile(dict)` does
# not touch the `.lts` parser at all, so the fixture and the expectation have no reader
# in common. The keys are named `given_*` so an input is never mistakable for an
# expectation.
#
# ⛔ `error_contains` IS NOT DECORATION. A refusal check that only asserts "it raised"
# cannot tell a guard from a function that refuses everything, and cannot tell a
# refusal that NAMES BOTH SIDES from one that says "invalid profile". The rule being
# controlled is "refuse, naming both definitions"; the substring list is what makes the
# second half mechanical.
def _norm_spec(v):
    """Tuples from the loader vs. lists from JSON — compare in one shape."""
    if isinstance(v, dict):
        return {k: _norm_spec(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_norm_spec(x) for x in v]
    return v


def _check_refusal(run, a: dict, where: str, call) -> None:
    from factlattice.profile import ProfileCollisionError

    try:
        produced = call()
    except ProfileCollisionError as exc:
        msg = str(exc)
        missing = [s for s in (a.get("error_contains") or []) if s not in msg]
        run.check("the refusal names every required part", [], missing, where=where)
        return
    except Exception as exc:  # noqa: BLE001
        run.fail("refused with ProfileCollisionError", "ProfileCollisionError",
                 f"{type(exc).__name__}: {exc}", where=where)
        return
    run.fail("the collision guard fired", "ProfileCollisionError raised",
             f"ACCEPTED CLEAN -> {_norm_spec(produced)!r}", where=where)


def _load_all(specs):
    from factlattice.profile import load_profile
    return [load_profile(dict(s)) for s in specs]


def _merger(name: str):
    from factlattice.profile import merge_edge_types, merge_property_edges
    return merge_property_edges if name == "property_edges" else merge_edge_types


def _check_profile_assertion(run: _CaseRun, a: dict, kind: str) -> bool:
    """Returns True if `kind` was one of the profile-load assertions."""
    from factlattice.profile import load_profile

    where = f"assert {kind}"
    if kind == "profile_refused":
        _check_refusal(run, a, where,
                       lambda: load_profile(dict(a["given_profile"])).edge_types)
    elif kind == "profile_accepted":
        try:
            prof = load_profile(dict(a["given_profile"]))
        except Exception as exc:  # noqa: BLE001
            run.fail("a legitimate registration loads", "loaded",
                     f"{type(exc).__name__}: {exc}", where=where)
            return True
        if "edge_types" in a:
            run.check("profile.edge_types", a["edge_types"],
                      _norm_spec(prof.edge_types), where=where)
        if "property_edges" in a:
            run.check("profile.property_edges", a["property_edges"],
                      _norm_spec(prof.property_edges), where=where)
    elif kind == "profiles_merge_refused":
        merge = _merger(a.get("merge", "edge_types"))
        _check_refusal(run, a, where,
                       lambda: merge(_load_all(a["given_profiles"])))
    elif kind == "profiles_merge_accepted":
        merge = _merger(a.get("merge", "edge_types"))
        try:
            got = merge(_load_all(a["given_profiles"]))
        except Exception as exc:  # noqa: BLE001
            run.fail("agreement across profiles merges", a.get("value"),
                     f"{type(exc).__name__}: {exc}", where=where)
            return True
        run.check("merged vocabulary", a["value"], _norm_spec(got), where=where)
    else:
        return False
    return True


def _check_era1_assertion(run: _CaseRun, a: dict, kind: str) -> bool:
    """Era 1 (A1/A2/A3) vocabulary-regime assertions. Returns True if `kind` was one.

    ⛔ WHY THESE CARRY `given_document` INSTEAD OF LIVING IN `control.lts`, and it is a
    STRUCTURAL reason rather than a convenience. Every one of these classes is about the
    SAME BYTES parsed under a DIFFERENT (mode, vocabulary) — a legacy document and a
    strict document that differ ONLY in severity, an undeclared key that is an error
    here and a warning there. `control.lts` is one file with one (absent) version marker
    parsed under one core-only config, so it cannot express a two-mode comparison at
    all: a region of it can hold one arm of the pair and never both. Putting them here
    is the SL-29..SL-32 precedent (cases whose subject is not the document), applied to
    a case whose subject is the DOCUMENT'S MODE.

    ⛔ THE CORRELATED-CORRUPTION RULE, honoured explicitly. The oracle's own readme
    forbids an expectation being read by the system under test. That rule is intact:
    `given_document` is an INPUT, named `given_*` exactly as SL-29..SL-32's inputs are,
    and every EXPECTATION beside it (`mode`, `kinds`, `severity`) is still read by
    `json` and never by the parser. This is the same arrangement `control.lts` itself
    has always had — a parser-read input with a json-read oracle — merely inline.
    """
    from factlattice.parser import parse_text
    from factlattice.parser_config import ParserConfig

    where = f"assert {kind}"
    if kind not in ("parse_mode", "vocabulary_findings", "entry_point_equivalent"):
        return False

    if kind == "entry_point_equivalent":
        from factlattice_engine.selfcheck.entry_point_equivalence import (
            run as epc_run, run_negative_control as epc_neg)
        got = epc_run()
        run.check("every production path agrees with the reference",
                  True, got["equivalent"], where=where)
        run.check("divergences", [], [f"{d['path']}: {d['probe']}"
                                      for d in got["divergences"]], where=where)
        if a.get("negative_control"):
            # ⛔ the instrument must still be ABLE to report divergence, or its green
            # above is a green light wired to the ignition.
            neg = epc_neg()
            run.check("the negative control fires when a path is starved",
                      True, neg["negative_control"]["fired"], where=where)
        return True

    cfg = ParserConfig.from_kwargs(
        declared_property_keys=a.get("given_declared_keys"),
        domain_verbs=(frozenset(a["given_domain_verbs"])
                      if a.get("given_domain_verbs") is not None else None))
    parsed = parse_text(a["given_document"], "case.lts", config=cfg)

    if kind == "parse_mode":
        run.check("format_version", a.get("format_version"), parsed.format_version,
                  where=where)
        run.check("mode", a["mode"], parsed.mode, where=where)
        if "graph_identical_to" in a:
            # THE A1 CONTRACT: the marker changes SEVERITIES, never the GRAPH.
            other = parse_text(a["graph_identical_to"], "case.lts", config=cfg)
            run.check("nodes are identical across the two modes",
                      sorted((n.id, tuple(sorted((n.properties or {}).items())))
                             for n in other.nodes),
                      sorted((n.id, tuple(sorted((n.properties or {}).items())))
                             for n in parsed.nodes), where=where)
            run.check("edges are identical across the two modes",
                      sorted((e.source, e.type, e.target, e.layer)
                             for e in other.edges),
                      sorted((e.source, e.type, e.target, e.layer)
                             for e in parsed.edges), where=where)
        return True

    # kind == "vocabulary_findings"
    got = [f for f in parsed.vocabulary_findings
           if a.get("kind") is None or f.kind == a["kind"]]
    if "kinds" in a:
        run.check("vocabulary finding kinds", sorted(a["kinds"]),
                  sorted({f.kind for f in parsed.vocabulary_findings}), where=where)
    if "keys" in a:
        run.check("keys named by the finding", sorted(a["keys"]),
                  sorted(f.key for f in got if f.key), where=where)
    if "severity" in a:
        run.check("severity", [a["severity"]] * len(got),
                  [f.detail.get("severity") for f in got], where=where)
    if "suggestions" in a:
        run.check("edit-distance suggestions",
                  a["suggestions"],
                  {f.key: f.detail.get("suggestions") for f in got
                   if f.key in a["suggestions"]}, where=where)
    if "count" in a:
        run.check("finding count", a["count"], len(got), where=where)
    return True



# ---------------------------------------------------------------------------
# REVIEW-SHAPE assertions (A-E4, SL-40) -- the SHIPPED reviewer profile judging review/extension nodes
# ---------------------------------------------------------------------------
# ⛔ THE THIRD SUBJECT OF THIS CONTROL (after the parser and the profile LOADER): the VOCABULARY the reviewer's own
# documents are judged against. A review that lost a required property must be visible to the reviewer's
# self-gate, and the self-gate counts V3 (required-property) findings ITSELF, because every profile finding is a
# WARNING (F3) and lint reports the corpus clean. That counting is only as good as the profile's `required` lists,
# so the lists are measured here: a valid document must give none, and a STRIPPED TWIN (the same nodes with every
# required property removed) must give EXACTLY the list the oracle names.
#
# ⛔ `given_document` is an INPUT (named `given_*`); `expect_v3` / `expect_undeclared_keys` are EXPECTATIONS, read
# by `json` and never by the parser. The PROFILE under test is the one the package ships
# (`reviewer_profile_path()`); the expectation does not come from it, which is the point: an expectation derived
# from the profile it checks agrees with it whatever it says.
def reviewer_profile_path() -> str:
    """The reviewer profile this control judges the review shapes against: the shipped package data. A function
    (not a constant) so a test can point it at an edited copy and watch the control go red."""
    from factlattice_engine.reviewer import PROFILE_PATH
    return PROFILE_PATH


def _check_reviewer_shapes(run: _CaseRun, a: dict) -> None:
    import tempfile

    from factlattice.corpus import parse as corpus_parse
    from factlattice.parser import parse_text
    from factlattice.parser_config import ParserConfig
    from factlattice.profile import load_profile
    from factlattice_engine.lint.profile_rules import (
        KIND_MISSING_REQUIRED_PROPERTY, find_profile_violations, profile_is_loaded)

    where = "assert reviewer_shapes"
    ppath = reviewer_profile_path()
    try:
        profile = load_profile(ppath)
        with tempfile.TemporaryDirectory(prefix="flsc-reviewer-") as d:
            f = os.path.join(d, "case.lts")
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(a["given_document"])
            pc = corpus_parse(paths=[f], profile=profile)
    except Exception as exc:  # noqa: BLE001 -- a profile that cannot load is a FAILED case, never a pass
        run.fail("the reviewer profile loads and the case parses", "loaded",
                 f"{type(exc).__name__}: {exc}", where=where)
        return
    run.check("the reviewer profile is in force", True, profile_is_loaded(pc), where=where)
    run.check("the case holds exactly the declared node shapes",
              dict(sorted(a["given_node_prefixes"].items())),
              dict(sorted((n.id, n.type_prefix) for n in pc.nodes)), where=where)
    got = sorted([f.node_id, f.detail.get("property")]
                 for f in find_profile_violations(pc, kinds=[KIND_MISSING_REQUIRED_PROPERTY]))
    run.check("V3 (missing required property) findings", sorted(a["expect_v3"]), got, where=where)
    parsed = parse_text(a["given_document"], "case.lts", config=ParserConfig.from_profile(profile=profile))
    if a.get("expect_strict_mode"):
        run.check("the case is parsed in strict mode (lts 1.1)", "strict", parsed.mode, where=where)
    run.check("undeclared keys", sorted(a.get("expect_undeclared_keys", [])),
              sorted(f.key for f in parsed.vocabulary_findings if f.kind == "undeclared_key"), where=where)


def _check_assertion(run: _CaseRun, a: dict, ctx: dict) -> None:
    kind = a.get("assert")
    if kind == "reviewer_shapes":
        _check_reviewer_shapes(run, a)
        return
    if _check_profile_assertion(run, a, kind):
        return
    if _check_era1_assertion(run, a, kind):
        return
    oracle = ctx["oracle"]
    parsed = ctx["parsed"]
    nodes_by_id = ctx["nodes_by_id"]
    where = f"assert {kind}"

    if kind == "document":
        doc = oracle["graph"]["document"]
        run.check("frontmatter", doc["frontmatter"], parsed.frontmatter, where=where)
        run.check("domain", doc["domain"], parsed.domain, where=where)
        run.check("document_tag", doc["document_tag"], parsed.document_tag, where=where)
    elif kind == "all_nodes_document_tag":
        bad = sorted(n.id for n in parsed.nodes if n.document_tag != a["value"])
        run.check("every node carries the document tag", [], bad, where=where)
    elif kind == "node_count":
        run.check("node_count", a["value"], len(parsed.nodes), where=where)
    elif kind == "edge_count":
        run.check("edge_count", a["value"], len(parsed.edges), where=where)
    elif kind == "finding_count":
        run.check("finding_count", a["value"], len(parsed.findings), where=where)
    elif kind == "flagged_line_count":
        run.check("flagged_line_count", a["value"],
                  sum(1 for v in ctx["verdicts"] if v.unaccounted), where=where)
    elif kind == "line_count":
        run.check("line_count", a["value"], len(ctx["verdicts"]), where=where)
    elif kind == "properties_absent":
        node = nodes_by_id.get(a["node"])
        present = sorted(k for k in a["keys"] if node is not None and k in node.properties)
        run.check(f"{a['node']}: keys absent", [], present, where=where)
    elif kind == "property_value":
        # EXACT value, not a prefix. Added for the WRAPPED-VALUE case: the defect it
        # pins truncates a value to its first physical line, which every substring or
        # `startswith` check in the world still passes.
        node = nodes_by_id.get(a["node"])
        got = node.properties.get(a["key"]) if node else None
        run.check(f"{a['node']}.{a['key']}", a["value"], got, where=where)
    elif kind == "property_is_mapping":
        node = nodes_by_id.get(a["node"])
        val = node.properties.get(a["key"]) if node else None
        run.check(f"{a['node']}.{a['key']} is a mapping", "dict", type(val).__name__,
                  where=where)
    elif kind == "mapping_subkey":
        node = nodes_by_id.get(a["node"])
        val = node.properties.get(a["key"]) if node else None
        got = val.get(a["subkey"]) if isinstance(val, dict) else None
        run.check(f"{a['node']}.{a['key']}[{a['subkey']!r}]", a["value"], got, where=where)
    elif kind == "no_edge_matching":
        hits = [_edge_tuple(e) for e in parsed.edges if _edge_matches(e, a["where"])]
        run.check(f"no edge matching {a['where']}", [], hits, where=where)
    elif kind == "no_property_anywhere":
        exempt = set(a.get("except_nodes") or [])
        hits = sorted(
            f"{n.id}.{k}" for n in parsed.nodes if n.id not in exempt
            for k in a["keys"] if k in n.properties)
        run.check(f"no node carries {a['keys']} as a top-level property", [], hits,
                  where=where)
    elif kind == "finding":
        matches = [f for f in parsed.findings
                   if f.kind == a["kind"] and f.node_id == a["node"] and f.key == a["key"]]
        if len(matches) != 1:
            run.fail("exactly one matching finding",
                     f"1 {a['kind']} on {a['node']}.{a['key']}", f"{len(matches)}",
                     where=where)
            return
        f = matches[0]
        run.check("finding.source_line", a["source_line"], f.source_line, where=where)
        run.check("finding.conflict_line", a["conflict_line"],
                  f.detail.get("conflict_line"), where=where)
    else:
        run.fail("known assertion kind", "one of the documented kinds", kind, where=where)


# ---------------------------------------------------------------------------
# Oracle self-consistency — the partition must be TOTAL
# ---------------------------------------------------------------------------
def _partition_failures(oracle: dict, line_total: int, node_ids: set) -> List[dict]:
    """Every line and every node belongs to EXACTLY ONE case.

    Checked before any case runs, so a line cannot be quietly dropped from the oracle to
    make a failure disappear — the drop itself becomes the failure.
    """
    out: List[dict] = []
    seen_lines: Dict[int, str] = {}
    seen_nodes: Dict[str, str] = {}
    for case in oracle["cases"]:
        cid = case["case_id"]
        for ln in case.get("lines", []):
            if ln in seen_lines:
                out.append({"check": "line owned by exactly one case", "expected": f"line {ln} once",
                            "produced": f"claimed by {seen_lines[ln]} and {cid}"})
            seen_lines[ln] = cid
        for nid in case.get("nodes", []):
            if nid in seen_nodes:
                out.append({"check": "node owned by exactly one case", "expected": f"{nid} once",
                            "produced": f"claimed by {seen_nodes[nid]} and {cid}"})
            seen_nodes[nid] = cid

    missing_lines = sorted(set(range(1, line_total + 1)) - set(seen_lines))
    if missing_lines:
        out.append({"check": "every source line is owned by a case",
                    "expected": [], "produced": missing_lines})
    extra_lines = sorted(ln for ln in seen_lines if not 1 <= ln <= line_total)
    if extra_lines:
        out.append({"check": "no case claims a line outside the file",
                    "expected": [], "produced": extra_lines})

    declared_nodes = set(oracle["graph"]["nodes"])
    if set(seen_nodes) != declared_nodes:
        out.append({"check": "every declared node is owned by a case",
                    "expected": sorted(declared_nodes), "produced": sorted(seen_nodes)})
    if declared_nodes != node_ids:
        out.append({"check": "the oracle's node set equals the parsed node set",
                    "expected": sorted(declared_nodes), "produced": sorted(node_ids)})
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def run_selfcheck(directory: Optional[str] = None) -> dict:
    """Re-parse the control corpus and check the result against the hand-authored oracle.

    Returns a structured verdict::

        {"success": bool, "verdict": "PASS"|"FAIL"|"CONTROL_MODIFIED",
         "control_digest": "sha256:…", "digest_ok": bool,
         "measured_against": "silent-loss-classes v4 …",
         "case_count": int, "passed": int, "failed": int,
         "cases": [{"case_id", "title", "kind", "sl_classes", "result",
                    "checks", "failures": [{"check","expected","produced","where"}]}],
         "limit": "…"}

    `directory` overrides where the control PAIR is read from. It is NOT an escape
    hatch: `CONTROL_DIGEST` is a fixed constant, so pointing this at any other pair
    yields CONTROL_MODIFIED, never a pass.
    """
    d = directory or control_dir()
    base = {
        "measured_against": None,
        "limit": _LIMIT,
        "control_dir": d,
    }

    try:
        digest = compute_control_digest(d)
    except OSError as exc:
        return {**base, "success": False, "verdict": VERDICT_CONTROL_MODIFIED,
                "digest_ok": False, "control_digest": None,
                "expected_digest": CONTROL_DIGEST, "case_count": 0,
                "passed": 0, "failed": 0, "cases": [],
                "error": f"control pair unreadable: {type(exc).__name__}: {exc}"}

    if digest != CONTROL_DIGEST:
        # LOUD and DISTINCT. Never a silent pass, and never reported as a case failure:
        # the control itself moved, so nothing downstream of here means anything.
        return {**base, "success": False, "verdict": VERDICT_CONTROL_MODIFIED,
                "digest_ok": False, "control_digest": digest,
                "expected_digest": CONTROL_DIGEST, "case_count": 0,
                "passed": 0, "failed": 0, "cases": [],
                "error": ("the control PAIR does not hash to CONTROL_DIGEST — the corpus "
                          "or its oracle changed. NO cases were run; this is not a parser "
                          "result. Reconcile the pair, or, if the change is deliberate, "
                          "update CONTROL_DIGEST in factlattice_engine/selfcheck as a "
                          "reviewed code diff.")}

    with open(os.path.join(d, _CONTROL_EXPECTED), "r", encoding="utf-8") as fh:
        oracle = json.load(fh)          # ⛔ `json`, never the system under test
    base["measured_against"] = oracle.get("measured_against")

    parsed = parse_file(os.path.join(d, _CONTROL_LTS))
    verdicts = account_file(parsed.coverage)
    nodes_by_id = {n.id: n for n in parsed.nodes}

    edges_by_line: Dict[int, list] = {}
    for e in parsed.edges:
        edges_by_line.setdefault(e.source_line, []).append(e)

    ctx = {"oracle": oracle, "parsed": parsed, "verdicts": verdicts,
           "nodes_by_id": nodes_by_id}

    results: List[dict] = []

    partition = _partition_failures(oracle, len(verdicts), set(nodes_by_id))
    results.append({
        "case_id": "ORACLE-PARTITION",
        "title": "every source line and every node is owned by exactly one case",
        "kind": "invariant", "sl_classes": [],
        "result": VERDICT_PASS if not partition else VERDICT_FAIL,
        "checks": 4, "failures": partition,
    })

    prop_claims: Dict[str, set] = {}
    for spec in oracle["cases"]:
        run = _CaseRun(spec)
        for ln in spec.get("lines", []):
            decl = oracle["lines"].get(str(ln))
            if decl is None:
                run.fail("line has a declared disposition", f"an entry for line {ln}",
                         "absent from `lines`", where=f"line {ln}")
                continue
            _check_line(run, ln, decl, verdicts, edges_by_line, nodes_by_id, prop_claims)
        for nid in spec.get("nodes", []):
            _check_node(run, nid, oracle["graph"]["nodes"][nid], nodes_by_id)
        for a in spec.get("assertions", []):
            _check_assertion(run, a, ctx)
        results.append({
            "case_id": spec["case_id"], "title": spec.get("title"),
            "kind": spec.get("kind"), "sl_classes": spec.get("sl_classes", []),
            "detection_status": spec.get("detection_status"),
            "result": VERDICT_PASS if not run.failures else VERDICT_FAIL,
            "checks": run.checks, "failures": run.failures,
        })

    # Closure the other way: no node may carry a property no line CLAIMED to set. This
    # is what catches a property appearing from nowhere — the mirror of a property going
    # missing, and the half a per-node equality check on its own would still pass.
    unclaimed = sorted(
        f"{nid}.{k}" for nid, n in nodes_by_id.items()
        for k in n.properties if k not in prop_claims.get(nid, set()))
    results.append({
        "case_id": "LINE-PROPERTY-CLOSURE",
        "title": "every parsed property was claimed by a declared source line",
        "kind": "invariant", "sl_classes": [],
        "result": VERDICT_PASS if not unclaimed else VERDICT_FAIL,
        "checks": 1,
        "failures": [] if not unclaimed else [{
            "check": "no property exists that no line claimed to set",
            "expected": [], "produced": unclaimed}],
    })

    failed = sum(1 for r in results if r["result"] == VERDICT_FAIL)
    verdict = VERDICT_PASS if failed == 0 else VERDICT_FAIL
    return {**base, "success": verdict == VERDICT_PASS, "verdict": verdict,
            "digest_ok": True, "control_digest": digest,
            "expected_digest": CONTROL_DIGEST,
            "case_count": len(results), "passed": len(results) - failed,
            "failed": failed, "cases": results}


def render_text(result: dict) -> str:
    """One-screen receipt. Carries the digest on PASS and on FAIL, so two runs weeks
    apart can be compared for whether they tested the SAME control."""
    lines = [f"lts-engine parser self-check: {result['verdict']}",
             f"  control  : {result.get('control_digest')}",
             f"  expected : {result.get('expected_digest')}",
             f"  classes  : {result.get('measured_against')}"]
    if result["verdict"] == VERDICT_CONTROL_MODIFIED:
        lines.append(f"  ⛔ {result.get('error')}")
        return "\n".join(lines)
    lines.append(f"  cases    : {result['passed']}/{result['case_count']} passed")
    for c in result["cases"]:
        mark = "PASS" if c["result"] == VERDICT_PASS else "FAIL"
        lines.append(f"  [{mark}] {c['case_id']}  ({c['checks']} checks)")
        for f in c["failures"]:
            lines.append(f"          {f.get('where','')}: {f['check']}")
            lines.append(f"            expected: {f['expected']!r}")
            lines.append(f"            produced: {f['produced']!r}")
    lines.append(f"  limit    : {result['limit']}")
    return "\n".join(lines)


__all__ = ["run_selfcheck", "render_text", "compute_control_digest",
           "CONTROL_DIGEST", "VERDICT_PASS", "VERDICT_FAIL", "VERDICT_CONTROL_MODIFIED"]
