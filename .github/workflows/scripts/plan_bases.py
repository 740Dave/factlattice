#!/usr/bin/env python3
"""merge-queue-plan (read-only): which bases have queued PRs now, and which PRs, oldest first?

Writes to $GITHUB_OUTPUT:  matrix={"include":[{"base": B, "pr": N, "head": SHA}]}   (one cell per PLANNED CANDIDATE; the cell's job is named from pr and head)
                            candidates={B: [{n, head}]}                      (JSON; the merge job binds every verdict and every job to this table)
                            has_work=true|false
                            forks=[N, ...]  has_forks=true|false              (queued PRs whose head is in another repository: never planned, dequeued by the merge job)
                            has_drafts=true|false                            (an open draft carries `queue`: the merge job flags it, SDLC 7.6 CI-QUEUE-FLAGS-QUEUED-DRAFT)
The test job's displayed name is an expression over the cell (`merge-queue-test (pr-<n> <head>)`), so GitHub does not append the matrix values to it; the merge
job finds each cell's job by that name (the jobs API returns names, not matrix values) and decides from the job's conclusion.
Only the bases in bases.QUEUE_BASES (bases.py, the one list) are served; a base with nothing queued gets no cell.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bases import QUEUE_BASES  # noqa: E402
from gh_api import Api  # noqa: E402
from merge_queue import MAX_CANDIDATES, queued_drafts, queued_forks, queued_prs  # noqa: E402


def plan(api, bases):
    """-> [{"base": B, "candidates": [{"n": N, "head": SHA}]}] for every base with a queued PR (at most MAX_CANDIDATES each)."""
    out = []
    for b in bases:
        prs = queued_prs(api, b)[:MAX_CANDIDATES]
        if prs:
            out.append({"base": b, "candidates": [{"n": p["number"], "head": p["head"]["sha"]} for p in prs]})
    return out


def matrix_of(cells):
    return {"include": [{"base": c["base"], "pr": x["n"], "head": x["head"]} for c in cells for x in c["candidates"]]}


def candidates_of(cells):
    return {c["base"]: c["candidates"] for c in cells}


def base_is_served(base):
    return base in QUEUE_BASES


def has_drafts(api, bases):
    return any(queued_drafts(api, b) for b in bases)


def plan_forks(api, bases):
    """Queued PRs whose head is not in this repository, by number: never planned, so their code never runs on the queue's runner (the merge job dequeues them)."""
    return sorted(n for b in bases for n in queued_forks(api, b))


def write_outputs(cells, drafts=False, forks=()):
    with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
        fh.write(f"matrix={json.dumps(matrix_of(cells))}\ncandidates={json.dumps(candidates_of(cells))}\n")
        fh.write(f"has_work={'true' if cells else 'false'}\n")
        fh.write(f"has_drafts={'true' if drafts else 'false'}\n")
        fh.write(f"forks={json.dumps(sorted(forks))}\nhas_forks={'true' if forks else 'false'}\n")


def main(argv):
    """`--pr-base B` is the base of the PR of a pull_request_target (label) event: a base that is not served ends the run here, with
    no API call and no work. A schedule or dispatch run has no such base and sweeps every served base."""
    repo = argv[argv.index("--repo") + 1]
    pr_base = (argv[argv.index("--pr-base") + 1] if "--pr-base" in argv else "") or None  # empty: not a label event
    if pr_base is not None and not base_is_served(pr_base):
        print(f"the label event is on base {pr_base!r}, which the queue does not serve (bases.py): nothing planned")
        write_outputs([])
        return
    api = Api(repo)
    cells = plan(api, list(QUEUE_BASES))
    print(f"queued: {[(c['base'], [x['n'] for x in c['candidates']]) for c in cells]}")
    try:  # never fatal (SDLC CI-QUEUE-FLAGS-QUEUED-DRAFT): a failed draft listing must not lose the sweep
        drafts = has_drafts(api, list(QUEUE_BASES))
    except (Exception, SystemExit) as exc:  # gh_api.pages raises SystemExit on an API error
        print(f"::warning::could not list queued drafts ({type(exc).__name__}); planning without the draft flag")
        drafts = False
    if drafts:
        print("a draft PR carries `queue`: the merge job flags it (it is never planned)")
    forks = plan_forks(api, list(QUEUE_BASES))
    if forks:
        print(f"queued PRs whose head is not in this repository (never planned; the merge job dequeues them): {forks}")
    write_outputs(cells, drafts, forks)


if __name__ == "__main__":
    main(sys.argv[1:])
