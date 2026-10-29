"""p95 latency percentile for the SLA dashboard.

Computes the 95th percentile of a list of latency measurements (milliseconds)
so the on-call team can report it on the SLA dashboard.

Run the tests with:  python impl_prose.py

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
"""

from __future__ import annotations

import math


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

    print("all tests passed")


def _expect_valueerror(fn) -> None:
    try:
        fn()
    except ValueError:
        return
    raise AssertionError("expected ValueError but none was raised")


if __name__ == "__main__":
    _run_tests()
