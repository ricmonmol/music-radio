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
   filtered by `climate_distance()` (the ML hook — see below)
2. Songs are written to `queue.m3u` ordered by climate proximity
3. Liquidsoap plays them in order; on each track it calls `gestor.py`
4. When fewer than 6 unplayed songs remain, `gestor.py` downloads a new batch,
   deletes the played mp3s, and writes a fresh queue — no gaps, no repeats

Song IDs are persisted in `data/jamendo_seen.json` so the same track is never
downloaded twice across cycles.

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
| `scripts/gestor.py` | Tunables: `LOW_WATERMARK`, `BATCH_SIZE`, `MAX_DIST` |

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

## ML hook

`climate_distance(song_climate, target_climate) -> float` in `gestor.py` is
the single point of contact between the scheduling system and the climate
model. Replace it with your model when ready — nothing else needs to change.

Expected signature: `(dict, dict) -> float` in `[0.0, 1.0]` (0 = perfect match).

## File structure

```
radio.liq            liquidsoap config
clima.json           target climate
songs.json           current catalog (downloaded mp3s + metadata)
queue.m3u            current playlist (rewritten automatically)
data/
  jamendo_seen.json  all-time seen Jamendo IDs (never re-downloaded)
logs/
  gestor.log         download + queue cycle log
  played.txt         playback history (written by liquidsoap)
  nowplaying.txt     currently playing track
music/               mp3 files (auto-managed, old ones deleted after each cycle)
scripts/
  gestor.py          core: download, climate filter, queue management
  web_server.py      web panel + /now and /history API
  radio.sh           start / stop / restart / status / logs
  refresh_queue.sh   cron backup (every 30 min)
  cleanup_logs.sh    log rotation (daily)
```

## License

MIT. Jamendo tracks are licensed under CC by their respective artists.
