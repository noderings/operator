"""Errors the server handler turns into status.phase=Error or a Kopf retry."""

from .client import PanelError


def is_permanent(exc: PanelError) -> bool:
    return 400 <= exc.status < 500 and exc.status != 429
