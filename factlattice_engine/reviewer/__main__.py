"""`python -m factlattice_engine.reviewer <command>` -- READ-ONLY commands the reviewer's procedure calls.

  resolve-output --corpus C --document P     where a review of P would be written (addendum C3(c), N1)
  check-extension-paths --document P --extension E [--extension E2 ...]
                                             refuse an extension that IS the document under review (v3 4.4)
  new-key --corpus C                         a fresh review key (REVIEW-KEY-GRAMMAR)
  self-gate --file F                         the self-gate (spec 4.5, addendum C4(b)) over a written review file
  check-registrations --corpus C             the stale-server tripwire for extension registrations (reads the audit itself)
  gen6-scan --path P                         lines that address the review itself (GEN-6): count and hit lines
  scan-secrets --document P                  lines the S5 detector would withhold: node, key, line, reason (never the value)
  lint-groups --document P                   lint warnings as one group per kind, true counts (S2)
  dangles (--corpus C | --own-corpus) --document P [--profile F]   the structure gate's lint at document scope, forward dangles resolved against the CORPUS
  frame --corpus C --source-file F [--exclude-source REAL ...]   what the review may read, in order (S3)
  verdict --review-file F [--frame-exceeded] | --args-json J   the verdict rule (S3, S4); inputs built from the review file
  evidence --args-json J                     a finding's evidence, with secrets withheld (S5)
  no-document-reply --reason R [--detail D] [--inspect-path P]   the reply when no document exists (P1-2)
  registry-check [--mcp-json J]              which registry the CLI route uses, compared with the MCP route's registry_choice (SPEC-U1a P2)

Each exists so that a decision the procedure depends on is CODE, not model judgement. None writes anything.

EXIT STATUS. 0 ok | 1 the command's own finding or in-band failure (gen6-scan and scan-secrets: hits found; lint-groups: lint errors
found; dangles: a TRUE dangle or a non-dangle lint error; the others: a failure with a `reason`) | 2 usage error (`usage-error`), a failure that raised (`helper-error` ...), or
`registry-not-found` | 3 the helper CRASHED (`helper-crashed`: an exception escaped every verb branch, for instance at the final
print). 3 is distinct on purpose: gen6-scan's 1 means "hits found", so a crash must never look like a finding. Every nonzero
exit other than the findings-only 1s above prints the failure contract JSON on stdout.

`resolve-output` in detail (addendum C3(c), N1).

READ-ONLY. It resolves where a review of document P in corpus C would be written, against THIS environment's registry
(`factlattice.registry`) and the live REVIEWER-SITE-CONFIG, by calling the same `resolve_output` the reviewer
obeys. It writes nothing (no file, no directory, no ledger record); there is no flag that makes it write.

Prints one JSON object: `{root, rule_used, folder, directory, reason, site_config, site_overrides, checks}`.
Exit status: 0 a root was resolved | 1 `no-output-root` (the reason is in the JSON) | 2 usage, the engine
could not be built, or `registry-not-found` (no corpus registry resolves, or the built-in example registry is in use and has
no such corpus or no site configuration: the configuration class, as `call`; the JSON carries `reason: registry-not-found`).
EVERY command exits 2 on `registry-not-found`.
"""
from __future__ import annotations

import argparse
import builtins
import json
import os
import stat
import sys
from typing import Optional

from factlattice import registry
from factlattice.hashing import CorpusFileError
from factlattice.registry import RegistryError

from . import RESOLVE_REFUSALS, UNVERIFIABLE_PRIVATE


# ⛔ THE FAILURE CONTRACT OF EVERY HELPER, raised OR in-band (tests/test_reviewer_reasons.py drives all 14 verbs):
#   * `reason`       a FIXED CODE from HELPER_REASON_CODES (a closed list, kebab-case): never message text, never a path;
#   * `error_class`  never null: the exception class for a raised failure, `ToolError` for an engine tool's text failure,
#                    `RegistryError` for a registry stop, `InBand` for a failure the helper itself found and reports;
#   * NO `error` key (it is the field name an agent is most likely to copy);
#   * `explanation`  (optional) a FIXED sentence that explains the code: nothing is interpolated into it;
#   * `raw_error`    (optional) the raw message, for the TERMINAL only, beside the NEVER-persist note;
#   * `terminal_only` (optional) a structure that holds full paths (check-extension-paths' findings, the ids of an
#                    ambiguous site config), beside the same note. A path-bearing list is never offered under a plain name.
# The agent that runs these helpers may copy a `reason` into a persisted UNREVIEWED document, and a message can carry a
# path with any directory name in it, so a path is reduced by STRUCTURE (it is kept out), never by a pattern over text.
RAW_NOTE = "raw_error is for the terminal only: NEVER persist it in a review document"
TERMINAL_NOTE = "terminal_only is for the terminal only: NEVER persist it in a review document"
# [ISSUE-NONREGULAR-MEMBER] a corpus whose build failed on a member `.lts`: the registry is FINE, so
# this is never `registry-not-found`. The code is the engine's own; the explanation is a FIXED sentence (nothing interpolated),
# and the root-relative path is deliberately NOT offered (a persisted record carries the code and class only).
CORPUS_FILE_EXPLANATIONS = {
    "corpus-file-not-regular": "a .lts member of the corpus is a FIFO, socket or device (or a link to one); it was not opened",
    "corpus-file-broken-link": "a .lts member of the corpus is a symbolic link that does not resolve (dangling or looping)",
    "corpus-file-unreadable": "a .lts member of the corpus cannot be read by this process",
    "corpus-dir-unreadable": "a directory inside the corpus root cannot be listed by this process",
}
HELPER_REASON_CODES = (
    "helper-error", "invalid-argument", "registration-audit-failed", "registrations-not-projected", "frame-read-incomplete",
    "registry-mismatch", registry.CODE, "usage-error", "helper-crashed", "lint-incomplete", "self-gate-failed", "extension-is-the-reviewed-document", "corpus-not-served", "own-corpus-ambiguous",
) + tuple(RESOLVE_REFUSALS) + tuple(CORPUS_FILE_EXPLANATIONS)


def _class_of_text(raw: str) -> str:
    """The class of a failure that arrives as TEXT (an engine tool's `error`). A class name is taken from the text only if
    it names an exception class that EXISTS in `builtins`; anything else (including text the caller chose) is `ToolError`."""
    name = raw.partition(":")[0].strip()
    cls = getattr(builtins, name, None)
    return name if isinstance(cls, type) and issubclass(cls, BaseException) else "ToolError"


def _fail(exc_or_text, code: str = "helper-error", **extra) -> str:
    if isinstance(exc_or_text, BaseException):
        cls = type(exc_or_text).__name__
        try:
            raw = f"{cls}: {exc_or_text}"
        except Exception:                                  # noqa: BLE001 -- an exception whose __str__ raises still gets the contract
            raw = f"{cls}: <message unprintable>"
    else:
        try:
            raw = str(exc_or_text)
        except Exception:                                  # noqa: BLE001
            raw = "<unprintable>"
        cls = _class_of_text(raw)
    out = {**extra, "reason": code, "error_class": cls, "raw_error": raw[:200], "raw_error_note": RAW_NOTE}
    return json.dumps(out)


def _in_band(out: dict, code: str) -> dict:
    """A failure the helper found itself: a fixed code and the class `InBand`, nothing raised."""
    out["reason"], out["error_class"] = code, "InBand"
    return out


def _lintable_file(path: str) -> bool:
    """A readable REGULAR file. A directory, `.`, a FIFO (never opened), a broken symlink, a missing path and a file this process
    cannot read all fail: the engine's lint raises for them and logs a traceback on stderr, or lints a directory as a corpus."""
    try:
        real = os.path.realpath(path)
        return stat.S_ISREG(os.stat(real).st_mode) and os.access(real, os.R_OK)
    except OSError:
        return False


def _not_a_file() -> int:
    print(json.dumps(_in_band({"ok": False, "complete": False, "explanation": "the document does not exist or is not a readable file"},
                              "lint-incomplete"), indent=1))
    return 2


def _corpus_file_stop(ans) -> Optional[int]:
    """If an engine reply is a corpus-file refusal, print the helper failure contract with the PRECISE code (exit 2) and
    return 2; else None. `raw_error` is the engine's own message (class + code + root-relative path: path-free)."""
    code = ans.get("reason") if isinstance(ans, dict) else None
    if code not in CORPUS_FILE_EXPLANATIONS:
        return None
    print(json.dumps({"ok": False, "success": False, "reason": code, "error_class": "CorpusFileError",
                      "explanation": CORPUS_FILE_EXPLANATIONS[code], "raw_error": str(ans.get("error", ""))[:200],
                      "raw_error_note": RAW_NOTE}, indent=1))
    return 2


def _real(path: str) -> str:
    """`os.path.realpath`, except that a DANGLING symlink is a `corpus-file-broken-link` (realpath would turn it into the missing
    target's path and the read would then fail as a bare FileNotFoundError)."""
    rp = os.path.realpath(path)
    if os.path.islink(path) and not os.path.exists(rp):
        raise CorpusFileError("corpus-file-broken-link", os.path.basename(path))
    return rp


def _corpus_file_exc_stop(exc: BaseException) -> int:
    """A `CorpusFileError` raised by a helper that READ an agent-named file: the same coded refusal as an engine reply."""
    return _corpus_file_stop({"reason": exc.code, "error": f"CorpusFileError: {exc}"})


def _failed_container_stop(engine, corpora, document) -> Optional[int]:
    """The `dangles` verb's document-side corpus-file stop: if a registered corpus whose root contains `document` FAILED to build on a
    corpus-file fault ([ISSUE-NONREGULAR-MEMBER]), print the coded helper failure (exit 2) and return 2;
    else None. The reply is the engine's path-mode refusal (no corpus id, root-relative path), so a private corpus is not named."""
    from . import corpora_containing
    from ..tools import _corpus_file_refusal
    failed = getattr(engine.store, "failed_corpora", None) or {}
    for cid in corpora_containing(document, {c: v for c, v in (corpora or {}).items() if c in failed}):
        return _corpus_file_stop(_corpus_file_refusal(None, failed[cid]))
    return None


def _registry_stop(exc: RegistryError) -> int:
    """`registry-not-found`: no usable corpus registry. The configuration class -- exit 2, like `call`."""
    # `detail` is the persist-safe text (basenames only, built from parts); the LIVE text, with full paths, is raw_error
    print(json.dumps({"ok": False, "success": False, "reason": registry.CODE, "error_class": "RegistryError",
                      "detail": exc.persist_text, "raw_error": str(exc), "raw_error_note": RAW_NOTE}, indent=1))
    return 2


def _reply_registry_error(ans: dict) -> RegistryError:
    """A RegistryError for a registry stop that arrived as an engine REPLY. The reply's own `error_persist` (built from
    structured parts, basenames only) is `detail` when it is a non-empty string; otherwise the error persists the fixed
    generic sentence. The reply's `error` (full paths) stays the live text."""
    live = str(ans.get("error") or "")
    live = live.partition(": ")[2] or live
    persist = ans.get("error_persist")
    if isinstance(persist, str) and persist.strip():
        prefix = f"{registry.CODE}: "
        return RegistryError(live, persist=persist[len(prefix):] if persist.startswith(prefix) else persist)
    return RegistryError(live)


def _example_site_missing() -> RegistryError:
    return RegistryError(
        f"no corpus registry is configured (set {registry.env_var_name()} or create {registry.pointer_display()}); "
        "the built-in example registry is in use, so the reviewer's site configuration cannot be found",
        source="example")


def _resolve(corpus, document):
    from .. import config, tools
    from ..server import build_tools
    from factlattice.profile import load_profile
    from . import lookup_site_config, resolve_output

    choice = registry.resolve_registry()            # RegistryError -> exit 2 at main()
    if corpus and choice.source == "example" and corpus not in choice.corpora:
        raise registry.example_missing(corpus)       # P1: an example answer for a non-example corpus is a stop
    spec = choice.corpora.get(corpus) if corpus else None
    profile = None
    ppath = spec.get("profile") if isinstance(spec, dict) else None
    if ppath and os.path.isfile(ppath):
        try:
            profile = load_profile(ppath)
        except Exception:                                  # noqa: BLE001 — an unloadable profile supplies no output
            profile = None
    engine = build_tools(watch=False, require_corpora=False)
    site = lookup_site_config(lambda **kw: tools.dispatch("lts_find", kw, tools=engine))
    if choice.source == "example" and site.status == "none":
        raise _example_site_missing()                # Q-4: the example source has no site config: a stop, not no-output-root
    return resolve_output(corpus, os.path.realpath(document) if document else None, spec, profile, site)


class UsageError(Exception):
    """argparse found a usage error. Raised instead of printing usage text (which echoes the caller's argument) to stderr."""


class _UsageParser(argparse.ArgumentParser):
    def error(self, message):                              # noqa: D401 -- argparse calls this and expects no return
        raise UsageError(message)


def build_parser() -> argparse.ArgumentParser:
    ap = _UsageParser(prog="python -m factlattice_engine.reviewer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve-output", help="where would a review of this document be written (read-only)")
    r.add_argument("--corpus", help="the reviewed corpus id (omit if the document is not in a registered corpus)")
    r.add_argument("--document", required=True, help="path of the document under review (need not exist)")
    c = sub.add_parser("check-extension-paths", help="refuse an extension path that is the document under review")
    c.add_argument("--document", required=True)
    c.add_argument("--extension", action="append", required=True, help="an extension path (repeatable)")
    k = sub.add_parser("new-key", help="print a fresh review key for a corpus")
    k.add_argument("--corpus", help="the reviewed corpus id (omit if none was given)")
    g = sub.add_parser("self-gate", help="run the self-gate over a review file (read-only)")
    g.add_argument("--file", required=True, help="the review file (REVIEW-*.lts or UNREVIEWED-*.lts)")
    cr = sub.add_parser("check-registrations", help="compare the profile file's registrations with the audit's")
    cr.add_argument("--corpus", required=True)
    g6 = sub.add_parser("gen6-scan", help="lines of a document that address the review itself (an aid, not a control)")
    g6.add_argument("--path", required=True)
    ss = sub.add_parser("scan-secrets", help="lines of a document that the secret detector would withhold")
    ss.add_argument("--document", required=True)
    lg = sub.add_parser("lint-groups", help="lint warnings grouped by kind with true counts")
    lg.add_argument("--document", required=True)
    dg = sub.add_parser("dangles", help="document-scope lint; each forward dangle resolved against the corpus")
    dg.add_argument("--corpus", help="the corpus to resolve against (exclusive with --own-corpus)")
    dg.add_argument("--own-corpus", action="store_true",
                    help="an extension's own-corpus lint: the SERVED corpus whose root contains --document, under THAT corpus's "
                         "registered profile; `applicable: false` (exit 0) when there is none or it has no profile")
    dg.add_argument("--document", required=True)
    dg.add_argument("--profile", help="lint under this profile path instead of the corpus's registered one "
                                      "(an extension is linted under the reviewer profile and under its own)")
    fr = sub.add_parser("frame", help="the nodes a review may read, in order, with the frame ceiling")
    fr.add_argument("--corpus", required=True)
    fr.add_argument("--source-file", required=True)
    fr.add_argument("--exclude-source", action="append", default=[], help="realpath of a registered extension document")
    vd = sub.add_parser("verdict", help="the verdict rule")
    vd.add_argument("--args-json")
    vd.add_argument("--review-file")
    vd.add_argument("--frame-exceeded", action="store_true")
    ev = sub.add_parser("evidence", help="build a finding's evidence; withholds secrets")
    ev.add_argument("--args-json", required=True)
    rc_ = sub.add_parser("registry-check", help="the CLI route's registry, compared with the MCP route's registry_choice")
    rc_.add_argument("--mcp-json", help="the MCP server's lts_list_corpora `registry_choice` object as JSON; "
                                        "the word `absent` if the answer had none (a server too old); omit if the MCP route is not used")
    nd = sub.add_parser("no-document-reply", help="the reply when no review document exists")
    nd.add_argument("--reason", required=True)
    nd.add_argument("--detail")
    nd.add_argument("--inspect-path")
    return ap


def verbs() -> tuple:
    """Every verb of the parser, as the parser itself declares them (tests/test_reviewer_reasons.py fails on a new verb
    that is not in its coverage list)."""
    sub = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    return tuple(sub.choices)


def _report(exc: BaseException, code: str, rc: int) -> int:
    """Print the failure contract for `exc` (ensure_ascii JSON, so it cannot fail to encode) and return `rc`. If stdout itself
    cannot be written (a closed or broken pipe or descriptor) nothing more can be said: the rc is still returned."""
    try:
        print(_fail(exc, code))
        sys.stdout.flush()
    except Exception:                                      # noqa: BLE001
        pass
    return rc


def main(argv=None) -> int:
    """Run one verb. EXACTLY what holds: every `Exception`, every `SystemExit` with a non-zero or non-int code and every other
    `BaseException` except `KeyboardInterrupt` that escapes a verb ends as the failure contract on stdout (`usage-error`, rc 2;
    anything else `helper-crashed`, rc 3). What still escapes: `KeyboardInterrupt`; `SystemExit(0)` (`--help`); an import-time
    failure before this function runs; and a stdout that cannot be written at all (a closed or broken pipe or descriptor:
    the print is attempted and abandoned, or the interpreter's exit flush fails, rc 120). The procedure's `helper-output-unreadable`
    stop covers those last two: an empty or unparseable stdout is never interpreted."""
    try:
        return _main(argv)
    except KeyboardInterrupt:
        raise
    except SystemExit as exc:
        if exc.code in (0, None):
            raise                                          # `--help`
        return _report(exc, "helper-crashed", 3)
    except UsageError as exc:
        return _report(exc, "usage-error", 2)
    except BaseException as exc:                           # noqa: BLE001 -- the last line
        return _report(exc, "helper-crashed", 3)


def _main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "registry-check":
        try:
            from . import compare_registries
            if args.mcp_json is None:
                mcp = False
            elif args.mcp_json.strip().lower() == "absent":
                mcp = None
            else:
                mcp = json.loads(args.mcp_json)
            res = compare_registries(mcp, registry.resolve_registry().statement())
        except RegistryError as exc:
            return _registry_stop(exc)
        except Exception as exc:                           # noqa: BLE001
            print(_fail(exc, ok=False))
            return 2
        if res["mismatch"]:
            _in_band(res, res["reason"])
        print(json.dumps(res, indent=1))
        return 1 if res["mismatch"] else 0
    if args.cmd == "check-registrations":
        try:
            from .. import tools
            from ..server import build_tools
            from . import check_registrations
            engine = build_tools(watch=False, require_corpora=False)
            ans = tools.dispatch("lts_profile_audit", {"corpus": args.corpus}, tools=engine)
            if isinstance(ans, dict) and ans.get("reason") == registry.CODE:
                return _registry_stop(_reply_registry_error(ans))
            stop = _corpus_file_stop(ans)
            if stop is not None:
                return stop
            ppath = (ans.get("profile") or {}).get("path") if isinstance(ans, dict) else None
            if not ans.get("success") or not ppath:
                # no profile in scope: nothing to register (or the audit failed, which is not "fine")
                res = {"ok": bool(ans.get("success")), "raw_count": 0, "reported": 0, "reason": ""}
                if not ans.get("success"):
                    res = json.loads(_fail(str(ans.get("error", "")), "registration-audit-failed", ok=False, raw_count=0, reported=0))
            else:
                res = check_registrations(ppath, ans)
                if not res["ok"]:
                    _in_band(res, res["reason"])
        except RegistryError as exc:
            return _registry_stop(exc)
        except CorpusFileError as exc:
            return _corpus_file_exc_stop(exc)
        except Exception as exc:                           # noqa: BLE001
            print(_fail(exc, ok=False))
            return 2
        print(json.dumps(res, indent=1))
        return 0 if res["ok"] else 1
    if args.cmd == "gen6-scan":
        try:
            from . import gen6_scan
            res = gen6_scan(_real(args.path))
        except CorpusFileError as exc:
            return _corpus_file_exc_stop(exc)
        except Exception as exc:                           # noqa: BLE001
            print(_fail(exc))
            return 2
        print(json.dumps(res, indent=1, ensure_ascii=False))
        return 1 if res["count"] else 0
    if args.cmd == "scan-secrets":
        try:
            from . import scan_secrets
            hits = scan_secrets(_real(args.document))
        except CorpusFileError as exc:
            return _corpus_file_exc_stop(exc)
        except Exception as exc:                           # noqa: BLE001
            print(_fail(exc))
            return 2
        print(json.dumps({"count": len(hits), "hits": hits}, indent=1))
        return 1 if hits else 0
    if args.cmd in ("verdict", "evidence"):
        try:
            from . import decide_verdict, evidence, review_verdict_inputs
            if args.cmd == "verdict" and args.review_file:
                a = review_verdict_inputs(os.path.realpath(args.review_file))
                a["frame_exceeded"] = args.frame_exceeded
            else:
                a = json.loads(args.args_json)
            if args.cmd == "verdict":
                out = decide_verdict(a.get("findings") or [], a.get("checks") or [], bool(a.get("frame_exceeded")),
                                     int(a.get("omitted") or 0))
                if a.get("other_nodes") is not None:
                    from . import plan_findings
                    plan = plan_findings(list(a.get("findings") or []) + out["added_findings"], int(a["other_nodes"]))
                    out["findings_by_severity_all"] = out["findings_by_severity"]
                    written = {k: 0 for k in ("info", "minor", "major", "blocker")}
                    for f in plan["write"]:
                        if f.get("severity") in written:
                            written[f["severity"]] += 1
                    out["findings_by_severity"] = " ".join(f"{k}={v}" for k, v in written.items())
                    out["plan"] = {"write_count": len(plan["write"]), "omitted": plan["omitted"],
                                   "node_ceiling": plan["node_ceiling"], "not_examined": plan["not_examined"],
                                   "write": plan["write"]}
            else:
                out = evidence(str(a["node"]), str(a.get("key", "")), a.get("line"), a.get("text", ""))
        except Exception as exc:                           # noqa: BLE001
            print(_fail(exc))
            return 2
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return 0
    if args.cmd == "no-document-reply":
        from . import no_document_reply
        try:
            print(no_document_reply(args.reason, args.detail, args.inspect_path))
        except ValueError as exc:
            print(_fail(exc, "invalid-argument"))
            return 2
        return 0
    if args.cmd == "dangles":
        if bool(args.corpus) == bool(args.own_corpus) or (args.own_corpus and args.profile):
            raise UsageError("give exactly one of --corpus and --own-corpus; --own-corpus takes no --profile")
        try:
            from .. import tools
            from ..server import build_tools
            from . import dangle_check
            from . import corpora_containing
            choice = registry.resolve_registry()                       # RegistryError -> a stop, before anything is read
            engine = build_tools(watch=False, require_corpora=False)
            corpus = args.corpus
            if args.own_corpus:
                served = [c for c in corpora_containing(args.document, choice.corpora) if engine.store.get_partition(c) is not None]
                note = "own-corpus lint: not applicable"
                if len(served) > 1:
                    print(json.dumps(_in_band({"ok": False, "explanation": "more than one served corpus has a root containing the "
                                               "document, so its own corpus cannot be decided"}, "own-corpus-ambiguous"), indent=1))
                    return 2
                stop = _failed_container_stop(engine, choice.corpora, args.document)   # AFTER ambiguity (served), BEFORE the file check
                if stop is not None:
                    return stop
                if not _lintable_file(args.document):          # AFTER the registry and the served-corpus checks; BEFORE `not applicable`
                    return _not_a_file()
                pf = (choice.corpora.get(served[0]) or {}).get("profile") if served else None
                if not served or not (pf and os.path.isfile(pf)):
                    why = "extension-in-no-served-corpus" if not served else "own-corpus-has-no-profile"
                    print(json.dumps({"own_corpus": {"applicable": False, "note": note, "why": why}}, indent=1))
                    return 0
                corpus = served[0]
            spec = choice.corpora.get(corpus)
            warnings, profile_source = [], "none"
            if args.profile:
                ppath, origin = args.profile, "argument"
            else:
                ppath, origin = (spec.get("profile") if isinstance(spec, dict) else None), "registry"
            if ppath and os.path.isfile(ppath):
                profile_source = origin
            else:
                if ppath:                                  # named and absent: reported by a FIXED code, never silently dropped
                    warnings.append({"code": "profile-path-not-found", "origin": origin})
                ppath = None

            def call(t, a):
                ans = tools.dispatch(t, a, tools=engine)
                if isinstance(ans, dict) and ans.get("reason") == registry.CODE:
                    raise _reply_registry_error(ans)
                return ans
            part, err = engine._gate(corpus)
            if part is None:
                if isinstance(err, dict) and err.get("reason") == registry.CODE:
                    return _registry_stop(_reply_registry_error(err))
                stop = _corpus_file_stop(err)                  # the reviewed corpus failed to build on a corpus-file fault
                if stop is not None:
                    return stop
                print(json.dumps(_in_band({"ok": False, "explanation": "the engine does not serve this corpus (not registered, "
                                           "not loaded, or not fresh)"}, "corpus-not-served"), indent=1))
                return 2

            stop = _failed_container_stop(engine, choice.corpora, args.document)   # the document's own corpus failed to build
            if stop is not None:
                return stop
            if not _lintable_file(args.document):             # AFTER the registry and the served-corpus checks
                return _not_a_file()
            from factlattice.index_builder import build_alias_index
            # the whole-corpus dangle lint resolves against node ids PLUS the alias index (an id of an alias-form
            # co-definition); the served index drops it, so it is rebuilt by the lint's OWN function, ONCE per call
            aliases = build_alias_index(part.nodes)

            def resolve(target, domain):
                # the engine's OWN resolution: a node of this corpus by Partition.get_node (what lts_node reads); a ref qualified
                # with another corpus's domain by the router that lts_node routes cross-corpus edges through. An ambiguous or
                # unknown domain, a stale target and a missing node all fail to resolve.
                if domain and domain != part.domain_suffix:
                    kind, payload = engine._route({"target": target, "target_domain": domain}, origin_corpus=corpus)
                    if kind == "withheld":
                        return False, UNVERIFIABLE_PRIVATE      # a private corpus answers the same stub for any id
                    if kind == "node":
                        return True, "resolved"
                    return False, str(payload.get("status") or kind)
                return (part.get_node(target) is not None or target in aliases), "not_found"
            out = dangle_check(os.path.realpath(args.document), call, resolve, profile=ppath)
            if args.own_corpus:
                out["own_corpus"] = {"applicable": True}
            out["profile"] = {"used": profile_source != "none", "source": profile_source}
            out["warnings"] = warnings
            if not out["complete"]:
                _in_band(out, "lint-incomplete")
        except RegistryError as exc:
            return _registry_stop(exc)
        except Exception as exc:                           # noqa: BLE001
            print(_fail(exc))
            return 2
        print(json.dumps(out, indent=1))
        return 2 if not out["complete"] else (1 if out["true_dangle_count"] or out["other_error_count"] else 0)
    if args.cmd in ("lint-groups", "frame"):
        try:
            from .. import config, tools
            from ..server import build_tools
            from . import lint_groups, plan_frame
            engine = build_tools(watch=False, require_corpora=False)
            call = lambda t, a: tools.dispatch(t, a, tools=engine)             # noqa: E731
            if args.cmd == "lint-groups":
                out = lint_groups(os.path.realpath(args.document), call)
                if not out["complete"]:
                    _in_band(out, "lint-incomplete")
                print(json.dumps(out, indent=1))
                return 2 if not out["complete"] else (1 if out["errors"] else 0)
            res = call("lts_find", {"corpus": args.corpus, "source_file": args.source_file, "tier": "digest"})
            if res.get("reason") == registry.CODE:
                return _registry_stop(_reply_registry_error(res))
            stop = _corpus_file_stop(res)
            if stop is not None:
                return stop
            if not res.get("success") or not res.get("complete", True):
                print(_fail(str(res.get("error", "")), "frame-read-incomplete"))
                return 2
            spec = registry.resolve_registry().corpora.get(args.corpus) or {}
            roots = [os.path.realpath(r["path"] if isinstance(r, dict) else r) for r in spec.get("roots") or []]
            nodes = []
            for n in res.get("digest") or res.get("nodes") or []:
                n = dict(n)
                sf = n.get("source_file")
                n["real_source"] = next((os.path.realpath(os.path.join(r, sf)) for r in roots
                                         if sf and os.path.exists(os.path.join(r, sf))), None)
                nodes.append(n)
            out = plan_frame(nodes, exclude_real_sources=args.exclude_source)
            print(json.dumps(out, indent=1))
            return 0
        except RegistryError as exc:
            return _registry_stop(exc)
        except Exception as exc:                           # noqa: BLE001
            print(_fail(exc))
            return 2
    if args.cmd == "self-gate":
        try:
            from .. import tools
            from ..server import build_tools
            from . import self_gate
            engine = build_tools(watch=False, require_corpora=False)
            res = self_gate(os.path.realpath(args.file), lambda t, a: tools.dispatch(t, a, tools=engine))
        except RegistryError as exc:
            return _registry_stop(exc)
        except Exception as exc:                           # noqa: BLE001
            print(_fail(exc, passed=False))
            return 2
        if not res["passed"]:
            _in_band(res, "self-gate-failed")
        print(json.dumps(res, indent=1))
        return 0 if res["passed"] else 1
    if args.cmd == "new-key":
        try:
            from . import review_key
            key = review_key(args.corpus)
        except Exception as exc:                           # noqa: BLE001
            print(_fail(exc))
            return 2
        print(key)
        return 0
    if args.cmd == "check-extension-paths":
        from . import check_extension_paths
        try:
            found = check_extension_paths(args.extension, args.document)
        except Exception as exc:                           # noqa: BLE001
            print(_fail(exc))
            return 2
        if not found:
            print(json.dumps({"findings": []}, indent=1))
            return 0
        # the findings hold the FULL paths the caller gave: they are offered only as `terminal_only`, never under a plain name
        out = _in_band({"count": len(found), "kinds": sorted({f["kind"] for f in found}),
                        "terminal_only": {"findings": found}, "terminal_only_note": TERMINAL_NOTE},
                       "extension-is-the-reviewed-document")
        print(json.dumps(out, indent=1))
        return 1
    if args.cmd != "resolve-output":
        raise RuntimeError(f"verb {args.cmd!r} has no branch")        # -> helper-crashed (rc 3), never a resolve-output answer
    try:
        res = _resolve(args.corpus, args.document)
    except RegistryError as exc:
        return _registry_stop(exc)
    except Exception as exc:                               # noqa: BLE001
        print(_fail(exc, success=False))
        return 2
    out = res.to_dict()
    if not res.root:
        out["error_class"] = "InBand"
    print(json.dumps(out, indent=1))
    return 0 if res.root else 1


if __name__ == "__main__":
    sys.exit(main())
