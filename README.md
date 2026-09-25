# music-radio

Streaming radio based on **musical climate**: downloads and plays songs from
Jamendo filtered by a configurable atmosphere (mood, energy, texture, etc.).

## Stack

- **Liquidsoap** — audio playback and stream output
- **Icecast2** — HTTP audio streaming
- **Python 3** (stdlib only) — download, climate filtering, queue, web panel
- **Jamendo API** — free music source (Creative Commons)

## How it works

1. `gestor.py` reads `clima.json` and downloads a batch of songs from Jamendo
   filtered by `climate_distance()` (the ML hook — see below).
2. Only unplayed tracks are written to `queue.m3u`, ordered by climate proximity.
3. Liquidsoap plays the online queue and marks each track as started immediately;
   that durable state is kept in `data/playback.json`.
4. When the online queue is low, `gestor.py` downloads another batch. Played MP3s
   are moved to `archive/music/` instead of being deleted and become the offline
   fallback pool.
5. If the online source is empty or renewal fails, Liquidsoap uses
   `queue.offline.m3u`, ordered by the least-recently-played timestamp.
6. `scripts/radio.sh start` brings the stream up first and renews the queues in
   the background, so a slow download never keeps the stream offline.

Normal online playback never repeats a track. Repetition is possible only when
the offline fallback is exhausted or the service has no online material.

Song IDs are persisted in `data/jamendo_seen.json`; playback timestamps and
play counts are persisted in `data/playback.json`, so restarting the service or
cleaning raw logs does not make a track eligible again.

## Usage

```bash
# Start everything (icecast + liquidsoap + web panel)
./scripts/radio.sh start

# Stop
./scripts/radio.sh stop

# Restart (e.g. after editing radio.liq or clima.json)
./scripts/radio.sh restart

# Show status + catalog info
./scripts/radio.sh status

# Tail logs
./scripts/radio.sh logs
```

**Listen:** `http://localhost:8000/radio`  
**Web panel:** `http://localhost:8080`

## Configuration

| File | Purpose |
|---|---|
| `clima.json` | Target atmosphere (mood, genre, energy, texture, voice…) |
| `.env` | Credentials — copy from `scripts/.env.example` |
| `scripts/gestor.py` | Tunables: `LOW_WATERMARK`, `BATCH_SIZE`, `MAX_DIST`, `HISTORY_RETENTION_DAYS` |
| `data/playback.json` | Durable per-track playback state |
| `data/jamendo_seen.json` | Permanent Jamendo ID history |

### clima.json example

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

Los estilos (género) se toman del tag `genres` de Jamendo: agregá los que quieras
como lista en `genero` (p.ej. `["folk", "indie"]`).

`logs/played.tsv` se conserva durante `HISTORY_RETENTION_DAYS` (7 por defecto);
`data/playback.json` y `data/jamendo_seen.json` son el estado anti-repetición
permanente.

## ML hook

`climate_distance(song_climate, target_climate) -> float` in `gestor.py` is
the single point of contact between the scheduling system and the climate
model. Replace it with your model when ready — nothing else needs to change.

Expected signature: `(dict, dict) -> float` in `[0.0, 1.0]` (0 = perfect match).

## File structure

```
radio.liq            liquidsoap config
clima.json           target climate
songs.json           current catalog and playback flags
queue.m3u            online playlist (rewritten automatically)
queue.offline.m3u    archive fallback playlist
data/
  playback.json      durable playback state
  jamendo_seen.json  all-time seen Jamendo IDs (never re-downloaded)
archive/
  music/             played MP3s retained for offline fallback
logs/
  gestor.log         download + queue cycle log
  played.txt         web playback history
  played.tsv         timestamped temporary playback events
  nowplaying.txt     currently playing track
music/               active downloaded MP3s
scripts/
  gestor.py          core: download, climate filter, state, queues and archive
  web_server.py      web panel + /now and /history API
  radio.sh           start / stop / restart / status / logs
  refresh_queue.sh   cron backup (every 30 min)
  cleanup_logs.sh    log rotation and temporary history cleanup
```

## License

MIT. Jamendo tracks are licensed under CC by their respective artists.
