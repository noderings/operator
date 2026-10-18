"""Kopf handlers for SolusVMControl resources."""

from __future__ import annotations

import logging
from typing import Any

import kopf

from ..client import SolusVMClient
from ..config import load_credentials
from ..constants import GROUP, PHASE_ERROR, PHASE_READY, TRANSIENT_RETRY_DELAY_SECONDS, VERSION
from ..errors import SolusVMHTTPError, TransientError
from ..types import CONTROL_PLURAL, SolusVMControlSpec

logger = logging.getLogger(__name__)


def _already_ready(status: dict[str, Any] | None) -> bool:
    return bool(status) and status.get("phase") == PHASE_READY


def _location_ids(entry: dict[str, Any]) -> list[int]:
    raw = entry.get("locations") or entry.get("location_ids") or entry.get("locationIds") or []
    ids: list[int] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("id") is not None:
                ids.append(int(item["id"]))
            elif item is not None and not isinstance(item, dict):
                try:
                    ids.append(int(item))
                except (TypeError, ValueError):
                    continue
    return ids


def _drop_secrets(obj: Any) -> Any:
    """Never persist tokens or passwords from inventory objects."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, val in obj.items():
            lk = str(key).lower()
            if any(s in lk for s in ("token", "password", "secret", "credential")):
                continue
            out[key] = _drop_secrets(val)
        return out
    if isinstance(obj, list):
        return [_drop_secrets(x) for x in obj]
    return obj


def shape_compute_resource(raw: dict[str, Any]) -> dict[str, Any]:
    """Inventory row for SolusVMControl.status.computeResources (no credentials)."""
    settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
    host = raw.get("host") or raw.get("ip") or settings.get("host")
    limits = (
        settings.get("limits") if isinstance(settings.get("limits"), dict) else raw.get("limits")
    )
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "host": host,
        "status": raw.get("status"),
        "locationIds": _location_ids(raw),
        "vmsCount": raw.get("vms_count")
        if raw.get("vms_count") is not None
        else raw.get("vmsCount"),
        "isLocked": bool(
            raw.get("is_locked") if raw.get("is_locked") is not None else raw.get("isLocked")
        ),
        "limits": _drop_secrets(limits) if limits else None,
    }


def _reconcile_control(
    spec: dict[str, Any],
    patch: kopf.Patch,
    log: kopf.Logger,
    status: dict[str, Any] | None = None,
    *,
    force: bool = False,
) -> None:
    parsed = SolusVMControlSpec.model_validate(spec)
    if not force and _already_ready(status):
        log.info("SolusVMControl already Ready; skip API refresh")
        return
    creds = load_credentials(parsed.endpoint)
    client = SolusVMClient(creds.url, creds.token)

    try:
        client.account()
        client.list_projects()
    except SolusVMHTTPError as e:
        patch.status["phase"] = PHASE_ERROR
        patch.status["message"] = str(e)
        if e.status_code in (401, 403):
            patch.status["message"] = "token rejected"
            raise kopf.PermanentError("token rejected") from e
        if isinstance(e, TransientError):
            raise kopf.TemporaryError(str(e), delay=TRANSIENT_RETRY_DELAY_SECONDS) from e
        raise kopf.PermanentError(str(e)) from e

    resources = []
    for row in client.list_compute_resources():
        resources.append(shape_compute_resource(row))

    patch.status["phase"] = PHASE_READY
    patch.status["message"] = "Connected"
    patch.status["computeResources"] = resources
    log.info("SolusVMControl reconciled: %d compute resources", len(resources))


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
