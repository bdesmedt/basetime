"""
Inloggen en rechten.

Twee soorten accounts, bewust naast elkaar:

1. **Het beheerdersaccount uit de omgevingsvariabelen** (`DASHBOARD_USER` /
   `DASHBOARD_PASSWORD`). Werkt altijd, ook zonder database, en is daarmee je sleutel
   als er iets misgaat met de accounts of de database. Dit account mag ook nog via HTTP
   basic-auth binnenkomen, zodat een externe planner `POST /api/snapshot` kan blijven
   aanroepen.
2. **Persoonlijke accounts in de database**, met een rol. Die loggen in via het
   inlogscherm en krijgen een sessiecookie.

Rollen, oplopend:

    manager     leest alles op rubriek- en totaalniveau; geen boekingsregels, wijzigt niets
    financieel  ziet ook de boekingsregels en de links naar Odoo, en mag sturen
                (betaalplan, eigen regels, W&V-indeling)
    beheerder   idem, plus gebruikers en het logboek

BELANGRIJK — de rechten worden per API-endpoint gecontroleerd, niet in het scherm. Knoppen
verbergen is opmaak; wie de URL kent haalt anders alsnog alles op. Het scherm gebruikt
`/api/me` alleen om te tonen wat je toch al mág.

Wachtwoorden worden nooit opgeslagen, alleen een scrypt-hash met een eigen salt. De
sessie staat in de database (alleen de hash van het token), zodat een account uitzetten
ook echt betekent dat de lopende sessies weg zijn.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import config, db

logger = logging.getLogger("basetime-dashboard.auth")

# Rolvolgorde. Een hoger getal mag alles wat een lager getal mag.
ROLES: dict[str, int] = {"manager": 10, "financieel": 20, "beheerder": 30}
ROLE_LABELS = {
    "manager": "Manager",
    "financieel": "Financieel",
    "beheerder": "Beheerder",
}
ROLE_DESCRIPTIONS = {
    "manager": "Leest alle tabbladen op rubriek- en totaalniveau. Geen boekingsregels, "
               "kan niets wijzigen.",
    "financieel": "Ziet ook de boekingsregels en de links naar Odoo, en mag het "
                  "betaalplan en de W&V-indeling aanpassen.",
    "beheerder": "Alles van Financieel, plus het beheren van gebruikers en het logboek.",
}

COOKIE_NAME = "basetime_sessie"

# scrypt-parameters. n=2**14 kost ongeveer 100 ms per poging op een Railway-container:
# genoeg om raden onaantrekkelijk te maken, weinig genoeg om inloggen snel te houden.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1


@dataclass(frozen=True)
class User:
    email: str
    name: str
    role: str
    source: str = "database"      # "database" of "omgeving"
    id: int | None = None

    @property
    def label(self) -> str:
        return self.name or self.email

    def may(self, minimum: str) -> bool:
        return ROLES.get(self.role, 0) >= ROLES.get(minimum, 99)

    def as_dict(self) -> dict:
        return {
            "email": self.email,
            "name": self.name,
            "label": self.label,
            "role": self.role,
            "role_label": ROLE_LABELS.get(self.role, self.role),
            "source": self.source,
            "may_see_lines": self.may("financieel"),
            "may_edit": self.may("financieel"),
            "may_manage_users": self.may("beheerder"),
        }


# --- Wachtwoorden -------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algorithm, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(base64.b64decode(digest_b64)),
        )
    except Exception:
        return False
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


def password_problem(password: str) -> str | None:
    """Geeft een uitlegbare melding terug als het wachtwoord niet voldoet, anders None."""
    if len(password or "") < config.MIN_PASSWORD_LENGTH:
        return (f"Kies een wachtwoord van minstens {config.MIN_PASSWORD_LENGTH} tekens. "
                "Een zin met een paar woorden is makkelijker te onthouden én sterker dan "
                "een kort wachtwoord met vreemde tekens.")
    return None


# --- Tokens -------------------------------------------------------------------

def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- Het beheerdersaccount uit de omgeving -------------------------------------

def env_admin() -> User:
    return User(email=config.DASHBOARD_USER, name=config.DASHBOARD_USER,
                role="beheerder", source="omgeving", id=None)


def _is_env_admin(identifier: str, password: str) -> bool:
    user_ok = secrets.compare_digest(identifier or "", config.DASHBOARD_USER)
    pass_ok = secrets.compare_digest(password or "", config.DASHBOARD_PASSWORD)
    return user_ok and pass_ok


# --- Pogingen afremmen ----------------------------------------------------------
# Simpel en in het geheugen: bij één webserver is dat genoeg, en het voorkomt dat iemand
# duizenden wachtwoorden per minuut kan proberen.

_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()


def _prune(now: float, moments: list[float]) -> list[float]:
    return [m for m in moments if now - m < config.LOGIN_LOCKOUT_MINUTES * 60]


def too_many_attempts(key: str) -> bool:
    now = time.time()
    with _attempts_lock:
        moments = _prune(now, _attempts.get(key, []))
        _attempts[key] = moments
        return len(moments) >= config.LOGIN_MAX_ATTEMPTS


def register_failure(key: str) -> None:
    now = time.time()
    with _attempts_lock:
        _attempts[key] = _prune(now, _attempts.get(key, [])) + [now]


def clear_attempts(key: str) -> None:
    with _attempts_lock:
        _attempts.pop(key, None)


# --- Inloggen en sessies --------------------------------------------------------

# Sessies van het omgevingsaccount kunnen niet in de database staan (dat account heeft
# daar geen rij), en zonder database moet inloggen sowieso blijven werken. Vandaar deze
# kleine tabel in het geheugen; die is na een herstart leeg, dan log je opnieuw in.
_env_sessions: dict[str, float] = {}
_env_lock = threading.Lock()


def authenticate(identifier: str, password: str) -> User | None:
    """Controleert e-mailadres/gebruikersnaam + wachtwoord. Geeft None bij een fout, zonder
    te verklappen wát er fout was."""
    identifier = (identifier or "").strip()
    if _is_env_admin(identifier, password):
        return env_admin()
    if not db.configured():
        return None
    try:
        row = db.get_user_by_email(identifier)
    except db.StorageUnavailable as exc:
        logger.warning("Inloggen kon de database niet bereiken: %s", exc)
        raise
    if not row or not row.get("active"):
        # Toch de hash-berekening doen, zodat een bestaand account niet te herkennen is
        # aan een sneller antwoord.
        verify_password(password, hash_password("x"))
        return None
    if not verify_password(password, row.get("password_hash")):
        return None
    return _user_from_row(row)


def _user_from_row(row: dict) -> User:
    return User(email=row["email"], name=row.get("name") or "", role=row["role"],
                source="database", id=row["id"])


def start_session(user: User) -> str:
    token = new_token()
    if user.source == "omgeving":
        with _env_lock:
            now = time.time()
            for old, expires in list(_env_sessions.items()):
                if expires < now:
                    _env_sessions.pop(old, None)
            _env_sessions[token_hash(token)] = now + config.SESSION_HOURS * 3600
        return token
    db.create_session(token_hash(token), user.id, config.SESSION_HOURS)
    try:
        db.touch_login(user.id)
    except Exception:  # pragma: no cover - een mislukte tijdstempel mag niets blokkeren
        pass
    return token


def user_for_token(token: str | None) -> User | None:
    if not token:
        return None
    digest = token_hash(token)
    with _env_lock:
        expires = _env_sessions.get(digest)
        if expires and expires > time.time():
            return env_admin()
        if expires:
            _env_sessions.pop(digest, None)
    if not db.configured():
        return None
    try:
        row = db.session_user(digest)
    except db.StorageUnavailable:
        return None
    return _user_from_row(row) if row else None


def end_session(token: str | None) -> None:
    if not token:
        return
    digest = token_hash(token)
    with _env_lock:
        _env_sessions.pop(digest, None)
    if db.configured():
        try:
            db.delete_session(digest)
        except db.StorageUnavailable:
            pass


# --- Uitnodigingen ---------------------------------------------------------------

def create_invite(user_id: int) -> str:
    """Nieuw eenmalig token voor 'stel je wachtwoord in'. Alleen de hash gaat de database
    in; de link zelf krijgt de beheerder één keer te zien en geeft hij zelf door."""
    token = new_token()
    db.set_invite(user_id, token_hash(token), config.INVITE_HOURS)
    return token


def user_for_invite(token: str) -> dict | None:
    if not token:
        return None
    try:
        return db.user_by_invite(token_hash(token))
    except db.StorageUnavailable:
        return None


def accept_invite(token: str, password: str) -> User | None:
    row = user_for_invite(token)
    if not row:
        return None
    db.set_password(row["id"], hash_password(password))
    return _user_from_row(row)


# --- Logboek ----------------------------------------------------------------------

def audit(user: User | None, action: str, detail: dict | None = None) -> None:
    if not db.configured():
        return
    db.add_audit(user.email if user else "onbekend", action, detail)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
