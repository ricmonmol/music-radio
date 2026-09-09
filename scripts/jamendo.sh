#!/bin/bash
# jamendo.sh: agrega música de Jamendo con derecho de emisión, en un solo paso.
#
#   bash jamendo.sh           -> interactivo: busca candidatas del clima,
#                                te muestra las válidas y pregunta qué ids bajar.
#   bash jamendo.sh --auto N  -> automático: baja N candidatas nuevas válidas
#                                sin preguntar (filtradas por clima y sin repetir
#                                las ya en el catálogo).
#
# El client id se guarda una vez en scripts/.jamendo_client (local, chmod 600).
set -euo pipefail
cd "$(dirname "$0")/.."

PY="venv/bin/python"
SCRIPT="scripts/fetch_jamendo.py"
CRED="scripts/.jamendo_client"
CLIMA="clima.json"

# Búsquedas del clima. Editá libremente (vocal/instrumental, acoustic/electric).
SEARCHES=(
  "melancholic acoustic"
  "calm guitar"
  "folk piano"
  "ambient"
  "organico"
)
SPEED="low,medium"
MAXDIST="0.4"

# ---------- client id ----------
if [[ -f "$CRED" ]]; then
  CID="$(cat "$CRED")"
else
  read -r -p "Client id de Jamendo (devportal.jamendo.com): " CID
  if [[ -z "${CID// /}" ]]; then
    echo "Sin client id, no se puede continuar." >&2
    exit 1
  fi
  umask 177
  printf '%s' "$CID" > "$CRED"
  echo "Client id guardado en $CRED."
fi

# ---------- busca y lista solo candidatas válidas (dwn=OK, cerca del clima,
# nuevas). Devuelve líneas con 'id=NNN' al final. ----------
list() {
  local out="" q r
  for q in "${SEARCHES[@]}"; do
    r="$($PY "$SCRIPT" --client-id "$CID" --search "$q" --speed "$SPEED" \
          --clima "$CLIMA" --max-distance "$MAXDIST" --exclude-existing \
          --limit 20 2>/dev/null || true)"
    out+="$r"$'\n'
  done
  printf '%s\n' "$out" | grep "dwn=OK" || true
}

usar_selector() {
  "$PY" scripts/selector.py --clima "$CLIMA" || true
  bash scripts/restart_radio.sh
  echo "Cola regenerada y radio reiniciada."
}

if [[ "${1:-}" == "--auto" ]]; then
  N="${2:-10}"
  ids="$(list | grep -o 'id=[0-9]*' | cut -d= -f2 | head -n "$N" | paste -sd, -)"
  if [[ -z "$ids" ]]; then
    echo "No hay candidatas nuevas (filtro de clima o ya en catálogo)."
    exit 0
  fi
  echo "Bajando hasta $N temas nuevos: ids=$ids"
  "$PY" "$SCRIPT" --client-id "$CID" --ids "$ids" --download
  usar_selector
else
  echo "=== Candidatas para el clima (nuevas, distancia <= $MAXDIST) ==="
  cand="$(list)"
  if [[ -z "$cand" ]]; then
    echo "No hay candidatas nuevas. Probá ajustar SEARCHES, SPEED o MAXDIST."
    exit 0
  fi
  printf '%s\n' "$cand" | sed 's/^/  /'
  echo
  read -r -p "¿Qué ids bajo? (números separados por espacio o coma; Enter para salir): " resp
  ids="$(printf '%s' "$resp" | tr ',' ' ' | sed 's/  */ /g' | sed 's/^ //;s/ $//' | tr ' ' ',')"
  if [[ -z "$ids" ]]; then
    echo "Nada que bajar."
    exit 0
  fi
  "$PY" "$SCRIPT" --client-id "$CID" --ids "$ids" --download
  usar_selector
fi