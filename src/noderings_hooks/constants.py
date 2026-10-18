"""Shared constants for lifecycle hook Jobs."""

from typing import Final

LABEL_RUNNER: Final[str] = "noderings.com/hook-runner"
ANN_VM_NAME: Final[str] = "noderings.com/vm-name"
ANN_VM_NAMESPACE: Final[str] = "noderings.com/vm-namespace"
ANN_VM_GROUP: Final[str] = "noderings.com/vm-group"
ANN_VM_VERSION: Final[str] = "noderings.com/vm-version"
ANN_VM_PLURAL: Final[str] = "noderings.com/vm-plural"
ANN_VM_KIND: Final[str] = "noderings.com/vm-kind"
ANN_HOOK: Final[str] = "noderings.com/hook"
ANN_HOOK_REPORTED: Final[str] = "noderings.com/hook-reported"

HOOK_RUN_AS_USER: Final[int] = 65532
HOOK_RUN_AS_GROUP: Final[int] = 65532
HOOK_FS_GROUP: Final[int] = 65532

DEFAULT_TIMEOUT_SECONDS: Final[int] = 300
MIN_TIMEOUT_SECONDS: Final[int] = 30
DEFAULT_MAX_CONCURRENT: Final[int] = 5
DEFAULT_TTL_SECONDS: Final[int] = 86400
MIN_TTL_SECONDS: Final[int] = 60
DEFAULT_JOB_IMAGE: Final[str] = "python:3.12-slim-bookworm"
DEFAULT_CPU_LIMIT: Final[str] = "100m"
DEFAULT_MEM_LIMIT: Final[str] = "128Mi"
DEFAULT_CPU_REQUEST: Final[str] = "50m"
DEFAULT_MEM_REQUEST: Final[str] = "64Mi"
DEFAULT_JOB_LIST_LIMIT: Final[int] = 50
HOOK_CONCURRENCY_RETRY_SECONDS: Final[int] = 15

# Provider-owned Secret in the hooks namespace (not stored on the platform).
# Keys are injected as env vars and mounted as files. Missing Secret is OK
# (optional: true). Empty HOOKS_PROVIDER_SECRET_NAME disables injection.
ENV_PROVIDER_SECRET_NAME: Final[str] = "HOOKS_PROVIDER_SECRET_NAME"
DEFAULT_PROVIDER_SECRET_NAME: Final[str] = "noderings-hook-secrets"
PROVIDER_SECRET_VOLUME: Final[str] = "provider-secrets"
PROVIDER_SECRET_MOUNT_PATH: Final[str] = "/var/run/secrets/noderings-hooks"
PROVIDER_SECRET_NAME_MAX_LEN: Final[int] = 253

PYTHON_BINARY: Final[str] = "python3"
SCRIPT_VOLUME: Final[str] = "script"
SCRIPT_MOUNT_PATH: Final[str] = "/hook"
SCRIPT_FILENAME: Final[str] = "script.py"
SCRIPT_COMMAND: Final[str] = f"{SCRIPT_MOUNT_PATH}/{SCRIPT_FILENAME}"
TMP_VOLUME: Final[str] = "tmp"
TMP_MOUNT_PATH: Final[str] = "/tmp"

HOOK_ALIAS: Final[dict[str, str]] = {
    "reinstall": "reinstalled",
    "suspended": "stopped",
    "unsuspend": "started",
}
