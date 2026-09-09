"""
FastAPI-app: serveert het KPI-dashboard (één HTML-pagina) en een JSON-API die de
cijfers live uit Odoo haalt (met een korte cache, zie config.CACHE_TTL_SECONDS).

Iedereen logt persoonlijk in en heeft een rol (manager / financieel / beheerder). De
rechten worden HIER gecontroleerd, per endpoint — niet in het scherm. Zie app/auth.py.
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from datetime import datetime
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from . import auth, config, db, kpis, snapshot

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

_TEMPLATES = Path(__file__).parent / "templates"
_DASHBOARD_HTML = (_TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
_LOGIN_HTML = (_TEMPLATES / "login.html").read_text(encoding="utf-8")
_INVITE_HTML = (_TEMPLATES / "invite.html").read_text(encoding="utf-8")

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


# --- Wie ben je, en wat mag je? ------------------------------------------------

def _basic_credentials(request: Request) -> tuple[str, str] | None:
    """HTTP basic-auth blijft bestaan voor precies één doel: het beheerdersaccount uit de
    omgevingsvariabelen, zodat een externe planner `POST /api/snapshot` kan blijven
    aanroepen. Persoonlijke accounts loggen in met een sessie."""
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    if ":" not in raw:
        return None
    name, password = raw.split(":", 1)
    return name, password


def current_user(request: Request) -> auth.User | None:
    user = auth.user_for_token(request.cookies.get(auth.COOKIE_NAME))
    if user:
        return user
    credentials = _basic_credentials(request)
    if credentials and credentials[0] and credentials[1]:
        try:
            found = auth.authenticate(*credentials)
        except db.StorageUnavailable:
            return None
        # Alleen het omgevingsaccount mag via basic-auth binnen; een persoonlijk account
        # zou daarmee zijn wachtwoord bij elk verzoek meesturen.
        if found and found.source == "omgeving":
            return found
    return None


def require(minimum: str):
    """Dependency-fabriek: geeft de ingelogde gebruiker terug, of een nette fout.

    401 = niet ingelogd (de frontend stuurt je dan naar het inlogscherm),
    403 = wel ingelogd, maar deze rol mag dit niet. Bewust GEEN WWW-Authenticate-header:
    dan zou de browser zijn eigen inlogpopup tonen in plaats van ons inlogscherm."""
    def dependency(request: Request) -> auth.User:
        user = current_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Niet ingelogd.")
        if not user.may(minimum):
            raise HTTPException(
                status_code=403,
                detail=f"Hiervoor heb je de rol {auth.ROLE_LABELS.get(minimum, minimum)} "
                       f"of hoger nodig; jij bent {auth.ROLE_LABELS.get(user.role, user.role)}.",
            )
        return user
    return dependency


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        auth.COOKIE_NAME, token, max_age=config.SESSION_HOURS * 3600,
        httponly=True, samesite="lax", secure=config.COOKIE_SECURE, path="/",
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
    _user: auth.User = Depends(require("manager")),
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
    _user: auth.User = Depends(require("manager")),
):
    """De vastgelegde standen door de tijd. Komt uit de eigen database, niet uit Odoo —
    Odoo kan deze reeks niet reconstrueren."""
    return kpis.build_snapshot_series(days=days)


@app.post("/api/snapshot")
def api_snapshot_now(_user: auth.User = Depends(require("financieel"))):
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
    _user: auth.User = Depends(require("manager")),
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
    _user: auth.User = Depends(require("manager")),
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
    _user: auth.User = Depends(require("manager")),
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
    _user: auth.User = Depends(require("financieel")),
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
    user: auth.User = Depends(require("financieel")),
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
    auth.audit(user, "wv-rekening-verplaatst", {"code": code, "rubriek": rubriek or "(terug naar Odoo)"})
    return _layout_changed()


@app.post("/api/pl/rubriek")
def api_pl_add_rubriek(
    payload: dict = Body(...),
    user: auth.User = Depends(require("financieel")),
):
    name = str(payload.get("name") or "").strip()
    parent = str(payload.get("parent") or "").strip()
    if not name or not parent:
        raise HTTPException(status_code=400, detail="Geef een naam en een bovenliggende rubriek.")
    if len(name) > 80:
        raise HTTPException(status_code=400, detail="Die naam is te lang (max. 80 tekens).")
    created = _storage_guard(lambda: db.add_rubriek(name, parent))
    auth.audit(user, "wv-rubriek-toegevoegd", {"naam": name, "onder": parent})
    return {**_layout_changed(), "rubriek": created}


@app.delete("/api/pl/rubriek/{rubriek_id}")
def api_pl_delete_rubriek(rubriek_id: str, user: auth.User = Depends(require("financieel"))):
    """Verwijdert een zelf toegevoegde rubriek; de rekeningen erin gaan terug naar de
    rubriek die Odoo ze geeft."""
    if not db.is_custom(rubriek_id):
        raise HTTPException(
            status_code=400,
            detail="Alleen zelf toegevoegde rubrieken kunnen worden verwijderd; de "
                   "rubrieken uit het Odoo-rapport horen daar thuis.",
        )
    _storage_guard(lambda: db.delete_rubriek(rubriek_id))
    auth.audit(user, "wv-rubriek-verwijderd", {"rubriek": rubriek_id})
    return _layout_changed()


@app.post("/api/pl/reset")
def api_pl_reset(user: auth.User = Depends(require("financieel"))):
    """Alles terug naar de indeling van het Odoo-rapport."""
    _storage_guard(db.reset)
    auth.audit(user, "wv-indeling-hersteld")
    return _layout_changed()


# --- Kasprognose -------------------------------------------------------------

@app.get("/api/cash")
def api_cash(
    refresh: bool = Query(False),
    weeks: int | None = Query(None, ge=2, le=52),
    _user: auth.User = Depends(require("manager")),
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
def api_cash_plan(payload: dict = Body(...), user: auth.User = Depends(require("financieel"))):
    """Zet de betaalafspraak voor één groep (meestal één leverancier). Een lege of
    ontbrekende `mode` zet 'm terug op de standaard: betalen op vervaldatum."""
    key = str(payload.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Geef aan om welke groep het gaat.")
    mode = str(payload.get("mode") or "").strip()
    if not mode or mode == "due":
        _storage_guard(lambda: db.clear_cash_plan(key))
        auth.audit(user, "betaalplan-teruggezet", {"groep": key})
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
    auth.audit(user, "betaalplan-gewijzigd", {
        "groep": key, "naam": label, "betaalwijze": mode,
        "vanaf_week": start_week + 1, "delen": spread,
    })
    return _cash_changed()


@app.post("/api/cash/plan/apply-suggestions")
def api_cash_apply_suggestions(user: auth.User = Depends(require("financieel"))):
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
    auth.audit(user, "betaalplan-voorstel-overgenomen", {"groepen": applied})
    return {**_cash_changed(), "applied": applied}


@app.post("/api/cash/reset")
def api_cash_reset(user: auth.User = Depends(require("financieel"))):
    """Alles terug naar 'op vervaldatum', inclusief de eigen regels."""
    _storage_guard(db.reset_cash_plan)
    auth.audit(user, "betaalplan-gewist")
    return _cash_changed()


@app.post("/api/cash/extra")
def api_cash_extra(payload: dict = Body(...), user: auth.User = Depends(require("financieel"))):
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
    auth.audit(user, "kasregel-toegevoegd", {"naam": label[:120], "bedrag": amount, "datum": on_date})
    return {**_cash_changed(), "extra": created}


@app.delete("/api/cash/extra/{extra_id}")
def api_cash_extra_delete(extra_id: int, user: auth.User = Depends(require("financieel"))):
    _storage_guard(lambda: db.delete_cash_extra(extra_id))
    auth.audit(user, "kasregel-verwijderd", {"id": extra_id})
    return _cash_changed()


# --- Inloggen, uitloggen, uitnodigingen -----------------------------------------

def _escape(value: str) -> str:
    return (str(value or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _login_page(message: str = "", email: str = "", status: int = 200) -> HTMLResponse:
    html = (_LOGIN_HTML
            .replace("{{MELDINGWEERGAVE}}", "block" if message else "none")
            .replace("{{MELDING}}", _escape(message))
            .replace("{{EMAIL}}", _escape(email)))
    return HTMLResponse(html, status_code=status)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return _login_page()


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    form = await request.form()
    identifier = str(form.get("email") or "").strip()
    password = str(form.get("password") or "")
    key = identifier.lower() or (request.client.host if request.client else "onbekend")

    if auth.too_many_attempts(key):
        auth.audit(None, "inloggen-geblokkeerd", {"account": identifier})
        return _login_page(
            f"Te veel mislukte pogingen. Probeer het over {config.LOGIN_LOCKOUT_MINUTES} "
            "minuten opnieuw.", identifier, status=429)

    try:
        user = auth.authenticate(identifier, password)
    except db.StorageUnavailable as exc:
        logger.warning("Inloggen mislukt door de database: %s", exc)
        return _login_page(
            "De database is nu niet bereikbaar, dus persoonlijke accounts kunnen even niet "
            "inloggen. Probeer het zo nog eens.", identifier, status=503)

    if not user:
        auth.register_failure(key)
        auth.audit(None, "inloggen-mislukt", {"account": identifier})
        # Bewust één melding voor beide fouten: anders verklap je welke accounts bestaan.
        return _login_page("Onbekende combinatie van e-mailadres en wachtwoord.",
                           identifier, status=401)

    auth.clear_attempts(key)
    token = auth.start_session(user)
    auth.audit(user, "ingelogd", {"via": user.source})
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response, token)
    return response


@app.get("/logout")
def logout(request: Request):
    user = current_user(request)
    auth.end_session(request.cookies.get(auth.COOKIE_NAME))
    if user:
        auth.audit(user, "uitgelogd")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


def _invite_page(token: str, message: str = "", name: str = "", status: int = 200) -> HTMLResponse:
    html = (_INVITE_HTML
            .replace("{{TOKEN}}", _escape(token))
            .replace("{{MELDINGWEERGAVE}}", "block" if message else "none")
            .replace("{{MELDING}}", _escape(message))
            .replace("{{NAAM}}", _escape(name))
            .replace("{{MINLENGTE}}", str(config.MIN_PASSWORD_LENGTH)))
    return HTMLResponse(html, status_code=status)


@app.get("/uitnodiging", response_class=HTMLResponse)
def invite_page(token: str = Query("")):
    row = auth.user_for_invite(token)
    if not row:
        return _login_page(
            "Deze uitnodigingslink is niet (meer) geldig. Vraag de beheerder om een nieuwe.",
            status=400)
    return _invite_page(token, name=row.get("name") or row["email"])


@app.post("/uitnodiging", response_class=HTMLResponse)
async def invite_submit(request: Request):
    form = await request.form()
    token = str(form.get("token") or "")
    password = str(form.get("password") or "")
    repeat = str(form.get("password2") or "")
    row = auth.user_for_invite(token)
    if not row:
        return _login_page(
            "Deze uitnodigingslink is niet (meer) geldig. Vraag de beheerder om een nieuwe.",
            status=400)
    name = row.get("name") or row["email"]
    if password != repeat:
        return _invite_page(token, "De twee wachtwoorden zijn niet gelijk.", name, status=400)
    problem = auth.password_problem(password)
    if problem:
        return _invite_page(token, problem, name, status=400)
    user = auth.accept_invite(token, password)
    if not user:
        return _login_page("Deze uitnodigingslink is niet (meer) geldig.", status=400)
    auth.audit(user, "wachtwoord-ingesteld")
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response, auth.start_session(user))
    return response


@app.get("/api/me")
def api_me(request: Request):
    """Wie ben ik en wat mag ik? Het scherm gebruikt dit alleen om te verbergen wat je
    tóch niet mag; de echte controle zit op de endpoints hierboven."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Niet ingelogd.")
    return {**user.as_dict(), "database": db.configured()}


# --- Gebruikersbeheer (alleen beheerder) ------------------------------------------

def _invite_link(request: Request, token: str) -> str:
    """De link die de beheerder doorstuurt. Achter de Railway-proxy ziet de app zelf
    http; de buitenkant is https. Vandaar de correctie — een http-link in een mailtje
    ziet er niet uit en werkt bij een strikte browser niet."""
    base = str(request.base_url).rstrip("/")
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if forwarded:
        base = forwarded + base[base.index("://"):]
    elif base.startswith("http://") and not base.startswith(("http://127.", "http://localhost")):
        base = "https://" + base[len("http://"):]
    return base + "/uitnodiging?token=" + token


def _public_user(row: dict) -> dict:
    return {
        "id": row["id"], "email": row["email"], "name": row.get("name") or "",
        "role": row["role"], "role_label": auth.ROLE_LABELS.get(row["role"], row["role"]),
        "active": bool(row.get("active")),
        "has_password": bool(row.get("password_hash")),
        "invite_open": bool(row.get("invite_hash")),
        "last_login_at": row["last_login_at"].isoformat() if row.get("last_login_at") else None,
    }


def _require_database() -> None:
    if not db.configured():
        raise HTTPException(
            status_code=503,
            detail="Persoonlijke accounts hebben een database nodig. Voeg in Railway een "
                   "Postgres toe en koppel DATABASE_URL aan deze service.",
        )


@app.get("/api/users")
def api_users(user: auth.User = Depends(require("beheerder"))):
    _require_database()
    return {
        "users": [_public_user(row) for row in _storage_guard(db.list_users)],
        "roles": [{"key": key, "label": auth.ROLE_LABELS[key],
                   "description": auth.ROLE_DESCRIPTIONS[key]} for key in auth.ROLES],
        "me": user.as_dict(),
    }


@app.post("/api/users")
def api_user_create(request: Request, payload: dict = Body(...),
                    user: auth.User = Depends(require("beheerder"))):
    """Maakt een account aan en geeft één keer een uitnodigingslink terug. Die stuur je
    zelf door; de nieuwe gebruiker kiest daarmee zijn eigen wachtwoord. Er komt hier dus
    nooit een wachtwoord langs."""
    _require_database()
    email = str(payload.get("email") or "").strip().lower()
    name = str(payload.get("name") or "").strip()[:80]
    role = str(payload.get("role") or config.DEFAULT_USER_ROLE).strip()
    if "@" not in email or len(email) < 5:
        raise HTTPException(status_code=400, detail="Geef een geldig e-mailadres op.")
    if role not in auth.ROLES:
        raise HTTPException(status_code=400, detail=f"Onbekende rol: {role}")
    if _storage_guard(lambda: db.get_user_by_email(email)):
        raise HTTPException(status_code=400, detail="Dat e-mailadres heeft al een account.")
    created = _storage_guard(lambda: db.create_user(email, name, role))
    token = _storage_guard(lambda: auth.create_invite(created["id"]))
    auth.audit(user, "gebruiker-toegevoegd", {"email": email, "rol": role})
    return {"ok": True, "user": created, "invite_url": _invite_link(request, token),
            "invite_hours": config.INVITE_HOURS}


@app.post("/api/users/{user_id}")
def api_user_update(user_id: int, payload: dict = Body(...),
                    user: auth.User = Depends(require("beheerder"))):
    _require_database()
    target = _storage_guard(lambda: db.get_user(user_id))
    if not target:
        raise HTTPException(status_code=404, detail="Die gebruiker bestaat niet.")
    role = payload.get("role")
    active = payload.get("active")
    name = payload.get("name")
    if role is not None and role not in auth.ROLES:
        raise HTTPException(status_code=400, detail=f"Onbekende rol: {role}")
    # Jezelf uitzetten of degraderen is de klassieke manier om jezelf buiten te sluiten.
    if user.source == "database" and target["id"] == user.id:
        if active is False:
            raise HTTPException(status_code=400, detail="Je kunt je eigen account niet uitzetten.")
        if role is not None and role != "beheerder":
            raise HTTPException(status_code=400,
                                detail="Je kunt je eigen beheerdersrol niet afnemen.")
    _storage_guard(lambda: db.update_user(
        user_id, name=None if name is None else str(name)[:80],
        role=role, active=None if active is None else bool(active)))
    auth.audit(user, "gebruiker-gewijzigd", {
        "email": target["email"],
        "rol": role or target["role"],
        "actief": target["active"] if active is None else bool(active),
    })
    return {"ok": True, "user": _public_user(_storage_guard(lambda: db.get_user(user_id)))}


@app.post("/api/users/{user_id}/invite")
def api_user_invite(user_id: int, request: Request,
                    user: auth.User = Depends(require("beheerder"))):
    """Nieuwe uitnodigingslink — ook de manier om een vergeten wachtwoord op te lossen."""
    _require_database()
    target = _storage_guard(lambda: db.get_user(user_id))
    if not target:
        raise HTTPException(status_code=404, detail="Die gebruiker bestaat niet.")
    token = _storage_guard(lambda: auth.create_invite(user_id))
    auth.audit(user, "uitnodiging-aangemaakt", {"email": target["email"]})
    return {"ok": True, "invite_url": _invite_link(request, token),
            "invite_hours": config.INVITE_HOURS, "email": target["email"]}


@app.delete("/api/users/{user_id}")
def api_user_delete(user_id: int, user: auth.User = Depends(require("beheerder"))):
    _require_database()
    target = _storage_guard(lambda: db.get_user(user_id))
    if not target:
        raise HTTPException(status_code=404, detail="Die gebruiker bestaat niet.")
    if user.source == "database" and target["id"] == user.id:
        raise HTTPException(status_code=400, detail="Je kunt je eigen account niet verwijderen.")
    _storage_guard(lambda: db.delete_user(user_id))
    auth.audit(user, "gebruiker-verwijderd", {"email": target["email"]})
    return {"ok": True}


@app.get("/api/audit")
def api_audit(limit: int = Query(100, ge=1, le=500),
              _user: auth.User = Depends(require("beheerder"))):
    _require_database()
    return {"entries": _storage_guard(lambda: db.list_audit(limit))}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(_DASHBOARD_HTML)
