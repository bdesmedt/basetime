"""
Alle KPI-berekeningen: elke functie hieronder is de programmatische versie van een
Odoo-query die tijdens de verkenning van dit dashboard handmatig is uitgevoerd en tegen
de julisluiting en de kasstroomprognose van Basetime is gecontroleerd (zie het
KPI-voorstel-document in het Claude-project "Basetime").

Belangrijk ontwerpprincipe: we matchen read_group-resultaten NOOIT op de tekst-labels die
Odoo teruggeeft (zoals "July 2026"), omdat die afhangen van de taal van de Odoo-gebruiker.
In plaats daarvan gebruiken we het `__range`-veld dat Odoo meestuurt, met de exacte
ISO-startdatum van elke groep — dat werkt onafhankelijk van taal/locale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import config
from .odoo_client import OdooClient, get_client

DUTCH_MONTH_ABBR = {
    1: "jan", 2: "feb", 3: "mrt", 4: "apr", 5: "mei", 6: "jun",
    7: "jul", 8: "aug", 9: "sep", 10: "okt", 11: "nov", 12: "dec",
}

# Volgorde waarin de ouderdomscategorieën altijd worden teruggegeven (ook als een bucket
# toevallig op nul staat) — zodat de grafiek voor debiteuren en crediteuren dezelfde
# x-as-categorieën gebruikt en de balken netjes uitlijnen.
AGING_BUCKET_ORDER = [
    "Nog niet vervallen", "1-30 dagen", "31-60 dagen", "61-90 dagen", "90+ dagen",
    "Onbekend",
]


def _aging_bucket_label(days_overdue: int | None) -> str:
    if days_overdue is None:
        return "Onbekend"
    if days_overdue <= 0:
        return "Nog niet vervallen"
    if days_overdue <= 30:
        return "1-30 dagen"
    if days_overdue <= 60:
        return "31-60 dagen"
    if days_overdue <= 90:
        return "61-90 dagen"
    return "90+ dagen"


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, 1)


def _iso(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def complete_month_windows(n: int) -> list[tuple[date, date]]:
    """De laatste n VOLLEDIGE kalendermaanden, oudste eerst. De lopende maand telt niet
    mee omdat die nooit compleet is en de vergelijking anders scheeftrekt."""
    today = date.today()
    this_month_start = date(today.year, today.month, 1)
    return [
        (_add_months(this_month_start, -i), _add_months(this_month_start, -i + 1))
        for i in range(n, 0, -1)
    ]


def current_month_window() -> tuple[date, date]:
    """De LOPENDE maand, van de 1e tot en met vandaag (einddatum exclusief, dus vandaag
    telt mee). Bewust gescheiden gehouden van complete_month_windows(): deze maand is per
    definitie onvolledig en hoort daarom niet mee te wegen in gemiddelden, de blended
    marge of de break-evenvergelijking — die blijven op volledige maanden gebaseerd.
    Omdat de startdatum de 1e van de maand is, kan dit venster gewoon achter de volledige
    maanden worden geplakt: alle fetch-functies bucketen per maandbegin en geven er dan
    één extra element voor terug."""
    today = date.today()
    return (date(today.year, today.month, 1), today + timedelta(days=1))


def current_month_progress() -> dict:
    """Hoe ver de lopende maand is gevorderd — zodat het dashboard erbij kan zetten dat
    een lagere staaf komt doordat de maand nog loopt, en niet doordat het slechter gaat."""
    today = date.today()
    month_start = date(today.year, today.month, 1)
    days_in_month = (_add_months(month_start, 1) - month_start).days
    return {
        "label": DUTCH_MONTH_ABBR[today.month],
        "day_of_month": today.day,
        "days_in_month": days_in_month,
        "elapsed_pct": round(today.day / days_in_month * 100),
    }


def _index_by_range_start(rows: list[dict], groupby_field: str) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for row in rows:
        rng = (row.get("__range") or {}).get(groupby_field)
        if not rng:
            continue
        idx[rng["from"]] = row
    return idx


def _resolve_account_ids(client: OdooClient, codes: list[str]) -> list[int]:
    rows = client.search_read("account.account", [["code", "in", codes]], ["id", "code"])
    if not rows:
        raise RuntimeError(
            f"Geen grootboekrekeningen gevonden met code in {codes}. "
            f"Controleer de rekeningcodes in config.py / de bijbehorende env vars."
        )
    return [r["id"] for r in rows]


# --- Individuele KPI-fetchers -----------------------------------------------

def fetch_revenue_and_cogs(client: OdooClient, windows: list[tuple[date, date]]):
    start, end = windows[0][0], windows[-1][1]
    rev_rows = client.read_group(
        "account.move.line",
        [
            ["account_id.account_type", "=", "income"],
            ["parent_state", "=", "posted"],
            ["date", ">=", _iso(start)],
            ["date", "<", _iso(end)],
        ],
        ["balance"],
        ["date:month"],
    )
    cogs_rows = client.read_group(
        "account.move.line",
        [
            ["account_id.account_type", "=", "expense_direct_cost"],
            ["parent_state", "=", "posted"],
            ["date", ">=", _iso(start)],
            ["date", "<", _iso(end)],
        ],
        ["balance"],
        ["date:month"],
    )
    rev_idx = _index_by_range_start(rev_rows, "date:month")
    cogs_idx = _index_by_range_start(cogs_rows, "date:month")
    revenue, cogs = [], []
    for mstart, _mend in windows:
        key = _iso(mstart)
        # income-rekeningen hebben van nature een credit- (negatieve) balance in Odoo;
        # -balance geeft de herkenbare positieve omzet.
        revenue.append(round(-(rev_idx.get(key, {}).get("balance") or 0), 2))
        cogs.append(round(cogs_idx.get(key, {}).get("balance") or 0, 2))
    return revenue, cogs


def fetch_subscription_revenue(client: OdooClient, windows: list[tuple[date, date]]):
    ids = _resolve_account_ids(client, config.SUBSCRIPTION_ACCOUNT_CODES)
    start, end = windows[0][0], windows[-1][1]
    rows = client.read_group(
        "account.move.line",
        [
            ["account_id", "in", ids],
            ["parent_state", "=", "posted"],
            ["date", ">=", _iso(start)],
            ["date", "<", _iso(end)],
        ],
        ["balance"],
        ["date:month"],
    )
    idx = _index_by_range_start(rows, "date:month")
    return [round(-(idx.get(_iso(mstart), {}).get("balance") or 0), 2) for mstart, _ in windows]


def fetch_order_intake(client: OdooClient, windows: list[tuple[date, date]]) -> list[float]:
    """Let op: `sale.order.date_order` is in Odoo een Datetime-veld (met tijdcomponent),
    anders dan de Date-velden (`date`, `invoice_date`) die de andere KPI's hierboven
    gebruiken. Odoo's read_group met groupby=['date_order:month'] geeft __range-grenzen
    terug als datetime-strings (bv. "2026-07-01 00:00:00", eventueel zelfs met een
    tijdzone-verschuiving als er geen expliciete UTC-context wordt meegegeven), terwijl
    _index_by_range_start() daar met een kale datumstring ("2026-07-01") naar op zoek
    ging — die twee matchten dus nooit, waardoor deze KPI altijd op 0 uitkwam voor élke
    maand. Om dat definitief te vermijden, halen we hier de losse orders op en bucketen
    we zelf in Python op het datumgedeelte van `date_order`."""
    start, end = windows[0][0], windows[-1][1]
    rows = client.search_read(
        "sale.order",
        [
            ["state", "=", "sale"],
            ["date_order", ">=", _iso(start)],
            ["date_order", "<", _iso(end)],
        ],
        ["date_order", "amount_total"],
    )
    totals: dict[str, float] = {_iso(mstart): 0.0 for mstart, _ in windows}
    for row in rows:
        date_order = row.get("date_order")
        if not date_order:
            continue
        order_date = datetime.strptime(date_order[:10], "%Y-%m-%d").date()
        month_key = _iso(date(order_date.year, order_date.month, 1))
        if month_key in totals:
            totals[month_key] += row.get("amount_total") or 0
    return [round(totals[_iso(mstart)], 2) for mstart, _ in windows]


def fetch_cashflow(client: OdooClient, windows: list[tuple[date, date]]):
    ids = _resolve_account_ids(client, [config.MAIN_OPERATING_BANK_CODE])
    start, end = windows[0][0], windows[-1][1]
    rows = client.read_group(
        "account.move.line",
        [
            ["account_id", "in", ids],
            ["parent_state", "=", "posted"],
            ["date", ">=", _iso(start)],
            ["date", "<", _iso(end)],
        ],
        ["balance"],
        ["date:month"],
    )
    idx = _index_by_range_start(rows, "date:month")
    return [round(idx.get(_iso(mstart), {}).get("balance") or 0, 2) for mstart, _ in windows]


def fetch_bank_balance_now(client: OdooClient) -> float:
    ids = _resolve_account_ids(client, config.BANK_ACCOUNT_CODES)
    rows = client.read_group(
        "account.move.line",
        [["account_id", "in", ids], ["parent_state", "=", "posted"]],
        ["balance"],
        ["account_id"],
    )
    return round(sum((r.get("balance") or 0) for r in rows), 2)


def fetch_purchase_backlog(client: OdooClient) -> dict:
    rows = client.search_read(
        "purchase.order",
        [["state", "in", ["purchase", "done"]], ["invoice_status", "!=", "invoiced"]],
        ["name", "amount_total", "date_planned"],
    )
    this_year = date.today().year
    total = sum(r.get("amount_total") or 0 for r in rows)
    future_years = sum(
        r.get("amount_total") or 0
        for r in rows
        if r.get("date_planned") and int(r["date_planned"][:4]) > this_year
    )
    return {
        "total": round(total, 2),
        "current_year_or_earlier": round(total - future_years, 2),
        "future_years": round(future_years, 2),
        "order_count": len(rows),
    }


def _fetch_pipeline_leads(client: OdooClient) -> list[dict]:
    """Alle open CRM-kansen (excl. gesloten-won/gesloten-verloren) — gedeeld door de
    samenvatting (fetch_pipeline) en het doorklik-detailscherm (fetch_pipeline_detail),
    zodat beide exact dezelfde selectie gebruiken."""
    closed_stages = client.search_read(
        "crm.stage",
        ["|", ["name", "ilike", "closed won"], ["name", "ilike", "closed lost"]],
        ["id", "name"],
    )
    excluded_ids = [s["id"] for s in closed_stages]
    domain: list[Any] = [["type", "=", "opportunity"], ["active", "=", True]]
    if excluded_ids:
        domain.append(["stage_id", "not in", excluded_ids])
    return client.search_read(
        "crm.lead",
        domain,
        ["name", "stage_id", "partner_id", "probability", "expected_revenue"],
    )


def fetch_pipeline(client: OdooClient, top_n: int, top_customers_n: int) -> dict:
    leads = _fetch_pipeline_leads(client)

    stage_summary: dict[str, dict] = {}
    customer_summary: dict[str, dict] = {}
    deals = []
    total_nominal = 0.0
    total_weighted = 0.0
    for lead in leads:
        prob = lead.get("probability") or 0
        rev = lead.get("expected_revenue") or 0
        weighted = prob / 100 * rev
        total_nominal += rev
        total_weighted += weighted

        stage_name = lead["stage_id"][1] if lead.get("stage_id") else "Onbekend"
        s = stage_summary.setdefault(stage_name, {"nominal": 0.0, "weighted": 0.0})
        s["nominal"] += rev
        s["weighted"] += weighted

        customer_name = lead["partner_id"][1] if lead.get("partner_id") else "Niet gekoppeld aan klant"
        c = customer_summary.setdefault(customer_name, {"nominal": 0.0, "weighted": 0.0})
        c["nominal"] += rev
        c["weighted"] += weighted

        deals.append(
            {
                "name": lead["name"],
                "stage": stage_name,
                "probability": round(prob, 2),
                "nominal": round(rev, 2),
                "weighted": round(weighted, 2),
            }
        )

    deals.sort(key=lambda d: -d["weighted"])
    stages_out = [
        {"stage": name, "nominal": round(v["nominal"], 2), "weighted": round(v["weighted"], 2)}
        for name, v in sorted(stage_summary.items(), key=lambda kv: -kv[1]["weighted"])
    ]
    customers_out = [
        {"name": name, "nominal": round(v["nominal"], 2), "weighted": round(v["weighted"], 2)}
        for name, v in sorted(customer_summary.items(), key=lambda kv: -kv[1]["weighted"])
    ][:top_customers_n]
    top_customer_share_pct = (
        round(sum(c["weighted"] for c in customers_out) / total_weighted * 100, 1)
        if total_weighted
        else 0.0
    )

    return {
        "opportunity_count": len(leads),
        "nominal_total": round(total_nominal, 2),
        "weighted_total": round(total_weighted, 2),
        "by_stage": stages_out,
        "top_deals": deals[:top_n],
        "by_customer": customers_out,
        "top_customer_share_pct": top_customer_share_pct,
    }


def fetch_ar_ap_aging(client: OdooClient, top_n: int) -> dict:
    """Ouderdomsanalyse van openstaande (nog niet volledig betaalde) debiteuren- en
    crediteurenposten, gebaseerd op de vervaldatum (`date_maturity`) van elke boekingsregel
    — valt terug op de boekingsdatum als er geen vervaldatum is vastgelegd."""
    today = date.today()

    def _fetch_side(account_type: str, sign: float) -> dict:
        rows = client.search_read(
            "account.move.line",
            [
                ["account_id.account_type", "=", account_type],
                ["parent_state", "=", "posted"],
                ["reconciled", "=", False],
            ],
            ["partner_id", "date_maturity", "date", "amount_residual"],
        )
        buckets: dict[str, float] = {label: 0.0 for label in AGING_BUCKET_ORDER}
        by_partner: dict[str, float] = {}
        total = 0.0
        for row in rows:
            amount = sign * (row.get("amount_residual") or 0)
            if not amount:
                continue
            due_str = row.get("date_maturity") or row.get("date")
            days_overdue = None
            if due_str:
                due_date = datetime.strptime(due_str[:10], "%Y-%m-%d").date()
                days_overdue = (today - due_date).days
            label = _aging_bucket_label(days_overdue)
            buckets[label] += amount
            total += amount
            partner_name = row["partner_id"][1] if row.get("partner_id") else "Onbekend"
            by_partner[partner_name] = by_partner.get(partner_name, 0.0) + amount

        top_partners = sorted(by_partner.items(), key=lambda kv: -kv[1])[:top_n]
        return {
            "total": round(total, 2),
            "buckets": [
                {"label": label, "amount": round(buckets[label], 2)}
                for label in AGING_BUCKET_ORDER
            ],
            "top_partners": [
                {"name": name, "amount": round(amount, 2)} for name, amount in top_partners
            ],
        }

    return {
        "receivables": _fetch_side("asset_receivable", 1.0),
        "payables": _fetch_side("liability_payable", -1.0),
    }


def fetch_customer_revenue_concentration(client: OdooClient, months: int, top_n: int) -> dict:
    """Aandeel van de grootste klanten in de gefactureerde omzet over de laatste `months`
    volledige maanden — een hoog aandeel betekent een kwetsbare afhankelijkheid van een
    klein aantal klanten."""
    windows = complete_month_windows(months)
    start, end = windows[0][0], windows[-1][1]
    rows = client.read_group(
        "account.move",
        [
            ["move_type", "in", ["out_invoice", "out_refund"]],
            ["state", "=", "posted"],
            ["invoice_date", ">=", _iso(start)],
            ["invoice_date", "<", _iso(end)],
        ],
        ["amount_untaxed_signed"],
        ["partner_id"],
    )
    total = sum(r.get("amount_untaxed_signed") or 0 for r in rows)
    ranked = sorted(rows, key=lambda r: -(r.get("amount_untaxed_signed") or 0))
    top = ranked[:top_n]
    top_sum = sum(r.get("amount_untaxed_signed") or 0 for r in top)
    return {
        "window_label": f"laatste {months} volledige maanden",
        "total_revenue": round(total, 2),
        "top_customers": [
            {
                "name": r["partner_id"][1] if r.get("partner_id") else "Onbekend",
                "amount": round(r.get("amount_untaxed_signed") or 0, 2),
                "share_pct": (
                    round((r.get("amount_untaxed_signed") or 0) / total * 100, 1)
                    if total
                    else 0.0
                ),
            }
            for r in top
        ],
        "top_n_share_pct": round(top_sum / total * 100, 1) if total else 0.0,
    }


# --- Voorraad ----------------------------------------------------------------
# Basetime gebruikt in Odoo een periodiek voorraadstelsel (zie de afsluitmemo van
# juli 2026): er wordt niet automatisch een boekhoudkundige voorraadwaarderingsregel
# per mutatie bijgehouden. `stock.quant.value` is Odoo's eigen (live) waardering per
# voorraadregel op standaard-/gemiddelde kostprijs — bruikbaar als actuele indicatie,
# maar niet gegarandeerd gelijk aan het grootboeksaldo. Zie ook de toelichting op het
# dashboard zelf.

def _fetch_stock_value_by_product(client: OdooClient) -> list[dict]:
    """Actuele voorraadwaarde per product in interne locaties (magazijnen), niet in
    klant-/leverancierslocaties. Eén product kan meerdere stock.quant-regels hebben
    (bv. bij serienummerregistratie, zoals bij Locator One) — die worden hier
    samengevoegd tot één regel per product."""
    rows = client.search_read(
        "stock.quant",
        [["location_id.usage", "=", "internal"], ["quantity", "!=", 0]],
        ["product_id", "quantity", "value"],
    )
    by_product: dict[Any, dict] = {}
    for row in rows:
        pid = row["product_id"][0] if row.get("product_id") else None
        name = row["product_id"][1] if row.get("product_id") else "Onbekend"
        entry = by_product.setdefault(pid, {"name": name, "quantity": 0.0, "value": 0.0})
        entry["quantity"] += row.get("quantity") or 0
        entry["value"] += row.get("value") or 0
    products = [
        {"name": p["name"], "quantity": round(p["quantity"], 2), "value": round(p["value"], 2)}
        for p in by_product.values()
    ]
    products.sort(key=lambda p: -p["value"])
    return products


def fetch_stock_value(client: OdooClient, top_n: int) -> dict:
    products = _fetch_stock_value_by_product(client)
    total = round(sum(p["value"] for p in products), 2)
    return {"total": total, "by_product": products[:top_n]}


def fetch_stock_value_detail(client: OdooClient) -> list[dict]:
    """Niet ingekort tot top-N, voor het doorklikscherm bij voorraadwaarde."""
    return _fetch_stock_value_by_product(client)


def _fetch_stock_move_lines(client: OdooClient, start: date, end: date) -> list[dict]:
    """Losse voorraadmutaties binnen een periode — gedeeld door de maandgrafiek
    (fetch_stock_movements) en het doorklikscherm (fetch_stock_movement_detail). Let op:
    `stock.move.line.date` is een Datetime-veld (zelfde categorie als
    `sale.order.date_order`) — we bucketen daarom zelf per maand in Python in plaats van
    op Odoo's read_group __range te vertrouwen (zie fetch_order_intake hierboven)."""
    return client.search_read(
        "stock.move.line",
        [
            ["state", "=", "done"],
            ["date", ">=", _iso(start)],
            ["date", "<", _iso(end)],
        ],
        ["date", "quantity", "product_id", "location_usage", "location_dest_usage"],
    )


def _classify_move_direction(row: dict) -> str | None:
    """'in' = ontvangst vanaf een niet-interne locatie (leverancier/productie) naar een
    interne locatie. 'out' = levering vanaf een interne locatie naar een niet-interne
    locatie (klant/afschrijving). Interne overboekingen (bv. tussen twee eigen
    magazijnlocaties) veranderen de totale voorraad niet en tellen niet mee."""
    src, dst = row.get("location_usage"), row.get("location_dest_usage")
    if src != "internal" and dst == "internal":
        return "in"
    if src == "internal" and dst != "internal":
        return "out"
    return None


def fetch_stock_movements(client: OdooClient, windows: list[tuple[date, date]]) -> dict:
    start, end = windows[0][0], windows[-1][1]
    rows = _fetch_stock_move_lines(client, start, end)
    units_in = {_iso(mstart): 0.0 for mstart, _ in windows}
    units_out = {_iso(mstart): 0.0 for mstart, _ in windows}
    for row in rows:
        direction = _classify_move_direction(row)
        if direction is None:
            continue
        move_date = row.get("date")
        if not move_date:
            continue
        d = datetime.strptime(move_date[:10], "%Y-%m-%d").date()
        month_key = _iso(date(d.year, d.month, 1))
        qty = row.get("quantity") or 0
        bucket = units_in if direction == "in" else units_out
        if month_key in bucket:
            bucket[month_key] += qty
    return {
        "in": [round(units_in[_iso(mstart)], 2) for mstart, _ in windows],
        "out": [round(units_out[_iso(mstart)], 2) for mstart, _ in windows],
    }


def fetch_stock_movement_detail(client: OdooClient, windows: list[tuple[date, date]]) -> list[dict]:
    """Elke losse mutatie binnen de getoonde maanden, voor het doorklikscherm bij
    voorraadbewegingen. Zelfde selectie/classificatie als fetch_stock_movements."""
    start, end = windows[0][0], windows[-1][1]
    rows = _fetch_stock_move_lines(client, start, end)
    detail = []
    for row in rows:
        direction = _classify_move_direction(row)
        if direction is None:
            continue
        detail.append(
            {
                "product": row["product_id"][1] if row.get("product_id") else "Onbekend",
                "date": (row.get("date") or "")[:10] or None,
                "quantity": round(row.get("quantity") or 0, 2),
                "direction": "Ontvangen" if direction == "in" else "Geleverd",
            }
        )
    detail.sort(key=lambda d: d.get("date") or "", reverse=True)
    return detail


def build_inventory_payload() -> dict:
    client = get_client()
    windows = complete_month_windows(config.MONTHS_LOOKBACK)
    month_labels = [DUTCH_MONTH_ABBR[w[0].month] for w in windows]

    # Zelfde aanpak als build_dashboard_payload: de lopende maand rijdt mee in dezelfde
    # query en wordt daarna afgesplitst, zodat de dekkingsberekening hieronder op
    # volledige maanden blijft rekenen (een halve maand kostprijs zou de dekking in
    # maanden anders kunstmatig hoog laten uitvallen).
    all_windows = windows + [current_month_window()]

    stock = fetch_stock_value(client, config.TOP_STOCK_PRODUCTS_N)
    movements_all = fetch_stock_movements(client, all_windows)
    _, cogs_all = fetch_revenue_and_cogs(client, all_windows)

    movements = {"in": movements_all["in"][:-1], "out": movements_all["out"][:-1]}
    cogs = cogs_all[:-1]
    avg_cogs = sum(cogs) / len(cogs) if cogs else 0
    months_coverage = round(stock["total"] / avg_cogs, 1) if avg_cogs else None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "labels": month_labels,
            "label_text": f"laatste {config.MONTHS_LOOKBACK} volledige maanden",
        },
        "stock_value": stock,
        "movements": movements,
        "coverage": {
            "months": months_coverage,
            "avg_monthly_cogs": round(avg_cogs, 2),
        },
        "current_month": {
            **current_month_progress(),
            "movements_in": movements_all["in"][-1],
            "movements_out": movements_all["out"][-1],
        },
    }


# --- Doorklik-detailschermen: volledige (niet-ingekorte) lijsten -----------
# Deze functies leveren de data voor de "Bekijk alle" doorklik-knoppen op het
# dashboard. Ze gebruiken zoveel mogelijk dezelfde domeinen/selecties als de
# samenvattingsfuncties hierboven, maar knippen niet af op top-N.

def fetch_ar_ap_aging_detail(client: OdooClient) -> dict:
    """Elke losse openstaande boekingsregel (niet alleen de top-partners), voor het
    doorklikscherm bij de ouderdomsanalyse."""
    today = date.today()

    def _fetch_side(account_type: str, sign: float) -> list[dict]:
        rows = client.search_read(
            "account.move.line",
            [
                ["account_id.account_type", "=", account_type],
                ["parent_state", "=", "posted"],
                ["reconciled", "=", False],
            ],
            ["partner_id", "move_id", "date_maturity", "date", "amount_residual"],
        )
        items = []
        for row in rows:
            amount = sign * (row.get("amount_residual") or 0)
            if not amount:
                continue
            due_str = row.get("date_maturity") or row.get("date")
            days_overdue = None
            if due_str:
                due_date = datetime.strptime(due_str[:10], "%Y-%m-%d").date()
                days_overdue = (today - due_date).days
            items.append(
                {
                    "partner": row["partner_id"][1] if row.get("partner_id") else "Onbekend",
                    "invoice": row["move_id"][1] if row.get("move_id") else "",
                    "due_date": due_str,
                    "days_overdue": days_overdue,
                    "bucket": _aging_bucket_label(days_overdue),
                    "amount": round(amount, 2),
                }
            )
        items.sort(key=lambda i: -(i["days_overdue"] if i["days_overdue"] is not None else -999999))
        return items

    return {
        "receivables": _fetch_side("asset_receivable", 1.0),
        "payables": _fetch_side("liability_payable", -1.0),
    }


def fetch_pipeline_detail(client: OdooClient) -> list[dict]:
    """Alle open kansen (niet alleen de top-N), inclusief klantnaam, voor het
    doorklikscherm bij de pijplijn."""
    leads = _fetch_pipeline_leads(client)
    deals = []
    for lead in leads:
        prob = lead.get("probability") or 0
        rev = lead.get("expected_revenue") or 0
        weighted = prob / 100 * rev
        deals.append(
            {
                "name": lead["name"],
                "customer": lead["partner_id"][1] if lead.get("partner_id") else "Niet gekoppeld aan klant",
                "stage": lead["stage_id"][1] if lead.get("stage_id") else "Onbekend",
                "probability": round(prob, 2),
                "nominal": round(rev, 2),
                "weighted": round(weighted, 2),
            }
        )
    deals.sort(key=lambda d: -d["weighted"])
    return deals


def fetch_customer_concentration_detail(client: OdooClient, months: int) -> list[dict]:
    """Alle klanten (niet alleen de top-N) met hun gefactureerde omzet over de laatste
    `months` volledige maanden, voor het doorklikscherm bij klantconcentratie."""
    windows = complete_month_windows(months)
    start, end = windows[0][0], windows[-1][1]
    rows = client.read_group(
        "account.move",
        [
            ["move_type", "in", ["out_invoice", "out_refund"]],
            ["state", "=", "posted"],
            ["invoice_date", ">=", _iso(start)],
            ["invoice_date", "<", _iso(end)],
        ],
        ["amount_untaxed_signed"],
        ["partner_id"],
    )
    total = sum(r.get("amount_untaxed_signed") or 0 for r in rows)
    ranked = sorted(rows, key=lambda r: -(r.get("amount_untaxed_signed") or 0))
    return [
        {
            "name": r["partner_id"][1] if r.get("partner_id") else "Onbekend",
            "amount": round(r.get("amount_untaxed_signed") or 0, 2),
            "share_pct": (
                round((r.get("amount_untaxed_signed") or 0) / total * 100, 1) if total else 0.0
            ),
        }
        for r in ranked
    ]


def fetch_order_intake_detail(client: OdooClient, windows: list[tuple[date, date]]) -> list[dict]:
    """Elke losse bevestigde order binnen de getoonde maanden (niet alleen het totaal per
    maand), voor het doorklikscherm bij order intake. Zelfde selectie/domein als
    fetch_order_intake."""
    start, end = windows[0][0], windows[-1][1]
    rows = client.search_read(
        "sale.order",
        [
            ["state", "=", "sale"],
            ["date_order", ">=", _iso(start)],
            ["date_order", "<", _iso(end)],
        ],
        ["name", "partner_id", "date_order", "amount_total"],
    )
    rows.sort(key=lambda r: r.get("date_order") or "", reverse=True)
    return [
        {
            "name": r.get("name") or "",
            "customer": r["partner_id"][1] if r.get("partner_id") else "Onbekend",
            "order_date": (r.get("date_order") or "")[:10] or None,
            "amount": round(r.get("amount_total") or 0, 2),
        }
        for r in rows
    ]


def fetch_purchase_backlog_detail(client: OdooClient) -> list[dict]:
    """Elke losse openstaande inkooporder (niet alleen het totaal), voor het
    doorklikscherm bij de inkoopbacklog."""
    rows = client.search_read(
        "purchase.order",
        [["state", "in", ["purchase", "done"]], ["invoice_status", "!=", "invoiced"]],
        ["name", "partner_id", "amount_total", "date_order", "date_planned"],
    )
    rows.sort(key=lambda r: -(r.get("amount_total") or 0))
    return [
        {
            "name": r.get("name") or "",
            "supplier": r["partner_id"][1] if r.get("partner_id") else "Onbekend",
            "amount": round(r.get("amount_total") or 0, 2),
            "order_date": (r.get("date_order") or "")[:10] or None,
            "planned_date": (r.get("date_planned") or "")[:10] or None,
        }
        for r in rows
    ]


def _detail_windows() -> list[tuple[date, date]]:
    """Periode voor de doorklikschermen: dezelfde maanden als de grafieken tonen, inclusief
    de lopende maand — anders zie je in de grafiek wel een staaf voor deze maand staan,
    maar ontbreken die regels in het bijbehorende detailoverzicht."""
    return complete_month_windows(config.MONTHS_LOOKBACK) + [current_month_window()]


DETAIL_FETCHERS = {
    "aging": lambda client: fetch_ar_ap_aging_detail(client),
    "pipeline": lambda client: fetch_pipeline_detail(client),
    "customer_concentration": lambda client: fetch_customer_concentration_detail(
        client, config.CONCENTRATION_MONTHS_LOOKBACK
    ),
    "purchase_backlog": lambda client: fetch_purchase_backlog_detail(client),
    "order_intake": lambda client: fetch_order_intake_detail(client, _detail_windows()),
    "stock_value": lambda client: fetch_stock_value_detail(client),
    "stock_movements": lambda client: fetch_stock_movement_detail(client, _detail_windows()),
}


def build_detail_payload(key: str) -> Any:
    """Volledige (niet-ingekorte) lijst voor het doorklik-detailscherm bij een KPI-sectie.
    Raist KeyError als `key` geen bekende sectie is (main.py zet dat om in een 404)."""
    if key not in DETAIL_FETCHERS:
        raise KeyError(key)
    client = get_client()
    return DETAIL_FETCHERS[key](client)


# --- Samenstellen van de complete dashboard-payload -------------------------

def build_dashboard_payload() -> dict:
    client = get_client()
    windows = complete_month_windows(config.MONTHS_LOOKBACK)
    month_labels = [DUTCH_MONTH_ABBR[w[0].month] for w in windows]

    # De lopende maand wordt in dezelfde Odoo-queries meegenomen (één extra maandbucket,
    # geen extra query) en daarna er weer afgesplitst, zodat alle bestaande arrays en
    # afgeleide cijfers hieronder op uitsluitend VOLLEDIGE maanden blijven rekenen.
    all_windows = windows + [current_month_window()]

    revenue_all, cogs_all = fetch_revenue_and_cogs(client, all_windows)
    recurring_all = fetch_subscription_revenue(client, all_windows)
    orders_all = fetch_order_intake(client, all_windows)
    cashflow_all = fetch_cashflow(client, all_windows)

    revenue, revenue_mtd = revenue_all[:-1], revenue_all[-1]
    cogs, cogs_mtd = cogs_all[:-1], cogs_all[-1]
    recurring, recurring_mtd = recurring_all[:-1], recurring_all[-1]
    orders, orders_mtd = orders_all[:-1], orders_all[-1]
    cashflow, cashflow_mtd = cashflow_all[:-1], cashflow_all[-1]

    margin = [
        round((r - c) / r * 100, 1) if r else 0.0 for r, c in zip(revenue, cogs)
    ]
    margin_mtd = round((revenue_mtd - cogs_mtd) / revenue_mtd * 100, 1) if revenue_mtd else 0.0

    bank_now = fetch_bank_balance_now(client)
    backlog = fetch_purchase_backlog(client)
    pipeline = fetch_pipeline(client, config.TOP_PIPELINE_DEALS, config.TOP_CUSTOMERS_N)
    aging = fetch_ar_ap_aging(client, config.TOP_CUSTOMERS_N)
    customer_concentration = fetch_customer_revenue_concentration(
        client, config.CONCENTRATION_MONTHS_LOOKBACK, config.TOP_CUSTOMERS_N
    )

    credit_headroom = round(bank_now - config.CREDIT_LIMIT, 2)
    runway_months = credit_headroom / config.FIXED_MONTHLY_COSTS if config.FIXED_MONTHLY_COSTS else 0
    runway_weeks = round(runway_months * 4.345, 1)

    avg_cashflow = round(sum(cashflow) / len(cashflow), 2) if cashflow else 0
    avg_recurring = round(sum(recurring) / len(recurring), 2) if recurring else 0

    # Break-evenomzet: bij de gemiddelde brutomarge over de getoonde periode, hoeveel omzet
    # is per maand nodig om de vaste maandlasten te dekken? Gebruikt de SOM van omzet/kostprijs
    # over de hele periode (niet het gemiddelde van de losse maandpercentages), zodat één
    # rare maand de uitkomst niet onevenredig laat schommelen.
    total_revenue = sum(revenue)
    total_cogs = sum(cogs)
    blended_margin_fraction = (total_revenue - total_cogs) / total_revenue if total_revenue else 0.0
    if blended_margin_fraction > 0:
        break_even_revenue = round(config.FIXED_MONTHLY_COSTS / blended_margin_fraction, 2)
        latest_revenue = revenue[-1] if revenue else 0.0
        gap_to_latest_month = round(break_even_revenue - latest_revenue, 2)
    else:
        break_even_revenue = None
        gap_to_latest_month = None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "months_lookback": config.MONTHS_LOOKBACK,
            "labels": month_labels,
            "label_text": f"laatste {config.MONTHS_LOOKBACK} volledige maanden",
        },
        "cash": {
            "available_now": bank_now,
            "credit_limit": config.CREDIT_LIMIT,
            "credit_headroom": credit_headroom,
        },
        "runway": {
            "months": round(runway_months, 2),
            "weeks": runway_weeks,
            "fixed_monthly_costs": config.FIXED_MONTHLY_COSTS,
        },
        "revenue": revenue,
        "cogs": cogs,
        "margin_pct": margin,
        "recurring_revenue": recurring,
        "recurring_revenue_avg": avg_recurring,
        "order_intake": orders,
        "order_intake_sum": round(sum(orders), 2),
        "cashflow": cashflow,
        "cashflow_avg": avg_cashflow,
        "purchase_backlog": backlog,
        "pipeline": pipeline,
        "aging": aging,
        "customer_concentration": customer_concentration,
        "break_even": {
            "monthly_revenue_needed": break_even_revenue,
            "based_on_margin_pct": round(blended_margin_fraction * 100, 1),
            "latest_month_revenue": revenue[-1] if revenue else 0.0,
            "gap_to_latest_month": gap_to_latest_month,
        },
        # Lopende maand: stand tot en met vandaag. Bewust NIET verwerkt in de arrays en
        # gemiddelden hierboven — die blijven volledige maanden vergelijken.
        "current_month": {
            **current_month_progress(),
            "revenue": revenue_mtd,
            "cogs": cogs_mtd,
            "margin_pct": margin_mtd,
            "recurring_revenue": recurring_mtd,
            "order_intake": orders_mtd,
            "cashflow": cashflow_mtd,
        },
    }
