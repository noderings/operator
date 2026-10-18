"""Panel URL and keys from the operator Secret. Never log the key values."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PanelCredentials:
    url: str
    application_key: str
    client_key: str
    panel_user_id: int


def load_credentials() -> PanelCredentials:
    url = os.environ.get("PTERODACTYL_PANEL_URL", "").rstrip("/")
    application_key = os.environ.get("PTERODACTYL_APPLICATION_KEY", "")
    client_key = os.environ.get("PTERODACTYL_CLIENT_KEY", "")
    raw_user = os.environ.get("PTERODACTYL_PANEL_USER_ID", "")
    if not url or not application_key or not client_key or not raw_user:
        raise RuntimeError("pterodactyl panel credentials are not configured")
    return PanelCredentials(url, application_key, client_key, int(raw_user))
