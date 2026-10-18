"""Configuration management for SolusVM 2 connections."""

import os
import re
from dataclasses import dataclass


@dataclass
class SolusVMCredentials:
    """Credentials for a SolusVM 2 management node."""

    instance_id: str
    url: str
    token: str


def _discover_instances() -> dict[str, SolusVMCredentials]:
    """Discover configured SolusVM instances from environment variables.

    Looks for SOLUSVM_<ID>_URL and SOLUSVM_<ID>_TOKEN pairs.
    Falls back to single-instance SOLUSVM_URL / SOLUSVM_TOKEN.
    """
    instances: dict[str, SolusVMCredentials] = {}
    pattern = re.compile(r"^SOLUSVM_(.+)_URL$")

    for env_key, env_value in os.environ.items():
        match = pattern.match(env_key)
        if not match:
            continue
        instance_id = match.group(1)
        if instance_id == "":
            continue
        token = os.getenv(f"SOLUSVM_{instance_id}_TOKEN")
        if token:
            url = env_value.rstrip("/")
            instances[url] = SolusVMCredentials(
                instance_id=instance_id,
                url=url,
                token=token,
            )

    if not instances:
        url = os.getenv("SOLUSVM_URL", "").rstrip("/")
        token = os.getenv("SOLUSVM_TOKEN", "")
        if url and token:
            instances[url] = SolusVMCredentials(
                instance_id="default",
                url=url,
                token=token,
            )

    return instances


def load_credentials(endpoint: str) -> SolusVMCredentials:
    """Load SolusVM credentials for a given management-node URL.

    Args:
        endpoint: Management node URL (e.g. https://mn.example.com)

    Raises:
        ValueError: If no credentials are found for the endpoint.
    """
    instances = _discover_instances()
    normalized = endpoint.rstrip("/")

    if normalized in instances:
        return instances[normalized]

    stripped = re.sub(r"^https?://", "", normalized, flags=re.IGNORECASE)
    for stored_url, creds in instances.items():
        stored_stripped = re.sub(r"^https?://", "", stored_url, flags=re.IGNORECASE)
        if stored_stripped == stripped:
            return creds

    if len(instances) == 1:
        return next(iter(instances.values()))

    raise ValueError(
        f"No credentials found for SolusVM endpoint: {endpoint}. "
        "Configure SOLUSVM_URL + SOLUSVM_TOKEN or SOLUSVM_<ID>_URL + SOLUSVM_<ID>_TOKEN."
    )
