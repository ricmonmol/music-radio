#!/usr/bin/env python3
"""Registro de visitas a las radios, consultable desde el servidor.

De dónde salen los datos
------------------------
Icecast ya escribe una línea por conexión en /var/log/icecast2/access.log con
IP, fecha, ruta, status, bytes, referer, user-agent y duración en segundos
(último campo). No hay que instrumentar nada: se lee ese archivo.

El problema es que ese archivo está casi lleno de basura. El gate de oyentes de
radio.liq pregunta el status a Icecast cada IDLE_POLL segundos, y eso son del
orden de 1,8 MB/día, con 92% de ruido. Icecast borra el log al llegar a 10 MB
(por eso access.log.old mide 10.240.022 bytes exactos), así que el histórico
real sería de poco más de una semana.

Por eso hay dos fuentes y se leen las dos:

  logs/visits/AAAA-MM-DD.tsv    snapshot propio, una línea por visita, chico
                                (~10 KB/día, 3,5 MB al año). Lo escribe
                                --snapshot, que cleanup_logs.sh llama @daily.
                                Un archivo por día, así que no se pisa nada.
  /var/log/icecast2/access.log* el log crudo de Icecast, incluidos .old y los
                                .gz de logrotate. Aporta lo que todavía no
                                está en un snapshot.

Se deduplican por (mount, ip, fin, duración, bytes).

Lo que este registro NO da
--------------------------
- Conexiones, no personas: alguien que reconecta tres veces cuenta tres visitas.
- Quien escucha desde el panel entra por el proxy de web_server.py, así que
  Icecast registra 127.0.0.1. El origen real se saca del referer.
- Icecast loguea al DESCONECTAR: la fecha es el fin de la visita, y una
  conexión viva todavía no aparece.

Modos:
  visits.py                  resumen por día (default)
  visits.py --visits         detalle visita por visita
  visits.py --snapshot       vuelca las visitas a logs/visits/ (cron diario)

Solo stdlib, como el resto del proyecto. No expone nada por la web: esto se lee
por SSH con menu.sh o con curl + jq.
"""

import argparse
import gzip
import json
import os
import re
import sys
import tempfile
import zlib
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ICECAST_LOG_DIR = Path(os.environ.get("ICECAST_LOG_DIR") or "/var/log/icecast2")
VISITS_DIR = Path(os.environ.get("VISITS_DIR") or (PROJECT_ROOT / "logs" / "visits"))
STATIONS_PATH = Path(os.environ.get("STATIONS_PATH") or (PROJECT_ROOT / "stations.json"))

# Icecast escribe en formato Apache "combined" más un último campo con la
# duración de la conexión en segundos:
#   IP - - [fecha] "GET /radio HTTP/1.1" 200 bytes "referer" "user-agent" seg
LINE_RE = re.compile(
    r'^(\S+) \S+ \S+ \[([^\]]+)\] "(\S+) (\S+) [^"]*" '
    r'(\d+) (\S+) "([^"]*)" "([^"]*)" (\d+)\s*$'
)
TS_RE = re.compile(
    r"^(\d{2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})$"
)
# Meses en inglés fijo: strptime("%b") depende del locale y con LC_TIME=es_ES
# "Sep" no parsea, y las visitas desaparecen del reporte sin avisar.
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

LOOPBACK = {"::1", "localhost"}
# Único status que significa "alguien escuchó de verdad". Un 404 es el mount
# inexistente y no se cuenta como visita.
OK_STATUS = 200
# Rutas de icecast que NO son visitas. El method == "GET" ya descarta los
# SOURCE que empuja liquidsoap, pero el gate sigue pidiendo status-json.xsl
# cada IDLE_POLL segundos: eso es el 92% del archivo.
IGNORED_PATHS = {
    "/status-json.xsl", "/status.xsl", "/admin/listclients.xml",
    "/admin/listmounts", "/admin/stats", "/admin/",
}

TSV_FIELDS = 8


class Visit:
    """Una conexión terminada a un mount."""

    __slots__ = ("when", "mount", "status", "nbytes", "duration",
                 "ip", "referer", "agent")

    def __init__(self, when, mount, status, nbytes, duration, ip, referer, agent):
        self.when = when
        self.mount = mount
        self.status = status
        self.nbytes = nbytes
        self.duration = duration
        self.ip = ip
        self.referer = referer
        self.agent = agent

    @property
    def start(self):
        return self.when - timedelta(seconds=self.duration)

    @property
    def key(self):
        return (self.mount, self.ip, self.when.isoformat(),
                self.duration, self.nbytes)

    def as_dict(self):
        return {
            "fin": self.when.isoformat(),
            "inicio": self.start.isoformat(),
            "mount": self.mount,
            "status": self.status,
            "bytes": self.nbytes,
            "duracion_s": self.duration,
            "ip": self.ip,
            "referer": self.referer,
            "user_agent": self.agent,
            "origen": origin(self),
            "cliente": client_class(self.agent),
        }

    def to_tsv(self):
        return "\t".join(str(v) for v in (
            self.when.isoformat(), self.mount, self.status, self.nbytes,
            self.duration, self.ip,
            clean_field(self.referer), clean_field(self.agent),
        ))

    @classmethod
    def from_tsv(cls, line):
        parts = line.rstrip("\n").split("\t")
        if len(parts) != TSV_FIELDS:
            return None
        try:
            return cls(
                when=datetime.fromisoformat(parts[0]),
                mount=parts[1],
                status=int(parts[2]),
                nbytes=int(parts[3]),
                duration=int(parts[4]),
                ip=parts[5],
                referer=parts[6],
                agent=parts[7],
            )
        except (ValueError, TypeError):
            return None


def clean_field(value):
    """Un tab o un salto de línea dentro del referer rompe el TSV."""
    return re.sub(r"[\t\r\n]+", " ", value or "")


# ── clocks ───────────────────────────────────────────────────────────────────

def parse_ts(raw):
    """'07/Sep/2026:16:53:59 -0300' -> datetime con zona horaria."""
    m = TS_RE.match(raw)
    if not m:
        return None
    day, mon, year, hh, mm, ss, tz = m.groups()
    if mon not in MONTHS:
        return None
    sign = -1 if tz[0] == "-" else 1
    offset = timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])) * sign
    try:
        return datetime(int(year), MONTHS[mon], int(day), int(hh), int(mm),
                        int(ss), tzinfo=timezone(offset))
    except ValueError:
        return None


def now_local():
    return datetime.now().astimezone()


# ── orígenes ─────────────────────────────────────────────────────────────────

def mask_ip(host, full=False):
    """192.168.88.37 -> 192.168.88.x

    El reporte no busca saber quién escucha sino de dónde, así que la IP se
    deja en /24 por defecto. --full-ip la enseña entera.
    """
    if full or not host:
        return host
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return "{}.{}.{}.x".format(parts[0], parts[1], parts[2])
    return host


def referer_host(referer):
    """'http://192.168.88.219:8080/' -> '192.168.88.219'."""
    if not referer or referer == "-":
        return ""
    m = re.match(r"^\w+://([^/:?#]+)", referer)
    return m.group(1) if m else ""


def is_loopback(ip):
    return ip in LOOPBACK or ip.startswith("127.") or ip == "::ffff:127.0.0.1"


def origin(visit, full_ip=False):
    """De dónde vino la visita, sin identidades.

    Casi todo el tráfico entra por el proxy de web_server.py, así que Icecast
    ve 127.0.0.1 y el host real viene en el referer. Las visitas directas
    (VLC contra el 8000) sí traen IP propia.
    """
    if is_loopback(visit.ip):
        host = referer_host(visit.referer)
        if host:
            return "panel " + mask_ip(host, full_ip)
        return "panel local"
    return mask_ip(visit.ip, full_ip)


BROWSERS = [
    ("firefox", "firefox"), ("chromium", "chrome"), ("chrome", "chrome"),
    ("safari", "safari"), ("vlc", "vlc"), ("curl", "curl"),
    ("wget", "wget"), ("libmpv", "mpv"), ("gstreamer", "gstreamer"),
]
PLATFORMS = [
    ("android", "android"), ("ipad", "ios"), ("iphone", "ios"),
    ("windows", "windows"), ("mac os", "macos"), ("macintosh", "macos"),
    ("x11", "linux"), ("linux", "linux"),
]


def client_class(agent):
    """'Mozilla/5.0 (X11; Linux...) Firefox/153' -> 'firefox/linux'."""
    text = (agent or "").lower()
    browser = next((b for k, b in BROWSERS if k in text), "otro")
    platform = next((p for k, p in PLATFORMS if k in text), "?")
    return "{}/{}".format(browser, platform)


# ── estaciones ───────────────────────────────────────────────────────────────

def load_mounts(path=STATIONS_PATH):
    """{mount: label} desde stations.json. Si no se puede leer, {}."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    mounts = {}
    for raw in doc.get("stations") or []:
        if isinstance(raw, dict) and raw.get("mount"):
            mounts[raw["mount"]] = raw.get("label") or raw["id"]
    return mounts


# ── lectura ──────────────────────────────────────────────────────────────────

def parse_line(line, mounts):
    m = LINE_RE.match(line)
    if not m:
        return None
    ip, raw_ts, method, path, status, nbytes, referer, agent, dur = m.groups()
    # method == "GET" descarta los ~35.000 "SOURCE /radio" que empuja liquidsoap
    # a Icecast: son la mitad de las líneas del log.
    if method != "GET" or path in IGNORED_PATHS:
        return None
    when = parse_ts(raw_ts)
    if when is None:
        return None
    if mounts and path not in mounts:
        return None
    try:
        return Visit(when=when, mount=path, status=int(status),
                     nbytes=int(nbytes), duration=int(dur), ip=ip,
                     referer=referer, agent=agent)
    except ValueError:
        # nbytes "-" en un 4xx: no es una visita.
        return None


def iter_access_lines(log_dir=None):
    """Rinde las líneas de access.log, access.log.old y los .gz de logrotate."""
    log_dir = Path(log_dir or ICECAST_LOG_DIR)
    try:
        paths = sorted(log_dir.glob("access.log*"))
    except OSError:
        return
    for path in paths:
        if not path.is_file():
            continue
        try:
            if path.suffix == ".gz":
                handle = gzip.open(path, "rt", encoding="utf-8", errors="replace")
            else:
                handle = path.open("r", encoding="utf-8", errors="replace")
            with handle as fh:
                for line in fh:
                    yield line
        except (OSError, EOFError, zlib.error):
            # Un log corrupto no puede tumbar el reporte entero. Sale EOFError
            # (y no OSError) con un .gz truncado a medio rotar, que es
            # justo lo que deja logrotate, y zlib.error si el deflate está
            # estropeado.
            continue


def read_access_log(mounts, log_dir=None):
    out = []
    for line in iter_access_lines(log_dir):
        visit = parse_line(line, mounts)
        if visit is not None:
            out.append(visit)
    return out


def read_snapshots(visits_dir=None):
    out = []
    try:
        paths = sorted(Path(visits_dir or VISITS_DIR).glob("*.tsv"))
    except OSError:
        return out
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            visit = Visit.from_tsv(line)
            if visit is not None:
                out.append(visit)
    return out


def load_visits(mounts=None, log_dir=None, visits_dir=None):
    """Snapshots + log crudo, del más reciente al más viejo.

    Ojo con los defaults: se resuelven dentro y no como argumentos por defecto,
    porque esos se evalúan una sola vez al definir la función y reasignar
    ICECAST_LOG_DIR no surtiría efecto.

    El access.log manda: sus líneas se toman tal cual, sin deduplicar entre sí,
    porque cada línea YA es una conexión. Icecast no da id de conexión ni
    marca de tiempo más fina que el segundo, así que dos reconexiones en el
    mismo segundo con la misma cantidad de bytes son indistinguibles, y
    colapsarlas contaría menos visitas de las que hubo (el panel llega a abrir
    cinco a la vez cuando algo se traba). Deduplicar solo tiene sentido entre
    fuentes: una visita ya volcada a un snapshot no se vuelve a contar porque
    siga todavía en el access.log.
    """
    if mounts is None:
        mounts = load_mounts()
    access = read_access_log(mounts, log_dir)
    in_access = {visit.key for visit in access}
    merged = access + [v for v in read_snapshots(visits_dir)
                       if v.key not in in_access]
    return sorted(merged, key=lambda v: v.when, reverse=True)


def recent(visits, days, mount=None):
    """Filtra por mount y ventana. days <= 0 es todo el histórico."""
    out = visits
    if mount:
        out = [v for v in out if v.mount == mount]
    if days and days > 0:
        cutoff = now_local() - timedelta(days=days)
        out = [v for v in out if v.when >= cutoff]
    return out


def split_failed(visits):
    """Separa lo que se escuchó de lo que falló.

    Icecast contesta 404 a quien pide un mount que no existe: 394 bytes y 0
    segundos. En el histórico medido eso es el 91% de las líneas (2.247 de
    2.460), casi todas de días con la estación caída, así que contarlas como
    visitas infla el reporte y tapa los minutos realmente escuchados. Se
    excluyen del reporte, pero el número se conserva para decir cuántos
    intentos fallidos hubo.
    """
    ok = [v for v in visits if v.status == OK_STATUS]
    return ok, len(visits) - len(ok)


# ── formato ──────────────────────────────────────────────────────────────────

def fmt_dur(seconds):
    if seconds < 60:
        return "{}s".format(seconds)
    if seconds < 3600:
        return "{}m".format(seconds // 60)
    return "{}h{:02d}".format(seconds // 3600, (seconds % 3600) // 60)


def render_table(headers, rows, aligns=None, pad="  "):
    """Tabla de texto plano. Todas las líneas reciben el mismo pad."""
    aligns = aligns or ["<"] * len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def line(cells):
        return (pad + " ".join(
            "{:{}{}}".format(str(c), aligns[i], widths[i])
            for i, c in enumerate(cells)
        )).rstrip()

    out = [line(headers), pad + "─" * (sum(widths) + len(widths) - 1)]
    out += [line(row) for row in rows]
    return out


# ── reporte: resumen por día ─────────────────────────────────────────────────

def daily_report(visits, mounts, full_ip=False, failed=0):
    if not visits:
        return ["  (sin visitas registradas)"] + failed_note(failed)
    by_day = {}
    for visit in visits:
        by_day.setdefault(visit.when.date(), []).append(visit)

    mount_names = sorted({v.mount for v in visits})
    headers = ["fecha"] + [mounts.get(m, m) for m in mount_names] + [
        "visitas", "origenes", "minutos", "mas_larga"]
    aligns = ["<"] + [">"] * len(mount_names) + [">", ">", ">", ">"]

    rows = []
    total_visits = total_seconds = 0
    longest = 0
    origin_totals = Counter()
    client_totals = Counter()
    for day in sorted(by_day):
        day_visits = by_day[day]
        per_mount = Counter(v.mount for v in day_visits)
        origins = {origin(v, full_ip) for v in day_visits}
        seconds = sum(v.duration for v in day_visits)
        longest = max(longest, max(v.duration for v in day_visits))
        total_visits += len(day_visits)
        total_seconds += seconds
        rows.append(
            [day.isoformat()]
            + [per_mount.get(m, 0) for m in mount_names]
            + [len(day_visits), len(origins), "{:.1f}".format(seconds / 60.0),
               fmt_dur(longest)]
        )
        origin_totals.update(origin(v, full_ip) for v in day_visits)
        client_totals.update(client_class(v.agent) for v in day_visits)

    lines = ["  ── visitas por día " + "─" * 34, ""]
    lines += render_table(headers, rows, aligns)
    lines += [
        "",
        "  {} días · {} conexiones · {:.1f} minutos · más larga {}".format(
            len(by_day), total_visits, total_seconds / 60.0, fmt_dur(longest)),
        "",
    ]
    if origin_totals:
        lines.append("  orígenes: " + "   ".join(
            "{} ×{}".format(o, n) for o, n in origin_totals.most_common()))
    if client_totals:
        lines.append("  clientes: " + "   ".join(
            "{} ×{}".format(c, n) for c, n in client_totals.most_common(4)))
    lines += [
        "",
        "  son conexiones, no personas: quien reconecta cuenta varias veces.",
        "  el tráfico del panel entra por el proxy y figura como 127.0.0.1.",
    ]
    return lines + failed_note(failed)


# ── reporte: detalle ─────────────────────────────────────────────────────────

def failed_note(failed):
    if not failed:
        return []
    return [
        "",
        "  además {} intento(s) fallido(s): status 404, el mount no existía.".format(failed),
    ]


def visits_report(visits, mounts, limit, full_ip=False, failed=0):
    if not visits:
        return ["  (sin visitas registradas)"] + failed_note(failed)
    # Se ordena acá y no se confia en el orden de entrada: --limit corta por
    # arriba, así que sin esto una lista sin ordenar mostraría las más viejas
    # en vez de las más recientes.
    shown = sorted(visits, key=lambda v: v.when, reverse=True)
    if limit and limit > 0:
        shown = shown[:limit]
    rows = []
    for visit in shown:
        rows.append([
            visit.when.strftime("%Y-%m-%d %H:%M:%S"),
            mounts.get(visit.mount, visit.mount),
            fmt_dur(visit.duration),
            origin(visit, full_ip),
            client_class(visit.agent),
        ])
    lines = ["  ── detalle de visitas " + "─" * 32, ""]
    lines += render_table(
        ["fin", "estación", "duración", "origen", "cliente"],
        rows, ["<", "<", ">", "<", "<"])
    if len(shown) < len(visits):
        lines.append("")
        lines.append("  mostrando {} de {} (--limit)".format(
            len(shown), len(visits)))
    if not full_ip:
        lines.append("")
        lines.append("  IP en /24: por defecto no se guardan identidades.")
    return lines + failed_note(failed)


def daily_json(visits, mounts, full_ip=False, failed=0):
    by_day = {}
    for visit in visits:
        by_day.setdefault(visit.when.date().isoformat(), []).append(visit)
    days = []
    for day in sorted(by_day):
        day_visits = by_day[day]
        days.append({
            "fecha": day,
            "visitas": len(day_visits),
            "conexiones_por_mount": dict(
                Counter(v.mount for v in day_visits)),
            "origenes": len({origin(v, full_ip) for v in day_visits}),
            "segundos": sum(v.duration for v in day_visits),
            "mas_larga_s": max(v.duration for v in day_visits),
            "origenes_detalle": dict(
                Counter(origin(v, full_ip) for v in day_visits)),
            "clientes": dict(
                Counter(client_class(v.agent) for v in day_visits)),
        })
    return {"monts": mounts, "intentos_fallidos": failed, "dias": days}


# ── snapshot diario ──────────────────────────────────────────────────────────

def write_day(path, visits):
    """Escribe por temporal y renombra: si lo corta el cron no queda a medias."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for visit in visits:
                fh.write(visit.to_tsv() + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def snapshot(days=7, prune_days=3650, mounts=None, log_dir=None,
             visits_dir=None):
    """Vuelca las visitas a un archivo por día y devuelve (escritas, borradas).

    Se reescriben los últimos `days` días completos en vez de anexar al del
    día: una visita que a las 00:05 todavía no había terminado entra sola en el
    snapshot siguiente, sin cursor ni duplicados. Es idempotente.
    """
    if mounts is None:
        mounts = load_mounts()
    # El default se resuelve acá y no como argumento: los defaults de una
    # función se evalúan una sola vez al definirla.
    directory = Path(visits_dir or VISITS_DIR)
    visits = load_visits(mounts, log_dir, visits_dir)

    wanted = None
    if days and days > 0:
        base = now_local().date()
        wanted = {base - timedelta(days=n) for n in range(days + 1)}

    grouped = {}
    for visit in visits:
        day = visit.when.date()
        if wanted is None or day in wanted:
            # Lista y no dict: load_visits ya resolvió el solape entre fuentes,
            # y colapsar por key aquí volvería a perder las reconexiones
            # legítimas que caen en el mismo segundo.
            grouped.setdefault(day, []).append(visit)

    written = 0
    for day in sorted(grouped):
        day_visits = sorted(grouped[day], key=lambda v: v.when)
        write_day(directory / "{}.tsv".format(day.isoformat()), day_visits)
        written += len(day_visits)

    pruned = 0
    if prune_days and prune_days > 0:
        cutoff = (now_local() - timedelta(days=prune_days)).timestamp()
        try:
            stale = list(directory.glob("*.tsv"))
        except OSError:
            stale = []
        for path in stale:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    pruned += 1
            except OSError:
                continue
    return written, pruned


# ── cli ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Registro de visitas a las radios (lee el access.log de icecast)")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--daily", action="store_true",
                       help="resumen por día (default)")
    group.add_argument("--visits", action="store_true",
                       help="detalle visita por visita")
    group.add_argument("--snapshot", action="store_true",
                       help="vuelca a logs/visits/ (lo llama cleanup_logs.sh)")
    ap.add_argument("--days", type=int, default=30,
                    help="ventana en días; 0 = todo el histórico (default 30)")
    ap.add_argument("--snapshot-days", type=int, default=7,
                    help="días a reescribir en --snapshot (default 7)")
    ap.add_argument("--prune-days", type=int, default=3650,
                    help="borrar snapshots de más de N días en --snapshot; 0 = no")
    ap.add_argument("--mount", help="filtra por mount, p.ej. /radio")
    ap.add_argument("--errors", action="store_true",
                    help="cuenta también los 404 (mount caído), no solo lo "
                         "que se escuchó de verdad")
    ap.add_argument("--limit", type=int, default=40,
                    help="máximo de filas en --visits; 0 = todas")
    ap.add_argument("--full-ip", action="store_true",
                    help="enseña la IP entera en vez de /24")
    ap.add_argument("--json", action="store_true", help="salida JSON")
    args = ap.parse_args(argv)

    if args.snapshot:
        written, pruned = snapshot(days=args.snapshot_days,
                                   prune_days=args.prune_days)
        print("visits: {} visitas volcadas a {}/ ({} días){}".format(
            written, VISITS_DIR, args.snapshot_days,
            ", {} archivo(s) borrados".format(pruned) if pruned else ""))
        return 0

    mounts = load_mounts()
    # Con --json el stdout tiene que ser JSON puro: un banner antes lo rompe.
    if not args.json:
        if not mounts:
            print("aviso: sin stations.json, se muestran los mounts en crudo\n")
        elif len(mounts) > 1 and not args.mount:
            print("  estaciones: " + "   ".join(
                "{} ({})".format(m, label) for m, label in sorted(mounts.items())))
            print("")

    visits = recent(load_visits(mounts), args.days, args.mount)
    if args.errors:
        failed = 0
    else:
        visits, failed = split_failed(visits)

    if args.json:
        if args.visits:
            shown = visits[:args.limit] if args.limit and args.limit > 0 else visits
            data = {
                "intentos_fallidos": failed,
                "visitas": [v.as_dict() for v in shown],
            }
        else:
            data = daily_json(visits, mounts, args.full_ip, failed)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if args.visits:
        print("\n".join(visits_report(
            visits, mounts, args.limit, args.full_ip, failed)))
    else:
        print("\n".join(daily_report(visits, mounts, args.full_ip, failed)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(0)
