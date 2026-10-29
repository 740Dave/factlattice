"""A/B decider for p95: intended semantics = nearest-rank (ceil, 1-indexed).
Ground truth is computed independently; both impls are scored against it."""
import importlib.util, sys, math, pathlib
HERE = pathlib.Path(__file__).parent

def load(name, fn):
    spec = importlib.util.spec_from_file_location(name, HERE / fn)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m

def gt(x):  # nearest-rank, ceil, 1-indexed
    s = sorted(x); r = math.ceil(0.95 * len(s))
    return float(s[r-1])

CASES = {
    "discriminator [10..100]": [10,20,30,40,50,60,70,80,90,100],
    "20 evenly spaced":        [i*5.0 for i in range(1,21)],
    "single value":            [42.0],
    "small n=3":               [1.0, 2.0, 3.0],
}

if __name__ == "__main__":
    prose = load("p_prose", "impl_prose.py")
    lts = load("p_lts", "impl_lts.py")
    print(f"{'case':<26}{'ground truth':>14}{'prose':>12}{'LTS':>12}   verdict")
    print("-"*80)
    for name, x in CASES.items():
        g = gt(list(x))
        try: p = float(prose.p95(list(x)))
        except Exception as e: p = f"ERR:{e}"
        try: l = float(lts.p95(list(x)))
        except Exception as e: l = f"ERR:{e}"
        pv = "ok" if p == g else "WRONG"
        lv = "ok" if l == g else "WRONG"
        div = "  <-- DIVERGE" if p != l else ""
        print(f"{name:<26}{g:>14}{str(p):>12}{str(l):>12}   prose:{pv} LTS:{lv}{div}")
