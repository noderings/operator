"""Configuration management for VirtFusion connections."""

import os
import re
from dataclasses import dataclass


@dataclass
class VirtFusionCredentials:
    """Credentials for a VirtFusion instance."""

    instance_id: str
    url: str
    token: str
    user_api_token: str = ""
    user_id: int = 0
    user_name: str = ""


def _parse_user_id(raw: str) -> int:
    s = (raw or "").strip()
    if not s:
        return 0
    try:
        n = int(s)
    except ValueError:
        return 0
    return n if n > 0 else 0


def _user_fields(instance_id: str | None) -> tuple[str, int, str]:
    """Read User API token, numeric user id, and username from the environment."""
    if instance_id and instance_id != "default":
        token = os.getenv(f"VIRTFUSION_{instance_id}_USER_API_TOKEN", "")
        name = os.getenv(f"VIRTFUSION_{instance_id}_USER_NAME", "")
        raw_id = os.getenv(f"VIRTFUSION_{instance_id}_USER_ID", "")
    else:
        token = os.getenv("VIRTFUSION_USER_API_TOKEN", "")
        name = os.getenv("VIRTFUSION_USER_NAME", "")
        raw_id = os.getenv("VIRTFUSION_USER_ID", "")
    return (token or "").strip(), _parse_user_id(raw_id), (name or "").strip()


def _discover_instances() -> dict[str, VirtFusionCredentials]:
    """Discover all configured VirtFusion instances from environment variables.

    Looks for VIRTFUSION_<ID>_URL and VIRTFUSION_<ID>_TOKEN pairs.
    Falls back to single-instance VIRTFUSION_URL / VIRTFUSION_TOKEN.
    User API fields: VIRTFUSION_<ID>_USER_API_TOKEN, _USER_ID, _USER_NAME
    (or the unprefixed VIRTFUSION_USER_* names on the single-instance fallback).
    """
    instances: dict[str, VirtFusionCredentials] = {}
    pattern = re.compile(r"^VIRTFUSION_(.+)_URL$")

    for env_key, env_value in os.environ.items():
        match = pattern.match(env_key)
        if not match:
            continue
        instance_id = match.group(1)
        # Skip the bare VIRTFUSION_URL (no instance suffix)
        if instance_id == "":
            continue
        token = os.getenv(f"VIRTFUSION_{instance_id}_TOKEN")
        if token:
            url = env_value.rstrip("/")
            user_api_token, user_id, user_name = _user_fields(instance_id)
            instances[url] = VirtFusionCredentials(
                instance_id=instance_id,
                url=url,
                token=token,
                user_api_token=user_api_token,
                user_id=user_id,
                user_name=user_name,
            )

    # Fallback: single-instance env
    if not instances:
        url = os.getenv("VIRTFUSION_URL", "").rstrip("/")
        token = os.getenv("VIRTFUSION_TOKEN", "")
        if url and token:
            user_api_token, user_id, user_name = _user_fields(None)
            instances[url] = VirtFusionCredentials(
                instance_id="default",
                url=url,
                token=token,
                user_api_token=user_api_token,
                user_id=user_id,
                user_name=user_name,
            )

    return instances


def load_credentials(endpoint: str) -> VirtFusionCredentials:
    """Load VirtFusion credentials for a given endpoint URL.

    Args:
        endpoint: Control server URL (e.g. https://cp.example.com)

    Raises:
        ValueError: If no credentials are found for the endpoint.
    """
    instances = _discover_instances()
    normalized = endpoint.rstrip("/")

    if normalized in instances:
        return instances[normalized]

    # Try matching without scheme
    stripped = re.sub(r"^https?://", "", normalized, flags=re.IGNORECASE)
    for stored_url, creds in instances.items():
        stored_stripped = re.sub(r"^https?://", "", stored_url, flags=re.IGNORECASE)
        if stored_stripped == stripped:
            return creds

    # If only one instance is configured, use it regardless of URL
    if len(instances) == 1:
        return next(iter(instances.values()))

    raise ValueError(
        f"No credentials found for VirtFusion endpoint: {endpoint}. "
        "Configure VIRTFUSION_URL + VIRTFUSION_TOKEN (and VIRTFUSION_USER_API_TOKEN "
        "+ VIRTFUSION_USER_ID) or the VIRTFUSION_<ID>_* equivalents."
    )
