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
        {"date_order": f"{complete_start.isoformat()} 10:30:00", "amount_total": 4000.0},
        {"date_order": f"{current_start.isoformat()} 08:15:00", "amount_total": 1500.0},
    ]
    intake = kpis.fetch_order_intake(client, all_windows)

    assert intake == [4000.0, 1500.0]


def test_build_dashboard_payload_excludes_current_month_from_averages(monkeypatch):
    """De belangrijkste garantie van deze feature: een halve lopende maand mag de
    gemiddelden, de blended marge en de break-evenvergelijking NIET omlaag trekken."""
    monkeypatch.setattr(kpis, "get_client", lambda: FakeOdooClient())
    monkeypatch.setattr(kpis, "complete_month_windows", lambda n: [
        (date(2026, 6, 1), date(2026, 7, 1)),
        (date(2026, 7, 1), date(2026, 8, 1)),
    ])
    monkeypatch.setattr(kpis, "current_month_window", lambda: (date(2026, 8, 1), date(2026, 8, 13)))
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
    monkeypatch.setattr(kpis, "complete_month_windows", lambda n: [
        (date(2026, 6, 1), date(2026, 7, 1)),
        (date(2026, 7, 1), date(2026, 8, 1)),
    ])
    monkeypatch.setattr(kpis, "current_month_window", lambda: (date(2026, 8, 1), date(2026, 8, 13)))
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
