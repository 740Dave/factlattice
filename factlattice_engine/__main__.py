"""`python -m factlattice_engine <command>`: human-facing entry points.

  semantic "<question>" --corpus <id> [--k N] [--prefix P] [--json]
      Semantic SUGGESTIONS for a question in one corpus ([SEM-FIND]). Human-readable by default,
      `--json` for agents (the same block `lts_semantic_find` returns). Loads ONLY the named
      corpus. The first use per corpus builds its local index synchronously, and says so first.

  call <tool> [--args-json '<json>' | --args-file F] [--allow-write] [--json]   |   call --list
      Run ONE engine tool through the same dispatch the MCP server uses, with no MCP connection.
      Read-only unless --allow-write. States which registry, package and build it ran against.
      Exit 0 ok | 1 tool error / failed verdict | 2 usage, unknown tool, cannot import | 3 writer
      refused. See factlattice_engine/callcli.py and docs/TRY-BEFORE-YOU-DEPLOY.md.
"""
from __future__ import annotations

import argparse
import json
import sys


def _semantic(args) -> int:
    from . import config
    from .tools import LtsEngine
    from .semantic.suggest import SemanticService
    from factlattice import registry
    try:
        choice = registry.resolve_registry()        # THE lookup; a broken configured registry is a stop
        corpora = choice.corpora
        if args.corpus not in corpora and choice.source == "example":
            raise registry.example_missing(args.corpus)     # P1: not "unknown corpus", but "no registry"
    except registry.RegistryError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.corpus not in corpora:
        print(f"unknown corpus {args.corpus!r}. Registered: {', '.join(sorted(corpora))}", file=sys.stderr)
        return 2
    store = config.build_store({args.corpus: corpora[args.corpus]})
    eng = LtsEngine(store)
    eng.semantic = SemanticService(build_mode="sync")
    part = store.get_partition(args.corpus)
    if part is not None and not args.json:
        from .semantic.index import SemanticIndex, IndexIncompatible
        try:
            SemanticIndex(eng.semantic.state_root, args.corpus, eng.semantic._get_embedder())._load()
        except IndexIncompatible:
            print(f"(first use for {args.corpus}: building its local semantic index for "
                  f"{len(part.nodes)} nodes. This runs once, then only changed nodes are re-embedded.)",
                  file=sys.stderr)
        except Exception:   # noqa: BLE001 -- unavailability is reported by the query itself
            pass
    res = eng.semantic_find(corpus=args.corpus, text=args.question, k=args.k, prefix=args.prefix)
    if args.json:
        print(json.dumps(res, indent=1, default=str))
        return 0 if res.get("success") else 1
    if not res.get("success"):
        print(res.get("error"), file=sys.stderr)
        return 1
    print(f'Suggestions for "{args.question}" in {args.corpus}')
    print("(ranked by relevance: keyword statistics + embedding similarity; NOT matches, NOT assertions)\n")
    if res.get("status") != "ok":
        print(f"semantic search is {res.get('status')}: {res.get('reason')}")
        return 0
    if not res["candidates"]:
        print("no suggestions (that is not evidence the corpus lacks it; try lts_find_in_prose)")
    for c in res["candidates"]:
        print(f"{c['rank']:2d}. {c['id']}  [{c['confidence']}]")
        print(f"    {c['title']}")
        unm = ", ".join(c["unmatched_terms"]) or "-"
        print(f"    matched: {', '.join(c['matched_terms']) or '-'}    not in it: {unm}")
        print(f"    \"{c['evidence']}\"\n")
    fresh = "fresh" if res.get("index_fresh") else f"STALE ({res.get('nodes_unembedded_stale')} nodes pending)"
    print(f"model {res['model']} · index {fresh} · {res['extent']['nodes_in_scope']} nodes")
    return 0


def _call(args) -> int:
    try:
        from . import callcli
    except ImportError as exc:      # P6: a broken install is loud and non-zero, never a traceback-or-nothing
        msg = f"cannot import the engine's call module ({type(exc).__name__}: {exc}). Is the package installed whole?"
        print(msg, file=sys.stderr)
        if args.json:
            print(json.dumps({"success": False, "refused": False, "exit_code": 2, "error": msg}))
        return 2
    return callcli.run(args.tool, args.args_json, args.args_file, args.allow_write, args.json,
                       list_only=args.list)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m factlattice_engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("semantic", help="semantic suggestions for a question in one corpus")
    s.add_argument("question")
    s.add_argument("--corpus", required=True)
    s.add_argument("--k", type=int, default=8)
    s.add_argument("--prefix")
    s.add_argument("--json", action="store_true")
    c = sub.add_parser("call", help="run one engine tool from the command line (read-only by default)")
    c.add_argument("tool", nargs="?")
    c.add_argument("--args-json", help="the tool's arguments as one JSON object")
    c.add_argument("--args-file", help="a file holding the tool's arguments as one JSON object")
    c.add_argument("--allow-write", action="store_true",
                   help="permit WRITE tools (typed writers, corpus registry, miss log); refused without it")
    c.add_argument("--json", action="store_true", help="print one JSON envelope (result + call_context)")
    c.add_argument("--list", action="store_true", help="list the tools `call` covers and exit")
    args = ap.parse_args(argv)
    if args.cmd == "semantic":
        return _semantic(args)
    if args.cmd == "call":
        return _call(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
