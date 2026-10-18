"""Status management utilities for the SolusVM 2 operator."""

from typing import Any, Optional

import kopf


def set_status(
    patch: kopf.Patch,
    phase: str,
    message: Optional[str] = None,
    observed_generation: Optional[int] = None,
    observed_requested_at: Optional[str] = None,
    server_id: Optional[int] = None,
    uuid: Optional[str] = None,
    task_id: Optional[int] = None,
    compute_resource_id: Optional[int] = None,
    project_id: Optional[int] = None,
    user_id: Optional[int] = None,
    mac_address: Optional[str] = None,
    ipv4: Optional[list[str]] = None,
    ipv6: Optional[str] = None,
    ipv4_gateway: Optional[str] = None,
    ipv6_gateway: Optional[str] = None,
    ipv4_prefix: Optional[str] = None,
    ipv6_prefix: Optional[str] = None,
    dns_servers: Optional[list[str]] = None,
) -> None:
    """Update the status of a SolusVMVM resource."""
    status: dict[str, Any] = {"phase": phase}
    if message is not None:
        status["message"] = message
    if observed_generation is not None:
        status["observedGeneration"] = observed_generation
    if observed_requested_at is not None:
        status["observedRequestedAt"] = observed_requested_at
    if server_id is not None:
        status["serverId"] = server_id
    if uuid is not None:
        status["uuid"] = uuid
    if task_id is not None:
        status["taskId"] = task_id
    if compute_resource_id is not None:
        status["computeResourceId"] = compute_resource_id
    if project_id is not None:
        status["projectId"] = project_id
    if user_id is not None:
        status["userId"] = user_id
    if mac_address is not None:
        status["macAddress"] = mac_address
    if ipv4 is not None:
        status["ipv4"] = ipv4
    if ipv6 is not None:
        status["ipv6"] = ipv6
    if ipv4_gateway is not None:
        status["ipv4Gateway"] = ipv4_gateway
    if ipv6_gateway is not None:
        status["ipv6Gateway"] = ipv6_gateway
    if ipv4_prefix is not None:
        status["ipv4Prefix"] = ipv4_prefix
    if ipv6_prefix is not None:
        status["ipv6Prefix"] = ipv6_prefix
    if dns_servers is not None:
        status["dnsServers"] = dns_servers
    patch.status.update(status)
