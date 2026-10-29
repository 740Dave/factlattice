# `lts_preview` controls -- provenance

Two FROZEN, byte-exact fixtures. They are DERIVATIVES of two real specification documents, produced
in two passes. Pass 1 kept every line, heading, bullet, indentation, id position and edge triple of the
originals, replaced the prose word-for-word with unrelated library-domain vocabulary, and renamed every id
(`LIB-nn`, `RULE-nn`, `DEC-nn`, `REF-nn`). That pass left some real tokens in place (hardware board and chip
names, a build command, source and tool paths, file names of sibling documents, and real dates). Pass 2
replaced those too, with invented names of the same shape: board, chip and part names became invented
ones (`GIZMO32`, `KIT`, `WGT003`, and similar), the build command and tool name were renamed, every source
path and sibling file name was replaced by an invented one, and every date was moved to an invented date.
Technical acronyms that name a generic technology (for example `MCU`, `SPI`, `I2C`, `PWM`, `USB`, `OTA`)
were kept. The fixture file names themselves are unchanged. The originals are not published.

Why derivatives and not hand-made fixtures: the claim under test is that a document a human read
as correct, and that `lts_lint` reported **0 findings** on, silently dropped its own root node and
three authored relationships. That depends on the exact line-level SHAPE of a real document, and the
shape is what the derivation preserves.

**The derivation was checked, not assumed.** `lts_preview` was run on each original and on its
derivative, and the two results were compared leaf by leaf (every integer, boolean, list length and
line number, with ids and prose set aside). They were IDENTICAL after pass 1 except for the byte count of the
source. Pass 2 changed words, paths and dates only (no line, bullet, heading or id was added, removed or
moved), and `tests/test_preview.py` passes unchanged except for the two sha256 digests, which were
re-derived from the new bytes. Every other pinned number in that file is unchanged.

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
