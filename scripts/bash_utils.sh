#!/bin/bash
# bash_utils.sh: funciones compartidas para los scripts bash.
# Se importa con: source "$(dirname "$0")/bash_utils.sh"

# Resuelve la raíz del proyecto (directorio padre de scripts/)
BASH_UTILS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$BASH_UTILS_DIR/.." && pwd)"

get_client_id() {
    if [[ -n "${JAMENDO_CLIENT_ID:-}" ]]; then
        echo "$JAMENDO_CLIENT_ID"
    elif [[ -f "$PROJECT_ROOT/scripts/.jamendo_client" ]]; then
        cat "$PROJECT_ROOT/scripts/.jamendo_client"
    fi
}

require_client_id() {
    local cid
    cid="$(get_client_id)"
    if [[ -z "$cid" ]]; then
        echo "Falta el client_id de Jamendo." >&2
        echo "  Opción 1: export JAMENDO_CLIENT_ID=tu_token" >&2
        echo "  Opción 2: echo 'tu_token' > scripts/.jamendo_client" >&2
        echo "  Consíguelo en https://devportal.jamendo.com" >&2
        exit 1
    fi
    echo "$cid"
}

run_selector() {
    local clima="${1:-$PROJECT_ROOT/clima.json}"
    "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/scripts/selector.py" --clima "$clima"
}

cleanup_logs() {
    "$PROJECT_ROOT/scripts/cleanup_logs.sh" || true
}

cd_project_root() {
    cd "$PROJECT_ROOT"
}

ensure_env() {
    set -a
    # shellcheck disable=SC1091
    . "$PROJECT_ROOT/.env"
    set +a
}
