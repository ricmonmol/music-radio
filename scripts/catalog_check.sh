#!/bin/bash
# catalog_check.sh: wrapper para cron. Refresca/rota el catálogo según
# thresholds. Agregar al cron, p. ej. cada 6 horas:
#   0 */6 * * * /home/ricardo/Descargas/radio/radio/scripts/catalog_check.sh
exec /home/ricardo/Descargas/radio/radio/venv/bin/python \
     /home/ricardo/Descargas/radio/radio/scripts/catalog_manager.py \
     auto --min-songs 30 --max-songs 200