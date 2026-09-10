#!/usr/bin/env python3
"""lib.py: utilidades compartidas para todos los scripts de la radio.

Centraliza I/O de JSON, carga de client_id de Jamendo, invocación del
selector y constantes del proyecto para eliminar duplicación.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
SONGS_PATH = PROJECT_ROOT / "songs.json"
QUEUE_PATH = PROJECT_ROOT / "queue.m3u"
CLIMA_PATH = PROJECT_ROOT / "clima.json"
PLAYED_PATH = PROJECT_ROOT / "logs" / "played.txt"
META_PATH = PROJECT_ROOT / "data" / "catalog_meta.json"
ARCHIVE_DIR = PROJECT_ROOT / "archive"
MUSIC_DIR = PROJECT_ROOT / "music"
CLIENT_FILE = SCRIPTS_DIR / ".jamendo_client"

EMIT_LICENSES = {
    "cc0", "public-domain", "cc-by", "cc-by-sa", "cc-by-nc", "permission",
}


def load_json(path):
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = p.read_text(encoding="utf-8")
        return json.loads(data) if data.strip() else []
    except json.JSONDecodeError:
        return []


def load_json_dict(path):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = p.read_text(encoding="utf-8")
        raw = json.loads(data) if data.strip() else {}
        return raw if isinstance(raw, dict) else {}
    except json.JSONDecodeError:
        return {}


def save_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def get_client_id():
    """Fuente única de verdad para el client_id de Jamendo.

    Busca en orden:
      1. Variable de entorno JAMENDO_CLIENT_ID
      2. Archivo scripts/.jamendo_client
    Retorna None si no se encuentra.
    """
    env_val = os.environ.get("JAMENDO_CLIENT_ID")
    if env_val:
        return env_val
    if CLIENT_FILE.exists():
        token = CLIENT_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    return None


def require_client_id():
    cid = get_client_id()
    if not cid:
        print(
            "Falta el client_id de Jamendo.\n"
            "  Opción 1: export JAMENDO_CLIENT_ID=tu_token\n"
            "  Opción 2: echo 'tu_token' > scripts/.jamendo_client\n"
            "  Consíguelo en https://devportal.jamendo.com",
            file=sys.stderr,
        )
        sys.exit(1)
    return cid


def run_selector(clima=None, extra_args=None):
    """Invoca selector.py y regenera queue.m3u.

    Retorna el número de canciones en la cola, o -1 si falla.
    """
    clima_path = clima or str(CLIMA_PATH)
    cmd = [
        str(PROJECT_ROOT / "venv" / "bin" / "python"),
        str(SCRIPTS_DIR / "selector.py"),
        "--clima", clima_path,
    ]
    if extra_args:
        cmd.extend(extra_args)
    res = subprocess.run(cmd, capture_output=True, text=True)
    print(res.stdout, end="")
    if res.returncode != 0:
        print(res.stderr, file=sys.stderr)
        return -1
    queue = Path(QUEUE_PATH)
    if not queue.exists():
        return 0
    count = sum(
        1 for line in queue.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
        and line.strip().split("?")[0].endswith((".mp3", ".flac", ".ogg",
                                                 ".m4a", ".aac", ".wav", ".opus"))
    )
    return count


def restart_liquidsoap():
    """Reinicia liquidsoap de forma segura vía PID file."""
    subprocess.run(
        ["bash", str(SCRIPTS_DIR / "restart_radio.sh")],
        check=False,
    )


def now_iso():
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")
