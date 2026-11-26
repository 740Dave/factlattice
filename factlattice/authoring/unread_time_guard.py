"""The engine's side of the placeholder-time guard: a thin caller of `unread_time_shared`.

It applies the quoting exemption (the engine always does), and turns hits into the sentence and `evidence` of the
writers' fixed refusal code. The detector, the quoting rule and the sentence live in the shared file and nowhere else.
Callers pass only text the call ADDS, never text already in a file.
"""
from __future__ import annotations

import os
import threading

from . import unread_time_shared as _shared

#: the fixed refusal code (SDLC 7.6 TOOL-REASON-IS-A-FIXED-CODE); the writers' `evidence.refusal` carries it.
CODE = "unread_time_placeholder"

#: FAIL OPEN (the maintainer's ruling): a fault in the DETECTOR itself never blocks a write. The write proceeds as if
#: there were no hit, the receipt carries `FAULT_CODE` with the exception's CLASS NAME (never its message, which may
#: hold a value), and the engine's write ledger gets one record of this kind (`edit._ledger_append`).
FAULT_CODE = "unread_time_guard_fault"

#: THE OFF SWITCH (the maintainer's ruling: its own setting, ON by default, that turns off only this check). It is an
#: environment variable, read with a LITERAL key in `mode()` and nowhere else (the repo's env-read guard requires a
#: literal). `off` turns the unread-time check off in the typed writers and `report_miss`; `on` or unset leaves it on;
#: ANY OTHER VALUE LEAVES IT ON and the receipt warns, so a typo can never disable it. It is env-only (no tool
#: argument, no file), and every receipt of a write made with the check off carries `unread_time_guard: off`.
ENV_NAME = "FACTLATTICE_UNREAD_TIME_GUARD"
RECEIPT_KEY = "unread_time_guard"

_PENDING = threading.local()


def mode() -> tuple:
    """-> ("on" | "off", bad) where bad is the unrecognised value (clipped) or None. Only a value that is `off` after
    trimming and lower-casing gives "off"; unset, empty and `on` give "on" with no complaint; anything else gives "on"
    and the value, so the receipt can name it."""
    raw = os.environ.get("FACTLATTICE_UNREAD_TIME_GUARD")
    if raw is None or not raw.strip():
        return "on", None
    v = raw.strip().lower()
    if v == "off":
        return "off", None
    if v == "on":
        return "on", None
    return "on", raw.strip()[:40]


def bad_value_warning(bad: str) -> str:
    return (f"{RECEIPT_KEY}: {ENV_NAME} has the unrecognised value {bad!r}; the check stays ON "
            f"(the only values are `on` and `off`)")


def stamp(out):
    """Put the switch's state on a receipt: `unread_time_guard: off` when the check was off for this write, and a warning
    naming an unrecognised value (the check stays on). Returns `out`; a non-dict is returned untouched."""
    if not isinstance(out, dict):
        return out
    state, bad = mode()
    if state == "off":
        out[RECEIPT_KEY] = "off"
    if bad is not None:
        out["warnings"] = list(out.get("warnings") or []) + [bad_value_warning(bad)]
    return out


def _faults() -> list:
    if not hasattr(_PENDING, "classes"):
        _PENDING.classes = []
    return _PENDING.classes


def pending() -> tuple:
    """The distinct exception class names the detector raised during the write now in progress on this thread."""
    return tuple(_faults())


def clear() -> None:
    """Every entry point of a write calls this when it starts and in its `finally`, so no fault outlives its write."""
    _faults().clear()


def begin() -> None:
    """Entry of a whole-document write path (`schema.write_document`, `edit.write_whole_file`). The OUTERMOST entry on a
    thread clears the fault list; a nested entry (`write_document` calls `write_whole_file` after its own checks have
    run) leaves it alone, so the outer write's faults are not lost."""
    depth = getattr(_PENDING, "depth", 0)
    if depth == 0:
        clear()
    _PENDING.depth = depth + 1


def end() -> None:
    """The matching exit, always in a `finally`: the outermost exit clears the list."""
    depth = max(getattr(_PENDING, "depth", 1) - 1, 0)
    _PENDING.depth = depth
    if depth == 0:
        clear()


def take() -> tuple:
    out = pending()
    clear()
    return out


def fault_warning(classes, recorded: bool) -> str:
    """The receipt line: the code and the class name(s), then what happened to the fault. It says "a record was made in
    the write ledger" ONLY when `recorded` is true (the fault record was really appended); otherwise it says plainly
    that none was made and that the fault will therefore not reach the alert that reads the ledger."""
    head = (f"{FAULT_CODE}: {', '.join(classes)} (the placeholder-time check could not run, so this write was NOT "
            f"checked for placeholder times; it was allowed through and ")
    if recorded:
        return head + "a record was made in the write ledger)"
    return head + "no ledger record was made, so this fault will not reach the maintainer's alert)"


def refusal(fields: dict):
    """(sentence, evidence) for the unquoted placeholder times in `fields` (name -> text), or None when there are none.
    Non-strings are skipped (another check owns them)."""
    if mode()[0] == "off":      # the off switch: this check only
        return None
    hits = []
    for name, text in fields.items():
        if not isinstance(text, str):
            continue
        try:
            found = _shared.find_unread_times(text, name, exempt_quoted=True)
        except Exception as exc:  # noqa: BLE001 -- FAIL OPEN: only the detector call is inside this try
            if type(exc).__name__ not in _faults():
                _faults().append(type(exc).__name__)
            continue
        hits.extend(found)
    if not hits:
        return None
    return _shared.refusal_text(hits), {
        "refusal": CODE, "field": hits[0].field, "count": len(hits),
        "hits": [{"field": h.field, "token": h.token, "line": h.line, "column": h.column} for h in hits]}
