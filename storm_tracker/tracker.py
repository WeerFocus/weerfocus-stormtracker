# storm_tracker/tracker.py
"""
Opsporen en volgen van depressies in MSLP-velden. Zuiver rekenwerk: geen netwerk,
geen bestanden, zodat het met synthetische velden te testen is.

Roosterconventie: `lats` en `lons` zijn 1D en oplopend, het veld heeft vorm (len(lats), len(lons)).
"""

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Callable, Optional

import numpy as np
from scipy import ndimage

AARDSTRAAL_KM = 6371.0

# Bergeron-norm voor explosieve verdieping (Sanders & Gyakum, 1980): 24 hPa in 24 uur op
# 60°N, gecorrigeerd met sin(φ)/sin(60°). Vastgelegd als referentie; de output gebruikt
# hem bewust niet als categorie.
BERGERON_HPA_PER_24H_60N = 24.0


def bergeron_drempel_hpa(lat: float) -> float:
    """Drempel voor explosieve verdieping (hPa per 24 uur) op breedtegraad `lat`."""
    return BERGERON_HPA_PER_24H_60N * abs(math.sin(math.radians(lat))) / math.sin(math.radians(60.0))


def afstand_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Grootcirkelafstand (haversine) in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * AARDSTRAAL_KM * math.asin(min(1.0, math.sqrt(a)))


def punt_op_afstand(lat: float, lon: float, koers_graden: float, km: float) -> tuple[float, float]:
    """Eindpunt na `km` over een grootcirkel vanaf (lat, lon) in richting `koers_graden`."""
    d = km / AARDSTRAAL_KM
    p1, l1, k = math.radians(lat), math.radians(lon), math.radians(koers_graden)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(k))
    l2 = l1 + math.atan2(math.sin(k) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


@dataclass
class TrackerInstellingen:
    glad_sigma_punten: float = 1.5
    zoekvenster_punten: int = 9
    rand_marge_graden: float = 1.5
    ring_straal_km: float = 500.0
    min_diepte_hpa: float = 2.0
    samenvoeg_straal_km: float = 300.0
    max_sprong_km: float = 650.0
    stap_h: int = 6
    min_duur_h: float = 24.0
    max_kerndruk_hpa: float = 1005.0
    orografie_max_m: float = 1000.0


@dataclass
class Minimum:
    lat: float
    lon: float
    druk_hpa: float
    diepte_hpa: float


@dataclass
class TrackPunt:
    tijd: datetime          # geldigheidstijd (UTC)
    lead_h: int
    lat: float
    lon: float
    druk_hpa: float
    snelheid_kmh: Optional[float] = None
    verdieping_24h: Optional[float] = None  # hPa; positief = kerndruk gedaald


@dataclass
class Track:
    punten: list[TrackPunt] = field(default_factory=list)
    storm_id: Optional[str] = None
    model: Optional[str] = None

    @property
    def duur_h(self) -> float:
        if len(self.punten) < 2:
            return 0.0
        return (self.punten[-1].tijd - self.punten[0].tijd).total_seconds() / 3600.0

    def punt_op(self, tijd: datetime) -> Optional[TrackPunt]:
        for p in self.punten:
            if p.tijd == tijd:
                return p
        return None


# ---------------------------------------------------------------------------
# 1 + 2. Afvlakken en minima zoeken
# ---------------------------------------------------------------------------
def _index_dichtstbij(waarden: np.ndarray, x: float) -> Optional[int]:
    if x < waarden[0] - 1e-9 or x > waarden[-1] + 1e-9:
        return None
    return int(np.abs(waarden - x).argmin())


def _parabooltop(links: float, midden: float, rechts: float) -> float:
    """Verschuiving (in roosterstappen, -0,5..0,5) van de top van de parabool door drie punten."""
    noemer = float(links) - 2.0 * float(midden) + float(rechts)
    if noemer <= 0:
        return 0.0
    return max(-0.5, min(0.5, 0.5 * (float(links) - float(rechts)) / noemer))


def zoek_minima(
    mslp_hpa: np.ndarray,
    lats: np.ndarray,
    lons: np.ndarray,
    inst: TrackerInstellingen,
    orografie: Optional[Callable[[float, float], Optional[float]]] = None,
) -> list[Minimum]:
    """Lokale minima met voldoende diepte, samengevoegd binnen `samenvoeg_straal_km`."""
    glad = ndimage.gaussian_filter(mslp_hpa.astype(np.float64), sigma=inst.glad_sigma_punten, mode="nearest")
    laagste = ndimage.minimum_filter(glad, size=inst.zoekvenster_punten, mode="nearest")
    hoogste = ndimage.maximum_filter(glad, size=inst.zoekvenster_punten, mode="nearest")
    # Vlakke stukken (geen drukverschil in het venster) zijn geen minimum
    kandidaten = np.argwhere((glad <= laagste + 1e-9) & (hoogste - glad > 0.01))

    ny, nx = glad.shape
    kandidaat_minima: list[Minimum] = []
    for j, i in kandidaten:
        lat, lon = float(lats[j]), float(lons[i])
        # Minima aan de rand van het domein zijn meestal schijnminima (helling die het domein uitloopt)
        if (lat - lats[0] < inst.rand_marge_graden or lats[-1] - lat < inst.rand_marge_graden
                or lon - lons[0] < inst.rand_marge_graden or lons[-1] - lon < inst.rand_marge_graden):
            continue

        # Omgevingsdruk: gemiddelde van het afgevlakte veld op een ring rond het minimum
        ring = []
        for koers in (k * 22.5 for k in range(16)):
            rlat, rlon = punt_op_afstand(lat, lon, koers, inst.ring_straal_km)
            rj, ri = _index_dichtstbij(lats, rlat), _index_dichtstbij(lons, rlon)
            if rj is not None and ri is not None:
                ring.append(glad[rj, ri])
        if len(ring) < 8:
            continue
        diepte = float(np.mean(ring) - glad[j, i])
        if diepte < inst.min_diepte_hpa:
            continue

        # Kern verfijnen op het onbewerkte veld (afvlakken maakt de kern ondieper)
        j0, j1 = max(0, j - 2), min(ny, j + 3)
        i0, i1 = max(0, i - 2), min(nx, i + 3)
        blok = mslp_hpa[j0:j1, i0:i1]
        bj, bi = np.unravel_index(int(np.argmin(blok)), blok.shape)
        kj, ki = j0 + bj, i0 + bi
        klat, klon = float(lats[kj]), float(lons[ki])
        # Positie tussen de roosterpunten: top van een parabool door de buren (anders trapjes van 0,25°)
        if 0 < kj < ny - 1:
            klat += _parabooltop(mslp_hpa[kj - 1, ki], mslp_hpa[kj, ki], mslp_hpa[kj + 1, ki]) * float(lats[1] - lats[0])
        if 0 < ki < nx - 1:
            klon += _parabooltop(mslp_hpa[kj, ki - 1], mslp_hpa[kj, ki], mslp_hpa[kj, ki + 1]) * float(lons[1] - lons[0])

        if orografie is not None:
            hoogte = orografie(klat, klon)
            if hoogte is not None and hoogte > inst.orografie_max_m:
                continue

        kandidaat_minima.append(Minimum(klat, klon, float(mslp_hpa[kj, ki]), diepte))

    # Dichtbij elkaar liggende minima samenvoegen: de diepste blijft
    kandidaat_minima.sort(key=lambda m: m.druk_hpa)
    behouden: list[Minimum] = []
    for m in kandidaat_minima:
        if all(afstand_km(m.lat, m.lon, b.lat, b.lon) > inst.samenvoeg_straal_km for b in behouden):
            behouden.append(m)
    return behouden


# ---------------------------------------------------------------------------
# 3. Minima over de tijdstappen koppelen
# ---------------------------------------------------------------------------
def _verwachte_positie(track: Track) -> tuple[float, float]:
    laatste = track.punten[-1]
    if len(track.punten) < 2:
        return laatste.lat, laatste.lon
    vorige = track.punten[-2]
    dlon = (laatste.lon - vorige.lon + 540.0) % 360.0 - 180.0
    return laatste.lat + (laatste.lat - vorige.lat), laatste.lon + dlon


def koppel_minima(stappen: list[tuple[datetime, int, list[Minimum]]], inst: TrackerInstellingen) -> list[Track]:
    """
    Nearest-neighbour met verwachte verplaatsing. `stappen` is een lijst (tijd, lead_h, minima),
    oplopend in tijd. Een track loopt door zolang er binnen `max_sprong_km` van zijn laatste
    positie een minimum is; bij meerdere kandidaten wint het minimum het dichtst bij de
    geëxtrapoleerde positie, één-op-één over alle tracks.
    """
    afgesloten: list[Track] = []
    actief: list[Track] = []

    for tijd, lead_h, minima in stappen:
        paren = []
        for ti, track in enumerate(actief):
            laatste = track.punten[-1]
            vlat, vlon = _verwachte_positie(track)
            for mi, m in enumerate(minima):
                if afstand_km(laatste.lat, laatste.lon, m.lat, m.lon) > inst.max_sprong_km:
                    continue
                paren.append((afstand_km(vlat, vlon, m.lat, m.lon), ti, mi))
        paren.sort()

        gebruikte_tracks, gebruikte_minima = set(), set()
        for _, ti, mi in paren:
            if ti in gebruikte_tracks or mi in gebruikte_minima:
                continue
            gebruikte_tracks.add(ti)
            gebruikte_minima.add(mi)
            m = minima[mi]
            actief[ti].punten.append(TrackPunt(tijd, lead_h, m.lat, m.lon, m.druk_hpa))

        nieuw_actief = []
        for ti, track in enumerate(actief):
            (nieuw_actief if ti in gebruikte_tracks else afgesloten).append(track)
        for mi, m in enumerate(minima):
            if mi not in gebruikte_minima:
                nieuw_actief.append(Track(punten=[TrackPunt(tijd, lead_h, m.lat, m.lon, m.druk_hpa)]))
        actief = nieuw_actief

    return afgesloten + actief


# ---------------------------------------------------------------------------
# 4 + 5. Filteren en kenmerken per punt
# ---------------------------------------------------------------------------
def bereken_kenmerken(track: Track, druk_historie: Optional[dict[datetime, float]] = None) -> None:
    """
    Snelheid (km/h) en verdieping over 24 uur (hPa, positief = kerndruk gedaald) per punt.
    `druk_historie` (tijd → kerndruk uit eerdere analyses) vult de verdieping aan voor
    punten waarvan de track zelf nog geen 24 uur terug gaat; anders blijft die null.
    """
    punten = track.punten
    for k, p in enumerate(punten):
        if len(punten) < 2:
            p.snelheid_kmh = None
        else:
            a = punten[k - 1] if k > 0 else p
            b = punten[k + 1] if k < len(punten) - 1 else p
            uren = (b.tijd - a.tijd).total_seconds() / 3600.0
            p.snelheid_kmh = round(afstand_km(a.lat, a.lon, b.lat, b.lon) / uren, 1) if uren > 0 else None

        eerder = p.tijd - timedelta(hours=24)
        vorige = track.punt_op(eerder)
        druk_eerder = vorige.druk_hpa if vorige else (druk_historie or {}).get(eerder)
        p.verdieping_24h = round(druk_eerder - p.druk_hpa, 1) if druk_eerder is not None else None


def filter_tracks(tracks: list[Track], inst: TrackerInstellingen) -> list[Track]:
    return [
        t for t in tracks
        if t.duur_h >= inst.min_duur_h and min(p.druk_hpa for p in t.punten) <= inst.max_kerndruk_hpa
    ]


def volg_depressies(
    velden: list[tuple[datetime, int, np.ndarray]],
    lats: np.ndarray,
    lons: np.ndarray,
    inst: TrackerInstellingen,
    orografie: Optional[Callable[[float, float], Optional[float]]] = None,
) -> list[Track]:
    """Volledige keten voor één modelrun: minima → koppelen → filteren → kenmerken."""
    stappen = [(tijd, lead_h, zoek_minima(veld, lats, lons, inst, orografie)) for tijd, lead_h, veld in velden]
    tracks = filter_tracks(koppel_minima(stappen, inst), inst)
    for t in tracks:
        bereken_kenmerken(t)
    return tracks


# ---------------------------------------------------------------------------
# 6. Stabiele storm-id's over runs (en modellen) heen
# ---------------------------------------------------------------------------
ID_VERGELIJK_TIJDSTIPPEN = 1  # alleen het eerste gemeenschappelijke tijdstip; daarna lopen runs en modellen uiteen


def _gemiddelde_afstand(a: Track, b: Track) -> Optional[float]:
    """Afstand op het eerste gemeenschappelijke geldigheidstijdstip (gemiddeld als er meer meetellen), of None zonder overlap."""
    afstanden = []
    for p in a.punten:
        q = b.punt_op(p.tijd)
        if q is not None:
            afstanden.append(afstand_km(p.lat, p.lon, q.lat, q.lon))
            if len(afstanden) == ID_VERGELIJK_TIJDSTIPPEN:
                break
    return sum(afstanden) / len(afstanden) if afstanden else None


def ken_ids_toe(
    nieuwe_tracks: list[Track],
    referentie: list[Track],
    straal_km: float,
    maak_id: Callable[[], str],
) -> None:
    """
    Geeft elke nieuwe track het id van de storm uit `referentie` (tracks met id, bijv. de
    vorige run) waarvan de positie op dezelfde geldigheidstijdstippen gemiddeld binnen
    `straal_km` ligt. Elk id gaat naar hoogstens één nieuwe track (de dichtstbijzijnde);
    zonder match krijgt de track een nieuw id.
    """
    beste_per_paar: dict[tuple[int, str], float] = {}
    for ni, nt in enumerate(nieuwe_tracks):
        for rt in referentie:
            if not rt.storm_id:
                continue
            d = _gemiddelde_afstand(nt, rt)
            if d is None or d > straal_km:
                continue
            sleutel = (ni, rt.storm_id)
            beste_per_paar[sleutel] = min(d, beste_per_paar.get(sleutel, math.inf))

    gebruikte_tracks, gebruikte_ids = set(), set()
    for (ni, sid), _ in sorted(beste_per_paar.items(), key=lambda kv: kv[1]):
        if ni in gebruikte_tracks or sid in gebruikte_ids:
            continue
        nieuwe_tracks[ni].storm_id = sid
        gebruikte_tracks.add(ni)
        gebruikte_ids.add(sid)

    for nt in nieuwe_tracks:
        if not nt.storm_id:
            nt.storm_id = maak_id()


def kopie(track: Track) -> Track:
    return Track(punten=[replace(p) for p in track.punten], storm_id=track.storm_id, model=track.model)
