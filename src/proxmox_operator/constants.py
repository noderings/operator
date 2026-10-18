"""Constants for the Proxmox operator."""

from typing import Final, Literal

# Operation types
OP_CREATED: Final[str] = "created"
OP_STARTED: Final[str] = "started"
OP_STOPPED: Final[str] = "stopped"
OP_DELETED: Final[str] = "deleted"
OP_RESTARTED: Final[str] = "restarted"
OP_REINSTALLED: Final[str] = "reinstalled"

# Status phases (ProxmoxVM / shared UI)
PHASE_PENDING: Final[str] = "Pending"
PHASE_PROVISIONING: Final[str] = "Provisioning"
PHASE_READY: Final[str] = "Ready"
PHASE_ERROR: Final[str] = "Error"
PHASE_DELETING: Final[str] = "Deleting"


class BackupPhase:
    """VMBackup CR status.phase (vm.proxmox.com/VMBackup)."""

    PENDING: Final[Literal["Pending"]] = "Pending"
    RUNNING: Final[Literal["Running"]] = "Running"
    SUCCEEDED: Final[Literal["Succeeded"]] = "Succeeded"
    FAILED: Final[Literal["Failed"]] = "Failed"
    DELETED: Final[Literal["Deleted"]] = "Deleted"


class SnapshotPhase:
    """VMSnapshot CR status.phase (vm.proxmox.com/VMSnapshot)."""

    PENDING: Final[Literal["Pending"]] = "Pending"
    ACTIVE: Final[Literal["Active"]] = "Active"
    ERROR: Final[Literal["Error"]] = "Error"


# Restore status.state (VMSnapshot / VMBackup status.restoreStatus.state)
RESTORE_STATE_COMPLETED: Final[str] = "completed"
RESTORE_STATE_FAILED: Final[str] = "failed"
RESTORE_STATE_RESTORING: Final[str] = "restoring"


# VM status values from Proxmox
VM_STATUS_RUNNING: Final[str] = "running"
VM_STATUS_STOPPED: Final[str] = "stopped"
VM_STATUS_UNKNOWN: Final[str] = "unknown"

# Task exit statuses
EXIT_STATUS_OK: Final[str] = "OK"
EXIT_STATUS_STOPPED: Final[str] = "stopped"

# Memory unit mappings (conversion factors to MB)
MEMORY_UNITS: Final[dict[str, float]] = {
    "K": 1 / 1024,
    "KB": 1 / 1024,
    "KI": 1 / 1024,
    "KIB": 1 / 1024,
    "M": 1,
    "MB": 1,
    "MI": 1,
    "MIB": 1,
    "G": 1024,
    "GB": 1024,
    "GI": 1024,
    "GIB": 1024,
    "T": 1024 * 1024,
    "TB": 1024 * 1024,
    "TI": 1024 * 1024,
    "TIB": 1024 * 1024,
}


def cloudinit_iso_filename(vm_name: str) -> str:
    """Per-VM cloudInit ISO basename on Proxmox ISO storage."""
    return f"cloudinit-{vm_name.lower()}.iso"


# Default values
DEFAULT_PROXMOX_PORT: Final[int] = 8006
DEFAULT_TASK_TIMEOUT: Final[int] = 300
DEFAULT_STOP_TIMEOUT: Final[int] = 60
MAX_VMID_SEARCH_ITERATIONS: Final[int] = 10000

# VNCRequest TTL (seconds): informational ticket-fetch window; backend owns create/delete.
# Keep in sync with charts/proxmox-operator-crds/templates/crds/vncrequest.yaml.
VNC_REQUEST_TTL_DEFAULT: Final[int] = 30
VNC_REQUEST_TTL_MIN: Final[int] = 10
VNC_REQUEST_TTL_MAX: Final[int] = 300

# Max length for ProxmoxNode status.nodeDetails.cpu.lastError
NODE_CPU_LAST_ERROR_MAX_LEN: Final[int] = 256

# VM stop verification delay (seconds to wait after stop before checking status)
VM_STOP_VERIFICATION_DELAY: Final[int] = 2

# Retry configuration for VM stop operations
VM_STOP_MAX_RETRIES: Final[int] = 5
VM_STOP_INITIAL_BACKOFF: Final[int] = 3
VM_STOP_MAX_BACKOFF: Final[int] = 30
VM_STOP_SKIPLOCK_THRESHOLD: Final[int] = 3  # Use skiplock after this many attempts

# Retry configuration for VM creation (VMID conflicts)
VM_CREATE_MAX_RETRIES: Final[int] = 5

# Kopf TemporaryError delay for transient Proxmox transport/API failures (seconds)
PROXMOX_TRANSIENT_RETRY_DELAY_SECONDS: Final[float] = 30.0
# Substrings matched (case-insensitive) against ProxmoxError messages to classify retries
PROXMOX_TRANSIENT_ERROR_MARKERS: Final[tuple[str, ...]] = (
    "failed to find vm by name",
    "failed to verify vm",
    "failed to connect",
    "timeout",
    "timed out",
    "ssl",
    "connection",
    "max retries",
    "connection aborted",
    "remote end closed",
)
# Proxmox responses indicating a guest is absent (get_vm_name may return None)
PROXMOX_VM_ABSENT_ERROR_MARKERS: Final[tuple[str, ...]] = (
    "not found",
    "does not exist",
    "doesn't exist",
    "no such",
)

# Task status check interval (seconds between status checks)
TASK_STATUS_CHECK_INTERVAL: Final[int] = 1

# Backup reconciliation configuration
# After vzdump POST succeeds: brief sleep (seconds) so the task is registered before the next reconcile polls it.
# Override via BACKUP_VZDUMP_POST_DELAY_SECONDS env or Helm values.backup.vzdumpPostDelaySeconds.
BACKUP_VZDUMP_POST_DELAY_SECONDS: Final[int] = 5
# Note: Uses VM_STOP_VERIFICATION_DELAY for interval between VM status checks while waiting for stop
# Note: Uses VM_STOP_MAX_RETRIES for maximum number of attempts to stop VM before failing restore

# Firewall reconciliation configuration
FIREWALL_RECONCILE_INTERVAL: Final[int] = (
    60  # Default reconciliation timer interval in seconds (can be overridden via FIREWALL_RECONCILE_INTERVAL_SECONDS env var)
)
FIREWALL_RECONCILE_INTERVAL_MIN: Final[int] = (
    10  # Minimum allowed reconciliation interval in seconds
)

# Proxmox ipfilter-* IPSets: single-host CIDR prefix lengths
IPFILTER_IPV4_HOST_PREFIX_LEN: Final[int] = 32
IPFILTER_IPV6_HOST_PREFIX_LEN: Final[int] = 128

# Snapshot reconciliation configuration
SNAPSHOT_RECONCILE_INTERVAL: Final[int] = 60  # Reconciliation timer interval in seconds
