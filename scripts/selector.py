#!/usr/bin/env python3
"""selector.py: puntúa las canciones y arma la cola dinámica (queue.m3u).

score = similarity x 0.35 + diversity x 0.25 + freshness x 0.15
      + rarity x 0.15 + randomness x 0.10
Elige de forma ponderada entre el top-K de candidatos (serendipia controlada).
Registra en el diario por qué eligió cada canción.

Modo ronda sin repetición (por defecto):
  - Cada canción solo suena UNA vez por ronda completa del catálogo.
  - Al terminar la ronda, el pool se reinicia automáticamente.
  - Se controla el espaciado entre canciones del mismo artista (--min-gap).
"""
import argparse
import json
import math
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import PROJECT_ROOT, load_json, save_json  # noqa: E402

from lib import EMIT_LICENSES  # noqa: E402

WEIGHTS = {
    "similarity": 0.35,
    "diversity": 0.25,
    "freshness": 0.15,
    "rarity": 0.15,
    "randomness": 0.10,
}
DEFAULT_WINDOW = 15
DEFAULT_TOP_K = 5
DEFAULT_TEMP = 1.0
NO_REPEAT_ARTIST = 5
DEFAULT_MIN_GAP = 10
DEFAULT_MAX_PLAYS = 3
AFFINITY_THRESHOLD = 0.7
AFFINITY_GAP_WINDOW = 5
MIN_EFFECTIVE_GAP = 2


def atomic_write(path, text):
    """Escribe el archivo atómico (tmp + os.replace): Liquidsoap con
    reload=watch nunca lee la playlist a medias."""
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def sfloat(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def climate_distance(a, b):
    d, n = 0.0, 0
    for dim in ("energy", "complexity"):
        d += abs(sfloat(a.get(dim)) - sfloat(b.get(dim)))
        n += 1
    for dim in ("mood", "instrumentation"):
        left, right = a.get(dim), b.get(dim)
        if isinstance(left, list) and isinstance(right, list):
            inter = set(left) & set(right)
            denom = max(len(left), len(right), 1)
            d += 1.0 - (len(inter) / denom)
            n += 1
    for dim in ("texture", "voice", "temporalidad"):
        d += 0.0 if a.get(dim) == b.get(dim) else 1.0
        n += 1
    return (d / n) if n else 0.0


def read_played(path, by_id):
    order = []
    counts = {}
    last_play = {}
    resolved = {s["id"]: str((PROJECT_ROOT / s["file"]).resolve())
                for s in by_id.values()}
    p = Path(path)
    if not p.exists():
        return order, counts, last_play
    for idx, raw in enumerate(p.read_text(encoding="utf-8",
                                          errors="replace").splitlines()):
        line = raw.strip()
        if not line:
            continue
        first = line.split("|", 1)[0].strip()
        if not first:
            continue
        sid = next((k for k, v in resolved.items()
                    if v == first or v.endswith(first)), None)
        if sid is None:
            continue
        order.append((idx, sid))
        counts[sid] = counts.get(sid, 0) + 1
        last_play[sid] = idx
    return order, counts, last_play


def main():
    ap = argparse.ArgumentParser(description="arma la cola dinámica de la radio")
    ap.add_argument("--songs", default=str(PROJECT_ROOT / "songs.json"))
    ap.add_argument("--queue", default=str(PROJECT_ROOT / "queue.m3u"))
    ap.add_argument("--played", default=str(PROJECT_ROOT / "logs" / "played.txt"))
    ap.add_argument("--state", default=str(PROJECT_ROOT / "data" / "scheduled.json"))
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--topk", type=int, default=DEFAULT_TOP_K)
    ap.add_argument("--temp", type=float, default=DEFAULT_TEMP,
                    help="1=balance, >1 más azar/serendipia, <1 más coherencia")
    ap.add_argument("--clima", default=str(PROJECT_ROOT / "clima.json"),
                    help="JSON con el clima objetivo (campos opcionales)")
    ap.add_argument("--min-gap", type=int, default=DEFAULT_MIN_GAP,
                    help="mínimo de canciones entre repeticiones de una misma canción")
    ap.add_argument("--max-plays", type=int, default=DEFAULT_MAX_PLAYS,
                    help="máximo de reproducciones de una canción en el historial "
                         "(se ignora en modo ronda sin repetición)")
    ap.add_argument("--cycle", default=str(PROJECT_ROOT / "data" / "cycle_state.json"),
                    help="estado de la ronda sin repetición (JSON)")
    ap.add_argument("--seed", type=int)
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    songs = [s for s in load_json(args.songs) if s.get("license") in EMIT_LICENSES]
    if not songs:
        print("No hay canciones emisibles. Revisa el campo license en songs.json.",
              file=sys.stderr)
        return 1
    by_id = {s["id"]: s for s in songs}

    order, counts, last_play = read_played(args.played, by_id)
    last_id = order[-1][1] if order else None
    recent = [sid for _, sid in order[-NO_REPEAT_ARTIST:]]
    last = by_id.get(last_id)
    history = [by_id[sid] for sid in recent if sid in by_id]

    # --- Ronda sin repetición: las canciones solo vuelven a ser elegibles
    # cuando terminó la ronda anterior (se reprodujo todo el catálogo).
    cycle_path = Path(args.cycle)
    cycle_played = set()
    round_no = 1
    if cycle_path.exists():
        try:
            _data = json.loads(cycle_path.read_text(encoding="utf-8"))
            cycle_played = set(_data.get("played", []))
            round_no = int(_data.get("round", 1)) or 1
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            cycle_played = set()
            round_no = 1
    current_ids = {s["id"] for s in songs}
    cycle_played &= current_ids
    round_over = False

    target_climate = None
    target_dims = None
    clima_path = Path(args.clima)
    if clima_path.exists():
        try:
            raw = json.loads(clima_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw:
                target_climate = raw
                target_dims = set(raw.keys())
        except (json.JSONDecodeError, OSError):
            pass

    def filter_climate(cl):
        if target_dims is None or not cl:
            return cl
        return {k: v for k, v in cl.items() if k in target_dims}

    def similarity(song):
        cl = filter_climate(song.get("climate", {}))
        if target_climate:
            return 1.0 - climate_distance(cl, filter_climate(target_climate))
        if last:
            return 1.0 - climate_distance(cl, filter_climate(last.get("climate", {})))
        return 0.7

    def score(song):
        count = counts.get(song["id"], 0)
        sim = similarity(song)
        cl = filter_climate(song.get("climate", {}))
        if history:
            div = sum(climate_distance(cl, filter_climate(h.get("climate", {})))
                      for h in history) / len(history)
        else:
            div = sim
        fresheness = 1.0 / (1.0 + count)
        parts = {
            "similarity": sim,
            "diversity": div,
            "freshness": fresheness,
            "rarity": 1.0 / (1.0 + count * 2),
            "randomness": random.random(),
        }
        total = sum(WEIGHTS[k] * parts[k] for k in WEIGHTS)
        return total, parts, sim

    queue, chosen_ids = [], set()
    recent_artists = [by_id[sid].get("artist") for sid in recent if sid in by_id]
    hist_len = len(order)

    def effective_gap(sim):
        if sim >= AFFINITY_THRESHOLD:
            scale = AFFINITY_GAP_WINDOW / (1.0 - AFFINITY_THRESHOLD)
            gap = max(MIN_EFFECTIVE_GAP,
                      args.min_gap - int((sim - AFFINITY_THRESHOLD) * scale))
        else:
            gap = args.min_gap
        return gap

    def eligible(s, current_pos):
        if s["id"] in chosen_ids:
            return False
        if s["id"] in cycle_played:
            return False
        lp = last_play.get(s["id"])
        if lp is not None:
            sim = similarity(s)
            gap = effective_gap(sim)
            if current_pos - lp < gap:
                return False
        return True

    while len(queue) < args.window and len(chosen_ids) < len(songs):
        scorable = [
            s for s in songs
            if eligible(s, hist_len + len(queue))
            and s.get("artist") not in recent_artists
        ]
        if not scorable:
            scorable = [s for s in songs if eligible(s, hist_len + len(queue))]
        if not scorable:
            if round_over:
                break
            round_over = True
            round_no += 1
            cycle_played = set(chosen_ids)
            print(f"  [ronda agotada] arrancando ronda {round_no} "
                  f"({len(songs)} candidatas)")
            continue
        scored = sorted(((score(s), s) for s in scorable),
                        key=lambda x: x[0][0], reverse=True)
        top = scored[:args.topk]
        weights = [math.exp(total * args.temp) for (total, parts, _), _ in top]
        chosen_s = random.choices(top, weights=weights, k=1)[0][1]
        chosen_ids.add(chosen_s["id"])
        cycle_played.add(chosen_s["id"])
        queue.append(chosen_s)
        recent_artists = (recent_artists + [chosen_s.get("artist")])[-NO_REPEAT_ARTIST:]

    # Rescate: si la cola quedó vacía porque todas las canciones alcanzaron max_plays,
    # relajar restricciones eligiendo las menos reproducidas para no dejar la radio en silencio.
    if not queue and songs:
        rescue = sorted(songs, key=lambda s: counts.get(s["id"], 0))
        for s in rescue[:args.window]:
            queue.append(s)
            chosen_ids.add(s["id"])
            cycle_played.add(s["id"])

    lines = ["#EXTM3U"]
    diario = []
    prev = last
    for i, s in enumerate(queue):
        total, parts, sim = score(s)
        dist = (climate_distance(prev.get("climate", {}), s.get("climate", {}))
                if prev else None)
        lp = last_play.get(s["id"])
        lines.append(f"#EXTINF:,{s['artist']} — {s['title']}")
        lines.append(str((PROJECT_ROOT / s["file"]).resolve()))

        diario.append({
            "id": s["id"],
            "artist": s["artist"],
            "title": s["title"],
            "distancia_clima": round(dist, 3) if dist is not None else None,
            "score": round(total, 3),
            "partes": {k: round(v, 3) for k, v in parts.items()},
            "license": s.get("license"),
            "plays": counts.get(s["id"], 0),
            "repeats": lp is not None,
            "min_gap": effective_gap(sim),
        })
        prev = s

    atomic_write(args.queue, "\n".join(lines) + "\n")
    save_json(args.state, [s["id"] for s in queue])

    cycle_path.parent.mkdir(parents=True, exist_ok=True)
    save_json(args.cycle, {"round": round_no, "played": sorted(cycle_played)})

    with open(PROJECT_ROOT / "logs" / "select.log", "a", encoding="utf-8") as fh:
        for entry in diario:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"Cola escrita en {args.queue}: {len(queue)} canciones. "
          f"Ronda {round_no} ({len(cycle_played)}/{len(songs)} reproducidas este ciclo).")
    for e in diario:
        print(f"  {e['artist']} — {e['title']}  score={e['score']}  "
              f"dist={e['distancia_clima']}  {e['partes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())