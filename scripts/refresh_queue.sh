#!/bin/bash
# Regenera la cola dinámica (queue.m3u).
# Liquidsoap la recarga automáticamente con reload_mode="watch".
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/bash_utils.sh
source "$SCRIPT_DIR/bash_utils.sh"
cd_project_root
# Verifica el ciclo de vida del catálogo y regenera la cola.
# Si la música ya fue escuchada en su mayoría, descarga un lote nuevo de Jamendo.
exec "$PROJECT_ROOT/scripts/catalog_check.sh"