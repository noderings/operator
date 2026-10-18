"""Constants for the SolusVM 2 operator."""

from typing import Final

GROUP: Final[str] = "vm.solusvm.com"
VERSION: Final[str] = "v1alpha1"

# Operation types (README §26)
OP_CREATED: Final[str] = "created"
OP_STARTED: Final[str] = "started"
OP_STOPPED: Final[str] = "stopped"
OP_RESTARTED: Final[str] = "restarted"
OP_REINSTALL: Final[str] = "reinstall"
# Control-plane CR operation (same string as ProxmoxVM). Treat as OP_REINSTALL.
OP_REINSTALLED: Final[str] = "reinstalled"
OP_PASSWORD: Final[str] = "password"
OP_SUSPENDED: Final[str] = "suspended"
OP_UNSUSPEND: Final[str] = "unsuspend"
OP_DELETED: Final[str] = "deleted"
OP_EXTRA_IPV4: Final[str] = "extra_ipv4"
OP_REMOVE_IPV4: Final[str] = "remove_ipv4"
OP_PLAN_CHANGE: Final[str] = "plan_change"

# Status phases
PHASE_PENDING: Final[str] = "Pending"
PHASE_READY: Final[str] = "Ready"
PHASE_ERROR: Final[str] = "Error"

# HTTP client (README §9.2)
CONNECT_TIMEOUT: Final[int] = 5
READ_TIMEOUT: Final[int] = 35
MAX_RETRIES: Final[int] = 5
RETRY_STATUS_CODES: Final[tuple[int, ...]] = (429, 502, 503)
SUCCESS_CODES: Final[tuple[int, ...]] = (200, 201, 204)

# Task polling (GET /tasks/{id})
TASK_POLL_INTERVAL: Final[int] = 5
TASK_POLL_TIMEOUT: Final[int] = 1800  # 30 minutes
# Extra IPv4: SolusVM often leaves POST /ips tasks pending after the address
# is already on GET /servers/{id}. Do not wait the full task timeout.
EXTRA_IPV4_POLL_INTERVAL: Final[float] = 2.0
EXTRA_IPV4_POLL_TIMEOUT: Final[int] = 90
TASK_FINISHED_STATUSES: Final[frozenset[str]] = frozenset(
    {"done", "done_with_errors", "failed", "canceled"}
)
TASK_FAILED_STATUSES: Final[frozenset[str]] = frozenset({"done_with_errors", "failed", "canceled"})
TASK_IN_FLIGHT_STATUSES: Final[frozenset[str]] = frozenset({"pending", "queued", "running"})

# Kopf TemporaryError delay for transient SolusVM API failures (seconds)
TRANSIENT_RETRY_DELAY_SECONDS: Final[float] = 30.0

# VNC TTL
VNC_REQUEST_TTL_DEFAULT: Final[int] = 30

# Guest password at create: 5 digits + 5 A–Z + 5 a–z, shuffled (WHMCS)
PASSWORD_CLASS_SIZE: Final[int] = 5
