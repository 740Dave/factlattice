#!/usr/bin/env python3
"""CI check `gitleaks`: a gitleaks scan for SECRETS ONLY, with gitleaks' built-in default rules.

    ci_gitleaks.py [--tree DIR] [--history-base REF] [--require-history]
        exit 0 clean; 1 a finding (the findings are printed, secrets redacted); 2 the scan could not run (no binary, no network, a bad base, no files)

There is deliberately NO scan for private names or terms here: that runs only at publication (spec 42, G2).

WHAT IS AND IS NOT READ FROM THE PR'S TREE (round 3, F-A). The scan reads NO gitleaks configuration, ignore file or baseline that the PR controls:
  - config: always our own `--config` (a scratch file holding only `[extend] useDefault = true`), so a `.gitleaks.toml` in the PR is never loaded;
  - ignore file: our own empty `--gitleaks-ignore-path`; and gitleaks loads `<scanned dir>/.gitleaksignore` ANYWAY when scanning a git repository, so the
    history scan runs on a `git clone --no-checkout` of the tree (no working-tree file exists to load), and the tree scan stages `.gitleaksignore` and
    `.gitleaks.toml` under other names (they are still scanned, as text, for secrets);
  - inline `gitleaks:allow` comments: ignored in both scans (`--ignore-gitleaks-allow`), so a token on a line carrying the comment is still a finding.
What IS read from the PR tree: the files and commits being scanned, and nothing else. This script itself is the PR tree's copy (tools/ci_*.py is on the full
path: a PR that edits it decides its own result).

Two scans: (1) the tree: every file git lists as tracked or untracked-not-ignored, copied into a scratch folder (so the .venv and caches a CI job makes inside
the tree are not scanned and a new file in a PR is); (2) the history of the PR (F11): `gitleaks git --log-opts "-m BASE..HEAD"` on the clone, so a secret added in
one commit and removed in the next is caught too, including one added in a merge commit's own conflict resolution (`-m`: git shows no diff for a merge otherwise). BASE is --history-base, else the BASE_REF environment variable (ci.yml and the merge queue set it). With
--require-history (the CI form) a missing or unresolvable BASE is exit 2; without it the history scan is announced as skipped.
The binary is the pinned release below, downloaded from GitHub and checked against the sha256 recorded here (from the release's own checksums file, 2026-11-19);
GITLEAKS_BIN names a binary to use instead (it is then NOT checked, and its version is printed). A scan that could not run is exit 2, never a pass.
"""
import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request

VERSION = "8.30.1"
SHA256 = {  # asset -> sha256, from gitleaks_8.30.1_checksums.txt of the v8.30.1 release
    "darwin_arm64": "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
    "linux_x64": "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    "linux_arm64": "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080",
}
URL = "https://github.com/gitleaks/gitleaks/releases/download/v{v}/gitleaks_{v}_{a}.tar.gz"


class CouldNotRun(SystemExit):
    """The scan could not run (exit 2): not a finding (exit 1). A SystemExit so callers that exit on it do so with code 2."""

    def __init__(self, message):
        super().__init__(2)
        self.message = message

    def __str__(self):
        return self.message


ARCH = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}


def asset_name(system=None, machine=None):
    system = (system or platform.system()).lower()
    arch = ARCH.get((machine or platform.machine()).lower())
    name = f"{system}_{arch}"
    if name not in SHA256:
        raise CouldNotRun(f"ci_gitleaks: no pinned gitleaks {VERSION} for {name} (known: {sorted(SHA256)}); set GITLEAKS_BIN (scan NOT run)")
    return name


def fetch_binary(workdir, fetch=None):
    """Download the pinned release into workdir, verify its sha256, extract the `gitleaks` member only. -> the binary's path."""
    asset = asset_name()
    url = URL.format(v=VERSION, a=asset)
    tgz = os.path.join(workdir, "gitleaks.tar.gz")
    try:
        if fetch:
            fetch(url, tgz)
        else:
            with urllib.request.urlopen(url, timeout=120) as r, open(tgz, "wb") as fh:
                shutil.copyfileobj(r, fh)
    except (OSError, urllib.error.URLError) as e:  # no network (F-G): the scan could not run, which is exit 2 and not a finding
        raise CouldNotRun(f"ci_gitleaks: could not download {url}: {type(e).__name__}: {e} (scan NOT run)") from e
    got = hashlib.sha256(open(tgz, "rb").read()).hexdigest()
    if got != SHA256[asset]:
        raise CouldNotRun(f"ci_gitleaks: sha256 of {url} is {got}, pinned {SHA256[asset]} (scan NOT run)")
    with tarfile.open(tgz) as t:
        m = t.getmember("gitleaks")
        m.name = "gitleaks"
        t.extract(m, workdir, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))  # one named member, written under workdir
    path = os.path.join(workdir, "gitleaks")
    os.chmod(path, 0o755)
    return path


GITLEAKS_OWN_FILES = (".gitleaksignore", ".gitleaks.toml", ".gitleaksbaseline")  # files gitleaks itself would load from a scanned directory


def stage_tree(tree, dest):
    """Copy the files git lists (tracked + untracked-not-ignored) into dest. -> the number of files copied."""
    out = subprocess.run(
        ["git", "-C", tree, "ls-files", "-z", "--cached", "--others", "--exclude-standard"], capture_output=True, check=True
    ).stdout
    n = 0
    for rel in filter(None, out.decode("utf-8", "surrogateescape").split("\0")):
        src = os.path.join(tree, rel)
        if os.path.islink(src) or not os.path.isfile(src):
            continue  # a symlink or a deleted tracked file: nothing to read
        dst = os.path.join(dest, rel)
        if os.path.basename(rel) in GITLEAKS_OWN_FILES:  # still scanned as text, but under a name gitleaks never loads
            dst = os.path.join(os.path.dirname(dst), os.path.basename(dst).lstrip(".") + ".staged-not-loaded")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        n += 1
    return n


DEFAULT_CONFIG = "[extend]\nuseDefault = true\n"  # the built-in rules, as a file we own (passing --config stops any auto-load from the tree)


def scan_args(work):
    """The flags that make a scan independent of the scanned tree: our config, an empty ignore file (both in the scratch folder), and inline
    `gitleaks:allow` comments ignored."""
    cfg, ign = os.path.join(work, "gitleaks.toml"), os.path.join(work, "gitleaksignore")
    with open(cfg, "w") as fh:
        fh.write(DEFAULT_CONFIG)
    open(ign, "w").close()
    return ["--config", cfg, "--gitleaks-ignore-path", ign, "--no-banner", "--redact", "--exit-code", "1", "--verbose", "--ignore-gitleaks-allow"]


def history_scan(binary, tree, base, work):
    """-> exit code of `gitleaks git` over BASE..HEAD (0 clean, 1 finding, 2 could not run), run on a `git clone --no-checkout` of the tree.
    The clone has no working-tree file, so no `.gitleaksignore` (or any other file the PR controls) exists for gitleaks to load. BASE and HEAD are
    resolved in the tree and passed as shas (a remote-tracking ref of the tree is not copied by a clone; a detached HEAD, such as the queue's merge
    commit, is, because a local clone copies the whole object store)."""

    def rev(ref):
        p = subprocess.run(["git", "-C", tree, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else None

    base_sha, head_sha = rev(base), rev("HEAD")
    if not base_sha or not head_sha:
        print(f"[ci_gitleaks] FAIL: history base {base!r} does not resolve to a commit in {tree} (fetch-depth 0 needed)")
        return 2
    clone = os.path.join(work, "history-clone")
    p = subprocess.run(["git", "clone", "--quiet", "--no-checkout", "--no-hardlinks", tree, clone], capture_output=True, text=True)
    if p.returncode != 0:
        print(f"[ci_gitleaks] FAIL: could not clone {tree} for the history scan: {p.stderr.strip()}")
        return 2
    # `-m`: git shows NO diff for a merge commit unless asked, so a token pasted into a merge's conflict resolution (in neither parent) and removed in the
    # next commit would never be scanned (round 3, N2). With -m a merge is shown against each parent; the commits of the PR are still listed one by one.
    present = subprocess.run(["git", "-C", clone, "cat-file", "-e", f"{head_sha}^{{commit}}"], capture_output=True).returncode == 0
    if not present:
        print(f"[ci_gitleaks] FAIL: the history clone does not hold {head_sha[:12]} (the scan did NOT run)")
        return 2
    rc = subprocess.run([binary, "git", clone, "--log-opts", f"-m {base_sha}..{head_sha}", *scan_args(work)]).returncode
    return rc if rc in (0, 1) else 2


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", default=".")
    ap.add_argument("--history-base", default=None)
    ap.add_argument("--require-history", action="store_true")
    a = ap.parse_args(argv)
    work = tempfile.mkdtemp(prefix="ci-gitleaks-")  # left in place (the runner's temp is discarded)
    try:
        binary = os.environ.get("GITLEAKS_BIN") or fetch_binary(work)
    except CouldNotRun as e:
        print(f"[ci_gitleaks] FAIL: {e}")
        return 2
    print(f"[ci_gitleaks] binary: {binary}", flush=True)
    try:
        subprocess.run([binary, "version"], check=True)
    except (OSError, subprocess.CalledProcessError) as e:  # N7: a missing or broken GITLEAKS_BIN is "could not run" (2), not a finding (1)
        print(f"[ci_gitleaks] FAIL: {binary!r} does not run ({type(e).__name__}: {e}) (scan NOT run)")
        return 2
    stage = os.path.join(work, "tree")
    os.makedirs(stage)
    try:
        n = stage_tree(os.path.abspath(a.tree), stage)
    except subprocess.CalledProcessError as e:  # not a git tree, or git failed: the scan did not run (2), which is not a finding (1)
        print(f"[ci_gitleaks] FAIL: could not list the files of {a.tree}: {e.stderr.decode(errors='replace').strip() or e}")
        return 2
    if n == 0:
        print("[ci_gitleaks] FAIL: git listed no files to scan (a scan of nothing is never a pass)")
        return 2
    print(f"[ci_gitleaks] scanning {n} files", flush=True)
    rc = subprocess.run([binary, "dir", stage, *scan_args(work)]).returncode
    rc = rc if rc in (0, 1) else 2
    base = a.history_base or os.environ.get("BASE_REF") or ""
    if not base:
        if a.require_history:
            print("[ci_gitleaks] FAIL: --require-history, but no --history-base and BASE_REF is not set (the PR's commits were NOT scanned)")
            return 2
        print("[ci_gitleaks] NOTE: history scan skipped (no BASE_REF); only the tree was scanned")
        hrc = 0
    else:
        hrc = history_scan(binary, os.path.abspath(a.tree), base, work)
        print(f"[ci_gitleaks] history {base}..HEAD: {'clean' if hrc == 0 else 'exit ' + str(hrc)}")
    worst = 2 if 2 in (rc, hrc) else max(rc, hrc)
    if worst == 0:
        print(f"[ci_gitleaks] clean: {n} files and the history, gitleaks {VERSION}")
    return worst


if __name__ == "__main__":
    sys.exit(main())
