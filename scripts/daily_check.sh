#!/usr/bin/env bash
# T6 Step 5 日检: 三条都跑, 追加一行到 logs/daily_check.tsv (gitignore).
# 用法(仓库根):  bash scripts/daily_check.sh
#
# 口径说明: R2/R3 判定只看**闸门上线后**的样本, 所以日检必须同时打印
# --gate-since 的口径; 全部样本那一行只作为历史参照。
set -u
cd "$(dirname "$0")/.." || exit 1

ts=$(date '+%Y-%m-%d %H:%M:%S')
all=$(.venv/bin/python tools/expectancy_report.py 2>&1 | head -1 | tr '\t' ' ')
post=$(.venv/bin/python tools/expectancy_report.py --gate-since 2>&1 | head -1 | tr '\t' ' ')
orders=$(ls records/orders/ 2>/dev/null | wc -l | tr -d ' ')

service="no"
if [ -f config/monitoring.pid ]; then
  if .venv/bin/python -c "import json,os,pathlib;p=json.loads(pathlib.Path('config/monitoring.pid').read_text())['pid'];os.kill(p,0)" 2>/dev/null; then
    service="yes"
  fi
fi

mkdir -p logs
printf '%s\t全部: %s\t上线后: %s\torders=%s\tservice=%s\n' \
  "$ts" "$all" "$post" "$orders" "$service" >> logs/daily_check.tsv
echo "--- 日检 ($ts) ---"
echo "1) 全部历史: $all"
echo "2) 上线后  : $post      <- R2/R3 判定只看这一行"
echo "3) 账本条目: $orders  (必须 > 0)   服务存活: $service"
tail -2 logs/daily_check.tsv
