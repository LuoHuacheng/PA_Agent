cd /Users/hc/Documents/ai/PA_Agent
for T in 30 60 120 240; do
  echo "=== timeout-min=$T ==="
  .venv/bin/python tools/shadow_replay.py --replay --days 45 --timeout-min $T \
    --min-stop-atr 1.5 --min-stop-pct 0.2 \
    --trend-days 7 --trend-neutral 1.5 2>&1 | grep -aE "净 |结局分布|成交 "
  cp logs/shadow_rows.csv logs/shadow_rows_to${T}.csv && echo "archived logs/shadow_rows_to${T}.csv"
done
echo ALLDONE
