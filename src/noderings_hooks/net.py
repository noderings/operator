"""Address helpers shared by hook Jobs and hypervisor status parsers."""

from __future__ import annotations

import ipaddress
from typing import Any


def normalize_prefix(value: Any, *, version: int = 4) -> str:
    """Return a CIDR prefix length string (e.g. '24'), or empty if unknown."""
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value) if 0 <= value <= (128 if version == 6 else 32) else ""
    text = str(value).strip().lstrip("/")
    if not text:
        return ""
    if "/" in text:
        text = text.rsplit("/", 1)[-1].strip()
    if text.isdigit():
        n = int(text)
        return str(n) if 0 <= n <= (128 if version == 6 else 32) else ""
    try:
        return str(ipaddress.IPv4Network(f"0.0.0.0/{text}").prefixlen)
    except ValueError:
        return ""


def netmask_from_prefix(prefix: str) -> str:
    """Dotted IPv4 netmask for a prefix length ('24' → '255.255.255.0')."""
    text = normalize_prefix(prefix, version=4)
    if not text:
        return ""
    try:
        return str(ipaddress.IPv4Network(f"0.0.0.0/{text}").netmask)
    except ValueError:
        return ""
