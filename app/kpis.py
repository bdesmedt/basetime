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
from datetime import date, datetime, timezone
from typing import Any

from . import config
from .odoo_client import OdooClient, get_client

DUTCH_MONTH_ABBR = {
    1: "jan", 2: "feb", 3: "mrt", 4: "apr", 5: "mei", 6: "jun",
    7: "jul", 8: "aug", 9: "sep", 10: "okt", 11: "nov", 12: "dec",
}


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


def fetch_order_intake(client: OdooClient, windows: list[tuple[date, date]]):
    start, end = windows[0][0], windows[-1][1]
    rows = client.read_group(
        "sale.order",
        [
            ["state", "=", "sale"],
            ["date_order", ">=", _iso(start)],
            ["date_order", "<", _iso(end)],
        ],
        ["amount_total"],
        ["date_order:month"],
    )
    idx = _index_by_range_start(rows, "date_order:month")
    return [round(idx.get(_iso(mstart), {}).get("amount_total") or 0, 2) for mstart, _ in windows]


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


def fetch_pipeline(client: OdooClient, top_n: int) -> dict:
    closed_stages = client.search_read(
        "crm.stage",
        ["|", ["name", "ilike", "closed won"], ["name", "ilike", "closed lost"]],
        ["id", "name"],
    )
    excluded_ids = [s["id"] for s in closed_stages]
    domain: list[Any] = [["type", "=", "opportunity"], ["active", "=", True]]
    if excluded_ids:
        domain.append(["stage_id", "not in", excluded_ids])
    leads = client.search_read(
        "crm.lead", domain, ["name", "stage_id", "probability", "expected_revenue"]
    )

    stage_summary: dict[str, dict] = {}
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
    return {
        "opportunity_count": len(leads),
        "nominal_total": round(total_nominal, 2),
        "weighted_total": round(total_weighted, 2),
        "by_stage": stages_out,
        "top_deals": deals[:top_n],
    }


# --- Samenstellen van de complete dashboard-payload -------------------------

def build_dashboard_payload() -> dict:
    client = get_client()
    windows = complete_month_windows(config.MONTHS_LOOKBACK)
    month_labels = [DUTCH_MONTH_ABBR[w[0].month] for w in windows]

    revenue, cogs = fetch_revenue_and_cogs(client, windows)
    margin = [
        round((r - c) / r * 100, 1) if r else 0.0 for r, c in zip(revenue, cogs)
    ]
    recurring = fetch_subscription_revenue(client, windows)
    orders = fetch_order_intake(client, windows)
    cashflow = fetch_cashflow(client, windows)
    bank_now = fetch_bank_balance_now(client)
    backlog = fetch_purchase_backlog(client)
    pipeline = fetch_pipeline(client, config.TOP_PIPELINE_DEALS)

    credit_headroom = round(bank_now - config.CREDIT_LIMIT, 2)
    runway_months = credit_headroom / config.FIXED_MONTHLY_COSTS if config.FIXED_MONTHLY_COSTS else 0
    runway_weeks = round(runway_months * 4.345, 1)

    avg_cashflow = round(sum(cashflow) / len(cashflow), 2) if cashflow else 0
    avg_recurring = round(sum(recurring) / len(recurring), 2) if recurring else 0

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
    }
