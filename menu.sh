#!/bin/bash
# menu.sh — menú de operación de las dos radios, pensado para trabajar por SSH.
#
#   ./menu.sh
#
# No agrega lógica nueva: cada opción ejecuta un script que ya existe en la
# repo. Después de correr, vuelve al menú con el estado ya refrescado, para no
# tener que abrir otra sesión ssh entre comandos.
#
# Ninguna opción queda esperando: los logs se muestran por pantalla (no con
# tail -f, que ata el menú y lo saca con un Ctrl-C) y las descargas se lanzan
# desacopladas con setsid, porque bajar un lote puede tardar media hora.

set -u
cd "$(dirname "$0")" || exit 1

PY=./venv/bin/python
JAC=radio-jacobs

# ── estado ────────────────────────────────────────────────────────────────────

# Un stream de audio nunca termina, así que "curl --max-time" sale con código
# 28 aunque haya AUDIENDO bien. Por eso se decide con el http_code que curl
# igual imprime, no con su exit code.
_http() {
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "$1" 2>/dev/null || true)
    case "$code" in
        200)    printf "OK" ;;
        000|"") printf "sin respuesta" ;;
        *)      printf "http %s" "$code" ;;
    esac
}

# logs/nowplaying.txt lo escribe radio.liq con el formato "archivo|artista — titulo"
_sonando() {
    local t
    [ -f "$1" ] || { printf "(nada)"; return; }
    t=$(cut -d'|' -f2- "$1" 2>/dev/null | head -1)
    printf "%s" "${t:-(nada)}"
}

_pg() { pgrep -f "$1" 2>/dev/null | wc -l; }

# liquidsoap se cuenta por nombre exacto (-x) y no por línea de comandos: con
# "pgrep -f liquidsoap" también contaría cualquier shell cuya línea mencione la
# palabra (incluido este menú al listar procesos).
_liq() { pgrep -x liquidsoap 2>/dev/null | wc -l; }

_cola() {
    local f="$1" n="${2:-15}"
    echo "  ── $f · últimas $n ────────────────────"
    if [ -f "$f" ]; then tail -n "$n" "$f"; else echo "    (sin log)"; fi
    echo
}

cabecera() {
    echo "════════════════════════════════════════════════════════════════"
    echo "  RADIO · $(date '+%a %d/%m %H:%M')"
    echo "════════════════════════════════════════════════════════════════"
    printf "  %-8s %-13s %s\n" "/radio"  "$(_http http://localhost:8000/radio)"  "$(_sonando logs/nowplaying.txt)"
    printf "  %-8s %-13s %s\n" "/jacobs" "$(_http http://localhost:8000/jacobs)" "$(_sonando $JAC/logs/nowplaying.txt)"
    printf "  %-8s %-13s %s\n" "panel"   "$(_http http://localhost:8080/)"     "http://localhost:8080"
    echo
    echo "  procesos: liquidsoap=$(_liq)  web=$(_pg 'web_serve[r].py')  gestor=$(_pg 'ia?_gesto[r].py')"
    echo "  oyentes:"
    ./scripts/listeners.sh 2>/dev/null | sed 's/^/    /'
    echo
}

# ── acciones ─────────────────────────────────────────────────────────────────

# Los logs se muestran por pantalla y no con tail -f: un tail -f deja el menú
# esperando y el Ctrl-C para cortarlo se lleva el menú puesto.
logs_jamendo() { _cola logs/gestor.log; _cola logs/liquidsoap.out; _cola logs/web.out; }
logs_jacobs()  { _cola $JAC/logs/gestor.log; _cola $JAC/logs/liquidsoap.out; }

# Descargar un lote puede tardar media hora, así que va desacoplado: el menú
# sigue libre y el gestor se termina sola cuando la cola queda lista.
enbg() {  # enbg <que> <comando> <log donde mirar>
    setsid nohup bash -c "$2" >> "$3" 2>&1 < /dev/null &
    echo "  $1 lanzado en segundo plano."
    echo "  seguilo con:  tail -f $3"
}

enbg_jamendo() { enbg "Descarga Jamendo" "$PY scripts/gestor.py --force" logs/gestor.log; }
enbg_jacobs()  { enbg "Descarga Jacobs"  "cd $JAC && ./venv/bin/python scripts/ia_gestor.py --force" "$JAC/logs/gestor.log"; }
enbg_refresh() { enbg "Renovación de colas" "./scripts/refresh_queue.sh" logs/gestor.log; }

tests() {
    # El gestor escribe mucho por stdout; del test solo interesa el resumen.
    ./$PY -m unittest discover -s tests 2>&1 | grep -E '^(Ran |OK$|FAILED|ERROR)'
}

procesos() { ps aux | grep -E '[l]iquidsoap|[w]eb_server|[g]estor'; }

# ── registro de visitas ───────────────────────────────────────────────────────
# Salen del access.log de icecast, no de ningún log propio: 404 = mount caído
# y se queda fuera salvo que se pida --errors. visits.py ya imprime el título.

visits_dia()     { $PY scripts/visits.py --days 30; }
visits_semana()  { $PY scripts/visits.py --days 7; }
visits_detalle() { $PY scripts/visits.py --visits --days 3 --limit 25; }
visits_fallos()  { $PY scripts/visits.py --daily --days 30 --errors; }
visits_todo()    { $PY scripts/visits.py --days 0; }
visits_volcar()  { $PY scripts/visits.py --snapshot; }

# ── el menú ───────────────────────────────────────────────────────────────────
# Cada opción es "etiqueta|comando". Las que empiezan con @ son títulos, no
# se numeran. El comando se evalúa en este shell, así que también puede ser el
# nombre de una función de arriba.

opciones=(
"@ARRANQUE — las dos estaciones"
"Reiniciar todo (stop + start)|./reset.sh"
"Arrancar todo|./reset.sh start"
"Parar todo|./reset.sh stop"
"Estado general|./reset.sh status"

"@JAMENDO — radio/ · mount /radio"
"Arrancar|./scripts/radio.sh start"
"Parar|./scripts/radio.sh stop"
"Reiniciar|./scripts/radio.sh restart"
"Estado|./scripts/radio.sh status"
"Ver logs|logs_jamendo"

"@JACOBS — radio-jacobs/ · mount /jacobs"
"Arrancar|cd $JAC && ./scripts/radio-jacobs.sh start"
"Parar|cd $JAC && ./scripts/radio-jacobs.sh stop"
"Reiniciar|cd $JAC && ./scripts/radio-jacobs.sh restart"
"Estado|cd $JAC && ./scripts/radio-jacobs.sh status"
"Ver logs|logs_jacobs"

    "@DIAGNÓSTICO"
    "Lista de scripts del proyecto|./indice.sh"
    "Oyentes por mount|./scripts/listeners.sh"
    "Procesos vivos|procesos"
    "Crontab (lo que corre solo)|crontab -l"
    "Tests del gestor|tests"

    "@VISITAS — registro de oyentes"
    "Resumen por día (30 d)|visits_dia"
    "Resumen última semana|visits_semana"
    "Detalle de visitas (3 d)|visits_detalle"
    "Contando también mount caído|visits_fallos"
    "Todo el histórico|visits_todo"
    "Volcar ahora (lo hace el cron)|visits_volcar"

    "@MANTENIMIENTO"
"Forzar descarga Jamendo|enbg_jamendo"
"Estado gestor Jamendo|./$PY scripts/gestor.py --status"
"Forzar descarga Jacobs|enbg_jacobs"
"Estado gestor Jacobs|cd $JAC && ./venv/bin/python scripts/ia_gestor.py --status"
"Renovar colas (como el cron)|enbg_refresh"
"Rotar y truncar logs|./scripts/cleanup_logs.sh"

"@"
"Salir|-"
)

while true; do
    cabecera
    echo "  ¿QUÉ HACER?"
    echo

    cmds=()
    n=0
    for opcion in "${opciones[@]}"; do
        case "$opcion" in
            "@")  echo ;;
            @*)   echo "  ${opcion#@}" ;;
            "")   echo ;;
            *)
                n=$((n + 1))
                cmds+=("${opcion#*|}")
                printf "  %2d  %s\n" "$n" "${opcion%%|*}"
                ;;
        esac
    done
    total=$n
    echo

    read -r -p "  número (vacío = salir): " n || exit 0
    case "$n" in
        ""|0) exit 0 ;;
    esac
    if ! [ "$n" -ge 1 ] 2>/dev/null || [ "$n" -gt "$total" ] 2>/dev/null; then
        echo "  no existe la opción $n."
        continue
    fi

    cmd="${cmds[$((n - 1))]}"
    [ "$cmd" = "-" ] && exit 0

    echo
    eval "$cmd"
    echo
done
