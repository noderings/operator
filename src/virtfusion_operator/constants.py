"""Constants for the VirtFusion operator."""

from typing import Final

GROUP: Final[str] = "vm.virtfusion.com"
VERSION: Final[str] = "v1alpha1"

# Operation types
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
OP_PACKAGE_CHANGE: Final[str] = "package_change"

# Status phases
PHASE_PENDING: Final[str] = "Pending"
PHASE_READY: Final[str] = "Ready"
PHASE_ERROR: Final[str] = "Error"

# HTTP client
CONNECT_TIMEOUT: Final[int] = 5
READ_TIMEOUT: Final[int] = 30
MAX_RETRIES: Final[int] = 5
RETRY_STATUS_CODES: Final[tuple[int, ...]] = (429, 502, 503, 423)
SUCCESS_CODES: Final[tuple[int, ...]] = (200, 201, 204)
PAGINATION_PAGE_SIZE: Final[int] = 200

# Queue polling
QUEUE_POLL_INTERVAL: Final[int] = 5
QUEUE_POLL_TIMEOUT: Final[int] = 1800  # 30 minutes

# Kopf TemporaryError delay for transient VF API failures (seconds)
TRANSIENT_RETRY_DELAY_SECONDS: Final[float] = 30.0

# VNC TTL
VNC_REQUEST_TTL_DEFAULT: Final[int] = 30
