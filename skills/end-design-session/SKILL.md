---
name: end-design-session
description: Close a bracketed design session: repeat the roadmap or backlog search for the concept as designed, write the end sentinel, then run the independent spec audit. Extracts the stakeholder's verbatim turns deterministically, dispatches a cold auditor on a different model from the spec's author to diff the spec against them in four directions (MISSING, ALTERED, INVENTED, AMBIGUOUS), verifies every citation mechanically, and hands back a document of questions and corrections. Call it when the design conversation wraps up, BEFORE dispatching the build the spec describes.
user-invocable: true
---

# End a design session, and audit the spec against the stakeholder's own words

> ⛔ **You cannot do this audit yourself.** Your memory of the conversation IS the thing that failed. An author re-reading its own spec recalls that a point "was addressed" and reads the intent it meant into the words it wrote. That is why the comparison goes to an agent that was not in the room.
>
> In an early measurement on two specs the architect believed were faithful: nine corrections, five live ambiguities, and **three of four load-bearing defects were inventions or altered scope**. The most consequential: the architect's own phrase for a cast of characters was specified as every enrolled person, contradicting a two-tier model the stakeholder had stated explicitly, and it became the stated precondition of a whole architecture.
>
> The steps are in `docs/SPEC-AUDIT-STEPS.md`. Read them; do not restate them.

> ⭐ **Run it routinely, not perfectly.** In the authors' paraphrase (not a quotation), the stakeholder's view is that an audit which catches only some of the real issues is still worth running. A 50% instrument run five times beats a 90% instrument run once, and this one costs two scripts and one subagent. Effort spent perfecting the auditor is effort not spent running it again.

**Paths.** Every path in this skill is relative to the root of the FactLattice repository this skill is installed from. A skill is usually reached through a symlink, so find that root from the real location of this file, not from the path you were given or from your working directory:

```
python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' <path of this SKILL.md as you were given it>
git -C <the directory part of the path printed above> rev-parse --show-toplevel
```

The second command prints the repository root. Run every tool from there, and look for the overlay there.

**Deployment overlay.** If `private/design-session.local.md` exists under that root, read it before you begin. It names this deployment's auditor agent, its own audit procedure and how its roadmap is searched; where it differs from this skill, it governs. If it does not exist, or names something you cannot fetch, run from this skill and `docs/SPEC-AUDIT-STEPS.md`, and say in your report that you did. Never refuse to run because a deployment's own procedure is unavailable.

## Before Step 0: search the roadmap again, inside the bracket

A design often ends somewhere other than where it began, so repeat the roadmap or backlog search from `/begin-design-session` before the spec is finalised, for the concept **as finally designed**. Use the same search (the overlay's `roadmap-search`, or wherever the project keeps its roadmap and backlog), and record it in your reply in the same way: what you searched, the query terms, and the items found and considered, or that nothing was found. Say whether the extend-or-new answer is the same as at the start. If it changed, say how and ask the stakeholder; a design that turned out to extend an existing item is written as an extension.

Do this in a message **before** the one that writes the end marker, so that the record and his answer come before the close; the record reaches the auditor through brief item 8. If he does not answer in the session (he is away), mark your call `[A]`, write the end marker in your next message, and put the call to him with the audit's questions.

## Step 0: close the bracket

**Write this line, alone, at the start of your reply**, with the same feature name `/begin-design-session` opened:

```
⌞DESIGN-SESSION-END⌟ feature: <feature-name>
```

⛔ **The trigger is conversational, like the opening one.** Call this when the design conversation wraps up (he says "ok, go build it", or you are about to author the spec), not only when he types the command.

⛔ **The exact phrase is defined once, in `tools/design-session/extract_design_inputs.py`** (`SENTINEL_BEGIN` / `SENTINEL_END`). This file cites it; do not keep a second copy. And when you are *discussing* the mechanism rather than using it, wrap the phrase in backticks or a fence; see the contamination rules in `begin-design-session`.

If the overlay names steps to take when a session closes (for example leaving a focus mode and reading your inbox), do them now, before the audit.

## Step 1: extract, deterministically

```
python3 tools/design-session/extract_design_inputs.py <SESSION>.jsonl --feature <name> --json \
  > <scratch>/design-inputs-<feature>.json
```

Write the output to a scratch location outside any repository: an extract of a transcript never stays in a repository. Find the transcript with whatever your harness provides for locating a session's `.jsonl`. The extracted block includes **the human turn that triggered the session** (labelled `"role": "trigger"`); it precedes your BEGIN marker by construction and is routinely the most load-bearing turn in the whole session.

If the extractor EXITS 2 with "REFUSED: no sentinel bracket or typed sentinel block exists for feature ..." (since 2026-11-24: a named feature with no bracket is refused, never silently widened to the whole transcript), the stderr line lists the features that do have a bracket: use the right name, or take the fallback below. If `"boundaries": []`, no bracket registered. Check `sentinel_ignored_*` in the reconciliation: nonzero there means the mechanism was discussed rather than invoked. Fall back to `--from-match "<a phrase from the opening turn>"` and **say in the report that the bounds were approximate**. (Two limits remain. `--from-match` is ignored when a bracket exists elsewhere in the transcript, so check the slice's first statement. And on who spoke: only a turn marked `origin.kind == human` counts as the stakeholder's. Another agent's message (`peer`), a task notification, a tool result, a meta record and a compaction summary are excluded and counted by kind (the `origin` block of the JSON and the `origin` lines of the text output), and the citation checker returns NOT-STAKEHOLDER, naming the kind, for a quote taken from one. A message the stakeholder types while the assistant is working appears as a `queued_command` attachment whose mark is one level down: it counts only when `attachment.origin.kind == human` (its text is `attachment.prompt`; a quote may be cited at that attachment's line), and a `queued_command` with any other origin kind, or none, is excluded and counted as `queued_command:<kind>`. A `queue-operation` record carries no origin, so it counts only when its text equals (after whitespace normalisation) a human-origin user turn or a human-origin `queued_command` attachment; before the first origin-marked record it also counts when it equals an admitted early user turn (a fallback record, origin unknown). No prefix or extension credit, in any zone. The residual limit is the older format: a transcript with no origin field on any user record falls back to the old rule and says "origin unknown: this transcript predates origin marking; a turn's speaker is not verified". In a marked transcript, user records before the first origin-marked record (origin unknown) fall back the same way; they are counted separately as `before_first_marked_fallback` and the tools say "origin unknown: records before the first origin-marked record; their speaker is not verified". In either case check the origin of each quoted turn by hand. HARNESS TEXT IS MARKED HUMAN. Text the Gnomon harness types into a session comes out marked human (a user record or an attachment), so the origin rule admits it. The tools exclude a record whose text begins with one of the fixed prefixes "[Gnomon]", "[Gnomon@", "[GNOMON NOTICE]", "[GNOMON", "[DSD ANSWERED]" or "[scheduled:" and count it as harness-injection:<prefix>; that list is data, the public default plus any prefixes this deployment adds under a `harness-prefixes:` entry (one per line) in `private/design-session.local.md`, each run names its source ("default" or "default plus overlay"), and an overlay that cannot be read is a loud warning with the default applied; the checker calls a quote of it NOT-STAKEHOLDER. The one layout that is read: the `- harness-prefixes:` line, then indented `- prefix` items directly under it, no blank line inside, ended by a top-level bullet, a heading or the end of the file. Only the one layout above is read. Some departures from it warn; any other may be read in part, wrongly, or not at all, without a warning. After any overlay edit, check the parsed list itself: run the extractor with --json and read harness_prefixes.prefixes (the counts line shows only how many were added). This is a lexical tripwire and nothing more: it catches only the fixed prefixes, it cannot see harness text that lacks one, and an operator who types a line starting with one of them would be excluded. The structural answer (a harness ledger of injected text) is the harness owner's and is not built. A line that merely contains one of them mid-sentence is still admitted. When the quotes resolve only that way, the checker ends with its verdict "citations resolve; SPEAKER NOT VERIFIED (origin unknown)", then exits 3. The tools' `--help`, and the checker's banner, say the same.)

**Read the reconciliation block before going further.** It is not decoration:

- `BALANCES: yes`: every record landed in exactly one bucket. A `no` means a class was lost silently and the extraction is not trustworthy; stop and find it.
- `admitted (queued)`: statements that arrived on the **queue channel**, typed while you were working. These used to be dropped entirely and **they skew load-bearing**: a human interrupts a working assistant precisely when something important has just occurred to him.
- `fingerprint`: bytes, records, mtime. ⛔ **A live transcript grows under you.** Two runs that disagree on the statement count are indistinguishable from a lossy tool unless you compare fingerprints first. Exactly that confusion was once filed as a 25% extraction defect, investigated, and found to be two reads of a growing file.

## Step 2: dispatch the auditor

⭐ **Dispatch a cold auditor: an agent with NO conversational context, on a different model from the spec's author**, following `docs/SPEC-AUDIT-STEPS.md`. If the overlay names a deployment auditor agent, dispatch that agent; otherwise brief a general reviewer with the brief template in that document. Give it exactly its inputs and nothing else:

- `SPEC`: the spec file(s);
- `VERBATIM`: the transcript path plus `--feature <name>` (or `--from-match`), and any decision-document answer ids;
- `ROADMAP_SEARCHES`: your two search records with their transcript lines (item 8);
- `SCOPE`, if the spec tags author design against the stakeholder's intent;
- `AUTHOR_MODEL`: your model.

Don't brief it on what you expect it to find.

The reason for a different model is MODEL DIVERSITY, not cost. The auditor's whole product is INDEPENDENCE. A same-model auditor holds independent CONTEXT but still shares the author's model-level priors, so a systematic blind spot rooted in the author's weights can survive a same-model review: the one failure the audit exists to prevent, arriving through the back door. A different model brings different weights and different failure modes. ⛔ **The invariant is auditor model is not author model.** If you do not know your own model, say so and ask; do not guess. The diversity is within one vendor's family, so it is an improvement on independence, not a guarantee of it. Give the auditor its own branch or scratch area, no push, and read-only access to the transcript.

**The brief must carry all eight of these. Each one is here because it did work.**

1. **The four directions, named and given equal weight.**
   - **MISSING**: the stakeholder said it; the spec does not carry it.
   - **ALTERED**: the spec carries a CHANGED version of what he said.
   - **INVENTED**: the spec asserts something with NO origin in anything he said.
   - **AMBIGUOUS**: he said something admitting more than one valid reading, and the spec silently PICKED ONE without recording that it chose.

   ⛔ **INVENTED is not an afterthought and MISSING is not the default.** Three of four real defects were inventions. An auditor briefed to "check nothing was dropped" will find almost nothing.

   ⛔ **AMBIGUOUS is the valuable one.** The other three are corrections the architect applies; AMBIGUOUS is a question only the stakeholder can answer, and the only PREVENTIVE direction: it surfaces the fork *before* the guess hardens into a citation.

2. **"Audit the design doc's completeness claim too."** A document that says "all of the stakeholder's inputs are incorporated" is making a checkable assertion. Check it.

3. **"⛔ Do not trust the extractor: grep the transcript yourself."** On its first run this line found a real defect in the extractor, which was dropping every message the human typed mid-turn. Keep it even though that defect is fixed: its value is that it makes the auditor's evidence independent of the tool's, and the next tool defect will be a different one.

4. **"State what you examined and what you did NOT."** ⛔ The output must never read as COVERAGE. Four findings means four findings and says nothing about the other hundred nodes. A clean-looking report read as "the spec was audited and is fine" is the report that LOOKS complete so the reader acts blind. Require an explicit scope statement: files read, nodes examined, what was deliberately skipped and why.

5. **The citation conventions below**, quoted into the brief. They are what makes step 3 mechanical rather than guessed.

6. **Inputs:** the extracted-statements JSON, the spec file(s), the design doc, and the transcript path. ⛔ Do NOT hand it your own predictions about what the audit will find: reading them anchors the auditor to what you already suspect, and the independence is the entire product.

7. **The three layers: goals, requirements, functionality** (see `/begin-design-session`, "Capture GOALS and REQUIREMENTS"). Check that the spec states its goals, and its requirements as testable properties of ANY implementation, apart from the functionality.
   - Report **NO-GOALS** or **NO-REQUIREMENTS** when a layer is absent.
   - Report **UNTESTABLE** for a requirement with no test or measure.
   - Report **UNTRACED** for functionality that serves no stated requirement.
   - Report **GOAL-ATTRIBUTION** when a goal is presented as his words with no source, or an inferred one is not marked `[A]`.
   - These go in CORRECTIONS. A goal or requirement that only he can supply goes in QUESTIONS.

8. **The roadmap searches** (`docs/SPEC-AUDIT-STEPS.md` step 3c). Copy your two search records from your replies, with their transcript lines, into the brief as `ROADMAP_SEARCHES`: they are your text, not his, so the extract does not carry them. The auditor checks that both are present (a record that no searchable roadmap exists counts), each with an extend-or-new call, and that a design that extends an existing item is written as one. A missing search is reported as **NO-ROADMAP-SEARCH** in CORRECTIONS.

## ⛔ The two citation conventions: enforced mechanically

These are duplicated verbatim in `tools/design-session/verify_dsd_citations.py`. **If you change one, change the other.** A convention that lives in only one of the two places is a convention the next author will not follow.

**1. A human quote carries an attribution marker AND a transcript line:**

An invented example, not a real quotation:

```
- **Stakeholder said** (line 461): *"what gets loaded into a designed-into-the-game
  sensor is 10 players, but the storage service needs to hold an unbounded population."*
```

Required: `**<Name> said**`, then `(line <N>)`, then the quote in double quotes. Wrapping across markdown lines is fine. **Nothing else in the document is treated as a human quote**, which is the point: the document's own framing prose is then unconstrained, and the check on the claims that matter becomes exact. The line number is checked too, so a real quote attributed to the wrong turn is caught.

*Why this exists:* before it, the verifier treated every markdown blockquote as a human quote and flagged **98 of 106** on a real document, not one of them a fabrication. A verifier that flags 98 non-defects is worse than none, because the only sustainable response is to stop reading it, and at that moment the real findings hiding in the flood are lost.

**2. A node the document proposes CREATING is written `[NEW:<ID>]`:**

```
- **Recommendation:** add a `[NEW:WIDGET-ENTITY-DEVICE]` node.
```

`[PROPOSED:<ID>]` is a synonym. These are not expected to resolve; a plain `[<ID>]` still is. A `[NEW:<ID>]` that already exists is its own finding.

**Structure otherwise:** split AMBIGUOUS findings into a **QUESTIONS** section with `Your call: ____` slots, and MISSING/ALTERED/INVENTED into a **CORRECTIONS** section with a one-line `Ratify?` slot, since those are defects the architect fixes and the stakeholder merely ratifies. A selection the stakeholder made from options someone else wrote is cited as "the stakeholder SELECTED '<label>' (document, item)", never as "said".

## Step 3: verify the citations before the stakeholder sees it

```
python3 tools/design-session/verify_dsd_citations.py <AUDIT-DOCUMENT>.md \
    --transcript <SESSION>.jsonl --spec <spec1>.lts --spec <spec2>.lts
```

Exit codes: 0 means every citation resolved and the speaker is verified; 1 means unverified citations (a failed audit document); 3 means the citations resolve but the speaker is not verified (an unmarked transcript, or a quote that resolves only through a record before the first origin-marked record). Exit 3 is not a failed audit: report it with the verifier's own verdict "citations resolve; SPEAKER NOT VERIFIED (origin unknown)" and check those turns' origin by hand.

⛔ **Forwarding an unverified audit document is accepting a green report on faith**, the exact thing this project spends its time arguing against, arriving inside the tool built to catch it.

**The location of a hallucination is diagnostic; do not average the two numbers:**

- Fabricated **node ids** mean the brief was **OVER-SCOPED**. Your error as dispatcher. Remedy: a narrower brief (one spec, or one category, at a time).
- Fabricated **human quotes** mean the **METHOD is unsound**. Far deeper. The statements were a short list in a file the auditor was told to read.

Also run `python3 tools/design-session/verify_dsd_citations.py --self-test` (and the extractor's `--self-test`) before you rely on either. They are **negative controls**: they assert that a genuinely fabricated quote and a genuinely fabricated node id are still caught, and that a real wrapped quote, a `[NEW:]` proposal and the document's own framing blockquote are not. **A verifier tuned until it stops complaining is worthless.**

## Step 4: deliver

Put the audit document in front of the stakeholder through this deployment's decision-document channel (the overlay names it); with no overlay, hand it to him as a document with its answer slots. Then, when he answers: AMBIGUOUS answers become decision records that carry the source document; corrections get applied to the spec.

⛔ **Do not dispatch the build until the AMBIGUOUS questions are answered.** They are the ones that become preconditions of an architecture. The tell that you skipped this: *you are about to dispatch a build from a spec you authored, and your evidence that it matches the stakeholder's intent is that you remember writing it that way.*
