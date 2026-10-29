"""Telephone-game step 2: does the SLA compliance verdict flip between branches?
Ground truth = nearest-rank p95, breach iff p95 >= 100 (compliant iff p95 < 100),
per corpus [DEC-PERCENTILE-METHOD] + [DEC-SLA-BREACH-BOUNDARY]."""
import importlib.util, sys, math, pathlib
HERE = pathlib.Path(__file__).parent

def load(name, fn):
    s = importlib.util.spec_from_file_location(name, HERE / fn)
    m = importlib.util.module_from_spec(s); sys.modules[name] = m
    s.loader.exec_module(m); return m

def gt(x):
    s = sorted(x); r = math.ceil(0.95*len(s)); p95 = float(s[r-1])
    return p95 < 100.0  # compliant iff strictly below target

CASES = {
    "[10..100] (the flip case)": [10,20,30,40,50,60,70,80,90,100],
    "all low  [1..10]":          [1,2,3,4,5,6,7,8,9,10],
    "all high [110..200]":       list(range(110,201,10)),
}

def verdict(b):  return "COMPLIANT (green)" if b else "BREACH (red)"

if __name__ == "__main__":
    ng = load("ng", "impl_nograph.py")
    gr = load("gr", "impl_graph.py")
    print(f"{'case':<28}{'ground truth':<20}{'no-graph':<20}{'graph':<20}")
    print("-"*88)
    for name, x in CASES.items():
        g = gt(list(x))
        try: n = bool(ng.is_p95_compliant(list(x)))
        except Exception as e: n = f"ERR:{e}"
        try: r = bool(gr.is_p95_compliant(list(x)))
        except Exception as e: r = f"ERR:{e}"
        flip = "   <-- VERDICT FLIP" if (n != g and isinstance(n,bool)) else ""
        nv = verdict(n) if isinstance(n,bool) else n
        rv = verdict(r) if isinstance(r,bool) else r
        print(f"{name:<28}{verdict(g):<20}{nv:<20}{rv:<20}{flip}")
