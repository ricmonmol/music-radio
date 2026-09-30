import gzip
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "visits.py"
spec = importlib.util.spec_from_file_location("visits_under_test", MODULE_PATH)
visits = importlib.util.module_from_spec(spec)
spec.loader.exec_module(visits)

FF_UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:153.0) Gecko/20100101 "
         "Firefox/153.0")
TZ = timezone(timedelta(hours=-3))
MOUNTS = {"/radio": "jamendo radio", "/jacobs": "jacobs collection"}


def line(ts, path, method="GET", status=200, nbytes=1000,
         referer="-", agent=FF_UA, seconds=10, ip="127.0.0.1"):
    return ('{ip} - - [{ts}] "{method} {path} HTTP/1.1" {status} {nbytes} '
            '"{referer}" "{agent}" {seconds}\n').format(
        ip=ip, ts=ts, method=method, path=path, status=status, nbytes=nbytes,
        referer=referer, agent=agent, seconds=seconds)


class ParseLineTests(unittest.TestCase):
    """El log de icecast mezcla visitas con mucho ruido: esto es lo que separa."""

    def test_parsea_una_visita(self):
        v = visits.parse_line(
            line("07/Sep/2026:16:53:59 -0300", "/radio"), MOUNTS)
        self.assertIsNotNone(v)
        self.assertEqual(v.mount, "/radio")
        self.assertEqual(v.status, 200)
        self.assertEqual(v.duration, 10)
        self.assertEqual(v.ip, "127.0.0.1")
        self.assertEqual(v.when.isoformat(), "2026-09-07T16:53:59-03:00")

    def test_inicio_es_fin_menos_duracion(self):
        # Icecast loguea al DESCONECTAR: la fecha es el fin.
        v = visits.parse_line(
            line("27/Sep/2026:17:13:51 -0300", "/jacobs", seconds=1514), MOUNTS)
        self.assertEqual(v.start.isoformat(), "2026-09-27T16:48:37-03:00")

    def test_descarta_source_que_empuja_liquidsoap(self):
        # Son la mitad de las lineas del access.log real.
        self.assertIsNone(visits.parse_line(
            line("29/Sep/2026:18:42:34 -0300", "/radio", method="SOURCE",
                 agent="Liquidsoap/2.2.4-1+dev (Unix; OCaml 4.14.1)"), MOUNTS))

    def test_descarta_el_sondeo_del_gate(self):
        # El 92% del archivo: el gate pregunta el status cada IDLE_POLL.
        self.assertIsNone(visits.parse_line(
            line("29/Sep/2026:18:42:36 -0300", "/status-json.xsl", nbytes=1388,
                 agent="Python-urllib/3.12"), MOUNTS))

    def test_descarta_rutas_que_no_son_mounts(self):
        self.assertIsNone(visits.parse_line(
            line("29/Sep/2026:18:42:36 -0300", "/jacobs.m3u"), MOUNTS))
        self.assertIsNone(visits.parse_line(
            line("29/Sep/2026:18:42:36 -0300", "/admin/metadata"), MOUNTS))

    def test_descarta_404_por_bytes_dash(self):
        self.assertIsNone(visits.parse_line(
            line("29/Sep/2026:18:42:36 -0300", "/radio", nbytes="-"), MOUNTS))

    def test_acepta_404_con_bytes_numericos(self):
        # 404 = mount inexistente: se conserva para contar intentos fallidos.
        v = visits.parse_line(
            line("28/Sep/2026:16:02:29 -0300", "/jacobs", status=404,
                 nbytes=394, seconds=0), MOUNTS)
        self.assertIsNotNone(v)
        self.assertEqual(v.status, 404)

    def test_acepta_404_igual_que_este_en_ignorados(self):
        # /radio y /jacobs son mounts, nunca admin: el filtro va por method.
        v = visits.parse_line(
            line("23/Sep/2026:16:00:00 -0300", "/radio", status=404), MOUNTS)
        self.assertIsNotNone(v)

    def test_linea_que_no_parsea(self):
        self.assertIsNone(visits.parse_line("basura\n", MOUNTS))
        self.assertIsNone(visits.parse_line("", MOUNTS))

    def test_mes_en_ingles_fijo(self):
        # strptime('%b') con LC_TIME=es_ES no parsea 'Sep' y las visitas
        # desaparecen del reporte sin avisar.
        for raw, expected in [("07/Sep/2026:00:00:00 -0300", (2026, 9, 7)),
                              ("01/Jan/2026:00:00:00 -0300", (2026, 1, 1)),
                              ("31/Dec/2026:00:00:00 -0300", (2026, 12, 31))]:
            parsed = visits.parse_ts(raw)
            self.assertEqual((parsed.year, parsed.month, parsed.day), expected)

    def test_fecha_invalida_no_revienta(self):
        self.assertIsNone(visits.parse_ts("99/Xxx/2026:00:00:00 -0300"))
        self.assertIsNone(visits.parse_ts("no-es-una-fecha"))


class OriginTests(unittest.TestCase):
    """El panel entra por el proxy, asi que icecast ve 127.0.0.1 siempre."""

    def visit(self, ip, referer="-"):
        v = visits.parse_line(
            line("28/Sep/2026:16:00:00 -0300", "/radio", ip=ip,
                 referer=referer), MOUNTS)
        return v

    def test_trafico_del_panel_usa_el_referer(self):
        v = self.visit("127.0.0.1", "http://192.168.88.219:8080/")
        self.assertEqual(visits.origin(v), "panel 192.168.88.x")
        self.assertEqual(visits.origin(v, full_ip=True),
                         "panel 192.168.88.219")

    def test_panel_sin_referer(self):
        self.assertEqual(visits.origin(self.visit("127.0.0.1")),
                         "panel local")
        self.assertEqual(visits.origin(self.visit("::1")), "panel local")

    def test_visita_directa_usa_la_ip(self):
        self.assertEqual(visits.origin(self.visit("192.168.88.37")),
                         "192.168.88.x")

    def test_ip_se_enmascara_por_defecto(self):
        # No se guardan identidades: /24 y punto.
        self.assertEqual(visits.mask_ip("192.168.88.37"), "192.168.88.x")
        self.assertEqual(visits.mask_ip("192.168.88.37", full=True),
                         "192.168.88.37")
        # Hostnames y IPv6 no son IPs: se dejan como vienen.
        self.assertEqual(visits.mask_ip("localhost"), "localhost")
        self.assertEqual(visits.mask_ip(""), "")

    def test_clase_de_cliente(self):
        self.assertEqual(visits.client_class(FF_UA), "firefox/linux")
        self.assertEqual(visits.client_class("curl/8.5.0"), "curl/?")
        self.assertEqual(visits.client_class("VLC/3.0.18 LibVLC/3.0.18"),
                         "vlc/?")
        self.assertEqual(visits.client_class(""), "otro/?")


class TsvTests(unittest.TestCase):
    def test_ida_y_vuelta(self):
        v = visits.parse_line(
            line("27/Sep/2026:17:13:51 -0300", "/jacobs", seconds=1514,
                 referer="http://localhost:8080/"), MOUNTS)
        back = visits.Visit.from_tsv(v.to_tsv())
        self.assertIsNotNone(back)
        self.assertEqual(back.key, v.key)
        self.assertEqual(back.referer, v.referer)
        self.assertEqual(back.agent, v.agent)

    def test_referer_con_tab_no_rompe_el_tsv(self):
        v = visits.parse_line(
            line("27/Sep/2026:17:13:51 -0300", "/radio",
                 referer="http://x/\ty\tz"), MOUNTS)
        self.assertEqual(v.to_tsv().count("\t"), visits.TSV_FIELDS - 1)
        self.assertIsNotNone(visits.Visit.from_tsv(v.to_tsv()))

    def test_linea_corta_se_ignora(self):
        self.assertIsNone(visits.Visit.from_tsv("no\tes\ttsv"))


class LoadVisitsTests(unittest.TestCase):
    """El access.log manda; el snapshot solo aporta lo que ya rotto."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log_dir = self.root / "icecast"
        self.visits_dir = self.root / "visits"
        self.log_dir.mkdir()
        self.visits_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write_log(self, name, text):
        (self.log_dir / name).write_text(text, encoding="utf-8")

    def load(self):
        return visits.load_visits(MOUNTS, self.log_dir, self.visits_dir)

    def test_lee_gz(self):
        # logrotate comprime access.log.1.gz y hay que seguir leyéndolo.
        raw = line("28/Sep/2026:16:00:00 -0300", "/radio").encode("utf-8")
        with gzip.open(self.log_dir / "access.log.2.gz", "wb") as fh:
            fh.write(raw)
        self.assertEqual(len(self.load()), 1)

    def test_une_log_y_snapshot_sin_duplicar(self):
        self.write_log("access.log", line("28/Sep/2026:16:00:00 -0300", "/radio"))
        v = self.load()[0]
        self.visits_dir.joinpath("2026-09-28.tsv").write_text(
            v.to_tsv() + "\n", encoding="utf-8")
        self.assertEqual(len(self.load()), 1)

    def test_snapshot_rescata_lo_que_ya_rotto(self):
        v = visits.parse_line(
            line("01/Sep/2026:16:00:00 -0300", "/radio"), MOUNTS)
        self.visits_dir.joinpath("2026-09-01.tsv").write_text(
            v.to_tsv() + "\n", encoding="utf-8")
        self.assertEqual(len(self.load()), 1)

    def test_no_colapsa_reconexiones_en_el_mismo_segundo(self):
        # Icecast no da id de conexion: dos GET identicos en el mismo segundo
        # son dos visitas reales (el panel abre varias a la vez cuando algo se
        # traba). Colapsarlas por claveeria menos visitas de las que hubo.
        self.write_log("access.log", line("28/Sep/2026:16:00:00 -0300",
                                          "/jacobs", nbytes=394, seconds=0) * 5)
        self.assertEqual(len(self.load()), 5)

    def test_ordenado_del_mas_reciente_al_mas_viejo(self):
        self.write_log("access.log", line("01/Sep/2026:16:00:00 -0300", "/radio")
                       + line("28/Sep/2026:16:00:00 -0300", "/radio"))
        self.assertEqual([v.when.day for v in self.load()], [28, 1])

    def test_log_ilegible_no_tumba_el_reporte(self):
        self.visits_dir.joinpath("2026-09-28.tsv").write_text(
            visits.parse_line(line("28/Sep/2026:16:00:00 -0300", "/radio"),
                              MOUNTS).to_tsv() + "\n", encoding="utf-8")
        self.log_dir.mkdir(exist_ok=True)
        (self.log_dir / "access.log.gz").write_bytes(b"\x1f\x8b roto")
        self.assertEqual(len(self.load()), 1)

    def test_directorio_inexistente(self):
        self.assertEqual(visits.load_visits(
            MOUNTS, self.root / "nope", self.root / "tampoco"), [])


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log_dir = self.root / "icecast"
        self.visits_dir = self.root / "visits"
        self.log_dir.mkdir()
        self.visits_dir.mkdir()
        self.day = visits.now_local().date() - timedelta(days=1)
        # Ojo el dos puntos tras el año: es el formato real de icecast
        # ([30/Sep/2026:13:16:03 -0300]) y el regex no perdona.
        self.stamp = "{}:10:00:00 -0300".format(self.day.strftime("%d/%b/%Y"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_escribe_un_archivo_por_dia(self):
        (self.log_dir / "access.log").write_text(
            line(self.stamp, "/radio") + line(self.stamp, "/jacobs"),
            encoding="utf-8")
        written, pruned = visits.snapshot(days=7, prune_days=0, mounts=MOUNTS,
                                          log_dir=self.log_dir,
                                          visits_dir=self.visits_dir)
        self.assertEqual(written, 2)
        self.assertEqual(pruned, 0)
        path = self.visits_dir / "{}.tsv".format(self.day.isoformat())
        self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_reescribir_no_duplica(self):
        (self.log_dir / "access.log").write_text(
            line(self.stamp, "/radio"), encoding="utf-8")
        for _ in range(3):
            visits.snapshot(days=7, prune_days=0, mounts=MOUNTS,
                            log_dir=self.log_dir, visits_dir=self.visits_dir)
        path = self.visits_dir / "{}.tsv".format(self.day.isoformat())
        self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_poda_los_archivos_viejos(self):
        old = self.visits_dir / "2000-01-01.tsv"
        old.write_text("x\n", encoding="utf-8")
        os.utime(old, (0, 0))
        (self.log_dir / "access.log").write_text(
            line(self.stamp, "/radio"), encoding="utf-8")
        written, pruned = visits.snapshot(days=7, prune_days=1, mounts=MOUNTS,
                                          log_dir=self.log_dir,
                                          visits_dir=self.visits_dir)
        self.assertEqual((written, pruned), (1, 1))
        self.assertFalse(old.exists())

    def test_no_deja_temporales(self):
        (self.log_dir / "access.log").write_text(
            line(self.stamp, "/radio"), encoding="utf-8")
        visits.snapshot(days=7, prune_days=0, mounts=MOUNTS,
                        log_dir=self.log_dir, visits_dir=self.visits_dir)
        self.assertEqual(list(self.visits_dir.glob("*.tmp")), [])

    def test_default_de_directorios(self):
        # Sin argumentos, snapshot() tiene que resolver ICECAST_LOG_DIR y
        # VISITS_DIR por su cuenta. Los defaults de una función se evalúan al
        # definirla, así que esto ya se rompió una vez.
        stamp = visits.now_local().strftime("%d/%b/%Y:10:00:00 -0300")
        (self.log_dir / "access.log").write_text(
            line(stamp, "/radio"), encoding="utf-8")
        original = (visits.ICECAST_LOG_DIR, visits.VISITS_DIR)
        try:
            visits.ICECAST_LOG_DIR = self.log_dir
            visits.VISITS_DIR = self.visits_dir
            written, _ = visits.snapshot(days=7, prune_days=0, mounts=MOUNTS)
        finally:
            visits.ICECAST_LOG_DIR, visits.VISITS_DIR = original
        self.assertEqual(written, 1)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.visits = [
            visits.parse_line(line("28/Sep/2026:16:00:00 -0300", "/radio",
                                   seconds=600), MOUNTS),
            visits.parse_line(line("28/Sep/2026:17:00:00 -0300", "/radio",
                                   seconds=300), MOUNTS),
            visits.parse_line(line("28/Sep/2026:18:00:00 -0300", "/jacobs",
                                   seconds=60, status=404, nbytes=394),
                              MOUNTS),
        ]

    def test_falla_404_queda_fuera_del_reporte(self):
        # 91% de las lineas reales: contarlas inflaria el reporte.
        ok, failed = visits.split_failed(self.visits)
        self.assertEqual(len(ok), 2)
        self.assertEqual(failed, 1)

    def test_daily_agrupa_por_dia_y_suma_minutos(self):
        ok, failed = visits.split_failed(self.visits)
        text = "\n".join(visits.daily_report(ok, MOUNTS, failed=failed))
        self.assertIn("2026-09-28", text)
        self.assertIn("15.0", text)          # (600+300)/60
        self.assertIn("2 conexiones", text)
        self.assertIn("intento(s) fallido(s)", text)

    def test_daily_sin_visitas_no_revienta(self):
        text = "\n".join(visits.daily_report([], MOUNTS, failed=7))
        self.assertIn("sin visitas registradas", text)
        self.assertIn("7 intento(s)", text)

    def test_detalle_ordena_y_limita(self):
        ok, failed = visits.split_failed(self.visits)
        text = "\n".join(visits.visits_report(ok, MOUNTS, limit=1, failed=failed))
        self.assertIn("2026-09-28 17:00:00", text)
        self.assertIn("mostrando 1 de 2", text)

    def test_detalle_muestra_las_mas_recientes(self):
        # --limit corta por arriba, así que el orden no puede depender de cómo
        # le llegue la lista.
        text = "\n".join(visits.visits_report(list(reversed(self.visits)),
                                              MOUNTS, limit=1))
        self.assertIn("2026-09-28 18:00:00", text)

    def test_tabla_sin_espacios_al_final(self):
        for line_text in visits.daily_report(self.visits, MOUNTS):
            self.assertEqual(line_text, line_text.rstrip(), line_text)

    def test_fmt_dur(self):
        self.assertEqual(visits.fmt_dur(9), "9s")
        self.assertEqual(visits.fmt_dur(90), "1m")
        self.assertEqual(visits.fmt_dur(3600), "1h00")
        self.assertEqual(visits.fmt_dur(1514), "25m")

    def test_json_daily(self):
        ok, failed = visits.split_failed(self.visits)
        data = visits.daily_json(ok, MOUNTS, failed=failed)
        self.assertEqual(data["intentos_fallidos"], 1)
        self.assertEqual(data["dias"][0]["visitas"], 2)
        self.assertEqual(data["dias"][0]["segundos"], 900)
        self.assertEqual(data["dias"][0]["mas_larga_s"], 600)
        json.dumps(data)  # tiene que ser serializable


class CliTests(unittest.TestCase):
    """El modo --json tiene que emitir JSON puro: un banner antes lo rompe."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log_dir = self.root / "icecast"
        self.visits_dir = self.root / "visits"
        self.stations = self.root / "stations.json"
        self.log_dir.mkdir()
        (self.log_dir / "access.log").write_text(
            line("28/Sep/2026:16:00:00 -0300", "/radio", seconds=600)
            + line("28/Sep/2026:18:00:00 -0300", "/jacobs", status=404,
                   nbytes=394),
            encoding="utf-8")
        self.stations.write_text(json.dumps({
            "default": "algoritmica",
            "stations": [
                {"id": "jacobs", "label": "jacobs collection",
                 "root": "radio-jacobs", "mount": "/jacobs"},
                {"id": "algoritmica", "label": "jamendo radio",
                 "root": ".", "mount": "/radio"},
            ],
        }), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def patch(self):
        visits.ICECAST_LOG_DIR = self.log_dir
        visits.VISITS_DIR = self.visits_dir
        visits.STATIONS_PATH = self.stations

    def run_main(self, *args):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = visits.main(list(args))
        self.assertEqual(code, 0)
        return buf.getvalue()

    def test_json_no_le_llega_el_banner(self):
        self.patch()
        out = self.run_main("--json", "--days", "0")
        data = json.loads(out)
        self.assertEqual(data["monts"], MOUNTS)
        self.assertEqual(data["intentos_fallidos"], 1)

    def test_json_visitas_respeta_limit(self):
        self.patch()
        out = self.run_main("--visits", "--json", "--days", "0", "--limit", "1")
        self.assertEqual(len(json.loads(out)["visitas"]), 1)

    def test_mount_filtra(self):
        self.patch()
        # Quedan solo las de /jacobs, que en el fixture es un 404.
        out = self.run_main("--json", "--days", "0", "--mount", "/jacobs")
        data = json.loads(out)
        self.assertEqual(data["intentos_fallidos"], 1)
        self.assertEqual(data["dias"], [])

    def test_errors_incluye_los_404(self):
        self.patch()
        out = self.run_main("--visits", "--json", "--days", "0", "--errors")
        data = json.loads(out)
        self.assertEqual(data["intentos_fallidos"], 0)
        self.assertEqual(len(data["visitas"]), 2)
        self.assertEqual(
            {v["status"] for v in data["visitas"]}, {200, 404})

    def test_reporte_texto(self):
        self.patch()
        out = self.run_main("--days", "0")
        self.assertIn("2026-09-28", out)
        self.assertIn("jamendo radio", out)


if __name__ == "__main__":
    unittest.main()
