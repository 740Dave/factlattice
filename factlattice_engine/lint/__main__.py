"""``python3 -m factlattice_engine.lint`` -- the CLI adapter over the
invocation-agnostic `core.lint()`. Deterministic, no LLM, ASCII.

    python3 -m factlattice_engine.lint lint <path-or-glob> [<path-or-glob> ...] \\
        [--dangle] [--no-structure] [--no-coverage] [--no-profile-rules] \\
        [--profile PATH] [--json]
    python3 -m factlattice_engine.lint --selftest

`lint` is the default subcommand and may be omitted:
    python3 -m factlattice_engine.lint path/to/corpus --dangle
"""
from __future__ import annotations

import argparse
import glob as glob_mod
import json
import os
import sys
from typing import List, Optional, Tuple

from factlattice import CorpusFileError, list_lts_files

from .core import lint

# Convenience-only CLI default: an optional domain profile discovered relative to the
# current working directory when --profile is omitted. Overridable via the env var so
# no deployment-specific path is baked into this generic library. When neither is set,
# `core.lint()` falls back to factlattice's DEFAULT_PROFILE.
_DEFAULT_PROFILE_HINT = os.environ.get("LTS_LINT_DEFAULT_PROFILE") or None

_GLOB_CHARS = set("*?[")


def _has_glob_chars(s: str) -> bool:
    return any(c in s for c in _GLOB_CHARS)


def _expand_dir(d: str) -> List[str]:
    return [os.path.join(d, rel) for rel in list_lts_files(d)]


def resolve_targets(raw_paths: List[str]) -> Tuple[Optional[str], Optional[List[str]]]:
    """Normalize CLI path/glob args into a `factlattice.parse()`-shaped call:
    `(source_dir, None)` for the single-bare-directory fast path (full recursive
    corpus, matching what `build_index`/lts-engine would ingest -- the RIGHT tool for
    "is the whole corpus dangle-free"), else `(None, file_list)` for explicit
    file-set mode (see `core.lint`'s CAVEAT on partial-corpus dangling).
    """
    if len(raw_paths) == 1 and not _has_glob_chars(raw_paths[0]) and os.path.isdir(raw_paths[0]):
        return os.path.abspath(raw_paths[0]), None

    files: List[str] = []
    seen = set()
    for raw in raw_paths:
        matches = sorted(glob_mod.glob(raw, recursive=True)) if _has_glob_chars(raw) else [raw]
        if not matches:
            print(f"lts_lint: no match for {raw!r}", file=sys.stderr)
            continue
        for m in matches:
            if os.path.isdir(m):
                candidates = _expand_dir(m)
            elif os.path.isfile(m):
                candidates = [m]
            else:
                print(f"lts_lint: not a file or directory: {m!r}", file=sys.stderr)
                continue
            for c in candidates:
                ac = os.path.abspath(c)
                if ac not in seen:
                    seen.add(ac)
                    files.append(ac)
    return None, files


def _default_profile() -> Optional[str]:
    hint = _DEFAULT_PROFILE_HINT
    return hint if hint and os.path.isfile(hint) else None


def main(argv=None) -> int:
    """The CLI door. A corpus member that is a FIFO/socket/device, a broken link or unreadable
    ([ISSUE-NONREGULAR-MEMBER]) is a PRECISE code and the root-relative path on stderr, exit 2 --
    never a raw traceback."""
    try:
        return _main(argv)
    except CorpusFileError as exc:
        print(f"lts_lint: {exc.code}: {exc.rel_path}", file=sys.stderr)
        return 2


def _main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        from .selftest import run
        return run()

    # "lint" is the only subcommand and is optional (bare paths default to it);
    # strip it manually rather than via argparse `choices` on a positional, which
    # cannot coexist with a following variadic `paths` positional.
    if argv and argv[0] == "lint":
        argv = argv[1:]

    ap = argparse.ArgumentParser(
        prog="python -m factlattice_engine.lint",
        description="Deterministic FactLattice (.lts) linter -- format/structure "
                    "diagnostics + bidirectional dangle-lint, consuming the shared "
                    "`factlattice` parser (lints-clean == ingests-clean).",
    )
    ap.add_argument("paths", nargs="*", help="a .lts file, a directory, or a glob "
                    "(repeatable); a single bare directory is linted recursively "
                    "as ONE corpus (full cross-file dangle resolution)")
    ap.add_argument("--dangle", action="store_true",
                    help="run the bidirectional dangle-lint (forward + orphan)")
    ap.add_argument("--no-structure", action="store_true",
                    help="skip the generic format/structure diagnostics (with "
                        "--dangle alone, only forward/orphan findings print)")
    ap.add_argument("--no-coverage", action="store_true",
                    help="skip the LINE-ACCOUNTING invariant (on by default): every "
                        "non-blank line is consumed by a parser branch or classified "
                        "ignorable; anything else is authored content the structured "
                        "view dropped in silence")
    ap.add_argument("--no-profile-rules", action="store_true",
                    help="skip PROFILE-DRIVEN VALIDATION V1-V11 (on by default): "
                        "registered prefixes, document tags, required/recommended "
                        "properties, enum values, decision-lifecycle stages, "
                        "confidence blocks, invalidating_assumption, required "
                        "frontmatter, density. Inert when the corpus loads no profile")
    ap.add_argument("--no-key-loss", action="store_true",
                    help="skip the KEY-LOSS rule (on by default): an authored top-level "
                        "bullet in a node body that MINTED NO PROPERTY, whose shape says "
                        "the author expected one. Warnings only; the summary counter "
                        "prints on every run, including a clean one")
    ap.add_argument("--no-corpus-files", action="store_true",
                    help="skip CORPUS-FILE HYGIENE (on by default): two .lts files in one "
                        "directory sharing a numeric prefix (an ERROR — git cannot flag "
                        "it, because the filenames differ), and .bak/.orig shadow copies "
                        "of live documents")
    ap.add_argument("--profile", default=None,
                    help="domain profile (.yaml/.json path); default: "
                        "$LTS_LINT_DEFAULT_PROFILE if set and present, "
                        "else factlattice's DEFAULT_PROFILE (only 'core' registered)")
    ap.add_argument("--json", action="store_true", help="emit the machine report")
    args = ap.parse_args(argv)

    if not args.paths:
        ap.error("give at least one path or glob to lint")

    source, paths = resolve_targets(args.paths)
    profile = args.profile if args.profile is not None else _default_profile()

    result = lint(source, paths=paths, profile=profile,
                  structure=not args.no_structure, dangle=args.dangle,
                  coverage=not args.no_coverage,
                  profile_rules=not args.no_profile_rules,
                  key_loss=not args.no_key_loss,
                  corpus_files=not args.no_corpus_files)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(result.render_text())

    return result.exit_code()


if __name__ == "__main__":
    sys.exit(main())
