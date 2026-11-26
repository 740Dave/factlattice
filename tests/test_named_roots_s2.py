"""S2a of specs/36-named-roots.lts: `locate`, the ledger search and x-write, the state-unavailable refusal, the absence wording and
new_chain. c3, c10, c15 (ledger half), c16, c17, c20, c21 of [ACC-NR]. A state is begun here by planting its marker, because this slice cannot begin one.

A real caller's shape ([ACC-NR] x-setup): every write and read is a fresh process, with a registry variable and a fake HOME, and moves
are renames. One thing is not a real caller's: the test tree is under the OS temp dir, so FACTLATTICE_LEDGER_EPHEMERAL_ROOTS is set empty, or
no record would be written at all. Names and paths are invented ([INV-NR-NO-DEPLOYMENT-NAMES]).
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE_TREE = str(Path(__file__).resolve().parent.parent)
NODE = "ALPHA-ONE"
DOC = f'---\nlts: "1.1"\ntitle: T\n---\n\n# One [{NODE}]\n\n- summary: s\n'


class World:
    def __init__(self, base: Path):
        self.base = base
        self.home = base / "home"
        self.cfg = base / "cfg"
        for d in (self.home, self.cfg, base / "corpora", base / "elsewhere", base / "drive"):
            d.mkdir(parents=True, exist_ok=True)
        self.registry = self.cfg / "reg.json"

    def env(self, registry=None, home=None) -> dict:
        return {"PATH": os.environ["PATH"], "HOME": str(home or self.home), "PYTHONPATH": ENGINE_TREE,
                "FACTLATTICE_CORPUS_CONFIG": str(registry or self.registry), "FACTLATTICE_LEDGER_EPHEMERAL_ROOTS": "",
                "FACTLATTICE_BACKUP_DIR": str(self.base / "bak")}

    def run(self, *argv, registry=None, home=None):
        p = subprocess.run([sys.executable, "-m", "factlattice_engine", *argv], cwd=self.base / "elsewhere",
                           env=self.env(registry, home), capture_output=True, text=True, timeout=120)
        return p

    def call(self, tool, registry=None, home=None, **args) -> dict:
        allow = ["--allow-write"] if tool.startswith(("lts_add", "lts_repair", "lts_rename", "lts_remove")) else []
        p = self.run("call", tool, *allow, "--json", "--args-json", json.dumps(args), registry=registry, home=home)
        return json.loads(p.stdout)["result"]

    def write(self, path, value="v", key="k", registry=None, home=None) -> dict:
        return self.call("lts_add_property", path=str(path), node_id=NODE, key=key, value=value, registry=registry, home=home)

    def history(self, corpus="demo", registry=None, home=None, limit=100) -> dict:
        return self.call("lts_history", corpus=corpus, node_id=NODE, limit=limit, registry=registry, home=home)

    def machine(self, home=None) -> Path:
        return Path(home or self.home) / ".local" / "state" / "factlattice" / "ledger"

    def set_registry(self, table=None, root=None, extra=None, file=None) -> Path:
        data = {"demo": {"roots": [{"path": str(root)}], "domain_suffix": "x.demo"}} if root is not None else {}
        if table is not None:
            data["named_roots"] = table
        data.update(extra or {})
        f = Path(file or self.registry)
        f.write_text(json.dumps(data), encoding="utf-8")
        return f

    def machine_records(self, home=None) -> int:
        d = self.machine(home)
        return sum(len(p.read_bytes().splitlines()) for p in d.glob("*.jsonl")) if d.exists() else 0


@pytest.fixture()
def w(tmp_path):
    return World(tmp_path)


def doc(folder: Path, name="a.lts") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / name
    f.write_text(DOC, encoding="utf-8")
    return f


def key_of(real) -> str:
    return hashlib.sha256(os.path.realpath(real).encode()).hexdigest()[:32]


def plant_marker(w, name: str, registry) -> None:
    """What the later slice's init-state does. Here so the state branch of `locate`, which is in this slice, can be driven."""
    state = Path(json.loads(Path(registry).read_text())["named_roots"][name]["state"])
    if str(state).startswith("${"):
        d = json.loads(Path(registry).read_text())["named_roots"]
        ref, rel = str(state)[2:].split("}/")
        state = Path(d[ref]["path"]) / rel
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    (state / ".factlattice-state").write_text(json.dumps({"root": name, "v": 1}))


def refusal(r: dict):
    return (r.get("evidence") or {}).get("refusal")


def assert_written(r: dict):
    assert r.get("success") is True and r.get("ledger", {}).get("status") == "ok", r


# ---- no table, no root: nothing has changed ---------------------------------------------------------------------------------------
def test_with_no_table_the_key_is_todays_and_a_record_has_no_new_field(w):
    f = doc(w.base / "corpora" / "c")
    w.set_registry(root=f.parent)
    assert_written(w.write(f))
    ledger = w.machine() / f"{key_of(f)}.jsonl"
    assert ledger.exists()
    rec = json.loads(ledger.read_bytes().splitlines()[0])
    assert "named_root" not in rec
    h = w.history()
    assert h["ledger"] == "present" and h["chain_file_breaks"] == [] and "ledger_left_out" not in h


def test_first_record_says_new_chain_and_the_second_does_not(w):
    f = doc(w.base / "corpora" / "c")
    w.set_registry(root=f.parent)
    first = w.write(f)["ledger"]
    second = w.write(f, value="v2")["ledger"]
    assert first["new_chain"] is True and "unknown, not absent" in first["new_chain_reason"]
    assert "new_chain" not in second and "new_chain_reason" not in second and "several_ledgers" not in second


# ---- [MECH-NR-LEDGER] x-absence ---------------------------------------------------------------------------------------------------
def test_absence_wording_declares_unknown_and_names_no_path(w):
    f = doc(w.base / "corpora" / "c")
    w.set_registry(root=f.parent)
    h = w.history()
    assert h["ledger"] == "none" and h["history_complete"] is False and h["ledger_files"] == []
    assert "no ledger was found in this machine's state folder or through the table" in h["note"]
    assert "unknown, not absent" in h["note"] and "NO ledger" not in h["note"]
    assert str(w.base) not in h["note"] and "/" not in h["note"].replace("and/or", "")


# ---- c3: a same-machine move, no state --------------------------------------------------------------------------------------------
def test_c3_a_rename_keeps_history_and_the_next_write_continues_it_in_one_file(w):
    old_root, new_root = w.base / "corpora" / "old", w.base / "corpora" / "new"
    f = doc(old_root / "c")
    w.set_registry(root=f.parent)
    for i in range(3):
        assert_written(w.write(f, value=f"v{i}"))
    before = w.history()
    old_ledger = w.machine() / f"{key_of(f)}.jsonl"
    old_root.rename(new_root)
    table = {"data": {"path": str(new_root), "was": [str(old_root)]}}
    w.set_registry(table=table, root="${data}/c")
    after = w.history()
    assert after["count"] == before["count"] == 3 and after["ledger_files"] == [str(old_ledger)] and after["chain_file_breaks"] == []
    r = w.write(new_root / "c" / "a.lts", value="v3")
    assert_written(r)
    assert r["ledger"]["seq"] == 4 and r["ledger"]["file"] == str(old_ledger) and "new_chain" not in r["ledger"]
    final = w.history()
    assert final["count"] == 4 and final["reconciled"] is True and final["chain_file_breaks"] == []
    assert [p.name for p in w.machine().glob("*.jsonl")] == [old_ledger.name], "one file, not a fork"
    rec = json.loads(old_ledger.read_bytes().splitlines()[-1])
    assert rec["named_root"] == {"name": "data", "rel": "c/a.lts"} and rec["path_real"] == os.path.realpath(new_root / "c" / "a.lts")


def test_control_with_the_was_spelling_removed_the_history_is_declared_absent(w):
    """The check above fails for the reason it names: take the table's knowledge away and the same move detaches."""
    old_root, new_root = w.base / "corpora" / "old", w.base / "corpora" / "new"
    f = doc(old_root / "c")
    w.set_registry(root=f.parent)
    assert_written(w.write(f))
    old_root.rename(new_root)
    w.set_registry(table={"data": {"path": str(new_root)}}, root="${data}/c")
    h = w.history()
    assert h["ledger"] == "none" and h["count"] == 0


def test_c15_a_local_spelling_stays_out_of_history_and_the_old_absolute_spelling_comes_in(w):
    """The ledger half of c15. `~/docs` on this machine is an unrelated folder with its own file and ledger; this root's old home was another machine's."""
    new_root = w.base / "corpora" / "new"
    g = doc(new_root / "c")
    unrelated = doc(w.home / "docs" / "c")
    w.set_registry(root=unrelated.parent)
    assert_written(w.write(unrelated, value="unrelated"))
    theirs = w.machine() / f"{key_of(unrelated)}.jsonl"
    theirs_bytes = theirs.read_bytes()
    other_machine = "/nosuch/otheruser/docs"
    rec = {"ledger_seq": 1, "ts": "2026-01-01T00:00:00.000+00:00", "prev_sha256": None, "node_id": NODE, "verb": "set_property",
           "file_sha256_after": hashlib.sha256(g.read_bytes()).hexdigest()}
    (w.machine() / (hashlib.sha256(f"{other_machine}/c/a.lts".encode()).hexdigest()[:32] + ".jsonl")).write_text(json.dumps(rec) + "\n")
    table = {"data": {"path": str(new_root), "was": ["~/docs", other_machine]}}
    w.set_registry(table=table, root="${data}/c")
    # not yet marked local: a different file sits at that spelling, so its ledger is left out either way, and said
    h = w.history()
    assert h["count"] == 1 and h["ledger_left_out"] == ["different-file-at-old-location"]
    table["data"]["local"] = ["~/docs"]
    w.set_registry(table=table, root="${data}/c")
    h = w.history()
    assert h["count"] == 1 and "ledger_left_out" not in h and h["reconciled"] is True, h
    r = w.write(g, value="mine")
    assert r["ledger"]["seq"] == 2 and "other" not in r["ledger"]
    assert theirs.read_bytes() == theirs_bytes, "a write to the root's file does not touch that ledger"


def test_a_spelling_that_names_the_same_place_as_a_local_one_is_ignored_too(w):
    old_abs, new_root = w.home / "docs", w.base / "corpora" / "new"
    f = doc(old_abs / "c")
    w.set_registry(root=f.parent)
    assert_written(w.write(f))
    old_abs.rename(new_root)
    w.set_registry(table={"data": {"path": str(new_root), "was": ["~/docs", str(old_abs)], "local": ["~/docs"]}}, root="${data}/c")
    assert w.history()["ledger"] == "none"
    w.set_registry(table={"data": {"path": str(new_root), "was": [str(old_abs)]}}, root="${data}/c")
    assert w.history()["count"] == 1, "control: unmarked, the same spelling finds it"


# ---- c20: several ledgers ---------------------------------------------------------------------------------------------------------
def _two_ledgers(w):
    old_root, new_root = w.base / "corpora" / "old", w.base / "corpora" / "new"
    f = doc(old_root / "c")
    w.set_registry(root=f.parent)
    assert_written(w.write(f, value="old1"))
    old_root.rename(new_root)
    g = new_root / "c" / "a.lts"
    w.set_registry(root=g.parent)                # a registry-only move: this is what one deployment already has
    assert_written(w.write(g, value="new1"))
    w.set_registry(table={"data": {"path": str(new_root), "was": [str(old_root)]}}, root="${data}/c")
    return g, w.machine() / f"{key_of(f)}.jsonl", w.machine() / f"{key_of(g)}.jsonl"


def test_c20_two_ledgers_read_in_ts_order_with_the_break_and_the_write_goes_to_the_current(w):
    g, old_l, cur_l = _two_ledgers(w)
    h = w.history()
    assert h["count"] == 2 and set(h["ledger_files"]) == {str(old_l), str(cur_l)}
    assert [r["delta"] is not None for r in h["records"]] and h["records"][0]["ts"] >= h["records"][1]["ts"]
    assert len(h["chain_file_breaks"]) == 1 and h["chain_file_breaks"][0]["ledger_file_boundary"] is True
    r = w.write(g, value="new2")
    assert r["ledger"]["file"] == str(cur_l) and r["ledger"]["seq"] == 2 and "several_ledgers" not in r["ledger"]


def test_several_counted_with_no_current_start_a_ledger_and_say_so(w):
    a, b, new_root = w.base / "corpora" / "a", w.base / "corpora" / "b", w.base / "corpora" / "n"
    f = doc(a / "c")
    w.set_registry(root=f.parent)
    assert_written(w.write(f))
    a.rename(b)
    g = b / "c" / "a.lts"
    w.set_registry(root=g.parent)
    assert_written(w.write(g))
    b.rename(new_root)
    h = new_root / "c" / "a.lts"
    w.set_registry(table={"data": {"path": str(new_root), "was": [str(a), str(b)]}}, root="${data}/c")
    r = w.write(h)
    assert r["ledger"]["several_ledgers"] == 2 and r["ledger"]["file"] == str(w.machine() / f"{key_of(h)}.jsonl")
    assert r["ledger"]["seq"] == 1 and w.history()["count"] == 3


# ---- the Diamond's F1: a `was` naming the real path of the current location --------------------------------------------------------
def _linked_root_world(w):
    real_root, link = w.base / "corpora" / "realdata", w.base / "corpora" / "linkdata"
    f = doc(real_root / "c")
    link.symlink_to(real_root)
    w.set_registry(root=f.parent)
    for i in range(3):
        assert_written(w.write(f, value=f"v{i}"))
    return f, real_root, link, w.machine() / f"{key_of(f)}.jsonl"


def test_a_was_naming_the_current_real_path_counts_its_ledger_once(w):
    f, real_root, link, ledger_file = _linked_root_world(w)
    w.set_registry(table={"data": {"path": str(link), "was": [str(real_root)]}}, root="${data}/c")
    h = w.history()
    assert h["count"] == 3 and h["ledger_files"] == [str(ledger_file)] and h["chain_file_breaks"] == []
    r = w.write(f, value="next")
    assert r["ledger"]["seq"] == 4 and r["ledger"]["file"] == str(ledger_file)
    assert w.history()["count"] == 4




# ---- the delta read's F14, F15: a ledger belongs to one current file ---------------------------------------------------------------------
def _planted(machine: Path, old: str, n=3) -> Path:
    """A real chain under an old path's key, as a writer there would have left it."""
    machine.mkdir(parents=True, exist_ok=True)
    lp = machine / (hashlib.sha256(old.encode()).hexdigest()[:32] + ".jsonl")
    prev = None
    with open(lp, "wb") as fh:
        for i in range(1, n + 1):
            line = (json.dumps({"ledger_seq": i, "ts": f"2026-01-01T00:00:0{i}.000+00:00", "node_id": NODE, "verb": "set_property", "path_real": old,
                                "prev_sha256": prev, "file_sha256_before": None if i == 1 else str(i - 1), "file_sha256_after": str(i)}) + "\n").encode()
            fh.write(line)
            prev = hashlib.sha256(line).hexdigest()
    return lp


def test_f14_nesting_spellings_give_one_ledger_to_one_file(w):
    old, cur = w.base / "gone" / "old", w.base / "corpora" / "c"
    moved, fresh = doc(cur / "sub"), doc(cur)
    lp = _planted(w.machine(), f"{old}/sub/a.lts")
    w.set_registry(table={"data": {"path": str(cur), "was": [str(old), f"{old}/sub"]}},
                   extra={"m": {"roots": [{"path": str(moved.parent)}], "domain_suffix": "x.m"}, "n": {"roots": [{"path": str(cur)}], "domain_suffix": "x.n"}})
    from factlattice import ledger
    ctx = ledger.context_from_file(str(w.registry))
    a = ledger.locate(fresh, state_dir=w.machine().parent, context=ctx)
    b = ledger.locate(moved, state_dir=w.machine().parent, context=ctx)
    assert (lp in a.read_files) != (lp in b.read_files), "exactly one of them holds it"
    loser = b if lp in a.read_files else a
    assert "old-spelling-belongs-to-another-file" in loser.left_out, "and the other is told"




def test_f14_two_roots_with_a_nesting_spelling_do_not_merge(w):
    from factlattice import ledger
    a, b, old = w.base / "corpora" / "a", w.base / "corpora" / "b", w.base / "gone" / "old"
    fa, fb = doc(a / "sub", "f.lts"), doc(b, "f.lts")
    lp = _planted(w.machine(), f"{old}/sub/f.lts")
    ctx = ledger.TableContext(table={"ra": {"path": str(a), "was": [str(old)], "local": []}, "rb": {"path": str(b), "was": [f"{old}/sub"], "local": []}})
    machine = w.machine().parent
    assert lp in ledger.locate(fb, state_dir=machine, context=ctx).read_files, "the longest spelling"
    assert lp not in ledger.locate(fa, state_dir=machine, context=ctx).read_files


@pytest.mark.parametrize("table", [
    {"r": {"path": "C/c", "was": ["G/a", "G/a/b", "G/a/b/d"]}},
    {"r": {"path": "C/c", "was": ["G/a"]}, "s": {"path": "C/s", "was": ["G/a/b"]}, "t": {"path": "C/c/n", "was": ["G/a/b/d"]}},
    {"r": {"path": "C/c", "was": ["G/a", "G/z"]}, "s": {"path": "C/s", "was": ["G/a/b/d"]}},
    {"r": {"path": "C/c", "was": ["G/a/b"]}, "s": {"path": "C/c/b", "was": ["G/a"]}},
])
def test_no_table_lets_two_current_files_claim_one_ledger(w, table):
    """A sweep, because three reads each found this class one step further on: every file, every ledger, over nesting tables."""
    from factlattice import ledger
    fix = lambda v: v.replace("C/", str(w.base / "corpora") + "/").replace("G/", str(w.base / "gone") + "/")
    resolved = {n: {"path": fix(s["path"]), "was": [fix(x) for x in s.get("was", [])], "local": []} for n, s in table.items()}
    from factlattice import registry
    assert registry.parse_named_roots({n: {k: v for k, v in s.items() if k != "local"} for n, s in resolved.items()}, None)[1] is None, "only a table the grammar accepts"
    rels = ["a.lts", "b/a.lts", "b/d/a.lts", "n/a.lts", "d/a.lts", "b/b/a.lts"]
    files = [doc(Path(s["path"]) / str(Path(r).parent), "a.lts") for s in resolved.values() for r in rels]
    machine = w.base / "machine"
    for s in resolved.values():
        for place in s["was"]:
            for r in rels:
                _planted(machine / "ledger", f"{place}/{r}", n=1)
    ctx = ledger.TableContext(table=resolved)
    claims: dict = {}
    for f in dict.fromkeys(os.path.realpath(x) for x in files):
        for lp in ledger.locate(f, state_dir=machine, context=ctx).read_files:
            claims.setdefault(lp, set()).add(f)
    assert claims, "the sweep has to have found ledgers, or it proved nothing"
    assert {k: v for k, v in claims.items() if len(v) > 1} == {}
    # and none is stranded: a ledger whose owner is a file that exists is found by that file
    stranded = []
    for s_ in resolved.values():
        for place in s_["was"]:
            for r in rels:
                old = f"{place}/{r}"
                owner = ledger.owner_of_old_path(old, resolved)
                if owner and os.path.isfile(owner):
                    lp = machine / "ledger" / (hashlib.sha256(old.encode()).hexdigest()[:32] + ".jsonl")
                    if lp not in ledger.locate(owner, state_dir=machine, context=ctx).read_files:
                        stranded.append(old)
    assert stranded == []










def _case_world(w):
    probe = w.base / "CaseProbe"
    probe.mkdir()
    if not (w.base / "caseprobe").exists():
        pytest.skip("case-sensitive volume")
    root = w.base / "corpora" / "Data"
    f = doc(root / "c")
    state = w.base / "drive" / "state"
    w.set_registry(table={"data": {"path": str(root), "state": str(state)}})
    plant_marker(w, "data", w.registry)
    return f, w.base / "corpora" / "DATA" / "c" / "a.lts", state


def test_a_case_variant_of_a_state_root_is_refused_and_says_how_to_write(w):
    f, variant, state = _case_world(w)
    before = f.read_bytes()
    r = w.write(variant)
    assert refusal(r) == "state-unavailable" and r["evidence"]["detail"] == "case-variant-of-a-state-root"
    assert "as the table spells the root" in r["evidence"]["fix"] and f.read_bytes() == before
    assert w.machine_records() == 0
    assert_written(w.write(f))


def test_writers_under_two_spellings_cannot_fork_the_chain(w):
    """The third read's F-B: half the writers by the table's spelling and half by a case variant. Every variant is refused, so one chain."""
    f, variant, state = _case_world(w)
    procs = [_writer(w, p, t, 6, w.registry) for p, t in ((f, "a"), (f, "b"))]
    outs = [q.communicate(timeout=120) for q in procs]
    assert all(q.returncode == 0 for q in procs), outs
    assert refusal(w.write(variant)) == "state-unavailable"
    ledger_file = next((state / "ledger").glob("*.jsonl"))
    prev = None
    for n, line in enumerate(ledger_file.read_bytes().splitlines(keepends=True), 1):
        rec = json.loads(line)
        assert rec["ledger_seq"] == n and rec["prev_sha256"] == (hashlib.sha256(prev).hexdigest() if prev else None)
        prev = line


def test_a_case_variant_under_a_root_with_no_state_keeps_todays_key(w):
    probe = w.base / "CaseProbe"
    probe.mkdir()
    if not (w.base / "caseprobe").exists():
        pytest.skip("case-sensitive volume")
    root = w.base / "corpora" / "Data"
    f = doc(root / "c")
    w.set_registry(table={"data": {"path": str(root)}})
    variant = w.base / "corpora" / "DATA" / "c" / "a.lts"
    r = w.write(variant)
    assert_written(r)
    assert r["ledger"]["file"].endswith(f"{hashlib.sha256(str(os.path.realpath(variant)).encode()).hexdigest()[:32]}.jsonl") and f.exists()


def test_a_different_folder_differing_only_in_case_is_not_a_variant(w, monkeypatch):
    from factlattice import ledger
    monkeypatch.setattr(os.path, "samefile", lambda a, b: False)
    assert ledger._variant_rest("/x/Data", "/x/DATA/a.lts") is None
    monkeypatch.undo()


# ---- the whole read's findings ------------------------------------------------------------------------------------------------------


def test_f_d_old_location_unknown_still_serves_what_can_be_read(w, monkeypatch):
    from factlattice_engine import config
    from factlattice_engine.tools import LtsEngine
    old, new = w.base / "gone" / "old", w.base / "corpora" / "new"
    f = doc(new / "c")
    machine = w.machine()
    machine.mkdir(parents=True)
    lp = machine / f"{key_of(f)}.jsonl"
    lp.write_text(json.dumps({"ledger_seq": 1, "ts": "2026-01-01T00:00:01.000+00:00", "node_id": NODE, "prev_sha256": None, "file_sha256_after": "x"}) + "\n")
    _planted(machine, f"{old}/c/a.lts")
    w.set_registry(table={"data": {"path": str(new), "was": [str(old)]}}, root="${data}/c")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(w.registry))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(machine.parent))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    real_stat, target = os.stat, f"{old}/c/a.lts"

    def stat(p, *a, **k):
        if str(p) == target:
            raise OSError(5, "Input/output error")
        return real_stat(p, *a, **k)
    eng = LtsEngine(config.build_store())
    monkeypatch.setattr(os, "stat", stat)
    h = eng.history(corpus="demo", node_id=NODE)
    assert h["ledger"] == "present" and h["count"] == 1 and h["ledger_left_out"] == ["old-location-unknown"]
    assert h["write_refused"] == "state-unavailable" and h["state_detail"] == "old-location-unknown" and "could not be read" in h["left_out_note"]


def test_f_e_a_ledger_whose_owner_is_under_an_inner_root_is_found(w):
    from factlattice import ledger
    outer, old = w.base / "corpora" / "c", w.base / "gone" / "old"
    f = doc(outer / "in")
    lp = _planted(w.machine(), f"{old}/in/a.lts")
    ctx = ledger.TableContext(table={"outer": {"path": str(outer), "was": [str(old)], "local": []}, "inner": {"path": str(outer / "in"), "was": [], "local": []}})
    loc = ledger.locate(f, state_dir=w.machine().parent, context=ctx)
    assert loc.root == "inner" and lp in loc.read_files


def test_f_c_the_left_out_note_gives_the_cause_and_the_nesting_warning(w):
    from factlattice import ledger
    assert "may be wrong about a folder that really was a subfolder" in ledger.LEFT_OUT_NOTES["old-spelling-belongs-to-another-file"]
    assert all("/" not in v for v in ledger.LEFT_OUT_NOTES.values())


def test_every_left_out_reason_and_detail_has_its_words():
    from factlattice import ledger
    assert set(ledger.LEFT_OUT_NOTES) == {"different-file-at-old-location", "old-location-unknown", "ledger-unreadable", "old-spelling-belongs-to-another-file"}
    for detail in ledger.DETAIL_FIX:
        assert ledger.fix_for("state-unavailable", detail).startswith("To clear it:")
    assert ledger.fix_for("state-unavailable", "no-marker-for-this-root") == ledger.REFUSAL_FIX["state-unavailable"]






@pytest.mark.parametrize("field", ["path", "state", "was"])
def test_f_g_a_nul_or_control_character_in_the_table_is_malformed_not_a_raw_error(w, field):
    from factlattice import registry
    p, bad = str(w.base / "x"), "/nul\x00here"
    spec = {"path": bad if field == "path" else p, **({"state": bad} if field == "state" else {}), **({"was": [bad]} if field == "was" else {})}
    table, problem = registry.parse_named_roots({"a": spec}, None)
    assert table is None and problem["code"] == "named-roots-malformed"








@pytest.mark.parametrize("form", ["{p}/.", "{p}/q/..", "{p}//"])
def test_f16_spellings_are_compared_in_normal_form(w, form):
    from factlattice import registry
    p = str(w.base / "corpora" / "x")
    table, problem = registry.parse_named_roots({"a": {"path": p}, "b": {"path": str(w.base / "y"), "was": [form.format(p=p)]}}, None)
    assert table is None and problem["detail"] == "was-equals-current-path"
    old = str(w.base / "gone")
    table, problem = registry.parse_named_roots({"a": {"path": p, "was": [old]}, "b": {"path": str(w.base / "y"), "was": [form.format(p=old)]}}, None)
    assert table is None and problem["detail"] == "duplicate-spelling"


def test_f25_a_was_with_two_leading_slashes_finds_its_ledger(w):
    from factlattice import ledger
    cur = w.base / "corpora" / "c"
    f = doc(cur)
    old = str(w.base / "gone")
    lp = _planted(w.machine(), f"{old}/a.lts")
    ctx = ledger.TableContext(table={"d": {"path": str(cur), "was": ["/" + old], "local": []}})
    assert lp in ledger.locate(f, state_dir=w.machine().parent, context=ctx).read_files


def test_f26_two_names_for_one_real_path_are_not_picked_by_the_files_order(w):
    from factlattice import ledger
    real = w.base / "corpora" / "real"
    f = doc(real)
    link = w.base / "corpora" / "link"
    link.symlink_to(real)
    t1 = {"b": {"path": str(link), "was": [], "local": []}, "a": {"path": str(real), "was": [], "local": []}}
    t2 = dict(reversed(list(t1.items())))
    m = w.base / "m"
    assert ledger.locate(f, state_dir=m, context=ledger.TableContext(table=t1)).root == ledger.locate(f, state_dir=m, context=ledger.TableContext(table=t2)).root == "a"






@pytest.mark.parametrize("code,says", [("state-unavailable", "never begun"), ("named-roots-malformed", "a corpus that uses a name is not served")])
def test_f19_f23_the_wording_is_true_of_each_cause(code, says):
    from factlattice import ledger
    assert says in ledger.REFUSAL_FIX[code]


# ---- c21: copy-then-cut ------------------------------------------------------------------------------------------------------------
def test_c21_copy_then_cut_leaves_the_old_ledger_out_then_joins_with_one_break(w):
    old_root, new_root = w.base / "corpora" / "old", w.base / "corpora" / "new"
    f = doc(old_root / "c")
    w.set_registry(root=f.parent)
    assert_written(w.write(f, value="old"))
    shutil.copytree(old_root, new_root)
    g = new_root / "c" / "a.lts"
    w.set_registry(table={"data": {"path": str(new_root), "was": [str(old_root)]}}, root="${data}/c")
    h = w.history()
    assert h["ledger"] == "none" and h["ledger_left_out"] == ["different-file-at-old-location"] and str(w.base) not in h["left_out_note"]
    r = w.write(g, value="new")
    assert r["ledger"]["file"] == str(w.machine() / f"{key_of(g)}.jsonl") and r["ledger"]["new_chain"] is True
    assert w.history()["count"] == 1
    shutil.rmtree(old_root)
    h = w.history()
    assert h["count"] == 2 and len(h["chain_file_breaks"]) == 1 and h["chain_file_breaks"][0]["ledger_file_boundary"] is True
    assert "ledger_left_out" not in h


def test_c21_a_link_left_at_the_old_location_is_the_same_file_and_has_no_break(w):
    old_root, new_root = w.base / "corpora" / "old", w.base / "corpora" / "new"
    f = doc(old_root / "c")
    w.set_registry(root=f.parent)
    assert_written(w.write(f, value="old"))
    old_ledger = w.machine() / f"{key_of(f)}.jsonl"
    old_root.rename(new_root)
    old_root.symlink_to(new_root)
    w.set_registry(table={"data": {"path": str(new_root), "was": [str(old_root)]}}, root="${data}/c")
    h = w.history()
    assert h["count"] == 1 and h["chain_file_breaks"] == [] and "ledger_left_out" not in h
    r = w.write(new_root / "c" / "a.lts", value="new")
    assert r["ledger"]["file"] == str(old_ledger) and r["ledger"]["seq"] == 2


def test_c21_a_hard_link_at_the_old_spelling_is_the_same_file_by_identity(w):
    old_root, new_root = w.base / "corpora" / "old", w.base / "corpora" / "new"
    f = doc(old_root / "c")
    w.set_registry(root=f.parent)
    assert_written(w.write(f, value="old"))
    (new_root / "c").mkdir(parents=True)
    os.link(f, new_root / "c" / "a.lts")
    w.set_registry(table={"data": {"path": str(new_root), "was": [str(old_root)]}}, root="${data}/c")
    h = w.history()
    assert h["count"] == 1 and "ledger_left_out" not in h


def test_c21_a_case_variant_on_a_case_insensitive_volume_has_no_break(w):
    probe = w.base / "CaseProbe"
    probe.mkdir()
    if not (w.base / "caseprobe").exists():
        pytest.skip("this volume is case-sensitive; the variant does not arise here")
    root = w.base / "corpora" / "Data"
    f = doc(root / "c")
    w.set_registry(root=f.parent)
    assert_written(w.write(f, value="old"))
    # the ledger as it would be if written under the other spelling
    variant = w.base / "corpora" / "DATA" / "c" / "a.lts"
    machine = w.machine()
    os.rename(machine / f"{key_of(f)}.jsonl", machine / f"{hashlib.sha256(str(variant).encode()).hexdigest()[:32]}.jsonl")
    w.set_registry(table={"data": {"path": str(root), "was": [str(w.base / "corpora" / "DATA")]}}, root="${data}/c")
    h = w.history()
    assert h["count"] == 1 and h["chain_file_breaks"] == [] and "ledger_left_out" not in h


def test_c21_a_root_moved_back_has_no_break(w):
    a, b = w.base / "corpora" / "a", w.base / "corpora" / "b"
    f = doc(a / "c")
    w.set_registry(root=f.parent)
    assert_written(w.write(f, value="one"))
    a.rename(b)
    w.set_registry(table={"data": {"path": str(b), "was": [str(a)]}}, root="${data}/c")
    assert_written(w.write(b / "c" / "a.lts", value="two"))
    b.rename(a)
    w.set_registry(table={"data": {"path": str(a), "was": [str(b)]}}, root="${data}/c")
    h = w.history()
    assert h["count"] == 2 and h["chain_file_breaks"] == []
    assert w.write(f, value="three")["ledger"]["seq"] == 3


def test_a_stat_that_fails_means_unknown_no_write_starts_and_the_ledger_is_left_out(w, monkeypatch, tmp_path):
    """Through the library, since an I/O error at an old spelling cannot be planted from outside a process."""
    from factlattice import ledger
    old, new = tmp_path / "corpora" / "old", tmp_path / "corpora" / "new"
    (new / "c").mkdir(parents=True)
    g = new / "c" / "a.lts"
    g.write_text(DOC)
    machine = tmp_path / "machine"
    (machine / "ledger").mkdir(parents=True)
    old_file = str(old / "c" / "a.lts")
    (machine / "ledger" / f"{hashlib.sha256(old_file.encode()).hexdigest()[:32]}.jsonl").write_text("{}\n")
    ctx = ledger.TableContext(table={"data": {"path": str(new), "was": [str(old)], "local": []}})
    real_stat = os.stat

    def stat(p, *a, **k):
        if str(p) == old_file:
            raise OSError(5, "Input/output error")
        return real_stat(p, *a, **k)
    monkeypatch.setattr(os, "stat", stat)
    loc = ledger.locate(g, state_dir=machine, context=ctx)
    assert loc.refusal == (ledger.STATE_UNAVAILABLE, "old-location-unknown") and loc.left_out == ("old-location-unknown",)
    assert loc.read_files == ()
    monkeypatch.undo()
    loc = ledger.locate(g, state_dir=machine, context=ctx)
    assert loc.refusal is None and len(loc.read_files) == 1


# ---- nesting and links in the root itself -------------------------------------------------------------------------------------------
def test_nested_roots_take_the_longest_and_a_root_through_a_link_still_keys_on_name_and_rel(w):
    from factlattice import ledger
    outer, inner = w.base / "drive" / "outer", w.base / "drive" / "outer" / "inner"
    f = doc(inner / "c")
    link = w.base / "elsewhere" / "alias"
    link.symlink_to(outer)
    ctx = ledger.TableContext(table={"o": {"path": str(link), "was": [], "local": []},
                                     "i": {"path": str(inner), "was": [], "local": []}})
    loc = ledger.locate(f, state_dir=w.base / "m", context=ctx)
    assert (loc.root, loc.rel) == ("i", "c/a.lts")
    g = doc(outer / "d")
    loc = ledger.locate(g, state_dir=w.base / "m", context=ctx)
    assert (loc.root, loc.rel) == ("o", "d/a.lts"), "the root path passes through a link, and the file's real path does not"


def test_a_file_at_a_was_spelling_that_still_exists_is_under_no_root(w):
    from factlattice import ledger
    old, new = w.base / "drive" / "old", w.base / "drive" / "new"
    f = doc(old)
    new.mkdir()
    loc = ledger.locate(f, state_dir=w.base / "m",
                        context=ledger.TableContext(table={"d": {"path": str(new), "was": [str(old)], "local": []}}))
    assert loc.root is None and loc.write_file.name == f"{key_of(f)}.jsonl"


# ---- c10: absent state --------------------------------------------------------------------------------------------------------------
def _state_world(w, name="data"):
    root = w.base / "corpora" / "root"
    f = doc(root / "c")
    state = w.base / "drive" / "state"
    w.set_registry(table={name: {"path": str(root), "state": str(state)}}, root=f"${{{name}}}/c")
    return f, state


@pytest.mark.parametrize("case", ["missing", "empty-folder", "another-root"])
def test_c10_declared_state_absent_serves_reads_and_refuses_writes_with_the_file_untouched(w, case):
    f, state = _state_world(w)
    if case == "empty-folder":
        state.mkdir()
    if case == "another-root":
        state.mkdir()
        (state / ".factlattice-state").write_text(json.dumps({"root": "other", "v": 1}))
    before = f.read_bytes()
    r = w.write(f)
    assert refusal(r) == "state-unavailable" and r.get("success") is False, r
    assert f.read_bytes() == before and w.machine_records() == 0
    h = w.history()
    assert h["success"] is True and h["ledger"] == "state-unavailable" and h["history_complete"] is False and h["records"] == []
    assert "/" not in h["note"] and str(w.base) not in h["note"]
    node = w.call("lts_node", corpus="demo", node_id=NODE)
    assert node["success"] is True, "the corpus still serves"
    assert not list(state.glob("ledger")) if state.exists() else True


def test_c10_when_the_drive_returns_history_is_whole_and_the_next_write_continues(w):
    f, state = _state_world(w)
    plant_marker(w, "data", w.registry)
    assert_written(w.write(f, value="one"))
    aside = w.base / "drive" / "unmounted"
    state.rename(aside)
    assert refusal(w.write(f, value="blocked")) == "state-unavailable"
    aside.rename(state)
    assert w.history()["count"] == 1
    r = w.write(f, value="two")
    assert_written(r)
    assert r["ledger"]["seq"] == 2 and w.history()["chain_file_breaks"] == [] and w.history()["reconciled"] is True


def test_c10_an_empty_folder_at_the_mount_point_refuses_rather_than_starting_history_there(w):
    f, state = _state_world(w)
    plant_marker(w, "data", w.registry)
    assert_written(w.write(f))
    aside = w.base / "drive" / "unmounted"
    state.rename(aside)
    state.mkdir()                                                   # what an unmounted mount point looks like
    assert refusal(w.write(f, value="x")) == "state-unavailable" and not (state / "ledger").exists()


def test_c10_undeclared_on_a_fresh_machine_serves_declares_and_starts_a_chain(w):
    root = w.base / "corpora" / "root"
    f = doc(root / "c")
    w.set_registry(table={"data": {"path": str(root)}}, root="${data}/c")
    h = w.history()
    assert h["ledger"] == "none" and "unknown, not absent" in h["note"]
    r = w.write(f)
    assert_written(r)
    assert r["ledger"]["new_chain"] is True


def test_every_writer_refuses_state_unavailable_through_its_own_route(w):
    f, _state = _state_world(w)
    before = f.read_bytes()
    results = {
        "add_property": w.call("lts_add_property", path=str(f), node_id=NODE, key="k", value="v"),
        "add_node": w.call("lts_add_node", path=str(f), node_id="BETA-TWO", title="B", properties={"a": "b"}),
        "repair": w.call("lts_repair_property", path=str(f), node_id=NODE, line=9, key="summary"),
        "rename": w.call("lts_rename_property", path=str(f), node_id=NODE, key="summary", new_key="abstract"),
        "remove": w.call("lts_remove_property", path=str(f), node_id=NODE, key="summary"),
    }
    assert {k: refusal(v) or v.get("reason") for k, v in results.items()} == dict.fromkeys(results, "state-unavailable"), results
    assert f.read_bytes() == before


def test_create_under_a_root_with_an_absent_state_is_refused_and_nothing_appears(w):
    f, _state = _state_world(w)
    new = f.parent / "new.lts"
    r = w.call("lts_add_node", path=str(new), node_id="BETA-TWO", title="B", create=True, properties={"a": "b"})
    assert r.get("success") is False and refusal(r) == "state-unavailable", r
    assert not new.exists()


# ---- init-state continuing a legacy chain ------------------------------------------------------------------------------------------


def test_the_state_key_is_the_hash_of_name_and_rel(w):
    f, state = _state_world(w)
    plant_marker(w, "data", w.registry)
    r = w.write(f)
    assert Path(r["ledger"]["file"]) == state / "ledger" / (hashlib.sha256(b"data:c/a.lts").hexdigest()[:32] + ".jsonl")
    assert oct(os.stat(state / "ledger").st_mode & 0o777) == "0o700" and oct(os.stat(r["ledger"]["file"]).st_mode & 0o777) == "0o600"


def test_a_state_written_as_a_reference_lands_under_that_root(w):
    root, drive = w.base / "corpora" / "root", w.base / "drive" / "d"
    drive.mkdir()
    f = doc(root / "c")
    w.set_registry(table={"data": {"path": str(root), "state": "${disk}/hist"}, "disk": {"path": str(drive)}}, root="${data}/c")
    plant_marker(w, "data", w.registry)
    assert (drive / "hist" / ".factlattice-state").exists()
    assert Path(w.write(f)["ledger"]["file"]).parent == drive / "hist" / "ledger"


# ---- c11, c12: a drive that moves, two machines ---------------------------------------------------------------------------------------
def test_c11_a_remount_moves_root_and_state_together_with_no_was_needed(w):
    m1, m2 = w.base / "drive" / "m1", w.base / "drive" / "m2"
    f = doc(m1 / "root" / "c")
    table = lambda m: {"data": {"path": str(m / "root"), "state": str(m / "state")}}
    w.set_registry(table=table(m1), root="${data}/c")
    m1_state = m1 / "state"
    plant_marker(w, "data", w.registry)
    assert_written(w.write(f, value="one"))
    m1.rename(m2)
    w.set_registry(table=table(m2), root="${data}/c")
    h = w.history()
    assert h["count"] == 1 and h["reconciled"] is True
    r = w.write(m2 / "root" / "c" / "a.lts", value="two")
    assert r["ledger"]["seq"] == 2 and Path(r["ledger"]["file"]).parent == m2 / "state" / "ledger"
    assert not m1_state.exists()


def test_c12_two_machines_in_turn_keep_one_chain_reconciled_throughout(w):
    drive = w.base / "drive" / "d"
    f = doc(drive / "root" / "c")
    homes = [w.base / "h1", w.base / "h2"]
    regs = []
    for i, h in enumerate(homes):
        h.mkdir()
        reg = w.cfg / f"m{i}.json"
        w.set_registry(table={"data": {"path": str(drive / "root"), "state": str(drive / "state")}}, root="${data}/c", file=reg)
        regs.append(reg)
    plant_marker(w, "data", regs[0])
    for i in range(6):
        n = i % 2
        r = w.write(f, value=f"v{i}", registry=regs[n], home=homes[n])
        assert r["ledger"]["seq"] == i + 1, r
        h = w.history(registry=regs[n], home=homes[n])
        assert h["reconciled"] is True and h["chain_file_breaks"] == [] and h["count"] == i + 1
    assert w.machine_records(homes[0]) == w.machine_records(homes[1]) == 0
    assert len(list((drive / "state" / "ledger").glob("*.jsonl"))) == 1


# ---- c22: an older state ----------------------------------------------------------------------------------------------------------
def test_c22_a_state_restored_from_before_a_write_passes_the_marker_and_shows_it(w):
    f, state = _state_world(w)
    plant_marker(w, "data", w.registry)
    assert_written(w.write(f, value="one"))
    backup = w.base / "drive" / "backup"
    shutil.copytree(state, backup)
    assert_written(w.write(f, value="two"))
    shutil.rmtree(state)
    backup.rename(state)
    h = w.history()
    assert h["reconciled"] is False
    r = w.write(f, value="three")
    assert_written(r)
    assert any("OUT-OF-BAND" in x for x in r.get("warnings") or []) or r["ledger"].get("out_of_band"), r


# ---- [L8] and what cannot be proved ------------------------------------------------------------------------------------------------
def test_a_state_inside_a_registered_root_refuses_every_write_under_it_and_reads_keep_serving(w):
    root = w.base / "corpora" / "root"
    f = doc(root / "c")
    other = doc(w.base / "corpora" / "other")
    state = other.parent / "state"
    state.mkdir()
    (state / ".factlattice-state").write_text(json.dumps({"root": "data", "v": 1}))
    w.set_registry(table={"data": {"path": str(root), "state": str(state)}}, root="${data}/c",
                   extra={"other": {"roots": [{"path": str(other.parent)}], "domain_suffix": "x.other"}})
    before = f.read_bytes()
    r = w.write(f)
    assert refusal(r) == "named-root-state-inside-corpus" and f.read_bytes() == before
    assert w.call("lts_node", corpus="demo", node_id=NODE)["success"] is True
    h = w.history()
    assert h["ledger"] == "named-root-state-inside-corpus" and "To clear it:" in h["note"] and "not reachable" not in h["note"]
    assert not (state / "ledger").exists()


def test_a_state_inside_a_case_variant_of_a_root_is_refused_too(w):
    probe = w.base / "CaseProbe"
    probe.mkdir()
    if not (w.base / "caseprobe").exists():
        pytest.skip("case-sensitive volume")
    root = w.base / "corpora" / "Root"
    f = doc(root / "c")
    state = w.base / "corpora" / "ROOT" / "state"
    state.mkdir()
    (state / ".factlattice-state").write_text(json.dumps({"root": "data", "v": 1}))
    w.set_registry(table={"data": {"path": str(root), "state": str(state)}}, root=str(root / "c"),
                   extra={"whole": {"roots": [{"path": str(root)}], "domain_suffix": "x.whole"}})
    assert refusal(w.write(f)) == "named-root-state-inside-corpus"


def test_a_state_inside_the_files_own_folder_is_refused(w):
    root = w.base / "corpora" / "root"
    f = doc(root)
    state = root / "state"
    state.mkdir()
    (state / ".factlattice-state").write_text(json.dumps({"root": "data", "v": 1}))
    w.set_registry(table={"data": {"path": str(root), "state": str(state)}})
    assert refusal(w.write(f)) == "named-root-state-inside-corpus"






def test_a_state_failing_both_is_reported_as_inside_the_corpus(w):
    f, state = _state_world(w)
    other = w.base / "corpora" / "other"
    state2 = other / "st"
    state2.mkdir(parents=True)
    (state2 / ".factlattice-state").write_text(json.dumps({"root": "data", "v": 1}))
    d = json.loads(w.registry.read_text())
    d["named_roots"]["data"]["state"] = str(state2)
    d["other"] = {"roots": [{"path": str(other)}], "domain_suffix": "x.o"}
    d["broken"] = {"roots": [{"pth": "/x"}]}
    w.registry.write_text(json.dumps(d))
    assert refusal(w.write(f)) == "named-root-state-inside-corpus"


def test_a_failed_entry_does_not_touch_a_write_under_a_root_with_no_state(w):
    root = w.base / "corpora" / "root"
    f = doc(root / "c")
    w.set_registry(table={"data": {"path": str(root)}}, root="${data}/c", extra={"broken": {"roots": [{"pth": "/x"}]}})
    assert_written(w.write(f))


@pytest.mark.parametrize("registry_text", ['{"named_roots": "nope", "demo": {"roots": [{"path": "%s"}], "domain_suffix": "x.demo"}}'])
def test_a_malformed_table_refuses_every_durable_write_and_keeps_serving_reads(w, registry_text):
    f = doc(w.base / "corpora" / "c")
    w.registry.write_text(registry_text % f.parent)
    before = f.read_bytes()
    r = w.write(f)
    assert refusal(r) == "named-roots-malformed" and r["evidence"]["detail"] == "table-not-object" and f.read_bytes() == before
    assert w.call("lts_node", corpus="demo", node_id=NODE)["success"] is True
    h = w.history()
    assert h["ledger"] == "none" and h["write_refused"] == "named-roots-malformed" and h["state_detail"] == "table-not-object"


def test_an_unreadable_registry_keeps_the_older_rule_and_says_new_chain(w, monkeypatch):
    """The older `registry_unavailable` rule: the write lands under today's key. A moved file starts a ledger, says so, and joins with one break when the table returns."""
    from factlattice import ledger
    from factlattice.authoring import edit
    old, new = w.base / "corpora" / "old", w.base / "corpora" / "new"
    f = doc(old / "c")
    w.set_registry(root=f.parent)
    assert_written(w.write(f))
    old.rename(new)
    g = new / "c" / "a.lts"
    good = w.set_registry(table={"data": {"path": str(new), "was": [str(old)]}}, root="${data}/c", file=w.cfg / "good.json")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(w.base / "gone.json"))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    monkeypatch.setenv("HOME", str(w.home))
    monkeypatch.delenv("FACTLATTICE_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)  # this test pins the HOME-relative default: a caller's XDG_STATE_HOME must not move it
    assert ledger.table_context().problem is None
    edit.set_property(g, NODE, "k2", "v", proposer="test")
    led = edit.last_ledger()
    assert led["status"] == "ok" and led["new_chain"] is True and led["file"].endswith(f"{key_of(g)}.jsonl")
    h = w.history(registry=good)
    assert h["count"] == 2 and len(h["chain_file_breaks"]) == 1 and h["chain_file_breaks"][0]["ledger_file_boundary"] is True


def test_a_scratch_copy_under_the_temp_dir_is_not_refused_by_a_malformed_table(w, monkeypatch):
    """[LEDGER-NO-EPHEMERAL-ROOTS]: it has no record to protect."""
    from factlattice.authoring import edit
    w.registry.write_text('{"named_roots": "nope"}')
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(w.registry))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", str(w.base))     # this suite moves the temp dir, so name it
    f = doc(w.base / "corpora" / "c")
    edit.set_property(f, NODE, "k", "v", proposer="test")
    assert edit.last_ledger()["reason"] == "ledger-skipped-ephemeral"


def test_a_machine_state_inside_a_root_the_engine_does_not_serve_is_refused(w):
    """The gap SDLC named: [L8] read served roots only. A corpus whose build failed on a FIFO is not served, and its root still counts."""
    root = w.base / "corpora" / "root"
    f = doc(root / "c")
    unserved = w.home / ".local" / "state"
    unserved.mkdir(parents=True)
    os.mkfifo(unserved / "pipe.lts")
    w.set_registry(root=f.parent, extra={"unserved": {"roots": [{"path": str(unserved)}], "domain_suffix": "x.un"}})
    r = w.write(f)
    assert r["ledger"]["status"] == "failed" and "INSIDE the registered corpus root" in r["ledger"]["live_detail"], r
    assert any("WRITTEN, BUT NOT LEDGERED" in x for x in r["warnings"])


def test_a_state_under_the_temp_dir_is_treated_like_a_root_there_and_says_so(w, monkeypatch, tmp_path):
    from factlattice import ledger
    from factlattice.authoring import edit
    root = w.base / "corpora" / "root"
    f = doc(root / "c")
    state = w.base / "drive" / "st"
    w.set_registry(table={"data": {"path": str(root), "state": str(state)}}, root="${data}/c")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(w.registry))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", str(state))
    assert ledger.locate(f).ephemeral_state is True
    edit.set_property(f, NODE, "k", "v", proposer="test")
    assert edit.last_ledger()["reason"] == "ledger-skipped-ephemeral" and not state.exists()


# ---- an external drive unplugged or asleep ([GRAM-NR-TABLE] x-refusals) --------------------------------------------------------------
def _volume_world(w):
    """A corpus on the internal disk, and on one external volume a corpus with a state, a corpus with none, a profile and an old spelling."""
    volume, inner = w.base / "volumes" / "ext", w.base / "corpora" / "inner"
    state = volume / "state"
    onvol, plain = doc(volume / "zb" / "c"), doc(volume / "plain" / "c")
    local = doc(inner / "c")
    w.set_registry(table={"zb": {"path": str(volume / "zb"), "state": str(state), "was": [str(volume / "older")]},
                          "flat": {"path": str(volume / "plain")},
                          "inner": {"path": str(inner), "was": [str(volume / "movedoff")]}},
                   root="${zb}/c",
                   extra={"plain": {"roots": [{"path": "${flat}/c"}], "domain_suffix": "x.plain"},
                          "local": {"roots": [{"path": "${inner}/c"}], "domain_suffix": "x.local", "profile": str(volume / "profile.yaml")}})
    plant_marker(w, "zb", w.registry)
    return volume, onvol, plain, local


def test_an_unplugged_volume_stops_only_its_own_stateful_root(w):
    from factlattice import ledger
    volume, onvol, plain, local = _volume_world(w)
    assert_written(w.write(onvol, value="before"))
    assert_written(w.write(local, value="before"))
    aside = w.base / "volumes" / "unplugged"
    volume.rename(aside)
    ctx = ledger.context_from_file(str(w.registry))
    assert ctx.problem is None and ctx.unprovable is False, "an absent volume is neither a malformed table nor a failed entry"
    r = w.write(local, value="during")
    assert_written(r)
    assert r["ledger"]["seq"] == 2, "the internal disk goes on writing, in its own chain"
    assert w.history("local")["count"] == 2 and w.history("local")["ledger"] == "present"
    assert w.call("lts_node", corpus="local", node_id=NODE)["success"] is True
    r = w.write(onvol, value="during")
    assert r.get("success") is not True and refusal(r) == "state-unavailable", r
    assert "reconnect or wake the drive" in r["evidence"]["fix"] and "Only files under this root" in r["evidence"]["fix"]
    volume.parent.joinpath("unplugged").rename(volume)
    h = w.history()
    assert h["count"] == 1 and h["reconciled"] is True, "whole when it returns"
    assert w.write(onvol, value="after")["ledger"]["seq"] == 2


def test_a_state_on_the_volume_with_its_root_on_the_internal_disk_stops_only_that_root(w):
    inner, other = w.base / "corpora" / "inner", w.base / "corpora" / "other"
    volume = w.base / "volumes" / "ext"
    volume.mkdir(parents=True)
    f, g = doc(inner / "c"), doc(other / "c")
    w.set_registry(table={"data": {"path": str(inner), "state": str(volume / "state")}, "other": {"path": str(other)}}, root="${data}/c",
                   extra={"o": {"roots": [{"path": "${other}/c"}], "domain_suffix": "x.o"}})
    plant_marker(w, "data", w.registry)
    volume.rename(w.base / "volumes" / "unplugged")
    assert refusal(w.write(f)) == "state-unavailable" and f.read_text() == DOC
    assert_written(w.write(g))
    assert w.call("lts_node", corpus="demo", node_id=NODE)["success"] is True, "the root is on the internal disk and still serves"


def test_a_control_shows_a_failed_entry_differs_from_an_absent_volume(w):
    """The absent-volume test above would pass if `unprovable` were never set. It is set for a real failure, in the same world."""
    from factlattice import ledger
    volume, onvol, plain, local = _volume_world(w)
    d = json.loads(w.registry.read_text())
    d["broken"] = {"roots": [{"pth": "/x"}]}
    w.registry.write_text(json.dumps(d))
    assert ledger.context_from_file(str(w.registry)).unprovable is True
    assert refusal(w.write(onvol)) == "named-root-state-unprovable"
    assert_written(w.write(local))


@pytest.mark.parametrize("code", ["state-unavailable", "named-root-state-inside-corpus", "named-root-state-unprovable", "named-roots-malformed"])
def test_every_refusal_says_what_clears_it_and_names_nothing(code):
    from factlattice import ledger
    fix = ledger.REFUSAL_FIX[code]
    assert fix.startswith("To clear it:") and "affected" in fix and "/Users" not in fix
    if code in ledger.CONFIGURATION_CODES:
        assert "restart every engine server" in fix, "a server keeps the registry it booted from, so a fix alone does not reach it"
    else:
        assert "no restart is needed" in fix


def test_the_writer_and_history_use_the_sentence_of_the_code_they_report(w):
    """A configuration code and a drive code get different words, so swapping them must go red."""
    from factlattice import ledger
    f, _state = _state_world(w)
    r = w.write(f)
    assert r["evidence"]["fix"] == ledger.REFUSAL_FIX["state-unavailable"] and "no restart is needed" in r["reason"]
    h = w.history()
    assert "not reachable now" in h["note"] and "fault in the registry" not in h["note"] and "no restart is needed" in h["note"]
    w.registry.write_text('{"named_roots": "nope", "demo": {"roots": [{"path": "%s"}], "domain_suffix": "x.demo"}}' % f.parent)
    r = w.write(f)
    assert r["evidence"]["fix"] == ledger.REFUSAL_FIX["named-roots-malformed"] and "restart every engine server" in r["reason"]
    assert "not reachable" not in r["reason"]


def test_the_refusal_a_writer_returns_carries_the_fix_on_every_route(w):
    f, _state = _state_world(w)
    for r in (w.write(f), w.call("lts_add_node", path=str(f), node_id="BETA-TWO", title="B", properties={"a": "b"})):
        assert r["evidence"]["fix"].startswith("To clear it:"), r


# ---- private roots: no path, no name ----------------------------------------------------------------------------------------------
def test_a_private_root_is_not_named_in_a_note(w):
    root = w.base / "corpora" / "root"
    doc(root / "c")
    state = w.base / "drive" / "st"
    w.set_registry(table={"secretname": {"path": str(root), "state": str(state), "private": True}}, root="${secretname}/c")
    h = w.history()
    assert "secretname" not in h["note"] and h["ledger"] == "state-unavailable"
    plant_marker(w, "secretname", w.registry)
    h = w.history()
    assert h["ledger"] == "none" and "secretname" not in h["note"] and "this root's state location" in h["note"]


def test_a_root_that_is_not_private_is_named_where_absence_is_declared(w):
    _f, _state = _state_world(w)
    plant_marker(w, "data", w.registry)
    assert "the state location of root data" in w.history()["note"]


# ---- [INV-NR-ONE-TABLE]: one reading per write, one table per server ---------------------------------------------------------------
def test_a_pinned_server_goes_by_the_table_it_booted_from(w, monkeypatch):
    from factlattice import ledger
    root = w.base / "corpora" / "root"
    f = doc(root / "c")
    w.set_registry(table={"data": {"path": str(root)}}, root="${data}/c")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(w.registry))
    ledger.pin_table(ledger.context_from_file(str(w.registry)))
    w.registry.write_text("not json")
    assert ledger.locate(f).refusal is None and ledger.locate(f).root == "data", "the file changed; the server did not"
    ledger.unpin_table()
    assert ledger.locate(f).root is None, "a short-lived process reads it each time: no table now"


def test_one_write_acts_on_one_reading_of_the_table(w, monkeypatch):
    """Two readings could differ: a registry replaced between the check and the append would move a record the check never saw."""
    from factlattice import ledger
    from factlattice.authoring import edit
    root = w.base / "corpora" / "root"
    f = doc(root / "c")
    w.set_registry(table={"data": {"path": str(root)}}, root="${data}/c")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(w.registry))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(w.base / "m"))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    real, n = ledger.table_context, []

    def counting():
        n.append(1)
        return real()
    monkeypatch.setattr(ledger, "table_context", counting)
    edit.set_property(f, NODE, "k", "v", proposer="test")
    assert edit.last_ledger()["status"] == "ok" and len(n) == 1


def test_a_drive_gone_between_the_check_and_the_append_starts_nothing_and_is_loud(w, monkeypatch):
    from factlattice import ledger
    f, state = _state_world(w)
    plant_marker(w, "data", w.registry)
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(w.registry))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(w.base / "m"))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    loc = ledger.locate(f)
    assert loc.refusal is None
    state.rename(w.base / "drive" / "gone")
    state.mkdir()                                         # a bare mount point
    with pytest.raises(ledger.LedgerError, match="state-went-away"):
        ledger.append(f, {"verb": "set_property"}, location=loc)
    assert os.listdir(state) == []


def test_a_state_ledger_that_cannot_be_read_refuses_rather_than_reading_as_none(w, monkeypatch):
    from factlattice import ledger
    f, state = _state_world(w)
    plant_marker(w, "data", w.registry)
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(w.registry))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    real_stat = os.stat

    def stat(p, *a, **k):
        if str(p).endswith(".jsonl") and str(state) in str(p):
            raise OSError(5, "Input/output error")
        return real_stat(p, *a, **k)
    monkeypatch.setattr(os, "stat", stat)
    assert ledger.locate(f).refusal == (ledger.STATE_UNAVAILABLE, "state-ledger-unreadable")


def test_boot_pins_the_table_and_registry_error_mode_pins_no_table(w, monkeypatch):
    from factlattice import ledger
    from factlattice_engine import server
    root = w.base / "corpora" / "root"
    doc(root / "c")
    w.set_registry(table={"data": {"path": str(root)}}, root="${data}/c")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(w.registry))
    server.build_tools(watch=False)
    assert ledger._PINNED is not None and "data" in ledger._PINNED.table
    ledger.unpin_table()
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(w.base / "gone.json"))
    server.boot_engine(watch=False, retry_delay_s=0, sleep=lambda _s: None)
    assert ledger._PINNED.table is None and ledger._PINNED.problem is None


# ---- c16, c17: a registry change under writers -------------------------------------------------------------------------------------
WRITER = """
import sys
from factlattice.authoring import edit
path, tag, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
done = refused = 0
for i in range(n):
    try:
        edit.set_property(path, "ALPHA-ONE", f"{tag}{i}", "v", proposer=tag)
        led = edit.last_ledger()
        assert led and led.get("status") == "ok", led
        done += 1
    except edit.EditRefused as e:
        if (e.evidence or {}).get("refusal") != "lock_busy":
            raise
        refused += 1
print(done, refused)
"""


def _writer(w, path, tag, n, registry):
    return subprocess.Popen([sys.executable, "-c", WRITER, str(path), tag, str(n)], cwd=w.base / "elsewhere",
                            env={**w.env(registry), "FACTLATTICE_WRITE_LOCK_TIMEOUT": "30"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _one_chain(w, f, expect_files=1):
    files = list(w.machine().glob("*.jsonl"))
    assert len(files) == expect_files, files
    lines = files[0].read_bytes().splitlines(keepends=True)
    prev = None
    for n, line in enumerate(lines, 1):
        rec = json.loads(line)
        assert rec["ledger_seq"] == n and rec["prev_sha256"] == (hashlib.sha256(prev).hexdigest() if prev else None)
        prev = line
    assert json.loads(lines[-1])["file_sha256_after"] == hashlib.sha256(f.read_bytes()).hexdigest(), "byte-consistent with its ledger"
    return lines


@pytest.mark.parametrize("seeded", [True, False], ids=["file-with-a-ledger", "file-with-none"])
def test_c16_mixed_fleet_old_and_new_registry_write_one_chain_in_turn_and_together(w, seeded):
    root = w.base / "corpora" / "root"
    f = doc(root / "c")
    old = w.set_registry(root=f.parent, file=w.cfg / "old.json")
    new = w.set_registry(table={"data": {"path": str(root)}}, root="${data}/c", file=w.cfg / "new.json")
    w.registry.write_text(old.read_text())
    if seeded:
        assert_written(w.write(f, value="seed", key="seed", registry=old))
    for i, reg in enumerate((new, old, new, old)):              # in turn, each going first in turn
        assert_written(w.write(f, value="t", key=f"turn{i}", registry=reg))
    procs = [_writer(w, f, "o", 25, old), _writer(w, f, "n", 25, new)]
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    done = sum(int(o[0].split()[0]) for o in outs)
    assert done == 50, outs
    lines = _one_chain(w, f)
    assert len(lines) == 50 + 4 + (1 if seeded else 0)
    assert w.history(registry=new)["chain_file_breaks"] == []


def test_c16_gates_stay_intact_on_both_sides(w):
    root = w.base / "corpora" / "root"
    f = doc(root / "c")
    old = w.set_registry(root=f.parent, file=w.cfg / "old.json")
    new = w.set_registry(table={"data": {"path": str(root)}}, root="${data}/c", file=w.cfg / "new.json")
    for reg in (old, new):
        r = w.call("lts_add_property", path=str(f), node_id=NODE, key="1digit", value="v", registry=reg)
        assert r.get("success") is False, r
    assert f.read_text() == DOC and w.machine_records() == 0


def test_c17_a_registry_replaced_under_a_short_lived_writer_loses_nothing(w):
    root = w.base / "corpora" / "root"
    f = doc(root / "c")
    old = w.set_registry(root=f.parent, file=w.cfg / "old.json")
    new = w.set_registry(table={"data": {"path": str(root)}}, root="${data}/c", file=w.cfg / "new.json")
    w.registry.write_text(old.read_text())
    done = 0
    for i in range(16):
        if i == 8:
            tmp = w.cfg / "swap.tmp"
            tmp.write_text(new.read_text())
            os.replace(tmp, w.registry)
        assert_written(w.write(f, value="v", key=f"k{i}"))
        done += 1
    lines = _one_chain(w, f)
    assert len(lines) == done == 16
    assert "named_root" not in json.loads(lines[7]) and "named_root" in json.loads(lines[8])


def test_c17_concurrent_library_writers_across_the_swap_land_whole(w):
    root = w.base / "corpora" / "root"
    f = doc(root / "c")
    old = w.set_registry(root=f.parent, file=w.cfg / "old.json")
    new = w.set_registry(table={"data": {"path": str(root)}}, root="${data}/c", file=w.cfg / "new.json")
    w.registry.write_text(old.read_text())
    procs = [_writer(w, f, t, 40, w.registry) for t in ("a", "b")]
    tmp = w.cfg / "swap.tmp"
    tmp.write_text(new.read_text())
    os.replace(tmp, w.registry)
    outs = [p.communicate(timeout=180) for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    assert len(_one_chain(w, f)) == sum(int(o[0].split()[0]) for o in outs) == 80


# ---- [LEDGER-LOCATE-ONE-FUNCTION] ---------------------------------------------------------------------------------------------------
LEDGER_PRIVATE = {"_machine_key", "_named_key", "_machine_base", "_legacy"}
LEDGER_FOLDERS = {"ledger", "ledger-blobs"}


def _bypasses(tree: ast.AST) -> list:
    """A private key helper used outside the ledger module, or a path built on the ledger's folder names (`x / "ledger"`, `join(x, "ledger")`).
    A lexical tripwire: it cannot see a path assembled from a name held in a variable, so `locate` staying the only route rests on this plus
    review. Its control is the planted bypasses in the next test."""
    found = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr in LEDGER_PRIVATE:
            found.append(n.attr)
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
            found += [c.value for c in (n.left, n.right) if isinstance(c, ast.Constant) and c.value in LEDGER_FOLDERS]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "join":
            found += [c.value for c in n.args if isinstance(c, ast.Constant) and c.value in LEDGER_FOLDERS]
    return found


def test_no_module_outside_the_ledger_builds_a_ledger_path():
    bad = {}
    seen = 0
    for pkg in ("factlattice", "factlattice_engine"):
        for p in (Path(ENGINE_TREE) / pkg).rglob("*.py"):
            if p.name == "ledger.py":
                continue
            seen += 1
            hits = _bypasses(ast.parse(p.read_text(encoding="utf-8")))
            if hits:
                bad[str(p)] = hits
    assert seen > 50 and not bad, bad


def test_the_bypass_scan_goes_red_on_planted_bypasses():
    planted = ['p = base / "ledger" / hashlib.sha256(real.encode()).hexdigest()[:32]',
               'p = os.path.join(base, "ledger-blobs", sha)',
               'from factlattice import ledger\nx = ledger._machine_key(real)',
               'k = ledger._named_key("a", "b")']
    for src in planted:
        assert _bypasses(ast.parse(src)), src
    assert not _bypasses(ast.parse('out = {"ledger": 1}\nx = hashlib.sha256(b"")')), "a receipt key beside a hash is not a bypass"


def test_a_read_and_a_write_and_the_path_helper_agree_through_locate(w, monkeypatch):
    from factlattice import ledger
    root = w.base / "corpora" / "root"
    f = doc(root / "c")
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(w.base / "m"))
    ctx_reg = w.set_registry(table={"data": {"path": str(root)}}, root="${data}/c")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(ctx_reg))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    out = ledger.append(f, {"verb": "set_property", "file_sha256_before": None, "file_sha256_after": "x"})
    assert Path(out["file"]) == ledger.ledger_path(f) == ledger.locate(f).write_file
    assert ledger.read_lines(f) == Path(out["file"]).read_bytes().splitlines(keepends=True)


def test_append_refuses_when_locate_refuses_so_a_bare_call_cannot_fork(w, monkeypatch):
    from factlattice import ledger
    f, state = _state_world(w)
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(w.registry))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(w.base / "m"))
    with pytest.raises(ledger.LedgerError, match="state-unavailable"):
        ledger.append(f, {"verb": "set_property"})
    assert not (w.base / "m" / "ledger").exists()


# ---- stable codes -----------------------------------------------------------------------------------------------------------------
def test_the_codes_are_stable():
    from factlattice import ledger
    from factlattice.authoring import edit
    assert ledger.LOCATE_CODES == ("state-unavailable", "named-root-state-inside-corpus", "named-root-state-unprovable",
                                   "named-roots-malformed")
    assert set(ledger.LOCATE_CODES) <= set(edit.WRITER_REFUSAL_CODES) and set(ledger.LOCATE_CODES) <= edit.KNOWN_CREATE_CODES
    assert "named_root" in ledger.FIELDS




# ---- what the split must not leave untested: the state branch stays in this slice ---------------------------------------------------------
def test_a_planted_state_continues_the_legacy_chain_in_the_state(w):
    root = w.base / "corpora" / "root"
    f = doc(root / "c")
    w.set_registry(root=f.parent)
    assert_written(w.write(f, value="one"))
    assert_written(w.write(f, value="two"))
    legacy = w.machine() / f"{key_of(f)}.jsonl"
    last = legacy.read_bytes().splitlines(keepends=True)[-1]
    state = w.base / "drive" / "state"
    w.set_registry(table={"data": {"path": str(root), "state": str(state)}}, root="${data}/c")
    plant_marker(w, "data", w.registry)
    r = w.write(f, value="three")
    assert_written(r)
    assert r["ledger"]["seq"] == 3 and Path(r["ledger"]["file"]).parent == state / "ledger" and "new_chain" not in r["ledger"]
    rec = json.loads(Path(r["ledger"]["file"]).read_bytes())
    assert rec["prev_sha256"] == hashlib.sha256(last).hexdigest() and rec["named_root"] == {"name": "data", "rel": "c/a.lts"}
    h = w.history()
    assert h["count"] == 3 and h["chain_file_breaks"] == [] and h["reconciled"] is True and len(h["ledger_files"]) == 2


def test_one_record_in_two_ledgers_reads_as_one():
    from factlattice import ledger
    a, b = b'{"ts": "1", "ledger_seq": 1}\n', b'{"ts": "2", "ledger_seq": 2}\n'
    assert ledger.merge_lines([(Path("x"), [a, b]), (Path("y"), [a])]) == [a, b]


@pytest.mark.parametrize("bad", ["roots-key-misspelt", "unknown-name"])
def test_a_failed_entry_is_not_consulted_so_a_state_root_write_is_refused(w, bad):
    """The S1 class: a misspelt key or an unknown name means that entry's root is unknown, so the state cannot be proved outside it."""
    f, state = _state_world(w)
    plant_marker(w, "data", w.registry)
    d = json.loads(w.registry.read_text())
    d["broken"] = ({"roots": [{"pth": str(state.parent)}]} if bad == "roots-key-misspelt" else {"roots": [{"path": "${nosuch}/x"}]})
    w.registry.write_text(json.dumps(d))
    r = w.write(f)
    assert refusal(r) == "named-root-state-unprovable" and r["evidence"]["detail"] == "registered-roots-unknown", r
    h = w.history()
    assert h["ledger"] == "named-root-state-unprovable" and "restart every engine server" in h["note"]


def test_f_a_old_location_unknown_says_its_own_cause_and_not_a_state_location(w, monkeypatch):
    from factlattice import ledger
    from factlattice.authoring import edit
    old, new = w.base / "gone" / "old", w.base / "corpora" / "new"
    f = doc(new / "c")
    w.machine().mkdir(parents=True)
    _planted(w.machine(), f"{old}/c/a.lts")
    w.set_registry(table={"data": {"path": str(new), "was": [str(old)]}}, root="${data}/c")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(w.registry))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(w.machine().parent))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    real_stat, target = os.stat, f"{old}/c/a.lts"

    def stat(p, *a, **k):
        if str(p) == target:
            raise OSError(13, "Permission denied")
        return real_stat(p, *a, **k)
    monkeypatch.setattr(os, "stat", stat)
    with pytest.raises(edit.EditRefused) as e:
        edit.set_property(f, NODE, "k", "v", proposer="t")
    assert e.value.evidence["detail"] == "old-location-unknown" and e.value.evidence["fix"] == ledger.DETAIL_FIX["old-location-unknown"]


def test_a_root_that_cannot_be_resolved_does_not_stop_boot_or_a_write(w, monkeypatch):
    """A NUL in a registry root, no table: today's engine boots and writes. Reading declared roots must not raise."""
    from factlattice import ledger
    from factlattice.authoring import edit
    from factlattice_engine import server
    f = doc(w.base / "corpora" / "c")
    w.registry.write_text(json.dumps({"demo": {"roots": [{"path": str(f.parent)}], "domain_suffix": "x.d"},
                                      "odd": {"roots": [{"path": "/nul\u0000here"}], "domain_suffix": "x.o"}}))
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(w.registry))
    monkeypatch.setenv("FACTLATTICE_STATE_DIR", str(w.base / "m"))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    ctx = ledger.table_context()
    assert ctx.unprovable is True and ctx.problem is None
    edit.set_property(f, NODE, "k", "v", proposer="t")
    assert edit.last_ledger()["status"] == "ok"
    server.boot_engine(watch=False, retry_delay_s=0, sleep=lambda _s: None)


def test_star_import_of_the_ledger_works():
    ns: dict = {}
    exec("from factlattice.ledger import *", ns)
    assert "locate" in ns


def test_a_write_beside_a_second_chain_says_so(w):
    """A writer that did not know the table started a second chain. The next write continues the current one, and its receipt names the split."""
    g, old_l, cur_l = _two_ledgers(w)
    r = w.write(g, value="again")
    assert r["ledger"]["other_ledgers"] == 1 and r["ledger"]["file"] == str(cur_l)


def test_a_malformed_table_blanks_no_history_it_can_read(w):
    f = doc(w.base / "corpora" / "c")
    w.set_registry(root=f.parent)
    assert_written(w.write(f))
    w.registry.write_text('{"named_roots": "nope", "demo": {"roots": [{"path": "%s"}], "domain_suffix": "x.demo"}}' % f.parent)
    h = w.history()
    assert h["ledger"] == "present" and h["count"] == 1 and h["write_refused"] == "named-roots-malformed" and "To clear it:" in h["write_refused_fix"]
    assert refusal(w.write(f, value="x")) == "named-roots-malformed"


def test_the_whole_document_writer_refuses_too(w, monkeypatch):
    from factlattice.authoring import edit
    f, _state = _state_world(w)
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(w.registry))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    monkeypatch.setenv("HOME", str(w.home))
    before = f.read_bytes()
    with pytest.raises(edit.EditRefused) as e:
        edit.write_whole_file(f, "# New [N-1]\n")
    assert e.value.evidence["refusal"] == "state-unavailable" and f.read_bytes() == before


@pytest.mark.parametrize("bad", ["/x/a\ud800b", "/nul\u0000here"])
@pytest.mark.parametrize("field", ["path", "was", "state"])
def test_unusable_text_in_any_table_field_is_malformed_not_a_raw_error(w, bad, field):
    f = doc(w.base / "corpora" / "c")
    p = str(w.base / "corpora" / "other")
    spec = {"path": bad if field == "path" else p, **({"was": [bad]} if field == "was" else {}), **({"state": bad} if field == "state" else {})}
    w.registry.write_text(json.dumps({"named_roots": {"stray": spec}, "demo": {"roots": [{"path": str(f.parent)}], "domain_suffix": "x.d"}}))
    r = w.write(f)
    assert refusal(r) == "named-roots-malformed" and f.read_text() == DOC, r
    h = w.history()
    assert h["success"] is True and h["write_refused"] == "named-roots-malformed"
    assert w.run("registry", "resolved", "--file", str(w.registry)).returncode == 1, "and the fix text's command shows the fault"


def test_a_malformed_table_marks_history_partial_and_a_healthy_one_does_not(w):
    g, old_l, cur_l = _two_ledgers(w)
    assert w.history()["count"] == 2 and "history_partial" not in w.history()
    d = json.loads(w.registry.read_text())
    d["named_roots"] = "nope"
    d["demo"]["roots"] = [{"path": str(g.parent)}]
    w.registry.write_text(json.dumps(d))
    h = w.history()
    assert h["count"] == 1 and h["history_partial"] is True and "old spellings were not searched" in h["partial_note"]


def test_with_only_an_old_ledger_and_a_malformed_table_the_note_says_the_table_was_not_searched(w):
    old, new = w.base / "corpora" / "old", w.base / "corpora" / "new"
    f = doc(old / "c")
    w.set_registry(root=f.parent)
    assert_written(w.write(f))
    old.rename(new)
    w.registry.write_text('{"named_roots": "nope", "demo": {"roots": [{"path": "%s"}], "domain_suffix": "x.demo"}}' % (new / "c"))
    h = w.history()
    assert h["ledger"] == "none" and "old spellings were not searched" in h["note"] and "through the table" not in h["note"]


def test_the_whole_document_writer_refuses_a_malformed_table(w, monkeypatch):
    from factlattice.authoring import edit
    f = doc(w.base / "corpora" / "c")
    w.registry.write_text('{"named_roots": "nope"}')
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(w.registry))
    monkeypatch.setenv("FACTLATTICE_LEDGER_EPHEMERAL_ROOTS", "")
    with pytest.raises(edit.EditRefused) as e:
        edit.write_whole_file(f, "# New [N-1]\n")
    assert e.value.evidence["refusal"] == "named-roots-malformed" and f.read_text() == DOC


def test_a_tab_in_a_table_root_is_a_legal_name_and_is_served(w):
    """The file system takes it, and the base served it. The rule is about what raises, not what looks odd."""
    root = w.base / "corpora" / "my\tdocs"
    f = doc(root / "c")
    w.set_registry(table={"data": {"path": str(root)}}, root="${data}/c")
    assert_written(w.write(f))
    assert w.history()["count"] == 1


@pytest.mark.parametrize("kind", ["not-an-object", "too-deep", "directory", "no-permission"])
def test_an_odd_old_ledger_never_raises_at_a_reader_and_says_it_is_partial(w, kind):
    old, new = w.base / "corpora" / "old", w.base / "corpora" / "new"
    f = doc(old / "c")
    w.set_registry(root=f.parent)
    assert_written(w.write(f, value="old"))
    lp = w.machine() / f"{key_of(f)}.jsonl"
    old.rename(new)
    g = new / "c" / "a.lts"
    w.set_registry(root=g.parent)
    assert_written(w.write(g, value="new"))
    w.set_registry(table={"data": {"path": str(new), "was": [str(old)]}}, root="${data}/c")
    if kind == "not-an-object":
        lp.write_bytes(b"[1, 2]\n")
    elif kind == "too-deep":
        lp.write_bytes(b"[" * 100000 + b"]" * 100000 + b"\n")
    elif kind == "directory":
        lp.unlink()
        lp.mkdir()
    else:
        lp.chmod(0)
    try:
        h = w.history()
    finally:
        if kind == "no-permission":
            lp.chmod(0o600)
    assert h["success"] is True and h["count"] == 1, h
    if kind in ("directory", "no-permission"):
        assert h["history_partial"] is True and h["ledger_left_out"] == ["ledger-unreadable"]
    else:
        assert h["unparseable_lines"] == 1


def test_refusal_words_do_not_read_as_already_present_to_a_consumer():
    """A consumer's writer (a DSD minting writer) reads `already` or `exists` in a refusal as an idempotent re-save. Our own words must not say either."""
    from factlattice import ledger
    for text in list(ledger.REFUSAL_FIX.values()) + list(ledger.DETAIL_FIX.values()):
        assert "already" not in text.lower() and "exists" not in text.lower(), text


def test_a_broken_chain_in_one_ledger_is_not_reported_as_two_ledgers_meeting(w):
    f = doc(w.base / "corpora" / "c")
    w.set_registry(root=f.parent)
    for i in range(3):
        assert_written(w.write(f, value=f"v{i}"))
    lp = w.machine() / f"{key_of(f)}.jsonl"
    lines = lp.read_bytes().splitlines(keepends=True)
    rec = json.loads(lines[1])
    rec["prev_sha256"] = "0" * 64
    lines[1] = (json.dumps(rec) + "\n").encode()
    lp.write_bytes(b"".join(lines))
    assert not [b for b in w.history()["chain_file_breaks"] if b.get("ledger_file_boundary")]


def test_partial_is_said_with_no_write_refused_when_only_a_ledger_cannot_be_read(w):
    f = doc(w.base / "corpora" / "c")
    w.set_registry(root=f.parent)
    assert_written(w.write(f))
    lp = w.machine() / f"{key_of(f)}.jsonl"
    lp.chmod(0)
    try:
        h = w.history()
    finally:
        lp.chmod(0o600)
    assert h["history_partial"] is True and "partial_note" in h and "write_refused" not in h


@pytest.mark.parametrize("name", ["my\ndocs", "my\x7fdocs"])
def test_other_control_characters_in_a_table_root_are_legal_names_too(w, name):
    root = w.base / "corpora" / name
    f = doc(root / "c")
    w.set_registry(table={"data": {"path": str(root)}}, root="${data}/c")
    assert_written(w.write(f))


def test_the_partial_note_says_the_cause(w, monkeypatch):
    """Each wording, on a run that has that cause, so reverting a branch goes red."""
    g, old_l, cur_l = _two_ledgers(w)
    good = w.registry.read_text()
    d = json.loads(good)
    d["named_roots"] = "nope"
    d["demo"]["roots"] = [{"path": str(g.parent)}]
    w.registry.write_text(json.dumps(d))
    assert "old spellings were not searched" in w.history()["partial_note"]
    w.registry.write_text(good)
    cur_l.chmod(0)
    try:
        assert "a ledger could not be read" in w.history()["partial_note"]
    finally:
        cur_l.chmod(0o600)


def test_the_partial_note_for_a_case_variant_of_a_state_root(w):
    probe = w.base / "CaseProbe"
    probe.mkdir()
    if not (w.base / "caseprobe").exists():
        pytest.skip("case-sensitive volume")
    root = w.base / "corpora" / "Data"
    doc(root / "c")
    state = w.base / "drive" / "state"
    w.set_registry(table={"data": {"path": str(root), "state": str(state)}},
                   extra={"demo": {"roots": [{"path": str(w.base / "corpora" / "DATA" / "c")}], "domain_suffix": "x.d"}})
    plant_marker(w, "data", w.registry)
    h = w.history()
    assert h["history_partial"] is True and "spells a state root in another case" in h.get("partial_note", ""), h


def test_registry_not_found_is_not_a_refusal_code():
    from factlattice import ledger
    assert "registry-not-found" not in ledger.LOCATE_CODES and "registry-not-found" not in ledger.REFUSAL_FIX


def test_this_slice_cannot_begin_a_state_location(w):
    """S2b is owed: nothing here writes a marker, and a table that declares a state refuses, with words that say so."""
    from factlattice import ledger
    f, state = _state_world(w)
    assert w.run("ledger", "adopt", "--root", "data", "--registry", str(w.registry)).returncode == 2, "no such command"
    r = w.write(f)
    assert refusal(r) == "state-unavailable" and "this engine cannot begin one" in r["evidence"]["fix"] and not state.exists()
    assert not hasattr(ledger, "ledgers_for")
