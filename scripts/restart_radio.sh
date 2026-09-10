#!/bin/bash
# Reinicia Liquidsoap de forma segura usando PID file.
# Ya no es necesario para el ciclo normal (reload_mode="watch" en radio.liq
# recarga queue.m3u automáticamente). Útil solo para cambios de configuración.
set -e
# shellcheck source=scripts/bash_utils.sh
source scripts/bash_utils.sh
cd_project_root
ensure_env

PIDFILE="liquidsoap.pid"

stop_liquidsoap() {
    if [ -f "$PIDFILE" ]; then
        local pid
        pid=$(cat "$PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "Deteniendo Liquidsoap (PID $pid)..."
            kill "$pid"
            # Esperar a que termine (máx 5 segundos)
            for i in $(seq 1 50); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.1
            done
            # Si aún vive, forzar
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$PIDFILE"
    fi
    # Limpieza: matar huérfanos por nombre exacto (no pkill -f)
    pgrep -x liquidsoap | while read -r pid; do
        kill "$pid" 2>/dev/null || true
    done
    sleep 1
}

start_liquidsoap() {
    echo "Iniciando Liquidsoap..."
    liquidsoap radio.liq >> logs/liquidsoap.out 2>&1 < /dev/null &
    local pid=$!
    echo "$pid" > "$PIDFILE"
    echo "Liquidsoap iniciado (PID $pid)."
}

stop_liquidsoap
start_liquidsoap