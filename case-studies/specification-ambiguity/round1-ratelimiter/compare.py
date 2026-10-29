"""Neutral A/B decider: run both implementations against identical timelines,
show where they diverge, and score each against the INTENDED sliding-window semantics.

Ground truth = the [SPEC:RATE-LIMITER] semantics: admit iff #admitted in (now-10, now] < 5,
only admitted requests count, half-open lower bound.
"""
import importlib.util, sys, pathlib

HERE = pathlib.Path(__file__).parent

def load(modname, filename):
    spec = importlib.util.spec_from_file_location(modname, HERE / filename)
    m = importlib.util.module_from_spec(spec)
    sys.modules[modname] = m
    spec.loader.exec_module(m)
    return m

def reset(mod):
    # each impl may expose reset() differently; try the common shapes, else reimport
    for name in ("reset", "_reset", "clear"):
        fn = getattr(mod, name, None)
        if callable(fn):
            try:
                fn(); return
            except TypeError:
                pass
    # fall back: reload the module fresh
    return None

def ground_truth(calls):
    """The intended sliding-window semantics, computed independently."""
    admitted = {}
    out = []
    for uid, now in calls:
        keep = [t for t in admitted.get(uid, []) if now - t < 10.0]  # (now-10, now]
        ok = len(keep) < 5
        if ok:
            keep.append(now)
        admitted[uid] = keep
        out.append(ok)
    return out

def run(mod, calls):
    reset(mod)
    return [bool(mod.allow_request(uid, now)) for uid, now in calls]

# --- the timelines --------------------------------------------------------
TIMELINES = {
    "straddling-burst (the decider)": [("u", t) for t in (5,6,7,8,9,10)],
    "age-out re-opens":              [("u", t) for t in (5,6,7,8,9)] + [("u", 15.0001)],
    "exact-boundary 10.0":           [("u", t) for t in (0,1,2,3,4)] + [("u", 10.0)],
    "at-limit sixth same instant":   [("u", 0.0)]*6,
    "per-user isolation":            [("a",0),("a",0),("a",0),("a",0),("a",0),("b",0),("a",0)],
}

def fmt(bools):
    return " ".join("Y" if b else "." for b in bools)

if __name__ == "__main__":
    prose = load("impl_prose", "impl_prose.py")
    lts = load("impl_lts", "impl_lts.py")
    print(f"{'timeline':<34} {'ground truth':<16} {'prose':<16} {'LTS':<16} verdict")
    print("-" * 96)
    for name, calls in TIMELINES.items():
        gt = ground_truth(calls)
        pr = run(prose, calls)
        lt = run(lts, calls)
        pr_ok = "ok" if pr == gt else "WRONG"
        lt_ok = "ok" if lt == gt else "WRONG"
        diverge = "" if pr == lt else "  <-- DIVERGE"
        print(f"{name:<34} {fmt(gt):<16} {fmt(pr)} [{pr_ok:<5}] {fmt(lt)} [{lt_ok:<5}]{diverge}")
