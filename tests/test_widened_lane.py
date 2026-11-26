"""`tools/widened_lane.py` computes the test lane from the tree. These tests pin what it must include, with a planted layout as the control."""
import importlib.util
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("widened_lane", REPO / "tools" / "widened_lane.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["widened_lane"] = mod
    spec.loader.exec_module(mod)
    return mod


def _layout(tmp_path):
    (tmp_path / "factlattice_engine").mkdir()
    (tmp_path / "factlattice").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "factlattice").joinpath("__init__.py").write_text("")
    (tmp_path / "factlattice_engine" / "__init__.py").write_text("from .tools import LtsEngine\nfrom .store import LtsStore\n")
    (tmp_path / "factlattice_engine" / "tools.py").write_text("class LtsEngine: pass\n")
    (tmp_path / "factlattice_engine" / "store.py").write_text("class LtsStore: pass\n")
    (tmp_path / "factlattice_engine" / "extra.py").write_text("from .tools import LtsEngine\n")
    (tmp_path / "tests" / "test_via_package.py").write_text("from factlattice_engine import LtsEngine\n")
    (tmp_path / "tests" / "test_via_store.py").write_text("from factlattice_engine import LtsStore\n")
    (tmp_path / "tests" / "test_direct.py").write_text("from factlattice_engine.tools import LtsEngine\n")
    (tmp_path / "tests" / "test_dependent.py").write_text("from factlattice_engine.extra import something\n")
    (tmp_path / "tests" / "test_reads_source.py").write_text("from pathlib import Path\nREPO = Path(__file__).parent.parent\ntext = (REPO / 'SDLC.md').read_text()\n")
    (tmp_path / "tests" / "test_registry_user.py").write_text("from factlattice import registry\n")
    (tmp_path / "tests" / "test_rootview_user.py").write_text("from factlattice.rootview import RootView\n")
    (tmp_path / "tests" / "test_unrelated.py").write_text("def test_x():\n    assert 1\n")


def test_the_lane_takes_a_class_the_package_reexports_a_direct_importer_and_a_reader_of_repo_text(tmp_path):
    """P3-7. `from factlattice_engine import LtsEngine` (a class the package `__init__` re-exports from `tools`) is an importer of `tools`; a test that imports a changed module
    directly is; a test that reads repo source or SDLC text without importing anything is; an unrelated test is not; a dependent of a changed module is NOT in the lane
    unless `--transitive`. Red on fbb55c2 (the package import was missed)."""
    W = _load()
    _layout(tmp_path)
    W.REPO = str(tmp_path)
    got = W.lane(["factlattice_engine/tools.py"])
    assert "tests/test_via_package.py" in got and "tests/test_direct.py" in got and "tests/test_reads_source.py" in got, got
    assert "tests/test_unrelated.py" not in got and "tests/test_via_store.py" not in got and "tests/test_dependent.py" not in got, got
    assert "tests/test_registry_user.py" in got and "tests/test_rootview_user.py" in got, got      # P3-1: the registry modules count whatever changed
    wide = W.lane(["factlattice_engine/tools.py"], transitive=True)
    assert "tests/test_dependent.py" in wide and "tests/test_unrelated.py" not in wide, wide


def test_the_real_lane_for_the_engine_modules_includes_the_five_tests_the_cold_read_found_missing():
    W = _load()
    W.REPO = str(REPO)
    lane = W.lane(["factlattice_engine/tools.py", "factlattice_engine/store.py"])
    for t in ("test_aggregate_truncated_field", "test_budget_oversize_skip_and_report", "test_capabilities_anchor_truncation", "test_list_valued_property_search", "test_query_limit_order"):
        assert f"tests/{t}.py" in lane, t
    for t in ("test_bounded_roots_docs", "test_root_io_inventory", "test_private_corpus_fanout", "test_boot_verdict_table"):
        assert f"tests/{t}.py" in lane, t


def test_a_change_to_the_boot_or_config_keeps_the_registry_tests_in_the_lane():
    """P3-1. Restores the rule that dropped three registry tests from the lane for a change to bootload.py or config.py."""
    W = _load()
    W.REPO = str(REPO)
    for changed in (["factlattice_engine/bootload.py"], ["factlattice_engine/config.py"], ["factlattice_engine/tools.py"]):
        lane = W.lane(changed)
        for t in ("test_registry_round5", "test_registry_hash_scope", "test_contradiction_external_prefixes"):
            assert f"tests/{t}.py" in lane, (changed, t)
