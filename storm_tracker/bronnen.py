# storm_tracker/bronnen.py
"""
Ophalen van MSLP (en orografie) per modelstap, alleen de benodigde GRIB-berichten via
byte-ranges uit de index-bestanden. Alles blijft in het geheugen: geen tussenbestanden.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from storm_tracker import instellingen as cfg

log = logging.getLogger("stormtracker")


class Bronfout(Exception):
    """Een bestand of bericht ontbreekt of is onleesbaar."""


def _maak_sessie() -> requests.Session:
    sessie = requests.Session()
    herhaal = Retry(total=3, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET", "HEAD"))
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=herhaal)
    sessie.mount("https://", adapter)
    sessie.headers["User-Agent"] = "weerfocus-stormtracker/1.0"
    return sessie


sessie = _maak_sessie()


def _bestaat(url: str) -> bool:
    try:
        return sessie.head(url, timeout=20).status_code == 200
    except requests.RequestException:
        return False


def _haal_bytes(url: str, start: int, eind: Optional[int]) -> bytes:
    """Haalt een byte-range op (eind inclusief; None = tot het einde) en leest hem gestreamd in."""
    bereik = f"bytes={start}-{'' if eind is None else eind}"
    with sessie.get(url, headers={"Range": bereik}, timeout=60, stream=True) as resp:
        if resp.status_code != 206:
            raise Bronfout(f"Byte-range niet ondersteund of mislukt ({resp.status_code}): {url}")
        data = bytearray()
        for blok in resp.iter_content(chunk_size=256 * 1024):
            data.extend(blok)
    if not data.startswith(b"GRIB"):
        raise Bronfout(f"Geen GRIB-bericht op {bereik}: {url}")
    return bytes(data)


# ---------------------------------------------------------------------------
# GRIB decoderen en uitsnijden
# ---------------------------------------------------------------------------
def decodeer_grib(bericht: bytes) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Eén GRIB-bericht op een regular_ll-rooster → (waarden[nj, ni], lats, lons)."""
    import eccodes  # pas hier, zodat de tracker-tests geen ecCodes nodig hebben

    gid = eccodes.codes_new_from_message(bericht)
    try:
        roostertype = eccodes.codes_get(gid, "gridType")
        if roostertype != "regular_ll":
            raise Bronfout(f"Onverwacht roostertype: {roostertype}")
        ni = eccodes.codes_get(gid, "Ni")
        nj = eccodes.codes_get(gid, "Nj")
        lat1 = eccodes.codes_get(gid, "latitudeOfFirstGridPointInDegrees")
        lon1 = eccodes.codes_get(gid, "longitudeOfFirstGridPointInDegrees")
        di = eccodes.codes_get(gid, "iDirectionIncrementInDegrees")
        dj = eccodes.codes_get(gid, "jDirectionIncrementInDegrees")
        j_positief = eccodes.codes_get(gid, "jScansPositively")
        i_negatief = eccodes.codes_get(gid, "iScansNegatively")
        waarden = eccodes.codes_get_values(gid).reshape(nj, ni).astype(np.float32)
    finally:
        eccodes.codes_release(gid)

    lats = lat1 + np.arange(nj) * (dj if j_positief else -dj)
    lons = lon1 + np.arange(ni) * (-di if i_negatief else di)
    return waarden, lats, lons


def snijd_uit(waarden: np.ndarray, lats: np.ndarray, lons: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Snijdt het domein uit, met oplopende lats en lons in -180..180."""
    lons_180 = (lons + 180.0) % 360.0 - 180.0
    d = cfg.DOMEIN
    ji = np.where((lats >= d["lat_min"] - 1e-6) & (lats <= d["lat_max"] + 1e-6))[0]
    ii = np.where((lons_180 >= d["lon_min"] - 1e-6) & (lons_180 <= d["lon_max"] + 1e-6))[0]
    ji = ji[np.argsort(lats[ji])]
    ii = ii[np.argsort(lons_180[ii])]
    if len(ji) < 10 or len(ii) < 10:
        raise Bronfout("Domein valt buiten het rooster")
    uit = np.ascontiguousarray(waarden[np.ix_(ji, ii)])
    return uit, lats[ji].astype(np.float64), lons_180[ii].astype(np.float64)


class Orografie:
    """Terreinhoogte (m) per punt, op het eigen rooster van de bron."""

    def __init__(self, hoogte: np.ndarray, lats: np.ndarray, lons: np.ndarray):
        self.hoogte, self.lats, self.lons = hoogte, lats, lons

    def __call__(self, lat: float, lon: float) -> Optional[float]:
        if not (self.lats[0] <= lat <= self.lats[-1] and self.lons[0] <= lon <= self.lons[-1]):
            return None
        j = int(np.abs(self.lats - lat).argmin())
        i = int(np.abs(self.lons - lon).argmin())
        return float(self.hoogte[j, i])


# ---------------------------------------------------------------------------
# GFS (NOAA Open Data, .idx + byte-range)
# ---------------------------------------------------------------------------
def _gfs_url(run: datetime, stap: int) -> str:
    return (f"{cfg.GFS_BASIS_URL}/gfs.{run:%Y%m%d}/{run:%H}/atmos/"
            f"gfs.t{run:%H}z.pgrb2.0p25.f{stap:03d}")


def _gfs_bereik(run: datetime, stap: int, variabele: str, niveau: str) -> tuple[int, Optional[int]]:
    url = _gfs_url(run, stap) + ".idx"
    resp = sessie.get(url, timeout=30)
    if resp.status_code != 200:
        raise Bronfout(f"GFS-index ontbreekt ({resp.status_code}): {url}")
    regels = [r.split(":") for r in resp.text.strip().splitlines() if r.count(":") >= 5]
    for k, r in enumerate(regels):
        if r[3] == variabele and r[4] == niveau:
            start = int(r[1])
            eind = int(regels[k + 1][1]) - 1 if k + 1 < len(regels) else None
            return start, eind
    raise Bronfout(f"{variabele} op '{niveau}' niet in {url}")


def gfs_run_compleet(run: datetime) -> bool:
    # Eerst de laatste stap (goedkoop), dan alle stappen
    if not _bestaat(_gfs_url(run, cfg.STAPPEN_H[-1]) + ".idx"):
        return False
    return all(_bestaat(_gfs_url(run, s) + ".idx") for s in cfg.STAPPEN_H)


def haal_gfs_mslp(run: datetime, stap: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    start, eind = _gfs_bereik(run, stap, "PRMSL", "mean sea level")
    waarden, lats, lons = decodeer_grib(_haal_bytes(_gfs_url(run, stap), start, eind))
    uit, lats, lons = snijd_uit(waarden, lats, lons)
    return uit / 100.0, lats, lons  # Pa → hPa


def haal_gfs_orografie(run: datetime) -> Orografie:
    start, eind = _gfs_bereik(run, 0, "HGT", "surface")
    waarden, lats, lons = decodeer_grib(_haal_bytes(_gfs_url(run, 0), start, eind))
    return Orografie(*snijd_uit(waarden, lats, lons))


# ---------------------------------------------------------------------------
# ECMWF IFS open data (.index met JSON-regels + byte-range)
# ---------------------------------------------------------------------------
def _ifs_url(run: datetime, stap: int) -> str:
    # 00 en 12 UTC lopen in de stream 'oper' tot +240 h; 06 en 18 UTC ('scda') maar tot +90 h
    return (f"{cfg.IFS_BASIS_URL}/{run:%Y%m%d}/{run:%H}z/ifs/0p25/oper/"
            f"{run:%Y%m%d%H}0000-{stap}h-oper-fc")


def ifs_run_compleet(run: datetime) -> bool:
    if not _bestaat(_ifs_url(run, cfg.STAPPEN_H[-1]) + ".index"):
        return False
    return all(_bestaat(_ifs_url(run, s) + ".index") for s in cfg.STAPPEN_H)


def haal_ifs_mslp(run: datetime, stap: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    url = _ifs_url(run, stap)
    resp = sessie.get(url + ".index", timeout=30)
    if resp.status_code != 200:
        raise Bronfout(f"IFS-index ontbreekt ({resp.status_code}): {url}.index")
    for regel in resp.text.splitlines():
        if not regel.strip():
            continue
        item = json.loads(regel)
        if item.get("param") == "msl":
            start = int(item["_offset"])
            waarden, lats, lons = decodeer_grib(_haal_bytes(url + ".grib2", start, start + int(item["_length"]) - 1))
            uit, lats, lons = snijd_uit(waarden, lats, lons)
            return uit / 100.0, lats, lons
    raise Bronfout(f"msl niet in {url}.index")


# ---------------------------------------------------------------------------
# Nieuwste complete run zoeken
# ---------------------------------------------------------------------------
def _cycli(uren: tuple[int, ...], terug_h: int) -> list[datetime]:
    nu = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    kandidaten = []
    for h in range(0, terug_h + 1):
        t = nu - timedelta(hours=h)
        if t.hour in uren:
            kandidaten.append(t)
    return kandidaten  # nieuwste eerst


def nieuwste_gfs_run() -> Optional[datetime]:
    for run in _cycli((0, 6, 12, 18), 30):
        if gfs_run_compleet(run):
            return run
    return None


def nieuwste_ifs_run() -> Optional[datetime]:
    for run in _cycli((0, 12), 48):
        if ifs_run_compleet(run):
            return run
    return None


MODELLEN = {
    "gfs": {"nieuwste_run": nieuwste_gfs_run, "haal_mslp": haal_gfs_mslp},
    "ifs": {"nieuwste_run": nieuwste_ifs_run, "haal_mslp": haal_ifs_mslp},
}
