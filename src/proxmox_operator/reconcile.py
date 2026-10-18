"""Reconciliation loop for ProxmoxVM resources."""

from __future__ import annotations

import logging
import os
from typing import Any

import kopf

from .client import ProxmoxClient, ProxmoxError
from .config import load_settings_from_env
from .constants import (
    OP_CREATED,
    OP_DELETED,
    OP_REINSTALLED,
    OP_RESTARTED,
    OP_STARTED,
    OP_STOPPED,
    PHASE_DELETING,
    PHASE_ERROR,
    PHASE_PROVISIONING,
    PHASE_READY,
    VM_STATUS_RUNNING,
    VM_STATUS_STOPPED,
)
from .node_handler import resolve_proxmox_url
from .operations import get_operation_handler
from .status import set_status
from .types import GROUP, PLURAL, ProxmoxVMSpec, VERSION
from .utils import check_maintenance_mode
from .firewall_reconcile import (
    _delete_ipset_entries,
)
from .smtp_outbound import (
    delete_non_smtp_firewall_rules,
    reconcile_vm_smtp_outbound_block,
)

logger = logging.getLogger(__name__)

# Default reconciliation interval in seconds (5 minutes)
# Can be configured via RECONCILE_INTERVAL_SECONDS environment variable
DEFAULT_RECONCILE_INTERVAL = 300


def _get_reconcile_interval() -> int:
    """Get reconciliation interval from environment or default.

    Returns:
        Reconciliation interval in seconds
    """
    interval_str = os.getenv("RECONCILE_INTERVAL_SECONDS")
    if interval_str:
        try:
            interval = int(interval_str)
            if interval < 60:
                logger.warning(
                    "Reconciliation interval %s is less than 60 seconds, "
                    "using minimum of 60 seconds",
                    interval,
                )
                return 60
            return interval
        except ValueError:
            logger.warning(
                "Invalid RECONCILE_INTERVAL_SECONDS value: %s, using default: %s",
                interval_str,
                DEFAULT_RECONCILE_INTERVAL,
            )
    return DEFAULT_RECONCILE_INTERVAL


# Calculate reconciliation interval at module load time
RECONCILE_INTERVAL = _get_reconcile_interval()


def _get_client(proxmox_url: str) -> ProxmoxClient:
    return ProxmoxClient(load_settings_from_env(proxmox_url))


def _client_for_vm(spec: ProxmoxVMSpec, meta: dict[str, Any], logger: kopf.Logger) -> ProxmoxClient:
    """Build a Proxmox client by resolving the URL from the linked ProxmoxNode."""
    namespace = meta.get("namespace", "default")
    proxmox_url = resolve_proxmox_url(spec.proxmoxNodeRef.name, namespace, logger)
    return _get_client(proxmox_url)


def _reconcile_vm_firewall_state(
    spec: ProxmoxVMSpec,
    meta: dict[str, Any],
    client: ProxmoxClient,
    node: str,
    vmid: int,
    logger: kopf.Logger,
) -> None:
    """Reconcile firewall state for a VM.

    Provider SMTP outbound rules are always reconciled from spec.blockOutboundSmtp.
    Attached rulesets are applied by the FirewallRuleSet timer — this path only
    cleans up orphaned Proxmox firewall resources when the VM has no ruleset ref.
    NIC firewall=1 is enabled at VM create (for ipfilter); attach/detach does not
    toggle it.
    """
    try:
        vm_name = spec.vmConfig.name
        proxmox = client._get_connection()

        # Always reconcile provider SMTP block (independent of FirewallRuleSet).
        reconcile_vm_smtp_outbound_block(
            proxmox,
            node,
            vmid,
            bool(spec.blockOutboundSmtp),
            logger,
        )

        # Ruleset apply is owned by FirewallRuleSet timer reconcile.
        # NIC firewall=1 is enabled at VM create (for ipfilter); attach/detach only
        # manages ruleset rules, not the NIC firewall flag.
        if spec.firewallRuleSet:
            logger.debug(
                "VM %s (vmid=%s) references FirewallRuleSet %s; rules applied by ruleset timer",
                vm_name,
                vmid,
                spec.firewallRuleSet.name,
            )
        else:
            # VM has no ruleset — clean up orphaned managed firewall resources.
            # Managed resources: IPSets starting with "frs-", plus non-SMTP guest rules.
            try:
                existing_rules = proxmox.nodes(node).qemu(vmid).firewall.rules.get() or []
                non_smtp_count = sum(
                    1
                    for r in existing_rules
                    if isinstance(r, dict)
                    and not str(r.get("comment") or "").startswith("noderings:smtp-outbound-block:")
                )

                ipsets = proxmox.nodes(node).qemu(vmid).firewall.ipset.get() or []
                managed_ipsets = []
                for ipset in ipsets:
                    ipset_name = ipset.get("name", "")
                    if ipset_name.startswith("ipfilter-"):
                        continue
                    if ipset_name.startswith("frs-"):
                        managed_ipsets.append(ipset_name)

                if non_smtp_count > 0 or managed_ipsets:
                    logger.info(
                        "Found orphaned firewall resources on VM without ruleset reference: "
                        "vm=%s, vmid=%s, non_smtp_rules=%d, ipsets=%d",
                        vm_name,
                        vmid,
                        non_smtp_count,
                        len(managed_ipsets),
                    )

                    if non_smtp_count > 0:
                        delete_non_smtp_firewall_rules(proxmox, node, vmid, logger)

                    for ipset_name in managed_ipsets:
                        try:
                            _delete_ipset_entries(proxmox, node, vmid, ipset_name, logger)
                            try:
                                proxmox.nodes(node).qemu(vmid).firewall.ipset(ipset_name).delete()
                                logger.info(
                                    "Cleaned up orphaned IPSet: vm=%s, vmid=%s, ipset=%s",
                                    vm_name,
                                    vmid,
                                    ipset_name,
                                )
                            except Exception as e:
                                logger.warning(
                                    "Failed to delete orphaned IPSet %s for VM %s (vmid: %s): %s",
                                    ipset_name,
                                    vm_name,
                                    vmid,
                                    e,
                                )
                        except Exception as e:
                            logger.warning(
                                "Failed to cleanup orphaned IPSet %s for VM %s (vmid: %s): %s",
                                ipset_name,
                                vm_name,
                                vmid,
                                e,
                            )

                    logger.info(
                        "Cleaned up orphaned firewall resources: vm=%s, vmid=%s, non_smtp_rules=%d, ipsets=%d",
                        vm_name,
                        vmid,
                        non_smtp_count,
                        len(managed_ipsets),
                    )
                else:
                    logger.debug(
                        "No orphaned firewall resources found on VM: vm=%s, vmid=%s",
                        vm_name,
                        vmid,
                    )
            except Exception as e:
                logger.warning(
                    "Failed to check for orphaned firewall resources on VM %s: %s", vm_name, e
                )

        try:
            client.reconcile_ipfilter_ipsets(node, vmid, spec.vmConfig)
        except Exception as e:
            logger.warning(
                "Failed to reconcile ipfilter ipsets for VM %s (vmid=%s): %s", vm_name, vmid, e
            )
    except Exception as e:
        logger.warning("Failed to reconcile firewall state for VM: %s", e)
        # Don't fail VM reconciliation if firewall reconciliation fails


def _reconcile_vm_state(
    spec: ProxmoxVMSpec,
    meta: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
) -> None:
    """
    Reconcile a single ProxmoxVM resource by comparing desired vs actual state.

    Args:
        spec: ProxmoxVM specification
        meta: Kubernetes metadata
        status: Current status
        patch: Kopf patch object for status updates
        logger: Logger instance
    """
    # Skip reconciliation if no operation specified. Pydantic + the CRD schema
    # already enforce required fields (proxmoxNodeRef.name, vmConfig); the URL
    # is validated when the ProxmoxNode is resolved below.
    if not spec.operation:
        logger.debug("Skipping reconciliation: No operation specified")
        return

    # Check if maintenance mode is enabled - if so, completely skip reconciliation
    maintenance_mode = check_maintenance_mode(spec, meta, logger)
    if maintenance_mode:
        logger.info(
            "Maintenance mode enabled: skipping reconciliation entirely. "
            "CR desired state will not be enforced."
        )
        # Do not update status or perform any operations
        return

    # Initialize current_gen for error handling
    current_gen = meta.get("generation", 0)
    observed_gen = status.get("observedGeneration")

    try:
        client = _client_for_vm(spec, meta, logger)
        node = spec.vmConfig.nodeName
        op_type = spec.operation.type
        vmid: int | None = None

        # CR is the source of truth - enforce desired state
        # Always check actual Proxmox state to detect drift (e.g., manual VM stop)
        # If CR says "Created" and VM doesn't exist, create it
        # If CR says "Started" and VM is stopped, start it
        # If CR says "Stopped" and VM is running, stop it
        # etc.

        expected_name = spec.vmConfig.name

        if op_type == OP_REINSTALLED:
            phase = status.get("phase")
            if phase in (PHASE_DELETING, PHASE_PROVISIONING) and observed_gen == current_gen:
                logger.debug(
                    "Reinstall in progress (phase=%s) for %s, skipping reconcile",
                    phase,
                    expected_name,
                )
                return

            if observed_gen == current_gen and phase == PHASE_READY:
                vmid = client.find_vm_by_name(node, expected_name)
                if vmid is not None:
                    actual_status = client.get_vm_status(node, vmid)
                    if observed_gen != current_gen or phase != PHASE_READY:
                        set_status(
                            patch,
                            phase=PHASE_READY,
                            message=f"Reinstall complete (status: {actual_status})",
                            observed_generation=current_gen,
                        )
                    _reconcile_vm_firewall_state(spec, meta, client, node, vmid, logger)
                    return
                logger.info(
                    "Reinstall marked Ready but guest %s missing on %s; re-running handler",
                    expected_name,
                    node,
                )
            elif phase in (PHASE_DELETING, PHASE_PROVISIONING):
                return

            handler = get_operation_handler(
                op_type=op_type,
                client=client,
                spec=spec,
                patch=patch,
                meta=meta,
                logger=logger,
            )
            message, executed_vmid = handler.execute()
            set_status(
                patch,
                phase=PHASE_READY,
                message=f"Reconciled reinstall: {message}",
                observed_generation=current_gen,
            )
            lookup_vmid = (
                executed_vmid if executed_vmid else client.find_vm_by_name(node, expected_name)
            )
            if lookup_vmid:
                _reconcile_vm_firewall_state(spec, meta, client, node, lookup_vmid, logger)
            return

        # For create operations, check if VM exists by name
        if op_type == OP_CREATED:
            vmid = client.find_vm_by_name(node, expected_name)
            if vmid is not None:
                logger.debug(
                    "VM with name '%s' (VMID: %s) already exists on node %s. No reconciliation needed.",
                    expected_name,
                    vmid,
                    node,
                )
                # Update status to reflect that we've verified the state
                if status.get("phase") != PHASE_READY:
                    actual_status = client.get_vm_status(node, vmid)
                    set_status(
                        patch,
                        phase=PHASE_READY,
                        message=f"VM already exists (status: {actual_status})",
                        observed_generation=current_gen,
                    )
                # Update observed generation if not already set
                elif observed_gen != current_gen:
                    actual_status = client.get_vm_status(node, vmid)
                    set_status(
                        patch,
                        phase=PHASE_READY,
                        message=f"VM already exists (status: {actual_status})",
                        observed_generation=current_gen,
                    )
                # Still reconcile firewall state even if VM already exists
                _reconcile_vm_firewall_state(spec, meta, client, node, vmid, logger)
                return
            # VM doesn't exist - need to create
            logger.info(
                "Reconciliation needed: CR desired state is 'Created' but VM with name '%s' doesn't exist. Creating VM.",
                expected_name,
            )
        else:
            # For other operations, find VM by name
            vmid = client.find_vm_by_name(node, expected_name)
            if vmid is None:
                # For delete operations, if VM doesn't exist, treat as success (already deleted)
                if op_type == OP_DELETED:
                    # TODO: Temporarily set to DEBUG - change back to INFO later
                    logger.debug(
                        "VM with name '%s' does not exist on node %s, already deleted",
                        expected_name,
                        node,
                    )
                    set_status(
                        patch,
                        phase=PHASE_READY,
                        message="VM already deleted",
                        observed_generation=current_gen,
                    )
                    return
                if op_type == OP_REINSTALLED:
                    phase = status.get("phase")
                    if phase in (PHASE_DELETING, PHASE_PROVISIONING):
                        logger.debug(
                            "Reinstall in progress (phase=%s), guest temporarily missing",
                            phase,
                        )
                        return
                    # Fall through to reinstall handler below
                else:
                    # For other operations, VM not found is an error
                    error_msg = f"Cannot reconcile {op_type}: VM with name '{expected_name}' does not exist on node {node}"
                    logger.warning(error_msg)
                    set_status(
                        patch,
                        phase=PHASE_ERROR,
                        message=error_msg,
                        observed_generation=current_gen,
                    )
                    return

            # Get actual VM status from Proxmox (always check, even if generation hasn't changed)
            # This detects drift when VM is manually stopped/started on Proxmox
            actual_status = client.get_vm_status(node, vmid)
            logger.info(
                "Reconciliation check: VM %s (name: %s), desired operation=%s, actual status=%s, observed_gen=%s, current_gen=%s",
                vmid,
                expected_name,
                op_type,
                actual_status,
                observed_gen,
                current_gen,
            )

            # Check if VM is already in desired state (idempotency)
            should_skip = False
            if op_type == OP_STARTED and actual_status == VM_STATUS_RUNNING:
                should_skip = True
            elif op_type == OP_STOPPED and actual_status == VM_STATUS_STOPPED:
                should_skip = True
            elif op_type == OP_RESTARTED:
                # Restart is an action, not a steady desired state.
                # Execute it once per spec generation, then skip on timer loops
                # until a new generation requests restart again.
                if observed_gen == current_gen and status.get("phase") == PHASE_READY:
                    should_skip = True
            elif op_type == OP_REINSTALLED:
                should_skip = True

            if should_skip:
                logger.debug(
                    "VM %s (name: %s) is already in desired state for operation %s. No reconciliation needed.",
                    vmid,
                    expected_name,
                    op_type,
                )
                # Update status and observed generation if needed
                if observed_gen != current_gen or status.get("phase") != PHASE_READY:
                    set_status(
                        patch,
                        phase=PHASE_READY,
                        message=f"VM is in desired state (status: {actual_status})",
                        observed_generation=current_gen,
                    )
                # Still reconcile firewall state even if VM is in desired state
                _reconcile_vm_firewall_state(spec, meta, client, node, vmid, logger)
                return

            # VM is not in desired state - CR is source of truth, so enforce it
            # This handles drift detection (e.g., VM manually stopped when CR says "started")
            logger.info(
                "Reconciliation needed for VM %s (name: %s): CR desired state is '%s' but actual status is '%s'. Enforcing CR state.",
                vmid,
                expected_name,
                op_type,
                actual_status,
            )

        # Perform reconciliation using the operation handler
        handler = get_operation_handler(
            op_type=op_type,
            client=client,
            spec=spec,
            patch=patch,
            meta=meta,
            logger=logger,
        )

        message, executed_vmid = handler.execute()

        # Update status to reflect successful reconciliation
        set_status(
            patch,
            phase=PHASE_READY,
            message=f"Reconciled: {message}",
            observed_generation=current_gen,
        )

        logger.info(
            "Successfully reconciled VM %s (name: %s) on node %s: %s",
            executed_vmid if executed_vmid else "new",
            spec.vmConfig.name,
            node,
            message,
        )

        # Reconcile firewall state after VM operations are complete
        # This ensures firewall rules match the VM's firewall ruleset reference
        lookup_vmid = executed_vmid if executed_vmid else vmid
        if lookup_vmid:
            _reconcile_vm_firewall_state(spec, meta, client, node, lookup_vmid, logger)

    except ProxmoxError as e:
        error_msg = f"Reconciliation failed: {str(e)}"
        vm_name = spec.vmConfig.name
        logger.error(
            "Failed to reconcile VM with name '%s' on node %s: %s",
            vm_name,
            spec.vmConfig.nodeName,
            e,
        )
        set_status(
            patch,
            phase=PHASE_ERROR,
            message=error_msg,
            observed_generation=current_gen,
        )
    except Exception as e:
        error_msg = f"Unexpected error during reconciliation: {str(e)}"
        vm_name = spec.vmConfig.name
        logger.exception(
            "Unexpected error reconciling VM with name '%s' on node %s",
            vm_name,
            spec.vmConfig.nodeName,
        )
        set_status(
            patch,
            phase=PHASE_ERROR,
            message=error_msg,
            observed_generation=current_gen,
        )


@kopf.timer(
    GROUP,
    VERSION,
    PLURAL,
    interval=RECONCILE_INTERVAL,
    idle=RECONCILE_INTERVAL,
)
def reconcile_all(
    spec: dict[str, Any],
    meta: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    """
    Periodic reconciliation handler for all ProxmoxVM resources.

    This handler runs periodically (default: every 5 minutes) to check all
    ProxmoxVM resources and reconcile any drift between desired and actual state.

    Args:
        spec: ProxmoxVM specification
        meta: Kubernetes metadata
        status: Current status
        patch: Kopf patch object for status updates
        logger: Logger instance
    """
    try:
        desired = ProxmoxVMSpec.model_validate(spec)
        _reconcile_vm_state(desired, meta, status, patch, logger)
    except Exception as e:
        logger.error(
            "Error during reconciliation: %s",
            e,
            exc_info=True,
        )
        set_status(
            patch,
            phase=PHASE_ERROR,
            message=f"Reconciliation error: {str(e)}",
            observed_generation=meta.get("generation"),
        )


@kopf.on.resume(GROUP, VERSION, PLURAL)
def on_resume(
    spec: dict[str, Any],
    meta: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    """
    Reconcile resources when operator resumes (e.g., after restart).

    This ensures that when the operator restarts, it immediately checks
    the state of all ProxmoxVM resources.

    Args:
        spec: ProxmoxVM specification
        meta: Kubernetes metadata
        status: Current status
        patch: Kopf patch object for status updates
        logger: Logger instance
    """
    logger.info(
        "Operator resumed, reconciling ProxmoxVM %s/%s",
        meta.get("namespace", "default"),
        meta.get("name"),
    )
    try:
        desired = ProxmoxVMSpec.model_validate(spec)
        _reconcile_vm_state(desired, meta, status, patch, logger)
    except Exception as e:
        logger.error(
            "Error during resume reconciliation: %s",
            e,
            exc_info=True,
        )
        set_status(
            patch,
            phase=PHASE_ERROR,
            message=f"Resume reconciliation error: {str(e)}",
            observed_generation=meta.get("generation"),
        )
