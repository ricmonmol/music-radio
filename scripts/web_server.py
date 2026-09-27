#!/usr/bin/env python3
"""web_server.py: sirve web/ + API mínima para las radios (solo stdlib).

Endpoints:
  GET /stations            -> configuración de las estaciones para el panel
  GET /now?station=<id>    -> JSON con el tema actual y su atribución, o {}
  GET /history?station=<id>&n=N -> últimos N temas (default 20)
  GET /<mount>             -> proxy del stream de icecast
  GET /                    -> web/index.html (panel con pestañas)

Multi-estación: todo lo que antes estaba fijo a PROJECT_ROOT (songs.json,
logs/nowplaying.txt, logs/played.txt) se resuelve contra el root de la
estación elegida en stations.json, así el mismo panel sirve la radio de Jamendo
y la de la Aadam Jacobs Collection sin duplicar procesos.

Se usa junto a Liquidsoap: el now-playing lo escribe cada radio.liq en su
logs/nowplaying.txt con el formato "filename|artista — título" y el server lo
resuelve contra el songs.json de esa estación.
"""
import argparse
import json
import re
import socket
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIONS_PATH = PROJECT_ROOT / "stations.json"


def load_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        data = p.read_text(encoding="utf-8").strip()
        return json.loads(data) if data else default
    except (json.JSONDecodeError, OSError):
        return default


def load_stations(path=STATIONS_PATH):
    """Lee stations.json y resuelve el root absoluto de cada estación.

    El root es relativo al directorio donde vive stations.json, así la radio
    de Jamendo es "." y la de AJC es "../radio-jacobs": las dos estaciones
    comparten icecast, venv y panel, pero nada más.
    """
    doc = load_json(path, {})
    base = Path(path).resolve().parent
    stations = []
    for raw in doc.get("stations") or []:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        station = dict(raw)
        station["root"] = str((base / raw.get("root", ".")).resolve())
        station.setdefault("mount", "")
        station.setdefault("label", raw["id"])
        stations.append(station)
    default_id = doc.get("default") or (stations[0]["id"] if stations else "")
    return stations, default_id


def track_identity(song):
    name = Path(str(song.get("file", ""))).name
    # El id de archive.org es alfanumérico ("0120_freakons2013-09-22t02"), así
    # que el patrón((\d+)$) de la radio de Jamendo no matcheaba y el panel
    # caía siempre en la rama de "canción rotada".
    # El prefijo ".*" es codicioso: el archivo es "{título} - {id}" y el
    # título puede traer " - " adentro, así que hay que partir por la última.
    match = re.match(r"^.* - (.+)$", Path(name).stem)
    return match.group(1) if match else Path(name).stem


class SongCache:
    def __init__(self, path, root):
        self.path = Path(path)
        self.root = Path(root)
        self.mtime = None
        self.songs = []
        self.by_resolved_path = {}
        self.by_basename = {}
        self.by_identity = {}

    def get_by_fname(self, fname):
        if not fname:
            return None
        self.refresh()
        try:
            candidate = Path(str(fname))
            if not candidate.is_absolute():
                candidate = self.root / candidate
            resolved = str(candidate.resolve())
        except OSError:
            resolved = fname
        song = self.by_resolved_path.get(resolved)
        if song is not None:
            return song
        basename = Path(str(fname)).name
        song = self.by_basename.get(basename)
        if song is not None:
            return song
        return self.by_identity.get(track_identity({"file": basename}))

    @staticmethod
    def add_unique(mapping, key, value):
        if not key:
            return
        if key in mapping and mapping[key] is not value:
            mapping[key] = None
        elif key not in mapping:
            mapping[key] = value

    def refresh(self):
        try:
            current_mtime = self.path.stat().st_mtime
        except OSError:
            return
        if self.mtime != current_mtime:
            self.songs = load_json(self.path, [])
            new_map = {}
            new_basenames = {}
            new_identities = {}
            for s in self.songs:
                if not isinstance(s, dict) or not s.get("file"):
                    continue
                try:
                    rp = str((self.root / s["file"]).resolve())
                except OSError:
                    continue
                new_map[rp] = s
                self.add_unique(new_basenames, Path(s["file"]).name, s)
                self.add_unique(new_identities, track_identity(s), s)
            self.by_resolved_path = new_map
            self.by_basename = new_basenames
            self.by_identity = new_identities
            self.mtime = current_mtime


def public(song):
    att = song.get("attribution") or {}
    return {
        "artist": song.get("artist"),
        "title": song.get("title"),
        "album": song.get("album"),
        "license": song.get("license"),
        "creator": att.get("creator"),
        "license_url": att.get("license_url"),
        "track_url": att.get("track_url"),
        "climate": song.get("climate") or {},
        # Crédito de archivo: lugar, fecha y grabador. La AJC no trae licencia
        # Creative Commons, así que esta línea es la atribución que exige el
        # modelo de permisos del Live Music Archive.
        "venue": att.get("venue") or "",
        "recorded": att.get("recorded") or "",
        "taper": att.get("taper") or "",
    }


def make_handler(stations, default_id, web_dir):
    caches = {
        st["id"]: SongCache(Path(st["root"]) / "songs.json", st["root"])
        for st in stations
    }
    by_id = {st["id"]: st for st in stations}
    mounts = {st["mount"]: st for st in stations if st.get("mount")}

    def pick_station(query):
        wanted = re.search(r"(?:^|[?&])station=([^&]*)", query)
        sid = wanted.group(1) if wanted else default_id
        return by_id.get(sid) or (stations[0] if stations else None)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(web_dir), **kwargs)

        def do_GET(self):
            raw_path, _, query = self.path.partition("?")
            path = raw_path
            if path == "/stations":
                return self._send_json({
                    "default": default_id,
                    "stations": [
                        {
                            "id": st["id"],
                            "label": st["label"],
                            "mount": st["mount"],
                        }
                        for st in stations
                    ],
                })
            if path == "/now":
                return self._send_json(self._now(pick_station(query)))
            if path == "/history":
                return self._send_json(self._history(pick_station(query), query))
            if path in mounts:
                return self._proxy_icecast()
            return super().do_GET()

        def _proxy_icecast(self):
            up = None
            try:
                up = socket.create_connection(("127.0.0.1", 8000), timeout=10)
            except OSError:
                self.send_error(503, "Icecast no disponible")
                return
            try:
                req = f"{self.command} {self.path} {self.request_version}\r\n"
                for k, v in self.headers.items():
                    if k.lower() in (
                        "proxy-connection",
                        "connection",
                        "keep-alive",
                        "te",
                        "trailer",
                        "transfer-encoding",
                        "upgrade",
                    ):
                        continue
                    req += f"{k}: {v}\r\n"
                req += "Connection: close\r\n\r\n"
                up.sendall(req.encode("latin-1", "replace"))
                headers = bytearray()
                while True:
                    chunk = up.recv(65536)
                    if not chunk:
                        break
                    headers += chunk
                    if b"\r\n\r\n" in headers:
                        break
                # head viene de bytes(): si se parte en bytes, split() devuelve
                # elementos int y partition(":") explota con
                # "a bytes-like object is required, not 'str'". Icecast manda
                # Server/Content-Type, y sin Content-Type el <audio> del panel
                # no carga: hay que decodificar antes de parsear.
                raw_head, _, body = bytes(headers).partition(b"\r\n\r\n")
                head = raw_head.decode("latin-1", "replace")
                status = head.split("\r\n", 1)[0]
                status_line = status
                m = re.match(r"HTTP/\d(?:\.\d)?\s+(\d{3})\b", status_line)
                code = int(m.group(1)) if m else 200
                reason = status_line.split(" ", 2)[2] if " " in status_line else ""
                proto = self.protocol_version
                self.send_response(code, reason)
                for line in head.split("\r\n")[1:]:
                    k, _, v = line.partition(":")
                    kl = k.strip().lower()
                    if kl in (
                        "connection",
                        "keep-alive",
                        "proxy-connection",
                        "transfer-encoding",
                        "content-length",
                        "cache-control",
                        "pragma",
                        "expires",
                    ):
                        continue
                    self.send_header(k.strip(), v.strip())
                self.send_header("Cache-Control", "no-store")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                if body:
                    self.wfile.write(body)
                while True:
                    data = up.recv(65536)
                    if not data:
                        break
                    self.wfile.write(data)
                    self.wfile.flush()
            finally:
                up.close()

        def _send_json(self, obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        @staticmethod
        def _label_fallback(line):
            label = line.split("|", 1)[1].strip() if "|" in line else line
            if " — " in label:
                artist, title = label.split(" — ", 1)
                return {"artist": artist.strip(), "title": title.strip()}
            return {"title": label}

        def _now(self, station):
            if station is None:
                return {}
            nowplaying = Path(station["root"]) / "logs" / "nowplaying.txt"
            if not nowplaying.exists():
                return {}
            line = nowplaying.read_text(encoding="utf-8", errors="replace").strip()
            if not line:
                return {}
            fname = line.split("|", 1)[0].strip()
            song = caches[station["id"]].get_by_fname(fname)
            if song is not None:
                return public(song)
            # Canción ya no está en songs.json (fue rotada) — parsear del label
            return self._label_fallback(line)

        def _history(self, station, query):
            n = 20
            m = re.search(r"[?&]n=(\d+)", query)
            if m:
                n = max(1, min(int(m.group(1)), 200))
            if station is None:
                return []
            played = Path(station["root"]) / "logs" / "played.txt"
            if not played.exists():
                return []
            current = None
            np = Path(station["root"]) / "logs" / "nowplaying.txt"
            if np.exists():
                line = np.read_text(encoding="utf-8", errors="replace").strip()
                if line:
                    current = line.split("|", 1)[0].strip()
            cache = caches[station["id"]]
            out = []
            last_seen = None
            for line in reversed(played.read_text(encoding="utf-8",
                                                  errors="replace").splitlines()):
                line = line.strip()
                if not line:
                    continue
                fname = line.split("|", 1)[0].strip()
                # el historial muestra las ANTERIORES: se omite la que suena ahora
                if current and fname == current:
                    continue
                # colapsar duplicados consecutivos (restos del doble on_track)
                if fname == last_seen:
                    continue
                last_seen = fname
                song = cache.get_by_fname(fname)
                if song is not None:
                    out.append(public(song))
                else:
                    # Canción rotada — parsear del label
                    out.append(self._label_fallback(line))
                if len(out) >= n:
                    break
            return out

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Panel web + API /now y /history")
    ap.add_argument("--root", default=str(PROJECT_ROOT / "web"))
    ap.add_argument("--stations", default=str(STATIONS_PATH))
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    stations, default_id = load_stations(args.stations)
    if not stations:
        print(f"ERROR: sin estaciones en {args.stations}", file=sys.stderr)
        return 1
    handler = partial(make_handler(stations, default_id, args.root))
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Web/API en http://{args.host}:{args.port} "
          f"({len(stations)} estaciones: "
          f"{', '.join(st['id'] + '@' + st['mount'] for st in stations)})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
