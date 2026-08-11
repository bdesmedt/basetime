"""
FastAPI-app: serveert het KPI-dashboard (één HTML-pagina) en een JSON-API die de
cijfers live uit Odoo haalt (met een korte cache, zie config.CACHE_TTL_SECONDS).

De hele site zit achter HTTP basic-auth (gebruikersnaam/wachtwoord uit environment
variables) — zie config.py en README.md.
"""

from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import config, kpis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("basetime-dashboard")

app = FastAPI(title="Basetime KPI-dashboard")
security = HTTPBasic()

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "dashboard.html"
_DASHBOARD_HTML = _TEMPLATE_PATH.read_text(encoding="utf-8")

_cache: dict = {"data": None, "fetched_at": 0.0, "error": None}
_detail_cache: dict[str, dict] = {}


def check_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    user_ok = secrets.compare_digest(credentials.username, config.DASHBOARD_USER)
    pass_ok = secrets.compare_digest(credentials.password, config.DASHBOARD_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Onjuiste gebruikersnaam of wachtwoord.",
            headers={"WWW-Authenticate": "Basic"},
        )


@app.get("/healthz")
def healthz():
    """Onbeveiligde health-check voor Railway — geeft geen bedrijfsdata terug."""
    return {"status": "ok"}


def _refresh_cache() -> dict:
    data = kpis.build_dashboard_payload()
    _cache["data"] = data
    _cache["fetched_at"] = time.time()
    _cache["error"] = None
    return data


@app.get("/api/kpis")
def api_kpis(refresh: bool = Query(False), _auth: None = Depends(check_auth)):
    age = time.time() - _cache["fetched_at"]
    stale = _cache["data"] is None or age > config.CACHE_TTL_SECONDS
    if refresh or stale:
        try:
            data = _refresh_cache()
        except Exception as exc:  # Odoo onbereikbaar, verkeerde inloggegevens, etc.
            logger.exception("Kon KPI-data niet ophalen uit Odoo")
            _cache["error"] = str(exc)
            if _cache["data"] is not None:
                # geef de laatst bekende cijfers terug, met een duidelijke waarschuwing
                stale_payload = dict(_cache["data"])
                stale_payload["stale_error"] = str(exc)
                return JSONResponse(stale_payload)
            raise HTTPException(status_code=502, detail=f"Kon geen data uit Odoo ophalen: {exc}")
        return data
    return _cache["data"]


@app.get("/api/details/{key}")
def api_details(key: str, refresh: bool = Query(False), _auth: None = Depends(check_auth)):
    """Volledige (niet-ingekorte) lijst voor de 'Bekijk alle' doorklik-knoppen op het
    dashboard — zelfde cache-aanpak als /api/kpis, maar per sectie (key) apart."""
    entry = _detail_cache.get(key, {"data": None, "fetched_at": 0.0})
    age = time.time() - entry["fetched_at"]
    stale = entry["data"] is None or age > config.CACHE_TTL_SECONDS
    if refresh or stale:
        try:
            data = kpis.build_detail_payload(key)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Onbekende detail-sectie: {key}")
        except Exception as exc:
            logger.exception("Kon detaildata niet ophalen uit Odoo (%s)", key)
            if entry["data"] is not None:
                # geef de laatst bekende detaildata terug in plaats van een harde fout,
                # zelfde aanpak als /api/kpis bij een tijdelijke Odoo-hik
                return entry["data"]
            raise HTTPException(status_code=502, detail=f"Kon geen detaildata ophalen: {exc}")
        entry = {"data": data, "fetched_at": time.time()}
        _detail_cache[key] = entry
    return entry["data"]


@app.get("/", response_class=HTMLResponse)
def dashboard(_auth: None = Depends(check_auth)):
    return HTMLResponse(_DASHBOARD_HTML)
