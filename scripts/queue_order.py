"""Orden de colas con separación de artistas, compartido por ambas radios."""
import re


def artist_key(name) -> str:
    """Normaliza el nombre de artista para comparar ("The X" == "x")."""
    s = (name or "").lower()
    s = re.sub(r"^(the|los|la|el|les)\s+", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def spread_artists(songs: list, window: int = 4, recent: list | None = None) -> list:
    """Reordena `songs` evitando repetir un artista dentro de `window` temas.

    Es estable: conserva el orden base y en cada posición toma el primer tema
    cuyo artista no esté entre los últimos `window` colocados. Si todos los
    restantes chocan, toma el de artista usado hace más tiempo; nunca descarta
    temas. `recent` son las claves de artista ya emitidas justo antes.
    """
    history = [artist_key(a) for a in (recent or [])][-window:] if window > 0 else []
    remaining = list(songs)
    ordered = []
    while remaining:
        blocked = set(history[-window:]) if window > 0 else set()
        idx = next(
            (i for i, s in enumerate(remaining) if artist_key(s.get("artist")) not in blocked),
            None,
        )
        if idx is None:
            # Todos chocan: el que lleva más tiempo sin sonar.
            def age(i):
                k = artist_key(remaining[i].get("artist"))
                for back, h in enumerate(reversed(history)):
                    if h == k:
                        return -back
                return -len(history) - 1
            idx = min(range(len(remaining)), key=lambda i: (age(i), i))
        chosen = remaining.pop(idx)
        ordered.append(chosen)
        history.append(artist_key(chosen.get("artist")))
    return ordered


def count_adjacent(songs: list) -> int:
    """Cantidad de pares consecutivos con el mismo artista."""
    keys = [artist_key(s.get("artist")) for s in songs]
    return sum(1 for a, b in zip(keys, keys[1:]) if a == b)
