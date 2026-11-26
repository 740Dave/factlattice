"""Detect and contain a comment, review or issue that names what the public repository must not name (scripts/comment_containment.py, run by the public workflow
comment-containment.yml).

Every term and rule here is SYNTHETIC (the ones tests/test_leak_check.py defines): nothing real is read, and a planted "leak" is a made-up string. The GitHub API is a FAKE,
a stub HTTP server on 127.0.0.1 that this file starts; nothing here contacts GitHub. The workflow's own `run:` text and `env:` wiring are what the tests execute (under bash,
in a base checkout holding the guard and the staged scripts): the three jobs run in the order the workflow gives them, and each job's outputs reach the next job's `env:` only
through the expressions the YAML writes, so what is measured is the same command line and the same wiring. A payload planted in a comment only ever writes a marker file; it
never runs git or touches the network.

What each group shows, and what the green of each cannot show:
  * an issue comment and an issue (the two event triggers) with a planted hit are detected, minimised through the API (the issue is alerted and its minimise job goes red, since the
    API cannot minimise an issue) and alerted in the overlay repository with the rule name, never the term;
  * the scheduled sweep finds a hit in a review and in a review comment updated in its window and in nothing outside it, and contains each;
  * a clean object is detected clean and nothing is written anywhere;
  * the guard's generic home-path rules are not applied to a comment; every overlay rule and generated-id rule still is;
  * an alert is opened once per object and rule set, at most ten per run (the rest in one summary), and once when a scan was refused or never ran;
  * a missing secret, in any of the three jobs, fails closed naming the secret and writes nothing;
  * each token goes only to the host and route that job uses;
  * shell text in a comment is handled as text: the marker a payload would write is absent;
  * text the check cannot read (a NUL character, a body that is not text) is a hit, never green.
"""

import datetime
import http.server
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from tests.test_leak_check import (
    BASE_TOKEN,
    DENY,
    DENY_TERM,
    GUARD,
    MODE_RE,
    OVERLAY,
    PUBLIC_DIR,
    REPO,
    SCRIPTS,
    SECRET,
    TERMS_PATH,
    VOCAB,
    VOCAB_TERM,
    terms_doc,
)

MINIMISE_SECRET, ALERT_SECRET = "minimise-write-token", "overlay-alert-token"
WORKFLOW = PUBLIC_DIR / "comment-containment.yml"
RUN_ID = "4242"
FILES = ("comment_containment.py", "leak_check.py", "gh_api.py")
OWN_HOST = "hostzeta"  # a synthetic own name: a host or volume of the maintainers
OWN_PATH_ROW = "\t".join([OWN_HOST, f"built on {OWN_HOST} here", "an own host name", "own-path-host"]) + "\n"
HOME_PATH = (
    "/" + "Users/jdoe/project"
)  # built by concatenation: a literal home path in this file would itself be a hit of the guard's built-in rule

# event name -> (a payload that names only ids, the kind the script reports, the route the script reads the text from)
EVENTS = {
    "issue_comment": (
        {"action": "created", "comment": {"id": 5, "body": "NOT READ FROM HERE"}, "issue": {"number": 3}},
        "issue-comment",
        f"/repos/{REPO}/issues/comments/5",
    ),
    "issues": (
        {"action": "opened", "issue": {"number": 9, "title": "NOT READ FROM HERE", "body": "NOT READ FROM HERE"}},
        "issue",
        f"/repos/{REPO}/issues/9",
    ),
}
SCHEDULE = {"schedule": "*/10 * * * *"}
IDS = {"issue-comment": 5, "review-comment": 6, "review": 8, "issue": 9}
PREFIX = {"issue-comment": "IC", "review-comment": "PRRC", "review": "PRR", "issue": "I"}


def node_of(kind, ident):
    return f"{PREFIX[kind]}_kwDOnode{ident}"


def url_of(kind, ident):
    return {
        "issue-comment": f"https://github.com/{REPO}/issues/3#issuecomment-{ident}",
        "review-comment": f"https://github.com/{REPO}/pull/7#discussion_r{ident}",
        "review": f"https://github.com/{REPO}/pull/7#pullrequestreview-{ident}",
        "issue": f"https://github.com/{REPO}/issues/{ident}",
    }[kind]


NODES = {k: node_of(k, i) for k, i in IDS.items()}
URLS = {k: url_of(k, i) for k, i in IDS.items()}


def now_stamp(minutes_ago=0):
    return (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------------------------------------------------------------- the fake API
class Fake:
    """An in-memory model of the routes this workflow uses. `calls` records (method, path, token, body) for every request."""

    def __init__(self):
        self.calls = []
        self.text = {
            "issue-comment": "a clean comment",
            "review-comment": "a clean comment",
            "review": "a clean review",
            "issue": "a clean body",
        }
        self.texts = {}  # (kind, id) -> text, overriding `text` for that object
        self.title = "a clean title"
        self.raw = {}  # kind -> the whole `body` value the API returns (any JSON value), overriding `text`
        self.gone = False
        self.status = 200  # the status the object routes answer with (the body is the full object either way)
        self.html_url = {}  # kind -> the `html_url` the API returns, overriding URLS
        self.overlay = {
            "private/publish-vocabulary.tsv": (VOCAB + OWN_PATH_ROW).encode(),
            "private/disclosure-denylist.tsv": DENY.encode(),
            TERMS_PATH: json.dumps(terms_doc()).encode(),
        }
        self.graphql = (200, {"data": {"minimizeComment": {"minimizedComment": {"isMinimized": True, "minimizedReason": "off_topic"}}}})
        self.issue_status = 201
        self.minimised = set()  # node ids the fake's mutation has minimised
        self.last_success_minutes_ago = 5  # this workflow's previous successful scheduled run (None: there is none)
        self.runs_status = 200
        self.comment_age = {}  # review comment id -> minutes since its own updated_at (default: now)
        self.pr_comments = {}  # pull request number -> its review comment ids (the per-PR list)
        self.lookup_status = 200  # the status of the alert-lookup route
        self.alerts = []  # the overlay's issues: {"number", "labels", "state", "title", "body"}
        # the sweep's world: pull requests (number, updated_at), their reviews and the repository's review comments, each (id,)
        self.prs = [{"number": 7, "updated_at": now_stamp(1)}]
        self.reviews = {7: [8]}
        self.review_comments = [6]
        self.list_status = 200

    def text_of(self, kind, ident):
        return self.texts.get((kind, ident), self.text[kind])

    def object_json(self, kind, ident):
        body = self.raw[kind] if kind in self.raw else self.text_of(kind, ident)
        obj = {"id": ident, "node_id": node_of(kind, ident), "html_url": self.html_url.get(kind, url_of(kind, ident)), "body": body}
        if kind == "issue":
            obj["title"] = self.title
        return obj

    def alert_json(self, a):
        return {"number": a["number"], "labels": [{"name": x} for x in a["labels"]], "state": a["state"]}

    def alert_edit(self, path, token, body):
        """A comment or a label added to an alert of the overlay."""
        if token != ALERT_SECRET:
            return 401, b'{"message":"Bad credentials"}'
        m = re.fullmatch(rf"/repos/{re.escape(OVERLAY)}/issues/(\d+)/(comments|labels)", path)
        alert = next((a for a in self.alerts if a["number"] == int(m.group(1))), None)
        if alert is None or self.issue_status != 201:
            return 404 if alert is None else self.issue_status, b'{"message":"x"}'
        if m.group(2) == "comments":
            alert.setdefault("comments", []).append(body["body"])
            return 201, b'{"id":1}'
        alert["labels"] += [x for x in body["labels"] if x not in alert["labels"]]
        return 200, b"[]"

    def graphql_route(self, token, body):
        is_read = "minimizeComment" not in (body or {}).get("query", "")
        allowed = (
            (BASE_TOKEN, MINIMISE_SECRET) if is_read else (MINIMISE_SECRET,)
        )  # the read may use either token; the mutation only the write token
        if token not in allowed:
            return 401, b'{"message":"Bad credentials"}'
        if is_read:  # the isMinimized read
            return 200, json.dumps({"data": {"node": {"isMinimized": body["variables"]["id"] in self.minimised}}}).encode()
        if self.graphql[0] == 200 and not self.graphql[1].get("errors"):
            self.minimised.add(body["variables"]["id"])
        return self.graphql[0], json.dumps(self.graphql[1]).encode()

    def route(self, method, path, query, token, body):
        self.calls.append((method, path, token, body))
        one = lambda k, d="": (query.get(k) or [d])[0]  # noqa: E731
        page = int(one("page", "1"))
        m = re.fullmatch(rf"/repos/({re.escape(OVERLAY)})/contents/(.+)", path)
        if method == "GET" and m:
            if token != SECRET:
                return 401, b'{"message":"Bad credentials"}'
            if m.group(2) not in self.overlay:
                return 404, b'{"message":"Not Found"}'
            return 200, self.overlay[m.group(2)]
        if path == f"/repos/{OVERLAY}/issues":
            if token != ALERT_SECRET:
                return 401, b'{"message":"Bad credentials"}'
            if method == "POST":
                if self.issue_status == 201:
                    n = len(self.alerts) + 41
                    self.alerts.append(
                        {"number": n, "labels": body.get("labels", []), "state": "open", "title": body["title"], "body": body["body"]}
                    )
                    return 201, json.dumps({"number": n, "html_url": "https://example.invalid/not-read"}).encode()
                return self.issue_status, b'{"message":"x"}'
            if self.lookup_status != 200:
                return self.lookup_status, b'{"message":"x"}'
            found = [a for a in self.alerts if one("labels") in a["labels"] and (one("state") == "all" or a["state"] == one("state"))]
            return 200, json.dumps([self.alert_json(a) for a in found][:1]).encode()
        if method == "POST" and re.fullmatch(rf"/repos/{re.escape(OVERLAY)}/issues/(\d+)/(comments|labels)", path):
            return self.alert_edit(path, token, body)
        if method == "POST" and path == "/graphql":
            return self.graphql_route(token, body)
        if method == "GET" and path.startswith(f"/repos/{REPO}/"):
            return self.repo_route(path[len(f"/repos/{REPO}") :], page, token, one)
        return 404, b'{"message":"Not Found"}'

    def repo_route(self, path, page, token, one):
        if token != BASE_TOKEN:
            return 401, b'{"message":"Bad credentials"}'
        singles = {"/issues/comments/5": ("issue-comment", 5), "/issues/9": ("issue", 9)}
        if path in singles:
            return (404, b'{"message":"Not Found"}') if self.gone else (self.status, json.dumps(self.object_json(*singles[path])).encode())
        sliced = lambda items: (self.list_status, json.dumps(items[(page - 1) * 100 : page * 100]).encode())  # noqa: E731
        if path == "/pulls":
            return sliced(sorted(self.prs, key=lambda p: p["updated_at"], reverse=True))
        m = re.fullmatch(r"/pulls/(\d+)/reviews", path)
        if m:
            return sliced([self.object_json("review", i) for i in self.reviews.get(int(m.group(1)), [])])
        if path == "/pulls/comments":
            since = one("since")
            seen = [i for i in self.review_comments if not since or now_stamp(self.comment_age.get(i, 0)) >= since]
            return sliced([self.object_json("review-comment", i) for i in seen])
        m = re.fullmatch(r"/pulls/(\d+)/comments", path)
        if m:
            return sliced([self.object_json("review-comment", i) for i in self.pr_comments.get(int(m.group(1)), [])])
        if path == "/actions/workflows/comment-containment.yml/runs":
            if self.runs_status != 200:
                return self.runs_status, b'{"message":"x"}'
            runs = [] if self.last_success_minutes_ago is None else [{"run_started_at": now_stamp(self.last_success_minutes_ago)}]
            return 200, json.dumps({"total_count": len(runs), "workflow_runs": runs}).encode()
        return 404, b'{"message":"Not Found"}'

    def posts(self):
        return [c for c in self.calls if c[0] != "GET"]

    def mutations(self):
        return [c for c in self.calls if c[1] == "/graphql" and "minimizeComment" in ((c[3] or {}).get("query") or "")]

    def issue_posts(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == f"/repos/{OVERLAY}/issues"]


@pytest.fixture
def server():
    fake = Fake()

    class Handler(http.server.BaseHTTPRequestHandler):
        def handle_any(self):
            u = urlsplit(self.path)
            token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"null") if n else None
            status, data = fake.route(self.command, u.path, parse_qs(u.query), token, body)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_any

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    fake.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield fake
    httpd.shutdown()
    httpd.server_close()


# ---------------------------------------------------------------------------------------------------------------------------------- running the workflow's jobs
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def make_base(tmp_path, guard=GUARD):
    """A base checkout as the workflow sees it: the guard and the staged scripts. Nothing of the event is in it."""
    base = tmp_path / "base"
    (base / "tools").mkdir(parents=True, exist_ok=True)
    shutil.copy(guard, base / "tools" / "disclosure_guard.py")
    scripts = base / ".github" / "workflows" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        shutil.copy(SCRIPTS / f, scripts / f)
    return base


def job_step(job):
    return next(s for s in workflow()["jobs"][job]["steps"] if "run" in s)


def resolve(value, secrets, needs):
    """The value of one `env:` entry of the workflow with its `${{ }}` expressions filled in from what the test provides. An expression of any other shape is a test
    failure, so a new expression in an `env:` cannot pass unseen."""

    def sub(m):
        e = " ".join(m.group(1).split())
        if e == "github.token":
            return BASE_TOKEN
        if (s := re.fullmatch(r"secrets\.([A-Z_]+)", e)) and s.group(1) in secrets:
            return secrets[s.group(1)]
        if s:
            return ""  # a secret the test does not provide is empty, as GitHub gives it
        if (o := re.fullmatch(r"needs\.detect\.outputs\.([a-z]+)", e)) and o.group(1) in needs:
            return needs[o.group(1)]
        if e in ("needs.minimise.result", "needs.detect.result"):
            return needs[e.split(".")[1] + "_result"]
        pytest.fail(f"an expression the tests do not know how to fill in: {e!r}")

    return re.sub(r"\$\{\{(.*?)\}\}", sub, str(value))


def run_job(tmp_path, fake, job, event_name, secrets=None, needs=None, extra_env=None, base=None, payload=None, tag=None):
    """Run the workflow's own `run:` text of `job` under bash in the base checkout with its own `env:` resolved. -> (CompletedProcess, the GITHUB_OUTPUT file's lines)."""
    base = base or make_base(tmp_path)
    tag = tag or job
    ev = tmp_path / f"event-{tag}.json"
    default = SCHEDULE if event_name == "schedule" else EVENTS[event_name][0] if event_name in EVENTS else {}
    ev.write_text(json.dumps(payload if payload is not None else default), encoding="utf-8")
    gh_out = tmp_path / f"output-{tag}.txt"
    gh_out.write_text("", encoding="utf-8")
    step = job_step(job)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "TMPDIR": os.environ.get("TMPDIR", str(tmp_path)),
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_EVENT_PATH": str(ev),
        "GITHUB_OUTPUT": str(gh_out),
        "GITHUB_API_URL": fake.url,
        "GITHUB_RUN_ID": RUN_ID,
        "GITHUB_SERVER_URL": "https://github.com",
    }
    all_secrets = {"OVERLAY_READ_TOKEN": SECRET, "COMMENT_MINIMISE_TOKEN": MINIMISE_SECRET, "OVERLAY_ALERT_TOKEN": ALERT_SECRET}
    all_secrets.update(secrets or {})
    env.update({k: resolve(v, all_secrets, needs or {}) for k, v in (step.get("env") or {}).items()})
    if "LC_OVERLAY_REPO" in env:
        env["LC_OVERLAY_REPO"] = OVERLAY  # the workflow names the real overlay repository; the fake serves a synthetic one
    env.update(extra_env or {})
    p = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=str(base), env=env, capture_output=True, text=True, timeout=120)
    lines = gh_out.read_text(encoding="utf-8").splitlines()
    return p, dict(x.split("=", 1) for x in lines)


def hits_of(outs):
    """The hits the scan wrote, each with its digest checked to be 64 hex digits and replaced by DIGEST (the digest of the text is tested on its own)."""
    hits = json.loads(outs["hits"])
    for h in hits:
        assert re.fullmatch(r"[0-9a-f]{64}", h["digest"]), h
        h["digest"] = DIGEST
    return hits


def pipeline(tmp_path, fake, event_name, secrets=None, extra_env=None, base=None, payload=None, tag=""):
    """The three jobs in the order the workflow gives them, with each job's `if:` applied. -> dict(detect, outputs, minimise, alert) where a job that was skipped is None."""
    wf = workflow()
    assert wf["jobs"]["minimise"]["if"] == "needs.detect.outputs.hit == 'true'"
    assert (
        wf["jobs"]["alert"]["if"]
        == "always() && (needs.detect.outputs.hit == 'true' || needs.detect.result != 'success' || needs.detect.outputs.refused != '' || needs.detect.outputs.gap != '')"
    )
    res = {"minimise": None, "alert": None}
    kw = {"extra_env": extra_env, "base": base, "payload": payload}
    res["detect"], outs = run_job(tmp_path, fake, "detect", event_name, secrets, tag=tag + "detect", **kw)
    res["outputs"] = outs
    detect_ok = res["detect"].returncode == 0
    needs = dict(outs, detect_result="success" if detect_ok else "failure", minimise_result="skipped")
    hit = detect_ok and outs.get("hit") == "true"
    if hit:
        res["minimise"], _ = run_job(tmp_path, fake, "minimise", event_name, secrets, needs=needs, tag=tag + "minimise", **kw)
        needs["minimise_result"] = "success" if res["minimise"].returncode == 0 else "failure"
    if hit or not detect_ok or outs.get("refused") or outs.get("gap"):
        res["alert"], _ = run_job(tmp_path, fake, "alert", event_name, secrets, needs=needs, tag=tag + "alert", **kw)
    return res


def out(p):
    return p.stdout + p.stderr


def no_term(*ps):
    text = "\n".join(out(p) for p in ps if p is not None).lower()
    for t in (VOCAB_TERM, DENY_TERM, OWN_HOST, "zorpbot", "frumpkin-lib"):
        assert t not in text, f"a term is in the output: {t!r}"


def plant(fake, kind, text, ident=None):
    if ident is None:
        fake.text[kind] = text
    else:
        fake.texts[(kind, ident)] = text


DIGEST = "a" * 64


def one_hit(kind="issue-comment", rules="vocab-vendor", count=1, ident=None):
    ident = ident or IDS[kind]
    return {
        "kind": kind,
        "id": ident,
        "node": node_of(kind, ident),
        "rules": rules,
        "count": count,
        "url": url_of(kind, ident),
        "digest": DIGEST,
    }


def alert_needs(hits, minimise_result="success", detect_result="success", refused="", overflow=0, gap=""):
    return {
        "hits": json.dumps(hits),
        "overflow": str(overflow),
        "refused": refused,
        "gap": gap,
        "hit": "true" if hits else "false",
        "detect_result": detect_result,
        "minimise_result": minimise_result,
    }


def alerts_text(fake):
    return json.dumps([(a["title"], a["body"], a["labels"]) for a in fake.alerts])


# ---------------------------------------------------------------------------------------------------------------------------------- a planted hit is minimised and alerted (the event triggers)
@pytest.mark.parametrize("event_name", sorted(EVENTS))
def test_a_planted_hit_is_detected_minimised_and_alerted_for_each_event_trigger_naming_the_rule_never_the_term(
    tmp_path, server, event_name
):
    _, kind, route = EVENTS[event_name]
    plant(server, kind, f"first line\nsecond line names {VOCAB_TERM}\n")
    r = pipeline(tmp_path, server, event_name)
    d, o = r["detect"], r["outputs"]
    assert d.returncode == 0, out(d)
    lines = d.stdout.splitlines()
    assert MODE_RE.match(lines[0]), lines[0]
    assert f"  {kind} #{IDS[kind]} body:2  [vocab-vendor]" in lines, lines
    assert any(x.startswith("RESULT: HIT. 1 finding(s) in 1 object(s)") and "{'vocab-vendor': 1}" in x for x in lines), lines
    assert o["hit"] == "true" and o["overflow"] == "0" and o["refused"] == "", o
    assert hits_of(o) == [one_hit(kind)], o
    # the text came from the API, not from the event (the event file holds a decoy)
    assert any(c[0] == "GET" and c[1] == route and c[2] == BASE_TOKEN for c in server.calls)
    # the alert: one issue in the overlay repository, the rule name and the count, never the term
    a = r["alert"]
    assert a is not None and a.returncode == 0, out(a)
    assert len(server.alerts) == 1 and server.issue_posts()[0][2] == ALERT_SECRET
    sent = server.issue_posts()[0][3]
    assert set(sent) == {"title", "body", "labels"} and re.fullmatch(r"cc-[0-9a-f]{20}", sent["labels"][0]), sent
    text = json.dumps(sent)
    assert "vocab-vendor" in text and URLS[kind] in text and RUN_ID in text and REPO in text and f"#{IDS[kind]}" in text, text
    no_term(d, r["minimise"], a)
    assert VOCAB_TERM not in text.lower() and "second line" not in text
    m = r["minimise"]
    if kind == "issue":
        # the API has no minimise for an issue: the job goes red (loud) and the alert says so
        assert m.returncode == 1 and "cannot be minimised" in m.stdout and not server.mutations(), out(m)
        assert "not minimised" in sent["body"].lower()
    else:
        assert m.returncode == 0, out(m)
        gql = server.mutations()
        assert len(gql) == 1 and gql[0][2] == MINIMISE_SECRET
        assert gql[0][3]["variables"] == {"id": NODES[kind], "classifier": "OFF_TOPIC"}, gql[0][3]
        assert "minimizeComment" in gql[0][3]["query"]
        assert "minimised: yes" in sent["body"].lower()
    print("RED comment-containment:", event_name, "->", o["hits"], "minimise rc", m.returncode)


def test_an_issues_title_is_scanned_too_and_every_hit_is_counted_by_rule(tmp_path, server):
    server.title = f"about {VOCAB_TERM}"
    plant(server, "issue", f"fine\nan order for {DENY_TERM}\nand {VOCAB_TERM} again\n")
    r = pipeline(tmp_path, server, "issues")
    lines = r["detect"].stdout.splitlines()
    assert (
        "  issue #9 title  [vocab-vendor]" in lines
        and "  issue #9 body:2  [deny-customer]" in lines
        and "  issue #9 body:3  [vocab-vendor]" in lines
    ), lines
    assert hits_of(r["outputs"]) == [one_hit("issue", "deny-customer,vocab-vendor", 3)], r["outputs"]
    no_term(r["detect"], r["alert"])


def test_a_clean_event_object_is_detected_clean_and_nothing_is_written_anywhere(tmp_path, server):
    for event_name, (_, kind, _) in EVENTS.items():
        plant(server, kind, "an ordinary remark, nothing to see")
        server.calls.clear()
        r = pipeline(tmp_path, server, event_name)
        assert r["detect"].returncode == 0 and r["minimise"] is None and r["alert"] is None, (event_name, out(r["detect"]))
        assert r["outputs"] == {"hit": "false", "hits": "[]", "overflow": "0", "refused": "", "gap": ""}, r["outputs"]
        lines = r["detect"].stdout.splitlines()
        assert MODE_RE.match(lines[0]) and lines[-1].startswith("RESULT: clean."), lines
        assert server.posts() == [], (event_name, server.posts())
        assert {c[1] for c in server.calls if c[0] == "GET" and f"/repos/{REPO}/" in c[1]} == {EVENTS[event_name][2]}


def test_the_hit_run_prints_a_scope_line_with_counts_and_digests_and_no_term(tmp_path, server):
    plant(server, "issue-comment", f"x {VOCAB_TERM}")
    d = pipeline(tmp_path, server, "issue_comment")["detect"]
    scope = next(x for x in d.stdout.splitlines() if x.startswith("comment-containment scope:"))
    assert re.fullmatch(
        r"comment-containment scope: mode=event objects=1 text-parts=1 lines=1 builtin-path-rules=skipped; vocabulary: rules=\d+ rows=\d+ sha256=[0-9a-f]{64}; "
        r"denylist: rules=\d+ rows=\d+ sha256=[0-9a-f]{64}; neutrality-terms: agent-ids=\d+ corpus-ids=\d+ sha256=[0-9a-f]{64} generated_at=\S+; rules-total=\d+",
        scope,
    ), scope
    no_term(d)


# ---------------------------------------------------------------------------------------------------------------------------------- the scheduled sweep (reviews and review comments)
def test_the_sweep_contains_a_hit_in_a_review_and_in_a_review_comment_and_reads_no_event_text(tmp_path, server):
    plant(server, "review", f"the review names {VOCAB_TERM}")
    plant(server, "review-comment", f"line one\nan order for {DENY_TERM}")
    r = pipeline(tmp_path, server, "schedule")
    d, o = r["detect"], r["outputs"]
    assert d.returncode == 0, out(d)
    assert sorted(hits_of(o), key=lambda h: h["kind"]) == [
        one_hit("review", "vocab-vendor", 1),
        one_hit("review-comment", "deny-customer", 1),
    ], o
    lines = d.stdout.splitlines()
    assert "  review #8 body:1  [vocab-vendor]" in lines and "  review-comment #6 body:2  [deny-customer]" in lines, lines
    assert "mode=sweep objects=2" in d.stdout
    assert r["minimise"].returncode == 0 and r["alert"].returncode == 0, (out(r["minimise"]), out(r["alert"]))
    gql = [c[3]["variables"] for c in server.mutations()]
    assert sorted(g["id"] for g in gql) == sorted([NODES["review"], NODES["review-comment"]]) and {g["classifier"] for g in gql} == {
        "OFF_TOPIC"
    }
    assert len(server.alerts) == 2
    no_term(d, r["minimise"], r["alert"])
    # the sweep reads lists from the default branch's API only, with the read token: no event id was used
    assert {c[1] for c in server.calls if c[0] == "GET" and f"/repos/{REPO}/" in c[1]} == {
        f"/repos/{REPO}/pulls",
        f"/repos/{REPO}/pulls/7/reviews",
        f"/repos/{REPO}/pulls/7/comments",
        f"/repos/{REPO}/pulls/comments",
        f"/repos/{REPO}/actions/workflows/comment-containment.yml/runs",
    }
    print("RED sweep:", o["hits"])


def test_the_sweep_reads_only_pull_requests_updated_in_its_window(tmp_path, server):
    server.prs = [{"number": 7, "updated_at": now_stamp(5)}, {"number": 12, "updated_at": now_stamp(30 * 60)}]
    server.reviews = {7: [8], 12: [13]}
    plant(server, "review", f"names {VOCAB_TERM}", ident=13)  # a review of a pull request nobody touched for thirty hours
    r = pipeline(tmp_path, server, "schedule")
    assert r["outputs"]["hit"] == "false" and "mode=sweep objects=2" in r["detect"].stdout, (r["outputs"], r["detect"].stdout)
    assert f"/repos/{REPO}/pulls/12/reviews" not in {c[1] for c in server.calls}
    assert since_in_calls(server, "/pulls/comments") is None  # the fake ignores it; the next test pins what is sent


def since_in_calls(server, path):
    return next((c for c in server.calls if c[1] == f"/repos/{REPO}{path}" and "since" in c[1]), None)


def test_the_sweep_asks_for_review_comments_since_the_window_start(tmp_path, server):
    seen = []
    orig = server.route

    def spy(method, path, query, token, body):
        if path.endswith("/pulls/comments"):
            seen.append(query.get("since"))
        return orig(method, path, query, token, body)

    server.route = spy
    pipeline(tmp_path, server, "schedule")
    assert seen and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", seen[0][0]), seen
    start = datetime.datetime.strptime(seen[0][0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
    assert 24 * 3600 - 60 <= (datetime.datetime.now(datetime.UTC) - start).total_seconds() <= 24 * 3600 + 60


def test_a_clean_sweep_writes_nothing_and_an_empty_world_is_clean(tmp_path, server):
    r = pipeline(tmp_path, server, "schedule")
    assert r["outputs"] == {"hit": "false", "hits": "[]", "overflow": "0", "refused": "", "gap": ""} and server.posts() == []
    server.prs, server.reviews, server.review_comments = [], {}, []
    r = pipeline(tmp_path, server, "schedule", tag="again")
    assert r["detect"].returncode == 0 and "objects=0" in r["detect"].stdout and r["outputs"]["hit"] == "false"


def test_a_sweep_that_cannot_list_fails_closed(tmp_path, server):
    server.list_status = 403
    p, outs = run_job(tmp_path, server, "detect", "schedule")
    assert p.returncode == 1 and "(object-unreadable)" in p.stdout and outs["refused"] == "object-unreadable" and outs["hit"] == "false", (
        out(p)
    )


def test_a_sweep_cut_by_a_page_cap_contains_and_alerts_what_it_read_then_refuses(tmp_path, server):
    """More than five full pages of pull requests inside the window: the PRs read are still scanned, their hits minimised and alerted, the refusal alert opens, and the run is red."""
    server.prs = [{"number": 7, "updated_at": now_stamp(1)}] + [{"number": n, "updated_at": now_stamp(2)} for n in range(100, 600)]
    server.reviews = {n: [] for n in range(100, 600)} | {7: [8]}
    plant(server, "review", f"x {VOCAB_TERM}")  # in PR 7, which the first page holds
    r = pipeline(tmp_path, server, "schedule")
    assert r["detect"].returncode == 0 and r["outputs"]["refused"] == "sweep-incomplete" and r["outputs"]["hit"] == "true", out(r["detect"])
    assert [h["kind"] for h in hits_of(r["outputs"])] == ["review"]
    assert r["minimise"].returncode == 0 and len(server.mutations()) == 1
    assert r["alert"].returncode == 1 and "sweep-incomplete" in r["alert"].stdout, out(r["alert"])  # red, after every alert was opened
    titles = sorted(a["title"] for a in server.alerts)
    assert titles == ["comment-containment: review #8 hit in owner/pub", "comment-containment: scan refused (sweep-incomplete)"], titles


def test_a_sweep_reads_every_page_of_a_long_list_and_stops_at_the_window(tmp_path, server):
    server.prs = [{"number": n, "updated_at": now_stamp(1)} for n in range(1, 251)] + [{"number": 999, "updated_at": now_stamp(300)}]
    server.reviews = {n: [] for n in range(1, 251)}
    server.review_comments = list(range(1000, 1150))  # 150 review comments: two pages
    for i in (1000, 1149):
        plant(server, "review-comment", f"x {VOCAB_TERM}", ident=i)
    r = pipeline(tmp_path, server, "schedule")
    assert sorted(h["id"] for h in hits_of(r["outputs"])) == [1000, 1149], r["outputs"]
    assert "objects=150" in r["detect"].stdout


def test_an_object_seen_twice_is_scanned_once_and_a_list_entry_without_an_id_fails_closed(tmp_path, server):
    server.reviews = {7: [8, 8]}
    plant(server, "review", f"x {VOCAB_TERM}")
    r = pipeline(tmp_path, server, "schedule")
    assert [h["id"] for h in hits_of(r["outputs"])] == [8] and "objects=2" in r["detect"].stdout
    orig = server.object_json
    server.object_json = lambda kind, ident: {k: v for k, v in orig(kind, ident).items() if k != "id"}
    p, _ = run_job(tmp_path, server, "detect", "schedule", tag="noid")
    assert p.returncode == 1 and "(object-unreadable)" in p.stdout, out(p)


# ---------------------------------------------------------------------------------------------------------------------------------- the guard's generic path rules are not applied to comments
def test_a_home_path_in_a_comment_is_not_a_hit_but_every_overlay_and_generated_rule_still_is(tmp_path, server):
    for kind in ("issue-comment", "review", "review-comment"):
        plant(server, kind, f'traceback: File "{HOME_PATH}/a.py", line 3')
    plant(server, "issue", f"see {HOME_PATH}/b.py")
    for event_name in ("issue_comment", "issues", "schedule"):
        r = pipeline(tmp_path, server, event_name, tag=event_name)
        assert r["outputs"]["hit"] == "false", (event_name, out(r["detect"]))
        assert "builtin-path-rules=skipped" in r["detect"].stdout
    assert server.posts() == []
    # with a vocabulary term, a denylist term or a generated id beside the path, the object is a hit and the path is not named
    for text, rule in (
        (f"{HOME_PATH}/a.py {VOCAB_TERM}", "vocab-vendor"),
        (f"{HOME_PATH} {DENY_TERM}", "deny-customer"),
        (f"{HOME_PATH} zorpbot", "neutrality-agent-id"),
    ):
        plant(server, "issue-comment", text)
        r = pipeline(tmp_path, server, "issue_comment", tag=rule)
        assert [h["rules"] for h in hits_of(r["outputs"])] == [rule], (text, r["outputs"])
        assert "jdoe" not in out(r["detect"]) and "absolute-home-path" not in out(r["detect"])
        no_term(r["detect"], r["alert"])


# ---------------------------------------------------------------------------------------------------------------------------------- alerts are keyed, capped and raised for a refused scan
def test_an_unchanged_text_opens_no_new_alert_and_a_changed_text_or_another_rule_set_does(tmp_path, server):
    plant(server, "issue-comment", f"x {VOCAB_TERM}")
    pipeline(tmp_path, server, "issue_comment", tag="one")
    assert len(server.alerts) == 1
    r = pipeline(tmp_path, server, "issue_comment", tag="two")  # an `edited` event that left the text as it was
    assert len(server.alerts) == 1 and "1 already alerted" in r["alert"].stdout and r["alert"].returncode == 0, (
        len(server.alerts),
        out(r["alert"]),
    )
    plant(
        server, "issue-comment", f"edited, still {VOCAB_TERM} twice {VOCAB_TERM}"
    )  # same object, same rule set, new text: a comment on the open alert
    pipeline(tmp_path, server, "issue_comment", tag="three")
    assert len(server.alerts) == 1 and len(server.alerts[0]["comments"]) == 1
    plant(server, "issue-comment", f"edited again: {VOCAB_TERM} and {DENY_TERM}")  # another rule set
    pipeline(tmp_path, server, "issue_comment", tag="four")
    assert (
        len(server.alerts) == 2 and len({a["labels"][0] for a in server.alerts}) == 2
    )  # another rule set is another object key: a new alert


def test_a_closed_alert_still_counts_as_alerted_for_an_object_and_a_failed_lookup_opens_one_anyway(tmp_path, server):
    plant(server, "issue-comment", f"x {VOCAB_TERM}")
    pipeline(tmp_path, server, "issue_comment", tag="one")
    server.alerts[0]["state"] = "closed"
    pipeline(tmp_path, server, "issue_comment", tag="two")
    assert len(server.alerts) == 1
    server.lookup_status = 500
    r = pipeline(tmp_path, server, "issue_comment", tag="three")
    assert len(server.alerts) == 2 and r["alert"].returncode == 0, out(r["alert"])


def test_a_sweep_that_sees_the_same_hits_again_opens_nothing_new(tmp_path, server):
    plant(server, "review-comment", f"x {VOCAB_TERM}")
    pipeline(tmp_path, server, "schedule", tag="a")
    pipeline(tmp_path, server, "schedule", tag="b")
    assert len(server.alerts) == 1
    assert len(server.mutations()) == 1  # the second sweep asks first, finds the object already minimised and skips it


def test_at_most_ten_alerts_are_opened_per_run_and_the_rest_are_one_summary_while_every_hit_is_minimised(tmp_path, server):
    ids = list(range(2000, 2013))  # thirteen hits
    server.review_comments = ids
    for i in ids:
        plant(server, "review-comment", f"x {VOCAB_TERM}", ident=i)
    r = pipeline(tmp_path, server, "schedule")
    assert r["minimise"].returncode == 0 and r["alert"].returncode == 0, (out(r["minimise"]), out(r["alert"]))
    assert len(server.mutations()) == 13
    titles = sorted(a["title"] for a in server.alerts)
    assert len(server.alerts) == 11 and sum(t.startswith("comment-containment: review-comment #") for t in titles) == 10, titles
    summary = next(a for a in server.alerts if "more hit(s)" in a["title"])
    assert summary["title"] == "comment-containment: 3 more hit(s) not alerted individually" and summary["labels"] == [
        f"cc-summary-{RUN_ID}"
    ]
    assert "Count: 3" in summary["body"] and VOCAB_TERM not in alerts_text(server).lower()
    # a second run of the same run id opens no second summary
    r = pipeline(tmp_path, server, "schedule", tag="again")
    assert len(server.alerts) == 14  # the three unalerted hits get their own alerts now (within the cap): 11 + 3, and no second summary
    assert len([a for a in server.alerts if "more hit(s)" in a["title"]]) == 1


def test_more_hits_than_a_run_carries_are_counted_in_the_summary(tmp_path, server):
    ids = list(range(3000, 3060))  # sixty hits: fifty are carried, ten are only counted
    server.review_comments = ids
    for i in ids:
        plant(server, "review-comment", f"x {VOCAB_TERM}", ident=i)
    r = pipeline(tmp_path, server, "schedule")
    assert r["outputs"]["overflow"] == "10" and len(hits_of(r["outputs"])) == 50, r["outputs"]["overflow"]
    assert len(server.mutations()) == 50
    summary = next(a for a in server.alerts if "more hit(s)" in a["title"])
    assert (
        summary["title"] == "comment-containment: 50 more hit(s) not alerted individually"
    )  # forty carried beyond the cap, ten beyond the carry


def test_a_refused_scan_opens_one_alert_naming_the_cause_only_and_not_a_second_while_it_is_open(tmp_path, server):
    plant(server, "issue-comment", f"x {VOCAB_TERM}")
    r = pipeline(tmp_path, server, "issue_comment", secrets={"OVERLAY_READ_TOKEN": ""})
    assert r["detect"].returncode == 1 and r["minimise"] is None and r["outputs"]["refused"] == "no-overlay-secret", r["outputs"]
    assert r["alert"].returncode == 0, out(r["alert"])
    assert [(a["title"], a["labels"]) for a in server.alerts] == [
        ("comment-containment: scan refused (no-overlay-secret)", ["cc-refused-no-overlay-secret"])
    ]
    assert "Cause: no-overlay-secret" in server.alerts[0]["body"] and VOCAB_TERM not in alerts_text(server).lower()
    assert not server.mutations()
    pipeline(tmp_path, server, "issue_comment", secrets={"OVERLAY_READ_TOKEN": ""}, tag="two")
    assert len(server.alerts) == 1
    server.alerts[0]["state"] = "closed"  # a maintainer closed it: the next refusal is a new alert
    pipeline(tmp_path, server, "issue_comment", secrets={"OVERLAY_READ_TOKEN": ""}, tag="three")
    assert len(server.alerts) == 2


@pytest.mark.parametrize("what", ["stale terms", "an unreadable overlay", "an unreadable object"])
def test_every_kind_of_refusal_is_alerted_once_with_its_cause_code(tmp_path, server, what):
    from tests.test_leak_check import stamp

    cause = None
    if what == "stale terms":
        server.overlay[TERMS_PATH] = json.dumps(terms_doc(generated_at=stamp(-200 * 3600))).encode()
    elif what == "an unreadable overlay":
        server.overlay.clear()
        cause = "missing"
    else:
        server.gone = True
        cause = "object-unreadable"
    r = pipeline(tmp_path, server, "issue_comment")
    assert r["detect"].returncode == 1 and len(server.alerts) == 1 and r["alert"].returncode == 0, out(r["detect"])
    got = r["outputs"]["refused"]
    assert re.fullmatch(r"[a-z-]+", got) and (cause is None or got == cause), got
    assert server.alerts[0]["labels"] == [f"cc-refused-{got}"]


def test_a_detect_job_that_never_ran_is_alerted_as_a_scan_that_did_not_run(tmp_path, server):
    p, _ = run_job(tmp_path, server, "alert", "issue_comment", needs=alert_needs([], "skipped", "failure"))
    assert p.returncode == 0 and server.alerts[0]["title"] == "comment-containment: scan refused (scan-did-not-run)", out(p)
    assert server.alerts[0]["labels"] == ["cc-refused-scan-did-not-run"]


def test_a_successful_detect_with_no_hit_alerts_nothing_even_if_the_alert_job_were_run(tmp_path, server):
    p, _ = run_job(tmp_path, server, "alert", "issue_comment", needs=alert_needs([]))
    assert p.returncode == 0 and server.alerts == [], out(p)


# ---------------------------------------------------------------------------------------------------------------------------------- fail closed
@pytest.mark.parametrize(
    ("job", "secret", "cause"),
    [
        ("detect", "OVERLAY_READ_TOKEN", "no-overlay-secret"),
        ("minimise", "COMMENT_MINIMISE_TOKEN", "no-minimise-secret"),
        ("alert", "OVERLAY_ALERT_TOKEN", "no-alert-secret"),
    ],
)
@pytest.mark.parametrize("empty", ["", "  \t"])
def test_a_missing_secret_fails_closed_in_each_job_naming_the_secret_and_writing_nothing(tmp_path, server, job, secret, cause, empty):
    plant(server, "issue-comment", f"x {VOCAB_TERM}")
    server.calls.clear()
    p, outs = run_job(tmp_path, server, job, "issue_comment", secrets={secret: empty}, needs=alert_needs([one_hit()]))
    assert p.returncode == 1, out(p)
    assert f"the secret {secret} is not available" in p.stdout and f"({cause})" in p.stdout, out(p)
    assert "::error::" in p.stdout
    assert server.posts() == [] and outs.get("hit") != "true", (server.posts(), outs)
    if job == "detect":
        assert p.stdout.splitlines()[0] == f"neutrality terms: REQUIRED, REFUSED ({cause})"
        assert not server.calls  # nothing at all was read: not the overlay, not the comment
    no_term(p)


@pytest.mark.parametrize(
    ("what", "cause"),
    [
        ("an overlay the token cannot read", "overlay-unreadable"),
        ("an absent terms file", "missing"),
        ("an absent vocabulary", "missing"),
    ],
)
def test_an_unusable_overlay_fails_closed_before_the_comment_is_read(tmp_path, server, what, cause):
    plant(server, "issue-comment", f"x {VOCAB_TERM}")
    if what.startswith("an overlay the token"):
        server.overlay.clear()
        p, _ = run_job(tmp_path, server, "detect", "issue_comment", secrets={"OVERLAY_READ_TOKEN": "wrong"})
    elif "terms" in what:
        del server.overlay[TERMS_PATH]
        p, _ = run_job(tmp_path, server, "detect", "issue_comment")
    else:
        del server.overlay["private/publish-vocabulary.tsv"]
        p, _ = run_job(tmp_path, server, "detect", "issue_comment")
    assert p.returncode == 1 and f"({cause})" in p.stdout, out(p)
    assert p.stdout.splitlines()[0] == f"neutrality terms: REQUIRED, REFUSED ({cause})"
    assert not [c for c in server.calls if f"/repos/{REPO}/" in c[1]], server.calls  # the comment was never read
    no_term(p)


def test_a_stale_terms_file_is_refused_past_192_hours_and_the_mode_line_prints_the_limit(tmp_path, server):
    from tests.test_leak_check import stamp

    server.overlay[TERMS_PATH] = json.dumps(terms_doc(generated_at=stamp(-191 * 3600))).encode()
    p, _ = run_job(tmp_path, server, "detect", "issue_comment")
    assert p.returncode == 0 and p.stdout.splitlines()[0].endswith(", max age 192 h"), out(p)
    server.overlay[TERMS_PATH] = json.dumps(terms_doc(generated_at=stamp(-193 * 3600))).encode()
    p, _ = run_job(tmp_path, server, "detect", "issue_comment")
    assert p.returncode == 1 and p.stdout.splitlines()[0].startswith("neutrality terms: REQUIRED, REFUSED ("), out(p)


def test_a_comment_that_cannot_be_read_fails_closed_and_is_never_green(tmp_path, server):
    server.gone = True
    p, outs = run_job(tmp_path, server, "detect", "issue_comment")
    assert p.returncode == 1 and "(object-unreadable)" in p.stdout and outs["hit"] == "false" and outs["refused"] == "object-unreadable", (
        out(p)
    )


@pytest.mark.parametrize("status", [201, 301, 403, 500])
def test_an_answer_with_a_status_other_than_200_is_refused_even_when_its_body_looks_like_the_object(tmp_path, server, status):
    server.status = status
    p, outs = run_job(tmp_path, server, "detect", "issue_comment")
    assert p.returncode == 1 and f"(HTTP {status})" in p.stdout and "(object-unreadable)" in p.stdout and outs["hit"] == "false", out(p)


@pytest.mark.parametrize(
    "url",
    [
        "https://elsewhere.example/x",
        "https://github.com/other/repo/issues/3#issuecomment-5",
        "http://github.com/" + REPO + "/issues/3",
        "",
        None,
        7,
    ],
)
def test_an_address_that_is_not_one_of_this_repositorys_is_not_passed_on_and_the_alert_still_opens(tmp_path, server, url):
    plant(server, "issue-comment", f"x {VOCAB_TERM}")
    server.html_url["issue-comment"] = url
    r = pipeline(tmp_path, server, "issue_comment")
    assert hits_of(r["outputs"])[0]["url"] == "" and r["outputs"]["hit"] == "true", r["outputs"]
    assert r["alert"].returncode == 0, out(r["alert"])
    assert "elsewhere" not in alerts_text(server) and "other/repo" not in alerts_text(server)
    assert "issue-comment #5 in owner/pub" in server.alerts[0]["body"]  # the alert still names the object by its id


@pytest.mark.parametrize("bad", [123, ["x"], {"a": 1}, True])
def test_a_body_that_is_not_text_fails_closed(tmp_path, server, bad):
    server.raw["issue-comment"] = bad
    p, outs = run_job(tmp_path, server, "detect", "issue_comment")
    assert p.returncode == 1 and "(object-unreadable)" in p.stdout and outs["hit"] == "false", out(p)


def test_an_empty_review_body_is_clean(tmp_path, server):
    server.raw["review"] = None  # an approval with no text: the API gives null
    r = pipeline(tmp_path, server, "schedule")
    assert r["detect"].returncode == 0 and r["outputs"]["hit"] == "false", out(r["detect"])


@pytest.mark.parametrize("text", ["a\x00b", f"clean\n\x00\n{VOCAB_TERM}"])
def test_text_the_check_cannot_read_is_a_hit_not_a_pass(tmp_path, server, text):
    plant(server, "issue-comment", text)
    r = pipeline(tmp_path, server, "issue_comment")
    assert r["outputs"]["hit"] == "true" and "unscannable-text" in hits_of(r["outputs"])[0]["rules"], (r["outputs"], out(r["detect"]))
    assert r["minimise"].returncode == 0 and r["alert"].returncode == 0
    no_term(r["detect"], r["alert"])


@pytest.mark.parametrize(
    ("event_name", "payload"),
    [
        ("push", {"ref": "x"}),
        ("pull_request_review", {"action": "submitted", "review": {"id": 8}, "pull_request": {"number": 7}}),
        ("pull_request_review_comment", {"action": "created", "comment": {"id": 6}}),
        ("issue_comment", {"action": "created", "issue": {"number": 3}}),
        ("issue_comment", {"action": "created", "comment": {"id": "5"}}),
        ("issue_comment", {"action": "created", "comment": {"id": True}}),
        ("issue_comment", {"action": "created", "comment": {"id": 0}}),
        ("issue_comment", {"action": "created", "comment": {"id": 5.5}}),
        ("issues", {"action": "opened", "issue": {}}),
        ("issues", None),
    ],
)
def test_an_event_that_does_not_name_the_object_fails_closed(tmp_path, server, event_name, payload):
    plant(server, "issue-comment", f"x {VOCAB_TERM}")
    p, outs = run_job(tmp_path, server, "detect", event_name, payload=payload if payload is not None else [])
    assert p.returncode == 1 and "(bad-input)" in p.stdout and outs["hit"] == "false", out(p)
    assert server.posts() == []


def test_a_malformed_environment_value_fails_closed(tmp_path, server):
    for key, value in (("GITHUB_REPOSITORY", "not a repo"), ("LC_OVERLAY_REPO", "a/b/c")):
        p, _ = run_job(tmp_path, server, "detect", "issue_comment", extra_env={key: value})
        assert p.returncode == 1 and "(bad-input)" in p.stdout, (key, out(p))


GOOD = one_hit()
BAD_HITS = [
    ("not json", "{"),
    ("not a list", json.dumps(GOOD)),
    ("a missing field", json.dumps([{k: v for k, v in GOOD.items() if k != "url"}])),
    ("an extra field", json.dumps([dict(GOOD, extra="x")])),
    ("a bad kind", json.dumps([dict(GOOD, kind="other")])),
    ("a bad id", json.dumps([dict(GOOD, id=0)])),
    ("a bool id", json.dumps([dict(GOOD, id=True)])),
    ("a string id", json.dumps([dict(GOOD, id="5")])),
    ("an empty node", json.dumps([dict(GOOD, node="")])),
    ("a node with a space", json.dumps([dict(GOOD, node="bad node")])),
    ("a node with a line break", json.dumps([dict(GOOD, node="a\nhit=true")])),
    ("a long node", json.dumps([dict(GOOD, node="x" * 300)])),
    ("a node with a shell character", json.dumps([dict(GOOD, node="$(touch X)")])),
    ("rules with a space", json.dumps([dict(GOOD, rules="a b")])),
    ("empty rules", json.dumps([dict(GOOD, rules="")])),
    ("a zero count", json.dumps([dict(GOOD, count=0)])),
    ("a string count", json.dumps([dict(GOOD, count="1")])),
    ("a foreign address", json.dumps([dict(GOOD, url="http://elsewhere.example/x")])),
    ("too many hits", json.dumps([GOOD] * 51)),
]


@pytest.mark.parametrize("job", ["minimise", "alert"])
@pytest.mark.parametrize(("what", "raw"), BAD_HITS)
def test_a_later_job_refuses_a_hit_that_is_not_what_the_scan_writes(tmp_path, server, job, what, raw):
    needs = dict(alert_needs([GOOD]), hits=raw)
    p, _ = run_job(tmp_path, server, job, "issue_comment", needs=needs)
    assert p.returncode == 1 and "(bad-input)" in p.stdout and server.posts() == [], (what, out(p))


def test_a_minimise_job_with_no_hits_refuses_and_an_alert_job_with_no_hits_and_a_good_detect_does_nothing(tmp_path, server):
    p, _ = run_job(tmp_path, server, "minimise", "issue_comment", needs=alert_needs([]))
    assert p.returncode == 1 and "(bad-input)" in p.stdout


def test_the_alert_job_refuses_a_result_word_a_cause_or_a_count_that_is_not_what_the_workflow_writes(tmp_path, server):
    base = alert_needs([GOOD])
    for key, bad in (
        ("minimise_result", "weird"),
        ("detect_result", "weird"),
        ("refused", "A B"),
        ("refused", "x" * 60),
        ("overflow", "x"),
        ("overflow", "-1"),
    ):
        p, _ = run_job(tmp_path, server, "alert", "issue_comment", needs=dict(base, **{key: bad}))
        assert p.returncode == 1 and "(bad-input)" in p.stdout, (key, bad, out(p))
    assert server.posts() == []


@pytest.mark.parametrize(
    ("job", "what"),
    [
        ("minimise", "a refused mutation"),
        ("minimise", "a mutation that did not minimise"),
        ("minimise", "an error"),
        ("minimise", "errors beside a true answer"),
        ("alert", "a refused issue"),
    ],
)
def test_a_write_that_did_not_land_is_red(tmp_path, server, job, what):
    needs = alert_needs([GOOD], "failure")
    if what == "a refused mutation":
        server.graphql = (403, {"message": "forbidden"})
    elif what == "a mutation that did not minimise":
        server.graphql = (200, {"data": {"minimizeComment": {"minimizedComment": {"isMinimized": False}}}})
    elif what == "an error":
        server.graphql = (200, {"errors": [{"type": "FORBIDDEN", "message": "text that must not print"}], "data": None})
    elif what == "errors beside a true answer":
        # the answer says minimised AND carries errors: the errors check alone refuses it
        server.graphql = (
            200,
            {"errors": [{"message": "text that must not print"}], "data": {"minimizeComment": {"minimizedComment": {"isMinimized": True}}}},
        )
    else:
        server.issue_status = 403
    p, _ = run_job(tmp_path, server, job, "issue_comment", needs=needs)
    assert p.returncode == 1 and f"comment-containment {job} refused" in p.stdout and "::error::" in p.stdout, out(p)
    assert "text that must not print" not in out(p)
    no_term(p)


def test_the_minimise_job_tries_every_hit_and_fails_if_any_failed(tmp_path, server):
    hits = [one_hit("issue-comment"), one_hit("issue", ident=9), one_hit("review", ident=8)]
    p, _ = run_job(tmp_path, server, "minimise", "schedule", needs=alert_needs(hits))
    assert p.returncode == 1 and "2 of 3 hit(s) minimised" in p.stdout and "cannot be minimised" in p.stdout, out(p)
    assert sorted(c[3]["variables"]["id"] for c in server.mutations()) == sorted([NODES["issue-comment"], NODES["review"]])


def test_the_alert_says_when_the_minimise_failed(tmp_path, server):
    plant(server, "issue-comment", f"x {VOCAB_TERM}")
    server.graphql = (403, {"message": "forbidden"})
    r = pipeline(tmp_path, server, "issue_comment")
    assert r["minimise"].returncode == 1 and r["alert"].returncode == 0
    body = server.alerts[0]["body"]
    assert "minimised: no" in body.lower() and "failure" in body.lower(), body


def test_a_failed_alert_post_does_not_stop_the_others_and_the_job_is_red(tmp_path, server):
    server.issue_status = 403
    p, _ = run_job(
        tmp_path, server, "alert", "schedule", needs=alert_needs([one_hit("review", ident=8), one_hit("review-comment", ident=6)])
    )
    assert p.returncode == 1 and "opened 0 of 2 alert(s)" in p.stdout and len(server.issue_posts()) == 2, out(p)


# ---------------------------------------------------------------------------------------------------------------------------------- the two guards no other test reaches
def script_module():
    spec = importlib.util.spec_from_file_location("cc_test_script", str(SCRIPTS / "comment_containment.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(SCRIPTS))
    return mod


@pytest.mark.parametrize("value", ["a\nhit=true", "a\rb", "x\r\ny"])
def test_the_outputs_writer_refuses_a_value_with_a_line_break_and_writes_nothing(tmp_path, value):
    """The upstream shape checks keep a line break from reaching the writer, so the writer's own re-check is only reachable by calling it: it must refuse, and leave the file untouched."""
    mod = script_module()
    path = tmp_path / "out.txt"
    path.write_text("keep=1\n", encoding="utf-8")
    with pytest.raises(mod.Refusal) as e:
        mod.write_outputs(str(path), {"hit": "false", "bad": value})
    assert e.value.cause == "bad-input" and path.read_text(encoding="utf-8") == "keep=1\n"
    mod.write_outputs(str(path), {"hit": "false", "n": 3})  # a clean value is written
    assert path.read_text(encoding="utf-8") == "keep=1\nhit=false\nn=3\n"


# ---------------------------------------------------------------------------------------------------------------------------------- tokens go only where they belong
def test_each_token_goes_only_to_the_route_its_job_uses_and_no_token_is_printed(tmp_path, server):
    plant(server, "review", f"x {VOCAB_TERM}")
    r = pipeline(tmp_path, server, "schedule")
    assert r["minimise"].returncode == 0 and r["alert"].returncode == 0
    by_token = {}
    for method, path, token, _ in server.calls:
        by_token.setdefault(token, set()).add((method, re.sub(r"/contents/.*", "/contents/<file>", path)))
    assert by_token == {
        SECRET: {("GET", f"/repos/{OVERLAY}/contents/<file>")},
        BASE_TOKEN: {
            ("GET", f"/repos/{REPO}/pulls"),
            ("GET", f"/repos/{REPO}/pulls/7/reviews"),
            ("GET", f"/repos/{REPO}/pulls/7/comments"),
            ("GET", f"/repos/{REPO}/pulls/comments"),
            ("GET", f"/repos/{REPO}/actions/workflows/comment-containment.yml/runs"),
            ("POST", "/graphql"),  # the isMinimized read of a hit's object, with the read token
        },
        MINIMISE_SECRET: {("POST", "/graphql")},
        ALERT_SECRET: {("GET", f"/repos/{OVERLAY}/issues"), ("POST", f"/repos/{OVERLAY}/issues")},
    }, by_token
    for p in (r["detect"], r["minimise"], r["alert"]):
        for tok in (SECRET, BASE_TOKEN, MINIMISE_SECRET, ALERT_SECRET):
            assert tok not in out(p)


def test_a_job_is_given_only_its_own_secret_in_its_environment():
    wf = workflow()
    wanted = {"detect": "OVERLAY_READ_TOKEN", "minimise": "COMMENT_MINIMISE_TOKEN", "alert": "OVERLAY_ALERT_TOKEN"}
    for job, secret in wanted.items():
        step = job_step(job)
        held = [k for k, v in step["env"].items() if "secrets." in str(v)]
        assert held == [secret], (job, held)
        assert step["env"][secret] == "${{ secrets." + secret + " }}"
        # no other step and no job-level key of the job names a secret
        rest = {k: v for k, v in wf["jobs"][job].items() if k != "steps"}
        assert "secrets" not in yaml.safe_dump(rest) and "secrets" not in yaml.safe_dump(
            [s for s in wf["jobs"][job]["steps"] if "run" not in s]
        )
    assert "secrets" not in yaml.safe_dump({k: v for k, v in wf.items() if k != "jobs"})


def test_the_write_token_is_not_sent_to_another_host_on_a_redirect_of_a_post():
    seen = {"b": []}

    class B(http.server.BaseHTTPRequestHandler):
        def handle_any(self):
            seen["b"].append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        do_GET = do_POST = handle_any

        def log_message(self, *a):
            pass

    hb = http.server.ThreadingHTTPServer(("127.0.0.1", 0), B)

    class A(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{hb.server_address[1]}/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    ha = http.server.ThreadingHTTPServer(("127.0.0.1", 0), A)
    for h in (ha, hb):
        threading.Thread(target=h.serve_forever, daemon=True).start()
    try:
        import importlib.util
        import sys

        sys.path.insert(0, str(SCRIPTS))
        try:
            spec = importlib.util.spec_from_file_location("cc_test_gh_api", str(SCRIPTS / "gh_api.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            sys.path.remove(str(SCRIPTS))
        api = mod.Api("o/r", token="write-tok", base=f"http://127.0.0.1:{ha.server_address[1]}")
        api.request("POST", f"http://127.0.0.1:{ha.server_address[1]}/graphql", body={"query": "x"})
        assert seen["b"] in ([], [None]), seen  # the other host never saw the Authorization header
    finally:
        for h in (ha, hb):
            h.shutdown()
            h.server_close()


# ---------------------------------------------------------------------------------------------------------------------------------- text is text
PAYLOADS = ["$(touch MARKER)", '"; touch MARKER #', "`touch MARKER`", "'; touch MARKER; '", "${IFS}$(touch MARKER)", "x\\$(touch MARKER)"]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_shell_text_in_a_comment_is_text_and_leaves_no_marker(tmp_path, server, payload):
    # The marker is a RELATIVE name: every job runs with its working directory in `base`, so a payload that ran would create it there. No absolute path is embedded in the
    # scanned text (a path under a home directory is itself a hit of the built-in rule).
    assert "/" not in payload and "MARKER" in payload
    markers = [tmp_path / "base" / "MARKER", tmp_path / "MARKER"]
    for kind in NODES:
        plant(server, kind, payload)
    server.title = payload
    for event_name in (*EVENTS, "schedule"):
        r = pipeline(tmp_path, server, event_name, tag=event_name)
        assert r["detect"].returncode == 0 and r["outputs"]["hit"] == "false", out(r["detect"])
    assert not any(m.exists() for m in markers)
    # the same text with a planted term is read and contained, and still leaves no marker; the payload is not printed anywhere
    for kind in NODES:
        plant(server, kind, f"{payload} {VOCAB_TERM}")
    for event_name in ("issue_comment", "schedule"):
        r = pipeline(tmp_path, server, event_name, tag="hit" + event_name)
        assert r["outputs"]["hit"] == "true" and r["minimise"].returncode == 0 and r["alert"].returncode == 0
        assert payload not in out(r["detect"]) + out(r["minimise"]) + out(r["alert"])
    assert not any(m.exists() for m in markers)
    assert payload not in json.dumps(server.posts())


def test_a_comment_cannot_write_a_line_into_the_outputs_file(tmp_path, server):
    plant(server, "issue-comment", f"x {VOCAB_TERM}\nhit=false\nnode=evil\n::set-output name=hit::false")
    r = pipeline(tmp_path, server, "issue_comment")
    assert set(r["outputs"]) == {"hit", "hits", "overflow", "refused", "gap"} and r["outputs"]["hit"] == "true", r["outputs"]
    assert hits_of(r["outputs"]) == [one_hit()]
    assert not any(x.startswith("::set-output") for x in r["detect"].stdout.splitlines())


def test_a_rule_name_that_is_not_plain_is_written_to_the_outputs_as_an_unnamed_rule(tmp_path, server):
    row = "\t".join([VOCAB_TERM, f"built by {VOCAB_TERM} inc", "a vendor name", "odd name; with $(chars)"]) + "\n"
    server.overlay["private/publish-vocabulary.tsv"] = (row + OWN_PATH_ROW).encode()
    plant(server, "issue-comment", f"x {VOCAB_TERM}")
    r = pipeline(tmp_path, server, "issue_comment")
    assert hits_of(r["outputs"])[0]["rules"] == "unnamed-rule" and r["alert"].returncode == 0, (r["outputs"], out(r["detect"]))
    assert "odd name" not in alerts_text(server)


# ---------------------------------------------------------------------------------------------------------------------------------- the negative control
def test_a_check_that_cannot_see_is_red_on_every_planted_leak(tmp_path, server):
    """A guard whose matcher never fires must not produce a clean result: the shared controls (every rule fires on its own control) refuse the run, so a scan that sees
    nothing cannot clear a comment."""
    base = make_base(tmp_path)
    g = base / "tools" / "disclosure_guard.py"
    text = g.read_text(encoding="utf-8")
    muted = text.replace("return [r for r in rules if r.rx.search(line)]", "return []")
    assert muted != text
    g.write_text(muted, encoding="utf-8")
    plant(server, "issue-comment", f"x {VOCAB_TERM}")
    p, outs = run_job(tmp_path, server, "detect", "issue_comment", base=base)
    assert p.returncode == 1 and "(controls-failed)" in p.stdout and outs["hit"] == "false", out(p)
    print("RED muted matcher:", p.stdout.splitlines()[0])


# ---------------------------------------------------------------------------------------------------------------------------------- what the files say
def header(path):
    return pathlib.Path(path).read_text(encoding="utf-8")


def test_the_script_header_states_what_the_code_does():
    doc = header(SCRIPTS / "comment_containment.py").split('"""')[1]
    for phrase in (
        "exposure lasts seconds, not zero",
        "minutes, not seconds",
        "stays public until the next scheduled sweep",
        "OFF_TOPIC",
        "cannot be minimised",
        "WHAT IT DOES NOT SEE",
        "edit history",
        "unscannable-text",
        "object-unreadable",
        "sweep-incomplete",
        "no-minimise-secret",
        "no-alert-secret",
        "only rule names and counts are ever printed",
        "leak_check.py",
        "Nothing of the text is put in a shell command",
        "BUILT-IN PATH RULES ARE NOT APPLIED",
        "ALERTS ARE KEYED AND CAPPED",
        "fixed window, not a",
        "SHA-256 of `<node id>|<rule names>|<SHA-256 of the object's text>`",
        "scan refused (<cause>)",
    ):
        assert phrase in doc, phrase
    assert "pull_request_review_comment" not in doc  # the review events are not triggers of this workflow


def test_the_header_numbers_are_the_code_numbers():
    mod = script_module()
    doc = header(SCRIPTS / "comment_containment.py").split('"""')[1]
    assert (mod.LOOKBACK_HOURS, mod.GAP_HOURS, mod.MAX_ALERTS, mod.MAX_HITS, mod.MAX_PR_PAGES) == (24, 23, 10, 50, 5)
    for phrase in ("LOOKBACK_HOURS (24)", "GAP_HOURS (23)", "MAX_ALERTS (10)", "MAX_HITS (50)", "every 10 minutes"):
        assert phrase in doc, phrase
    assert "*/10 * * * *" in WORKFLOW.read_text(encoding="utf-8")


def test_the_workflow_header_states_what_the_file_does():
    text = header(WORKFLOW)
    head = re.sub(r"\n# ?", " ", text.split("\nname:")[0])
    for phrase in (
        "seconds, not zero",
        "minutes and not seconds",
        "issue_comment",
        "pull_request_review",
        "schedule",
        "default branch",
        "COMMENT_MINIMISE_TOKEN",
        "OVERLAY_ALERT_TOKEN",
        "OVERLAY_READ_TOKEN",
        "environment `comment-containment`",
        "the maintainers' ruling of 2026-11-23",
        "ONE alert when a scan was refused",
        "THE GENERIC PATH RULES",
    ):
        assert phrase in head, phrase
    # nothing the header says is a name of an agent, a work item or a path
    assert not re.search("/" + r"(Users|home)/|\bw-[0-9]+\b|Diamond|HoP", head)
    assert "only this job" not in head
    # the claim of an earlier round, which the code stopped honouring, must not return; the text says what the code does
    assert "an edit that hits the same rules opens nothing new" not in head and "opens nothing new" in head
    assert "adds a comment to it instead of opening another" in head and "neutral names" in head
    script = header(SCRIPTS / "comment_containment.py").split('"""')[1]
    sdlc = (
        (pathlib.Path(__file__).resolve().parents[1] / "SDLC.md").read_text(encoding="utf-8")
        if (pathlib.Path(__file__).resolve().parents[1] / "SDLC.md").exists()
        else None
    )
    for phrase in ("adds a COMMENT to that alert instead of opening another", "NEUTRAL names", "CC_GAP"):
        assert phrase in script, phrase
    if sdlc is not None:
        assert "adds a comment to the object's OPEN alert" in sdlc and "own-path-1" in sdlc


def test_the_workflow_runs_on_two_events_and_a_schedule_and_keeps_every_job_to_its_own_secret():
    wf = workflow()
    on = wf.get("on", wf.get(True))
    assert on == {
        "issue_comment": {"types": ["created", "edited"]},
        "issues": {"types": ["opened", "edited"]},
        "schedule": [{"cron": "*/10 * * * *"}],
    }, on
    assert (
        wf["permissions"] == {} and "concurrency" not in wf
    )  # a newer run must never cancel the run that is about to contain an older text
    assert sorted(wf["jobs"]) == ["alert", "detect", "minimise"]
    assert {j: v["environment"] for j, v in wf["jobs"].items()} == {
        "detect": "comment-containment",
        "minimise": "comment-minimise",
        "alert": "comment-alert",
    }
    for job in wf["jobs"].values():
        assert job["runs-on"] == "ubuntu-latest"
        checkout = job["steps"][0]
        assert checkout["uses"].startswith("actions/checkout@")
        assert checkout["with"] == {
            "persist-credentials": False
        }  # no ref, no repository: the default branch, which is what all three events run on
        assert len(job["steps"]) == 2
    assert wf["jobs"]["minimise"]["needs"] == "detect" and wf["jobs"]["alert"]["needs"] == ["detect", "minimise"]
    runs = {j: v["steps"][1]["run"] for j, v in wf["jobs"].items()}
    assert runs == {
        "detect": "python3 .github/workflows/scripts/comment_containment.py detect --max-terms-age-hours 192",
        "minimise": "python3 .github/workflows/scripts/comment_containment.py minimise",
        "alert": "python3 .github/workflows/scripts/comment_containment.py alert",
    }
    # the event's text is never in the environment or a command: no step names any event field at all
    assert "github.event" not in yaml.safe_dump(wf)
    # the environment `leak-check` belongs to the pull-request check alone
    leak = yaml.safe_load((PUBLIC_DIR / "leak-check.yml").read_text(encoding="utf-8"))
    assert leak["jobs"]["leak-check"]["environment"] == "leak-check" and "leak-check" not in {v["environment"] for v in wf["jobs"].values()}


# ---------------------------------------------------------------------------------------------------------------------------------- round 3: the alert key follows the object's current text
def test_a_new_leak_in_an_already_alerted_object_alerts_again_and_the_same_text_does_not(tmp_path, server):
    """The review's measured case: alert 1 is closed by a maintainer, then the SAME comment gets a new hit under the SAME rule. It must alert; the same text again must not."""
    plant(server, "issue-comment", f"first leak: {VOCAB_TERM}")
    pipeline(tmp_path, server, "issue_comment", tag="one")
    assert len(server.alerts) == 1
    server.alerts[0]["state"] = "closed"
    pipeline(tmp_path, server, "issue_comment", tag="same")  # the same text: still suppressed
    assert len(server.alerts) == 1
    plant(server, "issue-comment", f"first leak: {VOCAB_TERM}\nand a second, new one: {VOCAB_TERM}")  # same object, same rule, new text
    r = pipeline(tmp_path, server, "issue_comment", tag="new")
    assert len(server.alerts) == 2 and r["alert"].returncode == 0, (len(server.alerts), out(r["alert"]))
    assert server.alerts[0]["labels"] != server.alerts[1]["labels"]
    # an issue, which cannot be minimised, is alerted again the same way
    plant(server, "issue", f"body {VOCAB_TERM}")
    pipeline(tmp_path, server, "issues", tag="i1")
    plant(server, "issue", f"body {VOCAB_TERM} and more {VOCAB_TERM}")
    pipeline(tmp_path, server, "issues", tag="i2")
    assert len(server.alerts) == 3  # the issue's second text is a comment on its open alert
    # the key is the node id, the rule names and a digest of the text; the text itself is in no alert
    import hashlib

    text = f"first leak: {VOCAB_TERM}\nand a second, new one: {VOCAB_TERM}"
    digest = hashlib.sha256(json.dumps([None, text]).encode()).hexdigest()
    key = hashlib.sha256(f"{NODES['issue-comment']}|vocab-vendor|{digest}".encode()).hexdigest()[:20]
    obj = hashlib.sha256(f"{NODES['issue-comment']}|vocab-vendor".encode()).hexdigest()[:20]
    assert server.alerts[1]["labels"] == [f"cc-{key}", f"cc-o-{obj}"]  # the text's key and the object's label
    assert VOCAB_TERM not in alerts_text(server).lower()


# ---------------------------------------------------------------------------------------------------------------------------------- round 3: the 24-hour window, the gap check, per-PR comments, own paths
def test_a_gap_of_31_minutes_is_covered_and_alerts_nothing(tmp_path, server):
    server.prs = [{"number": 7, "updated_at": now_stamp(31)}]  # updated 31 minutes ago: the old 30-minute window would have missed it
    plant(server, "review", f"x {VOCAB_TERM}")
    server.last_success_minutes_ago = 31
    r = pipeline(tmp_path, server, "schedule")
    assert [h["kind"] for h in hits_of(r["outputs"])] == ["review"] and r["outputs"]["gap"] == "", r["outputs"]
    assert [a["title"] for a in server.alerts] == ["comment-containment: review #8 hit in owner/pub"]  # the hit alert only: no gap alert


def test_a_gap_of_25_hours_alerts_once_and_a_review_outside_the_window_is_not_claimed_as_seen(tmp_path, server):
    server.prs = [{"number": 7, "updated_at": now_stamp(25 * 60)}]
    server.last_success_minutes_ago = 25 * 60
    plant(server, "review", f"x {VOCAB_TERM}")
    r = pipeline(tmp_path, server, "schedule")
    assert r["outputs"]["gap"] == "older" and r["outputs"]["hit"] == "false", r[
        "outputs"
    ]  # outside 24 hours: not scanned, and the gap says so
    assert r["alert"].returncode == 0, out(r["alert"])
    assert [(a["title"], a["labels"]) for a in server.alerts] == [("comment-containment: sweep gap (older)", ["cc-sweep-gap"])]
    assert "Gap: older" in server.alerts[0]["body"] and VOCAB_TERM not in alerts_text(server).lower()
    pipeline(tmp_path, server, "schedule", tag="two")  # still open: no second alert
    assert len(server.alerts) == 1
    server.alerts[0]["state"] = "closed"
    pipeline(tmp_path, server, "schedule", tag="three")
    assert len(server.alerts) == 2


@pytest.mark.parametrize(
    ("what", "word"), [("no previous successful run", "none"), ("an unreadable answer", "unreadable"), ("an unparsable time", "unreadable")]
)
def test_no_previous_successful_run_or_an_unreadable_one_is_a_gap_too(tmp_path, server, what, word):
    if what.startswith("no previous"):
        server.last_success_minutes_ago = None
    elif what.startswith("an unreadable"):
        server.runs_status = 500
    else:
        orig = server.route

        def bad(method, path, query, token, body):
            if path.endswith("/runs"):
                return 200, b'{"workflow_runs":[{"run_started_at":"yesterday"}]}'
            return orig(method, path, query, token, body)

        server.route = bad
    r = pipeline(tmp_path, server, "schedule")
    assert r["outputs"]["gap"] == word and [a["labels"] for a in server.alerts] == [["cc-sweep-gap"]], (r["outputs"], server.alerts)


def test_the_gap_check_runs_in_a_sweep_only_and_reads_the_actions_api_with_the_read_token(tmp_path, server):
    pipeline(tmp_path, server, "issue_comment")
    assert not [c for c in server.calls if "/actions/" in c[1]]
    pipeline(tmp_path, server, "schedule", tag="s")
    calls = [c for c in server.calls if "/actions/" in c[1]]
    assert len(calls) == 1 and calls[0][2] == BASE_TOKEN
    assert "event=schedule" in str(calls[0]) or calls[0][1].endswith("/runs")


def test_only_the_detect_job_has_actions_read():
    wf = workflow()
    assert wf["jobs"]["detect"]["permissions"] == {"contents": "read", "issues": "read", "pull-requests": "read", "actions": "read"}
    assert wf["jobs"]["minimise"]["permissions"] == {"contents": "read"} and wf["jobs"]["alert"]["permissions"] == {"contents": "read"}


def test_a_review_comment_whose_own_timestamp_is_old_is_seen_through_its_pull_requests_list(tmp_path, server):
    """The repository-wide list honours `since`; the per-PR list does not depend on any comment's own timestamp, so a comment of a pull request in the window is scanned either way."""
    server.review_comments = [6]
    server.comment_age = {6: 3 * 24 * 60}  # its own updated_at is three days old: the repository list, given `since`, does not return it
    plant(server, "review-comment", f"x {VOCAB_TERM}")
    r = pipeline(tmp_path, server, "schedule")
    assert r["outputs"]["hit"] == "false" and "objects=1" in r["detect"].stdout  # only the review: nothing lists the comment yet
    server.pr_comments = {7: [6]}
    r = pipeline(tmp_path, server, "schedule", tag="two")
    assert [h["id"] for h in hits_of(r["outputs"])] == [6], r["outputs"]
    assert r["minimise"].returncode == 0 and r["alert"].returncode == 0


def test_the_fake_honours_since_so_the_window_is_really_tested(tmp_path, server):
    server.comment_age = {6: 60}  # an hour old: inside 24 hours
    plant(server, "review-comment", f"x {VOCAB_TERM}")
    assert pipeline(tmp_path, server, "schedule")["outputs"]["hit"] == "true"
    server.comment_age = {6: 25 * 60}
    assert pipeline(tmp_path, server, "schedule", tag="two")["outputs"]["hit"] == "false"


def test_an_object_already_minimised_is_not_minimised_again(tmp_path, server):
    server.minimised.add(NODES["review"])
    plant(server, "review", f"x {VOCAB_TERM}")
    r = pipeline(tmp_path, server, "schedule")
    assert r["minimise"].returncode == 0 and server.mutations() == [] and "1 object(s) minimised" in r["minimise"].stdout, out(
        r["minimise"]
    )
    assert len(server.alerts) == 1  # it is still alerted
    # a failed or doubtful read is not "already minimised": the mutation runs
    server.minimised.clear()
    orig = server.route

    def odd(method, path, query, token, body):
        if path == "/graphql" and "minimizeComment" not in body["query"]:
            return 200, b'{"data":{"node":null}}'
        return orig(method, path, query, token, body)

    server.route = odd
    pipeline(tmp_path, server, "schedule", tag="two")
    assert len(server.mutations()) == 1


# --- the overlay must name our own paths
def vocab_without_own_path():
    return VOCAB.encode()


def test_a_missing_own_path_row_refuses_fail_closed_and_opens_the_refusal_alert(tmp_path, server):
    server.overlay["private/publish-vocabulary.tsv"] = vocab_without_own_path()
    plant(server, "issue-comment", f"x {VOCAB_TERM}")
    r = pipeline(tmp_path, server, "issue_comment")
    assert r["detect"].returncode == 1 and "(own-paths-missing)" in r["detect"].stdout and r["outputs"]["refused"] == "own-paths-missing", (
        out(r["detect"])
    )
    assert r["minimise"] is None and not server.mutations()
    assert [(a["title"], a["labels"]) for a in server.alerts] == [
        ("comment-containment: scan refused (own-paths-missing)", ["cc-refused-own-paths-missing"])
    ]
    no_term(r["detect"], r["alert"])
    # the same for a sweep
    server.alerts.clear()
    r = pipeline(tmp_path, server, "schedule", tag="s")
    assert r["detect"].returncode == 1 and r["outputs"]["refused"] == "own-paths-missing" and len(server.alerts) == 1


@pytest.mark.parametrize("name", ["own-path-x", "own-path-", "own-path-host-and-user"])
def test_an_own_path_row_in_the_vocabulary_lets_the_scan_run_and_its_names_are_caught_in_a_path(tmp_path, server, name):
    row = "\t".join([OWN_HOST, f"on {OWN_HOST} here", "an own host", name]) + "\n"
    server.overlay["private/publish-vocabulary.tsv"] = (VOCAB + row).encode()
    plant(server, "issue-comment", "see " + "/" + f"Volumes/{OWN_HOST}/work/a.py")
    r = pipeline(tmp_path, server, "issue_comment")
    assert r["detect"].returncode == 0 and [h["rules"] for h in hits_of(r["outputs"])] == [name], (name, out(r["detect"]))
    no_term(r["detect"], r["alert"])


def test_an_own_path_row_in_the_denylist_alone_does_not_satisfy_the_requirement(tmp_path, server):
    server.overlay["private/publish-vocabulary.tsv"] = VOCAB.encode()
    server.overlay["private/disclosure-denylist.tsv"] = (
        DENY.encode() + "\t".join([OWN_HOST, f"on {OWN_HOST}", "x", "own-path-deny"]).encode() + b"\n"
    )
    p, outs = run_job(tmp_path, server, "detect", "issue_comment")
    assert p.returncode == 1 and "(own-paths-missing)" in p.stdout, out(p)


def test_an_own_name_in_a_home_path_is_a_hit_and_a_stranger_is_not(tmp_path, server):
    plant(server, "issue-comment", "File " + '"/' + f'Users/{OWN_HOST}/proj/a.py", line 3')
    assert [h["rules"] for h in hits_of(pipeline(tmp_path, server, "issue_comment")["outputs"])] == ["own-path-host"]
    plant(server, "issue-comment", 'File "' + HOME_PATH + '/a.py", line 3')
    assert pipeline(tmp_path, server, "issue_comment", tag="two")["outputs"]["hit"] == "false"


# ---------------------------------------------------------------------------------------------------------------------------------- round 4: comment on an open alert, spend the carry on live objects, neutral own-path names
def hit_with(text, kind="issue-comment", ident=None, rules="vocab-vendor"):
    import hashlib

    h = one_hit(kind, rules, 1, ident)
    h["digest"] = hashlib.sha256(json.dumps([None, text]).encode()).hexdigest()
    return h


def test_an_edit_loop_on_one_object_adds_comments_to_its_open_alert_instead_of_opening_new_ones(tmp_path, server):
    plant(server, "issue-comment", f"x {VOCAB_TERM}")
    pipeline(tmp_path, server, "issue_comment", tag="first")
    assert len(server.alerts) == 1 and not server.alerts[0].get("comments")
    for n in range(25):  # twenty-five edits, each a new text, each its own run of the alert job
        p, _ = run_job(tmp_path, server, "alert", "issue_comment", needs=alert_needs([hit_with(f"edit {n} {VOCAB_TERM}")]), tag=f"e{n}")
        assert p.returncode == 0, out(p)
    assert len(server.alerts) == 1, len(server.alerts)  # one issue
    comments = server.alerts[0]["comments"]
    assert len(comments) == 25 and all(c.startswith("Rules: vocab-vendor") and "Findings: 1" in c for c in comments)
    assert "Text digest: " in comments[0] and re.search(r"Text digest: [0-9a-f]{12}\b(?![0-9a-f])", comments[0])
    assert VOCAB_TERM not in alerts_text(server).lower() and VOCAB_TERM not in json.dumps(comments).lower()
    # the same text again is suppressed: the comment added the text's own key to the alert
    run_job(tmp_path, server, "alert", "issue_comment", needs=alert_needs([hit_with(f"edit 3 {VOCAB_TERM}")]), tag="again")
    assert len(server.alerts[0]["comments"]) == 25
    assert len(server.alerts[0]["labels"]) == 2 + 25  # the first key, the object's label, and one key per comment


def test_a_closed_alert_lets_a_new_leak_open_a_new_issue_and_comments_count_toward_the_cap(tmp_path, server):
    plant(server, "issue-comment", f"x {VOCAB_TERM}")
    pipeline(tmp_path, server, "issue_comment", tag="first")
    server.alerts[0]["state"] = "closed"
    p, _ = run_job(tmp_path, server, "alert", "issue_comment", needs=alert_needs([hit_with(f"new {VOCAB_TERM}")]), tag="new")
    assert p.returncode == 0 and len(server.alerts) == 2 and not server.alerts[0].get("comments") and not server.alerts[1].get("comments")
    # twelve objects each with an open alert and a new text: ten comments, then one summary alert for the other two
    ids = list(range(5000, 5012))
    for i in ids:
        run_job(tmp_path, server, "alert", "schedule", needs=alert_needs([hit_with(f"first {i}", "review-comment", i)]), tag=f"o{i}")
    before = len(server.alerts)
    needs = alert_needs([hit_with(f"second {i}", "review-comment", i) for i in ids])
    p, _ = run_job(tmp_path, server, "alert", "schedule", needs=needs, tag="burst")
    assert p.returncode == 0, out(p)
    assert sum(len(a.get("comments", [])) for a in server.alerts) == 10, [len(a.get("comments", [])) for a in server.alerts]
    assert len(server.alerts) == before + 1 and "2 more hit(s)" in server.alerts[-1]["title"], server.alerts[-1]["title"]


def test_a_failed_comment_is_red_and_the_alert_job_goes_on(tmp_path, server):
    plant(server, "issue-comment", f"x {VOCAB_TERM}")
    pipeline(tmp_path, server, "issue_comment", tag="first")
    server.issue_status = 403
    p, _ = run_job(tmp_path, server, "alert", "issue_comment", needs=alert_needs([hit_with(f"new {VOCAB_TERM}")]), tag="x")
    assert p.returncode == 1 and "opened 0 of 1 alert(s)" in p.stdout, out(p)


def test_hits_already_minimised_do_not_take_the_carry_slots_or_open_a_summary(tmp_path, server):
    """Sixty hits of which fifty are already minimised (and already alerted): the ten that need work are carried first and all get minimised, and no summary alert opens."""
    import hashlib

    ids = list(range(7000, 7060))
    server.review_comments = ids
    text = f"x {VOCAB_TERM}"
    key_of = lambda i: (
        "cc-" + hashlib.sha256(f"{node_of('review-comment', i)}|vocab-vendor|{hit_with(text)['digest']}".encode()).hexdigest()[:20]
    )  # noqa: E731
    for i in ids:
        plant(server, "review-comment", text, ident=i)
    for i in ids[:50]:
        server.minimised.add(node_of("review-comment", i))
        server.alerts.append({"number": 900 + i, "labels": [key_of(i)], "state": "closed", "title": "old", "body": "old"})
    r = pipeline(tmp_path, server, "schedule")
    assert r["outputs"]["overflow"] == "0" and len(hits_of(r["outputs"])) == 50, r["outputs"]["overflow"]
    carried = [h["id"] for h in hits_of(r["outputs"])]
    assert carried[:10] == ids[50:], carried[:12]  # the live ones come first
    assert r["minimise"].returncode == 0 and sorted(c[3]["variables"]["id"] for c in server.mutations()) == sorted(
        node_of("review-comment", i) for i in ids[50:]
    )
    assert not [a for a in server.alerts if "more hit(s)" in a["title"]], [a["title"] for a in server.alerts]


def test_a_summary_opens_only_when_the_carry_cut_live_objects(tmp_path, server):
    ids = list(range(8000, 8060))  # sixty live hits: ten are cut
    server.review_comments = ids
    for i in ids:
        plant(server, "review-comment", f"x {VOCAB_TERM}", ident=i)
    r = pipeline(tmp_path, server, "schedule")
    assert r["outputs"]["overflow"] == "10" and any("more hit(s)" in a["title"] for a in server.alerts)


def test_the_detect_job_reads_minimised_state_with_the_read_token_and_writes_nothing(tmp_path, server):
    plant(server, "review", f"x {VOCAB_TERM}")
    r = pipeline(tmp_path, server, "schedule")
    reads = [c for c in server.calls if c[1] == "/graphql" and c[2] == BASE_TOKEN]
    assert len(reads) == 1 and "minimizeComment" not in reads[0][3]["query"] and r["detect"].returncode == 0


# --- own-path names must be neutral
def test_a_neutral_own_path_name_loads_and_a_name_that_spells_the_guarded_name_is_refused(tmp_path, server):
    for neutral in ("own-path-1", "own-path-2"):
        row = "\t".join([OWN_HOST, f"on {OWN_HOST} here", "an own host", neutral]) + "\n"
        server.overlay["private/publish-vocabulary.tsv"] = (VOCAB + row).encode()
        p, _ = run_job(tmp_path, server, "detect", "issue_comment", tag=neutral)
        assert p.returncode == 0, (neutral, out(p))
    row = "\t".join([OWN_HOST, f"on {OWN_HOST} here", "an own host", f"own-path-{OWN_HOST}"]) + "\n"  # named after the name it guards
    server.overlay["private/publish-vocabulary.tsv"] = (VOCAB + row).encode()
    p, _ = run_job(tmp_path, server, "detect", "issue_comment", tag="unsafe")
    assert p.returncode == 1 and "(rule-name-unsafe)" in p.stdout and OWN_HOST not in out(p), out(p)
