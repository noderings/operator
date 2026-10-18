"""Handler for ProxmoxNode Custom Resources — maintenance mode + CPU inventory.

This module also exposes the helpers used by other modules to resolve a
Proxmox endpoint (`proxmoxUrl`) from the owning ProxmoxNode CR. ProxmoxNode
is the **single source of truth** for the Proxmox API endpoint; ProxmoxVM CRs
reference it via ``spec.proxmoxNodeRef.name`` and never carry the URL
themselves.

Public surface for other modules:

- ``get_proxmox_node_spec(node_ref_name, namespace, logger) -> dict`` —
  fetch the ProxmoxNode CR's ``spec`` dict by name.
- ``resolve_proxmox_url(node_ref_name, namespace, logger) -> str`` —
  return that ProxmoxNode's ``spec.proxmoxUrl``.

Both raise ``kopf.PermanentError`` on misconfiguration (missing ref, node not
found, node missing proxmoxUrl, malformed spec) and ``kopf.TemporaryError``
on transient API errors. Callers that want best-effort semantics (e.g. a
reconcile timer iterating many VMs) wrap the call in their own try/except.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

import kopf
from kubernetes import client as k8s_client

from .client import ProxmoxClient, ProxmoxError
from .config import load_settings_from_env
from .constants import (
    NODE_CPU_LAST_ERROR_MAX_LEN,
    PHASE_ERROR,
    PHASE_PENDING,
    PHASE_READY,
)
from .types import ProxmoxNodeSpec
from .utils import _get_k8s_client

logger = logging.getLogger(__name__)

# ProxmoxNode CRD coordinates — used by both the handlers below and the
# resolve helpers exposed to other modules.
_NODE_GROUP = "vm.proxmox.com"
_NODE_VERSION = "v1alpha1"
_NODE_PLURAL = "proxmoxnodes"


# ---------------------------------------------------------------------------
# ProxmoxNode → Proxmox endpoint resolution (used by other modules)
# ---------------------------------------------------------------------------


def get_proxmox_node_spec(
    node_ref_name: str | None,
    namespace: str,
    logger: kopf.Logger,
) -> dict[str, Any]:
    """Fetch a ProxmoxNode CR's ``spec`` dict by name (same namespace as the VM).

    Raises:
        kopf.PermanentError: ``node_ref_name`` missing, node not found, or
            the CR has a malformed (non-dict) ``spec``.
        kopf.TemporaryError: transient API error (5xx, network, timeout).
    """
    if not node_ref_name:
        raise kopf.PermanentError("spec.proxmoxNodeRef.name is required on ProxmoxVM")

    _, custom_api = _get_k8s_client()
    try:
        node_cr = custom_api.get_namespaced_custom_object(
            group=_NODE_GROUP,
            version=_NODE_VERSION,
            namespace=namespace,
            plural=_NODE_PLURAL,
            name=node_ref_name,
        )
    except k8s_client.ApiException as e:
        if e.status == 404:
            raise kopf.PermanentError(f"ProxmoxNode '{namespace}/{node_ref_name}' not found") from e
        logger.warning(
            "Failed to fetch ProxmoxNode %s/%s (status %s): %s",
            namespace,
            node_ref_name,
            e.status,
            e.reason,
        )
        raise kopf.TemporaryError(
            f"failed to fetch ProxmoxNode '{namespace}/{node_ref_name}': {e.reason}"
        ) from e

    node_spec = node_cr.get("spec", {})
    if not isinstance(node_spec, dict):
        raise kopf.PermanentError(f"ProxmoxNode '{namespace}/{node_ref_name}' has invalid spec")
    return node_spec


def resolve_proxmox_url(
    node_ref_name: str | None,
    namespace: str,
    logger: kopf.Logger,
) -> str:
    """Resolve ``spec.proxmoxUrl`` from the ProxmoxNode named ``node_ref_name``.

    This is the single way to obtain a Proxmox endpoint URL anywhere in the
    operator: there is no equivalent field on the ProxmoxVM CR.

    Raises:
        kopf.PermanentError: missing ref, node not found, or node has no
            non-empty ``spec.proxmoxUrl``.
        kopf.TemporaryError: transient API error fetching the ProxmoxNode.
    """
    node_spec = get_proxmox_node_spec(node_ref_name, namespace, logger)
    url = node_spec.get("proxmoxUrl")
    if not isinstance(url, str) or not url.strip():
        raise kopf.PermanentError(
            f"ProxmoxNode '{namespace}/{node_ref_name}' has no spec.proxmoxUrl"
        )
    return url.strip()


# ---------------------------------------------------------------------------
# CPU inventory parsing
# ---------------------------------------------------------------------------


class CpuInventoryBrand(StrEnum):
    """CPU brand inferred from Proxmox cpuinfo model string."""

    INTEL = "intel"
    AMD = "amd"
    ARM = "arm"
    UNKNOWN = "unknown"


# Defaults merged into status.nodeDetails.cpu before applying updates
_STATUS_CPU_DEFAULTS: dict[str, Any] = {
    "phase": PHASE_PENDING,
    "brand": "",
    "model": "",
    "cores": 0,
    "sockets": 0,
    "mhz": 0.0,
    "collectedAt": "",
    "lastError": "",
}


# --- Proxmox /nodes/{name}/status cpuinfo → status.nodeDetails.cpu ---


def _get_client(proxmox_url: str) -> ProxmoxClient:
    return ProxmoxClient(load_settings_from_env(proxmox_url))


def _unwrap_status_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Proxmox API returns { data: { ... } }; proxmoxer may return inner dict."""
    if "data" in raw and isinstance(raw["data"], dict):
        return raw["data"]
    return raw


def extract_cpuinfo(status_payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return cpuinfo dict or None if missing."""
    data = _unwrap_status_payload(status_payload)
    ci = data.get("cpuinfo")
    if not isinstance(ci, dict):
        return None
    return ci


def parse_mhz(mhz_raw: Any) -> float | None:
    if mhz_raw is None:
        return None
    try:
        return float(str(mhz_raw).strip())
    except (TypeError, ValueError):
        return None


def infer_brand(model: str) -> CpuInventoryBrand:
    m = model.lower()
    if "intel" in m or "xeon" in m or "pentium" in m or "celeron" in m:
        return CpuInventoryBrand.INTEL
    if "amd" in m or "ryzen" in m or "epyc" in m or "threadripper" in m:
        return CpuInventoryBrand.AMD
    if "arm" in m or "aarch64" in m or "apple m" in m or "cortex-" in m:
        return CpuInventoryBrand.ARM
    return CpuInventoryBrand.UNKNOWN


def _cpu_from_status(status: dict[str, Any]) -> dict[str, Any]:
    nd = status.get("nodeDetails")
    if not isinstance(nd, dict):
        return {}
    cpu = nd.get("cpu")
    return dict(cpu) if isinstance(cpu, dict) else {}


def _merged_status_cpu(status: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    base = deepcopy(_STATUS_CPU_DEFAULTS)
    base.update(_cpu_from_status(status))
    base.update(updates)
    return base


def build_inventory_from_cpuinfo(cpuinfo: dict[str, Any]) -> dict[str, Any]:
    """Build the status.nodeDetails.cpu object (phase Ready when complete)."""
    model = str(cpuinfo.get("model") or "").strip()
    cpus_raw = cpuinfo.get("cpus")
    sockets_raw = cpuinfo.get("sockets")
    try:
        cores = int(cpus_raw) if cpus_raw is not None else 0
    except (TypeError, ValueError):
        cores = 0
    try:
        sockets = int(sockets_raw) if sockets_raw is not None else 0
    except (TypeError, ValueError):
        sockets = 0
    mhz_val = parse_mhz(cpuinfo.get("mhz"))
    mhz = float(mhz_val) if mhz_val is not None else 0.0
    brand = infer_brand(model) if model else CpuInventoryBrand.UNKNOWN
    collected = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "phase": PHASE_READY,
        "brand": brand,
        "model": model or "unknown",
        "cores": cores,
        "sockets": sockets,
        "mhz": mhz,
        "collectedAt": collected,
        "lastError": "",
    }


def _node_details_cpu_ready(status: dict[str, Any]) -> bool:
    cpu = _cpu_from_status(status)
    return cpu.get("phase") == PHASE_READY


def _patch_status_node_details_cpu(
    patch: kopf.Patch, status: dict[str, Any], updates: dict[str, Any]
) -> None:
    merged = _merged_status_cpu(status, updates)
    phase = merged.get("phase", PHASE_PENDING)
    message = ""
    if phase == PHASE_READY:
        message = "CPU inventory collected"
    elif merged.get("lastError"):
        message = str(merged["lastError"])[:NODE_CPU_LAST_ERROR_MAX_LEN]
    top_phase = phase if phase in (PHASE_PENDING, PHASE_READY, PHASE_ERROR) else PHASE_PENDING
    patch.status.update(
        {
            "phase": top_phase,
            "message": message,
            "nodeDetails": {"cpu": merged},
        }
    )


def _sync_cpu_inventory_from_proxmox(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
) -> None:
    if _node_details_cpu_ready(status):
        logger.debug("ProxmoxNode status.nodeDetails.cpu already Ready, skipping Proxmox fetch")
        return

    # Pydantic + the CRD schema enforce that proxmoxUrl is a non-empty string.
    desired = ProxmoxNodeSpec.model_validate(spec)
    proxmox_url = desired.proxmoxUrl.strip()
    node_name = desired.name

    try:
        client = _get_client(proxmox_url)
        payload = client.get_node_status(node_name)
    except ValueError as e:
        msg = str(e)[:NODE_CPU_LAST_ERROR_MAX_LEN]
        _patch_status_node_details_cpu(patch, status, {"phase": PHASE_PENDING, "lastError": msg})
        logger.warning("ProxmoxNode CPU details waiting on config/credentials: %s", msg)
        raise kopf.TemporaryError(msg) from e
    except ProxmoxError as e:
        msg = str(e)[:NODE_CPU_LAST_ERROR_MAX_LEN]
        _patch_status_node_details_cpu(patch, status, {"phase": PHASE_PENDING, "lastError": msg})
        logger.warning("ProxmoxNode CPU details Proxmox error (will retry): %s", msg)
        raise kopf.TemporaryError(msg) from e

    cpuinfo = extract_cpuinfo(payload)
    if not cpuinfo:
        msg = "no cpuinfo in Proxmox node status response"
        _patch_status_node_details_cpu(patch, status, {"phase": PHASE_PENDING, "lastError": msg})
        logger.warning("ProxmoxNode CPU details: %s", msg)
        raise kopf.TemporaryError(msg)

    inv = build_inventory_from_cpuinfo(cpuinfo)
    _patch_status_node_details_cpu(patch, status, inv)
    logger.info(
        "ProxmoxNode status.nodeDetails.cpu Ready for node %s (cores=%s brand=%s)",
        node_name,
        inv.get("cores"),
        inv.get("brand"),
    )


def _handle_proxmoxnode_change(
    spec: dict[str, Any],
    status: dict[str, Any],
    meta: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
) -> None:
    ns = meta.get("namespace", "default")
    name = meta.get("name", "")
    try:
        desired = ProxmoxNodeSpec.model_validate(spec)
        if not desired.nodeId:
            logger.error("ProxmoxNode %s/%s: nodeId is required in spec", ns, name)
            return
        if not desired.name:
            logger.error("ProxmoxNode %s/%s: name is required in spec", ns, name)
            return

        _sync_cpu_inventory_from_proxmox(spec, status or {}, patch, logger)

        logger.info(
            "ProxmoxNode %s/%s processed (maintenance mode: %s)",
            ns,
            name,
            desired.maintenanceMode,
        )
    except kopf.TemporaryError:
        raise
    except kopf.PermanentError:
        raise
    except Exception as e:
        logger.exception("Error processing ProxmoxNode %s/%s: %s", ns, name, e)


@kopf.on.create(_NODE_GROUP, _NODE_VERSION, _NODE_PLURAL)
@kopf.on.update(_NODE_GROUP, _NODE_VERSION, _NODE_PLURAL)
def on_proxmoxnode_change(
    spec: dict[str, Any],
    meta: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    _handle_proxmoxnode_change(spec, status or {}, meta, patch, logger)


@kopf.on.resume(_NODE_GROUP, _NODE_VERSION, _NODE_PLURAL)
def on_proxmoxnode_resume(
    spec: dict[str, Any],
    meta: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    logger.info(
        "Operator resumed, processing ProxmoxNode %s/%s",
        meta.get("namespace", "default"),
        meta.get("name"),
    )
    _handle_proxmoxnode_change(spec, status or {}, meta, patch, logger)
