#!/usr/bin/env python3
"""Write-then-read stress for caches keyed on a freshness signal (SDLC 7.6 [CACHE-KEYED-ON-WHAT-IT-WAS-BUILT-FROM]).

Three writer threads each append a unique token and a new node with an edge to a scratch corpus, then read the
write back through lts_find_in_prose (named and corpus-omitted), lts_reverse, lts_traverse and a structural find.
Six reader threads loop on the same tools. A miss is a read that began after the write completed and did not
see it. Exit status is 1 on any miss, on any exception in a writer or reader thread, and on any shortfall
against the expected number of reads (3 writers x n x 5), so a dead thread cannot read as a clean run.

    python3 tools/cache_freshness_stress.py --n 150 --jitter-ms 3 --seed 1
    python3 tools/cache_freshness_stress.py --pkg-root /path/to/other/checkout     # e.g. the unfixed commit, as a positive control
    python3 tools/cache_freshness_stress.py --inject-fault writer                  # self-test: must exit non-zero

LIMITS (read before quoting a number):
- In-process, nine threads: not the server's shape (one request thread and a watcher). The rate it prints is the
  rate of wrong follow-up reads by the writers in THIS run, at the load printed; it is not a server exposure rate.
  `--jitter-ms` adds a sleep after the gate passes and before the cache is reached in the reader threads; it
  raised the rate in every comparison made (about 1.5x in an independent run, about 2x in ours; the default is no sleep).
- Zero misses in a finite run is a count, not a proof. Only prose and lts_reverse have been seen to miss on the
  unfixed code here; traverse and structural find have not, so their 0 says nothing about them. The relevance
  cache and the semantic state are not exercised at all (see tests/test_cache_built_from.py).
- It reports 0 on 22884d9 and cannot see the reverse-index mix that commit had (an older build's index, key and
  sources landing in three separate assignments): that needs an interleaving inside the assignments, which a
  free-running thread run does not reach. Tests/test_cache_built_from.py pins the shape instead.
- One machine, one corpus shape (small, three corpora), the load printed at start.
"""
import argparse
import os
import random
import shutil
import sys
import tempfile
import threading
import time
import traceback

ap = argparse.ArgumentParser()
ap.add_argument("--n", type=int, default=150, help="iterations per writer")
ap.add_argument("--jitter-ms", type=float, default=0.0)
ap.add_argument("--seed", type=int, default=1)
ap.add_argument("--inject-fault", choices=("writer", "reader"), help="self-test of this harness: kill one thread; the run must then exit non-zero")
ap.add_argument("--pkg-root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
a = ap.parse_args()
sys.path.insert(0, a.pkg_root)
import factlattice_engine
from factlattice_engine import FreshnessKernel, LtsEngine
from factlattice_engine.config import build_store
from factlattice_engine.selfheal import SelfHealer
from factlattice_engine.verdict import PollVerifiedFreshness

print("package:", factlattice_engine.__file__)
print("load:", os.getloadavg(), "n/writer:", a.n, "jitter_ms:", a.jitter_ms, "seed:", a.seed)
random.seed(a.seed)
base = tempfile.mkdtemp(prefix="cache-stress-")
names = ("a", "b", "c")
corp = {}
for n in names:
    os.mkdir(os.path.join(base, n))
    with open(os.path.join(base, n, "f.lts"), "w") as fh:
        fh.write(f"---\ndomain: core.example\n---\n# T [SPEC:{n.upper()}-DOC]\n\n### [{n.upper()}1] N1\n- definition: base\n\n### [{n.upper()}2] N2\n- definition: base\n")
    corp[n] = {"root": os.path.join(base, n), "domain_suffix": "proj." + n}
store = build_store(corp)
k = FreshnessKernel(poll_interval=0.01)
k.seat_store(store)
store.kernel = k
k.start()
prov = PollVerifiedFreshness()
eng = LtsEngine(store, healer=SelfHealer(store, kernel=k, freshness_provider=prov), freshness_provider=prov)
real_gate = eng._gate
tl = threading.local()


def gate(c):
    r = real_gate(c)
    if a.jitter_ms and r[1] is None and getattr(tl, "jit", False):
        time.sleep(random.random() * a.jitter_ms / 1000)
    return r


eng._gate = gate
stop = threading.Event()


errors = []          # (thread, traceback text): any exception in a worker thread is a failure of the run


def reader(n):
    tl.jit = True
    ticks = 0
    while not stop.is_set():
        ticks += 1
        try:
            if a.inject_fault == "reader" and n == "a" and ticks == 3:
                raise RuntimeError("injected reader fault")
            eng.find_in_prose(text="base", corpus=n)
            eng.traverse(corpus=n, start_node=n.upper() + "1", direction="forward", max_depth=1)
            eng.find(corpus=n, all_of=[{"term": "base"}])
            eng.reverse(corpus=n, node_id=n.upper() + "2")
        except Exception:
            errors.append(("reader-" + n, traceback.format_exc()))


res = {}


def writer(n):
    try:
        _writer(n)
    except BaseException:
        errors.append(("writer-" + n, traceback.format_exc()))


def _writer(n):
    tl.jit = False
    path = os.path.join(base, n, "f.lts")
    miss = {"prose": 0, "prose_all": 0, "reverse": 0, "traverse": 0, "find": 0}
    reads = 0
    for i in range(a.n):
        if a.inject_fault == "writer" and n == "a" and i == a.n // 3:
            raise RuntimeError("injected writer fault")
        tok = f"tok{n}{i}x"
        with open(path, "a") as fh:
            fh.write(f"- k{i}: {tok}\n")
        for key, kw in (("prose", {"corpus": n}), ("prose_all", {})):
            r = eng.find_in_prose(text=tok, **kw)
            reads += 1
            if not (r.get("success") and r.get("count", 0) >= 1):
                miss[key] += 1
        eid = f"{n.upper()}E{i}"
        with open(path, "a") as fh:
            fh.write(f"\n### [{eid}] Gizmo{n}{i}x\n- depends-on: [{n.upper()}1]\n")
        r = eng.reverse(corpus=n, node_id=n.upper() + "1")
        reads += 1
        if not (r.get("success") and any(x["node_id"] == eid for x in r.get("referrers", []))):
            miss["reverse"] += 1
        r = eng.traverse(corpus=n, start_node=n.upper() + "1", direction="reverse", max_depth=1)
        reads += 1
        if not (r.get("success") and any(x.get("id") == eid for x in r.get("nodes", []))):
            miss["traverse"] += 1
        r = eng.find(corpus=n, all_of=[{"term": f"gizmo{n}{i}x"}])
        reads += 1
        if not (r.get("success") and r.get("count", 0) >= 1):
            miss["find"] += 1
    res[n] = (miss, reads)


rt = [threading.Thread(target=reader, args=(n,), daemon=True) for n in names for _ in range(2)]
wt = [threading.Thread(target=writer, args=(n,)) for n in names]
t0 = time.time()
for t in rt + wt:
    t.start()
for t in wt:
    t.join()
stop.set()
time.sleep(0.2)
tot, reads = {}, 0
for m, r in res.values():
    reads += r
    for key, v in m.items():
        tot[key] = tot.get(key, 0) + v
total = sum(tot.values())
expected = len(names) * a.n * 5
print(f"elapsed {time.time() - t0:.1f}s reads {reads} (expected {expected}) misses {tot} total {total} ({total / max(reads, 1):.2%})")
problems = []
if total:
    problems.append(f"{total} wrong follow-up read(s)")
if reads != expected:
    problems.append(f"read shortfall: {reads} of {expected} expected reads were made")
if len(res) != len(names):
    problems.append(f"{len(names) - len(res)} writer thread(s) did not finish")
if errors:
    problems.append(f"{len(errors)} thread error(s); first: {errors[0][0]}: {errors[0][1].strip().splitlines()[-1]}")
if hasattr(k, "stop"):
    k.stop()
if os.path.realpath(base).startswith(os.path.realpath(tempfile.gettempdir())):
    shutil.rmtree(base)
if problems:
    print("FAIL:", "; ".join(problems))
    sys.exit(1)
print("OK")
sys.exit(0)
