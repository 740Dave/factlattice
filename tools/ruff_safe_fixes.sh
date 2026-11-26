#!/usr/bin/env bash
# The ONE command that produced the "ruff safe fixes" change (work item w-57a). Run from anywhere; it works on
# the git tree that contains this script (or on $RUFF_FIX_ROOT when set, used by tools/ruff_safe_fixes_check.sh).
# Safe fixes only (--no-unsafe-fixes), ruff pinned to 0.17.5, every option on the command line, no config file read
# (--isolated), no cache written (--no-cache). C90 has no auto-fixes; it is in the rule set so this is the gate's own set.
set -euo pipefail
ROOT="${RUFF_FIX_ROOT:-$(cd "$(dirname "$0")/.." && pwd -P)}"
cd "$ROOT"
exec uvx ruff@0.17.5 check --isolated --no-cache --fix --no-unsafe-fixes \
  --select E,F,W,I,B,UP,C90 --line-length 140 --exit-zero \
  --exclude case-studies --exclude tests/test_rename_remove_round4.py \
  conftest.py factlattice factlattice_engine tests tools
# Excludes, one reason each:
#   case-studies : the four "specification ambiguity" experiments are published experimental artifacts (code written
#                  by isolated implementers, then scored); rewriting their bytes would alter the recorded results,
#                  and the README tells readers to run them as they are. Not engine code.
#   tests/test_rename_remove_round4.py : its `from tests.test_node_change_ledger import DOC as LG_DOC, _engine, env  # noqa: F401`
#                  imports the pytest fixture `env` only to register it in this module. The isort fix (I001) splits that
#                  line, and ruff then deletes `env` as an unused import (F401 no longer covered by the noqa); 4 tests
#                  (test_the_ledger_failure_warning_is_the_landed_describe_form) then error with "fixture 'env' not found".
#                  Found by the full suite on the first run (6994 passed, 4 errors); the file stays as it was on the base.
# Target list is explicit (not "."): every other tracked .py file lives under the five targets above
# (checked with `git ls-files '*.py'`: case-studies 12, conftest.py 1, factlattice 23, factlattice_engine 77, tests 202, tools 19).
