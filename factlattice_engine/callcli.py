"""`python -m factlattice_engine call <tool>` -- run ONE engine tool from the command line.

The same dispatch the MCP server uses (`tools.dispatch`), over the same composition root
(`server.build_tools`), so a result printed here and a result the MCP server returns are the
same for the same inputs, registry and build (P1; proved door-to-door by
`tests/test_cli_call.py`, which drives the server side through `Server.handle`).

  python -m factlattice_engine call <tool> [--args-json '<json>' | --args-file F]
                                           [--allow-write] [--json]
  python -m factlattice_engine call --list [--json]

PROPERTIES (design brief approved by the maintainer)
  P1  one code path: this module adds NO tool logic. It parses arguments, gates writers,
      calls `tools.dispatch(name, args, tools=<engine>)` and reports.
  P2  read-only by default: a WRITE tool (`tools.LTS_WRITE_TOOL_NAMES`, the engine's own
      list) is refused unless `--allow-write` is given, and the refusal names that flag.
  P3  privacy: this module holds NO private-corpus rule. Fan-out skipping, not-found
      wording and name hiding all live behind `dispatch`, so they bind this door exactly
      as they bind the server (a test greps this file for the rule's names).
  P4  configuration is STATED: the corpus-registry path actually loaded (or that it was
      NOT loaded and the built-in example registry is in use), the registry's corpus count,
      the engine package path, and a build stamp (content hash of the package source, plus
      the checkout's git HEAD and dirty flag, or "unknown", never a guess).
  P5  machine-readable: `--json` prints ONE envelope; exit status is 0 only for a tool that
      ran and succeeded. Tool error 1, refused writer 3, usage / unknown tool / cannot
      import / cannot build the engine 2. A selfcheck whose verdict is not PASS, and a lint
      with error-severity findings, exit 1 even though the tool itself ran.
  P6  fail closed and loud.
  P8  the query log and the node-change ledger are written by `dispatch` itself, so they
      behave as for the server. The differences (own session id per process, no file
      watcher, no long-lived caches) are listed in `call_context.differences_from_server`.

EXIT STATUS: 0 ok | 1 tool error or failed verdict | 2 usage, unknown tool, import or engine
build failure | 3 writer refused (no --allow-write).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from factlattice import registry
from factlattice.registry import RegistryError

from . import config, querylog
from . import server as _server
from .server import build_tools
from .tools import LTS_AUTHORING_TOOL_NAMES, LTS_READ_ONLY_TOOL_NAMES, LTS_WRITE_TOOL_NAMES

EXIT_OK, EXIT_TOOL_ERROR, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 3

ENV_LABEL = registry.env_var_name()     # DISPLAY only: the name comes from the one module that may spell it

# What `call` covers: EXACTLY what the server serves, classified FAIL CLOSED by the engine's
# explicit READ allow-list (`tools.LTS_READ_ONLY_TOOL_NAMES`): a served tool that is not on it is a
# WRITE, refused without --allow-write. Functions, not constants, so the answer tracks
# `server.TOOLS` as it is when asked.
def served_tool_names() -> Tuple[str, ...]:
    return tuple(t["name"] for t in _server.TOOLS)


def read_tool_names() -> Tuple[str, ...]:
    return tuple(n for n in served_tool_names() if n in LTS_READ_ONLY_TOOL_NAMES)


def write_tool_names() -> Tuple[str, ...]:
    return tuple(n for n in served_tool_names() if n not in LTS_READ_ONLY_TOOL_NAMES)


# The writers that append to the per-file node-change ledger (a subset of the write tools).
LEDGERED_TOOL_NAMES = tuple(n for n in LTS_AUTHORING_TOOL_NAMES if n in LTS_WRITE_TOOL_NAMES)

# Tools that legitimately need NO loaded corpus (a path, a draft, or the engine's own control).
# Every other tool is corpus-scoped: against an explicit registry that loaded zero corpora it
# FAILS rather than returning an empty, successful answer.
CORPUS_FREE_TOOLS = ("lts_selfcheck", "lts_lint", "lts_preview", "lts_property_loss",
                     "lts_add_property", "lts_add_node", "lts_repair_property",
                     "lts_rename_property", "lts_remove_property")

# ONE table of tool-specific verdict knowledge: tool -> (result field, predicate that is True when
# the field says OK, error text when it is not). A tool whose result carries a verdict beyond
# `success` gets its entry here in the same change (SDLC 7.6 [CLI-CALL-VERDICT-GATE]).
VERDICT_GATES = {
    "lts_selfcheck": ("verdict", lambda v: v == "PASS", "selfcheck verdict is not PASS"),
    "lts_lint": ("exit_code", lambda v: not v, "lint found error-severity findings (see result.summary)"),
}

WRITE_OPT_IN = "--allow-write"

# Honest, fixed statements of how a `call` process differs from a long-running server. They are
# printed with every call; a difference that is not on this list is a defect.
DIFFERENCES_FROM_SERVER = (
    "own process and session id: query-log records carry this process's session id, not the server's",
    "no file watcher or code-currency sentinel: the engine is built once for this call and "
    "re-hashes on demand, so a long-lived server's warm caches are absent (answers are the same)",
    "uses THIS checkout's code and THIS environment's registry, which can differ from the "
    "running server's (see corpus_config, engine_package and build)",
)

_GIT_TIMEOUT_S = 5


def _dispatch(name: str, arguments: dict, engine) -> dict:
    """The one call into the engine. Resolved at call time (a test seam), and it is
    `tools.dispatch`: the same function `Server.handle` calls."""
    from . import tools
    return tools.dispatch(name, arguments, tools=engine)


# ----------------------------------------------------------------------- P4: the statement
def _git(pkg_parent: str, *args: str) -> Optional[str]:
    try:
        p = subprocess.run(["git", "-C", pkg_parent, *args], capture_output=True, text=True,
                           timeout=_GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def _package_dirs() -> Tuple[str, str]:
    """(factlattice_engine dir, factlattice core dir) as actually imported by THIS process."""
    import factlattice
    import factlattice_engine
    return (os.path.dirname(os.path.abspath(factlattice_engine.__file__)),
            os.path.dirname(os.path.abspath(factlattice.__file__)))


def build_info(package_dir: str, core_dir: Optional[str] = None) -> Dict[str, Any]:
    """The build stamp. `source_sha256` hashes the source of BOTH the engine package and the
    `factlattice` core package (the parser, which the selfcheck exercises), through the engine's
    own `build_stamp.build_stamp`; the two halves are also given separately. The git fields are
    filled ONLY when the engine package sits at the root of a git checkout that tracks it;
    otherwise they are None and say why. A package installed into some unrelated repo's venv
    must not report that repo's HEAD."""
    from .build_stamp import build_stamp
    import hashlib
    if core_dir is None:
        core_dir = _package_dirs()[1]
    eng_h, core_h = build_stamp(package_dir), build_stamp(core_dir)
    combined = None if eng_h is None or core_h is None else \
        "sha256:" + hashlib.sha256((eng_h + "\n" + core_h).encode("utf-8")).hexdigest()
    info: Dict[str, Any] = {"source_sha256": combined, "engine_sha256": eng_h, "core_sha256": core_h,
                            "git_head": None, "git_dirty": None}
    parent = os.path.dirname(package_dir)
    top = _git(parent, "rev-parse", "--show-toplevel")
    tracked = _git(parent, "ls-files", "--error-unmatch", "factlattice_engine/__init__.py")
    if top is None or tracked is None or os.path.realpath(top.strip()) != os.path.realpath(parent):
        info["git_note"] = ("unknown: the engine package is not at the root of a git checkout "
                            "that tracks it (installed copy?); source_sha256 is the stamp")
        return info
    head = (_git(parent, "rev-parse", "HEAD") or "").strip()
    status = _git(parent, "status", "--porcelain")
    if not head or status is None:
        info["git_note"] = "unknown: git could not report HEAD or status for this checkout"
        return info
    info["git_head"] = head
    info["git_dirty"] = bool(status.strip())
    info["git_note"] = "HEAD of the checkout containing the engine package; dirty = uncommitted changes"
    return info


def corpus_config_info() -> Dict[str, Any]:
    """Which registry this run uses, from THE lookup: `source` (env | user-config | example), `path`, `sha256`
    (registry bytes, plus cwd/root/HOME/profile-dir only when a path needs them), `loaded`, and a `note`. It never reads the environment itself. A
    broken configured registry is `loaded: false` with the named `error` -- NOT a silent example fallback."""
    try:
        ch = registry.resolve_registry()
    except RegistryError as exc:
        return {"env_var": ENV_LABEL, "source": exc.source, "path": exc.path, "sha256": None,
                "loaded": False, "error": str(exc),
                "note": f"NOT LOADED: {exc.reason}. There is NO fallback to the built-in example registry; "
                        f"fix it (set {ENV_LABEL} to a registry file, or correct {registry.POINTER_DISPLAY}) "
                        "and run again"}
    if ch.source == "example":
        return {"env_var": ENV_LABEL, "source": "example", "path": None, "sha256": ch.sha256, "loaded": False,
                "note": f"no corpus registry is configured ({ENV_LABEL} is not set and "
                        f"{registry.POINTER_DISPLAY} does not exist): the built-in example registry is in use "
                        "(paths relative to the root variable or the working directory)"}
    via = "" if ch.source == "env" else f" (named by the per-user pointer {registry.pointer_display()})"
    return {"env_var": ENV_LABEL, "source": ch.source, "path": ch.path, "sha256": ch.sha256, "loaded": True,
            "note": "registry file loaded" + via}


def registry_statement(engine=None, reason: Optional[str] = None) -> Dict[str, Any]:
    """The registry statement every exit path that has read the environment carries (F3): the
    config (path, loaded, note) and the counts. `corpus_count` is an int, or None meaning the
    engine was NOT built, with `count_note` saying why. NAMES NO CORPUS: P3 binds it too."""
    entries, entries_note = None, None
    try:
        entries = len(config.default_corpora())
    except Exception as exc:  # noqa: BLE001 -- the statement must never be what breaks a call
        entries_note = f"registry unreadable: {type(exc).__name__}: {exc}"
    if engine is not None and hasattr(engine, "store"):
        count, note = len(engine.store.partitions()), "loaded corpora"
    else:
        count, note = None, "not read: " + (reason or "the engine was not built") + \
            (f"; {entries_note}" if entries_note else "")
    out = {"corpus_count": count, "registry_entries": entries, "count_note": note}
    if entries_note and engine is not None:
        out["entries_note"] = entries_note
    return {"corpus_config": corpus_config_info(), "registry": out}


def _explicit_registry_empty(ctx: Dict[str, Any]) -> bool:
    """F1: an EXPLICITLY configured registry (the env var is set) that loaded zero corpora."""
    return ctx["corpus_config"]["path"] is not None and ctx["registry"]["corpus_count"] == 0


def build_context(tool: str, allow_write: bool, engine=None) -> Dict[str, Any]:
    """The `call_context` block. NAMES NO CORPUS (a count only): P3 binds the statement too."""
    eng_dir, core_dir = _package_dirs()
    same = os.path.dirname(os.path.realpath(eng_dir)) == os.path.dirname(os.path.realpath(core_dir))
    qlog = querylog.log_path()
    from factlattice import ledger
    ctx = {"tool": tool, "allow_write": bool(allow_write), **registry_statement(engine),
           "engine_package": {"path": eng_dir},
           "core_package": {"path": core_dir},
           "packages_same_checkout": same,
           "build": build_info(eng_dir, core_dir),
           "query_log": {"enabled": qlog is not None, "path": str(qlog) if qlog else None,
                         "logged_for_this_tool": tool in querylog._LOGGED_TOOLS},
           "ledger": {"state_dir": str(ledger.state_dir()),
                      "ledgered_for_this_tool": tool in LEDGERED_TOOL_NAMES},
           "differences_from_server": list(DIFFERENCES_FROM_SERVER)}
    return ctx


def early_context(tool: Optional[str], allow_write: bool, reason: str) -> Dict[str, Any]:
    """F3: the context for an exit that came before the engine was built. It still states the
    registry (path, loaded flag, entries) and says the count was not read, and why."""
    return {"tool": tool, "allow_write": bool(allow_write), **registry_statement(None, reason)}


def context_warnings(ctx: Dict[str, Any], tool: str) -> List[str]:
    """Warnings that travel with a RESULT (top-level `warnings` in JSON, stderr in human mode)."""
    w: List[str] = []
    if _explicit_registry_empty(ctx):
        w.append(f"registry loaded 0 corpora: {ctx['corpus_config']['path']} is configured but "
                 f"yielded none; {tool} needs no corpus so it ran, but every corpus-scoped tool "
                 "would fail against this registry")
    if ctx.get("packages_same_checkout") is False:
        w.append("the engine and the factlattice core package were imported from different "
                 f"checkouts ({ctx['engine_package']['path']} vs {ctx['core_package']['path']}): "
                 "this run combines code from two trees, and the stamp covers both")
    return w


def _banner(ctx: Dict[str, Any], warnings: Optional[List[str]] = None) -> str:
    cc, n = ctx["corpus_config"], ctx["registry"]
    if cc["loaded"]:
        reg = f"{cc['path']} [{cc.get('source')}] sha256:{str(cc.get('sha256'))[:12]}"
    elif cc.get("source") == "example":
        reg = (f"built-in example registry (no registry configured: {ENV_LABEL} is not set "
               f"and {registry.POINTER_DISPLAY} does not exist)")
    else:
        reg = f"NOT LOADED: {cc.get('path') or cc.get('source')}: {cc.get('error') or 'registry-not-found'}"
    lines = [f"call {ctx['tool']}", f"  registry : {reg}",
             f"  corpora  : {n['corpus_count']} loaded ({n['registry_entries']} registry entries)"
             if n["corpus_count"] is not None else
             f"  corpora  : {n['count_note']}; {n['registry_entries']} registry entries"]
    if "build" in ctx:
        b = ctx["build"]
        head = b["git_head"][:12] + (" (dirty)" if b["git_dirty"] else "") if b["git_head"] else "unknown"
        lines += [f"  package  : {ctx['engine_package']['path']}",
                  f"  core     : {ctx['core_package']['path']}",
                  f"  build    : git {head}; source {str(b['source_sha256'])[:19]}",
                  f"  query log: {ctx['query_log']['path'] or 'OFF'}"]
    lines += [f"WARNING: {w}" for w in (warnings or [])]
    return "\n".join(lines)


# --------------------------------------------------------------------------- arguments
class UsageError(Exception):
    pass


def _load_args(args_json: Optional[str], args_file: Optional[str]) -> Dict[str, Any]:
    if args_json is not None and args_file is not None:
        raise UsageError("give --args-json OR --args-file, not both")
    if args_json is None and args_file is None:
        return {}
    if args_file is not None:
        try:
            with open(args_file, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            raise UsageError(f"cannot read --args-file {args_file!r}: {exc.strerror or exc}")
    else:
        raw = args_json
    try:
        val = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError(f"malformed args: not valid JSON ({exc})")
    if not isinstance(val, dict):
        raise UsageError(f"malformed args: must be a JSON object, got {type(val).__name__}")
    return val


# --------------------------------------------------------------------------- the verdict
def _gate(name: str, result: Any) -> Tuple[int, Optional[str]]:
    """Exit status and error text for a tool result. 0 ONLY for a tool that ran and succeeded.
    Tool-specific verdict fields come from `VERDICT_GATES`, never from this function's body."""
    if not isinstance(result, dict):
        return EXIT_TOOL_ERROR, f"tool returned {type(result).__name__}, not a result object"
    if result.get("success") is not True:
        # `registry-not-found` is a CONFIGURATION failure (no usable corpus registry), the same class as
        # "cannot build the engine": exit 2, never the tool-error 1 an unknown corpus gets.
        rc = EXIT_USAGE if result.get("reason") == registry.CODE else EXIT_TOOL_ERROR
        return rc, str(result.get("error") or "tool reported success: false")
    gate = VERDICT_GATES.get(name)
    if gate is not None:
        field, ok, message = gate
        if not ok(result.get(field)):
            return EXIT_TOOL_ERROR, f"{message} ({field}={result.get(field)!r})"
    return EXIT_OK, None


def _emit(env: Dict[str, Any], as_json: bool, banner: Optional[str]) -> None:
    if as_json:
        print(json.dumps(env, indent=1, default=str))
        return
    if banner:
        print(banner, file=sys.stderr)
    if env.get("error"):
        print(f"{'REFUSED' if env.get('refused') else 'ERROR'}: {env['error']}", file=sys.stderr)
    if env.get("result") is not None:
        print(json.dumps(env["result"], indent=2, default=str))


def _envelope(tool, ctx, *, result=None, error=None, refused=False, rc=EXIT_OK,
              warnings=None) -> Dict[str, Any]:
    env: Dict[str, Any] = {"success": rc == EXIT_OK, "refused": refused, "exit_code": rc,
                           "tool": tool, "call_context": ctx}
    if error:
        env["error"] = error
    if warnings:
        env["warnings"] = list(warnings)
    if result is not None:
        env["result"] = result
    return env


def list_tools(as_json: bool) -> int:
    read, write = read_tool_names(), write_tool_names()
    listing = {"read": list(read), "write": list(write),
               "write_opt_in": f"{WRITE_OPT_IN} (writers are refused without it)"}
    if as_json:
        print(json.dumps(listing, indent=1))
    else:
        print("READ tools (run by default):")
        for n in read:
            print(f"  {n}")
        print(f"WRITE tools (refused unless you pass {WRITE_OPT_IN}):")
        for n in write:
            print(f"  {n}")
    return EXIT_OK


def run(tool: Optional[str], args_json: Optional[str], args_file: Optional[str],
        allow_write: bool, as_json: bool, list_only: bool = False) -> int:
    if list_only:
        return list_tools(as_json)

    def early(msg, rc, reason, refused=False):
        ctx = early_context(tool, allow_write, reason)
        _emit(_envelope(tool, ctx, error=msg, refused=refused, rc=rc), as_json, _banner(ctx))
        return rc

    served = served_tool_names()
    if not tool or tool not in served:
        msg = (f"unknown tool {tool!r}. Covered tools: " + ", ".join(served)) if tool else \
              "no tool named. Use --list to see the covered tools"
        return early(msg, EXIT_USAGE, "usage error before the engine was built")
    try:
        arguments = _load_args(args_json, args_file)
    except UsageError as exc:
        return early(str(exc), EXIT_USAGE, "usage error before the engine was built")
    if tool in write_tool_names() and not allow_write:
        msg = (f"{tool} is a WRITE tool (or is not on the engine's read-only allow-list) and `call` "
               f"is read-only by default. Nothing was run. Pass {WRITE_OPT_IN} to run it for real.")
        return early(msg, EXIT_REFUSED, "refused before the engine was built", refused=True)
    try:
        engine = build_tools(watch=False, require_corpora=False)
    except RegistryError as exc:    # no fallback: a broken or missing configured registry stops the run (exit 2)
        return early(f"cannot build the engine: {exc}", EXIT_USAGE, f"the registry could not be resolved ({exc})")
    except Exception as exc:  # noqa: BLE001
        return early(f"cannot build the engine: {type(exc).__name__}: {exc}", EXIT_USAGE,
                     f"the engine could not be built ({type(exc).__name__}: {exc})")
    ctx = build_context(tool, allow_write, engine)
    warnings = context_warnings(ctx, tool)
    if tool not in CORPUS_FREE_TOOLS and _explicit_registry_empty(ctx):
        other = [w for w in warnings if not w.startswith("registry loaded 0")]
        msg = (f"registry loaded 0 corpora: {ctx['corpus_config']['path']} is configured "
               f"({ctx['registry']['registry_entries']} registry entries) but none of them loaded "
               f"(missing roots, or no parseable nodes), so {tool} has nothing to answer from. "
               "Nothing was run.")
        _emit(_envelope(tool, ctx, error=msg, rc=EXIT_TOOL_ERROR, warnings=other), as_json,
              _banner(ctx, other))
        return EXIT_TOOL_ERROR
    try:
        result = _dispatch(tool, arguments, engine)
    except Exception as exc:  # noqa: BLE001 -- an exception is an errored call, never a pass
        _emit(_envelope(tool, ctx, error=f"{type(exc).__name__}: {exc}", rc=EXIT_TOOL_ERROR,
                        warnings=warnings), as_json, _banner(ctx, warnings))
        return EXIT_TOOL_ERROR
    rc, err = _gate(tool, result)
    _emit(_envelope(tool, ctx, result=result, error=err, rc=rc, warnings=warnings), as_json,
          _banner(ctx, warnings))
    return rc
