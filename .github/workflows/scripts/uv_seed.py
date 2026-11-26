#!/usr/bin/env python3
"""The warm uv cache that every merge-queue test cell is SEEDED from, and the trusted job that refreshes it.

WHY. A test cell runs with a fresh HOME and a fresh UV_CACHE_DIR (merge_queue.cell_env), so nothing a PR's tests wrote to a shared cache can reach the next cell. A fresh cache is
empty, and tests that lock a fake project offline (tests/test_release_prep.py, the release gate's tests) then cannot resolve it and skip. So each cell starts from a COPY of a
warm cache: an APFS clone (`cp -c -R`; `--reflink=auto` elsewhere), made by the driver into the cell's own folder. The cell writes only its copy.

THE SOURCE and WHO WRITES IT. The source lives under a root owned by the runner user and outside every job's work folder and RUNNER_TEMP (default
`~/.cache/factlattice-queue/uv-seed`, overridable by the repository variable UV_SEED_DIR; the cell step exports it as MQ_UV_SEED). It is written ONLY by `refresh`, which only the
workflow `uv-seed.yml` runs, from the default branch's code, on push to the default branch, weekly and by hand: never by a PR's run. The cell side (merge_queue.cmd_test) calls
`seed_cell`, which only READS the source, and child_env removes MQ_UV_SEED from every process that runs PR code, so a PR's tests are not handed the path. Each seed is a directory
`seed-<run id>/` (cache/ and a marker `.seed-complete`, written last), made read-only after it is built, and `current` is a symlink repointed atomically, so a cell that is copying
one seed is never disturbed by a refresh. LIMIT: the cell runs as the same user that owns the source, so a PR that deliberately goes looking for the path can still write it
(chmod is not a boundary against its owner); isolation from a hostile collaborator needs an ephemeral runner (see merge-queue.yml).

A MISSING SOURCE is an error, not a fallback: `seed_cell` raises SystemExit naming the path and the exact remedy (`gh workflow run uv-seed.yml --ref main`), before any PR
code runs. The FIRST seed on a runner is always a manual dispatch of uv-seed.yml (a merge made with the default token starts no push workflow). A cell that quietly started from an empty
cache would bring the silent skips back. uv writes absolute symlinks into the directory it builds in; `refresh` rewrites them to relative links after the rename, so a clone of a seed resolves
inside the clone.

WHAT IS WARMED. Everything the suite resolves offline: the locked environment (`uv sync --locked --extra test --extra semantic` of the default branch), and the fake project of
tests/test_release_prep.py (FAKE_PYPROJECT below, the one definition; the test imports it): its lock and its sync, which need pytest and setuptools in the cache.

    uv_seed.py refresh --root ROOT --repo DIR --run-id ID     (the trusted job)
"""

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time

MARKER = ".seed-complete"
DEFAULT_ROOT_REL = os.path.join(".cache", "factlattice-queue", "uv-seed")
KEEP = 2  # seeds kept after a refresh: the new one and the one a cell may be copying right now
UV_TIMEOUT_S = 1800

# The fake project of tests/test_release_prep.py: ONE definition (the test imports it from here), so the warm cache and the test cannot drift apart.
FAKE_PYPROJECT = """[project]
name = "fakeproj"
version = "0.0.1"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
semantic = []
test = ["pytest>=7"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
packages = ["factlattice", "factlattice_engine"]
"""


def default_root(home=None):
    """The default source root: under the runner user's REAL home (a cell's HOME is moved, so this is read before that)."""
    return os.path.join(home or os.path.expanduser("~"), DEFAULT_ROOT_REL)


def current(root):
    """-> the real path of the seed `root/current` points at, when its marker exists; else None."""
    link = os.path.join(root, "current")
    if not os.path.islink(link):
        return None
    target = os.path.realpath(link)
    if os.path.commonpath([target, os.path.realpath(root)]) != os.path.realpath(root) or target == os.path.realpath(root):
        return None  # a `current` that resolves outside the seed root (a stray link) is not a seed of this root
    return target if os.path.isfile(os.path.join(target, MARKER)) and os.path.isdir(os.path.join(target, "cache")) else None


def _make_writable(path):
    """chmod u+w on everything under path (a clone of a read-only seed is read-only too, and the cell must be able to write its OWN copy)."""
    for d, dirs, files in os.walk(path):
        for n in (*dirs, *files):
            p = os.path.join(d, n)
            if not os.path.islink(p):
                os.chmod(p, os.stat(p).st_mode | stat.S_IWUSR)
    os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)


def _make_readonly(path):
    for d, dirs, files in os.walk(path, topdown=False):
        for n in (*files, *dirs):
            p = os.path.join(d, n)
            if not os.path.islink(p):
                os.chmod(p, os.stat(p).st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    os.chmod(path, os.stat(path).st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def clone_flags(platform=None):
    return ["-c", "-R"] if (platform or sys.platform) == "darwin" else ["-R", "--reflink=auto"]


def seed_cell(root, dst, run=subprocess.run, platform=None):
    """READ the seed under `root` and make `dst` a clone of its cache. Never writes `root`. -> the seed's real path. Raises SystemExit, naming the root, when there is no
    complete seed (see the module docstring: a missing source is an error, not a fallback)."""
    src = current(root)
    if src is None:
        raise SystemExit(
            f"merge-queue-test: the uv seed cache is missing or incomplete at {root} (no `current` seed with a {MARKER} marker): "
            "run the workflow uv-seed.yml on the default branch by hand: `gh workflow run uv-seed.yml --ref main` (or Actions > uv-seed > Run workflow). A merge made with the default token "
            "does not start it (no push trigger fires), so the first seed on a runner is always a manual dispatch. Not falling back to an empty cache: the release-gate tests would skip silently."
        )
    os.makedirs(os.path.dirname(dst), exist_ok=True)  # the cell's own folder (the workflow made it; a test may not have)
    if os.path.isdir(dst) and not os.listdir(dst):
        os.rmdir(dst)  # an empty folder a caller made: `cp` needs the destination not to exist
    run(["cp", *clone_flags(platform), os.path.join(src, "cache"), dst], check=True, timeout=900, stdin=subprocess.DEVNULL)
    _make_writable(dst)
    return src


def relink(final, old_prefix):
    """Rewrite every symlink under `final` whose absolute target is under `old_prefix` (the directory the seed was built in, renamed away since) to a RELATIVE link to the same
    place under `final`: a clone of the seed then resolves inside the clone, and nothing dangles. -> how many links were rewritten."""
    n = 0
    for d, dirs, files in os.walk(final):
        for name in (*dirs, *files):
            p = os.path.join(d, name)
            if os.path.islink(p):
                target = os.readlink(p)
                if os.path.isabs(target) and (target == old_prefix or target.startswith(old_prefix + os.sep)):
                    new = final + target[len(old_prefix) :]
                    os.remove(p)
                    os.symlink(os.path.relpath(new, os.path.dirname(p)), p)
                    n += 1
    return n


def _sha(path):
    try:
        return hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError:
        return None


def warm(cache, repo_dir, scratch, run=subprocess.run):
    """Fill `cache` online: the default branch's locked environment, then the fake project's lock and sync. `scratch` holds the throwaway HOME, venvs and fake project."""
    home = os.path.join(scratch, "home")
    os.makedirs(home)
    # uv's own Python was installed by the trusted setup step under the REAL home: find it before HOME moves
    pydir = run(["uv", "python", "dir"], capture_output=True, text=True, check=False, timeout=60, stdin=subprocess.DEVNULL).stdout.strip()
    base = {
        **os.environ,
        "HOME": home,
        "UV_CACHE_DIR": cache,
        "UV_PYTHON_DOWNLOADS": "never",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    for k in ("MQ_UV_SEED", "MQ_CELL_DIR"):
        base.pop(k, None)
    for k, v in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_CACHE_HOME", ".cache"),
        ("XDG_DATA_HOME", ".local/share"),
    ):  # a runner service that exports them must not steer the warm-up
        base[k] = os.path.join(home, v)
    if pydir:
        base["UV_PYTHON_INSTALL_DIR"] = pydir
    sync = ["uv", "sync", "--no-config", "--locked", "--extra", "test", "--extra", "semantic"]
    run(sync, cwd=repo_dir, env={**base, "UV_PROJECT_ENVIRONMENT": os.path.join(scratch, "venv-main")}, check=True, timeout=UV_TIMEOUT_S)
    fake = os.path.join(scratch, "fake")
    os.makedirs(os.path.join(fake, "factlattice"))
    os.makedirs(os.path.join(fake, "factlattice_engine"))
    open(os.path.join(fake, "pyproject.toml"), "w").write(FAKE_PYPROJECT)
    for pkg in ("factlattice", "factlattice_engine"):
        open(os.path.join(fake, pkg, "__init__.py"), "w").write("")
    run(["uv", "lock", "--no-config"], cwd=fake, env=base, check=True, timeout=UV_TIMEOUT_S)
    run(sync, cwd=fake, env={**base, "UV_PROJECT_ENVIRONMENT": os.path.join(scratch, "venv-fake")}, check=True, timeout=UV_TIMEOUT_S)


def refresh(root, repo_dir, run_id, run=subprocess.run, keep=KEEP):
    """THE ONLY WRITER of `root`. Build `seed-<run_id>` (cache/ and the marker, written last), make it read-only, repoint `current` atomically, prune old seeds.
    -> the new seed's path. Run by the trusted workflow only, from the default branch's code."""
    run_id = str(run_id)
    if not run_id.replace("-", "").isalnum():
        raise SystemExit(f"uv_seed: run id {run_id!r} is not a plain token")
    os.makedirs(root, exist_ok=True)
    final = os.path.join(root, f"seed-{run_id}")
    work = final + ".tmp"
    if os.path.exists(final) or os.path.exists(work):
        raise SystemExit(f"uv_seed: {final} (or its .tmp) already exists: refusing to overwrite")
    cache = os.path.join(work, "cache")
    os.makedirs(cache)
    warm(cache, repo_dir, os.path.join(work, "scratch"), run)
    shutil.rmtree(os.path.join(work, "scratch"))  # the throwaway HOME, venvs and fake project: this process made them, under `work`
    marker = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": run_id,
        "uv_lock_sha256": _sha(os.path.join(repo_dir, "uv.lock")),
        "pyproject_sha256": _sha(os.path.join(repo_dir, "pyproject.toml")),
        "fake_pyproject_sha256": hashlib.sha256(FAKE_PYPROJECT.encode()).hexdigest(),
    }
    open(os.path.join(work, MARKER), "w").write(json.dumps(marker, indent=1) + "\n")  # LAST: a seed without it is not a seed
    os.rename(work, final)  # (a read-only directory cannot be renamed: make it read-only after, and before `current` points at it)
    relink(final, work)  # uv writes ABSOLUTE links into the build directory; point them at the final name, relative
    _make_readonly(final)
    link, tmp_link = os.path.join(root, "current"), os.path.join(root, "current.new")
    if os.path.lexists(tmp_link):
        os.remove(tmp_link)
    os.symlink(os.path.basename(final), tmp_link)
    os.replace(tmp_link, link)  # atomic: a cell reads the old seed or the new one, never neither
    prune(root, keep)
    return final


def prune(root, keep=KEEP):
    """Remove all but the `keep` newest complete seeds (by name's mtime) and any leftover .tmp of an interrupted refresh; the seed `current` points at is never removed."""
    live = os.path.realpath(os.path.join(root, "current"))
    entries = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False) and e.name.startswith("seed-")]
    for e in entries:
        if e.name.endswith(".tmp"):  # an interrupted refresh: not a seed, takes no keep slot
            _make_writable(e.path)
            shutil.rmtree(e.path)
    seeds = sorted((e for e in entries if not e.name.endswith(".tmp")), key=lambda e: e.stat().st_mtime, reverse=True)
    for i, e in enumerate(seeds):
        if i >= keep and os.path.realpath(e.path) != live:
            _make_writable(e.path)
            shutil.rmtree(e.path)


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh")
    r.add_argument("--root", default=None, help="the seed root (default: ~/.cache/factlattice-queue/uv-seed of the runner user)")
    r.add_argument("--repo", required=True)
    r.add_argument("--run-id", required=True)
    a = ap.parse_args(argv)
    root = a.root or os.environ.get("MQ_UV_SEED") or default_root()
    print(f"uv_seed: refreshing {root}", flush=True)
    print(f"uv_seed: new seed {refresh(root, os.path.abspath(a.repo), a.run_id)}", flush=True)


if __name__ == "__main__":
    main()
