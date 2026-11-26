# `lts_preview` controls -- provenance

Two FROZEN, byte-exact fixtures. They are NEUTRALISED DERIVATIVES of two real specification
documents: every line, heading, bullet, indentation, id position and edge triple of the originals is
kept, but the prose has been replaced word-for-word with unrelated library-domain vocabulary and
every id renamed (`LIB-nn`, `RULE-nn`, `DEC-nn`, `REF-nn`). The originals are not published.

Why derivatives and not hand-made fixtures: the claim under test is that a document a human read
as correct, and that `lts_lint` reported **0 findings** on, silently dropped its own root node and
three authored relationships. That depends on the exact line-level SHAPE of a real document, and the
shape is what the derivation preserves.

**The derivation was checked, not assumed.** `lts_preview` was run on each original and on its
derivative, and the two results were compared leaf by leaf (every integer, boolean, list length and
line number, with ids and prose set aside). They are IDENTICAL except for the byte count of the
source. Every pinned number in `tests/test_preview.py` is therefore the original's number.

| fixture | role | sha256 |
|---|---|---|
| `spec41-broken-control.lts` | POSITIVE control: the tool must find its losses, with exact numbers | see `SPEC41_SHA` in `tests/test_preview.py` |
| `spec29-clean-control.lts`  | NEGATIVE control: must come back clean | see `SPEC29_SHA` in `tests/test_preview.py` |

## Why BOTH

`spec41` is the POSITIVE control. `spec29` is the NEGATIVE control, and it is not optional:
without a file that comes back **clean**, a tool that flags everything is indistinguishable from a
tool that works.

## Do not edit

`tests/test_preview.py::test_control_fixtures_are_byte_frozen` asserts both digests. An edit fails
there first, loudly, rather than quietly re-baselining the numbers every other assertion in that
file depends on.
