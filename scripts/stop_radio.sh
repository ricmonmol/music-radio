#!/bin/bash
# Detiene la radio: Liquidsoap (via PID file + fallback pgrep) y el panel web.
set -e
cd "$(dirname "$0")/.."

PIDFILE="liquidsoap.pid"

if [ -f "$PIDFILE" ]; then
    pid=$(cat "$PIDFILE")
    kill -0 "$pid" 2>/dev/null && kill "$pid" 2>/dev/null || true
    rm -f "$PIDFILE"
fi

pgrep -x liquidsoap | while read -r pid; do
    kill "$pid" 2>/dev/null || true
done

pkill -f web_server.py
echo "Radio detenida."