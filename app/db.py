"""
Alles wat het dashboard zelf moet onthouden (Postgres op Railway): de eigen
W&V-indeling en de wekelijkse momentopnames.

Waarom hier een database bij komt terwijl de rest van het dashboard stateless is:
het Odoo-rapport koppelt rubrieken aan codereeksen ("alles wat met 45 begint"). Je kunt
daarin geen losse grootboekrekening verplaatsen. Het dashboard vertaalt die reeksen bij
elke verversing naar een concrete indeling rekening -> rubriek, en legt hier alleen vast
wat de gebruiker daarvan afwijkt. Dat handjevol afwijkingen moet een deploy overleven,
en dat kan niet in de code of in het geheugen van de webserver.

Drie tabellen, meer is het niet:
  pl_account_override  rekeningcode -> rubriek (de afwijkingen)
  pl_custom_rubriek    zelf toegevoegde rubrieken
  kpi_snapshot         wekelijkse momentopname van de standen (zie hieronder)

Over die snapshots: bijna alles op het dashboard kan Odoo achteraf opnieuw uitrekenen —
omzet van maart, kosten van juli, dat staat er over een jaar nog net zo. Maar de STAND op
een moment kan Odoo NIET reconstrueren: hoe groot de pijplijn was, wat er openstond aan
debiteuren, hoeveel kredietruimte er was. Odoo bewaart alleen de situatie van nu. Dat
bleek al bij de gewogen pijplijn: die konden we niet terughalen omdat Odoo de kans per
opportunity niet historisch bijhoudt. Daarom leggen we die standen vast zodra ze
langskomen — elke week die je overslaat is voorgoed weg.

BELANGRIJK — dit bestand mag nooit de hele pagina onderuit halen. Ontbreekt DATABASE_URL,
is psycopg niet geïnstalleerd of ligt de database eruit, dan geeft load_layout() gewoon
een lege indeling terug (= de kale Odoo-indeling) met een uitlegbare foutmelding erbij.
Alleen SCHRIJFacties geven dan een nette fout terug aan de gebruiker.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from . import config

logger = logging.getLogger("basetime-dashboard.db")

# Postgres-connecties zijn hier zeldzaam (alleen bij een cache-miss of een wijziging),
# dus een connectiepool is overdreven: we openen per aanroep een verbinding en sluiten
# die weer. Het slot voorkomt alleen dat twee gelijktijdige verzoeken de schema-migratie
# tegelijk proberen te draaien.
_lock = threading.Lock()
_schema_ready = False

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS pl_account_override (
        account_code TEXT PRIMARY KEY,
        rubriek_id   TEXT        NOT NULL,
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pl_custom_rubriek (
        id         BIGSERIAL   PRIMARY KEY,
        name       TEXT        NOT NULL,
        parent_id  TEXT        NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Eén rij per dag. De metingen zitten als JSON in payload, zodat er later een cijfer
    # bij kan zonder de tabel te hoeven wijzigen — en oude rijen gewoon blijven staan.
    """
    CREATE TABLE IF NOT EXISTS kpi_snapshot (
        taken_on DATE        PRIMARY KEY,
        taken_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        payload  JSONB       NOT NULL
    )
    """,
    # Betaalplan voor de kasprognose: per leverancier (of per andere groep) wanneer je
    # betaalt. `key` is bv. "partner:6344". Zonder rij geldt de standaard: op vervaldatum.
    """
    CREATE TABLE IF NOT EXISTS cash_plan (
        key        TEXT        PRIMARY KEY,
        label      TEXT        NOT NULL DEFAULT '',
        mode       TEXT        NOT NULL,
        start_week INTEGER     NOT NULL DEFAULT 0,
        spread     INTEGER     NOT NULL DEFAULT 1,
        note       TEXT        NOT NULL DEFAULT '',
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Gebruikers, sessies en een logboek van wat er is gewijzigd. Zonder database is er
    # alleen het beheerdersaccount uit de omgevingsvariabelen (zie app/auth.py); zodra er
    # een database is, komen daar persoonlijke accounts met een rol bij.
    """
    CREATE TABLE IF NOT EXISTS app_user (
        id             BIGSERIAL   PRIMARY KEY,
        email          TEXT        NOT NULL UNIQUE,
        name           TEXT        NOT NULL DEFAULT '',
        role           TEXT        NOT NULL,
        password_hash  TEXT,
        active         BOOLEAN     NOT NULL DEFAULT true,
        invite_hash    TEXT,
        invite_expires TIMESTAMPTZ,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_login_at  TIMESTAMPTZ
    )
    """,
    # De sessie zelf staat in de database en niet in een ondertekend cookie: zo kun je
    # iemand er ook echt uit gooien (account uitzetten = sessies weg).
    """
    CREATE TABLE IF NOT EXISTS user_session (
        token_hash TEXT        PRIMARY KEY,
        user_id    BIGINT      NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL,
        last_seen  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Wijzigen is beslissen. Wie heeft welke leverancier op uitstel gezet, en wanneer?
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id     BIGSERIAL   PRIMARY KEY,
        at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        actor  TEXT        NOT NULL DEFAULT '',
        action TEXT        NOT NULL,
        detail JSONB       NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    # Eigen regels in de prognose die niet uit Odoo komen: een financieringsronde, een
    # toegezegde betaling, een verwachte uitgave.
    """
    CREATE TABLE IF NOT EXISTS cash_extra (
        id         BIGSERIAL   PRIMARY KEY,
        label      TEXT        NOT NULL,
        amount     NUMERIC     NOT NULL,
        on_date    DATE        NOT NULL,
        note       TEXT        NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
]

# Betaalwijzen voor cash_plan.mode:
#   "due"    op vervaldatum (standaard)
#   "week"   in één keer, in week `start_week`
#   "spread" in `spread` gelijke delen, vanaf week `start_week`
#   "defer"  buiten de horizon (betaal je niet binnen deze 13 weken)
PLAN_MODES = ("due", "week", "spread", "defer")

CUSTOM_PREFIX = "custom:"


class StorageUnavailable(RuntimeError):
    """Er is geen (werkende) database. Lezen valt terug op de Odoo-indeling, schrijven niet."""


def custom_id(row_id: int) -> str:
    return f"{CUSTOM_PREFIX}{row_id}"


def is_custom(rubriek_id: str) -> bool:
    return str(rubriek_id).startswith(CUSTOM_PREFIX)


def _custom_row_id(rubriek_id: str) -> int:
    try:
        return int(str(rubriek_id)[len(CUSTOM_PREFIX):])
    except ValueError as exc:
        raise ValueError(f"Onbekende rubriek-id: {rubriek_id}") from exc


def configured() -> bool:
    """Is er überhaupt een database ingesteld? (Zegt nog niets over bereikbaarheid.)"""
    return bool(config.DATABASE_URL)


def _connect():
    if not config.DATABASE_URL:
        raise StorageUnavailable(
            "Er is geen DATABASE_URL ingesteld. Voeg in Railway een Postgres-database toe "
            "en zet DATABASE_URL bij de variabelen van deze service."
        )
    try:
        import psycopg  # noqa: PLC0415 — bewust laat: zonder database is de import niet nodig
    except ImportError as exc:  # pragma: no cover - alleen bij een onvolledige installatie
        raise StorageUnavailable(
            "Het pakket psycopg is niet geïnstalleerd; zonder dat pakket kan de eigen "
            "indeling niet worden opgeslagen (zie requirements.txt)."
        ) from exc
    try:
        return psycopg.connect(config.DATABASE_URL, connect_timeout=config.DATABASE_TIMEOUT)
    except Exception as exc:
        raise StorageUnavailable(f"Kon geen verbinding maken met de database: {exc}") from exc


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _lock:
        if _schema_ready:
            return
        with conn.cursor() as cur:
            for statement in _SCHEMA_STATEMENTS:
                cur.execute(statement)
        conn.commit()
        _schema_ready = True


def _with_conn(fn):
    conn = _connect()
    try:
        _ensure_schema(conn)
        result = fn(conn)
        conn.commit()
        return result
    finally:
        conn.close()


# --- Lezen -------------------------------------------------------------------

EMPTY_LAYOUT: dict[str, Any] = {"overrides": {}, "custom": []}


def load_layout() -> dict:
    """De opgeslagen afwijkingen. Geeft ALTIJD een bruikbaar antwoord terug: bij een
    ontbrekende of onbereikbare database een lege indeling met `error` ingevuld, zodat
    het dashboard de kale Odoo-indeling laat zien in plaats van een foutpagina."""
    if not config.DATABASE_URL:
        return {
            **EMPTY_LAYOUT,
            "storage": "geen",
            "error": None,
            "message": "Geen database gekoppeld — het dashboard toont de indeling zoals "
                       "die in Odoo staat. Wijzigingen kunnen niet worden bewaard.",
        }
    try:
        def _read(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT account_code, rubriek_id FROM pl_account_override")
                overrides = {str(code): str(rid) for code, rid in cur.fetchall()}
                cur.execute(
                    "SELECT id, name, parent_id FROM pl_custom_rubriek ORDER BY id"
                )
                custom = [
                    {"id": custom_id(rid), "name": name, "parent": parent}
                    for rid, name, parent in cur.fetchall()
                ]
            return {"overrides": overrides, "custom": custom}

        data = _with_conn(_read)
        return {**data, "storage": "postgres", "error": None, "message": None}
    except Exception as exc:
        logger.warning("Kon de opgeslagen W&V-indeling niet lezen: %s", exc)
        return {
            **EMPTY_LAYOUT,
            "storage": "geen",
            "error": str(exc),
            "message": "De database is nu niet bereikbaar — het dashboard toont de "
                       "indeling zoals die in Odoo staat.",
        }


# --- Schrijven ---------------------------------------------------------------

def set_override(account_code: str, rubriek_id: str) -> None:
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pl_account_override (account_code, rubriek_id, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (account_code)
                DO UPDATE SET rubriek_id = EXCLUDED.rubriek_id, updated_at = now()
                """,
                (account_code, rubriek_id),
            )
    _with_conn(_write)


def clear_override(account_code: str) -> None:
    """Zet één rekening terug naar de indeling zoals Odoo die berekent."""
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM pl_account_override WHERE account_code = %s", (account_code,)
            )
    _with_conn(_write)


def add_rubriek(name: str, parent_id: str) -> dict:
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pl_custom_rubriek (name, parent_id) VALUES (%s, %s) RETURNING id",
                (name, parent_id),
            )
            new_id = cur.fetchone()[0]
        return {"id": custom_id(new_id), "name": name, "parent": parent_id}
    return _with_conn(_write)


def rename_rubriek(rubriek_id: str, name: str) -> None:
    row_id = _custom_row_id(rubriek_id)

    def _write(conn):
        with conn.cursor() as cur:
            cur.execute("UPDATE pl_custom_rubriek SET name = %s WHERE id = %s", (name, row_id))
    _with_conn(_write)


def delete_rubriek(rubriek_id: str) -> None:
    """Verwijdert een zelf toegevoegde rubriek. Rekeningen die erin stonden gaan terug
    naar de rubriek die Odoo ze geeft — ze verdwijnen dus niet uit de W&V."""
    row_id = _custom_row_id(rubriek_id)

    def _write(conn):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pl_account_override WHERE rubriek_id = %s", (rubriek_id,))
            cur.execute("DELETE FROM pl_custom_rubriek WHERE id = %s", (row_id,))
    _with_conn(_write)


def reset() -> None:
    """Alles terug naar de indeling van het Odoo-rapport."""
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pl_account_override")
            cur.execute("DELETE FROM pl_custom_rubriek")
    _with_conn(_write)


# --- Momentopnames ------------------------------------------------------------

def save_snapshot(payload: dict, taken_on: "datetime.date | None" = None) -> None:
    """Legt de standen van vandaag vast. Draai je het twee keer op een dag, dan
    overschrijft de laatste meting de eerste — één punt per dag houdt de reeks leesbaar."""
    import json as _json

    def _write(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kpi_snapshot (taken_on, taken_at, payload)
                VALUES (COALESCE(%s, CURRENT_DATE), now(), %s)
                ON CONFLICT (taken_on)
                DO UPDATE SET payload = EXCLUDED.payload, taken_at = now()
                """,
                (taken_on, _json.dumps(payload)),
            )
    _with_conn(_write)


def snapshot_exists_today() -> bool:
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM kpi_snapshot WHERE taken_on = CURRENT_DATE")
            return cur.fetchone() is not None
    return _with_conn(_read)


def load_snapshots(days: int = 365, limit: int = 400) -> list[dict]:
    """De vastgelegde reeks, oudste eerst. Geeft een lege lijst terug als er (nog) geen
    database is — het dashboard laat dan zien dat het vastleggen nog moet beginnen."""
    if not config.DATABASE_URL:
        return []
    try:
        def _read(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT taken_on, payload FROM kpi_snapshot
                    WHERE taken_on >= CURRENT_DATE - %s::int
                    ORDER BY taken_on DESC LIMIT %s
                    """,
                    (days, limit),
                )
                return [
                    {"date": taken_on.isoformat(), **(payload or {})}
                    for taken_on, payload in cur.fetchall()
                ]
        rows = _with_conn(_read)
        rows.reverse()
        return rows
    except Exception as exc:
        logger.warning("Kon de momentopnames niet lezen: %s", exc)
        return []


# --- Betaalplan en eigen regels voor de kasprognose --------------------------

def load_cash_plan() -> dict:
    """Het betaalplan per leverancier. Geeft altijd iets bruikbaars terug; zonder
    database een lege planning, wat neerkomt op "alles op vervaldatum"."""
    if not config.DATABASE_URL:
        return {"plan": {}, "extras": [], "storage": "geen", "error": None}
    try:
        def _read(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT key, label, mode, start_week, spread, note FROM cash_plan"
                )
                plan = {
                    key: {"key": key, "label": label, "mode": mode,
                          "start_week": start_week, "spread": spread, "note": note}
                    for key, label, mode, start_week, spread, note in cur.fetchall()
                }
                cur.execute(
                    "SELECT id, label, amount, on_date, note FROM cash_extra ORDER BY on_date"
                )
                extras = [
                    {"id": rid, "label": label, "amount": float(amount),
                     "date": on_date.isoformat(), "note": note}
                    for rid, label, amount, on_date, note in cur.fetchall()
                ]
            return {"plan": plan, "extras": extras}
        data = _with_conn(_read)
        return {**data, "storage": "postgres", "error": None}
    except Exception as exc:
        logger.warning("Kon het betaalplan niet lezen: %s", exc)
        return {"plan": {}, "extras": [], "storage": "geen", "error": str(exc)}


def set_cash_plan(key: str, label: str, mode: str, start_week: int = 0,
                  spread: int = 1, note: str = "") -> None:
    if mode not in PLAN_MODES:
        raise ValueError(f"Onbekende betaalwijze: {mode}")

    def _write(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cash_plan (key, label, mode, start_week, spread, note, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (key) DO UPDATE SET
                    label = EXCLUDED.label, mode = EXCLUDED.mode,
                    start_week = EXCLUDED.start_week, spread = EXCLUDED.spread,
                    note = EXCLUDED.note, updated_at = now()
                """,
                (key, label, mode, max(0, start_week), max(1, spread), note),
            )
    _with_conn(_write)


def clear_cash_plan(key: str) -> None:
    """Terug naar de standaard: betalen op vervaldatum."""
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cash_plan WHERE key = %s", (key,))
    _with_conn(_write)


def reset_cash_plan() -> None:
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cash_plan")
            cur.execute("DELETE FROM cash_extra")
    _with_conn(_write)


def add_cash_extra(label: str, amount: float, on_date: str, note: str = "") -> dict:
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cash_extra (label, amount, on_date, note) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (label, amount, on_date, note),
            )
            new_id = cur.fetchone()[0]
        return {"id": new_id, "label": label, "amount": amount, "date": on_date, "note": note}
    return _with_conn(_write)


def delete_cash_extra(extra_id: int) -> None:
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cash_extra WHERE id = %s", (extra_id,))
    _with_conn(_write)


# --- Gebruikers, sessies en logboek -------------------------------------------
# Alles hieronder raist StorageUnavailable als er geen database is. Dat is bewust: bij
# inloggen wil je géén stille terugval. Het beheerdersaccount uit de omgevingsvariabelen
# werkt wél zonder database, zodat je jezelf nooit buitensluit (zie app/auth.py).

def _user_row(row) -> dict | None:
    if not row:
        return None
    keys = ("id", "email", "name", "role", "password_hash", "active",
            "invite_hash", "invite_expires", "last_login_at")
    return dict(zip(keys, row))


_USER_FIELDS = ("id, email, name, role, password_hash, active, invite_hash, "
                "invite_expires, last_login_at")


def get_user_by_email(email: str) -> dict | None:
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_USER_FIELDS} FROM app_user WHERE lower(email) = lower(%s)",
                        (email,))
            return _user_row(cur.fetchone())
    return _with_conn(_read)


def get_user(user_id: int) -> dict | None:
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_USER_FIELDS} FROM app_user WHERE id = %s", (user_id,))
            return _user_row(cur.fetchone())
    return _with_conn(_read)


def list_users() -> list[dict]:
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_USER_FIELDS} FROM app_user ORDER BY lower(email)")
            return [_user_row(row) for row in cur.fetchall()]
    return _with_conn(_read)


def count_users() -> int:
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM app_user")
            return cur.fetchone()[0]
    return _with_conn(_read)


def create_user(email: str, name: str, role: str) -> dict:
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_user (email, name, role) VALUES (%s, %s, %s) RETURNING id",
                (email.strip().lower(), name.strip(), role),
            )
            new_id = cur.fetchone()[0]
        return {"id": new_id, "email": email.strip().lower(), "name": name.strip(),
                "role": role, "active": True}
    return _with_conn(_write)


def update_user(user_id: int, name: str | None = None, role: str | None = None,
                active: bool | None = None) -> None:
    sets, params = [], []
    if name is not None:
        sets.append("name = %s"); params.append(name.strip())
    if role is not None:
        sets.append("role = %s"); params.append(role)
    if active is not None:
        sets.append("active = %s"); params.append(active)
    if not sets:
        return

    def _write(conn):
        with conn.cursor() as cur:
            cur.execute(f"UPDATE app_user SET {', '.join(sets)} WHERE id = %s",
                        (*params, user_id))
            # Een uitgezet account moet ook meteen zijn sessies kwijt zijn.
            if active is False:
                cur.execute("DELETE FROM user_session WHERE user_id = %s", (user_id,))
    _with_conn(_write)


def delete_user(user_id: int) -> None:
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app_user WHERE id = %s", (user_id,))
    _with_conn(_write)


def set_password(user_id: int, password_hash: str) -> None:
    """Zet het wachtwoord en maakt de uitnodiging op. Bestaande sessies blijven staan;
    wie zichzelf eruit wil gooien gebruikt uitloggen."""
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app_user SET password_hash = %s, invite_hash = NULL, "
                "invite_expires = NULL WHERE id = %s",
                (password_hash, user_id),
            )
    _with_conn(_write)


def set_invite(user_id: int, invite_hash: str, hours: int) -> None:
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app_user SET invite_hash = %s, "
                "invite_expires = now() + make_interval(hours => %s) WHERE id = %s",
                (invite_hash, hours, user_id),
            )
    _with_conn(_write)


def user_by_invite(invite_hash: str) -> dict | None:
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_USER_FIELDS} FROM app_user "
                "WHERE invite_hash = %s AND invite_expires > now() AND active",
                (invite_hash,),
            )
            return _user_row(cur.fetchone())
    return _with_conn(_read)


def touch_login(user_id: int) -> None:
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute("UPDATE app_user SET last_login_at = now() WHERE id = %s", (user_id,))
    _with_conn(_write)


def create_session(token_hash: str, user_id: int, hours: int) -> None:
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_session WHERE expires_at < now()")
            cur.execute(
                "INSERT INTO user_session (token_hash, user_id, expires_at) "
                "VALUES (%s, %s, now() + make_interval(hours => %s))",
                (token_hash, user_id, hours),
            )
    _with_conn(_write)


def session_user(token_hash: str) -> dict | None:
    """De gebruiker achter een sessie, of None als de sessie niet (meer) geldig is."""
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join('u.' + f.strip() for f in _USER_FIELDS.split(','))} "
                "FROM user_session s JOIN app_user u ON u.id = s.user_id "
                "WHERE s.token_hash = %s AND s.expires_at > now() AND u.active",
                (token_hash,),
            )
            row = _user_row(cur.fetchone())
            if row:
                cur.execute("UPDATE user_session SET last_seen = now() WHERE token_hash = %s",
                            (token_hash,))
            return row
    return _with_conn(_read)


def delete_session(token_hash: str) -> None:
    def _write(conn):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_session WHERE token_hash = %s", (token_hash,))
    _with_conn(_write)


def add_audit(actor: str, action: str, detail: dict | None = None) -> None:
    """Logboekregel. Mag nooit een actie laten mislukken: kan het niet worden
    weggeschreven, dan komt er een regel in de log en gaat de actie gewoon door."""
    try:
        import json as _json

        def _write(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_log (actor, action, detail) VALUES (%s, %s, %s)",
                    (actor or "", action, _json.dumps(detail or {})),
                )
        _with_conn(_write)
    except Exception as exc:
        logger.warning("Kon de logboekregel '%s' niet vastleggen: %s", action, exc)


def list_audit(limit: int = 100) -> list[dict]:
    def _read(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT at, actor, action, detail FROM audit_log ORDER BY at DESC LIMIT %s",
                (max(1, min(limit, 500)),),
            )
            return [
                {"at": at.isoformat(), "actor": actor, "action": action, "detail": detail}
                for at, actor, action, detail in cur.fetchall()
            ]
    return _with_conn(_read)
