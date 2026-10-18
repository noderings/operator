"""Pydantic models for VirtFusion operator CRDs."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field
from noderings_hooks.types import LastHookStatus, VMHooks

from .constants import VNC_REQUEST_TTL_DEFAULT

# CRD identity — VirtFusionVM
VM_PLURAL: str = "virtfusionvms"
VM_KIND: str = "VirtFusionVM"

# CRD identity — VirtFusionControl
CONTROL_PLURAL: str = "virtfusioncontrols"
CONTROL_KIND: str = "VirtFusionControl"

# CRD identity — VirtFusionNode
NODE_PLURAL: str = "virtfusionnodes"
NODE_KIND: str = "VirtFusionNode"

# CRD identity — VirtFusionVNCRequest
VNC_PLURAL: str = "virtfusionvncrequests"
VNC_KIND: str = "VirtFusionVNCRequest"

OperationType = Literal[
    "created",
    "started",
    "stopped",
    "restarted",
    "reinstall",
    "reinstalled",
    "password",
    "suspended",
    "unsuspend",
    "deleted",
    "extra_ipv4",
    "remove_ipv4",
    "package_change",
]


class Operation(BaseModel):
    type: OperationType = "created"
    requestedAt: Optional[str] = None


class VirtFusionNodeRef(BaseModel):
    name: str = Field(..., min_length=1)


class VirtFusionVMSpec(BaseModel):
    virtfusionNodeRef: VirtFusionNodeRef
    packageId: int
    operatingSystemId: int
    userExtRelationId: str
    hostname: str | None = None
    name: str | None = None
    cpuCores: int | None = None
    memoryMb: int | None = None
    diskGb: int | None = None
    networkSpeedInKBs: int | None = None
    networkSpeedOutKBs: int | None = None
    cpuThrottlePercent: int | None = None
    ipv4: list[str] | None = None
    sshPublicKeys: list[str] | None = None
    ipv6: bool = False
    guestAdminUser: str | None = None
    blockOutboundSmtp: bool = Field(
        False,
        description="Provider SMTP outbound block via a VirtFusion firewall ruleset named smtp.",
    )
    operation: Operation | None = None
    hooks: VMHooks | None = None


class VirtFusionVMStatus(BaseModel):
    lastHook: LastHookStatus | None = None


class VirtFusionControlSpec(BaseModel):
    endpoint: str = Field(..., min_length=1)


class VirtFusionNodeSpec(BaseModel):
    hypervisorId: int
    hypervisorGroupId: int
    controlRef: VirtFusionNodeRef
    maintenanceMode: bool = False


class VirtFusionVMRef(BaseModel):
    name: str


class VirtFusionVNCRequestSpec(BaseModel):
    vmRef: VirtFusionVMRef
    ttl: int = VNC_REQUEST_TTL_DEFAULT
