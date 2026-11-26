"""Detect, and contain, a comment, a review or an issue that names what the public repository must not name (run by comment-containment.yml, three jobs, three subcommands).

SCOPE, STATED PLAINLY. This is detect-and-contain, not prevention: a comment is public the moment it posts, so for an issue comment or an issue the exposure lasts seconds, not zero
(the run starts with the event). A pull-request review or review comment is NOT covered by an event: it stays public until the next scheduled sweep (every 10 minutes, plus whatever
delay GitHub puts on a scheduled run, which can be many minutes), so for those the exposure is minutes, not seconds. Anyone who read the page in that time, and any copy taken in it,
is outside what this can undo. It is the same lexical tripwire as the pull-request check (scripts/leak_check.py, whose rule loading, controls, matcher call and refusal printing it
imports and does not copy): a name nobody added to the vocabulary passes.

TWO WAYS IN. (1) EVENT: `issue_comment` and `issues` (the workflow's own header says why only these): the event names one object by id, and that object is scanned. (2) SWEEP: the
`schedule` event: the run lists the pull-request reviews and review comments of every pull request updated in the last LOOKBACK_HOURS (24), and the repository's review comments updated in that
time, and scans each. The look-back is a fixed window, not a high-water mark kept anywhere: it needs no state and no write by the read-only job, and an object seen twice is scanned twice,
which is harmless because the minimise job first asks whether the object is already minimised and skips it, and an alert is keyed (below). The price: a maintainer who restores a minimised
comment that was a false hit will see it minimised again by a sweep, for as long as the object stays in the window and its text is unchanged. The sweep reads the default branch's code only and
nothing of any pull request. WHAT THE WINDOW COVERS, AND WHAT IT DOES NOT: gaps of up to 24 hours between sweeps are covered (a delayed or lost run is covered by a later one inside the window),
and nothing is claimed beyond that. GAP CHECK: the detect job reads this workflow's previous SUCCESSFUL scheduled run (Actions API, the job's own token, `actions: read` in that job only);
if there is none, or it started more than GAP_HOURS (23) ago, or the answer cannot be read, the run records the gap (`none`, `older` or `unreadable`) and the alert job opens ONE alert
`sweep gap` (label `cc-sweep-gap`, skipped while an open one exists): a gap longer than the window is alerted, not covered. PAGE CAPS: pull requests are read MAX_PR_PAGES (5) pages of 100,
every other list at most 50 pages; a list cut by its cap is read as far as it goes, what was read is scanned, minimised and alerted as usual, and the run then reports `sweep-incomplete`
(a refusal alert, and the alert job fails the run). What remains unseen: an edit to a review or review comment on a pull request that was not updated within 24 hours, unless the repository's
own list of review comments shows it (a comment's own `updated_at` moving on an edit was not measured), and anything in the part of a list beyond a page cap. Every review of a pull request in
the window is re-read, which is where an edit of a review lands (that an edit moves its pull request's `updated_at` was not measured either).

THREE SUBCOMMANDS, THREE JOBS, EACH HOLDING ONLY ITS OWN SECRET.
  detect    scans the object or objects and writes nothing to GitHub (it reads: the objects, and with its read-only token whether a hit's object is already minimised). Secret: OVERLAY_READ_TOKEN (read only, this workflow's own copy of it, in this workflow's own environment). It exits
            0 on a hit (the hit is its result, passed to the next jobs through the job outputs) and 1 only when it could not do its work; on that refusal it also writes the output
            `refused` (the cause code) so that the alert job can report it.
  minimise  hides each hit through the GraphQL mutation `minimizeComment`, classifier OFF_TOPIC (the API also offers OUTDATED, among others), after asking whether the object is already minimised and skipping it if so. Secret: COMMENT_MINIMISE_TOKEN (write).
            It tries every hit and fails if any failed. An issue cannot be minimised by the API (the mutation's subjects are comments and reviews, and an issue is neither): for an
            issue this job fails on purpose and says so, so the run is red and the alert says the issue is still public.
  alert     opens issues in the private overlay repository. Secret: OVERLAY_ALERT_TOKEN (meant to write to that repository only; the workflow cannot enforce a token's scope). One alert holds
            the rule names and counts, the kind and id of the object, its public address, whether the minimise job succeeded and the run's address; never the matched text, the text or a term.

ALERTS ARE KEYED AND CAPPED. (a) One alert per object, rule set and current text: each alert carries the label `cc-` plus 20 hex digits of the SHA-256 of `<node id>|<rule names>|<SHA-256 of the object's text>` (the
text itself is in no alert and no output), and before opening one the job asks the overlay for any issue (open or closed) with that label and opens nothing if there is one. So a sweep that
sees the same hit again, or an edit that leaves the text as it was, opens nothing new; a new leak in an already-alerted object changes the text, so it is a new key and alerts again
(a closed alert does not silence it); so does a hit by another rule set. While an OPEN alert holds the same object and rule set (label `cc-o-` plus 20 hex digits of the SHA-256 of
`<node id>|<rule names>`, which every alert carries beside its key), a new text adds a COMMENT to that alert instead of opening another (the rule names, the count and the first 12 hex digits of the new
text's digest, never text) and puts the new text's key on the alert, so an edit loop on one object is one issue and a comment per distinct text; a closed alert is not commented on. Each comment counts toward the cap. If the lookup itself fails the alert is opened anyway (a duplicate is better than a missed alert). The label is created by
GitHub when the issue is created with it, which assumes the alert token may write to the overlay. (b) At most MAX_ALERTS (10) alerts are opened or commented on per run; hits beyond that, and hits beyond the
MAX_HITS (50) a run carries from the scan to the later jobs, are counted in ONE summary alert of that run (label `cc-summary-<run id>`). Every carried hit is still minimised;
those beyond MAX_HITS are only counted, not minimised. The carry is spent on objects that need work: the detect job asks (GraphQL read, its read-only token) which hits are already minimised and
carries the others first, so persistent minimised hits do not crowd out new ones; the summary counts only live objects the carry cut. The consequence, stated: an already-minimised hit that is cut
by MAX_HITS is neither minimised nor alerted again, and one that is carried is alerted as usual.
(c) A scan that was REFUSED (the cause code is in the `refused` output) or did not run at all (the job's result is not success) opens ONE alert, `scan refused (<cause>)`, with the label
`cc-refused-<cause>`; it is skipped while an OPEN issue with that label exists, so a refusal that persists is one open alert, and a new one follows a closed alert if the cause returns.

BUILT-IN PATH RULES ARE NOT APPLIED, AND THE OVERLAY MUST NAME OUR OWN. The guard's generic built-in rules (an absolute home path on macOS or Linux) are skipped when scanning comments, reviews and
issues: a contributor who pastes a traceback is not a leak, and the rule would minimise it and open an alert. Because that leaves our own paths to the overlay, the scan REFUSES with the cause
`own-paths-missing` (and the refusal alert opens) unless the overlay's vocabulary holds at least one rule whose name starts with `own-path-` (the maintainers' usernames and host and volume names).
The check proves a row exists, not that it is complete. The rows need NEUTRAL names (`own-path-1`, `own-path-2`, ...): a row named after the name it guards makes a finding print that name, which the
rule-name check (`rule-name-unsafe`, shared with the leak check) refuses, so the scan would not run at all. Every overlay rule (the private vocabulary and denylist) and every generated-id rule still applies. The pull-request
check still applies the built-in rules to files, where they belong.

WHAT IT READS, AS DATA, THROUGH THE API. In event mode it reads ids only from the event file: `comment.id` (an issue comment) or `issue.number` (an issue), each a positive integer, and the runner's
event name. The text it scans is read from the API by that id, never from the event, so an `edited` event scans the text as it is now; in sweep mode it reads the lists above. What is scanned: a
comment's or review's body; an issue's title and body, each by the guard's own matcher, line by line. Nothing of the text is put in a shell command, an environment variable, a file name or a
workflow command: it stays in memory, and the later jobs are handed, in one JSON list, for each hit its kind, numeric id, node id, rule names, count and address, each checked against a fixed
shape by the job that receives it. Of the text, only rule names and counts are ever printed or written, never a term and never the text itself.

HOW IT FAILS CLOSED. Exit 1, a message naming the cause and never a term, in every one of these cases: the secret of the job is absent or empty (`no-overlay-secret`, `no-minimise-secret`,
`no-alert-secret`); the overlay cannot be read or its rules are unusable (the causes of leak_check.py, with its mode line REQUIRED, REFUSED); the event does not name the object (`bad-input`);
an object cannot be read, is gone, has no usable id or has text that is not text (`object-unreadable`); the overlay vocabulary has no `own-path-` rule (`own-paths-missing`); a write is refused or does not do what it
says (the GraphQL answer must report the object minimised and carry no errors; the issue must be created). A run that fails to scan minimises nothing, opens the one refusal alert and is red. The one exception is a sweep list cut by a page cap (`sweep-incomplete`): detect exits 0 with what it read,
the hits in it are minimised and alerted, the refusal alert opens, and the alert job then fails the run.
Text the check cannot read is never green: text holding a NUL character, or longer than a million characters, is the finding `unscannable-text`, which counts as a hit.

WHAT IT DOES NOT SEE (the code has no path for any of these). (1) Text that was edited away before the run read it: GitHub keeps an edit history that this check does not read, and a run that
starts after an edit sees only the new text; the history of a minimised comment is still there. (2) A label (no check reads labels), and a pull request's title, commits and files (the
pull-request check's). (3) A comment's author, a reaction, an attachment or an image's content. (4) A hit in text this workflow was never started for: an event that does not start it, and, for
a review, anything outside the sweep's window or beyond its page cap. (5) A commit comment or a discussion. Minimising collapses a comment and does not delete it: any reader can still expand it
and its edit history stays; whether to delete it is a maintainer's decision, which the alert asks for.

Inputs, all from the environment the workflow sets: GITHUB_REPOSITORY, GITHUB_EVENT_NAME, GITHUB_EVENT_PATH (event mode), GITHUB_OUTPUT, GITHUB_API_URL (and GITHUB_GRAPHQL_URL for the mutation,
which defaults to GITHUB_API_URL plus /graphql), GITHUB_TOKEN (read only), the job's own secret, LC_OVERLAY_REPO (the overlay repository), and for the later jobs CC_HITS, CC_OVERFLOW,
CC_REFUSED, CC_GAP, CC_DETECT_RESULT and CC_MINIMISE_RESULT, which are the earlier jobs' outputs and results. The guard is read from `tools/disclosure_guard.py` under the current directory (the
base checkout, the default branch) or `--base-root`.
"""

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import sys

import gh_api
import leak_check
from leak_check import Refusal

MINIMISE_SECRET = "COMMENT_MINIMISE_TOKEN"
ALERT_SECRET = "OVERLAY_ALERT_TOKEN"
CLASSIFIER = "OFF_TOPIC"
KINDS = ("issue-comment", "review-comment", "review", "issue")
NODE_RE = re.compile(r"[A-Za-z0-9_=-]{4,200}")
SAFE_RULE = re.compile(r"[A-Za-z0-9._-]{1,64}")
RULES_RE = re.compile(r"[A-Za-z0-9._-]{1,64}(,[A-Za-z0-9._-]{1,64})*")
COUNT_RE = re.compile(r"[1-9][0-9]{0,8}")
ID_RE = re.compile(r"[1-9][0-9]{0,19}")
RUN_ID_RE = re.compile(r"[0-9]{1,20}")
SERVER_RE = re.compile(r"https://[A-Za-z0-9.-]+")
CAUSE_RE = re.compile(r"[a-z][a-z-]{0,39}")
OVERFLOW_RE = re.compile(r"[0-9]{1,9}")
RESULTS = ("success", "failure", "cancelled", "skipped")
HIT_KEYS = ("kind", "id", "node", "rules", "count", "url", "digest")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
GAP_RE = re.compile(r"[a-z]{0,10}")
MAX_TEXT = 1_000_000
MAX_HITS = 50
MAX_ALERTS = 10
LOOKBACK_HOURS = 24
GAP_HOURS = 23
OWN_PATH_PREFIX = "own-path-"
WORKFLOW_FILE = "comment-containment.yml"
MAX_PR_PAGES = 5
UNNAMED = "unnamed-rule"
UNSCANNABLE = "unscannable-text"
QUERY = "query($id: ID!) { node(id: $id) { ... on Minimizable { isMinimized } } }"
MUTATION = (
    "mutation($id: ID!, $classifier: ReportedContentClassifiers!) "
    "{ minimizeComment(input: {subjectId: $id, classifier: $classifier}) { minimizedComment { isMinimized minimizedReason } } }"
)


# ---------------------------------------------------------------------------------------------------------------------------------- the event
def positive_id(event, *keys):
    """-> the integer at event[keys...], which must be a positive integer (not a bool). Refuses with `bad-input` otherwise."""
    node = event
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            raise Refusal("bad-input", f"the event does not hold {'.'.join(keys)}")
        node = node[k]
    if not isinstance(node, int) or isinstance(node, bool) or node < 1:
        raise Refusal("bad-input", f"the event's {'.'.join(keys)} is not a positive integer")
    return node


def locate(name, event):
    """-> (kind, id, the API path of the object, relative to the repository) for the two events that name one object."""
    if name == "issue_comment":
        n = positive_id(event, "comment", "id")
        return "issue-comment", n, f"/issues/comments/{n}"
    if name == "issues":
        n = positive_id(event, "issue", "number")
        return "issue", n, f"/issues/{n}"
    raise Refusal("bad-input", "the event is not one this workflow handles by id (reviews are the sweep's)")


def read_event(env):
    try:
        event = json.loads(pathlib.Path(env.get("GITHUB_EVENT_PATH", "")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refusal("bad-input", "GITHUB_EVENT_PATH does not name a readable JSON event") from None
    return locate(env.get("GITHUB_EVENT_NAME", ""), event)


def url_shape(repo):
    return re.compile(rf"https://github\.com/{re.escape(repo)}/(issues|pull)/[0-9]+(#[A-Za-z0-9_-]+)?")


def make_object(obj, kind, ident, repo):
    """-> {kind, id, node, url, title, body} from an API object, or refuses with `object-unreadable` (no usable node id, or text that is not text)."""
    if not isinstance(obj, dict):
        raise Refusal("object-unreadable", "the object is not a JSON object")
    node, body, title = obj.get("node_id"), obj.get("body"), obj.get("title") if kind == "issue" else None
    if not isinstance(node, str) or not NODE_RE.fullmatch(node):
        raise Refusal("object-unreadable", "the object has no usable node id")
    if any(v is not None and not isinstance(v, str) for v in (body, title)):
        raise Refusal("object-unreadable", "the object's text is not text")
    url = obj.get("html_url")
    shown = url if isinstance(url, str) and url_shape(repo).fullmatch(url) else ""
    return {"kind": kind, "id": ident, "node": node, "url": shown, "title": title, "body": body or ""}


def fetch_object(api, path, kind, ident, repo):
    """-> the object named by an event. Refuses with `object-unreadable` when it cannot be read."""
    try:
        status, obj = api.get(path)
    except (SystemExit, Exception):  # noqa: BLE001 -- a client stop (network): the object cannot be read
        raise Refusal("object-unreadable", "the object could not be read (the API did not answer)") from None
    if status != 200:
        raise Refusal("object-unreadable", f"the object could not be read (HTTP {status})")
    return make_object(obj, kind, ident, repo)


# ---------------------------------------------------------------------------------------------------------------------------------- the sweep
def since_stamp(now=None):
    now = now or datetime.datetime.now(datetime.UTC)
    return (now - datetime.timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_list(api, path):
    """-> one page of a list endpoint. Refuses with `object-unreadable` on an error, a non-200 status or an answer that is not a list."""
    try:
        status, data = api.get(path)
    except (SystemExit, Exception):  # noqa: BLE001
        raise Refusal("object-unreadable", "a list could not be read (the API did not answer)") from None
    if status != 200 or not isinstance(data, list):
        raise Refusal("object-unreadable", f"a list could not be read (HTTP {status})")
    return data


def recent_pull_numbers(api, since):
    """-> (the numbers of the pull requests updated since `since`, newest first; True when MAX_PR_PAGES pages were all still inside the window, so the list stops short)."""
    out = []
    for page in range(1, MAX_PR_PAGES + 1):
        items = get_list(api, f"/pulls?state=all&sort=updated&direction=desc&per_page=100&page={page}")
        for p in items:
            if not isinstance(p, dict) or not isinstance(p.get("updated_at"), str) or not isinstance(p.get("number"), int):
                raise Refusal("object-unreadable", "a pull request entry is not what the API documents")
            if p["updated_at"] < since:
                return out, False
            out.append(p["number"])
        if len(items) < 100:
            return out, False
    return out, True


def all_pages(api, path):
    """-> (every entry of a list endpoint, True when the list had more than 50 pages and was cut)."""
    out = []
    sep = "&" if "?" in path else "?"
    for page in range(1, 51):
        items = get_list(api, f"{path}{sep}per_page=100&page={page}")
        out.extend(items)
        if len(items) < 100:
            return out, False
    return out, True


def sweep_objects(api, repo, since):
    """-> (the scan objects, "" or `sweep-incomplete`). The objects are every review and every review comment of each pull request updated since `since`, and every review comment
    updated since then (kind, id, node, address, text), deduplicated by node id. A list that hit its page cap is read as far as it goes and the cause is returned beside what was read."""
    found, seen, cut = [], set(), False
    numbers, cut = recent_pull_numbers(api, since)
    for number in numbers:
        for kind, path in (("review", f"/pulls/{number}/reviews"), ("review-comment", f"/pulls/{number}/comments")):
            items, c = all_pages(api, path)
            cut = cut or c
            found += [(kind, x) for x in items]
    items, c = all_pages(api, f"/pulls/comments?sort=updated&direction=desc&since={since}")
    cut = cut or c
    found += [("review-comment", x) for x in items]
    out = []
    for kind, raw in found:
        ident = raw.get("id") if isinstance(raw, dict) else None
        if not isinstance(ident, int) or isinstance(ident, bool) or ident < 1:
            raise Refusal("object-unreadable", "a list entry has no usable id")
        obj = make_object(raw, kind, ident, repo)
        if obj["node"] not in seen:
            seen.add(obj["node"])
            out.append(obj)
    return out, ("sweep-incomplete" if cut else "")


def check_gap(api, now=None):
    """-> "" when this workflow's previous SUCCESSFUL scheduled run started within GAP_HOURS, else `none` (no such run), `older` (older than that) or `unreadable` (the answer could not be
    read: the gap cannot be shown to be absent). A gap longer than the look-back means reviews edited in it may never have been scanned."""
    try:
        status, data = api.get(f"/actions/workflows/{WORKFLOW_FILE}/runs?event=schedule&status=success&per_page=1")
    except (SystemExit, Exception):  # noqa: BLE001
        return "unreadable"
    runs = data.get("workflow_runs") if status == 200 and isinstance(data, dict) else None
    if not isinstance(runs, list):
        return "unreadable"
    if not runs:
        return "none"
    stamp = runs[0].get("run_started_at") or runs[0].get("created_at") if isinstance(runs[0], dict) else None
    try:
        started = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
    except (TypeError, ValueError):
        return "unreadable"
    now = now or datetime.datetime.now(datetime.UTC)
    return "older" if now - started > datetime.timedelta(hours=GAP_HOURS) else ""


# ---------------------------------------------------------------------------------------------------------------------------------- detect
def safe_rule(name):
    return name if SAFE_RULE.fullmatch(name) else UNNAMED


def scan_object(guard, rules, obj):
    """-> ([(label, line, rule name)], the text parts) over the object's text, each part by the guard's own matcher. Text holding a NUL or too long is the finding `unscannable-text`."""
    parts = [("body", obj["body"])] if obj["title"] is None else [("title", obj["title"] or ""), ("body", obj["body"])]
    out = []
    for label, text in parts:
        if "\x00" in text or len(text) > MAX_TEXT:
            out.append((label, 0, UNSCANNABLE))
        if len(text) <= MAX_TEXT:
            out += [(label, n, safe_rule(rule)) for n, rule in leak_check.scan_text(guard, rules, text)]
    return out, parts


def comment_rules(rules):
    """The rules that apply to a comment, a review or an issue: the generated-id rules and every overlay rule, without the guard's generic built-in path rules."""
    return [r for r in rules if r.source != "builtin"]


def text_digest(obj):
    """-> the SHA-256 of the object's current text (title and body). It goes into the alert key and nowhere else, and the text itself is in no output."""
    text = json.dumps([obj["title"], obj["body"]], ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def hit_record(obj, findings):
    return {
        "kind": obj["kind"],
        "id": obj["id"],
        "node": obj["node"],
        "rules": ",".join(sorted({f[2] for f in findings})),
        "count": len(findings),
        "url": obj["url"],
        "digest": text_digest(obj),
    }


MAX_LISTED = 200


def report(guard, results):
    """Print each finding as `<kind> #<id> <place>  [rule]` and the result line. -> (total findings, per-rule counts)."""
    by_rule, total = {}, 0
    for obj, findings in results:
        for label, line, rule in findings:
            if total < MAX_LISTED:
                place = "title" if label == "title" else f"{label}:{line}"
                print(f"  {obj['kind']} #{obj['id']} {place}  [{rule}]")
            total += 1
            by_rule[rule] = by_rule.get(rule, 0) + 1
    if total > MAX_LISTED:
        print(f"  ... {total - MAX_LISTED} more finding(s) not listed (the counts below cover all)")
    print(
        f"RESULT: HIT. {total} finding(s) in {len(results)} object(s). Per rule: {dict(sorted(by_rule.items()))}. The matched text is not printed. {guard.TRIPWIRE}"
    )
    print(f"::warning::comment-containment: {total} finding(s) in {len(results)} object(s); see the rule names above")


def write_outputs(path, values):
    """Append `key=value` lines to the job's outputs file. Every value has a fixed shape without a newline (checked by the caller's constants; re-checked here)."""
    if any("\n" in str(v) or "\r" in str(v) for v in values.values()):
        raise Refusal("bad-input", "an output value holds a line break")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.writelines(f"{k}={v}\n" for k, v in values.items())
    except OSError:
        raise Refusal("bad-input", "GITHUB_OUTPUT cannot be written") from None


def read_detect_inputs(env):
    repo, overlay_repo = leak_check.read_repos(env)
    sweep = env.get("GITHUB_EVENT_NAME", "") == "schedule"
    target = None if sweep else read_event(env)
    out_path = env.get("GITHUB_OUTPUT", "")
    if not out_path:
        raise Refusal("bad-input", "GITHUB_OUTPUT is not set (the next job's input)")
    token, secret = leak_check.read_tokens(env)
    return repo, overlay_repo, target, token, secret, out_path


def note_refusal(env, r):
    """Best effort: record the cause code where the alert job reads it. The code is a fixed word, never text of the object."""
    path = env.get("GITHUB_OUTPUT", "")
    cause = r.cause if CAUSE_RE.fullmatch(r.cause or "") else "unknown"
    if path:
        try:
            write_outputs(path, {"hit": "false", "hits": "[]", "overflow": 0, "refused": cause, "gap": ""})
        except Refusal:
            pass


def graphql_url(env):
    return env.get("GITHUB_GRAPHQL_URL") or f"{env.get('GITHUB_API_URL') or gh_api.API}/graphql"


def live_first(hits, minimised):
    """-> (the hits with those that still need minimising first (stable), how many of those are live). A hit whose object is already minimised needs no minimise call, so it must not take a carry slot
    from one that does. An issue cannot be minimised, so it is always live."""
    live = [h for h in hits if not minimised(h)]
    done = [h for h in hits if h not in live]
    return live + done, len(live)


def collect_objects(env, repo, token, base_url, target):
    api = gh_api.Api(repo, token=token, base=base_url)
    if target is None:
        objects, incomplete = sweep_objects(api, repo, since_stamp())
        return "sweep", objects, incomplete, check_gap(api)
    kind, ident, path = target
    return "event", [fetch_object(api, path, kind, ident, repo)], "", ""


def detect(env, base_root, max_terms_age_hours=None):
    """-> exit code. Prints the mode line first, always."""
    mode = leak_check.Mode()
    try:
        leak_check.clear_guard_env()
        age_seconds = leak_check.check_age(max_terms_age_hours)
        repo, overlay_repo, target, token, secret, out_path = read_detect_inputs(env)
        guard = leak_check.load_guard(base_root)
        base_url = env.get("GITHUB_API_URL") or gh_api.API
        base, nload, states = leak_check.load_overlay_rules(guard, overlay_repo, secret, base_url, base_root, age_seconds, mode)
        if not any(r.name.startswith(OWN_PATH_PREFIX) for r in states[0]["_rules"]):
            raise Refusal(
                "own-paths-missing",
                f"the overlay vocabulary has no rule named `{OWN_PATH_PREFIX}...` (the maintainers' usernames and host and volume names), and the generic path rules are not applied to comments",
            )
        rules = comment_rules(base + nload.rules)
        how, objects, incomplete, gap = collect_objects(env, repo, token, base_url, target)
        results = []
        lines = parts = 0
        for obj in objects:
            findings, texts = scan_object(guard, rules, obj)
            parts += len(texts)
            lines += sum(len(text.splitlines()) for _label, text in texts)
            if findings:
                results.append((obj, findings))
        print(
            f"comment-containment scope: mode={how} objects={len(objects)} text-parts={parts} lines={lines} builtin-path-rules=skipped; "
            + leak_check.rules_scope(states, nload, len(rules))
        )
        hits = [hit_record(o, f) for o, f in results]
        read_api = gh_api.Api(repo, token=token, base=base_url)
        gql = graphql_url(env)
        hits, live = live_first(hits, lambda h: h["kind"] != "issue" and already_minimised(read_api, gql, h["node"]))
        write_outputs(
            out_path,
            {
                "hit": "true" if hits else "false",
                "hits": json.dumps(hits[:MAX_HITS], separators=(",", ":")),
                "overflow": max(0, live - MAX_HITS),
                "refused": incomplete,
                "gap": gap,
            },
        )
    except Refusal as r:
        note_refusal(env, r)
        return leak_check.refuse(r, mode, "comment-containment", "comment")
    except SystemExit as exc:  # gh_api's loud stops: the message is the client's, never the text
        r = Refusal("api-failure", str(exc)[:200])
        note_refusal(env, r)
        return leak_check.refuse(r, mode, "comment-containment", "comment")
    if gap:
        print(
            f"::warning::comment-containment: the previous successful scheduled run is missing or older than {GAP_HOURS} hours ({gap}); the alert job reports the gap"
        )
    if incomplete:
        print(
            f"::warning::comment-containment: the sweep hit a page cap ({incomplete}); the objects it read were scanned, and the alert job reports the refusal and fails the run"
        )
    if results:
        report(guard, results)
    else:
        print(
            f"RESULT: clean. No rule of the named vocabulary matched in the {len(objects)} object(s) scanned. This is a lexical tripwire over the named vocabulary: a name nobody added passes untouched."
        )
    return 0


# ---------------------------------------------------------------------------------------------------------------------------------- the later jobs
def stop(job, r):
    """-> 1 after printing a refusal of a later job (no mode line: these jobs do not read the rules)."""
    print(
        f"⛔ comment-containment {job} refused: {r} ({r.cause}). The job FAILS; nothing was changed by this step beyond what is said above."
    )
    print(f"::error::comment-containment {job} refused ({r.cause})")
    return 1


def parse_hits(env, allow_empty):
    """-> the list of hits the scan handed on, each checked against a fixed shape. Refuses with `bad-input` on anything else."""
    raw = env.get("CC_HITS", "")
    if not raw and allow_empty:
        return []
    try:
        hits = json.loads(raw)
    except ValueError:
        raise Refusal("bad-input", "CC_HITS is not JSON") from None
    if not isinstance(hits, list) or len(hits) > MAX_HITS or (not hits and not allow_empty):
        raise Refusal("bad-input", "CC_HITS is not a list of one to MAX_HITS hits")
    for h in hits:
        check_hit(h, env.get("GITHUB_REPOSITORY", ""))
    return hits


def check_hit(h, repo):
    if not isinstance(h, dict) or sorted(h) != sorted(HIT_KEYS):
        raise Refusal("bad-input", "a hit does not have exactly the fields the scan writes")
    ok = (
        h["kind"] in KINDS
        and isinstance(h["id"], int)
        and not isinstance(h["id"], bool)
        and ID_RE.fullmatch(str(h["id"]))
        and isinstance(h["node"], str)
        and NODE_RE.fullmatch(h["node"])
        and isinstance(h["rules"], str)
        and RULES_RE.fullmatch(h["rules"])
        and isinstance(h["count"], int)
        and not isinstance(h["count"], bool)
        and COUNT_RE.fullmatch(str(h["count"]))
        and isinstance(h["url"], str)
        and (h["url"] == "" or url_shape(repo).fullmatch(h["url"]))
        and isinstance(h["digest"], str)
        and DIGEST_RE.fullmatch(h["digest"])
    )
    if not ok:
        raise Refusal("bad-input", "a hit field is not of the shape the scan writes")


def already_minimised(api, url, node):
    """True when the API says the object is already minimised (nothing to do: the sweep meets the same hit again for the whole look-back). Any doubt, or a failed read, is False."""
    try:
        status, data = api.request("POST", url, body={"query": QUERY, "variables": {"id": node}})
    except (SystemExit, Exception):  # noqa: BLE001
        return False
    try:
        return status == 200 and not data.get("errors") and data["data"]["node"]["isMinimized"] is True
    except (KeyError, TypeError, AttributeError):
        return False


def minimise_one(api, url, hit):
    if hit["kind"] == "issue":
        raise Refusal(
            "not-minimisable", "an issue cannot be minimised by the API, so it stays public until a maintainer acts (the alert says so)"
        )
    if already_minimised(api, url, hit["node"]):
        return
    status, data = api.request("POST", url, body={"query": MUTATION, "variables": {"id": hit["node"], "classifier": CLASSIFIER}})
    confirm_minimised(status, data)


def minimise(env):
    """-> exit code. Hides every hit the scan handed on; tries all of them and fails if any failed."""
    try:
        repo, _overlay = leak_check.read_repos(env)
        token = leak_check.read_secret(
            env, MINIMISE_SECRET, "no-minimise-secret", "the object cannot be minimised; the job fails rather than leave it public silently"
        )
        hits = parse_hits(env, allow_empty=False)
    except Refusal as r:
        return stop("minimise", r)
    base_url = env.get("GITHUB_API_URL") or gh_api.API
    url = graphql_url(env)
    api = gh_api.Api(repo, token=token, base=base_url)
    first, done = None, 0
    for hit in hits:
        try:
            minimise_one(api, url, hit)
            done += 1
        except Refusal as r:
            first = first or r
        except SystemExit as exc:
            first = first or Refusal("api-failure", str(exc)[:200])
    if first is not None:
        print(f"comment-containment minimise: {done} of {len(hits)} hit(s) minimised")
        return stop("minimise", first)
    print(f"comment-containment minimise: {done} object(s) minimised ({CLASSIFIER})")
    return 0


def confirm_minimised(status, data):
    """Refuses unless the answer is HTTP 200, carries no errors, and reports the object minimised. Nothing of the answer is printed."""
    if status != 200 or not isinstance(data, dict):
        raise Refusal("minimise-refused", f"the mutation was refused (HTTP {status})")
    if data.get("errors"):
        raise Refusal("minimise-refused", "the mutation's answer reports errors (not printed)")
    try:
        done = data["data"]["minimizeComment"]["minimizedComment"]["isMinimized"]
    except (KeyError, TypeError):
        done = None
    if done is not True:
        raise Refusal("minimise-refused", "the answer does not report the object minimised")


def alert_key(hit):
    """-> the label that keys an alert to its object, its rule set and the digest of its current text: a new leak in an already-alerted object is a new key, the same text again is not."""
    return "cc-" + hashlib.sha256(f"{hit['node']}|{hit['rules']}|{hit['digest']}".encode()).hexdigest()[:20]


def run_line(repo, env):
    run_id, server = env.get("GITHUB_RUN_ID", ""), env.get("GITHUB_SERVER_URL", "")
    if RUN_ID_RE.fullmatch(run_id) and SERVER_RE.fullmatch(server):
        return f"Run: {server}/{repo}/actions/runs/{run_id}"
    return None


def hit_alert(repo, hit, result, env):
    """-> (title, body) of the alert for one hit. Rule names and counts only."""
    where = f"{hit['kind']} #{hit['id']} in {repo}" + (f": {hit['url']}" if hit["url"] else "")
    lines = [
        f"Rules: {hit['rules'].replace(',', ', ')}",
        f"Findings: {hit['count']}",
        f"Where: {where}",
        f"Minimised: {'yes' if result == 'success' else 'no'} (the minimise job's result: {result})",
    ]
    if hit["kind"] == "issue":
        lines.append("An issue cannot be minimised by the API: it was not minimised, it is still public and needs a maintainer.")
    lines += [x for x in (run_line(repo, env),) if x]
    lines.append("The matched text is not repeated here.")
    return f"comment-containment: {hit['kind']} #{hit['id']} hit in {repo}", "\n".join(lines)


def find_alert(api, label, state):
    """-> the number of an issue of the overlay (in `state`: open or all) carrying this label, or None. A lookup that fails is None: a duplicate is better than a missed alert."""
    try:
        status, data = api.get(f"/issues?labels={label}&state={state}&per_page=1")
    except (SystemExit, Exception):  # noqa: BLE001
        return None
    if status == 200 and isinstance(data, list) and data and isinstance(data[0], dict) and isinstance(data[0].get("number"), int):
        return data[0]["number"]
    return None


def already_alerted(api, label, state):
    return find_alert(api, label, state) is not None


def object_key(hit):
    """-> the label that names an object and its rule set without its text: the open alert of an object is found by it."""
    return "cc-o-" + hashlib.sha256(f"{hit['node']}|{hit['rules']}".encode()).hexdigest()[:20]


def open_alert(api, labels, title, body):
    status, data = api.request("POST", "/issues", body={"title": title, "body": body, "labels": labels})
    if status != 201 or not isinstance(data, dict) or not isinstance(data.get("number"), int):
        raise Refusal("alert-refused", f"the alert was not created (HTTP {status})")
    return data["number"]


def comment_alert(api, number, key, body):
    """Add a comment to an open alert and put the text's own key on it, so the same text is not commented again."""
    status, _ = api.request("POST", f"/issues/{number}/comments", body={"body": body})
    if status != 201:
        raise Refusal("alert-refused", f"the comment was not added (HTTP {status})")
    status, _ = api.request("POST", f"/issues/{number}/labels", body={"labels": [key]})
    if status != 200:
        raise Refusal("alert-refused", f"the alert's label was not added (HTTP {status})")


def hit_comment(repo, hit, env):
    """-> the text of a comment on an open alert: rule names, the count and the first 12 hex digits of the new text's digest, never text."""
    lines = [
        f"Rules: {hit['rules'].replace(',', ', ')}",
        f"Findings: {hit['count']}",
        f"Text digest: {hit['digest'][:12]}",
        "The same object, the same rules, a new text.",
    ] + [x for x in (run_line(repo, env),) if x]
    return "\n".join(lines)


def read_alert_inputs(env):
    repo, overlay_repo = leak_check.read_repos(env)
    token = leak_check.read_secret(
        env, ALERT_SECRET, "no-alert-secret", "the alert cannot be opened; the job fails rather than let a hit pass unreported"
    )
    hits = parse_hits(env, allow_empty=True)
    result, detect_result = env.get("CC_MINIMISE_RESULT", ""), env.get("CC_DETECT_RESULT", "")
    refused, overflow = env.get("CC_REFUSED", ""), env.get("CC_OVERFLOW", "") or "0"
    gap = env.get("CC_GAP", "")
    if not GAP_RE.fullmatch(gap):
        raise Refusal("bad-input", "CC_GAP is not a gap word")
    if result not in RESULTS or detect_result not in RESULTS:
        raise Refusal("bad-input", "CC_MINIMISE_RESULT or CC_DETECT_RESULT is not a job result")
    if refused and not CAUSE_RE.fullmatch(refused):
        raise Refusal("bad-input", "CC_REFUSED is not a cause code")
    if not OVERFLOW_RE.fullmatch(overflow):
        raise Refusal("bad-input", "CC_OVERFLOW is not a count")
    return repo, overlay_repo, token, hits, result, detect_result, refused, int(overflow), gap


def hit_jobs(api, repo, hits, result, env, overflow):
    """-> (the actions for the hits, how many were already alerted, the overflow count with the hits beyond MAX_ALERTS added). An action is ("open", labels, title, body) or ("comment", issue number, key, body).
    A hit whose exact key (object, rules, text) is held is skipped; else if an OPEN alert holds the object's label, a comment goes on it; else a new alert opens. Each action counts toward MAX_ALERTS."""
    jobs, skipped = [], 0
    for hit in hits:
        key = alert_key(hit)
        if already_alerted(api, key, "all"):
            skipped += 1
        elif len(jobs) >= MAX_ALERTS:
            overflow += 1
        elif (number := find_alert(api, object_key(hit), "open")) is not None:
            jobs.append(("comment", number, key, hit_comment(repo, hit, env)))
        else:
            jobs.append(("open", [key, object_key(hit)], *hit_alert(repo, hit, result, env)))
    return jobs, skipped, overflow


def status_jobs(api, repo, env, detect_result, refused, gap, overflow):
    """-> the alerts for a refused or never-run scan, a sweep gap and the overflow summary, each skipped when its key is already held."""
    jobs = []
    if detect_result != "success" or refused:
        cause = refused or "scan-did-not-run"
        if not already_alerted(api, f"cc-refused-{cause}", "open"):
            lines = [
                f"Cause: {cause}",
                "The scan did not complete, so no object of this run was minimised or alerted unless this run says otherwise; the run is red.",
                run_line(repo, env) or "",
            ]
            jobs.append(("open", [f"cc-refused-{cause}"], f"comment-containment: scan refused ({cause})", "\n".join(x for x in lines if x)))
    if gap and not already_alerted(api, "cc-sweep-gap", "open"):
        lines = [
            f"Gap: {gap}",
            f"This workflow's previous successful scheduled run is missing, unreadable or older than {GAP_HOURS} hours, and the sweep looks back {LOOKBACK_HOURS} hours: "
            "reviews and review comments edited in an uncovered stretch may never have been scanned.",
            run_line(repo, env) or "",
        ]
        jobs.append(("open", ["cc-sweep-gap"], f"comment-containment: sweep gap ({gap})", "\n".join(x for x in lines if x)))
    label = f"cc-summary-{env.get('GITHUB_RUN_ID', '0')}"
    if overflow and RUN_ID_RE.fullmatch(env.get("GITHUB_RUN_ID", "")) and not already_alerted(api, label, "all"):
        jobs.append(
            (
                "open",
                [label],
                f"comment-containment: {overflow} more hit(s) not alerted individually",
                f"Count: {overflow}\nThe matched text is not repeated here.",
            )
        )
    return jobs


def alert(env):
    """-> exit code. Opens the alerts: one per new hit (at most MAX_ALERTS), one for a refused scan, one for a gap, one summary for the rest. Tries all; fails if any failed."""
    try:
        repo, overlay_repo, token, hits, result, detect_result, refused, overflow, gap = read_alert_inputs(env)
    except Refusal as r:
        return stop("alert", r)
    api = gh_api.Api(overlay_repo, token=token, base=env.get("GITHUB_API_URL") or gh_api.API)
    jobs, skipped, overflow = hit_jobs(api, repo, hits, result, env, overflow)
    jobs += status_jobs(api, repo, env, detect_result, refused, gap, overflow)
    first, made = None, 0
    for action in jobs:
        try:
            if action[0] == "open":
                open_alert(api, *action[1:])
            else:
                comment_alert(api, *action[1:])
            made += 1
        except Refusal as r:
            first = first or r
        except SystemExit as exc:
            first = first or Refusal("api-failure", str(exc)[:200])
    print(f"comment-containment alert: opened {made} of {len(jobs)} alert(s) in the overlay repository; {skipped} already alerted")
    if first:
        return stop("alert", first)
    if refused and detect_result == "success":  # the sweep was cut short: what it read is contained and alerted, and the run goes red
        return stop("alert", Refusal(refused, "the scan stopped short and said so (see the refusal alert)"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("detect", "minimise", "alert"))
    ap.add_argument(
        "--base-root",
        default=os.getcwd(),
        help="the base checkout (default: the current directory); detect reads tools/disclosure_guard.py from it",
    )
    ap.add_argument(
        "--max-terms-age-hours",
        type=float,
        default=None,
        help="detect only: the oldest the neutrality terms may be, in hours (the mode line prints the limit applied)",
    )
    args = ap.parse_args(argv)
    if args.command == "detect":
        return detect(os.environ, args.base_root, args.max_terms_age_hours)
    return {"minimise": minimise, "alert": alert}[args.command](os.environ)


if __name__ == "__main__":
    sys.exit(main())
