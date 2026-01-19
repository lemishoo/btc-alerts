#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== Processes =="
ps aux | grep -E "alerts.py|paper_exec.py|daily_paper_report.py" | grep -v grep || true

echo
echo "== Timers =="
systemctl --user list-timers --all | grep -E "paper|report" || true

echo
echo "== Last run.log =="
tail -n 40 "$DIR/run.log" 2>/dev/null || echo "no run.log"

echo
echo "== Last paper_exec.log =="
tail -n 40 "$DIR/paper_exec.log" 2>/dev/null || echo "no paper_exec.log"

echo
echo "== Files =="
ls -la "$DIR" | egrep "alerts.py|paper_exec.py|signals.jsonl|paper_trades.csv|paper_state.json|state.json|run.log|paper_exec.log" || true
