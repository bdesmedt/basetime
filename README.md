# Basetime KPI-dashboard

Een live KPI-dashboard voor Basetime B.V., dat de cijfers rechtstreeks uit Odoo
(basetimebv.odoo.com) haalt: beschikbare cash, runway-indicatie, netto cashburn, order
intake, recurring/subscription-omzet, brutomarge, inkoopbacklog, gewogen pipeline, en
(sinds deze versie) ouderdomsanalyse debiteuren/crediteuren, klantconcentratie in de
gefactureerde omzet, en de benodigde break-evenomzet per maand.

- **Backend**: Python (FastAPI), praat met Odoo via de officiële externe XML-RPC-API.
- **Frontend**: één HTML-pagina (in `app/templates/dashboard.html`), haalt de cijfers op
  via `/api/kpis` en tekent de tabellen/grafieken in de browser — geen build-stap nodig.
- **Beveiliging**: de hele site staat achter HTTP basic-auth (gebruikersnaam/wachtwoord).
- **Cache**: opgehaalde cijfers blijven 15 minuten warm (instelbaar), zodat niet elke
  paginabezoek meteen Odoo belast. Een "Vernieuwen"-knop op het dashboard forceert een
  verse ophaal-actie.

Dit project is voortgekomen uit een concept-dashboard (los HTML-bestand met een
momentopname) dat is besproken in het Claude-project "Basetime" — zie
`kpi-dashboard-voorstel-aug2026.md` daar voor de achtergrond en de aannames per KPI.

## Wat je nodig hebt

1. Een **Odoo API-sleutel** (zie stap 1 hieronder) — geen gewoon wachtwoord.
2. Een **GitHub-account** met een (nieuwe, lege) repository.
3. Een **Railway-account** (railway.app) — het gratis niveau is ruim voldoende voor dit
   dashboard.

Geen van deze accounts hoef je met iemand te delen: jij maakt ze aan, jij beheert ze.

---

## Stap 1 — Odoo API-sleutel aanmaken

1. Log in op `https://basetimebv.odoo.com` met het account waarmee het dashboard mag
   lezen (een account met leestoegang tot boekhouding, verkoop, inkoop en CRM volstaat —
   er wordt nergens geschreven).
2. Klik rechtsboven op je profielfoto → **Mijn profiel**.
3. Tabblad **Accountbeveiliging** → **API-sleutels** → **Nieuwe API-sleutel aanmaken**.
4. Geef een duidelijke naam, bv. "KPI-dashboard Railway", en bevestig met je wachtwoord.
5. Kopieer de sleutel direct — die wordt daarna niet meer getoond. Bewaar hem samen met:
   - **ODOO_URL**: `https://basetimebv.odoo.com`
   - **ODOO_DB**: de databasenaam (meestal `basetimebv` — te vinden onder Instellingen →
     Algemene instellingen, of vraag het na bij wie de Odoo-omgeving beheert)
   - **ODOO_USERNAME**: het e-mailadres waarmee je in Odoo inlogt
   - **ODOO_API_KEY**: de sleutel die je net kopieerde

## Stap 2 — Code naar een eigen GitHub-repository pushen

1. Maak op github.com een nieuwe, lege repository aan (privé mag), bv.
   `basetime-kpi-dashboard`. Voeg **geen** README/.gitignore toe bij het aanmaken — dit
   project heeft die al.
2. Pak deze projectmap uit op je computer en open een terminal in die map.
3. Voer uit (vervang de URL door die van jouw nieuwe repo):

   ```bash
   git init
   git add .
   git commit -m "Basetime KPI-dashboard"
   git branch -M main
   git remote add origin https://github.com/<jouw-gebruikersnaam>/basetime-kpi-dashboard.git
   git push -u origin main
   ```

   Git vraagt daarbij om in te loggen op GitHub (of gebruikt een al gekoppelde
   inlogmethode). `.env` wordt niet meegestuurd (staat in `.gitignore`) — je
   Odoo-sleutel en dashboard-wachtwoord komen dus nooit in de repository terecht.

## Stap 3 — Railway-project aanmaken en koppelen

1. Log in op railway.app en klik **New Project → Deploy from GitHub repo**.
2. Kies de repository die je net gepusht hebt. Railway herkent het als een
   Python-project (via `requirements.txt`) en gebruikt automatisch het opgegeven
   startcommando (`railway.json` / `Procfile`).
3. Er start meteen een eerste deployment — die zal **falen** totdat de environment
   variables zijn ingevuld (stap 4). Dat is normaal.

## Stap 4 — Environment variables instellen

In het Railway-project: tabblad **Variables** → voeg deze toe (zie ook `.env.example`):

| Variabele | Waarde |
|---|---|
| `ODOO_URL` | `https://basetimebv.odoo.com` |
| `ODOO_DB` | de databasenaam uit stap 1 |
| `ODOO_USERNAME` | het Odoo-inlogadres uit stap 1 |
| `ODOO_API_KEY` | de API-sleutel uit stap 1 |
| `DASHBOARD_USER` | een gebruikersnaam die jij kiest, bv. `bart` |
| `DASHBOARD_PASSWORD` | een sterk wachtwoord dat jij kiest |

Optioneel (staan anders op een verstandige standaardwaarde — zie `app/config.py`):
`CACHE_TTL_SECONDS`, `MONTHS_LOOKBACK`, `TOP_PIPELINE_DEALS`, `TOP_CUSTOMERS_N`,
`CONCENTRATION_MONTHS_LOOKBACK`, `BANK_ACCOUNT_CODES`, `MAIN_OPERATING_BANK_CODE`,
`CREDIT_LIMIT`, `FIXED_MONTHLY_COSTS`, `SUBSCRIPTION_ACCOUNT_CODES`.

Na het opslaan start Railway automatisch een nieuwe deployment. Onder **Settings →
Networking** kun je een publieke URL genereren (`*.up.railway.app`) of een eigen domein
koppelen.

## Stap 5 — Testen

1. Open de Railway-URL. Je krijgt een inlogvenster van de browser (basic-auth) —
   gebruik `DASHBOARD_USER` / `DASHBOARD_PASSWORD`.
2. Het dashboard laadt en haalt meteen live cijfers uit Odoo. Duurt dit lang of loopt het
   vast, kijk dan in Railway onder **Deployments → View Logs** naar de foutmelding
   (meestal een verkeerde `ODOO_*`-variabele).
3. `/healthz` (zonder inloggen) moet `{"status": "ok"}` teruggeven — dat gebruikt Railway
   zelf als health check.

---

## Lokaal draaien / testen (optioneel)

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                # vul de echte waarden in .env in
uvicorn app.main:app --reload --port 8000
```

Ga naar `http://localhost:8000`. De geautomatiseerde tests (mocken Odoo weg, doen dus
geen echte netwerkaanroepen) draai je met:

```bash
pytest
```

## Onderhoud — wat kun je zelf aanpassen?

- **Wachtwoord wijzigen**: pas `DASHBOARD_PASSWORD` aan in Railway → Variables. Geen
  code-wijziging nodig.
- **Rekeningschema wijzigt** (nieuwe/andere grootboekcodes): pas de bijbehorende
  environment variable aan (bv. `SUBSCRIPTION_ACCOUNT_CODES`) — hoeft niet in code.
- **Kredietlimiet of vaste maandlasten veranderen**: `CREDIT_LIMIT` /
  `FIXED_MONTHLY_COSTS` in Railway → Variables.
- **Andere periode in de maandgrafieken**: `MONTHS_LOOKBACK` (aantal volledige maanden).
- **Cijfers kloppen niet meer** (bv. na een reorganisatie van het rekeningschema in
  Odoo): begin met `app/config.py` — daar staan alle Basetime-specifieke aannames met
  toelichting waar ze vandaan komen.

## Bekende beperkingen (zie ook het KPI-voorstel-document in het Claude-project)

- **Runway** is een vereenvoudigde indicator (kredietruimte ÷ vaste maandlasten), geen
  vervanging voor een volledig scenariomodel met inkoopplanning en
  debiteuren/crediteurentiming.
- **Recurring/subscription-omzet** toont geboekte omzet op de subscription-rekeningen,
  geen echt deferred-revenue-saldo — daarvoor is verbruiksregistratie per klant nodig
  die nu niet in Odoo lijkt te zitten.
- **Inkoopbacklog** filtert geen verouderde/foutieve openstaande inkooporders.
- De pijplijn-weging gebruikt alle open CRM-kansen (excl. "Closed won"/"Closed lost"),
  wat breder is dan een handmatige "geldige offertes"-selectie.
- **Ouderdomsanalyse debiteuren/crediteuren** bucket't op `date_maturity` (met terugval op
  de factuurdatum als die leeg is) — geen rekening met betalingsregelingen of dispute-status.
- **Klantconcentratie** is gebaseerd op gefactureerde omzet over de laatste
  `CONCENTRATION_MONTHS_LOOKBACK` maanden (standaard 12), niet op de pipeline. De
  pijplijn heeft wél een eigen concentratiecijfer (`pipeline.top_customer_share_pct` in
  de API-data), maar dat is nog niet als apart onderdeel op dit dashboard gezet.
- **Break-evenomzet** gebruikt de gemiddelde (blended) marge over `MONTHS_LOOKBACK`
  maanden, niet de marge van losse maanden — bij sterk wisselende marges per maand is dit
  dus een indicatie, geen exacte drempel.
