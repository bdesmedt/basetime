"""
Zet verplichte environment variables VOORDAT app.config wordt geïmporteerd
(config.py valideert ze bij het importeren). Dit bestand wordt door pytest
automatisch als eerste geladen voor elk bestand in deze map.
"""

import os

os.environ.setdefault("ODOO_URL", "https://example-test.odoo.com")
os.environ.setdefault("ODOO_DB", "example_test")
os.environ.setdefault("ODOO_USERNAME", "test@example.com")
os.environ.setdefault("ODOO_API_KEY", "test-key")
os.environ.setdefault("DASHBOARD_USER", "testuser")
os.environ.setdefault("DASHBOARD_PASSWORD", "testpass")
