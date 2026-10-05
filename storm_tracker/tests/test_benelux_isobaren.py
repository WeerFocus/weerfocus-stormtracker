# storm_tracker/tests/test_benelux_isobaren.py
"""Tests van de Benelux-afstand, de zone en de isobaren (synthetische velden, alleen in de tests)."""

import unittest

import numpy as np

from storm_tracker import benelux
from storm_tracker.tracker import TrackerInstellingen, afstand_km, zoek_minima


class TestBenelux(unittest.TestCase):
    def test_binnen_is_nul(self):
        self.assertEqual(benelux.afstand_km(52.1, 5.1), 0.0)   # Utrecht
        self.assertEqual(benelux.afstand_km(50.85, 4.35), 0.0)  # Brussel
        self.assertEqual(benelux.afstand_km(49.6, 6.13), 0.0)   # Luxemburg

    def test_afstand_buiten(self):
        # Londen ligt ~ 200 km van de Zeeuwse/Belgische kust
        self.assertTrue(150 < benelux.afstand_km(51.5, -0.12) < 260)
        # Reykjavik ruim 1900 km
        self.assertTrue(1800 < benelux.afstand_km(64.15, -21.9) < 2100)

    def test_zone_ring_ligt_op_straal(self):
        ring = benelux.zone_ring(500, stappen=36)
        self.assertEqual(ring[0], ring[-1])
        for lon, lat in ring[:-1]:
            self.assertAlmostEqual(benelux.afstand_km(lat, lon), 500, delta=5)


class TestKernTussenRoosterpunten(unittest.TestCase):
    def test_kern_niet_op_roosterpunt(self):
        lats = np.arange(30.0, 75.01, 0.25)
        lons = np.arange(-70.0, 40.01, 0.25)
        lon2d, lat2d = np.meshgrid(lons, lats)
        lat0, lon0 = 55.1, -20.13  # tussen de roosterpunten
        dx = (lon2d - lon0) * np.cos(np.radians(lat0))
        veld = 1015 - 30 * np.exp(-(dx ** 2 + (lat2d - lat0) ** 2) / (2 * 4.0 ** 2))
        m = zoek_minima(veld, lats, lons, TrackerInstellingen())[0]
        self.assertLess(afstand_km(m.lat, m.lon, lat0, lon0), 6)


class TestIsobaren(unittest.TestCase):
    def test_contouren_om_een_put(self):
        try:
            import contourpy  # noqa: F401
        except ImportError:
            self.skipTest("contourpy niet geïnstalleerd")
        from storm_tracker.isobaren import bouw_isobaren
        lats = np.arange(30.0, 75.01, 0.25)
        lons = np.arange(-70.0, 40.01, 0.25)
        lon2d, lat2d = np.meshgrid(lons, lats)
        veld = 1016 - 30 * np.exp(-(((lon2d + 20) * 0.6) ** 2 + (lat2d - 55) ** 2) / (2 * 4.0 ** 2))
        fc = bouw_isobaren(veld, lats, lons)
        niveaus = {f["properties"]["hpa"] for f in fc["features"]}
        self.assertTrue({988, 992, 1000, 1008}.issubset(niveaus))
        self.assertTrue(all(n % 4 == 0 for n in niveaus))


    def test_masker_haalt_isobaren_weg(self):
        try:
            import contourpy  # noqa: F401
        except ImportError:
            self.skipTest("contourpy niet geïnstalleerd")
        from storm_tracker.isobaren import bouw_isobaren
        lats = np.arange(30.0, 75.01, 0.25)
        lons = np.arange(-70.0, 40.01, 0.25)
        lon2d, lat2d = np.meshgrid(lons, lats)
        # Diepe put links, het rechterdeel van het domein gemaskeerd (zoals hoog terrein)
        veld = 1016 - 30 * np.exp(-(((lon2d + 40) * 0.6) ** 2 + (lat2d - 55) ** 2) / (2 * 4.0 ** 2)) + 0.2 * lon2d
        masker = lon2d > 0
        for f in bouw_isobaren(veld, lats, lons, masker)["features"]:
            self.assertTrue(all(lon <= 0.3 for lon, _ in f["geometry"]["coordinates"]))


if __name__ == "__main__":
    unittest.main()
