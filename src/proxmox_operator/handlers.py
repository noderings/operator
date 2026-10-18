"""Kopf handlers for ProxmoxVM resources."""

from __future__ import annotations

import logging
from typing import Any

import kopf

from . import __version__
from .client import ProxmoxClient, ProxmoxError
from .config import load_settings_from_env
from .constants import (
    OP_CREATED,
    PHASE_ERROR,
    PHASE_READY,
    PROXMOX_TRANSIENT_ERROR_MARKERS,
    PROXMOX_TRANSIENT_RETRY_DELAY_SECONDS,
)
from .node_handler import resolve_proxmox_url
from .operations import get_operation_handler
from .status import set_status
from .types import GROUP, KIND, PLURAL, VERSION, ProxmoxVMSpec
from .utils import check_maintenance_mode, should_skip_operation

# Import reconciliation handlers to register them with Kopf
from . import reconcile  # noqa: F401
from . import firewall_reconcile  # noqa: F401
from . import backup_reconcile  # noqa: F401
from . import snapshot_reconcile  # noqa: F401
from .reconcile import _reconcile_vm_firewall_state
from . import vnc_reconcile  # noqa: F401
from . import node_handler  # noqa: F401
from noderings_hooks import watcher as _hook_watcher  # noqa: F401
from noderings_hooks.runner import dispatch_hook


def _addresses_from_proxmox_spec(spec: ProxmoxVMSpec) -> dict[str, Any]:
    """Guest IPs for hook env. Proxmox only has static cloud-init here.

    DHCP / qemu-guest-agent addresses are not published on ProxmoxVM status,
    so ``NODERINGS_IPV4`` / event JSON stay empty unless ``staticIpConfig`` is set.
    """
    cloud_init = spec.vmConfig.cloudInit if spec.vmConfig else None
    static_ip = cloud_init.staticIpConfig if cloud_init else None
    if static_ip is None:
        return {}
    ipv4: list[str] = []
    if static_ip.ipv4Address:
        ipv4.append(static_ip.ipv4Address)
    for extra in static_ip.additionalIpv4Addresses or []:
        if extra.address:
            ipv4.append(extra.address)
    ipv6 = [static_ip.ipv6Address] if static_ip.ipv6Address else []
    return {
        "ipv4": ipv4,
        "ipv6": ipv6,
        "ipv4Gateway": static_ip.ipv4Gateway or "",
        "ipv6Gateway": static_ip.ipv6Gateway or "",
        "ipv4Prefix": static_ip.ipv4Prefix or "",
        "ipv6Prefix": static_ip.ipv6Prefix or "",
    }


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_: Any) -> None:
    """Configure Kopf operator settings."""
    settings.posting.level = logging.INFO
    settings.peering.standalone = False
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(
        prefix="proxmox-operator.kopf.dev"
    )

    # Print version at startup
    logger = logging.getLogger(__name__)
    logger.info(f"Proxmox Operator starting - Version: {__version__}")


def _get_client(proxmox_url: str) -> ProxmoxClient:
    return ProxmoxClient(load_settings_from_env(proxmox_url))


def _client_for_vm(spec: ProxmoxVMSpec, meta: dict[str, Any], logger: kopf.Logger) -> ProxmoxClient:
    """Build a Proxmox client by resolving the URL from the linked ProxmoxNode."""
    namespace = meta.get("namespace", "default")
    proxmox_url = resolve_proxmox_url(spec.proxmoxNodeRef.name, namespace, logger)
    return _get_client(proxmox_url)


def _is_transient_proxmox_error(exc: ProxmoxError) -> bool:
    """Return True for transport/lookup failures that should be retried."""
    msg = str(exc).lower()
    return any(marker in msg for marker in PROXMOX_TRANSIENT_ERROR_MARKERS)


def _handle_operation(
    spec: ProxmoxVMSpec,
    meta: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    op_type: str,
) -> None:
    """
    Handle a VM operation (create, start, stop, etc.).

    Args:
        spec: ProxmoxVM specification
        meta: Kubernetes metadata
        status: Current status
        patch: Kopf patch object for status updates
        logger: Logger instance
        op_type: Operation type to execute
    """
    # Check if maintenance mode is enabled - if so, skip operation entirely
    maintenance_mode = check_maintenance_mode(spec, meta, logger)
    if maintenance_mode:
        logger.info(
            "Maintenance mode enabled: skipping operation %s. CR desired state will not be enforced.",
            op_type,
        )
        # Do not update status or perform any operations
        return

    try:
        client = _client_for_vm(spec, meta, logger)

        # Idempotency check: skip if VM is already in desired state in Proxmox
        if should_skip_operation(client, spec, op_type, logger, status=status, meta=meta):
            logger.info(
                "Skipping operation %s - VM already in desired state (idempotency check)",
                op_type,
            )
            # Ensure status.vmId is set when skipping create (Liqo mirrors status only).
            vm_id: str | None = None
            if op_type == OP_CREATED:
                existing = client.find_vm_by_name(spec.vmConfig.nodeName, spec.vmConfig.name)
                if existing:
                    vm_id = str(existing)
            else:
                existing_status_vm_id = status.get("vmId")
                if existing_status_vm_id:
                    vm_id = str(existing_status_vm_id)
            set_status(
                patch,
                phase=PHASE_READY,
                message=f"VM already in desired state for {op_type} operation",
                observed_generation=meta.get("generation"),
                vm_id=vm_id,
            )
            # Retry hook dispatch after a concurrency skip; job name is at-most-once.
            dispatch_hook(
                hooks=spec.hooks,
                op_type=op_type,
                meta=meta,
                status=status,
                logger=logger,
                group=GROUP,
                version=VERSION,
                plural=PLURAL,
                kind=KIND,
                vmid=vm_id,
                addresses=_addresses_from_proxmox_spec(spec),
                patch=patch,
            )
            return

        # Get operation handler and execute
        handler = get_operation_handler(
            op_type=op_type,
            client=client,
            spec=spec,
            patch=patch,
            meta=meta,
            logger=logger,
            status=status,
        )

        message, vmid = handler.execute()

        # Update status (include allocated vmId for create/reinstall; Liqo mirrors status only)
        set_status(
            patch,
            phase=PHASE_READY,
            message=message,
            observed_generation=meta.get("generation"),
            vm_id=str(vmid) if vmid and vmid > 0 else None,
        )
        dispatch_hook(
            hooks=spec.hooks,
            op_type=op_type,
            meta=meta,
            status=status,
            logger=logger,
            group=GROUP,
            version=VERSION,
            plural=PLURAL,
            kind=KIND,
            vmid=str(vmid) if vmid and vmid > 0 else None,
            addresses=_addresses_from_proxmox_spec(spec),
            patch=patch,
        )

        logger.info(
            "Completed Proxmox VM operation %s on node %s (VMID: %s, proxmoxNodeRef: %s)",
            op_type,
            spec.vmConfig.nodeName,
            vmid,
            spec.proxmoxNodeRef.name,
        )

    except ProxmoxError as e:
        set_status(patch, phase=PHASE_ERROR, message=str(e))
        err_msg = f"Failed to {op_type} VM: {e}"
        if _is_transient_proxmox_error(e):
            raise kopf.TemporaryError(err_msg, delay=PROXMOX_TRANSIENT_RETRY_DELAY_SECONDS) from e
        raise kopf.PermanentError(err_msg) from e


@kopf.on.create(GROUP, VERSION, PLURAL)
def on_create(
    spec: dict[str, Any],
    meta: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    """Handle ProxmoxVM creation: clone template, configure, and start."""
    desired = ProxmoxVMSpec.model_validate(spec)
    op_type = desired.operation.type if desired.operation else OP_CREATED

    _handle_operation(desired, meta, status, patch, logger, op_type)

    # Reconcile firewall state immediately after operation completes
    # This ensures immediate cleanup when firewall is detached (even if on_create is triggered)
    try:
        client = _client_for_vm(desired, meta, logger)
        node = desired.vmConfig.nodeName
        vm_name = desired.vmConfig.name
        vmid = client.find_vm_by_name(node, vm_name)
        if vmid is not None:
            _reconcile_vm_firewall_state(desired, meta, client, node, vmid, logger)
            # Backup reconciliation disabled (Phase 1 backup redesign)
        else:
            logger.debug(
                "VM %s not found on node %s, skipping firewall reconciliation", vm_name, node
            )
    except Exception as e:
        logger.warning("Failed to reconcile firewall state after create: %s", e)


@kopf.on.update(GROUP, VERSION, PLURAL)
def on_update(
    spec: dict[str, Any],
    meta: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    """Handle ProxmoxVM updates: execute operation based on op_type change."""
    desired = ProxmoxVMSpec.model_validate(spec)

    # Only process if operation is specified
    if not desired.operation:
        logger.info("No operation specified, skipping update")
        # Still reconcile firewall state even if no operation is specified
        # This ensures immediate cleanup when firewall is detached
        try:
            client = _client_for_vm(desired, meta, logger)
            node = desired.vmConfig.nodeName
            vm_name = desired.vmConfig.name
            vmid = client.find_vm_by_name(node, vm_name)
            if vmid is not None:
                _reconcile_vm_firewall_state(desired, meta, client, node, vmid, logger)
                # Backup reconciliation disabled (Phase 1 backup redesign)
            else:
                logger.debug(
                    "VM %s not found on node %s, skipping firewall reconciliation", vm_name, node
                )
        except Exception as e:
            logger.warning("Failed to reconcile firewall state during update: %s", e)
        return

    op_type = desired.operation.type
    logger.info("Processing update with operation: %s", op_type)

    _handle_operation(desired, meta, status, patch, logger, op_type)

    # Reconcile firewall state immediately after operation completes
    # This ensures immediate cleanup when firewall is detached
    try:
        client = _client_for_vm(desired, meta, logger)
        node = desired.vmConfig.nodeName
        vm_name = desired.vmConfig.name
        vmid = client.find_vm_by_name(node, vm_name)
        if vmid is not None:
            _reconcile_vm_firewall_state(desired, meta, client, node, vmid, logger)
            # Backup reconciliation disabled (Phase 1 backup redesign)
        else:
            logger.debug(
                "VM %s not found on node %s, skipping firewall reconciliation", vm_name, node
            )
    except Exception as e:
        logger.warning("Failed to reconcile firewall state after operation: %s", e)
