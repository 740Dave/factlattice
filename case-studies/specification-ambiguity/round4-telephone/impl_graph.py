"""p95 SLA compliance check for the on-call dashboard.

Every decision here is grounded in the team's knowledge corpus (ab-experiment3/corpus):

- p95 method: nearest-rank, no interpolation.
    Driven by [SPEC:P95] and [DEC-PERCENTILE-METHOD] in sla-metrics.lts:
    sort ascending; rank r = ceil(0.95 * n), 1-indexed; return sorted[r-1].
    (This upholds [INV-OBSERVED-METRIC]: a reported figure is always a real
    observed latency, never an interpolated value no request ever saw.)

- Empty sample: raises ValueError.
    Driven by [DEC-EMPTY-RAISES]: the dashboard shows "no data", never a
    fabricated 0.

- SLA target: 100ms; compliance boundary is STRICT.
    Driven by [DEC-SLA-BREACH-BOUNDARY]: compliant iff p95 < 100.0;
    a p95 equal to the 100ms target is a BREACH (>= 100.0).
"""

from math import ceil

# [DEC-SLA-BREACH-BOUNDARY]: the p95 latency SLA target is 100ms.
P95_SLA_TARGET_MS = 100.0


def p95(latencies: list[float]) -> float:
    """Return the 95th-percentile latency by the nearest-rank method.

    Per [SPEC:P95] / [DEC-PERCENTILE-METHOD]: sort ascending, rank
    r = ceil(0.95 * n) (1-indexed), return sorted[r-1]. No interpolation.

    Raises ValueError on an empty sample, per [DEC-EMPTY-RAISES].
    """
    n = len(latencies)
    if n == 0:
        raise ValueError("p95 of an empty sample is undefined (no data)")
    ordered = sorted(latencies)
    r = ceil(0.95 * n)  # 1-indexed rank
    return ordered[r - 1]


def is_p95_compliant(latencies: list[float]) -> bool:
    """Return True iff the team is currently meeting its p95 SLA.

    Compliance rule, per [DEC-SLA-BREACH-BOUNDARY]:
        compliant  iff  p95(latencies) <  100.0   (STRICTLY below target)
        breach     iff  p95(latencies) >= 100.0   (at-or-above is a breach)

    An empty sample raises ValueError (via p95), per [DEC-EMPTY-RAISES];
    "no data" is not the same as "compliant".
    """
    return p95(latencies) < P95_SLA_TARGET_MS


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def _test_p95_nearest_rank_worked_examples():
    # [SPEC:P95] worked-example: n=200 -> r = ceil(0.95*200) = 190 -> sorted[189]
    data = list(range(1, 201))  # 1..200, already sorted
    assert p95(data) == 190, "n=200 should return the 190th value (sorted[189])"

    # [SPEC:P95] worked-example: n=20 -> r = ceil(0.95*20) = 19 -> sorted[18]
    data = list(range(1, 21))  # 1..20
    assert p95(data) == 19, "n=20 should return the 19th value (sorted[18])"

    # [SPEC:P95] worked-example: n=1 -> r = ceil(0.95*1) = 1 -> sorted[0]
    assert p95([42.0]) == 42.0, "n=1 should return the only element"


def _test_p95_sorts_input():
    # Nearest-rank sorts ascending first; input order must not matter.
    assert p95([200.0, 1.0, 50.0, 3.0, 2.0]) == p95([1.0, 2.0, 3.0, 50.0, 200.0])
    # n=5 -> r = ceil(0.95*5) = ceil(4.75) = 5 -> sorted[4] = max
    assert p95([200.0, 1.0, 50.0, 3.0, 2.0]) == 200.0


def _test_p95_returns_observed_value():
    # [INV-OBSERVED-METRIC]: result is always an element of the sample.
    import random
    sample = [random.random() * 500 for _ in range(137)]
    assert p95(sample) in sample


def _test_p95_empty_raises():
    # [DEC-EMPTY-RAISES]
    try:
        p95([])
    except ValueError:
        pass
    else:
        raise AssertionError("p95([]) must raise ValueError")


def _test_compliant_strictly_below_target():
    # p95 well under 100ms -> compliant.
    assert is_p95_compliant([10.0, 20.0, 30.0, 40.0, 50.0]) is True


def _test_at_target_is_breach():
    # [DEC-SLA-BREACH-BOUNDARY]: p95 == 100.0 is a BREACH, not compliance.
    # Build a sample whose nearest-rank p95 is exactly 100.0.
    # n=20 -> p95 rank = 19 -> sorted[18]. Put 100.0 at index 18.
    data = [1.0] * 18 + [100.0, 100.0]  # sorted[18] = 100.0
    assert p95(data) == 100.0
    assert is_p95_compliant(data) is False, "p95 == target must be a breach"


def _test_above_target_is_breach():
    data = [1.0] * 18 + [100.1, 200.0]  # sorted[18] = 100.1
    assert p95(data) == 100.1
    assert is_p95_compliant(data) is False


def _test_just_below_target_is_compliant():
    data = [1.0] * 18 + [99.999, 100.0]  # sorted[18] = 99.999
    assert p95(data) == 99.999
    assert is_p95_compliant(data) is True


def _test_compliance_empty_raises():
    # "no data" must not read as compliant.
    try:
        is_p95_compliant([])
    except ValueError:
        pass
    else:
        raise AssertionError("is_p95_compliant([]) must raise ValueError")


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("_test_")]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"PASS {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed")


if __name__ == "__main__":
    _run_all()
