"""
Tests voor de FastAPI-routes: basic-auth-gedrag en de happy path van /api/kpis,
met app.kpis.build_dashboard_payload gemocked (dus geen echte Odoo-aanroep nodig).
"""

import base64

from fastapi.testclient import TestClient

from app import main


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
    main._cache["data"] = None
    main._cache["fetched_at"] = 0.0

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
    main._cache["data"] = dict(FAKE_PAYLOAD)
    main._cache["fetched_at"] = 0.0  # zo oud dat een refresh geforceerd wordt

    def failing_build_payload():
        raise RuntimeError("Odoo-authenticatie mislukt")

    monkeypatch.setattr(main.kpis, "build_dashboard_payload", failing_build_payload)

    client = TestClient(main.app)
    resp = client.get("/api/kpis", headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 200
    assert "Odoo-authenticatie mislukt" in resp.json()["stale_error"]


def test_api_kpis_returns_502_when_no_cache_and_odoo_fails(monkeypatch):
    main._cache["data"] = None
    main._cache["fetched_at"] = 0.0

    def failing_build_payload():
        raise RuntimeError("Odoo-authenticatie mislukt")

    monkeypatch.setattr(main.kpis, "build_dashboard_payload", failing_build_payload)

    client = TestClient(main.app)
    resp = client.get("/api/kpis", headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 502
