"""KEY-LOSS lint — the rule whose absence cost `overview.lts` nineteen fields.

    A top-level bullet in a node body that MINTED NO PROPERTY, and whose shape says the
    author expected one, is a WARNING naming the line and the reason.

WHY A NEW RULE AND NOT A WIDER `coverage.py`
--------------------------------------------
The ratified [FL-LINE-ACCOUNTING] rule asks "did any parser branch READ this line?" and
owns a deliberately NARROW structure-shape test — 01 measured the naive predicate at 96%
false positive and tuned against it. That precision is correct for what it does and it is
why it must not be widened: widening it re-creates the 96%.

But measured 2026-08-13 across 81 live `.lts` files, 582 of the 766 intent-carrying
losses classify as `list_prose` under that rule — i.e. **76% of them are invisible to
`lts_lint` today**, including every one of the nineteen on `overview.lts`
(0 of 19 shown) and all 77 attempt-history lines on the live `defects/defect-graph.lts`.
So this is not a tuning gap, it is a different question:

    coverage.py :  was this line READ by a branch?          (line accounting)
    key_loss.py :  did this bullet MINT the field its shape  (property accounting)
                   promises?

SEVERITY: `warning`. The argument, since silence is what caused the defect.
-----------------------------------------------------------------------------
NOT `error`. Legitimate prose bullets exist and are common — 668 of the 1,434 dropped
bullets in the scanned set carry no intent signal at all. More decisively: at
introduction this rule emits 766 findings across the Acme corpora and 77 on the
defect registry alone. An error-severity introduction red-gates every corpus on day one,
and the documented consequence in this codebase is not a fixed corpus — it is
`--no-verify`. `coverage.py`'s own docstring already ruled it: "a gate everyone disables
is worth less than a warning everyone reads."

NOT silence, which is the failure being fixed. The file looked perfect for a month.

AND NOT A BARE WARNING EITHER — the third option, which is the actual defence. A warning
in a list of 766 warnings is indistinguishable from noise, and this codebase has shipped
two counters structurally incapable of incrementing. So the rule carries a SUMMARY
COUNTER (`key_loss_summary`) that is emitted **on every run, including a clean one**,
reporting authored-vs-minted per corpus. A reader who scrolls past 766 lines still sees

    -- key loss: 1434 top-level bullets in node bodies minted no property (766 flagged)

which is a number that can be watched, ratcheted, and — critically — can FALL. That is
what makes it an instrument rather than a nag.

THE RATCHET is `lint(key_loss_severity={"key_loss_high": "error"})`, per corpus, once
clean — the same mechanism `coverage.py` documents. Recommended first ratchet: the HIGH
band only (`md_emphasis_key`, `pictograph_key`, `uppercase_key`, `no_space_after_colon`,
`nonascii_bullet_marker`), which is 100% mechanical — every one of them coerces to a
legal key under `_coerce_key` with no authoring decision required.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import factlattice

from ..key_loss import HIGH, MEDIUM, NONE, SHAPES, Dropped, scan_text

ORIGIN = "key_loss"

# Two kinds, not thirteen. The SHAPE goes in `detail` where it is greppable; the KIND is
# the ratchet handle, and a ratchet with thirteen levers is one nobody pulls.
KIND_HIGH = "key_loss_high"        # mechanical: coerces to a legal key, no choice needed
KIND_MEDIUM = "key_loss_choice"    # needs an authoring decision (a rename), not a fix
KIND_SUMMARY = "key_loss_summary"  # the always-emitted counter — see the docstring

KEY_LOSS_SEVERITY: Dict[str, str] = {
    KIND_HIGH: "warning",
    KIND_MEDIUM: "warning",
    KIND_SUMMARY: "info",
}

_KIND_FOR = {HIGH: KIND_HIGH, MEDIUM: KIND_MEDIUM}


def _message(d: Dropped) -> str:
    why = "; ".join(SHAPES[s][1] for s in d.shapes)
    guess = (f" — would have been `{d.normalised_key}`"
             + (" (a key that parses elsewhere in this corpus)" if d.key_parsed_elsewhere else "")
             if d.normalised_key else "")
    return (f"authored bullet minted NO property{guess}. {why}. "
            f"The text survives for a human reader; the graph never sees it.")


def find_key_losses(corpus: "factlattice.ParsedCorpus") -> List[Dropped]:
    """Every intent-carrying dropped bullet in `corpus`, deterministic in file order.

    ⛔ Re-scans from `LineCoverage.lines` rather than re-reading the file. In
    `parse()`'s file-set mode the recorded `source_file` is a basename inside a temp dir
    that is deleted before the caller sees the result, so re-reading from the label is
    not merely wasteful, it is wrong. (Same reason `LineCoverage` retains `lines`.)
    """
    out: List[Dropped] = []
    for cov in corpus.coverage:
        rep = scan_text("\n".join(cov.lines), cov.source_file)
        out += [d for d in rep.dropped if d.confidence != NONE]
    return out


def key_loss_stats(corpus: "factlattice.ParsedCorpus") -> Dict[str, int]:
    """authored-vs-minted for the whole corpus. Emitted on EVERY run, clean or not.

    The RATIO is the honest framing, and it is the number that can fall. A rule that
    reports only findings cannot distinguish "this corpus keeps what it authors" from
    "the rule was switched off" — the exact ambiguity `profile_line()` exists to close
    one level up.
    """
    authored = in_body = minted = lost = flagged = 0
    for cov in corpus.coverage:
        rep = scan_text("\n".join(cov.lines), cov.source_file)
        authored += rep.authored_bullets
        in_body += rep.authored_in_node_bodies
        minted += rep.minted_properties
        lost += rep.loss
        flagged += rep.flagged
    return {"authored_bullets": authored, "authored_in_node_bodies": in_body,
            "minted_properties": minted, "lost_bullets": lost, "flagged": flagged}


def summary_line(stats: Dict[str, int]) -> str:
    return (f"-- key loss: {stats['lost_bullets']} top-level bullet(s) in node bodies "
            f"minted no property ({stats['flagged']} carrying an intent signal) "
            f"— {stats['authored_bullets']} authored / {stats['minted_properties']} minted")
