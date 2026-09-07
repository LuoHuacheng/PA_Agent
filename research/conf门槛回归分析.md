# trade_confidence 对交易结果回归分析 (P1-1c)

时间: 2026-09-07 ~13:35
目的: 决定是否设置/收紧自动交易 conf 门槛 (handoff P1-1 第 3 项:
"conf 门槛(先用 trade_records/*.csv 的 trade_confidence 对结果回归)").

## 方法

- 复用 tools/_pa_sim_common.py + tools/trade_pnl_report.py 的链路:
  fapi userTrades/allOrders/income 重建逐笔交易, 再用 trade_records/*.csv
  决策索引按时间/symbol 关联 trade_confidence (conf).
- 窗口: 近 5 天 (2026-09-02 20:00 ~ 2026-09-07 20:00, window_bounds(5)),
  10 symbols, 共 50 笔开仓, 49 笔已平.
- 数据缓存: /tmp/pa-reg-cache-5d (fetch_account_data 产物, 可重跑分析).
- 敏感性: 单独剔除 ZEC bug 窗口单 (ZECUSDT 且 opened_at >= 1788688800000,
  即 09-06 18:00 后止损语义 bug 造成的 -60.43 单), 结论基于剔除后 n=47.

## 结果 (剔除 ZEC bug 窗口单, 净 = realized + fees, USDT)

| conf 区间 | n | 胜率 | 净 | 均/笔 |
|---|---|---|---|---|
| 40-44 | 3 | 66.7% | +17.23 | +5.74 |
| 45-49 | 3 | 33.3% | -17.01 | -5.67 |
| 50-54 | 6 | 66.7% | +1.34 | +0.22 |
| 55-59 | 24 | 54.2% | +15.04 | +0.63 |
| 60-64 | 9 | 33.3% | -30.11 | -3.35 |
| 65-69 | 2 | 50.0% | +1.22 | +0.61 |

累积切点 (剔除 bug 单): >=50 n=41 wr51.2% net -12.51; >=55 n=35 wr48.6%
net -13.85; >=60 n=11 wr36.4% net -28.89. conf<60 n=36 wr55.6% net +16.60.

## 结论

1. conf 与结果无稳定单调关系, 反而 60-64 高置信段是最大亏损段
   (n=9, -30.11), 55-59 段最正 (+15.04). 样本 n=47 太小, 逐桶差异
   大概率是噪声/异质性 (不同参数纪元混叠, 窗口 A/B 行为不同).
2. 因此不增设一刀切 conf 门槛, 也不把现有门槛上调:
   维持 counter_trend_min_confidence=55, breakeven_min_confidence=55.
3. 后续 P2 "conf->胜率回流" 用更大样本做自适应分桶再评估 (n>=100 时重跑本分析).

## 备注

- 若含 ZEC bug 单 (n=49): 50-60 段 wr53% net -57 (bug 单污染), >=60 n=11
  wr36.4% net -28.89, 结论不变.
- 复现: 保留 /tmp/pa-reg-cache-5d 前提下按上述逻辑跑一遍即可, 无需重新 fetch.

## 持仓时长回归 (P2-2 time-stop 依据, 同窗口同数据)

| 持仓时长 | n | 胜率 | 净 | 备注 |
|---|---|---|---|---|
| <30m | 19 | 21.1% | -78.22 | 含 ZEC bug 1; 剔除后 18 笔约 -18, 仍最差段 |
| 30-60m | 10 | 30.0% | -23.01 | 快进快出类 |
| 1-3h | 13 | 100% | +86.81 | 最优段: 盈利集中在 1-3h 到位 |
| 3-6h | 3 | 33.3% | -60.78 | 含 bug 1; 剔除后 2 笔约平 |
| 6-12h | 2 | 100% | +1.54 | |
| 12-24h | 2 | 50% | -12.03 | 僵尸单 |

结论: 12h+ 段转负(-12), 6-12h 段仍正(+1.5); 保守取 time_stop_minutes=720
(12h), 削 12h+ 僵尸单且不动 6-12h 段. (初版 360 已按此修订.)

## P2-3 conf->胜率回流 落地 (2026-09-07)

- 形态: 数据回流基建 = tools/trade_pnl_report.py 增加 conf 5 分位分桶与
  持仓时长分桶输出 (离线 fapi 重建 + CSV conf 关联, 精确 realized/fees).
- 运行时闸门(默认关): conf_feedback_mode=off 保持现状(代码已带); 开启需桶样本
  >= conf_feedback_min_samples(默认30) 且定期跑 trade_pnl_report.py
  --conf-buckets-out trade_records/conf_buckets.json, executor 用桶实测胜率覆盖
  estimated_win_rate 参与交易者方程. 回归显示 conf 60-64 段反最差, 未达标前保持 off.
- 操作: 定期执行 .venv/bin/python tools/trade_pnl_report.py --days 5 查看
  分桶, 与本次基线比对 (缓存 /tmp/pa-reg-cache-5d 可复用).

