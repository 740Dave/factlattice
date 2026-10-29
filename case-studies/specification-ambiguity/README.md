# Specification ambiguity and implementation divergence

*Four experiments in when a spec is worth writing: rounds 1-2 are fully reproducible from this repository; rounds 3-4 are reported results (their corpus is not published).*

Every experiment here hands a small software task to a fresh, isolated implementer with no
shared context and no instruction to "be careful" or "be precise." Then the resulting code is
run against identical inputs and scored against the intended behavior. Rounds 1 and 2 can be
re-run from scratch with what is in this directory. For rounds 3 and 4 the shipped implementations
and their scoring harnesses are here and run, but the corpus the implementers consulted is not
published, so those two rounds are reported results you can score, not re-run. Each round has a
`compare.py` (or `compare2.py` / `check.py`) you can run yourself:

```
python3 round1-ratelimiter/compare.py
python3 round2-p95/compare.py
python3 round4-telephone/compare2.py
```

The point is not "ambiguity is bad." Round 1 shows ambiguity is often *fine*. The point is to
locate exactly when it stops being fine, and what a knowledge graph does about it.

---

## Round 1 — rate limiter · ambiguity was harmless

`round1-ratelimiter/` — request: *"up to 5 requests every 10 seconds."* Intended behavior:
a sliding window.

The implementer working from that one sentence chose a sliding window **and explicitly noted
the fixed-window trap it was avoiding.** It matched the intent on every timeline. The precise
spec (`lts-spec.lts`) produced identical results. The spec was redundant.

**Why:** rate-limiting is one of the most heavily documented problems in software, and "sliding
vs fixed window" is a famous distinction. A capable implementer already holds the correct
answer *and* an awareness of the ambiguity. Writing a spec bought nothing.

---

## Round 2 — 95th-percentile latency · ambiguity produced confident, tested, wrong code

`round2-p95/` — request: *"compute the 95th percentile."* Intended behavior: nearest-rank
(the reported number must be a latency some request actually experienced).

The implementer chose linear interpolation — the numpy/Excel default — **considered nearest-rank
and explicitly rejected it**, and reported "all tests passed." Scored against intent:

```
case                     intended   prose    spec
discriminator [10..100]    100.0     95.5    100.0    prose WRONG
20 evenly spaced            95.0     95.25    95.0    prose WRONG
small n=3                    3.0      2.9      3.0    prose WRONG
```

Same input, different answer. The dashboard would report 95.5ms where the intended metric is
100ms — a number that never occurred in the data — and nothing in "tests passed" reveals it.

**Why the difference from Round 1:** there is no single famous-correct percentile method. numpy,
pandas, Excel and Python's `statistics` all interpolate by default; latency SLAs conventionally
use nearest-rank. The prose gave no way to know which, so the implementer guessed the library
default and justified it fluently.

### The boundary between Round 1 and Round 2 is the finding

> **Danger ≈ (obscurity of the correct answer) × (adjacency + documentation of the wrong one).**

Rate-limiting scores near zero on the first factor — the correct answer is famous — so ambiguity
was safe. The percentile method scores high on both — the correct answer is one of several
documented options and not the default — so the same style of ambiguity produced silent,
confident error. This is why a null result (Round 1) matters: it shows the risk has a boundary,
and locates it.

---

## Round 3 — the same task, from shorthand, over a graph · correct with no spec written

`round3-shorthand-graph/` — the interesting case. The human writes *no precise spec*. The
implementer gets only a terse request — *"add a p95 to the dashboard alongside the p99 and p50
we already have"* — plus a small knowledge corpus (`corpus/`) that had already settled the
convention:

```
[DEC-PERCENTILE-METHOD]  "Every percentile metric ... is computed by the nearest-rank method
                          ... Interpolation between ranks is not used for any SLA metric."
```

Result: **nearest-rank, 100.0 on the discriminator — correct.** The implementer cited that exact
node as its reason and reported it had to guess nothing. The word "nearest-rank" appears nowhere
in the request.

The same p95 task, three ways:

| how the task was given | result | who paid for the precision |
|---|---|---|
| shorthand alone | 95.5 — wrong | nobody; the implementer guessed |
| precise spec | 100.0 — right | the human wrote the spec |
| **shorthand + graph** | **100.0 — right** | the corpus, once; the human spoke shorthand |

A precise spec beats ambiguity, but that only relocates the cost of precision onto whoever
writes the spec. A graph *supplies* the precision: the human states intent loosely, and each
gap is filled from what the domain already decided rather than from a training prior. Filling a
gap from the corpus is the safe inverse of filling it from the prior.

---

## Round 4 — one more enhancement, by shorthand, on each branch · the verdict flips

`round4-telephone/` — a "telephone game": add one more feature by shorthand on top of each
branch's world. Request: *"add a compliance check for our 100ms p95 SLA."*

- The **no-graph** branch inherits the interpolated p95 (95.5) as existing code and must guess
  the breach boundary.
- The **graph** branch reads the corpus, which now carries a committed convention with an
  incident rationale: `[DEC-SLA-BREACH-BOUNDARY]` — *"at-target is a breach; compliant iff
  p95 < 100."*

```
case                     intended            no-graph            graph
[10..100] (flip case)    BREACH (red)        COMPLIANT (green)   BREACH (red)     <- VERDICT FLIP
all low  [1..10]         COMPLIANT (green)   COMPLIANT (green)   COMPLIANT (green)
all high [110..200]      BREACH (red)        BREACH (red)        BREACH (red)
```

On identical data the two branches return **opposite operational verdicts.** The no-graph branch
reports the SLA is met while it is being missed. Both branches pass their own tests.

The failure compounded from two *individually reasonable* choices: the inherited interpolation
(never re-examined) and an inclusive `<=` boundary ("hitting 100ms exactly still honors the
promise"). Neither is a careless mistake. But the corpus records an incident — *"a flat 100ms
p95 was dashboarded green while requests were failing the promise"* — that resolves the boundary
the other way, by name. Two sensible guesses, stacked across a session boundary, produced a
green light on a red condition.

The all-low and all-high controls agree across every branch, so the flip is real, not an
artifact of a rigged input: it appears only where the compounded errors actually bite.

---

## What the four show together

1. A precise specification beats ambiguity — but that only moves the cost of precision onto its
   author (Rounds 1–2).
2. A knowledge graph supplies the precision, so the human keeps the cheap shorthand and the
   answer is still grounded — auditable against the corpus rather than taken on trust (Round 3).
3. An unflagged wrong guess does not stay put. It becomes an inherited premise and compounds
   across a session boundary into a wrong decision (Round 4). A graph stops the compounding,
   because the next implementer grounds against the corpus instead of the previous one's guess.

The corpus never made an implementer smarter. It stated the distinction once — including the
explicit "this is *not* the default" — so the choice was made by the author, one time, instead
of guessed by every implementer, silently, indefinitely.
