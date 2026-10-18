"""Environment-driven settings for lifecycle hook Jobs."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .constants import (
    DEFAULT_CPU_LIMIT,
    DEFAULT_CPU_REQUEST,
    DEFAULT_JOB_IMAGE,
    DEFAULT_JOB_LIST_LIMIT,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_MEM_LIMIT,
    DEFAULT_MEM_REQUEST,
    DEFAULT_PROVIDER_SECRET_NAME,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TTL_SECONDS,
    ENV_PROVIDER_SECRET_NAME,
    MIN_TIMEOUT_SECONDS,
    MIN_TTL_SECONDS,
    PROVIDER_SECRET_NAME_MAX_LEN,
)

# Kubernetes Secret object names are DNS subdomains (RFC 1123).
_DNS1123_SUBDOMAIN = re.compile(
    r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _provider_secret_name() -> str:
    """Kubernetes Secret object name in the hooks ns, or empty to disable.

    Unset env → default name. Explicit empty / invalid DNS-1123 → disabled.
    """
    if ENV_PROVIDER_SECRET_NAME not in os.environ:
        return DEFAULT_PROVIDER_SECRET_NAME
    name = os.environ[ENV_PROVIDER_SECRET_NAME].strip()
    if not name:
        return ""
    if len(name) > PROVIDER_SECRET_NAME_MAX_LEN or not _DNS1123_SUBDOMAIN.fullmatch(name):
        return ""
    return name


@dataclass(frozen=True)
class HookSettings:
    enabled: bool
    job_image: str
    job_namespace: str
    timeout_seconds: int
    max_concurrent: int
    cpu_limit: str
    mem_limit: str
    cpu_request: str
    mem_request: str
    ttl_seconds: int
    job_list_limit: int
    # Provider-owned Secret object name in the hooks ns; empty disables injection.
    provider_secret_name: str

    def namespace_for(self, vm_namespace: str) -> str:
        """Dedicated hook-job namespace, or the VM namespace when unset."""
        return self.job_namespace or vm_namespace


def load_hook_settings() -> HookSettings:
    return HookSettings(
        enabled=_bool("HOOKS_ENABLED", True),
        job_image=os.environ.get("HOOKS_JOB_IMAGE", "").strip()
        or os.environ.get("HOOKS_OPERATOR_IMAGE", DEFAULT_JOB_IMAGE),
        job_namespace=os.environ.get("HOOKS_JOB_NAMESPACE", "").strip(),
        timeout_seconds=max(
            MIN_TIMEOUT_SECONDS, _int("HOOKS_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
        ),
        max_concurrent=max(1, _int("HOOKS_MAX_CONCURRENT", DEFAULT_MAX_CONCURRENT)),
        cpu_limit=os.environ.get("HOOKS_CPU_LIMIT", DEFAULT_CPU_LIMIT),
        mem_limit=os.environ.get("HOOKS_MEM_LIMIT", DEFAULT_MEM_LIMIT),
        cpu_request=os.environ.get("HOOKS_CPU_REQUEST", DEFAULT_CPU_REQUEST),
        mem_request=os.environ.get("HOOKS_MEM_REQUEST", DEFAULT_MEM_REQUEST),
        ttl_seconds=max(MIN_TTL_SECONDS, _int("HOOKS_TTL_SECONDS", DEFAULT_TTL_SECONDS)),
        job_list_limit=max(1, _int("HOOKS_JOB_LIST_LIMIT", DEFAULT_JOB_LIST_LIMIT)),
        provider_secret_name=_provider_secret_name(),
    )
