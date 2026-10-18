"""Create decisions that do not need Kubernetes. Handlers call these, then the panel."""

from __future__ import annotations

from typing import Any


def empty_required_keys(environment: dict[str, str], required: list[str]) -> list[str]:
    missing = []
    for key in required:
        if not str(environment.get(key, "")).strip():
            missing.append(key)
    return missing


def create_should_stop(spec: dict[str, Any], status: dict[str, Any] | None) -> str | None:
    """Return an error message when create must not call the panel, or None to continue.

    Ready with a server id is a no-op, signalled by the empty string.
    """
    status = status or {}
    if status.get("phase") == "Ready" and status.get("serverId"):
        return ""
    if spec.get("maintenance"):
        return "node is in maintenance"
    required = [str(key) for key in (spec.get("requiredEnvironment") or [])]
    environment = {
        str(k): "" if v is None else str(v) for k, v in (spec.get("environment") or {}).items()
    }
    missing = empty_required_keys(environment, required)
    if missing:
        return "required environment is empty: " + ", ".join(missing)
    return None


def power_signal(operation: str) -> str | None:
    return {
        "started": "start",
        "stopped": "stop",
        "restarted": "restart",
        "kill": "kill",
    }.get(operation)
