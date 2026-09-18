cd /Users/hc/Documents/ai/PA_Agent
run() {
  label=$1; shift
  echo "=== $label ==="
  .venv/bin/python tools/shadow_replay.py "$@" 2>&1 | grep -aE "净 |结局分布|成交 |收益|均 " | head -6
  if cp logs/shadow_rows.csv "logs/shadow_rows_${label}.csv" 2>/dev/null; then
    echo "archived logs/shadow_rows_${label}.csv"
  else
    echo "no csv for ${label}"
  fi
}
run stopatr25 --replay --days 45 --min-stop-atr 2.5 --min-stop-pct 0.2 --timeout-min 30 --trend-days 7 --trend-neutral 1.5
run partial0 --replay --days 45 --min-stop-atr 1.5 --min-stop-pct 0.2 --timeout-min 30 --partial-pct 0 --trend-days 7 --trend-neutral 1.5
run partial70 --replay --days 45 --min-stop-atr 1.5 --min-stop-pct 0.2 --timeout-min 30 --partial-pct 70 --trend-days 7 --trend-neutral 1.5
run baselineoff --baseline --days 45 --baseline-be off --trend-days 7 --trend-neutral 1.5
run baseline05r --baseline --days 45 --baseline-be 0.5r --trend-days 7 --trend-neutral 1.5
echo ALLDONE
