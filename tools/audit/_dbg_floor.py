# -*- coding: utf-8 -*-
from pa_agent.config.settings import Settings
from pa_agent.trading import binance_usdm_testnet as bn
from decimal import Decimal

s = Settings()
cfg = s.binance_usdm_testnet
print("default mode:", cfg.min_stop_mode, "| floor:", cfg.min_stop_distance_pct, "| mult:", cfg.min_stop_atr_multiple)
cfg.min_stop_mode = "atr"
cfg.min_stop_distance_pct = 0.2
cfg.min_stop_atr_multiple = 0.8
d = {"stop_loss_price": 99.65, "atr_pct": 0.5, "entry_price": 100}
print("floor pct:", bn._stop_distance_floor_pct(cfg, d))
print("gap:", bn._stop_gap_pct(Decimal("100"), Decimal("99.65")))
# 读执行链里 decision 是否原样到达
import inspect
src = inspect.getsource(bn._execute_market_signal_once)
print("gap-check 行存在:", "stop_floor = _stop_distance_floor_pct" in src)
