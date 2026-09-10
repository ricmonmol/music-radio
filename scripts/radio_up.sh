#!/bin/bash
# radio_up.sh: levanta todo lo necesario para escuchar la radio
# (Icecast -> cola -> liquidsoap -> web) y verifica que respondan.
set -e
# shellcheck source=scripts/bash_utils.sh
source scripts/bash_utils.sh
cd_project_root
ensure_env

PIDFILE="liquidsoap.pid"

# 1. Rotar logs (evita crecimiento infinito)
cleanup_logs

# 2. Icecast (debe correr antes que liquidsoap; idempotente)
if systemctl is-active --quiet icecast2 2>/dev/null; then
    echo "Icecast: ya corre."
else
    echo "Icecast: arrancando..."
    systemctl start icecast2 2>/dev/null || sudo systemctl start icecast2 2>/dev/null \
        || echo "AVISO: no se pudo iniciar icecast2 (correlo a mano: sudo systemctl start icecast2)."
    sleep 1
fi

# 3. Apagar instancias previas
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
pkill -f web_server.py 2>/dev/null || true

# 4. Cola + liquidsoap + web
run_selector clima.json || true
liquidsoap radio.liq >> logs/liquidsoap.out 2>&1 < /dev/null &
echo "$!" > "$PIDFILE"
LPID=$!
setsid nohup ./venv/bin/python scripts/web_server.py --host 0.0.0.0 >> logs/web.out 2>&1 < /dev/null &

# 5. Verificar
sleep 5
echo "-- Verificación --"
curl -s --max-time 3 -o /dev/null -w "Stream /radio: %{http_code} %{content_type}\n" http://localhost:8000/radio || echo "Stream /radio: sin respuesta"
curl -s --max-time 3 -o /dev/null -w "Web /:       %{http_code}\n" http://localhost:8080/ || echo "Web /: sin respuesta"
echo "Radio arriba (liquidsoap PID $LPID). Web en http://<ip>:8080"