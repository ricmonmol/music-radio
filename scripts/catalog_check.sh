#!/bin/bash
# catalog_check.sh: wrapper para cron. Refresca/rota el catálogo según
# thresholds. Agregar al cron, p. ej. cada 6 horas:
#   0 */6 * * * $(dirname /ruta/al/script/catalog_check.sh)
root="$(cd "$(dirname "$0")/.." && pwd)"
exec "$root/venv/bin/python" "$root/scripts/catalog_manager.py" \
     auto --min-songs 30 --max-songs 200