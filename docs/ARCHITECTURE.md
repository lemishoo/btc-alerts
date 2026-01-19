# Architecture

## Components
- alerts.py (scanner + telegram + regime)
- paper_exec.py (paper execution + logging)
- daily_paper_report.py (daily summary + report link)
- systemd timer/service (paper-report.timer/service)

## Data files (runtime)
- run.log, paper_exec.log
- signals.jsonl
- paper_trades.csv, paper_trades.jsonl
- state.json, paper_state.json

## Config sources
- .env (local secrets + thresholds)
- config.yml (static params, if used)
