import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gestor.py"
spec = importlib.util.spec_from_file_location("gestor_under_test", MODULE_PATH)
gestor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gestor)

WEB_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "web_server.py"
web_spec = importlib.util.spec_from_file_location("web_server_under_test", WEB_MODULE_PATH)
web_server = importlib.util.module_from_spec(web_spec)
web_spec.loader.exec_module(web_server)


class GestorQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        gestor.ROOT = self.root
        gestor.SONGS_PATH = self.root / "songs.json"
        gestor.QUEUE_PATH = self.root / "queue.m3u"
        gestor.OFFLINE_QUEUE_PATH = self.root / "queue.offline.m3u"
        gestor.CLIMA_PATH = self.root / "clima.json"
        gestor.SEEN_PATH = self.root / "data" / "jamendo_seen.json"
        gestor.STATE_PATH = self.root / "data" / "playback.json"
        gestor.PLAYED_PATH = self.root / "logs" / "played.txt"
        gestor.PLAYED_TS_PATH = self.root / "logs" / "played.tsv"
        gestor.NOWPLAYING = self.root / "logs" / "nowplaying.txt"
        gestor.MUSIC_DIR = self.root / "music"
        gestor.ARCHIVE_DIR = self.root / "archive" / "music"
        gestor.LOG_PATH = self.root / "logs" / "gestor.log"
        gestor.CLIENT_FILE = self.root / "scripts" / ".jamendo_client"
        web_server.PROJECT_ROOT = self.root
        gestor.MUSIC_DIR.mkdir(parents=True)
        gestor.ARCHIVE_DIR.mkdir(parents=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_song(self, relative, heard=False, title="Track", artist="Artist"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        return {
            "id": f"jm-{path.stem.rsplit(' - ', 1)[-1]}",
            "jamendo_id": path.stem.rsplit(" - ", 1)[-1],
            "file": str(path.relative_to(self.root)),
            "title": title,
            "artist": artist,
            "album": path.parent.name,
            "heard": heard,
            "archived": relative.startswith("archive/"),
        }

    def read_queue(self, path):
        return gestor.queue_paths(path)

    def test_online_queue_contains_only_unplayed_and_not_current(self):
        heard = self.make_song("music/A/a/Heard - 1.mp3", heard=True)
        current = self.make_song("music/B/b/Current - 2.mp3")
        pending = self.make_song("music/C/c/Pending - 3.mp3")
        songs = [heard, current, pending]

        count, changed = gestor.write_queue(
            songs,
            {},
            {gestor.path_identity(gestor.song_file(current))},
        )

        self.assertEqual(count, 1)
        self.assertTrue(changed)
        self.assertEqual(
            self.read_queue(gestor.QUEUE_PATH),
            [gestor.path_identity(gestor.song_file(pending))],
        )

    def test_offline_queue_is_oldest_first(self):
        older = self.make_song("archive/music/O/Older - 10.mp3", heard=True, title="Older")
        newer = self.make_song("archive/music/N/Newer - 11.mp3", heard=True, title="Newer")
        state = gestor.state_default()
        state["tracks"] = {
            gestor.track_key(newer): {"last_played": "2026-09-20T00:00:00+00:00"},
            gestor.track_key(older): {"last_played": "2026-09-01T00:00:00+00:00"},
        }

        count, changed = gestor.write_offline_queue([newer, older], state)

        self.assertEqual(count, 2)
        self.assertTrue(changed)
        self.assertEqual(
            self.read_queue(gestor.OFFLINE_QUEUE_PATH),
            [
                gestor.path_identity(gestor.song_file(older)),
                gestor.path_identity(gestor.song_file(newer)),
            ],
        )

    def test_mark_started_updates_song_and_state(self):
        song = self.make_song("music/A/a/Started - 20.mp3")
        gestor.save_songs([song])
        gestor.save_state(gestor.state_default())

        result = gestor.mark_started(str(gestor.song_file(song)))

        self.assertEqual(result, 0)
        songs = gestor.load_songs()
        state = gestor.load_state()
        self.assertTrue(songs[0]["heard"])
        self.assertEqual(state["tracks"][gestor.track_key(song)]["play_count"], 1)
        self.assertTrue(gestor.PLAYED_TS_PATH.exists())

    def test_archive_song_keeps_media_for_fallback(self):
        song = self.make_song("music/A/a/Archived - 30.mp3", heard=True)
        source = gestor.song_file(song)
        state = gestor.state_default()
        state["tracks"][gestor.track_key(song)] = {
            "last_played": "2026-09-01T00:00:00+00:00",
            "play_count": 1,
        }

        result, count = gestor.archive_songs([song], state, set())

        self.assertEqual(count, 1)
        self.assertEqual(result[0]["archived"], True)
        self.assertFalse(source.exists())
        archived_path = self.root / "archive" / "music" / "A" / "a" / "Archived - 30.mp3"
        self.assertTrue(archived_path.exists())
        self.assertEqual(gestor.path_string(archived_path), result[0]["file"])

    def test_web_cache_finds_track_after_archive_move(self):
        song = self.make_song("archive/music/A/a/Moved - 41.mp3", heard=True)
        songs_path = self.root / "songs.json"
        songs_path.write_text(
            json.dumps([song]),
            encoding="utf-8",
        )

        # El cache resuelve los paths de songs.json contra el root de la
        # estación, no contra el PROJECT_ROOT del proceso: así el mismo panel
        # sirve radio/ y radio-jacobs/ con un solo web_server.
        cache = web_server.SongCache(songs_path, self.root)
        found = cache.get_by_fname("music/A/a/Moved - 41.mp3")

        self.assertEqual(found["title"], song["title"])
        self.assertEqual(found["jamendo_id"], song["jamendo_id"])

    def test_legacy_history_preserves_last_played_timestamp(self):
        song = self.make_song("music/A/a/Legacy - 40.mp3")
        song["last_played_at"] = "2020-01-02T03:04:05+00:00"
        gestor.save_songs([song])
        gestor.PLAYED_PATH.parent.mkdir(parents=True, exist_ok=True)
        gestor.PLAYED_PATH.write_text(
            f"{song['file']}|Artist — Track\n",
            encoding="utf-8",
        )
        state = gestor.state_default()

        marked = gestor.consume_played([song], state)

        self.assertEqual(marked, 1)
        record = state["tracks"][gestor.track_key(song)]
        self.assertEqual(record["last_played"], "2020-01-02T03:04:05+00:00")
        self.assertTrue(song["heard"])

    def test_stale_nowplaying_is_not_treated_as_current(self):
        song = self.make_song("music/A/a/Stale - 42.mp3")
        song["last_played_at"] = "2020-01-02T03:04:05+00:00"
        gestor.save_songs([song])
        gestor.NOWPLAYING.parent.mkdir(parents=True, exist_ok=True)
        gestor.NOWPLAYING.write_text(
            f"{gestor.path_string(gestor.song_file(song))}|Artist — Track",
            encoding="utf-8",
        )
        stale = time.time() - gestor.NOWPLAYING_MAX_AGE - 60
        os.utime(gestor.NOWPLAYING, (stale, stale))
        state = gestor.state_default()

        gestor.consume_played([song], state)

        self.assertEqual(
            state["tracks"][gestor.track_key(song)]["last_played"],
            "2020-01-02T03:04:05+00:00",
        )

    def test_offline_queue_excludes_protected_online_tracks(self):
        buffered = self.make_song("music/B/b/Buffered - 70.mp3", heard=True)
        archived = self.make_song("archive/music/A/a/Archived - 71.mp3", heard=True)
        state = gestor.state_default()
        state["tracks"] = {
            gestor.track_key(buffered): {"last_played": "2026-09-24T00:00:00+00:00"},
            gestor.track_key(archived): {"last_played": "2026-09-01T00:00:00+00:00"},
        }

        count, changed = gestor.write_offline_queue(
            [buffered, archived], state, {gestor.path_identity(gestor.song_file(buffered))}
        )

        self.assertEqual(count, 1)
        self.assertTrue(changed)
        self.assertEqual(
            self.read_queue(gestor.OFFLINE_QUEUE_PATH),
            [gestor.path_identity(gestor.song_file(archived))],
        )

    def test_playlist_update_preserves_inode_and_ignores_padding(self):
        path = self.root / "queue.m3u"
        path.write_text("#EXTM3U\n# old padding\n", encoding="utf-8")
        inode = os.stat(path).st_ino

        gestor.write_playlist(path, "#EXTM3U\n/new.mp3\n")

        self.assertEqual(os.stat(path).st_ino, inode)
        self.assertEqual(gestor.queue_paths(path), [gestor.path_identity("/new.mp3")])
        self.assertFalse(gestor.playlist_needs_update(path, "#EXTM3U\n/new.mp3\n"))

    def test_fetch_batch_falls_back_to_another_order(self):
        track = {
            "id": "2",
            "name": "Fresh",
            "artist_name": "Artist",
            "album_name": "Album",
            "license_ccurl": "https://creativecommons.org/licenses/by/4.0/",
            "audiodownload_allowed": True,
            "audiodownload": "https://example.invalid/2",
            "shareurl": "https://example.invalid/track/2",
            "musicinfo": {"tags": {"genres": ["folk"]}},
        }
        responses = {
            "relevance": {"ok": True, "results": [], "headers": {}},
            "releasedate_desc": {"ok": True, "results": [track], "headers": {}},
        }
        orders = []

        def fake_api(_client_id, params):
            orders.append(params["order"])
            return responses[params["order"]]

        def fake_download(_client_id, item):
            relative = Path("music") / "Artist" / "Album" / f"Fresh - {item['id']}.mp3"
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")
            return relative

        with mock.patch.object(gestor, "api_get", fake_api), mock.patch.object(gestor, "download_mp3", fake_download):
            # fetch_batch recibe (client_id, target_climate, seen, state, n,
            # max_dist): el state es el que guarda el offset por consulta, así
            # va explícito para que el fallback de orden quede aislado.
            added, reason = gestor.fetch_batch(
                "client", {}, {"1"}, gestor.state_default(), 1, 0.55
            )

        self.assertEqual(reason, "ok")
        # _sweep no corta con la primera respuesta vacía: la API devuelve 200
        # con cuerpo vacío cuando limita el ritmo, así que reintenta el mismo
        # orden una vez y recién después da la ventana por agotada.
        self.assertEqual([entry["jamendo_id"] for entry in added], ["2"])
        self.assertEqual(orders, ["relevance", "relevance", "releasedate_desc"])

    def test_main_without_network_keeps_online_and_offline_separate(self):
        pending = self.make_song("music/P/p/Pending - 50.mp3")
        also_pending = self.make_song("music/Q/q/Also pending - 51.mp3")
        heard = self.make_song("music/H/h/Heard - 52.mp3", heard=True)
        heard["last_played_at"] = "2026-09-01T00:00:00+00:00"
        gestor.save_songs([pending, also_pending, heard])
        state = gestor.state_default()
        state["tracks"][gestor.track_key(heard)] = {
            "last_played": heard["last_played_at"],
            "play_count": 1,
        }
        gestor.save_state(state)

        with mock.patch.object(sys, "argv", ["gestor.py", "--low-watermark", "6"]):
            result = gestor.main()

        self.assertEqual(result, 0)
        self.assertEqual(gestor.load_state()["mode"], "online")
        self.assertEqual(
            set(gestor.queue_paths(gestor.QUEUE_PATH)),
            {
                gestor.path_identity(gestor.song_file(pending)),
                gestor.path_identity(gestor.song_file(also_pending)),
            },
        )
        self.assertEqual(
            gestor.queue_paths(gestor.OFFLINE_QUEUE_PATH),
            [gestor.path_identity(self.root / "archive" / "music" / "H" / "h" / "Heard - 52.mp3")],
        )

    def test_main_falls_back_when_network_and_new_tracks_are_unavailable(self):
        heard = self.make_song("music/H/h/Heard - 60.mp3", heard=True)
        heard["last_played_at"] = "2026-09-01T00:00:00+00:00"
        gestor.save_songs([heard])
        state = gestor.state_default()
        state["tracks"][gestor.track_key(heard)] = {
            "last_played": heard["last_played_at"],
            "play_count": 1,
        }
        gestor.save_state(state)

        with mock.patch.object(sys, "argv", ["gestor.py", "--low-watermark", "6"]):
            result = gestor.main()

        self.assertEqual(result, 0)
        self.assertEqual(gestor.load_state()["mode"], "offline")
        self.assertEqual(gestor.queue_paths(gestor.QUEUE_PATH), [])
        self.assertEqual(
            gestor.queue_paths(gestor.OFFLINE_QUEUE_PATH),
            [gestor.path_identity(self.root / "archive" / "music" / "H" / "h" / "Heard - 60.mp3")],
        )

    def test_online_offline_online_cycle_never_repeats_played_track(self):
        pending = self.make_song("music/P/p/Cycle - 80.mp3")
        gestor.save_songs([pending])
        gestor.save_state(gestor.state_default())
        low_watermark = ["--low-watermark", "6"]

        with mock.patch.object(sys, "argv", ["gestor.py", *low_watermark]), \
                mock.patch.object(gestor, "get_client_id", return_value=""):
            self.assertEqual(gestor.main(), 0)
        self.assertEqual(gestor.load_state()["mode"], "online")
        self.assertEqual(
            gestor.queue_paths(gestor.QUEUE_PATH),
            [gestor.path_identity(gestor.song_file(pending))],
        )

        gestor.mark_started(str(gestor.song_file(pending)))

        with mock.patch.object(sys, "argv", ["gestor.py", *low_watermark]), \
                mock.patch.object(gestor, "get_client_id", return_value=""):
            self.assertEqual(gestor.main(), 0)
        self.assertEqual(gestor.load_state()["mode"], "offline")
        self.assertEqual(gestor.queue_paths(gestor.QUEUE_PATH), [])
        self.assertEqual(gestor.queue_paths(gestor.OFFLINE_QUEUE_PATH), [])

        fresh = {
            "id": "99",
            "name": "Fresh",
            "artist_name": "Artist",
            "album_name": "Album",
            "license_ccurl": "https://creativecommons.org/licenses/by/4.0/",
            "audiodownload_allowed": True,
            "audiodownload": "https://example.invalid/99",
            "shareurl": "https://example.invalid/track/99",
            "musicinfo": {"tags": {"genres": ["folk"]}},
        }

        def fake_api(_client_id, params):
            return {"ok": True, "results": [fresh], "headers": {}}

        def fake_download(_client_id, item):
            relative = Path("music") / "Artist" / "Album" / f"Fresh - {item['id']}.mp3"
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")
            return relative

        with mock.patch.object(sys, "argv", ["gestor.py", *low_watermark]), \
                mock.patch.object(gestor, "get_client_id", return_value="cid"), \
                mock.patch.object(gestor, "api_get", fake_api), \
                mock.patch.object(gestor, "download_mp3", fake_download):
            self.assertEqual(gestor.main(), 0)

        self.assertEqual(gestor.load_state()["mode"], "online")
        self.assertEqual(
            gestor.queue_paths(gestor.QUEUE_PATH),
            [gestor.path_identity(self.root / "music" / "Artist" / "Album" / "Fresh - 99.mp3")],
        )
        self.assertEqual(
            gestor.queue_paths(gestor.OFFLINE_QUEUE_PATH),
            [gestor.path_identity(self.root / "archive" / "music" / "P" / "p" / "Cycle - 80.mp3")],
        )
        self.assertEqual(gestor.load_seen(), {"99"})


IA_MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "radio-jacobs" / "scripts" / "ia_gestor.py"
)
ia_spec = importlib.util.spec_from_file_location("ia_gestor_under_test", IA_MODULE_PATH)
ia_gestor = importlib.util.module_from_spec(ia_spec)
ia_spec.loader.exec_module(ia_gestor)


class JacobsParserTests(unittest.TestCase):
    """Parsing de la Aadam Jacobs Collection.

    Los casos caternos son de shows reales de la colección, no inventados: los
    ítems viejos (ajcNNNNN_*) traen description en texto plano y metadata.venue
    / metadata.taper, y los nuevos (NNNN_bandaAAAA-MM-DD) traen description en
    HTML <div>, sin esos campos, y setlist numerado. Los dos formatos ya
    rompieron el parser en producción antes de esto.
    """

    def test_description_lines_splits_html_divs(self):
        html = "<div>Freakons</div><div>2013</div><br /><div>01 One</div>"
        self.assertEqual(ia_gestor.description_lines(html), ["Freakons", "2013", "01 One"])

    def test_setlist_reads_html_numbered_lines(self):
        html = "<div>01 Tim Tuten intro</div><div>02 Chestnut Blight</div>"
        self.assertEqual(
            ia_gestor.parse_setlist(html),
            {1: "Tim Tuten intro", 2: "Chestnut Blight"},
        )

    def test_setlist_reads_plain_text_and_skips_bracket_lines(self):
        text = "01 Heroin\n[stage banter]\n3. Sally Boy Candy Bar"
        self.assertEqual(
            ia_gestor.parse_setlist(text),
            {1: "Heroin", 3: "Sally Boy Candy Bar"},
        )

    def test_setlist_ignores_explicit_no_setlist(self):
        self.assertEqual(ia_gestor.parse_setlist("Command Module\n\nno setlist"), {})

    def test_parse_seconds_handles_both_archive_formats(self):
        # MP3 derivado viene "mm:ss"; el FLAC maestro en segundos con decimales.
        self.assertEqual(ia_gestor.parse_seconds("03:20"), 200.0)
        self.assertEqual(ia_gestor.parse_seconds("199.92"), 199.92)
        self.assertIsNone(ia_gestor.parse_seconds(""))
        self.assertIsNone(ia_gestor.parse_seconds(None))

    def test_venue_from_album_handles_both_title_formats(self):
        # Losshows viejos cierran con "on <fecha>" y los nuevos no. El lugar
        # tiene que salir igual en los dos, porque metadata.venue solo existe
        # en los viejos.
        self.assertEqual(
            ia_gestor.venue_from_album("Run On Live at Empty Bottle on 1996-08-10"),
            "Empty Bottle",
        )
        self.assertEqual(
            ia_gestor.venue_from_album("Azita Live at The Hideout 2015-07-24"),
            "The Hideout",
        )

    def test_venue_from_album_survives_real_typos(self):
        # La colección tiene "LIve at" con I mayúscula, doble espacio en
        # "Overture  Center" y venues largos o con apóstrofo. Un regex sensible
        # a mayúsculas dejaba el 90% del catálogo sin lugar.
        self.assertEqual(
            ia_gestor.venue_from_album("Hushdrops LIve at The Hideout 2015-04-11"),
            "The Hideout",
        )
        self.assertEqual(
            ia_gestor.venue_from_album(
                "Belle & Sebastian Live at Overture  Center for the Arts on 2015-04-04"
            ),
            "Overture  Center for the Arts",
        )
        self.assertEqual(
            ia_gestor.venue_from_album(
                "Cheer-Accident with Lovely Little Girls Live at Martyrs' 2015-06-19"
            ),
            "Martyrs'",
        )
        self.assertEqual(
            ia_gestor.venue_from_album(
                "Friends of the Gamelan Live at Jay Pritzker Pavilion - Millennium Park 2013-07-11"
            ),
            "Jay Pritzker Pavilion - Millennium Park",
        )

    def test_venue_from_album_is_empty_without_the_pattern(self):
        for album in ("", "show", "Freakons Live at The Hideout", None):
            self.assertEqual(ia_gestor.venue_from_album(album), "")

    def test_tracks_of_falls_back_to_album_for_venue(self):
        """Sin metadata.venue, el lugar sale del título del show."""
        doc = {
            "metadata": {
                "title": "Run On Live at Empty Bottle on 1996-08-10",
                "creator": "Run On",
                "date": "1996-08-10",
                "description": "01 glad",
            },
            "files": [
                {
                    "name": "01 glad.mp3",
                    "source": "derivative",
                    "length": "1:23",
                    "track": "1",
                    "title": "glad",
                }
            ],
        }
        track = ia_gestor.tracks_of(doc, "ajc00026_runon_1996-08-10")[0]
        self.assertEqual(track["venue"], "Empty Bottle")

    def test_tracks_of_prefers_metadata_venue_over_album(self):
        """Si el ítem viejo trae el campo, manda él y no el título."""
        doc = {
            "metadata": {
                "title": "Run On Live at Empty Bottle on 1996-08-10",
                "creator": "Run On",
                "date": "1996-08-10",
                "venue": "Empty Bottle (backstage)",
                "description": "01 glad",
            },
            "files": [
                {
                    "name": "01 glad.mp3",
                    "source": "derivative",
                    "length": "1:23",
                    "track": "1",
                    "title": "glad",
                }
            ],
        }
        track = ia_gestor.tracks_of(doc, "ajc00026_runon_1996-08-10")[0]
        self.assertEqual(track["venue"], "Empty Bottle (backstage)")

    def test_license_of_etree_item_is_permission(self):
        meta = {"collection": ["aadamjacobs", "etree"]}
        self.assertEqual(ia_gestor.license_of(meta), "permission")

    def test_license_of_creative_commons_item(self):
        # Sin pasar por las colecciones de permiso: si el item dice pertenecer a
        # aadamjacobs/etree, license_of devuelve "permission" aunque traiga
        # licenseurl. La membresía explícita de la colección manda.
        meta = {
            "collection": ["audio"],
            "licenseurl": "http://creativecommons.org/licenses/by-nc-sa/4.0/",
        }
        self.assertEqual(ia_gestor.license_of(meta), "cc-by-nc")

    def test_no_derivatives_licenses_are_not_emitted(self):
        # ND y "zero" están mapeados a None a propósito: no se emite material
        # que prohíban derivada ni que sea de dominio público.
        for url in (
            "http://creativecommons.org/licenses/by-nc-nd/4.0/",
            "http://creativecommons.org/licenses/by-nd/4.0/",
        ):
            self.assertIsNone(ia_gestor.license_short(url), url)

    def test_tracks_of_reads_files_sibling_not_metadata(self):
        """files es hermano de metadata en /metadata/<id>, no un subcampo."""
        doc = {
            "metadata": {
                "title": "Freakons Live at The Hideout",
                "creator": "Freakons",
                "date": "2013-09-22",
                "description": "<div>01 One</div><div>02 Two</div>",
            },
            "files": [
                {
                    "name": "01 One.mp3",
                    "format": "VBR MP3",
                    "source": "derivative",
                    "length": "03:20",
                    "track": "1",
                    "title": "untitled",
                },
                {
                    "name": "01 One.flac",
                    "format": "Flac",
                    "source": "original",
                    "length": "199.92",
                    "track": "1",
                },
                {
                    "name": "02 Two.mp3",
                    "format": "VBR MP3",
                    "source": "derivative",
                    "length": "12:00",
                    "track": "2",
                },
            ],
        }
        tracks = ia_gestor.tracks_of(doc, "0120_freakons2013-09-22")

        # Solo MP3 derivados, y se descarta el de 12 min (MAX_TRACK_SECONDS).
        self.assertEqual([t["track"] for t in tracks], [1])
        first = tracks[0]
        self.assertEqual(first["title"], "One")
        self.assertEqual(first["duration_seconds"], 200)
        self.assertEqual(first["archive_id"], "0120_freakons2013-09-22t01")
        self.assertTrue(first["file_url"].endswith("01%20One.mp3"))

    def _talk_doc(self, titles):
        """Arma un /metadata con una pista derivative por cada título dado."""
        return {
            "metadata": {"title": "Show", "creator": "Someone", "date": "2013-01-01"},
            "files": [
                {"name": f"{i:02d} {t}.mp3", "source": "derivative",
                 "length": "03:00", "track": str(i), "title": t}
                for i, t in enumerate(titles, start=1)
            ],
        }

    def _kept_titles(self, titles):
        doc = self._talk_doc(titles)
        out = ia_gestor.tracks_of(doc, "9999_show")
        return [t["title"] for t in out]

    def test_tracks_of_drops_talk_tracks(self):
        self.assertEqual(self._kept_titles([
            "Another Sunny Day", "chat", "Tainted Love", "Tuning", "Your Turn",
        ]), ["Another Sunny Day", "Tainted Love", "Your Turn"])

    def test_tracks_of_drops_bare_intro_but_keeps_named_one(self):
        # En los sets de WFMU la pista 1 es la presentación del pinchador, y se
        # titula solo "intro". Pero hay instrumentales de verdad que se llaman
        # "Solar Winds Intro", y esos hay que conservarlos.
        self.assertEqual(self._kept_titles([
            "intro", "Tainted Love", "MC intro", "Solar Winds intro",
        ]), ["Tainted Love", "Solar Winds intro"])

    def test_tracks_of_keeps_intro_with_extra_words(self):
        # "Part One - Introduction - The Adoration of the Earth" es una pieza con
        # nombre, no una presentación: el patrón solo cae el intro que no dice
        # nada más.
        self.assertEqual(self._kept_titles([
            "Part One - Introduction - The Adoration of the Earth [2:53]",
            "Part Two",
        ]), ["Part One - Introduction - The Adoration of the Earth [2:53]",
            "Part Two"])

    def test_track_key_accepts_archive_ids(self):
        """El id de archive.org es alfanumérico, no numérico como el de Jamendo."""
        key = ia_gestor.track_key("music/A/a/One - 0120_freakons2013-09-22t01.mp3")
        self.assertEqual(key, "archive:0120_freakons2013-09-22t01")
        # El patrón es codicioso a propósito: el archivo es "{título} - {id}",
        # y los títulos pueden contener " - " (Sun Ra - Space Is the Place).
        # Partir por el ÚLTIMO separador es lo que recupera el id bien.
        self.assertEqual(
            ia_gestor.track_key("music/A/a/Sun Ra - Space Is the Place - 0120_x_t01.mp3"),
            "archive:0120_x_t01",
        )

    def test_web_cache_identity_matches_archive_ids(self):
        self.assertEqual(
            web_server.track_identity({"file": "music/A/a/One - 0120_x2013-09-22t01.mp3"}),
            "0120_x2013-09-22t01",
        )
        # El caso de Jamendo sigue funcionando.
        self.assertEqual(web_server.track_identity({"file": "music/A/a/One - 41.mp3"}), "41")

    def test_stations_config_resolves_both_roots(self):
        stations, default_id = web_server.load_stations()
        self.assertEqual(default_id, "jacobs")
        by_id = {st["id"]: st for st in stations}
        self.assertEqual(sorted(by_id), ["algoritmica", "jacobs"])
        for st in stations:
            self.assertTrue(Path(st["root"]).is_dir(), st["root"])
            self.assertTrue((Path(st["root"]) / "songs.json").exists(), st["id"])
        self.assertEqual(by_id["algoritmica"]["mount"], "/radio")
        self.assertEqual(by_id["jacobs"]["mount"], "/jacobs")


def _sweep_stub(results):
    """Devuelve un _sweep falso que responde según la lista de resultados.

    Cada elemento es (cursor, reason, n_canciones) y se consume en orden; si
    se llama más veces que resultados hay, repite el último. Registra los
    cursores con los que fue llamado en `calls`, que es lo que se asserta.
    """
    calls = []

    def stub(target_climate, seen, existing_ids, added, per_artist,
             n, max_dist, page_size, cursor):
        index = len(calls)
        calls.append(cursor)
        new_cursor, reason, n_songs = results[min(index, len(results) - 1)]
        for i in range(n_songs):
            added.append({"archive_id": f"stub-{index}-{i}"})
        return new_cursor, reason

    return stub, calls


class JacobsCursorWrapTests(unittest.TestCase):
    """Reinicio del cursor al terminar la colección.

    El dedup de fetch_batch es por canción, así que volver al principio relee
    shows y les saca las pistas que faltaban sin repetir nada. El riesgo real es
    el contrario: una colección ya consumida haciendo pedidos inútiles a
    archive.org en cada ciclo. Estos tests fijan las dos mitades.
    """

    def _run(self, state, results, n=10):
        stub, calls = _sweep_stub(results)
        with mock.patch.object(ia_gestor, "_sweep", stub), \
             mock.patch.object(ia_gestor, "load_songs", return_value=[]), \
             mock.patch.object(ia_gestor, "log", return_value=None):
            added, reason = ia_gestor.fetch_batch({}, set(), state, n, 0.55)
        return added, reason, calls

    def test_reinicia_el_cursor_al_llegar_al_final(self):
        state = {"source": {"identifier": "9999_z", "pasada": 0,
                            "canciones_pasada": 3}}
        added, reason, calls = self._run(
            state, [("9999_z", "empty", 0), ("0100_a", "ok", 4)], n=4)
        self.assertEqual(len(calls), 2, "debe barrenar una vez y reintentar una")
        self.assertEqual(calls[1], ia_gestor.IA_CURSOR_START)
        self.assertEqual(state["source"]["pasada"], 1)
        self.assertEqual(state["source"]["identifier"], "0100_a")
        self.assertEqual(len(added), 4)
        self.assertEqual(reason, "ok")

    def test_no_reinicia_si_la_pasada_no_saco_nada(self):
        # Pasada > 0 y 0 canciones acumuladas = colección agotada. Un reinicio
        # aquí vuelven los 3.430 shows de requests al pedo, en cada ciclo.
        state = {"source": {"identifier": "9999_z", "pasada": 1,
                            "canciones_pasada": 0}}
        added, reason, calls = self._run(state, [("9999_z", "empty", 0)])
        self.assertEqual(len(calls), 1, "no debe reintentar si la pasada fue estéril")
        self.assertEqual(state["source"]["pasada"], 1)
        self.assertEqual(reason, "exhausted")
        self.assertEqual(added, [])

    def test_acumula_canciones_de_la_pasada_entre_ciclos(self):
        # El total vive en el estado, así que tiene que sobrevivir entre ciclos
        # y solo se resetea cuando el cursor da la vuelta.
        state = {"source": {"identifier": "0100_a", "pasada": 1,
                            "canciones_pasada": 5}}
        added, reason, calls = self._run(state, [("0101_b", "ok", 3)], n=3)
        self.assertEqual(len(calls), 1, "reason ok no dispara reinicio")
        self.assertEqual(state["source"]["canciones_pasada"], 8)
        self.assertEqual(reason, "ok")

    def test_tope_de_vueltas_por_ciclo(self):
        # Aunque la vuelta nueva también termine en "empty", el bucle para.
        state = {"source": {"identifier": "9999_z", "pasada": 0,
                            "canciones_pasada": 7}}
        added, reason, calls = self._run(state, [("9999_z", "empty", 0)])
        self.assertEqual(len(calls), ia_gestor.MAX_WRAPS + 1)
        self.assertEqual(reason, "exhausted")

    def test_error_de_red_no_reinicia_el_cursor(self):
        # Un 500 de archive.org no es "se terminó la colección": no se toca el
        # cursor ni el contador de pasadas, se reintenta el ciclo que viene.
        state = {"source": {"identifier": "0100_a", "pasada": 0,
                            "canciones_pasada": 0}}
        added, reason, calls = self._run(state, [("0100_a", "network_error", 0)])
        self.assertEqual(len(calls), 1)
        self.assertEqual(reason, "network_error")
        self.assertEqual(state["source"]["identifier"], "0100_a")
        self.assertEqual(state["source"]["pasada"], 0)

    def test_estado_vacio_usa_el_inicio_del_cursor(self):
        state = {"source": {}}
        added, reason, calls = self._run(state, [("0100_a", "ok", 2)], n=2)
        self.assertEqual(calls[0], ia_gestor.IA_CURSOR_START)
        self.assertEqual(reason, "ok")


if __name__ == "__main__":
    unittest.main()
