# storm_tracker/worker.py
"""
Weerfocus Stormtracker - daemon.

Zoekt elke STORM_CONTROLE_INTERVAL_S seconden de nieuwste complete GFS- en IFS-run,
volgt daarin de depressies en publiceert het resultaat naar Bunny CDN onder
synoptiek/storms/. Elke combinatie van runs wordt één keer verwerkt (idempotent).

Service:   systemctl status weerfocus-stormtracker   (deploy/weerfocus-stormtracker.service)
Handmatig (in /root/weerfocus-stormtracker, met de variabelen uit .env):
           set -a; . ./.env; set +a
Starten:   venv/bin/python -m storm_tracker.worker
Eén ronde: python -m storm_tracker.worker --eenmalig
Vaste run: python -m storm_tracker.worker --eenmalig --gfs-run 2026100500 --ifs-run 2026100500
Proefrun:  python -m storm_tracker.worker --eenmalig --droog
           (uploadt niets; bestanden in toestand/droog/cdn/, eigen toestand in toestand/droog/)
"""

import argparse
import gc
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from storm_tracker import bronnen, bunny, uitvoer
from storm_tracker import instellingen as cfg
from storm_tracker.tracker import (
    Track,
    TrackerInstellingen,
    bereken_kenmerken,
    filter_tracks,
    ken_ids_toe,
    kopie,
    koppel_minima,
    zoek_minima,
)

log = logging.getLogger("stormtracker")

TRACKER_INSTELLINGEN = TrackerInstellingen(
    glad_sigma_punten=cfg.GLAD_SIGMA_PUNTEN,
    zoekvenster_punten=cfg.ZOEKVENSTER_PUNTEN,
    rand_marge_graden=cfg.RAND_MARGE_GRADEN,
    ring_straal_km=cfg.RING_STRAAL_KM,
    min_diepte_hpa=cfg.MIN_DIEPTE_HPA,
    samenvoeg_straal_km=cfg.SAMENVOEG_STRAAL_KM,
    max_sprong_km=cfg.MAX_SPRONG_KM,
    min_duur_h=cfg.MIN_DUUR_H,
    max_kerndruk_hpa=cfg.MAX_KERNDRUK_HPA,
    orografie_max_m=cfg.OROGRAFIE_MAX_M,
)


# ---------------------------------------------------------------------------
# Lokale toestand (map toestand/ naast de worker)
# ---------------------------------------------------------------------------
def _lees_json(pad: Path, standaard):
    try:
        with open(pad, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return standaard
    except (OSError, json.JSONDecodeError) as e:
        log.warning(f"{pad.name} onleesbaar ({e}); begin met een lege versie")
        return standaard


def _schrijf_json(pad: Path, data) -> None:
    tijdelijk = pad.with_suffix(pad.suffix + ".tmp")
    with open(tijdelijk, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tijdelijk, pad)


class StormWorker:
    def __init__(self, droog: bool = False, vaste_runs: Optional[dict[str, datetime]] = None):
        self.droog = droog
        self.vaste_runs = vaste_runs or {}
        # Een proefrun houdt een eigen toestand bij, zodat de echte niet verandert
        self.map = cfg.MAP_TOESTAND / "droog" if droog else cfg.MAP_TOESTAND
        self.map.mkdir(parents=True, exist_ok=True)
        self.pad_toestand = self.map / "toestand.json"
        self.pad_register = self.map / "register.json"
        self.pad_historie = self.map / "history.json"
        self.pad_orografie = self.map / "orografie.npz"
        self.orografie: Optional[bronnen.Orografie] = None
        # Ruwe tracks (zonder id) per model en run, zodat een nieuwe IFS-run de GFS-run niet opnieuw laat downloaden
        self.cache: dict[str, tuple[datetime, list[Track]]] = {}

    # -- orografie (statisch; één keer ophalen en lokaal bewaren) ------------
    def _laad_orografie(self, gfs_run: Optional[datetime]) -> bronnen.Orografie:
        if self.orografie is not None:
            return self.orografie
        if self.pad_orografie.exists():
            with np.load(self.pad_orografie) as d:
                self.orografie = bronnen.Orografie(d["hoogte"], d["lats"], d["lons"])
            return self.orografie
        if gfs_run is None:
            raise bronnen.Bronfout("Geen orografie: nog geen GFS-run beschikbaar om hem uit te halen")
        oro = bronnen.haal_gfs_orografie(gfs_run)
        np.savez_compressed(self.pad_orografie, hoogte=oro.hoogte, lats=oro.lats, lons=oro.lons)
        log.info(f"Orografie uit GFS {uitvoer.run_code(gfs_run)} opgeslagen")
        self.orografie = oro
        return oro

    # -- één model verwerken --------------------------------------------------
    def _volg_model(self, model: str, run: datetime) -> list[Track]:
        gecached = self.cache.get(model)
        if gecached and gecached[0] == run:
            return [kopie(t) for t in gecached[1]]

        haal = bronnen.MODELLEN[model]["haal_mslp"]
        stappen = []
        start = time.monotonic()
        for stap in cfg.STAPPEN_H:
            veld, lats, lons = haal(run, stap)
            # Meteen naar minima: het veld zelf is daarna niet meer nodig
            minima = zoek_minima(veld, lats, lons, TRACKER_INSTELLINGEN, self.orografie)
            stappen.append((run + timedelta(hours=stap), stap, minima))
            del veld
        tracks = filter_tracks(koppel_minima(stappen, TRACKER_INSTELLINGEN), TRACKER_INSTELLINGEN)
        for t in tracks:
            t.model = model
        log.info(f"{model.upper()} {uitvoer.run_code(run)}: {len(tracks)} depressies gevolgd "
                 f"({sum(len(s[2]) for s in stappen)} minima in {len(stappen)} stappen, {time.monotonic() - start:.0f} s)")
        self.cache[model] = (run, tracks)
        return [kopie(t) for t in tracks]

    # -- één ronde --------------------------------------------------------------
    def ronde(self) -> None:
        toestand = _lees_json(self.pad_toestand, {})
        gepubliceerd = toestand.get("runs", {})

        runs: dict[str, datetime] = {}
        if self.vaste_runs:
            runs = dict(self.vaste_runs)
        for model in ("gfs", "ifs"):
            if self.vaste_runs:
                break
            gevonden = bronnen.MODELLEN[model]["nieuwste_run"]()
            vorige = datetime.strptime(gepubliceerd[model], "%Y%m%d%H").replace(tzinfo=timezone.utc) if model in gepubliceerd else None
            # Nooit terug naar een oudere run dan al gepubliceerd (bijv. bij een tijdelijke storing van de bron)
            kandidaten = [r for r in (gevonden, vorige) if r is not None]
            if kandidaten:
                runs[model] = max(kandidaten)
        if not runs:
            log.info("Nog geen complete GFS- of IFS-run beschikbaar")
            return

        sleutel = "|".join(f"{m}:{uitvoer.run_code(r)}" for m, r in sorted(runs.items()))
        if sleutel == toestand.get("laatste_sleutel"):
            log.info(f"Niets nieuws ({sleutel})")
            return
        log.info(f"Verwerken: {sleutel}")

        self._laad_orografie(runs.get("gfs"))
        per_model = {m: self._volg_model(m, r) for m, r in sorted(runs.items())}
        nu = datetime.now(timezone.utc)
        publicatie = uitvoer.run_code(max(runs.values()))

        register = _lees_json(self.pad_register, {})
        historie = _lees_json(self.pad_historie, uitvoer.leeg_historie())
        vorige_tracks = uitvoer.toestand_naar_tracks(toestand.get("vorige_tracks", []))

        uitgegeven = set(register)

        def maak_id() -> str:
            n = 1
            while f"{publicatie}-{n:02d}" in uitgegeven:
                n += 1
            uitgegeven.add(f"{publicatie}-{n:02d}")
            return f"{publicatie}-{n:02d}"

        # Id's: GFS tegen de vorige publicatie, IFS tegen de vorige publicatie én de GFS van nu
        referentie = list(vorige_tracks)
        for model in ("gfs", "ifs"):
            if model not in per_model:
                continue
            tracks = per_model[model]
            ken_ids_toe(tracks, referentie, cfg.ID_STRAAL_KM, maak_id)
            for t in tracks:
                bereken_kenmerken(t, uitvoer.druk_historie(historie, t.storm_id))
            referentie = referentie + tracks

        alle_tracks = [t for ts in per_model.values() for t in ts]
        uitvoer.werk_register_bij(register, alle_tracks, max(runs.values()))
        uitvoer.snoei_register(register, nu)

        for model in ("gfs", "ifs"):
            if model in per_model:
                uitvoer.werk_historie_bij(historie, per_model[model], runs[model], model)
        uitvoer.snoei_historie(historie, nu)
        historie["updated_at"] = uitvoer.iso(nu)

        bestanden = {f"{m}/tracks.json": uitvoer.bouw_tracks_geojson(ts, historie, runs[m], m) for m, ts in per_model.items()}
        bestanden["summary.json"] = uitvoer.bouw_summary(per_model, register, max(runs.values()))
        bestanden["history.json"] = historie
        # De frontend herkent een nieuwe publicatie aan manifest.run; een nieuwe IFS-run bij
        # dezelfde GFS-run telt ook als nieuw, vandaar de volledige sleutel.
        bestanden["manifest.json"] = uitvoer.bouw_manifest(runs, sleutel_naar_run(sleutel, publicatie), nu)

        self._publiceer(publicatie, bestanden)

        toestand.update({
            "laatste_sleutel": sleutel,
            "runs": {m: uitvoer.run_code(r) for m, r in runs.items()},
            "vorige_tracks": uitvoer.tracks_naar_toestand(alle_tracks),
            "bijgewerkt": uitvoer.iso(nu),
        })
        _schrijf_json(self.pad_register, register)
        _schrijf_json(self.pad_historie, historie)
        _schrijf_json(self.pad_toestand, toestand)
        relevant = sum(1 for s in bestanden["summary.json"]["storms"] if s["relevant_nl"])
        log.info(f"{'Proefrun' if self.droog else 'Gepubliceerd'} {publicatie}: {len(bestanden['summary.json']['storms'])} stormen, {relevant} binnen {cfg.NL_STRAAL_KM:.0f} km van Nederland")

        if not self.droog:
            self._ruim_runs_op(nu)

    # -- publiceren -----------------------------------------------------------
    def _publiceer(self, publicatie: str, bestanden: dict[str, dict]) -> None:
        # Manifest steeds als laatste: pas als alles er staat, ziet de frontend de nieuwe run
        volgorde = [n for n in bestanden if n != "manifest.json"] + ["manifest.json"]
        if self.droog:
            doel = self.map / "cdn"
            for naam in volgorde:
                pad = doel / naam
                pad.parent.mkdir(parents=True, exist_ok=True)
                with open(pad, "w", encoding="utf-8") as f:
                    json.dump(bestanden[naam], f, ensure_ascii=False, indent=1)
            log.info(f"Proefrun: bestanden staan in {doel}, er is niets geüpload")
            return
        for naam in volgorde:
            bunny.upload_json(f"{cfg.CDN_MAP_RUNS}/{publicatie}/{naam}", bestanden[naam])
        for naam in volgorde:
            bunny.upload_json(f"{cfg.CDN_MAP_LATEST}/{naam}", bestanden[naam])

    def _ruim_runs_op(self, nu: datetime) -> None:
        grens = uitvoer.run_code(nu - timedelta(days=cfg.BEWAARDAGEN))
        try:
            for item in bunny.lijst_map(cfg.CDN_MAP_RUNS):
                naam = item.get("ObjectName", "")
                if item.get("IsDirectory") and len(naam) == 10 and naam.isdigit() and naam < grens:
                    bunny.verwijder_map(f"{cfg.CDN_MAP_RUNS}/{naam}")
                    log.info(f"Oude run {naam} van de CDN verwijderd")
        except Exception as e:
            log.warning(f"Opruimen van oude runs mislukt (volgende ronde opnieuw): {e}")


def sleutel_naar_run(sleutel: str, publicatie: str) -> str:
    """manifest.run: de publicatierun, met de IFS-run erachter als die afwijkt (bv. '2026100506' of '2026100506-ifs2026100500')."""
    delen = dict(d.split(":") for d in sleutel.split("|"))
    ifs = delen.get("ifs")
    return publicatie if ifs in (None, publicatie) else f"{publicatie}-ifs{ifs}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Weerfocus Stormtracker")
    parser.add_argument("--eenmalig", action="store_true", help="één ronde draaien en stoppen")
    parser.add_argument("--droog", action="store_true", help="niets uploaden; bestanden lokaal wegschrijven")
    parser.add_argument("--gfs-run", help="deze GFS-run verwerken (JJJJMMDDUU), alleen met --eenmalig")
    parser.add_argument("--ifs-run", help="deze IFS-run verwerken (JJJJMMDDUU), alleen met --eenmalig")
    args = parser.parse_args()
    vaste_runs = {
        m: datetime.strptime(r, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        for m, r in (("gfs", args.gfs_run), ("ifs", args.ifs_run)) if r
    }
    if vaste_runs and not args.eenmalig:
        parser.error("--gfs-run/--ifs-run alleen samen met --eenmalig")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] (StormTracker) %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    if not cfg.BUNNY_SLEUTEL and not args.droog:
        log.error("STORM_BUNNY_SLEUTEL ontbreekt in de omgeving; zonder sleutel kan er niets naar Bunny")
        sys.exit(1)

    worker = StormWorker(droog=args.droog, vaste_runs=vaste_runs)
    log.info(f"Stormtracker gestart (zone {cfg.BUNNY_OPSLAGZONE}, controle elke {cfg.CONTROLE_INTERVAL_S} s)")
    while True:
        try:
            worker.ronde()
        except Exception as e:
            log.error(f"Fout in ronde: {e}", exc_info=True)
            if args.eenmalig:
                sys.exit(1)
        gc.collect()
        if args.eenmalig:
            return
        time.sleep(cfg.CONTROLE_INTERVAL_S)


if __name__ == "__main__":
    main()
