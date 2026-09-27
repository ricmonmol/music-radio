#!/bin/bash
# radio-jacobs.sh — control de la radio AJC (start / stop / restart / status / logs)
#
# AJC comparte icecast, el venv y el panel web con radio/, pero tiene su propio
# liquidsoap, sus propias colas y su propio gestor. Por eso este script NUNCA
# usa "pkill -x liquidsoap": mataría también el liquidsoap de la radio de
# Jamendo. Solo se toca el PID de liquidsoap.pid, y se verifica que ese PID
# pertenezca de verdad a esta estación (comparando su cwd) antes de matarlo.
#
# El web server NO se toca acá: es compartido y lo levanta radio/scripts/radio.sh.
#
# Uso:
#   ./scripts/radio-jacobs.sh start     levanta liquidsoap (+ gestor en bg)
#   ./scripts/radio-jacobs.sh stop      apaga liquidsoap y el gestor
#   ./scripts/radio-jacobs.sh restart
#   ./scripts/radio-jacobs.sh status
#   ./scripts/radio-jacobs.sh logs
set -euo pipefail
cd "$(dirname "$0")/.."   # siempre desde la raíz de la estación

PIDFILE="liquidsoap.pid"
GESTOR="scripts/ia_gestor.py"
PYTHON="./venv/bin/python"
MOUNT="/jacobs"

# ── helpers ───────────────────────────────────────────────────────────────────

# Devuelve el PID solo si ese proceso es un liquidsoap lanzado desde ESTA
# estación. Si el PID se recicló o el archivo quedó viejo, no se mata nada.
_own_liquidsoap_pid() {
    [ -f "$PIDFILE" ] || return 0
    local pid
    pid=$(cat "$PIDFILE" 2>/dev/null || true)
    [ -n "$pid" ] || return 0
    kill -0 "$pid" 2>/dev/null || return 0
    # /proc/<pid>/cwd debe ser la raíz de esta estación
    local cwd
    cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
    if [ "$cwd" != "$(pwd -P)" ]; then
        echo "  AVISO: el PID $pid no corre desde $(pwd -P) sino desde ${cwd:-¿?}; no se toca."
        return 0
    fi
    echo "$pid"
}

_own_gestor_pids() {
    # pgrep -f acotado al path del gestor de esta estación: "ia_gestor.py"
    # no matchea "gestor.py" de la otra radio.
    pgrep -f "python.*${GESTOR}" 2>/dev/null || true
}

_stop() {
    local pid
    pid=$(_own_liquidsoap_pid)
    if [ -n "$pid" ]; then
        echo "→ Deteniendo liquidsoap (PID $pid)..."
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"

    # Gestor (descarga en curso) — solo el de esta estación
    local gpid
    for gpid in $(_own_gestor_pids); do
        echo "→ Deteniendo gestor (PID $gpid)..."
        kill "$gpid" 2>/dev/null || true
    done
    sleep 0.5
}

_start() {
    # Icecast es compartido: si ya corre, no se toca.
    if ! systemctl is-active --quiet icecast2 2>/dev/null; then
        echo "→ Arrancando icecast2..."
        systemctl start icecast2 2>/dev/null \
            || sudo systemctl start icecast2 2>/dev/null \
            || echo "  AVISO: arrancá icecast a mano: sudo systemctl start icecast2"
        sleep 1
    fi

    [ -f .env ] && { set -a; . ./.env; set +a; } || true
    mkdir -p logs data music archive/music

    if [ ! -f queue.m3u ] || [ ! -s queue.m3u ]; then
        printf '#EXTM3U\n' > queue.m3u
    fi
    if [ ! -f queue.offline.m3u ] || [ ! -s queue.offline.m3u ]; then
        printf '#EXTM3U\n' > queue.offline.m3u
    fi

    echo "→ Arrancando liquidsoap..."
    setsid nohup liquidsoap radio.liq >> logs/liquidsoap.out 2>&1 < /dev/null &
    echo "$!" > "$PIDFILE"
    echo "  liquidsoap PID $! (mount $MOUNT)"

    # La renovación de colas sigue en segundo plano: el stream ya está en vivo
    # y la playlist se recarga sola cuando el gestor termina de escribir.
    echo "→ Renovando colas en segundo plano..."
    setsid nohup $PYTHON "$GESTOR" >> logs/gestor.log 2>&1 < /dev/null &

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
    _check "http://localhost:8000$MOUNT" "Stream"
    _check "http://localhost:8080/" "Panel "
}

# ── comandos ──────────────────────────────────────────────────────────────────

case "${1:-}" in

  start)
    echo "== jacobs collection: start =="
    _start
    ;;

  stop)
    echo "== jacobs collection: stop =="
    _stop
    echo "Detenido."
    ;;

  restart)
    echo "== jacobs collection: restart =="
    _stop
    _start
    ;;

  status)
    echo "== jacobs collection: status =="
    liq="inactivo"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        liq="corriendo (PID $(cat "$PIDFILE"))"
    fi
    gestor="inactivo"
    gpid=$(_own_gestor_pids | head -1)
    [ -n "$gpid" ] && gestor="corriendo (PID $gpid)"
    ice=$(systemctl is-active icecast2 2>/dev/null || echo "desconocido")

    echo "  Icecast    : $ice"
    echo "  Liquidsoap : $liq"
    echo "  Gestor     : $gestor"
    echo "  Mount      : $MOUNT"
    echo "  Panel      : http://localhost:8080/ (compartido)"
    echo ""
    $PYTHON scripts/listeners.py --all
    echo ""
    $PYTHON "$GESTOR" --status
    ;;

  logs)
    tail -f logs/gestor.log logs/liquidsoap.out 2>/dev/null
    ;;

  *)
    echo "Uso: $0 {start|stop|restart|status|logs}" >&2
    exit 1
    ;;

esac
