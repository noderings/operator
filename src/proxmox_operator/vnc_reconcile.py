"""Reconciliation handlers for VNCRequest resources.

The backend owns VNCRequest create/delete on the consumer (origin). This
operator only reconciles status (Proxmox vncproxy ticket) on the provider.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import kopf
from kubernetes import client as k8s_client

from .client import ProxmoxClient, ProxmoxError
from .config import load_settings_from_env
from .constants import (
    DEFAULT_PROXMOX_PORT,
    PROXMOX_TRANSIENT_ERROR_MARKERS,
    PROXMOX_TRANSIENT_RETRY_DELAY_SECONDS,
)
from .node_handler import resolve_proxmox_url
from .types import (
    GROUP,
    PLURAL,
    VERSION,
    VNCRequestSpec,
    VNCRequestStatus,
    VNC_REQUEST_GROUP,
    VNC_REQUEST_PLURAL,
    VNC_REQUEST_VERSION,
)
from .utils import _get_k8s_client, resolve_vmid_str_from_vm_cr


def _is_transient_proxmox_error(exc: ProxmoxError) -> bool:
    """Return True for transport/lookup failures that should be retried."""
    msg = str(exc).lower()
    return any(marker in msg for marker in PROXMOX_TRANSIENT_ERROR_MARKERS)


def _get_proxmox_client_from_vm_ref(
    proxmox_vm_ref: dict[str, Any], namespace: str, logger: kopf.Logger
) -> tuple[ProxmoxClient, str, str, str]:
    """Get Proxmox client from ProxmoxVM CR reference.

    Args:
        proxmox_vm_ref: Reference to ProxmoxVM CR (name, namespace)
        namespace: Namespace of the VNCRequest (used as fallback for ProxmoxVM namespace)
        logger: Logger instance

    Returns:
        Tuple of (ProxmoxClient, node_name, vm_id, proxmox_url)

    Raises:
        kopf.PermanentError: If ProxmoxVM CR not found or invalid
        kopf.TemporaryError: If Kubernetes API fetch fails transiently
    """
    try:
        _, custom_api = _get_k8s_client()

        # Get ProxmoxVM namespace (use VNCRequest namespace if not specified)
        proxmox_vm_namespace = proxmox_vm_ref.get("namespace") or namespace
        proxmox_vm_name = proxmox_vm_ref.get("name")

        if not proxmox_vm_name:
            raise ValueError("ProxmoxVM reference name is required")

        # Get ProxmoxVM CR
        try:
            proxmox_vm_cr = custom_api.get_namespaced_custom_object(
                group=GROUP,
                version=VERSION,
                namespace=proxmox_vm_namespace,
                plural=PLURAL,
                name=proxmox_vm_name,
            )
        except k8s_client.rest.ApiException as e:
            if e.status == 404:
                raise kopf.PermanentError(
                    f"ProxmoxVM CR '{proxmox_vm_name}' not found in namespace '{proxmox_vm_namespace}'"
                ) from e
            logger.warning(
                "Failed to fetch ProxmoxVM %s/%s (status %s): %s",
                proxmox_vm_namespace,
                proxmox_vm_name,
                e.status,
                e.reason,
            )
            raise kopf.TemporaryError(
                f"failed to fetch ProxmoxVM '{proxmox_vm_namespace}/{proxmox_vm_name}': {e.reason}"
            ) from e

        # Extract nodeRef, nodeName, and vmId; prefer status.vmId (Liqo mirrors status).
        # Resolve the Proxmox URL via the linked ProxmoxNode CR (single source of truth).
        spec = proxmox_vm_cr.get("spec", {})
        node_ref = spec.get("proxmoxNodeRef") or {}
        node_ref_name = node_ref.get("name") if isinstance(node_ref, dict) else None
        proxmox_url = resolve_proxmox_url(node_ref_name, proxmox_vm_namespace, logger)
        vm_config = spec.get("vmConfig", {})
        node_name = vm_config.get("nodeName")
        vm_id = resolve_vmid_str_from_vm_cr(proxmox_vm_cr)

        if not node_name:
            raise kopf.PermanentError(
                f"ProxmoxVM CR '{proxmox_vm_name}' missing nodeName in vmConfig"
            )

        if not vm_id:
            raise kopf.PermanentError(
                f"ProxmoxVM CR '{proxmox_vm_name}' missing status.vmId (and spec.vmConfig.vmId)"
            )

        # Create Proxmox client
        client = ProxmoxClient(load_settings_from_env(proxmox_url))

        logger.info(
            "Retrieved ProxmoxVM CR '%s/%s' with URL %s, node %s, and VMID %s",
            proxmox_vm_namespace,
            proxmox_vm_name,
            proxmox_url,
            node_name,
            vm_id,
        )

        return client, node_name, vm_id, proxmox_url

    except kopf.TemporaryError:
        raise
    except kopf.PermanentError:
        raise
    except Exception as e:
        raise kopf.PermanentError(f"Failed to get ProxmoxVM CR: {e}") from e


@kopf.on.create(VNC_REQUEST_GROUP, VNC_REQUEST_VERSION, VNC_REQUEST_PLURAL)
@kopf.on.update(VNC_REQUEST_GROUP, VNC_REQUEST_VERSION, VNC_REQUEST_PLURAL)
def reconcile_vnc_request(
    spec: dict[str, Any],
    meta: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    """Reconcile a VNCRequest CR by getting VNC credentials from Proxmox.

    Args:
        spec: VNCRequest specification
        meta: Kubernetes metadata
        status: Current status
        patch: Kopf patch object for status updates
        logger: Logger instance
    """
    try:
        # Parse spec
        vnc_spec = VNCRequestSpec.model_validate(spec)

        # Skip if already ready
        if status.get("ready") is True:
            logger.debug("VNCRequest already ready, skipping reconciliation")
            return

        namespace = meta.get("namespace", "default")
        name = meta.get("name")

        # Get Proxmox client, node name, VMID, and URL from ProxmoxVM reference
        proxmox_client, node_name, vm_id, proxmox_url = _get_proxmox_client_from_vm_ref(
            vnc_spec.proxmoxVMRef.model_dump(), namespace, logger
        )

        logger.info(
            "Reconciling VNCRequest %s/%s for VM %s on node %s",
            namespace,
            name,
            vm_id,
            node_name,
        )

        # Get VNC ticket (and optional cert) from Proxmox vnproxy API
        vmid = int(vm_id)
        logger.info("[API CALL] get_vnc_ticket node=%s vmid=%s", node_name, vmid)
        vnc_data = proxmox_client.get_vnc_ticket(node_name, vmid)

        # Derive Proxmox host and port from the ProxmoxNode-resolved proxmoxUrl
        # (no node IP lookup needed).
        url_to_parse = (
            proxmox_url
            if proxmox_url.startswith(("http://", "https://"))
            else f"https://{proxmox_url}"
        )
        parsed_url = urlparse(url_to_parse)
        proxmox_ip = parsed_url.hostname or node_name
        proxmox_port = parsed_url.port if parsed_url.port is not None else DEFAULT_PROXMOX_PORT

        # Update status with vnproxy-style fields
        patch.status.update(
            VNCRequestStatus(
                ready=True,
                caCert=vnc_data.get("cert"),
                vncport=vnc_data["port"],
                ticket=vnc_data["ticket"],
                password=vnc_data.get("password"),
                vmid=str(vm_id),
                nodeName=node_name,
                proxmoxIp=proxmox_ip,
                proxmoxPort=proxmox_port,
                error=None,
            ).model_dump(exclude_none=True)
        )

        logger.info(
            "VNCRequest %s/%s reconciled successfully: vncport=%s, proxmoxIp=%s, proxmoxPort=%s",
            namespace,
            name,
            vnc_data["port"],
            proxmox_ip,
            proxmox_port,
        )

    except kopf.TemporaryError:
        raise
    except kopf.PermanentError as e:
        error_msg = str(e)
        logger.error("Permanent error reconciling VNCRequest: %s", error_msg)
        patch.status.update(
            VNCRequestStatus(ready=False, error=error_msg).model_dump(exclude_none=True)
        )
    except ProxmoxError as e:
        error_msg = f"Proxmox API error: {e}"
        logger.error("Proxmox error reconciling VNCRequest: %s", error_msg)
        patch.status.update(
            VNCRequestStatus(ready=False, error=error_msg).model_dump(exclude_none=True)
        )
        if _is_transient_proxmox_error(e):
            raise kopf.TemporaryError(error_msg, delay=PROXMOX_TRANSIENT_RETRY_DELAY_SECONDS) from e
    except Exception as e:
        error_msg = f"Unexpected error: {e}"
        logger.exception("Unexpected error reconciling VNCRequest: %s", error_msg)
        patch.status.update(
            VNCRequestStatus(ready=False, error=error_msg).model_dump(exclude_none=True)
        )
