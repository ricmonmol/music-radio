#!/usr/bin/env python3
"""ingest.py: escanea music/, extrae metadata con ffprobe y registra en songs.json."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import PROJECT_ROOT, load_json, save_json  # noqa: E402
AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wav", ".opus"}
DEFAULT_LICENSE = "download-only"


def ffmpeg_probe(path):
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_entries", "format_tags",
        "-show_streams", "-select_streams", "a:0", str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return {}
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}
    tags = (data.get("format") or {}).get("tags") or {}
    streams = data.get("streams") or []
    duration = None
    try:
        duration = float(((data.get("format") or {}).get("duration") or 0))
    except (TypeError, ValueError):
        duration = None
    return {
        "title": tags.get("title"),
        "artist": tags.get("artist"),
        "album": tags.get("album"),
        "duration_seconds": duration,
        "bit_rate": streams[0].get("bit_rate") if streams else None,
    }


def load_songs(path):
    return load_json(path)


def main():
    ap = argparse.ArgumentParser(description="Ingesta de música al catálogo")
    ap.add_argument("--music", default=str(PROJECT_ROOT / "music"))
    ap.add_argument("--songs", default=str(PROJECT_ROOT / "songs.json"))
    ap.add_argument(
        "--license", default=None,
        help="licencia para archivos nuevos: cc0, public-domain, cc-by, "
             "cc-by-sa, cc-by-nc, permission, download-only",
    )
    ap.add_argument("--source", default="local")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    music_dir = Path(args.music)
    songs_path = Path(args.songs)
    songs = load_songs(songs_path)
    by_id = {s["id"]: s for s in songs}
    by_file = {str(s.get("file")): s["id"] for s in songs}

    files = sorted(
        p for p in music_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS
    )
    if not files:
        print(f"No se encontraron archivos de audio en {music_dir}", file=sys.stderr)
        return 1

    added = skipped = 0
    for f in files:
        rel = str(f.relative_to(music_dir.parent))
        sid = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
        if sid in by_id or rel in by_file:
            skipped += 1
            continue
        info = ffmpeg_probe(f)
        entry = {
            "id": sid,
            "file": rel,
            "title": info.get("title") or f.stem,
            "artist": info.get("artist") or "desconocido",
            "album": info.get("album") or "desconocido",
            "duration_seconds": info.get("duration_seconds"),
            "license": args.license or DEFAULT_LICENSE,
            "source": args.source,
            "attribution": {"creator": None, "license_url": None, "track_url": None},
            "climate": {
                "mood": [],
                "texture": "organica",
                "energy": 0.5,
                "complexity": 0.5,
                "voice": "instrumental",
                "instrumentation": [],
                "temporalidad": "contemporaneo",
            },
        }
        by_id[sid] = entry
        added += 1
        print(f"+ {entry['license']:14s} {rel}")

    if not args.dry_run:
        save_json(args.songs, list(by_id.values()))
    print(f"\nResumen: {added} añadidas, {skipped} ya presentes en {songs_path}")
    print("Aviso: sin --license, los archivos quedan 'download-only' y NO se "
          "emiten hasta editar el campo license en songs.json.")


if __name__ == "__main__":
    sys.exit(main())