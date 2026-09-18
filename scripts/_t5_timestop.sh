cd /Users/hc/Documents/ai/PA_Agent
for TS in 720 1440 4320 100000; do
  echo "=== time-stop-min=$TS ==="
  .venv/bin/python tools/shadow_replay.py --replay --days 45 --time-stop-min $TS \
    --min-stop-atr 1.5 --min-stop-pct 0.2 --timeout-min 30 \
    --trend-days 7 --trend-neutral 1.5 2>&1 | grep -aE "净 |结局分布|成交 "
  cp logs/shadow_rows.csv logs/shadow_rows_ts${TS}.csv && echo "archived logs/shadow_rows_ts${TS}.csv"
done
echo ALLDONE
