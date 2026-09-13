# VergeX code-strategy detail.json 数据字典

> 抓取时点: 2026-09-14。数据来源为 4 个目标代码策略的 `backtest_snapshot`。
> **鉴权方式: Bearer token,浏览器 localStorage,勿入库。** 仓库代码/数据/文档一律不含 token 明文。

## 端点

```
GET /api/v1/marketplace/code-strategies/{id}
Authorization: Bearer <auth_token>   # token 存于 vergex.trade localStorage 的 auth_token
```

仅浏览器同源可用(Cloudflare 挡脚本);仓库内 Python 工具只读落盘 JSON,不发网络请求。

## 抓取清单

| slug | id | 策略 | paired_trades | total_trades | 校验 |
|------|----|------|---------------|--------------|------|
| eth_mid | 0cf1415d-5ca2-4bce-920f-18db10c90f2d | ETH中线策略-稳中求进 | 111 | 111 | ✅ |
| btc_zec_rotation | 227e057a-c21b-4b08-a674-cf43d0553630 | BTC_ZEC_MultiTF_Rotation | 653 | 653 | ✅ |
| zec_mom1_5x | e649c06d-fbc0-4c2b-b32c-c946a3f8814e | ZEC_MOM1_5x_50pct | 282 | 282 | ✅ |
| zec_high_freq | 23a82440-bc0b-43c2-b3df-eb4300251984 | ZEC高频趋势追踪大王 | 91 | 91 | ✅ |

## 顶层字段(节选)

`id, strategy_id, name, protocol_name, protocol_version, meta_interval, meta_pair,
published_at, has_backtest, pnl_pct, sharpe_ratio, max_drawdown_pct, win_rate,
total_trades, profit_factor, initial_capital, creator_username, has_active_paper_run,
follower_count, code_hash, description(部分), backtest_snapshot`

## backtest_snapshot 字段

| 字段 | 说明 |
|------|------|
| paired_trades | 成对成交列表(逐笔),见下表 |
| equity_curve | 净值曲线点列表,元素 `{timestamp, equity, ...}`,可选 `drawdown_pct`(eth_mid 有)、`source`("official"/"coverage")、`trade_key`(按成交聚合的曲线才有);official = 每笔成交后记录,coverage = 到数据末端的补点 |
| data_start_time_ms / data_end_time_ms | 回测数据起止(eth_mid 等部分策略有) |
| initial_capital / balance / total_pnl_net / total_pnl_pct | 资金口径 |
| win_rate / profit_factor / sharpe_ratio / max_drawdown_pct | 平台公布指标 |
| total_fees / total_funding | 费用合计(部分策略有) |
| signal_stats / open_positions / avg_holding_period / best_trade / worst_trade / live / metrics_source / performance_version | 部分策略有,口径补充 |

## paired_trades 字段表(实测)

| 字段 | 类型 | 说明 |
|------|------|------|
| action | str | 实测枚举: `CLOSE_LONG` / `CLOSE_SHORT` / `REDUCE_POSITION` |
| side | str | 实测枚举: `LONG` / `SHORT` |
| symbol | str | 实测取值: `ETHUSDT`、`ETH`、`ZEC` —— **裸 `ETH`/`ZEC` 需归一化为 `ETHUSDT`/`ZECUSDT`**(加载器职责) |
| entry_price / exit_price | number | 开/平仓价 |
| quantity | number | 数量(张或币,平台口径) |
| open_timestamp / close_timestamp | int(ms) | 开/平仓时间戳 |
| holding_seconds | int | 持仓秒数 |
| pnl_gross / pnl_net | number | 毛/净盈亏(USDT) |
| open_fee / close_fee / total_fee | number | 手续费 |
| slippage | number | 滑点 |
| funding | number | 资金费(部分策略行有,非全量) |
| source | str\|null | 实测取值: `backtest` / `paper` / `null` |
| trade_key | str | 成交指纹 |
| open_reason | str | **入场规则富文本**(如 `[多头]日线EMA50↑, 4h回踩EMA21(3116.0)收阳, 30m MACD金叉; 第1批50% SL=3104.3 TP1=3221.4`)——反推规则的主要依据 |
| close_reason | str | **出场原因富文本,非固定枚举**。固定 token 有 `stop_loss` / `take_profit`;其余为策略模板文本,实测样例: `[多头止损]4h收阴线跌破EMA21(3117.3)`、`MACD柱由正转负，平多仓`、`EMA死叉平多，ADX=25.5`、`2ATR止盈触发，当前524.14`、`Partial TP LONG 126.7628 at 3.5ATR=246.58, SL->cost`(分批减仓)。归类需按前缀/关键词匹配,不能按全串 |

## 时间范围(数据)

最早 open_timestamp ≈ 2025-11-19(eth_mid equity_curve 起点),最晚 ≈ 2026-09-14。
K 线对齐取数窗口须覆盖 `min(open_timestamp) ~ max(close_timestamp)`。
