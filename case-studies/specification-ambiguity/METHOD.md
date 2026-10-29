# Method

The design is deliberately simple so it can be re-run against any capable code model.

## Design

- **Blind.** Each implementer is a fresh agent with no shared context, no knowledge of the
  other conditions, and no knowledge of the hypothesis. None is told "be precise," "watch for
  ambiguity," or anything about specifications-vs-prose. Any precision in the output must come
  from the input the agent was given, not from the prompt framing.
- **Parallel and identical.** Within a round, the two implementers get an identical instruction
  shell. The *only* thing that differs is the specification embedded at the end.
- **Autonomous.** Each is told to implement without asking questions and to record any
  unspecified choice in an `ASSUMPTIONS` block. This simulates the realistic case where an agent
  codes to a spec with no human on hand to disambiguate — which is exactly the case where a bad
  substitution goes uncaught.
- **Standardized surface.** The callable signature is fixed in the shell (not the spec), so one
  neutral harness can run both implementations. Fixing the signature deliberately removes some
  ambiguity from *both* sides equally, isolating the semantic ambiguity under test.
- **Scored against intent by an independent oracle.** The `compare.py` in each round computes
  the ground truth itself and scores both implementations against it; it does not trust either
  agent's own tests.

## The instruction shell (rounds 1 and 2)

Each implementer received exactly this, with `<SIGNATURE>` and `<SPECIFICATION>` filled in:

```
Implement the software described in the specification below.

Requirements:
- Python, one self-contained file.
- Expose exactly this callable at module level: <SIGNATURE>
- Include tests.
- Implement autonomously: do NOT ask clarifying questions. If any behavior is
  unspecified, make a reasonable engineering choice and list each such choice in an
  ASSUMPTIONS comment block at the top of the file.

--- SPECIFICATION ---
<SPECIFICATION>
```

The `.lts` implementer additionally got one neutral line explaining the format: that
`### [ID] Title` lines are addressable items and `- key: value` lines are their properties. No
other difference.

- Round 1 signature: `allow_request(user_id: str, now: float) -> bool`
  - prose spec: `round1-ratelimiter/prose-spec.md`
  - lts spec:  `round1-ratelimiter/lts-spec.lts`
- Round 2 signature: `p95(latencies: list[float]) -> float`
  - prose request: `round2-p95/prose-request.md`
  - lts spec:  `round2-p95/lts-spec.lts`

## Round 3 — shorthand over a graph

One implementer, given the terse request in `round3-shorthand-graph/request.md` and told to
consult a corpus of convention nodes (not published with this repository) rather than guess. No precise spec was
written by a human; the implementer had to derive the method from the corpus's convention nodes.
Its output is `round3-shorthand-graph/impl_p95.py`; `check.py` scores it against the intended
nearest-rank result.

## Round 4 — the telephone game

Two implementers, identical shorthand request (`round4-telephone/request.md`), each building on
a different step-1 world:
- **no-graph**: given `existing_code.py` (the Round-2 prose implementation, which interpolates)
  and no corpus. Output: `impl_nograph.py`.
- **graph**: given the same unpublished corpus (now including the Round-3 p95 spec and an added
  `[DEC-SLA-BREACH-BOUNDARY]` convention). Output: `impl_graph.py`.

`compare2.py` scores both against the corpus-defined semantics.

## Reproduce it

Two ways. Rounds 1 and 2 are reproducible end to end from this repository. Rounds 3 and 4 are
reported results: the corpus their implementers consulted is not published, so you can score the
shipped implementations but not regenerate them.

**Verify this run** — run the shipped implementations against the oracles:

```
python3 round1-ratelimiter/compare.py
python3 round2-p95/compare.py
python3 round3-shorthand-graph/check.py
python3 round4-telephone/compare2.py
```

**Re-run from scratch (rounds 1 and 2 only)** — hand the shell + each spec/request file to any capable code model
under the blind conditions above, drop its output in place of the shipped `impl_*.py`, and run
the same harnesses. The result is a property of the specifications and the model, not of this
repository; a different model may land differently on Round 1 (the null) but the Round-2
divergence is driven by the specification content, not the implementer.

## What a re-run may and may not change

Round 1 is a null *for a model whose training already contains the correct answer*. A weaker or
differently-trained model may fail Round 1 too — that does not undermine the finding, it moves
the boundary. Rounds 2 and 4 depend on the correct answer being non-default and the corpus
carrying the convention; those hold regardless of the implementer, which is why the divergence
is expected to reproduce (round 4 as reported, since its corpus is not published).
