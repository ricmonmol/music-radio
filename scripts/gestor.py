#!/usr/bin/env python3
"""gestor.py — ciclo de vida de la radio musical.

Flujo (simple):
  1. Lee clima.json  →  qué música buscar
  2. Marca como escuchadas (`heard`) las canciones que aparecen en played.txt
     (referencia permanente: una escuchada nunca vuelve a la cola)
  3. Si quedan pocas sin reproducir (≤ LOW_WATERMARK):
       a. Descarga un lote de Jamendo filtrado por climate_distance
       b. Descarta las escuchadas: las saca del catálogo y borra sus mp3s
       c. Actualiza songs.json y jamendo_seen.json
  4. Recién ahí escribe queue.m3u, y solo si el contenido cambió
     (hook ML: reemplazá climate_distance() con un modelo cuando esté listo)

Fuentes de verdad:
  • songs.json         — catálogo de mp3s en disco (campo `heard` = escuchadas)
  • jamendo_seen.json  — IDs vistos alguna vez (nunca se vuelven a bajar)
  • played.txt         — registro crudo de lo que sonó (lo mantiene el panel web)

Uso:
  python scripts/gestor.py              # chequea y actúa si hace falta
  python scripts/gestor.py --force      # descarga aunque quede cola
  python scripts/gestor.py --status     # estado y salir
"""
import argparse
import fcntl
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
    "cheerful": "alegre",    "positive": "alegre",     "optimistic": "alegre",
    "sad": "melancolico",    "melancholic": "melancolico", "bittersweet": "melancolico",
    "melancholy": "melancolico", "sorrowful": "melancolico", "gloomy": "melancolico",
    "longing": "melancolico","yearning": "melancolico",
    "wistful": "nostalgico", "nostalgia": "nostalgico", "nostalgic": "nostalgico",
    "calm": "calmo",         "relaxing": "relajado",    "relaxed": "relajado",
    "peaceful": "calmo",     "serene": "calmo",         "soothing": "relajado",
    "dreamy": "sonador",     "dreamlike": "sonador",    "ethereal": "sonador",
    "daydreaming": "sonador","introspective": "intimo", "contemplative": "intimo",
    "intimate": "intimo",    "reflective": "intimo",    "thoughtful": "intimo",
    "pensive": "intimo",
    "mellow": "suave",       "soft": "suave",           "gentle": "suave",
    "tender": "suave",       "quiet": "suave",          "delicate": "suave",
    "warm": "calido",        "cozy": "calido",          "inviting": "calido",
    "hopeful": "esperanzador","uplifting": "esperanzador", "inspiring": "esperanzador",
    "dark": "oscuro",        "mysterious": "misterioso","epic": "epico",
    "emotional": "emotivo",  "romantic": "romantico",   "passionate": "intenso",
}

# Vocabulario canónico de instrumentos (Jamendo lo entrega en inglés).
INSTR_MAP = {
    "acoustic guitar": "guitarra", "acousticguitar": "guitarra", "acustic guitar": "guitarra",
    "guitar": "guitarra",          "classical guitar": "guitarra", "nylon guitar": "guitarra",
    "electricguitar": "guitarra electrica", "electric guitar": "guitarra electrica",
    "bass": "bajo", "bassguitar": "bajo", "bass guitar": "bajo", "uprightbass": "bajo",
    "drums": "bateria", "drum set": "bateria",
    "percussion": "percusion", "tambourine": "pandereta", "xylophone": "xilofono", "marimba": "marimba",
    "piano": "piano",
    "keyboard": "teclado", "keyboards": "teclado",
    "synthesizer": "sintetizador", "synth": "sintetizador",
    "organ": "organo",
    "strings": "cuerdas", "string section": "cuerdas",
    "violin": "cuerdas", "viola": "cuerdas", "cello": "cuerdas", "double bass": "cuerdas",
    "harp": "arpa",
    "flute": "flauta", "pan flute": "flauta",
    "saxophone": "saxofon", "sax": "saxofon",
    "trumpet": "trompeta", "trombone": "trombon",
    "banjo": "banjo", "mandolin": "mandolina", "ukulele": "ukelele",
    "harmonica": "armonica", "accordion": "acordeon",
    "lute": "laud",
    "bells": "campanas", "glockenspiel": "campanas",
    "vocal": "voz", "vocals": "voz", "voice": "voz", "singing": "voz",
    "chorus": "coros", "choir": "coros",
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


def write_in_place(path, text):
    """Escribe truncando el MISMO inode (sin os.replace).

    Es clave para liquidsoap: con reload_mode="watch" mira el inode original;
    si reemplazamos el archivo por otro inode (os.replace), deja de notar
    los cambios y la radio queda clavada en la cola vieja.
    """
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


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
    # Score mixto y distributivo: clima y estilo pesan por separado.
    # Los pesos se leen de clima.json (peso_clima / peso_genero), fallback 60/40.
    w_clima  = float(b.get("peso_clima", 0.6))
    w_genero = float(b.get("peso_genero", 0.4))
    if w_clima + w_genero <= 0:
        w_clima, w_genero = 0.6, 0.4

    # Valores continuos en [0,1] (pasos de 0.1) para dimensiones categóricas.
    # Esto evita el 0/1 binario: un track "electrica" o "instrumental" suma
    # una penalización parcial, no máxima. Valores fuera del mapa o faltantes
    # se omiten (nunca se castiga por falta de datos).
    TEX = {"organica": 0.1, "electrica": 0.9}
    VOZ = {"vocal": 0.1, "instrumental": 0.7}
    ERA = {"vintage": 0.1, "clasico": 0.5, "contemporaneo": 0.9}

    a = song_climate
    dims = []
    for dim in ("energy", "complexity"):
        av, bv = a.get(dim), b.get(dim)
        if av is None or bv is None:
            continue
        dims.append(abs(float(av) - float(bv)))
    for dim in ("mood", "instrumentation"):
        left, right = a.get(dim), b.get(dim)
        if isinstance(left, list) and isinstance(right, list) and left and right:
            dims.append(1.0 - len(set(left) & set(right)) / max(len(left), len(right), 1))
    for dim, scale in (("texture", TEX), ("voice", VOZ), ("temporalidad", ERA)):
        av, bv = a.get(dim), b.get(dim)
        if av not in scale or bv not in scale:
            continue
        dims.append(abs(scale[av] - scale[bv]))
    d_clima = (sum(dims) / len(dims)) if dims else 0.5

    # Distancia de estilo: 0 si el track toca algún estilo del target, 1 si no.
    # Si el target no define estilos, el score queda 100% clima.
    tg = set(b.get("genero") or [])
    if tg:
        d_genero = 0.0 if set(a.get("genero") or []) & tg else 1.0
        return w_clima * d_clima + w_genero * d_genero
    return d_clima


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
    mood = [MOOD_MAP.get(tag.lower()) for tag in (tags.get("vartags") or [])]
    mood = list(dict.fromkeys(m for m in mood if m))
    genero = list(dict.fromkeys(g.lower() for g in (tags.get("genres") or [])))
    instruments = [INSTR_MAP.get(i.strip().lower(), i.strip().lower())
                   for i in (tags.get("instruments") or [])]
    instruments = list(dict.fromkeys(i for i in instruments if i))
    return {
        "mood":            mood,
        "genero":          genero,
        "texture":         "organica" if eco == "acoustic" else ("electrica" if eco == "electric" else None),
        "energy":          SPEED_ENERGY.get(speed, 0.5),
        "complexity":      0.5,
        "voice":           voci or None,
        "instrumentation": instruments,
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

def acquire_lock() -> object | None:
    """Lock de instancia única: evita dos gestores corriendo a la vez."""
    data = SEEN_PATH.parent
    data.mkdir(parents=True, exist_ok=True)
    fh = open(data / "gestor.lock", "w", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        return None


def consume_played(songs: list) -> int:
    """Marca como `heard` las canciones que aparecen en played.txt (o sin archivo).

    Es la referencia permanente de lo escuchado: una canción marcada `heard`
    nunca vuelve a entrar en la cola ni se vuelve a bajar (jamendo_seen).
    No borra played.txt: ese archivo lo mantiene el historial del panel web y
    cleanup_logs.sh controla su tamaño. Devuelve cuántas marcó nuevas.
    """
    p = Path(PLAYED_PATH)
    if not p.exists() or p.stat().st_size == 0:
        return 0
    played = set()
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = line.strip()
        if raw:
            played.add(raw.split("|", 1)[0].strip())
    marked, changed = 0, False
    for s in songs:
        heard = bool(s.get("heard")) \
            or str((ROOT / s["file"]).resolve()) in played \
            or not (ROOT / s["file"]).exists()
        if heard and not s.get("heard"):
            s["heard"] = True
            marked += 1
            changed = True
    if changed:
        save_songs(songs)
    return marked


def sweep_orphans(songs: list, protect_path: str | None):
    """Borra mp3s en music/ que ya no están en el catálogo ni suenan ahora."""
    keep = {str((ROOT / s["file"]).resolve()) for s in songs}
    removed = 0
    for p in MUSIC_DIR.rglob("*.mp3"):
        if str(p.resolve()) in keep or str(p.resolve()) == protect_path:
            continue
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    for d in sorted(MUSIC_DIR.rglob("*"), reverse=True):
        if d.is_dir():
            try:
                d.rmdir()
            except OSError:
                pass
    if removed:
        log(f"cleanup: {removed} mp3s huérfanos eliminados")


def get_nowplaying_path() -> str | None:
    """Ruta absoluta del mp3 que está sonando ahora mismo."""
    p = Path(NOWPLAYING)
    if not p.exists():
        return None
    content = p.read_text(encoding="utf-8", errors="replace").strip()
    if content:
        return content.split("|", 1)[0].strip()
    return None


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

def write_queue(songs: list, target_climate: dict) -> tuple[int, bool]:
    """Escribe queue.m3u: primero por distancia de clima, luego espaciado por artista.

    Solo pisa el archivo si el contenido cambió (así liquidsoap recarga solo
    cuando hay novedades reales). Devuelve (n_canciones, hubo_cambio).

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
    text = "\n".join(lines) + "\n"

    changed = True
    try:
        if QUEUE_PATH.read_text(encoding="utf-8", errors="replace") == text:
            changed = False
    except OSError:
        changed = True
    if changed:
        write_in_place(QUEUE_PATH, text)
    return len(ordered), changed


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

    lock = acquire_lock()
    if lock is None:
        print("gestor: ya hay una instancia corriendo; saliendo.")
        return 0

    target_climate = load_clima()
    songs          = load_songs()
    now_playing    = get_nowplaying_path()

    # Registrar lo que sonó (referencia permanente: campo `heard` en songs.json)
    marked = consume_played(songs)
    if marked:
        log(f"gestor: {marked} canciones marcadas como escuchadas")

    def exists(song):
        return (ROOT / song["file"]).exists()

    # Canciones todavía sin reproducir y con mp3 en disco
    unplayed = [s for s in songs if not s.get("heard") and exists(s)]

    # ── status ────────────────────────────────────────────────────────────────
    if args.status:
        heard   = [s for s in songs if s.get("heard")]
        missing = [s for s in songs if not exists(s)]
        total_mb = sum(
            (ROOT / s["file"]).stat().st_size / 1e6
            for s in songs if exists(s)
        )
        seen = load_seen()
        print(f"Canciones en catálogo : {len(songs)}")
        print(f"  escuchadas          : {len(heard)}")
        print(f"  sin reproducir      : {len(unplayed)}")
        print(f"  sin archivo (drop)  : {len(missing)}")
        print(f"Espacio en disco      : {total_mb:.1f} MB")
        print(f"Jamendo IDs vistos    : {len(seen)}")
        print(f"Sonando ahora         : {now_playing or '(nada)'}")
        print(f"Clima objetivo        : {target_climate}")
        return 0

    log(f"gestor: {len(unplayed)} sin reproducir de {len(songs)} en catálogo")

    # ── ¿hace falta descargar? ────────────────────────────────────────────────
    # Si la cola ya tiene canciones: no tocar nada (reescribir en cada track
    # es lo que rompía el reload de liquidsoap).
    queue_vacio = not QUEUE_PATH.exists() or QUEUE_PATH.stat().st_size == 0
    if len(unplayed) > args.low_watermark and not args.force:
        if queue_vacio:
            n, _ = write_queue(unplayed, target_climate)
            log(f"gestor: queue.m3u vacío, escrito con {n} canciones.")
        else:
            log("gestor: cola suficiente, sin cambios.")
        sweep_orphans(songs, now_playing)
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
        # Sin nuevas y sin cola: no tocar nada, se reintenta en el próximo ciclo.
        log("ERROR: sin canciones disponibles (se reintentará).")
        return 1

    # ── sacar las escuchadas (o sin archivo) y liberar disco ──────────────────
    salientes = [s for s in songs if s.get("heard") or not exists(s)]
    if salientes:
        log(f"gestor: descartando {len(salientes)} canciones escuchadas...")
        delete_songs(salientes, protect_path=now_playing)

    # ── nuevo catálogo: unplayed + recién descargadas ─────────────────────────
    songs = unplayed + new_songs

    # Seguridad: si por alguna razón superamos el máximo, recortar las más viejas
    if len(songs) > MAX_CATALOG:
        songs = songs[-MAX_CATALOG:]

    save_songs(songs)
    save_seen(seen)

    # ── escribir cola ordenada por clima (solo si cambió) ──────────────────────
    n, cambio = write_queue(songs, target_climate)
    if cambio:
        log(f"gestor: queue.m3u escrito con {n} canciones. Ciclo completado.")
    else:
        log(f"gestor: cola sin cambios ({n} canciones).")
    sweep_orphans(songs, now_playing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
