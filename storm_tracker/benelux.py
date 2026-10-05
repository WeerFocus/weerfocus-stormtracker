# storm_tracker/benelux.py
"""
Afstand tot de Benelux (Nederland, België, Luxemburg) en de zone eromheen.
De grenzen komen uit benelux.geojson: dezelfde landsgrenzen als assets/custom.geo.json
van het dashboard, zodat kaart en berekening overeenkomen.
"""

import json
import math
from functools import lru_cache
from pathlib import Path

import numpy as np

AARDSTRAAL_KM = 6371.0
GEOJSON = Path(__file__).resolve().parent / "benelux.geojson"
VERDICHT_KM = 5.0  # randpunten hoogstens zo ver uit elkaar, voor de afstand tot de rand


@lru_cache(maxsize=1)
def _geometrie():
    """(lijst van ringen als [(lon, lat)], verdichte randpunten als radialen-arrays)."""
    with open(GEOJSON, encoding="utf-8") as f:
        data = json.load(f)
    ringen = []
    for feat in data["features"]:
        g = feat["geometry"]
        polygonen = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        for poly in polygonen:
            ringen.append([tuple(p) for p in poly[0]])  # alleen buitenring; Benelux heeft geen relevante gaten

    lons, lats = [], []
    for ring in ringen:
        for (lon1, lat1), (lon2, lat2) in zip(ring, ring[1:]):
            d = _haversine(lat1, lon1, lat2, lon2)
            n = max(1, int(math.ceil(d / VERDICHT_KM)))
            for k in range(n):
                t = k / n
                lons.append(lon1 + (lon2 - lon1) * t)
                lats.append(lat1 + (lat2 - lat1) * t)
    return ringen, np.radians(np.array(lats)), np.radians(np.array(lons))


def _haversine(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * AARDSTRAAL_KM * math.asin(min(1.0, math.sqrt(a)))


def _binnen(ring, lon, lat) -> bool:
    binnen = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > lat) != (y2 > lat):
            x = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x:
                binnen = not binnen
    return binnen


def afstand_km(lat: float, lon: float) -> float:
    """Kortste afstand (km) tot de Benelux; 0 boven Benelux-land."""
    ringen, rlats, rlons = _geometrie()
    if any(_binnen(r, lon, lat) for r in ringen):
        return 0.0
    p, l = math.radians(lat), math.radians(lon)
    a = np.sin((rlats - p) / 2) ** 2 + math.cos(p) * np.cos(rlats) * np.sin((rlons - l) / 2) ** 2
    return float(2 * AARDSTRAAL_KM * np.arcsin(np.sqrt(np.minimum(1.0, a.min()))))


def zwaartepunt() -> tuple[float, float]:
    """(lat, lon): gemiddelde van de randpunten; ligt binnen de Benelux."""
    _, rlats, rlons = _geometrie()
    return float(np.degrees(rlats.mean())), float(np.degrees(rlons.mean()))


def zone_ring(straal_km: float, stappen: int = 180) -> list[list[float]]:
    """
    Rand van het gebied binnen `straal_km` van de Benelux, als gesloten ring [lon, lat].
    Per richting vanuit het zwaartepunt wordt het punt gezocht waar de afstand precies
    `straal_km` is (bisectie); de buffer van de Benelux is vanuit dat punt stervormig.
    """
    from storm_tracker.tracker import punt_op_afstand

    clat, clon = zwaartepunt()
    ring = []
    for k in range(stappen):
        koers = 360.0 * k / stappen
        laag, hoog = 0.0, straal_km + 600.0
        for _ in range(28):
            midden = (laag + hoog) / 2
            if afstand_km(*punt_op_afstand(clat, clon, koers, midden)) < straal_km:
                laag = midden
            else:
                hoog = midden
        lat, lon = punt_op_afstand(clat, clon, koers, laag)
        ring.append([round(lon, 2), round(lat, 2)])
    ring.append(ring[0])
    return ring
