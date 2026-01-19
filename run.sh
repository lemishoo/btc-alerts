#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$APP_DIR/alerts.pid"
LOGFILE="$APP_DIR/run.log"
PY="$APP_DIR/.venv/bin/python"
SCRIPT="$APP_DIR/alerts.py"

if [[ ! -x "$PY" ]]; then
  echo "ERROR: Python venv not found at $PY"
  echo "Activate venv and install deps first."
  exit 1
fi

# If already running, do nothing (blbovzdorne)
if [[ -f "$PIDFILE" ]]; then
  PID="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
    echo "Already running (PID $PID)."
    exit 0
  fi
fi

# Extra safety: kill any stray duplicates started manually
pkill -f "alerts.py" >/dev/null 2>&1 || true

# Start fresh
echo "Starting alerts.py..."

# Load .env so TELEGRAM_* and other vars are available to nohup process
if [[ -f "$APP_DIR/.env" ]]; then
  set -a
  source "$APP_DIR/.env"
  set +a
fi

nohup "$PY" "$SCRIPT" >> "$LOGFILE" 2>&1 &
NEWPID=$!
echo "$NEWPID" > "$PIDFILE"

# Quick health check
sleep 0.4
if kill -0 "$NEWPID" 2>/dev/null; then
  echo "Started (PID $NEWPID)."
  exit 0
else
  echo "FAILED to start. Showing last 50 log lines:"
  tail -n 50 "$LOGFILE" || true
  exit 1
fi
