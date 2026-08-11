"""
Unit-tests voor app/kpis.py met een nagemaakte Odoo-client (geen netwerkverkeer).
De testcijfers zijn gebaseerd op de echte waarden die tijdens de bouw van dit
dashboard uit Odoo zijn gehaald (zie het KPI-voorstel-document), zodat deze tests
ook aantonen dat de rekenlogica dezelfde uitkomsten geeft als de handmatige analyse.
"""

from datetime import date, timedelta

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


def test_order_intake_buckets_by_month_from_datetime_field():
    """sale.order.date_order is een Datetime-veld (met tijdcomponent), anders dan de
    Date-velden die de andere KPI's gebruiken — deze test simuleert dus echte Odoo-rijen
    met een tijdcomponent (en state=sale), niet de __range-mock-vorm van read_group."""
    windows = kpis.complete_month_windows(2)  # bv. juni + juli
    june_start, _ = windows[0]
    july_start, july_end = windows[1]
    june_order_date = date(june_start.year, june_start.month, 15)
    july_order_date = date(july_start.year, july_start.month, 3)

    client = FakeOdooClient()

    def search_read(model, domain, fields, limit=0, order=None):
        assert model == "sale.order"
        assert ["state", "=", "sale"] in domain
        return [
            {"date_order": f"{june_order_date.isoformat()} 09:00:00", "amount_total": 25478.0},
            {"date_order": f"{july_order_date.isoformat()} 14:30:00", "amount_total": 145051.0},
        ]

    client.search_read = search_read
    orders = kpis.fetch_order_intake(client, windows)

    assert orders == [25478.0, 145051.0]  # juni, juli — niet allebei 0


def test_order_intake_is_zero_for_months_with_no_confirmed_orders():
    windows = kpis.complete_month_windows(2)
    client = FakeOdooClient()
    client.search_read = lambda model, domain, fields, limit=0, order=None: []

    orders = kpis.fetch_order_intake(client, windows)
    assert orders == [0.0, 0.0]


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

    def search_read(model, domain, fields, limit=0, order=None):
        if model == "crm.stage":
            return [{"id": 10, "name": "Closed won (100%)"}, {"id": 11, "name": "Closed lost (0%)"}]
        assert model == "crm.lead"
        # controleer dat de gesloten stages daadwerkelijk uitgesloten worden
        assert ["stage_id", "not in", [10, 11]] in domain
        return [
            {
                "name": "Deal A",
                "stage_id": [9, "Onderhandeling (75%)"],
                "partner_id": [1, "Grupoalava"],
                "probability": 50.0,
                "expected_revenue": 1000000.0,
            },
            {
                "name": "Deal B",
                "stage_id": [17, "Offerte (50%)"],
                "partner_id": [2, "Sixense"],
                "probability": 79.51,
                "expected_revenue": 400000.0,
            },
        ]

    client.search_read = search_read

    pipeline = kpis.fetch_pipeline(client, top_n=10, top_customers_n=5)
    assert pipeline["opportunity_count"] == 2
    assert pipeline["nominal_total"] == 1400000.0
    assert pipeline["weighted_total"] == round(0.5 * 1000000 + 0.7951 * 400000, 2)
    assert pipeline["top_deals"][0]["name"] == "Deal A"  # hoogste gewogen waarde eerst
    assert pipeline["by_customer"][0]["name"] == "Grupoalava"
    # Grupoalava (500.000 gewogen) is bijna het hele gewogen totaal -> hoge concentratie
    assert pipeline["top_customer_share_pct"] > 80


def test_pipeline_falls_back_to_placeholder_when_no_partner_linked():
    client = FakeOdooClient()

    def search_read(model, domain, fields, limit=0, order=None):
        if model == "crm.stage":
            return []
        return [
            {"name": "Losse lead", "stage_id": [7, "Introductie (10%)"], "partner_id": False,
             "probability": 10.0, "expected_revenue": 5000.0},
        ]

    client.search_read = search_read
    pipeline = kpis.fetch_pipeline(client, top_n=10, top_customers_n=5)
    assert pipeline["by_customer"][0]["name"] == "Niet gekoppeld aan klant"


def test_ar_ap_aging_buckets_by_days_overdue_and_flips_payable_sign():
    today = date.today()
    overdue_45 = (today - timedelta(days=45)).isoformat()
    not_due_yet = (today + timedelta(days=10)).isoformat()
    client = FakeOdooClient()
    calls = {"n": 0}

    def search_read(model, domain, fields, limit=0, order=None):
        calls["n"] += 1
        assert model == "account.move.line"
        if calls["n"] == 1:  # debiteuren
            return [
                {"partner_id": [1, "Klant A"], "date_maturity": overdue_45, "date": overdue_45, "amount_residual": 1000.0},
                {"partner_id": [2, "Klant B"], "date_maturity": not_due_yet, "date": not_due_yet, "amount_residual": 500.0},
            ]
        # crediteuren: amount_residual staat van nature negatief (credit-balans)
        return [
            {"partner_id": [3, "Leverancier X"], "date_maturity": overdue_45, "date": overdue_45, "amount_residual": -2000.0},
        ]

    client.search_read = search_read
    aging = kpis.fetch_ar_ap_aging(client, top_n=5)

    ar_buckets = {b["label"]: b["amount"] for b in aging["receivables"]["buckets"]}
    assert ar_buckets["31-60 dagen"] == 1000.0
    assert ar_buckets["Nog niet vervallen"] == 500.0
    assert aging["receivables"]["total"] == 1500.0

    ap_buckets = {b["label"]: b["amount"] for b in aging["payables"]["buckets"]}
    assert ap_buckets["31-60 dagen"] == 2000.0  # teken omgedraaid -> positief "verschuldigd" bedrag
    assert aging["payables"]["total"] == 2000.0
    assert aging["payables"]["top_partners"][0]["name"] == "Leverancier X"


def test_customer_revenue_concentration_computes_top_n_share():
    client = FakeOdooClient()

    def read_group(model, domain, fields, groupby):
        assert model == "account.move"
        return [
            {"partner_id": [1, "Grupoalava"], "amount_untaxed_signed": 800000.0},
            {"partner_id": [2, "Sixense"], "amount_untaxed_signed": 150000.0},
            {"partner_id": [3, "Kleine klant"], "amount_untaxed_signed": 50000.0},
        ]

    client.read_group = read_group
    result = kpis.fetch_customer_revenue_concentration(client, months=12, top_n=2)
    assert result["total_revenue"] == 1000000.0
    assert result["top_customers"][0]["name"] == "Grupoalava"
    assert result["top_customers"][0]["share_pct"] == 80.0
    assert result["top_n_share_pct"] == 95.0


# --- Doorklik-detailschermen ("Bekijk alle") --------------------------------

def test_ar_ap_aging_detail_returns_every_line_sorted_by_days_overdue():
    today = date.today()
    overdue_45 = (today - timedelta(days=45)).isoformat()
    overdue_5 = (today - timedelta(days=5)).isoformat()
    client = FakeOdooClient()
    calls = {"n": 0}

    def search_read(model, domain, fields, limit=0, order=None):
        calls["n"] += 1
        if calls["n"] == 1:  # debiteuren
            return [
                {"partner_id": [1, "Klant A"], "move_id": [11, "INV/001"], "date_maturity": overdue_45, "date": overdue_45, "amount_residual": 1000.0},
                {"partner_id": [2, "Klant B"], "move_id": [12, "INV/002"], "date_maturity": overdue_5, "date": overdue_5, "amount_residual": 500.0},
            ]
        return [  # crediteuren
            {"partner_id": [3, "Leverancier X"], "move_id": [13, "BILL/001"], "date_maturity": overdue_45, "date": overdue_45, "amount_residual": -2000.0},
        ]

    client.search_read = search_read
    detail = kpis.fetch_ar_ap_aging_detail(client)

    assert len(detail["receivables"]) == 2
    # meest achterstallige (45 dagen) eerst
    assert detail["receivables"][0]["partner"] == "Klant A"
    assert detail["receivables"][0]["days_overdue"] == 45
    assert detail["receivables"][0]["bucket"] == "31-60 dagen"
    assert detail["receivables"][0]["invoice"] == "INV/001"
    assert len(detail["payables"]) == 1
    assert detail["payables"][0]["amount"] == 2000.0  # teken omgedraaid, net als de samenvatting


def test_pipeline_detail_returns_every_deal_with_customer_name():
    client = FakeOdooClient()

    def search_read(model, domain, fields, limit=0, order=None):
        if model == "crm.stage":
            return []
        return [
            {"name": "Deal A", "stage_id": [9, "Onderhandeling (75%)"], "partner_id": [1, "Grupoalava"],
             "probability": 50.0, "expected_revenue": 1000000.0},
            {"name": "Losse lead", "stage_id": [7, "Introductie (10%)"], "partner_id": False,
             "probability": 10.0, "expected_revenue": 5000.0},
        ]

    client.search_read = search_read
    deals = kpis.fetch_pipeline_detail(client)

    assert len(deals) == 2
    assert deals[0]["name"] == "Deal A"  # hoogste gewogen waarde eerst
    assert deals[0]["customer"] == "Grupoalava"
    assert deals[1]["customer"] == "Niet gekoppeld aan klant"


def test_customer_concentration_detail_returns_every_customer():
    client = FakeOdooClient()

    def read_group(model, domain, fields, groupby):
        return [
            {"partner_id": [1, "Grupoalava"], "amount_untaxed_signed": 800000.0},
            {"partner_id": [2, "Sixense"], "amount_untaxed_signed": 150000.0},
            {"partner_id": [3, "Kleine klant"], "amount_untaxed_signed": 50000.0},
        ]

    client.read_group = read_group
    rows = kpis.fetch_customer_concentration_detail(client, months=12)

    assert len(rows) == 3  # niet ingekort tot top-N, zoals de samenvattingsfunctie wel doet
    assert rows[0]["name"] == "Grupoalava"
    assert rows[0]["share_pct"] == 80.0
    assert rows[-1]["name"] == "Kleine klant"


def test_purchase_backlog_detail_returns_every_order_sorted_by_amount():
    client = FakeOdooClient()
    rows = [
        {"name": "P00001", "partner_id": [1, "Leverancier A"], "amount_total": 1000.0,
         "date_order": "2026-06-01 00:00:00", "date_planned": "2026-07-01 00:00:00"},
        {"name": "P00002", "partner_id": [2, "Leverancier B"], "amount_total": 5000.0,
         "date_order": "2026-05-01 00:00:00", "date_planned": "2026-08-01 00:00:00"},
    ]
    client.search_read = lambda model, domain, fields, limit=0, order=None: rows
    detail = kpis.fetch_purchase_backlog_detail(client)

    assert len(detail) == 2
    assert detail[0]["name"] == "P00002"  # grootste bedrag eerst
    assert detail[0]["supplier"] == "Leverancier B"
    assert detail[0]["planned_date"] == "2026-08-01"


def test_order_intake_detail_returns_every_order_sorted_by_date_desc():
    windows = kpis.complete_month_windows(2)
    june_start, _ = windows[0]
    july_start, _ = windows[1]
    client = FakeOdooClient()

    def search_read(model, domain, fields, limit=0, order=None):
        assert model == "sale.order"
        assert ["state", "=", "sale"] in domain
        return [
            {"name": "S00010", "partner_id": [1, "Grupoalava"], "date_order": f"{june_start.isoformat()} 09:00:00", "amount_total": 25478.0},
            {"name": "S00042", "partner_id": [2, "Sixense"], "date_order": f"{july_start.isoformat()} 14:30:00", "amount_total": 145051.0},
            {"name": "S00099", "partner_id": False, "date_order": f"{july_start.isoformat()} 08:00:00", "amount_total": 500.0},
        ]

    client.search_read = search_read
    detail = kpis.fetch_order_intake_detail(client, windows)

    assert len(detail) == 3
    assert detail[0]["name"] == "S00042"  # meest recente orderdatum eerst
    assert detail[0]["customer"] == "Sixense"
    assert detail[-1]["customer"] == "Grupoalava"
    assert any(d["customer"] == "Onbekend" for d in detail)  # geen partner gekoppeld


def test_build_detail_payload_raises_key_error_for_unknown_section():
    try:
        kpis.build_detail_payload("onbekende-sectie")
        assert False, "had een KeyError moeten opleveren"
    except KeyError:
        pass
