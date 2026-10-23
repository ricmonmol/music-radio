#!/bin/bash
# gestor_trigger.sh — lanza el ciclo completo del gestor solo si la cola
# online está por debajo del umbral. Se invoca desde radio.liq en cada tema:
# así no arranca un Python completo por canción ni reescribe las playlists
# cuando todavía hay material suficiente.
set -u
cd "$(dirname "$0")/.." || exit 0

QUEUE="queue.m3u"
LOW_WATERMARK=6
COOLDOWN=900
MARK="data/gestor.last_attempt"
GESTOR="scripts/gestor.py"
LOG="logs/gestor.log"
PYTHON="./venv/bin/python"

n=$(grep -cvE '^[[:space:]]*(#|$)' "$QUEUE" 2>/dev/null)
n=${n:-0}
[ "$n" -lt "$LOW_WATERMARK" ] || exit 0

now=$(date +%s)
last=$(stat -c %Y "$MARK" 2>/dev/null || echo 0)
[ $((now - last)) -ge "$COOLDOWN" ] || exit 0

mkdir -p "$(dirname "$MARK")"
touch "$MARK"

[ -x "$PYTHON" ] || exit 0
"$PYTHON" "$GESTOR" >> "$LOG" 2>&1 &
