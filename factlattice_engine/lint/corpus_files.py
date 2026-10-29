"""CORPUS-FILE HYGIENE — the defects that live in the FILENAMES, which no parser reads.

Two shapes, both found live in `acme/corpora/acme-specs/` on 2026-08-13, both
invisible to every existing check and to git:

1. NUMERIC-PREFIX COLLISION — `61-config-over-bus.lts` AND `61-fleet-console.lts`;
   `46-console-sources-on-the-bus.lts` AND `46-handover-process.lts`.

   The numeric prefix is this corpus's ORDERING AND CROSS-REFERENCE convention — nodes,
   agent briefs and commit messages say "spec 32", "spec 61". When two files share a
   prefix the reference is AMBIGUOUS, and every consumer resolves it by guessing.

   ⛔ AND GIT WILL NEVER FLAG IT, because the filenames differ. There is no merge
   conflict, no duplicate-path error, no diff. Two authors each add a `61-` on separate
   branches and both land clean. This is the same shape as the property loss above it:
   the defect is in a namespace nothing validates.

   SEVERITY: `error`, and this is the one place in this module that is not a judgement
   call. There is no legitimate reading of a duplicated ordinal — the convention is
   either an identifier or it is decoration, and it is used as an identifier. The fix is
   mechanical (renumber one file) and needs no authoring decision. It is also the ONLY
   rule here that can be satisfied permanently: unlike key hygiene, a corpus that is
   clean of prefix collisions stays clean unless someone introduces one.

   ⚠ IT RED-GATES `acme-specs` TODAY, with exactly two findings. That is the point:
   the check is worthless if it cannot fail, and the corpus it fires on is the corpus
   that has the defect. Demote with `corpus_file_severity={"prefix_collision": "warning"}`
   if a corpus legitimately does not use its prefixes as identifiers.

2. SHADOW COPY — `60-board-hardware-inventory.lts.bak`, a second copy of a LIVE document
   with nothing marking which is authoritative.

   `.bak` is outside the `*.lts` glob, so the linter, the index builder and `lts_find`
   all ignore it — which is exactly why it is dangerous. It is invisible to the tooling
   and fully visible to a human (or an agent) grepping the directory, who has no way to
   tell that it is the stale one. A reader who opens the wrong copy is reading a fork.

   SEVERITY: `warning`. Unlike a prefix collision this one has legitimate variants (a
   deliberate pre-migration snapshot, a working file mid-edit) and the fix is a judgement
   — delete, or move it out of the corpus, or rename it to say what it is. A warning
   naming the file and its live twin is the whole intervention needed.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

ORIGIN = "corpus_files"

KIND_PREFIX_COLLISION = "prefix_collision"
KIND_SHADOW_COPY = "shadow_copy"
KIND_UNMIGRATED_PROSE = "unmigrated_prose"

CORPUS_FILE_SEVERITY: Dict[str, str] = {
    KIND_PREFIX_COLLISION: "error",
    KIND_SHADOW_COPY: "warning",
    KIND_UNMIGRATED_PROSE: "info",
}

_NUMERIC_PREFIX = re.compile(r"^(\d+)[-_]")
# Suffixes that make a file invisible to the `*.lts` glob while leaving it a readable
# copy of a document. NOT a guess: each of these is a real shape found in a corpus tree.
_SHADOW_SUFFIXES = (".bak", ".orig", ".old", ".save", ".copy", ".backup", ".tmp", ".rej")
# A CORPUS-SHAPED `.md` in a corpus root is the same defect wearing a respectable suffix:
# fully readable, fully invisible to the `*.lts` glob, and — unlike a `.bak` — with no
# twin to compare it to, so nothing at all marks it as outside the graph. Measured
# 2026-08-13: of six documents proposing improvements to this engine, TWO were `.md`,
# including the cross-cutting opinion on the whole series, and both were silently absent
# from the corpus that was registered to hold them. Reported separately from `shadow_copy`
# because the remedy differs — a `.bak` should usually be deleted, an `.md` should usually
# be MIGRATED.
_PROSE_SUFFIXES = (".md", ".markdown", ".txt", ".rst")
# Files whose presence in a repository root is ordinary infrastructure, not un-migrated
# corpus content. Named rather than pattern-matched: a check that fires on every README
# is a check that gets switched off in a day.
_PROSE_ALLOWLIST = frozenset({
    "readme", "license", "licence", "contributing", "changelog", "notice",
    "code_of_conduct", "security", "authors", "index", "makefile",
})


@dataclass
class FileFinding:
    kind: str
    message: str
    source_file: Optional[str]
    detail: dict = field(default_factory=dict)


def _corpus_roots(paths: Iterable[Path]) -> Dict[Path, List[Path]]:
    """Group `.lts` files by their IMMEDIATE DIRECTORY.

    ⛔ Per-directory, not per-corpus-tree. `acme-specs/` and `acme-specs/roadmap/`
    each run their own numbering (`00-render-engine-roadmap.lts` lives in `roadmap/` and
    does not collide with `00-` anywhere else), so grouping by the tree root would invent
    collisions the convention does not have. The convention is "the number orders THIS
    directory's listing", and that is what the check measures.
    """
    groups: Dict[Path, List[Path]] = defaultdict(list)
    for p in paths:
        groups[p.parent].append(p)
    return groups


def find_corpus_file_issues(roots: Iterable[str | Path]) -> List[FileFinding]:
    """Filename-level defects under `roots`. Deterministic; directory then name order."""
    lts: List[Path] = []
    shadows: List[Path] = []
    prose: List[Path] = []
    for root in roots:
        rp = Path(root).expanduser()
        if rp.is_dir():
            for p in sorted(rp.rglob("*")):
                if not p.is_file():
                    continue
                if p.suffix == ".lts":
                    lts.append(p)
                elif p.suffix.lower() in _SHADOW_SUFFIXES and ".lts" in p.name:
                    shadows.append(p)
                elif (p.suffix.lower() in _PROSE_SUFFIXES
                      and p.stem.lower() not in _PROSE_ALLOWLIST):
                    prose.append(p)
        elif rp.is_file() and rp.suffix == ".lts":
            lts.append(rp)

    out: List[FileFinding] = []

    for directory, files in sorted(_corpus_roots(lts).items()):
        by_prefix: Dict[str, List[Path]] = defaultdict(list)
        for f in sorted(files):
            m = _NUMERIC_PREFIX.match(f.name)
            if m:
                # `06` and `6` are the SAME ordinal to a human saying "spec 6", so the
                # key is the integer, not the digits. A collision between `06-` and `6-`
                # is the nastiest form of this defect precisely because the strings differ.
                by_prefix[str(int(m.group(1)))].append(f)
        for prefix, group in sorted(by_prefix.items(), key=lambda kv: int(kv[0])):
            if len(group) < 2:
                continue
            names = ", ".join(g.name for g in group)
            out.append(FileFinding(
                kind=KIND_PREFIX_COLLISION,
                message=(f"numeric prefix {prefix!r} is claimed by {len(group)} files in "
                         f"{directory}: {names}. The prefix is this corpus's ordering and "
                         f"cross-reference convention, so \"spec {prefix}\" is ambiguous — "
                         f"and git cannot flag it, because the filenames differ."),
                source_file=str(group[0]),
                detail={"prefix": prefix, "directory": str(directory),
                        "files": [str(g) for g in group]},
            ))

    live = {p.name for p in lts}
    for s in sorted(shadows):
        twin = s.name
        while Path(twin).suffix and not twin.endswith(".lts"):
            twin = Path(twin).stem
        is_live = twin in live
        out.append(FileFinding(
            kind=KIND_SHADOW_COPY,
            message=(f"{s.name} is a second copy of "
                     + (f"the LIVE document {twin}" if is_live else f"{twin}")
                     + ", and nothing marks which is authoritative. It is outside the "
                       "`*.lts` glob, so the linter, the index builder and lts_find all "
                       "ignore it — while a human or an agent grepping this directory "
                       "reads it as content."),
            source_file=str(s),
            detail={"twin": twin, "twin_is_live": is_live},
        ))

    # ⛔ ONLY IN A DIRECTORY THAT ACTUALLY HOLDS `.lts`. Measured live: without this the
    # rule fired 18 times on one `coordination/` subdirectory of agent messages that
    # contains no `.lts` at all — a sibling directory, not a corpus root, and prose there
    # is just prose. A rule that produces twenty findings from one folder of legitimate
    # files is a rule that gets switched off, which costs the two findings that mattered.
    # The claim is narrow and exact: a corpus-shaped `.md` sitting AMONG the documents.
    lts_dirs = {p.parent for p in lts}
    for m in sorted(p for p in prose if p.parent in lts_dirs):
        out.append(FileFinding(
            kind=KIND_UNMIGRATED_PROSE,
            message=(f"{m.name} sits in a corpus root but is not `.lts`, so it is "
                     f"OUTSIDE the graph entirely: no node, no edge, no `lts_find` hit, "
                     f"and — unlike a `.bak` — no live twin to compare it against, so "
                     f"nothing marks it as unqueryable. Either migrate it to `.lts` or "
                     f"move it out of the corpus root. (info-severity: a corpus may "
                     f"legitimately hold prose; what it may not do is hold prose nobody "
                     f"knows is invisible.)"),
            source_file=str(m),
            detail={"suffix": m.suffix.lower(), "directory": str(m.parent)},
        ))

    return out
