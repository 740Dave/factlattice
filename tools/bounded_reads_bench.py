"""[FL-BOUNDED-ROOTS] [ACC-BR] c8: is the engine slower when every root is healthy, and does any reply change?

Two code trees (--old, --new: each a checkout or a `git archive` of the engine) are run as REAL server processes
(`python -m factlattice_engine.server`) over the SAME synthetic corpora, with the runs interleaved (old, new, old, new ...) so
that drift in the machine's load falls on both. The machine load (1/5/15 minute load average, CPU count) is printed beside
every figure, because a latency without its load says little.

  boot     READY time: from process start to the reply of the first CORPUS-OMITTED `lts_find`, which on a server that reads its roots in the
           background (slice 4) waits for every corpus to be read and on a server that reads them first answers at once. The `initialize`
           reply alone is NOT boot any more: it no longer waits for a root, so it would read as a huge speedup that is no speedup. It is
           reported beside, as the handshake. Then the reply of the first `lts_list_corpora`
  find     one named `lts_find` (digest tier) on a corpus, p50 and p95 over many calls, the same calls to both trees
  write    one `lts_add_property` on a healthy file, p50 and p95
  list     one `lts_list_corpora`
  --boot-study   the boot comparison done so that it can ANSWER: old and new starts are PAIRED and alternate (load drift hits both), every
           tree is compiled first, there are enough pairs that a percentile is not the maximum, and the result carries its own spread:
           a bootstrap interval for each ratio, taken from the same samples, and two controls run through the same machinery: IDENTICAL code
           (its interval is the noise floor of this machine at this load) and a PLANTED slowdown of a known size (a sleep in a temp copy;
           it must be detected). The verdict against a limit is PASS only if the whole interval is under it, FAIL only if the whole interval
           is over it, and otherwise UNDECIDABLE: a number that straddles its own noise is not printed as a pass.
  --idle   an IDLE server with the watcher on (several trees, label=path): thread count and CPU over a quiet window, so the cost of
           the machinery with nothing to do is visible beside the latencies
  --diff   the SAME read calls against two byte-identical copies of the corpora, one per tree; the replies are compared after
           the fields that legitimately differ per run (times, the workspace path) are normalised. No stall: it checks that a
           healthy engine's replies are unchanged.

WHAT IT IS NOT. The corpora are synthetic (default 48 corpora, 72 roots, ~100 nodes per file), not the deployment's; it is a
scale model of the figures the spec quotes (48 corpora and 72 roots), not a measurement of the live registry. It measures one
machine at one moment. The stall model is not involved.

    python tools/bounded_reads_bench.py --old /path/to/old --new /path/to/new [--runs 9] [--finds 300] [--diff]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from factlattice.registry import env_var_name  # noqa: E402  the sanctioned way to NAME the registry variable ([GUARD-ALLOW-LIST-AND-DISPLAY-HELPERS])


def gen(base: Path, corpora: int = 48, two_root: int = 24, files: int = 3, nodes: int = 33) -> Path:
    """Synthetic corpora under base/c/<id>[b]; returns the registry file."""
    reg: dict[str, dict] = {}
    for i in range(corpora):
        cid = f"c{i:02d}"
        roots = [base / "c" / cid] + ([base / "c" / f"{cid}b"] if i < two_root else [])
        for r_i, root in enumerate(roots):
            root.mkdir(parents=True, exist_ok=True)
            for f in range(files):
                lines = [f'---\nlts: "1.1"\ntitle: {cid} {r_i} {f}\ndomain: x.{cid}\ndoctype: SPEC\n---\n',
                         f"# Root {cid}-{r_i}-{f} [{cid.upper()}-R{r_i}{f}]\n\n- summary: root of file {f}\n"]
                for n in range(nodes):
                    nid = f"{cid.upper()}-N{r_i}{f}{n:03d}"
                    prev = f"{cid.upper()}-N{r_i}{f}{n - 1:03d}" if n else f"{cid.upper()}-R{r_i}{f}"
                    lines.append(f"\n## Node {n} of {cid} [{nid}]\n\n- status: active\n- topic: alpha bravo {n % 7}\n"
                                 f"- depends-on: [{prev}]\n\nBody text for node {n} naming alpha and charlie {n}.\n")
                (root / f"{f:02d}-doc.lts").write_text("".join(lines), encoding="utf-8")
        reg[cid] = {"roots": [{"path": str(r)} for r in roots], "domain_suffix": f"x.{cid}"}
    path = base / "registry.json"
    path.write_text(json.dumps(reg), encoding="utf-8")
    return path


#: the only tool calls a server started with `--registry` (a registry that is NOT the bench's own) may be sent: the readiness find and the listing. Anything else is refused BEFORE it is sent.
READ_ONLY_CALLS = ("lts_find", "lts_list_corpora")
READ_ONLY_GUARANTEE = ("READ-ONLY against --registry: every server gets its own temporary state dir (FACTLATTICE_STATE_DIR and LTS_ENGINE_STATE_DIR) and a scratch HOME; the only calls "
                       "sent are the readiness find (lts_find, corpus omitted) and lts_list_corpora(heal=false); any other call, a write above all, is REFUSED by this tool before it is sent.")


class ReadOnlyViolation(RuntimeError):
    """A call that --registry forbids was about to be made."""


class Srv:
    def __init__(self, code_root: Path, base: Path, registry: Path, read_only: bool = False, env_extra: dict[str, str] | None = None) -> None:
        self.code_root, self.base = code_root, base
        self.read_only = read_only
        self.last_check: dict | None = None
        home, state, tmp = base / "h", base / "s", base / "tmp"
        for d in (home, state, tmp):
            d.mkdir(parents=True, exist_ok=True)
        # a minimal child environment (no whole-environment copy): the interpreter needs PATH and little else
        e = {"PATH": os.environ.get("PATH", ""), "LANG": os.environ.get("LANG", "C.UTF-8")}
        e.update({"HOME": str(home), "FACTLATTICE_STATE_DIR": str(state), "TMPDIR": str(tmp), env_var_name(): str(registry),
                  "FACTLATTICE_CODE_WATCH": "0", "PYTHONPATH": str(code_root), "PYTHONDONTWRITEBYTECODE": "1",
                  "FACTLATTICE_LEDGER_EPHEMERAL_ROOTS": ""})
        if read_only:
            e["LTS_ENGINE_STATE_DIR"] = str(state)                      # both state variables point at the temporary dir: the registry's own state is never touched
        e.update(env_extra or {})
        self.env = e
        self.proc: subprocess.Popen | None = None
        self._id = 0

    def start_ready(self) -> tuple[float, float]:
        """(handshake, ready) in seconds from process start. READY is the reply of the first corpus-omitted find (digest tier, one term),
        which returns only when every corpus can be read: on the old tree at once, on a tree that loads in the background after the wait. The
        same call is made on both trees, so what it costs cancels in the ratio."""
        t0 = time.perf_counter()
        self.start()
        handshake = time.perf_counter() - t0
        res, _dt = self.call("lts_find", {"text": "zzz-no-such-text", "tier": "digest"})
        if not res.get("success"):
            raise RuntimeError(f"the readiness find failed: {str(res)[:200]}")
        ready = time.perf_counter() - t0
        ext = res.get("extent") or {}
        self.last_check = {"complete": res.get("complete"), "consulted": len(ext.get("corpora_consulted") or []),
                           "skipped": list(ext.get("corpora_skipped") or [])}
        return ready, handshake

    def start(self) -> float:
        t0 = time.perf_counter()
        self.proc = subprocess.Popen([sys.executable, "-m", "factlattice_engine.server"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=str(self.base), env=self.env)
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "bench", "version": "0"}})
        dt = time.perf_counter() - t0
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
        self.proc.stdin.flush()
        return dt

    def _rpc(self, method: str, params: dict) -> dict:
        self._id += 1
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}) + "\n").encode())
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed its output")
            msg = json.loads(line)
            if msg.get("id") == self._id:
                return msg

    def call(self, name: str, args: dict) -> tuple[dict, float]:
        if self.read_only and (name not in READ_ONLY_CALLS or (name == "lts_list_corpora" and args.get("heal") is not False)):
            raise ReadOnlyViolation(f"refused before it was sent: {name}({sorted(args)}) under --registry (only the readiness find and lts_list_corpora(heal=false) are allowed)")
        t0 = time.perf_counter()
        r = self._rpc("tools/call", {"name": name, "arguments": args})
        dt = time.perf_counter() - t0
        try:
            return json.loads(r["result"]["content"][0]["text"]), dt
        except (KeyError, ValueError, IndexError):
            return {"success": False, "raw": r}, dt

    def package(self) -> str:
        out = subprocess.run([sys.executable, "-c", "import factlattice,sys;sys.stdout.write(factlattice.__file__)"],
                             cwd=str(self.base), env=self.env, capture_output=True, text=True)
        return out.stdout.strip()

    def close(self) -> None:
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self.proc.kill()


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]


def summary(label: str, ms: list[float]) -> str:
    return (f"{label:<28} n={len(ms):<4} p50={pct(ms, .5):8.2f} ms  p95={pct(ms, .95):8.2f} ms  mean={statistics.mean(ms):8.2f} ms"
            f"  load1={os.getloadavg()[0]:.1f}")


def load() -> str:
    a, b, c = os.getloadavg()
    return f"load {a:.2f}/{b:.2f}/{c:.2f} on {os.cpu_count()} cpus"


# --- the replies, normalised -------------------------------------------------------------------------
VOLATILE = {"elapsed_ms", "resolved_at", "sha256_now", "sha256_at_boot", "built_at", "generated_at", "ts", "timestamp"}


def norm(obj, prefix: str):
    if isinstance(obj, dict):
        return {k: ("<volatile>" if k in VOLATILE else norm(v, prefix)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [norm(v, prefix) for v in obj]
    if isinstance(obj, str):
        return re.sub(r"\b\d+(?:\.\d+)? ms\b", "<ms>", obj.replace(prefix, "<WS>"))   # a time written into a note is volatile too
    return obj


def read_calls(corpora: list[str]) -> list[tuple[str, dict]]:
    c0, c1 = corpora[0], corpora[1]
    return [
        ("lts_list_corpora", {"heal": False}), ("lts_list_corpora", {}),
        ("lts_find", {"corpus": c0, "text": "alpha", "tier": "digest", "limit": 20}),
        ("lts_find", {"corpus": c0, "prefix": c0.upper() + "-N", "limit": 15}),
        ("lts_find", {"corpus": c1, "text": "charlie 7", "tier": "page", "token_budget": 3000}),
        ("lts_find", {"text": "bravo 3", "tier": "digest", "limit": 30}),
        ("lts_find", {"text": "bravo 3"}),
        ("lts_find", {"corpora": [c0, c1], "text": "alpha", "tier": "digest", "limit": 10}),
        ("lts_find_in_prose", {"corpus": c0, "text": "naming alpha"}),
        ("lts_find_in_prose", {"text": "charlie 12"}),
        ("lts_node", {"corpus": c0, "node_id": c0.upper() + "-N00005"}),
        ("lts_node", {"corpus": c0, "node_id": c0.upper() + "-N00005", "tier": "digest"}),
        ("lts_reverse", {"corpus": c0, "node_id": c0.upper() + "-N00004"}),
        ("lts_traverse", {"corpus": c0, "start_node": c0.upper() + "-N00010", "max_depth": 2, "tier": "digest"}),
        ("lts_capabilities", {"corpus": c0, "topic": "overview"}),
        ("lts_aggregate", {"op": "count", "corpus": c0}),
        ("lts_aggregate", {"op": "group_by", "attr": "status"}),
        ("lts_history", {"corpus": c0, "node_id": c0.upper() + "-N00005"}),
        ("lts_find", {"corpus": "no-such-corpus", "text": "x"}),
    ]


def run_diff(old: Path, new: Path, corpora: int) -> int:
    root = Path(tempfile.mkdtemp(prefix="brbench-diff-"))
    try:
        results = {}
        for label, tree in (("old", old), ("new", new)):
            base = root / label
            reg = gen(base, corpora=corpora)
            s = Srv(tree, base, reg)
            print(f"{label}: package {s.package()}")
            s.start()
            ids = sorted(json.loads(reg.read_text()))
            out = []
            for name, args in read_calls(ids):
                r, _ = s.call(name, args)
                out.append((name, args, norm(r, str(base))))
            # one write then the reads again: the verify path
            w, _ = s.call("lts_add_property", {"path": str(base / "c" / ids[0] / "00-doc.lts"), "node_id": ids[0].upper() + "-N00001",
                                                "key": "stamp", "value": "diff"})
            out.append(("write", {}, norm({k: v for k, v in w.items() if k not in ("backup_path", "ledger")}, str(base))))
            for name, args in read_calls(ids)[:6]:
                r, _ = s.call(name, args)
                out.append((name + " (after write)", args, norm(r, str(base))))
            s.close()
            results[label] = out
        bad = 0
        for (n1, a1, r1), (n2, a2, r2) in zip(results["old"], results["new"]):
            same = json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)
            print(f"{'same' if same else 'DIFF':<5} {n1} {json.dumps(a1)[:90]}")
            if not same:
                bad += 1
                a, b = json.dumps(r1, sort_keys=True), json.dumps(r2, sort_keys=True)
                i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), 0)
                print("      old:", a[max(0, i - 80):i + 120])
                print("      new:", b[max(0, i - 80):i + 120])
        print(f"{len(results['old'])} calls compared, {bad} differ")
        return 1 if bad else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _warm_bytecode(trees) -> None:
    """The servers run with PYTHONDONTWRITEBYTECODE, so a tree whose __pycache__ is cold pays 40-60 ms of compilation at every start that a warm
    one does not (measured: a 14% 'regression' that was only a cold tree). Compile every tree first so the comparison is of the code."""
    for t in trees:
        subprocess.run([sys.executable, "-m", "compileall", "-q", "-f", "factlattice", "factlattice_engine"], cwd=str(t),
                       capture_output=True, check=False)


def _cpu_s(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "cputime=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    m, _, sec = out.partition(":")
    return int(m) * 60 + float(sec)


def _threads(pid: int) -> int:
    out = subprocess.run(["ps", "-M", "-p", str(pid)], capture_output=True, text=True).stdout.splitlines()
    return max(0, len(out) - 1)


def run_idle(trees, corpora: int, seconds: float) -> int:
    root = Path(tempfile.mkdtemp(prefix="brbench-idle-"))
    try:
        _warm_bytecode([t for _l, t in trees])
        print(f"idle window {seconds:.0f} s per tree, the watcher on, no calls; {load()}")
        for label, tree in trees:
            base = root / label
            reg = gen(base, corpora=corpora)
            s = Srv(tree, base, reg)
            s.start()
            s.call("lts_list_corpora", {"heal": False})
            time.sleep(3)                                  # let the first polls and any pool settle
            t_a, c_a = _threads(s.proc.pid), _cpu_s(s.proc.pid)
            time.sleep(seconds)
            t_b, c_b = _threads(s.proc.pid), _cpu_s(s.proc.pid)
            print(f"  {label:<8} threads {t_a} -> {t_b}   cpu {100 * (c_b - c_a) / seconds:5.2f}% of one core over the window   {load()}")
            s.close()
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _pct_idx(n: int, q: float) -> int:
    return min(n - 1, int(round(q * (n - 1))))


def _stat(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[_pct_idx(len(xs), q)]


def expected_consulted(registry: Path) -> int:
    """How many corpora a corpus-omitted find should CONSULT for this registry: its public corpus entries (a private corpus is never consulted by an unnamed read)."""
    from factlattice_engine.privacy import private_reason
    reg = json.loads(registry.read_text(encoding="utf-8"))
    return sum(1 for cid, spec in reg.items() if isinstance(spec, dict) and cid != "named_roots" and not cid.endswith("_note") and private_reason(cid, spec) is None)


def expected_private(registry: Path) -> int:
    """How many PRIVATE corpus entries the registry holds: an unnamed read never consults them, and declares them in ONE by-design entry whose count is this number."""
    from factlattice_engine.privacy import private_reason
    reg = json.loads(registry.read_text(encoding="utf-8"))
    return sum(1 for cid, spec in reg.items() if isinstance(spec, dict) and cid != "named_roots" and not cid.endswith("_note") and private_reason(cid, spec) is not None)


def readiness_valid(check: dict | None, expected: int, private: int = 0) -> tuple[bool, str]:
    """A READY that did not wait must never count. Valid iff the readiness find answered `complete: true`, consulted exactly `expected` corpora, and `corpora_skipped` holds nothing
    but the private-by-design entry (the engine's own `PRIVATE_REASON`) with a count equal to the registry's private corpora. Any other skip reason (stale, pending, root-*, corpus-*,
    private-corpus-failed) voids the run."""
    from factlattice_engine.privacy import PRIVATE_REASON
    if check is None:
        return False, "no readiness check was recorded"
    skipped = check["skipped"]
    by_design = [s for s in skipped if s.get("reason") == PRIVATE_REASON]
    other = [s for s in skipped if s.get("reason") != PRIVATE_REASON]
    counted = sum(int(s.get("count", 0)) for s in by_design)
    ok = check["complete"] is True and check["consulted"] == expected and not other and len(by_design) <= 1 and counted == private
    return ok, f"complete={check['complete']} consulted {check['consulted']}/{expected} skipped={skipped[:3]} (private by design expected {private})"


def boot_pairs(old: Path, new: Path, pairs: int, tag: str, root: Path, registry: Path | None = None) -> tuple[list[float], list[float]]:
    """PAIRED boots: pair i starts one old and one new server back to back, in alternating order, each against its own copy of
    the corpora (or, with `registry`, both against that registry READ-ONLY: see READ_ONLY_GUARANTEE). Returns (old_ms, new_ms) with
    index i the same pair. Every run prints its readiness-find check; a pair with an INVALID run is dropped from both lists and counted in `boot_pairs.invalid`. A run is valid iff
    `complete` is true, exactly the registry's public corpora were consulted, and `corpora_skipped` holds nothing but the private-by-design entry with a count equal to the registry's private
    corpora (see `readiness_valid`)."""
    bases = {}
    regs = {}
    for lbl in ("old", "new"):
        bases[lbl] = root / f"{tag}-{lbl}"
        regs[lbl] = registry if registry is not None else gen(bases[lbl])
    expected = expected_consulted(regs["old"])
    private = expected_private(regs["old"])
    trees = {"old": old, "new": new}
    out = {"old": [], "new": []}
    hs = {"old": [], "new": []}
    invalid = 0
    for i in range(pairs):
        pair = {}
        for lbl in (("old", "new") if i % 2 == 0 else ("new", "old")):
            srv = Srv(trees[lbl], bases[lbl], regs[lbl], read_only=registry is not None)
            ready, handshake = srv.start_ready()
            ok, why = readiness_valid(srv.last_check, expected, private)
            print((f"    run {tag}/{i}/{lbl}: READY {ready * 1000:7.1f} ms  readiness find: {why}  valid" if ok
                   else f"    run {tag}/{i}/{lbl}: answered after {ready * 1000:7.1f} ms but NOT READY; readiness find: {why}  INVALID (not counted)"), flush=True)
            pair[lbl] = (ready * 1000, handshake * 1000, ok)
            srv.close()
        if all(v[2] for v in pair.values()):
            for lbl, (r, h, _ok) in pair.items():
                out[lbl].append(r)
                hs[lbl].append(h)
        else:
            invalid += 1
    boot_pairs.handshakes = hs                      # the handshake times of the last call, for the report (not a limit)
    boot_pairs.invalid = invalid
    return out["old"], out["new"]


class NotReady(RuntimeError):
    """A persistent server was about to be measured before its READY (every corpus read) was shown."""


def start_ready_servers(trees: dict[str, Path], bases: dict[str, Path], regs: dict[str, Path], registry: Path | None = None, say=print) -> dict[str, Srv]:
    """Start every persistent server of the find, write and list modes with `start_ready()` and refuse to hand any of them back until its readiness find shows READY: complete, every public corpus
    consulted, nothing skipped but the private-by-design entry (the boot study's rule). The release's handshake waits for every corpus; the new tree's returns at once, so a handshake-only
    start would put the warm-up and the first measured calls INSIDE the new tree's first pass, a comparison of a booting server with a booted one. Raises NotReady otherwise."""
    expected = expected_consulted(regs[next(iter(regs))])
    private = expected_private(regs[next(iter(regs))])
    out: dict[str, Srv] = {}
    for label in trees:
        s = Srv(trees[label], bases[label], regs[label], read_only=registry is not None)
        ready, _handshake = s.start_ready()
        ok, why = readiness_valid(s.last_check, expected, private)
        say(f"  {label}: persistent server READY after {ready * 1000:.0f} ms (every corpus read) before warm-up and measurement; readiness find: {why}  valid" if ok
            else f"  {label}: persistent server started after {ready * 1000:.0f} ms but NOT READY; readiness find: {why}  INVALID")
        if not ok:
            s.close()
            for o in out.values():
                o.close()
            raise NotReady(f"{label}: not READY ({why}); no measurement was taken")
        out[label] = s
    return out


def find_delta_ci(old: list[float], new: list[float], q: float = 0.95, resamples: int = 1000, seed: int = 7) -> tuple[float, float, float]:
    """(5th, 50th, 95th percentile) of the DELTA stat(new) - stat(old) over bootstrap resamples of the PAIRS (the same resampling as `boot_ratio_ci`, for a difference in milliseconds)."""
    import random
    rnd = random.Random(seed)
    n = len(old)
    rs = []
    for _ in range(resamples):
        idx = [rnd.randrange(n) for _ in range(n)]
        rs.append(_stat([new[i] for i in idx], q) - _stat([old[i] for i in idx], q))
    rs.sort()
    return rs[int(0.05 * resamples)], rs[int(0.5 * resamples)], rs[int(0.95 * resamples)]


def plant_find_delay(tree: Path, ms: float, dest: Path) -> Path:
    """A temp copy of `tree` whose named-find path (`LtsEngine._find_exact`) sleeps `ms` first: a slowdown of a known size exactly where this study measures."""
    shutil.copytree(tree, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    f = dest / "factlattice_engine" / "tools.py"
    t = f.read_text(encoding="utf-8")
    anchor = "    def _find_exact(self, corpus=None, domain=None, function=None, service=None,\n"
    assert t.count(anchor) == 1, "the named-find path was not found to plant a delay in"
    wrapper = (f"    def _find_exact(self, *a, **k):\n        import time as _plant; _plant.sleep({ms / 1000.0})\n        return self._find_exact_unplanted(*a, **k)\n\n"
               "    def _find_exact_unplanted(self, corpus=None, domain=None, function=None, service=None,\n")
    f.write_text(t.replace(anchor, wrapper), encoding="utf-8")
    return dest


def find_pairs(a_tree: Path, b_tree: Path, runs: int, finds: int, tag: str, root: Path, registry: Path | None = None) -> tuple[list[float], list[float]]:
    """PAIRED named finds, the trees interleaved call for call. Every server is started READY (`start_ready_servers`, the boot study's validity rule) before its warm-up and before any
    measured call; `NotReady` stops the study. `runs` server pairs, `finds` finds each, pooled. Returns (a_ms, b_ms) with index i the same pair."""
    a_out: list[float] = []
    b_out: list[float] = []
    for r in range(runs):
        trees = {"a": a_tree, "b": b_tree}
        bases = {k: root / f"{tag}-{k}-{r}" for k in trees}
        regs = {k: (registry if registry is not None else gen(bases[k])) for k in trees}
        ids = sorted(json.loads(regs["a"].read_text()))
        srv = start_ready_servers(trees, bases, regs, registry, say=(print if r == 0 else (lambda *_a: None)))
        try:
            for _ in range(30):
                for s in srv.values():
                    s.call("lts_find", {"corpus": ids[0], "text": "alpha", "tier": "digest", "limit": 5})        # warm
            for i in range(finds):
                cid = ids[i % 5]
                got = {}
                for label in (("a", "b") if (i + r) % 2 == 0 else ("b", "a")):
                    res, dt = srv[label].call("lts_find", {"corpus": cid, "text": "charlie 7", "tier": "digest"})
                    if not res.get("success"):
                        raise SystemExit(f"{label}: find failed: {str(res)[:200]}")
                    got[label] = dt * 1000
                a_out.append(got["a"])
                b_out.append(got["b"])
        finally:
            for s in srv.values():
                s.close()
    return a_out, b_out


def find_reading(ident_ci, plant_ci, real_ci, plant_ms: float, limit_ms: float = 2.0) -> tuple[str, list[str]]:
    """(verdict word, lines). The PLANT must be caught (its whole interval above zero and its point at least half the planted delay) or the run is UNDECIDABLE, never a pass. The REAL
    verdict follows the interval rule: PASS only if the whole interval is under the limit, FAIL only if the whole interval is over, else UNDECIDABLE."""
    out = []
    ilo, imid, ihi = ident_ci
    plo, pmid, phi = plant_ci
    rlo, rmid, rhi = real_ci
    out.append(f"  noise floor (IDENTICAL code): p95 delta {imid:+.3f} ms, 90% interval {ilo:+.3f} to {ihi:+.3f} ms")
    detected = plo > 0 and pmid >= plant_ms * 0.5
    out.append(f"  planted +{plant_ms:.2f} ms in the named-find path: p95 delta {pmid:+.3f} ms, interval {plo:+.3f} to {phi:+.3f} ms: {'DETECTED' if detected else 'NOT DETECTED'}")
    if not detected:
        out.append(f"  p95 delta of the real comparison {rmid:+.3f} ms, interval {rlo:+.3f} to {rhi:+.3f} ms")
        out.append("  named-find p95 delta limit 2 ms: UNDECIDABLE (the planted delay was not detected, so this run could not have shown a regression)")
        return "UNDECIDABLE", out
    word = verdict(rlo, rhi, limit_ms)
    out.append(f"  named-find p95 delta limit {limit_ms:g} ms (whole interval {rlo:+.3f} to {rhi:+.3f} ms): {word}")
    return word.split(" ")[0], out


def find_study(old: Path, new: Path, runs: int, finds: int, plant_find_ms: float = 1.0, limit_ms: float = 2.0, registry: Path | None = None) -> int:
    """The named-find comparison done as the boot study is: trees interleaved call for call, an IDENTICAL-code control, a PLANTED delay that must be caught, N and the load at start and end for
    each comparison, p50 and p95 for each tree, a bootstrap 90% interval for the p95 delta, and the verdict by the interval rule. Every server is READY before it is measured."""
    root = Path(tempfile.mkdtemp(prefix="brbench-findstudy-"))
    if registry is not None:
        print(READ_ONLY_GUARANTEE)
    try:
        ident = root / "identical-copy"
        shutil.copytree(old, ident, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        planted = plant_find_delay(old, plant_find_ms, root / "planted-copy")
        _warm_bytecode([old, new, ident, planted])
        print(f"find study: {runs} server pairs x {finds} named finds per comparison (trees interleaved call for call); start {load()}")
        cis = []
        for label, a, b in (("IDENTICAL code (old vs a copy of old)", old, ident), (f"PLANTED +{plant_find_ms:.2f} ms in the named-find path (old vs old with a sleep)", old, planted), ("REAL (old vs new)", old, new)):
            print(f"\n{label}:")
            o, n = find_pairs(a, b, runs, finds, f"f{len(cis)}", root, registry)
            ci = find_delta_ci(o, n, 0.95)
            cis.append(ci)
            print(f"  N={len(o)} pairs; start/end {load()}")
            print(f"  named find old: p50 {pct(o, .5):7.3f} ms  p95 {pct(o, .95):7.3f} ms   new: p50 {pct(n, .5):7.3f} ms  p95 {pct(n, .95):7.3f} ms")
            print(f"  p95 delta new-old: {ci[1]:+.3f} ms (90% interval {ci[0]:+.3f} to {ci[2]:+.3f} ms)")
        print("\nREADING")
        word, lines = find_reading(cis[0], cis[1], cis[2], plant_find_ms, limit_ms)
        for ln in lines:
            print(ln)
        return 0 if word != "FAIL" else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def boot_window_finds(new: Path, base: Path, registry: Path, finds: int = 200) -> int:
    """REPORT ONLY, no limit and no verdict: named-find latency on the NEW tree from the handshake until READY. The release cannot answer at all in that window (its handshake waits
    for every corpus), so there is no old-tree equivalent to compare with."""
    ids = sorted(json.loads(registry.read_text()))
    s = Srv(new, base, registry)
    t0 = time.perf_counter()
    s.start()
    rows = []
    for i in range(finds):
        cid = ids[0]
        t1 = time.perf_counter()
        r, dt = s.call("lts_find", {"corpus": cid, "text": "charlie 7", "tier": "digest"})
        lst, _ = s.call("lts_list_corpora", {"heal": False})
        done = all(c.get("total_nodes") is not None for c in lst.get("corpora", []))
        rows.append(((t1 - t0) * 1000, dt * 1000, done))
        if done:
            break
    s.close()
    lat = [d for _t, d, _done in rows]
    print(f"boot-window finds (new tree only; no old-tree equivalent, the release cannot answer before its boot ends; no limit, no verdict): {len(rows)} finds from handshake until every corpus was served at {rows[-1][0]:.0f} ms")
    if lat:
        print(f"  named find during the first pass: p50 {pct(lat, .5):.2f} ms  p95 {pct(lat, .95):.2f} ms  max {max(lat):.2f} ms")
    return 0


def hung_root_study(old: Path, new: Path, pairs: int, deadline_s: float = 4.0, corpora: int = 12) -> int:
    """The hung-root case: ONE root blocks (the stall harness's model of a hang) for the root deadline; the time, from process start, until a HEALTHY corpus answers a named find
    is reported per tree. The new tree must answer within about GROW_AFTER_S plus the read, not one deadline; the old tree answers only after the deadline (it reads every root
    before it answers anything)."""
    from tests.stall_harness import RealServer, Workspace  # the repository's own stall harness
    root = Path(tempfile.mkdtemp(prefix="brbench-hung-"))
    try:
        ids = [f"c{i:02d}" for i in range(corpora)]
        res = {"old": [], "new": []}
        print(f"hung-root case: {pairs} pairs, {corpora} corpora, c00's root blocks (cap {deadline_s * 3:.0f} s), root deadline {deadline_s:.1f} s; start {load()}")
        for i in range(pairs):
            for lbl in (("old", "new") if i % 2 == 0 else ("new", "old")):
                ws = Workspace(root / f"{lbl}-{i}", corpora=tuple(ids))
                tree = old if lbl == "old" else new
                t0 = time.perf_counter()
                with RealServer(ws, stall_root=ws.root(ids[0]), engaged=True, env={"LTS_ROOT_DEADLINE_S": str(deadline_s)}, code_root=tree, cap_s=deadline_s * 3, confirm_s=60.0) as srv:
                    srv.initialize(timeout=deadline_s * 4)
                    r, _dt = srv.call("lts_find", {"corpus": ids[-1], "text": "lanternfish", "tier": "digest"}, timeout=deadline_s * 4)
                    ok = bool(r.get("success")) and r.get("count", 0) >= 1
                    ms = (time.perf_counter() - t0) * 1000
                print(f"    {lbl} run {i}: healthy corpus {ids[-1]} answered {'OK' if ok else 'NOT SERVED'} at {ms:8.1f} ms from process start (root deadline {deadline_s * 1000:.0f} ms)", flush=True)
                if ok:
                    res[lbl].append(ms)
        for lbl in ("old", "new"):
            if res[lbl]:
                print(f"  {lbl}: healthy corpus served at p50 {_stat(res[lbl], .5):8.1f} ms  max {max(res[lbl]):8.1f} ms  (n={len(res[lbl])})")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def boot_ratio_ci(old: list[float], new: list[float], q: float, resamples: int = 1000, seed: int = 7) -> tuple[float, float, float]:
    """(5th, 50th, 95th percentile) of the ratio stat(new)/stat(old) over bootstrap resamples of the PAIRS."""
    import random
    rnd = random.Random(seed)
    n = len(old)
    rs = []
    for _ in range(resamples):
        idx = [rnd.randrange(n) for _ in range(n)]
        rs.append(_stat([new[i] for i in idx], q) / _stat([old[i] for i in idx], q))
    rs.sort()
    return rs[int(0.05 * resamples)], rs[int(0.5 * resamples)], rs[int(0.95 * resamples)]


def verdict(lo: float, hi: float, limit: float) -> str:
    if hi <= limit:
        return f"PASS (interval {lo:.3f}-{hi:.3f} is under {limit})"
    if lo > limit:
        return f"FAIL (interval {lo:.3f}-{hi:.3f} is over {limit})"
    return f"UNDECIDABLE at this load (interval {lo:.3f}-{hi:.3f} straddles {limit})"


def plant_slowdown(tree: Path, ms: float, dest: Path) -> Path:
    """A temp copy of `tree` whose build_tools sleeps `ms` first: a slowdown of a known size at the place boot spends its time."""
    shutil.copytree(tree, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    f = dest / "factlattice_engine" / "server.py"
    t = f.read_text(encoding="utf-8")
    anchor = "    choice = registry.resolve_registry()\n"
    assert t.count(anchor) == 1
    f.write_text(t.replace(anchor, f"    import time as _plant; _plant.sleep({ms / 1000.0})\n" + anchor), encoding="utf-8")
    return dest


#: the boot limit of spec 38 x-c8: at most 12% on boot p50, on the real start-up registry (the maintainers' ruling of 2026-11-11). A run can state its own with --boot-limit.
BOOT_LIMIT = 1.12
#: the band the IDENTICAL-code control's p50 interval must lie within for a run to decide anything (spec 38 x-c8: identical code stays within a narrow band). It is about whether the
#: instrument is quiet enough, so it is fixed and does not move with the limit: raising the limit must not let a noisier run count as decidable.
CONTROL_BAND = (0.95, 1.05)


def boot_reading(ident_row, plant_row, real_row, plant_ms: float, base_ms: float, limit: float = BOOT_LIMIT):
    """The READING of a boot study, as lines. Rows are (label, p50 interval, p95 interval), each interval (lo, mid, hi) of the ratio new/old.
    Spec 38 c8: the limit is on boot P50, met only when the WHOLE interval is under it, and only in a run where identical code stays within a narrow
    band and a planted 10% is detected; any other result is undecidable, not a pass. Boot p95 is REPORTED with its interval and carries NO verdict
    (identical code gave p95 intervals as wide as 0.066 to 1.695 on one deployment, so a limit there cannot be tested)."""
    ip50, pp50, rp50 = ident_row[1], plant_row[1], real_row[1]
    out = ["", "READING"]
    out.append(f"  noise floor (identical code, 90% interval of the p50 ratio): {ip50[0]:.3f}-{ip50[2]:.3f}")
    narrow = ip50[2] <= CONTROL_BAND[1] and ip50[0] >= CONTROL_BAND[0]
    detected = pp50[0] > ip50[2]
    out.append(f"  identical code stays within a narrow band ({CONTROL_BAND[0]:.2f}-{CONTROL_BAND[1]:.2f}, fixed, whatever the limit): {'YES' if narrow else 'NO'}")
    out.append(f"  planted +{plant_ms:.0f} ms ({100 * plant_ms / base_ms:.0f}% of the boot): {'DETECTED' if detected else 'NOT DETECTED'} "
               f"(p50 ratio interval {pp50[0]:.3f}-{pp50[2]:.3f} against the identical-code {ip50[0]:.3f}-{ip50[2]:.3f})")
    if narrow and detected:
        out.append(f"  c8 boot limit {limit} on the p50 ratio: {verdict(rp50[0], rp50[2], limit)}")
    else:
        out.append(f"  c8 boot limit {limit} on the p50 ratio: UNDECIDABLE (real p50 interval {rp50[0]:.3f}-{rp50[2]:.3f}; the controls did not hold, so it is not read as a pass)")
    r95 = real_row[2]
    out.append(f"  p95 (reported, no limit, no verdict): ratio {r95[1]:.3f} with 90% interval {r95[0]:.3f}-{r95[2]:.3f}")
    return out


def boot_study(old: Path, new: Path, pairs: int, plant_ms: float, limit: float = BOOT_LIMIT, registry: Path | None = None) -> int:
    root = Path(tempfile.mkdtemp(prefix="brbench-bootstudy-"))
    if registry is not None:
        print(READ_ONLY_GUARANTEE)
        print(f"registry: {registry} (read; {expected_consulted(registry)} public corpora expected to be consulted, {expected_private(registry)} private left out by design)")
    try:
        ident = root / "identical-copy"
        shutil.copytree(old, ident, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        planted = plant_slowdown(old, plant_ms, root / "planted-copy")
        _warm_bytecode([old, new, ident, planted])
        print(f"boot study: {pairs} paired starts per comparison, 48 corpora / 72 roots; start {load()}")
        base_ms = None
        rows = []
        for label, a, b in (("IDENTICAL code (old vs a copy of old)", old, ident),
                            (f"PLANTED +{plant_ms:.0f} ms (old vs old with a sleep)", old, planted),
                            ("REAL (old vs new)", old, new)):
            o, n = (boot_pairs(a, b, pairs, f"c{len(rows)}", root) if registry is None else boot_pairs(a, b, pairs, f"c{len(rows)}", root, registry))
            if not o:
                print(f"\n{label}: EVERY PAIR WAS INVALID (readiness find incomplete): no figure is given")
                return 2
            print(f"  invalid pairs dropped: {getattr(boot_pairs, 'invalid', 0)} of {pairs}")
            base_ms = base_ms or _stat(o, 0.5)
            r50, r95 = boot_ratio_ci(o, n, 0.5), boot_ratio_ci(o, n, 0.95)
            diffs = sorted(n[i] - o[i] for i in range(len(o)))
            rows.append((label, r50, r95))
            print(f"\n{label}: n={len(o)}  {load()}")
            print(f"  READY (first corpus-omitted find answered): old p50 {_stat(o, .5):7.1f} ms  p95 {_stat(o, .95):7.1f} ms   new p50 {_stat(n, .5):7.1f} ms  p95 {_stat(n, .95):7.1f} ms")
            hs = getattr(boot_pairs, "handshakes", None)
            if hs and hs["old"]:
                print(f"  handshake (initialize reply; NOT boot, reported only): old p50 {_stat(hs['old'], .5):7.1f} ms   new p50 {_stat(hs['new'], .5):7.1f} ms")
            print(f"  ratio p50 {r50[1]:.3f} (90% interval {r50[0]:.3f}-{r50[2]:.3f})   ratio p95 {r95[1]:.3f} (90% interval {r95[0]:.3f}-{r95[2]:.3f})")
            print(f"  paired difference new-old: median {diffs[len(diffs) // 2]:+.1f} ms, 10th-90th percentile {diffs[int(.1 * len(diffs))]:+.1f} to {diffs[int(.9 * len(diffs))]:+.1f} ms")
        ident_row, plant_row, real_row = rows
        for line in boot_reading(ident_row, plant_row, real_row, plant_ms, base_ms, limit):
            print(line)
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", type=Path)
    ap.add_argument("--new", type=Path)
    ap.add_argument("--idle", nargs="+", metavar="LABEL=PATH", help="measure an idle server for each tree and exit")
    ap.add_argument("--idle-seconds", type=float, default=30.0)
    ap.add_argument("--runs", type=int, default=9)
    ap.add_argument("--finds", type=int, default=300)
    ap.add_argument("--writes", type=int, default=40)
    ap.add_argument("--corpora", type=int, default=48)
    ap.add_argument("--diff", action="store_true")
    ap.add_argument("--boot-study", action="store_true", help="paired boot comparison with an identical-code control and a planted slowdown")
    ap.add_argument("--pairs", type=int, default=60)
    ap.add_argument("--plant-ms", type=float, default=30.0)
    ap.add_argument("--boot-limit", type=float, default=BOOT_LIMIT, help="the boot p50 ratio limit for --boot-study (default 1.12, spec 38 x-c8); the identical-code control's band is fixed (CONTROL_BAND, 0.95 to 1.05) and does not move with it")
    ap.add_argument("--registry", type=Path, help="with --boot-study or --find-study: run both trees against THIS registry, READ-ONLY (own temporary state dirs, scratch HOME, readiness find and lts_list_corpora(heal=false) only)")
    ap.add_argument("--hung-root", action="store_true", help="the hung-root boot case: one root blocks for the root deadline; time to the healthy corpora being served, per tree")
    ap.add_argument("--hung-deadline", type=float, default=4.0)
    ap.add_argument("--find-study", action="store_true", help="the named-find p95 comparison done as the boot study is: interleaved, identical-code control, planted delay, interval verdict")
    ap.add_argument("--plant-find-ms", type=float, default=1.0)
    ap.add_argument("--boot-window-finds", action="store_true", help="REPORT ONLY: named-find latency on the new tree from the handshake until READY (no old-tree equivalent, no limit, no verdict)")
    a = ap.parse_args()
    if a.registry is not None and not (a.boot_study or a.find_study):
        ap.error("--registry is for --boot-study or --find-study only (it refuses to run anything that could write)")
    if a.idle:
        return run_idle([(x.split("=", 1)[0], Path(x.split("=", 1)[1])) for x in a.idle], a.corpora, a.idle_seconds)
    if not (a.old and a.new):
        ap.error("--old and --new are required")
    if a.diff:
        return run_diff(a.old, a.new, a.corpora)
    if a.hung_root:
        return hung_root_study(a.old, a.new, a.pairs, a.hung_deadline)
    if a.find_study:
        return find_study(a.old, a.new, a.runs, a.finds, a.plant_find_ms, registry=a.registry)
    if a.boot_window_finds:
        wroot = Path(tempfile.mkdtemp(prefix="brbench-bwf-"))
        try:
            return boot_window_finds(a.new, wroot / "new", gen(wroot / "new", corpora=a.corpora))
        finally:
            shutil.rmtree(wroot, ignore_errors=True)
    if a.boot_study:
        return boot_study(a.old, a.new, a.pairs, a.plant_ms, limit=a.boot_limit, registry=a.registry)
    root = Path(tempfile.mkdtemp(prefix="brbench-"))
    try:
        trees = {"old": a.old, "new": a.new}
        _warm_bytecode(trees.values())
        # one byte-identical workspace per tree, so the writes of one never reach the other
        bases = {k: root / k for k in trees}
        regs = {k: gen(bases[k], corpora=a.corpora) for k in trees}
        ids = sorted(json.loads(regs["old"].read_text()))
        print(f"corpora {len(ids)}, roots {sum(len(v['roots']) for v in json.loads(regs['old'].read_text()).values())}; {load()}")
        for label, tree in trees.items():
            print(f"  {label}: package {Srv(tree, bases[label], regs[label]).package()}")
        boot = {k: [] for k in trees}
        hshake = {k: [] for k in trees}
        first = {k: [] for k in trees}
        finds = {k: [] for k in trees}
        lists = {k: [] for k in trees}
        writes = {k: [] for k in trees}
        loads = []
        for r in range(a.runs):
            order = ["old", "new"] if r % 2 == 0 else ["new", "old"]
            for label in order:
                s = Srv(trees[label], bases[label], regs[label])
                ready, handshake = s.start_ready()
                boot[label].append(ready * 1000)
                hshake[label].append(handshake * 1000)
                _res, dt = s.call("lts_list_corpora", {"heal": False})
                first[label].append(dt * 1000)
                s.close()
            loads.append(os.getloadavg()[0])
        print(summary("boot -> READY (old)", boot["old"]))
        print(summary("boot -> READY (new)", boot["new"]))
        print(summary("handshake only (old)", hshake["old"]))
        print(summary("handshake only (new)", hshake["new"]))
        print(summary("first list_corpora (old)", first["old"]))
        print(summary("first list_corpora (new)", first["new"]))
        print(f"  during boot runs: load1 min {min(loads):.2f} max {max(loads):.2f}; {load()}")
        # persistent servers, calls interleaved one for one. Each is started READY (every corpus read): see `start_ready_servers`
        print("persistent servers for the find, write and list modes are started with start_ready() and measured only after their readiness find is valid:")
        srv = start_ready_servers(trees, bases, regs)
        for _ in range(30):
            for s in srv.values():
                s.call("lts_find", {"corpus": ids[0], "text": "alpha", "tier": "digest", "limit": 5})     # warm
        for i in range(a.finds):
            cid = ids[i % 5]
            for label in (("old", "new") if i % 2 == 0 else ("new", "old")):
                r, dt = srv[label].call("lts_find", {"corpus": cid, "text": "charlie 7", "tier": "digest"})
                if not r.get("success"):
                    raise SystemExit(f"{label}: find failed: {r}")
                finds[label].append(dt * 1000)
        for i in range(30):
            for label in (("old", "new") if i % 2 == 0 else ("new", "old")):
                _r, dt = srv[label].call("lts_list_corpora", {"heal": False})
                lists[label].append(dt * 1000)
        for i in range(a.writes):
            for label in (("old", "new") if i % 2 == 0 else ("new", "old")):
                r, dt = srv[label].call("lts_add_property", {"path": str(bases[label] / "c" / ids[0] / "00-doc.lts"),
                                                              "node_id": ids[0].upper() + "-N00002", "key": f"k{i}", "value": "v"})
                if not r.get("success"):
                    raise SystemExit(f"{label}: write failed: {r}")
                writes[label].append(dt * 1000)
        for s in srv.values():
            s.close()
        for label in ("old", "new"):
            print(summary(f"named find ({label})", finds[label]))
        for label in ("old", "new"):
            print(summary(f"list_corpora heal=false ({label})", lists[label]))
        for label in ("old", "new"):
            print(summary(f"write to a healthy file ({label})", writes[label]))
        print(f"  delta p50 find new-old = {pct(finds['new'], .5) - pct(finds['old'], .5):+.2f} ms, "
              f"p95 = {pct(finds['new'], .95) - pct(finds['old'], .95):+.2f} ms (limit 2 ms)")
        print("  boot above is UNPAIRED and carries no spread: it cannot answer the boot limit. Use --boot-study for the boot comparison.")
        print(f"  {load()}")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
