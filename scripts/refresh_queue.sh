#!/bin/bash
# refresh_queue.sh — ejecutado por cron cada 30 minutos como backup.
# Liquidsoap ya llama a gestor.py en cada track; este cron es un seguro
# por si la radio estuvo pausada o el trigger falló.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."
exec ./venv/bin/python scripts/gestor.py >> logs/gestor.log 2>&1