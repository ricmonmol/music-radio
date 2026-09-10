# music-radio

Radio en streaming basada en **clima musical**: selecciona canciones de forma
continua según una atmósfera configurable (mood, energía, textura, etc.).

## Stack

- Liquidsoap — reproducción y salida de audio
- Icecast2 — streaming HTTP
- Python 3 (stdlib) — selección, catálogo y panel web
- Jamendo — fuente de música libre (CC)

## Uso

```bash
# Arranque completo
./scripts/radio_reboot.sh

# Forzar descarga de N temas nuevos
./scripts/radio_reboot.sh --auto 15

# Regenerar la cola sin reiniciar
./scripts/refresh_queue.sh
```

## Configuración

- `clima.json` — atmósfera objetivo (mood, energy, texture…)
- `.env` — credenciales (ver `scripts/.env.example`)

## Licencia

MIT. Las pistas de Jamendo tienen licencias CC según el artista.
