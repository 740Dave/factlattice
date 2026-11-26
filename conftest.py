"""Root collection policy — the PUBLIC suite, plus a PRIVATE overlay when present.

WHY THIS EXISTS
---------------
The engine is domain-agnostic by design: it loads a domain's vocabulary from a
profile at runtime, and that is its narrow waist. So a test that names a real
deployment — its domain id, its corpus path, its pinned finding counts — is not
merely a test with an awkward dependency. It is a fork across that waist, sitting
in a PUBLIC repository, and it discloses a customer by existing.

Those tests are still worth having: a receipt against a real corpus catches what a
hermetic fixture cannot. They just cannot live here. `private/` is gitignored
(see `.gitignore`), so `private/tests/` holds them and this hook collects them when
the directory is present and says nothing when it is not.

    bare `pytest`      -> tests/ + private/tests/ if it exists
    `pytest tests/x.py` -> exactly what was asked for, never widened

⚠️ KNOWN TRADEOFF, NOT SOLVED HERE. A gitignored overlay exists on exactly ONE
machine. It is not backed up by this repo, not reviewed, and not run by anyone
else — so a live-corpus test that rots is invisible to everyone but its author.
The better long-term home is the deploying party's OWN repository, alongside the
corpus the test pins, where the people who change that corpus also own the test
that measures it. Recorded here so the choice is visible rather than inherited.
"""
from __future__ import annotations

import os
import pathlib

import pytest

PRIVATE_OVERLAY = ("private", "tests")


@pytest.fixture(autouse=True, scope="session")
def _isolate_query_log(tmp_path_factory):
    """⛔ THE QUERY LOG IS ON BY DEFAULT (a telemetry layer that must be opted into is
    one nobody has), so the suite would otherwise append several hundred records to the
    developer's real `~/.local/state/factlattice/query-log.jsonl` on every run —
    poisoning the read profile with synthetic fixture traffic and making a `never_read`
    measurement meaningless.

    It redirects rather than DISABLES: `FACTLATTICE_QUERY_LOG=0` would make every run
    exercise the disabled path, leaving the writer untested in exactly the
    configuration it ships in. A temp file keeps the real write path under test.

    ⛔ THIS LIVES AT THE ROOT, NOT IN `tests/`. It used to sit in `tests/conftest.py`,
    where it covered only `tests/`. The moment the private overlay was collected
    alongside it, the live-corpus tests — the ones that hit REAL corpora and therefore
    generate the most realistic-looking log traffic of anything here — would have
    written straight into the developer's actual query log. Moved up so the isolation
    covers everything collected, not everything that happens to sit in one directory.
    """
    d = tmp_path_factory.mktemp("querylog")
    os.environ["FACTLATTICE_QUERY_LOG"] = str(d / "query-log.jsonl")
    os.environ["FACTLATTICE_STATE_DIR"] = str(d / "state")
    yield
    for key in ("FACTLATTICE_QUERY_LOG", "FACTLATTICE_STATE_DIR"):
        os.environ.pop(key, None)


@pytest.fixture(autouse=True)
def _fresh_process_root_registry():
    """[MECH-BR-BOOT] x-library: ONE root registry per PROCESS, so a test that stalls a root must not leave its slots (or a deadline read from
    its own environment) to the next test. Dropped before AND after each test; the next use makes a new one and reads LTS_ROOT_DEADLINE_S then.
    Threads stuck in a dropped registry stay stuck (daemon), as they would in a process."""
    try:
        from factlattice_engine import boundedfs
    except ImportError:                       # a tree before the bounded reads
        yield
        return
    _drop_process_registry(boundedfs)
    yield
    _drop_process_registry(boundedfs)


def _drop_process_registry(boundedfs):
    """Reset the process registry, first letting the old one's IDLE workers leave now. A reset only forgets the registry; its idle workers
    would each wait out idle_exit_s (5 s) and a fast test file piles up thousands of threads (778 tests left ~2,800; a host with a
    2,048-thread cap then fails with "can't start new thread"). A busy or stuck worker is untouched by trim_idle, as before."""
    from factlattice_engine import rootstate
    old = getattr(rootstate, "_PROC", None)
    if old is not None:
        old.trim_idle()
    boundedfs._reset()


@pytest.fixture(autouse=True)
def _unpin_ledger_table():
    """A server pins the table it booted from for the life of the process; in a test process that would outlive the test."""
    yield
    from factlattice import ledger
    ledger.unpin_table()


@pytest.fixture(autouse=True)
def _isolate_registry_lookup(monkeypatch, tmp_path_factory):
    """[TEST-CONFIG-ISOLATION] No test may read the developer's REAL per-user configuration.

    The registry lookup order is env -> per-user pointer file -> built-in example. Once a pointer
    file can exist at `$XDG_CONFIG_HOME/factlattice/config.json` (default `~/.config/...`), a test
    that deletes the registry variable and expects the example registry would silently read the
    developer's own registry instead. So EVERY test gets a fresh, empty HOME and XDG_CONFIG_HOME,
    and the two registry variables start UNSET; a test that needs them sets them itself.

    It lives at the repo root, not in `tests/`, for the reason the query-log fixture does: the
    private overlay is collected alongside and must be covered too.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("FACTLATTICE_CORPUS_CONFIG", raising=False)
    monkeypatch.delenv("FACTLATTICE_CORPUS_ROOT", raising=False)


# THE DEPLOYMENT OVERLAY IS OPT-IN PER TEST. The ambient value is captured once, when pytest is configured, and then REMOVED from every
# test's environment (as the registry variables are above): the engine's identity hash and its profile set both depend on the overlay,
# so a suite run with an overlay in force must not change what an ordinary test measures. A deployment-only test asks for the ambient
# overlay by taking the `deployment_overlay` fixture, which puts back exactly what was set (a usable path, or a broken value that the
# test then skips on, naming it). Captured here because `pytest_configure` runs before any test and before the isolation fixture.
_AMBIENT_OVERLAY = {"raw": None}


@pytest.fixture(autouse=True)
def _isolate_overlay(monkeypatch):
    from factlattice import overlay
    monkeypatch.delenv(overlay.env_var_name(), raising=False)


@pytest.fixture
def deployment_overlay(monkeypatch):
    """The overlay that was in force when the run started (nothing when none was): the test sees the ambient state, as the suite's
    header reported it. Use it ONLY in a test that is deployment-only and skips with a reason when the overlay lacks what it needs."""
    from factlattice import overlay
    if _AMBIENT_OVERLAY["raw"] is not None:
        monkeypatch.setenv(overlay.env_var_name(), _AMBIENT_OVERLAY["raw"])
    return _AMBIENT_OVERLAY["raw"]


def pytest_configure(config):
    from factlattice import overlay
    _AMBIENT_OVERLAY["raw"] = overlay.raw_value()
    # An explicit target on the command line is honoured EXACTLY. Widening a
    # deliberate `pytest tests/test_parser.py` into something else would make the
    # thing that ran differ from the thing that was asked for, and every later
    # measurement would be against an unknown denominator.
    #
    # ⛔ The test is `file_or_dir` — the POSITIONAL targets — not
    # `invocation_params.args`, which also carries every flag. Written against the
    # latter, this hook silently no-ops the moment anyone types `pytest -q`, and a
    # no-op here looks exactly like "the overlay is not installed". Caught by running
    # it, not by reading it.
    if getattr(config.known_args_namespace, "file_or_dir", None):
        return
    overlay = pathlib.Path(config.rootpath).joinpath(*PRIVATE_OVERLAY)
    if overlay.is_dir():
        config.args.append(str(overlay))


def pytest_report_header(config):
    """Say, at the top of every run, whether a deployment overlay is in force: a run with no overlay SKIPS the deployment-only
    tests, and the header (with `-rs`, each skip's own reason) says so rather than leaving a green that reads as complete."""
    import warnings

    from factlattice import overlay
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", overlay.OverlayWarning)        # the header reports the problem itself, once, below
        root = overlay.overlay_root()
    problem = overlay.overlay_problem()
    if root is not None:
        return f"deployment overlay: IN FORCE at {root} (the deployment-only tests run)"
    if problem is not None:
        return f"deployment overlay: NOT IN FORCE, {overlay.env_var_name()} is set but unusable ({problem}); deployment-only tests SKIP"
    return f"deployment overlay: none ({overlay.env_var_name()} is not set); deployment-only tests SKIP, each naming its reason (-rs)"
