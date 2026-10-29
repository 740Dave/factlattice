# Try it before you deploy

You have downloaded FactLattice and want to know it works on your machine **before** you wire its
MCP server into anything. This page takes about ten minutes. You will run the engine's own
self-check, lint one shipped example file, see what a failure looks like, and learn how to tell
which registry and build you are talking to. No MCP connection and no configuration file; the
one third-party package, pydantic, is installed with the engine (`pip install .`).

Everything below goes through one command, `python3 -m factlattice_engine call <tool>`. It runs
the tool through the same dispatch the MCP server uses, so what you see here is what an agent
would see through the server, for the same inputs, registry and build.

## What you need

- Python 3.9 or newer, the project's declared minimum (`python3 --version`; this page was run on 3.13).
- This repository, checked out. Run every command from its top directory (the one holding
  `SPEC.md` and `factlattice_engine/`).
- **No corpus registry configured**, which is the normal state of a fresh download: the variable
  `FACTLATTICE_CORPUS_CONFIG` is unset and `~/.config/factlattice/config.json` does not exist. This page
  runs against the built-in *example* registry and says so in every banner. (If you have configured a
  registry, every command below names it instead, which is the point of step 5; to repeat the page
  exactly, unset the variable and set `XDG_CONFIG_HOME` to an empty scratch directory.)
- A scratch state directory, so the trial leaves nothing in your real one (the engine keeps a
  query log; writes keep a ledger and backups):

```
export FACTLATTICE_STATE_DIR="$(mktemp -d)"
export FACTLATTICE_BACKUP_DIR="$(mktemp -d)"
export T="$(mktemp -d)"        # scratch space for the files this page writes
```

`call` is **read-only by default**. A tool that would change something (the typed writers, the
corpus registry, the miss log) is refused unless you add `--allow-write`. Nothing on this page
needs it.

## 1. See what `call` covers

```
python3 -m factlattice_engine call --list
```

It prints the read tools (run by default) and the write tools (refused without `--allow-write`).
Every tool the MCP server serves is in one of the two lists.

## 2. Run the engine's self-check

```
python3 -m factlattice_engine call lts_selfcheck > "$T/selfcheck.json"; echo "exit status: $?"
grep -E '"(success|verdict|digest_ok|case_count|passed|failed)"' "$T/selfcheck.json"
```

The command prints a short statement of what it ran against on **stderr** and the tool's result
on **stdout** (so the result pipes cleanly). Expected, captured from a real run:

```
call lts_selfcheck
  registry : built-in example registry (no registry configured: FACTLATTICE_CORPUS_CONFIG is not set and ~/.config/factlattice/config.json does not exist)
  corpora  : 1 loaded (3 registry entries)
  package  : /path/to/your/checkout/factlattice_engine
  core     : /path/to/your/checkout/factlattice
  build    : git 6e74ff69fd67; source sha256:f1a11092b50e
  query log: /tmp/.../query-log.jsonl
exit status: 0
  "success": true,
  "verdict": "PASS",
  "digest_ok": true,
  "case_count": 58,
  "passed": 58,
  "failed": 0,
```

What it means: the engine just re-parsed its own control corpus and checked its output against
hand-written ground truth. `PASS` with `failed: 0` and `digest_ok: true` says this build's parser
behaves as the shipped control expects. Your `git` hash, `source sha256` and paths will differ,
and `(dirty)` (not shown above) appears only if your checkout has uncommitted changes. The case count grows as new
classes of silent loss are added to the control.

⛔ **A green self-check is a regression gate, not a proof of correctness.** It measures the
failure classes the project has already named; it cannot show there is no unnamed one.

## 3. Lint one shipped example file

```
python3 -m factlattice_engine call lts_lint \
    --args-json '{"source": "specs/40-domain-profiles.lts"}' > "$T/lint.json"; echo "exit status: $?"
python3 - <<'PY'
import json, os
r = json.load(open(os.environ["T"] + "/lint.json"))
print({k: r[k] for k in ("node_count", "edge_count")})
print({k: v for k, v in r["summary"].items() if k in ("total", "errors_observed", "warnings_observed")})
print(sorted({(f["origin"] + "." + f["kind"], f["severity"]) for f in r["findings"]}))
PY
```

Expected:

```
exit status: 0
{'node_count': 7, 'edge_count': 21}
{'total': 7, 'errors_observed': 0, 'warnings_observed': 7}
[('structure.dangling', 'warning'), ('vocabulary.legacy_document', 'warning'), ('vocabulary.undeclared_key', 'warning')]
```

It parsed seven nodes and twenty-one edges. Exit status 0 means **no error-severity finding**.
The seven warnings are expected for a spec linted by itself, with no domain profile in scope: three
references point at nodes defined in sibling files (`dangling`), the file carries no format-version
marker (`legacy_document`), and three keys are not declared by any profile (`undeclared_key`).

Read the `profile` block in the full output before trusting any lint verdict. Here it says
`"available": false`, and the result says in so many words that the profile-driven rules did not
run. A clean lint with no profile in scope means *nothing was checked against a vocabulary*.

## 4. See what a failure looks like

Make a two-node file that defines the same id twice, and lint it:

```
printf '# Demo [DEMO-ROOT]\n\n## First [DEMO-1]\n\n- status: active\n\n## Second [DEMO-1]\n\n- status: active\n' > "$T/broken.lts"
python3 -m factlattice_engine call lts_lint --args-json "{\"source\": \"$T/broken.lts\"}" --json > "$T/broken.json"
echo "exit status: $?"
python3 -c 'import json, os; e = json.load(open(os.environ["T"] + "/broken.json")); print(e["success"], e["exit_code"], e["error"])'
```

Expected:

```
exit status: 1
False 1 lint found error-severity findings (see result.summary) (exit_code=1)
```

An errored call never exits 0. The exit statuses are:

| exit | meaning |
|---|---|
| 0 | the tool ran and succeeded (a self-check says `PASS`; a lint has no error-severity finding) |
| 1 | the tool errored, a self-check did not `PASS`, or a lint found errors |
| 2 | usage: unknown tool, malformed `--args-json`, unreadable `--args-file`, or the engine could not be imported or built |
| 3 | a write tool was refused because `--allow-write` was not given |

Try the other two quickly:

```
python3 -m factlattice_engine call lts_nope;  echo "exit status: $?"
python3 -m factlattice_engine call lts_add_property --args-json '{"path":"x","node_id":"A","key":"k","value":"v"}'; echo "exit status: $?"
```

```
ERROR: unknown tool 'lts_nope'. Covered tools: lts_find, lts_semantic_find, ...
exit status: 2
REFUSED: lts_add_property is a WRITE tool (or is not on the engine's read-only allow-list) and `call` is read-only by default. Nothing was run. Pass --allow-write to run it for real.
exit status: 3
```

## 5. Know which registry and build you are talking to

Every call states its configuration, because an in-process run uses *this checkout's* code and
*this environment's* corpus registry, and those can differ from a long-running server's. With
`--json` the same statement is the `call_context` block of the output envelope
(`{success, refused, exit_code, tool, call_context, result}`):

- `corpus_config`: where the registry came from (`source`: `env` for the variable
  `FACTLATTICE_CORPUS_CONFIG`, `user-config` for the pointer file `~/.config/factlattice/config.json`, or
  `example` for the built-in one), its `path` and its `sha256`. There is **no silent fallback**: if you point
  the variable (or the pointer file) at a file that does not exist or does not parse, the output says
  `NOT LOADED`, the run stops with exit status 2 and the code `registry-not-found`, and it does not quietly
  answer from the example registry. Likewise, naming a corpus the example registry does not have while the
  example registry is in use is `registry-not-found` ("no corpus registry is configured ..."), not "corpus not found".
- `registry.corpus_count`: how many corpora this run loaded (a count; never their names). If you configured a registry explicitly and it loads zero corpora, a corpus-scoped tool fails with `registry loaded 0 corpora`, and a tool that needs no corpus (selfcheck, lint) still runs but carries a `WARNING`. Every exit, including usage errors and a malformed registry file, still states the registry.
- `engine_package.path` and `core_package.path`: which copies of the engine and of the `factlattice` parser ran. If they come from different checkouts the output carries a `WARNING` (and a top-level `warnings` entry).
- `build`: one content hash over the source of BOTH packages, plus the checkout's git HEAD and dirty flag, or
  `unknown` with the reason. It never guesses.
- `query_log` and `ledger`: where this call's log record and (for writers) ledger record go, and
  `differences_from_server`: the three ways a `call` process differs from a running server.

Without any registry configured, running from the repository's top directory loads the repository's
own `specs/` directory as an example corpus called `alpha`. Try a query against it:

```
python3 -m factlattice_engine call lts_find --args-json '{"corpus": "alpha", "text": "freshness", "limit": 3}' --json \
  | python3 -c 'import sys, json; r = json.load(sys.stdin)["result"]; print(r["total_matched"], [n["id"] for n in r["nodes"]])'
```

```
22 ['DEC-CONTENT-HASH-ORACLE', 'FL-FRESHNESS', 'FLOW-QUERY']
```

## Where to go next

- Point `FACTLATTICE_CORPUS_CONFIG` at your own registry, or record it once in the per-user pointer file
  `~/.config/factlattice/config.json` (`{"corpus_config": "/absolute/path/to/registry.json"}`), so the
  command line and the MCP server read the same one (see `factlattice/registry.py`), and repeat step 5: the
  banner and `call_context` should name your file, its hash and your corpus count.
- With a broken registry, `call` exits 2 (even for tools that need no corpus, such as `lts_selfcheck`), and names the reason. A registry file saved with a UTF-8 BOM is refused as "cannot be read as JSON" (JSONDecodeError): save it without a BOM.
- Edit a registry file or the pointer file **atomically**: write a temporary file beside it and rename it into
  place. A server that boots while an editor has half-written the file waits once and retries; if the file is
  still broken it starts in *registry-error mode* (every tool is listed, every corpus-scoped tool answers
  `registry-not-found` with the reason and returns no data) and only a restart ends that mode.
- Run the full suite: `python3 -m pytest tests -q`.
- Only then wire up the MCP server (`python3 -m factlattice_engine.server`). Because `call` and the
  server share one dispatch, anything you verified here is what the server will answer.
