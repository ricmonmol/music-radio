#!/bin/bash
# radio.sh — control de la radio (start / stop / restart / status / logs)
#
# Uso:
#   ./scripts/radio.sh start    levanta icecast + liquidsoap + web
#   ./scripts/radio.sh stop     apaga liquidsoap + web (icecast queda corriendo)
#   ./scripts/radio.sh restart  stop + start
#   ./scripts/radio.sh status   muestra si cada servicio responde
#   ./scripts/radio.sh logs     tail -f de los logs principales
set -euo pipefail
cd "$(dirname "$0")/.."   # siempre desde la raíz del proyecto

PIDFILE="liquidsoap.pid"
PYTHON="./venv/bin/python"

# ── helpers ───────────────────────────────────────────────────────────────────

_stop() {
    # Liquidsoap
    if [ -f "$PIDFILE" ]; then
        pid=$(cat "$PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "→ Deteniendo liquidsoap (PID $pid)..."
            kill "$pid" 2>/dev/null || true
            for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$PIDFILE"
    fi
    pkill -x liquidsoap 2>/dev/null || true
    # Web server
    pkill -f web_server.py 2>/dev/null || true
    # Gestor (descarga en curso)
    pkill -f "scripts/gestor.py" 2>/dev/null || true
    sleep 0.5
}

_start() {
    # Icecast (idempotente)
    if ! systemctl is-active --quiet icecast2 2>/dev/null; then
        echo "→ Arrancando icecast2..."
        systemctl start icecast2 2>/dev/null \
            || sudo systemctl start icecast2 2>/dev/null \
            || echo "  AVISO: arrancá icecast a mano: sudo systemctl start icecast2"
        sleep 1
    fi

    [ -f .env ] && { set -a; . ./.env; set +a; } || true
    mkdir -p logs data

    if [ ! -f queue.m3u ] || [ ! -s queue.m3u ]; then
        printf '#EXTM3U\n' > queue.m3u
    fi
    if [ ! -f queue.offline.m3u ] || [ ! -s queue.offline.m3u ]; then
        printf '#EXTM3U\n' > queue.offline.m3u
    fi

    echo "→ Arrancando liquidsoap..."
    liquidsoap radio.liq >> logs/liquidsoap.out 2>&1 < /dev/null &
    echo "$!" > "$PIDFILE"
    echo "  liquidsoap PID $!"

    echo "→ Arrancando web server..."
    setsid nohup $PYTHON scripts/web_server.py --host 0.0.0.0 \
        >> logs/web.out 2>&1 < /dev/null &
    echo "  web server PID $!"

    # La renovación de colas sigue en segundo plano: el stream ya está en vivo
    # y la playlist se recarga sola cuando el gestor termina de escribir.
    echo "→ Renovando colas en segundo plano..."
    setsid nohup $PYTHON scripts/gestor.py >> logs/gestor.log 2>&1 < /dev/null &

    # Verificación
    sleep 4
    echo ""
    curl -s --max-time 3 -o /dev/null \
        -w "  Stream : http://localhost:8000/radio  →  %{http_code} %{content_type}\n" \
        http://localhost:8000/radio 2>/dev/null \
        || echo "  Stream : sin respuesta aún (liquidsoap puede tardar ~10 s)"
    curl -s --max-time 3 -o /dev/null \
        -w "  Web    : http://localhost:8080        →  %{http_code}\n" \
        http://localhost:8080/ 2>/dev/null \
        || echo "  Web    : sin respuesta"
}

# ── comandos ──────────────────────────────────────────────────────────────────

case "${1:-}" in

  start)
    echo "== radio: start =="
    _start
    ;;

  stop)
    echo "== radio: stop =="
    _stop
    echo "Detenido."
    ;;

  restart)
    echo "== radio: restart =="
    _stop
    _start
    ;;

  status)
    echo "== radio: status =="
    liq="inactivo"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        liq="corriendo (PID $(cat "$PIDFILE"))"
    fi
    web="inactivo"
    wpid=$(pgrep -f web_server.py 2>/dev/null | head -1 || true)
    [ -n "$wpid" ] && web="corriendo (PID $wpid)"
    ice=$(systemctl is-active icecast2 2>/dev/null || echo "desconocido")

    echo "  Icecast    : $ice"
    echo "  Liquidsoap : $liq"
    echo "  Web server : $web"
    echo ""
    $PYTHON scripts/gestor.py --status
    ;;

  logs)
    tail -f logs/gestor.log logs/liquidsoap.out logs/web.out 2>/dev/null
    ;;

  *)
    echo "Uso: $0 {start|stop|restart|status|logs}" >&2
    exit 1
    ;;

esac
