"""p95 latency metric for the SLA dashboard.

Implements [SPEC:P95], consistent with the team's percentile conventions:

  - [DEC-PERCENTILE-METHOD]  All latency percentiles use nearest-rank:
        sort ascending; rank r = ceil(p/100 * n), 1-indexed; return sorted[r-1].
        Interpolation between ranks is NOT used.
  - [INV-OBSERVED-METRIC]    The returned value is always an element of the
        input sample -- never an average, interpolation, or synthesized value.
  - [DEC-EMPTY-RAISES]       An empty sample raises ValueError; never a
        fabricated 0.

This mirrors the in-service p99 and p50 exactly, changing only p = 95.
"""

from math import ceil


def p95(latencies: list[float]) -> float:
    """Return the 95th-percentile latency by the nearest-rank method.

    Nearest-rank per [DEC-PERCENTILE-METHOD]: sort ascending, take
    rank r = ceil(0.95 * n) (1-indexed), return sorted[r-1]. The result is
    always a value that some request actually experienced ([INV-OBSERVED-METRIC]).

    Raises ValueError on an empty sample ([DEC-EMPTY-RAISES]).
    """
    n = len(latencies)
    if n == 0:
        raise ValueError("p95 over an empty sample is undefined (no data)")
    ordered = sorted(latencies)
    r = ceil(0.95 * n)  # 1-indexed rank
    return ordered[r - 1]


# --- tests ---------------------------------------------------------------

def _test_empty_raises():
    try:
        p95([])
    except ValueError:
        return
    raise AssertionError("empty sample must raise ValueError [DEC-EMPTY-RAISES]")


def _test_spec_worked_example_n200():
    # [SPEC:P95] worked-example: n=200 -> r=190 -> sorted[189]
    data = list(range(1, 201))  # 1..200, already sorted
    assert p95(data) == 190, p95(data)


def _test_spec_worked_example_n20():
    # [SPEC:P95] worked-example: n=20 -> r=19 -> sorted[18]
    data = list(range(1, 21))  # 1..20
    assert p95(data) == 19, p95(data)


def _test_single_element():
    # n=1 -> r=ceil(0.95)=1 -> sorted[0]
    assert p95([42.0]) == 42.0


def _test_sorts_input():
    # Method sorts ascending; input order must not matter.
    data = [200, 5, 190, 1, 100]  # n=5, r=ceil(4.75)=5 -> max
    assert p95(data) == 200


def _test_result_is_observed_member():
    # [INV-OBSERVED-METRIC]: result is always an element of the input.
    import random
    for _ in range(1000):
        n = random.randint(1, 300)
        data = [random.random() * 1000 for _ in range(n)]
        result = p95(data)
        assert result in data
        # And it equals sorted[ceil(0.95*n)-1] exactly.
        assert result == sorted(data)[ceil(0.95 * n) - 1]

    # never interpolates: value is drawn from the sample, not between two.
    data = [10.0, 20.0]  # n=2, r=ceil(1.9)=2 -> sorted[1] = 20.0 (not 15/19)
    assert p95(data) == 20.0


def _test_does_not_mutate_input():
    data = [3.0, 1.0, 2.0]
    snapshot = list(data)
    p95(data)
    assert data == snapshot


def _test_duplicates():
    data = [5.0] * 10 + [9.0] * 10  # n=20, r=19 -> sorted[18]=9.0
    assert p95(data) == 9.0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("_test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\nAll {len(tests)} tests passed.")
