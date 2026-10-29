"""Per-user rate limiter.

Spec: each user may make up to 5 requests every 10 seconds. Requests over
that are rejected until the user is back under the limit.

Public API:
    allow_request(user_id: str, now: float) -> bool

ASSUMPTIONS
-----------
1. Sliding window, not fixed window. "5 requests every 10 seconds" is
   interpreted as a rolling window: a request at time `now` is allowed iff
   fewer than 5 requests have been recorded in the half-open interval
   (now - 10, now]. This avoids the burst-at-boundary problem of fixed
   windows (which would allow up to 10 requests across a 2ms span).
2. Window is exactly 10.0 seconds and the limit is exactly 5 requests.
   These are hardcoded constants (LIMIT, WINDOW) at module level.
3. The window boundary is exclusive at the old end: a request with
   timestamp t counts against `now` only if now - t < WINDOW (strictly).
   So a request made exactly 10.0s after an earlier one no longer "sees"
   that earlier one.
4. Only *allowed* requests consume quota. A rejected request does not count
   toward the limit and does not extend/refresh the window. This is the
   standard rate-limiter semantics (rejected traffic is free to retry).
5. `now` is assumed non-decreasing per user in normal operation, but the
   implementation does not require it: timestamps are stored and filtered
   by absolute difference-from-now, so out-of-order or repeated `now`
   values still produce a well-defined answer. Clock going backwards will
   simply prune less aggressively; it will not crash.
6. `user_id` is any hashable string; distinct strings are distinct users.
   State is unbounded in the number of users seen (no eviction of idle
   users) — acceptable for the spec's scope. Per user we keep at most
   LIMIT timestamps, so per-user memory is O(1).
7. Thread safety: a module-level lock guards all state mutation so the
   callable is safe under concurrent callers.
8. Module-level singleton state: importing the module and calling
   allow_request() "just works" without constructing anything, per the
   required signature.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from typing import Deque, Dict

LIMIT: int = 5
WINDOW: float = 10.0


class RateLimiter:
    """Sliding-window per-user rate limiter."""

    def __init__(self, limit: int = LIMIT, window: float = WINDOW) -> None:
        self.limit = limit
        self.window = window
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow_request(self, user_id: str, now: float) -> bool:
        with self._lock:
            hits = self._hits[user_id]
            cutoff = now - self.window
            # Drop timestamps that have aged out of the window.
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) < self.limit:
                hits.append(now)
                return True
            return False

    def reset(self, user_id: str | None = None) -> None:
        """Clear state — for a single user, or all users if user_id is None."""
        with self._lock:
            if user_id is None:
                self._hits.clear()
            else:
                self._hits.pop(user_id, None)


# Module-level singleton so the required callable works on import.
_limiter = RateLimiter()


def allow_request(user_id: str, now: float) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    return _limiter.allow_request(user_id, now)


def reset(user_id: str | None = None) -> None:
    """Reset the module-level limiter state."""
    _limiter.reset(user_id)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
def _run_tests() -> None:
    def fresh() -> RateLimiter:
        return RateLimiter()

    # 1. Up to LIMIT requests at the same instant are allowed.
    rl = fresh()
    for i in range(LIMIT):
        assert rl.allow_request("u", 100.0) is True, f"req {i} should pass"
    assert rl.allow_request("u", 100.0) is False, "6th at same time rejected"

    # 2. Rejected requests do not consume quota: still rejected while full.
    assert rl.allow_request("u", 100.0) is False
    assert rl.allow_request("u", 100.0) is False

    # 3. After the window fully passes, quota is restored.
    #    First hit was at t=100; at t=110 it is exactly WINDOW old and pruned
    #    (cutoff = 110 - 10 = 100, and hits[0]=100 <= 100 -> dropped).
    assert rl.allow_request("u", 110.0) is True, "window elapsed -> allowed"

    # 4. Sliding behavior: spread 5 across the window, 6th blocked, then
    #    as the oldest slides out one slot frees up.
    rl = fresh()
    for t in (0.0, 1.0, 2.0, 3.0, 4.0):
        assert rl.allow_request("s", t) is True
    assert rl.allow_request("s", 4.5) is False, "5 in window -> reject"
    # At t=10.0, the t=0.0 hit is exactly WINDOW old -> pruned; room for one.
    assert rl.allow_request("s", 10.0) is True
    # But immediately after, we're full again (hits at 1,2,3,4,10).
    assert rl.allow_request("s", 10.0) is False

    # 5. Per-user isolation: one user's saturation doesn't affect another.
    rl = fresh()
    for _ in range(LIMIT):
        assert rl.allow_request("a", 50.0) is True
    assert rl.allow_request("a", 50.0) is False
    assert rl.allow_request("b", 50.0) is True, "user b independent"

    # 6. Exact boundary: a hit exactly WINDOW seconds later sees the old one
    #    as expired. Fill at t=0, then at t=WINDOW the first is pruned.
    rl = fresh()
    for _ in range(LIMIT):
        assert rl.allow_request("edge", 0.0) is True
    assert rl.allow_request("edge", WINDOW) is True, "t=WINDOW prunes t=0"
    # Just before WINDOW, still blocked.
    rl = fresh()
    for _ in range(LIMIT):
        assert rl.allow_request("edge2", 0.0) is True
    assert rl.allow_request("edge2", WINDOW - 0.001) is False

    # 7. Per-user memory stays bounded at LIMIT timestamps.
    rl = fresh()
    for t in range(1000):
        rl.allow_request("mem", float(t))
    assert len(rl._hits["mem"]) <= LIMIT, "bounded per-user state"

    # 8. Module-level singleton callable works and reset clears it.
    reset()
    for _ in range(LIMIT):
        assert allow_request("mod", 5.0) is True
    assert allow_request("mod", 5.0) is False
    reset("mod")
    assert allow_request("mod", 5.0) is True, "reset restored quota"

    # 9. Thread safety smoke test: many threads hammering one user must
    #    never allow more than LIMIT within a single instantaneous window.
    import threading as _t

    rl = fresh()
    results = []
    rlock = _t.Lock()

    def worker() -> None:
        ok = rl.allow_request("race", 0.0)
        with rlock:
            results.append(ok)

    threads = [_t.Thread(target=worker) for _ in range(200)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sum(results) == LIMIT, f"exactly {LIMIT} allowed, got {sum(results)}"

    print("all tests passed")


if __name__ == "__main__":
    _run_tests()
