"""Kopf handlers for VirtFusionControl resources."""

from __future__ import annotations

import logging
from typing import Any

import kopf

from ..client import VirtFusionClient
from ..config import load_credentials
from ..constants import GROUP, PHASE_ERROR, PHASE_READY, TRANSIENT_RETRY_DELAY_SECONDS, VERSION
from ..errors import TransientError, VirtFusionHTTPError
from ..types import CONTROL_PLURAL, VirtFusionControlSpec

logger = logging.getLogger(__name__)


def _already_ready(status: dict[str, Any] | None) -> bool:
    return bool(status) and status.get("phase") == PHASE_READY


def _reconcile_control(
    spec: dict[str, Any],
    patch: kopf.Patch,
    log: kopf.Logger,
    status: dict[str, Any] | None = None,
    *,
    force: bool = False,
) -> None:
    parsed = VirtFusionControlSpec.model_validate(spec)
    if not force and _already_ready(status):
        log.info("VirtFusionControl already Ready; skip API refresh")
        return
    creds = load_credentials(parsed.endpoint)
    client = VirtFusionClient(creds.url, creds.token)

    try:
        client.connect()
    except VirtFusionHTTPError as e:
        patch.status["phase"] = PHASE_ERROR
        patch.status["message"] = str(e)
        if isinstance(e, TransientError):
            raise kopf.TemporaryError(str(e), delay=TRANSIENT_RETRY_DELAY_SECONDS) from e
        raise kopf.PermanentError(str(e)) from e

    # Populate hypervisor inventory (drop encryptedToken)
    hypervisors_raw = client.list_hypervisors()
    hypervisors = []
    for h in hypervisors_raw:
        group = h.get("group", {})
        entry = {
            "id": h.get("id"),
            "name": h.get("name"),
            "groupId": group.get("id") if isinstance(group, dict) else None,
            "maxCpu": h.get("maxCpu"),
            "maxMemory": h.get("maxMemory"),
            "enabled": h.get("enabled"),
            "maintenance": h.get("maintenance"),
            "ip": h.get("ip"),
        }
        hypervisors.append(entry)

    patch.status["phase"] = PHASE_READY
    patch.status["message"] = "Connected"
    patch.status["hypervisors"] = hypervisors
    log.info("VirtFusionControl reconciled: %d hypervisors", len(hypervisors))


@kopf.on.create(GROUP, VERSION, CONTROL_PLURAL)
def on_control_create(
    spec: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    status: dict[str, Any],
    **_: Any,
) -> None:
    _reconcile_control(spec, patch, logger, status)


@kopf.on.update(GROUP, VERSION, CONTROL_PLURAL)
def on_control_update(
    spec: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    status: dict[str, Any],
    **_: Any,
) -> None:
    _reconcile_control(spec, patch, logger, status, force=True)
