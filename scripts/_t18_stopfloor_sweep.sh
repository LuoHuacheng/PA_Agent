cd /Users/hc/Documents/ai/PA_Agent
for A in 0.7 1.0 2.0; do
  echo "=== min-stop-atr=$A ==="
  .venv/bin/python tools/shadow_replay.py --replay --days 45 --timeout-min 30 \
    --min-stop-atr $A --min-stop-pct 0.2 \
    --trend-days 7 --trend-neutral 1.5 2>&1 | grep -aE "净 |结局分布|成交 "
  cp logs/shadow_rows.csv logs/shadow_rows_atr${A}.csv && echo "archived logs/shadow_rows_atr${A}.csv"
done
echo ALLDONE
