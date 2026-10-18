"""Pydantic models and CRD constants for Proxmox operator."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator
from noderings_hooks.types import LastHookStatus, VMHooks

from .constants import (
    BackupPhase,
    SnapshotPhase,
    VNC_REQUEST_TTL_DEFAULT,
    VNC_REQUEST_TTL_MAX,
    VNC_REQUEST_TTL_MIN,
)

BackupPhaseLiteral = Literal["Pending", "Running", "Succeeded", "Failed", "Deleted"]
SnapshotPhaseLiteral = Literal["Pending", "Active", "Error", "Disabled"]

# CRD identity (ProxmoxVM)
GROUP: str = "vm.proxmox.com"
VERSION: str = "v1alpha1"
PLURAL: str = "proxmoxvms"
KIND: str = "ProxmoxVM"

# Firewall CRD identity
FIREWALL_PLURAL: str = "firewallrulesets"
FIREWALL_KIND: str = "FirewallRuleSet"


class CPUConfig(BaseModel):
    cores: int = Field(..., ge=1)
    sockets: int = Field(1, ge=1)
    type: str | None = Field(
        None, description="Proxmox guest CPU model (cpu= parameter), e.g. host. From node settings."
    )
    limit: int | None = Field(
        None, ge=0, le=128, description="Proxmox cpulimit=; 0/absent = unlimited. From plan."
    )
    units: int | None = Field(
        None, ge=1, le=500000, description="Proxmox cpuunits= fair-share weight. From plan."
    )
    affinity: str | None = Field(
        None,
        pattern=r"^\d+(-\d+)?(,\d+(-\d+)?)*$",
        description="Proxmox affinity= cpuset of host logical CPU ids, e.g. 0-3,64-67. "
        "Set by backend for dedicated-core VMs.",
    )
    numa: bool = Field(
        True,
        description="Proxmox numa= flag; always true (enabled for shared and dedicated VMs).",
    )

    @model_validator(mode="after")
    def numa_always_enabled(self) -> CPUConfig:
        """NUMA is always on in Proxmox for every VM."""
        if not self.numa:
            self.numa = True
        return self


class DiskConfig(BaseModel):
    name: str
    storage: str
    size: str  # e.g. "32G"


class NetworkConfig(BaseModel):
    name: str
    bridge: str
    model: str = "virtio"
    macAddress: str | None = Field(
        None,
        pattern=r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$",
        description="Allocated MAC address; absent = Proxmox auto-assigns.",
    )
    vlanId: int | None = Field(
        None, ge=0, le=4094, description="802.1Q VLAN tag (tag=); absent or 0 = untagged."
    )
    rateLimitMBps: float | None = Field(
        None, ge=0, description="Bandwidth cap in MB/s (rate=); absent or 0 = unlimited."
    )


class SecretRef(BaseModel):
    name: str


class ConfigMapRef(BaseModel):
    name: str


class AdditionalIPv4Address(BaseModel):
    """One extra IPv4 from the ADDITIONAL-IP addon (same prefix as primary)."""

    address: str
    prefix: str | None = None
    gateway: str | None = None


class StaticIPConfig(BaseModel):
    """Static IP network configuration for CloudInit (used when platform.self_managed_dhcp is false)."""

    ipv4Address: str | None = None
    ipv4Prefix: str | None = None
    ipv6Address: str | None = None
    ipv6Prefix: str | None = None
    ipv4Gateway: str | None = None
    ipv6Gateway: str | None = None
    dnsServers: list[str] | None = None
    additionalIpv4Addresses: list[AdditionalIPv4Address] | None = None


class CloudInitConfig(BaseModel):
    hostname: str | None = None
    userDataSecretRef: SecretRef | None = None
    vendorDataConfigMapRef: ConfigMapRef | None = None
    networkDataConfigMapRef: ConfigMapRef | None = None
    authSecretRef: SecretRef | None = Field(
        None,
        description=(
            "Reference to Kubernetes Secret containing auth data. Secret should contain "
            "exactly one of: 'userdata' (string), 'sshkeys' (newline-separated string), or "
            "'hashed_passwd' (SHA-512 crypt $6$... string for cloud-init / vm.hashed_passwd). "
            "Legacy key 'password' is accepted as an alias for hashed_passwd."
        ),
    )
    storage: str | None = None
    staticIpConfig: StaticIPConfig | None = None


class VMConfig(BaseModel):
    templateId: str
    name: str
    nodeName: str
    description: str | None = None
    vmId: str | None = None  # Proxmox VMID for non-create operations
    cpu: CPUConfig
    memory: str  # e.g. "4096" (MB), "2G", "2048M", "2Gi", "2048Mi"
    disks: list[DiskConfig]
    networks: list[NetworkConfig]
    cloudInit: CloudInitConfig | None = None


OperationType = Literal["created", "started", "stopped", "deleted", "restarted", "reinstalled"]


class Operation(BaseModel):
    type: OperationType = "created"
    force: bool = False  # For deletion operations, whether to force stop running VMs


class FirewallRuleSetRef(BaseModel):
    name: str


class ProxmoxNodeRef(BaseModel):
    """Reference to a ProxmoxNode CR in the same namespace.

    ProxmoxNode is the single source of truth for the Proxmox platform
    endpoint (`spec.proxmoxUrl`) and node-level policy (`maintenanceMode`).
    ProxmoxVM CRs reference their owning ProxmoxNode here; the operator
    resolves the URL by looking up the ProxmoxNode CR at reconcile time.
    """

    name: str = Field(
        ...,
        min_length=1,
        description="Name of the ProxmoxNode CR in the same namespace as this ProxmoxVM.",
    )


class ProxmoxVMSpec(BaseModel):
    proxmoxNodeRef: ProxmoxNodeRef = Field(
        ...,
        description="Reference to the owning ProxmoxNode CR. The operator resolves "
        "the Proxmox endpoint from ProxmoxNode.spec.proxmoxUrl.",
    )
    vmidSeed: int | None = Field(
        None, ge=100, description="Starting VMID seed number for VM allocation (e.g., 9000)"
    )
    vmConfig: VMConfig
    operation: Operation | None = None
    firewallRuleSet: FirewallRuleSetRef | None = Field(
        None,
        description="Optional reference to a FirewallRuleSet applied to this VM (rules contain their own direction).",
    )
    blockOutboundSmtp: bool = Field(
        False,
        description="Provider SMTP outbound block; applied by operator, not FirewallRuleSet.",
    )
    hooks: VMHooks | None = Field(
        None,
        description="Provider lifecycle hook scripts keyed by operation type.",
    )


class ProxmoxVMStatus(BaseModel):
    phase: Literal["Pending", "Provisioning", "Ready", "Error", "Deleting"] | None = None
    message: str | None = None
    observedGeneration: int | None = None
    vmId: str | None = Field(
        None,
        description="Allocated Proxmox VMID written by the operator after create/reinstall.",
    )
    lastHook: LastHookStatus | None = None


# ProxmoxNode types
class ProxmoxNodeDetailsCPU(BaseModel):
    """status.nodeDetails.cpu — operator collects from Proxmox (CRD OpenAPI)."""

    phase: Literal["Pending", "Ready", "Error"] | None = None
    brand: str | None = None
    model: str | None = None
    cores: int | None = Field(None, ge=0)
    sockets: int | None = Field(None, ge=0)
    mhz: float | None = None
    collectedAt: str | None = None
    lastError: str | None = None


class ProxmoxNodeDetails(BaseModel):
    cpu: ProxmoxNodeDetailsCPU | None = None


class ProxmoxNodeProvisioningDefaults(BaseModel):
    """spec.provisioningDefaults — ops-visibility mirror of node provisioning settings.

    The operator provisions from ProxmoxVM fields; this block is informational
    (GitOps review, debugging). MAC prefix is never mirrored to any CR.
    """

    networkBridge: str | None = None
    networkName: str | None = None
    networkModel: str | None = None
    networkVlanId: int | None = Field(None, ge=0, le=4094)
    proxmoxCpuType: str | None = None


class ProxmoxNodeSpec(BaseModel):
    nodeId: str = Field(..., description="Backend API Node ID (UUID)")
    name: str = Field(..., description="Proxmox node name")
    maintenanceMode: bool = Field(
        False, description="When enabled, reconciliation loop skips Proxmox state checks"
    )
    proxmoxUrl: str = Field(
        ...,
        min_length=1,
        description="Proxmox API base URL (platform endpoint). Single source of truth: every "
        "ProxmoxVM that references this node resolves its endpoint from here.",
    )
    provisioningDefaults: ProxmoxNodeProvisioningDefaults | None = None


class ProxmoxNodeStatus(BaseModel):
    phase: Literal["Pending", "Ready", "Error"] | None = None
    message: str | None = None
    observedGeneration: int | None = None
    nodeDetails: ProxmoxNodeDetails | None = None


# Firewall types
class FirewallTarget(BaseModel):
    name: str
    namespace: str | None = None


class FirewallRule(BaseModel):
    id: str  # Rule UUID - stable identifier that doesn't change with position (required)
    action: Literal["accept", "drop", "reject"]
    protocol: str  # tcp, udp, …, or "any" (omit Proxmox proto=)
    direction: Literal["ingress", "egress"]  # Direction is on each rule
    sourcePrefixes: list[str] | None = None
    destinationPrefixes: list[str] | None = None
    sourcePorts: str | None = Field(
        None, description="Source port or range, e.g., '80' or '8000-9000'"
    )
    destinationPorts: str | None = Field(
        None, description="Destination port or range, e.g., '443' or '5000-5010'"
    )
    position: int | None = Field(
        None, ge=0, description="Rule position (0, 1, 2, 3... sequential order)"
    )
    description: str | None = None


class FirewallRuleSetSpec(BaseModel):
    targets: list[FirewallTarget] | None = Field(
        None,
        description="List of target ProxmoxVM CR names (optional, can be computed from ProxmoxVM CRs)",
    )
    rules: list[FirewallRule]  # Rules contain their own direction


class FirewallRuleSetStatus(BaseModel):
    phase: Literal["Pending", "Ready", "Error"] | None = None
    message: str | None = None
    observedGeneration: int | None = None


# VMBackup types (one CR per backup run)
BACKUP_PLURAL: str = "vmbackups"
BACKUP_KIND: str = "VMBackup"


class VMBackupVMRef(BaseModel):
    name: str = Field(
        ..., min_length=1, description="ProxmoxVM CR name (vmid is read from that CR)"
    )
    namespace: Optional[str] = None


class VMBackupStorage(BaseModel):
    storageId: str = Field(..., min_length=1)
    compression: Literal["none", "lzo", "gzip", "zstd"]
    mode: Literal["snapshot", "suspend", "stop"]


class VMBackupRestoreRequest(BaseModel):
    backupName: Optional[str] = None
    backupVolid: Optional[str] = None
    stopVm: bool = True
    startAfterRestore: bool = False


class VMBackupRestoreStatus(BaseModel):
    state: Literal["none", "requested", "restoring", "completed", "failed"] = "none"
    requestedBackupVolid: Optional[str] = None
    requestedAt: Optional[str] = None
    startedAt: Optional[str] = None
    completedAt: Optional[str] = None
    taskUpid: Optional[str] = None
    error: Optional[str] = None


class VMBackupLastRestore(BaseModel):
    backupName: Optional[str] = None
    restoredAt: Optional[str] = None
    restoredBy: Optional[str] = None


class VMBackupSpec(BaseModel):
    vmRef: VMBackupVMRef
    addonId: str
    backupName: str
    scheduledAt: str  # ISO8601
    storage: VMBackupStorage
    expiresAt: Optional[str] = (
        None  # ISO8601; when set and now >= expiresAt, operator deletes backup from Proxmox
    )
    restoreRequest: Optional[VMBackupRestoreRequest] = None


class VMBackupStatus(BaseModel):
    phase: Optional[BackupPhaseLiteral] = BackupPhase.PENDING
    message: Optional[str] = None
    taskUpid: Optional[str] = None
    volid: Optional[str] = None
    sizeBytes: Optional[int] = None
    startedAt: Optional[str] = None
    completedAt: Optional[str] = None
    error: Optional[str] = None
    restoreStatus: Optional[VMBackupRestoreStatus] = Field(
        default_factory=lambda: VMBackupRestoreStatus(),
    )
    lastRestore: Optional[VMBackupLastRestore] = None


# VMSnapshot types
VMSNAPSHOT_PLURAL: str = "vmsnapshots"
VMSNAPSHOT_KIND: str = "VMSnapshot"


class VMSnapshotVMRef(BaseModel):
    name: str = Field(
        ..., min_length=1, description="ProxmoxVM CR name (Proxmox vmid is read from that CR)"
    )


class VMSnapshotCreateRequest(BaseModel):
    snapshotName: str  # Snapshot name defined by backend (format: snapshot_{YYYYMMDD}_{HHMM})
    description: str


class VMSnapshotRestoreRequest(BaseModel):
    snapshotName: str
    stopVm: bool = True


class VMSnapshotDeleteRequest(BaseModel):
    snapshotName: str


class SnapshotDetail(BaseModel):
    """Details of a snapshot in Proxmox."""

    name: str  # Snapshot name in Proxmox
    createdAt: Optional[str] = None  # ISO 8601 timestamp
    status: Optional[Literal["active", "deleted", "expired"]] = "active"


class VMSnapshotRestoreStatus(BaseModel):
    """Status of restore operation."""

    state: Literal["none", "requested", "restoring", "completed", "failed"] = "none"
    requestedSnapshotName: Optional[str] = None
    requestedAt: Optional[str] = None  # ISO 8601 timestamp
    startedAt: Optional[str] = None  # ISO 8601 timestamp
    completedAt: Optional[str] = None  # ISO 8601 timestamp
    taskUpid: Optional[str] = None  # Proxmox task UPID for restore operation
    error: Optional[str] = None


class LastRestore(BaseModel):
    """Information about the last restore operation."""

    snapshotName: str
    restoredAt: str  # ISO 8601 timestamp
    restoredBy: Optional[str] = None  # User ID who initiated the restore


class LastDelete(BaseModel):
    """Information about the last delete operation."""

    snapshotName: str
    deletedAt: str  # ISO 8601 timestamp


class VMSnapshotSpec(BaseModel):
    vmRef: VMSnapshotVMRef
    description: Optional[str] = None
    createRequest: Optional[VMSnapshotCreateRequest] = None
    restoreRequest: Optional[VMSnapshotRestoreRequest] = None
    deleteRequest: Optional[VMSnapshotDeleteRequest] = None


class VMSnapshotStatus(BaseModel):
    phase: Optional[SnapshotPhaseLiteral] = SnapshotPhase.PENDING
    snapshotCount: Optional[int] = 0
    taskUpid: Optional[str] = None  # Proxmox task UPID for snapshot creation
    snapshots: Optional[list[SnapshotDetail]] = Field(default_factory=list)
    restoreStatus: Optional[VMSnapshotRestoreStatus] = Field(
        default_factory=lambda: VMSnapshotRestoreStatus(),
        description="Current restore operation status",
    )
    lastRestore: Optional[LastRestore] = None
    lastDelete: Optional[LastDelete] = None


# VNCRequest CRD identity
VNC_REQUEST_GROUP: str = "vm.proxmox.com"
VNC_REQUEST_VERSION: str = "v1alpha1"
VNC_REQUEST_PLURAL: str = "vncrequests"
VNC_REQUEST_KIND: str = "VNCRequest"


class ProxmoxVMRef(BaseModel):
    """Reference to a ProxmoxVM CR."""

    name: str
    namespace: str | None = None


class VNCRequestSpec(BaseModel):
    """Specification for a VNCRequest CR."""

    proxmoxVMRef: ProxmoxVMRef
    ttl: int = Field(
        VNC_REQUEST_TTL_DEFAULT,
        ge=VNC_REQUEST_TTL_MIN,
        le=VNC_REQUEST_TTL_MAX,
        description="Informational ticket-fetch window in seconds; backend owns create/delete",
    )


class VNCRequestStatus(BaseModel):
    """Status for a VNCRequest CR."""

    ready: bool | None = None
    caCert: str | None = None
    vncport: int | None = None
    ticket: str | None = None
    password: str | None = None
    vmid: str | None = None
    nodeName: str | None = None
    proxmoxIp: str | None = None
    proxmoxPort: int | None = None
    error: str | None = None
