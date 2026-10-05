# storm_tracker/instellingen.py
"""
Instellingen van de stormtracker. Alles is te overschrijven met environment variables
(in .env naast de worker, geladen door systemd via EnvironmentFile); geheimen staan alleen
daar, nooit in de code.
"""

import os
from pathlib import Path


def _tekst(naam: str, standaard: str = "") -> str:
    return os.getenv(naam, standaard).strip().strip('"').strip("'")


def _getal(naam: str, standaard: float) -> float:
    waarde = _tekst(naam)
    return float(waarde) if waarde else float(standaard)


BASIS_MAP = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Bunny Storage (zelfde zone als de synoptiek-worker: weerfocus-synoptiek.b-cdn.net)
# ---------------------------------------------------------------------------
BUNNY_OPSLAGZONE = _tekst("STORM_BUNNY_OPSLAGZONE", "weerfocus-synoptiek")
BUNNY_REGIO = _tekst("STORM_BUNNY_REGIO", "de").lower()
BUNNY_SLEUTEL = _tekst("STORM_BUNNY_SLEUTEL")  # wachtwoord van de opslagzone, alleen via .env

if not BUNNY_REGIO or BUNNY_REGIO in ("de", "falkenstein", "main", "default", "storage"):
    BUNNY_HOST = "storage.bunnycdn.com"
else:
    BUNNY_HOST = f"{BUNNY_REGIO}.storage.bunnycdn.com"
BUNNY_BASIS_URL = f"https://{BUNNY_HOST}/{BUNNY_OPSLAGZONE}"

# Paden op de CDN; synoptiek/latest/ (de oude analyse) blijft onaangeroerd
CDN_MAP_LATEST = "synoptiek/storms/latest"
CDN_MAP_RUNS = "synoptiek/storms/runs"

# ---------------------------------------------------------------------------
# Bronnen
# ---------------------------------------------------------------------------
# GFS 0.25° via de NOAA Open Data-spiegel op AWS (zelfde bestanden als NOMADS, zonder
# verzoekenlimiet); byte-range op basis van de .idx-bestanden.
GFS_BASIS_URL = _tekst("STORM_GFS_BASIS_URL", "https://noaa-gfs-bdp-pds.s3.amazonaws.com").rstrip("/")
# ECMWF IFS open data via de Google Cloud-spiegel (zoals de modelpipeline; geen throttling).
# Alternatief met hetzelfde pad: https://data.ecmwf.int/forecasts
IFS_BASIS_URL = _tekst("STORM_IFS_BASIS_URL", "https://storage.googleapis.com/ecmwf-open-data").rstrip("/")

STAPPEN_H = list(range(0, 145, 6))  # f000 t/m f144, elke 6 uur

BRONNEN = {
    "gfs": {
        "naam": "GFS 0.25°",
        "bron": "NOAA NCEP Global Forecast System (GFS), via NOAA Open Data Dissemination",
        "licentie": "Publiek domein (U.S. Government Work)",
    },
    "ifs": {
        "naam": "ECMWF IFS 0.25°",
        "bron": "ECMWF IFS open data",
        "licentie": "CC-BY-4.0",
        "attributie": "Bevat gewijzigde ECMWF open data (CC-BY-4.0). ECMWF is niet verantwoordelijk voor fouten of omissies.",
    },
}

# Domein: Noord-Atlantische Oceaan tot Midden-Europa
DOMEIN = {
    "lat_min": 30.0,
    "lat_max": 75.0,
    "lon_min": -70.0,
    "lon_max": 40.0,
}

# Zwaartepunt van Nederland (lon, lat) voor de afstand tot Nederland
NL_ZWAARTEPUNT_LON = _getal("STORM_NL_LON", 5.29)
NL_ZWAARTEPUNT_LAT = _getal("STORM_NL_LAT", 52.13)
NL_STRAAL_KM = _getal("STORM_NL_STRAAL_KM", 500)

# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------
GLAD_SIGMA_PUNTEN = _getal("STORM_GLAD_SIGMA_PUNTEN", 1.5)       # gaussisch afvlakken, in roosterpunten (0,25°)
ZOEKVENSTER_PUNTEN = int(_getal("STORM_ZOEKVENSTER_PUNTEN", 9))  # venster van minimum_filter
RAND_MARGE_GRADEN = _getal("STORM_RAND_MARGE_GRADEN", 1.5)       # minima aan de domeinrand zijn schijnminima
RING_STRAAL_KM = _getal("STORM_RING_STRAAL_KM", 500)             # ring waarop de omgevingsdruk wordt gemeten
MIN_DIEPTE_HPA = _getal("STORM_MIN_DIEPTE_HPA", 2.0)             # minimaal drukverschil met die ring
SAMENVOEG_STRAAL_KM = _getal("STORM_SAMENVOEG_STRAAL_KM", 300)   # dichterbij elkaar = één minimum
MAX_SPRONG_KM = _getal("STORM_MAX_SPRONG_KM", 650)               # maximale verplaatsing per 6 uur
MIN_DUUR_H = _getal("STORM_MIN_DUUR_H", 24)                      # kortere tracks vervallen
MAX_KERNDRUK_HPA = _getal("STORM_MAX_KERNDRUK_HPA", 1005)        # track moet minstens één keer zo diep zijn
OROGRAFIE_MAX_M = _getal("STORM_OROGRAFIE_MAX_M", 1000)          # minima boven hoger terrein vervallen
ID_STRAAL_KM = _getal("STORM_ID_STRAAL_KM", 300)                 # koppeling aan een storm uit de vorige run

# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------
CONTROLE_INTERVAL_S = int(_getal("STORM_CONTROLE_INTERVAL_S", 600))
BEWAARDAGEN = int(_getal("STORM_BEWAARDAGEN", 7))
MAP_TOESTAND = Path(_tekst("STORM_MAP_TOESTAND", str(BASIS_MAP / "toestand")))
