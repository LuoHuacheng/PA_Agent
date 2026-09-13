from tools.vergex_report import build_matrix, verdict


def test_verdict_bands():
    assert verdict(50.45, 111, 52.0, 100) == "达标"
    assert verdict(50.45, 111, 60.0, 100) == "超差"   # 胜率差 9.55pp
    assert verdict(50.45, 111, 52.0, 70) == "超差"    # 笔数差 37%
    assert verdict(50.45, 111, 52.0, 10) == "样本不足"


def test_build_matrix_with_synthetic_oos(tmp_path):
    doc = {"source": "binance", "days": 120, "segments": 6,
           "oos_rows": [{"rule": "trend_pullback", "seg": 1, "combo": {}, "n": 40,
                         "r_sum": 1.0, "wr": 0.5}],
           "full_best": {"trend_pullback": {"combo": {"ema_fast": 10, "ema_slow": 200,
                         "target_rr": 3.0}, "n": 100, "wr": 0.52, "r_sum": 2.0,
                         "avg_r": 0.02}}}
    md, rows = build_matrix(doc)
    assert "不可还原边界" in md and "ETH中线策略-稳中求进" in md
    eth = next(r for r in rows if r["slug"] == "eth_mid")
    assert eth["判定"] == "达标" and abs(eth["回放wr%"] - 52.0) < 1e-9 and eth["回放n"] == 100
