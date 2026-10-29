"""[F-A1] A typed writer's `_serialised` finally popped `_BASES.d` directly. That thread-local is
created lazily by `_bases()`, so a writer that REFUSED before any `_bases()` call, on a thread's FIRST
writer call, raised AttributeError from the finally and masked the real EditRefused (the refusal reason is
the receipt's whole value). Fix: `_bases().pop(...)`."""
from __future__ import annotations

import threading

import pytest

from factlattice.authoring import edit
from factlattice.authoring.edit import EditRefused


def _in_new_thread(fn):
    box = {}

    def run():
        try:
            fn()
        except BaseException as e:  # noqa: BLE001
            box["exc"] = e
    t = threading.Thread(target=run)
    t.start()
    t.join()
    return box.get("exc")


def test_add_node_on_missing_path_in_a_fresh_thread_raises_the_named_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    (tmp_path / "corpus").mkdir(exist_ok=True)
    missing = tmp_path / "corpus" / "nope.lts"
    exc = _in_new_thread(lambda: edit.add_node(missing, "X-ONE", "t", {"kind": "a"}))
    assert isinstance(exc, EditRefused), f"expected EditRefused, got {type(exc).__name__}: {exc}"
    assert "is not a file" in str(exc)
    assert not missing.exists()


@pytest.mark.parametrize("writer,args", [
    ("set_property", ("X-ONE", "k", "v")),
    ("repair_property", ("X-ONE", 3, "k")),
])
def test_every_serialised_writer_refuses_by_name_in_a_fresh_thread(tmp_path, monkeypatch, writer, args):
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FACTLATTICE_BACKUP_DIR", str(tmp_path / "bak"))
    (tmp_path / "corpus").mkdir(exist_ok=True)
    missing = tmp_path / "corpus" / "nope.lts"
    exc = _in_new_thread(lambda: getattr(edit, writer)(missing, *args))
    assert isinstance(exc, EditRefused), f"{writer}: {type(exc).__name__}: {exc}"


def test_json_refusal_through_the_adapter_in_a_fresh_thread(tmp_path, monkeypatch):
    from factlattice_engine import authoring
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "corpus").mkdir(exist_ok=True)
    box = {}
    t = threading.Thread(target=lambda: box.update(r=authoring.add_node(
        str(tmp_path / "corpus" / "nope.lts"), "X-ONE", "t", {"kind": "a"})))
    t.start(); t.join()
    assert box["r"]["refused"] is True and "is not a file" in box["r"]["reason"]
