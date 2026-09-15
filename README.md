# music-radio

Streaming radio based on **musical climate**: continuously selects songs based
on a configurable atmosphere (mood, energy, texture, etc.).

## Stack

- Liquidsoap — audio playback and output
- Icecast2 — HTTP streaming
- Python 3 (stdlib) — selection, catalog, and web panel
- Jamendo — free music source (CC)

## Usage

```bash
# Full startup
./scripts/radio_reboot.sh

# Force download of N new tracks
./scripts/radio_reboot.sh --auto 15

# Refresh the queue without restarting
./scripts/refresh_queue.sh
```

## Configuration

- `clima.json` — target atmosphere (mood, energy, texture…)
- `.env` — credentials (see `scripts/.env.example`)

## License

MIT. Jamendo tracks are licensed under CC by their respective artists.
