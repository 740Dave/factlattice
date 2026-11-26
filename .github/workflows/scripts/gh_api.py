"""Tiny GitHub REST client for the CI scripts (stdlib only; one token, GITHUB_TOKEN; no secrets are printed)."""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
MAX_PAGES = 50


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect, but never carry the Authorization header to another host (urllib copies every header onto the redirected request)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc.lower() not in ("", urllib.parse.urlsplit(req.full_url).netloc.lower()):
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


urllib.request.install_opener(urllib.request.build_opener(_SameHostRedirect))  # urlopen (below) uses it


class Api:
    def __init__(self, repo, token=None, base=API):
        self.repo = repo
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not self.token:
            raise SystemExit("gh_api: no GITHUB_TOKEN / GH_TOKEN in the environment (fail loud: nothing was attempted)")
        self.base = base

    def request(self, method, path, body=None, accept="application/vnd.github+json", raw=False):
        """Return (status, parsed-json-or-text). Never raises on an HTTP error status (the caller decides); retries a 5xx twice,
        except PUT .../merge and POST .../dispatches, which are never re-sent."""
        url = path if path.startswith("http") else f"{self.base}/repos/{self.repo}{path}"
        data = None if body is None else json.dumps(body).encode()
        # A merge is not idempotent and the queue re-reads the base right before it: re-sending it seconds later would skip that re-read.
        # A dispatch is not idempotent either: a 5xx may have landed, and a second call would start a second run.
        tail = path.split("?")[0]
        tries = 1 if (method == "PUT" and tail.endswith("/merge")) or (method == "POST" and tail.endswith("/dispatches")) else 3
        for attempt in range(tries):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("Accept", accept)
            req.add_header("X-GitHub-Api-Version", "2022-11-28")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    status, text = r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                status, text = e.code, e.read().decode()
            except urllib.error.URLError as e:
                if attempt == tries - 1:
                    raise SystemExit(f"gh_api: {method} {url}: {e}") from e
                time.sleep(2 * (attempt + 1))
                continue
            if status >= 500 and attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            if raw:
                return status, text
            try:
                return status, (json.loads(text) if text else None)
            except ValueError:
                return status, text
        return status, text

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def pages(self, path):
        """All pages of a list endpoint (100 per page, at most MAX_PAGES: a loud stop beyond that, never an endless loop)."""
        out = []
        sep = "&" if "?" in path else "?"
        for n in range(1, MAX_PAGES + 1):
            status, d = self.get(f"{path}{sep}per_page=100&page={n}")
            if status != 200:
                raise SystemExit(f"gh_api: GET {path} page {n}: HTTP {status}: {str(d)[:300]}")
            out.extend(d)
            if len(d) < 100:
                return out
        raise SystemExit(f"gh_api: GET {path}: more than {MAX_PAGES} pages (fail loud)")
