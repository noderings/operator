"""Kopf handlers for SolusVMNode resources."""

from __future__ import annotations

import logging
from typing import Any

import kopf
import kubernetes

from ..client import SolusVMClient, unwrap_data
from ..config import load_credentials
from ..constants import GROUP, PHASE_ERROR, PHASE_READY, TRANSIENT_RETRY_DELAY_SECONDS, VERSION
from ..errors import SolusVMHTTPError, TransientError
from ..types import CONTROL_PLURAL, NODE_PLURAL, SolusVMNodeSpec

logger = logging.getLogger(__name__)

_KVM = "kvm"


def _resolve_control_endpoint(control_name: str, namespace: str) -> str:
    api = kubernetes.client.CustomObjectsApi()
    ctrl = api.get_namespaced_custom_object(
        group=GROUP,
        version=VERSION,
        namespace=namespace,
        plural=CONTROL_PLURAL,
        name=control_name,
    )
    endpoint = ctrl.get("spec", {}).get("endpoint", "")
    if not endpoint:
        raise ValueError(f"SolusVMControl {control_name} has no endpoint")
    return endpoint


def _already_ready(status: dict[str, Any] | None) -> bool:
    return bool(status) and status.get("phase") == PHASE_READY


def _virtualization_types(data: dict[str, Any]) -> list[str]:
    raw = data.get("virtualization_types") or data.get("virtualizationTypes") or []
    types: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                types.append(item.lower())
            elif isinstance(item, dict) and item.get("name"):
                types.append(str(item["name"]).lower())
    single = data.get("virtualization_type")
    if single:
        types.append(str(single).lower())
    return types


def _reconcile_node(
    spec: dict[str, Any],
    meta: dict[str, Any],
    patch: kopf.Patch,
    log: kopf.Logger,
    status: dict[str, Any] | None = None,
    *,
    force: bool = False,
) -> None:
    parsed = SolusVMNodeSpec.model_validate(spec)
    namespace = meta.get("namespace", "default")
    patch.status["maintenanceMode"] = parsed.maintenanceMode

    if parsed.maintenanceMode:
        patch.status["phase"] = PHASE_READY
        patch.status["message"] = "Maintenance mode; skip compute-resource probe"
        log.info("SolusVMNode maintenanceMode; skip API probe")
        return

    if not force and _already_ready(status):
        log.info("SolusVMNode already Ready; skip API refresh")
        return

    try:
        endpoint = _resolve_control_endpoint(parsed.controlRef.name, namespace)
        creds = load_credentials(endpoint)
        client = SolusVMClient(creds.url, creds.token)
        result = client.get_compute_resource(parsed.computeResourceId)
        data = unwrap_data(result)
        is_locked = bool(
            data.get("is_locked") if data.get("is_locked") is not None else data.get("isLocked")
        )
        cr_status = str(data.get("status") or "").strip().lower()
        patch.status["isLocked"] = is_locked
        if cr_status:
            patch.status["computeResourceStatus"] = cr_status

        types = _virtualization_types(data)
        if types and _KVM not in types:
            patch.status["phase"] = PHASE_ERROR
            patch.status["message"] = "KVM only: compute resource has no kvm virtualization"
            raise kopf.PermanentError(patch.status["message"])

        if is_locked:
            patch.status["phase"] = PHASE_ERROR
            patch.status["message"] = "compute resource is locked"
            raise kopf.PermanentError(patch.status["message"])
        if cr_status and cr_status not in ("active", "online"):
            patch.status["phase"] = PHASE_ERROR
            patch.status["message"] = f"compute resource status is {cr_status}"
            raise kopf.PermanentError(patch.status["message"])

        patch.status["phase"] = PHASE_READY
        patch.status["message"] = "Node reconciled"

    except SolusVMHTTPError as e:
        patch.status["phase"] = PHASE_ERROR
        patch.status["message"] = "token rejected" if e.status_code in (401, 403) else str(e)
        if e.status_code in (401, 403):
            raise kopf.PermanentError("token rejected") from e
        if isinstance(e, TransientError):
            raise kopf.TemporaryError(str(e), delay=TRANSIENT_RETRY_DELAY_SECONDS) from e
        raise kopf.PermanentError(str(e)) from e
    except kopf.PermanentError:
        raise
    except Exception as e:
        patch.status["phase"] = PHASE_ERROR
        patch.status["message"] = str(e)
        raise kopf.TemporaryError(str(e), delay=TRANSIENT_RETRY_DELAY_SECONDS) from e


@kopf.on.create(GROUP, VERSION, NODE_PLURAL)
def on_node_create(
    spec: dict[str, Any],
    meta: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    status: dict[str, Any],
    **_: Any,
) -> None:
    _reconcile_node(spec, meta, patch, logger, status)


@kopf.on.update(GROUP, VERSION, NODE_PLURAL)
def on_node_update(
    spec: dict[str, Any],
    meta: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    status: dict[str, Any],
    **_: Any,
) -> None:
    _reconcile_node(spec, meta, patch, logger, status, force=True)
