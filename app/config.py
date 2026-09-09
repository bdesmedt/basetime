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

# --- Inloggen en rechten ------------------------------------------------------
# Het account hierboven is de beheerder die altijd werkt, ook zonder database — je sleutel
# als er iets misgaat met de accounts. Persoonlijke accounts komen in de database.
# Hoe lang een sessie geldig blijft (uren) voordat er opnieuw ingelogd moet worden:
SESSION_HOURS = int(_get_env("SESSION_HOURS", "12"))
# Hoe lang een uitnodigingslink bruikbaar blijft (uren):
INVITE_HOURS = int(_get_env("INVITE_HOURS", "168"))
# Minimale wachtwoordlengte voor persoonlijke accounts:
MIN_PASSWORD_LENGTH = int(_get_env("MIN_PASSWORD_LENGTH", "12"))
# Na hoeveel mislukte pogingen binnen LOGIN_LOCKOUT_MINUTES het even niet meer mag:
LOGIN_MAX_ATTEMPTS = int(_get_env("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCKOUT_MINUTES = int(_get_env("LOGIN_LOCKOUT_MINUTES", "15"))
# Rol die een nieuwe gebruiker krijgt als er niets is gekozen:
DEFAULT_USER_ROLE = _get_env("DEFAULT_USER_ROLE", "manager")
# Het sessiecookie alleen over https meesturen. Aan laten staan; alleen uitzetten om
# lokaal op http te kunnen testen.
COOKIE_SECURE = _get_env("COOKIE_SECURE", "1") not in ("0", "false", "False", "")

# --- Database (alleen voor de eigen W&V-indeling) ---------------------------
# Railway vult DATABASE_URL automatisch zodra je een Postgres-database aan het project
# toevoegt en die aan deze service koppelt. Blijft de variabele leeg, dan werkt het
# dashboard gewoon door: de W&V-tab toont dan de indeling zoals die in Odoo staat en
# meldt erbij dat wijzigingen niet bewaard kunnen worden.
DATABASE_URL = _get_env("DATABASE_URL", "")

# Hoe lang (seconden) op de database gewacht wordt voordat we het opgeven en terugvallen
# op de Odoo-indeling. Kort houden: een trage database mag de pagina niet ophouden.
DATABASE_TIMEOUT = int(_get_env("DATABASE_TIMEOUT", "10"))

# --- Winst-en-verliesrapport in Odoo ----------------------------------------
# Het rapport (account.report) dat de rubrieksindeling van de W&V-tab bepaalt. Bij
# Basetime is dat id 25, "Profit and loss report V2" — de eigen kopie van Odoo's
# standaard W&V-rapport. Zie de W&V-tab: het rapport is puur het startpunt; afwijkingen
# worden in de database bewaard.
PL_REPORT_ID = int(_get_env("PL_REPORT_ID", "25"))

# De regel van dat rapport die het eindresultaat berekent. Alles waar deze regel (direct
# of indirect) op steunt, vormt de W&V-boom; losse memoblokken onderaan het rapport
# (bij Basetime: Stock, marge Locator One/Two) vallen daarmee vanzelf buiten de boom.
PL_RESULT_LINE_CODE = _get_env("PL_RESULT_LINE_CODE", "NL_RESNB_COPY")

# Sjabloon voor een directe link naar een record in Odoo, gebruikt door de doorklik op
# de W&V-tab ("open in Odoo"). De standaard is Odoo's eigen redirect-endpoint — datzelfde
# adres zit onder de knoppen in Odoo's notificatiemails, werkt in alle recente versies en
# stuurt een niet-ingelogde gebruiker eerst langs het inlogscherm en daarna naar het
# record. Werkt dit in een andere omgeving niet, dan kan het klassieke formaat er ook in:
#   {base}/web#id={id}&model={model}&view_type=form
ODOO_RECORD_URL_TEMPLATE = _get_env(
    "ODOO_RECORD_URL_TEMPLATE", "{base}/mail/view?model={model}&res_id={id}"
)

# Maximum aantal boekingsregels dat het doorklikscherm ophaalt. Boven dit aantal wordt
# de lijst afgekapt (met melding); het totaalbedrag blijft wel het volledige bedrag.
PL_DETAIL_LINE_LIMIT = int(_get_env("PL_DETAIL_LINE_LIMIT", "500"))

# --- Vastleggen van standen (momentopnames) ---------------------------------
# Het dashboard legt zelf één keer per dag de standen vast (cash, debiteuren, pijplijn,
# backlog) omdat Odoo die achteraf niet kan reconstrueren. Dat gebeurt automatisch zodra
# iemand de pagina opent, en daarnaast door een ingebouwde planner die blijft doorlopen
# als er een tijd niemand kijkt. Zo is er geen aparte cron-service nodig.
SNAPSHOT_SCHEDULER_ENABLED = _get_env("SNAPSHOT_SCHEDULER_ENABLED", "1") not in (
    "0", "false", "False", "nee", "off", ""
)

# Hoe vaak de planner kijkt óf de meting van vandaag er al staat. Staat hij er, dan doet
# de planner niets (één goedkope databasevraag). Alleen als hij ontbreekt, wordt er een
# verse set cijfers uit Odoo gehaald — dus maximaal één keer per dag.
SNAPSHOT_CHECK_MINUTES = int(_get_env("SNAPSHOT_CHECK_MINUTES", "60"))

# Wachttijd na het opstarten voordat de planner voor het eerst kijkt. Geeft de webserver
# de kans om eerst gezond te worden (Railway doet een health check) voordat er een
# Odoo-aanroep overheen gaat.
SNAPSHOT_STARTUP_DELAY_SECONDS = int(_get_env("SNAPSHOT_STARTUP_DELAY_SECONDS", "90"))

# --- Kasprognose -------------------------------------------------------------
# Aantal weken vooruit. 13 weken is de gangbare horizon voor een kasprognose: ver genoeg
# om een probleem te zien aankomen, kort genoeg om nog te kunnen sturen.
FORECAST_WEEKS = int(_get_env("FORECAST_WEEKS", "13"))

# Loonkosten lopen niet als factuur door Odoo en staan dus niet bij de crediteuren, maar
# ze zijn wel de grootste vaste uitgaande stroom. Het dashboard leidt het maandbedrag af
# uit deze grootboekreeksen, gemiddeld over de laatste volledige maanden. Let op: de
# managementvergoedingen (402000) zitten er BEWUST niet bij — die worden gefactureerd en
# staan dus al bij de crediteuren; meetellen zou dubbeltellen zijn.
PAYROLL_ACCOUNT_CODE_PREFIXES = [
    p.strip() for p in _get_env("PAYROLL_ACCOUNT_CODE_PREFIXES", "400,4010,4011").split(",")
    if p.strip()
]
PAYROLL_LOOKBACK_MONTHS = int(_get_env("PAYROLL_LOOKBACK_MONTHS", "3"))
PAYROLL_PAY_DAY = int(_get_env("PAYROLL_PAY_DAY", "25"))

# Btw-rekeningen (af te dragen én voorbelasting). Het saldo over het lopende kwartaal is
# wat er aan het eind van de maand ná dat kwartaal betaald moet worden.
VAT_ACCOUNT_CODE_PREFIX = _get_env("VAT_ACCOUNT_CODE_PREFIX", "15")

# Bevestigde verkooporders die nog gefactureerd moeten worden, tellen mee als toekomstige
# ontvangst. Filters tegen de ruis: oude orders met een restje van een paar tientjes
# worden nooit meer gefactureerd en horen niet in een kasprognose.
BACKLOG_MIN_AMOUNT = float(_get_env("BACKLOG_MIN_AMOUNT", "1000"))
BACKLOG_MAX_AGE_MONTHS = int(_get_env("BACKLOG_MAX_AGE_MONTHS", "12"))

# Standaardaannames, in het dashboard bij te stellen.
# Hoeveel dagen ná de vervaldatum klanten gemiddeld betalen.
DEBTOR_DELAY_DAYS = int(_get_env("DEBTOR_DELAY_DAYS", "14"))
# Na hoeveel dagen een nog te versturen factuur binnenkomt (facturatie + betaaltermijn).
BACKLOG_INVOICE_DELAY_DAYS = int(_get_env("BACKLOG_INVOICE_DELAY_DAYS", "45"))
# Btw-opslag op nog te factureren orderbedragen (die staan excl. btw in Odoo).
BACKLOG_VAT_RATE = float(_get_env("BACKLOG_VAT_RATE", "0.21"))

# Regels voor het beredeneerde startvoorstel per leverancier. Het dashboard zet dit
# nooit zelf klaar als plan: het toont het als voorstel dat je in één klik overneemt of
# per leverancier corrigeert.
#   - Alles wat langer dan dit open staat, is feitelijk geen lopende verplichting meer
#     en wordt voorgesteld als "buiten de horizon" (bij Basetime: SODAQ, 2023/2024).
SUGGEST_DEFER_AGE_DAYS = int(_get_env("SUGGEST_DEFER_AGE_DAYS", "540"))
#   - Een achterstand boven dit bedrag kun je niet in één week ophoesten; voorstel is
#     spreiden over de horizon (bij Basetime: Faber Electronics).
SUGGEST_SPREAD_MIN_AMOUNT = float(_get_env("SUGGEST_SPREAD_MIN_AMOUNT", "25000"))

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

# Rekeningen die in Odoo wél het type "income" hebben, maar geen omzet zijn en dus
# buiten de netto-omzet-KPI horen. 892000 Exchange rate differences is financieel
# resultaat; die stond eerder wél in het dashboardcijfer, waardoor de omzet een paar
# euro afweek van Odoo's "Total Net Sales" (juli 2026: €87.965,32 vs €87.963,46).
REVENUE_EXCLUDED_ACCOUNT_CODES = [
    code for code in _get_env("REVENUE_EXCLUDED_ACCOUNT_CODES", "892000").split(",") if code
]

# Grootboekrekening met de overlopende (nog te nemen) omzet — voedt de tegel
# "Nog te nemen omzet" op het dashboard.
DEFERRED_REVENUE_ACCOUNT_CODE = _get_env("DEFERRED_REVENUE_ACCOUNT_CODE", "135000")

# Producten waarvan de omzet niet direct maar gespreid valt (creditpakketten,
# garantieverlengingen). Herkend aan het begin van de productnaam, omdat de interne
# referentie (default_code) in deze administratie niet is ingevuld.
DEFERRED_PRODUCT_NAME_PREFIXES = [
    p.strip() for p in _get_env("DEFERRED_PRODUCT_NAME_PREFIXES", "CR-,SC-").split(",") if p.strip()
]
