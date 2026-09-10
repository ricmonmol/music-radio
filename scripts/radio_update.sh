#!/bin/bash
# radio_update.sh: actualiza el catálogo de canciones y regenera la cola.
#
#   ./scripts/radio_update.sh          -> ingesta de MP3 locales nuevos en music/
#   ./scripts/radio_update.sh --auto N -> además baja N temas nuevos de Jamendo
set -e
# shellcheck source=scripts/bash_utils.sh
source scripts/bash_utils.sh
cd_project_root

echo "1) Ingesta de música local (music/ -> songs.json)..."
./venv/bin/python scripts/ingest.py --license cc-by-nc || true

if [[ "${1:-}" == "--auto" ]]; then
    N="${2:-10}"
    echo "2) Bajando $N temas nuevos de Jamendo..."
    bash scripts/jamendo.sh --auto "$N" || true
else
    echo "2) (omitido: pasá --auto N para bajar de Jamendo)"
fi

echo "3) Regenerando cola (queue.m3u)..."
run_selector clima.json || true
cleanup_logs
echo "Catálogo actualizado. Liquidsoap recarga queue.m3u automáticamente."