# Operations (VPS runbook)

## Start / Stop
- ./run.sh
- ./stop.sh
- ./restart.sh

## Health checks
- ps aux | grep -E "alerts.py|paper_exec.py" | grep -v grep
- tail -n 80 run.log
- tail -n 80 paper_exec.log

## Timers
- systemctl --user list-timers --all | grep -E "report|paper"
- journalctl --user -u paper-report.service -n 80 --no-pager

## Common files
- .env (local only, never commit)
- signals.jsonl (runtime)
- paper_trades.csv / paper_trades.jsonl (runtime)
