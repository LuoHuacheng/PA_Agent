# VergeX 对齐报告: btc_zec_rotation (BTC_ZEC_MultiTF_Rotation_41249e_812d)

- K 线周期: 15m (bar_ms=900000)
- 成交笔数: 653

## 指标

| 指标 | 值 |
| --- | --- |
| bar_close_entry_rate | 0.406 |
| stop_dist_atr (中位数) | 2.806 |
| hold_hours_median | 31.000 |
| win_rate | 0.458 |
| n | 653 |

入场偏移分桶(open_ms % bar_ms):

- <60s: 265
- 60-300s: 0
- 300-900s: 388

## 平仓原因分组

| 分组 | 笔数 |
| --- | --- |
| 止损类 | 53 |
| 止盈类 | 27 |
| 指标平仓类 | 573 |

## 月度盈亏 (UTC+8, pnl_net 合计)

| 月份 | pnl_net |
| --- | --- |
| 2026-03 | +841.60 |
| 2026-04 | +7210.44 |
| 2026-05 | +21653.38 |
| 2026-06 | -2000.00 |
| 2026-07 | +7674.31 |
| 2026-08 | +4551.80 |

## Top5 open_reason

- [88 次] SHORT: 4h=bear, ADX=14.6, mom=-1.9%, w=1.0, RSI=34.8, SK=34.3, dd_scale=1.00
- [76 次] LONG: 4h=bull, ADX=20.4, mom=6.6%, w=3.0, RSI=58.3, SK=83.9, dd_scale=1.00
- [70 次] LONG: 4h=bull, ADX=33.5, mom=12.4%, w=3.0, RSI=52.9, SK=39.3, dd_scale=1.00
- [64 次] SHORT: 4h=bear, ADX=17.7, mom=-0.8%, w=1.0, RSI=36.9, SK=22.6, dd_scale=1.00
- [50 次] SHORT: 4h=bear, ADX=25.1, mom=-8.7%, w=3.0, RSI=30.7, SK=12.7, dd_scale=1.00

## 判读

- bar_close_entry_rate=0.406 <= 0.8: 入场不固定在收盘后 60s 内, 建模时按信号触发时点(盘中)生成器处理。
- stop_dist_atr 中位数=2.81 ATR: 建议初始止损设为 2.1~3.51 ATR。
- hold_hours_median=31.00h: 属隔夜短线(1-2 日), 建模持仓周期按日级校准。
