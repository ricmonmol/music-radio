#!/bin/bash
# reload.sh — punto único para refrescar la radio.
#
#   ./scripts/reload.sh             → solo regenera la cola (liquidsoap la
#                                      recarga solo si tiene reload_mode="watch")
#   ./scripts/reload.sh --fetch N   → baja N temas nuevos de Jamendo y luego
#                                      regenera la cola
#   ./scripts/reload.sh --full      → baja canciones (usa BATCH del .env o 15),
#                                      regenera la cola y reinicia liquidsoap
#                                      + web limpiamente
#
# En todos los modos el web_server recarga songs.json al vuelo (sin reiniciar),
# salvo en --full donde también se reinicia para asegurarse.
set -euo pipefail
# shellcheck source=scripts/bash_utils.sh
source "$(dirname "$0")/bash_utils.sh"
cd_project_root
ensure_env

MODE="${1:-}"
FETCH_N="${2:-${BATCH:-15}}"
PIDFILE="liquidsoap.pid"

# ── helpers ───────────────────────────────────────────────────────────────────

fetch_songs() {
    local n="$1"
    echo "→ Descargando $n temas nuevos de Jamendo..."
    bash scripts/jamendo.sh --auto "$n" || echo "  (sin novedades o error no fatal, se continúa)"
}

ingest_local() {
    if command -v ffprobe >/dev/null 2>&1; then
        echo "→ Ingesta de MP3 locales (music/ → songs.json)..."
        ./venv/bin/python scripts/ingest.py --license cc-by-nc || true
    else
        echo "  (ffprobe ausente, ingesta local omitida)"
    fi
}

gen_queue() {
    echo "→ Regenerando cola..."
    run_selector clima.json || true

    local count
    count=$(grep -cE '\.(mp3|flac|ogg|m4a|aac|wav|opus)$' queue.m3u 2>/dev/null || true)

    # Auto-recuperación: si quedó en 0 y hay historial, limpiar y reintentar una vez
    if [ "${count:-0}" -eq 0 ] && [ -f logs/played.txt ]; then
        local ts; ts=$(date +%s)
        cp logs/played.txt "logs/played.backup.$ts.txt"
        echo "  Cola en 0: historial respaldado → logs/played.backup.$ts.txt, reintentando..."
        > logs/played.txt
        run_selector clima.json || true
        count=$(grep -cE '\.(mp3|flac|ogg|m4a|aac|wav|opus)$' queue.m3u 2>/dev/null || true)
    fi

    if [ "${count:-0}" -eq 0 ]; then
        echo "  ⚠  Cola vacía. Revisá songs.json, music/ y clima.json." >&2
    else
        echo "  Cola: $count canciones"
    fi
}

stop_liquidsoap() {
    if [ -f "$PIDFILE" ]; then
        local pid; pid=$(cat "$PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "→ Deteniendo liquidsoap (PID $pid)..."
            kill "$pid" 2>/dev/null || true
            for _ in $(seq 1 50); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.1
            done
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$PIDFILE"
    fi
    # Matar cualquier huérfano
    pgrep -x liquidsoap 2>/dev/null | while read -r pid; do
        kill "$pid" 2>/dev/null || true
    done
    sleep 0.5
}

start_liquidsoap() {
    echo "→ Iniciando liquidsoap..."
    liquidsoap radio.liq >> logs/liquidsoap.out 2>&1 < /dev/null &
    local lpid=$!
    echo "$lpid" > "$PIDFILE"
    echo "  liquidsoap PID $lpid"
}

stop_web() {
    pkill -f web_server.py 2>/dev/null || true
    sleep 0.5
}

start_web() {
    echo "→ Iniciando web server..."
    setsid nohup ./venv/bin/python scripts/web_server.py --host 0.0.0.0 \
        >> logs/web.out 2>&1 < /dev/null &
    echo "  web server PID $!"
}

ensure_icecast() {
    if systemctl is-active --quiet icecast2 2>/dev/null; then
        echo "  Icecast: ya corre."
    else
        echo "→ Iniciando Icecast..."
        systemctl start icecast2 2>/dev/null \
            || sudo systemctl start icecast2 2>/dev/null \
            || echo "  AVISO: no se pudo iniciar icecast2."
        sleep 1
    fi
}

verify() {
    sleep 5
    echo ""
    echo "── Verificación ──────────────────────────────────────────"
    curl -s --max-time 3 -o /dev/null \
        -w "  Stream /radio : %{http_code} %{content_type}\n" \
        http://localhost:8000/radio || echo "  Stream /radio : sin respuesta"
    curl -s --max-time 3 -o /dev/null \
        -w "  Web /         : %{http_code}\n" \
        http://localhost:8080/ || echo "  Web /: sin respuesta"
    echo "──────────────────────────────────────────────────────────"
}

# ── modos ─────────────────────────────────────────────────────────────────────

case "$MODE" in

  "")
    # Modo ligero: solo regenerar la cola
    echo "== reload: cola =="
    gen_queue
    cleanup_logs
    echo "Listo. Liquidsoap recarga queue.m3u automáticamente."
    ;;

  --fetch)
    # Bajar canciones nuevas + regenerar cola
    echo "== reload: fetch + cola =="
    fetch_songs "$FETCH_N"
    ingest_local
    gen_queue
    cleanup_logs
    echo "Listo. Liquidsoap recarga queue.m3u automáticamente."
    ;;

  --full)
    # Reinicio completo: fetch + cola + reiniciar todo
    echo "== reload: full restart =="
    fetch_songs "$FETCH_N"
    ingest_local
    gen_queue
    cleanup_logs
    ensure_icecast
    stop_liquidsoap
    stop_web
    start_liquidsoap
    start_web
    verify
    echo "Radio arriba. Web en http://<ip>:8080"
    ;;

  *)
    echo "Uso: $0 [--fetch N | --full]" >&2
    echo "  (sin args)    regenera la cola" >&2
    echo "  --fetch N     baja N temas de Jamendo y regenera la cola" >&2
    echo "  --full        baja canciones + reinicia liquidsoap y web" >&2
    exit 1
    ;;

esac
