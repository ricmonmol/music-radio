#!/bin/bash
# radio_up.sh: levanta todo lo necesario para escuchar la radio
# (Icecast -> cola -> liquidsoap -> web) y verifica que respondan.
set -e
cd "$(dirname "$0")/.."
set -a
. ./.env
set +a

PIDFILE="liquidsoap.pid"

# 1. Icecast (debe correr antes que liquidsoap; idempotente)
if systemctl is-active --quiet icecast2 2>/dev/null; then
    echo "Icecast: ya corre."
else
    echo "Icecast: arrancando..."
    systemctl start icecast2 2>/dev/null || sudo systemctl start icecast2 2>/dev/null \
        || echo "AVISO: no se pudo iniciar icecast2 (correlo a mano: sudo systemctl start icecast2)."
    sleep 1
fi

# 2. Apagar instancias previas
if [ -f "$PIDFILE" ]; then
    old_pid=$(cat "$PIDFILE")
    kill -0 "$old_pid" 2>/dev/null && kill "$old_pid" 2>/dev/null || true
    rm -f "$PIDFILE"
    sleep 1
fi
pkill -f web_server.py 2>/dev/null || true

# 3. Cola + liquidsoap + web
./venv/bin/python scripts/selector.py --clima clima.json || true
liquidsoap radio.liq >> logs/liquidsoap.out 2>&1 < /dev/null &
echo "$!" > "$PIDFILE"
LPID=$!
setsid nohup ./venv/bin/python scripts/web_server.py --host 0.0.0.0 >> logs/web.out 2>&1 < /dev/null &

# 4. Verificar
sleep 3
echo "-- Verificación --"
curl -s --max-time 3 -o /dev/null -w "Stream /radio: %{http_code} %{content_type}\n" http://localhost:8000/radio || echo "Stream /radio: sin respuesta"
curl -s --max-time 3 -o /dev/null -w "Web /:       %{http_code}\n" http://localhost:8080/ || echo "Web /: sin respuesta"
echo "Radio arriba (liquidsoap PID $LPID). Web en http://<ip>:8080"