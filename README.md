# Weerfocus Stormtracker

Volgt depressies in de Noord-Atlantische Oceaan en Europa (70°W–40°O, 30°N–75°N) in de
zeeniveaudruk (MSLP) van GFS en ECMWF IFS, en publiceert tracks voor de module Stormtracker
(`js/modules/synoptiek.js` in het dashboard). Draait als systemd-service
`weerfocus-stormtracker` in `/root/weerfocus-stormtracker`, zoals de radar- en satellietworker.

## Invoer
| Model | Bron | Parameter | Stappen | Runs |
|---|---|---|---|---|
| GFS 0.25° | NOAA Open Data op AWS (`noaa-gfs-bdp-pds`), `.idx` + byte-range | `PRMSL:mean sea level` | f000–f144 elke 6 uur | 00/06/12/18 UTC |
| ECMWF IFS 0.25° | IFS open data (Google Cloud-spiegel), `.index` + byte-range | `msl` | 0–144 h elke 6 uur | 00/12 UTC (alleen die lopen tot +144 h) |

Orografie (`HGT:surface` uit GFS f000) wordt één keer opgehaald en in `toestand/orografie.npz` bewaard.

Een run telt pas als **alle** 25 indexbestanden er zijn. Er wordt per stap één GRIB-bericht
in het geheugen gelezen, uitgesneden en direct omgezet naar minima; er komen geen GRIB-bestanden
op schijf.

## Algoritme (`tracker.py`)
1. MSLP gaussisch afvlakken (σ = 1,5 roosterpunt).
2. Lokale minima met `minimum_filter` (venster 9×9). Een minimum telt als het afgevlakte veld
   gemiddeld op een ring van 500 km minstens 2 hPa hoger ligt. Kern verfijnd op het onbewerkte
   veld; minima binnen 300 km samengevoegd (de diepste blijft); minima binnen 1,5° van de
   domeinrand en boven terrein > 1000 m vervallen.
3. Koppelen over de tijd: nearest-neighbour vanaf de geëxtrapoleerde positie (laatste
   verplaatsing herhaald), hoogstens 650 km van de vorige positie per 6 uur, één-op-één.
4. Tracks korter dan 24 uur, of nooit dieper dan 1005 hPa, vervallen.
5. Per punt `pressure_hpa`, `speed_kmh` (centraal verschil) en `deepening_24h`
   (kerndruk 24 uur eerder min nu, positief = gedaald; `null` zonder 24 uur historie,
   aangevuld uit eerdere analyses in `history.json`). De Bergeron-norm staat als constante
   (`BERGERON_HPA_PER_24H_60N`, `bergeron_drempel_hpa()`), maar wordt niet in de output gebruikt.
6. Storm-id's: een nieuwe track krijgt het id van een storm uit de vorige publicatie (en, voor
   IFS, van de GFS-track van nu) als de positie op het **eerste gemeenschappelijke
   geldigheidstijdstip** binnen 300 km ligt (`STORM_ID_STRAAL_KM`). Anders een nieuw id
   `{JJJJMMDDUU}-{nn}`.
7. Historie: de f000-positie van elke verwerkte run komt per storm in `history.json`
   (7 dagen bewaard).

Elke combinatie van runs (`gfs:…|ifs:…`) wordt één keer verwerkt. Een nieuwe IFS-run bij
dezelfde GFS-run telt als nieuwe combinatie (GFS komt dan uit het geheugen). De toestand
(`toestand.json`, `register.json`, `history.json`) staat in `toestand/`; wordt die
gewist, dan begint de historie opnieuw en krijgen stormen nieuwe id's.

## Datacontract (Bunny, zone van `weerfocus-synoptiek.b-cdn.net`)
```
synoptiek/storms/latest/manifest.json
synoptiek/storms/latest/summary.json
synoptiek/storms/latest/history.json
synoptiek/storms/latest/gfs/tracks.json
synoptiek/storms/latest/ifs/tracks.json
synoptiek/storms/runs/{JJJJMMDDUU}/…      # zelfde bestanden, 7 dagen bewaard
```
`synoptiek/latest/` (de oude analyse) wordt niet aangeraakt. Volgorde van publiceren: eerst alles
naar `runs/{run}/`, dan naar `latest/`, met `manifest.json` steeds als laatste. Mislukt één
upload, dan stopt de publicatie en volgt de volgende ronde opnieuw.

**manifest.json**
```json
{
  "run": "2026100506-ifs2026100500",
  "updated_at": "2026-10-05T14:14:46Z",
  "models": [
    {"id": "gfs", "naam": "GFS 0.25°", "run": "2026100506", "bron": "…", "licentie": "Publiek domein (U.S. Government Work)"},
    {"id": "ifs", "naam": "ECMWF IFS 0.25°", "run": "2026100500", "bron": "ECMWF IFS open data", "licentie": "CC-BY-4.0", "attributie": "…"}
  ],
  "steps_h": [0, 6, 12, "…", 144],
  "source": "…",
  "license": "GFS 0.25°: Publiek domein (U.S. Government Work); ECMWF IFS 0.25°: CC-BY-4.0"
}
```
`run` is de publicatiesleutel: de nieuwste run (JJJJMMDDUU), met `-ifs{run}` erachter als de
IFS-run ouder is. Verandert `run`, dan zijn er nieuwe tracks. `models` bevat alleen de modellen
in deze publicatie; elk model heeft zijn eigen run.

**{model}/tracks.json**: GeoJSON FeatureCollection (WGS84, lon/lat op 2 decimalen).
- `kind: "track"`, LineString: `storm_id`, `segment: "history" | "forecast"`. De historielijn
  loopt door tot het eerste verwachtingspunt.
- `kind: "point"`, Point: `storm_id`, `valid_time` (ISO, UTC), `lead_h`, `pressure_hpa`,
  `deepening_24h` (of `null`), `speed_kmh`.

**summary.json**
```json
{"run": "2026100506", "nl_radius_km": 500, "storms": [
  {"id": "2026100500-10", "name": null, "first_seen": "2026-10-05T18:00:00Z",
   "min_pressure_hpa": 1000.5, "min_pressure_time": "…", "closest_nl_km": 210, "closest_nl_time": "…",
   "max_deepening_24h_hpa": 8.2, "relevant_nl": true,
   "models": ["gfs", "ifs"], "per_model": {"gfs": {"min_pressure_hpa": "…"}, "ifs": {"…": "…"}}}
]}
```
De kernvelden gaan over alle modellen samen; `per_model` heeft dezelfde velden per model.
`relevant_nl` = dichtste nadering tot het zwaartepunt van Nederland (5,29°O 52,13°N) ≤
`STORM_NL_STRAAL_KM`. Gesorteerd: relevant eerst, dan op afstand.

**history.json**: `{"updated_at", "storms": {"<id>": [{"time", "lon", "lat", "pressure_hpa", "model"}]}}`.

## Instellingen (`.env`, naast de worker; door systemd geladen met `EnvironmentFile`)
Verplicht:
```
STORM_BUNNY_SLEUTEL=<wachtwoord van de opslagzone achter weerfocus-synoptiek.b-cdn.net>
STORM_BUNNY_OPSLAGZONE=weerfocus-synoptiek    # naam van die opslagzone
STORM_BUNNY_REGIO=storage                     # storage/de = storage.bunnycdn.com; anders bv. uk, ny
```
Optioneel (standaard tussen haakjes): `STORM_NL_STRAAL_KM` (500), `STORM_ID_STRAAL_KM` (300),
`STORM_MAX_SPRONG_KM` (650), `STORM_MIN_DUUR_H` (24), `STORM_MIN_DIEPTE_HPA` (2),
`STORM_MAX_KERNDRUK_HPA` (1005), `STORM_OROGRAFIE_MAX_M` (1000), `STORM_CONTROLE_INTERVAL_S` (600),
`STORM_BEWAARDAGEN` (7), `STORM_GFS_BASIS_URL`, `STORM_IFS_BASIS_URL`, `STORM_MAP_TOESTAND` (`toestand/`).

## Installatie (VPS)
```bash
cd /root/weerfocus-stormtracker
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp deploy/weerfocus-stormtracker.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now weerfocus-stormtracker
journalctl -u weerfocus-stormtracker -f
```
De service heeft `Nice=10`, `MemoryMax=512M` en `CPUQuota=50%`, zodat radar en modellen voorgaan.
Gemeten (5 okt 2026): ~70 MB geheugen, ~7 s voor GFS en 10-20 s voor IFS per run, ~50 HTTP-verzoeken per model.

Handmatig (proefrun zonder upload, of een vaste run opnieuw verwerken):
```bash
set -a; . ./.env; set +a
venv/bin/python -m storm_tracker.worker --eenmalig --droog      # bestanden in toestand/droog/cdn/
venv/bin/python -m storm_tracker.worker --eenmalig --gfs-run 2026100500 --ifs-run 2026100500
```

## Tests
```bash
python -m unittest discover -s storm_tracker/tests -t .
```
De tests gebruiken synthetische MSLP-velden (Gauss-putten). Die bestaan alleen in de tests.
