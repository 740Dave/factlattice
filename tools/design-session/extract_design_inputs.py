#!/usr/bin/env python3
"""Extract the HUMAN's design statements from a Claude Code session transcript.

WHY THIS EXISTS
---------------
Measured 2026-08-15, twice in one session:

  1. The stakeholder specified a message vocabulary. Seven hours later the architect
     re-derived a MORE COMPLEX version of it, having received the original.
     Recovered only because the stakeholder told the architect to search its transaction
     log for the vocabulary (the authors' paraphrase, not his words).
  2. The stakeholder proposed six-month retention. The architect raised an objection that
     was WRONG, the stakeholder dropped the feature rather than argue, and the architect
     then solved the same problem independently four sections later.

Both are the same root: a design input that lives ONLY in conversation is
re-derived — badly — by the same instance that received it.

THE SPLIT THIS TOOL ENFORCES
----------------------------
EXTRACTION is deterministic and belongs in code: this script. Zero tokens,
mechanically faithful, repeatable.

COMPARISON is judgment and belongs to a model — but NOT to the instance that
authored the spec. That instance cannot audit itself: its memory of the
conversation is the thing that failed. Hand this output to a SUBAGENT with no
conversational context. It has no recollection that something "was addressed"
and no investment in the spec being complete.

  Deterministic code for extraction, a model for judgment, and light from
  outside the author's own self: the two principles, composed.

See docs/SPEC-AUDIT-STEPS.md for the governing rules (audit the spec against the
stakeholder's own words; an interpretation of his words is an inference). This
file is their mechanism, not their statement.

⛔ AUDIT FOUR DIRECTIONS
-----------------------
  MISSING   — the human said it; the spec does not carry it.
  ALTERED   — the spec carries a CHANGED version of what he said.
  INVENTED  — the spec asserts something with NO origin in anything he said.
  AMBIGUOUS — he said something admitting MORE THAN ONE valid reading, and the
              spec silently PICKED ONE without recording that it chose.

INVENTED is not an afterthought. On 2026-08-15 every one of the architect's
three known failures was an INVENTION (an over-specified attempt event, per-site
service replication, a device-side retention requirement), not a loss. A
missing-only check would have caught NONE of them.

AMBIGUOUS is the stakeholder's addition and it is the only PREVENTIVE one — the other
three catch an error after it is made; this one raises a question before it
becomes one. The authors' paraphrase of his framing (not a quotation): he said
something vague, the spec took one reading of it, other readings were valid, and
he has to be asked to be more specific about the point in question.

It has a home in the existing discipline: a spec carries its evidence, and already
requires that "every inference [is] recorded AS an inference rather than promoted
to fact." That was being honoured for DATASHEET inferences (the 3.3VT level claim
was downgraded from fact to inference on exactly those grounds) and NOT for
INTERPRETATIONS OF THE HUMAN'S WORDS — which are the same epistemic class.

Measured examples from the session that produced this tool:

  * a remark that no sensor would ever hold more than 10 fingerprints (the authors'
    paraphrase, not his words) — read as 10 TEMPLATES;
    he meant 10 IDENTITIES x 2 fingers = 20. Arithmetic was written into a
    decision node on the wrong reading before he corrected it. AMBIGUOUS would
    have caught it BEFORE it propagated.
  * a list of tabs for the discrete things, ending in "etc." (the authors'
    paraphrase, not his words) — the "etc." was filled with three DERIVED tabs. They may be right; they were
    presented as if specified.
  * `location` in the NAMESPACE vs the PAYLOAD — he never said which. The
    architect decided, and the trade-off (relocation becomes a config change) is
    real and was never put to him.

⛔ OUTPUT SHAPE: an AMBIGUOUS finding is a QUESTION, not a defect. Its natural
destination is a decision-support doc — a queue of "be specific about X" items
the human answers in one pass, rather than four interruptions.

USAGE
-----
    # skill-bracketed block (preferred — the begin-design-session skill opened the bracket)
    python3 extract_design_inputs.py TRANSCRIPT.jsonl --feature widget-identity

    # no bracket present: bound it explicitly
    python3 extract_design_inputs.py TRANSCRIPT.jsonl --from-match "sensor" --to-end

    # machine-readable, for a subagent brief
    python3 extract_design_inputs.py TRANSCRIPT.jsonl --feature X --json

    # run the contamination + channel control (no transcript needed)
    python3 extract_design_inputs.py --self-test

BRACKETING A DESIGN CONVERSATION — THE SENTINEL PHRASE
------------------------------------------------------
⛔ THE BOUNDARY IS A PHRASE THE ASSISTANT WRITES INTO ITS OWN REPLY. It is not a
record type, not a command, not a harness artefact. The stakeholder's design, 2026-08-15:

    [the authors' paraphrase, not his words: when he says he wants to design
     something, the assistant calls the begin-design-session skill, which has it
     write a key start phrase in its reply; the phrase is kept in the transcript,
     with a similar one when the session ends, and can be checked against any
     live transcript.]

⛔ THIS FILE IS THE ONE AUTHORITY FOR THE EXACT PHRASE. skills/begin-design-session/SKILL.md
and skills/end-design-session/SKILL.md
CITE it (a test, tests/test_design_session_tools.py, fails if they drift); they do not restate it. Two copies of a sentinel drift, and a drifted
sentinel brackets nothing while looking correct.

    ⌜DESIGN-SESSION-BEGIN⌝ feature: <feature-name>
    ⌞DESIGN-SESSION-END⌟ feature: <feature-name>

WHY A PHRASE BEATS THE RECORD SHAPE (an earlier design, rejected)
  * IT DOES NOT DEPEND ON HARNESS INTERNALS. Keying on the `<command-name>`
    record shape keys on an implementation detail that can change silently,
    taking the mechanism with it and reporting nothing.
  * IT IS VERIFIABLE AGAINST ANY LIVE TRANSCRIPT — the stakeholder's words and the actual
    point. `grep -n 'DESIGN-SESSION-BEGIN' <session>.jsonl` and you are done. No
    knowledge of record types, no fixture derived from an assumed shape.
  * ⭐ IT IS VISIBLE TO THE HUMAN. He sees the bracket open in the reply and knows
    it is live. A record-shaped marker is invisible to everyone, including him, so
    a silently-failed bracket is discovered only when an audit returns nothing.
  * IT IS SIMPLER. The extractor needs no knowledge of command records at all.

TWO RECORD ROLES, STILL CLEANLY SEPARATED
  BOUNDARIES are read from ASSISTANT records. STATEMENTS are collected only from
  HUMAN turns. An assistant record never becomes a statement and a human turn
  never opens a bracket — so the human quoting the phrase back cannot move the
  boundary, and the assistant's own marker line can never be mistaken for a
  design input.

⛔ THE CONTAMINATION PROBLEM, WHICH IS NOT HYPOTHETICAL
  The assistant WILL write the sentinel phrase in later replies — every
  discussion of this mechanism does, including the conversation that designed it.
  A naive first-match / last-match scan mis-brackets on its own documentation.
  FOUR RULES make a mention non-qualifying, and a mention must clear ALL of them:

    1. It must appear in an ASSISTANT record. A human mention is never a boundary.
    2. It must START ITS OWN LINE (at most 3 leading spaces, for markdown). Any
       prose or inline-code wrapper — `` `⌜DESIGN-SESSION-BEGIN⌝` `` — puts a
       backtick before it and disqualifies it.
    3. It must NOT be inside a fenced code block (``` or ~~~) or a markdown
       blockquote (`>`). Those are how a reply QUOTES a phrase rather than
       ASSERTS it.
    4. It must carry `feature: <name>`. A bare mention of the marker, which is
       what discussion produces, does not qualify.

  Then a STATE MACHINE pairs them, PER FEATURE NAME (changed 2026-11-24; it used to allow
  one open bracket in all): a BEGIN for feature F opens F's bracket even while
  another feature's bracket is open; an END for F closes F only. A second BEGIN
  for F while F is open is a re-mention and is IGNORED AND COUNTED; an END for a
  feature with nothing open likewise. Silent scanning is what let the earlier
  defects hide, so both are surfaced in the reconciliation.

  SELECTING (changed 2026-11-24): `--feature F` matches by EXACT name (it was a
  substring, so `x` matched `x-two`) and takes F's BEGIN and the first END for F
  after it. With no `--feature` the first bracket is used, closed by the first END
  for its own feature. ⛔ `--feature F` with no bracket and no typed block for F
  (and no --from-match) is REFUSED: empty slice, exit status 2, stderr names the
  features that do have a bracket. It never falls back to the whole transcript.
  In an interleaved transcript, the outer bracket's slice contains the inner
  session's human turns (they are interleaved by position and cannot be told apart).

  MEASURED, and deliberately against the hardest available case: the transcript
  of the session that BUILT this mechanism, in which the sentinel
  phrase occurs SIX times — in prose, in fences, and in the skill files being
  authored — and no design session was ever opened. Result: 6 occurrences, ZERO
  qualifying boundaries. The rules survive their own documentation, which is the
  only contamination test that means anything.

⛔ THE TRIGGERING TURN IS INSIDE THE BRACKET. The human says "let's design X",
and the assistant's reply — carrying the BEGIN marker — necessarily comes after
it. Bounding strictly after the marker would therefore drop the request that
opened the session, which is routinely the single most load-bearing turn in it.
So the human turn IMMEDIATELY PRECEDING the BEGIN marker is included, and
labelled `trigger` in the JSON output.

FALLBACK — TYPED SENTINELS (still supported). The human types, on its own line:

    FEATURE DEFINITION START: <feature-name>
    ...
    FEATURE DEFINITION STOP: <feature-name>

Case-insensitive. Useful for bracketing after the fact, or across sessions.
The stakeholder's original framing, in the authors' paraphrase (not his words):
a "feature definition" mode with start and stop markers, as plain strings that
can be found with grep in the transcript.

⛔ THE HUMAN DOES NOT ONLY SPEAK IN `type: "user"` RECORDS
----------------------------------------------------------
This is the defect that actually cost statements, and it is worth stating at
length because two successive diagnoses of it were wrong in opposite directions.

A message the human types WHILE THE ASSISTANT IS WORKING is QUEUED. The queue is
journalled as its own record type:

    {"type": "queue-operation", "operation": "enqueue",
     "timestamp": "...", "sessionId": "...",
     "content": "So unlike some other hardware elements, we will need an actual
                 visual editor …"}

⛔ `content` IS AT THE TOP LEVEL, NOT UNDER `message`. There is no `message` key
at all, so the usual content-flattening returns '' even if the type filter is
relaxed. Both had to change.

`operation` is one of `enqueue` (the human typed it), `remove` (it left the
queue) and `dequeue` (bare, no content). ONLY `enqueue` is read; `remove`
carries a verbatim copy of the same text and would double every queued message.

If the queue drains normally, the SAME text later reappears as an ordinary
`type: "user"` record and the dedup collapses the pair. IT DOES NOT ALWAYS
DRAIN. Measured on the session that built this tool, SEVEN of the stakeholder's design statements
existed ONLY as `queue-operation` records and appear nowhere in the `user`
stream — including three that are quoted as load-bearing evidence in the
governing corpus nodes. Every bracketed line below is the authors' paraphrase,
not his words; none of it has been checked here against a transcript:

    line 366  [paraphrase: a remark that no sensor would hold more than 10
               fingerprints]   <- the 10-templates-vs-10-identities ambiguity
    line 421  [paraphrase: he accepts a (person_id, finger) key, while noting this is a toy]
    line 513  [paraphrase: a need to identify every sensor in use, and a rule that
               a user and fingers are auto-deleted after six months without
               authenticating]
    line 567  [paraphrase: for the initial build, a set of tabs]   <- the "etc." case
    line 606, 850, 881

⛔ AND THESE SKEW LOAD-BEARING, which is not a coincidence. A human interrupts
a working assistant precisely when something important has just occurred to him.
The class that was being dropped was the class most worth keeping.

Census across every transcript on the author's machine (2026-08-15): 29,313 `user`
records and 16,487 `queue-operation` records. The dropped channel was more than
a third the size of the one being read.

TWO WRONG DIAGNOSES, RECORDED BECAUSE THE PATTERN REPEATS
  * An independent auditor said the tool "returns 21 of 28 … it only reads
    `type: user` records". THE COUNT AND THE CAUSE WERE BOTH CORRECT.
  * A subsequent correction asserted the auditor was wrong and located the bug
    in the admit/reject predicate's 400-character head check instead. That was
    checked empirically against all 54 transcripts on the author's machine and found
    ZERO records of the shape it described. The "correction" was confident,
    specific, plausible and wrong — and it would have shipped a fix that
    recovered nothing while closing the ticket.

The lesson is not "trust auditors". It is that BOTH claims were cheap to test
against the file and only one of them was tested. Test the claim, not the
claimant.

⛔ WHO SPOKE (the origin rule)
-----------------------------
Claude Code marks each user record with `origin.kind`. Only `human` counts as
the stakeholder's. `peer` (another agent), `task-notification`,
`auto-continuation`, tool results (no origin), meta records, compaction
summaries and anything else are EXCLUDED and counted by kind. A queued record
has no origin, so it counts only when its text EQUALS (after whitespace
normalisation) a human-origin user turn or a human-origin queued_command
attachment, or, before the first origin-marked record, an admitted early user
turn (a fallback record); no prefix or extension credit. A mid-turn message the
stakeholder types is a `queued_command` attachment, marked one level down at
`attachment.origin.kind`; only `human` counts (text: attachment.prompt). A transcript with NO origin field on any user record (an older format)
falls back to the old rule and says "origin unknown: this transcript predates
origin marking; a turn's speaker is not verified". `excluded_kind()` is the one
decision; the verifier imports it.

⛔ TRAPS THIS SCRIPT HANDLES, each one observed
----------------------------------------------
  * A task-notification arrives as a `type: "user"` record. It is NOT the human.
    It also arrives as a `queue-operation` — the same filter list covers both.
    (2 of 6 hits when grepping this session for "rbac" were notifications.)
  * A system-reminder arrives the same way. Also not the human.
  * A local-command stdout block (`<local-command-stdout>`) is not the human.
  * The ASSISTANT quoting a sentinel back in its own reply would be found by a
    naive grep. Only `type: "user"` records are considered for statements, and
    even then the filters below apply.
  * A slash-command invocation (`<command-name>`) is not a design statement —
    but see the BOUNDARY note above: it is still read, for a different purpose.
  * `/compact` carries a long human-written directive in `<command-args>`. It
    reads like a design statement and is not one; it is instructions to a
    summariser. Measured: 51 such records across the author's transcripts.

⛔ THE TRAP THAT COST THE MOST, AND IT WAS NOT A CODE BUG
---------------------------------------------------------
An independent auditor reported on 2026-08-15 that this tool "returns 21 of 28
of the human's design statements" — a 25% silent loss. It did not. Reproduced
2026-08-15 by replaying the transcript at successive truncation points:

    cut@ 910 lines -> 22 statements   <- what the auditor's TOOL RUN saw
    cut@1042 lines -> 28 statements   <- what the auditor's LATER GREP saw

A live session transcript is an APPEND-ONLY FILE THAT IS STILL GROWING. The
auditor ran the extractor, then grepped the file some minutes later, and
attributed the difference between the two reads to a defect in the tool. Every
statement was present at the moment it ran. The tool lost nothing.

That is a genuinely hard failure to see, and the remedy is not care — it is
INSTRUMENTATION. Two of them, both below:

  * Every run reports a TRANSCRIPT FINGERPRINT — byte length, record count,
    mtime. Two runs that disagree now disagree VISIBLY, and a grown file is
    distinguishable from a lossy tool at a glance.
  * Every run reports a COUNTED RECONCILIATION — records seen, admitted,
    rejected by class, deduped — which must add up. The original tool could have
    lost a whole class of input and said nothing; a filter list is a closed set
    with no instrument watching its boundary, so silence was structural.

  Corollary for anyone auditing this tool: compare the FINGERPRINTS of the two
  reads before concluding anything about extraction.

⛔ AND THE REJECTION PATH ITSELF WAS FRAGILE
---------------------------------------------
Separately from the above, the admit/reject decision used to run on the raw
text's first 400 characters, and rejected the ENTIRE record if any machinery
marker appeared there. A record that carried machinery AND real human text
would have been discarded whole. Measured across every transcript on this
author's machine (2026-08-15): ZERO records of that shape exist today — so this change
recovers 0 statements by itself, and it is recorded as costing nothing rather
than as a fix. It is kept because the cost is one function and the failure mode
is a silent whole-record loss. Machinery is now STRIPPED first and the decision
is made on the RESIDUE.

⛔ DO NOT MISTAKE THIS FOR THE FIX. It is the change that was PREDICTED to
matter and did not; the queue-operation channel above is the one that did. Both
are in this file so the next reader can see which prediction paid.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ── machinery classification ────────────────────────────────────────────────
#
# Three kinds, because they must be handled three different ways.

#: PAIRED blocks. Removable in place, from anywhere in the text, leaving any
#: human prose around them intact.
_PAIRED_TAGS = (
    "system-reminder",
    "local-command-caveat",
    "local-command-stdout",
    "command-name",
    "command-message",
    "command-args",
    "task-notification",
    "function_results",
)

#: WHOLE-RECORD markers. Unpaired; when the residue STARTS with one of these the
#: record is machinery end to end and is dropped.
_WHOLE_RECORD = (
    "Base directory for this skill:",      # a Skill invocation's body
    "Result of calling the",
    "[SYSTEM NOTIFICATION - NOT USER INPUT]",
    "[Request interrupted by user]",
)

#: TAIL markers. The mid-turn delivery WRAPPER. A message the human sends while
#: the assistant is working appears TWICE: once as its own record, and once
#: folded into the next turn with this explainer stapled on. Without stripping
#: it the two copies differ textually, the dedup misses, and the HARNESS's own
#: prose gets attributed to the human. Observed at 708/712.
_TAIL = (
    "This is how Claude Code surfaces messages",
)

#: Stripped from the FRONT of a mid-turn delivery, same reason as above.
_LEADING_NOISE = (
    "The user sent a new message while you were working:",
)

#: Every entry above was added because it ACTUALLY LEAKED in a test run, not
#: because it was anticipated. First run against this session admitted 4 false
#: positives out of 26 — two Skill bodies, two local-command caveats, and an
#: interrupt marker — each of which would have been handed to an auditing
#: subagent AS THE HUMAN'S WORDS. A filter list is a closed set with no
#: instrument watching its boundary, so it will leak again: when it does, add
#: the marker here rather than post-filtering at the call site. The
#: reconciliation counters below exist so the NEXT leak is visible as a number
#: instead of as a silent absence.

_START = re.compile(r"FEATURE\s+DEFINITION\s+START\s*:?\s*(?P<name>[\w.\-/]+)?", re.I)
_STOP = re.compile(r"FEATURE\s+DEFINITION\s+STOP\s*:?\s*(?P<name>[\w.\-/]+)?", re.I)

#: ⛔ THE SENTINEL PHRASE. THIS IS THE ONE AUTHORITY — both skills cite these two
#: constants rather than restating them. See the module docstring for the design.
SENTINEL_BEGIN = "⌜DESIGN-SESSION-BEGIN⌝"
SENTINEL_END = "⌞DESIGN-SESSION-END⌟"

#: A qualifying marker starts its own line (rule 2) and carries a feature (rule 4).
#: Rules 1 and 3 — assistant-only, and not inside a fence or blockquote — are
#: enforced by the caller, because they are properties of the RECORD and of the
#: SURROUNDING LINES respectively, not of the line in isolation.
_SENTINEL_LINE = re.compile(
    r"[ \t]{0,3}(?P<marker>%s|%s)\s*feature\s*:\s*(?P<name>[\w.\-/]+)"
    % (re.escape(SENTINEL_BEGIN), re.escape(SENTINEL_END))
)


#: ⛔ THE `index` INVARIANT — THIS IS THE ONE AUTHORITY.
#: -------------------------------------------------------------------------
#: `Statement.index` and `Boundary.index` are the 1-BASED `.jsonl` line number
#: of the record — the SAME number `grep -n <session>.jsonl` prints and a human
#: reads off the file. It is NOT a 0-based `enumerate` counter and NOT a record
#: ordinal. The whole design rests on greppability ("grep -n 'DESIGN-SESSION-
#: BEGIN' <session>.jsonl and you are done", module docstring; the design-session
#: skills tell the auditor to grep-verify line numbers), and `grep -n` is 1-based.
#:
#: ⛔ FORK NOTICE. `verify_dsd_citations.py` reads the SAME transcript to check a
#: DSD's `**Stakeholder said** (line N)` cites, keying `human_turns` by the SAME line
#: number. The two files MUST agree on the base, or the verifier calls a
#: correctly-grepped (1-based) citation a mismatch. This invariant is cited there
#: by reference; the cross-implementation test `test_transcript_line_numbers.py`
#: FAILS if EITHER file drifts off 1-based. Measured 2026-08-19: both files used
#: bare `enumerate` (0-based), so every ledgered audit's line cites were low by 1
#: — self-consistent between the two tools, wrong against every human `grep -n`.
@dataclass
class Statement:
    index: int
    text: str
    channel: str = "user"     # "user" | "queued"
    role: str = "statement"   # "statement" | "trigger"


@dataclass
class Boundary:
    index: int
    kind: str            # "begin" | "end"
    feature: str | None
    raw: str


@dataclass
class Reconciliation:
    """Every record is accounted for in exactly one bucket. They must sum.

    The tool this replaced could have lost 25% of its input and said nothing,
    because a filter list has no instrument watching its own boundary. These
    counters ARE that instrument: a class-loss now shows up as a bucket that
    grew, rather than as an absence nobody can see.
    """

    records_total: int = 0
    json_undecodable: int = 0
    not_a_human_channel: int = 0
    queue_op_not_enqueue: int = 0
    empty_text: int = 0
    rejected_machinery: dict = field(default_factory=dict)
    admitted_user: int = 0
    admitted_queued: int = 0
    #: of admitted_queued, the `queued_command` attachments (the stakeholder's
    #: mid-turn messages, marked one level down at attachment.origin.kind)
    admitted_queued_command: int = 0
    deduped_exact: int = 0
    deduped_prefix_stub: int = 0
    deduped_prefix_upgrade: int = 0
    deduped_queue_echo: int = 0
    #: ⛔ WHO SPOKE. True when at least one user record carries an `origin` field
    #: (the harness's own marking of a turn's source); then ONLY `origin.kind ==
    #: "human"` counts as the stakeholder's and every other user turn lands in
    #: `excluded_by_kind`. False = an older transcript: today's rule, said loudly.
    origin_marked: bool = False
    excluded_by_kind: dict = field(default_factory=dict)
    #: ⛔ records that came BEFORE origin marking began in a marked transcript
    #: (line < the first origin-marked user record). They fall back to the old
    #: rule, are admitted if not machinery, and are counted here so a reader
    #: sees that part of the transcript's speakers is not verified.
    before_first_marked_fallback: int = 0
    boundaries_seen: int = 0
    #: A sentinel mention that did NOT move the bracket. Surfaced rather than
    #: silently dropped: a nonzero count here on a session that produced no
    #: bracket is the signal that the assistant discussed the mechanism instead
    #: of invoking it — which is a real and easily-missed way to get an empty
    #: audit that looks like "the human said nothing".
    sentinel_ignored_reopen: int = 0
    sentinel_ignored_unopened: int = 0

    def reject(self, why: str) -> None:
        self.rejected_machinery[why] = self.rejected_machinery.get(why, 0) + 1

    def exclude(self, kind: str) -> None:
        self.excluded_by_kind[kind] = self.excluded_by_kind.get(kind, 0) + 1

    @property
    def excluded_total(self) -> int:
        return sum(self.excluded_by_kind.values())

    @property
    def rejected_total(self) -> int:
        return sum(self.rejected_machinery.values())

    @property
    def admitted(self) -> int:
        return self.admitted_user + self.admitted_queued

    def balances(self, emitted: int) -> bool:
        """Every record lands in exactly one bucket, and admitted - deduped == emitted.

        ⛔ `not_a_human_channel` is the bucket that mattered. When the queue
        channel was unread it silently absorbed 16,487 records machine-wide and
        7 real design statements in the session that exposed it. A bucket that
        large with no name on it is how a class-loss stays invisible.
        """
        seen = (self.json_undecodable + self.not_a_human_channel
                + self.queue_op_not_enqueue + self.empty_text
                + self.excluded_total
                + self.rejected_total + self.admitted)
        deduped = (self.deduped_exact + self.deduped_prefix_stub
                   + self.deduped_prefix_upgrade + self.deduped_queue_echo)
        return seen == self.records_total and self.admitted - deduped == emitted


def _text_of(rec: dict) -> str:
    """Flatten a transcript record's content to plain text, or '' if not text.

    ⛔ TWO PLACES CARRY CONTENT, not one. A `type: "user"` record nests it under
    `message.content`; a `type: "queue-operation"` record puts a bare string at
    the TOP LEVEL and has no `message` key at all. Reading only the nested path
    returns '' for every queued message the human ever typed — silently, because
    '' is also what a legitimately text-free record returns.
    """
    if rec.get("type") == "queue-operation":
        content = rec.get("content")
        return content if isinstance(content, str) else ""
    if rec.get("type") == "attachment":
        # a `queued_command` attachment holds a mid-turn message in `prompt`
        prompt = (rec.get("attachment") or {}).get("prompt")
        if isinstance(prompt, str):
            return prompt
        if isinstance(prompt, list):
            return "\n".join(c.get("text", "") for c in prompt
                             if isinstance(c, dict) and c.get("type") == "text")
        return ""
    msg = rec.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


def sentinels_in(text: str) -> list[tuple[str, str]]:
    """Qualifying sentinel markers in one ASSISTANT reply -> [(kind, feature)].

    ⛔ THE HARD PART IS NOT FINDING THE PHRASE, IT IS IGNORING THE MENTIONS. Any
    reply that DISCUSSES this mechanism contains the phrase, and a naive scan
    brackets on its own documentation. Rules 2, 3 and 4 of the module docstring
    are enforced here; rule 1 (assistant-only) is enforced by the caller, which
    is the only place that knows the record's type.

    The distinction being drawn is ASSERTING the marker versus QUOTING it. A
    reply asserts it by putting it alone at the head of a line with a feature
    name; every natural way of quoting it — inline backticks, a fenced block, a
    blockquote, mid-sentence prose — fails at least one rule.
    """
    out: list[tuple[str, str]] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:                       # rule 3: a fenced block QUOTES
            continue
        if stripped.startswith(">"):       # rule 3: a blockquote QUOTES
            continue
        m = _SENTINEL_LINE.match(line)     # rules 2 + 4: line-initial, has feature
        if not m:
            continue
        kind = "begin" if m.group("marker") == SENTINEL_BEGIN else "end"
        out.append((kind, m.group("name")))
    return out


def _strip_machinery(text: str) -> str:
    """Reduce a record to the human prose it carries, or '' if it carries none.

    ⛔ ORDER IS THE WHOLE POINT. This runs BEFORE the admit/reject decision, not
    after it. The predicate it replaced looked at the raw first 400 characters
    and threw the whole record away on a marker hit — so a record carrying
    machinery AND real text was lost entire, and the loss was invisible.
    """
    # 1. paired blocks, removed in place wherever they sit
    for tag in _PAIRED_TAGS:
        text = re.sub(r"<%s>.*?</%s>" % (tag, tag), " ", text, flags=re.S)
    # 1b. an UNCLOSED opening tag swallows the rest of the record: a truncated
    #     notification is still a notification, and prose after it is unreachable.
    for tag in _PAIRED_TAGS:
        idx = text.find("<%s>" % tag)
        if idx != -1:
            text = text[:idx]
    # 2. leading wrapper prose
    for marker in _LEADING_NOISE:
        idx = text.find(marker)
        if idx != -1:
            text = text[idx + len(marker):]
    # 3. tail wrappers: keep what precedes, drop the record if it leads
    for marker in _TAIL:
        idx = text.find(marker)
        if idx == 0:
            return ""
        if idx > 0:
            text = text[:idx]
    # 4. unpaired whole-record markers
    stripped = text.strip()
    for marker in _WHOLE_RECORD:
        if stripped.startswith(marker):
            return ""
        idx = stripped.find(marker)
        if idx > 0:
            stripped = stripped[:idx]
    return stripped.strip()


#: The one sentence said whenever a transcript cannot tell us who spoke. It is
#: printed by this tool, written into its JSON, and shown in the verifier's banner.
ORIGIN_UNKNOWN_WARNING = ("origin unknown: this transcript predates origin marking; "
                          "a turn's speaker is not verified")

#: Said for a marked transcript's records that come BEFORE its first origin-marked
#: record. They carry the same harness version as the marked ones (measured), so
#: the transcript does not "predate marking": those records simply have no origin.
ORIGIN_PARTIAL_WARNING = ("origin unknown: records before the first origin-marked record; "
                          "their speaker is not verified")

HUMAN_ORIGIN = "human"

#: ⛔ A LEXICAL TRIPWIRE, not a structural answer. Text the Gnomon harness types
#: into a session comes out marked `human` (a user record or a queued_command
#: attachment), so the origin rule admits it. A record whose text BEGINS with one
#: of these prefixes is excluded as `harness-injection:<prefix>`.
#:
#: THE LIST IS DATA. This is the public default: the public harness's own
#: prefixes only. A deployment adds its own, one per line, under a
#: `harness-prefixes:` entry in `private/design-session.local.md` (the private
#: overlay); load_harness_prefixes() reads it and every run names its source
#: ("default" or "default plus overlay"). An overlay that cannot be read is a
#: loud warning and the default applies.
#:
#: Measured on 302 transcripts (statements the origin rule admitted): "[Gnomon]"
#: 537, "[GNOMON" 134 (all "[GNOMON NOTICE]"), "[DSD ANSWERED]" 389,
#: "[scheduled:" 27; "[Gnomon@" 40.
#: LIMITS: it catches only the listed prefixes; it cannot see harness text
#: without one; an operator who types a line starting with one of them is
#: excluded. The structural answer, a harness ledger of injected text, is the
#: harness owner's and is not built. More specific prefixes come first.
DEFAULT_HARNESS_PREFIXES = ("[GNOMON NOTICE]", "[GNOMON", "[Gnomon@", "[Gnomon]",
                            "[DSD ANSWERED]", "[scheduled:")

OVERLAY_RELATIVE = "private/design-session.local.md"
_MIN_PREFIX = 3

#: the active list and where it came from; set by load_harness_prefixes()
_HARNESS = {"prefixes": DEFAULT_HARNESS_PREFIXES,
            "info": {"source": "default", "prefixes": list(DEFAULT_HARNESS_PREFIXES),
                     "overlay": None, "warning": None}}


def default_overlay_path() -> Path:
    return Path(__file__).resolve().parents[2] / OVERLAY_RELATIVE


def _overlay_entries(text: str) -> tuple[list[str], list[str]]:
    """-> (prefixes, warnings) from the `- harness-prefixes:` entry.

    THE ONE LAYOUT THAT IS READ: the `- harness-prefixes:` line, then indented
    `- prefix` items directly under it, no blank line inside, ended by a
    top-level bullet, a heading or the end of the file.

    Only the one layout above is read. Some departures from it warn; any other
    may be read in part, wrongly, or not at all, without a warning. After any
    overlay edit, check the parsed list itself: run the extractor with --json
    and read harness_prefixes.prefixes (the counts line shows only how many
    were added). The warnings below are best-effort and are not a contract."""
    lines = text.splitlines()
    out: list[str] = []
    warns: list[str] = []
    key_at = None
    for n, line in enumerate(lines):
        if re.match(r"^\s*[-*]?\s*harness-prefixes:\s*$", line):
            key_at = n
            break
    if key_at is None:
        for line in lines:
            if re.match(r"^\s*[-*]?\s*harness[-_ ]?prefix\w*\s*:", line, re.I):
                inline = re.match(r"^\s*[-*]?\s*harness-prefixes:\s*\S", line)
                warns.append("OVERLAY: the harness-prefixes entry does not parse (%s); no prefixes were read from it"
                             % ("the prefixes must be indented bullet lines under the key, not on the key's own line"
                                if inline else "the key must be spelled exactly `harness-prefixes:`"))
                break
        return out, warns
    i = key_at + 1
    unindented = False
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^\s+[-*]\s+(.*\S)\s*$", line)
        if not m:
            # a top-level bullet that is not `- key: value` is a prefix written
            # without its indent; `- key: value` is simply the next entry
            if re.match(r"^[-*]\s+\S", line) and not re.match(r"^[-*]\s+[A-Za-z][\w -]*:(\s|$)", line):
                unindented = True
            break
        item = m.group(1).strip().strip("`").strip()
        i += 1
        if item.startswith("<") and item.endswith(">"):
            continue                       # a placeholder copied from the example
        if len(item) < _MIN_PREFIX:
            warns.append("OVERLAY: entry %r is too short to be a prefix (minimum %d characters); ignored"
                         % (item, _MIN_PREFIX))
            continue
        out.append(item)
    if unindented:
        warns.append("OVERLAY: the harness-prefixes entry does not parse (its bullets are not indented under the key); "
                     "those prefixes were not read")
    tail_after_blank = False
    j = i
    if j < len(lines) and lines[j].strip() == "":
        while j < len(lines) and lines[j].strip() == "":
            j += 1
        if j < len(lines) and re.match(r"^\s+[-*]\s+\S", lines[j]):
            tail_after_blank = True
    if tail_after_blank:
        warns.append("OVERLAY: a prefix bullet after a blank line is not part of the entry and was ignored; "
                     "keep the bullets together under `harness-prefixes:`")
    if not out and not warns and not unindented:
        warns.append("OVERLAY: the harness-prefixes entry has no items; no prefixes were read from it")
    return out, warns


def load_harness_prefixes(overlay: Path | None = None, use_overlay: bool = True) -> dict:
    """Set the active prefix list to the default plus the private overlay's
    entries, and return what was done: {source, prefixes, overlay, warning}.

    No overlay at the default location is normal (the default applies, said in
    the output). A path named on purpose that is missing, or any overlay that
    cannot be read, is a warning: never a silent default."""
    prefixes = list(DEFAULT_HARNESS_PREFIXES)
    info = {"source": "default", "prefixes": prefixes, "overlay": None, "warning": None}
    explicit = overlay is not None
    path = overlay if explicit else default_overlay_path()
    if use_overlay:
        warns: list[str] = []
        try:
            if not path.exists():
                if explicit:
                    warns.append("OVERLAY UNREADABLE: %s not found; the default prefix list applies" % path.name)
                path = None
            else:
                text = path.read_text(encoding="utf-8")
                added, w = _overlay_entries(text)
                warns += w
                if added:
                    prefixes += [x for x in added if x not in prefixes]
                    info["source"] = "default+overlay"
                    info["overlay"] = path.name
        except (OSError, UnicodeDecodeError) as exc:
            warns.append("OVERLAY UNREADABLE: %s (%s); the default prefix list applies"
                         % (path.name, type(exc).__name__))
            prefixes[:] = list(DEFAULT_HARNESS_PREFIXES)
            info["source"] = "default"
            info["overlay"] = None
        info["warning"] = "; ".join(warns) if warns else None
    _HARNESS["prefixes"] = tuple(prefixes)
    _HARNESS["info"] = info
    return info


def harness_source_line(info: dict) -> str:
    if info["source"] == "default+overlay":
        n = len(info["prefixes"]) - len(DEFAULT_HARNESS_PREFIXES)
        return "default plus overlay (%d added from %s)" % (n, info["overlay"])
    return "default (%d prefixes)" % len(info["prefixes"])


def harness_prefix(text: str) -> str | None:
    t = text.lstrip()
    for p in _HARNESS["prefixes"]:
        if t.startswith(p):
            return p
    return None


def excluded_kind(rec: dict) -> str | None:
    """⛔ THE ONE DECISION ABOUT WHO SPOKE (the verifier imports this, it does
    not copy it). For a `type: "user"` record of an origin-marked transcript:
    None if the stakeholder's (`origin.kind == "human"`), else the kind it is.

    Measured on 302 real transcripts: `origin.kind` is `human`, `peer` (another
    agent's cross-session message), `task-notification` or `auto-continuation`;
    tool results, meta records, compaction summaries and slash-command echoes
    carry NO origin at all. Everything but `human` is excluded.
    """
    if "origin" in rec:
        o = rec["origin"]
        if not isinstance(o, dict):
            return "origin-malformed"
        k = o.get("kind")
        if k == HUMAN_ORIGIN:
            return None
        return k if isinstance(k, str) and k else "origin-malformed"
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return "no-origin:tool-result"
    if rec.get("isCompactSummary"):
        return "no-origin:compact-summary"
    if rec.get("isMeta"):
        return "no-origin:meta"
    return "no-origin:other"


def is_queued_command(rec: dict) -> bool:
    return (rec.get("type") == "attachment"
            and isinstance((rec.get("attachment")), dict)
            and rec["attachment"].get("type") == "queued_command")


def queued_command_kind(rec: dict) -> str | None:
    """⛔ The mark of a mid-turn message is ONE LEVEL DOWN: `attachment.origin.kind`
    (the attachment record itself carries no `origin`). None if the stakeholder's
    (`human`), else `queued_command:<kind>`, `queued_command:no-origin` or
    `queued_command:origin-malformed`. Measured on 302 transcripts: 5,178
    queued_command attachments; 974 human (humanTurn true), 2,699 peer, 1,440
    task-notification, 65 with no origin."""
    a = rec["attachment"]
    if "origin" not in a:
        return "queued_command:no-origin"
    o = a["origin"]
    if not isinstance(o, dict):
        return "queued_command:origin-malformed"
    k = o.get("kind")
    if k == HUMAN_ORIGIN:
        return None
    return "queued_command:" + k if isinstance(k, str) and k else "queued_command:origin-malformed"


def _squash(text: str) -> str:
    return " ".join(text.split())


def queued_has_human_copy(text: str, human_texts: list[str]) -> bool:
    """A queue-operation record has NO origin field, so by itself it cannot be
    called the stakeholder's. It counts only if its text EQUALS (after
    whitespace normalisation) one of `human_texts`: the texts of human-origin
    user records and human-origin queued_command attachments (scan_origin), plus,
    from the caller and only before the first origin-marked record, admitted
    early user turns. No prefix credit and no extension credit: a queue record
    that extends a human turn carries words no human-origin turn does."""
    q = _squash(text)
    return bool(q) and q in set(human_texts)


def scan_origin(path: Path) -> tuple[bool, list[str], int | None, list[str]]:
    """-> (origin_marked, normalised texts of the human-origin user turns, the
    1-based line of the first origin-marked user record or None, normalised
    texts of the fallback user turns before that record).
    Marked = at least one user record carries an `origin` field."""
    first = None
    humans: list[str] = []
    recs: list[tuple[int, dict]] = []
    with path.open(errors="replace") as fh:
        for n, line in enumerate(fh, start=1):
            if '"user"' not in line and '"queued_command"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if is_queued_command(rec):
                # (Belt-and-braces: collect() and read_turns() exclude a harness-prefixed
                # record BEFORE the queue-equality check, so leaving such text in
                # `humans` would change no outcome; it is kept out here as well.)
                if queued_command_kind(rec) is None and not harness_prefix(_text_of(rec)):
                    humans.append(_squash(_text_of(rec)))
                continue
            if rec.get("type") != "user":
                continue
            if "origin" in rec:
                if first is None:
                    first = n
                if excluded_kind(rec) is None and not harness_prefix(_text_of(rec)):
                    humans.append(_squash(_text_of(rec)))
            else:
                recs.append((n, rec))
    early = [] if first is None else [
        _squash(_text_of(r)) for n, r in recs
        if n < first and excluded_kind(r) == "no-origin:other" and not harness_prefix(_text_of(r))]
    return first is not None, humans, first, early


def fingerprint(path: Path) -> dict:
    """⛔ A live transcript GROWS UNDER YOU. Two runs that disagree on statement
    count are indistinguishable from a lossy tool unless the reads are labelled.
    On 2026-08-15 exactly that confusion was filed as a 25% extraction defect.
    """
    st = path.stat()
    n = sum(1 for _ in path.open(errors="replace"))
    return {
        "path": str(path),
        "bytes": st.st_size,
        "records": n,
        "mtime": st.st_mtime,
        "mtime_iso": __import__("datetime").datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
    }


def collect(path: Path) -> tuple[list[Statement], list[Boundary], Reconciliation]:
    out: list[Statement] = []
    bounds: list[Boundary] = []
    rc = Reconciliation()
    # Brackets are tracked PER FEATURE NAME: a BEGIN for feature F opens F's
    # bracket even while another feature's bracket is open.
    open_features: set[str] = set()
    rc.origin_marked, human_texts, first_marked, early_texts = scan_origin(path)

    with path.open(errors="replace") as fh:
        # ⛔ start=1: `index` is the 1-based `.jsonl` line number (== `grep -n`),
        # NOT a 0-based counter. See the `index` INVARIANT authority above.
        for i, line in enumerate(fh, start=1):
            rc.records_total += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                rc.json_undecodable += 1
                continue

            kind = rec.get("type")

            # ── ROLE 1: BOUNDARIES, read from ASSISTANT records only (rule 1).
            # An assistant record is never a statement, and a human turn never
            # opens a bracket — so the human quoting the phrase back cannot move
            # the boundary, and the marker line cannot become a design input.
            if kind == "assistant":
                for skind, feature in sentinels_in(_text_of(rec)):
                    if skind == "begin":
                        if feature not in open_features:
                            bounds.append(Boundary(i, "begin", feature,
                                                   SENTINEL_BEGIN))
                            open_features.add(feature)
                            rc.boundaries_seen += 1
                        else:
                            # a re-mention while THIS feature's bracket is already open
                            rc.sentinel_ignored_reopen += 1
                    else:
                        if feature in open_features:
                            bounds.append(Boundary(i, "end", feature, SENTINEL_END))
                            open_features.discard(feature)
                            rc.boundaries_seen += 1
                        else:
                            rc.sentinel_ignored_unopened += 1

            # ── ROLE 2: STATEMENTS, from HUMAN channels only.
            qc = kind == "attachment" and is_queued_command(rec)
            if kind not in ("user", "queue-operation") and not qc:
                rc.not_a_human_channel += 1
                continue
            if kind == "queue-operation":
                # `remove` repeats the enqueued text verbatim and would double
                # every queued message; `dequeue` carries no content at all.
                if rec.get("operation") != "enqueue":
                    rc.queue_op_not_enqueue += 1
                    continue
            channel = "user" if kind == "user" else "queued"

            # ⛔ WHO SPOKE. In an origin-marked transcript only a human-origin
            # user turn goes on; every other user turn is counted by kind.
            premarking = rc.origin_marked and i < first_marked and not qc
            if qc:
                # ⛔ R1: a queued_command attachment is the stakeholder's only if
                # attachment.origin.kind == "human". Any other kind is excluded;
                # with NO origin it is excluded only where the transcript is
                # origin-marked (an older transcript never counted attachments).
                qk = queued_command_kind(rec)
                if qk is not None:
                    if qk == "queued_command:no-origin" and not rc.origin_marked:
                        rc.not_a_human_channel += 1
                    else:
                        rc.exclude(qk)
                    continue
            if rc.origin_marked and kind == "user":
                ek = excluded_kind(rec)
                if ek is not None and not (premarking and ek == "no-origin:other"):
                    rc.exclude(ek)
                    continue

            raw = _text_of(rec)
            if not raw.strip():
                rc.empty_text += 1
                continue

            hp = harness_prefix(raw)
            if hp is not None:
                rc.exclude("harness-injection:" + hp)
                continue

            # ⛔ A queue record is NEVER credited without the equality check, in
            # any zone. Before the first marked record it may equal an admitted
            # user turn of that zone as well as a human-origin turn.
            if (rc.origin_marked and kind == "queue-operation"
                    and not queued_has_human_copy(raw, human_texts + (early_texts if premarking else []))):
                rc.exclude("queued-no-origin")
                continue

            cleaned = _strip_machinery(raw)
            if not cleaned:
                head = raw.lstrip()[:200]
                why = "unclassified"
                for tag in _PAIRED_TAGS:
                    if "<%s>" % tag in head:
                        why = "<%s>" % tag
                        break
                else:
                    for marker in _WHOLE_RECORD + _TAIL:
                        if marker in head:
                            why = marker
                            break
                rc.reject(why)
                continue

            if premarking and not (kind == "user" and "origin" in rec):
                rc.before_first_marked_fallback += 1
            if channel == "user":
                rc.admitted_user += 1
            else:
                rc.admitted_queued += 1
                if qc:
                    rc.admitted_queued_command += 1

            # ⛔ A mid-turn user message appears TWICE, and the first copy can be
            # TRUNCATED. Measured at 708/712 on 2026-08-15: the mid-turn record
            # carried 252 chars, the folded-in record 355, and the missing tail
            # was the load-bearing sentence of the whole message. So the rule is
            # NOT equality-dedup — that keeps the SHORT one and silently hands an
            # auditor a design input with its conclusion amputated, which is
            # worse than the duplicate it was meant to fix.
            #
            # Rule: if one is a strict PREFIX of the other, keep the LONGER.
            if out:
                prev = out[-1].text
                if cleaned == prev:
                    rc.deduped_exact += 1
                    continue
                if cleaned.startswith(prev):
                    out[-1] = Statement(index=i, text=cleaned, channel=out[-1].channel)
                    rc.deduped_prefix_upgrade += 1
                    continue
                if prev.startswith(cleaned):
                    rc.deduped_prefix_stub += 1
                    continue

            # ⛔ GLOBAL, not adjacent-only. A queued message is journalled when
            # the human types it and again — much later, past many assistant
            # turns — when the queue drains into a `user` record. The adjacent
            # comparison above cannot see across that gap, so without this the
            # queue channel would DOUBLE every message it rescued, which is a
            # worse outcome than the loss it was added to fix: a duplicated
            # design input reads to an auditor as the human saying it twice.
            hit = next((s for s in out
                        if s.text == cleaned or s.text.startswith(cleaned)), None)
            if hit is not None:
                rc.deduped_queue_echo += 1
                continue
            longer = next((n for n, s in enumerate(out) if cleaned.startswith(s.text)), None)
            if longer is not None:
                out[longer] = Statement(index=out[longer].index, text=cleaned,
                                        channel=out[longer].channel)
                rc.deduped_queue_echo += 1
                continue

            out.append(Statement(index=i, text=cleaned, channel=channel))

    return out, bounds, rc


def _bracket_slice(
    stmts: list[Statement],
    bounds: list[Boundary],
    feature: str | None,
) -> tuple[list[Statement], str] | None:
    """The slice inside the sentinel bracket `bound` selects, with its description, or None when no bracket
    qualifies (for a named feature: none of THAT name). Split out of `bound` to keep both simple."""
    # 1. THE SENTINEL BRACKET (preferred). Boundaries carry TRANSCRIPT line
    #    indices, and so do statements, so the slice is by line rather than by
    #    statement ordinal — the assistant's marker turn is not itself a
    #    statement and never occupies an ordinal.
    #    With `feature`, the bracket is selected by EXACT name (it was a
    #    substring match, so `x` matched `x-two`). Without it, the first bracket.
    begins = [b for b in bounds if b.kind == "begin"
              and (not feature or b.feature == feature)]
    if not begins:
        return None
    # the bracket closes at the first END for ITS OWN feature after it (with one
    # feature open at a time this is the same END the old code found)
    ends = [b for b in bounds if b.kind == "end" and b.feature == begins[0].feature]
    start_line = begins[0].index
    end_line = next((e.index for e in ends if e.index > start_line), None)
    sl = [s for s in stmts if s.index > start_line
          and (end_line is None or s.index < end_line)]

    # ⛔ THE TRIGGERING TURN IS INSIDE THE BRACKET. The human says "let's
    # design X"; the assistant's reply carrying the marker necessarily comes
    # AFTER it. Bounding strictly after the marker drops the request that
    # opened the session — routinely the most load-bearing turn in it. So the
    # human turn immediately preceding the marker is included, labelled.
    trigger = [s for s in stmts if s.index < start_line]
    if trigger:
        t = trigger[-1]
        sl = [Statement(t.index, t.text, t.channel, role="trigger")] + sl

    how = "sentinel bracket %s" % SENTINEL_BEGIN
    if begins[0].feature:
        how += " feature %r" % begins[0].feature
    if end_line is None:
        how += " — opened at line %d, NOT YET CLOSED (ran to end of transcript)" % start_line
    else:
        how += " — transcript lines %d..%d" % (start_line, end_line)
    if trigger:
        how += " (+ the triggering turn at line %d)" % trigger[-1].index
    return sl, how


def _refusal(bounds: list[Boundary], feature: str) -> str:
    """The `how` text of a refused `--feature` (empty slice, exit status 2 in `main`): names the features that do
    have a bracket in this transcript."""
    names = sorted({b.feature for b in bounds if b.feature})
    return ("REFUSED: no sentinel bracket or typed sentinel block exists for "
            "feature %r; features with a bracket in this transcript: %s"
            % (feature, ", ".join(repr(n) for n in names) if names else "none"))


def bound(
    stmts: list[Statement],
    bounds: list[Boundary],
    feature: str | None,
    from_match: str | None,
    to_end: bool,
) -> tuple[list[Statement], str]:
    """Return the bounded slice and a human-readable description of the bounds."""

    # 1. THE SENTINEL BRACKET (preferred). Boundaries carry TRANSCRIPT line
    #    indices, and so do statements, so the slice is by line rather than by
    #    statement ordinal — the assistant's marker turn is not itself a
    #    statement and never occupies an ordinal.
    #    With `feature`, the bracket is selected by EXACT name (it was a
    #    substring match, so `x` matched `x-two`). Without it, the first bracket.
    bracketed = _bracket_slice(stmts, bounds, feature)
    if bracketed is not None:
        return bracketed

    # 2. typed sentinels (fallback, the original greppable-string form)
    if feature or any(_START.search(s.text) for s in stmts):
        start_i = stop_i = None
        for n, s in enumerate(stmts):
            m = _START.search(s.text)
            if m and start_i is None and (not feature or (m.group("name") or "") == feature):
                start_i = n
                continue
            m = _STOP.search(s.text)
            if m and start_i is not None and (not feature or (m.group("name") or "") == feature):
                stop_i = n
                break
        if start_i is not None:
            sl = stmts[start_i: (stop_i + 1 if stop_i is not None else len(stmts))]
            how = f"typed sentinel block{' for ' + feature if feature else ''}"
            if stop_i is None:
                how += " (START found, STOP absent — ran to end of transcript)"
            return sl, how

    if from_match:
        pat = re.compile(from_match, re.I)
        for n, s in enumerate(stmts):
            if pat.search(s.text):
                return stmts[n:], f"first human statement matching /{from_match}/i to end"
        return [], f"NO human statement matched /{from_match}/i"

    # ⛔ A NAMED feature with no bracket is REFUSED: the old fall-through
    # handed back the whole transcript while the caller believed it held the bracket.
    if feature:
        return [], _refusal(bounds, feature)

    if to_end:
        return stmts, "entire transcript (no bounds given)"
    return stmts, "entire transcript (no bounds given)"


# ── self-test ───────────────────────────────────────────────────────────────

def _asst(text: str) -> str:
    return json.dumps({"type": "assistant",
                       "message": {"role": "assistant",
                                   "content": [{"type": "text", "text": text}]}})


def _user(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}})


_B = SENTINEL_BEGIN
_E = SENTINEL_END

#: The happy path plus every CONTAMINATION shape the assistant actually produces
#: when it talks about this mechanism. Each contaminating line below is one a
#: real reply would contain; none of them may move the bracket.
_FIXTURES_SENTINEL = [
    _user("Before the session; must be excluded."),
    # a reply that MENTIONS the phrase without asserting it — the commonest case,
    # and the one a naive scan brackets on.
    _asst("You can open one with the `%s feature: x` phrase, but I have not yet." % _B),
    _user("Let's design the widget roster."),           # the TRIGGERING turn
    _asst("%s feature: widget-roster\n\nOpened." % _B),  # the real BEGIN
    _user("First real design statement."),
    # contamination INSIDE the bracket, in all four disqualifying forms
    _asst("Quoting it inline: `%s feature: other` — not a boundary." % _B),
    _asst("In a fence:\n```\n%s feature: other\n%s feature: other\n```\ndone." % (_B, _E)),
    _asst("> %s feature: other\n\nThat was a blockquote." % _E),
    _asst("Mid-sentence the marker %s feature: other appears; not line-initial." % _E),
    _asst("%s\n\nBare marker, no feature field: does not qualify." % _E),
    _user("Second real design statement."),
    # a second BEGIN while one is open: a re-mention, ignored and COUNTED
    _asst("%s feature: widget-roster" % _B),
    _asst("%s feature: widget-roster\n\nClosed." % _E),   # the real END
    _user("After the session; must be excluded."),
    # a stray END with nothing open: ignored and COUNTED
    _asst("%s feature: widget-roster" % _E),
]

_FIXTURES_QUEUE = [
    _asst("%s feature: q" % _B),
    # a queued message that DOES later drain into a user record -> exactly one copy.
    # Shape taken from a real transcript (a short reply typed mid-turn).
    '{"type":"queue-operation","operation":"enqueue","content":"Queued and later drained."}',
    '{"type":"queue-operation","operation":"dequeue"}',
    _user("An ordinary turn in between."),
    _user("Queued and later drained."),
    # a queued message that NEVER drains -> lost entirely without the fix.
    # Shape taken from a real transcript (a statement typed mid-turn that never drained).
    '{"type":"queue-operation","operation":"enqueue","content":"Queued and NEVER drained."}',
    '{"type":"queue-operation","operation":"remove","content":"Queued and NEVER drained."}',
    # machinery arrives on the queue channel too, and must still be rejected
    ('{"type":"queue-operation","operation":"enqueue","content":'
     '"<task-notification>\\n<task-id>abc</task-id>\\n</task-notification>"}'),
]


def _fx_rows(*items):
    """Self-test fixture rows from ('u', text) / ('a', text) pairs. Split out of `self_test`."""
    return [_user(x[1]) if x[0] == "u" else _asst(x[1]) for x in items]


def self_test() -> int:
    """⛔ This proves the CODE matches the record shapes documented above. It
    ⛔ THE SENTINEL IS ASSISTANT TEXT, so this test is REAL rather than a model
    of a record format. The earlier design keyed on a harness record shape, and
    no fixture could prove that shape was what a live invocation produced — the
    fixture was the author's belief, and a green run certified the belief. A
    phrase the assistant writes has no such gap: the fixture below contains the
    same bytes a real reply does.

    The contamination cases are the point. Every one of them is a line a real
    reply produces when it DISCUSSES this mechanism, and none may move the
    bracket.
    """
    import tempfile

    failures: list[str] = []

    def run(lines, label, want, want_in_how=None, want_rc=None, feature=None):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write("\n".join(lines) + "\n")
            p = Path(fh.name)
        try:
            stmts, bnds, rc = collect(p)
            sl, how = bound(stmts, bnds, feature, None, False)
            got = [s.text for s in sl]
            if got != want:
                failures.append("%s: bracket contents\n    want %r\n    got  %r"
                                % (label, want, got))
            if want_in_how and want_in_how not in how:
                failures.append("%s: %r not in bounds description %r"
                                % (label, want_in_how, how))
            if not rc.balances(len(stmts)):
                failures.append("%s: reconciliation does not balance: %r" % (label, asdict(rc)))
            for k, v in (want_rc or {}).items():
                if getattr(rc, k) != v:
                    failures.append("%s: rc.%s want %r got %r" % (label, k, v, getattr(rc, k)))
        finally:
            os.unlink(p)

    # ⛔ THE CONTAMINATION TEST. Nine contaminating mentions, one real bracket.
    # The triggering turn is INSIDE; everything outside the marker pair is not.
    run(_FIXTURES_SENTINEL, "sentinel + contamination",
        ["Let's design the widget roster.",
         "First real design statement.",
         "Second real design statement."],
        want_in_how="transcript lines",
        want_rc={"boundaries_seen": 2,
                 "sentinel_ignored_reopen": 1,
                 "sentinel_ignored_unopened": 1})

    # ⛔ PER-FEATURE BRACKETS. A named feature is selected by EXACT name,
    # interleaved sessions are allowed, and an unknown feature is REFUSED (an empty
    # slice), never answered with the whole transcript.
    seq_ab = _fx_rows(("u", "Design A."), ("a", "%s feature: feat-a" % _B), ("u", "A one."),
                 ("a", "%s feature: feat-a" % _E), ("u", "Design B."),
                 ("a", "%s feature: feat-b" % _B), ("u", "B one."), ("u", "B two."),
                 ("a", "%s feature: feat-b" % _E), ("u", "After both."))
    run(seq_ab, "sequential A then B: --feature feat-b",
        ["Design B.", "B one.", "B two."], want_in_how="feat-b", feature="feat-b",
        want_rc={"boundaries_seen": 4, "sentinel_ignored_reopen": 0,
                 "sentinel_ignored_unopened": 0})
    run(seq_ab, "sequential A then B: --feature feat-a",
        ["Design A.", "A one."], want_in_how="feat-a", feature="feat-a")

    nest = _fx_rows(("u", "Design A."), ("a", "%s feature: feat-a" % _B), ("u", "A one."),
               ("u", "Design B."), ("a", "%s feature: feat-b" % _B), ("u", "B one."),
               ("a", "%s feature: feat-b" % _E), ("u", "A two."),
               ("a", "%s feature: feat-a" % _E), ("u", "After both."))
    nest_rc = {"boundaries_seen": 4, "sentinel_ignored_reopen": 0,
               "sentinel_ignored_unopened": 0}
    run(nest, "interleaved: B opens while A is open, --feature feat-b",
        ["Design B.", "B one."], want_in_how="feat-b", feature="feat-b", want_rc=nest_rc)
    # A's bracket spans B's session by transcript line; the human turns inside it
    # are interleaved and cannot be separated, so they are A's by position.
    run(nest, "interleaved: --feature feat-a",
        ["Design A.", "A one.", "Design B.", "B one.", "A two."],
        want_in_how="feat-a", feature="feat-a", want_rc=nest_rc)
    run(nest, "interleaved: no --feature keeps today's first bracket",
        ["Design A.", "A one.", "Design B.", "B one.", "A two."], feature=None)

    # a re-BEGIN of an OPEN feature is still a counted re-mention; an END for a
    # feature with nothing open is still counted, even while another is open.
    run(_fx_rows(("u", "Design A."), ("a", "%s feature: feat-a" % _B), ("u", "A one."),
            ("a", "%s feature: feat-a" % _B), ("a", "%s feature: feat-z" % _E),
            ("a", "%s feature: feat-a" % _E)),
        "per-feature reopen/unopened counts", ["Design A.", "A one."], feature="feat-a",
        want_rc={"boundaries_seen": 2, "sentinel_ignored_reopen": 1,
                 "sentinel_ignored_unopened": 1})

    run(seq_ab, "unknown feature is refused", [], want_in_how="no sentinel bracket",
        feature="feat-nope")
    run(seq_ab, "refusal lists the existing features", [],
        want_in_how="'feat-a'", feature="feat-nope")
    two = _fx_rows(("u", "Design X2."), ("a", "%s feature: x-two" % _B), ("u", "X2 one."),
              ("a", "%s feature: x-two" % _E))
    run(two, "--feature x does not match x-two", [], want_in_how="x-two", feature="x")
    run(two, "--feature x-two still selects x-two", ["Design X2.", "X2 one."],
        feature="x-two")

    # ⛔ the channel that actually lost statements. Both directions asserted:
    # the never-drained message must be RECOVERED, and the drained one must
    # appear EXACTLY ONCE — a rescue that duplicates is not a rescue.
    run(_FIXTURES_QUEUE, "queue channel",
        ["Queued and later drained.",
         "An ordinary turn in between.",
         "Queued and NEVER drained."])

    # ⛔ WHO SPOKE: three checks. Origin-marked: only the human-origin turn
    # counts and the others are counted by kind; a queue record with no human
    # copy is excluded; an unmarked transcript falls back and says so.
    def _o(text, kind=None, **extra):
        rec = {"type": "user", "message": {"role": "user", "content": text}}
        if kind is not None:
            rec["origin"] = {"kind": kind}
        return json.dumps(rec | extra)

    marked = [
        _o("A human turn with enough words to read as a design statement.", "human"),
        _o("A peer agent's turn with enough words to read as a design statement.", "peer", isMeta=True),
        _o("A notification turn with enough words to read as a design statement.", "task-notification"),
        json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}}),
        '{"type":"queue-operation","operation":"enqueue","content":"A queued line nobody human-origin repeats."}',
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        fh.write("\n".join(marked) + "\n")
        mp = Path(fh.name)
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        fh.write(_o("An older-format turn with no origin field at all.") + "\n")
        up = Path(fh.name)
    try:
        ms, _, mrc = collect(mp)
        us, _, urc = collect(up)
    finally:
        os.unlink(mp)
        os.unlink(up)
    if [s.text for s in ms] != ["A human turn with enough words to read as a design statement."]:
        failures.append("origin-marked: only the human-origin turn may count, got %r" % [s.text for s in ms])
    want_ex = {"peer": 1, "task-notification": 1, "no-origin:tool-result": 1, "queued-no-origin": 1}
    if mrc.excluded_by_kind != want_ex or not mrc.balances(len(ms)):
        failures.append("origin-marked: excluded by kind want %r got %r (balances=%r)"
                        % (want_ex, mrc.excluded_by_kind, mrc.balances(len(ms))))
    if urc.origin_marked or len(us) != 1 or urc.excluded_by_kind:
        failures.append("unmarked fallback: want today's rule with nothing excluded")

    # the sentinel scanner in isolation, both directions
    if sentinels_in("%s feature: x" % SENTINEL_BEGIN) != [("begin", "x")]:
        failures.append("a bare qualifying BEGIN did not register")
    for bad, why in (
        ("`%s feature: x`" % SENTINEL_BEGIN, "inline code"),
        ("> %s feature: x" % SENTINEL_BEGIN, "blockquote"),
        ("```\n%s feature: x\n```" % SENTINEL_BEGIN, "code fence"),
        ("text %s feature: x" % SENTINEL_BEGIN, "mid-line"),
        ("%s" % SENTINEL_BEGIN, "no feature field"),
    ):
        if sentinels_in(bad):
            failures.append("contamination via %s was accepted as a boundary" % why)

    # the mixed record must survive stripping in BOTH directions
    mixed = _strip_machinery("<system-reminder>noise</system-reminder>\nReal text here.")
    if mixed != "Real text here.":
        failures.append("mixed-record strip: got %r" % mixed)
    if _strip_machinery("<system-reminder>only noise</system-reminder>") != "":
        failures.append("pure-machinery strip leaked")
    if _strip_machinery("Base directory for this skill: /x/skills/foo\n\n# Foo") != "":
        failures.append("skill-body strip leaked")

    for f in failures:
        print("⛔ FAIL " + f)
    print("self-test: 16 checks, %d failures" % len(failures))
    print("PASS" if not failures else "⛔ FAIL")
    return 1 if failures else 0


#: Shown at the end of `--help`, where a user of the tool meets it. A test pins the first line.
KNOWN_LIMITS = """\
WHO SPOKE, AND THE LIMITS THAT REMAIN:
  1. Only a turn marked origin.kind == human counts as the stakeholder's. Other
     agents' messages (peer), task notifications, tool results, meta records,
     compaction summaries and anything else are excluded and counted by kind in
     the output; a quote taken from one is never verified as the stakeholder's.
     A message the stakeholder types while the assistant is working is a
     `queued_command` attachment whose mark is one level down: it counts when
     attachment.origin.kind == human (its text is attachment.prompt); any other
     kind, or none, is excluded and counted as queued_command:<kind>. A
     queue-operation record carries no origin, so it counts only when its text
     equals (after whitespace normalisation) a human-origin user turn or a
     human-origin queued_command attachment; before the first origin-marked
     record it also counts when it equals an admitted early user turn (a
     fallback record, origin unknown). No prefix or extension credit, in any
     zone. RESIDUAL LIMIT, the older format: a transcript with no origin
     field on any user record falls back to the old rule, which cannot tell
     a stakeholder's turn from another agent's. It says so as
     "%s".
     In a marked transcript, user records BEFORE THE FIRST ORIGIN-MARKED
     RECORD (origin unknown) fall back the same way, counted separately as
     before_first_marked_fallback, and say
     "%s".
  2. HARNESS TEXT is marked human. A record whose text begins with one of the
     fixed prefixes "[Gnomon]", "[Gnomon@", "[GNOMON NOTICE]", "[GNOMON",
     "[DSD ANSWERED]" or "[scheduled:" is excluded and counted as
     harness-injection:<prefix>. That list is DATA: the public default above,
     plus any prefixes a deployment adds under a `harness-prefixes:` entry (one
     per line) in private/design-session.local.md.
     The one layout that is read: the `- harness-prefixes:` line, then
     indented `- prefix` items directly under it, no blank line inside,
     ended by a top-level bullet, a heading or the end of the file.
     Only the one layout above is read. Some departures from it warn; any
     other may be read in part, wrongly, or not at all, without a warning.
     After any overlay edit, check the parsed list itself: run the extractor
     with --json and read harness_prefixes.prefixes (the counts line shows
     only how many were added).
     Each run names its source ("default" or "default plus overlay"); an
     overlay that cannot be read is a loud warning and the default applies
     (--overlay PATH names another file, --no-overlay reads none). This is a
     LEXICAL TRIPWIRE and nothing more: it catches only the fixed prefixes,
     it cannot see harness text that lacks one, and an operator who types a
     line that starts with one of them would be excluded. The structural
     answer (a harness ledger of injected text) belongs to the harness owner
     and is not built.
  3. --from-match is ignored when a sentinel bracket exists elsewhere in the
     transcript; check that the slice's first statement contains the phrase.
""" % (ORIGIN_UNKNOWN_WARNING, ORIGIN_PARTIAL_WARNING)


def _origin_info(rc: Reconciliation) -> dict:
    """The `origin` block of the output (mode, warning, rule, exclusions). Split out of `main`."""
    origin = {
        "mode": "marked" if rc.origin_marked else "unmarked",
        "warning": (None if rc.origin_marked and not rc.before_first_marked_fallback
                    else ORIGIN_PARTIAL_WARNING if rc.origin_marked else ORIGIN_UNKNOWN_WARNING),
        "before_first_marked_fallback": rc.before_first_marked_fallback,
        "rule": (("only origin.kind == human counts as the stakeholder's"
                  if not rc.before_first_marked_fallback else
                  "only origin.kind == human counts from the first origin-marked record; "
                  "earlier records fall back to the old rule (origin unknown)")
                 if rc.origin_marked else "fallback: every user turn that is not machinery counts"),
        "excluded_total": rc.excluded_total,
        "excluded_by_kind": dict(rc.excluded_by_kind),
    }
    return origin


def _print_origin(rc: Reconciliation) -> None:
    """The `origin` lines of the text output. Split out of `main`."""
    if rc.origin_marked:
        by = ", ".join(f"{k} {n}" for k, n in sorted(rc.excluded_by_kind.items(), key=lambda kv: (-kv[1], kv[0])))
        if rc.before_first_marked_fallback:
            print("origin     : partial; only origin.kind == human counts from the first origin-marked record")
            print(f"origin     : ⛔ {ORIGIN_PARTIAL_WARNING}")
            print(f"             {rc.before_first_marked_fallback} statement record(s) came before the first origin-marked record;")
            print("             they fall back to the old rule")
        else:
            print("origin     : verified; only origin.kind == human counts as the stakeholder's")
        print(f"             excluded {rc.excluded_total} user turns and queue records" + (f": {by}" if by else ""))
    else:
        print(f"origin     : ⛔ {ORIGIN_UNKNOWN_WARNING}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, epilog=KNOWN_LIMITS,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("transcript", type=Path, nargs="?")
    ap.add_argument("--feature", help="feature name carried by the design-session bracket")
    ap.add_argument("--from-match", help="fallback: start at the first human statement matching this regex")
    ap.add_argument("--to-end", action="store_true", help="fallback: run to the end of the transcript")
    ap.add_argument("--json", action="store_true", help="machine-readable output for a subagent brief")
    ap.add_argument("--self-test", action="store_true",
                    help="run the contamination + channel control; no transcript required")
    ap.add_argument("--overlay", type=Path, help="read the harness-prefixes entry from this overlay file "
                    "instead of private/design-session.local.md (a missing file is a warning)")
    ap.add_argument("--no-overlay", action="store_true", help="read no overlay: the default prefix list only")
    ap.add_argument("--print-sentinels", action="store_true",
                    help="print the BEGIN and END sentinel phrases (the one definition) and exit")
    args = ap.parse_args()

    if args.print_sentinels:
        print(SENTINEL_BEGIN)
        print(SENTINEL_END)
        return 0

    if args.self_test:
        return self_test()
    if args.transcript is None:
        ap.error("a transcript is required (or use --self-test)")
    if not args.transcript.exists():
        print(f"transcript not found: {args.transcript}", file=sys.stderr)
        return 2

    hinfo = load_harness_prefixes(args.overlay, use_overlay=not args.no_overlay)
    if hinfo["warning"]:
        print("⛔ " + hinfo["warning"], file=sys.stderr)
    fp = fingerprint(args.transcript)
    everything, bounds, rc = collect(args.transcript)
    slice_, how = bound(everything, bounds, args.feature, args.from_match, args.to_end)
    if how.startswith("REFUSED:"):
        print(how, file=sys.stderr)
        return 2
    balanced = rc.balances(len(everything))
    origin = _origin_info(rc)

    if args.json:
        print(json.dumps({
            "transcript": str(args.transcript),
            "transcript_fingerprint": fp,
            "origin": origin,
            "harness_prefixes": hinfo,
            "bounds": how,
            "boundaries": [asdict(b) for b in bounds],
            "reconciliation": asdict(rc) | {"emitted": len(everything), "balances": balanced},
            "human_statements_in_transcript": len(everything),
            "human_statements_in_block": len(slice_),
            "statements": [asdict(s) for s in slice_],
        }, indent=2))
        return 0 if balanced else 1

    print(f"transcript : {args.transcript}")
    print("fingerprint: {bytes} bytes · {records} records · mtime {mtime_iso}".format(**fp))
    print("             ⛔ a live transcript GROWS. Compare this line before concluding")
    print("                that two runs disagreeing on the count means a lossy tool.")
    _print_origin(rc)
    print(f"bounds     : {how}")
    print(f"statements : {len(slice_)} of {len(everything)} human turns in the transcript")
    print()
    print("── RECONCILIATION ──  (every record lands in exactly one bucket)")
    print(f"   records seen        : {rc.records_total}")
    print(f"     json undecodable  : {rc.json_undecodable}")
    print(f"     not a human chan  : {rc.not_a_human_channel}")
    print(f"     queue-op non-enq  : {rc.queue_op_not_enqueue}")
    print(f"     empty text        : {rc.empty_text}")
    print(f"     harness prefixes  : {harness_source_line(hinfo)}")
    if hinfo["warning"]:
        print("         ⛔ " + hinfo["warning"])
    print(f"     not the stakeholder: {rc.excluded_total}  (by origin kind)")
    for kd, n in sorted(rc.excluded_by_kind.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"         {n:5d}  {kd}")
    print(f"     rejected machinery: {rc.rejected_total}")
    for why, n in sorted(rc.rejected_machinery.items(), key=lambda kv: -kv[1]):
        print(f"         {n:5d}  {why}")
    print(f"     admitted (user)   : {rc.admitted_user}")
    print(f"     admitted (queued) : {rc.admitted_queued}")
    print(f"         of which queued_command attachments (attachment.origin.kind == human): {rc.admitted_queued_command}")
    print(f"   deduped exact       : {rc.deduped_exact}")
    print(f"   deduped prefix-stub : {rc.deduped_prefix_stub}")
    print(f"   deduped upgrade     : {rc.deduped_prefix_upgrade}")
    print(f"   deduped queue-echo  : {rc.deduped_queue_echo}")
    print(f"   sentinel boundaries : {rc.boundaries_seen}")
    for b in bounds:
        print(f"         line {b.index}: {b.kind.upper()} feature={b.feature!r}")
    print(f"   sentinel mentions ignored: reopen={rc.sentinel_ignored_reopen} "
          f"unopened={rc.sentinel_ignored_unopened}")
    if rc.boundaries_seen == 0 and (rc.sentinel_ignored_reopen or rc.sentinel_ignored_unopened):
        print("         ⛔ mentions but no bracket — the assistant DISCUSSED the")
        print("            mechanism instead of invoking it. Not the same as silence.")
    print(f"   emitted             : {len(everything)}")
    print("   BALANCES            : " + ("yes" if balanced else "⛔ NO — a class was lost silently"))
    print()
    print("⛔ This is EXTRACTION only. The comparison is judgment and must be done by an")
    print("   agent with NO conversational context — see this file's module docstring.")
    print("   Audit FOUR directions: MISSING · ALTERED · INVENTED · AMBIGUOUS.")
    print()
    queued = sum(1 for s in slice_ if s.channel == "queued")
    if queued:
        print(f"⛔ {queued} of these {len(slice_)} statements exist ONLY as QUEUED messages —")
        print("   typed while the assistant was working and never echoed as a `user`")
        print("   record. They are marked [queued] below. They skew load-bearing.")
        print()
    for n, s in enumerate(slice_, 1):
        tag = " [queued]" if s.channel == "queued" else ""
        print(f"───── statement {n} (transcript line {s.index}){tag} " + "─" * 20)
        print(s.text)
        print()
    return 0 if balanced else 1


if __name__ == "__main__":
    raise SystemExit(main())
