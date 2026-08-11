"""
Unit-tests voor app/kpis.py met een nagemaakte Odoo-client (geen netwerkverkeer).
De testcijfers zijn gebaseerd op de echte waarden die tijdens de bouw van dit
dashboard uit Odoo zijn gehaald (zie het KPI-voorstel-document), zodat deze tests
ook aantonen dat de rekenlogica dezelfde uitkomsten geeft als de handmatige analyse.
"""

from datetime import date

from app import kpis


def _iso(d):
    return d.strftime("%Y-%m-%d")


def _fake_group_row(groupby_field, start, end, **extra):
    return {
        "__range": {groupby_field: {"from": _iso(start), "to": _iso(end)}},
        **extra,
    }


class FakeOdooClient:
    """Duck-typed vervanger van OdooClient — kpis.py roept alleen search_read en
    read_group aan. Elke test wijst deze twee methodes toe aan een eigen functie
    die de verwachte canned response teruggeeft."""

    def read_group(self, model, domain, fields, groupby):
        raise NotImplementedError("stel client.read_group in deze test in")

    def search_read(self, model, domain, fields, limit=0, order=None):
        raise NotImplementedError("stel client.search_read in deze test in")


def test_complete_month_windows_returns_n_consecutive_complete_months():
    windows = kpis.complete_month_windows(3)
    assert len(windows) == 3
    for (start, end), (next_start, _next_end) in zip(windows, windows[1:]):
        assert start.day == 1
        assert end == next_start
    # de laatste window eindigt op de eerste dag van de huidige (lopende) maand
    today = date.today()
    assert windows[-1][1] == date(today.year, today.month, 1)


def test_revenue_and_cogs_matches_known_july_afsluiting_figures():
    windows = kpis.complete_month_windows(2)  # bv. juni + juli
    june_start, _ = windows[0]
    july_start, july_end = windows[1]

    # Odoo income-rekeningen hebben een credit- (negatieve) balance; -balance = omzet.
    rev_rows = [
        _fake_group_row("date:month", june_start, july_start, balance=-87834.11),
        _fake_group_row("date:month", july_start, july_end, balance=-87965.32),
    ]
    cogs_rows = [
        _fake_group_row("date:month", june_start, july_start, balance=25899.34),
        _fake_group_row("date:month", july_start, july_end, balance=46019.18),
    ]
    client = FakeOdooClient()
    # de eerste aanroep (omzet) en tweede (kostprijs) gaan naar hetzelfde model,
    # dus we simuleren met een teller die per aanroep een andere lijst teruggeeft
    calls = {"n": 0}

    def read_group(model, domain, fields, groupby):
        calls["n"] += 1
        return rev_rows if calls["n"] == 1 else cogs_rows

    client.read_group = read_group

    revenue, cogs = kpis.fetch_revenue_and_cogs(client, windows)
    assert revenue == [87834.11, 87965.32]
    assert cogs == [25899.34, 46019.18]

    margin_july = round((revenue[1] - cogs[1]) / revenue[1] * 100, 1)
    assert margin_july == 47.7  # sluit aan op de julisluiting (47,7%)


def test_bank_balance_now_sums_all_matched_accounts():
    client = FakeOdooClient()

    def search_read(model, domain, fields, limit=0, order=None):
        assert model == "account.account"
        return [{"id": 390, "code": "103006"}, {"id": 437, "code": "103001"}, {"id": 444, "code": "103007"}]

    def read_group(model, domain, fields, groupby):
        assert model == "account.move.line"
        return [
            {"account_id": [390, "Rabobank"], "balance": -89320.14},
            {"account_id": [437, "Rabo Businesscard"], "balance": 447.65},
            {"account_id": [444, "Spaarrekening"], "balance": 0.0},
        ]

    client.search_read = search_read
    client.read_group = read_group

    balance = kpis.fetch_bank_balance_now(client)
    assert balance == -88872.49


def test_purchase_backlog_splits_by_year():
    this_year = date.today().year
    rows = [
        {"name": "P00001", "amount_total": 1000.0, "date_planned": f"{this_year}-06-01 00:00:00"},
        {"name": "P00002", "amount_total": 500.0, "date_planned": f"{this_year + 1}-02-01 00:00:00"},
    ]
    client = FakeOdooClient()
    client.search_read = lambda model, domain, fields, limit=0, order=None: rows

    backlog = kpis.fetch_purchase_backlog(client)
    assert backlog["total"] == 1500.0
    assert backlog["current_year_or_earlier"] == 1000.0
    assert backlog["future_years"] == 500.0
    assert backlog["order_count"] == 2


def test_pipeline_excludes_closed_stages_and_computes_weighted_value():
    client = FakeOdooClient()
    calls = {"n": 0}

    def search_read(model, domain, fields, limit=0, order=None):
        calls["n"] += 1
        if model == "crm.stage":
            return [{"id": 10, "name": "Closed won (100%)"}, {"id": 11, "name": "Closed lost (0%)"}]
        assert model == "crm.lead"
        # controleer dat de gesloten stages daadwerkelijk uitgesloten worden
        assert ["stage_id", "not in", [10, 11]] in domain
        return [
            {"name": "Deal A", "stage_id": [9, "Onderhandeling (75%)"], "probability": 50.0, "expected_revenue": 1000000.0},
            {"name": "Deal B", "stage_id": [17, "Offerte (50%)"], "probability": 79.51, "expected_revenue": 400000.0},
        ]

    client.search_read = search_read

    pipeline = kpis.fetch_pipeline(client, top_n=10)
    assert pipeline["opportunity_count"] == 2
    assert pipeline["nominal_total"] == 1400000.0
    assert pipeline["weighted_total"] == round(0.5 * 1000000 + 0.7951 * 400000, 2)
    assert pipeline["top_deals"][0]["name"] == "Deal A"  # hoogste gewogen waarde eerst
