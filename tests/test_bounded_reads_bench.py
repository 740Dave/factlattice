"""tools/bounded_reads_bench.py is what [ACC-BR] c8 rests on, so the parts that DECIDE are tested: the interval a ratio comes with, the
verdict that refuses to read a straddling interval as a pass, and the planted slowdown. These are pins of the instrument, run on synthetic
numbers and a fake tree; they start no server and measure no machine."""
from __future__ import annotations

import random
from pathlib import Path

from tools import bounded_reads_bench as B


def _samples(n, mean, sd, seed):
    r = random.Random(seed)
    return [max(1.0, r.gauss(mean, sd)) for _ in range(n)]


def test_identical_distributions_give_an_interval_around_one_and_a_shifted_one_does_not():
    old = _samples(80, 300, 15, 1)
    same = _samples(80, 300, 15, 2)
    shifted = [x * 1.10 for x in _samples(80, 300, 15, 3)]
    lo, mid, hi = B.boot_ratio_ci(old, same, 0.5)
    assert hi - lo < 0.08 and abs(mid - 1.0) < 0.05, (lo, mid, hi)     # narrow, and near one (a 90% interval misses the true 1.0 about one draw in ten)
    lo2, mid2, hi2 = B.boot_ratio_ci(old, shifted, 0.5)
    assert lo2 > hi, "a 10% slowdown is above the noise of identical samples"
    assert 1.05 < mid2 < 1.15


def test_the_verdict_is_a_pass_only_when_the_whole_interval_is_under_the_limit():
    assert B.verdict(0.95, 1.03, 1.05).startswith("PASS")
    assert B.verdict(1.06, 1.12, 1.05).startswith("FAIL")
    v = B.verdict(0.90, 1.20, 1.05)
    assert v.startswith("UNDECIDABLE") and "straddles" in v          # a number that straddles its own noise is not printed as a pass


def test_heavy_tails_widen_the_p95_interval_so_a_5_percent_limit_reads_undecidable():
    r = random.Random(5)
    heavy = lambda seed: [300 + (r.random() < 0.08) * r.uniform(300, 900) + r.gauss(0, 10) for _ in range(60)]
    old, new = heavy(1), heavy(2)
    lo, _mid, hi = B.boot_ratio_ci(old, new, 0.95)
    assert B.verdict(lo, hi, 1.05).startswith("UNDECIDABLE")


def test_plant_slowdown_inserts_one_sleep_before_the_registry_resolution(tmp_path):
    tree = tmp_path / "t"
    (tree / "factlattice_engine").mkdir(parents=True)
    (tree / "factlattice_engine" / "server.py").write_text("def build_tools():\n    choice = registry.resolve_registry()\n    return choice\n", encoding="utf-8")
    out = B.plant_slowdown(tree, 25.0, tmp_path / "planted")
    text = (out / "factlattice_engine" / "server.py").read_text(encoding="utf-8")
    assert text.count("_plant.sleep(0.025)") == 1 and text.index("_plant.sleep") < text.index("resolve_registry")
    compile(text, "server.py", "exec")
    assert "_plant" not in (tree / "factlattice_engine" / "server.py").read_text(encoding="utf-8"), "the original is untouched"


# ---- P2-6 (cold read of 04cc80c): the c8 verdict is on boot p50; p95 is reported with its interval and no verdict ----------------
def _rows(i50, p50, r50, i95=(0.89, 0.98, 1.10), r95=(0.83, 1.0, 1.6)):
    return ("ident", i50, i95), ("plant", p50, (0.9, 1.1, 2.7)), ("real", r50, r95)


def test_the_boot_verdict_is_on_p50_and_a_straddling_p95_does_not_change_it():
    good_ctl = ((0.97, 1.0, 1.03), (1.06, 1.1, 1.2))                      # identical stays in the band, planted +10% detected
    ident, plant, real = _rows(good_ctl[0], good_ctl[1], (0.95, 0.99, 1.03))
    lines = B.boot_reading(ident, plant, real, 120, 1200)
    verdict_line = next(l for l in lines if "c8 boot limit" in l)
    assert "on the p50 ratio" in verdict_line and "PASS" in verdict_line, lines            # p50 whole interval under 1.05, controls held; p95 straddles 1.05 and is irrelevant
    p95_line = next(l for l in lines if l.strip().startswith("p95"))
    assert "no limit, no verdict" in p95_line and "0.830-1.600" in p95_line and "PASS" not in p95_line and "FAIL" not in p95_line, p95_line


def test_a_p50_over_the_limit_fails_and_a_run_whose_controls_did_not_hold_is_undecidable():
    good = ((0.97, 1.0, 1.03), (1.06, 1.1, 1.2))
    lines = B.boot_reading(*_rows(good[0], good[1], (1.08, 1.12, 1.2)), 120, 1200, limit=1.05)         # the rule, at an explicit limit (the default is 1.12)
    assert "FAIL" in next(l for l in lines if "c8 boot limit" in l)
    noisy = ((0.90, 0.97, 1.08), (0.99, 1.1, 1.2))                      # identical code straddles the band; the planted one is not clear of it
    lines = B.boot_reading(*_rows(noisy[0], noisy[1], (0.95, 0.99, 1.03)), 60, 1200, limit=1.05)
    v = next(l for l in lines if "c8 boot limit" in l)
    assert "UNDECIDABLE" in v and "PASS" not in v, v                    # an interval under the limit is still not a pass when the controls failed


# ---- round 3: the CLI entry itself, and no name defined twice (the p95 `boot_study` had shadowed the p50 one) ------------------
def test_no_top_level_name_is_defined_twice_in_the_tool():
    import ast
    import collections
    tree = ast.parse(Path(B.__file__).read_text(encoding="utf-8"))
    names = collections.Counter(n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)))
    assert {k: v for k, v in names.items() if v > 1} == {}, "a later definition silently replaces the earlier one"
    # negative control: the same scan finds a planted duplicate
    planted = ast.parse("def f(): pass\ndef f(): pass\n")
    assert [k for k, v in collections.Counter(n.name for n in planted.body).items() if v > 1] == ["f"]


def test_the_boot_study_the_command_line_runs_prints_a_p50_verdict(tmp_path, monkeypatch, capsys):
    """Through `main()` with the study's own entry (argv), the boots stubbed: the printed READING says p50 for the limit and gives p95 no verdict."""
    import sys
    old, new = tmp_path / "old", tmp_path / "new"
    for d in (old, new):
        (d / "factlattice_engine").mkdir(parents=True)
        (d / "factlattice_engine" / "server.py").write_text("def build_tools():\n    choice = registry.resolve_registry()\n", encoding="utf-8")
    seen = []

    def fake_boot_pairs(a, b, pairs, tag, root):
        seen.append(tag)
        rnd = random.Random(7)
        o = [1000.0 * (1 + rnd.uniform(-0.01, 0.01)) for _ in range(pairs)]
        factor = {"c0": 1.0, "c1": 1.12, "c2": 0.99}[tag]
        return o, [x * factor * (1 + rnd.uniform(-0.01, 0.01)) for x in o]
    monkeypatch.setattr(B, "boot_pairs", fake_boot_pairs)
    monkeypatch.setattr(B, "_warm_bytecode", lambda trees: None)
    monkeypatch.setattr(sys, "argv", ["bench", "--boot-study", "--old", str(old), "--new", str(new), "--pairs", "20", "--plant-ms", "120"])
    assert B.main() == 0
    out = capsys.readouterr().out
    assert seen == ["c0", "c1", "c2"]
    assert "c8 boot limit 1.12 on the p50 ratio: PASS" in out, out
    assert "on the p95 ratio" not in out and "p95 (reported, no limit, no verdict)" in out, out


def test_the_identical_code_band_is_fixed_and_does_not_move_with_the_limit():
    """Round 30b. The control band (spec 38 x-c8: identical code stays within a narrow band) is `CONTROL_BAND` = (0.95, 1.05) whatever the boot limit. With the limit at 1.12 an identical-code control whose
    p50 interval reaches 1.07 makes the run UNDECIDABLE, though the real interval is far under 1.12; at 1.05 and at 1.12 alike. A quiet control still decides."""
    assert B.CONTROL_BAND == (0.95, 1.05)
    plant = (1.06, 1.1, 1.2)
    real = (0.95, 0.99, 1.03)
    for limit in (1.05, 1.12, B.BOOT_LIMIT):
        noisy = (1.04, 1.055, 1.07)                                         # the control's own p50 interval reaches 1.07
        lines = B.boot_reading(*_rows(noisy, plant, real), 120, 1200, limit=limit)
        v = next(l for l in lines if "c8 boot limit" in l)
        assert "UNDECIDABLE" in v and "PASS" not in v, (limit, v)
        assert any("narrow band (0.95-1.05, fixed, whatever the limit): NO" in l for l in lines), lines
        quiet = (0.97, 1.0, 1.03)
        lines = B.boot_reading(*_rows(quiet, plant, real), 120, 1200, limit=limit)
        assert "PASS" in next(l for l in lines if "c8 boot limit" in l), (limit, lines)
