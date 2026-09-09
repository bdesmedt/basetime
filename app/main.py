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

from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import config, db, kpis, snapshot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("basetime-dashboard")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start bij het opstarten de planner die de standen vastlegt, en zet 'm bij het
    afsluiten weer stil. De planner draait mee in deze webserver, zodat er op Railway
    geen aparte cron-service nodig is (zie app/snapshot.py)."""
    snapshot.start_scheduler()
    try:
        yield
    finally:
        snapshot.stop_scheduler()


app = FastAPI(title="Basetime KPI-dashboard", lifespan=lifespan)
security = HTTPBasic()

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "dashboard.html"
_DASHBOARD_HTML = _TEMPLATE_PATH.read_text(encoding="utf-8")

# Caches zijn gesleuteld op de gekozen periode: iemand die 12 maanden opvraagt mag niet
# de cijfers van een collega te zien krijgen die net 3 maanden koos.
_cache: dict[str, dict] = {}
_detail_cache: dict[str, dict] = {}
_inventory_cache: dict[str, dict] = {}
_pl_cache: dict[str, dict] = {}
_pl_lines_cache: dict[str, dict] = {}
_cash_cache: dict[str, dict] = {}


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
    """Onbeveiligde health-check voor Railway — geeft geen bedrijfsdata terug.
    De twee vlaggen erbij zeggen alleen óf er een database is gekoppeld en óf de planner
    draait; geen cijfers, geen namen."""
    return {
        "status": "ok",
        "database": db.configured(),
        "snapshot_scheduler": snapshot.scheduler_running(),
    }


def _capture_snapshot(payload: dict) -> None:
    """Legt de standen van vandaag vast zodra er verse cijfers uit Odoo komen.

    Waarom hier en niet in een aparte taak: de payload is op dit moment toch al opgebouwd,
    dus het kost geen enkele extra Odoo-query. Er is óók een endpoint (/api/snapshot) voor
    een geplande taak, zodat de reeks blijft doorlopen als niemand het dashboard opent.

    Dit mag nooit een paginabezoek laten mislukken: gaat het schrijven mis, dan komt er een
    regel in de log en verder niets."""
    if not db.configured():
        return
    try:
        if db.snapshot_exists_today():
            return
        db.save_snapshot(kpis.snapshot_from_payload(payload))
        logger.info("Momentopname van vandaag vastgelegd.")
    except Exception as exc:
        logger.warning("Kon de momentopname niet vastleggen: %s", exc)


@app.get("/api/kpis")
def api_kpis(
    refresh: bool = Query(False),
    months: int | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    _auth: None = Depends(check_auth),
):
    period = _parse_period(months, date_from, date_to)

    def build():
        payload = kpis.build_dashboard_payload(**period)
        _capture_snapshot(payload)
        return payload

    return _cached(_cache, _period_key(period), refresh, build)


@app.get("/api/snapshots")
def api_snapshots(
    days: int = Query(365, ge=1, le=1825),
    _auth: None = Depends(check_auth),
):
    """De vastgelegde standen door de tijd. Komt uit de eigen database, niet uit Odoo —
    Odoo kan deze reeks niet reconstrueren."""
    return kpis.build_snapshot_series(days=days)


@app.post("/api/snapshot")
def api_snapshot_now(_auth: None = Depends(check_auth)):
    """Legt de standen van dit moment vast, ook als die van vandaag er al staat (die wordt
    dan overschreven). Normaal gesproken doet de ingebouwde planner dit vanzelf; dit
    endpoint is er om het handmatig af te dwingen of vanuit een externe planner."""
    if not db.configured():
        raise HTTPException(
            status_code=503,
            detail="Er is geen DATABASE_URL ingesteld, dus er valt niets vast te leggen.",
        )
    try:
        captured = snapshot.capture(force=True)
    except db.StorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"ok": True, "captured": captured}


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


@app.get("/api/pl/lines")
def api_pl_lines(
    codes: str = Query(..., description="Komma-gescheiden grootboekcodes"),
    date_from: str = Query(...),
    date_to: str = Query(...),
    refresh: bool = Query(False),
    _auth: None = Depends(check_auth),
):
    """De boekingsregels achter één bedrag op de W&V-tab. `date_to` is exclusief, net als
    in de rest van het dashboard."""
    wanted = [c.strip() for c in codes.split(",") if c.strip()]
    if not wanted:
        raise HTTPException(status_code=400, detail="Geef minstens één rekeningcode mee.")
    if len(wanted) > 200:
        raise HTTPException(status_code=400, detail="Te veel rekeningen in één keer.")
    try:
        start = datetime.strptime(date_from, "%Y-%m-%d").date()
        end = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Ongeldige datum: gebruik JJJJ-MM-DD.")
    if end <= start:
        raise HTTPException(status_code=400, detail="De einddatum ligt voor de begindatum.")
    key = f"{','.join(sorted(wanted))}|{date_from}|{date_to}"
    return _cached(
        _pl_lines_cache, key, refresh,
        lambda: kpis.fetch_pl_lines(kpis.get_client(), wanted, start, end),
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


# --- Kasprognose -------------------------------------------------------------

@app.get("/api/cash")
def api_cash(
    refresh: bool = Query(False),
    weeks: int | None = Query(None, ge=2, le=52),
    _auth: None = Depends(check_auth),
):
    """De rollende kasprognose. Eigen endpoint/cache; wordt pas opgehaald zodra de
    gebruiker de Kas-tab voor het eerst opent."""
    horizon = weeks or config.FORECAST_WEEKS
    return _cached(
        _cash_cache, f"weeks:{horizon}", refresh,
        lambda: kpis.build_cash_forecast(weeks=horizon),
    )


def _cash_changed() -> dict:
    """Na elke wijziging in het betaalplan moet de prognose opnieuw worden gerekend."""
    _cash_cache.clear()
    return {"ok": True, "plan": kpis.cash_plan_summary()}


@app.post("/api/cash/plan")
def api_cash_plan(payload: dict = Body(...), _auth: None = Depends(check_auth)):
    """Zet de betaalafspraak voor één groep (meestal één leverancier). Een lege of
    ontbrekende `mode` zet 'm terug op de standaard: betalen op vervaldatum."""
    key = str(payload.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Geef aan om welke groep het gaat.")
    mode = str(payload.get("mode") or "").strip()
    if not mode or mode == "due":
        _storage_guard(lambda: db.clear_cash_plan(key))
        return _cash_changed()
    if mode not in db.PLAN_MODES:
        raise HTTPException(status_code=400, detail=f"Onbekende betaalwijze: {mode}")
    try:
        start_week = int(payload.get("start_week") or 0)
        spread = int(payload.get("spread") or 1)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Week en spreiding moeten getallen zijn.")
    if not 0 <= start_week <= 52 or not 1 <= spread <= 52:
        raise HTTPException(status_code=400, detail="Week en spreiding vallen buiten bereik.")
    label = str(payload.get("label") or "")[:120]
    note = str(payload.get("note") or "")[:300]
    _storage_guard(
        lambda: db.set_cash_plan(key, label, mode, start_week, spread, note)
    )
    return _cash_changed()


@app.post("/api/cash/plan/apply-suggestions")
def api_cash_apply_suggestions(_auth: None = Depends(check_auth)):
    """Neemt het beredeneerde voorstel per leverancier in één keer over. Alles wat al
    handmatig is ingesteld blijft staan — het voorstel wordt alleen aangeboden voor
    groepen zonder eigen afspraak."""
    forecast = kpis.build_cash_forecast()
    applied = []
    for suggestion in forecast.get("suggestions") or []:
        _storage_guard(lambda s=suggestion: db.set_cash_plan(
            s["key"], s["label"], s["mode"], s.get("start_week", 0),
            s.get("spread", 1), s.get("reason", ""),
        ))
        applied.append(suggestion["key"])
    return {**_cash_changed(), "applied": applied}


@app.post("/api/cash/reset")
def api_cash_reset(_auth: None = Depends(check_auth)):
    """Alles terug naar 'op vervaldatum', inclusief de eigen regels."""
    _storage_guard(db.reset_cash_plan)
    return _cash_changed()


@app.post("/api/cash/extra")
def api_cash_extra(payload: dict = Body(...), _auth: None = Depends(check_auth)):
    """Een eigen regel in de prognose: een financieringsronde, een toegezegde betaling,
    een verwachte uitgave. Positief = ontvangst, negatief = uitgave."""
    label = str(payload.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="Geef de regel een naam.")
    try:
        amount = float(payload.get("amount"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Geef een bedrag op.")
    if amount == 0:
        raise HTTPException(status_code=400, detail="Een bedrag van nul verandert niets.")
    on_date = str(payload.get("date") or "").strip()
    try:
        datetime.strptime(on_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Ongeldige datum: gebruik JJJJ-MM-DD.")
    created = _storage_guard(lambda: db.add_cash_extra(
        label[:120], amount, on_date, str(payload.get("note") or "")[:300]
    ))
    return {**_cash_changed(), "extra": created}


@app.delete("/api/cash/extra/{extra_id}")
def api_cash_extra_delete(extra_id: int, _auth: None = Depends(check_auth)):
    _storage_guard(lambda: db.delete_cash_extra(extra_id))
    return _cash_changed()


@app.get("/", response_class=HTMLResponse)
def dashboard(_auth: None = Depends(check_auth)):
    return HTMLResponse(_DASHBOARD_HTML)
