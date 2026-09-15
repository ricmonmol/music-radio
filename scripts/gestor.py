#!/usr/bin/env python3
"""gestor.py — ciclo de vida de la radio musical.

Flujo completo en un solo lugar:
  1. Lee clima.json  →  qué música buscar
  2. Calcula cuántas canciones sin reproducir quedan en el catálogo
  3. Si quedan pocas (≤ LOW_WATERMARK):
       a. Descarga un lote de Jamendo filtrado por climate_distance
       b. Borra los mp3s ya reproducidos para liberar disco
       c. Actualiza songs.json y jamendo_seen.json
  4. Escribe queue.m3u ordenado por distancia de clima
     (hook ML: reemplazá climate_distance() con un modelo cuando esté listo)

Fuentes de verdad:
  • songs.json       — catálogo de mp3s en disco
  • jamendo_seen.json — IDs vistos alguna vez (nunca se vuelven a bajar)
  • played.txt        — qué sonó (escrito por liquidsoap)

Uso:
  python scripts/gestor.py              # chequea y actúa si hace falta
  python scripts/gestor.py --force      # descarga aunque quede cola
  python scripts/gestor.py --status     # estado y salir
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# ── rutas ─────────────────────────────────────────────────────────────────────
ROOT         = Path(__file__).resolve().parent.parent
SONGS_PATH   = ROOT / "songs.json"
QUEUE_PATH   = ROOT / "queue.m3u"
CLIMA_PATH   = ROOT / "clima.json"
SEEN_PATH    = ROOT / "data" / "jamendo_seen.json"
PLAYED_PATH  = ROOT / "logs" / "played.txt"
NOWPLAYING   = ROOT / "logs" / "nowplaying.txt"
MUSIC_DIR    = ROOT / "music"
LOG_PATH     = ROOT / "logs" / "gestor.log"
CLIENT_FILE  = ROOT / "scripts" / ".jamendo_client"

# ── parámetros ────────────────────────────────────────────────────────────────
LOW_WATERMARK  = 6    # si quedan ≤ N sin reproducir → descargar más
BATCH_SIZE     = 25   # canciones a descargar por lote
MAX_DIST       = 0.55 # distancia de clima máxima aceptable (0=exacto, 1=opuesto)
MAX_CATALOG    = 80   # mp3s máximos en disco al mismo tiempo

# ── constantes Jamendo ────────────────────────────────────────────────────────
EMIT_LICENSES = {"cc0", "public-domain", "cc-by", "cc-by-sa", "cc-by-nc", "permission"}
JAMENDO_API   = "https://api.jamendo.com/v3.0/tracks/"
USER_AGENT    = "radio-algoritmica/1.0"
LICENSE_MAP   = {
    "by-nc-sa": "cc-by-nc", "by-nc-nd": None, "by-nc": "cc-by-nc",
    "by-sa":    "cc-by-sa", "by-nd":    None,  "by":    "cc-by",
    "publicdomain": "public-domain", "zero": "cc0",
}
SPEED_ENERGY = {
    "verylow": 0.2, "low": 0.35, "medium": 0.5, "high": 0.7, "veryhigh": 0.85,
}
MOOD_MAP = {
    "happy": "alegre",       "joyful": "alegre",       "upbeat": "alegre",
    "sad": "melancolico",    "melancholic": "melancolico", "bittersweet": "melancolico",
    "calm": "calmo",         "relaxing": "relajado",    "relaxed": "relajado",
    "dreamy": "sonador",     "dreamlike": "sonador",
    "intimate": "intimo",    "mellow": "suave",         "soft": "suave",
    "warm": "calido",        "hopeful": "esperanzador", "nostalgic": "nostalgico",
    "dark": "oscuro",        "mysterious": "misterioso","epic": "epico",
}


# ── logging ───────────────────────────────────────────────────────────────────

def log(msg):
    ts   = datetime.now().isoformat(timespec="seconds")
    line = f"{ts}  {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ── I/O ───────────────────────────────────────────────────────────────────────

def load_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        data = p.read_text(encoding="utf-8").strip()
        return json.loads(data) if data else default
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def atomic_write(path, text):
    p   = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


# ── clima ─────────────────────────────────────────────────────────────────────

def load_clima():
    return load_json(CLIMA_PATH, {})


def climate_distance(song_climate: dict, target_climate: dict) -> float:
    """Distancia normalizada 0..1 entre dos vectores de clima.

    ╔══════════════════════════════════════════════════════╗
    ║  HOOK PARA ML                                        ║
    ║  Reemplazá esta función con un modelo cuando         ║
    ║  esté disponible.  Firma esperada:                   ║
    ║    (song_climate: dict, target_climate: dict)        ║
    ║    -> float  en [0.0, 1.0]                           ║
    ╚══════════════════════════════════════════════════════╝
    """
    a, b = song_climate, target_climate
    d, n = 0.0, 0
    for dim in ("energy", "complexity"):
        av, bv = a.get(dim), b.get(dim)
        if av is None or bv is None:
            continue
        d += abs(float(av) - float(bv))
        n += 1
    for dim in ("mood", "instrumentation"):
        left, right = a.get(dim), b.get(dim)
        if isinstance(left, list) and isinstance(right, list) and left and right:
            inter = set(left) & set(right)
            d    += 1.0 - len(inter) / max(len(left), len(right), 1)
            n    += 1
    for dim in ("texture", "voice", "temporalidad"):
        av, bv = a.get(dim), b.get(dim)
        if av is None or bv is None:
            continue
        d += 0.0 if av == bv else 1.0
        n += 1
    return (d / n) if n else 0.5


def climate_from_track(t: dict) -> dict:
    """Extrae el clima de un track devuelto por la API de Jamendo."""
    mi    = t.get("musicinfo") or {}
    tags  = mi.get("tags") or {}
    speed = mi.get("speed") or "medium"
    eco   = mi.get("acousticelectric") or ""
    voci  = mi.get("vocalinstrumental") or ""
    date  = (t.get("releasedate") or "")[:4]
    try:
        year = int(date)
        temp = "vintage" if year < 2000 else ("clasico" if year < 2016 else "contemporaneo")
    except (TypeError, ValueError):
        temp = "contemporaneo"
    mood = [MOOD_MAP.get(tag.lower(), tag.lower()) for tag in (tags.get("vartags") or [])]
    mood = list(dict.fromkeys(m for m in mood if m))
    return {
        "mood":            mood,
        "texture":         "organica" if eco == "acoustic" else ("electrica" if eco == "electric" else None),
        "energy":          SPEED_ENERGY.get(speed, 0.5),
        "complexity":      0.5,
        "voice":           voci or None,
        "instrumentation": list(tags.get("instruments") or []),
        "temporalidad":    temp,
    }


# ── Jamendo API ───────────────────────────────────────────────────────────────

def get_client_id() -> str | None:
    env = os.environ.get("JAMENDO_CLIENT_ID")
    if env:
        return env
    if CLIENT_FILE.exists():
        t = CLIENT_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    return None


def api_get(client_id: str, params: dict) -> dict:
    q = {"client_id": client_id, "format": "json", "include": "musicinfo"}
    q.update(params)
    url = JAMENDO_API + "?" + urllib.parse.urlencode(q)
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as r:
                doc = json.loads(r.read().decode("utf-8"))
            if doc.get("results"):
                return doc
        except OSError:
            pass
        time.sleep(1.0)
    return {"headers": {"status": "failed"}, "results": []}


def license_short(curl: str | None) -> str | None:
    if not curl:
        return None
    for key, short in LICENSE_MAP.items():
        if key in curl:
            return short
    return None


def sanitize(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "-", s).strip(" .")
    return s or "track"


def _clean(v):
    return html.unescape(v) if v else v


def download_mp3(client_id: str, t: dict) -> Path | None:
    """Descarga el mp3 de un track y devuelve la ruta relativa a ROOT."""
    url = t.get("audiodownload")
    if not url:
        return None
    sep   = "&" if "?" in url else "?"
    url   = url + sep + urllib.parse.urlencode({"client_id": client_id})
    artist = sanitize(_clean(t.get("artist_name") or "desconocido"))
    album  = sanitize(_clean((t.get("album_name") or "single") or "single"))
    title  = sanitize(_clean(t.get("name") or f"track-{t.get('id')}"))
    rel    = Path("music") / artist / album / f"{title} - {t.get('id')}.mp3"
    dest   = ROOT / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp    = dest.with_suffix(".mp3.part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as fh:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
        if tmp.exists() and tmp.stat().st_size > 1024:
            tmp.replace(dest)
            return rel
        if tmp.exists():
            tmp.unlink()
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return None


# ── catálogo ──────────────────────────────────────────────────────────────────

def load_songs() -> list:
    return load_json(SONGS_PATH, [])


def save_songs(songs: list):
    save_json(SONGS_PATH, songs)


def load_seen() -> set:
    data = load_json(SEEN_PATH, [])
    return set(data) if isinstance(data, list) else set()


def save_seen(seen: set):
    save_json(SEEN_PATH, sorted(seen))


# ── historial de reproducción ─────────────────────────────────────────────────

def get_played_paths() -> set:
    """Rutas absolutas que ya se reprodujeron (desde played.txt)."""
    p = Path(PLAYED_PATH)
    if not p.exists():
        return set()
    paths = set()
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = line.strip()
        if raw:
            paths.add(raw.split("|", 1)[0].strip())
    return paths


def get_nowplaying_path() -> str | None:
    """Ruta absoluta del mp3 que está sonando ahora mismo."""
    p = Path(NOWPLAYING)
    if not p.exists():
        return None
    content = p.read_text(encoding="utf-8", errors="replace").strip()
    if content:
        return content.split("|", 1)[0].strip()
    return None


def backup_and_clear_played():
    """Guarda backup de played.txt y lo vacía para la nueva ronda."""
    p = Path(PLAYED_PATH)
    if p.exists() and p.stat().st_size > 0:
        ts     = datetime.now().strftime("%Y%m%d%H%M%S")
        backup = ROOT / "logs" / f"played.bak.{ts}.txt"
        import shutil
        shutil.copy(p, backup)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")


# ── descarga de Jamendo ───────────────────────────────────────────────────────

def fetch_batch(client_id: str, target_climate: dict, seen: set, n: int) -> list:
    """Descarga hasta n canciones que encajen con el clima objetivo.

    Retorna lista de entradas nuevas listas para agregar a songs.json.
    """
    existing_jids = {s.get("jamendo_id") for s in load_songs() if s.get("jamendo_id")}
    PAGE    = min(200, max(n * 4, 50))
    added   = []
    offset  = 0
    max_api = n * 10  # intentos máximos en la API

    while len(added) < n and offset < max_api:
        doc    = api_get(client_id, {"limit": PAGE, "offset": offset, "order": "relevance"})
        tracks = doc.get("results") or []
        if not tracks:
            break

        for t in tracks:
            if len(added) >= n:
                break
            tid = str(t.get("id"))
            if tid in existing_jids or tid in seen:
                continue
            lic = license_short(t.get("license_ccurl"))
            if not lic or lic not in EMIT_LICENSES:
                continue
            if not t.get("audiodownload_allowed"):
                continue

            # ── filtro de clima (ML hook) ──────────────────────────────────
            song_climate = {k: v for k, v in climate_from_track(t).items() if v is not None}
            dist = climate_distance(song_climate, target_climate) if target_climate else 0.5
            if target_climate and dist > MAX_DIST:
                continue

            rel = download_mp3(client_id, t)
            if rel is None:
                continue

            artist = _clean((t.get("artist_name") or "desconocido").strip())
            album  = _clean((t.get("album_name")  or "single").strip()) or "single"
            title  = _clean((t.get("name")         or "").strip()) or f"track-{tid}"

            entry = {
                "id":               f"jm-{tid}",
                "jamendo_id":       tid,
                "file":             str(rel),
                "title":            title,
                "artist":           artist,
                "album":            album,
                "duration_seconds": t.get("duration"),
                "license":          lic,
                "source":           "jamendo",
                "climate":          song_climate,
                "climate_dist":     round(dist, 3),
                "releasedate":      t.get("releasedate"),
                "attribution": {
                    "creator":     artist,
                    "license_url": t.get("license_ccurl"),
                    "track_url":   t.get("shareurl"),
                },
            }
            added.append(entry)
            existing_jids.add(tid)
            seen.add(tid)
            log(f"  + [{lic}] {artist} — {title}  dist={dist:.2f}")

        if len(tracks) < PAGE:
            break
        offset += len(tracks)

    return added


# ── cola ──────────────────────────────────────────────────────────────────────

def write_queue(songs: list, target_climate: dict) -> int:
    """Escribe queue.m3u: primero por distancia de clima, luego espaciado por artista.

    Orden = ML hook: reemplazá sort_key con un modelo de ranking cuando esté listo.
    Firma esperada: score(song: dict, target: dict) -> float  (menor = antes en la cola)
    """
    MIN_ARTIST_GAP = 4  # mínimo de canciones entre dos del mismo artista

    def sort_key(s):
        dist = s.get("climate_dist")
        if dist is None and target_climate:
            dist = climate_distance(s.get("climate", {}), target_climate)
        return dist if dist is not None else 0.5

    # 1. Ordenar por proximidad al clima (mejor primero)
    pool = sorted(
        [s for s in songs if (ROOT / s["file"]).exists()],
        key=sort_key,
    )

    # 2. Intercalar para espaciar artistas — algoritmo greedy:
    #    en cada posición elegimos la mejor canción cuyo artista
    #    no haya aparecido en las últimas MIN_ARTIST_GAP posiciones.
    #    Si no queda ninguna que cumpla, se relaja el constraint.
    ordered     = []
    recent_artists: list[str] = []

    while pool:
        chosen = None
        window = recent_artists[-MIN_ARTIST_GAP:]
        # Intentar respetar el gap
        for i, s in enumerate(pool):
            if s.get("artist") not in window:
                chosen = pool.pop(i)
                break
        # No queda opción dentro del gap: tomar la mejor disponible
        if chosen is None:
            chosen = pool.pop(0)

        ordered.append(chosen)
        recent_artists.append(chosen.get("artist", ""))

    lines = ["#EXTM3U"]
    for s in ordered:
        path = (ROOT / s["file"]).resolve()
        lines.append(f"#EXTINF:,{s['artist']} — {s['title']}")
        lines.append(str(path))
    atomic_write(QUEUE_PATH, "\n".join(lines) + "\n")
    return len(ordered)


# ── limpieza de mp3s ──────────────────────────────────────────────────────────

def delete_songs(songs_to_delete: list, protect_path: str | None):
    """Borra los mp3s de las canciones indicadas (excepto la que suena ahora)."""
    deleted = 0
    for s in songs_to_delete:
        p = (ROOT / s["file"]).resolve()
        if str(p) == protect_path:
            continue
        if p.exists():
            try:
                p.unlink()
                deleted += 1
            except OSError as e:
                log(f"  ! no se pudo borrar {p}: {e}")
    # limpiar carpetas vacías dentro de music/
    for d in sorted(MUSIC_DIR.rglob("*"), reverse=True):
        if d.is_dir():
            try:
                d.rmdir()
            except OSError:
                pass
    if deleted:
        log(f"cleanup: {deleted} mp3s eliminados")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Gestor de la radio musical")
    ap.add_argument("--force",         action="store_true",
                    help="descargar aunque quede cola suficiente")
    ap.add_argument("--status",        action="store_true",
                    help="mostrar estado y salir")
    ap.add_argument("--batch-size",    type=int,   default=BATCH_SIZE,
                    help=f"canciones a descargar por lote (default {BATCH_SIZE})")
    ap.add_argument("--low-watermark", type=int,   default=LOW_WATERMARK,
                    help=f"umbral de cola para disparar descarga (default {LOW_WATERMARK})")
    ap.add_argument("--max-dist",      type=float, default=MAX_DIST,
                    help=f"distancia de clima máxima (default {MAX_DIST})")
    args = ap.parse_args()

    target_climate = load_clima()
    songs          = load_songs()
    played_paths   = get_played_paths()
    now_playing    = get_nowplaying_path()

    # Canciones que todavía no se reprodujeron y tienen mp3 en disco
    unplayed = [
        s for s in songs
        if str((ROOT / s["file"]).resolve()) not in played_paths
        and (ROOT / s["file"]).exists()
    ]

    # ── status ────────────────────────────────────────────────────────────────
    if args.status:
        played = [s for s in songs if str((ROOT / s["file"]).resolve()) in played_paths]
        total_mb = sum(
            (ROOT / s["file"]).stat().st_size / 1e6
            for s in songs
            if (ROOT / s["file"]).exists()
        )
        seen = load_seen()
        print(f"Canciones en catálogo : {len(songs)}")
        print(f"  reproducidas        : {len(played)}")
        print(f"  sin reproducir      : {len(unplayed)}")
        print(f"Espacio en disco      : {total_mb:.1f} MB")
        print(f"Jamendo IDs vistos    : {len(seen)}")
        print(f"Sonando ahora         : {now_playing or '(nada)'}")
        print(f"Clima objetivo        : {target_climate}")
        return 0

    log(f"gestor: {len(unplayed)} sin reproducir de {len(songs)} en catálogo")

    # ── ¿hace falta descargar? ────────────────────────────────────────────────
    if len(unplayed) > args.low_watermark and not args.force:
        # Cola suficiente — solo refrescar queue.m3u por si acaso
        n = write_queue(unplayed, target_climate)
        log(f"gestor: cola suficiente, queue.m3u actualizado ({n} canciones).")
        return 0

    # ── descargar nuevo lote ──────────────────────────────────────────────────
    client_id = get_client_id()
    if not client_id:
        log("ERROR: falta JAMENDO_CLIENT_ID (env o scripts/.jamendo_client)")
        return 1

    log(f"gestor: cola baja ({len(unplayed)} ≤ {args.low_watermark}). "
        f"Descargando lote de {args.batch_size}...")

    seen      = load_seen()
    new_songs = fetch_batch(client_id, target_climate, seen, args.batch_size)
    log(f"gestor: {len(new_songs)} canciones nuevas descargadas")

    if not new_songs and not unplayed:
        log("ERROR: sin canciones disponibles.")
        return 1

    # ── borrar las ya reproducidas para liberar disco ─────────────────────────
    played_songs = [
        s for s in songs
        if str((ROOT / s["file"]).resolve()) in played_paths
    ]
    if played_songs:
        log(f"gestor: borrando {len(played_songs)} mp3s ya reproducidos...")
        delete_songs(played_songs, protect_path=now_playing)

    # ── nuevo catálogo: unplayed + recién descargadas ─────────────────────────
    songs = unplayed + new_songs

    # Seguridad: si por alguna razón superamos el máximo, recortar las más viejas
    if len(songs) > MAX_CATALOG:
        songs = songs[-MAX_CATALOG:]

    save_songs(songs)
    save_seen(seen)
    backup_and_clear_played()

    # ── escribir cola ordenada por clima ──────────────────────────────────────
    n = write_queue(songs, target_climate)
    log(f"gestor: queue.m3u escrito con {n} canciones. Ciclo completado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
