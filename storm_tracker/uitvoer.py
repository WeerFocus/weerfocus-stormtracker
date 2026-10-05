# storm_tracker/uitvoer.py
"""Opbouw van de CDN-bestanden: tracks.json (GeoJSON), summary.json en history.json."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from storm_tracker import benelux
from storm_tracker import instellingen as cfg
from storm_tracker.tracker import Track, afstand_km


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def lees_iso(tekst: str) -> datetime:
    return datetime.strptime(tekst, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def run_code(run: datetime) -> str:
    return f"{run:%Y%m%d%H}"


def _coord(lon: float, lat: float) -> list[float]:
    return [round(lon, 2), round(lat, 2)]


# ---------------------------------------------------------------------------
# Historie: geanalyseerde posities (f000) van opeenvolgende runs
# ---------------------------------------------------------------------------
def werk_historie_bij(historie: dict, tracks: list[Track], run: datetime, model: str) -> None:
    """Voegt per storm de f000-positie toe, als er voor dat tijdstip nog geen positie is."""
    stormen = historie.setdefault("storms", {})
    for t in tracks:
        p = t.punt_op(run)
        if p is None:
            continue
        reeks = stormen.setdefault(t.storm_id, [])
        if any(e["time"] == iso(run) for e in reeks):
            continue
        reeks.append({
            "time": iso(run),
            "lon": round(p.lon, 2),
            "lat": round(p.lat, 2),
            "pressure_hpa": round(p.druk_hpa, 1),
            "model": model,
        })
        reeks.sort(key=lambda e: e["time"])


def snoei_historie(historie: dict, nu: datetime) -> None:
    grens = iso(nu - timedelta(days=cfg.BEWAARDAGEN))
    stormen = historie.get("storms", {})
    for sid in list(stormen):
        stormen[sid] = [e for e in stormen[sid] if e["time"] >= grens]
        if not stormen[sid]:
            del stormen[sid]


def druk_historie(historie: dict, storm_id: str) -> dict[datetime, float]:
    return {lees_iso(e["time"]): e["pressure_hpa"] for e in historie.get("storms", {}).get(storm_id, [])}


# ---------------------------------------------------------------------------
# tracks.json per model
# ---------------------------------------------------------------------------
def bouw_tracks_geojson(tracks: list[Track], historie: dict, run: datetime, model: str) -> dict:
    features = []
    for t in tracks:
        eerdere = [e for e in historie.get("storms", {}).get(t.storm_id, []) if lees_iso(e["time"]) < run]
        if eerdere:
            # Doorgetrokken tot het eerste verwachtingspunt, zodat de lijn aansluit
            coords = [_coord(e["lon"], e["lat"]) for e in eerdere] + [_coord(t.punten[0].lon, t.punten[0].lat)]
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {"kind": "track", "storm_id": t.storm_id, "segment": "history"},
            })
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [_coord(p.lon, p.lat) for p in t.punten]},
            "properties": {"kind": "track", "storm_id": t.storm_id, "segment": "forecast"},
        })
        for p in t.punten:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": _coord(p.lon, p.lat)},
                "properties": {
                    "kind": "point",
                    "storm_id": t.storm_id,
                    "valid_time": iso(p.tijd),
                    "lead_h": p.lead_h,
                    "pressure_hpa": round(p.druk_hpa, 1),
                    "deepening_24h": p.verdieping_24h,
                    "speed_kmh": p.snelheid_kmh,
                    "benelux_km": round(benelux.afstand_km(p.lat, p.lon)),
                },
            })
    return {"type": "FeatureCollection", "model": model, "run": run_code(run), "features": features}


# ---------------------------------------------------------------------------
# summary.json
# ---------------------------------------------------------------------------
def _kenmerken(tracks: list[Track]) -> dict:
    punten = [p for t in tracks for p in t.punten]
    laagste = min(punten, key=lambda p: p.druk_hpa)
    afstanden = [(benelux.afstand_km(p.lat, p.lon), p) for p in punten]
    dichtst_km, dichtst = min(afstanden, key=lambda a: (a[0], a[1].tijd))
    verdiepingen = [p.verdieping_24h for p in punten if p.verdieping_24h is not None]
    return {
        "min_pressure_hpa": round(laagste.druk_hpa, 1),
        "min_pressure_time": iso(laagste.tijd),
        "closest_benelux_km": round(dichtst_km),
        "closest_benelux_time": iso(dichtst.tijd),
        "max_deepening_24h_hpa": round(max(verdiepingen), 1) if verdiepingen else None,
        "relevant_benelux": dichtst_km <= cfg.BENELUX_STRAAL_KM,
    }


def bouw_summary(per_model: dict[str, list[Track]], register: dict, run: datetime) -> dict:
    """
    Eén regel per storm. De kernvelden gaan over alle modellen samen (laagste druk,
    dichtste nadering tot de Benelux); `per_model` bevat dezelfde velden per model.
    """
    ids = sorted({t.storm_id for tracks in per_model.values() for t in tracks})
    stormen = []
    for sid in ids:
        tracks_per_model = {m: [t for t in tracks if t.storm_id == sid] for m, tracks in per_model.items()}
        tracks_per_model = {m: ts for m, ts in tracks_per_model.items() if ts}
        alle = [t for ts in tracks_per_model.values() for t in ts]
        regel = {
            "id": sid,
            "name": None,
            "first_seen": register[sid]["first_seen"],
            **_kenmerken(alle),
            "models": sorted(tracks_per_model),
            "per_model": {m: _kenmerken(ts) for m, ts in tracks_per_model.items()},
        }
        stormen.append(regel)
    stormen.sort(key=lambda s: (not s["relevant_benelux"], s["closest_benelux_km"]))
    return {"run": run_code(run), "benelux_radius_km": cfg.BENELUX_STRAAL_KM, "storms": stormen}


def werk_register_bij(register: dict, tracks: list[Track], run: datetime) -> None:
    """first_seen = vroegste bekende geldigheidstijd van de storm; nooit later dan eerder vastgelegd."""
    for t in tracks:
        eerste = t.punten[0].tijd
        regel = register.setdefault(t.storm_id, {"first_seen": iso(eerste), "last_run": run_code(run)})
        if iso(eerste) < regel["first_seen"]:
            regel["first_seen"] = iso(eerste)
        regel["last_run"] = max(regel["last_run"], run_code(run))


def snoei_register(register: dict, nu: datetime) -> None:
    grens = run_code(nu - timedelta(days=cfg.BEWAARDAGEN))
    for sid in [s for s, r in register.items() if r["last_run"] < grens]:
        del register[sid]


def bouw_benelux(straal_km: float) -> dict:
    """Zone binnen `straal_km` van de Benelux (Polygon), voor de kaart."""
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [benelux.zone_ring(straal_km)]},
        "properties": {"kind": "zone", "radius_km": straal_km},
    }]}


def bouw_manifest(runs: dict[str, datetime], publicatie: str, map_publicatie: str, nu: datetime) -> dict:
    modellen = []
    for m in ("gfs", "ifs"):
        if m in runs:
            bron = cfg.BRONNEN[m]
            modellen.append({
                "id": m,
                "naam": bron["naam"],
                "run": run_code(runs[m]),
                # Isobaren per stap: {isobaren}/{lead_h:03d}.json, relatief aan synoptiek/storms/
                "isobaren": f"runs/{map_publicatie}/{m}/isobaren",
                "bron": bron["bron"],
                "licentie": bron["licentie"],
                **({"attributie": bron["attributie"]} if "attributie" in bron else {}),
            })
    bron_tekst = "; ".join(cfg.BRONNEN[m]["bron"] for m in ("gfs", "ifs") if m in runs)
    licentie = "; ".join(f"{cfg.BRONNEN[m]['naam']}: {cfg.BRONNEN[m]['licentie']}" for m in ("gfs", "ifs") if m in runs)
    return {
        "run": publicatie,
        "updated_at": iso(nu),
        "models": modellen,
        "steps_h": cfg.STAPPEN_H,
        "source": bron_tekst,
        "license": licentie,
    }


def tracks_naar_toestand(tracks: list[Track]) -> list[dict]:
    """Compacte vorm van de tracks van deze run, als referentie voor de id's van de volgende run."""
    return [{
        "storm_id": t.storm_id,
        "model": t.model,
        "punten": [{"tijd": iso(p.tijd), "lat": p.lat, "lon": p.lon} for p in t.punten],
    } for t in tracks]


def toestand_naar_tracks(regels: list[dict]) -> list[Track]:
    from storm_tracker.tracker import TrackPunt
    return [Track(
        punten=[TrackPunt(lees_iso(p["tijd"]), 0, p["lat"], p["lon"], 0.0) for p in r["punten"]],
        storm_id=r["storm_id"],
        model=r.get("model"),
    ) for r in regels]


def leeg_historie() -> dict:
    return {"updated_at": None, "storms": {}}
