"""
Opslag van de EIGEN W&V-indeling (Postgres op Railway).

Waarom hier een database bij komt terwijl de rest van het dashboard stateless is:
het Odoo-rapport koppelt rubrieken aan codereeksen ("alles wat met 45 begint"). Je kunt
daarin geen losse grootboekrekening verplaatsen. Het dashboard vertaalt die reeksen bij
elke verversing naar een concrete indeling rekening -> rubriek, en legt hier alleen vast
wat de gebruiker daarvan afwijkt. Dat handjevol afwijkingen moet een deploy overleven,
en dat kan niet in de code of in het geheugen van de webserver.

Twee tabellen, meer is het niet:
  pl_account_override  rekeningcode -> rubriek (de afwijkingen)
  pl_custom_rubriek    zelf toegevoegde rubrieken

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
]

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
