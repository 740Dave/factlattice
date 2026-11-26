"""The required leak check over a pull request's content (scripts/leak_check.py, run by the public workflow leak-check.yml).

Every term and every rule here is SYNTHETIC (`quillworks`, `marblefoundry`, `zorpbot`, `frumpkin-lib`): nothing real is read, and a planted "leak" is a made-up
string. The GitHub API is a FAKE, a stub HTTP server on 127.0.0.1 that this file starts; nothing here contacts GitHub. The script runs as a subprocess, the way the
workflow runs it (cwd = a base checkout holding the guard and the staged scripts, environment as the workflow sets it, the event as a JSON file), so what is
measured is the same command line. A payload planted in a title only ever writes a marker file; it never runs git or touches the network.

What each group shows, and what the green of each cannot show:
  * a planted leak in a file, the title, the body and a commit message is refused, naming the rule and never the term (RED, exit 1);
  * a clean PR passes, with the mode line REQUIRED and a fresh sha first;
  * a missing secret, an overlay the token cannot read, an absent / stale / unparsable / future-dated / partial terms file, an absent or empty vocabulary or denylist FAIL
    CLOSED, each with a mode line `REQUIRED, REFUSED (<cause>)` and no PR read at all;
  * shell text in the title is handled as text: the marker a payload would write is absent afterwards, and the same title with a planted term is still refused.
Each of these fails when the check is made mute: see `test_a_check_that_cannot_see_is_red_on_every_planted_leak` (a run with the rules emptied is not a pass).
"""

import base64
import datetime
import hashlib
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

import pytest
import yaml

from tests._snapshot import is_published_snapshot, skip_reason

ROOT = pathlib.Path(__file__).resolve().parents[1]
PUBLIC_DIR = (
    ROOT / "publish" / "github" / "workflows" if (ROOT / "publish" / "github" / "workflows").is_dir() else ROOT / ".github" / "workflows"
)
SCRIPTS = PUBLIC_DIR / "scripts"
GUARD = ROOT / "tools" / "disclosure_guard.py"
T = "\t"
REPO, OVERLAY = "owner/pub", "owner/overlay"
BASE_TOKEN, SECRET = "base-read-token", "overlay-read-token"
VOCAB_TERM, DENY_TERM, AGENT, CORPUS = "quillworks", "marblefoundry", "zorpbot", "frumpkin-lib"
VOCAB = T.join([VOCAB_TERM, f"built by {VOCAB_TERM} inc", "a vendor name", "vocab-vendor"]) + "\n"
DENY = T.join([DENY_TERM, f"a {DENY_TERM} order", "a synthetic customer", "deny-customer"]) + "\n"
TERMS_PATH = "private/neutrality-terms.json"
SHA1 = "1" * 40
SHA2 = "2" * 40
NOW = datetime.datetime.now(datetime.UTC).replace(microsecond=0)


def stamp(offset_s=0):
    return (NOW + datetime.timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def terms_doc(generated_at=None, hand=None, sha=None, agents=None, corpora=None):
    agents, corpora = sorted(agents or [AGENT]), sorted(corpora or [CORPUS])
    digest = hashlib.sha256(
        json.dumps({"agent_ids": agents, "corpus_ids": corpora}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "_header": {
            "generated_at": stamp(-60) if generated_at is None else generated_at,
            "generator": "x",
            "sources": {"hand_additions": {"agent_ids": 1, "corpus_ids": 1} if hand is None else hand},
            "sha256": digest if sha is None else sha,
        },
        "agent_ids": agents,
        "corpus_ids": corpora,
    }


# ---------------------------------------------------------------------------------------------------------------------------------- the fake API
class Fake:
    """An in-memory model of the GitHub API's read routes this script uses. `calls` records (method, path, token) for every request."""

    def __init__(self):
        self.calls = []
        self.pr = {
            "title": "a clean title",
            "body": "a clean body",
            "commits": [("a" * 40, "a clean message")],
            "files": {"src/a.py": "x = 1\n"},
            "head_repo": REPO,
            "branch": "feature/clean",
            "author": ("A Person", "a@example.test"),
            "committer": ("B Person", "b@example.test"),
            "head_sha": SHA1,
        }
        self.overlay = {
            "private/publish-vocabulary.tsv": VOCAB.encode(),
            "private/disclosure-denylist.tsv": DENY.encode(),
            TERMS_PATH: json.dumps(terms_doc()).encode(),
        }
        self.overlay_status = {}  # path -> forced status
        self.changed_files_override = None
        self.commits_override = None
        self.removed = set()
        self.unreadable_blobs = set()
        self.null_commits = False
        self.move_head_after_files = False

    def blob_sha(self, path):
        return hashlib.sha1(path.encode()).hexdigest()

    def pr_json(self):
        pr = self.pr
        return {
            "title": pr["title"],
            "body": pr["body"],
            "head": {"sha": pr["head_sha"], "ref": pr["branch"], "repo": {"full_name": pr["head_repo"]} if pr["head_repo"] else None},
            "commits": len(pr["commits"]) if self.commits_override is None else self.commits_override,
            "changed_files": len(pr["files"]) if self.changed_files_override is None else self.changed_files_override,
        }

    def route(self, method, path, query, token):
        self.calls.append((method, path, token))
        m = re.fullmatch(rf"/repos/({re.escape(OVERLAY)})/contents/(.+)", path)
        if m:
            if token != SECRET:
                return 401, b'{"message":"Bad credentials"}', "application/json"
            rel = m.group(2)
            if rel in self.overlay_status:
                return self.overlay_status[rel], b'{"message":"x"}', "application/json"
            if rel not in self.overlay:
                return 404, b'{"message":"Not Found"}', "application/json"
            return 200, self.overlay[rel], "application/vnd.github.raw"
        if token != BASE_TOKEN:
            return 401, b'{"message":"Bad credentials"}', "application/json"
        if path == f"/repos/{REPO}/pulls/7":
            return 200, json.dumps(self.pr_json()).encode(), "application/json"
        page = int((query.get("page") or ["1"])[0])
        if path == f"/repos/{REPO}/pulls/7/commits" and self.null_commits:
            return 200, b"null", "application/json"
        if path == f"/repos/{REPO}/pulls/7/commits":

            def who(t):
                return {"name": t[0], "email": t[1], "date": "2026-01-01T00:00:00Z"}

            items = [
                {"sha": s, "commit": {"message": m_, "author": who(self.pr["author"]), "committer": who(self.pr["committer"])}}
                for s, m_ in self.pr["commits"]
            ]
            return 200, json.dumps(items[(page - 1) * 100 : page * 100]).encode(), "application/json"
        if path == f"/repos/{REPO}/pulls/7/files":
            items = [
                # no `size` here: GitHub's pull-request files answer carries none
                {"filename": f, "status": "removed" if f in self.removed else "added", "sha": self.blob_sha(f)}
                for f in self.pr["files"]
            ]
            if self.move_head_after_files:
                self.pr["head_sha"] = SHA2  # a push lands while the lists are being read
            return 200, json.dumps(items[(page - 1) * 100 : page * 100]).encode(), "application/json"
        b = re.fullmatch(rf"/repos/{re.escape(self.pr['head_repo'] or 'none/none')}/git/blobs/([0-9a-f]{{40}})", path)
        if b:
            for f, c in self.pr["files"].items():
                if self.blob_sha(f) == b.group(1) and f not in self.unreadable_blobs:
                    raw = c if isinstance(c, bytes) else c.encode()
                    body = {"encoding": "base64", "content": base64.b64encode(raw).decode(), "size": len(raw)}
                    return 200, json.dumps(body).encode(), "application/json"
        return 404, b'{"message":"Not Found"}', "application/json"


@pytest.fixture
def server():
    fake = Fake()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import parse_qs, urlsplit

            u = urlsplit(self.path)
            token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            status, body, ctype = fake.route("GET", u.path, parse_qs(u.query), token)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # any write is recorded and refused: this check only reads
            fake.calls.append(("POST", self.path, ""))
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_PUT = do_PATCH = do_DELETE = do_POST

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    fake.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield fake
    httpd.shutdown()
    httpd.server_close()


# ---------------------------------------------------------------------------------------------------------------------------------- running the script
def make_base(tmp_path, agents=(), guard=GUARD):
    """A base checkout as the workflow sees it: the guard, the staged scripts and (optionally) agents/<stem>.md files. Nothing of the PR is in it."""
    base = tmp_path / "base"
    (base / "tools").mkdir(parents=True, exist_ok=True)
    shutil.copy(guard, base / "tools" / "disclosure_guard.py")
    (base / ".github" / "workflows" / "scripts").mkdir(parents=True, exist_ok=True)
    for f in ("leak_check.py", "gh_api.py"):
        shutil.copy(SCRIPTS / f, base / ".github" / "workflows" / "scripts" / f)
    if agents:
        (base / "agents").mkdir(exist_ok=True)
        for a in agents:
            (base / "agents" / f"{a}.md").write_text("# a shipped agent\n", encoding="utf-8")
    return base


def workflow_run_text():
    wf = yaml.safe_load((PUBLIC_DIR / "leak-check.yml").read_text(encoding="utf-8"))
    return next(s["run"] for s in wf["jobs"]["leak-check"]["steps"] if "run" in s)


def run_check(tmp_path, fake, secret=SECRET, extra_env=None, base=None, number=7, event=None, run_text=None):
    """Run the workflow's own run text under bash in the base checkout, with the environment the workflow's `env:` gives it. -> CompletedProcess."""
    base = base or make_base(tmp_path)
    ev = tmp_path / "event.json"
    ev.write_text(
        json.dumps(
            event
            if event is not None
            else {"action": "opened", "pull_request": {"number": number, "title": "NOT READ FROM HERE", "head": {"sha": SHA1}}}
        ),
        encoding="utf-8",
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "TMPDIR": os.environ.get("TMPDIR", str(tmp_path)),
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_EVENT_PATH": str(ev),
        "GITHUB_API_URL": fake.url,
        "GITHUB_TOKEN": BASE_TOKEN,
        "OVERLAY_READ_TOKEN": secret,
        "LC_OVERLAY_REPO": OVERLAY,
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", "-e", "-c", run_text or workflow_run_text()], cwd=str(base), env=env, capture_output=True, text=True, timeout=120
    )


def listed(p):
    return [x.strip() for x in p.stdout.splitlines()]


def out(p):
    return p.stdout + p.stderr


def no_term(p):
    text = out(p).lower()
    for t in (VOCAB_TERM, DENY_TERM, AGENT, CORPUS):
        assert t.lower() not in text, f"a term is in the output: {t!r}"


MODE_RE = re.compile(r"^neutrality terms: REQUIRED, sha256 [0-9a-f]{64}, generated_at \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ, max age \d+ h$")


# ---------------------------------------------------------------------------------------------------------------------------------- a planted leak is refused, naming the rule
PLANTS = {
    "file": (
        "deny-customer",
        lambda f: f.pr["files"].update({"docs/notes.md": f"line one\nan order for {DENY_TERM} here\n"}),
        "file docs/notes.md:2  [deny-customer]",
    ),
    "title": ("vocab-vendor", lambda f: f.pr.update(title=f"fix the {VOCAB_TERM} case"), "title  [vocab-vendor]"),
    "body": ("vocab-vendor", lambda f: f.pr.update(body=f"fine\nsecond line names {VOCAB_TERM}\n"), "body:2  [vocab-vendor]"),
    "commit": (
        "deny-customer",
        lambda f: f.pr.update(commits=[("a" * 40, "fine"), ("b" * 40, f"subject\n\nbody for {DENY_TERM}")]),
        "commit " + "b" * 12 + ":3  [deny-customer]",
    ),
    "generated agent id in the title": (
        "neutrality-agent-id",
        lambda f: f.pr.update(title=f"ask {AGENT} about it"),
        "title  [neutrality-agent-id]",
    ),
    "generated corpus id in a file": (
        "neutrality-corpus-id",
        lambda f: f.pr["files"].update({"x/y.txt": f"see {CORPUS} for more\n"}),
        "file x/y.txt:1  [neutrality-corpus-id]",
    ),
    "structural rule in a file": (
        "absolute-home-path",
        lambda f: f.pr["files"].update({"c.py": 'ROOT = "/Us' + 'ers/jdoe/checkout"\n'}),
        "file c.py:1  [absolute-home-path]",
    ),
}


@pytest.mark.parametrize("where", sorted(PLANTS))
def test_a_planted_leak_is_refused_naming_the_rule_and_never_the_term(tmp_path, server, where):
    rule, plant, line = PLANTS[where]
    plant(server)
    p = run_check(tmp_path, server)
    assert p.returncode == 1, out(p)
    lines = p.stdout.splitlines()
    assert MODE_RE.match(lines[0]), lines[0]
    assert line in listed(p), (line, lines)
    assert any(x.startswith("RESULT: RED. 1 finding(s). Per rule: {'%s': 1}." % rule) for x in lines), lines
    assert "jdoe" not in out(p)  # the matched span of a structural rule is not printed either
    no_term(p)
    print("RED leak-check:", where, "->", line)


def test_a_planted_path_is_masked_where_the_path_itself_is_the_hit(tmp_path, server):
    server.pr["files"].update({f"docs/{VOCAB_TERM}-notes.md": "clean content\n"})
    p = run_check(tmp_path, server)
    assert p.returncode == 1, out(p)
    assert "file docs/" + "*" * len(VOCAB_TERM) + "-notes.md:0  [vocab-vendor]" in listed(p), p.stdout
    no_term(p)


def test_a_file_name_cannot_inject_a_workflow_command_or_a_line(tmp_path, server):
    server.pr["files"].update({f"a::error::x\n::set-output name=y::z {DENY_TERM}.txt": "clean\n"})
    p = run_check(tmp_path, server)
    assert p.returncode == 1, out(p)
    printed = [x for x in p.stdout.splitlines() if x.startswith("  file ")]
    assert printed and all(re.fullmatch(r"  file [A-Za-z0-9._/*?-]+:\d+  \[[a-z-]+\]", x) for x in printed), printed
    assert not any(x.startswith("::set-output") or x.startswith("::error::x") for x in p.stdout.splitlines())


def test_every_leak_in_every_input_is_counted_and_listed_by_rule(tmp_path, server):
    for where in ("file", "title", "body", "commit"):
        PLANTS[where][1](server)
    p = run_check(tmp_path, server)
    assert p.returncode == 1
    result = next(x for x in p.stdout.splitlines() if x.startswith("RESULT: RED."))
    assert "4 finding(s)" in result and "'deny-customer': 2" in result and "'vocab-vendor': 2" in result, result
    no_term(p)


# ---------------------------------------------------------------------------------------------------------------------------------- a clean PR passes
def test_a_clean_pr_passes_with_the_required_mode_line_and_a_fresh_sha_first(tmp_path, server):
    p = run_check(tmp_path, server)
    assert p.returncode == 0, out(p)
    lines = p.stdout.splitlines()
    assert MODE_RE.match(lines[0]), lines[0]
    doc = terms_doc()
    assert (
        lines[0]
        == f"neutrality terms: REQUIRED, sha256 {doc['_header']['sha256']}, generated_at {json.loads(server.overlay[TERMS_PATH])['_header']['generated_at']}, max age 192 h"
    )
    assert any(
        x.startswith(
            "leak-check scope: files-changed=1 files-scanned=1 files-unscannable=0 commits=1 title=1 body=1 branch=1 commit-identities=2;"
        )
        for x in lines
    ), lines
    assert lines[-1].startswith("RESULT: green.") and "lexical tripwire" in lines[-1]
    no_term(p)


@pytest.mark.skipif(is_published_snapshot(), reason=skip_reason("tools/release_prep.py"))
def test_the_mode_line_is_release_preps_required_line_word_for_word(tmp_path, server):
    spec = importlib.util.spec_from_file_location("lc_mode_src", str(ROOT / "tools" / "release_prep.py"))
    src = pathlib.Path(spec.origin).read_text(encoding="utf-8")
    release = re.search(r'^LINE_REQUIRED = "([^"]+)"', src, re.M).group(1)
    ours = re.search(r'^LINE_REQUIRED = "([^"]+)"', (SCRIPTS / "leak_check.py").read_text(encoding="utf-8"), re.M).group(1)
    assert release == ours == "neutrality terms: REQUIRED, sha256 {sha}, generated_at {at}"
    refused = re.search(r'^LINE_REFUSED = "([^"]+)"', (SCRIPTS / "leak_check.py").read_text(encoding="utf-8"), re.M).group(1)
    assert refused == "neutrality terms: REQUIRED, REFUSED ({cause})"  # release_prep.mode_refusal_line's required form


def test_a_shipped_agent_name_is_not_a_leak_but_a_term_that_is_not_shipped_still_is(tmp_path, server):
    base = make_base(tmp_path, agents=[AGENT])
    server.pr["title"] = f"document {AGENT}'s definition"
    p = run_check(tmp_path, server, base=base)
    assert p.returncode == 0, out(p)
    assert "1 generated agent id(s) exempt as shipped product names" in p.stdout  # the guard's own line, by name
    server.pr["body"] = f"and {CORPUS} too"
    p = run_check(tmp_path, server, base=base)
    assert p.returncode == 1 and "body:1  [neutrality-corpus-id]" in listed(p), out(p)


def test_a_removed_file_is_not_read_and_a_pr_of_many_files_is_paged(tmp_path, server):
    server.pr["files"] = {f"d/f{i}.txt": "ok\n" for i in range(130)}
    server.removed = {"d/f3.txt"}
    p = run_check(tmp_path, server)
    assert p.returncode == 0, out(p)
    assert "files-changed=130 files-scanned=129" in p.stdout


# ---------------------------------------------------------------------------------------------------------------------------------- fail closed
def test_a_missing_secret_fails_closed_naming_the_secret_and_reads_nothing(tmp_path, server):
    for value in ("", "   "):
        p = run_check(tmp_path, server, secret=value)
        assert p.returncode == 1, out(p)
        lines = p.stdout.splitlines()
        assert lines[0] == "neutrality terms: REQUIRED, REFUSED (no-overlay-secret)", lines
        assert "OVERLAY_READ_TOKEN" in p.stdout and "check fails rather than pass without them" in p.stdout
        assert not any(x.startswith("RESULT: green") for x in lines)
        assert server.calls == [], server.calls  # not one request: no overlay read and no PR read
    print("RED missing secret:", p.stdout.splitlines()[0])


def test_the_secret_variable_absent_from_the_environment_is_the_same_failure(tmp_path, server):
    base = make_base(tmp_path)
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps({"pull_request": {"number": 7, "head": {"sha": SHA1}}}))
    env = {
        "PATH": os.environ["PATH"],
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_EVENT_PATH": str(ev),
        "GITHUB_API_URL": server.url,
        "GITHUB_TOKEN": BASE_TOKEN,
    }
    p = subprocess.run(["bash", "-e", "-c", workflow_run_text()], cwd=str(base), env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 1 and p.stdout.splitlines()[0] == "neutrality terms: REQUIRED, REFUSED (no-overlay-secret)", out(p)


def set_terms(server, doc):
    server.overlay[TERMS_PATH] = doc if isinstance(doc, bytes) else json.dumps(doc).encode()


CAUSES = {
    "terms file absent": (lambda s: s.overlay.pop(TERMS_PATH), "missing"),
    "terms file stale": (lambda s: set_terms(s, terms_doc(generated_at=stamp(-193 * 3600))), "stale"),
    "terms file future-dated": (lambda s: set_terms(s, terms_doc(generated_at=stamp(7200))), "future-dated"),
    "terms file unparsable": (lambda s: set_terms(s, b"{not json"), "unparsable"),
    "terms file not the shape": (lambda s: set_terms(s, {"agent_ids": []}), "shape"),
    "terms file sha256 does not match its lists": (lambda s: set_terms(s, terms_doc(sha="0" * 64)), "sha256-mismatch"),
    "terms file written without hand additions": (lambda s: set_terms(s, terms_doc(hand="NONE (file absent)")), "partial"),
    "vocabulary file absent": (lambda s: s.overlay.pop("private/publish-vocabulary.tsv"), "missing"),
    "denylist file absent": (lambda s: s.overlay.pop("private/disclosure-denylist.tsv"), "missing"),
    "vocabulary file empty": (lambda s: s.overlay.update({"private/publish-vocabulary.tsv": b"# only a comment\n"}), "rules-unusable"),
    "denylist row that misses its own control": (
        lambda s: s.overlay.update({"private/disclosure-denylist.tsv": T.join(["abc", "xyz", "why", "bad"]).encode() + b"\n"}),
        "rules-unusable",
    ),
    "vocabulary and denylist are the same content": (
        lambda s: s.overlay.update({"private/publish-vocabulary.tsv": DENY.encode()}),
        "rules-unusable",
    ),
    "the token is refused for the terms file": (lambda s: s.overlay_status.update({TERMS_PATH: 403}), "overlay-unreadable"),
    "the overlay answers 401 for every file": (
        lambda s: s.overlay_status.update(
            {k: 401 for k in ("private/publish-vocabulary.tsv", "private/disclosure-denylist.tsv", TERMS_PATH)}
        ),
        "overlay-unreadable",
    ),
    "a file that is not text": (lambda s: s.overlay.update({"private/disclosure-denylist.tsv": b"\xff\xfe\x00\x01"}), "overlay-unreadable"),
}


@pytest.mark.parametrize("what", sorted(CAUSES))
def test_an_unusable_overlay_fails_closed_with_its_cause_and_never_reads_the_pr(tmp_path, server, what):
    mutate, cause = CAUSES[what]
    mutate(server)
    server.pr["title"] = f"{VOCAB_TERM} planted, but the rules never loaded"
    p = run_check(tmp_path, server)
    assert p.returncode == 1, out(p)
    lines = p.stdout.splitlines()
    assert lines[0] == f"neutrality terms: REQUIRED, REFUSED ({cause})", lines
    assert not any(x.startswith("RESULT: green") for x in lines)
    assert not any("/pulls/" in c[1] for c in server.calls), server.calls
    no_term(p)
    print("RED fail closed:", what, "->", lines[0])


def test_a_stale_terms_file_is_refused_only_past_the_guards_own_limit(tmp_path, server):
    set_terms(server, terms_doc(generated_at=stamp(-191 * 3600)))  # inside the workflow's 192-hour limit
    p = run_check(tmp_path, server)
    assert p.returncode == 0, out(p)


def test_an_optional_contexts_table_that_is_absent_does_not_fail_and_one_that_is_present_is_used(tmp_path, server):
    p = run_check(tmp_path, server)
    assert p.returncode == 0 and "contexts" not in p.stdout.split("RESULT")[0].lower().replace("neutrality term contexts", "")
    server.overlay["private/neutrality-term-contexts.json"] = json.dumps({"zorp": [r"(?!(?<=\.)(?i:zorp_store)(?![A-Za-z0-9]))"]}).encode()
    p = run_check(tmp_path, server)
    assert p.returncode == 0, out(p)


@pytest.mark.parametrize(
    ("what", "mutate", "cause"),
    [
        ("more commits than the list returns", lambda s: setattr(s, "commits_override", 300), "commits-incomplete"),
        ("more files than the list returns", lambda s: setattr(s, "changed_files_override", 3001), "files-incomplete"),
        ("a head repository that is gone", lambda s: s.pr.update(head_repo=None), "pr-unreadable"),
    ],
)
def test_a_pr_that_cannot_be_read_in_full_fails_closed(tmp_path, server, what, mutate, cause):
    mutate(server)
    p = run_check(tmp_path, server)
    assert p.returncode == 1, out(p)
    assert MODE_RE.match(p.stdout.splitlines()[0])  # the rules loaded; the refusal is about the PR
    assert f"({cause})" in p.stdout and "RESULT: green" not in p.stdout, out(p)


def test_a_file_the_api_cannot_return_is_a_finding_not_a_pass(tmp_path, server):
    server.pr["files"]["gone.txt"] = "x\n"
    server.unreadable_blobs = {"gone.txt"}  # the listing names a blob the blob route will not return
    p = run_check(tmp_path, server)
    assert p.returncode == 1 and "file gone.txt:0  [unscannable-file]" in listed(p), out(p)


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"pull_request": {}},
        {"pull_request": {"number": "7"}},
        {"pull_request": {"number": 0}},
        {"pull_request": {"number": True}},
        {"pull_request": {"number": 7}},  # no head sha: the event cannot be compared with the API
        {"pull_request": {"number": 7, "head": {"sha": "xyz"}}},
    ],
)
def test_a_malformed_event_fails_closed(tmp_path, server, event):
    p = run_check(tmp_path, server, event=event)
    assert p.returncode == 1 and "(bad-input)" in p.stdout, out(p)
    assert server.calls == []


def test_the_guards_environment_variables_cannot_move_a_rule_file(tmp_path, server):
    """A runner's own FACTLATTICE_* variables must not point the guard at another rules file: the overlay's files are the only ones read."""
    other = tmp_path / "other.tsv"
    other.write_text(T.join(["anything", "anything", "x", "other-rule"]) + "\n")
    server.pr["title"] = "anything at all"
    extra = {
        "FACTLATTICE_DISCLOSURE_RULES": str(other),
        "FACTLATTICE_PUBLISH_VOCAB": str(other),
        "FACTLATTICE_NEUTRALITY_TERMS": str(other),
    }
    # the contexts table is the one input the guard reads from the environment when no path is passed: a runner variable naming a missing file must not refuse the run
    extra["FACTLATTICE_NEUTRALITY_CONTEXTS"] = str(tmp_path / "no-such-contexts.json")
    p = run_check(tmp_path, server, extra_env=extra)
    assert p.returncode == 0 and "other-rule" not in out(p), out(p)


# ---------------------------------------------------------------------------------------------------------------------------------- read as data
def test_the_check_only_reads_and_each_token_goes_only_where_it_belongs(tmp_path, server):
    PLANTS["title"][1](server)
    p = run_check(tmp_path, server)
    assert p.returncode == 1
    assert server.calls and {c[0] for c in server.calls} == {"GET"}
    overlay_calls = [c for c in server.calls if f"/repos/{OVERLAY}/" in c[1]]
    pr_calls = [c for c in server.calls if f"/repos/{REPO}/" in c[1]]
    assert overlay_calls and pr_calls and len(overlay_calls) + len(pr_calls) == len(server.calls)
    assert {c[2] for c in overlay_calls} == {SECRET} and {c[2] for c in pr_calls} == {BASE_TOKEN}
    assert SECRET not in out(p) and BASE_TOKEN not in out(p)
    # the PR was read through the pull request, commits, files and blob routes only
    kinds = {re.sub(r"[0-9a-f]{40}", "<sha>", c[1]).replace("/7", "/<n>") for c in pr_calls}
    assert kinds == {
        f"/repos/{REPO}/pulls/<n>",
        f"/repos/{REPO}/pulls/<n>/commits",
        f"/repos/{REPO}/pulls/<n>/files",
        f"/repos/{REPO}/git/blobs/<sha>",
    }, kinds


def test_a_fork_heads_blobs_are_read_from_the_head_repository_with_the_base_token(tmp_path, server):
    server.pr["head_repo"] = "someone/pub"
    PLANTS["file"][1](server)
    p = run_check(tmp_path, server)
    assert p.returncode == 1 and "file docs/notes.md:2  [deny-customer]" in listed(p), out(p)
    assert any(c[1].startswith("/repos/someone/pub/git/blobs/") and c[2] == BASE_TOKEN for c in server.calls)


# a title that is shell syntax. The payload writes a marker file ONLY; it never runs git and never touches the network.
PAYLOADS = ["$(touch MARKER)", '"; touch MARKER #', "`touch MARKER`", "'; touch MARKER; '", "${IFS}$(touch MARKER)", "x\\$(touch MARKER)"]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_shell_text_in_the_title_body_and_commit_message_is_text_and_leaves_no_marker(tmp_path, server, payload):
    # The marker is a RELATIVE name: the check runs with its working directory in `base`, so a payload that ran would create it there. No absolute path is embedded in the
    # scanned text (a path under a home directory is itself a hit of the built-in absolute-home-path rule, which would make this test depend on where tmp_path sits).
    cmd = payload
    assert "/" not in cmd and "MARKER" in cmd
    markers = [tmp_path / "base" / "MARKER", tmp_path / "MARKER"]
    server.pr["title"] = cmd
    server.pr["body"] = cmd
    server.pr["commits"] = [("a" * 40, cmd)]
    server.pr["files"]["pay.txt"] = cmd + "\n"
    p = run_check(tmp_path, server)
    assert p.returncode == 0 and p.stdout.splitlines()[-1].startswith("RESULT: green."), out(p)  # clean text passes
    assert not any(m.exists() for m in markers)
    # the same text with a planted term is read (not skipped) and refused, still with no marker
    server.pr["title"] = f"{cmd} {VOCAB_TERM}"
    p = run_check(tmp_path, server)
    assert p.returncode == 1 and "title  [vocab-vendor]" in listed(p), out(p)
    assert not any(m.exists() for m in markers)
    assert cmd not in out(p)
    print("no marker:", payload)


def test_the_workflow_never_puts_pr_text_in_the_environment_or_a_command():
    wf = yaml.safe_load((PUBLIC_DIR / "leak-check.yml").read_text(encoding="utf-8"))
    step = next(s for s in wf["jobs"]["leak-check"]["steps"] if "run" in s)
    assert step["run"] == "python3 .github/workflows/scripts/leak_check.py --max-terms-age-hours 192"
    assert sorted(step["env"]) == ["GITHUB_TOKEN", "LC_OVERLAY_REPO", "OVERLAY_READ_TOKEN"]
    assert not any("github.event" in str(v) for v in step["env"].values())


# ---------------------------------------------------------------------------------------------------------------------------------- the negative control
def test_a_check_that_cannot_see_is_red_on_every_planted_leak(tmp_path, server):
    """The control of the controls: a guard whose matcher never fires must not produce a pass. Here the guard copy in the base checkout has its predicate made mute; the
    script's own controls (every rule fires on its own control) refuse the run, so a check that sees nothing cannot report green."""
    base = make_base(tmp_path)
    g = base / "tools" / "disclosure_guard.py"
    text = g.read_text(encoding="utf-8")
    muted = text.replace("return [r for r in rules if r.rx.search(line)]", "return []")
    assert muted != text
    g.write_text(muted, encoding="utf-8")
    PLANTS["title"][1](server)
    p = run_check(tmp_path, server, base=base)
    assert p.returncode == 1 and "(controls-failed)" in p.stdout, out(p)
    assert not any(x.startswith("RESULT: green") for x in p.stdout.splitlines())
    print("RED muted matcher:", p.stdout.splitlines()[0])


# ---------------------------------------------------------------------------------------------------------------------------------- the fix round
def scripts_module(name):
    spec = importlib.util.spec_from_file_location(f"lc_test_{name}", str(SCRIPTS / f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(SCRIPTS))
    return mod


# --- a rule name must never print a term
def vocab_with(extra):
    return (VOCAB + extra).encode()


UNSAFE = {
    "a vocabulary row named after its own term": lambda s: s.overlay.update(
        {"private/publish-vocabulary.tsv": vocab_with(T.join(["zzterm", "a zzterm here", "x", "zzterm-rule"]) + "\n")}
    ),
    "a denylist row named after a term of the vocabulary": lambda s: s.overlay.update(
        {
            "private/disclosure-denylist.tsv": DENY.encode()
            + T.join(["otherthing", "an otherthing", "x", f"{VOCAB_TERM}-customer"]).encode()
            + b"\n"
        }
    ),
}


@pytest.mark.parametrize("what", sorted(UNSAFE))
def test_a_rule_whose_name_is_matched_by_a_loaded_rule_refuses_with_a_count_never_the_name(tmp_path, server, what):
    UNSAFE[what](server)
    server.pr["title"] = f"a title naming {VOCAB_TERM} and zzterm"
    p = run_check(tmp_path, server)
    assert p.returncode == 1, out(p)
    lines = p.stdout.splitlines()
    assert lines[0] == "neutrality terms: REQUIRED, REFUSED (rule-name-unsafe)", lines
    assert re.search(r"\b[1-9][0-9]* rule name\(s\) are matched by a loaded rule", p.stdout), p.stdout
    for leak in ("zzterm", f"{VOCAB_TERM}-customer", "neutrality-agent-id", "vocab-vendor", "deny-customer"):
        assert leak not in out(p), leak
    assert not any("/pulls/" in c[1] for c in server.calls)
    print("RED rule-name-unsafe:", what, "->", lines[0])


def test_a_row_refused_at_load_never_prints_its_name(tmp_path, server):
    server.overlay["private/disclosure-denylist.tsv"] = T.join(["abc", "xyz", "why", "leakyname"]).encode() + b"\n"
    p = run_check(tmp_path, server)
    assert p.returncode == 1 and "(rules-unusable)" in p.stdout, out(p)
    assert "leakyname" not in out(p)


def test_a_green_run_has_rule_names_that_no_rule_matches(tmp_path, server):
    p = run_check(tmp_path, server)
    assert p.returncode == 0 and "rule-name-unsafe" not in out(p)


# --- binary, compressed and non-UTF-8 content never passes green
import gzip  # noqa: E402

TEXT_WITH_TERM = f"first line\nsecond line names {VOCAB_TERM}\n"
BINARIES = {
    "a gzip file holding a term": (gzip.compress(f"order for {DENY_TERM}\n".encode()), "unscannable-file", 0),
    "a zip archive": (b"PK\x03\x04" + b"\x00" * 40, "unscannable-file", 0),
    "a bzip2 file": (b"BZh9" + b"1AY&SY" + b"\x00" * 20, "unscannable-file", 0),
    "an xz file": (b"\xfd7zXZ\x00" + b"\x00" * 20, "unscannable-file", 0),
    "a zstd file": (b"\x28\xb5\x2f\xfd" + b"\x00" * 20, "unscannable-file", 0),
    "a 7z archive": (b"7z\xbc\xaf\x27\x1c" + b"\x00" * 20, "unscannable-file", 0),
    "a png-like binary": (b"\x89PNG\r\n\x1a\n" + bytes(range(128, 200)), "unscannable-file", 0),
    "latin-1 text (not UTF-8)": (b"caf\xe9 " + VOCAB_TERM.encode() + b"\n", "unscannable-file", 0),
    "UTF-16 with NULs and no byte order mark": (TEXT_WITH_TERM.encode("utf-16-le"), "unscannable-file", 0),
    "text with a NUL byte": (b"clean\x00text\n", "unscannable-file", 0),
    "UTF-16 (little endian, with a mark) holding a term": (TEXT_WITH_TERM.encode("utf-16"), "vocab-vendor", 2),
    "UTF-16 (big endian, with a mark) holding a term": (b"\xfe\xff" + TEXT_WITH_TERM.encode("utf-16-be"), "vocab-vendor", 2),
    "UTF-32 (with a mark) holding a term": (TEXT_WITH_TERM.encode("utf-32"), "vocab-vendor", 2),
    "UTF-8 with a mark holding a term on its first line": (b"\xef\xbb\xbf" + f"{VOCAB_TERM} first\n".encode(), "vocab-vendor", 1),
    "UTF-16 with a mark that is cut in the middle of a character": (b"\xff\xfe" + b"a\x00b", "unscannable-file", 0),
}


@pytest.mark.parametrize("what", sorted(BINARIES))
def test_binary_compressed_and_non_utf8_content_is_never_green(tmp_path, server, what):
    data, rule, line = BINARIES[what]
    server.pr["files"] = {"assets/data.bin": data}
    p = run_check(tmp_path, server)
    assert p.returncode == 1, out(p)
    assert f"file assets/data.bin:{line}  [{rule}]" in listed(p), listed(p)
    assert "RESULT: green" not in p.stdout
    no_term(p)
    print("RED not green:", what, "->", f"{rule}:{line}")


def test_clean_utf8_with_a_mark_and_clean_utf16_pass_and_the_scope_line_counts_what_it_could_not_read(tmp_path, server):
    server.pr["files"] = {"a.txt": b"\xef\xbb\xbfclean\n", "b.txt": "clean\n".encode("utf-16"), "c.txt": b"", "d.txt": "café clean\n"}
    p = run_check(tmp_path, server)
    assert p.returncode == 0, out(p)
    assert "files-changed=4 files-scanned=4 files-unscannable=0" in p.stdout
    server.pr["files"]["e.bin"] = gzip.compress(b"x")
    p = run_check(tmp_path, server)
    assert p.returncode == 1 and "files-changed=5 files-scanned=5 files-unscannable=1" in p.stdout, out(p)


def test_a_masked_path_names_the_unscannable_file(tmp_path, server):
    server.pr["files"] = {f"docs/{VOCAB_TERM}.gz": gzip.compress(b"x")}
    p = run_check(tmp_path, server)
    assert "file docs/" + "*" * len(VOCAB_TERM) + ".gz:0  [unscannable-file]" in listed(p) and "[vocab-vendor]" in p.stdout, out(p)
    no_term(p)


# --- the branch name and each commit's author and committer
def test_the_head_branch_name_is_scanned(tmp_path, server):
    server.pr["branch"] = f"fix/{VOCAB_TERM}-export"
    p = run_check(tmp_path, server)
    assert p.returncode == 1 and "branch  [vocab-vendor]" in listed(p), out(p)
    no_term(p)


IDENTITIES = {
    "author name": (
        lambda s: s.pr.update(author=(f"{DENY_TERM} Person", "a@example.test")),
        "commit " + "a" * 12 + " author  [deny-customer]",
    ),
    "author email": (
        lambda s: s.pr.update(author=("A Person", f"a@{DENY_TERM}.example")),
        "commit " + "a" * 12 + " author  [deny-customer]",
    ),
    "committer name": (
        lambda s: s.pr.update(committer=(f"{VOCAB_TERM} Person", "b@example.test")),
        "commit " + "a" * 12 + " committer  [vocab-vendor]",
    ),
    "committer email": (
        lambda s: s.pr.update(committer=("B Person", f"b@{VOCAB_TERM}.example")),
        "commit " + "a" * 12 + " committer  [vocab-vendor]",
    ),
}


@pytest.mark.parametrize("what", sorted(IDENTITIES))
def test_each_commits_author_and_committer_name_and_email_are_scanned(tmp_path, server, what):
    plant, line = IDENTITIES[what]
    plant(server)
    p = run_check(tmp_path, server)
    assert p.returncode == 1 and line in listed(p), out(p)
    no_term(p)
    print("RED identity:", what, "->", line)


# --- the head the event names is the head the API reports
def test_a_head_that_moved_since_the_event_fails_closed(tmp_path, server):
    server.pr["head_sha"] = SHA2
    p = run_check(tmp_path, server)
    assert p.returncode == 1 and "(head-moved)" in p.stdout and "RESULT: green" not in p.stdout, out(p)
    assert not any(c[1].endswith("/commits") or "/files" in c[1] for c in server.calls), server.calls  # nothing of the moved head was read


# --- limits fail closed, with the shapes GitHub really sends
def test_a_blob_longer_than_the_limit_is_unscannable_over_the_real_wire_shape(tmp_path, server):
    mod = scripts_module("leak_check")
    server.pr["files"] = {"big.txt": b"a" * (mod.MAX_BLOB_BYTES + 10)}
    p = run_check(tmp_path, server)
    assert p.returncode == 1 and "file big.txt:0  [unscannable-file]" in listed(p), out(p)


def test_the_length_limit_in_process_with_a_small_limit_and_a_blob_that_has_no_size(tmp_path):
    mod = scripts_module("leak_check")
    guard = mod.load_guard(ROOT)
    mod.MAX_BLOB_BYTES = 10

    class Api:
        def get(self, path, **kw):
            return 200, {"encoding": "base64", "content": base64.b64encode(b"x" * 11).decode()}  # no `size` field

    nload = type("N", (), {"rules": []})()
    pr = {"title": "", "body": "", "branch": "b", "commits": [], "files": [("a.txt", "added", "1" * 40)]}
    found, scanned = mod.scan_pr(guard, list(guard.BUILTIN_RULES), nload, Api(), pr)
    assert found == [("file", "a.txt", 0, "unscannable-file")] and scanned == 1


@pytest.mark.parametrize("which", ["commits", "count"])
def test_a_null_or_odd_page_refuses_cleanly_without_a_traceback(tmp_path, server, which):
    if which == "commits":
        server.null_commits = True
    else:
        server.changed_files_override = "many"  # a non-integer count from the API
    p = run_check(tmp_path, server)
    assert p.returncode == 1 and "Traceback" not in out(p), out(p)
    assert "leak-check refused" in p.stdout and "::error::leak-check refused" in p.stdout, out(p)


# --- the token does not follow a redirect to another host
def test_the_token_is_not_sent_to_another_host_on_a_redirect_and_is_kept_on_the_same_host():
    seen = {"b": [], "a": []}

    class B(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["b"].append(self.headers.get("Authorization"))
            body = b'{"where": "b"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    hb = http.server.ThreadingHTTPServer(("127.0.0.1", 0), B)
    port_b = hb.server_address[1]

    class A(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["a"].append(self.headers.get("Authorization"))
            if self.path.startswith("/repos/o/r/hop"):
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{port_b}/elsewhere")
            elif self.path.startswith("/repos/o/r/same"):
                self.send_response(302)
                self.send_header("Location", "/repos/o/r/landing")
            else:
                body = b'{"where": "a"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    ha = http.server.ThreadingHTTPServer(("127.0.0.1", 0), A)
    for h in (ha, hb):
        threading.Thread(target=h.serve_forever, daemon=True).start()
    try:
        gh_api = scripts_module("gh_api")
        api = gh_api.Api("o/r", token="tok", base=f"http://127.0.0.1:{ha.server_address[1]}")
        status, d = api.get("/hop")
        assert status == 200 and d == {"where": "b"} and seen["b"] == [None], seen  # the other host got no Authorization
        status, d = api.get("/same")
        assert status == 200 and d == {"where": "a"}
        assert seen["a"][-1] == "Bearer tok", seen  # the same host keeps it
    finally:
        for h in (ha, hb):
            h.shutdown()
            h.server_close()


# --- the age limit of the terms
def at_age(hours):
    return terms_doc(generated_at=stamp(-int(hours * 3600)))


def test_the_workflow_passes_192_hours_and_191_passes_193_refuses_and_the_mode_line_prints_the_limit(tmp_path, server):
    set_terms(server, at_age(191))
    p = run_check(tmp_path, server)
    assert p.returncode == 0 and p.stdout.splitlines()[0].endswith(", max age 192 h"), out(p)
    set_terms(server, at_age(193))
    p = run_check(tmp_path, server)
    assert p.returncode == 1 and p.stdout.splitlines()[0] == "neutrality terms: REQUIRED, REFUSED (stale)", out(p)
    assert "the limit is 691200 s" in p.stdout
    print("RED stale at 193 h:", p.stdout.splitlines()[0])


def test_without_the_flag_the_guards_own_six_hour_limit_applies_and_the_line_says_so(tmp_path, server):
    bare = "python3 .github/workflows/scripts/leak_check.py"
    set_terms(server, at_age(5))
    p = run_check(tmp_path, server, run_text=bare)
    assert p.returncode == 0 and p.stdout.splitlines()[0].endswith(", max age 6 h"), out(p)
    set_terms(server, at_age(7))
    p = run_check(tmp_path, server, run_text=bare)
    assert p.returncode == 1 and "(stale)" in p.stdout.splitlines()[0]


@pytest.mark.parametrize("value", ["0", "-3", "nan", "inf"])
def test_an_age_limit_that_is_not_a_positive_finite_number_refuses(tmp_path, server, value):
    p = run_check(tmp_path, server, run_text=f"python3 .github/workflows/scripts/leak_check.py --max-terms-age-hours={value}")
    assert p.returncode == 1 and "(bad-input)" in p.stdout and server.calls == [], out(p)


# ---------------------------------------------------------------------------------------------------------------------------------- round three
@pytest.mark.parametrize("word", ["home", "agent", "path", "linux"])
def test_a_generated_id_equal_to_a_word_of_a_fixed_rule_name_does_not_refuse_the_run(tmp_path, server, word):
    """The names of the built-in and generated rules are fixed text of the base code; only overlay row names are checked."""
    set_terms(server, terms_doc(agents=[word], corpora=[CORPUS]))
    p = run_check(tmp_path, server)
    assert p.returncode == 0 and "rule-name-unsafe" not in out(p), out(p)


@pytest.mark.parametrize(
    "name",
    [
        f"x] {VOCAB_TERM}",
        f"x] does not match its own control] {VOCAB_TERM}",
        f"[x]] {VOCAB_TERM} [",
    ],
)
def test_a_row_refused_at_load_with_a_bracket_in_its_name_never_prints_any_of_the_name(tmp_path, server, name):
    server.overlay["private/disclosure-denylist.tsv"] = T.join(["abc", "xyz", "why", name]).encode() + b"\n"
    p = run_check(tmp_path, server)
    assert p.returncode == 1 and "(rules-unusable)" in p.stdout, out(p)
    assert VOCAB_TERM not in out(p) and "x]" not in out(p), out(p)
    assert "rule [name withheld] does not match its own control" in p.stdout


def test_a_head_that_moves_while_the_lists_are_read_fails_closed(tmp_path, server):
    server.move_head_after_files = True
    p = run_check(tmp_path, server)
    assert p.returncode == 1 and "(head-moved)" in p.stdout and "RESULT: green" not in p.stdout, out(p)
    assert sum(1 for c in server.calls if c[1] == f"/repos/{REPO}/pulls/7") == 2  # read once before the lists and once after


def test_the_header_states_what_the_code_does():
    """Every sentence of the header of the script is pinned for the points that went stale once: what is read, how a file is decoded, the mode line, the known limits."""
    doc = (SCRIPTS / "leak_check.py").read_text(encoding="utf-8").split('"""')[1]
    for phrase in (
        "head branch name",
        "author and committer",
        "`pull_request.number` and `pull_request.head.sha`",
        "UTF-16 or UTF-32 with a byte order mark",
        "`unscannable-file`",
        "max age <N> h",
        "release_prep's required line, plus the age limit this run applied",
        "--max-terms-age-hours",
        "KNOWN LIMITS",
        "spells a term differently from its pattern",
        "fork owner's login",
    ):
        assert phrase in doc, phrase
    for stale in ("scanned as the bytes it decodes to", "It does not read the author or committer", "only `pull_request.number` is read"):
        assert stale not in doc, stale
