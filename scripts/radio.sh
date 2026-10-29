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
GESTOR="scripts/gestor.py"

# ── helpers ───────────────────────────────────────────────────────────────────

# Devuelve el PID solo si ese proceso es un liquidsoap lanzado desde ESTA
# estación. Sin esta comprobación, "pkill -x liquidsoap" mataba también el
# liquidsoap de radio-jacobs/ (la Aadam Jacobs Collection), que comparte
# icecast y panel pero no catálogo. Comparar el cwd evita cruzar las radios.
_own_liquidsoap_pid() {
    [ -f "$PIDFILE" ] || return 0
    local pid
    pid=$(cat "$PIDFILE" 2>/dev/null || true)
    [ -n "$pid" ] || return 0
    kill -0 "$pid" 2>/dev/null || return 0
    local cwd
    cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
    if [ "$cwd" != "$(pwd -P)" ]; then
        echo "  AVISO: el PID $pid no corre desde $(pwd -P) sino desde ${cwd:-¿?}; no se toca."
        return 0
    fi
    echo "$pid"
}

_own_gestor_pids() {
    # "gestor.py" también matchearía "ia_gestor.py" de la otra estación si no
    # se ancla al path completo: se exige "python.../scripts/gestor.py".
    pgrep -f "python.*/${GESTOR}\$" 2>/dev/null || true
}

_stop() {
    # Liquidsoap (solo el de esta estación)
    local pid
    pid=$(_own_liquidsoap_pid)
    if [ -n "$pid" ]; then
        echo "→ Deteniendo liquidsoap (PID $pid)..."
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
    # Web server (compartido: sirve las dos estaciones, por eso se para desde acá)
    pkill -f "python.*scripts/web_server.py" 2>/dev/null || true
    # Gestor (descarga en curso) — solo el de esta estación
    local gpid
    for gpid in $(_own_gestor_pids); do
        kill "$gpid" 2>/dev/null || true
    done
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
    setsid nohup liquidsoap radio.liq >> logs/liquidsoap.out 2>&1 < /dev/null &
    echo "$!" > "$PIDFILE"
    echo "  liquidsoap PID $!"

    # El web server es compartido entre estaciones: si ya está corriendo
    # (otra estación lo levantó), no arrancar un segundo -> "Address in use".
    if pgrep -f "python.*scripts/web_server.py" > /dev/null 2>&1; then
        echo "→ Web server ya está corriendo, no se relanza"
    else
        echo "→ Arrancando web server..."
        setsid nohup $PYTHON scripts/web_server.py --host 0.0.0.0 \
            >> logs/web.out 2>&1 < /dev/null &
        echo "  web server PID $!"
    fi

    # La renovación de colas sigue en segundo plano: el stream ya está en vivo
    # y la playlist se recarga sola cuando el gestor termina de escribir.
    echo "→ Renovando colas en segundo plano..."
    setsid nohup $PYTHON scripts/gestor.py >> logs/gestor.log 2>&1 < /dev/null &

    # Verificación
    sleep 4
    echo ""
    # Un stream de audio no termina nunca, así que "curl --max-time" sale con
    # código 28 aunque haya AUDIENDO bien. Por eso no se encadena con "||": se
    # lee el http_code que curl igualmente imprime y se decide con ese valor.
    _check() {
        local url="$1" label="$2" out code
        out=$(curl -s --max-time 3 -o /dev/null -w "%{http_code} %{content_type}" "$url" 2>/dev/null || true)
        code="${out%% *}"
        if [ "$code" = "200" ]; then
            echo "  $label : $url  →  $out"
        else
            echo "  $label : sin respuesta aún (liquidsoap puede tardar ~10 s)"
        fi
    }
    _check "http://localhost:8000/radio" "Stream"
    _check "http://localhost:8080/" "Panel "
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
    $PYTHON scripts/listeners.py --all
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
