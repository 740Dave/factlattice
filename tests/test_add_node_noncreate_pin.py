"""SCOPE PIN (architect bound 1): `outcome: "unknown"` and every create-only rule exist ONLY on the create
path. Every `lts_add_node` call WITHOUT create/create_parent returns exactly the replies recorded at 8861625
(tests/fixtures/add_node_noncreate_base.json, recorded by tests/fixtures/record_add_node_base.py on that
commit): success and refusal cases, wrong-typed arguments, a core OSError and a raw core exception. Field by
field, normalised only for the temp root, backup stamps and times."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


def _recorder():
    spec = importlib.util.spec_from_file_location("record_add_node_base", FIX / "record_add_node_base.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    import os
    t = tmp_path_factory.mktemp("pin")
    old = {k: os.environ.get(k) for k in ("FACTLATTICE_STATE_DIR", "FACTLATTICE_BACKUP_DIR", "FACTLATTICE_LEDGER_EPHEMERAL_ROOTS")}
    os.environ.update(FACTLATTICE_STATE_DIR=str(t / "state"), FACTLATTICE_BACKUP_DIR=str(t / "bak"),
                      FACTLATTICE_LEDGER_EPHEMERAL_ROOTS="")
    try:
        yield _recorder().run(t)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


BASE = json.loads((FIX / "add_node_noncreate_base.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(BASE))
def test_noncreate_reply_is_identical_to_8861625(replay, name):
    assert replay[name] == BASE[name], name


def test_no_create_only_key_appears_on_any_noncreate_reply(replay):
    for name, r in replay.items():
        if isinstance(r, dict):
            for k in ("outcome", "created", "created_parent", "inner_refusal", "receipt_error"):
                assert k not in r, (name, k)
            assert (r.get("evidence") or {}).get("refusal") not in (
                "create_outcome_unknown", "create_unexpected_error", "create_refused", "create_argument_invalid"), name


def test_the_pin_covers_enough():
    assert len(BASE) >= 24
