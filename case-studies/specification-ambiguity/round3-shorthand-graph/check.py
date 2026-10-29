"""Round 3 oracle: the graph-derived impl must produce nearest-rank p95."""
import importlib.util, math, pathlib
HERE = pathlib.Path(__file__).parent
s = importlib.util.spec_from_file_location("g", HERE / "impl_p95.py")
g = importlib.util.module_from_spec(s); s.loader.exec_module(g)

def nearest_rank(x):
    xs = sorted(x); return float(xs[math.ceil(0.95*len(xs))-1])

cases = [[10,20,30,40,50,60,70,80,90,100], [42.0], [1,2,3]]
print(f"{'input':<34}{'intended':>10}{'graph-derived':>16}")
allok = True
for x in cases:
    gt = nearest_rank(x); got = float(g.p95(list(x))); ok = gt == got; allok &= ok
    print(f"{str(x)[:32]:<34}{gt:>10}{got:>16}  {'ok' if ok else 'WRONG'}")
print("\nround 3:", "PASS — shorthand + corpus yielded the intended method" if allok else "FAIL")
