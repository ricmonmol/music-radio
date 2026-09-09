#!/usr/bin/env python3
"""tag_climate.py: etiqueta clima musical y attribution por nivel artista/álbum.

Asigna campos de "climate" y "attribution" a todas las canciones de un bucket
(artista, u artista + álbum) sin editar songs.json a mano. Los campos que no se
pasan no se tocan (merge), permitiendo afinar en pasos sucesivos.

Uso:
  tag_climate.py --artist "fleet_foxes" \
      --energy 0.35 --complexity 0.5 \
      --mood melancolico,intimo --texture organica --voice voz-intima \
      --instrumentation guitarra,cuerdas --temporalidad contemporaneo \
      --creator "Fleet Foxes" --license-url "https://..." --track-url "https://..."

  tag_climate.py --artist "milton_nascimento" --album "Clube da Esquina" \
      --energy 0.4 --mood suave
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CLIMATE_FIELDS = {
    "mood": "list",
    "texture": "str",
    "energy": "float",
    "complexity": "float",
    "voice": "str",
    "instrumentation": "list",
    "temporalidad": "str",
}
ATTRIBUTION_FIELDS = ("creator", "license_url", "track_url")


def load_songs(path):
    if Path(path).exists():
        try:
            data = Path(path).read_text(encoding="utf-8")
            return json.loads(data) if data.strip() else []
        except json.JSONDecodeError:
            return []
    return []


def parse_list(value):
    return [v.strip().lower() for v in value.split(",") if v.strip()]


def main():
    ap = argparse.ArgumentParser(
        description="Etiqueta climate/attribution por artista o artista+álbum")
    ap.add_argument("--songs", default=str(PROJECT_ROOT / "songs.json"))
    ap.add_argument("--artist", required=True,
                    help="artista objetivo (match exacto del campo artist)")
    ap.add_argument("--album", default=None,
                    help="si se pasa, limita el bucket a ese álbum")
    ap.add_argument("--mood", default=None, help="lista csv de moods")
    ap.add_argument("--texture", default=None)
    ap.add_argument("--energy", type=float, default=None)
    ap.add_argument("--complexity", type=float, default=None)
    ap.add_argument("--voice", default=None)
    ap.add_argument("--instrumentation", default=None, help="lista csv")
    ap.add_argument("--temporalidad", default=None)
    ap.add_argument("--creator", default=None)
    ap.add_argument("--license-url", default=None, dest="license_url")
    ap.add_argument("--track-url", default=None, dest="track_url")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for name in ("energy", "complexity"):
        v = getattr(args, name)
        if v is not None and not (0.0 <= v <= 1.0):
            print(f"error: {name} debe estar entre 0.0 y 1.0 (recibido {v})",
                  file=sys.stderr)
            return 1

    songs = load_songs(args.songs)
    if not songs:
        print("songs.json vacío o inexistente.", file=sys.stderr)
        return 1

    matches = [s for s in songs if s.get("artist") == args.artist
               and (args.album is None or s.get("album") == args.album)]
    if not matches:
        print(f"No hay canciones de artista='{args.artist}'"
              + (f" y album='{args.album}'" if args.album else "")
              + " en songs.json.", file=sys.stderr)
        return 1

    climate = {
        k: v for k, v in {
            "mood": parse_list(args.mood) if args.mood else None,
            "texture": args.texture,
            "energy": args.energy,
            "complexity": args.complexity,
            "voice": args.voice,
            "instrumentation": (parse_list(args.instrumentation)
                                if args.instrumentation else None),
            "temporalidad": args.temporalidad,
        }.items() if v is not None
    }
    attribution = {
        k: v for k, v in {
            "creator": args.creator,
            "license_url": args.license_url,
            "track_url": args.track_url,
        }.items() if v is not None
    }

    if not climate and not attribution:
        print("No se pasó ningún campo para etiquetar.", file=sys.stderr)
        return 1

    for s in matches:
        s.setdefault("climate", {})["bucket"] = args.artist
        s["climate"].update(climate)
        if attribution:
            s.setdefault("attribution", {}).update(attribution)

    if not args.dry_run:
        Path(args.songs).write_text(
            json.dumps(songs, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

    print(f"Etiquetadas {len(matches)} canciones de '{args.artist}'"
          + (f" / '{args.album}'" if args.album else "")
          + ("" if args.dry_run else "") + ":")
    for s in matches:
        a = s.get("attribution") or {}
        print(f"  {s['file']}  clima={s.get('climate')}  "
              f"attribution.creator={a.get('creator')}")
    if args.dry_run:
        print("(dry-run: no se escribió songs.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())