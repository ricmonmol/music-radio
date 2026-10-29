import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from queue_order import spread_artists, count_adjacent  # noqa: E402


def mk(*artists):
    return [{"artist": a, "title": str(i)} for i, a in enumerate(artists)]


class SpreadArtistsTest(unittest.TestCase):
    def test_separa_pares(self):
        songs = mk("Tullycraft", "Tullycraft", "Slink Moss", "Slink Moss",
                   "Paulina Hollers", "Paulina Hollers", "The Hollers Trio", "The Hollers Trio")
        out = spread_artists(songs, window=3)
        self.assertEqual(len(out), 8)
        self.assertEqual(count_adjacent(out), 0)

    def test_nunca_descarta(self):
        songs = mk("A", "A", "A", "B")
        out = spread_artists(songs, window=4)
        self.assertEqual(sorted(s["title"] for s in out), ["0", "1", "2", "3"])

    def test_estable_si_ya_separado(self):
        songs = mk("A", "B", "C", "D", "E")
        self.assertEqual(spread_artists(songs, window=4), songs)

    def test_clave_normalizada(self):
        out = spread_artists(mk("The Wire", "Wire", "Bjork"), window=2)
        self.assertEqual(count_adjacent(out), 0)

    def test_recent(self):
        out = spread_artists(mk("A", "B"), window=2, recent=["A"])
        self.assertEqual(out[0]["artist"], "B")


if __name__ == "__main__":
    unittest.main()
