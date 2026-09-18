#!/usr/bin/env bash
# T6 Step 5 日检: 两条命令都跑, 追加一行到 logs/daily_check.tsv (gitignore).
# 用法(仓库根):  bash scripts/daily_check.sh
set -u
cd "$(dirname "$0")/.." || exit 1

ts=$(date '+%Y-%m-%d %H:%M:%S')
report=$(.venv/bin/python tools/expectancy_report.py 2>&1 | head -1 | tr '\t' ' ')
orders=$(ls records/orders/ 2>/dev/null | wc -l | tr -d ' ')

service="no"
if [ -f config/monitoring.pid ]; then
  if .venv/bin/python -c "import json,os,pathlib;p=json.loads(pathlib.Path('config/monitoring.pid').read_text())['pid'];os.kill(p,0)" 2>/dev/null; then
    service="yes"
  fi
fi

mkdir -p logs
printf '%s\t%s\torders=%s\tservice=%s\n' "$ts" "$report" "$orders" "$service" >> logs/daily_check.tsv
echo "--- 日检 ($ts) ---"
echo "1) $report"
echo "2) records/orders/ 条目数 = $orders  (必须 > 0)"
echo "3) 服务存活 = $service"
tail -3 logs/daily_check.tsv
