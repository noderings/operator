"""Patch PterodactylServer status. Do not put Wings tokens here."""

from __future__ import annotations

from typing import Any

import kopf


def set_status(
    patch: kopf.Patch,
    *,
    phase: str,
    message: str | None = None,
    server_id: int | None = None,
    uuid: str | None = None,
    identifier: str | None = None,
    node_id: int | None = None,
    allocation: dict[str, Any] | None = None,
    observed_requested_at: str | None = None,
    egg_change_step: str | None = None,
    egg_change_request: str | None = None,
) -> None:
    status: dict[str, Any] = {"phase": phase}
    if message is not None:
        status["message"] = message
    if server_id is not None:
        status["serverId"] = server_id
    if uuid is not None:
        status["uuid"] = uuid
    if identifier is not None:
        status["identifier"] = identifier
    if node_id is not None:
        status["nodeId"] = node_id
    if allocation is not None:
        status["allocation"] = allocation
    if observed_requested_at is not None:
        status["observedRequestedAt"] = observed_requested_at
    if egg_change_step is not None:
        status["eggChangeStep"] = egg_change_step
    if egg_change_request is not None:
        status["eggChangeRequest"] = egg_change_request
    patch.status.update(status)
