"""p95 latency percentile + SLA compliance check for the dashboard.

Computes the 95th percentile of a list of latency measurements (milliseconds)
and reports whether we are currently meeting the 100ms p95 SLA, so the
on-call team can show a green/red status on the SLA dashboard.

Run the tests with:  python impl_nograph.py

============================================================================
ASSUMPTIONS
============================================================================
The spec left several behaviors unspecified. Choices made:

1. INTERPOLATION METHOD: We use linear interpolation between the two closest
   ranks (the "linear" / type-7 method, same as numpy.percentile default and
   Excel's PERCENTILE.INC). For SLA reporting this is the most widely
   recognized convention, so dashboard numbers match what people expect from
   spreadsheets and numpy. (Alternatives considered: nearest-rank, which is
   common in monitoring tools like Prometheus; we chose linear for
   familiarity, but note the difference below.)

2. EMPTY INPUT: p95 of an empty list is undefined. We raise ValueError rather
   than returning 0.0 or NaN, because silently reporting a fake latency on an
   SLA dashboard would be misleading to the on-call team.

3. SINGLE ELEMENT: p95 of a one-element list is that element.

4. INPUT NOT MUTATED: We sort a copy, so the caller's list is left untouched.

5. INPUT VALIDATION: Non-numeric values, NaN, and negative latencies are
   rejected with ValueError. Negative latency is physically meaningless, and
   NaN would silently corrupt the sort/result. Booleans are rejected too
   (True/False are technically ints in Python but are almost certainly a bug
   in a latency list). Infinity is allowed (a timeout sentinel is a legitimate
   measurement of "very slow").

6. RETURN TYPE: Always returns float, even for integer inputs, for a
   consistent dashboard display format.

============================================================================
COMPLIANCE CHECK (is_p95_compliant) — additional choices
============================================================================

7. SLA THRESHOLD: 100.0 ms, exposed as the module constant SLA_P95_MS so the
   dashboard (and tests) reference one source of truth instead of a literal.

8. COMPARISON / BOUNDARY: We are compliant when p95 <= 100.0 ms, i.e. the
   comparison is `p95(latencies) <= SLA_P95_MS`. The boundary value of exactly
   100.0 ms is treated as COMPLIANT (returns True). Rationale: an SLA of
   "100ms p95" is an upper bound the service is promising to stay at or under;
   hitting it exactly still honors the promise. Only a p95 strictly above
   100ms is a breach.

9. EMPTY / INVALID INPUT: is_p95_compliant does NOT swallow errors. If p95()
   raises ValueError (empty list, NaN, negative, non-numeric, bool), that
   exception propagates. Returning False for "no data" would paint the
   dashboard red on a breach we cannot actually confirm; returning True would
   hide a real breach. The caller decides how to render "unknown/no data",
   which is a distinct state from "not compliant".
============================================================================
"""

from __future__ import annotations

import math


# The p95 latency (in milliseconds) we promise to stay at or under.
SLA_P95_MS: float = 100.0


def p95(latencies: list[float]) -> float:
    """Return the 95th percentile of the given latency measurements (ms).

    Uses linear interpolation between closest ranks (numpy/Excel default).

    Raises:
        ValueError: if the list is empty, or contains a non-numeric value,
            NaN, a boolean, or a negative number.
    """
    if not latencies:
        raise ValueError("cannot compute p95 of an empty list")

    cleaned: list[float] = []
    for i, x in enumerate(latencies):
        if isinstance(x, bool):
            raise ValueError(f"latency at index {i} is a bool, not a number: {x!r}")
        if not isinstance(x, (int, float)):
            raise ValueError(f"latency at index {i} is not a number: {x!r}")
        xf = float(x)
        if math.isnan(xf):
            raise ValueError(f"latency at index {i} is NaN")
        if xf < 0:
            raise ValueError(f"latency at index {i} is negative: {xf}")
        cleaned.append(xf)

    return _percentile(cleaned, 95.0)


def is_p95_compliant(latencies: list[float]) -> bool:
    """Return True if we are currently meeting the 100ms p95 SLA.

    Compliant means the 95th-percentile latency is at or below the SLA
    threshold: ``p95(latencies) <= SLA_P95_MS``. Exactly 100.0 ms counts as
    compliant (the SLA is an at-or-under promise); a p95 strictly above
    100.0 ms is a breach.

    This is the function the dashboard calls to show green (True) vs.
    red (False).

    Raises:
        ValueError: propagated from :func:`p95` for empty/invalid input.
            "No data" is not the same as "not compliant", so the caller must
            decide how to render that distinct state rather than have this
            function guess.
    """
    return p95(latencies) <= SLA_P95_MS


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (type 7). `values` is non-empty."""
    s = sorted(values)
    n = len(s)
    if n == 1:
        return s[0]
    # Rank in [0, n-1] space.
    rank = (pct / 100.0) * (n - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return s[int(rank)]
    frac = rank - lo
    return s[lo] * (1.0 - frac) + s[hi] * frac


# ============================================================================
# Tests
# ============================================================================

def _almost(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def _run_tests() -> None:
    # ---- p95 tests (unchanged) --------------------------------------------
    # Single element.
    assert p95([42.0]) == 42.0

    # Two elements: rank = 0.95 * 1 = 0.95 -> interp between 10 and 20.
    assert _almost(p95([10.0, 20.0]), 19.5)

    # 1..100: numpy.percentile(range(1,101), 95) == 95.05
    assert _almost(p95([float(i) for i in range(1, 101)]), 95.05)

    # 0..99: rank = 0.95 * 99 = 94.05 -> 94 + 0.05*(95-94) = 94.05
    assert _almost(p95([float(i) for i in range(0, 100)]), 94.05)

    # Unsorted input gives same result as sorted.
    data = [30.0, 10.0, 50.0, 20.0, 40.0]
    assert _almost(p95(data), p95(sorted(data)))

    # All identical.
    assert p95([7.0, 7.0, 7.0, 7.0]) == 7.0

    # Integer input accepted, returns float.
    r = p95([1, 2, 3, 4, 5])
    assert isinstance(r, float)

    # Input list is not mutated.
    original = [5.0, 1.0, 3.0, 2.0, 4.0]
    snapshot = list(original)
    p95(original)
    assert original == snapshot

    # Infinity allowed (timeout sentinel).
    assert p95([1.0, 2.0, 3.0, float("inf")]) == float("inf")

    # Empty raises.
    _expect_valueerror(lambda: p95([]))
    # NaN raises.
    _expect_valueerror(lambda: p95([1.0, float("nan"), 2.0]))
    # Negative raises.
    _expect_valueerror(lambda: p95([1.0, -2.0, 3.0]))
    # Non-numeric raises.
    _expect_valueerror(lambda: p95([1.0, "slow", 3.0]))  # type: ignore[list-item]
    # Bool raises.
    _expect_valueerror(lambda: p95([1.0, True, 3.0]))  # type: ignore[list-item]

    # ---- is_p95_compliant tests -------------------------------------------
    # Comfortably under the SLA -> compliant.
    assert is_p95_compliant([10.0, 20.0, 30.0, 40.0, 50.0]) is True

    # All well over the SLA -> not compliant.
    assert is_p95_compliant([200.0, 300.0, 400.0]) is False

    # Boundary: p95 lands exactly on 100.0 -> compliant (at-or-under).
    # All values 100.0 => p95 == 100.0.
    assert is_p95_compliant([100.0] * 10) is True

    # Boundary via interpolation: two points 100 and 100 -> exactly 100.
    assert is_p95_compliant([100.0, 100.0]) is True

    # Deterministic boundary check: p95 just above threshold -> breach.
    # Uniform list => p95 == the value, independent of interpolation.
    over = [100.0000001] * 5
    assert _almost(p95(over), 100.0000001)
    assert is_p95_compliant(over) is False

    # Deterministic boundary check: p95 exactly at threshold -> compliant.
    at = [100.0] * 5
    assert _almost(p95(at), 100.0)
    assert is_p95_compliant(at) is True

    # Deterministic boundary check: p95 just under threshold -> compliant.
    under = [99.9999999] * 5
    assert _almost(p95(under), 99.9999999)
    assert is_p95_compliant(under) is True

    # Single element under / at / over.
    assert is_p95_compliant([42.0]) is True
    assert is_p95_compliant([100.0]) is True
    assert is_p95_compliant([100.1]) is False

    # Infinity (timeout) is never compliant.
    assert is_p95_compliant([1.0, 2.0, 3.0, float("inf")]) is False

    # Return type is a real bool.
    assert isinstance(is_p95_compliant([1.0]), bool)

    # Invalid/empty input propagates (does NOT silently return a bool).
    _expect_valueerror(lambda: is_p95_compliant([]))
    _expect_valueerror(lambda: is_p95_compliant([1.0, float("nan")]))
    _expect_valueerror(lambda: is_p95_compliant([-1.0]))

    # Threshold constant is the single source of truth.
    assert SLA_P95_MS == 100.0

    print("all tests passed")


def _expect_valueerror(fn) -> None:
    try:
        fn()
    except ValueError:
        return
    raise AssertionError("expected ValueError but none was raised")


if __name__ == "__main__":
    _run_tests()
