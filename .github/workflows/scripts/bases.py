"""The base branches the merge queue serves: the ONE list. plan_bases.py (the queue's plan job) reads it, and so does any script that
labels pull requests by base.

The workflows have no `branches:` filter (a workflow trigger cannot read this file); the queue skips any other base.
To add or remove a base, edit this tuple only. (The scripts are a CODEOWNERS full path through /.github/workflows/.)
"""

QUEUE_BASES = ("main",)  # a rehearsal adds its own base here, on its own branch
