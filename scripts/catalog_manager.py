#!/usr/bin/env python3
"""catalog_manager.py: gestión del ciclo de vida del catálogo de la radio.

Subcomandos:
  status                  imprime el estado del catálogo y de la cola.
  refresh                 descarga un lote nuevo de Jamendo (batch) con
                          --exclude-existing implícito y regenera la cola.
  rotate                  archiva canciones viejas (mp3 + entrada de
                          songs.json) y limpia logs/played.txt.
  auto                    decide entre refresh/rotate según thresholds.

El uso típico es un cron cada pocas horas que ejecuta 'auto'; con el catálogo
entre --min-songs y --max-songs no hace nada, con menos baja un lote y con
más rota. Así se reduce el consumo de la API de Jamendo (se descarga por
lotes grandes, no de a una).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
SONGS_PATH = PROJECT_ROOT / "songs.json"
PLAYED_PATH = PROJECT_ROOT / "logs" / "played.txt"
QUEUE_PATH = PROJECT_ROOT / "queue.m3u"
CLIMA_PATH = PROJECT_ROOT / "clima.json"
META_PATH = PROJECT_ROOT / "data" / "catalog_meta.json"
ARCHIVE_DIR = PROJECT_ROOT / "archive"
MUSIC_DIR = PROJECT_ROOT / "music"
CLIENT_FILE = SCRIPTS_DIR / ".jamendo_client"

sys.path.insert(0, str(SCRIPTS_DIR))
import fetch_jamendo  # noqa: E402

DEFAULT_META = {
    "last_refresh": None,
    "last_rotate": None,
    "total_downloaded": 0,
    "total_archived": 0,
    "batch_count": 0,
}


def load_json(path):
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = p.read_text(encoding="utf-8")
        return json.loads(data) if data.strip() else []
    except json.JSONDecodeError:
        return []


def save_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")


def load_meta():
    if META_PATH.exists():
        try:
            data = json.loads(META_PATH.read_text(encoding="utf-8"))
            return {**DEFAULT_META, **data}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_META)


def save_meta(meta):
    save_json(META_PATH, meta)


def client_id():
    if os.environ.get("JAMENDO_CLIENT_ID"):
        return os.environ["JAMENDO_CLIENT_ID"]
    if CLIENT_FILE.exists():
        token = CLIENT_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    return None


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def emisible(songs):
    return [s for s in songs if s.get("license") in fetch_jamendo.EMIT_LICENSES]


def read_played_counts():
    """Mapea los paths de logs/played.txt a ids de songs.json y cuenta."""
    songs = load_json(SONGS_PATH)
    by_path = {}
    for s in songs:
        try:
            rp = str((PROJECT_ROOT / s["file"]).resolve())
        except OSError:
            continue
        by_path[rp] = s["id"]
    counts, last = {}, {}
    p = Path(PLAYED_PATH)
    if not p.exists():
        return counts, last
    for idx, raw in enumerate(p.read_text(encoding="utf-8",
                                          errors="replace").splitlines()):
        line = raw.strip()
        if not line:
            continue
        first = line.split("|", 1)[0].strip()
        sid = by_path.get(first)
        if sid is None:
            continue
        counts[sid] = counts.get(sid, 0) + 1
        last[sid] = idx
    return counts, last


def queue_paths():
    p = Path(QUEUE_PATH)
    if not p.exists():
        return set()
    out = set()
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if (line and not line.startswith("#") and line.endswith(".mp3")):
            out.add(line)
    return out


def regenerate_queue():
    cmd = [str(PROJECT_ROOT / "venv" / "bin" / "python"),
           str(SCRIPTS_DIR / "selector.py"),
           "--clima", str(CLIMA_PATH)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    lines = (res.stdout or "").strip().splitlines()
    print("\n".join(lines))
    if res.returncode != 0:
        print(res.stderr, file=sys.stderr)
    else:
        restart_radio()


def restart_radio():
    subprocess.run(["bash", str(SCRIPTS_DIR / "restart_radio.sh")],
                   check=False)


def log(msg):
    p = PROJECT_ROOT / "logs" / "catalog.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(f"{now_iso()}  {msg}\n")
    print(msg)


# ---------------------------------------------------------------- status --

def cmd_status(args):
    songs = load_json(SONGS_PATH)
    emit = emisible(songs)
    counts, _ = read_played_counts()
    played_distinct = len([s for s in emit if s["id"] in counts])
    queue = queue_paths()
    meta = load_meta()

    total_sec = sum(int(s.get("duration_seconds") or 0) for s in emit)
    hours = total_sec / 3600 if total_sec else 0
    total_mb = 0
    for s in emit:
        try:
            total_mb += (PROJECT_ROOT / s["file"]).stat().st_size / 1e6
        except OSError:
            pass

    print("Estado del catálogo")
    print(f"  Canciones en songs.json : {len(songs)} ({len(emit)} emisibles)")
    print(f"  Duración total          : {hours:.1f} h")
    print(f"  Espacio en disco        : {total_mb:.1f} MB")
    print(f"  Reproducidas distintas  : {played_distinct} de {len(emit)} "
          f"({100.0 * played_distinct / len(emit) if emit else 0:.0f}%)")
    print(f"  Canciones en la cola    : {len(queue)}")
    print(f"  Último refresh          : {meta.get('last_refresh') or 'nunca'}")
    print(f"  Último rotate           : {meta.get('last_rotate') or 'nunca'}")
    print(f"  Descargadas en total    : {meta.get('total_downloaded')} "
          f"({meta.get('batch_count')} lotes)")
    print(f"  Archivadas en total     : {meta.get('total_archived')}")
    return 0


# --------------------------------------------------------------- refresh --

def refresh_filters(a):
    filters = {}
    for key in ("search", "tags", "fuzzytags", "acoustic", "vocal", "speed",
                "duration_from", "duration_to", "boost", "clima",
                "max_distance"):
        filters[key] = getattr(a, key, None)
    return filters


def batch_namespace(client_id, music_dir, songs_path, bsize, filters):
    ns = argparse.Namespace(
        client_id=client_id,
        songs=str(songs_path),
        music=str(music_dir),
        limit=bsize,
        order=getattr(filters, "order", "relevance"),
        search=filters.get("search"),
        tags=filters.get("tags"),
        fuzzytags=filters.get("fuzzytags"),
        acoustic=filters.get("acoustic"),
        vocal=filters.get("vocal"),
        speed=filters.get("speed"),
        duration_from=filters.get("duration_from"),
        duration_to=filters.get("duration_to"),
        boost=filters.get("boost"),
        clima=filters.get("clima"),
        max_distance=filters.get("max_distance"),
        ids=None,
        download=False,
        batch=True,
        no_save=False,
    )
    if isinstance(ns.tags, str):
        ns.tags = [x.strip() for x in ns.tags.split(",") if x.strip()]
    if isinstance(ns.fuzzytags, str):
        ns.fuzzytags = [x.strip() for x in ns.fuzzytags.split(",") if x.strip()]
    if isinstance(ns.speed, str):
        ns.speed = [x.strip() for x in ns.speed.split(",") if x.strip()]
    return ns


def cmd_refresh(args):
    cid = client_id()
    if not cid:
        print("Falta el client_id de Jamendo. Revisa scripts/.jamendo_client "
              "o la env JAMENDO_CLIENT_ID.", file=sys.stderr)
        return 1
    before = len(load_json(SONGS_PATH))
    ns = batch_namespace(cid, MUSIC_DIR, SONGS_PATH, args.batch_size,
                         refresh_filters(args))
    rc = fetch_jamendo.cmd_batch(ns)
    after = len(load_json(SONGS_PATH))
    added = after - before
    log(f"refresh: +{added} canciones (catálogo {before} -> {after})")
    meta = load_meta()
    meta["last_refresh"] = now_iso()
    meta["total_downloaded"] += added
    meta["batch_count"] += 1
    save_meta(meta)
    if added:
        regenerate_queue()
    return rc


# ---------------------------------------------------------------- rotate --

def cmd_rotate(args):
    songs = load_json(SONGS_PATH)
    if len(songs) <= args.keep:
        print(f"Catálogo ({len(songs)}) ya está en/por debajo de --keep "
              f"({args.keep}). Nada que archivar.")
        meta = load_meta()
        meta["last_rotate"] = now_iso()
        save_meta(meta)
        return 0

    queued = queue_paths()
    n_archive = len(songs) - args.keep
    candidates = []
    for s in songs:
        try:
            resolved = str((PROJECT_ROOT / s["file"]).resolve())
        except OSError:
            resolved = s.get("file")
        if resolved in queued:
            continue
        candidates.append(s)
    if len(candidates) < n_archive:
        print(f"Solo {len(candidates)} candidatas fuera de la cola actual; "
              f"se archivarán esa cantidad en lugar de {n_archive}.")
        n_archive = len(candidates)
    to_archive = candidates[-n_archive:] if n_archive else []

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archived_paths = {}
    moved = 0
    for s in to_archive:
        src = PROJECT_ROOT / s["file"]
        dst = ARCHIVE_DIR / s["file"]
        if getattr(args, "dry_run", False):
            archived_paths[s["id"]] = s["file"]
            moved += 1
            print(f"  (dry) archivar {s['file']}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if src.exists():
                shutil.move(str(src), str(dst))
        except OSError as e:
            print(f"  ! no se pudo mover {src}: {e}", file=sys.stderr)
            continue
        archived_paths[s["id"]] = s["file"]
        moved += 1
        print(f"  archivar {s['file']}")

    remaining = [s for s in songs if s["id"] not in archived_paths]

    if not (args.dry_run):
        save_json(SONGS_PATH, remaining)
        if args.keep_played:
            print("Se mantiene logs/played.txt.")
        else:
            backup = PROJECT_ROOT / "logs" / f"played.rotate.{datetime.now().strftime('%Y%m%d%H%M%S')}.txt"
            if PLAYED_PATH.exists():
                shutil.copy(PLAYED_PATH, backup)
                PLAYED_PATH.write_text("", encoding="utf-8")
                print(f"logs/played.txt limpiado (copia en {backup.name}).")
        meta = load_meta()
        meta["last_rotate"] = now_iso()
        meta["total_archived"] += moved
        save_meta(meta)
        log(f"rotate: {moved} archivadas (quedan {len(remaining)} en el "
            f"catálogo), played.txt {'conservado' if args.keep_played else 'limpio'}")
        regenerate_queue()
    return 0


# ------------------------------------------------------------------- auto --

def cmd_auto(args):
    songs = load_json(SONGS_PATH)
    emit = emisible(songs)
    n = len(emit)
    log(f"auto: catálogo con {n} canciones "
        f"(min {args.min_songs}, max {args.max_songs})")
    if n < args.min_songs:
        bsize = min(args.batch_size, args.min_songs - n + 10)
        log(f"auto: bajo de mínimo, descargando lote de {bsize}")
        rargs = argparse.Namespace(**vars(args))
        rargs.batch_size = bsize
        return cmd_refresh(rargs)
    if n > args.max_songs:
        keep = args.max_songs // 2
        log(f"auto: sobre el máximo, rotando a {keep}")
        return cmd_rotate(argparse.Namespace(keep=keep, keep_played=False,
                                             dry_run=args.dry_run))
    log("auto: catálogo en rango, no se hace nada.")
    return 0


# ------------------------------------------------------------------ main --

def main():
    ap = argparse.ArgumentParser(description="Gestión del ciclo del catálogo")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="estado del catálogo y la cola")
    p_status.set_defaults(func=cmd_status)

    p_refresh = sub.add_parser("refresh", help="descarga un lote nuevo")
    p_refresh.add_argument("--batch-size", type=int, default=80,
                           help="lote objetivo (default 80)")
    add_fetch_args(p_refresh)
    p_refresh.set_defaults(func=cmd_refresh)

    p_rotate = sub.add_parser("rotate", help="archiva canciones viejas")
    p_rotate.add_argument("--keep", type=int, default=30,
                          help="cuántas canciones dejar en songs.json (default 30)")
    p_rotate.add_argument("--keep-played", action="store_true",
                          help="no limpiar logs/played.txt")
    p_rotate.add_argument("--dry-run", action="store_true",
                          help="solo mostrar qué se archivaría")
    p_rotate.set_defaults(func=cmd_rotate)

    p_auto = sub.add_parser("auto", help="decide refresh/rotate por thresholds")
    p_auto.add_argument("--min-songs", type=int, default=30)
    p_auto.add_argument("--max-songs", type=int, default=200)
    p_auto.add_argument("--batch-size", type=int, default=80)
    p_auto.add_argument("--dry-run", action="store_true")
    add_fetch_args(p_auto)
    p_auto.set_defaults(func=cmd_auto)

    args = ap.parse_args()
    return args.func(args)


def add_fetch_args(parser):
    parser.add_argument("--search", default=None)
    parser.add_argument("--tags", default=None, help="csv, combinados AND")
    parser.add_argument("--fuzzytags", default=None, help="csv, combinados OR")
    parser.add_argument("--acoustic", choices=["acoustic", "electric"], default=None)
    parser.add_argument("--vocal", choices=["vocal", "instrumental"], default=None)
    parser.add_argument("--speed", default=None,
                        help="csv: verylow,low,medium,high,veryhigh")
    parser.add_argument("--duration-from", type=int, default=None)
    parser.add_argument("--duration-to", type=int, default=None)
    parser.add_argument("--boost", default=None)
    parser.add_argument("--clima", default=str(CLIMA_PATH),
                        help="JSON del clima objetivo")
    parser.add_argument("--max-distance", type=float, default=0.6,
                        help="umbral de distancia de clima (default 0.6)")


if __name__ == "__main__":
    sys.exit(main())