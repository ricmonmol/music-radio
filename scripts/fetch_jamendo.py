#!/usr/bin/env python3
"""fetch_jamendo.py: descubre y descarga música con derecho de emisión vía API Jamendo v3.

Cubre la "Vía Jamendo" del spec (§17): archivo + licencia CC verificada. La API
devuelve la URL de descarga, la licencia exacta (license_ccurl) y metadata musical
(musicinfo: vocal/instrumental, acustico/electrico, speed, tags) con la que se
rellenan climate y attribution de songs.json automáticamente.

La API requiere client_id: https://devportal.jamendo.com (crear una app). Se pasa
con --client-id o la variable JAMENDO_CLIENT_ID.

Modos:
  list      (default) busca canciones que encajan con el clima y lista candidatas.
  download  descarga las canciones elegidas (--ids) y las registra en songs.json
            con licencia emisible, atribución CC y climate derivado.
  batch     descarga un lote de hasta --limit temas sin confirmar, excluyendo
            los ya presentes y aplicando los filtros de clima; ideal para
            llenar el catálogo de golpe (menos llamadas a la API).

Uso:
  JAMENDO_CLIENT_ID=xyz ./fetch_jamendo.py --search "melancholic acoustic" \
      --acoustic acoustic --vocal vocal --speed low,medium --limit 20
  ./fetch_jamendo.py --ids 1848357,1880336 --download
  ./fetch_jamendo.py --batch --limit 80 --clima clima.json --max-distance 0.6

Solo se descargan tracks con audiodownload_allowed=true y licencia CCBY / CCBY-SA /
CCBY-NC(-SA) / CC0; se descartan CC-BY-ND y 'download-only'.
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
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
API = "https://api.jamendo.com/v3.0/tracks/"
SEEN_PATH = PROJECT_ROOT / "data" / "jamendo_seen.json"

# Licencias que el selector puede emitir (EMIT_LICENSES)
EMIT_LICENSES = {
    "cc0", "public-domain", "cc-by", "cc-by-sa", "cc-by-nc", "permission",
}
# (clave en license_ccurl -> nombre corto)
LICENSE_MAP = {
    "by-nc-sa": "cc-by-nc",
    "by-nc-nd": None,
    "by-nc": "cc-by-nc",
    "by-sa": "cc-by-sa",
    "by-nd": None,
    "by": "cc-by",
    "publicdomain": "public-domain",
    "zero": "cc0",
}

SPEED_ENERGY = {
    "verylow": 0.2, "low": 0.35, "medium": 0.5, "high": 0.7, "veryhigh": 0.85,
}
MOOD_MAP = {
    "happy": "alegre", "joyful": "alegre", "upbeat": "alegre",
    "sad": "melancolico", "melancholic": "melancolico", "bittersweet": "melancolico",
    "calm": "calmo", "relaxing": "relajado", "relaxed": "relajado",
    "dreamy": "sonador", "dreamlike": "sonador", "lullaby": "sonador",
    "intimate": "intimo", "mellow": "suave", "soft": "suave",
    "warm": "calido", "hopeful": "esperanzador", "nostalgic": "nostalgico",
    "dark": "oscuro", "mysterious": "misterioso", "epic": "epico",
}
USER_AGENT = "radio-algoritmica/0.9 (proyecto clima musical)"


def load_songs(path):
    if Path(path).exists():
        try:
            data = Path(path).read_text(encoding="utf-8")
            return json.loads(data) if data.strip() else []
        except json.JSONDecodeError:
            return []
    return []


def save_songs(path, songs):
    Path(path).write_text(
        json.dumps(songs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_seen():
    """Registro persistente de jamendo_id ya descargados alguna vez; sobrevive
    a los rotates (no se vuelven a descargar los archivados)."""
    if SEEN_PATH.exists():
        try:
            return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError):
            return set()
    return set()


def save_seen(seen):
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def license_short(curl):
    if not curl:
        return None
    for key, short in LICENSE_MAP.items():
        if key in curl:
            return short
    return None


def api_get(client_id, params):
    # Ojo: combinar search con audiodlformat devuelve 0 resultados; el default
    # de audiodownload ya es mp32, así que no se envía.
    q = {"client_id": client_id, "format": "json", "include": "musicinfo"}
    q.update(params)
    url = API + "?" + urllib.parse.urlencode(q)
    # La API responde a veces con 0 resultados válidos de forma intermitente;
    # se reintenta algunas veces antes de rendirse.
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as r:
                doc = json.loads(r.read().decode("utf-8"))
        except OSError:
            doc = None
        if doc is not None and doc.get("results"):
            return doc
        time.sleep(1.0)
    return doc if doc is not None else {
        "headers": {"status": "failed", "error_message": "no response"}}


def climate_from_track(t):
    mi = t.get("musicinfo") or {}
    tags = mi.get("tags") or {}
    speed = mi.get("speed") or "medium"
    eco = mi.get("acousticelectric") or ""
    voci = mi.get("vocalinstrumental") or ""
    date = (t.get("releasedate") or "")[:4]
    try:
        year = int(date)
        temporalidad = "vintage" if year < 2000 else ("clasico" if year < 2016 else "contemporaneo")
    except (TypeError, ValueError):
        temporalidad = "contemporaneo"
    mood = [MOOD_MAP.get(tag.lower(), tag.lower())
            for tag in (tags.get("vartags") or [])]
    mood = list(dict.fromkeys(m for m in mood if m))
    return {
        "mood": mood,
        "texture": "organica" if eco == "acoustic" else ("electrica" if eco == "electric" else None),
        "energy": SPEED_ENERGY.get(speed, 0.5),
        "complexity": 0.5,
        "voice": voci or None,
        "instrumentation": [i for i in (tags.get("instruments") or [])],
        "temporalidad": temporalidad,
    }


def climate_distance(a, b):
    """Distancia normalizada 0..1 entre dos vectores de clima, comparando solo
    las dimensiones presentes en ambos (None/ausente no suma)."""
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
            denom = max(len(left), len(right), 1)
            d += 1.0 - (len(inter) / denom)
            n += 1
    for dim in ("texture", "voice", "temporalidad"):
        av, bv = a.get(dim), b.get(dim)
        if av is None or bv is None:
            continue
        d += 0.0 if av == bv else 1.0
        n += 1
    return (d / n) if n else 0.0


def sanitize(s):
    s = re.sub(r"[\\/:*?\"<>|]", "-", s).strip(" .")
    return s or "track"


def _clean(v):
    return html.unescape(v) if v else v


def build_entry(t):
    """Convierte un track de la API en una entrada de songs.json (o None si
    no es emisible). Presupone que ya se descargó el MP3 a la ruta local."""
    lic = license_short(t.get("license_ccurl")) or ""
    if lic not in EMIT_LICENSES:
        return None
    artist = _clean((t.get("artist_name") or "desconocido").strip())
    album = _clean((t.get("album_name") or "single").strip()) or "single"
    title = _clean((t.get("name") or "").strip()) or f"track-{t.get('id')}"
    rel = Path("music") / sanitize(artist) / sanitize(album) / \
        f"{sanitize(title)} - {t.get('id')}.mp3"
    return {
        "id": f"jm-{t.get('id')}",
        "jamendo_id": str(t.get("id")),
        "file": str(rel),
        "title": title,
        "artist": artist,
        "album": album,
        "duration_seconds": t.get("duration"),
        "license": lic,
        "source": "jamendo",
        "attribution": {
            "creator": artist,
            "license_url": t.get("license_ccurl"),
            "track_url": t.get("shareurl"),
        },
        "climate": {k: v for k, v in climate_from_track(t).items() if v is not None},
        "releasedate": t.get("releasedate"),
    }


def download_track(client_id, t, out_root):
    url = t.get("audiodownload")
    if not url:
        return None
    sep = "&" if "?" in url else "?"
    url = url + sep + urllib.parse.urlencode({"client_id": client_id})
    artist = sanitize(_clean(t.get("artist_name") or "desconocido"))
    album = sanitize(_clean((t.get("album_name") or "single") or "single"))
    title = sanitize(_clean(t.get("name") or f"track-{t.get('id')}"))
    # file en songs.json es relativo a PROJECT_ROOT; el dest va dentro de music/
    rel = Path("music") / artist / album / f"{title} - {t.get('id')}.mp3"
    dest = out_root.parent / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as fh:
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            fh.write(chunk)
    return rel


def build_params(args, limit, offset=0):
    params = {"limit": limit, "offset": offset, "order": args.order}
    if args.search:
        params["search"] = args.search
    if args.tags:
        params["tags"] = "+".join(args.tags)
    if args.fuzzytags:
        params["fuzzytags"] = "+".join(args.fuzzytags)
    if args.acoustic:
        params["acousticelectric"] = args.acoustic
    if args.vocal:
        params["vocalinstrumental"] = args.vocal
    if args.speed:
        params["speed"] = "+".join(args.speed)
    if args.duration_from is not None or args.duration_to is not None:
        lo = args.duration_from if args.duration_from is not None else 0
        hi = args.duration_to if args.duration_to is not None else 9999
        params["durationbetween"] = f"{lo}_{hi}"
    if args.boost:
        params["boost"] = args.boost
    return params


def load_target_climate(path):
    if not path:
        return None
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw:
            return raw
    except (json.JSONDecodeError, OSError):
        pass
    return None


def cmd_list(args):
    doc = api_get(args.client_id, build_params(args, args.limit))
    if doc.get("headers", {}).get("status") != "success":
        print("Error de la API:", doc.get("headers"), file=sys.stderr)
        return 1
    tracks = doc.get("results") or []
    if not tracks:
        print("Sin resultados.")
        return 0

    target_climate = load_target_climate(args.clima)
    existing = None
    if args.exclude_existing:
        existing = {s.get("jamendo_id")
                    for s in load_songs(args.songs) if s.get("jamendo_id")}

    rows = []
    for i, t in enumerate(tracks, 1):
        tid = str(t.get("id"))
        if existing is not None and tid in existing:
            continue
        lic = license_short(t.get("license_ccurl")) or "-"
        ok = "OK" if (t.get("audiodownload_allowed") and lic in EMIT_LICENSES) else "no"
        dur = f"{t.get('duration') or 0}s"
        dist_label = ""
        if target_climate:
            est = {k: v for k, v in climate_from_track(t).items()
                   if v is not None and k in target_climate}
            tgt = {k: v for k, v in target_climate.items() if k in est}
            if est and tgt:
                dist = climate_distance(est, tgt)
                if args.max_distance is not None and dist > args.max_distance:
                    continue
                dist_label = f"dist={dist:.2f} "
        rows.append(f"{i:2d}. [{lic:10s}] {t.get('artist_name'):28.28} "
                    f"{t.get('name'):42.42} {dur:5s} {dist_label}dwn={ok} id={tid}")
    if not rows:
        print("Sin candidatas: todas ya están en el catálogo o quedaron fuera "
              "del filtro de clima.")
        return 0
    print("\n".join(rows))
    print(f"\nTotal: {len(rows)}. Usar --ids con el id para descargar.")
    return 0


def cmd_download(args):
    ids = [x.strip() for x in args.ids if x.strip()]
    if not ids:
        print("Se necesita --ids (p. ej. --ids 1848357,1880336).", file=sys.stderr)
        return 1
    songs = load_songs(args.songs)
    existing = {s.get("jamendo_id") for s in songs if s.get("jamendo_id")}
    ever_seen = load_seen()
    out_root = Path(args.music)
    added = skipped = nonlegible = failed = 0
    for tid in ids:
        doc = api_get(args.client_id, {"id": tid})
        if doc.get("headers", {}).get("status") != "success":
            print(f"  ! {tid}: error de la API -> "
                  f"{doc.get('headers', {}).get('error_message')}", file=sys.stderr)
            failed += 1
            continue
        tracks = doc.get("results") or []
        if not tracks:
            print(f"  - {tid}: no existe")
            failed += 1
            continue
        t = tracks[0]
        tid = str(t.get("id"))
        entry = build_entry(t)
        if entry is None:
            print(f"  - {tid}: licencia NO emisible ({t.get('license_ccurl')}) -> saltado")
            nonlegible += 1
            continue
        if tid in existing:
            print(f"  - {tid}: ya en el catálogo -> saltado")
            skipped += 1
            continue
        if tid in ever_seen:
            print(f"  - {tid}: ya descargado antes (historial persistente) -> saltado")
            skipped += 1
            continue
        if not t.get("audiodownload_allowed"):
            print(f"  - {tid}: audiodownload_allowed=false -> saltado")
            nonlegible += 1
            continue
        rel = download_track(args.client_id, t, out_root)
        if rel is None:
            print(f"  ! {tid}: fallo la descarga")
            failed += 1
            continue
        print(f"  + [{entry['license']}] {entry['artist']} — {entry['title']} -> {rel}")
        entry["file"] = str(rel)
        songs.append(entry)
        existing.add(tid)
        ever_seen.add(tid)
        added += 1
    if not args.no_save and added:
        save_songs(args.songs, songs)
        save_seen(ever_seen)
    print(f"\nResumen: {added} añadidas, {skipped} ya presentes, "
          f"{nonlegible} no emisibles, {failed} con error.")
    if added:
        print("Clima y atribución rellenados desde la API. Ejecuta luego "
              "selector.py para regenerar la cola.")
    return 0


def cmd_batch(args):
    """--batch: descarga hasta args.limit temas emisibles sin confirmación,
    excluyendo los ya presentes, paginando la API de Jamendo."""
    PAGE = min(200, max(args.limit, 1))
    songs = load_songs(args.songs)
    existing = {s.get("jamendo_id") for s in songs if s.get("jamendo_id")}
    ever_seen = load_seen()
    out_root = Path(args.music)
    target_climate = load_target_climate(args.clima)
    max_dist = args.max_distance

    added = fetched = filtered = nonlegible = failed = 0
    page_seen = set()
    offset = 0
    try_limit = max(args.limit * 4, 200)

    def fits_climate(t):
        if target_climate is None or max_dist is None:
            return True, ""
        est = {k: v for k, v in climate_from_track(t).items()
               if v is not None and k in target_climate}
        tgt = {k: v for k, v in target_climate.items() if k in est}
        if not est or not tgt:
            return True, ""
        dist = climate_distance(est, tgt)
        return dist <= max_dist, f"dist={dist:.2f} "

    while added < args.limit and fetched < try_limit:
        remaining = args.limit - added + PAGE
        limit = min(PAGE, remaining)
        doc = api_get(args.client_id, build_params(args, limit, offset))
        if doc.get("headers", {}).get("status") != "success":
            print("Error de la API:", doc.get("headers"), file=sys.stderr)
            break
        tracks = doc.get("results") or []
        if not tracks:
            break
        fetched += len(tracks)
        for t in tracks:
            if added >= args.limit:
                break
            tid = str(t.get("id"))
            if tid in existing or tid in ever_seen or tid in page_seen:
                continue
            page_seen.add(tid)
            entry = build_entry(t)
            if entry is None:
                nonlegible += 1
                continue
            if not t.get("audiodownload_allowed"):
                nonlegible += 1
                continue
            ok, label = fits_climate(t)
            if not ok:
                filtered += 1
                continue
            rel = download_track(args.client_id, t, out_root)
            if rel is None:
                print(f"  ! {tid}: fallo la descarga")
                failed += 1
                continue
            entry["file"] = str(rel)
            print(f"  + [{entry['license']}] {entry['artist']} — {entry['title']}"
                  f" {label}-> {rel}")
            songs.append(entry)
            existing.add(tid)
            ever_seen.add(tid)
            added += 1
        if len(tracks) < limit:
            break
        offset += len(tracks)

    if added and not args.no_save:
        save_songs(args.songs, songs)
        save_seen(ever_seen)
    print(f"\nResumen batch: {added} añadidas, {nonlegible} no emisibles, "
          f"{filtered} fuera de clima, {failed} con error.")
    print(f"Historial persistente: {len(ever_seen)} jamendo_id únicos descargados.")
    if added:
        print("Ejecuta luego selector.py para regenerar la cola.")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Descubre/descarga música emisible de Jamendo (API v3)")
    ap.add_argument("--client-id", default=os.environ.get("JAMENDO_CLIENT_ID"),
                    help="Client ID de devportal.jamendo.com (o env JAMENDO_CLIENT_ID)")
    ap.add_argument("--songs", default=str(PROJECT_ROOT / "songs.json"))
    ap.add_argument("--music", default=str(PROJECT_ROOT / "music"))
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--order", default="relevance")
    ap.add_argument("--boost", default=None)
    ap.add_argument("--search", default=None)
    ap.add_argument("--tags", default=None, help="csv, combinados con AND")
    ap.add_argument("--fuzzytags", default=None, help="csv, combinados con OR")
    ap.add_argument("--acoustic", choices=["acoustic", "electric"], default=None)
    ap.add_argument("--vocal", choices=["vocal", "instrumental"], default=None)
    ap.add_argument("--speed", default=None,
                    help="csv: verylow,low,medium,high,veryhigh")
    ap.add_argument("--duration-from", type=int, default=None)
    ap.add_argument("--duration-to", type=int, default=None)
    ap.add_argument("--clima", default=None,
                    help="JSON del clima objetivo: oculta candidatas cuya "
                         "distancia estimada lo supere (o muestra 'dist=')")
    ap.add_argument("--max-distance", type=float, default=None,
                    help="umbral 0.0-1.0: ocultar candidatas por encima")
    ap.add_argument("--exclude-existing", action="store_true",
                    help="ocultar ids ya presentes en songs.json")
    ap.add_argument("--ids", default=None, help="csv de track ids a descargar")
    ap.add_argument("--download", action="store_true",
                    help="modo descarga (requiere --ids)")
    ap.add_argument("--batch", action="store_true",
                    help="modo lote: descarga hasta --limit temas emisibles sin "
                         "confirmar, excluyendo los ya presentes")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    if not args.client_id:
        print("Falta el client_id de Jamendo (--client-id o env JAMENDO_CLIENT_ID). "
              "Consíguelo en https://devportal.jamendo.com", file=sys.stderr)
        return 1
    if args.tags:
        args.tags = [x.strip() for x in args.tags.split(",") if x.strip()]
    if args.fuzzytags:
        args.fuzzytags = [x.strip() for x in args.fuzzytags.split(",") if x.strip()]
    if args.speed:
        args.speed = [x.strip() for x in args.speed.split(",") if x.strip()]
        bad = [s for s in args.speed
               if s not in ("verylow", "low", "medium", "high", "veryhigh")]
        if bad:
            print(f"speed inválido: {bad}", file=sys.stderr)
            return 1
    if args.ids:
        args.ids = [x.strip() for x in args.ids.split(",") if x.strip()]
        args.download = True

    if args.batch:
        return cmd_batch(args)
    if args.download:
        return cmd_download(args)
    return cmd_list(args)


if __name__ == "__main__":
    sys.exit(main())