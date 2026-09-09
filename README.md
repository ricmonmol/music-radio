# music-radio

Radio algorítmica basada en "clima musical": un flujo continuo que mantiene
una atmósfera sonora (melancólico, cálido, introspectivo…) en lugar de una
playlist tradicional. El sistema construye y mantiene una secuencia coherente
de canciones desde un conjunto de artistas semilla, con serendipia como
característica, no como error.

Stack: Python (stdlib) + [Liquidsoap](https://www.liquidsoap.info/) +
[Icecast2](https://icecast.org/) + `songs.json` como única base de datos.

## Arquitectura

```
songs.json          → catálogo (NO está en el repo: contiene derechos de emisión)
  │
  ▼
selector.py         genera queue.m3u según el clima (clima.json)
  │
  ▼
Queue (m3u)         ──►  Liquidsoap (radio.liq)  ──►  Icecast  (/radio)
  │
  ▼
web_server.py       panel web de estado (web/index.html)
```

Archivos clave:

- `radio.liq` — configuración de Liquidsoap (emisión Icecast).
- `scripts/selector.py` — selección de canciones según clima.
- `scripts/catalog_manager.py` — gestión del catálogo.
- `scripts/ingest.py` / `scripts/fetch_jamendo.py` — ingesta de música.
- `scripts/start_radio.sh` — arranca lector de cola, Liquidsoap y web/API.

## Despliegue en el servidor

El repositorio contiene solo el código. El catálogo con derechos de emisión
(`songs.json`, `music/`) y los secretos (`.env`) se transfieren aparte, fuera
de git:

```bash
git clone git@github.com:ricmonmol/music-radio.git
rsync -av songs.json music/ .env servidor:/ruta/a/radio/
```

En el servidor ajusta las rutas del directorio en los scripts y lanza:

```bash
./scripts/start_radio.sh
```

Las contraseñas del sistema (p. ej. Icecast) van en `.env`.

## Licencia

MIT. El software es libre; el contenido musical con derechos de emisión no se
distribuye desde este repositorio.