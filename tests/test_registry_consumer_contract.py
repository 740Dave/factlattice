"""Consumer contract for factlattice.registry.resolved_entries, relied on by another product's registry readers. Changing any pinned behaviour here reaches that product: the land record must name it and that product's maintainers must be told before the land.

What is pinned, each in its own test:
  1. the return shape: a dict of corpus id to entry dict; the `named_roots` key never appears, and no non-corpus key is added;
  2. a failed entry (an unknown `${name}`, or a malformed table that a corpus uses) keeps its value AS WRITTEN and carries `registry.NAMED_FAILURE_KEY` with keys exactly `code`, plus
     `name` or `detail`, plus `root` only when given, and no other key;
  3. the failure codes, as one set read from the engine's own constants;
  4. FAILURE IS THE KEY: every entry that carries `named_root_failure` is a failure, whatever its code. A reader must treat the KEY as the failure, never a list of known codes, or a code
     added later reads as success;
  5. `RegistryError` for a missing file, unparseable JSON and a non-object top level;
  6. a good entry with `${name}` expands to the table's absolute path, and an entry with no reference is returned unchanged.
"""
from __future__ import annotations

import json
import os

import pytest

from factlattice import registry


def _write(tmp_path, data, name="reg.json"):
    p = tmp_path / name
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return str(p)


def _table(tmp_path):
    root = tmp_path / "proj-root"
    root.mkdir(exist_ok=True)
    return {"proj": {"path": str(root)}}, os.path.realpath(str(root))


def test_the_return_is_a_dict_of_corpus_id_to_entry_dict_and_never_holds_the_table(tmp_path):
    """1. A dict of corpus id to entry dict; `named_roots` never appears even when the file holds a table; no non-corpus key is added (the keys are the file's, less the table)."""
    table, _root = _table(tmp_path)
    data = {"named_roots": table, "a": {"root": str(tmp_path / "a-root")}, "b": {"roots": [{"path": "${proj}/sub"}]}, "c_note": "free text"}
    got = registry.resolved_entries(_write(tmp_path, data))
    assert isinstance(got, dict) and registry.NAMED_ROOTS_KEY not in got
    assert set(got) == {"a", "b", "c_note"}, "exactly the file's keys, less the table: nothing added"
    assert all(isinstance(k, str) for k in got)
    assert isinstance(got["a"], dict) and isinstance(got["b"], dict)
    noroots = registry.resolved_entries(_write(tmp_path, {"a": {"root": str(tmp_path / "a-root")}}, "plain.json"))
    assert set(noroots) == {"a"}


def test_a_failed_entry_keeps_its_value_as_written_and_carries_exactly_the_failure_keys(tmp_path):
    """2. For an unknown `${name}` and for a malformed table that a corpus uses: the value stays as written (the unexpanded string) and the entry carries `named_root_failure` whose keys are
    exactly `code` plus `name` or `detail`, plus `root` only when given. No other key."""
    assert registry.NAMED_FAILURE_KEY == "named_root_failure"
    table, _root = _table(tmp_path)
    unknown = registry.resolved_entries(_write(tmp_path, {"named_roots": table, "u": {"root": "${nosuch}/thing", "domain_suffix": "x.u"}}, "u.json"))["u"]
    assert unknown["root"] == "${nosuch}/thing", "as written, not expanded"
    f = unknown[registry.NAMED_FAILURE_KEY]
    assert set(f) == {"code", "name"} and f["name"] == "nosuch" and f["code"] == registry.NAMED_ROOT_UNKNOWN
    assert unknown["domain_suffix"] == "x.u", "the rest of the entry is untouched"
    bad_ref = registry.resolved_entries(_write(tmp_path, {"named_roots": table, "r": {"root": "${NOT A NAME}"}}, "r.json"))["r"]
    assert bad_ref["root"] == "${NOT A NAME}" and set(bad_ref[registry.NAMED_FAILURE_KEY]) == {"code", "detail"}
    malformed = registry.resolved_entries(_write(tmp_path, {"named_roots": "not an object", "m": {"roots": [{"path": "${proj}/x"}]}}, "m.json"))["m"]
    assert malformed["roots"] == [{"path": "${proj}/x"}], "as written"
    mf = malformed[registry.NAMED_FAILURE_KEY]
    assert set(mf) <= {"code", "detail", "root"} and {"code", "detail"} <= set(mf) and mf["code"] == registry.NAMED_ROOTS_MALFORMED
    with_root = registry.resolved_entries(_write(tmp_path, {"named_roots": {"p": "not an object"}, "w": {"root": "${p}"}}, "w.json"))["w"]
    wf = with_root[registry.NAMED_FAILURE_KEY]
    assert set(wf) == {"code", "detail", "root"} and wf["root"] == "p", "`root` appears only when the table problem names one"
    assert set(unknown[registry.NAMED_FAILURE_KEY]) == {"code", "name"} and "root" not in unknown[registry.NAMED_FAILURE_KEY]


def test_the_failure_codes_are_one_pinned_set_read_from_the_engines_own_constants():
    """3. The codes a failure can carry, as the engine defines them (no literal copied here). Adding or removing a code changes `NAMED_ROOT_CODES`, and this test says so: it is a change a
    consumer must be told about."""
    assert set(registry.NAMED_ROOT_CODES) == {registry.NAMED_ROOT_UNKNOWN, registry.NAMED_ROOTS_MALFORMED}
    assert len(registry.NAMED_ROOT_CODES) == 2 and len(set(registry.NAMED_ROOT_CODES)) == 2
    assert all(isinstance(c, str) and c == c.lower() for c in registry.NAMED_ROOT_CODES)
    # the codes a failure of resolved_entries actually carries are among them (the two expansion routes)
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        seen = set()
        for i, data in enumerate(({"x": {"root": "${nosuch}"}}, {"named_roots": 7, "x": {"root": "${a}"}}, {"x": {"root": "${A}"}})):
            e = registry.resolved_entries(_write(p, data, f"c{i}.json"))["x"]
            seen.add(e[registry.NAMED_FAILURE_KEY]["code"])
        assert seen == set(registry.NAMED_ROOT_CODES), seen


def test_failure_is_the_key_whatever_the_code(tmp_path, monkeypatch):
    """4. FAILURE IS THE KEY. A reader must treat an entry as failed when it carries `named_root_failure`, never when its code is one it knows: a code added later (here a NEW code is planted into
    the expansion) still arrives as the key, with the value as written. A reader that listed the known codes would read this entry as success."""
    monkeypatch.setattr(registry, "NAMED_ROOT_UNKNOWN", "named-root-brand-new")
    e = registry.resolved_entries(_write(tmp_path, {"x": {"root": "${nosuch}/thing"}}))["x"]
    assert registry.NAMED_FAILURE_KEY in e, "the key is there for a code the engine did not have before"
    assert e[registry.NAMED_FAILURE_KEY]["code"] == "named-root-brand-new" and e[registry.NAMED_FAILURE_KEY]["code"] not in registry.NAMED_ROOT_CODES
    assert e["root"] == "${nosuch}/thing", "and the value is still as written"


@pytest.mark.parametrize("content,kind", [(None, "missing"), ("{ not json", "unparseable"), ("[1, 2]", "not an object"), ("\"text\"", "a string")])
def test_a_registry_that_cannot_be_read_raises_registry_error(tmp_path, content, kind):
    """5. `RegistryError` for a missing file, unparseable JSON and a top level that is not an object."""
    path = str(tmp_path / "gone.json") if content is None else _write(tmp_path, content, "bad.json")
    with pytest.raises(registry.RegistryError):
        registry.resolved_entries(path)


def test_a_good_reference_expands_to_the_tables_absolute_path_and_an_entry_without_one_is_unchanged(tmp_path):
    """6. `${name}` and `${name}/relative` expand to the table's absolute path; an entry with no reference (absolute root, nothing to expand) comes back equal to what was written."""
    table, root = _table(tmp_path)
    plain = {"root": str(tmp_path / "plain-root"), "domain_suffix": "x.plain"}
    got = registry.resolved_entries(_write(tmp_path, {"named_roots": table, "g": {"roots": [{"path": "${proj}"}, {"path": "${proj}/sub"}]}, "h": {"root": "${proj}/deep/er"}, "p": plain}))
    assert os.path.isabs(got["g"]["roots"][0]["path"]) and got["g"]["roots"][0]["path"] == root
    assert got["g"]["roots"][1]["path"] == os.path.join(root, "sub")
    assert got["h"]["root"] == os.path.join(root, "deep/er")
    for k in ("g", "h"):
        assert registry.NAMED_FAILURE_KEY not in got[k], "a good entry carries no failure"
    assert got["p"] == plain, "an entry with no reference is returned unchanged"
