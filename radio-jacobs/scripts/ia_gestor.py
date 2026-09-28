#!/usr/bin/env python3
"""ia_gestor.py — ciclo de vida de la radio Aadam Jacobs Collection.

Estación hermana de radio/ (la de Jamendo). Habla únicamente con archive.org,
así que no comparte estado, catálogo ni locks con la otra: cada una tiene su
propio songs.json, sus propias colas y su propio data/.

Flujo:
  1. Lee clima.json  →  qué perfil musical buscamos
  2. Consume played.txt/played.tsv y marca las canciones al inicio
  3. Si quedan pocas sin reproducir (≤ LOW_WATERMARK), descarga un lote de
     shows de la colección aadamjacobs y se queda con 1-2 tracks de cada uno
  4. Conserva las pistas escuchadas en archive/music/ para el fallback offline
  5. Escribe queue.m3u y queue.offline.m3u solo cuando cambia el contenido

Fuentes de verdad:
  • songs.json           — catálogo de mp3s y flags de reproducción
  • data/playback.json   — timestamps y conteos durables
  • data/archive_seen.json — "identificador::track" vistos alguna vez
  • logs/played.txt      — historial legible del panel web

Permisos: estos shows no traen licencia Creative Commons. Vienen del modelo
del Live Music Archive (colección etree): las bandas autorizan distribución
pública, gratuita y sin fines de lucro, a cambio de atribución. Por eso la
licencia emitida es "permission" y el crédito (banda, lugar, fecha, taper)
viaja en songs.json hasta el panel web.

Uso:
  python scripts/ia_gestor.py              # chequea y actúa si hace falta
  python scripts/ia_gestor.py --force      # descarga aunque quede cola
  python scripts/ia_gestor.py --status     # estado y salir
"""
import argparse
import fcntl
import html
import json
import os
import random
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
SEEN_PATH    = ROOT / "data" / "archive_seen.json"
STATE_PATH   = ROOT / "data" / "playback.json"
PLAYED_PATH  = ROOT / "logs" / "played.txt"
PLAYED_TS_PATH = ROOT / "logs" / "played.tsv"
NOWPLAYING   = ROOT / "logs" / "nowplaying.txt"
MUSIC_DIR    = ROOT / "music"
ARCHIVE_DIR  = ROOT / "archive" / "music"
LOG_PATH     = ROOT / "logs" / "gestor.log"
BAND_GENERO  = ROOT / "data" / "band_genero.json"
LOCK_PATH    = ROOT / "data" / "gestor.lock"

# ── parámetros ────────────────────────────────────────────────────────────────
# LOW_WATERMARK dispara la recarga cuando quedan pocas. Tiene que ser MAYOR que
# el tiempo que tarda un lote en bajar, medido en pistas: un lote de 120 tarda
# ~37 min (18 s por pista), o sea ~9 pistas. Con 20 de margen quedan 86 min de
# música en la caja contra 37 de descarga, y la radio nunca entra al fallback.
LOW_WATERMARK  = 20
# Pool local: 120 pistas ≈ 9 h de música ≈ 600 MB en disco. A 2 por show son
# ~60 recitales distintos en la caja, y la recarga tarda menos de 9 h.
BATCH_SIZE     = 120
MAX_DIST       = 0.55
GENERO_PENALTY_CAP = 0.6
OFFLINE_QUEUE_SIZE = 200
PROTECTED_QUEUE_ITEMS = 2
HISTORY_RETENTION_DAYS = 7
NOWPLAYING_MAX_AGE = 900
# Cuántos shows mirar por ciclo. Cada show trae 8-30 tracks, así que 6 alcanza
# sobrado para llenar un lote de 120 y no castiga a archive.org.
SOURCE_MAX_SHOWS = 6
# Cuántas veces puede reiniciar el cursor dentro de un mismo ciclo cuando la
# colección se terminó. Con 1 hay, en el peor caso, 2 llamadas a _sweep por
# ciclo: la original y la del reinicio. Acota el bucle de fetch_batch.
MAX_WRAPS = 1
# Tracks que se toman por show. La libretita deduplica por CANCIÓN, no por show:
# un recital puede volver a pasar y aportar las pistas que faltaban. El 2 es una
# decisión de variedad, no de correctitud — en un lote de 120 son ~60 bandas
# distintas en vez de 4 repetidas 30 veces.
MAX_TRACKS_PER_SHOW = 2
# Los shows en vivo duran 5-10' y hay jams de 15'. Por encima de este umbral
# el track se descarta antes de descargar.
MAX_TRACK_SECONDS = 420
# Pistas de relleno entre canciones: el público habla, el que graba chequea los
# equipos, anuncia el show. No son música, y con 2 slots por show cada una se
# come un tercio del show. Heurística sobre título y nombre de archivo, a
# propósito conservadora: "intro", "welcome" o "thanks" quedan fuera porque hay
# instrumentales que se llaman así de verdad (el "Solar Winds Intro" de Björk
# está en la colección). Se miran los dos campos porque en los shows viejos el
# title del mp3 no siempre las distingue.
TALK_RE = re.compile(
    r"\b(chat|chatter|tuning|tune\s*up|level\s*check|sound\s*check|commentary|"
    r"banter|intermission|intermision|applause|announcements?|setlist|crowd|"
    r"audience|raffle)\b",
    re.IGNORECASE,
)
# Un "intro" a secas sí es el presentador, y hay muchos: en los sets de WFMU la
# pista 1 es la presentación. Pero hay instrumentales de verdad que se llaman
# "Solar Winds Intro" o "Part One - Introduction - The Adoration of the Earth",
# así que la regla es: solo cae el título que NO dice nada más. Si el "intro"
# viene acompañado ("MC intro", "DJ introduction"), también es hablado.
BARE_INTRO_RE = re.compile(
    r"^\s*(?:(?:mc|dj|host|station|band|announcer)\b[\s.'-]*)*"
    r"(?:the\s+)?(?:intro|introduction)s?[\s.!:-]*$",
    re.IGNORECASE,
)
# Tope del archivo de respaldo. Con ~45.000 tracks disponibles en la colección
# el offline es red de seguridad, no inventario: 8 GB ≈ 1.400 pistas.
MAX_ARCHIVE_BYTES = 8 * 1024**3

# ── constantes Internet Archive ───────────────────────────────────────────────
EMIT_LICENSES = {"cc0", "public-domain", "cc-by", "cc-by-sa", "cc-by-nc", "permission"}
IA_SEARCH     = "https://archive.org/advancedsearch.php"
IA_META       = "https://archive.org/metadata/{identifier}"
IA_ITEM       = "https://archive.org/details/{identifier}"
IA_DOWNLOAD   = "https://archive.org/download/{identifier}/{name}"
IA_COLLECTION = "aadamjacobs"
# Cota inferior del rango de identificadores. "0" es menor que cualquier id que
# empiece con letra o dígito, así que el primer ciclo ve la colección entera.
IA_CURSOR_START = "0"
# El permiso del Live Music Archive se aplica a las colecciones etree y a las
# que se archivan dentro de ellas. Sin esto caería el 100% de AJC, que no
# declara licenseurl.
IA_PERMISSION_COLLECTIONS = {"etree", "aadamjacobs"}
# Energías y complejidad: un set en vivo de una banda de indie no trae nada
# medible, así que son valores fijos del perfil en vez de datos inventados.
IA_ENERGY = 0.6
IA_COMPLEXITY = 0.5
IA_DEFAULT_GENERO = ["indierock", "alternativerock", "postrock"]
USER_AGENT    = "radio-algoritmica/1.0 (jacobs collection; non-commercial stream)"
LICENSE_MAP   = {
    "by-nc-sa": "cc-by-nc", "by-nc-nd": None, "by-nc": "cc-by-nc",
    "by-sa":    "cc-by-sa", "by-nd":    None,  "by":    "cc-by",
    "publicdomain": "public-domain", "zero": "cc0", "mark": "public-domain",
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
    except (OSError, ValueError):
        # ValueError: un byte nulo en la línea de played.tsv rompe .resolve()
        # y tumbaba el ciclo entero. Se degrada al valor crudo en vez de morir.
        try:
            return str(path_from_value(value))
        except (OSError, ValueError):
            return str(value or "")


def track_key(value) -> str:
    if isinstance(value, dict):
        archive_id = str(value.get("archive_id") or "")
        if archive_id:
            return f"archive:{archive_id}"
        value = value.get("file", "")
    name = path_from_value(value).name
    # Los ids de archive.org son alfanuméricos (ajc04106_tortoise_2000-01-06t01),
    # así que un \d+ los perdería y la identidad caería a "file:<basename>",
    # que se rompe en cuanto el archivo se renombra.
    #
    # El prefijo ".*" es codicioso a propósito: el archivo es "{título} - {id}"
    # y los títulos pueden traer " - " adentro (p.ej. "Sun Ra - Space Is the
    # Place"). Con re.search(r" - (.+)$") el motor ancla en la PRIMERA
    # separación y el id salía contaminado con el resto del título, lo que
    # rompía el dedup, el archivado y la poda. Codicioso en el prefijo = se
    # parte por la ÚLTIMA separación, que es la que separa el id.
    match = re.match(r"^.* - (.+)$", Path(name).stem)
    if match:
        return f"archive:{match.group(1)}"
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
    # Cursor de ingesta: el último identifier leído de la colección. Se avanza
    # con rangos identifier:[cursor TO *] ordenados asc, así el barrido recorre
    # los 3.430 shows una vez y da la vuelta al terminar.
    data.setdefault("source", {})
    if not isinstance(data["source"], dict):
        data["source"] = {}
    data["source"].setdefault("identifier", "")
    # Claves del gestor de Jamendo: se descartan para no arrastrar offsets.
    for stale in ("tag_index", "offsets"):
        data["source"].pop(stale, None)
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
    # El año del show es el único eje que la AJC describe de verdad, pero ya no
    # se contrasta: clima.json quitó "temporalidad" a propósito. Se mantiene la
    # escala porque el valor sigue siendo dato válido para mostrar.
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


def _band_key(name: str) -> str:
    """Normaliza el nombre de la banda para buscar en data/band_genero.json."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def load_band_genero() -> dict:
    data = load_json(BAND_GENERO, {})
    return data if isinstance(data, dict) else {}


def climate_from_ia(meta: dict) -> dict:
    """Extrae el clima de un show de la Aadam Jacobs Collection.

    archive.org no tiene nada equivalente al bloque musicinfo de Jamendo: el
    campo subject de estos ítems solo dice "Aadam Jacobs;AJC Project", así que
    no hay mood, ni instruments, ni acousticelectric que traducir a nada.

    Por eso se hace lo contrario que en Jamendo: se rellena SOLO lo que se
    puede sostener y se omiten las dimensiones desconocidas. climate_distance
    saltea toda dimensión ausente de cualquiera de los dos lados, de modo que
    omitir mood/texture/voice/instrumentation evita que el filtro las_castigue
    sin inventar datos. Es la diferencia entre que el lote pase y que entre
    cero material: con genero vacío, climate_distance parte de d_genero = 1.0
    y ningún show sobrevive al MAX_DIST.

    OJO: en la configuración actual el score NO filtra nada. Como la colección
    es material de un solo artista, IA_ENERGY e IA_COMPLEXITY son constantes
    fijas idénticas a las del target y el genero cae siempre al default, que ya
    es el del target. climate_distance devuelve 0.00 para toda pista, así que
    MAX_DIST (0.55) nunca rechaza un show. El mecanismo queda armado a propósito:
    en cuanto data/band_genero.json se puegue con los generos reales de cada
    banda, d_genero empieza a variar y el filtro vuelve a estar vivo solo.
    """
    date = (meta.get("date") or "")[:4]
    try:
        year = int(date)
    except (TypeError, ValueError):
        year = 0
    if not year:
        # La colección va de 1985 a 2023: lo más honesto sin fecha es vintage.
        temp = "vintage"
    else:
        temp = "vintage" if year < 2000 else ("clasico" if year < 2016 else "contemporaneo")
    genero = load_band_genero().get(_band_key(item_creator(meta))) or IA_DEFAULT_GENERO
    return {
        "genero": list(genero),
        "energy": IA_ENERGY,
        "complexity": IA_COMPLEXITY,
        "temporalidad": temp,
    }


# ── Internet Archive ──────────────────────────────────────────────────────────

def ia_get(params: dict) -> dict:
    """Busca ítems en advancedsearch.php.

    Mismo contrato de retorno que usaba api_get (ok/docs/empty/network_error)
    para que el resto del ciclo no tenga que saber de dónde salió la página.
    """
    q = {"output": "json"}
    q.update(params)
    url = IA_SEARCH + "?" + urllib.parse.urlencode(q, doseq=True)
    answered = False
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as response:
                doc = json.loads(response.read().decode("utf-8"))
            resp = doc.get("response") or {}
            docs = resp.get("docs") or []
            answered = True
            if docs:
                return {"ok": True, "docs": docs, "network_error": False,
                        "num_found": int(resp.get("numFound") or len(docs)),
                        "empty": False}
            # Página vacía: puede ser el final real del rango o una limitación
            # de ritmo. Se reintenta antes de dar la ventana por agotada.
        except (OSError, ValueError):
            pass
        if attempt < 3:
            time.sleep(1.5 * (attempt + 1))
    return {"ok": answered, "network_error": not answered,
            "docs": [], "num_found": 0, "empty": True}


def ia_meta(identifier: str) -> dict | None:
    """Metadata de un ítem: archivos disponibles, setlist y créditos."""
    url = IA_META.format(identifier=urllib.parse.quote(identifier))
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as response:
                doc = json.loads(response.read().decode("utf-8"))
            if not doc or not doc.get("files"):
                # Ítem "dark": la metadata quedó pero los archivos no.
                return None
            return doc
        except (OSError, ValueError):
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
    return None


def parse_seconds(value) -> float | None:
    """Normaliza las duraciones, que vienen en dos formatos.

    El MP3 derivado las trae como "mm:ss" ("02:23") y el FLAC maestro en
    segundos con decimales ("143.21"). Confundir ambos deja tracks de 1 o de
    140 segundos.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if ":" in s:
        total = 0.0
        for part in s.split(":"):
            try:
                total = total * 60 + float(part)
            except ValueError:
                return None
        return total
    try:
        return float(s)
    except ValueError:
        return None


def description_lines(description: str) -> list[str]:
    """Parte la description en líneas reales.

    La colección tiene dos formatos de description y no se pueden parsear igual:
    los uploads viejos (ajcNNNNN_*) son texto plano con saltos de línea, y los
    más nuevos son HTML con un <div> por línea. Sin convertir </div> y <br> en
    saltos, el setlist aparece como un bloque único y no se puede numerar.
    """
    text = description or ""
    text = re.sub(r"(?i)</(div|p|tr|h\d)>", "\n", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</li>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("&gt;", ">")
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_setlist(description: str) -> dict:
    """Saca el título de cada canción del setlist que va dentro de description.

    Viene como líneas tipo "01 The Equator" o "3. Heroin". Los soundchecks y
    afinaciones van entre corchetes y no son canciones. Muchos ítems cierran
    con un "no setlist" explícito, que además no matchea el patrón numerado.
    """
    titles: dict[int, str] = {}
    for line in description_lines(description):
        m = re.match(r"^(\d{1,2})\s*[.)\-]?\s+(.*\S)$", line)
        if not m:
            continue
        title = m.group(2).strip()
        if not title or title.startswith("["):
            continue
        titles.setdefault(int(m.group(1)), title)
    return titles


VENUE_IN_ALBUM_RE = re.compile(
    r"\blive at (.+?)(?:\s+on)?\s+\d{4}-\d{2}-\d{2}\s*$", re.IGNORECASE)


def venue_from_album(album: str) -> str:
    """Saca el lugar del título del show, que es el único que trae la AJC.

    Solo 21 de los ítems viejos traen metadata.venue, y ningún show nuevo la
    trae. Pero el título del ítem siempre lo dice: "Run On Live at Empty
    Bottle on 1996-08-10" o, sin el "on", "Azita Live at The Hideout
    2015-07-24". También hay erratas ("LIve at") y espacios dobles
    ("Overture  Center"), así que el patrón va con IGNORECASE y grupo perezoso
    antes de la fecha.
    """
    match = VENUE_IN_ALBUM_RE.search(album or "")
    return _clean(match.group(1)) if match else ""


def parse_credits(description: str) -> dict:
    """Saca el grabador del texto de description.

    Los ítems viejos traen metadata.taper, pero los más recientes no: esos
    puts solo "Recorded by:" dentro del HTML de la description. Sin este
    fallback el crédito visible del grabador saldría vacío justo en los shows
    más recientes. El lugar no sale de acá sino del título del show, vía
    venue_from_album().
    """
    lines = description_lines(description)
    out: dict = {}
    for i, line in enumerate(lines):
        low = line.lower()
        if "recorded by" in low or "taper" in low:
            value = re.split(r"(?i)recorded\s+by|taper", line, maxsplit=1)[-1]
            value = value.lstrip(": ").strip(" .")
            value = re.sub(r"\s*\(.*?\)\s*$", "", value).strip()
            if value:
                out.setdefault("taper", value)
        if low.startswith("recorded by") and i + 1 < len(lines):
            nxt = lines[i + 1].strip(" .")
            if nxt and len(nxt) < 80:
                out.setdefault("taper", nxt)
    return out


def item_creator(meta: dict) -> str:
    creator = meta.get("creator")
    if isinstance(creator, list):
        creator = creator[0] if creator else ""
    return _clean(str(creator or "").strip()) or "desconocido"


def item_collections(meta: dict) -> set:
    cols = meta.get("collection") or []
    if isinstance(cols, str):
        cols = [cols]
    return {str(c).strip().lower() for c in cols}


def license_short(curl: str | None) -> str | None:
    if not curl:
        return None
    for key, short in LICENSE_MAP.items():
        if key in curl:
            return short
    return None


def license_of(meta: dict) -> str | None:
    """Licencia del ítem.

    La colección aadamjacobs no declara licenseurl: el permiso viene del modelo
    del Live Music Archive (artistas trade-friendly que autorizan distribución
    pública, gratuita y sin fines de lucro a cambio de atribución). Sin este
    rama el 100% de los shows se descartaría antes de descargar nada.
    """
    if item_collections(meta) & IA_PERMISSION_COLLECTIONS:
        return "permission"
    return license_short(meta.get("licenseurl"))


def tracks_of(doc: dict, identifier: str) -> list[dict]:
    """Tracks descargables de un show, ordenados y filtrados por duración.

    Recibe el doc completo de /metadata/<id>, NO solo su campo "metadata":
    la lista de archivos vive en "files", que es hermano de "metadata" en la
    respuesta, no un subcampo. Por eso el identifier se pasa aparte.

    Solo se toman los MP3 derivados (source == "derivative"), que son los que
    archive.org genera para escucha a 128k — igual formato que los de Jamendo —
    en vez de los FLAC maestros de 30 a 80 MB por canción.
    """
    meta = doc.get("metadata") or {}
    description = meta.get("description") or ""
    setlist = parse_setlist(description)
    credits = parse_credits(description)
    album = _clean(str(meta.get("title") or "show"))
    artist = item_creator(meta)
    venue = _clean(str(meta.get("venue") or "")) or credits.get("venue") \
        or venue_from_album(album)
    date = str(meta.get("date") or "")
    taper = _clean(str(meta.get("taper") or "")) or credits.get("taper") or "Aadam Jacobs"
    out = []
    for f in doc.get("files") or []:
        name = f.get("name") or ""
        if not name.lower().endswith(".mp3") or f.get("source") != "derivative":
            continue
        seconds = parse_seconds(f.get("length"))
        if seconds is None or seconds <= 10 or seconds > MAX_TRACK_SECONDS:
            continue
        try:
            num = int(f.get("track") or 0)
        except (TypeError, ValueError):
            continue
        if not num:
            continue
        num_title = _clean(str(f.get("title") or ""))
        # Se miran el title del mp3 y el nombre del archivo: en los shows viejos
        # el title viene "untitled" y el único lugar donde dice "chat" es el
        # nombre ("05 chat.mp3"). Con 2 slots por show, dejarlo pasar se come un
        # tercio de la tanda.
        if TALK_RE.search(num_title) or TALK_RE.search(name) \
                or BARE_INTRO_RE.search(num_title) or BARE_INTRO_RE.search(name):
            continue
        # El campo title de los MP3 antiguos vale "untitled", que no es un
        # título: se descarta. Tampoco se usa el nombre de archivo, porque en
        # estos shows ("no setlist") es el nombre de la banda más un
        # t01/t02: "CommandModule1996-10-04t01" no es el título de nada. Mejor
        # un "Track 7" honesto; el panel ya muestra banda, lugar y fecha.
        if num_title.lower() in ("untitled", "unknown", ""):
            num_title = ""
        out.append({
            "archive_id": f"{identifier}t{num:02d}",
            "identifier": identifier,
            "track": num,
            "title": setlist.get(num) or num_title or f"Track {num}",
            "artist": artist,
            "album": album,
            "duration_seconds": int(seconds),
            "venue": venue,
            "recorded": date,
            "taper": taper,
            "file_url": IA_DOWNLOAD.format(
                identifier=urllib.parse.quote(identifier),
                name=urllib.parse.quote(name),
            ),
        })
    out.sort(key=lambda t: t["track"])
    return out


def sanitize(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', "-", s).strip(" .")
    return s or "track"


def _clean(v):
    return html.unescape(v) if v else v


def download_ia(track: dict) -> Path | None:
    """Descarga el MP3 derivado del track y devuelve la ruta relativa a ROOT.

    El sufijo " - {archive_id}" del nombre es lo que permite reconstruir la
    identidad del track desde el path cuando la entrada de songs.json se
    pierde (adopción desde disco, huérfanos del archive). Por eso el id se
    sanitiza aparte: los ids de archive.org traen puntos y guiones, y el
    separador " - " no puede aparecer adentro.
    """
    url = track.get("file_url")
    if not url:
        return None
    artist = sanitize(track.get("artist") or "desconocido")
    album = sanitize(track.get("album") or "show")
    title = sanitize(track.get("title") or f"track-{track.get('track')}")
    archive_id = str(track.get("archive_id") or "desconocido").replace(" - ", "_")
    rel = Path("music") / artist / album / f"{title} - {archive_id}.mp3"
    dest = ROOT / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".mp3.part")
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


def backfill_venue() -> int:
    """Rellena attribution.venue de las canciones que lo tienen vacío.

    El lugar se deriva del título del show, que siempre lo trae, así que
    no hace falta volver a pegarle al API de archive.org. Solo escribe en las
    entradas vacías: es idempotente y no pisa un venue que ya venía de
    metadata.venue. Se llama una vez con --backfill-venue y devuelve cuántas
    entradas corrigió.
    """
    songs = load_songs()
    changed = 0
    for song in songs:
        if not isinstance(song, dict):
            continue
        att = song.get("attribution")
        if not isinstance(att, dict):
            att = {}
            song["attribution"] = att
        if str(att.get("venue") or "").strip():
            continue
        venue = venue_from_album(str(song.get("album") or ""))
        if venue:
            att["venue"] = venue
            changed += 1
    if changed:
        save_songs(songs)
    return changed


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
    # Mismo criterio que track_key: se parte por la última separación " - " para
    # no comerse parte del título cuando el título trae " - " adentro.
    match = re.match(r"^.* - (.+)$", stem)
    archive_id = match.group(1) if match else None
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
        "id": f"ia-{archive_id}" if archive_id else track_key(p),
        "archive_id": archive_id,
        "file": path_string(p),
        "title": title or "desconocido",
        "artist": artist or "desconocido",
        "album": album or "single",
        "source": "archive" if archive_id else "local",
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
        # Sin el filtro isdigit() que usa la radio de Jamendo: los ids de
        # archive.org son alfanuméricos y filtrar por dígito los descartaría
        # todos, con lo cual el archivo podría volver a descargarse.
        archive_id = key.removeprefix("archive:")
        if archive_id != key:
            seen.add(archive_id)
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


def human_size(size: float) -> str:
    if size >= 1073741824:
        return f"{size / 1073741824:.1f} GB"
    if size >= 1048576:
        return f"{size / 1048576:.1f} MB"
    return f"{size / 1024:.0f} KB"


def archive_size() -> int:
    if not ARCHIVE_DIR.exists():
        return 0
    return sum(p.stat().st_size for p in ARCHIVE_DIR.rglob("*.mp3"))


def prune_archive(songs: list, state: dict, protect_paths: set[str]) -> int:
    """Recorta el archivo offline para no pasar de MAX_ARCHIVE_BYTES.

    Sin esto el archivo crece sin límite: MAX_ARCHIVE_BYTES estaba declarado
    pero no se comprobaba en ningún lado. Se borra de lo más viejo a lo más
    reciente por última reproducción, saltando lo que esté sonando ahora y lo
    que siga en la cola online. Las entradas se quitan de songs.json pero
    quedan en archive_seen.json, para que el cursor no las vuelva a bajar.
    """
    total = archive_size()
    if total <= MAX_ARCHIVE_BYTES:
        return 0
    candidates = []
    for song in songs:
        path = song_file(song)
        try:
            path.resolve().relative_to(ARCHIVE_DIR.resolve())
        except ValueError:
            continue
        if not path.exists() or path_identity(path) in protect_paths:
            continue
        record = state["tracks"].get(track_key(song)) or {}
        stamp = record.get("last_played") or song.get("last_played_at") or ""
        candidates.append((stamp, track_key(song), song, path))
    # sort() es estable: ante empates de timestamp, el orden de catálogo manda.
    candidates.sort(key=lambda c: (c[0], c[1]))
    removed = 0
    for stamp, key, song, path in candidates:
        if total <= MAX_ARCHIVE_BYTES:
            break
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            continue
        total -= size
        songs.remove(song)
        state["tracks"].pop(key, None)
        removed += 1
        log(f"  - archive: {song.get('artist')} — {song.get('title')} "
            f"({stamp or 'sin fecha'}, {size / 1048576:.1f} MB)")
    if removed:
        log(f"  archive: {removed} pistas podadas, quedan "
            f"{human_size(total)} (tope {human_size(MAX_ARCHIVE_BYTES)})")
    return removed


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
            if key.startswith("archive:"):
                archived_ids.add(key.removeprefix("archive:"))
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


# ── descarga desde archive.org ────────────────────────────────────────────────

def _harvest_show(identifier: str, target_climate: dict, seen: set,
                  existing_ids: set, added: list, per_artist: dict,
                  n: int, max_dist: float) -> bool:
    """Baja 1-2 tracks de un show. Muta added/seen/existing_ids/per_artist."""
    doc = ia_meta(identifier)
    if doc is None:
        return False
    meta = doc.get("metadata") or {}
    lic = license_of(meta)
    if not lic or lic not in EMIT_LICENSES:
        return False
    artist = item_creator(meta)
    if per_artist.get(artist, 0) >= MAX_TRACKS_PER_SHOW:
        return False
    song_climate = {
        key: value
        for key, value in climate_from_ia(meta).items()
        if value is not None
    }
    dist = climate_distance(song_climate, target_climate) if target_climate else 0.5
    if target_climate and dist > max_dist:
        return False
    item_url = IA_ITEM.format(identifier=urllib.parse.quote(identifier))
    got = 0
    for track in tracks_of(doc, identifier):
        if got >= MAX_TRACKS_PER_SHOW or len(added) >= n:
            break
        tid = track["archive_id"]
        if tid in existing_ids:
            continue
        rel = download_ia(track)
        if rel is None:
            continue
        added.append({
            "id": f"ia-{tid}",
            "archive_id": tid,
            "file": str(rel),
            "title": track["title"],
            "artist": artist,
            "album": track["album"],
            "duration_seconds": track["duration_seconds"],
            "license": lic,
            "source": "archive",
            "climate": song_climate,
            "climate_dist": round(dist, 3),
            "attribution": {
                "creator": artist,
                "license_url": meta.get("licenseurl"),
                "track_url": item_url,
                "taper": track["taper"],
                "venue": track["venue"],
                "recorded": track["recorded"],
                "collection": IA_COLLECTION,
            },
        })
        existing_ids.add(tid)
        seen.add(tid)
        got += 1
        per_artist[artist] = per_artist.get(artist, 0) + 1
        log(f"  + [{lic}] {artist} — {track['title']}  "
            f"({track['recorded'] or 's/f'})  dist={dist:.2f}")
    return got > 0


def _sweep(target_climate: dict, seen: set, existing_ids: set, added: list,
           per_artist: dict, n: int, max_dist: float, page_size: int,
           cursor: str) -> tuple[str, str]:
    """Avanza el cursor sobre la colección, show por show.

    Motivo devuelto: "ok" ventana leída, "empty" no quedan shows, "network_error".
    """
    consecutive_empty = 0
    for _ in range(SOURCE_MAX_SHOWS):
        if len(added) >= n:
            break
        params = {
            "q": f"collection:({IA_COLLECTION}) AND identifier:[{cursor} TO *]",
            "fl[]": ["identifier"],
            "sort[]": "identifier asc",
            "rows": page_size,
        }
        doc = ia_get(params)
        if not doc.get("ok"):
            return cursor, "network_error"
        shows = doc.get("docs") or []
        if not shows:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                return cursor, "empty"
            continue
        consecutive_empty = 0
        for show in shows:
            identifier = str(show.get("identifier") or "")
            if not identifier:
                continue
            # El rango es inclusivo, así que el show del cursor reaparece en la
            # consulta siguiente. Se avanza igual: el cursor significa "leí
            # hasta acá". Los shows que quedaron sin mirar porque el lote se
            # llenó se releen el ciclo que viene, no se pierden.
            if identifier > cursor:
                cursor = identifier
            if len(added) >= n:
                return cursor, "ok"
            _harvest_show(identifier, target_climate, seen, existing_ids,
                          added, per_artist, n, max_dist)
        if doc.get("num_found", 0) <= len(shows):
            return cursor, "empty"
    return cursor, "ok"


def fetch_batch(target_climate: dict, seen: set, state: dict,
                n: int, max_dist: float) -> tuple[list, str]:
    existing_ids = {
        str(song.get("archive_id"))
        for song in load_songs()
        if song.get("archive_id")
    }
    existing_ids.update(str(value) for value in seen)
    page_size = 20
    added = []
    per_artist: dict = {}

    src = state.setdefault("source", {})
    # Cursor por identificador en vez de por offset: la búsqueda ordena por
    # identifier y el rango [cursor TO *] devuelve "de acá en adelante". Así el
    # ciclo avanza aunque la colección cambie de tamaño, y nunca se relee la
    # cabeza del ranking.
    cursor = str(src.get("identifier") or "") or IA_CURSOR_START
    # Contador de pasadas, para reiniciar el cursor al llegar al final.
    #
    # El dedup es por CANCIÓN (existing_ids), así que releer un show no repite
    # nada: le saca las pistas que todavía no se habían tomado. Eso convierte el
    # final de la colección en otra vuelta con material nuevo, en vez de en un
    # suministro que se agota para siempre.
    #
    # El freno es lo importante. Sin él, una colección ya consumida haría
    # recorrer los 3.430 shows en cada ciclo pidiendo /metadata de cada uno para
    # no sacar nada. La condición para reiniciar es que la pasada anterior haya
    # producido canciones de verdad: si una pasada entera termina en 0, la
    # colección está agotada y se devuelve "exhausted".
    #
    # MAX_WRAPS acota las llamadas a _sweep por ciclo, así que el bucle no puede
    # dar vueltas de más ni aunque la vuelta nueva también termine en "empty".
    pasada = int(src.get("pasada", 0) or 0)
    canciones_pasada = int(src.get("canciones_pasada", 0) or 0)
    reason = "empty"
    wraps = 0

    while True:
        cursor, reason = _sweep(target_climate, seen, existing_ids, added,
                                per_artist, n, max_dist, page_size, cursor)
        if reason != "empty":
            break
        if wraps >= MAX_WRAPS:
            log(f"gestor: alcanzó el tope de {MAX_WRAPS} vueltas en un ciclo; "
                f"cursor en {cursor}")
            break
        if pasada > 0 and canciones_pasada == 0:
            log(f"gestor: la pasada {pasada} terminó sin sacar canciones; "
                f"colección agotada")
            break
        log(f"gestor: fin de la pasada {pasada} en {cursor}; "
            f"vuelve al inicio para la pasada {pasada + 1}")
        pasada += 1
        src["pasada"] = pasada
        src["canciones_pasada"] = 0
        cursor = IA_CURSOR_START
        wraps += 1

    # El running total de canciones de la pasada vive en el estado, para que
    # sobreviva entre ciclos: el wrap lo resetea, cada ciclo le suma lo suyo.
    src["canciones_pasada"] = canciones_pasada + len(added)
    src["identifier"] = cursor
    log(f"gestor: consulta en {IA_COLLECTION} desde {cursor}; "
        f"motivo={reason}; pasada={pasada}; "
        f"candidatos nuevos acumulados={len(added)}/{n}")
    if reason == "network_error":
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

    # Orden al azar. La libretita ya garantiza que no se repita una canción, y
    # como el clima no discrimina (ver climate_from_ia), ordenar por distancia
    # solo daba la falsa sensación de un criterio. La ventana de recent_artists
    # también sobra: obligaba a partir los shows en trozos, que es justo lo
    # contrario de lo que se pidió.
    random.shuffle(pool)

    text = render_queue(pool)
    changed = playlist_needs_update(QUEUE_PATH, text)
    if changed:
        write_playlist(QUEUE_PATH, text)
    return len(pool), changed


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
    ap.add_argument("--backfill-venue", action="store_true", help="rellenar el lugar de las canciones que lo tienen vacío")
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

    if args.backfill_venue:
        changed = backfill_venue()
        print(f"Lugares rellenados: {changed}")
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
        print(f"IDs archive vistos    : {len(seen)}")
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
        seen.update(archive_seen)
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

        log(f"gestor: cola baja ({len(unplayed)} ≤ {args.low_watermark}). Descargando lote de {args.batch_size}...")
        new_songs, _ = fetch_batch(target_climate, seen, state, args.batch_size, args.max_dist)
        log(f"gestor: {len(new_songs)} canciones nuevas descargadas")

        if new_songs:
            songs.extend(new_songs)
        songs, archived = archive_songs(songs, state, protected)
        if archived:
            log(f"gestor: {archived} canciones archivadas para fallback offline")
        prune_archive(songs, state, protected)

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
