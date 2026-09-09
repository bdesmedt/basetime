"""
Unit-tests voor app/kpis.py met een nagemaakte Odoo-client (geen netwerkverkeer).
De testcijfers zijn gebaseerd op de echte waarden die tijdens de bouw van dit
dashboard uit Odoo zijn gehaald (zie het KPI-voorstel-document), zodat deze tests
ook aantonen dat de rekenlogica dezelfde uitkomsten geeft als de handmatige analyse.
"""

from datetime import date, timedelta

from app import config, kpis


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
    # opzoeken van uit te sluiten rekeningen (koersverschillen): hier geen
    client.search_read = lambda model, domain, fields, limit=0, order=None: []
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
            {"date_order": f"{june_order_date.isoformat()} 09:00:00", "amount_untaxed": 25478.0},
            {"date_order": f"{july_order_date.isoformat()} 14:30:00", "amount_untaxed": 145051.0},
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
            {"name": "S00010", "partner_id": [1, "Grupoalava"], "date_order": f"{june_start.isoformat()} 09:00:00", "amount_untaxed": 25478.0},
            {"name": "S00042", "partner_id": [2, "Sixense"], "date_order": f"{july_start.isoformat()} 14:30:00", "amount_untaxed": 145051.0},
            {"name": "S00099", "partner_id": False, "date_order": f"{july_start.isoformat()} 08:00:00", "amount_untaxed": 500.0},
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


# --- Voorraad ----------------------------------------------------------------

def test_stock_value_aggregates_quant_lines_per_product_and_sorts_by_value():
    """Locator One is serienummer-getrackt in Odoo: één stock.quant-regel per stuk,
    niet één regel per product — deze test simuleert precies dat patroon."""
    client = FakeOdooClient()

    def search_read(model, domain, fields, limit=0, order=None):
        if model == "product.product":
            return [{"id": 1, "list_price": 1389.0}, {"id": 23, "list_price": 110.0}]
        assert model == "stock.quant"
        assert ["location_id.usage", "=", "internal"] in domain
        return [
            {"product_id": [1, "HW-101 Locator One"], "quantity": 1, "value": 421.0},
            {"product_id": [1, "HW-101 Locator One"], "quantity": 1, "value": 421.0},
            {"product_id": [23, "AC-103 Charge/ reset cable"], "quantity": 38, "value": 1330.0},
        ]

    client.search_read = search_read
    client.read_group = lambda model, domain, fields, groupby: []
    stock = kpis.fetch_stock_value(client, top_n=10)

    assert stock["total"] == 2172.0
    assert stock["by_product"][0]["name"] == "AC-103 Charge/ reset cable"  # hoogste waarde eerst
    assert stock["by_product"][0]["value"] == 1330.0
    locator_one = next(p for p in stock["by_product"] if p["name"] == "HW-101 Locator One")
    assert locator_one["quantity"] == 2  # twee losse quant-regels samengevoegd
    assert locator_one["value"] == 842.0


def test_stock_value_detail_is_not_truncated_to_top_n():
    client = FakeOdooClient()

    def search_read(model, domain, fields, limit=0, order=None):
        if model == "product.product":
            return []
        return [
            {"product_id": [i, f"Product {i}"], "quantity": 1, "value": float(i)}
            for i in range(1, 15)
        ]

    client.search_read = search_read
    client.read_group = lambda model, domain, fields, groupby: []
    detail = kpis.fetch_stock_value_detail(client)
    assert len(detail) == 14
    assert detail[0]["name"] == "Product 14"  # hoogste waarde eerst


def test_stock_movements_classifies_supplier_in_and_customer_out_and_ignores_internal_transfer():
    windows = kpis.complete_month_windows(1)
    month_start, _ = windows[0]
    client = FakeOdooClient()

    def search_read(model, domain, fields, limit=0, order=None):
        assert model == "stock.move.line"
        assert ["state", "=", "done"] in domain
        base = f"{month_start.isoformat()} 09:00:00"
        return [
            # ontvangst van leverancier
            {"date": base, "quantity": 10.0, "product_id": [1, "Product A"],
             "location_usage": "supplier", "location_dest_usage": "internal"},
            # levering aan klant
            {"date": base, "quantity": 3.0, "product_id": [1, "Product A"],
             "location_usage": "internal", "location_dest_usage": "customer"},
            # interne overboeking tussen twee eigen magazijnlocaties — telt niet mee
            {"date": base, "quantity": 5.0, "product_id": [1, "Product A"],
             "location_usage": "internal", "location_dest_usage": "internal"},
        ]

    client.search_read = search_read
    movements = kpis.fetch_stock_movements(client, windows)

    assert movements["in"] == [10.0]
    assert movements["out"] == [3.0]


def test_stock_movement_detail_returns_only_in_and_out_sorted_by_date_desc():
    windows = kpis.complete_month_windows(2)
    prev_month_start, _ = windows[0]
    month_start, _ = windows[1]
    client = FakeOdooClient()

    def search_read(model, domain, fields, limit=0, order=None):
        return [
            {"date": f"{prev_month_start.isoformat()} 09:00:00", "quantity": 10.0, "product_id": [1, "Product A"],
             "location_usage": "supplier", "location_dest_usage": "internal"},
            {"date": f"{month_start.isoformat()} 15:00:00", "quantity": 3.0, "product_id": [1, "Product A"],
             "location_usage": "internal", "location_dest_usage": "customer"},
            {"date": f"{month_start.isoformat()} 12:00:00", "quantity": 5.0, "product_id": [1, "Product A"],
             "location_usage": "internal", "location_dest_usage": "internal"},
        ]

    client.search_read = search_read
    detail = kpis.fetch_stock_movement_detail(client, windows)

    assert len(detail) == 2  # de interne overboeking is uitgesloten
    assert detail[0]["direction"] == "Geleverd"  # latere maand eerst (nieuwste eerst)
    assert detail[1]["direction"] == "Ontvangen"


# --- Lopende (onvolledige) maand ---------------------------------------------

def test_current_month_window_starts_first_of_month_and_includes_today():
    start, end = kpis.current_month_window()
    today = date.today()
    assert start == date(today.year, today.month, 1)
    # einddatum is exclusief; vandaag moet er nog binnen vallen
    assert end == today + timedelta(days=1)
    assert start < end


def test_current_month_window_directly_follows_the_complete_months():
    """De lopende maand moet naadloos achter complete_month_windows() passen, want beide
    lijsten worden aan elkaar geplakt tot één reeks maandbuckets."""
    windows = kpis.complete_month_windows(3)
    current_start, _ = kpis.current_month_window()
    assert windows[-1][1] == current_start


def test_current_month_progress_reports_elapsed_days():
    progress = kpis.current_month_progress()
    today = date.today()
    assert progress["day_of_month"] == today.day
    assert progress["days_in_month"] in (28, 29, 30, 31)
    assert progress["label"] == kpis.DUTCH_MONTH_ABBR[today.month]
    assert 0 < progress["elapsed_pct"] <= 100


def test_fetchers_return_one_extra_bucket_when_current_month_is_appended():
    """Kern van de aanpak: de lopende maand rijdt mee in dezelfde query en levert
    gewoon één extra element op, zodat er geen tweede Odoo-aanroep nodig is."""
    windows = kpis.complete_month_windows(2)
    all_windows = windows + [kpis.current_month_window()]
    june_start, july_start = windows[0][0], windows[1][0]
    current_start, current_end = all_windows[-1]

    rev_rows = [
        _fake_group_row("date:month", june_start, july_start, balance=-1000.0),
        _fake_group_row("date:month", july_start, current_start, balance=-2000.0),
        _fake_group_row("date:month", current_start, current_end, balance=-500.0),
    ]
    client = FakeOdooClient()
    client.search_read = lambda model, domain, fields, limit=0, order=None: []
    calls = {"n": 0}

    def read_group(model, domain, fields, groupby):
        calls["n"] += 1
        return rev_rows if calls["n"] == 1 else []

    client.read_group = read_group
    revenue, cogs = kpis.fetch_revenue_and_cogs(client, all_windows)

    assert revenue == [1000.0, 2000.0, 500.0]
    assert cogs == [0, 0, 0]
    # slechts twee read_group-aanroepen (omzet + kostprijs), niet vier
    assert calls["n"] == 2


def test_order_intake_buckets_current_month_separately():
    windows = kpis.complete_month_windows(1)
    all_windows = windows + [kpis.current_month_window()]
    complete_start = windows[0][0]
    current_start = all_windows[-1][0]

    client = FakeOdooClient()
    client.search_read = lambda model, domain, fields, limit=0, order=None: [
        {"date_order": f"{complete_start.isoformat()} 10:30:00", "amount_untaxed": 4000.0},
        {"date_order": f"{current_start.isoformat()} 08:15:00", "amount_untaxed": 1500.0},
    ]
    intake = kpis.fetch_order_intake(client, all_windows)

    assert intake == [4000.0, 1500.0]


def test_build_dashboard_payload_excludes_current_month_from_averages(monkeypatch):
    """De belangrijkste garantie van deze feature: een halve lopende maand mag de
    gemiddelden, de blended marge en de break-evenvergelijking NIET omlaag trekken."""
    monkeypatch.setattr(kpis, "get_client", lambda: FakeOdooClient())
    # Datum-onafhankelijk: pak de ECHTE laatste twee volledige maanden, zodat de lopende
    # maand er altijd naadloos op aansluit. Met hardgecodeerde datums brak deze test
    # zodra de kalender een maand verder ging.
    real_windows = kpis.complete_month_windows(2)
    monkeypatch.setattr(kpis, "complete_month_windows", lambda n: real_windows)
    monkeypatch.setattr(kpis, "current_month_progress", lambda: {
        "label": "aug", "day_of_month": 12, "days_in_month": 31, "elapsed_pct": 39,
    })

    # laatste element = lopende maand, telkens bewust veel lager dan een volle maand
    monkeypatch.setattr(kpis, "fetch_revenue_and_cogs", lambda c, w: ([100000.0, 100000.0, 10000.0],
                                                                     [40000.0, 40000.0, 9000.0]))
    monkeypatch.setattr(kpis, "fetch_subscription_revenue", lambda c, w: [30000.0, 30000.0, 3000.0])
    monkeypatch.setattr(kpis, "fetch_order_intake", lambda c, w: [80000.0, 80000.0, 5000.0])
    monkeypatch.setattr(kpis, "fetch_cashflow", lambda c, w: [-20000.0, -20000.0, -1000.0])
    monkeypatch.setattr(kpis, "fetch_bank_balance_now", lambda c: -88872.49)
    monkeypatch.setattr(kpis, "fetch_purchase_backlog", lambda c: {})
    monkeypatch.setattr(kpis, "fetch_pipeline", lambda c, a, b: {})
    monkeypatch.setattr(kpis, "fetch_ar_ap_aging", lambda c, n: {})
    monkeypatch.setattr(kpis, "fetch_customer_revenue_concentration", lambda c, m, n: {})
    monkeypatch.setattr(kpis, "fetch_pipeline_movement", lambda c, w, partial_last=False: {"months": [], "categories": []})
    monkeypatch.setattr(kpis, "fetch_order_intake_deferred", lambda c, w: [0.0] * len(w))
    monkeypatch.setattr(kpis, "fetch_deferred_revenue_balance", lambda c: 222463.88)

    payload = kpis.build_dashboard_payload()

    # de getoonde reeksen bevatten alleen de twee VOLLEDIGE maanden
    assert payload["revenue"] == [100000.0, 100000.0]
    assert payload["order_intake"] == [80000.0, 80000.0]

    # gemiddelden op basis van volledige maanden, niet vervuild door de lopende maand
    assert payload["cashflow_avg"] == -20000.0
    assert payload["recurring_revenue_avg"] == 30000.0
    assert payload["order_intake_sum"] == 160000.0

    # blended marge = (200000 - 80000) / 200000 = 60%, niet omlaag getrokken door de
    # veel slechtere marge van de lopende maand (10%)
    assert payload["break_even"]["based_on_margin_pct"] == 60.0
    assert payload["break_even"]["latest_month_revenue"] == 100000.0

    # en de lopende maand staat apart, mét zijn eigen (lagere) cijfers
    current = payload["current_month"]
    assert current["label"] == "aug"
    assert current["day_of_month"] == 12
    assert current["revenue"] == 10000.0
    assert current["margin_pct"] == 10.0
    assert current["order_intake"] == 5000.0
    assert current["cashflow"] == -1000.0


def test_build_inventory_payload_excludes_current_month_from_coverage(monkeypatch):
    monkeypatch.setattr(kpis, "get_client", lambda: FakeOdooClient())
    # Datum-onafhankelijk: pak de ECHTE laatste twee volledige maanden, zodat de lopende
    # maand er altijd naadloos op aansluit. Met hardgecodeerde datums brak deze test
    # zodra de kalender een maand verder ging.
    real_windows = kpis.complete_month_windows(2)
    monkeypatch.setattr(kpis, "complete_month_windows", lambda n: real_windows)
    monkeypatch.setattr(kpis, "current_month_progress", lambda: {
        "label": "aug", "day_of_month": 12, "days_in_month": 31, "elapsed_pct": 39,
    })
    monkeypatch.setattr(kpis, "fetch_stock_value", lambda c, n: {"total": 400000.0, "by_product": []})
    monkeypatch.setattr(kpis, "fetch_stock_movements", lambda c, w: {
        "in": [100.0, 120.0, 8.0], "out": [90.0, 110.0, 6.0],
    })
    monkeypatch.setattr(kpis, "fetch_revenue_and_cogs", lambda c, w: ([0, 0, 0], [40000.0, 40000.0, 2000.0]))

    payload = kpis.build_inventory_payload()

    assert payload["movements"]["in"] == [100.0, 120.0]
    assert payload["movements"]["out"] == [90.0, 110.0]
    # dekking op gemiddelde kostprijs van VOLLEDIGE maanden (40.000), niet 27.333
    assert payload["coverage"]["avg_monthly_cogs"] == 40000.0
    assert payload["coverage"]["months"] == 10.0
    assert payload["current_month"]["movements_in"] == 8.0
    assert payload["current_month"]["movements_out"] == 6.0


# --- Mogelijke opbrengst/marge op de voorraad --------------------------------

def _stock_client(quants, invoice_rows, refund_rows, list_rows):
    """Bouwt een FakeOdooClient die de drie aanroepen van _stock_value_payload bedient:
    stock.quant (search_read), account.move.line (read_group, 2x) en product.product."""
    client = FakeOdooClient()

    def search_read(model, domain, fields, limit=0, order=None):
        if model == "product.product":
            return list_rows
        return quants

    calls = {"n": 0}

    def read_group(model, domain, fields, groupby):
        calls["n"] += 1
        return invoice_rows if calls["n"] == 1 else refund_rows

    client.search_read = search_read
    client.read_group = read_group
    return client


def test_stock_revenue_uses_realized_price_and_nets_out_credit_notes():
    """Creditnota's staan in Odoo met een positieve hoeveelheid én een positief
    price_subtotal op de regel. Zonder aftrek zou de gemiddelde prijs verkeerd
    uitkomen; deze test dekt precies dat af."""
    quants = [{"product_id": [1, "HW-101 Locator One"], "quantity": 10, "value": 4210.0}]
    # 100 verkocht voor 90.000 (= 900 p/st), waarvan 20 gecrediteerd voor 18.000
    # netto: 80 stuks voor 72.000 => 900 per stuk
    invoices = [{"product_id": [1, "HW-101 Locator One"], "quantity": 100, "price_subtotal": 90000.0}]
    refunds = [{"product_id": [1, "HW-101 Locator One"], "quantity": 20, "price_subtotal": 18000.0}]
    lists = [{"id": 1, "list_price": 1389.0}]

    stock = kpis.fetch_stock_value(_stock_client(quants, invoices, refunds, lists), top_n=10)
    product = stock["by_product"][0]

    assert product["realized_price"] == 900.0
    assert product["price_source"] == "gerealiseerd"
    assert product["revenue_expected"] == 9000.0        # 10 x 900
    assert product["revenue_list"] == 13890.0           # 10 x 1389 (theoretisch plafond)
    assert product["margin_expected"] == 4790.0         # 9000 - 4210
    assert stock["revenue_expected"] == 9000.0
    assert stock["margin_list"] == 9680.0               # 13890 - 4210


def test_stock_revenue_falls_back_to_list_price_without_sales_history():
    quants = [{"product_id": [5, "AC-101 Beam clamps"], "quantity": 9, "value": 252.0}]
    lists = [{"id": 5, "list_price": 75.0}]

    stock = kpis.fetch_stock_value(_stock_client(quants, [], [], lists), top_n=10)
    product = stock["by_product"][0]

    assert product["realized_price"] is None
    assert product["price_source"] == "catalogus"
    assert product["revenue_expected"] == 675.0  # 9 x 75, terugval op de catalogusprijs
    # en dat wordt eerlijk gerapporteerd als "leunt niet op gerealiseerde prijzen"
    assert stock["fallback_cost_value"] == 252.0
    assert stock["fallback_share_pct"] == 100.0


def test_stock_revenue_is_zero_for_products_without_any_price():
    """Showcase-/verzamelartikelen zonder verkoopprijs mogen geen opbrengst opleveren;
    hun kostprijs telt wel gewoon mee, zodat de marge daar eerlijk negatief op uitkomt."""
    quants = [{"product_id": [370, "HW-105 Locator One DUMMY"], "quantity": 23, "value": 1610.0}]
    lists = [{"id": 370, "list_price": 0.0}]

    stock = kpis.fetch_stock_value(_stock_client(quants, [], [], lists), top_n=10)
    product = stock["by_product"][0]

    assert product["price_source"] == "geen"
    assert product["revenue_expected"] == 0.0
    assert product["margin_expected"] == -1610.0


def test_stock_margin_percentages_use_revenue_as_denominator():
    quants = [{"product_id": [1, "Product"], "quantity": 10, "value": 400.0}]
    invoices = [{"product_id": [1, "Product"], "quantity": 10, "price_subtotal": 1000.0}]
    lists = [{"id": 1, "list_price": 200.0}]

    stock = kpis.fetch_stock_value(_stock_client(quants, invoices, [], lists), top_n=10)

    # verwacht: omzet 1000, kostprijs 400 => marge 600 = 60%
    assert stock["revenue_expected"] == 1000.0
    assert stock["margin_expected_pct"] == 60.0
    # catalogus: 10 x 200 = 2000, marge 1600 = 80%
    assert stock["margin_list_pct"] == 80.0


def test_stock_value_detail_includes_revenue_columns():
    quants = [{"product_id": [i, f"Product {i}"], "quantity": 2, "value": float(i)} for i in range(1, 15)]
    lists = [{"id": i, "list_price": 10.0} for i in range(1, 15)]

    detail = kpis.fetch_stock_value_detail(_stock_client(quants, [], [], lists))

    assert len(detail) == 14  # niet ingekort
    assert detail[0]["revenue_list"] == 20.0
    assert "margin_expected" in detail[0]


# --- Periodekeuze ------------------------------------------------------------

def test_resolve_windows_preset_returns_last_n_complete_months():
    windows, meta = kpis.resolve_windows(months=3)
    assert len(windows) == 3
    assert meta["mode"] == "months"
    assert meta["label_text"] == "laatste 3 volledige maanden"
    today = date.today()
    assert windows[-1][1] == date(today.year, today.month, 1)  # lopende maand valt erbuiten


def test_resolve_windows_range_snaps_to_whole_months():
    """Een eigen periode wordt afgerond op hele maanden: alle maandgrafieken en
    gemiddelden gaan daarvan uit, dus halve maanden zouden opnieuw vertekenen."""
    windows, meta = kpis.resolve_windows(
        date_from=date(2025, 11, 10), date_to=date(2026, 4, 20)
    )
    assert windows[0][0] == date(2025, 11, 1)   # begint bij de 1e van november
    assert windows[-1][1] == date(2026, 5, 1)   # april telt volledig mee
    assert len(windows) == 6
    assert meta["mode"] == "range"
    assert meta["label_text"] == "nov 2025 t/m apr 2026"


def test_resolve_windows_never_includes_the_running_month():
    today = date.today()
    windows, _meta = kpis.resolve_windows(
        date_from=date(today.year, today.month, 1), date_to=date(today.year + 1, 12, 31)
    )
    current_start = date(today.year, today.month, 1)
    assert all(end <= current_start for _start, end in windows)


def test_resolve_windows_caps_at_max_period_months():
    _windows, meta = kpis.resolve_windows(months=999)
    assert meta["months"] == kpis.config.MAX_PERIOD_MONTHS


def test_month_labels_include_year_when_period_spans_multiple_years():
    spanning = [(date(2025, 12, 1), date(2026, 1, 1)), (date(2026, 1, 1), date(2026, 2, 1))]
    assert kpis.month_labels_for(spanning) == ["dec '25", "jan '26"]
    within = [(date(2026, 1, 1), date(2026, 2, 1)), (date(2026, 2, 1), date(2026, 3, 1))]
    assert kpis.month_labels_for(within) == ["jan", "feb"]


# --- Pipelinebeweging --------------------------------------------------------

STAGE_FIELD_ID, REVENUE_FIELD_ID = 8931, 8935

PIPELINE_STAGES = [
    {"id": 12, "name": "Unqualified Lead", "sequence": 0, "is_won": False},
    {"id": 7, "name": "Introduction (10%)", "sequence": 6, "is_won": False},
    {"id": 17, "name": "Quotation (50%)", "sequence": 9, "is_won": False},
    {"id": 9, "name": "Negotiation (75%)", "sequence": 10, "is_won": False},
    {"id": 10, "name": "Closed won (100%)", "sequence": 12, "is_won": True},
    # let op: verloren heeft een HOGER volgnummer dan gewonnen in deze administratie
    {"id": 11, "name": "Closed lost (0%)", "sequence": 15, "is_won": False},
]


def _pipeline_client(leads, transitions):
    """leads: (id, huidige fase, omzet, aanmaakdatum). transitions: (msg_id, lead_id,
    oude fase, nieuwe fase, tijdstip)."""
    client = FakeOdooClient()

    def search_read(model, domain, fields, limit=0, order=None):
        if model == "crm.stage":
            return PIPELINE_STAGES
        if model == "ir.model.fields":
            return [{"id": STAGE_FIELD_ID, "name": "stage_id"},
                    {"id": REVENUE_FIELD_ID, "name": "expected_revenue"}]
        if model == "crm.lead":
            return [
                {"id": lid, "name": f"Kans {lid}", "stage_id": [0, stage],
                 "partner_id": False, "expected_revenue": rev, "create_date": created,
                 "active": True}
                for lid, stage, rev, created in leads
            ]
        if model == "mail.tracking.value":
            return [
                {"mail_message_id": [mid, ""], "field_id": [STAGE_FIELD_ID, "Stage"],
                 "create_date": at, "old_value_char": old, "new_value_char": new,
                 "old_value_float": 0.0, "new_value_float": 0.0}
                for mid, _lid, old, new, at in transitions
            ]
        if model == "mail.message":
            return [{"id": mid, "res_id": lid} for mid, lid, _o, _n, _a in transitions]
        raise AssertionError("onverwacht model: " + model)

    client.search_read = search_read
    return client


JULY = [(date(2026, 7, 1), date(2026, 8, 1))]


def test_pipeline_movement_ignores_corrections_within_the_same_month():
    """Echt waargenomen patroon: op 3 juli werd één kans binnen 90 seconden door zes
    fases geklikt en kwam uiteindelijk weer terug waar hij begon. Dat is correctiewerk
    in Odoo, geen pipelinebeweging — het nettoresultaat moet nul zijn."""
    leads = [(1, "Negotiation (75%)", 50000.0, "2026-01-05 09:00:00")]
    transitions = [
        (101, 1, "Negotiation (75%)", "Qualification Stage", "2026-07-03 11:59:22"),
        (102, 1, "Qualification Stage", "Quotation (50%)", "2026-07-03 11:59:52"),
        (103, 1, "Quotation (50%)", "Negotiation (75%)", "2026-07-03 12:00:49"),
    ]
    movement = kpis.fetch_pipeline_movement(_pipeline_client(leads, transitions), JULY)
    buckets = movement["months"][0]["buckets"]

    assert all(b["count"] == 0 for b in buckets.values())
    # en de open pijplijn staat aan begin en eind van de maand even hoog
    assert movement["months"][0]["open_start"]["value"] == 50000.0
    assert movement["months"][0]["open_end"]["value"] == 50000.0
    assert movement["months"][0]["net_value"] == 0.0


def test_pipeline_movement_counts_reopened_lost_deal_as_won():
    """Ook echt gebeurd: een eerder verloren kans die alsnog gewonnen wordt."""
    leads = [(1, "Closed won (100%)", 4.0, "2026-02-10 15:32:29")]
    transitions = [(101, 1, "Closed lost (0%)", "Closed won (100%)", "2026-07-06 14:10:27")]

    movement = kpis.fetch_pipeline_movement(_pipeline_client(leads, transitions), JULY)
    assert movement["months"][0]["buckets"]["gewonnen"]["count"] == 1
    assert movement["months"][0]["buckets"]["verloren"]["count"] == 0


def test_pipeline_movement_counts_lead_created_and_won_in_same_month_as_won():
    leads = [(1, "Closed won (100%)", 185.0, "2026-07-24 08:41:49")]
    transitions = [(101, 1, "Unqualified Lead", "Closed won (100%)", "2026-07-24 08:44:44")]

    buckets = kpis.fetch_pipeline_movement(_pipeline_client(leads, transitions), JULY)["months"][0]["buckets"]
    assert buckets["gewonnen"]["count"] == 1
    assert buckets["nieuw"]["count"] == 0  # winst gaat vóór 'nieuw'


def test_pipeline_movement_does_not_treat_lost_as_progress_despite_higher_sequence():
    """Closed lost heeft volgnummer 15 en Closed won 12; puur op volgorde vergelijken zou
    een verloren deal als vooruitgang tellen. Deze test bewaakt dat."""
    leads = [(1, "Closed lost (0%)", 75000.0, "2026-04-30 11:28:58")]
    transitions = [(101, 1, "Quotation (50%)", "Closed lost (0%)", "2026-07-09 14:38:18")]

    buckets = kpis.fetch_pipeline_movement(_pipeline_client(leads, transitions), JULY)["months"][0]["buckets"]
    assert buckets["verloren"]["count"] == 1
    assert buckets["vooruit"]["count"] == 0


def test_pipeline_movement_classifies_forward_and_backward_steps():
    leads = [
        (1, "Negotiation (75%)", 10000.0, "2026-01-01 09:00:00"),   # vooruit
        (2, "Unqualified Lead", 8000.0, "2026-01-01 09:00:00"),     # achteruit
    ]
    transitions = [
        (101, 1, "Introduction (10%)", "Negotiation (75%)", "2026-07-10 09:00:00"),
        (102, 2, "Quotation (50%)", "Unqualified Lead", "2026-07-11 09:00:00"),
    ]
    buckets = kpis.fetch_pipeline_movement(_pipeline_client(leads, transitions), JULY)["months"][0]["buckets"]

    assert buckets["vooruit"]["count"] == 1
    assert buckets["vooruit"]["value"] == 10000.0
    assert buckets["achteruit"]["count"] == 1
    assert buckets["achteruit"]["value"] == 8000.0


def test_pipeline_movement_detail_lists_every_moved_opportunity():
    leads = [
        (1, "Closed won (100%)", 10000.0, "2026-01-01 09:00:00"),
        (2, "Quotation (50%)", 5000.0, "2026-01-01 09:00:00"),  # beweegt niet
    ]
    transitions = [(101, 1, "Quotation (50%)", "Closed won (100%)", "2026-07-10 09:00:00")]

    detail = kpis.fetch_pipeline_movement_detail(_pipeline_client(leads, transitions), JULY)
    assert len(detail) == 1
    assert detail[0]["category"] == "gewonnen"
    assert detail[0]["stage_from"] == "Quotation (50%)"
    assert detail[0]["stage_to"] == "Closed won (100%)"


def test_current_month_is_only_appended_when_the_period_runs_up_to_now():
    """Bij een periode in het verleden hoort de lopende maand er niet bij — anders komt
    er een losse augustusstaaf naast januari te staan, met een gat ertussen."""
    recent = kpis.complete_month_windows(3)
    combined, has_current = kpis.windows_including_current_month(recent)
    assert has_current is True
    assert len(combined) == 4

    historic = [(date(2025, 11, 1), date(2025, 12, 1)), (date(2025, 12, 1), date(2026, 1, 1))]
    combined, has_current = kpis.windows_including_current_month(historic)
    assert has_current is False
    assert combined == historic


def test_build_dashboard_payload_omits_current_month_for_a_historic_period(monkeypatch):
    monkeypatch.setattr(kpis, "get_client", lambda: FakeOdooClient())
    monkeypatch.setattr(kpis, "fetch_revenue_and_cogs", lambda c, w: ([100.0] * len(w), [40.0] * len(w)))
    monkeypatch.setattr(kpis, "fetch_subscription_revenue", lambda c, w: [30.0] * len(w))
    monkeypatch.setattr(kpis, "fetch_order_intake", lambda c, w: [80.0] * len(w))
    monkeypatch.setattr(kpis, "fetch_cashflow", lambda c, w: [-20.0] * len(w))
    monkeypatch.setattr(kpis, "fetch_bank_balance_now", lambda c: 0.0)
    monkeypatch.setattr(kpis, "fetch_purchase_backlog", lambda c: {})
    monkeypatch.setattr(kpis, "fetch_pipeline", lambda c, a, b: {})
    monkeypatch.setattr(kpis, "fetch_ar_ap_aging", lambda c, n: {})
    monkeypatch.setattr(kpis, "fetch_customer_revenue_concentration", lambda c, m, n: {})
    monkeypatch.setattr(kpis, "fetch_pipeline_movement", lambda c, w, partial_last=False: {"months": [], "categories": []})
    monkeypatch.setattr(kpis, "fetch_order_intake_deferred", lambda c, w: [0.0] * len(w))
    monkeypatch.setattr(kpis, "fetch_deferred_revenue_balance", lambda c: 222463.88)

    payload = kpis.build_dashboard_payload(
        date_from=date(2025, 11, 1), date_to=date(2026, 1, 31)
    )

    assert payload["current_month"] is None
    assert len(payload["revenue"]) == 3  # nov, dec, jan — geen extra lopende maand
    assert len(payload["window"]["labels"]) == 3


def _pipeline_client_full(leads, transitions):
    """Als _pipeline_client, maar met expliciete archiefstatus per kans:
    (id, fase, omzet, aanmaakdatum, active)."""
    client = FakeOdooClient()

    def search_read(model, domain, fields, limit=0, order=None):
        if model == "crm.stage":
            return PIPELINE_STAGES
        if model == "ir.model.fields":
            return [{"id": STAGE_FIELD_ID, "name": "stage_id"},
                    {"id": REVENUE_FIELD_ID, "name": "expected_revenue"},
                    {"id": 8928, "name": "active"}]
        if model == "crm.lead":
            return [
                {"id": lid, "name": f"Kans {lid}", "stage_id": [0, stage],
                 "partner_id": False, "expected_revenue": rev, "create_date": created,
                 "active": active}
                for lid, stage, rev, created, active in leads
            ]
        if model == "mail.tracking.value":
            return transitions
        if model == "mail.message":
            return [{"id": t["mail_message_id"][0], "res_id": t["_lead"]} for t in transitions]
        raise AssertionError("onverwacht model: " + model)

    client.search_read = search_read
    return client


def _stage_change(msg_id, lead_id, old, new, at):
    return {"mail_message_id": [msg_id, ""], "field_id": [STAGE_FIELD_ID, "Stage"],
            "create_date": at, "old_value_char": old, "new_value_char": new,
            "old_value_float": 0.0, "new_value_float": 0.0,
            "old_value_integer": 0, "new_value_integer": 0, "_lead": lead_id}


def test_archived_opportunity_in_an_open_stage_is_not_counted_as_open_pipeline():
    """De fout die de klant vond: 263 kansen stonden gearchiveerd in een open fase en
    telden samen voor €13,65 mln mee als openstaande pijplijn."""
    leads = [
        (1, "Quotation (50%)", 500000.0, "2026-01-05 09:00:00", False),  # gearchiveerd
        (2, "Quotation (50%)", 80000.0, "2026-01-05 09:00:00", True),    # echt open
    ]
    movement = kpis.fetch_pipeline_movement(_pipeline_client_full(leads, []), JULY)
    month = movement["months"][0]

    assert month["open_start"]["count"] == 1
    assert month["open_start"]["value"] == 80000.0
    assert month["open_end"]["value"] == 80000.0


def test_active_opportunity_in_a_lost_stage_is_not_counted_as_open_pipeline():
    """Het spiegelbeeld, zoals lead 2999 'Taludmeting 5 units': active = true terwijl
    de kans in Closed lost staat. Alleen op de archiefvlag filteren zou die als open
    pijplijn meetellen."""
    leads = [(2999, "Closed lost (0%)", 6000.0, "2025-09-01 07:18:42", True)]
    movement = kpis.fetch_pipeline_movement(_pipeline_client_full(leads, []), JULY)
    month = movement["months"][0]

    assert month["open_start"]["count"] == 0
    assert month["open_end"]["value"] == 0.0


def test_archiving_an_opportunity_counts_as_lost_in_that_month():
    """In Odoo wordt een verloren kans doorgaans gearchiveerd zonder dat de fase
    wijzigt. Dat moet als 'verloren' tellen in de maand van archiveren."""
    leads = [(1, "Quotation (50%)", 40000.0, "2026-01-05 09:00:00", False)]
    archived = [{
        "mail_message_id": [900, ""], "field_id": [8928, "Active"],
        "create_date": "2026-07-14 10:00:00",
        "old_value_char": False, "new_value_char": False,
        "old_value_float": 0.0, "new_value_float": 0.0,
        "old_value_integer": 1, "new_value_integer": 0, "_lead": 1,
    }]
    movement = kpis.fetch_pipeline_movement(_pipeline_client_full(leads, archived), JULY)
    month = movement["months"][0]

    assert month["buckets"]["verloren"]["count"] == 1
    assert month["buckets"]["verloren"]["value"] == 40000.0
    assert month["open_start"]["value"] == 40000.0  # begin juli nog open
    assert month["open_end"]["value"] == 0.0        # eind juli eruit


def test_lead_2999_appears_as_lost_in_the_month_it_was_closed():
    """Volledig doorgerekend voorbeeld met de echte historie van lead 2999."""
    leads = [(2999, "Closed lost (0%)", 6000.0, "2025-09-01 07:18:42", True)]
    transitions = [
        _stage_change(131450, 2999, "Unqualified Lead", "Quotation (50%)", "2025-09-30 06:41:25"),
        _stage_change(177424, 2999, "Quotation (50%)", "Closed lost (0%)", "2026-04-15 13:30:02"),
    ]
    windows = [
        (date(2026, 3, 1), date(2026, 4, 1)),
        (date(2026, 4, 1), date(2026, 5, 1)),
        (date(2026, 5, 1), date(2026, 6, 1)),
    ]
    movement = kpis.fetch_pipeline_movement(_pipeline_client_full(leads, transitions), windows)
    maart, april, mei = movement["months"]

    assert maart["open_start"]["value"] == 6000.0        # nog open in maart
    assert april["buckets"]["verloren"]["count"] == 1    # verloren in april
    assert april["buckets"]["verloren"]["value"] == 6000.0
    assert april["open_end"]["value"] == 0.0
    assert mei["buckets"]["verloren"]["count"] == 0      # niet nogmaals in mei


# --- Order intake: gespreide producten en overlopende omzet ------------------

def test_order_intake_deferred_only_counts_credit_and_warranty_products():
    windows = kpis.complete_month_windows(1)
    month_start = windows[0][0]
    client = FakeOdooClient()
    captured = {}

    def search_read(model, domain, fields, limit=0, order=None):
        if model == "sale.order.line":
            captured["domain"] = domain
            return [
                {"order_id": [10, "S001"], "price_subtotal": 2430.0},   # CR-006
                {"order_id": [11, "S002"], "price_subtotal": 348.75},   # SC-501
            ]
        if model == "sale.order":
            return [
                {"id": 10, "date_order": f"{month_start.isoformat()} 09:00:00"},
                {"id": 11, "date_order": f"{month_start.isoformat()} 11:00:00"},
            ]
        raise AssertionError("onverwacht model: " + model)

    client.search_read = search_read
    deferred = kpis.fetch_order_intake_deferred(client, windows)

    assert deferred == [2778.75]
    # het domein moet op de productnaam-prefixen filteren, met een OR ertussen
    assert "|" in captured["domain"]
    assert ["product_id.name", "=like", "CR-%"] in captured["domain"]
    assert ["product_id.name", "=like", "SC-%"] in captured["domain"]


def test_order_intake_deferred_is_zero_without_matching_lines():
    windows = kpis.complete_month_windows(2)
    client = FakeOdooClient()
    client.search_read = lambda model, domain, fields, limit=0, order=None: []
    assert kpis.fetch_order_intake_deferred(client, windows) == [0.0, 0.0]


def test_deferred_revenue_balance_is_reported_as_a_positive_amount():
    """De rekening staat credit (negatief); 'nog te nemen omzet' lees je als positief."""
    client = FakeOdooClient()
    client.search_read = lambda model, domain, fields, limit=0, order=None: [{"id": 126}]
    client.read_group = lambda model, domain, fields, groupby: [
        {"account_id": [126, "135000 Deferred revenue"], "balance": -222463.88}
    ]
    assert kpis.fetch_deferred_revenue_balance(client) == 222463.88


def test_dashboard_payload_splits_order_intake_into_direct_and_deferred(monkeypatch):
    monkeypatch.setattr(kpis, "get_client", lambda: FakeOdooClient())
    # Datum-onafhankelijk: pak de ECHTE laatste twee volledige maanden, zodat de lopende
    # maand er altijd naadloos op aansluit. Met hardgecodeerde datums brak deze test
    # zodra de kalender een maand verder ging.
    real_windows = kpis.complete_month_windows(2)
    monkeypatch.setattr(kpis, "complete_month_windows", lambda n: real_windows)
    monkeypatch.setattr(kpis, "current_month_progress", lambda: {
        "label": "aug", "day_of_month": 12, "days_in_month": 31, "elapsed_pct": 39,
    })
    monkeypatch.setattr(kpis, "fetch_revenue_and_cogs", lambda c, w: ([100000.0]*len(w), [40000.0]*len(w)))
    monkeypatch.setattr(kpis, "fetch_subscription_revenue", lambda c, w: [30000.0]*len(w))
    monkeypatch.setattr(kpis, "fetch_order_intake", lambda c, w: [80000.0, 90000.0, 10000.0])
    monkeypatch.setattr(kpis, "fetch_order_intake_deferred", lambda c, w: [20000.0, 25000.0, 3000.0])
    monkeypatch.setattr(kpis, "fetch_cashflow", lambda c, w: [-20000.0]*len(w))
    monkeypatch.setattr(kpis, "fetch_bank_balance_now", lambda c: 0.0)
    monkeypatch.setattr(kpis, "fetch_deferred_revenue_balance", lambda c: 222463.88)
    monkeypatch.setattr(kpis, "fetch_purchase_backlog", lambda c: {})
    monkeypatch.setattr(kpis, "fetch_pipeline", lambda c, a, b: {})
    monkeypatch.setattr(kpis, "fetch_ar_ap_aging", lambda c, n: {})
    monkeypatch.setattr(kpis, "fetch_customer_revenue_concentration", lambda c, m, n: {})
    monkeypatch.setattr(kpis, "fetch_pipeline_movement", lambda c, w, partial_last=False: {"months": [], "categories": []})

    payload = kpis.build_dashboard_payload()

    # de lopende maand is er afgesplitst; direct + gespreid telt op tot het totaal
    assert payload["order_intake"] == [80000.0, 90000.0]
    assert payload["order_intake_deferred"] == [20000.0, 25000.0]
    assert payload["order_intake_direct"] == [60000.0, 65000.0]
    assert payload["deferred_revenue_balance"] == 222463.88


def test_pipeline_movement_marks_the_running_month_and_ends_at_todays_standing():
    """Het orderboek is een momentopname, geen periodecijfer. De laatste kolom moet de
    stand van vandaag zijn — hetzelfde bedrag dat de pipelinetegel toont — en als lopend
    gemarkeerd staan zodat de winratio 'm overslaat."""
    leads = [
        (1, "Quotation (50%)", 500000.0, "2026-01-05 09:00:00", True),
        (2, "Negotiation (75%)", 250000.0, "2026-01-05 09:00:00", True),
    ]
    windows = kpis.complete_month_windows(1) + [kpis.current_month_window()]
    movement = kpis.fetch_pipeline_movement(
        _pipeline_client_full(leads, []), windows, partial_last=True
    )
    volle_maand, lopend = movement["months"]

    assert volle_maand["partial"] is False
    assert lopend["partial"] is True
    # geen wijzigingen, dus de stand van nu is gewoon de som van de open kansen
    assert lopend["open_end"]["value"] == 750000.0
    assert lopend["open_end"]["count"] == 2


def test_pipeline_movement_without_partial_flag_marks_nothing_as_running():
    leads = [(1, "Quotation (50%)", 1000.0, "2026-01-05 09:00:00", True)]
    movement = kpis.fetch_pipeline_movement(_pipeline_client_full(leads, []), JULY)
    assert movement["months"][0]["partial"] is False


# --- Winst- en verliesrekening ---------------------------------------------
#
# Een klein maar structureel representatief W&V-rapport: een groep met een
# aggregation-formule waarin een teken zit (bruto = omzet MIN kostprijs), een groep met
# een subgroep (bedrijfskosten > personeel), codereeksen die elkaar overlappen
# (400 / 4010 / 401200) en een memoblok onderaan dat buiten de resultaatberekening valt.

PL_LINES = [
    (10, "Bruto verkoopresultaat", "BRUTO", None, 1),
    (11, "Omzet", "OMZ", 10, 2),
    (12, "Kostprijs omzet", "COGS", 10, 3),
    (20, "Bedrijfskosten", "KOST", None, 4),
    (21, "Personeelskosten", None, 20, 5),
    (23, "Lonen", "LONEN", 21, 6),
    (24, "Sociale lasten", "SOC", 21, 7),
    (22, "Kantoorkosten", "KANT", 20, 8),
    (30, "Resultaat", "RES", None, 9),
    (90, "Memo kostprijs Locator", "MEMO", None, 10),
]
PL_EXPRS = [
    (11, "account_codes", "- 80"),
    (12, "account_codes", "7"),
    (10, "aggregation", "OMZ.balance - COGS.balance"),
    (23, "account_codes", "400"),
    (24, "account_codes", "4010 + 401200"),
    (22, "account_codes", "43"),
    (21, "aggregation", "LONEN.balance + SOC.balance"),
    (20, "aggregation", "LONEN.balance + SOC.balance + KANT.balance"),
    (30, "aggregation", "BRUTO.balance - KOST.balance"),
    (90, "account_codes", "700500"),
]
PL_ACCOUNTS = {
    "800100": "Omzet NL",
    "700500": "Kostprijs Locator One",
    "400100": "Brutolonen",
    "401000": "Sociale lasten werkgever",
    "401200": "Premies",
    "430500": "Abonnementen",
    "999999": "Rekening buiten elke reeks",
}
# saldo per rekening per maandsleutel (zoals Odoo ze teruggeeft: omzet negatief)
PL_BALANCES = {
    "800100": {0: -10000.0, 1: -12000.0},
    "700500": {0: 3000.0, 1: 4000.0},
    "400100": {0: 5000.0, 1: 5000.0},
    "401000": {0: 900.0, 1: 900.0},
    "401200": {0: 100.0, 1: 100.0},
    "430500": {0: 200.0, 1: 250.0},
    "999999": {0: 42.0, 1: 8.0},
}
PL_ACCOUNT_IDS = {code: 500 + i for i, code in enumerate(sorted(PL_ACCOUNTS))}


class FakePlClient(FakeOdooClient):
    """Nep-Odoo met bovenstaand rapport. `month_boundaries=False` bootst een Odoo na die
    bij read_group geen bruikbare maandgrenzen teruggeeft — dan hoort kpis.py terug te
    vallen op een query per maand."""

    def __init__(self, month_boundaries=True):
        self.month_boundaries = month_boundaries
        self.read_group_calls = []

    def search_read(self, model, domain, fields, limit=0, order=None):
        if model == "account.report":
            return [{"id": config.PL_REPORT_ID, "name": "Testrapport W&V"}]
        if model == "account.report.line":
            return [
                {"id": i, "name": n, "code": c, "parent_id": ([p, ""] if p else False), "sequence": s}
                for i, n, c, p, s in PL_LINES
            ]
        if model == "account.report.expression":
            return [
                {"report_line_id": [lid, ""], "label": "balance", "engine": e, "formula": f}
                for lid, e, f in PL_EXPRS
            ]
        if model == "account.account":
            wanted = set(domain[0][2])
            return [
                {"id": PL_ACCOUNT_IDS[c], "code": c, "name": n}
                for c, n in sorted(PL_ACCOUNTS.items())
                if PL_ACCOUNT_IDS[c] in wanted
            ]
        raise AssertionError(f"onverwacht model {model}")

    def read_group(self, model, domain, fields, groupby, lazy=True):
        self.read_group_calls.append((tuple(groupby), lazy))
        assert model == "account.move.line"
        start, end = domain[1][2], domain[2][2]
        windows = kpis.complete_month_windows(2)
        rows = []
        for index, (win_start, _win_end) in enumerate(windows):
            key = _iso(win_start)
            if not (start <= key < end):
                continue
            if not lazy and "date:month" not in groupby:
                continue
            for code, per_month in PL_BALANCES.items():
                if index not in per_month:
                    continue
                row = {"account_id": [PL_ACCOUNT_IDS[code], code], "balance": per_month[index]}
                if "date:month" in groupby and self.month_boundaries:
                    row["__range"] = {"date:month": {"from": key, "to": key}}
                rows.append(row)
        return rows


def _pl_payload(client=None, layout=None, months=2):
    client = client or FakePlClient()
    original = kpis.get_client
    kpis.get_client = lambda: client
    try:
        return kpis.build_pl_payload(
            months=months, layout=layout or {"overrides": {}, "custom": [], "storage": "geen"}
        )
    finally:
        kpis.get_client = original


def _pl_index(payload):
    nodes = {}

    def walk(items):
        for n in items:
            nodes[n["id"]] = n
            walk(n.get("children") or [])
    walk(payload["tree"])
    return nodes


def _pl_total(payload, node_id, key="cur"):
    """Telt een regel op zoals de browser dat doet, zodat de tests dezelfde uitkomst
    bewaken als het scherm laat zien."""
    nodes = _pl_index(payload)
    accounts = payload["accounts"]

    def value(node):
        if node["kind"] == "group":
            return sum(node["signs"][c["id"]] * value(c) for c in node["children"])
        if node["kind"] == "calc":
            return sum(r["sign"] * value(nodes[r["id"]]) for r in node["refs"])
        if node["kind"] == "rubriek":
            return sum(
                (sum(a["m"].values()) if key == "cur" else a["prev"])
                for a in accounts if a["rubriek"] == node["id"]
            )
        return 0.0

    return round(value(nodes[node_id]), 2)


def test_pl_longest_code_range_wins_when_ranges_overlap():
    payload = _pl_payload()
    rubriek = {a["code"]: a["rubriek"] for a in payload["accounts"]}
    assert rubriek["400100"] == "23"   # reeks "400"  -> Lonen
    assert rubriek["401000"] == "24"   # reeks "4010" is langer dan "400"
    assert rubriek["401200"] == "24"   # exact genoemd in de reeks van Sociale lasten
    assert rubriek["430500"] == "22"   # reeks "43"   -> Kantoorkosten


def test_pl_revenue_accounts_keep_odoo_sign_flip():
    payload = _pl_payload()
    omzet = next(a for a in payload["accounts"] if a["code"] == "800100")
    assert omzet["sign"] == -1
    # Odoo levert omzet als creditsaldo (negatief); in de W&V hoort hij positief te staan
    assert sum(omzet["m"].values()) == 22000.0
    assert _pl_total(payload, "11") == 22000.0


def test_pl_account_outside_every_code_range_lands_in_unallocated():
    payload = _pl_payload()
    los = next(a for a in payload["accounts"] if a["code"] == "999999")
    assert los["rubriek"] == kpis.UNALLOCATED_ID
    # en telt daardoor niet mee in het resultaat — net zoals in Odoo zelf
    assert _pl_total(payload, "30") == 22000.0 - 7000.0 - 12450.0


def test_pl_memo_block_stays_out_of_the_tree_and_does_not_steal_accounts():
    payload = _pl_payload()
    nodes = _pl_index(payload)
    assert "90" not in nodes, "het memoblok hoort niet in de W&V-boom te staan"
    kostprijs = next(a for a in payload["accounts"] if a["code"] == "700500")
    # de memoregel heeft de langere reeks (700500 vs 7) maar valt buiten de boom,
    # dus de rekening hoort gewoon bij de kostprijs omzet te blijven
    assert kostprijs["rubriek"] == "12"


def test_pl_override_moves_an_account_without_changing_the_result():
    layout = {"overrides": {"430500": "23"}, "custom": [], "storage": "postgres"}
    payload = _pl_payload(layout=layout)
    abonnementen = next(a for a in payload["accounts"] if a["code"] == "430500")
    assert abonnementen["rubriek"] == "23"          # nu bij Lonen
    assert abonnementen["default_rubriek"] == "22"  # volgens Odoo bij Kantoorkosten
    assert _pl_total(payload, "22") == 0.0
    assert _pl_total(payload, "30") == 22000.0 - 7000.0 - 12450.0


def test_pl_override_pointing_at_a_vanished_rubriek_falls_back_to_odoo():
    layout = {"overrides": {"430500": "999"}, "custom": [], "storage": "postgres"}
    payload = _pl_payload(layout=layout)
    abonnementen = next(a for a in payload["accounts"] if a["code"] == "430500")
    assert abonnementen["rubriek"] == "22"
    assert payload["layout"]["dangling"] == ["430500"]


def test_pl_custom_rubriek_is_added_under_its_parent_and_counts_with_a_plus():
    layout = {
        "overrides": {"430500": "custom:1"},
        "custom": [{"id": "custom:1", "name": "Marketing", "parent": "20"}],
        "storage": "postgres",
    }
    payload = _pl_payload(layout=layout)
    nodes = _pl_index(payload)
    assert "custom:1" in nodes
    assert nodes["20"]["signs"]["custom:1"] == 1
    assert _pl_total(payload, "custom:1") == 450.0
    # het totaal van de bedrijfskosten blijft kloppen: de eigen rubriek telt gewoon mee
    assert _pl_total(payload, "20") == 12450.0


def test_pl_monthly_columns_add_up_to_the_period_total():
    payload = _pl_payload()
    assert len(payload["months"]) >= 2
    for account in payload["accounts"]:
        maanden = sum(account["m"].values())
        assert abs(maanden - sum(account["m"].get(m["key"], 0.0) for m in payload["months"])) < 0.01


def test_pl_falls_back_to_one_query_per_month_when_odoo_gives_no_month_boundaries():
    client = FakePlClient(month_boundaries=False)
    payload = _pl_payload(client=client)
    # eerst de gecombineerde poging, daarna één query per maand
    assert (("account_id", "date:month"), False) in client.read_group_calls
    assert (("account_id",), True) in client.read_group_calls
    assert _pl_total(payload, "11") == 22000.0


def test_pl_reports_that_the_layout_is_not_stored_without_a_database():
    payload = _pl_payload(layout={"overrides": {}, "custom": [], "storage": "geen",
                                  "message": "Geen database gekoppeld"})
    assert payload["layout"]["storage"] == "geen"
    assert payload["layout"]["override_count"] == 0


# --- Doorklik naar de boekingsregels ----------------------------------------

class FakeLinesClient(FakeOdooClient):
    def __init__(self, line_count=3):
        self.line_count = line_count
        self.domains = []

    def search_read(self, model, domain, fields, limit=0, order=None):
        if model == "account.account":
            wanted = set(domain[0][2])
            return [
                {"id": PL_ACCOUNT_IDS[c], "code": c, "name": PL_ACCOUNTS[c]}
                for c in sorted(PL_ACCOUNTS) if c in wanted
            ]
        if model == "account.move.line":
            self.domains.append(domain)
            return [
                {
                    "id": i, "date": "2026-08-0%d" % (i + 1),
                    "move_id": [900 + i, "BILL/2026/00%d" % i],
                    "move_name": "BILL/2026/00%d" % i,
                    "name": "Regel %d" % i, "ref": "",
                    "partner_id": [7, "Leverancier BV"],
                    "journal_id": [3, "Inkoopfacturen"],
                    "account_id": [PL_ACCOUNT_IDS["430500"], "430500 Abonnementen"],
                    "balance": 100.0, "move_type": "in_invoice",
                }
                for i in range(self.line_count)
            ]
        raise AssertionError(model)

    def read_group(self, model, domain, fields, groupby, lazy=True):
        return [{"account_id": [PL_ACCOUNT_IDS["430500"], "430500"], "balance": 450.0}]


def test_pl_lines_use_the_same_scope_as_the_cell_and_report_the_full_total():
    client = FakeLinesClient(line_count=3)
    result = kpis.fetch_pl_lines(client, ["430500"], date(2026, 8, 1), date(2026, 9, 1))
    domain = dict((d[0], d) for d in client.domains[0])
    assert domain["parent_state"] == ["parent_state", "=", "posted"]
    assert domain["date"][1] in (">=", "<")
    assert ["date", ">=", "2026-08-01"] in client.domains[0]
    assert ["date", "<", "2026-09-01"] in client.domains[0]
    # het totaal komt uit een aparte optelling, niet uit de (mogelijk afgekapte) lijst
    assert result["total"] == 450.0
    assert result["total_per_account"] == {"430500": 450.0}
    assert result["count"] == 3
    assert result["truncated"] is False


def test_pl_lines_are_capped_but_the_total_stays_complete():
    client = FakeLinesClient(line_count=12)
    result = kpis.fetch_pl_lines(client, ["430500"], date(2026, 8, 1), date(2026, 9, 1), limit=5)
    assert result["count"] == 5
    assert result["truncated"] is True
    assert result["total"] == 450.0


def test_pl_lines_carry_a_direct_link_to_the_booking_in_odoo():
    client = FakeLinesClient(line_count=1)
    result = kpis.fetch_pl_lines(client, ["430500"], date(2026, 8, 1), date(2026, 9, 1))
    url = result["lines"][0]["move_url"]
    assert url.startswith(config.ODOO_URL.rstrip("/"))
    assert "account.move" in url
    assert "900" in url


def test_pl_lines_for_an_unknown_account_code_return_nothing_instead_of_everything():
    class Empty(FakeOdooClient):
        def search_read(self, model, domain, fields, limit=0, order=None):
            return []
    result = kpis.fetch_pl_lines(Empty(), ["123456"], date(2026, 8, 1), date(2026, 9, 1))
    assert result == {"lines": [], "total": 0.0, "count": 0, "truncated": False}


def test_pl_lines_total_adds_up_when_odoo_returns_several_rows_per_account():
    """Bewaakt dat het totaal wordt opgeteld en niet overschreven — anders zou het
    doorklikscherm bij een fijnere groepering stilletjes één maand laten zien."""
    class MultiRow(FakeLinesClient):
        def read_group(self, model, domain, fields, groupby, lazy=True):
            return [
                {"account_id": [PL_ACCOUNT_IDS["430500"], "430500"], "balance": 200.0},
                {"account_id": [PL_ACCOUNT_IDS["430500"], "430500"], "balance": 250.0},
            ]

    result = kpis.fetch_pl_lines(MultiRow(1), ["430500"], date(2026, 8, 1), date(2026, 9, 1))
    assert result["total"] == 450.0
    assert result["total_per_account"] == {"430500": 450.0}


# --- Momentopnames ----------------------------------------------------------

SNAPSHOT_SOURCE = {
    "cash": {"available_now": -133725.41, "credit_limit": -150000.0, "credit_headroom": 16274.59},
    "aging": {
        "receivables": {"total": 210000.0, "buckets": [
            {"label": "Nog niet vervallen", "amount": 150000.0},
            {"label": "1-30 dagen", "amount": 40000.0},
            {"label": "90+ dagen", "amount": 20000.0},
        ]},
        "payables": {"total": 320000.0, "buckets": [
            {"label": "Nog niet vervallen", "amount": 320000.0},
        ]},
    },
    "pipeline": {"opportunity_count": 88, "nominal_total": 3694750.0, "weighted_total": 1842633.0},
    "purchase_backlog": {"total": 1873025.92},
    "deferred_revenue_balance": 222463.88,
    # stromen: die horen er juist NIET in, Odoo kan die altijd opnieuw uitrekenen
    "revenue": [87834.11, 87965.32],
    "order_intake": [104086.41, 79071.17],
}


def test_snapshot_captures_only_the_standing_figures():
    snap = kpis.snapshot_from_payload(SNAPSHOT_SOURCE)
    assert snap["cash"] == -133725.41
    assert snap["credit_headroom"] == 16274.59
    assert snap["receivables"] == 210000.0
    assert snap["payables"] == 320000.0
    assert snap["pipeline_weighted"] == 1842633.0
    assert snap["pipeline_count"] == 88
    assert snap["purchase_backlog"] == 1873025.92
    assert snap["deferred_revenue"] == 222463.88
    # stromen worden bewust niet vastgelegd — die zijn achteraf uit Odoo te halen
    assert "revenue" not in snap
    assert "order_intake" not in snap
    assert set(snap) == {key for key, _, _ in kpis.SNAPSHOT_METRICS}


def test_snapshot_overdue_receivables_exclude_what_is_not_due_yet():
    snap = kpis.snapshot_from_payload(SNAPSHOT_SOURCE)
    assert snap["receivables_overdue"] == 60000.0   # 40.000 + 20.000, niet de 150.000
    assert snap["payables"] == 320000.0


def test_snapshot_of_an_incomplete_payload_does_not_crash():
    snap = kpis.snapshot_from_payload({})
    assert set(snap) == {key for key, _, _ in kpis.SNAPSHOT_METRICS}
    assert snap["cash"] is None
    assert snap["receivables_overdue"] == 0.0


def test_snapshot_series_reports_that_nothing_is_recorded_yet(monkeypatch):
    monkeypatch.setattr(kpis.db, "load_snapshots", lambda days=365: [])
    monkeypatch.setattr(kpis.db, "configured", lambda: False)
    series = kpis.build_snapshot_series()
    assert series["count"] == 0
    assert series["first_date"] is None
    assert series["storage"] == "geen"
    assert [m["key"] for m in series["metrics"]] == [k for k, _, _ in kpis.SNAPSHOT_METRICS]


def test_snapshot_series_returns_the_points_oldest_first(monkeypatch):
    rows = [
        {"date": "2026-09-02", "cash": -100.0},
        {"date": "2026-09-09", "cash": -200.0},
    ]
    monkeypatch.setattr(kpis.db, "load_snapshots", lambda days=365: rows)
    monkeypatch.setattr(kpis.db, "configured", lambda: True)
    series = kpis.build_snapshot_series()
    assert series["first_date"] == "2026-09-02"
    assert series["last_date"] == "2026-09-09"
    assert series["count"] == 2
