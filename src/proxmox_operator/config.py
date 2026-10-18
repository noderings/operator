"""Configuration management for Proxmox connections."""

import os
import re
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import urlparse

from .constants import DEFAULT_PROXMOX_PORT


@dataclass
class ProxmoxSettings:
    """Configuration for Proxmox API connection using token-based authentication."""

    host: str
    username: str
    token_name: str
    token_value: str
    verify_ssl: bool = False
    timeout: int = 30


@dataclass
class ProxmoxInstanceCredentials:
    """Credentials for a specific Proxmox instance."""

    instance_id: str
    url: str
    username: str
    token_id: str
    token_secret: str


def _parse_proxmox_url(proxmox_url: str) -> str:
    """Parse Proxmox URL to extract host:port.

    Args:
        proxmox_url: Full URL (e.g., "https://192.0.2.10:8006") or host:port (e.g., "192.0.2.10:8006")

    Returns:
        host:port string (e.g., "192.0.2.10:8006")
    """
    # If it's already just host:port without protocol, return as-is
    if not proxmox_url.startswith(("http://", "https://")):
        return proxmox_url

    # Parse the URL
    parsed = urlparse(proxmox_url)

    # Extract host and port
    host = parsed.hostname
    port = parsed.port

    if not host:
        raise ValueError(f"Invalid Proxmox URL: {proxmox_url}")

    # Return host:port format
    if port:
        return f"{host}:{port}"
    else:
        return f"{host}:{DEFAULT_PROXMOX_PORT}"


def _discover_proxmox_instances() -> Dict[str, ProxmoxInstanceCredentials]:
    """Discover all configured Proxmox instances from environment variables.

    Looks for environment variables in the format:
    - PROXMOX_<INSTANCE_ID>_URL
    - PROXMOX_<INSTANCE_ID>_USERNAME
    - PROXMOX_<INSTANCE_ID>_TOKEN_ID
    - PROXMOX_<INSTANCE_ID>_TOKEN_SECRET

    Returns:
        Dictionary mapping normalized URLs to credentials
    """
    instances: Dict[str, ProxmoxInstanceCredentials] = {}
    pattern = re.compile(r"^PROXMOX_(.+)_URL$")

    # Find all Proxmox instance URL environment variables
    for env_key, env_value in os.environ.items():
        match = pattern.match(env_key)
        if match:
            instance_id = match.group(1)
            url = env_value

            # Get credentials for this instance
            username = os.getenv(f"PROXMOX_{instance_id}_USERNAME")
            token_id = os.getenv(f"PROXMOX_{instance_id}_TOKEN_ID")
            token_secret = os.getenv(f"PROXMOX_{instance_id}_TOKEN_SECRET")

            # Only add if all required credentials are present
            if username and token_id and token_secret:
                # Normalize the URL for matching
                normalized_url = _normalize_url(url)
                instances[normalized_url] = ProxmoxInstanceCredentials(
                    instance_id=instance_id,
                    url=url,
                    username=username,
                    token_id=token_id,
                    token_secret=token_secret,
                )

    # Host `make run` uses PROXMOX_URL (no instance id). Helm injects PROXMOX_<ID>_URL.
    default_url = os.getenv("PROXMOX_URL")
    default_username = os.getenv("PROXMOX_USERNAME")
    default_token_id = os.getenv("PROXMOX_TOKEN_ID")
    default_token_secret = os.getenv("PROXMOX_TOKEN_SECRET")
    if default_url and default_username and default_token_id and default_token_secret:
        normalized_url = _normalize_url(default_url)
        instances.setdefault(
            normalized_url,
            ProxmoxInstanceCredentials(
                instance_id="default",
                url=default_url,
                username=default_username,
                token_id=default_token_id,
                token_secret=default_token_secret,
            ),
        )

    return instances


def _normalize_url(url: str) -> str:
    """Normalize a Proxmox URL for comparison.

    Removes protocol, converts to lowercase, and ensures consistent format.
    Examples:
        "https://192.0.2.10:8006" -> "192.0.2.10:8006"
        "HTTPS://192.0.2.10:8006/" -> "192.0.2.10:8006"
        "192.0.2.10" -> "192.0.2.10:8006"
    """
    # Remove protocol if present
    url = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    # Remove trailing slash
    url = url.rstrip("/")
    # Convert to lowercase
    url = url.lower()
    # Add default port if not present
    if ":" not in url:
        url = f"{url}:{DEFAULT_PROXMOX_PORT}"
    return url


def _find_credentials_for_url(proxmox_url: str) -> Optional[ProxmoxInstanceCredentials]:
    """Find credentials for a specific Proxmox URL.

    Args:
        proxmox_url: The Proxmox URL from the CR spec

    Returns:
        ProxmoxInstanceCredentials if found, None otherwise
    """
    instances = _discover_proxmox_instances()
    normalized_url = _normalize_url(proxmox_url)

    # Direct match
    if normalized_url in instances:
        return instances[normalized_url]

    # Try matching without port (in case port is omitted)
    url_without_port = normalized_url.split(":")[0]
    for stored_url, credentials in instances.items():
        if stored_url.startswith(url_without_port):
            return credentials

    return None


def load_settings_from_env(proxmox_url: str) -> ProxmoxSettings:
    """Load Proxmox settings from environment variables and CR spec.

    Args:
        proxmox_url: Proxmox URL from CR spec (e.g., "https://192.0.2.10:8006" or "192.0.2.10:8006")

    Discovers all configured Proxmox instances and matches credentials to the URL.

    Raises:
        ValueError: If no credentials are found for the specified URL
    """
    # Parse the URL to extract host:port
    host = _parse_proxmox_url(proxmox_url)

    # Find instance-specific credentials
    credentials = _find_credentials_for_url(proxmox_url)

    if not credentials:
        raise ValueError(
            f"No credentials found for Proxmox URL: {proxmox_url}. "
            "Set PROXMOX_URL / PROXMOX_USERNAME / PROXMOX_TOKEN_ID / PROXMOX_TOKEN_SECRET "
            "for make run, or PROXMOX_<INSTANCE_ID>_URL (and matching USERNAME/TOKEN_*) "
            "as Helm does for proxmox.instances[]"
        )

    # Use instance-specific credentials
    verify_ssl = os.getenv("PROXMOX_VERIFY_SSL", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    timeout = int(os.getenv("PROXMOX_TIMEOUT_SECONDS", "30"))

    return ProxmoxSettings(
        host=host,
        username=credentials.username,
        token_name=credentials.token_id,
        token_value=credentials.token_secret,
        verify_ssl=verify_ssl,
        timeout=timeout,
    )
