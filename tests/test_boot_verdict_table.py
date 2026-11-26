"""[MECH-BR-BOOT] x-load-error, ENTRY SHAPE (third amendment, the architects' design in specs/38): NO ENTRY SHAPE'S BOOT VERDICT CHANGES from release af4cfc2,
pinned by a table that is measured THROUGH THE REAL SERVER'S BOOT, never through a library function the server does not use.

Two drivers, one shapes file (`tests/boot_verdicts/shapes.json`: a healthy corpus beside ONE entry of each shape):
  server_driver.py   spawns a real stdio server per shape (its kernel, loader and first pass), and records what `lts_list_corpora` lists (ids, failed
                     tables with codes, registry error) and what a named find on the healthy corpus and on the entry's corpus answer. THE table (af4cfc2.json).
  library_driver.py  `config.build_store`, the blocking library boot, classified by the store's tables (af4cfc2_library.json). A second table: the library
                     is a boot path of its own (the call CLI, the reviewer helpers).
Both pinned files were recorded by running the SAME driver from a `git archive` of af4cfc2 with the package path printed.

THE NAMED DIFFERENCES (`expected_differences.json`): the deliberate differences from the release (specs/38 x-load-error) are (a) a corpus that failed on an
unknown key and has a non-boolean `private` (the release names it in `failed_corpora` and the extent's skipped list, the branch counts it in `private_failed`), and
(b) the WRITE side: the server driver also records a write to a file of the healthy corpus and one to the entry's folder (corpora, flagged, outside, refused), and a
write beside an entry the registry cannot place lands flagged where the release attributed it or called it outside every root (39 shapes, 44 with (c)), and a write under a
failed entry's path-like string is a flagged candidate (5, and 3 more beside the private rows; 1 where the string covers the HEALTHY corpus's folder, so only the
healthy corpus's write is the candidate), and (c) the same flag for an entry whose root names a named root the registry cannot resolve (`named-root-unknown` in four shapes, `named-roots-malformed` in one, plus one shape, `named unknown, typo key`, that fails `registry-entry-unknown-key` at the release and on the branch alike and is a named-root reference only in its value: 5 shapes in all, added after the whole-slice read of 4384356). Each such shape is listed BY NAME with the branch's exact answer. The server table fails on a difference that is not on the list, on a listed difference that
moved, and on a listed name that no longer differs.

SHAPE KEYS beyond the entry itself (both drivers honour them the same way): `$registry` (top-level registry keys), `$profile` (a profile BODY, written to a file named by the
entry's `profile` key) and `$profile_dir` (the ENGINE's own profile directory, files name -> text, read as FACTLATTICE_PROFILE_DIR). A failed corpus's observation carries
its `reason`, `names` and `detail` when it has them (spec 39), so a mislabelled reason moves a row; and a refused profile is pinned in BOTH entry forms (a single `root` and a `roots` list).

THE HONESTY CHECK (`test_the_pinned_tables_are_what_the_release_answers`) RUNS IN THE GATE, never skips on the trunk (the one named exception:
a published snapshot, where PUBLISH-EXCLUDE is absent and the private release is not in the history; the skip reason says so, spec 42 R10). It needs the real repository the tree came from, because
a `git archive` has no .git. It looks, in order, at `FACTLATTICE_RELEASE_REPO` (a path to any clone or work tree that holds commit af4cfc2) and then at this tree's
own .git; if neither holds the release, THE TEST FAILS with the instruction. A runner that tests an archive exports FACTLATTICE_RELEASE_REPO=<the repository it
archived from>.

THE PLANT (`test_a_change_on_the_servers_boot_path_turns_the_table_red`): a copy of this tree with `server.after_first_pass` changed so that the server never
enters registry-error mode for a shape met on a loader thread. A table that measured `config.build_store` stayed green on exactly this change.

Red on 90737fb: 26 of the 82 shapes then listed differ from the release through the server (24 list the healthy corpus in registry-error mode where the
release lists nothing; `path int + root ok` fails `corpus-load-error`; `only_file ""` answers `root-set-changed`)."""
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile

import pytest

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
DIR = HERE / "boot_verdicts"
RELEASE = "af4cfc2"
ENV_REPO = "FACTLATTICE_RELEASE_REPO"


def _run(driver: str, tree: pathlib.Path, tmp: pathlib.Path, shapes: pathlib.Path = DIR / "shapes.json"):
    """(observations, package path printed) for a driver run with `tree` on PYTHONPATH from a neutral cwd and a scratch HOME."""
    cwd = tmp / "cwd"
    cwd.mkdir(exist_ok=True)
    (tmp / "home").mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTLATTICE_", "LTS_"))}
    env.update(PYTHONPATH=str(tree), HOME=str(tmp / "home"), TMPDIR=str(tmp))
    p = subprocess.run([sys.executable, "-P", str(DIR / driver), str(shapes)], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=900)
    assert p.returncode == 0, p.stderr[-800:]
    pkg = [ln for ln in p.stderr.splitlines() if ln.startswith("PKG ")]
    assert len(pkg) == 1, p.stderr[-400:]
    return json.loads(p.stdout.strip().splitlines()[-1]), pathlib.Path(pkg[0][4:]).resolve()


def _diff(a: dict, b: dict):
    return [(k, a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]


def _named():
    return json.loads((DIR / "expected_differences.json").read_text())


def _unexpected(pinned: dict, got: dict, named: dict):
    """Every difference between the release's answers and the branch's that is NOT on the named list, plus every named one that no longer differs or whose
    branch answer is not the recorded one. The list is by name, with the branch's exact answer: an unnamed difference, a changed named one and a stale name all fail."""
    out = []
    for k, was, now in _diff(pinned, got):
        if k not in named:
            out.append((k, "unnamed difference", was, now))
        elif named[k]["branch"] != now:
            out.append((k, "named difference moved", named[k]["branch"], now))
    for k in named:
        if pinned.get(k) == got.get(k):
            out.append((k, "named but no longer different", pinned.get(k), got.get(k)))
    return out


def _release_repo() -> pathlib.Path:
    """The repository that holds the release. FAILS (never skips) when none does on the trunk: a skip reads as a pass. In a published
    snapshot (no PUBLISH-EXCLUDE) it skips with a reason that says so."""
    tried = []
    for cand in (os.environ.get(ENV_REPO), str(REPO)):
        if not cand:
            continue
        r = subprocess.run(["git", "-C", cand, "cat-file", "-e", RELEASE + "^{commit}"], capture_output=True)
        if r.returncode == 0:
            return pathlib.Path(cand)
        tried.append(cand)
    if not (REPO / "PUBLISH-EXCLUDE").exists():
        # A published snapshot: PUBLISH-EXCLUDE is trunk-only, and the snapshot's history is the public repository's, which
        # does not hold the private release commit. Named skip (spec 42 R10); on the trunk the file exists and this test fails
        # exactly as before. The pinned tables are checked against the release on the trunk, by the release gate.
        pytest.skip(f"release {RELEASE} is not reachable and PUBLISH-EXCLUDE is absent, so this is a published snapshot whose history "
                    f"does not hold the private release; the honesty check runs on the trunk (or with {ENV_REPO} set)")
    pytest.fail(f"the honesty check cannot find release {RELEASE}: tried {tried or 'nothing'}. This tree has no .git, or the clone lacks the release. "
                f"Export {ENV_REPO}=<path to a clone or work tree that holds commit {RELEASE}> in the runner (the repository the archive was made from). "
                "This test never skips: a skipped honesty check reads as a pass.")


def test_the_comparison_can_tell_two_answers_apart():
    """Negative control: the comparison fails if one observation moves, and the pinned tables hold every answer class, so a table of one answer could not pass."""
    shapes = json.loads((DIR / "shapes.json").read_text())
    server = json.loads((DIR / "af4cfc2.json").read_text())
    lib = json.loads((DIR / "af4cfc2_library.json").read_text())
    assert set(server) == set(shapes) == set(lib), "every shape has a recorded release answer on both tables"
    answers = {(o["find_okc"], o["find_badc"], bool(o["list"]["tables"]), o["list"]["registry_error"], tuple(o["list"]["ids"])) for o in server.values()}
    assert len(answers) >= 5, answers
    assert {v.split(":")[0].split(" ")[0] for v in lib.values()} >= {"registry-error", "failed", "served", "no-roots", "none"}
    moved = json.loads(json.dumps(server))
    moved["roots string"]["list"]["ids"] = ["okc"]
    assert [d[0] for d in _diff(server, moved)] == ["roots string"]
    # the named list: the branch's answers exactly as recorded pass; an unnamed difference, a moved named one, and a stale name each fail
    named = _named()
    branch = json.loads(json.dumps(server))
    for k, v in named.items():
        branch[k] = v["branch"]
    assert _unexpected(server, branch, named) == []
    unnamed_label = next(k for k in sorted(server) if k not in named)
    changed = json.loads(json.dumps(server[unnamed_label]))
    changed["list"]["ids"] = ["okc"]
    assert [u[1] for u in _unexpected(server, dict(branch, **{unnamed_label: changed}), named)] == ["unnamed difference"]
    some = sorted(named)[0]
    mv = json.loads(json.dumps(branch)); mv[some]["list"]["private_failed"] = 2
    assert [u[1] for u in _unexpected(server, mv, named)] == ["named difference moved"]
    assert [u[1] for u in _unexpected(server, dict(branch, **{some: server[some]}), named)] == ["named but no longer different"]
    import collections
    kinds = collections.Counter(tuple(v["differs"]) for v in named.values())
    assert kinds == {("write_badc", "write_okc"): 44, ("write_badc",): 5, ("list",): 1, ("list", "write_badc"): 3, ("write_okc",): 1,
                     ("find_badc", "find_okc", "list"): 3, ("find_badc", "list"): 3}, kinds       # the named differences, by kind (the last two kinds are the six spec 39 profile shapes: a refused profile fails its corpora only; the engine-profile-directory shape is pinned and does NOT differ)
    assert all(k in server for k in named) and {"private yes + pth blank", "r000 (seed 12)", "r022 (seed 12)", "r026 (seed 12)"} <= set(named)


def test_every_shapes_boot_through_the_real_server_is_the_releases(tmp_path):
    """The branch's REAL SERVER answers what release af4cfc2's server answered, for every shape. Package path printed and checked."""
    got, pkg = _run("server_driver.py", REPO, tmp_path)
    assert pkg.is_relative_to(REPO), pkg
    assert _unexpected(json.loads((DIR / "af4cfc2.json").read_text()), got, _named()) == [], "a boot verdict moved from release " + RELEASE + " (through the server)"


def test_every_shapes_library_boot_is_the_releases(tmp_path):
    got, pkg = _run("library_driver.py", REPO, tmp_path)
    assert pkg.is_relative_to(REPO), pkg
    # the named differences that also differ through the LIBRARY boot carry their branch verdict under `library` (spec 39: a refused profile fails its corpora)
    pinned = json.loads((DIR / "af4cfc2_library.json").read_text())
    named_lib = {k: v["library"] for k, v in _named().items() if "library" in v}
    assert all(pinned[k] != v for k, v in named_lib.items()), "a named library difference that no longer differs"
    assert _diff({**pinned, **named_lib}, got) == []


def test_the_pinned_tables_are_what_the_release_answers(tmp_path):
    """The honesty check. Runs in the gate and FAILS when the release cannot be found (see the module docstring for what the runner exports)."""
    repo = _release_repo()
    tree = tmp_path / "release"
    tree.mkdir()
    blob = subprocess.run(["git", "-C", str(repo), "archive", RELEASE], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        tf.extractall(tree, filter="data")
    for driver, pinned in (("server_driver.py", "af4cfc2.json"), ("library_driver.py", "af4cfc2_library.json")):         # the RELEASE answers its own pinned file exactly: no named differences here
        got, pkg = _run(driver, tree, tmp_path)
        assert pkg.is_relative_to(tree.resolve()) and not pkg.is_relative_to(REPO), pkg
        assert _diff(json.loads((DIR / pinned).read_text()), got) == [], f"{pinned} is not what release {RELEASE} answers"


def test_a_change_on_the_servers_boot_path_turns_the_table_red(tmp_path):
    """The plant of the cold read: `server.after_first_pass` changed so the server never enters registry-error mode for a shape met on a loader thread. The
    server table must go RED for a shape whose registry error needs the disk and so is met on a loader thread (`only_file` that is not a string, beside a root that exists); the shapes reached in `declare()` do not pass through the changed line (and the library table, which never runs that function, stays green: the reason the library table alone was the wrong instrument)."""
    plant = tmp_path / "plant"
    shutil.copytree(REPO, plant, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "private"))
    src = plant / "factlattice_engine" / "server.py"
    text = src.read_text(encoding="utf-8")
    old = "            if loader.fatal is not None:                  # an entry's SHAPE met on a loader thread"
    assert text.count(old) == 1, "the plant did not apply"
    src.write_text(text.replace(old, "            if False and loader.fatal is not None:                  # an entry's SHAPE met on a loader thread"), encoding="utf-8")
    subset = tmp_path / "subset.json"
    shapes = json.loads((DIR / "shapes.json").read_text())
    subset.write_text(json.dumps({k: shapes[k] for k in ("control: roots", "only_file int", "roots string", "entry string")}))
    pinned = json.loads((DIR / "af4cfc2.json").read_text())
    got, pkg = _run("server_driver.py", plant, tmp_path, subset)
    assert pkg.is_relative_to(plant.resolve()) and not pkg.is_relative_to(REPO), pkg
    named = {k: v for k, v in _named().items() if k in got}
    moved = sorted({u[0] for u in _unexpected({k: pinned[k] for k in got}, got, named)})
    assert moved == ["only_file int"], moved        # the shape met on a loader thread (it needs the disk: a root that exists); every shape that needs no disk raises in declare(), before the changed line, and the control is untouched
    lib_pinned = json.loads((DIR / "af4cfc2_library.json").read_text())
    lib_got, _ = _run("library_driver.py", plant, tmp_path, subset)
    assert _diff({k: lib_pinned[k] for k in lib_got}, lib_got) == [], "the library table does not run the changed function"


def test_a_shape_that_never_settles_fails_that_shape_by_name_and_only_that_shape(tmp_path):
    """P3-4 / P3-8. The driver waits for the settled state (three identical listings, nothing pending) and does NOT accept what it last saw when a shape never settles:
    that shape's observation is an error record NAMING the shape (so the table fails on it), and every other shape still gets its observation. Staged with the knob that
    gives one shape a single round. Control: without the knob both shapes settle."""
    shapes = json.loads((DIR / "shapes.json").read_text())
    two = tmp_path / "two.json"
    two.write_text(json.dumps({k: shapes[k] for k in ("control: roots", "entry string")}))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTLATTICE_", "LTS_"))}
    env.update(PYTHONPATH=str(REPO), HOME=str(tmp_path), TMPDIR=str(tmp_path))
    run = lambda extra: subprocess.run([sys.executable, "-P", str(DIR / "server_driver.py"), str(two)], cwd=str(tmp_path), env={**env, **extra}, capture_output=True, text=True, timeout=300)
    bad = run({"BOOT_VERDICT_UNSETTLE": "entry string"})
    assert bad.returncode == 0, bad.stderr[-400:]
    got = json.loads(bad.stdout.strip().splitlines()[-1])
    assert "entry string" in got["entry string"]["error"] and "never settled" in got["entry string"]["error"] and "SHAPE FAILED entry string" in bad.stderr, got
    assert "error" not in got["control: roots"] and got["control: roots"]["find_okc"] == "ok", got["control: roots"]
    good = json.loads(run({}).stdout.strip().splitlines()[-1])
    assert all("error" not in v for v in good.values())
    # and the table's own comparison fails on the error record (it differs from the release's answer)
    pinned = json.loads((DIR / "af4cfc2.json").read_text())
    assert _unexpected({"entry string": pinned["entry string"]}, {"entry string": got["entry string"]}, {})


def test_the_drivers_calls_are_in_the_order_that_makes_the_wait_sound(tmp_path):
    """P3-5. The driver takes a named find on the entry's corpus BEFORE it waits on the listing (a named call waits for that corpus's read; the listing cannot show a
    pending PRIVATE corpus, which is counted inside `private_failed`), takes the listing until it settles, and takes the healthy corpus's find LAST (before it, the find can
    return a moment before registry-error mode begins). The order is pinned from the driver's own trace of its calls: moving `find_badc` back after the wait, or `find_okc`
    before it, fails here."""
    one = tmp_path / "one.json"
    one.write_text(json.dumps({"control: roots": json.loads((DIR / "shapes.json").read_text())["control: roots"]}))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FACTLATTICE_", "LTS_"))}
    env.update(PYTHONPATH=str(REPO), HOME=str(tmp_path), TMPDIR=str(tmp_path), BOOT_VERDICT_TRACE="1")
    p = subprocess.run([sys.executable, "-P", str(DIR / "server_driver.py"), str(one)], cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr[-400:]
    calls = [ln.split()[1:] for ln in p.stderr.splitlines() if ln.startswith("CALL ")]
    names = [(c[0], c[1] if len(c) > 1 else "") for c in calls]
    assert names[0] == ("lts_find", "badc"), names
    lists = [i for i, n in enumerate(names) if n[0] == "lts_list_corpora"]
    assert lists and lists[0] == 1 and names.index(("lts_find", "okc")) > lists[-1], names
