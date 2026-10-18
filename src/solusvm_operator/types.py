"""Pydantic models for SolusVM 2 operator CRDs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from noderings_hooks.types import LastHookStatus, VMHooks

from .constants import VNC_REQUEST_TTL_DEFAULT

# CRD identity — SolusVMVM
VM_PLURAL: str = "solusvmvms"
VM_KIND: str = "SolusVMVM"

# CRD identity — SolusVMControl
CONTROL_PLURAL: str = "solusvmcontrols"
CONTROL_KIND: str = "SolusVMControl"

# CRD identity — SolusVMNode
NODE_PLURAL: str = "solusvmnodes"
NODE_KIND: str = "SolusVMNode"

# CRD identity — SolusVMVNCRequest
VNC_PLURAL: str = "solusvmvncrequests"
VNC_KIND: str = "SolusVMVNCRequest"

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
    "plan_change",
]


class Operation(BaseModel):
    type: OperationType = "created"
    requestedAt: str | None = None


class SolusVMNodeRef(BaseModel):
    name: str = Field(..., min_length=1)


class SolusVMVMSpec(BaseModel):
    solusvmNodeRef: SolusVMNodeRef
    planId: int
    osImageVersionId: int
    locationId: int
    computeResourceId: int
    userEmail: str
    hostname: str | None = None
    name: str | None = None
    cpuCores: int | None = None
    memoryMb: int | None = None
    diskGb: int | None = None
    primaryIP: str | None = None
    additionalIPv4: list[str] | None = None
    ipv6: bool = False
    sshPublicKeys: list[str] | None = None
    userData: str | None = None
    operation: Operation | None = None
    hooks: VMHooks | None = None


class SolusVMVMStatus(BaseModel):
    lastHook: LastHookStatus | None = None


class SolusVMControlSpec(BaseModel):
    endpoint: str = Field(..., min_length=1)


class SolusVMControlRef(BaseModel):
    name: str = Field(..., min_length=1)


class SolusVMNodeSpec(BaseModel):
    computeResourceId: int
    locationId: int
    controlRef: SolusVMControlRef
    maintenanceMode: bool = False


class SolusVMVMRef(BaseModel):
    name: str


class SolusVMVNCRequestSpec(BaseModel):
    vmRef: SolusVMVMRef
    ttl: int = VNC_REQUEST_TTL_DEFAULT
