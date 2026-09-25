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

        cache = web_server.SongCache(songs_path)
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
            added, reason = gestor.fetch_batch("client", {}, {"1"}, 1, 0.55)

        self.assertEqual(reason, "ok")
        self.assertEqual([entry["jamendo_id"] for entry in added], ["2"])
        self.assertEqual(orders, ["relevance", "releasedate_desc"])

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


if __name__ == "__main__":
    unittest.main()
