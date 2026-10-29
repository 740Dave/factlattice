"""p95 latency computation for the SLA dashboard.

Implements [SPEC:P95] — the 95th-percentile by the NEAREST-RANK method,
pinned to one method so every dashboard and alert agrees on the number.

Method [DD-NEAREST-RANK]: sort ascending, rank r = ceil(0.95 * n) (1-indexed),
return sorted[r-1]. This returns an actually-observed measurement, never an
interpolated point between two measurements ([INV-OBSERVED], [INV-DETERMINISTIC]).

ASSUMPTIONS
-----------
The spec fully pins the computation. The following were unspecified; choices made:

1. NON-NUMERIC / NaN INPUT: The spec types the argument as list[float] and does
   not define behavior for NaN or non-numeric elements. Choice: do not add
   special NaN handling or type coercion. NaN would sort unpredictably, but the
   spec's domain is real latency measurements; guarding this is out of scope and
   would add non-spec behavior. Non-float numerics (int) sort/compare fine and
   are accepted as-is.

2. RETURN TYPE FOR INTEGER INPUTS: signature says -> float. Choice: return the
   selected element unchanged (e.g. an int input yields the int's value). The
   spec's INV-OBSERVED requires returning an element *of the input*, and the
   acceptance tests compare against float literals (100.0, 42.0) which equal the
   underlying values. We do NOT coerce to float, to preserve exact element
   identity per INV-OBSERVED. Callers passing floats get floats.

3. INPUT MUTATION: Choice: do not mutate the caller's list; sort a copy.

4. NEGATIVE / ZERO LATENCIES: Not addressed by spec. Choice: no validation of
   value ranges; the rank formula applies uniformly. (r >= 1 always holds for
   n >= 1 since ceil(0.95*1) = 1.)
"""

import math


def p95(latencies: list[float]) -> float:
    """Return the 95th-percentile latency by the nearest-rank method.

    See [DM-P95], [DD-NEAREST-RANK], [DD-EDGES].

    Raises:
        ValueError: if ``latencies`` is empty.
    """
    n = len(latencies)
    if n == 0:
        raise ValueError("p95() requires at least one latency measurement")
    ordered = sorted(latencies)
    r = math.ceil(0.95 * n)  # 1-based rank
    return ordered[r - 1]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def _test_discriminator():
    # [TEST-DISCRIMINATOR] separates nearest-rank from interpolation.
    latencies = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    result = p95(latencies)
    assert result == 100.0, f"expected 100.0 (nearest-rank), got {result}"
    # Guard against an interpolating implementation sneaking back in.
    assert result != 95.5, "interpolation detected — violates INV-OBSERVED"


def _test_single():
    # [TEST-SINGLE]
    assert p95([42.0]) == 42.0


def _test_worked_example():
    # [DD-NEAREST-RANK] worked example, same as discriminator.
    assert p95([10, 20, 30, 40, 50, 60, 70, 80, 90, 100]) == 100


def _test_empty_raises():
    # [DD-EDGES] empty input raises ValueError.
    try:
        p95([])
    except ValueError:
        pass
    else:
        raise AssertionError("empty input must raise ValueError")


def _test_observed_invariant():
    # [INV-OBSERVED] result is always an element of the input.
    import random

    rng = random.Random(1234)
    for _ in range(1000):
        n = rng.randint(1, 200)
        data = [rng.uniform(0, 1000) for _ in range(n)]
        result = p95(data)
        assert result in data, "result must be an observed element"


def _test_deterministic():
    # [INV-DETERMINISTIC] same input yields same result; order-independent.
    data = [5.0, 1.0, 9.0, 3.0, 7.0, 2.0, 8.0, 4.0, 6.0, 10.0]
    first = p95(data)
    assert p95(data) == first
    assert p95(list(reversed(data))) == first


def _test_no_mutation():
    # ASSUMPTION 3: input list is not mutated.
    data = [3.0, 1.0, 2.0]
    snapshot = list(data)
    p95(data)
    assert data == snapshot, "input list must not be mutated"


def _test_rank_boundaries():
    # n=20 -> r = ceil(19.0) = 19 -> sorted[18].
    data = list(range(1, 21))  # 1..20
    assert p95(data) == 19
    # n=1 -> r = ceil(0.95) = 1.
    assert p95([7.0]) == 7.0
    # n=2 -> r = ceil(1.9) = 2 -> the max.
    assert p95([4.0, 9.0]) == 9.0
    # n=100 -> r = ceil(95.0) = 95 -> sorted[94] == 95.
    assert p95(list(range(1, 101))) == 95


def _run_all():
    tests = [
        _test_discriminator,
        _test_single,
        _test_worked_example,
        _test_empty_raises,
        _test_observed_invariant,
        _test_deterministic,
        _test_no_mutation,
        _test_rank_boundaries,
    ]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    _run_all()
