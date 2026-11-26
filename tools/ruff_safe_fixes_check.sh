#!/usr/bin/env bash
# Proves a "ruff safe fixes" change is mechanical (work item w-57a).
#   usage: tools/ruff_safe_fixes_check.sh BASE HEAD [EXTRA_PATH ...]
# BASE, HEAD : git revisions in the repo the script runs in. HEAD may instead be a DIRECTORY holding a tree
#              (used to test the check itself on a deliberately altered copy).
# It exports BASE into a scratch dir, copies tools/ruff_safe_fixes.sh and this script FROM HEAD into it (plus any
# EXTRA_PATH, e.g. a CI workflow file that the PR adds), runs the script there, then compares that tree with HEAD's
# tree using `git diff --no-index --exit-code`. Exit 0 only if identical; 1 if they differ; 2 on a usage or setup error.
# Scratch dirs are made under $RUFF_CHECK_SCRATCH (default: mktemp -d) and are left in place; their paths are printed.
set -euo pipefail
[ $# -ge 2 ] || { echo "usage: $0 BASE HEAD [EXTRA_PATH ...]" >&2; exit 2; }
BASE="$1"; HEADREF="$2"; shift 2
REPO="$(git rev-parse --show-toplevel)"
W="${RUFF_CHECK_SCRATCH:-$(mktemp -d)}"
mkdir -p "$W"
A="$W/head-tree"; B="$W/base-tree-fixed"
mkdir "$A" "$B"
if [ -d "$HEADREF" ]; then
  cp -R "$HEADREF"/. "$A"/
else
  git -C "$REPO" archive "$HEADREF" | tar -x -C "$A"
fi
git -C "$REPO" archive "$BASE" | tar -x -C "$B"
for p in tools/ruff_safe_fixes.sh tools/ruff_safe_fixes_check.sh "$@"; do
  [ -e "$A/$p" ] || { echo "HEAD has no $p" >&2; exit 2; }
  mkdir -p "$B/$(dirname "$p")"; cp -p "$A/$p" "$B/$p"
done
RUFF_FIX_ROOT="$B" bash "$A/tools/ruff_safe_fixes.sh" > "$W/ruff-output.txt" 2>&1 || { echo "ruff script failed; see $W/ruff-output.txt" >&2; exit 2; }
# Compare. Tool runs leave no cache (--no-cache); ignore __pycache__ only if present.
if git diff --no-index --exit-code --stat "$B" "$A" > "$W/diff-stat.txt"; then
  echo "IDENTICAL: re-running tools/ruff_safe_fixes.sh on $BASE gives exactly the tree of $HEADREF ($(find "$A" -type f | wc -l | tr -d ' ') files compared)"
  echo "scratch: $W"
  exit 0
fi
echo "DIFFERENT: re-running tools/ruff_safe_fixes.sh on $BASE does NOT give the tree of $HEADREF"
tail -5 "$W/diff-stat.txt"
echo "scratch: $W"
exit 1
