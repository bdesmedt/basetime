"""
Dunne wrapper rond Odoo's externe XML-RPC API (stdlib xmlrpc.client, geen extra
dependency nodig). Odoo biedt dit standaard aan op /xmlrpc/2/common (inloggen) en
/xmlrpc/2/object (ORM-methodes aanroepen zoals search_read en read_group) — precies
zoals ook interne Odoo-modules en de Odoo-app het gebruiken.

Zie https://www.odoo.com/documentation/18.0/developer/reference/external_api.html
"""

from __future__ import annotations

import xmlrpc.client
from typing import Any

from . import config


class OdooAuthError(RuntimeError):
    pass


class OdooClient:
    def __init__(self, url: str, db: str, username: str, api_key: str):
        self.url = url.rstrip("/")
        self.db = db
        self.username = username
        self.api_key = api_key
        self._uid: int | None = None
        self._object_proxy: xmlrpc.client.ServerProxy | None = None

    def _common_proxy(self) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")

    def _object_proxy_cached(self) -> xmlrpc.client.ServerProxy:
        if self._object_proxy is None:
            self._object_proxy = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")
        return self._object_proxy

    def uid(self) -> int:
        if self._uid is None:
            try:
                uid = self._common_proxy().authenticate(self.db, self.username, self.api_key, {})
            except Exception as exc:  # netwerk/SSL/timeout fouten
                raise OdooAuthError(f"Kon niet verbinden met Odoo op {self.url}: {exc}") from exc
            if not uid:
                raise OdooAuthError(
                    "Odoo-authenticatie mislukt — controleer ODOO_URL, ODOO_DB, "
                    "ODOO_USERNAME en ODOO_API_KEY."
                )
            self._uid = uid
        return self._uid

    def execute_kw(self, model: str, method: str, args: list, kwargs: dict | None = None) -> Any:
        try:
            return self._object_proxy_cached().execute_kw(
                self.db, self.uid(), self.api_key, model, method, args, kwargs or {}
            )
        except OdooAuthError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Odoo-aanroep {model}.{method} mislukt: {exc}") from exc

    def search_read(
        self,
        model: str,
        domain: list,
        fields: list[str],
        limit: int = 0,
        order: str | None = None,
    ) -> list[dict]:
        kwargs: dict[str, Any] = {"fields": fields}
        if limit:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order
        return self.execute_kw(model, "search_read", [domain], kwargs)

    def read_group(
        self,
        model: str,
        domain: list,
        fields: list[str],
        groupby: list[str],
    ) -> list[dict]:
        return self.execute_kw(model, "read_group", [domain, fields, groupby])


def get_client() -> OdooClient:
    """Bouwt een client op basis van de environment variables in config.py."""
    return OdooClient(
        url=config.ODOO_URL,
        db=config.ODOO_DB,
        username=config.ODOO_USERNAME,
        api_key=config.ODOO_API_KEY,
    )
