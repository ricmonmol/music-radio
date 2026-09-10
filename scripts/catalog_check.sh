#!/bin/bash
# catalog_check.sh: wrapper para cron. Refresca/rota el catálogo según
# thresholds. Agregar al cron, p. ej. cada 6 horas:
#   0 */6 * * * $(dirname /ruta/al/script/catalog_check.sh)
set -euo pipefail
# shellcheck source=scripts/bash_utils.sh
source "$(dirname "$0")/bash_utils.sh"
cd_project_root
ensure_env 2>/dev/null || true

exec "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/scripts/catalog_manager.py" \
     auto --min-songs 30 --max-songs 200 --batch-size 40 --played-threshold 0.80 --unplayed-min 8