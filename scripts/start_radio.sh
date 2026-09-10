#!/bin/bash
# Arranca la radio: genera la cola inicial y lanza Liquidsoap y el panel web
# desacoplados.
set -e
cd "$(dirname "$0")/.."
set -a
. ./.env
set +a

PIDFILE="liquidsoap.pid"

# Detener instancias previas de forma segura
if [ -f "$PIDFILE" ]; then
    old_pid=$(cat "$PIDFILE")
    kill -0 "$old_pid" 2>/dev/null && kill "$old_pid" 2>/dev/null || true
    rm -f "$PIDFILE"
    sleep 1
fi

./venv/bin/python scripts/selector.py --clima clima.json || true
liquidsoap radio.liq >> logs/liquidsoap.out 2>&1 < /dev/null &
echo "$!" > "$PIDFILE"
LPID=$!
setsid nohup ./venv/bin/python scripts/web_server.py --host 0.0.0.0 >> logs/web.out 2>&1 < /dev/null &
WPID=$!
echo "Liquidsoap lanzado (PID $LPID). Stream en http://localhost:8000/radio"
echo "Web/API lanzada (PID $WPID). Interfaz en http://localhost:8080"