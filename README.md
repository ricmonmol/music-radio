# music-radio

Streaming radio with two stations sharing a single Icecast server and web
panel:

| Station                     | Source                                               | Mount     |
| --------------------------- | ---------------------------------------------------- | --------- |
| **Jamendo Radio**           | Jamendo API — Creative Commons tracks by mood         | `/radio`  |
| **Aadam Jacobs Collection** | Internet Archive `aadamjacobs` — live recordings      | `/jacobs` |

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

./menu.sh             # interactive menu for everything (SSH-friendly)
./indice.sh           # list every script with its description and usage
./indice.sh gestor    # detail for one script
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
   style list both **narrows the search** and **scores the results**.
3. Results are written to `queue.m3u`, ordered by climate proximity.
4. Liquidsoap plays the online queue and moves each played MP3 to
   `archive/music/`.
5. When the queue runs low, `gestor.py` downloads another batch.
6. If the online source is empty, Liquidsoap falls back to
   `queue.offline.m3u`, ordered by least-recently-played timestamp.

---

## How it works — Aadam Jacobs Collection

1. `radio-jacobs/scripts/ia_gestor.py` reads `radio-jacobs/clima.json` and
   walks the `aadamjacobs` collection on Internet Archive, show by show.
2. Per show it keeps a few tracks — MP3 derivatives only, excluding crowd
   noise, tuning and bare intros.
3. All shows in the collection carry the Live Music Archive permission model
   (public, free, non-commercial with attribution). The license is emitted as
   `"permission"` in `songs.json`; attribution (venue, date, taper) travels to
   the web panel.
4. A few hours of music are kept buffered on disk at all times.

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

| File                | Purpose                                                     |
| ------------------- | ----------------------------------------------------------- |
| `clima.json`        | Target atmosphere (mood, genre, energy, texture, voice…)    |
| `.env`              | `ICE_PASSWORD` and runtime tuning — see `.env.example`      |

### Aadam Jacobs Collection

| File                                | Purpose                                                            |
| ----------------------------------- | ------------------------------------------------------------------ |
| `radio-jacobs/clima.json`           | Target genre/energy/complexity profile                             |
| `radio-jacobs/.env`                 | `ICE_PASSWORD` and runtime tuning — see `.env.example`             |
| `radio-jacobs/data/band_genero.json` | Optional: `{"band name": ["genre"]}` to enable live genre filtering |

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

---

## License

MIT. Jamendo tracks carry their own Creative Commons licenses.  
Aadam Jacobs Collection shows are distributed under the Live Music Archive
permission model (public, free, non-commercial, with attribution).
