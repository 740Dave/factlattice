"""Boot every shape through a REAL stdio server and print what it answers ([MECH-BR-BOOT] x-load-error: the table is measured through the server's own entry point).

usage: PYTHONPATH=<tree> python -P server_driver.py shapes.json   (prints {label: observation} as JSON on stdout, `PKG <path>` on stderr)

For each shape a registry holds a healthy corpus `okc` beside ONE entry `badc` of that shape. A server is spawned with that registry (its kernel, loader and first
pass, as the product boots), sent `initialize`, and then asked three things, whose answers are the observation:
  find_okc  a named find on the healthy corpus ("ok" or the reason)
  list      lts_list_corpora(heal=False): the corpora it lists (ids), every `*_corpora` table it carries other than `corpora` and `stale_corpora`
            ({table: [(id, code)]}), whether it carries a registry error, `private_failed`, and the skipped entries of its extent
  find_badc a named find on the entry's corpus ("ok", or the reason and, for a corpus-scoped refusal, its error class)
  write_okc / write_badc  a write to a file of the healthy corpus / of the entry's folder, after the settled state: {written, corpora, flagged, partial, outside, refused}
No library function stands in for the server: a change in `server.after_first_pass`, the loader or the listing moves an observation."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

SHAPES = json.load(open(sys.argv[1]))
TREE = os.environ.get("PYTHONPATH", "").split(os.pathsep)[0]
DOC = '---\nlts: "1.1"\ntitle: t\ndomain: x.{n}\ndoctype: SPEC\n---\n\n# R [{N}-ROOT]\n\n- summary: r\n'


def _sub(v, bad, ok):
    if isinstance(v, str):
        return v.replace("{BAD}", str(bad)).replace("{OK}", str(ok))
    if isinstance(v, list):
        return [_sub(x, bad, ok) for x in v]
    if isinstance(v, dict):
        return {_sub(k, bad, ok): _sub(x, bad, ok) for k, x in v.items()}
    return v


def _verdict(reply):
    if reply is None:
        return "no-reply"
    if reply.get("success") is True:
        return "ok"
    return (str(reply.get("reason") or reply.get("error_class") or "refused") + (":" + str(reply["error_class"]) if reply.get("error_class") and reply.get("reason") else "")
            + (":" + str(reply["profile_failure"]) if reply.get("profile_failure") else ""))


def observe(label, entry):
    base = pathlib.Path(tempfile.mkdtemp(prefix="bv-"))
    ok, bad = base / "ok", base / "bad"
    for d, n in ((ok, "ok"), (bad, "bad")):
        d.mkdir()
        (d / f"{n}.lts").write_text(DOC.format(n=n, N=n.upper()), encoding="utf-8")
    top = {}
    if isinstance(entry, dict) and "$registry" in entry:
        entry = dict(entry)
        top = _sub(entry.pop("$registry", {}), bad, ok)            # a shape may add TOP-LEVEL registry keys (a `named_roots` table) beside the two corpora
    pdir = None
    if isinstance(entry, dict) and "$profile_dir" in entry:         # a shape may carry the ENGINE's own profile directory (files: name -> text), read by the child as FACTLATTICE_PROFILE_DIR
        entry = dict(entry)
        pdir = base / "pdir"
        pdir.mkdir()
        for name, text in entry.pop("$profile_dir").items():
            (pdir / name).write_text(text, encoding="utf-8")
    if isinstance(entry, dict) and "$profile" in entry:            # a shape may carry a profile BODY: written beside the registry and named by the entry's `profile` key (spec 39 shapes)
        entry = dict(entry)
        (base / "prof.json").write_text(json.dumps(entry.pop("$profile")))
        entry["profile"] = str(base / "prof.json")
    reg = {"okc": {"root": str(ok), "domain_suffix": "x.ok"}, "badc": _sub(entry, bad, ok), **top}
    (base / "reg.json").write_text(json.dumps(reg))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTLATTICE_", "LTS_"))}
    env.update(PYTHONPATH=TREE, FACTLATTICE_CORPUS_CONFIG=str(base / "reg.json"), FACTLATTICE_STATE_DIR=str(base / "st"), LTS_ENGINE_STATE_DIR=str(base / "st"),
               FACTLATTICE_CODE_WATCH="0", LTS_ROOT_DEADLINE_S="5", HOME=str(base), TMPDIR=str(base))
    if pdir is not None:
        env["FACTLATTICE_PROFILE_DIR"] = str(pdir)
    p = subprocess.Popen([sys.executable, "-P", "-m", "factlattice_engine.server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         cwd=str(base), env=env, text=True)
    n = [0]

    def rpc(method, params):
        n[0] += 1
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n[0], "method": method, "params": params}) + "\n")
        p.stdin.flush()
        while True:
            line = p.stdout.readline()
            if not line:
                return None
            o = json.loads(line)
            if o.get("id") == n[0]:
                return o

    def call(name, args):
        if os.environ.get("BOOT_VERDICT_TRACE"):
            print("CALL", name, args.get("corpus", ""), file=sys.stderr, flush=True)        # the order of the driver's calls, for the test that pins it
        o = rpc("tools/call", {"name": name, "arguments": args})
        if o is None or "result" not in o:
            return None
        return json.loads(o["result"]["content"][0]["text"])
    try:
        rpc("initialize", {})
        # a NAMED call on the entry's corpus waits for its read to end. It is taken BEFORE the settle wait because the listing cannot show a pending PRIVATE corpus
        # (it is counted inside `private_failed`), so the wait on the listing alone could end while that corpus is still being read
        find_badc = call("lts_find", {"corpus": "badc", "prefix": "BAD"})
        # the SETTLED state after the first pass: the listing must carry no pending corpus and be the same three times running, 0.3 s apart (a find on the healthy
        # corpus returns as soon as THAT corpus is read, which can be before a slower entry's read ends and the registry error is seated)
        last, same, lst = None, 0, {}
        need = int(os.environ.get("BOOT_VERDICT_SETTLE_NEED", "3"))
        rounds = 1 if os.environ.get("BOOT_VERDICT_UNSETTLE") == label else int(os.environ.get("BOOT_VERDICT_SETTLE_ROUNDS", "100"))       # the knob: one shape cannot settle
        for _ in range(rounds):
            lst = call("lts_list_corpora", {"heal": False}) or {}
            key = json.dumps([lst.get("corpora"), lst.get("failed_corpora"), lst.get("private_failed"), (lst.get("registry_choice") or {}).get("error")], sort_keys=True, default=str)
            same = same + 1 if key == last and "pending" not in key else 0
            last = key
            if same >= need:
                break
            time.sleep(0.3)
        else:
            raise RuntimeError(f"the listing never settled for shape {label!r}: the shape is NOT accepted on what was last seen")      # a never-settling shape fails ITS shape
        # the healthy corpus's find is taken AFTER the settled state, not before: before it, the find returns as soon as THAT corpus is read, which can be a moment before
        # a slower entry's read ends and registry-error mode begins (seen under load 94: `ok` where the release answers `registry-not-found`)
        find_okc = call("lts_find", {"corpus": "okc", "prefix": "OK"})
        # the WRITE side, in the settled state: a write to a file of the healthy corpus and one to a file in the entry's folder. What the receipt says of where the
        # file lives is part of the verdict: the corpora it is attributed to, whether the registry could not place it (flagged), whether a root was marked
        def write(path, node):
            out = call("lts_add_property", {"path": str(path), "node_id": node, "key": "bv", "value": "v"}) or {}
            rm = out.get("root_membership") or {}
            return {"written": out.get("written"), "corpora": rm.get("corpora"), "flagged": bool(rm.get("registry_error")), "partial": bool(rm.get("membership_partial")),
                    "outside": "OUTSIDE EVERY REGISTERED" in json.dumps(out), "refused": out.get("reason") or (out.get("evidence") or {}).get("refusal")}
        write_okc = write(ok / "ok.lts", "OK-ROOT")
        write_badc = write(bad / "bad.lts", "BAD-ROOT")
    finally:
        try:
            p.stdin.close()
            p.wait(timeout=15)
        except Exception:  # noqa: BLE001
            p.kill()
    tables = {}
    for k, v in lst.items():
        if k.endswith("corpora") and k not in ("corpora", "stale_corpora") and isinstance(v, list) and v:
            # a profile the loader refused carries its `reason` and `names` or `detail` (spec 39), so a mislabelled reason moves a row; the entry of any other failure is unchanged
            tables[k] = sorted((str(x.get("corpus_id")), str(x.get("code") or x.get("reason") or x.get("state") or ""))
                               + ((str(x["reason"]),) if x.get("code") == "corpus-load-error" and x.get("reason") else ())
                               + (("names:" + ",".join(x["names"]),) if x.get("code") == "corpus-load-error" and x.get("names") else ())
                               + (("detail:" + str(x["detail"]),) if x.get("code") == "corpus-load-error" and x.get("detail") else ())
                               if isinstance(x, dict) else (str(x), "") for x in v)
    return {"find_okc": _verdict(find_okc),
            "list": {"ids": sorted(c.get("corpus_id") for c in lst.get("corpora", [])), "tables": tables,
                     "registry_error": bool((lst.get("registry_choice") or {}).get("error")),
                     "private_failed": lst.get("private_failed"),
                     "skipped": sorted(json.dumps(x, sort_keys=True) for x in ((lst.get("extent") or {}).get("corpora_skipped") or []))},
            "find_badc": _verdict(find_badc), "write_okc": write_okc, "write_badc": write_badc}


print("PKG", os.path.dirname(__import__("factlattice_engine").__file__), file=sys.stderr)
with ThreadPoolExecutor(max_workers=6) as ex:
    futs = {label: ex.submit(observe, label, entry) for label, entry in SHAPES.items()}
    out = {}
    for label, f in futs.items():
        try:
            out[label] = f.result()
        except Exception as exc:  # noqa: BLE001 -- THIS shape fails, naming itself; every other shape still gets its observation
            out[label] = {"error": f"{type(exc).__name__}: {exc}"}
            print("SHAPE FAILED", label, out[label]["error"], file=sys.stderr)
print(json.dumps(out, sort_keys=True))
