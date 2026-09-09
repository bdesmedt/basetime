# Basetime KPI-dashboard

Een live KPI-dashboard voor Basetime B.V., dat de cijfers rechtstreeks uit Odoo
(basetimebv.odoo.com) haalt: beschikbare cash, runway-indicatie, netto cashburn, order
intake, recurring/subscription-omzet, brutomarge, inkoopbacklog, gewogen pipeline, en
(sinds deze versie) ouderdomsanalyse debiteuren/crediteuren, klantconcentratie in de
gefactureerde omzet, en de benodigde break-evenomzet per maand.

Vier tabbladen: **Overzicht** (de KPI's, met de standen door de tijd), **Winst &
verlies** (de W&V tot op grootboekniveau, met een aanpasbare rubrieksindeling, een
overzicht van de grootste verschuivingen en doorklik naar de boekingsregels),
**Voorraad** en **Actieplan**.

- **Backend**: Python (FastAPI), praat met Odoo via de officiële externe XML-RPC-API.
- **Frontend**: één HTML-pagina (in `app/templates/dashboard.html`), haalt de cijfers op
  via `/api/kpis` en tekent de tabellen/grafieken in de browser — geen build-stap nodig.
- **Beveiliging**: de hele site staat achter HTTP basic-auth (gebruikersnaam/wachtwoord).
- **Cache**: opgehaalde cijfers blijven 15 minuten warm (instelbaar), zodat niet elke
  paginabezoek meteen Odoo belast. Een "Vernieuwen"-knop op het dashboard forceert een
  verse ophaal-actie.
- **Database**: nodig voor de eigen rubrieksindeling van de W&V-tab (stap 4b) en voor het
  vastleggen van standen door de tijd (stap 4c). Postgres op Railway. Alle cijfers komen
  live uit Odoo; alleen de standen die Odoo niet kan reconstrueren worden bewaard.

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
`CACHE_TTL_SECONDS`, `MONTHS_LOOKBACK`, `MAX_PERIOD_MONTHS`, `TOP_PIPELINE_DEALS`,
`TOP_CUSTOMERS_N`, `TOP_STOCK_PRODUCTS_N`, `CONCENTRATION_MONTHS_LOOKBACK`,
`BANK_ACCOUNT_CODES`, `MAIN_OPERATING_BANK_CODE`, `CREDIT_LIMIT`,
`FIXED_MONTHLY_COSTS`, `SUBSCRIPTION_ACCOUNT_CODES`,
`REVENUE_EXCLUDED_ACCOUNT_CODES`, `DEFERRED_REVENUE_ACCOUNT_CODE`,
`DEFERRED_PRODUCT_NAME_PREFIXES`, `PL_REPORT_ID`, `PL_RESULT_LINE_CODE`,
`PL_DETAIL_LINE_LIMIT`, `ODOO_RECORD_URL_TEMPLATE`.

Na het opslaan start Railway automatisch een nieuwe deployment. Onder **Settings →
Networking** kun je een publieke URL genereren (`*.up.railway.app`) of een eigen domein
koppelen.

## Stap 4b — Database toevoegen (alleen voor de W&V-tab)

De tab **Winst & verlies** haalt zijn rubrieksindeling uit het Odoo-rapport
"Profit and loss report V2", maar je kunt die indeling in het dashboard zelf aanpassen:
een grootboekrekening naar een andere rubriek slepen, of een eigen rubriek toevoegen.
Die afwijkingen moeten een nieuwe deploy overleven, en daarvoor is een kleine database
nodig.

1. In het Railway-project: **New → Database → Add PostgreSQL**.
2. Ga naar de service van het dashboard → **Variables** → **Add Reference** en kies de
   `DATABASE_URL` van de zojuist toegevoegde Postgres-service.
3. Klaar — de twee benodigde tabellen maakt het dashboard bij het eerste gebruik zelf
   aan. Kosten: een paar euro per maand.

**Zonder database werkt het dashboard gewoon door.** De W&V-tab toont dan de indeling
zoals die in Odoo staat, met de melding erbij dat wijzigingen niet bewaard kunnen
worden; de knoppen om te verplaatsen staan dan uit. Ligt de database er tijdelijk uit,
dan gebeurt hetzelfde — de pagina blijft werken.

## Stap 4c — Standen vastleggen (aanbevolen, maar niet verplicht)

Bijna alles op dit dashboard kan Odoo achteraf opnieuw uitrekenen: de omzet van maart
staat er volgend jaar nog net zo. Maar de **stand op een moment** niet — hoe groot de
pijplijn vorige maand was, wat er toen openstond aan debiteuren, hoeveel kredietruimte er
nog was. Odoo bewaart alleen de situatie van nu. Daarom legt het dashboard die standen
zelf vast, in de tabel `kpi_snapshot`. Ze verschijnen op het Overzicht-tabblad onder
"Standen door de tijd".

Vastgelegd worden: beschikbare cash, kredietruimte, openstaande en vervallen debiteuren,
openstaande crediteuren, gewogen en nominale pipeline, aantal open kansen, inkoopbacklog
en het saldo nog te nemen omzet. Stromen (omzet, order intake, kosten per maand) juist
níet — die haalt het dashboard altijd vers uit Odoo.

**Er is niets te configureren.** Zodra er een database is gekoppeld gebeurt het
vastleggen vanzelf, op twee manieren die elkaar aanvullen:

1. **Bij een paginabezoek.** Het eerste bezoek van elke dag schrijft een meting weg uit
   de cijfers die op dat moment toch al zijn opgehaald — nul extra Odoo-queries.
2. **Door de ingebouwde planner.** In dezelfde webserver draait een achtergrondtaak die
   elk uur kijkt of de meting van vandaag er al staat. Staat hij er, dan doet hij niets
   (één goedkope databasevraag). Ontbreekt hij, dan haalt hij één keer verse cijfers op
   en legt de standen vast. Zo blijft de reeks doorlopen als er een week niemand kijkt,
   en is er **geen aparte cron-service nodig**.

Hoe dan ook blijft het bij **één punt per dag**; een tweede meting op dezelfde dag
overschrijft de eerste.

Of de planner draait, zie je aan `/healthz`:
`{"status":"ok","database":true,"snapshot_scheduler":true}`. Staat `database` op `false`,
dan is `DATABASE_URL` nog niet gekoppeld en start de planner bewust niet — er valt dan
immers niets vast te leggen.

Bijstellen kan met deze variabelen (allemaal optioneel):

| Variabele | Standaard | Wat het doet |
|---|---|---|
| `SNAPSHOT_SCHEDULER_ENABLED` | `1` | Zet de ingebouwde planner uit met `0` |
| `SNAPSHOT_CHECK_MINUTES` | `60` | Hoe vaak de planner kijkt of de meting van vandaag er al staat |
| `SNAPSHOT_STARTUP_DELAY_SECONDS` | `90` | Wachttijd na het opstarten, zodat de health check eerst slaagt |

**Handmatig een meting forceren** (bijvoorbeeld na een correctie in Odoo): een POST op
`/api/snapshot`, of `python -m app.snapshot` in een shell. Dat overschrijft de meting van
vandaag.

**Liever buiten de webserver om?** Dat kan ook: maak een tweede Railway-service op
dezelfde repository met start command `python -m app.snapshot` en een **Cron Schedule**
(Railway verwacht dat een cron-service afsluit als hij klaar is, en dat doet dit script).
Geef die service dezelfde `ODOO_*`-variabelen en een reference naar `DATABASE_URL`; een
dashboardwachtwoord is niet nodig, want het script praat rechtstreeks met Odoo en de
database. Zet dan `SNAPSHOT_SCHEDULER_ENABLED=0` op de webservice om dubbel werk te
voorkomen — al is dat niet strikt nodig, want beide routes schrijven naar dezelfde ene
rij per dag.

Wat je niet kunt: met terugwerkende kracht standen aanvullen. Elke dag die niet is
vastgelegd, blijft leeg.

## Stap 5 — Testen

1. Open de Railway-URL. Je krijgt een inlogvenster van de browser (basic-auth) —
   gebruik `DASHBOARD_USER` / `DASHBOARD_PASSWORD`.
2. Het dashboard laadt en haalt meteen live cijfers uit Odoo. Duurt dit lang of loopt het
   vast, kijk dan in Railway onder **Deployments → View Logs** naar de foutmelding
   (meestal een verkeerde `ODOO_*`-variabele).
3. `/healthz` (zonder inloggen) moet `{"status": "ok", ...}` teruggeven — dat gebruikt
   Railway zelf als health check. De twee vlaggen erbij (`database` en
   `snapshot_scheduler`) laten zien of de database is gekoppeld en of de planner draait
   die de standen vastlegt; er staan geen bedrijfscijfers in.

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
- **Indeling van de W&V wijzigen**: dat doe je in het dashboard zelf, op de tab
  Winst & verlies. Sleep een grootboekrekening naar een andere rubriek of gebruik het
  knopje `⇄` op de regel; met `+ Rubriek` maak je een eigen rubriek aan en met
  "Terug naar Odoo" zet je alles terug zoals het Odoo-rapport het berekent. Het knopje
  **i** achter een rubriek laat zien welke codereeks Odoo gebruikt en welke rekeningen
  er nu in vallen.
- **Standen door de tijd**: de grafiek onderaan het Overzicht-tabblad groeit vanzelf mee
  (zie stap 4c) — het dashboard legt de standen zelf dagelijks vast, zonder dat je iets
  hoeft in te plannen. Wil je een meting nu forceren: `python -m app.snapshot`, of een
  POST op `/api/snapshot`.
- **Grootste verschuivingen**: onder de W&V-tabel, in de weergaven met twee kolommen
  (periode vs vorig jaar, of de laatste maand tegen de vorige). Gerangschikt op het
  effect op het eindresultaat, niet op het ruwe verschil — een omzetdaling en een
  kostenstijging hebben tegengestelde tekens in de tabel maar doen allebei pijn onderaan
  de streep. Te bekijken per rubriek of per grootboekrekening, met dezelfde doorklik naar
  de boekingsregels. Dit rekent volledig in de browser op de cijfers die al voor de tabel
  zijn opgehaald; het kost geen extra Odoo-query.
- **Doorklikken naar de boeking**: klik op een bedrag in de W&V-tabel (op een rubriek- of
  rekeningregel) en je krijgt de boekingsregels erachter, met leverancier, omschrijving,
  dagboek en een link naar het boekstuk in Odoo — daar zit ook de factuur-PDF aan vast.
  De lijst gebruikt exact dezelfde afbakening als het bedrag waarop je klikte, en de
  regel onderaan het venster laat zien dat beide op elkaar aansluiten. Boven
  `PL_DETAIL_LINE_LIMIT` regels wordt de lijst afgekapt; het getoonde totaal blijft dan
  wél het volledige bedrag. Wie op een boekstuk klikt heeft een Odoo-account nodig.
- **Ander W&V-rapport als basis**: `PL_REPORT_ID` (het id van het `account.report`-record
  in Odoo) en `PL_RESULT_LINE_CODE` (de code van de regel die het eindresultaat
  berekent). Alles waar die regel op steunt vormt de W&V-boom; losse memoblokken
  onderaan zo'n rapport vallen daarmee vanzelf buiten beeld.
- **Cijfers kloppen niet meer** (bv. na een reorganisatie van het rekeningschema in
  Odoo): begin met `app/config.py` — daar staan alle Basetime-specifieke aannames met
  toelichting waar ze vandaan komen.

## Bekende beperkingen (zie ook het KPI-voorstel-document in het Claude-project)

- **Rekeningen die in geen enkele codereeks van het W&V-rapport vallen** komen op de
  W&V-tab in de rubriek **Niet ingedeeld** terecht, buiten de resultaatberekening. Dat
  is precies wat Odoo zelf ook doet — Odoo laat ze alleen stilzwijgend weg. Bij Basetime
  gaat het (augustus 2026) om rekening 481000 "Depreciation Buildings / conversions":
  de afschrijvingsregels van het V2-rapport pakken 480, 482 en 483, maar niet 481. De
  controleregel onder de tabel benoemt dit bedrag expliciet.
- **Niet elke kostenregel heeft een factuur achter zich.** De doorklik komt altijd uit
  bij de boeking, maar bij afschrijvingen (480xxx–483xxx, uit de activaregistratie),
  kostprijs omzet (700500, uit de voorraadwaardering bij een periodiek voorraadstelsel)
  en lonen (één journaalpost per maand uit de salarisverwerking) is er geen onderliggend
  document. Bij de rekeningen die door inkoopfacturen worden gevoed — 43xxx, 44xxx,
  45xxx, 46xxx — kom je wél tot de factuur.
- **Het teken van een rekening blijft bij de rekening.** Sleep je een omzetrekening naar
  een kostenrubriek, dan houdt hij zijn omgedraaide teken. Dat is voorspelbaar, maar
  betekent wel dat zo'n verplaatsing een negatief bedrag in de kosten kan opleveren.
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
