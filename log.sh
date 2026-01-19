#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGFILE="$APP_DIR/run.log"
touch "$LOGFILE"
tail -n 200 -f "$LOGFILE"
