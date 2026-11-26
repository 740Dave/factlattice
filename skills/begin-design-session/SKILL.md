---
name: begin-design-session
description: Open a bracketed DESIGN SESSION on a named feature by writing the start sentinel into your reply, then summarise the concept and search the project's roadmap or backlog for similar items before designing. Call this WHENEVER the stakeholder signals a design conversation is starting ("let's design X", "I want to think through Y", "how should we build Z"), not only when he types the command. Closing it with /end-design-session runs an independent audit of the resulting spec against what he actually said.
user-invocable: true
---

# Begin a design session

> An architect authors a spec from a design conversation and reliably gets it subtly wrong. In an early measurement, an independent audit of two freshly authored specs found **nine corrections and five live ambiguities**, and **three of the four load-bearing defects were inventions rather than losses**: the spec asserted things the stakeholder never said. A check for missing items alone would have caught almost none of them.
>
> Opening the session costs one line of your reply; the summary and roadmap search below cost one short turn. This is the cheap half; `/end-design-session` does the work. The audit steps are in `docs/SPEC-AUDIT-STEPS.md`; read them rather than re-deriving the argument.

**Paths.** Every path in this skill is relative to the root of the FactLattice repository this skill is installed from. A skill is usually reached through a symlink, so find that root from the real location of this file, not from the path you were given or from your working directory:

```
python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' <path of this SKILL.md as you were given it>
git -C <the directory part of the path printed above> rev-parse --show-toplevel
```

The second command prints the repository root. Run every tool from there, and look for the overlay there.

**Deployment overlay.** If `private/design-session.local.md` exists under that root, read it before you begin. It names this deployment's own steps (an auditor agent, a focus-mode step, a decision-document tool, how its roadmap is searched) and where they differ from this skill they govern. If it does not exist, carry on with this skill alone.

## The trigger is conversational, not typed

The intent, in the authors' paraphrase (not a quotation): when the stakeholder says he wants to design something, the assistant calls the begin-design-session skill.

**You call this skill.** He should not have to remember a command in order to get the audit. If the mechanism depends on him typing something, it will run when he thinks of it rather than when it is needed, which is exactly backwards. Fire it when a conversation turns into design:

- "let's design X", "I want to think through Y", "how should we do Z"
- he starts specifying behaviour, vocabulary, entities or constraints for something not yet built
- you are about to author a spec, a decision node, or a dispatched build from what he is telling you

Ask nothing before opening it. Open the session and carry on. If you turn out to be wrong, the cost is a bracket around a conversation that produced no artifact, which is nothing.

## What to do

**Write this line, alone, at the very start of your reply:**

```
⌜DESIGN-SESSION-BEGIN⌝ feature: <feature-name>
```

Pick a short hyphenated `<feature-name>` from what he just said (`widget-identity`, `roster-view`). The name is what makes two interleaved design sessions separable and what the extractor's `--feature` filter matches on.

If the overlay names further steps to take when a session opens (for example entering a focus mode so that peers mail you instead of interrupting), do them in the same turn. If it names none, there are none.

Then add **one short sentence** naming what is being designed. Do not emit a banner around the marker: its job is to be found, and prose around it is noise in the very transcript the auditor will read. Before any design work, do the two steps in the next section.

## Before designing: summarise the concept, then search the roadmap

Overlapping work comes from ideas that feel new and are not. Two steps, in this order, before the design starts:

1. **Summarise the concept at a high level** in a few sentences: what it is, who it is for, and what it should achieve. Ask the stakeholder to confirm or correct the summary.
2. **Search the project's roadmap or backlog for similar items.** Use the search the overlay names (see `roadmap-search` in the overlay); if it names none, search wherever this project keeps its roadmap and backlog. Then say, in one line, whether this idea **extends an existing item** (name the item, and the roadmap or backlog it is on) or is **new** (naming the roadmap or backlog it would join), and ask the stakeholder which it is.

These steps set an order; they do not hold the design up. If the stakeholder is busy or away, write your summary and your extend-or-new call, mark both `[A]`, carry on, and put them to him for correction when he is next available.

**Record the search in your reply**: what you searched (the tool or files), the query terms, and the items you found and considered, or that nothing was found. If the project has no roadmap or backlog you can search, say so; that is a result too, not a skipped step. At close the record is copied into the auditor's brief (`/end-design-session`, brief item 8).

`/end-design-session` repeats the search before the spec is finalised, because a design often ends somewhere other than where it began.

## Capture GOALS and REQUIREMENTS, not only functionality

A stakeholder will usually describe WHAT to build, because the goal and the requirements are so clear to him that he does not say them. They have to be captured anyway, as the way of testing later whether a different implementation would serve the goal better, whether the implementation meets the requirements, and whether the requirements themselves need to change.

The spec must carry WHY and HOW WE'D VERIFY IT, in three layers, kept apart:

1. **Goals:** the outcome he wants, in his words where he said it. Not a feature, but what the feature is for.
2. **Requirements:** properties that ANY implementation must meet. Each one says how it would be tested or measured, so that a different design could be judged against it later. Write them so they would still be true if the functionality were replaced.
3. **Functionality / specification:** the design chosen to meet them. Each part traces to the requirement it serves.

During the conversation:

- When he jumps to functionality and the goal has not been said, ask once, in one line: *"What is this for, and how would we know a different design met it?"* Don't hold the design up for it. If he is busy or away, write your own inferred goals and requirements, mark each `[A]`, and put them to him for correction (in a decision document when he is next at the console).
- Never present an inferred goal as his words. Quote his words with their source; everything else is `[A]`.
- A requirement with no test or measure is not finished. Write "measure: TBD" rather than leave it implicit.
- Functionality with no requirement behind it is a finding. Either a requirement is missing, or the functionality is scope he did not ask for.

`/end-design-session`'s audit checks all three layers.

## Why the marker is text you write, and not something the harness stamps

The design, in the authors' paraphrase (not a quotation): a key start phrase in the assistant's reply is kept in the transcript, with a similar one when the session ends, and that can be checked against any live transcript.

An earlier design keyed the boundary on the record a slash command leaves behind. It was rejected, and the reasons are worth carrying:

1. **It depended on harness internals.** A record shape is an implementation detail that can change silently, taking the mechanism with it and reporting nothing.
2. **It was not verifiable by eye.** This one is: `grep -n 'DESIGN-SESSION-BEGIN' <session>.jsonl`.
3. **It was invisible to the stakeholder.** He sees this marker appear in your message and knows the bracket is live. A silently failed invisible bracket is discovered only when an audit comes back with nothing to say.
4. **It was more complicated.** The extractor now needs no knowledge of command records at all.

⛔ **The exact phrase is defined once, in `tools/design-session/extract_design_inputs.py`** (`SENTINEL_BEGIN` / `SENTINEL_END`; `python3 tools/design-session/extract_design_inputs.py --print-sentinels` prints them). This file cites it. **Do not maintain a second copy anywhere**: two copies of a sentinel drift, and a drifted sentinel brackets nothing while looking correct. `tests/test_design_session_tools.py` fails if either skill's line differs from the extractor's constants.

## When you DISCUSS this mechanism, do not accidentally open a session

You will write the sentinel phrase in replies that talk *about* design sessions; this file does it, and so will any explanation of the mechanism. The extractor distinguishes **asserting** the marker from **quoting** it. A mention is ignored unless it clears all four rules:

1. it is in an **assistant** record (a human quoting it never moves the bracket);
2. it **starts its own line** (at most 3 leading spaces);
3. it is **not** inside a fenced code block or a `>` blockquote;
4. it carries **`feature: <name>`**.

**So when you are talking about the mechanism rather than using it, wrap the phrase in backticks or a code fence.** That is the normal way to quote a token anyway, and it is what makes the mention safe.

## Verifying a bracket landed

```
python3 tools/design-session/extract_design_inputs.py <SESSION>.jsonl --json \
  | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["boundaries"], indent=2))'
```

An entry with `"kind": "begin"`, the transcript line, and your feature name. Or just grep for the phrase; that is the whole point of choosing text over a record shape.

⛔ **The reconciliation block reports sentinel mentions that were IGNORED.** If a session shows mentions but zero boundaries, you discussed the mechanism instead of invoking it. That is a different failure from "the human said nothing", and the tool says which.

## Fallback: typed sentinels

Still supported, for bracketing after the fact or across sessions. The stakeholder types, on its own line: `FEATURE DEFINITION START: <feature-name>` ... `FEATURE DEFINITION STOP: <feature-name>` (case-insensitive). The assistant-written sentinel is the same idea moved to the turn that reliably happens.

## When NOT to open one

A design session earns its bracket when the conversation will produce something an auditor can diff: a spec, a decision node, a dispatched build. A quick factual question does not. The audit costs one subagent; running it on a conversation with no artifact produces a document with nothing to say.
