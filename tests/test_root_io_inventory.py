"""[MECH-BR-PROBE] x-inventory: a MECHANICAL inventory of file-system calls in the engine and core packages.

WHAT IT DOES. Parses every .py file under factlattice/ and factlattice_engine/ with `ast`, finds calls that touch the file
system (the forms listed in SCANNED_FORMS), and compares the result, keyed by (file, enclosing function, call form) with a
count, against the table INVENTORY below. It fails when a site APPEARS or DISAPPEARS or changes count outside that table.
Every table entry says one of:

    root         the site may touch a path under a registered root; the value names the slice that will bound it
    not-root     it cannot: the value gives the reason (temp dir, state dir, package data, the checkout's own files, a
                 string or pathlib method that is not a disk call at all)
    caller-path  it touches a path the CALLER supplied (a file to write or lint), which may or may not lie under a root

A site the author could not classify from reading it is marked `root`, so the bounded-reads work looks at it.

WHAT IT IS NOT. A TRIPWIRE, not enforcement. It cannot see: `getattr(os, name)(...)` and any call whose name is built at run
time; an alias it does not track (`f = os.stat; f(p)`, a name bound by `from x import *`, a function stored in a dict);
a call reached through a third-party or standard-library helper that touches the disk for the caller (`shutil.copy`,
`tempfile`, `glob.glob`, `json.load(open(...))` counts only the `open`); and every form not in SCANNED_FORMS: os.makedirs,
os.remove, os.rename, os.readlink, os.chmod, os.truncate, os.lexists/islink/samefile, fcntl.lockf, shutil.*,
`.write_text`, `.write_bytes`, `.rename`, `.touch`. A Path-form match is by
method NAME only, since the AST cannot type the receiver, so a method that is not a disk call but shares a name is listed
(and marked not-root with that reason). `.replace` is counted only as `os.replace`, never `str.replace`.

WHY IT DID NOT FLAG THE UNKNOWN-KEY WRITE GUARD (round 9, read of 49bfd12). The table is keyed by (file, enclosing function, call form) and the scan reads
CALL SITES of file-system calls. The guard's unbounded `os.path.realpath` sat in an existing, listed function (`reviewer._path_in_untrusted_entry`); a NEW
CALLER of an existing disk helper in another module (`membership._untrusted_entry_guard`, on every write) adds no site, no count and no key, so the inventory is
green. It cannot see a new caller; the check for that is a PLANTED STALL through the real harness at the new site (spec 38 MECH-BR-PROBE x-sites), which
`tests/test_bounded_roots_boot.py::test_a_failed_entry_naming_a_hung_path_refuses_no_healthy_write_and_costs_what_a_declared_hung_root_costs` is.

KNOWN MISS (round 11, read of fe465bc P3-3): `reviewer._path_in_untrusted_entry` still resolves the strings of a failed entry with an unbounded
`os.path.realpath`, on the `dangles` verb's path (`reviewer.__main__._failed_container_stop`). That is the reviewer's own process, which writes and routes
nothing, so a hung string stalls that command and not a server or a writer. It is listed with that reason on its entry below, and is not fixed here.

The table is hand-made from reading each site. Adding a site means reading it, classifying it, and saying which slice bounds it.
"""
from __future__ import annotations

import ast
import collections
import textwrap
from collections.abc import Iterable
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGES = ("factlattice", "factlattice_engine")

SCANNED_FORMS = (
    "os.stat", "os.lstat", "os.scandir", "os.listdir", "os.walk", "os.open", "os.replace", "os.path.realpath",
    "os.path.isdir", "os.path.isfile", "os.path.exists", "open",
    "os.link", "os.mkdir", "os.rmdir", "os.access", "os.fsync", "os.unlink", "os.utime", "os.fchmod", "os.fstat",
    "fcntl.flock", "io.FileIO",
    ".exists", ".is_file", ".is_dir", ".stat", ".resolve", ".open", ".read_text", ".read_bytes", ".iterdir", ".glob", ".rglob",
    ".mkdir", ".unlink",
)
_OS_FUNCS = {"stat", "lstat", "scandir", "listdir", "walk", "open", "replace", "link", "mkdir", "rmdir", "access", "fsync",
             "unlink", "utime", "fchmod", "fstat"}
_PATH_FUNCS = {"realpath": "os.path.realpath", "isdir": "os.path.isdir", "isfile": "os.path.isfile", "exists": "os.path.exists"}
_METHODS = {"exists", "is_file", "is_dir", "stat", "resolve", "open", "read_text", "read_bytes", "iterdir", "glob", "rglob",
            "mkdir", "unlink"}
_OS_MODS = {"os"}
_PATH_MODS = {"os.path", "posixpath", "genericpath"}


def _aliases(tree: ast.AST) -> tuple[dict[str, str], dict[str, str]]:
    """(module aliases {local: 'os'|'os.path'|'io'}, bare names {local: canonical form}) for one file."""
    mods: dict[str, str] = {}
    bare: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name in ("os", "io", "fcntl", "posixpath", "genericpath"):
                    mods[a.asname or a.name.split(".")[0]] = {"posixpath": "os.path", "genericpath": "os.path"}.get(a.name, a.name)
                elif a.name == "os.path":
                    mods[a.asname or "os"] = "os.path" if a.asname else "os"
        elif isinstance(n, ast.ImportFrom) and n.module in ("os", "os.path", "posixpath", "genericpath", "io", "fcntl"):
            for a in n.names:
                loc = a.asname or a.name
                if n.module == "os" and a.name == "path":
                    mods[loc] = "os.path"
                elif n.module == "os" and a.name in _OS_FUNCS:
                    bare[loc] = f"os.{a.name}"
                elif n.module in ("os.path", "posixpath", "genericpath") and a.name in _PATH_FUNCS:
                    bare[loc] = _PATH_FUNCS[a.name]
                elif n.module == "io" and a.name == "open":
                    bare[loc] = "open"
                elif n.module == "io" and a.name == "FileIO":
                    bare[loc] = "io.FileIO"
                elif n.module == "fcntl" and a.name == "flock":
                    bare[loc] = "fcntl.flock"
    return mods, bare


def _form(call: ast.Call, mods: dict[str, str], bare: dict[str, str]) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        if f.id in bare:
            return bare[f.id]
        return "open" if f.id == "open" else ""
    if isinstance(f, ast.Attribute):
        base = f.value
        # os.stat, os.path.exists, io.open
        if isinstance(base, ast.Name) and mods.get(base.id) == "os" and f.attr in _OS_FUNCS:
            return f"os.{f.attr}"
        if isinstance(base, ast.Name) and mods.get(base.id) == "io" and f.attr == "open":
            return "open"
        if isinstance(base, ast.Name) and mods.get(base.id) == "io" and f.attr == "FileIO":
            return "io.FileIO"
        if isinstance(base, ast.Name) and mods.get(base.id) == "fcntl" and f.attr == "flock":
            return "fcntl.flock"
        if isinstance(base, ast.Name) and mods.get(base.id) == "os.path" and f.attr in _PATH_FUNCS:
            return _PATH_FUNCS[f.attr]
        if (isinstance(base, ast.Attribute) and base.attr == "path" and isinstance(base.value, ast.Name)
                and mods.get(base.value.id) == "os" and f.attr in _PATH_FUNCS):
            return _PATH_FUNCS[f.attr]
        if f.attr in _METHODS:
            return "." + f.attr
    return ""


class _Visitor(ast.NodeVisitor):
    def __init__(self, mods: dict[str, str], bare: dict[str, str]) -> None:
        self.mods, self.bare, self.stack = mods, bare, []
        self.found: list[tuple[str, str, int]] = []

    def _scope(self, node, name):
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, n):       self._scope(n, n.name)
    visit_AsyncFunctionDef = visit_FunctionDef
    def visit_ClassDef(self, n):          self._scope(n, n.name)

    def visit_Lambda(self, n):            self._scope(n, "<lambda>")

    def visit_Call(self, n):
        form = _form(n, self.mods, self.bare)
        if form:
            self.found.append((".".join(self.stack) or "<module>", form, n.lineno))
        self.generic_visit(n)


def scan(root: Path = REPO, packages: Iterable[str] = PACKAGES) -> dict[tuple[str, str, str], int]:
    """{(relative file, enclosing scope, call form): count}. Lines are returned by `scan_lines`, not keyed (they drift)."""
    out: dict[tuple[str, str, str], int] = collections.Counter()
    for (rel, scope, form), lines in scan_lines(root, packages).items():
        out[(rel, scope, form)] = len(lines)
    return dict(out)


def scan_lines(root: Path = REPO, packages: Iterable[str] = PACKAGES) -> dict[tuple[str, str, str], list[int]]:
    out: dict[tuple[str, str, str], list[int]] = collections.defaultdict(list)
    for pkg in packages:
        for f in sorted((root / pkg).rglob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            mods, bare = _aliases(tree)
            v = _Visitor(mods, bare)
            v.visit(tree)
            for scope, form, line in v.found:
                out[(str(f.relative_to(root)), scope, form)].append(line)
    return dict(out)


# ---- the declared table ------------------------------------------------------------------------------------------
ROOT, NOT_ROOT, CALLER_PATH = "root", "not-root", "caller-path"
CLASSES = (ROOT, NOT_ROOT, CALLER_PATH)

N_ROOT_WRITE = 'slice 3: a touch on the written file or its directory. ORDER, read from edit.py: @_serialised wrapper (realpath) -> _file_lock (realpath, then the state-dir lock) -> the writer body (.resolve, .is_file, _read_base: realpath + safe_read_bytes) -> _write -> _preflight (exists, access, stat) -> _atomic_replace. The wrapper and _file_lock are the FIRST touches, so the slice 3 refuse-before must sit AHEAD of every @_serialised writer (add_node, set_property, repair_property and the others); a direct library or CLI writer has no gate (x-inventory known miss)'
N_CALLER_INPUT = 'a file or directory the caller passed in'
N_CALLER_OUT = 'an output path the caller passed in'
N_PKG = 'package data shipped inside the installed package'
N_S2_LIVE = ("slice 2 DONE for a served corpus: with a kernel attached the root-set check reads the workers' kept answer (Partition._live_root_set) "
             "and this is not called; with NO kernel (a library caller, a test) it asks the disk, "
             "unbounded: the library caller's own thread, slice 5")
N_S2_HEALTH = ("slice 2 DONE for the root and its pinned file: with a kernel attached `observe` supplies the kept answer, so a marked "
               "root's disk is not touched (a marked corpus skips root_health). STILL UNBOUNDED: the registered PROFILE path (a profile on "
               "a second root is a known miss) and callers with no kernel: slice 4")
N_REGISTRY = 'the registry file or its pointer: read at boot AND on every lts_list_corpora (tools.py registry_choice, sha256_now). Not a registered root, but a hung disk under it stalls the listing and boot; outside this spec (see the build report)'
N_STATE = 'the query or miss log under the state dir'
N_PKG_OR_TEMP = 'the shipped control files, or a scratch file under a temp dir'
N_RUNTIME = "the embedder's own runtime files (pid, socket, launch agent, interpreter) under the state or run dir"
N_TEMP_WRITE = 'a scratch file under a temp dir'
N_BACKUP = 'the backup copy under the temp dir'

#: file -> [(enclosing scope, call form, count, class, note)]. `root` notes start "slice N: ..." (the slice that bounds the site:
#: 2 probes off the lock, gate and report; 3 membership and write rules; 4 boot and build_store; 5 calls on workers).
#: Seeded 2026-11-05 at the slice 1 commit by reading each site; where a site could not be placed it is `root`.
N_S4_READ = "slice 4 DONE: the disk half of a corpus build, run as a job on the root's own worker in the process registry (bootload.StoreLoader), bounded by the no-progress root deadline"

INVENTORY = {
    "factlattice/authoring/edit.py": [
        ('_atomic_replace', '.exists', 1, ROOT, N_ROOT_WRITE),
        ('_atomic_replace', 'os.fchmod', 1, ROOT, N_ROOT_WRITE),
        ('_atomic_replace', 'os.fsync', 2, ROOT, N_ROOT_WRITE),
        ('_atomic_replace', 'os.open', 2, ROOT, N_ROOT_WRITE),
        ('_atomic_replace', 'os.path.realpath', 1, ROOT, N_ROOT_WRITE),
        ('_atomic_replace', 'os.replace', 1, ROOT, N_ROOT_WRITE),
        ('_atomic_replace', 'os.stat', 1, ROOT, N_ROOT_WRITE),
        ('_atomic_replace', 'os.unlink', 1, ROOT, N_ROOT_WRITE),
        ('_atomic_replace', 'os.utime', 1, ROOT, N_ROOT_WRITE),
        ('_backup_path', '.exists', 1, NOT_ROOT, N_BACKUP),
        ('_cas_check', 'os.unlink', 1, ROOT, N_ROOT_WRITE),
        ('_create_exclusive', 'os.fchmod', 1, ROOT, N_ROOT_WRITE),
        ('_create_exclusive', 'os.fstat', 1, ROOT, N_ROOT_WRITE),
        ('_create_exclusive', 'os.fsync', 1, ROOT, N_ROOT_WRITE),
        ('_create_exclusive', 'os.link', 1, ROOT, N_ROOT_WRITE),
        ('_create_exclusive', 'os.open', 1, ROOT, N_ROOT_WRITE),
        ('_create_node_file', '.is_dir', 4, ROOT, N_ROOT_WRITE),
        ('_create_node_file', 'os.mkdir', 1, ROOT, N_ROOT_WRITE),
        ('_create_node_file', 'os.path.realpath', 1, ROOT, N_ROOT_WRITE),
        ('_create_node_file', 'os.rmdir', 1, ROOT, N_ROOT_WRITE),
        ('_file_lock', '.mkdir', 1, NOT_ROOT, 'the write-lock dir under the state dir'),
        ('_file_lock', 'fcntl.flock', 2, NOT_ROOT, 'flock on the write-lock file under the state dir'),
        ('_file_lock', 'os.open', 1, NOT_ROOT, 'the write-lock file under the state dir (refused if it lies inside the corpus)'),
        ('_file_lock', 'os.path.realpath', 3, ROOT, N_ROOT_WRITE),
        ('_lstat_ident', 'os.lstat', 1, ROOT, N_ROOT_WRITE),
        ('_open_node', '.is_file', 1, ROOT, N_ROOT_WRITE),
        ('_open_node', '.resolve', 1, ROOT, N_ROOT_WRITE),
        ('_preflight', '.exists', 1, ROOT, N_ROOT_WRITE),
        ('_preflight', 'os.access', 1, ROOT, N_ROOT_WRITE),
        ('_preflight', 'os.stat', 2, ROOT, N_ROOT_WRITE),
        ('_prune_backups', '.is_file', 1, NOT_ROOT, N_BACKUP),
        ('_prune_backups', 'os.scandir', 1, NOT_ROOT, N_BACKUP),
        ('_prune_backups', 'os.unlink', 1, NOT_ROOT, N_BACKUP),
        ('_read_base', 'os.path.realpath', 1, ROOT, N_ROOT_WRITE),
        ('_read_raw', 'os.path.realpath', 1, ROOT, N_ROOT_WRITE),
        ('_serialised.wrapper', 'os.path.realpath', 1, ROOT, N_ROOT_WRITE),
        ('_sweep_stale_temps', '.iterdir', 1, ROOT, N_ROOT_WRITE),
        ('_sweep_stale_temps', '.stat', 1, ROOT, N_ROOT_WRITE),
        ('_sweep_stale_temps', '.unlink', 1, ROOT, N_ROOT_WRITE),
        ('_unlink_quiet', 'os.unlink', 1, ROOT, N_ROOT_WRITE),
        ('_write', 'os.path.realpath', 2, ROOT, N_ROOT_WRITE),
        ('add_node', '.is_file', 1, ROOT, N_ROOT_WRITE),
        ('add_node', '.resolve', 1, ROOT, N_ROOT_WRITE),
        ('backup_dir', '.mkdir', 1, NOT_ROOT, N_BACKUP),
        ('oob_warnings', 'os.path.realpath', 1, ROOT, N_ROOT_WRITE),
        ('repair_property', '.is_file', 1, ROOT, N_ROOT_WRITE),
        ('repair_property', '.resolve', 1, ROOT, N_ROOT_WRITE),
        ('set_property', '.is_file', 1, ROOT, N_ROOT_WRITE),
        ('set_property', '.resolve', 1, ROOT, N_ROOT_WRITE),
        ('write_whole_file', '.exists', 2, ROOT, N_ROOT_WRITE),
        ('write_whole_file', 'os.path.realpath', 1, ROOT, N_ROOT_WRITE),
    ],
    "factlattice/corpus.py": [
        ('_build_over_files', 'os.path.isfile', 1, CALLER_PATH, N_CALLER_INPUT),
        ('_normalize_inputs', 'os.path.isdir', 1, CALLER_PATH, N_CALLER_INPUT),
        ('_normalize_inputs', 'os.path.isfile', 1, CALLER_PATH, N_CALLER_INPUT),
    ],
    "factlattice/domain_profiles.py": [
        ('discover_profiles', '.glob', 1, ROOT, 'slice 4: the profile directory is FACTLATTICE_PROFILE_DIR or the shipped dir, and may sit on a root'),
        ('discover_profiles', 'os.path.isdir', 1, ROOT, 'slice 4: the profile directory is FACTLATTICE_PROFILE_DIR or the shipped dir, and may sit on a root'),
    ],
    "factlattice/enumeration_gap.py": [
        ('control_corpus_text', 'open', 1, NOT_ROOT, N_PKG),
    ],
    "factlattice/hashing.py": [
        ('_open_regular', 'os.fstat', 1, ROOT, "slice 2 DONE for the kernel (a job on the root's own worker, no lock held); build_store and the boot loader read it on the root's own worker (slice 4 DONE); the self-heal rebuild calls it on the request thread: slice 5"),
        ('_open_regular', 'os.open', 1, ROOT, "slice 2 DONE for the kernel (a job on the root's own worker, no lock held); build_store and the boot loader read it on the root's own worker (slice 4 DONE); the self-heal rebuild calls it on the request thread: slice 5"),
        ('check_member', 'os.lstat', 1, ROOT, "slice 2 DONE for the kernel (a job on the root's own worker, no lock held); build_store and the boot loader read it on the root's own worker (slice 4 DONE); the self-heal rebuild calls it on the request thread: slice 5"),
        ('check_member', 'os.stat', 1, ROOT, "slice 2 DONE for the kernel (a job on the root's own worker, no lock held); build_store and the boot loader read it on the root's own worker (slice 4 DONE); the self-heal rebuild calls it on the request thread: slice 5"),
        ('list_lts_files', 'os.walk', 1, ROOT, "slice 2 DONE for the kernel (a job on the root's own worker, no lock held); build_store and the boot loader read it on the root's own worker (slice 4 DONE); the self-heal rebuild calls it on the request thread: slice 5"),
        ('walk_lts_paths', 'os.walk', 1, ROOT, 'slice 2: same walk, used by callers that name a root (scan tools pass it a caller path)'),
    ],
    "factlattice/index_builder.py": [
        ('write_index_atomic', 'os.fsync', 1, CALLER_PATH, N_CALLER_OUT),
        ('write_index_atomic', 'os.path.exists', 1, CALLER_PATH, N_CALLER_OUT),
        ('write_index_atomic', 'os.replace', 1, CALLER_PATH, N_CALLER_OUT),
        ('write_index_atomic', 'os.unlink', 1, CALLER_PATH, N_CALLER_OUT),
    ],
    "factlattice/rootview.py": [
        ('RootView.real', 'os.path.realpath', 1, ROOT, 'slice 3: the library fallback for a root the reading does not know when no resolver is installed (a library caller, as before); the engine installs the bounded one'),
    ],
    "factlattice_engine/rootstate.py": [
        ('_copy_lines_without_hooks', 'open', 1, NOT_ROOT, "the engine's own source file (rootstate.py), read at import to compare the CPython copy with the interpreter's"),
        ('_posixpath_realpath_lines', 'open', 1, NOT_ROOT, "the interpreter's own posixpath.py, read at import to compare it with the CPython copy"),
        ('_realpath_copy', 'os.lstat', 1, ROOT, "slice 3 DONE: CPython's own posixpath.realpath copied with a progress report after each lstat and readlink, so, with the copy active, a slow root whose every call answers is never late (the fallback has the limits in spec 38 x-progress); the attach confirm of one ok candidate root and the probe's own resolve run it on a pool worker, never the caller (used only when the interpreter's own algorithm equals the copy)"),
        ('_realpath_copy', 'os.stat', 1, ROOT, "slice 3 DONE: the strict-mode loop check of the CPython copy; the engine never calls it strict"),
        ('_realpath_fallback', 'os.path.realpath', 1, ROOT, "slice 3 DONE: the resolver on an interpreter whose own algorithm differs from the copy: os.path.realpath with one progress report when it returns, on a pool worker, never the caller"),
    ],
    "factlattice_engine/boundedfs.py": [
        ('_resolve_job.fn', 'os.path.realpath', 1, ROOT, "slice 3: the bounded resolve of a declared root in a process with no probe; it runs on a pool worker, never on the caller"),
    ],
    "factlattice/ledger.py": [
        ('_identity', 'os.path.realpath', 1, ROOT, "slice 3: bounded: with a root view installed by the call this read runs through ledger._disk (the engine deadline; a marked root answers at once); with none it is as it was"),
        ('_identity', 'os.stat', 2, ROOT, "slice 3: bounded: with a root view installed by the call this read runs through ledger._disk (the engine deadline; a marked root answers at once); with none it is as it was"),
        ('_last_line', 'os.fstat', 1, ROOT, 'slice 3: ledger state may live on a root (state_path), and old-spelling stats'),
        ('_legacy', 'os.stat', 1, ROOT, 'slice 3: ledger state may live on a root (state_path), and old-spelling stats'),
        ('_norm', 'os.path.realpath', 1, ROOT, 'slice 3: ledger state may live on a root (state_path), and old-spelling stats'),
        ('_present', 'os.stat', 1, ROOT, "slice 3: stat of a ledger or marker location, which may be on a root's drive"),
        ('append', '.exists', 1, ROOT, "slice 3: ledger.append inside the lock; the ledger may be on a root's drive"),
        ('append', '.mkdir', 2, ROOT, 'slice 3: ledger state may live on a root (state_path), and old-spelling stats'),
        ('append', 'os.fsync', 1, ROOT, 'slice 3: ledger state may live on a root (state_path), and old-spelling stats'),
        ('append', 'os.open', 2, ROOT, "slice 3: ledger.append inside the lock; the ledger may be on a root's drive"),
        ('append', 'os.path.realpath', 1, ROOT, 'slice 3: resolves the written file inside the lock'),
        ('append', 'os.unlink', 1, ROOT, 'slice 3: ledger state may live on a root (state_path), and old-spelling stats'),
        ('default_ephemeral_roots', 'os.path.realpath', 1, NOT_ROOT, 'the OS temp dirs'),
        ('ephemeral_roots', 'os.path.realpath', 1, NOT_ROOT, 'the OS temp dirs or the test override list'),
        ('is_ephemeral', 'os.path.realpath', 1, ROOT, 'slice 3: resolves the written file to judge it ephemeral, on the write path'),
        ('locate', '.exists', 4, ROOT, 'slice 3: ledger.locate stats the ledger file before the lock; spec x-sites names locate'),
        ('locate', 'os.path.realpath', 1, ROOT, 'slice 3: resolves the written file; spec x-sites names locate'),
        ('read_blob', '.read_bytes', 1, ROOT, 'slice 3: ledger state may live on a root (state_path), and old-spelling stats'),
        ('read_marker', 'open', 1, ROOT, "slice 3: bounded: with a root view installed by the call this read runs through ledger._disk (the engine deadline; a marked root answers at once); with none it is as it was"),
        ('read_sources', '.read_bytes', 1, ROOT, 'slice 3: ledger state may live on a root (state_path), and old-spelling stats'),
        ('store_blob', '.exists', 1, ROOT, 'slice 3: ledger state may live on a root (state_path), and old-spelling stats'),
        ('store_blob', '.mkdir', 1, ROOT, 'slice 3: ledger state may live on a root (state_path), and old-spelling stats'),
        ('store_blob', 'os.fsync', 1, ROOT, 'slice 3: ledger state may live on a root (state_path), and old-spelling stats'),
        ('store_blob', 'os.open', 1, ROOT, 'slice 3: ledger state may live on a root (state_path), and old-spelling stats'),
        ('store_blob', 'os.replace', 1, ROOT, 'slice 3: ledger state may live on a root (state_path), and old-spelling stats'),
        ('store_blob', 'os.unlink', 1, ROOT, 'slice 3: ledger state may live on a root (state_path), and old-spelling stats'),
    ],
    "factlattice/overlay.py": [
        ('_state', 'os.access', 1, NOT_ROOT, 'the deployment overlay is a configuration directory named by an environment variable (factlattice/overlay.py), never a registered corpus root'),
        ('_state', 'os.lstat', 1, NOT_ROOT, 'the deployment overlay is a configuration directory named by an environment variable (factlattice/overlay.py), never a registered corpus root'),
        ('overlay_path', '.exists', 1, NOT_ROOT, 'the deployment overlay is a configuration directory named by an environment variable (factlattice/overlay.py), never a registered corpus root'),
        ('overlay_profile_dir', '.is_dir', 1, NOT_ROOT, 'the deployment overlay is a configuration directory named by an environment variable (factlattice/overlay.py), never a registered corpus root'),
    ],
    "factlattice/profile.py": [
        ('Profile.resolve_path', 'os.path.realpath', 1, ROOT, 'slice 4: a path named inside a profile, which may point under a root'),
        ('_read_profile_file', 'open', 2, ROOT, 'slice 4: a profile file or its discovery directory may be on a registered root'),
        ('discover_profile', '.glob', 1, ROOT, 'slice 4: a profile file or its discovery directory may be on a registered root'),
        ('discover_profile', 'os.path.isdir', 1, ROOT, 'slice 4: a profile file or its discovery directory may be on a registered root'),
        ('load_profile', 'os.path.isfile', 1, ROOT, 'slice 4: a profile file or its discovery directory may be on a registered root'),
        ('resolve_profile_ex', 'os.path.isfile', 1, ROOT, 'slice 4: a profile file or its discovery directory may be on a registered root'),
        ('unprojected_review_extensions', 'os.path.isfile', 1, ROOT, 'slice 4: a profile file or its discovery directory may be on a registered root'),
        ('validate_review_extensions', 'os.path.isfile', 1, ROOT, 'slice 4: a profile file or its discovery directory may be on a registered root'),
        ('validate_review_extensions', 'os.path.realpath', 1, ROOT, 'slice 3: realpath of each corpus root'),
    ],
    "factlattice/registry.py": [
        ('_read_pointer', 'open', 1, NOT_ROOT, N_REGISTRY),
        ('_read_pointer', 'os.path.isfile', 1, NOT_ROOT, N_REGISTRY),
        ('_read_pointer', 'os.path.realpath', 1, NOT_ROOT, N_REGISTRY),
        ('_read_pointer', 'os.stat', 1, NOT_ROOT, N_REGISTRY),
        ('_read_raw', 'open', 1, NOT_ROOT, N_REGISTRY),
        ('_read_raw', 'os.path.isfile', 1, NOT_ROOT, N_REGISTRY),
    ],
    "factlattice_engine/analytics/profile_audit.py": [
        ('_property_edges_wiring', 'open', 1, NOT_ROOT, N_TEMP_WRITE),
        ('resolve_profile_adapter', 'os.path.isfile', 1, ROOT, 'slice 5: request-time read of a registered profile path, which may be on a root'),
    ],
    "factlattice_engine/analytics/profile_registration.py": [
        ('_real', 'os.path.realpath', 1, ROOT, 'slice 4: the boot audit reads each registered profile path, which may be on a root'),
        ('audit_profile_registration', 'os.path.isfile', 1, ROOT, 'slice 4: the boot audit reads each registered profile path, which may be on a root'),
    ],
    "factlattice_engine/authoring.py": [
        ('marked_refusal', 'os.path.realpath', 1, CALLER_PATH, "slice 3: resolves the written file, as the writer will, after the string check against the marked roots and before the writer's lock; the file is the caller's own path (x-inside-marked)"),
        ('_edge_context', '.resolve', 1, ROOT, 'slice 3: resolves the written path before the gate and again inside the refusal reply, so a path under a hung root stalls the refusal itself; the refuse-before must precede it'),
        ('_edge_context', 'os.path.isfile', 1, ROOT, "slice 5: the registry entry's profile path, which may be on a root"),
        ('_ledger_kw', '.resolve', 1, ROOT, 'slice 3: resolves the written path before the gate and again inside the refusal reply, so a path under a hung root stalls the refusal itself; the refuse-before must precede it'),
        ('_refusal', '.resolve', 1, ROOT, 'slice 3: resolves the written path before the gate and again inside the refusal reply, so a path under a hung root stalls the refusal itself; the refuse-before must precede it'),
    ],
    "factlattice_engine/build_stamp.py": [
        ('_check_dir_fd', 'os.fstat', 1, NOT_ROOT, "the engine's own code tree and the stdin-carry files under the state/temp dir"),
        ('_open_carry_dir', 'os.fchmod', 1, NOT_ROOT, "the engine's own code tree and the stdin-carry files under the state/temp dir"),
        ('_open_carry_dir', 'os.fstat', 1, NOT_ROOT, "the engine's own code tree and the stdin-carry files under the state/temp dir"),
        ('_open_carry_dir', 'os.mkdir', 1, NOT_ROOT, "the engine's own code tree and the stdin-carry files under the state/temp dir"),
        ('_open_carry_dir', 'os.open', 1, NOT_ROOT, "the engine's own code tree and the stdin-carry files under the state/temp dir"),
        ('_remove_carry_name', 'os.unlink', 1, NOT_ROOT, "the engine's own code tree and the stdin-carry files under the state/temp dir"),
        ('_write_carry_file', 'os.fchmod', 1, NOT_ROOT, "the engine's own code tree and the stdin-carry files under the state/temp dir"),
        ('_write_carry_file', 'os.open', 1, NOT_ROOT, "the engine's own code tree and the stdin-carry files under the state/temp dir"),
        ('_write_carry_file', 'os.unlink', 1, NOT_ROOT, "the engine's own code tree and the stdin-carry files under the state/temp dir"),
        ('_source_records', 'open', 1, NOT_ROOT, "the engine's own and the core package's source files"),
        ('_source_records', 'os.path.exists', 1, NOT_ROOT, "tells a dangling link in the engine's own or the core package's source tree from a file"),
        ('_source_records', 'os.walk', 1, NOT_ROOT, "the engine's own and the core package's source tree"),
        ('sweep_stale_carry', 'os.listdir', 1, NOT_ROOT, "the engine's own code tree and the stdin-carry files under the state/temp dir"),
        ('sweep_stale_carry', 'os.stat', 1, NOT_ROOT, "the engine's own code tree and the stdin-carry files under the state/temp dir"),
        ('sweep_stale_carry', 'os.unlink', 1, NOT_ROOT, "the engine's own code tree and the stdin-carry files under the state/temp dir"),
        ('take_inherited_stdin_carry', 'os.fstat', 1, NOT_ROOT, "the engine's own code tree and the stdin-carry files under the state/temp dir"),
        ('take_inherited_stdin_carry', 'os.open', 1, NOT_ROOT, "the engine's own code tree and the stdin-carry files under the state/temp dir"),
        ('take_inherited_stdin_carry', 'os.stat', 1, NOT_ROOT, "the engine's own code tree and the stdin-carry files under the state/temp dir"),
        ('take_inherited_stdin_carry', 'os.unlink', 1, NOT_ROOT, "the engine's own code tree and the stdin-carry files under the state/temp dir"),
    ],
    "factlattice_engine/bootload.py": [
        ('StoreLoader._read.fn', 'os.path.isdir', 1, ROOT, N_S4_READ + ' (the pinned-file check of a root that exists, on the root\'s own worker job)'),
        ('StoreLoader._read.fn1', 'os.path.isdir', 1, ROOT, N_S4_READ),
    ],
    "factlattice_engine/callcli.py": [
        ('_load_args', 'open', 1, CALLER_PATH, 'the args file the CLI user passed'),
        ('build_context', 'os.path.realpath', 2, NOT_ROOT, "the engine's own package and git locations"),
        ('build_info', 'os.path.realpath', 2, NOT_ROOT, "the engine's own package and git locations"),
    ],
    "factlattice_engine/config.py": [
        ('declared_projects', 'os.path.isfile', 1, NOT_ROOT, "the built-in EXAMPLE registry's decision file (the first-run trial's own folder), not a deployment root"),
        ('registered_profile', 'os.path.isfile', 1, ROOT, "slice 4 DONE for boot and build_store: the loader calls it through boundedfs.bounded_call (a worker of the process registry, keyed by the profile path), so a profile under a hung root fails its corpus; still unbounded on its OTHER callers (register_corpus, the library build_partition, parser_config_for without `registered`): slice 5"),
        ('root_missing_reason', 'os.path.isdir', 1, ROOT, 'slice 5: request-time. The no-roots listing re-reads a root that was missing when the corpus was built (no slot, no kept answer); bounded by the call deadline'),
        ('root_missing_reason', 'os.path.isfile', 1, ROOT, 'slice 5: request-time. The no-roots listing re-reads a root that was missing when the corpus was built (no slot, no kept answer); bounded by the call deadline'),
        ('served_root_paths', 'os.path.realpath', 1, ROOT, "slice 3: realpath of each declared root, which the probe's kept path will replace"),
    ],
    "factlattice_engine/freshness.py": [
        ('main', 'open', 1, CALLER_PATH, 'a path the CLI user passed'),
    ],
    "factlattice_engine/incremental.py": [
        ('CorpusIndex.read_root', 'os.path.isdir', 1, ROOT, N_S4_READ),
        ('CorpusIndex.read_root', 'os.path.isfile', 1, ROOT, N_S4_READ),
        ('CorpusIndex.live_roots', 'os.path.isdir', 1, ROOT, N_S2_LIVE),
        ('CorpusIndex.live_roots', 'os.path.isfile', 1, ROOT, N_S2_LIVE),
        ('CorpusIndex.root_health', 'os.path.isdir', 1, ROOT, N_S2_HEALTH),
        ('CorpusIndex.root_health', 'os.path.isfile', 2, ROOT, N_S2_HEALTH),
    ],
    "factlattice_engine/kernel.py": [
        ('FreshnessKernel._observe', 'os.path.isdir', 1, ROOT, "slice 2 DONE: runs on the root's own worker (a job), never under the kernel lock; the answer is kept and read from memory"),
        ('FreshnessKernel._observe', 'os.path.isfile', 1, ROOT, "slice 2 DONE: runs on the root's own worker (a job), never under the kernel lock; the answer is kept and read from memory"),
        ('FreshnessKernel._stat_hint', 'os.stat', 1, ROOT, "slice 2 DONE: the poll's stat of a leaf under a root, now a job on the root's own worker with no kernel lock held"),
    ],
    "factlattice_engine/key_loss.py": [
        ('scan_roots', '.exists', 1, ROOT, 'slice 5: the scan tool is handed corpus roots, usually registered ones; request-time'),
        ('scan_roots', '.is_dir', 1, ROOT, 'slice 5: the scan tool is handed corpus roots, usually registered ones; request-time'),
    ],
    "factlattice_engine/lint/__main__.py": [
        ('_default_profile', 'os.path.isfile', 1, CALLER_PATH, N_CALLER_INPUT),
        ('resolve_targets', '.glob', 1, CALLER_PATH, N_CALLER_INPUT),
        ('resolve_targets', 'os.path.isdir', 2, CALLER_PATH, N_CALLER_INPUT),
        ('resolve_targets', 'os.path.isfile', 1, CALLER_PATH, N_CALLER_INPUT),
    ],
    "factlattice_engine/lint/core.py": [
        ('lint', 'os.path.isdir', 1, ROOT, 'slice 5: lts_lint names a file or directory that may be a registered root; request-time, bounded by the call deadline'),
    ],
    "factlattice_engine/lint/corpus_files.py": [
        ('find_corpus_file_issues', '.is_dir', 1, ROOT, 'slice 5: lts_lint names a file or directory that may be a registered root; request-time, bounded by the call deadline'),
        ('find_corpus_file_issues', '.is_file', 2, ROOT, 'slice 5: lts_lint names a file or directory that may be a registered root; request-time, bounded by the call deadline'),
        ('find_corpus_file_issues', '.rglob', 1, ROOT, 'slice 5: lts_lint names a file or directory that may be a registered root; request-time, bounded by the call deadline'),
    ],
    "factlattice_engine/membership.py": [
        ('_norm', 'os.path.realpath', 1, ROOT, 'slice 3: resolves every registered root and the written file; spec x-sites names _norm'),
    ],
    "factlattice_engine/pathsource.py": [
        ('load_partition', 'os.path.isfile', 1, ROOT, 'slice 5: find(path=...) names a file that may sit under a registered root; request-time'),
        ('load_partition', 'os.path.realpath', 1, ROOT, 'slice 5: find(path=...) names a file that may sit under a registered root; request-time'),
    ],
    "factlattice_engine/preview.py": [
        ('_preview_in_corpus', 'os.path.isfile', 2, ROOT, 'slice 5: request-time read under a corpus root'),
        ('_preview_isolated', 'open', 1, NOT_ROOT, N_TEMP_WRITE),
        ('_rel_within', 'os.path.realpath', 2, ROOT, 'slice 5: request-time read under a corpus root'),
        ('_resolve_corpus_root', 'os.path.isdir', 2, ROOT, 'slice 5: request-time read under a corpus root'),
        ('_stage', 'open', 1, NOT_ROOT, N_TEMP_WRITE),
        ('preview', 'os.path.isfile', 1, ROOT, 'slice 5: preview(path) names a file that may be under a root; request-time'),
    ],
    "factlattice_engine/property_loss.py": [
        ('scan_corpus', '.is_dir', 1, ROOT, 'slice 5: the scan tool is handed corpus roots, usually registered ones; request-time'),
        ('scan_corpus', '.resolve', 1, ROOT, 'slice 5: the scan tool is handed corpus roots, usually registered ones; request-time'),
    ],
    "factlattice_engine/query_surface.py": [
        ('CompiledQuery.validate_source_file', 'os.path.isfile', 1, ROOT, 'slice 5: request-time check of a source file under each root of the corpus'),
    ],
    "factlattice_engine/querylog.py": [
        ('write', '.mkdir', 1, NOT_ROOT, N_STATE),
        ('write', 'open', 1, NOT_ROOT, N_STATE),
    ],
    "factlattice_engine/readprofile.py": [
        ('read_records', '.is_file', 1, NOT_ROOT, N_STATE),
        ('read_records', 'open', 1, NOT_ROOT, N_STATE),
    ],
    "factlattice_engine/render/html.py": [
        ('write_site', '.mkdir', 2, CALLER_PATH, N_CALLER_OUT),
    ],
    "factlattice_engine/reviewer/__init__.py": [
        ('_doc_rel', 'os.path.realpath', 1, ROOT, 'slice 3: resolves registry roots or documents with no probe to lean on (x-build-traps 2)'),
        ('_path_in_untrusted_entry', 'os.path.realpath', 2, ROOT, 'slice 5 or a reviewer change; KNOWN MISS, not bounded (round 11): the reviewer\'s `dangles` verb (`reviewer.__main__._failed_container_stop`) resolves every string of a FAILED entry here, unbounded, in the reviewer\'s own process (it writes and routes nothing; a hung string stalls that command, never the server). Bounding it is a reviewer change of its own; the server\'s write guard no longer does this (facts only)'),
        ('check_extension_paths', 'os.path.realpath', 2, ROOT, 'slice 3: resolves registry roots or documents with no probe to lean on (x-build-traps 2)'),
        ('corpora_containing', 'os.path.realpath', 1, ROOT, 'slice 3: resolves registry roots or documents with no probe to lean on (x-build-traps 2)'),
        ('plan_frame', 'os.path.realpath', 2, ROOT, 'slice 3: resolves registry roots or documents with no probe to lean on (x-build-traps 2)'),
        ('resolve_output.accept', 'os.path.realpath', 1, ROOT, 'slice 3: resolves registry roots or documents with no probe to lean on (x-build-traps 2)'),
    ],
    "factlattice_engine/reviewer/__main__.py": [
        ('_failed_container_stop', 'os.path.realpath', 1, CALLER_PATH, 'the document the reviewer was asked about'),
        ('_lintable_file', 'os.access', 1, CALLER_PATH, 'the document the reviewer was asked about'),
        ('_lintable_file', 'os.path.realpath', 1, CALLER_PATH, 'the document the reviewer was asked about'),
        ('_lintable_file', 'os.stat', 1, CALLER_PATH, 'the document the reviewer was asked about'),
        ('_main', 'os.path.exists', 1, ROOT, 'slice 3: probes each root of a corpus for a source file, in a process with no probe'),
        ('_main', 'os.path.isfile', 2, ROOT, 'slice 4: profile path of a registry entry, via seat_store in the reviewer'),
        ('_main', 'os.path.realpath', 5, CALLER_PATH, 'the document or review file the reviewer was asked about'),
        ('_real', 'os.path.exists', 1, CALLER_PATH, 'the document the reviewer was asked about'),
        ('_real', 'os.path.realpath', 1, CALLER_PATH, 'the document the reviewer was asked about'),
        ('_resolve', 'os.path.isfile', 1, ROOT, 'slice 4: profile path of a registry entry, via seat_store in the reviewer'),
        ('_resolve', 'os.path.realpath', 1, CALLER_PATH, 'the document the reviewer was asked about'),
    ],
    "factlattice_engine/selfcheck/__init__.py": [
        ('_check_reviewer_shapes', 'open', 1, NOT_ROOT, N_PKG_OR_TEMP),
        ('compute_control_digest', 'open', 1, NOT_ROOT, N_PKG_OR_TEMP),
        ('run_selfcheck', 'open', 1, NOT_ROOT, N_PKG_OR_TEMP),
    ],
    "factlattice_engine/selfcheck/entry_point_equivalence.py": [
        ('_run', 'open', 2, NOT_ROOT, N_PKG_OR_TEMP),
    ],
    "factlattice_engine/semantic/embedd.py": [
        ('Server._bind', 'os.unlink', 1, NOT_ROOT, N_RUNTIME),
        ('Server._cleanup', 'os.unlink', 1, NOT_ROOT, N_RUNTIME),
        ('Server.acquire_pidfile', 'fcntl.flock', 1, NOT_ROOT, N_RUNTIME),
        ('Server.acquire_pidfile', 'os.fstat', 1, NOT_ROOT, N_RUNTIME),
        ('Server.acquire_pidfile', 'os.open', 1, NOT_ROOT, N_RUNTIME),
        ('SharedEmbedder._connect', 'fcntl.flock', 2, NOT_ROOT, N_RUNTIME),
        ('SharedEmbedder._connect', 'os.fstat', 1, NOT_ROOT, N_RUNTIME),
        ('SharedEmbedder._connect', 'os.open', 1, NOT_ROOT, N_RUNTIME),
        ('_check_owned', 'os.lstat', 1, NOT_ROOT, N_RUNTIME),
        ('_interpreter_identity', 'os.path.realpath', 1, NOT_ROOT, N_RUNTIME),
        ('_interpreter_stat', 'os.stat', 1, NOT_ROOT, N_RUNTIME),
        ('_mcp_install_env', 'open', 1, NOT_ROOT, "the user's MCP client config file"),
        ('_read_starts', 'os.open', 1, NOT_ROOT, N_RUNTIME),
        ('_read_tripped', 'os.open', 1, NOT_ROOT, N_RUNTIME),
        ('_record_start', 'os.open', 1, NOT_ROOT, N_RUNTIME),
        ('_record_start', 'os.replace', 1, NOT_ROOT, N_RUNTIME),
        ('_record_start', 'os.unlink', 1, NOT_ROOT, N_RUNTIME),
        ('_write_private_json', 'os.open', 1, NOT_ROOT, N_RUNTIME),
        ('_write_private_json', 'os.replace', 1, NOT_ROOT, N_RUNTIME),
        ('_write_private_json', 'os.unlink', 1, NOT_ROOT, N_RUNTIME),
        ('keep._ready', 'os.unlink', 1, NOT_ROOT, N_RUNTIME),
        ('launchagent', 'open', 2, NOT_ROOT, N_RUNTIME),
        ('launchagent', 'os.open', 3, NOT_ROOT, N_RUNTIME),
        ('launchagent', 'os.replace', 2, NOT_ROOT, N_RUNTIME),
        ('launchagent', 'os.unlink', 2, NOT_ROOT, N_RUNTIME),
        ('pidfiles', 'os.listdir', 1, NOT_ROOT, N_RUNTIME),
        ('run_dir', 'os.mkdir', 1, NOT_ROOT, N_RUNTIME),
        ('source_fingerprint', 'open', 1, NOT_ROOT, "the engine's own source files"),
        ('wait_for_socket', 'os.path.exists', 1, NOT_ROOT, N_RUNTIME),
        ('watch_fingerprint', 'open', 1, NOT_ROOT, N_RUNTIME),
    ],
    "factlattice_engine/semantic/embedder.py": [
        ('_sha256', 'open', 1, NOT_ROOT, 'the model weights under the model dir'),
        ('load_embedder', 'os.path.isfile', 1, NOT_ROOT, 'the model weights under the model dir'),
    ],
    "factlattice_engine/semantic/index.py": [
        ('SemanticIndex._adopt_legacy', 'fcntl.flock', 2, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._adopt_legacy', 'os.open', 1, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._adopt_legacy', 'os.path.isfile', 2, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._adopt_legacy', 'os.replace', 1, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._gc_other_versions', 'fcntl.flock', 1, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._gc_other_versions', 'os.open', 1, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._gc_other_versions', 'os.path.realpath', 2, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._gc_other_versions', 'os.rmdir', 1, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._gc_other_versions.newest', 'os.listdir', 1, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._gc_other_versions.newest', 'os.stat', 2, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._load', 'os.path.isdir', 1, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._load', 'os.path.isfile', 2, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._locked', 'fcntl.flock', 2, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._locked', 'os.open', 1, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._manifest_stamp', 'os.stat', 1, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._other_version_dirs', 'os.listdir', 1, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._other_version_dirs', 'os.path.isdir', 1, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._other_version_dirs', 'os.path.isfile', 1, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._write.<lambda>', 'open', 4, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex._write.put', 'os.replace', 1, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
        ('SemanticIndex.refresh', 'os.path.isfile', 1, NOT_ROOT, 'the semantic sidecar under the state dir (state_root/semantic/...), never under a corpus root'),
    ],
    "factlattice_engine/semantic/suggest.py": [
        ('_pin_from_env', 'open', 1, NOT_ROOT, 'a pin file named by the environment'),
    ],
    "factlattice_engine/semantic/tokenizer.py": [
        ('WordPieceTokenizer.__init__', 'open', 1, NOT_ROOT, 'the vocabulary file under the model dir'),
    ],
    "factlattice_engine/store.py": [
        ('Partition._resolve_source', 'os.path.isfile', 1, ROOT, 'slice 5: source excerpt, request-time read under a root (the gate refuses a marked corpus first)'),
        ('_root_fingerprint', 'os.lstat', 1, ROOT, 'slice 5: runs on the request thread only in a root-set heal, after the gate has refused a marked corpus; not bounded for a root that hangs for the first time inside it'),
        ('_root_fingerprint', 'os.stat', 2, ROOT, 'slice 5: runs on the request thread only in a root-set heal, after the gate has refused a marked corpus; not bounded for a root that hangs for the first time inside it'),
        ('_root_fingerprint', 'os.walk', 1, ROOT, 'slice 5: runs on the request thread only in a root-set heal, after the gate has refused a marked corpus; not bounded for a root that hangs for the first time inside it'),
    ],
    "factlattice_engine/tools.py": [
        ('LtsEngine._no_roots_view', 'os.path.isfile', 1, ROOT, "slice 4: lts_list_corpora stats the registry entry's profile path for a no-roots corpus, which has no slot or kernel state; NOT bounded by slice 2 (a profile on a hung root would stall the listing)"),
        ('LtsEngine._source_excerpt', 'os.path.isfile', 1, ROOT, 'slice 5: source excerpt, request-time read under a root (the gate refuses a marked corpus first)'),
        ('LtsEngine.history', 'os.path.isfile', 1, ROOT, 'slice 5: locates the file of a node under a root, request-time'),
        ('LtsEngine.history', 'os.path.realpath', 1, ROOT, 'slice 5: resolves the file whose history was asked for, which may be under a root; request-time'),
        ('LtsEngine.register_corpus', 'os.path.isdir', 1, ROOT, 'slice 5: request-thread stat of the registered root. Slice 2 DONE for the rest: seating no longer holds the kernel lock across the read (it waits on the root workers)'),
        ('LtsEngine._miss_append', '.mkdir', 1, NOT_ROOT, N_STATE),
        ('LtsEngine._miss_append', 'open', 1, NOT_ROOT, N_STATE),
        ('LtsEngine._miss_append', 'os.path.realpath', 1, NOT_ROOT, 'the miss log path under the state dir, compared against the roots'),
        ('_path_in_any_root', 'os.path.realpath', 1, ROOT, 'slice 3 DONE: now only the miss-log path, which the guard asks about; roots go through the reading'),
        ('_path_in_any_root.rreal', 'os.path.realpath', 1, ROOT, 'slice 3 DONE: a root is resolved from the call reading of the roots; this is the library fallback when none is installed'),
        ('_path_in_any_root', 'os.stat', 2, ROOT, 'slice 3 DONE: a root the reading found not answering is not stat-ed (the report_miss root guard); the other stat is of the miss-log path and its ancestors'),
        ('_path_in_any_root.fold', 'os.path.realpath', 1, ROOT, 'slice 3: resolves a registered root or the path being compared'),
        ('_writer_target_error', 'os.access', 1, ROOT, 'slice 3: stats the written path BEFORE any gate; the refuse-before must precede it'),
        ('_writer_target_error', 'os.path.isdir', 1, ROOT, 'slice 3: stats the written path BEFORE any gate; the refuse-before must precede it'),
        ('_writer_target_error', 'os.path.isfile', 1, ROOT, 'slice 3: stats the written path BEFORE any gate; the refuse-before must precede it'),
    ],
}


def declared() -> dict[tuple[str, str, str], tuple[int, str, str]]:
    out: dict[tuple[str, str, str], tuple[int, str, str]] = {}
    for f, entries in INVENTORY.items():
        for scope, form, n, cls, note in entries:
            key = (f, scope, form)
            assert key not in out, f"duplicate inventory entry {key}"
            out[key] = (n, cls, note)
    return out


def diff(found: dict[tuple[str, str, str], int], table=None) -> list[str]:
    """Human-readable differences between a scan and the declared table; empty when they agree."""
    table = declared() if table is None else table
    msgs: list[str] = []
    for key in sorted(set(found) | set(table)):
        have, want = found.get(key), (table[key][0] if key in table else None)
        if have == want:
            continue
        f, scope, form = key
        if want is None:
            msgs.append(f"NEW  {f} :: {scope} :: {form} x{have}  (undeclared: read it, classify it, add it to INVENTORY)")
        elif have is None:
            msgs.append(f"GONE {f} :: {scope} :: {form} x{want}  (declared but no longer found: remove or move the entry)")
        else:
            msgs.append(f"COUNT {f} :: {scope} :: {form}: declared {want}, found {have}")
    return msgs


# ======================================================================================================================
# the tests
# ======================================================================================================================


def test_the_engine_matches_the_declared_inventory():
    problems = diff(scan())
    assert not problems, ("a file-system call appeared, disappeared or changed count outside the declared table "
                          "(tests/test_root_io_inventory.py::INVENTORY). Each entry says root (with its slice), not-root "
                          "(with why) or caller-path. TRIPWIRE only; see the module docstring for what it cannot see:\n  "
                          + "\n  ".join(problems))


def test_every_entry_is_well_formed():
    for key, (n, cls, note) in declared().items():
        assert n >= 1, key
        assert cls in CLASSES, (key, cls)
        assert isinstance(note, str) and len(note) >= 10, (key, "an entry needs its reason or slice")
        if cls == ROOT:
            assert note.startswith("slice ") and note[6] in "2345", (key, "a root entry names the slice that bounds it", note)


def test_every_declared_file_exists_and_every_package_file_with_sites_is_declared():
    for f in INVENTORY:
        assert (REPO / f).is_file(), f"declared file is gone: {f}"
    files_with_sites = {k[0] for k in scan()}
    assert files_with_sites == set(INVENTORY)


# ---- the scanner has its own positive controls: a planted source with every form must be seen ------------------------
_PLANT = textwrap.dedent('''
    import os, io, fcntl
    import os.path as osp
    from fcntl import flock as fl
    from os import stat as st, scandir
    from os.path import realpath as rp, isdir
    from pathlib import Path

    def f(p):
        os.stat(p); os.lstat(p); os.scandir(p); os.listdir(p); os.walk(p); os.open(p, 0); os.replace(p, p)
        os.path.realpath(p); os.path.isdir(p); os.path.isfile(p); os.path.exists(p)
        open(p); io.open(p)
        osp.isfile(p)
        os.link(p, p); os.mkdir(p); os.rmdir(p); os.access(p, 0); os.fsync(1); os.unlink(p); os.utime(p); os.fchmod(1, 0); os.fstat(1)
        fcntl.flock(1, 0); fl(1, 0); io.FileIO(p)
        st(p); scandir(p); rp(p); isdir(p)
        q = Path(p)
        q.exists(); q.is_file(); q.is_dir(); q.stat(); q.resolve(); q.open(); q.read_text(); q.read_bytes()
        q.iterdir(); q.glob("*"); q.rglob("*"); q.mkdir(); q.unlink()
        "abc".replace("a", "b")            # str.replace: must NOT be counted
        p.write_text("x"); os.makedirs(p)  # forms outside the scanned set: must NOT be counted
''')


def _plant_root(tmp_path, body=_PLANT, name="planted"):
    pkg = tmp_path / "factlattice"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / f"{name}.py").write_text(body, encoding="utf-8")
    (tmp_path / "factlattice_engine").mkdir(exist_ok=True)
    return tmp_path


def test_scanner_sees_every_listed_form_and_ignores_str_replace(tmp_path):
    found = scan(_plant_root(tmp_path))
    forms = collections.Counter()
    for (_f, _s, form), n in found.items():
        forms[form] += n
    expected = {
        "os.stat": 2, "os.lstat": 1, "os.scandir": 2, "os.listdir": 1, "os.walk": 1, "os.open": 1, "os.replace": 1,
        "os.path.realpath": 2, "os.path.isdir": 2, "os.path.isfile": 2, "os.path.exists": 1, "open": 2,
        ".exists": 1, ".is_file": 1, ".is_dir": 1, ".stat": 1, ".resolve": 1, ".open": 1, ".read_text": 1, ".read_bytes": 1,
        ".iterdir": 1, ".glob": 1, ".rglob": 1, ".mkdir": 1, ".unlink": 1,
        "os.link": 1, "os.mkdir": 1, "os.rmdir": 1, "os.access": 1, "os.fsync": 1, "os.unlink": 1, "os.utime": 1,
        "os.fchmod": 1, "os.fstat": 1, "fcntl.flock": 2, "io.FileIO": 1,
    }
    assert dict(forms) == expected, {k: (forms.get(k), expected.get(k)) for k in set(forms) | set(expected) if forms.get(k) != expected.get(k)}
    assert set(expected) == set(SCANNED_FORMS), "every documented form has a planted site"


def test_goes_red_on_a_planted_new_call(tmp_path):
    """The negative control: a copy of the real scan plus ONE planted call must differ from the table, and name the new site."""
    clean = scan()
    assert diff(clean) == []
    planted = dict(clean)
    planted[("factlattice_engine/tools.py", "LtsEngine.find", "os.path.isfile")] = 1
    msgs = diff(planted)
    assert len(msgs) == 1 and msgs[0].startswith("NEW") and "LtsEngine.find" in msgs[0], msgs
    # the same on a real scan of a real tree: copy the package, plant one call in a function that has none, scan the copy
    import shutil
    for pkg in PACKAGES:
        shutil.copytree(REPO / pkg, tmp_path / pkg, ignore=shutil.ignore_patterns("__pycache__"))
    target = tmp_path / "factlattice_engine" / "ordering.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n\ndef _planted_probe(p):\n    import os\n    return os.stat(p)\n", encoding="utf-8")
    msgs = diff(scan(tmp_path))
    assert len(msgs) == 1 and "ordering.py" in msgs[0] and "_planted_probe" in msgs[0] and msgs[0].startswith("NEW"), msgs


def test_goes_red_when_a_site_disappears_and_when_a_count_changes(tmp_path):
    import shutil
    for pkg in PACKAGES:
        shutil.copytree(REPO / pkg, tmp_path / pkg, ignore=shutil.ignore_patterns("__pycache__"))
    k = tmp_path / "factlattice_engine" / "kernel.py"
    src = k.read_text(encoding="utf-8")
    assert "st = os.stat(abs_path)" in src
    k.write_text(src.replace("st = os.stat(abs_path)", "st = None", 1), encoding="utf-8")
    msgs = diff(scan(tmp_path))
    assert len(msgs) == 1 and msgs[0].startswith("GONE") and "_stat_hint" in msgs[0], msgs
    k.write_text(src.replace("st = os.stat(abs_path)", "st = os.stat(abs_path); st = os.stat(abs_path)", 1), encoding="utf-8")
    msgs = diff(scan(tmp_path))
    assert len(msgs) == 1 and msgs[0].startswith("COUNT") and "declared 1, found 2" in msgs[0], msgs


def test_inventory_summary_for_the_build_report(capsys):
    """Prints the counts by class (run with -s). Not an assertion about their values."""
    by_sites = collections.Counter()
    by_keys = collections.Counter()
    for _k, (n, cls, _note) in declared().items():
        by_sites[cls] += n
        by_keys[cls] += 1
    print("\ninventory: keys", dict(by_keys), "sites", dict(by_sites), "total sites", sum(by_sites.values()))


def test_every_os_level_scanned_form_has_a_harness_name():
    """Cross-check the two instruments: a call form the inventory scans for must be one the stall harness can block (or be
    listed here as not blockable, with why). Forms are scanned by name, so this keeps the lists from drifting apart."""
    from tests import stall_harness as H
    to_harness = {
        "os.stat": "stat", "os.lstat": "lstat", "os.scandir": "scandir", "os.listdir": "listdir", "os.walk": "walk",
        "os.open": "os.open", "os.replace": "os.replace", "os.path.realpath": "realpath", "os.path.isdir": "isdir",
        "os.path.isfile": "isfile", "os.path.exists": "exists", "open": "open", "os.link": "link", "os.mkdir": "mkdir",
        "os.rmdir": "rmdir", "os.access": "access", "os.fsync": "fsync", "os.unlink": "unlink", "os.utime": "utime",
        "os.fchmod": "fchmod", "os.fstat": "fstat", "fcntl.flock": "flock", "io.FileIO": "FileIO",
    }
    path_forms_through_os = {".exists", ".is_file", ".is_dir", ".stat", ".resolve", ".open", ".read_text", ".read_bytes",
                             ".iterdir", ".glob", ".rglob", ".mkdir", ".unlink"}   # reach the names above (planted-site tests)
    assert set(to_harness) | path_forms_through_os == set(SCANNED_FORMS)
    assert set(to_harness.values()) <= set(H.CALL_NAMES)
