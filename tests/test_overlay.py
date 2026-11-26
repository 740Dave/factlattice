"""The deployment overlay seam (R1 step 2): ONE resolver, one variable, nothing hard-coded.

Each behaviour has a test that is RED on the tree before the seam existed (the module does not exist there, so every test here
fails at import: that is the RED recorded in the commit) and a NEGATIVE CONTROL where the check could pass for the wrong reason:
the link case would pass under `realpath` and fails under `lstat`; the single-reader scan is shown to FIRE on a planted second reader.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import warnings
from pathlib import Path

import pytest

from tests import _registry_guard as G
from tests._snapshot import is_published_snapshot, skip_reason

REPO = Path(__file__).resolve().parent.parent
VAR = "FACTLATTICE_OVERLAY"


@pytest.fixture
def ov():
    from factlattice import overlay

    overlay._WARNED.clear()  # "warns once" is per process: each test starts with nothing warned
    yield overlay
    overlay._WARNED.clear()


def _made(tmp_path, name="overlay"):
    d = tmp_path / name
    d.mkdir()
    return d


# ------------------------------------------------------------------------------------------------- 1. the resolver
def test_unset_means_no_overlay_and_says_nothing(ov, monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert ov.overlay_root() is None and ov.overlay_root(strict=True) is None
    assert ov.overlay_problem() is None


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_value_is_unset(ov, monkeypatch, value):
    monkeypatch.setenv(VAR, value)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert ov.overlay_root() is None and ov.overlay_root(strict=True) is None


def test_a_real_directory_is_returned(ov, monkeypatch, tmp_path):
    d = _made(tmp_path)
    monkeypatch.setenv(VAR, str(d))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert ov.overlay_root() == d and ov.overlay_root(strict=True) == d
    assert ov.overlay_problem() is None


def _unusable_value(what, tmp_path, monkeypatch):
    """(value, the reason text the resolver must give). Every case would pass for a SECOND reason if the check under test were removed
    (a relative path that does not exist, a file that is not executable): so each case is built to be unusable for exactly ONE."""
    real = _made(tmp_path, "real")
    if what == "missing":
        return str(tmp_path / "nope"), "does not exist"
    if what == "file":
        f = tmp_path / "f"
        f.write_text("x")
        f.chmod(0o755)  # readable AND executable: only 'not a directory' remains
        return str(f), "not a directory"
    if what == "relative":
        monkeypatch.chdir(tmp_path)  # `real` exists from here: only 'not absolute' remains
        return "real", "not an absolute path"
    if what == "broken-link":
        (tmp_path / "dangling").symlink_to(tmp_path / "nothing-here")
        return str(tmp_path / "dangling"), "is a link"
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    return {"link": str(link), "link/": str(link) + "/", "link/.": str(link) + "/.", "link//": str(link) + "//"}[what], "is a link"


BAD = ["missing", "file", "relative", "broken-link", "link", "link/", "link/.", "link//"]


def test_a_link_to_a_real_directory_is_not_usable_lstat_not_realpath(ov, monkeypatch, tmp_path):
    """The negative control: `realpath`/`isdir` would ACCEPT this (it points at a real directory). Only an lstat of the named path refuses."""
    real = _made(tmp_path, "real")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert link.is_dir() and os.path.realpath(link) == str(real)  # what a lazier check would have accepted
    monkeypatch.setenv(VAR, str(link))
    with pytest.warns(ov.OverlayWarning, match="is a link"):
        assert ov.overlay_root() is None
    with pytest.raises(ov.OverlayUnusable) as e:
        ov.overlay_root(strict=True)
    assert "link" in e.value.reason


@pytest.mark.parametrize("what", ["link/", "link/.", "link//"])
def test_a_link_spelled_with_a_trailing_slash_or_dot_is_still_a_link(ov, monkeypatch, tmp_path, what):
    """F1: `lstat('link/')` and `lstat('link/.')` FOLLOW the link (POSIX), so an lstat of the raw value accepted them. The check is on the
    normalised path. Shell tab-completion appends the slash, so this is the ordinary spelling of the mistake."""
    value, _ = _unusable_value(what, tmp_path, monkeypatch)
    assert os.path.isdir(value) and not os.path.islink(value)  # the premise: the raw spelling hides the link from lstat
    assert not stat_is_link(value)
    monkeypatch.setenv(VAR, value)
    assert ov.overlay_problem() is not None and "is a link" in ov.overlay_problem()
    with pytest.raises(ov.OverlayUnusable, match="is a link"):
        ov.overlay_root(strict=True)


def stat_is_link(path):
    import stat as _stat

    return _stat.S_ISLNK(os.lstat(path).st_mode)


@pytest.mark.parametrize("what", BAD)
def test_set_but_unusable_warns_once_in_a_dev_run(ov, monkeypatch, tmp_path, what):
    value, reason = _unusable_value(what, tmp_path, monkeypatch)
    monkeypatch.setenv(VAR, value)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        assert ov.overlay_root() is None
        assert ov.overlay_root() is None  # a second call: no second warning
        assert ov.overlay_root() is None
    mine = [x for x in w if issubclass(x.category, ov.OverlayWarning)]
    assert len(mine) == 1, [str(x.message) for x in mine]
    assert value in str(mine[0].message) and "NO overlay" in str(mine[0].message)
    assert reason in str(mine[0].message), (reason, str(mine[0].message))  # F2: the REASON, so removing one refusal turns this red
    assert reason in ov.overlay_problem()


def test_a_different_bad_value_warns_again(ov, monkeypatch, tmp_path):
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        for v in (str(tmp_path / "a"), str(tmp_path / "b")):
            monkeypatch.setenv(VAR, v)
            ov.overlay_root()
    assert len([x for x in w if issubclass(x.category, ov.OverlayWarning)]) == 2


@pytest.mark.parametrize("what", BAD)
def test_set_but_unusable_refuses_in_a_release_run(ov, monkeypatch, tmp_path, what):
    value, reason = _unusable_value(what, tmp_path, monkeypatch)
    monkeypatch.setenv(VAR, value)
    with pytest.raises(ov.OverlayUnusable) as e:
        ov.overlay_root(strict=True)
    assert reason in e.value.reason and VAR in str(e.value), (reason, e.value.reason)


def test_overlay_root_returns_the_normalised_path(ov, monkeypatch, tmp_path):
    """F13: the path that was CHECKED (lstat of the normalised value) must be the path that is USED. pathlib hides the difference for a
    trailing `/` or `/.`, not for `..`: `x/sub/../ov` must come back as `x/ov`. If the raw spelling came back, a link inside the
    `..` walk could be checked at one path and used at another."""
    x = _made(tmp_path, "x")
    (x / "sub").mkdir()
    (x / "ov").mkdir()
    for raw in (f"{x}/sub/../ov", f"{x}/ov/", f"{x}/ov/.", f"{x}//ov", f"{x}/./ov"):
        monkeypatch.setenv(VAR, raw)
        got = ov.overlay_root()
        assert got == x / "ov" and ".." not in got.parts and str(got) == f"{x}/ov", (raw, str(got))
        assert ov.overlay_root(strict=True) == x / "ov"


def test_overlay_path_stays_inside_the_layout(ov, monkeypatch, tmp_path):
    d = _made(tmp_path)
    (d / "domain-profiles").mkdir()
    (d / "domain-profiles" / "x.lts").write_text("x")
    (tmp_path / "outside.txt").write_text("secret")
    monkeypatch.setenv(VAR, str(d))
    assert ov.overlay_path("domain-profiles", "x.lts") == d / "domain-profiles" / "x.lts"
    assert ov.overlay_path("domain-profiles", "absent.lts") is None
    assert ov.overlay_path("..", "outside.txt") is None  # negative control: the file EXISTS, the path escapes
    assert (d / ".." / "outside.txt").exists()
    assert ov.overlay_path(str(tmp_path / "outside.txt")) is None
    monkeypatch.delenv(VAR)
    assert ov.overlay_path("domain-profiles") is None


def test_the_skip_reason_names_what_is_missing_and_why(ov, monkeypatch, tmp_path):
    monkeypatch.delenv(VAR, raising=False)
    assert (
        VAR in ov.skip_reason("the witness")
        and "not set" in ov.skip_reason("the witness")
        and "the witness" in ov.skip_reason("the witness")
    )
    d = _made(tmp_path)
    monkeypatch.setenv(VAR, str(d))
    assert "does not provide it" in ov.skip_reason("the witness") and str(d) in ov.skip_reason("the witness")
    monkeypatch.setenv(VAR, str(tmp_path / "gone"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert "not usable" in ov.skip_reason("the witness")


# ------------------------------------------------------------------------------------- the single-reader rule
def test_the_real_tree_has_no_second_reader_of_the_overlay_variable():
    assert G.scan_overlay_tree(REPO) == []


def test_the_overlay_scan_fires_on_every_form_of_second_reader(tmp_path):
    """The negative control: each planted file must be found, and the resolver itself must not be."""
    forms = {
        "plain.py": 'import os\nx = os.environ.get("FACTLATTICE_OVERLAY")\n',
        "concat.py": 'import os\nx = os.environ.get("FACTLATTICE_" + "OVERLAY")\n',
        "joined.py": 'import os\nx = os.environ.get("".join(["FACTLATTICE", "_", "OVERLAY"]))\n',
        "fstring.py": 'import os\nA = "FACTLATTICE"\nx = os.environ.get(f"{A}_OVERLAY")\n',
        "reversed.py": 'import os\nx = os.environ.get("YALREVO_ECITTALTCAF"[::-1])\n',
        "const.py": "from factlattice.overlay import ENV_OVERLAY\n",
        "attr.py": "from factlattice import overlay\nx = overlay.ENV_OVERLAY\n",
    }
    for name, body in forms.items():
        (tmp_path / name).write_text(body)
    got = G.scan_overlay_tree(tmp_path)
    assert {r[0] for r in got} == set(forms), sorted({r[0] for r in got} ^ set(forms))
    (tmp_path / "factlattice").mkdir()
    (tmp_path / "factlattice" / "overlay.py").write_text('import os\nx = os.environ.get("FACTLATTICE_OVERLAY")\n')
    assert not any(r[0] == "factlattice/overlay.py" for r in G.scan_overlay_tree(tmp_path))  # the resolver is exempt


def test_the_registry_resolver_may_not_read_the_overlay_variable_either(tmp_path):
    """A separate scan, so the registry's allow-list does not carry over: registry.py naming the overlay variable is a finding."""
    (tmp_path / "factlattice").mkdir()
    (tmp_path / "factlattice" / "registry.py").write_text('import os\nx = os.environ.get("FACTLATTICE_OVERLAY")\n')
    assert [r[0] for r in G.scan_overlay_tree(tmp_path)] == ["factlattice/registry.py"]


# ------------------------------------------------------------------------- the profile directory and the registry hash
PROFILE = textwrap.dedent("""\
    ---
    title: "Domain Profile: Synthetic"
    ---
    # Synthetic [DOMAIN:SYN]

    | Type | Layer |
    |---|---|
    | syn.USES | semantic |
    """)


def _overlay_with_profile(tmp_path, name="overlay"):
    d = _made(tmp_path, name)
    (d / "domain-profiles").mkdir()
    (d / "domain-profiles" / "syn.lts").write_text(PROFILE, encoding="utf-8")
    return d


def test_no_overlay_leaves_the_profile_dir_exactly_as_it_was(monkeypatch):
    from factlattice import domain_profiles as dp

    monkeypatch.delenv(VAR, raising=False)
    monkeypatch.delenv("FACTLATTICE_PROFILE_DIR", raising=False)
    assert dp.default_profile_dirs() == (dp.default_profile_dir(),)


def test_the_overlay_profile_dir_is_read_after_the_shipped_one(ov, monkeypatch, tmp_path):
    from factlattice import domain_profiles as dp

    d = _overlay_with_profile(tmp_path)
    monkeypatch.setenv(VAR, str(d))
    monkeypatch.delenv("FACTLATTICE_PROFILE_DIR", raising=False)
    assert dp.default_profile_dirs() == (dp.default_profile_dir(), str(d / "domain-profiles"))
    cfg = dp.load_profile_edge_config(dp.default_profile_dirs())
    assert cfg.edge_layers.get("syn.USES") == "semantic" and "syn" in cfg.registered_ns  # the edge reaches the config
    # the negative control: the same profile, no overlay, is absent
    monkeypatch.delenv(VAR)
    assert "syn.USES" not in dp.load_profile_edge_config(dp.default_profile_dirs()).edge_layers


def test_an_overlay_with_no_profiles_adds_nothing(ov, monkeypatch, tmp_path):
    from factlattice import domain_profiles as dp

    monkeypatch.setenv(VAR, str(_made(tmp_path)))
    monkeypatch.delenv("FACTLATTICE_PROFILE_DIR", raising=False)
    assert dp.default_profile_dirs() == (dp.default_profile_dir(),)


def test_an_explicit_profile_dir_is_authoritative(ov, monkeypatch, tmp_path):
    from factlattice import domain_profiles as dp

    monkeypatch.setenv(VAR, str(_overlay_with_profile(tmp_path)))
    monkeypatch.setenv("FACTLATTICE_PROFILE_DIR", str(tmp_path / "explicit"))
    assert dp.default_profile_dirs() == (str(tmp_path / "explicit"),)


def test_the_engine_config_follows_the_overlay(ov, monkeypatch, tmp_path):
    """Through the engine's own entry point (`engine_profile_config`), not the helper: memoised per directory SET, so toggling the overlay changes the answer."""
    from factlattice_engine import config

    monkeypatch.delenv("FACTLATTICE_PROFILE_DIR", raising=False)
    monkeypatch.delenv(VAR, raising=False)
    assert "syn.USES" not in config.engine_profile_config().edge_layers
    monkeypatch.setenv(VAR, str(_overlay_with_profile(tmp_path)))
    assert config.engine_profile_config().edge_layers.get("syn.USES") == "semantic"


def _digest():
    from factlattice import registry

    return registry._digest(b"{}", "/r", {})


def test_the_registry_hash_moves_only_with_the_loaded_overlay_profiles(ov, monkeypatch, tmp_path):
    """The ruling: the identity hash changes only when the LOADED profile content changes. An overlay that loads nothing is no input."""
    monkeypatch.delenv("FACTLATTICE_PROFILE_DIR", raising=False)
    monkeypatch.delenv(VAR, raising=False)
    base = _digest()
    nodir = _made(tmp_path, "nodir")
    monkeypatch.setenv(VAR, str(nodir))
    assert _digest() == base  # an overlay with no domain-profiles/
    (nodir / "domain-profiles").mkdir()
    assert _digest() == base  # ... and with an EMPTY one: byte-identical (F3)
    (nodir / "domain-profiles" / "notes.txt").write_text("not a profile")
    assert _digest() == base  # a file the loader does not read is no input
    d = _overlay_with_profile(tmp_path)
    monkeypatch.setenv(VAR, str(d))
    withov = _digest()
    assert withov != base  # a profile present: a different hash
    (d / "domain-profiles" / "syn.lts").write_text(PROFILE.replace("syn.USES", "syn.OWNS"), encoding="utf-8")
    assert _digest() not in (base, withov)  # its CONTENT changed: the hash moved again
    (d / "domain-profiles" / "syn.lts").write_text(PROFILE, encoding="utf-8")
    assert _digest() == withov
    other = _overlay_with_profile(tmp_path, "other")
    monkeypatch.setenv(VAR, str(other))
    assert _digest() == withov  # the same loaded content from another path: the same hash
    monkeypatch.setenv("FACTLATTICE_PROFILE_DIR", str(tmp_path / "explicit"))
    explicit = _digest()
    monkeypatch.delenv(VAR)
    assert explicit == _digest()  # explicit dir wins: the overlay is not hashed under it


def test_the_hash_reads_the_overlay_profile_dir_once(ov, monkeypatch, tmp_path):
    """F7: a directory that vanishes between two looks must not raise inside `_digest` (it used to: two calls, `.encode` on None)."""
    from factlattice import overlay

    monkeypatch.delenv("FACTLATTICE_PROFILE_DIR", raising=False)
    calls = []
    answers = iter([str(_overlay_with_profile(tmp_path) / "domain-profiles"), None, None, None])

    def once_then_gone():
        calls.append(1)
        return next(answers)

    monkeypatch.setattr(overlay, "overlay_profile_dir", once_then_gone)
    _digest()  # must not raise
    assert len(calls) == 1, calls


# ------------------------------------------------------------------------------------------ move 1: a deployment's own profile
SHIPPED_PROFILES = [
    "agentic_artifacts.lts",
    "agentic_decisions.lts",
    "clinical-medical.lts",
    "external-company-intelligence.lts",
    "software-engineering.lts",
]


def test_the_public_tree_ships_exactly_the_generic_reference_profiles():
    """Pins the shipped set by NAME, so a profile that belongs to one deployment cannot come back into the public tree unnoticed
    (the one that left, the hardware-workspace profile, now lives only in the overlay layout)."""
    shipped = sorted(p.name for p in (REPO / "domain-profiles").glob("*.lts"))
    assert shipped == SHIPPED_PROFILES, shipped
    assert sorted(p.name for p in (REPO / "domain-profiles").iterdir()) == SHIPPED_PROFILES


def test_a_profile_in_the_overlay_layout_is_found_through_the_resolver(ov, monkeypatch, tmp_path):
    d = _made(tmp_path)
    (d / "domain-profiles").mkdir()
    (d / "domain-profiles" / "workspace.lts").write_text(PROFILE, encoding="utf-8")
    monkeypatch.setenv(VAR, str(d))
    assert ov.overlay_path("domain-profiles", "workspace.lts") == d / "domain-profiles" / "workspace.lts"
    monkeypatch.delenv(VAR)
    assert ov.overlay_path("domain-profiles", "workspace.lts") is None  # absent cleanly without an overlay


# --------------------------------------------------------------------------------- the report header and the release run
def test_the_run_header_says_whether_an_overlay_is_in_force(ov, monkeypatch, tmp_path):
    import conftest

    monkeypatch.delenv(VAR, raising=False)
    assert "none" in conftest.pytest_report_header(None) and "SKIP" in conftest.pytest_report_header(None)
    monkeypatch.setenv(VAR, str(_made(tmp_path)))
    assert "IN FORCE" in conftest.pytest_report_header(None)
    monkeypatch.setenv(VAR, str(tmp_path / "gone"))
    h = conftest.pytest_report_header(None)
    assert "NOT IN FORCE" in h and "does not exist" in h


def _release_prep():
    import importlib.util

    spec = importlib.util.spec_from_file_location("release_prep_under_test", REPO / "tools" / "release_prep.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.mark.skipif(is_published_snapshot(), reason=skip_reason("tools/release_prep.py"))
def test_a_required_release_run_refuses_an_unusable_overlay_and_a_dev_run_does_not(ov, monkeypatch, tmp_path, capsys):
    rp = _release_prep()
    monkeypatch.setenv(VAR, str(tmp_path / "gone"))
    with pytest.raises(rp.Refusal) as e:
        rp.step_overlay(True)
    assert "refuses" in str(e.value) and "gone" in str(e.value)
    with pytest.warns(ov.OverlayWarning):  # the same input, a non-required run: a warning and a line
        line = rp.step_overlay(False)
    assert "unusable" in line and "not passed" in line


@pytest.mark.skipif(is_published_snapshot(), reason=skip_reason("tools/release_prep.py"))
def test_a_release_run_with_a_usable_overlay_says_it_is_not_passed_on_and_the_children_cannot_see_it(ov, monkeypatch, tmp_path):
    rp = _release_prep()
    monkeypatch.setenv(VAR, str(_made(tmp_path)))
    assert "NOT passed" in rp.step_overlay(True)
    rp.set_scratch_home(tmp_path / "home")
    env = rp.scrubbed_env()
    assert VAR not in env and not any("overlay" in k.lower() for k in env), sorted(env)
    monkeypatch.delenv(VAR)
    assert rp.step_overlay(True).startswith("deployment overlay: none")


def test_the_suite_with_no_overlay_runs_the_public_tests_and_skips_the_witness_with_its_reason(tmp_path):
    """Through a real pytest subprocess in a scratch HOME, the way a stranger's clone runs it."""
    env = {k: v for k, v in os.environ.items() if k != VAR and not k.startswith("FACTLATTICE_")}
    env.update({"HOME": str(tmp_path / "h"), "XDG_CONFIG_HOME": str(tmp_path / "h" / ".config"), "PYTHONDONTWRITEBYTECODE": "1"})
    (tmp_path / "h").mkdir()
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_reviewer_dangles.py", "-rs", "-p", "no:cacheprovider"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = r.stdout
    assert r.returncode == 0, out[-1500:]
    assert "deployment overlay: none" in out
    assert "SKIPPED" in out and "the real-document witness" in out and "is not set (no deployment overlay)" in out


# ------------------------------------------------------------------ the overlay is opt-in per test (the ambient one is removed)
def test_an_ordinary_test_never_sees_the_ambient_overlay():
    from factlattice import overlay

    assert overlay.raw_value() is None and overlay.overlay_root() is None


def test_the_deployment_overlay_fixture_gives_the_ambient_state_back(deployment_overlay):
    from factlattice import overlay

    assert overlay.raw_value() == deployment_overlay
    assert (overlay.overlay_root() is None) if deployment_overlay is None else (overlay.overlay_root() == Path(deployment_overlay))


def test_a_run_with_an_ambient_overlay_hides_it_from_ordinary_tests_and_gives_it_to_the_fixture(tmp_path):
    """Through a real pytest subprocess with the variable set: the two tests above must PASS there too (the first proves the isolation,
    the second the hand-back), and the header must say it is in force."""
    d = _made(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith("FACTLATTICE_")}
    env.update({VAR: str(d), "PYTHONDONTWRITEBYTECODE": "1"})
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_overlay.py",
            "-p",
            "no:cacheprovider",
            "-q",
            "-k",
            "an_ordinary_test_never_sees or deployment_overlay_fixture_gives",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert r.returncode == 0 and "2 passed" in r.stdout, r.stdout[-1500:]
    r2 = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_overlay.py", "-p", "no:cacheprovider", "--co", "-k", "no_such_test"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert f"deployment overlay: IN FORCE at {d}" in r2.stdout, r2.stdout[-800:]


# ------------------------------------------- F5: a test that runs WITH an overlay in force, so an overlay-caused break is seen
CLASH = textwrap.dedent("""\
    ---
    title: "Domain Profile: Clash"
    ---
    # Clash [DOMAIN:CLASH]

    | Type | Layer |
    |---|---|
    | dec.DEPENDS_ON | temporal |
    """)


def _overlay_profiles_load_clean_with_the_shipped_five(tmp_path):
    """What a deployment's overlay must satisfy: its profiles merge with the shipped ones with no layer collision (a collision stops the
    WHOLE registry: every corpus fails with registry-not-found), and a corpus built through the engine's own entry point reads."""
    from factlattice import domain_profiles as dp
    from factlattice_engine import config

    cfg = dp.load_profile_edge_config(dp.default_profile_dirs())  # raises ProfileCollisionError on a clash
    root = tmp_path / "corpus-under-overlay"
    root.mkdir()
    (root / "a.lts").write_text('---\nlts: "1.1"\ntitle: A\n---\n# A [SPEC:A-ROOT]\n\n## One [A-1]\n\n- status: active\n', encoding="utf-8")
    part = config.build_partition("overlay-probe", str(root), "overlay.probe", profile=config.engine_profile_config())
    assert part.get_node("A-1") is not None
    return cfg


def test_the_ambient_overlay_profiles_load_clean_with_the_shipped_five(tmp_path, deployment_overlay):
    """DEPLOYMENT-ONLY, and the one test that sees the overlay the run started with (the suite strips it from every other test).
    Without an overlay it skips with the reason; with one, a clashing profile in it FAILS here, loudly, instead of passing a green run
    that the served path would not survive."""
    from factlattice import overlay

    if overlay.overlay_profile_dir() is None:
        pytest.skip(overlay.skip_reason("the overlay's profile directory (domain-profiles/)"))
    _overlay_profiles_load_clean_with_the_shipped_five(tmp_path)


def test_a_clashing_overlay_profile_fails_that_check_loudly_and_a_clean_one_passes(ov, monkeypatch, tmp_path):
    """The check above, driven with a SYNTHETIC overlay in force (not stripped): clash -> ProfileCollisionError; clean -> passes."""
    from factlattice.profile import ProfileCollisionError

    monkeypatch.delenv("FACTLATTICE_PROFILE_DIR", raising=False)
    bad = _made(tmp_path, "bad")
    (bad / "domain-profiles").mkdir()
    (bad / "domain-profiles" / "clash.lts").write_text(CLASH, encoding="utf-8")
    monkeypatch.setenv(VAR, str(bad))
    (tmp_path / "x").mkdir()
    with pytest.raises(ProfileCollisionError, match="dec.DEPENDS_ON"):
        _overlay_profiles_load_clean_with_the_shipped_five(tmp_path / "x")
    good = _overlay_with_profile(tmp_path, "good")
    monkeypatch.setenv(VAR, str(good))
    (tmp_path / "y").mkdir()
    cfg = _overlay_profiles_load_clean_with_the_shipped_five(tmp_path / "y")
    assert cfg.edge_layers["syn.USES"] == "semantic"
