#!/usr/bin/env python3
"""gestor.py — ciclo de vida de la radio musical.

Flujo:
  1. Lee clima.json  →  qué música buscar
  2. Consume played.txt/played.tsv y marca las canciones al inicio
  3. Si quedan pocas sin reproducir (≤ LOW_WATERMARK), descarga un lote de Jamendo
  4. Conserva las pistas escuchadas en archive/music/ para el fallback offline
  5. Escribe queue.m3u y queue.offline.m3u solo cuando cambia el contenido

Fuentes de verdad:
  • songs.json         — catálogo de mp3s y flags de reproducción
  • data/playback.json — timestamps y conteos durables
  • data/jamendo_seen.json — IDs vistos alguna vez (nunca se vuelven a bajar)
  • logs/played.txt    — historial legible del panel web

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
import shutil
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
OFFLINE_QUEUE_PATH = ROOT / "queue.offline.m3u"
CLIMA_PATH   = ROOT / "clima.json"
SEEN_PATH    = ROOT / "data" / "jamendo_seen.json"
STATE_PATH   = ROOT / "data" / "playback.json"
PLAYED_PATH  = ROOT / "logs" / "played.txt"
PLAYED_TS_PATH = ROOT / "logs" / "played.tsv"
NOWPLAYING   = ROOT / "logs" / "nowplaying.txt"
MUSIC_DIR    = ROOT / "music"
ARCHIVE_DIR  = ROOT / "archive" / "music"
LOG_PATH     = ROOT / "logs" / "gestor.log"
CLIENT_FILE  = ROOT / "scripts" / ".jamendo_client"

# ── parámetros ────────────────────────────────────────────────────────────────
LOW_WATERMARK  = 6
BATCH_SIZE     = 25
MAX_DIST       = 0.55
GENERO_PENALTY_CAP = 0.6
OFFLINE_QUEUE_SIZE = 200
PROTECTED_QUEUE_ITEMS = 2
HISTORY_RETENTION_DAYS = 7
NOWPLAYING_MAX_AGE = 900
SOURCE_ORDERS = ["relevance", "releasedate_desc", "popularity_total"]
SOURCE_MAX_TAGS = 4
SOURCE_MAX_PAGES = 6
MAX_TRACKS_PER_ARTIST = 2

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
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)


def write_in_place(path, text):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def playlist_entries(text):
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def playlist_needs_update(path, text):
    try:
        current = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return playlist_entries(current) != playlist_entries(text)


def write_playlist(path, text):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = text.encode("utf-8")
    try:
        old_size = p.stat().st_size
    except OSError:
        old_size = 0
    if old_size > len(payload):
        padding_size = old_size - len(payload)
        if padding_size == 1:
            padding = b"#"
        elif padding_size == 2:
            padding = b"#\n"
        else:
            padding = b"# " + b" " * (padding_size - 3) + b"\n"
    else:
        padding = b""
    data = payload + padding
    fd = os.open(p, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("no se pudo escribir la playlist")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def path_from_value(value) -> Path:
    raw = str(value or "")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme == "file":
        raw = urllib.parse.unquote(parsed.path)
    elif parsed.scheme in {"http", "https"}:
        raw = parsed.path
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def path_string(path) -> str:
    p = Path(path)
    try:
        return str(p.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(p)


def path_identity(value) -> str:
    try:
        return str(path_from_value(value).resolve())
    except OSError:
        return str(path_from_value(value))


def track_key(value) -> str:
    if isinstance(value, dict):
        jamendo_id = str(value.get("jamendo_id") or "")
        if jamendo_id:
            return f"jamendo:{jamendo_id}"
        value = value.get("file", "")
    name = path_from_value(value).name
    match = re.search(r" - (\d+)$", Path(name).stem)
    if match:
        return f"jamendo:{match.group(1)}"
    return f"file:{name}"


def state_default() -> dict:
    return {"version": 1, "mode": "online", "tracks": {}, "source": {}}


def load_state() -> dict:
    data = load_json(STATE_PATH, state_default())
    if not isinstance(data, dict):
        return state_default()
    data.setdefault("version", 1)
    data.setdefault("mode", "online")
    data.setdefault("tracks", {})
    if not isinstance(data["tracks"], dict):
        data["tracks"] = {}
    # Cursor de ingesta: por qué tag de clima se empieza y hasta qué offset se
    # leyó cada consulta, para no releer siempre la misma cabeza del ranking.
    data.setdefault("source", {})
    if not isinstance(data["source"], dict):
        data["source"] = {}
    data["source"].setdefault("tag_index", 0)
    data["source"].setdefault("offsets", {})
    if not isinstance(data["source"]["offsets"], dict):
        data["source"]["offsets"] = {}
    return data


def save_state(state: dict):
    save_json(STATE_PATH, state)


def file_mtime(path) -> str:
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return "1970-01-01T00:00:00+00:00"


def parse_time(value) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def queue_paths(path) -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    result = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            result.append(path_identity(value))
    return result


def append_played_event(path: str):
    p = Path(PLAYED_TS_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(f"{now_iso()}|{path}\n")


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

    # Distancia de estilo: gradual. Mide qué fracción de los estilos del track
    # cae dentro del target, así un tag ajeno (p.ej. "electronic" en un track
    # etiquetado "rock,electronic") penaliza en vez de anular la penalización.
    # El cap evita que un track con muchos tags quede castigado de más.
    # Si el target no define estilos, el score queda 100% clima.
    tg = set(b.get("genero") or [])
    if tg:
        ag = list(a.get("genero") or [])
        if not ag:
            d_genero = 1.0
        else:
            d_genero = min(1.0 - len(set(ag) & tg) / len(ag), GENERO_PENALTY_CAP)
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
    headers: dict = {}
    answered = False
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as response:
                doc = json.loads(response.read().decode("utf-8"))
            results = doc.get("results") or []
            headers = doc.get("headers") or {}
            answered = True
            if results:
                return {
                    "ok": True,
                    "results": results,
                    "headers": headers,
                    "empty": False,
                }
            # Cuerpo vacío con HTTP 200: así responde la API cuando limita el
            # ritmo. No es error de red, pero tampoco es "no hay más", así que
            # se reintenta antes de dar la ventana por agotada.
        except (OSError, ValueError):
            headers = {}
        if attempt < 3:
            time.sleep(1.5 * (attempt + 1))
    return {
        "ok": answered,
        "network_error": not answered,
        "results": [],
        "headers": headers,
        "empty": True,
    }


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
    data = load_json(SONGS_PATH, [])
    return data if isinstance(data, list) else []


def save_songs(songs: list):
    save_json(SONGS_PATH, songs)


def load_seen() -> set:
    data = load_json(SEEN_PATH, [])
    if not isinstance(data, list):
        return set()
    return {str(value) for value in data if str(value)}


def save_seen(seen: set):
    save_json(SEEN_PATH, sorted(seen))


# ── historial de reproducción ─────────────────────────────────────────────────

def acquire_lock(wait: bool = False) -> object | None:
    data = SEEN_PATH.parent
    data.mkdir(parents=True, exist_ok=True)
    fh = open(data / "gestor.lock", "w", encoding="utf-8")
    flags = fcntl.LOCK_EX
    if not wait:
        flags |= fcntl.LOCK_NB
    try:
        fcntl.flock(fh, flags)
        return fh
    except OSError:
        fh.close()
        return None


def get_nowplaying_path() -> str | None:
    p = Path(NOWPLAYING)
    if not p.exists():
        return None
    content = p.read_text(encoding="utf-8", errors="replace").strip()
    if content:
        return content.split("|", 1)[0].strip()
    return None


def nowplaying_is_current() -> bool:
    try:
        age = time.time() - Path(NOWPLAYING).stat().st_mtime
    except OSError:
        return False
    return age <= NOWPLAYING_MAX_AGE


def song_file(song: dict) -> Path:
    return path_from_value(song.get("file", ""))


def metadata_from_path(path) -> dict:
    p = Path(path)
    try:
        rel = p.relative_to(ROOT)
        parts = rel.parts
    except ValueError:
        parts = p.parts
    stem = p.stem
    match = re.search(r" - (\d+)$", stem)
    jamendo_id = match.group(1) if match else None
    title = stem[:match.start()].strip() if match else stem
    artist = "desconocido"
    album = "single"
    if parts[:2] == ("archive", "music") and len(parts) >= 5:
        artist = parts[2]
        album = parts[3]
    elif parts[:1] == ("music",) and len(parts) >= 3:
        artist = parts[1]
        album = parts[2]
    return {
        "id": f"jm-{jamendo_id}" if jamendo_id else track_key(p),
        "jamendo_id": jamendo_id,
        "file": path_string(p),
        "title": title or "desconocido",
        "artist": artist or "desconocido",
        "album": album or "single",
        "source": "jamendo" if jamendo_id else "local",
        "heard": True,
        "archived": parts[:2] == ("archive", "music"),
        "last_played_at": file_mtime(p),
    }


def find_song(songs: list, value) -> dict | None:
    target_path = path_identity(value)
    target_key = track_key(value)
    for song in songs:
        if path_identity(song.get("file", "")) == target_path:
            return song
    for song in songs:
        if track_key(song) == target_key:
            return song
    return None


def update_song_state(song: dict, state: dict, timestamp: str) -> bool:
    key = track_key(song)
    record = state["tracks"].setdefault(key, {})
    record["file"] = path_string(song_file(song))
    record["last_played"] = timestamp
    record["play_count"] = int(record.get("play_count", 0)) + 1
    song["last_played_at"] = timestamp
    was_heard = bool(song.get("heard"))
    song["heard"] = True
    return not was_heard


def ensure_state_records(songs: list, state: dict) -> bool:
    changed = False
    for song in songs:
        if not song.get("heard"):
            continue
        key = track_key(song)
        record = state["tracks"].setdefault(key, {})
        if not record.get("last_played"):
            record["last_played"] = song.get("last_played_at") or file_mtime(song_file(song))
            changed = True
        if not record.get("file"):
            record["file"] = path_string(song_file(song))
            changed = True
        if not song.get("last_played_at"):
            song["last_played_at"] = record["last_played"]
            changed = True
    return changed


def adopt_archive(songs: list, state: dict) -> tuple[bool, set[str]]:
    if not ARCHIVE_DIR.exists():
        return False, set()
    by_key = {track_key(song): song for song in songs}
    changed = False
    seen = set()
    for path in sorted(ARCHIVE_DIR.rglob("*.mp3")):
        key = track_key(path)
        seen.add(key.removeprefix("jamendo:"))
        song = by_key.get(key)
        if song is None:
            song = metadata_from_path(path)
            songs.append(song)
            by_key[key] = song
            changed = True
        elif not song.get("heard"):
            song["heard"] = True
            changed = True
        if not song.get("file") or not song_file(song).exists():
            song["file"] = path_string(path)
            song["archived"] = True
            changed = True
        record = state["tracks"].setdefault(key, {})
        if not record.get("last_played"):
            record["last_played"] = song.get("last_played_at") or file_mtime(path)
            record["file"] = song["file"]
            changed = True
    return changed, seen


def consume_played(songs: list, state: dict) -> int:
    values = []
    timestamps = {}
    tsv = Path(PLAYED_TS_PATH)
    if tsv.exists():
        for line in tsv.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            raw_timestamp, separator, value = line.partition("|")
            if not separator or not value.strip():
                continue
            value = value.strip()
            values.append(value)
            timestamps[path_identity(value)] = raw_timestamp.strip()

    legacy = Path(PLAYED_PATH)
    if legacy.exists():
        values.extend(
            line.split("|", 1)[0].strip()
            for line in legacy.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        )

    current = get_nowplaying_path()
    if current:
        values.append(current)

    marked = 0
    seen_values = set()
    current_identity = path_identity(current) if current and nowplaying_is_current() else None
    for value in values:
        identity = path_identity(value)
        if identity in seen_values:
            continue
        seen_values.add(identity)
        song = find_song(songs, value)
        if song is None:
            continue
        key = track_key(song)
        record = state["tracks"].setdefault(key, {})
        is_current = identity == current_identity
        timestamp = now_iso() if is_current else timestamps.get(identity)
        if not timestamp:
            timestamp = song.get("last_played_at") or file_mtime(song_file(song))
        if song.get("heard") and record.get("last_played") and not is_current:
            if parse_time(timestamp) <= parse_time(record["last_played"]):
                continue
        if not song.get("heard"):
            marked += 1
        update_song_state(song, state, timestamp)
    return marked


def mark_started(value: str) -> int:
    lock = acquire_lock()
    if lock is None:
        return 0
    try:
        songs = load_songs()
        state = load_state()
        ensure_state_records(songs, state)
        song = find_song(songs, value)
        if song is None and path_from_value(value).exists():
            song = metadata_from_path(path_from_value(value))
            songs.append(song)
        if song is not None:
            update_song_state(song, state, now_iso())
            save_songs(songs)
        save_state(state)
        append_played_event(value)
        if state.get("mode") == "offline":
            write_offline_queue(songs, state, protected_paths(value))
        return 0
    finally:
        lock.close()


def archive_path_for(path: Path) -> Path:
    try:
        relative = path.resolve().relative_to(MUSIC_DIR.resolve())
    except ValueError:
        return ARCHIVE_DIR / path.name
    return ARCHIVE_DIR / relative


def archive_songs(songs: list, state: dict, protect_paths: set[str]) -> tuple[list, int]:
    result = []
    archived = 0
    for song in songs:
        source = song_file(song)
        identity = path_identity(source)
        try:
            source.resolve().relative_to(ARCHIVE_DIR.resolve())
            song["archived"] = True
            result.append(song)
            continue
        except ValueError:
            pass
        if not song.get("heard") or not source.exists() or identity in protect_paths:
            result.append(song)
            continue
        destination = archive_path_for(source)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                source.unlink()
            else:
                shutil.move(str(source), str(destination))
            song["file"] = path_string(destination)
            song["archived"] = True
            record = state["tracks"].setdefault(track_key(song), {})
            record["file"] = song["file"]
            record["archived"] = True
            archived += 1
        except OSError as exc:
            log(f"  ! no se pudo archivar {source}: {exc}")
            result.append(song)
            continue
        result.append(song)
    return result, archived


def archive_orphans(songs: list, state: dict, protect_paths: set[str]) -> tuple[int, set[str]]:
    known = {path_identity(song.get("file", "")) for song in songs}
    archived = 0
    archived_ids = set()
    if not MUSIC_DIR.exists():
        return 0, archived_ids
    for path in sorted(MUSIC_DIR.rglob("*.mp3")):
        identity = path_identity(path)
        if identity in known or identity in protect_paths:
            continue
        destination = archive_path_for(path)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                path.unlink()
            else:
                shutil.move(str(path), str(destination))
            song = metadata_from_path(destination)
            songs.append(song)
            state["tracks"].setdefault(track_key(song), {
                "file": song["file"],
                "last_played": file_mtime(destination),
                "play_count": 0,
            })
            key = track_key(song)
            if key.startswith("jamendo:"):
                archived_ids.add(key.removeprefix("jamendo:"))
            archived += 1
        except OSError:
            pass
    return archived, archived_ids


def cleanup_playback_events() -> int:
    p = Path(PLAYED_TS_PATH)
    if not p.exists():
        return 0
    cutoff = datetime.now().astimezone().timestamp() - HISTORY_RETENTION_DAYS * 86400
    kept = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = line.split("|", 1)[0].strip()
        try:
            if datetime.fromisoformat(raw).timestamp() >= cutoff:
                kept.append(line)
        except ValueError:
            continue
    text = "\n".join(kept)
    if text:
        text += "\n"
    write_in_place(p, text)
    return len(kept)


# ── descarga de Jamendo ───────────────────────────────────────────────────────

def _harvest(client_id: str, tracks: list, target_climate: dict, seen: set,
             existing_jids: set, added: list, per_artist: dict, n: int, max_dist: float):
    """Filtra una página de resultados y descarga lo que sirve. Muta added/seen."""
    for track in tracks:
        if len(added) >= n:
            return
        tid = str(track.get("id") or "")
        if not tid or tid in existing_jids:
            continue
        lic = license_short(track.get("license_ccurl"))
        if not lic or lic not in EMIT_LICENSES:
            continue
        if not track.get("audiodownload_allowed"):
            continue
        artist = _clean((track.get("artist_name") or "desconocido").strip())
        # Un mismo artista publica bloques de 20+ tracks en Jamendo; sin este
        # tope un solo tag se queda con medio lote y la radio suena a un
        # artista repetido durante días.
        if per_artist.get(artist, 0) >= MAX_TRACKS_PER_ARTIST:
            continue
        song_climate = {
            key: value
            for key, value in climate_from_track(track).items()
            if value is not None
        }
        dist = climate_distance(song_climate, target_climate) if target_climate else 0.5
        if target_climate and dist > max_dist:
            continue
        rel = download_mp3(client_id, track)
        if rel is None:
            continue
        album = _clean((track.get("album_name") or "single").strip()) or "single"
        title = _clean((track.get("name") or "").strip()) or f"track-{tid}"
        entry = {
            "id": f"jm-{tid}",
            "jamendo_id": tid,
            "file": str(rel),
            "title": title,
            "artist": artist,
            "album": album,
            "duration_seconds": track.get("duration"),
            "license": lic,
            "source": "jamendo",
            "climate": song_climate,
            "climate_dist": round(dist, 3),
            "releasedate": track.get("releasedate"),
            "attribution": {
                "creator": artist,
                "license_url": track.get("license_ccurl"),
                "track_url": track.get("shareurl"),
            },
        }
        added.append(entry)
        existing_jids.add(tid)
        seen.add(tid)
        per_artist[artist] = per_artist.get(artist, 0) + 1
        log(f"  + [{lic}] {artist} — {title}  dist={dist:.2f}")


def _sweep(client_id: str, target_climate: dict, seen: set, existing_jids: set,
           added: list, per_artist: dict, n: int, max_dist: float, page_size: int,
           tag: str | None, order: str, offset: int) -> tuple[int, str]:
    """Recorre una consulta (tag + orden) desde offset y devuelve el siguiente.

    Motivo devuelto: "ok" ventana leída, "empty" sin material, "network_error".
    Una página vacía no corta el barrido: la API devuelve 200 con cuerpo vacío
    cuando limita el ritmo, así que recién dos vacías seguidas se toman por
    ventana agotada.
    """
    consecutive_empty = 0
    for _ in range(SOURCE_MAX_PAGES):
        if len(added) >= n:
            break
        params = {"limit": page_size, "offset": offset, "order": order}
        if tag:
            params["fuzzytags"] = tag
        doc = api_get(client_id, params)
        if not doc.get("ok"):
            return offset, "network_error"
        tracks = doc.get("results") or []
        if not tracks:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                return offset, "empty"
            continue
        consecutive_empty = 0
        _harvest(client_id, tracks, target_climate, seen, existing_jids, added, per_artist, n, max_dist)
        if len(tracks) < page_size:
            return 0, "empty"
        offset += len(tracks)
    return offset, "ok"


def fetch_batch(client_id: str, target_climate: dict, seen: set, state: dict,
                n: int, max_dist: float) -> tuple[list, str]:
    existing_jids = {
        str(song.get("jamendo_id"))
        for song in load_songs()
        if song.get("jamendo_id")
    }
    existing_jids.update(str(value) for value in seen)
    page_size = min(200, max(n * 4, 50))
    added = []
    per_artist: dict = {}
    network_error = False
    received_response = False

    src = state.setdefault("source", {})
    offsets = src.setdefault("offsets", {})
    # Cada tag de clima abre su propia ventana de resultados, así que el estilo
    # objetivo también multiplica el material alcanzable.
    tags = [str(t) for t in (target_climate.get("genero") or [])] if target_climate else []
    start = int(src.get("tag_index", 0) or 0) % max(1, len(tags))
    tried: list[str] = []
    reasons: dict[str, int] = {}

    def run(tag, order):
        nonlocal network_error, received_response
        key = f"{tag or '-'}|{order}"
        offset = int(offsets.get(key, 0) or 0)
        offset, reason = _sweep(client_id, target_climate, seen, existing_jids,
                                added, per_artist, n, max_dist, page_size, tag,
                                order, offset)
        offsets[key] = offset
        reasons[reason] = reasons.get(reason, 0) + 1
        if reason == "network_error":
            network_error = True
        else:
            received_response = True
        return reason

    if tags:
        for step in range(min(SOURCE_MAX_TAGS, len(tags))):
            tag = tags[(start + step) % len(tags)]
            tried.append(tag)
            for order in SOURCE_ORDERS:
                if len(added) >= n:
                    break
                run(tag, order)
            if len(added) >= n or network_error:
                break
        src["tag_index"] = (start + len(tried)) % len(tags)
    # Sin material por estilo objetivo se cae a la búsqueda abierta, para que la
    # radio nunca quede muda aunque el catálogo de un estilo esté agotado.
    if not added and not network_error:
        for order in SOURCE_ORDERS:
            if len(added) >= n:
                break
            run(None, order)
        tried.append("sin filtro de estilo")

    if tried:
        log(f"gestor: consulta con {', '.join(tried)}; motivos={reasons or '{}'}; "
            f"candidatos nuevos acumulados={len(added)}/{n}")
    if network_error and not received_response:
        return added, "network_error"
    if len(added) >= n:
        return added, "ok"
    return added, "exhausted"


# ── cola ──────────────────────────────────────────────────────────────────────

def render_queue(songs: list) -> str:
    lines = ["#EXTM3U"]
    for song in songs:
        path = song_file(song).resolve()
        artist = song.get("artist") or "desconocido"
        title = song.get("title") or path.stem
        lines.append(f"#EXTINF:,{artist} — {title}")
        lines.append(str(path))
    return "\n".join(lines) + "\n"


def write_queue(songs: list, target_climate: dict, exclude_paths: set[str] | None = None) -> tuple[int, bool]:
    exclude = exclude_paths or set()
    pool = []
    seen_keys = set()
    for song in songs:
        key = track_key(song)
        path = song_file(song)
        if key in seen_keys or song.get("heard") or song.get("archived"):
            continue
        if not path.exists() or path_identity(path) in exclude:
            continue
        seen_keys.add(key)
        pool.append(song)

    def sort_key(song):
        dist = song.get("climate_dist")
        if dist is None and target_climate:
            dist = climate_distance(song.get("climate", {}), target_climate)
        return dist if dist is not None else 0.5

    pool.sort(key=sort_key)
    ordered = []
    recent_artists = []
    while pool:
        window = set(recent_artists[-4:])
        chosen_index = next(
            (i for i, song in enumerate(pool) if song.get("artist") not in window),
            0,
        )
        chosen = pool.pop(chosen_index)
        ordered.append(chosen)
        recent_artists.append(chosen.get("artist", ""))

    text = render_queue(ordered)
    changed = playlist_needs_update(QUEUE_PATH, text)
    if changed:
        write_playlist(QUEUE_PATH, text)
    return len(ordered), changed


def offline_sort_key(song: dict, state: dict):
    record = state["tracks"].get(track_key(song), {})
    timestamp = record.get("last_played") or song.get("last_played_at")
    if not timestamp:
        timestamp = file_mtime(song_file(song))
    return parse_time(timestamp), track_key(song)


def write_offline_queue(songs: list, state: dict, exclude_paths: set[str] | None = None) -> tuple[int, bool]:
    exclude = {path_identity(value) for value in (exclude_paths or set())}
    candidates = {}
    for song in songs:
        path = song_file(song)
        if not song.get("heard") or not path.exists() or path_identity(path) in exclude:
            continue
        candidates[track_key(song)] = song

    for key, record in state["tracks"].items():
        if key in candidates or not record.get("file"):
            continue
        path = path_from_value(record["file"])
        if not path.exists() or path_identity(path) in exclude:
            continue
        song = metadata_from_path(path)
        song["last_played_at"] = record.get("last_played") or file_mtime(path)
        candidates[key] = song

    ordered = sorted(candidates.values(), key=lambda song: offline_sort_key(song, state))
    ordered = ordered[:OFFLINE_QUEUE_SIZE]
    text = render_queue(ordered)
    changed = playlist_needs_update(OFFLINE_QUEUE_PATH, text)
    if changed:
        write_playlist(OFFLINE_QUEUE_PATH, text)
    return len(ordered), changed


def protected_paths(now_playing: str | None) -> set[str]:
    protected = set()
    if now_playing:
        protected.add(path_identity(now_playing))
    protected.update(queue_paths(QUEUE_PATH)[:PROTECTED_QUEUE_ITEMS])
    return protected


def remove_empty_music_dirs():
    if not MUSIC_DIR.exists():
        return
    for directory in sorted(MUSIC_DIR.rglob("*"), reverse=True):
        if directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                pass


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Gestor de la radio musical")
    ap.add_argument("--force", action="store_true", help="descargar aunque quede cola suficiente")
    ap.add_argument("--status", action="store_true", help="mostrar estado y salir")
    ap.add_argument("--mark-played", metavar="PATH", help="marcar una pista como iniciada")
    ap.add_argument("--cleanup-history", action="store_true", help="limpiar eventos temporales")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE, help=f"canciones a descargar por lote (default {BATCH_SIZE})")
    ap.add_argument("--low-watermark", type=int, default=LOW_WATERMARK, help=f"umbral de cola para disparar descarga (default {LOW_WATERMARK})")
    ap.add_argument("--max-dist", type=float, default=MAX_DIST, help=f"distancia de clima máxima (default {MAX_DIST})")
    args = ap.parse_args()

    if args.mark_played:
        return mark_started(args.mark_played)

    if args.cleanup_history:
        cleanup_lock = acquire_lock()
        if cleanup_lock is None:
            return 0
        try:
            cleanup_playback_events()
        finally:
            cleanup_lock.close()
        return 0

    if args.status:
        songs = load_songs()
        state = load_state()
        seen = load_seen()
        heard = [song for song in songs if song.get("heard")]
        unplayed = [song for song in songs if not song.get("heard") and song_file(song).exists()]
        missing = [song for song in songs if not song_file(song).exists()]
        total_mb = sum(song_file(song).stat().st_size / 1e6 for song in songs if song_file(song).exists())
        print(f"Canciones en catálogo : {len(songs)}")
        print(f"  escuchadas          : {len(heard)}")
        print(f"  sin reproducir      : {len(unplayed)}")
        print(f"  sin archivo (drop)  : {len(missing)}")
        print(f"Modo de reproducción  : {state.get('mode', 'online')}")
        print(f"Cola online           : {len(queue_paths(QUEUE_PATH))}")
        print(f"Cola offline          : {len(queue_paths(OFFLINE_QUEUE_PATH))}")
        print(f"Espacio en disco      : {total_mb:.1f} MB")
        print(f"Jamendo IDs vistos    : {len(seen)}")
        print(f"Sonando ahora         : {get_nowplaying_path() or '(nada)'}")
        return 0

    lock = acquire_lock()
    if lock is None:
        print("gestor: ya hay una instancia corriendo; saliendo.")
        return 0

    try:
        target_climate = load_clima()
        songs = load_songs()
        state = load_state()
        seen = load_seen()
        now_playing = get_nowplaying_path()

        _, archive_seen = adopt_archive(songs, state)
        seen.update(value for value in archive_seen if value.isdigit())
        marked = consume_played(songs, state)
        ensure_state_records(songs, state)
        if marked:
            log(f"gestor: {marked} canciones marcadas como escuchadas")

        protected = protected_paths(now_playing)
        archived_orphans, orphan_ids = archive_orphans(songs, state, protected)
        seen.update(orphan_ids)
        if archived_orphans:
            log(f"cleanup: {archived_orphans} mp3s huérfanos archivados")

        unplayed = [song for song in songs if not song.get("heard") and song_file(song).exists()]
        log(f"gestor: {len(unplayed)} sin reproducir de {len(songs)} en catálogo")

        if len(unplayed) > args.low_watermark and not args.force:
            exclude = {path_identity(now_playing)} if now_playing else set()
            online_count, online_changed = write_queue(songs, target_climate, exclude)
            offline_count, offline_changed = write_offline_queue(songs, state, protected)
            state["mode"] = "online" if online_count else "offline"
            save_songs(songs)
            save_state(state)
            save_seen(seen)
            if online_changed:
                log(f"gestor: queue.m3u escrito con {online_count} canciones.")
            if offline_changed:
                log(f"gestor: queue.offline.m3u escrito con {offline_count} canciones.")
            cleanup_playback_events()
            return 0

        client_id = get_client_id()
        if not client_id:
            log("ERROR: falta JAMENDO_CLIENT_ID (env o scripts/.jamendo_client)")
            new_songs = []
        else:
            log(f"gestor: cola baja ({len(unplayed)} ≤ {args.low_watermark}). Descargando lote de {args.batch_size}...")
            new_songs, _ = fetch_batch(client_id, target_climate, seen, state, args.batch_size, args.max_dist)
        log(f"gestor: {len(new_songs)} canciones nuevas descargadas")

        if new_songs:
            songs.extend(new_songs)
        songs, archived = archive_songs(songs, state, protected)
        if archived:
            log(f"gestor: {archived} canciones archivadas para fallback offline")

        save_songs(songs)
        save_seen(seen)

        exclude = {path_identity(now_playing)} if now_playing else set()
        online_count, online_changed = write_queue(songs, target_climate, exclude)
        offline_count, offline_changed = write_offline_queue(songs, state, protected)
        state["mode"] = "online" if online_count else "offline"
        save_state(state)
        remove_empty_music_dirs()
        cleanup_playback_events()
        if online_changed:
            log(f"gestor: queue.m3u escrito con {online_count} canciones. Ciclo completado.")
        else:
            log(f"gestor: cola online sin cambios ({online_count} canciones).")
        if offline_changed:
            log(f"gestor: queue.offline.m3u escrito con {offline_count} canciones.")
        if state["mode"] == "offline" and offline_count == 0:
            available = any(song.get("heard") and song_file(song).exists() for song in songs)
            if not available:
                log("ERROR: fallback offline sin archivos disponibles")
                return 2
            log("gestor: fallback offline en espera (pistas aún protegidas por la cola online)")
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
