#!/usr/bin/env python3
"""Oyentes de un mount de Icecast.

Modos:
  listeners.py <mount>        imprime el numero de oyentes de un mount.
                              Es lo que invoca radio.liq cada IDLE_POLL segundos.
  listeners.py --all          imprime una tabla con todos los mounts.
  listeners.py --stamp        idem, precedido de la hora. Para --watch.
  listeners.py --log <mount>  anade una linea con la hora a logs/listeners.log.

Ante cualquier error imprime 0 y sale con codigo 0: es preferible pausar de
mas que gastar canciones sin que nadie escuche.
"""

import html
import json
import os
import sys
import urllib.request
from datetime import datetime

STATUS_URL = "http://localhost:8000/status-json.xsl"
TIMEOUT = 4.0
LOG_FILE = os.path.join("logs", "listeners.log")


def fetch():
    """Devuelve [(mount, oyentes, pico, titulo)] de todos los mounts."""
    with urllib.request.urlopen(STATUS_URL, timeout=TIMEOUT) as r:
        data = json.load(r)

    # Con un solo mount activo icecast devuelve source como dict (la propia
    # source), no como lista. Con varios mounts devuelve una lista de dicts.
    # Interpretar el dict como mapa de sources rompía fetch() con un
    # AttributeError, y el except global devolvía 0 oyentes: el gate de
    # radio.liq pausaba la radio para siempre aunque hubiera gente escuchando.
    sources = data["icestats"]["source"]
    if isinstance(sources, dict):
        sources = [sources]

    rows = []
    for entry in sources:
        url = (entry.get("listenurl") or "").rstrip("/")
        mount = "/" + url.rsplit("/", 1)[-1] if url else "?"
        try:
            n = int(entry.get("listeners", 0))
        except (TypeError, ValueError):
            n = 0
        try:
            peak = int(entry.get("listener_peak", 0))
        except (TypeError, ValueError):
            peak = 0
        # Icecast escapa entidades XML en los titulos (&#8212; = em dash).
        rows.append((mount, n, peak, html.unescape(entry.get("title") or "")))
    return sorted(rows)


def count_listeners(mount: str) -> int:
    for m, n, _, _ in fetch():
        if m.rstrip("/") == mount.rstrip("/"):
            return n
    return 0


def print_all(stamp: bool) -> None:
    rows = fetch()
    if stamp:
        print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if not rows:
        print("  (icecast no responde o no hay mounts)")
        return
    width = max(len(m) for m, _, _, _ in rows)
    for mount, n, peak, title in rows:
        line = "  {}{}  {:>3} oyente(s)".format(
            mount.ljust(width), ":" if len(mount) == width else ":", n
        )
        if peak:
            line += "   pico {}".format(peak)
        if title:
            line += "   {}".format(title)
        print(line)


def log_line(mount: str) -> None:
    n = count_listeners(mount)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs("logs", exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write("{}  {:<9} {:>3} oyente(s)\n".format(stamp, mount, n))
    except OSError:
        pass


def main() -> None:
    args = sys.argv[1:]
    try:
        if not args:
            print(0)
        elif args[0] == "--all":
            print_all(stamp=False)
        elif args[0] == "--stamp":
            print_all(stamp=True)
        elif args[0] == "--log" and len(args) == 2:
            log_line(args[1])
        elif args[0] not in ("--all", "--stamp", "--log"):
            print(count_listeners(args[0]))
    except Exception:
        if args and args[0] in ("--all", "--stamp"):
            print("  (icecast no responde: {})".format(datetime.now()
                  .strftime("%H:%M:%S")))
        else:
            print(0)
    sys.exit(0)


if __name__ == "__main__":
    main()
