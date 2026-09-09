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
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_json(path):
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = p.read_text(encoding="utf-8")
        return json.loads(data) if data.strip() else []
    except json.JSONDecodeError:
        return []


def load_song_by_file(songs, fname):
    if not fname:
        return None
    try:
        resolved = str(Path(fname).resolve())
    except OSError:
        return None
    for s in songs:
        try:
            if resolved == str((PROJECT_ROOT / s["file"]).resolve()):
                return s
        except OSError:
            continue
    return None


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
    def songs_now():
        return load_json(songs_path)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(web_dir), **kwargs)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/now":
                return self._send_json(self._now())
            if path == "/history":
                return self._send_json(self._history())
            return super().do_GET()

        def _send_json(self, obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _now(self):
            songs = songs_now()
            p = Path(nowplaying)
            if not p.exists():
                return {}
            line = p.read_text(encoding="utf-8", errors="replace").strip()
            if not line:
                return {}
            fname = line.split("|", 1)[0].strip()
            song = load_song_by_file(songs, fname)
            if song is None:
                return {"label": line}
            return public(song)

        def _history(self):
            n = 20
            m = re.search(r"[?&]n=(\d+)", self.path)
            if m:
                n = max(1, min(int(m.group(1)), 200))
            songs = songs_now()
            p = Path(played)
            if not p.exists():
                return []
            out = []
            for line in reversed(p.read_text(encoding="utf-8",
                                             errors="replace").splitlines()):
                line = line.strip()
                if not line:
                    continue
                fname = line.split("|", 1)[0].strip()
                song = load_song_by_file(songs, fname)
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