"""Status management utilities for the Proxmox operator."""

from typing import Any, Dict, Optional

import kopf


def set_status(
    patch: kopf.Patch,
    phase: str,
    message: Optional[str] = None,
    observed_generation: Optional[int] = None,
    vm_id: Optional[str] = None,
) -> None:
    """
    Update the status of a ProxmoxVM resource.

    Args:
        patch: Kopf patch object for status updates
        phase: Current phase of the VM (e.g., "Ready", "Error")
        message: Optional status message
        observed_generation: Optional observed generation number
        vm_id: Optional allocated Proxmox VMID (status.vmId; Liqo mirrors status only)
    """
    status: Dict[str, Any] = {
        "phase": phase,
    }
    if message is not None:
        status["message"] = message
    if observed_generation is not None:
        status["observedGeneration"] = observed_generation
    if vm_id is not None:
        status["vmId"] = vm_id
    patch.status.update(status)
