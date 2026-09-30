#!/bin/bash
# indice.sh — lista los scripts del proyecto con su descripción y su modo de uso.
#
#   ./indice.sh              lista todos agrupados por estación
#   ./indice.sh visits       detalle de un script
#   ./indice.sh -h           esta ayuda
#
# No mantiene una lista propia: lee la cabecera de cada script y extrae la
# primera línea (que por convención es "nombre — descripción") y el bloque
# "Uso:" si está. Así el índice no se desactualiza nunca: si agregás un script
# nuevo, aparece solo, y si le cambiás la descripción, sale la nueva.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

# ── dónde buscar ─────────────────────────────────────────────────────────────
# Excluye venv, __pycache__ y los .bak que deja el gestor. El glob de radio-
# jacobs incluye su venv, que es un symlink al de arriba.

scripts_de() {
    find . \( -name venv -o -name __pycache__ -o -name '*.bak.*' \) -prune -o \
        \( -name '*.sh' -o -name '*.py' \) -print 2>/dev/null \
        | grep -v '/venv/' | sort
}

# ── cabecera ─────────────────────────────────────────────────────────────────

# Primera línea con texto tras el shebang: quita "#", """ y comillas sueltas.
primera_linea() {
    sed -n '2p' "$1" \
        | sed -e 's/^#\{1,\} \{0,1\}//' -e 's/^"""//' -e 's/"""$//' \
        | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

# "gestor.py — ciclo de vida" -> nombre + descripción.
# Si no hay raya, se cae al nombre del archivo y la línea entera como
# descripción: web_server.py y listeners.py no usan la raya.
describir() {
    local archivo="$1" linea nombre descripcion
    base=$(basename "$archivo")
    linea=$(primera_linea "$archivo")
    if [[ "$linea" == *" — "* ]]; then
        nombre="${linea%% — *}"
        descripcion="${linea#* — }"
    elif [[ "$linea" == "$base"* ]]; then
        nombre="$base"
        descripcion="${linea#"$base"}"
        descripcion="${descripcion#[: ]}"
    else
        nombre="$base"
        descripcion="$linea"
    fi
    printf '%s\t%s\n' "$nombre" "$descripcion"
}

# Bloque "Uso:" de la cabecera, dedenteado. Se corta en la primera línea que no
# está indentada: a partir de ahí ya es código, no documentación.
bloque_uso() {
    sed -n '1,60p' "$1" \
        | awk '
            /(^|[^A-Za-z])Uso:/ { found = 1; next }
            !found { next }
            {
                line = $0
                sub(/^#[[:space:]]?/, "", line)
                sub(/^[[:space:]]+$/, "", line)
                if (line == "") { if (n > 0) blank++; next }
                if (line ~ /^[[:space:]]/) {
                    if (blank > 0 && n > 0) { blank = 0 }
                    sub(/^[[:space:]]+/, "", line)
                    print line
                    n++
                    next
                }
                if (n > 0) exit
            }'
}

# ── salida ───────────────────────────────────────────────────────────────────

# título|ruta. Si la ruta es un directorio se lista su contenido; si es un
# archivo se toma tal cual (para el grupo raíz). Los tests quedan fuera a
# propósito: no son scripts de operación.
GRUPOS=(
    "raíz|./reset.sh ./menu.sh ./indice.sh"
    "jamendo (radio/)|./scripts"
    "jacobs (radio-jacobs/)|./radio-jacobs/scripts"
)

listar() {
    local grupo titulo ruta ruta_uno archivo nombre descripcion
    echo
    for grupo in "${GRUPOS[@]}"; do
        titulo="${grupo%%|*}"
        ruta="${grupo#*|}"
        local encontrados=0
        for ruta_uno in $ruta; do
            if [ -d "$ruta_uno" ]; then
                for archivo in "$ruta_uno"/*.sh "$ruta_uno"/*.py; do
                    [ -f "$archivo" ] || continue
                    [ "$encontrados" -eq 0 ] && { titulo_linea "$titulo"; encontrados=1; }
                    entrada "$archivo"
                done
            elif [ -f "$ruta_uno" ]; then
                [ "$encontrados" -eq 0 ] && { titulo_linea "$titulo"; encontrados=1; }
                entrada "$ruta_uno"
            fi
        done
    done
    echo
    echo "  detalle de uno:   ./indice.sh gestor"
    echo "  menú completo:    ./menu.sh"
}

titulo_linea() {
    printf "  ── %s %s\n" "$1" "$(printf '─%.0s' $(seq 1 $((46 - ${#1}))))"
}

entrada() {
    IFS=$'\t' read -r nombre descripcion <<< "$(describir "$1")"
    printf "  %-20s %s\n" "$nombre" "$descripcion"
    printf "  %-20s %s\n" "" "./${1#./}"
}

coincide() {
    # Acepta gestor, gestor.py, scripts/gestor.py o ./scripts/gestor.py.
    local objetivo="$1" archivo="$2" base
    base="${archivo##*/}"
    [ "$base" = "$objetivo" ] && return 0
    [ "${base%.*}" = "${objetivo%.*}" ] && return 0
    [ "$archivo" = "./$objetivo" ] && return 0
    [ "${archivo#./}" = "${objetivo#./}" ] && return 0
    return 1
}

detalle() {
    local objetivo="$1" archivo encontrado=""
    while read -r archivo; do
        [ -f "$archivo" ] || continue
        if coincide "$objetivo" "$archivo"; then
            encontrado="$archivo"
            break
        fi
    done < <(scripts_de)

    if [ -z "$encontrado" ]; then
        echo "  no está: $objetivo"
        echo "  ver la lista con:  ./indice.sh"
        return 1
    fi

    IFS=$'\t' read -r nombre descripcion <<< "$(describir "$encontrado")"
    echo
    echo "  ── $encontrado ─────────────────────────────────────"
    echo
    echo "  $descripcion"
    local uso
    uso=$(bloque_uso "$encontrado")
    if [ -n "$uso" ]; then
        echo
        echo "  uso:"
        while read -r u; do echo "    $u"; done <<< "$uso"
    fi
    echo
}

# ── cli ──────────────────────────────────────────────────────────────────────

case "${1:-}" in
    -h|--help)  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' ;;
    "")         listar ;;
    -*)         echo "  opción desconocida: $1"; sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' ;;
    *)          detalle "$1" ;;
esac
