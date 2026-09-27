#!/bin/bash
# listeners.sh — cuantos oyentes hay ahora mismo en cada mount de Icecast.
#
#   ./scripts/listeners.sh              estado actual de todos los mounts
#   ./scripts/listeners.sh -w [seg]     refresca en vivo (por defecto 5 s)
#
# El historial queda en logs/listeners.log (una linea cada 5 min, lo escribe
# el gate de oyentes de radio.liq). Para consultarlo:
#
#   tail -f logs/listeners.log
#   grep '> 0' logs/listeners.log | tail -20
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
PY=./venv/bin/python

usage() {
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
}

case "${1:-}" in
    -h|--help)
        usage
        ;;
    -w|--watch)
        cada="${2:-5}"
        case "$cada" in
            ''|*[!0-9]*) echo "intervalo invalido: $cada" >&2; exit 1 ;;
        esac
        trap 'echo; exit 0' INT
        while true; do
            "$PY" scripts/listeners.py --stamp
            sleep "$cada"
        done
        ;;
    '')
        "$PY" scripts/listeners.py --all
        ;;
    *)
        echo "opcion desconocida: $1" >&2
        usage >&2
        exit 1
        ;;
esac
