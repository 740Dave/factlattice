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


def pytest_configure(config):
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
