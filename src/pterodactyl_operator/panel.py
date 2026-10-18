"""Panel client built from the operator Secret."""

from __future__ import annotations

from .client import PanelClient
from .config import load_credentials


def panel_client() -> PanelClient:
    creds = load_credentials()
    return PanelClient(creds.url, creds.application_key, creds.client_key)
