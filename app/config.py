"""
Configuratie voor het Basetime KPI-dashboard.

Alles wat per omgeving verschilt (Odoo-inloggegevens, dashboard-wachtwoord) komt uit
environment variables — zet die in Railway onder "Variables", niet in deze code.

De constanten onderin (rekeningcodes, kredietlimiet, vaste maandlasten) zijn specifiek voor
de administratie van Basetime B.V. in Odoo (basetimebv.odoo.com) zoals die was op 11 augustus
2026. Als het rekeningschema, de kredietlimiet of de vaste lasten wijzigen, pas ze hier aan —
er hoeft niets in kpis.py of odoo_client.py te veranderen.
"""

import os

from dotenv import load_dotenv

# Laadt variabelen uit een lokaal .env-bestand als dat bestaat (handig om dit project
# op je eigen laptop te draaien/testen). Op Railway zet je de variabelen gewoon onder
# "Variables" in de project-instellingen — load_dotenv() doet daar niets schadelijks,
# er is dan simpelweg geen .env-bestand om te laden.
load_dotenv()


def _get_env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(
            f"Environment variable {name} ontbreekt. Zet 'm in Railway onder Variables "
            f"(zie .env.example / README.md)."
        )
    return value


# --- Odoo-verbinding -------------------------------------------------------
# ODOO_URL: bv. https://basetimebv.odoo.com (zonder trailing slash, mag met of zonder)
# ODOO_DB: de database-naam. Vaak gelijk aan het subdomein, bv. "basetimebv"
# ODOO_USERNAME: het e-mailadres/gebruikersnaam waarmee wordt ingelogd
# ODOO_API_KEY: een Odoo API-sleutel (Instellingen > Mijn profiel > Accountbeveiliging
#               > API-sleutels > Nieuwe API-sleutel aanmaken). Gebruik GEEN gewoon
#               wachtwoord — een API-sleutel kan losstaand worden ingetrokken.
ODOO_URL = _get_env("ODOO_URL", required=True)
ODOO_DB = _get_env("ODOO_DB", required=True)
ODOO_USERNAME = _get_env("ODOO_USERNAME", required=True)
ODOO_API_KEY = _get_env("ODOO_API_KEY", required=True)

# --- Dashboard-beveiliging (HTTP basic-auth over de hele site) ------------
DASHBOARD_USER = _get_env("DASHBOARD_USER", required=True)
DASHBOARD_PASSWORD = _get_env("DASHBOARD_PASSWORD", required=True)

# --- Cache ------------------------------------------------------------------
# Hoe lang (in seconden) een opgehaalde KPI-set warm blijft voordat een nieuwe
# paginabezoek een verse Odoo-query triggert. 900s = 15 minuten. Zet lager als je
# vaker verse cijfers wilt, hoger om Odoo minder te belasten.
CACHE_TTL_SECONDS = int(_get_env("CACHE_TTL_SECONDS", "900"))

# --- Periode ----------------------------------------------------------------
# Aantal volledige (afgesloten) kalendermaanden dat in de maandgrafieken komt.
# De lopende maand wordt altijd apart getoond (als "deze maand, tot nu"), niet
# meegenomen in de maandvergelijkingen, omdat die nooit een volledige maand is.
MONTHS_LOOKBACK = int(_get_env("MONTHS_LOOKBACK", "7"))

# Bovengrens op de periode die via het dashboard te kiezen is. Voorkomt dat iemand per
# ongeluk tien jaar aan boekingsregels opvraagt en Odoo daarmee onnodig belast.
MAX_PERIOD_MONTHS = int(_get_env("MAX_PERIOD_MONTHS", "36"))

# Aantal pipeline-deals dat in de "top kansen"-tabel komt.
TOP_PIPELINE_DEALS = int(_get_env("TOP_PIPELINE_DEALS", "10"))

# Aantal klanten dat in de "top klanten"-tabellen komt (omzetconcentratie en
# pipeline-concentratie).
TOP_CUSTOMERS_N = int(_get_env("TOP_CUSTOMERS_N", "5"))

# Aantal producten dat in de "grootste voorraadposten"-tabel komt (voorraadtab).
TOP_STOCK_PRODUCTS_N = int(_get_env("TOP_STOCK_PRODUCTS_N", "10"))

# Aantal maanden dat wordt meegenomen voor de klantconcentratie-KPI. Losstaand van
# MONTHS_LOOKBACK omdat concentratie over een kortere periode snel ruizig wordt (één
# grote order trekt het meteen scheef) — 12 maanden geeft een stabieler beeld.
CONCENTRATION_MONTHS_LOOKBACK = int(_get_env("CONCENTRATION_MONTHS_LOOKBACK", "12"))

# --- Bedrijfsspecifieke constanten (Basetime B.V.) --------------------------
# Rekeningcodes van de liquide-middelenrekeningen die samen "beschikbare cash" vormen.
# Gevonden via Odoo (account.account, account_type = asset_cash): Rabobank (103006),
# Rabo Businesscard (103001), Rabobank spaarrekening (103007).
BANK_ACCOUNT_CODES = _get_env("BANK_ACCOUNT_CODES", "103006,103001,103007").split(",")

# De hoofd-betaalrekening, gebruikt voor de maandelijkse netto-kasstroomgrafiek.
MAIN_OPERATING_BANK_CODE = _get_env("MAIN_OPERATING_BANK_CODE", "103006")

# Kredietlimiet op de hoofdrekening (negatief getal = hoe diep in het rood mag).
CREDIT_LIMIT = float(_get_env("CREDIT_LIMIT", "-150000"))

# Vaste maandlasten, gebruikt als structurele burn-rate-indicator (los van de
# wisselvallige werkelijke bankmutatie). Uit de kasstroomprognose van 7 augustus 2026.
FIXED_MONTHLY_COSTS = float(_get_env("FIXED_MONTHLY_COSTS", "130626"))

# Rekeningcodes die de "recurring/subscription"-omzet uit credit packages benaderen.
# Dit is geboekte omzet, GEEN deferred-revenue-saldo (zie README voor de nuance).
SUBSCRIPTION_ACCOUNT_CODES = _get_env(
    "SUBSCRIPTION_ACCOUNT_CODES", "800500,800510,800520"
).split(",")
