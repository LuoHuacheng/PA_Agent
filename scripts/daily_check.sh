#!/usr/bin/env bash
# 日检: 全部跑完, 追加一行到 logs/daily_check.tsv (gitignore).
# 用法(仓库根):  bash scripts/daily_check.sh
#
# 口径说明:
#   - R2/R3 判定只看**最近一次配置变更之后**的样本(--gate-since)。
#   - **现行判定目标是路 B**: --loss-bound 2, 能否排除"均亏损 > 2U/笔"
#     (见 docs/正期望验证规程.md §1.1)。
set -u
cd "$(dirname "$0")/.." || exit 1

ts=$(date '+%Y-%m-%d %H:%M:%S')
# 先回填 outcomes.csv 再判定: expectancy_report 读的就是它。不跑就断链
# (2026-09-20 实测断了 10 天, 期间日检报的 n 全是停滞值)。
backfill=$(.venv/bin/python tools/outcome_merge.py --days 7 2>&1 | grep '新 OutcomeRows' | tail -1 | tr '\t' ' ')
[ -z "$backfill" ] && backfill="(无输出)"
# 参照本金 = 账户现有余额(2026-09-20 用户裁定); 取不到就退回计划口径。
equity=$(.venv/bin/python -c "
from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import load_settings
from pa_agent.trading.binance_usdm_testnet import BinanceUSDMTestnetClient
c = load_settings(SETTINGS_JSON_PATH).binance_usdm_testnet
cl = BinanceUSDMTestnetClient(c.api_key, c.api_secret)
acct = cl._request('GET', '/fapi/v2/account', signed=True)
print('%.2f' % float(acct.get('totalWalletBalance') or 0))
" 2>/dev/null)
dd=""
if [ -n "$equity" ]; then dd="--capital $equity --account-dd-gate"; fi

all=$(.venv/bin/python tools/expectancy_report.py $dd 2>&1 | head -1 | tr '\t' ' ')
post=$(.venv/bin/python tools/expectancy_report.py --gate-since $dd 2>&1 | head -1 | tr '\t' ' ')
rb=$(.venv/bin/python tools/expectancy_report.py --gate-since --loss-bound 2 $dd 2>&1 | tail -1 | tr '\t' ' ')
gate=$(.venv/bin/python scripts/verify_gate.py 2>&1 | tail -1 | tr '\t' ' ')
rbk=$(.venv/bin/python tools/expectancy_report.py --rollback-check $dd 2>&1 | tail -1 | tr '\t' ' ')
orders=$(ls records/orders/ 2>/dev/null | wc -l | tr -d ' ')

service="no"
if [ -f config/monitoring.pid ]; then
  if .venv/bin/python -c "import json,os,pathlib;p=json.loads(pathlib.Path('config/monitoring.pid').read_text())['pid'];os.kill(p,0)" 2>/dev/null; then
    service="yes"
  fi
fi

mkdir -p logs
printf '%s\t全部: %s\t上线后: %s\t路B: %s\t回滚: %s\t闸门: %s\torders=%s\tservice=%s\t回填: %s\n' \
  "$ts" "$all" "$post" "$rb" "$rbk" "$gate" "$orders" "$service" "$backfill" >> logs/daily_check.tsv
echo "--- 日检 ($ts) ---"
echo "1) 全部历史: $all"
echo "2) 上线后  : $post      <- 看 n 的进度"
echo "3) 路B判定 : $rb      <- 现行判定目标"
echo "4) 回滚检查: $rbk"
echo "5) 闸门    : $gate"
echo "6) 账本条目: $orders  (必须 > 0)   服务存活: $service"
echo "7) 样本回填: $backfill   <- outcomes.csv 自动补新(前几行判定读它)"
tail -2 logs/daily_check.tsv
