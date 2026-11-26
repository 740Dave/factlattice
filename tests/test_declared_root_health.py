"""[DECLARED-ROOT-HEALTH] — a registered root the engine skips is DECLARED, never silent.

THE DEFECT. `CorpusIndex.live_roots` skips a registered root whose path is not a directory (or whose `only_file`
pin is absent). The skip is deliberate (a registered-but-empty project is not an error, and a root that appears
later is picked up by self-heal), but nothing said it happened: `lts_list_corpora` reported the corpus fresh with
only the surviving roots in `root_paths`, and a scoped query answered from whatever roots remained. Observed on a
real deployment: several corpora whose own roots did not exist each served only a root shared with other corpora, so
different corpus names returned the same nodes, with nothing in any response saying so.

THE CONTRACT (serving is unchanged; only declaration is added):
  * `lts_list_corpora` carries `root_health` on an entry ONLY when it is not healthy:
      `missing_roots` [{path, area, reason: not-a-directory | only-file-absent}],
      `empty_roots`   [{path, area}]   a live root contributing no `.lts` file,
      `own_content`   false when every contributing root is also registered by another corpus,
      `status`        "degraded" (missing or empty roots) | "shared-only".
    A healthy entry is unchanged (no new key).
  * A corpus-scoped query over a degraded corpus says so in `extent.notes`; a healthy one adds no note.
  * `heal=false` reports the same (it is status, not a side effect).
  * A root that appears after the index was built is declared `unindexed_roots` until a rebuild serves it.
    (That it is not served, and the corpus reports fresh, is a separate serving defect: see the strict xfail.)
"""

from __future__ import annotations

import pytest

from factlattice_engine import (
    FreshnessKernel,
    LtsEngine,
    LtsStore,
    PollVerifiedFreshness,
    SelfHealer,
)
from factlattice_engine.config import build_merged_partition


def _lts(node_id: str) -> str:
    return ("---\ndomain: core.example\n---\n"
            f"# Test [SPEC:{node_id}-DOC]\n\n"
            f"### [{node_id}] Node {node_id}\n- definition: node {node_id}\n")


def _engine(*parts):
    store = LtsStore()
    for part in parts:
        store.add_partition(part)
    kernel = FreshnessKernel()
    kernel.seat_store(store)
    store.kernel = kernel
    provider = PollVerifiedFreshness()
    return LtsEngine(store, healer=SelfHealer(store, kernel=kernel, freshness_provider=provider),
                     freshness_provider=provider)


def _entry(res, cid):
    return next(c for c in res["corpora"] if c["corpus_id"] == cid)


@pytest.fixture
def world(tmp_path):
    def own(name, node):
        d = tmp_path / name; d.mkdir(); (d / f"{name}.lts").write_text(_lts(node), encoding="utf-8"); return d
    shared = own("shared", "SHARED-1")                   # the only root two corpora register
    empty = tmp_path / "empty"; empty.mkdir()
    missing = tmp_path / "missing"                       # registered, never created
    pin = own("pin", "PIN-1")
    parts = {
        "healthy": build_merged_partition("healthy", [{"path": str(own("h", "OTHER-1")), "area": "specs"}],
                                          "proj.healthy"),
        "has-missing": build_merged_partition("has-missing", [{"path": str(own("m", "OWN-1")), "area": "specs"},
                                                             {"path": str(missing), "area": "decisions"}], "proj.m"),
        "has-empty": build_merged_partition("has-empty", [{"path": str(own("e", "OWN-2")), "area": "specs"},
                                                         {"path": str(empty), "area": "artifacts"}], "proj.e"),
        # serves ONLY the shared root (its own root is missing) -> shared-only (it is also degraded)
        "shared-only": build_merged_partition("shared-only", [{"path": str(tmp_path / "missing2"), "area": "specs"},
                                                             {"path": str(shared), "area": "artifacts"}], "proj.so"),
        "shares-but-owns": build_merged_partition("shares-but-owns", [{"path": str(own("s", "OTHER-2")), "area": "specs"},
                                                                     {"path": str(shared), "area": "artifacts"}],
                                                  "proj.sbo"),
        "pin-absent": build_merged_partition("pin-absent", [{"path": str(own("p", "OWN-3")), "area": "specs"},
                                                           {"path": str(pin), "area": "decisions",
                                                            "only_file": "absent.lts"}], "proj.pa"),
    }
    assert all(p is not None for p in parts.values())
    return {"tmp": tmp_path, "missing": missing, "empty": empty, "pin": pin, "parts": parts}


def test_missing_root_is_declared_with_its_reason(world):
    eng = _engine(*world["parts"].values())
    rh = _entry(eng.list_corpora(), "has-missing")["root_health"]
    assert rh["status"] == "degraded"
    assert rh["missing_roots"] == [{"path": str(world["missing"]), "area": "decisions", "reason": "not-a-directory"}]
    assert rh["empty_roots"] == []
    assert rh["own_content"] is True


def test_empty_root_is_declared(world):
    eng = _engine(*world["parts"].values())
    rh = _entry(eng.list_corpora(), "has-empty")["root_health"]
    assert rh["status"] == "degraded"
    assert rh["empty_roots"] == [{"path": str(world["empty"]), "area": "artifacts"}]
    assert rh["missing_roots"] == []


def test_absent_only_file_pin_is_declared(world):
    eng = _engine(*world["parts"].values())
    rh = _entry(eng.list_corpora(), "pin-absent")["root_health"]
    assert rh["missing_roots"] == [{"path": str(world["pin"]), "area": "decisions", "reason": "only-file-absent"}]


def test_a_corpus_served_only_by_shared_roots_says_so(world):
    eng = _engine(*world["parts"].values())
    res = eng.list_corpora()
    rh = _entry(res, "shared-only")["root_health"]
    assert rh["own_content"] is False
    assert rh["status"] == "shared-only"
    # sharing a root is not by itself a defect: a corpus with content of its own carries no root_health
    assert "root_health" not in _entry(res, "shares-but-owns")


def test_a_healthy_entry_is_unchanged(world):
    eng = _engine(*world["parts"].values())
    e = _entry(eng.list_corpora(), "healthy")
    assert "root_health" not in e
    assert set(e) == {"corpus_id", "domain_suffix", "total_nodes", "total_nodes_live", "counts_agree", "total_edges",
                      "resolved_edge_count", "root_paths", "fresh", "built_at", "counts_are_as_of"}


def test_heal_false_reports_the_same(world):
    eng = _engine(*world["parts"].values())
    assert _entry(eng.list_corpora(heal=False), "has-missing")["root_health"]["missing_roots"][0]["reason"] \
        == "not-a-directory"


def test_scoped_query_over_a_degraded_corpus_says_so_and_a_healthy_one_does_not(world):
    eng = _engine(*world["parts"].values())
    bad = eng.find(corpus="has-missing", prefix="OWN")
    assert bad["success"] and "OWN-1" in {n["id"] for n in bad["nodes"]}    # serving unchanged
    notes = bad["extent"].get("notes", [])
    assert any(str(world["missing"]) in n and "does not exist" in n for n in notes), notes
    good = eng.find(corpus="healthy", prefix="OTHER")
    assert not any("does not exist" in n or "no .lts" in n for n in good["extent"].get("notes", []))


def test_a_root_that_appears_later_is_stale_and_declared_until_healed(world):
    """A root created after the index was built makes the corpus NOT fresh (reason root-set-changed), and an observe-only
    listing declares it unindexed; a healing call re-seats it, serves it, and the declaration clears."""
    eng = _engine(*world["parts"].values())
    world["missing"].mkdir()
    (world["missing"] / "late.lts").write_text(_lts("LATE-1"), encoding="utf-8")
    e = _entry(eng.list_corpora(heal=False), "has-missing")
    assert e["fresh"] is False
    rh = e["root_health"]
    assert rh["status"] == "degraded" and rh["missing_roots"] == []
    assert rh["unindexed_roots"] == [{"path": str(world["missing"]), "area": "decisions"}]
    healed = _entry(eng.list_corpora(), "has-missing")             # heal=True by default
    assert healed["fresh"] is True and "root_health" not in healed
    assert str(world["missing"]) in healed["root_paths"]


def test_a_root_that_vanishes_after_the_index_was_built_is_not_served_as_fresh(world):
    """The mirror case: a seated root deleted later. The corpus must not report fresh over content it no longer has."""
    import shutil
    eng = _engine(*world["parts"].values())
    gone = world["tmp"] / "m"                                        # has-missing's own (seated) root
    assert eng.find(corpus="has-missing", prefix="OWN-1")["success"]
    shutil.rmtree(gone)
    res = eng.find(corpus="has-missing", prefix="OWN")
    assert not (res.get("success") and "OWN-1" in {n["id"] for n in res.get("nodes", [])}), res


def test_a_root_that_appears_later_is_served(world):
    eng = _engine(*world["parts"].values())
    world["missing"].mkdir()
    (world["missing"] / "late.lts").write_text(_lts("LATE-1"), encoding="utf-8")
    assert "LATE-1" in {n["id"] for n in eng.find(corpus="has-missing", prefix="LATE")["nodes"]}


# ---------------------------------------------------------------------------- root-set change: recovery and edge shapes
def _late_world(tmp_path):
    a = tmp_path / "a"; a.mkdir(); (a / "a.lts").write_text(_lts("A-1"), encoding="utf-8")
    late = tmp_path / "late"
    part = build_merged_partition("c", [{"path": str(a), "area": "specs"}, {"path": str(late), "area": "decisions"}],
                                  "proj.c")
    return part, late


def _ids(res):
    return {n["id"] for n in res.get("nodes", [])} if res.get("success") else set()


def test_the_refusal_carries_the_root_set_reason(tmp_path):
    part, late = _late_world(tmp_path)
    _engine(part)
    late.mkdir(); (late / "l.lts").write_text(_lts("LATE-1"), encoding="utf-8")
    fr = part.freshness()
    assert fr.fresh is False and fr.reason == "root-set-changed"


def test_one_rebuild_per_root_set_change(tmp_path):
    part, late = _late_world(tmp_path)
    eng = _engine(part)
    late.mkdir(); (late / "l.lts").write_text(_lts("LATE-1"), encoding="utf-8")
    for _ in range(5):
        assert "LATE-1" in _ids(eng.find(corpus="c", prefix="LATE"))
    assert eng.healer.rebuilds == 1


@pytest.mark.skipif(not hasattr(__import__("os"), "geteuid") or __import__("os").geteuid() == 0, reason="root ignores chmod")
def test_a_failed_reseat_on_an_unreadable_new_root_recovers_once_fixed(tmp_path):
    part, late = _late_world(tmp_path)
    eng = _engine(part)
    late.mkdir(); (late / "l.lts").write_text(_lts("LATE-1"), encoding="utf-8")
    late.chmod(0o000)
    try:
        assert not eng.find(corpus="c", prefix="A")["success"]          # fail-closed while the new root is unreadable
        assert not eng.find(corpus="c", prefix="A")["success"]
    finally:
        late.chmod(0o755)
    assert "LATE-1" in _ids(eng.find(corpus="c", prefix="LATE"))       # fixed -> retried -> served, no restart


def test_a_failed_reseat_on_a_fifo_recovers_once_replaced(tmp_path):
    import os
    part, late = _late_world(tmp_path)
    eng = _engine(part)
    late.mkdir(); os.mkfifo(late / "l.lts")
    assert not eng.find(corpus="c", prefix="A")["success"]
    (late / "l.lts").unlink(); (late / "l.lts").write_text(_lts("LATE-1"), encoding="utf-8")
    assert "LATE-1" in _ids(eng.find(corpus="c", prefix="LATE"))


def test_a_failed_reseat_whose_cause_is_a_fifo_in_a_seated_root_recovers_once_removed(tmp_path):
    """P1-A1 (round-2 Diamond, probe N1): a root appears while a SEATED root holds a FIFO `.lts`. The re-seat fails and
    the corpus is refused; once the FIFO is removed, the next query must retry and serve (never suppressed for good).
    The storm-guard key must therefore move with the seated roots' state, not only the new roots'."""
    import os
    part, late = _late_world(tmp_path)
    eng = _engine(part)
    seated = tmp_path / "a"
    os.mkfifo(seated / "bad.lts")
    late.mkdir(); (late / "l.lts").write_text(_lts("LATE-1"), encoding="utf-8")
    assert not eng.find(corpus="c", prefix="LATE")["success"]
    assert not eng.find(corpus="c", prefix="LATE")["success"]           # the same failure is suppressed, not retried
    (seated / "bad.lts").unlink()
    assert "LATE-1" in _ids(eng.find(corpus="c", prefix="LATE"))       # cause fixed in the seated root -> retried


@pytest.mark.skipif(not hasattr(__import__("os"), "geteuid") or __import__("os").geteuid() == 0, reason="root ignores chmod")
def test_a_failed_reseat_whose_cause_is_an_unreadable_file_in_a_seated_root_recovers_once_fixed(tmp_path):
    """P1-A1, probe N1b: the same with a chmod-000 `.lts` in a seated root, then made readable again."""
    part, late = _late_world(tmp_path)
    eng = _engine(part)
    bad = tmp_path / "a" / "u.lts"
    bad.write_text(_lts("A-U"), encoding="utf-8"); bad.chmod(0o000)
    late.mkdir(); (late / "l.lts").write_text(_lts("LATE-1"), encoding="utf-8")
    try:
        assert not eng.find(corpus="c", prefix="LATE")["success"]
        assert not eng.find(corpus="c", prefix="LATE")["success"]
    finally:
        bad.chmod(0o644)
    assert "LATE-1" in _ids(eng.find(corpus="c", prefix="LATE"))


def test_a_failed_reseat_on_a_broken_link_in_a_seated_root_recovers_once_its_target_exists(tmp_path):
    """P1-C1 (round-3 Diamond, probe R3): a seated `.lts` is a symlink to a missing target when a root appears; the
    re-seat fails. Creating the TARGET (the link itself unchanged) must let the next query retry and serve. The engine
    follows the link, so the storm-guard key must move with the target, not only with the link's own lstat."""
    import os
    part, late = _late_world(tmp_path)
    eng = _engine(part)
    ext = tmp_path / "ext"; ext.mkdir()
    target = ext / "t.lts.txt"
    os.symlink(target, tmp_path / "a" / "lnk.lts")
    late.mkdir(); (late / "l.lts").write_text(_lts("LATE-1"), encoding="utf-8")
    assert not eng.find(corpus="c", prefix="LATE")["success"]
    assert not eng.find(corpus="c", prefix="LATE")["success"]
    target.write_text(_lts("OWN-L"), encoding="utf-8")
    assert "LATE-1" in _ids(eng.find(corpus="c", prefix="LATE"))


@pytest.mark.skipif(not hasattr(__import__("os"), "geteuid") or __import__("os").geteuid() == 0, reason="root ignores chmod")
def test_a_failed_reseat_on_an_unreadable_link_target_in_a_seated_root_recovers_once_fixed(tmp_path):
    """P1-C1, probe R4: the same with a symlinked seated `.lts` whose target is chmod 000, then made readable."""
    import os
    part, late = _late_world(tmp_path)
    eng = _engine(part)
    ext = tmp_path / "ext"; ext.mkdir()
    target = ext / "t.lts.txt"; target.write_text(_lts("OWN-L"), encoding="utf-8")
    os.symlink(target, tmp_path / "a" / "lnk.lts")
    assert "OWN-L" in _ids(eng.find(corpus="c", prefix="OWN"))        # the link is seated through the content path
    target.chmod(0o000)
    late.mkdir(); (late / "l.lts").write_text(_lts("LATE-1"), encoding="utf-8")
    try:
        assert not eng.find(corpus="c", prefix="LATE")["success"]
        assert not eng.find(corpus="c", prefix="LATE")["success"]
    finally:
        target.chmod(0o644)
    assert "LATE-1" in _ids(eng.find(corpus="c", prefix="LATE"))


def test_a_failed_reseat_on_a_broken_link_in_a_new_root_recovers_once_its_target_exists(tmp_path):
    """P1-C1, probe R5: the broken-link case in the NEW root (stuck at 15d01e2 and 6846084 too)."""
    import os
    part, late = _late_world(tmp_path)
    eng = _engine(part)
    ext = tmp_path / "ext"; ext.mkdir()
    target = ext / "t.lts.txt"
    late.mkdir(); os.symlink(target, late / "lnk.lts")
    assert not eng.find(corpus="c", prefix="LATE")["success"]
    assert not eng.find(corpus="c", prefix="LATE")["success"]
    target.write_text(_lts("LATE-L"), encoding="utf-8")
    assert "LATE-L" in _ids(eng.find(corpus="c", prefix="LATE"))


@pytest.mark.skipif(__import__("sys").platform != "darwin" or __import__("os").geteuid() == 0,
                    reason="macOS ACLs via chmod +a; root ignores them")
@pytest.mark.parametrize("via_link", [False, True])
def test_a_failed_reseat_on_an_acl_denied_member_recovers_once_the_acl_is_removed(tmp_path, via_link):
    """P1-D1 (round-3b Diamond): an ACL deny makes a seated member unreadable with NO change to its mode, size or
    mtime, so a fingerprint of those alone never moves when the ACL is removed. The change time (ctime) does move,
    so the next query after the fix must retry and serve. Covered for a direct member and a symlinked member's target."""
    import os
    import subprocess
    part, late = _late_world(tmp_path)
    eng = _engine(part)
    if via_link:
        ext = tmp_path / "ext"; ext.mkdir()
        bad = ext / "t.lts.txt"; bad.write_text(_lts("A-U"), encoding="utf-8")
        os.symlink(bad, tmp_path / "a" / "u.lts")
    else:
        bad = tmp_path / "a" / "u.lts"; bad.write_text(_lts("A-U"), encoding="utf-8")
    assert "A-U" in _ids(eng.find(corpus="c", prefix="A-U"))          # seated and served through the content path
    subprocess.run(["chmod", "+a", "everyone deny read", str(bad)], check=True)
    late.mkdir(); (late / "l.lts").write_text(_lts("LATE-1"), encoding="utf-8")
    try:
        assert not eng.find(corpus="c", prefix="LATE")["success"]
        s0 = eng.healer.suppressed
        assert not eng.find(corpus="c", prefix="LATE")["success"]
        assert eng.healer.suppressed == s0 + 1          # the same failure is suppressed, not rebuilt again (r3c P3-E2)
    finally:
        subprocess.run(["chmod", "-a", "everyone deny read", str(bad)], check=True)
    assert "LATE-1" in _ids(eng.find(corpus="c", prefix="LATE"))


def test_a_transient_corpus_moved_is_retried_on_the_next_call(tmp_path, monkeypatch):
    from factlattice_engine import incremental, selfheal
    part, late = _late_world(tmp_path)
    eng = _engine(part)
    late.mkdir(); (late / "l.lts").write_text(_lts("LATE-1"), encoding="utf-8")
    real = incremental.CorpusIndex.cold_build
    calls = {"n": 0}
    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise selfheal.CorpusMoved("simulated: a file changed during the re-index")
        return real(self, *a, **k)
    monkeypatch.setattr(incremental.CorpusIndex, "cold_build", flaky)
    assert not eng.find(corpus="c", prefix="LATE")["success"]
    assert "LATE-1" in _ids(eng.find(corpus="c", prefix="LATE"))       # not suppressed for good


def test_a_vanished_root_is_refused_with_a_stated_reason_not_served_around(tmp_path):
    """The existing rule: a seated root that vanishes is unverifiable, so the corpus is refused (fail-closed), never
    served from the remaining roots. What changed: the refusal states why (root-vanished), with no traceback."""
    import shutil
    a = tmp_path / "a"; a.mkdir(); (a / "a.lts").write_text(_lts("A-1"), encoding="utf-8")
    b = tmp_path / "b"; b.mkdir(); (b / "b.lts").write_text(_lts("B-1"), encoding="utf-8")
    part = build_merged_partition("c", [{"path": str(a), "area": "specs"}, {"path": str(b), "area": "decisions"}],
                                  "proj.c")
    eng = _engine(part)
    shutil.rmtree(b)
    res = eng.find(corpus="c", prefix="A")
    assert res["success"] is False and res["stale"] is True and res["reason"] == "root-set-changed"
    assert res["heal"]["reason"] == "root-vanished", res["heal"]
    b.mkdir(); (b / "b.lts").write_text(_lts("B-1"), encoding="utf-8")       # the root returns -> served again
    assert {"A-1", "B-1"} <= _ids(eng.find(corpus="c", prefix=""))


def test_a_legacy_single_root_corpus_is_not_called_unindexed(tmp_path):
    from factlattice_engine.config import build_partition
    a = tmp_path / "legacy"; a.mkdir(); (a / "a.lts").write_text(_lts("L-1"), encoding="utf-8")
    eng = _engine(build_partition("legacy", str(a), "proj.legacy"))
    assert "root_health" not in _entry(eng.list_corpora(heal=False), "legacy")
    notes = eng.find(corpus="legacy", prefix="L")["extent"].get("notes", [])
    assert not any("not served" in n or "does not exist" in n for n in notes), notes


def test_one_directory_pinned_twice_is_not_shared_only(tmp_path):
    d = tmp_path / "d"; d.mkdir()
    (d / "x.lts").write_text(_lts("X-1"), encoding="utf-8"); (d / "y.lts").write_text(_lts("Y-1"), encoding="utf-8")
    part = build_merged_partition("c", [{"path": str(d), "area": "specs", "only_file": "x.lts"},
                                        {"path": str(d), "area": "decisions", "only_file": "y.lts"}], "proj.c")
    eng = _engine(part)
    assert "root_health" not in _entry(eng.list_corpora(heal=False), "c")
