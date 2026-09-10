#!/bin/bash
# radio_reboot.sh: reinicia la radio completa en un comando.
#
#   ./scripts/radio_reboot.sh           -> operación normal: rota logs,
#                                          descarga música nueva si el catálogo
#                                          está bajo el mínimo, regenera la cola,
#                                          levanta Icecast+liquidsoap+web y verifica.
#   ./scripts/radio_reboot.sh --auto N  -> fuerza además la descarga de N temas
#                                          de Jamendo.
#
# Auto-recuperación: si la cola sale en 0, respalda logs/played.txt, lo limpia
# y reintenta una vez (el historial web se pierde solo en ese caso extremo).
set -euo pipefail
# shellcheck source=scripts/bash_utils.sh
source scripts/bash_utils.sh
cd_project_root
ensure_env

MIN_SONGS="${MIN_SONGS:-20}"
MAX_SONGS="${MAX_SONGS:-200}"
BATCH="${BATCH:-15}"

echo "== radio_reboot =="

# 1) Pre-flight
command -v liquidsoap >/dev/null || { echo "Falta liquidsoap." >&2; exit 1; }
[ -x venv/bin/python ] || { echo "Falta venv/. Corré: python3 -m venv venv" >&2; exit 1; }
[ -f .env ] || { echo "Falta .env con ICE_PASSWORD." >&2; exit 1; }
if ! command -v ffprobe >/dev/null; then
    echo "AVISO: ffprobe no está. Se omite la ingesta local (solo afecta MP3 propios)."
    HAS_FFPROBE=0
else
    HAS_FFPROBE=1
fi

# 2) Rotar logs (evita crecimiento infinito)
cleanup_logs

# 3) Descargar música nueva
if [[ "${1:-}" == "--auto" ]]; then
    N="${2:-10}"
    echo "1) Descargando $N temas nuevos de Jamendo..."
    bash scripts/jamendo.sh --auto "$N" || echo "  (no se pudo/descargó nada, se continúa)"
else
    echo "1) Catálogo activo >= $MIN_SONGS -> saltar descarga; "
    echo "   --auto N para forzar descarga de N temas."
fi

# 4) Ingesta de MP3 locales nuevos (idempotente; requiere ffprobe)
if [ "$HAS_FFPROBE" -eq 1 ]; then
    echo "2) Ingesta de música local (music/ -> songs.json)..."
    ./venv/bin/python scripts/ingest.py --license cc-by-nc || true
else
    echo "2) Ingesta local omitida (falta ffprobe). Caso: sudo apt install -y ffmpeg"
fi

# 5) Regenerar cola (con recuperación si queda en 0)
gen_queue() {
    run_selector clima.json || true
}
count_queue() {
    grep -cE '\.(mp3|flac|ogg|m4a|aac|wav|opus)$' queue.m3u 2>/dev/null || true
}

echo "3) Regenerando cola..."
gen_queue
count=$(count_queue)
if [ "$count" -eq 0 ] && [ -f logs/played.txt ]; then
    ts=$(date +%s)
    cp logs/played.txt "logs/played.backup.$ts.txt"
    echo "  Cola en 0: historial respaldado en logs/played.backup.$ts.txt, reintentando..."
    > logs/played.txt
    gen_queue
    count=$(count_queue)
fi
if [ "$count" -eq 0 ]; then
    echo "  La cola quedó vacía. Revisá songs.json (licencias), music/ y clima.json." >&2
fi

# 6) Levantar servicios
if systemctl is-active --quiet icecast2 2>/dev/null; then
    echo "Icecast: ya corre."
else
    echo "Icecast: arrancando..."
    systemctl start icecast2 2>/dev/null || sudo systemctl start icecast2 2>/dev/null \
        || echo "AVISO: no se pudo iniciar icecast2 (correlo a mano: sudo systemctl start icecast2)."
    sleep 1
fi

PIDFILE="liquidsoap.pid"
if [ -f "$PIDFILE" ]; then
    old_pid=$(cat "$PIDFILE")
    kill -0 "$old_pid" 2>/dev/null && kill "$old_pid" 2>/dev/null || true
    rm -f "$PIDFILE"
    sleep 1
fi
pkill -f web_server.py 2>/dev/null || true

echo "4) Levantando liquidsoap y web..."
liquidsoap radio.liq >> logs/liquidsoap.out 2>&1 < /dev/null &
echo "$!" > "$PIDFILE"
LPID=$!
setsid nohup ./venv/bin/python scripts/web_server.py --host 0.0.0.0 >> logs/web.out 2>&1 < /dev/null &

# 7) Verificar
sleep 5
echo "5) Verificación"
curl -s --max-time 3 -o /dev/null -w "  Stream /radio: %{http_code} %{content_type}\n" http://localhost:8000/radio || echo "  Stream /radio: sin respuesta"
curl -s --max-time 3 -o /dev/null -w "  Web /:       %{http_code}\n" http://localhost:8080/ || echo "  Web /: sin respuesta"
echo "Radio arriba (liquidsoap PID $LPID, cola $count canciones). Web en http://<ip>:8080"