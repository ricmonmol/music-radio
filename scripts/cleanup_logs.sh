#!/bin/bash
# Rota/trunca los logs para evitar crecimiento ilimitado.
set -euo pipefail
cd /home/ricardo/Descargas/radio/radio

MAX_LINES=2000
LOG_DIR="logs"

# Truncar played.txt a las últimas MAX_LINES entradas
if [ -f "$LOG_DIR/played.txt" ]; then
    tail -n "$MAX_LINES" "$LOG_DIR/played.txt" > "$LOG_DIR/played.txt.tmp"
    mv "$LOG_DIR/played.txt.tmp" "$LOG_DIR/played.txt"
fi

# Rotar logs grandes: liquidsoap.out, select.log, web.out, catalog.log
for f in liquidsoap.out select.log web.out catalog.log; do
    path="$LOG_DIR/$f"
    if [ -f "$path" ]; then
        size_kb=$(( $(stat -c%s "$path") / 1024 ))
        if [ "$size_kb" -gt 1000 ]; then  # >1 MB
            cp "$path" "$path.old"
            > "$path"
            echo "$(date -Is) Rotado $f ($size_kb KB) -> $f.old" >> "$LOG_DIR/rotation.log"
        fi
    fi
done