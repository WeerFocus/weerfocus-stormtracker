# storm_tracker/bunny.py
"""Bunny Storage: uploaden (PUT met AccessKey, zoals de synoptiek-worker), mappen lezen en opruimen."""

import json
import logging
import time

import requests

from storm_tracker import instellingen as cfg

log = logging.getLogger("stormtracker")


class Uploadfout(Exception):
    pass


def _url(pad: str) -> str:
    return f"{cfg.BUNNY_BASIS_URL}/{pad.lstrip('/')}"


def upload_json(pad: str, data: dict, pogingen: int = 3) -> None:
    inhoud = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    kop = {"AccessKey": cfg.BUNNY_SLEUTEL, "Content-Type": "application/json"}
    for poging in range(1, pogingen + 1):
        try:
            resp = requests.put(_url(pad), data=inhoud, headers=kop, timeout=30)
            if resp.status_code in (200, 201):
                return
            fout = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.RequestException as e:
            fout = str(e)
        log.warning(f"Upload {pad} mislukt (poging {poging}/{pogingen}): {fout}")
        time.sleep(2 * poging)
    raise Uploadfout(f"Upload van {pad} definitief mislukt")


def lijst_map(pad: str) -> list[dict]:
    resp = requests.get(_url(pad.rstrip("/") + "/"), headers={"AccessKey": cfg.BUNNY_SLEUTEL}, timeout=30)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return resp.json()


def verwijder_map(pad: str) -> None:
    # Bunny verwijdert een map met alle inhoud als het pad op '/' eindigt
    resp = requests.delete(_url(pad.rstrip("/") + "/"), headers={"AccessKey": cfg.BUNNY_SLEUTEL}, timeout=60)
    if resp.status_code not in (200, 404):
        raise Uploadfout(f"Verwijderen van {pad} mislukt: HTTP {resp.status_code}")


def purge(paden: list[str]) -> None:
    """
    Leegt de edge-cache van de pull zone voor deze paden, zodat bezoekers meteen de nieuwe
    publicatie zien. Zonder account-API-sleutel alleen een waarschuwing.
    """
    if not cfg.BUNNY_API_SLEUTEL:
        log.warning("STORM_BUNNY_API_SLEUTEL ontbreekt: de cache van latest/ wordt niet gepurged")
        return
    for pad in paden:
        url = f"{cfg.PULLZONE_URL}/{pad.lstrip('/')}"
        try:
            resp = requests.get("https://api.bunny.net/purge", params={"url": url, "async": "false"},
                                headers={"AccessKey": cfg.BUNNY_API_SLEUTEL}, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning(f"Purgen van {url} mislukt: {e}")
