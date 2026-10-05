# storm_tracker/tests/test_tracker.py
"""
Tests van het trackeralgoritme met synthetische MSLP-velden (Gauss-putten).
Deze velden bestaan alleen in de tests; niets ervan komt in de app of op de CDN.

Draaien:  python -m unittest discover -s storm_tracker/tests -t .
"""

import math
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from storm_tracker.tracker import (
    TrackerInstellingen,
    afstand_km,
    bergeron_drempel_hpa,
    ken_ids_toe,
    volg_depressies,
    zoek_minima,
)

LATS = np.arange(30.0, 75.0 + 1e-9, 0.25)
LONS = np.arange(-70.0, 40.0 + 1e-9, 0.25)
LON2D, LAT2D = np.meshgrid(LONS, LATS)
RUN = datetime(2026, 10, 5, 0, tzinfo=timezone.utc)
INST = TrackerInstellingen()


def veld_met_putten(putten, achtergrond=1015.0):
    """putten: lijst (lat, lon, diepte_hpa, straal_graden)."""
    veld = np.full(LAT2D.shape, achtergrond)
    for lat, lon, diepte, straal in putten:
        dx = (LON2D - lon) * math.cos(math.radians(lat))
        dy = LAT2D - lat
        veld -= diepte * np.exp(-(dx ** 2 + dy ** 2) / (2 * straal ** 2))
    return veld


def reeks(putten_per_stap, run=RUN):
    return [(run + timedelta(hours=6 * k), 6 * k, veld_met_putten(p)) for k, p in enumerate(putten_per_stap)]


class TestMinima(unittest.TestCase):
    def test_enkele_put_gevonden_op_juiste_plek(self):
        minima = zoek_minima(veld_met_putten([(55.0, -20.0, 30.0, 4.0)]), LATS, LONS, INST)
        self.assertEqual(len(minima), 1)
        self.assertLess(afstand_km(minima[0].lat, minima[0].lon, 55.0, -20.0), 30)
        self.assertAlmostEqual(minima[0].druk_hpa, 985.0, delta=0.5)

    def test_dichtbij_liggende_putten_samengevoegd(self):
        veld = veld_met_putten([(55.0, -20.0, 30.0, 3.0), (55.0, -17.5, 25.0, 3.0)])
        self.assertEqual(len(zoek_minima(veld, LATS, LONS, INST)), 1)

    def test_ondiepe_put_genegeerd(self):
        veld = veld_met_putten([(55.0, -20.0, 1.0, 4.0)])
        self.assertEqual(zoek_minima(veld, LATS, LONS, INST), [])

    def test_minimum_boven_hoog_terrein_genegeerd(self):
        veld = veld_met_putten([(72.0, -40.0, 20.0, 3.0)])
        groenland = lambda lat, lon: 2500.0 if lat > 60 and -55 < lon < -25 else 0.0
        self.assertEqual(len(zoek_minima(veld, LATS, LONS, INST)), 1)
        self.assertEqual(zoek_minima(veld, LATS, LONS, INST, orografie=groenland), [])


class TestTracking(unittest.TestCase):
    def test_bewegende_gausput(self):
        # 3° lengte en 0,5° breedte per 6 uur naar het oosten, steeds dieper
        stappen = [[(50.0 + 0.5 * k, -40.0 + 3.0 * k, 20.0 + 2.0 * k, 4.0)] for k in range(9)]
        tracks = volg_depressies(reeks(stappen), LATS, LONS, INST)
        self.assertEqual(len(tracks), 1)
        t = tracks[0]
        self.assertEqual(len(t.punten), 9)
        for k, p in enumerate(t.punten):
            self.assertLess(afstand_km(p.lat, p.lon, 50.0 + 0.5 * k, -40.0 + 3.0 * k), 30)
            self.assertAlmostEqual(p.druk_hpa, 1015.0 - (20.0 + 2.0 * k), delta=0.5)
        # Verdieping pas vanaf 24 uur historie: 4 stappen × 2 hPa = 8 hPa
        self.assertTrue(all(p.verdieping_24h is None for p in t.punten[:4]))
        for p in t.punten[4:]:
            self.assertAlmostEqual(p.verdieping_24h, 8.0, delta=0.6)
        # Snelheid: ~3° lengte op ~52° N in 6 uur ≈ 35 km/h
        verwacht = afstand_km(52.0, -30.0, 52.5, -27.0) / 6.0
        self.assertAlmostEqual(t.punten[4].snelheid_kmh, verwacht, delta=5)

    def test_meerdere_minima_over_stappen_gekoppeld(self):
        # Eén depressie trekt oost, een andere noord; ze mogen niet van track wisselen
        stappen = [[(45.0, -50.0 + 4.0 * k, 25.0, 3.5), (40.0 + 2.0 * k, -10.0, 22.0, 3.5)] for k in range(7)]
        tracks = volg_depressies(reeks(stappen), LATS, LONS, INST)
        self.assertEqual(len(tracks), 2)
        oost = min(tracks, key=lambda t: t.punten[0].lon)
        noord = max(tracks, key=lambda t: t.punten[0].lon)
        self.assertEqual(len(oost.punten), 7)
        self.assertEqual(len(noord.punten), 7)
        self.assertTrue(all(abs(p.lat - 45.0) < 0.3 for p in oost.punten))
        self.assertTrue(all(abs(p.lon + 10.0) < 0.3 for p in noord.punten))

    def test_korte_track_verworpen(self):
        # 3 stappen = 12 uur: korter dan 24 uur
        stappen = [[(55.0, -30.0 + 2.0 * k, 25.0, 4.0)] for k in range(3)] + [[] for _ in range(3)]
        self.assertEqual(volg_depressies(reeks(stappen), LATS, LONS, INST), [])

    def test_te_grote_sprong_breekt_track(self):
        stappen = [[(50.0, -50.0, 25.0, 3.0)]] * 5 + [[(50.0, -30.0, 25.0, 3.0)]] * 5
        tracks = volg_depressies(reeks(stappen), LATS, LONS, INST)
        self.assertEqual(len(tracks), 2)


class TestStormIds(unittest.TestCase):
    def _teller(self):
        n = iter(range(1, 100))
        return lambda: f"nieuw-{next(n):02d}"

    def test_id_blijft_tussen_twee_runs(self):
        def stappen(start_k):
            return [[(48.0, -45.0 + 3.0 * k, 25.0, 3.5), (62.0, -30.0 + 2.0 * k, 22.0, 3.5)]
                    for k in range(start_k, start_k + 9)]

        run1 = volg_depressies(reeks(stappen(0)), LATS, LONS, INST)
        maak = self._teller()
        ken_ids_toe(run1, [], 300.0, maak)
        ids_run1 = {round(t.punten[0].lat): t.storm_id for t in run1}

        # Volgende run, 6 uur later: dezelfde depressies, iets verschoven (modelverschil)
        run2_start = RUN + timedelta(hours=6)
        stappen2 = [[(48.3, -45.0 + 3.0 * k + 0.5, 25.0, 3.5), (62.0, -30.0 + 2.0 * k, 22.0, 3.5),
                     (38.0, -60.0 + 2.0 * (k - 1), 24.0, 3.5)]
                    for k in range(1, 10)]
        run2 = volg_depressies(reeks(stappen2, run=run2_start), LATS, LONS, INST)
        self.assertEqual(len(run2), 3)
        ken_ids_toe(run2, run1, 300.0, maak)

        per_lat = {round(t.punten[0].lat): t.storm_id for t in run2}
        self.assertEqual(per_lat[48], ids_run1[48])
        self.assertEqual(per_lat[62], ids_run1[62])
        self.assertNotIn(per_lat[38], ids_run1.values())
        self.assertEqual(len(set(per_lat.values())), 3)

    def test_ver_verschoven_storm_krijgt_nieuw_id(self):
        run1 = volg_depressies(reeks([[(50.0, -40.0 + 3.0 * k, 25.0, 3.5)] for k in range(8)]), LATS, LONS, INST)
        maak = self._teller()
        ken_ids_toe(run1, [], 300.0, maak)
        run2 = volg_depressies(reeks([[(42.0, -40.0 + 3.0 * k, 25.0, 3.5)] for k in range(8)]), LATS, LONS, INST)
        ken_ids_toe(run2, run1, 300.0, maak)
        self.assertNotEqual(run2[0].storm_id, run1[0].storm_id)


class TestBergeron(unittest.TestCase):
    def test_breedtecorrectie(self):
        self.assertAlmostEqual(bergeron_drempel_hpa(60.0), 24.0, places=6)
        self.assertLess(bergeron_drempel_hpa(45.0), 24.0)


if __name__ == "__main__":
    unittest.main()
