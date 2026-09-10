#!/bin/bash
# Arranca la radio: genera la cola inicial y lanza Liquidsoap y el panel web
# desacoplados.
set -e
# shellcheck source=scripts/bash_utils.sh
source scripts/bash_utils.sh
cd_project_root
ensure_env

PIDFILE="liquidsoap.pid"

# Detener instancias previas de forma segura
if [ -f "$PIDFILE" ]; then
    old_pid=$(cat "$PIDFILE")
    if kill -0 "$old_pid" 2>/dev/null; then
        kill "$old_pid" 2>/dev/null || true
        for _ in $(seq 1 30); do
            kill -0 "$old_pid" 2>/dev/null || break
            sleep 0.1
        done
        kill -0 "$old_pid" 2>/dev/null && kill -9 "$old_pid" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
    sleep 0.5
fi

cleanup_logs

run_selector clima.json || true
liquidsoap radio.liq >> logs/liquidsoap.out 2>&1 < /dev/null &
echo "$!" > "$PIDFILE"
LPID=$!
setsid nohup ./venv/bin/python scripts/web_server.py --host 0.0.0.0 >> logs/web.out 2>&1 < /dev/null &
WPID=$!
echo "Liquidsoap lanzado (PID $LPID). Stream en http://localhost:8000/radio"
echo "Web/API lanzada (PID $WPID). Interfaz en http://localhost:8080"