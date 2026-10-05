# storm_tracker/isobaren.py
"""Isobaren (MSLP-contouren) per tijdstap als compacte GeoJSON, voor de achtergrond van de kaart."""

import numpy as np
from scipy import ndimage

INTERVAL_HPA = 4
GLAD_SIGMA_PUNTEN = 1.5      # zelfde afvlakking als de tracker: geen ruis van het 0,25°-rooster
VEREENVOUDIG_GRADEN = 0.06   # Douglas-Peucker-tolerantie
MIN_PUNTEN = 4               # kortere stukjes (randeffecten) vervallen


def _vereenvoudig(punten: np.ndarray, tol: float) -> np.ndarray:
    """Douglas-Peucker, iteratief."""
    if len(punten) < 3:
        return punten
    houden = np.zeros(len(punten), dtype=bool)
    houden[0] = houden[-1] = True
    stapel = [(0, len(punten) - 1)]
    while stapel:
        a, b = stapel.pop()
        if b - a < 2:
            continue
        p, q = punten[a], punten[b]
        seg = q - p
        lengte = np.hypot(*seg)
        tussen = punten[a + 1:b]
        if lengte == 0:
            d = np.hypot(*(tussen - p).T)
        else:
            d = np.abs(seg[0] * (tussen[:, 1] - p[1]) - seg[1] * (tussen[:, 0] - p[0])) / lengte
        k = int(np.argmax(d))
        if d[k] > tol:
            m = a + 1 + k
            houden[m] = True
            stapel += [(a, m), (m, b)]
    return punten[houden]


def bouw_isobaren(mslp_hpa: np.ndarray, lats: np.ndarray, lons: np.ndarray) -> dict:
    import contourpy

    glad = ndimage.gaussian_filter(mslp_hpa.astype(np.float64), sigma=GLAD_SIGMA_PUNTEN, mode="nearest")
    gen = contourpy.contour_generator(lons, lats, glad, line_type=contourpy.LineType.Separate)
    laagste = int(np.floor(glad.min() / INTERVAL_HPA) * INTERVAL_HPA)
    hoogste = int(np.ceil(glad.max() / INTERVAL_HPA) * INTERVAL_HPA)

    features = []
    for niveau in range(laagste, hoogste + 1, INTERVAL_HPA):
        for lijn in gen.lines(niveau):
            lijn = _vereenvoudig(np.asarray(lijn), VEREENVOUDIG_GRADEN)
            if len(lijn) < MIN_PUNTEN:
                continue
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": np.round(lijn, 2).tolist()},
                "properties": {"hpa": niveau},
            })
    return {"type": "FeatureCollection", "features": features}
