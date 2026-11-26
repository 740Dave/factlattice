# Spec audit: the steps

A spec is authored from a design conversation. The author then holds a memory of that conversation, and that memory is the thing that fails: an author re-reading its own spec recalls that a point "was addressed" and reads the intent it meant into the words it wrote. In the audits that motivated this procedure, most of the load-bearing defects were not losses; the spec asserted things the stakeholder never said. A check for dropped items alone finds almost none of them.

So the comparison goes to a reader who was not in the room, who is a different model from the author, and who works from the stakeholder's own words.

This page is the procedure the `end-design-session` skill (`skills/end-design-session/SKILL.md`) relies on. It is complete without any private corpus: a deployment that keeps its own, longer procedure names it in `private/design-session.local.md` (see "Deployment overlay"), and that procedure governs where it differs.

## Terms

- **Stakeholder**: the human whose intent the spec records. Never a name in a shared file.
- **Verbatim**: the stakeholder's own words, extracted from the conversation transcript by `tools/design-session/extract_design_inputs.py`, plus any written answers he gave to decision documents.
- **Selection**: a stakeholder choosing an option someone else wrote. The option's wording belongs to its author. The act of choosing is his; the words are not.
- **Frame**: the exact inputs a verdict was reached on (spec path and sha256, verbatim fingerprint, the two models, the date).

## Inputs

| Input | Required | Meaning |
|---|---|---|
| `SPEC` | yes | the spec file or files under audit |
| `VERBATIM` | yes | the transcript path and the feature name (or a match phrase), and any decision-document answers |
| `AUTHOR_MODEL` | yes | the model that wrote the spec; if it is unknown the audit does not start |
| `ROADMAP_SEARCHES` | when the session used the design-session skills | the two roadmap or backlog search records the author wrote (at the start, and before the end sentinel), copied from its replies with their transcript lines |
| `SCOPE` | no | which parts of the spec are the author's own design (marked `[A]`) and which claim to be the stakeholder's intent |

The auditor is given these and nothing else. In particular it is not told what the dispatcher expects it to find: that anchors it, and the independence is the whole product.

## Steps

**Step 0: instrument gate.** Run `python3 tools/design-session/verify_dsd_citations.py --self-test` and `python3 tools/design-session/extract_design_inputs.py --self-test` from the repository root. If either fails, stop: report that the instrument failed, with no comparison and no verdict. A comparison made with a broken extractor reads as a clean audit.

**Step 1: build the verbatim.** Run the extractor with `--json` and keep its reconciliation block and fingerprint. Read the reconciliation before going on: `BALANCES: yes` means every record landed in exactly one bucket; a `no` means a class of record was lost and the extraction is not trustworthy. A transcript that is still being written grows between two reads, so two runs that disagree on the statement count are first compared by fingerprint before anyone blames the tool.

- The extractor also reads statements typed while the assistant was working (the queue channel). Those skew load-bearing, because a person interrupts a working assistant when something important has just occurred to him.
- A selection never enters the verbatim as speech. Record it as "the stakeholder SELECTED '<label>' (decision document <id>, item <n>)", never as "the stakeholder said". Only a written comment of his counts as his words.
- Messages from other agents are not the stakeholder's words. Only a turn marked `origin.kind == human` counts as the stakeholder's: the extractor excludes `peer` (another agent), `task-notification`, tool-result, meta and compaction-summary turns and reports how many it excluded, by kind. A quote taken from an excluded turn is never verified as his: the citation checker says which kind it came from. A message the stakeholder types while the assistant is working appears as a `queued_command` attachment whose mark is one level down: it counts only when `attachment.origin.kind == human` (its text is `attachment.prompt`; a quote may be cited at that attachment's line), and a `queued_command` with any other origin kind, or none, is excluded and counted as `queued_command:<kind>`. A `queue-operation` record carries no origin, so it counts only when its text equals (after whitespace normalisation) a human-origin user turn or a human-origin `queued_command` attachment; before the first origin-marked record it also counts when it equals an admitted early user turn (a fallback record, origin unknown). No prefix or extension credit, in any zone. The checker also fails a quote cited at an excluded line. Residual limit, the older format: a transcript with no origin field on any user record falls back to the old rule and prints "origin unknown: this transcript predates origin marking; a turn's speaker is not verified"; then check the origin of each quoted turn in the raw transcript by hand, drop any record that is another agent's message, and state the count dropped in the report. HARNESS TEXT IS MARKED HUMAN. Text the Gnomon harness types into a session comes out marked human (a user record or an attachment), so the origin rule admits it. The tools exclude a record whose text begins with one of the fixed prefixes "[Gnomon]", "[Gnomon@", "[GNOMON NOTICE]", "[GNOMON", "[DSD ANSWERED]" or "[scheduled:" and count it as harness-injection:<prefix>; that list is data, the public default plus any prefixes this deployment adds under a `harness-prefixes:` entry (one per line) in `private/design-session.local.md`, each run names its source ("default" or "default plus overlay"), and an overlay that cannot be read is a loud warning with the default applied; the checker calls a quote of it NOT-STAKEHOLDER. The one layout that is read: the `- harness-prefixes:` line, then indented `- prefix` items directly under it, no blank line inside, ended by a top-level bullet, a heading or the end of the file. Only the one layout above is read. Some departures from it warn; any other may be read in part, wrongly, or not at all, without a warning. After any overlay edit, check the parsed list itself: run the extractor with --json and read harness_prefixes.prefixes (the counts line shows only how many were added). This is a lexical tripwire and nothing more: it catches only the fixed prefixes, it cannot see harness text that lacks one, and an operator who types a line starting with one of them would be excluded. The structural answer (a harness ledger of injected text) is the harness owner's and is not built. A line that merely contains one of them mid-sentence is still admitted. In a marked transcript, user records before the first origin-marked record (origin unknown) fall back the same way; they are counted separately as `before_first_marked_fallback` and the tools say "origin unknown: records before the first origin-marked record; their speaker is not verified".
- `--from-match` is ignored by the extractor when a sentinel bracket exists elsewhere in the transcript. When it is used, confirm by search that the slice's first statement contains the match phrase; otherwise take the slice by line range.

**Step 1a: do not trust the extractor.** Independently search the raw transcript for the stakeholder's turns in the same window, and reconcile the count with the extractor's. Count by origin: a user record with `origin.kind == human`, plus a `queued_command` attachment with `attachment.origin.kind == human` (the mark is one level down; its text is `attachment.prompt`), plus a queue record whose text equals the text of either of those, in any zone, or, before the first origin-marked record only, the text of an admitted early user turn (that early-turn equality is an addition, not a limit on the human equality); a `peer`, `task-notification`, tool-result, meta or compaction-summary record, or a `queued_command` whose `attachment.origin.kind` is not human, is not a missed statement, and adding one back undoes the origin rule; nor is a record that begins with one of the harness prefixes (the lexical tripwire above, with its limits), which the tools exclude as `harness-injection:<prefix>`. On an unmarked transcript, or for the records before the first origin-marked record (origin unknown), count ordinary user records and queue records and say the speaker is unverified. A `[Gnomon] reminder: ...` record stays excluded: a reminder re-delivers text, and if the words are the stakeholder's they count at the turn where he wrote them, not where the harness replays them. A missed statement is reported as an instrument finding and added to the verbatim with its source line. This keeps the auditor's evidence independent of the tool's.

**Step 2: the model rule.** The auditor's model must differ from `AUTHOR_MODEL`. A same-model auditor holds independent context but shares the author's model-level priors, so a systematic blind spot can survive it. Record both models in the report. If the author's model is unknown, refuse. This rule is for spec audits; a build review has a different rationale.

**Step 3: four directions, with equal weight.**

- **MISSING**: the stakeholder said it; the spec does not carry it.
- **ALTERED**: the spec carries a changed version of what he said.
- **INVENTED**: the spec asserts something with no origin in anything he said.
- **AMBIGUOUS**: he said something that admits more than one valid reading, and the spec silently picked one without recording that it chose.

Each finding carries a risk-if-proceeded rating (HIGH, MEDIUM, LOW) and a citation: the transcript line and the exact quote. Author design clearly marked `[A]` is allowed; flag only invention that is not marked, or scope that should go to the stakeholder. A question he asked is a question, not a directive. An AMBIGUOUS finding is written as a ready-to-paste question for him, because it is the only preventive direction: it surfaces the fork before the guess hardens into something other work cites.

**Step 3a: the completeness claim.** If the spec says all the stakeholder's inputs are incorporated, or that it covers every requirement, that sentence is a checkable assertion. List every verbatim statement the spec does not carry and report the claim TRUE or FALSE with those counterexamples. A false completeness claim is at least a MEDIUM finding of its own.

**Step 3b: the three layers.** Check that the spec states its goals, its requirements and its functionality, kept apart:

1. goals: what the feature is for, in his words where he said it;
2. requirements: testable properties that any implementation must meet, each with how it would be tested or measured, written so they would still be true if the functionality were replaced;
3. functionality: the design chosen, each part tracing to the requirement it serves.

Report `NO-GOALS` or `NO-REQUIREMENTS` for an absent layer, `UNTESTABLE` for a requirement with no test or measure, `UNTRACED` for functionality that serves no stated requirement, and `GOAL-ATTRIBUTION` for a goal presented as his words without a source or an inferred goal not marked `[A]`. A goal or requirement that only he can supply goes to his questions.

**Step 3c: the roadmap searches.** For a session run with the design-session skills, check that `ROADMAP_SEARCHES` holds a search near the start of the session and another before it closed, each saying what was searched, the terms and the items found (a record that the project has no searchable roadmap or backlog counts as a search), with an extend-or-new call each time. If the call was that the design extends an existing item, check that the spec is written as an extension of that item. Report `NO-ROADMAP-SEARCH` with the corrections (beside the MISSING, ALTERED and INVENTED findings) when either search is absent, and when `ROADMAP_SEARCHES` itself was not given, say that the searches could not be checked. You may read the transcript lines the records cite, to confirm the copies.

**Step 4: check the citations mechanically.**

```
python3 tools/design-session/verify_dsd_citations.py <audit-document>.md \
    --transcript <transcript-or-bundle>.jsonl --spec <spec>.lts
```

Exit codes: 0 means every citation resolved and the speaker is verified; 1 means unverified citations (a failed audit document); 3 means the citations resolve but the speaker is not verified (an unmarked transcript, or a quote that resolves only through a record before the first origin-marked record). Exit 3 is not a failed audit: report it with the verifier's own verdict "citations resolve; SPEAKER NOT VERIFIED (origin unknown)" and check those turns' origin by hand.

Two conventions make this exact rather than guessed:

1. A quote of the stakeholder carries an attribution marker and a transcript line: `- **Stakeholder said** (line 461): *"..."*`. Nothing else in the document counts as his quote.
2. A node the audit proposes creating is written `[NEW:<ID>]` (or `[PROPOSED:<ID>]`). A plain `[<ID>]` must resolve in the spec.

Report failures by location and do not average them. Fabricated node ids or line numbers mean the brief was over-scoped (the dispatcher's error; give a narrower brief). Fabricated quotes of the stakeholder mean the method is unsound. The tool proves that citations resolve; it cannot prove a finding is right. A document that passes is well-formed, not correct. Also string-match every quote that the spec itself attributes to the stakeholder against the verbatim only, never against the selections, so an option label quoted as his speech fails, which is correct.

**Step 5: verdict, bound to its frame.** PASS, MINOR-REVISION or NEEDS-REVISION, naming the spec path and sha256, the verbatim fingerprint, both models and the date. A verdict that does not name its frame cannot be told from one about a later edit of the spec.

**Scope statement.** The report states what was examined and what was not: files read, nodes examined, what was skipped and why. Four findings say nothing about the other nodes. A clean-looking report must never read as coverage.

## What the auditor never does

- read the author's conversation beyond the extracted verbatim, the `ROADMAP_SEARCHES` records it was given, and the transcript lines those records cite;
- edit the spec;
- write anywhere except its own report location;
- give a verdict when step 0 failed;
- treat a selected option label as the stakeholder's reasoning or speech;
- leave a transcript extract in a repository: a report records the transcript path, the line range and a sha256 of the extract instead.

## Auditor brief template

The dispatcher fills the angle brackets and sends exactly this, with no hint of what to expect.

```
You are auditing a spec against the stakeholder's own words. You were not in the
conversation. Follow docs/SPEC-AUDIT-STEPS.md in the repository named below.

SPEC:          <path or paths>
VERBATIM:      <transcript path> --feature <name>   (or --from-match "<phrase>")
               <decision-document answer ids, if any>
SCOPE:         <which parts are author design [A] and which claim his intent, or "none">
ROADMAP_SEARCHES: <the author's two roadmap search records, each with its transcript line,
               or "not given">
AUTHOR_MODEL:  <the model that wrote the spec>
REPOSITORY:    <path of a checkout of this repository>
REPORT DIR:    <the one place you may write>

Run step 0 first. If an instrument self-test fails, stop and say so.

Audit in four directions, with equal weight: MISSING, ALTERED, INVENTED, AMBIGUOUS.
INVENTED is not an afterthought and MISSING is not the default. AMBIGUOUS findings
are written as questions he can answer.

Audit the spec's completeness claim, if it makes one.

Check the three layers: goals, requirements, functionality.

Check the two roadmap searches in ROADMAP_SEARCHES (step 3c); if it says "not given",
report NO-ROADMAP-SEARCH.

Do not trust the extractor: search the raw transcript yourself for his turns in
the same window and reconcile the count.

State what you examined and what you did NOT examine. Your output must never read
as coverage.

Write findings in the two citation conventions (Step 4), run the citation checker
on your own output, and report its result. End with a verdict bound to its frame.
```

## Deployment overlay

Everything above stands alone. A deployment may keep a fuller procedure, a named auditor agent, or extra product checklists. It records them in `private/design-session.local.md` (gitignored and excluded from publication; a worked example is `tools/design-session/design-session.local.example.md`). When that file exists the skills read it first; the procedure it names governs where it differs from this page, and where it names an auditor agent, that agent is dispatched instead of a hand-written brief. When the file is absent, or names a procedure that cannot be fetched, the audit runs from this page and the report says so. It never refuses to run because a deployment's own procedure is unavailable.
