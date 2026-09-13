import json

from tools.audit.baseline_lab import build_timeline_from_klines


def test_build_timeline_from_klines(tmp_path):
    rows = {"ZECUSDT": [[1700000000000 + i * 900000, "10", "11", "9", "10.5", "1.0"]
                        for i in range(3)]}
    (tmp_path / "klines.json").write_text(json.dumps(rows))
    tl = build_timeline_from_klines(tmp_path, "ZECUSDT")
    assert len(tl) == 3 and tl[0][0] == 1700000000000 and tl[0][4] == 10.5


def test_rules_registry_contains_vergex_gens():
    from tools.audit.baseline_lab import RULES
    assert {"momo_break", "dual_supertrend", "trend_pullback"} <= set(RULES)
    for name in ("ema_pullback", "donchian_break", "momo_break",
                 "dual_supertrend", "trend_pullback"):
        gen, grid = RULES[name]
        assert callable(gen) and grid and all(isinstance(c, dict) for c in grid)
