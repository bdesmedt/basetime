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
from datetime import datetime
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import config, db, kpis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("basetime-dashboard")

app = FastAPI(title="Basetime KPI-dashboard")
security = HTTPBasic()

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "dashboard.html"
_DASHBOARD_HTML = _TEMPLATE_PATH.read_text(encoding="utf-8")

# Caches zijn gesleuteld op de gekozen periode: iemand die 12 maanden opvraagt mag niet
# de cijfers van een collega te zien krijgen die net 3 maanden koos.
_cache: dict[str, dict] = {}
_detail_cache: dict[str, dict] = {}
_inventory_cache: dict[str, dict] = {}
_pl_cache: dict[str, dict] = {}


def _parse_period(months: int | None, date_from: str | None, date_to: str | None) -> dict:
    """Zet de queryparameters om in argumenten voor kpis.resolve_windows(). Een
    onbruikbare datum levert een nette 400 op in plaats van een 500 verderop."""
    if date_from and date_to:
        try:
            return {
                "date_from": datetime.strptime(date_from, "%Y-%m-%d").date(),
                "date_to": datetime.strptime(date_to, "%Y-%m-%d").date(),
            }
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Ongeldige datum: gebruik het formaat JJJJ-MM-DD."
            )
    return {"months": months} if months else {}


def _period_key(period: dict) -> str:
    if "date_from" in period:
        return f"range:{period['date_from']}:{period['date_to']}"
    return f"months:{period.get('months') or config.MONTHS_LOOKBACK}"


def _cached(store: dict[str, dict], key: str, refresh: bool, build):
    """Gedeelde cachelogica: verse data als de cache koud is of ververst wordt, met
    terugval op de laatst bekende cijfers als Odoo er even uit ligt."""
    entry = store.get(key) or {"data": None, "fetched_at": 0.0}
    if refresh or entry["data"] is None or (time.time() - entry["fetched_at"]) > config.CACHE_TTL_SECONDS:
        try:
            data = build()
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Kon data niet ophalen uit Odoo (%s)", key)
            if entry["data"] is not None:
                stale = dict(entry["data"]) if isinstance(entry["data"], dict) else entry["data"]
                if isinstance(stale, dict):
                    stale["stale_error"] = str(exc)
                return stale
            raise HTTPException(status_code=502, detail=f"Kon geen data uit Odoo ophalen: {exc}")
        store[key] = {"data": data, "fetched_at": time.time()}
        return data
    return entry["data"]


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


@app.get("/api/kpis")
def api_kpis(
    refresh: bool = Query(False),
    months: int | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    _auth: None = Depends(check_auth),
):
    period = _parse_period(months, date_from, date_to)
    return _cached(
        _cache, _period_key(period), refresh, lambda: kpis.build_dashboard_payload(**period)
    )


@app.get("/api/inventory")
def api_inventory(
    refresh: bool = Query(False),
    months: int | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    _auth: None = Depends(check_auth),
):
    """Voorraadtab: eigen endpoint/cache, apart van /api/kpis — wordt pas opgehaald
    zodra de gebruiker de 'Voorraad'-tab voor het eerst opent."""
    period = _parse_period(months, date_from, date_to)
    return _cached(
        _inventory_cache, _period_key(period), refresh,
        lambda: kpis.build_inventory_payload(**period),
    )


@app.get("/api/details/{key}")
def api_details(
    key: str,
    refresh: bool = Query(False),
    months: int | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    _auth: None = Depends(check_auth),
):
    """Volledige (niet-ingekorte) lijst voor de 'Bekijk alle' doorklik-knoppen op het
    dashboard — zelfde cache-aanpak als /api/kpis, maar per sectie én periode apart."""
    if key not in kpis.DETAIL_FETCHERS:
        raise HTTPException(status_code=404, detail=f"Onbekende detail-sectie: {key}")
    period = _parse_period(months, date_from, date_to)
    return _cached(
        _detail_cache, f"{key}|{_period_key(period)}", refresh,
        lambda: kpis.build_detail_payload(key, period),
    )


@app.get("/api/pl")
def api_pl(
    refresh: bool = Query(False),
    months: int | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    _auth: None = Depends(check_auth),
):
    """Winst-en-verliesrekening tot op grootboekniveau. Eigen endpoint/cache; wordt pas
    opgehaald zodra de gebruiker de W&V-tab voor het eerst opent."""
    period = _parse_period(months, date_from, date_to)
    return _cached(
        _pl_cache, _period_key(period), refresh, lambda: kpis.build_pl_payload(**period)
    )


def _layout_changed() -> dict:
    """Na elke wijziging aan de indeling moet de W&V-cache leeg: die bevat de oude
    rubrieken. De andere caches (Odoo-cijfers) blijven staan — die veranderen niet."""
    _pl_cache.clear()
    return {"ok": True, "layout": kpis.layout_summary()}


def _storage_guard(fn):
    try:
        return fn()
    except db.StorageUnavailable as exc:
        # 503: het verzoek zelf klopt, alleen de opslag ontbreekt. De frontend zet dit
        # om in een nette melding in plaats van een stille mislukking.
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/pl/account")
def api_pl_move_account(
    payload: dict = Body(...),
    _auth: None = Depends(check_auth),
):
    """Verplaatst één grootboekrekening naar een andere rubriek. Een lege rubriek zet de
    rekening terug naar de indeling die Odoo zelf berekent."""
    code = str(payload.get("code") or "").strip()
    rubriek = str(payload.get("rubriek") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="Geef een rekeningcode mee.")
    if rubriek:
        _storage_guard(lambda: db.set_override(code, rubriek))
    else:
        _storage_guard(lambda: db.clear_override(code))
    return _layout_changed()


@app.post("/api/pl/rubriek")
def api_pl_add_rubriek(
    payload: dict = Body(...),
    _auth: None = Depends(check_auth),
):
    name = str(payload.get("name") or "").strip()
    parent = str(payload.get("parent") or "").strip()
    if not name or not parent:
        raise HTTPException(status_code=400, detail="Geef een naam en een bovenliggende rubriek.")
    if len(name) > 80:
        raise HTTPException(status_code=400, detail="Die naam is te lang (max. 80 tekens).")
    created = _storage_guard(lambda: db.add_rubriek(name, parent))
    return {**_layout_changed(), "rubriek": created}


@app.delete("/api/pl/rubriek/{rubriek_id}")
def api_pl_delete_rubriek(rubriek_id: str, _auth: None = Depends(check_auth)):
    """Verwijdert een zelf toegevoegde rubriek; de rekeningen erin gaan terug naar de
    rubriek die Odoo ze geeft."""
    if not db.is_custom(rubriek_id):
        raise HTTPException(
            status_code=400,
            detail="Alleen zelf toegevoegde rubrieken kunnen worden verwijderd; de "
                   "rubrieken uit het Odoo-rapport horen daar thuis.",
        )
    _storage_guard(lambda: db.delete_rubriek(rubriek_id))
    return _layout_changed()


@app.post("/api/pl/reset")
def api_pl_reset(_auth: None = Depends(check_auth)):
    """Alles terug naar de indeling van het Odoo-rapport."""
    _storage_guard(db.reset)
    return _layout_changed()


@app.get("/", response_class=HTMLResponse)
def dashboard(_auth: None = Depends(check_auth)):
    return HTMLResponse(_DASHBOARD_HTML)
