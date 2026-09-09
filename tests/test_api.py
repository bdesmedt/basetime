"""
Tests voor de FastAPI-routes: basic-auth-gedrag en de happy path van /api/kpis,
met app.kpis.build_dashboard_payload gemocked (dus geen echte Odoo-aanroep nodig).
"""

import base64
import time

import pytest

from fastapi.testclient import TestClient

from app import config, main

# cachesleutel voor de standaardperiode (geen expliciete keuze in de URL)
_DEFAULT_KEY = f"months:{config.MONTHS_LOOKBACK}"


def _auth_header(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


FAKE_PAYLOAD = {
    "generated_at": "2026-08-11T12:00:00+00:00",
    "window": {"months_lookback": 2, "labels": ["jun", "jul"], "label_text": "laatste 2 volledige maanden"},
    "cash": {"available_now": -88872.49, "credit_limit": -150000.0, "credit_headroom": 61127.51},
    "runway": {"months": 0.47, "weeks": 2.0, "fixed_monthly_costs": 130626.0},
    "revenue": [87834.11, 87965.32],
    "cogs": [25899.34, 46019.18],
    "margin_pct": [70.5, 47.7],
    "recurring_revenue": [31465.44, 34219.85],
    "recurring_revenue_avg": 32842.65,
    "order_intake": [104086.41, 79071.17],
    "order_intake_sum": 183157.58,
    "cashflow": [-62201.84, 7580.14],
    "cashflow_avg": -27310.85,
    "purchase_backlog": {"total": 1873025.92, "current_year_or_earlier": 1269066.52, "future_years": 603959.4, "order_count": 23},
    "pipeline": {
        "opportunity_count": 2,
        "nominal_total": 1400000.0,
        "weighted_total": 818040.0,
        "by_stage": [{"stage": "Onderhandeling (75%)", "nominal": 1000000.0, "weighted": 500000.0}],
        "top_deals": [{"name": "Deal A", "stage": "Onderhandeling (75%)", "probability": 50.0, "nominal": 1000000.0, "weighted": 500000.0}],
    },
}


def test_healthz_requires_no_auth_and_leaks_no_business_data():
    client = TestClient(main.app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # alleen ja/nee-vlaggen over de opzet, geen cijfers of namen
    assert set(body) == {"status", "database", "snapshot_scheduler"}
    assert body["database"] is False          # geen DATABASE_URL in de tests
    assert body["snapshot_scheduler"] is False


def test_dashboard_sends_you_to_the_login_page_when_not_signed_in():
    client = TestClient(main.app)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_dashboard_rejects_wrong_password():
    client = TestClient(main.app)
    resp = client.get("/", headers=_auth_header("testuser", "wrong-password"),
                      follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_dashboard_accepts_correct_credentials():
    client = TestClient(main.app)
    resp = client.get("/", headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 200
    assert "KPI-dashboard" in resp.text


def test_api_kpis_requires_auth():
    client = TestClient(main.app)
    resp = client.get("/api/kpis")
    assert resp.status_code == 401


def test_api_kpis_returns_payload_and_uses_cache(monkeypatch):
    call_count = {"n": 0}

    def fake_build_payload():
        call_count["n"] += 1
        return FAKE_PAYLOAD

    monkeypatch.setattr(main.kpis, "build_dashboard_payload", fake_build_payload)
    main._cache.clear()

    client = TestClient(main.app)
    headers = _auth_header("testuser", "testpass")

    resp1 = client.get("/api/kpis", headers=headers)
    assert resp1.status_code == 200
    assert resp1.json()["cash"]["available_now"] == -88872.49
    assert call_count["n"] == 1

    # tweede aanroep binnen de cache-periode mag Odoo niet opnieuw aanroepen
    resp2 = client.get("/api/kpis", headers=headers)
    assert resp2.status_code == 200
    assert call_count["n"] == 1

    # met ?refresh=1 moet de cache wel worden overgeslagen
    resp3 = client.get("/api/kpis?refresh=1", headers=headers)
    assert resp3.status_code == 200
    assert call_count["n"] == 2


def test_api_kpis_falls_back_to_stale_data_on_odoo_error(monkeypatch):
    main._cache.clear()
    # zo oud dat een refresh geforceerd wordt
    main._cache[_DEFAULT_KEY] = {"data": dict(FAKE_PAYLOAD), "fetched_at": 0.0}

    def failing_build_payload():
        raise RuntimeError("Odoo-authenticatie mislukt")

    monkeypatch.setattr(main.kpis, "build_dashboard_payload", failing_build_payload)

    client = TestClient(main.app)
    resp = client.get("/api/kpis", headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 200
    assert "Odoo-authenticatie mislukt" in resp.json()["stale_error"]


def test_api_kpis_returns_502_when_no_cache_and_odoo_fails(monkeypatch):
    main._cache.clear()

    def failing_build_payload():
        raise RuntimeError("Odoo-authenticatie mislukt")

    monkeypatch.setattr(main.kpis, "build_dashboard_payload", failing_build_payload)

    client = TestClient(main.app)
    resp = client.get("/api/kpis", headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 502


# --- /api/details/{key} — doorklik-detailschermen ---------------------------

def test_api_details_requires_auth():
    client = TestClient(main.app)
    resp = client.get("/api/details/pipeline")
    assert resp.status_code == 401


def test_api_details_returns_404_for_unknown_key(monkeypatch):
    client = TestClient(main.app)
    resp = client.get("/api/details/onbekend", headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 404


def test_api_details_returns_payload_and_uses_per_key_cache(monkeypatch):
    call_count = {"n": 0}
    fake_rows = [{"name": "Deal A", "customer": "Grupoalava", "stage": "Onderhandeling (75%)",
                  "probability": 50.0, "nominal": 1000000.0, "weighted": 500000.0}]

    def fake_build_detail_payload(key, period=None):
        call_count["n"] += 1
        assert key == "pipeline"
        return fake_rows

    monkeypatch.setattr(main.kpis, "build_detail_payload", fake_build_detail_payload)
    main._detail_cache.clear()

    client = TestClient(main.app)
    headers = _auth_header("testuser", "testpass")

    resp1 = client.get("/api/details/pipeline", headers=headers)
    assert resp1.status_code == 200
    assert resp1.json() == fake_rows
    assert call_count["n"] == 1

    # binnen de cache-periode: geen nieuwe Odoo-aanroep
    resp2 = client.get("/api/details/pipeline", headers=headers)
    assert resp2.status_code == 200
    assert call_count["n"] == 1

    # met ?refresh=1 wordt de cache overgeslagen
    resp3 = client.get("/api/details/pipeline?refresh=1", headers=headers)
    assert resp3.status_code == 200
    assert call_count["n"] == 2


def test_api_details_falls_back_to_stale_data_on_odoo_error(monkeypatch):
    stale_rows = [{"name": "Oude data"}]
    main._detail_cache.clear()
    main._detail_cache[f"purchase_backlog|{_DEFAULT_KEY}"] = {"data": stale_rows, "fetched_at": 0.0}

    def failing_build_detail_payload(key, period=None):
        raise RuntimeError("Odoo-authenticatie mislukt")

    monkeypatch.setattr(main.kpis, "build_detail_payload", failing_build_detail_payload)
    client = TestClient(main.app)
    resp = client.get("/api/details/purchase_backlog", headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 200
    assert resp.json() == stale_rows


# --- /api/inventory — voorraadtab -------------------------------------------

FAKE_INVENTORY_PAYLOAD = {
    "generated_at": "2026-08-11T12:00:00+00:00",
    "window": {"labels": ["jun", "jul"], "label_text": "laatste 2 volledige maanden"},
    "stock_value": {"total": 2172.0, "by_product": [{"name": "AC-103", "quantity": 38, "value": 1330.0}]},
    "movements": {"in": [10.0, 4.0], "out": [3.0, 8.0]},
    "coverage": {"months": 12.3, "avg_monthly_cogs": 35000.0},
}


def test_api_inventory_requires_auth():
    client = TestClient(main.app)
    resp = client.get("/api/inventory")
    assert resp.status_code == 401


def test_api_inventory_returns_payload_and_uses_cache(monkeypatch):
    call_count = {"n": 0}

    def fake_build_inventory_payload():
        call_count["n"] += 1
        return FAKE_INVENTORY_PAYLOAD

    monkeypatch.setattr(main.kpis, "build_inventory_payload", fake_build_inventory_payload)
    main._inventory_cache.clear()

    client = TestClient(main.app)
    headers = _auth_header("testuser", "testpass")

    resp1 = client.get("/api/inventory", headers=headers)
    assert resp1.status_code == 200
    assert resp1.json()["stock_value"]["total"] == 2172.0
    assert call_count["n"] == 1

    resp2 = client.get("/api/inventory", headers=headers)
    assert resp2.status_code == 200
    assert call_count["n"] == 1  # binnen cache-periode, geen nieuwe Odoo-aanroep

    resp3 = client.get("/api/inventory?refresh=1", headers=headers)
    assert resp3.status_code == 200
    assert call_count["n"] == 2


def test_api_inventory_falls_back_to_stale_data_on_odoo_error(monkeypatch):
    main._inventory_cache.clear()
    main._inventory_cache[_DEFAULT_KEY] = {"data": dict(FAKE_INVENTORY_PAYLOAD), "fetched_at": 0.0}

    def failing_build_inventory_payload():
        raise RuntimeError("Odoo-authenticatie mislukt")

    monkeypatch.setattr(main.kpis, "build_inventory_payload", failing_build_inventory_payload)
    client = TestClient(main.app)
    resp = client.get("/api/inventory", headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 200
    assert "Odoo-authenticatie mislukt" in resp.json()["stale_error"]


def test_api_inventory_returns_502_when_no_cache_and_odoo_fails(monkeypatch):
    main._inventory_cache.clear()

    def failing_build_inventory_payload():
        raise RuntimeError("Odoo-authenticatie mislukt")

    monkeypatch.setattr(main.kpis, "build_inventory_payload", failing_build_inventory_payload)
    client = TestClient(main.app)
    resp = client.get("/api/inventory", headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 502


# --- Periodekeuze op de endpoints -------------------------------------------

def test_api_kpis_caches_per_period(monkeypatch):
    """Twee verschillende periodes mogen elkaars cijfers niet uit de cache krijgen."""
    seen = []

    def fake_build_payload(**period):
        seen.append(period)
        return FAKE_PAYLOAD

    monkeypatch.setattr(main.kpis, "build_dashboard_payload", fake_build_payload)
    main._cache.clear()

    client = TestClient(main.app)
    headers = _auth_header("testuser", "testpass")

    client.get("/api/kpis?months=3", headers=headers)
    client.get("/api/kpis?months=12", headers=headers)
    client.get("/api/kpis?months=3", headers=headers)  # opnieuw: nu uit de cache

    assert seen == [{"months": 3}, {"months": 12}]


def test_api_kpis_accepts_a_custom_date_range(monkeypatch):
    seen = []

    def fake_build_payload(**period):
        seen.append(period)
        return FAKE_PAYLOAD

    monkeypatch.setattr(main.kpis, "build_dashboard_payload", fake_build_payload)
    main._cache.clear()

    client = TestClient(main.app)
    resp = client.get(
        "/api/kpis?date_from=2025-11-01&date_to=2026-04-30",
        headers=_auth_header("testuser", "testpass"),
    )
    assert resp.status_code == 200
    assert seen[0]["date_from"].isoformat() == "2025-11-01"
    assert seen[0]["date_to"].isoformat() == "2026-04-30"


def test_api_kpis_rejects_an_unparseable_date():
    client = TestClient(main.app)
    resp = client.get(
        "/api/kpis?date_from=01-11-2025&date_to=2026-04-30",
        headers=_auth_header("testuser", "testpass"),
    )
    assert resp.status_code == 400
    assert "JJJJ-MM-DD" in resp.json()["detail"]


def test_api_details_caches_per_period(monkeypatch):
    seen = []

    def fake_build_detail_payload(key, period=None):
        seen.append((key, period))
        return []

    monkeypatch.setattr(main.kpis, "build_detail_payload", fake_build_detail_payload)
    main._detail_cache.clear()

    client = TestClient(main.app)
    headers = _auth_header("testuser", "testpass")
    client.get("/api/details/order_intake?months=3", headers=headers)
    client.get("/api/details/order_intake?months=6", headers=headers)
    client.get("/api/details/order_intake?months=3", headers=headers)

    assert [s[1] for s in seen] == [{"months": 3}, {"months": 6}]


# --- Winst- en verliestab ----------------------------------------------------

FAKE_PL_PAYLOAD = {
    "period": {"label_text": "laatste 2 volledige maanden"},
    "report": {"id": 25, "name": "Testrapport"},
    "months": [{"key": "2026-07-01", "label": "jul", "partial": False}],
    "tree": [], "accounts": [], "rubriek_options": [], "group_options": [],
    "unallocated_id": "unallocated", "result_line_id": "30",
    "layout": {"storage": "postgres", "override_count": 0, "custom_count": 0, "dangling": []},
}


def test_api_pl_requires_authentication():
    client = TestClient(main.app)
    assert client.get("/api/pl").status_code == 401


def test_api_pl_returns_the_payload_and_caches_per_period(monkeypatch):
    seen = []

    def fake_build(**kwargs):
        seen.append(kwargs)
        return FAKE_PL_PAYLOAD

    monkeypatch.setattr(main.kpis, "build_pl_payload", fake_build)
    main._pl_cache.clear()

    client = TestClient(main.app)
    headers = _auth_header("testuser", "testpass")
    resp = client.get("/api/pl?months=3", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["report"]["name"] == "Testrapport"
    client.get("/api/pl?months=3", headers=headers)
    client.get("/api/pl?months=6", headers=headers)
    assert seen == [{"months": 3}, {"months": 6}]


def test_moving_an_account_clears_the_pl_cache(monkeypatch):
    moved = []
    monkeypatch.setattr(main.db, "set_override", lambda code, rubriek: moved.append((code, rubriek)))
    monkeypatch.setattr(main.kpis, "layout_summary", lambda: {"storage": "postgres"})
    main._pl_cache["months:3"] = {"data": FAKE_PL_PAYLOAD, "fetched_at": 1e12}

    client = TestClient(main.app)
    resp = client.post(
        "/api/pl/account", json={"code": "481000", "rubriek": "172"},
        headers=_auth_header("testuser", "testpass"),
    )
    assert resp.status_code == 200
    assert moved == [("481000", "172")]
    assert main._pl_cache == {}, "de W&V-cache bevat nog de oude indeling"


def test_moving_an_account_back_to_odoo_clears_the_override(monkeypatch):
    cleared = []
    monkeypatch.setattr(main.db, "clear_override", lambda code: cleared.append(code))
    monkeypatch.setattr(main.kpis, "layout_summary", lambda: {"storage": "postgres"})

    client = TestClient(main.app)
    resp = client.post(
        "/api/pl/account", json={"code": "481000", "rubriek": ""},
        headers=_auth_header("testuser", "testpass"),
    )
    assert resp.status_code == 200
    assert cleared == ["481000"]


def test_changing_the_layout_without_a_database_gives_a_clear_message(monkeypatch):
    def boom(*args, **kwargs):
        raise main.db.StorageUnavailable("Er is geen DATABASE_URL ingesteld.")

    monkeypatch.setattr(main.db, "set_override", boom)

    client = TestClient(main.app)
    resp = client.post(
        "/api/pl/account", json={"code": "481000", "rubriek": "172"},
        headers=_auth_header("testuser", "testpass"),
    )
    assert resp.status_code == 503
    assert "DATABASE_URL" in resp.json()["detail"]


def test_rubrieken_from_the_odoo_report_cannot_be_deleted():
    client = TestClient(main.app)
    resp = client.delete("/api/pl/rubriek/172", headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 400
    assert "zelf toegevoegde" in resp.json()["detail"]


def test_adding_a_rubriek_needs_a_name_and_a_parent():
    client = TestClient(main.app)
    resp = client.post(
        "/api/pl/rubriek", json={"name": "", "parent": "20"},
        headers=_auth_header("testuser", "testpass"),
    )
    assert resp.status_code == 400


def test_api_pl_lines_needs_a_valid_date_range():
    client = TestClient(main.app)
    headers = _auth_header("testuser", "testpass")
    assert client.get("/api/pl/lines?codes=430500&date_from=gisteren&date_to=2026-09-01",
                      headers=headers).status_code == 400
    assert client.get("/api/pl/lines?codes=430500&date_from=2026-09-01&date_to=2026-08-01",
                      headers=headers).status_code == 400
    assert client.get("/api/pl/lines?codes=&date_from=2026-08-01&date_to=2026-09-01",
                      headers=headers).status_code == 400


def test_api_pl_lines_passes_the_codes_and_period_through(monkeypatch):
    seen = {}

    def fake_fetch(client, codes, start, end, *args, **kwargs):
        seen["codes"] = codes
        seen["start"] = start.isoformat()
        seen["end"] = end.isoformat()
        return {"lines": [], "total": 0.0, "count": 0, "truncated": False}

    monkeypatch.setattr(main.kpis, "fetch_pl_lines", fake_fetch)
    monkeypatch.setattr(main.kpis, "get_client", lambda: object())
    main._pl_lines_cache.clear()

    client = TestClient(main.app)
    resp = client.get("/api/pl/lines?codes=430500,431000&date_from=2026-08-01&date_to=2026-09-01",
                      headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 200
    assert seen == {"codes": ["430500", "431000"], "start": "2026-08-01", "end": "2026-09-01"}


def test_loading_the_kpis_records_todays_snapshot(monkeypatch):
    saved = []
    monkeypatch.setattr(main.kpis, "build_dashboard_payload", lambda **kw: FAKE_PAYLOAD)
    monkeypatch.setattr(main.db, "configured", lambda: True)
    monkeypatch.setattr(main.db, "snapshot_exists_today", lambda: False)
    monkeypatch.setattr(main.db, "save_snapshot", lambda payload: saved.append(payload))
    main._cache.clear()

    client = TestClient(main.app)
    resp = client.get("/api/kpis?months=2", headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 200
    assert len(saved) == 1
    assert saved[0]["cash"] == FAKE_PAYLOAD["cash"]["available_now"]


def test_a_snapshot_is_recorded_once_a_day(monkeypatch):
    saved = []
    monkeypatch.setattr(main.kpis, "build_dashboard_payload", lambda **kw: FAKE_PAYLOAD)
    monkeypatch.setattr(main.db, "configured", lambda: True)
    monkeypatch.setattr(main.db, "snapshot_exists_today", lambda: True)
    monkeypatch.setattr(main.db, "save_snapshot", lambda payload: saved.append(payload))
    main._cache.clear()

    client = TestClient(main.app)
    client.get("/api/kpis?months=4", headers=_auth_header("testuser", "testpass"))
    assert saved == []


def test_a_failing_snapshot_never_breaks_the_dashboard(monkeypatch):
    def boom():
        raise RuntimeError("database ligt eruit")

    monkeypatch.setattr(main.kpis, "build_dashboard_payload", lambda **kw: FAKE_PAYLOAD)
    monkeypatch.setattr(main.db, "configured", lambda: True)
    monkeypatch.setattr(main.db, "snapshot_exists_today", boom)
    main._cache.clear()

    client = TestClient(main.app)
    resp = client.get("/api/kpis?months=5", headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 200
    assert resp.json()["cash"]["available_now"] == FAKE_PAYLOAD["cash"]["available_now"]


def test_the_snapshot_endpoint_reports_a_missing_database():
    # zonder DATABASE_URL (de situatie in de tests) hoort dit een nette 503 te geven en
    # niet stilletjes "gelukt" te melden
    client = TestClient(main.app)
    resp = client.post("/api/snapshot", headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 503
    assert "DATABASE_URL" in resp.json()["detail"]


def test_the_snapshot_endpoint_forces_a_new_measurement(monkeypatch):
    """Ook als de meting van vandaag er al staat, moet dit endpoint 'm overschrijven —
    anders kun je na een correctie in Odoo niet opnieuw meten."""
    saved = []
    monkeypatch.setattr(main.db, "configured", lambda: True)
    monkeypatch.setattr(main.snapshot.db, "configured", lambda: True)
    monkeypatch.setattr(main.snapshot.db, "snapshot_exists_today", lambda: True)
    monkeypatch.setattr(main.snapshot.db, "save_snapshot", lambda standen: saved.append(standen))
    monkeypatch.setattr(main.snapshot.kpis, "build_dashboard_payload", lambda **kw: FAKE_PAYLOAD)

    client = TestClient(main.app)
    resp = client.post("/api/snapshot", headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 200
    assert len(saved) == 1
    assert resp.json()["captured"]["cash"] == FAKE_PAYLOAD["cash"]["available_now"]


def test_api_snapshots_requires_authentication():
    client = TestClient(main.app)
    assert client.get("/api/snapshots").status_code == 401


# --- Ingebouwde planner ------------------------------------------------------

def test_the_scheduler_does_not_start_without_a_database():
    # in de tests staat geen DATABASE_URL; dan valt er niets vast te leggen en hoeft er
    # ook geen achtergrondtaak te draaien
    assert main.snapshot.start_scheduler() is False
    assert main.snapshot.scheduler_running() is False


def test_the_scheduler_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(main.snapshot.db, "configured", lambda: True)
    monkeypatch.setattr(main.snapshot.config, "SNAPSHOT_SCHEDULER_ENABLED", False)
    assert main.snapshot.start_scheduler() is False
    assert main.snapshot.scheduler_running() is False


def test_the_scheduler_starts_and_stops_cleanly(monkeypatch):
    monkeypatch.setattr(main.snapshot.db, "configured", lambda: True)
    monkeypatch.setattr(main.snapshot.config, "SNAPSHOT_SCHEDULER_ENABLED", True)
    # meteen aan de slag, en daarna lang wachten zodat de test niet op de lus hoeft
    monkeypatch.setattr(main.snapshot.config, "SNAPSHOT_STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(main.snapshot.config, "SNAPSHOT_CHECK_MINUTES", 60)
    calls = []
    monkeypatch.setattr(main.snapshot, "capture", lambda: calls.append(1))

    assert main.snapshot.start_scheduler() is True
    for _ in range(50):
        if calls:
            break
        time.sleep(0.02)
    assert calls, "de planner heeft niet gemeten"
    main.snapshot.stop_scheduler()
    assert main.snapshot.scheduler_running() is False


def test_a_failing_measurement_keeps_the_scheduler_alive(monkeypatch):
    """Een mislukte meting (Odoo eruit, database traag) mag de achtergrondtaak niet
    doden — anders stopt het vastleggen stilletjes tot de volgende deploy."""
    monkeypatch.setattr(main.snapshot.db, "configured", lambda: True)
    monkeypatch.setattr(main.snapshot.config, "SNAPSHOT_SCHEDULER_ENABLED", True)
    monkeypatch.setattr(main.snapshot.config, "SNAPSHOT_STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(main.snapshot.config, "SNAPSHOT_CHECK_MINUTES", 60)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("Odoo ligt eruit")

    monkeypatch.setattr(main.snapshot, "capture", boom)
    assert main.snapshot.start_scheduler() is True
    for _ in range(50):
        if calls:
            break
        time.sleep(0.02)
    assert calls
    assert main.snapshot.scheduler_running() is True
    main.snapshot.stop_scheduler()


def test_capture_skips_when_todays_measurement_already_exists(monkeypatch):
    monkeypatch.setattr(main.snapshot.db, "configured", lambda: True)
    monkeypatch.setattr(main.snapshot.db, "snapshot_exists_today", lambda: True)
    monkeypatch.setattr(main.snapshot.kpis, "build_dashboard_payload",
                        lambda **kw: pytest.fail("Odoo hoort niet bevraagd te worden"))
    assert main.snapshot.capture() is None


# --- Kasprognose -------------------------------------------------------------

FAKE_FORECAST = {
    "as_of": "2026-09-09", "weeks": 13, "rows": [], "groups": [],
    "start_balance": -133725.41, "credit_limit": -150000.0, "headroom_now": 16274.59,
    "suggestions": [{"key": "ap:partner:5", "label": "Faber", "mode": "spread",
                     "start_week": 0, "spread": 13, "overdue": 204441.0,
                     "reason": "te groot voor één week"}],
    "storage": "postgres",
}


@pytest.fixture(autouse=True)
def _clear_cash_cache():
    main._cash_cache.clear()
    yield
    main._cash_cache.clear()


def test_api_cash_requires_authentication():
    client = TestClient(main.app)
    assert client.get("/api/cash").status_code == 401


def test_api_cash_returns_the_forecast(monkeypatch):
    monkeypatch.setattr(main.kpis, "build_cash_forecast", lambda weeks: FAKE_FORECAST)
    client = TestClient(main.app)
    resp = client.get("/api/cash", headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 200
    assert resp.json()["start_balance"] == -133725.41


def test_changing_the_payment_plan_empties_the_forecast_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(main.kpis, "build_cash_forecast",
                        lambda weeks: {**FAKE_FORECAST, "call": len(calls) or calls.append(1)})
    monkeypatch.setattr(main.db, "set_cash_plan",
                        lambda *args, **kwargs: calls.append(("set", args)))
    monkeypatch.setattr(main.kpis, "cash_plan_summary", lambda: {"storage": "postgres"})

    client = TestClient(main.app)
    headers = _auth_header("testuser", "testpass")
    client.get("/api/cash", headers=headers)
    assert main._cash_cache
    resp = client.post("/api/cash/plan", headers=headers, json={
        "key": "ap:partner:5", "label": "Faber", "mode": "spread", "spread": 13})
    assert resp.status_code == 200
    assert main._cash_cache == {}


def test_setting_a_group_back_to_the_due_date_removes_the_plan_row(monkeypatch):
    cleared = []
    monkeypatch.setattr(main.db, "clear_cash_plan", lambda key: cleared.append(key))
    monkeypatch.setattr(main.kpis, "cash_plan_summary", lambda: {"storage": "postgres"})
    client = TestClient(main.app)
    resp = client.post("/api/cash/plan", headers=_auth_header("testuser", "testpass"),
                       json={"key": "ap:partner:5", "mode": "due"})
    assert resp.status_code == 200
    assert cleared == ["ap:partner:5"]


def test_an_unknown_payment_mode_is_refused():
    client = TestClient(main.app)
    resp = client.post("/api/cash/plan", headers=_auth_header("testuser", "testpass"),
                       json={"key": "ap:partner:5", "mode": "ooit"})
    assert resp.status_code == 400
    assert "betaalwijze" in resp.json()["detail"]


def test_applying_the_suggestions_writes_one_row_per_proposal(monkeypatch):
    written = []
    monkeypatch.setattr(main.kpis, "build_cash_forecast", lambda weeks=None: FAKE_FORECAST)
    monkeypatch.setattr(main.db, "set_cash_plan",
                        lambda key, label, mode, start_week, spread, note:
                        written.append((key, mode, spread)))
    monkeypatch.setattr(main.kpis, "cash_plan_summary", lambda: {"storage": "postgres"})
    client = TestClient(main.app)
    resp = client.post("/api/cash/plan/apply-suggestions",
                       headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 200
    assert written == [("ap:partner:5", "spread", 13)]
    assert resp.json()["applied"] == ["ap:partner:5"]


def test_an_extra_line_needs_a_name_an_amount_and_a_date():
    client = TestClient(main.app)
    headers = _auth_header("testuser", "testpass")
    assert client.post("/api/cash/extra", headers=headers,
                       json={"label": "", "amount": 100, "date": "2026-10-01"}
                       ).status_code == 400
    assert client.post("/api/cash/extra", headers=headers,
                       json={"label": "Lening", "amount": 0, "date": "2026-10-01"}
                       ).status_code == 400
    assert client.post("/api/cash/extra", headers=headers,
                       json={"label": "Lening", "amount": 100, "date": "1 oktober"}
                       ).status_code == 400


def test_adding_an_extra_line_without_a_database_gives_a_clear_message(monkeypatch):
    def boom(*args, **kwargs):
        raise main.db.StorageUnavailable("Er is geen DATABASE_URL ingesteld.")
    monkeypatch.setattr(main.db, "add_cash_extra", boom)
    client = TestClient(main.app)
    resp = client.post("/api/cash/extra", headers=_auth_header("testuser", "testpass"),
                       json={"label": "Financiering", "amount": 500000, "date": "2026-11-01"})
    assert resp.status_code == 503
    assert "DATABASE_URL" in resp.json()["detail"]


# --- Inloggen en rechten -------------------------------------------------------

from app import auth   # noqa: E402 — bewust hier, na de omgevingsvariabelen uit conftest


@pytest.fixture(autouse=True)
def _clean_sessions():
    auth._env_sessions.clear()
    auth._attempts.clear()
    yield
    auth._env_sessions.clear()
    auth._attempts.clear()


def _fake_db_user(monkeypatch, email="manager@basetime.nl", role="manager",
                  password="een lang genoeg wachtwoord", active=True):
    """Zet een nep-database neer met precies één gebruiker, zodat de rolcontrole
    getest kan worden zonder Postgres."""
    row = {
        "id": 7, "email": email, "name": "Test Gebruiker", "role": role,
        "password_hash": auth.hash_password(password), "active": active,
        "invite_hash": None, "invite_expires": None, "last_login_at": None,
    }
    sessions = {}
    monkeypatch.setattr(main.db, "configured", lambda: True)
    monkeypatch.setattr(auth.db, "configured", lambda: True)
    monkeypatch.setattr(auth.db, "get_user_by_email",
                        lambda e: row if e.lower() == email.lower() else None)
    monkeypatch.setattr(auth.db, "create_session",
                        lambda token_hash, user_id, hours: sessions.__setitem__(token_hash, user_id))
    monkeypatch.setattr(auth.db, "session_user", lambda th: row if th in sessions else None)
    monkeypatch.setattr(auth.db, "delete_session", lambda th: sessions.pop(th, None))
    monkeypatch.setattr(auth.db, "touch_login", lambda uid: None)
    monkeypatch.setattr(auth.db, "add_audit", lambda *a, **k: None)
    return row


def _login(client, email, password):
    return client.post("/login", data={"email": email, "password": password},
                       follow_redirects=False)


def test_a_wrong_password_does_not_get_you_a_session():
    client = TestClient(main.app)
    resp = _login(client, "testuser", "fout")
    assert resp.status_code == 401
    assert auth.COOKIE_NAME not in resp.cookies
    # bewust één melding voor beide fouten: anders verklap je welke accounts bestaan
    assert "Onbekende combinatie" in resp.text


def test_the_environment_admin_can_always_log_in():
    client = TestClient(main.app)
    resp = _login(client, "testuser", "testpass")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    me = client.get("/api/me").json()
    assert me["role"] == "beheerder"
    assert me["source"] == "omgeving"
    assert me["may_edit"] is True


def test_logging_out_ends_the_session():
    client = TestClient(main.app)
    _login(client, "testuser", "testpass")
    assert client.get("/api/me").status_code == 200
    client.get("/logout", follow_redirects=False)
    assert client.get("/api/me").status_code == 401


def test_a_manager_may_read_the_figures(monkeypatch):
    _fake_db_user(monkeypatch, role="manager")
    monkeypatch.setattr(main.kpis, "build_dashboard_payload", lambda **kw: FAKE_PAYLOAD)
    main._cache.clear()
    client = TestClient(main.app)
    _login(client, "manager@basetime.nl", "een lang genoeg wachtwoord")
    assert client.get("/api/kpis").status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 200


def test_a_manager_may_not_see_the_booking_lines(monkeypatch):
    """De kern van de rechtenstructuur: dit endpoint is de doorklik naar de boeking,
    inclusief de personeelsrekeningen. Alleen verbergen in het scherm is niet genoeg."""
    _fake_db_user(monkeypatch, role="manager")
    client = TestClient(main.app)
    _login(client, "manager@basetime.nl", "een lang genoeg wachtwoord")
    resp = client.get("/api/pl/lines?codes=400100&date_from=2026-08-01&date_to=2026-09-01")
    assert resp.status_code == 403
    assert "Financieel" in resp.json()["detail"]


def test_a_manager_may_not_change_the_payment_plan(monkeypatch):
    _fake_db_user(monkeypatch, role="manager")
    client = TestClient(main.app)
    _login(client, "manager@basetime.nl", "een lang genoeg wachtwoord")
    resp = client.post("/api/cash/plan", json={"key": "ap:partner:5", "mode": "defer"})
    assert resp.status_code == 403


def test_a_manager_may_not_manage_users(monkeypatch):
    _fake_db_user(monkeypatch, role="manager")
    client = TestClient(main.app)
    _login(client, "manager@basetime.nl", "een lang genoeg wachtwoord")
    assert client.get("/api/users").status_code == 403


def test_a_financial_user_may_see_the_lines_but_not_manage_users(monkeypatch):
    _fake_db_user(monkeypatch, role="financieel")
    monkeypatch.setattr(main.kpis, "fetch_pl_lines",
                        lambda client, codes, start, end: {"lines": [], "total": 0.0})
    monkeypatch.setattr(main.kpis, "get_client", lambda: object())
    main._pl_lines_cache.clear()
    client = TestClient(main.app)
    _login(client, "manager@basetime.nl", "een lang genoeg wachtwoord")
    assert client.get(
        "/api/pl/lines?codes=430500&date_from=2026-08-01&date_to=2026-09-01"
    ).status_code == 200
    assert client.get("/api/users").status_code == 403


def test_an_api_call_without_a_session_is_refused_without_a_browser_popup():
    """401 zonder WWW-Authenticate: anders toont de browser zijn eigen inlogvenster in
    plaats van ons inlogscherm."""
    client = TestClient(main.app)
    resp = client.get("/api/pl")
    assert resp.status_code == 401
    assert "www-authenticate" not in {k.lower() for k in resp.headers}


def test_the_environment_account_still_works_over_basic_auth_for_a_scheduler(monkeypatch):
    """Een externe planner die POST /api/snapshot aanroept moet blijven werken."""
    monkeypatch.setattr(main.db, "configured", lambda: True)
    monkeypatch.setattr(main.snapshot.db, "configured", lambda: True)
    monkeypatch.setattr(main.snapshot.db, "snapshot_exists_today", lambda: True)
    monkeypatch.setattr(main.snapshot.db, "save_snapshot", lambda standen: None)
    monkeypatch.setattr(main.snapshot.kpis, "build_dashboard_payload", lambda **kw: FAKE_PAYLOAD)
    monkeypatch.setattr(auth.db, "add_audit", lambda *a, **k: None)
    client = TestClient(main.app)
    resp = client.post("/api/snapshot", headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 200


def test_a_personal_account_cannot_use_basic_auth(monkeypatch):
    """Persoonlijke accounts loggen in met een sessie; via basic-auth zou het wachtwoord
    bij elk verzoek meegaan."""
    _fake_db_user(monkeypatch, role="financieel")
    client = TestClient(main.app)
    resp = client.get("/api/pl",
                      headers=_auth_header("manager@basetime.nl", "een lang genoeg wachtwoord"))
    assert resp.status_code == 401


def test_a_disabled_account_cannot_log_in(monkeypatch):
    _fake_db_user(monkeypatch, role="financieel", active=False)
    client = TestClient(main.app)
    resp = _login(client, "manager@basetime.nl", "een lang genoeg wachtwoord")
    assert resp.status_code == 401


def test_too_many_failed_attempts_are_slowed_down():
    client = TestClient(main.app)
    for _ in range(config.LOGIN_MAX_ATTEMPTS):
        _login(client, "iemand@basetime.nl", "fout")
    resp = _login(client, "iemand@basetime.nl", "fout")
    assert resp.status_code == 429
    assert "Te veel mislukte pogingen" in resp.text


def test_an_admin_invites_a_user_and_never_handles_a_password(monkeypatch):
    created = {}
    monkeypatch.setattr(main.db, "configured", lambda: True)
    monkeypatch.setattr(auth.db, "configured", lambda: True)
    monkeypatch.setattr(main.db, "get_user_by_email", lambda e: None)
    monkeypatch.setattr(main.db, "create_user",
                        lambda email, name, role: created.update(
                            {"id": 12, "email": email, "name": name, "role": role}) or created)
    monkeypatch.setattr(auth.db, "set_invite",
                        lambda uid, invite_hash, hours: created.update({"invite": invite_hash}))
    monkeypatch.setattr(auth.db, "add_audit", lambda *a, **k: None)

    client = TestClient(main.app)
    _login(client, "testuser", "testpass")
    resp = client.post("/api/users", json={"email": "nieuw@basetime.nl", "name": "Nieuw",
                                           "role": "manager"})
    assert resp.status_code == 200
    body = resp.json()
    assert "/uitnodiging?token=" in body["invite_url"]
    # alleen de hash gaat de database in, nooit het token zelf
    token = body["invite_url"].split("token=")[1]
    assert created["invite"] == auth.token_hash(token)
    assert token not in created["invite"]


def test_a_role_that_does_not_exist_is_refused(monkeypatch):
    monkeypatch.setattr(main.db, "configured", lambda: True)
    monkeypatch.setattr(main.db, "get_user_by_email", lambda e: None)
    client = TestClient(main.app)
    _login(client, "testuser", "testpass")
    resp = client.post("/api/users", json={"email": "x@basetime.nl", "role": "directeur"})
    assert resp.status_code == 400
    assert "Onbekende rol" in resp.json()["detail"]


def test_managing_users_without_a_database_explains_why(monkeypatch):
    client = TestClient(main.app)
    _login(client, "testuser", "testpass")
    resp = client.get("/api/users")
    assert resp.status_code == 503
    assert "database" in resp.json()["detail"]


def test_the_invite_link_uses_https_behind_the_railway_proxy(monkeypatch):
    """Achter de proxy ziet de app zelf http; de link die de beheerder doorstuurt moet
    wél de https-buitenkant zijn."""
    monkeypatch.setattr(main.db, "configured", lambda: True)
    monkeypatch.setattr(auth.db, "configured", lambda: True)
    monkeypatch.setattr(main.db, "get_user_by_email", lambda e: None)
    monkeypatch.setattr(main.db, "create_user",
                        lambda email, name, role: {"id": 3, "email": email, "name": name,
                                                   "role": role, "active": True})
    monkeypatch.setattr(auth.db, "set_invite", lambda *a, **k: None)
    monkeypatch.setattr(auth.db, "add_audit", lambda *a, **k: None)
    client = TestClient(main.app)
    _login(client, "testuser", "testpass")
    resp = client.post("/api/users", json={"email": "x@basetime.nl", "role": "manager"},
                       headers={"x-forwarded-proto": "https"})
    assert resp.json()["invite_url"].startswith("https://")
