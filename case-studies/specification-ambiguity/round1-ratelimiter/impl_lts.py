"""Per-user sliding-window rate limiter [SPEC:RATE-LIMITER].

A request for a given user at time `now` is ADMITTED iff the number of
previously-admitted requests for that same user with timestamp in the
half-open interval (now - 10.0, now] is strictly less than 5. Only admitted
requests consume capacity; rejected requests are not recorded.

Public callable:
    allow_request(user_id: str, now: float) -> bool

ASSUMPTIONS
-----------
1. WINDOW = 10.0 seconds and LIMIT = 5 admitted requests are taken verbatim
   from the spec ([DD-SLIDING-WINDOW], [DD-STRICT-LESS-THAN]) and are the
   only rate parameters.
2. `now` is monotonic non-decreasing across calls, exactly as [DM-DECISION]
   guarantees. We therefore prune each user's stored timestamps to the open
   lower bound (now - 10.0) on every call; we do NOT defend against a caller
   that violates monotonicity (out-of-order `now` is out of contract). The
   decision itself only ever counts timestamps in (now - 10.0, now], so even
   if pruning were skipped the returned booleans would be identical.
3. State is process-local, in-memory, and NOT thread-safe. The spec's boundary
   explicitly excludes storage durability and distribution across processes.
   A single module-level singleton holds all per-user state, so the exact
   `allow_request(user_id, now)` callable works on import. A module-level
   `reset()` is provided so tests (and callers) can clear state; it is not
   required by the spec.
4. `user_id` is used as an opaque dict key; any hashable string works and
   distinct strings are independent users ([DD-PER-USER]). No normalization
   (trimming/casing) is applied — "u" and "U " are different users.
5. Floating-point comparison is used directly (`ts > now - WINDOW`), matching
   the exact real-number semantics the spec describes. No epsilon fuzzing is
   added; the acceptance cases ([TEST-BOUNDARY-BURST], [TEST-AGE-OUT]) are
   chosen to avoid representational ambiguity.
"""

from collections import deque

WINDOW = 10.0
LIMIT = 5


class _RateLimiter:
    """Holds per-user deques of admitted-request timestamps (ascending)."""

    def __init__(self):
        self._by_user = {}

    def allow_request(self, user_id: str, now: float) -> bool:
        dq = self._by_user.get(user_id)
        if dq is None:
            dq = deque()
            self._by_user[user_id] = dq

        # Age out anything at or before the exclusive lower bound.
        # Interval is (now - WINDOW, now]; a timestamp == now - WINDOW is OUT.
        lower = now - WINDOW
        while dq and dq[0] <= lower:
            dq.popleft()

        # Every remaining timestamp is in (now - WINDOW, now] because `now`
        # is monotonic non-decreasing, so len(dq) is the window count.
        if len(dq) < LIMIT:
            dq.append(now)
            return True
        return False

    def reset(self):
        self._by_user.clear()


# Module-level singleton so the exact callable signature works on import.
_limiter = _RateLimiter()


def allow_request(user_id: str, now: float) -> bool:
    """Return True if the request is admitted, False if rejected."""
    return _limiter.allow_request(user_id, now)


def reset() -> None:
    """Clear all rate-limiter state (test/utility helper; not in spec)."""
    _limiter.reset()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def _run_tests():
    import math

    def fresh():
        reset()

    # [DD-STRICT-LESS-THAN] empty window admits 1..5, rejects 6th at same-ish time.
    fresh()
    for i in range(5):
        assert allow_request("u", 100.0 + i * 0.001) is True, f"req {i} should admit"
    assert allow_request("u", 100.006) is False, "6th within window must reject"

    # [TEST-BOUNDARY-BURST] straddling burst separates sliding from fixed window.
    fresh()
    for t in (5, 6, 7, 8, 9):
        assert allow_request("u", float(t)) is True, f"admit at {t}"
    # (0.0, 10.0] still holds all five of 5,6,7,8,9 -> count 5, not < 5 -> reject.
    assert allow_request("u", 10.0) is False, "boundary 6th must be REJECTED (sliding)"

    # [TEST-AGE-OUT] capacity re-opens once the oldest ages out.
    fresh()
    for t in (5, 6, 7, 8, 9):
        assert allow_request("u", float(t)) is True
    # (5.0001, 15.0001] excludes the 5 -> count 4 -> admit.
    assert allow_request("u", 15.0001) is True, "age-out should admit"

    # [DD-HALF-OPEN] lower bound exclusive: a request at exactly now-10 aged out.
    fresh()
    assert allow_request("u", 0.0) is True      # admitted at t=0
    for t in (1, 2, 3, 4):
        assert allow_request("u", float(t)) is True
    # window now holds {0,1,2,3,4}. At now=10.0, lower bound = 0.0 (exclusive):
    # the t=0 request ages out -> count 4 -> admit.
    assert allow_request("u", 10.0) is True, "t=now-10 is excluded (half-open)"
    # And now {1,2,3,4,10} => 5 in (0,10] ... check a 6th at 10.0 rejects.
    assert allow_request("u", 10.0) is False, "now window full again"

    # [DD-ADMITTED-ONLY] rejected requests do not consume capacity / extend lockout.
    fresh()
    for t in (0, 1, 2, 3, 4):
        assert allow_request("u", float(t)) is True
    # Fire a burst of rejects at t=5..9 (window (t-10,t] still has all 5 originals).
    for t in (5, 6, 7, 8, 9):
        assert allow_request("u", float(t)) is False, f"reject at {t}"
    # At t=10.5 the interval (0.5, 10.5] excludes t=0 admit; the rejects were
    # never recorded, so only {1,2,3,4} count -> admit. If rejects had counted,
    # this would still reject.
    assert allow_request("u", 10.5) is True, "rejects must not extend lockout"

    # [DD-PER-USER] users are independent.
    fresh()
    for _ in range(5):
        assert allow_request("a", 1.0) is True
    assert allow_request("a", 1.0) is False
    # user b is unaffected.
    for _ in range(5):
        assert allow_request("b", 1.0) is True
    assert allow_request("b", 1.0) is False

    # [INV-ROLLING-RATE] brute-force invariant check over a long sequence:
    # no 10s span (t-10, t] ever holds > 5 admitted requests.
    fresh()
    admitted_times = []
    now = 0.0
    for _ in range(2000):
        now += 0.37  # steady stream faster than the rate allows
        if allow_request("u", now):
            admitted_times.append(now)
    for t in admitted_times:
        count = sum(1 for ts in admitted_times if t - WINDOW < ts <= t)
        assert count <= LIMIT, f"rolling invariant violated: {count} in (t-10,t] at {t}"

    # [INV-DETERMINISTIC] same input sequence -> identical decisions.
    seq = [("u", 0.0), ("u", 1.0), ("u", 2.0), ("u", 3.0), ("u", 4.0),
           ("u", 5.0), ("v", 5.0), ("u", 11.0), ("u", 11.0), ("u", 11.0)]

    def run(sequence):
        reset()
        return [allow_request(uid, t) for uid, t in sequence]

    r1 = run(seq)
    r2 = run(seq)
    assert r1 == r2, "decisions must be deterministic"

    # sanity: math import used, avoid unused warning
    assert not math.isnan(WINDOW)

    print("All tests passed.")
    print(f"  deterministic sequence result: {r1}")
    print(f"  admitted in rolling-invariant stress run: {len(admitted_times)}")


if __name__ == "__main__":
    _run_tests()
