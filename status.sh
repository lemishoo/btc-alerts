#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$APP_DIR/alerts.pid"

if [[ -f "$PIDFILE" ]]; then
  PID="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
    echo "RUNNING (PID $PID)"
    exit 0
  fi
fi

# Fallback: search process
if pgrep -f "alerts.py" >/dev/null 2>&1; then
  echo "RUNNING (pidfile missing, but process exists):"
  pgrep -af "alerts.py" || true
  exit 0
fi

echo "NOT RUNNING"
exit 1
