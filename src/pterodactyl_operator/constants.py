"""CRD identifiers and console-bridge constants for gs.pterodactyl.com."""

from typing import Final

GROUP = "gs.pterodactyl.com"
VERSION = "v1alpha1"
SERVER_PLURAL = "pterodactylservers"
SERVER_KIND = "PterodactylServer"
CATALOG_PLURAL = "pterodactylgamecatalogs"
CATALOG_KIND = "PterodactylGameCatalog"

PHASE_PENDING = "Pending"
PHASE_READY = "Ready"
PHASE_ERROR = "Error"

OP_CREATED = "created"
OP_STARTED = "started"
OP_STOPPED = "stopped"
OP_RESTARTED = "restarted"
OP_KILL = "kill"

ACCEPT = "Application/vnd.pterodactyl.v1+json"

# Console / file bridge grants (HMAC). Anti-replay is process-local, so the
# console Deployment must stay at a single replica.
CONSOLE_SESSION_TTL_SECONDS: Final[int] = 90
CONSOLE_TRANSFER_TTL_SECONDS: Final[int] = 180
CONSOLE_GATEWAY_REQUIRED_REPLICAS: Final[int] = 1
CONSOLE_BRIDGE_ADDR_ENV: Final[str] = "PTERODACTYL_CONSOLE_BRIDGE_ADDR"
CONSOLE_GATEWAY_REPLICAS_ENV: Final[str] = "PTERODACTYL_CONSOLE_GATEWAY_REPLICAS"
CONSOLE_BRIDGE_ADDR_DEFAULT: Final[str] = "127.0.0.1:9230"
CONSOLE_BRIDGE_ADDR_LISTEN_ALL: Final[str] = "0.0.0.0:9230"
CONSOLE_CLIENT_KEY_ENV: Final[str] = "PTERODACTYL_CLIENT_KEY"
CONSOLE_TRANSFER_OPS: Final[frozenset[str]] = frozenset(
    {"write", "upload", "read", "download", "stream"}
)
