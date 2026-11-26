"""The required leak check over a pull request's content (run by leak-check.yml, a `pull_request_target` workflow, as BASE-branch code).

WHAT IT READS, AS DATA, THROUGH THE API (the job checks out the base repository only, and nothing here executes or imports anything from the pull request): the PR's title,
body and head branch name; every commit's message and its author and committer (each as `name <email>`); and every changed file's path and contents (the git blobs of the head
commit, read from the head repository). From the event file it reads two values only, `pull_request.number` and `pull_request.head.sha`; the head sha must equal the head the API
reports, both before the commit and file lists are read and again after (cause `head-moved`). The text is never put in a shell command, an environment variable, a file name or a
workflow command: it is decoded and handed to the guard's matcher.

HOW A FILE IS READ. UTF-8 (a byte order mark is dropped) and UTF-16 or UTF-32 with a byte order mark are decoded and scanned line by line. Anything else is the finding
`unscannable-file`, and so is a file the API did not return, a file over MAX_BLOB_BYTES, a compressed file or an archive (recognised by its first bytes), bytes that are not valid
UTF-8, and text holding a NUL byte (which includes UTF-16 with no mark). A binary or an image added by a PR therefore keeps the check red. The scope line counts them.

WHAT IT CHECKS WITH: the rules of tools/disclosure_guard.py of the BASE checkout (its structural rules, the private vocabulary and denylist, and the generated
neutrality terms), loaded by the guard's own readers and run through the guard's own controls (every rule fires on its own control, or the run is refused). The
private rules live in a private overlay repository and are read through the API with ONE read-only token, the secret named OVERLAY_READ_TOKEN of the environment `leak-check`, which only this workflow's one job names (the comment workflow holds its own copy of a secret of the same name in its own environment `comment-containment`).
A rule's name is printed in every finding. A name from an overlay row that is matched by any loaded rule refuses the run (cause `rule-name-unsafe`, with a count and no name);
the fixed names of the built-in and the generated rules are not checked, being text of the base code. A row refused at load is reported without its name.

FAIL CLOSED. The neutrality terms are REQUIRED. The run FAILS (exit 1, a message naming the cause, never a term) when: the secret is absent or empty; the overlay cannot be read
(a refused token, a network error); the vocabulary, the denylist or the terms file is absent, empty, unreadable or has a row the guard refuses; the terms file is older than the
limit this run applies (`--max-terms-age-hours`, 192 in the workflow; the guard's own 6 hours when the option is absent), future-dated, partial, unparsable or fails its sha256;
a rule name is unsafe (above); the guard's controls fail; the event does not name a PR number and head sha; the head moved; the PR cannot be read in full (the API's own caps on
files and commits are not hidden: a count that disagrees with the PR's own count refuses). It never passes because nothing was checked.

THE FIRST LINE is the mode line, printed on every run, a refusal included: `neutrality terms: REQUIRED, sha256 <sha>, generated_at <time>, max age <N> h` once the terms file
is usable and fresh (release_prep's required line, plus the age limit this run applied), `neutrality terms: REQUIRED, REFUSED (<cause>)` otherwise. It never reads NOT REQUIRED.

THE OUTPUT is rule names, counts and places, never a term and never PR text: a finding is `file <path>:<line>  [rule]` (the matched span of the path is masked), `title  [rule]`,
`branch  [rule]`, `body:<line>  [rule]`, `commit <12 hex>:<line>  [rule]` (the message), or `commit <12 hex> author  [rule]` / `commit <12 hex> committer  [rule]`. A path is printed
after any character outside `[A-Za-z0-9._/*-]` is replaced (`*` is the mask), so a file name cannot start a workflow command.

WHAT IT DOES NOT DO, stated once. It is a lexical tripwire over the named vocabulary: a name nobody added passes. It scans each changed file as a WHOLE (the guard's staged mode
scans added lines only), so a PR that touches a file already holding a hit is refused until the hit is gone. It does not read labels or comments (comments are another workflow's
job). KNOWN LIMITS: (1) the rule-name check tests each overlay rule name against the loaded rules' own patterns, so a name that spells a term differently from its pattern (for
example a hyphenated spelling) is not caught and would be printed; overlay rule names must be neutral words. (2) The fork owner's login, which GitHub writes into the message of a
later merge commit ("Merge pull request #N from <owner>/<branch>"), is not scanned; the branch name is.

Inputs, all from the environment the workflow sets: GITHUB_REPOSITORY (this repository), GITHUB_EVENT_PATH (the event's JSON; see above), GITHUB_TOKEN (read only),
OVERLAY_READ_TOKEN (the secret), LC_OVERLAY_REPO (the overlay repository, `owner/name`), GITHUB_API_URL. The guard is read from `tools/disclosure_guard.py` under the current
directory (the base checkout) or `--base-root`.
"""

import argparse
import base64
import codecs
import importlib.util
import json
import math
import os
import pathlib
import re
import sys
import tempfile

import gh_api

OVERLAY_SECRET = "OVERLAY_READ_TOKEN"
OVERLAY_REPO_ENV = "LC_OVERLAY_REPO"
DEFAULT_OVERLAY_REPO = "740Dave/factlattice-deploy"
# The overlay's files (the layout PUBLISHING.md gives the private folder): label -> (path in the overlay, required).
OVERLAY_FILES = {
    "vocabulary": ("private/publish-vocabulary.tsv", True),
    "denylist": ("private/disclosure-denylist.tsv", True),
    "terms": ("private/neutrality-terms.json", True),
    "contexts": ("private/neutrality-term-contexts.json", False),
}
MAX_BLOB_BYTES = 25 * 1024 * 1024
MAX_LISTED = 200
# the mode lines: release_prep's LINE_REQUIRED and its required REFUSED form (a test compares them with that tool's text)
LINE_REQUIRED = "neutrality terms: REQUIRED, sha256 {sha}, generated_at {at}"
# The guard's refusal of a row whose pattern misses its own control reads `rule [<name>] does not match its own control (refused at load)`, and <name> is the overlay author's free
# text: it can hold a `]`. So the match runs up to the guard's FIXED suffix, not to the first `]` (greedy: a name holding the suffix itself still ends at the last one).
REDACT_NAME = re.compile(r"rule \[.*\] does not match its own control")
LINE_REFUSED = "neutrality terms: REQUIRED, REFUSED ({cause})"
LINE_AGE = ", max age {hours} h"  # appended to the REQUIRED line: the age limit this run applied
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA_RE = re.compile(r"[0-9a-f]{40}")
SAFE_PATH = re.compile(r"[^A-Za-z0-9._/*-]")
# Files that are compressed or archives, by their first bytes (a tar's marker is at offset 257): they hold text this check cannot read, so they are findings, never a pass.
COMPRESSED_MAGIC = (
    b"\x1f\x8b",  # gzip
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",  # zip (and docx, jar, ...)
    b"BZh",  # bzip2
    b"\xfd7zXZ\x00",  # xz
    b"\x28\xb5\x2f\xfd",  # zstd
    b"7z\xbc\xaf\x27\x1c",  # 7z
    b"Rar!\x1a\x07",  # rar
    b"\x04\x22\x4d\x18",  # lz4
    b"\x1f\x9d",  # compress
    b"MSCF",  # cab
)


class Refusal(Exception):
    """A named reason to fail. `cause` is the short word of the mode line; the message never holds a term or PR text."""

    def __init__(self, cause, message):
        super().__init__(message)
        self.cause = cause


def load_guard(base_root):
    path = pathlib.Path(base_root) / "tools" / "disclosure_guard.py"
    if not path.is_file():
        raise Refusal("guard-missing", f"the base checkout has no tools/disclosure_guard.py under {base_root}")
    spec = importlib.util.spec_from_file_location("leak_check_guard", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def read_repos(env):
    """-> (repo, overlay repo) from the environment. Refuses on a value that is not owner/name."""
    repo = env.get("GITHUB_REPOSITORY", "")
    if not REPO_RE.fullmatch(repo):
        raise Refusal("bad-input", "GITHUB_REPOSITORY is not owner/name")
    overlay_repo = env.get(OVERLAY_REPO_ENV) or DEFAULT_OVERLAY_REPO
    if not REPO_RE.fullmatch(overlay_repo):
        raise Refusal("bad-input", f"{OVERLAY_REPO_ENV} is not owner/name")
    return repo, overlay_repo


def read_secret(env, name, cause, what):
    """-> the value of the secret `name` in the environment. Refuses, with `cause` and a message that names the secret, when it is absent or empty."""
    secret = env.get(name, "")
    if not secret.strip():
        raise Refusal(cause, f"the secret {name} is not available to this job (absent or empty), so {what}")
    return secret


def read_tokens(env):
    """-> (the read-only token that reads the repository, the overlay secret). Shared by every workflow that reads the overlay's rules."""
    token = env.get("GITHUB_TOKEN", "")
    if not token:
        raise Refusal("no-token", "GITHUB_TOKEN is not set (the read-only token that reads the pull request)")
    return token, read_secret(
        env, OVERLAY_SECRET, "no-overlay-secret", "the private rules cannot be read; the check fails rather than pass without them"
    )


def read_inputs(env):
    """-> (repo, pr number, event head sha, overlay repo, github token, overlay token). Refuses on a malformed value; the overlay secret absent is the cause `no-overlay-secret`."""
    repo, overlay_repo = read_repos(env)
    ev = env.get("GITHUB_EVENT_PATH", "")
    try:
        pr_event = json.loads(pathlib.Path(ev).read_text(encoding="utf-8"))["pull_request"]
        number = pr_event["number"]
        head_sha = pr_event["head"]["sha"]
    except (OSError, ValueError, KeyError, TypeError):
        raise Refusal("bad-input", "GITHUB_EVENT_PATH does not name an event with pull_request.number and pull_request.head.sha") from None
    if not isinstance(head_sha, str) or not SHA_RE.fullmatch(head_sha):
        raise Refusal("bad-input", "the event's pull_request.head.sha is not a 40-hex sha")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise Refusal("bad-input", "the event's pull_request.number is not a positive integer")
    token, secret = read_tokens(env)
    return repo, number, head_sha, overlay_repo, token, secret


def fetch_overlay(api, dest):
    """Write the overlay's files into `dest` (a private temp folder) and return {label: Path or None}. Refuses on any file but the optional contexts table being absent, or
    on any read error (status other than 200; an exception from the client is reported by class only)."""
    out = {}
    for label, (rel, required) in OVERLAY_FILES.items():
        try:
            status, text = api.get(f"/contents/{rel}", accept="application/vnd.github.raw+json", raw=True)
        except (SystemExit, Exception) as exc:  # noqa: BLE001 -- the client raises SystemExit on a network failure; either way the overlay is unreadable
            raise Refusal("overlay-unreadable", f"the overlay's {label} file could not be read ({type(exc).__name__})") from None
        if status == 404 and not required:
            out[label] = None
            continue
        if status == 404:
            raise Refusal("missing", f"the overlay has no {label} file")
        if status != 200:
            raise Refusal("overlay-unreadable", f"the overlay's {label} file could not be read (HTTP {status})")
        target = pathlib.Path(dest) / f"{label}.dat"
        target.write_bytes(text.encode("utf-8") if isinstance(text, str) else b"")
        out[label] = target
    return out


def load_rules(guard, files, base_root):
    """-> (rules, scan_rules, nload, scope fragments). `rules` is every rule (builtin first), for the controls; refuses on anything the publication gate refuses."""
    refusals = []
    states = [
        guard._private_file_state("vocabulary file", "FACTLATTICE_PUBLISH_VOCAB", files["vocabulary"]),
        guard._private_file_state("denylist file", "FACTLATTICE_DISCLOSURE_RULES", files["denylist"]),
    ]
    for st in states:
        refusals += [
            REDACT_NAME.sub("rule [name withheld] does not match its own control", x) for x in st["refusals"]
        ]  # a refused row's name is the author's free text: never printed
    v, d = states
    if v["sha256"] is not None and v["sha256"] == d["sha256"]:
        refusals.append("the vocabulary file and the denylist file have identical content (same sha256)")
    if refusals:
        raise Refusal("rules-unusable", "; ".join(refusals))
    nload = guard.load_neutrality(
        str(files["terms"]), str(files["contexts"]) if files["contexts"] else None, pathlib.Path(files["terms"]).parent
    )
    if nload.state != "loaded":
        cause = nload.terms.cause if nload.terms is not None and not nload.terms.ok else "unusable"
        raise Refusal(
            cause, f"the neutrality terms are unusable ({nload.problem})"
        )  # the guard's problem text names a cause and a field, never a term
    base = list(guard.BUILTIN_RULES) + v["_rules"] + d["_rules"]
    unsafe = unsafe_rule_names(v["_rules"] + d["_rules"], base + nload.rules)
    if unsafe:
        raise Refusal(
            "rule-name-unsafe",
            f"{unsafe} rule name(s) are matched by a loaded rule, so printing a name could print a term; the names are not printed",
        )
    root = pathlib.Path(base_root)
    guard.exempt_shipped_names(nload, guard._tree_files(root), guard.load_excludes(root)[0], root)
    return base, nload, states


def unsafe_rule_names(overlay_rules, all_rules):
    """-> the number of distinct names of OVERLAY rules (vocabulary and denylist rows) that some loaded rule matches. A rule name is printed in every finding, and an overlay author
    writes it freely: a name that holds a term would put the term in a public log. Counted, never listed. The built-in and generated rules' names are fixed text of the base code
    and are not checked (checking them would refuse every run when a generated id equals a word of one of them)."""
    names = {r.name for r in overlay_rules}
    return sum(1 for n in names if any(r.rx.search(n) for r in all_rules))


def decode_text(data):
    """-> the text of a file's bytes, or None when it is not text this check can read: a compressed file or an archive (by magic bytes), bytes that are not valid UTF-8, UTF-8 with
    a byte order mark, UTF-16 or UTF-32 (with a mark), or text holding a NUL byte (UTF-16 with no mark, or a binary)."""
    if data.startswith(COMPRESSED_MAGIC) or data[257:262] == b"ustar":
        return None
    attempts = ["utf-8"]
    if data.startswith(codecs.BOM_UTF8):
        attempts = ["utf-8-sig"]
    elif data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        attempts = ["utf-32", "utf-16"]
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        attempts = ["utf-16"]
    for enc in attempts:
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        return None if "\x00" in text else text
    return None


def decode_blob(blob):
    """-> bytes of a git blob answer, or None when it is not one."""
    if not isinstance(blob, dict) or blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
        return None
    try:
        return base64.b64decode(blob["content"], validate=False)
    except ValueError:
        return None


def paged(api, path):
    try:
        return api.pages(path)
    except (SystemExit, Exception):  # noqa: BLE001 -- a client stop, or an answer that is not a list: the PR cannot be read in full
        raise Refusal("pr-unreadable", "a list of the pull request could not be read in full") from None


def person(side):
    """-> 'name <email>' of a commit's author or committer object as one line of text (an absent or odd object is empty text, which no rule matches)."""
    if not isinstance(side, dict):
        return ""
    return f"{side.get('name') or ''} <{side.get('email') or ''}>".replace("\n", " ")


def collect_pr(api, number, event_head_sha):
    """-> {title, body, branch, commits: [(sha, message, author, committer)], files: [(path, status, blob sha)], head_repo}. Reads only; refuses when the PR cannot be read in full
    or its head is not the head the event named."""
    status, pr = api.get(f"/pulls/{number}")
    if status != 200 or not isinstance(pr, dict):
        raise Refusal("pr-unreadable", f"the pull request could not be read (HTTP {status})")
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    if head.get("sha") != event_head_sha:
        raise Refusal(
            "head-moved", "the pull request's head is not the head the event named (a push landed since); a newer run covers the new head"
        )
    head_repo = (head.get("repo") or {}).get("full_name") or ""
    if not REPO_RE.fullmatch(head_repo):
        raise Refusal("pr-unreadable", "the pull request's head repository is gone or unnamed, so its files cannot be read")
    commits = paged(api, f"/pulls/{number}/commits")
    files = paged(api, f"/pulls/{number}/files")
    if not all(isinstance(c, dict) for c in commits) or not all(isinstance(f, dict) for f in files):
        raise Refusal("pr-unreadable", "a list of the pull request holds an entry that is not an object")
    status2, again = api.get(f"/pulls/{number}")  # the head may have moved while the lists were read: read it again
    head2 = again.get("head") if status2 == 200 and isinstance(again, dict) and isinstance(again.get("head"), dict) else {}
    if head2.get("sha") != event_head_sha:
        raise Refusal(
            "head-moved", "the pull request's head changed while its commits and files were being read; a newer run covers the new head"
        )
    if len(commits) != pr.get("commits"):
        raise Refusal(
            "commits-incomplete", f"read {len(commits)} commit(s) of the {pr.get('commits')} the pull request has (the API caps the list)"
        )
    if len(files) != pr.get("changed_files"):
        raise Refusal(
            "files-incomplete",
            f"read {len(files)} file(s) of the {pr.get('changed_files')} the pull request changes (the API caps the list)",
        )
    return {
        "title": pr.get("title") or "",
        "body": pr.get("body") or "",
        "branch": head.get("ref") or "",
        "commits": [
            (
                c.get("sha", ""),
                (c.get("commit") or {}).get("message") or "",
                person((c.get("commit") or {}).get("author")),
                person((c.get("commit") or {}).get("committer")),
            )
            for c in commits
        ],
        "files": [(f.get("filename", ""), f.get("status", ""), f.get("sha", "")) for f in files],
        "head_repo": head_repo,
    }


def mask(guard, text, rules):
    return SAFE_PATH.sub("?", guard._mask(text, rules))


def scan_text(guard, rules, text):
    """-> [(line number (0 for a name), rule name)] over `text` line by line, with the guard's own predicate."""
    return [(n, r.name) for n, line in enumerate(text.splitlines(), 1) for r in guard._matches(rules, line)]


def read_blob(api_head, blob_sha):
    """-> the file's text, or None when it cannot be had as text (not returned, over the limit, compressed, not UTF-8 or UTF-16)."""
    if not SHA_RE.fullmatch(blob_sha or ""):
        return None
    try:
        st, blob = api_head.get(f"/git/blobs/{blob_sha}")
        data = decode_blob(blob) if st == 200 else None
    except (SystemExit, Exception):  # noqa: BLE001
        return None
    if data is None or len(data) > MAX_BLOB_BYTES:
        return None
    return decode_text(data)


def scan_pr(guard, base, nload, api_head, pr):
    """-> (findings [(kind, ident, line, rule)], files scanned). `base` is the rules without the generated ones; exempt files (the guard's own) are scanned with the non-builtin rules.
    Scanned: the title, body and head branch name, each commit's message, author and committer, each changed file's path and text."""
    rules = base + nload.rules
    non_builtin = [r for r in rules if r.source != "builtin"]
    out = []
    for _n, rule in scan_text(guard, rules, pr["title"]):
        out.append(("title", "", 0, rule))
    for _n, rule in scan_text(guard, rules, pr["branch"]):
        out.append(("branch", "", 0, rule))
    for n, rule in scan_text(guard, rules, pr["body"]):
        out.append(("body", "", n, rule))
    for sha, message, author, committer in pr["commits"]:
        ident = sha[:12] if SHA_RE.fullmatch(sha) else "unknown"
        out += [("commit", ident, n, rule) for n, rule in scan_text(guard, rules, message)]
        out += [("commit", f"{ident} author", 0, rule) for _n, rule in scan_text(guard, rules, author)]
        out += [("commit", f"{ident} committer", 0, rule) for _n, rule in scan_text(guard, rules, committer)]
    scanned = 0
    for path, status, blob_sha in pr["files"]:
        if status == "removed":
            continue
        shown = mask(guard, path, rules)
        use = non_builtin if guard._is_exempt(path) else rules
        out += [("file", shown, 0, r.name) for r in guard._matches(use, path)]
        scanned += 1
        text = read_blob(api_head, blob_sha)
        if text is None:
            out.append(("file", shown, 0, "unscannable-file"))
            continue
        out += [("file", shown, n, rule) for n, rule in scan_text(guard, use, text)]
    return out, scanned


def rules_scope(states, nload, rule_count):
    """-> the part of a scope line that names the rules in force (counts and digests, never a term). Shared by every workflow that applies the overlay's rules."""

    def priv(label, st):
        return f"{label}: rules={st['rules']} rows={st['rows']} sha256={st['sha256']}"

    t = nload.terms
    return (
        f"{priv('vocabulary', states[0])}; {priv('denylist', states[1])}; "
        f"neutrality-terms: agent-ids={len(t.agent_ids)} corpus-ids={len(t.corpus_ids)} sha256={t.sha256} generated_at={t.generated_at}; rules-total={rule_count}"
    )


def scope_line(pr, scanned, unscannable, states, nload, rule_count):
    return (
        f"leak-check scope: files-changed={len(pr['files'])} files-scanned={scanned} files-unscannable={unscannable} commits={len(pr['commits'])} "
        f"title=1 body=1 branch=1 commit-identities={2 * len(pr['commits'])}; " + rules_scope(states, nload, rule_count)
    )


def report(guard, findings):
    by_rule = {}
    for i, (kind, ident, line, rule) in enumerate(findings):
        label = {
            "title": "title",
            "branch": "branch",
            "body": f"body:{line}",
            "commit": f"commit {ident}" + (f":{line}" if "author" not in ident and "committer" not in ident else ""),
            "file": f"file {ident}:{line}",
        }[kind]
        if i < MAX_LISTED:
            print(f"  {label}  [{rule}]")
        by_rule[rule] = by_rule.get(rule, 0) + 1
    if len(findings) > MAX_LISTED:
        print(f"  ... {len(findings) - MAX_LISTED} more finding(s) not listed (the counts below cover all)")
    print(
        f"RESULT: RED. {len(findings)} finding(s). Per rule: {dict(sorted(by_rule.items()))}. The matched text is not printed. {guard.TRIPWIRE}"
    )


def check_age(hours):
    """-> the limit to apply in seconds, or None for the guard's own. Refuses a limit that is not a positive finite number."""
    if hours is None:
        return None
    if not isinstance(hours, (int, float)) or isinstance(hours, bool) or not math.isfinite(hours) or hours <= 0:
        raise Refusal("bad-input", "--max-terms-age-hours must be a positive, finite number")
    return int(hours * 3600)


def shown_hours(seconds):
    hours = seconds / 3600
    return str(int(hours)) if hours == int(hours) else f"{hours:g}"


def clear_guard_env():
    """The guard reads os.environ itself (not the dict a run was given): clear its variables there, spelled out one by one."""
    os.environ.pop("FACTLATTICE_DISCLOSURE_RULES", None)
    os.environ.pop("FACTLATTICE_PUBLISH_VOCAB", None)
    os.environ.pop("FACTLATTICE_NEUTRALITY_TERMS", None)
    os.environ.pop("FACTLATTICE_NEUTRALITY_CONTEXTS", None)


class Mode:
    """Whether the mode line has been printed yet: a refusal prints it (as REFUSED) only if the run had not already printed the REQUIRED form."""

    printed = False


def refuse(r, mode, who="leak-check", subject="pull request"):
    """-> 1, after printing the refusal (the mode line first, when not yet printed). The message never holds a term or the text of what was read. `who` and `subject` name
    the workflow and the thing it read, for a second workflow that uses this machinery."""
    if not mode.printed:
        print(LINE_REFUSED.format(cause=r.cause))
    print(f"⛔ {who} refused: {r} ({r.cause}). The check FAILS; nothing was cleared. The text of the {subject} is not printed.")
    print(f"::error::{who} refused ({r.cause})")
    return 1


def load_overlay_rules(guard, overlay_repo, secret, base_url, base_root, age_seconds, mode):
    """Read the overlay with the one read-only token, load every rule, run the guard's controls and print the mode line (then `mode.printed` is set). -> (base rules, loaded
    neutrality terms, the vocabulary's and denylist's states). Used by every workflow that applies the overlay's rules; refuses on anything the publication gate refuses."""
    if age_seconds is not None:
        guard.NEUTRALITY_MAX_AGE_SECONDS = (
            age_seconds  # this run's own copy of the guard: the guard's 6 h default is unchanged for local use
        )
    with tempfile.TemporaryDirectory(prefix="lc-overlay-") as tmp:
        os.chmod(tmp, 0o700)
        files = fetch_overlay(gh_api.Api(overlay_repo, token=secret, base=base_url), tmp)
        base, nload, states = load_rules(guard, files, base_root)
        failures = guard._control_failures(base + nload.rules, echo=None, reveal=False)
        if failures:
            raise Refusal("controls-failed", "; ".join(f"control: {x}" for x in failures))
        print(
            LINE_REQUIRED.format(sha=nload.terms.sha256, at=nload.terms.generated_at)
            + LINE_AGE.format(hours=shown_hours(guard.NEUTRALITY_MAX_AGE_SECONDS))
        )
        mode.printed = True
        print(guard.exempt_note(nload))
        print(guard.corpus_exempt_note(nload))
    return base, nload, states


def run(env, base_root, max_terms_age_hours=None):
    """-> exit code. Prints the mode line first, always."""
    mode = Mode()
    try:
        clear_guard_env()
        age_seconds = check_age(max_terms_age_hours)
        repo, number, event_head, overlay_repo, token, secret = read_inputs(env)
        guard = load_guard(base_root)
        base_url = env.get("GITHUB_API_URL") or gh_api.API
        base, nload, states = load_overlay_rules(guard, overlay_repo, secret, base_url, base_root, age_seconds, mode)
        rules = base + nload.rules
        api = gh_api.Api(repo, token=token, base=base_url)
        pr = collect_pr(api, number, event_head)
        head_api = api if pr["head_repo"] == repo else gh_api.Api(pr["head_repo"], token=token, base=base_url)
        findings, scanned = scan_pr(guard, base, nload, head_api, pr)
    except Refusal as r:
        return refuse(r, mode)
    except SystemExit as exc:  # gh_api's loud stops (no token, a network failure): the message is the client's, never PR text
        return refuse(Refusal("api-failure", str(exc)[:200]), mode)
    unscannable = sum(1 for f in findings if f[3] == "unscannable-file")
    print(scope_line(pr, scanned, unscannable, states, nload, len(rules)))
    if findings:
        report(guard, findings)
        print(f"::error::leak-check: {len(findings)} finding(s); see the rule names above")
        return 1
    print(
        "RESULT: green. No rule of the named vocabulary matched in the title, body, head branch name, commit messages and identities or changed files. This is a lexical tripwire over the named vocabulary: a name nobody added passes untouched."
    )
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--base-root",
        default=os.getcwd(),
        help="the base checkout (default: the current directory); tools/disclosure_guard.py is read from it",
    )
    ap.add_argument(
        "--max-terms-age-hours",
        type=float,
        default=None,
        help="the oldest the neutrality terms may be, in hours (default: the guard's own limit, 6 h); the mode line prints the limit applied",
    )
    args = ap.parse_args(argv)
    return run(os.environ, args.base_root, args.max_terms_age_hours)


if __name__ == "__main__":
    sys.exit(main())
