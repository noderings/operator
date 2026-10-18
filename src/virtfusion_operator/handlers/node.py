"""Kopf handlers for VirtFusionNode resources."""

from __future__ import annotations

import logging
from typing import Any

import kopf
import kubernetes

from ..client import VirtFusionClient
from ..config import load_credentials
from ..constants import GROUP, PHASE_ERROR, PHASE_READY, TRANSIENT_RETRY_DELAY_SECONDS, VERSION
from ..errors import TransientError, VirtFusionHTTPError
from ..types import CONTROL_PLURAL, NODE_PLURAL, VirtFusionNodeSpec

logger = logging.getLogger(__name__)


def _resolve_control_endpoint(control_name: str, namespace: str) -> str:
    """Resolve the VirtFusion control endpoint from a VirtFusionControl CR."""
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
        raise ValueError(f"VirtFusionControl {control_name} has no endpoint")
    return endpoint


def group_resources_status(
    payload: dict[str, Any] | None, hypervisor_id: int
) -> dict[str, Any] | None:
    """Shape VF group-resources for CRD status (object, not a bare array).

    Admin API returns a paginated array of {hypervisor, resources} rows.
    VirtFusionNode.status.groupResources is typed as object.
    """
    if not payload:
        return None
    data = payload.get("data", payload)
    if isinstance(data, list):
        items = [_zero_none_percent(row) if isinstance(row, dict) else row for row in data]
        out: dict[str, Any] = {"items": items}
        for row in items:
            if not isinstance(row, dict):
                continue
            hyp = row.get("hypervisor") or {}
            if hyp.get("id") == hypervisor_id:
                out["thisHypervisor"] = row
                break
        return out
    if isinstance(data, dict):
        return _zero_none_percent(data)
    return None


def _zero_none_percent(obj: Any) -> Any:
    """CRD/JSON cannot round-trip Python None; Liqo+Kopf treat percent:null vs 0 as a patch fight."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k == "percent" and v is None:
                out[k] = 0
            else:
                out[k] = _zero_none_percent(v)
        return out
    if isinstance(obj, list):
        return [_zero_none_percent(x) for x in obj]
    return obj


def _already_ready(status: dict[str, Any] | None) -> bool:
    return bool(status) and status.get("phase") == PHASE_READY


def _reconcile_node(
    spec: dict[str, Any],
    meta: dict[str, Any],
    patch: kopf.Patch,
    log: kopf.Logger,
    status: dict[str, Any] | None = None,
    *,
    force: bool = False,
) -> None:
    parsed = VirtFusionNodeSpec.model_validate(spec)
    namespace = meta.get("namespace", "default")
    if not force and _already_ready(status):
        log.info("VirtFusionNode already Ready; skip API refresh")
        return

    # Copy maintenance mode onto status
    patch.status["maintenanceMode"] = parsed.maintenanceMode

    try:
        endpoint = _resolve_control_endpoint(parsed.controlRef.name, namespace)
        creds = load_credentials(endpoint)
        client = VirtFusionClient(creds.url, creds.token)

        # Optional: fetch group resources
        try:
            resources = client.get_group_resources(parsed.hypervisorGroupId)
            shaped = group_resources_status(resources, parsed.hypervisorId)
            if shaped:
                patch.status["groupResources"] = shaped
        except Exception as e:
            log.warning(
                "Failed to fetch group resources for group %s: %s", parsed.hypervisorGroupId, e
            )

        patch.status["phase"] = PHASE_READY
        patch.status["message"] = "Node reconciled"

    except VirtFusionHTTPError as e:
        patch.status["phase"] = PHASE_ERROR
        patch.status["message"] = str(e)
        if isinstance(e, TransientError):
            raise kopf.TemporaryError(str(e), delay=TRANSIENT_RETRY_DELAY_SECONDS) from e
        raise kopf.PermanentError(str(e)) from e
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
    # Liqo strips Kopf progress annotations, so create retriggers forever.
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
