"""Kopf handlers for gs.pterodactyl.com."""

from __future__ import annotations

import logging
import os
from typing import Any

import kopf

from .. import __version__

from ..bridge import start_bridge
from ..constants import CONSOLE_BRIDGE_ADDR_ENV
from . import action  # noqa: F401
from . import catalog  # noqa: F401
from . import server  # noqa: F401


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_: Any) -> None:
    settings.posting.level = logging.INFO
    # Liqo drops extra annotations on a reflected object. Kopf used to store
    # "already handled" and the retry delay there, so every drop looked like a
    # new catalog and listed the panel again until the rate limit was spent.
    # Status stays on the object, so both marks live under status.
    settings.persistence.progress_storage = kopf.StatusProgressStorage()
    settings.persistence.diffbase_storage = kopf.StatusDiffBaseStorage()
    logger = logging.getLogger(__name__)
    # Production runs the bridge in pterodactyl-console (offloaded like
    # vnc-gateway). Only start it here when the address is set (lab / fallback).
    if os.environ.get(CONSOLE_BRIDGE_ADDR_ENV, "").strip():
        start_bridge()
    else:
        logger.info("console bridge not started in-operator; expect pterodactyl-console Deployment")
    logger.info("Pterodactyl operator starting - version %s", __version__)
