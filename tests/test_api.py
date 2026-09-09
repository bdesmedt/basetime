"""
Tests voor de FastAPI-routes: basic-auth-gedrag en de happy path van /api/kpis,
met app.kpis.build_dashboard_payload gemocked (dus geen echte Odoo-aanroep nodig).
"""

import base64

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


def test_healthz_requires_no_auth():
    client = TestClient(main.app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_dashboard_requires_auth():
    client = TestClient(main.app)
    resp = client.get("/")
    assert resp.status_code == 401


def test_dashboard_rejects_wrong_password():
    client = TestClient(main.app)
    resp = client.get("/", headers=_auth_header("testuser", "wrong-password"))
    assert resp.status_code == 401


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
