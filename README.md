# music-radio

Streaming radio with two stations sharing a single Icecast server and web
panel:

| Station                     | Source                                                            | Mount     |
| --------------------------- | ----------------------------------------------------------------- | --------- |
| **Jamendo Radio**           | Jamendo API — Creative Commons tracks filtered by musical climate | `/radio`  |
| **Aadam Jacobs Collection** | Internet Archive `aadamjacobs` — live recordings (1985–2023)      | `/jacobs` |

---

## Stack

- **Liquidsoap** — audio playback and stream output (one process per station)
- **Icecast2** — HTTP audio streaming (shared)
- **Python 3** (stdlib only) — download, climate filtering, queue, web panel
- **Jamendo API** — Creative Commons music source
- **Internet Archive** — live concert archive (`aadamjacobs` collection)

---

## Quick start

```bash
# First time only — install venv, liquidsoap, icecast2, copy .env
cp scripts/.env.example .env
cp scripts/.env.example radio-jacobs/.env
# Edit both .env files with your ICE_PASSWORD

# Start / stop / restart both stations at once
./reset.sh            # full reset (kills everything, restarts clean)
./reset.sh start      # start only
./reset.sh stop       # stop only
./reset.sh status     # show processes, streams, listeners, now-playing
```

**Listen:**

- Jacobs Collection → `http://your-server:8000/jacobs`
- Jamendo Radio → `http://your-server:8000/radio`

**Web panel:** `http://your-server:8080`

---

## How it works — Jamendo Radio

1. `gestor.py` reads `clima.json` and downloads a batch from Jamendo filtered
   by `climate_distance()` (the ML hook — see below).
2. Each `genero` in `clima.json` is sent to Jamendo as `fuzzytags`, so the
   style list both **narrows the search** and **scores the results**. Every tag
   is its own result window, which multiplies how much of Jamendo is reachable.
3. Only unplayed tracks are written to `queue.m3u`, ordered by climate proximity.
4. Liquidsoap plays the online queue; each track is marked as started immediately
   and state is persisted in `data/playback.json`.
5. When the queue is low, `gestor.py` downloads another batch. Played MP3s are
   moved to `archive/music/` and become the offline fallback pool.
6. If the online source is empty, Liquidsoap falls back to `queue.offline.m3u`
   ordered by least-recently-played timestamp.

Song IDs are persisted in `data/jamendo_seen.json`; playback timestamps and
play counts are persisted in `data/playback.json` — restarting never makes a
track eligible again.

---

## How it works — Aadam Jacobs Collection

1. `radio-jacobs/scripts/ia_gestor.py` reads `radio-jacobs/clima.json` and
   walks the `aadamjacobs` collection on Internet Archive using a persistent
   cursor (ordered by identifier, resumable across restarts).
2. Per show it picks up to **2 tracks** (`MAX_TRACKS_PER_SHOW`) — MP3
   derivatives only, 10 s–7 min long, excluding crowd noise, tuning and bare
   intros.
3. All shows in the collection carry the Live Music Archive permission model
   (public, free, non-commercial with attribution). The license is emitted as
   `"permission"` in `songs.json`; attribution (venue, date, taper) travels to
   the web panel.
4. A `LOW_WATERMARK` of 20 tracks and a `BATCH_SIZE` of 120 gives ≈ 9 h of
   music in the box at all times (≈ 600 MB on disk).

---

## Listener gate

Both stations pause playback when no one is connected, so tracks and download
quota are not wasted on an empty audience. The gate is configured via `.env`:

```bash
IDLE_POLL=2.0   # seconds between listener polls (0 = disable gate)
IDLE_HITS=1     # consecutive empty polls before pausing
```

With the defaults above the station resumes within **2 seconds** of a new
connection.

---

## Ingest behaviour — Jamendo

`data/playback.json` → `source` keeps the ingest cursor:

```json
"source": {
  "tag_index": 3,
  "offsets": {"bluesrock|relevance": 600}
}
```

- `tag_index` rotates which `genero` starts each cycle so every tag gets used.
- `offsets` remembers how deep each `(tag, order)` window was read. Without it
  every cycle re-reads the top of the ranking, `jamendo_seen.json` swallows it,
  and the station quietly runs dry.

---

## Ingest behaviour — Aadam Jacobs Collection

`radio-jacobs/data/playback.json` → `source` keeps the cursor:

```json
"source": {
  "identifier": "ajc01234_band_name_2005-06-07",
  "pasada": 1,
  "canciones_pasada": 87
}
```

- `identifier` is the last Archive.org identifier read. The next cycle queries
  `identifier:[cursor TO *]` ordered ascending, so the sweep always moves
  forward.
- When the end of the collection is reached the cursor wraps to the start for
  another pass. The dedup is per **track** (`data/archive_seen.json`), so
  re-reading a show only adds the tracks not yet downloaded.
- If a full pass produces 0 new tracks the collection is considered exhausted
  and the gestor stops until new shows are added to the archive.

---

## Per-station management

```bash
# Jamendo Radio
./scripts/radio.sh start|stop|restart|status|logs

# Aadam Jacobs Collection
./radio-jacobs/scripts/radio-jacobs.sh start|stop|restart|status|logs
```

---

## Configuration

### Jamendo Radio

| File                     | Purpose                                                            |
| ------------------------ | ------------------------------------------------------------------ |
| `clima.json`             | Target atmosphere (mood, genre, energy, texture, voice…)           |
| `.env`                   | `ICE_PASSWORD`, `IDLE_POLL`, `IDLE_HITS`                           |
| `scripts/gestor.py`      | `LOW_WATERMARK`, `BATCH_SIZE`, `MAX_DIST`, `MAX_TRACKS_PER_ARTIST` |
| `data/playback.json`     | Durable per-track state + ingest cursor                            |
| `data/jamendo_seen.json` | All-time seen Jamendo IDs                                          |

### Aadam Jacobs Collection

| File                                  | Purpose                                                                                        |
| ------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `radio-jacobs/clima.json`             | Target genre/energy/complexity profile                                                         |
| `radio-jacobs/.env`                   | `ICE_PASSWORD`, `IDLE_POLL`, `IDLE_HITS`                                                       |
| `radio-jacobs/scripts/ia_gestor.py`   | `LOW_WATERMARK` (20), `BATCH_SIZE` (120), `MAX_TRACKS_PER_SHOW` (2), `MAX_TRACK_SECONDS` (420) |
| `radio-jacobs/data/playback.json`     | Durable per-track state + collection cursor                                                    |
| `radio-jacobs/data/archive_seen.json` | All-time seen Archive.org track IDs                                                            |
| `radio-jacobs/data/band_genero.json`  | Optional: `{"band name": ["genre"]}` to enable live genre filtering                            |

### clima.json example (Jamendo)

```json
{
  "mood": ["melancolico", "intimo"],
  "genero": ["folk"],
  "texture": "organica",
  "energy": 0.35,
  "complexity": 0.45,
  "voice": "vocal",
  "instrumentation": ["guitarra", "piano"],
  "temporalidad": "contemporaneo"
}
```

---

## ML hook

`climate_distance(song_climate, target_climate) -> float` in both `gestor.py`
and `ia_gestor.py` is the single point of contact between the scheduling system
and the climate model. Replace it with your model when ready.

Expected signature: `(dict, dict) -> float` in `[0.0, 1.0]` (0 = perfect match).

`MAX_DIST` only gates new downloads in `fetch_batch`. `write_queue` uses the
`climate_dist` stored at download time and never re-filters the existing catalog.

---

## File structure

```
reset.sh                      full reset / start / stop / status for both stations
stations.json                 web panel station registry (id, mount, root)
radio.liq                     Liquidsoap config — Jamendo Radio
clima.json                    target climate — Jamendo Radio
songs.json                    catalog + playback flags — Jamendo Radio
queue.m3u / queue.offline.m3u playlists — Jamendo Radio
data/
  playback.json               durable state — Jamendo Radio
  jamendo_seen.json           all-time seen IDs — Jamendo Radio
archive/music/                played MP3s retained for offline fallback
music/                        active downloaded MP3s — Jamendo Radio
logs/                         gestor.log, played.tsv, nowplaying.txt — Jamendo Radio
scripts/
  gestor.py                   core: download, filter, state, queues
  web_server.py               web panel + /stations /now /history API
  radio.sh                    start / stop / restart / status / logs
  listeners.py                icecast listener count helper
  .env.example                environment template

radio-jacobs/                 Aadam Jacobs Collection station
  radio.liq                   Liquidsoap config
  clima.json                  target climate
  songs.json                  catalog + playback flags
  queue.m3u / queue.offline.m3u
  data/
    playback.json             durable state + collection cursor
    archive_seen.json         all-time seen Archive.org track IDs
    band_genero.json          optional genre map per band
  archive/music/              offline fallback MP3s
  music/                      active downloaded MP3s
  logs/                       gestor.log, played.tsv, nowplaying.txt
  scripts/
    ia_gestor.py              core: Archive.org ingest, filter, state, queues
    radio-jacobs.sh           start / stop / restart / status / logs
    listeners.py              icecast listener count helper
  venv -> ../venv             symlink to shared Python venv (created by reset.sh)
web/
  index.html                  web panel UI (multi-station, English)
```

---

## License

MIT. Jamendo tracks carry their own Creative Commons licenses.  
Aadam Jacobs Collection shows are distributed under the Live Music Archive
permission model (public, free, non-commercial, with attribution).
