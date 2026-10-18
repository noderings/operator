"""Tests for CreateOperationHandler name-based create identity."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from proxmox_operator.client import ProxmoxError
from proxmox_operator.operations import CreateOperationHandler
from proxmox_operator.types import (
    CPUConfig,
    DiskConfig,
    NetworkConfig,
    Operation,
    ProxmoxNodeRef,
    ProxmoxVMSpec,
    VMConfig,
)


@pytest.fixture()
def create_spec() -> ProxmoxVMSpec:
    return ProxmoxVMSpec(
        proxmoxNodeRef=ProxmoxNodeRef(name="node-1"),
        vmidSeed=11000,
        operation=Operation(type="created"),
        vmConfig=VMConfig(
            templateId="14002",
            name="NR-44d3f84e",
            nodeName="proxmox01-lab01-dev",
            cpu=CPUConfig(cores=1),
            memory="1G",
            disks=[DiskConfig(name="rootdisk", storage="local-lvm", size="20G")],
            networks=[NetworkConfig(name="net0", bridge="vmbr0")],
        ),
    )


def _handler(
    client: MagicMock,
    create_spec: ProxmoxVMSpec,
    *,
    status: dict[str, Any] | None = None,
) -> CreateOperationHandler:
    return CreateOperationHandler(
        client=client,
        spec=create_spec,
        patch=MagicMock(),
        meta={"generation": 1},
        logger=MagicMock(),
        status=status or {},
    )


def test_execute_skips_clone_when_name_exists(create_spec: ProxmoxVMSpec) -> None:
    client = MagicMock()
    client.find_vm_by_name.return_value = 11004
    handler = _handler(client, create_spec, status={"vmId": "11001"})

    message, vmid = handler.execute()

    assert message == "VM already exists"
    assert vmid == 11004
    client.find_vm_by_name.assert_called_once_with("proxmox01-lab01-dev", "NR-44d3f84e")
    client.clone_template.assert_not_called()
    client.get_next_vmid.assert_not_called()


def test_execute_clones_when_name_absent(create_spec: ProxmoxVMSpec) -> None:
    client = MagicMock()
    client.find_vm_by_name.return_value = None
    handler = _handler(client, create_spec, status={"vmId": "11001"})
    handler._clone_and_configure_vm = MagicMock(return_value=("Creating VM", 11002))  # type: ignore[method-assign]

    message, vmid = handler.execute()

    assert message == "Creating VM"
    assert vmid == 11002
    client.find_vm_by_name.assert_called_once()
    handler._clone_and_configure_vm.assert_called_once()


def test_execute_raises_when_name_lookup_hits_api_error(create_spec: ProxmoxVMSpec) -> None:
    client = MagicMock()
    client.find_vm_by_name.side_effect = ProxmoxError(
        "Failed to find VM by name 'NR-44d3f84e' on node proxmox01-lab01-dev: SSL EOF"
    )
    handler = _handler(client, create_spec, status={"vmId": "11001"})

    with pytest.raises(ProxmoxError, match="Failed to find VM by name"):
        handler.execute()

    client.clone_template.assert_not_called()
