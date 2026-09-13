from tools.shadow_replay import simulate_plan
from tools.vergex_gens import gen_dual_supertrend, gen_momo_breakout, gen_trend_pullback


def _tl(n=300, drift=0.4):
    # 上行随机游走: 保证三种规则都有信号
    out, price, base = [], 100.0, 1_700_000_000_000
    import random
    rng = random.Random(7)
    for i in range(n):
        o = price
        price = max(1.0, price + drift + rng.uniform(-0.6, 0.6))
        out.append((base + i * 900_000, o, price + 0.3, price - 0.3, price))
    return out

PLANS = (gen_momo_breakout(_tl(), "ZECUSDT", "15m"),
         gen_dual_supertrend(_tl(), "ZECUSDT", "15m"),
         gen_trend_pullback(_tl(), "ETHUSDT", "15m"))

def test_each_gen_produces_plans():
    for plans in PLANS:
        assert plans, "上行序列应产生信号"

def test_plan_geometry_valid():
    for plans in PLANS:
        for p in plans:
            assert p.stop < p.entry < p.target
            assert p.order_type == "突破单" and p.direction in ("做多", "做空")

def test_simulate_plan_accepts_plans():
    tl = _tl()
    outs = [simulate_plan(p, tl, risk_usdt=4.0, fee_rate=0.0, leverage=10,
                          margin_usdt=250.0, partial_pct=50.0, timeout_min=60,
                          time_stop_min=720, min_stop_pct=0.2, min_stop_atr=0.0,
                          be_trigger=0.5, entry_gtx=True, maker_fee=0.0003,
                          taker_fee=0.0006) for p in PLANS[0]]
    assert outs and all(hasattr(o, "filled") for o in outs)
