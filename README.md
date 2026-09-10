# music-radio

Radio en streaming basada en criterios de "clima musical". Mantiene un flujo
continuo de canciones coherente con una atmósfera sonora configurable, a
partir de un conjunto de artistas.

## Stack

- Python (stdlib) — Selección, ingesta y API
- Liquidsoap — Reproducción y salida
- Icecast2 — Streaming HTTP
- `songs.json` — Base de datos del catálogo

## Estructura

- `radio.liq` — Configuración de Liquidsoap (salida Icecast)
- `scripts/selector.py` — Genera la cola de reproducción según clima
- `scripts/catalog_manager.py` — Gestión del catálogo (CRUD)
- `scripts/ingest.py` — Ingesta de pistas al catálogo
- `scripts/fetch_jamendo.py` — Obtención de metadatos/acústica desde Jamendo
- `scripts/start_radio.sh` — Lanza lector de cola + Liquidsoap + web
- `web/index.html` — Panel de estado (sirve `web_server.py`)

## Flujo

1. `selector.py` lee `songs.json` y genera `queue.m3u` según `clima.json`.
2. `radio.liq` reproduce `queue.m3u` y emite vía Icecast (mount `/radio`).
3. `web_server.py` expone estado y control en `web/index.html`.

## En vivo

- Radio (stream MP3 192 kbps): <http://201.236.188.118:8000/radio>
- Panel web: <http://201.236.188.118:8080>

Los puertos 8000 (Icecast) y 8080 (panel, `--host 0.0.0.0` en
`start_radio.sh`) se redirigen desde el router hacia el server.

## Configuración

- `clima.json` — Clima musical activo
- `.env` — Credenciales (p. ej. `ICE_PASSWORD`) y API keys. No se versiona.

## Licencia

MIT — ver `LICENSE`.

