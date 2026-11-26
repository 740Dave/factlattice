"""The fake GitHub the merge-queue tests run against: GitHub's REST API (request/get/pages) and the git objects the queue fetches, over one state. Shared by
tests/test_merge_queue.py and tests/test_queue_verdict_binding.py (it ships with the second)."""

import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, ".github", "workflows", "scripts"))
import merge_queue as mq  # noqa: E402


class FakeGH:
    """GitHub's REST API (request/get/pages) and the git objects the queue fetches (fetch_base/fetch_head/merge_local), over one state.

    Two assumptions that the review found hidden in the first version of this fake, now modelled (the live rehearsal checks both):
      (a) a branch update pushed with GITHUB_TOKEN starts no check runs: such a head has none (`token_heads`);
      (b) any other writer can move the base at any moment, and the merge API merges into the CURRENT base (the live ruleset has no
          "require up to date" rule yet): `merged_onto` records the base a merge really landed on. `strict` models that rule.
    """

    MAX_CALLS = 3000
    repo = "o/r"  # the repository the queue runs in (Api.repo): a PR whose head repo is another one is a fork head

    def __init__(self):
        self.base = "B0"
        self.prs, self.merged, self.merged_onto, self.comments, self.statuses, self.calls = {}, [], [], [], [], []
        self.merge_bodies, self.token_heads, self.t = [], set(), 0
        self.strict = False
        self.delete_status = 200
        self.merge_status = {}  # PR number -> (status, message) the merge API answers instead of merging
        self.merge_then_status = {}  # PR number -> (status, message): the merge IS applied, then the API answers this (a 500 after GitHub merged)
        self.in_base = set()  # PR heads the base contains (merged by this fake)
        self.history = []  # (merge commit sha, [parents]) of every merge, oldest first
        self.update_conflicts = set()
        self.update_refusals = {}  # PR number -> (status, message) the update-branch API answers instead of updating
        self.sha_pr = {}  # every head sha this fake has seen -> the PR number it was a head of (a force-pushed-away head stays answerable)
        self.compare_heads = []  # the head sha of every GET /compare the queue made, in order
        self.before_merge = None  # called inside PUT /merge, before it is applied: a writer that moves the base in that window
        self.fetch_timeouts = set()  # PR numbers whose git fetch hangs (TimeoutExpired)
        self.dropped = []  # PRs whose merge worktree the queue dropped
        self.commit_trees = {}  # merge commit sha -> its tree sha, as GitHub reports it
        self.tree_override = {}  # PR number -> the tree GitHub's merge actually has (default: the tested one)
        self.merge_ref_reads = []  # PRs whose refs/pull/N/merge the queue read: the queue must never do it (GitHub does not rebuild it)
        self.local_merges = []  # (n, base, head) the queue merged locally
        self.extra_events = {}
        self.dispatches = []  # (workflow file, body) of every POST .../dispatches
        self.jobs, self.jobs_reads = [], []  # the test jobs of the run under test (see get_), and the (run, attempt) of every jobs read
        self.dispatch_status = 204
        self.comment_status = 201  # what POST .../comments answers (a non-2xx posts nothing)
        self.comment_raises = False  # POST .../comments raises SystemExit, as gh_api does on a network error

    def force_push(self, n, new_head, in_base=False):
        """The PR's head becomes `new_head` (a force-push by its author). `in_base`: that sha is already an ancestor of the base."""
        self.sha_pr[new_head] = n
        self.prs[n]["head"] = new_head
        if in_base:
            self.in_base.add(new_head)

    def add(self, n, *, queue=True, draft=False, conflict=False, behind=False):
        self.t += 1
        self.sha_pr[f"H{n}"] = n
        self.prs[n] = {
            "n": n,
            "head": f"H{n}",
            "contains": "B-1" if behind else self.base,
            "merge_ref_base": self.base,  # GitHub built refs/pull/N/merge on this base and does not rebuild it when the base moves
            "labels": {"queue"} if queue else set(),
            "title": f"pr{n}",
            "queued_at": f"2026-11-19T00:00:{self.t:02d}Z",
            "state": "open",
            "draft": draft,
            "conflict": conflict,
        }

    def pr_json(self, p):
        return {
            "number": p["n"],
            "title": p["title"],
            "state": p["state"],
            "draft": p["draft"],
            "created_at": "2026-11-18T00:00:00Z",
            "labels": [{"name": x} for x in sorted(p["labels"])],
            "head": {"sha": p["head"], "repo": {"full_name": p.get("head_repo", self.repo)}},
            "base": {"ref": p.get("base_ref", "main")},
            "mergeable": False if p["conflict"] else True,
        }

    def all_events(self, n):
        p = self.prs[n]
        return self.extra_events.get(n, []) + [{"event": "labeled", "label": {"name": "queue"}, "created_at": p["queued_at"]}]

    # -- git side
    def fetch_base(self, base):
        return self.base

    def fetch_pr(self, n):
        """GitHub's refs/pull/N/merge: STALE after the base moves (a stale merge ref once wedged every queued PR after a merge). The queue must not call this."""
        self.merge_ref_reads.append(n)
        p = self.prs[n]
        if p["conflict"]:
            return p["head"], None
        return p["head"], f"M[{p['head']}+{p['merge_ref_base']}]"

    def fetch_head(self, n):
        if n in self.fetch_timeouts:
            raise subprocess.TimeoutExpired(["git", "fetch"], 600)
        return self.prs[n]["head"]

    def tree_of(self, sha):
        return f"T[{sha}]"

    def drop_merge_tree(self, n):
        self.dropped.append(n)

    def merge_local(self, n, base_sha, head_sha):
        """The queue's own merge of (head onto base): always on the base it was given; None when the PR conflicts."""
        self.local_merges.append((n, base_sha, head_sha))
        return None if self.prs[n]["conflict"] else f"M[{head_sha}+{base_sha}]"

    def parents(self, sha):
        m = re.fullmatch(r"M\[(.*)\+(.*)\]", sha)
        return [m.group(2), m.group(1)]

    # -- REST side
    def request(self, method, path, body=None, **kw):
        self.calls.append((method, path))
        assert len(self.calls) < self.MAX_CALLS, "runaway: the queue made more than MAX_CALLS GitHub calls"
        num = lambda: int(re.search(r"/(?:pulls|issues)/(\d+)", path).group(1))  # noqa: E731
        if method == "GET":
            return self.get_(path, num)
        if method == "POST" and path.endswith("/dispatches"):
            self.dispatches.append((path.split("/")[3], body))
            return self.dispatch_status, None
        if method == "POST" and path.startswith("/statuses/"):
            self.statuses.append((path.split("/")[2], body))
            return 201, {}
        if method == "POST" and path.endswith("/comments"):
            if self.comment_raises:
                raise SystemExit("gh_api: POST comments: network down")
            if self.comment_status in (200, 201):
                self.comments.append((num(), body["body"]))
            return self.comment_status, {}
        if method == "DELETE" and "/labels/queue" in path:
            if self.delete_status == 200:
                self.prs[num()]["labels"].discard("queue")
            return self.delete_status, []
        if method == "PUT" and path.endswith("/update-branch"):
            return self.update_branch(self.prs[num()], body)
        if method == "PUT" and path.endswith("/merge"):
            return self.merge(self.prs[num()], body)
        raise AssertionError(f"FakeGH: unmodelled {method} {path}")

    def get_(self, path, num):
        if path == "":
            return 200, {"default_branch": "main"}
        mj = re.fullmatch(r"/actions/runs/(\d+)/attempts/(\d+)/jobs\?per_page=100&page=(\d+)", path)
        if mj:  # GitHub's record of the jobs of one run attempt: `self.jobs` is [{name, status, conclusion}], set by the test
            self.jobs_reads.append((mj.group(1), mj.group(2)))
            page = int(mj.group(3))
            return 200, {"total_count": len(self.jobs), "jobs": self.jobs[(page - 1) * 100 : page * 100]}
        if path.startswith("/pulls?state=open"):
            return 200, [self.pr_json(p) for p in self.prs.values() if p["state"] == "open"]
        if re.fullmatch(r"/issues/\d+/events(\?.*)?", path):
            return 200, self.all_events(num())[:100]  # the real endpoint returns the FIRST page only
        if re.fullmatch(r"/issues/\d+/comments(\?.*)?", path):
            return 200, [{"body": b} for (n_, b) in self.comments if n_ == num()]
        if path.startswith("/git/commits/"):
            return 200, {"tree": {"sha": self.commit_trees[path.rsplit("/", 1)[1]]}}
        if path.startswith("/git/ref/heads/"):
            return 200, {"object": {"sha": self.base}}
        if re.fullmatch(r"/pulls/\d+", path):
            return 200, self.pr_json(self.prs[num()])
        if path.startswith("/compare/"):
            head = path.split("...")[1]
            self.compare_heads.append(head)
            for q in self.prs.values():  # a test may set a PR's head directly: that sha counts as seen
                self.sha_pr.setdefault(q["head"], q["n"])
            assert head in self.sha_pr, f"FakeGH: compare of a sha it has never seen: {head}"
            p = self.prs[self.sha_pr[head]]  # answers for a head that is no longer the PR's head too
            if head in self.in_base:  # real GitHub (measured against GitHub): the head is an ancestor of the base -> `behind`
                return 200, {"status": "behind", "ahead_by": 0, "behind_by": 1}
            behind = 0 if p["contains"] == self.base else 1
            return 200, {"status": "diverged" if behind else "ahead", "ahead_by": 1, "behind_by": behind}
        if path.startswith("/commits?sha="):  # newest first; a merge commit lists the PR head as a parent
            return 200, [{"sha": s_, "parents": [{"sha": x} for x in ps]} for s_, ps in reversed(self.history)]
        if path.endswith("/check-runs?per_page=100"):
            head = path.split("/")[2]
            return 200, {
                "check_runs": []
                if head in self.token_heads
                else [{"name": n, "status": "completed", "conclusion": "success"} for n in mq.REQUIRED]
            }
        raise AssertionError(f"FakeGH: unmodelled GET {path}")

    def update_branch(self, p, body):
        if p["n"] in self.update_conflicts:
            return 422, {"message": "merge conflict between base and head"}
        if p["n"] in self.update_refusals:
            return self.update_refusals[p["n"]][0], {"message": self.update_refusals[p["n"]][1]}
        assert body["expected_head_sha"] == p["head"]
        p["head"] += "u"
        self.sha_pr[p["head"]] = p["n"]
        p["contains"] = self.base
        p["merge_ref_base"] = self.base  # GitHub rebuilds the merge ref for a new head
        self.token_heads.add(p["head"])  # model (a)
        return 202, {}

    def merge(self, p, body):
        self.merge_bodies.append((p["n"], body))
        if body["sha"] != p["head"]:
            return 409, {"message": "head changed"}
        if p["n"] in self.merge_status:
            return self.merge_status[p["n"]][0], {"message": self.merge_status[p["n"]][1]}
        if self.before_merge:
            self.before_merge()
        if self.strict and p["contains"] != self.base:
            return 405, {"message": "Head branch is out of date"}
        self.merged_onto.append((p["n"], self.base))  # model (b): the merge lands on whatever the base is NOW
        onto, self.base = self.base, f"B{len(self.merged) + 1}"
        self.commit_trees[self.base] = self.tree_override.get(p["n"], f"T[M[{p['head']}+{onto}]]")
        self.in_base.add(p["head"])
        self.history.append((self.base, [onto, p["head"]]))
        if p["n"] in self.merge_then_status:  # applied on GitHub's side, but the answer is an error and the PR is never flipped to merged
            self.merged.append(p["n"])
            return self.merge_then_status[p["n"]][0], {"message": self.merge_then_status[p["n"]][1]}
        p["state"] = "merged"
        self.merged.append(p["n"])
        return 200, {"sha": self.base, "merged": True}

    def get(self, path, **kw):
        return self.request("GET", path)

    def pages(self, path):
        if re.fullmatch(r"/issues/\d+/events(\?.*)?", path):
            return self.all_events(int(re.search(r"/issues/(\d+)", path).group(1)))  # all pages
        return self.request("GET", path)[1]


quiet = lambda *_: None  # noqa: E731
