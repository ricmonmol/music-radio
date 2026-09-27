#!/bin/bash
# reset.sh — mata todo y levanta ambas radios desde cero.
#
# Uso:
#   ./reset.sh          reinicio completo
#   ./reset.sh stop     solo detener todo
#   ./reset.sh start    solo arrancar (asume que ya están parados)
set -euo pipefail
cd "$(dirname "$0")"   # siempre desde la raíz del repo

# ── stop ─────────────────────────────────────────────────────────────────────

_stop_all() {
    echo "→ Deteniendo liquidsoap (todos los procesos)..."
    pkill -x liquidsoap 2>/dev/null || true
    sleep 2
    # Por si alguno quedó colgado
    pkill -9 -x liquidsoap 2>/dev/null || true

    echo "→ Deteniendo web server..."
    pkill -f "web_server.py" 2>/dev/null || true

    echo "→ Deteniendo gestores en segundo plano..."
    pkill -f "gestor.py" 2>/dev/null || true
    pkill -f "ia_gestor.py" 2>/dev/null || true

    sleep 1
    echo "  Procesos detenidos."
}

# ── start ─────────────────────────────────────────────────────────────────────

_start_all() {
    # Symlink del venv de jacobs: siempre se recrea para que apunte al correcto
    # aunque el repo se haya movido de carpeta.
    echo "→ Enlazando venv de radio-jacobs..."
    rm -f radio-jacobs/venv
    ln -s "$(pwd)/venv" radio-jacobs/venv
    echo "  $(radio-jacobs/venv/bin/python --version)"

    echo "→ Arrancando radio principal (Jamendo)..."
    ./scripts/radio.sh start

    echo "→ Arrancando radio-jacobs (Aadam Jacobs Collection)..."
    cd radio-jacobs
    ./scripts/radio-jacobs.sh start
    cd ..

    echo ""
    echo "✓ Listo. Streams:"
    echo "  Jamendo  → http://localhost:8000/radio"
    echo "  Jacobs   → http://localhost:8000/jacobs"
    echo "  Panel    → http://localhost:8080"
}

# ── main ─────────────────────────────────────────────────────────────────────

case "${1:-reset}" in
    stop)
        _stop_all
        ;;
    start)
        _start_all
        ;;
    reset|restart)
        echo "== reset: deteniendo todo =="
        _stop_all
        echo ""
        echo "== reset: arrancando todo =="
        _start_all
        ;;
    status)
        echo "== status =="
        echo ""
        echo "── procesos ─────────────────────────────"
        ps aux | grep -E "liquidsoap|web_server|ia_gestor|gestor" | grep -v grep || echo "  (ninguno)"
        echo ""
        echo "── streams ──────────────────────────────"
        for mount in /radio /jacobs; do
            code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "http://localhost:8000${mount}" 2>/dev/null || echo "000")
            echo "  http://localhost:8000${mount}  →  ${code}"
        done
        echo "  http://localhost:8080  →  $(curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://localhost:8080/ 2>/dev/null || echo "000")"
        echo ""
        echo "── oyentes ──────────────────────────────"
        ./venv/bin/python scripts/listeners.py --all 2>/dev/null || echo "  (icecast no responde)"
        echo ""
        echo "── sonando ahora ────────────────────────"
        echo "  jamendo : $(cat logs/nowplaying.txt 2>/dev/null | cut -d'|' -f2 || echo '(nada)')"
        echo "  jacobs  : $(cat radio-jacobs/logs/nowplaying.txt 2>/dev/null | cut -d'|' -f2 || echo '(nada)')"
        echo ""
        echo "── cola jacobs ──────────────────────────"
        grep -c "^/" radio-jacobs/queue.m3u 2>/dev/null && echo " pistas en queue.m3u" || echo "  (cola vacía)"
        echo ""
        echo "── último log jacobs ────────────────────"
        tail -5 radio-jacobs/logs/liquidsoap.out 2>/dev/null || echo "  (sin log)"
        ;;
    *)
        echo "Uso: $0 {reset|start|stop|status}" >&2
        exit 1
        ;;
esac
