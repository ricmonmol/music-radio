#!/bin/bash
# refresh_queue.sh — ejecutado por cron cada 30 minutos como backup.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."
[ -f .env ] && { set -a; . ./.env; set +a; } || true
exec ./venv/bin/python scripts/gestor.py >> logs/gestor.log 2>&1
