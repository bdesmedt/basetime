"""
Het vastleggen van de standen: cash, debiteuren, crediteuren, pijplijn, backlog.

Waarom dit bestaat: bijna alles op het dashboard kan Odoo achteraf opnieuw uitrekenen
(de omzet van maart staat er volgend jaar nog net zo), maar de STAND op een moment niet.
Hoe groot de pijplijn vorige maand was of wat er toen openstond, bewaart Odoo nergens.
Elke dag die niet wordt vastgelegd, is voorgoed weg.

Er zijn drie manieren waarop een meting in de database komt, en ze vullen elkaar aan:

1. Iemand opent het dashboard. `/api/kpis` schrijft dan de standen weg uit de payload die
   toch al is opgebouwd — nul extra Odoo-queries (zie main.py).
2. De INGEBOUWDE PLANNER hieronder. Die draait als achtergrondtaak mee in dezelfde
   webserver en kijkt elk uur of de meting van vandaag er al staat. Zo blijft de reeks
   doorlopen als er een week niemand kijkt, zonder aparte cron-service.
3. Handmatig of vanuit een externe planner: `python -m app.snapshot`, of een POST op
   /api/snapshot.

De planner mag nooit de webserver in gevaar brengen: hij draait in een aparte thread, hij
vangt elke fout op, en hij stopt netjes als de server afsluit.
"""

from __future__ import annotations

import logging
import sys
import threading
from datetime import date

from . import config, db, kpis

logger = logging.getLogger("basetime-dashboard.snapshot")


def capture(force: bool = False) -> dict | None:
    """Legt de standen van vandaag vast.

    Geeft de vastgelegde metingen terug, of None als er niets te doen was (de meting van
    vandaag stond er al) of als er geen database is. Haalt alleen cijfers uit Odoo als er
    daadwerkelijk iets vastgelegd moet worden."""
    if not db.configured():
        logger.debug("Geen database ingesteld — er wordt niets vastgelegd.")
        return None
    if not force and db.snapshot_exists_today():
        return None
    payload = kpis.build_dashboard_payload()
    standen = kpis.snapshot_from_payload(payload)
    db.save_snapshot(standen)
    logger.info("Momentopname van %s vastgelegd.", date.today().isoformat())
    return standen


# --- Ingebouwde planner ------------------------------------------------------

_stop_event: threading.Event | None = None
_thread: threading.Thread | None = None


def _loop(stop: threading.Event) -> None:
    # Eerst de webserver rustig laten opstarten (Railway doet een health check) voordat
    # we er een Odoo-aanroep overheen gooien.
    if stop.wait(config.SNAPSHOT_STARTUP_DELAY_SECONDS):
        return
    interval = max(60, config.SNAPSHOT_CHECK_MINUTES * 60)
    while not stop.is_set():
        try:
            if capture() is not None:
                logger.info("Planner heeft de meting van vandaag vastgelegd.")
        except Exception as exc:
            # Bewust alleen loggen: een mislukte meting mag de webserver niet raken.
            logger.warning("Planner kon de momentopname niet vastleggen: %s", exc)
        if stop.wait(interval):
            return


def start_scheduler() -> bool:
    """Start de achtergrondtaak. Geeft terug of hij daadwerkelijk gestart is."""
    global _stop_event, _thread
    if not config.SNAPSHOT_SCHEDULER_ENABLED:
        logger.info("De planner staat uit (SNAPSHOT_SCHEDULER_ENABLED).")
        return False
    if not db.configured():
        logger.info(
            "Geen database gekoppeld, dus de planner start niet — er valt niets vast te "
            "leggen. Koppel een Postgres-database om de reeks op te bouwen."
        )
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop_event = threading.Event()
    _thread = threading.Thread(
        target=_loop, args=(_stop_event,), name="snapshot-scheduler", daemon=True
    )
    _thread.start()
    logger.info(
        "Planner gestart: kijkt elke %d minuten of de meting van vandaag er al staat.",
        config.SNAPSHOT_CHECK_MINUTES,
    )
    return True


def stop_scheduler() -> None:
    """Zet de achtergrondtaak stil bij het afsluiten van de webserver."""
    global _stop_event, _thread
    if _stop_event is not None:
        _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=5)
    _stop_event, _thread = None, None


def scheduler_running() -> bool:
    return _thread is not None and _thread.is_alive()


# --- Losstaand draaien -------------------------------------------------------

def main() -> int:
    """`python -m app.snapshot` — legt de standen vast en sluit af. Bedoeld voor een
    externe planner; met de ingebouwde planner hierboven is dit meestal niet nodig."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not db.configured():
        logger.error(
            "Er is geen DATABASE_URL ingesteld — zonder database valt er niets vast te "
            "leggen."
        )
        return 1
    try:
        standen = capture(force=True) or {}
    except Exception as exc:
        logger.error("Vastleggen mislukt: %s", exc)
        return 1

    gevuld = {k: v for k, v in standen.items() if v is not None}
    logger.info("%d van de %d metingen gevuld.", len(gevuld), len(standen))
    for key, label, _unit in kpis.SNAPSHOT_METRICS:
        logger.info("  %-24s %s", label, standen.get(key))
    return 0


if __name__ == "__main__":
    sys.exit(main())
