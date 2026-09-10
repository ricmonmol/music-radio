#!/usr/bin/env python3
"""web_server.py: sirve web/ + API mínima para la radio (solo stdlib).

Endpoints:
  GET /now      -> JSON con el tema actual y su atribución CC, o {}
  GET /history  -> últimos N temas con atribución (query ?n=, default 20)
  GET /         -> web/index.html (panel de la radio)

Se usa junto a Liquidsoap: el now-playing lo escribe radio.liq en
logs/nowplaying.txt con el formato "filename|artista — título" y el server lo
resuelve contra songs.json para mostrar la atribución completa.
"""
import argparse
import json
import re
import socket
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import PROJECT_ROOT, load_json  # noqa: E402


class SongCache:
    def __init__(self, path):
        self.path = Path(path)
        self.mtime = None
        self.songs = []
        self.by_resolved_path = {}

    def get_by_fname(self, fname):
        if not fname:
            return None
        self.refresh()
        try:
            resolved = str(Path(fname).resolve())
        except OSError:
            resolved = fname
        return self.by_resolved_path.get(resolved)

    def refresh(self):
        try:
            current_mtime = self.path.stat().st_mtime
        except OSError:
            return
        if self.mtime != current_mtime:
            self.songs = load_json(self.path)
            new_map = {}
            for s in self.songs:
                try:
                    rp = str((PROJECT_ROOT / s["file"]).resolve())
                    new_map[rp] = s
                except OSError:
                    continue
            self.by_resolved_path = new_map
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
    }


def make_handler(songs_path, nowplaying, played, web_dir):
    cache = SongCache(songs_path)
    cache.refresh()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(web_dir), **kwargs)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/now":
                return self._send_json(self._now())
            if path == "/history":
                return self._send_json(self._history())
            if path.startswith("/radio"):
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

        def _now(self):
            p = Path(nowplaying)
            if not p.exists():
                return {}
            line = p.read_text(encoding="utf-8", errors="replace").strip()
            if not line:
                return {}
            fname = line.split("|", 1)[0].strip()
            song = cache.get_by_fname(fname)
            if song is None:
                return {"label": line}
            return public(song)

        def _history(self):
            n = 20
            m = re.search(r"[?&]n=(\d+)", self.path)
            if m:
                n = max(1, min(int(m.group(1)), 200))
            p = Path(played)
            if not p.exists():
                return []
            current = None
            np = Path(nowplaying)
            if np.exists():
                line = np.read_text(encoding="utf-8", errors="replace").strip()
                if line:
                    current = line.split("|", 1)[0].strip()
            out = []
            last_seen = None
            for line in reversed(p.read_text(encoding="utf-8",
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
                out.append(public(song) if song else {"label": line})
                if len(out) >= n:
                    break
            return out

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Panel web + API /now y /history")
    ap.add_argument("--root", default=str(PROJECT_ROOT / "web"))
    ap.add_argument("--songs", default=str(PROJECT_ROOT / "songs.json"))
    ap.add_argument("--nowplaying", default=str(PROJECT_ROOT / "logs" / "nowplaying.txt"))
    ap.add_argument("--played", default=str(PROJECT_ROOT / "logs" / "played.txt"))
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    songs_path = args.songs
    handler = partial(make_handler(songs_path, args.nowplaying, args.played, args.root))
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Web/API en http://{args.host}:{args.port} "
          f"(catálogo: {args.songs})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())