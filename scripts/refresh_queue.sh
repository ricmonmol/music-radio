#!/bin/bash
# Regenera la cola dinámica (queue.m3u).
# Liquidsoap la recarga automáticamente con reload_mode="watch".
set -e
cd /home/ricardo/Descargas/radio/radio
./venv/bin/python scripts/selector.py --clima clima.json